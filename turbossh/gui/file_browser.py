"""Dual-pane file browser: this PC on the left, the remote host on the right, and
the transfer history underneath — laid out as TurboADB's Files tab is.

Quick links to folders on both sides run along the top; each pane has its
heading (the drive, the path, Up, Refresh), a listing with Name, Size, Date
Modified, Permissions and Owner (folders say ``<DIR>``, ".." leads up), and
its file operations underneath. Push and Pull between the panes move the
selection one way or the other; so does dragging between the panes (or in
from Explorer), and so do the context menus. The remote pane is
:class:`SftpBrowser`, which owns the transfer queue; this module adds the
local pane and ties the two together.

Nothing here blocks the GUI thread: the local listing is read on a worker (a
folder on a share that is gone must not freeze the window), as are a saved
start folder and deletes; everything remote runs on the remote pane's worker
threads.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import string
import struct
import time

from collections import OrderedDict

from PyQt5.QtCore import (QDir, QDirIterator, QFile, QFileInfo, QFileSystemWatcher,
                          QItemSelectionModel, QMimeData, QPoint, QStandardPaths, Qt,
                          QTimer, QUrl, pyqtSignal)
from PyQt5.QtGui import QDesktopServices, QGuiApplication, QIcon, QKeySequence
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                             QFileIconProvider, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QMenu, QMessageBox, QPushButton, QShortcut,
                             QSplitter, QStyle, QVBoxLayout, QWidget)

from . import glyphs
from .qtutil import close_jobs, run_job
from .sftp_browser import (_FILL_CHUNK, PARENT_KEYS, REMOTE_MIME, REMOTE_SOURCE_MIME,
                           ListTree, SftpBrowser, _Entry, _row_specs, _sorted_rows,
                           _Spec, listing_summary, make_row, parent_icon,
                           reorder_rows, select_rows)
from .transfer_log import TransferLog
from .transfer_panel import TransferPanel
from .widgets import flat_button, ops_row, pane_title

_HAS_TRASH = hasattr(QFile, "moveToTrash")       # Qt 5.15+
_WINDOWS = os.name == "nt"
_SETTLE_MS = 300         # a folder that changes is listed again this long after
                         # the last change (a download writes and renames)...
_SETTLE_MAX = 2.0        # ...or this many seconds after the first, if it keeps
                         # changing (a log being written)


@contextlib.contextmanager
def _com():
    """The recycle bin is a shell (COM) operation, and COM has to be set up on
    the thread that runs it — a worker thread here. Harmless if Qt does it too."""
    ole32 = None
    if os.name == "nt":
        try:
            import ctypes
            ole32 = ctypes.windll.ole32
            if ole32.CoInitializeEx(None, 0x2) < 0:      # COINIT_APARTMENTTHREADED
                ole32 = None             # set up differently already: leave it be
        except Exception:
            ole32 = None
    try:
        yield
    finally:
        if ole32 is not None:
            ole32.CoUninitialize()


def _moved_to_trash(path) -> bool:
    """``QFile.moveToTrash(path)``. PyQt5's static form returns ``(ok, path in
    the recycle bin)`` — and a tuple is always true, ``(False, '')`` included,
    so every failed move counted as done. A plain bool is taken as it is."""
    result = QFile.moveToTrash(path)
    if isinstance(result, tuple):
        result = result[0] if result else False
    return bool(result)


def _trash(paths):
    """``(moved, left)``: *paths* sent to the recycle bin, and ``[(path, why)]``
    of those that could not be (worker thread)."""
    moved, left = 0, []
    with _com():
        for path in paths:
            try:
                if _moved_to_trash(path):
                    moved += 1
                    continue
            except Exception:
                pass
            left.append((path, "not on a drive with a recycle bin (a network share?), "
                               "or the recycle bin refused it"))
    return moved, left


def _remove_for_good(paths):
    """``(removed, errors)``: *paths* deleted permanently (worker thread)."""
    removed, errors = 0, []
    for path in paths:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except OSError as exc:
            errors.append(f"{os.path.basename(path)}: {exc}")
    return removed, errors


def _names(paths, limit=5) -> str:
    more = f" and {len(paths) - limit} more" if len(paths) > limit else ""
    return ", ".join(os.path.basename(p) for p in paths[:limit]) + more


# --------------------------------------------------------------------------- #
# Reading a local folder (worker thread)
# --------------------------------------------------------------------------- #
def drives() -> list:
    """This PC's drives, ``["C:\\\\", "D:\\\\", …]``, from one bitmask: no drive
    is touched, so an empty card reader or a dead network mapping cannot
    stall it. ``["/"]`` where there are no drive letters."""
    if not _WINDOWS:
        return ["/"]
    mask = 0
    try:
        import ctypes
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except Exception:
        mask = 0
    return [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase)
            if mask & (1 << i)] or ["C:\\"]


def drive_of(path) -> str:
    """The drive *path* is on ("C:\\\\"; "" for a share or the drive list)."""
    if not _WINDOWS:
        return "/" if path else ""
    drive = os.path.splitdrive(path or "")[0]
    return drive + "\\" if len(drive) == 2 else ""


# GetDriveType: removable, fixed, CD-ROM, RAM disk are this PC's own disks;
# remote (4) is a mapped share. Fixed and RAM disks always answer at once.
_DISKS = (2, 3, 5, 6)
_ALWAYS_THERE = (3, 6)
_REMOTE_DRIVE = 4
_MAX_SHORTCUT_ICONS = 64     # shortcuts of one folder that get the system's
                             # icon (each takes Qt ~5 ms to read on the worker)
# FILE_ATTRIBUTE_OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS: a placeholder
# (OneDrive "online only") that reading would download
_NOT_HERE = 0x1000 | 0x40000 | 0x400000


def drive_type(root) -> int:
    """Windows' GetDriveType of a drive root (0 where unknown). It reads the
    drive table; it does not wait on the drive."""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except Exception:
        return 0


# file systems on another machine (or behind a user-space driver, fuse.*)
_REMOTE_FS = frozenset(("nfs", "nfs4", "cifs", "smb3", "smbfs", "ncpfs", "9p", "afs",
                        "ceph", "glusterfs", "lustre", "gpfs", "davfs", "sshfs"))


def mount_type(path, mounts="/proc/self/mounts") -> str:
    """The type of the file system *path* (a real path) is on, from the mount
    table ("" where there is none to read)."""
    best, kind = "", ""
    try:
        with open(mounts, encoding="utf-8", errors="replace") as table:
            for line in table:
                fields = line.split()
                if len(fields) < 3:
                    continue
                point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)),
                               fields[1])        # "\040" is a space
                inside = path == point or path.startswith(point.rstrip("/") + "/")
                if inside and len(point) >= len(best):
                    best, kind = point, fields[2]
    except OSError:
        return ""
    return kind


def on_local_disk(path) -> bool:
    """Is *path* (a real path) on one of this PC's own disks — not a share
    (mapped to a letter, named by UNC, or mounted)? Asked on a worker."""
    if not _WINDOWS:
        kind = mount_type(path)
        return not (kind in _REMOTE_FS or kind.startswith("fuse."))
    root = drive_of(path)
    return bool(root) and drive_type(root) in _DISKS


def _natural_key(name) -> tuple:
    """Sorts "file2" before "file10", ignoring case, as Qt's file model did.
    Text and numbers alternate, so two keys compare part by part."""
    return tuple(int(part) if i % 2 else part.lower()
                 for i, part in enumerate(re.split(r"(\d+)", name or "")))


_OWNERS: dict = {}


def _owner_name(uid) -> str:
    """The user name of *uid* ("" on Windows, where a file's owner takes a
    security lookup per file)."""
    if uid is None or _WINDOWS:
        return ""
    name = _OWNERS.get(uid)
    if name is None:
        try:
            import pwd
            name = pwd.getpwuid(uid).pw_name
        except Exception:
            name = str(uid)
        _OWNERS[uid] = name
    return name


def _local_perms(mode) -> str:
    """The permission bits in octal, as TurboADB's PC pane shows them ("755";
    on Windows that says read-only, "555", or not, "777")."""
    try:
        return format(int(mode) & 0o777, "03o") if mode is not None else ""
    except (TypeError, ValueError):
        return ""


def _hidden(entry, lst) -> bool:
    """Hidden as Qt's file model hid it: the hidden attribute on Windows, a
    leading dot elsewhere."""
    if _WINDOWS:
        return bool(getattr(lst, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)
    return entry.name.startswith(".")


def scan_folder(path, attributes=None) -> list:
    """The entries of the local folder *path* (``_Entry`` rows; "" = the drives):
    its folders and files, hidden ones left out. A link shows what it points
    at. *attributes*: a dict to fill with each entry's Windows file attributes.
    Raises OSError if the folder cannot be read."""
    if not path:
        return [_Entry(d, True, False, None, None, None, stat.S_IFDIR) for d in drives()]
    entries = []
    # Only folders and files, as Qt's file model showed them: a FIFO or a
    # device read for an upload never ends, and a broken link is nothing.
    with os.scandir(path) as listing:
        for item in listing:
            try:
                lst = item.stat(follow_symlinks=False)
            except OSError:
                continue                 # gone meanwhile
            if _hidden(item, lst):
                continue
            if attributes is not None:
                attributes[item.name] = getattr(lst, "st_file_attributes", 0)
            is_link = stat.S_ISLNK(lst.st_mode)
            st = lst
            if is_link:
                try:
                    st = item.stat()
                except OSError:
                    st = lst             # dangling: the link itself
            ftype = stat.S_IFMT(st.st_mode)      # a junction is a folder
            if ftype not in (stat.S_IFDIR, stat.S_IFREG):
                continue
            is_dir = ftype == stat.S_IFDIR
            entries.append(_Entry(item.name, is_dir, is_link,
                                  None if is_dir else st.st_size, st.st_mtime,
                                  st.st_mode, ftype,
                                  _owner_name(getattr(st, "st_uid", None))))
    return entries


class _Listing:
    """A local folder as read by a worker (see :func:`_local_listing`)."""

    def __init__(self, path, entries, specs, order, infos, kinds, watch):
        self.path, self.entries, self.specs, self.order = path, entries, specs, order
        self.infos = infos       # name -> QFileInfo already read: for system icons
        self.kinds = kinds       # drive -> GetDriveType (the drive list)
        self.watch = watch       # watch the folder for changes


def _local_listing(path, order, native=False) -> _Listing:
    """Worker: read a local folder ("" = the drives) and prepare its rows. With
    *native* (system icons), a QFileInfo of each row on a disk of this PC is
    read here: the shell's icon asks for the file's type when it is painted,
    and a fresh QFileInfo would read the disk on the GUI thread then — for a
    network drive that went away, for the network's timeout. Rows on a share
    get the style's icons instead."""
    attributes = {}
    entries = scan_folder(path, attributes)
    kinds, shown = {}, []
    if path:
        specs = _row_specs(entries, order, _local_perms, _natural_key)
        # where it really is: a folder reached through a link may be a share
        local = _really_local(path)
        if local and native:
            shown = [e.name for e in entries
                     if not e.is_link or _really_local(os.path.join(path, e.name))]
    else:                                # the drives: in drive order, nothing else
        specs = [_Spec(1, (e.name.lower(), 0, 0, "", ""), e.name, True, False,
                       ("", "", "", "")) for e in entries]
        if _WINDOWS:
            kinds = {e.name: drive_type(e.name) for e in entries}
            shown = [name for name, kind in kinds.items() if kind in _ALWAYS_THERE]
        local = False
    infos = {}
    if native and shown and path:
        # read with the folder, in one scan: a QFileInfo made per file asked
        # the disk once per query (20,000 files took nine seconds)
        wanted = set(shown)
        folders = {e.name for e in entries if e.is_dir}
        # read-only or system folders: the shell takes their icon from their
        # desktop.ini; a cloud placeholder ("online only") would be downloaded
        custom = {e.name for e in entries if e.is_dir and attributes.get(e.name, 0)
                  & (stat.FILE_ATTRIBUTE_READONLY | stat.FILE_ATTRIBUTE_SYSTEM)}
        wanted -= {name for name, flags in attributes.items() if flags & _NOT_HERE}
        shortcuts = 0
        found = QDirIterator(path, QDir.AllEntries | QDir.NoDotAndDotDot
                             | QDir.Hidden | QDir.System)
        with _com():                     # Qt reads each shortcut through COM
            while found.hasNext():
                found.next()
                info = found.fileInfo()
                name = info.fileName()
                if name not in wanted:
                    continue
                full = os.path.join(path, name)
                if name.lower().endswith(".lnk") and name not in folders:
                    # each costs a few ms to read: past a handful, the
                    # style's icon (a folder of hundreds, like Recent)
                    shortcuts += 1
                    if shortcuts > _MAX_SHORTCUT_ICONS or not _shortcut_ready(full, info):
                        continue
                elif name in custom or name.lower().endswith(_ICON_FILES):
                    if _icon_on_a_share(full, name in folders):
                        continue
                infos[name] = info
    elif native:                         # the fixed drives
        for name in shown:
            info = QFileInfo(name)
            info.isFile(), info.isDir()  # read here, not when painted
            infos[name] = info
    return _Listing(path, entries, specs, order, infos, kinds, local)


