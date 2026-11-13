"""Regression tests for the second review of the 2.1.0 terminal fixes: what the
first round made safer must still behave as 2.0.0 did where users rely on it —
Ctrl-C on a local pty, the end of an interrupted bracketed paste, the Logs
tab's Save result and Find, and the capture of a long session."""

from __future__ import annotations

import os
import queue
import re
import threading
import time

import pytest

from turbossh.gui.localshell import LocalShell


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


# ====================================================================== local pty
class PipePty(LocalShell):
    """A LocalShell on a fake pty. Its "master" is the write end of a pipe that
    a thread reads only while ``reading`` is set — otherwise the pipe fills and
    a write blocks, as a pty full of input the program has not read does.
    ``ready`` stands for the pty's POLLOUT; signal_interrupt counts (a
    foreground job that takes SIGINT)."""

    def __init__(self, label=None, cwd=None):
        super().__init__(label, cwd)
        self.uses_pty = True
        self.ready = True
        self.signals = 0
        self.got = bytearray()
        self.reading = threading.Event()
        self._ended = threading.Event()
        self._rfd, self._wfd = os.pipe()
        self._drainer = threading.Thread(target=self._drain, daemon=True)
        self._drainer.start()

    def _drain(self):
        while True:
            self.reading.wait()
            try:
                data = os.read(self._rfd, 65536)
            except OSError:
                return
            if not data:
                return
            self.got += data

    def start(self):
        self._master = self._wfd

    def read(self, size=65536):
        self._ended.wait(0.05)
        return None if self._ended.is_set() else b""

    def write_ready(self):
        return self.ready

    def signal_interrupt(self):
        self.signals += 1
        return True

    def resize(self, cols, rows):
        pass

    def close(self):
        self._ended.set()
        self.reading.set()
        super().close()                  # closes the "master": the reader sees EOF
        self._drainer.join(5)
        try:
            os.close(self._rfd)
        except OSError:
            pass


def test_local_shell_interrupt_sends_ctrl_c_through_the_pty():
    """2.1.0's first cut sent SIGINT (killpg) instead of writing ^C into the
    pty: the line discipline never saw it, so the lines typed while a command
    ran were not thrown away — they ran once it was interrupted."""
    shell = PipePty()
    shell.start()
    shell.reading.set()
    try:
        shell.interrupt()
        assert wait_plain(lambda: bytes(shell.got) == b"\x03")
        assert shell.signals == 0
        # a pty too full to take the byte: the job is signalled, the byte follows
        shell.ready = False
        shell.interrupt()
        assert wait_plain(lambda: bytes(shell.got) == b"\x03\x03")
        assert shell.signals == 1
    finally:
        shell.close()


def test_write_ready_polls_the_pty_without_blocking(monkeypatch):
    import select
    shell = LocalShell()
    assert shell.write_ready() is True              # pipes / not started: not asked
    shell._master = 42
    seen = []
    try:
        monkeypatch.setattr(select, "select",
                            lambda r, w, x, t=None: seen.append((r, w, x, t)) or ([], [], []))
        assert shell.write_ready() is False and seen == [([], [42], [], 0)]
        monkeypatch.setattr(select, "select", lambda r, w, x, t=None: ([], w, []))
        assert shell.write_ready() is True
    finally:
        shell._master = None


@pytest.fixture()
def pty_tab(qapp, monkeypatch):
    pytest.importorskip("pyte")
    from turbossh.gui import local_terminal, qtutil
    made = []

    def make(label=None, cwd=None):
        made.append(PipePty(label, cwd))
        return made[-1]

    monkeypatch.setattr(local_terminal, "LocalShell", make)
    widget = local_terminal.LocalTerminalWidget()
    widget.pty = made[-1]
    yield widget
    widget.close_session()
    qtutil.wait_for_teardowns(5)
    widget.deleteLater()


