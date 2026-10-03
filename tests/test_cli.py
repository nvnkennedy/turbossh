"""Every CLI command, run for real against the loopback server (or with the one
external piece it needs replaced), and a check that CLI.md documents exactly the
parser that exists."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import socket
import threading
import time

import pytest

from turbossh import __version__, cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ CLI.md
def _subcommands():
    parser = cli.build_parser()
    action = next(a for a in parser._actions
                  if isinstance(a, argparse._SubParsersAction))
    return action.choices


def _doc_sections():
    """{command: text of its section} from CLI.md."""
    text = open(os.path.join(ROOT, "CLI.md"), encoding="utf-8").read()
    parts = re.split(r"^### `turbossh ([a-z-]+)`\s*$", text, flags=re.M)
    sections = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}
    # a section ends where the next "## " chapter starts
    return text, {name: body.split("\n## ")[0] for name, body in sections.items()}


def test_cli_md_documents_every_command_and_nothing_else():
    _text, sections = _doc_sections()
    assert sorted(sections) == sorted(_subcommands())


def test_cli_md_documents_every_option():
    text, sections = _doc_sections()
    shared = text.split("## Connection options")[1].split("\n## ")[0]
    missing = []
    for name, parser in _subcommands().items():
        connects = "connection options" in sections[name].lower()
        for action in parser._actions:
            for option in action.option_strings:
                if not option.startswith("--") or option == "--help":
                    continue
                documented = f"`{option}" in sections[name] or (
                    connects and f"`{option}" in shared)
                if not documented:
                    missing.append(f"{name} {option}")
    assert not missing, f"not in CLI.md: {missing}"


def test_cli_md_does_not_document_options_that_do_not_exist():
    _text, sections = _doc_sections()
    connection = {o for a in _subcommands()["run"]._actions for o in a.option_strings}
    stale = []
    for name, parser in _subcommands().items():
        real = {o for a in parser._actions for o in a.option_strings} | connection
        table = "\n".join(line for line in sections[name].splitlines()
                          if line.startswith("| `--"))
        for option in re.findall(r"`(--[a-z-]+)", table):
            if option not in real:
                stale.append(f"{name} {option}")
    assert not stale, f"documented but not in the parser: {stale}"


def test_entry_points_resolve_to_callables():
    import importlib
    text = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    targets = re.findall(r'^turbossh[a-z-]* = "([\w.]+):(\w+)"', text, flags=re.M)
    assert len(targets) == 5
    for module, name in targets:
        assert callable(getattr(importlib.import_module(module), name))


# ------------------------------------------------------------------ helpers
@pytest.fixture()
def run(capsys, monkeypatch, ssh_server):
    """run(*argv) -> (exit code, stdout, stderr), against the loopback server."""
    monkeypatch.setenv("TURBOSSH_PASSWORD", "good")

    def _run(*argv, target=True):
        args = [str(a) for a in argv]
        if target:
            args = args[:1] + ["--host", "127.0.0.1", "--port", str(ssh_server.port),
                               "--user", "u", "--host-key-policy", "ignore"] + args[1:]
        capsys.readouterr()
        try:
            code = cli.main(args)
        except SystemExit as stop:               # argparse errors / --version
            code = stop.code
        out = capsys.readouterr()
        return code, out.out, out.err
    return _run


# ------------------------------------------------------------------ remote commands
def test_run_returns_the_remote_exit_code_and_json(run):
    assert run("run", "echo") == (0, "ok\n", "")
    code, out, err = run("run", "exit", "3")
    assert code == 3 and err == "failing\n"
    code, out, _ = run("run", "--json", "echo")
    assert code == 0 and json.loads(out)["stdout"] == "ok\n"
    code, _, err = run("run")
    assert code == 2 and "No command" in err


def test_run_times_out_cleanly(run):
    code, _, err = run("run", "--timeout", "1", "hang")
    assert code == 1 and "timed out" in err and "Traceback" not in err


def test_sudo(run):
    code, out, _ = run("sudo", "whoami")
    assert (code, out.strip()) == (0, "root")
    code, out, _ = run("sudo", "--json", "whoami")
    assert json.loads(out)["exit_code"] == 0


def test_push_pull_and_ls(run, ssh_server, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("payload")
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "b.txt").write_text("bb")
    assert run("push", src, "/up/a.txt")[0] == 0
    assert run("push", "--recursive", tree, "/tree")[0] == 0
    assert (ssh_server.root / "up" / "a.txt").read_text() == "payload"
    assert (ssh_server.root / "tree" / "sub" / "b.txt").read_text() == "bb"

    assert run("pull", "/up/a.txt", tmp_path / "back.txt")[0] == 0
    assert (tmp_path / "back.txt").read_text() == "payload"
    code, out, _ = run("pull", "--json", "--recursive", "/tree", tmp_path / "copy")
    assert code == 0 and json.loads(out)["files"] == 1
    code, _, err = run("pull", "/nope", tmp_path / "nope")
    assert code == 1 and "does not exist" in err

    code, out, _ = run("ls", "/")
    lines = out.splitlines()
    assert code == 0 and lines[0].endswith("tree/") and lines[1].endswith("up/")
    code, out, _ = run("ls", "--json", "/up")
    assert json.loads(out) == [{"name": "a.txt", "is_dir": False, "size": 7,
                                "mtime": json.loads(out)[0]["mtime"],
                                "mode": json.loads(out)[0]["mode"]}]


def test_info(run):
    code, out, _ = run("info", "--json")
    assert code == 0
    assert json.loads(out) == {"host": "127.0.0.1", "remote_os": "linux",
                               "connected": True}


def test_stream_with_match_and_save(run, tmp_path):
    saved = tmp_path / "logs" / "stream.log"
    code, out, err = run("stream", "--match", "o.", "--stop-on-match",
                         "--save", saved, "echo")
    assert code == 0 and out == "ok\n" and "1 matched" in err
    assert saved.read_text().strip() == "ok"
    code, _, err = run("stream", "--match", "(", "echo")
    assert code == 1 and "ERROR" in err and "Traceback" not in err


def test_forward_relays_and_ends_when_the_connection_drops(run, ssh_server):
    with socket.socket() as probe:               # a free local port
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(code=run(
            "forward", "--local-port", local_port, "--to-host", "127.0.0.1",
            "--to-port", ssh_server.port)[0]), daemon=True)
    worker.start()
    banner, deadline = b"", time.monotonic() + 20
    while not banner and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=2) as s:
                s.settimeout(5)
                banner = s.recv(64)
        except OSError:
            time.sleep(0.2)
    assert banner.startswith(b"SSH-2.0"), "the forward did not reach the far side"
    ssh_server.close()                           # the SSH link goes away
    worker.join(20)
    assert result.get("code") == 1


def test_copy_id(run, tmp_path):
    public = tmp_path / "id.pub"
    public.write_text("ssh-ed25519 AAAAC3Nza test@pc\n")
    code, out, _ = run("copy-id", "--public-key", public)
    assert code == 0 and "Installed" in out


def test_jump_host_from_the_command_line(run, ssh_server, monkeypatch):
    monkeypatch.setenv("TURBOSSH_JUMP_PASSWORD", "good")
    code, out, _ = run("run", "--jump-host", "127.0.0.1", "--jump-port",
                       ssh_server.port, "--jump-user", "u", "echo")
    assert (code, out) == (0, "ok\n")
    assert ssh_server.server.auth_log == ["good", "good"]     # jump, then target


def test_wrong_password_is_an_error_not_a_traceback(run, monkeypatch):
    monkeypatch.setenv("TURBOSSH_PASSWORD", "WRONG")
    code, _, err = run("run", "echo")
    assert code == 1 and err.startswith("ERROR:") and "Traceback" not in err


def test_password_from_stdin_and_missing_target(run, monkeypatch, ssh_server):
    monkeypatch.delenv("TURBOSSH_PASSWORD")
    monkeypatch.setattr("sys.stdin", io.StringIO("good\n"))
    assert run("run", "--password-stdin", "echo")[0] == 0
    code, _, err = run("run", "echo", target=False)
    assert code == 2 and "--host" in err


# ------------------------------------------------------------------ serial
def test_serial_commands(run):
    code, out, _ = run("list-serial", "--json", target=False)
    assert code == 0 and isinstance(json.loads(out), list)
    assert run("list-serial", target=False)[0] == 0
    code, _, err = run("serial-monitor", "--port", "NO-SUCH-PORT-99", target=False)
    assert code == 1 and "ERROR" in err and "Traceback" not in err

    assert run("scan-ports")[0] == 0
    code, out, _ = run("scan-ports", "--json")
    assert code == 0 and isinstance(json.loads(out), list)
    code, out, err = run("serial-ssh", "--device", "/dev/ttyUSB0", "--send", "hi",
                         "--match", "ok")
    assert code == 0 and "ok" in out and "1 matched" in err


# ------------------------------------------------------------------ camera
def test_camera_commands(run, ssh_server, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolve_ffmpeg", lambda explicit=None: None)
    code, _, err = run("camera-list", target=False)
    assert code == 2 and "ffmpeg not found" in err
    code, _, err = run("camera-grab", "--camera", "Cam", "--out", tmp_path / "x.jpg",
                       target=False)
    assert code == 2

    code, out, err = run("camera-list")                  # remote: none on this fake
    assert code == 0 and "No cameras" in err
    assert json.loads(run("camera-list", "--json")[1]) == []

    # remote grab: the fake host reports "ok" as its TEMP folder; put the file
    # the remote ffmpeg would have written there
    (ssh_server.root / "ok").mkdir()
    (ssh_server.root / "ok\\turbossh_grab.jpg").write_bytes(b"\xff\xd8jpeg\xff\xd9") \
        if os.name != "nt" else \
        (ssh_server.root / "ok" / "turbossh_grab.jpg").write_bytes(b"\xff\xd8jpeg\xff\xd9")
    out_file = tmp_path / "shot.jpg"
    code, out, err = run("camera-grab", "--camera", "Cam", "--out", out_file, "--force")
    assert code == 0, err
    assert out_file.read_bytes().startswith(b"\xff\xd8")


# ------------------------------------------------------------------ setup
def test_setup_and_install_commands_call_the_right_thing(run, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    code = run("setup-server", "--port", "2222", "--install-pip", target=False)[0]
    if os.name == "nt":
        assert code == 0 and calls[0][-3:] == ["-Port", "2222", "-InstallPip"]
    else:
        assert code == 2 and not calls           # Windows-only, says so

    from turbossh import winrm_bootstrap
    seen = {}

    def fake_install(host, user, password, openssh_dir, **kw):
        seen.update(host=host, user=user, password=password, **kw)
        return {"status": "Running"}
    monkeypatch.setattr(winrm_bootstrap, "enable_openssh_via_winrm_offline", fake_install)
    monkeypatch.setattr(cli, "prompt_password", lambda prompt: cli.Secret("adminpw"))
    code, out, _ = run("install-ssh-remote", "--host", "10.9.9.9", "--user", "adm",
                       "--domain", "CORP", "--ssh-port", "2200", "--winrm-port",
                       "5986", target=False)
    assert code == 0 and "Running" in out
    assert (seen["host"], seen["user"], seen["password"]) == ("10.9.9.9", "adm", "adminpw")
    assert (seen["domain"], seen["ssh_port"], seen["winrm_port"]) == ("CORP", 2200, 5986)


def test_diagnose(run, ssh_server):
    code, out, _ = run("diagnose", "--host", "127.0.0.1", "--port", ssh_server.port,
                       "--json", target=False)
    assert code == 0 and json.loads(out)["ssh_open"] is True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    assert run("diagnose", "--host", "127.0.0.1", "--port", closed, target=False)[0] == 1


# ------------------------------------------------------------------ keys / credentials
def test_store_credential_then_use_it(run, monkeypatch, _memory_keyring, ssh_server):
    monkeypatch.delenv("TURBOSSH_PASSWORD")
    monkeypatch.setattr(cli, "prompt_password", lambda prompt: cli.Secret("good"))
    code, out, _ = run("store-credential", "--user", "u", "--host", "127.0.0.1",
                       target=False)
    assert code == 0 and "u@127.0.0.1" in out
    assert _memory_keyring.store[("turbossh", "u@127.0.0.1")] == "good"
    assert run("run", "--use-stored", "echo")[0] == 0
    code, _, err = run("run", "--use-stored", "--service", "elsewhere", "echo")
    assert code == 2 and "No stored credential" in err


def test_keygen(run, tmp_path):
    key = tmp_path / "id_test"
    code, out, err = run("keygen", key, "--bits", "2048", "--comment", "ci",
                         target=False)
    assert code == 0 and out.startswith("ssh-rsa ") and out.strip().endswith(" ci")
    assert key.exists() and (tmp_path / "id_test.pub").read_text() == out
    code, _, err = run("keygen", key, target=False)
    assert code == 2 and "already exists" in err
    assert run("keygen", key, "--bits", "2048", "--force", target=False)[0] == 0


def test_sessions_and_session_flag(run, monkeypatch, ssh_server):
    from turbossh.gui.sessions import SessionStore
    monkeypatch.delenv("TURBOSSH_PASSWORD")
    store = SessionStore()
    store.save({"name": "cli-test-box", "type": "ssh", "host": "127.0.0.1",
                "port": ssh_server.port, "user": "u", "ignore_hostkey": True}, "good")
    store.save({"name": "cli-test-com", "type": "serial", "device": "COM7"})
    try:
        code, out, _ = run("sessions", "--json", target=False)
        rows = {r["name"]: r for r in json.loads(out)}
        assert rows["cli-test-box"]["host"] == "127.0.0.1"
        assert "cli-test-box" in run("sessions", target=False)[1]

        # host, user, port and the stored password all come from the session
        assert run("run", "-s", "cli-test-box", "echo", target=False) == (0, "ok\n", "")
        code, out, _ = run("info", "--session", "cli-test-box", "--json", target=False)
        assert json.loads(out)["host"] == "127.0.0.1"
        code, _, err = run("run", "-s", "no-such-session", "echo", target=False)
        assert code == 2 and "No saved session" in err
        code, _, err = run("run", "-s", "cli-test-com", "echo", target=False)
        assert code == 2 and "serial session" in err
    finally:
        store.delete("cli-test-box")
        store.delete("cli-test-com")


# ------------------------------------------------------------------ the app
def test_app_commands_dispatch(run, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "launch_gui", lambda argv=None: called.append("gui") or 0)
    monkeypatch.setattr(cli, "open_docs", lambda argv=None: called.append("docs") or 0)
    monkeypatch.setattr(cli, "create_shortcut",
                        lambda argv=None: called.append("shortcut") or 0)
    for name in ("gui", "docs", "shortcut"):
        assert run(name, target=False)[0] == 0
    assert called == ["gui", "docs", "shortcut"]
    assert run("version", target=False)[1].strip() == f"turbossh {__version__}"
    code, out, _ = run("--version", target=False)
    assert code == 0 and __version__ in out


def test_check_update(run, monkeypatch):
    from turbossh import update
    monkeypatch.setattr(update, "latest_version", lambda timeout=5.0: "99.0.0")
    code, out, _ = run("check-update", "--json", target=False)
    assert code == 0 and json.loads(out) == {
        "installed": __version__, "latest": "99.0.0", "update_available": True}
    assert "99.0.0 is available" in run("check-update", target=False)[1]
    monkeypatch.setattr(update, "latest_version", lambda timeout=5.0: __version__)
    assert "latest version" in run("check-update", target=False)[1]
    monkeypatch.setattr(update, "latest_version", lambda timeout=5.0: None)
    assert run("check-update", target=False)[0] == 1


def test_every_command_has_a_test():
    """A new command must come with a test here (and a section in CLI.md)."""
    source = open(__file__, encoding="utf-8").read()
    untested = [name for name in _subcommands() if f'"{name}"' not in source]
    assert not untested, untested
