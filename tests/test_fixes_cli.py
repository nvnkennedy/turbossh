"""Regressions for the 2.1.0 review fixes in the CLI, FTP, serial, tunnel, WinRM,
JSON-store and setup-script code. No network, device, admin rights or display:
the "remote" ends are the loopback SSH server or scripted fakes."""

from __future__ import annotations

import base64
import ftplib
import io
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import types

import pytest

from turbossh import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP_PS1 = os.path.join(ROOT, "turbossh", "setup_openssh_server.ps1")


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
        except SystemExit as stop:
            code = stop.code
        out = capsys.readouterr()
        return code, out.out, out.err
    return _run


# ------------------------------------------------------------------ 1. output encoding
def test_run_output_survives_a_narrow_pipe_encoding(ssh_server, monkeypatch):
    """`turbossh run ... > out.txt` on Windows writes cp1252: a "●" used to end
    in "ERROR: UnicodeEncodeError" with the output lost."""
    original = ssh_server.server._run

    def scripted(channel, cmd):
        if cmd != "systemctl status sshd":
            return original(channel, cmd)
        time.sleep(0.2)
        channel.sendall("● sshd.service – OpenSSH server\n\ufffd\n".encode("utf-8"))
        channel.send_exit_status(0)
        channel.close()
    monkeypatch.setattr(ssh_server.server, "_run", scripted)

    env = dict(os.environ, PYTHONIOENCODING="cp1252", TURBOSSH_PASSWORD="good",
               PYTHONPATH=ROOT)
    env.pop("PYTHONUTF8", None)
    proc = subprocess.run(
        [sys.executable, "-m", "turbossh", "run", "--host", "127.0.0.1", "--port",
         str(ssh_server.port), "--user", "u", "--host-key-policy", "ignore",
         "systemctl status sshd"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=120)
    err = proc.stderr.decode("cp1252", "replace")
    assert proc.returncode == 0, err
    assert "Traceback" not in err and "UnicodeEncodeError" not in err
    out = proc.stdout.replace(b"\r\n", b"\n")
    assert out == "? sshd.service – OpenSSH server\n?\n".encode("cp1252")


def test_tolerant_output_keeps_the_encoding_and_a_chosen_error_handler(monkeypatch):
    strict = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    chosen = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                              errors="backslashreplace")
    monkeypatch.setattr(sys, "stdout", strict)
    monkeypatch.setattr(sys, "stderr", chosen)
    cli._tolerant_output()
    assert (strict.encoding, strict.errors) == ("cp1252", "replace")
    assert chosen.errors == "backslashreplace"
    print("serial \ufffd line")                     # a monitor's on_line=print
    strict.flush()
    assert strict.buffer.getvalue().replace(b"\r\n", b"\n") == b"serial ? line\n"