def test_ctrl_c_on_a_local_pty_goes_through_it(pty_tab):
    """Keyboard Ctrl-C is the byte, written through the pty (after what was
    typed ahead), not a signal: no SIGINT while the pty takes input."""
    shell = pty_tab.pty
    shell.reading.set()
    pty_tab._on_keys(b"sleep 30\r")
    pty_tab._on_keys(b"echo typed-ahead\r" * 3)      # lines typed while it runs
    assert wait_until(lambda: shell.got.endswith(b"echo typed-ahead\r"))
    pty_tab._on_keys(b"\x03")
    assert wait_until(lambda: shell.got.endswith(b"\x03"))
    pty_tab._interrupt()                             # the Ctrl-C button: the same
    assert wait_until(lambda: shell.got.endswith(b"\x03\x03"))
    assert shell.signals == 0


def test_ctrl_c_on_a_full_pty_signals_at_once_and_the_byte_follows(pty_tab):
    """Only when the pty cannot take the byte (a paste the program does not
    read fills it) is the job signalled — straight away, from the GUI thread —
    and the rest of the paste is dropped; the byte goes in once there is room,
    and the job is not signalled twice for one Ctrl-C."""
    shell = pty_tab.pty
    paste = b"echo pasted-while-busy\r" * 50_000          # ~1.1 MB: far more than a pipe holds
    pty_tab._on_keys(paste)
    time.sleep(0.3)                                       # the writer is stuck
    shell.ready = False
    t0 = time.perf_counter()
    pty_tab._on_keys(b"\x03")
    assert time.perf_counter() - t0 < 0.5
    assert shell.signals == 1
    shell.reading.set()                                   # the program reads again
    assert wait_until(lambda: shell.got.endswith(b"\x03"))
    assert len(shell.got) < len(paste) // 4
    time.sleep(0.1)
    assert shell.signals == 1


# ====================================================================== writer
def _writer(write_fn, **kwargs):
    pytest.importorskip("PyQt5")
    from turbossh.gui.terminal import Writer
    errors = []
    return Writer(write_fn, on_error=errors.append, **kwargs), errors


def test_the_paste_end_marker_gets_through_a_full_window():
    """The end marker of an interrupted bracketed paste had one write: a full
    SSH window (just when Ctrl-C is pressed on a stuck paste) took nothing, or
    part of it, and vim stayed in paste mode."""
    got, gate, under_way = bytearray(), threading.Event(), threading.Event()
    full = [0]

    def write(data):
        if len(got) >= 6:                 # the start marker is out: the window shuts
            under_way.set()
            gate.wait(10)
        if full[0]:
            full[0] -= 1
            return 0                      # nothing taken (paramiko: socket.timeout)
        n = min(len(data), 4)             # a small window: a little at a time
        got.extend(data[:n])
        return n

    writer, errors = _writer(write, chunk=16)
    writer.send(b"\x1b[200~" + b"x" * 1000 + b"\x1b[201~")
    assert under_way.wait(10)
    full[0] = 5
    writer.interrupt(b"\x03")
    gate.set()
    assert wait_plain(lambda: got.endswith(b"\x03"))
    assert bytes(got) == b"\x1b[200~xx\x1b[201~\x03"     # the whole marker, then ^C
    writer.close()
    assert not errors


def test_the_paste_end_marker_waits_no_longer_than_the_stall_timeout():
    got, opened = bytearray(), threading.Event()

    def write(data):
        if len(got) >= 8 and not opened.is_set():
            return 0                      # the window stays shut
        n = min(len(data), 4)
        got.extend(data[:n])
        return n

    writer, errors = _writer(write, chunk=16, stall_timeout=0.5)
    writer.send(b"\x1b[200~" + b"x" * 100 + b"\x1b[201~")
    assert wait_plain(lambda: len(got) == 8)
    writer.interrupt(b"\x03")
    # the marker gives up, then the Ctrl-C does — reported, not a hang
    assert wait_plain(lambda: errors, 5)
    assert isinstance(errors[0], TimeoutError)
    opened.set()
    writer.send(b"ok")
    assert wait_plain(lambda: got.endswith(b"ok"))
    writer.close()


