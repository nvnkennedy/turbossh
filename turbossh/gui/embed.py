"""Put the whole TurboSSH GUI inside another PyQt5 application.

    from turbossh.gui import TurboSSHWidget

    turbo = TurboSSHWidget(parent=self, log_dock=False)
    tabs.addTab(turbo, "SSH")                    # an ordinary QWidget

    session = turbo.connect_ssh("10.0.0.5", username="root", password="…")
    session.connected.connect(lambda: session.send_line("uname -a"))

It is the same window as the desktop app, running as a child widget. Everything
global stays with the host: the stylesheet and window icon are not touched, the
keyboard shortcuts only work while the focus is inside the widget, and nothing
here ever quits the application. EMBEDDING.md is the full reference.

Everything in this module must be used on the GUI thread.
"""

from __future__ import annotations

import sys
import traceback
from typing import Dict, List, Optional, Sequence, Union

from PyQt5.QtCore import QCoreApplication, QObject, QSize, pyqtSignal
from PyQt5.QtWidgets import QAction, QMenu, QMessageBox, QVBoxLayout, QWidget

from . import qtutil
from . import theme as theme_mod
from .main_window import INHERIT_THEME, MainWindow, WindowOptions

__all__ = ["TurboSSHWidget", "EmbeddedSession", "INHERIT_THEME"]

_QUIT_WAIT = 6.0        # seconds a quitting host gives remote ports to be released

# Every widget that has not been shut down yet, by id(). A strong reference on
# purpose: its sessions own QThreads, and Qt aborts the whole process if one is
# destroyed while running — which is what would happen if a host simply dropped
# its last reference, or deleted a parent, with sessions open.
_LIVE: Dict[int, "TurboSSHWidget"] = {}
_hooked_app = None
# True once the last shutdown has waited for the teardowns as long as its caller
# chose (``shutdown(wait=…)``, or control.py ending the process): quitting then
# does not wait another 6 s on top of it.
_teardowns_waited = False


def _shutdown_all() -> None:
    """The application is quitting (a child widget never gets a closeEvent)."""
    live = list(_LIVE.values())
    for widget in live:
        widget.shutdown()
    if live or not _teardowns_waited:
        qtutil.wait_for_teardowns(_QUIT_WAIT)


def _quit_wait_done() -> None:
    """The owner of the process has given the teardowns the time it chose (even
    none): quitting must not add a wait of its own."""
    global _teardowns_waited
    _teardowns_waited = True


def _reap(key: int) -> None:
    """The host deleted the widget (or a parent of it) without shutting it down."""
    widget = _LIVE.get(key)
    if widget is not None:
        widget.shutdown()


