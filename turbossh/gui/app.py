"""GUI entry point: builds the QApplication, installs a crash-safe exception
hook (popup + log, non-fatal), creates a desktop shortcut on first run, and
shows the main window."""

from __future__ import annotations

import os
import sys
import traceback
import webbrowser

from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QMessageBox

from .main_window import MainWindow, ICON_PATH

DOCS_URL = "https://pypi.org/project/turbossh/"
_FLAG_DIR = os.path.join(os.path.expanduser("~"), ".turbossh")
_window = None          # set after creation, used by the exception hook


def _crash_log(text: str):
    try:
        os.makedirs(_FLAG_DIR, exist_ok=True)
        with open(os.path.join(_FLAG_DIR, "crash.log"), "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def _install_excepthook():
    """Uncaught GUI-thread errors -> log to the panel + a non-fatal popup,
    instead of crashing the app."""
    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        _crash_log(msg)
        if _window is not None:
            try:
                _window.log_panel.append(f"[ERROR] Unexpected error:\n{msg.rstrip()}")
            except Exception:
                pass
        try:
            QMessageBox.warning(_window, "turbossh — error",
                                f"{exc_type.__name__}: {exc}\n\n"
                                "The app stayed open; details are in the log.")
        except Exception:
            pass
    sys.excepthook = hook


def _ensure_shortcuts():
    """Make sure the Desktop + Start-Menu shortcuts (with the proper icon) exist.
    Runs every launch but only shells out when one is missing — so a deleted or
    never-created shortcut gets restored, and a fresh install gets both."""
    try:
        from ..cli import ensure_shortcuts
        ensure_shortcuts()
    except Exception as exc:
        _crash_log(f"shortcut creation failed: {exc}")


def _first_run_tasks():
    """Open docs the first time the app runs; (re)create shortcuts every time."""
    _ensure_shortcuts()
    try:
        os.makedirs(_FLAG_DIR, exist_ok=True)
        flag = os.path.join(_FLAG_DIR, "first-run-done")
        if os.path.exists(flag):
            return
        webbrowser.open(DOCS_URL)
        with open(flag, "w") as fh:
            fh.write("1")
    except Exception:
        pass


def main():
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
    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))
    # apply the saved theme at the application level so every window/dialog is styled
    from . import theme, settings as settings_mod
    app.setStyleSheet(theme.stylesheet(settings_mod.get("theme")))
    _install_excepthook()
    _first_run_tasks()

    global _window
    _window = MainWindow()
    _window.show()
    # Update check is on-demand via the ribbon "Check updates" button — not on
    # startup, so the app opens instantly.
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
