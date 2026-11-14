"""The terminal's cursor and font, and the local shell's Windows console.

* The cursor was drawn where it had been after a move along its row (Left
  arrow, a shell putting it back after completing a word, CR) or a show /
  hide: none of those changes text, and only changed text was repainted.
* A proportional terminal font (the Settings font picker saved one when the
  configured font was not installed) drew text narrower than the cell grid:
  the cursor stood far to the right of the typing.
* The local terminal ran the Windows shell over pipes: no Tab completion, no
  line editing of the shell's own. It now runs in a pseudo console (ConPTY).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest


def _term(qapp):
    pytest.importorskip("pyte")
    from turbossh.gui.vt100 import Vt100Terminal
    term = Vt100Terminal(lambda data: None, cols=80, rows=24)
    term.resize(800, 400)
    return term


# ====================================================================== cursor
@pytest.mark.parametrize("label, data", [
    ("Left arrow in bash (BS)", b"\b"),
    ("two Left arrows", b"\b\b"),
    ("Right arrow (CSI C)", b"\x1b[C"),
    ("back to the start of the line (CR)", b"\r"),
    ("a column on the same row (CSI G)", b"\x1b[3G"),
    ("hidden while a shell redraws (DECTCEM off)", b"\x1b[?25l"),
])
def test_a_cursor_move_along_its_row_is_repainted(qapp, label, data):
    term = _term(qapp)
    try:
        term.feed(b"$ abcdef")
        repaints = []
        original = term.update
        term.update = lambda *a: (repaints.append(a), original(*a))[1]
        term.feed(data)
        assert repaints, f"{label}: the cursor moved but nothing was repainted"
        # its row is repainted (or the whole screen)
        rows = {a[0].top() // term.ch for a in repaints if a}
        assert any(not a for a in repaints) or term.screen.cursor.y in rows
    finally:
        term.cleanup()
        term.deleteLater()


def test_the_cursor_shows_again_after_a_redraw_hid_it(qapp):
    term = _term(qapp)
    try:
        term.feed(b"$ ls\x1b[?25l")
        repaints = []
        original = term.update
        term.update = lambda *a: (repaints.append(a), original(*a))[1]
        term.feed(b"\x1b[?25h")                  # PSReadLine / ConPTY: show it again
        assert repaints
    finally:
        term.cleanup()
        term.deleteLater()


# ====================================================================== blink
def test_the_cursor_blinks_while_focused_and_is_solid_while_typing(qapp, monkeypatch):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication
    term = _term(qapp)
    try:
        monkeypatch.setattr(QApplication, "cursorFlashTime", staticmethod(lambda: 1000))
        monkeypatch.setattr(term, "hasFocus", lambda: True)
        monkeypatch.setattr(term, "isVisible", lambda: True)
        term._restart_blink()
        assert term._blink.isActive() and term._blink.interval() == 500
        term._toggle_blink()
        assert term._blink_on is False           # off half of the blink
        QTest.keyClick(term, Qt.Key_A)           # typing shows it at once
        assert term._blink_on is True and term._blink.isActive()
        term._toggle_blink()
        term.feed(b"x")                          # so does output
        assert term._blink_on is True
        # no keyboard: no blinking (the hollow cursor stays)
        monkeypatch.setattr(term, "hasFocus", lambda: False)
        term._toggle_blink()
        assert not term._blink.isActive() and term._blink_on is True
        # a system set not to blink the caret
        monkeypatch.setattr(term, "hasFocus", lambda: True)
        monkeypatch.setattr(QApplication, "cursorFlashTime", staticmethod(lambda: 0))
        term._restart_blink()
        assert not term._blink.isActive() and term._blink_on is True
    finally:
        term.cleanup()
        term.deleteLater()


# ====================================================================== font
class _Metrics:
    """QFontMetrics for a font whose families map to per-character widths."""
    widths: dict = {}

    def __init__(self, font):
        fam = font.families()[0] if font.families() else font.family()
        self._w = self.widths.get(fam, self.widths["*"])

    def horizontalAdvance(self, text):
        return sum(self._w(c) for c in text)

    def height(self):
        return 17

    def ascent(self):
        return 13


def test_a_proportional_font_is_replaced_by_a_monospaced_one(qapp, monkeypatch):
    from turbossh.gui import vt100
    _Metrics.widths = {
        "Sitka Heading": lambda c: 5 if c in "il.:|1 " else 15,   # proportional
        "*": lambda c: 10,                                         # monospaced
    }
    monkeypatch.setattr(vt100, "QFontMetrics", _Metrics)
    font = vt100.terminal_font("Sitka Heading", 11)
    assert font.families()[0] == vt100._MONO_FALLBACK[0]
    assert font.pointSize() == 11
    # a monospaced one is kept, with the fallbacks behind it
    font = vt100.terminal_font("Consolas", 12)
    assert font.families()[0] == "Consolas" and "Cascadia Mono" in font.families()


def test_one_run_is_drawn_at_once_only_when_every_character_is_as_wide(qapp, monkeypatch):
    """The old check (64 "M"s against 64 times an "M") held for a proportional
    font too, and whole runs were drawn off the cell grid."""
    from turbossh.gui import vt100
    term = _term(qapp)
    try:
        _Metrics.widths = {"*": lambda c: 5 if c == "i" else 10}
        monkeypatch.setattr(vt100, "QFontMetrics", _Metrics)
        term._apply_font()
        assert term._batch is False              # one cell at a time: on the grid
        _Metrics.widths = {"*": lambda c: 10}
        term._apply_font()
        assert term._batch is True
    finally:
        term.cleanup()
        term.deleteLater()


def test_a_terminal_with_a_proportional_font_setting_keeps_text_on_the_grid(qapp):
    """With a real proportional font of this machine, if there is one."""
    from PyQt5.QtGui import QFont, QFontDatabase, QFontMetrics
    from turbossh.gui import settings
    from turbossh.gui import vt100
    proportional = None
    for family in QFontDatabase().families():
        fm = QFontMetrics(QFont(family, 11))
        if fm.horizontalAdvance("i") * 2 < fm.horizontalAdvance("M"):
            proportional = family
            break
    if proportional is None:
        pytest.skip("no proportional font here")
    old = settings.get("term_font")
    settings.save({"term_font": proportional})
    try:
        term = _term(qapp)
        try:
            fm = QFontMetrics(term._font)
            text = "PS C:\\Users\\me> ls -il"
            assert vt100._monospaced(fm) or term._batch is False
            if term._batch:
                assert fm.horizontalAdvance(text) == len(text) * term.cw
        finally:
            term.cleanup()
            term.deleteLater()
    finally:
        settings.save({"term_font": old})


def test_bright_colours_are_drawn_bright(qapp):
    """SGR 90-97 / 100-107 — PSReadLine's colours — came out in the default
    colour: pyte names them "bright…" (and one "bfight…")."""
    from turbossh.gui import vt100
    term = _term(qapp)
    try:
        assert term._color("brightyellow").name() == vt100._BRIGHT["yellow"].lower()
        assert term._color("brightred").name() == vt100._BRIGHT["red"].lower()
        assert term._color("bfightmagenta").name() == vt100._BRIGHT["magenta"].lower()
        assert term._color("ffaa00").name() == "#ffaa00"
        assert term._color("nonsense").name() == vt100._PALETTE["default"].lower()
    finally:
        term.cleanup()
        term.deleteLater()


# ====================================================================== settings
def test_the_font_picker_offers_monospaced_fonts_and_keeps_an_untouched_setting(qapp):
    """OK in Settings saved whatever the picker showed: for a font not installed
    here that was some other font (the first in the list), often a proportional
    one. The setting changes only when another font is picked."""
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import QFontComboBox
    from turbossh.gui import settings
    from turbossh.gui.settings_dialog import SettingsDialog
    old = settings.get("term_font")
    settings.save({"term_font": "No Such Font 123"})
    dlg = SettingsDialog(None)
    try:
        assert dlg.font.fontFilters() & QFontComboBox.MonospacedFonts
        assert dlg.result_settings()["term_font"] == "No Such Font 123"   # untouched
        shown = dlg.font.currentFont().family()
        others = [dlg.font.itemText(i) for i in range(dlg.font.count())
                  if dlg.font.itemText(i) != shown]
        if others:
            dlg.font.setCurrentFont(QFont(others[0]))
            picked = dlg.font.currentFont().family()
            if picked != shown:
                assert dlg.result_settings()["term_font"] == picked
    finally:
        dlg.deleteLater()
        settings.save({"term_font": old})


# ====================================================================== console
needs_console = pytest.mark.skipif(
    os.name != "nt", reason="the Windows pseudo console")


def _console_or_skip():
    from turbossh.gui import conpty
    if not conpty.available():
        pytest.skip("this Windows has no pseudo console")
    return conpty


class _Out:
    def __init__(self, reader):
        self.data = bytearray()
        self.ended = threading.Event()
        threading.Thread(target=self._run, args=(reader,), daemon=True).start()

    def _run(self, reader):
        while True:
            chunk = reader()
            if chunk is None:
                self.ended.set()
                return
            self.data.extend(chunk)

    def wait_for(self, needle: bytes, timeout=30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.data:
                return True
            time.sleep(0.05)
        return needle in self.data


@needs_console
@pytest.mark.parametrize("label, typed, completed", [
    ("PowerShell", b"Get-Chi\t", b"Get-ChildItem"),
    ("Command Prompt", b"dir C:\\Win\t", b"C:\\Windows"),
])
def test_the_windows_local_shell_completes_on_tab(label, typed, completed, tmp_path):
    """Over pipes Tab put four spaces in; in the console the shell completes."""
    _console_or_skip()
    from turbossh.gui.localshell import LocalShell
    shell = LocalShell(label, cwd=str(tmp_path))
    shell.start()
    try:
        assert shell.console and shell.uses_pty
        out = _Out(shell.read)
        assert out.wait_for(b">"), bytes(out.data[-300:])      # the prompt
        shell.write(typed)
        assert out.wait_for(completed, 20), bytes(out.data[-400:])
    finally:
        shell.close()


@needs_console
def test_the_console_ends_when_the_shell_exits_and_close_does_not_wait(tmp_path):
    conpty = _console_or_skip()
    console = conpty.PseudoConsole(["cmd.exe", "/c", "echo bye"], cwd=str(tmp_path))
    out = _Out(console.read)
    assert out.ended.wait(20), "the end of the program never reached the reader"
    assert b"bye" in out.data
    assert console.poll() == 0
    console.close()
    console.close()                              # twice is fine
    # nobody reading its output: closing must not wait for it to be read
    console = conpty.PseudoConsole(
        ["cmd.exe", "/c", "for /L %i in (1,1,3000) do @echo line %i"], cwd=str(tmp_path))
    time.sleep(1.0)
    t0 = time.monotonic()
    console.close()
    assert time.monotonic() - t0 < 10
    with pytest.raises(OSError):
        console.write(b"x")
    assert console.wait(10) is not None


@needs_console
def test_ctrl_c_reaches_the_shells_program_when_turbossh_was_started_ignoring_it(tmp_path):
    """The updater relaunches TurboSSH in a new process group (and a host may
    start it so), which makes it ignore Ctrl+C — inherited by the shell and what
    it runs, so Ctrl+C in the local terminal stopped nothing. Without a console
    of its own TurboSSH clears that before starting the shell."""
    _console_or_skip()
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pytest.skip("no pythonw.exe to start without a console")
    report = tmp_path / "report.txt"
    script = tmp_path / "probe.py"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {root!r})\n"
        "from turbossh.gui.conpty import PseudoConsole\n"
        f"pc = PseudoConsole([{sys.executable!r}, '-c', "
        "\"import time; print('ready', flush=True); time.sleep(30)\"])\n"
        "buf = b''\n"
        "t0 = time.monotonic()\n"
        "while b'ready' not in buf and time.monotonic() - t0 < 20:\n"
        "    buf += pc.read() or b''\n"
        "pc.write(b'\\x03')\n"
        "try:\n"
        "    pc.wait(10); result = 'stopped'\n"
        "except Exception:\n"
        "    result = 'not stopped'\n"
        "pc.close()\n"
        f"open({str(report)!r}, 'w').write(result)\n")
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: as the updater starts it
    proc = subprocess.Popen([pythonw, str(script)], creationflags=0x00000008 | 0x00000200)
    proc.wait(90)
    assert report.read_text() == "stopped"


def test_ctrl_c_is_let_through_only_where_nobody_else_can_send_it():
    """No console, or a hidden one nobody else is attached to (the embed
    client starts TurboSSH so): cleared. A console shared with a terminal:
    kept. (GetConsoleWindow, used at first, is empty for any hidden console,
    shared or not.)"""
    from turbossh.gui import conpty

    class Kernel:
        def __init__(self, cp, attached):
            self.cp, self.attached, self.calls = cp, attached, []

        def GetConsoleCP(self):
            return self.cp

        def GetConsoleWindow(self):
            return None                          # hidden console: no window

        def GetConsoleProcessList(self, pids, size):
            return self.attached

        def SetConsoleCtrlHandler(self, handler, add):
            self.calls.append((handler, add))
            return 1

    for cp, attached, cleared in ((0, 0, True), (850, 1, True), (850, 3, False)):
        kernel = Kernel(cp, attached)
        conpty._let_ctrl_c_through(kernel)
        assert (kernel.calls == [(None, False)]) is cleared, (cp, attached)


@needs_console
def test_the_local_terminal_tab_runs_the_shell_in_a_console(qapp, console_ctrl_c):
    """The tab end to end: Tab completes, Ctrl+C (the key) stops a running
    command, the cursor is where the shell put it, exit is noticed."""
    _console_or_skip()
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    from turbossh.gui import qtutil
    from turbossh.gui.local_terminal import LocalTerminalWidget

    def screen():
        s = widget.term.screen
        return [s.row_text(i).rstrip() for i in range(s.total_rows)]

    def wait_until(predicate, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            QTest.qWait(30)
        return predicate()

    widget = LocalTerminalWidget()
    widget.show()
    try:
        assert wait_until(lambda: widget.shell is not None and any(screen()))
        assert widget.shell.console and widget.editor is None
        term = widget.term
        if widget.shell.label == "PowerShell":
            QTest.keyClicks(term, "Get-Chi")
            QTest.keyClick(term, Qt.Key_Tab)
            assert wait_until(lambda: any("Get-ChildItem" in r for r in screen()), 20)
            QTest.keyClick(term, Qt.Key_Escape)       # PSReadLine: clear the line
        else:
            QTest.keyClicks(term, "dir C:\\Win")
            QTest.keyClick(term, Qt.Key_Tab)
            assert wait_until(lambda: any("C:\\Windows" in r for r in screen()), 20)
            QTest.keyClick(term, Qt.Key_Escape)       # cmd: clear the line
        QTest.qWait(500)
        cur = term.screen.cursor
        row = term.screen.row_text(len(term.screen.history.top) + cur.y)
        # the cursor is right after the prompt the shell left
        assert row[:cur.x].rstrip().endswith(">") and not row[cur.x:].strip(), (row, cur.x)
        sleeper = f'"{sys.executable}" -c "import time; print(7*191, flush=True); time.sleep(60)"'
        if widget.shell.label == "PowerShell":
            sleeper = "& " + sleeper
        widget._on_keys(sleeper.encode() + b"\r")
        assert wait_until(lambda: any("1337" in r for r in screen()), 30)
        QTest.keyClick(term, Qt.Key_C, Qt.ControlModifier)
        assert wait_until(lambda: any("KeyboardInterrupt" in r for r in screen()), 20)
        QTest.qWait(700)
        widget._on_keys(b"exit\r")
        assert wait_until(lambda: any("shell exited" in r for r in screen()), 20)
    finally:
        shell = widget.shell
        widget.close_session()
        assert qtutil.wait_for_teardowns(15)
        assert shell is None or not shell.alive
        widget.deleteLater()


def test_the_pipe_fallback_stays_when_asked_for(monkeypatch):
    """TURBOSSH_NO_CONPTY (or no pseudo console): the 2.0 shell over pipes."""
    from turbossh.gui.localshell import LocalShell
    monkeypatch.setenv("TURBOSSH_NO_CONPTY", "1")
    shell = LocalShell()
    assert shell._want_console is False
    monkeypatch.delenv("TURBOSSH_NO_CONPTY")
    assert LocalShell()._want_console == (os.name == "nt")
    assert LocalShell(console=False)._want_console is False


# ====================================================================== review
def test_after_output_runs_in_order_with_the_output(qapp):
    term = _term(qapp)
    try:
        backlog = [b"old-output\r\n"]
        term.set_source(lambda n: backlog.pop() if backlog else b"")
        seen = []
        term.notice(b"[notice]\r\n")
        term.after_output(lambda: seen.append(term.screen.row_text(1).strip()))
        term._tick()
        assert seen == ["[notice]"]              # after the output and the notice
        term.set_source(None)
        term.after_output(lambda: seen.append("now"))   # nothing pending: at once
        assert seen[-1] == "now"
    finally:
        term.cleanup()
        term.deleteLater()


@needs_console
def test_the_shell_pid_is_held_until_close(tmp_path):
    """The process handle was closed as soon as the shell ended: Windows could
    hand its PID to another program, and close() then looked for that PID's
    children — and ended a stranger's process tree."""
    conpty = _console_or_skip()
    console = conpty.PseudoConsole(["cmd.exe", "/c", "exit 3"], cwd=str(tmp_path))
    assert console.wait(20) == 3
    time.sleep(0.2)
    assert console._hproc is not None            # ended, not closed: still held
    console.close()
    assert console._hproc is None
    # close() first, then the end: the exit thread lets go of it
    console = conpty.PseudoConsole(["cmd.exe"], cwd=str(tmp_path))
    console.close()
    assert console.wait(20) is not None
    deadline = time.monotonic() + 5
    while console._hproc is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert console._hproc is None


