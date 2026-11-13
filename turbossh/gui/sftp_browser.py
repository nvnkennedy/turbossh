"""Remote file browser over SFTP: navigate, upload, download, mkdir, delete,
rename.

Nothing here blocks the GUI thread. Listings and the small operations run on
worker threads through ONE SFTP client of the browser's own; transfers are queued
and moved one at a time by a worker thread with a second client, so a large file
never holds up browsing. The session's shared ``handler.sftp()`` is never used:
Paramiko can hand one thread's reply to another when two threads share a client,
and the thread that lost its reply then waits forever.

A transfer never writes into the file it replaces: it writes a temporary file
next to it and renames that into place once complete, so a cancel or a dropped
link leaves the original as it was."""

from __future__ import annotations

import os
import re
import socket
import stat
import time
import uuid
import posixpath
import threading
from collections import deque, namedtuple

import paramiko
from paramiko.ssh_exception import SSHException
from PyQt5.QtCore import (QItemSelectionModel, QMimeData, QThread, QTimer,
                          pyqtSignal, Qt)
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QTreeWidget, QTreeWidgetItem, QFileDialog,
                             QInputDialog, QMessageBox, QLabel, QProgressBar,
                             QAbstractItemView, QHeaderView, QShortcut, QMenu,
                             QStyle, QApplication, QSizePolicy)

from ..results import TransferResult
from . import transfer_log as tl
from .qtutil import run_job, close_jobs, owner_window, park_thread, run_teardown

try:
    from PyQt5 import sip
except ImportError:                      # PyQt5 < 5.11 ships it on its own
    import sip

_COLUMNS = ("Name", "Size", "Modified", "Permissions")
# what a drag out of the remote list carries: the names of the dragged entries,
# one per line (the local pane turns a drop of this into downloads)...
REMOTE_MIME = "application/x-turbossh-remote"
# ...and where they are: "<browser token>\n<folder>". The names mean nothing
# without it — in split view, ECU B's rows dropped on ECU A's local pane were
# looked up in A's folder, and A's file of that name was downloaded.
REMOTE_SOURCE_MIME = "application/x-turbossh-remote-source"
_PROGRESS_EVERY = 0.1    # s between progress signals: a fast link calls back
                         # thousands of times a second, a label needs ~10
_CLOSE_WAIT = 2.0        # s a closing browser lets a busy worker finish before
                         # its SFTP client is closed under it
_SFTP_TIMEOUT = 30.0     # s of silence from the server before a request fails;
                         # without it a dead link said "Loading…" until TCP gave
                         # up (which can take a quarter of an hour)
_ABORT_AFTER = 2.0       # s between looks at a cancelled transfer still running
_STALL_AFTER = 8.0       # s without a block moved before a cancelled transfer
                         # counts as stuck (dead link) and its client is closed
                         # under it — a live one is still settling what it had
                         # in flight, and closing it then left its temp file
_IN_FLIGHT = 2 * 1024 * 1024     # bytes a cancel may still wait for: the SSH
                                 # channel window — seconds on a slow VPN
_STAT_WINDOW = 64        # link stats in flight at once
_FILL_CHUNK = 2000       # listing rows added per turn of the event loop
_MAX_LINK_HOPS = 8       # symlinked folders followed inside one download
_PART = ".turbossh-part"     # suffix of a file still being written
_WINDOWS = os.name == "nt"   # this PC's file names follow Windows' rules


# --------------------------------------------------------------------------- #
# Reading a listing
# --------------------------------------------------------------------------- #
def _attr_type(attr) -> int:
    """The file-type bits of an SFTP entry, 0 if unknown. QNX/embedded servers
    can return None or out-of-range st_mode values that crash stat.S_ISDIR."""
    try:
        return int(getattr(attr, "st_mode", None) or 0) & 0o170000
    except Exception:
        return 0


def _attr_is_dir(attr) -> bool:
    return _attr_type(attr) == stat.S_IFDIR


_KINDS = {stat.S_IFDIR: "a folder", stat.S_IFCHR: "a character device",
          stat.S_IFBLK: "a block device", stat.S_IFIFO: "a named pipe (FIFO)",
          stat.S_IFSOCK: "a socket", stat.S_IFLNK: "a broken link"}


def kind_problem(ftype, unknown_ok=True) -> str:
    """Why an entry of type *ftype* cannot be read as a file ("" if it can):
    "is a character device". A device or a FIFO has no end (/dev/zero filled
    the memory) or blocks for ever (a FIFO, /dev/ser1). *unknown_ok*: a server
    that sends no file type at all is taken at its word that it is a file."""
    if ftype == stat.S_IFREG or (ftype == 0 and unknown_ok):
        return ""
    if ftype == 0:
        return "is of an unknown type (the server did not say it is a file)"
    return "is " + _KINDS.get(ftype, "not a regular file")


# is_dir / ftype describe the entry AFTER following a symlink; mode stays the
# entry's own, so the Permissions column still shows a link as one ("lrwxrwxrwx")
_Entry = namedtuple("_Entry", "name is_dir is_link size mtime mode ftype")


class _Replies:
    """Collects replies to pipelined requests, the way Paramiko's own read-ahead
    does (it calls ``_async_response`` with each one)."""

    def __init__(self):
        self.got = {}

    def _async_response(self, t, msg, num):
        self.got[num] = (t, msg)


def _stat_pipelined(sftp, paths):
    from paramiko.sftp import CMD_ATTRS, CMD_STAT
    from paramiko.sftp_attr import SFTPAttributes
    replies, results, pending, sent = _Replies(), [None] * len(paths), {}, 0
    while sent < len(paths) or pending:
        while sent < len(paths) and len(pending) < _STAT_WINDOW:
            pending[sftp._async_request(replies, CMD_STAT, paths[sent])] = sent
            sent += 1
        sftp._read_response()            # one reply: handed to `replies`
        for num in [n for n in pending if n in replies.got]:
            t, msg = replies.got.pop(num)
            index = pending.pop(num)
            if t == CMD_ATTRS:           # else a status: the link is dangling
                results[index] = SFTPAttributes._from_msg(msg)
    return results


def _stat_many(sftp, paths) -> list:
    """``stat()`` of every path (None where it failed). The requests go out
    together, so a folder full of links costs about one round trip, not one per
    link — over a VPN, /dev or a lib folder took minutes one by one. Paramiko
    has no public API for it: where its internals differ, one at a time."""
    if not paths:
        return []
    if hasattr(sftp, "_async_request") and hasattr(sftp, "_read_response"):
        try:
            return _stat_pipelined(sftp, paths)
        except (AttributeError, TypeError, ImportError):
            pass                         # not the Paramiko this was written for
    results = []
    for path in paths:
        try:
            results.append(sftp.stat(path))
        except socket.timeout:
            raise                        # the link: not this one path
        except IOError:
            results.append(None)
    return results


def _list_dir(sftp, path):
    """(normalised path, entries) of a remote folder — folders first, then by
    name ignoring case. Runs on a worker thread."""
    try:
        path = sftp.normalize(path)
    except socket.timeout:
        raise
    except Exception:
        pass                             # the listing below reports a bad path
    attrs = sftp.listdir_attr(path)
    # a link's own mode says nothing about what it points at: a stat tells a
    # linked folder (which can be entered) from a linked file
    links = [a.filename for a in attrs if _attr_type(a) == stat.S_IFLNK]
    targets = dict(zip(links, _stat_many(sftp, [posixpath.join(path, n) for n in links])))
    entries = []
    for attr in attrs:
        target = targets.get(attr.filename) or attr      # dangling: the link itself
        ftype = _attr_type(target)
        entries.append(_Entry(attr.filename, ftype == stat.S_IFDIR,
                              attr.filename in targets,
                              getattr(target, "st_size", None),
                              getattr(target, "st_mtime", None),
                              getattr(attr, "st_mode", None), ftype))
    entries.sort(key=lambda e: (not e.is_dir, (e.name or "").lower()))
    return path, entries


def _human(size) -> str:
    try:
        n = float(size)
    except (TypeError, ValueError):
        return ""                        # the server sent no size
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"


