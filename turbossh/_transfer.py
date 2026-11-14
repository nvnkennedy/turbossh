"""How TurboSSH writes a file it transfers: ONE policy for SSHHandler.push /
pull, the file browser's transfers and the remote editor. No Qt here — it takes
a Paramiko SFTPClient (or anything with the same methods) and plain callbacks.

Uploads (:func:`put_file`, :func:`put_data`):

* Nothing there yet: written under a temporary name in the same folder and
  renamed into place, so a dropped link never leaves a partial file under the
  real name (and costs no more round trips than writing it directly did).
* A regular file with one link: the same, renamed over it, keeping its mode AND
  its owner / group (a root push used to leave a service's 0600 config
  root-owned, i.e. unreadable to the service).
* Written in place, as 2.0 did, wherever replacing would change what the file
  is or cannot be done: more than one hard link, not a regular file
  (``/dev/null``, a FIFO, a block device), a folder we may not create files in,
  an owner or mode the new copy may not be given, a file that cannot be
  replaced (in use, a mount point such as a bind-mounted /etc/resolv.conf), or
  no room for a second copy (the server's "Failure" while the temporary copy
  is written) when the new data fits where the old file is. The caller is told
  why (the return value). A new file that would not fit fails, and the old
  file is kept.

The temporary copy gets the old file's mode before any data is written to it
(a 0600 secret was world-readable while it uploaded).

A symbolic link is resolved by the server (realpath), so the file it points at
is the one replaced — the link stays a link. Temporary names are short:
``.<name>.<n>.tsp``, the first n (0, 1, …) not taken, created exclusively, so
every attempt has a name of its own — two transfers to one target never write
or remove each other's temporary file. A temporary file left by an
interrupted run is skipped while it may still be in use, and removed by a
later transfer to that target once it has not changed for :data:`STALE_AFTER`
seconds, so they do not pile up.

Downloads (:func:`get_file`) go to ``.<name>.<n>.tsp`` next to the target the
same way and are moved over it once complete; a target another program holds
open without delete sharing (Windows) is written in place instead.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import io
import os
import posixpath
import re
import shutil
import socket
import stat
import threading
import time
import weakref
from typing import Optional

import paramiko

TEMP_SUFFIX = ".tsp"             # a file still being written
ASIDE_SUFFIX = ".turbossh-old"   # the old file, moved aside for a moment (servers
                                 # without posix-rename)
PREFETCH = 64                    # read requests in flight per download (as OpenSSH's
                                 # sftp): Paramiko's default asks for the WHOLE file
                                 # at once, which on a slow link outlasts the SFTP
                                 # timeout and swamps small embedded servers
UNKNOWN = object()               # "what is at the target has not been looked at"
STALE_AFTER = 10 * 60            # s a temporary file another run left must have been
                                 # unchanged before it is taken for abandoned

_CHUNK = 32768
_SLOTS = 8                       # temporary names .<name>.0.tsp … .7.tsp, then a random one
_NAME_MAX = 255                  # bytes in one file name on most file systems
_WIN_PATH_MAX = 259              # characters in a path without long-path support
_LISTING_TTL = 5.0               # s a folder's link counts are reused for
_S_IFMT, _S_IFDIR, _S_IFLNK, _S_IFREG = 0o170000, 0o040000, 0o120000, 0o100000
_NO_ROOM = {None, errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
_MODE_STRING = re.compile(r"^[-a-zA-Z?][-rwxsStTlL?]{9}")


def _get_takes_prefetch_limit() -> bool:
    """SFTPClient.get grew max_concurrent_prefetch_requests in Paramiko 3.3."""
    try:
        return ("max_concurrent_prefetch_requests"
                in inspect.signature(paramiko.SFTPClient.get).parameters)
    except (TypeError, ValueError):
        return False


GET_TAKES_PREFETCH_LIMIT = _get_takes_prefetch_limit()


def file_type(mode) -> int:
    """The file-type bits of an SFTP st_mode, 0 if unknown (stat.S_ISDIR raises
    for None, and on Windows for the modes above 0xFFFF that QNX servers send)."""
    try:
        return int(mode or 0) & _S_IFMT
    except (TypeError, ValueError):
        return 0


def _perm(mode) -> Optional[int]:
    try:
        return None if mode is None else int(mode) & 0o7777
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Temporary names
# --------------------------------------------------------------------------- #
def _encoded_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _hash8(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def temp_name(name: str, suffix: str = TEMP_SUFFIX, slot=0) -> str:
    """``.<name>.<slot><suffix>``: the temporary name number *slot* of a target
    (see :func:`_open_temp`; the number always comes last, so no other
    target's names can look the same). A name too long to take the extra
    characters is cut, and a hash of the whole name keeps it apart from its
    neighbours'."""
    name = str(name)
    tail = f".{slot}{suffix}"
    full = "." + name + tail
    if _encoded_len(full) <= _NAME_MAX:
        return full
    tail = "~" + _hash8(name) + tail
    keep = name[:_NAME_MAX]
    while keep and _encoded_len("." + keep + tail) > _NAME_MAX:
        keep = keep[:-1]
    return "." + keep + tail


def _short_name(name: str, suffix: str, slot=0) -> str:
    return ".~" + _hash8(str(name)) + f".{slot}" + suffix


def remote_temp(path: str, suffix: str = TEMP_SUFFIX, slot=0) -> str:
    """The temporary name number *slot* next to the remote *path*. A Windows
    server without long-path support refuses paths over 259 characters: where
    the usual name would cross that, a short hashed one is used."""
    folder, name = posixpath.split(path)
    tmp = posixpath.join(folder, temp_name(name, suffix, slot))
    if len(tmp) > _WIN_PATH_MAX >= len(path):
        short = posixpath.join(folder, _short_name(name, suffix, slot))
        if len(short) < len(tmp):
            tmp = short
    return tmp


def local_temp(path: str, slot=0) -> str:
    """The temporary name number *slot* next to the local *path* (kept within
    MAX_PATH on Windows, where a few extra characters broke downloads into deep
    folders)."""
    folder, name = os.path.split(path)
    tmp = os.path.join(folder, temp_name(name, TEMP_SUFFIX, slot))
    if (os.name == "nt" and not tmp.startswith("\\\\?\\")
            and len(os.path.abspath(tmp)) > _WIN_PATH_MAX):
        short = os.path.join(folder, _short_name(name, TEMP_SUFFIX, slot))
        if len(short) < len(tmp):
            tmp = short
    return tmp


def _slots():
    """The temporary-name numbers to try, in order: a few small ones (so names
    stay short and what an interrupted run left is found again), then — every
    one taken by transfers still going on — a random one."""
    yield from range(_SLOTS)
    yield os.urandom(3).hex()


# --------------------------------------------------------------------------- #
# Per-client caches (one entry per SFTP client, gone with it)
# --------------------------------------------------------------------------- #
_CACHE_LOCK = threading.Lock()
_LINKS = weakref.WeakKeyDictionary()     # client -> {folder: (when, {name: links})}
_OWNERS = weakref.WeakKeyDictionary()    # client -> {folder: (uid, gid, perm)}
_NO_POSIX_RENAME = weakref.WeakKeyDictionary()   # client -> True


def _per_client(table, sftp) -> dict:
    with _CACHE_LOCK:
        try:
            per = table.get(sftp)
            if per is None:
                per = table[sftp] = {}
            return per
        except TypeError:                # cannot be weakly referenced: no cache
            return {}


def _flag(table, sftp) -> bool:
    with _CACHE_LOCK:
        try:
            return bool(table.get(sftp, False))
        except TypeError:
            return False


def _set_flag(table, sftp) -> None:
    with _CACHE_LOCK:
        try:
            table[sftp] = True
        except TypeError:
            pass


# --------------------------------------------------------------------------- #
# What is at the target
# --------------------------------------------------------------------------- #
S_IFLNK = _S_IFLNK


def stat_or_none(sftp, path: str):
    """``sftp.stat(path)`` (links followed), or None if nothing is there; a
    timeout is raised, not taken for "nothing"."""
    try:
        return sftp.stat(path)
    except socket.timeout:
        raise
    except IOError:
        return None


def resolve(sftp, path: str):
    """``(path, attr)``: *path* with a symbolic link resolved by the server
    (realpath — a lexical join got ``..`` wrong in symlinked folders), and the
    lstat of what is there (None: nothing). A link the server does not resolve
    (dangling, or a server whose realpath only tidies the string) is returned
    as the link itself; it is then written through, as 2.0 did."""
    try:
        attr = sftp.lstat(path)
    except socket.timeout:
        raise
    except IOError:
        return path, None
    if file_type(getattr(attr, "st_mode", None)) != _S_IFLNK:
        return path, attr
    try:
        real = sftp.normalize(path)
        real_attr = sftp.lstat(real)
    except socket.timeout:
        raise
    except (IOError, paramiko.SFTPError):    # no realpath, or a strange answer
        return path, attr
    if file_type(getattr(real_attr, "st_mode", None)) == _S_IFLNK:
        return path, attr
    return real, real_attr


def link_count(attr) -> Optional[int]:
    """The hard links of a listing entry, read from its ``ls -l`` style
    longname (SFTP v3 has no field for it; OpenSSH and most servers fill the
    longname in). None when unknown — an lstat result has no longname."""
    text = getattr(attr, "longname", None)
    if not text:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    parts = str(text).split(None, 2)
    if len(parts) < 2 or not _MODE_STRING.match(parts[0]) or not parts[1].isdigit():
        return None
    return int(parts[1])


def _listing(sftp, folder: str):
    """The entries of *folder* (with longnames). Paramiko's listdir_iter keeps a
    few READDIR requests in flight, so a large folder costs a round trip or two
    instead of one per hundred entries; it is read to the end, which also closes
    the folder handle."""
    folder = folder or "."
    if isinstance(sftp, paramiko.SFTPClient):
        return list(sftp.listdir_iter(folder, read_aheads=8))
    lister = getattr(sftp, "listdir_attr", None)
    return list(lister(folder)) if lister is not None else []


def _links_of(sftp, path: str) -> Optional[int]:
    """The hard links of the file *path* — one listing of its folder, reused for
    a few seconds (a script pushing file after file into one folder)."""
    folder, name = posixpath.split(path)
    per = _per_client(_LINKS, sftp)
    hit = per.get(folder)
    if hit is None or time.monotonic() - hit[0] > _LISTING_TTL:
        try:
            entries = _listing(sftp, folder)
        except socket.timeout:
            raise
        except (IOError, EOFError, paramiko.SSHException):
            return None                  # an unreadable folder: not known
        counts = {}
        for entry in entries:
            n = link_count(entry)
            if n is not None:
                counts[getattr(entry, "filename", None)] = n
        hit = (time.monotonic(), counts)
        per[folder] = hit
    return hit[1].get(name)


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #
class _InPlace(IOError):
    """Replacing the file cannot be done here; it is written in place (why).
    An IOError, so a caller of :func:`rename_over` that does not write in place
    sees it as the failure it is."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def _remove_stale(sftp, path: str) -> None:
    """Remove what an interrupted run left at *path* (a link is unlinked, never
    followed). A timeout is raised: the client is out of step after it."""
    try:
        sftp.remove(path)
    except socket.timeout:
        raise
    except (IOError, OSError):
        pass


def _lstat_or_none(sftp, path: str):
    try:
        return sftp.lstat(path)
    except socket.timeout:
        raise
    except (IOError, AttributeError):
        return None


def _remove_abandoned(sftp, fh, taken) -> None:
    """Remove the temporary files in *taken* (``(path, lstat)`` pairs: names
    another run holds) that have not changed for :data:`STALE_AFTER` seconds —
    measured by the server's clock, which our own new file *fh* tells (an
    embedded target's clock may be years off ours). A transfer still going on
    keeps its file's time current and is never touched."""
    try:
        now = getattr(fh.stat(), "st_mtime", None)
    except socket.timeout:
        raise
    except (IOError, AttributeError):
        return
    for path, attr in taken:
        when = getattr(attr, "st_mtime", None)
        if now is not None and when is not None and now - when > STALE_AFTER:
            _remove_stale(sftp, path)


def _cleanup(sftp, path: str, exc: BaseException) -> None:
    """Remove a temporary file after a failure — not after a timeout (the client
    is out of step with the server then, and is dropped by its owner)."""
    if isinstance(exc, socket.timeout):
        return
    try:
        sftp.remove(path)
    except Exception:
        pass


def _close_quietly(fh) -> None:
    try:
        fh.close()
    except Exception:
        pass


def _open_temp(sftp, path: str, suffix: str = TEMP_SUFFIX):
    """Create a temporary file of this attempt's own next to *path*: the first
    of ``.<name>.0.tsp``, ``.<name>.1.tsp``, … that is not taken, created
    exclusively — never through a link planted under that name, never into
    another transfer's file (two uploads to one target used to share one name:
    one removed the other's file, or renamed it half-written over the target).
    A name that is taken is skipped, whoever holds it (a transfer going on, or
    a server session a VPN drop orphaned); the ones that are clearly abandoned
    are removed afterwards (see :func:`_remove_abandoned`). Returns ``(handle,
    temporary path, slot)``; raises the server's IOError when the file cannot
    be created for another reason than a taken name."""
    taken = []
    for slot in _slots():
        tmp = remote_temp(path, suffix, slot)
        try:
            fh = sftp.open(tmp, "wbx")
        except socket.timeout:
            raise
        except IOError:
            attr = _lstat_or_none(sftp, tmp)
            if attr is None:                 # not taken: refused for another reason
                raise
            taken.append((tmp, attr))
            continue
        if taken:
            try:
                _remove_abandoned(sftp, fh, taken)
            except BaseException as exc:
                if not isinstance(exc, socket.timeout):   # else: out of step
                    _close_quietly(fh)
                    _remove_stale(sftp, tmp)
                raise
        return fh, tmp, slot
    raise IOError(errno.EEXIST, f"no temporary name next to {path} is free")


def _collect_replies(fh) -> bool:
    """Read the server's replies to the pipelined writes still outstanding.
    Paramiko files them under no owner and drops them — a write the server
    refused (a full disk) went unnoticed, which is why its put() stats the file
    afterwards. Reading them costs no extra round trip: they arrive before the
    reply to the close anyway. False if this client offers no way to (then the
    size is checked with a stat instead)."""
    reqs = getattr(fh, "_reqs", None)
    read = getattr(getattr(fh, "sftp", None), "_read_response", None)
    if reqs is None or read is None:
        return False
    while reqs:
        read(reqs.popleft())             # raises the IOError of a refused write
    return True


def _send(fh, fl, size: int, callback):
    """Copy *fl* into the open remote file *fh* and close it. Returns
    ``(bytes sent, error, acknowledged)``: *error* is the IOError the SERVER
    reported for a write (the file is closed then too); *acknowledged* says
    every write was confirmed. Anything else — a local read error, the callback
    aborting, a dropped link, a timeout — is raised as it is."""
    done = 0
    try:
        pipelined = getattr(fh, "set_pipelined", None)
        if pipelined is not None:
            pipelined(True)
        while True:
            data = fl.read(_CHUNK)
            if not data:
                break
            try:
                fh.write(data)
            except socket.timeout:
                raise
            except IOError as exc:
                _close_quietly(fh)
                return done, exc, False
            done += len(data)
            if callback is not None:
                callback(done, size)
        try:
            flush = getattr(fh, "flush", None)
            if flush is not None:
                flush()
            acknowledged = _collect_replies(fh)
        except socket.timeout:
            raise
        except IOError as exc:
            _close_quietly(fh)
            return done, exc, False
    except BaseException as exc:
        # a timed-out client is out of step: send nothing more on it (closing
        # would wait for another timeout)
        if not isinstance(exc, socket.timeout):
            _close_quietly(fh)
        raise
    try:
        fh.close()
    except socket.timeout:
        raise
    except IOError as exc:
        return done, exc, False
    return done, None, acknowledged


def _confirm(sftp, path: str, done: int) -> None:
    size = getattr(sftp.stat(path), "st_size", None)
    if size is not None and size != done:
        raise IOError(f"only {size} of {done} bytes reached {path}")


def _temp_owner(sftp, fh, folder: str):
    """(uid, gid, perm) a new file of ours gets in *folder* — one fstat per
    folder and client (the same for every file there)."""
    per = _per_client(_OWNERS, sftp)
    hit = per.get(folder)
    if hit is None:
        fstat = getattr(fh, "stat", None)
        if fstat is None:
            return None
        try:
            st = fstat()
        except socket.timeout:
            raise
        except IOError:
            return None
        hit = (getattr(st, "st_uid", None), getattr(st, "st_gid", None),
               _perm(getattr(st, "st_mode", None)))
        per[folder] = hit
    return hit


def _rename_unsupported(exc) -> bool:
    text = str(exc).lower()
    return getattr(exc, "errno", None) is None and ("unsupported" in text
                                                    or "not supported" in text)


_CANNOT_REPLACE = "it cannot be replaced (in use, or a mount point)"


def rename_over(sftp, tmp: str, dst: str, existed: bool = True, *,
                aside: Optional[str] = None) -> None:
    """Move the finished *tmp* to *dst*. posix-rename replaces in one step. A
    server without it (whose plain rename never replaces) gets the old file
    moved aside (to *aside*, default ``.<name>.0.turbossh-old``) first and put
    back if the new one cannot take its place — the content of *dst* is never
    lost on the way.

    An existing *dst* that cannot be replaced — the server refuses the rename
    for another reason than not knowing posix-rename: a bind-mounted
    /etc/resolv.conf (EBUSY), a Windows file held open without delete sharing
    — is left untouched and :class:`_InPlace` raised (an IOError), so the
    caller writes it in place, as 2.0 did."""
    if not _flag(_NO_POSIX_RENAME, sftp):
        try:
            sftp.posix_rename(tmp, dst)
            return
        except socket.timeout:
            raise
        except IOError as exc:
            if _rename_unsupported(exc):
                _set_flag(_NO_POSIX_RENAME, sftp)    # asked once per client
            elif existed:
                raise _InPlace(f"{_CANNOT_REPLACE}: {exc}") from None
    if not existed:
        sftp.rename(tmp, dst)
        return
    aside = aside or remote_temp(dst, ASIDE_SUFFIX)
    try:
        sftp.rename(dst, aside)
    except socket.timeout:
        raise
    except IOError:
        _remove_stale(sftp, aside)       # a stray copy an earlier replace left
        try:
            sftp.rename(dst, aside)
        except socket.timeout:
            raise
        except IOError as exc:
            # nothing there to move aside after all (removed meanwhile): the
            # new file takes the free name; or it cannot be moved — untouched
            try:
                sftp.rename(tmp, dst)
            except socket.timeout:
                raise
            except IOError:
                raise _InPlace(f"{_CANNOT_REPLACE}: {exc}") from None
            return
    try:
        sftp.rename(tmp, dst)
    except BaseException:
        try:
            sftp.rename(aside, dst)
        except Exception:
            pass
        raise
    try:
        sftp.remove(aside)
    except Exception:
        pass                             # a stray copy of the old file; nothing lost


def _chmod(sftp, path: str, perm: int) -> bool:
    """chmod; False if the server refused it (a timeout is raised)."""
    try:
        sftp.chmod(path, perm)
        return True
    except socket.timeout:
        raise
    except IOError:
        return False


def _early_mode(perm: int) -> int:
    """The mode a temporary copy gets BEFORE its data is written: the final
    one, but writable by its owner (a read-only mode could keep the copy from
    being written or removed again on some servers). Nobody else gets
    anything the final mode does not give them."""
    return perm | 0o200


def _late_mode(sftp, path: str, perm: int, early: int) -> None:
    """After the data: the exact final mode, where it differs from *early* —
    no owner write, or setuid / setgid / sticky bits (a write clears setuid,
    so they are set last). A refusal keeps *early*; the data is safe."""
    if perm != early or perm & 0o7000:
        _chmod(sftp, path, perm)


def _no_room_maybe(exc) -> bool:
    """Could this write failure be a full disk / quota? SFTP v3 has no code
    for it: ENOSPC arrives as a plain "Failure" (no errno), like EIO, EFBIG
    and others that cannot be told apart."""
    return getattr(exc, "errno", None) in _NO_ROOM and not _rename_unsupported(exc)


def _size_or_zero(sftp, path: str) -> int:
    try:
        return int(getattr(sftp.stat(path), "st_size", 0) or 0)
    except socket.timeout:
        raise
    except (IOError, TypeError, ValueError):
        return 0


def _create(sftp, fl, size, path, callback, mode) -> None:
    """Nothing at *path*: write a temporary file and rename it into place."""
    fh, tmp, _slot = _open_temp(sftp, path)
    try:
        perm = _perm(mode)
        if perm is not None:                 # before the data (see _early_mode)
            _chmod(sftp, tmp, _early_mode(perm))   # refused: the server's default
        done, err, acknowledged = _send(fh, fl, size, callback)
        if err is not None:
            raise err
        if not acknowledged:
            _confirm(sftp, tmp, done)
        if perm is not None:
            _late_mode(sftp, tmp, perm, _early_mode(perm))
        rename_over(sftp, tmp, path, existed=False)
    except BaseException as exc:
        if not isinstance(exc, socket.timeout):
            _close_quietly(fh)
        _cleanup(sftp, tmp, exc)
        raise


def _replace(sftp, fl, size, path, old, callback) -> None:
    """A regular file with one link at *path*: write a temporary copy with its
    owner, group and mode, then rename it over the old file. :class:`_InPlace`
    where that cannot be done (nothing is left behind then)."""
    folder = posixpath.dirname(path)
    try:
        fh, tmp, slot = _open_temp(sftp, path)
    except socket.timeout:
        raise
    except IOError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise _InPlace("the folder is not writable") from None
        raise _InPlace(f"a temporary copy could not be created: {exc}") from None
    try:
        made = _temp_owner(sftp, fh, folder)
        uid, gid = getattr(old, "st_uid", None), getattr(old, "st_gid", None)
        # only where the server reports both owners (one that reports none
        # cannot be compared, and is not asked to change anything)
        if (made is not None and None not in (made[0], made[1], uid, gid)
                and (made[0], made[1]) != (uid, gid)):
            try:
                sftp.chown(tmp, uid, gid)
            except socket.timeout:
                raise
            except IOError:
                raise _InPlace(f"the new copy could not be given its owner "
                               f"{uid}:{gid}") from None
        # The old file's mode BEFORE the data: the copy was created with the
        # server's default (0644), and a 0600 secret was world-readable while
        # it uploaded. A server that refuses it gets the file written in place,
        # which keeps its mode (the copy would not have it).
        want = _perm(getattr(old, "st_mode", None))
        early = None if want is None else _early_mode(want)
        if early is not None and (made is None or made[2] != early):
            if not _chmod(sftp, tmp, early):
                raise _InPlace(f"the new copy could not be given its mode "
                               f"{want:o}")
        done, err, acknowledged = _send(fh, fl, size, callback)
        if err is None and not acknowledged:
            try:
                _confirm(sftp, tmp, done)
            except socket.timeout:
                raise
            except IOError as exc:
                err = exc
        if err is not None:
            if _no_room_maybe(err):
                # ENOSPC is a plain "Failure" in SFTP v3: replacing needs room
                # for two copies, and 2.0 truncated and rewrote the one. That
                # is done only where the new data fits: in the old file's room
                # plus what the copy took (freed again). Otherwise the old file
                # is kept as it is — written in place, a bigger new one would
                # truncate it and stop part-way.
                room = (int(getattr(old, "st_size", 0) or 0)
                        + _size_or_zero(sftp, tmp))
                if size <= room:
                    raise _InPlace(f"no room for a second copy ({err})")
                why = (f"{err} — while writing a copy of {path}: probably no "
                       f"room for its {size} bytes; {path} was left as it was")
                code = getattr(err, "errno", None)
                raise (IOError(code, why) if code is not None else IOError(why)) \
                    from err
            raise err
        if want is not None:
            _late_mode(sftp, tmp, want, early)
        rename_over(sftp, tmp, path, existed=True,
                    aside=remote_temp(path, ASIDE_SUFFIX, slot))
    except BaseException as exc:
        if not isinstance(exc, socket.timeout):
            _close_quietly(fh)
        _cleanup(sftp, tmp, exc)
        raise


def _write_in_place(sftp, fl, size, path, callback, regular: bool) -> None:
    fh = sftp.open(path, "wb")
    done, err, acknowledged = _send(fh, fl, size, callback)
    if err is not None:
        raise err
    if regular and not acknowledged:
        _confirm(sftp, path, done)


def put_stream(sftp, fl, size: int, remote: str, *, callback=None,
               existing=UNKNOWN, mode=None) -> str:
    """Write the seekable *fl* (*size* bytes) to *remote* by the policy above.

    *existing* is what is at *remote* when the caller knows it — a listing entry
    (it carries the hard-link count) or None for nothing — else it is looked up.
    *mode* is the mode for a file that did not exist yet. ``callback(done,
    total)`` reports progress; raising from it aborts. Returns ``""``, or
    ``"in place (why)"`` when the file was written in place."""
    if existing is UNKNOWN or (existing is not None and file_type(
            getattr(existing, "st_mode", None)) == _S_IFLNK):
        remote, existing = resolve(sftp, remote)
    if existing is None:
        _create(sftp, fl, size, remote, callback, mode)
        return ""
    kind = file_type(getattr(existing, "st_mode", None))
    if kind == _S_IFDIR:
        raise IOError(errno.EISDIR, f"{remote} is a folder")
    regular = kind in (0, _S_IFREG)
    if kind == _S_IFLNK:
        why = "through the symbolic link, which the server did not resolve"
    elif not regular:
        why = "it is not a regular file"
    else:
        links = link_count(existing)
        if links is None:
            links = _links_of(sftp, remote)
        why = f"it has {links} hard links" if links and links > 1 else None
    if why is None:
        start = fl.tell()
        try:
            _replace(sftp, fl, size, remote, existing, callback)
            return ""
        except _InPlace as fallback:
            why = fallback.why
        fl.seek(start)
    _write_in_place(sftp, fl, size, remote, callback, regular)
    return f"in place ({why})"


def put_file(sftp, local: str, remote: str, *, callback=None, existing=UNKNOWN,
             mode=None) -> str:
    """Upload the local file *local* to *remote* (see :func:`put_stream`)."""
    size = os.stat(local).st_size
    with open(local, "rb") as fl:
        return put_stream(sftp, fl, size, remote, callback=callback,
                          existing=existing, mode=mode)


def put_data(sftp, data: bytes, remote: str, *, existing=UNKNOWN, mode=None) -> str:
    """Write *data* as *remote* (see :func:`put_stream`)."""
    return put_stream(sftp, io.BytesIO(data), len(data), remote, existing=existing,
                      mode=mode)


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #
def remove_local(path: str) -> None:
    """Remove a local file if it is there — on Windows a read-only one too (its
    read-only bit is cleared first)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except PermissionError:
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            os.remove(path)
        except OSError:
            pass
    except OSError:
        pass


def move_into_place(tmp: str, target: str) -> str:
    """Move the finished download *tmp* over *target*, keeping *target*'s mode.
    Windows refuses the rename while another program holds *target* open without
    delete sharing (a log viewer): the complete copy is then written over it in
    place. Returns ``""`` or ``"in place (why)"``."""
    try:
        mode = os.stat(target).st_mode
    except OSError:
        mode = None
    try:
        os.replace(tmp, target)
    except PermissionError:
        if not os.path.isfile(target):
            raise
        shutil.copyfile(tmp, target)
        remove_local(tmp)
        return "in place (another program holds it open)"
    if mode is not None:
        try:
            os.chmod(target, stat.S_IMODE(mode))
        except OSError:
            pass
    return ""


def _can_stop_prefetch() -> bool:
    """Paramiko starts a download's read-ahead with SFTPFile._start_prefetch
    (chunks, max_concurrent_requests) on a thread that walks *chunks*; given an
    iterable that can be stopped, a cancelled download sends no more reads."""
    fn = getattr(paramiko.SFTPFile, "_start_prefetch", None)
    try:
        return fn is not None and list(inspect.signature(fn).parameters)[1:3] == [
            "chunks", "max_concurrent_requests"]
    except (TypeError, ValueError):
        return False


_CAN_STOP_PREFETCH = _can_stop_prefetch()


class _ReadAhead:
    """The (offset, length) reads of a download's read-ahead, handed out until
    :meth:`stop` — Paramiko's own list made its thread go on sending reads for
    the whole file after a cancelled download had closed it."""

    def __init__(self, start: int, size: int, step: int):
        self.start, self.size, self.step = start, size, step
        self.stopped = False

    def __iter__(self):
        n = self.start
        while n < self.size and not self.stopped:
            length = min(self.step, self.size - n)
            yield n, length
            n += length

    def stop(self) -> None:
        self.stopped = True


def _get_stoppable(sftp, remote: str, local: str, callback) -> None:
    """SFTPClient.get (Paramiko 3.3+), with at most :data:`PREFETCH` reads in
    flight and the read-ahead stopped when the download ends early."""
    file_size = sftp.stat(remote).st_size
    with open(local, "wb") as fl:
        with sftp.open(remote, "rb") as fr:
            ahead = None
            if file_size:                # (no read-ahead for nothing: it would wait)
                ahead = _ReadAhead(0, file_size,
                                   getattr(fr, "MAX_REQUEST_SIZE", _CHUNK))
                fr._start_prefetch(ahead, PREFETCH)
            try:
                size = 0
                while True:
                    data = fr.read(_CHUNK)
                    fl.write(data)
                    size += len(data)
                    if not data:
                        break
                    if callback is not None:
                        callback(size, file_size)
            finally:
                if ahead is not None:
                    ahead.stop()
    written = os.stat(local).st_size
    if written != size:
        raise IOError(f"size mismatch in get!  {written} != {size}")


def sftp_get(sftp, remote: str, local: str, callback=None) -> None:
    """``sftp.get`` with at most :data:`PREFETCH` reads in flight where this
    Paramiko can limit them (and, where it can, no reads sent on after a
    cancelled download)."""
    if GET_TAKES_PREFETCH_LIMIT and isinstance(sftp, paramiko.SFTPClient):
        if _CAN_STOP_PREFETCH:
            _get_stoppable(sftp, remote, local, callback)
        else:
            sftp.get(remote, local, callback=callback,
                     max_concurrent_prefetch_requests=PREFETCH)
    else:
        sftp.get(remote, local, callback=callback)


def _abandoned_local(path: str) -> bool:
    try:
        return time.time() - os.lstat(path).st_mtime > STALE_AFTER
    except OSError:
        return False


def _reserve_local_temp(target: str) -> str:
    """Create (exclusively, empty) the first free temporary name next to the
    local *target* — ``.<name>.0.tsp``, ``.<name>.1.tsp``, … — so two
    downloads to one file never share one (they did: the second removed or
    wrote into the first's). One left by an interrupted run is reused once it
    has not changed for :data:`STALE_AFTER` seconds."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    for slot in _slots():
        tmp = local_temp(target, slot)
        for _ in range(2):
            try:
                os.close(os.open(tmp, flags, 0o666))
                return tmp
            except FileExistsError:
                pass
            except PermissionError:
                # Windows: a name still being deleted, or a read-only leftover
                if not os.path.lexists(tmp):
                    raise
            if not _abandoned_local(tmp):
                break                        # in use: the next name
            remove_local(tmp)                # abandoned: take it over
    raise FileExistsError(errno.EEXIST, f"no temporary name next to {target} "
                                        f"is free")


def get_file(sftp, remote: str, local: str, callback=None):
    """Download *remote* to *local* through ``.<name>.<n>.tsp`` next to it,
    moved into place once complete: a failed or aborted download leaves an
    existing *local* untouched and no partial file behind. Through a local
    symlink, the file it points at is replaced. Returns ``(size, how)`` (see
    :func:`move_into_place`)."""
    target = os.path.realpath(local) if os.path.islink(local) else local
    tmp = _reserve_local_temp(target)
    try:
        sftp_get(sftp, remote, tmp, callback)
        size = os.path.getsize(tmp)
        how = move_into_place(tmp, target)
    except BaseException:
        remove_local(tmp)
        raise
    return size, how
