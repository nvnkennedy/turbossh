"""The Qt-free terminal screen model: alternate screen, replies, scrollback,
the fast text path (must match pyte exactly), and resize."""

from __future__ import annotations

import random

import pytest

pyte = pytest.importorskip("pyte")

from turbossh.gui.termscreen import TermScreen  # noqa: E402


def make(cols=40, rows=6, history=100, reply=None):
    screen = TermScreen(cols, rows, history=history, reply=reply)
    return screen, pyte.ByteStream(screen)


def shown(screen):
    return [line.rstrip() for line in screen.display]


def test_alternate_screen_restores_what_was_there():
    screen, stream = make()
    stream.feed(b"$ ls\r\nfile_a  file_b\r\n$ vim notes.txt\r\n")
    before, cursor = shown(screen), (screen.cursor.x, screen.cursor.y)
    stream.feed(b"\x1b[?1049h\x1b[2J\x1b[H~ VIM LINE 1\r\n~ VIM LINE 2\r\n")
    assert screen.in_alt_screen and shown(screen)[0] == "~ VIM LINE 1"
    stream.feed(b"\x1b[?1049l")
    assert not screen.in_alt_screen
    assert shown(screen) == before
    assert (screen.cursor.x, screen.cursor.y) == cursor


def test_alternate_screen_output_never_reaches_the_scrollback():
    screen, stream = make(rows=4)
    stream.feed(b"one\r\ntwo\r\n")
    kept = len(screen.history.top)
    stream.feed(b"\x1b[?1049h" + b"redraw\r\n" * 50 + b"\x1b[?1049l")
    assert len(screen.history.top) == kept


def test_cursor_position_and_device_queries_are_answered():
    replies = []
    screen, stream = make(reply=replies.append)
    stream.feed(b"abc\r\nde\x1b[6n")
    assert replies == ["\x1b[2;3R"]
    stream.feed(b"\x1b[c")
    assert replies[-1] == "\x1b[?6c"


def test_scrollback_collects_lines_and_is_capped():
    screen, stream = make(rows=3, history=50)
    for i in range(2000):
        stream.feed(b"line %d\r\n" % i)
    top = screen.history.top
    assert screen.history.pushed == 2000 - 2            # all but the visible rows
    assert 50 <= len(top) <= 50 + 1000                  # trimmed in batches
    assert top[-1][0].data == "l"
    assert screen.row_text(len(top) - 1).rstrip() == "line 1997"


def test_scroll_region_does_not_feed_the_scrollback():
    screen, stream = make(rows=6)
    stream.feed(b"\x1b[2;5r\x1b[5;1H" + b"x\r\n" * 20)   # scroll inside rows 2-5
    assert screen.history.top == []


def test_reset_keeps_scrollback_but_clear_scrollback_sequence_drops_it():
    screen, stream = make(rows=3)
    stream.feed(b"a\r\nb\r\nc\r\nd\r\ne\r\n")
    assert screen.history.top
    screen.reset()
    assert screen.history.top
    stream.feed(b"\x1b[3J")
    assert screen.history.top == []


def test_modes_are_visible_to_the_widget():
    screen, stream = make()
    assert not screen.application_cursor_keys and not screen.bracketed_paste
    stream.feed(b"\x1b[?1h\x1b[?2004h")
    assert screen.application_cursor_keys and screen.bracketed_paste
    stream.feed(b"\x1b[?1l\x1b[?2004l")
    assert not screen.application_cursor_keys and not screen.bracketed_paste


def _random_stream(rng, n):
    pieces = []
    for _ in range(n):
        kind = rng.random()
        if kind < 0.60:
            pieces.append("".join(rng.choice(
                "abcdefghij KLMNOP 0123456789 !@#$%^&*()_+-=[]{};:,./<>?")
                for _ in range(rng.randint(1, 70))))
        elif kind < 0.72:
            pieces.append("\r\n")
        elif kind < 0.80:
            pieces.append("\x1b[%dm" % rng.choice([0, 1, 4, 7, 31, 32, 44, 91, 39]))
        elif kind < 0.86:
            pieces.append("\x1b[%d;%dH" % (rng.randint(1, 8), rng.randint(1, 30)))
        elif kind < 0.90:
            pieces.append(rng.choice(["\x1b[K", "\x1b[2K", "\x1b[J", "\b", "\t", "\r"]))
        elif kind < 0.93:
            pieces.append(rng.choice(["\x1b[?7l", "\x1b[?7h", "\x1b[4h", "\x1b[4l"]))
        elif kind < 0.96:
            pieces.append(rng.choice(["é", "日本", "→", "\x0e", "\x0f"]))
        else:
            pieces.append("\x1b[%dL" % rng.randint(1, 3))
    return "".join(pieces).encode("utf-8")


