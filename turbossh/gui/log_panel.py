"""Colored, capped log view with levels (debug/info/success/warning/error), a
level filter, and Clear / Save controls."""

from __future__ import annotations

import time
import webbrowser

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor
from PyQt5.QtWidgets import (QGroupBox, QVBoxLayout, QHBoxLayout, QPushButton,
                             QPlainTextEdit, QFileDialog, QComboBox, QLabel)

from . import theme

DOCS_URL = "https://pypi.org/project/turbossh/"

# severity rank used by the level filter
_RANK = {"DEBUG": 0, "INFO": 1, "SUCCESS": 1, "OK": 1,
         "WARN": 2, "WARNING": 2, "stderr": 2, "ERROR": 3}
# the filter choices -> minimum rank shown
_FILTERS = [("All", 0), ("Info and up", 1), ("Warnings and up", 2), ("Errors only", 3)]
_TIME_COLOR = "#7a828c"         # the dim timestamp
_RECOLOR_MS = 150               # theme changes this close together re-draw once


class LogPanel(QGroupBox):
    appended = pyqtSignal(str)          # every line, for an embedding application

    def __init__(self, parent=None):
        super().__init__("Log", parent)
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Cascadia Mono", 9))
        self.view.setMaximumBlockCount(50000)
        self.view.setUndoRedoEnabled(False)  # a read-only log: no undo copy of every line

        self._entries = []          # (rank, level, text) for re-rendering on filter
        self._min_rank = 0
        try:
            self._colors = theme.log_colors(theme.current_name())
        except Exception:
            self._colors = theme.LOG_COLORS
        self._fmts = None           # char formats for _colors, made when first needed
        # A theme change re-draws the whole log; stepping through the Settings
        # theme list (or clicking the ribbon toggle repeatedly) does it once.
        self._recolor = QTimer(self)
        self._recolor.setSingleShot(True)
        self._recolor.setInterval(_RECOLOR_MS)
        self._recolor.timeout.connect(self._render_all)

        row = QHBoxLayout()
        row.addWidget(QLabel("Show:"))
        self.level_filter = QComboBox()
        for label, _rank in _FILTERS:
            self.level_filter.addItem(label, _rank)
        self.level_filter.setToolTip("Filter the log by severity.")
        self.level_filter.currentIndexChanged.connect(self._on_filter)
        row.addWidget(self.level_filter)
        row.addStretch(1)
        clear = QPushButton("Clear"); clear.setProperty("role", "ghost")
        clear.clicked.connect(self.clear)
        save = QPushButton("Save log…"); save.setProperty("role", "ghost")
        save.clicked.connect(self._save)
        docs = QPushButton("Help / Docs"); docs.setProperty("role", "ghost")
        docs.clicked.connect(lambda: webbrowser.open(DOCS_URL))
        row.addWidget(clear); row.addWidget(save); row.addWidget(docs)

        lay.addWidget(self.view, 1)
        lay.addLayout(row)

    # ---- levels ----
    @staticmethod
    def _detect(text: str):
        """Return (rank, level) for a message — from an explicit [LEVEL] tag or a
        leading LEVEL word, defaulting to INFO."""
        up = text.upper()
        for key in ("ERROR", "WARNING", "WARN", "SUCCESS", "OK", "DEBUG", "INFO"):
            if f"[{key}]" in up or up.lstrip().startswith(key):
                return _RANK.get(key, 1), key
        if "stderr" in text:
            return 2, "stderr"
        return 1, "INFO"

    def append(self, text: str):
        rank, level = self._detect(text)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._entries.append((rank, level, text, ts))
        if len(self._entries) > 50000:
            del self._entries[:10000]
        if rank >= self._min_rank:
            self._render(level, text, ts)
        self.appended.emit(text)

    def set_theme(self, name: str):
        """Re-colour the log for a new app theme (light needs darker hues). New
        lines take the colours at once; the lines already shown are re-drawn
        shortly after, once however many changes come in a row."""
        self._colors = theme.log_colors(name)
        self._fmts = None
        self._recolor.start()

    def _formats(self) -> dict:
        """One char format per level (and the dim timestamp under None)."""
        if self._fmts is None:
            self._fmts = {}
            for level, color in list(self._colors.items()) + [(None, _TIME_COLOR)]:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(color))
                self._fmts[level] = fmt
        return self._fmts

    @staticmethod
    def _insert(cur, formats, level: str, text: str, ts: str = ""):
        fmt = formats.get(level) or formats["INFO"]
        tfmt = formats[None]
        lines = text.splitlines() or [""]
        pad = " " * (len(ts) + 1) if ts else ""
        for i, line in enumerate(lines):
            if i:
                cur.insertText("\n")
                if pad:
                    cur.insertText(pad, tfmt)        # align continuation lines
            elif ts:
                cur.insertText(ts + " ", tfmt)       # timestamp prefix (first line)
            cur.insertText(line, fmt)
        cur.insertText("\n")

    def _render(self, level: str, text: str, ts: str = ""):
        cur = self.view.textCursor(); cur.movePosition(QTextCursor.End)
        self._insert(cur, self._formats(), level, text, ts)
        # follow new lines only if the user is at the bottom — don't yank the
        # view away from something they scrolled up to read
        bar = self.view.verticalScrollBar()
        if bar.value() >= bar.maximum() - 4:
            self.view.setTextCursor(cur)
            self.view.ensureCursorVisible()

    def _render_all(self):
        """Re-draw every entry the filter lets through, in one pass: one edit
        block, with the view not repainting until it is done. Line by line (a
        layout and a scroll each) a full log took 10 s, on every theme change."""
        self._recolor.stop()
        view, bar = self.view, self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        position = bar.value()
        formats = self._formats()
        view.setUpdatesEnabled(False)
        try:
            view.clear()
            cur = QTextCursor(view.document())
            cur.beginEditBlock()
            try:
                for rank, level, text, ts in self._entries:
                    if rank >= self._min_rank:
                        self._insert(cur, formats, level, text, ts)
            finally:
                cur.endEditBlock()
        finally:
            view.setUpdatesEnabled(True)
        if at_bottom:
            view.moveCursor(QTextCursor.End)
            view.ensureCursorVisible()
        else:                            # a re-colour keeps what the user was reading
            bar.setValue(min(position, bar.maximum()))

    def _on_filter(self):
        self._min_rank = self.level_filter.currentData() or 0
        self._render_all()

    def clear(self):
        self._entries = []
        self.view.clear()

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "session.log")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self.view.toPlainText())
            except OSError as exc:
                self.append(f"[ERROR] Could not save the log to {path}: {exc}")
                return
            self.append(f"[OK] Log saved to {path}")
