"""Regressions for the 2.1.0 review of the window and its hosts: the desktop
window, TurboSSHWidget, the JSON control channel and its Python client, and the
settings / sessions files they share. Each test names what used to go wrong."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_until(predicate, timeout=20.0):
    from PyQt5.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(25)
    return predicate()


def pause(ms):
    from PyQt5.QtTest import QTest
    QTest.qWait(ms)


def flush_deletes():
    from PyQt5.QtCore import QEvent
    from PyQt5.QtWidgets import QApplication
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture(autouse=True)
def _restore_settings():
    """Every test leaves the settings (and a host-chosen theme) as it found them."""
    from turbossh.gui import settings, theme
    before = settings.load()
    yield
    theme.set_active(None)
    settings.save(before)


# ------------------------------------------------------------------ fixtures with Qt
@pytest.fixture()
def window(qapp):
    """The desktop app's window."""
    pytest.importorskip("pyte")
    from turbossh.gui.main_window import MainWindow

    class Window(MainWindow):
        def closeEvent(self, event):
            event.accept()

    win = Window()
    win.show()
    qapp.processEvents()                 # its start-up report runs now, not mid-test
    yield win
    win.shut_down()
    win.hide()
    win.deleteLater()
    pause(50)


@pytest.fixture()
def embed(qapp):
    """TurboSSHWidgets on a host window's page, shut down afterwards."""
    pytest.importorskip("pyte")
    from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
    from turbossh.gui import TurboSSHWidget, qtutil
    host = QMainWindow()
    page = QWidget()
    QVBoxLayout(page)
    host.setCentralWidget(page)
    host.resize(1000, 700)
    host.show()
    made = []

    def make(**options):
        widget = TurboSSHWidget(page, **options)
        page.layout().addWidget(widget)
        made.append(widget)
        QApplication.processEvents()     # its start-up report runs now, not mid-test
        return widget
    yield make
    for widget in made:
        widget.shutdown()
    assert qtutil.wait_for_teardowns(10)
    assert wait_until(lambda: not qtutil._PARKED, 30)
    host.hide()
    host.deleteLater()
    pause(30)


class _Out(io.RawIOBase):
    """The host's end of stdout: every JSON line TurboSSH wrote."""

    def __init__(self):
        self.lines = []

    def writable(self):
        return True

    def write(self, data):
        for line in bytes(data).decode("utf-8").splitlines():
            self.lines.append(json.loads(line))
        return len(data)

    def events(self, name=None):
        return [m for m in self.lines if "event" in m and (name is None or m["event"] == name)]

    def reply(self, rid):
        return next((m for m in self.lines if m.get("id") == rid), None)


def _requester(b):
    counter = iter(range(1, 10_000))

    def request(cmd, **args):
        rid = next(counter)
        b._on_line(json.dumps({"id": rid, "cmd": cmd, "args": args}).encode())
        b._outbox.join()                 # what it wrote has been delivered
        return rid
    return request


@pytest.fixture()
def bridge(qapp, monkeypatch):
    pytest.importorskip("pyte")
    from turbossh.gui import ffmpeg_tools, qtutil
    from turbossh.gui.control import ControlBridge
    from turbossh.gui.embed import TurboSSHWidget
    monkeypatch.setattr(ffmpeg_tools, "cached_ffmpeg", lambda: None)
    widget = TurboSSHWidget()
    out = _Out()
    b = ControlBridge(widget, out)
    b.request, b.out = _requester(b), out
    yield b
    widget.shutdown()
    assert qtutil.wait_for_teardowns(10)
    assert wait_until(lambda: not qtutil._PARKED, 30)
    widget.deleteLater()


@pytest.fixture()
def hosted(qapp, monkeypatch):
    """What run_hosted builds (``build_hosted``), short of the event loop."""
    pytest.importorskip("pyte")
    from turbossh.gui import control, options, qtutil
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)          # it installs its own
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    quit_rule = qapp.quitOnLastWindowClosed()
    made = []

    def make(argv):
        out = _Out()
        widget, b = control.build_hosted(options.parse(argv), qapp, out)
        b.request, b.out = _requester(b), out
        made.append((widget, b))
        return widget, b
    yield make
    for widget, b in made:
        qapp.aboutToQuit.disconnect(b._say_exited)
        widget.shutdown()
        widget.hide()
        widget.deleteLater()
    qapp.setQuitOnLastWindowClosed(quit_rule)
    assert qtutil.wait_for_teardowns(10)
    pause(30)


