"""Regression tests for the 2.1.0 review fixes in the file browser, the remote
editor, the Transfers panel and the Camera tab. No network, device or download:
the loopback SSH/SFTP server and fakes stand in."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import threading
import time

import pytest

from turbossh.gui import transfer_log as tl


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


# ------------------------------------------------------------ 1: transfer log
def test_transfer_log_is_indexed_and_counts_as_it_goes():
    log = tl.TransferLog(clock=Clock(), limit=3)
    ids = [log.add("download", f"/r/{n}", f"C:/{n}") for n in range(5)]
    assert log.get(ids[3]).remote == "/r/3" and log.active
    log.start(ids[0])
    assert log.requeue(ids[0]) and log.get(ids[0]).status == tl.QUEUED
    assert not log.requeue(ids[1])                       # only a running one
    for item_id in ids:
        log.start(item_id)
        log.finish(item_id, tl.DONE, size=1)
    assert [i.id for i in log] == ids[2:]                # the oldest finished went
    assert log.get(ids[0]) is None and not log.active and log.speed() == 0.0
    revision = log.revision
    log.progress(ids[4], "x", 1, 1)                      # finished: ignored
    assert log.revision == revision


def test_cancelling_a_long_queue_is_not_quadratic():
    """5,000 queued items took 3.4 s to cancel (a scan of the list per item)."""
    log = tl.TransferLog(clock=Clock())
    for n in range(20000):
        log.add("download", f"/r/{n}", f"C:/{n}")
    started = time.perf_counter()
    assert len(log.cancel_queued()) == 20000
    assert time.perf_counter() - started < 2.0
    assert len(log) == tl._HISTORY_LIMIT and not log.active
    assert all(i.status == tl.CANCELLED for i in log)


# ------------------------------------------------------------ Qt from here on
pytest.importorskip("PyQt5")

from PyQt5.QtCore import QFile, QMimeData, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QHeaderView, QMessageBox  # noqa: E402

from turbossh.gui import ffmpeg_tools, qtutil  # noqa: E402
from turbossh.gui import sftp_browser as sb  # noqa: E402


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(20)
    return predicate()


def parts(folder):
    """Temporary transfer files left in *folder*."""
    return [n for n in os.listdir(folder) if n.endswith((sb._PART, ".turbossh-old"))]


def select(widget, *names):
    tree = widget.tree
    tree.clearSelection()
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        item.setSelected(item.text(0) in names)


def throttle(monkeypatch, delay=0.02):
    """Slow every transfer down to ~32 KB per *delay*, so it can be caught
    moving. Wraps the module's own atomic helpers (what the thread calls)."""
    get, put = sb.get_atomic, sb.put_atomic

    def slow(callback):
        return lambda done, total: (time.sleep(delay), callback(done, total))
    monkeypatch.setattr(sb, "get_atomic", lambda s, r, l, cb=None: get(s, r, l, slow(cb)))
    monkeypatch.setattr(sb, "put_atomic", lambda s, l, r, cb=None, existing=sb._UNKNOWN:
                        put(s, l, r, slow(cb), existing))


@pytest.fixture()
def remote(qapp, ssh, ssh_server):
    root = ssh_server.root
    (root / "docs").mkdir()
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "beta.txt").write_bytes(b"beta\n")
    widget = sb.SftpBrowser(ssh)
    widget.messages = []
    widget.log.connect(widget.messages.append)
    assert wait_until(lambda: not widget.busy and widget.tree.topLevelItemCount() > 1)
    yield widget
    widget.close_browser()
    widget.deleteLater()
    QTest.qWait(50)


def open_editor(widget, name):
    select(widget, name)
    dialog = widget.edit_selected()
    assert dialog is not None
    assert wait_until(lambda: dialog._loaded or dialog.status.text().startswith("⚠"))
    return dialog


# ------------------------------------------------------------ 1: the panel
def test_transfer_table_repaints_only_what_changed(qapp):
    from turbossh.gui.transfer_panel import TransferPanel
    log = tl.TransferLog(limit=50)
    for n in range(60):
        item_id = log.add("download", f"/r/{n}", f"C:/{n}")
        log.start(item_id)
        log.finish(item_id, tl.DONE, size=n)
    running = log.add("download", "/r/big", "C:/big")
    log.start(running)
    panel = TransferPanel(log)
    panel.show()
    header = panel.table.horizontalHeader()
    assert all(header.sectionResizeMode(c) == QHeaderView.Interactive
               for c in range(header.count()))
    events = []
    model = panel.model
    model.dataChanged.connect(lambda a, b, *_: events.append(("changed", a.row(), b.row())))
    model.modelReset.connect(lambda: events.append(("reset",)))
    model.rowsInserted.connect(lambda _p, a, b: events.append(("inserted", a, b)))
    model.rowsRemoved.connect(lambda _p, a, b: events.append(("removed", a, b)))
    try:
        log.progress(running, "big", 10, 100, total=1000)
        panel.refresh()
        assert events == [("changed", 50, 50)], "every row was repainted"
        events.clear()
        log.add("upload", "/r/new", "C:/new")
        panel.refresh()
        assert ("inserted", 51, 51) in events and ("reset",) not in events
        events.clear()
        log.finish(running, tl.DONE)                     # history full: oldest goes
        panel.refresh()
        assert ("removed", 0, 0) in events and ("reset",) not in events
        assert model.rowCount() == 51 and model.item_at(49).id == running

        # hidden (another tab in front): the timer leaves the table alone
        panel.hide()
        seen = model._rev
        log.add("upload", "/r/later", "C:/later")
        panel._tick()
        assert model._rev == seen
        panel.show()                                     # catches up at once
        assert model._rev == log.revision and model.rowCount() == 52
    finally:
        panel.close_panel()
        panel.deleteLater()


