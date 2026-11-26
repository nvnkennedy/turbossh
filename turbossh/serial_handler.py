"""
Serial / COM-port handler with the same streaming + match + save-to-file model
as the SSH side. Built on pyserial.

    pip install pyserial               # a required dependency of turbossh

Typical use: watch a device's serial console live, match patterns (boot
complete, errors, prompts), send commands, and tee everything to a log file.
"""

from __future__ import annotations

import errno
import os
import re
import threading
import time
import logging
from typing import Callable, Optional

from . import _logging
from .exceptions import SSHError
from .results import OperationResult

try:
    import serial
    from serial.tools import list_ports
    _HAS_SERIAL = True
except Exception:  # pragma: no cover
    serial = None
    list_ports = None
    _HAS_SERIAL = False


class SerialError(SSHError):
    """A serial-port operation failed.

    ``kind`` says what went wrong, for code that reacts to it: ``"busy"``
    (another program has the port open: SerialBusyError), ``"missing"`` (there
    is no such port, or it went away: SerialMissingError) or ``"error"``
    (anything else). It is a class attribute, so each subclass has its own,
    and it can be set on an instance (``SerialError(msg, kind=...)``, or by
    assignment)."""

    kind = "error"

    def __init__(self, *args, kind: Optional[str] = None):
        super().__init__(*args)
        if kind is not None:
            self.kind = kind


class SerialBusyError(SerialError):
    """The port is there, but another program has it open (a second terminal,
    a flashing tool, a serial bridge still running): close that one first."""

    kind = "busy"


class SerialMissingError(SerialError):
    """There is no such port (unplugged, never there, or another name), or an
    open port went away (a USB console unplugged, an ECU power cycle that
    drops its USB-serial adapter). With ``auto_reopen``, a write while the
    port is gone raises it too."""

    kind = "missing"


# The write timeout of a port without flow control: one that takes no data for
# this long is stalled (a wedged USB adapter), since nothing else can hold it off.
DEFAULT_WRITE_TIMEOUT = 5.0

# How often auto_reopen tries to open a port that went away, in seconds.
REOPEN_INTERVAL = 1.0
# While the port is gone, iter_lines waits at most this long at a time, so it
# still notices stop_event and its own timeout on a port without a read timeout.
_GAP_POLL = 0.25
# How long close() waits for a reopen attempt in progress to give the port up.
_CLOSE_WAIT = 2.0

# How open() tells "in use" from "not there". pyserial words its errors as the
# OS does; on Windows it puts the repr of the Windows error in its text
# ("PermissionError(13, 'Access is denied.', None, 5)"), whose class name and
# trailing error number do not depend on the display language, so those come
# first and the phrases are the fallback for messages without them. On Linux,
# EACCES ("Permission denied": not in the dialout group) is not "busy": it
# stays an "error", since closing other programs would not help.
_BUSY_WINERRORS = frozenset({5, 32})                # access denied, sharing violation
_MISSING_WINERRORS = frozenset({2, 3, 433, 1167})   # file / path not found, no such
#                                                     device, device not connected
_BUSY_ERRNOS = frozenset({errno.EBUSY})
_MISSING_ERRNOS = frozenset({errno.ENOENT, errno.ENODEV, errno.ENXIO})
_BUSY_TEXT = ("permissionerror(", "access is denied", "zugriff verweigert",
              "device or resource busy", "could not exclusively lock port")
_MISSING_TEXT = ("filenotfounderror(", "the system cannot find the file specified",
                 "das system kann die angegebene datei nicht finden",
                 "no such file or directory", "no such device",
                 "device not connected", "device is not connected",
                 "a device which does not exist was specified")
_WINERROR_IN_TEXT = re.compile(r",\s*None,\s*(\d+)\)")


