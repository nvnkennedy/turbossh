"""The Files tab laid out as TurboADB's: quick links, the two panes with the
same columns, ".." and <DIR>, Push / Pull, New tab — and the theme's tab
close buttons and check boxes, which must be plain to see in every theme."""

from __future__ import annotations

import os
import stat
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QCheckBox, QTabBar, QTabWidget,  # noqa: E402
                             QWidget)

from turbossh.gui import sftp_browser as sb  # noqa: E402

COLUMNS = ["Name", "Size", "Date Modified", "Permissions", "Owner"]


def wait_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(20)
    return predicate()


def rows(tree):
    return [[tree.topLevelItem(i).text(c) for c in range(tree.columnCount())]
            for i in range(tree.topLevelItemCount())]


def names(tree):
    return [r[0] for r in rows(tree)]


def same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


@pytest.fixture()
def browser(qapp, ssh, ssh_server, tmp_path):
    from turbossh.gui.file_browser import FileBrowser
    root = ssh_server.root
    (root / "docs").mkdir()
    (root / "tmp").mkdir()
    (root / "alpha.txt").write_text("alpha")
    local = tmp_path / "pc"
    (local / "folder").mkdir(parents=True)
    (local / "note.txt").write_text("note")
    (local / "big.bin").write_bytes(b"x" * 3000)
    widget = FileBrowser(ssh)
    widget.resize(1200, 600)
    widget.show()
    assert widget.local.navigate(str(local))
    assert wait_until(lambda: not widget.busy and not widget.local._loading
                      and widget.remote.tree.topLevelItemCount() > 1)
    widget.local_dir = local
    yield widget
    widget.close_browser()
    widget.deleteLater()
    QTest.qWait(50)


def local_tree(widget):
    return widget.local.view


def select(tree, *wanted):
    tree.clearSelection()
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        item.setSelected(item.text(0) in wanted)


# ------------------------------------------------------------------ layout
def test_both_panes_show_the_same_columns_dirs_and_parent_row(browser):
    for tree in (browser.local.view, browser.remote.tree):
        header = tree.headerItem()
        assert [header.text(c) for c in range(tree.columnCount())] == COLUMNS
        assert tree.alternatingRowColors() and tree.objectName() == "fileList"
        assert not tree.rootIsDecorated()
        assert tree.topLevelItem(0).text(0) == ".."     # on top

    local = rows(browser.local.view)
    assert [r[0] for r in local] == ["..", "folder", "big.bin", "note.txt"]
    assert local[1][1] == sb.DIR_SIZE and local[2][1] == "2.9 KB"
    assert all(len(r[3]) == 3 and r[3].isdigit() for r in local[1:])   # "755"
    assert local[1][2].count("-") == 2 and ":" in local[1][2]          # a date

    remote = rows(browser.remote.tree)
    assert [r[0] for r in remote] == ["..", "docs", "tmp", "alpha.txt"]
    assert remote[1][1] == sb.DIR_SIZE and remote[3][1] == "5 B"
    assert remote[3][3].startswith("-r") and remote[3][4] != ""        # owner
    # a listing leaves ".." current, not selected
    assert browser.remote.tree.currentItem().text(0) == ".."
    assert not browser.remote.tree.selectedItems()
    assert browser.local.status.text() == "1 folder, 2 files"
    assert browser.remote.status.text() == "2 folders, 1 file"


def test_quick_links_push_pull_and_titles(browser):
    assert browser.remote.title.text().endswith("@127.0.0.1")
    assert browser.local.title.text() == "This PC"
    assert browser.push_btn.text() == "Push" and browser.push_btn.property("tone") == "blue"
    assert browser.pull_btn.text() == "Pull" and browser.pull_btn.property("tone") == "green"
    assert browser.push_btn.layoutDirection() == Qt.RightToLeft        # "Push →"
    labels = [b.text() for b in browser.remote_links]
    assert labels == ["Home", "/", "/tmp"]

    tmp = browser.remote_links[2]
    tmp.click()
    assert wait_until(lambda: not browser.busy and browser.remote.cwd == "/tmp")
    assert names(browser.remote.tree) == [".."]
    # the links are locked while the remote pane is listing
    assert browser.remote.navigate("/")
    assert not tmp.isEnabled()
    assert wait_until(lambda: not browser.busy and tmp.isEnabled())


