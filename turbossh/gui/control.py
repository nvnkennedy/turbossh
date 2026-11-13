"""TurboSSH as a guest of another application — whatever its UI toolkit.

    turbossh-gui --embed <window handle> --control stdio

``--embed`` puts the whole TurboSSH GUI inside a native window of the host (an
HWND on Windows) and keeps it filling that window. ``--control stdio`` makes it
take commands and send events as JSON lines on stdin / stdout, so the host can
drive it from any language. The protocol is in EMBEDDING.md;
:mod:`turbossh.embed_client` is a ready-made client for Python hosts.

Two processes sharing one window can freeze each other: a window change in here
(focus, activation) can need the host's UI thread to answer, while the host's UI
thread waits for an answer from here. So:

* every request is answered straight away; whatever touches the UI happens
  after the answer has gone (results that come later — ``run``,
  ``confirm_close`` — are answered when ready, by the same id);
* nothing here ever waits on the host; and the embedded window does not notify
  its parent of clicks (WS_EX_NOPARENTNOTIFY), which would be a synchronous
  call into the host's thread.
"""

from __future__ import annotations

import base64
import inspect
import json
import math
import os
import queue
import re
import sys
import threading
import traceback

from PyQt5.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QWindow
from PyQt5.QtWidgets import QApplication, QWidget

from .. import __version__
from . import embed as embed_mod
from . import qtutil
from . import theme as theme_mod
from .embed import INHERIT_THEME, EmbeddedSession, TurboSSHWidget
from .options import PARTS

PROTOCOL = 1
_FOLLOW_MS = 50                 # how often the host's window size is checked
_BACKLOG = 5000                 # unsent messages before log events are dropped
_LATER = object()               # a command that answers by itself, later
_CHROME = tuple(p for p in PARTS if p != "home")
_QUIT_WAIT = 6.0                # s for remote ports / cameras to be released at the end
_MAX_WAIT = 60.0                # the most a host may ask for that
_MAX_RUN = 86400.0              # the longest timeout a `run` may have
_MAX_PIXELS = 1_000_000         # beyond any screen: set_geometry's bound
# what a host may still ask once TurboSSH has shut down; the rest is refused (it
# used to be answered ok, then silently dropped)
_AFTER_END = frozenset(("ping", "shutdown", "sessions", "current_session",
                        "saved_session_names", "camera_enabled", "current_theme",
                        "action_names", "window"))


class ProtocolError(Exception):
    """A request that cannot be served (answered with ``ok: false``)."""


