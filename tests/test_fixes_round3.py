"""Regression tests for what the review of the second fix pass found: a Ctrl-C
fallback that left a paste to run as commands, Logs tab counts and Find, capture
parts left on disk, Cancel on slow links, the camera's release order and jump
password, a NUL stdin taken for a terminal, the shared jump host saved apart
from its password, the sessions.json retry, FTP downloads into folders that
take no new files, and the embed client's error after close()."""

from __future__ import annotations

import io
import json
import os
import sys
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
        QTest.qWait(20)
    return predicate()


# ---------------------------------------------------------------- local pty Ctrl-C
def test_the_fallback_signal_flushes_the_unread_paste_first(monkeypatch):
    """A pty full of a paste: SIGINT went straight to the job, the job died,
    and the shell then read what was left of the paste as commands. The line
    discipline flushes unread input before it signals; so does the fallback."""
    from turbossh.gui import localshell
    calls = []
    fake_termios = types.SimpleNamespace(
        ISIG=1, NOFLSH=0x80, VINTR=0, TCIFLUSH=0,
        tcgetattr=lambda fd: [0, 0, 0, 1, 0, 0, [b"\x03"]],
        tcflush=lambda fd, how: calls.append(("tcflush", fd, how)))
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setattr(os, "tcgetpgrp", lambda fd: 4242, raising=False)
    monkeypatch.setattr(os, "getpgrp", lambda: 1, raising=False)
    monkeypatch.setattr(os, "killpg", lambda group, sig: calls.append(("killpg", group)),
                        raising=False)
    opened = []
    real_open, real_close = os.open, os.close

    def fake_open(path, flags, *a):
        if path == "/dev/pts/7":
            opened.append(flags)
            return 99
        return real_open(path, flags, *a)
    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "close", lambda fd: None if fd == 99 else real_close(fd))
    shell = localshell.LocalShell.__new__(localshell.LocalShell)
    shell._master, shell._proc, shell._slave_name = 5, None, "/dev/pts/7"
    assert shell.signal_interrupt() is True
    assert calls == [("tcflush", 99, 0), ("killpg", 4242)]     # flushed, then signalled
    assert opened and opened[0] & getattr(os, "O_RDWR", 2)
    # NOFLSH set: the line discipline would not flush, nor does this
    calls.clear()
    fake_termios.tcgetattr = lambda fd: [0, 0, 0, 1 | 0x80, 0, 0, [b"\x03"]]
    assert shell.signal_interrupt() is True
    assert calls == [("killpg", 4242)]


# ---------------------------------------------------------------- Logs tab
class _OneShot:
    """A one-shot log command: N lines, then it ends."""
    known_os = "linux"

    def __init__(self, count):
        self.count = count

    def iter_lines(self, command, stop_event=None, get_pty=True):
        for i in range(self.count):
            yield f"boot[{i}]: message {i}"


def test_a_one_shot_commands_backlog_is_counted_after_it_ends(qapp):
    """The count was updated only while the worker ran: 120,000 lines of a
    one-shot command were stored, but the status said "stopped — 40000"."""
    from turbossh.gui import logs_tab
    tab = logs_tab.LogsTab()
    tab.set_handler(_OneShot(60_000))
    tab._start()
    try:
        assert wait_until(lambda: tab._thread is None and not tab._busy(), 60)
        assert wait_until(lambda: tab.statusrow.text().endswith("— 60000 lines"), 10), \
            tab.statusrow.text()
    finally:
        tab.close_tab()
        tab.deleteLater()


def test_filter_for_it_keeps_a_trailing_space(qapp):
    """re.escape("error ") is "error\\ "; the filter text is stripped, which
    left a lone backslash: "not a valid regex", and every line was shown."""
    from turbossh.gui import logs_tab
    tab = logs_tab.LogsTab()
    lines = [f"svc: info {i}" for i in range(80_000)]
    lines[10] = "svc: error E-1 at boot"
    tab._accept(lines)
    try:
        assert wait_until(lambda: not tab._busy(), 60)
        tab.find.setText("error ")
        assert tab.find_next() is False
        assert wait_until(lambda: "found" in tab._find_status.text())
        tab._find_show.click()
        assert wait_until(lambda: not tab._busy(), 60)
        assert "not a valid regex" not in tab.statusrow.text()
        assert tab.view.toPlainText().splitlines() == [lines[10]]
    finally:
        tab.close_tab()
        tab.deleteLater()


