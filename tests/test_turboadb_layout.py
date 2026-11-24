"""The sessions list and the Files tab as TurboADB has them: a ‹ that hides the
list and a › on the window's edge that brings it back (however it was hidden),
and the panes' operations — New file, Open, Edit, Copy, Paste — with the
clipboard both panes share."""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent, QKeySequence  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402


def wait_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(20)
    return predicate()


def names(tree):
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def select(tree, *wanted):
    tree.clearSelection()
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        item.setSelected(item.text(0) in wanted)


@pytest.fixture()
def answer(monkeypatch):
    """Every question box answers what ``answer.value`` says (Yes by default)
    and is recorded."""
    class Answer:
        value = QMessageBox.Yes
        asked = []

    def question(_parent, title, text, *_args, **_kwargs):
        Answer.asked.append((title, text))
        return Answer.value
    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    Answer.asked = []
    return Answer


# --------------------------------------------------------------------------- #
# The sessions list
# --------------------------------------------------------------------------- #
@pytest.fixture()
def window(qapp):
    from turbossh.gui.main_window import MainWindow

    class Window(MainWindow):
        def closeEvent(self, event):
            event.accept()

    win = Window()
    win.resize(1000, 700)
    win.show()
    QTest.qWait(30)
    yield win
    win.hide()
    win.deleteLater()
    QTest.qWait(50)


def handle_shown(win):
    return not win._sidebar_handle_dock.isHidden()


def test_the_sessions_list_cannot_be_closed_or_floated_away(window):
    from PyQt5.QtWidgets import QDockWidget
    dock = window._sidebar_dock
    assert dock.features() == QDockWidget.NoDockWidgetFeatures
    assert dock.titleBarWidget() is not None          # no ✕ / float buttons
    assert dock.isVisible() and not handle_shown(window)
    assert window._sidebar_act.isChecked()


def test_the_arrow_hides_the_list_and_the_edge_bar_brings_it_back(window):
    dock = window._sidebar_dock
    width = dock.width()
    window._sidebar_collapse.click()
    QTest.qWait(30)
    assert dock.isHidden() and handle_shown(window)
    assert not window._sidebar_act.isChecked()
    assert window._sidebar_handle.width() <= 30        # a slim bar, not a panel
    window._sidebar_handle.arrow.click()
    QTest.qWait(30)
    assert dock.isVisible() and not handle_shown(window)
    assert window._sidebar_act.isChecked()
    assert abs(dock.width() - width) <= 4, "the list came back at another width"
    # a click anywhere on the bar does it too
    window.toggle_sidebar(False)
    QTest.mouseClick(window._sidebar_handle, Qt.LeftButton, Qt.NoModifier,
                     QPoint(10, window._sidebar_handle.height() - 10))
    assert dock.isVisible() and not handle_shown(window)


def test_hidden_any_way_the_list_comes_back(window):
    dock = window._sidebar_dock
    # the docks' own menu (a right-click on the ribbon) used to hide it for good
    dock.toggleViewAction().trigger()
    assert dock.isHidden() and handle_shown(window)
    window._sidebar_act.trigger()                      # View → Sessions list
    assert dock.isVisible() and not handle_shown(window)
    window._sidebar_act.trigger()
    assert dock.isHidden() and handle_shown(window)
    window.toggle_sidebar()                            # flip
    assert dock.isVisible()


def test_the_edge_bar_is_not_in_the_docks_menu(window):
    menu = window.createPopupMenu()
    try:
        shown = [a for a in menu.actions() if a.isVisible() and not a.isSeparator()]
        assert window._sidebar_handle_dock.toggleViewAction() not in shown
        assert all(a.text() for a in shown)
    finally:
        menu.deleteLater()


def test_the_shortcut_works_in_a_terminal_and_ctrl_b_stays_the_shells(window):
    from turbossh.gui.vt100 import Vt100Terminal
    keys = [k.toString() for k in window._sidebar_act.shortcuts()]
    assert keys == ["Ctrl+Shift+B", "Ctrl+B"]
    claims = Vt100Terminal._claims
    assert claims(QKeyEvent(QEvent.ShortcutOverride, Qt.Key_B, Qt.ControlModifier))
    assert not claims(QKeyEvent(QEvent.ShortcutOverride, Qt.Key_B,
                                Qt.ControlModifier | Qt.ShiftModifier))


