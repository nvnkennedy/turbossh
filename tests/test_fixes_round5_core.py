"""Regression tests for what the review of the fourth pass found: a partial
temporary file left for good after a quick retry, a late probe channel never
closed, setuid bits on a half-written copy, and a split serial write failing
without saying what had gone out."""

from __future__ import annotations

import os
import stat
import threading
import time

import paramiko
import pytest

import sshserver
from turbossh import SSHConfig, SSHHandler, _transfer
from turbossh.exceptions import SSHCommandError, SSHTransferError
from turbossh.results import CommandResult


def _leftovers(root):
    return sorted(p for p in os.listdir(str(root)) if p.endswith(".tsp"))


def test_a_quick_retry_removes_the_partial_copy_the_drop_left(ssh, ssh_server, tmp_path,
                                                              monkeypatch):
    """The VPN drops during the 160 MB ffmpeg upload; nothing can remove its
    partial copy. The retry right after skipped it (too young to be taken for
    abandoned), succeeded under the next name — and that file is never pushed
    again, so the partial copy stayed for good."""
    src = tmp_path / "ffmpeg.exe.part"
    src.write_bytes(os.urandom(300_000))
    dropped = []
    original_remove = sshserver.StubSFTP.remove

    def remove(self, path):
        if dropped:                      # the clean-up after the drop never arrives
            dropped.pop()
            return paramiko.SFTP_FAILURE
        return original_remove(self, path)
    monkeypatch.setattr(sshserver.StubSFTP, "remove", remove)

    def drop(done, total):
        if done > 100_000:
            dropped.append(True)
            raise ConnectionResetError("link dropped")
    with pytest.raises(SSHTransferError):
        ssh.push(str(src), "/ffmpeg.exe.part", callback=drop)
    assert _leftovers(ssh_server.root) == [".ffmpeg.exe.part.0.tsp"]
    ssh.push(str(src), "/ffmpeg.exe.part")          # the retry, at once
    assert _leftovers(ssh_server.root) == []
    assert (ssh_server.root / "ffmpeg.exe.part").read_bytes() == src.read_bytes()


def test_the_probe_closes_a_channel_whose_answer_came_late(make_config, ssh_server,
                                                           monkeypatch):
    """Given up on after the caller's wait, a confirmation that arrived later
    was ignored by Paramiko and never closed: the server kept the session slot
    (OpenSSH allows 10) until the connection ended. The probe now waits for
    its channel and closes it, whenever it comes."""
    monkeypatch.setattr(SSHHandler, "_LIVENESS_TIMEOUT", 0.5)
    handler = SSHHandler(make_config(connect_timeout=0.5), quiet=True)
    handler.connect()
    try:
        transport = handler.transport
        srv = ssh_server.server
        original = srv.check_channel_request

        def slow(kind, chanid):
            time.sleep(1.5)                 # answered after the caller gave up
            return original(kind, chanid)
        monkeypatch.setattr(srv, "check_channel_request", slow)
        opened, closed = [], []
        real_open = transport.open_session

        def open_session(*a, **kw):
            chan = real_open(*a, **kw)
            opened.append(chan)
            real_close = chan.close
            chan.close = lambda: (closed.append(chan), real_close())[1]
            return chan
        monkeypatch.setattr(transport, "open_session", open_session)
        started = time.monotonic()
        handler._link_alive(transport, 0.5)
        assert time.monotonic() - started < 1.4         # the caller did not wait
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not closed:
            time.sleep(0.05)
        assert opened, "the late channel was given up on"
        assert closed == opened, "the probe's late channel was left open"
    finally:
        handler.disconnect()


def test_a_temporary_copy_gets_no_setuid_bit_before_its_data():
    """A root upload over a 4755 file: the half-written copy was setuid-root
    and executable by everyone for the whole upload."""
    early = _transfer._early_mode(0o4755)
    assert early & 0o7000 == 0
    assert early == 0o755
    assert _transfer._early_mode(0o600) == 0o600
    assert _transfer._early_mode(0o440) == 0o640      # its owner may still write


