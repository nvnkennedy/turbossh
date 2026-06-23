"""Remote file browser over SFTP: navigate, upload, download, mkdir, delete,
rename. Transfers run on their own SFTP channel so the UI stays responsive."""

from __future__ import annotations

import os
import stat
import posixpath

from PyQt5.QtCore import QThread, pyqtSignal, Qt


def _attr_is_dir(attr) -> bool:
    """Safely decide if an SFTP entry is a directory. QNX/embedded servers can
    return None or out-of-range st_mode values that crash stat.S_ISDIR."""
    try:
        m = getattr(attr, "st_mode", None)
        if not m:
            return False
        return bool(stat.S_ISDIR(int(m) & 0o170000))
    except Exception:
        return False

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLineEdit, QListWidget, QListWidgetItem, QFileDialog,
                             QInputDialog, QMessageBox, QLabel)


class _TransferThread(QThread):
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, handler, kind, remote, local):
        super().__init__()
        self.handler, self.kind = handler, kind
        self.remote, self.local = remote, local

    def run(self):
        try:
            # own SFTP channel (don't clash with the browser's listing channel)
            sftp = self.handler.client.open_sftp()
            try:
                if self.kind == "download":
                    sftp.get(self.remote, self.local)
                    self.done.emit(f"Downloaded {self.remote} -> {self.local}")
                else:
                    sftp.put(self.local, self.remote)
                    self.done.emit(f"Uploaded {self.local} -> {self.remote}")
            finally:
                sftp.close()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SftpBrowser(QWidget):
    log = pyqtSignal(str)

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.cwd = "."
        self._threads = []

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.path = QLineEdit(); self.path.returnPressed.connect(self._go)
        up = QPushButton("Up"); up.setProperty("role", "ghost"); up.clicked.connect(self._up)
        ref = QPushButton("Refresh"); ref.setProperty("role", "ghost"); ref.clicked.connect(self.refresh)
        top.addWidget(QLabel("Remote:")); top.addWidget(self.path, 1)
        top.addWidget(up); top.addWidget(ref)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._open_item)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._ctx_menu)
        # keyboard shortcuts scoped to the browser
        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)
        QShortcut(QKeySequence("Delete"), self, activated=self._delete)
        QShortcut(QKeySequence("Backspace"), self, activated=self._up)

        ops = QHBoxLayout()
        for label, slot, role in (("Download", self._download, "ok"),
                                   ("Upload", self._upload, None),
                                   ("Mkdir", self._mkdir, "ghost"),
                                   ("Rename", self._rename, "ghost"),
                                   ("Delete", self._delete, "danger")):
            b = QPushButton(label)
            if role:
                b.setProperty("role", role)
            b.clicked.connect(slot)
            ops.addWidget(b)

        lay.addLayout(top); lay.addWidget(self.list, 1); lay.addLayout(ops)
        self.refresh()

    # --- navigation ---
    def refresh(self):
        sftp = self.handler.sftp()
        try:
            try:
                self.cwd = sftp.normalize(self.cwd)
            except Exception:
                pass
            self.path.setText(self.cwd)
            entries = sftp.listdir_attr(self.cwd)
        except Exception as exc:
            self.log.emit(f"[ERROR] list {self.cwd}: {exc}")
            return
        self.list.clear()
        self.list.addItem(self._mkitem("..", True))
        try:
            entries = sorted(entries, key=lambda x: (not _attr_is_dir(x),
                                                     (x.filename or "").lower()))
        except Exception:
            pass
        for e in entries:
            self.list.addItem(self._mkitem(e.filename, _attr_is_dir(e)))

    def _mkitem(self, name, is_dir):
        it = QListWidgetItem(("📁 " if is_dir else "📄 ") + name)
        it.setData(Qt.UserRole, (name, is_dir))
        return it

    def _ctx_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        m = QMenu(self)
        m.addAction("Download", self._download)
        m.addAction("Upload…", self._upload)
        m.addSeparator()
        m.addAction("New folder", self._mkdir)
        m.addAction("Rename", self._rename)
        m.addAction("Delete", self._delete)
        m.addSeparator()
        m.addAction("Refresh (F5)", self.refresh)
        m.exec_(self.list.viewport().mapToGlobal(pos))

    def _go(self):
        self.cwd = self.path.text().strip() or "."
        self.refresh()

    def _up(self):
        self.cwd = posixpath.dirname(self.cwd.rstrip("/")) or "/"
        self.refresh()

    def _open_item(self, item):
        name, is_dir = item.data(Qt.UserRole)
        if name == "..":
            self._up(); return
        if is_dir:
            self.cwd = posixpath.join(self.cwd, name)
            self.refresh()

    def _selected(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else (None, None)

    # --- operations ---
    def _download(self):
        name, is_dir = self._selected()
        if not name or name == "..":
            return
        if is_dir:
            QMessageBox.information(self, "Download", "Pick a file (folders: use pull --recursive).")
            return
        local, _ = QFileDialog.getSaveFileName(self, "Save as", name)
        if local:
            self._run("download", posixpath.join(self.cwd, name), local)

    def _upload(self):
        local, _ = QFileDialog.getOpenFileName(self, "Upload file")
        if local:
            remote = posixpath.join(self.cwd, os.path.basename(local))
            self._run("upload", remote, local)

    def _run(self, kind, remote, local):
        t = _TransferThread(self.handler, kind, remote, local)
        t.done.connect(lambda m: (self.log.emit("[OK] " + m), self.refresh()))
        t.failed.connect(lambda m: self.log.emit("[ERROR] " + m))
        t.finished.connect(lambda: self._threads.remove(t) if t in self._threads else None)
        self._threads.append(t)
        self.log.emit(f"{kind}…")
        t.start()

    def _mkdir(self):
        name, ok = QInputDialog.getText(self, "New folder", "Name:")
        if ok and name:
            try:
                self.handler.sftp().mkdir(posixpath.join(self.cwd, name))
                self.refresh()
            except Exception as exc:
                self.log.emit(f"[ERROR] mkdir: {exc}")

    def _rename(self):
        name, _ = self._selected()
        if not name or name == "..":
            return
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=name)
        if ok and new:
            try:
                sftp = self.handler.sftp()
                sftp.posix_rename(posixpath.join(self.cwd, name),
                                  posixpath.join(self.cwd, new))
                self.refresh()
            except Exception as exc:
                self.log.emit(f"[ERROR] rename: {exc}")

    def _delete(self):
        name, is_dir = self._selected()
        if not name or name == "..":
            return
        if QMessageBox.question(self, "Delete", f"Delete {name}?") != QMessageBox.Yes:
            return
        try:
            sftp = self.handler.sftp()
            target = posixpath.join(self.cwd, name)
            sftp.rmdir(target) if is_dir else sftp.remove(target)
            self.refresh()
        except Exception as exc:
            self.log.emit(f"[ERROR] delete: {exc}")
