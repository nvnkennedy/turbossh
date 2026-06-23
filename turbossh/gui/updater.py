"""On-demand update check for the GUI (the ribbon "Check updates" button).

We ask PyPI for the latest ``turbossh`` version in a background thread, so the
UI never blocks and startup stays instant. If a newer one exists we offer to
update: the GUI closes, a detached helper runs ``pip install -U turbossh``, pops
a native "Updated to X" message, and relaunches the app — overwriting the
running exe is impossible while it's open, so the helper waits for us to exit
first.
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import subprocess
import urllib.request

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QMessageBox

from .. import __version__

_PYPI_JSON = "https://pypi.org/pypi/turbossh/json"


def _ver_tuple(v: str):
    parts = []
    for chunk in v.split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _ver_tuple(latest) > _ver_tuple(current)
    except Exception:
        return bool(latest) and latest != current


class _CheckThread(QThread):
    """Fetches the latest version from PyPI off the UI thread."""
    result = pyqtSignal(str)        # latest version string ("" on failure)

    def run(self):
        try:
            req = urllib.request.Request(
                _PYPI_JSON, headers={"User-Agent": f"turbossh/{__version__}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            self.result.emit(str(data["info"]["version"]))
        except Exception:
            self.result.emit("")


def _python_for_pip() -> str:
    """Best guess at the interpreter that owns this install. When frozen, the
    exe lives at <python>/Lib/site-packages/turbossh/bin/turbossh-gui.exe, so
    the interpreter is four directories up. Falls back to PATH."""
    try:
        bindir = os.path.dirname(sys.executable)
        root = os.path.abspath(os.path.join(bindir, "..", "..", "..", ".."))
        for exe in ("python.exe", "pythonw.exe"):
            cand = os.path.join(root, exe)
            if os.path.exists(cand):
                return cand
    except Exception:
        pass
    return shutil.which("python") or shutil.which("py") or "python"


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


def _spawn_updater(latest: str) -> bool:
    """Launch a detached PowerShell helper that waits for this process to exit,
    upgrades the package, shows a result message, and relaunches the GUI."""
    py = _python_for_pip()
    exe = _relaunch_target()
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName PresentationFramework
Start-Sleep -Seconds 2
& "{py}" -m pip install --upgrade --no-input turbossh
$code = $LASTEXITCODE
if ($code -eq 0) {{
  [System.Windows.MessageBox]::Show("TurboSSH has been updated to {latest}.","TurboSSH update") | Out-Null
  Start-Process "{exe}"
}} else {{
  [System.Windows.MessageBox]::Show("Couldn't update automatically. Open a terminal and run:`n`n    pip install -U turbossh","TurboSSH update") | Out-Null
}}
"""
    try:
        DETACHED = 0x00000008 | 0x00000200      # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=DETACHED, close_fds=True)
        return True
    except Exception:
        return False


def check_now(window):
    """Manually check for updates (ribbon button). Always reports the result —
    a newer version offers a one-click update, otherwise says you're current."""
    btn = getattr(window, "_update_action_btn", None)
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


def _offer_openssh(window):
    """After a manual check, also offer to (re)install OpenSSH on the user's
    machines (the part the user asked for: 'check OpenSSH too')."""
    try:
        fn = getattr(window, "offer_openssh_install", None)
        if fn:
            fn()
    except Exception:
        pass


def _on_result(window, latest: str, manual: bool = False):
    if not latest:
        if manual:
            QMessageBox.warning(
                window, "Check for updates",
                "Couldn't reach PyPI to check for updates.\n\n"
                "Check your internet connection and try again.")
            _offer_openssh(window)
        return
    if not _is_newer(latest, __version__):
        if manual:
            QMessageBox.information(
                window, "Check for updates",
                f"You're on the latest version of TurboSSH ({__version__}).")
            _offer_openssh(window)
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
        if manual:
            _offer_openssh(window)
        return
    box.setInformativeText("Update now? TurboSSH will close, update, and reopen.")
    update_btn = box.addButton("Update now", QMessageBox.AcceptRole)
    box.addButton("Later", QMessageBox.RejectRole)
    box.setDefaultButton(update_btn)
    box.exec_()
    if box.clickedButton() is update_btn:
        if _spawn_updater(latest):
            from PyQt5.QtWidgets import QApplication
            QApplication.quit()
        else:
            QMessageBox.warning(window, "TurboSSH update",
                                "Could not start the updater. Run "
                                "'pip install -U turbossh' manually.")
    elif manual:
        # chose "Later" — still offer the OpenSSH install
        _offer_openssh(window)
