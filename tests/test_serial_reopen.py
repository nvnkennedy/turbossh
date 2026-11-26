"""SerialHandler: typed open errors (busy / missing / error), auto_reopen across a
port that goes away (a USB console unplugged, an ECU power cycle that drops its
USB-serial adapter), on_gap, and reads and writes from different threads.

No hardware: pyserial's Serial is replaced by a fake port whose far end (the
"device") can be unplugged and plugged back in."""

from __future__ import annotations

import os

import threading
import time

import pytest

serial = pytest.importorskip("serial")

from turbossh import serial_handler  # noqa: E402
from turbossh.exceptions import SSHError  # noqa: E402
from turbossh.serial_handler import (  # noqa: E402
    SerialBusyError, SerialError, SerialHandler, SerialMissingError)


class Device:
    """The far end of the fake port: what the device sends, what it was sent,
    and whether it is plugged in. A handle opened before an unplug stays dead
    after the replug, as a real one does."""

    def __init__(self):
        self.cond = threading.Condition()
        self.present = True
        self.epoch = 0                  # bumped by every unplug
        self.outgoing = bytearray()     # sent by the device, for the host to read
        self.received = bytearray()     # what the host wrote
        self.attempts = 0               # every open, failed or not
        self.opening = 0                # opens waiting at open_gate
        self.ports = []                 # every port that opened
        self.reading = self.writing = 0
        self.overlaps = []              # two reads (or two writes) at once
        self.read_entered = threading.Event()
        self.open_error = None          # raised by every open while set
        self.open_gate = None           # an Event every open waits for while set
        self.handlers = []

    def send(self, data: bytes):
        with self.cond:
            self.outgoing += data
            self.cond.notify_all()

    def unplug(self):
        with self.cond:
            self.present = False
            self.epoch += 1
            self.cond.notify_all()      # a read blocked on the port fails now

    def replug(self, gate=None):
        """Plug it back in; with *gate*, every open from now on waits for it
        (set together, so an open already under way cannot slip past it)."""
        with self.cond:
            self.present = True
            self.open_gate = gate

    def open_ports(self):
        return [p for p in self.ports if p.is_open]


class FakePort:
    """Just enough of pyserial's Serial for SerialHandler."""

    def __init__(self, device, port, baudrate, **kw):
        with device.cond:
            device.attempts += 1
            gate = device.open_gate
            if gate is None:
                self._open(device, port, baudrate, kw)
                return
            device.opening += 1
        gate.wait(10)
        with device.cond:
            device.opening -= 1
            self._open(device, port, baudrate, kw)

    def _open(self, device, port, baudrate, kw):
        """With device.cond held."""
        if device.open_error is not None:
            raise device.open_error
        if not device.present:
            raise serial.SerialException(
                f"could not open port {port!r}: FileNotFoundError(2, 'The system "
                f"cannot find the file specified.', None, 2)")
        self.device, self.epoch = device, device.epoch
        self.baudrate, self.timeout = int(baudrate), kw.get("timeout")
        self.is_open = True
        device.ports.append(self)

    def _alive(self):
        """With device.cond held: raise as pyserial does on a dead port."""
        if not self.is_open:
            raise serial.SerialException("Attempting to use a port that is not open")
        if self.epoch != self.device.epoch:
            raise serial.SerialException(
                "ClearCommError failed (PermissionError(13, 'Access is denied.', None, 5))")

    @property
    def in_waiting(self):
        with self.device.cond:
            self._alive()
            return len(self.device.outgoing)

    def _take(self, ready, amount):
        """Wait, up to the port's timeout, until *ready* says the device's
        output has what the read wants; then (or at the timeout) take
        ``amount`` bytes of it."""
        d = self.device
        with d.cond:
            d.reading += 1
            if d.reading > 1:
                d.overlaps.append("read")
            d.read_entered.set()
            try:
                deadline = None if self.timeout is None else time.monotonic() + self.timeout
                while True:
                    self._alive()
                    left = None if deadline is None else deadline - time.monotonic()
                    if ready(d.outgoing) or (left is not None and left <= 0):
                        n = amount(d.outgoing)
                        data = bytes(d.outgoing[:n])
                        del d.outgoing[:n]
                        return data
                    d.cond.wait(left)
            finally:
                d.reading -= 1

    def read(self, n):
        return self._take(lambda buf: len(buf) > 0, lambda buf: min(n, len(buf)))

    def readline(self):
        return self._take(lambda buf: b"\n" in buf,
                          lambda buf: buf.index(b"\n") + 1 if b"\n" in buf else len(buf))

    def write(self, data):
        d = self.device
        with d.cond:
            self._alive()
            d.writing += 1
            if d.writing > 1:
                d.overlaps.append("write")
        try:                            # (released in between: another write could come in)
            with d.cond:
                self._alive()
                d.received += data
            return len(data)
        finally:
            with d.cond:
                d.writing -= 1

    def close(self):
        with self.device.cond:
            self.is_open = False
            self.device.cond.notify_all()