def test_a_find_result_for_an_older_view_is_dropped(qapp, monkeypatch):
    """Clear (or a new filter) while "searching older lines…": the answer still
    came, "found in 1 older line", with Filter for it, on an empty log."""
    from turbossh.gui import logs_tab
    tab = logs_tab.LogsTab()
    lines = [f"svc: info {i}" for i in range(80_000)]
    lines[10] = "svc: error E-2"
    tab._accept(lines)
    try:
        assert wait_until(lambda: not tab._busy(), 60)
        tab.find.setText("E-2")
        tab.find_next()                               # it searches the older lines
        seq = tab._find_seq
        tab._clear()                                  # Ctrl+L while it searches
        tab._on_find_done(seq, 1)                     # the worker's late answer
        assert tab._find_status.text() == ""
        assert tab._find_show.isHidden()
        tab._accept(lines)
        assert wait_until(lambda: not tab._busy(), 60)
        tab.find_next()
        seq = tab._find_seq
        tab.filter.setText("info")
        tab._apply_filter()                           # a new filter meanwhile
        tab._on_find_done(seq, 1)
        assert tab._find_status.text() == "" and tab._find_show.isHidden()
    finally:
        tab.close_tab()
        tab.deleteLater()


# ---------------------------------------------------------------- capture parts
def _rotated_terminal(monkeypatch, lines=1500):
    from turbossh.gui import vt100
    monkeypatch.setattr(vt100, "_CAPTURE_LIMIT", 64 * 1024)
    term = vt100.Vt100Terminal(send_fn=lambda data: None)
    for i in range(lines):
        term._feed(b"capture line %06d end\r\n" % i)
    assert len(term._cap_parts) == 3
    return term


def test_what_a_closed_terminal_left_is_swept_by_the_next_one(qapp, monkeypatch):
    """Closed during a Save on Windows, its parts could not be deleted; the
    sweep skipped every file of this process, so they stayed (up to 256 MB)
    until another process started."""
    pytest.importorskip("pyte")
    from turbossh.gui import vt100
    first = _rotated_terminal(monkeypatch)
    parts = list(first._cap_parts)
    held = [open(p, "rb") for p in parts]             # a Save reading them
    try:
        first.cleanup()
    finally:
        for fh in held:
            fh.close()
    leftovers = [p for p in parts if os.path.exists(p)]
    second = vt100.Vt100Terminal(send_fn=lambda data: None)    # it sweeps
    try:
        assert not any(os.path.exists(p) for p in leftovers)
        assert all(os.path.exists(p) for p in second._cap_parts)  # its own: kept
    finally:
        second.cleanup()
        second.deleteLater()
        first.deleteLater()


def test_a_save_removes_the_parts_dropped_while_it_read_them(qapp, tmp_path, monkeypatch):
    pytest.importorskip("pyte")
    term = _rotated_terminal(monkeypatch)
    parts = list(term._cap_parts)
    try:
        held = [open(p, "rb") for p in parts]
        term.cleanup()                                # the tab closes during the save
        for fh in held:
            fh.close()
        term._save_worker(parts, str(tmp_path / "out.txt"), [])
        assert not any(os.path.exists(p) for p in parts)
    finally:
        term.deleteLater()


