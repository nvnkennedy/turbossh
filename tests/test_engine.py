"""SSHHandler against the loopback server: timeouts, large output, auth
attempts, sudo, jump hosts, SFTP."""

from __future__ import annotations

import os
import time

import paramiko
import pytest

from turbossh import (SSHAuthenticationError, SSHConfig, SSHHandler,
                      SSHTimeoutError, SSHTransferError)


# ------------------------------------------------------------------ commands
def test_timeout_is_enforced_and_keeps_partial_output(ssh):
    t0 = time.monotonic()
    with pytest.raises(SSHTimeoutError) as caught:
        ssh.run("partial-hang", timeout=1.5)
    assert time.monotonic() - t0 < 6, "the timeout did not bound the call"
    assert caught.value.result.stdout == "before\n"
    assert caught.value.result.exit_code == -1
    # the handler is still usable afterwards
    assert ssh.run("echo").text == "ok"


def test_command_timeout_from_config_applies(make_config):
    handler = SSHHandler(make_config(command_timeout=1.0), quiet=True)
    handler.connect()
    try:
        with pytest.raises(SSHTimeoutError):
            handler.run("hang")
    finally:
        handler.disconnect()


def test_output_larger_than_the_channel_window_does_not_deadlock(ssh):
    res = ssh.run("big", timeout=60)
    assert res.ok and len(res.stdout) == 6 * 1024 * 1024


def test_large_stderr_and_stdout_are_both_drained(ssh):
    res = ssh.run("bigerr", timeout=60)
    assert res.ok
    assert len(res.stdout) == 3 * 1024 * 1024
    assert len(res.stderr) == 3 * 1024 * 1024


def test_exit_code_stderr_and_start_time(ssh):
    before = time.time()
    res = ssh.run("exit 3")
    assert res.exit_code == 3 and not res.ok
    assert res.stderr == "failing\n"
    # started_at is when the command STARTED, not when the result was built
    assert before - 0.5 <= res.started_at <= before + 0.5


def test_command_whose_child_holds_the_output_open_still_returns(ssh):
    t0 = time.monotonic()
    res = ssh.run("bg", timeout=30)
    assert res.ok and res.stdout == "done\n"
    assert time.monotonic() - t0 < 6


def test_check_raises_with_the_result(ssh):
    from turbossh import SSHCommandError
    with pytest.raises(SSHCommandError) as caught:
        ssh.run("exit 2", check=True)
    assert caught.value.result.exit_code == 2


# ------------------------------------------------------------------ auth
def test_wrong_password_costs_exactly_one_login(ssh_server, make_config):
    with pytest.raises(SSHAuthenticationError):
        SSHHandler(make_config(password="WRONG"), quiet=True).connect()
    assert ssh_server.server.auth_log == ["WRONG"]


def test_explicit_blank_password_is_sent_once(ssh_server, make_config):
    handler = SSHHandler(make_config(username="blank", password=""), quiet=True)
    handler.connect()
    handler.disconnect()
    # 2.7.0: SSH's "none" login is tried first (head units take it); the
    # blank password is still sent once, on the same connection
    assert ssh_server.server.auth_log == ["<none>", ""]
    assert ssh_server.accepted == 1


def test_no_password_falls_back_to_blank(ssh_server, make_config):
    """Lab targets with an empty root password and nothing configured."""
    handler = SSHHandler(make_config(username="blank", password=None), quiet=True)
    handler.connect()
    assert handler.run("echo").ok
    handler.disconnect()
    assert ssh_server.server.auth_log[-1] == ""


def test_blank_fallback_can_be_switched_off(ssh_server, make_config):
    cfg = make_config(username="blank", password=None, allow_empty_password=False)
    with pytest.raises(SSHAuthenticationError):
        SSHHandler(cfg, quiet=True).connect()
    assert "" not in ssh_server.server.auth_log


def test_key_file_login(ssh_server, make_config, tmp_path):
    key = paramiko.RSAKey.generate(2048)
    keyfile = tmp_path / "id_rsa"
    key.write_private_key_file(str(keyfile))
    ssh_server.server.accepted_key = key
    handler = SSHHandler(make_config(password=None, key_filename=str(keyfile)),
                         quiet=True)
    handler.connect()
    assert handler.run("echo").ok
    handler.disconnect()
    assert "<publickey>" in ssh_server.server.auth_log


def test_legacy_algorithms_do_not_change_paramiko_globally(make_config):
    before = paramiko.Transport._preferred_kex
    handler = SSHHandler(make_config(enable_legacy_algorithms=True), quiet=True)
    handler.connect()
    try:
        assert handler.run("echo").ok
        if SSHHandler._supports_transport_factory():
            assert type(handler.transport).__name__ == "LegacyTransport"
            assert paramiko.Transport._preferred_kex == before
    finally:
        handler.disconnect()


