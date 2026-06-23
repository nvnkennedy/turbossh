"""
Serial / COM-port handler with the same streaming + match + save-to-file model
as the SSH side. Built on pyserial.

    pip install "turbossh[serial]"     # pulls in pyserial

Typical use: watch a device's serial console live, match patterns (boot
complete, errors, prompts), send commands, and tee everything to a log file.
"""

from __future__ import annotations

import re
import time
import logging
from typing import Callable, Optional

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
        raise SerialError('pyserial is required. Install: pip install "turbossh[serial]"')
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
                'pyserial is required for serial support. '
                'Install: pip install "turbossh[serial]"')
        self.port = port
        self.baudrate = baudrate
        self._kw = dict(bytesize=bytesize, parity=parity, stopbits=stopbits,
                        timeout=timeout, rtscts=rtscts, xonxoff=xonxoff)
        self._safe_default = safe
        self._log_callback = log_callback
        self.log = logger or logging.getLogger(f"serial_handler.{port}")
        if not self.log.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            self.log.addHandler(h)
        self.log.setLevel(logging.WARNING if quiet else logging.INFO)
        self._ser = None

    def _emit(self, level, msg):
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
            self._ser = serial.Serial(self.port, self.baudrate, **self._kw)
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
    def write(self, data: str, *, safe=None):
        """Write raw text (no newline added)."""
        return self._guard("write",
                           lambda: self._require().write(data.encode("utf-8", "replace")),
                           safe=safe)

    def write_line(self, data: str, *, eol: str = "\n", safe=None):
        """Write a line (with EOL). Use eol='\\r\\n' for many embedded consoles."""
        return self._guard("write_line",
                           lambda: self._require().write((data + eol).encode("utf-8",
                                                                              "replace")),
                           safe=safe)

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
            chunk = ser.readline()           # returns b"" on read timeout
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
            fh = open(save_to, "a" if append else "w", encoding=encoding) \
                if save_to else None
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
