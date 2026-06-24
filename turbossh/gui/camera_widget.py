"""Camera panel — view any camera on THIS machine (Local) or on a saved remote
machine (over its own SSH connection). Pick a source, pick a camera, watch it;
snapshot, record, pause. Local needs no connecting and is the default. Runs on
its own threads so it never touches the terminal/serial work.
"""

from __future__ import annotations

import os
import time
import threading
import subprocess

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QComboBox, QFileDialog, QMessageBox)

from ..core import SSHHandler
from ..results import OperationResult
from .sessions import SessionStore
from .session_widgets import config_from_session
from . import ffmpeg_tools

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class _FrameReader(threading.Thread):
    """Reads an MJPEG byte stream (from a pipe or SSH channel) via ``read_fn``,
    splits it into JPEG frames, keeps the latest for display, and tees raw bytes
    to a recorder when attached."""

    def __init__(self, read_fn):
        super().__init__(daemon=True)
        self._read = read_fn
        self._buf = bytearray()
        self._latest = None
        self._lock = threading.Lock()
        self._alive = True
        self._record_fh = None
        self.frames = 0

    def run(self):
        while self._alive:
            try:
                data = self._read(262144)
            except Exception:
                break
            if data is None:
                break
            if not data:
                time.sleep(0.01); continue
            rec = self._record_fh
            if rec is not None:
                try:
                    rec.write(data)
                except Exception:
                    pass
            self._buf.extend(data)
            self._extract()

    def _extract(self):
        buf = self._buf
        end = buf.rfind(b"\xff\xd9")
        if end == -1:
            if len(buf) > 8 * 1024 * 1024:
                del buf[:-1024 * 1024]
            return
        start = buf.rfind(b"\xff\xd8", 0, end)
        if start == -1:
            return
        frame = bytes(buf[start:end + 2])
        del buf[:end + 2]
        with self._lock:
            self._latest = frame
            self.frames += 1

    def latest(self):
        with self._lock:
            return self._latest

    def set_recorder(self, fh):
        self._record_fh = fh

    def stop(self):
        self._alive = False


