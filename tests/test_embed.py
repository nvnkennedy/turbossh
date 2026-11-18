"""The optional Camera, and TurboSSHWidget: the whole GUI as a child widget of
somebody else's application. Sessions run against the loopback SSH server."""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import time

import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("pyte")

from PyQt5 import sip  # noqa: E402
from PyQt5.QtCore import QEvent, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QLineEdit, QMainWindow, QMessageBox,  # noqa: E402
                             QShortcut, QVBoxLayout, QWidget)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(25)
    return predicate()


def flush_deletes():
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture(autouse=True)
def _clean_settings(qapp):
    """Each test starts from the defaults for what these tests change, and leaves
    every setting as it found it (a changed terminal font size used to change the
    terminal width for the tests that run next)."""
    from turbossh.gui import settings, theme
    before = settings.load()
    settings.save({"camera_enabled": False, "theme": "dark"})
    yield
    theme.set_active(None)
    settings.save(before)


@pytest.fixture(autouse=True)
def _no_camera_side_effects(monkeypatch):
    """Opening a Camera tab here must never reach for ffmpeg."""
    from turbossh.gui import ffmpeg_tools

    def refuse(*_args, **_kwargs):
        raise AssertionError("a download was started")
    monkeypatch.setattr(ffmpeg_tools, "cached_ffmpeg", lambda: None)
    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", refuse)


@pytest.fixture()
def host(qapp):
    """A host application's window, with a central page TurboSSH goes on."""
    window = QMainWindow()
    window.setWindowTitle("Host")
    page = QWidget()
    QVBoxLayout(page)
    window.setCentralWidget(page)
    window.resize(1000, 700)
    window.show()
    yield window
    window.hide()
    window.deleteLater()
    QTest.qWait(30)


@pytest.fixture()
def embed(host):
    from turbossh.gui import TurboSSHWidget, qtutil
    made = []

    def make(**options):
        widget = TurboSSHWidget(host.centralWidget(), **options)
        host.centralWidget().layout().addWidget(widget)
        made.append(widget)
        QApplication.processEvents()
        return widget
    yield make
    for widget in made:
        widget.shutdown()
    assert qtutil.wait_for_teardowns(10)
    # a connect still in flight was parked: let it finish against the live server
    # (requested before this fixture, so torn down after it), not leak into the
    # next test as a retry loop against a closed port
    assert wait_until(lambda: not qtutil._PARKED, 30), \
        f"still parked: {[type(t).__name__ for t in qtutil._PARKED]}"


@pytest.fixture()
def window(qapp):
    """The desktop app's window."""
    from turbossh.gui.main_window import MainWindow

    class Window(MainWindow):
        def closeEvent(self, event):
            event.accept()

    win = Window()
    win.show()
    yield win
    win.shut_down()
    win.hide()
    win.deleteLater()
    QTest.qWait(50)


def camera_tabs(win):
    return [w for w in win._all_sessions() if w.session_kind == "camera"]


# ------------------------------------------------------------------ the Camera
def test_camera_is_off_until_asked_for(window):
    from turbossh.gui import settings
    assert settings.DEFAULTS["camera_enabled"] is False
    assert not window.camera_enabled()
    assert not window.named_actions["camera"].isVisible()
    assert not window._camera_menu_act.isVisible()
    assert window._camera_toggle_act.isVisible()           # how you ask for it
    assert not window._camera_toggle_act.isChecked()
    assert "camera" in window.hidden_home_cards()
    assert camera_tabs(window) == []


def test_view_camera_shows_it_opens_a_tab_and_remembers(window):
    from turbossh.gui import settings
    window._camera_toggle_act.trigger()
    assert window.camera_enabled() and window._camera_toggle_act.isChecked()
    assert window.named_actions["camera"].isVisible() and window._camera_menu_act.isVisible()
    assert len(camera_tabs(window)) == 1
    assert "camera" not in window.hidden_home_cards()
    assert settings.reload()["camera_enabled"] is True     # really on disk


def test_turning_the_camera_off_closes_its_tabs_and_hides_it(window):
    window._camera_toggle_act.trigger()
    window.open_camera_panel()
    panels = camera_tabs(window)
    assert len(panels) == 2
    window._camera_toggle_act.trigger()
    assert not window.camera_enabled() and camera_tabs(window) == []
    assert not window.named_actions["camera"].isVisible()
    flush_deletes()
    assert all(sip.isdeleted(p) for p in panels)


def test_a_camera_tab_can_close_while_the_camera_stays_available(window):
    window._camera_toggle_act.trigger()
    window._close_session_widget(camera_tabs(window)[0])
    assert window.camera_enabled() and window.named_actions["camera"].isVisible()


def test_hiding_a_streaming_camera_asks_first(window, monkeypatch):
    window._camera_toggle_act.trigger()
    panel = camera_tabs(window)[0]
    monkeypatch.setattr(panel, "is_streaming", lambda: True)
    asked = []

    def answer(*args, **_kwargs):
        asked.append(args[2])
        return QMessageBox.No
    monkeypatch.setattr(QMessageBox, "question", answer)
    window._camera_toggle_act.trigger()
    assert asked and "streaming" in asked[0]
    assert window.camera_enabled() and window._camera_toggle_act.isChecked()
    assert camera_tabs(window) == [panel]


