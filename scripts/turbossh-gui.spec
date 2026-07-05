# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the one-FOLDER TurboSSH GUI build.

    python scripts/build_exe.py            # uses this spec (recommended)
    python scripts/build_exe.py --onefile  # single .exe (no spec; slower startup)

Run from the repo root so the relative paths below resolve. Output:
    dist/turbossh-gui/turbossh-gui.exe
"""

import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.getcwd())
PKG = os.path.join(ROOT, "turbossh")
ICON = os.path.join(PKG, "assets", "icon.ico")

datas = [
    (os.path.join(PKG, "assets", "icon.ico"), "turbossh/assets"),
    (os.path.join(PKG, "assets", "icon.png"), "turbossh/assets"),
    (os.path.join(PKG, "setup_openssh_server.ps1"), "turbossh"),
    (os.path.join(PKG, "README.md"), "turbossh"),
    (os.path.join(PKG, "openssh"), "turbossh/openssh"),
]

# WinRM (offline remote install) pulls in requests_ntlm + spnego lazily; collect
# them (and the rest) so the frozen app has everything it imports at runtime.
hiddenimports = ["winrm", "keyring.backends", "requests_ntlm", "spnego"]
for mod in ("paramiko", "serial", "pyte", "turbossh", "winrm",
            "requests_ntlm", "spnego"):
    hiddenimports += collect_submodules(mod)

block_cipher = None

a = Analysis(
    # NB: the package entry script (absolute `import turbossh…`), NOT a module run
    # as __main__ — the latter breaks relative imports.
    [os.path.join(ROOT, "scripts", "gui_entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="turbossh-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="turbossh-gui",
)