# ------------------------------------------------------------ 2: atomic transfers
def test_a_cancelled_download_leaves_the_file_it_would_replace(remote, ssh_server,
                                                              tmp_path, monkeypatch):
    throttle(monkeypatch)
    data = os.urandom(2 * 1024 * 1024)
    (ssh_server.root / "big.bin").write_bytes(data)
    target = tmp_path / "big.bin"
    target.write_bytes(b"ORIGINAL")
    assert remote.queue_download("/big.bin", str(target))
    assert wait_until(lambda: remote.transfers.items()[-1].moved > 0)
    assert remote.cancel_transfer()
    assert wait_until(lambda: not remote.busy)
    assert remote.transfers.items()[-1].status == tl.CANCELLED
    assert target.read_bytes() == b"ORIGINAL", "the cancel destroyed the original"
    assert parts(tmp_path) == []
    assert remote.queue_download("/big.bin", str(target))      # complete: replaced
    assert wait_until(lambda: not remote.busy, 30)
    assert target.read_bytes() == data and parts(tmp_path) == []


@pytest.mark.parametrize("posix_rename", [True, False])
def test_a_cancelled_upload_leaves_the_remote_file_it_would_replace(
        remote, ssh_server, tmp_path, monkeypatch, posix_rename):
    ssh_server.server.posix_rename_supported = posix_rename
    throttle(monkeypatch)
    (ssh_server.root / "keep.bin").write_bytes(b"ORIGINAL")
    local = tmp_path / "keep.bin"
    data = os.urandom(2 * 1024 * 1024)
    local.write_bytes(data)
    assert remote.queue_upload(str(local), "/keep.bin")
    assert wait_until(lambda: remote.transfers.items()[-1].moved > 0)
    assert remote.cancel_transfer()
    assert wait_until(lambda: not remote.busy)
    # Cancel used to delete the remote file outright
    assert (ssh_server.root / "keep.bin").read_bytes() == b"ORIGINAL"
    assert parts(ssh_server.root) == []
    assert remote.queue_upload(str(local), "/keep.bin")
    assert wait_until(lambda: not remote.busy, 30)
    assert remote.transfers.items()[-1].status == tl.DONE
    assert (ssh_server.root / "keep.bin").read_bytes() == data
    assert parts(ssh_server.root) == []


def test_replacing_without_posix_rename_never_loses_the_old_file():
    calls = []

    class Server:
        def posix_rename(self, src, dst):
            raise IOError("unsupported")

        def rename(self, src, dst):
            calls.append((src, dst))
            if src == "/d/.new":
                raise IOError("refused")

        def remove(self, path):
            calls.append(("remove", path))
    with pytest.raises(IOError, match="refused"):
        sb.replace_remote(Server(), "/d/.new", "/d/f.txt")
    aside = calls[0][1]
    assert calls[0][0] == "/d/f.txt" and aside.endswith(".turbossh-old")
    assert calls[-1] == (aside, "/d/f.txt"), "the old file was not put back"


# ------------------------------------------------------------ 3: what the editor opens
def test_the_editor_opens_only_regular_files_and_reads_a_bounded_amount(monkeypatch):
    from turbossh.gui import remote_edit

    class Attr:
        st_mode, st_size, st_mtime = 0o100644, 0, 1

    asked = []

    class Handle:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            asked.append(n)
            return b"x" * (n if n and n > 0 else 50_000)

    class Server:
        def stat(self, path):
            return Attr()

        def open(self, path, mode="r"):
            return Handle()
    monkeypatch.setattr(remote_edit, "MAX_EDIT_BYTES", 1000)
    # size 0 (a device says so too) but more than the limit there: refused
    with pytest.raises(remote_edit.NotEditable, match="too large"):
        remote_edit.read_remote_text(Server(), "/var/log/grows")
    assert asked == [1001], "read to the end instead of to the limit"
    for mode, word in ((0o020666, "character device"), (0o010644, "named pipe"),
                       (0o060660, "block device"), (0, "unknown type")):
        Attr.st_mode = mode
        with pytest.raises(remote_edit.NotEditable, match=word):
            remote_edit.read_remote_text(Server(), "/dev/thing")
    assert asked == [1001], "a device was opened"


def test_double_clicking_a_device_does_not_open_the_editor(remote):
    entry = remote._entries["alpha.txt"]
    remote._entries["alpha.txt"] = entry._replace(ftype=stat.S_IFCHR)
    select(remote, "alpha.txt")
    assert remote.edit_selected() is None
    assert "character device" in remote.status.text()
    assert remote._live_editors() == []


