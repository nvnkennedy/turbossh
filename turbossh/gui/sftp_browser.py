"""Remote file browser over SFTP: navigate, upload, download, mkdir, delete,
rename.

Nothing here blocks the GUI thread. Listings and the small operations run on
worker threads through ONE SFTP client of the browser's own; transfers are queued
and moved one at a time by a worker thread with a second client, so a large file
never holds up browsing. The session's shared ``handler.sftp()`` is never used:
Paramiko can hand one thread's reply to another when two threads share a client,
and the thread that lost its reply then waits forever."""

from __future__ import annotations

import os
import stat
import time
import posixpath
import threading
from collections import deque, namedtuple

from PyQt5.QtCore import QMimeData, QThread, pyqtSignal, Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QTreeWidget, QTreeWidgetItem, QFileDialog,
                             QInputDialog, QMessageBox, QLabel, QProgressBar,
                             QAbstractItemView, QHeaderView, QShortcut, QMenu,
                             QStyle, QApplication, QSizePolicy)

from . import transfer_log as tl
from .qtutil import run_job, close_jobs, park_thread, run_teardown

_COLUMNS = ("Name", "Size", "Modified", "Permissions")
# what a drag out of the remote list carries: the names of the dragged entries,
# one per line (the local pane turns a drop of this into downloads)
REMOTE_MIME = "application/x-turbossh-remote"
_PROGRESS_EVERY = 0.1    # s between progress signals: a fast link calls back
                         # thousands of times a second, a label needs ~10
_CLOSE_WAIT = 2.0        # s a closing browser lets a busy worker finish before
                         # its SFTP client is closed under it


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


# is_dir is the type AFTER following a symlink; mode stays the entry's own, so
# the Permissions column still shows a link as one ("lrwxrwxrwx")
_Entry = namedtuple("_Entry", "name is_dir is_link size mtime mode")


def _list_dir(sftp, path):
    """(normalised path, entries) of a remote folder — folders first, then by
    name ignoring case. Runs on a worker thread."""
    try:
        path = sftp.normalize(path)
    except Exception:
        pass                             # the listing below reports a bad path
    entries = []
    for attr in sftp.listdir_attr(path):
        is_link, target = _attr_type(attr) == stat.S_IFLNK, attr
        if is_link:
            # a link's own mode says nothing about what it points at: one stat
            # tells a linked folder (which can be entered) from a linked file
            try:
                target = sftp.stat(posixpath.join(path, attr.filename))
            except Exception:
                pass                     # dangling: listed as a file
        entries.append(_Entry(attr.filename, _attr_is_dir(target), is_link,
                              getattr(target, "st_size", None),
                              getattr(target, "st_mtime", None),
                              getattr(attr, "st_mode", None)))
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
        except IOError:
            pass
        else:
            raise IOError(f"{dst} already exists")
    try:
        sftp.posix_rename(src, dst)
    except IOError:
        # posix-rename is an OpenSSH extension; servers without it get the plain
        # SFTP rename (a genuine failure fails again there and is reported)
        sftp.rename(src, dst)


class _Cancelled(Exception):
    """Aborts a transfer from its callback. Deliberately not an IOError: push /
    pull wrap those in SSHTransferError, and this must come back as itself."""


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
            return fn(self._open())

    def _open(self):
        if self._shut:
            raise _Cancelled("the file browser was closed")
        client = self._client
        chan = client.get_channel() if client is not None else None
        if chan is None or chan.closed:
            client = self._handler.client.open_sftp()
            if self._shut:               # closed while the channel was opening
                client.close()
                raise _Cancelled("the file browser was closed")
            self._client = client
        return client

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


class _FileWatch:
    """What push / pull are given as their SFTP client: the real one plus a hook
    per file. Their callback only carries byte counts, so this is how the
    progress label learns WHICH file is moving — and how a cancel is noticed
    between the files of a folder (an empty file never reaches the callback)."""

    def __init__(self, sftp, on_file):
        self._sftp, self._on_file = sftp, on_file

    def __getattr__(self, name):
        return getattr(self._sftp, name)

    def get(self, remotepath, localpath, *args, **kwargs):
        self._on_file(remotepath)
        return self._sftp.get(remotepath, localpath, *args, **kwargs)

    def put(self, localpath, remotepath, *args, **kwargs):
        self._on_file(remotepath)
        try:
            return self._sftp.put(localpath, remotepath, *args, **kwargs)
        except _Cancelled:
            # pull deletes its partial local file; likewise a half-written
            # upload must not be left to pass for the real file. Only on a
            # cancel — that proves the file was opened and written by us.
            try:
                self._sftp.remove(remotepath)
            except Exception:
                pass
            raise


