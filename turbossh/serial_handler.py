"""
Serial / COM-port handler with the same streaming + match + save-to-file model
as the SSH side. Built on pyserial.

    pip install pyserial               # a required dependency of turbossh

Typical use: watch a device's serial console live, match patterns (boot
complete, errors, prompts), send commands, and tee everything to a log file.
"""

from __future__ import annotations

import os
import re
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
    """A serial-port operation failed."""


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
    """

    def __init__(self, port: str, baudrate: int = 115200, *, bytesize: int = 8,
                 parity: str = "N", stopbits: int = 1, timeout: float = 1.0,
                 rtscts: bool = False, xonxoff: bool = False,
                 log_callback: Optional[Callable[[str], None]] = None,
                 logger: Optional[logging.Logger] = None, safe: bool = False,
                 quiet: bool = False):
        if not _HAS_SERIAL:
            raise SerialError(
                "pyserial is required for serial support. "
                "Install it with: pip install pyserial")
        self.port = port
        self.baudrate = baudrate
        self._kw = dict(bytesize=bytesize, parity=parity, stopbits=stopbits,
                        timeout=timeout, rtscts=rtscts, xonxoff=xonxoff)
        self._safe_default = safe
        self._log_callback = log_callback
        self._quiet = quiet
        if logger is None:
            _logging.ensure_console_handler()
            logger = _logging.get_logger(f"serial.{port}")
        self.log = logger
        self._ser = None

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
            try:
                self._ser = serial.Serial(self.port, self.baudrate, **self._kw)
            except (serial.SerialException, OSError, ValueError) as exc:
                # pyserial's own exception is not part of our hierarchy, so
                # `except SSHError` (and the CLI) would otherwise miss it
                raise SerialError(f"Could not open {self.port}: {exc}") from exc
            self._emit(logging.INFO, f"Opened {self.port} @ {self.baudrate} baud.")
            return self
        return self._guard("open", _do, safe=safe)

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._emit(logging.INFO, f"Closed {self.port}.")
            self._ser = None

    def _require(self):
        if self._ser is None or not self._ser.is_open:
            raise SerialError(f"{self.port} is not open. Call open() first.")
        return self._ser

    # --- write ---
    def _write(self, payload: bytes) -> int:
        try:
            return self._require().write(payload)
        except (serial.SerialException, OSError) as exc:
            raise SerialError(f"Write to {self.port} failed: {exc}") from exc

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
        Returns ``b""`` when nothing arrived in time."""
        ser = self._require()
        try:
            waiting = ser.in_waiting
            return ser.read(min(max(waiting, 1), max_bytes))
        except (serial.SerialException, OSError) as exc:
            raise SerialError(f"Read from {self.port} failed "
                              f"(port closed or unplugged?): {exc}") from exc

    # --- streaming read (line by line, live) ---
    def iter_lines(self, *, stop_event=None, timeout: Optional[float] = None,
                   encoding: str = "utf-8"):
        """
        Yield serial console lines live. Stop via break, ``stop_event``, or
        ``timeout`` seconds. Mirrors SSHHandler.iter_lines.
        """
        ser = self._require()
        start = time.time()
        buf = b""
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if timeout and (time.time() - start) > timeout:
                break
            try:
                chunk = ser.readline()       # returns b"" on read timeout
            except (serial.SerialException, OSError) as exc:
                raise SerialError(f"Read from {self.port} failed "
                                  f"(port closed or unplugged?): {exc}") from exc
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
