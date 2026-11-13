"""Settings dialog — categorised (sidebar + pages), like a proper preferences
window. The theme applies LIVE the moment you pick it. The wide 'Saved machines'
table lives in its own MachinesDialog opened from a button.
"""

from __future__ import annotations

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QComboBox, QSpinBox, QFontComboBox, QCheckBox,
                             QDialogButtonBox, QLabel, QLineEdit, QPushButton,
                             QTableWidget, QTableWidgetItem, QHeaderView, QWidget,
                             QListWidget, QListWidgetItem, QStackedWidget,
                             QApplication)
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QFont

from . import settings as settings_mod
from . import theme

_LIVE_THEME_MS = 200     # the theme list applies once it rests, not on every step


def _wrap(text: str) -> QLabel:
    lab = QLabel(text); lab.setWordWrap(True)
    lab.setStyleSheet("color:#8a8a8a;")
    return lab


class MachinesDialog(QDialog):
    """Edit the saved-machine list (its own dialog so Settings stays compact)."""

    def __init__(self, machines, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TurboSSH — Saved machines")
        self.resize(580, 380)
        lay = QVBoxLayout(self)
        lay.addWidget(_wrap("The RDP / Windows machines you use often. Only Host is "
                            "required; Name / User / Domain are optional and auto-fill "
                            "where they can. They appear as host drop-downs in the "
                            "SSH / Serial dialog and the Camera Remote source."))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Host / IP", "User", "Domain"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        for m in (machines or []):
            self._add_row(m)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("＋ Add"); add.setProperty("role", "ghost")
        rem = QPushButton("－ Remove selected"); rem.setProperty("role", "ghost")
        add.clicked.connect(lambda: self._add_row({}))
        rem.clicked.connect(self._remove_row)
        row.addWidget(add); row.addWidget(rem); row.addStretch(1)
        lay.addLayout(row)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _add_row(self, m):
        r = self.table.rowCount(); self.table.insertRow(r)
        for c, key in enumerate(("name", "host", "user", "domain")):
            self.table.setItem(r, c, QTableWidgetItem(str(m.get(key, "") or "")))

    def _remove_row(self):
        for r in sorted({i.row() for i in self.table.selectedItems()}, reverse=True):
            self.table.removeRow(r)

    def result_machines(self) -> list:
        out = []
        for r in range(self.table.rowCount()):
            def cell(c):
                it = self.table.item(r, c)
                return it.text().strip() if it is not None else ""
            host = cell(1)
            if host:
                out.append({"name": cell(0), "host": host,
                            "user": cell(2), "domain": cell(3)})
        return out


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TurboSSH — Settings")
        self.resize(660, 480)
        # as saved now: machines, jump host … another TurboSSH process saved since
        # this one read the file are shown (and kept on OK). This window's own
        # theme and Camera stay what it shows (settings.LIVE).
        self.cfg = settings_mod.refresh()
        # what the Jump host page shows: the file and the vault as they are now
        self._jump_before = settings_mod.shared_jump()
        self._machines = list(self.cfg.get("machines") or [])
        self._orig_theme = self.cfg.get("theme", "dark")
        self._win = parent
        # what the application embedding us has decided, or does not apply there
        self._embedded = getattr(parent, "embedded", False)
        self._theme_locked = getattr(parent, "theme_locked", False)
        self._camera_locked = getattr(parent, "camera_locked", False)

        outer = QVBoxLayout(self)
        body = QHBoxLayout(); outer.addLayout(body, 1)
        self.nav = QListWidget()
        self.nav.setFixedWidth(168)
        body.addWidget(self.nav)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)

        self._add_page("Appearance", self._page_appearance())
        self._add_page("Defaults", self._page_defaults())
        self._add_page("Jump host", self._page_jump())
        self._add_page("Saved machines", self._page_machines())
        self._add_page("Camera", self._page_camera())
        if not self._embedded:          # first-run shortcuts / docs are the app's own
            self._add_page("Startup", self._page_startup())
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _add_page(self, name, content):
        self.nav.addItem(QListWidgetItem(name))
        page = QWidget(); v = QVBoxLayout(page)
        v.setContentsMargins(16, 6, 8, 8)
        title = QLabel(name)
        title.setStyleSheet(f"color:{theme.ACCENT}; font-size:14pt; font-weight:700;")
        v.addWidget(title)
        v.addWidget(content)
        v.addStretch(1)
        self.pages.addWidget(page)

    # ---- pages ----
    def _page_appearance(self):
        w = QWidget(); f = QFormLayout(w)
        self.theme = QComboBox(); self.theme.addItems(theme.THEME_NAMES)
        self.theme.setCurrentText(theme.current_name())
        # LIVE, but arrowing through the list restyled the whole window and
        # re-drew the log once per step: apply when the choice rests
        self._theme_timer = QTimer(self)
        self._theme_timer.setSingleShot(True)
        self._theme_timer.setInterval(_LIVE_THEME_MS)
        self._theme_timer.timeout.connect(
            lambda: self._apply_theme_live(self.theme.currentText()))
        self.theme.currentTextChanged.connect(lambda _name: self._theme_timer.start())
        if self._theme_locked:
            self.theme.setEnabled(False)
            self.theme.setToolTip("Set by the application TurboSSH is embedded in.")
        self.font = QFontComboBox()
        self.font.setCurrentFont(QFont(self.cfg.get("term_font", "Cascadia Mono")))
        self.font_size = QSpinBox(); self.font_size.setRange(7, 28)
        self.font_size.setValue(self.cfg.get("term_font_size", 11))
        self.scrollback = QSpinBox(); self.scrollback.setRange(2000, 50000)
        self.scrollback.setSingleStep(1000)
        self.scrollback.setValue(self.cfg.get("term_scrollback", 10000))
        self.highlight = QCheckBox("Highlight keywords (error / warning / success)")
        self.highlight.setChecked(self.cfg.get("highlight_keywords", True))
        f.addRow("Theme", self.theme)
        f.addRow("Terminal font", self.font)
        f.addRow("Terminal font size", self.font_size)
        f.addRow("Terminal scrollback (lines)", self.scrollback)
        f.addRow("Keyword colouring", self.highlight)
        f.addRow("", _wrap("Tints plain words like 'error', 'warning' and "
                           "'success' in the terminal — on top of the server's "
                           "own colours, never replacing them. Font, scrollback and "
                           "colouring apply to sessions opened from now on "
                           "(Ctrl + mouse wheel zooms an open terminal)."))
        return w

    def _page_defaults(self):
        w = QWidget(); f = QFormLayout(w)
        self.baud = QSpinBox(); self.baud.setRange(300, 4000000)
        self.baud.setValue(self.cfg.get("default_baud", 115200))
        f.addRow("Default serial baud", self.baud)
        return w

    def _page_jump(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(_wrap("The RDP / Windows machine TurboSSH hops through. Entered "
                          "once here, reused by every session that uses 'via jump host'."))
        f = QFormLayout(); v.addLayout(f)
        self.jhost = QLineEdit(self._jump_before["host"])
        self.juser = QLineEdit(self._jump_before["user"])
        self.jdomain = QLineEdit(self._jump_before["domain"])
        self.jpass = QLineEdit(self._jump_before["password"])
        self.jpass.setEchoMode(QLineEdit.Password); theme.attach_eye(self.jpass)
        f.addRow("Host", self.jhost); f.addRow("User", self.juser)
        f.addRow("Domain", self.jdomain); f.addRow("Password", self.jpass)
        return w

    def _page_machines(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(_wrap("Saved RDP / Windows machines — host drop-downs in the "
                          "SSH / Serial dialog and the Camera Remote source."))
        self.manage_btn = QPushButton(); self.manage_btn.setProperty("role", "ghost")
        self.manage_btn.clicked.connect(self._manage_machines)
        self._update_manage_label()
        v.addWidget(self.manage_btn)
        return w

    def _page_camera(self):
        w = QWidget(); f = QFormLayout(w)
        self.camera_on = None
        if not self._camera_locked:
            self.camera_on = QCheckBox("Show the Camera in the ribbon, File menu and Home tab")
            shown = getattr(self._win, "camera_enabled", None)
            # what the window shows: OK must not close its Camera tabs unasked
            self.camera_on.setChecked(bool(shown() if callable(shown)
                                           else self.cfg.get("camera_enabled")))
            f.addRow(self.camera_on)
            f.addRow(_wrap("Off by default: nothing camera-related appears or runs "
                           "until you turn it on. View → Camera does the same. "
                           "Turning it off closes any Camera tabs."))
        self.ffmpeg_path = QLineEdit(self.cfg.get("ffmpeg_path", ""))
        self.ffmpeg_path.setPlaceholderText("optional: path to ffmpeg.exe")
        f.addRow("ffmpeg path", self.ffmpeg_path)
        return w

    def _page_startup(self):
        w = QWidget(); v = QVBoxLayout(w)
        self.docs = QCheckBox("Open docs on first run")
        self.docs.setChecked(self.cfg.get("open_docs_first_run", True))
        self.shortcut = QCheckBox("Create a desktop shortcut on first run")
        self.shortcut.setChecked(self.cfg.get("make_shortcut_first_run", True))
        v.addWidget(self.docs); v.addWidget(self.shortcut)
        return w

    # ---- live theme: applies AND persists the instant you pick it (so it sticks
    #      without OK, and Cancel doesn't undo it — theme is an immediate setting) ----
    def done(self, result):
        # a theme picked just before OK / Cancel still applies (and sticks)
        if self._theme_timer.isActive():
            self._theme_timer.stop()
            self._apply_theme_live(self.theme.currentText())
        super().done(result)

    def _apply_theme_live(self, name):
        # persist immediately so the choice survives Cancel / closing the dialog
        settings_mod.save({"theme": name})
        if self._win is not None and hasattr(self._win, "apply_theme"):
            try:
                self._win.apply_theme(name)        # styles + log + icons everywhere
                return
            except Exception:
                pass
        if not self._embedded:          # hosted, the application's look is the host's
            QApplication.instance().setStyleSheet(theme.stylesheet(name))

    # ---- machines ----
    def _update_manage_label(self):
        self.manage_btn.setText(f"Manage machines…   ({len(self._machines)} saved)")

    def _manage_machines(self):
        dlg = MachinesDialog(self._machines, self)
        if dlg.exec_() == QDialog.Accepted:
            self._machines = dlg.result_machines()
            self._update_manage_label()

    def result_settings(self) -> dict:
        new = {"host": self.jhost.text().strip(), "user": self.juser.text().strip(),
               "password": self.jpass.text()}
        old = settings_mod.shared_jump()         # another process may have saved one
        if (old["host"], old["user"], old["password"]) != \
                (new["host"], new["user"], new["password"]):
            # Sessions through the old jump host keep ITS password: a stale copy
            # of a changed one is dropped (2.0.0 stored one per session, and it
            # locked the account), and one that took the shared password keeps
            # it when Settings moves to another jump host. Sessions through any
            # other jump host are left alone.
            from .sessions import SessionStore
            SessionStore().shared_jump_changing(old, new)
        settings_mod.set_jump_password(new["password"])     # password -> OS vault
        result = {
            "machines": self._machines,
            "term_font": self.font.currentFont().family(),
            "term_font_size": self.font_size.value(),
            "term_scrollback": self.scrollback.value(),
            "highlight_keywords": self.highlight.isChecked(),
            "default_baud": self.baud.value(),
            "jump_host": self.jhost.text().strip(),
            "jump_user": self.juser.text().strip(),
            "jump_domain": self.jdomain.text().strip(),
            "ffmpeg_path": self.ffmpeg_path.text().strip(),
        }
        if not self._theme_locked:
            result["theme"] = self.theme.currentText()
        if self.camera_on is not None:
            result["camera_enabled"] = self.camera_on.isChecked()
        if not self._embedded:
            result["open_docs_first_run"] = self.docs.isChecked()
            result["make_shortcut_first_run"] = self.shortcut.isChecked()
        return result