def _when(mtime) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(mtime)))
    except Exception:                    # None, or a value no calendar has
        return ""


def _perms(mode) -> str:
    try:
        return stat.filemode(int(mode) & 0o177777) if mode else ""
    except Exception:
        return ""


def _plural(n, word="item") -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _some(names, limit=5) -> str:
    more = len(names) - limit
    return ", ".join(names[:limit]) + (f" and {more} more" if more > 0 else "")


# --------------------------------------------------------------------------- #
# Remote names on this PC
# --------------------------------------------------------------------------- #
_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = frozenset(["CON", "PRN", "AUX", "NUL"]
                          + [f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)])


def local_name(name, windows=None) -> str:
    """A remote entry's *name* made into one this PC can hold, as ONE path
    component. Windows: ``<>:"/\\|?*`` and control characters become "_" (":"
    would write an NTFS alternate stream, the others abort the copy; a "\\" or a
    "C:" would leave the destination folder), trailing dots and spaces too
    (Windows drops them: "a." and "a" are one file), and a reserved device name
    (CON, NUL, COM1, LPT1…, with any extension) gets a "_" in front. Everywhere:
    "/" and NUL, and "." / ".." never stay what they are."""
    windows = _WINDOWS if windows is None else windows
    if windows:
        name = _WIN_BAD.sub("_", name)
        kept = name.rstrip(" .")
        name = kept + "_" * (len(name) - len(kept))
        if name.split(".")[0].rstrip(" ").upper() in _WIN_RESERVED:
            name = "_" + name
    else:
        name = name.replace("/", "_").replace("\0", "_")
    if name in ("", ".", ".."):
        name = name.replace(".", "_") or "_"
    return name


def _local_names(names, windows=None) -> dict:
    """name -> local name for the entries of ONE remote folder: each made valid
    here (:func:`local_name`) and unique — on Windows "Readme" and "README" are
    one file, and the second would silently replace the first. Names that need
    no change keep it; the others get " (2)", " (3)"…"""
    windows = _WINDOWS if windows is None else windows
    fold = str.casefold if windows else (lambda n: n)
    taken, out, later = set(), {}, []
    for name in names:
        safe = local_name(name, windows)
        if safe == name and fold(safe) not in taken:
            taken.add(fold(safe))
            out[name] = safe
        else:
            later.append((name, safe))
    for name, safe in later:
        stem, ext = os.path.splitext(safe)
        candidate, n = safe, 2
        while fold(candidate) in taken:
            candidate, n = f"{stem} ({n}){ext}", n + 1
        taken.add(fold(candidate))
        out[name] = candidate
    return out


def _inside(folder, path) -> bool:
    """Is *path* somewhere below *folder*? The last guard before a remote name
    becomes a local file: never anywhere else."""
    folder = os.path.normcase(os.path.abspath(folder))
    path = os.path.normcase(os.path.abspath(path))
    try:
        return path != folder and os.path.commonpath([folder, path]) == folder
    except ValueError:                   # another drive
        return False


# --------------------------------------------------------------------------- #
# Operations (worker thread, on the browser's own client)
# --------------------------------------------------------------------------- #
def _remove(sftp, path, is_dir):
    """Delete *path*; a folder goes with everything inside it. *is_dir* comes
    from the entry's own (lstat-style) mode, so a symlink to a folder is
    unlinked and what it points at is left alone."""
    if is_dir:
        for child in sftp.listdir_attr(path):
            _remove(sftp, posixpath.join(path, child.filename), _attr_is_dir(child))
        sftp.rmdir(path)
    else:
        sftp.remove(path)


def _rename(sftp, src, dst):
    """Rename without replacing anything. posix_rename overwrites an existing
    target without a word, and the listing the new name was checked against may
    be stale — so look on the server once more. (Not for a change of case only:
    on a case-insensitive server that lookup finds the source itself.)"""
    if posixpath.basename(src).lower() != posixpath.basename(dst).lower():
        try:
            sftp.lstat(dst)
        except socket.timeout:
            raise
        except IOError:
            pass
        else:
            raise IOError(f"{dst} already exists")
    try:
        sftp.posix_rename(src, dst)
    except socket.timeout:
        raise
    except IOError:
        # posix-rename is an OpenSSH extension; servers without it get the plain
        # SFTP rename (a genuine failure fails again there and is reported)
        sftp.rename(src, dst)


def _part_name(name) -> str:
    """A temporary name next to *name* for a file still being written: unique,
    and recognisably ours if a dropped link leaves it behind."""
    return f".{name[:150]}.{uuid.uuid4().hex[:8]}{_PART}"


def resolve_remote_link(sftp, path):
    """``(path, attr)``: *path* with symlinks followed — replacing a linked file
    must replace what it points at, not turn the link into a file — and the
    lstat of what is there (None if nothing is).

    A link is resolved by the server (realpath): a relative target is relative
    to where the link really is, and taken as text, "../x" in a symlinked
    folder pointed somewhere else — an upload made a new file there, and an
    editor save said the file had been deleted. Only a server that cannot
    resolve it gets the link's target joined on as text."""
    for _hop in range(_MAX_LINK_HOPS):
        try:
            attr = sftp.lstat(path)
        except socket.timeout:
            raise
        except IOError:
            return path, None
        if _attr_type(attr) != stat.S_IFLNK:
            return path, attr
        try:
            real = sftp.normalize(path)
        except socket.timeout:
            raise
        except (IOError, paramiko.SFTPError):    # no realpath, or a strange answer
            real = ""
        if real and real != path:        # checked on the next hop: it may be a
            path = real                  # link still (a server that only tidies)
            continue
        target = sftp.readlink(path)
        if not target:
            return path, attr
        path = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    raise IOError(f"{path}: too many levels of symbolic links")


def replace_remote(sftp, tmp, dst, existed=True) -> None:
    """Move the finished *tmp* over *dst*. posix-rename does that in one step.
    A server without it (the plain SFTP rename never replaces) gets the old file
    moved aside first and put back if the new one cannot take its place — the
    content of *dst* is never lost on the way."""
    try:
        sftp.posix_rename(tmp, dst)
        return
    except socket.timeout:
        raise
    except IOError:
        if not existed:
            sftp.rename(tmp, dst)
            return
    folder, name = posixpath.split(dst)
    old = posixpath.join(folder, _part_name(name)[:-len(_PART)] + ".turbossh-old")
    sftp.rename(dst, old)
    try:
        sftp.rename(tmp, dst)
    except BaseException:
        try:
            sftp.rename(old, dst)
        except Exception:
            pass
        raise
    try:
        sftp.remove(old)
    except IOError:
        pass                             # a stray copy of the old file; nothing lost


_UNKNOWN = object()


def put_atomic(sftp, local, remote, callback=None, existing=_UNKNOWN) -> None:
    """Upload *local* to *remote* without ever writing into *remote* itself: to
    a temporary name in the same folder, then renamed over it (with the old
    file's permissions). A cancel or a dropped link leaves the old file as it
    was — it used to be truncated, or deleted outright by Cancel. *existing* is
    the lstat of what is at *remote* (None: nothing) when the caller knows."""
    if existing is _UNKNOWN or (existing is not None
                                and _attr_type(existing) == stat.S_IFLNK):
        remote, existing = resolve_remote_link(sftp, remote)
    if existing is not None and _attr_is_dir(existing):
        raise IOError(f"{remote} is a folder")
    tmp = posixpath.join(posixpath.dirname(remote), _part_name(posixpath.basename(remote)))
    try:
        sftp.put(local, tmp, callback=callback, confirm=True)
        if existing is not None and getattr(existing, "st_mode", None):
            try:
                sftp.chmod(tmp, stat.S_IMODE(int(existing.st_mode)))
            except socket.timeout:
                raise
            except IOError:
                pass                     # the server's default; still a good file
        replace_remote(sftp, tmp, remote, existed=existing is not None)
    except BaseException as exc:
        # a timed-out client is out of step with the server: it is dropped, so
        # nothing more is sent on it (the stray temporary file is harmless)
        if not isinstance(exc, socket.timeout):
            try:
                sftp.remove(tmp)
            except Exception:
                pass
        raise


