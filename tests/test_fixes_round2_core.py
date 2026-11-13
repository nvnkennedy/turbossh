"""Regression tests for the second core review (2.1.0): what the first fix pass
changed that 2.0.0 users depend on — the transfer policy shared by push / pull,
the file browser and the remote editor (owner, hard links, devices, folders that
are not writable, no room for two copies, symlinks, temporary names), a
disconnect() that no longer blocks reuse, connect() after a target reboot, the
serial port lifecycle (live bridges, the force kill's own parent, busy drivers,
newline translations, the POSIX PID folder), SFTP timeouts, camera command lines
and camera clean-up hints."""

from __future__ import annotations

import base64
import errno
import logging
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

import sshserver
from turbossh import SSHConfig, SSHHandler, SSHNotConnectedError, SSHTransferError
from turbossh import _transfer, core
from turbossh.exceptions import SSHConnectionError


def decode_ps(cmd: str) -> str:
    b64 = cmd.split("-EncodedCommand", 1)[1].split()[0]
    return base64.b64decode(b64).decode("utf-16-le")


def leftovers(folder):
    """Temporary transfer files left in *folder* — any naming scheme."""
    return sorted(n for n in os.listdir(folder)
                  if n.endswith((_transfer.TEMP_SUFFIX, _transfer.ASIDE_SUFFIX,
                                 ".turbossh-part")) or ".part-" in n)


@pytest.fixture()
def sftp(ssh):
    client = ssh.open_sftp()
    yield client
    client.close()


@pytest.fixture()
def requests_seen(monkeypatch):
    """Every SFTP request the loopback server handles, by type."""
    seen = []
    original = paramiko.SFTPServer._process

    def spy(self, t, request_number, msg):
        seen.append(t)
        return original(self, t, request_number, msg)
    monkeypatch.setattr(paramiko.SFTPServer, "_process", spy)
    return seen


class _LinkedAttr(paramiko.SFTPAttributes):
    """A listing entry whose ls-style longname carries a real link count (the
    stub server's always says 1)."""
    nlink = 1

    def __str__(self):
        return super().__str__().replace("   1 ", f"   {self.nlink} ", 1)


def _report_link_counts(monkeypatch):
    original = sshserver.StubSFTP.list_folder

    def list_folder(self, path):
        out = original(self, path)
        if isinstance(out, int):
            return out
        real = self._real(path)
        linked = []
        for attr in out:
            entry = _LinkedAttr()
            entry.__dict__.update(attr.__dict__)
            entry.nlink = os.lstat(os.path.join(real, attr.filename)).st_nlink
            linked.append(entry)
        return linked
    monkeypatch.setattr(sshserver.StubSFTP, "list_folder", list_folder)


def _pretend(monkeypatch, path, **fields):
    """Make the stub server report *fields* (st_uid, st_mode, …) for *path*."""
    for name in ("lstat", "stat"):
        original = getattr(sshserver.StubSFTP, name)

        def patched(self, p, _original=original):
            attr = _original(self, p)
            if p == path and not isinstance(attr, int):
                for key, value in fields.items():
                    setattr(attr, key, value)
            return attr
        monkeypatch.setattr(sshserver.StubSFTP, name, patched)


def _hard_link_pair(root):
    (root / "cfg").mkdir()
    (root / "cfg" / "app.conf").write_bytes(b"OLD")
    try:
        os.link(str(root / "cfg" / "app.conf"), str(root / "cfg" / "twin.conf"))
    except (OSError, NotImplementedError):
        pytest.skip("this file system cannot make hard links")


# ===================================================================== A
# ------------------------------------------------- hard links: in place (2.0)
def test_push_over_a_hard_linked_file_writes_it_in_place(ssh, ssh_server, tmp_path,
                                                         monkeypatch):
    _report_link_counts(monkeypatch)
    _hard_link_pair(ssh_server.root)
    src = tmp_path / "app.conf"
    src.write_bytes(b"NEW CONTENT")
    said = []
    ssh._log_callback = said.append
    ssh.push(str(src), "/cfg/app.conf")
    # both names are still one file: a rename would have split them
    assert (ssh_server.root / "cfg" / "twin.conf").read_bytes() == b"NEW CONTENT"
    assert ssh_server.server.rename_calls == []
    assert any("in place" in m and "2 hard links" in m for m in said), said
    assert leftovers(ssh_server.root / "cfg") == []


