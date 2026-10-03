"""The SFTP browser against the loopback server's SFTP subsystem."""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtTest import QTest  # noqa: E402


def wait_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(20)
    return predicate()


@pytest.fixture()
def browser(qapp, ssh, ssh_server):
    from turbossh.gui.sftp_browser import SftpBrowser
    root = ssh_server.root
    (root / "docs").mkdir()
    (root / "docs" / "inner.txt").write_text("inner")
    (root / "alpha.txt").write_text("alpha")
    (root / "Beta.log").write_text("beta-beta")
    widget = SftpBrowser(ssh)
    widget.messages = []
    widget.log.connect(widget.messages.append)
    assert wait_until(lambda: not widget.busy and widget.tree.topLevelItemCount() > 1)
    yield widget
    widget.close_browser()
    widget.deleteLater()
    QTest.qWait(50)


def names(widget):
    return [widget.tree.topLevelItem(i).text(0)
            for i in range(widget.tree.topLevelItemCount())]


def test_listing_shows_folders_first_with_columns(browser):
    assert names(browser) == ["..", "docs", "alpha.txt", "Beta.log"]
    assert browser.cwd == "/"
    row = browser.tree.topLevelItem(2)
    assert row.text(1) == "5 B" and row.text(2) and row.text(3).startswith("-")


def test_failed_navigation_keeps_the_current_folder(browser):
    """cwd used to switch to the bad path while the old listing stayed up, so
    Delete / Rename then acted on the wrong place."""
    assert browser.navigate("/no/such/folder")
    assert wait_until(lambda: not browser.busy)
    assert browser.cwd == "/"
    assert browser.path.text() == "/"
    assert names(browser) == ["..", "docs", "alpha.txt", "Beta.log"]
    assert "cannot list" in browser.status.text()
    assert any("[ERROR]" in m for m in browser.messages)


def test_navigation_is_refused_while_a_listing_is_in_flight(browser):
    assert browser.navigate("/docs")
    assert browser.busy
    assert browser.navigate("/docs/inner.txt") is False     # stale double-click
    assert wait_until(lambda: not browser.busy)
    assert browser.cwd == "/docs" and names(browser) == ["..", "inner.txt"]


def test_mkdir_rename_and_their_refusals(browser, ssh_server):
    assert browser.do_mkdir("new folder")
    assert wait_until(lambda: not browser.busy)
    assert (ssh_server.root / "new folder").is_dir()

    assert browser.do_mkdir("docs") is False                # already there
    assert browser.do_rename("alpha.txt", "Beta.log") is False   # would replace
    assert browser.do_rename("alpha.txt", "sub/alpha.txt") is False  # a path
    assert (ssh_server.root / "alpha.txt").read_text() == "alpha"
    assert (ssh_server.root / "Beta.log").read_text() == "beta-beta"

    assert browser.do_rename("alpha.txt", "renamed.txt")
    assert wait_until(lambda: not browser.busy)
    assert (ssh_server.root / "renamed.txt").read_text() == "alpha"
    assert not (ssh_server.root / "alpha.txt").exists()


def test_rename_falls_back_without_the_openssh_extension(browser, ssh_server):
    ssh_server.server.posix_rename_supported = False
    assert browser.do_rename("alpha.txt", "plain.txt")
    assert wait_until(lambda: not browser.busy)
    assert (ssh_server.root / "plain.txt").exists()


def test_delete_removes_a_folder_with_its_contents(browser, ssh_server):
    (ssh_server.root / "docs" / "deep").mkdir()
    (ssh_server.root / "docs" / "deep" / "x.bin").write_bytes(b"x")
    assert browser.do_delete(["docs", "alpha.txt"])
    assert wait_until(lambda: not browser.busy)
    assert not (ssh_server.root / "docs").exists()
    assert not (ssh_server.root / "alpha.txt").exists()
    assert names(browser) == ["..", "Beta.log"]
    question = browser._delete_question([e for e in browser._entries.values()])
    assert "1 selected item" in question


def test_download_and_upload_files_and_folders(browser, ssh_server, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    assert browser.queue_download("/alpha.txt", str(out / "alpha.txt"))
    assert browser.queue_download("/docs", str(out / "docs"), is_dir=True)
    assert wait_until(lambda: not browser.busy)
    assert (out / "alpha.txt").read_text() == "alpha"
    assert (out / "docs" / "inner.txt").read_text() == "inner"

    tree = tmp_path / "push"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("pushed")
    single = tmp_path / "single.txt"
    single.write_text("one")
    assert browser.queue_upload(str(single), "/single.txt")
    assert browser.queue_upload(str(tree), "/push")
    assert wait_until(lambda: not browser.busy)
    assert (ssh_server.root / "single.txt").read_text() == "one"
    assert (ssh_server.root / "push" / "sub" / "f.txt").read_text() == "pushed"
    assert "single.txt" in names(browser) and "push" in names(browser)
    assert not browser.xfer_row.isVisible()


def test_cancel_leaves_no_partial_file(browser, ssh_server, tmp_path):
    (ssh_server.root / "big.bin").write_bytes(b"\0" * (48 * 1024 * 1024))
    target = tmp_path / "big.bin"
    assert browser.queue_download("/big.bin", str(target))
    assert browser.queue_download("/alpha.txt", str(tmp_path / "queued.txt"))
    assert wait_until(lambda: browser._percent > 0 or not browser.busy, 20)
    assert browser.busy, "the transfer finished before it could be cancelled"
    assert browser.cancel_transfer()
    assert wait_until(lambda: not browser.busy, 20)
    assert not target.exists(), "a cancelled download left a partial file"
    assert not (tmp_path / "queued.txt").exists(), "the queue was not dropped"
    assert any("Cancelled" in m for m in browser.messages)


def test_symlinked_folder_can_be_entered(browser, ssh_server):
    try:
        os.symlink(str(ssh_server.root / "docs"), str(ssh_server.root / "link"),
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs a privilege this account lacks")
    browser.refresh()
    assert wait_until(lambda: not browser.busy)
    assert browser._entries["link"].is_dir and browser._entries["link"].is_link
    assert browser.navigate("/link")
    assert wait_until(lambda: not browser.busy)
    assert names(browser) == ["..", "inner.txt"]


def test_close_while_listing_is_safe_and_late_results_are_ignored(qapp, ssh):
    from turbossh.gui.sftp_browser import SftpBrowser
    widget = SftpBrowser(ssh)                # its first listing is now in flight
    widget.close_browser()
    widget.close_browser()                   # twice is fine
    assert widget.navigate("/") is False
    assert widget.queue_download("/x", "y") is False
    widget.deleteLater()
    QTest.qWait(400)                         # the parked worker finishes meanwhile
    assert ssh.run("echo").ok                # the session itself is untouched
