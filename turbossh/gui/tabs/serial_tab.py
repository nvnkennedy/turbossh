"""Serial monitor: device + baud + match + save, with Start/Stop and a port
picker that lists local COM ports."""

from __future__ import annotations

from PyQt5.QtWidgets import (QWidget, QGridLayout, QLabel, QLineEdit, QSpinBox,
                             QPushButton, QComboBox, QFileDialog)


class SerialTab(QWidget):
    title = "Serial"

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker = worker
        g = QGridLayout(self)

        self.device = QComboBox(); self.device.setEditable(True)
        refresh = QPushButton("↻"); refresh.setProperty("role", "ghost")
        refresh.setFixedWidth(40); refresh.clicked.connect(self.refresh_ports)
        self.baud = QSpinBox(); self.baud.setRange(300, 4000000); self.baud.setValue(115200)
        self.match = QLineEdit(); self.match.setPlaceholderText("regex to match (optional)")
        self.save = QLineEdit(); self.save.setPlaceholderText("save to file (optional)")
        browse = QPushButton("…"); browse.setProperty("role", "ghost")
        browse.setFixedWidth(40); browse.clicked.connect(self._browse)

        start = QPushButton("Start"); start.setProperty("role", "ok")
        stop = QPushButton("Stop"); stop.setProperty("role", "danger")
        start.clicked.connect(self._start)
        stop.clicked.connect(self.worker.stop_stream)

        g.addWidget(QLabel("Device"), 0, 0); g.addWidget(self.device, 0, 1)
        g.addWidget(refresh, 0, 2)
        g.addWidget(QLabel("Baud"), 0, 3); g.addWidget(self.baud, 0, 4)
        g.addWidget(QLabel("Match"), 1, 0); g.addWidget(self.match, 1, 1, 1, 4)
        g.addWidget(QLabel("Save"), 2, 0); g.addWidget(self.save, 2, 1, 1, 3)
        g.addWidget(browse, 2, 4)
        g.addWidget(start, 3, 1); g.addWidget(stop, 3, 2)
        self.refresh_ports()

    def refresh_ports(self):
        self.device.clear()
        try:
            from ...serial_handler import list_serial_ports
            ports = [p["device"] for p in list_serial_ports()]
        except Exception:
            ports = []
        self.device.addItems(ports or ["COM5", "/dev/ttyUSB0"])

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save serial log", "serial.log")
        if path:
            self.save.setText(path)

    def _start(self):
        self.worker.do_serial(self.device.currentText().strip(), self.baud.value(),
                             self.match.text().strip(), self.save.text().strip())