def get_atomic(sftp, remote, local, callback=None) -> int:
    """Download *remote* to *local* through a temporary file in the same folder,
    renamed into place once complete — a cancel or a dropped link leaves an
    existing *local* untouched. Returns the size."""
    if os.path.islink(local):
        local = os.path.realpath(local)  # replace what the link points at
    tmp = os.path.join(os.path.dirname(local) or ".", _part_name(os.path.basename(local)))
    try:
        sftp.get(remote, tmp, callback=callback)
        size = os.path.getsize(tmp)
        os.replace(tmp, local)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return size


def _mkdir_p(sftp, folder) -> None:
    if folder in ("", "/", "."):
        return
    parts = [p for p in folder.split("/") if p]
    current = "/" if folder.startswith("/") else ""
    for part in parts:
        current = posixpath.join(current, part) if current else part
        try:
            sftp.stat(current)
        except socket.timeout:
            raise
        except IOError:
            try:
                sftp.mkdir(current)
            except socket.timeout:
                raise
            except IOError:
                pass                     # made meanwhile; a real problem shows next


class _Cancelled(Exception):
    """Aborts a transfer from its callback. Deliberately not an IOError: that is
    what a failure looks like, and this must come back as itself."""


def _open_sftp(handler):
    """An SFTP client whose requests give up after _SFTP_TIMEOUT s of silence.
    Paramiko's ``open_sftp()`` sets no timeout anywhere: the channel open alone
    may wait an hour, and every request for as long as TCP takes to notice."""
    transport = handler.client.get_transport()
    if transport is None or not transport.is_active():
        raise IOError("the SSH connection is closed")
    chan = transport.open_session(timeout=_SFTP_TIMEOUT)
    try:
        chan.settimeout(_SFTP_TIMEOUT)
        chan.invoke_subsystem("sftp")
        return paramiko.SFTPClient(chan)
    except BaseException:
        chan.close()
        raise


class _OwnSftp:
    """One SFTP client on the handler's connection, used by one worker at a time.

    Opened on first use — always on a worker thread, the handshake is a network
    round trip — and reopened if its channel has died. Every use holds the lock,
    which is what keeps two threads off the same client. Not a QObject on
    purpose: workers and the teardown thread hold this, never the widget, so
    nothing touches the widget after it is gone."""

    def __init__(self, handler):
        self._handler = handler
        self._lock = threading.Lock()
        self._client = None
        self._shut = False

    def run(self, fn):
        """``fn(client)`` on the calling worker thread, holding the lock."""
        with self._lock:
            client = self._open()
            try:
                return fn(client)
            except socket.timeout:
                # a reply cut off half-way leaves the stream out of step with
                # the server: this client is never used again
                self.discard(client)
                raise IOError(f"the remote host did not answer for "
                              f"{_SFTP_TIMEOUT:.0f} s") from None
            except (SSHException, EOFError):
                self.discard(client)     # the channel is gone
                raise

    def _open(self):
        if self._shut:
            raise _Cancelled("the file browser was closed")
        client = self._client
        chan = client.get_channel() if client is not None else None
        if chan is None or chan.closed:
            client = _open_sftp(self._handler)
            if self._shut:               # closed while the channel was opening
                client.close()
                raise _Cancelled("the file browser was closed")
            self._client = client
        return client

    def current(self):
        """The client in use now (None before the first use)."""
        return self._client

    def discard(self, client):
        """Never use *client* again, and close it on a teardown thread: a request
        stuck in it then fails at once (closing waits on the link too). Cheap,
        so the GUI thread may call it."""
        if self._client is client:
            self._client = None
        if client is not None:
            run_teardown(client.close, "sftp-discard")

    def shut(self):
        """Refuse new work from now on (cheap: for the GUI thread). Without it a
        parked worker could reopen a client after close() and leak the channel."""
        self._shut = True

    def close(self):
        """Close the client. Blocks, so it runs on a teardown thread: a worker in
        mid-request gets a moment to finish; one that is stuck on a dead link is
        freed by closing the channel under it (its request then fails)."""
        self._shut = True
        held = self._lock.acquire(timeout=_CLOSE_WAIT)
        try:
            client, self._client = self._client, None
            if client is not None:
                client.close()
        finally:
            if held:
                self._lock.release()


# --------------------------------------------------------------------------- #
# Transfers
# --------------------------------------------------------------------------- #
# kind: download | upload; log_id: the job's row in the transfer log
_Job = namedtuple("_Job", "kind remote local is_dir log_id")


def _job_name(job) -> str:
    return posixpath.basename(job.remote.rstrip("/")) or job.remote


