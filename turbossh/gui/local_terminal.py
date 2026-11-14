"""A terminal on THIS machine, in a tab: PowerShell or Command Prompt on Windows,
your login shell elsewhere. The process and the line editing live in
:mod:`turbossh.gui.localshell` (no Qt); this is the tab widget around them."""

from __future__ import annotations

import threading

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from . import qtutil
from .localshell import LineEditor, LocalShell, available_shells


class LocalTerminalWidget(QWidget):
    """A local shell in a tab. Same contract as the session widgets:
    ``close_session()`` shuts it down without blocking.

    What is typed goes to the shell through a :class:`~.terminal.Writer`: a
    shell busy with a command does not read its input, and a paste bigger than
    the pipe / pty buffer (4-8 KB) used to block the window until it did —
    for good with ``ping -t``, where not even Ctrl-C could be pressed."""
    session_kind = "local"
    _HINT_MS = 6000                      # how long a hint stays in the status line
    log = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from .terminal import ReaderThread          # noqa: F401 - used in _start
        from .vt100 import Vt100Terminal
        self.shell = None
        self.reader = None
        self.editor = None
        self.writer = None
        self._closed = False
        self._log_gate = qtutil.SignalGate(self.log)  # the writer thread reports here
        self._hint_text = self._hint_before = ""
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._end_hint)

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
        clear.clicked.connect(self._clear)
        row.addWidget(self.status, 1); row.addWidget(self.which)
        row.addWidget(restart); row.addWidget(ctrlc); row.addWidget(clear)
        lay.addLayout(row)
        self.term = Vt100Terminal(send_fn=self._on_keys)
        self.term.resized.connect(self._on_resize)
        lay.addWidget(self.term, 1)
        self._start()

    def _start(self):
        from .terminal import ReaderThread, Writer
        shell = LocalShell(self.which.currentText() or None)
        try:
            shell.start()
        except Exception as exc:
            self.status.setText(f"Could not start {shell.label}: {exc}")
            self.term.notice(f"\r\n\x1b[31m[could not start {shell.label}: {exc}]"
                             f"\x1b[0m\r\n".encode("utf-8"))
            return
        self.shell = shell
        gate = self._log_gate
        writer = self.writer = Writer(
            shell.write, name="local-shell-writer", chunk=1024,
            on_error=lambda exc: gate.emit(f"[ERROR] local shell input: {exc}"))
        self.editor = None if shell.uses_pty else LineEditor(
            echo=self.term.feed,
            submit=lambda line, w=writer: w.send((line + "\r\n").encode("utf-8")),
            interrupt=self._stop_running, shell_echoes=shell.echoes_input)
        if getattr(shell, "console", False):
            # a new console starts by wiping the screen: what the previous shell
            # left there (Restart) goes into the scrollback first, as it did
            # over pipes, where the new shell's output came below it
            self.term.after_output(self.term.clear)
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
        if self.editor is not None:
            self.editor.feed(data)       # echo + line editing; Enter -> writer
        elif data == b"\x03":
            self._interrupt_pty()
        else:
            self.writer.send(data)

    def _interrupt_pty(self):
        """Ctrl-C on a pty: what is still queued is dropped, and Ctrl-C goes
        through the pty ahead of anything else, so the line discipline throws
        away the lines typed while a command ran, as any terminal does (see
        LocalShell.send_interrupt). When the pty is full — the writer is stuck
        behind a paste the program does not read, and the byte would wait
        there — the job is signalled at once as well (non-blocking calls)."""
        shell = self.shell
        signalled = not shell.write_ready() and shell.signal_interrupt()
        self.writer.interrupt(lambda: shell.send_interrupt(signalled))

    def _stop_running(self):
        """Ctrl-C on a shell over pipes: drop the queued input, then end what the
        shell started (finding the process tree is not free: off the GUI thread)."""
        shell = self.shell
        if shell is None:
            return
        if self.writer is not None:
            self.writer.interrupt(None)
        threading.Thread(target=shell.interrupt, name="local-shell-interrupt",
                         daemon=True).start()

    def _clear(self):
        """Clear: what is on screen goes into the scrollback.

        A Windows console keeps a screen of its own and draws on it with
        absolute positions: clearing only ours put what came next in the wrong
        places. So the console is cleared too, and nothing is typed into a
        command that may be reading its input (``set /p``, ``Read-Host``,
        ``pause``):

        * at PowerShell's prompt (the cursor's line starts with it): Ctrl+L,
          PSReadLine's Clear Screen, which keeps the line being typed — in
          order with what was typed before and after the click;
        * in cmd, whose console edits its line (also for ``set /p``): the
          console starts afresh from the cursor's line (see _restart_console);
        * otherwise — PowerShell at a prompt of another look (custom, two
          lines, the cursor at the start of a row), a command asking something,
          or printing away: nothing is touched. PSReadLine, which remembers
          where its line began, would draw it in the wrong place after the
          console's redraw, and Ctrl+L could become part of an answer. The
          status line says how to clear there."""
        shell, writer = self.shell, self.writer
        if shell is None or writer is None or not getattr(shell, "console", False):
            self.term.clear()
        else:
            head = self._cursor_line_head()
            if head.startswith("PS ") and "> " in head:
                self.term.clear()
                writer.send(b"\x0c")
            elif shell.label != "PowerShell":
                self.term.clear()
                self._restart_console(shell)
            else:
                self._hint("Clear: at a prompt PowerShell clears itself — press Ctrl+L "
                           "(or type cls)")
        self.term.setFocus()

    def _cursor_line_head(self) -> str:
        """The text on the cursor's line before the cursor."""
        screen = self.term.screen
        line = screen.row_text(len(screen.history.top) + screen.cursor.y)
        return line[:screen.cursor.x]

    def _hint(self, text: str):
        """*text* in the status line for a few seconds. (The timer is the
        tab's own, so it dies with the tab: a free one fired into a closed tab,
        and an error in a slot ends a PyQt application.)"""
        if self.status.text() != self._hint_text or not self._hint_timer.isActive():
            self._hint_before = self.status.text()
        self._hint_text = text
        self.status.setText(text)
        self._hint_timer.start(self._HINT_MS)

    def _end_hint(self):
        if self.status.text() == self._hint_text:   # (not replaced meanwhile)
            self.status.setText(self._hint_before)

    def _restart_console(self, shell):
        """Have the console redraw from the cursor's line: it redraws all of its
        screen when its size changes, and shrunk to one row and grown back, it
        shows that line on top with blank rows below — cleared, and it knows.
        Back to back, so hardly any output meets the one-row console (which
        keeps only its last line)."""
        shell.resize(self.term.cols, 1)
        shell.resize(self.term.cols, self.term.rows)

    def _interrupt(self):
        if self.shell is not None:
            if self.editor is not None:
                self.editor.feed(b"\x03")    # shows ^C, clears the line, _stop_running
            else:
                self._interrupt_pty()
        self.term.setFocus()

    def _on_resize(self, cols, rows):
        if self.shell is not None:
            self.shell.resize(cols, rows)

    def _on_exit(self):
        if self._closed:
            return
        self.status.setText("The shell has exited — Restart for a new one")
        self.term.notice("\r\n\x1b[33m[shell exited — press Restart]\x1b[0m\r\n"
                         .encode("utf-8"))

    def _stop_shell(self):
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.stop()
            qtutil.park_thread(reader)
        writer, self.writer = self.writer, None
        if writer is not None:
            writer.close()               # a write still blocked ends with the shell
        shell, self.shell = self.shell, None
        if shell is not None:
            qtutil.run_teardown(shell.close, "local-shell-close")

    def restart(self):
        self._stop_shell()
        self.term.notice(b"\r\n")
        self._start()

    def showEvent(self, event):
        super().showEvent(event)
        self.term.setFocus()

    def close_session(self):
        if self._closed:
            return
        self._closed = True
        self._log_gate.close()
        self._stop_shell()
        try:
            self.term.cleanup()
        except Exception:
            pass
