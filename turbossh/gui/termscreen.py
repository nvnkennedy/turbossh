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
  mode checks and a ``namedtuple._replace`` for every character; in replace mode
  with the default charset the result is fully determined by (attributes, char),
  so cells are taken from a cache instead. This is what makes a log flood
  usable — and it means a cell is shared, not one object per character.
* **Compact scrollback.** A screen line is a dict of column -> cell (~10 KB for
  a full line). A line that scrolls off becomes a :class:`Row`, a tuple of
  shared cells (~1.6 KB), so 10k lines of scrollback is ~20 MB, not 100-270 MB.
"""

from __future__ import annotations

import copy
import unicodedata
from typing import Callable, List, Optional

import pyte
import pyte.charsets as cs
import pyte.modes as mo
from pyte.screens import Char, Margins, StaticDefaultDict, wcwidth

_ALT_SCREEN_MODES = (47, 1047, 1049)
DECCKM = 1 << 5             # application cursor keys (private mode 1, as pyte stores it)
BRACKETED_PASTE = 2004 << 5
_TRIM_SLACK = 1000          # scrollback is trimmed in batches of this many lines
_BLANK = Char(" ")          # pyte's default cell
_tuple_item = tuple.__getitem__


class Row(tuple):
    """A scrollback line: its cells, up to the last one written, as a tuple.

    Read like a screen line (``row[x]``): a column past the end is a blank. It
    never changes — a line that comes back onto the screen (the window grew) is
    turned back into a dict first — so a save on a worker thread can read it."""

    __slots__ = ()

    def __getitem__(self, x):
        try:
            return _tuple_item(self, x)
        except IndexError:
            return _BLANK


_EMPTY = Row()
_COLUMN_KEYS: dict = {}     # n -> (0, 1, …, n-1)


def compact(line) -> Row:
    """*line* (a screen line: dict of column -> cell) as a :class:`Row`."""
    if type(line) is Row:
        return line
    n = len(line)
    if not n:
        return _EMPTY
    keys = tuple(line)
    in_order = _COLUMN_KEYS.get(n)
    if in_order is None and n <= 4096:
        in_order = _COLUMN_KEYS[n] = tuple(range(n))
    if keys == in_order:
        # written left to right without gaps (nearly every line): the values
        # already are the row — a third of the cost of looking each one up
        return Row(line.values())
    # a gap reads as the line's own blank; past the end, a row reads as pyte's
    return Row(map(line.__getitem__, range(max(keys) + 1)))


def rows_text(rows) -> str:
    """Plain text of *rows* (screen lines or :class:`Row`), trailing blanks
    dropped — for saving what the terminal holds."""
    out = []
    for row in rows:
        if type(row) is Row:
            out.append("".join([cell.data for cell in row]).rstrip())
        elif row:
            out.append("".join([row[x].data for x in range(max(row) + 1)]).rstrip())
        else:
            out.append("")
    return "\n".join(out)


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
        history.top.append(compact(line))
        history.pushed += 1
        if len(history.top) > history.limit + _TRIM_SLACK:
            del history.top[:len(history.top) - history.limit]

    def clear_history(self) -> None:
        self.history.top.clear()

    def _as_line(self, row):
        """A :class:`Row` coming back onto the screen, as a writable line."""
        if type(row) is not Row:
            return row
        line = StaticDefaultDict(self.default_char)
        line.update(enumerate(row))
        return line

    def index(self) -> None:
        # pyte's own index, with the rows shifted by C loops instead of a Python
        # one: a flood of short lines is one index per line, and this is O(rows)
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y != bottom:
            self.cursor_down()
            return
        buffer = self.buffer
        if top == 0 and self._alt is None:
            self._push_history(buffer[0])
        self.dirty.update(range(self.lines))
        # sequential: row y is read before it is overwritten by row y + 1
        buffer.update(zip(range(top, bottom),
                          map(buffer.__getitem__, range(top + 1, bottom + 1))))
        buffer.pop(bottom, None)

    # pyte raises UnboundLocalError for a mode it does not know (ESC[3K, ESC[5K,
    # ESC[4J), and the rest of the data being fed is lost with it. A terminal
    # ignores an unknown mode.
    def erase_in_line(self, how: int = 0, *args, **kwargs) -> None:
        if how in (0, 1, 2):
            super().erase_in_line(how, *args, **kwargs)

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        if how not in (0, 1, 2, 3):
            return
        if how == 3 and self._alt is None:
            self.clear_history()         # ESC[3J: what `clear` sends to drop scrollback
        super().erase_in_display(how, *args, **kwargs)

    # ------------------------------------------------------------- drawing
    def _cells_for(self, attrs) -> dict:
        cells = self._cells.get(attrs)
        if cells is None:
            if len(self._cells) > 256:   # bound it: SGR combinations are few
                self._cells.clear()
            cells = self._cells[attrs] = {}
        elif len(cells) > 4096:          # and the characters drawn in one style
            cells.clear()
        return cells

    def draw(self, data: str) -> None:
        if self.charset or self.g0_charset is not cs.LAT1_MAP or mo.IRM in self.mode:
            super().draw(data)           # another charset / insert mode: rare
            return
        if not (data.isascii() and data.isprintable()):
            self._draw_unicode(data)
            return
        # Fast path: printable ASCII, replace mode, default charset. Every such
        # character is one cell wide and maps to itself, so the loop pyte runs
        # per character collapses to "copy cached cells into the line".
        cursor = self.cursor
        columns = self.columns
        attrs = cursor.attrs
        cells = self._cells_for(attrs)
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
            chunk = data[pos:pos + take]
            row = list(map(cells.get, chunk))
            if not all(row):             # a character not drawn in this style yet
                for i, ch in enumerate(chunk):
                    if row[i] is None:
                        row[i] = cells.get(ch) or cells.setdefault(
                            ch, attrs._replace(data=ch))
            x = cursor.x
            self.buffer[cursor.y].update(zip(range(x, x + take), row))
            cursor.x = x + take
            pos += take
        self.dirty.add(cursor.y)

    def _draw_unicode(self, data: str) -> None:
        """pyte's ``draw`` loop, step for step, for text beyond printable ASCII
        (accents, symbols, CJK) — except that a cell comes from the cache. pyte
        made a new cell object for every character drawn, ~27 KB per line."""
        cursor = self.cursor
        columns = self.columns
        buffer = self.buffer
        attrs = cursor.attrs
        cells = self._cells_for(attrs)
        for char in data:
            width = wcwidth(char)
            if cursor.x == columns:
                if mo.DECAWM in self.mode:
                    self.dirty.add(cursor.y)
                    self.carriage_return()
                    self.linefeed()
                elif width > 0:
                    cursor.x -= width
            line = buffer[cursor.y]
            if width == 1 or width == 2:
                cell = cells.get(char)
                if cell is None:
                    cell = cells[char] = attrs._replace(data=char)
                line[cursor.x] = cell
                if width == 2 and cursor.x + 1 < columns:
                    stub = cells.get("")         # a wide character's second cell
                    if stub is None:
                        stub = cells[""] = attrs._replace(data="")
                    line[cursor.x + 1] = stub
            elif width == 0 and unicodedata.combining(char):
                # a combining mark joins the character before it (rare: as pyte)
                if cursor.x:
                    last = line[cursor.x - 1]
                    line[cursor.x - 1] = last._replace(
                        data=unicodedata.normalize("NFC", last.data + char))
                elif cursor.y:
                    above = buffer[cursor.y - 1]
                    last = above[columns - 1]
                    above[columns - 1] = last._replace(
                        data=unicodedata.normalize("NFC", last.data + char))
            else:
                break                    # unprintable: pyte stops here too
            if width > 0:
                cursor.x = min(cursor.x + width, columns)
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
        top[:] = [compact(line) for line in rows[:start]]
        for y in range(new_rows):
            if y < len(window):
                self.buffer[y] = self._as_line(window[y])
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

    def cells(self, line) -> list:
        """The ``columns`` cells of a screen line or :class:`Row`, for painting."""
        columns = self.columns
        if type(line) is Row:
            out = list(_tuple_item(line, slice(0, columns)))
            if len(out) < columns:
                out.extend([_BLANK] * (columns - len(out)))
            return out
        return list(map(line.__getitem__, range(columns)))

    def row_text(self, index: int, first: int = 0, last: Optional[int] = None) -> str:
        """Text of a row between columns [first, last) — for selection / copy."""
        line = self.row(index)
        if line is None:
            return ""
        last = self.columns if last is None else min(last, self.columns)
        return "".join(line[x].data for x in range(max(0, first), last))

    def snapshot(self) -> list:
        """Every row (scrollback + screen) as :class:`Row` objects, which do not
        change afterwards — cheap to take on the GUI thread (the scrollback rows
        are shared, only the screen's are copied), so the text can be built on
        a worker thread with :func:`rows_text`."""
        buffer = self.buffer
        return list(self.history.top) + [compact(buffer.get(y) or _EMPTY)
                                          for y in range(self.lines)]

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