def _shown(value) -> str:
    text = json.dumps(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _flag(name: str, value) -> bool:
    """A real JSON true / false. bool() turned "false" into True: the Camera came
    on, and "ignore_host_key": "false" would have accepted any host key."""
    if not isinstance(value, bool):
        raise ProtocolError(f"{name} must be true or false, not {_shown(value)}")
    return value


_WHOLE_TEXT = re.compile(r"\s*[+-]?\d+\s*\Z")
_NUMBER_TEXT = re.compile(r"\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*\Z")


def _numeric(value, whole: bool = False):
    """*value* as a finite number, or None. A JSON number, or a string holding
    one: 2.0 took ``"port": "22"``, ``"baudrate": "115200"`` and ``"timeout":
    "30"`` (as Python and paramiko do), and hosts send them. Never ``true``,
    NaN or an infinity, nor "inf" / "nan" spelled out; *whole*: no fraction
    (22.0 is 22, 22.5 is refused)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not (_WHOLE_TEXT if whole else _NUMBER_TEXT).match(value):
            return None
        value = int(value) if whole else float(value)
    if isinstance(value, float):
        if not math.isfinite(value) or (whole and not value.is_integer()):
            return None
        return int(value) if whole else value
    return value if isinstance(value, int) else None


def _number(name: str, value, low: float, high: float, whole: bool = False):
    """A finite number from *low* to *high* (see :func:`_numeric`). JSON also
    brings Infinity, NaN and 1e999: a wait of Infinity overflowed the join and
    the process never ended."""
    number = _numeric(value, whole)
    if number is None or not low <= number <= high:
        kind = "a whole number" if whole else "a number"
        raise ProtocolError(f"{name} must be {kind} from {low:g} to {high:g}, "
                            f"not {_shown(value)}")
    return number


def _wait(value) -> float:
    """shutdown's ``wait``: seconds from 0 to 60, a longer one counting as 60 and
    a negative one as 0 (2.0 took any number). Refusing a long one made
    embed_client's close() kill the process after its timeout: remote COM ports
    and cameras stayed taken. Only what is no number at all is refused."""
    if value is None:
        return _QUIT_WAIT
    if isinstance(value, float) and math.isinf(value):
        number = value                   # 1e999: "a long time", i.e. the most
    else:
        number = _numeric(value)
    if number is None:
        raise ProtocolError(f"wait must be a number of seconds from 0 to "
                            f"{_MAX_WAIT:g} (more counts as {_MAX_WAIT:g}), "
                            f"not {_shown(value)}")
    return float(min(max(number, 0.0), _MAX_WAIT))


_OPEN_FLAGS = ("legacy_algorithms", "ignore_host_key")
_OPEN_PORTS = ("port", "jump_port")
_OPEN_TEXT = ("host", "device", "username", "password", "domain", "key_file", "name",
              "jump_host", "jump_username", "jump_password", "jump_domain")
# optional arguments for which null means "the default", as it did in 2.0
_OPEN_DEFAULTED = _OPEN_FLAGS + _OPEN_PORTS + ("baudrate", "quick_commands")


def _open_args(options: dict) -> dict:
    """The arguments of an open request, checked by type (the opener checks the
    names) and in the form the opener takes: a whole number given as a string
    becomes that number, and null for an optional flag, port, baud rate or
    ``quick_commands`` leaves the default (2.0 took both)."""
    clean = {}
    for key, value in options.items():
        if value is None and key in _OPEN_DEFAULTED:
            continue
        if key in _OPEN_FLAGS:
            value = _flag(key, value)
        elif key in _OPEN_PORTS:
            value = _number(key, value, 1, 65535, whole=True)
        elif key == "baudrate":
            value = _number(key, value, 1, 100_000_000, whole=True)
        elif key == "quick_commands":
            if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
                raise ProtocolError("quick_commands must be a list of strings")
        elif key in _OPEN_TEXT and value is not None and not isinstance(value, str):
            raise ProtocolError(f"{key} must be a string, not {_shown(value)}")
        clean[key] = value
    return clean


# --------------------------------------------------------------------------- #
# The native side: living inside someone else's window
# --------------------------------------------------------------------------- #
class _NoNative:
    """Platforms without the Win32 calls: Qt still reparents, the host sizes us."""
    follows = False

    def alive(self, handle):
        return True

    def client_size(self, handle):
        return None

    def keyboard_inside(self, own):
        return True

    def take_keyboard(self, own):
        pass

    def share_input(self, handle):
        pass

    def quiet_parent(self, own):
        pass


class _Win32:
    follows = True

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetFocus.argtypes = []
        user32.GetFocus.restype = wintypes.HWND
        user32.SetFocus.argtypes = [wintypes.HWND]
        user32.SetFocus.restype = wintypes.HWND
        user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
        user32.IsChild.restype = wintypes.BOOL
        get = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        put = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        get.argtypes = [wintypes.HWND, ctypes.c_int]
        get.restype = ctypes.c_ssize_t
        put.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        put.restype = ctypes.c_ssize_t
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._u, self._get, self._put = user32, get, put
        self._k = kernel32
        self._rect = wintypes.RECT

    def alive(self, handle):
        return bool(self._u.IsWindow(handle))

    def client_size(self, handle):
        rect = self._rect()
        if not self._u.GetClientRect(handle, self._ct.byref(rect)):
            return None
        return rect.right - rect.left, rect.bottom - rect.top

    def keyboard_inside(self, own):
        focus = self._u.GetFocus()
        return bool(focus) and (focus == own or bool(self._u.IsChild(own, focus)))

    def take_keyboard(self, own):
        self._u.SetFocus(own)

    def share_input(self, handle):
        """Join our UI thread's input state to the host window's thread.

        The keyboard goes to the focus window of the foreground thread's input
        state, and SetFocus only works on windows that share the caller's: with
        separate states a click left the keys with the host (SetFocus failed
        with ERROR_INVALID_PARAMETER). Windows sometimes links the two by itself
        for a cross-process parent and child, but not reliably; this is what it
        would do. Repeating it is harmless; it ends with this process."""
        theirs = self._u.GetWindowThreadProcessId(handle, None)
        mine = self._k.GetCurrentThreadId()
        if theirs and theirs != mine:
            self._u.AttachThreadInput(mine, theirs, True)

    def quiet_parent(self, own):
        GWL_EXSTYLE, WS_EX_NOPARENTNOTIFY = -20, 0x00000004
        self._put(own, GWL_EXSTYLE, self._get(own, GWL_EXSTYLE) | WS_EX_NOPARENTNOTIFY)


def _native():
    return _Win32() if os.name == "nt" else _NoNative()


def match_dpi_awareness(handle: int, qt_args: list) -> list:
    """Qt arguments that make this process as DPI-aware as the host's window.

    Windows translates coordinates between windows of different DPI awareness,
    and a child that does not match its parent ends up the wrong size (an
    unaware host — Tk, an old WinForms app — saw TurboSSH 25 % too big at 125 %
    scaling). Matching it puts both sides in the same coordinates; TurboSSH then
    renders exactly as the host does."""
    if os.name != "nt" or os.environ.get("QT_QPA_PLATFORM") or \
            any(a.startswith("-platform") for a in qt_args):
        return list(qt_args)            # someone chose the platform already
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        user32.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
        user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        context = user32.GetWindowDpiAwarenessContext(handle)
        awareness = user32.GetAwarenessFromDpiAwarenessContext(context) if context else -1
    except (OSError, AttributeError):   # before Windows 10 1607: Qt's default
        return list(qt_args)
    if awareness not in (0, 1, 2):      # unaware, system aware, per monitor
        return list(qt_args)
    return list(qt_args) + ["-platform", f"windows:dpiawareness={awareness}"]


class NativeParent(QObject):
    """Puts a top-level widget inside a native window of another process, keeps
    it filling that window (or where the host puts it), and gets it the keyboard
    when it is clicked."""

    gone = pyqtSignal()              # the host's window no longer exists

    def __init__(self, widget: QWidget, handle: int):
        super().__init__(widget)
        self.widget = widget
        self.handle = handle
        self._native = _native()
        self.follow = self._native.follows
        self._manual = None          # (x, y, w, h) the host asked for, in pixels
        self._placed = None          # the geometry last applied
        self._gone = False
        self._foreign = QWindow.fromWinId(handle)
        widget.setWindowFlags(Qt.FramelessWindowHint)
        own = int(widget.winId())    # create the native window, then re-home it
        widget.windowHandle().setParent(self._foreign)
        self._native.quiet_parent(own)
        self._native.share_input(handle)
        widget.windowHandle().installEventFilter(self)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._tick()
        widget.show()
        self._timer.start(_FOLLOW_MS)

    @property
    def own(self) -> int:
        return int(self.widget.winId())

    def _scale(self):
        return self.widget.devicePixelRatioF() or 1.0

    def _tick(self):
        if self._gone:
            return
        if not self._native.alive(self.handle):
            self._gone = True
            self._timer.stop()
            self.gone.emit()
            return
        if self._manual is not None:
            x, y, width, height = self._manual
        elif self.follow:
            size = self._native.client_size(self.handle)
            if size is None:
                return
            x, y, (width, height) = 0, 0, size
        else:
            return
        dpr = self._scale()
        want = (round(x / dpr), round(y / dpr), max(1, round(width / dpr)),
                max(1, round(height / dpr)))
        # only when the target moved: re-applying whenever Qt's idea differs
        # would fight the system for ever if it ever adjusted the result
        if want != self._placed:
            self._placed = want
            self.widget.setGeometry(*want)

    def set_geometry(self, x, y, width, height):
        """Stop filling the host's window; sit at this rectangle (pixels, relative
        to the host window's client area) until fill_parent()."""
        self._manual = (int(x), int(y), int(width), int(height))
        self._tick()

    def fill_parent(self):
        self._manual = None
        self.follow = self._native.follows
        self._tick()

    def take_keyboard(self):
        self._native.share_input(self.handle)
        self._native.take_keyboard(self.own)

    def eventFilter(self, obj, event):
        # A click in a child window does not move the keyboard to it by itself.
        if event.type() == QEvent.MouseButtonPress and \
                not self._native.keyboard_inside(self.own):
            self.take_keyboard()
        return False


# --------------------------------------------------------------------------- #
# The control channel: JSON lines on stdin / stdout
# --------------------------------------------------------------------------- #
def _std_stream(fd, mode):
    """A binary stream on the inherited stdin / stdout. A windowed exe starts
    with sys.stdin / sys.stdout set to None even when its parent passed pipes,
    so fall back to the OS handles."""
    try:
        os.fstat(fd)
        return os.fdopen(os.dup(fd), mode)
    except OSError:
        if os.name != "nt":
            raise
    import ctypes
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel32.GetStdHandle(-10 if fd == 0 else -11)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError(f"no standard {'input' if fd == 0 else 'output'} to talk on")
    flags = (os.O_RDONLY if fd == 0 else os.O_WRONLY) | getattr(os, "O_BINARY", 0)
    return os.fdopen(msvcrt.open_osfhandle(handle, flags), mode)


def claim_stdio():
    """The protocol's streams. Standard output is then pointed at standard error
    (or nowhere), so a stray print() — ours or a library's — can never corrupt
    the channel."""
    proto_in = _std_stream(0, "rb")
    proto_out = _std_stream(1, "wb")
    try:
        os.dup2(sys.stderr.fileno(), 1)
    except Exception:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.close(devnull)
        except OSError:
            pass
    sys.stdout = sys.stderr if sys.stderr is not None else open(os.devnull, "w")
    return proto_in, proto_out


def end_process(widget: TurboSSHWidget, wait: float = _QUIT_WAIT) -> None:
    """Close every session, give remote ports and cameras *wait* seconds to be
    released, and quit. Quitting then adds no wait of its own: it used to block
    another 6 s after the host's own wait, with the window unpainted."""
    try:
        widget.hide()                # not a frozen, unpainted window while it waits
    except RuntimeError:
        pass
    widget.shutdown(wait=wait)
    embed_mod._quit_wait_done()
    QApplication.instance().quit()


class _Opening:
    """The id announced for the next session that opens while this is the
    innermost request running: an open request's own id, or None (a fresh id)."""
    __slots__ = ("sid",)

    def __init__(self, sid):
        self.sid = sid


class _Queued:
    """UI work queued for an answered request, and what answers for it if
    TurboSSH ends first (``on_skip``, or None when nothing is owed)."""
    __slots__ = ("on_skip",)

    def __init__(self, on_skip):
        self.on_skip = on_skip


class ControlBridge(QObject):
    """Serves one host over a pair of byte streams (one JSON object per line)."""

    _line = pyqtSignal(bytes)        # from the reader thread
    host_gone = pyqtSignal()         # the host closed its end (or went away)

    def __init__(self, widget: TurboSSHWidget, out, native: NativeParent = None):
        super().__init__()
        self.widget = widget
        self.native = native
        self._out = out
        # Written by a thread of its own: a host that stops reading must not
        # freeze this process's UI on a full pipe. Log lines are dropped first.
        self._outbox: queue.Queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, name="turbossh-out",
                                        daemon=True)
        self._writer.start()
        self._broken = False
        self._finished = False
        self._ids = {}               # EmbeddedSession -> id
        self._by_id = {}             # id -> EmbeddedSession
        self._count = 0
        # The requests running now, innermost last. Not one "pending id" slot: an
        # open that asks a question ("Serial port in use") runs the next requests
        # in the question's event loop, and those took or cleared that slot — the
        # session came out under another id, or its id never heard a word.
        self._frames: list = []
        self._queued: list = []      # _Queued: work of answered requests not run yet
        self._running: dict = {}     # token -> id of a `run` not answered yet
        self._jobs: set = set()
        self._line.connect(self._on_line)
        widget.session_opened.connect(self._on_session_opened)
        widget.current_session_changed.connect(
            lambda s: self.send_event("current_session_changed", session=self._ids.get(s)))
        widget.log_message.connect(lambda text: self.send_event("log", text=text))
        widget.theme_changed.connect(lambda name: self.send_event("theme_changed", theme=name))
        widget.camera_enabled_changed.connect(
            lambda on: self.send_event("camera_enabled_changed", enabled=on))

    # ---- wire ----
    def start(self, stream) -> None:
        """Read requests on a thread of their own until the host hangs up."""
        def pump():
            try:
                for raw in iter(stream.readline, b""):
                    if raw.strip():
                        self._line.emit(raw)
            except (OSError, ValueError):
                pass
            self.host_gone.emit()
        threading.Thread(target=pump, name="turbossh-control", daemon=True).start()

    def write(self, message: dict) -> None:
        if self._broken:
            return
        if message.get("event") == "log" and self._outbox.qsize() > _BACKLOG:
            return                       # the host is not keeping up: lose chatter only
        self._outbox.put((json.dumps(message, ensure_ascii=False, default=str)
                          + "\n").encode("utf-8"))

    def _write_loop(self) -> None:
        while True:
            data = self._outbox.get()
            try:
                if data is None:
                    return
                if self._broken:
                    continue
                try:
                    self._out.write(data)
                    self._out.flush()
                except (OSError, ValueError):
                    self._broken = True
                    self.host_gone.emit()    # nobody is listening any more
            finally:
                self._outbox.task_done()

    def flush(self, timeout: float = 3.0) -> None:
        """Deliver what is queued (the last answers, ``exited``) before the end."""
        self._outbox.put(None)
        self._writer.join(timeout)

    def send_event(self, event_name: str, /, **fields) -> None:
        self.write({"event": event_name, **fields})

    def reply(self, rid, result=None, error: BaseException = None) -> None:
        if error is None:
            self.write({"id": rid, "ok": True, "result": result})
        else:
            message = str(error) if isinstance(error, ProtocolError) \
                else f"{type(error).__name__}: {error}"
            self.write({"id": rid, "ok": False, "error": message,
                        "type": type(error).__name__})

    # ---- dispatch ----
    def _on_line(self, raw: bytes) -> None:
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            # RecursionError: nested a few thousand levels deep, which escaped
            # this handler and left the request without an answer
            self.reply(None, error=ProtocolError(f"not a JSON object: {exc}"))
            return
        if not isinstance(request, dict):
            self.reply(None, error=ProtocolError("a request is a JSON object"))
            return
        rid, name = request.get("id"), request.get("cmd")
        args = request.get("args") or {}
        handler = getattr(self, f"cmd_{name}", None) if isinstance(name, str) \
            and name.isidentifier() else None
        if handler is None:
            self.reply(rid, error=ProtocolError(f"unknown command {name!r}"))
            return
        if not isinstance(args, dict):
            self.reply(rid, error=ProtocolError("args must be a JSON object"))
            return
        if self.ended and name not in _AFTER_END:
            self.reply(rid, error=ProtocolError(f"{name}: TurboSSH has shut down"))
            return
        try:
            inspect.signature(handler).bind(rid, **args)
        except TypeError as exc:
            self.reply(rid, error=ProtocolError(f"{name}: {exc}"))
            return
        try:
            result = handler(rid, **args)
        except Exception as exc:          # noqa: BLE001 - reported to the host
            self.reply(rid, error=exc)
            return
        if result is not _LATER:
            self.reply(rid, result)

    @property
    def ended(self) -> bool:
        """Shut down: by a request, the host going away, or our window closing."""
        return self._finished or self.widget.is_shut_down

    def _later(self, fn, *args, on_skip=None) -> None:
        """UI work, after the answer has gone (see the module docstring). If
        TurboSSH has shut down by then, ``on_skip`` answers for it instead: when
        the work comes up, or when :meth:`finish` runs first (a timer still
        waiting when the application quits never fires)."""
        entry = _Queued(on_skip)
        self._queued.append(entry)

        def run():
            if not self._take(entry):
                return                   # finish() has answered for it already
            if self.ended:
                if on_skip is not None:
                    on_skip()
                return
            frame = _Opening(None)       # a session this opens gets an id of its own
            self._frames.append(frame)
            try:
                fn(*args)
            except Exception as exc:      # noqa: BLE001
                self.send_event("error", text=f"{type(exc).__name__}: {exc}")
            finally:
                self._drop(frame)
        QTimer.singleShot(0, run)

    def _drop(self, frame) -> None:
        for i in range(len(self._frames) - 1, -1, -1):
            if self._frames[i] is frame:
                del self._frames[i]
                return

    def _take(self, entry) -> bool:
        """Remove *entry* from the work still queued; False if it was not there."""
        for i, queued in enumerate(self._queued):
            if queued is entry:
                del self._queued[i]
                return True
        return False

    def _answer_for_the_rest(self) -> None:
        """The end has come before the work queued for answered requests: say
        what became of each now (an open's id gets session_failed and
        session_closed, confirm_close an answer), and answer every ``run``
        still under way. Once the application quits, queued timers never fire,
        and a parked job's result is dropped: those ids never heard back."""
        queued, self._queued = self._queued, []
        for entry in queued:
            if entry.on_skip is not None:
                try:
                    entry.on_skip()
                except Exception as exc:  # noqa: BLE001 - the rest still answer
                    self.send_event("error", text=f"{type(exc).__name__}: {exc}")
        running, self._running = self._running, {}
        for rid in running.values():
            self.reply(rid, error=ProtocolError(
                "run: TurboSSH has shut down before the command ended"))

    # ---- sessions ----
    def _new_id(self) -> str:
        self._count += 1
        return f"s{self._count}"

    def _session(self, sid) -> EmbeddedSession:
        session = self._by_id.get(sid)
        if session is None:
            raise ProtocolError(f"no open session {sid!r}")
        return session

    @staticmethod
    def _describe(sid, session):
        return {"session": sid, "name": session.name, "kind": session.kind,
                "state": session.state}

    def _on_session_opened(self, session: EmbeddedSession) -> None:
        # The innermost running request decides: an open request's own page is
        # announced while it is innermost (its question, if any, comes before the
        # page exists); anything opened by other requests run inside that
        # question's event loop is innermost there itself.
        frame = self._frames[-1] if self._frames else None
        if frame is not None and frame.sid is not None:
            sid, frame.sid = frame.sid, None
        else:
            sid = self._new_id()
        self._ids[session], self._by_id[sid] = sid, session
        session.connected.connect(lambda: self.send_event("session_connected", session=sid))
        session.disconnected.connect(
            lambda message: self.send_event("session_disconnected", session=sid, message=message))
        session.failed.connect(
            lambda message: self.send_event("session_failed", session=sid, message=message))
        session.closed.connect(lambda: self._on_session_closed(sid, session))
        self.send_event("session_opened", **self._describe(sid, session))

    def _on_session_closed(self, sid, session) -> None:
        self._ids.pop(session, None)
        self._by_id.pop(sid, None)
        self.send_event("session_closed", session=sid)

    def _open(self, opener, *args, **kwargs) -> dict:
        """Answer with the new session's id now; open it right after."""
        inspect.signature(opener).bind(*args, **kwargs)   # bad arguments: now
        sid = self._new_id()

        def run():
            frame = _Opening(sid)        # what this opens is announced as sid
            self._frames.append(frame)
            reason = "not opened (the user chose not to take over the port)"
            try:
                opener(*args, **kwargs)
            except Exception as exc:      # noqa: BLE001 - reported as an event
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                self._drop(frame)
            if frame.sid is not None:    # nothing was opened under it
                self._unopened(sid, reason)
        self._later(run, on_skip=lambda: self._unopened(sid, "TurboSSH has shut down"))
        return {"session": sid}

    def _unopened(self, sid, reason) -> None:
        """An id that was answered but ended up with no session: the host hears
        the end of it, as for any other session."""
        self.send_event("session_failed", session=sid, message=reason)
        self.send_event("session_closed", session=sid)

    # ---- commands: opening ----
    def cmd_connect_ssh(self, rid, host, **options):
        options = _open_args(dict(options, host=host))
        return self._open(self.widget.connect_ssh, options.pop("host"), **options)

    def cmd_open_serial(self, rid, device, **options):
        options = _open_args(dict(options, device=device))
        return self._open(self.widget.open_serial, options.pop("device"), **options)

    def cmd_open_serial_over_ssh(self, rid, device, host, **options):
        options = _open_args(dict(options, device=device, host=host))
        return self._open(self.widget.open_serial_over_ssh, options.pop("device"),
                          options.pop("host"), **options)

    def cmd_open_saved(self, rid, name):
        if name not in self.widget.saved_session_names():
            raise ProtocolError(f"no saved session named {name!r}")
        return self._open(self.widget.open_saved, name)

    def cmd_open_local_terminal(self, rid):
        return self._open(self.widget.open_local_terminal)

    def cmd_open_camera(self, rid):
        return self._open(self.widget.open_camera)

    def cmd_open_home(self, rid):
        self._later(self.widget.open_home)
        return {}

    def cmd_new_session_dialog(self, rid):
        self._later(self.widget.new_session_dialog)
        return {}

    def cmd_show_settings(self, rid):
        self._later(self.widget.show_settings)
        return {}

    # ---- commands: sessions ----
    def cmd_sessions(self, rid):
        return [self._describe(sid, s) for sid, s in self._by_id.items()]

    def cmd_current_session(self, rid):
        return {"session": self._ids.get(self.widget.current_session())}

    def cmd_saved_session_names(self, rid):
        return self.widget.saved_session_names()

    def cmd_send(self, rid, session, text=None, data_base64=None):
        if (text is None) == (data_base64 is None):
            raise ProtocolError("send needs exactly one of text or data_base64")
        data = text if text is not None else base64.b64decode(data_base64)
        self._session(session).send(data)
        return {}

    def cmd_send_line(self, rid, session, text):
        self._session(session).send_line(text)
        return {}

    def cmd_screen_text(self, rid, session):
        return {"text": self._session(session).screen_text()}

    def cmd_activate(self, rid, session):
        target = self._session(session)

        def run():
            self._take_keyboard()
            target.activate()
        self._later(run)
        return {}

    def cmd_close_session(self, rid, session):
        self._later(self._session(session).close)
        return {}

    def cmd_run(self, rid, session, command, timeout=60.0):
        """Run a command over the session's connection (its own channel: the
        terminal is not touched) and answer with its output when it ends.
        ``timeout`` 0 is no limit, as in the library (and 2.0); null is 60 s."""
        timeout = 60.0 if timeout is None else _number("timeout", timeout, 0, _MAX_RUN)
        handler = self._session(session).handler
        if handler is None or not hasattr(handler, "run"):
            raise ProtocolError(f"session {session!r} has no SSH connection to run on")
        token = object()
        self._running[token] = rid       # answered by finish() if it comes first

        def done(result):
            if self._running.pop(token, None) is None:
                return
            if not getattr(result, "success", True):
                self.reply(rid, error=result.error or ProtocolError("the command failed"))
                return
            value = getattr(result, "value", result)
            self.reply(rid, {"exit_code": value.exit_code, "stdout": value.stdout,
                             "stderr": value.stderr, "duration": value.duration})

        def fail(text):
            if self._running.pop(token, None) is not None:
                self.reply(rid, error=ProtocolError(text))
        qtutil.run_job(self._jobs, lambda: handler.run(command, timeout=float(timeout)),
                       on_done=done, on_fail=fail)
        return _LATER

    # ---- commands: the window ----
    def cmd_camera_enabled(self, rid):
        return self.widget.camera_enabled()

    def cmd_set_camera_enabled(self, rid, enabled):
        self._later(self.widget.set_camera_enabled, _flag("enabled", enabled))
        return {}

    def cmd_current_theme(self, rid):
        return self.widget.current_theme()

    def cmd_set_theme(self, rid, name):
        if name != INHERIT_THEME and name not in theme_mod.THEME_NAMES:
            raise ProtocolError(f"unknown theme {name!r}; one of "
                                f"{theme_mod.THEME_NAMES} or {INHERIT_THEME!r}")
        self._later(self.widget.set_theme, name)
        return {}

    def cmd_set_chrome(self, rid, **parts):
        unknown = sorted(set(parts) - set(_CHROME))
        if unknown:
            raise ProtocolError(f"unknown part(s) {unknown}; choose from {list(_CHROME)}")
        # null leaves a part as it is (the Python API's None)
        parts = {k: _flag(k, v) for k, v in parts.items() if v is not None}
        self._later(lambda: self.widget.set_chrome(**parts))
        return {}

    def cmd_action_names(self, rid):
        return self.widget.action_names()

    def cmd_trigger_action(self, rid, name):
        action = self.widget.action(name)
        self._later(action.trigger)
        return {}

    def cmd_focus(self, rid):
        def run():
            self._take_keyboard()
            self.widget.setFocus()
        self._later(run)
        return {}

    def _take_keyboard(self):
        if self.native is not None:
            self.native.take_keyboard()
        else:
            self.widget.activateWindow()

    def cmd_set_geometry(self, rid, x, y, width, height):
        self._need_embedding()
        x, y = (_number(n, v, -_MAX_PIXELS, _MAX_PIXELS) for n, v in (("x", x), ("y", y)))
        width, height = (_number(n, v, 0, _MAX_PIXELS)
                         for n, v in (("width", width), ("height", height)))
        self._later(self.native.set_geometry, x, y, width, height)
        return {}

    def cmd_fill_parent(self, rid):
        self._need_embedding()
        self._later(self.native.fill_parent)
        return {}

    def _need_embedding(self):
        if self.native is None:
            raise ProtocolError("only when started with --embed")

    def cmd_set_visible(self, rid, visible):
        self._later(self.widget.setVisible, _flag("visible", visible))
        return {}

    def cmd_window(self, rid):
        """Where TurboSSH is, as it sees it (for a host chasing a layout problem)."""
        g = self.widget.geometry()
        info = {"window": int(self.widget.winId()),
                "embedded_in": self.native.handle if self.native else None,
                "geometry": [g.x(), g.y(), g.width(), g.height()],
                "device_pixel_ratio": self.widget.devicePixelRatioF(),
                "follows_parent": bool(self.native and self.native.follow
                                       and self.native._manual is None)}
        if self.native is not None:
            info["parent_client_size"] = self.native._native.client_size(self.native.handle)
        return info

    def cmd_ping(self, rid):
        return {"version": __version__, "protocol": PROTOCOL, "pid": os.getpid()}

    def cmd_confirm_close(self, rid):
        self._later(lambda: self.reply(rid, self.widget.confirm_close()),
                    on_skip=lambda: self.reply(rid, error=ProtocolError(
                        "confirm_close: TurboSSH has shut down")))
        return _LATER

    def cmd_shutdown(self, rid, wait=_QUIT_WAIT):
        self._later(self.finish, _wait(wait))
        return {}

    # ---- the end ----
    def finish(self, wait: float = _QUIT_WAIT) -> None:
        """Close everything, say so, and end the process — once, whoever asks
        first: the host's shutdown, its hang-up (noticed by the reader and the
        writer both), its window going away, or our own window being closed.
        Every request answered before is accounted for first (the writer
        delivers that while the sessions close, and is flushed at the end)."""
        if self._finished:
            return
        self._finished = True
        self._answer_for_the_rest()
        qtutil.close_jobs(self._jobs)
        end_process(self.widget, wait)

    def _say_exited(self) -> None:
        self.send_event("exited")


