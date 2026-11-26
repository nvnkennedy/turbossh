"""2.7.0: the "none" login, cancelling every blocking call, commands in a login
shell (QNX), raw channels, transport tuning, an SFTP start that cannot hang,
and a login check that says what happened — against loopback servers."""

from __future__ import annotations

import os
import socket
import threading
import time

import paramiko
import pytest

from turbossh import SSHCancelledError, SSHConfig, SSHHandler
from turbossh.exceptions import (SSHAuthenticationError, SSHConnectionError,
                                 SSHTimeoutError)


def _event_after(seconds: float) -> threading.Event:
    event = threading.Event()
    timer = threading.Timer(seconds, event.set)
    timer.daemon = True
    timer.start()
    return event


def _handler(make_config, **overrides) -> SSHHandler:
    return SSHHandler(make_config(**overrides), quiet=True)


# --------------------------------------------------------------------------- #
# 1. the "none" login
# --------------------------------------------------------------------------- #
def test_a_server_that_takes_only_none_logs_in_without_a_password(ssh_server, make_config):
    server = ssh_server.server
    server.allowed_auths, server.none_ok = "none", True
    h = _handler(make_config, password=None)
    try:
        h.connect()
        assert h.auth_method == "none"
        assert h.run("echo hi").ok
        assert server.auth_log == ["<none>"]        # nothing else was tried
        assert ssh_server.accepted == 1
    finally:
        h.disconnect()


def test_none_comes_first_where_a_password_would_also_do(ssh_server, make_config):
    server = ssh_server.server
    server.allowed_auths, server.none_ok = "none,password", True
    h = _handler(make_config, password="")
    try:
        h.connect()
        assert h.auth_method == "none" and server.auth_log == ["<none>"]
    finally:
        h.disconnect()


def test_a_blank_password_server_is_logged_in_on_the_same_connection(ssh_server,
                                                                       make_config):
    server = ssh_server.server
    server.allowed_auths = "password"
    h = _handler(make_config, username="blank", password=None)
    try:
        h.connect()
        assert h.auth_method == "empty-password"
        assert server.auth_log == ["<none>", ""]
        assert ssh_server.accepted == 1               # no second connection
    finally:
        h.disconnect()


def test_a_method_the_server_does_not_offer_is_skipped(ssh_server, make_config):
    """The answer to "none" lists what the server allows: no blank password
    is sent to a key-only server; the error says what it offers."""
    server = ssh_server.server
    server.allowed_auths = "publickey"
    h = _handler(make_config, password=None)
    with pytest.raises(SSHAuthenticationError) as caught:
        h.connect()
    assert "publickey" in str(caught.value)
    assert server.auth_log == ["<none>"]
    assert ssh_server.accepted == 1


def test_a_password_login_still_works_and_says_so(ssh_server, ssh):
    assert ssh.auth_method == "password"
    assert ssh_server.server.auth_log == ["good"]   # no "none" probe first


def test_a_refused_password_falls_back_to_none_where_the_server_lists_it(
        ssh_server, make_config):
    server = ssh_server.server
    server.allowed_auths, server.none_ok = "none", True
    h = _handler(make_config, password="wrong")
    try:
        h.connect()
        assert h.auth_method == "none" and ssh_server.accepted == 1
    finally:
        h.disconnect()


def test_none_can_be_switched_off(ssh_server, make_config):
    server = ssh_server.server
    server.allowed_auths = "password"
    h = _handler(make_config, username="blank", password=None, allow_none_auth=False)
    try:
        h.connect()
        assert h.auth_method == "empty-password" and "<none>" not in server.auth_log
    finally:
        h.disconnect()


def test_a_key_login_says_key(ssh_server, make_config, tmp_path):
    key = paramiko.RSAKey.generate(2048)
    path = tmp_path / "id_test"
    key.write_private_key_file(str(path))
    ssh_server.server.accepted_key = key
    h = _handler(make_config, password=None, key_filename=str(path))
    try:
        h.connect()
        assert h.auth_method == "key"
    finally:
        h.disconnect()


