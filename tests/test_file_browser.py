"""The transfer log (no Qt) and the dual-pane file browser built on it."""

from __future__ import annotations

import os
import time

import pytest

from turbossh.gui import transfer_log as tl


# ------------------------------------------------------------------ transfer log
class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_transfer_log_tracks_progress_speed_and_eta():
    clock = Clock()
    log = tl.TransferLog(clock=clock)
    first = log.add("download", "/var/log/big.bin", "C:/tmp/big.bin")
    second = log.add("upload", "/srv/tree", "C:/tmp/tree", is_dir=True)
    assert log.active and log.stats().queued == 2
    assert log.stats().summary() == "2 queued"

    log.start(first)
    log.progress(first, "big.bin", 0, 0, total=1000)
    clock.now += 2
    log.progress(first, "big.bin", 40, 400, total=1000)
    assert log.speed() == pytest.approx(200.0)
    stats = log.stats()
    assert stats.running == 1 and stats.queued == 1
    assert stats.eta is None, "a queued folder has no known size: no honest ETA"

    log.finish(second, tl.CANCELLED)
    stats = log.stats()
    assert stats.eta == pytest.approx(3.0)              # 600 bytes left at 200 B/s
    assert "1 running" in stats.summary() and "left" in stats.summary()

    clock.now += 3
    log.finish(first, tl.DONE, size=1000)
    item = log.get(first)
    assert item.finished and item.percent == 100 and item.total == 1000
    assert item.elapsed(clock()) == pytest.approx(5.0)
    assert tl.past_tense(item) == "Downloaded big.bin (1000 B in 5s)"
    assert not log.active and log.speed() == 0.0


def test_transfer_log_retry_report_and_history_limit():
    log = tl.TransferLog(clock=Clock(), limit=3)
    failed = log.add("upload", "/remote/a.txt", "C:/a.txt")
    log.start(failed)
    log.finish(failed, tl.FAILED, error="Permission denied")
    queued = log.add("upload", "/remote/b.txt", "C:/b.txt")
    assert log.cancel_queued() == [queued]
    assert [i.id for i in log.retryable()] == [failed, queued]
    assert log.retryable([queued])[0].job == ("upload", "/remote/b.txt", "C:/b.txt", False)
    report = log.report()
    assert "[FAILED   ]" in report and "Permission denied" in report
    assert "PC → Remote" in report

    revision = log.revision
    for n in range(6):                                   # oldest finished fall off
        item = log.add("download", f"/r/{n}", f"C:/{n}")
        log.finish(item, tl.DONE, size=1)
    assert len(log) == 3 and log.revision > revision
    running = log.add("download", "/r/live", "C:/live")
    log.start(running)
    assert log.clear_finished() == 3
    assert [i.id for i in log] == [running], "an active item was dropped"


def test_folder_items_show_files_and_bytes_not_a_percentage():
    log = tl.TransferLog(clock=Clock())
    item_id = log.add("download", "/srv/tree", "C:/tree", is_dir=True)
    log.start(item_id)
    log.progress(item_id, "b.txt", 50, 3 * 1024 * 1024, total=10, files=2)
    item = log.get(item_id)
    assert item.total is None and item.progress_text() == "2 files, 3.0 MB"


# ------------------------------------------------------------------ dual pane
pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(25)
    return predicate()


@pytest.fixture()
def browser(qapp, ssh, ssh_server, tmp_path):
    from turbossh.gui.file_browser import FileBrowser
    root = ssh_server.root
    (root / "docs").mkdir()
    (root / "docs" / "inner.txt").write_text("inner")
    (root / "alpha.txt").write_text("alpha")
    (root / "zeta.cfg").write_bytes(b"key=1\r\nname=x\r\n")
    local = tmp_path / "pc"
    (local / "folder").mkdir(parents=True)
    (local / "folder" / "deep.txt").write_text("deep")
    (local / "note.txt").write_text("note")
    widget = FileBrowser(ssh)
    widget.messages = []
    widget.notify.connect(lambda text, kind: widget.messages.append((kind, text)))
    widget.show()
    assert widget.local.navigate(str(local))
    assert wait_until(lambda: not widget.busy
                      and widget.remote.tree.topLevelItemCount() > 1)
    widget.local_dir = local
    yield widget
    widget.close_browser()
    widget.close_browser()
    widget.deleteLater()
    QTest.qWait(50)


def remote_names(widget):
    tree = widget.remote.tree
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def select_remote(widget, *names):
    tree = widget.remote.tree
    tree.clearSelection()
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        item.setSelected(item.text(0) in names)


def test_upload_and_download_between_the_panes(browser, ssh_server):
    local = browser.local_dir
    assert browser.upload([str(local / "note.txt"), str(local / "folder")]) == 2
    assert wait_until(lambda: not browser.busy)
    assert (ssh_server.root / "note.txt").read_text() == "note"
    assert (ssh_server.root / "folder" / "deep.txt").read_text() == "deep"

    select_remote(browser, "alpha.txt", "docs")
    assert browser.download() == 2
    assert wait_until(lambda: not browser.busy)
    assert (local / "alpha.txt").read_text() == "alpha"
    assert (local / "docs" / "inner.txt").read_text() == "inner"

    # every transfer is in the history, with sizes, and the panel shows them
    items = browser.transfers.items()
    assert [i.status for i in items] == [tl.DONE] * 4
    assert {i.kind for i in items} == {"upload", "download"}
    assert sum(i.moved for i in items) == len("note") + len("deep") + len("alpha") + len("inner")
    browser.panel.refresh()
    assert browser.panel.model.rowCount() == 4
    assert "4 done" in browser.panel.summary.text()
    assert [kind for kind, _text in browser.messages] == ["ok"] * 4
    assert not browser.remote.xfer_row.isVisible()       # the panel replaces it