# ------------------------------------------------------------------ 6. clean errors
def test_library_and_tool_errors_end_in_an_error_line(run, monkeypatch):
    paramiko = pytest.importorskip("paramiko")

    # camera-list with a local ffmpeg that hangs past its timeout
    monkeypatch.setattr(cli, "_resolve_ffmpeg", lambda explicit=None: "ffmpeg")

    def hung(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(cli.subprocess, "run", hung)
    code, _, err = run("camera-list", target=False)
    assert code == 1 and err.startswith("ERROR: TimeoutExpired") and "Traceback" not in err

    # a raw paramiko error escaping the connection
    def banner(args):
        raise paramiko.SSHException("Error reading SSH protocol banner")
    monkeypatch.setattr(cli, "_connect", banner)
    code, _, err = run("run", "echo")
    assert (code, err.strip()) == (
        1, "ERROR: SSHException: Error reading SSH protocol banner")

    # standard input closed under a prompt
    def closed(prompt):
        raise EOFError
    monkeypatch.setattr(cli, "prompt_password", closed)
    code, _, err = run("store-credential", "--user", "u", target=False)
    assert code == 1 and "EOFError: standard input ended" in err

    # a real bug still shows its traceback
    def bug(args):
        raise KeyError("bug")
    monkeypatch.setattr(cli, "_connect", bug)
    with pytest.raises(KeyError):
        run("run", "echo")


# ------------------------------------------------------------------ 7. install-ssh-remote
@pytest.fixture()
def fake_install(monkeypatch):
    from turbossh import winrm_bootstrap
    seen = {}

    def install(host, user, password, openssh_dir, **kw):
        seen.update(host=host, user=user, password=password, **kw)
        return {"status": "Running"}
    monkeypatch.setattr(winrm_bootstrap, "enable_openssh_via_winrm_offline", install)

    def no_prompt(prompt):
        raise AssertionError("must not prompt")
    monkeypatch.setattr(cli, "prompt_password", no_prompt)
    return seen


def test_install_ssh_remote_takes_the_password_like_every_command(
        run, monkeypatch, fake_install, _memory_keyring):
    monkeypatch.delenv("TURBOSSH_PASSWORD")
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdinpw\n"))
    code, out, _ = run("install-ssh-remote", "--host", "10.9.9.9", "--user", "adm",
                       "--password-stdin", target=False)
    assert code == 0 and fake_install["password"] == "stdinpw"

    if _memory_keyring is not None:
        _memory_keyring.store[("turbossh", "CORP\\adm@10.9.9.9")] = "vaultpw"
        code, _, _ = run("install-ssh-remote", "--host", "10.9.9.9", "--user", "adm",
                         "--domain", "CORP", "--use-stored", target=False)
        assert code == 0 and fake_install["password"] == "vaultpw"


def test_install_ssh_remote_without_a_password_or_terminal_stops(
        run, monkeypatch, fake_install):
    """A scripted run used to wait for a keypress on the console for ever."""
    monkeypatch.delenv("TURBOSSH_PASSWORD")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))          # not a terminal
    code, _, err = run("install-ssh-remote", "--host", "10.9.9.9", "--user", "adm",
                       target=False)
    assert code == 2 and "--password-stdin" in err and not fake_install


# ------------------------------------------------------------------ 11. a leading "--"
def test_a_leading_double_dash_is_not_sent(run, ssh_server):
    assert run("run", "--", "echo") == (0, "ok\n", "")
    code, out, _ = run("stream", "--", "echo")
    assert (code, out) == (0, "ok\n")
    assert [c for c in ssh_server.server.execs if "--" in c] == []
    assert ssh_server.server.execs.count("echo") == 2


# ------------------------------------------------------------------ 2 / 9. WinRM
class _FakeProtocol:
    """pywinrm's Protocol, scripted: ``reply(script)`` is the script's stdout,
    or None while it is still running (then a Receive times out, as WinRM's
    does after its operation timeout)."""

    def __init__(self, reply):
        self.reply, self.calls, self.scripts = reply, [], {}

    def open_shell(self):
        self.calls.append("open")
        return "S"

    def run_command(self, shell, command):
        self.calls.append("run")
        cid = f"C{len(self.scripts)}"
        self.scripts[cid] = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        return cid

    def get_command_output_raw(self, shell, cid):
        from winrm.exceptions import WinRMOperationTimeoutError
        out = self.reply(self.scripts[cid])
        if out is None:
            time.sleep(0.02)
            raise WinRMOperationTimeoutError()
        return out, b"", 0, True

    def cleanup_command(self, shell, cid):
        self.calls.append("cleanup")

    def close_shell(self, shell):
        self.calls.append("close")


@pytest.fixture()
def winrm_fake(monkeypatch):
    pytest.importorskip("winrm")
    from turbossh import winrm_bootstrap

    def install(reply):
        proto = _FakeProtocol(reply)

        class Session:
            def __init__(self, *a, **kw):
                self.protocol = proto
        monkeypatch.setattr(winrm_bootstrap, "winrm", types.SimpleNamespace(Session=Session))
        return proto
    return install


def test_a_remote_command_that_never_ends_is_stopped(winrm_fake):
    """pywinrm retries its operation timeout for ever: Add-WindowsCapability with
    Windows Update blocked used to hang the caller (and core's connect lock)."""
    from turbossh import winrm_bootstrap as w
    proto = winrm_fake(lambda script: None)
    start = time.monotonic()
    with pytest.raises(w.WinRMTimeoutError) as caught:
        w.enable_openssh_via_winrm("10.9.9.9", "adm", "pw", transport="basic",
                                   command_timeout=0.3)
    assert time.monotonic() - start < 10
    assert "did not finish within 0.3 s" in str(caught.value)
    assert "offline install" in str(caught.value)
    assert proto.calls == ["open", "run", "cleanup", "close"]     # stopped, shell closed