# ------------------------------------------------------------------ 1: SSH server setup
def test_starting_the_ssh_server_setup_twice_leaves_one_poll(window, tmp_path, monkeypatch):
    """A second start made a second poll next to the first; the old one re-showed
    "SSH server ready" every few seconds for minutes, and polled for ever."""
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[1]))
    monkeypatch.setattr(type(window), "_SSHD_POLL_MS", 10)
    result = tmp_path / "sshd-setup-result.txt"
    window._watch_sshd_setup(str(result))
    window._watch_sshd_setup(str(result))           # started again
    result.write_text("status=OK\nport=22\n", encoding="utf-8")
    assert wait_until(lambda: shown)
    pause(300)                                      # many poll intervals
    assert shown == ["SSH server ready"] and not window._sshd_pending()
    assert not [t for t in window.findChildren(QTimer)
                if t.isActive() and t.interval() == window._SSHD_POLL_MS]


def test_a_setup_result_written_with_a_bom_reads_as_success(window, tmp_path, monkeypatch):
    """Windows PowerShell 5.1 wrote the result file with a UTF-8 BOM: the first
    key read as "\\ufeffstatus" and a working server was reported as failed."""
    from PyQt5.QtWidgets import QMessageBox
    shown, warned = [], []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(type(window), "_SSHD_POLL_MS", 10)
    result = tmp_path / "sshd-setup-result.txt"
    result.write_bytes(b"\xef\xbb\xbfstatus=OK\r\nport=2222\r\nsshd=Running\r\n")
    window._watch_sshd_setup(str(result))
    assert wait_until(lambda: shown or warned)
    assert warned == [] and "port 2222" in shown[0]


@pytest.mark.skipif(os.name != "nt", reason="the setup is Windows-only")
def test_a_second_setup_asks_and_still_reports_once(window, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from turbossh import cli
    asked, shown, launched = [], [], []
    result = tmp_path / "sshd-setup-result.txt"
    monkeypatch.setattr(cli, "launch_setup_server", lambda: launched.append(1) or True)
    monkeypatch.setattr(cli, "sshd_result_file", lambda: str(result))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[1]))
    monkeypatch.setattr(type(window), "_SSHD_POLL_MS", 10)
    window._setup_ssh_server_local()
    window._setup_ssh_server_local()
    assert len(launched) == 2 and "still running" in asked[1]
    result.write_text("status=OK\nport=22\n", encoding="utf-8")
    assert wait_until(lambda: shown)
    pause(300)
    assert shown == ["SSH server ready"]
    window._watch_sshd_setup(str(tmp_path / "never"))
    window.stop_background_work()                   # closing stops the poll too
    assert not window._sshd_pending()


# ------------------------------------------------------------------ 2: settings.json
@pytest.fixture()
def settings_file():
    """settings.json, its .corrupt copies and the module's state, as they were."""
    from turbossh.gui import settings
    path = settings._FILE
    folder = os.path.dirname(path)
    before = open(path, "rb").read() if os.path.exists(path) else None
    existing = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    state = (settings.load_problem, settings.save_problem, dict(settings._unsaved))
    yield settings
    if before is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        with open(path, "wb") as fh:
            fh.write(before)
    for name in set(os.listdir(folder)) - existing:
        if name.startswith("settings.json.corrupt-"):
            os.remove(os.path.join(folder, name))
    settings.load_problem, settings.save_problem = state[:2]
    settings._unsaved.clear()
    settings._unsaved.update(state[2])
    settings.reload()


def _on_disk(settings):
    with open(settings._FILE, encoding="utf-8") as fh:
        return json.load(fh)


def test_a_save_keeps_what_another_process_saved_meanwhile(settings_file):
    """Each process wrote its whole in-memory copy: the desktop app and a hosted
    TurboSSH wiped out each other's saved machines and jump host."""
    from turbossh._jsonstore import atomic_write_json
    s = settings_file
    s.save({"compact_ribbon": not s.get("compact_ribbon")})   # the file exists
    mine = s.load()                                           # this process's view
    other = _on_disk(s)                                       # the other process saves
    other["machines"] = [{"name": "rig", "host": "10.0.0.9", "user": "", "domain": ""}]
    other["jump_host"] = "jump-from-the-other-process"
    atomic_write_json(s._FILE, other)
    new_theme = "nord" if mine["theme"] != "nord" else "paper"
    mine["theme"] = new_theme                                 # cfg = load(); cfg[k] = v
    s.save(mine)                                              # save(cfg): the whole dict
    saved = _on_disk(s)
    assert saved["theme"] == new_theme
    assert saved["jump_host"] == "jump-from-the-other-process"
    assert saved["machines"][0]["host"] == "10.0.0.9"
    assert s.get("jump_host") == "jump-from-the-other-process"   # and this one sees it