def test_a_folder_push_reads_link_counts_from_its_one_listing(ssh, ssh_server, tmp_path,
                                                              monkeypatch):
    _report_link_counts(monkeypatch)
    _hard_link_pair(ssh_server.root)
    tree = tmp_path / "cfg"
    tree.mkdir()
    (tree / "app.conf").write_bytes(b"FROM THE TREE")
    ssh.push(str(tree), "/cfg", recursive=True)
    assert (ssh_server.root / "cfg" / "twin.conf").read_bytes() == b"FROM THE TREE"


def test_browser_upload_over_a_hard_linked_file_writes_it_in_place(
        ssh, ssh_server, sftp, tmp_path, monkeypatch):
    pytest.importorskip("PyQt5")
    from turbossh.gui import sftp_browser as sb
    _report_link_counts(monkeypatch)
    _hard_link_pair(ssh_server.root)
    src = tmp_path / "app.conf"
    src.write_bytes(b"NEW")
    how = sb.put_atomic(sftp, str(src), "/cfg/app.conf")
    assert (ssh_server.root / "cfg" / "twin.conf").read_bytes() == b"NEW"
    assert "hard links" in how


def test_editor_save_of_a_hard_linked_file_writes_it_in_place(ssh, ssh_server, sftp,
                                                              monkeypatch):
    pytest.importorskip("PyQt5")
    from turbossh.gui import remote_edit
    _report_link_counts(monkeypatch)
    _hard_link_pair(ssh_server.root)
    size, _stamp, how = remote_edit.write_remote_text(sftp, "/cfg/app.conf", b"EDITED")
    assert size == 6 and "in place" in how
    assert (ssh_server.root / "cfg" / "twin.conf").read_bytes() == b"EDITED"


# ------------------------------------------------- not a regular file
def test_push_to_a_device_never_replaces_it(ssh, ssh_server, tmp_path, monkeypatch):
    """push(x, "/dev/null") must write through the device, not rename a regular
    file over it (the stub reports a character device for a plain file)."""
    (ssh_server.root / "null").write_bytes(b"")
    _pretend(monkeypatch, "/null", st_mode=stat.S_IFCHR | 0o666)
    src = tmp_path / "x.bin"
    src.write_bytes(b"discard me")
    ssh.push(str(src), "/null")
    assert ssh_server.server.rename_calls == [], "the device was replaced"
    assert (ssh_server.root / "null").read_bytes() == b"discard me"   # written through
    assert leftovers(ssh_server.root) == []


# ------------------------------------------------- owner and group
def _record_setstat(monkeypatch, *, refuse_chown=False):
    calls = []

    def chattr(self, path, attr):
        owner = bool(attr._flags & attr.FLAG_UIDGID)
        calls.append((path, attr.st_uid if owner else None,
                      attr.st_gid if owner else None,
                      attr.st_mode if attr._flags & attr.FLAG_PERMISSIONS else None))
        if owner and refuse_chown:
            return paramiko.SFTP_PERMISSION_DENIED
        return paramiko.SFTP_OK
    monkeypatch.setattr(sshserver.StubSFTP, "chattr", chattr)

    def fstat(self):                     # a new file of ours: root's
        attr = paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        attr.st_uid = attr.st_gid = 0
        attr.st_mode = stat.S_IFREG | 0o644
        return attr
    monkeypatch.setattr(sshserver._Handle, "stat", fstat)
    return calls