# ------------------------------------------------------------ 4: closing with edits
def test_closing_the_browser_keeps_a_modified_editor_open_offline(remote, ssh_server,
                                                                 tmp_path):
    from turbossh.gui.remote_edit import unsaved_editors
    changed = open_editor(remote, "alpha.txt")
    untouched = open_editor(remote, "beta.txt")
    changed.edit.setPlainText("my edit\n")
    changed.edit.document().setModified(True)
    remote.close_browser()
    QTest.qWait(50)
    assert sb.sip.isdeleted(untouched) or not untouched.isVisible()
    assert changed.isVisible() and changed.offline and changed.has_unsaved_changes()
    assert not changed.save_btn.isEnabled() and changed.copy_btn.isEnabled()
    assert "NOT saved" in changed.status.text()
    assert changed in unsaved_editors()
    copy = tmp_path / "alpha copy.txt"
    changed.save_copy_to(str(copy))
    # the copy is written on a worker and the editor marked saved when it reports
    # back: wait for that, not for the file to appear (it can before the report)
    assert wait_until(lambda: not changed.has_unsaved_changes())
    assert copy.read_bytes() == b"my edit\n"
    assert not changed.has_unsaved_changes() and changed not in unsaved_editors()
    assert (ssh_server.root / "alpha.txt").read_bytes() == b"alpha\n"
    changed.close()


def test_a_closed_editor_deletes_itself(remote):
    dialog = open_editor(remote, "alpha.txt")
    dialog.close()
    assert wait_until(lambda: sb.sip.isdeleted(dialog))
    assert remote._live_editors() == []


# ------------------------------------------------------------ 5: saving safely
def test_save_asks_when_the_file_changed_on_the_remote(remote, ssh_server, monkeypatch):
    path = ssh_server.root / "alpha.txt"
    dialog = open_editor(remote, "alpha.txt")
    dialog.edit.setPlainText("mine\n")
    dialog.edit.document().setModified(True)
    path.write_bytes(b"theirs, and longer\n")            # someone else saved it
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1])
    dialog.save()
    assert wait_until(lambda: asked)
    assert "changed on the remote" in asked[0]
    assert path.read_bytes() == b"theirs, and longer\n"
    assert dialog.has_unsaved_changes() and "not saved" in dialog.status.text()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    dialog.save()
    assert wait_until(lambda: dialog.status.text().startswith("Saved"))
    assert path.read_bytes() == b"mine\n"
    assert parts(ssh_server.root) == []
    assert "posix_rename" in ssh_server.server.rename_calls      # never in place
    dialog.edit.setPlainText("again\n")                  # the new stamp is kept:
    dialog.edit.document().setModified(True)             # no question this time
    asked.clear()
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1])
    dialog.save()
    assert wait_until(lambda: path.read_bytes() == b"again\n")
    assert not asked, "it asked about its own save"
    dialog.close()


@pytest.mark.parametrize("name, data, why", [
    ("nbsp.txt", "a\u00a0b\n".encode("utf-8"), "non-breaking"),
    ("cr.txt", b"a\rb\n", "carriage returns"),
    ("sep.txt", "a\u2028b\n".encode("utf-8"), "line separators"),
    ("mixed.txt", b"a\r\nb\nc\r\n", "mixes CRLF and LF"),
    ("broken.txt", "é".encode("utf-8") + b"\xff\n", "not valid UTF-8"),
])
def test_text_the_editor_would_change_opens_read_only(remote, ssh_server, name, data, why):
    (ssh_server.root / name).write_bytes(data)
    remote.refresh()
    assert wait_until(lambda: not remote.busy and name in remote._entries)
    dialog = open_editor(remote, name)
    assert dialog.edit.isReadOnly() and why in dialog.status.text()
    dialog.edit.document().setModified(True)
    assert not dialog.save_btn.isEnabled()
    dialog.save()                                        # refused, nothing written
    QTest.qWait(100)
    assert (ssh_server.root / name).read_bytes() == data
    dialog.discard()
    dialog.close()


def test_a_legacy_8_bit_file_is_edited_byte_for_byte(remote, ssh_server):
    path = ssh_server.root / "legacy.ini"
    path.write_bytes("Grüße=1\r\n".encode("cp1252"))
    remote.refresh()
    assert wait_until(lambda: not remote.busy and "legacy.ini" in remote._entries)
    dialog = open_editor(remote, "legacy.ini")
    assert not dialog.edit.isReadOnly() and "cp1252" in dialog.status.text()
    dialog.edit.setPlainText("Grüße=2\n")
    dialog.edit.document().setModified(True)
    dialog.save()
    assert wait_until(lambda: dialog.status.text().startswith("Saved"))
    assert path.read_bytes() == "Grüße=2\r\n".encode("cp1252")
    dialog.edit.setPlainText("arrow → 3\n")             # not in cp1252: refused
    dialog.edit.document().setModified(True)
    dialog.save()
    assert "cannot be written in cp1252" in dialog.status.text()
    dialog.discard()
    dialog.close()


