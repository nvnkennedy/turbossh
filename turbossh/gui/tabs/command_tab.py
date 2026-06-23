"""Run a remote command; output goes to the shared log panel."""

from __future__ import annotations

from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLabel, QLineEdit, QPushButton


class CommandTab(QWidget):
    title = "Command"

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker = worker
        lay = QHBoxLayout(self)
        self.cmd = QLineEdit("uname -a")
        run = QPushButton("Run"); run.setProperty("role", "ok")
        run.clicked.connect(self._run)
        self.cmd.returnPressed.connect(self._run)
        lay.addWidget(QLabel("Command"))
        lay.addWidget(self.cmd, 1)
        lay.addWidget(run)

    def _run(self):
        self.worker.do_run(self.cmd.text().strip())
