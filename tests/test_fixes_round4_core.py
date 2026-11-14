"""Regression tests for the fourth core review (2.1.0): Windows command lines
cmd.exe refuses (over 8,191 characters), connect() / disconnect() blocked for
ever on a stalled link, a liveness probe that took live links for dead and a
changed config for the same target, two transfers to one target clobbering each
other's temporary file, targets that cannot be renamed over, a full disk that
truncated the old file, a temporary copy readable by everyone while it uploads,
releases that report "released" with nothing stopped, the ffmpeg path losing
its %VAR%, and a cancelled download that went on reading."""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import sys
import threading
import time

import paramiko
import pytest

import sshserver
from turbossh import SSHConfig, SSHHandler, SSHTransferError
from turbossh import _transfer, core
from turbossh.exceptions import SSHConnectionError
from turbossh.results import CommandResult, OperationResult

HAVE_PS = os.name == "nt" and bool(shutil.which("powershell"))
CMD_MAX = 8000          # cmd.exe's limit is 8,191 characters a line
_PSHOME = b"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\r\n"


def decode_ps(cmd: str) -> str:
    b64 = cmd.split("-EncodedCommand", 1)[1].split()[0]
    return base64.b64decode(b64).decode("utf-16-le")


def leftovers(folder):
    return sorted(n for n in os.listdir(folder)
                  if n.endswith((_transfer.TEMP_SUFFIX, _transfer.ASIDE_SUFFIX)))


def _age(path, seconds=3600):
    when = time.time() - seconds
    os.utime(str(path), (when, when))


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
    """A handler that only records the commands it would send."""
    handler = SSHHandler(SSHConfig(host="target"), quiet=True)
    sent = []

    class Transport:
        def open_session(self, timeout=None):
            return _CaptureChan(sent)

    def run(cmd, **kw):
        sent.append(cmd)
        return CommandResult(cmd, 0, "", "", 0.0)
    monkeypatch.setattr(handler, "_ensure_connected", lambda: None)
    monkeypatch.setattr(SSHHandler, "transport", property(lambda self: Transport()))
    monkeypatch.setattr(handler, "_run", run)
    monkeypatch.setattr(handler, "run", run)
    monkeypatch.setattr(handler, "stream", lambda cmd, **kw: sent.append(cmd))
    monkeypatch.setattr(SSHHandler, "is_connected", property(lambda self: True))
    return handler, sent


@pytest.fixture()
def sftp_client(ssh):
    client = ssh.open_sftp()
    yield client
    client.close()


@pytest.fixture()
def script(ssh_server):
    """``script(fn)``: fn(cmd) -> (stdout, stderr, status) or None ("ok")."""
    def install(fn):
        def run(channel, cmd):
            time.sleep(0.05)
            try:
                stdout, stderr, status = fn(cmd) or (b"ok\n", b"", 0)
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


# ===================================================================== H1
# A port name as long as Windows makes them, and a long one a user may pass
# with mode="windows"; a long camera name and ffmpeg path.
_PORTS = [("COM256", "auto"), ("\\\\.\\GLOBALROOT\\Device\\VCP0_USB_S", "windows")]
_CAMERA = "USB2.0 HD UVC WebCam (Integrated Camera, Rear) " + "x" * 80
_FFMPEG = ("C:\\Users\\a.very.long.user.name\\AppData\\Local\\TurboSSH\\"
           "turbossh_cam\\ffmpeg-7.1-full_build\\bin\\" + "d" * 60 + "\\ffmpeg.exe")


def _windows_serial_commands(monkeypatch):
    handler, sent = _offline_handler(monkeypatch)
    for dev, mode in _PORTS:
        for force in (False, True):
            handler.serial_bridge(dev, mode=mode, force=force)
            handler.serial_stream(dev, mode=mode, force=force)
            handler.serial_release(dev, mode=mode, force=force)
        handler.serial_in_use(dev, mode=mode)
        handler.serial_write(dev, "reset\u00e4" * 1000, mode=mode)   # 7,000 bytes
        handler._serial_stop_run(dev, "a" * 12)          # a stream's end
    sent.append(handler._webcam_kill_cmd(
        tokens=[f"turbossh_cam_{i:010d}" for i in range(6)], camera=_CAMERA,
        marker="turbossh_cam" + "m" * 60, report=True))
    return sent


def test_every_windows_serial_command_fits_a_cmd_exe_line(monkeypatch):
    """Windows OpenSSH runs a command as `cmd /c <command>`, which refuses lines
    over 8,191 characters: a forced open of COM12 was 9,414 ("Open it anyway?"
    always failed under a cmd default shell)."""
    sent = _windows_serial_commands(monkeypatch)
    assert len(sent) > 20
    long_ones = [(len(c), c[:60]) for c in sent if len(c) >= CMD_MAX]
    assert not long_ones, long_ones
    # the long writes went out in several commands each
    writes = [c for c in sent if "-EncodedCommand" in c
              and "serial write failed" in decode_ps(c)]
    assert len(writes) >= 2 * len(_PORTS) * 2


