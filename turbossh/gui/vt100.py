"""True VT100/ANSI terminal widget backed by pyte, so full-screen apps
(htop, vim, top, less, nano) render correctly. A character grid is painted
cell-by-cell with colors, bold, reverse video, and a block cursor."""

from __future__ import annotations

import os
import re
import pyte
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QFontMetrics, QPainter, QColor
from PyQt5.QtWidgets import (QWidget, QApplication, QMenu, QFileDialog,
                             QScrollBar)

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
# light-mode terminal: dark text on near-white, ANSI colours darkened to stay
# readable on a light background (the dark palette is invisible on white).
_PALETTE_LIGHT = {
    "black": "#2b2b2b", "red": "#c0392b", "green": "#1e8449",
    "brown": "#9a7d0a", "yellow": "#9a7d0a", "blue": "#1f6feb",
    "magenta": "#8e44ad", "cyan": "#0e7c86", "white": "#1d2329",
    "default": "#1d2329",
}
_BRIGHT_LIGHT = {
    "black": "#555555", "red": "#e74c3c", "green": "#229954",
    "brown": "#b9770e", "yellow": "#b9770e", "blue": "#2e86de",
    "magenta": "#9b59b6", "cyan": "#1391a0", "white": "#000000",
    "default": "#101418",
}
_BG_LIGHT = "#fbfbfb"