def test_replacing_a_file_keeps_its_owner_group_and_mode(ssh, ssh_server, tmp_path,
                                                         monkeypatch):
    """A root push over a service's 0600 config used to leave it root-owned."""
    (ssh_server.root / "svc.conf").write_bytes(b"OLD")
    _pretend(monkeypatch, "/svc.conf", st_uid=1000, st_gid=1001,
             st_mode=stat.S_IFREG | 0o600)
    calls = _record_setstat(monkeypatch)
    src = tmp_path / "svc.conf"
    src.write_bytes(b"NEW")
    ssh.push(str(src), "/svc.conf")
    assert (ssh_server.root / "svc.conf").read_bytes() == b"NEW"
    tmp = _transfer.remote_temp("/svc.conf")
    assert (tmp, 1000, 1001, None) in calls, calls
    assert (tmp, None, None, 0o600) in calls, calls
    assert ssh_server.server.rename_calls == ["posix_rename"]


def test_a_refused_chown_writes_the_file_in_place(ssh, ssh_server, tmp_path, monkeypatch):
    (ssh_server.root / "svc.conf").write_bytes(b"OLD")
    _pretend(monkeypatch, "/svc.conf", st_uid=1000, st_gid=1001,
             st_mode=stat.S_IFREG | 0o600)
    _record_setstat(monkeypatch, refuse_chown=True)
    src = tmp_path / "svc.conf"
    src.write_bytes(b"NEW, OWNER KEPT")
    said = []
    ssh._log_callback = said.append
    ssh.push(str(src), "/svc.conf")
    assert (ssh_server.root / "svc.conf").read_bytes() == b"NEW, OWNER KEPT"
    assert ssh_server.server.rename_calls == [], "renamed: the owner was lost"
    assert leftovers(ssh_server.root) == []
    assert any("in place" in m and "owner" in m for m in said), said


# ------------------------------------------------- the folder is not writable
def _refuse_new_files(monkeypatch, allowed):
    original = sshserver.StubSFTP.open

    def open_(self, path, flags, attr):
        if flags & (os.O_WRONLY | os.O_RDWR) and path != allowed:
            return paramiko.SFTP_PERMISSION_DENIED
        return original(self, path, flags, attr)
    monkeypatch.setattr(sshserver.StubSFTP, "open", open_)


def test_browser_upload_into_a_read_only_folder_writes_the_file_in_place(
        ssh, ssh_server, sftp, tmp_path, monkeypatch):
    pytest.importorskip("PyQt5")
    from turbossh.gui import sftp_browser as sb
    (ssh_server.root / "etc").mkdir()
    (ssh_server.root / "etc" / "hosts").write_bytes(b"OLD")
    _refuse_new_files(monkeypatch, "/etc/hosts")
    src = tmp_path / "hosts"
    src.write_bytes(b"127.0.0.1 ecu")
    how = sb.put_atomic(sftp, str(src), "/etc/hosts")
    assert (ssh_server.root / "etc" / "hosts").read_bytes() == b"127.0.0.1 ecu"
    assert "not writable" in how


# ------------------------------------------------- no room for a second copy
def test_no_room_for_a_second_copy_writes_the_file_in_place(ssh, ssh_server, tmp_path,
                                                             monkeypatch):
    """ENOSPC is a plain "Failure" in SFTP v3. 2.0 truncated and rewrote."""
    target = ssh_server.root / "big.img"
    target.write_bytes(b"O" * 1000)
    real_target = ssh_server.server.sftp_root + "/big.img"
    original = paramiko.SFTPHandle.write

    def write(self, offset, data):
        if self.filename.replace("\\", "/") != real_target:
            return paramiko.SFTP_FAILURE         # the disk holds one copy only
        return original(self, offset, data)
    monkeypatch.setattr(paramiko.SFTPHandle, "write", write)
    src = tmp_path / "big.img"
    src.write_bytes(os.urandom(200_000))
    said = []
    ssh._log_callback = said.append
    ssh.push(str(src), "/big.img")
    assert target.read_bytes() == src.read_bytes()
    assert leftovers(ssh_server.root) == []
    assert any("no room" in m for m in said), said