def test_a_forced_open_of_com256_is_well_under_the_limit(monkeypatch):
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_bridge("COM256", force=True)
    assert all(len(c) < 7000 for c in sent), [len(c) for c in sent]
    # the forced stop runs first, as its own command, then the bridge (whose
    # command line still starts with the port tag)
    stop, bridge = sent[-2:]
    assert "$P|?{TsOurs $_}" in decode_ps(stop)
    tag = core._ps_b64(core._serial_ps_header("COM256"))
    assert bridge.split("-EncodedCommand ", 1)[1].startswith(tag)


@pytest.mark.parametrize("shell", ["cmd", "powershell"])
def test_camera_commands_fit_a_cmd_exe_line(ssh, ssh_server, script, shell):
    script(lambda cmd: (_PSHOME, b"", 0) if cmd == "echo $PSHOME" and shell ==
           "powershell" else (b"", b"", 1))
    ssh._cam_tokens = {f"turbossh_cam_{i:010d}" for i in range(4)}
    ssh.list_cameras(ffmpeg=_FFMPEG)
    with pytest.raises(SSHConnectionError):
        ssh.webcam_channel(_CAMERA, ffmpeg=_FFMPEG, force=True)
    ssh.webcam_release(force=True)
    cmds = ssh_server.server.execs
    assert any("-metadata comment=" in c for c in cmds)
    assert all(len(c) < CMD_MAX for c in cmds), \
        sorted(len(c) for c in cmds)[-3:]


@pytest.mark.skipif(not HAVE_PS, reason="needs Windows PowerShell")
def test_every_windows_script_parses_in_windows_powershell(monkeypatch, tmp_path):
    """The shortened scripts, checked by PowerShell's own parser (5.1)."""
    scripts = [decode_ps(c) for c in _windows_serial_commands(monkeypatch)
               if "-EncodedCommand" in c]
    for n, src in enumerate(scripts):
        (tmp_path / f"s{n}.ps1").write_text(src, encoding="utf-8")
    check = (
        "$bad=0;Get-ChildItem -LiteralPath '" + str(tmp_path) + "' -Filter *.ps1|"
        "ForEach-Object{$t=$null;$e=$null;[void][System.Management.Automation."
        "Language.Parser]::ParseInput([IO.File]::ReadAllText($_.FullName),[ref]$t,"
        "[ref]$e);if($e.Count){$bad++;[Console]::Out.WriteLine($_.Name+': '+"
        "($e|ForEach-Object{$_.Message})-join '; ')}};exit $bad")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          check], capture_output=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stdout.decode(errors="replace")
    assert len(scripts) > 20


# --- the scripts run for real (Windows): a fake port stands in for the device
_FAKE_PORT = """class FakePort {
  [bool]$IsOpen = $false; [int]$BytesToRead = 0; [string]$q = ''
  $WriteTimeout; $Encoding
  [void]Open() { if ($env:TS_FAKE -eq 'busy') {
      throw [System.UnauthorizedAccessException]::new('Access to the port is denied.') }
    if ($env:TS_FAKE -eq 'missing') {
      throw [System.IO.IOException]::new('The port does not exist.', -2147024894) }
    $this.IsOpen = $true; $this.q = 'hello '; $this.BytesToRead = 6 }
  [string]ReadExisting() { $s = $this.q; $this.q = ''; $this.BytesToRead = 0; return $s }
  [void]Write([byte[]]$b, [int]$o, [int]$n) {
    $this.q += [Text.Encoding]::UTF8.GetString($b, $o, $n); $this.BytesToRead = $this.q.Length }
  [void]Close() { $this.IsOpen = $false }
}
"""


def _with_fake_port(cmd: str) -> str:
    src = decode_ps(cmd)
    assert "New-Object IO.Ports.SerialPort $dev," in src
    head, _, rest = src.partition("New-Object IO.Ports.SerialPort $dev,")
    rest = rest.split(";", 1)[1]
    return _FAKE_PORT + head + "[FakePort]::new();" + rest


def _run_ps(src, tmp_path, fake="", stdin=b""):
    env = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), TS_FAKE=fake)
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                           "-EncodedCommand", core._ps_b64(src)], input=stdin,
                          capture_output=True, timeout=120, env=env)


@pytest.mark.skipif(not HAVE_PS, reason="runs the Windows scripts for real")
def test_the_short_bridge_script_still_bridges_both_ways(monkeypatch, tmp_path):
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_bridge("COM9387")
    src = _with_fake_port(sent[-1])
    out = _run_ps(src, tmp_path, stdin=b"abc")
    # the device's output, then what was typed (the fake echoes it), and on
    # end of input the bridge ends and drops its own PID file
    assert out.stdout.replace(b"\r", b"") == b"hello abc", out
    assert out.returncode == 0, out
    assert not (tmp_path / "turbossh_ser_COM9387.pid").exists()
    for fake, said in (("busy", b"BUSY:COM9387"),
                       ("missing", b"[serial] cannot open COM9387: The port does "
                                   b"not exist.")):
        out = _run_ps(src, tmp_path, fake=fake)
        assert out.returncode == 2 and out.stdout.strip() == said, out


