"""The terminal widget inside the real main window: which keys reach the shell,
selection / copy / paste, and that painting works."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("pyte")

from PyQt5.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402


@pytest.fixture()
def window(qapp):
    """The real MainWindow with its slots replaced by recorders (nothing modal
    opens), holding one fake session tab whose terminal records what it sends."""
    from turbossh.gui.main_window import MainWindow
    from turbossh.gui.vt100 import Vt100Terminal

    actions = []

    class Window(MainWindow):
        def new_session(self, *a, **k):
            actions.append("new session")

        def _close_current_tab(self):
            actions.append("close tab")

        def _save_current_output(self):
            actions.append("save output")

        def closeEvent(self, event):
            actions.append("quit")
            event.ignore()

    win = Window()
    win.resize(1100, 700)
    win.show()
    sent = []
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    term = Vt100Terminal(send_fn=sent.append)
    layout.addWidget(term)
    page.term = term
    win.tabs.addTab(page, "fake session")
    win.tabs.setCurrentWidget(page)
    win.activateWindow()
    QApplication.setActiveWindow(win)
    QTest.qWait(250)                     # let the debounced terminal resize settle
    term.setFocus()
    QTest.qWait(50)
    sent.clear()
    win.actions, win.sent, win.term = actions, sent, term
    yield win
    term.cleanup()
    win.hide()
    win.deleteLater()


def press(win, key, mods=Qt.NoModifier):
    win.actions.clear()
    win.sent.clear()
    QTest.keyClick(win.term, key, mods)
    QApplication.processEvents()
    return b"".join(win.sent), list(win.actions)


@pytest.mark.parametrize("key, expected", [
    (Qt.Key_A, b"\x01"), (Qt.Key_W, b"\x17"), (Qt.Key_N, b"\x0e"),
    (Qt.Key_T, b"\x14"), (Qt.Key_S, b"\x13"), (Qt.Key_Q, b"\x11"),
    (Qt.Key_R, b"\x12"), (Qt.Key_L, b"\x0c"), (Qt.Key_C, b"\x03"),
])
def test_ctrl_keys_reach_the_shell_not_the_window(window, key, expected):
    """Ctrl+W used to close the tab, Ctrl+Q quit the app, Ctrl+S / Ctrl+T open
    dialogs and Ctrl+N do nothing at all."""
    sent, actions = press(window, key, Qt.ControlModifier)
    assert sent == expected
    assert actions == []


def test_window_shortcuts_live_on_ctrl_shift(window):
    sent, actions = press(window, Qt.Key_W, Qt.ControlModifier | Qt.ShiftModifier)
    assert actions == ["close tab"] and sent == b""
    sent, actions = press(window, Qt.Key_T, Qt.ControlModifier | Qt.ShiftModifier)
    assert actions == ["new session"] and sent == b""


def test_ctrl_n_works_when_the_terminal_does_not_have_focus(window):
    """It was registered twice (menu + QShortcut), which Qt treats as ambiguous
    and fires neither."""
    window.session_list.setFocus()
    QApplication.processEvents()
    window.actions.clear()
    QTest.keyClick(window.session_list, Qt.Key_N, Qt.ControlModifier)
    QApplication.processEvents()
    assert window.actions == ["new session"]


@pytest.mark.parametrize("key, mods, expected", [
    (Qt.Key_F1, Qt.NoModifier, b"\x1bOP"),
    (Qt.Key_F5, Qt.NoModifier, b"\x1b[15~"),
    (Qt.Key_F10, Qt.NoModifier, b"\x1b[21~"),
    (Qt.Key_F12, Qt.NoModifier, b"\x1b[24~"),
    (Qt.Key_Insert, Qt.NoModifier, b"\x1b[2~"),
    (Qt.Key_Delete, Qt.NoModifier, b"\x1b[3~"),
    (Qt.Key_Backspace, Qt.NoModifier, b"\x7f"),
    (Qt.Key_Up, Qt.NoModifier, b"\x1b[A"),
    (Qt.Key_Left, Qt.ControlModifier, b"\x1b[1;5D"),
    (Qt.Key_Backtab, Qt.ShiftModifier, b"\x1b[Z"),
    (Qt.Key_F, Qt.AltModifier, b"\x1bf"),
])
def test_xterm_key_encoding(window, key, mods, expected):
    sent, _ = press(window, key, mods)
    assert sent == expected


def test_cursor_keys_switch_to_application_mode(window):
    window.term.feed(b"\x1b[?1h")
    sent, _ = press(window, Qt.Key_Up)
    assert sent == b"\x1bOA"
    window.term.feed(b"\x1b[?1l")
    sent, _ = press(window, Qt.Key_Up)
    assert sent == b"\x1b[A"


def test_cursor_position_query_is_answered_on_the_wire(window):
    window.sent.clear()
    window.term.feed(b"abc\x1b[6n")
    assert b"".join(window.sent) == b"\x1b[1;4R"


def test_drag_selects_and_ctrl_c_copies_instead_of_interrupting(window):
    term = window.term
    term.feed(b"\x1b[2J\x1b[Hhello world\r\nsecond line\r\n")
    QApplication.processEvents()
    y = term.ch // 2
    QTest.mousePress(term, Qt.LeftButton, Qt.NoModifier, QPoint(6 * term.cw + 1, y))
    # QTest.mouseMove only moves the cursor (no event on the offscreen platform),
    # so deliver the drag as the event a real mouse would produce
    end = QPoint(10 * term.cw + 1, y)
    QApplication.sendEvent(term, QMouseEvent(QEvent.MouseMove, end, Qt.NoButton,
                                             Qt.LeftButton, Qt.NoModifier))
    QTest.mouseRelease(term, Qt.LeftButton, Qt.NoModifier, end)
    assert term.selected_text() == "world"
    sent, _ = press(window, Qt.Key_C, Qt.ControlModifier)
    assert sent == b"", "Ctrl+C with a selection must copy, not send ^C"
    assert QApplication.clipboard().text() == "world"
    # selection is gone now, so the next Ctrl+C is the interrupt again
    sent, _ = press(window, Qt.Key_C, Qt.ControlModifier)
    assert sent == b"\x03"


def test_double_click_selects_a_word(window):
    term = window.term
    term.feed(b"\x1b[2J\x1b[Halpha beta gamma\r\n")
    QTest.mouseDClick(term, Qt.LeftButton, Qt.NoModifier,
                      QPoint(7 * term.cw + 1, term.ch // 2))
    assert term.selected_text() == "beta"


def test_paste_sends_carriage_returns_and_honours_bracketed_mode(window):
    term = window.term
    window.sent.clear()
    term.paste_text("one\r\ntwo\n", confirm=False)
    assert b"".join(window.sent) == b"one\rtwo\r"
    window.sent.clear()
    term.feed(b"\x1b[?2004h")
    term.paste_text("a\nb")              # bracketed: no confirmation needed
    assert b"".join(window.sent) == b"\x1b[200~a\rb\x1b[201~"


def test_clear_keeps_the_scrollback(window):
    term = window.term
    for i in range(term.rows + 20):
        term.feed(b"line %d\r\n" % i)
    before = len(term.screen.history.top)
    assert before >= 20
    term.clear()
    assert len(term.screen.history.top) >= before
    assert all(not row.strip() for row in term.screen.display)


def test_painting_a_full_screen_does_not_raise_and_is_fast_enough(window):
    import time
    term = window.term
    line = ("Jan 01 12:00:00 host \x1b[32mservice\x1b[0m[1234]: error "
            + "payload text " * 20)
    for _ in range(term.rows + 5):
        term.feed((line[:term.cols - 1] + "\r\n").encode())
    t0 = time.perf_counter()
    for _ in range(5):
        pixmap = term.grab()
    per_frame = (time.perf_counter() - t0) / 5
    assert not pixmap.isNull()
    # the old per-cell painter took ~37 ms for this grid; allow lots of headroom
    assert per_frame < 0.030, f"{per_frame * 1000:.1f} ms per frame"


def test_reader_reports_what_it_had_to_drop():
    from turbossh.gui.terminal import ReaderThread
    chunks = [b"x" * 600, b"y" * 600, None]
    reader = ReaderThread(lambda: chunks.pop(0), cap=1000)
    reader.run()                          # run inline: no thread needed
    assert reader.take_dropped() == 200
    assert reader.take_dropped() == 0
    assert len(reader.pull()) == 1000