class _TransferThread(QThread):
    """Move ONE queued item with handler.pull / handler.push, on the transfer
    client. The signal names are the ones qtutil.park_thread silences."""
    # file being moved, percent of that file, (bytes moved in all, size of this
    # file, files completed)
    progress = pyqtSignal(str, int, object)
    done = pyqtSignal(str, object, int)  # summary line, bytes, files
    failed = pyqtSignal(str)
    stopped = pyqtSignal()               # cancelled

    def __init__(self, handler, link, job):
        super().__init__()
        self.job = job
        self._handler, self._link = handler, link
        self._cancel = threading.Event()
        self._file, self._last = "", 0.0
        self._before = 0                 # bytes of the files already finished
        self._here = self._size = 0      # bytes done / size of the current file
        self._files = 0

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self):
        try:
            result = self._link.run(self._move)
        except Exception as exc:        # noqa: BLE001 - reported to the GUI
            # whatever a cancelled transfer died of, the user asked for it
            if self.cancelled or isinstance(exc, _Cancelled):
                self.stopped.emit()
            else:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(str(result), getattr(result, "size_bytes", None),
                           int(getattr(result, "files", 1) or 1))

    def _move(self, sftp):
        job = self.job
        how = dict(recursive=True, callback=self._on_bytes, safe=False,
                   sftp=_FileWatch(sftp, self._on_file))
        if job.kind == "download":
            return self._handler.pull(job.remote, job.local, **how)
        return self._handler.push(job.local, job.remote, **how)

    def _on_file(self, remote):
        if self.cancelled:
            raise _Cancelled()
        if self._file:                   # the previous file is complete
            self._before += self._here
            self._files += 1
        self._here = self._size = 0
        self._file = posixpath.basename(remote)
        self._report(0, force=True)

    def _on_bytes(self, done, total):
        if self.cancelled:
            raise _Cancelled()
        self._here, self._size = done, total
        self._report(int(done * 100 / total) if total else 0)

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
    """A listing row that sorts by the clicked column — with ".." pinned on top
    and folders before files whichever way the column is sorted."""

    def __init__(self, texts, rank, keys):
        super().__init__(texts)
        self.rank, self.keys = rank, keys        # rank: 0 "..", 1 folder, 2 file

    def __lt__(self, other):
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        ascending = (tree is None
                     or tree.header().sortIndicatorOrder() == Qt.AscendingOrder)
        if self.rank != other.rank:
            # Qt reverses the result for a descending sort; undo that here so
            # the grouping stays put
            return (self.rank < other.rank) == ascending
        mine, theirs = self.keys[column], other.keys[column]
        if mine == theirs:
            mine, theirs = self.keys[0], other.keys[0]   # ties: by name
        return mine < theirs


