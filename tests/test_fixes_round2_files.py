"""Regression tests for the second review of the 2.1.0 file-browser fixes: what
the first fix pass broke that worked in 2.0.0. No network, device or download:
the loopback SSH/SFTP server and fakes stand in."""

from __future__ import annotations

import errno
import posixpath
import stat
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QFile, QPoint  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QMenu, QMessageBox  # noqa: E402

from turbossh.gui import transfer_log as tl  # noqa: E402
from turbossh.gui import sftp_browser as sb  # noqa: E402


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(20)
    return predicate()


def select(widget, *names):
    tree = widget.tree
    tree.clearSelection()
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        item.setSelected(item.text(0) in names)


def row(widget, name):
    tree = widget.tree
    return next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).text(0) == name)


@pytest.fixture()
def remote(qapp, ssh, ssh_server):
    root = ssh_server.root
    (root / "docs").mkdir()
    (root / "alpha.txt").write_bytes(b"alpha\n")
    widget = sb.SftpBrowser(ssh)
    widget.messages = []
    widget.log.connect(widget.messages.append)
    assert wait_until(lambda: not widget.busy and widget.tree.topLevelItemCount() > 1)
    yield widget
    widget.close_browser()
    widget.deleteLater()
    QTest.qWait(50)


@pytest.fixture()
def fresh(make_config):
    """A second connection to the same server: what a reconnect hands over."""
    from turbossh import SSHHandler
    handler = SSHHandler(make_config(), quiet=True)
    handler.connect()
    yield handler
    handler.disconnect()


# ------------------------------------------------------------ 1: the recycle bin
needs_trash = pytest.mark.skipif(not hasattr(QFile, "moveToTrash"), reason="needs Qt 5.15")


@needs_trash
def test_a_failed_recycle_is_not_counted_as_moved_with_the_real_qt(qapp, tmp_path):
    """PyQt5's static QFile.moveToTrash returns (ok, path in the bin), and
    (False, '') is true: every failed move counted as done."""
    from turbossh.gui.file_browser import _trash
    missing = str(tmp_path / "gone" / "nothing.txt")
    moved, left = _trash([missing])
    assert moved == 0, "a file that was not recycled was counted as moved"
    assert [path for path, _why in left] == [missing]


@needs_trash
@pytest.mark.parametrize("answer, moved", [((False, ""), 0), (False, 0),
                                           ((True, "C:/$Recycle.Bin/x"), 1), (True, 1)])
def test_moving_to_the_trash_reads_a_tuple_or_a_bool(qapp, tmp_path, monkeypatch,
                                                      answer, moved):
    from turbossh.gui.file_browser import _trash
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda path: answer))
    done, left = _trash([str(tmp_path / "x.txt")])
    assert (done, len(left)) == (moved, 1 - moved)


@needs_trash
def test_a_locked_file_that_is_not_recycled_asks_before_deleting_it(qapp, tmp_path,
                                                                    monkeypatch):
    from turbossh.gui.file_browser import LocalPane
    locked = tmp_path / "locked.log"
    locked.write_text("in use")
    asked, gone = [], []
    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(lambda path: (False, "")))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: (asked.append(a[2]), QMessageBox.No)[1])
    pane = LocalPane(str(tmp_path))
    try:
        pane.deleted.connect(gone.append)
        assert pane.do_delete([str(locked)])
        assert wait_until(lambda: gone)
        assert gone == [0], "a file still there was reported deleted"
        assert asked and "permanently" in asked[0], "the question never came"
        assert locked.exists() and "Not deleted" in pane.status.text()
    finally:
        pane.close_pane()
        pane.deleteLater()


# ------------------------------------------------------------ 2: cancel vs. reconnect
def held_download(monkeypatch):
    """A download that moves one block, then waits for *release* (a link slower
    than the user's Cancel), then moves the next block — where a cancel lands."""
    release, calls = threading.Event(), []

    def fake(sftp, remote_path, local, callback=None, *args, **kwargs):
        calls.append(remote_path)
        time.sleep(sb._PROGRESS_EVERY * 1.5)            # so the progress shows
        callback(1, 10)
        release.wait(20)
        callback(10, 10)
        with open(local, "wb") as fh:
            fh.write(b"x" * 10)
        return 10
    monkeypatch.setattr(sb, "get_atomic", fake)
    return release, calls