def test_turning_the_camera_off_reaches_a_tab_in_split_view(window):
    window._camera_toggle_act.trigger()
    window.toggle_split()
    assert window._tiled and len(camera_tabs(window)) == 1
    window.set_camera_enabled(False)
    assert camera_tabs(window) == []


def test_settings_dialog_turns_the_camera_on_and_off(window):
    from turbossh.gui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(window)
    assert dlg.camera_on is not None and not dlg.camera_on.isChecked()
    dlg.camera_on.setChecked(True)
    assert dlg.result_settings()["camera_enabled"] is True
    dlg.done(0)
    dlg.deleteLater()


def test_show_settings_applies_the_camera_choice(window, monkeypatch):
    import turbossh.gui.main_window as module
    seen = []
    window.camera_enabled_changed.connect(seen.append)

    class Dialog:
        Accepted = 1

        def __init__(self, parent):
            pass

        def exec_(self):
            return 1

        def deleteLater(self):
            pass

        def result_settings(self):
            return {"term_font_size": 12, "camera_enabled": True}

    monkeypatch.setattr(module, "SettingsDialog", Dialog)
    window.show_settings()
    assert window.camera_enabled() and window.named_actions["camera"].isVisible()
    assert camera_tabs(window) == []                       # a preference, not a tab
    assert seen == [True]


def test_home_cards_leave_no_hole(window):
    home = window._home
    positions = {}
    for key, card in home._cards:
        index = home._grid.indexOf(card)
        if index >= 0:
            row, column, _r, _c = home._grid.getItemPosition(index)
            positions[key] = row * 3 + column
    assert "camera" not in positions
    assert sorted(positions.values()) == list(range(len(positions)))
    assert dict(home._cards)["camera"].isHidden()

    window.set_camera_enabled(True)
    assert not dict(home._cards)["camera"].isHidden()
    assert home._grid.indexOf(dict(home._cards)["camera"]) >= 0


def test_opening_the_tab_starts_no_download(qapp):
    """The ~160 MB ffmpeg is fetched when you ask, not because a tab appeared."""
    from turbossh.gui.camera_widget import CameraPanel
    panel = CameraPanel()
    QTest.qWait(40)
    assert "Scan cameras" in panel.status.text() and panel._prep is None
    assert panel.refresh_btn.isEnabled()
    panel.close_session()
    panel.deleteLater()


def test_a_camera_tab_does_not_force_a_wide_window(qapp):
    """It needed ~1040 px on one row (more with a long status), which forced the
    whole window — or a host's page — that wide."""
    from turbossh.gui.camera_widget import CameraPanel
    panel = CameraPanel()
    narrow = panel.minimumSizeHint().width()
    panel._set_status("A rather long status message that used to set the minimum "
                      "width of the whole Camera tab, and so of the window", "info")
    assert panel.minimumSizeHint().width() == narrow
    top_row = panel.layout().itemAt(0).layout()
    assert all(top_row.indexOf(w) == -1 for w in (panel.res, panel.fps, panel.view_mode))
    assert "used to set" in panel.status.toolTip()
    panel.close_session()
    panel.deleteLater()


def test_a_blank_camera_password_means_keys(qapp, monkeypatch):
    from turbossh.gui import camera_widget
    captured = []

    class Prep(camera_widget._RemotePrep):
        def __init__(self, cfg):
            captured.append(cfg)
            super().__init__(cfg)

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(camera_widget, "_RemotePrep", Prep)
    panel = camera_widget.CameraPanel()
    panel.source.setCurrentIndex(1)
    panel.r_host.setText("cam-host")
    panel.r_pass.setText("")
    panel._refresh()
    assert captured and captured[-1].password is None
    panel.close_session()
    panel.deleteLater()


def test_remote_scan_closes_its_connection_when_it_fails(qapp, monkeypatch):
    from turbossh.gui import camera_widget, ffmpeg_tools
    disconnected = []

    class Handler:
        def __init__(self, *_a, **_k):
            pass

        def connect(self):
            return True

        def disconnect(self):
            disconnected.append(True)

    def boom(*_a, **_k):
        raise RuntimeError("upload failed")
    monkeypatch.setattr(camera_widget, "SSHHandler", Handler)
    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(ffmpeg_tools, "ensure_remote_ffmpeg", boom)
    errors = []
    prep = camera_widget._RemotePrep(object())
    prep.fail.connect(errors.append)
    prep.run()
    assert disconnected == [True] and "upload failed" in errors[0]


# ------------------------------------------------------------------ good guest
def test_a_hosted_gui_leaves_the_application_alone(qapp, host, embed):
    from turbossh.gui import theme
    qapp.setStyleSheet("QPushButton { color: red; }")
    icon = qapp.windowIcon().cacheKey()
    try:
        turbo = embed()
        turbo.main_window.apply_theme("nord")
        turbo.main_window._apply_app_icon()                 # what showEvent re-asserts
        assert qapp.styleSheet() == "QPushButton { color: red; }"
        assert qapp.windowIcon().cacheKey() == icon
        assert host.windowTitle() == "Host"
        assert not turbo.main_window.isWindow()
        assert turbo.main_window.window() is host
        assert turbo.main_window.styleSheet() == theme.stylesheet("nord")
    finally:
        qapp.setStyleSheet("")


def test_the_desktop_app_still_themes_the_whole_application(qapp, window):
    from turbossh.gui import theme
    assert not window.embedded and window.isWindow()
    assert qapp.styleSheet() == theme.stylesheet("dark")
    assert window.named_actions["updates"]
    qapp.setStyleSheet("")