# ---------------------------------------------------------------- camera
@pytest.fixture()
def panel(qapp, monkeypatch):
    from turbossh.gui import camera_widget, ffmpeg_tools
    monkeypatch.setattr(ffmpeg_tools, "cached_ffmpeg", lambda: sys.executable)
    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg",
                        lambda log=None, cancel=None: sys.executable)
    monkeypatch.setattr(ffmpeg_tools, "list_local_cameras", lambda ff: ["Cam A"])
    widget = camera_widget.CameraPanel()
    widget.show()
    assert wait_until(lambda: widget.camera.count() == 1)
    yield widget
    widget.close_session()
    widget.deleteLater()
    wait_until(lambda: False, 0.05)


def test_a_rescan_releases_the_camera_before_it_disconnects(panel, monkeypatch):
    """The release ran on one teardown and the old connection's disconnect on
    another: on a slow old link the disconnect won, and the remote ffmpeg kept
    the camera."""
    from turbossh.gui import qtutil
    order = []

    class Handler:
        def webcam_release(self, **kw):
            time.sleep(0.2)                           # a slow old link
            order.append("release")

        def disconnect(self, sticky=False):
            order.append("disconnect")

    class Chan:
        def close(self):
            pass
    jobs = []
    monkeypatch.setattr(qtutil, "run_teardown", lambda fn, name="": jobs.append(fn))
    monkeypatch.setattr(panel, "_start_prep", lambda prep, done: None)
    panel._handler, panel._chan = Handler(), Chan()
    panel._refresh()
    for job in jobs:                                  # however they are scheduled
        threading.Thread(target=job).start()
    deadline = time.monotonic() + 5
    while len(order) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert order == ["release", "disconnect"]
    assert panel._handler is None


def test_the_jump_password_does_not_follow_to_another_machine(panel, monkeypatch):
    """Picking the jump host filled in its password; picking another saved
    machine left it there, and Scan sent it to that machine."""
    from turbossh.gui import camera_widget
    monkeypatch.setattr(camera_widget.settings_mod, "shared_jump", lambda: {
        "host": "rdp-box", "user": "me", "domain": "", "password": "jump-secret"})
    monkeypatch.setattr(panel.r_host, "machine_for", lambda host: None)
    panel.r_host.setEditText("rdp-box") if hasattr(panel.r_host, "setEditText") \
        else panel.r_host.setText("rdp-box")
    panel._machine_picked()
    assert panel.r_pass.text() == "jump-secret" and panel.r_user.text() == "me"
    panel.r_host.setEditText("other-box") if hasattr(panel.r_host, "setEditText") \
        else panel.r_host.setText("other-box")
    panel._machine_picked()
    assert panel.r_pass.text() == ""
    # a password the user typed is theirs: kept
    panel.r_pass.setText("typed")
    panel._machine_picked()
    assert panel.r_pass.text() == "typed"
    # the jump host with another user: not its account, no password
    panel.r_pass.clear()
    panel.r_user.setText("someone-else")
    panel.r_host.setEditText("rdp-box") if hasattr(panel.r_host, "setEditText") \
        else panel.r_host.setText("rdp-box")
    panel._machine_picked()
    assert panel.r_pass.text() == ""


# ---------------------------------------------------------------- CLI: NUL stdin
def test_a_nul_stdin_is_not_a_terminal_to_prompt_at(monkeypatch):
    """On Windows, isatty() is True for NUL: install-ssh-remote run by a script
    with stdin=DEVNULL ignored TURBOSSH_PASSWORD and waited at a console
    prompt for ever."""
    from turbossh import cli
    monkeypatch.setenv("TURBOSSH_PASSWORD", "from-env")
    monkeypatch.setattr(cli, "prompt_password",
                        lambda *a: pytest.fail("prompted with nobody at the keyboard"))
    args = cli.build_parser().parse_args(
        ["install-ssh-remote", "--host", "10.0.0.9", "--user", "admin"])
    with open(os.devnull) as nul:
        monkeypatch.setattr(sys, "stdin", nul)
        assert not cli._stdin_is_console()
        assert cli._winrm_password(args, "admin").reveal() == "from-env"