def test_decoding_keeps_damaged_utf8_apart_from_legacy_files():
    from turbossh.gui.remote_edit import _decode
    assert _decode("ok €".encode("utf-8")) == ("ok €", "utf-8", "")
    text, encoding, problem = _decode("é".encode("utf-8") + b"\xa0")
    assert encoding == "utf-8" and problem and "\ufffd" in text
    assert _decode("Grüße".encode("cp1252"))[1:] == ("cp1252", "")
    assert _decode(b"a\x81b")[1:] == ("latin-1", "")    # 0x81: not cp1252


# ------------------------------------------------------------ 6: big listings
def test_a_big_listing_is_filled_in_chunks_and_sorted_without_qt(qapp, monkeypatch):
    class Handler:
        known_os = "linux"
    monkeypatch.setattr(sb.SftpBrowser, "_list", lambda self, path, select=None: True)
    compared = []
    monkeypatch.setattr(sb._Row, "__lt__", lambda a, b: compared.append(1) or False)
    browser = sb.SftpBrowser(Handler())
    try:
        entries = [sb._Entry(f"e{n:05d}", n % 7 == 0, False, n, 1_700_000_000 + n,
                             0o100644, stat.S_IFDIR if n % 7 == 0 else stat.S_IFREG)
                   for n in range(5000)]
        browser._busy = "Loading /big…"
        browser._on_listed(("/big", entries, sb._row_specs(entries, browser.tree.order),
                            browser.tree.order))
        assert browser.tree.topLevelItemCount() < 5001, "all rows in one go"
        assert browser.busy
        assert wait_until(lambda: not browser._busy)
        tree = browser.tree
        assert tree.topLevelItemCount() == 5001
        rows = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        assert rows[0].text(0) == ".." and rows[1].text(0) == "e00000"
        tree.sortByColumn(1, Qt.DescendingOrder)          # size, largest first
        rows = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        folders = [r for r in rows if r.rank == 1]
        files = [r for r in rows if r.rank == 2]
        assert rows[0].text(0) == ".." and rows[1].rank == 1
        assert rows.index(files[0]) == len(folders) + 1   # folders stay first
        assert [r.keys[1] for r in files] == sorted((r.keys[1] for r in files), reverse=True)
        assert not compared, "Qt sorted through __lt__"
    finally:
        browser.close_browser()
        browser.deleteLater()


def test_link_targets_are_stat_ed_together(ssh, ssh_server):
    for n in range(100):
        (ssh_server.root / f"f{n}").write_bytes(b"x" * n)
    sftp = sb._open_sftp(ssh)
    try:
        assert sftp.get_channel().gettimeout() == sb._SFTP_TIMEOUT
        paths = [f"/f{n}" for n in range(100)] + ["/missing"]
        together = sb._stat_many(sftp, paths)
        assert [a.st_size for a in together[:100]] == list(range(100))
        assert together[100] is None

        class OneByOne:                                   # no Paramiko internals
            def stat(self, path):
                return sftp.stat(path)
        alone = sb._stat_many(OneByOne(), paths)
        assert [a and a.st_size for a in alone] == [a and a.st_size for a in together]
        assert sftp.stat("/f7").st_size == 7, "the client is out of step"
    finally:
        sftp.close()


# ------------------------------------------------------------ 7: local names
def test_remote_names_become_valid_local_names():
    name = sb.local_name
    assert name("a:b", windows=True) == "a_b"
    assert name("..\\..\\evil.txt", windows=True) == ".._.._evil.txt"
    assert name("C:\\Windows\\x.dll", windows=True) == "C__Windows_x.dll"
    assert name('q?"<>|*.txt', windows=True) == "q______.txt"
    assert name("CON", windows=True) == "_CON" and name("nul.txt", windows=True) == "_nul.txt"
    assert name("com1.log", windows=True) == "_com1.log"
    assert name("console.txt", windows=True) == "console.txt"
    assert name("trail. ", windows=True) == "trail__"
    assert name("..", windows=True) == "__" and name(".", windows=False) == "_"
    assert name("a/b", windows=False) == "a_b" and name("a:b", windows=False) == "a:b"
    names = sb._local_names(["a:b", "Readme", "a_b", "README"], windows=True)
    assert names == {"Readme": "Readme", "a_b": "a_b", "a:b": "a_b (2)",
                     "README": "README (2)"}
    assert sb._local_names(["Readme", "README"], windows=False) == {
        "Readme": "Readme", "README": "README"}


