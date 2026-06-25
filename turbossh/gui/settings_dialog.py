"""Settings dialog — categorised (sidebar + pages), like a proper preferences
window. The theme applies LIVE the moment you pick it. The wide 'Saved machines'
table lives in its own MachinesDialog opened from a button.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QComboBox, QSpinBox, QFontComboBox, QCheckBox,
                             QDialogButtonBox, QLabel, QLineEdit, QPushButton,
                             QTableWidget, QTableWidgetItem, QHeaderView, QWidget,
                             QListWidget, QListWidgetItem, QStackedWidget,
                             QApplication)
from PyQt5.QtGui import QFont

from . import settings as settings_mod
from . import theme


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
        self.cfg = settings_mod.load()
        self._machines = list(self.cfg.get("machines") or [])
        self._orig_theme = self.cfg.get("theme", "dark")
        self._win = parent

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
        self.theme = QComboBox(); self.theme.addItems(["dark", "light"])
        self.theme.setCurrentText(self.cfg.get("theme", "dark"))
        self.theme.currentTextChanged.connect(self._apply_theme_live)   # LIVE
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
                           "own colours, never replacing them. Applies to new output."))
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
        self.jhost = QLineEdit(self.cfg.get("jump_host", ""))
        self.juser = QLineEdit(self.cfg.get("jump_user", ""))
        self.jdomain = QLineEdit(self.cfg.get("jump_domain", ""))
        self.jpass = QLineEdit(settings_mod.jump_password())
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
    def _apply_theme_live(self, name):
        # persist immediately so the choice survives Cancel / closing the dialog
        cfg = settings_mod.load(); cfg["theme"] = name; settings_mod.save(cfg)
        if self._win is not None and hasattr(self._win, "apply_theme"):
            try:
                self._win.apply_theme(name)        # styles + log + icons everywhere
                return
            except Exception:
                pass
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
        settings_mod.set_jump_password(self.jpass.text())     # password -> OS vault
        return {
            "machines": self._machines,
            "theme": self.theme.currentText(),
            "term_font": self.font.currentFont().family(),
            "term_font_size": self.font_size.value(),
            "term_scrollback": self.scrollback.value(),
            "highlight_keywords": self.highlight.isChecked(),
            "default_baud": self.baud.value(),
            "open_docs_first_run": self.docs.isChecked(),
            "make_shortcut_first_run": self.shortcut.isChecked(),
            "jump_host": self.jhost.text().strip(),
            "jump_user": self.juser.text().strip(),
            "jump_domain": self.jdomain.text().strip(),
            "ffmpeg_path": self.ffmpeg_path.text().strip(),
        }