def test_max_retries_one_is_one_attempt_with_no_wait(make_config, monkeypatch):
    from turbossh import core
    tries = []

    def refused(*_a, **_k):
        tries.append(1)
        raise ConnectionRefusedError(10061, "refused")
    monkeypatch.setattr(core, "_tcp_connect", refused)
    h = _handler(make_config, max_retries=1, retry_backoff=5.0)
    start = time.monotonic()
    with pytest.raises(SSHConnectionError):
        h.connect()
    assert tries == [1] and time.monotonic() - start < 1.0


# --------------------------------------------------------------------------- #
# 2. cancel
# --------------------------------------------------------------------------- #
def test_a_running_command_stops_at_once_and_the_connection_stays(ssh):
    start = time.monotonic()
    result = ssh.run("partial-hang", cancel=_event_after(0.8))
    assert time.monotonic() - start < 2.5
    assert result.cancelled and result.exit_code == -1 and not result.ok
    assert "before" in result.stdout                  # what had arrived
    assert ssh.run("echo again").ok                   # same connection


def test_cancel_may_be_a_callable(ssh):
    stop_at = time.monotonic() + 0.5
    result = ssh.run("hang", cancel=lambda: time.monotonic() > stop_at)
    assert result.cancelled


def test_a_cancelled_command_with_check_raises(ssh):
    with pytest.raises(SSHCancelledError) as caught:
        ssh.run("hang", check=True, cancel=_event_after(0.3))
    assert caught.value.result.cancelled


def test_a_stream_stops_on_cancel(ssh):
    start = time.monotonic()
    out = ssh.stream("partial-hang", cancel=_event_after(0.8))
    assert out["cancelled"] and out["lines"] == 1
    assert time.monotonic() - start < 2.5


def test_read_until_stops_on_cancel(ssh):
    shell = ssh.open_shell()
    start = time.monotonic()
    res = shell.read_until("never-comes", timeout=30, cancel=_event_after(0.5))
    assert res.cancelled and not res and time.monotonic() - start < 2.0
    assert shell.channel.closed


def test_a_cancelled_pull_leaves_no_partial_file(ssh_server, ssh, tmp_path):
    (ssh_server.root / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024))
    target = tmp_path / "out" / "big.bin"
    seen = []
    with pytest.raises(SSHCancelledError):
        ssh.pull("big.bin", str(target), callback=lambda d, t: seen.append(d),
                 cancel=lambda: bool(seen))
    assert not target.exists()
    assert not [p for p in target.parent.iterdir()] if target.parent.exists() else True
    assert ssh.run("echo still").ok
    ssh.pull("big.bin", str(target))                 # and the SFTP client too
    assert target.stat().st_size == 3 * 1024 * 1024