def test_legacy_preferences_only_offer_what_paramiko_implements():
    prefs = SSHHandler._legacy_preferences()
    assert set(prefs["_preferred_kex"]) <= set(paramiko.Transport._kex_info)
    assert set(prefs["_preferred_ciphers"]) <= set(paramiko.Transport._cipher_info)
    assert set(prefs["_preferred_macs"]) <= set(paramiko.Transport._mac_info)
    assert set(prefs["_preferred_keys"]) <= set(paramiko.Transport._key_info)


# ------------------------------------------------------------------ sudo
def test_sudo_sends_the_password_when_prompted(ssh):
    res = ssh.sudo("whoami", "good", timeout=20)
    assert res.ok and res.text == "root"
    assert "sudo-" not in res.stdout + res.stderr, "the prompt marker leaked"


def test_sudo_wrong_password_fails_promptly(ssh):
    t0 = time.monotonic()
    res = ssh.sudo("whoami", "nope", timeout=30)
    assert not res.ok
    assert time.monotonic() - t0 < 10
    assert "Sorry, try again." in res.stderr


def test_sudo_never_writes_the_password_when_sudo_does_not_ask(ssh, ssh_server):
    ssh_server.server.sudo_mode = "nopasswd"
    res = ssh.sudo("whoami", "s3cret-pw", timeout=20)
    assert res.ok and res.text == "root"
    assert "s3cret-pw" not in res.stdout + res.stderr
    assert "STDIN:" not in res.stdout


# ------------------------------------------------------------------ jump host
def _jump_config(make_config, **target):
    return make_config(jump_host=make_config(), **target)


def test_connect_through_a_jump_host(make_config):
    handler = SSHHandler(_jump_config(make_config), quiet=True)
    handler.connect()
    try:
        assert handler.run("echo").text == "ok"
    finally:
        handler.disconnect()
    assert handler._jump is None


def test_jump_host_wrong_target_password_is_an_auth_error(ssh_server, make_config):
    handler = SSHHandler(_jump_config(make_config, password="WRONG", max_retries=3,
                                      retry_backoff=0.1), quiet=True)
    with pytest.raises(SSHAuthenticationError):
        handler.connect()
    # one login for the jump, one for the target — no retries, no blank attempt
    assert ssh_server.server.auth_log == ["good", "WRONG"]
    assert handler._jump is None, "the jump connection was left open"


def test_jump_host_blank_fallback_gets_its_own_channel(ssh_server, make_config):
    handler = SSHHandler(_jump_config(make_config, username="blank", password=None),
                         quiet=True)
    handler.connect()
    try:
        assert handler.run("echo").ok
    finally:
        handler.disconnect()


def test_connection_hint_does_not_probe_locally_for_jump_targets(make_config):
    handler = SSHHandler(_jump_config(make_config), quiet=True)
    probed = []
    handler._port_open = lambda *a, **k: probed.append(a) or False
    hint = handler._connection_hint(OSError("boom"))
    assert "jump host" in hint and not probed


# ------------------------------------------------------------------ SFTP
def test_push_and_pull_a_file_with_progress(ssh, ssh_server, tmp_path):
    src = tmp_path / "local.bin"
    src.write_bytes(os.urandom(200_000))
    seen = []
    up = ssh.push(str(src), "/up/dir/remote.bin",
                  callback=lambda done, total: seen.append((done, total)))
    assert up.size_bytes == 200_000 and seen and seen[-1] == (200_000, 200_000)
    assert (ssh_server.root / "up" / "dir" / "remote.bin").read_bytes() == src.read_bytes()

    back = tmp_path / "back" / "copy.bin"
    down = ssh.pull("/up/dir/remote.bin", str(back))
    assert down.size_bytes == 200_000 and back.read_bytes() == src.read_bytes()


def test_pull_of_a_missing_file_leaves_nothing_behind(ssh, tmp_path):
    target = tmp_path / "nothing.txt"
    with pytest.raises(SSHTransferError):
        ssh.pull("/does/not/exist", str(target))
    assert not target.exists()


def test_aborting_from_the_callback_removes_the_partial_file(ssh, ssh_server, tmp_path):
    (ssh_server.root / "big.bin").write_bytes(os.urandom(600_000))
    target = tmp_path / "big.bin"

    class Cancelled(Exception):
        pass

    def cancel(done, total):
        raise Cancelled()

    with pytest.raises(Cancelled):
        ssh.pull("/big.bin", str(target), callback=cancel)
    assert not target.exists()