def _really_local(path) -> bool:
    """:func:`on_local_disk` of where *path* really is (links followed). A
    share that drops while the link is followed: not local."""
    try:
        return on_local_disk(os.path.realpath(path))
    except (OSError, ValueError):
        return False


def _on_fixed_disk(path) -> bool:
    root = drive_of(path)
    return bool(root) and drive_type(root) in _ALWAYS_THERE


def shortcut_drive(path) -> str:
    """The drive ("C:\\") a Windows shortcut (.lnk) points at, read from the
    file alone — its LinkInfo, see Microsoft's [MS-SHLLINK] — or "": a target
    on a share, a shell item with no path, an icon kept on a share, or a file
    that is not a shortcut. (Asking Qt or the shell for the target looks at
    the target too, which on a share that does not answer takes 20 s.)"""
    try:
        if _NOT_HERE & getattr(os.stat(path, follow_symlinks=False),
                               "st_file_attributes", 0):
            return ""                    # in the cloud: reading it downloads it
        with open(path, "rb") as fh:
            data = fh.read(1 << 16)
        if len(data) < 0x4C or struct.unpack_from("<I", data, 0)[0] != 0x4C:
            return ""
        flags = struct.unpack_from("<I", data, 20)[0]
        pos = 0x4C
        if flags & 0x1:                  # HasLinkTargetIDList: skip it
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        drive = ""
        if flags & 0x2:                  # HasLinkInfo
            size, _head, where, _volume, base = struct.unpack_from("<5I", data, pos)
            if where & 0x2:              # CommonNetworkRelativeLink: a share
                return ""
            # VolumeIDAndLocalBasePath — unless ForceNoLinkInfo tells the
            # shell to ignore it
            if where & 0x1 and not flags & 0x100:
                drive = data[pos + base:pos + base + 2].decode("latin-1")
            pos += size
        # the strings that follow: name, relative path, working folder,
        # arguments, icon location — the icon's file is read when painted
        wide = 2 if flags & 0x80 else 1  # IsUnicode
        icon = ""
        for bit in (0x4, 0x8, 0x10, 0x20, 0x40):
            if not flags & bit:
                continue
            count = struct.unpack_from("<H", data, pos)[0]
            text = data[pos + 2:pos + 2 + count * wide]
            pos += 2 + count * wide
            if bit == 0x40:
                icon = text.decode("utf-16-le" if wide == 2 else "latin-1", "replace")
        # ExtraData: the target and the icon may be given with environment
        # variables (Windows' own tools: "%windir%\\system32\\cmd.exe"); the
        # shell then goes by those
        found = _shortcut_blocks(data, pos)
        if flags & 0x200 and found.get(0xA0000001):                  # HasExpString
            target = os.path.expandvars(found[0xA0000001]).strip().strip('"')
            if not drive:
                drive = target[:2]
            elif not _on_fixed_disk(target):     # a share, or a variable gone
                return ""
        if flags & 0x4000 and found.get(0xA0000007):                 # HasExpIcon
            icon = found[0xA0000007]
        # a known / special folder the shell re-points the target by when the
        # link is loaded (Documents redirected to a share since it was made);
        # a virtual one (the user's files) has no place: the LinkInfo decides.
        # DisableKnownFolderTracking (0x200000) is about the known folder only.
        tracked = [found.get(0xA0000005)]
        if not flags & 0x200000:
            tracked.append(found.get(0xA000000B))
        for folder in filter(None, tracked):
            if not _on_fixed_disk(folder):
                return ""
            drive = drive or folder[:2]          # no LinkInfo: rebuilt under it
        if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
            return ""
        icon = os.path.expandvars(icon).strip().strip('"')
        if icon.startswith(("\\\\", "//")) or (drive_of(icon) and not _on_fixed_disk(icon)):
            return ""
        return drive.upper() + "\\"
    except (OSError, struct.error, ValueError):
        return ""


