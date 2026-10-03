"""The terminal's screen model — pyte's ``Screen`` plus what a real terminal
needs and pyte leaves out. No Qt in here, so it is unit-testable headless.

* **Alternate screen** (DEC private modes 47 / 1047 / 1049). Full-screen programs
  (vim, less, htop, mc) switch to it and expect the shell's screen back when they
  exit; pyte ignores those modes, so their last frame stayed on screen and their
  redraws were pushed into the scrollback.
* **Replies.** ``ESC[6n`` (where is the cursor?) and ``ESC[c`` (who are you?)
  must be answered on the wire — BusyBox ``ash`` and several prompts wait for
  the answer. pyte computes the reply but drops it unless a writer is wired up.
* **Scrollback** as a plain list (O(1) indexing while painting, trimmed in
  batches) and only for lines that scroll off a full-height screen — not for the
  inner scrolling of a region, and never while the alternate screen is up.
* **A fast path for plain text.** pyte's ``draw`` does a width lookup, several
  mode checks and a ``namedtuple._replace`` for every character; for ordinary
  ASCII in replace mode the result is fully determined by (attributes, char), so
  cells are taken from a cache instead. This is what makes a log flood usable.
"""

from __future__ import annotations

import copy
from typing import Callable, List, Optional

import pyte
import pyte.charsets as cs
import pyte.modes as mo
from pyte.screens import Margins

_ALT_SCREEN_MODES = (47, 1047, 1049)
DECCKM = 1 << 5             # application cursor keys (private mode 1, as pyte stores it)
BRACKETED_PASTE = 2004 << 5
_TRIM_SLACK = 1000          # scrollback is trimmed in batches of this many lines


class _History:
    """Scrollback. ``top`` holds the lines that scrolled off (oldest first);
    ``pushed`` counts every line ever added, so a scrolled-back view can stay
    anchored even while old lines are being trimmed away."""

    __slots__ = ("top", "limit", "pushed")

    def __init__(self, limit: int):
        self.top: list = []
        self.limit = max(0, int(limit))
        self.pushed = 0


