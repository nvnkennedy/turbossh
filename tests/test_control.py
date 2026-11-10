"""TurboSSH for any application: the GUI's hosting options, the JSON-lines control
channel (in-process and through a real child process), and — opt-in, on a real
Windows desktop — living inside a foreign (Tk) window."""

from __future__ import annotations

import io
import json
import os
import sys
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


# ------------------------------------------------------------------ options (no Qt)
def test_hosting_options_parse():
    from turbossh.gui import options
    args = options.parse(["--embed", "0x1A", "--control", "stdio",
                          "--hide", "ribbon,log-dock", "--theme", "nord",
                          "--camera", "off", "-platform", "offscreen"])
    assert args.embed == 26 and args.control == "stdio" and options.is_hosted(args)
    assert args.hide == ["ribbon", "log_dock"]
    assert args.qt_args == ["-platform", "offscreen"]             # left for Qt
    assert options.parse(options.to_argv(args)).hide == ["ribbon", "log_dock"]
    assert not options.is_hosted(options.parse([]))


@pytest.mark.parametrize("argv", [["--hide", "ribbon"], ["--theme", "dark"],
                                  ["--embed", "nope"], ["--embed", "0"],
                                  ["--control", "stdio", "--hide", "wings"]])
def test_hosting_options_refuse_nonsense(argv):
    from turbossh.gui import options
    with pytest.raises(SystemExit):
        options.parse(argv)


def test_turbossh_gui_passes_the_hosting_options_on(monkeypatch):
    from turbossh import cli
    seen = []
    monkeypatch.setattr(cli, "launch_gui", lambda argv=None: seen.append(argv) or 0)
    assert cli.main(["gui", "--embed", "123", "--control", "stdio",
                     "--hide", "home"]) == 0
    assert seen == [["--embed", "123", "--control", "stdio", "--hide", "home"]]
    assert cli.main(["gui"]) == 0 and seen[-1] == []
    with pytest.raises(SystemExit):
        cli.main(["gui", "--camera", "on"])                        # needs hosting


def test_gui_help_prints_on_any_console():
    """`turbossh gui --help` piped on Windows is cp1252: a non-ASCII character in
    the help text made it crash with UnicodeEncodeError."""
    import argparse
    from turbossh import cli
    from turbossh.gui import options
    sub = next(a for a in cli.build_parser()._actions
               if isinstance(a, argparse._SubParsersAction)).choices["gui"]
    for text in (options.build_parser().format_help(), sub.format_help()):
        assert "--embed" in text
        text.encode("ascii")


