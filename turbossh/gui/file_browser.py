"""Dual-pane file browser: this PC on the left, the remote host on the right, and
the transfer history underneath.

Move files with the two arrow buttons, by dragging between the panes (or in from
Explorer), or from the context menus. The remote pane is :class:`SftpBrowser`,
which owns the transfer queue; this module adds the local pane and ties the two
together. Nothing here blocks the GUI thread: the local listing is Qt's own
asynchronous file-system model, a saved start folder (maybe on a share that is
gone) is checked on a worker, deleting runs on a worker, and everything remote
runs on the remote pane's worker threads.
"""

from __future__ import annotations

import contextlib
import os
import shutil

from PyQt5.QtCore import QDir, QFile, QStandardPaths, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QKeySequence
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QFileSystemModel,
                             QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                             QLineEdit, QMenu, QMessageBox, QPushButton, QShortcut,
                             QSplitter, QTreeView, QVBoxLayout, QWidget)

from .qtutil import close_jobs, run_job
from .sftp_browser import REMOTE_MIME, REMOTE_SOURCE_MIME, SftpBrowser
from .transfer_log import TransferLog
from .transfer_panel import TransferPanel

_HAS_TRASH = hasattr(QFile, "moveToTrash")       # Qt 5.15+


@contextlib.contextmanager
def _com():
    """The recycle bin is a shell (COM) operation, and COM has to be set up on
    the thread that runs it — a worker thread here. Harmless if Qt does it too."""
    ole32 = None
    if os.name == "nt":
        try:
            import ctypes
            ole32 = ctypes.windll.ole32
            if ole32.CoInitializeEx(None, 0x2) < 0:      # COINIT_APARTMENTTHREADED
                ole32 = None             # set up differently already: leave it be
        except Exception:
            ole32 = None
    try:
        yield
    finally:
        if ole32 is not None:
            ole32.CoUninitialize()


def _trash(paths):
    """``(moved, left)``: *paths* sent to the recycle bin, and ``[(path, why)]``
    of those that could not be (worker thread)."""
    moved, left = 0, []
    with _com():
        for path in paths:
            try:
                if QFile.moveToTrash(path):
                    moved += 1
                    continue
            except Exception:
                pass
            left.append((path, "not on a drive with a recycle bin (a network share?), "
                               "or the recycle bin refused it"))
    return moved, left


def _remove_for_good(paths):
    """``(removed, errors)``: *paths* deleted permanently (worker thread)."""
    removed, errors = 0, []
    for path in paths:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except OSError as exc:
            errors.append(f"{os.path.basename(path)}: {exc}")
    return removed, errors


def _names(paths, limit=5) -> str:
    more = f" and {len(paths) - limit} more" if len(paths) > limit else ""
    return ", ".join(os.path.basename(p) for p in paths[:limit]) + more


class _LocalView(QTreeView):
    """The local listing. Rows can be dragged out (onto the remote pane, or into
    Explorer); what it accepts is ONLY a drag from its own remote pane, which
    means "download here". Local-to-local drops are refused, so files are never
    moved around on disk by a slip of the mouse — and so are rows dragged from
    another session's remote pane: their names would be looked up in THIS
    pane's remote folder, and a different file downloaded."""
    remote_dropped = pyqtSignal(list)    # names of remote entries dropped here
    refused = pyqtSignal(str)            # why a drag from elsewhere is not taken

    def __init__(self, parent=None):
        super().__init__(parent)
        self.accepts = None              # callable -> the REMOTE_SOURCE_MIME we take
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

    def remote_names(self, mime):
        """``(names, refused)`` of a drag: the remote names it carries if it
        comes from this pane's own remote pane (and folder), else ``[]`` — with
        *refused* True when it is remote rows from somewhere else."""
        if not mime.hasFormat(REMOTE_MIME):
            return [], False
        source = bytes(mime.data(REMOTE_SOURCE_MIME)).decode("utf-8", "replace")
        if self.accepts is None or not source or source != self.accepts():
            return [], True
        text = bytes(mime.data(REMOTE_MIME)).decode("utf-8", "replace")
        return [name for name in text.split("\n") if name], False

    def _take(self, event) -> list:
        names, _refused = self.remote_names(event.mimeData())
        if names:
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()
        return names

    def dragEnterEvent(self, event):
        if self.remote_names(event.mimeData())[1]:
            self.refused.emit("Those remote files belong to another session's file "
                              "browser — drop them on the local pane next to it.")
        self._take(event)

    def dragMoveEvent(self, event):
        self._take(event)

    def dropEvent(self, event):
        names = self._take(event)
        if names:
            self.remote_dropped.emit(names)