def _shortcut_blocks(data, pos) -> dict:
    """signature -> path, of a shortcut's ExtraData blocks that name a place:
    the environment-variable target (0xA0000001) and icon (0xA0000007) — 788
    bytes, the path in ANSI at 8 and in UTF-16 at 268 — and the known folder
    (0xA000000B, a GUID at 8) or special folder (0xA0000005, a CSIDL at 8)
    the target lies in, as where that folder is now ("" if it cannot be
    told)."""
    found = {}
    while pos + 8 <= len(data):
        size, signature = struct.unpack_from("<II", data, pos)
        if size < 8:                     # the terminal block
            break
        block = data[pos:pos + size]
        if signature in (0xA0000001, 0xA0000007) and size >= 788:
            text = block[268:788].decode("utf-16-le", "replace").split("\0", 1)[0]
            found[signature] = text or block[8:268].decode(
                "latin-1").split("\0", 1)[0]
        elif signature == 0xA000000B and size >= 28:
            found[signature] = _known_folder(block[8:24])
        elif signature == 0xA0000005 and size >= 16:
            found[signature] = _special_folder(struct.unpack_from("<I", block, 8)[0])
        pos += size
    return found


def _known_folder(guid) -> str:
    """Where the known folder *guid* (16 bytes, as stored) is now — from the
    registry, the folder itself is not looked at (KF_FLAG_DONT_VERIFY)."""
    try:
        import ctypes
        buffer = ctypes.create_string_buffer(bytes(guid), 16)
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(buffer, 0x4000, None,
                                                      ctypes.byref(path)) != 0:
            return ""
        try:
            return path.value or ""
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path)
    except Exception:
        return ""


def _special_folder(csidl) -> str:
    """Where the special folder *csidl* is now (CSIDL_FLAG_DONT_VERIFY)."""
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(520)
        if ctypes.windll.shell32.SHGetFolderPathW(None, (csidl & 0xFF) | 0x4000, None,
                                                  0, buffer) != 0:
            return ""
        return buffer.value
    except Exception:
        return ""


_ICON_FILES = (".url", ".website", ".scf")   # name an icon file inside them
_FOLDER_SHORTCUT = "{0AFACED1-E828-11D1-9187-B532F1E9575D}"


def _icon_on_a_share(path, is_dir) -> bool:
    """Does a row's icon come from a file on a share? An internet shortcut or
    a command file names it (IconFile=), a folder in its desktop.ini
    (IconResource= / IconFile=). The shell would read it when the row is
    painted: on a share that does not answer, 5 s each time (worker thread)."""
    try:
        with open(os.path.join(path, "desktop.ini") if is_dir else path, "rb") as fh:
            raw = fh.read(1 << 15)
    except OSError:
        return False                     # none: nothing to fetch
    text = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") \
        else raw.decode("latin-1")
    for line in text.splitlines():
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if is_dir and key == "clsid2" and value.strip().upper() == _FOLDER_SHORTCUT:
            # a folder shortcut (a Network Location): drawn as its target.lnk
            drive = shortcut_drive(os.path.join(path, "target.lnk"))
            if not drive or drive_type(drive) not in _ALWAYS_THERE:
                return True
        elif is_dir and key in ("clsid", "clsid2", "uiclsid"):
            return True                  # a shell extension draws it: not asked
        if key in ("iconfile", "iconresource"):
            where = os.path.expandvars(value.strip().strip('"')).split(",")[0].strip()
            if where.lower().startswith("file:"):
                where = QUrl(where).toLocalFile()        # file://server/x: UNC
                if not (drive_of(where) and _on_fixed_disk(where)):
                    return True          # (or a form Qt reads differently)
            elif re.match(r"[a-z][a-z0-9+.-]+:", where, re.I) and not drive_of(where):
                return True              # http: and the like: not a local file
            if where.startswith(("\\\\", "//")) or (drive_of(where)
                                                    and not _on_fixed_disk(where)):
                return True
    return False


def _shortcut_ready(path, info) -> bool:
    """Can the row of the Windows shortcut *path* have its system icon? The
    icon asks what the shortcut points at when it is painted, and Qt keeps
    no answer that failed: a target on a share that does not answer would
    hold the window at every repaint. So only a shortcut to a fixed disk
    (:func:`shortcut_drive`), its target looked at here, where the answer is
    kept (worker thread)."""
    drive = shortcut_drive(path)
    if not drive or drive_type(drive) not in _ALWAYS_THERE:
        return False
    try:
        return info.exists() and (info.isFile() or info.isDir())
    except Exception:
        return False


# the system's pictures of files, by (path, modified, side) — a null one where
# the shell gave none, so it is not asked again (it gives up after 5 s)
_SHELL_PICTURES: "OrderedDict" = OrderedDict()
_SHELL_PICTURES_MAX = 2048
_LATER = Qt.UserRole + 1       # a row's (QFileInfo, modified): its picture to come
_PICTURE_TURN = 0.02           # s of pictures fetched per turn of the event loop
_PICTURE_HUNG = 1.0            # s one picture may take before the rest are skipped
_PICTURE_BUDGET = 1.5          # s of the shell's time for pictures in any...
_PICTURE_WINDOW = 10.0         # ...this many s, as a bucket that refills (a folder
                               # relisted every 2 s, a download in it: asked again)
_HUNG_FOR = 600.0              # s a file whose picture hung is not asked about again
_HUNG: dict = {}               # path -> when its picture hung (perf_counter)


def _hung_key(info) -> str:
    return os.path.normcase(info.absoluteFilePath())


def _hung(info) -> bool:
    """Did the shell hang on this file's picture lately?"""
    when = _HUNG.get(_hung_key(info))
    return when is not None and time.perf_counter() - when <= _HUNG_FOR


def _note_hung(info) -> None:
    """Remember that the shell hung on *info*'s picture (the oldest first;
    expired ones and the oldest past 512 go)."""
    now, key = time.perf_counter(), _hung_key(info)
    _HUNG.pop(key, None)
    _HUNG[key] = now
    for old in [k for k, when in _HUNG.items() if now - when > _HUNG_FOR]:
        del _HUNG[old]
    while len(_HUNG) > 512:
        del _HUNG[next(iter(_HUNG))]


