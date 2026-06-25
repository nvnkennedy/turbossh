"""Welcome / Home tab — a landing screen (MobaXterm-style) with quick actions and
one-click access to saved sessions. Shown at startup; reopen from File → Home."""

from __future__ import annotations

import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                             QPushButton, QListWidget, QListWidgetItem, QScrollArea)

from . import theme
from .. import __version__

_ICON = os.path.join(os.path.dirname(__file__), "..", "assets", "icon.png")


class HomeTab(QWidget):
    """A self-contained welcome screen. Holds a ref to the MainWindow so its
    cards/sessions can drive the real actions."""

    def __init__(self, window, store, parent=None):
        super().__init__(parent)
        self._win = window
        self._store = store

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(); lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 22); lay.setSpacing(16)
        scroll.setWidget(body); outer.addWidget(scroll)

        # --- header: logo + title + version ---
        head = QHBoxLayout()
        if os.path.exists(_ICON):
            logo = QLabel()
            logo.setPixmap(QPixmap(_ICON).scaled(64, 64, Qt.KeepAspectRatio,
                                                 Qt.SmoothTransformation))
            head.addWidget(logo)
        tbox = QVBoxLayout(); tbox.setSpacing(2)
        title = QLabel("TurboSSH")
        title.setStyleSheet(f"color:{theme.ACCENT}; font-size:26pt; font-weight:800;")
        sub = QLabel(f"v{__version__}   ·   SSH / Serial / SFTP toolkit for "
                     f"automotive & embedded work")
        sub.setStyleSheet("color:#9aa0a6; font-size:10pt;")
        tbox.addWidget(title); tbox.addWidget(sub)
        head.addLayout(tbox); head.addStretch(1)
        lay.addLayout(head)

        # --- quick-action cards ---
        grid = QGridLayout(); grid.setSpacing(10)
        cards = [
            ("🖥", "New session", "SSH or serial", self._win.new_session),
            ("📷", "Camera", "Local or RDP webcam", self._win.open_camera_panel),
            ("🖧", "Set up SSH server", "Install OpenSSH", self._win.setup_ssh_server),
            ("⚙", "Settings", "Theme · machines · font", self._win.show_settings),
            ("⬆", "Check updates", "Latest from PyPI", self._win.check_updates),
            ("❓", "Documentation", "Guides & reference", self._win._open_docs),
        ]
        for i, (emo, label, desc, slot) in enumerate(cards):
            grid.addWidget(self._card(emo, label, desc, slot), i // 3, i % 3)
        lay.addLayout(grid)

        # --- saved sessions (double-click to connect) ---
        sh = QLabel("Saved sessions")
        sh.setStyleSheet(f"color:{theme.ACCENT}; font-size:13pt; font-weight:700;")
        lay.addWidget(sh)
        hint = QLabel("Double-click to connect.  Manage them from the left sidebar.")
        hint.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(hint)
        self.sessions = QListWidget()
        self.sessions.itemDoubleClicked.connect(self._open_item)
        self.sessions.setMinimumHeight(150)
        lay.addWidget(self.sessions, 1)

        self.refresh()

    def _card(self, emo, label, desc, slot):
        b = QPushButton(f"{emo}  {label}\n{desc}" if desc else f"{emo}  {label}")
        b.setProperty("role", "ghost")
        b.setMinimumHeight(58)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet("text-align:left; padding:10px 14px;")
        b.clicked.connect(lambda _=False, s=slot: s())
        return b

    def refresh(self):
        """(Re)load the saved-session list — call when sessions change."""
        self.sessions.clear()
        for s in self._store.sessions:
            t = s.get("type")
            if t == "serial":
                default = "📡" if s.get("via_ssh") else "🔌"
                sub = "serial/ssh" if s.get("via_ssh") else "serial"
            else:
                default = "🖥"; sub = "ssh"
            icon = s.get("icon") or default
            host = s.get("host") or s.get("device") or ""
            it = QListWidgetItem(f"{icon}   {s.get('name')}    —  {host}   ·  {sub}")
            it.setData(Qt.UserRole, s.get("name"))
            self.sessions.addItem(it)

    def _open_item(self, item):
        name = item.data(Qt.UserRole)
        if name:
            self._win.open_session_named(name)
