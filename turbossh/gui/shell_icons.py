"""Windows' own file icons, asked for on a thread of their own (as on
TurboADB's Files tab, where this comes from).

Asking the shell for an icon (SHGetFileInfo) takes up to a tenth of a second
for a type it has not shown yet, a program's icon is read out of the program,
and the very first ask starts the shell's icon machinery (a third of a
second).  Qt's file icon provider asks on the window's thread, while a row is
drawn: opening a PC folder of programs and new file types froze the window
for up to a second, every time.

Here a worker thread asks and turns the answer into a QImage (which, unlike a
QPixmap, may be made off the window's thread); the window's thread only wraps
it in a QIcon.  A row shows its own glyph until its icon is in.  The newest
folder's icons are looked up first, and one lookup that never ends (a network
folder gone offline) does not hold up the rest: a new worker takes over.
"""

from __future__ import annotations

import collections
import os
import threading
import time
from types import SimpleNamespace
from typing import List, Optional

from PyQt5.QtCore import QCoreApplication, QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon, QImage, QPixmap

# File types whose icon is the file's own (a program, a shortcut, an icon
# file, ...): looked up by path.  Every other file is looked up by its type,
# without touching the file, as Qt and Explorer cache them.
OWN_ICON_TYPES = frozenset((".exe", ".lnk", ".ico", ".url", ".cur", ".ani", ".scr", ".cpl",
                            ".msc", ".appref-ms"))

PATH_ICONS_KEPT = 4000  # icons kept by path (programs, shortcuts, folders)
MOST_WAITING = 5000  # lookups waiting: past this the oldest folders' give way
STUCK_S = 3.0  # one lookup taking this long: a new worker takes over
SETTLE_MS = 40  # icons that come in together are shown together

_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_NORMAL = 0x80
_SHGFI_ICON = 0x100
_SHGFI_LARGEICON = 0x0
_SHGFI_SMALLICON = 0x1
_SHGFI_USEFILEATTRIBUTES = 0x10
_SHGFI_ADDOVERLAYS = 0x20
_DI_MASK = 0x1
_DI_NORMAL = 0x3


def _win32() -> SimpleNamespace:
    """The Windows functions used here, on their own ctypes loaders (the
    argument types set here never meet another module's)."""
    import ctypes
    from ctypes import wintypes

    class SHFILEINFOW(ctypes.Structure):
        _fields_ = [("hIcon", wintypes.HANDLE), ("iIcon", ctypes.c_int),
                    ("dwAttributes", wintypes.DWORD), ("szDisplayName", wintypes.WCHAR * 260),
                    ("szTypeName", wintypes.WCHAR * 80)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HANDLE),
                    ("hbmColor", wintypes.HANDLE)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                    ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    shell32 = ctypes.WinDLL("shell32")
    user32 = ctypes.WinDLL("user32")
    gdi32 = ctypes.WinDLL("gdi32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetFileInfoW.restype = ctypes.c_size_t
    shell32.SHGetFileInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.POINTER(SHFILEINFOW), wintypes.UINT, wintypes.UINT)
    user32.GetIconInfo.restype = wintypes.BOOL
    user32.GetIconInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(ICONINFO))
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.DestroyIcon.argtypes = (wintypes.HANDLE,)
    user32.DrawIconEx.restype = wintypes.BOOL
    user32.DrawIconEx.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HANDLE,
                                  wintypes.UINT)
    gdi32.GetObjectW.restype = ctypes.c_int
    gdi32.GetObjectW.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    gdi32.CreateDIBSection.restype = wintypes.HANDLE
    gdi32.CreateDIBSection.argtypes = (wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER),
                                       wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD)
    gdi32.SelectObject.restype = wintypes.HANDLE
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HANDLE)
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = (wintypes.HANDLE,)
    gdi32.GdiFlush.restype = wintypes.BOOL
    gdi32.GdiFlush.argtypes = ()
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoUninitialize.restype = None
    ole32.CoUninitialize.argtypes = ()
    return SimpleNamespace(ctypes=ctypes, SHFILEINFOW=SHFILEINFOW, ICONINFO=ICONINFO,
                           BITMAP=BITMAP, BITMAPINFOHEADER=BITMAPINFOHEADER, shell32=shell32,
                           user32=user32, gdi32=gdi32, ole32=ole32)