class _LocalPrep(QThread):
    """Ensure local ffmpeg + list local cameras (off the UI thread)."""
    progress = pyqtSignal(str)
    done = pyqtSignal(str, list)        # ffmpeg path, cameras
    fail = pyqtSignal(str)

    def run(self):
        try:
            ff = ffmpeg_tools.ensure_local_ffmpeg(self.progress.emit)
            cams = ffmpeg_tools.list_local_cameras(ff)
            self.done.emit(ff, cams)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _RemotePrep(QThread):
    """Connect SSH, ensure ffmpeg on the remote, list its cameras."""
    progress = pyqtSignal(str)
    done = pyqtSignal(object, str, list)   # handler, remote ffmpeg, cameras
    fail = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            local = ffmpeg_tools.ensure_local_ffmpeg(self.progress.emit)
            h = SSHHandler(self.cfg, safe=True)
            res = h.connect()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error)); return
            remote = ffmpeg_tools.ensure_remote_ffmpeg(h, local, self.progress.emit)
            r = h.list_cameras(ffmpeg=remote)
            cams = r.value if isinstance(r, OperationResult) else r
            self.done.emit(h, remote, list(cams or []))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class CameraPanel(QWidget):
    """A camera viewer tab. ``store`` is the SessionStore so remote machines can
    be picked from saved sessions (reusing their stored credentials)."""
    log = pyqtSignal(str)

    def __init__(self, store: SessionStore, parent=None):
        super().__init__(parent)
        self.store = store
        self._handler = None            # SSH handler for the current remote source
        self._remote_ffmpeg = None
        self._local_ffmpeg = None
        self._proc = None               # local capture subprocess
        self._chan = None               # remote capture channel
        self.reader = None
        self._rec_proc = None
        self._rec_path = None
        self._paused = False
        self._busy_checked = False

        lay = QVBoxLayout(self)

        # --- source / camera / refresh / start ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Source:"))
        self.source = QComboBox()
        self.source.addItem("Local (this PC)", None)
        for s in self.store.sessions:
            if s.get("host"):
                self.source.addItem(f"Remote · {s.get('name')}", s.get("name"))
        self.source.currentIndexChanged.connect(lambda *_: self._refresh())
        top.addWidget(self.source, 2)
        top.addWidget(QLabel("Camera:"))
        self.camera = QComboBox(); top.addWidget(self.camera, 2)
        self.refresh_btn = QPushButton("🔄 Refresh"); self.refresh_btn.setProperty("role", "ghost")
        self.refresh_btn.clicked.connect(self._refresh)
        self.start_btn = QPushButton("▶ Start"); self.start_btn.setProperty("role", "ok")
        self.start_btn.clicked.connect(self._toggle_start)
        top.addWidget(self.refresh_btn); top.addWidget(self.start_btn)
        lay.addLayout(top)

        # --- the view (front and centre) ---
        self.view = QLabel("Pick a source and camera, then Start.")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumSize(560, 400)
        self.view.setStyleSheet("background:#000; color:#8a8a8a; border-radius:6px;")
        lay.addWidget(self.view, 1)

        # --- controls ---
        row = QHBoxLayout()
        self.snap_btn = QPushButton("📷 Snapshot"); self.snap_btn.setProperty("role", "ghost")
        self.rec_btn = QPushButton("⏺ Record"); self.rec_btn.setProperty("role", "ghost")
        self.pause_btn = QPushButton("⏸ Pause"); self.pause_btn.setProperty("role", "ghost")
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(False)
        self.snap_btn.clicked.connect(self._snapshot)
        self.rec_btn.clicked.connect(self._toggle_record)
        self.pause_btn.clicked.connect(self._toggle_pause)
        row.addWidget(self.snap_btn); row.addWidget(self.rec_btn); row.addWidget(self.pause_btn)
        row.addStretch(1)
        self.status = QLabel("")
        self.status.setStyleSheet("color:#8a8a8a;")
        row.addWidget(self.status)
        self.link = QLabel(""); self.link.setOpenExternalLinks(True)
        self.link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        row.addWidget(self.link)
        lay.addLayout(row)

        self._timer = QTimer(self); self._timer.timeout.connect(self._tick)
        self._timer.start(40)

        QTimer.singleShot(0, self._refresh)        # auto-list local cameras on open

    # ---- camera enumeration ----
    def _refresh(self):
        self._stop_stream()
        self.camera.clear()
        self.refresh_btn.setEnabled(False)
        name = self.source.currentData()
        if name is None:
            self.status.setText("Finding local cameras…")
            self._prep = _LocalPrep()
            self._prep.progress.connect(self.status.setText)
            self._prep.done.connect(self._local_ready)
            self._prep.fail.connect(self._prep_fail)
            self._prep.start()
        else:
            s = self.store.get(name)
            cfg = config_from_session(s, SessionStore.password(name) or "",
                                      SessionStore.jump_password(name) or "")
            self.status.setText(f"Connecting to {s.get('host')} and finding cameras…")
            self._prep = _RemotePrep(cfg)
            self._prep.progress.connect(self.status.setText)
            self._prep.done.connect(self._remote_ready)
            self._prep.fail.connect(self._prep_fail)
            self._prep.start()

    def _local_ready(self, ffmpeg, cams):
        self.refresh_btn.setEnabled(True)
        self._local_ffmpeg = ffmpeg
        self._handler = None
        self._fill_cameras(cams)

    def _remote_ready(self, handler, remote_ffmpeg, cams):
        self.refresh_btn.setEnabled(True)
        self._handler = handler
        self._remote_ffmpeg = remote_ffmpeg
        self._fill_cameras(cams)

    def _fill_cameras(self, cams):
        self.camera.clear()
        for c in cams:
            self.camera.addItem(c)
        if cams:
            self.status.setText(f"{len(cams)} camera(s) found — Start to view.")
            self.start_btn.setEnabled(True)
        else:
            self.status.setText("No cameras found on this source.")
            self.start_btn.setEnabled(False)

    def _prep_fail(self, msg):
        self.refresh_btn.setEnabled(True)
        self.status.setText("Couldn't list cameras.")
        QMessageBox.warning(self, "Camera", f"Couldn't list cameras:\n\n{msg}")

    # ---- start / stop ----
    def _toggle_start(self):
        if self.reader is not None:
            self._stop_stream()
            self.start_btn.setText("▶ Start")
            return
        cam = self.camera.currentText().strip()
        if not cam:
            return
        self._busy_checked = False
        if self.source.currentData() is None:
            self._start_local(cam, force=False)
        else:
            self._start_remote(cam, force=False)

    def _start_local(self, cam, force):
        try:
            args = ffmpeg_tools.local_capture_args(self._local_ffmpeg, cam)
            self._proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL,
                                          creationflags=_NO_WINDOW)
        except Exception as exc:
            QMessageBox.warning(self, "Camera", f"Couldn't start the camera:\n\n{exc}")
            return
        self.reader = _FrameReader(lambda n, p=self._proc: p.stdout.read(n))
        self.reader.start()
        self._after_start(cam)

    def _start_remote(self, cam, force):
        s = self.source.currentData()
        sess = self.store.get(s)
        try:
            self._chan = self._handler.webcam_channel(
                cam, ffmpeg=self._remote_ffmpeg,
                width=int(sess.get("cam_width", 640)),
                height=int(sess.get("cam_height", 480)),
                fps=int(sess.get("cam_fps", 15)), force=force)
        except Exception as exc:
            QMessageBox.warning(self, "Camera", f"Couldn't start the camera:\n\n{exc}")
            return
        self._chan.settimeout(0.1)

        def read_fn(n, c=self._chan):
            if c.closed or c.eof_received:
                return None
            try:
                import socket as _s
                return c.recv(n)
            except Exception as exc:
                return b"" if exc.__class__.__name__ == "timeout" else None

        self.reader = _FrameReader(read_fn)
        self.reader.start()
        self._after_start(cam)
        QTimer.singleShot(6000, lambda: self._check_busy(cam))

    def _after_start(self, cam):
        self.start_btn.setText("⏹ Stop")
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(True)
        self.status.setText(f"Viewing {cam}")

    def _check_busy(self, cam):
        if self.reader is None or self.reader.frames > 0 or self._busy_checked:
            return
        self._busy_checked = True
        r = QMessageBox.question(
            self, "Camera not responding",
            f"No video from {cam} yet — it may be in use by another app.\n\n"
            f"Force it open (take it from whatever TurboSSH left holding it)?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self._stop_stream()
            self._start_remote(cam, force=True)

    # ---- display ----
    def _tick(self):
        if self._paused or self.reader is None:
            return
        frame = self.reader.latest()
        if not frame:
            return
        img = QImage.fromData(frame, "JPG")
        if img.isNull():
            return
        self.view.setPixmap(QPixmap.fromImage(img).scaled(
            self.view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ---- controls ----
    def _snapshot(self):
        frame = self.reader.latest() if self.reader else None
        if not frame:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save snapshot",
                                              f"snapshot-{int(time.time())}.jpg",
                                              "Images (*.jpg *.png)")
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(frame)
            self._show_link(path)
            self.log.emit(f"[OK] snapshot saved: {path}")
        except Exception as exc:
            self.log.emit(f"[ERROR] snapshot: {exc}")

    def _toggle_record(self):
        if self._rec_proc is not None:
            self._stop_record(); return
        ff = ffmpeg_tools.cached_ffmpeg()
        if not ff:
            QMessageBox.warning(self, "Recording", "ffmpeg isn't ready yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Record video to",
                                              f"recording-{int(time.time())}.mp4",
                                              "Video (*.mp4)")
        if not path:
            return
        try:
            self._rec_proc = subprocess.Popen(
                [ff, "-y", "-f", "mjpeg", "-i", "-", "-c", "copy", path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            self._rec_path = path
            self.reader.set_recorder(self._rec_proc.stdin)
            self.rec_btn.setText("⏺ Stop recording")
            self.log.emit(f"[OK] recording to {path}")
        except Exception as exc:
            self._rec_proc = None
            self.log.emit(f"[ERROR] record: {exc}")

    def _stop_record(self):
        if self.reader:
            self.reader.set_recorder(None)
        proc, self._rec_proc = self._rec_proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.rec_btn.setText("⏺ Record")
        if self._rec_path:
            self._show_link(self._rec_path)
            self.log.emit(f"[OK] recording saved: {self._rec_path}")

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause_btn.setText("▶ Resume" if self._paused else "⏸ Pause")

    def _show_link(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        self.link.setText(f'Saved — <a href="file:///{folder.replace(chr(92), "/")}">open folder</a>')

    # ---- teardown ----
    def _stop_stream(self):
        self._stop_record()
        if self.reader:
            try:
                self.reader.stop()
            except Exception:
                pass
        self.reader = None
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._chan is not None:
            try:
                self._chan.close()
            except Exception:
                pass
            self._chan = None
        # release a remote camera (kill the remote ffmpeg) so others can use it
        if self._handler is not None:
            try:
                self._handler.webcam_release(safe=True)
            except Exception:
                pass
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(False)
        self._paused = False
        self.pause_btn.setText("⏸ Pause")

    def close_session(self):
        try:
            self._timer.stop()
        except Exception:
            pass
        self._stop_stream()
        if self._handler is not None:
            try:
                self._handler.disconnect()
            except Exception:
                pass
            self._handler = None
