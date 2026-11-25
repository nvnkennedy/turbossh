"""The system's own file icons for the remote pane of the Files tab, as
TurboADB's device pane shows them.

Where the platform has a real icon source (Windows Explorer's shell icons,
macOS Finder, a Linux desktop's icon theme) a remote row shows what the
system's own file manager shows: its folder icon and one icon per file type.
A remote file does not exist on this PC, so it gets the icon of its type,
looked up by extension. A symbolic link gets its target kind's icon with a
small link badge, as Explorer marks a shortcut.

Where there is no real source (a bare X server, the headless platform the
tests run on) Qt would fall back to generic style icons, which say less than
TurboSSH's own glyphs, so those stay. So does a row the system draws nothing
for: the ".." row, and device nodes, pipes and sockets.

On Windows the shell is asked on a thread of its own (see shell_icons): a row
shows its glyph until its icon is in, and the window never waits for the
shell. Elsewhere Qt's provider answers; this module adds one shared QIcon per
extension on top.
"""

from __future__ import annotations

import os
import posixpath
import sys
import tempfile
from typing import Callable, Optional, Tuple

from PyQt5.QtCore import QFileInfo, QMimeDatabase, QPoint, QRect, QRectF, Qt
from PyQt5.QtGui import QColor, QGuiApplication, QIcon, QIconEngine, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QFileIconProvider

try:
    from PyQt5 import sip
except ImportError:                      # older PyQt5 wheels
    import sip

# Qt platform plugins with real file icons; X11/Wayland have them only through
# a desktop icon theme.
_NATIVE_PLATFORMS = ("windows", "cocoa")
_THEMED_PLATFORMS = ("xcb", "wayland")


def _shell_lookups() -> bool:
    """True where Windows' shell is asked directly, off the window's thread."""
    return (sys.platform == "win32"
            and (QGuiApplication.platformName() or "").lower() == "windows")


def native_icons_available() -> bool:
    """True where the system can supply real file icons."""
    platform = (QGuiApplication.platformName() or "").lower()
    if platform in _NATIVE_PLATFORMS:
        return True
    if platform.startswith(_THEMED_PLATFORMS):
        return QIcon.themeName() not in ("", "hicolor")
    return False


# The link badge is drawn on a small white plate, as Explorer draws its
# shortcut arrow: a bare glyph at that size vanished into the icon beneath it.
_PLATE = "#ffffff"
_PLATE_EDGE = QColor(0, 0, 0, 110)
_BADGE_COLOR = "#1f6fd1"            # the link mark: a blue that reads on white
_TYPE_MOST = 512                    # file types kept; a new one past this: generic


def type_of(name: str) -> str:
    """The extension that names *name*'s file type, lower case with its dot;
    "" for a suffix that is no type: longer than 8, not plain letters and
    digits, or all digits. A Maildir message (``...host,S=1234:2,S``),
    ``core.1234`` or ``x.001`` would each be a type of its own, looked up and
    kept for good."""
    body = posixpath.splitext(name or "")[1][1:].lower()
    if (not body or len(body) > 8 or not body.isascii() or not body.isalnum()
            or body.isdigit()):
        return ""
    return "." + body