@pytest.fixture()
def device(monkeypatch):
    dev = Device()
    monkeypatch.setattr(serial_handler.serial, "Serial",
                        lambda *a, **kw: FakePort(dev, *a, **kw))
    monkeypatch.setattr(serial_handler, "REOPEN_INTERVAL", 0.02)
    yield dev
    for handler in dev.handlers:
        handler.close()


def _handler(device, port="COM9", **kw):
    kw.setdefault("timeout", 0.05)
    kw.setdefault("quiet", True)
    handler = SerialHandler(port, **kw)
    device.handlers.append(handler)
    return handler


def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.002)


def _read_until(handler, wanted, timeout=5.0):
    got, deadline = b"", time.monotonic() + timeout
    while wanted not in got:
        if time.monotonic() > deadline:
            raise AssertionError(f"never read {wanted!r} (got {got!r})")
        got += handler.read_available()
    return got


def _reopen_threads(port):
    return [t for t in threading.enumerate() if t.name == f"turbossh-serial-reopen-{port}"]


# ------------------------------------------------------------------ auto_reopen
def test_an_unplugged_port_is_opened_again_and_the_gap_reported(device):
    gaps, logs = [], []
    handler = _handler(device, auto_reopen=True, log_callback=logs.append,
                       on_gap=lambda started, ended: gaps.append((started, ended)))
    handler.open()
    device.send(b"before\n")
    _read_until(handler, b"before\n")

    unplugged = time.time()
    device.unplug()
    assert handler.read_available() == b""         # finds it gone: no exception
    _until(lambda: device.attempts >= 3)            # and keeps trying to open it
    took = time.perf_counter()
    assert handler.read_available() == b""         # a read waits its timeout, then b""
    assert 0.03 <= time.perf_counter() - took < 2.0
    assert gaps == []

    device.replug()
    _until(lambda: gaps)
    device.send(b"after\n")
    _read_until(handler, b"after\n")
    assert len(gaps) == 1
    started, ended = gaps[0]
    assert unplugged - 1 < started < ended <= time.time()
    assert len(device.open_ports()) == 1            # the dead handle was closed
    assert any(m.startswith("[WARNING]") and "COM9 went away" in m for m in logs)
    assert any(m.startswith("[INFO]") and "COM9 is back after a" in m and " s gap" in m
               for m in logs)


def test_iter_lines_and_stream_keep_going_while_the_port_is_gone(device):
    gaps = []
    handler = _handler(device, auto_reopen=True, on_gap=lambda s, e: gaps.append(e))
    handler.open()
    stop = threading.Event()
    lines, errors = [], []

    def reader():
        try:
            for line in handler.iter_lines(stop_event=stop):
                lines.append(line)
        except Exception as exc:        # pragma: no cover - the test fails on it
            errors.append(exc)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    device.send(b"one\npart")
    _until(lambda: lines == ["one"] and not device.outgoing)    # "part" is read too
    device.unplug()
    _until(lambda: "part" in lines)     # the line the unplug cut off, on its own
    device.replug()
    _until(lambda: gaps)
    device.send(b"two\n")
    _until(lambda: "two" in lines)

    attempts = device.attempts
    device.unplug()                     # gone again: stop_event still ends it
    _until(lambda: device.attempts > attempts)
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == [] and lines == ["one", "part", "two"]

    took = time.monotonic()             # stream's own timeout too
    result = handler.stream(timeout=0.2)
    assert result["lines"] == 0 and time.monotonic() - took < 2.0


