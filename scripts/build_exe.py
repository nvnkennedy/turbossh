#!/usr/bin/env python
"""
Build the turbossh PyQt5 app into a standalone Windows executable with the
bundled automotive-SSH icon, using PyInstaller.

    pip install "turbossh[gui]" pyinstaller
    python scripts/build_exe.py            # one-folder build (recommended)
    python scripts/build_exe.py --onefile  # single .exe (slower startup)

Output:
    one-folder: dist/turbossh-gui/turbossh-gui.exe
    onefile:    dist/turbossh-gui.exe

Run from the repo root so the spec finds turbossh/.
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "turbossh" / "assets" / "icon.ico"
SPEC = ROOT / "scripts" / "turbossh-gui.spec"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    onefile = "--onefile" in argv

    try:
        import PyInstaller  # noqa
    except ImportError:
        sys.exit("PyInstaller is required: pip install pyinstaller")

    if onefile:
        # Build directly (no spec) for a single-file exe.
        datas = [
            (ROOT / "turbossh" / "assets" / "icon.ico", "turbossh/assets"),
            (ROOT / "turbossh" / "assets" / "icon.png", "turbossh/assets"),
            (ROOT / "turbossh" / "setup_openssh_server.ps1", "turbossh"),
            (ROOT / "turbossh" / "README.md", "turbossh"),
            (ROOT / "turbossh" / "openssh", "turbossh/openssh"),
        ]
        sep = ";" if os.name == "nt" else ":"
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--onefile", "--windowed", "--name", "turbossh-gui",
               "--icon", str(ICON)]
        for src, dest in datas:
            cmd += ["--add-data", f"{src}{sep}{dest}"]
        for mod in ("winrm", "keyring.backends", "requests_ntlm", "spnego"):
            cmd += ["--hidden-import", mod]
        cmd += ["--collect-submodules", "paramiko",
                "--collect-submodules", "serial",
                "--collect-submodules", "pyte",
                "--collect-submodules", "turbossh",
                # WinRM (offline remote install) uses requests_ntlm + spnego,
                # imported lazily — collect them so the frozen exe has them.
                "--collect-submodules", "winrm",
                "--collect-submodules", "requests_ntlm",
                "--collect-submodules", "spnego",
                # NB: use the package entry script (absolute `import turbossh…`),
                # NOT turbossh/gui_app.py — running the latter as __main__ breaks
                # its relative imports ("attempted relative import with no known
                # parent package").
                str(ROOT / "scripts" / "gui_entry.py")]
    else:
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)]

    print("Running:", " ".join(str(c) for c in cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc == 0:
        out = ("dist/turbossh-gui.exe" if onefile
               else "dist/turbossh-gui/turbossh-gui.exe")
        print(f"\nDone. Executable at: {out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