@needs_console
def test_a_process_older_than_its_parent_pid_is_not_its_child(monkeypatch):
    """A process whose parent ended long ago still names that PID; once Windows
    gives the PID to the shell, it looked like the shell's child — and was
    ended with it."""
    from turbossh.gui import localshell
    parent = subprocess.Popen(["cmd.exe", "/c", sys.executable, "-c",
                               "import time; time.sleep(30)"], creationflags=0x08000000)
    try:
        time.sleep(1.5)
        assert localshell._descendants(parent.pid)           # a real child
        real = localshell._start_time
        monkeypatch.setattr(localshell, "_start_time",
                            lambda pid: 0 if pid != parent.pid else real(pid))
        assert localshell._descendants(parent.pid) == []     # "older": not ours
        # 30 s "older": a stranger — unless the clock was set back that much
        # since the shell started (the leeway is what was measured, no more)
        parent_born = real(parent.pid)
        monkeypatch.setattr(localshell, "_start_time",
                            lambda pid: parent_born - 30 * 10_000_000
                            if pid != parent.pid else parent_born)
        assert localshell._descendants(parent.pid) == []
        assert localshell._descendants(parent.pid, clock_back=10) == []
        assert localshell._descendants(parent.pid, clock_back=40)
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(parent.pid)],
                       capture_output=True)