@pytest.mark.parametrize("seed", range(12))
def test_fast_text_path_matches_pyte_exactly(seed):
    """Same bytes into pyte's own Screen and into TermScreen must give the same
    cells, cursor and modes — the fast path is an optimisation, not a dialect."""
    rng = random.Random(seed)
    data = _random_stream(rng, 400)
    reference = pyte.Screen(30, 8)
    pyte.ByteStream(reference).feed(data)
    screen, stream = make(cols=30, rows=8)
    stream.feed(data)
    assert screen.display == reference.display
    for y in range(8):
        for x in range(30):
            assert screen.buffer[y][x] == reference.buffer[y][x], (seed, y, x)
    assert (screen.cursor.x, screen.cursor.y) == (reference.cursor.x, reference.cursor.y)
    assert screen.mode == reference.mode


def test_resize_keeps_the_cursor_line_and_moves_lines_through_scrollback():
    screen, stream = make(cols=20, rows=6)
    for i in range(5):
        stream.feed(b"row %d\r\n" % i)
    stream.feed(b"prompt$ ")
    screen.resize_keeping_cursor(3, 20)                 # shrink: old rows scroll off
    assert shown(screen)[-1] == "prompt$" and screen.cursor.y == 2
    assert [screen.row_text(i).rstrip() for i in range(len(screen.history.top))] == \
        ["row 0", "row 1", "row 2"]
    screen.resize_keeping_cursor(6, 20)                 # grow: they come back
    assert shown(screen) == ["row 0", "row 1", "row 2", "row 3", "row 4", "prompt$"]
    assert screen.history.top == []


def test_resize_while_a_full_screen_program_is_up_then_exit():
    screen, stream = make(cols=20, rows=6)
    for i in range(5):
        stream.feed(b"row %d\r\n" % i)
    stream.feed(b"$ less f")
    stream.feed(b"\x1b[?1049h\x1b[Hpager")
    screen.resize_keeping_cursor(3, 20)
    assert len(screen.history.top) == 0, "alt-screen resize must not touch scrollback"
    stream.feed(b"\x1b[?1049l")
    assert shown(screen) == ["row 3", "row 4", "$ less f"]
    assert screen.cursor.y == 2
    assert len(screen.history.top) == 3


def test_text_range_for_copy():
    screen, stream = make(cols=20, rows=4)
    stream.feed(b"hello world\r\nsecond line\r\nthird")
    assert screen.text_range((0, 6), (1, 5)) == "world\nsecond"
    assert screen.text_range((1, 5), (0, 6)) == "world\nsecond"   # reversed drag
    assert screen.text_range((2, 0), (2, 19)) == "third"


def test_throughput_is_far_above_plain_pyte():
    """Not a benchmark, a floor: the fast path has to beat pyte's HistoryScreen
    by a wide margin on log-like text or the GUI stalls under a flood."""
    import time
    line = (b"Jan 01 12:00:00 host \x1b[32mservice\x1b[0m[1234]: "
            + b"payload " * 12 + b"\r\n")
    blob = line * 400
    slow = pyte.HistoryScreen(160, 45, history=10000, ratio=0.5)
    slow_stream = pyte.ByteStream(slow)
    t0 = time.perf_counter(); slow_stream.feed(blob); slow_dt = time.perf_counter() - t0
    screen, stream = make(cols=160, rows=45, history=10000)
    t0 = time.perf_counter(); stream.feed(blob); fast_dt = time.perf_counter() - t0
    assert fast_dt * 3 < slow_dt, (fast_dt, slow_dt)
