"""Regressions for the second 2.1.0 review: the first round of fixes made the
CLI, serial writes, FTPS, FTP downloads and the WinRM install safer but changed
what worked in 2.0.0. No network, device, admin rights or display: the "remote"
ends are the loopback SSH server or scripted fakes."""

from __future__ import annotations

import base64
import inspect
import io
import os
import shutil
import ssl
import subprocess
import sys
import time
import types

import pytest

from turbossh import cli


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


# ------------------------------------------------------------------ 1 / 2. serial writes
@pytest.fixture()
def serial_port(monkeypatch):
    """Install a fake pyserial port class; returns (serial module, handler module,
    the keyword arguments the last port was opened with)."""
    serial = pytest.importorskip("serial")
    from turbossh import serial_handler
    opened = {}

    class Port:
        is_open = True

        def __init__(self, port, baudrate, **kw):
            opened.clear()
            opened.update(kw, baudrate=baudrate)
            self.baudrate = int(baudrate)           # pyserial normalises it
            self.received = b""

        def write(self, data):
            self.received += data
            return len(data)

        def close(self):
            pass
    monkeypatch.setattr(serial_handler.serial, "Serial", Port)
    return serial, serial_handler, opened


def test_flow_controlled_ports_wait_for_a_busy_device_by_default(serial_port):
    """A device holding XOFF / CTS for longer than 5 s (a flash erase) made every
    write fail; 2.0.0 waited. With flow control there is no default timeout."""
    _serial, serial_handler, opened = serial_port
    for flow in ({"rtscts": True}, {"xonxoff": True}, {"dsrdtr": True}):
        with serial_handler.SerialHandler("COM9", quiet=True, **flow):
            assert opened["write_timeout"] is None, flow
            assert all(opened[k] for k in flow)
    with serial_handler.SerialHandler("COM9", quiet=True):
        assert opened["write_timeout"] == 5.0 and opened["dsrdtr"] is False
    with serial_handler.SerialHandler("COM9", quiet=True, rtscts=True, write_timeout=2.0):
        assert opened["write_timeout"] == 2.0                  # asked for: kept


class _WindowsPort:
    """A port that, like pyserial on Windows, raises SerialTimeoutException when a
    write() does not finish in time and leaves the count it did move in the
    write's OVERLAPPED structure. ``capacity(size)`` is how many bytes of a piece
    of that size get out before the timeout."""

    def __init__(self, serial, capacity):
        self.serial, self.capacity = serial, capacity
        self.is_open, self.baudrate = True, 1200
        self._overlapped_write = types.SimpleNamespace(InternalHigh=0)
        self.received, self.calls = b"", 0

    def write(self, data):
        self.calls += 1
        moved = min(len(data), self.capacity(len(data), len(self.received)))
        self.received += data[:moved]
        self._overlapped_write.InternalHigh = moved
        if moved < len(data):
            raise self.serial.SerialTimeoutException("Write timeout")
        return moved


def _handler(serial_handler, port, **kw):
    handler = serial_handler.SerialHandler("COM9", baudrate=1200, quiet=True,
                                           write_timeout=1.0, **kw)
    handler._ser = port
    return handler


def test_a_slow_but_moving_serial_write_is_not_given_up(serial_port):
    """XON/XOFF throttling the link to a fraction of the baud rate: every write
    ran into the timeout although bytes kept moving, and the rest of the payload
    was never sent."""
    serial, serial_handler, _ = serial_port
    port = _WindowsPort(serial, lambda size, done: 7)       # 7 bytes per timeout
    payload = bytes(range(256)) * 2
    assert _handler(serial_handler, port).write_bytes(payload) == len(payload)
    assert port.received == payload                         # all of it, in order, once


def test_a_stalled_write_counts_the_bytes_of_the_interrupted_piece(serial_port):
    serial, serial_handler, _ = serial_port
    port = _WindowsPort(serial, lambda size, done: max(0, 107 - done))
    with pytest.raises(serial_handler.SerialError) as caught:
        _handler(serial_handler, port).write_bytes(b"x" * 1000)
    assert "107 of 1000 bytes were sent" in str(caught.value)
    assert "no byte went out for 1 s" in str(caught.value)
    assert port.received == b"x" * 107