class _LocalIcons:
    """Row icons. On Windows the system's own, as Qt's file model showed them
    (Desktop and Downloads have their own, programs and shortcuts theirs) for
    rows on this PC's disks, from a QFileInfo the listing worker already read;
    the style's folder, file and drive icons for the rest, and everywhere on
    other systems and on Qt's headless platform.

    A system icon is never left to be drawn by the shell when the row is
    painted — Qt asks it again at every repaint for programs, shortcuts and
    icon files (16 ms a row; seconds for an icon on a share), and through
    Python an icon waits on the GIL. The row shows the style's icon, and its
    picture is fetched once, when the row is in view (:meth:`picture`), kept,
    and set as a plain pixmap."""

    def __init__(self, style):
        self.native = _WINDOWS and (QGuiApplication.platformName() or "") == "windows"
        self._provider = QFileIconProvider() if self.native else None
        self._style = {(True, False): style.standardIcon(QStyle.SP_DirIcon),
                       (True, True): style.standardIcon(QStyle.SP_DirLinkIcon),
                       (False, False): style.standardIcon(QStyle.SP_FileIcon),
                       (False, True): style.standardIcon(QStyle.SP_FileLinkIcon)}
        self._drive = style.standardIcon(QStyle.SP_DriveHDIcon)
        self._network = style.standardIcon(QStyle.SP_DriveNetIcon)

    def get(self, is_dir, is_link, drive_kind=None):
        """The style's icon of a row; *drive_kind*: GetDriveType of a drive's."""
        if drive_kind is not None:
            return self._network if drive_kind == _REMOTE_DRIVE else self._drive
        return self._style[is_dir, is_link]

    def kept(self, info, stamp, side):
        """The picture of *info* if it was fetched already (else None)."""
        key = (info.absoluteFilePath(), stamp, side)
        picture = _SHELL_PICTURES.get(key)
        if picture is not None:
            _SHELL_PICTURES.move_to_end(key)
        return picture

    def picture(self, info, stamp, side):
        """The system's picture of *info* (null if the shell gave none), from
        the shell the first time, then kept."""
        picture = self.kept(info, stamp, side)
        if picture is None:
            picture = self._provider.icon(info).pixmap(side, side)
            _SHELL_PICTURES[(info.absoluteFilePath(), stamp, side)] = picture
            while len(_SHELL_PICTURES) > _SHELL_PICTURES_MAX:
                _SHELL_PICTURES.popitem(last=False)
        return picture


# --------------------------------------------------------------------------- #
# The local pane
# --------------------------------------------------------------------------- #
class _LocalView(ListTree):
    """The local listing. Rows can be dragged out (onto the remote pane, or into
    Explorer — always as a copy); what it accepts is ONLY a drag from its own
    remote pane, which means "download here". Local-to-local drops are refused,
    so files are never moved around on disk by a slip of the mouse — and so are
    rows dragged from another session's remote pane: their names would be
    looked up in THIS pane's remote folder, and a different file downloaded."""
    remote_dropped = pyqtSignal(list)    # names of remote entries dropped here
    refused = pyqtSignal(str)            # why a drag from elsewhere is not taken
    shown_more = pyqtSignal()            # resized: other rows may be in view

    def __init__(self, parent=None):
        super().__init__(parent)
        self.accepts = None              # callable -> the REMOTE_SOURCE_MIME we take
        self.folder = ""                 # the folder the rows are in
        self.dragging = False
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

    # ---- dragging rows out ----
    def path_of(self, item) -> str:
        """The local path of a row ("" for ".." and for a drive)."""
        name = item.data(0, Qt.UserRole)
        if not name or name == ".." or not self.folder:
            return ""
        return os.path.join(self.folder, name)

    def supportedDropActions(self):  # noqa: N802 - Qt API
        # a drag out is a copy: offered a move, Explorer moves the user's file
        return Qt.CopyAction

    def mimeTypes(self):  # noqa: N802 - Qt API
        return ["text/uri-list"]

    def mimeData(self, items):  # noqa: N802 - Qt API
        data = QMimeData()
        paths = [p for p in (self.path_of(it) for it in items) if p]
        if paths:
            data.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return data

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.shown_more.emit()

    def startDrag(self, actions):  # noqa: N802 - Qt API
        self.dragging = True
        try:
            super().startDrag(Qt.CopyAction)
        finally:
            self.dragging = False

    # ---- drops ----
    def remote_names(self, mime):
        """``(names, refused)`` of a drag: the remote names it carries if it
        comes from this pane's own remote pane (and folder), else ``[]`` — with
        *refused* True when it is remote rows from somewhere else."""
        if not mime.hasFormat(REMOTE_MIME):
            return [], False
        source = bytes(mime.data(REMOTE_SOURCE_MIME)).decode("utf-8", "replace")
        if self.accepts is None or not source or source != self.accepts():
            return [], True
        text = bytes(mime.data(REMOTE_MIME)).decode("utf-8", "replace")
        return [name for name in text.split("\n") if name], False

    def _take(self, event) -> list:
        names, _refused = self.remote_names(event.mimeData())
        if names:
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()
        return names

    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        if self.remote_names(event.mimeData())[1]:
            self.refused.emit("Those remote files belong to another session's file "
                              "browser — drop them on the local pane next to it.")
        self._take(event)

    def dragMoveEvent(self, event):  # noqa: N802 - Qt API
        self._take(event)

    def dropEvent(self, event):  # noqa: N802 - Qt API
        names = self._take(event)
        if names:
            self.remote_dropped.emit(names)


class _DriveCombo(QComboBox):
    """The drives; read again each time the list opens (a USB stick)."""

    def reload(self, current=""):
        self.blockSignals(True)
        try:
            self.clear()
            self.addItems(drives())
            self.setCurrentIndex(self.findText(current))     # -1: a share, the list
        finally:
            self.blockSignals(False)

    def showPopup(self):  # noqa: N802 - Qt API
        self.reload(self.currentText())
        super().showPopup()


