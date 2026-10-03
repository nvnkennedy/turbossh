"""Library pieces that need neither a server nor Qt: results, the JSON store,
the session store, update logic, tunnels, serial, WinRM helpers, logging, CLI."""

from __future__ import annotations

import io
import json
import logging
import os

import pytest

from turbossh import _jsonstore, update
from turbossh.results import strip_ansi


# ------------------------------------------------------------------ results
@pytest.mark.parametrize("raw, clean", [
    ("\x1b[31mred\x1b[0m\r\nok", "red\nok"),
    ("\x1b(B\x1b[mload average", "load average"),       # ncurses sgr0
    ("\x1b]0;my title\x07plain", "plain"),
    ("x\x1b]0;t\x1b\\y", "xy"),
    ("a\x1b7b\x1b8c\x1bMd", "abcd"),                     # save/restore cursor, RI
    ("\x1bPsome dcs\x1b\\text", "text"),
    ("a\x1b[2K\x1b[1Gb", "ab"),
])
def test_strip_ansi(raw, clean):
    assert strip_ansi(raw) == clean


# ------------------------------------------------------------------ json store
def test_atomic_write_leaves_only_the_file(tmp_path):
    target = tmp_path / "deep" / "data.json"
    _jsonstore.atomic_write_json(str(target), {"a": 1})
    _jsonstore.atomic_write_json(str(target), {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2}
    assert [p.name for p in target.parent.iterdir()] == ["data.json"]


def test_a_failed_write_keeps_the_previous_file(tmp_path):
    target = tmp_path / "data.json"
    _jsonstore.atomic_write_json(str(target), {"good": True})
    with pytest.raises(TypeError):
        _jsonstore.atomic_write_json(str(target), {"bad": object()})
    assert json.loads(target.read_text()) == {"good": True}
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_unreadable_file_is_copied_aside_and_reported(tmp_path):
    target = tmp_path / "sessions.json"
    target.write_text('[{"name": "a"},')                 # cut off mid-write
    value, problem = _jsonstore.read_json(str(target), list)
    assert value is None and "could not be read" in problem
    backups = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
    assert len(backups) == 1 and backups[0].read_text() == '[{"name": "a"},'

    target.write_text('{"not": "a list"}')
    value, problem = _jsonstore.read_json(str(target), list)
    assert value is None and "unexpected layout" in problem
    assert _jsonstore.read_json(str(tmp_path / "missing.json"), list) == (None, None)


# ------------------------------------------------------------------ sessions
@pytest.fixture()
def store_file(tmp_path, monkeypatch):
    from turbossh.gui import sessions
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(sessions, "_FILE", str(path))
    return path


def test_session_store_survives_a_corrupt_file(store_file):
    from turbossh.gui.sessions import SessionStore
    store_file.write_text('[{"name": "kept"')
    store = SessionStore()
    assert store.sessions == [] and store.load_problem
    store.save({"name": "new"}, "pw")
    backups = [p for p in store_file.parent.iterdir() if ".corrupt-" in p.name]
    assert backups and "kept" in backups[0].read_text()


def test_session_save_replaces_in_place_and_rename_moves_secrets(store_file):
    from turbossh.gui.sessions import SessionStore
    store = SessionStore()
    for name in ("a", "b", "c"):
        store.save({"name": name, "host": name}, f"pw-{name}", f"jump-{name}")
    store.save({"name": "a", "host": "changed"}, "pw-a2")
    assert store.names() == ["a", "b", "c"], "editing moved the session"
    assert store.get("a")["host"] == "changed"

    assert store.rename("a", "c") is False               # taken
    assert store.rename("a", "renamed") is True
    assert store.names() == ["renamed", "b", "c"]
    assert SessionStore.password("renamed") == "pw-a2"
    assert SessionStore.jump_password("renamed") == "jump-a"
    assert SessionStore.password("a") is None

    assert store.unique_name("b") == "b (2)"
    store.save({"name": "b (2)"})
    assert store.unique_name("b") == "b (3)"
    store.delete("b")
    assert store.names() == ["renamed", "c", "b (2)"]


def test_two_windows_do_not_overwrite_each_others_sessions(store_file):
    from turbossh.gui.sessions import SessionStore
    first, second = SessionStore(), SessionStore()
    first.save({"name": "from-first"})
    second.save({"name": "from-second"})                 # had a stale, empty list
    assert SessionStore().names() == ["from-first", "from-second"]


# ------------------------------------------------------------------ update
def test_version_comparison():
    assert update.is_newer("1.6.1", "1.6.0")
    assert not update.is_newer("1.6", "1.6.0")
    assert not update.is_newer("1.6.0rc1", "1.6.0")
    assert update.is_newer("1.10.0", "1.9.9")
    assert not update.is_newer(None, "1.0.0")


def test_owning_python_is_found_from_where_the_exe_lives(tmp_path):
    def exe_under(prefix):
        folder = prefix / "Lib" / "site-packages" / "turbossh" / "bin"
        folder.mkdir(parents=True)
        exe = folder / "turbossh-gui.exe"
        exe.write_text("x")
        return str(exe)

    system = tmp_path / "Python311"
    exe = exe_under(system)
    (system / "python.exe").write_text("x")
    assert update.owning_python(exe) == str(system / "python.exe")

    venv = tmp_path / "venv"
    exe = exe_under(venv)
    (venv / "Scripts").mkdir()
    (venv / "Scripts" / "python.exe").write_text("x")
    assert update.owning_python(exe) == str(venv / "Scripts" / "python.exe")

    # a downloaded, standalone exe: nothing for pip to upgrade
    lone = tmp_path / "Downloads" / "turbossh-gui.exe"
    lone.parent.mkdir()
    lone.write_text("x")
    assert update.owning_python(str(lone)) is None


def test_update_script_waits_pins_verifies_and_quotes():
    script = update.build_update_script(r"C:\Users\O'Brien\py\python.exe", "1.7.0",
                                        r"C:\app\turbossh-gui.exe", 4242)
    assert "Wait-Process -Id 4242" in script
    assert "'turbossh==1.7.0'" in script
    assert "turbossh.__version__" in script
    assert r"'C:\Users\O''Brien\py\python.exe'" in script
    assert "Start-Sleep" not in script


# ------------------------------------------------------------------ tunnels
def test_each_remote_forward_gets_its_own_handler():
    from turbossh.tunnel import RemoteForward

    class FakeTransport:
        def __init__(self):
            self.handlers = {}

        def request_port_forward(self, address, port, handler=None):
            self.handlers[port] = handler
            return port

        def cancel_port_forward(self, address, port):
            pass

    transport = FakeTransport()
    a = RemoteForward(transport, 9001, "127.0.0.1", 1)
    b = RemoteForward(transport, 9002, "127.0.0.1", 2)
    assert transport.handlers[9001] == a._on_channel
    assert transport.handlers[9002] == b._on_channel
    assert not hasattr(a, "_accept_loop"), "the shared accept queue is back"

    class Chan:
        closed = False

        def close(self):
            self.closed = True

    a.close()
    chan = Chan()
    a._on_channel(chan, ("x", 1), ("y", 2))               # after close: refused
    assert chan.closed


def test_local_forward_does_not_share_a_busy_port_on_windows():
    from turbossh.tunnel import _ForwardServer
    assert _ForwardServer.allow_reuse_address is (os.name != "nt")


# ------------------------------------------------------------------ serial
def test_serial_errors_are_part_of_our_hierarchy(monkeypatch):
    serial_handler = pytest.importorskip("turbossh.serial_handler")
    if not serial_handler.serial_available():
        pytest.skip("pyserial not installed")
    from turbossh import SSHError

    def boom(*args, **kwargs):
        raise serial_handler.serial.SerialException("could not open port 'COM99'")
    monkeypatch.setattr(serial_handler.serial, "Serial", boom)
    with pytest.raises(SSHError) as caught:
        serial_handler.SerialHandler("COM99", quiet=True).open()
    assert isinstance(caught.value, serial_handler.SerialError)


def test_serial_read_available_asks_only_for_what_has_arrived():
    serial_handler = pytest.importorskip("turbossh.serial_handler")
    if not serial_handler.serial_available():
        pytest.skip("pyserial not installed")

    class FakePort:
        is_open = True

        def __init__(self, waiting):
            self.in_waiting = waiting
            self.asked = []

        def read(self, n):
            self.asked.append(n)
            return b"x" * min(n, self.in_waiting)

    handler = serial_handler.SerialHandler("COM1", quiet=True)
    handler._ser = FakePort(waiting=7)
    assert handler.read_available() == b"x" * 7 and handler._ser.asked == [7]
    handler._ser = FakePort(waiting=0)
    handler.read_available()
    assert handler._ser.asked == [1], "an idle port must block for ONE byte only"


# ------------------------------------------------------------------ winrm
def test_shared_winrm_shell_reuses_one_shell():
    from turbossh.winrm_bootstrap import _SharedShell
    calls = []

    class Protocol:
        def open_shell(self):
            calls.append("open"); return "S"

        def run_command(self, shell, command):
            calls.append("run"); return "C"

        def get_command_output(self, shell, command):
            calls.append("out"); return b"ok", b"", 0

        def cleanup_command(self, shell, command):
            calls.append("cleanup")

        def close_shell(self, shell):
            calls.append("close")

    class Session:
        protocol = Protocol()

    shell = _SharedShell(Session())
    assert shell.run_ps("'a'") == (b"ok", b"", 0)
    shell.run_ps("'b'")
    shell.close()
    assert calls == ["open", "run", "out", "cleanup", "run", "out", "cleanup", "close"]


def test_winrm_scripts_set_one_port_line_and_update_the_firewall_rule():
    from turbossh import winrm_bootstrap as w
    for script in (w._ENABLE_SCRIPT.format(ssh_port=2222, extra=""),
                   w._INSTALL_FROM_ZIP.format(zip="z", port=2222)):
        assert "@('Port 2222')" in script
        assert "Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -LocalPort 2222" in script


# ------------------------------------------------------------------ logging
def test_the_library_does_not_own_logging():
    from turbossh import SSHConfig, SSHHandler
    package = logging.getLogger("turbossh")
    level_before = package.level
    noisy = SSHHandler(SSHConfig(host="log-test-host"))
    quiet = SSHHandler(SSHConfig(host="log-test-host"), quiet=True)
    assert noisy.log is quiet.log
    assert not noisy.log.handlers, "a per-host logger grew its own handler"
    assert noisy.log.level == logging.NOTSET, "quiet= changed a shared logger"
    assert package.level == level_before
    seen = []
    quiet._log_callback = seen.append
    quiet._emit(logging.INFO, "hello")
    assert seen == ["[INFO] hello"]                     # callbacks still get it


# ------------------------------------------------------------------ pool
def test_pool_pull_template_tolerates_other_braces(monkeypatch):
    from turbossh import SSHConfig, SSHPool
    pool = SSHPool([SSHConfig(host="h1", password="x")])
    handler = pool.handlers["h1"]
    handler.connect = lambda: None
    monkeypatch.setattr(type(handler), "is_connected", property(lambda self: True))
    got = []
    handler.pull = lambda remote, local, **kw: got.append(local)
    pool.pull("/var/log/x", "logs/{host}_{date}.txt")
    assert got == ["logs/h1_{date}.txt"]


# ------------------------------------------------------------------ CLI
def test_cli_version_and_exit_codes(capsys, monkeypatch):
    from turbossh import __version__, cli
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    assert stop.value.code == 0 and __version__ in capsys.readouterr().out

    def interrupted(argv):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "_main", interrupted)
    assert cli.main([]) == 130

    def bad_regex(argv):
        import re
        re.compile("(")
    monkeypatch.setattr(cli, "_main", bad_regex)
    assert cli.main([]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_cli_does_not_import_paramiko_until_it_connects():
    import subprocess
    import sys
    code = ("import sys, turbossh.cli; "
            "print('turbossh.core' in sys.modules, 'paramiko' in sys.modules)")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=root)
    assert out.stdout.split() == ["False", "False"], out.stderr


def test_cli_run_with_password_from_stdin(ssh_server, monkeypatch, capsys):
    from turbossh import cli
    monkeypatch.setattr("sys.stdin", io.StringIO("good\n"))
    code = cli.main(["run", "--host", "127.0.0.1", "--port", str(ssh_server.port),
                     "--user", "u", "--password-stdin", "exit", "3"])
    assert code == 3                                     # the remote exit code
    monkeypatch.setenv("TURBOSSH_PASSWORD", "good")
    code = cli.main(["run", "--host", "127.0.0.1", "--port", str(ssh_server.port),
                     "--user", "u", "--json", "echo"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["stdout"] == "ok\n"


def test_cli_stored_credentials_are_per_host(monkeypatch):
    from turbossh import cli
    assert cli._vault_key("root", "10.0.0.5") == "root@10.0.0.5"
    assert cli._vault_key("root", None) == "root"
    assert cli._ps_quote("C:\\Users\\O'Brien") == "'C:\\Users\\O''Brien'"
