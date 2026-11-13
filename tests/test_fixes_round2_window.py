"""Regressions of the 2.1.0 window / hosting fixes, against what worked in 2.0.0:
the shared settings file, shutdown's wait, per-session jump passwords, request
arguments a host sends, a sessions file locked at start-up, and requests
answered just before the end. Each test names what went wrong."""

from __future__ import annotations

import io
import json
import os
import threading
import time
import types

import pytest


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


@pytest.fixture(autouse=True)
def _restore_settings():
    """Every test leaves the settings (and a host-chosen theme) as it found them."""
    from turbossh.gui import settings, theme
    before = settings.load()
    password = settings.jump_password()
    yield
    theme.set_active(None)
    settings.save(before)
    settings.set_jump_password(password)


@pytest.fixture()
def settings_file():
    """settings.json and the module's state, as they were (tests here play the
    other TurboSSH process by writing the file behind this one's back)."""
    from turbossh.gui import settings
    path = settings._FILE
    settings.save({"theme": settings.get("theme")})  # the file exists from here on
    before = open(path, "rb").read()
    state = (settings.load_problem, settings.save_problem, dict(settings._unsaved))
    yield settings
    with open(path, "wb") as fh:
        fh.write(before)
    settings.load_problem, settings.save_problem = state[:2]
    settings._unsaved.clear()
    settings._unsaved.update(state[2])
    settings.reload()


def _on_disk(settings):
    with open(settings._FILE, encoding="utf-8") as fh:
        return json.load(fh)


def _another_process_saves(settings, **values):
    """What a second TurboSSH (the desktop app, a hosted one) does to the file."""
    from turbossh._jsonstore import atomic_write_json
    data = _on_disk(settings)
    data.update(values)
    atomic_write_json(settings._FILE, data)


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

    def events(self, name=None, session=None):
        return [m for m in self.lines if "event" in m
                and (name is None or m["event"] == name)
                and (session is None or m.get("session") == session)]

    def reply(self, rid):
        return next((m for m in self.lines if m.get("id") == rid), None)


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
    counter = iter(range(1, 10_000))

    def request(cmd, **args):
        rid = next(counter)
        b._on_line(json.dumps({"id": rid, "cmd": cmd, "args": args}).encode())
        b._outbox.join()                 # what it wrote has been delivered
        return rid
    b.request, b.out = request, out
    yield b
    widget.shutdown()
    assert qtutil.wait_for_teardowns(10)
    assert wait_until(lambda: not qtutil._PARKED, 30)
    widget.deleteLater()


@pytest.fixture()
def ending(monkeypatch):
    """The end of the process, recorded instead of done: (waits, quits)."""
    from PyQt5.QtWidgets import QApplication
    from turbossh.gui import qtutil
    waits, quits = [], []
    monkeypatch.setattr(qtutil, "wait_for_teardowns", lambda t=6.0: waits.append(t) or True)
    monkeypatch.setattr(QApplication, "quit", lambda *_a: quits.append(True))
    yield waits, quits
    monkeypatch.undo()


# ------------------------------------------------------------------ 1: settings of two processes
def test_set_theme_saves_it_when_another_process_saved_another(settings_file, window):
    """A hosted TurboSSH showing dark, after the desktop app saved light: its
    set_theme("dark") wrote nothing (dark was no change for it) and then took the
    file's light, so the window turned light."""
    from turbossh.gui import theme
    s = settings_file
    window.set_theme("dark")
    _another_process_saves(s, theme="light")
    window.set_theme("dark")
    assert _on_disk(s)["theme"] == "dark"
    assert theme.current_name() == "dark" and s.get("theme") == "dark"


def test_a_save_keeps_this_windows_theme_and_camera(settings_file):
    """Any save took on the other process's theme and Camera choice: changing only
    the font flipped the window to the other theme, and an unrelated save (the
    file browser's folder on closing a tab) turned this window's Camera off."""
    s = settings_file
    s.save({"theme": "dark", "camera_enabled": True})
    _another_process_saves(s, theme="light", camera_enabled=False,
                           jump_host="their-jump")
    s.save({"term_font_size": 13})                   # the font, in Settings
    assert s.get("theme") == "dark" and s.get("camera_enabled") is True
    s.save({"local_browser_dir": os.path.dirname(s._FILE)})   # a tab closing
    assert s.get("theme") == "dark" and s.get("camera_enabled") is True
    saved = _on_disk(s)                              # theirs stay on disk
    assert saved["theme"] == "light" and saved["camera_enabled"] is False
    assert saved["term_font_size"] == 13
    assert s.get("jump_host") == "their-jump"        # shared data is still taken in