def test_a_folder_download_keeps_every_file_inside_the_destination(qapp, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(sb, "_WINDOWS", True)

    class Attr:
        def __init__(self, filename, mode=0o100644):
            self.filename, self.st_mode, self.st_size, self.st_mtime = filename, mode, 1, 0

    listing = [Attr("..\\..\\evil.txt"), Attr("C:\\x.txt"), Attr("a:b"),
               Attr("Case.txt"), Attr("case.txt"), Attr("CON"), Attr("tail. "),
               Attr("dev", 0o020666), Attr("sub", 0o040755)]

    class Server:
        def normalize(self, path):
            return path

        def stat(self, path):
            return Attr(path, 0o040755)

        def listdir_attr(self, path):
            return listing if path == "/src" else [Attr("inner.txt")]

        def get(self, remote, local, callback=None):
            with open(local, "wb") as fh:
                fh.write(remote.encode("utf-8"))
            if callback:
                callback(1, 1)

    dest = tmp_path / "dest"
    thread = sb._TransferThread(None, sb._Job("download", "/src", str(dest), True, 0))
    size, files = thread._download(Server(), "/src", str(dest))
    assert sorted(os.listdir(dest)) == sorted([
        ".._.._evil.txt", "C__x.txt", "a_b", "Case.txt", "case (2).txt", "_CON",
        "tail__", "sub"])
    assert (dest / "case (2).txt").read_bytes() == b"/src/case.txt"
    assert (dest / "sub" / "inner.txt").exists() and files == 8
    assert not (tmp_path / "evil.txt").exists()
    assert any("dev" in note and "character device" in note for note in thread.notes)
    assert any("saved as case (2).txt" in note for note in thread.notes)


def test_an_upload_asks_about_a_name_that_differs_only_in_case(remote, tmp_path,
                                                               monkeypatch):
    local = tmp_path / "ALPHA.TXT"
    local.write_bytes(b"new")
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1])
    monkeypatch.setattr(remote.handler, "_remote_os", "windows")
    assert remote.upload_paths([str(local)]) == 0
    assert asked and "ALPHA.TXT" in asked[0]
    asked.clear()
    monkeypatch.setattr(remote.handler, "_remote_os", "linux")   # tells them apart
    assert remote.upload_paths([str(local)]) == 1 and not asked
    assert wait_until(lambda: not remote.busy)


# ------------------------------------------------------------ 8: drags between browsers
def test_a_drop_from_another_browser_or_folder_is_refused(qapp, ssh, ssh_server, tmp_path):
    from turbossh.gui.file_browser import FileBrowser
    (ssh_server.root / "docs").mkdir()
    (ssh_server.root / "alpha.txt").write_bytes(b"alpha")
    browser = FileBrowser(ssh)
    browser.show()
    try:
        assert browser.local.navigate(str(tmp_path))
        assert wait_until(lambda: not browser.busy
                          and browser.remote.tree.topLevelItemCount() > 1)
        view, tree = browser.local.view, browser.remote.tree
        own = tree.mimeData([tree.topLevelItem(i) for i in range(tree.topLevelItemCount())])
        assert view.remote_names(own) == (["docs", "alpha.txt"], False)
        other = QMimeData()
        other.setData(sb.REMOTE_MIME, b"alpha.txt")
        other.setData(sb.REMOTE_SOURCE_MIME, b"another-browser\n/")
        bare = QMimeData()
        bare.setData(sb.REMOTE_MIME, b"alpha.txt")
        assert view.remote_names(other) == ([], True)
        assert view.remote_names(bare) == ([], True)

        dropped, refused = [], []
        view.remote_dropped.connect(dropped.append)
        view.refused.connect(refused.append)
        enter = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction, other, Qt.LeftButton,
                                Qt.NoModifier)
        enter.accept()
        view.dragEnterEvent(enter)
        assert not enter.isAccepted() and refused
        assert "another session" in browser.local.status.text()
        view.dropEvent(QDropEvent(QPointF(5, 5), Qt.CopyAction, other, Qt.LeftButton,
                                  Qt.NoModifier))
        assert dropped == []
        view.dropEvent(QDropEvent(QPointF(5, 5), Qt.CopyAction, own, Qt.LeftButton,
                                  Qt.NoModifier))
        assert dropped == [["docs", "alpha.txt"]]
        assert wait_until(lambda: not browser.busy)
        assert (tmp_path / "alpha.txt").read_bytes() == b"alpha"

        # rows dragged before the pane moved to another folder are stale
        assert browser.remote.navigate("/docs")
        assert wait_until(lambda: not browser.busy and browser.remote.cwd == "/docs")
        assert view.remote_names(own) == ([], True)
    finally:
        browser.close_browser()
        browser.deleteLater()
        QTest.qWait(50)


# ------------------------------------------------------------ 10: the recycle bin
@pytest.mark.skipif(not hasattr(QFile, "moveToTrash"), reason="needs Qt 5.15")
def test_a_delete_that_cannot_use_the_recycle_bin_asks_first(qapp, tmp_path, monkeypatch):
    from turbossh.gui.file_browser import LocalPane
    keep = tmp_path / "keep.txt"
    keep.write_text("x")
    threads, asked, gone = [], [], []

    def no_bin(path):
        threads.append(threading.current_thread() is threading.main_thread())
        return False
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(no_bin))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1])
    pane = LocalPane(str(tmp_path))
    try:
        pane.deleted.connect(gone.append)
        assert pane.do_delete([str(keep)])
        assert wait_until(lambda: gone == [0])
        assert keep.exists(), "deleted for good although the user said No"
        assert threads == [False], "the recycle bin was used on the GUI thread"
        assert "permanently" in asked[0] and "Not deleted" in pane.status.text()
    finally:
        pane.close_pane()
        pane.deleteLater()


