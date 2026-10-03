"""GUI behaviour above the terminal: sessions end to end against the loopback
server, tab lifetime, split view, the session dialog, logs tab, error popup."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("pyte")

from PyQt5 import sip  # noqa: E402
from PyQt5.QtCore import QEvent  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(25)
    return predicate()


def screen_text(term):
    return "\n".join(term.screen.display)


# ------------------------------------------------------------------ config
def test_blank_gui_password_means_keys_not_an_empty_password():
    """The dialog says "leave blank … uses keys"; a blank field used to be sent
    as an empty password with key lookup switched off."""
    from turbossh import SSHHandler
    from turbossh.gui.session_widgets import config_from_session
    session = {"host": "h", "user": "u", "use_jump": True, "jhost": "j"}
    cfg = config_from_session(session, "", "")
    assert cfg.password is None and cfg.jump_host.password is None
    kwargs = SSHHandler(cfg, quiet=True)._build_connect_kwargs(
        empty_password=False, sock=None)
    assert kwargs["look_for_keys"] is True and kwargs["allow_agent"] is True
    assert "password" not in kwargs

    cfg = config_from_session(dict(session, key_file="~/.ssh/id_x"), "pass", "")
    assert cfg.normalized_key_files() and cfg.password.reveal() == "pass"


# ------------------------------------------------------------------ sessions
def _session(port, **extra):
    data = {"name": "loop", "type": "ssh", "host": "127.0.0.1", "port": port,
            "user": "u", "ignore_hostkey": True}
    data.update(extra)
    return data


def test_ssh_session_connects_types_and_closes(qapp, ssh_server):
    from turbossh.gui import qtutil
    from turbossh.gui.session_widgets import SshSessionWidget
    logged = []
    widget = SshSessionWidget(_session(ssh_server.port), "good", "")
    widget.log.connect(logged.append)
    widget.show()
    assert wait_until(lambda: widget.status.text().startswith("Connected"))
    handler = widget.handler
    assert handler.known_os == "linux"               # probed off the GUI thread
    assert "loop" in screen_text(widget.term)         # the welcome banner

    widget._send(b"hello-from-test")
    assert wait_until(lambda: "hello-from-test" in screen_text(widget.term))
    big = b"x" * 100_000                              # > one SSH packet
    widget._send(big + b"END")
    assert wait_until(lambda: "END" in screen_text(widget.term))

    # the engine's own log lines reach the GUI log now
    assert any("Connected to" in line for line in logged)

    # SFTP tab was filled in
    assert type(widget.sftp_page).__name__ == "FileBrowser"

    widget.close_session()
    widget.close_session()                            # twice is fine
    assert qtutil.wait_for_teardowns(10)
    assert not handler.is_connected
    widget.deleteLater()
    QTest.qWait(100)


def test_failed_connect_is_reported_in_the_tab(qapp, ssh_server):
    from turbossh.gui.session_widgets import SshSessionWidget
    failures = []
    widget = SshSessionWidget(_session(ssh_server.port), "WRONG", "")
    widget.failed.connect(failures.append)
    assert wait_until(lambda: widget.status.text() == "Connect failed")
    assert failures and "connect failed" in screen_text(widget.term)
    assert ssh_server.server.auth_log == ["WRONG"]
    widget.close_session()
    widget.deleteLater()


def test_closing_a_tab_mid_connect_closes_what_the_connect_opened(qapp, ssh_server):
    from turbossh.gui import qtutil
    from turbossh.gui.session_widgets import SshSessionWidget
    widget = SshSessionWidget(_session(ssh_server.port), "good", "")
    widget.close_session()                            # before the connect returns
    widget.deleteLater()
    QTest.qWait(50)
    assert wait_until(lambda: len(ssh_server.transports) >= 1)
    assert wait_until(lambda: qtutil.wait_for_teardowns(0.1)
                      and not any(t.is_active() for t in ssh_server.transports), 20)


def test_logs_tab_streams_and_refilters_what_already_arrived(qapp, ssh):
    from turbossh.gui.logs_tab import LogsTab
    tab = LogsTab()
    tab.set_handler(ssh)
    tab.cmd.setEditText("echo")
    tab._start()
    assert wait_until(lambda: "ok" in tab.view.toPlainText())
    assert wait_until(lambda: tab._thread is None)    # command ended by itself
    assert not tab._timer.isActive()

    tab._accept(["alpha error", "beta ok", "gamma error"])
    tab.filter.setText("error")
    tab._apply_filter()
    assert tab.view.toPlainText().splitlines() == ["alpha error", "gamma error"]
    tab.filter.setText("")                            # lines were kept, not lost
    tab._apply_filter()
    assert "beta ok" in tab.view.toPlainText()
    tab.filter.setText("(")
    tab._apply_filter()
    assert "not a valid regex" in tab.statusrow.text()
    tab.close_tab()
    tab.deleteLater()


# ------------------------------------------------------------------ main window
@pytest.fixture()
def window(qapp):
    from turbossh.gui.main_window import MainWindow

    class Window(MainWindow):
        def closeEvent(self, event):
            event.accept()

    win = Window()
    win.show()
    yield win
    win.hide()
    win.deleteLater()
    QTest.qWait(50)


class _FakeSession:
    """Stands in for a session widget: records close_session."""

    def __new__(cls, *args, **kwargs):
        from PyQt5.QtWidgets import QWidget

        class Fake(QWidget):
            closed = 0

            def close_session(self):
                type(self).closed += 1
        return Fake()


def test_closing_a_tab_deletes_its_widget(window):
    page = _FakeSession()
    window.tabs.addTab(page, "x")
    index = window.tabs.indexOf(page)
    window._close_tab(index)
    # deleteLater() is only honoured by an event loop; deliver it by hand here
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    assert type(page).closed == 1
    assert sip.isdeleted(page), "a closed tab was only hidden, not deleted"


def test_menu_actions_find_the_session_in_split_view(window):
    from turbossh.gui.vt100 import Vt100Terminal
    panes = []
    for i in range(2):
        page = _FakeSession()
        page.term = Vt100Terminal(send_fn=lambda data: None, parent=page)
        window.tabs.addTab(page, f"s{i}")
        panes.append(page)
    home = window._home
    window._close_tab(window.tabs.indexOf(home))      # Home can't be tiled
    window.toggle_split()
    assert window._tiled and window.tabs.count() == 0
    assert window._current_session_widget() in panes  # used to be None
    assert window._current_term() is not None
    panes[1].term.setFocus()
    QApplication.processEvents()
    window._close_current_tab()
    assert len(window._tiled_items) == 1
    window._untile()
    for page in panes:
        page.term.cleanup()


def test_session_dialog_round_trips_the_key_file(qapp):
    from turbossh.gui.session_dialog import SessionDialog
    dlg = SessionDialog(None, existing={"name": "k", "type": "ssh", "host": "h",
                                        "user": "u", "key_file": "C:/keys/id"})
    assert dlg.key_file.text() == "C:/keys/id"
    assert dlg.result_session()["key_file"] == "C:/keys/id"
    dlg.done(0)
    dlg.deleteLater()


def test_one_error_box_however_many_errors(qapp):
    from turbossh.gui import app as app_module

    class Relay(app_module._ErrorRelay):
        shown = 0

        def _present(self, box):         # QMessageBox.show() needs a real display
            Relay.shown += 1

    relay = Relay()
    for i in range(5):
        relay.error.emit(f"ValueError: {i}", "Traceback …")
    QApplication.processEvents()
    first = relay._box
    assert first is not None and relay._count == 5 and Relay.shown == 5
    assert "5 errors" in first.text() and "ValueError: 4" in first.text()
    relay._dismissed()                   # the user pressed OK
    relay.error.emit("KeyError: x", "Traceback …")
    assert relay._box is not first and relay._count == 1