class _TransferThread(QThread):
    """Move ONE queued item — a file, or a folder with its contents — on the
    transfer client. Every file goes through :func:`get_atomic` /
    :func:`put_atomic`, and every remote name through :func:`_local_names`
    before it becomes a local path. The signal names are the ones
    qtutil.park_thread silences."""
    # file being moved, percent of that file, (bytes moved in all, size of this
    # file, files completed)
    progress = pyqtSignal(str, int, object)
    done = pyqtSignal(str, object, int)  # summary line, bytes, files
    failed = pyqtSignal(str)
    stopped = pyqtSignal()               # cancelled

    def __init__(self, link, job):
        super().__init__()
        self.job = job
        self._link = link
        self._cancel = threading.Event()
        self.restart = False             # stopped to run again on a new link
        self.notes = []                  # names changed, entries skipped
        self._file, self._last = "", 0.0
        self._before = 0                 # bytes of the files already finished
        self._here = self._size = 0      # bytes done / size of the current file
        self._files = 0
        # signs of life, read by the GUI thread after a cancel: when a block
        # last moved, and how fast they were moving (bytes/s, 0 = not known)
        self.heard = time.monotonic()
        self.pace = 0.0
        self._mark = (self.heard, 0)     # (time, bytes) the pace is measured from

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def stalled(self, now=None) -> bool:
        """Has nothing moved for longer than a live link would need? After a
        cancel the transfer still waits for what it had in flight — up to
        _IN_FLIGHT bytes at the pace it was going — and only a dead link takes
        longer. Never longer than _SFTP_TIMEOUT, when a request fails anyway."""
        now = time.monotonic() if now is None else now
        pace = self.pace
        wait = _STALL_AFTER + (_IN_FLIGHT / pace if pace > 0 else 0.0)
        return now - self.heard >= min(wait, _SFTP_TIMEOUT)

    def abort(self):
        """Close the client under this (stalled) transfer: the request it is
        stuck in fails at once. Cheap, for the GUI thread."""
        link = self._link
        client = link.current()
        if client is not None:
            link.discard(client)

    def run(self):
        started = time.monotonic()
        try:
            size, files = self._link.run(self._move)
        except Exception as exc:        # noqa: BLE001 - reported to the GUI
            # whatever a cancelled transfer died of, the user asked for it
            if self.cancelled or isinstance(exc, _Cancelled):
                self.stopped.emit()
            else:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            job = self.job
            down = job.kind == "download"
            result = TransferResult(job.remote if down else job.local,
                                    job.local if down else job.remote,
                                    "pull" if down else "push", "sftp", size,
                                    time.monotonic() - started, files)
            self.done.emit(str(result), size, files)

    def _move(self, sftp):
        job = self.job
        if job.kind == "download":
            return self._download(sftp, job.remote, job.local)
        return self._upload(sftp, job.local, job.remote)

    # ---- download ----
    def _download(self, sftp, remote, local):
        try:
            attr = sftp.stat(remote)
        except socket.timeout:
            raise
        except IOError as exc:
            raise IOError(f"cannot read {remote}: {exc}") from None
        ftype = _attr_type(attr)
        if ftype == stat.S_IFDIR:
            return self._get_dir(sftp, remote, local, set(), 0)
        problem = kind_problem(ftype)
        if problem:
            raise IOError(f"{remote} {problem}, not a file that can be copied")
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        return self._get_file(sftp, remote, local), 1

    def _get_file(self, sftp, remote, local) -> int:
        self._on_file(remote)
        return get_atomic(sftp, remote, local, self._on_bytes)

    def _get_dir(self, sftp, remote_dir, local_dir, seen, hops):
        try:
            real = sftp.normalize(remote_dir)
        except socket.timeout:
            raise
        except IOError:
            real = remote_dir
        if real in seen:                 # a symlink loop: already copied
            return 0, 0
        seen.add(real)
        os.makedirs(local_dir, exist_ok=True)
        attrs = sftp.listdir_attr(remote_dir)
        links = [a.filename for a in attrs if _attr_type(a) == stat.S_IFLNK]
        targets = dict(zip(links, _stat_many(
            sftp, [posixpath.join(remote_dir, n) for n in links])))
        names = _local_names([a.filename for a in attrs])
        total = count = 0
        for attr in attrs:
            self._check()
            name = attr.filename
            rpath = posixpath.join(remote_dir, name)
            lpath = os.path.join(local_dir, names[name])
            if not _inside(local_dir, lpath):
                self.notes.append(f"skipped {rpath}: no usable local name")
                continue
            if names[name] != name:
                self.notes.append(f"{rpath} was saved as {names[name]}")
            is_link = name in targets
            target = targets[name] if is_link else attr
            if target is None:
                self.notes.append(f"skipped the broken link {rpath}")
                continue
            ftype = _attr_type(target)
            if ftype == stat.S_IFDIR:
                deeper = hops + (1 if is_link else 0)
                if deeper > _MAX_LINK_HOPS:
                    self.notes.append(f"skipped {rpath}: too many nested symlinked "
                                      f"folders (a link loop?)")
                    continue
                size, files = self._get_dir(sftp, rpath, lpath, seen, deeper)
            elif kind_problem(ftype):
                self.notes.append(f"skipped {rpath}: it {kind_problem(ftype)}")
                continue
            else:
                size, files = self._get_file(sftp, rpath, lpath), 1
            total += size
            count += files
        return total, count

    # ---- upload ----
    def _upload(self, sftp, local, remote):
        if not os.path.exists(local):
            raise IOError(f"Local path does not exist: {local}")
        if os.path.isdir(local):
            return self._put_dir(sftp, local, remote, set())
        _mkdir_p(sftp, posixpath.dirname(remote))
        return self._put_file(sftp, local, remote), 1

    def _put_file(self, sftp, local, remote, existing=_UNKNOWN) -> int:
        self._on_file(remote)
        put_atomic(sftp, local, remote, self._on_bytes, existing)
        return os.path.getsize(local)

    def _put_dir(self, sftp, local_dir, remote_dir, seen):
        real = os.path.realpath(local_dir)
        if real in seen:                 # a link back up the tree
            return 0, 0
        seen.add(real)
        _mkdir_p(sftp, remote_dir)
        try:
            # what is there already, in one listing: one lstat per file before
            # each upload would cost a round trip per file
            there = {a.filename: a for a in sftp.listdir_attr(remote_dir)}
        except socket.timeout:
            raise
        except IOError:
            there = None
        total = count = 0
        for name in sorted(os.listdir(local_dir)):
            self._check()
            lpath, rpath = os.path.join(local_dir, name), posixpath.join(remote_dir, name)
            if os.path.isdir(lpath):
                size, files = self._put_dir(sftp, lpath, rpath, seen)
            elif os.path.isfile(lpath):
                existing = _UNKNOWN if there is None else there.get(name)
                size, files = self._put_file(sftp, lpath, rpath, existing), 1
            else:
                self.notes.append(f"skipped {lpath}: not a file or a folder")
                continue
            total += size
            count += files
        return total, count

    # ---- progress ----
    def _check(self):
        if self.cancelled:
            raise _Cancelled()

    def _on_file(self, remote):
        self._alive(self._before + self._here)
        self._check()
        if self._file:                   # the previous file is complete
            self._before += self._here
            self._files += 1
        self._here = self._size = 0
        self._file = posixpath.basename(remote)
        self._report(0, force=True)

    def _on_bytes(self, done, total):
        # before the check: the block a cancel lands on shows the link is alive
        self._alive(self._before + done)
        self._check()
        self._here, self._size = done, total
        self._report(int(done * 100 / total) if total else 0)

    def _alive(self, moved):
        """A block moved (*moved*: bytes in all so far): note when, and the
        pace over the last second or so."""
        now = time.monotonic()
        self.heard = now
        since, base = self._mark
        if now - since >= 1.0:
            self.pace = max(0, moved - base) / (now - since)
            self._mark = (now, moved)

    def _report(self, percent, force=False):
        now = time.monotonic()
        if force or now - self._last >= _PROGRESS_EVERY:
            self._last = now
            self.progress.emit(self._file, percent,
                               (self._before + self._here, self._size, self._files))


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #
class _Row(QTreeWidgetItem):
    """A listing row: *rank* keeps ".." on top and folders before files whichever
    way a column is sorted, *keys* are its sort key per column, worked out once.

    The browser orders the rows itself, in Python with these keys
    (:func:`_sorted_rows`): Qt's own sort calls ``__lt__`` once per comparison,
    and that — asking the header for the order each time — froze the window for
    seconds on a big folder. ``__lt__`` stays for anything else that sorts, and
    reads the order the tree caches."""

    def __init__(self, texts, rank, keys):
        super().__init__(texts)
        self.rank, self.keys = rank, keys        # rank: 0 "..", 1 folder, 2 file
        self.name = texts[0]

    def __lt__(self, other):
        tree = self.treeWidget()
        column, ascending = getattr(tree, "order", (0, True))
        if self.rank != other.rank:
            # Qt reverses the result for a descending sort; undo that here so
            # the grouping stays put
            return (self.rank < other.rank) == ascending
        mine, theirs = self.keys[column], other.keys[column]
        if mine == theirs:
            mine, theirs = self.keys[0], other.keys[0]   # ties: by name
        return mine < theirs


def _sorted_rows(rows, column, ascending) -> list:
    """*rows* (or row specs) in display order: ".." first, then folders, then
    files, each group by *column* (ties by name)."""
    top = [r for r in rows if r.rank == 0]
    rest = sorted((r for r in rows if r.rank),
                  key=lambda r: (r.keys[column], r.keys[0]), reverse=not ascending)
    rest.sort(key=lambda r: r.rank)      # stable: the column order is kept
    return top + rest


# what a row shows, worked out on the worker thread
_Spec = namedtuple("_Spec", "rank keys name is_dir is_link texts")


def _row_specs(entries, order) -> list:
    """The rows of a listing, formatted and in display order — on the worker
    thread: formatting the dates of 20,000 entries on the GUI thread was a good
    part of the time the window froze."""
    specs = []
    for e in entries:
        perms = _perms(e.mode)
        specs.append(_Spec(1 if e.is_dir else 2,
                           ((e.name or "").lower(), e.size or 0, e.mtime or 0, perms),
                           e.name, e.is_dir, e.is_link,
                           ("" if e.is_dir else _human(e.size), _when(e.mtime), perms)))
    return _sorted_rows(specs, *order)


def _listing(link, path, order):
    """Worker: list *path* and prepare its rows (outside the link's lock)."""
    path, entries = link.run(lambda sftp: _list_dir(sftp, path))
    return path, entries, _row_specs(entries, order), order


