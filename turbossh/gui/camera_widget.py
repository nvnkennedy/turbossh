"""Camera panel — view a camera on THIS machine (Local) or on the RDP / Windows
machine over SSH. Pick the source, pick a camera, watch it; snapshot, record,
pause. Frames are decoded off the UI thread so the view stays smooth, and the
view fills the tab. Runs on its own threads — never touches terminal/serial work.
"""

from __future__ import annotations

import os
import time
import threading
import subprocess

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QImage, QPixmap, QTransform
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QComboBox, QLineEdit, QFileDialog, QMessageBox,
                             QSizePolicy, QProgressDialog)

from ..core import SSHHandler
from ..config import SSHConfig
from ..results import OperationResult
from . import ffmpeg_tools
from . import settings as settings_mod
from .widgets import HostCombo

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_RES = {"480p (640×480)": (640, 480), "540p (960×540)": (960, 540),
        "720p (1280×720)": (1280, 720), "1080p (1920×1080)": (1920, 1080)}


class _FrameReader(threading.Thread):
    """Reads an MJPEG byte stream (pipe or SSH channel) via ``read_fn``, splits it
    into JPEG frames, DECODES the latest to a QImage here (off the UI thread), and
    tees raw bytes to a recorder when attached."""

    def __init__(self, read_fn):
        super().__init__(daemon=True)
        self._read = read_fn
        self._buf = bytearray()
        self._img = None
        self._raw = None
        self._lock = threading.Lock()
        self._alive = True
        self._record_fh = None
        self.frames = 0

    def run(self):
        while self._alive:
            try:
                data = self._read(131072)
            except Exception:
                break
            if data is None:
                break
            if not data:
                time.sleep(0.005); continue
            self._buf.extend(data)
            self._extract()

    def _extract(self):
        """Pull every COMPLETE JPEG out of the buffer in order. Each whole frame
        is tee'd to the recorder (clean SOI…EOI boundaries -> a valid MJPEG stream,
        so recordings aren't corrupt), and the LAST one is decoded for display."""
        buf = self._buf
        rec = self._record_fh
        last = None
        while True:
            start = buf.find(b"\xff\xd8")
            if start == -1:
                if len(buf) > 8 * 1024 * 1024:     # no SOI in a huge buffer -> trim
                    del buf[:-1024 * 1024]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end == -1:
                if start:                          # drop junk before the next SOI
                    del buf[:start]
                break
            frame = bytes(buf[start:end + 2])
            del buf[:end + 2]
            if rec is not None:
                try:
                    rec.write(frame)
                except Exception:
                    pass
            last = frame
        if last is None:
            return
        img = QImage.fromData(last, "JPG")          # decode here, not on the UI thread
        if img.isNull():
            return
        with self._lock:
            self._img = img
            self._raw = last
            self.frames += 1

    def latest_image(self):
        with self._lock:
            return self._img

    def latest_raw(self):
        with self._lock:
            return self._raw

    def set_recorder(self, fh):
        self._record_fh = fh

    def stop(self):
        self._alive = False