# ------------------------------------------------- symlinks: the server resolves
def test_browser_upload_through_a_link_in_a_linked_folder(ssh, ssh_server, sftp,
                                                          tmp_path, monkeypatch):
    """/lnk -> a/b and a/b/f -> ../t.txt: f is a/t.txt. A lexical join of the
    link text made it /t.txt."""
    pytest.importorskip("PyQt5")
    from turbossh.gui import sftp_browser as sb
    monkeypatch.setattr(sshserver.StubSFTP, "readlink", lambda self, path: os.readlink(
        self._real(path)).replace("\\", "/"), raising=False)
    root = ssh_server.root
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "t.txt").write_bytes(b"OLD")
    try:
        os.symlink(str(root / "a" / "b"), str(root / "lnk"), target_is_directory=True)
        os.symlink(os.path.join("..", "t.txt"), str(root / "a" / "b" / "f"))
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs a privilege this account lacks")
    src = tmp_path / "f"
    src.write_bytes(b"NEW")
    sb.put_atomic(sftp, str(src), "/lnk/f")
    assert (root / "a" / "t.txt").read_bytes() == b"NEW"
    assert not (root / "t.txt").exists()
    assert os.path.islink(str(root / "a" / "b" / "f")), "the link became a file"


# ------------------------------------------------- temporary names
def test_a_stale_temporary_file_is_replaced_not_added_to(ssh, ssh_server, tmp_path):
    (ssh_server.root / "fw.bin").write_bytes(b"GOOD")
    stale = ssh_server.root / _transfer.temp_name("fw.bin")
    stale.write_bytes(b"left by a run whose link dropped")
    src = tmp_path / "fw.bin"
    src.write_bytes(b"NEW FIRMWARE")
    ssh.push(str(src), "/fw.bin")
    assert (ssh_server.root / "fw.bin").read_bytes() == b"NEW FIRMWARE"
    assert leftovers(ssh_server.root) == []
    assert _transfer.temp_name("fw.bin") == ".fw.bin.tsp"


def test_a_failed_push_leaves_at_most_one_temporary_file_per_target(
        ssh, ssh_server, tmp_path, monkeypatch):
    """A link that drops leaves the temporary file (nothing can remove it): the
    next attempts reuse that one name instead of adding one each (the camera's
    160 MB ffmpeg upload left one per failure)."""
    (ssh_server.root / "ffmpeg.exe").write_bytes(b"OLD")
    src = tmp_path / "ffmpeg.exe"
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
    for _ in range(3):
        with pytest.raises(SSHTransferError):
            ssh.push(str(src), "/ffmpeg.exe", callback=drop)
    assert leftovers(ssh_server.root) == [".ffmpeg.exe.tsp"]
    assert (ssh_server.root / "ffmpeg.exe").read_bytes() == b"OLD"
    ssh.push(str(src), "/ffmpeg.exe")
    assert leftovers(ssh_server.root) == []
    assert (ssh_server.root / "ffmpeg.exe").read_bytes() == src.read_bytes()


def test_a_name_too_long_for_the_temporary_suffix_still_uploads(ssh, ssh_server,
                                                                tmp_path):
    name = "n" * 250                                 # +".part-xxxxxx" > 255
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    ssh.push(str(src), "/" + name)
    assert (ssh_server.root / name).read_bytes() == b"payload"
    assert leftovers(ssh_server.root) == []
    long_tmp = _transfer.temp_name(name)
    assert len(long_tmp.encode()) <= 255 and long_tmp.endswith(".tsp")
    assert _transfer.temp_name(name) != _transfer.temp_name(name[:-1] + "m")


@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH is a Windows limit")
def test_local_temporary_names_stay_within_max_path(tmp_path):
    folder = str(tmp_path) + "\\" + "d" * max(1, 240 - len(str(tmp_path)))
    target = os.path.join(folder, "x" * (258 - len(folder)))
    assert len(target) == 259
    assert len(os.path.abspath(_transfer.local_temp(target))) <= 259


