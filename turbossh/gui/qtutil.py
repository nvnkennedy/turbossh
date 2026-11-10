"""Thread and teardown helpers shared by the GUI.

Three rules they exist to enforce:

* a ``QThread`` object must never be destroyed while its thread still runs (Qt
  aborts the whole app) — so a worker that outlives its widget is *parked*:
  silenced, and kept alive until it finishes on its own;
* a result that arrives after its widget is gone must not be delivered to it —
  parking disconnects the worker's signals (lambda slots are not disconnected
  automatically when the widget they capture is deleted);
* blocking work (network round trips, process waits) never runs on the GUI
  thread — and teardown that must complete before the process exits (releasing a
  remote COM port or camera) is joined at quit.
"""

from __future__ import annotations

import threading
import time

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QMainWindow


class SignalGate:
    """Lets a plain Python thread emit a widget's signal until the widget closes.

    Emitting a signal of a widget Qt has already deleted crashes the process with
    an access violation, not an exception. A thread that can outlive its widget —
    a teardown that logs while it disconnects, a save or a recording that finishes
    later — emits through a gate, and the widget closes the gate before it goes.
    The lock means no emit is still running once ``close()`` has returned."""

    def __init__(self, signal):
        self._signal = signal
        self._lock = threading.RLock()   # re-entrant: a direct slot may close it

    def emit(self, *args) -> bool:
        with self._lock:
            if self._signal is None:
                return False
            self._signal.emit(*args)
            return True

    def close(self) -> None:
        with self._lock:
            self._signal = None


def owner_window(widget):
    """The TurboSSH window *widget* lives in. ``widget.window()`` is the top-level
    window, which is someone else's when TurboSSH is embedded: a dialog parented
    there would lose the theme, which only reaches TurboSSH's own widgets."""
    current = widget
    while current is not None and not isinstance(current, QMainWindow):
        current = current.parentWidget()
    return current if current is not None else widget.window()


# signal names our worker threads use for results; park_thread silences these
_RESULT_SIGNALS = ("ok", "fail", "done", "failed", "progress", "closed",
                   "stopped", "result")

_PARKED: set = set()


def park_thread(thread, orphan: dict | None = None) -> None:
    """Detach a (possibly still running) QThread from its owner.

    Its result signals are disconnected so nothing is delivered to a widget that
    is closing, and a reference is kept until it finishes. ``orphan`` maps a
    signal name to a callable that still receives that result — e.g. to close a
    connection the thread was busy opening, which would otherwise leak.
    """
    if thread is None:
        return
    for name in _RESULT_SIGNALS:
        signal = getattr(thread, name, None)
        if signal is not None:
            try:
                signal.disconnect()
            except Exception:
                pass
    try:
        running = thread.isRunning()
    except RuntimeError:                 # the C++ object is already gone
        return
    if not running:
        return
    for name, callback in (orphan or {}).items():
        signal = getattr(thread, name, None)
        if signal is not None:
            signal.connect(callback)
    _PARKED.add(thread)
    thread.finished.connect(lambda t=thread: _PARKED.discard(t))
    if thread.isFinished():
        # it ended between isRunning() and connect(): finished() has already
        # been emitted, and it would otherwise stay parked for good
        _PARKED.discard(thread)


class FunctionThread(QThread):
    """Run one callable off the GUI thread; ``done(result)`` or ``fail(text)``."""
    done = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as exc:        # noqa: BLE001 - reported to the caller
            self.fail.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


def run_job(jobs: set, fn, on_done=None, on_fail=None) -> FunctionThread:
    """Start *fn* on a worker thread tracked in *jobs* (a set owned by the
    widget). Call :func:`close_jobs` on that set when the widget closes."""
    thread = FunctionThread(fn)
    if on_done is not None:
        thread.done.connect(on_done)
    if on_fail is not None:
        thread.fail.connect(on_fail)
    jobs.add(thread)
    thread.finished.connect(lambda t=thread: jobs.discard(t))
    thread.start()
    return thread


def close_jobs(jobs: set) -> None:
    """Park every worker in *jobs*: their results are dropped, none is destroyed
    while running, and the GUI thread does not wait for them."""
    for thread in list(jobs):
        park_thread(thread)
    jobs.clear()


# --------------------------------------------------------------------------- #
# Teardown that must finish before the process exits
# --------------------------------------------------------------------------- #
_TEARDOWNS: list = []
_teardown_lock = threading.Lock()


def run_teardown(fn, name: str = "teardown") -> threading.Thread:
    """Run a blocking teardown step (remote release, disconnect) on a background
    thread, so closing a tab never freezes the window. The thread is remembered:
    :func:`wait_for_teardowns` joins them at quit, because a daemon thread killed
    mid-way would leave the remote port / camera locked."""
    def _safe():
        try:
            fn()
        except Exception:
            pass
    thread = threading.Thread(target=_safe, name=name, daemon=True)
    with _teardown_lock:
        _TEARDOWNS[:] = [t for t in _TEARDOWNS if t.is_alive()]
        _TEARDOWNS.append(thread)
    thread.start()
    return thread


def wait_for_teardowns(timeout: float = 6.0) -> bool:
    """Join outstanding teardown threads, for at most *timeout* seconds in total.
    Returns True if they all finished."""
    deadline = time.monotonic() + timeout
    with _teardown_lock:
        pending = list(_TEARDOWNS)
    for thread in pending:
        thread.join(max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in pending)
