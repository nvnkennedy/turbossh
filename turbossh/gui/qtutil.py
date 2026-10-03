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
