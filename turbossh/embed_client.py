"""Put the TurboSSH GUI inside any application — Tk, wxPython, PySide6, PyQt6,
a test framework's window — and drive it. No Qt needed on this side.

    from turbossh.embed_client import TurboSSHProcess

    gui = TurboSSHProcess.launch(parent=frame.winfo_id())   # fills that window
    session = gui.connect_ssh("10.0.0.5", username="root", password="…")
    gui.wait_for("session_connected", session)
    gui.send_line(session, "uname -a")
    …
    gui.close()

TurboSSH runs as a process of its own (``python -m turbossh.gui``, or the
bundled exe where this Python has no PyQt5) started with ``--embed`` and
``--control stdio``; this module speaks the JSON-lines protocol described in
EMBEDDING.md. Hosts in other languages speak the same protocol directly.

Calls block until TurboSSH answers, which it does at once (see control.py). On
Windows a waiting call keeps answering the window messages the embedded window
sends to this thread, so calling from your UI thread cannot deadlock the two.
Events arrive on a background thread: use ``on_event`` callbacks (marshal to
your UI thread yourself), or ``poll_events()`` from a UI timer.

Two threads read for you. One reads TurboSSH's output and does nothing else: it
hands every answer to the call waiting for it. The other runs your ``on_event``
and ``call_async`` callbacks, one at a time, in the order things arrived — so a
callback may itself ``call()`` or ``wait_for()``: the answer is read meanwhile.
(Both used to run on the one reader, so a call from a callback waited for an
answer only it could read, for the whole timeout.)
"""

from __future__ import annotations

import base64
import collections
import importlib.util
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Union

PROTOCOL = 1

__all__ = ["TurboSSHProcess", "TurboSSHError", "gui_command", "PROTOCOL"]


class TurboSSHError(RuntimeError):
    """TurboSSH refused or failed a request, did not answer, or is not running.
    ``type`` is the name of the error on TurboSSH's side, when there was one."""

    def __init__(self, message: str, type: str = ""):
        super().__init__(message)
        self.type = type


def gui_command() -> List[str]:
    """How to start the TurboSSH GUI from here: this Python when it has PyQt5,
    otherwise the Windows exe bundled in the wheel."""
    if not getattr(sys, "frozen", False) and importlib.util.find_spec("PyQt5"):
        return [sys.executable, "-m", "turbossh.gui"]
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin",
                       "turbossh-gui.exe")
    if os.path.exists(exe):
        return [exe]
    raise TurboSSHError('the TurboSSH GUI needs PyQt5 (pip install "turbossh[gui]") '
                        "or the bundled Windows exe")


# --------------------------------------------------------------------------- #
# Waiting without freezing a window that shares our input queue
# --------------------------------------------------------------------------- #
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32")
    _user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                     wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _PM_QS_SENDMESSAGE = 0x0040 << 16          # PM_NOREMOVE (0) | QS_SENDMESSAGE

    def _pump() -> None:
        """Deliver messages other threads SENT to this one (and nothing else: no
        posted message is taken off the queue). The embedded window is a child
        of a window of this thread, so focus changes there send messages here."""
        msg = wintypes.MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_QS_SENDMESSAGE)
else:
    def _pump() -> None:
        pass


_MAX_SHUTDOWN_WAIT = 60.0      # the most `shutdown` waits for remote releases


def _shutdown_wait(wait) -> float:
    """``close(wait=…)`` as TurboSSH takes it: 0 to 60 s (more counts as 60)."""
    try:
        wait = float(wait)
    except (TypeError, ValueError):
        return 6.0
    if wait != wait:                     # NaN
        return 6.0
    return min(max(wait, 0.0), _MAX_SHUTDOWN_WAIT)


def _wait(flag: threading.Event, timeout: Optional[float]) -> bool:
    deadline = None if timeout is None else time.monotonic() + timeout
    while not flag.wait(0.01):
        _pump()
        if deadline is not None and time.monotonic() >= deadline:
            return flag.is_set()
    return True


class _Pending:
    def __init__(self, callback=None):
        self.done = threading.Event()
        self.message: dict = {}
        self.callback = callback
        self.rid = 0


