"""Session tab widgets: an SSH tab (interactive terminal + SFTP browser split)
and a Serial tab (interactive serial console). Each connects in a background
thread so the UI never blocks."""

from __future__ import annotations

import socket
import threading

from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QTabWidget, QLineEdit)

from ..config import SSHConfig
from ..core import SSHHandler
from ..serial_handler import SerialHandler
from ..results import OperationResult
from .terminal import ReaderThread
from .vt100 import Vt100Terminal
from .sftp_browser import SftpBrowser
from .logs_tab import LogsTab


def config_from_session(s: dict, password: str, jump_password: str) -> SSHConfig:
    policy = "ignore" if s.get("ignore_hostkey", True) else "auto"
    jump = None
    if s.get("use_jump") and s.get("jhost"):
        jump = SSHConfig(host=s["jhost"], username=s.get("juser") or None,
                         domain=s.get("jdomain") or None, password=jump_password,
                         host_key_policy=policy,
                         enable_legacy_algorithms=s.get("legacy", False))
    return SSHConfig(
        host=s.get("host", ""), port=s.get("port", 22),
        username=s.get("user") or None, domain=s.get("domain") or None,
        password=password, jump_host=jump, host_key_policy=policy,
        enable_legacy_algorithms=s.get("legacy", False),
    )


