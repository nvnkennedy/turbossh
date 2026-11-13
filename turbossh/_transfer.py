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
  an owner the new copy may not be given, or no room for a second copy (the
  server's "Failure" while the temporary copy is written). The caller is told
  why (the return value).

A symbolic link is resolved by the server (realpath), so the file it points at
is the one replaced — the link stays a link. Temporary names are short and the
same for every attempt at one target (``.<name>.tsp``): a retry replaces what
an interrupted run left instead of adding one more next to it.

Downloads (:func:`get_file`) go to ``.<name>.tsp`` next to the target and are
moved over it once complete; a target another program holds open without
delete sharing (Windows) is written in place instead.
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

_CHUNK = 32768
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


def temp_name(name: str, suffix: str = TEMP_SUFFIX) -> str:
    """``.<name><suffix>`` — the same for every attempt at one target, so a retry
    replaces what an interrupted run left. A name too long to take the extra
    characters is cut, and a hash of the whole name keeps it apart from its
    neighbours'."""
    name = str(name)
    full = "." + name + suffix
    if _encoded_len(full) <= _NAME_MAX:
        return full
    tail = "~" + _hash8(name) + suffix
    keep = name[:_NAME_MAX]
    while keep and _encoded_len("." + keep + tail) > _NAME_MAX:
        keep = keep[:-1]
    return "." + keep + tail


def _short_name(name: str, suffix: str) -> str:
    return ".~" + _hash8(str(name)) + suffix


def remote_temp(path: str, suffix: str = TEMP_SUFFIX) -> str:
    """The temporary name next to the remote *path*. A Windows server without
    long-path support refuses paths over 259 characters: where the usual name
    would cross that, a short hashed one is used."""
    folder, name = posixpath.split(path)
    tmp = posixpath.join(folder, temp_name(name, suffix))
    if len(tmp) > _WIN_PATH_MAX >= len(path):
        short = posixpath.join(folder, _short_name(name, suffix))
        if len(short) < len(tmp):
            tmp = short
    return tmp


def local_temp(path: str) -> str:
    """The temporary name next to the local *path* (kept within MAX_PATH on
    Windows, where a few extra characters broke downloads into deep folders)."""
    folder, name = os.path.split(path)
    tmp = os.path.join(folder, temp_name(name))
    if (os.name == "nt" and not tmp.startswith("\\\\?\\")
            and len(os.path.abspath(tmp)) > _WIN_PATH_MAX):
        short = os.path.join(folder, _short_name(name, TEMP_SUFFIX))
        if len(short) < len(tmp):
            tmp = short
    return tmp


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
class _InPlace(Exception):
    """Replacing the file cannot be done here; it is written in place (why)."""

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


def _open_temp(sftp, tmp: str):
    """Create *tmp* exclusively — never through a link planted under that name,
    never into someone else's file. A leftover of an interrupted run (same name)
    is removed and the create tried once more. Raises the server's IOError."""
    try:
        return sftp.open(tmp, "wbx")
    except socket.timeout:
        raise
    except IOError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise
    _remove_stale(sftp, tmp)
    return sftp.open(tmp, "wbx")


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


def rename_over(sftp, tmp: str, dst: str, existed: bool = True) -> None:
    """Move the finished *tmp* to *dst*. posix-rename replaces in one step. A
    server without it (whose plain rename never replaces) gets the old file
    moved aside first and put back if the new one cannot take its place — the
    content of *dst* is never lost on the way."""
    if not _flag(_NO_POSIX_RENAME, sftp):
        try:
            sftp.posix_rename(tmp, dst)
            return
        except socket.timeout:
            raise
        except IOError as exc:
            if _rename_unsupported(exc):         # asked once per client
                _set_flag(_NO_POSIX_RENAME, sftp)
    if not existed:
        sftp.rename(tmp, dst)
        return
    aside = remote_temp(dst, ASIDE_SUFFIX)
    try:
        sftp.rename(dst, aside)
    except socket.timeout:
        raise
    except IOError:
        _remove_stale(sftp, aside)       # an older one an interrupted replace left
        try:
            sftp.rename(dst, aside)
        except socket.timeout:
            raise
        except IOError:
            # nothing there to move aside after all (or it cannot be moved, and
            # this fails as well, saying why)
            sftp.rename(tmp, dst)
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


def _create(sftp, fl, size, path, callback, mode) -> None:
    """Nothing at *path*: write a temporary file and rename it into place."""
    tmp = remote_temp(path)
    fh = _open_temp(sftp, tmp)
    try:
        done, err, acknowledged = _send(fh, fl, size, callback)
        if err is not None:
            raise err
        if not acknowledged:
            _confirm(sftp, tmp, done)
        if _perm(mode) is not None:
            try:
                sftp.chmod(tmp, _perm(mode))
            except socket.timeout:
                raise
            except IOError:
                pass                     # the server's default; still a good file
        rename_over(sftp, tmp, path, existed=False)
    except BaseException as exc:
        _cleanup(sftp, tmp, exc)
        raise


def _replace(sftp, fl, size, path, old, callback) -> None:
    """A regular file with one link at *path*: write a temporary copy with its
    owner, group and mode, then rename it over the old file. :class:`_InPlace`
    where that cannot be done (nothing is left behind then)."""
    folder = posixpath.dirname(path)
    tmp = remote_temp(path)
    try:
        fh = _open_temp(sftp, tmp)
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
        done, err, acknowledged = _send(fh, fl, size, callback)
        if err is None and not acknowledged:
            try:
                _confirm(sftp, tmp, done)
            except socket.timeout:
                raise
            except IOError as exc:
                err = exc
        if err is not None:
            if getattr(err, "errno", None) in _NO_ROOM:
                # ENOSPC is a plain "Failure" in SFTP v3: replacing needs room
                # for two copies; 2.0 truncated and rewrote the one
                raise _InPlace(f"no room for a second copy ({err})")
            raise err
        want = _perm(getattr(old, "st_mode", None))
        if want is not None and (made is None or made[2] != want):
            try:
                sftp.chmod(tmp, want)
            except socket.timeout:
                raise
            except IOError:
                pass
        rename_over(sftp, tmp, path, existed=True)
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


def sftp_get(sftp, remote: str, local: str, callback=None) -> None:
    """``sftp.get`` with at most :data:`PREFETCH` reads in flight where this
    Paramiko can limit them."""
    if GET_TAKES_PREFETCH_LIMIT and isinstance(sftp, paramiko.SFTPClient):
        sftp.get(remote, local, callback=callback,
                 max_concurrent_prefetch_requests=PREFETCH)
    else:
        sftp.get(remote, local, callback=callback)


def get_file(sftp, remote: str, local: str, callback=None):
    """Download *remote* to *local* through ``.<name>.tsp`` next to it, moved
    into place once complete: a failed or aborted download leaves an existing
    *local* untouched and no partial file behind. Through a local symlink, the
    file it points at is replaced. Returns ``(size, how)`` (see
    :func:`move_into_place`)."""
    target = os.path.realpath(local) if os.path.islink(local) else local
    tmp = local_temp(target)
    remove_local(tmp)                    # what an interrupted run left
    try:
        sftp_get(sftp, remote, tmp, callback)
        size = os.path.getsize(tmp)
        how = move_into_place(tmp, target)
    except BaseException:
        remove_local(tmp)
        raise
    return size, how
