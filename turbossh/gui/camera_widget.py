"""Remote-webcam session: stream a camera on the RDP machine over its own SSH
connection (ffmpeg/dshow -> MJPEG), shown in a dedicated tab. Snapshot, record,
pause, and stop; files are saved locally with an open-folder link. Everything
runs on its own threads so it never touches the terminal/serial work.
"""

from __future__ import annotations

import os
import time
import threading
import subprocess

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QFileDialog, QMessageBox)

from ..core import SSHHandler
from ..results import OperationResult
from .session_widgets import config_from_session
from . import ffmpeg_tools


class _CameraConnectThread(QThread):
    """Off-thread setup: connect SSH, make sure ffmpeg is local + on the remote."""
    progress = pyqtSignal(str)
    ok = pyqtSignal(object, str)        # handler, remote ffmpeg path
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
            self.ok.emit(h, remote)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _FrameReader(threading.Thread):
    """Reads the MJPEG channel, splits it into JPEG frames (FFD8…FFD9), keeps the
    latest for display, and tees raw bytes to a recorder when one is attached."""

    def __init__(self, chan):
        super().__init__(daemon=True)
        self.chan = chan
        self._buf = bytearray()
        self._latest = None
        self._lock = threading.Lock()
        self._alive = True
        self._record_fh = None          # file object / subprocess stdin to tee into
        self.frames = 0

    def run(self):
        while self._alive:
            try:
                if self.chan.closed or self.chan.eof_received:
                    break
                data = self.chan.recv(262144)
                if data == b"":
                    time.sleep(0.01); continue
            except Exception:
                break
            rec = self._record_fh
            if rec is not None:
                try:
                    rec.write(data)
                except Exception:
                    pass
            self._buf.extend(data)
            self._extract()

    def _extract(self):
        # keep only the most recent complete JPEG; drop everything before it
        buf = self._buf
        end = buf.rfind(b"\xff\xd9")
        if end == -1:
            if len(buf) > 8 * 1024 * 1024:        # runaway guard
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


