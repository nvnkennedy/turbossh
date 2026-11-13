"""Reader and writer threads for interactive terminals.

A background thread pumps raw bytes from an SSH channel / serial port into a
capped buffer; the terminal widget (see :mod:`turbossh.gui.vt100`) drains it on a
timer, so a flood of output never blocks the UI thread. The other way, a
:class:`Writer` sends what is typed or pasted on a thread of its own, so a link
that does not take it at once never blocks the UI thread either.
"""

from __future__ import annotations

import threading
import time
from collections import deque

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
        self._dropped = 0             # bytes discarded since take_dropped()

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
                    over = len(self._buf) - self._cap
                    if over > 0:                      # keep only the latest data
                        del self._buf[:over]
                        self._dropped += over
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

    def take_dropped(self) -> int:
        """Bytes discarded because the backlog hit its cap, since the last call —
        so the terminal can show that output was skipped instead of hiding it."""
        with self._lock:
            n, self._dropped = self._dropped, 0
            return n

    def flush(self):
        """Drop any buffered-but-not-yet-shown bytes. Called on Ctrl-C so a flood
        of output (slog2info -w etc.) stops draining immediately instead of
        trickling out for seconds after the command was interrupted. (That is the
        user's own choice, so it is not reported as skipped output.)"""
        with self._lock:
            self._buf.clear()
            self._dropped = 0

    def stop(self):
        self._alive = False


_PASTE_START, _PASTE_END = b"\x1b[200~", b"\x1b[201~"


class Writer:
    """Sends a terminal's input — keystrokes, pastes, window-size changes — in
    order, on a thread of its own.

    It used to be written on the GUI thread, which froze the window whenever the
    other end did not take it at once: paramiko waits up to 30 s during a rekey
    (and without end on a stalled link), a serial port at a low baud rate blocks
    until every byte is out, a shell's stdin pipe is full while it runs a
    command. :meth:`interrupt` drops whatever is still queued (a terminal
    discards type-ahead on Ctrl-C too) and puts Ctrl-C first, so it never waits
    behind a long paste.

    ``write_fn(bytes)`` returns how many bytes it took (``None``: all of them;
    ``0``: none yet, try again). It may raise: the error goes to
    ``on_error(exc)`` and the rest of that input is dropped. ``on_error`` is
    called on the writer thread — route it through a
    :class:`~turbossh.gui.qtutil.SignalGate`.
    """

    def __init__(self, write_fn, on_error=None, *, chunk: int = 4096,
                 stall_timeout: float = 60.0, name: str = "term-writer"):
        self._write = write_fn
        self._on_error = on_error
        self._chunk = max(1, int(chunk))
        self._stall = stall_timeout
        self._queue: deque = deque()      # bytes, or (key, callable)
        self._cond = threading.Condition()
        self._epoch = 0                   # bumped by interrupt(): abandon the current input
        self._closed = False
        self._last_error = ("", 0.0)
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    # ---- the GUI side (never blocks) ----
    def send(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            if not self._closed:
                self._queue.append(bytes(data))
                self._cond.notify()

    def call(self, fn, key=None) -> None:
        """Run ``fn()`` on the writer thread, in order with the input (a PTY
        resize goes through the same link). A still-queued call with the same
        *key* is replaced instead: only the latest window size matters."""
        with self._cond:
            if self._closed:
                return
            if key is not None:
                for i, item in enumerate(self._queue):
                    if type(item) is tuple and item[0] == key:
                        self._queue[i] = (key, fn)
                        return
            self._queue.append((key, fn))
            self._cond.notify()

    def interrupt(self, data=b"\x03") -> int:
        """Drop the input still queued (and the rest of what is being sent), then
        send *data* (Ctrl-C; None: nothing) before anything else. Queued calls
        are kept. Returns how many bytes were dropped."""
        with self._cond:
            dropped = sum(len(item) for item in self._queue if type(item) is bytes)
            self._queue = deque(item for item in self._queue if type(item) is tuple)
            self._epoch += 1
            if data and not self._closed:
                self._queue.appendleft(bytes(data))
            self._cond.notify()
            return dropped

    def close(self) -> None:
        """Stop: nothing more is sent. Does not wait (a write in progress ends
        on its own, or when its link is closed)."""
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._epoch += 1
            self._cond.notify()

    @property
    def pending(self) -> int:
        """Bytes queued and not yet handed to ``write_fn``."""
        with self._cond:
            return sum(len(item) for item in self._queue if type(item) is bytes)

    @property
    def closed(self) -> bool:
        return self._closed

    # ---- the writer thread ----
    def _run(self):
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                item = self._queue.popleft()
                epoch = self._epoch
            try:
                if type(item) is tuple:
                    item[1]()
                else:
                    self._send_all(item, epoch)
            except Exception as exc:       # noqa: BLE001 - reported, then go on
                self._report(exc)

    def _send_all(self, data: bytes, epoch: int):
        view = memoryview(data)
        stalled = None
        while view:
            if self._epoch != epoch:       # interrupted: the rest is dropped
                self._end_paste(data, view)
                return
            n = self._write(bytes(view[:self._chunk]))
            if n is None:
                n = min(len(view), self._chunk)
            if n > 0:
                view = view[n:]
                stalled = None
                continue
            # nothing taken (a full window on a slow link): try again, until
            # the other end has taken nothing at all for stall_timeout
            now = time.monotonic()
            if stalled is None:
                stalled = now
            elif now - stalled > self._stall:
                raise TimeoutError(f"the other end took no input for "
                                   f"{self._stall:.0f} s")
            time.sleep(0.005)

    def _end_paste(self, data: bytes, rest: memoryview):
        """A bracketed paste cut short still gets its end marker, or the program
        would take everything typed afterwards as part of the paste."""
        if (self._closed or not data.startswith(_PASTE_START)
                or not data.endswith(_PASTE_END)
                or len(data) - len(rest) < len(_PASTE_START)):
            return
        tail = bytes(rest) if len(rest) <= len(_PASTE_END) else _PASTE_END
        try:
            self._write(tail)
        except Exception:
            pass

    def _report(self, exc):
        if self._closed or self._on_error is None:
            return
        # a dead link fails every keystroke the same way: say so once in a while
        text, when = self._last_error
        now = time.monotonic()
        if str(exc) == text and now - when < 5.0:
            return
        self._last_error = (str(exc), now)
        try:
            self._on_error(exc)
        except Exception:
            pass
