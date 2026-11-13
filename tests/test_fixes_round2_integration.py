"""Regression tests for what joins the round-2 fixes across modules: the CLI's
serial-ssh no longer releases the port's tracked holder (it may be the GUI's
live session), FTPConfig takes the FTPS options, SSHConfig sets the WinRM
install deadline, and the file browser says when an upload went in place."""

from __future__ import annotations

import ssl
import types

import pytest

from turbossh import cli


# ------------------------------------------------------------------ serial-ssh
class _SerialHandler:
    def __init__(self):
        self.streams, self.released = [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def serial_write(self, *a, **kw):
        pass

    def serial_stream(self, device, **kw):
        self.streams.append((device, kw))
        return {"matches": []}

    def serial_release(self, device, **kw):
        self.released.append(device)


@pytest.mark.parametrize("extra, force", [([], False), (["--force"], True)])
def test_serial_ssh_leaves_other_holders_of_the_port_alone(monkeypatch, extra, force):
    """The command's own reader is stopped by serial_stream itself; the
    serial_release it used to call afterwards stopped the port's TRACKED holder,
    which can be the GUI's live serial tab on that port."""
    handler = _SerialHandler()
    monkeypatch.setattr(cli, "_connect", lambda args: handler)
    code = cli.main(["serial-ssh", "--host", "rdp-box", "--user", "me",
                     "--device", "COM4", *extra])
    assert code in (0, None)
    assert handler.released == []
    assert handler.streams and handler.streams[0][0] == "COM4"
    assert handler.streams[0][1].get("force") is force


def test_serial_ssh_force_is_documented():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "CLI.md").read_text(encoding="utf-8")
    assert "| `--force` | first stop a stale TurboSSH reader" in text


# ------------------------------------------------------------------ FTPConfig
def test_ftpconfig_takes_the_ftps_options(monkeypatch):
    """FTPConfig(verify_tls=False) was a TypeError: only FTPHandler had them."""
    from turbossh import ftp
    from turbossh.config import FTPConfig
    cfg = FTPConfig(host="10.0.0.7", use_tls=True, verify_tls=True,
                    ca_file=None, check_hostname=False)
    assert (cfg.verify_tls, cfg.ca_file, cfg.check_hostname) == (True, None, False)
    made = []

    class FakeTLS:
        def __init__(self, *a, context=None, **kw):
            self.context = context
            made.append(context)

        def connect(self, *a, **kw):
            pass

        def login(self, *a, **kw):
            pass

        def prot_p(self):
            pass

        def set_pasv(self, *a):
            pass

        def quit(self):
            pass
    monkeypatch.setattr(ftp, "FTP_TLS", FakeTLS)
    ftp.FTPHandler(cfg).connect()
    assert made[-1].verify_mode == ssl.CERT_REQUIRED and not made[-1].check_hostname
    ftp.FTPHandler(FTPConfig(host="ftp.lab", use_tls=True, verify_tls=False)).connect()
    assert made[-1].verify_mode == ssl.CERT_NONE
    defaults = FTPConfig(host="ftp.lab")
    assert (defaults.verify_tls, defaults.ca_file, defaults.check_hostname) == (
        True, None, True)


# ------------------------------------------------------------------ WinRM deadline
def test_the_winrm_install_deadline_comes_from_sshconfig(monkeypatch):
    """connect()'s auto-bootstrap and bootstrap_sshd_via_winrm run the remote
    install with the bootstrap's default deadline unless SSHConfig says one."""
    from turbossh import winrm_bootstrap
    from turbossh.config import SSHConfig
    from turbossh.core import SSHHandler
    seen = []
    monkeypatch.setattr(winrm_bootstrap, "enable_openssh_via_winrm",
                        lambda *a, **kw: seen.append(kw) or {"ok": True})
    handler = SSHHandler(SSHConfig(host="10.0.0.9", username="admin", password="pw"))
    monkeypatch.setattr(handler, "_port_open", lambda *a, **kw: True)
    handler.bootstrap_sshd_via_winrm()
    assert "command_timeout" not in seen[-1]          # the bootstrap's own default
    handler = SSHHandler(SSHConfig(host="10.0.0.9", username="admin", password="pw",
                                   winrm_command_timeout=300))
    monkeypatch.setattr(handler, "_port_open", lambda *a, **kw: True)
    handler.bootstrap_sshd_via_winrm()
    assert seen[-1]["command_timeout"] == 300.0


# ------------------------------------------------------------------ in-place note
def test_an_upload_written_in_place_is_logged(qapp, monkeypatch, tmp_path):
    """put_atomic says when it could not replace the file (hard links, a device,
    a folder that is not writable, no room): the browser logs it."""
    from turbossh.gui import sftp_browser as sb
    local = tmp_path / "config.ini"
    local.write_bytes(b"x=1\n")
    monkeypatch.setattr(sb, "put_atomic",
                        lambda *a, **kw: "in place (it has 2 hard links)")
    thread = sb._TransferThread(None, types.SimpleNamespace(log_id=None))
    assert thread._put_file(None, str(local), "/etc/app/config.ini") == 4
    assert thread.in_place == ["/etc/app/config.ini was written in place "
                               "(it has 2 hard links)"]
    monkeypatch.setattr(sb, "put_atomic", lambda *a, **kw: "")
    thread._put_file(None, str(local), "/etc/app/other.ini")
    assert len(thread.in_place) == 1
