"""Regression tests for the 2.1.0 core fixes: serial over SSH, transfers,
known_hosts, the connection lifecycle, SFTP timeouts, the remote camera and
recursive pulls. Remote Windows behaviour is checked through the PowerShell /
cmd scripts the handler sends (decoded from the loopback server's log)."""

from __future__ import annotations

import base64
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time

import paramiko
import pytest

from turbossh import (SSHCommandError, SSHConfig, SSHHandler, SSHNotConnectedError,
                      SSHTimeoutError, SSHTransferError)
from turbossh import core
from turbossh.results import OperationResult

QUOTES = "'" + "".join(chr(c) for c in (0x2018, 0x2019, 0x201A, 0x201B))
LSQ, RSQ = chr(0x2018), chr(0x2019)


def decode_ps(cmd: str) -> str:
    """The PowerShell source of the first -EncodedCommand in a command line."""
    b64 = cmd.split("-EncodedCommand", 1)[1].split()[0]
    return base64.b64decode(b64).decode("utf-16-le")


def encoded_arg(cmd: str) -> str:
    return cmd.split("-EncodedCommand", 1)[1].split()[0]


@pytest.fixture()
def script(ssh_server):
    """Script the loopback server: ``script(fn)``, where ``fn(cmd)`` returns
    ``(stdout, stderr, status)``, a callable taking the channel, or None for the
    default reply ("ok", exit 0)."""
    def install(fn):
        def run(channel, cmd):
            time.sleep(0.05)
            try:
                out = fn(cmd)
                if out is None:
                    out = (b"ok\n", b"", 0)
                if callable(out):
                    out(channel)
                    return
                stdout, stderr, status = out
                if stderr:
                    channel.sendall_stderr(stderr)
                if stdout:
                    channel.sendall(stdout)
                channel.send_exit_status(status)
                channel.close()
            except Exception:
                pass
        ssh_server.server._run = run
    return install


# ------------------------------------------------------------- serial: POSIX
class _CaptureChan:
    def __init__(self, sink):
        self.sink = sink

    def get_pty(self, **kw):
        pass

    def exec_command(self, cmd):
        self.sink.append(cmd)

    def close(self):
        pass


def _offline_handler(monkeypatch):
    """A handler whose channels only record the command they were given."""
    handler = SSHHandler(SSHConfig(host="target"), quiet=True)
    sent = []

    class Transport:
        def open_session(self, timeout=None):
            return _CaptureChan(sent)
    monkeypatch.setattr(handler, "_ensure_connected", lambda: None)
    monkeypatch.setattr(SSHHandler, "transport", property(lambda self: Transport()))
    return handler, sent


def test_posix_bridge_puts_the_pty_in_raw_mode_and_records_its_pids(monkeypatch):
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_bridge("/dev/ttyUSB0", baudrate=9600)
    cmd = sent[-1]
    # the PTY (stdin) goes raw before the cats start: no double echo, no line
    # buffering, and Ctrl-C is a byte for the device instead of a SIGINT
    assert "stty raw -echo -isig" in cmd
    assert cmd.index("stty raw -echo -isig") < cmd.index("cat /dev/ttyUSB0 &")
    # ...keeping 2.0's newline translations (LF -> CRLF out, Enter CR -> LF in)
    assert (cmd.index("stty raw -echo -isig") < cmd.index("stty icrnl opost onlcr")
            < cmd.index("cat /dev/ttyUSB0 &"))
    # QNX's stty has no -F: the device is configured through stdin instead
    assert "stty -F /dev/ttyUSB0 9600 raw -echo 2>/dev/null || " \
           "stty 9600 raw -echo < /dev/ttyUSB0" in cmd
    # the PID file lives in a folder of the user's own, not at a fixed /tmp name
    assert '__d="${TMPDIR:-/tmp}/turbossh-$(id -u)"' in cmd
    assert '__pf="$__d/turbossh_ser_devttyUSB0.pid"' in cmd
    assert "/tmp/turbossh_ser_" not in cmd
    assert "echo \"$__r $__w\" > \"$__pf\"" in cmd
    assert "fuser" not in cmd                       # only a forced open uses it
    handler.serial_bridge("/dev/ttyUSB0", force=True)
    assert "fuser -k /dev/ttyUSB0" in sent[-1]


