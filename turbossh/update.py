"""Self-update logic with no GUI dependency.

The GUI's "Check updates" button drives this, but nothing here imports Qt, so it
can be tested headless and reused from the command line.

Updating a running, pip-installed Windows exe has three traps this module is
built around:

* the exe is locked while the app runs, and pip removes the old version before
  installing the new one — so the helper must wait for the app to really exit
  (by PID), not sleep and hope;
* "the interpreter that owns this install" has to be found from where the exe
  actually lives; guessing a ``python`` from PATH can upgrade a *different*
  Python and then report success;
* a zero exit code from pip is not proof — the installed version is read back.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from typing import Optional

from . import __version__

PYPI_JSON = "https://pypi.org/pypi/turbossh/json"
RELEASES_URL = "https://github.com/nvnkennedy/turbossh/releases/latest"


def version_tuple(version: str) -> tuple:
    """Numeric parts of a version; only the LEADING digits of each chunk count,
    so ``1.3.0rc1`` reads as (1, 3, 0) rather than something newer than 1.3.0."""
    parts = []
    for chunk in str(version).split("."):
        match = re.match(r"\d+", chunk)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def is_newer(latest: Optional[str], current: Optional[str] = None) -> bool:
    """True if *latest* is a later version than *current* (default: this one)."""
    current = __version__ if current is None else current
    if not latest:
        return False
    a, b = version_tuple(latest), version_tuple(current)
    width = max(len(a), len(b))            # so 1.6 == 1.6.0
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def latest_version(timeout: float = 5.0) -> Optional[str]:
    """The newest release on PyPI, or None if it could not be fetched."""
    try:
        request = urllib.request.Request(
            PYPI_JSON, headers={"User-Agent": f"turbossh/{__version__}"})
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return str(json.load(reply)["info"]["version"])
    except Exception:
        return None


def owning_python(exe_path: Optional[str] = None) -> Optional[str]:
    """The interpreter whose ``site-packages`` holds the bundled GUI exe.

    The exe ships at ``<site-packages>/turbossh/bin/turbossh-gui.exe``. Returns
    None when *exe_path* is not there (a standalone download pip knows nothing
    about) or when no interpreter can be found next to that ``site-packages`` —
    in both cases there is nothing pip can safely upgrade.
    """
    exe_path = os.path.abspath(exe_path or sys.executable)
    package = os.path.dirname(os.path.dirname(exe_path))       # …/turbossh
    site_packages = os.path.dirname(package)
    if (os.path.basename(package).lower() != "turbossh"
            or os.path.basename(site_packages).lower() != "site-packages"):
        return None
    prefix = os.path.dirname(os.path.dirname(site_packages))   # above Lib/
    for relative in ("python.exe", os.path.join("Scripts", "python.exe"),
                     os.path.join("bin", "python3"), os.path.join("bin", "python")):
        candidate = os.path.join(prefix, relative)
        if os.path.isfile(candidate):
            return candidate
    return None


def _ps_quote(value) -> str:
    """A PowerShell single-quoted literal (an embedded quote is doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def build_update_script(python: str, version: str, relaunch: str, wait_pid: int) -> str:
    """The PowerShell helper that upgrades to exactly *version* and relaunches."""
    message = "[System.Windows.MessageBox]::Show"
    manual = "Open a terminal and run:`n`n    pip install -U turbossh"
    return f"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName PresentationFramework
$py = {_ps_quote(python)}
# The running exe is locked until the app exits, and pip removes the old version
# before installing the new one - so wait for the app itself, not a fixed delay.
try {{ Wait-Process -Id {int(wait_pid)} -Timeout 120 }} catch {{}}
if (Get-Process -Id {int(wait_pid)}) {{
  {message}("TurboSSH is still running, so it was not updated. Close it, then:`n`n    pip install -U turbossh","TurboSSH update") | Out-Null
  exit 1
}}
& $py -m pip install --upgrade --no-input {_ps_quote("turbossh==" + version)}
$code = $LASTEXITCODE
# pip's exit code is not proof: read the installed version back
$installed = (& $py -c "import turbossh; print(turbossh.__version__)" | Out-String).Trim()
if ($code -eq 0 -and $installed -eq {_ps_quote(version)}) {{
  {message}("TurboSSH has been updated to {version}.","TurboSSH update") | Out-Null
  Start-Process -FilePath {_ps_quote(relaunch)}
}} else {{
  {message}("Couldn't update automatically (installed version: $installed). {manual}","TurboSSH update") | Out-Null
}}
"""


def spawn_update(version: str, python: str, relaunch: str,
                 wait_pid: Optional[int] = None) -> bool:
    """Start the detached helper. The caller must then exit the app."""
    script = build_update_script(python, version, relaunch,
                                 os.getpid() if wait_pid is None else wait_pid)
    try:
        detached = 0x00000008 | 0x00000200     # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=detached if os.name == "nt" else 0, close_fds=True)
        return True
    except Exception:
        return False
