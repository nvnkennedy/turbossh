"""GUI entry point: builds the QApplication, installs a crash-safe exception
hook (popup + log, non-fatal), creates a desktop shortcut on first run, and
shows the main window."""

from __future__ import annotations

import os
import sys
import threading
import traceback
import webbrowser

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QMessageBox

from .. import paths
from .main_window import MainWindow

DOCS_URL = "https://pypi.org/project/turbossh/"
_FLAG_DIR = paths.state_dir()
_CRASH_LOG_LIMIT = 1024 * 1024       # crash.log is trimmed to its tail past this
_window = None          # set after creation, used by the exception hook


def _crash_log(text: str):
    try:
        os.makedirs(_FLAG_DIR, exist_ok=True)
        path = os.path.join(_FLAG_DIR, "crash.log")
        try:
            if os.path.getsize(path) > _CRASH_LOG_LIMIT:
                with open(path, "rb") as fh:            # keep the recent half
                    fh.seek(-_CRASH_LOG_LIMIT // 2, os.SEEK_END)
                    tail = fh.read()
                with open(path, "wb") as fh:
                    fh.write(tail)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


class _ErrorRelay(QObject):
    """Brings an uncaught error to the GUI thread and shows ONE non-modal box.

    The hook may fire on any thread (a worker, a paramiko thread), and it may
    fire 30 times a second (an error inside a timer tick or paintEvent). A modal
    box per error — opened from whatever thread raised — stacked dialogs without
    end; this reuses a single box and counts.
    """
    error = pyqtSignal(str, str)         # one-line summary, full traceback

    def __init__(self):
        super().__init__()
        self._box = None
        self._count = 0
        self.error.connect(self._show)   # queued when emitted from another thread

    def _show(self, summary, details):
        if _window is not None:
            try:
                _window.log_panel.append(f"[ERROR] Unexpected error:\n{details.rstrip()}")
            except Exception:
                pass
        try:
            if self._box is None:
                self._count = 0
                box = QMessageBox(_window)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle("turbossh — error")
                box.setStandardButtons(QMessageBox.Ok)
                box.setWindowModality(Qt.NonModal)
                box.finished.connect(self._dismissed)
                self._box = box
            self._count += 1
            more = (f"\n\n({self._count} errors so far — see the log.)"
                    if self._count > 1 else "")
            self._box.setText(f"{summary}\n\nThe app stayed open; details are in "
                              f"the log.{more}")
            self._box.setDetailedText(details)
            self._present(self._box)
        except Exception:
            pass

    def _present(self, box):
        box.show()

    def _dismissed(self, *_):
        box, self._box = self._box, None
        if box is not None:
            box.deleteLater()


_relay = None


def _install_excepthook():
    """Uncaught errors -> crash.log + the log panel + one non-fatal popup,
    instead of crashing the app. Covers worker threads too (their errors used to
    go to a stderr the windowed app doesn't have)."""
    global _relay
    _relay = _ErrorRelay()

    def report(exc_type, exc, tb):
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        _crash_log(details)
        try:
            _relay.error.emit(f"{exc_type.__name__}: {exc}", details)
        except Exception:
            pass

    sys.excepthook = report
    if hasattr(threading, "excepthook"):          # Python 3.8+
        threading.excepthook = lambda args: report(
            args.exc_type, args.exc_value, args.exc_traceback)


def _create_shortcuts_once():
    """Create the Desktop + Start-Menu shortcuts ONCE (the setting is "on first
    run"). They are not re-created on later launches: a shortcut the user deleted
    stays deleted, and launching never shells out to PowerShell again."""
    try:
        from . import settings as _s
        if not _s.get("make_shortcut_first_run"):
            return                       # user opted out of shortcut creation
        flag = os.path.join(_FLAG_DIR, "shortcuts-fast-v1")
        if os.path.exists(flag):
            return
        from ..cli import create_desktop_shortcut
        create_desktop_shortcut()
        os.makedirs(_FLAG_DIR, exist_ok=True)
        with open(flag, "w") as fh:
            fh.write("1")
    except Exception as exc:
        _crash_log(f"shortcut creation failed: {exc}")


def _migrate_settings_once():
    """One-time: bump the OLD default terminal font (Consolas) to the crisper
    Cascadia Mono. Guarded by a flag so a later explicit font choice sticks."""
    try:
        from . import settings as s
        flag = os.path.join(_FLAG_DIR, "font-cascadia-v1")
        if os.path.exists(flag):
            return
        cfg = s.load()
        if (cfg.get("term_font") or "") == "Consolas":
            cfg["term_font"] = "Cascadia Mono"
            if int(cfg.get("term_font_size") or 10) < 11:
                cfg["term_font_size"] = 11
            s.save(cfg)
        os.makedirs(_FLAG_DIR, exist_ok=True)
        with open(flag, "w") as fh:
            fh.write("1")
    except Exception:
        pass


def _first_run_tasks():
    """Shortcuts (first run only) and the docs page the first time the app runs.
    Runs on a background thread: creating shortcuts starts PowerShell, which
    takes seconds on a cold machine and must not freeze the freshly shown window."""
    _create_shortcuts_once()
    try:
        from . import settings as _s
        os.makedirs(_FLAG_DIR, exist_ok=True)
        flag = os.path.join(_FLAG_DIR, "first-run-done")
        if os.path.exists(flag):
            return
        if _s.get("open_docs_first_run"):
            webbrowser.open(DOCS_URL)
        with open(flag, "w") as fh:
            fh.write("1")
    except Exception:
        pass


def main():
    # When launched from a shortcut (or elevated) the working directory is often
    # System32, so cwd-relative saves (snapshots, "save output", recordings) would
    # try to write there and fail. Move to the user's Documents/home so everything
    # defaults to a writable, sensible place.
    try:
        home = os.path.expanduser("~")
        docs = os.path.join(home, "Documents")
        os.chdir(docs if os.path.isdir(docs) else home)
    except Exception:
        pass
    # Windows: set an explicit AppUserModelID BEFORE any window so the taskbar
    # shows our icon (instead of the generic python/pythonw icon).
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "TurboSSH.Terminal.1")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("TurboSSH")
    # apply the saved theme + its matching app/taskbar icon (dark = original
    # artwork, light = generated light-tile variant)
    from . import theme, settings as settings_mod
    _theme = settings_mod.get("theme") or "dark"
    app.setStyleSheet(theme.stylesheet(_theme))
    try:
        _ic = theme.app_icon(_theme)
        if _ic is not None and not _ic.isNull():
            app.setWindowIcon(_ic)
    except Exception:
        pass
    _install_excepthook()

    global _window
    _window = MainWindow()
    _window.show()

    # Run one-time housekeeping AFTER the window is on screen, so startup feels
    # instant: the settings migration here (cheap), the rest — which can shell
    # out to PowerShell — on a background thread.
    def _post_show():
        _migrate_settings_once()
        threading.Thread(target=_first_run_tasks, name="first-run",
                         daemon=True).start()
    QTimer.singleShot(0, _post_show)

    # Update check is on-demand via the ribbon "Check updates" button — not on
    # startup, so the app opens instantly.
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