class TermScreen(pyte.Screen):
    """``reply`` is called with the text the terminal must send back to the
    program (cursor-position / device-attribute reports)."""

    def __init__(self, columns: int, lines: int, history: int = 10000,
                 reply: Optional[Callable[[str], None]] = None):
        self.history = _History(history)
        self._reply = reply
        self._alt = None                 # the saved main screen while alt is shown
        self._cells: dict = {}           # attrs -> {char: Char}; see draw()
        super().__init__(columns, lines)

    # ------------------------------------------------------------- replies
    def write_process_input(self, data: str) -> None:
        if self._reply is not None:
            self._reply(data)

    # ------------------------------------------------------------- modes
    @property
    def in_alt_screen(self) -> bool:
        return self._alt is not None

    @property
    def application_cursor_keys(self) -> bool:
        return DECCKM in self.mode

    @property
    def bracketed_paste(self) -> bool:
        return BRACKETED_PASTE in self.mode

    def set_mode(self, *modes, **kwargs) -> None:
        if kwargs.get("private"):
            alt = [m for m in modes if m in _ALT_SCREEN_MODES]
            if alt:
                self._enter_alt()
                self.mode.update(m << 5 for m in alt)
                modes = tuple(m for m in modes if m not in _ALT_SCREEN_MODES)
                if not modes:
                    return
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs) -> None:
        if kwargs.get("private"):
            alt = [m for m in modes if m in _ALT_SCREEN_MODES]
            if alt:
                self._exit_alt()
                self.mode.difference_update(m << 5 for m in _ALT_SCREEN_MODES)
                modes = tuple(m for m in modes if m not in _ALT_SCREEN_MODES)
                if not modes:
                    return
        super().reset_mode(*modes, **kwargs)

    def _enter_alt(self) -> None:
        if self._alt is not None:
            return
        rows = [self.buffer[y] for y in range(self.lines)]
        self._alt = (rows, copy.copy(self.cursor), self.margins)
        self.buffer.clear()
        self.margins = None
        self.cursor_position()
        self.dirty.update(range(self.lines))

    def _exit_alt(self) -> None:
        if self._alt is None:
            return
        rows, cursor, margins = self._alt
        self._alt = None
        shift = 0
        if len(rows) > self.lines:
            # the window shrank while the full-screen program was up: the rows
            # that no longer fit scroll off into the scrollback, as they would
            # have on the main screen
            shift = len(rows) - self.lines
            for line in rows[:shift]:
                self._push_history(line)
            rows = rows[shift:]
        self.buffer.clear()
        for y, line in enumerate(rows):
            self.buffer[y] = line
        self.cursor = cursor
        self.cursor.y = max(0, min(self.cursor.y - shift, self.lines - 1))
        self.cursor.x = max(0, min(self.cursor.x, self.columns - 1))
        # scroll margins belong to the size they were set for
        self.margins = margins if (margins and margins.bottom < self.lines) else None
        self.dirty.update(range(self.lines))

    def reset(self) -> None:
        """Terminal reset (RIS). The scrollback is kept — it is the user's
        record of the session, not terminal state."""
        self._alt = None
        super().reset()

    # ------------------------------------------------------------- scrollback
    def _push_history(self, line) -> None:
        history = self.history
        if history.limit <= 0:
            return
        history.top.append(line)
        history.pushed += 1
        if len(history.top) > history.limit + _TRIM_SLACK:
            del history.top[:len(history.top) - history.limit]

    def clear_history(self) -> None:
        self.history.top.clear()

    def index(self) -> None:
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0 and self._alt is None:
            self._push_history(self.buffer[0])
        super().index()

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        if how == 3 and self._alt is None:
            self.clear_history()         # ESC[3J: what `clear` sends to drop scrollback
        super().erase_in_display(how, *args, **kwargs)

    # ------------------------------------------------------------- drawing
    def draw(self, data: str) -> None:
        # Fast path: printable ASCII, replace mode, default charset. Every such
        # character is one cell wide and maps to itself, so the loop pyte runs
        # per character collapses to "copy cached cells into the line".
        if (self.charset or self.g0_charset is not cs.LAT1_MAP
                or mo.IRM in self.mode
                or not (data.isascii() and data.isprintable())):
            super().draw(data)
            return
        cursor = self.cursor
        columns = self.columns
        attrs = cursor.attrs
        cells = self._cells.get(attrs)
        if cells is None:
            if len(self._cells) > 256:   # bound it: SGR combinations are few
                self._cells.clear()
            cells = self._cells[attrs] = {}
        pos, end = 0, len(data)
        while pos < end:
            if cursor.x >= columns:
                if mo.DECAWM in self.mode:
                    self.dirty.add(cursor.y)
                    self.carriage_return()
                    self.linefeed()
                else:
                    cursor.x = columns - 1      # no wrap: overwrite the last cell
            take = min(end - pos, columns - cursor.x)
            line = self.buffer[cursor.y]
            x = cursor.x
            for ch in data[pos:pos + take]:
                cell = cells.get(ch)
                if cell is None:
                    cell = cells[ch] = attrs._replace(data=ch)
                line[x] = cell
                x += 1
            cursor.x = x
            pos += take
        self.dirty.add(cursor.y)

    # ------------------------------------------------------------- resize
    def resize_keeping_cursor(self, new_rows: int, new_cols: int) -> None:
        """Resize like a real terminal: the cursor line stays visible, lines that
        no longer fit slide into the scrollback, and lines come back out of it
        when the window grows. (pyte's own resize clips from the top and never
        moves the cursor, which stranded or blanked the prompt.)

        On the alternate screen the program redraws itself after the size
        change, so the plain resize is right there.
        """
        if self._alt is not None:
            pyte.Screen.resize(self, new_rows, new_cols)
            return
        old_rows = self.lines
        cur_y = max(0, min(self.cursor.y, old_rows - 1))
        cur_x = self.cursor.x
        top = self.history.top
        rows = top + [self.buffer[y] for y in range(old_rows)]
        cur_abs = len(top) + cur_y
        start = 0 if cur_abs < new_rows else cur_abs - (new_rows - 1)
        window = rows[start:start + new_rows]
        pyte.Screen.resize(self, new_rows, new_cols)
        top[:] = rows[:start]
        for y in range(new_rows):
            if y < len(window):
                self.buffer[y] = window[y]
            else:
                self.buffer.pop(y, None)        # fresh blank line on next access
        self.cursor.y = max(0, min(cur_abs - start, new_rows - 1))
        self.cursor.x = max(0, min(cur_x, new_cols - 1))
        self.dirty.update(range(new_rows))

    # ------------------------------------------------------------- reading
    @property
    def total_rows(self) -> int:
        """Scrollback lines + the visible screen."""
        return len(self.history.top) + self.lines

    def row(self, index: int):
        """Line *index* counted from the oldest scrollback line; None if out of
        range."""
        top = self.history.top
        if 0 <= index < len(top):
            return top[index]
        y = index - len(top)
        if 0 <= y < self.lines:
            return self.buffer[y]
        return None

    def row_text(self, index: int, first: int = 0, last: Optional[int] = None) -> str:
        """Text of a row between columns [first, last) — for selection / copy."""
        line = self.row(index)
        if line is None:
            return ""
        last = self.columns if last is None else min(last, self.columns)
        return "".join(line[x].data for x in range(max(0, first), last))

    def text_range(self, start: tuple, end: tuple) -> str:
        """Text between two (row, column) positions, inclusive of *end*. Trailing
        blanks are dropped per line, as a terminal does when copying."""
        if start > end:
            start, end = end, start
        (r0, c0), (r1, c1) = start, end
        out: List[str] = []
        for r in range(r0, r1 + 1):
            first = c0 if r == r0 else 0
            last = c1 + 1 if r == r1 else None
            out.append(self.row_text(r, first, last).rstrip())
        return "\n".join(out)