def test_settings_ok_does_not_close_camera_tabs_another_process_turned_off(
        settings_file, window, monkeypatch):
    from PyQt5.QtWidgets import QDialog
    from turbossh.gui.settings_dialog import SettingsDialog
    s = settings_file
    window.set_camera_enabled(True)
    closed = []
    monkeypatch.setattr(window, "_camera_panels", lambda: ["camera tab"])
    monkeypatch.setattr(window, "_close_session_widget", closed.append)
    _another_process_saves(s, camera_enabled=False)
    s.save({"local_browser_dir": os.path.dirname(s._FILE)})   # an unrelated save
    assert window.camera_enabled()
    monkeypatch.setattr(SettingsDialog, "exec_", lambda self: QDialog.Accepted)
    window.show_settings()                           # OK, nothing changed
    assert closed == [] and window.camera_enabled()


def test_the_settings_dialog_shows_what_another_process_saved(settings_file, window):
    """It showed this process's old copy: adding a machine and pressing OK wrote
    that old list over the machine the other process had saved."""
    from turbossh.gui.settings_dialog import SettingsDialog
    s = settings_file
    rig = {"name": "rig", "host": "10.0.0.9", "user": "", "domain": ""}
    _another_process_saves(s, machines=[rig], jump_host="their-jump")
    dlg = SettingsDialog(window)
    try:
        assert dlg._machines == [rig] and dlg.jhost.text() == "their-jump"
        dlg._machines = dlg._machines + [{"name": "mine", "host": "10.0.0.7",
                                          "user": "", "domain": ""}]
        s.save(dlg.result_settings())
        assert [m["host"] for m in _on_disk(s)["machines"]] == ["10.0.0.9", "10.0.0.7"]
    finally:
        dlg.done(0)
        dlg.deleteLater()


# ------------------------------------------------------------------ 2: shutdown's wait
@pytest.mark.parametrize("wait,waited", [(100, [60.0]), (1e999, [60.0]), ("6", [6.0]),
                                         (-3, []), (None, [6.0])])
def test_shutdown_takes_any_wait_as_0_to_60(bridge, ending, wait, waited):
    """A wait over 60 s was refused, so embed_client's close() waited its timeout
    and killed the process: exit 1, no session_closed, remote ports and cameras
    left taken. 2.0 took any wait."""
    waits, quits = ending
    rid = bridge.request("shutdown", wait=wait)
    assert bridge.out.reply(rid)["ok"]
    assert wait_until(lambda: quits)
    assert waits == waited


class _FakeTurboSSH:
    """Just enough of a TurboSSH process for TurboSSHProcess.close(): it answers
    shutdown as told, and ends when told to shut down or when its stdin closes."""

    def __init__(self, refuse=False):
        read, write = os.pipe()
        self.stdout = os.fdopen(read, "rb")
        self._out = os.fdopen(write, "wb")
        self.stderr = io.BytesIO()
        self.stdin = self
        self.refuse = refuse
        self.requests, self.killed, self.hung_up = [], False, False
        self._code = None
        self._lock = threading.Lock()

    def write(self, data):
        request = json.loads(bytes(data).decode("utf-8"))
        self.requests.append(request)
        if request["cmd"] == "shutdown":
            if self.refuse:
                self.emit({"id": request["id"], "ok": False, "error": "no",
                           "type": "ProtocolError"})
            else:
                self.emit({"id": request["id"], "ok": True, "result": {}})
                self.end(0)
        return len(data)

    def flush(self):
        pass

    def close(self):                     # its stdin closed: it ends cleanly
        self.hung_up = True
        self.end(0)

    def emit(self, message):
        with self._lock:
            if not self._out.closed:
                self._out.write((json.dumps(message) + "\n").encode("utf-8"))
                self._out.flush()

    def end(self, code):
        if self._code is None:
            self._code = code
            with self._lock:
                self._out.close()

    def poll(self):
        return self._code

    def kill(self):
        self.killed = True
        self.end(1)

    def wait(self, timeout=None):
        return self._code