def _icon_size(w32, hicon):
    """``(width, height)`` of *hicon*, or None."""
    ctypes = w32.ctypes
    info = w32.ICONINFO()
    if not w32.user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        source = info.hbmColor or info.hbmMask
        bitmap = w32.BITMAP()
        if not source or not w32.gdi32.GetObjectW(source, ctypes.sizeof(bitmap),
                                                  ctypes.byref(bitmap)):
            return None
        # a colourless icon's mask holds its AND and XOR halves, one above the other
        height = bitmap.bmHeight if info.hbmColor else bitmap.bmHeight // 2
        return bitmap.bmWidth, height
    finally:
        for handle in (info.hbmColor, info.hbmMask):
            if handle:
                w32.gdi32.DeleteObject(handle)


def icon_image(w32, hicon) -> Optional[QImage]:
    """*hicon* drawn as Qt draws it (``qt_pixmapFromWinHICON``): onto a clear
    32-bit bitmap, which leaves premultiplied ARGB; an icon without alpha
    gets it from its mask.  Safe on any thread."""
    ctypes = w32.ctypes
    size = _icon_size(w32, hicon)
    if size is None:
        return None
    width, height = size
    if not (0 < width <= 256 and 0 < height <= 256):
        return None
    count = width * height * 4
    dc = w32.gdi32.CreateCompatibleDC(None)
    if not dc:
        return None
    try:
        header = w32.BITMAPINFOHEADER(ctypes.sizeof(w32.BITMAPINFOHEADER), width, -height, 1, 32,
                                      0, count, 0, 0, 0, 0)  # top-down, BI_RGB
        bits = ctypes.c_void_p()
        dib = w32.gdi32.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not dib or not bits.value:
            return None
        try:
            previous = w32.gdi32.SelectObject(dc, dib)
            try:
                if not w32.user32.DrawIconEx(dc, 0, 0, hicon, width, height, 0, None, _DI_NORMAL):
                    return None
                w32.gdi32.GdiFlush()
                data = bytearray(ctypes.string_at(bits.value, count))
                if not any(data[3::4]):  # no alpha at all: the mask says what shows
                    ctypes.memset(bits.value, 0, count)
                    w32.user32.DrawIconEx(dc, 0, 0, hicon, width, height, 0, None, _DI_MASK)
                    w32.gdi32.GdiFlush()
                    mask = ctypes.string_at(bits.value, count)
                    for i in range(0, count, 4):
                        if mask[i + 2]:  # white in the mask: see-through
                            data[i:i + 4] = b"\0\0\0\0"
                        else:
                            data[i + 3] = 255
            finally:
                w32.gdi32.SelectObject(dc, previous)
        finally:
            w32.gdi32.DeleteObject(dib)
    finally:
        w32.gdi32.DeleteDC(dc)
    raw = bytes(data)
    return QImage(raw, width, height, width * 4, QImage.Format_ARGB32_Premultiplied).copy()


def shell_images(w32, path: str, attributes: int, flags: int) -> List[QImage]:
    """The shell's small and large icon for *path*, as images (none it has
    no icon for).  Safe on any thread that has COM."""
    ctypes = w32.ctypes
    images = []
    for size in (_SHGFI_SMALLICON, _SHGFI_LARGEICON):
        info = w32.SHFILEINFOW()
        if not w32.shell32.SHGetFileInfoW(path, attributes, ctypes.byref(info),
                                          ctypes.sizeof(info), flags | _SHGFI_ICON | size):
            continue
        if not info.hIcon:
            continue
        try:
            image = icon_image(w32, info.hIcon)
        finally:
            w32.user32.DestroyIcon(info.hIcon)
        if image is not None and not image.isNull():
            images.append(image)
    return images


