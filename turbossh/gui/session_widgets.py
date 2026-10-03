"""Session tab widgets: an SSH tab (interactive terminal + SFTP browser + logs)
and a Serial tab (interactive serial console). Everything that touches the
network or a port — connecting, probing, opening the serial bridge, releasing it,
disconnecting — runs off the GUI thread, so the window never blocks."""

from __future__ import annotations

import socket
import time

from PyQt5.QtCore import QThread, pyqtSignal, QTimer
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QTabWidget)

from ..config import SSHConfig
from ..core import SSHHandler
from ..serial_handler import SerialHandler
from ..results import OperationResult
from . import qtutil
from .terminal import ReaderThread
from .vt100 import Vt100Terminal
from .file_browser import FileBrowser
from .logs_tab import LogsTab


def config_from_session(s: dict, password: str, jump_password: str) -> SSHConfig:
    policy = "ignore" if s.get("ignore_hostkey", True) else "auto"
    # A blank password field means "no password" — pass None, not "". With None
    # the handler tries the key file, the agent and the default keys, and only
    # then a blank-password login. Passing "" told it to send an empty password
    # and switched key lookup OFF, although the dialog says to leave the field
    # blank for key logins — so key-based login from the GUI could never work.
    jump = None
    if s.get("use_jump") and s.get("jhost"):
        jump = SSHConfig(host=s["jhost"], port=s.get("jport", 22),
                         username=s.get("juser") or None,
                         domain=s.get("jdomain") or None,
                         password=jump_password or None,
                         host_key_policy=policy,
                         enable_legacy_algorithms=s.get("legacy", False))
    return SSHConfig(
        host=s.get("host", ""), port=s.get("port", 22),
        username=s.get("user") or None, domain=s.get("domain") or None,
        password=password or None, key_filename=s.get("key_file") or None,
        jump_host=jump, host_key_policy=policy,
        enable_legacy_algorithms=s.get("legacy", False),
    )


def _engine_log(signal, name: str):
    """A log_callback that forwards the handler's own log lines (connect, each
    command, errors) to the GUI log. They used to go to stderr only, which a
    windowed app does not have. DEBUG chatter is left out."""
    def forward(message: str):
        if not message.startswith("[DEBUG]"):
            signal.emit(f"{message}  ({name})")
    return forward


def _close_handler(handler):
    """Close a handler nobody is waiting for any more, off the GUI thread."""
    if handler is not None:
        closer = getattr(handler, "disconnect", None) or getattr(handler, "close", None)
        if closer is not None:
            qtutil.run_teardown(closer, "close-orphan")


class _SshConnectThread(QThread):
    ok = pyqtSignal(object, object)     # handler, shell
    fail = pyqtSignal(str)

    def __init__(self, cfg, log_callback=None):
        super().__init__()
        self.cfg = cfg
        self._log = log_callback

    def run(self):
        h = None
        try:
            h = SSHHandler(self.cfg, safe=True, quiet=True, log_callback=self._log)
            res = h.connect()
            if isinstance(res, OperationResult) and not res.success:
                self.fail.emit(str(res.error)); return
            sh = h.open_shell()
            shell = sh.value if isinstance(sh, OperationResult) else sh
            # Probe the remote OS HERE: it costs one or two round trips, and the
            # welcome banner and the Logs tab then read the cached answer instead
            # of running commands on the GUI thread.
            try:
                h.detect_os()
            except Exception:
                pass
            self.ok.emit(h, shell)
        except Exception as exc:
            if h is not None:
                try:
                    h.disconnect()
                except Exception:
                    pass
            self.fail.emit(f"{type(exc).__name__}: {exc}")