class _LocalPrep(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(str, list)        # ffmpeg path, cameras
    fail = pyqtSignal(str)

    def run(self):
        try:
            ff = ffmpeg_tools.ensure_local_ffmpeg(self.progress.emit)
            self.done.emit(ff, ffmpeg_tools.list_local_cameras(ff))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _RemotePrep(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(object, str, list, str)   # handler, remote ffmpeg, cameras, diag
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
            cams = list((r.value if isinstance(r, OperationResult) else r) or [])
            diag = ""
            if not cams:
                # No cameras -> capture the raw ffmpeg enumeration so the user can
                # see WHY (privacy block, none attached, ffmpeg error, …) instead
                # of a silent "no camera".
                probe = h.run(f'"{remote}" -hide_banner -list_devices true '
                              f'-f dshow -i dummy', safe=False, timeout=20)
                diag = ((getattr(probe, "stdout", "") or "") + "\n" +
                        (getattr(probe, "stderr", "") or "")).strip()
            self.done.emit(h, remote, cams, diag)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _RemoteProbe(QThread):
    """When the remote camera yields no video, run a short ffmpeg capture with
    verbose logging on the RDP machine and return its output, so the real reason
    (device in use / privacy block / can't open in this session) is visible
    instead of a blind 'force open?'. Bounded to ~10s and closes the channel
    (killing ffmpeg) so a hung dshow open can't wedge anything."""
    done = pyqtSignal(str)

    def __init__(self, handler, ffmpeg, camera):
        super().__init__()
        self.handler, self.ffmpeg, self.camera = handler, ffmpeg, camera

    def run(self):
        out = b""
        try:
            cmd = (f'"{self.ffmpeg}" -hide_banner -loglevel verbose -f dshow '
                   f'-rtbufsize 16M -i video="{self.camera}" -frames:v 1 -f null -')
            chan = self.handler._client.get_transport().open_session()
            chan.settimeout(1.0)
            chan.exec_command(cmd)
            t0 = time.time()
            while time.time() - t0 < 10:
                progressed = False
                for recv, ready in ((chan.recv_stderr, chan.recv_stderr_ready),
                                    (chan.recv, chan.recv_ready)):
                    try:
                        if ready():
                            d = recv(65536)
                            if d:
                                out += d; progressed = True
                    except Exception:
                        pass
                if chan.exit_status_ready() and not chan.recv_stderr_ready() \
                        and not chan.recv_ready():
                    break
                if not progressed:
                    time.sleep(0.1)
            try:
                chan.close()
            except Exception:
                pass
        except Exception as exc:
            out += f"\n[probe error] {type(exc).__name__}: {exc}".encode()
        text = out.decode("utf-8", "replace").strip()[:3000]
        self.done.emit(text or
                       "ffmpeg produced no output in 10s — it most likely hung "
                       "opening the camera, which usually means this SSH session "
                       "can't reach the device without an interactive desktop "
                       "logged in on that machine.")


class _RemoteStart(QThread):
    """Open the remote webcam channel off the UI thread — it now clears any stale
    ffmpeg and waits for the binary-clean tunnel to connect, which takes a moment."""
    ok = pyqtSignal(object)         # data channel
    fail = pyqtSignal(str)

    def __init__(self, handler, camera, ffmpeg, width, height, fps, force):
        super().__init__()
        self.handler, self.camera, self.ffmpeg = handler, camera, ffmpeg
        self.width, self.height, self.fps, self.force = width, height, fps, force

    def run(self):
        try:
            chan = self.handler.webcam_channel(
                self.camera, ffmpeg=self.ffmpeg, width=self.width,
                height=self.height, fps=self.fps, force=self.force)
            self.ok.emit(chan)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class CameraPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, store=None, parent=None):
        super().__init__(parent)
        self._handler = None
        self._remote_ffmpeg = None
        self._local_ffmpeg = None
        self._proc = None
        self._chan = None
        self.reader = None
        self._rec_proc = None
        self._rec_path = None
        self._paused = False
        self._busy_checked = False
        self._dl_dialog = None

        lay = QVBoxLayout(self)

        # --- source row ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Source:"))
        self.source = QComboBox()
        self.source.addItem("Local (this PC)", "local")
        self.source.addItem("Remote (RDP / Windows machine)", "remote")
        self.source.currentIndexChanged.connect(self._source_changed)
        top.addWidget(self.source)
        top.addWidget(QLabel("Camera:"))
        self.camera = QComboBox(); top.addWidget(self.camera, 2)
        top.addWidget(QLabel("Quality:"))
        self.res = QComboBox(); self.res.addItems(list(_RES.keys()))
        self.res.setCurrentText("720p (1280×720)")
        top.addWidget(self.res)
        self.fps = QComboBox(); self.fps.addItems(["15", "20", "25", "30"])
        self.fps.setCurrentText("25")
        top.addWidget(self.fps)
        top.addWidget(QLabel("View:"))
        self.view_mode = QComboBox()
        self.view_mode.addItem("Fill (no bars)", "fill")
        self.view_mode.addItem("Fit (whole frame)", "fit")
        self.view_mode.addItem("Stretch", "stretch")
        self.view_mode.setToolTip("Fill = no black bars, edges may be cropped.\n"
                                  "Fit = the whole frame, with thin bars where the "
                                  "shape doesn't match.\nStretch = fill exactly "
                                  "(slight distortion).")
        self.view_mode.currentIndexChanged.connect(lambda *_: self._repaint_now())
        top.addWidget(self.view_mode)
        self.refresh_btn = QPushButton("🔍 Scan cameras"); self.refresh_btn.setProperty("role", "ghost")
        self.refresh_btn.setToolTip("Scan this source for available cameras.")
        self.refresh_btn.clicked.connect(self._refresh)
        self.start_btn = QPushButton("▶ Start"); self.start_btn.setProperty("role", "ok")
        self.start_btn.clicked.connect(self._toggle_start)
        top.addWidget(self.refresh_btn); top.addWidget(self.start_btn)
        # keep the combos readable (the earlier Ignored policy collapsed them to a
        # dot) but allow modest shrinking so the panel still resizes in a split.
        for _cb in (self.source, self.camera, self.res, self.fps, self.view_mode):
            _cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.source.setMinimumWidth(110); self.camera.setMinimumWidth(120)
        self.res.setMinimumWidth(95); self.fps.setMinimumWidth(55)
        self.view_mode.setMinimumWidth(95)
        lay.addLayout(top)

        # --- remote connection row (hidden unless Source = Remote) ---
        self.remote_row = QWidget()
        rl = QHBoxLayout(self.remote_row); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("RDP host:"))
        self.r_host = HostCombo()
        self.r_host.setText(settings_mod.get("jump_host") or "")
        self.r_host.setPlaceholderText("RDP machine IP — or pick a saved machine")
        self.r_host.activated.connect(self._machine_picked)
        self.r_user = QLineEdit(settings_mod.get("jump_user") or ""); self.r_user.setPlaceholderText("user")
        self.r_domain = QLineEdit(settings_mod.get("jump_domain") or ""); self.r_domain.setPlaceholderText("domain")
        self.r_pass = QLineEdit(settings_mod.jump_password()); self.r_pass.setEchoMode(QLineEdit.Password)
        self.r_pass.setPlaceholderText("password")
        rl.addWidget(self.r_host, 2); rl.addWidget(QLabel("user:")); rl.addWidget(self.r_user, 1)
        rl.addWidget(QLabel("domain:")); rl.addWidget(self.r_domain, 1)
        rl.addWidget(QLabel("pass:")); rl.addWidget(self.r_pass, 1)
        self.remote_row.setVisible(False)
        lay.addWidget(self.remote_row)

        # --- the view (fills the tab) ---
        self.view = QLabel("Pick a source and camera, then Start.")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.view.setMinimumSize(120, 90)        # small min so it resizes in a split
        # near-black that matches the dark panels, so any letterbox bars in "Fit"
        # mode read as deliberate framing rather than a broken black gap.
        self.view.setStyleSheet("background:#0d1014; color:#8a8a8a; border-radius:6px;")
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._view_menu)
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
        row.addWidget(QLabel("Rotate:"))
        self.rotate = QComboBox()
        for label, deg in (("0°", 0), ("90°", 90), ("180°", 180), ("270°", 270)):
            self.rotate.addItem(label, deg)
        self.rotate.setToolTip("Rotate the view (and snapshots) — useful for a camera "
                               "mounted sideways or upside-down.")
        self.rotate.currentIndexChanged.connect(lambda *_: self._repaint_now())
        row.addWidget(self.rotate)
        self.fps_lbl = QLabel(""); self.fps_lbl.setStyleSheet("color:#8a8a8a;")
        row.addWidget(self.fps_lbl)
        row.addStretch(1)
        self.status = QLabel("")
        row.addWidget(self.status)
        self._set_status("Pick a source and camera, then Start.", "idle")
        self.link = QLabel(""); self.link.setOpenExternalLinks(True)
        self.link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        row.addWidget(self.link)
        lay.addLayout(row)

        self._last_paint = 0
        self._fps_count = 0
        self._fps_t0 = time.time()
        self._timer = QTimer(self); self._timer.timeout.connect(self._tick)
        self._timer.start(30)

        QTimer.singleShot(0, self._refresh)

    # ---- status toast (coloured pill) ----
    _STATUS = {
        "idle":  ("#9aa0a6", "transparent"),
        "info":  ("#ffffff", "#1f6feb"),     # blue   — working / connecting
        "ok":    ("#ffffff", "#238636"),     # green  — viewing / found
        "rec":   ("#ffffff", "#cf222e"),     # red    — recording
        "warn":  ("#241a00", "#d29922"),     # amber  — nothing found / attention
        "error": ("#ffffff", "#cf222e"),     # red    — failure
    }

    def _set_status(self, text, kind="idle"):
        fg, bg = self._STATUS.get(kind, self._STATUS["idle"])
        if bg == "transparent":
            self.status.setStyleSheet(f"color:{fg}; padding:2px 4px;")
        else:
            self.status.setStyleSheet(f"color:{fg}; background:{bg}; padding:2px 10px;"
                                      f"border-radius:9px; font-weight:600;")
        self.status.setText(text)

    # ---- one-time ffmpeg setup popup ----
    def _on_progress(self, msg):
        """Prep-thread progress: update the toast, and show a real progress popup
        for the one-time ffmpeg download/upload so it's obvious something big is
        happening (it's ~160 MB)."""
        self._set_status(msg, "info")
        low = msg.lower()
        if any(k in low for k in ("download", "uploading", "extract")):
            self._ensure_dl_dialog()
            if self._dl_dialog is not None:
                self._dl_dialog.setLabelText(msg)
                import re
                m = re.search(r"\((\d+)%\)", msg)
                if m:
                    self._dl_dialog.setRange(0, 100)
                    self._dl_dialog.setValue(int(m.group(1)))
                else:                                  # no percentage -> busy bar
                    self._dl_dialog.setRange(0, 0)

    def _ensure_dl_dialog(self):
        if self._dl_dialog is not None:
            return
        dlg = QProgressDialog("Setting up ffmpeg (one-time, ~160 MB)…", None, 0, 100, self)
        dlg.setWindowTitle("TurboSSH — camera setup")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False); dlg.setAutoReset(False)
        dlg.setCancelButton(None)
        dlg.setValue(0)
        self._dl_dialog = dlg
        dlg.show()

    def _close_dl_dialog(self):
        if self._dl_dialog is not None:
            try:
                self._dl_dialog.close()
            except Exception:
                pass
            self._dl_dialog = None

    def _repaint_now(self):
        """Force a re-scale of the current frame (e.g. after changing View mode)."""
        self._last_paint = -1

    def _source_changed(self, *_):
        remote = self.source.currentData() == "remote"
        self.remote_row.setVisible(remote)
        self._stop_stream()
        if self._handler is not None:            # drop any previous remote connection
            try:
                self._handler.disconnect()
            except Exception:
                pass
            self._handler = None
        self.camera.clear()
        self.start_btn.setEnabled(False)
        if remote:
            self._set_status("Enter the RDP machine's details above, then Scan cameras.", "idle")
        else:
            self._refresh()                      # local auto-lists (no creds needed)

    def _machine_picked(self, *_):
        """When a saved machine is chosen from the RDP host drop-down, fill in its
        user/domain (and the shared jump password if it's the jump host)."""
        host = self.r_host.text()
        m = self.r_host.machine_for(host)
        if m:
            if m.get("user"):
                self.r_user.setText(m["user"])
            if m.get("domain"):
                self.r_domain.setText(m["domain"])
        if host and host == (settings_mod.get("jump_host") or "").strip():
            if not self.r_user.text():
                self.r_user.setText(settings_mod.get("jump_user") or "")
            if not self.r_domain.text():
                self.r_domain.setText(settings_mod.get("jump_domain") or "")
            if not self.r_pass.text():
                self.r_pass.setText(settings_mod.jump_password())

    def _res(self):
        return _RES.get(self.res.currentText(), (1280, 720))

    # ---- enumerate ----
    def _refresh(self):
        self._stop_stream()
        self.camera.clear()
        self.refresh_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        if self.source.currentData() == "local":
            self._set_status("Finding local cameras…", "info")
            self._prep = _LocalPrep()
            self._prep.progress.connect(self._on_progress)
            self._prep.done.connect(self._local_ready)
            self._prep.fail.connect(self._prep_fail)
            self._prep.start()
        else:
            host = self.r_host.text().strip()
            if not host:
                self._set_status("Enter the RDP machine's host above.", "warn")
                self.refresh_btn.setEnabled(True)
                return
            cfg = SSHConfig(host=host, port=22,
                            username=self.r_user.text().strip() or None,
                            domain=self.r_domain.text().strip() or None,
                            password=self.r_pass.text(), host_key_policy="ignore")
            self._set_status(f"Connecting to {host} and finding cameras…", "info")
            self._prep = _RemotePrep(cfg)
            self._prep.progress.connect(self._on_progress)
            self._prep.done.connect(self._remote_ready)
            self._prep.fail.connect(self._prep_fail)
            self._prep.start()

    def _local_ready(self, ffmpeg, cams):
        self._close_dl_dialog()
        self.refresh_btn.setEnabled(True)
        self._local_ffmpeg = ffmpeg
        self._handler = None
        self._fill(cams)

    def _remote_ready(self, handler, remote_ffmpeg, cams, diag):
        self._close_dl_dialog()
        self.refresh_btn.setEnabled(True)
        self._handler = handler
        self._remote_ffmpeg = remote_ffmpeg
        self._fill(cams, diag)

    def _fill(self, cams, diag=""):
        self.camera.clear()
        for c in cams:
            self.camera.addItem(c)
        if cams:
            self._set_status(f"{len(cams)} camera(s) — Start to view.", "ok")
            self.start_btn.setEnabled(True)
        else:
            self._set_status("No cameras found on this source.", "warn")
            if diag:
                # Show exactly what ffmpeg reported on the remote machine so the
                # cause is visible (none attached / privacy block / ffmpeg error).
                QMessageBox.information(
                    self, "No camera found on the RDP machine",
                    "ffmpeg didn't report a camera on the remote machine. Its raw "
                    "device listing is below — if a camera should be there, check "
                    "Windows camera privacy ('Let desktop apps access your camera') "
                    "and that nothing else is using it.\n\n" + diag[:4000])

    def _prep_fail(self, msg):
        self._close_dl_dialog()
        self.refresh_btn.setEnabled(True)
        self._set_status("Couldn't list cameras.", "error")
        QMessageBox.warning(self, "Camera", f"Couldn't list cameras:\n\n{msg}")

    # ---- start / stop ----
    def _toggle_start(self):
        if self.reader is not None:
            self._stop_stream(); self.start_btn.setText("▶ Start"); return
        cam = self.camera.currentText().strip()
        if not cam:
            return
        self._busy_checked = False
        w, h = self._res(); fps = int(self.fps.currentText())
        if self.source.currentData() == "local":
            self._start_local(cam, w, h, fps)
        else:
            self._start_remote(cam, w, h, fps, force=False)

    def _start_local(self, cam, w, h, fps):
        if not self._local_ffmpeg:
            self._refresh(); return
        try:
            args = ffmpeg_tools.local_capture_args(self._local_ffmpeg, cam,
                                                   width=w, height=h, fps=fps)
            self._proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL,
                                          bufsize=0, creationflags=_NO_WINDOW)
        except Exception as exc:
            QMessageBox.warning(self, "Camera", f"Couldn't start the camera:\n\n{exc}")
            return
        self.reader = _FrameReader(lambda n, p=self._proc: p.stdout.read(n))
        self.reader.start()
        self._after_start(cam)

    def _start_remote(self, cam, w, h, fps, force):
        if self._handler is None:
            QMessageBox.information(self, "Camera",
                                   "Click Scan cameras first to connect to the RDP "
                                   "machine and list its cameras.")
            return
        # opening the tunnel clears stale ffmpeg + waits for connect -> do it off
        # the UI thread so the app doesn't freeze for a second or two.
        self.start_btn.setEnabled(False)
        self._set_status(f"Starting {cam}…", "info")
        self._starter = _RemoteStart(self._handler, cam, self._remote_ffmpeg, w, h, fps, force)
        self._starter.ok.connect(lambda chan, c=cam, a=(w, h, fps): self._remote_started(chan, c, a))
        self._starter.fail.connect(self._remote_start_fail)
        self._starter.start()

    def _remote_started(self, chan, cam, whfps):
        self.start_btn.setEnabled(True)
        self._chan = chan
        self._chan.settimeout(0.1)
        # webcam_channel already read some bytes to verify the stream — replay them
        # first so the frames it proved aren't lost.
        prebuf = [getattr(chan, "_prebuf", b"")]

        def read_fn(n, c=self._chan, first=prebuf):
            if first[0]:
                d, first[0] = first[0], b""
                return d
            if c.closed or c.eof_received:
                return None
            try:
                return c.recv(n)
            except Exception as exc:
                return b"" if exc.__class__.__name__ == "timeout" else None

        self.reader = _FrameReader(read_fn)
        self.reader.start()
        self._after_start(cam)
        w, h, fps = whfps
        QTimer.singleShot(8000, lambda: self._check_busy(cam, w, h, fps))

    def _remote_start_fail(self, msg):
        self.start_btn.setEnabled(True)
        self._set_status("Couldn't start the camera", "error")
        QMessageBox.warning(self, "Camera", f"Couldn't start the camera:\n\n{msg}")

    def _after_start(self, cam):
        self.start_btn.setText("⏹ Stop")
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(True)
        self._set_status(f"● Viewing {cam}", "ok")
        self._fps_count = 0; self._fps_t0 = time.time()

    def _check_busy(self, cam, w, h, fps):
        if self.reader is None or self.reader.frames > 0 or self._busy_checked:
            return
        self._busy_checked = True
        # 1) whatever the streaming ffmpeg already wrote to stderr (read while the
        #    channel is still open) — often the direct reason.
        err = self._drain_remote_stderr()
        handler, ffmpeg = self._handler, self._remote_ffmpeg
        self._stop_stream()                      # release the camera before probing
        if err:
            self._remote_no_video(cam, err, probed=False)
            return
        # 2) ffmpeg was silent -> run a short verbose capture probe to find out why
        if handler is None:
            self._set_status("Remote camera: no video", "error"); return
        self._set_status("Diagnosing the remote camera…", "info")
        self._probe = _RemoteProbe(handler, ffmpeg, cam)
        self._probe.done.connect(lambda out, c=cam: self._remote_no_video(c, out, True))
        self._probe.start()

    def _remote_no_video(self, cam, detail, probed):
        self._set_status("Remote camera: no video", "error")
        head = f"ffmpeg on the RDP machine produced no video from “{cam}”."
        if probed:
            head += " A short diagnostic capture was run; its output is below."
        r = QMessageBox.question(
            self, "Remote camera — no video",
            f"{head}\n\n{detail}\n\n"
            "Most likely:\n"
            "• The camera is in use by another app on that machine — close it.\n"
            "• Windows camera privacy is blocking it — turn ON 'Let desktop apps "
            "access your camera' on the RDP machine.\n"
            "(You do NOT normally need to be signed into the desktop — the camera "
            "opens fine over SSH.)\n\n"
            "Try again, forcing any stuck ffmpeg closed first?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes and self._handler is not None:
            self._busy_checked = False
            w, h = self._res(); fps = int(self.fps.currentText())
            self._start_remote(cam, w, h, fps, force=True)

    def _drain_remote_stderr(self):
        """Pull any stderr text the remote ffmpeg emitted. With the TCP-tunnel
        transport, ffmpeg's stderr is on the exec channel, not the data channel."""
        c = self._chan
        if c is None:
            return ""
        c = getattr(c, "_ffmpeg_exec", c)
        out = b""
        try:
            for _ in range(40):
                if c.recv_stderr_ready():
                    chunk = c.recv_stderr(65536)
                    if not chunk:
                        break
                    out += chunk
                else:
                    break
        except Exception:
            pass
        return out.decode("utf-8", "replace").strip()[:1500]

    # ---- display (cheap: just scale an already-decoded QImage) ----
    def _tick(self):
        if self._paused or self.reader is None:
            return
        n = self.reader.frames
        if n == self._last_paint:            # no new frame -> don't rescale again
            return
        self._last_paint = n
        img = self.reader.latest_image()
        if img is None:
            return
        deg = self.rotate.currentData() or 0
        if deg:
            img = img.transformed(QTransform().rotate(deg))
        target = self.view.size()
        mode = self.view_mode.currentData()
        # Smooth (bilinear) scaling -> much less blocky/blurry than fast nearest-
        # neighbour, especially when enlarging the camera image to fill the tab.
        sm = Qt.SmoothTransformation
        if mode == "stretch":
            # fill the view exactly, ignoring aspect ratio (slight distortion)
            pm = QPixmap.fromImage(img).scaled(target, Qt.IgnoreAspectRatio, sm)
        elif mode == "fit":
            # the WHOLE frame, centred; thin bars where shapes don't match
            pm = QPixmap.fromImage(img).scaled(target, Qt.KeepAspectRatio, sm)
        else:
            # "fill": cover the view with no bars, centre-cropping any overflow
            pm = QPixmap.fromImage(img).scaled(target, Qt.KeepAspectRatioByExpanding, sm)
            if pm.width() > target.width() or pm.height() > target.height():
                x = max(0, (pm.width() - target.width()) // 2)
                y = max(0, (pm.height() - target.height()) // 2)
                pm = pm.copy(x, y, target.width(), target.height())
        self.view.setPixmap(pm)
        self._fps_count += 1
        now = time.time()
        if now - self._fps_t0 >= 1.0:
            self.fps_lbl.setText(f"{self._fps_count} fps")
            self._fps_count = 0; self._fps_t0 = now

    # ---- controls ----
    def _current_frame_image(self):
        """The current frame as a QImage, rotated to match what's on screen."""
        img = self.reader.latest_image() if self.reader else None
        if img is None:
            return None
        deg = self.rotate.currentData() or 0
        return img.transformed(QTransform().rotate(deg)) if deg else img

    def _view_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        from . import theme as _t
        m = QMenu(self)
        a_copy = m.addAction(_t.emoji_icon("📋"), "Copy image to clipboard")
        a_snap = m.addAction(_t.emoji_icon("📷"), "Save snapshot…")
        has = self.reader is not None and self.reader.latest_image() is not None
        a_copy.setEnabled(has); a_snap.setEnabled(has)
        chosen = m.exec_(self.view.mapToGlobal(pos))
        if chosen == a_copy:
            self._copy_frame()
        elif chosen == a_snap:
            self._snapshot()

    def _copy_frame(self):
        from PyQt5.QtWidgets import QApplication
        img = self._current_frame_image()
        if img is None:
            self._set_status("No frame to copy yet.", "warn"); return
        QApplication.clipboard().setImage(img)
        self._set_status("Frame copied to clipboard ✓", "ok")
        self.log.emit("[OK] camera frame copied to clipboard")

    def _snapshot(self):
        raw = self.reader.latest_raw() if self.reader else None
        if not raw:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save snapshot",
                                              f"snapshot-{int(time.time())}.jpg",
                                              "Images (*.jpg *.png)")
        if not path:
            return
        try:
            deg = self.rotate.currentData() or 0
            if deg:
                # match the on-screen rotation in the saved file
                img = QImage.fromData(raw, "JPG").transformed(QTransform().rotate(deg))
                img.save(path)
            else:
                with open(path, "wb") as fh:
                    fh.write(raw)
            self._show_link(path)
            self.log.emit(f"[OK] snapshot saved: {path}")
        except Exception as exc:
            self.log.emit(f"[ERROR] snapshot: {exc}")

    def _toggle_record(self):
        if self._rec_proc is not None:
            self._stop_record(); return
        if self.reader is None:
            return
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
            # Feed the clean MJPEG frames to ffmpeg and RE-ENCODE to H.264 MP4 — a
            # file that plays in any player. ``-use_wallclock_as_timestamps`` time-
            # stamps each frame as it arrives, so the recording runs at real speed
            # even if the camera can't sustain the requested fps (the old
            # ``-c copy`` into .mp4 produced unplayable/corrupt files).
            self._rec_proc = subprocess.Popen(
                [ff, "-y",
                 "-f", "mjpeg", "-use_wallclock_as_timestamps", "1", "-i", "-",
                 "-an", "-c:v", "libx264", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", "-fps_mode", "vfr",
                 "-movflags", "+faststart", path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            self._rec_path = path
            self.reader.set_recorder(self._rec_proc.stdin)
            self.rec_btn.setText("⏺ Stop recording")
            self._set_status("● Recording…", "rec")
            self.log.emit(f"[OK] recording to {path}")
        except Exception as exc:
            self._rec_proc = None
            self.log.emit(f"[ERROR] record: {exc}")

    def _stop_record(self):
        if self.reader:
            self.reader.set_recorder(None)
        proc, self._rec_proc = self._rec_proc, None
        if proc is None:
            return                       # not recording -> nothing to save (no spam)
        try:
            proc.stdin.close()           # flush remaining frames + write the trailer
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self.rec_btn.setText("⏺ Record")
        path, self._rec_path = self._rec_path, None   # clear so we report it once
        if path:
            self._show_link(path)
            self.log.emit(f"[OK] recording saved: {path}")
            if self.reader is not None:               # still streaming
                self._set_status("Recording saved ✓ — still viewing", "ok")

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause_btn.setText("▶ Resume" if self._paused else "⏸ Pause")

    def _show_link(self, path):
        folder = os.path.dirname(os.path.abspath(path)).replace("\\", "/")
        self.link.setText(f'Saved — <a href="file:///{folder}">open folder</a>')

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
            exec_c = getattr(self._chan, "_ffmpeg_exec", None)
            try:
                self._chan.close()
            except Exception:
                pass
            if exec_c is not None:       # also end the ffmpeg exec (TCP-tunnel mode)
                try:
                    exec_c.close()
                except Exception:
                    pass
            self._chan = None
        if self._handler is not None:
            try:
                self._handler.webcam_release(safe=True)
            except Exception:
                pass
        for b in (self.snap_btn, self.rec_btn, self.pause_btn):
            b.setEnabled(False)
        self._paused = False
        self.pause_btn.setText("⏸ Pause")
        self.start_btn.setText("▶ Start")     # never leave 'Stop' showing with no stream
        self.fps_lbl.setText("")
        # clear the frozen last frame -> blank view instead of a stale snapshot
        self.view.clear()
        self.view.setText("Camera stopped — Start to view again.")
        self._set_status("Stopped.", "idle")

    def close_session(self):
        self._close_dl_dialog()
        try:
            self._timer.stop()
        except Exception:
            pass
        self._stop_stream()
        # wait for any helper QThreads (prep / start / probe) so they don't
        # outlive the widget and trip "QThread destroyed while running" on exit.
        for attr in ("_prep", "_starter", "_probe"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.wait(2000)
                except Exception:
                    pass
        if self._handler is not None:
            try:
                self._handler.disconnect()
            except Exception:
                pass
            self._handler = None