class ShellIcons(QObject):
    """File icons from Windows' shell, looked up on a worker thread.

    The lookups return an icon at once when it is in (a null one when the
    shell has none), else None: the caller shows its glyph, and asks again
    after :attr:`changed`.  Only the window's thread calls them."""

    changed = pyqtSignal()  # icons came in: rows still waiting can ask again
    _arrived = pyqtSignal()

    def __init__(self, parent=None, *, lookup=None):
        super().__init__(parent)
        # *lookup(path, attributes, flags) -> [QImage]*: the tests give their own
        self._w32 = None if lookup is not None else _win32()
        self._lookup = lookup or (lambda path, attributes, flags:
                                  shell_images(self._w32, path, attributes, flags))
        self._lock = threading.Condition()
        self._batches = collections.deque([collections.deque()])  # newest last
        self._waiting = set()  # keys asked for, not in yet
        self._done = []  # (key, images) for the window's thread
        self._worker: Optional[threading.Thread] = None
        self._busy_since: Optional[float] = None
        self._stopped = False
        self._icons = {}  # ("folder",) / ("type", ext) -> QIcon
        self._by_path = collections.OrderedDict()  # ("path", path) -> QIcon, oldest first
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self.changed)
        self._arrived.connect(self._take, Qt.QueuedConnection)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    # ---- lookups (the window's thread) ---------------------------------------
    # *ask=False* only looks: a miss is not looked up
    def folder(self, ask: bool = True) -> Optional[QIcon]:
        """The shell's folder icon."""
        return self._icon(("folder",), "folder", _FILE_ATTRIBUTE_DIRECTORY,
                          _SHGFI_USEFILEATTRIBUTES, ask)

    def type_icon(self, ext: str, ask: bool = True) -> Optional[QIcon]:
        """The icon of files ending in *ext* (a remote file: no file here)."""
        return self._icon(("type", ext), "x" + ext, _FILE_ATTRIBUTE_NORMAL,
                          _SHGFI_USEFILEATTRIBUTES, ask)

    def path_icon(self, path: str, is_dir: bool, ask: bool = True) -> Optional[QIcon]:
        """The icon of *path* on this PC as Explorer shows it: a folder's own
        (Downloads, Desktop), a program's, a shortcut's with its arrow."""
        ext = os.path.splitext(path)[1].lower()
        if not is_dir and ext not in OWN_ICON_TYPES:
            return self.type_icon(ext, ask)
        return self._icon(("path", os.path.normcase(path)), path, 0, _SHGFI_ADDOVERLAYS, ask)

    def new_batch(self) -> None:
        """Lookups asked for from now on go before those still waiting (the
        folder just opened, before the one left)."""
        with self._lock:
            if self._batches[-1]:
                self._batches.append(collections.deque())

    @property
    def stopped(self) -> bool:
        """True once :meth:`stop` was called (the application quit)."""
        return self._stopped

    def stop(self) -> None:
        """No more lookups (the app is quitting)."""
        with self._lock:
            self._stopped = True
            self._batches = collections.deque([collections.deque()])
            self._waiting.clear()
            self._lock.notify_all()

    def _icon(self, key, path, attributes, flags, ask) -> Optional[QIcon]:
        if key[0] == "path":
            icon = self._by_path.get(key)
            if icon is not None:
                self._by_path.move_to_end(key)
        else:
            icon = self._icons.get(key)
        if icon is None and ask:
            self._ask(key, path, attributes, flags)
        return icon

    def _ask(self, key, path, attributes, flags) -> None:
        with self._lock:
            if self._stopped or key in self._waiting:
                return
            self._waiting.add(key)
            self._batches[-1].append((key, path, attributes, flags))
            while len(self._waiting) > MOST_WAITING and len(self._batches) > 1:
                for dropped in self._batches.popleft():  # asked again if still shown
                    self._waiting.discard(dropped[0])
            stuck = self._busy_since is not None and time.monotonic() - self._busy_since > STUCK_S
            if self._worker is None or not self._worker.is_alive() or stuck:
                self._busy_since = None
                self._worker = threading.Thread(target=self._work, name="TurboSSH file icons",
                                                daemon=True)
                self._worker.start()
            self._lock.notify_all()

    # ---- the worker ----------------------------------------------------------
    def _next_locked(self):
        for batch in reversed(self._batches):
            if batch:
                item = batch.popleft()
                break
        else:
            return None
        newest = self._batches[-1]
        self._batches = collections.deque(b for b in self._batches if b or b is newest)
        return item

    def _work(self) -> None:
        me = threading.current_thread()
        com = self._w32 is not None and self._w32.ole32.CoInitializeEx(None, 0x2) in (0, 1)
        try:
            while True:
                with self._lock:
                    item = None
                    while not self._stopped and self._worker is me:
                        item = self._next_locked()
                        if item is not None:
                            break
                        self._lock.wait()
                    if item is None:
                        return
                    self._busy_since = time.monotonic()
                key, path, attributes, flags = item
                try:
                    images = self._lookup(path, attributes, flags)
                except Exception:
                    images = []
                with self._lock:
                    if self._worker is me:
                        self._busy_since = None
                    first = not self._done
                    self._done.append((key, images))
                if first:
                    try:
                        self._arrived.emit()
                    except RuntimeError:  # the app is gone
                        return
        finally:
            if com:
                self._w32.ole32.CoUninitialize()

    # ---- back on the window's thread -----------------------------------------
    def _take(self) -> None:
        with self._lock:
            done, self._done = self._done, []
            for key, _images in done:
                self._waiting.discard(key)
        for key, images in done:
            icon = QIcon()
            for image in images:
                icon.addPixmap(QPixmap.fromImage(image))
            if key[0] == "path":
                self._by_path[key] = icon
                self._by_path.move_to_end(key)
                while len(self._by_path) > PATH_ICONS_KEPT:
                    self._by_path.popitem(last=False)
            else:
                self._icons[key] = icon
        if done and not self._settle.isActive():
            self._settle.start()