def test_recursive_push_and_pull(ssh, ssh_server, tmp_path):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.txt").write_text("a")
    (tree / "sub" / "b.txt").write_text("bb")
    up = ssh.push(str(tree), "/tree", recursive=True)
    assert up.files == 2 and up.size_bytes == 3
    down = ssh.pull("/tree", str(tmp_path / "copy"), recursive=True)
    assert down.files == 2 and down.size_bytes == 3
    assert (tmp_path / "copy" / "sub" / "b.txt").read_text() == "bb"


def test_transfer_with_a_caller_owned_sftp_client(ssh, ssh_server, tmp_path):
    (ssh_server.root / "f.txt").write_text("hello")
    own = ssh.client.open_sftp()
    try:
        ssh.pull("/f.txt", str(tmp_path / "f.txt"), sftp=own)
    finally:
        own.close()
    assert (tmp_path / "f.txt").read_text() == "hello"
    assert ssh._sftp is None, "the shared client should not have been opened"


def test_rename_falls_back_when_posix_rename_is_unsupported(ssh, ssh_server):
    (ssh_server.root / "old.txt").write_text("x")
    ssh_server.server.posix_rename_supported = False
    ssh.rename("/old.txt", "/new.txt")
    assert (ssh_server.root / "new.txt").exists()
    assert ssh_server.server.rename_calls == ["posix_rename", "rename"]


def test_sftp_client_is_reopened_after_its_channel_dies(ssh, ssh_server):
    (ssh_server.root / "f.txt").write_text("x")
    first = ssh.sftp()
    first.get_channel().close()
    assert ssh.exists("/f.txt") is True
    assert ssh.sftp() is not first


