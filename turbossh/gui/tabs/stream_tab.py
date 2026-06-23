"""Live SSH log streaming (slog2info -w, tail -f, journalctl -f) with match + save."""

from __future__ import annotations

from PyQt5.QtWidgets import (QWidget, QGridLayout, QLabel, QLineEdit, QPushButton,
                             QFileDialog)


class StreamTab(QWidget):
    title = "Log stream"

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker = worker
        g = QGridLayout(self)

        self.cmd = QLineEdit("slog2info -w")
        self.match = QLineEdit(); self.match.setPlaceholderText("regex to match (optional)")
        self.save = QLineEdit(); self.save.setPlaceholderText("save to file (optional)")
        browse = QPushButton("…"); browse.setProperty("role", "ghost")
        browse.setFixedWidth(40); browse.clicked.connect(self._browse)

        start = QPushButton("Start"); start.setProperty("role", "ok")
        stop = QPushButton("Stop"); stop.setProperty("role", "danger")
        start.clicked.connect(self._start)
        stop.clicked.connect(self.worker.stop_stream)

        g.addWidget(QLabel("Command"), 0, 0); g.addWidget(self.cmd, 0, 1, 1, 4)
        g.addWidget(QLabel("Match"), 1, 0); g.addWidget(self.match, 1, 1, 1, 4)
        g.addWidget(QLabel("Save"), 2, 0); g.addWidget(self.save, 2, 1, 1, 3)
        g.addWidget(browse, 2, 4)
        g.addWidget(start, 3, 1); g.addWidget(stop, 3, 2)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "stream.log")
        if path:
            self.save.setText(path)

    def _start(self):
        self.worker.do_stream(self.cmd.text().strip(), self.match.text().strip(),
                             self.save.text().strip())