def test_push_and_pull_move_the_selection(browser, ssh_server):
    select(browser.local.view, "note.txt", "folder")
    browser.push_btn.click()
    assert wait_until(lambda: not browser.busy)
    assert (ssh_server.root / "note.txt").read_text() == "note"
    assert (ssh_server.root / "folder").is_dir()

    assert wait_until(lambda: "alpha.txt" in names(browser.remote.tree))
    select(browser.remote.tree, "alpha.txt")
    browser.pull_btn.click()
    assert wait_until(lambda: not browser.busy)
    assert (browser.local_dir / "alpha.txt").read_text() == "alpha"
    # the local pane shows what arrived by itself
    assert wait_until(lambda: "alpha.txt" in names(browser.local.view))

    browser.remote.tree.clearSelection()
    assert browser.download() == 0
    assert "Select the remote" in browser.remote.status.text()


def test_new_tab_shows_only_where_something_opens_it(browser):
    assert not browser.new_tab_btn.isVisible()
    asked = []
    browser.new_tab_requested.connect(lambda remote, local: asked.append((remote, local)))
    browser.hide()
    browser.show()
    assert browser.new_tab_btn.isVisible()
    browser.new_tab_btn.click()
    assert asked == [("/", browser.local.cwd)]


# ------------------------------------------------------------------ the local pane
def test_local_pane_up_open_and_sort_keep_parent_and_folders_first(browser):
    pane = browser.local
    tree = pane.view
    item = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).text(0) == "folder")
    pane._open_item(item)
    assert wait_until(lambda: not pane._loading)
    assert same(pane.cwd, browser.local_dir / "folder")
    assert names(tree) == [".."]
    pane._open_item(tree.topLevelItem(0))                 # ".." goes up...
    assert wait_until(lambda: not pane._loading)
    assert same(pane.cwd, browser.local_dir)
    assert tree.currentItem().text(0) == "folder"        # ...onto where it was

    tree.sortByColumn(1, Qt.DescendingOrder)              # size, largest first
    assert names(tree) == ["..", "folder", "big.bin", "note.txt"]
    tree.sortByColumn(0, Qt.DescendingOrder)
    assert names(tree) == ["..", "folder", "note.txt", "big.bin"]


def test_local_pane_drags_out_copies_of_files_never_the_parent(browser):
    view = browser.local.view
    items = [view.topLevelItem(i) for i in range(view.topLevelItemCount())]
    mime = view.mimeData(items)
    paths = sorted(os.path.basename(u.toLocalFile()) for u in mime.urls())
    assert paths == ["big.bin", "folder", "note.txt"]
    assert view.supportedDropActions() == Qt.CopyAction  # never a move to Explorer


def test_local_pane_follows_the_folder_and_keeps_the_selection(browser):
    pane = browser.local
    select(pane.view, "note.txt")
    (browser.local_dir / "fresh.txt").write_text("new")
    assert wait_until(lambda: "fresh.txt" in names(pane.view))
    assert [it.text(0) for it in pane.view.selectedItems()] == ["note.txt"]
    assert pane.selection_paths() == [str(browser.local_dir / "note.txt")]


def test_local_pane_hides_hidden_files(qapp, tmp_path):
    from turbossh.gui.file_browser import scan_folder
    (tmp_path / "shown.txt").write_text("x")
    hidden = tmp_path / ("hidden.txt" if os.name == "nt" else ".hidden")
    hidden.write_text("x")
    if os.name == "nt":
        import ctypes
        assert ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
    assert [e.name for e in scan_folder(str(tmp_path))] == ["shown.txt"]


def test_a_folder_that_cannot_be_listed_says_so_and_keeps_the_way_up(browser,
                                                                      monkeypatch):
    from turbossh.gui import file_browser
    pane = browser.local
    assert pane.navigate(str(browser.local_dir / "folder"))
    assert wait_until(lambda: not pane._loading)

    def denied(path):
        raise PermissionError(13, "Access is denied", path)
    monkeypatch.setattr(file_browser, "scan_folder", denied)
    pane.refresh()
    assert wait_until(lambda: not pane._loading)
    assert "Cannot list" in pane.status.text()
    assert names(pane.view) == [".."]


