"""A terminal on THIS machine, in a tab: PowerShell or Command Prompt on Windows,
your login shell elsewhere. The process and the line editing live in
:mod:`turbossh.gui.localshell` (no Qt); this is the tab widget around them."""

from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from . import qtutil
from .localshell import LineEditor, LocalShell, available_shells


class LocalTerminalWidget(QWidget):
    """A local shell in a tab. Same contract as the session widgets:
    ``close_session()`` shuts it down without blocking."""
    log = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from .terminal import ReaderThread          # noqa: F401 - used in _start
        from .vt100 import Vt100Terminal
        self.shell = None
        self.reader = None
        self.editor = None
        self._closed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 2); lay.setSpacing(2)
        row = QHBoxLayout(); row.setSpacing(4)
        self.status = QLabel("Starting…")
        self.status.setStyleSheet("font-size:8pt; color:#8a9aa5;")
        self.which = QComboBox()
        self.which.addItems(available_shells())
        self.which.setVisible(len(available_shells()) > 1)
        self.which.activated.connect(lambda _i: self.restart())
        restart = QPushButton("Restart"); restart.setProperty("role", "ghost")
        restart.setToolTip("Close this shell and start a fresh one")
        restart.clicked.connect(self.restart)
        ctrlc = QPushButton("Ctrl-C"); ctrlc.setProperty("role", "danger")
        ctrlc.setToolTip("Stop the running command")
        ctrlc.clicked.connect(self._interrupt)
        clear = QPushButton("Clear"); clear.setProperty("role", "ghost")
        clear.clicked.connect(lambda: (self.term.clear(), self.term.setFocus()))
        row.addWidget(self.status, 1); row.addWidget(self.which)
        row.addWidget(restart); row.addWidget(ctrlc); row.addWidget(clear)
        lay.addLayout(row)
        self.term = Vt100Terminal(send_fn=self._on_keys)
        self.term.resized.connect(self._on_resize)
        lay.addWidget(self.term, 1)
        self._start()

    def _start(self):
        from .terminal import ReaderThread
        shell = LocalShell(self.which.currentText() or None)
        try:
            shell.start()
        except Exception as exc:
            self.status.setText(f"Could not start {shell.label}: {exc}")
            self.term.feed(f"\r\n\x1b[31m[could not start {shell.label}: {exc}]"
                           f"\x1b[0m\r\n".encode("utf-8"))
            return
        self.shell = shell
        self.editor = None if shell.uses_pty else LineEditor(
            echo=self.term.feed,
            submit=lambda line, s=shell: s.write((line + "\r\n").encode("utf-8")),
            interrupt=shell.interrupt, shell_echoes=shell.echoes_input)
        self.reader = ReaderThread(shell.read)
        self.reader.closed.connect(self._on_exit)
        self.reader.start()
        self.term.set_source(self.reader.pull, self.reader.take_dropped)
        shell.resize(self.term.cols, self.term.rows)
        self.status.setText(f"{shell.label} — this PC"
                            + ("" if shell.uses_pty else "  ·  ↑/↓ history, Ctrl-C "
                               "stops the running command"))
        self.term.setFocus()

    def _on_keys(self, data: bytes):
        if self.shell is None:
            return
        if self.editor is None:
            self.shell.write(data)
        else:
            self.editor.feed(data)

    def _interrupt(self):
        if self.shell is not None:
            if self.editor is not None:
                self.editor.feed(b"\x03")
            else:
                self.shell.interrupt()
        self.term.setFocus()

    def _on_resize(self, cols, rows):
        if self.shell is not None:
            self.shell.resize(cols, rows)

    def _on_exit(self):
        if self._closed:
            return
        self.status.setText("The shell has exited — Restart for a new one")
        self.term.feed("\r\n\x1b[33m[shell exited — press Restart]\x1b[0m\r\n"
                       .encode("utf-8"))

    def _stop_shell(self):
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.stop()
            qtutil.park_thread(reader)
        shell, self.shell = self.shell, None
        if shell is not None:
            qtutil.run_teardown(shell.close, "local-shell-close")

    def restart(self):
        self._stop_shell()
        self.term.feed(b"\r\n")
        self._start()

    def showEvent(self, event):
        super().showEvent(event)
        self.term.setFocus()

    def close_session(self):
        if self._closed:
            return
        self._closed = True
        self._stop_shell()
        try:
            self.term.cleanup()
        except Exception:
            pass