def test_a_baud_rate_given_as_text_still_writes(serial_port):
    """pyserial accepts baudrate="115200" (a value read from a config file);
    the write-piece arithmetic raised TypeError on it."""
    _serial, serial_handler, opened = serial_port
    with serial_handler.SerialHandler("COM9", baudrate="115200", quiet=True) as ser:
        assert opened["baudrate"] == "115200"
        assert ser.write("hello") == 5
        assert ser.write_line("x", safe=True).value == 2
    with serial_handler.SerialHandler("COM9", baudrate="9600", quiet=True,
                                      write_timeout=0.5) as ser:
        assert ser.write_bytes(b"y" * 5000) == 5000
        assert ser._ser.received == b"y" * 5000


# ------------------------------------------------------------------ 3. FTPS options
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


def test_ftps_options_can_be_set_on_the_config(monkeypatch, tmp_path):
    """verify_tls / ca_file only existed on FTPHandler: code that builds an
    FTPConfig (and passes it around) could not turn the check off or name a CA."""
    from turbossh import ftp
    from turbossh.config import FTPConfig
    made = []
    _fake_tls(monkeypatch, made)
    cfg = FTPConfig(host="ftp.lab", use_tls=True)
    cfg.verify_tls = False                  # an FTPConfig field (or an attribute)
    ftp.FTPHandler(cfg).connect()
    assert made[-1].verify_mode == ssl.CERT_NONE

    ftp.FTPHandler(cfg, verify_tls=True).connect()          # the handler's value wins
    assert made[-1].verify_mode == ssl.CERT_REQUIRED

    cfg = FTPConfig(host="10.0.0.7", use_tls=True)
    cfg.check_hostname = False
    ftp.FTPHandler(cfg).connect()
    assert made[-1].verify_mode == ssl.CERT_REQUIRED and not made[-1].check_hostname

    asked = []
    real = ssl.create_default_context
    monkeypatch.setattr(ftp.ssl, "create_default_context",
                        lambda *a, cafile=None, **kw: asked.append(cafile) or real())
    ca = tmp_path / "lab-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    cfg = FTPConfig(host="ftp.lab", use_tls=True)
    cfg.ca_file = str(ca)
    ftp.FTPHandler(cfg).connect()
    assert asked == [str(ca)]


def test_ftps_can_skip_only_the_host_name_check(monkeypatch):
    """A lab server reached by IP with a CA-signed certificate that lists only its
    name: the choice was all (verify_tls=False) or nothing."""
    from turbossh import ftp
    from turbossh.config import FTPConfig
    made = []
    _fake_tls(monkeypatch, made)
    ftp.FTPHandler(FTPConfig(host="10.0.0.7", use_tls=True),
                   check_hostname=False).connect()
    assert made[-1].verify_mode == ssl.CERT_REQUIRED and not made[-1].check_hostname


def test_a_missing_or_bad_ca_file_is_named(monkeypatch, tmp_path):
    from turbossh import ftp
    from turbossh.config import FTPConfig
    from turbossh.exceptions import FTPError
    _fake_tls(monkeypatch, [])
    cfg = FTPConfig(host="ftp.lab", use_tls=True)
    missing = tmp_path / "lab-ca.pem"
    with pytest.raises(FTPError) as caught:
        ftp.FTPHandler(cfg, ca_file=str(missing)).connect()
    assert str(missing) in str(caught.value) and "ca_file" in str(caught.value)

    bad = tmp_path / "not-a-cert.pem"
    bad.write_text("hello")
    with pytest.raises(FTPError) as caught:
        ftp.FTPHandler(cfg, ca_file=str(bad)).connect()
    assert str(bad) in str(caught.value) and "ca_file" in str(caught.value)


def test_ftps_certificate_hints_say_where_the_options_go(monkeypatch):
    from turbossh import ftp
    from turbossh.config import FTPConfig
    from turbossh.exceptions import FTPError
    untrusted = ssl.SSLCertVerificationError(
        1, "certificate verify failed: unable to get local issuer certificate")
    untrusted.verify_code = 20
    _fake_tls(monkeypatch, [], fail=untrusted)
    with pytest.raises(FTPError) as caught:
        ftp.FTPHandler(FTPConfig(host="ftp.lab", use_tls=True)).connect()
    text = str(caught.value)
    assert "FTPConfig(..., ca_file=" in text and "FTPHandler(config, ca_file=" in text
    assert "FTPHandler(config, verify_tls=False)" in text

    mismatch = ssl.SSLCertVerificationError(
        1, "certificate verify failed: IP address mismatch, certificate is not "
           "valid for '10.0.0.7'")
    mismatch.verify_code = 64
    _fake_tls(monkeypatch, [], fail=mismatch)
    with pytest.raises(FTPError) as caught:
        ftp.FTPHandler(FTPConfig(host="10.0.0.7", use_tls=True)).connect()
    assert "FTPConfig(..., check_hostname=False)" in str(caught.value)