@pytest.mark.skipif(os.name != "nt", reason="drive letters")
def test_the_drive_list_and_the_drive_box(browser):
    from turbossh.gui.file_browser import drive_of, drives
    pane = browser.local
    root = drive_of(str(browser.local_dir))
    assert root in drives()
    assert pane.drive.currentText() == root
    assert pane.navigate(root)
    pane.up()                                             # above C:\ : the drives
    assert pane.cwd == ""
    assert wait_until(lambda: not pane._loading)
    shown = names(pane.view)
    assert ".." not in shown and root in shown
    assert pane.view.currentItem().text(0) == root        # where it came from
    assert pane.drive.currentIndex() == -1
    assert browser.download() == 0 and "local folder" in pane.status.text()


def test_local_permissions_and_remote_owner_texts():
    from turbossh.gui.file_browser import _local_perms
    assert _local_perms(0o40755) == "755" and _local_perms(0o100444) == "444"
    assert _local_perms(None) == ""

    class Attr:
        def __init__(self, longname=None, uid=None):
            self.longname, self.st_uid = longname, uid
    assert sb._owner(Attr("-rw-r--r--    1 root     wheel   12 Jan  1 00:00 a")) == "root"
    assert sb._owner(Attr("drwxr-x---   2 u0_a272  u0_a272 4096 Nov 3 22:06 DCIM")) == "u0_a272"
    assert sb._owner(Attr("just-a-name", uid=1000)) == "1000"
    assert sb._owner(Attr(b"-rw-------   1 pi  pi  1 Jan 1 2024 x")) == "pi"
    assert sb._owner(Attr()) == ""


# ------------------------------------------------------------------ theme
def luminance(color: QColor) -> float:
    def channel(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(color.red()) + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue()))


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(QColor(a)), luminance(QColor(b))), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.fixture()
def styled(qapp):
    """Apply a theme to the whole app for one test, then put back what was."""
    from turbossh.gui import theme
    before = qapp.styleSheet()

    def apply(name):
        theme.set_active(name)
        qapp.setStyleSheet(theme.stylesheet(name))
    yield apply
    theme.set_active(None)
    qapp.setStyleSheet(before)


def visible_pixels(image: QImage, background: QColor, minimum=2.5) -> int:
    """Pixels that stand out from *background* by at least *minimum* contrast."""
    count = 0
    bg = background.name()
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            if c.alpha() > 128 and contrast(c.name(), bg) >= minimum:
                count += 1
    return count


@pytest.mark.parametrize("name", ["dark", "light", "midnight", "paper"])
def test_tab_close_buttons_are_plain_to_see(qapp, styled, name):
    from turbossh.gui import theme
    styled(name)
    tabs = QTabWidget()
    tabs.setTabsClosable(True)
    for title in ("one", "two"):
        tabs.addTab(QWidget(), title)
    tabs.resize(400, 120)
    tabs.show()
    QTest.qWait(50)
    bar = tabs.tabBar()
    side = QTabBar.ButtonPosition(
        bar.style().styleHint(bar.style().SH_TabBar_CloseButtonPosition, None, bar))
    button = bar.tabButton(1, side) or bar.tabButton(1, QTabBar.RightSide)
    assert button is not None and button.isVisible()
    image = button.grab().toImage()
    tab_bg = QColor(theme.THEMES[name]["tab"])
    assert visible_pixels(image, tab_bg) >= 8, "the close button vanishes on its tab"
    # the image the stylesheet names is the native glyph, recoloured
    sheet = theme.stylesheet(name)
    assert "QTabBar::close-button { image: url(" in sheet
    url = sheet.split("QTabBar::close-button { image: url('", 1)[1].split("'", 1)[0]
    glyph = QImage(url)
    assert not glyph.isNull()
    shades = {glyph.pixelColor(x, y).name() for y in range(glyph.height())
              for x in range(glyph.width()) if glyph.pixelColor(x, y).alpha() == 255}
    assert shades == {theme.close_color(name)}
    assert contrast(theme.close_color(name), theme.THEMES[name]["tab"]) >= 4.5
    tabs.deleteLater()