def test_nothing_hosted_quits_the_application(qapp, embed, monkeypatch):
    quits = []
    monkeypatch.setattr(QApplication, "quit", lambda *a: quits.append(a))
    turbo = embed()
    turbo.main_window.close()
    turbo.shutdown()
    assert quits == []


def test_no_exit_and_no_self_update_when_hosted(embed):
    turbo = embed()
    texts = [a.text() for m in turbo.menus() for a in m.actions()]
    assert "Exit" not in texts and "Check for updates…" not in texts
    assert "updates" not in turbo.action_names()
    assert "updates" in turbo.main_window.hidden_home_cards()
    assert "Exit" not in [a.text() for a in turbo.main_window._ribbon.actions()]


def test_shortcuts_only_work_inside_the_widget(embed):
    turbo = embed()
    win = turbo.main_window
    own = [s for s in win.findChildren(QShortcut) if s.parent() is win]
    assert own and all(s.context() == Qt.WidgetWithChildrenShortcut for s in own)
    with_keys = [a for m in turbo.menus() for a in m.actions() if not a.shortcut().isEmpty()]
    assert with_keys
    for action in with_keys:
        assert action.shortcutContext() == Qt.WidgetWithChildrenShortcut
        assert action in win.actions()                     # or that context never matches


def test_a_host_shortcut_still_fires_with_the_focus_outside(qapp, host, embed):
    turbo = embed()
    fired = []
    outside = QLineEdit(host.centralWidget())
    host.centralWidget().layout().addWidget(outside)
    # Ctrl+N is also File → New session: hosted, it must not be taken from the host
    shortcut = QShortcut("Ctrl+N", host, activated=lambda: fired.append(True))
    host.activateWindow()
    QTest.qWaitForWindowActive(host, 2000)
    outside.setFocus()
    QApplication.processEvents()
    if not outside.hasFocus():
        shortcut.setParent(None)
        pytest.skip("this platform plugin does not give windows the keyboard")
    called = []
    turbo.main_window.new_session = lambda *a, **k: called.append(True)
    QTest.keyClick(outside, "n", Qt.ControlModifier)
    assert fired == [True] and called == []
    shortcut.setParent(None)


def test_chrome_is_the_hosts_choice(embed):
    turbo = embed(menu_bar=False, ribbon=False, sidebar=False, log_dock=False,
                  status_bar=False, home=False)
    win = turbo.main_window
    assert not any(p.isVisible() for p in (win.menuBar(), win._ribbon, win._sidebar_dock,
                                           win._log_dock, win.statusBar()))
    assert win.tabs.count() == 0
    turbo.set_chrome(sidebar=True)
    assert win._sidebar_dock.isVisible() and not win._ribbon.isVisible()
    assert turbo.menus()                                   # still there for the host


def test_docks_cannot_float_out_of_the_host(embed):
    from PyQt5.QtWidgets import QDockWidget
    win = embed().main_window
    for dock in (win._sidebar_dock, win._log_dock):
        assert not dock.features() & QDockWidget.DockWidgetFloatable


def test_actions_join_the_hosts_toolbar(host, embed):
    turbo = embed()
    toolbar = host.addToolBar("host")
    toolbar.addAction(turbo.action("session"))
    assert turbo.action("session") in toolbar.actions()
    with pytest.raises(KeyError, match="no action"):
        turbo.action("nonsense")


def test_settings_dialog_defers_to_the_host(embed):
    from turbossh.gui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(embed(camera=True, theme="nord").main_window)
    names = [dlg.nav.item(i).text() for i in range(dlg.nav.count())]
    assert "Startup" not in names
    assert dlg.camera_on is None and not dlg.theme.isEnabled()
    assert dlg.theme.currentText() == "nord"
    result = dlg.result_settings()
    assert not {"theme", "camera_enabled", "open_docs_first_run"} & set(result)
    dlg.done(0)
    dlg.deleteLater()


def test_a_file_editor_dialog_keeps_the_theme(embed):
    from turbossh.gui import qtutil
    win = embed().main_window
    inner = QWidget(win)
    assert inner.window() is not win                       # the host's window...
    assert qtutil.owner_window(inner) is win               # ...is not who we want


# ------------------------------------------------------------------ the camera, hosted
def test_host_can_force_the_camera_without_touching_the_users_choice(embed):
    from turbossh.gui import settings
    turbo = embed(camera=True)
    assert turbo.camera_enabled() and not turbo.main_window._camera_toggle_act.isVisible()
    turbo.set_camera_enabled(False)
    assert not turbo.camera_enabled()
    assert settings.get("camera_enabled") is False
    settings.save({"camera_enabled": True})
    assert not embed(camera=False).camera_enabled()


def test_open_camera_turns_it_on_and_reports(embed):
    turbo = embed()
    changes, opened = [], []
    turbo.camera_enabled_changed.connect(changes.append)
    turbo.session_opened.connect(opened.append)
    session = turbo.open_camera()
    assert session.kind == "camera" and session.state == "open" and opened == [session]
    assert changes == [True] and turbo.camera_enabled()
    assert session.terminal is None and session.screen_text() == ""
    with pytest.raises(TypeError):
        session.send("x")
    turbo.set_camera_enabled(False)
    assert session.state == "closed" and turbo.sessions() == []