# ------------------------------------------------------------------ 7. FTP download target
class _FakeFTP:
    def __init__(self, chunks):
        self.chunks = chunks

    def retrbinary(self, command, callback):
        for chunk in self.chunks:
            callback(chunk)


def _ftp_handler(chunks):
    from turbossh import ftp
    from turbossh.config import FTPConfig
    handler = ftp.FTPHandler(FTPConfig(host="x"))
    handler._ftp = _FakeFTP(chunks)
    return handler


def test_a_download_into_a_file_that_refuses_the_swap_is_written_in_place(
        tmp_path, monkeypatch):
    """A log viewer holding the target open (no delete sharing) refuses the
    rename: the finished download was deleted and "Access is denied" shown."""
    from turbossh import ftp
    target = tmp_path / "trace.log"
    target.write_bytes(b"old")

    def refused(src, dst, attempts=5):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(ftp, "_replace_with_retry", refused)
    res = _ftp_handler([b"new ", b"trace"]).pull("/trace.log", str(target))
    assert target.read_bytes() == b"new trace" and res.size_bytes == 9
    assert os.listdir(tmp_path) == ["trace.log"]


def test_a_download_that_cannot_be_put_in_place_is_kept(tmp_path, monkeypatch):
    from turbossh import ftp
    from turbossh.exceptions import FTPError
    target = tmp_path / "trace.log"
    target.write_bytes(b"old")

    def refused(*a, **kw):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(ftp, "_replace_with_retry", refused)
    monkeypatch.setattr(ftp.shutil, "copyfile", refused)
    with pytest.raises(FTPError) as caught:
        _ftp_handler([b"new trace"]).pull("/trace.log", str(target))
    kept = [n for n in os.listdir(tmp_path) if n != "trace.log"]
    assert len(kept) == 1 and kept[0] in str(caught.value)
    assert (tmp_path / kept[0]).read_bytes() == b"new trace"
    assert target.read_bytes() == b"old"


@pytest.mark.skipif(os.name != "nt", reason="sharing modes are a Windows thing")
def test_a_download_into_a_file_held_open_by_a_viewer_on_windows(tmp_path):
    target = tmp_path / "trace.log"
    target.write_bytes(b"old")
    with open(target, "rb"):             # Python opens without FILE_SHARE_DELETE
        _ftp_handler([b"new trace"]).pull("/trace.log", str(target))
    assert target.read_bytes() == b"new trace"
    assert os.listdir(tmp_path) == ["trace.log"]


def test_a_download_onto_a_symlink_writes_through_it(tmp_path):
    real = tmp_path / "data" / "real.bin"
    real.parent.mkdir()
    real.write_bytes(b"old")
    link = tmp_path / "link.bin"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    _ftp_handler([b"new"]).pull("/real.bin", str(link))
    assert os.path.islink(link)                  # still the link …
    assert real.read_bytes() == b"new"           # … and its target has the data
    assert sorted(os.listdir(real.parent)) == ["real.bin"]


# ------------------------------------------------------------------ 4 / 6. WinRM
class _Protocol:
    """pywinrm's Protocol, scripted: ``reply(script)`` is the script's stdout,
    or None while it is still running."""

    def __init__(self, reply):
        self.reply, self.scripts = reply, {}

    def open_shell(self):
        return "S"

    def run_command(self, shell, command):
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
        pass

    def close_shell(self, shell):
        pass


@pytest.fixture()
def winrm_fake(monkeypatch):
    pytest.importorskip("winrm")
    from turbossh import winrm_bootstrap

    def install(reply):
        proto = _Protocol(reply)

        class Session:
            def __init__(self, *a, **kw):
                self.protocol = proto
        monkeypatch.setattr(winrm_bootstrap, "winrm", types.SimpleNamespace(Session=Session))
        return proto
    return install