# ------------------------------------------------- round trips for new files
def test_many_small_new_files_cost_no_more_requests_than_in_2_0(ssh, ssh_server,
                                                                 tmp_path, requests_seen):
    """2.0 sent OPEN, CLOSE and a STAT (put's size check) per new file; the first
    fix added an LSTAT and a RENAME, +25-50 % on 5,000 small files at 80 ms."""
    tree = tmp_path / "many"
    tree.mkdir()
    n = 30
    for i in range(n):
        (tree / f"f{i}.txt").write_text(str(i))
    ssh.sftp()                                       # the handshake is not counted
    del requests_seen[:]
    ssh.push(str(tree), "/many", recursive=True)
    control = [t for t in requests_seen
               if t not in (paramiko.sftp.CMD_WRITE, paramiko.sftp.CMD_READ)]
    assert len(control) <= 3 * n + 6, len(control)
    assert sorted(os.listdir(ssh_server.root / "many")) == sorted(os.listdir(tree))


# ------------------------------------------------- downloads
def test_a_pull_over_a_read_only_file_leaves_no_temporary_file(ssh, ssh_server,
                                                               tmp_path):
    """copymode before the move made the temporary file read-only too: the move
    and its fallback failed, and the read-only temporary could not be removed."""
    (ssh_server.root / "r.txt").write_text("remote")
    local = tmp_path / "local"
    local.mkdir()
    target = local / "r.txt"
    target.write_text("local, read-only")
    os.chmod(str(target), stat.S_IREAD)
    try:
        try:
            ssh.pull("/r.txt", str(target))
        except SSHTransferError:
            pass                                     # Windows: read-only stays so
        assert sorted(os.listdir(local)) == ["r.txt"]
    finally:
        os.chmod(str(target), stat.S_IREAD | stat.S_IWRITE)


def test_pull_keeps_the_mode_of_the_file_it_replaces(ssh, ssh_server, tmp_path):
    (ssh_server.root / "tool").write_bytes(b"#!/bin/sh\n")
    target = tmp_path / "tool"
    target.write_bytes(b"old")
    os.chmod(str(target), 0o755 if os.name != "nt" else stat.S_IREAD | stat.S_IWRITE)
    before = stat.S_IMODE(os.stat(str(target)).st_mode)
    ssh.pull("/tool", str(target))
    assert target.read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE(os.stat(str(target)).st_mode) == before


@pytest.mark.skipif(not _transfer.GET_TAKES_PREFETCH_LIMIT,
                    reason="this Paramiko cannot limit prefetch")
def test_browser_download_bounds_prefetch(ssh, ssh_server, sftp, tmp_path, monkeypatch):
    """Unlimited prefetch of a multi-GB file over a slow VPN outlasted the 30 s
    SFTP timeout of the browser's client."""
    pytest.importorskip("PyQt5")
    from turbossh.gui import sftp_browser as sb
    (ssh_server.root / "big.log").write_bytes(b"x" * 100_000)
    seen = []
    original_get = paramiko.SFTPClient.get

    def spy(self, remote, local, **kw):
        seen.append(kw)
        return original_get(self, remote, local, **kw)
    monkeypatch.setattr(paramiko.SFTPClient, "get", spy)
    assert sb.get_atomic(sftp, "/big.log", str(tmp_path / "big.log")) == 100_000
    assert seen[-1].get("max_concurrent_prefetch_requests") == 64


def test_browser_download_over_a_held_file_writes_it_in_place(ssh, ssh_server, sftp,
                                                              tmp_path, monkeypatch):
    """Windows refuses the rename while another program holds the target open
    without delete sharing (a log viewer): core.pull copied in place, the browser
    failed."""
    pytest.importorskip("PyQt5")
    from turbossh.gui import sftp_browser as sb
    (ssh_server.root / "log.txt").write_bytes(b"remote log")
    local = tmp_path / "local"
    local.mkdir()
    target = local / "log.txt"
    target.write_bytes(b"open in a viewer")

    def held(src, dst):
        raise PermissionError(13, "in use by another process")
    monkeypatch.setattr(os, "replace", held)
    assert sb.get_atomic(sftp, "/log.txt", str(target)) == len(b"remote log")
    assert target.read_bytes() == b"remote log"
    assert sorted(os.listdir(local)) == ["log.txt"]


