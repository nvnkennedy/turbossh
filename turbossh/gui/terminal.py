"""Interactive terminal widget for SSH shells and serial consoles.

A reader thread pumps incoming bytes into a QPlainTextEdit; key presses are
translated to bytes and sent to the channel/port. ANSI escape sequences are
stripped for readability (this is a clean line/stream terminal, not a full
VT100 — full-screen TUIs like vim/htop won't render perfectly)."""

from __future__ import annotations

import threading

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QFont, QTextCursor
from PyQt5.QtWidgets import QPlainTextEdit

from ..results import strip_ansi


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


_CTRL = {Qt.Key_C: b"\x03", Qt.Key_D: b"\x04", Qt.Key_Z: b"\x1a",
         Qt.Key_A: b"\x01", Qt.Key_E: b"\x05", Qt.Key_K: b"\x0b",
         Qt.Key_L: b"\x0c", Qt.Key_U: b"\x15", Qt.Key_W: b"\x17"}

_KEYS = {Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r", Qt.Key_Backspace: b"\x7f",
         Qt.Key_Tab: b"\t", Qt.Key_Escape: b"\x1b",
         Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
         Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
         Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F",
         Qt.Key_PageUp: b"\x1b[5~", Qt.Key_PageDown: b"\x1b[6~",
         Qt.Key_Delete: b"\x1b[3~"}


class TerminalView(QPlainTextEdit):
    """Displays remote output and forwards keystrokes via ``send_fn(bytes)``."""

    def __init__(self, send_fn, parent=None):
        super().__init__(parent)
        self._send = send_fn
        self.setFont(QFont("Consolas", 10))
        self.setMaximumBlockCount(100000)
        self.setStyleSheet("background:#06101d; color:#cfe3f7;")
        self.setUndoRedoEnabled(False)

    def feed(self, text: str):
        text = strip_ansi(text)
        if not text:
            return
        cur = self.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def keyPressEvent(self, event):
        if not self._send:
            return
        mods = event.modifiers()
        key = event.key()
        try:
            if mods & Qt.ControlModifier and key in _CTRL:
                self._send(_CTRL[key]); return
            if key in _KEYS:
                self._send(_KEYS[key]); return
            text = event.text()
            if text:
                self._send(text.encode("utf-8"))
        except Exception:
            pass

    def paste_clipboard(self):
        from PyQt5.QtWidgets import QApplication
        txt = QApplication.clipboard().text()
        if txt and self._send:
            self._send(txt.encode("utf-8"))