class LocalPane(QWidget):
    """This PC. ``cwd`` is the folder shown ("" = the list of drives)."""
    upload_requested = pyqtSignal(list)  # local paths to send to the remote pane
    message = pyqtSignal(str)
    deleted = pyqtSignal(int)            # a delete finished: how many items went

    def __init__(self, start_dir: str = "", parent=None):
        super().__init__(parent)
        self.cwd = ""
        self._jobs = set()               # start-folder check, deletes
        self._moved = False              # navigated since the pane opened
        self._deleting = False
        self.model = QFileSystemModel(self)
        # read-only: the model then refuses drops and in-place edits; renames and
        # deletes go through the explicit actions below, with confirmation
        self.model.setReadOnly(True)
        self.model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.path = QLineEdit(); self.path.returnPressed.connect(self._go)
        top.addWidget(QLabel("Local:")); top.addWidget(self.path, 1)
        for label, tip, slot in (
                ("Up", "Parent folder", self.up),
                ("Home", "Your user folder", lambda: self.navigate(os.path.expanduser("~"))),
                ("Desktop", "Desktop", lambda: self._standard(QStandardPaths.DesktopLocation)),
                ("Downloads", "Downloads", lambda: self._standard(QStandardPaths.DownloadLocation)),
                ("Drives", "All drives", lambda: self.navigate(""))):
            button = QPushButton(label); button.setProperty("role", "ghost")
            button.setToolTip(tip); button.clicked.connect(slot)
            top.addWidget(button)
        lay.addLayout(top)

        self.view = _LocalView()
        self.view.setModel(self.model)
        self.view.setRootIsDecorated(False)          # a flat list, like the remote pane
        self.view.setItemsExpandable(False)
        self.view.setUniformRowHeights(True)
        self.view.setAllColumnsShowFocus(True)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.AscendingOrder)
        header = self.view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.view.activated.connect(self._open_index)        # double-click / Enter
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        lay.addWidget(self.view, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        for keys, slot in (("F5", self.refresh), ("Backspace", self.up),
                           ("Delete", self.delete_selected), ("F2", self.rename_selected)):
            QShortcut(QKeySequence(keys), self.view, activated=slot,
                      context=Qt.WidgetWithChildrenShortcut)
        # Start at home. The folder saved last time may be on a share that is
        # gone, and asking Windows about it here hung the window for the SMB
        # timeout (on every reconnect, too): look on a worker, then go there.
        self.navigate(os.path.expanduser("~"))
        self._moved = False
        start = os.path.abspath(os.path.expanduser(start_dir.strip())) if start_dir else ""
        if start and os.path.normcase(start) != os.path.normcase(self.cwd):
            run_job(self._jobs, lambda: os.path.isdir(start),
                    lambda ok: self._saved_folder(start, ok))

    def _saved_folder(self, path, ok):
        if ok and not self._moved:       # the user has not gone elsewhere meanwhile
            self.navigate(path, checked=True)

    def close_pane(self):
        """Drop the workers' results (the owner deletes the pane next)."""
        close_jobs(self._jobs)

    # ---- navigation ----
    def navigate(self, path: str, checked: bool = False) -> bool:
        """Show *path* ("" = the drives). *checked*: it is known to be a folder."""
        path = (path or "").strip()
        if path:
            path = os.path.abspath(os.path.expanduser(path))
            if not checked and not os.path.isdir(path):
                self._say(f"Not a folder: {path}")
                self.path.setText(self.cwd)
                return False
        self._moved = True
        self.cwd = path
        self.view.setRootIndex(self.model.setRootPath(path))
        self.path.setText(path)
        self._say("")
        return True

    def _go(self):
        self.navigate(self.path.text())

    def _standard(self, location):
        self.navigate(QStandardPaths.writableLocation(location))

    def up(self):
        if not self.cwd:
            return
        parent = os.path.dirname(self.cwd.rstrip("\\/"))
        # above a drive root ("C:\\") is the list of drives; above "/" is "/"
        if not parent or os.path.normcase(parent) == os.path.normcase(self.cwd.rstrip("\\/")) \
                or parent == self.cwd:
            parent = "" if os.name == "nt" else "/"
        self.navigate(parent)

    def refresh(self):
        self.navigate(self.cwd)

    def _open_index(self, index):
        path = self.model.filePath(index)
        if self.model.isDir(index):
            self.navigate(path)

    # ---- selection ----
    def selection_paths(self) -> list:
        rows = self.view.selectionModel().selectedRows(0)
        return [os.path.normpath(self.model.filePath(i)) for i in rows]

    def _say(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))
        if text:
            self.message.emit(text)

    # ---- actions ----
    def _context_menu(self, pos):
        paths = self.selection_paths()
        menu = QMenu(self)
        if paths and self.cwd:
            menu.addAction("Upload to the remote pane", lambda: self.upload_requested.emit(paths))
        if len(paths) == 1:
            menu.addAction("Open", lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(paths[0])))
        if self.cwd:
            menu.addAction("Show this folder in the file manager",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.cwd)))
            menu.addSeparator()
            menu.addAction("New folder", self.new_folder)
        if len(paths) == 1:
            menu.addAction("Rename (F2)", self.rename_selected)
        if paths:
            menu.addAction("Delete", self.delete_selected)
            menu.addAction("Copy path", lambda: QApplication.clipboard().setText(
                "\n".join(paths)))
        menu.addSeparator()
        menu.addAction("Refresh (F5)", self.refresh)
        menu.exec_(self.view.viewport().mapToGlobal(pos))

    def do_new_folder(self, name: str) -> bool:
        name = (name or "").strip()
        if not self.cwd or not name or os.sep in name or "/" in name:
            self._say("New folder: give a plain name (inside a folder, not the "
                      "drive list).")
            return False
        try:
            os.mkdir(os.path.join(self.cwd, name))
        except OSError as exc:
            self._say(f"Could not create {name}: {exc}")
            return False
        return True

    def new_folder(self):
        name, ok = QInputDialog.getText(self, "New folder", "Name:")
        if ok and name.strip():
            self.do_new_folder(name)

    def do_rename(self, path: str, new_name: str) -> bool:
        new_name = (new_name or "").strip()
        target = os.path.join(os.path.dirname(path), new_name)
        if not new_name or os.sep in new_name or "/" in new_name:
            self._say("Rename: give a plain name, not a path.")
            return False
        if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(path):
            self._say(f"Rename: '{new_name}' already exists.")
            return False
        try:
            os.rename(path, target)
        except OSError as exc:
            self._say(f"Could not rename: {exc}")
            return False
        return True

    def rename_selected(self):
        paths = self.selection_paths()
        if len(paths) != 1:
            return
        old = os.path.basename(paths[0])
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=old)
        if ok and new.strip() and new != old:
            self.do_rename(paths[0], new)

    def do_delete(self, paths) -> bool:
        """Send *paths* to the recycle bin, on a worker thread (a big folder or a
        slow disk must not freeze the window). Whatever cannot go there is
        deleted for good only if the user says so: the question they answered
        promised the recycle bin, and a failed move used to fall through to a
        permanent delete without a word. ``deleted(count)`` follows. False if a
        delete is still running."""
        paths = list(paths)
        if self._deleting or not paths:
            return False
        self._deleting = True
        if not _HAS_TRASH:               # the question already said "permanently"
            self._say(f"Deleting {_names(paths)}…")
            run_job(self._jobs, lambda: _remove_for_good(paths),
                    lambda result: self._removed(0, result), self._delete_failed)
        else:
            self._say(f"Moving {_names(paths)} to the recycle bin…")
            run_job(self._jobs, lambda: _trash(paths), self._trashed, self._delete_failed)
        return True

    def _trashed(self, result):
        moved, left = result
        if not left:
            self._delete_done(moved, "")
            return
        paths = [path for path, _why in left]
        answer = QMessageBox.question(
            self, "Not moved to the recycle bin",
            f"{len(paths)} item(s) could not be moved to the recycle bin:\n"
            f"{_names(paths)}\n\nThey are {left[0][1]}.\n\nDelete them permanently "
            f"instead? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            self._delete_done(moved, f"Not deleted (still there): {_names(paths)}")
            return
        run_job(self._jobs, lambda: _remove_for_good(paths),
                lambda result: self._removed(moved, result), self._delete_failed)

    def _removed(self, moved, result):
        removed, errors = result
        self._delete_done(moved + removed,
                          ("Could not delete " + "; ".join(errors[:3])) if errors else "")

    def _delete_failed(self, msg):
        self._delete_done(0, f"Could not delete: {msg}")

    def _delete_done(self, count, problem):
        self._deleting = False
        self._say(problem)
        self.deleted.emit(count)

    def delete_selected(self):
        paths = self.selection_paths()
        if not paths:
            return
        where = "moved to the recycle bin" if _HAS_TRASH else "deleted permanently"
        answer = QMessageBox.question(
            self, "Delete",
            f"Delete {len(paths)} local item(s)?\n{_names(paths)}\n\nThey are {where}.")
        if answer == QMessageBox.Yes:
            self.do_delete(paths)


class FileBrowser(QWidget):
    """Local pane | transfer buttons | remote pane, over the Transfers panel."""
    log = pyqtSignal(str)
    notify = pyqtSignal(str, str)        # text, kind — the main window's toasts

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        from . import settings
        self.transfers = TransferLog()
        self.local = LocalPane(settings.get("local_browser_dir") or "")
        self.remote = SftpBrowser(handler, transfer_log=self.transfers)
        self.remote.local_target = lambda: self.local.cwd or None
        self.remote.show_inline_progress = False     # the panel below shows it
        self.remote.log.connect(self.log)
        self.remote.notify.connect(self.notify)

        middle = QWidget()
        column = QVBoxLayout(middle)
        column.setContentsMargins(2, 0, 2, 0)
        self.upload_btn = QPushButton("Upload  →"); self.upload_btn.setProperty("role", "ok")
        self.upload_btn.setToolTip("Send the selected local items to the remote folder")
        self.upload_btn.clicked.connect(lambda: self.upload())
        self.download_btn = QPushButton("←  Download"); self.download_btn.setProperty("role", "ok")
        self.download_btn.setToolTip("Fetch the selected remote items into the local folder")
        self.download_btn.clicked.connect(self.download)
        column.addStretch(1)
        column.addWidget(self.upload_btn); column.addWidget(self.download_btn)
        column.addStretch(1)

        panes = QSplitter(Qt.Horizontal)
        panes.setChildrenCollapsible(False)
        panes.addWidget(self.local); panes.addWidget(middle); panes.addWidget(self.remote)
        panes.setStretchFactor(0, 1); panes.setStretchFactor(1, 0); panes.setStretchFactor(2, 1)

        self.panel = TransferPanel(self.transfers)
        self.panel.retry_requested.connect(self.remote.retry)
        self.panel.cancel_requested.connect(self.remote.cancel_transfer)

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(panes); split.addWidget(self.panel)
        split.setStretchFactor(0, 4); split.setStretchFactor(1, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(split)

        self.local.upload_requested.connect(self.upload)
        self.local.view.remote_dropped.connect(self._download_names)
        # the local pane takes drops of THIS remote pane's rows only
        self.local.view.accepts = lambda: self.remote.tree.source
        self.local.view.refused.connect(self.local._say)

    # ---- the two directions ----
    def upload(self, paths=None) -> int:
        """Queue local *paths* (default: the local selection) for upload into the
        remote pane's folder."""
        paths = list(paths) if paths else self.local.selection_paths()
        if not paths:
            self.local._say("Select the local files or folders to upload first.")
            return 0
        return self.remote.upload_paths(paths)

    def download(self) -> int:
        """Queue the remote selection for download into the local pane's folder."""
        if not self.local.cwd:
            self.local._say("Open a local folder to download into first.")
            return 0
        return self.remote.download_to_local_pane()

    def _download_names(self, names) -> int:
        if not self.local.cwd:
            self.local._say("Open a local folder to download into first.")
            return 0
        return self.remote.download_entries_to(self.local.cwd, names=names)

    # ---- what the session tab expects of its file page ----
    @property
    def busy(self) -> bool:
        return self.remote.busy

    def rebind(self, handler) -> None:
        """The session reconnected with a new *handler*: carry on over it instead
        of being rebuilt. The transfer queue and history, the open editors (with
        their unsaved changes) and the local pane stay as they are; the remote
        pane's SFTP links move to *handler* and its folder is listed again
        (:meth:`SftpBrowser.rebind`)."""
        self.remote.rebind(handler)

    def close_browser(self):
        """Stop everything (the owner deletes the widget next). Safe to call twice."""
        try:
            from . import settings
            if self.local.cwd and self.local.cwd != settings.get("local_browser_dir"):
                settings.save({"local_browser_dir": self.local.cwd})
        except Exception:
            pass
        self.local.close_pane()
        self.panel.close_panel()
        self.remote.close_browser()