# ------------------------------------------------------------------ theme
def test_a_theme_set_by_the_host_is_not_saved(embed):
    from turbossh.gui import settings, theme
    turbo = embed(theme="nord")
    assert theme.current_name() == "nord" and turbo.current_theme() == "nord"
    assert settings.get("theme") == "dark"
    assert not turbo.main_window._theme_act.isVisible()
    turbo.set_theme("dracula")
    assert turbo.current_theme() == "dracula" and settings.get("theme") == "dark"
    turbo.shutdown()
    assert theme.current_name() == "dark"


def test_a_host_theme_lasts_while_any_widget_using_it_is_alive(embed):
    from turbossh.gui import theme
    first = embed(theme="nord")
    embed(theme="nord")
    first.shutdown()
    assert theme.current_name() == "nord"


def test_the_settings_dialog_never_restyles_the_host(qapp, embed, monkeypatch):
    from turbossh.gui.settings_dialog import SettingsDialog
    win = embed().main_window
    dlg = SettingsDialog(win)

    def broken(_name):
        raise RuntimeError("boom")
    monkeypatch.setattr(win, "apply_theme", broken)
    qapp.setStyleSheet("QPushButton { color: red; }")
    try:
        dlg._apply_theme_live("paper")
        assert qapp.styleSheet() == "QPushButton { color: red; }"
    finally:
        qapp.setStyleSheet("")
        dlg.done(0)
        dlg.deleteLater()


def test_inherit_keeps_the_hosts_styling(embed):
    from turbossh.gui import settings, theme
    turbo = embed(theme="inherit")
    assert turbo.main_window.styleSheet() == ""
    assert turbo.current_theme() == "inherit"
    assert theme.current_name() in ("dark", "light") and settings.get("theme") == "dark"


def dark_palette():
    from PyQt5.QtGui import QColor, QPalette
    palette = QPalette()
    for role, color in ((QPalette.Window, "#1e1e1e"), (QPalette.WindowText, "#e0e0e0"),
                        (QPalette.Base, "#121212"), (QPalette.AlternateBase, "#1a1a1a"),
                        (QPalette.Text, "#e0e0e0"), (QPalette.Button, "#2a2a2a"),
                        (QPalette.ButtonText, "#e0e0e0"), (QPalette.Highlight, "#2a82da"),
                        (QPalette.HighlightedText, "#ffffff")):
        palette.setColor(role, QColor(color))
    return palette


@pytest.fixture()
def app_look(qapp):
    """Set the application's style, palette and stylesheet for one test, as a
    host does, and put back exactly what was there."""
    from PyQt5.QtGui import QPalette
    from PyQt5.QtWidgets import QStyleFactory
    sheet = qapp.styleSheet()
    qapp.setStyleSheet("")                       # (the style's own name shows then)
    before = (qapp.style().objectName(), QPalette(qapp.palette()))

    def apply(style="Fusion", palette=None, stylesheet=""):
        qapp.setStyle(QStyleFactory.create(style))
        qapp.setPalette(palette or dark_palette())
        qapp.setStyleSheet(stylesheet)
    yield apply
    qapp.setStyleSheet("")
    qapp.setPalette(before[1])
    qapp.setStyle(QStyleFactory.create(before[0]) or QStyleFactory.create("Windows"))
    qapp.setStyleSheet(sheet)


# what a dark host's stylesheet may do: style check boxes, leave radio buttons
# to the style (Fusion then drew no ring on the dark background)
HOST_SHEET = ("QWidget { background: #1e1e1e; color: #e0e0e0; }\n"
              "QCheckBox::indicator { width: 16px; height: 16px; "
              "border: 2px solid #c0c0c0; border-radius: 4px; }\n"
              "QCheckBox::indicator:checked { background: #2a82da; border-color: #2a82da; }")


def visible(image, background, minimum=2.5) -> int:
    """Pixels of *image* that stand out from *background*."""
    from turbossh.gui.theme import contrast
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            if c.alpha() > 128 and contrast(c.name(), background) >= minimum:
                count += 1
    return count


def accent_pixels(image, accent="#2a82da") -> int:
    from turbossh.gui.theme import contrast
    return sum(1 for y in range(image.height()) for x in range(image.width())
               if contrast(image.pixelColor(x, y).name(), accent) < 1.15)


def indicator_image(button):
    """The indicator of a radio button / check box, as drawn (not its text)."""
    from PyQt5.QtCore import QRect
    from PyQt5.QtWidgets import QStyle, QStyleOptionButton
    option = QStyleOptionButton()
    option.initFrom(button)
    element = (QStyle.SE_RadioButtonIndicator if isinstance(button, _radio())
               else QStyle.SE_CheckBoxIndicator)
    rect = button.style().subElementRect(element, option, button)
    return button.grab(QRect(rect.x() - 1, rect.y() - 1, rect.width() + 2,
                             rect.height() + 2)).toImage()


def _radio():
    from PyQt5.QtWidgets import QRadioButton
    return QRadioButton