def test_a_write_while_the_port_is_gone_raises_missing(device):
    gaps = []
    handler = _handler(device, auto_reopen=True, on_gap=lambda s, e: gaps.append(e))
    handler.open()
    device.unplug()
    # here the write is what finds the port gone: it raises, and reopening starts
    with pytest.raises(SerialMissingError, match="Write to COM9 failed") as caught:
        handler.write_line("first")
    assert caught.value.kind == "missing" and caught.value.__cause__ is not None
    assert handler.read_available() == b""         # reads do not raise meanwhile
    with pytest.raises(SerialMissingError, match="nothing was written"):
        handler.write_bytes(b"second")
    result = handler.write("third", safe=True)
    assert not result.success and isinstance(result.error, SerialMissingError)

    device.replug()
    _until(lambda: gaps)
    assert handler.write_line("fourth") == 7
    assert device.received == b"fourth\n"
    assert len(gaps) == 1


def test_without_auto_reopen_a_lost_port_still_raises_typed(device):
    handler = _handler(device, port="COM7")
    handler.open()
    device.unplug()
    with pytest.raises(SerialMissingError, match="Read from COM7 failed") as caught:
        handler.read_available()
    assert isinstance(caught.value, SerialError) and isinstance(caught.value, SSHError)
    assert caught.value.kind == "missing" and caught.value.__cause__ is not None
    with pytest.raises(SerialMissingError, match="Write to COM7 failed"):
        handler.write("x")
    with pytest.raises(SerialMissingError):
        list(handler.iter_lines(timeout=1))
    device.replug()                     # nothing opens it again: the handle stays dead
    with pytest.raises(SerialMissingError):
        handler.read_available()
    assert device.attempts == 1 and _reopen_threads("COM7") == []
    handler.close()
    with pytest.raises(SerialError, match="COM7 is not open"):
        handler.read_available()


def test_close_while_the_port_is_gone_stops_reopening_at_once(device, monkeypatch):
    monkeypatch.setattr(serial_handler, "REOPEN_INTERVAL", 30.0)
    handler = _handler(device, port="COM8", auto_reopen=True)
    handler.open()
    device.unplug()
    assert handler.read_available() == b""         # the reopen loop now waits 30 s
    assert len(_reopen_threads("COM8")) == 1
    took = time.perf_counter()
    handler.close()
    assert time.perf_counter() - took < 1.0        # not after the 30 s wait
    assert _reopen_threads("COM8") == []
    device.replug()
    assert device.attempts == 1 and device.open_ports() == []
    with pytest.raises(SerialError, match="COM8 is not open"):
        handler.read_available()
    with pytest.raises(SerialError, match="COM8 is not open"):
        handler.write("x")


def test_close_during_a_reopen_in_progress_leaves_the_port_closed(device):
    gaps = []
    handler = _handler(device, auto_reopen=True, on_gap=lambda s, e: gaps.append(e))
    handler.open()
    device.unplug()
    assert handler.read_available() == b""
    gate = threading.Event()
    device.replug(gate)
    _until(lambda: device.opening == 1)             # the reopen is inside Serial()
    stop = handler._stop                            # the reopen loop's stop event
    threading.Thread(target=lambda: (stop.wait(5), gate.set()), daemon=True).start()
    handler.close()                 # stops the loop, then waits for that open to end
    assert len(device.ports) == 2 and device.open_ports() == []
    assert gaps == [] and _reopen_threads("COM9") == []
    with pytest.raises(SerialError, match="not open"):
        handler.read_available()