# ===================================================================== B
def test_a_disconnected_handler_reconnects_by_itself_as_in_2_0(make_config, ssh_server):
    """Scripts disconnect before a DUT power cycle and reuse the handler."""
    handler = SSHHandler(make_config(auto_reconnect=True), quiet=True)
    handler.connect()
    try:
        handler.disconnect()
        assert handler.run("echo").ok
        with handler:
            pass
        assert handler.run("echo").ok             # after a with block too
        handler.disconnect(sticky=True)            # disposing of it for good
        with pytest.raises(SSHNotConnectedError):
            handler.run("echo")
        handler.connect()
        assert handler.run("echo").ok
    finally:
        handler.disconnect()


def test_gui_teardown_disposes_of_a_handler_for_good(make_config, ssh_server):
    """A worker still running after its tab closed must not resurrect the
    connection the teardown closed."""
    pytest.importorskip("PyQt5")
    from turbossh.gui import qtutil, session_widgets
    handler = SSHHandler(make_config(auto_reconnect=True), quiet=True)
    handler.connect()
    session_widgets._close_handler(handler)
    assert qtutil.wait_for_teardowns(10)
    before = len(ssh_server.transports)
    with pytest.raises(SSHNotConnectedError):
        handler.run("echo")
    assert len(ssh_server.transports) == before


# ===================================================================== C
def test_connect_replaces_a_link_that_stopped_answering(make_config, ssh_server,
                                                        monkeypatch):
    """After a DUT reboot the old TCP link still looks open until something is
    sent; connect() must notice and open a fresh one (2.0 always did)."""
    monkeypatch.setattr(SSHHandler, "_LIVENESS_TIMEOUT", 1.0, raising=False)
    handler = SSHHandler(make_config(), quiet=True)
    handler.connect()
    release, asked = threading.Event(), []

    def silent(kind, msg):                # the first connection goes quiet
        asked.append(kind)
        if len(asked) == 1:
            release.wait(30)
        return False
    ssh_server.server.check_global_request = silent
    try:
        old = handler.client
        assert handler.is_connected                # it still looks connected
        t0 = time.monotonic()
        handler.connect()
        assert time.monotonic() - t0 < 10
        assert handler.client is not old
        assert not old.get_transport() or not old.get_transport().is_active(), \
            "the dead connection was leaked"
        assert handler.run("echo").ok
    finally:
        release.set()
        handler.disconnect()


def test_connect_keeps_a_live_link(ssh, ssh_server):
    before, client = len(ssh_server.transports), ssh.client
    t0 = time.monotonic()
    ssh.connect()
    assert time.monotonic() - t0 < 3
    assert ssh.client is client and len(ssh_server.transports) == before


# ===================================================================== D / E / G
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
    handler = SSHHandler(SSHConfig(host="target"), quiet=True)
    sent = []

    class Transport:
        def open_session(self, timeout=None):
            return _CaptureChan(sent)
    monkeypatch.setattr(handler, "_ensure_connected", lambda: None)
    monkeypatch.setattr(SSHHandler, "transport", property(lambda self: Transport()))
    return handler, sent


def test_a_stream_does_not_kill_a_live_bridge_on_its_port(ssh, ssh_server):
    """The tracked holder may be the GUI's serial tab: 2.0 failed with "cannot
    open the port" and the GUI kept working. Only force=True takes it."""
    ssh.serial_stream("COM5", timeout=10)
    src = decode_ps(ssh_server.server.execs[-2])
    assert "Stop-Process" not in src
    ssh.serial_stream("COM5", timeout=10, force=True)
    src = decode_ps(ssh_server.server.execs[-2])
    assert "Stop-Process -Id $op" in src and "TsOurs" in src


