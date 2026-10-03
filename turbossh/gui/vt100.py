"""True VT100/ANSI terminal widget, so full-screen apps (htop, vim, top, less,
nano) render correctly. A character grid is painted with colors, reverse video,
a block cursor, mouse selection and a draggable scrollback.

The screen model (escape parsing, alternate screen, scrollback) lives in
:mod:`turbossh.gui.termscreen` and has no Qt in it; this module is the widget:
painting, keyboard / mouse / clipboard, and the timer that drains the reader.
"""

from __future__ import annotations

import os
import re
import threading
import time

import pyte
from PyQt5.QtCore import QEvent, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt5.QtWidgets import (QApplication, QCheckBox, QFileDialog, QMenu,
                             QMessageBox, QScrollBar, QToolTip, QWidget)

from .. import paths
from .termscreen import TermScreen

# 16-color ANSI palette (xterm-ish), on a dark background. The terminal is ALWAYS
# dark (black bg, light text) regardless of app theme — like MobaXterm and most
# terminals; a white terminal looked bad.
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
_SELECTION_BG = "#264f78"

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

# The disk capture of a session stops growing here (a week-long `slog2info -w`
# must not fill the disk); the on-screen scrollback is unaffected.
_CAPTURE_LIMIT = 256 * 1024 * 1024

_MODIFIER_KEYS = (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta,
                  Qt.Key_AltGr, Qt.Key_CapsLock, Qt.Key_NumLock, Qt.Key_ScrollLock)
# xterm key encodings: cursor keys end in a letter, the editing keypad and
# F5..F12 are "CSI n ~", F1..F4 are SS3 letters.
_CURSOR_KEYS = {Qt.Key_Up: "A", Qt.Key_Down: "B", Qt.Key_Right: "C", Qt.Key_Left: "D",
                Qt.Key_Home: "H", Qt.Key_End: "F"}
_TILDE_KEYS = {Qt.Key_Insert: 2, Qt.Key_Delete: 3, Qt.Key_PageUp: 5,
               Qt.Key_PageDown: 6, Qt.Key_F5: 15, Qt.Key_F6: 17, Qt.Key_F7: 18,
               Qt.Key_F8: 19, Qt.Key_F9: 20, Qt.Key_F10: 21, Qt.Key_F11: 23,
               Qt.Key_F12: 24}
_F1_F4 = {Qt.Key_F1: "P", Qt.Key_F2: "Q", Qt.Key_F3: "R", Qt.Key_F4: "S"}
# Ctrl + these punctuation keys produce the remaining C0 control codes
_CTRL_PUNCT = {Qt.Key_Space: 0, Qt.Key_At: 0, Qt.Key_2: 0, Qt.Key_BracketLeft: 27,
               Qt.Key_Backslash: 28, Qt.Key_BracketRight: 29,
               Qt.Key_AsciiCircum: 30, Qt.Key_6: 30, Qt.Key_Underscore: 31,
               Qt.Key_Minus: 31, Qt.Key_Slash: 31}