@pytest.mark.parametrize("name", ["dark", "light", "nord", "solarized-light"])
def test_checkboxes_show_their_state_clearly(qapp, styled, name):
    from turbossh.gui import theme
    styled(name)
    c = theme.THEMES[name]
    sheet = theme.stylesheet(name)
    # an unticked box's edge stands out from the field
    edge = sheet.split("QCheckBox::indicator, QRadioButton::indicator {", 1)[1]
    edge = edge.split("border: 2px solid ", 1)[1].split(";", 1)[0]
    assert contrast(edge, c["input"]) >= 3.0
    assert contrast(edge, c["panel"]) >= 3.0
    # a ticked one is filled with the accent and carries a tick
    url = sheet.split("QCheckBox::indicator:checked { ", 1)[1]
    url = url.split("image: url('", 1)[1].split("'", 1)[0]
    assert os.path.exists(url) and os.path.exists(url.replace(".png", "@2x.png"))

    box = QCheckBox("Ignore host key")
    box.resize(200, 30)
    box.show()
    QTest.qWait(30)
    off = box.grab().toImage()
    box.setChecked(True)
    QTest.qWait(30)
    on = box.grab().toImage()
    changed = sum(1 for y in range(off.height()) for x in range(min(off.width(), 30))
                  if off.pixelColor(x, y) != on.pixelColor(x, y))
    assert changed >= 100, "ticking the box hardly changes it"
    accent = sum(1 for y in range(on.height()) for x in range(30)
                 if contrast(on.pixelColor(x, y).name(), theme.ACCENT) < 1.2)
    assert accent >= 60
    box.deleteLater()


def test_the_close_glyph_has_a_drawn_fallback(qapp, monkeypatch):
    """The close glyph falls back to a drawn × when the style has none."""
    from turbossh.gui import theme
    monkeypatch.setattr(theme, "_native_close_glyph", lambda side: None)
    image = theme.close_glyph(16, "#ff0000")
    red = sum(1 for y in range(16) for x in range(16)
              if image.pixelColor(x, y).alpha() > 128)
    assert red >= 10


def test_file_list_rows_alternate_in_the_theme(qapp, styled):
    from turbossh.gui import theme
    for name in ("dark", "light"):
        styled(name)
        tree = sb.ListTree()
        icon = QApplication.style().standardIcon(QApplication.style().SP_FileIcon)
        for i in range(4):
            tree.addTopLevelItem(sb.make_row(f"f{i}", icon, 2, ("", 0, 0, "", ""),
                                             "1 B", "", "", ""))
        tree.resize(400, 200)
        tree.show()
        QTest.qWait(30)
        image = tree.viewport().grab().toImage()
        # rows 1 and 2: row 0 is the current one
        first = tree.visualItemRect(tree.topLevelItem(1))
        second = tree.visualItemRect(tree.topLevelItem(2))
        x = image.width() - 10
        a = image.pixelColor(QPoint(x, first.center().y()))
        b = image.pixelColor(QPoint(x, second.center().y()))
        assert a != b, f"{name}: rows are not shaded alternately"
        c = theme.THEMES[name]
        assert {a.name(), b.name()} == {c["panel"], c["raised"]}
        tree.deleteLater()


def test_entries_and_rows_carry_the_owner_column():
    entry = sb._Entry("a", False, False, 1, 2, stat.S_IFREG | 0o644, stat.S_IFREG)
    assert entry.owner == ""                           # a default: old callers work
    spec = sb._row_specs([entry], (0, True))[0]
    assert len(spec.keys) == len(COLUMNS) and len(spec.texts) == len(COLUMNS) - 1


# ------------------------------------------------------------------ review fixes
def test_a_selection_made_while_the_folder_is_relisted_is_the_one_kept(browser,
                                                                      monkeypatch):
    """The rows on screen stay until the new ones are complete, and what is
    selected then is what stays selected: a refresh used to put back the
    selection it found when it started."""
    from turbossh.gui import file_browser
    pane = browser.local
    for n in range(7):
        (browser.local_dir / f"f{n}.txt").write_text("x")
    pane.refresh()
    assert wait_until(lambda: not pane._loading and "f6.txt" in names(pane.view))
    monkeypatch.setattr(file_browser, "_FILL_CHUNK", 2)     # several turns
    select(pane.view, "f1.txt")
    shown = pane.view.topLevelItemCount()
    pane.refresh()
    select(pane.view, "f2.txt")                           # before it lands
    assert pane.selection_paths() == [str(browser.local_dir / "f2.txt")]
    assert wait_until(lambda: pane._fill is not None or not pane._loading)
    if pane._fill is not None:                            # mid-way: the old rows
        assert pane.view.topLevelItemCount() == shown
        select(pane.view, "f3.txt")
    assert wait_until(lambda: not pane._loading)
    chosen = [it.text(0) for it in pane.view.selectedItems()]
    assert chosen in (["f2.txt"], ["f3.txt"]) and len(chosen) == 1