# ------------------------------------------------------------ 11: the saved folder
def test_the_saved_local_folder_is_checked_off_the_gui_thread(qapp, tmp_path, monkeypatch):
    from turbossh.gui import file_browser
    saved = tmp_path / "saved"
    saved.mkdir()
    checks, real = [], os.path.isdir
    release = threading.Event()
    release.set()

    def isdir(path):
        if os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(str(saved)):
            checks.append(threading.current_thread() is threading.main_thread())
            release.wait(10)
        return real(path)
    monkeypatch.setattr(file_browser.os.path, "isdir", isdir)
    home = os.path.normcase(os.path.expanduser("~"))
    pane = file_browser.LocalPane(str(saved))
    try:
        assert os.path.normcase(pane.cwd) == home        # at first: home
        assert wait_until(lambda: os.path.normcase(pane.cwd) == os.path.normcase(str(saved)))
        assert checks and not any(checks), "the saved folder was looked at on the GUI thread"
    finally:
        pane.close_pane()
        pane.deleteLater()

    release.clear()                                      # a slow share...
    pane = file_browser.LocalPane(str(saved))
    try:
        assert pane.navigate(str(tmp_path))              # ...and the user moved on
        release.set()
        QTest.qWait(300)
        assert pane.cwd == str(tmp_path)
    finally:
        pane.close_pane()
        pane.deleteLater()


# ------------------------------------------------------------ 12: dead links
def test_a_request_that_times_out_drops_its_client(ssh):
    link = sb._OwnSftp(ssh)
    first = link.run(lambda sftp: sftp)

    def stuck(_sftp):
        raise socket.timeout("timed out")
    with pytest.raises(IOError, match="did not answer"):
        link.run(stuck)
    assert link.current() is None
    assert qtutil.wait_for_teardowns(10)
    assert first.get_channel().closed
    assert link.run(lambda sftp: sftp) is not first
    link.close()


def test_cancel_frees_a_transfer_stuck_on_a_dead_link(remote, tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "_ABORT_AFTER", 0.2)

    def dead_link(sftp, remote_path, local, callback=None):
        while not sftp.get_channel().closed:             # no data, no callback
            time.sleep(0.02)
        raise IOError("Socket is closed")
    monkeypatch.setattr(sb, "get_atomic", dead_link)
    assert remote.queue_download("/alpha.txt", str(tmp_path / "a.txt"))
    assert wait_until(lambda: remote._transfer is not None and remote._transfer.isRunning())
    QTest.qWait(100)
    started = time.monotonic()
    assert remote.cancel_transfer()
    assert wait_until(lambda: not remote.busy, 10)
    assert time.monotonic() - started < 8
    assert remote.transfers.items()[-1].status == tl.CANCELLED


# ------------------------------------------------------------ rebind (reconnect)
def test_rebind_keeps_the_queue_history_and_unsaved_edits(qapp, ssh, ssh_server, make_config,
                                                        tmp_path, monkeypatch):
    from turbossh import SSHHandler
    from turbossh.gui.file_browser import FileBrowser
    root = ssh_server.root
    (root / "alpha.txt").write_bytes(b"alpha\n")
    data = os.urandom(3 * 1024 * 1024)
    (root / "big.bin").write_bytes(data)
    throttle(monkeypatch)
    browser = FileBrowser(ssh)
    said = []
    browser.log.connect(said.append)
    browser.show()
    fresh = SSHHandler(make_config(), quiet=True)
    fresh.connect()
    try:
        assert browser.local.navigate(str(tmp_path))
        assert wait_until(lambda: not browser.busy
                          and browser.remote.tree.topLevelItemCount() > 1)
        pane = browser.remote
        assert pane.queue_download("/alpha.txt", str(tmp_path / "alpha.txt"))
        assert wait_until(lambda: not browser.busy)
        editor = open_editor(pane, "alpha.txt")
        editor.edit.setPlainText("edited\n")
        editor.edit.document().setModified(True)
        assert pane.queue_download("/big.bin", str(tmp_path / "big.bin"))
        assert pane.queue_download("/alpha.txt", str(tmp_path / "again.txt"))
        assert wait_until(lambda: browser.transfers.items()[1].moved > 0)
        old = (pane._remote, pane._xfer)

        browser.rebind(fresh)
        assert pane.handler is fresh and old[0]._shut and old[1]._shut
        assert editor._link is pane._remote and editor.has_unsaved_changes()
        assert wait_until(lambda: not browser.busy, 40)
        assert [i.status for i in browser.transfers] == [tl.DONE] * 3
        assert (tmp_path / "big.bin").read_bytes() == data
        assert any("starts again" in line for line in said)
        assert pane.tree.topLevelItemCount() > 1 and pane.cwd == "/"

        editor.save()
        assert wait_until(lambda: editor.status.text().startswith("Saved"))
        assert (root / "alpha.txt").read_bytes() == b"edited\n"
        editor.close()
    finally:
        browser.close_browser()
        browser.deleteLater()
        QTest.qWait(50)
        fresh.disconnect()


# ------------------------------------------------------------ the Camera tab
@pytest.fixture()
def panel(qapp, monkeypatch):
    from turbossh.gui import camera_widget
    monkeypatch.setattr(ffmpeg_tools, "cached_ffmpeg", lambda: sys.executable)
    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg",
                        lambda log=None, cancel=None: sys.executable)
    monkeypatch.setattr(ffmpeg_tools, "list_local_cameras", lambda ff: ["Cam A"])
    widget = camera_widget.CameraPanel()
    widget.show()
    assert wait_until(lambda: widget.camera.count() == 1)
    yield widget
    widget.close_session()
    widget.deleteLater()
    QTest.qWait(50)


