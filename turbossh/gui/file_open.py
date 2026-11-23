"""Open remote files from the Files tab in the apps this PC has for them (as
TurboADB opens device files).

A remote file is first copied into a folder of its own in the temp folder
(through the transfer queue, so a big file shows its progress and can be
cancelled), opened there, and watched: each time its app saves it, the new
content goes back to the remote file, which keeps its owner and permissions
(the upload policy, turbossh._transfer). When the remote file changed in the
meantime, the user is asked first.

Programs are never started from here. What Windows would run, install or apply
instead of showing (``.exe``, ``.msi``, ``.lnk``, ...) is refused, and scripts,
which a double click would run too (``.bat``, ``.ps1``, ``.sh``, ``.py``,
``.reg``, ...), open in TurboSSH's own editor, as does any type this PC opens
with a program that runs what it opens (Python, the script hosts, ``mshta``
...), and a file no app on this PC opens (``.conf`` on most PCs).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from typing import List, Optional, Tuple

from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices

PROGRAM, SCRIPT, DOCUMENT = "program", "script", "document"
REFUSE, EDIT, OPEN = "refuse", "edit", "open"
# a RemoteCopy's stamp when the remote file could not be looked at after a
# save: the next save asks before it replaces the file
UNCHECKED = "unchecked"
_PREFIX = "turbossh-open-"        # the folders made here; only these are tidied

# What Windows would run, install, load or apply rather than show, and the
# binaries a Linux or Android target runs: never started from the Files tab.
PROGRAM_EXTENSIONS = frozenset("""
.exe .com .scr .pif .cpl .msc .msi .msp .mst .msu .msix .msixbundle .appx .appxbundle
.appinstaller .application .appref-ms .xbap .gadget .jar .jnlp .hta .chm .hlp .lnk .url
.website .rdp .scf .library-ms .settingcontent-ms .search-ms .searchconnector-ms .diagcab
.diagpkg .theme .themepack .deskthemepack .ppkg .wsb .psc1 .dll .sys .ocx .drv .efi
.xll .xla .xlam .ppa .ppam .vsto .pyc .pyo .pyz .pyzw
.apk .apks .apkm .xapk .aab .ipa .so .dex .odex .vdex .oat .art .ko .elf
""".split())
# Text that a double click would run: opened in TurboSSH's editor instead.
SCRIPT_EXTENSIONS = frozenset("""
.bat .cmd .ps1 .psm1 .psd1 .ps1xml .vbs .vbe .js .jse .wsf .wsh .wsc .sct .reg .inf
.py .pyw .sh .bash .zsh .ksh .csh .fish .pl .rb .php .lua .tcl .ahk .au3 .command
.desktop .applescript
""".split())
# Programs that run the file they open: a type opened by one of these is
# treated as a script, whatever its extension.
_LAUNCHERS = frozenset("""
py.exe pyw.exe python.exe pythonw.exe wscript.exe cscript.exe mshta.exe powershell.exe
pwsh.exe cmd.exe java.exe javaw.exe javaws.exe rundll32.exe regedit.exe regsvr32.exe
msiexec.exe wusa.exe bash.exe sh.exe wsl.exe node.exe perl.exe ruby.exe php.exe hh.exe
control.exe mmc.exe
""".split())

# AssocQueryStringW: ASSOCF_INIT_IGNOREUNKNOWN | ASSOCF_NOTRUNCATE, and what to ask
_ASSOC_FLAGS = 0x400 | 0x20
_ASSOC_EXECUTABLE, _ASSOC_APP_NAME = 2, 4


def kind_of(name: str) -> str:
    """:data:`PROGRAM`, :data:`SCRIPT` or :data:`DOCUMENT` for the file *name*."""
    ext = os.path.splitext(name)[1].lower()
    if ext in PROGRAM_EXTENSIONS:
        return PROGRAM
    if ext in SCRIPT_EXTENSIONS:
        return SCRIPT
    return DOCUMENT


def app_for(name: str) -> Optional[str]:
    """The app this PC opens the file *name* with ("Photos", "Notepad"): ""
    when there is one whose name is not known, None when there is none."""
    ext = os.path.splitext(name)[1].lower()
    if not ext:
        return None
    if sys.platform == "win32":
        answer = _windows_assoc(ext, _ASSOC_APP_NAME)
        if answer is None:
            return None
        return "" if answer.startswith("@") else answer  # "@": an unresolved resource
    import mimetypes
    return "" if mimetypes.guess_type("file" + ext)[0] else None


def runs_what_it_opens(name: str) -> str:
    """The program this PC would open *name* with when that program runs the
    file (``py.exe``; ``"itself"`` for a type that is its own program), else
    ``""``. Windows only: elsewhere the lists above decide."""
    ext = os.path.splitext(name)[1].lower()
    if not ext or sys.platform != "win32":
        return ""
    exe = (_windows_assoc(ext, _ASSOC_EXECUTABLE) or "").strip().strip('"')
    if not exe:
        return ""
    if exe in ("%1", "%L"):
        return "itself"
    base = os.path.basename(exe).lower()
    return base if base in _LAUNCHERS else ""


def how_to_open(name: str) -> Tuple[str, str]:
    """``(how, why)`` for opening the remote file *name* on this PC: REFUSE a
    program, EDIT (in TurboSSH's editor) a script or a type no app opens or
    one opened by a program that runs it, else OPEN in its app."""
    kind = kind_of(name)
    if kind == PROGRAM:
        return REFUSE, (f"{name} is a program: TurboSSH does not start programs "
                        f"from a remote host. Download it to look at it.")
    if kind == SCRIPT:
        return EDIT, f"{name} is a script: it opens in TurboSSH's editor, not run"
    runner = runs_what_it_opens(name)
    if runner:
        return EDIT, (f"this PC opens {os.path.splitext(name)[1]} files with "
                      f"{runner}, which runs them: it opens in TurboSSH's editor")
    if app_for(name) is None:
        return EDIT, "no app on this PC opens it: it opens in TurboSSH's editor"
    return OPEN, ""


_ASSOC_QUERY = []  # shlwapi's AssocQueryStringW, set up on first use


def _assoc_query():
    if not _ASSOC_QUERY:
        import ctypes
        from ctypes import wintypes

        query = ctypes.WinDLL("shlwapi").AssocQueryStringW
        query.argtypes = [ctypes.c_uint, ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR,
                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        query.restype = ctypes.c_long
        _ASSOC_QUERY.append(query)
    return _ASSOC_QUERY[0]


def _windows_assoc(ext: str, what: int) -> Optional[str]:
    """What Windows says about opening *ext* files (*what*: the app's name or
    its program), None when no app opens them, "" when it can't be asked."""
    try:
        import ctypes
        from ctypes import wintypes

        query = _assoc_query()
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if query(_ASSOC_FLAGS, what, ext, None, buf, ctypes.byref(size)) != 0:
            return None
    except Exception:
        return ""
    return buf.value.strip()


def start(path: str) -> bool:
    """Open the local file *path* in its app; False if that could not be done."""
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))