def test_names_sort_naturally_on_this_pc(qapp, tmp_path):
    from turbossh.gui.file_browser import _local_listing
    for name in ("file10.log", "File2.log", "file1.log"):
        (tmp_path / name).write_text("x")
    listing = _local_listing(str(tmp_path), (0, True))
    assert [s.name for s in listing.specs] == ["file1.log", "File2.log", "file10.log"]


def test_folders_the_user_names_are_checked_off_the_gui_thread(browser, monkeypatch,
                                                             tmp_path):
    from turbossh.gui import file_browser
    pane = browser.local
    seen, real = [], os.path.isdir
    target = tmp_path / "there"
    target.mkdir()

    def isdir(path):
        if same(str(path), target) or "nowhere" in str(path):
            import threading
            seen.append(threading.current_thread() is threading.main_thread())
        return real(path)
    monkeypatch.setattr(file_browser.os.path, "isdir", isdir)
    pane.path.setText(str(target))
    pane._go()                                            # Enter in the path box
    assert wait_until(lambda: same(pane.cwd, target))
    pane.go(str(tmp_path / "nowhere"))
    assert wait_until(lambda: "Not a folder" in pane.status.text())
    assert same(pane.cwd, target) and seen and not any(seen)
    # a drive picked in the box is listed, not looked at first
    if os.name == "nt":
        from turbossh.gui.file_browser import drive_of
        seen.clear()
        pane._drive_picked(drive_of(str(target)))
        assert seen == [] and pane.cwd == drive_of(str(target))
    # a later click wins over a check still out
    pane.go(str(tmp_path))
    pane.navigate(str(target), checked=True)
    QTest.qWait(300)
    assert same(pane.cwd, target)


def test_system_icons_come_from_file_info_read_on_the_worker(qapp, tmp_path):
    from PyQt5.QtWidgets import QFileIconProvider
    from turbossh.gui.file_browser import _LocalIcons, _local_listing
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    listing = _local_listing(str(tmp_path), (0, True), native=True)
    if os.name == "nt":                  # (elsewhere every disk counts as local)
        from turbossh.gui.file_browser import on_local_disk
        assert on_local_disk(str(tmp_path))
    assert set(listing.infos) == {"a.txt", "sub"} and listing.watch
    assert listing.infos["sub"].isDir()   # cached already
    assert _local_listing(str(tmp_path), (0, True)).infos == {}

    icons = _LocalIcons(QApplication.style())
    icons._provider = QFileIconProvider()                # as on the real platform
    assert not icons.get(False, False, listing.infos["a.txt"]).isNull()
    style = icons.get(True, False)                       # no info: the style's
    assert style.cacheKey() == icons._style[True, False].cacheKey()
    assert icons.get(True, False, None, 4).cacheKey() == icons._network.cacheKey()
    assert icons.get(True, False, None, 3).cacheKey() == icons._drive.cacheKey()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs")
def test_fifos_are_not_listed_or_uploaded(qapp, tmp_path):
    from turbossh.gui.file_browser import scan_folder
    fifo = tmp_path / "pipe"
    os.mkfifo(str(fifo))
    (tmp_path / "file.txt").write_text("x")
    os.symlink(str(tmp_path / "gone"), str(tmp_path / "broken"))
    assert [e.name for e in scan_folder(str(tmp_path))] == ["file.txt"]
    job = sb._Job("upload", "/x", str(fifo), False, 0)
    with pytest.raises(IOError, match="not a file or a folder"):
        sb._TransferThread(None, job)._upload(None, str(fifo), "/x")


def test_disabled_ticked_boxes_still_show_their_tick(qapp):
    from turbossh.gui import theme
    for name in ("dark", "light", "midnight", "solarized-dark"):
        c = theme.THEMES[name]
        sheet = theme.stylesheet(name)
        rule = sheet.split("QCheckBox::indicator:checked:disabled {", 1)[1].split("}", 1)[0]
        fill = rule.split("background: ", 1)[1].split(";", 1)[0]
        assert "image: url(" in rule
        assert contrast(c["text"], fill) >= 3.0, name             # the tick on it
        assert contrast(fill, c["panel"]) >= 1.3, name            # ticked vs not