@pytest.mark.parametrize("stylesheet", [HOST_SHEET, ""], ids=["host-sheet", "no-sheet"])
def test_radio_rings_and_check_boxes_show_on_a_dark_host(app_look, embed, stylesheet):
    """The Connect dialog of TurboSSH in a dark Fusion host that keeps its own
    look: an unticked radio button has a ring, a ticked one is filled, and so
    are check boxes — with or without a host stylesheet."""
    from PyQt5.QtWidgets import QCheckBox, QRadioButton
    from turbossh.gui.session_dialog import SessionDialog
    app_look(stylesheet=stylesheet)
    turbo = embed(theme="inherit")
    dlg = SessionDialog(turbo.main_window)
    dlg.show()
    try:
        QTest.qWait(50)
        radios = [r for r in dlg.findChildren(QRadioButton) if r.isVisible()]
        on = [r for r in radios if r.isChecked()]
        off = [r for r in radios if not r.isChecked()]
        assert on and off
        background = "#1e1e1e"
        for radio in off:                                # a ring, not nothing
            assert visible(indicator_image(radio), background) >= 20, radio.text()
        for radio in on:                                 # filled with the accent
            assert accent_pixels(indicator_image(radio)) >= 30, radio.text()
        box = [b for b in dlg.findChildren(QCheckBox) if b.isVisible()][0]
        box.setChecked(False)
        QTest.qWait(20)
        assert visible(indicator_image(box), background) >= 20
        box.setChecked(True)
        QTest.qWait(20)
        assert accent_pixels(indicator_image(box)) >= 30
    finally:
        dlg.done(0)
        dlg.deleteLater()


def test_a_host_stylesheet_that_draws_indicators_keeps_its_own(app_look, embed):
    """The host's ::indicator rule still wins: TurboSSH draws only what the
    host leaves to the style."""
    from PyQt5.QtWidgets import QCheckBox
    app_look(stylesheet=HOST_SHEET.replace("#c0c0c0", "#ff00ff"))
    turbo = embed(theme="inherit")
    box = QCheckBox("x", turbo.main_window)
    box.show()
    QTest.qWait(30)
    image = indicator_image(box)
    magenta = sum(1 for y in range(image.height()) for x in range(image.width())
                  if image.pixelColor(x, y).name() == "#ff00ff")
    assert magenta >= 20


def test_turbossh_keeps_the_palette_and_font_of_the_host_window(host, embed):
    """A host that sets its colours and font on its window, not on the
    application: TurboSSH's widgets take them (a stylesheet of TurboSSH's on
    its window would cut them off) and its marks follow a switch to light."""
    from PyQt5.QtGui import QColor, QFont, QPalette
    from PyQt5.QtWidgets import QCheckBox, QLineEdit, QRadioButton
    host.setPalette(dark_palette())
    font = QFont(host.font())
    font.setPointSize(15)
    host.setFont(font)
    turbo = embed(theme="inherit")
    win = turbo.main_window
    field = QLineEdit(win)
    radio = QRadioButton("off", win)
    box = QCheckBox("on", win)
    box.setChecked(True)
    for w in (field, radio, box):
        w.show()
    radio.move(0, 40)
    box.move(0, 80)
    QTest.qWait(30)
    assert field.palette().color(QPalette.Base).name() == "#121212"
    assert field.font().pointSize() == 15 and radio.font().pointSize() == 15
    assert visible(indicator_image(radio), "#121212") >= 20
    assert accent_pixels(indicator_image(box)) >= 30

    light = QPalette(dark_palette())
    for role, color in ((QPalette.Window, "#f0f0f0"), (QPalette.Base, "#ffffff"),
                        (QPalette.Text, "#202020"), (QPalette.WindowText, "#202020")):
        light.setColor(role, QColor(color))
    host.setPalette(light)
    QTest.qWait(30)
    assert field.palette().color(QPalette.Base).name() == "#ffffff"
    assert visible(indicator_image(radio), "#ffffff") >= 20


def test_a_theme_of_its_own_gives_the_marks_back(embed):
    from PyQt5.QtWidgets import QCheckBox
    turbo = embed(theme="inherit")
    box = QCheckBox("x", turbo.main_window)
    box.show()
    QTest.qWait(20)
    assert box.property("turbossh_indicator")
    turbo.set_theme("nord")
    assert not box.property("turbossh_indicator")
    assert turbo.main_window.styleSheet()


def magenta_pixels(image) -> int:
    return sum(1 for y in range(image.height()) for x in range(image.width())
               if image.pixelColor(x, y).name() == "#ff00ff")


def test_a_host_stylesheet_set_later_still_wins(qapp, app_look, embed):
    """A host that switches theme by setting its stylesheet after TurboSSH's
    marks were drawn: the host's rule takes over (Qt leaves a widget with a
    style of its own out of a new application stylesheet)."""
    from PyQt5.QtWidgets import QCheckBox
    app_look(stylesheet="")
    turbo = embed(theme="inherit")
    box = QCheckBox("x", turbo.main_window)
    box.show()
    QTest.qWait(30)
    assert box.property("turbossh_indicator")
    qapp.setStyleSheet(HOST_SHEET.replace("#c0c0c0", "#ff00ff"))
    QTest.qWait(30)
    assert magenta_pixels(indicator_image(box)) >= 20
    qapp.setStyleSheet("")                              # and back: TurboSSH's ring
    QTest.qWait(30)
    assert magenta_pixels(indicator_image(box)) == 0
    assert visible(indicator_image(box), "#121212") >= 20


