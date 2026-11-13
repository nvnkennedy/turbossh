"""Welcome / Home tab — a landing screen (MobaXterm-style) with quick actions and
one-click access to saved sessions. Shown at startup; reopen from File → Home."""

from __future__ import annotations

import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                             QPushButton, QListWidget, QListWidgetItem, QScrollArea)

from . import theme

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
        lay.setContentsMargins(18, 12, 18, 12); lay.setSpacing(10)
        scroll.setWidget(body); outer.addWidget(scroll)

        # --- header: logo + title (compact) ---
        head = QHBoxLayout()
        if os.path.exists(_ICON):
            logo = QLabel()
            logo.setPixmap(QPixmap(_ICON).scaled(40, 40, Qt.KeepAspectRatio,
                                                 Qt.SmoothTransformation))
            head.addWidget(logo)
        tbox = QVBoxLayout(); tbox.setSpacing(0)
        title = QLabel("TurboSSH")
        title.setStyleSheet(f"color:{theme.ACCENT}; font-size:16pt; font-weight:800;")
        sub = QLabel("SSH / Serial / SFTP toolkit for automotive & embedded work")
        sub.setStyleSheet("color:#9aa0a6; font-size:9pt;")
        tbox.addWidget(title); tbox.addWidget(sub)
        head.addLayout(tbox); head.addStretch(1)
        lay.addLayout(head)

        # --- quick-action cards ---
        self._grid = QGridLayout(); self._grid.setSpacing(10)
        win = self._win
        self._cards = [
            (key, self._card(emo, label, desc, slot)) for key, emo, label, desc, slot in (
                ("session", "🖥", "New session", "SSH or serial", win.new_session),
                ("camera", "📷", "Camera", "Local or RDP webcam", win.open_camera_panel),
                ("server", "🖧", "Set up SSH server", "Install OpenSSH", win.setup_ssh_server),
                ("settings", "⚙", "Settings", "Theme · machines · font", win.show_settings),
                ("updates", "⬆", "Check updates", "Latest from PyPI", win.check_updates),
                ("docs", "❓", "Documentation", "Guides & reference", win._open_docs),
            )]
        lay.addLayout(self._grid)

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
        text = f"{emo}  {label}\n{desc}" if desc else f"{emo}  {label}"
        # "&" marks a keyboard accelerator on a button: "Guides & reference"
        # used to show as "Guides  reference"
        b = QPushButton(text.replace("&", "&&"), self)
        b.setProperty("role", "ghost")
        b.setMinimumHeight(54)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet("text-align:left; padding:8px 12px;")
        b.clicked.connect(lambda _=False, s=slot: s())
        return b

    def _place_cards(self):
        """Lay out the cards, with no hole where a switched-off one would be."""
        hidden = self._win.hidden_home_cards()
        cell = 0
        for key, card in self._cards:
            self._grid.removeWidget(card)
            if key in hidden:
                card.hide()
                continue
            self._grid.addWidget(card, cell // 3, cell % 3)
            card.show()
            cell += 1

    def refresh(self):
        """Bring the cards and the saved-session list up to date."""
        self._place_cards()
        current = self.sessions.currentItem()
        keep = current.data(Qt.UserRole) if current is not None else None
        self.sessions.clear()            # rebuilt: keep the user's selection
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
            if keep is not None and s.get("name") == keep:
                self.sessions.setCurrentItem(it)

    def _open_item(self, item):
        name = item.data(Qt.UserRole)
        if name:
            self._win.open_session_named(name)