class _HostedWidget(TurboSSHWidget):
    """TurboSSH's whole window when it is a process of its own for a host.

    As a window of its own (``--control`` without ``--embed``), closing it asks
    about open sessions first, like the desktop app. Either way, closing it ends
    the process (``on_close``): embedded, a WM_CLOSE sent to it used to shut the
    sessions down and leave the process running, empty, answering requests it
    then dropped."""
    in_native_parent = False
    on_close = None                      # ends the process (ControlBridge.finish)

    def closeEvent(self, event):
        if not self.in_native_parent and not self.is_shut_down \
                and not self.confirm_close():
            event.ignore()
            return
        super().closeEvent(event)        # shuts the sessions down
        if self.on_close is not None:
            QTimer.singleShot(0, self.on_close)


def _install_error_hook(bridge_ref: list) -> None:
    """Our own process: an uncaught error is logged (crash.log) and reported to the
    host as an ``error`` event; it never ends the process."""
    from .app import _crash_log

    def report(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        _crash_log(text)
        bridge = bridge_ref[0] if bridge_ref else None
        if bridge is not None:
            bridge.send_event("error", text=text.rstrip())
    sys.excepthook = report
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: report(a.exc_type, a.exc_value, a.exc_traceback)


def run_hosted(args) -> int:
    """``turbossh-gui --embed … / --control …``: TurboSSH for another application."""
    if args.embed and not _native().alive(args.embed):
        sys.stderr.write(f"turbossh-gui: no window with handle {args.embed} "
                         f"to embed into\n") if sys.stderr else None
        return 2
    proto_in = proto_out = None
    if args.control == "stdio":
        proto_in, proto_out = claim_stdio()
    qt_args = match_dpi_awareness(args.embed, args.qt_args) if args.embed else args.qt_args
    app = QApplication.instance() or QApplication([sys.argv[0]] + list(qt_args))
    widget, bridge = build_hosted(args, app, proto_out)
    if bridge is not None:
        bridge.start(proto_in)
    code = app.exec_()
    if bridge is not None:
        bridge.flush()
    return code


def build_hosted(args, app, out=None):
    """Everything run_hosted sets up between the QApplication and its event loop:
    ``(widget, bridge)``, the bridge (``None`` without *out*) having sent
    ``ready``. Its reader is not started."""
    app.setApplicationName("TurboSSH")
    # Hosted, only an explicit end quits: the host's shutdown or hang-up, its
    # window going away, or our own window closed. Qt's "last window closed" rule
    # quit (closing every session) after set_visible(false) as soon as any of our
    # dialogs closed — a question, Settings, confirm_close.
    app.setQuitOnLastWindowClosed(False)
    try:
        icon = theme_mod.app_icon(args.theme if args.theme in theme_mod.THEME_NAMES
                                  else theme_mod.current_name())
        if icon is not None and not icon.isNull():
            app.setWindowIcon(icon)  # our own dialogs; the host's window keeps its icon
    except Exception:
        pass
    bridge_ref: list = []
    _install_error_hook(bridge_ref)

    hidden = set(args.hide or ())
    camera = None if args.camera is None else args.camera == "on"
    widget = _HostedWidget(**{part: part not in hidden for part in PARTS},
                           camera=camera, theme=args.theme)
    native = None
    if args.embed:
        widget.in_native_parent = True
        native = NativeParent(widget, args.embed)
    else:
        widget.setWindowTitle("TurboSSH")
        widget.resize(1200, 780)
        widget.show()

    bridge = None
    if out is not None:
        bridge = ControlBridge(widget, out, native)
        bridge_ref.append(bridge)
        bridge.host_gone.connect(bridge.finish)
        app.aboutToQuit.connect(bridge._say_exited)
        widget.on_close = bridge.finish
    else:
        ended = []

        def end(wait=_QUIT_WAIT):
            if not ended:
                ended.append(True)
                end_process(widget, wait)
        widget.on_close = end
    if native is not None:
        def parent_closed():
            if bridge is not None and not bridge.ended:
                bridge.send_event("parent_closed")
            widget.on_close()
        native.gone.connect(parent_closed)
    if bridge is not None:
        bridge.send_event("ready", protocol=PROTOCOL, version=__version__, pid=os.getpid(),
                          window=int(widget.winId()),
                          embedded_in=args.embed or None)
    return widget, bridge