def _error_kind(exc: BaseException) -> str:
    """"busy", "missing" or "error" for an exception from opening a port: what
    the exception (or one it was raised from) is, its Windows error or errno,
    and failing those, the words of its message."""
    chain, seen = [], exc
    while seen is not None and len(chain) < 8 and not any(seen is e for e in chain):
        chain.append(seen)
        seen = seen.__cause__ or seen.__context__
    for e in chain:
        # Windows: access denied is the port held by another program; on
        # Linux / macOS it is a permission (the dialout group), no "busy"
        if isinstance(e, PermissionError) and (os.name == "nt" or
                                               getattr(e, "winerror", None)):
            return "busy"
        if isinstance(e, FileNotFoundError):
            return "missing"
        winerror, code = getattr(e, "winerror", None), getattr(e, "errno", None)
        if winerror in _BUSY_WINERRORS or code in _BUSY_ERRNOS:
            return "busy"
        if winerror in _MISSING_WINERRORS or code in _MISSING_ERRNOS:
            return "missing"
    text = " ".join(str(e) for e in chain)
    for found in _WINERROR_IN_TEXT.findall(text):
        if int(found) in _BUSY_WINERRORS:
            return "busy"
        if int(found) in _MISSING_WINERRORS:
            return "missing"
    text = text.lower()
    if any(phrase in text for phrase in _BUSY_TEXT):
        return "busy"
    if any(phrase in text for phrase in _MISSING_TEXT):
        return "missing"
    return "error"


def _open_error(port: str, exc: BaseException) -> SerialError:
    """The SerialError for a failed open: SerialBusyError, SerialMissingError
    or a plain SerialError, with the port and the OS's own words."""
    kind = _error_kind(exc)
    if kind == "busy":
        return SerialBusyError(f"Could not open {port} (in use by another program?): {exc}")
    if kind == "missing":
        return SerialMissingError(
            f"Could not open {port} (no such port: unplugged, or another name?): {exc}")
    return SerialError(f"Could not open {port}: {exc}")


class _Auto:
    """``write_timeout``'s default: DEFAULT_WRITE_TIMEOUT, or none at all (wait
    as long as it takes, as pyserial does) when flow control is on."""

    def __repr__(self):
        return "auto"


_AUTO = _Auto()


def list_serial_ports() -> list[dict]:
    """Return available COM ports: [{'device','description','hwid'}, ...]."""
    if not _HAS_SERIAL:
        raise SerialError("pyserial is required. Install it with: pip install pyserial")
    return [{"device": p.device, "description": p.description, "hwid": p.hwid}
            for p in list_ports.comports()]