def _linked_tree(ssh_server):
    """/data/real/f.txt, /data/link -> /data/real, /data/real/loop -> /data."""
    real = ssh_server.root / "data" / "real"
    real.mkdir(parents=True)
    (real / "f.txt").write_text("inside")
    try:
        os.symlink(str(real), str(ssh_server.root / "data" / "link"),
                   target_is_directory=True)
        os.symlink(str(ssh_server.root / "data"), str(real / "loop"),
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs a privilege this account lacks")


@pytest.mark.parametrize("order", ["sorted", "reverse"])
@pytest.mark.parametrize("server_resolves_links", [True, False])
def test_symlinked_folder_is_followed_and_loops_are_not(ssh, ssh_server, tmp_path,
                                                        server_resolves_links, order):
    """Each real folder is copied once, and always under the same name: its
    own, with the link to it skipped. Which of the two was copied hung on the
    order the server listed them in — on Linux, the file system's hash order,
    so it changed from one machine to the next. A loop ends the walk: at once
    where the server's realpath shows the folder was copied (OpenSSH), else at
    the cap on nested symlinked folders."""
    ssh_server.server.resolve_links = server_resolves_links
    ssh_server.server.listing_order = order
    _linked_tree(ssh_server)
    down = ssh.pull("/data", str(tmp_path / "data"), recursive=True)
    assert (tmp_path / "data" / "real" / "f.txt").read_text() == "inside"
    if server_resolves_links:
        assert down.files == 1
        assert not (tmp_path / "data" / "link").exists()
        assert not (tmp_path / "data" / "real" / "loop").exists()
    else:                               # no way to tell: the link is copied too
        assert (tmp_path / "data" / "link" / "f.txt").read_text() == "inside"
        assert down.files >= 2          # and it terminated despite the loop


def _cross_linked(top, n=4):
    """n folders, each with a file and a link to every other one."""
    names = [chr(ord("a") + i) for i in range(n)]
    for name in names:
        (top / name).mkdir(parents=True)
        (top / name / "f.txt").write_text(name)
    try:
        for name in names:
            for other in names:
                if other != name:
                    os.symlink(str(top / other), str(top / name / f"to_{other}"),
                               target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs a privilege this account lacks")


@pytest.mark.parametrize("order", ["sorted", "reverse"])
def test_cross_linked_folders_are_downloaded_once_each(ssh, ssh_server, tmp_path, order):
    """As /sys does: folders linking to each other. Following every way to a
    folder copies n·e·(n-1)! of them (64 here, 110 000 for 8)."""
    ssh_server.server.listing_order = order
    _cross_linked(ssh_server.root / "data")
    assert ssh.pull("/data", str(tmp_path / "data"), recursive=True).files == 4
    assert sorted(os.listdir(tmp_path / "data")) == ["a", "b", "c", "d"]
    assert os.listdir(tmp_path / "data" / "a") == ["f.txt"]


def test_an_upload_of_cross_linked_folders_ends(ssh, ssh_server, tmp_path):
    """An upload copies a symlinked folder, as scp -r does (and as it always
    did); it went round the links until the path was too long."""
    _cross_linked(tmp_path / "local", n=3)
    up = ssh.push(str(tmp_path / "local"), "/up", recursive=True)
    assert up.files == 15           # a, a/to_b, a/to_b/to_c, a/to_c, a/to_c/to_b ...
    assert (ssh_server.root / "up" / "a" / "to_b" / "to_c" / "f.txt").read_text() == "c"
    assert not (ssh_server.root / "up" / "a" / "to_b" / "to_a").exists()


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows's")
def test_a_junction_counts_as_a_link(tmp_path):
    """Python 3.12 tells a junction from a folder; before, the reparse tag does."""
    import _winapi
    from turbossh import _transfer
    (tmp_path / "real").mkdir()
    _winapi.CreateJunction(str(tmp_path / "real"), str(tmp_path / "j"))
    assert _transfer.local_link(str(tmp_path / "j"))
    assert not _transfer.local_link(str(tmp_path / "real"))
    assert not _transfer.local_link(str(tmp_path / "missing"))


def test_push_copies_a_linked_folder_and_ends_a_loop(ssh, ssh_server, tmp_path):
    """A local link back up the tree was followed for ever (until the path got
    too long): an upload had no loop check at all. A link to a folder in the
    tree is still copied in full (site/current -> releases/2 must arrive)."""
    src = tmp_path / "src"
    (src / "real").mkdir(parents=True)
    (src / "real" / "f.txt").write_text("inside")
    try:
        os.symlink(str(src / "real"), str(src / "link"), target_is_directory=True)
        os.symlink(str(src), str(src / "real" / "loop"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs a privilege this account lacks")
    up = ssh.push(str(src), "/up", recursive=True)
    for name in ("real", "link"):
        assert (ssh_server.root / "up" / name / "f.txt").read_text() == "inside", name
    assert up.files == 2
    assert not (ssh_server.root / "up" / "real" / "loop").exists()  # no empty folder
    assert not (ssh_server.root / "up" / "link" / "loop").exists()


@pytest.mark.parametrize("order", ["sorted", "reverse"])
def test_walk_takes_each_folder_once_under_its_own_name(ssh, ssh_server, order):
    ssh_server.server.listing_order = order
    _linked_tree(ssh_server)
    assert list(ssh.walk("/data")) == [("/data", ["link", "real"], []),
                                       ("/data/real", ["loop"], ["f.txt"])]


def test_walk_takes_a_folder_inside_the_tree_under_its_own_name(ssh, ssh_server):
    """/a/to_b -> /b: "a" comes first, but b is not walked as /a/to_b."""
    _cross_linked(ssh_server.root, n=2)
    assert [top for top, _, _ in ssh.walk("/")] == ["/", "/a", "/b"]


def test_walk_can_be_pruned_and_follows_links_outside_the_tree(ssh, ssh_server):
    root = ssh_server.root
    for name in ("tree/keep", "tree/drop", "outside"):
        (root / name).mkdir(parents=True)
    (root / "outside" / "o.txt").write_text("o")
    try:
        os.symlink(str(root / "outside"), str(root / "tree" / "keep" / "ext"),
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs a privilege this account lacks")
    seen = []
    for top, dirs, files in ssh.walk("/tree"):
        seen.append((top, files))
        if "drop" in dirs:
            dirs.remove("drop")
    assert seen == [("/tree", []), ("/tree/keep", []), ("/tree/keep/ext", ["o.txt"])]


def test_stream_creates_the_save_folder(ssh, tmp_path):
    target = tmp_path / "logs" / "new" / "out.log"
    res = ssh.stream("echo", save_to=str(target), timeout=10)
    assert res["lines"] == 1 and target.read_text().strip() == "ok"


def test_concurrent_callers_reconnect_only_once(make_config):
    import threading
    handler = SSHHandler(make_config(), quiet=True)
    connects = []
    original = handler._connect_locked

    def counting():
        connects.append(1)
        time.sleep(0.2)
        return original()
    handler._connect_locked = counting
    threads = [threading.Thread(target=handler._ensure_connected) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    try:
        assert handler.is_connected and len(connects) == 1
    finally:
        handler.disconnect()


def test_secret_never_reaches_the_log(make_config):
    lines = []
    handler = SSHHandler(make_config(password="good"), quiet=True,
                         log_callback=lines.append)
    handler.connect()
    handler._emit(20, "auth with good done")
    handler.disconnect()
    assert all("with good" not in ln for ln in lines)


def test_config_repr_hides_password():
    assert "hunter2" not in repr(SSHConfig(host="h", password="hunter2"))