def test_a_recording_whose_ffmpeg_quits_is_reported_at_once(panel, monkeypatch, tmp_path):
    from turbossh.gui import camera_widget
    warned = []
    monkeypatch.setattr(camera_widget.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(camera_widget.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "rec.mp4"), ""))
    reader = camera_widget._FrameReader(lambda n: b"")
    reader.start()
    panel.reader = reader
    try:
        panel._toggle_record()       # "ffmpeg" is Python here: it rejects -y at once
        assert panel._rec_proc is not None
        assert wait_until(lambda: warned, 15), "still 'Recording…' after ffmpeg quit"
        assert panel._rec_proc is None and panel.status.text() == "Recording failed"
        assert panel.rec_btn.text() == "⏺ Record"
    finally:
        reader.stop()
        panel.reader = None


def test_a_finishing_recording_is_never_killed_while_it_still_writes(tmp_path):
    from turbossh.gui.camera_widget import _wait_for_recorder
    path = tmp_path / "rec.mp4"
    path.write_bytes(b"")
    grown = []

    class Proc:
        calls, killed_at = 0, None

        def wait(self, timeout=None):
            if self.killed_at is not None:
                return -9
            self.calls += 1
            if self.calls <= 8:                          # still writing its trailer
                with open(path, "ab") as fh:
                    fh.write(b"x")
                grown.append(time.monotonic())
            time.sleep(0.05)
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

        def kill(self):
            self.killed_at = time.monotonic()
    proc = Proc()
    _wait_for_recorder(proc, str(path), stall=0.6)
    assert proc.calls > 8 and proc.killed_at is not None
    assert proc.killed_at - grown[-1] >= 0.6, "killed while the file was growing"

    class Quick:
        def __init__(self):
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls < 3:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return 0

        def kill(self):
            pytest.fail("a finished recording was killed")
    _wait_for_recorder(Quick(), str(path), stall=0.1)


def test_recordings_are_fragmented_mp4_without_a_final_rewrite():
    from turbossh.gui import camera_widget
    flags = camera_widget._REC_ARGS[camera_widget._REC_ARGS.index("-movflags") + 1]
    assert "frag_keyframe" in flags and "faststart" not in flags


def test_failed_starts_close_their_stderr_files(panel, monkeypatch, tmp_path):
    from turbossh.gui import camera_widget
    made, real = [], camera_widget.tempfile.TemporaryFile

    def temp(*a, **k):
        made.append(real())
        return made[-1]

    def cannot_run(*a, **k):
        raise OSError("cannot run")
    monkeypatch.setattr(camera_widget.tempfile, "TemporaryFile", temp)
    monkeypatch.setattr(camera_widget.subprocess, "Popen", cannot_run)
    monkeypatch.setattr(camera_widget.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(camera_widget.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "rec.mp4"), ""))
    panel._start_local("Cam A", 640, 480, 15)
    assert made[-1].closed and panel._proc_err is None and panel.reader is None
    panel.reader = camera_widget._FrameReader(lambda n: None)
    try:
        panel._toggle_record()
        assert len(made) == 2 and made[-1].closed and panel._rec_err is None
    finally:
        panel.reader = None


def test_stop_closes_the_remote_channels_off_the_gui_thread(panel):
    on_gui = []

    class Channel:
        def close(self):
            on_gui.append(threading.current_thread() is threading.main_thread())

    class Handler:
        def webcam_release(self, **_k):
            on_gui.append(threading.current_thread() is threading.main_thread())

        def disconnect(self):
            pass
    chan = Channel()
    chan._ffmpeg_exec = Channel()
    panel._handler, panel._chan = Handler(), chan
    panel._stop_stream()
    assert qtutil.wait_for_teardowns(10)
    assert on_gui == [False, False, False]               # two closes, one release
    panel._handler = None


def test_dropping_a_scan_cancels_its_download(panel, monkeypatch):
    from turbossh.gui import camera_widget
    started = threading.Event()

    def slow(log=None, cancel=None):
        started.set()
        if cancel.wait(10):
            raise RuntimeError("The ffmpeg download was cancelled.")
        return "never cancelled"
    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", slow)
    prep = camera_widget._LocalPrep()
    panel._start_prep(prep, panel._local_ready)
    assert started.wait(5)
    panel._drop_prep()                                   # e.g. the source changed
    assert prep.cancel.is_set()
    assert wait_until(lambda: not prep.isRunning(), 5)


