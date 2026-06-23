"""Settings dialog: theme, terminal font, default baud, first-run behaviour."""

from __future__ import annotations

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QComboBox,
                             QSpinBox, QFontComboBox, QCheckBox, QDialogButtonBox,
                             QGroupBox, QLabel, QLineEdit)
from PyQt5.QtGui import QFont

from . import settings as settings_mod
from . import theme


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TurboSSH — Settings")
        self.resize(440, 380)
        self.cfg = settings_mod.load()
        lay = QVBoxLayout(self)

        appear = QGroupBox("Appearance")
        af = QFormLayout(appear)
        self.theme = QComboBox(); self.theme.addItems(["dark", "light"])
        self.theme.setCurrentText(self.cfg.get("theme", "dark"))
        self.font = QFontComboBox()
        self.font.setCurrentFont(QFont(self.cfg.get("term_font", "Consolas")))
        self.font_size = QSpinBox(); self.font_size.setRange(7, 28)
        self.font_size.setValue(self.cfg.get("term_font_size", 10))
        self.scrollback = QSpinBox(); self.scrollback.setRange(2000, 50000)
        self.scrollback.setSingleStep(1000)
        self.scrollback.setValue(self.cfg.get("term_scrollback", 10000))
        self.scrollback.setToolTip("Lines kept in memory for wheel-scrolling "
                                   "(~16 KB/line, so higher = more RAM). The full "
                                   "session is always saved to disk, so 'Save all "
                                   "output' is unlimited regardless of this.")
        af.addRow("Theme", self.theme)
        af.addRow("Terminal font", self.font)
        af.addRow("Terminal font size", self.font_size)
        af.addRow("Terminal scrollback (lines)", self.scrollback)
        lay.addWidget(appear)

        defaults = QGroupBox("Defaults")
        df = QFormLayout(defaults)
        self.baud = QSpinBox(); self.baud.setRange(300, 4000000)
        self.baud.setValue(self.cfg.get("default_baud", 115200))
        df.addRow("Default serial baud", self.baud)
        lay.addWidget(defaults)

        # shared jump host (RDP machine) — enter once, reused by every session
        jump = QGroupBox("Jump host (RDP machine) — shared, entered once")
        jf = QFormLayout(jump)
        self.jhost = QLineEdit(self.cfg.get("jump_host", ""))
        self.juser = QLineEdit(self.cfg.get("jump_user", ""))
        self.jdomain = QLineEdit(self.cfg.get("jump_domain", ""))
        self.jpass = QLineEdit(settings_mod.jump_password())
        self.jpass.setEchoMode(QLineEdit.Password)
        theme.attach_eye(self.jpass)
        jf.addRow("Host", self.jhost)
        jf.addRow("User", self.juser)
        jf.addRow("Domain", self.jdomain)
        jf.addRow("Password", self.jpass)
        lay.addWidget(jump)

        startup = QGroupBox("Startup")
        sf = QVBoxLayout(startup)
        self.docs = QCheckBox("Open docs on first run")
        self.docs.setChecked(self.cfg.get("open_docs_first_run", True))
        self.shortcut = QCheckBox("Create a desktop shortcut on first run")
        self.shortcut.setChecked(self.cfg.get("make_shortcut_first_run", True))
        sf.addWidget(self.docs); sf.addWidget(self.shortcut)
        lay.addWidget(startup)

        lay.addWidget(QLabel("Theme & font apply immediately. Open tabs keep their "
                             "current font until reopened."))

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def result_settings(self) -> dict:
        settings_mod.set_jump_password(self.jpass.text())     # password -> OS vault
        return {
            "theme": self.theme.currentText(),
            "term_font": self.font.currentFont().family(),
            "term_font_size": self.font_size.value(),
            "term_scrollback": self.scrollback.value(),
            "default_baud": self.baud.value(),
            "open_docs_first_run": self.docs.isChecked(),
            "make_shortcut_first_run": self.shortcut.isChecked(),
            "jump_host": self.jhost.text().strip(),
            "jump_user": self.juser.text().strip(),
            "jump_domain": self.jdomain.text().strip(),
        }