def test_a_cancelled_push_leaves_the_remote_file_as_it_was(ssh_server, ssh, tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(os.urandom(3 * 1024 * 1024))
    (ssh_server.root / "big.bin").write_bytes(b"old")
    seen = []
    with pytest.raises(SSHCancelledError):
        ssh.push(str(source), "big.bin", callback=lambda d, t: seen.append(d),
                 cancel=lambda: bool(seen))
    assert (ssh_server.root / "big.bin").read_bytes() == b"old"
    assert sorted(p.name for p in ssh_server.root.iterdir()) == ["big.bin"]


def test_an_already_cancelled_transfer_does_nothing(ssh_server, ssh, tmp_path):
    (ssh_server.root / "a.txt").write_text("a")
    done = threading.Event()
    done.set()
    with pytest.raises(SSHCancelledError):
        ssh.pull("a.txt", str(tmp_path / "a.txt"), cancel=done)
    assert not (tmp_path / "a.txt").exists()


class _Silent:
    """A TCP server that takes connections and says nothing (an SSH server
    still starting); ``reset=True``: it closes each one at once."""

    def __init__(self, reset=False):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(20)
        self.port = self.sock.getsockname()[1]
        self.clients = []
        self.reset = reset
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            if self.reset:
                client.close()
            else:
                self.clients.append(client)

    def close(self):
        for s in [self.sock, *self.clients]:
            try:
                s.close()
            except OSError:
                pass


def test_connect_stops_on_cancel_during_the_handshake(make_config):
    silent = _Silent()
    try:
        h = _handler(make_config, port=silent.port, banner_timeout=30, max_retries=3)
        start = time.monotonic()
        with pytest.raises(SSHCancelledError):
            h.connect(cancel=_event_after(0.5))
        assert time.monotonic() - start < 2.0 and not h.is_connected
    finally:
        silent.close()


# --------------------------------------------------------------------------- #
# 3. a command in an interactive login shell (QNX)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def qnx(ssh_server, make_config):
    ssh_server.server.qnx = True
    handlers = []

    def make(**overrides):
        h = _handler(make_config, **overrides)
        handlers.append(h)
        return h
    yield make
    for h in handlers:
        h.disconnect()


def test_run_login_gives_the_output_and_exit_code(qnx):
    h = qnx()
    r = h.run_login("echo hello")
    assert (r.exit_code, r.stdout.strip()) == (0, "hello")
    assert "NDK" not in r.stdout and "#" not in r.stdout
    assert h.run_login("false").exit_code == 1


def test_run_login_takes_several_lines_and_environment(qnx):
    r = qnx().run_login("echo one\necho two", environment={"A": "b c"})
    assert r.ok and r.stdout.split() == ["one", "two"]


def test_plain_exec_cannot_start_programs_on_this_target(qnx):
    r = qnx().run("uname")
    assert r.exit_code == 126 and "cannot execute" in r.stderr


def test_command_mode_login_runs_through_the_login_shell(qnx):
    h = qnx(command_mode="login")
    r = h.run("uname")
    assert r.ok and r.stdout.strip() == "QNX" and h.command_way == "login"


def test_command_mode_auto_finds_the_way_once(qnx, ssh_server):
    h = qnx(command_mode="auto")
    assert h.command_way is None
    assert h.run("uname").stdout.strip() == "QNX"
    assert h.command_way == "login"
    execs = list(ssh_server.server.execs)
    assert h.run("echo again").stdout.strip() == "again"
    assert ssh_server.server.execs == execs           # no new probe


def test_command_mode_auto_keeps_exec_where_it_works(ssh_server, make_config):
    h = _handler(make_config, command_mode="auto")
    try:
        assert h.run("echo hi").ok and h.command_way == "exec"
    finally:
        h.disconnect()


def test_a_login_shell_that_closes_early_raises(qnx):
    with pytest.raises(SSHConnectionError):
        qnx().run_login("quit-now")


def test_run_login_stops_on_cancel(qnx):
    h = qnx()
    start = time.monotonic()
    r = h.run_login("hang", cancel=_event_after(0.5))
    assert r.cancelled and time.monotonic() - start < 3.0
    assert h.run_login("echo ok").stdout.strip() == "ok"


def test_command_mode_is_checked():
    with pytest.raises(ValueError):
        SSHConfig(host="h", command_mode="shell")


# --------------------------------------------------------------------------- #
# 4. raw channels
# --------------------------------------------------------------------------- #
def test_open_channel_streams_raw_bytes(ssh):
    chan = ssh.open_channel("big")
    got = bytearray()
    try:
        while True:
            data = chan.recv(1 << 20)
            if not data:
                break
            got.extend(data)
        assert len(got) == 6 * 1024 * 1024 and chan.recv_exit_status() == 0
    finally:
        chan.close()


def test_exec_bytes_hands_over_every_byte(ssh):
    sizes = []
    r = ssh.exec_bytes("big", lambda data: sizes.append(len(data)))
    assert r.ok and sum(sizes) == 6 * 1024 * 1024 and r.stdout == ""


def test_exec_bytes_stops_on_cancel(ssh):
    r = ssh.exec_bytes("partial-hang", lambda data: None, cancel=_event_after(0.5))
    assert r.cancelled


# --------------------------------------------------------------------------- #
# 5. transport tuning
# --------------------------------------------------------------------------- #
def test_window_packet_and_nodelay_reach_the_connection(make_config):
    h = _handler(make_config, window_size=2 ** 26, max_packet_size=2 ** 15,
                 tcp_nodelay=True, keepalive_interval=5)
    try:
        h.connect()
        t = h.transport
        assert t.default_window_size == 2 ** 26 and t.default_max_packet_size == 2 ** 15
        assert t.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        chan = h.open_channel("echo x")
        assert chan.in_window_size == 2 ** 26
        chan.close()
        assert h.run("echo hi").ok
    finally:
        h.disconnect()


# --------------------------------------------------------------------------- #
# 6. SFTP that cannot hang
# --------------------------------------------------------------------------- #
def test_an_sftp_that_never_starts_fails_in_time_and_closes(ssh_server, ssh):
    ssh_server.sftp_silent = True
    ssh.disconnect()
    ssh.connect()
    start = time.monotonic()
    with pytest.raises(SSHTimeoutError) as caught:
        ssh.open_sftp(timeout=1.0)
    assert time.monotonic() - start < 4.0
    assert "SFTP did not start within 1 s" in str(caught.value)
    assert not ssh.is_connected                        # the connection was closed
    ssh_server.sftp_silent = False
    assert ssh.run("echo back").ok                    # and comes back


def test_the_shared_sftp_has_the_same_guard(ssh_server, make_config):
    ssh_server.sftp_silent = True
    h = _handler(make_config, sftp_start_timeout=1.0)
    try:
        h.connect()
        with pytest.raises(SSHTimeoutError):
            h.listdir(".")
    finally:
        h.disconnect()


# --------------------------------------------------------------------------- #
# 7. check(): what happened
# --------------------------------------------------------------------------- #
def test_check_ok_with_the_banner_and_leaves_the_connection_alone(ssh):
    before = ssh.client
    out = ssh.check(timeout=5)
    assert out["ok"] and out["kind"] == "ok" and out["banner"].startswith("SSH-")
    assert out["seconds"] >= 0 and out["error"] == ""
    assert ssh.client is before and ssh.run("echo hi").ok


def test_check_refused(make_config):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    # (Windows retries the SYN for about 2 s before it reports a refusal)
    out = _handler(make_config, port=port).check(timeout=6)
    assert out["kind"] == "refused" and not out["ok"]


def test_check_banner_timeout(make_config):
    silent = _Silent()
    try:
        out = _handler(make_config, port=silent.port).check(timeout=1)
        assert out["kind"] == "banner-timeout"
    finally:
        silent.close()


def test_check_reset(make_config):
    closing = _Silent(reset=True)
    try:
        out = _handler(make_config, port=closing.port).check(timeout=2)
        assert out["kind"] == "reset", out
    finally:
        closing.close()


def test_check_auth_failed(make_config):
    out = _handler(make_config, password="wrong").check(timeout=5)
    assert out["kind"] == "auth-failed" and out["banner"].startswith("SSH-")


def test_check_accepts_lines_before_the_greeting(ssh_server, make_config):
    ssh_server.pre_banner = b"Welcome to the ECU\r\nAuthorised use only\r\n"
    out = _handler(make_config).check(timeout=5)
    assert out["kind"] == "ok", out


@pytest.mark.parametrize("raised, kind", [
    (socket.timeout("timed out"), "timeout"),
    (OSError(113, "No route to host"), "unreachable"),
    (OSError(10065, "A socket operation was attempted to an unreachable host"),
     "unreachable"),
])
def test_check_timeout_and_unreachable(make_config, monkeypatch, raised, kind):
    from turbossh import core

    def fail(*_a, **_k):
        raise raised
    monkeypatch.setattr(core, "_tcp_connect", fail)
    assert _handler(make_config).check(timeout=1)["kind"] == kind


def test_check_stops_on_cancel(make_config):
    silent = _Silent()
    try:
        start = time.monotonic()
        out = _handler(make_config, port=silent.port).check(timeout=20,
                                                            cancel=_event_after(0.4))
        assert out["kind"] == "cancelled" and time.monotonic() - start < 2.0
    finally:
        silent.close()


def test_check_says_none_for_a_head_unit(ssh_server, make_config):
    ssh_server.server.allowed_auths, ssh_server.server.none_ok = "none", True
    assert _handler(make_config, password=None).check(timeout=5)["ok"]


def test_a_bad_cancel_is_refused(ssh):
    with pytest.raises(TypeError):
        ssh.run("echo hi", cancel=42)


# --------------------------------------------------------------------------- #
# review fixes
# --------------------------------------------------------------------------- #
def test_no_user_name_logs_in_as_this_computers_user(ssh_server, make_config):
    """A blank User field (username=None): Paramiko's connect used this
    computer's user name; the "none" login crashed on None."""
    ssh_server.server.allowed_auths, ssh_server.server.none_ok = "none", True
    h = _handler(make_config, username=None, password=None)
    try:
        h.connect()
        assert h.auth_method == "none"
        assert _handler(make_config, username=None, password=None).check(5)["ok"]
    finally:
        h.disconnect()


@pytest.mark.parametrize("have_keys, order", [
    (True, ["<none>", "keys", ""]),        # keys before a blank password
    (False, ["<none>", ""]),               # no keys: the blank password at once
])
def test_keys_come_before_a_blank_password_when_there_are_any(
        ssh_server, make_config, monkeypatch, have_keys, order):
    server = ssh_server.server
    h = _handler(make_config, username="blank", password=None, allow_agent=True)
    monkeypatch.setattr(h, "_have_keys", lambda: have_keys)
    monkeypatch.setattr(h, "_paramiko_auth",
                        lambda client, user: server.auth_log.append("keys"))
    try:
        h.connect()
        assert h.auth_method == "empty-password" and server.auth_log == order
    finally:
        h.disconnect()


def _disconnect_all(ssh_server):
    """What OpenSSH does at MaxAuthTries: SSH_MSG_DISCONNECT, then close."""
    from paramiko.common import cMSG_DISCONNECT
    for tr in list(ssh_server.transports):
        m = paramiko.Message()
        m.add_byte(cMSG_DISCONNECT)
        m.add_int(2)
        m.add_string("Too many authentication failures")
        m.add_string("")
        try:
            tr._send_message(m)
        except Exception:
            pass
        tr.close()


def test_a_server_that_hangs_up_after_a_refusal_is_a_login_failure(ssh_server,
                                                                   make_config):
    server = ssh_server.server
    server.allowed_auths = "password,publickey"
    server.on_refusal = lambda: _disconnect_all(ssh_server)
    h = _handler(make_config, password=None, allow_agent=True)
    h._have_keys = lambda: False
    with pytest.raises(SSHAuthenticationError) as caught:
        h.connect()
    assert "closed the connection" in str(caught.value)
    assert ssh_server.accepted == 1                  # not retried as a network error


def test_command_mode_auto_asks_again_after_the_target_was_not_there(make_config):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    h = _handler(make_config, port=port, command_mode="auto", connect_timeout=2)
    with pytest.raises((SSHConnectionError, SSHTimeoutError)):   # (Windows: ~2 s to refuse)
        h.run("uname")
    assert h.command_way is None                      # nothing remembered


def test_a_connect_to_a_silent_address_stops_on_cancel():
    from turbossh import core
    watch = core._CancelWatch(_event_after(0.3))
    start = time.monotonic()
    with watch, pytest.raises(OSError):
        core._tcp_connect("10.255.255.1", 22, 10, watch=watch)
    assert time.monotonic() - start < 2.0


def test_run_login_parses_the_command_with_its_end_marker(qnx, ssh_server):
    """A command that reads its input must not get the end marker: the { }
    group ends on the marker's own line, read before anything runs."""
    r = qnx().run_login("echo x\necho echo x")
    assert r.stdout.split("\n")[:2] == ["x", "echo x"]   # output like a sent line kept
    import re
    sent = ssh_server.server.shell_lines
    assert re.search(r"'\); echo NDK''END[0-9a-f]{8}:\$\?; exit$", sent[-1])
    assert any(re.match(r"echo NDK''BEGIN[0-9a-f]{8}; \(eval '", line) for line in sent)


def test_on_bytes_errors_pass_through(ssh):
    def full(_data):
        raise OSError(28, "No space left on device")
    with pytest.raises(OSError) as caught:
        ssh.exec_bytes("big", full)
    assert caught.value.errno == 28


@pytest.mark.parametrize("call", ["run", "stream", "run_login", "exec_bytes"])
def test_a_bad_cancel_starts_nothing(ssh_server, ssh, call):
    with pytest.raises(TypeError):
        if call == "exec_bytes":
            ssh.exec_bytes("hang", lambda d: None, cancel=42)
        else:
            getattr(ssh, call)("hang", cancel=42)
    time.sleep(0.3)
    assert "hang" not in ssh_server.server.execs


def test_check_lets_ctrl_c_through(make_config, monkeypatch):
    from turbossh import core

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(core, "_tcp_connect", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _handler(make_config).check(timeout=1)


def test_a_cancelled_push_on_a_slow_link_still_tidies_up(ssh_server, ssh, tmp_path):
    """The call that noticed the cancel removes its temporary file over the
    channel: the watcher must not close that under it."""
    ssh_server.server.sftp_delay = 0.3
    source = tmp_path / "big.bin"
    source.write_bytes(os.urandom(3 * 1024 * 1024))
    (ssh_server.root / "big.bin").write_bytes(b"old")
    seen = []
    with pytest.raises(SSHCancelledError):
        ssh.push(str(source), "big.bin", callback=lambda d, t: seen.append(d),
                 cancel=lambda: bool(seen))
    assert sorted(p.name for p in ssh_server.root.iterdir()) == ["big.bin"]
    assert (ssh_server.root / "big.bin").read_bytes() == b"old"


def _shells():
    import shutil
    found = []
    for name in ("sh", "dash", "bash", "ksh", "mksh"):
        path = shutil.which(name)
        if path and path not in found:
            found.append(path)
    return found


@pytest.mark.skipif(not _shells(), reason="no POSIX shell here")
@pytest.mark.parametrize("shell", _shells() or ["none"])
@pytest.mark.parametrize("command, code, output", [
    ("echo hi", 0, "hi"),
    ("fi", None, None),                   # a syntax error: the marker still comes
    ("echo $((1/0))", None, None),
    ("exit 3", 3, ""),                    # ends the command, not the shell
    ("set -e; false; echo not here", 1, ""),
    ("# only a comment", 0, ""),
    ("cat <<EOF\nbody }\nEOF", 0, "body }"),
    ("printf 'a b'", 0, "a b"),
    ("printf 'NDKEND:5\\n'; echo real", 0, "NDKEND:5\nreal"),   # an old marker
])
def test_the_login_script_survives_what_a_real_shell_makes_of_it(shell, command, code,
                                                                 output):
    """The script run_login sends, through real interactive shells (dash, ash
    and ksh drop the rest of a line after a syntax error in eval): the end
    marker always comes, with the command's status."""
    import re
    import subprocess
    tag = "0a1b2c3d"
    script = SSHHandler._login_script(command, {"A": "it's"}, tag) + "\n"
    done = subprocess.run([shell, "-i"], input=script.encode(), capture_output=True,
                          timeout=20)
    text = done.stdout.decode(errors="replace").replace("\r", "")
    end = re.search(r"NDKEND" + tag + r":(\d+)\n", text)
    assert end is not None, (shell, text, done.stderr[-300:])
    status = int(end.group(1))
    if code is None:
        assert status != 0
    else:
        assert status == code
        body = text.split("NDKBEGIN" + tag + "\n", 1)[1].split("NDKEND" + tag, 1)[0]
        assert body.strip() == output


def test_environment_names_are_checked():
    with pytest.raises(ValueError):
        SSHHandler._login_script("true", {"BAD NAME": "x"})


def test_the_keys_using_up_the_tries_leave_room_for_the_blank_password(
        ssh_server, make_config, monkeypatch):
    """An agent and ~/.ssh holding the same keys can use up MaxAuthTries before
    the blank password comes: one more connection, the blank password alone."""
    server = ssh_server.server
    server.allowed_auths = "password,publickey"
    h = _handler(make_config, username="blank", password=None, allow_agent=True)
    monkeypatch.setattr(h, "_have_keys", lambda: True)
    calls = []

    def too_many(client, user):
        calls.append(1)
        _disconnect_all(ssh_server)
        time.sleep(0.2)
    monkeypatch.setattr(h, "_paramiko_auth", too_many)
    try:
        h.connect()
        assert h.auth_method == "empty-password"
        assert calls == [1] and ssh_server.accepted == 2
        # the second connection knew the methods: no "none" there again
        assert server.auth_log.count("<none>") == 1
    finally:
        h.disconnect()


def test_after_a_dropped_link_the_keys_are_tried_again(ssh_server, make_config,
                                                       monkeypatch):
    """Paramiko cannot tell MaxAuthTries from a dropped link (an SSH server
    restarting): the second connection tries the keys again after the blank
    password, so a key user is still logged in."""
    server = ssh_server.server
    server.allowed_auths = "password,publickey"
    h = _handler(make_config, username="x", password=None, allow_agent=True)
    monkeypatch.setattr(h, "_have_keys", lambda: True)
    calls = []

    def first_drops(client, user):
        calls.append(1)
        if len(calls) == 1:
            for tr in list(ssh_server.transports):
                tr.close()
            time.sleep(0.2)
            return
        server.none_ok = True                     # stands in for a key the server takes
        client.get_transport().auth_none("x")
    monkeypatch.setattr(h, "_paramiko_auth", first_drops)
    try:
        h.connect()
        assert len(calls) == 2 and ssh_server.accepted == 2
    finally:
        h.disconnect()


def test_check_reads_what_the_jump_host_said():
    from turbossh import core
    refused = "Secsh channel 1 open FAILED: Connection refused: Connect failed"
    timed = "Secsh channel 1 open FAILED: Connection timed out: Connect failed"
    failed = paramiko.ChannelException(2, "Connect failed")
    assert core.SSHHandler._check_kind(failed, "", refused) == "refused"
    assert core.SSHHandler._check_kind(failed, "", timed) == "timeout"
    assert core.SSHHandler._check_kind(failed, "", "") == "unreachable"
    prohibited = paramiko.ChannelException(1, "Administratively prohibited")
    assert core.SSHHandler._check_kind(prohibited, "", "") == "error"
    unknown = "Secsh channel 1 open FAILED: Name or service not known: Connect failed"
    assert core.SSHHandler._check_kind(failed, "", unknown) == "error"
    name, reasons = core._quiet_paramiko_log()
    import logging
    logging.getLogger(name).error(refused)
    assert "Connection refused" in reasons.take() and reasons.take() == ""


def test_a_key_paramiko_cannot_read_does_not_stop_the_blank_password(
        ssh_server, make_config, monkeypatch):
    server = ssh_server.server
    server.allowed_auths = "password,publickey"
    h = _handler(make_config, username="blank", password=None, allow_agent=True)
    monkeypatch.setattr(h, "_have_keys", lambda: True)

    def unreadable(client, user):
        raise paramiko.SSHException("not a valid RSA private key file")
    monkeypatch.setattr(h, "_paramiko_auth", unreadable)
    try:
        h.connect()
        assert h.auth_method == "empty-password"
    finally:
        h.disconnect()


def test_check_through_a_jump_host_says_why(make_config, monkeypatch):
    from turbossh import core
    h = _handler(make_config)
    cases = [
        (paramiko.SSHException("Timeout opening channel."), "timeout"),
        (SSHTimeoutError("Timed out connecting to jump"), "timeout"),
        (paramiko.SSHException("the connection was lost during the login"), "reset"),
    ]
    for raised, kind in cases:
        assert core.SSHHandler._check_kind(raised, "") == kind, raised
    wrapped = SSHConnectionError("Could not connect to jump")
    wrapped.__cause__ = ConnectionRefusedError(111, "refused")
    assert core.SSHHandler._check_kind(wrapped, "") == "refused"
    assert h is not None


def test_an_on_bytes_error_comes_out_exactly_as_raised(ssh):
    class Stop(Exception):
        pass

    def stop(_data):
        try:
            raise KeyError("inner")
        except KeyError as exc:
            raise Stop("enough") from exc
    with pytest.raises(Stop) as caught:
        ssh.exec_bytes("big", stop)
    assert isinstance(caught.value.__cause__, KeyError)