def test_posix_release_stops_only_the_tracked_bridge(ssh, ssh_server):
    ssh.serial_release("/dev/ttyUSB0")
    cmd = ssh_server.server.execs[-1]
    assert "fuser" not in cmd, "every process holding the device was killed"
    assert '__pf="$__d/turbossh_ser_devttyUSB0.pid"' in cmd
    ssh.serial_release("/dev/ttyUSB0", force=True)
    assert "fuser -k /dev/ttyUSB0" in ssh_server.server.execs[-1]


@pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("mkfifo"),
                    reason="runs the POSIX bridge script for real (Linux)")
def test_posix_bridge_and_release_end_to_end(monkeypatch, tmp_path):
    """The bridge script with a FIFO as a loopback 'device' and a stty that only
    records its arguments: bytes (Ctrl-C included) pass through, its two cats
    are recorded, and release stops exactly them — not a stranger whose PID is
    in the file, and not another process that holds the device."""
    handler, sent = _offline_handler(monkeypatch)
    monkeypatch.setattr(SSHHandler, "_POSIX_PID_DIR", str(tmp_path))
    dev = str(tmp_path / "ttyFAKE0")
    os.mkfifo(dev)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "stty").write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path}/stty.log"\n')
    (bindir / "stty").chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    handler.serial_bridge(dev)
    bridge = subprocess.Popen(["sh", "-c", sent[-1]], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, env=env)
    pidfile = tmp_path / ("turbossh_ser_" + core._port_key(dev) + ".pid")
    try:
        deadline = time.time() + 10
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.1)
        bridge.stdin.write(b"hi\x03\n")
        bridge.stdin.flush()
        time.sleep(1)                                    # through the 'device'
        pids = pidfile.read_text().split()
        assert len(pids) == 2 and all(p.isdigit() for p in pids)
        assert "raw -echo -isig" in (tmp_path / "stty.log").read_text()

        other = subprocess.Popen(["sleep", "30"])        # PID listed, not a cat
        handler.run = lambda cmd, **kw: subprocess.run(["sh", "-c", cmd], env=env)
        handler.serial_release(dev)
        out, _ = bridge.communicate(timeout=10)
        assert out == b"hi\x03\n"
        assert not pidfile.exists()

        pidfile.write_text(f"{other.pid}\n")
        handler.serial_release(dev)
        time.sleep(0.5)
        assert other.poll() is None, "a process that is not our bridge was killed"
        other.kill()
    finally:
        if bridge.poll() is None:
            bridge.kill()


# ----------------------------------------------------------- serial: Windows
def test_windows_stream_records_its_pid_only_once_open_and_stops_its_reader(ssh,
                                                                           ssh_server):
    res = ssh.serial_stream("COM5", timeout=10, force=True)
    assert res["lines"] == 1
    stream_cmd, stop_cmd = ssh_server.server.execs[-2:]
    src = decode_ps(stream_cmd)
    lines = src.splitlines(True)
    assert lines[2] == core._ps_tag_line("port=COM5") and lines[3].startswith("#run=")
    # the tracked stale holder first, then Open(), and only then the PID file
    assert (src.index("Stop-Process -Id $op") < src.index("$p.Open()")
            < src.index("Set-Content -LiteralPath $pf -Value $PID"))
    failed = src[src.index("if(-not $ok)"):]
    failed = failed[:failed.index("exit 2")]
    assert "Remove-Item" not in failed, "a failed open deleted the real holder's record"
    assert core._PS_DROP_OWN_PIDFILE in src          # removed only while it is ours
    # the stream's end stops exactly this run's reader
    run_tag = core._ps_b64("".join(lines[:4]))
    assert encoded_arg(stream_cmd).startswith(run_tag)
    assert run_tag in decode_ps(stop_cmd)


def test_force_and_the_tracked_holder_are_scoped_to_one_port(ssh, ssh_server):
    ssh.serial_bridge("COM5", force=True).close()
    cmd = ssh_server.server.execs[-1]
    src = decode_ps(cmd)
    port_tag = core._ps_b64(core._serial_ps_header("COM5"))
    # a forced open looks for the holders of COM5 by the tag its own command
    # line starts with — no longer every TurboSSH serial reader on the machine
    assert encoded_arg(cmd).startswith(port_tag)
    assert f"$hit=$_.CommandLine -like '*{port_tag}*'" in src
    # neither of two ports' tags is a prefix of the other's
    for a, b in (("COM5", "COM50"), ("COM9", "COM98"), ("COM1", "COM10")):
        ta = core._ps_b64(core._serial_ps_header(a))
        tb = core._ps_b64(core._serial_ps_header(b))
        assert not ta.startswith(tb) and not tb.startswith(ta)