class _BadgeEngine(QIconEngine):
    """A native icon with the link mark on a small plate, bottom-left."""

    def __init__(self, base: QIcon):
        super().__init__()
        self._base = base

    def paint(self, painter, rect, mode, state):
        from . import glyphs
        self._base.paint(painter, rect, Qt.AlignCenter, mode, state)
        side = max(8, int(round(min(rect.width(), rect.height()) * 0.56)))
        plate = QRectF(rect.left(), rect.bottom() - side + 1, side, side)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(_PLATE_EDGE, max(1.0, side / 12.0)))
        painter.setBrush(QColor(_PLATE))
        radius = side * 0.22
        painter.drawRoundedRect(plate.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        inset = max(1, int(side * 0.14))
        glyphs._paint(painter, plate.toRect().adjusted(inset, inset, -inset, -inset),
                      "link", QColor(_BADGE_COLOR), weight=2.6)
        painter.restore()

    def pixmap(self, size, mode, state):
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        self.paint(painter, QRect(QPoint(0, 0), size), mode, state)
        painter.end()
        return pm

    def clone(self):
        return _BadgeEngine(self._base)


class FileIcons:
    """The remote rows' icons: native where the system has them."""

    def __init__(self, *, native: Optional[bool] = None,
                 provider: Optional[Callable[[], QFileIconProvider]] = None, shell=None):
        """*shell*: what answers instead of a provider while icons are looked
        up elsewhere (a :class:`shell_icons.ShellIcons`); by default Windows'
        shell on Windows, unless a *provider* is given."""
        self.native = native_icons_available() if native is None else bool(native)
        if self.native and shell is None and provider is None and _shell_lookups():
            from .shell_icons import ShellIcons
            shell = ShellIcons(QGuiApplication.instance())   # it goes with the app
        self._shell = shell if self.native else None
        self._provider = None
        if self.native and self._shell is None:
            self._provider = (provider or QFileIconProvider)()
        # icons came in: rows still showing their glyph can ask again (None
        # where every answer is there at once)
        self.changed = getattr(self._shell, "changed", None)
        self._by_type = {}               # extension -> QIcon
        self._linked = {}                # ("dir", "") / ("file", ext) -> QIcon
        self._folder = None
        # a folder that never exists: a remote file's name is looked up here,
        # so only its extension counts (never a same-named file on this PC)
        self._nowhere = os.path.join(tempfile.gettempdir(),
                                     f"turbossh-no-such-folder-{os.getpid()}")

    # ---- lookups: None while the shell is still being asked ------------------
    def _folder_or_none(self, ask: bool = True) -> Optional[QIcon]:
        if self._folder is None:
            if self._shell is not None:
                self._folder = self._shell.folder(ask)
            else:
                self._folder = self._provider.icon(QFileIconProvider.Folder)
        return self._folder

    def _type_or_none(self, name: str, ask: bool = True) -> Optional[QIcon]:
        ext = type_of(name)
        if ext not in self._by_type and len(self._by_type) >= _TYPE_MOST:
            ext = ""                     # enough types kept: the generic file
        icon = self._by_type.get(ext)
        if icon is None:
            icon = self._type_icon(ext, ask)
            if icon is None:
                return None
            self._by_type[ext] = icon
        return icon

    def _type_icon(self, ext: str, ask: bool = True) -> Optional[QIcon]:
        """What the system shows for files ending in *ext*: the platform's own
        answer, else the icon theme's for the type's MIME name, else a null
        icon. None while the shell is being asked."""
        if self._shell is not None:
            return self._shell.type_icon(ext, ask)
        icon = self._provider.icon(QFileInfo(os.path.join(self._nowhere, "x" + ext)))
        if icon.isNull():
            mime = QMimeDatabase().mimeTypeForFile("x" + ext, QMimeDatabase.MatchExtension)
            for theme_name in (mime.iconName(), mime.genericIconName()):
                if theme_name and QIcon.hasThemeIcon(theme_name):
                    return QIcon.fromTheme(theme_name)
        return icon

    def _link_or_none(self, name: str, is_dir: bool, ask: bool = True) -> Optional[QIcon]:
        key = ("dir", "") if is_dir else ("file", type_of(name))
        icon = self._linked.get(key)
        if icon is None:
            base = self._folder_or_none(ask) if is_dir else self._type_or_none(name, ask)
            if base is None:
                return None
            icon = base if base.isNull() else QIcon(_BadgeEngine(base))
            self._linked[key] = icon
        return icon

    def new_batch(self) -> None:
        """A folder is being listed: its icons are looked up before those of
        the folders listed earlier."""
        if self._shell is not None:
            self._shell.new_batch()

    # ---- one call for a listing row ------------------------------------------
    def lookup(self, name: str, is_dir: bool, is_link: bool,
               ask: bool = True) -> Optional[QIcon]:
        """The system's icon of a remote entry: None while the shell is still
        being asked, a null icon where the system has none."""
        if is_link:
            return self._link_or_none(name, is_dir, ask)
        return self._folder_or_none(ask) if is_dir else self._type_or_none(name, ask)

    @staticmethod
    def wait_key(name: str, is_dir: bool, is_link: bool) -> tuple:
        """What a row waits for: rows with the same key get the same icon
        (look it up again with ``lookup("x" + key[2], key[0], key[1])``)."""
        return (is_dir, is_link, "" if is_dir else type_of(name))

    def row_icon_ready(self, name: str, is_dir: bool, is_link: bool, special: bool,
                       fallback: QIcon, *, ask: bool = True) -> Tuple[QIcon, bool]:
        """``(icon, ready)`` of a remote row: the system's icon, or *fallback*
        (the row's glyph) where it has none or for a *special* entry (a device
        node, pipe or socket). *ready* is False while the shell is still being
        asked: the row shows *fallback* till then; ask again after
        :attr:`changed`. *ask=False* only looks."""
        if not self.native or special:
            return fallback, True
        icon = self.lookup(name, is_dir, is_link, ask)
        if icon is None:
            return fallback, False
        return (fallback if icon.isNull() else icon), True


_shared: Optional[FileIcons] = None


def file_icons() -> FileIcons:
    """The one FileIcons every Files tab shares (created on first use, once a
    QApplication exists; anew for a new application, or once the shell's
    lookups were stopped when the last one quit)."""
    global _shared
    shell = _shared._shell if _shared is not None else None
    if _shared is None or (shell is not None and (sip.isdeleted(shell) or shell.stopped)):
        _shared = FileIcons()
    return _shared