def test_split_view_keeps_the_edge_bar(window):
    from turbossh.gui.vt100 import Vt100Terminal

    class Page(QWidget):
        def close_session(self):
            pass
    pages = []
    for i in range(2):
        page = Page()
        page.term = Vt100Terminal(send_fn=lambda data: None, parent=page)
        window.tabs.addTab(page, f"s{i}")
        pages.append(page)
    window._close_tab(window.tabs.indexOf(window._home))   # Home can't be tiled
    window.toggle_sidebar(False)
    window.toggle_split()
    try:
        assert window._tiled and handle_shown(window)
        window.toggle_sidebar(True)
        assert window._sidebar_dock.isVisible() and not handle_shown(window)
        window.toggle_sidebar(False)
    finally:
        window._untile()
    assert handle_shown(window) and window.centralWidget() is window.tabs
    window.toggle_sidebar(True)
    for page in pages:
        page.term.cleanup()


def test_the_count_shows_the_saved_sessions(window):
    window.store.save({"name": "count-me", "type": "ssh", "host": "10.0.0.9",
                       "user": "myuser"})
    window.refresh_sessions()
    assert window._session_count.text() == str(len(window.store.sessions))
    window.store.delete("count-me")
    window.refresh_sessions()
    assert window._session_count.text() == str(len(window.store.sessions))