@pytest.mark.skipif(os.name != "nt", reason="runs the Windows scripts for real")
def test_force_and_release_stop_only_this_ports_readers_for_real(monkeypatch, tmp_path):
    """Sleeping PowerShells stand in for serial readers (an unusual port name, so
    nothing else on the machine can match): a forced release of COM9387 stops
    its 2.1 reader and a 2.0 one (no port line in its header), and leaves
    COM93870's, COM938's and an untagged PowerShell alone. Then the tracked-PID
    release: a PID file naming a PowerShell that is not ours is not obeyed."""
    handler = SSHHandler(SSHConfig(host="x"), quiet=True)
    sent = []
    handler.run = lambda cmd, **kw: sent.append(cmd)

    def run_sent():
        return subprocess.run(["powershell", "-NoProfile", "-EncodedCommand",
                               encoded_arg(sent[-1])], capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120)

    def sleeper(src):
        return subprocess.Popen(["powershell", "-NoProfile", "-EncodedCommand",
                                 core._ps_b64(src + "Start-Sleep 60")],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    procs = {
        "old_mine": sleeper(core._SERIAL_PS_TAG + "$dev='COM9387';"),
        "old_other": sleeper(core._SERIAL_PS_TAG + "$dev='COM93870';"),
        "new_other": sleeper(core._serial_ps_header("COM938")),
        "new_mine": sleeper(core._serial_ps_header("COM9387", "a" * 12)),
        "untagged": sleeper("$dev='COM9387';"),
    }
    try:
        time.sleep(4)
        handler.serial_release("COM9387", force=True)
        assert run_sent().returncode == 0
        time.sleep(1)
        alive = {k for k, p in procs.items() if p.poll() is None}
        assert alive == {"old_other", "new_other", "untagged"}

        monkeypatch.setenv("TEMP", str(tmp_path))       # the PID file lives there
        (tmp_path / "turbossh_ser_COM9387.pid").write_text(str(procs["untagged"].pid))
        handler.serial_release("COM9387")
        run_sent()
        time.sleep(1)
        assert procs["untagged"].poll() is None, "a PowerShell that is not ours died"
        assert not (tmp_path / "turbossh_ser_COM9387.pid").exists()
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()


@pytest.mark.parametrize("reply, busy", [
    (b"BUSY\n", True),
    (b"FREE\n", False),
    (b"UNAVAILABLE: The port 'COM9' does not exist.\n", False),
    (b"UNAVAILABLE: BUSY wording in a message\n", False),
])
def test_serial_in_use_is_busy_only_when_access_is_denied(ssh, ssh_server, script,
                                                         reply, busy):
    script(lambda cmd: (reply, b"", 0))
    assert ssh.serial_in_use("COM9") is busy
    src = decode_ps(ssh_server.server.execs[-1])
    assert "[System.UnauthorizedAccessException]" in src


def test_serial_write_failure_raises_with_the_reason(ssh, ssh_server, script):
    script(lambda cmd: (b"", b"serial write failed: Access to the port is denied.\n", 3))
    with pytest.raises(SSHCommandError) as caught:
        ssh.serial_write("COM9", "reboot")
    assert "Access to the port is denied" in caught.value.result.stderr
    res = ssh.serial_write("COM9", "reboot", safe=True)
    assert isinstance(res, OperationResult) and not res.success
    src = decode_ps(ssh_server.server.execs[-1])
    assert "exit 3" in src and "[Console]::Error.WriteLine" in src
    # the payload travels base64-encoded, as UTF-8 bytes
    sent = base64.b64encode("reboot\n".encode("utf-8")).decode("ascii")
    assert sent in src


def test_serial_write_to_a_missing_posix_device_raises(ssh, script):
    script(lambda cmd: (b"", b"serial write failed: not a serial device\n", 2))
    with pytest.raises(SSHCommandError):
        ssh.serial_write("/dev/ttyUSB7", "hi")


def _ps_single_quoted(src):
    """PowerShell's tokenizer for a '...' literal starting at src[0]: ANY of the
    five single-quote characters ends it; two in a row stand for the second.
    Returns (value, rest of the source)."""
    assert src[0] in QUOTES
    i, out = 1, []
    while i < len(src):
        c = src[i]
        if c in QUOTES:
            if i + 1 < len(src) and src[i + 1] in QUOTES:
                out.append(src[i + 1])
                i += 2
                continue
            return "".join(out), src[i + 1:]
        out.append(c)
        i += 1
    raise ValueError("unterminated string")


# (the injection attempt is harmless if it ever got through: it just exits 9)
_NASTY = ["O'Brien", f"it{RSQ}s", f"{LSQ}quoted{RSQ}", "a" + chr(0x201A) + "b",
          chr(0x201B) * 3, f"x{RSQ};exit 9;{LSQ}", "C:\\Users\\O'Neil\\COM5"]


@pytest.mark.parametrize("value", _NASTY)
def test_ps_quoting_escapes_every_single_quote_character(value):
    literal, rest = _ps_single_quoted("'" + SSHHandler._ps_sq(value) + "';NEXT")
    assert literal == value and rest == ";NEXT"


def test_the_old_quoting_let_a_curly_apostrophe_break_out():
    old = f"x{RSQ};exit 9;{LSQ}".replace("'", "''")
    _, rest = _ps_single_quoted("'" + old + "';NEXT")
    assert rest != ";NEXT"                        # i.e. injected commands ran


@pytest.mark.skipif(not (shutil.which("powershell") or shutil.which("pwsh")),
                    reason="needs PowerShell")
def test_ps_quoting_round_trips_through_real_powershell():
    exe = shutil.which("powershell") or shutil.which("pwsh")
    body = ";".join(f"[Console]::Out.Write('{SSHHandler._ps_sq(v)}'+[char]10)"
                    for v in _NASTY)
    ps = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + body
    out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                          core._ps_b64(ps)], capture_output=True, timeout=60,
                         stdin=subprocess.DEVNULL)
    assert out.returncode == 0
    assert out.stdout.decode("utf-8").split("\n")[:len(_NASTY)] == _NASTY