def test_a_reconnect_does_not_restart_a_transfer_the_user_cancelled(
        remote, fresh, tmp_path, monkeypatch):
    release, calls = held_download(monkeypatch)
    target = tmp_path / "alpha.txt"
    assert remote.queue_download("/alpha.txt", str(target))
    assert wait_until(lambda: remote.transfers.items()[-1].status == tl.RUNNING
                      and remote.transfers.items()[-1].moved > 0)
    assert remote.cancel_transfer()
    remote.rebind(fresh)                 # the link came back within the cancel
    assert "Reconnected" not in remote.xfer_label.text()
    release.set()
    assert wait_until(lambda: not remote.busy)
    assert calls == ["/alpha.txt"], "the cancelled transfer ran again"
    assert remote.transfers.items()[-1].status == tl.CANCELLED
    assert not target.exists()
    assert not any("starts again" in m for m in remote.messages)


def test_cancel_during_a_reconnect_restart_stops_the_transfer(
        remote, fresh, tmp_path, monkeypatch):
    release, calls = held_download(monkeypatch)
    target = tmp_path / "alpha.txt"
    assert remote.queue_download("/alpha.txt", str(target))
    assert wait_until(lambda: remote.transfers.items()[-1].status == tl.RUNNING
                      and remote.transfers.items()[-1].moved > 0)
    remote.rebind(fresh)
    assert "Reconnected" in remote.xfer_label.text()
    assert remote.cancel_transfer()      # ...and the user does not want it after all
    release.set()
    assert wait_until(lambda: not remote.busy)
    assert calls == ["/alpha.txt"], "the cancel was overruled by the restart"
    assert remote.transfers.items()[-1].status == tl.CANCELLED
    assert not target.exists()


# ------------------------------------------------------------ 3: a slow cancel
def test_cancel_lets_a_slow_but_live_transfer_settle(remote, tmp_path, monkeypatch):
    """Closing the file after a cancel waits for what is still in flight (up to
    ~2 MB: seconds on a slow VPN). Its client was closed under it after 2 s, so
    the temporary file could not be removed."""
    monkeypatch.setattr(sb, "_ABORT_AFTER", 0.2)
    seen = []

    def slow_link(sftp, remote_path, local, callback=None, *args, **kwargs):
        done = 0
        try:
            while True:
                time.sleep(0.02)
                done += 32768
                callback(done, 64 * 1024 * 1024)
        except sb._Cancelled:
            settle = time.monotonic() + 1.5          # the in-flight data drains
            while time.monotonic() < settle and not sftp.get_channel().closed:
                time.sleep(0.02)
            try:
                sftp.stat("/")                       # the clean-up after it
                seen.append("cleaned up")
            except Exception as exc:                 # noqa: BLE001 - recorded
                seen.append(f"cut off: {exc!r}")
            raise
    monkeypatch.setattr(sb, "get_atomic", slow_link)
    assert remote.queue_download("/alpha.txt", str(tmp_path / "a.txt"))
    assert wait_until(lambda: remote.transfers.items()[-1].moved > 0)
    assert remote.cancel_transfer()
    assert wait_until(lambda: not remote.busy, 15)
    assert seen == ["cleaned up"], "the client was closed under a live transfer"
    assert remote.transfers.items()[-1].status == tl.CANCELLED


def test_a_transfer_that_went_quiet_before_the_cancel_is_freed_at_once(
        remote, tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "_ABORT_AFTER", 0.2)
    monkeypatch.setattr(sb, "_STALL_AFTER", 1.0)

    def dies(sftp, remote_path, local, callback=None, *args, **kwargs):
        time.sleep(sb._PROGRESS_EVERY * 1.5)            # so the progress shows
        callback(32768, 65536)
        while not sftp.get_channel().closed:          # the link died: no more data
            time.sleep(0.02)
        raise IOError("Socket is closed")
    monkeypatch.setattr(sb, "get_atomic", dies)
    assert remote.queue_download("/alpha.txt", str(tmp_path / "a.txt"))
    assert wait_until(lambda: remote.transfers.items()[-1].moved > 0)
    QTest.qWait(1300)                    # quiet for longer than _STALL_AFTER
    started = time.monotonic()
    assert remote.cancel_transfer()
    assert wait_until(lambda: not remote.busy, 10)
    assert time.monotonic() - started < 1.0, "a dead link was given more time"
    assert remote.transfers.items()[-1].status == tl.CANCELLED


