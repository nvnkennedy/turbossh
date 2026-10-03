"""View / edit a remote text file in place, over the file browser's own SFTP
client. Loading and saving run on worker threads; the dialog is non-modal, so
several files can be open while the session carries on."""

from __future__ import annotations

import posixpath
import stat

from PyQt5.QtGui import QFont, QKeySequence
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox,
                             QPlainTextEdit, QPushButton, QShortcut, QVBoxLayout)

from .qtutil import close_jobs, run_job

MAX_EDIT_BYTES = 2 * 1024 * 1024     # larger files: download them instead


class NotEditable(Exception):
    """The file is too big or is not text."""


def read_remote_text(sftp, path):
    """``(text, newline, encoding, mode)`` of a remote text file.

    Line endings are normalised to ``\\n`` for editing and *newline* remembers
    what the file used, so saving does not rewrite every line of a CRLF file.
    """
    info = sftp.stat(path)
    if (info.st_size or 0) > MAX_EDIT_BYTES:
        raise NotEditable(
            f"{posixpath.basename(path)} is {info.st_size / 1048576:.1f} MB — too "
            f"large to edit here (limit {MAX_EDIT_BYTES // 1048576} MB). Download "
            f"it instead.")
    with sftp.open(path, "rb") as fh:
        data = fh.read()
    if b"\0" in data[:8192]:
        raise NotEditable(f"{posixpath.basename(path)} looks like a binary file.")
    try:
        text, encoding = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = data.decode("latin-1"), "latin-1"   # never fails; round-trips
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline, encoding, info.st_mode


def write_remote_text(sftp, path, text, newline="\n", encoding="utf-8", mode=None):
    """Write *text* back with the file's own line endings, encoding and mode."""
    data = text.replace("\r\n", "\n").replace("\n", newline).encode(encoding, "replace")
    with sftp.open(path, "wb") as fh:
        fh.write(data)
    if mode is not None:
        try:
            sftp.chmod(path, stat.S_IMODE(mode))     # a rewrite must not drop +x
        except IOError:
            pass
    return len(data)


class RemoteEditDialog(QDialog):
    """``link`` is the browser's locked SFTP client wrapper (``link.run(fn)``)."""

    def __init__(self, link, path: str, parent=None):
        super().__init__(parent)
        self._link, self._path = link, path
        self._newline, self._encoding, self._mode = "\n", "utf-8", None
        self._jobs: set = set()
        self._loaded = False
        self.setWindowTitle(f"{posixpath.basename(path)} — {path}")
        self.resize(820, 560)

        lay = QVBoxLayout(self)
        self.edit = QPlainTextEdit()
        font = QFont("Cascadia Mono", 10)
        font.setStyleHint(QFont.Monospace)
        self.edit.setFont(font)
        self.edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.edit.setReadOnly(True)
        lay.addWidget(self.edit, 1)

        row = QHBoxLayout()
        self.status = QLabel("Loading…")
        self.save_btn = QPushButton("Save"); self.save_btn.setProperty("role", "ok")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save)
        reload_btn = QPushButton("Reload"); reload_btn.setProperty("role", "ghost")
        reload_btn.clicked.connect(self.load)
        close_btn = QPushButton("Close"); close_btn.setProperty("role", "ghost")
        close_btn.clicked.connect(self.close)
        row.addWidget(self.status, 1); row.addWidget(self.save_btn)
        row.addWidget(reload_btn); row.addWidget(close_btn)
        lay.addLayout(row)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save)
        self.edit.document().modificationChanged.connect(self._on_modified)
        self.load()

    # ---- load / save ----
    def load(self):
        self.status.setText("Loading…")
        link, path = self._link, self._path
        run_job(self._jobs, lambda: link.run(lambda sftp: read_remote_text(sftp, path)),
                self._on_loaded, self._on_failed)

    def _on_loaded(self, result):
        text, self._newline, self._encoding, self._mode = result
        self.edit.setPlainText(text)
        self.edit.setReadOnly(False)
        self.edit.document().setModified(False)
        self._loaded = True
        endings = "CRLF" if self._newline == "\r\n" else "LF"
        self.status.setText(f"{len(text.splitlines())} lines · {self._encoding} · {endings}")

    def _on_failed(self, message):
        self.status.setText(f"⚠ {message}")

    def _on_modified(self, modified):
        self.save_btn.setEnabled(bool(modified) and self._loaded)

    def save(self):
        if not self._loaded or not self.edit.document().isModified():
            return
        text = self.edit.toPlainText()
        link, path = self._link, self._path
        newline, encoding, mode = self._newline, self._encoding, self._mode
        self.status.setText("Saving…")
        self.save_btn.setEnabled(False)
        run_job(self._jobs,
                lambda: link.run(lambda sftp: write_remote_text(
                    sftp, path, text, newline, encoding, mode)),
                lambda size, saved=text: self._on_saved(size, saved), self._on_save_failed)

    def _on_saved(self, size, saved_text):
        # typed more while the save was in flight? then it is still modified
        if self.edit.toPlainText() == saved_text:
            self.edit.document().setModified(False)
        self.status.setText(f"Saved {size} bytes to {self._path}")

    def _on_save_failed(self, message):
        self.status.setText(f"⚠ could not save: {message}")
        self.save_btn.setEnabled(True)

    # ---- closing ----
    def closeEvent(self, event):
        if self.edit.document().isModified():
            answer = QMessageBox.question(
                self, "Unsaved changes",
                f"{posixpath.basename(self._path)} has unsaved changes. Close "
                f"without saving?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.discard()
        event.accept()

    def discard(self):
        """Stop quietly (the browser is closing): nothing may call back here."""
        close_jobs(self._jobs)
        self.edit.document().setModified(False)
