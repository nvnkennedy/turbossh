"""Themes, toasts, the status summary, the local-terminal entry point, and the
log viewer's colouring and find."""

from __future__ import annotations

import re
import time

import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("pyte")

from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


# ------------------------------------------------------------------ themes
def test_every_theme_defines_every_token_and_builds_a_stylesheet(qapp):
    from turbossh.gui import theme
    tokens = set(theme.THEMES["dark"])
    assert len(theme.THEME_NAMES) >= 8
    for name in theme.THEME_NAMES:
        palette = theme.THEMES[name]
        assert set(palette) == tokens, name
        assert all(re.fullmatch(r"#[0-9a-fA-F]{6}", v) for v in palette.values()), name
        sheet = theme.stylesheet(name)
        assert palette["win"] in sheet and palette["text"] in sheet
        assert theme.log_colors(name)["ERROR"]


def test_theme_toggle_pairs_dark_with_light():
    from turbossh.gui import theme
    for name in theme.THEME_NAMES:
        other = theme.counterpart(name)
        assert other in theme.THEMES
        assert theme.is_light(other) != theme.is_light(name), name
    assert theme.counterpart("dark") == "light" and theme.counterpart("light") == "dark"
    assert theme.counterpart("nord") == "nord-light"
    assert theme.counterpart("nord-light") == "nord"
    assert theme.accent_text("paper") == theme.ACCENT_DARK      # readable on light


# ------------------------------------------------------------------ window
@pytest.fixture()
def window(qapp):
    from turbossh.gui.main_window import MainWindow

    class Window(MainWindow):
        def closeEvent(self, event):
            event.accept()

    win = Window()
    win.resize(1000, 650)
    win.show()
    yield win
    for widget in win._all_sessions():
        widget.close_session()
    win.hide()
    win.deleteLater()
    QTest.qWait(50)


def test_toasts_stack_in_the_corner_and_are_capped(window):
    host = window._toasts
    for i in range(6):
        host.show(f"message {i}", "info" if i % 2 else "error")
    assert len(host.toasts) == 4, "old toasts must make room"
    assert [t.label.text() for t in host.toasts] == [f"message {i}" for i in range(2, 6)]
    for toast in host.toasts:
        assert toast.isVisible()
        assert 0 <= toast.x() and toast.x() + toast.width() <= window.width()
        assert toast.y() + toast.height() <= window.height()
    lowest = host.toasts[-1]
    assert all(t.y() < lowest.y() for t in host.toasts[:-1])   # newest at the bottom
    host.dismiss(lowest)
    assert len(host.toasts) == 3
    host.clear()
    assert host.toasts == []
    assert host.show("", "ok") is None


def test_notify_reaches_the_log_the_status_bar_and_a_toast(window):
    window.log_panel.clear()
    window.notify("Transfer finished", "ok")
    assert "Transfer finished" in window.log_panel.view.toPlainText()
    assert window.statusBar().currentMessage() == "Transfer finished"
    assert window._toasts.toasts[-1].kind == "ok"
    window.notify("already logged elsewhere", "error", log=False)
    assert "already logged elsewhere" not in window.log_panel.view.toPlainText()
    assert window._toasts.toasts[-1].kind == "error"
    # a failed connection is a toast now, never a modal dialog
    window._on_session_failed("box: timed out")
    assert "Connection failed" in window._toasts.toasts[-1].label.text()
    window._toasts.clear()


def test_local_terminal_opens_in_a_tab_and_the_summary_counts_it(window):
    assert window._summary.text() == ""
    window.open_local_terminal()
    page = window.tabs.currentWidget()
    assert type(page).__name__ == "LocalTerminalWidget"
    assert "1 open session" in window._summary.text()
    assert window._current_term() is page.term
    window._close_tab(window.tabs.indexOf(page))
    assert window._summary.text() == ""


# ------------------------------------------------------------------ log viewer
def test_log_lines_are_coloured_by_level(qapp):
    from turbossh.gui.logs_tab import LogsTab
    tab = LogsTab()
    tab._accept(["kernel: all fine", "sshd[1]: error: bad key", "WARNING: disk 91%",
                 "DEBUG handshake ok"])
    QApplication.processEvents()
    doc = tab.view.document()

    def colour(row):
        formats = doc.findBlockByNumber(row).layout().additionalFormats()
        return formats[0].format.foreground().color().name() if formats else None

    assert colour(0) is None                      # plain lines keep the default
    assert colour(1) == "#ff7a6e"
    assert colour(2) == "#ffc34d"
    assert colour(3) == "#7d8794"                 # "debug" wins over the "ok" in it
    tab.close_tab()
    tab.deleteLater()


def test_find_in_log_walks_matches_and_wraps(qapp):
    from turbossh.gui.logs_tab import LogsTab
    tab = LogsTab()
    tab._accept(["alpha needle", "beta", "gamma NEEDLE", "delta"])

    def at():
        return tab.view.textCursor().blockNumber()

    cursor = tab.view.textCursor()
    cursor.movePosition(cursor.Start)
    tab.view.setTextCursor(cursor)
    tab.find.setText("needle")
    assert tab.find_next() and at() == 0
    assert tab.find_next() and at() == 2          # case-insensitive by default
    assert tab.find_next() and at() == 0 and tab._find_status.text() == "wrapped"
    assert tab.find_previous() and at() == 2
    tab.find_case.setChecked(True)
    assert tab.find_previous() and at() == 0      # only the lower-case one now
    tab.find.setText("absent")
    assert tab.find_next() is False and tab._find_status.text() == "not found"
    tab.close_tab()
    tab.deleteLater()


def test_toast_goes_away_by_itself(window, monkeypatch):
    from turbossh.gui import toast
    monkeypatch.setitem(toast.KINDS, "info", ("#fff", "#1f6feb", 0.2))
    window._toasts.show("brief", "info")
    assert len(window._toasts.toasts) == 1
    deadline = time.monotonic() + 3
    while window._toasts.toasts and time.monotonic() < deadline:
        QTest.qWait(30)
    assert window._toasts.toasts == []