def test_the_online_install_gives_a_slow_update_source_time(winrm_fake):
    """Add-WindowsCapability from a slow WSUS took longer than ten minutes and
    succeeded in 2.0.0; the new deadline stopped it."""
    from turbossh import winrm_bootstrap as w
    default = inspect.signature(w.enable_openssh_via_winrm).parameters[
        "command_timeout"].default
    assert default >= 30 * 60
    assert w._STEP_TIMEOUT <= 120                # the probes keep short deadlines

    winrm_fake(lambda script: None)              # never finishes
    with pytest.raises(w.WinRMTimeoutError) as caught:
        w.enable_openssh_via_winrm("10.9.9.9", "adm", "pw", transport="basic",
                                   command_timeout=0.3)
    assert "command_timeout=" in str(caught.value)       # how to give it longer


def _remote(stage, install_output=b"STATUS:Running\r\n"):
    """The remote end of the offline install, answering in the OEM code page
    (cp850) as a German Windows does; base64 output is plain ASCII."""
    def reply(script):
        if "Expand-Archive" in script:
            return install_output
        if "OSArchitecture" in script:
            return b"X64\r\n"
        if "New-Item -ItemType Directory" in script:
            if "ToBase64String" in script:
                return base64.b64encode(stage.encode("utf-8")) + b"\r\n"
            return stage.encode("cp850") + b"\r\n"
        return b"OK\r\n"
    return reply


