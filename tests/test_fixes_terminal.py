"""Regression tests for the terminal / logs / local-shell review fixes (2.1.0):
nothing blocks the GUI thread on input, no QThread is dropped while it runs,
no output is lost at EOF / past a cap / at the capture limit, and the
scrollback and the logs view stay within their memory budgets."""

from __future__ import annotations

import os
import random
import socket
import sys
import threading
import time
from collections import deque

import pytest

pyte = pytest.importorskip("pyte")

from turbossh.gui.termscreen import Row, TermScreen, rows_text  # noqa: E402


def _qt():
    pytest.importorskip("PyQt5")


def wait_until(predicate, timeout=20.0):
    from PyQt5.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(20)
    return predicate()


def wait_plain(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ====================================================================== screen
def make(cols=40, rows=6, history=100):
    screen = TermScreen(cols, rows, history=history)
    return screen, pyte.ByteStream(screen)


@pytest.mark.parametrize("seq", [b"\x1b[3K", b"\x1b[5K", b"\x1b[4J", b"\x1b[?5K",
                                 b"\x1b[9J"])
def test_unknown_erase_modes_are_ignored_and_nothing_after_them_is_lost(seq):
    """pyte raised UnboundLocalError for these; the rest of the data being fed
    (up to 16 KB of output) was dropped with it."""
    screen, stream = make()
    stream.feed(b"abc" + seq + b"def\r\nnext line")
    assert [line.rstrip() for line in screen.display[:2]] == ["abcdef", "next line"]


def test_scrollback_rows_are_compact_and_share_their_cells():
    """A scrollback line was a dict with a cell object per character: ~10 KB a
    line of ASCII, ~27 KB of UTF-8 — 100-270 MB per terminal at 10k lines."""
    import tracemalloc
    line = ("Jan 01 12:00:00 h\xf6st s\xe9rvice[1]: " + "p\xe2yl\xf6ad → 日本 "
            * 8 + "\x1b[K\r\n").encode("utf-8")
    tracemalloc.start()
    screen, stream = make(cols=200, rows=40, history=3000)
    stream.feed(line * 3000)
    used, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    top = screen.history.top
    assert len(top) >= 2900 and all(type(row) is Row for row in top)
    per_line = used / len(top)
    assert per_line < 4096, f"{per_line:.0f} bytes per scrollback line"
    cells = {id(cell) for row in top[-200:] for cell in row}
    assert len(cells) < 100, "cells are not shared between lines"
    # still readable like a screen line: text, a blank past the end, selection
    text = screen.row_text(len(top) - 1).rstrip()
    assert text.startswith("Jan 01 12:00:00 h\xf6st s\xe9rvice[1]:") and "日" in text
    assert top[-1][500].data == " "
    assert "s\xe9rvice" in screen.text_range((len(top) - 2, 0), (len(top) - 1, 30))


def test_rows_come_back_from_the_scrollback_as_writable_lines():
    screen, stream = make(cols=20, rows=6)
    for i in range(5):
        stream.feed(b"row %d\r\n" % i)
    stream.feed(b"prompt$ ")
    screen.resize_keeping_cursor(3, 20)
    assert all(type(row) is Row for row in screen.history.top)
    screen.resize_keeping_cursor(6, 20)                 # they come back
    assert [line.rstrip() for line in screen.display][:2] == ["row 0", "row 1"]
    stream.feed(b"\x1b[1;1HX")                          # and can be written again
    assert screen.display[0].rstrip() == "Xow 0"


def _unicode_stream(rng, n):
    pieces = []
    for _ in range(n):
        kind = rng.random()
        if kind < 0.45:
            pieces.append("".join(rng.choice("abc XYZ 019 \xe9\xfc\xdf→│日本"
                                              "中가\U0001f600éä")
                                  for _ in range(rng.randint(1, 50))))
        elif kind < 0.60:
            pieces.append("\r\n")
        elif kind < 0.70:
            pieces.append("\x1b[%dm" % rng.choice([0, 1, 7, 31, 42, 39]))
        elif kind < 0.80:
            pieces.append("\x1b[%d;%dH" % (rng.randint(1, 8), rng.randint(1, 30)))
        elif kind < 0.86:
            pieces.append(rng.choice(["\x1b[?7l", "\x1b[?7h", "\x1b[K", "\b", "\t"]))
        elif kind < 0.92:
            pieces.append("\x1b[%dC" % rng.randint(20, 40))   # to the right edge
        else:
            pieces.append(rng.choice(["\x1b[4h", "\x1b[4l", "\x0e", "\x0f", "\x1b(0",
                                      "\x1b(B"]))
    return "".join(pieces).encode("utf-8")


@pytest.mark.parametrize("seed", range(10))
def test_unicode_text_path_matches_pyte_exactly(seed):
    """Text beyond ASCII now takes cached cells too; it must give exactly what
    pyte gives — wide characters at the right edge, combining marks, no-wrap."""
    data = _unicode_stream(random.Random(seed), 300)
    reference = pyte.Screen(30, 8)
    pyte.ByteStream(reference).feed(data)
    screen, stream = make(cols=30, rows=8, history=0)
    stream.feed(data)
    for y in range(8):
        for x in range(30):
            assert screen.buffer[y][x] == reference.buffer[y][x], (seed, y, x)
    assert (screen.cursor.x, screen.cursor.y) == (reference.cursor.x, reference.cursor.y)


def test_snapshot_is_detached_and_rows_text_matches_the_screen():
    screen, stream = make(cols=30, rows=4, history=50)
    for i in range(10):
        stream.feed(("l\xefne %d 日\r\n" % i).encode("utf-8"))
    stream.feed(b"last")
    rows = screen.snapshot()
    expected = "\n".join(screen.row_text(i).rstrip() for i in range(screen.total_rows))
    assert rows_text(rows) == expected
    stream.feed(b"\r\nchanged afterwards\r\n")
    assert rows_text(rows) == expected                  # a snapshot does not change


# ====================================================================== writer
def _writer(write_fn, **kwargs):
    _qt()
    from turbossh.gui.terminal import Writer
    errors = []
    return Writer(write_fn, on_error=errors.append, **kwargs), errors


def test_writer_never_blocks_and_ctrl_c_drops_the_queued_paste():
    gate, got = threading.Event(), bytearray()

    def write(data):
        gate.wait(10)                       # the link takes nothing (rekey, full pipe)
        got.extend(data)

    writer, errors = _writer(write, chunk=1024)
    t0 = time.perf_counter()
    writer.send(b"p" * 200_000)
    writer.send(b"more typing")
    assert time.perf_counter() - t0 < 0.5
    assert wait_plain(lambda: writer.pending == len(b"more typing"))   # paste in flight
    assert writer.interrupt(b"\x03") == len(b"more typing")
    gate.set()
    assert wait_plain(lambda: got.endswith(b"\x03"))
    # the chunk in flight finished, the rest of the paste was dropped, ^C came next
    assert got == b"p" * 1024 + b"\x03"
    writer.close()
    assert not errors


def test_writer_finishes_an_interrupted_bracketed_paste():
    gate, got = threading.Event(), bytearray()

    def write(data):
        gate.wait(10)
        got.extend(data)

    writer, _ = _writer(write, chunk=16)
    writer.send(b"\x1b[200~" + b"x" * 1000 + b"\x1b[201~")
    assert wait_plain(lambda: writer.pending == 0)       # in flight
    writer.interrupt(b"\x03")
    gate.set()
    assert wait_plain(lambda: got.endswith(b"\x03"))
    assert got.startswith(b"\x1b[200~") and got.endswith(b"\x1b[201~\x03")
    writer.close()


def test_writer_reports_errors_once_and_keeps_going():
    calls = []

    def write(data):
        calls.append(data)
        if data.startswith(b"bad"):
            raise OSError("port gone")

    writer, errors = _writer(write)
    for _ in range(5):
        writer.send(b"bad")
    writer.send(b"good")
    assert wait_plain(lambda: b"good" in calls)
    assert [str(e) for e in errors] == ["port gone"]      # not once per keystroke
    writer.close()


def test_writer_coalesces_resizes():
    gate, done = threading.Event(), []
    writer, _ = _writer(lambda data: gate.wait(10))
    writer.send(b"x")                                    # busy
    for size in range(10):
        writer.call(lambda s=size: done.append(s), key="resize")
    gate.set()
    assert wait_plain(lambda: done)
    time.sleep(0.05)
    assert done == [9]
    writer.close()


# ====================================================================== channels
class FakeTransport:
    def __init__(self):
        self.active = True

    def is_active(self):
        return self.active


class FakeChannel:
    """A paramiko channel as the terminal uses it. ``window`` is how many bytes
    the server takes per send (None: all); ``full`` cleared = window shut."""

    def __init__(self, window=None):
        self.timeout = None
        self.inbox = deque()
        self.sent = bytearray()
        self.closed = self.eof_received = False
        self.full = threading.Event()
        self.full.set()
        self.window = window
        self.status = None
        self.resizes = []
        self.resize_gate = threading.Event()
        self.resize_gate.set()
        self.transport = FakeTransport()

    def settimeout(self, value):
        self.timeout = value

    def recv(self, n):
        if self.inbox:
            return self.inbox.popleft()
        if self.closed or self.eof_received:
            return b""
        time.sleep(self.timeout or 0.1)
        raise socket.timeout()

    def recv_ready(self):
        return bool(self.inbox)

    def send(self, data):
        if self.closed:
            raise OSError("Socket is closed")
        if not self.full.wait(self.timeout):
            raise socket.timeout()       # what paramiko does when the window stays shut
        n = len(data) if self.window is None else min(len(data), self.window)
        self.sent += data[:n]
        return n

    def sendall(self, data):             # the old code path; paramiko's sendall
        while data:
            data = data[self.send(data):]

    def resize_pty(self, width, height):
        self.resize_gate.wait(10)
        self.resizes.append((width, height))

    def exit_status_ready(self):
        # as paramiko: a closed channel counts as having an exit status (-1) —
        # including one closed because the whole connection died
        return self.closed or self.status is not None

    @property
    def exit_status(self):
        return -1 if self.status is None else self.status

    def get_transport(self):
        return self.transport


def test_channel_reader_drains_what_arrived_before_eof():
    """read_fn reported EOF as soon as eof_received was set: the last output
    (logout / reboot messages, the bridge's BUSY line) vanished."""
    _qt()
    from turbossh.gui.session_widgets import _channel_reader
    chan = FakeChannel()
    chan.settimeout(0.01)
    chan.inbox.extend([b"logout\r\n", b"Connection to box closed.\r\n"])
    chan.eof_received = chan.closed = True
    read = _channel_reader(chan)
    assert read() == b"logout\r\n"
    assert read() == b"Connection to box closed.\r\n"
    assert read() is None
    idle = FakeChannel()
    idle.settimeout(0.01)
    assert _channel_reader(idle)() == b""                # nothing yet: not EOF


def test_a_paste_survives_a_small_window_on_a_slow_link():
    """chan.settimeout(0.1) also governs sendall: a paste to a 24 KB-window server
    whose window stayed shut over 0.1 s raised socket.timeout, and the rest of
    the paste was silently dropped."""
    _qt()
    from turbossh.gui.session_widgets import _channel_writer
    chan = FakeChannel(window=3000)
    chan.settimeout(0.1)
    with pytest.raises(socket.timeout):                  # the old way
        chan.full.clear()
        chan.sendall(b"y" * 10)
    chan.full.set()
    chan.sent.clear()
    writer, errors = _writer(_channel_writer(chan))
    paste = bytes(random.Random(1).getrandbits(8) for _ in range(100_000))
    writer.send(paste)

    def stall():                                         # the window shuts a while
        for _ in range(3):
            time.sleep(0.05)
            chan.full.clear()
            time.sleep(0.35)
            chan.full.set()
    staller = threading.Thread(target=stall)
    staller.start()
    staller.join()
    assert wait_plain(lambda: len(chan.sent) == len(paste))
    assert bytes(chan.sent) == paste and not errors
    writer.close()


# ====================================================================== SSH tab
class FakeShell:
    def __init__(self, chan):
        self.channel = chan

    def resize(self, width, height):
        self.channel.resize_pty(width=width, height=height)


class FakeHandler:
    known_os = "linux"

    def __init__(self):
        self.closed = False

    def disconnect(self):
        self.closed = True


class RebindPage:
    """An SFTP page that can follow a reconnect (the FileBrowser.rebind contract)."""

    def __new__(cls):
        from PyQt5.QtWidgets import QWidget

        class Page(QWidget):
            def __init__(self):
                super().__init__()
                self.handlers = []
                self.closed = 0

            def rebind(self, handler):
                self.handlers.append(handler)

            def close_browser(self):
                self.closed += 1
        return Page()


@pytest.fixture()
def ssh_tab(qapp, monkeypatch):
    """An SshSessionWidget that does not connect by itself; ``connect()`` hands
    it a fake handler + shell as the connect thread would."""
    from turbossh.gui import qtutil
    from turbossh.gui.session_widgets import SshSessionWidget
    connects = []
    monkeypatch.setattr(SshSessionWidget, "_connect", lambda self: connects.append(1))
    widget = SshSessionWidget({"name": "fake", "host": "box", "user": "u"}, "", "")
    connects.clear()
    page = RebindPage()
    widget.inner.removeTab(widget.inner.indexOf(widget.sftp_page))
    widget.sftp_page = page
    widget.inner.addTab(page, "SFTP")
    logged = []
    widget.log.connect(logged.append)

    def connect(chan=None):
        chan = chan or FakeChannel()
        handler = FakeHandler()
        widget._on_connected(handler, FakeShell(chan))
        return chan, handler

    widget.connect_fake, widget.connects, widget.logged = connect, connects, logged
    yield widget
    widget.close_session()
    qtutil.wait_for_teardowns(5)
    widget.deleteLater()


def test_typing_pasting_and_resizing_never_block_the_gui(ssh_tab):
    chan, _ = ssh_tab.connect_fake()
    assert wait_until(lambda: chan.resizes)              # the initial PTY size
    chan.full.clear()                                    # a rekey / stalled link
    chan.resize_gate.clear()
    t0 = time.perf_counter()
    ssh_tab._send(b"z" * 300_000)                        # a big paste
    ssh_tab._on_term_resize(100, 30)
    ssh_tab._on_term_resize(101, 31)
    ssh_tab._send(b"typed")
    assert time.perf_counter() - t0 < 0.5
    # Ctrl-C drops what is still queued and goes first
    ssh_tab._interrupt()
    chan.full.set()
    chan.resize_gate.set()
    assert wait_until(lambda: chan.sent.endswith(b"\x03"))
    assert len(chan.sent) < 10_000 and b"typed" not in chan.sent
    assert wait_until(lambda: chan.resizes[-1] == (101, 31))
    assert (100, 30) not in chan.resizes                 # coalesced: the last size only


def test_send_errors_reach_the_log_without_blocking(ssh_tab):
    chan, _ = ssh_tab.connect_fake()
    chan.closed = True
    ssh_tab._send(b"lost")
    assert wait_until(lambda: any("[ERROR] send:" in line for line in ssh_tab.logged))


def test_a_reconnect_rebinds_the_sftp_page_instead_of_rebuilding_it(ssh_tab):
    page = ssh_tab.sftp_page
    _, first = ssh_tab.connect_fake()
    _, second = ssh_tab.connect_fake()
    assert ssh_tab.sftp_page is page and page.handlers == [first, second]
    assert page.closed == 0
    assert ssh_tab.inner.indexOf(page) >= 0


def test_a_shell_that_exits_is_not_reconnected(ssh_tab):
    """A nologin / ForceCommand account (the shell ends at once) looped connect /
    close with no delay, and a deliberate `exit` reconnected too."""
    chan, _ = ssh_tab.connect_fake()
    states = []
    ssh_tab.connected.connect(lambda ok, msg: states.append((ok, msg)))
    chan.status = 1
    chan.inbox.append(b"This account is currently not available.\r\n")
    chan.closed = True
    assert wait_until(lambda: "remote shell ended" in "\n".join(ssh_tab.term.screen.display))
    assert "not available" in "\n".join(ssh_tab.term.screen.display)   # drained first
    assert ssh_tab.connects == [] and not ssh_tab._reconnecting
    assert ssh_tab.term.reconnect_armed and states and states[-1][0] is False
    ssh_tab._manual_reconnect()                          # Ctrl+R still works
    assert ssh_tab.connects == [1]


def test_a_link_that_dies_reconnects_and_backs_off_when_it_dies_at_once(ssh_tab):
    chan, _ = ssh_tab.connect_fake()
    ssh_tab._connected_at = time.monotonic() - 60        # it had been up a while
    chan.transport.active = False
    chan.closed = True
    assert wait_until(lambda: ssh_tab.connects == [1])   # straight away
    assert ssh_tab._quick_drops == 0
    # the new connection dies the moment it opens: wait before trying again
    chan, _ = ssh_tab.connect_fake()
    chan.transport.active = False
    chan.closed = True
    assert wait_until(lambda: ssh_tab._reconnecting)
    assert ssh_tab._quick_drops == 1 and ssh_tab.connects == [1]
    assert "reconnecting in 2s" in "\n".join(ssh_tab.term.screen.display)
    assert wait_until(lambda: ssh_tab.connects == [1, 1], timeout=5)


def test_last_output_and_clean_exit_end_to_end(qapp, ssh_server):
    """Against the loopback server: a shell that prints a last line and exits."""
    from turbossh.gui import qtutil
    from turbossh.gui.session_widgets import SshSessionWidget

    def shell(channel):
        def run():
            channel.sendall(b"$ ")
            time.sleep(0.3)
            channel.sendall(b"bye from the box\r\n")
            channel.send_exit_status(0)
            channel.close()
        threading.Thread(target=run, daemon=True).start()
        return True

    ssh_server.server.check_channel_shell_request = shell
    widget = SshSessionWidget({"name": "loop", "host": "127.0.0.1", "port": ssh_server.port,
                               "user": "u", "ignore_hostkey": True}, "good", "")
    try:
        assert wait_until(lambda: "remote shell ended" in "\n".join(widget.term.screen.display))
        assert "bye from the box" in "\n".join(widget.term.screen.display)
        time.sleep(0.5)
        assert len(ssh_server.transports) == 1 and not widget._reconnecting
    finally:
        widget.close_session()
        qtutil.wait_for_teardowns(10)
        widget.deleteLater()



def test_a_dropped_link_reconnects_end_to_end(qapp, ssh_server):
    """Against the loopback server: the connection dies under the shell (VPN drop,
    reboot). paramiko then reports an exit status (-1) on the closed channel,
    which 2.1.0's first cut took for a deliberate exit, so nothing reconnected."""
    from turbossh.gui import qtutil
    from turbossh.gui.session_widgets import SshSessionWidget
    widget = SshSessionWidget({"name": "loop", "host": "127.0.0.1", "port": ssh_server.port,
                               "user": "u", "ignore_hostkey": True}, "good", "")
    try:
        assert wait_until(lambda: widget.status.text().startswith("Connected"))
        ssh_server.transports[0].close()                  # the link goes away
        assert wait_until(lambda: "reconnected" in "\n".join(widget.term.screen.display))
        assert "remote shell ended" not in "\n".join(widget.term.screen.display)
        assert len(ssh_server.transports) >= 2
    finally:
        widget.close_session()
        qtutil.wait_for_teardowns(10)
        widget.deleteLater()

# ====================================================================== serial tab
class FakeSerial:
    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.written = bytearray()
        self.fail = None

    def write_bytes(self, data, safe=None):
        if self.fail:
            raise self.fail
        self.gate.wait(10)
        self.written += data
        return len(data)

    def read_available(self):
        time.sleep(0.02)
        return b""

    def close(self):
        pass


def test_serial_input_goes_through_the_writer(qapp, monkeypatch):
    from PyQt5.QtCore import QThread
    from turbossh.gui import qtutil
    from turbossh.gui import session_widgets as sw

    class NoOpen(QThread):
        ok = sw._SerialConnectThread.ok
        fail = sw._SerialConnectThread.fail

        def __init__(self, *args):
            super().__init__()

        def start(self):
            pass

    monkeypatch.setattr(sw, "_SerialConnectThread", NoOpen)
    widget = sw.SerialSessionWidget({"name": "s", "device": "COM9", "baud": 9600})
    logged = []
    widget.log.connect(logged.append)
    port = FakeSerial()
    widget._on_open_local(port)
    try:
        port.gate.clear()                                # 9600 baud: it takes a while
        t0 = time.perf_counter()
        widget._send(b"q" * 50_000)
        widget._send_quick("reboot")
        assert time.perf_counter() - t0 < 0.5
        widget._interrupt()
        port.gate.set()
        assert wait_until(lambda: port.written.endswith(b"\x03"))
        assert len(port.written) <= 1024 and b"reboot" not in port.written
        port.fail = OSError("Write timeout")             # the port's write timeout
        widget._send(b"x")
        assert wait_until(lambda: any("serial write: Write timeout" in line
                                      for line in logged))
    finally:
        widget.close_session()
        qtutil.wait_for_teardowns(5)
        widget.deleteLater()


# ====================================================================== local shell
class FakeLocalShell:
    """Stands in for LocalShell: write blocks while ``gate`` is clear, as a
    shell's stdin pipe / pty does while the shell runs a command."""
    instances: list = []

    def __init__(self, label=None, cwd=None, *, uses_pty=True):
        self.label = label or "fake"
        self.uses_pty = uses_pty
        self.echoes_input = False
        self.gate = threading.Event()
        self.gate.set()
        self.written = bytearray()
        self.interrupts = 0
        self.signalled = 0
        self._ended = threading.Event()
        FakeLocalShell.instances.append(self)

    alive = True

    def start(self):
        pass

    def read(self, size=65536):
        self._ended.wait(0.05)
        return None if self._ended.is_set() else b""

    def write(self, data):
        self.gate.wait(10)
        self.written += data

    def write_ready(self):
        return True

    def send_interrupt(self, signalled=False):
        self.write(b"\x03")

    def signal_interrupt(self):
        return False

    def interrupt(self):
        self.interrupts += 1

    def resize(self, cols, rows):
        pass

    def close(self):
        self._ended.set()
        self.gate.set()


@pytest.mark.parametrize("pty", [True, False])
def test_local_terminal_paste_never_blocks_and_ctrl_c_still_works(qapp, monkeypatch, pty):
    """Every typed / pasted byte was written to the shell on the GUI thread: a
    paste bigger than the pipe while the shell was busy froze the window (for
    good with `ping -t`, where Ctrl-C could not even be pressed)."""
    from turbossh.gui import local_terminal, qtutil
    FakeLocalShell.instances.clear()
    monkeypatch.setattr(local_terminal, "LocalShell",
                        lambda label=None, cwd=None: FakeLocalShell(label, cwd, uses_pty=pty))
    widget = local_terminal.LocalTerminalWidget()
    shell = FakeLocalShell.instances[-1]
    try:
        shell.gate.clear()                               # the shell is busy
        t0 = time.perf_counter()
        widget._on_keys(b"echo hello\r" * 400)           # ~4.4 KB of commands
        assert time.perf_counter() - t0 < 0.5
        widget._interrupt()                              # the Ctrl-C button
        shell.gate.set()
        if pty:
            assert wait_until(lambda: shell.written.endswith(b"\x03"))
            assert len(shell.written) <= 1024 + 1
        else:
            assert wait_until(lambda: shell.interrupts == 1)
            time.sleep(0.1)
            assert shell.written.count(b"echo hello") < 400
    finally:
        widget.close_session()
        qtutil.wait_for_teardowns(5)
        widget.deleteLater()


@pytest.mark.skipif(os.name != "nt", reason="the stdin pipe of a Windows shell")
def test_real_shell_busy_with_a_command_does_not_freeze_a_paste(qapp, tmp_path):
    from turbossh.gui import qtutil
    from turbossh.gui.local_terminal import LocalTerminalWidget

    def screen():
        s = widget.term.screen
        return "\n".join(s.row_text(i) for i in range(s.total_rows))

    widget = LocalTerminalWidget()
    try:
        assert wait_until(lambda: widget.shell is not None and screen().strip(), 30)
        sleeper = (f'"{sys.executable}" -c "import time; print(7*191, flush=True); '
                   f'time.sleep(60)"')
        if widget.shell.label == "PowerShell":
            sleeper = "& " + sleeper
        widget._on_keys(sleeper.encode() + b"\r")
        assert wait_until(lambda: "1337" in screen(), 30)
        t0 = time.perf_counter()
        widget._on_keys(b"echo queued-while-busy\r" * 1500)   # ~35 KB > the pipe
        assert time.perf_counter() - t0 < 5.0, "the paste blocked the GUI thread"
        widget._interrupt()
        widget._on_keys(b"echo after-the-interrupt\r")
        assert wait_until(lambda: screen().count("after-the-interrupt") >= 2, 30)
    finally:
        shell = widget.shell
        widget.close_session()
        assert qtutil.wait_for_teardowns(15)
        assert shell is None or not shell.alive
        widget.deleteLater()


def test_local_shell_signal_interrupt_is_off_without_a_pty():
    from turbossh.gui.localshell import LocalShell
    shell = LocalShell()
    assert shell.signal_interrupt() is False             # not started / pipes


# ====================================================================== logs tab
class FakeLogHandler:
    known_os = "linux"

    def __init__(self, lines, delay=0.0):
        self.lines, self.delay = lines, delay

    def iter_lines(self, command, stop_event=None, get_pty=True):
        for line in self.lines:
            if stop_event is not None and stop_event.is_set():
                return
            yield line
        if self.delay:
            time.sleep(self.delay)


def test_a_one_shot_log_command_parks_its_thread(qapp, monkeypatch):
    """run() emits `stopped` as its last step, and the slot dropped the only
    reference to the still-running QThread: Qt aborted the whole app after
    every one-shot preset (journalctl, slog2info, dmesg)."""
    from turbossh.gui import logs_tab, qtutil
    made = []

    class SlowToEnd(logs_tab._LogThread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

        def run(self):
            super().run()
            time.sleep(0.6)              # still running when `stopped` is handled

    monkeypatch.setattr(logs_tab, "_LogThread", SlowToEnd)
    tab = logs_tab.LogsTab()
    tab.set_handler(FakeLogHandler(["one", "two"]))
    tab._start()
    assert wait_until(lambda: tab._thread is None)
    thread = made[0]
    assert thread.isRunning() and thread in qtutil._PARKED
    assert "two" in tab.view.toPlainText()
    assert wait_until(lambda: thread not in qtutil._PARKED)
    tab.close_tab()
    tab.deleteLater()


def test_worker_overflow_is_counted_and_shown(qapp):
    from turbossh.gui import logs_tab
    thread = logs_tab._LogThread(FakeLogHandler([f"line {i}" for i in range(50)]),
                                 "x", threading.Event(), cap=10)
    thread.run()                                         # inline: no thread needed
    lines = thread.pull(100)
    assert "40 lines skipped" in lines[0] and lines[1:] == [f"line {i}" for i in range(40, 50)]
    assert thread.pull(100) == []
    tab = logs_tab.LogsTab()
    tab.filter.setText("line 4")
    tab._apply_filter()
    tab._accept(lines)
    shown = tab.view.toPlainText().splitlines()
    assert shown[0] == lines[0]                          # the marker passes any filter
    assert shown[1:] == [f"line {i}" for i in range(40, 50)]
    tab.close_tab()
    tab.deleteLater()


def test_logs_view_is_capped_and_refiltering_never_freezes(qapp):
    from turbossh.gui import logs_tab
    tab = logs_tab.LogsTab()
    tab.resize(900, 500)
    tab.show()
    assert tab.view.maximumBlockCount() == logs_tab._VIEW_LINES <= logs_tab._KEEP_LINES
    lines = [f"Jan 01 svc[{i}]: {'error' if i % 5 == 0 else 'info'} n={i}"
             for i in range(logs_tab._KEEP_LINES)]
    t0 = time.perf_counter()
    tab._accept(lines)
    assert time.perf_counter() - t0 < 1.0
    assert wait_until(lambda: not tab._busy(), 60)
    doc = tab.view.document()
    assert doc.blockCount() <= logs_tab._VIEW_LINES
    assert doc.lastBlock().text() == lines[-1]
    # re-filtering 200k stored lines: a short step now, the rest on the timer
    tab.filter.setText("error")
    t0 = time.perf_counter()
    tab._apply_filter()
    assert time.perf_counter() - t0 < 0.5
    # the newest matches are at the bottom straight away
    assert tab.view.document().lastBlock().text() == lines[-5]
    assert wait_until(lambda: not tab._busy(), 60)
    shown = tab.view.toPlainText().splitlines()
    assert shown == [ln for ln in lines if "error" in ln][-len(shown):]
    assert len(shown) == min(logs_tab._VIEW_LINES, len(lines) // 5)
    assert tab._count == len(lines) // 5                  # every stored match counted
    tab.close_tab()
    tab.deleteLater()


def test_stop_and_close_with_a_full_backlog_do_not_render_it_at_once(qapp):
    from turbossh.gui import logs_tab, qtutil
    lines = [f"backlog {i}" for i in range(logs_tab._KEEP_LINES)]
    for closing in (False, True):
        tab = logs_tab.LogsTab()
        tab.set_handler(FakeLogHandler(lines, delay=30))
        tab._start()
        thread = tab._thread
        tab._timer.stop()                                # the GUI fell behind
        assert wait_until(lambda: len(thread._lines) == len(lines), 30)
        t0 = time.perf_counter()
        if closing:
            tab.close_tab()
        else:
            tab._stop()
        assert time.perf_counter() - t0 < 0.5
        if closing:
            assert tab.view.document().isEmpty() and not tab._timer.isActive()
        else:
            assert wait_until(lambda: not tab._busy(), 60)
            assert tab.view.document().lastBlock().text() == lines[-1]
        assert thread in qtutil._PARKED or thread.isFinished()
        tab.close_tab()
        tab.deleteLater()


def test_logs_save_writes_every_stored_match(qapp, tmp_path):
    from turbossh.gui import logs_tab
    tab = logs_tab.LogsTab()
    tab._accept([f"{'error' if i % 2 else 'ok'} {i}" for i in range(120_000)])
    tab.filter.setText("error")
    tab._apply_filter()
    assert wait_until(lambda: not tab._busy(), 60)
    out = tmp_path / "log.txt"
    tab._save_to(str(out)).join(30)
    saved = out.read_text(encoding="utf-8").splitlines()
    assert len(saved) == 60_000 and saved[0] == "error 1"   # beyond the view's cap
    assert wait_until(lambda: "saved 60000 lines" in tab.statusrow.text())
    tab.close_tab()
    tab.deleteLater()


# ====================================================================== the widget
def test_capture_keeps_the_latest_output_and_save_includes_it(qapp, tmp_path, monkeypatch):
    """The capture stopped at 256 MB and Save then saved only that: everything
    after it — including what was on screen — was missing."""
    from turbossh.gui import vt100
    monkeypatch.setattr(vt100, "_CAPTURE_LIMIT", 64 * 1024)
    term = vt100.Vt100Terminal(send_fn=lambda data: None)
    try:
        for i in range(20000):
            term._feed(b"capture line %06d \xe2\x86\x92 end\r\n" % i)
        parts = [p for p in term._cap_parts if os.path.exists(p)]
        assert len(parts) <= vt100._CAPTURE_PARTS
        assert sum(os.path.getsize(p) for p in parts) <= vt100._CAPTURE_LIMIT
        out = tmp_path / "all.txt"
        term.save_output_to(str(out)).join(30)
        text = out.read_text(encoding="utf-8")
        assert "earlier output is not included" in text.splitlines()[0]
        assert text.rstrip().endswith("capture line 019999 → end")
        body = [ln for ln in text.splitlines()[2:] if ln]
        # parts are one stream: no line cut in two where a part began
        assert all(ln.startswith("capture line ") and ln.endswith("→ end")
                   for ln in body)
        numbers = [int(ln.split()[2]) for ln in body]
        assert numbers == list(range(numbers[0], 20000))
    finally:
        paths = list(term._cap_parts)
        term.cleanup()
        assert not any(os.path.exists(p) for p in paths)
        term.deleteLater()


def test_save_builds_the_fallback_text_on_the_worker_and_only_if_needed(qapp, tmp_path,
                                                                       monkeypatch):
    from turbossh.gui import vt100
    term = vt100.Vt100Terminal(send_fn=lambda data: None)
    built = []
    real = vt100.rows_text
    monkeypatch.setattr(vt100, "rows_text",
                        lambda rows: built.append(threading.current_thread()) or real(rows))
    try:
        for i in range(30000):
            term._feed(b"history line %d\r\n" % i)
        t0 = time.perf_counter()
        worker = term.save_output_to(str(tmp_path / "with-capture.txt"))
        assert time.perf_counter() - t0 < 0.5             # was 1.7 s at 50k x 200
        worker.join(30)
        assert built == []                                # the capture was used
        # no capture: the scrollback + screen, built on the worker
        term._cap_parts = []
        term.save_output_to(str(tmp_path / "screen.txt")).join(30)
        assert built and built[0] is not threading.main_thread()
        text = (tmp_path / "screen.txt").read_text(encoding="utf-8")
        assert "history line 29999" in text
    finally:
        term.cleanup()
        term.deleteLater()


class Flood:
    """A reader that always has more output (short lines: the worst case)."""

    def __init__(self):
        self.taken = 0

    def pull(self, maxn):
        self.taken += maxn
        return (b"x\r\n" * (maxn // 3 + 1))[:maxn]


def test_terminals_share_one_budget_per_tick(qapp):
    """Each terminal had its own 12 ms budget, checked between 16 KB slices
    (55-130 ms per tick with short lines): several flooding tabs in split view
    took the whole GUI thread."""
    from turbossh.gui import vt100
    terms, floods = [], []
    for _ in range(4):
        term = vt100.Vt100Terminal(send_fn=lambda data: None)
        flood = Flood()
        term.set_source(flood.pull)
        terms.append(term)
        floods.append(flood)
    import gc
    try:
        pump = vt100._pump()
        pump._tick()                                      # warm up
        ticks = []
        gc.collect()
        gc.disable()                 # a full collection of the suite's heap is not ours
        try:
            for _ in range(8):
                t0 = time.perf_counter()
                pump._tick()
                ticks.append(time.perf_counter() - t0)
        finally:
            gc.enable()
        # one budget (12 ms) + one 2 KB slice + the repaint bookkeeping; the old
        # per-terminal budgets took 4 x (12 ms + a 16 KB slice) here
        typical = sorted(ticks)[len(ticks) // 2]
        assert typical < 0.040, f"{typical * 1000:.0f} ms in one tick for 4 floods"
        assert all(flood.taken > 0 for flood in floods), "a terminal was starved"
    finally:
        for term in terms:
            term.cleanup()
            term.deleteLater()


def test_paint_reads_scrollback_rows(qapp):
    from turbossh.gui import vt100
    term = vt100.Vt100Terminal(send_fn=lambda data: None)
    term.resize(600, 300)
    try:
        for i in range(300):
            term.feed(("r\xf6w %d 日本 \x1b[31mred\x1b[0m\r\n" % i).encode("utf-8"))
        term._scroll_lines(100)                           # into the compact rows
        assert term.grab().isNull() is False
        assert type(term._visible_rows()[0]) is Row
    finally:
        term.cleanup()
        term.deleteLater()