def test_force_kill_never_matches_its_own_ancestors_or_wrappers(monkeypatch):
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_bridge("COM5", force=True)
    src = decode_ps(sent[-1])
    assert "$global:tsAnc" in src and "ParentProcessId" in src
    assert "-EncodedCommand\\s+" + core._serial_ps_tag_b64() in src
    assert "Where-Object{TsOurs $_}" in src


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"),
                    reason="runs the Windows scripts for real")
def test_a_forced_bridge_does_not_kill_the_shell_that_started_it(monkeypatch, tmp_path):
    """Under a PowerShell default shell, sshd's wrapper ("powershell -c
    "powershell ... -EncodedCommand <tagged>"") carries the bridge's tag: the
    forced open killed it — its own parent."""
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_bridge("COM9387", force=True)      # a port no machine has
    marker = tmp_path / "wrapper-finished"
    inner = sent[-1]
    wrapper = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         f"{inner}; Set-Content -LiteralPath '{marker}' -Value done"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wrapper.wait(120)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
    assert marker.exists(), "the forced open killed the shell that started it"


def test_serial_in_use_counts_a_drivers_in_use_error_as_busy(ssh, ssh_server):
    ssh_server.server.execs.clear()
    try:
        ssh.serial_in_use("COM9")
    except Exception:
        pass
    src = decode_ps(ssh_server.server.execs[-1])
    assert "[System.IO.IOException]" in src and "HResult" in src
    assert "[System.UnauthorizedAccessException]" in src


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"),
                    reason="runs the Windows scripts for real")
@pytest.mark.parametrize("throw, verdict", [
    ("[System.IO.IOException]::new('Sharing violation on COM9', -2147024864)", "BUSY"),
    ("[System.IO.IOException]::new('The port COM9 is in use.')", "BUSY"),
    ("[System.UnauthorizedAccessException]::new('Access to the port is denied.')",
     "BUSY"),
    ("[System.IO.IOException]::new('The port COM9 does not exist.', -2147024894)",
     "UNAVAILABLE"),
])
def test_serial_in_use_verdicts_for_real(monkeypatch, throw, verdict):
    handler = SSHHandler(SSHConfig(host="x"), quiet=True)
    sent = []

    class Res:
        text = ""
    monkeypatch.setattr(handler, "run", lambda cmd, **kw: (sent.append(cmd), Res())[1])
    handler.serial_in_use("COM9")
    src = decode_ps(sent[-1]).replace("$p.Open()", f"throw {throw}")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                          core._ps_b64(src)], capture_output=True, timeout=60,
                         stdin=subprocess.DEVNULL)
    assert out.stdout.decode(errors="replace").strip().startswith(verdict), out


# ===================================================================== F / J
def test_posix_bridge_keeps_2_0_newline_translations(monkeypatch):
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_bridge("/dev/ttyUSB0")
    cmd = sent[-1]
    assert "stty raw -echo -isig" in cmd
    assert (cmd.index("stty raw -echo -isig") < cmd.index("stty icrnl opost onlcr")
            < cmd.index("cat /dev/ttyUSB0 &"))