def _install_crash_guard() -> None:
    """PyQt5 aborts the process when an exception escapes a slot, unless
    ``sys.excepthook`` has been replaced: without this, one bug in a TurboSSH slot
    would take the host down. A host that installed its own hook keeps it."""
    if sys.excepthook is not sys.__excepthook__:
        return

    def report(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:                             # None under pythonw or a windowed host
            if sys.stderr is not None:
                sys.stderr.write(text)
        except Exception:
            pass
        for widget in list(_LIVE.values()):
            try:
                widget.log_message.emit(f"[ERROR] Unexpected error:\n{text.rstrip()}")
            except RuntimeError:
                pass

    sys.excepthook = report


class EmbeddedSession(QObject):
    """One open tab: returned by the ``open_*`` / ``connect_*`` methods of
    :class:`TurboSSHWidget` and delivered by its ``session_opened`` signal."""

    connected = pyqtSignal()            # the link is up (again, after a reconnect)
    disconnected = pyqtSignal(str)      # the link dropped; SSH retries by itself
    failed = pyqtSignal(str)            # it could not connect at all
    closed = pyqtSignal()               # the tab is gone

    def __init__(self, owner: "TurboSSHWidget", page: QWidget, name: str):
        super().__init__()
        self._owner = owner
        self._page = page
        self.name = name
        self.kind: str = getattr(page, "session_kind", "other")
        self.state = "connecting" if self.kind in ("ssh", "serial") else "open"
        if hasattr(page, "connected"):
            page.connected.connect(self._on_link)
        if hasattr(page, "failed"):
            page.failed.connect(self._on_failed)

    def __repr__(self):
        return f"<EmbeddedSession {self.kind} {self.name!r} {self.state}>"

    # ---- what it is ----
    @property
    def widget(self) -> QWidget:
        """The tab's page."""
        return self._page

    @property
    def terminal(self):
        """The terminal widget, or ``None`` for a Camera tab."""
        return getattr(self._page, "term", None)

    @property
    def handler(self):
        """The connection's :class:`~turbossh.SSHHandler` (or
        :class:`~turbossh.SerialHandler`), to run commands or transfer files over
        it; ``None`` until it has connected. It is in safe mode: calls return an
        ``OperationResult``. It blocks, so keep it off the GUI thread."""
        return getattr(self._page, "handler", None)

    # ---- what it can do ----
    def send(self, data: Union[str, bytes]) -> None:
        """Type into the terminal: text, or bytes such as ``b"\\x03"`` for Ctrl-C.
        An SSH or serial session takes input only while connected — before that
        (or while it reconnects) it would be lost, so this raises instead."""
        if self.state == "closed":
            raise RuntimeError(f"session {self.name!r} is closed")
        term = self.terminal
        if term is None:
            raise TypeError(f"a {self.kind} tab has no terminal")
        if self.kind in ("ssh", "serial") and self.state != "connected":
            raise RuntimeError(f"session {self.name!r} is {self.state}, not connected; "
                               f"send from its connected signal")
        term._send(data.encode("utf-8") if isinstance(data, str) else bytes(data))

    def send_line(self, text: str) -> None:
        """Type a line and press Enter."""
        self.send(text + "\r")

    def screen_text(self) -> str:
        """What the terminal shows right now, as plain text."""
        term = self.terminal
        if term is None:
            return ""
        return "\n".join(line.rstrip() for line in term.screen.display).rstrip("\n")

    def activate(self) -> None:
        """Bring this tab to the front and give it the keyboard."""
        if self.state != "closed":
            self._owner.main_window.activate_session_widget(self._page)

    def close(self) -> None:
        """Close the tab and its connection."""
        if self.state != "closed":
            self._owner.main_window._close_session_widget(self._page)

    # ---- internals ----
    def _on_link(self, up: bool, message: str) -> None:
        if up:
            self.state = "connected"
            self.connected.emit()
        else:
            self.state = "disconnected"
            self.disconnected.emit(message)

    def _on_failed(self, message: str) -> None:
        self.state = "failed"
        self.failed.emit(message)

    def _finish(self) -> None:
        """The tab is gone (or the host deleted it): close what is still open."""
        if self.state == "closed":
            return
        self.state = "closed"
        try:
            self._page.close_session()   # idempotent; copes with a deleted widget
        except Exception:
            pass
        self.closed.emit()


class TurboSSHWidget(QWidget):
    """The entire TurboSSH GUI as a QWidget for another PyQt5 application.

    ``menu_bar``, ``ribbon``, ``sidebar``, ``log_dock``, ``status_bar`` and
    ``home`` choose what is shown around the tabs. ``camera`` is ``None`` to
    follow the user's own View → Camera choice, or a bool for the host to decide.
    ``theme`` is ``None`` for the user's saved TurboSSH theme, one of
    ``theme.THEME_NAMES``, or ``"inherit"`` to keep the host's own styling.
    """

    session_opened = pyqtSignal(object)             # EmbeddedSession
    session_closed = pyqtSignal(object)             # EmbeddedSession
    current_session_changed = pyqtSignal(object)    # EmbeddedSession or None
    log_message = pyqtSignal(str)                   # every line of the log dock
    theme_changed = pyqtSignal(str)
    camera_enabled_changed = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None, *, menu_bar: bool = True,
                 ribbon: bool = True, sidebar: bool = True, log_dock: bool = True,
                 status_bar: bool = True, home: bool = True,
                 camera: Optional[bool] = None, theme: Optional[str] = None):
        super().__init__(parent)
        if theme not in (None, INHERIT_THEME) and theme not in theme_mod.THEME_NAMES:
            raise ValueError(f"unknown theme {theme!r}; expected None, "
                             f"{INHERIT_THEME!r} or one of {theme_mod.THEME_NAMES}")
        self._sessions: Dict[QWidget, EmbeddedSession] = {}
        self._is_shut_down = False
        self._win = MainWindow(self, WindowOptions(
            embedded=True, menu_bar=menu_bar, ribbon=ribbon, sidebar=sidebar,
            log_dock=log_dock, status_bar=status_bar, home=home, camera=camera,
            theme=theme))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._win)

        self._win.session_opened.connect(self._on_opened)
        self._win.session_closed.connect(self._on_closed)
        self._win.current_page_changed.connect(self._on_current_changed)
        self._win.log_panel.appended.connect(self.log_message)
        self._win.theme_changed.connect(self.theme_changed)
        self._win.camera_enabled_changed.connect(self.camera_enabled_changed)

        self._register()

    def _register(self) -> None:
        global _hooked_app
        key = id(self)
        _LIVE[key] = self
        # not a bound method: that would keep the widget alive after shutdown
        self.destroyed.connect(lambda _obj=None, k=key: _reap(k))
        app = QCoreApplication.instance()
        if app is not None and app is not _hooked_app:
            app.aboutToQuit.connect(_shutdown_all)
            _hooked_app = app
        _install_crash_guard()

    # ---- opening things ----
    def connect_ssh(self, host: str, *, username: Optional[str] = None,
                    password: Optional[str] = None, port: int = 22,
                    domain: Optional[str] = None, key_file: Optional[str] = None,
                    name: Optional[str] = None, jump_host: Optional[str] = None,
                    jump_username: Optional[str] = None,
                    jump_password: Optional[str] = None, jump_port: int = 22,
                    jump_domain: Optional[str] = None, legacy_algorithms: bool = False,
                    ignore_host_key: bool = False,
                    quick_commands: Sequence[str] = ()) -> EmbeddedSession:
        """Open an SSH tab. Nothing is written to the saved sessions or the vault,
        and with no password it logs in with the key file, the agent or the
        default keys. Host keys are checked like the library does (known_hosts,
        a changed key is refused); ``ignore_host_key=True`` accepts any key, for
        lab devices that are reimaged and get a new one."""
        return self._open({
            "name": name or (f"{username}@{host}" if username else host),
            "type": "ssh", "host": host, "port": port, "user": username or "",
            "domain": domain or "", "key_file": key_file or "",
            "ignore_hostkey": ignore_host_key, "legacy": legacy_algorithms,
            "use_jump": bool(jump_host), "jhost": jump_host or "", "jport": jump_port,
            "juser": jump_username or "", "jdomain": jump_domain or "",
            "quick_cmds": ",".join(quick_commands),
        }, password or "", jump_password or "")

    def open_serial(self, device: str, *, baudrate: int = 115200,
                    name: Optional[str] = None,
                    quick_commands: Sequence[str] = ()) -> Optional[EmbeddedSession]:
        """Open a serial port on this machine. ``None`` if the port is already
        open in another tab and the user chose to leave it there."""
        return self._open({
            "name": name or device, "type": "serial", "device": device,
            "baud": baudrate, "via_ssh": False, "quick_cmds": ",".join(quick_commands),
        })

    def open_serial_over_ssh(self, device: str, host: str, *,
                             username: Optional[str] = None,
                             password: Optional[str] = None, port: int = 22,
                             domain: Optional[str] = None,
                             key_file: Optional[str] = None, baudrate: int = 115200,
                             name: Optional[str] = None, ignore_host_key: bool = False,
                             quick_commands: Sequence[str] = ()) -> Optional[EmbeddedSession]:
        """Open a serial port on another machine, over SSH (not saved)."""
        return self._open({
            "name": name or f"{device} on {host}", "type": "serial", "device": device,
            "baud": baudrate, "via_ssh": True, "host": host, "port": port,
            "user": username or "", "domain": domain or "", "key_file": key_file or "",
            "ignore_hostkey": ignore_host_key, "quick_cmds": ",".join(quick_commands),
        }, password or "")

    def open_saved(self, name: str) -> Optional[EmbeddedSession]:
        """Open a session saved in TurboSSH, with its stored passwords. ``KeyError``
        if there is none by that name; ``None`` if the user declined to take over
        a serial port that is already open."""
        self._require_running()
        self._reload_saved()
        if not self._win.store.exists(name):
            raise KeyError(name)
        return self._sessions.get(self._win.open_session_named(name))

    def _reload_saved(self) -> None:
        """Re-read sessions.json if it changed: the desktop app or another widget
        may have saved one since this widget was created. Only then: a host
        polling the names rebuilt the lists each time, losing the selection."""
        if self._win.store.reload_if_changed():
            self._win.refresh_sessions()

    def open_local_terminal(self) -> EmbeddedSession:
        """Open a shell on this machine in a tab."""
        self._require_running()
        return self._sessions[self._win.open_local_terminal()]

    def open_camera(self) -> EmbeddedSession:
        """Open a Camera tab, turning the Camera on first if it is off."""
        self._require_running()
        self._win.set_camera_enabled(True)
        return self._sessions[self._win.open_camera_panel()]

    def open_home(self) -> None:
        """Show (or focus) the Home tab."""
        self._require_running()
        self._win.open_home()

    def new_session_dialog(self) -> None:
        """Show TurboSSH's own New session dialog."""
        self._require_running()
        self._win.new_session()

    def show_settings(self) -> None:
        """Show TurboSSH's own Settings dialog."""
        self._require_running()
        self._win.show_settings()

    def _open(self, description: dict, password: str = "",
              jump_password: str = "") -> Optional[EmbeddedSession]:
        self._require_running()
        return self._sessions.get(
            self._win.open_session_config(description, password, jump_password))

    # ---- what is open ----
    def sessions(self) -> List[EmbeddedSession]:
        """Every open tab, in the order they were opened."""
        return list(self._sessions.values())

    def current_session(self) -> Optional[EmbeddedSession]:
        """The session the user is working in, or ``None``."""
        return self._sessions.get(self._win._current_session_widget())

    def saved_session_names(self) -> List[str]:
        self._reload_saved()
        return self._win.store.names()

    # ---- camera, look ----
    def camera_enabled(self) -> bool:
        return self._win.camera_enabled()

    def set_camera_enabled(self, enabled: bool) -> None:
        """Show or hide the Camera. Hiding it closes any Camera tabs."""
        self._require_running()
        self._win.set_camera_enabled(enabled)

    def current_theme(self) -> str:
        return self._win.current_theme()

    def set_theme(self, name: str) -> None:
        """Switch theme at once: a name from ``theme.THEME_NAMES`` or ``"inherit"``.
        Saved as the user's choice unless the host set ``theme=`` when creating
        this widget, in which case it lasts for this run only."""
        self._require_running()
        self._win.set_theme(name)

    def set_chrome(self, *, menu_bar: Optional[bool] = None,
                   ribbon: Optional[bool] = None, sidebar: Optional[bool] = None,
                   log_dock: Optional[bool] = None,
                   status_bar: Optional[bool] = None) -> None:
        """Show or hide the parts around the tabs; ``None`` leaves one as it is."""
        self._require_running()
        self._win.set_chrome(menu_bar=menu_bar, ribbon=ribbon, sidebar=sidebar,
                             log_dock=log_dock, status_bar=status_bar)

    # ---- joining the host's own menus and toolbars ----
    def action_names(self) -> List[str]:
        return list(self._win.named_actions)

    def action(self, name: str) -> QAction:
        """A TurboSSH command as a QAction, for the host's own toolbar or menu."""
        try:
            return self._win.named_actions[name]
        except KeyError:
            raise KeyError(f"no action {name!r}; have {self.action_names()}") from None

    def menus(self) -> List[QMenu]:
        """TurboSSH's menus, for ``host.menuBar().addMenu(menu)`` (pair with
        ``menu_bar=False``)."""
        return [entry.menu() for entry in self._win.menuBar().actions() if entry.menu()]

    @property
    def main_window(self) -> MainWindow:
        """The window inside, for what this API does not cover. Its methods are
        internal and may change between releases."""
        return self._win

    # ---- lifetime ----
    @property
    def is_shut_down(self) -> bool:
        return self._is_shut_down

    def confirm_close(self) -> bool:
        """Ask before closing, as the desktop app does: ``True`` when nothing is
        open or the user agreed. Call it from the host's own ``closeEvent``.
        Remote-file editors with unsaved changes are asked about too."""
        from .main_window import confirm_unsaved_editors
        if self._sessions and QMessageBox.question(
                self, "Close TurboSSH",
                f"{len(self._sessions)} open session(s) will be disconnected and "
                f"closed.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return False
        return confirm_unsaved_editors(self)

    def shutdown(self, wait: float = 0.0) -> None:
        """Close every session and stop all background work; the application keeps
        running. Safe to call twice, and done for you when the application quits
        or the widget is deleted. ``wait`` is how many seconds to block for remote
        ports and cameras to be released — pass some if the process is about to end."""
        global _teardowns_waited
        if not self._is_shut_down:
            self._is_shut_down = True
            _teardowns_waited = False    # new teardowns: nobody has waited for them
            _LIVE.pop(id(self), None)
            try:
                self._win.shut_down()
            except RuntimeError:         # the host already deleted the Qt side
                pass
            for session in list(self._sessions.values()):
                self._on_closed(session.widget)
            # the host's theme is process-wide: drop it only with its last user
            if self._win.theme_locked and not any(
                    other._win.theme_locked for other in _LIVE.values()):
                theme_mod.set_active(None)
        if wait > 0:
            qtutil.wait_for_teardowns(wait)
            _teardowns_waited = True

    def _require_running(self) -> None:
        if self._is_shut_down:
            raise RuntimeError("this TurboSSHWidget has been shut down")

    # ---- Qt ----
    def sizeHint(self) -> QSize:
        return QSize(1100, 700)

    def focusInEvent(self, event) -> None:
        """``setFocus()`` from the host lands in the terminal, ready to type."""
        super().focusInEvent(event)
        term = self._win._current_term()
        if term is not None:
            term.setFocus()

    def closeEvent(self, event) -> None:
        """Reached only when this is shown as a window of its own."""
        self.shutdown()
        super().closeEvent(event)

    # ---- from the window inside ----
    def _on_opened(self, page: QWidget, name: str) -> None:
        session = EmbeddedSession(self, page, name)
        self._sessions[page] = session
        self.session_opened.emit(session)

    def _on_closed(self, page: QWidget) -> None:
        session = self._sessions.pop(page, None)
        if session is not None:
            session._finish()
            try:
                self.session_closed.emit(session)
            except RuntimeError:         # the host already deleted this widget
                pass

    def _on_current_changed(self, page) -> None:
        """The tab in front changed (a new tab is announced after session_opened,
        so it is known here: it used to come out as None)."""
        self.current_session_changed.emit(self._sessions.get(page))