def test_a_host_style_of_its_own_is_left_alone(qapp, app_look, embed):
    """A host with its own style (a proxy, a desktop theme): TurboSSH's marks
    are the host's, as before — only palette-drawn styles get the help."""
    from PyQt5.QtWidgets import QCheckBox, QProxyStyle, QStyleFactory
    app_look(stylesheet="")

    class HostStyle(QProxyStyle):
        pass
    qapp.setStyle(HostStyle(QStyleFactory.create("Fusion")))
    turbo = embed(theme="inherit")
    box = QCheckBox("x", turbo.main_window)
    box.show()
    QTest.qWait(30)
    assert not box.property("turbossh_indicator")


def test_the_application_filter_goes_with_the_last_window(embed):
    from turbossh.gui import indicators
    turbo = embed(theme="inherit")
    assert indicators._filter is not None
    turbo.set_theme("nord")                             # a look of its own
    assert indicators._filter is None
    turbo.set_theme("inherit")
    assert indicators._filter is not None
    turbo.shutdown()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    assert indicators._filter is None


def test_the_ring_takes_the_text_colour_when_the_palette_is_flat(qapp):
    from PyQt5.QtGui import QColor, QPalette
    from turbossh.gui.indicators import indicator_colors
    flat = dark_palette()
    flat.setColor(QPalette.Text, QColor("#2c2c2c"))      # text barely off the base
    flat.setColor(QPalette.HighlightedText, QColor("#2a82da"))   # and on the accent
    colors = indicator_colors(flat)
    assert colors["edge"] == "#ffffff"                   # the text would not show
    assert colors["on_accent"] in ("#ffffff", "#000000")
    usable = dark_palette()
    usable.setColor(QPalette.Base, QColor("#1e1e1e"))
    usable.setColor(QPalette.Text, QColor("#9a9a9a"))
    assert indicator_colors(usable)["edge"] in ("#9a9a9a", "#5c5c5c")


def test_set_theme_is_the_users_choice_when_the_host_set_none(embed):
    from turbossh.gui import settings, theme
    turbo = embed()
    seen = []
    turbo.theme_changed.connect(seen.append)
    turbo.set_theme("paper")
    assert settings.get("theme") == "paper" and seen == ["paper"]
    assert turbo.main_window.styleSheet() == theme.stylesheet("paper")
    with pytest.raises(ValueError):
        turbo.set_theme("nonsense")


def test_unknown_theme_is_refused_up_front(qapp):
    from turbossh.gui import TurboSSHWidget
    with pytest.raises(ValueError, match="unknown theme"):
        TurboSSHWidget(theme="nonsense")


# ------------------------------------------------------------------ sessions
def _connect(turbo, ssh_server, password="good", **extra):
    return turbo.connect_ssh("127.0.0.1", port=ssh_server.port, username="u",
                             password=password, name="loop", **extra)


def test_ssh_session_from_code_end_to_end(ssh_server, embed):
    turbo = embed()
    opened, closed, logged, up = [], [], [], []
    turbo.session_opened.connect(opened.append)
    turbo.session_closed.connect(closed.append)
    turbo.log_message.connect(logged.append)

    session = _connect(turbo, ssh_server)
    session.connected.connect(lambda: up.append(True))
    assert opened == [session] and turbo.sessions() == [session]
    assert (session.kind, session.name, session.state) == ("ssh", "loop", "connecting")
    assert turbo.current_session() is session
    with pytest.raises(RuntimeError, match="connecting"):
        session.send_line("too early")                     # would be lost, so it says so

    assert wait_until(lambda: session.state == "connected")
    assert up == [True] and session.handler.is_connected
    assert any("Opening 'loop'" in line for line in logged)

    session.send("typed-from-the-host")
    assert wait_until(lambda: "typed-from-the-host" in session.screen_text())
    session.send(b"raw-bytes")
    assert wait_until(lambda: "raw-bytes" in session.screen_text())

    finished = []
    session.closed.connect(lambda: finished.append(True))
    session.close()
    assert session.state == "closed" and finished == [True]
    assert closed == [session] and turbo.sessions() == []
    with pytest.raises(RuntimeError, match="closed"):
        session.send("x")
    session.close()                                        # twice is fine


def test_a_connection_from_code_leaves_nothing_on_disk(ssh_server, embed):
    from turbossh.gui import sessions as sessions_mod
    turbo = embed()
    session = _connect(turbo, ssh_server, password="good")
    assert wait_until(lambda: session.state == "connected")
    assert "loop" not in turbo.saved_session_names()
    assert sessions_mod.SessionStore.password("loop") is None
    if os.path.exists(sessions_mod._FILE):
        with open(sessions_mod._FILE, encoding="utf-8") as fh:
            assert '"loop"' not in fh.read()


def test_host_keys_are_checked_unless_the_host_says_otherwise(ssh_server, embed):
    turbo = embed()
    checked = _connect(turbo, ssh_server)
    lab = _connect(turbo, ssh_server, ignore_host_key=True)
    assert checked.widget._cfg.host_key_policy == "auto"   # known_hosts, refuse a change
    assert lab.widget._cfg.host_key_policy == "ignore"
    assert wait_until(lambda: checked.state == lab.state == "connected")


def test_a_session_saved_elsewhere_after_start_can_be_opened(ssh_server, embed):
    from turbossh.gui.sessions import SessionStore
    turbo = embed()
    other = SessionStore()                                 # e.g. the desktop app
    other.save({"name": "later", "type": "ssh", "host": "127.0.0.1",
                "port": ssh_server.port, "user": "u", "ignore_hostkey": True}, "good")
    try:
        assert "later" in turbo.saved_session_names()
        session = turbo.open_saved("later")
        assert wait_until(lambda: session.state == "connected")
    finally:
        other.delete("later")