class _FileTree(QTreeWidget):
    """The listing — which also takes local files and folders dragged in from
    Explorer (or from the local pane), and lets its own rows be dragged out. The
    item view's own drag handlers refuse anything that is not its model's data,
    hence the overrides."""
    dropped = pyqtSignal(list)           # local paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def mimeTypes(self):
        return [REMOTE_MIME]

    def mimeData(self, items):
        """Dragging rows out carries their names (never ".."); the local pane
        downloads what is dropped on it."""
        names = [it.data(0, Qt.UserRole) for it in items]
        names = [n for n in names if n and n != ".."]
        data = QMimeData()
        if names:
            data.setData(REMOTE_MIME, "\n".join(names).encode("utf-8"))
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
        self._editors = []
        self.cwd = "."                   # only ever the last folder that LISTED
        self._entries = {}               # name -> _Entry: the listing on screen
        self._jobs = set()               # listing / operation workers
        self._remote = _OwnSftp(handler)     # listings, mkdir, rename, delete
        self._xfer = _OwnSftp(handler)       # transfers
        self._queue = deque()            # _Job items waiting their turn
        self._transfer = None            # the _TransferThread moving one now
        self._file, self._percent = "", 0
        self._busy = ""                  # what the list is waiting for; "" = idle
        self._message = ""               # last error / result, until the next action
        self._summary = ""
        self._target = self._select = None
        self._op = ""
        self._again = False              # re-list as soon as the list is idle
        self._focus_back = None
        self._closing = False
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
        # click a column to sort by it (folders stay first, ".." stays on top)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
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
        current = self.tree.currentItem()
        self._list(self.cwd, current.data(0, Qt.UserRole) if current else None)

    def _list(self, path, select=None) -> bool:
        self._target, self._select = path, select
        self._set_busy(f"Loading {path}…")
        remote = self._remote            # the worker must not reach into the widget
        run_job(self._jobs, lambda: remote.run(lambda sftp: _list_dir(sftp, path)),
                self._on_listed, self._on_list_fail)
        return True

    def _on_listed(self, result):
        if self._closing:
            return
        self.cwd, entries = result
        self._entries = {e.name: e for e in entries}
        self._summary = _plural(len(entries))
        self.path.setText(self.cwd)
        items = [self._item("..", self._icons[True, False], 0, ("", -1, 0, ""))]
        for e in entries:
            it = self._item(e.name, self._icons[e.is_dir, e.is_link],
                            1 if e.is_dir else 2,
                            ((e.name or "").lower(), e.size or 0, e.mtime or 0,
                             _perms(e.mode)),
                            "" if e.is_dir else _human(e.size), _when(e.mtime),
                            _perms(e.mode))
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            items.append(it)
        self.tree.clear()
        self.tree.addTopLevelItems(items)
        # keep the keyboard's place: the entry that was current (refresh), just
        # made (mkdir / rename) or just left (Up) — else the top of the list
        current = next((it for it in items[1:]
                        if it.data(0, Qt.UserRole) == self._select), items[0])
        self.tree.setCurrentItem(current)
        self.tree.scrollToItem(current)
        self._settled()

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
        if len(entries) == 1 and not entries[0].is_dir:
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

    def edit_selected(self):
        """Open the selected remote text file in an editor window."""
        entries = self._selection()
        if len(entries) != 1 or entries[0].is_dir or self._closing:
            return None
        from .remote_edit import RemoteEditDialog
        dialog = RemoteEditDialog(self._remote, posixpath.join(self.cwd, entries[0].name),
                                  self.window())
        self._editors = [d for d in self._editors if d.isVisible()] + [dialog]
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
        self._set_busy("")
        self._list(self.cwd, self._select)

    def _op_failed(self, msg):
        if self._closing:
            return
        self._set_busy("")
        self._error(f"{self._op} failed: {msg}")
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
        and drop the queued ones. The partial file is removed."""
        if self._closing or self._transfer is None:
            return False
        self._queue.clear()
        self.transfers.cancel_queued()
        self._transfer.cancel()
        self.cancel_btn.setEnabled(False)
        self._show_transfer()
        return True

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
        t = self._transfer = _TransferThread(self.handler, self._xfer, job)
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
        if t.cancelled:
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
        if self._transfer is not None:
            self.transfers.finish(self._transfer.job.log_id, tl.DONE,
                                  size=size, files=files)
        self._notice(summary)
        self.notify.emit(summary, "ok")
        self._relist_if_here()

    def _on_transfer_failed(self, msg):
        if self._closing:
            return
        if self._transfer is not None:
            job = self._transfer.job
            self.transfers.finish(job.log_id, tl.FAILED, error=msg)
            self.notify.emit(f"{job.kind.capitalize()} of {_job_name(job)} failed: "
                             f"{msg}", "error")
        self._error(msg)
        self._relist_if_here()

    def _on_transfer_stopped(self):
        if self._closing or self._transfer is None:
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
            local, _ = QFileDialog.getSaveFileName(self, "Save as", name)
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
        is already there. Returns how many were queued."""
        if self._closing or not dest:
            return 0
        if names is not None:
            entries = [self._entries[n] for n in names if n in self._entries]
        elif entries is None:
            entries = self._selection()
        cwd, count = self.cwd, 0
        wanted = self._keep_or_replace(
            [e.name for e in entries],
            lambda n: os.path.exists(os.path.join(dest, n)), dest)
        for e in entries:
            if e.name in wanted and self.queue_download(
                    posixpath.join(cwd, e.name), os.path.join(dest, e.name), e.is_dir):
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

    def _upload_paths(self, paths):
        """Queue local files / folders for upload into the current folder (the
        Upload dialogs, a drop and the local pane all end here). Upload used to
        overwrite a remote file of the same name without a word — now it asks
        first. Returns how many were queued."""
        if self._closing:
            return 0
        cwd, count = self.cwd, 0
        local = {os.path.basename(os.path.normpath(p)): p for p in paths}
        local.pop("", None)              # a drive root has no name to upload as
        for name in self._keep_or_replace(list(local), self._entries.__contains__, cwd):
            if self.queue_upload(local[name], posixpath.join(cwd, name)):
                count += 1
        return count

    upload_paths = _upload_paths

    # --- teardown ---
    def close_browser(self):
        """Stop everything; the owner deletes the widget next. Workers are
        parked (silenced, and kept alive until they finish by themselves), the
        transfer in flight is cancelled, both SFTP clients are closed on a
        teardown thread, and from here on every late callback is a no-op.
        Safe to call twice."""
        if self._closing:
            return
        self._closing = True
        self._queue.clear()
        self.transfers.cancel_queued()
        for editor in self._editors:
            try:
                editor.discard()
                editor.close()
            except Exception:
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