def test_a_hint_showing_when_the_tab_closes_does_not_end_the_app(qapp, monkeypatch):
    """The status hint first ran on a free single-shot timer: it fired into the
    closed tab, and an error in a slot aborts a PyQt application. The timer is
    the tab's own now; the hint also leaves a status that changed alone."""
    pytest.importorskip("pyte")
    from PyQt5.QtTest import QTest
    from turbossh.gui import local_terminal, qtutil
    monkeypatch.setattr(local_terminal.LocalTerminalWidget, "_HINT_MS", 200)

    class Shell:
        label, uses_pty, echoes_input, console, alive = "PowerShell", True, False, True, True

        def start(self):
            pass

        def read(self, size=65536):
            time.sleep(0.05)
            return b""

        def write(self, data):
            pass

        def resize(self, cols, rows):
            pass

        def close(self):
            pass

    monkeypatch.setattr(local_terminal, "LocalShell", lambda label=None, cwd=None: Shell())
    widget = local_terminal.LocalTerminalWidget()
    try:
        before = widget.status.text()
        widget._hint("hint one")
        assert widget.status.text() == "hint one"
        QTest.qWait(400)
        assert widget.status.text() == before            # back when it ends
        widget._hint("hint two")
        widget.status.setText("The shell has exited")     # replaced meanwhile
        QTest.qWait(400)
        assert widget.status.text() == "The shell has exited"
        widget._hint("hint three")
    finally:
        widget.close_session()
        widget.deleteLater()
    QTest.qWait(600)                                     # past the hint's end: no crash
    qtutil.wait_for_teardowns(5)


