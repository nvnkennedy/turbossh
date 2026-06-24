"""Dialog to create / edit a saved session (SSH / Serial).

Designed to be self-explanatory: every field has a tooltip and each group has a
one-line description so a first-time user knows exactly what to type and when.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QLineEdit, QSpinBox, QComboBox, QCheckBox,
                             QDialogButtonBox, QGroupBox, QLabel, QRadioButton,
                             QButtonGroup, QCompleter, QScrollArea, QWidget,
                             QPushButton, QMessageBox)

from .sessions import SessionStore


class _PortScanThread(QThread):
    """Connect over SSH and list the remote host's serial ports (off the UI
    thread so the dialog never freezes during the scan)."""
    done = pyqtSignal(list)
    fail = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        from ..core import SSHHandler
        from ..results import OperationResult
        h = None
        try:
            h = SSHHandler(self.cfg, safe=True)
            res = h.connect()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error)); return
            r = h.remote_serial_ports()
            ports = r.value if isinstance(r, OperationResult) else r
            self.done.emit(list(ports or []))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if h is not None:
                try:
                    h.disconnect()
                except Exception:
                    pass


class _CamScanThread(QThread):
    """Connect over SSH, make sure ffmpeg is on the remote, and list its cameras."""
    progress = pyqtSignal(str)
    done = pyqtSignal(list)
    fail = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        from ..core import SSHHandler
        from ..results import OperationResult
        from . import ffmpeg_tools
        h = None
        try:
            local = ffmpeg_tools.ensure_local_ffmpeg(self.progress.emit)
            h = SSHHandler(self.cfg, safe=True)
            res = h.connect()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error)); return
            remote = ffmpeg_tools.ensure_remote_ffmpeg(h, local, self.progress.emit)
            r = h.list_cameras(ffmpeg=remote)
            cams = r.value if isinstance(r, OperationResult) else r
            self.done.emit(list(cams or []))
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if h is not None:
                try:
                    h.disconnect()
                except Exception:
                    pass


def _hint(text: str) -> QLabel:
    """A small dim description line shown under a group title."""
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color:#8a8a8a; font-size:8.5pt;")
    return lab


class SessionDialog(QDialog):
    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("New / edit session")
        self.setMinimumWidth(620)

        # The body scrolls ONLY if the dialog gets capped by the screen height
        # (e.g. RDP fields shown on a short screen). Normally it fits and shows
        # no scrollbar. The OK/Cancel buttons live OUTSIDE the scroll area, so
        # they're always visible at the bottom — never pushed off-screen.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        self._scroll.setWidget(self._body)
        outer.addWidget(self._scroll, 1)

        from . import theme

        # --- name ---
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("optional — defaults to the host / device")
        self.name.setToolTip("A label for this saved session — shown in the sidebar list. "
                             "Leave blank and it's named after the host (or COM port).")
        form.addRow("Name", self.name)
        lay.addLayout(form)

        # --- connection-type radio buttons ---
        type_box = QGroupBox("Connection type")
        tv = QVBoxLayout(type_box)
        tv.addWidget(_hint("SSH = log into a Linux/QNX machine and get a terminal.\n"
                           "Serial = talk to a COM port / debug board (optionally on a remote machine)."))
        trow = QHBoxLayout()
        self.rb = {"ssh": QRadioButton("SSH"),
                   "serial": QRadioButton("Serial")}
        # Camera is an opt-in feature — only offer it when enabled in Settings.
        from . import settings as _s
        self._camera_enabled = bool(_s.get("camera_enabled"))
        if self._camera_enabled:
            self.rb["camera"] = QRadioButton("Camera")
        self._grp = QButtonGroup(self)
        for k, b in self.rb.items():
            self._grp.addButton(b)
            # reserve room so the styled indicator never clips the label
            # ("Serial" was rendering as "Seria")
            b.setMinimumWidth(96)
            trow.addWidget(b)            # connect/check AFTER the field groups exist
        trow.addStretch(1)
        tv.addLayout(trow)
        lay.addWidget(type_box)

        # --- SSH group ---
        self.ssh_box = QGroupBox("SSH")
        sv = QVBoxLayout(self.ssh_box)
        self.ssh_hint = _hint("The machine you want a shell on. For a QNX/Linux "
                              "target reached directly from your laptop, fill this in.")
        sv.addWidget(self.ssh_hint)
        sf = QFormLayout()
        self._ssh_form = sf
        sv.addLayout(sf)
        self.host = QLineEdit(); self.host.setPlaceholderText("IP address, e.g. 192.168.1.50")
        self.host.setToolTip("IP address of the target machine.")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(22)
        self.port.setToolTip("SSH port (22 unless changed).")
        self.user = QLineEdit(); self.user.setPlaceholderText("login user, e.g. root")
        self.user.setToolTip("Username to log in as.")
        self.domain = QLineEdit()        # kept (hidden) for storage compatibility
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("leave blank if the target needs no password / uses keys")
        self.password.setToolTip("Password for the target. Blank is fine for key-based "
                                 "or no-auth lab targets.")
        theme.attach_eye(self.password)
        for label, w in (("Host", self.host), ("Port", self.port),
                         ("User", self.user), ("Password", self.password)):
            sf.addRow(label, w)

        self.ignore = QCheckBox("Ignore host key (lab / embedded)")
        self.ignore.setToolTip("Don't verify the SSH host key. Convenient for devices "
                               "that get re-imaged often.")
        self.ignore.setChecked(True)
        self.legacy = QCheckBox("Enable legacy algorithms (old ECUs)")
        self.legacy.setToolTip("Allow old ciphers/kex some embedded SSH servers need.")
        sv.addWidget(self.ignore); sv.addWidget(self.legacy)

        # jump host: ticking the box reveals the RDP details group right below.
        self.use_jump = QCheckBox("Connect through a jump host (RDP machine)")
        self.use_jump.setToolTip("Tunnel through an intermediate machine first — e.g. your "
                                 "laptop → RDP Windows box → the target. Defaults come from Settings.")
        self.use_jump.toggled.connect(self._sync_jump)
        sv.addWidget(self.use_jump)
        lay.addWidget(self.ssh_box)

        # --- RDP / jump-host details (hidden until 'use jump host' is ticked) ---
        self.jump_group = QGroupBox("RDP / jump host")
        jv = QVBoxLayout(self.jump_group)
        jv.addWidget(_hint("The intermediate machine TurboSSH logs into first, then "
                           "hops to the target from there. Pre-filled from "
                           "Settings → Jump host."))
        jf = QFormLayout(); jv.addLayout(jf)
        self.jhost = QLineEdit(); self.jhost.setPlaceholderText("jump host / RDP machine name or IP")
        self.juser = QLineEdit(); self.juser.setPlaceholderText("jump user (e.g. your Windows login)")
        self.jdomain = QLineEdit(); self.jdomain.setPlaceholderText("Windows domain (optional)")
        self.jpass = QLineEdit(); self.jpass.setEchoMode(QLineEdit.Password)
        theme.attach_eye(self.jpass)
        for label, w in (("Jump host", self.jhost), ("Jump user", self.juser),
                         ("Jump domain", self.jdomain), ("Jump password", self.jpass)):
            jf.addRow(label, w)
        self.jump_group.setVisible(False)
        lay.addWidget(self.jump_group)

        # --- Serial group ---
        self.ser_box = QGroupBox("Serial")
        rv = QVBoxLayout(self.ser_box)
        rv.addWidget(_hint("Pick the port and baud rate of the debug board.\n"
                           "Continuous console output streams into the terminal; type in the "
                           "Send box to write to the port."))
        rf = QFormLayout(); rv.addLayout(rf)
        self.device = QComboBox(); self.device.setEditable(True)
        self.device.setToolTip("Serial device — e.g. COM6 on Windows or /dev/ser1 on QNX/Linux.")
        try:
            from ..serial_handler import list_serial_ports
            ports = [p["device"] for p in list_serial_ports()]
        except Exception:
            ports = []
        self.device.addItems(ports or ["COM1", "COM3", "COM5", "COM6", "/dev/ser1", "/dev/ttyUSB0"])
        # device dropdown + a "Scan" button that lists the available ports
        self.scan_btn = QPushButton("🔍 Scan ports")
        self.scan_btn.setProperty("role", "ghost")
        self.scan_btn.setToolTip("List the serial ports that actually exist. With "
                                 "'reach it over SSH' ticked this scans the REMOTE "
                                 "(RDP) machine; otherwise it scans this PC.")
        self.scan_btn.clicked.connect(self._scan_ports)
        dev_row = QHBoxLayout(); dev_row.setContentsMargins(0, 0, 0, 0)
        dev_row.addWidget(self.device, 1); dev_row.addWidget(self.scan_btn)
        dev_w = QWidget(); dev_w.setLayout(dev_row)
        self.baud = QComboBox(); self.baud.setEditable(True)
        self.baud.setToolTip("Baud rate the board uses (115200 is the common default).")
        self.baud.addItems(["9600", "19200", "38400", "57600", "115200",
                            "230400", "460800", "921600"])
        self.baud.setCurrentText("115200")
        rf.addRow("Device / port", dev_w)
        rf.addRow("Baud", self.baud)
        self.scan_status = QLabel("")
        self.scan_status.setWordWrap(True)
        self.scan_status.setStyleSheet("color:#8a8a8a; font-size:8.5pt;")
        rv.addWidget(self.scan_status)
        self.ser_via_ssh = QCheckBox("Port is on the RDP machine (connect to it remotely)")
        self.ser_via_ssh.setToolTip("Tick this when the COM port is plugged into the "
                                    "Windows RDP machine. Fill the RDP machine's details "
                                    "above; TurboSSH reaches its port over SSH.")
        self.ser_via_ssh.toggled.connect(lambda *_: self._toggle(self._type()))
        rv.addWidget(self.ser_via_ssh)
        self.ser_ssh_hint = _hint("With this on, the section above asks for the RDP "
                                  "machine — TurboSSH logs into it over SSH and reads "
                                  "the COM port there. Use ‘Scan remote’ to list its ports.")
        rv.addWidget(self.ser_ssh_hint)
        lay.addWidget(self.ser_box)

        # --- Camera group (only built when the feature is enabled) ---
        self.cam_box = None
        if self._camera_enabled:
            self.cam_box = QGroupBox("Camera")
            cv = QVBoxLayout(self.cam_box)
            cv.addWidget(_hint("Stream a webcam on the RDP machine. Fill the machine's "
                               "SSH details above, then ‘Scan cameras’ and pick one."))
            cf = QFormLayout(); cv.addLayout(cf)
            self.camera = QComboBox(); self.camera.setEditable(True)
            self.cam_scan = QPushButton("🔍 Scan cameras"); self.cam_scan.setProperty("role", "ghost")
            self.cam_scan.clicked.connect(self._scan_cameras)
            cam_row = QHBoxLayout(); cam_row.setContentsMargins(0, 0, 0, 0)
            cam_row.addWidget(self.camera, 1); cam_row.addWidget(self.cam_scan)
            cam_w = QWidget(); cam_w.setLayout(cam_row)
            self.cam_res = QComboBox()
            self.cam_res.addItems(["640x480", "800x600", "1280x720", "1920x1080"])
            self.cam_res.setCurrentText("640x480")
            self.cam_fps = QComboBox()
            self.cam_fps.addItems(["10", "15", "20", "30"]); self.cam_fps.setCurrentText("15")
            cf.addRow("Camera", cam_w)
            cf.addRow("Resolution", self.cam_res)
            cf.addRow("FPS", self.cam_fps)
            self.cam_status = QLabel(""); self.cam_status.setWordWrap(True)
            self.cam_status.setStyleSheet("color:#8a8a8a; font-size:8.5pt;")
            cv.addWidget(self.cam_status)
            lay.addWidget(self.cam_box)

        lay.addStretch(1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        btns.setContentsMargins(11, 6, 11, 9)
        self._btns = btns
        outer.addWidget(btns)            # outside the scroll area → always visible

        # now the field groups exist — safe to react to type changes
        for b in self.rb.values():
            b.toggled.connect(lambda on: on and self._toggle(self._type()))

        self._add_completers()
        if existing:
            self._load(existing)
        else:
            self.rb["ssh"].setChecked(True)
        self._prefill_jump_defaults()
        self._toggle(self._type())

    def _prefill_jump_defaults(self):
        """Fill jump fields from the shared Settings values if empty."""
        from . import settings as s
        if not self.jhost.text():
            self.jhost.setText(s.get("jump_host") or "")
        if not self.juser.text():
            self.juser.setText(s.get("jump_user") or "")
        if not self.jdomain.text():
            self.jdomain.setText(s.get("jump_domain") or "")
        if not self.jpass.text():
            self.jpass.setText(s.jump_password())

    def accept(self):
        # warn if "via jump host" is on but no jump host is configured anywhere
        uses_jump = (self._type() == "ssh" or
                     (self._type() == "serial" and self.ser_via_ssh.isChecked()))
        if uses_jump and self.use_jump.isChecked() and not self.jhost.text().strip():
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Jump host not set",
                "‘Connect through a jump host’ is enabled but no jump host is "
                "configured.\n\nFill the jump fields here, or set them once in "
                "Settings → Jump host (RDP machine).")
            return
        # gentle nudge if a serial-over-SSH session has no target host
        if (self._type() == "serial" and self.ser_via_ssh.isChecked()
                and not self.host.text().strip()):
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "RDP host required",
                "This port is marked as being on the RDP machine, so set ‘RDP host’ "
                "above to that machine's IP (it's where the port is plugged in).")
            return
        super().accept()

    # remembered values from previously-saved sessions
    def _add_completers(self):
        try:
            store = SessionStore()
            hosts = sorted({s.get("host", "") for s in store.sessions if s.get("host")})
            users = sorted({s.get("user", "") for s in store.sessions if s.get("user")})
            jhosts = sorted({s.get("jhost", "") for s in store.sessions if s.get("jhost")})
            self.host.setCompleter(QCompleter(hosts, self))
            self.user.setCompleter(QCompleter(users, self))
            self.juser.setCompleter(QCompleter(users, self))
            self.jhost.setCompleter(QCompleter(jhosts, self))
        except Exception:
            pass

    def _type(self) -> str:
        for k, b in self.rb.items():
            if b.isChecked():
                return k
        return "ssh"

    def _toggle(self, kind):
        if not hasattr(self, "ssh_box"):
            return
        serial_via_ssh = (kind == "serial" and self.ser_via_ssh.isChecked())
        # Camera and serial-over-RDP both talk to the RDP machine, so the shared
        # connection box re-brands as "RDP machine" in those modes.
        rdp_mode = serial_via_ssh or (kind == "camera")
        self.ssh_box.setVisible(kind == "ssh" or rdp_mode)
        if kind == "camera":
            self.ssh_box.setTitle("RDP machine (the camera is on it)")
        elif serial_via_ssh:
            self.ssh_box.setTitle("RDP machine (the COM port is plugged into it)")
        else:
            self.ssh_box.setTitle("SSH")
        self.ssh_hint.setVisible(not rdp_mode)
        self._relabel_conn_fields(rdp_mode)
        self.ser_box.setVisible(kind == "serial")
        self.ser_ssh_hint.setVisible(serial_via_ssh)
        if self.cam_box is not None:
            self.cam_box.setVisible(kind == "camera")
        if hasattr(self, "scan_btn"):
            self.scan_btn.setText("🔍 Scan remote" if serial_via_ssh else "🔍 Scan ports")
        # Jump host is only for a *plain SSH* session (laptop → jump → target).
        self.use_jump.setVisible(kind == "ssh")
        self._sync_jump()

    def _relabel_conn_fields(self, rdp: bool):
        """Re-label the shared connection fields as the RDP machine's details
        when scanning/streaming a COM port that lives on it."""
        f = self._ssh_form
        pairs = ((self.host, "RDP host", "Host"),
                 (self.port, "SSH port", "Port"),
                 (self.user, "RDP user", "User"),
                 (self.password, "RDP password", "Password"))
        for w, rdp_label, ssh_label in pairs:
            lab = f.labelForField(w)
            if lab is not None:
                lab.setText(rdp_label if rdp else ssh_label)
        # clarify the port is the SSH port to the RDP machine, NOT the COM port
        self.port.setToolTip("SSH port to reach the RDP machine (22 unless changed). "
                             "This is NOT the serial/COM port." if rdp
                             else "SSH port (22 unless changed).")
        if rdp:
            self.host.setPlaceholderText("RDP machine IP, e.g. 192.168.1.50")
            self.host.setToolTip("IP of the Windows RDP machine the COM port is plugged into.")
            self.user.setPlaceholderText("your Windows login on the RDP machine")
            self.password.setPlaceholderText("that Windows login's password (blank if key-based)")
        else:
            self.host.setPlaceholderText("IP address, e.g. 192.168.1.50")
            self.host.setToolTip("IP address of the target machine.")
            self.user.setPlaceholderText("login user, e.g. root")
            self.password.setPlaceholderText("leave blank if the target needs no password / uses keys")

    def _sync_jump(self, *_):
        # jump host applies to a plain SSH session only (not serial-via-RDP)
        show = (self._type() == "ssh") and self.use_jump.isChecked()
        if hasattr(self, "jump_group"):
            self.jump_group.setVisible(show)
        self._refit()

    def _refit(self):
        """Size the dialog to its content height, but never taller than the
        screen. When it fits, the scroll area shows no scrollbar; when capped,
        the body scrolls and the OK/Cancel buttons (outside it) stay visible."""
        self._body.layout().activate()
        body_h = self._body.sizeHint().height()
        btn_h = self._btns.sizeHint().height()
        desired = body_h + btn_h + 4
        cap = desired
        scr = self._screen()
        if scr is not None:
            cap = max(360, scr.availableGeometry().height() - 56)
        h = min(desired, cap)
        w = max(self.minimumWidth(), self._body.sizeHint().width() + 2)
        self.resize(w, h)
        if self.isVisible():
            self._keep_on_screen()

    # ---- scan serial ports (remote over SSH, or local) ----
    def _scan_ports(self):
        remote = self.ser_via_ssh.isChecked()
        if remote:
            if not self.host.text().strip():
                QMessageBox.warning(
                    self, "RDP host needed",
                    "To scan the RDP machine's ports, first enter its ‘RDP host’ (IP) "
                    "and login above — then press Scan remote.")
                return
            from .session_widgets import config_from_session
            cfg = config_from_session(self.result_session(),
                                      self.password_value(), self.jump_password_value())
            self.scan_btn.setEnabled(False)
            self.scan_status.setText("⏳ Scanning the remote machine's serial ports…")
            self._scan = _PortScanThread(cfg)
            self._scan.done.connect(self._scan_done)
            self._scan.fail.connect(self._scan_fail)
            self._scan.start()
        else:
            try:
                from ..serial_handler import list_serial_ports
                ports = [{"device": p["device"],
                          "description": p.get("description") or p["device"]}
                         for p in list_serial_ports()]
            except Exception as exc:
                self._scan_fail(f"{type(exc).__name__}: {exc}"); return
            self._scan_done(ports)

    def _scan_done(self, ports):
        self.scan_btn.setEnabled(True)
        keep = self.device.currentText().strip()
        self.device.clear()
        for p in ports:
            dev = p.get("device", "") if isinstance(p, dict) else str(p)
            if not dev:
                continue
            self.device.addItem(dev)
            desc = p.get("description", dev) if isinstance(p, dict) else dev
            self.device.setItemData(self.device.count() - 1, desc, Qt.ToolTipRole)
        if ports:
            devs = [self.device.itemText(i) for i in range(self.device.count())]
            self.device.setCurrentText(keep if keep in devs else devs[0])
            self.scan_status.setText(f"✓ Found {len(devs)} port(s): "
                                     f"{', '.join(devs)} — pick one above.")
        else:
            if keep:
                self.device.setEditText(keep)
            self.scan_status.setText("No serial ports found on that machine.")
        self._refit()

    def _scan_fail(self, msg):
        self.scan_btn.setEnabled(True)
        self.scan_status.setText("✗ Scan failed (see popup).")
        QMessageBox.warning(
            self, "Couldn't scan ports",
            "Could not list the serial ports:\n\n" + msg +
            "\n\nCheck the Host / User / Password (and jump host, if used) are "
            "correct, then try again.")

    # ---- scan remote cameras (needs ffmpeg on the remote) ----
    def _scan_cameras(self):
        if not self.host.text().strip():
            QMessageBox.warning(self, "RDP host needed",
                                "Enter the RDP machine's host and login above, then "
                                "Scan cameras.")
            return
        from .session_widgets import config_from_session
        cfg = config_from_session(self.result_session(),
                                  self.password_value(), self.jump_password_value())
        self.cam_scan.setEnabled(False)
        self.cam_status.setText("⏳ Preparing ffmpeg and scanning cameras "
                                "(first run downloads ffmpeg)…")
        self._camscan = _CamScanThread(cfg)
        self._camscan.progress.connect(lambda m: self.cam_status.setText(m))
        self._camscan.done.connect(self._cam_scan_done)
        self._camscan.fail.connect(self._cam_scan_fail)
        self._camscan.start()

    def _cam_scan_done(self, cams):
        self.cam_scan.setEnabled(True)
        keep = self.camera.currentText().strip()
        self.camera.clear()
        for c in cams:
            self.camera.addItem(c)
        if cams:
            self.camera.setCurrentText(keep if keep in cams else cams[0])
            self.cam_status.setText(f"✓ Found {len(cams)} camera(s): {', '.join(cams)}")
        else:
            if keep:
                self.camera.setEditText(keep)
            self.cam_status.setText("No cameras found on that machine.")
        self._refit()

    def _cam_scan_fail(self, msg):
        self.cam_scan.setEnabled(True)
        self.cam_status.setText("✗ Scan failed (see popup).")
        QMessageBox.warning(self, "Couldn't scan cameras",
                            "Could not list cameras:\n\n" + msg +
                            "\n\nCheck the RDP host/login, and that ffmpeg could be "
                            "fetched (or set a local ffmpeg path in Settings).")

    def _screen(self):
        from PyQt5.QtWidgets import QApplication
        par = self.parentWidget()
        if par is not None and par.screen() is not None:
            return par.screen()
        return (QApplication.screenAt(self.frameGeometry().center())
                or QApplication.primaryScreen())

    def _keep_on_screen(self):
        """Clamp the window so its whole frame stays inside the visible screen."""
        scr = self._screen()
        if scr is None:
            return
        avail = scr.availableGeometry()
        g = self.frameGeometry()    # includes the title bar / window frame
        x = min(max(g.x(), avail.left()), max(avail.left(), avail.right() - g.width() + 1))
        y = min(max(g.y(), avail.top()), max(avail.top(), avail.bottom() - g.height() + 1))
        dx, dy = x - g.x(), y - g.y()
        if dx or dy:                # move by the delta so the frame offset is kept
            self.move(self.x() + dx, self.y() + dy)

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_centered", False):
            self._centered = True
            self._refit()
            scr = self._screen()
            par = self.parentWidget()
            center = (par.frameGeometry().center() if par is not None and par.isVisible()
                      else (scr.availableGeometry().center() if scr else None))
            if center is not None:
                g = self.frameGeometry(); g.moveCenter(center); self.move(g.topLeft())
            self._keep_on_screen()

    def _load(self, s):
        self.name.setText(s.get("name", ""))
        self.rb.get(s.get("type", "ssh"), self.rb["ssh"]).setChecked(True)
        self.host.setText(s.get("host", "")); self.port.setValue(s.get("port", 22))
        self.user.setText(s.get("user", "")); self.domain.setText(s.get("domain", ""))
        self.ignore.setChecked(s.get("ignore_hostkey", True))
        self.legacy.setChecked(s.get("legacy", False))
        self.use_jump.setChecked(s.get("use_jump", False))
        self.jhost.setText(s.get("jhost", "")); self.juser.setText(s.get("juser", ""))
        self.jdomain.setText(s.get("jdomain", ""))
        self.device.setCurrentText(s.get("device", "COM5"))
        self.baud.setCurrentText(str(s.get("baud", 115200)))
        self.ser_via_ssh.setChecked(s.get("via_ssh", False))
        if self.cam_box is not None:
            self.camera.setCurrentText(s.get("camera", ""))
            self.cam_res.setCurrentText(f"{s.get('cam_width', 640)}x{s.get('cam_height', 480)}")
            self.cam_fps.setCurrentText(str(s.get("cam_fps", 15)))
        self.password.setText(SessionStore.password(s.get("name", "")) or "")
        self.jpass.setText(SessionStore.jump_password(s.get("name", "")) or "")

    def result_session(self) -> dict:
        try:
            baud = int(self.baud.currentText())
        except ValueError:
            baud = 115200
        # Name is optional — if left blank, derive a sensible label so the
        # session is never nameless in the sidebar.
        name = self.name.text().strip()
        kind = self._type()
        if not name:
            if kind == "serial":
                name = self.device.currentText().strip() or "serial"
            elif kind == "camera":
                cam = self.camera.currentText().strip() if self.cam_box else ""
                host = self.host.text().strip()
                name = (f"cam {cam}" if cam else f"camera @ {host}") or "camera"
            else:
                host = self.host.text().strip()
                user = self.user.text().strip()
                name = (f"{user}@{host}" if user and host else host) or "ssh session"
        out = {
            "name": name,
            "type": kind,
            "host": self.host.text().strip(), "port": self.port.value(),
            "user": self.user.text().strip(), "domain": self.domain.text().strip(),
            "ignore_hostkey": self.ignore.isChecked(),
            "legacy": self.legacy.isChecked(),
            "use_jump": self.use_jump.isChecked(),
            "jhost": self.jhost.text().strip(), "juser": self.juser.text().strip(),
            "jdomain": self.jdomain.text().strip(),
            "device": self.device.currentText().strip(), "baud": baud,
            "via_ssh": self.ser_via_ssh.isChecked(),
        }
        if self.cam_box is not None:
            res = (self.cam_res.currentText() or "640x480").lower().split("x")
            try:
                w, h = int(res[0]), int(res[1])
            except (ValueError, IndexError):
                w, h = 640, 480
            try:
                fps = int(self.cam_fps.currentText())
            except ValueError:
                fps = 15
            out.update(camera=self.camera.currentText().strip(),
                       cam_width=w, cam_height=h, cam_fps=fps)
        return out

    def password_value(self) -> str:
        return self.password.text()

    def jump_password_value(self) -> str:
        return self.jpass.text()