# ---------------------------------------------------------------- shared jump host
@pytest.fixture()
def settings_file():
    from turbossh.gui import settings
    path = settings._FILE
    settings.save({"theme": settings.get("theme")})
    before = open(path, "rb").read()
    password = settings.jump_password()
    state = (settings.load_problem, settings.save_problem, dict(settings._unsaved))
    yield settings
    with open(path, "wb") as fh:
        fh.write(before)
    settings.load_problem, settings.save_problem = state[:2]
    settings._unsaved.clear()
    settings._unsaved.update(state[2])
    settings.reload()
    settings.set_jump_password(password)


def _another_process_saves(settings, **values):
    from turbossh._jsonstore import atomic_write_json
    with open(settings._FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    data.update(values)
    atomic_write_json(settings._FILE, data)


@pytest.fixture()
def window(qapp):
    pytest.importorskip("pyte")
    from turbossh.gui.main_window import MainWindow

    class Window(MainWindow):
        def closeEvent(self, event):
            event.accept()
    win = Window()
    win.show()
    qapp.processEvents()
    yield win
    win.shut_down()
    win.hide()
    win.deleteLater()


def test_ok_with_the_jump_page_untouched_pairs_nothing_new(settings_file, window):
    """The dialog showed X / its password; another process moved the jump host to
    Y / Y's password; OK in the first one wrote X's password next to Y."""
    from turbossh.gui.settings_dialog import SettingsDialog
    s = settings_file
    s.save({"jump_host": "x-box", "jump_user": "me"})
    s.set_jump_password("x-secret")
    dlg = SettingsDialog(window)
    try:
        assert dlg.jhost.text() == "x-box"
        _another_process_saves(s, jump_host="y-box")
        s.set_jump_password("y-secret")              # (the vault is shared)
        s.save(dlg.result_settings())
    finally:
        dlg.done(0)
        dlg.deleteLater()
    jump = s.shared_jump()
    assert (jump["host"], jump["password"]) == ("y-box", "y-secret")


def test_a_changed_jump_host_is_written_with_its_password(settings_file, window):
    """Back to a host this process had cached while the file held another: the
    host was not written (no change against the cache), the password was."""
    from turbossh.gui.settings_dialog import SettingsDialog
    s = settings_file
    s.save({"jump_host": "x-box", "jump_user": "me"})
    s.set_jump_password("x-secret")
    dlg = SettingsDialog(window)
    try:
        _another_process_saves(s, jump_host="y-box")
        dlg.jpass.setText("x-new")                   # a new password for x-box
        s.save(dlg.result_settings())
    finally:
        dlg.done(0)
        dlg.deleteLater()
    jump = s.shared_jump()
    assert (jump["host"], jump["password"]) == ("x-box", "x-new")


def test_a_jump_host_that_cannot_be_saved_changes_nothing(settings_file, window, monkeypatch):
    from turbossh.gui.settings_dialog import SettingsDialog
    s = settings_file
    s.save({"jump_host": "x-box", "jump_user": "me"})
    s.set_jump_password("x-secret")
    dlg = SettingsDialog(window)
    try:
        monkeypatch.setattr(s, "_read_for_save",
                            lambda: (None, "settings.json is in use by another program"))
        dlg.jhost.setText("z-box")
        dlg.jpass.setText("z-secret")
        result = dlg.result_settings()
        assert dlg.jump_problem and "in use" in dlg.jump_problem
        assert "jump_host" not in result
    finally:
        dlg.done(0)
        dlg.deleteLater()
    monkeypatch.undo()
    s.save({"term_font_size": s.get("term_font_size")})     # a later save
    jump = s.shared_jump()
    assert (jump["host"], jump["password"]) == ("x-box", "x-secret")


def test_jump_user_names_compare_without_case(window):
    from turbossh.gui import settings
    from turbossh.gui.sessions import SessionStore, jump_password_for
    settings.save({"jump_host": "rdp-box", "jump_user": "jdoe"})
    settings.set_jump_password("old")
    store = SessionStore()
    hop = {"use_jump": True, "jhost": "rdp-box", "type": "ssh", "host": "t"}
    store.save(dict(hop, name="r3-case", juser="JDoe"), "", "old")       # a 2.0.0 copy
    store.save(dict(hop, name="r3-fqdn", jhost="rdp-box.lab.example", juser="jdoe"),
               "", "old")
    try:
        kept = store.shared_jump_changing(
            {"host": "rdp-box", "user": "jdoe", "password": "old"},
            {"host": "rdp-box", "user": "jdoe", "password": "new"})
        settings.set_jump_password("new")
        assert jump_password_for("r3-case", store.get("r3-case")) == "new"
        assert kept == ["r3-fqdn"]                    # named, so the user can look
    finally:
        store.delete("r3-case")
        store.delete("r3-fqdn")
        settings.set_jump_password("")


# ---------------------------------------------------------------- sessions.json retry
def test_the_sessions_file_retry_backs_off(qapp, monkeypatch):
    """Each try can block the GUI thread for half a second; a file that stayed
    unreadable was tried every 1.5 s for the whole run."""
    pytest.importorskip("pyte")
    from turbossh.gui.main_window import MainWindow
    win = MainWindow.__new__(MainWindow)

    class Timer:
        def __init__(self):
            self.active, self.ms = False, 0

        def isActive(self):
            return self.active

        def interval(self):
            return self.ms

        def start(self, ms):
            self.active, self.ms = True, ms

        def stop(self):
            self.active = False
    store = types.SimpleNamespace(unread=True, reload_if_changed=lambda: False)
    win.__dict__.update(store=store, _store_retry=Timer())
    seen = []
    for _ in range(10):
        MainWindow.reload_saved_sessions(win)
        seen.append(win._store_retry.ms)
    assert seen[0] == MainWindow._STORE_RETRY_MS
    assert seen == sorted(seen) and seen[-1] == MainWindow._STORE_RETRY_MAX_MS


# ---------------------------------------------------------------- FTP pull
def test_an_ftp_download_into_a_folder_that_takes_no_new_files(monkeypatch, tmp_path):
    """The temporary file needs a new file in the folder; a file we may write in
    a folder we may not create files in was written in place by 2.0.0."""
    import builtins
    from turbossh import ftp
    from turbossh.config import FTPConfig
    target = tmp_path / "app.log"
    target.write_bytes(b"old")
    real_open = builtins.open

    def guarded(path, mode="r", *a, **kw):
        if str(path).endswith(".part") and "x" in mode:
            raise PermissionError(13, "Access is denied", str(path))
        return real_open(path, mode, *a, **kw)
    monkeypatch.setattr(builtins, "open", guarded)

    class Conn:
        def retrbinary(self, cmd, cb):
            cb(b"new ")
            cb(b"content")
    handler = ftp.FTPHandler(FTPConfig(host="ftp.lab"))
    monkeypatch.setattr(handler, "_require", lambda: Conn())
    result = handler.pull("/logs/app.log", str(target), safe=False)
    monkeypatch.undo()
    assert target.read_bytes() == b"new content"
    assert result.size_bytes == len(b"new content")
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".part")]


# ---------------------------------------------------------------- embed client
def test_a_call_after_close_hung_up_raises_turbossh_error():
    """After close() closed stdin, a call from another thread raised ValueError
    ("write to closed file"), not TurboSSHError, and its entry stayed."""
    from turbossh.embed_client import TurboSSHError, TurboSSHProcess

    class Closed(io.RawIOBase):
        def write(self, data):
            raise ValueError("write to closed file")

    class Proc:
        def __init__(self):
            read, write = os.pipe()
            self.stdout = os.fdopen(read, "rb")
            self._w = os.fdopen(write, "wb")
            self.stderr = io.BytesIO()
            self.stdin = Closed()

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass
    proc = Proc()
    gui = TurboSSHProcess(proc)
    try:
        with pytest.raises(TurboSSHError):
            gui.call("ping", timeout=1)
        assert not gui._pending
    finally:
        proc._w.close()