@pytest.fixture()
def bundle(tmp_path):
    (tmp_path / "OpenSSH-Win64.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    return str(tmp_path)


def test_the_offline_install_finds_its_zip_under_a_non_ascii_profile(
        winrm_fake, monkeypatch, bundle):
    """%TEMP% came back in the OEM code page and was read as UTF-8: "Müller"
    became "M\ufffdller", the ZIP went to (or was looked for in) the wrong folder
    and Expand-Archive failed after the whole upload."""
    from turbossh import winrm_bootstrap as w
    stage = "C:\\Users\\M\u00fcller\\AppData\\Local\\Temp\\turbossh_ossh"
    copied = []
    monkeypatch.setattr(w, "_smb_copy", lambda *a, **kw: copied.append(a[4]) or True)
    proto = winrm_fake(_remote(stage))
    res = w.enable_openssh_via_winrm_offline("h", "u", "pw", bundle, transport="basic")
    assert res["ok"]
    assert copied == [stage + "\\openssh.zip"]
    install = next(s for s in proto.scripts.values() if "Expand-Archive" in s)
    assert "$zip = Join-Path $env:TEMP 'turbossh_ossh\\openssh.zip'" in install
    assert "\ufffd" not in install


def test_the_offline_install_uploads_over_winrm_when_the_folder_is_unknown(
        winrm_fake, monkeypatch, bundle):
    from turbossh import winrm_bootstrap as w
    monkeypatch.setattr(w, "_smb_copy", lambda *a, **kw: pytest.fail("no SMB"))
    proto = winrm_fake(lambda s: (b"garbled \x81\r\n" if "ToBase64String" in s
                                  else _remote("")(s)))
    assert w.enable_openssh_via_winrm_offline("h", "u", "pw", bundle,
                                              transport="basic")["ok"]
    assert any("Add-Content" in s for s in proto.scripts.values())


def test_the_offline_install_timeout_says_how_to_wait_longer(winrm_fake, monkeypatch,
                                                              bundle):
    from turbossh import winrm_bootstrap as w
    monkeypatch.setattr(w, "_smb_copy", lambda *a, **kw: True)
    winrm_fake(_remote("C:\\T\\turbossh_ossh", install_output=None))
    with pytest.raises(w.WinRMTimeoutError) as caught:
        w.enable_openssh_via_winrm_offline("h", "u", "pw", bundle, transport="basic",
                                           command_timeout=0.3)
    assert "command_timeout=" in str(caught.value) and "--timeout" in str(caught.value)


@pytest.fixture()
def fake_install(monkeypatch):
    from turbossh import winrm_bootstrap
    seen = {}

    def install(host, user, password, openssh_dir, **kw):
        seen.update(host=host, user=user, password=password, **kw)
        return {"status": "Running"}
    monkeypatch.setattr(winrm_bootstrap, "enable_openssh_via_winrm_offline", install)
    return seen


def test_install_ssh_remote_timeout_option(run, fake_install):
    assert run("install-ssh-remote", "--host", "h", "--user", "u", target=False)[0] == 0
    assert "command_timeout" not in fake_install          # the library's default
    assert run("install-ssh-remote", "--host", "h", "--user", "u", "--timeout", "1800",
               target=False)[0] == 0
    assert fake_install["command_timeout"] == 1800
    code, _, err = run("install-ssh-remote", "--host", "h", "--user", "u",
                       "--timeout", "0", target=False)
    assert code == 2 and "--timeout" in err


# ------------------------------------------------------------------ 8. WinRM password
class _Terminal(io.StringIO):
    def isatty(self):
        return True


def test_install_ssh_remote_at_a_terminal_asks_rather_than_use_the_env(
        run, monkeypatch, fake_install):
    """TURBOSSH_PASSWORD is set for the SSH targets; sent silently to a domain
    account over WinRM, a wrong one counts toward locking it. 2.0.0 asked."""
    asked = []
    monkeypatch.setattr(cli, "prompt_password",
                        lambda prompt: asked.append(prompt) or cli.Secret("typed"))
    monkeypatch.setattr(sys, "stdin", _Terminal(""))
    code, _, err = run("install-ssh-remote", "--host", "h", "--user", "adm",
                       "--domain", "CORP", target=False)
    assert code == 0 and fake_install["password"] == "typed"
    assert asked == ["Password for CORP\\adm@h: "]
    assert "TURBOSSH_PASSWORD is set" in err

    # no terminal: the environment is what a script has, and it says so
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code, _, err = run("install-ssh-remote", "--host", "h", "--user", "adm",
                       target=False)
    assert code == 0 and fake_install["password"] == "good" and len(asked) == 1
    assert "comes from TURBOSSH_PASSWORD" in err

    # asked for on purpose: used, and named
    monkeypatch.setattr(sys, "stdin", _Terminal("piped\n"))
    code, _, err = run("install-ssh-remote", "--host", "h", "--user", "adm",
                       "--password-stdin", target=False)
    assert code == 0 and fake_install["password"] == "piped"
    assert "comes from standard input" in err


# ------------------------------------------------------------------ 9. jump password
def test_a_command_line_jump_host_uses_the_sessions_jump_password(monkeypatch):
    """`run -s X --jump-host bastion` for a session saved without a jump host
    but with a jump password: 2.0.0 sent it, the first fix round sent none.
    Without one of its own, the shared password in Settings goes only to the
    jump host and user in Settings, never to another bastion."""
    from turbossh.gui import settings
    from turbossh.gui.sessions import SessionStore
    monkeypatch.delenv("TURBOSSH_JUMP_PASSWORD", raising=False)
    monkeypatch.setattr(settings, "shared_jump", lambda: {
        "host": "bastion", "user": "", "domain": "", "password": "shared-secret"})
    store = SessionStore()
    store.save({"name": "cli-nojump-box", "type": "ssh", "host": "10.0.0.5",
                "user": "u"}, "pw")
    SessionStore._set_secret("cli-nojump-box::jump", "own-jump")   # as 2.0.0 kept it
    try:
        def jump_password(*extra):
            args = cli.build_parser().parse_args(
                ["run", "-s", "cli-nojump-box", *extra, "uptime"])
            cfg = cli._build_config(args)
            if cfg.jump_host is None:
                return None
            return cfg.jump_host.password and cfg.jump_host.password.reveal()
        assert jump_password("--jump-host", "bastion") == "own-jump"
        assert jump_password() is None                       # no jump host at all
        SessionStore._set_secret("cli-nojump-box::jump", None)
        assert jump_password("--jump-host", "bastion") == "shared-secret"
        assert jump_password("--jump-host", "BASTION") == "shared-secret"
        assert not jump_password("--jump-host", "other-bastion")
        assert not jump_password("--jump-host", "bastion", "--jump-user", "x")
        monkeypatch.setenv("TURBOSSH_JUMP_PASSWORD", "from-env")
        assert jump_password("--jump-host", "bastion") == "from-env"
    finally:
        store.delete("cli-nojump-box")


# ------------------------------------------------------------------ 10. camera-grab lines
def _script_shell(monkeypatch, ssh_server, pshome):
    """Make the loopback host answer the default-shell probe with *pshome*."""
    original = ssh_server.server._run

    def scripted(channel, cmd):
        if cmd != "echo $PSHOME":
            return original(channel, cmd)
        time.sleep(0.2)
        channel.sendall(pshome.encode() + b"\r\n")
        channel.send_exit_status(0)
        channel.close()
    monkeypatch.setattr(ssh_server.server, "_run", scripted)


@pytest.mark.parametrize("pshome", [
    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0",     # PowerShell default shell
    "$PSHOME",                                           # cmd echoes it back
])
def test_camera_grab_lines_run_under_either_default_shell(run, ssh_server, monkeypatch,
                                                          tmp_path, pshome):
    """The grab built raw cmd.exe lines: a PowerShell default shell could not
    parse them, and cmd /c stripped the first and last quote of the ffmpeg line."""
    _script_shell(monkeypatch, ssh_server, pshome)
    (ssh_server.root / "ok").mkdir()
    (ssh_server.root / "ok" / "turbossh_grab.jpg").write_bytes(b"\xff\xd8jpeg\xff\xd9")
    out_file = tmp_path / "shot.jpg"
    code, _, err = run("camera-grab", "--camera", 'Cam "A"|%TEMP%', "--out", out_file)
    assert code == 0, err
    assert out_file.read_bytes().startswith(b"\xff\xd8")
    execs = ssh_server.server.execs
    grab = next(c for c in execs if "-f dshow" in c)
    probe = next(c for c in execs if "$env:TEMP" in c)
    remove = next(c for c in execs if "del " in c)
    if "WindowsPowerShell" in pshome:
        for line in (grab, probe, remove):
            assert line.startswith('cmd /s /c --% "'), line
    else:
        assert probe == 'powershell -NoProfile -Command "$env:TEMP"'
    assert 'ver >nul & "ffmpeg" -y' in grab        # cmd keeps the exe's quotes
    assert 'video="Cam ATEMP"' in grab            # no " % | from the name


# ------------------------------------------------------------------ 5. elevate.ps1
def _windows_powershell():
    return shutil.which("powershell") if os.name == "nt" else None


def _single_quoted_strings(path):
    """The single-quoted string values in a .ps1, as Windows PowerShell 5.1 reads
    the file (it takes one without a BOM to be in the ANSI code page)."""
    command = ("$e=$null; $a=[System.Management.Automation.Language.Parser]::"
               "ParseFile($env:TURBOSSH_TEST_PS1,[ref]$null,[ref]$e); $a.FindAll("
               "{param($n) $n -is "
               "[System.Management.Automation.Language.StringConstantExpressionAst]"
               " -and $n.StringConstantType -eq 'SingleQuoted'}, $true) | ForEach-"
               "Object {[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("
               "$_.Value))}")
    proc = subprocess.run([_windows_powershell(), "-NoProfile", "-NonInteractive",
                           "-Command", command], capture_output=True, text=True,
                          errors="replace", timeout=120,
                          env=dict(os.environ, TURBOSSH_TEST_PS1=path))
    return [base64.b64decode(line).decode("utf-8")
            for line in proc.stdout.split()]


@pytest.mark.skipif(os.name != "nt", reason="the SSH-server setup is Windows-only")
def test_the_elevation_launcher_survives_a_non_ascii_temp_folder(tmp_path, monkeypatch):
    """Windows PowerShell 5.1 read elevate.ps1 (UTF-8 without a BOM) as ANSI: a
    %TEMP% under C:\\Users\\Müller garbled the script path and the elevated setup
    never ran (the GUI polled for three minutes, then "no result yet")."""
    import tempfile
    temp = tmp_path / "M\u00fcller\u2019s temp"
    temp.mkdir()
    started = []
    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "gettempdir", lambda: str(temp))
        patch.setattr(shutil, "copytree", lambda *a, **kw: None)       # the ZIPs
        patch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
        assert cli.launch_setup_server(port=2222) is True
    launcher = started[0][-1]
    assert launcher.endswith("elevate.ps1") and launcher.startswith(str(temp))
    raw = open(launcher, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")
    script = os.path.join(os.path.dirname(launcher), "setup_openssh_server.ps1")
    assert script in raw.decode("utf-8-sig").replace("\u2019\u2019", "\u2019")
    if _windows_powershell():
        argument_list = _single_quoted_strings(launcher)[-1]
        assert f'-File "{script}" -Port 2222' in argument_list