def test_an_on_gap_exception_does_not_break_the_reader(device):
    calls, logs = [], []

    def on_gap(started, ended):
        calls.append((started, ended))
        raise RuntimeError("recorder is full")
    handler = _handler(device, auto_reopen=True, on_gap=on_gap, log_callback=logs.append)
    handler.open()
    stop = threading.Event()
    lines, errors = [], []

    def reader():
        try:
            for line in handler.iter_lines(stop_event=stop):
                lines.append(line)
        except Exception as exc:        # pragma: no cover - the test fails on it
            errors.append(exc)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    device.unplug()
    _until(lambda: device.attempts >= 2)
    device.replug()
    _until(lambda: calls)
    device.send(b"still here\n")
    _until(lambda: "still here" in lines)
    stop.set()
    thread.join(2)
    assert not thread.is_alive() and errors == []
    assert any("on_gap" in m and "recorder is full" in m for m in logs)


# ------------------------------------------------------------------ threads
def test_a_write_does_not_wait_for_a_read_waiting_for_data(device):
    handler = _handler(device, timeout=10.0, auto_reopen=True)
    handler.open()
    outcome = []

    def reader():
        try:
            outcome.append(handler.read_available())
        except Exception as exc:
            outcome.append(exc)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    assert device.read_entered.wait(2)
    assert handler.write_line("ping") == 5          # while the read still waits
    assert device.reading == 1 and device.received == b"ping\n"
    handler.close()                                 # ends the blocked read
    thread.join(2)
    assert not thread.is_alive()
    # closed on purpose: a plain SerialError, not "missing", and no reopening
    assert type(outcome[0]) is SerialError and outcome[0].kind == "error"
    assert device.attempts == 1 and _reopen_threads("COM9") == []


def test_reads_and_writes_from_several_threads_across_a_reopen(device):
    """Two readers and two writers at once, with an unplug in the middle. At
    1200 baud with a 1 s write timeout a write goes out in 15-byte pieces, so
    each line below takes several: without the write lock they interleave."""
    gaps = []
    handler = _handler(device, baudrate=1200, write_timeout=1.0, timeout=0.01,
                       auto_reopen=True, on_gap=lambda s, e: gaps.append(e))
    handler.open()
    stop, done = threading.Event(), threading.Event()
    errors, got = [], []
    sent = {0: [], 1: []}
    missing = {0: 0, 1: 0}

    def talker():                       # the device prints something now and then
        while not stop.wait(0.002):
            device.send(b"tick\n")

    def reader():
        try:
            while not stop.is_set():
                got.append(handler.read_available())
        except Exception as exc:        # pragma: no cover - the test fails on it
            errors.append(exc)

    def writer(n):
        try:
            i = 0
            while not done.is_set():
                line = f"writer {n} line {i:05d} {'x' * 20}\n"
                while True:
                    try:
                        handler.write(line)
                        break
                    except SerialMissingError:      # gone: the caller waits, retries
                        missing[n] += 1
                        time.sleep(0.005)
                sent[n].append(line)
                i += 1
                time.sleep(0.001)
        except Exception as exc:        # pragma: no cover - the test fails on it
            errors.append(exc)
    threads = ([threading.Thread(target=reader, daemon=True) for _ in range(2)]
               + [threading.Thread(target=talker, daemon=True)]
               + [threading.Thread(target=writer, args=(n,), daemon=True) for n in (0, 1)])
    for thread in threads:
        thread.start()
    _until(lambda: len(sent[0]) >= 8 and len(sent[1]) >= 8)
    device.unplug()
    _until(lambda: missing[0] + missing[1] >= 1 and device.attempts >= 3)
    device.replug()
    _until(lambda: gaps)
    before = {n: len(sent[n]) for n in (0, 1)}
    _until(lambda: all(len(sent[n]) >= before[n] + 8 for n in (0, 1)))
    done.set()
    for thread in threads[3:]:
        thread.join(5)
    stop.set()
    for thread in threads[:3]:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)

    assert errors == [] and device.overlaps == [] and len(gaps) == 1
    text = device.received.decode()
    for n in (0, 1):                    # every write that returned arrived whole, in order
        at = 0
        for line in sent[n]:
            found = text.find(line, at)
            assert found >= 0, line
            at = found + len(line)
    assert b"tick\n" in b"".join(got)


