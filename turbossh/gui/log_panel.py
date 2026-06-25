"""Colored, capped log view with levels (debug/info/success/warning/error), a
level filter, and Clear / Save controls."""

from __future__ import annotations

import webbrowser

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


class LogPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Log", parent)
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Cascadia Mono", 9))
        self.view.setMaximumBlockCount(50000)

        self._entries = []          # (rank, level, text) for re-rendering on filter
        self._min_rank = 0
        try:
            from . import settings as _s
            self._colors = theme.log_colors(_s.get("theme") or "dark")
        except Exception:
            self._colors = theme.LOG_COLORS

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
        self._entries.append((rank, level, text))
        if len(self._entries) > 50000:
            del self._entries[:10000]
        if rank >= self._min_rank:
            self._render(level, text)

    def set_theme(self, name: str):
        """Re-colour the log for a new app theme (light needs darker hues)."""
        self._colors = theme.log_colors(name)
        self._on_filter()                 # re-render all entries with new colours

    def _render(self, level: str, text: str):
        color = self._colors.get(level, self._colors["INFO"])
        fmt = QTextCharFormat(); fmt.setForeground(QColor(color))
        cur = self.view.textCursor(); cur.movePosition(QTextCursor.End)
        for i, line in enumerate(text.splitlines() or [""]):
            if i:
                cur.insertText("\n")
            cur.insertText(line, fmt)
        cur.insertText("\n")
        self.view.setTextCursor(cur)
        self.view.ensureCursorVisible()

    def _on_filter(self):
        self._min_rank = self.level_filter.currentData() or 0
        self.view.clear()
        for rank, level, text in self._entries:
            if rank >= self._min_rank:
                self._render(level, text)

    def clear(self):
        self._entries = []
        self.view.clear()

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "session.log")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.view.toPlainText())
            self.append(f"[OK] Log saved to {path}")