def test_an_extra_browser_does_not_change_the_saved_start_folder(qapp, ssh, tmp_path):
    from turbossh.gui import settings
    from turbossh.gui.file_browser import FileBrowser
    before = settings.get("local_browser_dir")
    extra = FileBrowser(ssh, local_start=str(tmp_path), save_start=False)
    try:
        assert wait_until(lambda: same(extra.local.cwd, tmp_path) and not extra.busy)
    finally:
        extra.close_browser()
        extra.deleteLater()
    assert settings.get("local_browser_dir") == before


# ------------------------------------------------------------------ second review
def test_a_big_selection_is_put_back_in_one_go(qapp, monkeypatch):
    """One setSelected per row is merged against every range before it: 5,000
    rows held the window for seconds, on every automatic refresh."""
    from PyQt5.QtWidgets import QTreeWidgetItem
    calls = []
    real = QTreeWidgetItem.setSelected
    monkeypatch.setattr(QTreeWidgetItem, "setSelected",
                        lambda self, on: (calls.append(on), real(self, on))[1])
    tree = sb.ListTree()
    icon = QApplication.style().standardIcon(QApplication.style().SP_FileIcon)
    tree.addTopLevelItems([sb.make_row(f"f{i:05d}", icon, 2, (f"f{i:05d}", i, 0, "", ""),
                                       "1 B", "", "", "") for i in range(5000)])
    started = time.monotonic()
    sb.select_rows(tree, range(0, 5000, 2))             # scattered: 2,500 runs
    assert len(tree.selectedItems()) == 2500
    tree.sortByColumn(1, Qt.DescendingOrder)            # through reorder_rows
    assert len(tree.selectedItems()) == 2500 and calls == []
    assert {it.text(0) for it in tree.selectedItems()} == {f"f{i:05d}" for i in
                                                          range(0, 5000, 2)}
    assert time.monotonic() - started < 3.0
    tree.deleteLater()


def test_the_local_pane_restores_a_selection_without_a_call_per_row(browser, monkeypatch):
    from PyQt5.QtWidgets import QTreeWidgetItem
    pane = browser.local
    calls = []
    monkeypatch.setattr(QTreeWidgetItem, "setSelected",
                        lambda self, on: calls.append(on))
    pane.view.selectAll()
    pane.refresh()
    assert wait_until(lambda: not pane._loading)
    assert len(pane.selection_paths()) == 3 and calls == []


def test_a_folder_deleted_and_made_again_is_watched_again(browser):
    import shutil
    pane = browser.local
    folder = browser.local_dir / "folder"
    assert pane.navigate(str(folder))
    assert wait_until(lambda: not pane._loading)
    assert str(folder) in pane._watcher.directories()
    shutil.rmtree(str(folder))

    def made_again():                    # (the name can stay taken a moment while
        try:                             # a watch handle on it is still open)
            folder.mkdir()
        except FileExistsError:
            return folder.is_dir()
        except OSError:
            return False
        return True
    assert wait_until(made_again)
    pane.refresh()
    assert wait_until(lambda: not pane._loading
                      and str(folder) in pane._watcher.directories())
    (folder / "new.txt").write_text("x")
    assert wait_until(lambda: "new.txt" in names(pane.view))


def test_a_late_not_a_folder_leaves_what_was_typed_since(browser, tmp_path):
    pane = browser.local
    pane.go(str(tmp_path / "nowhere"))
    pane.path.setText("C:/still/typing")                 # before the answer
    assert wait_until(lambda: "Not a folder" in pane.status.text())
    assert pane.path.text() == "C:/still/typing"


def test_only_a_download_into_the_local_folder_lists_it_again(browser):
    pane = browser.local
    pane._settle.stop()
    browser._transfer_ended("upload", str(browser.local_dir / "note.txt"))
    browser._transfer_ended("download", str(browser.local_dir / "folder" / "x.txt"))
    assert not pane._settle.isActive()
    browser._transfer_ended("download", str(browser.local_dir / "x.txt"))
    assert pane._settle.isActive()


