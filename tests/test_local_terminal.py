"""The local shell (no Qt), its line editor, and the tab widget."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from turbossh.gui.localshell import LineEditor, LocalShell, available_shells


# ------------------------------------------------------------------ line editor
def make_editor():
    echoed, lines, interrupts = [], [], []
    editor = LineEditor(echo=echoed.append, submit=lines.append,
                        interrupt=lambda: interrupts.append(1))
    return editor, echoed, lines, interrupts


def test_line_editor_echoes_edits_and_submits():
    editor, echoed, lines, _ = make_editor()
    editor.feed(b"dirx")
    editor.feed(b"\x7f")                         # backspace
    editor.feed(b" /b\r")
    assert lines == ["dir /b"]
    assert b"".join(echoed) == b"dirx\b \b /b\r\n"
    assert editor.line == ""


def test_line_editor_history_with_the_arrow_keys():
    editor, echoed, lines, _ = make_editor()
    editor.feed(b"first\rsecond\r")
    echoed.clear()
    editor.feed(b"\x1b[A")                       # up: "second"
    assert editor.line == "second"
    editor.feed(b"\x1b[A")                       # up: "first"
    assert editor.line == "first"
    assert b"".join(echoed) == b"second" + b"\b \b" * 6 + b"first"
    editor.feed(b"\x1bOB\x1bOB")                 # down past the end: empty line
    assert editor.line == ""
    editor.feed(b"\x1b[A\r")
    assert lines[-1] == "second" and editor.history == ["first", "second"]


def test_line_editor_ctrl_c_ctrl_u_paste_and_split_sequences():
    editor, echoed, lines, interrupts = make_editor()
    editor.feed(b"half typed\x03")
    assert interrupts == [1] and editor.line == "" and lines == []
    editor.feed(b"junk\x15clean\r")
    assert lines == ["clean"]
    editor.feed(b"\x1b[200~one\rtwo\r\x1b[201~")  # a bracketed multi-line paste
    assert lines == ["clean", "one", "two"]
    editor.feed(b"caf\xc3")                       # UTF-8 split across two reads
    editor.feed(b"\xa9\x1b")                      # ...and an escape cut in half
    editor.feed(b"[A")
    assert editor.line == "two"                   # the arrow replaced "café"
    editor.feed(b"\x15ok\x1bx\r")                 # Alt+x is ignored
    assert lines[-1] == "ok"


def test_line_editor_for_a_shell_that_echoes_the_command_itself():
    echoed, lines = [], []
    editor = LineEditor(echo=echoed.append, submit=lines.append, shell_echoes=True)
    editor.feed(b"dir\r")
    assert lines == ["dir"]
    # the local copy is wiped rather than followed by a newline: PowerShell is
    # about to print "dir" itself
    assert b"".join(echoed) == b"dir" + b"\b \b" * 3


# ------------------------------------------------------------------ the process
class Collector:
    """Reads a LocalShell on a thread, as the GUI's ReaderThread does."""

    def __init__(self, shell):
        self.data = b""
        self.ended = False
        self._shell = shell
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            chunk = self._shell.read()
            if chunk is None:
                self.ended = True
                return
            self.data += chunk

    def wait_for(self, needle: bytes, timeout=30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.data:
                return True
            time.sleep(0.05)
        return needle in self.data


@pytest.mark.parametrize("label", available_shells())
def test_local_shell_runs_commands_and_stops_the_running_one(label, tmp_path):
    shell = LocalShell(label, cwd=str(tmp_path))
    shell.start()
    out = Collector(shell)
    newline = b"\n" if shell.uses_pty else b"\r\n"
    try:
        shell.write(b"echo hello-local" + newline)
        assert out.wait_for(b"hello-local"), out.data[-300:]

        # a command that would run for a minute; interrupt must end it and leave
        # the shell usable
        # (it prints 7*191 so the marker "1337" is the program's output, not the
        # command line a shell may echo back)
        sleeper = f'"{sys.executable}" -c "import time; print(7*191, flush=True); time.sleep(60)"'
        if label == "PowerShell":
            sleeper = "& " + sleeper
        shell.write(sleeper.encode() + newline)
        assert out.wait_for(b"1337"), out.data[-300:]
        shell.interrupt()
        shell.write(b"echo after-interrupt" + newline)
        assert out.wait_for(b"after-interrupt", 20), out.data[-300:]
        assert shell.alive
    finally:
        shell.close()
    deadline = time.monotonic() + 10
    while not out.ended and time.monotonic() < deadline:
        time.sleep(0.05)
    assert out.ended and not shell.alive


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree helper")
def test_descendants_excludes_the_console_host():
    import subprocess
    from turbossh.gui.localshell import _descendants
    parent = subprocess.Popen(
        ["cmd.exe", "/c", sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=0x08000000)
    try:
        time.sleep(1.5)
        kids = _descendants(parent.pid)
        assert kids, "the python child was not found"
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(parent.pid)],
                       capture_output=True)


# ------------------------------------------------------------------ the widget
def test_local_terminal_tab_types_runs_and_closes(qapp):
    pytest.importorskip("pyte")
    from PyQt5.QtTest import QTest
    from turbossh.gui import qtutil
    from turbossh.gui.local_terminal import LocalTerminalWidget

    def wait_until(predicate, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            QTest.qWait(30)
        return predicate()

    def screen():
        term = widget.term
        return "\n".join(term.screen.row_text(i) for i in range(term.screen.total_rows))

    widget = LocalTerminalWidget()
    widget.show()
    assert wait_until(lambda: widget.shell is not None and screen().strip())
    widget._on_keys(b"echo widget-says-hi\r")
    # once as the local echo of what was typed, once as the shell's answer
    assert wait_until(lambda: screen().count("widget-says-hi") >= 2), screen()[-400:]
    shell = widget.shell
    widget.close_session()
    widget.close_session()
    assert qtutil.wait_for_teardowns(15)
    assert not shell.alive
    widget.deleteLater()
    QTest.qWait(50)