# MobaXterm-style keyword highlighting: colour these words in OTHERWISE-plain
# output. We only ever recolour cells the server left at the default colour, so
# real ANSI colours are never overridden. Conservative word lists to avoid noise.
_HIGHLIGHTS = [
    (re.compile(r"\b(error|errors|fail|failed|failure|fatal|denied|refused|"
                r"unable|exception|panic|critical|segfault|traceback|timeout|"
                r"timed out)\b", re.I), "red"),
    (re.compile(r"\b(warning|warn|deprecated|caution)\b", re.I), "brown"),
    (re.compile(r"\b(success|successful|succeeded|passed|connected|enabled|"
                r"completed|online|active|listening|started|running|ready)\b",
                re.I), "green"),
]


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
        self.on_reconnect = None        # optional hook for Ctrl-R when disconnected
        self.reconnect_armed = False    # when True, Ctrl-R reconnects (not shell search)
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
            fam = _settings.get("term_font") or "Cascadia Mono"
            size = int(_settings.get("term_font_size") or 11)
        except Exception:
            fam, size = "Cascadia Mono", 11
        self.font = QFont(fam, size)
        # crisp, MobaXterm-like rendering: monospace hint, antialiasing, and a
        # fallback chain so it stays a clean mono even if the chosen font is absent.
        self.font.setStyleHint(QFont.Monospace, QFont.PreferAntialias)
        try:
            self.font.setFamilies([fam, "Cascadia Mono", "Consolas",
                                   "DejaVu Sans Mono", "Courier New"])
        except Exception:
            pass
        self.font.setFixedPitch(True)
        self.font.setHintingPreference(QFont.PreferFullHinting)
        fm = QFontMetrics(self.font)
        self.cw = max(1, fm.horizontalAdvance("M"))
        self.ch = fm.height()
        self._ascent = fm.ascent()
        # the terminal is ALWAYS dark (black bg, light text) regardless of app
        # theme — like MobaXterm/most terminals; a white terminal looked bad.
        self._bg, self._pal, self._bright = _BG_DEFAULT, _PALETTE, _BRIGHT
        try:
            from . import settings as _settings
            self._highlight = _settings.get("highlight_keywords")
            if self._highlight is None:
                self._highlight = True
        except Exception:
            self._highlight = True
        self.setStyleSheet(f"background:{self._bg};")

        # drain the reader buffer + repaint at ~30fps, with a bounded amount of
        # work per tick so a flood of output (journalctl/slog2info) never freezes
        self._dirty = False
        self._pull = None
        self._timer = QTimer(self); self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        # resize is DEBOUNCED. Maximise / split / dragging a divider fires a STORM
        # of resize events; applying each one (pyte screen resize + a remote
        # SIGWINCH) mangled the buffer and left the prompt missing until you
        # pressed Enter. We coalesce them into ONE clean resize once the size
        # settles — see _apply_resize.
        self._pending_size = None
        self._resize_timer = QTimer(self); self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._apply_resize)

        # a real, draggable vertical scrollbar so you can scroll the WHOLE session
        # (not just nudge the wheel). The pyte screen always stays at the live
        # bottom; _view_offset = how many lines we're scrolled UP from it, and we
        # paint a window into history.top + the live buffer (see _visible_rows).
        self._view_offset = 0           # 0 = live tail; >0 = scrolled back
        self._prev_top = 0              # len(history.top) last tick (anchor on growth)
        self._sb_w = 13
        self._sb = QScrollBar(Qt.Vertical, self)
        self._sb.setStyleSheet(
            "QScrollBar:vertical{background:#0c0c0c;width:13px;margin:0;}"
            "QScrollBar::handle:vertical{background:#3a3f47;border-radius:6px;"
            "min-height:28px;}"
            "QScrollBar::handle:vertical:hover{background:#525a66;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{"
            "background:transparent;}")
        self._sb.valueChanged.connect(self._on_scrollbar)

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
        # if we're tailing, keep the view pinned to the live bottom
        if self._following:
            self._view_offset = 0
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
        # while scrolled up, keep the view anchored to its content as new lines
        # scroll off the top (so the page doesn't drift under you)
        top = len(self.screen.history.top)
        if not self._following and top > self._prev_top:
            self._view_offset = min(top, self._view_offset + (top - self._prev_top))
            self._dirty = True
        self._prev_top = top
        self._update_scrollbar()
        if self._dirty:
            self._dirty = False
            self.update()

    # ---- scrollback ----
    def _update_scrollbar(self):
        """Keep the scrollbar's range/value in sync with the scrollback. 0 = top
        (oldest), maximum = bottom (live)."""
        sb = self._sb
        total = len(self.screen.history.top)
        if (sb.maximum() != total or sb.pageStep() != max(1, self.rows)
                or sb.value() != total - self._view_offset):
            sb.blockSignals(True)
            sb.setRange(0, total)
            sb.setPageStep(max(1, self.rows))
            sb.setSingleStep(1)
            sb.setValue(total - self._view_offset)
            sb.blockSignals(False)

    def _on_scrollbar(self, value):
        total = len(self.screen.history.top)
        self._view_offset = max(0, total - value)
        self._following = (self._view_offset == 0)
        self._dirty = True
        self.update()

    def _to_bottom(self):
        """Return the view to the live tail (after the user scrolled up)."""
        self._view_offset = 0
        self._following = True
        self._dirty = True

    def _scroll_lines(self, delta):
        """Scroll by `delta` lines (+ = up/older, - = down/newer)."""
        total = len(self.screen.history.top)
        self._view_offset = max(0, min(total, self._view_offset + delta))
        self._following = (self._view_offset == 0)
        self._update_scrollbar()
        self._dirty = True
        self.update()

    def wheelEvent(self, event):
        try:
            steps = event.angleDelta().y() // 120 or (1 if event.angleDelta().y() > 0 else -1)
            self._scroll_lines(steps * 3)        # 3 lines per wheel notch
        except Exception:
            pass

    def showEvent(self, event):
        # When the widget is re-shown (tab switch, or split<->tabs reparenting),
        # nothing has marked us dirty, so the existing pyte screen wouldn't be
        # repainted -> a blank terminal with no prompt. Force a re-render.
        super().showEvent(event)
        self._dirty = True
        self.update()

    # ---- sizing ----
    def resizeEvent(self, event):
        # keep the scrollbar pinned to the right edge and paint to its left
        sbw = self._sb_w
        self._sb.setGeometry(self.width() - sbw, 0, sbw, self.height())
        cols = max(20, (self.width() - sbw) // self.cw)
        rows = max(5, self.height() // self.ch)
        if (cols, rows) != (self.cols, self.rows):
            # defer the real resize until the drag / maximise settles. Paint keeps
            # using the current grid for the ~90 ms gap (harmless), then we apply
            # ONE clean resize — no SIGWINCH storm, no lost prompt.
            self._pending_size = (cols, rows)
            self._resize_timer.start(90)
        super().resizeEvent(event)

    def _apply_resize(self):
        size = self._pending_size
        self._pending_size = None
        if not size or size == (self.cols, self.rows):
            return
        cols, rows = size
        old_rows = self.screen.lines
        self.cols, self.rows = cols, rows
        try:
            self._resize_screen(old_rows, rows, cols)
        except Exception:
            pass
        # pin to the live tail so the prompt / cursor stay on-screen after a
        # resize, instead of leaving a blank gap where the prompt used to be
        if self._following:
            self._to_bottom()
        else:
            self._view_offset = min(self._view_offset, len(self.screen.history.top))
        # tell the remote PTY its new size ONCE (a single SIGWINCH); bash/zsh
        # redraw the current prompt line on it, so it reappears on its own
        # without needing an Enter press
        self.resized.emit(cols, rows)
        self._dirty = True
        self.update()

    def _resize_screen(self, old_rows, new_rows, new_cols):
        """Reflow on resize like a real terminal (pyte's own resize is naive: it
        clips from the top and never moves the cursor, which stranded/blanked the
        prompt). Flatten history + the visible screen into one list of lines, then
        choose the visible window so the cursor line stays put:

        * content FITS  (cursor above the new bottom) -> TOP-anchored: content at
          the top, blank rows BELOW it (no empty rows above a short banner).
        * content DOESN'T fit -> cursor pinned to the BOTTOM row, older lines slide
          up into history (scrollback preserved, draggable later).
        """
        s = self.screen
        cur_y = max(0, min(s.cursor.y, old_rows - 1))
        cur_x = s.cursor.x
        top = s.history.top
        all_lines = list(top) + [s.buffer[y] for y in range(old_rows)]
        cur_abs = len(top) + cur_y
        start = 0 if cur_abs < new_rows else cur_abs - (new_rows - 1)
        new_top = all_lines[:start]
        window = all_lines[start:start + new_rows]
        # set the new dimensions; pyte's buffer/cursor churn here is overwritten below
        s.resize(new_rows, new_cols)
        top.clear()
        top.extend(new_top)
        for y in range(new_rows):
            if y < len(window):
                s.buffer[y] = window[y]
            else:
                s.buffer.pop(y, None)            # fresh blank line on next access
        s.cursor.y = max(0, min(cur_abs - start, new_rows - 1))
        s.cursor.x = max(0, min(cur_x, new_cols - 1))
        s.dirty.update(range(new_rows))

    # ---- rendering ----
    def _col(self, name, bright=False):
        table = self._bright if bright else self._pal
        if name in table:
            return QColor(table[name])
        if isinstance(name, str) and len(name) == 6:       # pyte gives hex sometimes
            try:
                return QColor("#" + name)
            except Exception:
                pass
        return QColor(self._pal["default"])

    def _row_highlights(self, line):
        """Per-column keyword colour for a row (None where no keyword) — used to
        tint plain words like 'error'/'warning'/'success'. Returns None when off."""
        if not self._highlight:
            return None
        cols = self.cols
        text = "".join((line[x].data or " ") for x in range(cols))
        if not text.strip():
            return None
        hl = None
        for rx, color in _HIGHLIGHTS:
            for m in rx.finditer(text):
                if hl is None:
                    hl = [None] * cols
                for i in range(m.start(), min(m.end(), cols)):
                    hl[i] = color
        return hl

    def _visible_rows(self):
        """The `rows` line-objects to paint: the live buffer when at the bottom,
        otherwise a window into history.top + the live buffer for the current
        scroll offset (None = blank row past the end)."""
        buf = self.screen.buffer
        if self._view_offset <= 0:
            return [buf[y] for y in range(self.rows)]
        top = list(self.screen.history.top)
        T = len(top)
        start = T - self._view_offset
        out = []
        for i in range(start, start + self.rows):
            if 0 <= i < T:
                out.append(top[i])
            elif 0 <= i - T < self.rows:
                out.append(buf[i - T])
            else:
                out.append(None)
        return out

    def paintEvent(self, event):
        p = QPainter(self)
        bg_default = self._bg
        p.fillRect(self.rect(), QColor(bg_default))
        p.setFont(self.font)
        rows_data = self._visible_rows()
        for y in range(self.rows):
            line = rows_data[y]
            if line is None:
                continue
            hl = self._row_highlights(line)
            for x in range(self.cols):
                cell = line[x]
                ch = cell.data or " "
                reverse = cell.reverse
                fg = self._col(cell.fg, cell.bold)
                # keyword highlight: ONLY when the server left this cell at the
                # default colour (never override real ANSI colours or reverse).
                if hl is not None and not reverse and cell.fg == "default" and hl[x]:
                    fg = self._col(hl[x])
                bg = QColor(bg_default) if cell.bg == "default" else self._col(cell.bg)
                if reverse:
                    fg, bg = bg, fg
                px, py = x * self.cw, y * self.ch
                if bg.name().lower() != bg_default.lower():
                    p.fillRect(px, py, self.cw, self.ch, bg)
                if ch != " ":
                    p.setPen(fg)
                    p.drawText(px, py + self._ascent, ch)
        # block cursor — only at the live tail (hidden while scrolled back)
        if self._view_offset <= 0 and not self.screen.cursor.hidden:
            cx, cy = self.screen.cursor.x, self.screen.cursor.y
            if cy < self.rows and cx < self.cols:
                cur = self._col("green"); cur.setAlpha(150)
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
            # Ctrl-R reconnects when the session is down (otherwise it's the shell's
            # reverse-search as usual).
            if (self.reconnect_armed and (mods & Qt.ControlModifier)
                    and key == Qt.Key_R and self.on_reconnect):
                try:
                    self.on_reconnect()
                except Exception:
                    pass
                return
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
            self._resize_timer.stop(); self._timer.stop()
        except Exception:
            pass
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