def test_the_client_imports_without_qt():
    import subprocess
    code = ("import sys, turbossh.embed_client as c; assert 'PyQt5' not in sys.modules;"
            " print(c.PROTOCOL)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1"


# ------------------------------------------------------------------ the bridge, in-process


class _Out(io.RawIOBase):
    def __init__(self):
        self.lines = []
        self.broken = False

    def writable(self):
        return True

    def write(self, data):
        if self.broken:
            raise BrokenPipeError("host gone")
        for line in bytes(data).decode("utf-8").splitlines():
            self.lines.append(json.loads(line))
        return len(data)

    def events(self, name=None):
        return [m for m in self.lines if "event" in m and (name is None or m["event"] == name)]

    def reply(self, rid):
        return next((m for m in self.lines if m.get("id") == rid), None)


@pytest.fixture()
def bridge(qapp, monkeypatch):
    pytest.importorskip("pyte")
    from turbossh.gui import ffmpeg_tools, qtutil, settings, theme
    from turbossh.gui.control import ControlBridge
    from turbossh.gui.embed import TurboSSHWidget
    before = settings.load()
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
    theme.set_active(None)
    settings.save(before)
    widget.deleteLater()


def test_bad_requests_are_answered_not_fatal(bridge):
    out = bridge.out
    bridge._on_line(b"{not json")
    bridge._outbox.join()
    assert out.lines[-1]["ok"] is False and "not a JSON object" in out.lines[-1]["error"]
    bridge._on_line(b"[1, 2]")
    bridge._outbox.join()
    assert "JSON object" in out.lines[-1]["error"]
    rid = bridge.request("fly")
    assert out.reply(rid)["error"] == "unknown command 'fly'"
    rid = bridge.request("ping", extra=1)
    assert "unexpected keyword" in out.reply(rid)["error"]
    rid = bridge.request("connect_ssh")                            # no host
    assert "host" in out.reply(rid)["error"]
    rid = bridge.request("connect_ssh", host="h", colour="red")    # checked up front
    assert "colour" in out.reply(rid)["error"]
    bridge._on_line(json.dumps({"id": 99, "cmd": "ping", "args": [1]}).encode())
    bridge._outbox.join()
    assert "args must be" in out.reply(99)["error"]
    for cmd, args, words in (("screen_text", {"session": "s9"}, "no open session"),
                             ("set_theme", {"name": "neon"}, "unknown theme"),
                             ("set_chrome", {"wings": True}, "unknown part"),
                             ("trigger_action", {"name": "fly"}, "no action"),
                             ("set_geometry", {"x": 0, "y": 0, "width": 1, "height": 1},
                              "--embed"),
                             ("open_saved", {"name": "nobody"}, "no saved session")):
        rid = bridge.request(cmd, **args)
        assert words in out.reply(rid)["error"], cmd


def test_ping_and_queries(bridge):
    from turbossh import __version__
    rid = bridge.request("ping")
    assert bridge.out.reply(rid)["result"]["version"] == __version__
    rid = bridge.request("action_names")
    assert "session" in bridge.out.reply(rid)["result"]
    rid = bridge.request("current_theme")
    assert bridge.out.reply(rid)["result"] == "dark"
    rid = bridge.request("camera_enabled")
    assert bridge.out.reply(rid)["result"] is False
    rid = bridge.request("window")
    assert bridge.out.reply(rid)["result"]["embedded_in"] is None


def test_an_ssh_session_over_the_channel(bridge, ssh_server):
    out = bridge.out
    rid = bridge.request("connect_ssh", host="127.0.0.1", port=ssh_server.port,
                         username="u", password="good", name="loop")
    reply = out.reply(rid)
    sid = reply["result"]["session"]
    # answered before anything was opened: the UI work comes after the answer
    assert out.lines.index(reply) == len(out.lines) - 1 and not out.events("session_opened")
    assert wait_until(lambda: out.events("session_opened"))
    assert out.events("session_opened")[0] == {"event": "session_opened", "session": sid,
                                               "name": "loop", "kind": "ssh",
                                               "state": "connecting"}
    rid = bridge.request("send_line", session=sid, text="too early")
    assert "connecting" in out.reply(rid)["error"]
    assert wait_until(lambda: out.events("session_connected"))

    rid = bridge.request("send_line", session=sid, text="hello-channel")
    assert out.reply(rid)["ok"]

    def screen():
        rid = bridge.request("screen_text", session=sid)
        return out.reply(rid)["result"]["text"]
    assert wait_until(lambda: "hello-channel" in screen())
    rid = bridge.request("send", session=sid, data_base64="AwQ=")  # raw bytes
    assert out.reply(rid)["ok"]
    rid = bridge.request("send", session=sid)
    assert "exactly one" in out.reply(rid)["error"]

    rid = bridge.request("run", session=sid, command="exit 3")
    assert out.reply(rid) is None                                 # answered later
    assert wait_until(lambda: out.reply(rid) is not None)
    result = out.reply(rid)["result"]
    assert result["exit_code"] == 3 and "failing" in result["stderr"]
    rid = bridge.request("run", session=sid, command="uname -s")
    assert wait_until(lambda: out.reply(rid) is not None)
    assert out.reply(rid)["result"]["stdout"].strip() == "Linux"

    rid = bridge.request("sessions")
    assert out.reply(rid)["result"][0]["state"] == "connected"
    rid = bridge.request("current_session")
    assert out.reply(rid)["result"] == {"session": sid}
    rid = bridge.request("close_session", session=sid)
    assert out.reply(rid)["ok"]
    assert wait_until(lambda: out.events("session_closed"))
    rid = bridge.request("sessions")
    assert out.reply(rid)["result"] == []


def test_a_failed_open_is_reported_under_its_id(bridge, monkeypatch):
    out = bridge.out

    def boom(*_a, **_k):
        raise OSError("no such port")
    monkeypatch.setattr(bridge.widget, "open_serial", boom)
    rid = bridge.request("open_serial", device="COM99")
    sid = out.reply(rid)["result"]["session"]
    assert wait_until(lambda: out.events("session_closed"))
    failed = out.events("session_failed")[0]
    assert failed["session"] == sid and "no such port" in failed["message"]


def test_window_commands_happen_after_the_answer(bridge):
    out = bridge.out
    rid = bridge.request("set_theme", name="nord")
    assert out.reply(rid)["ok"] and not out.events("theme_changed")
    assert wait_until(lambda: out.events("theme_changed"))
    assert out.events("theme_changed")[-1]["theme"] == "nord"
    bridge.request("set_camera_enabled", enabled=True)
    assert wait_until(lambda: out.events("camera_enabled_changed"))
    rid = bridge.request("set_chrome", ribbon=False)
    assert wait_until(lambda: not bridge.widget.main_window._ribbon.isVisible())
    rid = bridge.request("open_local_terminal")
    sid = out.reply(rid)["result"]["session"]
    assert wait_until(lambda: out.events("session_opened"))
    assert out.events("session_opened")[-1]["kind"] == "local"
    bridge.request("close_session", session=sid)
    assert wait_until(lambda: out.events("session_closed"))


def test_confirm_close_is_answered_when_the_user_has(bridge, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    rid = bridge.request("confirm_close")
    assert bridge.out.reply(rid) is None
    assert wait_until(lambda: bridge.out.reply(rid) is not None)
    assert bridge.out.reply(rid)["result"] is True                 # nothing was open


def test_log_lines_become_events(bridge):
    bridge.widget.main_window.log_panel.append("[OK] something happened")
    bridge._outbox.join()
    assert bridge.out.events("log")[-1]["text"] == "[OK] something happened"


def test_a_host_that_stopped_listening_ends_it(bridge):
    gone = []
    bridge.host_gone.connect(lambda: gone.append(True))
    bridge.out.broken = True
    bridge.send_event("log", text="x")
    bridge.send_event("log", text="y")                             # once is enough
    assert wait_until(lambda: gone == [True])


def test_shutdown_answers_then_closes_and_quits(bridge, monkeypatch, ssh_server):
    from PyQt5.QtWidgets import QApplication
    quits = []
    monkeypatch.setattr(QApplication, "quit", lambda *a: quits.append(True))
    rid = bridge.request("connect_ssh", host="127.0.0.1", port=ssh_server.port,
                         username="u", password="good")
    assert wait_until(lambda: bridge.out.events("session_connected"))
    rid = bridge.request("shutdown", wait=5)
    assert bridge.out.reply(rid)["ok"]
    assert wait_until(lambda: quits == [True])
    assert bridge.widget.is_shut_down and bridge.out.events("session_closed")


# ------------------------------------------------------------------ the reference
def test_every_command_event_and_client_method_is_in_the_reference():
    """EMBEDDING.md is the contract for hosts in other languages: it cannot drift.
    Read from the source, not imported: this check also runs where PyQt5 is not
    installed (the library and Python 3.8 CI jobs)."""
    import re
    from turbossh.embed_client import TurboSSHProcess
    with open(os.path.join(ROOT, "EMBEDDING.md"), encoding="utf-8") as fh:
        text = fh.read()
    with open(os.path.join(ROOT, "turbossh", "gui", "control.py"), encoding="utf-8") as fh:
        source = fh.read()
    commands = re.findall(r"^    def cmd_([a-z_]+)\(", source, flags=re.M)
    events = set(re.findall(r'send_event\("([a-z_]+)"', source))
    methods = [n for n, v in vars(TurboSSHProcess).items()
               if not n.startswith("_") and (callable(v) or isinstance(v, (property, classmethod)))]
    assert len(commands) > 30 and len(events) > 10
    missing = [n for n in commands + sorted(events) + methods if f"`{n}" not in text]
    assert not missing, f"not documented in EMBEDDING.md: {missing}"


# ------------------------------------------------------------------ a real child process
def _child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def _launch(**kwargs):
    from turbossh.embed_client import TurboSSHProcess
    command = os.environ.get("TURBOSSH_TEST_EXE")
    return TurboSSHProcess.launch(command=[command] if command else None,
                                  env=_child_env(), cwd=ROOT, **kwargs)


def test_child_process_end_to_end(ssh_server):
    pytest.importorskip("PyQt5")
    from turbossh.embed_client import TurboSSHError
    gui = _launch(hide=["log_dock"], theme="paper", camera=False)
    try:
        assert gui.ready["protocol"] == 1 and gui.window
        assert gui.call("current_theme") == "paper"
        assert gui.call("camera_enabled") is False
        sid = gui.connect_ssh("127.0.0.1", port=ssh_server.port, username="u",
                              password="good", name="child")
        assert gui.wait_for("session_opened", sid)["kind"] == "ssh"
        gui.wait_for("session_connected", sid)
        gui.send_line(sid, "from-the-host")
        deadline = time.monotonic() + 15
        while "from-the-host" not in gui.screen_text(sid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "from-the-host" in gui.screen_text(sid)
        assert gui.run(sid, "uname -s")["stdout"].strip() == "Linux"
        assert gui.sessions()[0]["session"] == sid
        with pytest.raises(TurboSSHError, match="unknown theme") as info:
            gui.set_theme("neon")
        assert info.value.type == "ProtocolError"
        events = []
        gui.on_event(events.append)
        gui.close_session(sid)
        gui.wait_for("session_closed", sid)
        assert any(e["event"] == "session_closed" for e in events + gui.poll_events())
    finally:
        code = gui.close(wait=3)
    assert code == 0
    assert gui.wait_for("exited", timeout=5)
    with pytest.raises(TurboSSHError, match="not running"):
        gui.call("ping")


def test_child_process_ends_when_the_host_hangs_up():
    pytest.importorskip("PyQt5")
    gui = _launch()
    gui.process.stdin.close()                       # the host went away
    assert gui.process.wait(timeout=30) == 0


def test_a_window_handle_that_does_not_exist_is_reported():
    pytest.importorskip("PyQt5")
    if os.name != "nt":
        pytest.skip("handle checking is Windows-only")
    from turbossh.embed_client import TurboSSHError
    with pytest.raises(TurboSSHError, match="no window with handle 1234567"):
        _launch(parent=1234567)


def test_a_host_that_reads_slowly_never_blocks_the_ui(bridge):
    """Writes go through a queue: a full pipe must not freeze TurboSSH."""
    from turbossh.gui import control
    import threading
    gate = threading.Event()
    written = []

    class Stuck(io.RawIOBase):
        def writable(self):
            return True

        def write(self, data):
            gate.wait(10)                    # a host that is not reading
            written.append(data)
            return len(data)
    bridge._out = Stuck()
    started = time.monotonic()
    for i in range(control._BACKLOG + 50):
        bridge.send_event("log", text=f"line {i}")
    bridge.send_event("theme_changed", theme="x")    # never dropped
    assert time.monotonic() - started < 5
    assert bridge._outbox.qsize() <= control._BACKLOG + 3
    gate.set()
    bridge.flush(10)
    assert b'"theme_changed"' in written[-1]


def test_a_child_that_cannot_start_is_reported():
    from turbossh.embed_client import TurboSSHError, TurboSSHProcess
    with pytest.raises(TurboSSHError, match="exited"):
        TurboSSHProcess.launch(command=[sys.executable, "-c", "import sys; "
                                        "sys.stderr.write('broken install'); sys.exit(3)"],
                               timeout=20)


# ------------------------------------------------------------------ a real desktop (opt-in)
desktop = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("TURBOSSH_DESKTOP_TESTS") != "1",
    reason="opens real windows: set TURBOSSH_DESKTOP_TESTS=1 on a Windows desktop")


@desktop
def test_lives_inside_a_tk_window_and_follows_it(tmp_path):
    import ctypes
    import tkinter as tk
    from ctypes import wintypes
    from turbossh.embed_client import TurboSSHProcess
    user32 = ctypes.WinDLL("user32")
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    def rect(handle):
        r = wintypes.RECT()
        user32.GetWindowRect(handle, ctypes.byref(r))
        return (r.left, r.top, r.right, r.bottom)

    root = tk.Tk()
    root.geometry("900x600+100+80")
    box = tk.Frame(root)
    box.pack(fill="both", expand=True, padx=12, pady=12)

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(0.01)
    pump(0.3)
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env["PYTHONPATH"] = ROOT
    command = os.environ.get("TURBOSSH_TEST_EXE")
    gui = TurboSSHProcess.launch(parent=box.winfo_id(), env=env, cwd=ROOT,
                                 command=[command] if command else None)
    try:
        pump(1.0)
        assert user32.GetParent(gui.window) == box.winfo_id()
        assert rect(gui.window) == rect(box.winfo_id())
        root.geometry("1150x720+100+80")
        pump(1.0)
        assert rect(gui.window) == rect(box.winfo_id())
        gui.set_geometry(10, 20, 300, 200)
        pump(0.5)
        left, top = rect(box.winfo_id())[:2]
        assert rect(gui.window)[:2] == (left + 10, top + 20)
        gui.fill_parent()
        pump(0.5)
        assert rect(gui.window) == rect(box.winfo_id())
        sid = gui.open_local_terminal()
        gui.wait_for("session_opened", sid)
        pump(1.5)
        gui.send_line(sid, "echo inside-tk")
        deadline = time.time() + 15
        while "inside-tk" not in gui.screen_text(sid).split("echo inside-tk")[-1] \
                and time.time() < deadline:
            pump(0.1)
        assert "inside-tk" in gui.screen_text(sid).split("echo inside-tk")[-1]
    finally:
        root.destroy()                              # the host just closes its window
    assert gui.process.wait(timeout=30) == 0        # TurboSSH notices and ends cleanly
    assert gui.wait_for("parent_closed", timeout=5)