def test_an_interrupt_replaces_one_still_waiting():
    gate, calls, got = threading.Event(), [], bytearray()

    def write(data):
        gate.wait(10)
        got.extend(data)

    writer, _ = _writer(write)
    writer.send(b"busy")
    assert wait_plain(lambda: writer.pending == 0)
    writer.interrupt(lambda: calls.append(1))
    writer.interrupt(lambda: calls.append(2))
    gate.set()
    assert wait_plain(lambda: calls)
    time.sleep(0.05)
    assert calls == [2] and got == b"busy"
    writer.close()


# ====================================================================== logs tab
class QueueLogHandler:
    known_os = "linux"

    def __init__(self):
        self.queue = queue.Queue()

    def iter_lines(self, command, stop_event=None, get_pty=True):
        while not (stop_event is not None and stop_event.is_set()):
            try:
                line = self.queue.get(timeout=0.02)
            except queue.Empty:
                continue
            yield line


def test_a_save_result_is_not_wiped_by_the_live_status(qapp, tmp_path, monkeypatch):
    """While live, "● live — N lines" was re-set every 50 ms tick, new lines or
    not: "saved N lines to …" flashed for < 50 ms, and a failed save went
    unreported."""
    from PyQt5.QtTest import QTest
    from turbossh.gui import logs_tab
    monkeypatch.setattr(logs_tab, "_NOTE_SECONDS", 1.0, raising=False)
    tab = logs_tab.LogsTab()
    handler = QueueLogHandler()
    tab.set_handler(handler)
    for i in range(3):
        handler.queue.put(f"line {i}")
    tab._start()
    try:
        assert wait_until(lambda: tab.statusrow.text() == "● live — 3 lines")
        tab._save_to(str(tmp_path / "log.txt")).join(10)
        assert wait_until(lambda: tab.statusrow.text().startswith("saved 3 lines"))
        QTest.qWait(1500)            # live, past the note's time, no new lines
        assert tab.statusrow.text().startswith("saved 3 lines")
        tab._save_to(str(tmp_path / "no-such-folder" / "log.txt")).join(10)
        assert wait_until(lambda: "could not save" in tab.statusrow.text())
        handler.queue.put("line 3")  # lines keep coming: the note stays its time
        QTest.qWait(300)
        assert "could not save" in tab.statusrow.text()
        assert wait_until(lambda: tab.statusrow.text() == "● live — 4 lines", 5)
    finally:
        tab.close_tab()
        tab.deleteLater()


def test_find_looks_in_the_lines_older_than_the_view(qapp):
    """The view holds the latest 50k lines and Find searched only those: an
    error 60k-200k lines back was "not found", though Save wrote it (2.0.0's
    view held them all)."""
    from turbossh.gui import logs_tab
    tab = logs_tab.LogsTab()
    lines = [f"svc[{i}]: info n={i}" for i in range(120_000)]
    lines[100] = "svc[100]: error E-4711 checksum mismatch"
    tab._accept(lines)
    try:
        assert wait_until(lambda: not tab._busy(), 60)
        assert "E-4711" not in tab.view.toPlainText()  # beyond the view's 50k lines
        tab.find.setText("e-4711")                  # case-insensitive by default
        assert tab.find_next() is False
        assert wait_until(lambda: "found" in tab._find_status.text())
        assert tab._find_status.text() == "found in 1 older line (not shown)"
        assert not tab._find_show.isHidden()
        tab._on_find_done(tab._find_seq - 1, 7)       # a late answer is dropped
        assert tab._find_status.text() == "found in 1 older line (not shown)"
        tab._find_show.click()                        # filter for it
        assert wait_until(lambda: not tab._busy(), 60)
        assert tab.view.toPlainText().splitlines() == [lines[100]]
        assert tab.find_next() is True and tab._find_show.isHidden()
        # in no stored line at all: still "not found"
        tab.find.setText("absent")
        assert tab.find_next() is False
        assert wait_until(lambda: tab._find_status.text() == "not found")
        # the filter still decides what counts: a line it hides is not offered
        tab.filter.setText("info")
        tab._apply_filter()
        assert wait_until(lambda: not tab._busy(), 60)
        tab.find.setText("E-4711")
        assert tab.find_next() is False
        assert wait_until(lambda: tab._find_status.text() == "not found")
    finally:
        tab.close_tab()
        tab.deleteLater()


