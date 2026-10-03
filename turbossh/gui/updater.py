"""On-demand update check for the GUI (the ribbon "Check updates" button).

We ask PyPI for the latest ``turbossh`` version in a background thread, so the
UI never blocks and startup stays instant. If a newer one exists we offer to
update: the GUI closes, a detached helper waits for it to exit, runs
``pip install turbossh==<version>`` with the interpreter that owns this install,
checks the result, and relaunches the app.

The logic (version comparison, finding the owning interpreter, the helper script)
lives in :mod:`turbossh.update`, which has no Qt in it; this module is the dialog.
"""

from __future__ import annotations

import os
import sys

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QMessageBox

from .. import __version__, update


class _CheckThread(QThread):
    """Fetches the latest version from PyPI off the UI thread."""
    result = pyqtSignal(str)        # latest version string ("" on failure)

    def run(self):
        self.result.emit(update.latest_version() or "")


def _relaunch_target() -> str:
    """Path to relaunch after the update (the bundled exe if we're it)."""
    try:
        from ..cli import _gui_exe_path
        exe = _gui_exe_path()
        if os.path.exists(exe):
            return exe
    except Exception:
        pass
    return sys.executable


def check_now(window):
    """Manually check for updates (ribbon button). Always reports the result —
    a newer version offers a one-click update, otherwise says you're current."""
    th = _CheckThread(window)
    th.result.connect(lambda latest: _on_result(window, latest, manual=True))
    th.finished.connect(lambda: window.statusBar().clearMessage()
                        if window.statusBar() else None)
    window._update_thread = th
    try:
        window.statusBar().showMessage("Checking for updates…", 4000)
    except Exception:
        pass
    th.start()


def _on_result(window, latest: str, manual: bool = False):
    # "Check for updates" ONLY reports the PyPI version status. It no longer also
    # offers to (re)install OpenSSH on your RDP machines — that unexpected popup
    # is gone; use the "Set up SSH server" button for that, on demand.
    if not latest:
        if manual:
            QMessageBox.warning(
                window, "Check for updates",
                "Couldn't reach PyPI to check for updates.\n\n"
                "Check your internet connection and try again.")
        return
    if not update.is_newer(latest):
        if manual:
            QMessageBox.information(
                window, "Check for updates",
                f"You're on the latest version of TurboSSH ({__version__}).")
        return

    box = QMessageBox(window)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("TurboSSH update available")
    box.setText(f"A new version of TurboSSH is available.\n\n"
                f"    Installed:  {__version__}\n    Latest:     {latest}")
    if not getattr(sys, "frozen", False):
        box.setInformativeText("You're running from source — update with "
                               "'pip install -U turbossh' (or git pull).")
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()
        return
    python = update.owning_python(sys.executable)
    if python is None:
        # A standalone exe (downloaded, not installed by pip): there is no
        # install for pip to upgrade. Running some `python` from PATH would
        # upgrade a different copy and then relaunch this same old exe.
        box.setInformativeText(
            "This copy isn't managed by pip, so it can't update itself. "
            f"Download the new version from:\n{update.RELEASES_URL}\n\n"
            "(or install with 'pip install -U turbossh').")
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()
        return
    box.setInformativeText("Update now? TurboSSH will close, update, and reopen.")
    update_btn = box.addButton("Update now", QMessageBox.AcceptRole)
    box.addButton("Later", QMessageBox.RejectRole)
    box.setDefaultButton(update_btn)
    box.exec_()
    if box.clickedButton() is not update_btn:
        return
    # Close through the window's own closeEvent (it asks about open sessions and
    # releases remote ports) rather than quitting the event loop under it. Only
    # if the window really closed is the helper started — it waits for this
    # process to exit before pip touches the locked exe.
    if not window.close():
        return
    if not update.spawn_update(latest, python, _relaunch_target()):
        QMessageBox.warning(None, "TurboSSH update",
                            "Could not start the updater. Run "
                            "'pip install -U turbossh' manually.")
