"""
Minimal PyQt5 demo for turbossh — fill in host/credentials, Connect, Run.

    pip install "turbossh[gui]"          # or: pip install PyQt5
    python examples/pyqt5_demo.py

Shows the intended GUI pattern: SSHWorker (safe mode) lives in a QThread, the
GUI never blocks or crashes on an SSH error, and every log line (secret-masked)
streams into the log pane.
"""

import sys
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QWidget, QGridLayout, QLabel,
                             QLineEdit, QPushButton, QTextEdit, QSpinBox)

from turbossh import SSHConfig
from turbossh.pyqt_worker import SSHWorker


class Demo(QWidget):
    request_connect = pyqtSignal()
    request_run = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("turbossh PyQt5 demo")
        self.resize(640, 480)
        self.worker = None
        self.thread = None

        g = QGridLayout(self)
        self.host = QLineEdit("127.0.0.1")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(22)
        self.user = QLineEdit("myuser")
        self.domain = QLineEdit(""); self.domain.setPlaceholderText("optional, e.g. CORP")
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.Password)
        self.command = QLineEdit("whoami")
        self.log = QTextEdit(); self.log.setReadOnly(True)

        self.btn_connect = QPushButton("Connect")
        self.btn_run = QPushButton("Run command"); self.btn_run.setEnabled(False)

        g.addWidget(QLabel("Host"), 0, 0);     g.addWidget(self.host, 0, 1)
        g.addWidget(QLabel("Port"), 0, 2);     g.addWidget(self.port, 0, 3)
        g.addWidget(QLabel("User"), 1, 0);     g.addWidget(self.user, 1, 1)
        g.addWidget(QLabel("Domain"), 1, 2);   g.addWidget(self.domain, 1, 3)
        g.addWidget(QLabel("Password"), 2, 0); g.addWidget(self.password, 2, 1, 1, 3)
        g.addWidget(self.btn_connect, 3, 0, 1, 4)
        g.addWidget(QLabel("Command"), 4, 0);  g.addWidget(self.command, 4, 1, 1, 2)
        g.addWidget(self.btn_run, 4, 3)
        g.addWidget(self.log, 5, 0, 1, 4)

        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_run.clicked.connect(lambda: self.request_run.emit(self.command.text()))

    def on_connect(self):
        cfg = SSHConfig(
            host=self.host.text().strip(),
            port=self.port.value(),
            username=self.user.text().strip(),
            domain=self.domain.text().strip() or None,
            password=self.password.text(),   # wrapped in a Secret internally
            connect_timeout=10, max_retries=2,
        )
        self.thread = QThread()
        self.worker = SSHWorker(cfg)
        self.worker.moveToThread(self.thread)

        self.worker.log.connect(self.log.append)
        self.worker.connected.connect(self.on_connected)
        self.worker.command_done.connect(self.on_command_done)
        self.worker.error.connect(lambda msg: self.log.append(f"<b>ERROR:</b> {msg}"))
        self.request_connect.connect(self.worker.connect_host)
        self.request_run.connect(self.worker.run_command)

        self.thread.start()
        self.btn_connect.setEnabled(False)
        self.log.append("Connecting…")
        self.request_connect.emit()

    def on_connected(self, ok: bool):
        self.log.append("Connected." if ok else "Connect failed.")
        self.btn_run.setEnabled(ok)
        self.btn_connect.setEnabled(not ok)

    def on_command_done(self, result):
        if hasattr(result, "stdout"):
            self.log.append(f"$ {result.command}  (exit {result.exit_code})")
            if result.stdout:
                self.log.append(result.stdout.rstrip())
            if result.stderr:
                self.log.append(f"<i>{result.stderr.rstrip()}</i>")
        else:
            self.log.append(f"Result: {result}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Demo(); w.show()
    sys.exit(app.exec_())