def test_close_takes_a_long_wait_as_the_longest():
    from turbossh.embed_client import TurboSSHProcess
    fake = _FakeTurboSSH()
    gui = TurboSSHProcess(fake)
    assert gui.close(wait=100, timeout=5) == 0
    assert [r["args"] for r in fake.requests if r["cmd"] == "shutdown"] == [{"wait": 60.0}]
    assert not fake.killed


def test_close_ends_it_cleanly_even_when_shutdown_is_refused():
    """The refusal was swallowed and the process killed after the timeout."""
    from turbossh.embed_client import TurboSSHProcess
    fake = _FakeTurboSSH(refuse=True)
    gui = TurboSSHProcess(fake)
    assert gui.close(wait=0, timeout=1) == 0
    assert fake.hung_up and not fake.killed


# ------------------------------------------------------------------ 3: jump passwords
@pytest.fixture()
def jump(window):
    """Settings' jump host rdp-box / me with password "shared", and saved
    sessions made with ``save(name, password=None, **session)``; all removed."""
    from turbossh.gui import settings
    from turbossh.gui.sessions import SessionStore
    settings.save({"jump_host": "rdp-box", "jump_user": "me"})
    settings.set_jump_password("shared")
    store, made = SessionStore(), []

    def save(name, password=None, **session):
        made.append(name)
        store.save(dict({"name": name, "type": "ssh", "host": "target",
                         "use_jump": True}, **session), "", password)
        return store.get(name)
    yield save
    for name in made:
        store.delete(name)
    settings.set_jump_password("")


def test_a_session_through_another_jump_host_is_not_sent_the_shared_password(window, jump,
                                                                            monkeypatch):
    """A 2.0.0 session with no jump password of its own (a key / agent login to
    a bastion) was sent the shared one: with a password and no key file, the
    agent and the default keys are not tried, and the login failed."""
    from turbossh.gui.sessions import jump_password_for
    bastion = jump("j-bastion", jhost="bastion", juser="me")
    assert jump_password_for("j-bastion", bastion) == ""
    opened = []
    monkeypatch.setattr(window, "open_session_config",
                        lambda s, pw="", jpw="": opened.append(jpw))
    window.store.load()
    window.open_session_named("j-bastion")
    assert opened == [""]
    hop = jump("j-hop", jhost="RDP-BOX", juser="me")    # the Settings one: it shares
    assert jump_password_for("j-hop", hop) == "shared"


def test_another_jump_hosts_password_is_its_own_even_when_equal(window, jump):
    """A session through its own jump host whose password happened to equal the
    shared one lost it (counted as a copy), then was sent the next shared
    password: to its own host, locking that account."""
    from turbossh.gui.session_dialog import SessionDialog
    from turbossh.gui.sessions import SessionStore, jump_password_for, jump_secret_to_store
    from turbossh.gui.settings_dialog import SettingsDialog
    other = jump("j-other", "shared", jhost="bastion", juser="me")
    assert jump_secret_to_store(other, "shared") == "shared"
    dlg = SessionDialog(window, existing=other)
    try:
        assert dlg.jump_password_value() == "shared"     # not shown as "from Settings"
        assert "Settings" not in dlg.jpass.placeholderText()
    finally:
        dlg.done(0)
        dlg.deleteLater()
    settings = SettingsDialog(window)
    try:
        settings.jpass.setText("changed")
        settings.result_settings()
    finally:
        settings.done(0)
        settings.deleteLater()
    assert SessionStore.jump_password("j-other") == "shared"
    assert jump_password_for("j-other", other) == "shared"