def test_a_set_uid_file_keeps_its_bits_after_the_upload(ssh, ssh_server, tmp_path):
    if os.name == "nt":
        pytest.skip("this file system keeps no setuid bit")
    ssh_server.server.apply_modes = True          # a server that really chmods
    target = ssh_server.root / "tool"
    target.write_bytes(b"old")
    os.chmod(str(target), 0o4755)
    src = tmp_path / "tool"
    src.write_bytes(b"new")
    ssh.push(str(src), "/tool")
    assert stat.S_IMODE(os.stat(str(target)).st_mode) == 0o4755


def test_the_setuid_bit_is_asked_for_only_after_the_data(ssh, ssh_server, tmp_path,
                                                        monkeypatch):
    """What the upload asks the server for, in order (runs on every system):
    the copy's mode without the setuid bit before the data, the exact mode
    after it — then the rename."""
    import turbossh._transfer as transfer
    target = ssh_server.root / "tool"
    target.write_bytes(b"old")
    old_mode = os.stat(str(target)).st_mode
    # Windows keeps no setuid bit: the old file's mode is said to be 4755, and
    # the new copy's own mode is not known (so the mode is always asked for)
    monkeypatch.setattr(transfer, "_perm", lambda mode: 0o4755 if mode == old_mode
                        else (stat.S_IMODE(mode) if mode else None))
    monkeypatch.setattr(transfer, "_temp_owner", lambda *a, **kw: None)
    asked = ssh_server.server.chmod_calls
    data_starts = []                       # how many chmods came before the data
    real_send = transfer._send

    def send(*a, **kw):
        data_starts.append(len(asked))
        return real_send(*a, **kw)
    monkeypatch.setattr(transfer, "_send", send)
    src = tmp_path / "tool"
    src.write_bytes(b"new")
    ssh.push(str(src), "/tool")
    assert len(data_starts) == 1
    before = [mode for _path, mode in asked[:data_starts[0]]]
    after = [mode for _path, mode in asked[data_starts[0]:]]
    assert before == [0o755], before       # the mode, without the setuid bit
    assert after == [0o4755], after        # the exact mode, once the data is in


def test_a_split_serial_write_that_fails_says_what_went_out(monkeypatch):
    """Longer than one command can carry, a write goes out in parts. One that
    failed after the first part raised the plain command error: the device had
    1,536 bytes and a partial line, and nothing said so."""
    handler = SSHHandler(SSHConfig(host="target"), quiet=True)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 2:
            raise SSHCommandError("powershell …", CommandResult(
                "powershell …", 3, "", "serial write failed: access denied", 0.0))
        return CommandResult(cmd, 0, "", "", 0.0)
    monkeypatch.setattr(handler, "_ensure_connected", lambda: None)
    monkeypatch.setattr(handler, "run", run)
    data = "x" * 5000
    with pytest.raises(SSHCommandError) as caught:
        handler.serial_write("COM5", data, mode="windows", safe=False)
    step = SSHHandler._SERIAL_WRITE_CHUNK
    assert caught.value.partial == step
    assert f"{step} of {len(data) + 1} bytes had been written to COM5" in str(caught.value)
    assert len(calls) == 2                    # nothing after the failed part


def test_a_probe_on_a_dead_link_ends_with_the_transport(make_config, ssh_server,
                                                        monkeypatch):
    """Waiting for its answer without a deadline of its own, the probe's helper
    must still end once the link is closed (no thread left per connect())."""
    monkeypatch.setattr(SSHHandler, "_LIVENESS_TIMEOUT", 0.3)
    handler = SSHHandler(make_config(connect_timeout=0.3), quiet=True)
    handler.connect()
    srv = ssh_server.server
    block = threading.Event()
    original = srv.check_channel_request
    monkeypatch.setattr(srv, "check_channel_request",
                        lambda kind, chanid: block.wait(30) and original(kind, chanid))

    def probes():
        return sum(t.name == "turbossh-liveness" for t in threading.enumerate())
    before = probes()
    handler._link_alive(handler.transport, 0.3)
    assert probes() == before + 1               # still waiting for its answer
    handler.disconnect()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and probes() > before:
        time.sleep(0.05)
    block.set()
    assert probes() == before, "the probe outlived the link"