class _SshConnectThread(QThread):
    ok = pyqtSignal(object, object)     # handler, shell
    fail = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            h = SSHHandler(self.cfg, safe=True)
            res = h.connect()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error)); return
            sh = h.open_shell()
            shell = sh.value if isinstance(sh, OperationResult) else sh
            self.ok.emit(h, shell)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class SshSessionWidget(QWidget):
    log = pyqtSignal(str)
    connected = pyqtSignal(bool, str)        # ok, message  -> main window status
    failed = pyqtSignal(str)                 # error message -> main window popup

    def __init__(self, session: dict, password: str, jump_password: str, parent=None):
        super().__init__(parent)
        self.session = session
        self.handler = None
        self.shell = None
        self.reader = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.status = QLabel("Connecting…")
        lay.addWidget(self.status)

        # inner tabs: Terminal | SFTP | Logs
        self.inner = QTabWidget()
        self.inner.currentChanged.connect(self._on_inner_tab)

        # --- Terminal tab (toolbar + VT100) ---
        term_page = QWidget()
        tlay = QVBoxLayout(term_page); tlay.setContentsMargins(0, 0, 0, 0)
        quick = QHBoxLayout()
        quick.addWidget(QLabel("Quick:"))
        for cmd in ("slog2info", "journalctl", "dmesg", "ls", "ps"):
            b = QPushButton(cmd); b.setProperty("role", "ghost")
            b.clicked.connect(lambda _=False, c=cmd: self._send_cmd(c))
            quick.addWidget(b)
        ctrlc = QPushButton("Ctrl-C"); ctrlc.setProperty("role", "danger")
        ctrlc.setToolTip("Send Ctrl-C (interrupt the running command)")
        ctrlc.clicked.connect(self._interrupt)
        paste = QPushButton("Paste"); paste.setProperty("role", "ghost")
        paste.clicked.connect(lambda: (self.term.paste_clipboard(), self.term.setFocus()))
        clr = QPushButton("Clear"); clr.setProperty("role", "ghost")
        clr.clicked.connect(lambda: (self.term.clear(), self.term.setFocus()))
        quick.addWidget(ctrlc); quick.addWidget(paste); quick.addWidget(clr)
        quick.addStretch(1)
        tlay.addLayout(quick)
        # A real, native VT100 terminal: click in it and type — keystrokes,
        # arrows, Tab-completion, Ctrl-C, paste etc. all go straight to the PTY.
        self.term = Vt100Terminal(send_fn=self._send)
        self.term.resized.connect(self._on_term_resize)
        tlay.addWidget(self.term, 1)
        self.inner.addTab(term_page, "Terminal")

        # --- SFTP tab (filled after connect) ---
        self.sftp_page = QWidget()
        QVBoxLayout(self.sftp_page).addWidget(QLabel("Connecting…"))
        self.inner.addTab(self.sftp_page, "SFTP")

        # --- Logs tab ---
        self.logs_tab = LogsTab()
        self.inner.addTab(self.logs_tab, "Logs")

        lay.addWidget(self.inner, 1)

        cfg = config_from_session(session, password, jump_password)
        self._connect(cfg)

    def _connect(self, cfg):
        self._ct = _SshConnectThread(cfg)
        self._ct.ok.connect(self._on_connected)
        self._ct.fail.connect(self._on_fail)
        self._ct.start()

    def _on_connected(self, handler, shell):
        self.handler = handler
        self.shell = shell
        if shell is None:
            self._on_fail("Connected, but the interactive shell could not open.")
            return
        host = self.session.get("host")
        self.status.setText(f"Connected — {host}")
        self.log.emit(f"[OK] {self.session['name']}: connected")
        self.connected.emit(True, f"Connected to {host}")
        chan = shell.channel
        chan.settimeout(0.1)

        def read_fn():
            if chan.closed or chan.eof_received:
                return None
            try:
                data = chan.recv(65536)
                return None if data == b"" else data
            except socket.timeout:
                return b""
            except Exception:
                return None

        self.reader = ReaderThread(read_fn)
        self.reader.closed.connect(self._on_disconnected)
        self.reader.start()
        self.term.set_source(self.reader.pull)        # GUI drains on a timer
        # keyboard Ctrl-C should also drop the buffered flood, not just the button
        self.term.on_interrupt = lambda: self.reader.flush() if self.reader else None
        try:
            self.shell.resize(self.term.cols, self.term.rows)
        except Exception:
            pass

        # focus the working terminal FIRST so an SFTP hiccup can't steal it
        self.logs_tab.set_handler(handler)
        self.inner.setCurrentIndex(0)
        self.term.setFocus()

        # populate the SFTP tab — never let an SFTP error break the session
        try:
            browser = SftpBrowser(handler)
            browser.log.connect(self.log)
            idx = self.inner.indexOf(self.sftp_page)
            self.inner.removeTab(idx)
            self.sftp_page = browser
            self.inner.insertTab(idx, browser, "SFTP")
        except Exception as exc:
            self.log.emit(f"[ERROR] SFTP browser unavailable: {exc}")

    def _on_disconnected(self):
        self.status.setText("Disconnected")
        self.connected.emit(False, f"Disconnected from {self.session.get('host')}")

    def _on_fail(self, msg):
        self.status.setText("Connect failed")
        self.term.feed(f"\n[connect failed] {msg}\n".encode())
        self.log.emit(f"[ERROR] {self.session['name']}: {msg}")
        self.failed.emit(f"{self.session.get('name')}: {msg}")

    def _send(self, data: bytes):
        try:
            if self.shell:
                self.shell.channel.send(data)
        except Exception as exc:
            self.log.emit(f"[ERROR] send: {exc}")

    def _send_cmd(self, command: str):
        """Used by the one-click Quick buttons (slog2info/journalctl/…)."""
        self._send((command + "\n").encode("utf-8"))
        self.term.setFocus()

    def _interrupt(self):
        """Send Ctrl-C to the shell and immediately drop the buffered backlog, so
        a flood (slog2info -w / journalctl -f) stops on screen at once instead of
        trickling for seconds. Then refocus so the prompt + keyboard return."""
        self._send(b"\x03")
        try:
            if self.reader:
                self.reader.flush()        # discard not-yet-shown flood
        except Exception:
            pass
        self.term.setFocus()

    def _on_term_resize(self, cols, rows):
        try:
            if self.shell:
                self.shell.resize(cols, rows)
        except Exception:
            pass

    def _on_inner_tab(self, index):
        # keep the terminal focused so keystrokes reach the shell
        if self.inner.tabText(index) == "Terminal":
            self.term.setFocus()

    def close_session(self):
        try:
            self.logs_tab.close_tab()
        except Exception:
            pass
        if self.reader:
            self.reader.stop(); self.reader.wait(1000)
        try:
            self.term.cleanup()
        except Exception:
            pass
        if self.handler:
            self.handler.disconnect()


