"""Colored, capped log view with Clear / Save controls."""

from __future__ import annotations

import webbrowser

from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor
from PyQt5.QtWidgets import (QGroupBox, QVBoxLayout, QHBoxLayout, QPushButton,
                             QPlainTextEdit, QFileDialog)

from . import theme

DOCS_URL = "https://pypi.org/project/turbossh/"


class LogPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Log", parent)
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setMaximumBlockCount(50000)

        row = QHBoxLayout()
        clear = QPushButton("Clear"); clear.setProperty("role", "ghost")
        clear.clicked.connect(self.view.clear)
        save = QPushButton("Save log…"); save.setProperty("role", "ghost")
        save.clicked.connect(self._save)
        docs = QPushButton("Help / Docs"); docs.setProperty("role", "ghost")
        docs.clicked.connect(lambda: webbrowser.open(DOCS_URL))
        row.addWidget(clear); row.addWidget(save)
        row.addStretch(1); row.addWidget(docs)

        lay.addWidget(self.view, 1)
        lay.addLayout(row)

    def append(self, text: str):
        color = theme.LOG_COLORS["INFO"]
        for key, c in theme.LOG_COLORS.items():
            if key != "INFO" and (f"[{key}]" in text or text.startswith(key)):
                color = c
                break
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        for i, line in enumerate(text.splitlines() or [""]):
            if i:
                cur.insertText("\n")
            cur.insertText(line, fmt)
        cur.insertText("\n")
        self.view.setTextCursor(cur)
        self.view.ensureCursorVisible()

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "session.log")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.view.toPlainText())
            self.append(f"[OK] Log saved to {path}")