def test_moving_the_shared_jump_host_keeps_sessions_on_their_own_password(window, jump):
    """Sessions made in 2.1 take the jump host and user from Settings but not the
    password: when Settings moved to another jump host, they sent the new host's
    password to the old one."""
    from turbossh.gui import settings
    from turbossh.gui.sessions import SessionStore, jump_password_for, jump_secret_to_store
    from turbossh.gui.settings_dialog import SettingsDialog
    session = {"name": "j-made", "type": "ssh", "host": "t", "use_jump": True,
               "jhost": "rdp-box", "juser": "me"}
    assert jump_secret_to_store(session, "shared") == ""     # follows Settings
    made = jump("j-made", None, jhost="rdp-box", juser="me")
    old_copy = jump("j-old-copy", "shared", jhost="rdp-box", juser="me")   # 2.0.0
    own = jump("j-own", "its-own", jhost="rdp-box", juser="me")            # 2.0.0
    assert jump_password_for("j-made", made) == "shared"
    dlg = SettingsDialog(window)
    try:
        dlg.jhost.setText("rdp-new")
        dlg.jpass.setText("new-host-password")
        settings.save(dlg.result_settings())
    finally:
        dlg.done(0)
        dlg.deleteLater()
    assert settings.jump_password() == "new-host-password"
    assert jump_password_for("j-made", made) == "shared"     # the old host's, kept
    assert jump_password_for("j-old-copy", old_copy) == "shared"
    assert jump_password_for("j-own", own) == "its-own"
    assert SessionStore.jump_password("j-made") == "shared"


def test_a_changed_shared_password_drops_only_copies_for_that_jump_host(window, jump):
    from turbossh.gui.sessions import jump_password_for
    from turbossh.gui.settings_dialog import SettingsDialog
    copy = jump("j-copy", "shared", jhost="rdp-box", juser="me")      # a 2.0.0 copy
    differs = jump("j-differs", "kept", jhost="rdp-box", juser="me")  # 2.0.0, its own
    elsewhere = jump("j-elsewhere", "shared", jhost="rdp-box", juser="other-user")
    dlg = SettingsDialog(window)
    try:
        dlg.jpass.setText("rotated")
        dlg.result_settings()
    finally:
        dlg.done(0)
        dlg.deleteLater()
    assert jump_password_for("j-copy", copy) == "rotated"           # follows it
    assert jump_password_for("j-differs", differs) == "kept"
    assert jump_password_for("j-elsewhere", elsewhere) == "shared"


# ------------------------------------------------------------------ 4: arguments hosts send
def test_numbers_as_strings_and_null_defaults_open_a_session(bridge, ssh_server):
    """2.0 took "port": "22" (paramiko does), null for an optional flag or port
    (the default), and a run timeout of 0 (no limit) or "30"; 2.1 refused them."""
    out = bridge.out
    rid = bridge.request("connect_ssh", host="127.0.0.1", port=str(ssh_server.port),
                         username="u", password="good", ignore_host_key=None,
                         legacy_algorithms=None, jump_port=None, quick_commands=None)
    reply = out.reply(rid)
    assert reply["ok"], reply
    sid = reply["result"]["session"]
    assert wait_until(lambda: out.events("session_connected", sid))
    for timeout in (0, "30", None):
        rid = bridge.request("run", session=sid, command="exit 3", timeout=timeout)
        assert out.reply(rid) is None, out.reply(rid)            # answered when it ends
        assert wait_until(lambda: out.reply(rid) is not None)
        assert out.reply(rid)["result"]["exit_code"] == 3, timeout
    rid = bridge.request("run", session=sid, command="exit 3", timeout=-1)
    assert "timeout" in out.reply(rid)["error"]


def test_a_baud_rate_as_a_string_reaches_the_opener_as_a_number(bridge, monkeypatch):
    opened = []

    def open_serial(device, *, baudrate=115200, name=None, quick_commands=()):
        opened.append((device, baudrate))
    monkeypatch.setattr(bridge.widget, "open_serial", open_serial)
    for baudrate in ("115200", 9600.0, None):
        rid = bridge.request("open_serial", device="COM7", baudrate=baudrate)
        assert bridge.out.reply(rid)["ok"], baudrate
    assert wait_until(lambda: len(opened) == 3)
    assert opened == [("COM7", 115200), ("COM7", 9600), ("COM7", 115200)]
    assert all(type(b) is int for _d, b in opened)
    for bad in ("fast", "9600.5", -9600, True, float("nan")):
        rid = bridge.request("open_serial", device="COM7", baudrate=bad)
        assert "baudrate" in bridge.out.reply(rid)["error"], bad