# ------------------------------------------------------------------ typed open errors
WIN = "could not open port 'COM9': "


@pytest.mark.parametrize("exc, kind", [
    (serial.SerialException(WIN + "PermissionError(13, 'Access is denied.', None, 5)"), "busy"),
    (serial.SerialException(WIN + "PermissionError(13, 'Zugriff verweigert', None, 5)"), "busy"),
    (serial.SerialException("Zugriff verweigert"), "busy"),
    (serial.SerialException("Access is denied."), "busy"),
    (serial.SerialException(16, "could not open port /dev/ttyUSB0: [Errno 16] Device or "
                                "resource busy: '/dev/ttyUSB0'"), "busy"),
    (serial.SerialException(11, "Could not exclusively lock port /dev/ttyUSB0: [Errno 11] "
                                "Resource temporarily unavailable"), "busy"),
    # access denied: another program on Windows, a permission on Linux / macOS
    (PermissionError(13, "Permission denied"), "busy" if os.name == "nt" else "error"),
    (serial.SerialException(WIN + "FileNotFoundError(2, 'The system cannot find the file "
                                  "specified.', None, 2)"), "missing"),
    (serial.SerialException(WIN + "FileNotFoundError(2, 'Das System kann die angegebene "
                                  "Datei nicht finden.', None, 2)"), "missing"),
    (serial.SerialException("Das System kann die angegebene Datei nicht finden."), "missing"),
    (serial.SerialException("The system cannot find the file specified."), "missing"),
    (serial.SerialException(2, "could not open port /dev/ttyUSB9: [Errno 2] No such file or "
                               "directory: '/dev/ttyUSB9'"), "missing"),
    (serial.SerialException(WIN + "OSError(22, 'A device which does not exist was "
                                  "specified.', None, 433)"), "missing"),
    (serial.SerialException(WIN + "OSError(22, 'Das Gerät ist nicht angeschlossen.', "
                                  "None, 1167)"), "missing"),
    (serial.SerialException("device not connected"), "missing"),
    (FileNotFoundError(2, "No such file or directory"), "missing"),
    (serial.SerialException(WIN.rstrip(": ")), "error"),
    (ValueError("Invalid baud rate: -1"), "error"),
    # Linux "Permission denied" (not in the dialout group): closing others won't help
    (serial.SerialException(13, "could not open port /dev/ttyUSB0: [Errno 13] Permission "
                                "denied: '/dev/ttyUSB0'"), "error"),
], ids=lambda value: value if isinstance(value, str) else None)
def test_open_errors_say_busy_missing_or_error(device, exc, kind):
    device.open_error = exc
    handler = _handler(device)
    with pytest.raises(SerialError) as caught:
        handler.open()
    error = caught.value
    expected = {"busy": SerialBusyError, "missing": SerialMissingError, "error": SerialError}
    assert type(error) is expected[kind] and error.kind == kind
    assert isinstance(error, SSHError) and error.__cause__ is exc
    assert "COM9" in str(error) and str(exc) in str(error)
    result = handler.open(safe=True)
    assert not result.success and type(result.error) is expected[kind]


def test_every_serial_error_has_a_kind():
    assert SerialError("x").kind == "error"
    assert SerialBusyError("x").kind == "busy" and SerialMissingError("x").kind == "missing"
    assert issubclass(SerialBusyError, SerialError) and issubclass(SerialMissingError, SerialError)
    assert SerialError("x", kind="missing").kind == "missing"
    error = SerialError("x")
    error.kind = "busy"
    assert error.kind == "busy" and SerialError.kind == "error" and str(error) == "x"


def test_a_port_that_opens_keeps_todays_behaviour(device):
    """auto_reopen and on_gap default to off: the settings reach pyserial as
    before, and nothing else starts."""
    handler = _handler(device)
    assert handler.auto_reopen is False and handler.on_gap is None
    with handler:
        assert handler.write("hi") == 2
        device.send(b"ok\n")
        assert list(handler.iter_lines(timeout=0.3)) == ["ok"]
    assert device.open_ports() == [] and _reopen_threads("COM9") == []
