"""True VT100/ANSI terminal widget backed by pyte, so full-screen apps
(htop, vim, top, less, nano) render correctly. A character grid is painted
cell-by-cell with colors, bold, reverse video, and a block cursor."""

from __future__ import annotations

import os
import pyte
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QFontMetrics, QPainter, QColor
from PyQt5.QtWidgets import QWidget, QApplication, QMenu, QFileDialog

# 16-color ANSI palette (xterm-ish), on a dark background
_PALETTE = {
    "black": "#1c1c1c", "red": "#e25c52", "green": "#5fd38a",
    "brown": "#d4b86a", "yellow": "#d4b86a", "blue": "#4f9bea",
    "magenta": "#c678dd", "cyan": "#56c5d0", "white": "#cfd8e3",
    "default": "#cfd8e3",
}
_BRIGHT = {
    "black": "#5c6370", "red": "#ff7a6e", "green": "#7ee2a4",
    "brown": "#ffd479", "yellow": "#ffd479", "blue": "#6fb3ff",
    "magenta": "#d79bf0", "cyan": "#74dbe6", "white": "#ffffff",
    "default": "#ffffff",
}
_BG_DEFAULT = "#000000"


def _color(name, bright=False):
    table = _BRIGHT if bright else _PALETTE
    if name in table:
        return QColor(table[name])
    if isinstance(name, str) and len(name) == 6:        # pyte gives hex sometimes
        try:
            return QColor("#" + name)
        except Exception:
            pass
    return QColor(_PALETTE["default"])