def test_failed_login_is_reported(ssh_server, embed):
    turbo = embed()
    session = _connect(turbo, ssh_server, password="WRONG")
    reasons = []
    session.failed.connect(reasons.append)
    assert wait_until(lambda: session.state == "failed")
    assert reasons and "loop" in reasons[0]


def test_open_saved_uses_the_stored_password(ssh_server, embed):
    turbo = embed()
    with pytest.raises(KeyError):
        turbo.open_saved("nobody")
    store = turbo.main_window.store
    store.save({"name": "saved", "type": "ssh", "host": "127.0.0.1",
                "port": ssh_server.port, "user": "u", "ignore_hostkey": True}, "good")
    try:
        assert "saved" in turbo.saved_session_names()
        session = turbo.open_saved("saved")
        assert wait_until(lambda: session.state == "connected")
    finally:
        store.delete("saved")


def test_activate_brings_a_tab_forward_and_current_changes_are_reported(ssh_server, embed):
    turbo = embed()
    first = _connect(turbo, ssh_server)
    second = _connect(turbo, ssh_server)
    current = []
    turbo.current_session_changed.connect(current.append)
    assert turbo.current_session() is second
    first.activate()
    assert turbo.current_session() is first and current == [first]
    assert wait_until(lambda: first.state == second.state == "connected")


def test_set_focus_lands_in_the_terminal(ssh_server, host, embed):
    turbo = embed()
    session = _connect(turbo, ssh_server)
    host.activateWindow()
    QTest.qWaitForWindowActive(host, 2000)
    session.terminal.clearFocus()
    turbo.setFocus()
    QApplication.processEvents()
    if QApplication.focusWidget() is None:
        pytest.skip("this platform plugin does not give windows the keyboard")
    assert QApplication.focusWidget() is session.terminal


# ------------------------------------------------------------------ lifetime
def test_shutdown_closes_everything_and_is_final(ssh_server, embed):
    from turbossh.gui import qtutil
    turbo = embed()
    session = _connect(turbo, ssh_server)
    assert wait_until(lambda: session.state == "connected")
    handler = session.handler
    closed = []
    turbo.session_closed.connect(closed.append)

    turbo.shutdown()
    turbo.shutdown()
    assert turbo.is_shut_down and session.state == "closed" and closed == [session]
    assert turbo.main_window.tabs.indexOf(session.widget) == -1
    assert qtutil.wait_for_teardowns(10) and not handler.is_connected
    for call in (lambda: turbo.connect_ssh("x"), turbo.open_local_terminal, turbo.open_camera,
                 lambda: turbo.open_saved("x"), lambda: turbo.set_theme("dark")):
        with pytest.raises(RuntimeError, match="shut down"):
            call()


def _hosted(qapp, ssh_server):
    from turbossh.gui import TurboSSHWidget
    parent = QWidget()
    QVBoxLayout(parent)
    turbo = TurboSSHWidget(parent)
    parent.layout().addWidget(turbo)
    session = _connect(turbo, ssh_server)
    assert wait_until(lambda: session.state == "connected")
    return parent, turbo, session, session.handler


def _closed_cleanly(turbo, session, handler):
    from turbossh.gui import embed, qtutil
    assert turbo.is_shut_down and id(turbo) not in embed._LIVE
    assert session.state == "closed"
    assert wait_until(lambda: qtutil.wait_for_teardowns(0.1) and not handler.is_connected)


def test_deleting_a_parent_closes_the_sessions(qapp, ssh_server):
    """Qt tears the child down with no event a Python class can see. Sessions own
    running QThreads: losing them unnoticed would abort the whole host process."""
    parent, turbo, session, handler = _hosted(qapp, ssh_server)
    sip.delete(parent)
    _closed_cleanly(turbo, session, handler)


def test_delete_later_closes_the_sessions(qapp, ssh_server):
    parent, turbo, session, handler = _hosted(qapp, ssh_server)
    parent.layout().removeWidget(turbo)
    turbo.setParent(None)
    turbo.deleteLater()
    flush_deletes()
    assert sip.isdeleted(turbo)
    _closed_cleanly(turbo, session, handler)
    parent.deleteLater()


def test_dropping_the_last_reference_to_a_parent_closes_the_sessions(qapp, ssh_server):
    import gc
    parent, turbo, session, handler = _hosted(qapp, ssh_server)
    del parent
    gc.collect()
    _closed_cleanly(turbo, session, handler)


def test_a_parentless_widget_the_host_forgot_is_kept_alive(qapp, ssh_server):
    """Its sessions own running QThreads: collecting it would abort the process."""
    import gc
    from turbossh.gui import TurboSSHWidget, embed
    turbo = TurboSSHWidget()
    session = _connect(turbo, ssh_server)
    assert wait_until(lambda: session.state == "connected")
    handler, key = session.handler, id(turbo)
    del turbo
    gc.collect()
    QTest.qWait(30)
    assert key in embed._LIVE and session.state == "connected"
    embed._LIVE[key].shutdown()
    assert wait_until(lambda: not handler.is_connected)


def test_quitting_the_application_shuts_everything_down(qapp, ssh_server):
    from turbossh.gui import embed
    parent, turbo, session, handler = _hosted(qapp, ssh_server)
    embed._shutdown_all()                                  # what aboutToQuit runs
    _closed_cleanly(turbo, session, handler)
    assert not handler.is_connected                        # joined, not just started
    parent.deleteLater()