def test_the_remote_ffmpeg_upload_can_be_cancelled(ssh, ssh_server, tmp_path, monkeypatch):
    local = tmp_path / "ffmpeg.exe"
    local.write_bytes(os.urandom(200_000))
    cancel = threading.Event()
    push = ssh.push

    def cancelled_midway(src, dst, safe=None, callback=None):
        def progress(done, total):
            cancel.set()                                 # the tab was closed
            callback(done, total)
        return push(src, dst, safe=safe, callback=progress)
    monkeypatch.setattr(ssh, "push", cancelled_midway)
    with pytest.raises(RuntimeError, match="cancelled"):
        ffmpeg_tools.ensure_remote_ffmpeg(ssh, str(local), remote_dir="/cam", cancel=cancel)
    leftovers = os.listdir(ssh_server.root / "cam") if (ssh_server.root / "cam").exists() else []
    assert "ffmpeg.exe" not in leftovers and "ffmpeg.exe.part" not in leftovers
    with pytest.raises(RuntimeError, match="cancelled"):        # before it starts
        ffmpeg_tools.ensure_remote_ffmpeg(ssh, str(local), remote_dir="/cam", cancel=cancel)


def test_force_is_not_the_default_and_says_what_it_stops(panel, monkeypatch):
    from turbossh.gui import camera_widget
    asked = []

    def question(_parent, _title, text, _buttons=None, default=None):
        asked.append((text, default))
        return camera_widget.QMessageBox.No
    monkeypatch.setattr(camera_widget.QMessageBox, "question", question)
    monkeypatch.setattr(camera_widget.QMessageBox, "warning", lambda *a, **k: None)
    panel._handler = object()
    panel._last_remote = ("Cam A", 640, 480, 15, False)
    try:
        panel._remote_start_fail(panel._stream_gen, "Could not run graph: device in use")
        text, default = asked[0]
        assert default == camera_widget.QMessageBox.No
        assert "EVERY TurboSSH camera stream" in text
        panel._remote_no_video("Cam A", "no frames", probed=False)
        text, default = asked[1]
        assert default == camera_widget.QMessageBox.No
        assert "EVERY TurboSSH camera stream" in text
    finally:
        panel._handler = None


def test_the_probe_keeps_the_camera_name_inside_its_quotes(qapp):
    from turbossh.gui.camera_widget import _RemoteProbe
    sent = []

    class Channel:
        def settimeout(self, _t):
            pass

        def exec_command(self, cmd):
            sent.append(cmd)

        def recv_stderr_ready(self):
            return False

        def recv_ready(self):
            return False

        def exit_status_ready(self):
            return True

        def close(self):
            pass

    class Transport:
        def open_session(self, timeout=None):
            return Channel()

    class Handler:                          # what SSHHandler offers the probe
        transport = Transport()

        def _cmd_line(self, line):          # cmd.exe default shell: unchanged
            return line
    probe = _RemoteProbe(Handler(), "C:\\t\\ffmpeg.exe", 'Cam "1" %TEMP% $x`y')
    probe.run()
    assert 'video="Cam 1 TEMP xy"' in sent[0]


def test_ffmpeg_is_never_taken_from_the_current_folder(tmp_path, monkeypatch):
    here, elsewhere = tmp_path / "here", tmp_path / "bin"
    here.mkdir()
    elsewhere.mkdir()
    exe = here / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    exe.write_bytes(b"MZ")
    os.chmod(str(exe), 0o755)
    monkeypatch.chdir(str(here))
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "relative", str(elsewhere)]))
    assert ffmpeg_tools._which("ffmpeg") is None
    monkeypatch.setenv("PATH", str(here))
    assert ffmpeg_tools._which("ffmpeg") == str(exe)


# ------------------------------------------------------------ follow-ups with the core fixes
def test_replacing_the_remote_ffmpeg_force_releases_it():
    """webcam_release() now stops only the streams the calling handler started;
    replacing the exe on a fresh connection must stop any ffmpeg running it."""
    from turbossh.gui import ffmpeg_tools
    calls = []

    class Ssh:
        def __init__(self):
            self.renamed = False

        def rename(self, part, dest, safe=False):
            if not self.renamed:
                self.renamed = True
                raise OSError("in use")

        def webcam_release(self, **kwargs):
            calls.append(kwargs)

        def remove(self, path, safe=False):
            pass

    ffmpeg_tools._replace_remote(Ssh(), "C:/t/ffmpeg.part", "C:/t/ffmpeg.exe")
    assert calls and calls[0].get("force") is True


def test_camera_probes_use_command_lines_for_the_remote_shell(qapp):
    """The probe's and the device listing's ffmpeg lines are cmd.exe syntax; they
    go through the handler's _cmd_line so a PowerShell default shell works too."""
    from turbossh.gui import camera_widget
    wrapped, opened = [], []

    class Chan:
        def settimeout(self, _t):
            pass

        def exec_command(self, cmd):
            opened.append(cmd)

        def exit_status_ready(self):
            return True

        def recv_stderr_ready(self):
            return False

        def recv_ready(self):
            return False

        def close(self):
            pass

    class Handler:
        transport = type("T", (), {"open_session": staticmethod(lambda timeout=None: Chan())})()

        def _cmd_line(self, line):
            wrapped.append(line)
            return "WRAPPED " + line

    probe = camera_widget._RemoteProbe(Handler(), "C:/t/ffmpeg.exe", 'Cam "1"')
    probe.run()
    assert wrapped and wrapped[0].startswith('"C:/t/ffmpeg.exe"')
    assert opened == ["WRAPPED " + wrapped[0]]
    assert 'video="Cam 1"' in wrapped[0]          # the quote was stripped from the name
