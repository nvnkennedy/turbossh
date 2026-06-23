"""A dedicated live-log viewer tab (journalctl / syslog / dmesg / slog2info):
pick or type a follow command, filter by regex, pause, clear, save."""

from __future__ import annotations

import re
import threading

from PyQt5.QtCore import QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                             QLineEdit, QPushButton, QPlainTextEdit, QFileDialog)

from ..results import strip_ansi

_PRESETS = ["journalctl", "slog2info", "dmesg", "journalctl -f", "slog2info -w",
            "dmesg -w", "tail -f /var/log/messages"]


class _LogThread(QThread):
    """Buffers log lines; the GUI pulls them on a timer so a flood never freezes
    the UI thread."""
    stopped = pyqtSignal()

    def __init__(self, handler, command, stop_event, cap=200000):
        super().__init__()
        self.handler, self.command, self.stop_event = handler, command, stop_event
        self._lines = []
        self._lock = threading.Lock()
        self._cap = cap

    def run(self):
        try:
            # get_pty=True: tools like slog2info line-buffer on a TTY but
            # FULLY buffer on a pipe (so nothing shows until ~4 KB fills). A PTY
            # makes them stream line-by-line. Pagers are disabled in the command
            # (SYSTEMD_PAGER/PAGER=cat) so journalctl doesn't wait on `less`.
            for ln in self.handler.iter_lines(self.command, stop_event=self.stop_event,
                                              get_pty=True):
                with self._lock:
                    self._lines.append(strip_ansi(ln))
                    if len(self._lines) > self._cap:
                        del self._lines[:-self._cap]
        except Exception as exc:
            with self._lock:
                self._lines.append(f"[ERROR] {exc}")
        self.stopped.emit()

    def pull(self, maxn=3000):
        with self._lock:
            if not self._lines:
                return []
            out = self._lines[:maxn]
            del self._lines[:maxn]
            return out


class LogsTab(QWidget):
    title = "Logs"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.handler = None
        self.thread = None
        self.stop_event = threading.Event()
        self._paused = False

        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.cmd = QComboBox(); self.cmd.setEditable(True); self.cmd.addItems(_PRESETS)
        self.filter = QLineEdit(); self.filter.setPlaceholderText("filter (regex, optional)")
        self.start = QPushButton("Start"); self.start.setProperty("role", "ok")
        self.stop = QPushButton("Stop"); self.stop.setProperty("role", "danger")
        self.pause = QPushButton("Pause"); self.pause.setProperty("role", "ghost")
        self.clear = QPushButton("Clear"); self.clear.setProperty("role", "ghost")
        self.save = QPushButton("Save…"); self.save.setProperty("role", "ghost")
        self.start.clicked.connect(self._start)
        self.stop.clicked.connect(self._stop)
        self.pause.clicked.connect(self._toggle_pause)
        self.clear.clicked.connect(lambda: self.view.clear())
        self.save.clicked.connect(self._save)
        row.addWidget(QLabel("Cmd")); row.addWidget(self.cmd, 2)
        row.addWidget(self.filter, 2)
        row.addWidget(self.start); row.addWidget(self.stop)
        row.addWidget(self.pause); row.addWidget(self.clear); row.addWidget(self.save)

        self.statusrow = QLabel("○ stopped")
        self._count = 0

        self.view = QPlainTextEdit(); self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setMaximumBlockCount(500000)
        self.view.setStyleSheet("background:#000000; color:#cfe3f7;")

        lay.addLayout(row); lay.addWidget(self.statusrow); lay.addWidget(self.view, 1)

        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.view.clear)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._start)

        # drain buffered lines on a timer (bounded work -> never freezes)
        self._timer = QTimer(self); self._timer.timeout.connect(self._drain)
        self._timer.start(80)

    def set_handler(self, handler):
        self.handler = handler

    def _start(self):
        if not self.handler:
            self.statusrow.setText("○ not connected")
            return
        self._stop()
        self._count = 0
        self.stop_event = threading.Event()
        raw = self.cmd.currentText().strip()
        # The log runs over a NON-login exec channel, which (unlike the terminal's
        # login PTY) has a bare PATH — so QNX tools like slog2info show up as
        # "No such file or directory". Source the login profile to inherit the
        # real PATH, AND prepend the usual QNX/embedded bin dirs as a fallback.
        qnx_path = ("/ifs/bin:/ifs/usr/bin:/ifs/usr/sbin:/ifs/sbin:/proc/boot:"
                    "/system/bin:/system/xbin:/usr/bin:/usr/sbin:/bin:/sbin")
        # disable pagers (we run on a PTY now) so journalctl etc. stream instead
        # of piping into `less` and hanging.
        cmd = (f'. /etc/profile 2>/dev/null; . $HOME/.profile 2>/dev/null; '
               f'export PATH="$PATH:{qnx_path}"; '
               f'export SYSTEMD_PAGER=cat PAGER=cat GIT_PAGER=cat; {raw}')
        self.thread = _LogThread(self.handler, cmd, self.stop_event)
        self.thread.stopped.connect(lambda: self.statusrow.setText(
            f"○ stopped — {self._count} lines"))
        self.thread.start()
        self.statusrow.setText(f"● live — {raw}")

    def _stop(self):
        try:
            self.stop_event.set()
            if self.thread:
                self.thread.wait(800)
        except Exception:
            pass
        self.statusrow.setText(f"○ stopped — {self._count} lines")

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause.setText("Resume" if self._paused else "Pause")

    def _drain(self):
        if self._paused or not self.thread:
            return
        lines = self.thread.pull(3000)
        if not lines:
            return
        pat = self.filter.text().strip()
        if pat:
            try:
                rx = re.compile(pat)
                lines = [l for l in lines if rx.search(l)]
            except re.error:
                pass
        if lines:
            self._count += len(lines)
            self.view.appendPlainText("\n".join(lines))
            self.statusrow.setText(f"● live — {self._count} lines")

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "log.txt")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.view.toPlainText())

    def close_tab(self):
        self._stop()