def test_about_to_quit_is_hooked_once(qapp, embed):
    from turbossh.gui import embed as module
    embed()
    embed()
    assert module._hooked_app is qapp


def test_close_event_on_a_stand_alone_widget_shuts_it_down(qapp):
    from turbossh.gui import TurboSSHWidget
    turbo = TurboSSHWidget()
    turbo.show()
    turbo.close()
    assert turbo.is_shut_down
    turbo.deleteLater()
    QTest.qWait(30)


def test_confirm_close_asks_only_with_sessions_open(ssh_server, embed, monkeypatch):
    turbo = embed()
    asked = []

    def answer(*_a, **_k):
        asked.append(True)
        return QMessageBox.No
    monkeypatch.setattr(QMessageBox, "question", answer)
    assert turbo.confirm_close() is True and asked == []
    _connect(turbo, ssh_server)
    assert turbo.confirm_close() is False and asked == [True]


def test_crash_guard_never_replaces_the_hosts_hook(embed, monkeypatch):
    def mine(*_args):
        pass
    monkeypatch.setattr(sys, "excepthook", mine)
    embed()
    assert sys.excepthook is mine


def test_crash_guard_stops_an_uncaught_error_aborting_a_host_with_no_hook(embed, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    turbo = embed()
    assert sys.excepthook is not sys.__excepthook__
    logged = []
    turbo.log_message.connect(logged.append)
    sys.excepthook(ValueError, ValueError("boom"), None)
    assert logged and "boom" in logged[-1]


# ------------------------------------------------------------------ the rest
def test_the_package_exports_the_api_without_importing_qt_eagerly():
    import subprocess
    code = ("import sys, turbossh.gui; assert 'PyQt5' not in sys.modules; "
            "from turbossh.gui import settings; assert 'PyQt5' not in sys.modules; "
            "print(turbossh.gui.TurboSSHWidget.__name__); assert 'PyQt5' in sys.modules")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "TurboSSHWidget"


def test_unknown_attribute_is_still_an_attribute_error():
    import turbossh.gui
    with pytest.raises(AttributeError):
        turbossh.gui.NoSuchThing


def _public_names(cls):
    qt_overrides = {"sizeHint", "focusInEvent", "closeEvent"}
    for name, member in vars(cls).items():
        if name.startswith("_") or name in qt_overrides:
            continue
        if inspect.isfunction(member) or isinstance(member, property) \
                or type(member).__name__ == "pyqtSignal":
            yield name


def test_every_public_name_is_in_the_reference():
    """EMBEDDING.md is the contract: it cannot drift from the code."""
    from turbossh.gui import EmbeddedSession, TurboSSHWidget
    with open(os.path.join(ROOT, "EMBEDDING.md"), encoding="utf-8") as fh:
        text = fh.read()
    names = [n for cls in (TurboSSHWidget, EmbeddedSession) for n in _public_names(cls)]
    assert len(names) > 30
    missing = [n for n in names if f"`{n}" not in text]
    assert not missing, f"not documented in EMBEDDING.md: {missing}"


def test_every_action_name_is_in_the_reference(embed):
    with open(os.path.join(ROOT, "EMBEDDING.md"), encoding="utf-8") as fh:
        text = fh.read()
    missing = [n for n in embed().action_names() if f"`{n}`" not in text]
    assert not missing, f"not documented in EMBEDDING.md: {missing}"


def _load_example(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "examples", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_embed_example_runs(qapp, ssh_server, monkeypatch):
    window = _load_example("embed_demo").HostWindow()
    window.show()
    try:
        assert window.turbo.main_window.window() is window
        assert window.menuBar().actions()                  # TurboSSH's menus, in ours
        window.host.setText("127.0.0.1")
        window.user.setText("u")
        window.password.setText("good")
        real = window.turbo.connect_ssh
        monkeypatch.setattr(window.turbo, "connect_ssh",
                            lambda host, **kw: real(host, port=ssh_server.port, **kw))
        window.connect_target()
        session = window.turbo.sessions()[0]
        assert wait_until(lambda: session.state == "connected")
    finally:
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        window.close()
    assert window.turbo.is_shut_down
    window.deleteLater()
    QTest.qWait(30)


def test_the_library_demo_imports_and_connects(qapp, ssh_server):
    demo = _load_example("pyqt5_demo").Demo()
    demo.host.setText("127.0.0.1")
    demo.port.setValue(ssh_server.port)
    demo.user.setText("u")
    demo.password.setText("good")
    demo.on_connect()
    assert wait_until(lambda: demo.btn_run.isEnabled())
    demo.command.setText("echo hi")
    demo.on_run()
    assert wait_until(lambda: "echo hi" in demo.log.toPlainText())
    demo.close()
    demo.deleteLater()
    QTest.qWait(30)


def test_split_view_menu_actions_skip_the_home_tab(window):
    from turbossh.gui.vt100 import Vt100Terminal

    class Page(QWidget):
        def close_session(self):
            pass

    page = Page()
    page.term = Vt100Terminal(send_fn=lambda data: None, parent=page)
    window.tabs.addTab(page, "s")
    window.toggle_split()
    assert window._home in [w for (w, _t) in window._tiled_items]
    assert window._current_session_widget() is page       # was the Home tile
    window._untile()
    page.term.cleanup()
