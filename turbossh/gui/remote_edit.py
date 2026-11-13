"""View / edit a remote text file in place, over the file browser's own SFTP
client. Loading and saving run on worker threads; the dialog is non-modal, so
several files can be open while the session carries on.

Saving takes care of the file it replaces: it asks first if the file changed on
the remote since it was loaded, writes a temporary file and renames it over the
original (keeping its permissions), and a file the editor could not hand back
exactly as it read it — some characters, mixed line endings, invalid UTF-8 — is
opened read-only instead of being quietly rewritten."""

from __future__ import annotations

import os
import posixpath
import re
import weakref
from collections import namedtuple

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QKeySequence
from PyQt5.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                             QPlainTextEdit, QPushButton, QShortcut, QVBoxLayout)

from .. import _transfer
from .qtutil import close_jobs, run_job
from .sftp_browser import (_attr_type, _part_name, kind_problem, local_name,
                           resolve_remote_link, sip)

MAX_EDIT_BYTES = 2 * 1024 * 1024     # larger files: download them instead

_OPEN = weakref.WeakSet()            # every editor window, for unsaved_editors()


class NotEditable(Exception):
    """The file is too big, is not text, or is not a regular file."""


class ChangedOnRemote(Exception):
    """The file on the remote is not the one that was loaded any more."""


# text has "\n" line ends; newline is what the file used; stamp is (size, mtime)
# when loaded; problem says why it may only be viewed ("" = it can be edited)
Loaded = namedtuple("Loaded", "text newline encoding mode stamp problem")

# a valid multi-byte UTF-8 sequence: a file with these and a stray bad byte is
# UTF-8 with damage, not a file in a legacy 8-bit encoding
_UTF8_SEQ = re.compile(rb"[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}"
                       rb"|[\xf0-\xf4][\x80-\xbf]{3}")


def _stamp(attr):
    return getattr(attr, "st_size", None), getattr(attr, "st_mtime", None)


def _decode(data):
    """``(text, encoding, problem)``. UTF-8 if it is; a legacy 8-bit file
    (cp1252, else latin-1: one character per byte) round-trips byte for byte.
    But UTF-8 with a stray bad byte must not be read as latin-1: every "é" would
    become two characters, and any of them the editor alters (0xA0 is a
    non-breaking space, saved as a space) would break the UTF-8 around it. It
    is shown with the bad bytes as U+FFFD — and only shown."""
    try:
        return data.decode("utf-8"), "utf-8", ""
    except UnicodeDecodeError:
        pass
    if _UTF8_SEQ.search(data):
        return (data.decode("utf-8", "replace"), "utf-8",
                "it is not valid UTF-8 (the bad bytes are shown as �)")
    try:
        return data.decode("cp1252"), "cp1252", ""
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1", ""


def read_remote_text(sftp, path) -> Loaded:
    """Read a remote text file for editing (on a worker thread).

    Line endings are normalised to ``\\n`` and *newline* remembers what the
    file used, so saving does not rewrite every line of a CRLF file. No device
    or FIFO is read, and never more than MAX_EDIT_BYTES + 1 bytes: the size of
    a device or a FIFO is 0, and reading one to its end filled the memory
    (/dev/zero) or never came back (a FIFO, /dev/ser1). A file the server
    gives no type for is read — that bound is what keeps it safe."""
    info = sftp.stat(path)
    name = posixpath.basename(path)
    problem = kind_problem(_attr_type(info))
    if problem:
        raise NotEditable(f"{name} {problem} — only a regular file can be opened "
                          f"here.")
    too_large = NotEditable(
        f"{name} is more than {MAX_EDIT_BYTES // 1048576} MB — too large to edit "
        f"here. Download it instead.")
    if (info.st_size or 0) > MAX_EDIT_BYTES:
        raise too_large
    with sftp.open(path, "rb") as fh:
        data = fh.read(MAX_EDIT_BYTES + 1)
    if len(data) > MAX_EDIT_BYTES:       # it grew, or its size was not true
        raise too_large
    if b"\0" in data[:8192]:
        raise NotEditable(f"{name} looks like a binary file.")
    text, encoding, problem = _decode(data)
    crlf = text.count("\r\n")
    if crlf and crlf != text.count("\n") and not problem:
        problem = "it mixes CRLF and LF line endings (saving would make them all one kind)"
    return Loaded(text.replace("\r\n", "\n"), "\r\n" if crlf else "\n", encoding,
                  info.st_mode, _stamp(info), problem)