def test_the_clock_set_back_since_the_shell_started_is_measured(monkeypatch):
    from turbossh.gui import localshell
    shell = localshell.LocalShell(console=False)
    assert shell._clock_back() == 0.0                    # not started
    shell._started_at = (100.0, 5000.0)
    monkeypatch.setattr(localshell.time, "monotonic", lambda: 160.0)
    monkeypatch.setattr(localshell.time, "time", lambda: 5060.0)
    assert shell._clock_back() == 0.0                    # in step
    monkeypatch.setattr(localshell.time, "time", lambda: 5015.0)
    assert shell._clock_back() == 45.0                   # set back 45 s
    monkeypatch.setattr(localshell.time, "time", lambda: 5300.0)
    assert shell._clock_back() == 0.0                    # set forward: no leeway


@needs_console
def test_a_write_while_the_console_closes_is_not_an_error(tmp_path):
    _console_or_skip()
    from turbossh.gui.localshell import LocalShell
    shell = LocalShell("Command Prompt", cwd=str(tmp_path))
    shell.start()
    console = shell._console
    try:
        shell._console = None                    # close() half done: _proc left
        shell.write(b"echo x\r")                 # no AttributeError
    finally:
        shell._console = console
        shell.close()
    assert shell._proc is None and shell._console is None