class Vt100Terminal(QWidget):
    """Renders a pyte screen; forwards keystrokes via ``send_fn(bytes)``.
    Emits ``resized(cols, rows)`` so the caller can resize the remote PTY."""

    resized = pyqtSignal(int, int)

    _CTRL = {Qt.Key_C: b"\x03", Qt.Key_D: b"\x04", Qt.Key_Z: b"\x1a",
             Qt.Key_A: b"\x01", Qt.Key_E: b"\x05", Qt.Key_K: b"\x0b",
             Qt.Key_L: b"\x0c", Qt.Key_U: b"\x15", Qt.Key_W: b"\x17",
             Qt.Key_R: b"\x12"}
    _KEYS = {Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r", Qt.Key_Backspace: b"\x7f",
             Qt.Key_Tab: b"\t", Qt.Key_Escape: b"\x1b",
             Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
             Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
             Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F",
             Qt.Key_PageUp: b"\x1b[5~", Qt.Key_PageDown: b"\x1b[6~",
             Qt.Key_Delete: b"\x1b[3~", Qt.Key_F1: b"\x1bOP", Qt.Key_F2: b"\x1bOQ",
             Qt.Key_F3: b"\x1bOR", Qt.Key_F4: b"\x1bOS"}

    def __init__(self, send_fn, cols=120, rows=34, parent=None):
        super().__init__(parent)
        self._send = send_fn
        self.on_interrupt = None        # optional hook called on Ctrl-C (flush)
        self.cols, self.rows = cols, rows
        try:
            from . import settings as _s
            scrollback = int(_s.get("term_scrollback") or 10000)
        except Exception:
            scrollback = 10000
        # In-widget scrollback is memory-bound (pyte's VT100 cell grid costs
        # ~16 KB/line), so it's capped — but the FULL session is teed to disk
        # (see _open_capture), so "Save all output" is effectively unlimited
        # (hundreds of thousands / millions of lines).
        scrollback = max(2000, min(scrollback, 50000))
        self.screen = pyte.HistoryScreen(cols, rows, history=scrollback, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        self._following = True          # auto-stick to the bottom (live tail)
        self._cap_fh = None             # disk tee of everything ever printed
        self._cap_path = None
        self._open_capture()

        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.IBeamCursor)
        try:
            from . import settings as _settings
            fam = _settings.get("term_font") or "Consolas"
            size = int(_settings.get("term_font_size") or 10)
        except Exception:
            fam, size = "Consolas", 10
        self.font = QFont(fam, size)
        self.font.setStyleHint(QFont.Monospace)
        fm = QFontMetrics(self.font)
        self.cw = max(1, fm.horizontalAdvance("M"))
        self.ch = fm.height()
        self._ascent = fm.ascent()
        self.setStyleSheet(f"background:{_BG_DEFAULT};")

        # drain the reader buffer + repaint at ~30fps, with a bounded amount of
        # work per tick so a flood of output (journalctl/slog2info) never freezes
        self._dirty = False
        self._pull = None
        self._timer = QTimer(self); self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def set_source(self, pull_fn):
        """Provide a callable() -> bytes that the timer drains each tick."""
        self._pull = pull_fn

    # ---- full-session capture to disk (unlimited; never grows RAM) ----
    def _open_capture(self):
        try:
            import time
            capdir = os.path.join(os.path.expanduser("~"), ".turbossh", "captures")
            os.makedirs(capdir, exist_ok=True)
            # best-effort sweep of stale captures (>1 day old) so they don't pile up
            now = time.time()
            for f in os.listdir(capdir):
                p = os.path.join(capdir, f)
                try:
                    if f.startswith("term-") and now - os.path.getmtime(p) > 86400:
                        os.remove(p)
                except Exception:
                    pass
            self._cap_path = os.path.join(
                capdir, f"term-{os.getpid()}-{id(self)}.log")
            self._cap_fh = open(self._cap_path, "wb")
        except Exception:
            self._cap_fh = None

    def _capture(self, data: bytes):
        if self._cap_fh is not None:
            try:
                self._cap_fh.write(data)
            except Exception:
                pass

    def _feed(self, data: bytes):
        # if we're tailing, make sure we're at the bottom before drawing new data
        if self._following:
            self._to_bottom()
        try:
            self.stream.feed(data)
        except Exception:
            pass
        self._capture(data)
        self._dirty = True

    def feed(self, data: bytes):
        self._feed(data if isinstance(data, bytes) else data.encode())

    def _tick(self):
        if self._pull is not None:
            # bounded: at most ~256KB/tick (~7 MB/s) keeps the UI responsive
            data = self._pull(262144)
            if data:
                self._feed(data)
        if self._dirty:
            self._dirty = False
            self.update()

    # ---- scrollback ----
    def _to_bottom(self):
        """Return the view to the live tail (after the user scrolled up)."""
        try:
            hist = self.screen.history
            for _ in range(hist.size + 5):
                before = self.screen.history.position
                self.screen.next_page()
                if self.screen.history.position == before:
                    break
        except Exception:
            pass
        self._following = True

    def wheelEvent(self, event):
        try:
            steps = event.angleDelta().y() // 120 or (1 if event.angleDelta().y() > 0 else -1)
            if steps > 0:
                for _ in range(steps):
                    self.screen.prev_page()
                self._following = False
            elif steps < 0:
                for _ in range(-steps):
                    self.screen.next_page()
                if self.screen.history.position >= self.screen.history.size:
                    self._following = True
            self._dirty = True
            self.update()
        except Exception:
            pass

    # ---- sizing ----
    def resizeEvent(self, event):
        cols = max(20, self.width() // self.cw)
        rows = max(5, self.height() // self.ch)
        if (cols, rows) != (self.cols, self.rows):
            self.cols, self.rows = cols, rows
            try:
                self.screen.resize(rows, cols)
            except Exception:
                pass
            self.resized.emit(cols, rows)
        super().resizeEvent(event)

    # ---- rendering ----
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_BG_DEFAULT))
        p.setFont(self.font)
        buf = self.screen.buffer
        for y in range(self.rows):
            line = buf[y]
            for x in range(self.cols):
                cell = line[x]
                ch = cell.data or " "
                reverse = cell.reverse
                fg = _color(cell.fg, cell.bold)
                bg = QColor(_BG_DEFAULT) if cell.bg == "default" else _color(cell.bg)
                if reverse:
                    fg, bg = bg, fg
                px, py = x * self.cw, y * self.ch
                if bg.name() != _BG_DEFAULT:
                    p.fillRect(px, py, self.cw, self.ch, bg)
                if ch != " ":
                    p.setPen(fg)
                    p.drawText(px, py + self._ascent, ch)
        # block cursor
        if not self.screen.cursor.hidden:
            cx, cy = self.screen.cursor.x, self.screen.cursor.y
            if cy < self.rows and cx < self.cols:
                cur = QColor("#8fe39a"); cur.setAlpha(150)
                p.fillRect(cx * self.cw, cy * self.ch, self.cw, self.ch, cur)
        p.end()

    # ---- focus / keyboard ----
    def focusNextPrevChild(self, nxt):
        # never let Tab/Backtab move focus away — the shell needs Tab for completion
        return False

    def mousePressEvent(self, event):
        self.setFocus()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if not self._send:
            return
        if not self._following:        # typing snaps back to the live tail
            self._to_bottom(); self._dirty = True
        mods, key = event.modifiers(), event.key()
        try:
            if (mods & Qt.ControlModifier) and (mods & Qt.ShiftModifier):
                if key == Qt.Key_C:
                    self._copy(); return
                if key == Qt.Key_V:
                    self._paste(); return
            if (mods & Qt.ControlModifier) and key in self._CTRL:
                self._send(self._CTRL[key])
                if key == Qt.Key_C and self.on_interrupt:
                    try:
                        self.on_interrupt()        # drop buffered flood -> stop fast
                    except Exception:
                        pass
                return
            if key in self._KEYS:
                self._send(self._KEYS[key]); return
            text = event.text()
            if text:
                self._send(text.encode("utf-8"))
        except Exception:
            pass

    def _copy(self):
        lines = self.screen.display
        QApplication.clipboard().setText("\n".join(l.rstrip() for l in lines))

    def _paste(self):
        txt = QApplication.clipboard().text()
        if txt and self._send:
            self._send(txt.encode("utf-8"))

    paste_clipboard = _paste

    def contextMenuEvent(self, event):
        from . import theme
        m = QMenu(self)
        m.addAction(theme.emoji_icon("📋"), "Copy", self._copy)
        m.addAction(theme.emoji_icon("📥"), "Paste", self._paste)
        m.addSeparator()
        m.addAction(theme.emoji_icon("⤓"), "Scroll to bottom (live)", self._to_bottom_update)
        m.addAction(theme.emoji_icon("🧹"), "Clear screen", self.clear)
        m.addSeparator()
        m.addAction(theme.emoji_icon("💾"), "Save all output…", self._save_output)
        m.exec_(event.globalPos())

    def _to_bottom_update(self):
        self._to_bottom(); self._dirty = True; self.update()

    def full_text(self) -> str:
        """Everything that ever scrolled past (ANSI stripped), read back from the
        on-disk capture — so it covers the whole session, not just scrollback."""
        raw = b""
        try:
            if self._cap_fh is not None:
                self._cap_fh.flush()
            if self._cap_path and os.path.exists(self._cap_path):
                with open(self._cap_path, "rb") as fh:
                    raw = fh.read()
        except Exception:
            pass
        try:
            from ..results import strip_ansi
            text = strip_ansi(raw.decode("utf-8", "replace"))
        except Exception:
            text = raw.decode("utf-8", "replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _save_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save all terminal output",
                                              "terminal.txt")
        if not path:
            return
        try:
            data = self.full_text()
            if not data.strip():        # fall back to the visible screen
                data = "\n".join(l.rstrip() for l in self.screen.display)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(data)
        except Exception:
            pass

    def clear(self):
        """Clear the visible screen only — the on-disk capture is kept, so a
        later 'Save all output' still has the full session."""
        try:
            self.screen.reset()
        except Exception:
            pass
        self._following = True
        self._dirty = True

    def cleanup(self):
        """Close + delete the disk capture (call when the session closes)."""
        try:
            if self._cap_fh is not None:
                self._cap_fh.close()
        except Exception:
            pass
        self._cap_fh = None
        try:
            if self._cap_path and os.path.exists(self._cap_path):
                os.remove(self._cap_path)
        except Exception:
            pass