class _SerialConnectThread(QThread):
    ok = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, device, baud):
        super().__init__()
        self.device, self.baud = device, baud

    def run(self):
        try:
            h = SerialHandler(self.device, baudrate=self.baud, safe=True)
            res = h.open()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error)); return
            self.ok.emit(h)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _SerialSshConnectThread(QThread):
    """Connect the SSH handler and check whether the target serial port is busy
    (so the GUI can ask before forcibly taking it). Serial-over-SSH then runs a
    native bridge channel via handler.serial_bridge()."""
    ok = pyqtSignal(object, bool)       # handler, busy
    fail = pyqtSignal(str)

    def __init__(self, cfg, device):
        super().__init__()
        self.cfg = cfg
        self.device = device

    def run(self):
        try:
            h = SSHHandler(self.cfg, safe=True)
            res = h.connect()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error)); return
            busy = False
            try:
                busy = bool(h.serial_in_use(self.device, mode="auto"))
            except Exception:
                busy = False
            self.ok.emit(h, busy)
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class _SerialStreamThread(threading.Thread):
    """Runs handler.serial_stream() on a background thread and buffers every line
    so the GUI can drain it on a timer (no per-line signal flooding). Mirrors the
    working 1.5.0 'script' approach: a line-buffered exec channel reading the
    remote serial port — COMx via PowerShell, /dev/* via stty+cat (auto)."""

    def __init__(self, handler, device, baud):
        super().__init__(daemon=True)
        self.handler = handler
        self.device = device
        self.baud = int(baud)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error = None

    def _on_line(self, line: str):
        with self._lock:
            self._buf.extend((line + "\r\n").encode("utf-8", "replace"))
            if len(self._buf) > 4 * 1024 * 1024:        # cap at 4MB
                del self._buf[:len(self._buf) - 4 * 1024 * 1024]

    def pull(self, maxn=262144):
        with self._lock:
            if not self._buf:
                return b""
            chunk = bytes(self._buf[:maxn])
            del self._buf[:maxn]
            return chunk

    def run(self):
        try:
            self.handler.serial_stream(self.device, baudrate=self.baud,
                                       mode="auto", on_line=self._on_line,
                                       clean=True, stop_event=self._stop)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._on_line(f"[serial stream ended] {self.error}")

    def stop(self):
        self._stop.set()