# ====================================================================== capture
def _rotated_terminal(monkeypatch, lines=1500):
    """A terminal whose capture has rotated into 3 parts (none dropped yet)."""
    from turbossh.gui import vt100
    monkeypatch.setattr(vt100, "_CAPTURE_LIMIT", 64 * 1024)
    term = vt100.Vt100Terminal(send_fn=lambda data: None)
    for i in range(lines):
        term._feed(b"capture line %06d \xe2\x86\x92 end\r\n" % i)
    assert len(term._cap_parts) == 3 and not term._cap_trimmed
    return term


def _numbers(text):
    return [int(m.group(1)) for m in re.finditer(r"^capture line (\d{6}) → end$", text, re.M)]


def test_a_new_terminal_does_not_sweep_a_live_sessions_parts(qapp, tmp_path, monkeypatch):
    """Every new terminal deleted capture files a day old — the rotated parts of
    a session still running too (a part is not written once the next starts),
    and Save then left them out without a word."""
    from turbossh.gui import vt100
    pytest.importorskip("pyte")
    first = _rotated_terminal(monkeypatch)
    second = None
    parts = list(first._cap_parts)
    try:
        day_old = time.time() - 2 * 86400
        for path in parts:
            os.utime(path, (day_old, day_old))
        crashed = os.path.join(os.path.dirname(parts[0]), "term-999999999-1.0.log")
        with open(crashed, "wb") as fh:
            fh.write(b"a crashed session's capture\r\n")
        os.utime(crashed, (day_old, day_old))
        second = vt100.Vt100Terminal(send_fn=lambda data: None)     # it sweeps
        assert all(os.path.exists(path) for path in parts)
        assert not os.path.exists(crashed)                         # still swept
        out = tmp_path / "all.txt"
        first.save_output_to(str(out)).join(30)
        text = out.read_text(encoding="utf-8")
        assert "not included" not in text
        assert _numbers(text) == list(range(1500))
    finally:
        first.cleanup()
        first.deleteLater()
        if second is not None:
            second.cleanup()
            second.deleteLater()


def test_a_terminal_keeps_its_capture_parts_fresh(qapp, monkeypatch):
    """Another TurboSSH process (the embedded GUI next to the desktop one)
    sweeps by age: a live session keeps its parts' times fresh, quiet or not."""
    pytest.importorskip("pyte")
    term = _rotated_terminal(monkeypatch)
    try:
        assert term._cap_timer.isActive()
        assert term._cap_timer.interval() <= 86400 * 1000 // 4
        day_old = time.time() - 2 * 86400
        for path in term._cap_parts:
            os.utime(path, (day_old, day_old))
        term._keep_capture_fresh()
        assert all(time.time() - os.path.getmtime(path) < 3600 for path in term._cap_parts)
    finally:
        term.cleanup()
        term.deleteLater()


def test_save_says_so_when_a_capture_part_is_missing(qapp, tmp_path, monkeypatch):
    """A part gone at save time (dropped since the snapshot, or deleted) was
    skipped without the "earlier output is not included" note."""
    pytest.importorskip("pyte")
    term = _rotated_terminal(monkeypatch)
    parts = list(term._cap_parts)
    try:
        os.remove(parts[1])                          # a gap in the middle
        out = tmp_path / "gap.txt"
        term.save_output_to(str(out)).join(30)
        lines = out.read_text(encoding="utf-8").splitlines()
        assert "not included" not in lines[0]
        gap = [i for i, ln in enumerate(lines) if "not included here" in ln]
        assert len(gap) == 1
        before, after = _numbers("\n".join(lines[:gap[0]])), _numbers("\n".join(lines[gap[0]:]))
        assert before[0] == 0 and after[-1] == 1499 and after[0] > before[-1] + 1
        os.remove(parts[0])                          # the oldest output is gone
        out = tmp_path / "trimmed.txt"
        term.save_output_to(str(out)).join(30)
        text = out.read_text(encoding="utf-8")
        assert "earlier output is not included" in text.splitlines()[0]
        assert _numbers(text)[-1] == 1499
    finally:
        term.cleanup()
        term.deleteLater()
