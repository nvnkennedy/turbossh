"""Reader thread for interactive terminals.

A background thread pumps raw bytes from an SSH channel / serial port into a
capped buffer; the terminal widget (see :mod:`turbossh.gui.vt100`) drains it on a
timer, so a flood of output never blocks the UI thread.
"""

from __future__ import annotations

import threading

from PyQt5.QtCore import QThread, pyqtSignal


class ReaderThread(QThread):
    """Pumps raw bytes from a channel/port into a capped buffer. The GUI pulls
    from it on a timer (pull()), so a flood of output never blocks the UI thread.
    """
    closed = pyqtSignal()

    def __init__(self, read_fn, encoding="utf-8", cap=4 * 1024 * 1024):
        super().__init__()
        self._read = read_fn          # callable() -> bytes (b"" idle, None EOF)
        self._alive = True
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._cap = cap

    def run(self):
        while self._alive:
            try:
                chunk = self._read()
            except Exception:
                break
            if chunk is None:
                break
            if chunk:
                with self._lock:
                    self._buf += chunk
                    if len(self._buf) > self._cap:    # keep only the latest data
                        del self._buf[:-self._cap]
            else:
                self.msleep(15)
        self.closed.emit()

    def pull(self, maxn=262144) -> bytes:
        """Return up to maxn buffered bytes (and remove them). Called by the GUI."""
        with self._lock:
            if not self._buf:
                return b""
            data = bytes(self._buf[:maxn])
            del self._buf[:maxn]
            return data

    def flush(self):
        """Drop any buffered-but-not-yet-shown bytes. Called on Ctrl-C so a flood
        of output (slog2info -w etc.) stops draining immediately instead of
        trickling out for seconds after the command was interrupted."""
        with self._lock:
            self._buf.clear()

    def stop(self):
        self._alive = False