class LocalPane(QWidget):
    """This PC. ``cwd`` is the folder shown ("" = the list of drives); it
    changes at once, its listing follows from a worker."""
    upload_requested = pyqtSignal(list)  # local paths to send to the remote pane
    message = pyqtSignal(str)
    deleted = pyqtSignal(int)            # a delete finished: how many items went

    def __init__(self, start_dir: str = "", parent=None):
        super().__init__(parent)
        self.cwd = ""
        self._jobs = set()               # listings, start-folder check, deletes
        self._moved = False              # navigated since the pane opened
        self._deleting = False
        self._closing = False
        self._gen = 0                    # the listing the pane waits for
        self._entries = {}               # name -> _Entry: the listing on screen
        self._message = ""               # last error / result, until the next action
        self._summary = ""
        self._loading = False
        self._keep = False               # the listing coming keeps the selection
        self._after = None               # (selected, current, scroll) to set after it
        self._fill = None                # [gen, listing, next spec, rows]: building
        self._watched = ""               # the folder watched for changes
        self._first_change = 0.0
        self._go_gen = 0                 # the folder check the pane waits for
        self._no_pictures = -1           # rows whose pictures hung: no more
        self._shown_gen = 0              # the listing whose rows are on screen
        self._shell_time = 0.0           # s of the shell's time owed (the bucket)
        self._drained = 0.0              # when it was last brought up to date
        self._after_base = None
        self._stale = False              # changed on disk while hidden
        self._icons = _LocalIcons(self.style())
        self._fill_timer = QTimer(self)
        self._fill_timer.setSingleShot(True)
        self._fill_timer.setInterval(0)
        self._fill_timer.timeout.connect(self._fill_more)
        # the folder on screen is watched: what lands in it (a download, a file
        # saved from another program) shows up by itself
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._changed)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(_SETTLE_MS)
        self._settle.timeout.connect(self._settled_change)
        self._pictures = QTimer(self)    # system icons for the rows in view
        self._pictures.setSingleShot(True)
        self._pictures.setInterval(0)
        self._pictures.timeout.connect(self._fetch_pictures)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.head = QWidget()
        top = QHBoxLayout(self.head)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.title = pane_title("This PC", "monitor", "blue")
        self.drive = _DriveCombo()
        self.drive.setToolTip("Drive")
        self.drive.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.drive.activated.connect(lambda i: self._drive_picked(self.drive.itemText(i)))
        self.drive.setVisible(_WINDOWS)
        self.path = QLineEdit(); self.path.returnPressed.connect(self._go)
        self.path.setToolTip("Folder on this PC — type a path and press Enter")
        up = flat_button("Up", "arrow-up", "accent", "Parent folder (Backspace)")
        up.clicked.connect(self.up)
        ref = flat_button("Refresh", "refresh", "green", "List this folder again (F5)")
        ref.clicked.connect(self.refresh)
        top.addWidget(self.title); top.addWidget(self.drive)
        top.addWidget(self.path, 1); top.addWidget(up); top.addWidget(ref)
        lay.addWidget(self.head)

        self.view = _LocalView()
        self.view.header().sortIndicatorChanged.connect(self._resort)
        # scrolling: once it pauses (a drag through a folder of programs
        # asked the shell at every step)
        self.view.verticalScrollBar().valueChanged.connect(lambda _v: self._pictures.start(40))
        self.view.shown_more.connect(lambda: self._pictures.start(40))
        self.view.itemActivated.connect(self._open_item)        # double-click / Enter
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        lay.addWidget(self.view, 1)

        self.status = QLabel()
        self.status.setObjectName("mutedHint")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        ops, _buttons = ops_row((
            ("New folder", self.new_folder, "Create a folder here"),
            ("Open", self.open_selected, "Open the selected item with its program"),
            ("Rename", self.rename_selected, "Rename the selected item (F2)"),
        ), self.delete_selected, "Delete the selection (to the recycle bin where "
                                 "there is one) (Del)")
        lay.addLayout(ops)

        # one owner per key: two shortcuts on one key that both apply cancel
        # out (neither fires). Backspace and Delete belong to the list alone,
        # so the path box keeps them.
        for keys, owner, slot in (("F5", self, self.refresh), ("Alt+Up", self, self.up),
                                  ("Backspace", self.view, self.up),
                                  ("Delete", self.view, self.delete_selected),
                                  ("F2", self.view, self.rename_selected)):
            QShortcut(QKeySequence(keys), owner, activated=slot,
                      context=Qt.WidgetWithChildrenShortcut)
        # Start at home. The folder saved last time may be on a share that is
        # gone, and asking Windows about it here hung the window for the SMB
        # timeout (on every reconnect, too): look on a worker, then go there.
        self.navigate(os.path.expanduser("~"))
        self._moved = False
        start = os.path.abspath(os.path.expanduser(start_dir.strip())) if start_dir else ""
        if start and os.path.normcase(start) != os.path.normcase(self.cwd):
            run_job(self._jobs, lambda: os.path.isdir(start),
                    lambda ok: self._saved_folder(start, ok))

    def _saved_folder(self, path, ok):
        if ok and not self._moved:       # the user has not gone elsewhere meanwhile
            self.navigate(path, checked=True)

    def close_pane(self):
        """Drop the workers' results (the owner deletes the pane next)."""
        self._closing = True
        self._fill_timer.stop()
        self._settle.stop()
        self._pictures.stop()
        self._watch("")
        close_jobs(self._jobs)

    # ---- navigation ----
    def navigate(self, path: str, checked: bool = False) -> bool:
        """Show *path* ("" = the drives). *checked*: it is known to be a folder
        (else it is looked at here, on the GUI thread: the user's own clicks
        go through :meth:`go`, or know it is a folder)."""
        path = (path or "").strip()
        if path:
            path = os.path.abspath(os.path.expanduser(path))
            if not checked and not os.path.isdir(path):
                self._say(f"Not a folder: {path}")
                self.path.setText(self.cwd)
                return False
        self._moved = True
        self._go_gen += 1                # a folder check still out is too late
        self.cwd = path
        self.path.setText(path)
        self.drive.reload(drive_of(path))
        self._message = ""
        self._list(keep=False)
        return True

    def go(self, path):
        """Go to a folder the user named (the path box, a quick link). It is
        looked at on a worker first: asking about a share that is gone held the
        window for the network's timeout."""
        path = (path or "").strip()
        if not path:
            self.navigate("")
            return
        path = os.path.abspath(os.path.expanduser(path))
        self._moved = True               # the saved start folder is not wanted now
        self._go_gen += 1
        gen = self._go_gen
        self._say(f"Opening {path}…")
        run_job(self._jobs, lambda: os.path.isdir(path),
                lambda ok: self._gone_to(gen, path, ok))

    def _gone_to(self, gen, path, ok):
        if self._closing or gen != self._go_gen:
            return                       # the user went elsewhere meanwhile
        if ok:
            self.navigate(path, checked=True)
            return
        self._say(f"Not a folder: {path}")
        typed = self.path.text().strip()
        if not typed or os.path.abspath(os.path.expanduser(typed)) == path:
            self.path.setText(self.cwd)      # (not what was typed since)

    def _go(self):
        self.go(self.path.text())

    def _drive_picked(self, drive):
        if drive:                        # unread drives fail in the listing
            self.navigate(drive, checked=True)
            self.view.setFocus(Qt.OtherFocusReason)

    def _standard(self, location):
        self.go(QStandardPaths.writableLocation(location))

    def go_home(self):
        self.go(os.path.expanduser("~"))

    def go_desktop(self):
        self._standard(QStandardPaths.DesktopLocation)

    def go_downloads(self):
        self._standard(QStandardPaths.DownloadLocation)

    def up(self):
        if not self.cwd:
            return
        here = self.cwd.rstrip("\\/") or self.cwd
        parent = os.path.dirname(here)
        # above a drive root ("C:\\") is the list of drives; above "/" is "/"
        if not parent or os.path.normcase(parent) == os.path.normcase(here) \
                or parent == self.cwd:
            parent = "" if _WINDOWS else "/"
        if parent == self.cwd:
            return
        left = os.path.basename(here) if parent else drive_of(self.cwd)
        self.navigate(parent, checked=True)
        self._after = (set(), left, None)        # on the folder it came from

    def refresh(self):
        """List the folder again, keeping the selection and the scroll."""
        self._message = ""
        self._list(keep=True)

    # ---- listing ----
    def _list(self, keep=False, after=None):
        """List the folder on a worker. *keep*: the rows on screen stay until the
        new ones are complete, and their selection and scroll carry over;
        *after*: ``(selected, current, scroll)`` to set instead."""
        if self._closing:
            return
        self._gen += 1
        gen, path, order = self._gen, self.cwd, self.view.order
        native = self._icons.native
        self._fill = None
        self._fill_timer.stop()
        self._loading = True
        self._stale = False              # this listing is the fresh one
        self._keep, self._after = keep, after
        # the selection when a "select the new name" was queued: if the user
        # changes it meanwhile, theirs is kept
        self._after_base = self._selected_names() if after is not None else None
        if not keep:
            self.view.clear()
            self._entries = {}
            self.view.folder = path
            self._rows_are(gen)
        self._show()
        run_job(self._jobs, lambda: _local_listing(path, order, native),
                lambda result: self._listed(gen, result),
                lambda msg: self._list_failed(gen, path, msg))

    def _selected_names(self) -> set:
        return {it.data(0, Qt.UserRole) for it in self.view.selectedItems()}

    def _view_state(self):
        """(selected names, current name, scroll position) of the list."""
        current = self.view.currentItem()
        return ({it.data(0, Qt.UserRole) for it in self.view.selectedItems()},
                current.data(0, Qt.UserRole) if current is not None else None,
                self.view.verticalScrollBar().value())

    def _listed(self, gen, listing):
        if self._closing or gen != self._gen:
            return                       # an older listing: a newer one is coming
        self._fill = [gen, listing, 0, []]
        self._fill_more()

    def _fill_more(self):
        """Make the rows, a chunk per turn of the event loop (the remote pane
        does the same: 20,000 rows at once froze the window), then put them in
        all at once. Until then the list on screen stays as it is — and what
        the user selects there meanwhile is what stays selected."""
        if self._closing or self._fill is None:
            return
        gen, listing, start, rows = self._fill
        if gen != self._gen:
            self._fill = None
            return
        specs, path, infos, kinds = listing.specs, listing.path, listing.infos, listing.kinds
        end = min(len(specs), start + _FILL_CHUNK)
        icons, side = self._icons, self.view.iconSize().width()
        for spec in specs[start:end]:
            # a .lnk is a link to the eye even where it gets no system icon
            link = spec.is_link or spec.name.lower().endswith(".lnk")
            icon = icons.get(spec.is_dir, link, None if path else kinds.get(spec.name, 0))
            info = infos.get(spec.name)
            stamp = spec.keys[2]
            picture = icons.kept(info, stamp, side) if info is not None else None
            if picture is not None and not picture.isNull():
                icon = QIcon(picture)
            row = make_row(spec.name, icon, spec.rank, spec.keys, *spec.texts)
            if info is not None and picture is None and not _hung(info):
                row.setData(0, _LATER, (info, stamp))
            rows.append(row)
        if end < len(specs):
            self._fill[2] = end
            self._fill_timer.start()
            return
        self._fill = None
        if listing.order != self.view.order:     # a column was clicked meanwhile
            rows = _sorted_rows(rows, *self.view.order)
        if self._after is not None and not (
                self._keep and self._selected_names() != self._after_base):
            state = self._after
        else:
            state = self._view_state() if self._keep else None
        self._entries = {e.name: e for e in listing.entries}
        self._summary = (listing_summary(listing.entries) if path
                         else f"{len(listing.entries)} drives")
        self.view.clear()
        self.view.folder = path
        if path:
            self.view.addTopLevelItem(make_row("..", parent_icon(), 0, PARENT_KEYS))
        self.view.addTopLevelItems(rows)
        self._rows_are(gen)
        self._loading = False
        self._keep, self._after = False, None
        self._restore(state)
        self._show()
        self._watch(path if listing.watch else "")
        self._pictures.start(0)          # the system icons of the rows in view

    def _rows_are(self, gen):
        """The rows on screen are now listing *gen*'s: a stop for the rows
        before (a picture that hung) does not hold for them."""
        self._shown_gen = gen

    def _fetch_pictures(self):
        """Give the rows in view their system icon, a few per turn of the event
        loop (each is one question to the shell, once per file version). The
        rows on screen keep the style's icons from the first picture that took
        the shell over _PICTURE_HUNG (a share that does not answer); and the
        shell gets _PICTURE_BUDGET of every _PICTURE_WINDOW, the rest waits for
        the next window (slow disks, big programs, a folder that keeps
        changing)."""
        if (self._closing or self._icons._provider is None
                or not self.view.isVisible() or self._no_pictures == self._shown_gen):
            return                       # (shown again: showEvent starts it)
        view, side = self.view, self.view.iconSize().width()
        started = time.perf_counter()    # (monotonic ticks in 15.6 ms on Windows)
        item = view.itemAt(QPoint(1, 1))
        bottom = view.viewport().height()
        while item is not None and view.visualItemRect(item).top() < bottom:
            later = item.data(0, _LATER)
            if later is not None and _hung(later[0]):
                item.setData(0, _LATER, None)    # it hung meanwhile: not again
            elif later is not None:
                now = time.perf_counter()
                if now - started > _PICTURE_TURN:
                    self._pictures.start(0)      # the rest next turn
                    return
                # the time owed drains at _PICTURE_BUDGET per _PICTURE_WINDOW:
                # a 5 s hang buys half a minute without pictures
                rate = _PICTURE_BUDGET / _PICTURE_WINDOW
                self._shell_time = max(0.0, self._shell_time - (now - self._drained) * rate)
                self._drained = now
                if self._shell_time > _PICTURE_BUDGET:
                    self._pictures.start(min(60000, int((self._shell_time - _PICTURE_BUDGET)
                                                        / rate * 1000) + 1))
                    return
                item.setData(0, _LATER, None)
                picture = self._icons.picture(later[0], later[1], side)
                if not picture.isNull():
                    item.setIcon(0, QIcon(picture))
                took = time.perf_counter() - now
                # owed: capped, so one long hang (or a suspend) pauses pictures
                # for a minute or so, not for hours
                self._shell_time = min(self._shell_time + took, 2 * _PICTURE_BUDGET)
                if took > _PICTURE_HUNG:
                    # not this file again for a while (a file being written
                    # changes at every listing), nor the rest of these rows
                    _note_hung(later[0])
                    self._no_pictures = self._shown_gen
                    return
            item = view.itemBelow(item)

    def _restore(self, state):
        """Select and scroll back to where the list was (a refresh), or put the
        keyboard on the entry just made or left (Up), else the top."""
        chosen, current, scroll = state or (set(), None, None)
        first, picked = None, []
        for i in range(self.view.topLevelItemCount()):
            it = self.view.topLevelItem(i)
            name = it.data(0, Qt.UserRole)
            if name in chosen:
                picked.append(i)
            if name == current:
                first = it
        select_rows(self.view, picked)
        if first is None and self.view.topLevelItemCount():
            first = self.view.topLevelItem(0)
        if first is not None:
            self.view.setCurrentItem(first, 0, QItemSelectionModel.NoUpdate)
        if scroll is not None:
            self.view.doItemsLayout()    # the scroll range of the new rows
            self.view.verticalScrollBar().setValue(scroll)
        elif first is not None:
            self.view.scrollToItem(first)

    def _list_failed(self, gen, path, msg):
        if self._closing or gen != self._gen:
            return
        self._rows_are(gen)
        self._loading = False
        self._keep, self._after = False, None
        self._entries = {}
        self.view.clear()
        self.view.folder = path
        if path:                         # ".." to get away from here
            self.view.addTopLevelItem(make_row("..", parent_icon(), 0, PARENT_KEYS))
        self._summary = ""
        self._watch("")
        self._say(f"Cannot list {path or 'the drives'}: {msg}")

    def _resort(self, column, order):
        """A column was clicked: re-order the rows (rows still being made are
        put in order when they come)."""
        self.view.order = (column, order == Qt.AscendingOrder)
        reorder_rows(self.view)
        self._pictures.start(0)          # other rows may be in view now

    # ---- the folder changing on disk ----
    def _watch(self, path):
        """Watch *path* alone ("" = nothing), if it is not watched already. The
        listing says whether to: a folder on a share is not watched (a watch
        there can wait on the network); a transfer's end lists it again."""
        if path == self._watched and (not path or path in self._watcher.directories()):
            return                       # (Qt drops a folder that was deleted)
        old = self._watcher.directories()
        if old:
            self._watcher.removePaths(old)
        self._watched = path
        if path:
            self._watcher.addPath(path)

    def _changed(self, _path=""):
        """List again once the folder has settled — or, if it keeps changing,
        _SETTLE_MAX seconds after the first change."""
        if self._closing:
            return
        now = time.monotonic()
        if not self._settle.isActive():
            self._first_change = now
            self._settle.start()
        elif now - self._first_change < _SETTLE_MAX:
            self._settle.start()         # wait for it to settle

    def changed_soon(self):
        """Something may have changed in the folder (a transfer ended): list it
        again shortly, keeping the selection."""
        self._changed()

    def _settled_change(self):
        if self._closing:
            return
        if self.view.dragging or self._loading or self._deleting:
            self._settle.start()         # not under the user's hands: later
            return
        if not self.isVisible():         # another tab is up: when it shows
            self._stale = True
            return
        self._list(keep=True)            # (a message on screen stays)

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        if self._stale and not self._closing and not self._loading:
            self._stale = False
            self._list(keep=True)
        self._pictures.start(0)

    # ---- selection ----
    def selection_paths(self) -> list:
        """The selected entries' paths (".." never; the drives themselves in
        the list of drives)."""
        names = [it.data(0, Qt.UserRole) for it in self.view.selectedItems()]
        names = [n for n in names if n and n != ".."]
        if not self.cwd:
            return names
        return [os.path.normpath(os.path.join(self.cwd, n)) for n in names]

    def _say(self, text):
        self._message = text
        self._show()
        if text:
            self.message.emit(text)

    def _show(self):
        if self._message:
            text = self._message
        elif self._loading:
            text = f"Loading {self.cwd or 'the drives'}…"
        else:
            text = self._summary
        self.status.setText(text)

    # ---- actions ----
    def _open_item(self, item, _column=0):
        name = item.data(0, Qt.UserRole)
        if name == "..":
            self.up()
            return
        if not self.cwd:                 # a drive
            self.navigate(name, checked=True)
            return
        entry = self._entries.get(name)
        path = os.path.join(self.cwd, name)
        if entry is not None and entry.is_dir:
            self.navigate(path, checked=True)    # a vanished one fails to list
        elif entry is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_selected(self):
        paths = self.selection_paths()
        if len(paths) == 1:
            entry = self._entries.get(os.path.basename(paths[0]))
            if not self.cwd or (entry is not None and entry.is_dir):
                self.navigate(paths[0], checked=True)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(paths[0]))
        elif not paths:
            self._say("Select a file or folder to open first.")

    def _context_menu(self, pos):
        paths = self.selection_paths()
        menu = QMenu(self)
        if paths and self.cwd:
            menu.addAction("Upload to the remote pane", lambda: self.upload_requested.emit(paths))
        if len(paths) == 1:
            menu.addAction("Open", self.open_selected)
        if self.cwd:
            menu.addAction("Show this folder in the file manager",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.cwd)))
            menu.addSeparator()
            menu.addAction("New folder", self.new_folder)
        if len(paths) == 1 and self.cwd:
            menu.addAction("Rename (F2)", self.rename_selected)
        if paths and self.cwd:
            menu.addAction("Delete", self.delete_selected)
        if paths:
            menu.addAction("Copy path", lambda: QApplication.clipboard().setText(
                "\n".join(paths)))
        menu.addSeparator()
        menu.addAction("Refresh (F5)", self.refresh)
        menu.exec_(self.view.viewport().mapToGlobal(pos))

    def do_new_folder(self, name: str) -> bool:
        name = (name or "").strip()
        if not self.cwd or not name or os.sep in name or "/" in name:
            self._say("New folder: give a plain name (inside a folder, not the "
                      "drive list).")
            return False
        try:
            os.mkdir(os.path.join(self.cwd, name))
        except OSError as exc:
            self._say(f"Could not create {name}: {exc}")
            return False
        self._message = ""
        self._list(keep=True, after=({name}, name, None))      # on it, alone
        return True

    def new_folder(self):
        if not self.cwd:
            self._say("New folder: open a folder first (not the drive list).")
            return
        name, ok = QInputDialog.getText(self, "New folder", "Name:")
        if ok and name.strip():
            self.do_new_folder(name)

    def do_rename(self, path: str, new_name: str) -> bool:
        new_name = (new_name or "").strip()
        target = os.path.join(os.path.dirname(path), new_name)
        if not new_name or os.sep in new_name or "/" in new_name:
            self._say("Rename: give a plain name, not a path.")
            return False
        if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(path):
            self._say(f"Rename: '{new_name}' already exists.")
            return False
        try:
            os.rename(path, target)
        except OSError as exc:
            self._say(f"Could not rename: {exc}")
            return False
        self._message = ""
        self._list(keep=True, after=({new_name}, new_name, None))
        return True

    def rename_selected(self):
        paths = self.selection_paths()
        if len(paths) != 1 or not self.cwd:
            return
        old = os.path.basename(paths[0])
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=old)
        if ok and new.strip() and new != old:
            self.do_rename(paths[0], new)

    def do_delete(self, paths) -> bool:
        """Send *paths* to the recycle bin, on a worker thread (a big folder or a
        slow disk must not freeze the window). Whatever cannot go there is
        deleted for good only if the user says so: the question they answered
        promised the recycle bin, and a failed move used to fall through to a
        permanent delete without a word. ``deleted(count)`` follows. False if a
        delete is still running."""
        paths = list(paths)
        if self._deleting or not paths:
            return False
        self._deleting = True
        if not _HAS_TRASH:               # the question already said "permanently"
            self._say(f"Deleting {_names(paths)}…")
            run_job(self._jobs, lambda: _remove_for_good(paths),
                    lambda result: self._removed(0, result), self._delete_failed)
        else:
            self._say(f"Moving {_names(paths)} to the recycle bin…")
            run_job(self._jobs, lambda: _trash(paths), self._trashed, self._delete_failed)
        return True

    def _trashed(self, result):
        moved, left = result
        if not left:
            self._delete_done(moved, "")
            return
        paths = [path for path, _why in left]
        answer = QMessageBox.question(
            self, "Not moved to the recycle bin",
            f"{len(paths)} item(s) could not be moved to the recycle bin:\n"
            f"{_names(paths)}\n\nThey are {left[0][1]}.\n\nDelete them permanently "
            f"instead? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if self._closing:                # closed while it asked
            return
        if answer != QMessageBox.Yes:
            self._delete_done(moved, f"Not deleted (still there): {_names(paths)}")
            return
        run_job(self._jobs, lambda: _remove_for_good(paths),
                lambda result: self._removed(moved, result), self._delete_failed)

    def _removed(self, moved, result):
        removed, errors = result
        self._delete_done(moved + removed,
                          ("Could not delete " + "; ".join(errors[:3])) if errors else "")

    def _delete_failed(self, msg):
        self._delete_done(0, f"Could not delete: {msg}")

    def _delete_done(self, count, problem):
        self._deleting = False
        if self._closing:
            return
        self._list(keep=True)
        self._say(problem)
        self.deleted.emit(count)

    def delete_selected(self):
        paths = self.selection_paths()
        if not paths or not self.cwd:
            return
        where = "moved to the recycle bin" if _HAS_TRASH else "deleted permanently"
        answer = QMessageBox.question(
            self, "Delete",
            f"Delete {len(paths)} local item(s)?\n{_names(paths)}\n\nThey are {where}.")
        if answer == QMessageBox.Yes:
            self.do_delete(paths)


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #
# the remote quick links: (label, glyph, folder, tip)
REMOTE_LINKS = (("Home", "house", ".", "Your login folder on the remote host"),
                ("/", "folder", "/", "The top of the remote file system"),
                ("/tmp", "folder", "/tmp", "Temporary files on the remote host"))


def _transfer_button(text, glyph, tone, tip) -> QPushButton:
    """Push or Pull: both the same size, in the colour of the side the files
    come from (this PC blue, the remote host green)."""
    button = QPushButton(text)
    button.setProperty("tone", tone)
    button.setIcon(glyphs.icon(glyph, tone))
    button.setToolTip(tip)
    button.setFixedSize(96, 34)
    # placed by the button itself: the room a style keeps beside a button is
    # mirrored for Push (right to left, for its icon), which set the two apart
    button.setAttribute(Qt.WA_LayoutUsesWidgetRect, True)
    button.setFocusPolicy(Qt.TabFocus)   # a click keeps the pane's focus
    return button


class FileBrowser(QWidget):
    """Quick links | local pane, Push / Pull, remote pane | the Transfers panel."""
    log = pyqtSignal(str)
    notify = pyqtSignal(str, str)        # text, kind — the main window's toasts
    # (remote folder, local folder): open another Files tab there
    new_tab_requested = pyqtSignal(str, str)

    def __init__(self, handler, parent=None, *, local_start=None, remote_start=".",
                 save_start=True):
        """*local_start* / *remote_start*: the folders to open (default: the
        local one saved last time, the remote login folder). *save_start*: on
        close, save the local folder as the next start (not for the extra
        browsers New tab opens)."""
        super().__init__(parent)
        from . import settings
        self._save_start = save_start
        self.transfers = TransferLog()
        if local_start is None:
            local_start = settings.get("local_browser_dir") or ""
        self.local = LocalPane(local_start)
        self.remote = SftpBrowser(handler, transfer_log=self.transfers, start=remote_start)
        self.remote.local_target = lambda: self.local.cwd or None
        self.remote.show_inline_progress = False     # the panel below shows it
        self.remote.log.connect(self.log)
        self.remote.notify.connect(self.notify)
        # a download into the local folder ended: list it again (a share is
        # not watched)
        self.remote.transfer_ended.connect(self._transfer_ended)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._quick_links())

        body = QVBoxLayout()
        body.setContentsMargins(8, 8, 8, 6)
        body.setSpacing(6)
        lay.addLayout(body, 1)

        middle = QWidget()
        column = QVBoxLayout(middle)
        column.setContentsMargins(4, 0, 4, 0)
        column.setSpacing(10)
        self.push_btn = _transfer_button(
            "Push", "arrow-right", "blue",
            "Push: upload the selected local items into the remote folder")
        self.push_btn.setLayoutDirection(Qt.RightToLeft)   # reads "Push →"
        self.push_btn.clicked.connect(lambda: self.upload())
        self.pull_btn = _transfer_button(
            "Pull", "arrow-left", "green",
            "Pull: download the selected remote items into the local folder")
        self.pull_btn.clicked.connect(self.download)
        column.addStretch(1)
        column.addWidget(self.push_btn, 0, Qt.AlignHCenter)
        column.addWidget(self.pull_btn, 0, Qt.AlignHCenter)
        column.addStretch(1)

        panes = QSplitter(Qt.Horizontal)
        panes.setChildrenCollapsible(False)
        panes.setHandleWidth(4)
        panes.addWidget(self.local); panes.addWidget(middle); panes.addWidget(self.remote)
        panes.setStretchFactor(0, 1); panes.setStretchFactor(1, 0); panes.setStretchFactor(2, 1)

        self.panel = TransferPanel(self.transfers)
        self.panel.retry_requested.connect(self.remote.retry)
        self.panel.cancel_requested.connect(self.remote.cancel_transfer)

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(panes); split.addWidget(self.panel)
        split.setStretchFactor(0, 4); split.setStretchFactor(1, 1)
        body.addWidget(split, 1)

        delete = "Del Recycle Bin" if _HAS_TRASH else "Del Delete"
        self.hint = QLabel("Drag files between the panes or in from Explorer   ·   "
                           "Enter Open   ·   F2 Rename   ·   F5 Refresh   ·   "
                           f"Backspace Up   ·   {delete}")
        self.hint.setObjectName("mutedHint")
        self.hint.setWordWrap(True)
        body.addWidget(self.hint)

        self.local.upload_requested.connect(self.upload)
        self.local.view.remote_dropped.connect(self._download_names)
        # the local pane takes drops of THIS remote pane's rows only
        self.local.view.accepts = lambda: self.remote.tree.source
        self.local.view.refused.connect(self.local._say)

    def _quick_links(self) -> QWidget:
        """The row on top: folders on this PC (blue) on the left, on the remote
        host (green) and New tab on the right."""
        bar = QWidget()
        bar.setObjectName("pageToolbar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(2)
        label = QLabel("PC folders")
        label.setObjectName("mutedHint")
        row.addWidget(label)
        row.addSpacing(4)
        for text, glyph, slot in (("Home", "house", self.local.go_home),
                                  ("Desktop", "monitor", self.local.go_desktop),
                                  ("Downloads", "download", self.local.go_downloads)):
            button = flat_button(text, glyph, "blue", f"Open your {text} folder")
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        label = QLabel("Remote folders")
        label.setObjectName("mutedHint")
        row.addWidget(label)
        row.addSpacing(4)
        self.remote_links = []
        for text, glyph, folder, tip in REMOTE_LINKS:
            button = flat_button(text, glyph, "green", tip)
            button.clicked.connect(lambda _=False, f=folder: self.remote.navigate(f))
            row.addWidget(button)
            self.remote_links.append(button)
        self.remote.add_controls(*self.remote_links)      # locked while it lists
        self.new_tab_btn = flat_button(
            "New tab", "plus", "blue",
            "Open another file browser on this connection, starting in these folders")
        self.new_tab_btn.clicked.connect(self._request_new_tab)
        self.new_tab_btn.hide()          # shown where something opens it (showEvent)
        row.addWidget(self.new_tab_btn)
        return bar

    def _transfer_ended(self, kind, local):
        if kind == "download" and self.local.cwd and local and os.path.normcase(
                os.path.dirname(os.path.abspath(local))) == os.path.normcase(self.local.cwd):
            self.local.changed_soon()

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        self.new_tab_btn.setVisible(self.receivers(self.new_tab_requested) > 0)

    def _request_new_tab(self):
        self.new_tab_requested.emit(self.remote.cwd, self.local.cwd)

    # ---- the two directions ----
    def upload(self, paths=None) -> int:
        """Queue local *paths* (default: the local selection) for upload into the
        remote pane's folder."""
        paths = list(paths) if paths else self.local.selection_paths()
        if not paths:
            self.local._say("Select the local files or folders to push first.")
            return 0
        return self.remote.upload_paths(paths)

    def download(self) -> int:
        """Queue the remote selection for download into the local pane's folder."""
        if not self.local.cwd:
            self.local._say("Open a local folder to download into first.")
            return 0
        if not self.remote._selection():
            self.remote.hint("Select the remote files or folders to pull first.")
            return 0
        return self.remote.download_to_local_pane()

    def _download_names(self, names) -> int:
        if not self.local.cwd:
            self.local._say("Open a local folder to download into first.")
            return 0
        return self.remote.download_entries_to(self.local.cwd, names=names)

    # ---- what the session tab expects of its file page ----
    @property
    def busy(self) -> bool:
        return self.remote.busy

    @property
    def transferring(self) -> bool:
        return self.remote.transferring

    def rebind(self, handler) -> None:
        """The session reconnected with a new *handler*: carry on over it instead
        of being rebuilt. The transfer queue and history, the open editors (with
        their unsaved changes) and the local pane stay as they are; the remote
        pane's SFTP links move to *handler* and its folder is listed again
        (:meth:`SftpBrowser.rebind`)."""
        self.remote.rebind(handler)

    def close_browser(self):
        """Stop everything (the owner deletes the widget next). Safe to call twice."""
        try:
            from . import settings
            if self._save_start and self.local.cwd \
                    and self.local.cwd != settings.get("local_browser_dir"):
                settings.save({"local_browser_dir": self.local.cwd})
        except Exception:
            pass
        self.local.close_pane()
        self.panel.close_panel()
        self.remote.close_browser()