class Vt100Terminal(QWidget):
    """Renders a terminal screen; forwards keystrokes via ``send_fn(bytes)``.
    Emits ``resized(cols, rows)`` so the caller can resize the remote PTY."""

    resized = pyqtSignal(int, int)
    _saved = pyqtSignal(str, str)           # path, error ("" = ok); from the save worker

    # seconds of escape-sequence parsing allowed per timer tick: the reader's
    # backlog is fed in slices until this is used up, so a flood can never hold
    # the GUI thread for more than a frame
    _TICK_BUDGET = 0.012
    _SLICE = 16384

    def __init__(self, send_fn, cols=120, rows=34, parent=None):
        super().__init__(parent)
        self._send = send_fn
        self.on_interrupt = None        # optional hook called on Ctrl-C (flush)
        self.on_reconnect = None        # optional hook for Ctrl-R when disconnected
        self.reconnect_armed = False    # when True, Ctrl-R reconnects (not shell search)
        self.cols, self.rows = cols, rows
        from . import settings
        try:
            scrollback = int(settings.get("term_scrollback") or 10000)
        except (TypeError, ValueError):
            scrollback = 10000
        # In-widget scrollback is memory-bound (a cell grid costs ~16 KB/line), so
        # it's capped — the session is also teed to disk (see _open_capture) for
        # "Save all output".
        scrollback = max(2000, min(scrollback, 50000))
        self.screen = TermScreen(cols, rows, history=scrollback, reply=self._reply)
        self.stream = pyte.ByteStream(self.screen)
        self._following = True          # auto-stick to the bottom (live tail)
        self._cap_fh = None             # disk tee of everything printed
        self._cap_path = None
        self._cap_bytes = 0
        self._open_capture()

        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.IBeamCursor)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._font_family = settings.get("term_font") or "Cascadia Mono"
        try:
            self._font_size = int(settings.get("term_font_size") or 11)
        except (TypeError, ValueError):
            self._font_size = 11
        highlight = settings.get("highlight_keywords")
        self._highlight = True if highlight is None else bool(highlight)
        self._bg = QColor(_BG_DEFAULT)
        self._sel_bg = QColor(_SELECTION_BG)
        self._colors: dict = {}         # (name, bright) -> QColor
        self._styles: dict = {}         # (cell style, highlight) -> (fg, bg|None, font key)
        self._fonts: dict = {}          # (italic, underline, strike) -> QFont
        self._hl_cache: dict = {}       # row text -> per-column colours | None
        self._apply_font()

        # drain the reader buffer + repaint at ~30fps
        self._pull = None
        self._dropped = None
        self._timer = QTimer(self); self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        # resize is DEBOUNCED. Maximise / split / dragging a divider fires a STORM
        # of resize events; applying each one (screen resize + a remote SIGWINCH)
        # mangled the buffer and left the prompt missing until you pressed Enter.
        # We coalesce them into ONE clean resize once the size settles.
        self._pending_size = None
        self._resize_timer = QTimer(self); self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._apply_resize)

        # a real, draggable vertical scrollbar so you can scroll the WHOLE session.
        # The screen always stays at the live bottom; _view_offset = how many lines
        # we're scrolled UP from it, and we paint a window into the scrollback +
        # the live buffer (see _visible_rows).
        self._view_offset = 0           # 0 = live tail; >0 = scrolled back
        self._prev_pushed = 0           # scrollback lines ever pushed, last tick
        self._last_cursor_row = 0
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

        # mouse selection: (anchor, point), each (row id, column). A row id counts
        # from the first line of the session, so it stays valid while output
        # scrolls (see _row_base).
        self._sel = None
        self._selecting = False
        self._anchor = (0, 0)
        self._saved.connect(self._on_saved)

    # ---- wiring -----------------------------------------------------------
    def set_source(self, pull_fn, dropped_fn=None):
        """Provide a callable(maxn) -> bytes that the timer drains each tick, and
        optionally one returning how many bytes the reader had to discard since
        it was last asked (so the gap is shown instead of silently skipped)."""
        self._pull = pull_fn
        self._dropped = dropped_fn

    def _reply(self, text: str):
        """The screen answering a query from the program (cursor position …)."""
        if self._send:
            try:
                self._send(text.encode("utf-8"))
            except Exception:
                pass

    # ---- font ---------------------------------------------------------------
    def _apply_font(self):
        # NB: store as _font, not `font` — `self.font` would shadow QWidget.font().
        self._font = QFont(self._font_family, self._font_size)
        # crisp, MobaXterm-like rendering: monospace hint, antialiasing, and a
        # fallback chain so it stays a clean mono even if the chosen font is absent.
        self._font.setStyleHint(QFont.Monospace, QFont.PreferAntialias)
        try:
            self._font.setFamilies([self._font_family, "Cascadia Mono", "Consolas",
                                    "DejaVu Sans Mono", "Courier New"])
        except Exception:
            pass
        self._font.setFixedPitch(True)
        self._font.setHintingPreference(QFont.PreferFullHinting)
        fm = QFontMetrics(self._font)
        self.cw = max(1, fm.horizontalAdvance("M"))
        self.ch = fm.height()
        self._ascent = fm.ascent()
        # A whole run of text is drawn in one call only if the font really
        # advances cw pixels per character; otherwise the run would drift off the
        # cell grid, so fall back to one cell at a time.
        self._batch = fm.horizontalAdvance("M" * 64) == 64 * self.cw
        self._fonts.clear()

    def _font_for(self, key):
        font = self._fonts.get(key)
        if font is None:
            font = QFont(self._font)
            italic, underline, strike = key
            font.setItalic(italic); font.setUnderline(underline)
            font.setStrikeOut(strike)
            self._fonts[key] = font
        return font

    def set_font_size(self, size: int):
        """Live zoom (Ctrl+wheel): re-measure the cell and re-fit the grid."""
        size = max(6, min(int(size), 40))
        if size == self._font_size:
            return
        self._font_size = size
        self._apply_font()
        self._request_resize()
        self.update()

    # ---- full-session capture to disk (bounded; never grows RAM) ----
    def _open_capture(self):
        try:
            capdir = paths.state_path("captures")
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
        fh = self._cap_fh
        if fh is None:
            return
        try:
            if self._cap_bytes + len(data) > _CAPTURE_LIMIT:
                fh.write(b"\r\n[capture stopped: the %d MB limit for one session "
                         b"was reached]\r\n" % (_CAPTURE_LIMIT // (1024 * 1024)))
                fh.flush()
                self._cap_fh = None     # keep the file for saving, stop writing
                self._cap_closed = fh
                return
            fh.write(data)
            self._cap_bytes += len(data)
        except Exception:
            pass

    # ---- feeding ------------------------------------------------------------
    def _feed(self, data: bytes):
        # if we're tailing, keep the view pinned to the live bottom
        if self._following:
            self._view_offset = 0
        try:
            self.stream.feed(data)
        except Exception:
            pass
        self._capture(data)

    def feed(self, data):
        self._feed(data if isinstance(data, bytes) else data.encode())
        self._refresh()

    def _tick(self):
        if self._pull is not None:
            deadline = time.perf_counter() + self._TICK_BUDGET
            while True:
                data = self._pull(self._SLICE)
                if not data:
                    break
                self._feed(data)
                if time.perf_counter() > deadline:
                    break
            if self._dropped is not None:
                lost = self._dropped()
                if lost:
                    self._feed(("\r\n\x1b[33m[… %d KB of output skipped: it arrived "
                                "faster than a terminal can show …]\x1b[0m\r\n"
                                % max(1, lost // 1024)).encode("utf-8"))
        self._refresh()

    def _refresh(self):
        """Repaint what changed since the last call (and nothing if nothing did)."""
        screen = self.screen
        history = screen.history
        # while scrolled up, keep the view anchored to its content as new lines
        # scroll off the top (so the page doesn't drift under you). Counting lines
        # ever pushed, not the list's length, keeps this right once the scrollback
        # is full and old lines are being trimmed.
        pushed = history.pushed
        if not self._following and pushed > self._prev_pushed:
            self._view_offset = min(len(history.top),
                                    self._view_offset + (pushed - self._prev_pushed))
        scrolled = pushed != self._prev_pushed
        self._prev_pushed = pushed
        self._update_scrollbar()
        dirty = screen.dirty
        cursor_row = screen.cursor.y
        if not dirty and cursor_row == self._last_cursor_row and not scrolled:
            return
        if (scrolled or self._view_offset or self._sel is not None
                or len(dirty) > self.rows // 2):
            self.update()
        else:
            # typing / a status line: repaint only the rows that changed
            width = self.width() - self._sb_w
            for y in set(dirty) | {cursor_row, self._last_cursor_row}:
                self.update(QRect(0, y * self.ch, width, self.ch))
        dirty.clear()
        self._last_cursor_row = cursor_row

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
        self.update()

    def scroll_to_bottom(self):
        """Return the view to the live tail (after the user scrolled up)."""
        self._view_offset = 0
        self._following = True
        self._update_scrollbar()
        self.update()

    def _scroll_lines(self, delta):
        """Scroll by `delta` lines (+ = up/older, - = down/newer)."""
        total = len(self.screen.history.top)
        self._view_offset = max(0, min(total, self._view_offset + delta))
        self._following = (self._view_offset == 0)
        self._update_scrollbar()
        self.update()

    def wheelEvent(self, event):
        dy = event.angleDelta().y()
        if not dy:
            return
        steps = dy // 120 or (1 if dy > 0 else -1)
        if event.modifiers() & Qt.ControlModifier:
            self.set_font_size(self._font_size + (1 if steps > 0 else -1))
        else:
            self._scroll_lines(steps * 3)        # 3 lines per wheel notch

    def showEvent(self, event):
        # When the widget is re-shown (tab switch, or split<->tabs reparenting),
        # nothing has marked us dirty, so force a re-render.
        super().showEvent(event)
        self.update()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()                            # solid cursor

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()                            # hollow cursor

    # ---- sizing ----
    def resizeEvent(self, event):
        # keep the scrollbar pinned to the right edge and paint to its left
        self._sb.setGeometry(self.width() - self._sb_w, 0, self._sb_w, self.height())
        self._request_resize()
        super().resizeEvent(event)

    def _request_resize(self):
        cols = max(20, (self.width() - self._sb_w) // self.cw)
        rows = max(5, self.height() // self.ch)
        if (cols, rows) != (self.cols, self.rows):
            # defer the real resize until the drag / maximise settles. Paint keeps
            # using the current grid for the ~90 ms gap (harmless), then we apply
            # ONE clean resize — no SIGWINCH storm, no lost prompt.
            self._pending_size = (cols, rows)
            self._resize_timer.start(90)

    def _apply_resize(self):
        size = self._pending_size
        self._pending_size = None
        if not size or size == (self.cols, self.rows):
            return
        cols, rows = size
        self.cols, self.rows = cols, rows
        self._sel = None                # row ids shift when lines move to scrollback
        try:
            self.screen.resize_keeping_cursor(rows, cols)
        except Exception:
            pass
        # pin to the live tail so the prompt / cursor stay on-screen after a
        # resize, instead of leaving a blank gap where the prompt used to be
        if self._following:
            self._view_offset = 0
        else:
            self._view_offset = min(self._view_offset, len(self.screen.history.top))
        self._prev_pushed = self.screen.history.pushed
        # tell the remote PTY its new size ONCE (a single SIGWINCH); bash/zsh
        # redraw the current prompt line on it, so it reappears on its own
        self.resized.emit(cols, rows)
        self._update_scrollbar()
        self.update()

    # ---- colours ----
    def _color(self, name, bright=False):
        key = (name, bright)
        color = self._colors.get(key)
        if color is None:
            table = _BRIGHT if bright else _PALETTE
            if name in table:
                color = QColor(table[name])
            elif isinstance(name, str) and len(name) == 6:   # 256 / true colour hex
                color = QColor("#" + name)
                if not color.isValid():
                    color = QColor(_PALETTE["default"])
            else:
                color = QColor(_PALETTE["default"])
            self._colors[key] = color
        return color

    def _style(self, attrs, highlight):
        """(fg, bg or None, font key) for a cell's attributes — cached, because a
        screen has a handful of distinct styles but thousands of cells."""
        key = (attrs, highlight)
        style = self._styles.get(key)
        if style is None:
            fg_name, bg_name, bold, italics, underscore, strike, reverse = attrs
            fg = self._color(fg_name, bold)
            # keyword highlight: ONLY when the server left this cell at the
            # default colour (never override real ANSI colours or reverse)
            if highlight and not reverse and fg_name == "default":
                fg = self._color(highlight)
            bg = None if bg_name == "default" else self._color(bg_name)
            if reverse:
                fg, bg = (bg or self._bg), fg
            if len(self._styles) > 4096:
                self._styles.clear()
            style = self._styles[key] = (fg, bg, (bool(italics), bool(underscore),
                                                  bool(strike)))
        return style

    def _row_highlights(self, text):
        """Per-column keyword colour for a row's text (None where no keyword)."""
        cached = self._hl_cache.get(text, False)
        if cached is not False:
            return cached
        hl = None
        if text.strip():
            for rx, color in _HIGHLIGHTS:
                for m in rx.finditer(text):
                    if hl is None:
                        hl = [None] * len(text)
                    for i in range(m.start(), m.end()):
                        hl[i] = color
        if len(self._hl_cache) > 4000:
            self._hl_cache.clear()
        self._hl_cache[text] = hl
        return hl

    # ---- rendering ----
    def _row_base(self) -> int:
        """Row id of the first line still in the scrollback list (= the number of
        lines trimmed away so far)."""
        history = self.screen.history
        return history.pushed - len(history.top)

    def _visible_rows(self):
        """The `rows` line-objects to paint: the live buffer when at the bottom,
        otherwise a window into the scrollback + the live buffer for the current
        scroll offset (None = blank row past the end)."""
        screen = self.screen
        first = len(screen.history.top) - self._view_offset
        return [screen.row(first + y) for y in range(self.rows)]

    def paintEvent(self, event):
        p = QPainter(self)
        rect = event.rect()
        p.fillRect(rect, self._bg)
        cw, ch, cols = self.cw, self.ch, self.cols
        first = max(0, rect.top() // ch)
        last = min(self.rows - 1, rect.bottom() // ch)
        screen = self.screen
        top_index = len(screen.history.top) - self._view_offset
        sel = self._selection_rows()
        for y in range(first, last + 1):
            line = screen.row(top_index + y)
            if line is None:
                continue
            sel_cols = None
            if sel is not None:
                (r0, c0), (r1, c1) = sel
                r = top_index + y
                if r0 <= r <= r1:
                    sel_cols = (c0 if r == r0 else 0, c1 if r == r1 else cols - 1)
            if not line and sel_cols is None:
                continue                 # an untouched (blank) line: bg is painted
            self._paint_row(p, y * ch, line, sel_cols)
        # block cursor — only at the live tail (hidden while scrolled back)
        if self._view_offset <= 0 and not screen.cursor.hidden:
            cx, cy = min(screen.cursor.x, cols - 1), screen.cursor.y
            if cy < self.rows:
                cur = QColor(self._color("green")); cur.setAlpha(150)
                box = QRect(cx * cw, cy * ch, cw, ch)
                if self.hasFocus():
                    p.fillRect(box, cur)
                else:                    # hollow when another widget has the keys
                    p.setPen(cur); p.drawRect(box.adjusted(0, 0, -1, -1))
        p.end()

    def _paint_row(self, p, py, line, sel_cols):
        """Paint one row as RUNS of same-styled cells: one fillRect + one drawText
        per run instead of one per cell (a 200x50 screen is 10,000 cells but
        typically a few hundred runs)."""
        cw, ch, cols = self.cw, self.ch, self.cols
        cells = [line[x] for x in range(cols)]
        hl = None
        if self._highlight:
            hl = self._row_highlights("".join(c.data or " " for c in cells))
        batch = self._batch
        x = 0
        while x < cols:
            cell = cells[x]
            attrs = cell[1:8]            # fg, bg, bold, italics, underscore, strike, reverse
            mark = hl[x] if hl is not None and x < len(hl) else None
            selected = sel_cols is not None and sel_cols[0] <= x <= sel_cols[1]
            data = cell.data
            start = x
            x += 1
            # extend the run while style, highlight and selection state all match;
            # only plain ASCII is batched (a wide / combining char needs its own
            # cell position)
            if batch and len(data) == 1 and data < "\x7f":
                while x < cols:
                    nxt = cells[x]
                    nd = nxt.data
                    if (nxt[1:8] != attrs or len(nd) != 1 or nd >= "\x7f"
                            or (hl[x] if hl is not None and x < len(hl) else None) != mark
                            or (sel_cols is not None
                                and (sel_cols[0] <= x <= sel_cols[1]) != selected)):
                        break
                    x += 1
                text = "".join(c.data for c in cells[start:x])
            else:
                text = data
            fg, bg, font_key = self._style(attrs, mark)
            if selected:
                bg = self._sel_bg
            px = start * cw
            if bg is not None:
                p.fillRect(px, py, (x - start) * cw, ch, bg)
            if text and not text.isspace():
                p.setFont(self._font_for(font_key))
                p.setPen(fg)
                p.drawText(px, py + self._ascent, text)

    # ---- focus / keyboard ----
    def focusNextPrevChild(self, nxt):
        # never let Tab/Backtab move focus away — the shell needs Tab for completion
        return False

    def event(self, event):
        # Qt offers a key to the window's shortcuts BEFORE the focused widget. A
        # terminal must get first refusal, or Ctrl+W (delete word) closes the tab,
        # Ctrl+Q (resume output) quits the app, Ctrl+S / Ctrl+T / Ctrl+N open
        # dialogs, and Backspace / Delete trigger another pane's file browser.
        # Accepting ShortcutOverride claims the key for keyPressEvent.
        if event.type() == QEvent.ShortcutOverride and self._claims(event):
            event.accept()
            return True
        return super().event(event)

    @staticmethod
    def _claims(event) -> bool:
        """Everything goes to the shell except Ctrl+Shift+<key>, which is the
        application's own namespace (copy, paste, new session, close tab) —
        the same split GNOME Terminal and Windows Terminal use."""
        if event.key() in _MODIFIER_KEYS:
            return False
        mods = event.modifiers()
        if event.key() == Qt.Key_F4 and mods & Qt.AltModifier:
            return False                 # Alt+F4 stays the window's
        return not (mods & Qt.ControlModifier and mods & Qt.ShiftModifier)

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        if key in _MODIFIER_KEYS:
            return                       # a bare Ctrl/Shift must not scroll or send
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        if key == Qt.Key_F4 and mods & Qt.AltModifier:
            event.ignore()               # let the window close
            return
        if ctrl and shift:
            if key == Qt.Key_C:
                self.copy_selection()
            elif key == Qt.Key_V:
                self.paste_clipboard()
            else:
                event.ignore()           # the window's Ctrl+Shift shortcuts
            return
        if shift and key in (Qt.Key_PageUp, Qt.Key_PageDown):
            self._scroll_lines(self.rows - 1 if key == Qt.Key_PageUp
                               else -(self.rows - 1))
            return
        if shift and key == Qt.Key_Insert:
            self.paste_clipboard(); return
        if ctrl and key == Qt.Key_Insert:
            self.copy_selection(); return
        if ctrl and key == Qt.Key_C and self._sel is not None:
            # with text selected, Ctrl+C copies it (what a Windows user expects);
            # with nothing selected it is the interrupt, as always
            self.copy_selection(); self.clear_selection(); return
        # Ctrl-R reconnects when the session is down (otherwise it's the shell's
        # reverse-search as usual).
        if self.reconnect_armed and ctrl and key == Qt.Key_R and self.on_reconnect:
            try:
                self.on_reconnect()
            except Exception:
                pass
            return
        if not self._send:
            return
        data = self._encode_key(key, mods, event.text())
        if not data:
            return
        if not self._following:          # typing snaps back to the live tail
            self.scroll_to_bottom()
        self.clear_selection()
        try:
            self._send(data)
        except Exception:
            pass
        if data == b"\x03" and self.on_interrupt:
            try:
                self.on_interrupt()      # drop buffered flood -> stop fast
            except Exception:
                pass

    def _encode_key(self, key, mods, text) -> bytes:
        """The bytes an xterm sends for this key."""
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        alt = bool(mods & Qt.AltModifier)
        # AltGr arrives as Ctrl+Alt with the composed character as text
        # (AltGr+Q = "@" on a German layout): that is typing, not a control key.
        if ctrl and alt and text and text.isprintable():
            return text.encode("utf-8")
        # xterm modifier parameter: 1 + Shift(1) + Alt(2) + Ctrl(4)
        mod = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
        if key in _CURSOR_KEYS:
            letter = _CURSOR_KEYS[key]
            if mod > 1:
                return f"\x1b[1;{mod}{letter}".encode()
            # full-screen programs switch the cursor keys to "application" mode
            intro = "\x1bO" if self.screen.application_cursor_keys else "\x1b["
            return (intro + letter).encode()
        if key in _TILDE_KEYS:
            n = _TILDE_KEYS[key]
            return (f"\x1b[{n};{mod}~" if mod > 1 else f"\x1b[{n}~").encode()
        if key in _F1_F4:
            letter = _F1_F4[key]
            return (f"\x1b[1;{mod}{letter}" if mod > 1 else f"\x1bO{letter}").encode()
        if key == Qt.Key_Backtab or (key == Qt.Key_Tab and shift):
            return b"\x1b[Z"
        if key == Qt.Key_Tab:
            data = b"\t"
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            data = b"\r"
        elif key == Qt.Key_Backspace:
            data = b"\x08" if ctrl else b"\x7f"
        elif key == Qt.Key_Escape:
            data = b"\x1b"
        elif ctrl and Qt.Key_A <= key <= Qt.Key_Z:
            data = bytes([key - Qt.Key_A + 1])
        elif ctrl and key in _CTRL_PUNCT:
            data = bytes([_CTRL_PUNCT[key]])
        else:
            data = text.encode("utf-8") if text else b""
        if alt and data:
            data = b"\x1b" + data        # Alt+key = ESC prefix (readline's Meta)
        return data

    # ---- mouse selection ----
    def _cell_at(self, pos):
        col = max(0, min(self.cols - 1, pos.x() // self.cw))
        y = max(0, min(self.rows - 1, pos.y() // self.ch))
        index = len(self.screen.history.top) - self._view_offset + y
        return (self._row_base() + index, col)

    def _selection_rows(self):
        """The selection as ((row index, col), (row index, col)), ordered, in
        indices into the current scrollback+screen — or None."""
        if self._sel is None:
            return None
        a, b = self._sel
        if a > b:
            a, b = b, a
        base = self._row_base()
        if b[0] < base:
            return None                  # it scrolled out of the scrollback
        return (max(0, a[0] - base), a[1] if a[0] >= base else 0), (b[0] - base, b[1])

    def selected_text(self) -> str:
        sel = self._selection_rows()
        return self.screen.text_range(*sel) if sel is not None else ""

    def clear_selection(self):
        if self._sel is not None:
            self._sel = None
            self.update()

    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.LeftButton:
            cell = self._cell_at(event.pos())
            self._selecting = True
            self._anchor = cell
            self.clear_selection()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # (mouse tracking is off, so moves only arrive while a button is held;
        # _selecting says it was the left one)
        if self._selecting:
            # dragging past the top / bottom edge scrolls through the scrollback
            if event.pos().y() < 0:
                self._scroll_lines(1)
            elif event.pos().y() > self.height():
                self._scroll_lines(-1)
            cell = self._cell_at(event.pos())
            if cell != self._anchor or self._sel is not None:
                self._sel = (self._anchor, cell)
                self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._selecting = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Select the word under the pointer."""
        if event.button() != Qt.LeftButton:
            return
        row_id, col = self._cell_at(event.pos())
        text = self.screen.row_text(row_id - self._row_base())
        if not text or col >= len(text) or text[col].isspace():
            return
        left = right = col
        while left > 0 and not text[left - 1].isspace():
            left -= 1
        while right + 1 < len(text) and not text[right + 1].isspace():
            right += 1
        self._sel = ((row_id, left), (row_id, right))
        self._selecting = False
        self.update()

    # ---- clipboard ----
    def copy_selection(self):
        """Copy the selection; with nothing selected, the visible screen."""
        text = self.selected_text()
        if not text:
            first = len(self.screen.history.top) - self._view_offset
            text = "\n".join(self.screen.row_text(first + y).rstrip()
                             for y in range(self.rows)).rstrip("\n")
        QApplication.clipboard().setText(text)

    def paste_clipboard(self):
        self.paste_text(QApplication.clipboard().text())

    def paste_text(self, text: str, confirm: bool = True):
        if not text or not self._send:
            return
        # a clipboard line break is CRLF or LF; a terminal sends CR for Enter
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        bracketed = self.screen.bracketed_paste
        if "\n" in text.rstrip("\n") and not bracketed and confirm:
            # without bracketed paste the shell runs each line as it arrives
            if not self._confirm_multiline(text):
                return
        payload = text.replace("\n", "\r")
        if bracketed:
            # the program asked for pasted text to be marked, so it is inserted
            # as text rather than run; ESC is stripped so the paste cannot close
            # the bracket itself
            payload = "\x1b[200~" + payload.replace("\x1b", "") + "\x1b[201~"
        try:
            self._send(payload.encode("utf-8"))
        except Exception:
            pass

    def _confirm_multiline(self, text: str) -> bool:
        from . import settings
        if not settings.get("confirm_multiline_paste"):
            return True
        lines = text.rstrip("\n").count("\n") + 1
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Paste several lines?")
        box.setText(f"The clipboard holds {lines} lines. Each one will be sent "
                    f"to the shell and run as it arrives.")
        preview = text if len(text) <= 600 else text[:600] + "\n…"
        box.setDetailedText(preview)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText("Paste")
        box.button(QMessageBox.No).setText("Cancel")
        box.setDefaultButton(QMessageBox.No)
        again = QCheckBox("Don't ask again")
        box.setCheckBox(again)
        ok = box.exec_() == QMessageBox.Yes
        if ok and again.isChecked():
            settings.save({"confirm_multiline_paste": False})
        return ok

    def contextMenuEvent(self, event):
        from . import theme
        m = QMenu(self)
        m.addAction(theme.emoji_icon("📋"), "Copy", self.copy_selection)
        m.addAction(theme.emoji_icon("📥"), "Paste", self.paste_clipboard)
        m.addSeparator()
        m.addAction(theme.emoji_icon("⤓"), "Scroll to bottom (live)", self.scroll_to_bottom)
        m.addAction(theme.emoji_icon("🧹"), "Clear screen", self.clear)
        m.addSeparator()
        m.addAction(theme.emoji_icon("💾"), "Save all output…", self.save_output)
        m.exec_(event.globalPos())

    # ---- save ----
    def save_output(self):
        """Save the whole session (ANSI stripped) from the disk capture. The copy
        runs on a worker thread: a long session is hundreds of MB."""
        path, _ = QFileDialog.getSaveFileName(self, "Save all terminal output",
                                              "terminal.txt")
        if not path:
            return
        source = self._cap_path
        fh = self._cap_fh
        if fh is not None:
            try:
                fh.flush()
            except Exception:
                pass
        fallback = "\n".join(self.screen.row_text(i).rstrip()
                             for i in range(self.screen.total_rows))
        threading.Thread(target=self._save_worker, args=(source, path, fallback),
                         daemon=True).start()

    def _save_worker(self, source, path, fallback):
        from ..results import strip_ansi
        try:
            wrote = False
            with open(path, "w", encoding="utf-8", newline="\n") as out:
                if source and os.path.exists(source):
                    with open(source, "rb") as src:
                        for raw in src:              # line by line: constant memory
                            text = strip_ansi(raw.decode("utf-8", "replace"))
                            if text:
                                out.write(text if text.endswith("\n") else text + "\n")
                                wrote = wrote or bool(text.strip())
                if not wrote:                        # no capture: what is on screen
                    out.write(fallback)
            self._saved.emit(path, "")
        except Exception as exc:
            self._saved.emit(path, f"{type(exc).__name__}: {exc}")

    def _on_saved(self, path, error):
        if error:
            QMessageBox.warning(self, "Save output",
                                f"Could not save to {path}:\n\n{error}")
        else:
            # a tooltip, not text fed into the terminal: that would be drawn over
            # whatever a full-screen program has on screen
            QToolTip.showText(self.mapToGlobal(self.rect().center()),
                              f"Output saved to {path}", self)

    # ---- misc ----
    def clear(self):
        """Clear the visible screen only — the scrollback and the on-disk capture
        are kept, so you can still scroll up or 'Save all output'."""
        screen = self.screen
        try:
            if not screen.in_alt_screen:
                # move what is on screen into the scrollback instead of losing it
                last = max((y for y in range(screen.lines) if screen.buffer.get(y)),
                           default=-1)
                for y in range(last + 1):
                    screen._push_history(screen.buffer[y])
            screen.buffer.clear()
            screen.cursor_position()
            screen.dirty.update(range(screen.lines))
        except Exception:
            pass
        self._sel = None
        self._view_offset = 0
        self._following = True
        self._prev_pushed = screen.history.pushed
        self._update_scrollbar()
        self.update()

    def cleanup(self):
        """Close + delete the disk capture (call when the session closes)."""
        try:
            self._resize_timer.stop(); self._timer.stop()
        except Exception:
            pass
        self._pull = None
        for fh in (self._cap_fh, getattr(self, "_cap_closed", None)):
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass
        self._cap_fh = None
        try:
            if self._cap_path and os.path.exists(self._cap_path):
                os.remove(self._cap_path)
        except Exception:
            pass

    # the names the main window's menus call
    _copy = copy_selection
    _paste = paste_clipboard
    _save_output = save_output
    _to_bottom_update = scroll_to_bottom