def test_how_long_a_cancelled_transfer_may_stay_quiet(qapp):
    t = sb._TransferThread(None, sb._Job("download", "/r", "C:/l", False, 0))
    heard = t.heard
    # nothing measured yet: the floor
    assert not t.stalled(heard + sb._STALL_AFTER - 0.1)
    assert t.stalled(heard + sb._STALL_AFTER)
    # a slow link (1 Mbit/s) needs ~16 s to settle 2 MB in flight: it gets them
    t.pace = 128 * 1024
    assert not t.stalled(heard + sb._STALL_AFTER + 2 * 1024 * 1024 / t.pace - 0.5)
    assert t.stalled(heard + sb._SFTP_TIMEOUT)
    # however slow, never longer than a request waits for an answer anyway
    t.pace = 100
    assert t.stalled(heard + sb._SFTP_TIMEOUT)
    t.deleteLater()


def test_progress_is_what_tells_a_live_transfer_from_a_stuck_one(qapp):
    t = sb._TransferThread(None, sb._Job("download", "/r", "C:/l", False, 0))
    t.heard = 0.0
    t._on_file("/r")
    assert t.heard > 0.0
    t.heard = 0.0
    t._on_bytes(65536, 131072)
    assert t.heard > 0.0
    t.heard = 0.0
    t.cancel()
    with pytest.raises(sb._Cancelled):
        t._on_bytes(131072, 131072)      # the block the cancel lands on: alive too
    assert t.heard > 0.0
    t.deleteLater()


# ------------------------------------------------------------ 4: unknown file types
@pytest.mark.parametrize("mode", [0, None])
def test_a_file_the_server_gives_no_type_for_opens_in_the_editor(mode):
    """A server that sends no st_mode: 2.0.0 opened the file; the read is
    bounded now, which is what protects against a device."""
    from turbossh.gui import remote_edit
    asked = []

    class Attr:
        st_mode, st_size, st_mtime = mode, 6, 1

    class Handle:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            asked.append(n)
            return b"hello\n"

    class Server:
        def stat(self, path):
            return Attr()

        def open(self, path, mode="r"):
            return Handle()
    loaded = remote_edit.read_remote_text(Server(), "/flash/config.txt")
    assert loaded.text == "hello\n" and not loaded.problem
    assert asked == [remote_edit.MAX_EDIT_BYTES + 1]


def test_an_entry_of_unknown_type_offers_the_editor(remote, monkeypatch):
    menus = []
    monkeypatch.setattr(QMenu, "exec_",
                        lambda self, *a: menus.append([x.text() for x in self.actions()]))
    entry = remote._entries["alpha.txt"]
    remote._entries["alpha.txt"] = entry._replace(ftype=0)
    select(remote, "alpha.txt")
    remote._ctx_menu(QPoint(1, 1))
    assert "Edit / view…" in menus[-1]
    remote._open_item(row(remote, "alpha.txt"))             # double-click
    editors = remote._live_editors()
    assert len(editors) == 1, "double-click did not open the editor"
    dialog = editors[0]
    assert wait_until(lambda: not dialog.edit.isReadOnly())
    assert dialog.edit.toPlainText() == "alpha\n"
    dialog.close()

    # known kinds that are not files are still refused
    remote._entries["alpha.txt"] = entry._replace(ftype=stat.S_IFIFO)
    remote._ctx_menu(QPoint(1, 1))
    assert "Edit / view…" not in menus[-1]
    assert remote.edit_selected() is None and "named pipe" in remote.status.text()