class _FileTree(QTreeWidget):
    """The listing — which also takes local files and folders dragged in from
    Explorer (or from the local pane), and lets its own rows be dragged out. The
    item view's own drag handlers refuse anything that is not its model's data,
    hence the overrides."""
    dropped = pyqtSignal(list)           # local paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.order = (0, True)           # sort column, ascending
        self.source = ""                 # REMOTE_SOURCE_MIME of the rows shown
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def sortByColumn(self, column, order=Qt.AscendingOrder):  # noqa: N802 - Qt API
        """Through the header's sort indicator, which the browser follows by
        re-ordering the rows itself — never Qt's per-comparison sort."""
        self.header().setSortIndicator(column, order)

    def mimeTypes(self):
        return [REMOTE_MIME]

    def mimeData(self, items):
        """Dragging rows out carries their names (never "..") and where they
        are; the local pane downloads what is dropped on it."""
        names = [it.data(0, Qt.UserRole) for it in items]
        names = [n for n in names if n and n != ".."]
        data = QMimeData()
        if names:
            data.setData(REMOTE_MIME, "\n".join(names).encode("utf-8"))
            data.setData(REMOTE_SOURCE_MIME, self.source.encode("utf-8"))
        return data

    def supportedDropActions(self):
        return Qt.CopyAction

    def _local_paths(self, event):
        mime = event.mimeData()
        if not self.isEnabled() or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]

    def _take(self, event) -> list:
        paths = self._local_paths(event)
        if paths:
            # an upload is a copy. Accepting the proposed action could report a
            # MOVE back to Explorer, which then deletes the user's originals.
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()
        return paths

    def dragEnterEvent(self, event):
        self._take(event)

    def dragMoveEvent(self, event):
        self._take(event)

    def dropEvent(self, event):
        paths = self._take(event)
        if paths:
            self.dropped.emit(paths)