def test_dragging_remote_rows_onto_the_local_pane_downloads_them(browser):
    from turbossh.gui.sftp_browser import REMOTE_MIME
    tree = browser.remote.tree
    rows = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    mime = tree.mimeData(rows)
    dragged = bytes(mime.data(REMOTE_MIME)).decode().split("\n")
    assert dragged == ["docs", "alpha.txt", "zeta.cfg"]      # never ".."
    browser.local.view.remote_dropped.emit(["alpha.txt"])
    assert wait_until(lambda: not browser.busy)
    assert (browser.local_dir / "alpha.txt").read_text() == "alpha"


def test_download_needs_a_local_folder(browser):
    select_remote(browser, "alpha.txt")
    browser.local.navigate("")                               # the list of drives
    assert browser.download() == 0
    assert "local folder" in browser.local.status.text()
    assert len(browser.transfers) == 0


def test_failed_transfer_can_be_retried_from_the_panel(browser, ssh_server):
    target = browser.local_dir / "ghost.txt"
    assert browser.remote.queue_download("/ghost.txt", str(target))
    assert wait_until(lambda: not browser.busy)
    item = browser.transfers.items()[0]
    assert item.status == tl.FAILED and item.error
    assert ("error" in [kind for kind, _ in browser.messages])

    (ssh_server.root / "ghost.txt").write_text("now it exists")
    browser.panel.refresh()
    browser.panel._retry_failed()
    assert wait_until(lambda: not browser.busy)
    assert target.read_text() == "now it exists"
    assert [i.status for i in browser.transfers] == [tl.FAILED, tl.DONE]


def test_cancel_marks_the_running_and_queued_transfers(browser, ssh_server, tmp_path):
    (ssh_server.root / "big.bin").write_bytes(b"\0" * (48 * 1024 * 1024))
    browser.remote.queue_download("/big.bin", str(tmp_path / "big.bin"))
    browser.remote.queue_download("/alpha.txt", str(tmp_path / "later.txt"))
    assert wait_until(lambda: browser.transfers.items()[0].moved > 0 or not browser.busy)
    assert browser.busy, "the transfer finished before it could be cancelled"
    browser.panel.cancel_requested.emit()
    assert wait_until(lambda: not browser.busy)
    assert [i.status for i in browser.transfers] == [tl.CANCELLED, tl.CANCELLED]
    assert not (tmp_path / "big.bin").exists()


def test_remote_columns_sort_with_folders_first(browser):
    tree = browser.remote.tree
    assert remote_names(browser) == ["..", "docs", "alpha.txt", "zeta.cfg"]
    tree.sortByColumn(0, Qt.DescendingOrder)
    assert remote_names(browser) == ["..", "docs", "zeta.cfg", "alpha.txt"]
    tree.sortByColumn(1, Qt.DescendingOrder)                 # size, largest first
    assert remote_names(browser) == ["..", "docs", "zeta.cfg", "alpha.txt"]
    tree.sortByColumn(1, Qt.AscendingOrder)
    assert remote_names(browser) == ["..", "docs", "alpha.txt", "zeta.cfg"]


def test_local_pane_actions(browser, monkeypatch):
    local, pane = browser.local_dir, browser.local
    assert pane.do_new_folder("made here") and (local / "made here").is_dir()
    assert pane.do_new_folder("a/b") is False
    assert pane.do_rename(str(local / "note.txt"), "folder") is False     # taken
    assert pane.do_rename(str(local / "note.txt"), "renamed.txt")
    assert (local / "renamed.txt").read_text() == "note"

    # no recycle bin in a test: fall through to plain removal of the temp files
    from PyQt5.QtCore import QFile
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda path: False))
    assert pane.do_delete([str(local / "renamed.txt"), str(local / "folder")]) == 2
    assert not (local / "renamed.txt").exists() and not (local / "folder").exists()

    assert pane.navigate(str(local / "nope")) is False and pane.cwd == str(local)
    pane.up()
    assert os.path.normcase(pane.cwd) == os.path.normcase(str(local.parent))


def test_edit_a_remote_text_file_keeps_its_line_endings(browser, ssh_server):
    select_remote(browser, "zeta.cfg")
    dialog = browser.remote.edit_selected()
    assert wait_until(lambda: not dialog.edit.isReadOnly())
    assert dialog.edit.toPlainText() == "key=1\nname=x\n"
    assert "CRLF" in dialog.status.text()
    dialog.edit.setPlainText("key=2\nname=y\n")
    dialog.edit.document().setModified(True)
    dialog.save()
    assert wait_until(lambda: dialog.status.text().startswith("Saved"))
    assert (ssh_server.root / "zeta.cfg").read_bytes() == b"key=2\r\nname=y\r\n"
    assert not dialog.edit.document().isModified()
    dialog.close()


def test_editor_refuses_binary_and_oversized_files(ssh, ssh_server, monkeypatch):
    from turbossh.gui import remote_edit
    (ssh_server.root / "blob.bin").write_bytes(b"MZ\0\0binary")
    (ssh_server.root / "long.txt").write_text("x" * 5000)
    sftp = ssh.client.open_sftp()
    try:
        with pytest.raises(remote_edit.NotEditable, match="binary"):
            remote_edit.read_remote_text(sftp, "/blob.bin")
        monkeypatch.setattr(remote_edit, "MAX_EDIT_BYTES", 1000)
        with pytest.raises(remote_edit.NotEditable, match="too large"):
            remote_edit.read_remote_text(sftp, "/long.txt")
    finally:
        sftp.close()