def test_the_ribbon_has_no_box_between_its_buttons(window):
    """The empty stretch between Help and Theme took the window colour (every
    plain widget's), a box on the ribbon most visible in the light themes."""
    # drawn on its own over nothing: it must leave the ribbon showing through
    # (offscreen the buttons can overflow the ribbon and hide the stretch, so
    # sampling the ribbon itself would depend on the fonts there are)
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QImage, QRegion
    from PyQt5.QtWidgets import QToolBar
    from turbossh.gui import theme
    spacer = window.findChild(QWidget, "ribbonSpacer")
    assert isinstance(spacer.parentWidget(), QToolBar)
    before = theme.current_name()
    try:
        window.apply_theme("light")
        spacer.ensurePolished()
        image = QImage(40, 20, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        spacer.render(image, QPoint(), QRegion(), QWidget.DrawChildren)
        assert image.pixelColor(20, 10).alpha() == 0
    finally:
        window.apply_theme(before)


def test_a_host_that_hides_the_list_gets_no_edge_bar(qapp):
    from turbossh.gui.main_window import MainWindow, WindowOptions
    holder = QWidget()
    win = MainWindow(holder, WindowOptions(embedded=True, sidebar=False, home=False))
    holder.show()
    try:
        assert win._sidebar_dock.isHidden() and not handle_shown(win)
        win.set_chrome(sidebar=True)
        assert not win._sidebar_dock.isHidden() and not handle_shown(win)
        win.toggle_sidebar(False)                    # the user's own choice
        assert handle_shown(win)
        win.set_chrome(sidebar=False)                # hidden already: no event
        assert not handle_shown(win)
        win.set_chrome(sidebar=True)
        assert not win._sidebar_dock.isHidden() and not handle_shown(win)
    finally:
        holder.hide()
        win.deleteLater()
        holder.deleteLater()
        QTest.qWait(50)


# --------------------------------------------------------------------------- #
# The Files tab
# --------------------------------------------------------------------------- #
@pytest.fixture()
def browser(qapp, ssh, ssh_server, tmp_path):
    from turbossh.gui.file_browser import FileBrowser
    root = ssh_server.root
    (root / "docs").mkdir()
    (root / "docs" / "inner.txt").write_text("inner")
    (root / "tmp").mkdir()
    (root / "alpha.txt").write_text("alpha")
    local = tmp_path / "pc"
    (local / "folder").mkdir(parents=True)
    (local / "note.txt").write_text("note")
    widget = FileBrowser(ssh)
    widget.resize(1200, 600)
    widget.show()
    assert widget.local.navigate(str(local))
    assert wait_until(lambda: not widget.busy and not widget.local._loading
                      and widget.remote.tree.topLevelItemCount() > 1)
    widget.local_dir = local
    widget.root = root
    yield widget
    widget.close_browser()
    widget.deleteLater()
    QTest.qWait(50)


def settle(browser):
    assert wait_until(lambda: not browser.busy and not browser.local._loading
                      and not browser.local._pasting)
    QTest.qWait(30)


OPS = ["New folder", "New file", "Open", "Edit", "Copy", "Paste", "Rename", "Delete"]


def test_both_panes_have_turboadbs_buttons_with_icons(browser):
    for pane in (browser.local, browser.remote):
        assert list(pane.ops_buttons) == OPS
        assert all(not b.icon().isNull() for b in pane.ops_buttons.values())
        assert pane.ops_buttons["Delete"].property("role") == "danger"
        assert pane.ops_buttons["Copy"].property("role") == "ghost"


def test_the_button_row_wraps_rather_than_widen_the_pane(qapp):
    from turbossh.gui.widgets import ops_row
    row, buttons = ops_row([(name, lambda: None, "") for name in OPS[:-1]], lambda: None)
    layout = row.layout()
    wide = sum(b.sizeHint().width() for b in buttons)
    assert row.minimumSizeHint().width() < wide / 3
    assert layout.heightForWidth(200) > layout.heightForWidth(wide + 200)


def test_a_standalone_remote_browser_keeps_download_and_upload(qapp, ssh):
    from turbossh.gui.sftp_browser import SftpBrowser
    pane = SftpBrowser(ssh)
    try:
        assert list(pane.ops_buttons) == ["New folder", "New file", "Edit", "Rename",
                                          "Download…", "Upload…", "Delete"]
    finally:
        pane.close_browser()
        pane.deleteLater()


def test_transfers_show_only_once_there_are_some(browser):
    assert browser.panel.isHidden()
    select(browser.local.view, "note.txt")
    assert browser.upload() == 1
    settle(browser)
    assert wait_until(lambda: not browser.panel.isHidden())
    browser.panel._clear_finished()
    assert wait_until(lambda: browser.panel.isHidden())


def test_local_new_file_never_replaces_one(browser):
    pane = browser.local
    assert pane.do_new_file("fresh.cfg")
    assert (browser.local_dir / "fresh.cfg").read_bytes() == b""
    assert not pane.do_new_file("note.txt")
    assert (browser.local_dir / "note.txt").read_text() == "note"
    assert not pane.do_new_file("..")
    assert not pane.do_new_file(os.path.join("folder", "x"))


def test_local_edit_saves_and_asks_before_overwriting_a_change(browser, answer):
    pane = browser.local
    select(pane.view, "note.txt")
    dialog = pane.edit_selected()
    try:
        assert wait_until(lambda: dialog._loaded)
        assert dialog.edit.toPlainText() == "note"
        dialog.edit.setPlainText("note, edited")
        dialog.edit.document().setModified(True)     # (as typing does)
        dialog.save()
        assert wait_until(lambda: not dialog._saving and not dialog.has_unsaved_changes())
        assert (browser.local_dir / "note.txt").read_text() == "note, edited"
        # another program changes it: the save asks first
        (browser.local_dir / "note.txt").write_text("someone else's")
        os.utime(browser.local_dir / "note.txt", (1, 1))
        dialog.edit.setPlainText("mine")
        dialog.edit.document().setModified(True)
        answer.value = QMessageBox.No
        dialog.save()
        assert wait_until(lambda: answer.asked)
        assert (browser.local_dir / "note.txt").read_text() == "someone else's"
        assert dialog.has_unsaved_changes()
    finally:
        dialog.discard()
        dialog.close()
    select(pane.view, "folder")
    assert pane.edit_selected() is None              # a folder is not edited


def test_local_copy_paste_names_copies_and_refuses_a_folder_into_itself(browser, answer):
    pane = browser.local
    select(pane.view, "note.txt")
    pane.copy_selected()
    assert browser.clipboard == ("local", [str(browser.local_dir / "note.txt")])
    assert browser.paste_local() == 1                # into its own folder
    settle(browser)
    assert (browser.local_dir / "note - Copy.txt").read_text() == "note"
    assert browser.paste_local() == 1
    settle(browser)
    assert (browser.local_dir / "note - Copy (2).txt").read_text() == "note"
    # into another folder: the same name
    pane.navigate(str(browser.local_dir / "folder"))
    settle(browser)
    assert browser.paste_local() == 1
    settle(browser)
    assert (browser.local_dir / "folder" / "note.txt").read_text() == "note"
    # again: it is there — "No" pastes nothing
    answer.value = QMessageBox.No
    assert browser.paste_local() == 0 and answer.asked
    # a folder into itself
    pane.navigate(str(browser.local_dir))
    settle(browser)
    select(pane.view, "folder")
    pane.copy_selected()
    pane.navigate(str(browser.local_dir / "folder"))
    settle(browser)
    assert browser.paste_local() == 0
    assert "into itself" in pane.status.text()
    assert not (browser.local_dir / "folder" / "folder").exists()


def test_a_folder_pasted_beside_itself_is_copied_whole(browser):
    (browser.local_dir / "folder" / "sub").mkdir()
    (browser.local_dir / "folder" / "sub" / "deep.txt").write_text("deep")
    pane = browser.local
    select(pane.view, "folder")
    pane.copy_selected()
    assert browser.paste_local() == 1
    settle(browser)
    assert (browser.local_dir / "folder - Copy" / "sub" / "deep.txt").read_text() == "deep"


def test_remote_new_file_never_replaces_one(browser):
    remote = browser.remote
    assert remote.do_new_file("fresh.cfg")
    settle(browser)
    assert (browser.root / "fresh.cfg").read_bytes() == b""
    assert "fresh.cfg" in names(remote.tree)
    assert not remote.do_new_file("alpha.txt")       # in the listing
    assert (browser.root / "alpha.txt").read_text() == "alpha"


def test_remote_copy_paste_copies_on_the_remote_host(browser, answer):
    from turbossh.gui import transfer_log as tl
    remote = browser.remote
    select(remote.tree, "alpha.txt")
    browser.copy_remote()
    assert browser.clipboard == ("remote", [("/alpha.txt", False)])
    assert browser.paste_remote() == 1               # into its own folder
    settle(browser)
    assert (browser.root / "alpha - Copy.txt").read_text() == "alpha"
    assert wait_until(lambda: "alpha - Copy.txt" in names(remote.tree))
    item = browser.transfers.items()[-1]
    assert item.kind == "copy" and item.status == tl.DONE
    # a folder, into another folder
    select(remote.tree, "docs")
    browser.copy_remote()
    assert remote.navigate("/tmp")
    settle(browser)
    assert browser.paste_remote() == 1
    settle(browser)
    assert (browser.root / "tmp" / "docs" / "inner.txt").read_text() == "inner"
    # and never into itself
    assert remote.navigate("/docs")
    settle(browser)
    browser.clipboard = ("remote", [("/docs", True)])
    assert browser.paste_remote() == 0
    assert "into itself" in remote.status.text()


def test_paste_across_the_panes_uploads_and_downloads(browser, answer):
    select(browser.local.view, "note.txt")
    browser.local.copy_selected()
    assert browser.paste_remote() == 1
    settle(browser)
    assert (browser.root / "note.txt").read_text() == "note"
    select(browser.remote.tree, "alpha.txt")
    browser.copy_remote()
    assert browser.paste_local() == 1
    settle(browser)
    assert (browser.local_dir / "alpha.txt").read_text() == "alpha"


def test_the_lists_claim_ctrl_c_and_ctrl_v_over_a_hosts_shortcut(browser):
    """No QShortcut of ours on Copy / Paste: a window-wide one of an embedding
    application would make the key ambiguous, and Qt fires neither. The list
    claims the key while it has the keyboard instead."""
    from PyQt5.QtWidgets import QShortcut
    host = []
    QShortcut(QKeySequence(QKeySequence.Copy), browser, activated=lambda: host.append(1),
              context=Qt.WindowShortcut)
    browser.activateWindow()
    QApplication.setActiveWindow(browser)
    copy = QKeySequence(QKeySequence.Copy)
    for view in (browser.local.view, browser.remote.tree):
        assert not [s for s in view.findChildren(QShortcut) if s.key() == copy]
        override = QKeyEvent(QEvent.ShortcutOverride, Qt.Key_C, Qt.ControlModifier)
        assert view.event(override) and override.isAccepted()
    select(browser.local.view, "note.txt")
    browser.local.view.setFocus()
    QTest.keyClick(browser.local.view, Qt.Key_C, Qt.ControlModifier)
    assert browser.clipboard == ("local", [str(browser.local_dir / "note.txt")])
    select(browser.remote.tree, "alpha.txt")
    browser.remote.tree.setFocus()
    QTest.keyClick(browser.remote.tree, Qt.Key_C, Qt.ControlModifier)
    assert browser.clipboard == ("remote", [("/alpha.txt", False)])
    assert host == []
    for view in (browser.local.view, browser.remote.tree):
        keys = {s.key().toString() for s in view.findChildren(QShortcut)}
        assert {"F4", "Shift+Del"} <= keys


def test_open_copies_a_remote_file_and_sends_its_saves_back(browser, monkeypatch, answer):
    from turbossh.gui import file_open
    opened = []
    monkeypatch.setattr(file_open, "how_to_open", lambda name: (file_open.OPEN, ""))
    monkeypatch.setattr(file_open, "start", lambda path: opened.append(path) or True)
    select(browser.remote.tree, "alpha.txt")
    copy = browser.open_remote()
    assert copy is not None
    assert wait_until(lambda: opened)
    assert opened == [copy.local]
    with open(copy.local, encoding="utf-8") as fh:
        assert fh.read() == "alpha"
    # the app saves it: it goes back to the remote file
    with open(copy.local, "w", encoding="utf-8") as fh:
        fh.write("alpha, saved on this PC")
    browser._copies.check()                          # changed…
    browser._copies.check()                          # …and settled: saved
    assert wait_until(lambda: (browser.root / "alpha.txt").read_text()
                      == "alpha, saved on this PC")
    assert wait_until(lambda: not copy.saving)
    assert not answer.asked
    # changed on the remote meanwhile: asked first, "No" leaves it
    (browser.root / "alpha.txt").write_text("changed over there")
    os.utime(browser.root / "alpha.txt", (1, 1))
    with open(copy.local, "w", encoding="utf-8") as fh:
        fh.write("second save")
    answer.value = QMessageBox.No
    browser._copies.check()
    browser._copies.check()
    assert wait_until(lambda: answer.asked)
    assert wait_until(lambda: not copy.saving)
    assert (browser.root / "alpha.txt").read_text() == "changed over there"


def test_open_refuses_programs_and_edits_scripts(browser, monkeypatch):
    (browser.root / "tool.exe").write_bytes(b"MZ")
    (browser.root / "run.sh").write_text("echo hi")
    browser.remote.refresh()
    settle(browser)
    edited = []
    monkeypatch.setattr(browser.remote, "edit_selected", lambda: edited.append(1))
    select(browser.remote.tree, "tool.exe")
    assert browser.open_remote() is None
    assert "program" in browser.remote.status.text()
    assert len(browser.transfers) == 0
    select(browser.remote.tree, "run.sh")
    browser.open_remote()
    assert edited == [1] and len(browser.transfers) == 0
    select(browser.remote.tree, "docs")
    browser.open_remote()                            # a folder is entered
    assert wait_until(lambda: browser.remote.cwd == "/docs")


def test_remote_rows_get_icons_by_kind():
    import stat as st
    from turbossh.gui.sftp_browser import kind_glyph
    assert kind_glyph("docs", True) == ("folder", "amber")
    assert kind_glyph("lib", True, True) == ("link", "teal")
    assert kind_glyph("photo.JPG", False) == ("image", "purple")
    assert kind_glyph("app.log", False) == ("file", "blue")
    assert kind_glyph("fw.tar.gz", False) == ("file", "orange")
    assert kind_glyph("ttyS0", False, ftype=st.S_IFCHR) == ("chip", "dim")
    assert kind_glyph("blob", False) == ("file", "dim")


def test_copies_are_named_as_explorer_names_them():
    from turbossh.gui.sftp_browser import copy_name
    assert copy_name("notes.txt", set()) == "notes - Copy.txt"
    assert copy_name("notes.txt", {"notes - Copy.txt"}) == "notes - Copy (2).txt"
    assert copy_name("my.dir", set(), is_dir=True) == "my.dir - Copy"
    assert copy_name(".bashrc", set()) == ".bashrc - Copy"
    assert copy_name("A.txt", {"a - copy.txt"}, fold=str.casefold) == "A - Copy (2).txt"


def test_a_copy_has_its_own_labels_in_the_history():
    from turbossh.gui import transfer_log as tl
    log = tl.TransferLog()
    item_id = log.add("copy", "/srv/a - Copy.txt", "/srv/a.txt")
    log.start(item_id)
    log.finish(item_id, tl.DONE, size=5)
    item = log.get(item_id)
    assert tl.DIRECTION_LABELS[item.kind] == "Remote → Remote"
    assert tl.past_tense(item).startswith("Copied a - Copy.txt")
    assert "Remote → Remote" in log.report()


def test_programs_are_never_opened_and_scripts_are_edited():
    from turbossh.gui import file_open
    assert file_open.how_to_open("setup.exe")[0] == file_open.REFUSE
    assert file_open.how_to_open("flash.apk")[0] == file_open.REFUSE
    assert file_open.how_to_open("start.sh")[0] == file_open.EDIT
    assert file_open.how_to_open("Makefile")[0] == file_open.EDIT     # no app for it


def test_an_application_wide_sheet_keeps_flat_pane_buttons(qapp):
    from turbossh.gui import theme
    sheet = theme.stylesheet("dark")
    assert 'QPushButton#paneOp[role="ghost"] {' in sheet
    assert "QSplitter#filePanes::handle" in sheet
    assert "QWidget#sidebarHandle" in sheet
    QApplication.processEvents()


# --------------------------------------------------------------------------- #
# What the review of the above found
# --------------------------------------------------------------------------- #
def _opened(browser, monkeypatch):
    from turbossh.gui import file_open
    opened = []
    monkeypatch.setattr(file_open, "how_to_open", lambda name: (file_open.OPEN, ""))
    monkeypatch.setattr(file_open, "start", lambda path: opened.append(path) or True)
    select(browser.remote.tree, "alpha.txt")
    copy = browser.open_remote()
    assert wait_until(lambda: opened)
    return copy


def _save(browser, copy, text):
    with open(copy.local, "w", encoding="utf-8") as fh:
        fh.write(text)
    browser._copies.check()
    browser._copies.check()


def test_a_save_waiting_on_a_dropped_look_up_is_sent_after_a_reconnect(browser, monkeypatch):
    copy = _opened(browser, monkeypatch)
    real = browser.remote.stamp_of
    held = []
    monkeypatch.setattr(browser.remote, "stamp_of",
                        lambda path, done, fail: held.append(path) or True)
    _save(browser, copy, "saved while the link died")
    assert held and copy.saving                  # its look-up never answers
    monkeypatch.setattr(browser.remote, "stamp_of", real)
    browser.remote.rebound.emit()                # the reconnect: asked again
    assert wait_until(lambda: (browser.root / "alpha.txt").read_text()
                      == "saved while the link died")
    assert wait_until(lambda: not copy.saving)


def test_a_cancelled_save_back_is_reported_and_the_next_one_goes(browser, monkeypatch, tmp_path):
    from turbossh.gui import sftp_browser as sb
    copy = _opened(browser, monkeypatch)
    original = sb._TransferThread._download

    def held(self, sftp, remote, local):         # until cancelled
        while True:
            self._check()
            time.sleep(0.02)
    monkeypatch.setattr(sb._TransferThread, "_download", held)
    browser.remote.queue_download("/docs/inner.txt", str(tmp_path / "inner.txt"))
    _save(browser, copy, "queued behind the big one")
    assert wait_until(lambda: browser._sending)  # its upload waits in the queue
    said = []
    browser.notify.connect(lambda text, kind: said.append(text))
    browser.remote.cancel_transfer()
    assert wait_until(lambda: not copy.saving)
    assert any("NOT sent" in text for text in said), said
    assert wait_until(lambda: not browser.busy)
    monkeypatch.setattr(sb._TransferThread, "_download", original)
    _save(browser, copy, "the next save")
    assert wait_until(lambda: (browser.root / "alpha.txt").read_text() == "the next save")


def test_keeping_the_remote_change_asks_again_at_the_next_save(browser, monkeypatch, answer):
    copy = _opened(browser, monkeypatch)
    (browser.root / "alpha.txt").write_text("changed over there")
    os.utime(browser.root / "alpha.txt", (1, 1))
    answer.value = QMessageBox.No
    _save(browser, copy, "first")
    assert wait_until(lambda: len(answer.asked) == 1 and not copy.saving)
    _save(browser, copy, "second")
    assert wait_until(lambda: len(answer.asked) == 2 and not copy.saving)
    assert (browser.root / "alpha.txt").read_text() == "changed over there"


def test_a_remote_copy_never_writes_its_own_source_in_place(browser, monkeypatch, answer):
    from test_fixes_round2_core import _report_link_counts
    from turbossh.gui import transfer_log as tl
    root = browser.root
    (root / "data.bin").write_bytes(b"precious")
    try:
        os.link(str(root / "data.bin"), str(root / "docs" / "data.bin"))
    except (OSError, NotImplementedError):
        pytest.skip("this file system cannot make hard links")
    _report_link_counts(monkeypatch)
    browser.remote.refresh()
    settle(browser)
    select(browser.remote.tree, "data.bin")
    browser.copy_remote()
    assert browser.remote.navigate("/docs")
    settle(browser)
    assert browser.paste_remote() == 1                # "Replace?" — Yes
    settle(browser)
    assert (root / "data.bin").read_bytes() == b"precious"
    assert (root / "docs" / "data.bin").read_bytes() == b"precious"
    item = browser.transfers.items()[-1]
    assert item.status == tl.FAILED and "hard links" in item.error


def test_local_edit_through_a_link_edits_the_file(browser):
    target = browser.local_dir / "real.txt"
    target.write_text("real")
    link = browser.local_dir / "link.txt"
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("cannot make symbolic links here")
    from turbossh.gui.remote_edit import read_local_text, write_local_text
    loaded = read_local_text(str(link))
    write_local_text(str(link), b"edited", stamp=loaded.stamp, mode=loaded.mode)
    assert os.path.islink(str(link))
    assert target.read_text() == "edited"


def test_hosted_only_ctrl_shift_b_and_not_while_the_host_hides_the_list(qapp):
    from turbossh.gui.main_window import MainWindow, WindowOptions
    holder = QWidget()
    win = MainWindow(holder, WindowOptions(embedded=True, home=False))
    try:
        keys = [k.toString() for k in win._sidebar_act.shortcuts()]
        assert keys == ["Ctrl+Shift+B"]
        win.set_chrome(sidebar=False)
        assert not win._sidebar_act.isEnabled()
        win.set_chrome(sidebar=True)
        assert win._sidebar_act.isEnabled()
    finally:
        win.deleteLater()
        holder.deleteLater()
        QTest.qWait(50)


def test_remote_new_file_folds_case_where_the_remote_does(browser, monkeypatch):
    monkeypatch.setattr(browser.remote, "remote_ignores_case", lambda: True)
    assert not browser.remote.do_new_file("ALPHA.TXT")
    assert "already exists" in browser.remote.status.text()


def test_windows_names_with_a_colon_are_refused(browser, monkeypatch):
    from turbossh.gui import file_browser
    monkeypatch.setattr(file_browser, "_WINDOWS", True)
    for name in ("C:x", "note.txt:hidden", "a|b"):
        assert not browser.local.do_new_file(name)
        assert not browser.local.do_new_folder(name)


def test_the_white_delete_glyph_shows_on_a_light_button(qapp):
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QColor, QImage, QPainter
    from turbossh.gui import glyphs
    image = QImage(QSize(32, 32), QImage.Format_ARGB32)
    image.fill(QColor("#f0f0f0"))
    painter = QPainter(image)
    glyphs.icon("trash", "on-danger").paint(painter, 0, 0, 32, 32)
    painter.end()
    dark = sum(1 for x in range(32) for y in range(32)
               if QColor(image.pixel(x, y)).lightness() < 200)
    assert dark >= 20, "the trash can vanished on a light button"


def test_opened_copies_go_where_the_environment_says(monkeypatch, tmp_path):
    from turbossh.gui import file_open
    monkeypatch.setenv("TURBOSSH_OPENED_DIR", str(tmp_path / "copies"))
    folder = file_open.new_copy_folder()
    assert os.path.dirname(folder) == str(tmp_path / "copies")


def test_a_save_not_checked_afterwards_asks_before_the_next(browser, monkeypatch, answer):
    copy = _opened(browser, monkeypatch)
    real = browser.remote.stamp_of
    calls = []

    def flaky(path, done, fail):                  # the look-up after the upload fails
        calls.append(path)
        if len(calls) == 2:
            fail("the remote host did not answer")
            return True
        return real(path, done, fail)
    monkeypatch.setattr(browser.remote, "stamp_of", flaky)
    _save(browser, copy, "first")
    assert wait_until(lambda: len(calls) >= 2 and not copy.saving)
    assert (browser.root / "alpha.txt").read_text() == "first"
    answer.value = QMessageBox.No
    _save(browser, copy, "second")
    assert wait_until(lambda: answer.asked and not copy.saving)
    assert "could not look" in answer.asked[0][1]
    assert (browser.root / "alpha.txt").read_text() == "first"


def test_a_folder_copy_skips_a_file_it_may_not_replace_and_goes_on(browser, monkeypatch,
                                                                    answer):
    from test_fixes_round2_core import _report_link_counts
    root = browser.root
    (root / "tree").mkdir()
    (root / "tree" / "a.bin").write_bytes(b"aaa")
    (root / "tree" / "b.bin").write_bytes(b"bbb")
    (root / "tmp" / "tree").mkdir()
    try:
        os.link(str(root / "tree" / "a.bin"), str(root / "tmp" / "tree" / "a.bin"))
    except (OSError, NotImplementedError):
        pytest.skip("this file system cannot make hard links")
    _report_link_counts(monkeypatch)
    browser.remote.refresh()
    settle(browser)
    browser.clipboard = ("remote", [("/tree", True)])
    assert browser.remote.navigate("/tmp")
    settle(browser)
    assert browser.paste_remote() == 1             # "Replace? (merged)" — Yes
    settle(browser)
    assert (root / "tree" / "a.bin").read_bytes() == b"aaa"
    assert (root / "tmp" / "tree" / "b.bin").read_bytes() == b"bbb"
    item = browser.transfers.items()[-1]
    assert item.status == "done" and "skipped" in item.error


def test_a_retried_open_still_opens(browser, monkeypatch):
    from turbossh.gui import file_open
    opened = []
    monkeypatch.setattr(file_open, "how_to_open", lambda name: (file_open.OPEN, ""))
    monkeypatch.setattr(file_open, "start", lambda path: opened.append(path) or True)
    (browser.root / "late.txt").write_text("late")
    browser.remote.refresh()
    settle(browser)
    select(browser.remote.tree, "late.txt")
    (browser.root / "late.txt").unlink()           # gone before it is copied
    copy = browser.open_remote()
    assert wait_until(lambda: not browser.busy)
    assert not opened and browser.transfers.items()[-1].status == "failed"
    (browser.root / "late.txt").write_text("late")
    browser.panel.refresh()
    browser.panel._retry_failed()
    assert wait_until(lambda: opened == [copy.local])


def test_reserved_windows_names_are_refused(browser, monkeypatch):
    from turbossh.gui import file_browser
    monkeypatch.setattr(file_browser, "_WINDOWS", True)
    for name in ("NUL", "con.txt", "COM1", "trailing."):
        assert not browser.local.do_new_file(name), name
    assert browser.local.do_new_file("console.txt")


def test_only_folders_made_for_opened_files_are_tidied(monkeypatch, tmp_path):
    from turbossh.gui import file_open
    monkeypatch.setenv("TURBOSSH_OPENED_DIR", str(tmp_path))
    ours = file_open.new_copy_folder()
    theirs = tmp_path / "keep-me"
    theirs.mkdir()
    for path in (ours, str(theirs)):
        os.utime(path, (1, 1))
    file_open.forget_old_copies()
    assert not os.path.exists(ours) and theirs.is_dir()