# ---- local copies of remote files ---------------------------------------------
def copies_root() -> str:
    """The folder that holds the copies of opened remote files: in the temp
    folder, or where ``TURBOSSH_OPENED_DIR`` says (the test suite's own)."""
    return (os.environ.get("TURBOSSH_OPENED_DIR")
            or os.path.join(tempfile.gettempdir(), "TurboSSH", "opened"))


def new_copy_folder() -> str:
    """A new, empty folder for the copy of one opened remote file."""
    root = copies_root()
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(dir=root, prefix=_PREFIX)


def forget_old_copies(max_age_days: float = 7.0) -> None:
    """Remove folders of opened remote files older than *max_age_days*: their
    apps were closed long ago (a copy still open elsewhere just stays)."""
    root = copies_root()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    oldest = time.time() - max_age_days * 86400
    for entry in entries:
        if not entry.name.startswith(_PREFIX):
            continue                     # not one of ours
        try:
            if entry.is_dir(follow_symlinks=False) and entry.stat().st_mtime < oldest:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            pass


def stat_key(path: str) -> Optional[Tuple[int, int]]:
    """``(size, modified ns)`` of the file *path*, None while it isn't there."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


class RemoteCopy:
    """A remote file open in an app on this PC: where its copy is, where it came
    from, and what the remote had when the copy was last in step."""

    def __init__(self, name: str, local: str, remote: str, stamp=None):
        self.name = name        # as listed on the remote
        self.local = local      # the copy on this PC
        self.remote = remote    # the remote file
        self.stamp = stamp      # its (size, mtime) on the remote; None: not known
        #                         when opened; UNCHECKED: not looked at after a save
        self.synced = None      # stat_key() of the copy when the remote had the same
        self.seen = None        # stat_key() of the copy at the last look
        self.settled = True     # the copy was looked at since it last changed
        self.saving = False     # being sent to the remote (or asked about) right now
        self.sending = None     # stat_key() of the copy being sent
        self.again = False      # saved again meanwhile: look once more after that


class CopyWatcher(QObject):
    """Tells when an app saved a copy of a remote file (:attr:`saved`).

    It looks at each copy's size and time every :attr:`INTERVAL_MS`, never at
    its content, so a copy of a big file costs nothing: a copy whose size or
    time changed, then stayed put between two looks, and is not what the
    remote has, was saved. An app that saves by writing a new file and renaming
    it over the old one is seen as well."""

    saved = pyqtSignal(object)  # the RemoteCopy
    INTERVAL_MS = 700

    def __init__(self, parent=None):
        super().__init__(parent)
        self._copies: List[RemoteCopy] = []
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self.check)

    def watch(self, copy: RemoteCopy) -> None:
        copy.seen = stat_key(copy.local)
        if copy.synced is None:
            copy.synced = copy.seen
        copy.settled = True
        self._copies.append(copy)
        if not self._timer.isActive():
            self._timer.start()

    def copies(self) -> List[RemoteCopy]:
        return list(self._copies)

    def recheck(self, copy: RemoteCopy) -> None:
        """Look at *copy* again at the next check (it was saved while being sent)."""
        copy.settled = False

    def check(self) -> None:
        for copy in list(self._copies):
            key = stat_key(copy.local)
            if key is None:
                continue  # gone for a moment: an app replacing it
            if key != copy.seen:
                copy.seen, copy.settled = key, False  # still changing: look again
                continue
            if copy.settled:
                continue
            copy.settled = True
            if key != copy.synced:
                self.saved.emit(copy)

    def stop(self) -> None:
        self._timer.stop()
        self._copies.clear()
