"""Background worker thread: owns the SSHHandler and runs queued jobs so the
GUI never blocks. All SSH work happens on this one thread (paramiko-friendly)."""

from __future__ import annotations

import queue
import threading
import traceback

from PyQt5.QtCore import QThread, pyqtSignal

from ..config import SSHConfig
from ..core import SSHHandler
from ..results import OperationResult


class Worker(QThread):
    log = pyqtSignal(str)                  # a log/output line (already masked)
    status = pyqtSignal(str, bool)         # message, connected?
    busy = pyqtSignal(bool)                # an operation is running

    def __init__(self):
        super().__init__()
        self._jobs: "queue.Queue" = queue.Queue()
        self._alive = True
        self.ssh: SSHHandler | None = None
        self.stream_stop = threading.Event()

    # ---- thread loop ----
    def run(self):
        while self._alive:
            job = self._jobs.get()
            if job is None:
                break
            label, fn = job
            self.busy.emit(True)
            try:
                fn()
            except Exception as exc:                # never kill the thread
                self.log.emit(f"[ERROR] {label}: {type(exc).__name__}: {exc}")
                tb = traceback.format_exc(limit=4).rstrip()
                self.log.emit("[ERROR] " + tb.replace("\n", "\n          "))
            finally:
                self.busy.emit(False)

    def submit(self, label, fn):
        self._jobs.put((label, fn))

    def shutdown(self):
        self._alive = False
        self.stream_stop.set()
        self._jobs.put(None)

    def _need(self) -> bool:
        if not self.ssh or not self.ssh.is_connected:
            self.log.emit("[ERROR] Not connected. Click Connect first.")
            return False
        return True

    # ---- jobs ----
    def do_connect(self, cfg: SSHConfig):
        def _job():
            self.ssh = SSHHandler(cfg, log_callback=self.log.emit, safe=True)
            res = self.ssh.connect()
            if bool(res):
                self.status.emit(f"Connected to {cfg.host}", True)
            else:
                err = getattr(res, "error", "")
                self.status.emit(f"Connect failed: {err}", False)
        self.submit("connect", _job)

    def do_disconnect(self):
        def _job():
            self.stream_stop.set()
            if self.ssh:
                self.ssh.disconnect()
            self.status.emit("Disconnected", False)
        self.submit("disconnect", _job)

    def do_run(self, command: str):
        def _job():
            if not command or not self._need():
                return
            res = self.ssh.run(command)
            if isinstance(res, OperationResult) and res.success:
                r = res.value
                if r.stdout:
                    self.log.emit(r.stdout.rstrip())
                if r.stderr:
                    self.log.emit("[stderr] " + r.stderr.rstrip())
                self.log.emit(f"[exit {r.exit_code}, {r.duration:.2f}s]")
        self.submit("run", _job)

    def do_push(self, local, remote, recursive):
        def _job():
            if not self._need():
                return
            res = self.ssh.push(local, remote, recursive=recursive)
            if isinstance(res, OperationResult) and res.success:
                self.log.emit("[OK] " + str(res.value))
        self.submit("push", _job)

    def do_pull(self, remote, local, recursive):
        def _job():
            if not self._need():
                return
            res = self.ssh.pull(remote, local, recursive=recursive)
            if isinstance(res, OperationResult) and res.success:
                self.log.emit("[OK] " + str(res.value))
        self.submit("pull", _job)

    def do_serial(self, device, baud, match, save_to):
        def _job():
            if not self._need():
                return
            self.stream_stop.clear()
            self.log.emit(f"[OK] serial {device} @ {baud} — Stop to end")
            self.ssh.serial_stream(device, baudrate=baud, on_line=self.log.emit,
                                   match=(match or None), save_to=(save_to or None),
                                   stop_event=self.stream_stop)
            self.log.emit("[OK] serial stopped")
        self.submit("serial", _job)

    def do_stream(self, command, match, save_to):
        def _job():
            if not command or not self._need():
                return
            self.stream_stop.clear()
            self.log.emit(f"[OK] streaming '{command}' — Stop to end")
            self.ssh.stream(command, on_line=self.log.emit, match=(match or None),
                            save_to=(save_to or None), stop_event=self.stream_stop)
            self.log.emit("[OK] stream stopped")
        self.submit("stream", _job)

    def stop_stream(self):
        self.stream_stop.set()