_STAGE_DIR = "C:\\Users\\O'Brien\\AppData\\Local\\Temp\\turbossh_ossh"


def _offline_reply(install_output, stage=_STAGE_DIR):
    """The remote end of the offline install. Console output comes back in the
    remote OEM code page (cp850 here), as WinRM returns it; base64 is ASCII."""
    def reply(script):
        if "Expand-Archive" in script:               # the install script
            return install_output
        if "OSArchitecture" in script:
            return b"X64\r\n"
        if "New-Item -ItemType Directory" in script:
            if "ToBase64String" in script:
                return base64.b64encode(stage.encode("utf-8")) + b"\r\n"
            return stage.encode("cp850") + b"\r\n"
        return b"OK\r\n"
    return reply


def test_offline_install_keeps_the_remote_temp_path_out_of_the_script(
        winrm_fake, monkeypatch, tmp_path):
    """An account like O'Brien broke the install script after the whole upload:
    the ZIP's path is now built on the remote side, so it never is in the script."""
    from turbossh import winrm_bootstrap as w
    (tmp_path / "OpenSSH-Win64.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    copied = []
    monkeypatch.setattr(w, "_smb_copy", lambda *a, **kw: copied.append(a[4]) or True)
    proto = winrm_fake(_offline_reply(b"STATUS:Running\r\n"))
    res = w.enable_openssh_via_winrm_offline("h", "u", "pw", str(tmp_path),
                                             transport="basic")
    assert res["ok"] and res["status"] == "Running"
    assert copied == [_STAGE_DIR + "\\openssh.zip"]
    install = next(s for s in proto.scripts.values() if "Expand-Archive" in s)
    assert "$zip = Join-Path $env:TEMP 'turbossh_ossh\\openssh.zip'" in install
    assert "O'Brien" not in install
    if _powershell():
        script = tmp_path / "install.ps1"
        script.write_text(install, encoding="utf-8")
        assert _ps_parse_errors(str(script)) == 0


def test_offline_install_and_upload_steps_have_deadlines(winrm_fake, monkeypatch, tmp_path):
    from turbossh import winrm_bootstrap as w
    (tmp_path / "OpenSSH-Win64.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    monkeypatch.setattr(w, "_smb_copy", lambda *a, **kw: True)
    winrm_fake(_offline_reply(None))                    # the install never ends
    with pytest.raises(w.WinRMTimeoutError, match="Installing OpenSSH Server on h"):
        w.enable_openssh_via_winrm_offline("h", "u", "pw", str(tmp_path),
                                           transport="basic", command_timeout=0.3)

    # the chunked upload (no SMB): one chunk that hangs stops the upload
    monkeypatch.setattr(w, "_smb_copy", lambda *a, **kw: False)
    base = _offline_reply(b"STATUS:Running\r\n")
    proto = winrm_fake(lambda s: None if "Add-Content" in s else base(s))
    with pytest.raises(w.WinRMTimeoutError, match="Uploading chunk 1/"):
        w.enable_openssh_via_winrm_offline("h", "u", "pw", str(tmp_path),
                                           transport="basic", command_timeout=0.3)
    assert proto.calls[-2:] == ["cleanup", "close"]


# ------------------------------------------------------------------ 3. serial writes
def test_a_stalled_serial_write_times_out_and_says_how_much_went(monkeypatch):
    serial = pytest.importorskip("serial")
    from turbossh import serial_handler
    opened = {}

    class Port:
        is_open = True

        def __init__(self, port, baudrate, **kw):
            opened.update(kw)
            self.taken = 0

        def write(self, data):
            if self.taken >= 100:                 # XOFF: the port stops taking data
                raise serial.SerialTimeoutException("Write timeout")
            self.taken += len(data)
            return len(data)

        def close(self):
            pass
    monkeypatch.setattr(serial_handler.serial, "Serial", Port)

    with serial_handler.SerialHandler("COM9", quiet=True) as default:
        assert opened["write_timeout"] == 5.0
        assert default.write("hi") == 2
    handler = serial_handler.SerialHandler("COM9", baudrate=1200, quiet=True,
                                           write_timeout=1.0)
    handler.open()
    assert opened["write_timeout"] == 1.0
    # 1200 baud, 1 s: pieces of 15 bytes (an eighth of the timeout); seven go
    # out, then the port stalls. This port does not say how much of the eighth
    # piece went (only Windows does), so the error says it may have.
    with pytest.raises(serial_handler.SerialError,
                       match=r"timed out after 1 s: 105 of 1000 bytes were sent, "
                             r"and perhaps part of the next 15"):
        handler.write_bytes(b"x" * 1000)
    res = handler.write_line("x", safe=True)
    assert not res.success and isinstance(res.error, serial_handler.SerialError)


# ------------------------------------------------------------------ 4. FTPS verification
def _fake_tls(monkeypatch, made, fail=None):
    from turbossh import ftp

    class FakeTLS:
        def __init__(self, context=None):
            made.append(context)

        def connect(self, *a, **kw):
            if fail:
                raise fail

        def login(self, *a):
            pass

        def prot_p(self):
            pass

        def set_pasv(self, value):
            pass

        def quit(self):
            pass
    monkeypatch.setattr(ftp, "FTP_TLS", FakeTLS)


def test_ftps_verifies_the_server_certificate_by_default(monkeypatch):
    from turbossh import ftp
    from turbossh.config import FTPConfig
    made = []
    _fake_tls(monkeypatch, made)
    cfg = FTPConfig(host="ftp.lab", use_tls=True)
    ftp.FTPHandler(cfg).connect()
    assert made[-1].verify_mode == ssl.CERT_REQUIRED and made[-1].check_hostname

    ftp.FTPHandler(cfg, verify_tls=False).connect()
    assert made[-1].verify_mode == ssl.CERT_NONE and not made[-1].check_hostname

    asked = []
    real = ssl.create_default_context
    monkeypatch.setattr(ftp.ssl, "create_default_context",
                        lambda *a, cafile=None, **kw: asked.append(cafile) or real())
    ftp.FTPHandler(cfg, ca_file="lab-ca.pem").connect()
    assert asked == ["lab-ca.pem"]


def test_ftps_certificate_failure_says_what_to_do(monkeypatch):
    from turbossh import ftp
    from turbossh.config import FTPConfig
    from turbossh.exceptions import FTPError
    _fake_tls(monkeypatch, [], fail=ssl.SSLCertVerificationError(
        1, "certificate verify failed: self-signed certificate"))
    with pytest.raises(FTPError, match="ca_file=.*verify_tls=False"):
        ftp.FTPHandler(FTPConfig(host="ftp.lab", use_tls=True)).connect()


# ------------------------------------------------------------------ 5. FTP downloads
class _FakeFTP:
    def __init__(self, chunks, fail=None):
        self.chunks, self.fail = chunks, fail

    def retrbinary(self, command, callback):
        for chunk in self.chunks:
            callback(chunk)
        if self.fail is not None:
            raise self.fail


def test_an_interrupted_ftp_download_keeps_the_existing_file(tmp_path):
    from turbossh import ftp
    from turbossh.config import FTPConfig
    from turbossh.exceptions import FTPError
    target = tmp_path / "flash.bin"
    target.write_bytes(b"good copy")
    handler = ftp.FTPHandler(FTPConfig(host="x"))

    handler._ftp = _FakeFTP([b"new-par"], fail=ftplib.error_temp("426 transfer aborted"))
    with pytest.raises(FTPError, match="426"):
        handler.pull("/flash.bin", str(target))
    assert target.read_bytes() == b"good copy"
    assert os.listdir(tmp_path) == ["flash.bin"]             # no .part left behind

    handler._ftp = _FakeFTP([b"new-par"], fail=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        handler.pull("/flash.bin", str(target))
    assert target.read_bytes() == b"good copy" and os.listdir(tmp_path) == ["flash.bin"]

    seen = []
    handler._ftp = _FakeFTP([b"new ", b"image"])
    res = handler.pull("/flash.bin", str(target), callback=seen.append)
    assert target.read_bytes() == b"new image" and res.size_bytes == 9 and seen == [4, 5]
    assert os.listdir(tmp_path) == ["flash.bin"]

    fresh = tmp_path / "sub" / "fresh.bin"
    handler._ftp = _FakeFTP([b"x"], fail=EOFError())
    with pytest.raises(FTPError):
        handler.pull("/fresh.bin", str(fresh))
    assert os.listdir(tmp_path / "sub") == []


# ------------------------------------------------------------------ 12. tunnels
def _echo_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)

    def relay(conn):
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(data)

    def accept():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=relay, args=(conn,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return listener


def _recv_exactly(sock, n):
    data = b""
    while len(data) < n:
        piece = sock.recv(n - len(data))
        if not piece:
            break
        data += piece
    return data


def test_closing_a_local_forward_ends_its_open_connections(ssh):
    echo = _echo_server()
    try:
        fwd = ssh.forward_local("127.0.0.1", echo.getsockname()[1])
        with socket.create_connection(("127.0.0.1", fwd.local_port), timeout=10) as c:
            c.sendall(b"ping")
            assert _recv_exactly(c, 4) == b"ping"
            fwd.close()
            # (the loopback server drops a connection idle for 5 s by itself,
            # so the check is that it no longer relays, well inside that)
            c.settimeout(3)
            try:
                c.sendall(b"pong")
                rest = c.recv(4096)                 # used to come back: b"pong"
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                rest = b""
            assert rest == b""
    finally:
        echo.close()


def test_a_refused_forward_is_logged_and_opening_has_a_timeout(caplog):
    paramiko = pytest.importorskip("paramiko")
    from turbossh.tunnel import LocalForward
    asked = {}

    class Transport:
        def open_channel(self, kind, dest, src, timeout=None):
            asked["timeout"] = timeout
            raise paramiko.ChannelException(2, "Connect failed")

    with caplog.at_level(logging.WARNING, logger="turbossh"):
        with LocalForward(Transport(), "10.1.2.3", 80) as fwd:
            with socket.create_connection(("127.0.0.1", fwd.local_port), timeout=5) as c:
                c.settimeout(5)
                try:
                    assert c.recv(1) == b""                  # dropped, as before
                except (ConnectionResetError, ConnectionAbortedError):
                    pass
    assert asked["timeout"] is not None and 0 < asked["timeout"] <= 60
    assert "10.1.2.3:80" in caplog.text and "Connect failed" in caplog.text


# ------------------------------------------------------------------ 13. JSON store
def test_a_briefly_locked_settings_file_is_read_after_a_retry(tmp_path, monkeypatch):
    from turbossh import _jsonstore
    target = tmp_path / "settings.json"
    target.write_text('{"theme": "dark"}', encoding="utf-8")
    real_open, denied = open, []

    def flaky_open(path, *a, **kw):
        if len(denied) < 2:
            denied.append(path)
            raise PermissionError(13, "The process cannot access the file")
        return real_open(path, *a, **kw)
    monkeypatch.setattr(_jsonstore, "open", flaky_open, raising=False)
    assert _jsonstore.read_json(str(target), dict) == ({"theme": "dark"}, None)
    assert len(denied) == 2


def test_a_locked_settings_file_is_not_treated_as_corrupt(tmp_path, monkeypatch):
    """A sharing violation used to set the file aside as corrupt: settings fell
    back to defaults and the next save replaced the user's file."""
    from turbossh import _jsonstore
    target = tmp_path / "settings.json"
    target.write_text('{"theme": "dark"}', encoding="utf-8")

    def locked(path, *a, **kw):
        raise PermissionError(13, "The process cannot access the file")
    monkeypatch.setattr(_jsonstore, "open", locked, raising=False)
    value, problem = _jsonstore.read_json(str(target), dict)
    assert value is None and problem.transient and problem.backup is None
    assert "left as it is" in problem
    assert os.listdir(tmp_path) == ["settings.json"]          # nothing set aside

    monkeypatch.undo()
    target.write_text('{"theme": ', encoding="utf-8")            # really corrupt
    value, problem = _jsonstore.read_json(str(target), dict)
    assert value is None and not problem.transient and problem.backup
    assert os.path.exists(problem.backup) and "could not be read" in problem


# ------------------------------------------------------------------ 8 / 10. setup script
def _powershell():
    return shutil.which("powershell") or shutil.which("pwsh")


def _ps_parse_errors(path: str) -> int:
    command = ("$e=$null; [void][System.Management.Automation.Language.Parser]::"
               "ParseFile('%s',[ref]$null,[ref]$e); $e.Count" % path.replace("'", "''"))
    proc = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command",
                           command], capture_output=True, text=True, timeout=120)
    return int(proc.stdout.strip().splitlines()[-1])


def test_setup_script_keeps_path_expandable_and_guards_the_result_file():
    text = open(SETUP_PS1, encoding="utf-8").read()
    # PATH: read unexpanded and written back as REG_EXPAND_SZ, never through
    # [Environment]::SetEnvironmentVariable (which froze every %VAR% entry)
    assert "SetEnvironmentVariable('Path'" not in text
    assert "DoNotExpandEnvironmentNames" in text and "RegistryValueKind]::ExpandString" in text
    # the elevated result write: no links, admin-only folder, a fresh file, no BOM
    writer = text.split("function Write-SetupResult")[1]
    assert "Test-AdminOnlyFolder" in writer and "FileMode]::CreateNew" in writer
    assert "UTF8Encoding($false)" in writer
    assert "ReparsePoint" in text.split("function Test-AdminOnlyFolder")[1]
    assert "Set-Content -Path (Join-Path $resultDir" not in text
    # the GUI still polls the same file
    assert cli.sshd_result_file().endswith(os.path.join("turbossh", "sshd-setup-result.txt"))


@pytest.mark.skipif(not _powershell(), reason="needs PowerShell to parse the script")
def test_setup_script_parses():
    assert _ps_parse_errors(SETUP_PS1) == 0


# ------------------------------------------------------------------ follow-ups
def test_a_second_ssh_server_setup_keeps_a_stage_still_in_use(tmp_path):
    """Each launch swept every earlier stage, including one an elevated setup
    started a minute ago was still running from."""
    old = tmp_path / "turbossh-sshd-setup-old"
    recent = tmp_path / "turbossh-sshd-setup-recent"
    other = tmp_path / "unrelated"
    for folder in (old, recent, other):
        folder.mkdir()
    past = time.time() - cli._STAGE_MAX_AGE - 60
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    cli._sweep_setup_stages(str(tmp_path))
    assert not old.exists()
    assert recent.exists() and other.exists()


def test_a_saved_jump_session_falls_back_to_the_shared_jump_password(monkeypatch):
    """Sessions through the jump host in Settings keep no copy of its password,
    so the CLI must fall back to the shared one for them; a session through any
    other jump host is never sent it."""
    from turbossh.gui import settings
    from turbossh.gui.sessions import SessionStore
    monkeypatch.delenv("TURBOSSH_JUMP_PASSWORD", raising=False)
    monkeypatch.setattr(settings, "shared_jump", lambda: {
        "host": "10.0.0.20", "user": "j", "domain": "", "password": "shared-secret"})
    store = SessionStore()
    store.save({"name": "cli-jump-box", "type": "ssh", "host": "10.0.0.5", "user": "u",
                "use_jump": True, "jhost": "10.0.0.20", "juser": "j"}, "pw")
    store.save({"name": "cli-other-jump", "type": "ssh", "host": "10.0.0.5", "user": "u",
                "use_jump": True, "jhost": "10.0.0.30", "juser": "j"}, "pw")
    try:
        args = cli.build_parser().parse_args(["run", "-s", "cli-jump-box", "uptime"])
        cfg = cli._build_config(args)
        assert cfg.jump_host is not None
        assert cfg.jump_host.password.reveal() == "shared-secret"
        args = cli.build_parser().parse_args(["run", "-s", "cli-other-jump", "uptime"])
        assert cli._build_config(args).jump_host.password is None
    finally:
        store.delete("cli-jump-box")
        store.delete("cli-other-jump")


def test_powershell_quoting_doubles_every_quote_powershell_honours():
    """PowerShell ends a single-quoted string at U+2018..U+201B as well as at ',
    so doubling only ' let a curly apostrophe (pasted from Word) break out."""
    from turbossh import update
    for quote in (cli._ps_quote, update._ps_quote):
        for ch in "'\u2018\u2019\u201a\u201b":
            assert quote(f"a{ch}b") == f"'a{ch}{ch}b'"
        assert quote("plain") == "'plain'"


def test_winrm_timeout_error_is_exported():
    import turbossh
    from turbossh.winrm_bootstrap import WinRMError, WinRMTimeoutError
    assert turbossh.WinRMTimeoutError is WinRMTimeoutError
    assert issubclass(WinRMTimeoutError, WinRMError)