def test_null_in_set_chrome_leaves_that_part(bridge):
    rid = bridge.request("set_chrome", ribbon=None, sidebar=False)
    assert bridge.out.reply(rid)["ok"]
    win = bridge.widget.main_window
    assert wait_until(lambda: win._sidebar_dock.isHidden())
    assert not win._ribbon.isHidden()                # left as it was: shown


# ------------------------------------------------------------------ 5: sessions.json locked at start-up
def test_a_sessions_file_locked_at_start_up_is_read_once_it_can_be(qapp, monkeypatch):
    """Only an embedding host ever read it again: the desktop app showed an empty
    sidebar and Home for the whole run, and told the user to restore a copy that
    was never made."""
    pytest.importorskip("pyte")
    from PyQt5.QtWidgets import QMessageBox
    from turbossh._jsonstore import ReadProblem
    from turbossh.gui import sessions, settings
    from turbossh.gui.main_window import MainWindow
    monkeypatch.setattr(settings, "load_problem", None)      # this file only
    monkeypatch.setattr(settings, "save_problem", None)
    store = sessions.SessionStore()
    store.save({"name": "locked-at-start", "type": "ssh", "host": "h"})
    locked = [True]
    real = sessions.read_json

    def read_json(*args, **kwargs):
        if locked[0]:
            return None, ReadProblem("sessions.json could not be read right now "
                                     "(in use by another program).", transient=True)
        return real(*args, **kwargs)
    warned = []
    monkeypatch.setattr(sessions, "read_json", read_json)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(MainWindow, "_STORE_RETRY_MS", 20, raising=False)
    win = MainWindow()
    try:
        win.show()
        assert wait_until(lambda: warned)
        assert "restore the copy" not in warned[0] and "again" in warned[0]
        assert win.session_list.count() == 0
        pause(100)                                   # still locked: retried, still empty
        assert win.session_list.count() == 0
        locked[0] = False                            # the antivirus lets go
        assert wait_until(lambda: win.session_list.count() >= 1)
        assert "locked-at-start" in win.store.names()
        assert not win._store_retry.isActive()
    finally:
        win.shut_down()
        win.hide()
        win.deleteLater()
        locked[0] = False
        store.delete("locked-at-start")
        pause(30)


# ------------------------------------------------------------------ 6: answered just before the end
def test_an_open_answered_just_before_the_end_hears_its_end(bridge, ending):
    """Its work was a zero timer that never fires once the application quits:
    the id was answered, then never heard of again."""
    out = bridge.out
    rid = bridge.request("connect_ssh", host="127.0.0.1", port=1)
    sid = out.reply(rid)["result"]["session"]
    asked = bridge.request("confirm_close")
    bridge.finish(0)                                 # the host hung up, say
    bridge._outbox.join()
    assert [e["session"] for e in out.events("session_failed")] == [sid]
    assert [e["session"] for e in out.events("session_closed")] == [sid]
    answer = out.reply(asked)
    assert answer is not None and answer["ok"] is False and "shut down" in answer["error"]
    pause(100)                                       # the timers, if they still fire
    bridge._outbox.join()
    assert len(out.events("session_failed", sid)) == 1 and not out.events("session_opened")
    assert len([m for m in out.lines if m.get("id") == asked]) == 1


def test_a_run_under_way_at_the_end_is_answered(bridge, ending):
    out = bridge.out
    release, started = threading.Event(), threading.Event()

    class Handler:
        def run(self, command, timeout=None):
            started.set()
            release.wait(20)
            return types.SimpleNamespace(success=True, value=types.SimpleNamespace(
                exit_code=0, stdout="", stderr="", duration=0.0))
    bridge._by_id["s-fake"] = types.SimpleNamespace(handler=Handler())
    try:
        rid = bridge.request("run", session="s-fake", command="sleep 100")
        assert started.wait(10) and out.reply(rid) is None
        bridge.finish(0)
        bridge._outbox.join()
        answer = out.reply(rid)
        assert answer is not None and "shut down" in answer["error"]
    finally:
        release.set()
    pause(200)
    bridge._outbox.join()
    assert len([m for m in out.lines if m.get("id") == rid]) == 1