class SshSessionWidget(QWidget):
    log = pyqtSignal(str)
    connected = pyqtSignal(bool, str)        # ok, message  -> main window status
    failed = pyqtSignal(str)                 # error message -> main window status
    notify = pyqtSignal(str, str)            # text, kind   -> main window toast

    def __init__(self, session: dict, password: str, jump_password: str, parent=None):
        super().__init__(parent)
        self.session = session
        self.handler = None
        self.shell = None
        self.reader = None
        self._ct = None
        self._closed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 2); lay.setSpacing(2)
        self.status = QLabel("Connecting…")
        self.status.setStyleSheet("font-size:8pt; color:#8a9aa5;")
        lay.addWidget(self.status)

        # inner tabs: Terminal | SFTP | Logs
        self.inner = QTabWidget()
        self.inner.currentChanged.connect(self._on_inner_tab)

        # --- Terminal tab (toolbar + VT100) ---
        term_page = QWidget()
        tlay = QVBoxLayout(term_page); tlay.setContentsMargins(0, 0, 0, 0)
        tlay.setSpacing(2)
        quick = QHBoxLayout(); quick.setSpacing(4)
        ql = QLabel("Quick:"); ql.setStyleSheet("font-size:8pt; color:#8a9aa5;")
        quick.addWidget(ql)
        cmds = [c.strip() for c in (session.get("quick_cmds") or "").split(",")
                if c.strip()] or ["slog2info", "journalctl", "dmesg", "ls", "ps"]
        for cmd in cmds:
            b = QPushButton(cmd); b.setProperty("role", "ghost")
            b.setToolTip(f"Send: {cmd}")
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

        self._cfg = config_from_session(session, password, jump_password)
        self._user_closed = False          # set in close_session -> no auto-reconnect
        self._reconnecting = False
        self._reconnect_until = 0.0
        self.term.on_reconnect = self._manual_reconnect   # Ctrl-R when disconnected
        self._connect()

    def _connect(self):
        self._ct = _SshConnectThread(
            self._cfg, _engine_log(self.log, self.session.get("name", "")))
        self._ct.ok.connect(self._on_connected)
        self._ct.fail.connect(self._on_reconnect_fail if self._reconnecting
                              else self._on_fail)
        self._ct.start()

    def _drop_reader(self):
        """Stop the current reader without waiting on the GUI thread. It is
        parked, so it can finish on its own and is never destroyed mid-run."""
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.stop()
            qtutil.park_thread(reader)

    def _on_connected(self, handler, shell):
        if self._closed:                   # the tab was closed while connecting
            _close_handler(handler)
            return
        # re-entrant: this also runs on a successful auto/Ctrl-R reconnect, so
        # tear down the previous connection's pieces first.
        was_reconnect = self._reconnecting
        self._reconnecting = False
        self._reconnect_until = 0.0
        self.term.reconnect_armed = False
        self._drop_reader()
        previous = self.handler
        self.handler = handler
        self.shell = shell
        if previous is not None and previous is not handler:
            # a reconnect made a NEW handler; the old one (and its jump host
            # connection) used to be left open for the life of the app
            _close_handler(previous)
        if shell is None:
            self._on_fail("Connected, but the interactive shell could not open.")
            return
        host = self.session.get("host")
        self.status.setText(f"Connected — {host}")
        self.log.emit(f"[OK] {self.session['name']}: "
                      f"{'reconnected' if was_reconnect else 'connected'}")
        self.connected.emit(True, f"{'Reconnected' if was_reconnect else 'Connected'} to {host}")
        if was_reconnect:
            self.term.feed(b"\r\n\x1b[32m[reconnected]\x1b[0m\r\n")
        else:
            self._welcome_banner()
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
        # the GUI drains on a timer, and is told when output had to be skipped
        self.term.set_source(self.reader.pull, self.reader.take_dropped)
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
            browser = FileBrowser(handler)        # local | remote + transfers
            browser.log.connect(self.log)
            browser.notify.connect(self.notify)
            idx = self.inner.indexOf(self.sftp_page)
            self.inner.removeTab(idx)
            self._discard_sftp_page()
            self.sftp_page = browser
            self.inner.insertTab(idx, browser, "SFTP")
        except Exception as exc:
            self.log.emit(f"[ERROR] SFTP browser unavailable: {exc}")

    def _discard_sftp_page(self):
        """Close and delete the page that is being replaced (removeTab only
        hides it — on every reconnect one more browser used to be left behind)."""
        old = self.sftp_page
        closer = getattr(old, "close_browser", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
        old.setParent(None)
        old.deleteLater()

    def _on_disconnected(self):
        # reader signals this when the channel drops. Unless the user is closing
        # the session, try to reconnect automatically for ~30s.
        if self._user_closed or self._reconnecting:
            return
        self.connected.emit(False, f"Disconnected from {self.session.get('host')}")
        self._start_reconnect()

    def _start_reconnect(self, window: float = 30.0):
        self._reconnecting = True
        self.term.reconnect_armed = False
        self._reconnect_until = time.time() + window
        self.status.setText("Connection lost — reconnecting…")
        self.term.feed("\r\n\x1b[33m[connection lost — reconnecting for 30s…]"
                       "\x1b[0m\r\n".encode("utf-8"))
        self._try_reconnect()

    def _try_reconnect(self):
        if self._reconnecting and not self._user_closed:
            self._connect()      # fail routes to _on_reconnect_fail while reconnecting

    def _on_reconnect_fail(self, msg):
        if self._user_closed:
            return
        if time.time() < self._reconnect_until:
            self.term.feed(b".")                       # progress dots
            QTimer.singleShot(3000, self._try_reconnect)
            return
        # window elapsed -> stop and let the user retry with Ctrl+R
        self._reconnecting = False
        self.status.setText("SSH connection closed")
        self.term.feed("\r\n\x1b[31m[SSH connection closed]\x1b[0m  press "
                       "\x1b[1mCtrl+R\x1b[0m to reconnect\r\n".encode("utf-8"))
        self.term.reconnect_armed = True
        self.term.setFocus()
        self.connected.emit(False, f"SSH connection closed: {self.session.get('host')}")

    def _manual_reconnect(self):
        if self._user_closed or self._reconnecting:
            return
        self.term.reconnect_armed = False
        self.term.feed("\r\n\x1b[33m[reconnecting…]\x1b[0m\r\n".encode("utf-8"))
        self._start_reconnect()

    def _on_fail(self, msg):
        self.status.setText("Connect failed")
        self.term.feed(f"\r\n\x1b[31m[connect failed]\x1b[0m {msg}\r\n".encode())
        self.log.emit(f"[ERROR] {self.session['name']}: {msg}")
        self.failed.emit(f"{self.session.get('name')}: {msg}")

    def _send(self, data: bytes):
        try:
            if self.shell:
                # sendall: send() may take only part of a large write (a long
                # paste was silently cut off at the SSH packet size)
                self.shell.channel.sendall(data)
        except Exception as exc:
            self.log.emit(f"[ERROR] send: {exc}")

    def _send_cmd(self, command: str):
        """Used by the one-click Quick buttons (slog2info/journalctl/…)."""
        self._send((command + "\n").encode("utf-8"))
        self.term.setFocus()

    def _welcome_banner(self):
        """A clean two-line welcome shown in the terminal on first connect:
        who/where (+ jump host) on line 1, OS and time on line 2."""
        s = self.session
        name = s.get("name") or s.get("host") or "session"
        user = s.get("user") or "—"
        host = s.get("host"); port = s.get("port", 22)
        C, R, G, Y, B, D = ("\x1b[36m", "\x1b[0m", "\x1b[32m", "\x1b[33m",
                            "\x1b[1m", "\x1b[90m")
        extra = f"{Y}  via {s.get('jhost')}{R}" if (s.get("use_jump") and s.get("jhost")) else ""
        # the connect thread already probed the OS; this never hits the network
        osname = self.handler.known_os or ""
        meta = "  ·  ".join(x for x in (osname, time.strftime("%Y-%m-%d %H:%M:%S")) if x)
        line1 = f"{C}●{R} {B}{name}{R}  {G}{user}@{host}:{port}{R}{extra}"
        line2 = f"{D}  {meta}{R}"
        self.term.feed((line1 + "\r\n" + line2 + "\r\n\r\n").encode("utf-8"))

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
        """Shut the session down. Never waits on the GUI thread, and leaves
        nothing that could call back into this widget (it is deleted next)."""
        if self._closed:
            return
        self._closed = True
        self._user_closed = True          # stop any auto-reconnect
        self._reconnecting = False
        # a connect still in flight: let it finish, then close what it opened
        qtutil.park_thread(self._ct, orphan={"ok": lambda h, _shell: _close_handler(h)})
        try:
            self.logs_tab.close_tab()
        except Exception:
            pass
        self._drop_reader()
        closer = getattr(self.sftp_page, "close_browser", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
        try:
            self.term.cleanup()
        except Exception:
            pass
        handler, self.handler, self.shell = self.handler, None, None
        _close_handler(handler)


class _SerialConnectThread(QThread):
    ok = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, device, baud):
        super().__init__()
        self.device, self.baud = device, baud

    def run(self):
        try:
            # a short read timeout: the reader asks for whatever has arrived, and
            # this bounds how long it takes to notice the tab was closed
            h = SerialHandler(self.device, baudrate=self.baud, safe=True,
                              quiet=True, timeout=0.1)
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

    def __init__(self, cfg, device, log_callback=None):
        super().__init__()
        self.cfg = cfg
        self.device = device
        self._log = log_callback

    def run(self):
        h = None
        try:
            h = SSHHandler(self.cfg, safe=True, quiet=True, log_callback=self._log)
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
            if h is not None:
                try:
                    h.disconnect()
                except Exception:
                    pass
            self.fail.emit(f"{type(exc).__name__}: {exc}")


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
        self.bridge_chan = None        # raw SSH channel for native serial-over-SSH
        self._jobs: set = set()        # worker threads (bridge open)
        self._closed = False

        lay = QVBoxLayout(self); lay.setContentsMargins(2, 1, 2, 2); lay.setSpacing(2)
        self.status = QLabel("Opening…")
        self.status.setStyleSheet("font-size:8pt; color:#8a9aa5;")
        lay.addWidget(self.status)
        # a small toolbar (NO input box) — this is a native terminal: click in it
        # and type directly; keystrokes go straight to the port.
        crow = QHBoxLayout(); crow.setSpacing(4)
        tl = QLabel("Type directly:"); tl.setStyleSheet("font-size:8pt; color:#8a9aa5;")
        crow.addWidget(tl)
        ctrlc = QPushButton("Ctrl-C"); ctrlc.setProperty("role", "danger")
        ctrlc.setToolTip("Send Ctrl-C (0x03) to the port to interrupt a running command")
        ctrlc.clicked.connect(self._interrupt)
        paste = QPushButton("Paste"); paste.setProperty("role", "ghost")
        paste.clicked.connect(lambda: (self.term.paste_clipboard(), self.term.setFocus()))
        clr = QPushButton("Clear"); clr.setProperty("role", "ghost")
        clr.clicked.connect(lambda: (self.term.clear(), self.term.setFocus()))
        crow.addWidget(ctrlc); crow.addWidget(paste); crow.addWidget(clr)
        for cmd in [c.strip() for c in (session.get("quick_cmds") or "").split(",")
                    if c.strip()]:
            b = QPushButton(cmd); b.setProperty("role", "ghost")
            b.setToolTip(f"Send to the port: {cmd}")
            b.clicked.connect(lambda _=False, c=cmd: self._send_quick(c))
            crow.addWidget(b)
        crow.addStretch(1)
        self.term = Vt100Terminal(send_fn=self._send)
        self.term.resized.connect(self._on_term_resize)
        lay.addLayout(crow)
        lay.addWidget(self.term, 1)

        if self.via_ssh:
            cfg = config_from_session(session, password, jump_password)
            self._ct = _SerialSshConnectThread(
                cfg, session.get("device", "/dev/ser1"),
                _engine_log(self.log, session.get("name", "")))
            self._ct.ok.connect(self._on_ssh_ok)
            self._ct.fail.connect(self._on_fail)
            self._ct.start()
        else:
            self._ct = _SerialConnectThread(session.get("device", "COM5"),
                                            session.get("baud", 115200))
            self._ct.ok.connect(self._on_open_local)
            self._ct.fail.connect(self._on_fail)
            self._ct.start()

    def _start_reader(self, read_fn):
        self.reader = ReaderThread(read_fn)
        # the port going away (unplugged, bridge ended, link dropped) used to
        # leave the tab silently showing "Open"
        self.reader.closed.connect(self._on_port_closed)
        self.reader.start()
        self.term.set_source(self.reader.pull, self.reader.take_dropped)
        self.term.setFocus()

    def _on_port_closed(self):
        if self._closed:
            return
        dev = self.session.get("device", "the port")
        self.status.setText(f"{dev} — closed")
        self.term.feed(f"\r\n\x1b[31m[{dev} closed — the port was unplugged or the "
                       f"link dropped; reopen the session to continue]\x1b[0m\r\n"
                       .encode("utf-8"))
        self.log.emit(f"[WARN] serial {dev} closed")
        self.connected.emit(False, f"Serial {dev} closed")

    # --- local serial (native interactive) ---
    def _on_open_local(self, handler):
        if self._closed:
            _close_handler(handler)
            return
        self.handler = handler
        dev = self.session.get("device")
        self.status.setText(f"Open — {dev} @ {self.session.get('baud')}")
        self.log.emit(f"[OK] serial {dev} open")
        self.connected.emit(True, f"Serial {dev} open")
        self._serial_banner(dev, self.session.get("baud"), False)

        def read_fn():
            # read_available returns what has arrived as soon as it arrives.
            # (ser.read(4096) waited for 4096 bytes or the whole timeout, so
            # every keystroke echoed up to a second late.)
            try:
                return handler.read_available()
            except Exception:
                return None

        self._start_reader(read_fn)

    # --- serial over SSH: a NATIVE, bidirectional bridge (char-by-char, Tab
    #     completion, Ctrl-C all work). Keystrokes -> raw SSH channel -> port;
    #     port -> channel -> terminal. Closing the channel releases the port.
    def _on_ssh_ok(self, handler, busy=False):
        if self._closed:
            _close_handler(handler)
            return
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
        self.status.setText(f"Opening {dev} on {self.session.get('host')}…")
        # opening the bridge is an SSH round trip (plus up to several seconds of
        # retries on the remote when forcing) — not on the GUI thread
        qtutil.run_job(
            self._jobs,
            lambda: handler.serial_bridge(dev, baudrate=int(baud), mode="auto",
                                          force=force),
            on_done=self._on_bridge,
            on_fail=lambda msg: self._on_fail(f"could not open serial bridge: {msg}"))

    def _on_bridge(self, chan):
        if self._closed:
            try:
                chan.close()
            except Exception:
                pass
            return
        dev = self.session.get("device", "/dev/ser1")
        baud = self.session.get("baud", 115200)
        self.bridge_chan = chan
        chan.settimeout(0.1)
        self.status.setText(f"Serial over SSH — {dev} @ {baud} (native)")
        self.log.emit(f"[OK] serial-over-SSH {dev} (native bridge)")
        self.connected.emit(True, f"Serial over SSH: {dev}")
        self._serial_banner(dev, baud, True)

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

        self._start_reader(read_fn)
        # match the remote PTY to our terminal so the conpty doesn't pad the
        # output with stray blank lines.
        self._on_term_resize(self.term.cols, self.term.rows)

    def _on_term_resize(self, cols, rows):
        # keep the remote bridge's PTY the SAME size as our terminal — an oversized
        # (250x50) conpty was repositioning output across a taller screen, which is
        # what produced the stray blank lines in serial-over-SSH.
        try:
            if self.bridge_chan is not None and not self.bridge_chan.closed:
                self.bridge_chan.resize_pty(width=int(cols), height=int(rows))
        except Exception:
            pass

    def _retry_open(self):
        self.status.setText("Retrying…")
        dev = self.session.get("device", "COM5")
        self._ct = _SerialConnectThread(dev, self.session.get("baud", 115200))
        self._ct.ok.connect(self._on_open_local)
        self._ct.fail.connect(self._on_fail)
        self._ct.start()

    def _on_fail(self, msg):
        if self._closed:
            return
        from PyQt5.QtWidgets import QMessageBox
        self.status.setText("Open failed")
        dev = self.session.get("device", "the port")
        low = (msg or "").lower()
        busy = any(k in low for k in ("access is denied", "permission", "in use",
                                      "denied", "being used", "cannot open",
                                      "could not open"))
        if busy and not self.via_ssh:
            self.term.feed(f"\r\n\x1b[31m[{dev} is in use by another "
                           f"application]\x1b[0m\r\n".encode())
            r = QMessageBox.question(
                self, "Serial port in use",
                f"{dev} is held by another application (MobaXterm, PuTTY, another "
                f"TurboSSH window, …).\n\nWindows does not let one app take a COM "
                f"port away from another, so close it in that app first.\n\n"
                f"Retry now?", QMessageBox.Retry | QMessageBox.Cancel,
                QMessageBox.Retry)
            if r == QMessageBox.Retry:
                self._retry_open(); return
            self.failed.emit(f"{self.session.get('name')}: {dev} in use")
            self.log.emit(f"[ERROR] serial: {dev} in use (held by another app)")
            return
        self.term.feed(f"\r\n\x1b[31m[serial open failed]\x1b[0m {msg}\r\n".encode())
        self.log.emit(f"[ERROR] serial: {msg}")
        self.failed.emit(f"{self.session.get('name')}: {msg}")

    def _send(self, data: bytes):
        """Native input. via_ssh -> raw SSH bridge channel; local -> the port."""
        try:
            if self.via_ssh:
                if self.bridge_chan and not self.bridge_chan.closed:
                    self.bridge_chan.sendall(data)
            elif self.handler:
                self.handler.write_bytes(data, safe=False)
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

    def _send_quick(self, cmd: str):
        """One-click Quick command -> send to the port with a carriage return."""
        self._send((cmd + "\r").encode("utf-8"))
        self.term.setFocus()

    def _serial_banner(self, dev, baud, via_ssh):
        name = self.session.get("name") or dev
        C, R, G, B, D = "\x1b[36m", "\x1b[0m", "\x1b[32m", "\x1b[1m", "\x1b[90m"
        where = f"on {self.session.get('host')} via SSH" if via_ssh else "local"
        line1 = f"{C}●{R} {B}{name}{R}  {G}{dev} @ {baud}{R}  {D}{where}{R}"
        line2 = f"{D}  {time.strftime('%Y-%m-%d %H:%M:%S')}{R}"
        self.term.feed((line1 + "\r\n" + line2 + "\r\n\r\n").encode("utf-8"))

    def close_session(self):
        """Release the port and close the connection — on a background thread,
        because the release is a remote command (seconds on Windows) and used to
        freeze the window on every tab close and once per tab at exit. The main
        window joins these teardowns before the process ends."""
        if self._closed:
            return
        self._closed = True
        # an open still in flight: let it finish, then close what it opened
        qtutil.park_thread(self._ct, orphan={"ok": lambda h, *_: _close_handler(h)})
        qtutil.close_jobs(self._jobs)
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.stop()
            qtutil.park_thread(reader)
        try:
            self.term.cleanup()
        except Exception:
            pass
        handler, chan = self.handler, self.bridge_chan
        self.handler, self.bridge_chan = None, None
        device, via_ssh = self.session.get("device", ""), self.via_ssh

        def teardown():
            # Release the COM port reliably: EOF alone can leave the remote bridge
            # orphaned (Windows OpenSSH), so EXPLICITLY kill it by PID over the
            # still-open SSH connection, THEN tear the channel down. Otherwise the
            # port stays locked ("access denied") next time. This also covers a
            # bridge whose open was still in flight when the tab closed.
            if chan is not None:
                try:
                    chan.shutdown_write()          # ask it to stop (EOF)
                except Exception:
                    pass
            if via_ssh and handler is not None:
                try:
                    handler.serial_release(device, mode="auto", safe=True)
                except Exception:
                    pass
            if chan is not None:
                try:
                    chan.close()
                except Exception:
                    pass
            if handler is not None:
                closer = getattr(handler, "disconnect", None) or handler.close
                try:
                    closer()
                except Exception:
                    pass

        if handler is not None or chan is not None:
            qtutil.run_teardown(teardown, "serial-teardown")
