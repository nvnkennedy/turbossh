"""Connection panel: target + optional jump-host (RDP) fields, Connect/Disconnect."""

from __future__ import annotations

from PyQt5.QtWidgets import (QGroupBox, QGridLayout, QLabel, QLineEdit, QSpinBox,
                             QPushButton, QCheckBox)

from ..config import SSHConfig


class ConnectionPanel(QGroupBox):
    def __init__(self, worker, parent=None):
        super().__init__("Connection", parent)
        self.worker = worker
        g = QGridLayout(self)

        self.host = QLineEdit(); self.host.setPlaceholderText("target host / IP")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(22)
        self.user = QLineEdit(); self.user.setPlaceholderText("username")
        self.domain = QLineEdit(); self.domain.setPlaceholderText("domain (optional)")
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("password")
        self.ignore_hostkey = QCheckBox("Ignore host key (lab devices)")
        self.ignore_hostkey.setChecked(True)

        self.use_jump = QCheckBox("Via jump host (RDP machine)")
        self.use_jump.toggled.connect(self._toggle_jump)
        self.jhost = QLineEdit(); self.jhost.setPlaceholderText("jump host / IP")
        self.juser = QLineEdit(); self.juser.setPlaceholderText("jump user")
        self.jdomain = QLineEdit(); self.jdomain.setPlaceholderText("jump domain")
        self.jpass = QLineEdit(); self.jpass.setEchoMode(QLineEdit.Password)
        self.jpass.setPlaceholderText("jump password")

        self.btn_connect = QPushButton("Connect"); self.btn_connect.setProperty("role", "ok")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setProperty("role", "danger")
        self.btn_disconnect.setEnabled(False)
        self.btn_connect.clicked.connect(self._connect)
        self.btn_disconnect.clicked.connect(worker.do_disconnect)

        g.addWidget(QLabel("Host"), 0, 0); g.addWidget(self.host, 0, 1)
        g.addWidget(QLabel("Port"), 0, 2); g.addWidget(self.port, 0, 3)
        g.addWidget(QLabel("User"), 1, 0); g.addWidget(self.user, 1, 1)
        g.addWidget(QLabel("Domain"), 1, 2); g.addWidget(self.domain, 1, 3)
        g.addWidget(QLabel("Password"), 2, 0); g.addWidget(self.password, 2, 1)
        g.addWidget(self.ignore_hostkey, 2, 2, 1, 2)
        g.addWidget(self.use_jump, 3, 0, 1, 4)
        g.addWidget(self.jhost, 4, 0, 1, 2); g.addWidget(self.juser, 4, 2, 1, 2)
        g.addWidget(self.jdomain, 5, 0, 1, 2); g.addWidget(self.jpass, 5, 2, 1, 2)
        g.addWidget(self.btn_connect, 6, 0, 1, 2)
        g.addWidget(self.btn_disconnect, 6, 2, 1, 2)
        self._toggle_jump(False)

    def _toggle_jump(self, on):
        for w in (self.jhost, self.juser, self.jdomain, self.jpass):
            w.setVisible(on)

    def _policy(self):
        return "ignore" if self.ignore_hostkey.isChecked() else "auto"

    def build_config(self) -> SSHConfig:
        jump = None
        if self.use_jump.isChecked() and self.jhost.text().strip():
            jump = SSHConfig(host=self.jhost.text().strip(),
                             username=self.juser.text().strip() or None,
                             domain=self.jdomain.text().strip() or None,
                             password=self.jpass.text(), host_key_policy=self._policy())
        return SSHConfig(
            host=self.host.text().strip(), port=self.port.value(),
            username=self.user.text().strip() or None,
            domain=self.domain.text().strip() or None,
            password=self.password.text(), jump_host=jump,
            host_key_policy=self._policy(),
        )

    def _connect(self):
        if not self.host.text().strip():
            self.worker.log.emit("[ERROR] Enter a host first.")
            return
        self.worker.log.emit(f"Connecting to {self.host.text().strip()}…")
        self.worker.do_connect(self.build_config())

    def set_connected(self, connected: bool):
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