def test_posix_pid_file_lives_in_a_private_folder(monkeypatch):
    handler, sent = _offline_handler(monkeypatch)
    handler.serial_bridge("/dev/ttyUSB0")
    cmd = sent[-1]
    assert "/tmp/turbossh_ser_" not in cmd
    assert '"${TMPDIR:-/tmp}/turbossh-$(id -u)"' in cmd
    assert 'mkdir -m 700 "$__d"' in cmd
    assert '[ ! -L "$__d" ] && [ -O "$__d" ]' in cmd
    assert '[ -n "$__pf" ] && echo' in cmd


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runs sh for real")
def test_posix_pid_folder_refuses_a_planted_link(monkeypatch, tmp_path):
    handler = SSHHandler(SSHConfig(host="x"), quiet=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    planted = tmp_path / f"turbossh-{os.getuid()}"
    os.symlink(str(victim), str(planted))
    script = handler._posix_pid_setup("/dev/ttyUSB0") + 'printf %s "$__pf"'
    out = subprocess.run(["sh", "-c", script], capture_output=True,
                         env=dict(os.environ, TMPDIR=str(tmp_path)), timeout=20)
    assert out.stdout == b"", "the PID file went into a folder someone planted"
    os.unlink(str(planted))
    out = subprocess.run(["sh", "-c", script], capture_output=True,
                         env=dict(os.environ, TMPDIR=str(tmp_path)), timeout=20)
    assert out.stdout.decode().endswith("turbossh_ser_devttyUSB0.pid")
    assert stat.S_IMODE(os.stat(str(planted)).st_mode) == 0o700


# ===================================================================== H
class _Timeout:
    """An SFTP client whose every request times out."""

    def __getattr__(self, name):
        def stalled(*a, **kw):
            raise socket.timeout("timed out")
        return stalled


def test_sftp_timeouts_are_not_mistaken_for_missing_files():
    h = SSHHandler(SSHConfig(host="x"), quiet=True)
    link = core._S_IFLNK | 0o777

    class Entry:
        st_mode = link
    with pytest.raises(socket.timeout):
        h._entry_kind(_Timeout(), Entry(), "/l")           # not a dangling link
    with pytest.raises(socket.timeout):
        h._real_dir(_Timeout(), "/d")

    class Missing:
        def stat(self, path):
            raise IOError(errno.ENOENT, "no such file")

        def mkdir(self, path, mode=0o777):
            raise socket.timeout("timed out")
    with pytest.raises(socket.timeout):
        h._mkdir_p(Missing(), "/a/b")


def test_a_push_whose_lstat_times_out_writes_nothing(tmp_path):
    """A timed-out lstat said "nothing there": the upload went ahead on a client
    that was out of step with the server."""
    h = SSHHandler(SSHConfig(host="x"), quiet=True)
    src = tmp_path / "a.txt"
    src.write_text("a")
    sent = []

    class LstatTimesOut:
        def lstat(self, path):
            raise socket.timeout("timed out")

        def __getattr__(self, name):
            return lambda *a, **kw: sent.append(name)
    with pytest.raises(SSHTransferError, match="timed out"):
        h.push(str(src), "a.txt", make_dirs=False, sftp=LstatTimesOut())
    assert sent == [], f"went on after the timeout: {sent}"


# ===================================================================== I / K
_PSHOME = b"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\r\n"


@pytest.fixture()
def script(ssh_server):
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


def test_camera_names_and_paths_cannot_break_the_powershell_command_line(
        ssh, ssh_server, script):
    """Behind --%, PowerShell still ends the line at a | and expands %VAR%."""
    script(lambda cmd: (_PSHOME, b"", 0) if cmd == "echo $PSHOME" else
           (b"", b"", 1))
    ssh.list_cameras(ffmpeg="C:\\a|b\\%TEMP%\\ffmpeg.exe")
    sent = ssh_server.server.execs[-1]
    assert sent.startswith("cmd /s /c --% ")
    assert "|" not in sent and "%" not in sent.replace("--%", "")
    with pytest.raises(SSHConnectionError) as caught:
        ssh.webcam_channel("Cam|A %USERNAME%", ffmpeg="ffmpeg")
    for cmd in ssh_server.server.execs:
        if "dshow" in cmd:
            assert "|" not in cmd and "%" not in cmd.replace("--%", "")
    # K: the error says how to clear a stale ffmpeg of an earlier session
    assert "force=True" in str(caught.value)


def test_webcam_release_with_nothing_of_its_own_says_how_to_clean_up(ssh, ssh_server,
                                                                     caplog):
    said = []
    ssh._log_callback = said.append
    n = len(ssh_server.server.execs)
    with caplog.at_level(logging.WARNING):
        assert ssh.webcam_release().ok
    assert len(ssh_server.server.execs) == n
    assert any("force=True" in m and "nothing" in m for m in said), said