# ------------------------------------------------------------- transfers
def _leftovers(folder):
    return [p for p in os.listdir(folder)
            if p.endswith((core._transfer.TEMP_SUFFIX, core._transfer.ASIDE_SUFFIX))]


def test_an_aborted_pull_keeps_the_existing_local_file(ssh, ssh_server, tmp_path):
    (ssh_server.root / "big.bin").write_bytes(os.urandom(600_000))
    target = tmp_path / "big.bin"
    target.write_bytes(b"GOOD COPY")

    def cancel(done, total):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ssh.pull("/big.bin", str(target), callback=cancel)
    assert target.read_bytes() == b"GOOD COPY"
    assert _leftovers(tmp_path) == []
    ssh.pull("/big.bin", str(target))
    assert target.read_bytes() == (ssh_server.root / "big.bin").read_bytes()
    assert _leftovers(tmp_path) == []


def test_an_aborted_push_keeps_the_existing_remote_file(ssh, ssh_server, tmp_path):
    (ssh_server.root / "fw.bin").write_bytes(b"GOOD COPY")
    src = tmp_path / "fw.bin"
    src.write_bytes(os.urandom(600_000))

    class Cancelled(Exception):
        pass

    def cancel(done, total):
        if done > 100_000:
            raise Cancelled()

    with pytest.raises(Cancelled):
        ssh.push(str(src), "/fw.bin", callback=cancel)
    assert (ssh_server.root / "fw.bin").read_bytes() == b"GOOD COPY"
    assert _leftovers(ssh_server.root) == []


@pytest.mark.parametrize("posix_rename", [True, False])
def test_push_replaces_an_existing_file(ssh, ssh_server, tmp_path, posix_rename):
    ssh_server.server.posix_rename_supported = posix_rename
    (ssh_server.root / "dir").mkdir()
    (ssh_server.root / "dir" / "a.txt").write_bytes(b"old")
    src = tmp_path / "a.txt"
    src.write_bytes(b"new contents")
    res = ssh.push(str(src), "/dir/a.txt")
    assert res.size_bytes == len(b"new contents")
    assert (ssh_server.root / "dir" / "a.txt").read_bytes() == b"new contents"
    assert _leftovers(ssh_server.root / "dir") == []


def test_a_recursive_push_over_existing_files_replaces_them(ssh, ssh_server, tmp_path):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "b.txt").write_text("fresh")
    (ssh_server.root / "tree" / "sub").mkdir(parents=True)
    (ssh_server.root / "tree" / "sub" / "b.txt").write_text("stale")
    ssh.push(str(tree), "/tree", recursive=True)
    assert (ssh_server.root / "tree" / "sub" / "b.txt").read_text() == "fresh"
    assert _leftovers(ssh_server.root / "tree" / "sub") == []