def test_a_high_dpi_close_glyph_is_scaled_not_cropped(qapp, monkeypatch):
    from PyQt5.QtGui import QPainter, QPen, QPixmap
    from turbossh.gui import theme

    class Icon:
        def pixmap(self, w, h):                           # what Qt gives at 200 %
            pm = QPixmap(2 * w, 2 * h)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setPen(QPen(QColor("#202020"), 3))
            p.drawLine(6, 6, 2 * w - 7, 2 * h - 7)
            p.drawLine(2 * w - 7, 6, 6, 2 * h - 7)
            p.end()
            pm.setDevicePixelRatio(2.0)
            return pm

    class Style:
        def standardIcon(self, which):
            return Icon()
    monkeypatch.setattr(QApplication, "style", staticmethod(lambda: Style()))
    glyph = theme._native_close_glyph(16)
    assert (glyph.width(), glyph.height()) == (16, 16)
    image = theme.close_glyph(16, "#ffffff")
    corners = [image.pixelColor(x, y).alpha() for x, y in ((0, 0), (15, 0), (0, 15), (15, 15))]
    assert max(corners) < 60, "the glyph fills the button: cropped, not scaled"


# ------------------------------------------------------------------ third review
def test_system_icon_infos_come_from_one_scan_not_a_query_per_file(qapp, tmp_path,
                                                                   monkeypatch):
    from turbossh.gui import file_browser
    for n in range(50):
        (tmp_path / f"f{n}.txt").write_text("x")
    made = []
    real = file_browser.QFileInfo
    monkeypatch.setattr(file_browser, "QFileInfo",
                        lambda *a: (made.append(a), real(*a))[1])
    listing = file_browser._local_listing(str(tmp_path), (0, True), native=True)
    assert len(listing.infos) == 50 and made == []
    assert listing.infos["f7.txt"].isFile()


def test_a_selection_made_after_a_rename_is_not_replaced_by_it(browser):
    pane = browser.local
    path = str(browser.local_dir / "note.txt")
    assert pane.do_rename(path, "renamed.txt")
    select(pane.view, "big.bin")                          # the user moves on
    assert wait_until(lambda: not pane._loading)
    assert [it.text(0) for it in pane.view.selectedItems()] == ["big.bin"]
    # left alone, the renamed entry is the one selected
    assert pane.do_rename(str(browser.local_dir / "renamed.txt"), "note.txt")
    assert wait_until(lambda: not pane._loading)
    assert [it.text(0) for it in pane.view.selectedItems()] == ["note.txt"]


def test_network_and_fuse_mounts_are_not_local(tmp_path):
    from turbossh.gui.file_browser import mount_type
    table = tmp_path / "mounts"
    table.write_text("/dev/sda1 / ext4 rw 0 0\n"
                     "nas:/vol /mnt/nas nfs4 rw 0 0\n"
                     "u@h:/ /home/u/my\\040box fuse.sshfs rw 0 0\n"
                     "/dev/sdb1 /mnt/win fuseblk rw 0 0\n")
    assert mount_type("/home/u/work", str(table)) == "ext4"
    assert mount_type("/mnt/nas/logs", str(table)) == "nfs4"
    assert mount_type("/mnt/nasty", str(table)) == "ext4"
    assert mount_type("/home/u/my box/x", str(table)) == "fuse.sshfs"
    assert mount_type("/mnt/win", str(table)) == "fuseblk"
    assert mount_type("/x", str(tmp_path / "none")) == ""


def test_a_hidden_pane_lists_the_changes_when_it_shows_again(browser):
    pane = browser.local
    browser.hide()
    (browser.local_dir / "while-away.txt").write_text("x")
    QTest.qWait(1500)
    assert "while-away.txt" not in names(pane.view) and pane._stale
    browser.show()
    assert wait_until(lambda: "while-away.txt" in names(pane.view))


def test_image_urls_survive_an_apostrophe_in_the_path():
    from turbossh.gui import theme
    assert theme._css_url("C:/Users/O'Brien/x.png") == '''url("C:/Users/O'Brien/x.png")'''
    assert theme._css_url("C:/Users/ann/x.png") == "url('C:/Users/ann/x.png')"


def test_a_refused_sftp_channel_says_why(qapp):
    import paramiko

    class Transport:
        def is_active(self):
            return True

        def open_session(self, timeout=None):
            raise paramiko.ChannelException(1, "Administratively prohibited")

    class Handler:
        class client:
            @staticmethod
            def get_transport():
                return Transport()
    with pytest.raises(IOError, match="refused another channel"):
        sb._open_sftp(Handler())