def write_remote_text(sftp, path, data: bytes, *, stamp=None, mode=None):
    """Save *data* as the remote file *path* (on a worker thread); returns
    ``(bytes written, new stamp, how)``.

    *stamp* is the (size, mtime) the file had when it was loaded: if it is not
    that any more, :class:`ChangedOnRemote` is raised and nothing is written.
    Saving follows TurboSSH's one transfer policy (turbossh._transfer, the same
    as uploads): a temporary file next to it replaces it with its mode, owner
    and group — the file is never truncated first. It is written in place where
    replacing would change what the file is (hard links) or cannot be done (a
    folder that is not writable, an owner that cannot be kept, no room for a
    second copy); *how* then says so. *mode* is used if the file is gone."""
    name = posixpath.basename(path)
    target, attr = resolve_remote_link(sftp, path)       # edit the file, not the link
    if stamp is not None:
        if attr is not None and _attr_type(attr) == _transfer.S_IFLNK:
            # a link the server did not resolve (it is written through): the
            # file was loaded through it, so compare what it points at
            attr = _transfer.stat_or_none(sftp, target)
        if attr is None:
            raise ChangedOnRemote(f"{name} was deleted on the remote after you "
                                  f"opened it.")
        if _stamp(attr) != tuple(stamp):
            raise ChangedOnRemote(f"{name} was changed on the remote after you "
                                  f"opened it (by someone or something else).")
    how = _transfer.put_data(sftp, data, target, existing=attr, mode=mode)
    return len(data), _stamp(sftp.stat(target)), how


def _write_local(path, data: bytes) -> int:
    """Write a local file through a temporary one (worker thread)."""
    tmp = os.path.join(os.path.dirname(path) or ".", _part_name(os.path.basename(path)))
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return len(data)


def _lossy(text) -> str:
    """What the editor changed of *text* (it did not hand it back as given)."""
    found = [what for char, what in (
        (" ", "non-breaking spaces (into spaces)"),
        ("\r", "carriage returns that do not end a line (into line ends)"),
        (" ", "Unicode line separators"),
        (" ", "Unicode paragraph separators")) if char in text]
    return ("saving would change its " + ", ".join(found) if found
            else "the editor cannot keep some of its characters as they are")


def unsaved_editors() -> list:
    """Editor windows with changes not saved anywhere — for whoever is about to
    quit the application (an offline editor outlives its session tab)."""
    return [d for d in list(_OPEN) if not sip.isdeleted(d) and d.has_unsaved_changes()]