class SftpBrowser(QWidget):
    """The remote pane. On its own it is a complete browser; inside the dual-pane
    :class:`~turbossh.gui.file_browser.FileBrowser` it shares a transfer log with
    the Transfers panel and exchanges drags with the local pane."""
    log = pyqtSignal(str)
    notify = pyqtSignal(str, str)        # text, kind (ok / warn / error): toasts

    def __init__(self, handler, parent=None, transfer_log=None):
        super().__init__(parent)
        self.handler = handler
        self.transfers = transfer_log if transfer_log is not None else tl.TransferLog()
        self.local_target = None         # callable -> the local pane's folder
        self.show_inline_progress = True # off when a Transfers panel shows it
        self.drag_token = uuid.uuid4().hex   # names this browser in its drags
        self._editors = []
        self.cwd = "."                   # only ever the last folder that LISTED
        self._entries = {}               # name -> _Entry: the listing on screen
        self._jobs = set()               # listing / operation workers
        self._remote = _OwnSftp(handler)     # listings, mkdir, rename, delete
        self._xfer = _OwnSftp(handler)       # transfers
        self._queue = deque()            # _Job items waiting their turn
        self._transfer = None            # the _TransferThread moving one now
        self._stopping = None            # the cancelled transfer watched by _abort_stuck
        self._file, self._percent = "", 0
        self._busy = ""                  # what the list is waiting for; "" = idle
        self._message = ""               # last error / result, until the next action
        self._summary = ""
        self._target = self._select = None
        self._op = ""                    # the operation in flight ("" = none)
        self._again = False              # re-list as soon as the list is idle
        self._list_gen = 0               # which listing the rows being added are of
        self._fill = None                # [gen, specs, next row, current]: filling
        self._sort_pending = False       # re-order once the fill is complete
        self._focus_back = None
        self._closing = False
        self._fill_timer = QTimer(self)  # a child: never fires once we are gone
        self._fill_timer.setSingleShot(True)
        self._fill_timer.setInterval(0)
        self._fill_timer.timeout.connect(self._fill_more)
        self._stop_timer = QTimer(self)  # looks at a cancelled transfer until it stops
        self._stop_timer.timeout.connect(self._abort_stuck)
        self._build_ui()
        self._list(".")

    def _build_ui(self):
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.path = QLineEdit(); self.path.returnPressed.connect(self._go)
        up = QPushButton("Up"); up.setProperty("role", "ghost"); up.clicked.connect(self._up)
        home = QPushButton("Home"); home.setProperty("role", "ghost")
        home.setToolTip("The login folder")
        home.clicked.connect(lambda: self.navigate("."))
        root = QPushButton("/"); root.setProperty("role", "ghost")
        root.setToolTip("The top of the file system")
        root.clicked.connect(lambda: self.navigate("/"))
        ref = QPushButton("Refresh"); ref.setProperty("role", "ghost")
        ref.clicked.connect(self.refresh)
        top.addWidget(QLabel("Remote:")); top.addWidget(self.path, 1)
        top.addWidget(up); top.addWidget(home); top.addWidget(root); top.addWidget(ref)

        self.tree = _FileTree()
        self.tree.setHeaderLabels(list(_COLUMNS))
        self.tree.setRootIsDecorated(False)          # a flat list with columns
        self.tree.setUniformRowHeights(True)         # big folders lay out fast
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        head = self.tree.header()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            head.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        head.setResizeContentsPrecision(200)         # rows measured per column
        # click a column to sort by it (folders stay first, ".." stays on top).
        # The tree's own sorting stays off: the rows are ordered in Python.
        head.setSectionsClickable(True)
        head.setSortIndicatorShown(True)
        head.setSortIndicator(0, Qt.AscendingOrder)
        head.sortIndicatorChanged.connect(self._resort)
        self.tree.itemActivated.connect(self._open_item)     # double-click / Enter
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._ctx_menu)
        # queued: the handler may ask a question, and a modal dialog opened
        # inside the drop itself freezes Explorer until it is answered
        self.tree.dropped.connect(self._upload_paths, Qt.QueuedConnection)
        style = self.style()
        self._icons = {(True, False): style.standardIcon(QStyle.SP_DirIcon),
                       (True, True): style.standardIcon(QStyle.SP_DirLinkIcon),
                       (False, False): style.standardIcon(QStyle.SP_FileIcon),
                       (False, True): style.standardIcon(QStyle.SP_FileLinkIcon)}

        # Scoped to the browser: with the default (window) context these fired
        # while typing in another session's terminal in split view. Delete and
        # Backspace belong to the list alone, so the path box keeps them too.
        for keys, owner, slot in (("F5", self, self.refresh),
                                  ("Delete", self.tree, self._delete),
                                  ("Backspace", self.tree, self._up)):
            QShortcut(QKeySequence(keys), owner, activated=slot,
                      context=Qt.WidgetWithChildrenShortcut)

        # transfer row: only there while something is moving
        self.xfer_row = QWidget()
        row = QHBoxLayout(self.xfer_row); row.setContentsMargins(0, 0, 0, 0)
        self.xfer_label = QLabel()
        # a long file name is clipped, it must not force the window wider
        self.xfer_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.bar = QProgressBar(); self.bar.setRange(0, 100); self.bar.setFixedWidth(160)
        self.cancel_btn = QPushButton("Cancel"); self.cancel_btn.setProperty("role", "danger")
        self.cancel_btn.clicked.connect(self.cancel_transfer)
        row.addWidget(self.xfer_label, 1); row.addWidget(self.bar)
        row.addWidget(self.cancel_btn)
        self.xfer_row.hide()

        # errors land here as well as in the log dock, which may be hidden
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)

        ops = QHBoxLayout()
        self._controls = [self.path, up, home, root, ref, self.tree]  # locked while busy
        for label, slot, role in (("Download", self._download, "ok"),
                                   ("Upload", self._upload, None),
                                   ("Mkdir", self._mkdir, "ghost"),
                                   ("Rename", self._rename_selected, "ghost"),
                                   ("Delete", self._delete, "danger")):
            b = QPushButton(label)
            if role:
                b.setProperty("role", role)
            b.clicked.connect(slot)
            ops.addWidget(b)
            self._controls.append(b)
        self._controls[7].setToolTip("Upload files — for a folder, right-click "
                                     "or drag it onto the list")

        lay.addLayout(top); lay.addWidget(self.tree, 1); lay.addWidget(self.xfer_row)
        lay.addWidget(self.status); lay.addLayout(ops)

    # --- state ---
    @property
    def busy(self) -> bool:
        """True while a listing, an operation or a transfer is in flight."""
        return bool(self._busy or self._transfer is not None or self._queue)

    def _show(self):
        self.status.setText(self._busy or self._message or self._summary)

    def _set_busy(self, text):
        """Lock (*text* says why) or release ("") everything that acts on the
        listing, so nothing can be started from a list that is about to change."""
        if text and not self._busy:
            # Qt hands the focus of a widget being disabled to the next one in
            # the window's tab chain — in split view that can be another
            # session's terminal, which would then get the user's keystrokes.
            # Hold it on the browser itself and give it back afterwards.
            focus = QApplication.focusWidget()
            if focus is not None and self.isAncestorOf(focus):
                self._focus_back = focus
                self.setFocus()
        self._busy = text
        for w in self._controls:
            w.setEnabled(not text)
        if not text and self._focus_back is not None:
            back, self._focus_back = self._focus_back, None
            if self.hasFocus():          # not if the user went elsewhere meanwhile
                back.setFocus()
        self._show()

    def _error(self, text):
        self._message = f"Error: {text}"
        self.log.emit(f"[ERROR] {text}")
        self._show()

    def _notice(self, text, tag="[OK] "):
        self._message = text
        self.log.emit(tag + text)
        self._show()

    # --- navigation ---
    def navigate(self, path) -> bool:
        """List *path*; it becomes the current folder only once it HAS listed.
        Ignored (False) while another listing or an operation is in flight."""
        if self._closing or self._busy:
            return False
        self._message = ""
        return self._list((path or "").strip() or ".")

    def refresh(self):
        """Re-list the current folder (F5)."""
        if not self._busy:
            self._message = ""
        self._relist()

    def _relist(self):
        if self._closing:
            return
        if self._busy:                   # it is changing right now: once it settles
            self._again = True
            return
        self._list(self.cwd, self._current_name())

    def _current_name(self):
        current = self.tree.currentItem()
        return current.data(0, Qt.UserRole) if current else None

    def _list(self, path, select=None) -> bool:
        self._target, self._select = path, select
        self._list_gen += 1              # rows of an earlier listing stop coming
        self._set_busy(f"Loading {path}…")
        remote, order = self._remote, self.tree.order    # the worker must not
        run_job(self._jobs, lambda: _listing(remote, path, order),   # touch the widget
                self._on_listed, self._on_list_fail)
        return True

    def _on_listed(self, result):
        if self._closing:
            return
        self.cwd, entries, specs, order = result
        self._entries = {e.name: e for e in entries}
        self._summary = _plural(len(entries))
        self.path.setText(self.cwd)
        self.tree.source = f"{self.drag_token}\n{self.cwd}"
        if order != self.tree.order:     # the order was changed meanwhile
            specs = _sorted_rows(specs, *self.tree.order)
        self.tree.clear()
        top = self._item("..", self._icons[True, False], 0, ("", -1, 0, ""))
        self.tree.addTopLevelItem(top)
        self._fill = [self._list_gen, specs, 0, top]
        self._fill_more()

    def _fill_more(self):
        """Add the next rows of the listing, a chunk per turn of the event loop:
        20,000 rows at once kept the window frozen for half a second. The list
        stays locked (busy) until the last one is in."""
        if self._closing or self._fill is None:
            return
        gen, specs, start, current = self._fill
        if gen != self._list_gen:        # another listing has started since
            self._fill = None
            return
        end = min(len(specs), start + _FILL_CHUNK)
        icons, item, select, rows = self._icons, self._item, self._select, []
        for s in specs[start:end]:
            it = item(s.name, icons[s.is_dir, s.is_link], s.rank, s.keys, *s.texts)
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if s.name == select:
                current = it
            rows.append(it)
        self.tree.addTopLevelItems(rows)
        if end < len(specs):
            self._fill[2:] = [end, current]
            self._fill_timer.start()
            return
        self._fill = None
        if self._sort_pending:           # a column was clicked while it filled
            self._sort_pending = False
            self._reorder()
        # keep the keyboard's place: the entry that was current (refresh), just
        # made (mkdir / rename) or just left (Up) — else the top of the list
        self.tree.setCurrentItem(current)
        self.tree.scrollToItem(current)
        self._settled()

    def _resort(self, column, order):
        """A column header was clicked (or sortByColumn called): re-order the
        rows in Python, keeping the selection and the current row."""
        self.tree.order = (column, order == Qt.AscendingOrder)
        if self._fill is not None:       # half-filled: sort once it is complete
            self._sort_pending = True
            return
        self._reorder()

    def _reorder(self):
        root = self.tree.invisibleRootItem()
        if not root.childCount():
            return
        current = self._current_name()
        chosen = {it.data(0, Qt.UserRole) for it in self.tree.selectedItems()}
        rows = _sorted_rows(root.takeChildren(), *self.tree.order)
        root.addChildren(rows)
        for it in rows:
            if it.name in chosen:
                it.setSelected(True)
            if it.name == current:
                self.tree.setCurrentItem(it, 0, QItemSelectionModel.NoUpdate)

    def _on_list_fail(self, msg):
        if self._closing:
            return
        # cwd was never changed, so it still names the folder on screen; put the
        # path box back too — it may hold the path that could not be listed
        self.path.setText(self.cwd)
        self._error(f"cannot list {self._target}: {msg}")
        self._settled()

    def _settled(self):
        self._set_busy("")
        if self._again:
            self._again = False
            self._relist()

    @staticmethod
    def _item(name, icon, rank, keys, *columns):
        it = _Row([name, *columns], rank, keys)
        it.setIcon(0, icon)
        it.setData(0, Qt.UserRole, name)
        return it

    def _ctx_menu(self, pos):
        entries = self._selection()
        m = QMenu(self)
        if self.local_target is not None and entries:
            m.addAction("Download to the local pane", self.download_to_local_pane)
        m.addAction("Download…", self._download)
        if len(entries) == 1 and not kind_problem(entries[0].ftype):
            m.addAction("Edit / view…", self.edit_selected)
        m.addSeparator()
        m.addAction("Upload files…", self._upload)
        m.addAction("Upload folder…", self._upload_folder)
        m.addSeparator()
        m.addAction("New folder", self._mkdir)
        m.addAction("Rename", self._rename_selected)
        m.addAction("Delete", self._delete)
        if entries:
            m.addAction("Copy path", self._copy_paths)
        m.addSeparator()
        m.addAction("Refresh (F5)", self.refresh)
        m.exec_(self.tree.viewport().mapToGlobal(pos))

    def _copy_paths(self):
        QApplication.clipboard().setText(
            "\n".join(posixpath.join(self.cwd, e.name) for e in self._selection()))

    # --- the editor ---
    def _live_editors(self) -> list:
        """The editors still open (a closed one deletes itself)."""
        self._editors = [d for d in self._editors if not sip.isdeleted(d)]
        return self._editors

    def edit_selected(self):
        """Open the selected remote text file in an editor window. Not a device
        or a FIFO: one never ends, the other never answers. A file the server
        gives no type for opens (as in 2.0.0): the read is bounded anyway."""
        entries = self._selection()
        if len(entries) != 1 or entries[0].is_dir or self._closing:
            return None
        entry = entries[0]
        problem = kind_problem(entry.ftype)
        if problem:
            self._notice(f"{entry.name} {problem} — it cannot be opened in the "
                         f"editor", "[WARN] ")
            return None
        from .remote_edit import RemoteEditDialog
        dialog = RemoteEditDialog(self._remote, posixpath.join(self.cwd, entry.name),
                                  owner_window(self))
        self._live_editors().append(dialog)
        dialog.show()
        return dialog

    def _go(self):
        self.navigate(self.path.text())

    def _up(self):
        if self._closing or self._busy:
            return
        here = self.cwd.rstrip("/")
        self._message = ""
        self._list(posixpath.dirname(here) or "/", posixpath.basename(here))

    def _open_item(self, item, _column=0):
        name = item.data(0, Qt.UserRole)
        if name == "..":
            self._up()
        elif name in self._entries and self._entries[name].is_dir:
            self.navigate(posixpath.join(self.cwd, name))
        elif name in self._entries:
            self.edit_selected()         # double-click a file: view / edit it

    def _selection(self):
        """The selected entries in listing order; ".." is never one of them."""
        names = {it.data(0, Qt.UserRole) for it in self.tree.selectedItems()}
        return [e for name, e in self._entries.items() if name in names]

    # --- operations: mkdir / rename / delete ---
    def _run_op(self, what, fn, select=None) -> bool:
        """Run ``fn(sftp)`` on a worker through the browser's own (locked)
        client, then re-list. False if the list is busy."""
        if self._closing or self._busy:
            return False
        self._op, self._select, self._message = what, select, ""
        self._set_busy(f"{what}…")
        remote = self._remote
        run_job(self._jobs, lambda: remote.run(fn), self._op_done, self._op_failed)
        return True

    def _op_done(self, _result):
        if self._closing:
            return
        self._op = ""
        self._set_busy("")
        self._list(self.cwd, self._select)

    def _op_failed(self, msg):
        if self._closing:
            return
        what, self._op = self._op, ""
        self._set_busy("")
        self._error(f"{what} failed: {msg}")
        self._list(self.cwd)             # a recursive delete may have got part-way

    def _bad_name(self, name) -> str:
        """Why an entry of this folder cannot be called *name* ("" if it can)."""
        if not name or name in (".", ".."):
            return "a name is needed"
        if "/" in name:
            return f"'{name}' contains '/' — give a name, not a path"
        if name in self._entries:
            return f"'{name}' already exists"
        return ""

    def do_mkdir(self, name) -> bool:
        """Create a folder in the current one. False if refused or busy."""
        if self._closing or self._busy:
            return False
        name = (name or "").strip()
        problem = self._bad_name(name)
        if problem:
            self._error(f"New folder: {problem}")
            return False
        path = posixpath.join(self.cwd, name)
        return self._run_op(f"Creating {name}", lambda sftp: sftp.mkdir(path), name)

    def do_rename(self, old, new) -> bool:
        """Rename an entry of the current folder. Refused (False) if *new* is
        already in the listing or is a path: a rename must never replace or
        move anything."""
        if self._closing or self._busy:
            return False
        new = (new or "").strip()
        if old not in self._entries or new == old:
            return False
        problem = self._bad_name(new)
        if problem:
            self._error(f"Rename: {problem}")
            return False
        src, dst = posixpath.join(self.cwd, old), posixpath.join(self.cwd, new)
        return self._run_op(f"Renaming {old}", lambda sftp: _rename(sftp, src, dst), new)

    def do_delete(self, names) -> bool:
        """Delete entries of the current folder; folders go with their contents."""
        if self._closing or self._busy:
            return False
        entries = [self._entries[n] for n in names if n in self._entries]
        if not entries:
            return False
        # a LINK to a folder is unlinked, never emptied
        targets = [(posixpath.join(self.cwd, e.name), e.is_dir and not e.is_link)
                   for e in entries]
        return self._run_op(f"Deleting {_plural(len(targets))}",
                            lambda sftp: [_remove(sftp, p, d) for p, d in targets])

    def _delete_question(self, entries) -> str:
        text = (f"Delete {_plural(len(entries), 'selected item')} from {self.cwd}?\n"
                f"{_some([e.name for e in entries])}")
        if any(e.is_dir and not e.is_link for e in entries):
            text += "\n\nFolders are removed together with everything inside them."
        return text + "\n\nThis cannot be undone."

    def _mkdir(self):
        name, ok = QInputDialog.getText(self, "New folder", "Name:")
        if ok and name.strip():
            self.do_mkdir(name)

    def _rename_selected(self):
        entries = self._selection()
        if len(entries) != 1 or self._busy:
            return
        old = entries[0].name
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=old)
        if ok and new.strip():
            self.do_rename(old, new)

    def _delete(self):
        entries = self._selection()
        if not entries or self._busy:
            return
        if QMessageBox.question(self, "Delete",
                                self._delete_question(entries)) == QMessageBox.Yes:
            self.do_delete([e.name for e in entries])

    # --- transfers ---
    def queue_download(self, remote_path, local_path, is_dir=False) -> bool:
        """Queue a remote file — or a folder with its contents — for download."""
        return self._enqueue(_Job("download", remote_path, local_path, is_dir, 0))

    def queue_upload(self, local_path, remote_path) -> bool:
        """Queue a local file — or a folder with its contents — for upload. It
        replaces what is already there; asking first is _upload_paths' job."""
        return self._enqueue(_Job("upload", remote_path, local_path,
                                  os.path.isdir(local_path), 0))

    def retry(self, items) -> int:
        """Queue failed / cancelled transfers (TransferItems) again."""
        count = 0
        for item in items:
            if self._enqueue(_Job(*item.job, 0)):
                count += 1
        return count

    def cancel_transfer(self) -> bool:
        """Abort the transfer in flight (its callback raises on the next block)
        and drop the queued ones. Only the transfer's temporary file goes; what
        it was going to replace stays as it was. It wins over a reconnect that
        was about to start the transfer again."""
        if self._closing or self._transfer is None:
            return False
        self._queue.clear()
        self.transfers.cancel_queued()
        t = self._transfer
        t.restart = False
        t.cancel()
        self._stopping = t
        if not self._stop_timer.isActive():
            self._stop_timer.start(int(_ABORT_AFTER * 1000))
        self.cancel_btn.setEnabled(False)
        self._show_transfer()
        return True

    def _abort_stuck(self):
        """A cancelled transfer stops at its next block of data, then waits for
        what it still had in flight — seconds on a slow link, for ever on a
        dead one. Look every _ABORT_AFTER s; once it has stalled (nothing moved
        for longer than a live link needs) close its client under it. A live
        one is left to finish: closing it mid-way left its temp file behind."""
        t = self._stopping
        if (self._closing or t is None or t is not self._transfer
                or not t.isRunning()):   # it stopped (by itself or not)
            self._stopping = None
            self._stop_timer.stop()
            return
        if t.stalled():
            t.abort()

    def _enqueue(self, job) -> bool:
        if self._closing:
            return False
        job = job._replace(log_id=self.transfers.add(job.kind, job.remote, job.local,
                                                     job.is_dir))
        self._queue.append(job)
        self._pump()
        return True

    def _pump(self):
        """Start the next queued item unless one is moving: one at a time, as
        they all share the transfer client."""
        if self._closing:
            return
        if self._transfer is not None:
            self._show_transfer()        # the queued count changed
            return
        if not self._queue:
            self.xfer_row.hide()
            return
        job = self._queue.popleft()
        t = self._transfer = _TransferThread(self._xfer, job)
        t.progress.connect(self._on_progress)
        t.done.connect(self._on_transfer_done)
        t.failed.connect(self._on_transfer_failed)
        t.stopped.connect(self._on_transfer_stopped)
        # the queue advances on finished, not on done: only then is the thread
        # object safe to drop, and the client free for the next item
        t.finished.connect(self._on_transfer_finished)
        self._file, self._percent = "", 0
        self.cancel_btn.setEnabled(True)
        self._show_transfer()
        if self.show_inline_progress:
            self.xfer_row.show()
        self.transfers.start(job.log_id)
        self.log.emit(f"{job.kind} {job.remote}…")
        t.start()

    def _show_transfer(self):
        t = self._transfer
        if t.restart:
            text = "Reconnected — starting again…"
        elif t.cancelled:
            text = "Cancelling…"
        else:
            name = _job_name(t.job)
            what = name if self._file in ("", name) else f"{name}: {self._file}"
            verb = "Downloading" if t.job.kind == "download" else "Uploading"
            text = f"{verb} {what} — {self._percent}%"
        if self._queue:
            text += f"  ·  {len(self._queue)} queued"
        self.xfer_label.setText(text)
        self.xfer_label.setToolTip(text)
        self.bar.setValue(0 if t.cancelled else self._percent)

    def _on_progress(self, name, percent, figures):
        if self._closing or self._transfer is None or self._transfer.cancelled:
            return
        self._file, self._percent = name, percent
        moved, size, files = figures
        self.transfers.progress(self._transfer.job.log_id, name, percent, moved,
                                size, files)
        self._show_transfer()

    def _on_transfer_done(self, summary, size, files):
        if self._closing:
            return
        notes = ""
        if self._transfer is not None:
            for note in self._transfer.notes:
                self.log.emit(f"[WARN] {note}")
            if self._transfer.notes:
                notes = (f"{_plural(len(self._transfer.notes), 'name')} changed or "
                         f"skipped (see the log)")
                summary += f" — {notes}"
            self.transfers.finish(self._transfer.job.log_id, tl.DONE, error=notes,
                                  size=size, files=files)
        self._notice(summary)
        self.notify.emit(summary, "warn" if notes else "ok")
        self._relist_if_here()

    def _restarting(self) -> bool:
        """The transfer was stopped by :meth:`rebind`: queue it again, first."""
        t = self._transfer
        if t is None or not t.restart:
            return False
        if self.transfers.requeue(t.job.log_id):
            self._queue.appendleft(t.job)
            self.log.emit(f"[WARN] the connection was replaced: {t.job.kind} of "
                          f"{_job_name(t.job)} starts again")
        return True

    def _on_transfer_failed(self, msg):
        if self._closing or self._restarting():
            return
        if self._transfer is not None:
            job = self._transfer.job
            self.transfers.finish(job.log_id, tl.FAILED, error=msg)
            self.notify.emit(f"{job.kind.capitalize()} of {_job_name(job)} failed: "
                             f"{msg}", "error")
        self._error(msg)
        self._relist_if_here()

    def _on_transfer_stopped(self):
        if self._closing or self._transfer is None or self._restarting():
            return
        job = self._transfer.job
        self.transfers.finish(job.log_id, tl.CANCELLED)
        self._notice(f"Cancelled the {job.kind} of {_job_name(job)}", "[WARN] ")
        self._relist_if_here()

    def _relist_if_here(self):
        """An upload — finished, failed or cancelled part-way through a folder —
        may have changed the folder on screen."""
        job = self._transfer.job if self._transfer is not None else None
        if job and job.kind == "upload" and posixpath.dirname(job.remote) == self.cwd:
            self._relist()

    def _on_transfer_finished(self):
        if self._closing:
            return
        self._transfer = None
        self._pump()

    def _keep_or_replace(self, names, exists, where) -> list:
        """Ask ONCE before a batch replaces what is already in *where*; returns
        the names to transfer (all of them, or only the new ones)."""
        clash = [n for n in names if exists(n)]
        if not clash:
            return names
        answer = QMessageBox.question(
            self, "Already there",
            f"{_plural(len(clash))} already in {where}:\n{_some(clash)}\n\n"
            "Replace? (Folders are merged.) \"No\" transfers only the others.")
        return names if answer == QMessageBox.Yes else [n for n in names
                                                        if n not in clash]

    def _download(self):
        entries, cwd = self._selection(), self.cwd
        if not entries or self._closing:
            return
        if len(entries) == 1 and not entries[0].is_dir:
            name = entries[0].name
            local, _ = QFileDialog.getSaveFileName(self, "Save as", local_name(name))
            if local:
                self.queue_download(posixpath.join(cwd, name), local)
            return
        # a folder, or several items: ONE destination for all of them
        dest = QFileDialog.getExistingDirectory(
            self, f"Download {_plural(len(entries))} to")
        if dest:
            self.download_entries_to(dest, entries)

    def download_entries_to(self, dest, entries=None, names=None) -> int:
        """Queue the selection (or *entries*, or the entries called *names*) for
        download into the local folder *dest*; asks once before replacing what
        is already there. Each name is made one this PC can hold
        (:func:`_local_names`). Returns how many were queued."""
        if self._closing or not dest:
            return 0
        if names is not None:
            entries = [self._entries[n] for n in names if n in self._entries]
        elif entries is None:
            entries = self._selection()
        cwd, count = self.cwd, 0
        local = _local_names([e.name for e in entries])
        wanted = set(self._keep_or_replace(
            [local[e.name] for e in entries],
            lambda n: os.path.exists(os.path.join(dest, n)), dest))
        for e in entries:
            name = local[e.name]
            if name not in wanted:
                continue
            if name != e.name:
                self.log.emit(f"[WARN] {e.name} is saved as {name}: the original "
                              f"name is not possible on this PC")
            if self.queue_download(posixpath.join(cwd, e.name),
                                   os.path.join(dest, name), e.is_dir):
                count += 1
        return count

    def download_to_local_pane(self) -> int:
        dest = self.local_target() if self.local_target is not None else None
        return self.download_entries_to(dest) if dest else 0

    def _upload(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        self._upload_paths(files)

    def _upload_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Upload folder")
        if folder:
            self._upload_paths([folder])

    def remote_ignores_case(self) -> bool:
        """Does the remote file system take "Config.ini" and "config.ini" for one
        file? (Windows: the OS the session found, or a "/C:/…" path.)"""
        return (getattr(self.handler, "known_os", None) == "windows"
                or bool(re.match(r"/?[A-Za-z]:", self.cwd or "")))

    def _upload_paths(self, paths):
        """Queue local files / folders for upload into the current folder (the
        Upload dialogs, a drop and the local pane all end here). Upload used to
        overwrite a remote file of the same name without a word — now it asks
        first, also for a name that differs only in case where the remote does
        not tell those apart. Returns how many were queued."""
        if self._closing:
            return 0
        cwd, count = self.cwd, 0
        local = {os.path.basename(os.path.normpath(p)): p for p in paths}
        local.pop("", None)              # a drive root has no name to upload as
        fold = str.casefold if self.remote_ignores_case() else (lambda n: n)
        there = {fold(n) for n in self._entries}
        for name in self._keep_or_replace(list(local), lambda n: fold(n) in there, cwd):
            if self.queue_upload(local[name], posixpath.join(cwd, name)):
                count += 1
        return count

    upload_paths = _upload_paths

    # --- a new connection ---
    def rebind(self, handler) -> None:
        """Carry on over a new connection to the same host (the session tab calls
        this after it reconnected). The transfer queue and history and the open
        editors stay; the SFTP links are re-made on *handler* and the old ones
        closed on a teardown thread; a transfer that was moving starts again (it
        never wrote into its destination, so that is safe) — unless the user
        had cancelled it; whatever else was in flight is dropped, and the
        current folder is listed again."""
        if self._closing or handler is None:
            return
        self.handler = handler
        old = (self._remote, self._xfer)
        self._remote, self._xfer = _OwnSftp(handler), _OwnSftp(handler)
        for link in old:
            link.shut()
            run_teardown(link.close, "sftp-browser-rebind")
        close_jobs(self._jobs)           # their results belong to the old link
        if self._op:
            self._message = (f"{self._op} was interrupted by the reconnect — check "
                             f"the listing")
            self.log.emit(f"[WARN] {self._message}")
        self._op, self._again = "", False
        if self._busy:
            self._set_busy("")
        for editor in self._live_editors():
            editor.rebind(self._remote)
        t = self._transfer
        if t is not None and not t.cancelled:    # a cancelled one stays cancelled
            t.restart = True
            t.cancel()                   # comes back via stopped / failed
            self._show_transfer()
        self._list(self.cwd, self._current_name())

    # --- teardown ---
    def close_browser(self):
        """Stop everything; the owner deletes the widget next. Workers are
        parked (silenced, and kept alive until they finish by themselves), the
        transfer in flight is cancelled, both SFTP clients are closed on a
        teardown thread, and from here on every late callback is a no-op. An
        editor with unsaved changes stays open, offline, so they can still be
        saved to a local file; the others close. Safe to call twice."""
        if self._closing:
            return
        self._closing = True
        self._fill_timer.stop()
        self._stop_timer.stop()
        self._queue.clear()
        self.transfers.cancel_queued()
        for editor in self._live_editors():
            try:
                editor.connection_closed()
            except RuntimeError:         # deleted meanwhile
                pass
        self._editors = []
        transfer, self._transfer = self._transfer, None
        if transfer is not None:
            self.transfers.finish(transfer.job.log_id, tl.CANCELLED)
            transfer.cancel()
            try:                         # park_thread only knows the result signals
                transfer.finished.disconnect(self._on_transfer_finished)
            except Exception:
                pass
            park_thread(transfer)
        close_jobs(self._jobs)
        for link in (self._remote, self._xfer):
            link.shut()
            run_teardown(link.close, "sftp-browser-close")