class SerialSessionWidget(QWidget):
    log = pyqtSignal(str)
    connected = pyqtSignal(bool, str)
    failed = pyqtSignal(str)

    def __init__(self, session: dict, password: str = "", jump_password: str = "",
                 parent=None):
        super().__init__(parent)
        self.session = session
        self.password = password
        self.jump_password = jump_password
        self.via_ssh = bool(session.get("via_ssh"))
        self.handler = None
        self.shell = None
        self.reader = None
        self.stream_thread = None
        self.bridge_chan = None        # raw SSH channel for native serial-over-SSH

        lay = QVBoxLayout(self)
        self.status = QLabel("Opening…")
        lay.addWidget(self.status)
        # a small toolbar (NO input box) — this is a native terminal: click in it
        # and type directly; keystrokes go straight to the port.
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Type directly in the terminal:"))
        ctrlc = QPushButton("Ctrl-C"); ctrlc.setProperty("role", "danger")
        ctrlc.setToolTip("Send Ctrl-C (0x03) to the port to interrupt a running command")
        ctrlc.clicked.connect(self._interrupt)
        paste = QPushButton("Paste"); paste.setProperty("role", "ghost")
        paste.clicked.connect(lambda: (self.term.paste_clipboard(), self.term.setFocus()))
        clr = QPushButton("Clear"); clr.setProperty("role", "ghost")
        clr.clicked.connect(lambda: (self.term.clear(), self.term.setFocus()))
        crow.addWidget(ctrlc); crow.addWidget(paste); crow.addWidget(clr)
        crow.addStretch(1)
        lay.addLayout(crow)
        self.term = Vt100Terminal(send_fn=self._send)
        self.term.resized.connect(self._on_term_resize)
        lay.addWidget(self.term, 1)
        lay.addLayout(crow)

        if self.via_ssh:
            cfg = config_from_session(session, password, jump_password)
            self._ct = _SerialSshConnectThread(cfg, session.get("device", "/dev/ser1"))
            self._ct.ok.connect(self._on_ssh_ok)
            self._ct.fail.connect(self._on_fail)
            self._ct.start()
        else:
            self._ct = _SerialConnectThread(session.get("device", "COM5"),
                                            session.get("baud", 115200))
            self._ct.ok.connect(self._on_open_local)
            self._ct.fail.connect(self._on_fail)
            self._ct.start()

    # --- local serial (native interactive) ---
    def _on_open_local(self, handler):
        self.handler = handler
        dev = self.session.get("device")
        self.status.setText(f"Open — {dev} @ {self.session.get('baud')}")
        self.log.emit(f"[OK] serial {dev} open")
        self.connected.emit(True, f"Serial {dev} open")
        ser = handler._require()

        def read_fn():
            try:
                return ser.read(4096)
            except Exception:
                return None

        self.reader = ReaderThread(read_fn)
        self.reader.start()
        self.term.set_source(self.reader.pull)
        self.term.setFocus()

    # --- serial over SSH: a NATIVE, bidirectional bridge (char-by-char, Tab
    #     completion, Ctrl-C all work). Keystrokes -> raw SSH channel -> port;
    #     port -> channel -> terminal. Closing the channel releases the port.
    def _on_ssh_ok(self, handler, busy=False):
        self.handler = handler
        dev = self.session.get("device", "/dev/ser1")
        baud = self.session.get("baud", 115200)
        force = False
        if busy:
            from PyQt5.QtWidgets import QMessageBox
            r = QMessageBox.question(
                self, "Port already in use",
                f"{dev} is already in use on {self.session.get('host')}.\n\n"
                f"Open it anyway (this will take the port from whatever is "
                f"holding it)?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                self.status.setText(f"{dev} is in use — not opened")
                self.term.feed(f"--- {dev} is in use; not opened ---\r\n".encode())
                self.failed.emit(f"{self.session.get('name')}: {dev} in use")
                return
            force = True
        try:
            chan = handler.serial_bridge(dev, baudrate=int(baud), mode="auto",
                                         force=force)
        except Exception as exc:
            self._on_fail(f"could not open serial bridge: {exc}")
            return
        self.bridge_chan = chan
        chan.settimeout(0.1)
        self.status.setText(f"Serial over SSH — {dev} @ {baud} (native)")
        self.log.emit(f"[OK] serial-over-SSH {dev} (native bridge)")
        self.connected.emit(True, f"Serial over SSH: {dev}")
        self.term.feed(f"--- connected to {dev} @ {baud} (via SSH) ---\r\n".encode())

        def read_fn():
            if chan.closed or chan.eof_received:
                return None
            try:
                data = chan.recv(65536)
                return None if data == b"" else data
            except socket.timeout:
                return b""
            except Exception:
                return None

        self.reader = ReaderThread(read_fn)
        self.reader.start()
        self.term.set_source(self.reader.pull)
        self.term.setFocus()

    def _on_term_resize(self, cols, rows):
        pass

    def _on_fail(self, msg):
        self.status.setText("Open failed")
        self.term.feed(f"\n[serial open failed] {msg}\n".encode())
        self.log.emit(f"[ERROR] serial: {msg}")
        self.failed.emit(f"{self.session.get('name')}: {msg}")

    def _send(self, data: bytes):
        """Native input. via_ssh -> raw SSH bridge channel; local -> the port."""
        try:
            if self.via_ssh:
                if self.bridge_chan and not self.bridge_chan.closed:
                    self.bridge_chan.send(data)
            elif self.handler:
                self.handler._require().write(data)
        except Exception as exc:
            self.log.emit(f"[ERROR] serial write: {exc}")

    def showEvent(self, event):
        super().showEvent(event)
        # when this session tab is shown, put the cursor in the terminal so the
        # user can type immediately (native terminal — no separate input box)
        self.term.setFocus()

    def _interrupt(self):
        """Send Ctrl-C (0x03) to the port to interrupt a running command."""
        self._send(b"\x03")
        self.term.setFocus()

    def close_session(self):
        if self.stream_thread:
            try:
                self.stream_thread.stop()
            except Exception:
                pass
        # Release the COM port reliably: EOF alone can leave the remote bridge
        # orphaned (Windows OpenSSH), so EXPLICITLY kill it by PID over the still-
        # open SSH connection, THEN tear the channel down. Otherwise the port
        # stays locked ("access denied" / "permission denied") next time.
        if self.bridge_chan is not None:
            try:
                self.bridge_chan.shutdown_write()      # ask it to stop (EOF)
            except Exception:
                pass
            if self.via_ssh and self.handler is not None:
                try:                                   # the decisive step: kill it
                    self.handler.serial_release(
                        self.session.get("device", ""), mode="auto", safe=True)
                except Exception:
                    pass
            try:
                self.bridge_chan.close()
            except Exception:
                pass
        if self.reader:
            try:
                self.reader.stop()
            except Exception:
                pass
            self.reader.wait(1000)
        try:
            self.term.cleanup()
        except Exception:
            pass
        if self.handler:
            try:
                self.handler.close()
            except Exception:
                try:
                    self.handler.disconnect()
                except Exception:
                    pass