def test_a_file_that_cannot_be_read_is_not_written_over(settings_file, monkeypatch):
    s = settings_file
    s.save({"term_font_size": 13})
    before = open(s._FILE, "rb").read()

    def locked(path, *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(s._FILE):
            raise PermissionError(13, "in use by another program")
        return open(path, *args, **kwargs)
    monkeypatch.setattr(s, "open", locked, raising=False)    # the module's open() only
    s.save({"term_font_size": 14})
    monkeypatch.delattr(s, "open")
    assert open(s._FILE, "rb").read() == before
    assert s.save_problem and "not written over" in s.save_problem
    assert s.get("term_font_size") == 14                     # the running app has it
    s.save({"default_baud": 9600})                           # readable again
    saved = _on_disk(s)
    assert saved["term_font_size"] == 14 and saved["default_baud"] == 9600
    assert s.save_problem is None


def test_a_damaged_file_is_replaced_once_a_copy_is_kept(settings_file, monkeypatch):
    from turbossh import _jsonstore
    s = settings_file
    copies = []
    real = _jsonstore.set_aside
    monkeypatch.setattr(_jsonstore, "set_aside", lambda p: copies.append(p) or real(p))
    monkeypatch.setattr(s, "set_aside", lambda p: copies.append(p) or real(p))
    with open(s._FILE, "w", encoding="utf-8") as fh:
        fh.write("{damaged")
    s.reload()                                       # start-up: copied aside, reported
    assert s.load_problem and len(copies) == 1
    s.save({"term_font_size": 15})                   # that copy is enough: no second
    s.save({"term_font_size": 16})
    assert len(copies) == 1 and _on_disk(s)["term_font_size"] == 16
    folder = os.path.dirname(s._FILE)
    kept = [n for n in os.listdir(folder) if n.startswith("settings.json.corrupt-")]
    assert any(open(os.path.join(folder, n), encoding="utf-8").read() == "{damaged"
               for n in kept)


class _Transient(str):
    """What _jsonstore.read_json reports for a file that is there but could not
    be read just now (a lock that outlasted its retries): nothing set aside."""
    transient = True


def _locked_open(path_to_lock):
    def locked(path, *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(path_to_lock):
            raise PermissionError(13, "in use by another program")
        return open(path, *args, **kwargs)
    return locked


def test_settings_a_lock_at_start_up_never_puts_defaults_on_disk(settings_file, monkeypatch):
    s = settings_file
    s.save({"jump_host": "real-jump"})               # what the file really holds
    real = _on_disk(s)
    monkeypatch.setattr(s, "read_json", lambda *_a, **_k: (None, _Transient(
        "settings.json could not be read (in use by another program).")))
    s.reload()                                       # start-up hit the lock
    assert s.get("jump_host") == "" and s.load_problem.transient
    monkeypatch.setattr(s, "open", _locked_open(s._FILE), raising=False)
    s.save({"term_font_size": 17})                   # still locked: not written
    assert _on_disk(s) == real and "not written over" in s.save_problem
    assert s.get("term_font_size") == 17             # the running app has it
    monkeypatch.delattr(s, "open")
    s.save({"default_baud": 57600})                  # readable: onto the real file
    saved = _on_disk(s)
    assert saved["jump_host"] == "real-jump"
    assert saved["term_font_size"] == 17 and saved["default_baud"] == 57600
    assert s.get("jump_host") == "real-jump" and s.save_problem is None


def test_sessions_a_file_in_use_is_never_written_over(window, monkeypatch):
    """_commit wrote its own list when it could not read the file: the user's
    saved sessions were replaced by the one being saved."""
    from PyQt5.QtWidgets import QMessageBox
    from turbossh.gui import sessions
    store = sessions.SessionStore()
    store.save({"name": "busy-keep", "type": "ssh", "host": "k"})
    before = open(sessions._FILE, "rb").read()
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(sessions, "read_json", lambda *_a, **_k: (None, _Transient(
        "sessions.json could not be read (in use by another program).")))
    try:
        with pytest.raises(sessions.SessionStoreError, match="not written over"):
            store.save({"name": "busy-new", "type": "ssh", "host": "n"})
        assert open(sessions._FILE, "rb").read() == before
        assert not window._change_store("Saving the session", window.store.save,
                                        {"name": "busy-new", "type": "ssh", "host": "n"})
        assert warned and "not written over" in warned[0]
        late = sessions.SessionStore()               # start-up during the lock
        assert late.sessions == [] and not late.reload_if_changed()
        monkeypatch.undo()
        assert late.reload_if_changed() and "busy-keep" in late.names()
    finally:
        monkeypatch.undo()
        store.delete("busy-keep")


# ------------------------------------------------------------------ 3: the log panel
def test_a_theme_change_redraws_the_log_once_and_fast(qapp):
    """Every theme step re-drew all 50,000 lines one insert at a time: 10.7 s."""
    from turbossh.gui import theme
    from turbossh.gui.log_panel import LogPanel
    panel = LogPanel()
    panel.resize(800, 300)
    panel.show()
    panel._entries = [(1, "OK", f"line {i}", "2026-01-01 00:00:00") for i in range(20000)]
    started = time.monotonic()
    panel._on_filter()                               # a full re-draw
    took = time.monotonic() - started
    assert panel.view.blockCount() == 20001
    assert took < 3.0, f"a full re-draw took {took:.1f} s"   # line by line: ~5 s here
    redraws = []
    panel._recolor.timeout.connect(lambda: redraws.append(1))
    for name in ("paper", "nord", "light", "dark"):  # arrowing through the list
        panel.set_theme(name)
    assert wait_until(lambda: redraws)
    pause(300)
    assert redraws == [1]
    block = panel.view.document().lastBlock().previous()
    colours = set()
    it = block.begin()
    while not it.atEnd():
        colours.add(it.fragment().charFormat().foreground().color().name())
        it += 1
    assert theme.log_colors("dark")["OK"].lower() in colours
    panel.deleteLater()


def test_the_settings_theme_list_applies_once_it_rests(window, monkeypatch):
    from turbossh.gui.settings_dialog import SettingsDialog
    applied = []
    monkeypatch.setattr(window, "apply_theme", applied.append)
    dlg = SettingsDialog(window)
    for name in ("paper", "nord", "light"):
        dlg.theme.setCurrentText(name)
    assert applied == []
    assert wait_until(lambda: applied)
    pause(300)
    assert applied == ["light"]
    dlg.theme.setCurrentText("dracula")
    dlg.done(0)                                      # picked just before Cancel: applies
    assert applied == ["light", "dracula"]
    dlg.deleteLater()


# ------------------------------------------------------------------ 4, 9: hosted ends
def test_hosted_only_an_explicit_end_quits(hosted, qapp):
    """Without --embed, Qt's "last window closed" stayed on: after set_visible
    (false), closing any TurboSSH dialog quit and closed every session."""
    hosted(["--control", "stdio"])
    assert qapp.quitOnLastWindowClosed() is False


@pytest.mark.parametrize("embedded", [False, True], ids=["own-window", "wm-close-embedded"])
def test_closing_the_hosted_window_ends_the_process(hosted, monkeypatch, embedded):
    """Closing it (the user, or a WM_CLOSE sent to the embedded window) shut the
    sessions down but left the process running, answering ok and doing nothing."""
    from PyQt5.QtWidgets import QApplication
    quits = []
    monkeypatch.setattr(QApplication, "quit", lambda *a: quits.append(True))
    widget, b = hosted(["--control", "stdio"])
    widget.in_native_parent = embedded               # embedded: no question first
    widget.close()
    assert wait_until(lambda: quits)
    assert widget.is_shut_down and b.ended
    rid = b.request("set_theme", name="nord")
    reply = b.out.reply(rid)
    assert reply["ok"] is False and "shut down" in reply["error"]
    rid = b.request("ping")
    assert b.out.reply(rid)["ok"]
    rid = b.request("confirm_close")
    assert "shut down" in b.out.reply(rid)["error"]
    pause(50)
    assert quits == [True]


# ------------------------------------------------------------------ 5: ids of concurrent opens
def _ask_first(opener, ms=300):
    """What "Serial port in use" does: a question, i.e. a nested event loop in
    which the requests that came in meanwhile run."""
    from PyQt5.QtCore import QEventLoop, QTimer

    def run(*_a, **_k):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec_()
        return opener()
    return run


def test_opens_waiting_on_a_question_keep_their_ids(bridge, monkeypatch, ssh_server):
    out, widget = bridge.out, bridge.widget
    login = dict(host="127.0.0.1", port=ssh_server.port, username="u", password="good")
    monkeypatch.setattr(widget, "open_serial",
                        _ask_first(lambda: widget.connect_ssh(name="first", **login)))
    first = out.reply(bridge.request("open_serial", device="COM1"))["result"]["session"]
    second = out.reply(bridge.request("connect_ssh", name="second", **login))["result"]["session"]
    bridge.request("trigger_action", name="local")   # the user's own tab, meanwhile
    assert wait_until(lambda: len(out.events("session_opened")) == 3)
    opened = {e["session"]: e["name"] for e in out.events("session_opened")}
    assert opened[first] == "first" and opened[second] == "second"
    assert len(opened) == 3 and not out.events("session_failed")
    rid = bridge.request("sessions")
    assert {s["session"] for s in out.reply(rid)["result"]} == set(opened)


def test_an_open_that_ends_with_nothing_is_closed_under_its_own_id(bridge, monkeypatch,
                                                                   ssh_server):
    out = bridge.out
    monkeypatch.setattr(bridge.widget, "open_serial", _ask_first(lambda: None))  # "No"
    declined = out.reply(bridge.request("open_serial", device="COM1"))["result"]["session"]
    other = out.reply(bridge.request("connect_ssh", host="127.0.0.1", port=ssh_server.port,
                                     username="u", password="good"))["result"]["session"]
    assert wait_until(lambda: out.events("session_closed"))
    assert [e["session"] for e in out.events("session_opened")] == [other]
    assert [e["session"] for e in out.events("session_failed")] == [declined]
    assert [e["session"] for e in out.events("session_closed")] == [declined]


# ------------------------------------------------------------------ 6: the current tab
def test_a_new_tab_is_announced_as_current(embed, ssh_server):
    """It became current before it was registered: every open reported "no
    session", and the new current session was never announced."""
    for options in ({"home": False}, {}):
        turbo = embed(**options)
        seen = []
        turbo.current_session_changed.connect(seen.append)
        first = turbo.connect_ssh("127.0.0.1", port=ssh_server.port, username="u",
                                  password="good")
        second = turbo.connect_ssh("127.0.0.1", port=ssh_server.port, username="u",
                                   password="good")
        assert seen == [first, second], options
        first.activate()
        assert seen == [first, second, first]


def test_a_new_tab_is_announced_as_current_over_the_channel(bridge, ssh_server):
    out = bridge.out
    rid = bridge.request("connect_ssh", host="127.0.0.1", port=ssh_server.port,
                         username="u", password="good")
    sid = out.reply(rid)["result"]["session"]
    assert wait_until(lambda: out.events("current_session_changed"))
    names = [m["event"] for m in out.events() if m["event"] != "log"]
    assert names.index("session_opened") < names.index("current_session_changed")
    assert [e["session"] for e in out.events("current_session_changed")] == [sid]


# ------------------------------------------------------------------ 7: the update check
def test_closing_the_window_during_an_update_check_does_not_abort(qapp, monkeypatch):
    """The check's QThread was the window's child: closing the app during a slow
    check destroyed it running, and Qt aborts the process for that."""
    pytest.importorskip("pyte")
    from PyQt5 import sip
    from turbossh import update
    from turbossh.gui import qtutil, updater
    from turbossh.gui.main_window import MainWindow
    release = threading.Event()
    monkeypatch.setattr(update, "latest_version", lambda: release.wait(20) and "0.0.1")
    win = MainWindow()
    win.check_updates()
    thread = win._update_thread
    assert thread.parent() is None and thread.isRunning()
    win.check_updates()                              # clicked again while it checks
    assert win._update_thread is thread
    win.stop_background_work()                       # what closing the window does
    win.deleteLater()
    flush_deletes()
    assert sip.isdeleted(win) and not sip.isdeleted(thread) and thread.isRunning()
    release.set()
    assert wait_until(lambda: not thread.isRunning())
    assert wait_until(lambda: thread not in qtutil._PARKED and thread not in updater._RUNNING)


# ------------------------------------------------------------------ 8: one end, one wait
def test_the_end_runs_once_and_waits_as_long_as_the_host_said(bridge, monkeypatch):
    """finish() blocked for the host's wait, then quitting blocked 6 s more; the
    reader, the writer and the window going away could each start it again."""
    from PyQt5.QtWidgets import QApplication
    from turbossh.gui import embed, qtutil
    assert all(w is bridge.widget for w in embed._LIVE.values())
    waits, quits = [], []
    monkeypatch.setattr(qtutil, "wait_for_teardowns", lambda t=6.0: waits.append(t) or True)

    def quit_(*_a):
        quits.append(True)
        embed._shutdown_all()                        # what aboutToQuit runs
    monkeypatch.setattr(QApplication, "quit", quit_)
    bridge.host_gone.connect(bridge.finish)          # as run_hosted wires it
    rid = bridge.request("shutdown", wait=0.5)
    assert bridge.out.reply(rid)["ok"]
    assert wait_until(lambda: quits)
    bridge.host_gone.emit()                          # the reader and the writer both
    bridge.host_gone.emit()
    bridge.finish()
    pause(100)
    assert quits == [True] and waits == [0.5]
    monkeypatch.undo()


# ------------------------------------------------------------------ 10: arguments by type
@pytest.mark.parametrize("cmd,args,words", [
    ("set_camera_enabled", {"enabled": "false"}, "true or false"),
    ("set_visible", {"visible": 0}, "true or false"),
    ("set_chrome", {"ribbon": "no"}, "true or false"),
    ("shutdown", {"wait": float("inf")}, "from 0 to 60"),
    ("shutdown", {"wait": float("nan")}, "from 0 to 60"),
    ("shutdown", {"wait": 1e9}, "from 0 to 60"),
    ("shutdown", {"wait": True}, "from 0 to 60"),
    ("connect_ssh", {"host": "h", "ignore_host_key": "false"}, "true or false"),
    ("connect_ssh", {"host": "h", "port": "22"}, "whole number"),
    ("connect_ssh", {"host": "h", "quick_commands": "ls"}, "list of strings"),
    ("open_serial", {"device": 5}, "string"),
    ("run", {"session": "s1", "command": "x", "timeout": float("inf")}, "timeout"),
])
def test_arguments_are_checked_by_type(bridge, cmd, args, words):
    """bool("false") is True (the Camera came on, a host key would be ignored);
    wait=Infinity overflowed the join and the process never ended."""
    reply = bridge.out.reply(bridge.request(cmd, **args))
    assert reply["ok"] is False and reply["type"] == "ProtocolError"
    assert words in reply["error"]
    assert not bridge.ended


def test_deeply_nested_json_is_answered(bridge):
    bridge._on_line(b"[" * 100_000 + b"]" * 100_000)
    bridge._outbox.join()
    assert bridge.out.lines[-1]["ok"] is False
    assert "not a JSON object" in bridge.out.lines[-1]["error"]


def test_an_unknown_theme_is_refused_on_the_command_line():
    """It raised ValueError while building the window, after stdio was claimed."""
    from turbossh.gui import options
    with pytest.raises(SystemExit):
        options.parse(["--control", "stdio", "--theme", "bogus"])
    for name in ("inherit", "nord"):
        assert options.parse(["--control", "stdio", "--theme", name]).theme == name


# ------------------------------------------------------------------ 11: jump passwords
def test_a_session_keeps_its_own_jump_password_only_when_it_differs(window, monkeypatch):
    """The dialog filled in the shared jump password even with the jump host off,
    and every saved session kept a copy: stale after a change (AD lockouts)."""
    import turbossh.gui.main_window as module
    from turbossh.gui import settings
    from turbossh.gui.session_dialog import SessionDialog
    from turbossh.gui.sessions import SessionStore
    settings.set_jump_password("shared-pw")
    names = ("jt-plain", "jt-hop", "jt-own")
    try:
        dlg = SessionDialog(window)
        assert dlg.jump_password_value() == ""       # not pre-filled any more
        dlg.done(0)
        dlg.deleteLater()

        opened = []
        monkeypatch.setattr(window, "open_session_config",
                            lambda s, pw="", jpw="": opened.append((s["name"], jpw)))
        current = {}

        class Dialog:
            Accepted = 1

            def __init__(self, *_a, **_k):
                pass

            def exec_(self):
                return 1

            def deleteLater(self):
                pass

            def result_session(self):
                return dict(current["session"])

            def password_value(self):
                return ""

            def jump_password_value(self):
                return current["entered"]
        monkeypatch.setattr(module, "SessionDialog", Dialog)
        for name, use_jump, entered, stored, used in (
                ("jt-plain", False, "shared-pw", None, ""),
                ("jt-hop", True, "shared-pw", None, "shared-pw"),
                ("jt-own", True, "own-pw", "own-pw", "own-pw")):
            current["session"] = {"name": name, "type": "ssh", "host": "h",
                                  "use_jump": use_jump, "jhost": "j"}
            current["entered"] = entered
            window.new_session()
            assert SessionStore.jump_password(name) == stored, name
            assert opened[-1] == (name, used), name
        settings.set_jump_password("changed-pw")
        window.open_session_named("jt-hop")
        assert opened[-1] == ("jt-hop", "changed-pw")     # follows the shared one
    finally:
        for name in names:
            window.store.delete(name)
        settings.set_jump_password("")


def test_changing_the_shared_jump_password_drops_old_copies(window):
    from turbossh.gui import settings
    from turbossh.gui.sessions import SessionStore
    from turbossh.gui.settings_dialog import SettingsDialog
    store = SessionStore()
    store.save({"name": "jc-copy", "type": "ssh", "host": "h", "use_jump": True},
               "", "shared-1")                       # what earlier versions stored
    store.save({"name": "jc-own", "type": "ssh", "host": "h", "use_jump": True},
               "", "its-own")
    settings.set_jump_password("shared-1")
    try:
        dlg = SettingsDialog(window)
        dlg.jpass.setText("shared-2")
        dlg.result_settings()
        dlg.done(0)
        dlg.deleteLater()
        assert settings.jump_password() == "shared-2"
        assert SessionStore.jump_password("jc-copy") is None
        assert SessionStore.jump_password("jc-own") == "its-own"
    finally:
        store.delete("jc-copy")
        store.delete("jc-own")
        settings.set_jump_password("")


# ------------------------------------------------------------------ 12: the crash guard
def test_the_crash_guard_works_without_a_stderr(embed, monkeypatch):
    """Under pythonw sys.stderr is None: the hook raised before logging the error."""
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    turbo = embed()
    logged = []
    turbo.log_message.connect(logged.append)
    monkeypatch.setattr(sys, "stderr", None)
    sys.excepthook(ValueError, ValueError("not lost"), None)
    assert logged and "not lost" in logged[-1]


# ------------------------------------------------------------------ 13: polling saved names
def _select(widget, name):
    from PyQt5.QtCore import Qt
    for i in range(widget.count()):
        if widget.item(i).data(Qt.UserRole) == name:
            widget.setCurrentRow(i)
            return
    raise AssertionError(f"{name} not listed")


def _selected(widget):
    from PyQt5.QtCore import Qt
    item = widget.currentItem()
    return item.data(Qt.UserRole) if item is not None else None


def test_polling_saved_names_rebuilds_nothing_until_the_file_changes(embed, monkeypatch):
    """Each poll re-read the file and rebuilt the sidebar and Home lists, so a
    host polling the names cleared the user's selection under Edit / Delete."""
    from turbossh.gui.sessions import SessionStore
    turbo = embed()
    win = turbo.main_window
    other = SessionStore()                           # e.g. the desktop app
    other.save({"name": "poll-a", "type": "ssh", "host": "a"})
    other.save({"name": "poll-b", "type": "ssh", "host": "b"})
    try:
        assert {"poll-a", "poll-b"} <= set(turbo.saved_session_names())
        _select(win.session_list, "poll-b")
        _select(win._home.sessions, "poll-b")
        rebuilds = []
        real = win.refresh_sessions
        monkeypatch.setattr(win, "refresh_sessions", lambda: rebuilds.append(1) or real())
        for _ in range(5):
            turbo.saved_session_names()
        assert rebuilds == []
        other.save({"name": "poll-c", "type": "ssh", "host": "c"})
        assert "poll-c" in turbo.saved_session_names() and rebuilds == [1]
        assert _selected(win.session_list) == "poll-b"
        assert _selected(win._home.sessions) == "poll-b"
    finally:
        for name in ("poll-a", "poll-b", "poll-c"):
            other.delete(name)


def test_an_unreadable_sessions_file_is_set_aside_once_not_per_poll(embed, monkeypatch):
    from turbossh import _jsonstore
    from turbossh.gui import sessions
    turbo = embed()
    path = sessions._FILE
    folder = os.path.dirname(path)
    before = open(path, "rb").read() if os.path.exists(path) else None
    existing = set(os.listdir(folder))
    copies = []
    real = _jsonstore.set_aside
    monkeypatch.setattr(_jsonstore, "set_aside", lambda p: copies.append(p) or real(p))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[broken")
        for _ in range(4):
            turbo.saved_session_names()
        assert len(copies) == 1
    finally:
        if before is None:
            os.remove(path)
        else:
            with open(path, "wb") as fh:
                fh.write(before)
        for name in set(os.listdir(folder)) - existing:
            if name.startswith("sessions.json.corrupt-"):
                os.remove(os.path.join(folder, name))
        turbo.main_window.store.load()
        turbo.main_window.store.load_problem = None


# ------------------------------------------------------------------ 14: the Python client
class _FakeTurboSSH:
    """Just enough of a TurboSSH process for TurboSSHProcess: it answers ping at
    once and stays silent to anything else; the test sends its events."""

    def __init__(self):
        read, write = os.pipe()
        self.stdout = os.fdopen(read, "rb")
        self._out = os.fdopen(write, "wb")
        self.stderr = io.BytesIO()
        self.stdin = self
        self._lock = threading.Lock()
        self._alive = True

    def write(self, data):                           # a request from the client
        request = json.loads(bytes(data).decode("utf-8"))
        if request["cmd"] == "ping":
            self.emit({"id": request["id"], "ok": True, "result": {"pong": True}})
        return len(data)

    def flush(self):
        pass

    def emit(self, message):
        with self._lock:
            self._out.write((json.dumps(message) + "\n").encode("utf-8"))
            self._out.flush()

    def end(self):
        self._alive = False
        with self._lock:
            self._out.close()

    def poll(self):
        return None if self._alive else 0

    def kill(self):
        self.end()

    def wait(self, timeout=None):
        return 0


def test_a_callback_can_call_turbossh():
    """Callbacks ran on the reader thread, which alone delivers answers: a call()
    from an on_event or call_async callback waited the whole timeout, then failed."""
    from turbossh.embed_client import TurboSSHError, TurboSSHProcess
    fake = _FakeTurboSSH()
    gui = TurboSSHProcess(fake)
    got, order, from_async, after_exit = [], [], [], []

    def on_event(event):
        order.append(event["event"])
        if event["event"] == "session_opened":
            got.append(gui.call("ping", timeout=5))
            got.append(gui.wait_for("session_connected", "s1", timeout=5)["event"])
    gui.on_event(on_event)
    fake.emit({"event": "session_opened", "session": "s1"})
    fake.emit({"event": "session_connected", "session": "s1"})
    gui.call_async("ping", lambda _result: from_async.append(gui.call("ping", timeout=5)))
    deadline = time.monotonic() + 10
    while (len(got) < 2 or not from_async) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert got == [{"pong": True}, "session_connected"]
    assert from_async == [{"pong": True}]
    assert order[:2] == ["session_opened", "session_connected"]    # in order
    gui.call_async("never_answered", after_exit.append)
    fake.end()                                       # TurboSSH exits first
    assert gui.wait_for("exited", timeout=5)
    deadline = time.monotonic() + 5
    while not after_exit and time.monotonic() < deadline:
        time.sleep(0.02)
    assert after_exit and isinstance(after_exit[0], TurboSSHError)


# ------------------------------------------------------------------ quitting with unsaved edits
def test_quitting_asks_about_remote_editors_with_unsaved_changes(qapp, monkeypatch):
    """An offline editor outlives its session tab but not the window: quitting
    used to destroy unsaved edits without a word."""
    import sys as _sys
    import types
    from PyQt5.QtWidgets import QMessageBox
    from turbossh.gui import main_window
    assert main_window.unsaved_editor_count() == 0 or "turbossh.gui.remote_edit" in _sys.modules
    fake = types.SimpleNamespace(unsaved_editors=lambda: ["editor"])
    monkeypatch.setitem(_sys.modules, "turbossh.gui.remote_edit", fake)
    asked = []

    def answer(*args, **_kwargs):
        asked.append(args[2])
        return QMessageBox.No
    monkeypatch.setattr(QMessageBox, "question", answer)
    assert main_window.unsaved_editor_count() == 1
    assert main_window.confirm_unsaved_editors(None) is False
    assert asked and "unsaved changes" in asked[0]
    fake.unsaved_editors = lambda: []
    assert main_window.confirm_unsaved_editors(None) is True


def test_confirm_close_for_hosts_includes_unsaved_editors(qapp, monkeypatch):
    import sys as _sys
    import types
    from PyQt5.QtWidgets import QMessageBox
    from turbossh.gui import TurboSSHWidget
    monkeypatch.setitem(_sys.modules, "turbossh.gui.remote_edit",
                        types.SimpleNamespace(unsaved_editors=lambda: ["editor"]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    turbo = TurboSSHWidget()
    try:
        assert turbo.confirm_close() is False          # no session, but an edit
    finally:
        turbo.shutdown()
        turbo.deleteLater()