@needs_console
@pytest.mark.parametrize("label", ["PowerShell", "Command Prompt"])
def test_clear_and_restart_keep_the_console_in_step(qapp, label, console_ctrl_c):
    """Clear blanked only the widget: the console, which draws with absolute
    positions, put the next lines in the wrong places. Now the console clears
    too — and nothing is typed into a command that reads its input (an
    earlier cut sent Esc + cls / Ctrl+L, which became the answer to `set /p` /
    `Read-Host`). Restart kept nothing of the previous screen (the new console
    wipes it)."""
    _console_or_skip()
    from PyQt5.QtTest import QTest
    from turbossh.gui import qtutil
    from turbossh.gui.local_terminal import LocalTerminalWidget

    def rows():
        s = widget.term.screen
        return [s.row_text(len(s.history.top) + y).rstrip() for y in range(s.lines)]

    def history():
        s = widget.term.screen
        return [s.row_text(i).rstrip() for i in range(len(s.history.top))]

    def wait_until(predicate, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            QTest.qWait(30)
        return predicate()

    logs = []
    widget = LocalTerminalWidget()
    widget.log.connect(logs.append)
    try:
        if widget.which.currentText() != label:
            widget.which.setCurrentIndex(widget.which.findText(label))
            widget.restart()
        widget.resize(1200, 500)
        widget.show()
        assert wait_until(lambda: widget.shell is not None and widget.shell.console
                          and any(r.endswith(">") for r in rows()))
        widget._on_keys(b"echo first-line\r")
        assert wait_until(lambda: "first-line" in rows())
        QTest.qWait(500)
        widget._clear()
        # only the prompt is left (it may wrap, and cmd puts it on row 2)
        def only_the_prompt():
            shown = [r for r in rows() if r]
            return (bool(shown) and shown[-1].endswith(">")
                    and not any("first-line" in r for r in shown))
        assert wait_until(only_the_prompt, 15), rows()
        assert "first-line" in history()                      # kept, not wiped
        QTest.qWait(500)                                      # a person's pause
        widget._on_keys(b"echo after-clear\r")

        def answer_below_command():
            shown = [r for r in rows() if r]
            return any(shown[i + 1] == "after-clear" and shown[i].endswith("after-clear")
                       for i in range(len(shown) - 1))
        assert wait_until(answer_below_command, 15), rows()
        # a command of the shell's own reading its input: Clear types nothing
        if label == "PowerShell":
            ask, show = b"$x = Read-Host 'name'\r", b"\"[$x]\"\r"
        else:
            ask, show = b"set /p x=name: \r", b"echo [%x%]\r"
        widget._on_keys(ask)
        assert wait_until(lambda: any("name:" in r for r in rows()), 15), rows()
        QTest.qWait(500)
        widget._clear()
        QTest.qWait(1500)
        shown = [r for r in rows() if r]
        if label == "PowerShell":
            # PSReadLine or a question? It cannot tell: nothing is touched
            assert "after-clear" in shown, rows()
            assert "Ctrl+L" in widget.status.text()
        else:
            assert shown and shown[0].startswith("name:"), rows()   # cleared, in step
        widget._on_keys(b"abc\r")
        QTest.qWait(1000)
        widget._on_keys(show)
        assert wait_until(lambda: "[abc]" in rows(), 15), rows()  # nothing slipped in
        if label == "PowerShell":
            # PSReadLine at a prompt of its own look: the same — and the line
            # being typed is drawn where it belongs afterwards
            widget._on_keys(b"function prompt { 'MY> ' }\r")
            assert wait_until(lambda: any(r == "MY>" for r in rows()), 15), rows()
            widget._on_keys(b"echo zz")
            QTest.qWait(500)
            widget._clear()
            QTest.qWait(1000)
            widget._on_keys(b"z\r")
            assert wait_until(lambda: any(r == "MY> echo zzz" for r in rows())
                              and "zzz" in rows(), 15), rows()
        widget.restart()
        assert wait_until(lambda: widget.shell is not None
                          and any(r.endswith(">") for r in rows())
                          and "[abc]" in history(), 30), (rows(), history()[-5:])
    finally:
        widget.close_session()
        assert qtutil.wait_for_teardowns(15)
        widget.deleteLater()