# --------------------------------------------------------------------------- #
class TurboSSHProcess:
    """A running TurboSSH GUI, embedded in (or next to) your application."""

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self._ids = itertools.count(1)
        self._pending: Dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._history: collections.deque = collections.deque(maxlen=5000)
        self._inbox: collections.deque = collections.deque()
        self._callbacks: List[Callable[[dict], None]] = []
        self._stderr: collections.deque = collections.deque(maxlen=200)
        self.ready: dict = {}
        # (callback, argument) for the dispatcher, in arrival order; None ends it
        self._dispatch: queue.Queue = queue.Queue()
        self._stderr_reader = threading.Thread(target=self._drain_stderr,
                                               name="turbossh-stderr", daemon=True)
        self._stderr_reader.start()
        threading.Thread(target=self._run_callbacks, name="turbossh-callbacks",
                         daemon=True).start()
        threading.Thread(target=self._read, name="turbossh-events", daemon=True).start()

    # ---- starting ----
    @classmethod
    def launch(cls, parent: Optional[int] = None, *, hide: Iterable[str] = (),
               theme: Optional[str] = None, camera: Optional[bool] = None,
               command: Optional[Sequence[str]] = None, env: Optional[dict] = None,
               cwd: Optional[str] = None, timeout: float = 60.0) -> "TurboSSHProcess":
        """Start TurboSSH. ``parent`` is the native handle of the window it should
        fill (an HWND on Windows: ``frame.winfo_id()``, ``int(widget.winId())``,
        ``panel.Handle``); without one it opens as a window of its own. ``hide``
        names parts not to show (menu_bar, ribbon, sidebar, log_dock,
        status_bar, home); ``theme`` and ``camera`` as for TurboSSHWidget."""
        argv = list(command or gui_command()) + ["--control", "stdio"]
        if parent:
            argv += ["--embed", str(int(parent))]
        hide = list(hide)
        if hide:
            argv += ["--hide", ",".join(hide)]
        if theme:
            argv += ["--theme", theme]
        if camera is not None:
            argv += ["--camera", "on" if camera else "off"]
        flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, cwd=cwd,
                                   creationflags=flags)
        gui = cls(process)
        try:
            gui.ready = gui.wait_for("ready", timeout=timeout)
        except TurboSSHError:
            gui.kill()
            raise
        if gui.ready.get("protocol") != PROTOCOL:
            gui.kill()
            raise TurboSSHError(f"TurboSSH speaks protocol {gui.ready.get('protocol')}, "
                                f"this client {PROTOCOL}")
        return gui

    @property
    def window(self) -> Optional[int]:
        """TurboSSH's own native window handle."""
        return self.ready.get("window")

    @property
    def version(self) -> str:
        return self.ready.get("version", "")

    @property
    def is_running(self) -> bool:
        return self.process.poll() is None

    def stderr_tail(self) -> str:
        """The last lines TurboSSH wrote to standard error (for diagnostics)."""
        return "\n".join(self._stderr)

    # ---- the wire ----
    def _read(self) -> None:
        for raw in self.process.stdout:
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if "event" in message:
                self._on_event(message)
                continue
            with self._lock:
                pending = self._pending.pop(message.get("id"), None)
            if pending is not None:
                self._answer(pending, message)
        # the process is gone: nobody will answer what is still waiting. Let its
        # last words on stderr arrive first, so errors can quote them.
        self._stderr_reader.join(2.0)
        with self._lock:
            waiting, self._pending = list(self._pending.values()), {}
        for pending in waiting:
            self._answer(pending, {"ok": False, "error": "TurboSSH has exited",
                                   "type": "Exited"})
        self._on_event({"event": "exited"}, final=True)
        self._dispatch.put(None)

    def _answer(self, pending: "_Pending", message: dict) -> None:
        pending.message = message
        pending.done.set()               # a waiting call() returns now
        if pending.callback is not None:
            self._dispatch.put((pending.callback, self._outcome(message)))

    def _run_callbacks(self) -> None:
        """The host's callbacks, one at a time, in order, off the reader thread:
        a callback that calls TurboSSH waits for an answer the reader delivers."""
        while True:
            item = self._dispatch.get()
            if item is None:
                return
            self._safe(*item)

    def _drain_stderr(self) -> None:
        for raw in self.process.stderr:
            self._stderr.append(raw.decode("utf-8", "replace").rstrip())

    def _on_event(self, message: dict, final: bool = False) -> None:
        if final and any(e.get("event") == "exited" for e in list(self._history)[-3:]):
            return                       # it already said so itself
        self._history.append(message)    # here: wait_for() sees it at once
        self._inbox.append(message)
        for callback in list(self._callbacks):
            self._dispatch.put((callback, message))

    @staticmethod
    def _safe(fn, arg) -> None:
        try:
            fn(arg)
        except Exception:                # a host callback must not kill the reader
            import traceback
            traceback.print_exc()

    @staticmethod
    def _outcome(message: dict):
        if message.get("ok"):
            return message.get("result")
        return TurboSSHError(message.get("error", "failed"), message.get("type", ""))

    def _send(self, cmd: str, args: dict, callback=None) -> _Pending:
        if not self.is_running:
            raise TurboSSHError("TurboSSH is not running" + (
                f":\n{self.stderr_tail()}" if self._stderr else ""))
        rid = next(self._ids)
        pending = _Pending(callback)
        pending.rid = rid
        with self._lock:
            self._pending[rid] = pending
        line = json.dumps({"id": rid, "cmd": cmd, "args": args}) + "\n"
        try:
            with self._write_lock:
                self.process.stdin.write(line.encode("utf-8"))
                self.process.stdin.flush()
        except OSError as exc:
            with self._lock:
                self._pending.pop(rid, None)
            raise TurboSSHError(f"TurboSSH is not listening: {exc}") from exc
        return pending

    # ---- requests ----
    def call(self, cmd: str, *, timeout: float = 30.0, **args):
        """Send one request and return its result (``TurboSSHError`` if refused)."""
        return self._request(cmd, args, timeout)

    def _request(self, cmd: str, args: dict, timeout: float):
        pending = self._send(cmd, args)
        if not _wait(pending.done, timeout):
            with self._lock:
                self._pending.pop(pending.rid, None)
            raise TurboSSHError(f"{cmd}: no answer within {timeout} s")
        outcome = self._outcome(pending.message)
        if isinstance(outcome, TurboSSHError):
            raise outcome
        return outcome

    def call_async(self, cmd: str, callback: Optional[Callable] = None, **args) -> None:
        """Send a request without waiting. ``callback(result_or_TurboSSHError)``
        runs on the callback thread (with the events, in order); it also runs,
        with an error, if TurboSSH exits first."""
        self._send(cmd, args, callback)

    # ---- events ----
    def on_event(self, callback: Callable[[dict], None]) -> None:
        """``callback(event_dict)`` for every event, on the callback thread: one
        at a time, in order. It may ``call()`` TurboSSH and ``wait_for()``."""
        self._callbacks.append(callback)

    def poll_events(self) -> List[dict]:
        """Events since the last poll — call it from a UI timer."""
        out = []
        while self._inbox:
            out.append(self._inbox.popleft())
        return out

    def wait_for(self, event: str, session: Optional[str] = None, *,
                 timeout: float = 30.0, since: int = 0) -> dict:
        """The first event of this kind (for this session) — including one that
        already arrived — waiting up to ``timeout`` seconds for it."""
        deadline = time.monotonic() + timeout
        while True:
            for message in list(self._history)[since:]:
                if message.get("event") == event and \
                        (session is None or message.get("session") == session):
                    return message
                if message.get("event") == "exited" and event != "exited":
                    raise TurboSSHError(f"TurboSSH exited while waiting for {event}"
                                        + (f":\n{self.stderr_tail()}" if self._stderr else ""))
            if time.monotonic() >= deadline:
                raise TurboSSHError(f"no {event} event within {timeout} s")
            _pump()
            time.sleep(0.01)

    # ---- opening ----
    def connect_ssh(self, host: str, **options) -> str:
        """Open an SSH tab; returns its session id at once. Watch for its
        ``session_connected`` / ``session_failed`` event. Options as
        TurboSSHWidget.connect_ssh (username, password, port, key_file, …)."""
        return self.call("connect_ssh", host=host, **options)["session"]

    def open_serial(self, device: str, **options) -> str:
        return self.call("open_serial", device=device, **options)["session"]

    def open_serial_over_ssh(self, device: str, host: str, **options) -> str:
        return self.call("open_serial_over_ssh", device=device, host=host,
                         **options)["session"]

    def open_saved(self, name: str) -> str:
        return self.call("open_saved", name=name)["session"]

    def open_local_terminal(self) -> str:
        return self.call("open_local_terminal")["session"]

    def open_camera(self) -> str:
        return self.call("open_camera")["session"]

    # ---- sessions ----
    def sessions(self) -> List[dict]:
        return self.call("sessions")

    def current_session(self) -> Optional[str]:
        return self.call("current_session")["session"]

    def saved_session_names(self) -> List[str]:
        return self.call("saved_session_names")

    def send(self, session: str, data: Union[str, bytes]) -> None:
        if isinstance(data, str):
            self.call("send", session=session, text=data)
        else:
            self.call("send", session=session,
                      data_base64=base64.b64encode(bytes(data)).decode("ascii"))

    def send_line(self, session: str, text: str) -> None:
        self.call("send_line", session=session, text=text)

    def screen_text(self, session: str) -> str:
        return self.call("screen_text", session=session)["text"]

    def run(self, session: str, command: str, timeout: float = 60.0) -> dict:
        """Run a command over the session's SSH connection (not in its terminal):
        ``{"exit_code", "stdout", "stderr", "duration"}``. ``timeout`` 0 is no
        limit (the answer is then waited for as long as it takes)."""
        return self._request("run", {"session": session, "command": command,
                                     "timeout": timeout},
                             timeout + 15 if timeout else None)

    def activate(self, session: str) -> None:
        self.call("activate", session=session)

    def close_session(self, session: str) -> None:
        self.call("close_session", session=session)

    # ---- the window ----
    def focus(self) -> None:
        """Give TurboSSH the keyboard."""
        self.call("focus")

    def set_geometry(self, x: int, y: int, width: int, height: int) -> None:
        """Sit at this rectangle of the parent window (physical pixels) instead of
        filling it — e.g. over a placeholder in a web page."""
        self.call("set_geometry", x=x, y=y, width=width, height=height)

    def fill_parent(self) -> None:
        self.call("fill_parent")

    def set_visible(self, visible: bool) -> None:
        self.call("set_visible", visible=visible)

    def set_theme(self, name: str) -> None:
        self.call("set_theme", name=name)

    def set_camera_enabled(self, enabled: bool) -> None:
        self.call("set_camera_enabled", enabled=enabled)

    def set_chrome(self, **parts: bool) -> None:
        self.call("set_chrome", **parts)

    def trigger_action(self, name: str) -> None:
        self.call("trigger_action", name=name)

    def show_settings(self) -> None:
        self.call("show_settings")

    def new_session_dialog(self) -> None:
        self.call("new_session_dialog")

    # ---- the end ----
    def close(self, wait: float = 6.0, timeout: float = 20.0) -> int:
        """Close every session (waiting up to ``wait`` s, 0 to 60, for remote
        ports and cameras to be released) and end the process. Returns its exit
        code. The process gets ``timeout`` s to end, and at least ``wait`` + 5;
        only then is it killed (its sessions are not released then)."""
        wait = _shutdown_wait(wait)
        if self.is_running:
            try:
                self.call("shutdown", wait=wait, timeout=5.0)
            except TurboSSHError as exc:
                if exc.type != "Exited":
                    # refused, or no answer: closing its stdin ends it as cleanly
                    self._hang_up()
        deadline = time.monotonic() + max(float(timeout), wait + 5.0)
        while self.process.poll() is None and time.monotonic() < deadline:
            _pump()
            time.sleep(0.02)
        if self.process.poll() is None:
            self.kill()
        for stream in (self.process.stdin,):
            try:
                stream.close()
            except OSError:
                pass
        return self.process.wait()

    def _hang_up(self) -> None:
        """Close TurboSSH's stdin: it notices, closes every session cleanly and
        ends, as for ``shutdown``."""
        try:
            with self._write_lock:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass

    def kill(self) -> None:
        try:
            self.process.kill()
        except OSError:
            pass

    def __enter__(self) -> "TurboSSHProcess":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