def test_a_pull_over_a_file_that_cannot_be_renamed_over_is_written_in_place(
        ssh, ssh_server, tmp_path, monkeypatch):
    """Windows refuses os.replace while another program holds the target open
    without delete sharing (a log viewer)."""
    (ssh_server.root / "log.txt").write_text("remote")
    target = tmp_path / "log.txt"
    target.write_text("local")

    def refuse(src, dst):
        raise PermissionError(13, "in use")
    monkeypatch.setattr(core.os, "replace", refuse)
    ssh.pull("/log.txt", str(target))
    assert target.read_text() == "remote"
    assert _leftovers(tmp_path) == []


# ------------------------------------------------------------- known_hosts
def _known_hosts_lines(port):
    from sshserver import HOST_KEY
    return (f"[127.0.0.1]:{port} {HOST_KEY.get_name()} {HOST_KEY.get_base64()}",)


_UNPARSEABLE = [
    "# a comment",
    "@cert-authority *.example.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ==",
    "@revoked * ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==",
    "broken.example.com ssh-rsa !!!not-base64",
]


def test_known_hosts_with_lines_paramiko_cannot_parse_still_connects(ssh_server,
                                                                    make_config,
                                                                    tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("\n".join(_UNPARSEABLE) + "\n")
    handler = SSHHandler(make_config(host_key_policy="auto", known_hosts=str(kh)),
                         quiet=True)
    handler.connect()
    try:
        assert handler.run("echo").ok
    finally:
        handler.disconnect()
    lines = kh.read_text().splitlines()
    assert lines[:len(_UNPARSEABLE)] == _UNPARSEABLE, "the file was rewritten"
    assert lines[len(_UNPARSEABLE):] == list(_known_hosts_lines(ssh_server.port))


def test_a_known_key_is_accepted_under_reject_despite_marker_lines(ssh_server,
                                                                  make_config, tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("\n".join(_UNPARSEABLE + list(_known_hosts_lines(ssh_server.port))))
    handler = SSHHandler(make_config(host_key_policy="reject", known_hosts=str(kh)),
                         quiet=True)
    handler.connect()
    handler.disconnect()


def test_parallel_connects_add_the_new_key_once_and_keep_the_file(ssh_server,
                                                                 make_config, tmp_path):
    from turbossh import SSHPool
    kh = tmp_path / "known_hosts"
    kh.write_text("\n".join(_UNPARSEABLE) + "\n")
    pool = SSHPool([make_config(host_key_policy="auto", known_hosts=str(kh),
                                username=f"u{i}") for i in range(6)], max_workers=6)
    try:
        results = pool.connect()
        assert all(r.success for r in results.values()), results
    finally:
        pool.close()
    lines = kh.read_text().splitlines()
    assert lines == _UNPARSEABLE + list(_known_hosts_lines(ssh_server.port))


# ------------------------------------------------------------- lifecycle
def test_connect_on_a_connected_handler_opens_no_second_connection(ssh, ssh_server):
    before, client = len(ssh_server.transports), ssh.client
    ssh.connect()
    with ssh:
        assert ssh.client is client
    time.sleep(0.3)
    assert len(ssh_server.transports) == before


def test_a_reconnect_closes_the_dead_connection(make_config):
    handler = SSHHandler(make_config(), quiet=True)
    handler.connect()
    try:
        old = handler.client
        closed = []
        original = old.close
        old.close = lambda: (closed.append(1), original())
        old.get_transport().close()
        assert handler.run("echo").ok
        assert handler.client is not old and closed
    finally:
        handler.disconnect()


def test_a_sticky_disconnect_holds_until_the_next_connect(make_config, ssh_server):
    handler = SSHHandler(make_config(auto_reconnect=True), quiet=True)
    handler.connect()
    handler.disconnect(sticky=True)
    before = len(ssh_server.transports)
    with pytest.raises(SSHNotConnectedError):
        handler.run("echo")
    res = handler.run("echo", safe=True)
    assert not res.success and isinstance(res.error, SSHNotConnectedError)
    assert len(ssh_server.transports) == before, "a torn-down handler reconnected"
    handler.connect()
    try:
        assert handler.run("echo").ok
    finally:
        handler.disconnect()


def test_client_never_hands_out_none(monkeypatch):
    handler = SSHHandler(SSHConfig(host="x"), quiet=True)
    monkeypatch.setattr(handler, "_ensure_connected", lambda: None)
    with pytest.raises(SSHNotConnectedError):
        handler.client
    with pytest.raises(SSHNotConnectedError):
        handler.transport
    assert handler.is_connected is False


# ------------------------------------------------------------- timeouts
def test_a_channel_request_that_is_never_answered_times_out():
    class Silent:
        """exec_command waits for the reply like Paramiko's: until the channel
        is closed under it."""
        def __init__(self):
            self.gone = threading.Event()

        def exec_command(self, cmd):
            self.gone.wait(30)
            raise paramiko.SSHException("Channel closed.")

        def close(self):
            self.gone.set()

    chan = Silent()
    t0 = time.monotonic()
    with pytest.raises(SSHTimeoutError):
        SSHHandler._request(chan, 0.5, chan.exec_command, "x")
    assert time.monotonic() - t0 < 5


def test_the_sftp_channel_has_a_timeout_and_downloads_bound_prefetch(
        ssh, ssh_server, tmp_path, monkeypatch):
    assert ssh.sftp().get_channel().gettimeout() == ssh.config.sftp_timeout
    own = ssh.open_sftp()
    try:
        assert own is not ssh.sftp()
        assert own.get_channel().gettimeout() == ssh.config.sftp_timeout
    finally:
        own.close()
    seen = []
    original = paramiko.SFTPClient.get

    def spy(self, remote, local, **kw):
        seen.append(kw)
        return original(self, remote, local, **kw)
    monkeypatch.setattr(paramiko.SFTPClient, "get", spy)
    (ssh_server.root / "f.bin").write_bytes(b"x" * 1000)
    ssh.pull("/f.bin", str(tmp_path / "f.bin"))
    if core._GET_TAKES_PREFETCH_LIMIT:
        assert seen[-1].get("max_concurrent_prefetch_requests") == core._SFTP_PREFETCH


def test_sftp_opens_outside_the_connection_lock(ssh, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    original = ssh._new_sftp

    def slow(client):
        entered.set()
        gate.wait(10)
        return original(client)
    monkeypatch.setattr(ssh, "_new_sftp", slow)
    got = []
    worker = threading.Thread(target=lambda: got.append(ssh.sftp()))
    worker.start()
    try:
        assert entered.wait(10)
        assert ssh._conn_lock.acquire(timeout=2), "sftp() held the lock while opening"
        ssh._conn_lock.release()
    finally:
        gate.set()
        worker.join(10)
    assert got and got[0] is ssh.sftp()


def test_a_target_without_an_sftp_server_raises_transfer_errors(ssh, ssh_server,
                                                               tmp_path):
    ssh_server.server.check_channel_subsystem_request = lambda channel, name: False
    src = tmp_path / "a.txt"
    src.write_text("a")
    with pytest.raises(SSHTransferError):
        ssh.listdir_attr("/")
    with pytest.raises(SSHTransferError):
        ssh.push(str(src), "/a.txt")
    with pytest.raises(SSHTransferError):
        ssh.pull("/a.txt", str(tmp_path / "b.txt"))


# ------------------------------------------------------------- remote modes
class _Attr:
    def __init__(self, name, mode):
        self.filename, self.st_mode, self.st_size = name, mode, 1


QNX_BITS = 1 << 16           # QNX servers send st_mode values above 0xFFFF


def test_entry_kind_and_is_dir_take_qnx_and_missing_modes():
    class Sftp:
        def stat(self, path):
            return _Attr(path, {"/d": QNX_BITS | stat.S_IFDIR | 0o755,
                                "/f": QNX_BITS | stat.S_IFREG | 0o644,
                                "/n": None}[path])
    h, sftp = SSHHandler(SSHConfig(host="x"), quiet=True), Sftp()
    assert h._remote_is_dir(sftp, "/d") is True
    assert h._remote_is_dir(sftp, "/f") is False
    assert h._remote_is_dir(sftp, "/n") is False
    assert h._entry_kind(sftp, _Attr("d", QNX_BITS | stat.S_IFDIR), "/d") == ("dir", False)
    assert h._entry_kind(sftp, _Attr("n", None), "/n") == ("file", False)
    link = _Attr("l", QNX_BITS | stat.S_IFLNK | 0o777)
    assert h._entry_kind(sftp, link, "/d") == ("dir", True)


# ------------------------------------------------------------- recursive pull
@pytest.mark.parametrize("name, safe", [
    ("a:b.txt", "a_b.txt"), ("what?.log", "what_.log"), ('q"x<y>|z*', "q_x_y__z_"),
    ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("Com1.tar.gz", "_Com1.tar.gz"),
    ("trail. ", "trail__"), ("..", "__"), (".", "_"), ("..\\x", ".._x"),
    ("ok name.txt", "ok name.txt"), ("CONSOLE", "CONSOLE"),
])
def test_safe_local_name_on_windows(name, safe):
    assert core._safe_local_name(name, windows=True) == safe


@pytest.mark.parametrize("name, safe", [
    ("a:b.txt", "a:b.txt"), ("../x", ".._x"), ("/abs", "_abs"), ("..", "__"),
    ("CON", "CON"),
])
def test_safe_local_name_elsewhere(name, safe):
    assert core._safe_local_name(name, windows=False) == safe


def test_a_recursive_pull_never_writes_outside_the_target(tmp_path):
    names = ["../escape.txt", "..\\up.txt", "/abs.txt", "a:b.txt", "a_b.txt", "CON",
             "dot.", "ok.txt", "sub"]

    class Sftp:
        def stat(self, path):
            return _Attr(path, stat.S_IFDIR if path in ("/data", "/data/sub")
                         else stat.S_IFREG)

        def normalize(self, path):
            return path

        def listdir_attr(self, path):
            if path == "/data":
                return [_Attr(n, stat.S_IFDIR if n == "sub" else stat.S_IFREG)
                        for n in names]
            return [_Attr("inner.txt", stat.S_IFREG)]

        def get(self, remote, local, callback=None):
            with open(local, "wb") as fh:
                fh.write(remote.encode())

    dest = tmp_path / "dest"
    h = SSHHandler(SSHConfig(host="x"), quiet=True)
    res = h.pull("/data", str(dest), recursive=True, sftp=Sftp())
    written = sorted(os.path.relpath(os.path.join(d, f), dest)
                     for d, _, fs in os.walk(dest) for f in fs)
    assert res.files == len(written) == len(names)          # 8 files + sub/inner
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "up.txt").exists()
    assert sorted(os.listdir(tmp_path)) == ["dest"]
    if os.name == "nt":
        # the mapped "a:b.txt" must not overwrite the real "a_b.txt"
        assert (dest / "a_b.txt").read_bytes() == b"/data/a_b.txt"
        assert (dest / "a_b.txt~1").read_bytes() == b"/data/a:b.txt"
        assert (dest / "_CON").exists() and (dest / "dot_").exists()
    assert (dest / "sub" / "inner.txt").read_bytes() == b"/data/sub/inner.txt"


# ------------------------------------------------------------- remote camera
_PSHOME = b"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\r\n"


@pytest.mark.parametrize("shell", ["cmd", "powershell"])
def test_camera_commands_run_under_either_default_shell(ssh, ssh_server, script, shell):
    script(lambda cmd: (_PSHOME, b"", 0) if cmd == "echo $PSHOME" and shell ==
           "powershell" else None)
    ssh.list_cameras(ffmpeg="C:\\Users\\A B\\turbossh_cam\\ffmpeg.exe")
    sent = ssh_server.server.execs[-1]
    inner = ('ver >nul & "C:\\Users\\A B\\turbossh_cam\\ffmpeg.exe" -hide_banner '
             '-list_devices true -f dshow -i dummy')
    if shell == "powershell":
        # cmd syntax is a parse error in PowerShell: handed to cmd verbatim
        assert sent == f'cmd /s /c --% "{inner}"'
    else:
        # a line that starts with a quote loses its first and last quote to cmd /c
        assert sent == inner
    assert ssh._remote_shell() == shell


def _camera_server(script, *, stderr_flood=0):
    """Transport A (tcp://) ffmpeg exits at once; transport B (stdout) sends a
    JPEG — after *stderr_flood* bytes of stderr — and keeps streaming."""
    def behave(cmd):
        if "tcp://" in cmd:
            return (b"", b"", 1)
        if "-f mjpeg" in cmd and cmd.rstrip().endswith("-"):
            def stream(channel):
                if stderr_flood:
                    channel.sendall_stderr(b"x" * stderr_flood + b"\nLAST ERROR LINE\n")
                channel.sendall(b"\xff\xd8\xff\xe0frame")
                time.sleep(30)
            return stream
        return None
    script(behave)


def test_each_camera_stream_is_marked_and_a_restart_stops_only_its_own(ssh, ssh_server,
                                                                       script):
    _camera_server(script)
    chan = ssh.webcam_channel("Cam A", ffmpeg="C:\\t\\turbossh_cam\\ffmpeg.exe")
    try:
        first = [c for c in ssh_server.server.execs if "-metadata comment=" in c]
        token = first[0].split("-metadata comment=")[1].split()[0]
        assert token.startswith("turbossh_cam_") and len(token) > len("turbossh_cam_")
        assert all(token in c for c in first)
        assert ssh._cam_tokens == {token}
        assert chan._prebuf.startswith(b"\xff\xd8")
    finally:
        chan.close()
    n = len(ssh_server.server.execs)
    chan = ssh.webcam_channel("Cam A", ffmpeg="C:\\t\\turbossh_cam\\ffmpeg.exe")
    chan.close()
    started = [c for c in ssh_server.server.execs[n:] if "-metadata comment=" in c]
    kill = decode_ps(started[0])
    assert f"'{token}'" in kill                   # the earlier stream of THIS handler
    assert "video=" not in kill and "$x" not in kill.split("if(")[1], \
        "a normal start must not stop other streams on that machine"
    n = len(ssh_server.server.execs)
    ssh.webcam_channel("Cam A", ffmpeg="C:\\t\\turbossh_cam\\ffmpeg.exe",
                       force=True).close()
    started = [c for c in ssh_server.server.execs[n:] if "-metadata comment=" in c]
    assert "'video=\"Cam A\"'" in decode_ps(started[0])    # force: whoever holds Cam A


def test_webcam_release_stops_only_this_handlers_streams(ssh, ssh_server):
    n = len(ssh_server.server.execs)
    assert ssh.webcam_release().ok
    assert len(ssh_server.server.execs) == n, "nothing of ours to stop: no remote call"
    ssh._cam_tokens = {"turbossh_cam_aaaaaaaaaa", "turbossh_cam_bbbbbbbbbb"}
    ssh.webcam_release()
    src = decode_ps(ssh_server.server.execs[-1])
    assert "'turbossh_cam_aaaaaaaaaa'" in src and "'turbossh_cam_bbbbbbbbbb'" in src
    assert "(TsHas $x" not in src and "'turbossh_cam')" not in src
    assert ssh._cam_tokens == set()
    ssh.webcam_release(force=True)                # the explicit machine-wide clean-up
    assert "(TsHas $x 'turbossh_cam')" in decode_ps(ssh_server.server.execs[-1])
    ssh.webcam_release(ffmpeg_marker="turbossh")  # the CLI's camera-grab --force
    assert "(TsHas $x 'turbossh')" in decode_ps(ssh_server.server.execs[-1])


def test_camera_stderr_is_drained_while_streaming_and_its_tail_kept(ssh, script):
    """3 MB of unread stderr is more than the channel window: without the drain
    the server blocks on it and the first frame never comes."""
    _camera_server(script, stderr_flood=3 * 1024 * 1024)
    chan = ssh.webcam_channel("Cam A", ffmpeg="ffmpeg")
    try:
        assert chan._prebuf.startswith(b"\xff\xd8")
        # the Camera tab's after-failure read (camera_widget._drain_remote_stderr)
        exec_chan = getattr(chan, "_ffmpeg_exec", chan)
        deadline = time.time() + 10
        while "LAST ERROR LINE" not in exec_chan._stderr_tail.text() \
                and time.time() < deadline:
            time.sleep(0.1)
        out = b""
        for _ in range(40):
            if not exec_chan.recv_stderr_ready():
                break
            out += exec_chan.recv_stderr(65536)
        assert out.endswith(b"LAST ERROR LINE\n")
        assert len(out) <= core._FFMPEG_STDERR_KEEP
    finally:
        chan.close()


def test_stderr_tail_keeps_whole_recent_lines():
    class Chan:
        closed, eof_received = False, False

        def __init__(self):
            self.data = [b"first line\n" * 400, b"the end\n"]

        def recv_stderr_ready(self):
            return bool(self.data)

        def recv_stderr(self, n):
            return self.data.pop(0)

    chan = Chan()
    tail = core._StderrTail(chan, keep=100)
    try:
        assert chan.recv_stderr_ready()
        text = b""
        while chan.recv_stderr_ready():
            text += chan.recv_stderr(30)
        assert text.endswith(b"the end\n") and text.startswith(b"first line\n")
        assert len(text) <= 100
    finally:
        chan.closed = True
    assert tail.text() == ""


def test_socket_timeout_on_the_shared_sftp_drops_it(ssh, ssh_server, tmp_path,
                                                    monkeypatch):
    shared = ssh.sftp()

    def stall(*a, **kw):
        raise socket.timeout("timed out")
    monkeypatch.setattr(shared, "stat", stall)
    with pytest.raises(SSHTransferError):
        ssh.pull("/x", str(tmp_path / "x"))
    assert ssh._sftp is None, "a timed-out SFTP client was kept"
    monkeypatch.undo()
    (ssh_server.root / "x").write_text("x")
    ssh.pull("/x", str(tmp_path / "x"))