class SerialHandler:
    """
    >>> with SerialHandler("COM5", baudrate=115200) as s:
    ...     s.write_line("help")
    ...     res = s.stream(match="login:", stop_on_match=True,
    ...                    save_to="console.log", on_line=print)

    ``write_timeout`` is how long a write may go without a byte going out
    before it fails with SerialError (which says how many bytes were sent). A
    slow link that keeps moving is waited for, however long the whole write
    takes. By default it is 5 s on a port without flow control, where only a
    stalled adapter stops the data, and none (wait as long as it takes) with
    ``rtscts``, ``xonxoff`` or ``dsrdtr``, where a busy device (a flash erase)
    may hold the port off for longer. ``write_timeout=None`` always waits —
    also the choice for a USB virtual COM port (CDC-ACM: many ECU and gadget
    consoles), which holds writes back without flow control while the device
    is busy or has not opened its side yet.
    (Windows reports how much of an interrupted write went out; elsewhere a
    write also fails when the rate drops more than eightfold all at once.)

    open() raises SerialBusyError when another program has the port open,
    SerialMissingError when there is no such port, and SerialError otherwise
    (``kind`` is "busy", "missing" or "error"); the OS's error is chained.

    ``auto_reopen=True`` rides out a port that goes away while open (a USB
    console unplugged, an ECU power cycle that drops its USB-serial adapter):
    the dead handle is closed and the same port is opened again, with the
    same settings, every REOPEN_INTERVAL (1 s) until it is back or close() is
    called. Meanwhile reads do not raise: read_available returns b"" after its
    usual timeout, and iter_lines / stream keep going (and still stop on
    ``stop_event`` or their ``timeout``). A write raises SerialMissingError:
    the caller decides whether to wait and send it again. When the port is
    back, ``on_gap(started, ended)`` is called with the two time.time()
    values (on the handler's reopen thread, once the port is usable again; an
    exception from it is logged and ignored) and an INFO line says how long
    the port was gone. Without auto_reopen, a port that goes away makes reads
    and writes raise SerialMissingError, as they raised SerialError before.

    One thread may read (read_available, iter_lines, stream) while another
    writes (write, write_bytes, write_line), also across a reopen: reads take
    turns with reads and writes with writes (a write goes out whole), but a
    write never waits for a read that is waiting for data. close() may come
    from any thread at any time; a read or write blocked on the port ends.
    """

    def __init__(self, port: str, baudrate: int = 115200, *, bytesize: int = 8,
                 parity: str = "N", stopbits: int = 1, timeout: float = 1.0,
                 rtscts: bool = False, xonxoff: bool = False,
                 log_callback: Optional[Callable[[str], None]] = None,
                 logger: Optional[logging.Logger] = None, safe: bool = False,
                 quiet: bool = False, write_timeout: Optional[float] = _AUTO,
                 dsrdtr: bool = False, auto_reopen: bool = False,
                 on_gap: Optional[Callable[[float, float], None]] = None):
        if not _HAS_SERIAL:
            raise SerialError(
                "pyserial is required for serial support. "
                "Install it with: pip install pyserial")
        self.port = port
        self.baudrate = baudrate
        # write_timeout: without one, pyserial's write() blocks until every byte
        # is out, so a wedged adapter froze the caller (the GUI's UI thread, a
        # test script) with no way out. With flow control the device decides
        # when data may flow, so there the wait stays unlimited (as in 2.0.0).
        if write_timeout is _AUTO:
            write_timeout = (None if (rtscts or xonxoff or dsrdtr)
                             else DEFAULT_WRITE_TIMEOUT)
        self._kw = dict(bytesize=bytesize, parity=parity, stopbits=stopbits,
                        timeout=timeout, rtscts=rtscts, xonxoff=xonxoff,
                        dsrdtr=dsrdtr, write_timeout=write_timeout)
        self._safe_default = safe
        self._log_callback = log_callback
        self._quiet = quiet
        if logger is None:
            _logging.ensure_console_handler()
            logger = _logging.get_logger(f"serial.{port}")
        self.log = logger
        self.auto_reopen = auto_reopen
        self.on_gap = on_gap
        self._ser = None
        # _lock guards _ser and the gap state below (the reopen thread swaps
        # _ser, close() may come from any thread); it is only ever held for a
        # moment, never across a read or write. Reads take turns under
        # _read_lock and writes under _write_lock: pyserial copes with one of
        # each at once, not with two reads or two writes, and a write must not
        # wait behind a read that is blocked waiting for data.
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)    # back, or closed
        self._read_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False                # close() was called, open() not since
        self._gap_since: Optional[float] = None     # when the port went away
        self._reopener: Optional[threading.Thread] = None
        self._stop = threading.Event()      # the running reopen loop's: set to end it

    def _emit(self, level, msg):
        if not (self._quiet and level < logging.WARNING):
            self.log.log(level, msg)
        if self._log_callback:
            try:
                self._log_callback(f"[{logging.getLevelName(level)}] {msg}")
            except Exception:
                pass

    def _guard(self, action, fn, *a, safe=None, **k):
        use_safe = self._safe_default if safe is None else safe
        if not use_safe:
            return fn(*a, **k)
        try:
            return OperationResult(True, action, value=fn(*a, **k))
        except Exception as exc:
            self._emit(logging.ERROR, f"{action} failed: {exc}")
            return OperationResult(False, action, error=exc)

    # --- open / close ---
    def open(self, *, safe=None):
        def _do():
            with self._lock:                # open() while the port is gone: start over
                reopener, _ = self._end_gap()
            self._join(reopener)
            try:
                ser = serial.Serial(self.port, self.baudrate, **self._kw)
            except (serial.SerialException, OSError, ValueError) as exc:
                # pyserial's own exception is not part of our hierarchy, so
                # `except SSHError` (and the CLI) would otherwise miss it
                raise _open_error(self.port, exc) from exc
            with self._lock:
                self._ser = ser
                self._closed = False
            self._emit(logging.INFO, f"Opened {self.port} @ {self.baudrate} baud.")
            return self
        return self._guard("open", _do, safe=safe)

    def close(self):
        """Close the port; safe from any thread at any time (a read or write
        blocked on the port ends with an error). Also ends an auto_reopen wait
        for a port that went away, and an open it has in progress."""
        with self._lock:
            ser, self._ser = self._ser, None
            self._closed = True
            reopener, was_gone = self._end_gap()
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self._join(reopener)
        if ser is not None:
            self._emit(logging.INFO, f"Closed {self.port}.")
        elif was_gone:
            self._emit(logging.INFO, f"Closed {self.port} (it was gone: no longer "
                                     f"waiting for it to come back).")

    def _end_gap(self):
        """With _lock held: stop the reopen loop, if one runs, and forget the gap.
        Returns (the loop's thread or None, whether the port was gone)."""
        self._stop.set()
        self._stop = threading.Event()
        reopener, self._reopener = self._reopener, None
        was_gone, self._gap_since = self._gap_since is not None, None
        self._changed.notify_all()          # reads waiting for the port: stop waiting
        return reopener, was_gone

    @staticmethod
    def _join(reopener):
        """Let a stopped reopen loop finish an open it has in progress (and close
        what it opened), so the port is free once close() returns."""
        if reopener is not None and reopener is not threading.current_thread():
            reopener.join(_CLOSE_WAIT)

    def _current(self):
        """The open port, or None while auto_reopen waits for it to come back.
        Raises SerialError when it is not open (never opened, or closed)."""
        with self._lock:
            ser = self._ser
            if ser is not None and ser.is_open:
                return ser
            if self._gap_since is not None:
                return None
        raise SerialError(f"{self.port} is not open. Call open() first.")

    def _require(self):
        """The open port, for a write: while it is gone, SerialMissingError."""
        ser = self._current()
        if ser is None:
            since = self._gap_since
            ago = f" {max(0.0, time.time() - since):.1f} s ago" if since else ""
            raise SerialMissingError(
                f"Cannot write to {self.port}: it went away{ago} (unplugged, or "
                f"the device restarted) and is not back yet, so nothing was "
                f"written. It is opened again as soon as it is back.")
        return ser

    def _lost(self, ser, exc, what: str) -> Optional[SerialError]:
        """A read or write on *ser* failed with *exc*: the port went away, or
        close() closed it under the call. Returns the error to raise, or None
        when auto_reopen has it covered (the port is being opened again)."""
        message = f"{what}: {exc}"
        with self._lock:
            if self._closed:
                return SerialError(message)         # closed on purpose: not lost
            if not self.auto_reopen:
                return SerialMissingError(message)
            if self._ser is not ser:
                return None     # another read or write saw it first (or it is back)
            self._ser = None
            self._gap_since = time.time()
            self._reopener = threading.Thread(
                target=self._reopen, args=(ser, self._stop), daemon=True,
                name=f"turbossh-serial-reopen-{self.port}")
            # started with the lock held, so close() always finds and stops it
            self._reopener.start()
        self._emit(logging.WARNING, f"{self.port} went away ({exc}); opening it "
                                    f"again every {REOPEN_INTERVAL:g} s until it is back.")
        return None

    def _reopen(self, dead, stop):
        """The auto_reopen loop, on its own thread: give up the dead handle, then
        open the port again every REOPEN_INTERVAL until that works or close()
        (or open()) sets *stop*. Never a busy loop: a failed attempt waits."""
        try:
            dead.close()                    # also ends a write still blocked on it
        except Exception:
            pass
        while not stop.wait(max(REOPEN_INTERVAL, 0.01)):
            try:
                ser = serial.Serial(self.port, self.baudrate, **self._kw)
            except Exception as exc:        # still gone, or busy: try again
                self.log.debug(f"{self.port} is not back yet: {exc}")
                continue
            ended = time.time()
            with self._lock:
                wanted = not stop.is_set()  # close() came in while it opened?
                if wanted:
                    started = self._gap_since if self._gap_since is not None else ended
                    self._ser, self._gap_since, self._reopener = ser, None, None
                    self._changed.notify_all()
            if not wanted:
                try:
                    ser.close()
                except Exception:
                    pass
                return
            self._emit(logging.INFO, f"{self.port} is back after a {ended - started:.1f} s "
                                     f"gap; opened it again.")
            if self.on_gap is not None:
                try:
                    self.on_gap(started, ended)
                except Exception as exc:
                    self._emit(logging.WARNING, f"on_gap callback for {self.port} failed "
                                                f"(ignored): {type(exc).__name__}: {exc}")
            return

    def _read(self, op, wait: Optional[float]):
        """Run ``op(port)`` for a read, one read at a time. While auto_reopen
        waits for the port to come back, wait up to *wait* seconds for it
        (None: until it is back, or closed) and return None instead."""
        with self._read_lock:
            ser = self._current()
            if ser is not None:
                try:
                    return op(ser)
                except (serial.SerialException, OSError) as exc:
                    error = self._lost(ser, exc, f"Read from {self.port} failed "
                                                 f"(port closed or unplugged?)")
                    if error is not None:
                        raise error from exc
            with self._lock:
                self._changed.wait_for(
                    lambda: self._ser is not None or self._gap_since is None, wait)
            return None

    # --- write ---
    def _baud(self, ser) -> int:
        """The port's baud rate as a number: pyserial normalises it (it accepts
        "115200" too), so ask the port first and fall back to what was given."""
        for value in (getattr(ser, "baudrate", None), self.baudrate):
            try:
                baud = int(float(value))
            except (TypeError, ValueError):
                continue
            if baud > 0:
                return baud
        return 9600

    @staticmethod
    def _moved(ser, size: int) -> Optional[int]:
        """How many bytes of a write() that timed out went out, or None when the
        platform does not tell. pyserial's timeout exception leaves the count
        out; on Windows it stays in the write's OVERLAPPED structure, which is
        where GetOverlappedResult read it from."""
        try:
            moved = int(getattr(ser, "_overlapped_write").InternalHigh)
        except (AttributeError, TypeError, ValueError):
            return None
        return moved if 0 <= moved < size else None

    def _write(self, payload: bytes) -> int:
        with self._write_lock:      # one write at a time, so writes never interleave
            return self._write_to(self._require(), payload)

    def _write_failed(self, ser, exc) -> SerialError:
        """The error for a write that failed with *exc*. A write timeout is a
        port that is there but not taking data; anything else is a lost port."""
        what = f"Write to {self.port} failed"
        if isinstance(exc, serial.SerialTimeoutException):
            return SerialError(f"{what}: {exc}")   # there, but not taking data
        error = self._lost(ser, exc, what)
        if error is None:                       # auto_reopen: it is opened again
            error = SerialMissingError(f"{what}: {exc} ({self.port} went away; it is "
                                       f"opened again as soon as it is back)")
        return error

    def _write_to(self, ser, payload: bytes) -> int:
        timeout = self._kw.get("write_timeout")
        total = len(payload)
        if not timeout:     # None: wait as long as it takes (0: pyserial's non-blocking)
            try:
                n = ser.write(payload)
            except (serial.SerialException, OSError) as exc:
                raise self._write_failed(ser, exc) from exc
            return total if n is None else n
        # pyserial's timeout is a deadline for the whole write() call (Windows:
        # WriteTotalTimeoutConstant), so the payload goes in pieces that take
        # about an eighth of it at the rate the port last managed (at first the
        # baud rate, 10 bits a byte): a slow link that keeps moving never runs
        # into it. When one does run out, the bytes it moved still count; only
        # a piece that moved nothing for the whole timeout is a stall.
        first = max(1, int(self._baud(ser) / 10 * timeout / 8))
        step, sent = first, 0
        while sent < total:
            piece = payload[sent:sent + step]
            started = time.perf_counter()
            try:
                n = ser.write(piece)
            except serial.SerialTimeoutException as exc:
                moved = self._moved(ser, len(piece))
                if moved is None:
                    raise SerialError(
                        f"Write to {self.port} timed out after {timeout:g} s: "
                        f"{sent} of {total} bytes were sent, and perhaps part of "
                        f"the next {len(piece)} (the port is not taking data, or "
                        f"far more slowly than before: flow control holding it "
                        f"off, or a stalled adapter)") from exc
                sent += moved
                if moved == 0:
                    raise SerialError(
                        f"Write to {self.port} timed out after {timeout:g} s: "
                        f"{sent} of {total} bytes were sent (no byte went out "
                        f"for {timeout:g} s: flow control holding the port off, "
                        f"or a stalled adapter)") from exc
                # slow, but moving: go on in pieces this rate gets through in time
                step = max(1, moved // 8)
                continue
            except (serial.SerialException, OSError) as exc:
                raise self._write_failed(ser, exc) from exc
            done = len(piece) if n is None else n
            sent += done
            if done < len(piece):
                break                   # the write was cancelled (cancel_write)
            elapsed = time.perf_counter() - started
            if elapsed > 0:
                step = max(1, min(first, int(done / elapsed * timeout / 8)))
        return sent

    def write(self, data: str, *, safe=None):
        """Write raw text (no newline added)."""
        return self._guard("write",
                           lambda: self._write(data.encode("utf-8", "replace")),
                           safe=safe)

    def write_bytes(self, data: bytes, *, safe=None):
        """Write raw bytes exactly as given (terminal keystrokes, binary)."""
        return self._guard("write_bytes", lambda: self._write(bytes(data)), safe=safe)

    def write_line(self, data: str, *, eol: str = "\n", safe=None):
        """Write a line (with EOL). Use eol='\\r\\n' for many embedded consoles."""
        return self._guard("write_line",
                           lambda: self._write((data + eol).encode("utf-8", "replace")),
                           safe=safe)

    # --- raw read (interactive terminals) ---
    def read_available(self, max_bytes: int = 65536) -> bytes:
        """Return whatever has arrived, as soon as it arrives: block (up to the
        port timeout) for the first byte only, then take what is waiting.

        ``Serial.read(n)`` on its own waits for *n* bytes or the full timeout, so
        asking for a big buffer made every keystroke echo late by that timeout.
        Returns ``b""`` when nothing arrived in time, and (with auto_reopen)
        while the port is gone, after waiting the same timeout for it."""
        def op(ser):
            waiting = ser.in_waiting
            return ser.read(min(max(waiting, 1), max_bytes))
        data = self._read(op, self._kw.get("timeout"))
        return b"" if data is None else data

    # --- streaming read (line by line, live) ---
    def iter_lines(self, *, stop_event=None, timeout: Optional[float] = None,
                   encoding: str = "utf-8"):
        """
        Yield serial console lines live. Stop via break, ``stop_event``, or
        ``timeout`` seconds. Mirrors SSHHandler.iter_lines. With auto_reopen it
        keeps going while the port is gone (a line cut off by the gap is
        yielded as it was, not glued to what comes after it).
        """
        def readline(ser):
            return ser.readline()            # returns b"" on read timeout

        self._current()                      # not open: raise now, as before
        port_timeout = self._kw.get("timeout")
        start = time.time()
        buf = b""
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if timeout and (time.time() - start) > timeout:
                break
            # while the port is gone, wait in short steps: stop_event and the
            # timeout still count, even on a port without a read timeout
            wait = min(port_timeout or _GAP_POLL, _GAP_POLL)
            if timeout:
                wait = max(0.0, min(wait, timeout - (time.time() - start)))
            chunk = self._read(readline, wait)
            if chunk is None:                # the port is gone for now
                if buf:
                    yield buf.decode(encoding, errors="replace").rstrip("\r")
                    buf = b""
                continue
            if chunk:
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                for ln in parts:
                    yield ln.decode(encoding, errors="replace").rstrip("\r")
        if buf:
            yield buf.decode(encoding, errors="replace").rstrip("\r")

    def stream(self, *, on_line=None, on_match=None, match=None,
               stop_on_match: bool = False, save_to: Optional[str] = None,
               append: bool = True, clean: bool = True,
               timeout: Optional[float] = None, stop_event=None,
               encoding: str = "utf-8", safe=None):
        """
        Read the serial console continuously with built-in matching + file
        logging. Same signature/semantics as SSHHandler.stream. ``clean=True``
        strips ANSI escape codes and control chars from each line.
        """
        from .results import strip_ansi

        def _do():
            pat = re.compile(match) if isinstance(match, str) else match
            matches, count = [], 0
            fh = None
            if save_to:
                target = os.path.abspath(os.path.expanduser(save_to))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                fh = open(target, "a" if append else "w", encoding=encoding)
            try:
                for line in self.iter_lines(stop_event=stop_event, timeout=timeout,
                                            encoding=encoding):
                    if clean:
                        line = strip_ansi(line)
                    count += 1
                    if fh:
                        fh.write(line + "\n")
                        fh.flush()
                    if on_line:
                        on_line(line)
                    if pat and pat.search(line):
                        matches.append(line)
                        if on_match:
                            on_match(line)
                        if stop_on_match:
                            break
                return {"lines": count, "matches": matches,
                        "matched": bool(matches), "saved_to": save_to}
            finally:
                if fh:
                    fh.close()
        return self._guard("stream", _do, safe=safe)

    def __enter__(self):
        r = self.open()
        if isinstance(r, OperationResult) and not r.success:
            raise r.error or SerialError("open failed")
        return self

    def __exit__(self, *exc):
        self.close()


def serial_available() -> bool:
    return _HAS_SERIAL