class CameraSessionWidget(QWidget):
    log = pyqtSignal(str)
    connected = pyqtSignal(bool, str)
    failed = pyqtSignal(str)

    def __init__(self, session: dict, password: str = "", jump_password: str = "",
                 parent=None):
        super().__init__(parent)
        self.session = session
        self.handler = None
        self.chan = None
        self.reader = None
        self.ffmpeg_remote = None
        self._record_proc = None
        self._record_path = None
        self._paused = False
        self._got_frame = False

        lay = QVBoxLayout(self)
        self.status = QLabel("Connecting…")
        lay.addWidget(self.status)

        self.view = QLabel("camera starting…")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumSize(480, 360)
        self.view.setStyleSheet("background:#000; color:#8a8a8a;")
        lay.addWidget(self.view, 1)

        row = QHBoxLayout()
        self.snap_btn = QPushButton("📷 Snapshot"); self.snap_btn.setProperty("role", "ghost")
        self.rec_btn = QPushButton("⏺ Record"); self.rec_btn.setProperty("role", "ghost")
        self.pause_btn = QPushButton("⏸ Pause"); self.pause_btn.setProperty("role", "ghost")
        self.stop_btn = QPushButton("⏹ Stop"); self.stop_btn.setProperty("role", "danger")
        for b in (self.snap_btn, self.rec_btn, self.pause_btn, self.stop_btn):
            b.setEnabled(False)
        self.snap_btn.clicked.connect(self._snapshot)
        self.rec_btn.clicked.connect(self._toggle_record)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn.clicked.connect(self.close_session)
        row.addWidget(self.snap_btn); row.addWidget(self.rec_btn)
        row.addWidget(self.pause_btn); row.addWidget(self.stop_btn)
        row.addStretch(1)
        self.link = QLabel("")
        self.link.setOpenExternalLinks(True)
        self.link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        row.addWidget(self.link)
        lay.addLayout(row)

        cfg = config_from_session(session, password, jump_password)
        self._ct = _CameraConnectThread(cfg)
        self._ct.progress.connect(lambda m: self.status.setText(m))
        self._ct.ok.connect(self._on_ready)
        self._ct.fail.connect(self._on_fail)
        self._ct.start()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)            # ~25 fps display refresh

    # ---- setup ----
    def _on_ready(self, handler, ffmpeg_remote):
        self.handler = handler
        self.ffmpeg_remote = ffmpeg_remote
        self._open_stream(force=False)

    def _open_stream(self, force):
        cam = self.session.get("camera", "")
        if not cam:
            self._on_fail("No camera selected. Edit the session and pick one "
                          "(use Scan cameras).")
            return
        s = self.session
        try:
            self.chan = self.handler.webcam_channel(
                cam, ffmpeg=self.ffmpeg_remote,
                width=int(s.get("cam_width", 640)), height=int(s.get("cam_height", 480)),
                fps=int(s.get("cam_fps", 15)), force=force)
        except Exception as exc:
            self._on_fail(f"could not start the camera: {exc}")
            return
        self.chan.settimeout(0.1)
        self.reader = _FrameReader(self.chan)
        self.reader.start()
        self.status.setText(f"Streaming {cam} from {self.session.get('host')}")
        self.connected.emit(True, f"Camera {cam}")
        for b in (self.snap_btn, self.rec_btn, self.pause_btn, self.stop_btn):
            b.setEnabled(True)
        self._got_frame = False
        QTimer.singleShot(6000, self._check_first_frame)

    def _check_first_frame(self):
        if self._got_frame or self.reader is None:
            return
        r = QMessageBox.question(
            self, "Camera not responding",
            f"No video from {self.session.get('camera')} yet — it may be in use by "
            f"another app on {self.session.get('host')}.\n\nForce it open (take the "
            f"camera from whatever TurboSSH left holding it)?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self._restart(force=True)

    def _restart(self, force):
        self._teardown_stream()
        self._open_stream(force=force)

    # ---- display ----
    def _tick(self):
        if self._paused or self.reader is None:
            return
        frame = self.reader.latest()
        if not frame:
            return
        self._got_frame = True
        img = QImage.fromData(frame, "JPG")
        if img.isNull():
            return
        pm = QPixmap.fromImage(img).scaled(self.view.size(), Qt.KeepAspectRatio,
                                           Qt.SmoothTransformation)
        self.view.setPixmap(pm)

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
        if self._record_proc is not None:
            self._stop_record()
            return
        ff = ffmpeg_tools.cached_ffmpeg()
        if not ff:
            QMessageBox.warning(self, "Recording needs ffmpeg",
                                "Local ffmpeg isn't ready yet. Try again in a moment.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Record video to",
                                              f"recording-{int(time.time())}.mp4",
                                              "Video (*.mp4)")
        if not path:
            return
        try:
            # mux the incoming MJPEG straight into an mp4 (no re-encode)
            self._record_proc = subprocess.Popen(
                [ff, "-y", "-f", "mjpeg", "-i", "-", "-c", "copy", path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000 if os.name == "nt" else 0)
            self._record_path = path
            self.reader.set_recorder(self._record_proc.stdin)
            self.rec_btn.setText("⏺ Stop recording")
            self.log.emit(f"[OK] recording to {path}")
        except Exception as exc:
            self._record_proc = None
            self.log.emit(f"[ERROR] record: {exc}")

    def _stop_record(self):
        if self.reader:
            self.reader.set_recorder(None)
        proc, self._record_proc = self._record_proc, None
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
        if self._record_path:
            self._show_link(self._record_path)
            self.log.emit(f"[OK] recording saved: {self._record_path}")

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause_btn.setText("▶ Resume" if self._paused else "⏸ Pause")

    def _show_link(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        url = "file:///" + folder.replace("\\", "/")
        self.link.setText(f'Saved — <a href="{url}">open folder</a>')

    # ---- teardown ----
    def _on_fail(self, msg):
        self.status.setText("Camera failed")
        self.view.setText(msg)
        self.log.emit(f"[ERROR] camera: {msg}")
        self.failed.emit(f"{self.session.get('name')}: {msg}")

    def _teardown_stream(self):
        if self.reader:
            try:
                self.reader.stop()
            except Exception:
                pass
        if self.chan is not None:
            try:
                self.chan.close()
            except Exception:
                pass
        self.chan = None
        self.reader = None

    def close_session(self):
        try:
            self._timer.stop()
        except Exception:
            pass
        self._stop_record()
        self._teardown_stream()
        # kill the remote ffmpeg so the camera is released for others
        if self.handler is not None:
            try:
                self.handler.webcam_release(safe=True)
            except Exception:
                pass
            try:
                self.handler.disconnect()
            except Exception:
                pass
        for b in (self.snap_btn, self.rec_btn, self.pause_btn, self.stop_btn):
            try:
                b.setEnabled(False)
            except Exception:
                pass