# ------------------------------------------------------------ 5: following links
class LinkServer:
    """The link calls of an SFTP client over a made-up tree. /a is a symlinked
    folder (-> /b/c), and /b/c/x a relative link up out of it (-> ../x): the
    file behind /a/x is /b/x, where the kernel would look — not /x."""

    def __init__(self, links, files, dirs, realpath=True):
        self.links, self.files, self.dirs = links, files, dirs
        self.realpath = realpath
        self.calls = []

    def _resolve(self, path, follow_last=True, depth=0):
        if depth > 20:
            raise IOError(errno.ELOOP, "Too many levels of symbolic links")
        parts = [p for p in path.split("/") if p not in ("", ".")]
        here = "/"
        for i, part in enumerate(parts):
            if part == "..":
                here = posixpath.dirname(here)
                continue
            there = posixpath.join(here, part)
            if there in self.links and (follow_last or i < len(parts) - 1):
                target = self.links[there]
                there = self._resolve(target if target.startswith("/")
                                      else posixpath.join(here, target), True, depth + 1)
            here = there
        return here

    def lstat(self, path):
        self.calls.append(("lstat", path))
        real = self._resolve(path, follow_last=False)
        attr = type("Attr", (), {})()
        if real in self.links:
            attr.st_mode = stat.S_IFLNK | 0o777
        elif real in self.files:
            attr.st_mode = stat.S_IFREG | 0o644
        elif real in self.dirs:
            attr.st_mode = stat.S_IFDIR | 0o755
        else:
            raise IOError(errno.ENOENT, "No such file")
        return attr

    def readlink(self, path):
        self.calls.append(("readlink", path))
        return self.links[self._resolve(path, follow_last=False)]

    def normalize(self, path):
        self.calls.append(("normalize", path))
        if not self.realpath:
            raise IOError(errno.EOPNOTSUPP, "Operation unsupported")
        return self._resolve(path)


def tree(**kw):
    return LinkServer({"/a": "/b/c", "/b/c/x": "../x"},
                      {"/b/x", "/x"}, {"/", "/b", "/b/c"}, **kw)


def test_a_relative_link_inside_a_symlinked_folder_is_followed_where_it_points():
    server = tree()
    path, attr = sb.resolve_remote_link(server, "/a/x")
    assert path == "/b/x", "the link was resolved as text, past the linked folder"
    assert stat.S_ISREG(attr.st_mode)


def test_a_path_that_is_no_link_costs_no_realpath():
    server = tree()
    assert sb.resolve_remote_link(server, "/b/x")[0] == "/b/x"
    assert [c for c, _p in server.calls] == ["lstat"]


def test_a_link_the_server_cannot_resolve_is_kept_and_written_through():
    """No realpath (or a dangling link, which realpath refuses): the link itself
    comes back — writing to it goes through it on the server, which is always
    where it points; a target joined on as text could be the wrong file."""
    server = tree(realpath=False)
    path, attr = sb.resolve_remote_link(server, "/b/c/x")
    assert path == "/b/c/x" and stat.S_ISLNK(attr.st_mode)
    loop = LinkServer({"/l1": "l2", "/l2": "l1"}, set(), {"/"}, realpath=False)
    path, attr = sb.resolve_remote_link(loop, "/l1")
    assert path == "/l1" and stat.S_ISLNK(attr.st_mode)


def test_a_dangling_link_is_kept_as_the_link():
    server = LinkServer({"/a": "/b/c", "/b/c/new": "../new"}, set(), {"/", "/b", "/b/c"})

    def no_target(path):
        server.calls.append(("normalize", path))
        raise IOError(errno.ENOENT, "No such file")   # what OpenSSH's realpath says
    server.normalize = no_target
    path, attr = sb.resolve_remote_link(server, "/a/new")
    assert path == "/a/new" and stat.S_ISLNK(attr.st_mode)


def test_an_editor_save_through_an_unresolved_link_compares_what_it_points_at():
    """The editor loaded the file through the link (stat follows it): its save
    must compare that, not the link's own lstat — or every save on a server
    without realpath says "changed on the remote"."""
    from turbossh.gui import remote_edit
    server = tree(realpath=False)
    target_attr = type("Attr", (), {"st_mode": stat.S_IFREG | 0o644,
                                     "st_size": 5, "st_mtime": 100})()
    server.stat = lambda path: target_attr
    written = []

    def put_data(sftp, data, remote, existing=None, mode=None):
        written.append((remote, data))
        return ""
    import turbossh._transfer as transfer
    original = transfer.put_data
    transfer.put_data = put_data
    try:
        size, _stamp, how = remote_edit.write_remote_text(
            server, "/b/c/x", b"hello", stamp=(5, 100))
    finally:
        transfer.put_data = original
    assert size == 5 and written == [("/b/c/x", b"hello")]