class RemoteEditDialog(QDialog):
    """``link`` is the browser's locked SFTP client wrapper (``link.run(fn)``).
    The window deletes itself when closed."""

    def __init__(self, link, path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)       # closed editors used to pile up
        _OPEN.add(self)
        self._link, self._path = link, path
        self._newline, self._encoding, self._mode = "\n", "utf-8", None
        self._stamp = None
        self._jobs: set = set()
        self._loaded = self._saving = self._offline = False
        self._problem = ""               # why it may only be viewed
        self._title = f"{posixpath.basename(path)} — {path}"
        self.setWindowTitle(self._title)
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
        self.status.setWordWrap(True)
        self.save_btn = QPushButton("Save"); self.save_btn.setProperty("role", "ok")
        self.save_btn.clicked.connect(lambda: self.save())
        self.copy_btn = QPushButton("Save a copy…"); self.copy_btn.setProperty("role", "ghost")
        self.copy_btn.setToolTip("Save the text as it is now to a file on this PC")
        self.copy_btn.clicked.connect(self.save_copy)
        self.reload_btn = QPushButton("Reload"); self.reload_btn.setProperty("role", "ghost")
        self.reload_btn.clicked.connect(self._reload)
        close_btn = QPushButton("Close"); close_btn.setProperty("role", "ghost")
        close_btn.clicked.connect(self.close)
        row.addWidget(self.status, 1); row.addWidget(self.save_btn)
        row.addWidget(self.copy_btn); row.addWidget(self.reload_btn)
        row.addWidget(close_btn)
        lay.addLayout(row)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=lambda: self.save())
        self.edit.document().modificationChanged.connect(lambda _m: self._buttons())
        self._buttons()
        self.load()

    # ---- state ----
    def has_unsaved_changes(self) -> bool:
        return self.edit.document().isModified()

    @property
    def offline(self) -> bool:
        return self._offline

    def _buttons(self):
        self.save_btn.setEnabled(self._loaded and not self._saving and not self._offline
                                 and not self._problem and self.has_unsaved_changes())
        self.copy_btn.setEnabled(self._loaded)
        self.reload_btn.setEnabled(not self._offline)

    def _describe(self, lines) -> str:
        endings = "CRLF" if self._newline == "\r\n" else "LF"
        text = f"{lines} lines · {self._encoding} · {endings}"
        if self._problem:
            text += f" · read-only: {self._problem}. Download it to edit it elsewhere."
        return text

    # ---- load / save ----
    def _reload(self):
        if self.has_unsaved_changes() and QMessageBox.question(
                self, "Reload",
                f"Throw away your changes to {posixpath.basename(self._path)} and "
                f"load it again from the remote?", QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        self.load()

    def load(self):
        if self._offline:
            return
        self.status.setText("Loading…")
        link, path = self._link, self._path
        run_job(self._jobs, lambda: link.run(lambda sftp: read_remote_text(sftp, path)),
                self._on_loaded, self._on_failed)

    def _on_loaded(self, loaded):
        self._newline, self._encoding = loaded.newline, loaded.encoding
        self._mode, self._stamp = loaded.mode, loaded.stamp
        self.edit.setPlainText(loaded.text)
        problem = loaded.problem
        if not problem and self.edit.toPlainText() != loaded.text:
            problem = _lossy(loaded.text)
        self._problem = problem
        self.edit.setReadOnly(bool(problem))
        self.edit.document().setModified(False)
        self._loaded = True
        self.status.setText(self._describe(len(loaded.text.splitlines())))
        self._buttons()

    def _on_failed(self, message):
        self.status.setText(f"⚠ {message}")

    def save(self, force=False):
        """Save over the remote file — asking first if it changed there since it
        was loaded (*force*: do not ask)."""
        if (not self._loaded or self._saving or self._offline or self._problem
                or not self.has_unsaved_changes()):
            return
        text = self.edit.toPlainText()
        try:
            data = text.replace("\n", self._newline).encode(self._encoding)
        except UnicodeEncodeError as exc:
            # "replace" used to turn it into "?" without a word
            self.status.setText(f"⚠ not saved: {text[exc.start:exc.end]!r} cannot be "
                                f"written in {self._encoding}, the file's encoding")
            return
        link, path, mode = self._link, self._path, self._mode
        stamp = None if force else self._stamp

        def job():
            try:
                return link.run(lambda sftp: write_remote_text(
                    sftp, path, data, stamp=stamp, mode=mode))
            except ChangedOnRemote as exc:
                return exc               # asked about on the GUI thread
        self._saving = True
        self.status.setText("Saving…")
        self._buttons()
        run_job(self._jobs, job, lambda result, saved=text: self._on_saved(result, saved),
                self._on_save_failed)

    def _on_saved(self, result, saved_text):
        self._saving = False
        if isinstance(result, ChangedOnRemote):
            self.status.setText(f"⚠ not saved: {result}")
            self._buttons()
            if QMessageBox.question(
                    self, "Changed on the remote",
                    f"{result}\n\nSave your version over it anyway? Choose No to keep "
                    f"editing (Save a copy… keeps your text in a local file; Reload "
                    f"shows what is there now).",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
                self.save(force=True)
            return
        size, self._stamp, how = result
        # typed more while the save was in flight? then it is still modified
        if self.edit.toPlainText() == saved_text:
            self.edit.document().setModified(False)
        self.status.setText(f"Saved {size} bytes to {self._path}"
                            + (f" — written {how}" if how else ""))
        self._buttons()

    def _on_save_failed(self, message):
        self._saving = False
        self.status.setText(f"⚠ could not save: {message}")
        self._buttons()

    def save_copy(self):
        """Save the text as it is now to a local file (what an offline editor
        is left with)."""
        if not self._loaded:
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "Save a copy", local_name(posixpath.basename(self._path)))
        if target:
            self.save_copy_to(target)

    def save_copy_to(self, target):
        text = self.edit.toPlainText().replace("\n", self._newline)
        try:
            data = text.encode(self._encoding)
        except UnicodeEncodeError:
            data = text.encode("utf-8")  # a copy loses nothing this way
        offline = self._offline
        run_job(self._jobs, lambda: _write_local(target, data),
                lambda _n: self._on_copied(target, offline),
                lambda msg: self.status.setText(f"⚠ could not save a copy: {msg}"))

    def _on_copied(self, target, offline):
        if offline:                      # nothing else can be saved: it is safe now
            self.edit.document().setModified(False)
        self.status.setText(f"A copy is saved in {target}")
        self._buttons()

    # ---- the browser's connection ----
    def rebind(self, link):
        """The browser reconnected: carry on over its new link. A load or save
        that was in flight on the old one is dropped (it cannot finish)."""
        was_saving = self._saving
        close_jobs(self._jobs)
        self._link, self._saving = link, False
        if not self._loaded:
            self.load()
        elif was_saving:
            self.status.setText("⚠ Reconnected while saving — save again to be sure "
                                "your changes are on the remote.")
        self._buttons()

    def connection_closed(self):
        """The browser is closing and the connection with it. Without unsaved
        changes the editor just closes; with them it stays open, offline, and
        offers to save a local copy — they are never thrown away unasked (they
        used to be, on tab close and on every reconnect)."""
        close_jobs(self._jobs)
        self._saving = False
        if not self.has_unsaved_changes():
            self.close()
            return
        self._offline = True
        self.setWindowTitle(self._title + " (offline)")
        self.status.setText("⚠ The connection is closed: your changes are NOT saved on "
                            "the remote. Save a copy… keeps them in a local file.")
        self._buttons()

    # ---- closing ----
    def closeEvent(self, event):  # noqa: N802 - Qt API
        if self.has_unsaved_changes():
            where = (" and can no longer be saved on the remote (the connection is "
                     "closed)" if self._offline else "")
            answer = QMessageBox.question(
                self, "Unsaved changes",
                f"{posixpath.basename(self._path)} has unsaved changes{where}. Close "
                f"without saving?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.discard()
        event.accept()

    def discard(self):
        """Stop quietly: nothing may call back here."""
        close_jobs(self._jobs)
        self.edit.document().setModified(False)
