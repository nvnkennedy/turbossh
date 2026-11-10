"""
Minimal PyQt5 demo for turbossh — fill in host/credentials, Connect, Run.

    pip install "turbossh[gui]"          # or: pip install PyQt5
    python examples/pyqt5_demo.py

Shows the intended GUI pattern for using the library from your own widgets: a
safe-mode SSHHandler, every blocking call on a worker thread, so the GUI never
blocks or crashes on an SSH error. To embed the whole TurboSSH GUI instead, see
examples/embed_demo.py.
"""

import sys
from PyQt5.QtWidgets import (QApplication, QWidget, QGridLayout, QLabel,
                             QLineEdit, QPushButton, QTextEdit, QSpinBox)

from turbossh import SSHConfig, SSHHandler
from turbossh.gui.qtutil import close_jobs, run_job


class Demo(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("turbossh PyQt5 demo")
        self.resize(640, 480)
        self.ssh = None
        self.jobs = set()          # worker threads; close_jobs() parks them on exit

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
        self.btn_run.clicked.connect(self.on_run)

    def on_connect(self):
        cfg = SSHConfig(
            host=self.host.text().strip(),
            port=self.port.value(),
            username=self.user.text().strip(),
            domain=self.domain.text().strip() or None,
            password=self.password.text(),   # wrapped in a Secret internally
            connect_timeout=10, max_retries=2,
        )
        self.ssh = SSHHandler(cfg, safe=True, quiet=True)   # results, never exceptions
        self.btn_connect.setEnabled(False)
        self.log.append("Connecting…")
        run_job(self.jobs, self.ssh.connect,
                on_done=lambda result: self.on_connected(bool(result)),
                on_fail=self.on_error)

    def on_run(self):
        command = self.command.text()
        run_job(self.jobs, lambda: self.ssh.run(command),
                on_done=self.on_command_done, on_fail=self.on_error)

    def on_error(self, message: str):
        self.log.append(f"<b>ERROR:</b> {message}")

    def on_connected(self, ok: bool):
        self.log.append("Connected." if ok else "Connect failed.")
        self.btn_run.setEnabled(ok)
        self.btn_connect.setEnabled(not ok)

    def on_command_done(self, result):
        result = getattr(result, "value", result)      # safe mode wraps the result
        if hasattr(result, "stdout"):
            self.log.append(f"$ {result.command}  (exit {result.exit_code})")
            if result.stdout:
                self.log.append(result.stdout.rstrip())
            if result.stderr:
                self.log.append(f"<i>{result.stderr.rstrip()}</i>")
        else:
            self.log.append(f"Result: {result}")

    def closeEvent(self, event):
        close_jobs(self.jobs)
        if self.ssh is not None:
            self.ssh.disconnect()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Demo(); w.show()
    sys.exit(app.exec_())