@pytest.mark.skipif(not HAVE_PS, reason="runs the Windows scripts for real")
def test_the_short_stream_script_reports_a_busy_port(monkeypatch, tmp_path):
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_stream("COM9387", force=True)
    stream = [c for c in sent if "SerialPort" in decode_ps(c)][-1]
    out = _run_ps(_with_fake_port(stream), tmp_path, fake="busy")
    text = out.stdout.decode(errors="replace")
    assert out.returncode == 2 and "cannot open COM9387" in text and "in use" in text


@pytest.mark.skipif(not HAVE_PS, reason="runs the Windows scripts for real")
def test_a_forced_release_never_stops_its_own_ancestors(tmp_path):
    """A parent whose own command line is a tagged reader of the port (the
    worst case of sshd's wrapper) must survive the forced stop it runs."""
    handler = SSHHandler(SSHConfig(host="x"), quiet=True)
    sent = []
    handler.run = handler._run = lambda cmd, **kw: sent.append(cmd)
    handler.serial_release("COM9387", force=True)
    release = sent[-1]
    marker = tmp_path / "parent-finished"
    parent_src = (core._serial_ps_header("COM9387") + "& " + release
                  + f"; Set-Content -LiteralPath '{marker}' -Value done")
    parent = subprocess.Popen(["powershell", "-NoProfile", "-EncodedCommand",
                               core._ps_b64(parent_src)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        parent.wait(120)
    finally:
        if parent.poll() is None:
            parent.kill()
    assert marker.exists(), "the forced stop killed its own parent"


# ===================================================================== L4
@pytest.mark.skipif(not HAVE_PS, reason="runs the Windows scripts for real")
def test_a_windows_release_with_nothing_tracked_says_so(monkeypatch, tmp_path):
    # a port name of this test run's own: a forced release of a shared name in
    # another run on this machine (it stops every TurboSSH reader of the port)
    # would stop this run's tracked reader
    port = f"COM8{os.getpid() % 100000}"
    handler = SSHHandler(SSHConfig(host="x"), quiet=True)
    sent = []
    handler.run = handler._run = lambda cmd, **kw: sent.append(cmd)
    handler.serial_release(port)
    out = _run_ps(decode_ps(sent[-1]), tmp_path)
    assert out.returncode == 1 and b"force=True" in out.stderr, out
    assert b"released" not in out.stdout
    # one of ours tracked: stopped, "released"
    sleeper = subprocess.Popen(
        ["powershell", "-NoProfile", "-EncodedCommand",
         core._ps_b64(core._serial_ps_header(port, "b" * 12) + "Start-Sleep 60")],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        (tmp_path / f"turbossh_ser_{port}.pid").write_text(str(sleeper.pid))
        out = _run_ps(decode_ps(sent[-1]), tmp_path)
        assert out.returncode == 0 and out.stdout.strip() == b"released", out
        sleeper.wait(20)
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_a_posix_release_with_nothing_tracked_says_so(monkeypatch):
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_release("/dev/ttyUSB0")
    assert "exit 1" in sent[-1] and "force=True" in sent[-1]
    assert "fuser" not in sent[-1]
    handler.serial_release("/dev/ttyUSB0", force=True)
    assert "fuser -k /dev/ttyUSB0" in sent[-1] and "exit 1" not in sent[-1]


def test_nothing_to_release_is_an_answer_not_a_warning(ssh, ssh_server, script):
    """A GUI tab whose bridge ended by itself releases nothing on close: the
    result says so (exit 1, the hint), and the log has no warning for it."""
    hint = core.SSHHandler._NOTHING_TRACKED.encode()
    script(lambda cmd: (b"", hint + b"\n", 1))
    said = []
    ssh._log_callback = said.append
    res = ssh.serial_release("/dev/ttyUSB0")
    assert res.exit_code == 1 and "force=True" in res.stderr
    assert not any(m.startswith("[WARNING]") for m in said), said
    safe = ssh.serial_release("COM5", safe=True)
    assert safe.success and safe.value.exit_code == 1


@pytest.mark.skipif(not shutil.which("sh"), reason="runs the sh script for real")
def test_a_posix_release_with_nothing_tracked_exits_1_for_real(monkeypatch, tmp_path):
    monkeypatch.setattr(SSHHandler, "_POSIX_PID_DIR", str(tmp_path).replace("\\", "/"))
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_release("/dev/ttyUSB0")
    out = subprocess.run([shutil.which("sh"), "-c", sent[-1]], capture_output=True,
                         timeout=30)
    assert out.returncode == 1 and b"force=True" in out.stderr, out
    assert b"released" not in out.stdout


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc")
def test_a_posix_release_of_a_tracked_bridge_says_released(monkeypatch, tmp_path):
    monkeypatch.setattr(SSHHandler, "_POSIX_PID_DIR", str(tmp_path))
    handler, sent = _offline_handler(monkeypatch)
    cat = subprocess.Popen(["cat"], stdin=subprocess.PIPE)
    try:
        (tmp_path / "turbossh_ser_devttyUSB0.pid").write_text(f"{cat.pid} {cat.pid}\n")
        handler.serial_release("/dev/ttyUSB0")
        out = subprocess.run(["sh", "-c", sent[-1]], capture_output=True, timeout=30)
        assert out.returncode == 0 and out.stdout.strip() == b"released", out
        cat.wait(10)
    finally:
        if cat.poll() is None:
            cat.kill()


# ===================================================================== M6
def test_webcam_release_with_nothing_of_its_own_fails_visibly(ssh, ssh_server):
    """A clean-up script after a crashed run took "released 0" (exit 0) for a
    freed camera."""
    res = ssh.webcam_release()
    assert isinstance(res, CommandResult)
    assert res.exit_code == 1 and not res.ok
    assert "force=True" in res.stderr
    safe = ssh.webcam_release(safe=True)          # the GUI's call: no exception
    assert isinstance(safe, OperationResult) and safe.success
    assert safe.value.exit_code == 1


# ===================================================================== L5
@pytest.mark.parametrize("shell", ["cmd", "powershell"])
def test_the_ffmpeg_path_keeps_its_environment_variables(ssh, ssh_server, script,
                                                         shell):
    """ffmpeg="%ProgramFiles%\\ffmpeg\\bin\\ffmpeg.exe" worked in 2.0: cmd (and
    PowerShell behind --%) expand it."""
    script(lambda cmd: (_PSHOME, b"", 0) if cmd == "echo $PSHOME" and shell ==
           "powershell" else (b"", b"", 1))
    exe = "%ProgramFiles%\\ffmpeg\\bin\\ffmpeg.exe"
    ssh.list_cameras(ffmpeg=exe)
    assert f'"{exe}" -hide_banner -list_devices' in ssh_server.server.execs[-1]
    with pytest.raises(SSHConnectionError):
        ssh.webcam_channel("Cam %USERNAME%", ffmpeg=exe + '"|')
    started = [c for c in ssh_server.server.execs if "video=" in c]
    assert started and all(f'"{exe}"' in c for c in started)
    assert all('video="Cam USERNAME"' in c for c in started)   # names: literal


# ===================================================================== H2
@pytest.fixture()
def stalled_link(monkeypatch, make_config, tmp_path):
    """A handler whose shared SFTP client is open while another thread uploads,
    and a server that then stops reading (a hung target / dead VPN): the
    upload's send buffer fills and Paramiko's writer holds the transport's
    send lock. ``.release()`` lets the server read again."""
    gate = threading.Event()
    server_side, frozen = [], set()
    read = paramiko.packet.Packetizer.read_message
    start = paramiko.Transport.start_server

    def read_message(self):
        if self in frozen:
            gate.wait(120)
        return read(self)

    def start_server(self, *a, **kw):
        server_side.append(self.packetizer)
        return start(self, *a, **kw)
    monkeypatch.setattr(paramiko.packet.Packetizer, "read_message", read_message)
    monkeypatch.setattr(paramiko.Transport, "start_server", start_server)
    monkeypatch.setattr(SSHHandler, "_LIVENESS_TIMEOUT", 1.0)
    handler = SSHHandler(make_config(connect_timeout=1), quiet=True)
    handler.connect()
    handler.sftp()                               # the shared client is open
    worker = handler.open_sftp()
    local = tmp_path / "big.bin"
    local.write_bytes(os.urandom(48 * 1024 * 1024))
    progress = {"done": 0, "when": time.monotonic()}

    def callback(done, total):
        progress["done"], progress["when"] = done, time.monotonic()
        if done > 4 * 1024 * 1024 and not frozen:
            frozen.update(server_side)           # the target stops reading

    def upload():
        try:
            handler.push(str(local), "/big.bin", sftp=worker, callback=callback)
        except Exception:
            pass
    threading.Thread(target=upload, daemon=True).start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:          # until the upload is stuck
        time.sleep(0.2)
        if frozen and time.monotonic() - progress["when"] > 2.0:
            break
    assert frozen, "the upload never got going"

    class Stalled:
        pass
    s = Stalled()
    s.handler, s.release = handler, gate.set
    yield s
    gate.set()
    try:
        handler.disconnect()
    except Exception:
        pass


def _returns_within(fn, seconds):
    done = threading.Event()
    errors = []

    def call():
        try:
            fn()
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    threading.Thread(target=call, daemon=True).start()
    return done.wait(seconds), errors


def test_connect_on_a_stalled_link_does_not_block(stalled_link):
    """connect() closed the shared SFTP client first: Channel.close waited for
    the send lock the stuck upload held — for ever, holding the connection
    lock (every other thread's connect / disconnect hung behind it)."""
    returned, errors = _returns_within(stalled_link.handler.connect, 20)
    assert returned, "connect() blocked on the stalled link"
    assert not errors, errors
    assert stalled_link.handler.run("echo").ok


def test_disconnect_on_a_stalled_link_does_not_block(stalled_link):
    returned, errors = _returns_within(stalled_link.handler.disconnect, 20)
    assert returned, "disconnect() blocked on the stalled link"
    assert not errors and stalled_link.handler._client is None


# ===================================================================== M4
def test_connect_keeps_a_live_link_whose_server_ignores_keepalives(
        make_config, ssh_server, monkeypatch):
    """A server that answers keepalive@openssh.com with SSH_MSG_UNIMPLEMENTED
    (Paramiko ignores that reply) looked dead: connect() closed the live
    client, and its shell, tunnels and other threads' SFTP with it."""
    original = paramiko.Transport._parse_global_request

    def unimplemented(self, m):
        if self.server_mode:
            msg = paramiko.Message()
            msg.add_byte(paramiko.common.cMSG_UNIMPLEMENTED)
            msg.add_int(0)
            self._send_message(msg)
            return
        return original(self, m)
    monkeypatch.setattr(paramiko.Transport, "_parse_global_request", unimplemented)
    monkeypatch.setattr(SSHHandler, "_LIVENESS_TIMEOUT", 1.0)
    handler = SSHHandler(make_config(), quiet=True)
    handler.connect()
    try:
        client = handler.client
        shell = handler.open_shell()
        handler.connect()
        assert handler.client is client, "a live connection was replaced"
        assert not shell.channel.closed
        shell.close()
    finally:
        handler.disconnect()


def test_connect_gives_a_slow_link_the_connect_timeout(make_config, ssh_server,
                                                       monkeypatch):
    """An answer stuck behind bulk data on a slow VPN took longer than the
    fixed 5 s: the probe waits the connect timeout (at least 10 s)."""
    monkeypatch.setattr(SSHHandler, "_LIVENESS_TIMEOUT", 1.0)
    handler = SSHHandler(make_config(connect_timeout=5), quiet=True)
    handler.connect()
    srv = ssh_server.server
    channel_request, global_request = srv.check_channel_request, srv.check_global_request

    def slow_channel(kind, chanid):
        time.sleep(2.5)
        return channel_request(kind, chanid)

    def slow_global(kind, msg):
        time.sleep(2.5)
        return global_request(kind, msg)
    try:
        client = handler.client
        srv.check_channel_request, srv.check_global_request = slow_channel, slow_global
        handler.connect()
        assert handler.client is client, "a slow but live connection was replaced"
    finally:
        srv.check_channel_request, srv.check_global_request = (channel_request,
                                                               global_request)
        handler.disconnect()


def test_any_packet_that_arrives_counts_as_a_sign_of_life():
    """The probe's own answer may sit behind bulk data: packets arriving
    meanwhile already show the server is alive."""
    stuck = threading.Event()

    class Packetizer:
        pass
    packetizer = Packetizer()
    setattr(packetizer, "_Packetizer__received_packets", 0)
    setattr(packetizer, "_Packetizer__received_bytes", 0)

    class Transport:
        def __init__(self):
            self.packetizer = packetizer

        def is_active(self):
            return True

        def open_session(self, timeout=None):
            stuck.wait(30)
            raise EOFError()

        def global_request(self, *a, **kw):
            stuck.wait(30)

    def bulk():
        for _ in range(20):
            time.sleep(0.1)
            setattr(packetizer, "_Packetizer__received_packets",
                    getattr(packetizer, "_Packetizer__received_packets") + 1)
    threading.Thread(target=bulk, daemon=True).start()
    try:
        t0 = time.monotonic()
        assert SSHHandler._link_alive(Transport(), 5.0)
        assert time.monotonic() - t0 < 3
    finally:
        stuck.set()


# ===================================================================== L1
@pytest.fixture()
def second_server(tmp_path):
    root = tmp_path / "remote2"
    root.mkdir()
    srv = sshserver.LoopbackSSH(root)
    yield srv
    srv.close()


def test_connect_follows_a_changed_config(make_config, ssh_server, second_server):
    """``ssh.config.port = other; ssh.connect()`` kept the old session while it
    answered (2.0 reconnected)."""
    handler = SSHHandler(make_config(), quiet=True)
    handler.connect()
    try:
        old = handler.client
        handler.config.port = second_server.port
        handler.connect()
        assert handler.client is not old
        assert len(second_server.transports) == 1
        assert not old.get_transport() or not old.get_transport().is_active()
        handler.run("echo from b")
        assert "echo from b" in second_server.server.execs
        handler.connect()                       # unchanged now: kept
        assert len(second_server.transports) == 1
    finally:
        handler.disconnect()


def test_connect_follows_a_changed_jump_host(make_config, ssh_server, second_server):
    """The loopback servers forward direct-tcpip, so each can be the jump host."""
    jump = make_config()
    handler = SSHHandler(make_config(jump_host=jump), quiet=True)
    handler.connect()
    try:
        assert handler.run("echo").ok
        before_a = len(ssh_server.transports)
        jump.port = second_server.port           # the same target, another jump host
        handler.connect()
        assert len(second_server.transports) >= 1, "the old jump host was kept"
        assert handler._jump.config.port == second_server.port
        assert handler.run("echo").ok
        assert len(ssh_server.transports) > before_a      # the target, through B
    finally:
        handler.disconnect()


# ===================================================================== M2
def _two_clients(ssh):
    a, b = ssh.open_sftp(), ssh.open_sftp()
    return a, b


def test_two_uploads_to_one_target_never_share_a_temporary_file(ssh, ssh_server,
                                                                tmp_path):
    """B removed A's .<name>.tsp (or, where it could not — Windows — wrote the
    real file in place), and A renamed B's half-written copy over the target."""
    target = ssh_server.root / "app.bin"
    target.write_bytes(b"O" * 1000)
    size = 32768 * 40
    la, lb = tmp_path / "a.bin", tmp_path / "b.bin"
    la.write_bytes(b"A" * size)
    lb.write_bytes(b"B" * size)
    a_paused, b_started, b_go = threading.Event(), threading.Event(), threading.Event()
    results = {}

    def cb_a(done, total):
        if done == 32768:
            a_paused.set()
            b_started.wait(10)

    def cb_b(done, total):
        if done == 32768 * 3:
            b_started.set()
            b_go.wait(20)

    def run(name, sftp, local, cb):
        try:
            results[name] = _transfer.put_file(sftp, str(local), "/app.bin", callback=cb)
        except Exception as exc:
            results[name] = exc
    sa, sb = _two_clients(ssh)
    try:
        ta = threading.Thread(target=run, args=("A", sa, la, cb_a))
        ta.start()
        assert a_paused.wait(10)
        tb = threading.Thread(target=run, args=("B", sb, lb, cb_b))
        tb.start()
        ta.join(30)
        assert results.get("A") == "", results
        assert target.read_bytes() == b"A" * size, "A's upload was clobbered"
        b_go.set()
        tb.join(30)
        assert results.get("B") == "", results
        assert target.read_bytes() == b"B" * size
        assert leftovers(ssh_server.root) == []
    finally:
        b_go.set()
        sa.close()
        sb.close()


def test_a_temporary_name_an_orphaned_session_holds_is_skipped(ssh, ssh_server,
                                                               tmp_path):
    """After a VPN drop the old server session may still hold its temporary
    file: a retry must pick another name, not write the file in place (or
    fail), and must not remove a file that may still be written."""
    (ssh_server.root / "fw.bin").write_bytes(b"OLD")
    held = ssh_server.root / _transfer.temp_name("fw.bin")
    held.write_bytes(b"half an upload")
    src = tmp_path / "fw.bin"
    src.write_bytes(b"NEW FIRMWARE")
    said = []
    ssh._log_callback = said.append
    with open(str(held), "rb"):               # held open (Windows: no delete)
        ssh.push(str(src), "/fw.bin")
    assert (ssh_server.root / "fw.bin").read_bytes() == b"NEW FIRMWARE"
    assert not any("in place" in m for m in said), said
    assert held.read_bytes() == b"half an upload", "a live temporary file was removed"


def test_abandoned_temporary_files_are_removed_live_ones_kept(ssh, ssh_server, tmp_path):
    (ssh_server.root / "fw.bin").write_bytes(b"OLD")
    old = ssh_server.root / _transfer.temp_name("fw.bin", slot=0)
    live = ssh_server.root / _transfer.temp_name("fw.bin", slot=1)
    old.write_bytes(b"left an hour ago")
    _age(old)
    live.write_bytes(b"being written now")
    src = tmp_path / "fw.bin"
    src.write_bytes(b"NEW")
    ssh.push(str(src), "/fw.bin")
    assert (ssh_server.root / "fw.bin").read_bytes() == b"NEW"
    assert leftovers(ssh_server.root) == [live.name]


def test_two_downloads_to_one_path_never_share_a_temporary_file(ssh, ssh_server,
                                                                tmp_path):
    size = 32768 * 40
    (ssh_server.root / "a.bin").write_bytes(b"A" * size)
    (ssh_server.root / "b.bin").write_bytes(b"B" * size)
    local = tmp_path / "out.bin"
    a_paused, b_started, b_go = threading.Event(), threading.Event(), threading.Event()
    results = {}

    def cb_a(done, total):
        if not a_paused.is_set():
            a_paused.set()
            b_started.wait(10)

    def cb_b(done, total):
        if done >= 32768 * 3 and not b_started.is_set():
            b_started.set()
            b_go.wait(20)

    def run(name, sftp, remote, cb):
        try:
            results[name] = _transfer.get_file(sftp, remote, str(local), cb)
        except Exception as exc:
            results[name] = exc
    sa, sb = _two_clients(ssh)
    try:
        ta = threading.Thread(target=run, args=("A", sa, "/a.bin", cb_a))
        ta.start()
        assert a_paused.wait(10)
        tb = threading.Thread(target=run, args=("B", sb, "/b.bin", cb_b))
        tb.start()
        ta.join(30)
        assert results.get("A") == (size, ""), results
        assert local.read_bytes() == b"A" * size
        b_go.set()
        tb.join(30)
        assert results.get("B") == (size, ""), results
        assert local.read_bytes() == b"B" * size
        assert leftovers(tmp_path) == []
    finally:
        b_go.set()
        sa.close()
        sb.close()


def test_temporary_names_are_short_and_never_another_targets():
    assert _transfer.temp_name("fw.bin") == ".fw.bin.0.tsp"
    assert _transfer.temp_name("fw.bin", slot=3) == ".fw.bin.3.tsp"
    # the number always comes last: "fw.bin" #1 is not "fw.bin.1" #0
    assert _transfer.temp_name("fw.bin", slot=1) != _transfer.temp_name("fw.bin.1")
    long_name = "n" * 254
    for slot in (0, 7, "a1b2c3"):
        name = _transfer.temp_name(long_name, slot=slot)
        assert len(name.encode()) <= 255 and name.endswith(f".{slot}.tsp")


# ===================================================================== M3
def _refuse_renames_of(monkeypatch, name):
    calls = []
    for op in ("posix_rename", "rename"):
        original = getattr(sshserver.StubSFTP, op)

        def refused(self, old, new, _original=original, _op=op):
            calls.append(_op)
            if old.endswith("/" + name) or new.endswith("/" + name):
                return paramiko.SFTP_FAILURE          # EBUSY maps to "Failure"
            return _original(self, old, new)
        monkeypatch.setattr(sshserver.StubSFTP, op, refused)
    return calls


def test_a_target_that_cannot_be_renamed_over_is_written_in_place(
        ssh, ssh_server, sftp_client, monkeypatch):
    """A bind-mounted /etc/resolv.conf (EBUSY), or a Windows file held open
    without delete sharing: the push failed with "OSError: Failure" after four
    renames; 2.0's in-place write worked."""
    calls = _refuse_renames_of(monkeypatch, "resolv.conf")
    (ssh_server.root / "etc").mkdir()
    (ssh_server.root / "etc" / "resolv.conf").write_bytes(b"nameserver 1.1.1.1\n")
    how = _transfer.put_data(sftp_client, b"nameserver 9.9.9.9\n", "/etc/resolv.conf")
    assert "in place" in how and "cannot be replaced" in how
    assert (ssh_server.root / "etc" / "resolv.conf").read_bytes() == \
        b"nameserver 9.9.9.9\n"
    assert calls == ["posix_rename"], "the move-aside dance ran on a posix-rename server"
    assert leftovers(ssh_server.root / "etc") == []


# ===================================================================== M5
def _fail_temp_writes_after(monkeypatch, limit):
    original = paramiko.SFTPHandle.write

    def write(self, offset, data):
        if os.path.basename(self.filename).endswith(_transfer.TEMP_SUFFIX) \
                and offset + len(data) > limit:
            return paramiko.SFTP_FAILURE         # what ENOSPC looks like in SFTP v3
        return original(self, offset, data)
    monkeypatch.setattr(paramiko.SFTPHandle, "write", write)


def test_no_room_for_a_bigger_new_file_keeps_the_old_one(ssh, ssh_server, tmp_path,
                                                         monkeypatch):
    """Written in place, the bigger new binary truncated the old one and
    stopped part-way on a nearly full partition."""
    target = ssh_server.root / "app.bin"
    target.write_bytes(b"O" * 100_000)
    _fail_temp_writes_after(monkeypatch, 50_000)
    src = tmp_path / "app.bin"
    src.write_bytes(os.urandom(400_000))           # > old + what the copy took
    with pytest.raises(SSHTransferError, match="left as it was"):
        ssh.push(str(src), "/app.bin")
    assert target.read_bytes() == b"O" * 100_000
    assert leftovers(ssh_server.root) == []


def test_no_room_but_the_new_file_fits_is_written_in_place(ssh, ssh_server, tmp_path,
                                                           monkeypatch):
    target = ssh_server.root / "app.bin"
    target.write_bytes(b"O" * 100_000)
    _fail_temp_writes_after(monkeypatch, 150_000)
    src = tmp_path / "app.bin"
    src.write_bytes(os.urandom(200_000))           # <= old + the copy's 150 KB
    said = []
    ssh._log_callback = said.append
    ssh.push(str(src), "/app.bin")
    assert target.read_bytes() == src.read_bytes()
    assert any("no room" in m for m in said), said
    assert leftovers(ssh_server.root) == []


# ===================================================================== L3
def test_the_copy_gets_the_old_mode_before_any_data(ssh, ssh_server, tmp_path,
                                                    monkeypatch):
    """The copy was created 0644 and chmod'ed 0600 only after the data: a
    secret was readable by everyone while it uploaded."""
    (ssh_server.root / "secret.key").write_bytes(b"OLD")
    original_lstat, original_stat = sshserver.StubSFTP.lstat, sshserver.StubSFTP.stat

    def private(fn):
        def patched(self, path):
            attr = fn(self, path)
            if path == "/secret.key" and not isinstance(attr, int):
                attr.st_mode = stat.S_IFREG | 0o600
            return attr
        return patched
    monkeypatch.setattr(sshserver.StubSFTP, "lstat", private(original_lstat))
    monkeypatch.setattr(sshserver.StubSFTP, "stat", private(original_stat))
    events = []

    def chattr(self, path, attr):
        if attr._flags & attr.FLAG_PERMISSIONS:
            events.append(("chmod", path, stat.S_IMODE(attr.st_mode)))
        return paramiko.SFTP_OK
    monkeypatch.setattr(sshserver.StubSFTP, "chattr", chattr)
    write = paramiko.SFTPHandle.write

    def spy_write(self, offset, data):
        events.append(("write", self.filename.replace("\\", "/").rsplit("/", 1)[-1]))
        return write(self, offset, data)
    monkeypatch.setattr(paramiko.SFTPHandle, "write", spy_write)
    src = tmp_path / "secret.key"
    src.write_bytes(b"NEW SECRET" * 1000)
    ssh.push(str(src), "/secret.key")
    tmp = _transfer.temp_name("secret.key")
    first_write = events.index(("write", tmp))
    assert ("chmod", "/" + tmp, 0o600) in events[:first_write], events[:5]
    assert (ssh_server.root / "secret.key").read_bytes() == src.read_bytes()


def test_a_refused_chmod_writes_the_file_in_place(ssh, ssh_server, tmp_path,
                                                  monkeypatch):
    """A failed chmod was ignored: the file got the server's default mode."""
    (ssh_server.root / "tool.sh").write_bytes(b"OLD")
    original_lstat = sshserver.StubSFTP.lstat

    def lstat(self, path):
        attr = original_lstat(self, path)
        if path == "/tool.sh" and not isinstance(attr, int):
            attr.st_mode = stat.S_IFREG | 0o750
        return attr
    monkeypatch.setattr(sshserver.StubSFTP, "lstat", lstat)

    def chattr(self, path, attr):
        if attr._flags & attr.FLAG_PERMISSIONS:
            return paramiko.SFTP_PERMISSION_DENIED
        return paramiko.SFTP_OK
    monkeypatch.setattr(sshserver.StubSFTP, "chattr", chattr)
    src = tmp_path / "tool.sh"
    src.write_bytes(b"#!/bin/sh\necho new\n")
    said = []
    ssh._log_callback = said.append
    ssh.push(str(src), "/tool.sh")
    assert (ssh_server.root / "tool.sh").read_bytes() == src.read_bytes()
    assert any("in place" in m and "mode" in m for m in said), said
    assert ssh_server.server.rename_calls == []
    assert leftovers(ssh_server.root) == []


# ===================================================================== L7
def _read_ahead_threads():
    return [t for t in threading.enumerate()
            if getattr(getattr(t, "_target", None), "__name__", "") == "_prefetch_thread"]


@pytest.mark.skipif(not getattr(_transfer, "_CAN_STOP_PREFETCH", True),
                    reason="this Paramiko's read-ahead cannot be stopped")
def test_a_cancelled_download_sends_no_more_reads(ssh, ssh_server, sftp_client,
                                                  tmp_path, monkeypatch):
    """Paramiko's read-ahead thread went on after a cancelled download had
    closed the file: it sent more reads on the closed handle, and then — its
    replies being errors that never free a slot — waited for room for ever,
    one spinning thread per cancelled download.

    How far the read-ahead got before the cancel depends on the machine: it
    caps the reads in flight, not how far it runs ahead of what was read, so
    on a fast loopback it may have asked for the whole file by then. What is
    checked: its thread ends, and after the cancel no more than the reads
    already in flight go out."""
    before = set(_read_ahead_threads())
    size = 32 * 1024 * 1024
    (ssh_server.root / "big.bin").write_bytes(b"\0" * size)
    reads = {"n": 0}
    original = paramiko.SFTPHandle.read

    def counting(self, offset, length):
        reads["n"] += 1
        return original(self, offset, length)
    monkeypatch.setattr(paramiko.SFTPHandle, "read", counting)

    class Cancelled(Exception):
        pass

    at_cancel = {}

    def cancel(done, total):
        if done > 2 * 1024 * 1024:
            at_cancel.setdefault("reads", reads["n"])
            raise Cancelled()
    with pytest.raises(Cancelled):
        _transfer.get_file(sftp_client, "/big.bin", str(tmp_path / "big.bin"), cancel)
    time.sleep(1.5)                               # time for a runaway thread
    for _ in range(3):
        sftp_client.stat("/big.bin")
    deadline = time.monotonic() + 5
    while set(_read_ahead_threads()) - before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not set(_read_ahead_threads()) - before, "the read-ahead thread lives on"
    settled = reads["n"]
    time.sleep(0.5)
    assert reads["n"] == settled, "reads still go out after the read-ahead ended"
    # the ones in flight at the cancel (PREFETCH), and the one its thread held
    assert reads["n"] - at_cancel["reads"] <= _transfer.PREFETCH + 2, (
        reads["n"], at_cancel["reads"])
    assert leftovers(tmp_path) == []
    # and the client still works for the next download
    (ssh_server.root / "small.bin").write_bytes(b"S" * 100_000)
    assert _transfer.get_file(sftp_client, "/small.bin",
                              str(tmp_path / "small.bin"))[0] == 100_000
    assert (tmp_path / "small.bin").read_bytes() == b"S" * 100_000


def test_a_socket_timeout_on_the_shared_sftp_does_not_wait_to_close_it(
        ssh, monkeypatch):
    """The timed-out client is dropped without anyone waiting on its close (on
    a stalled link the close waits for room in the send buffer)."""
    shared = ssh.sftp()
    gate = threading.Event()
    monkeypatch.setattr(shared, "close", lambda: gate.wait(30))
    t0 = time.monotonic()
    ssh._drop_shared_sftp(only=shared)
    assert time.monotonic() - t0 < 5 and ssh._sftp is None
    gate.set()
