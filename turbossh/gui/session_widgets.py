"""Session tab widgets: an SSH tab (interactive terminal + SFTP browser + logs)
and a Serial tab (interactive serial console). Everything that touches the
network or a port — connecting, probing, opening the serial bridge, releasing it,
disconnecting, and every keystroke, paste and window-size change — runs off the
GUI thread, so the window never blocks."""

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
from .terminal import ReaderThread, Writer
from .vt100 import Vt100Terminal
from .file_browser import FileBrowser
from .logs_tab import LogsTab

# a connection that dies sooner than this after it opened is reconnected only
# after a growing pause (2, 4, 8 … 30 s), not in a tight loop
_QUICK_DROP = 5.0
_MAX_BACKOFF = 30.0


def _channel_reader(chan):
    """``read_fn`` for a :class:`ReaderThread` on a paramiko channel (whose
    timeout is short, so the reader notices being stopped). EOF is reported
    only once everything already received has been read: checking
    ``eof_received`` / ``closed`` first lost the last output — logout and
    reboot messages, the serial bridge's BUSY line."""
    def read():
        try:
            data = chan.recv(65536)
        except socket.timeout:
            # nothing yet — or a channel closed under us with nothing left
            return None if chan.closed and not chan.recv_ready() else b""
        except Exception:
            return None
        return data or None              # b"": closed / EOF, and drained
    return read


def _channel_writer(chan):
    """``write_fn`` for a :class:`Writer` on a paramiko channel. The channel's
    timeout is the reader's 0.1 s (paramiko has one for both directions), and
    ``sendall`` raised socket.timeout mid-paste whenever the window was full
    that long — a paste to a small-window server (Dropbear: 24 KB) over a slow
    VPN lost its rest. Here a full window just means "try again"; the Writer
    gives up only when nothing at all is taken for a minute."""
    def write(data: bytes):
        if chan.closed:
            raise OSError("the connection is closed")
        try:
            return chan.send(data)
        except socket.timeout:
            return 0
    return write


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


class _EngineLog:
    """A log_callback that forwards the handler's own log lines (connect, each
    command, errors) to the GUI log. They used to go to stderr only, which a
    windowed app does not have. DEBUG chatter is left out.

    The handler logs from other threads, and still logs while it is disconnected
    on a teardown thread after its tab is gone; ``close()`` (from close_session)
    stops that before the widget is deleted — emitting into it then crashed."""

    def __init__(self, signal, name: str):
        self._gate = qtutil.SignalGate(signal)
        self._name = name

    def __call__(self, message: str) -> None:
        if not message.startswith("[DEBUG]"):
            self._gate.emit(f"{message}  ({self._name})")

    def close(self) -> None:
        self._gate.close()


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
    session_kind = "ssh"
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
        self._engine_log = _EngineLog(self.log, session.get("name", ""))
        self._write_gate = qtutil.SignalGate(self.log)   # the writer thread's errors
        self._writer = None
        self._user_closed = False          # set in close_session -> no auto-reconnect
        self._reconnecting = False
        self._reconnect_until = 0.0
        self._connected_at = 0.0           # time.monotonic() of the last connect
        self._quick_drops = 0              # connections in a row that died at once
        self.term.on_reconnect = self._manual_reconnect   # Ctrl-R when disconnected
        self._connect()

    def _connect(self):
        # (the previous attempt has finished; parking is only a safety net)
        qtutil.park_thread(self._ct, orphan={"ok": lambda h, _shell: _close_handler(h)})
        self._ct = _SshConnectThread(self._cfg, self._engine_log)
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

    def _drop_writer(self):
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()

    def _on_write_error(self, exc):
        self._write_gate.emit(f"[ERROR] send: {exc}")    # (on the writer thread)

    def _on_connected(self, handler, shell):
        if self._closed:                   # the tab was closed while connecting
            _close_handler(handler)
            return
        # re-entrant: this also runs on a successful auto/Ctrl-R reconnect, so
        # tear down the previous connection's pieces first.
        was_reconnect = self._reconnecting
        self._reconnecting = False
        self._reconnect_until = 0.0
        self._connected_at = time.monotonic()
        self.term.reconnect_armed = False
        self._drop_reader()
        self._drop_writer()
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
            self.term.notice(b"\r\n\x1b[32m[reconnected]\x1b[0m\r\n")
        else:
            self._welcome_banner()
        chan = shell.channel
        chan.settimeout(0.1)               # the reader's; the writer copes with it
        # keystrokes, pastes and resizes go out on a thread of their own
        self._writer = Writer(_channel_writer(chan), on_error=self._on_write_error,
                              name="ssh-input")
        self.reader = ReaderThread(_channel_reader(chan))
        self.reader.closed.connect(self._on_disconnected)
        self.reader.start()
        # the GUI drains on a timer, and is told when output had to be skipped
        self.term.set_source(self.reader.pull, self.reader.take_dropped)
        # keyboard Ctrl-C should also drop the buffered flood, not just the button
        self.term.on_interrupt = lambda: self.reader.flush() if self.reader else None
        self._on_term_resize(self.term.cols, self.term.rows)

        # focus the working terminal FIRST so an SFTP hiccup can't steal it
        self.logs_tab.set_handler(handler)
        self.inner.setCurrentIndex(0)
        self.term.setFocus()

        # populate the SFTP tab — never let an SFTP error break the session
        rebind = getattr(self.sftp_page, "rebind", None)
        if callable(rebind):
            # a reconnect: the browser keeps its queue, transfer history and
            # open editors, and just carries on over the new connection
            try:
                rebind(handler)
            except Exception as exc:
                self.log.emit(f"[ERROR] SFTP browser: {exc}")
            return
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
        # the reader signals this when the channel ends: either the remote shell
        # ended (exit / logout, or an account whose shell quits at once) — then
        # it stays down until Ctrl+R — or the link died, and it reconnects
        # automatically for ~30 s
        if self._user_closed or self._reconnecting:
            return
        status = self._exit_status()
        if status is not None:
            self._on_shell_ended(status)
            return
        self.connected.emit(False, f"Disconnected from {self.session.get('host')}")
        # a connection that dies right after it opened would otherwise be
        # retried in a tight connect / drop loop
        lived = time.monotonic() - self._connected_at
        self._quick_drops = self._quick_drops + 1 if lived < _QUICK_DROP else 0
        delay = min(_MAX_BACKOFF, 2.0 ** self._quick_drops) if self._quick_drops else 0.0
        self._start_reconnect(delay=delay)

    def _exit_status(self):
        """The shell's exit status if the server sent one (the shell ended on its
        own), else None (the link died: no status; a shell killed by a reboot
        sends a signal, not a status, so that reconnects too)."""
        chan = getattr(self.shell, "channel", None)
        try:
            if chan is not None and chan.exit_status_ready():
                return chan.exit_status
        except Exception:
            pass
        return None

    def _on_shell_ended(self, status):
        host = self.session.get("host")
        self._quick_drops = 0
        self.status.setText(f"Session ended — {host}")
        self.term.notice(f"\r\n\x1b[33m[the remote shell ended (exit status {status})]"
                         f"\x1b[0m  press \x1b[1mCtrl+R\x1b[0m to reconnect\r\n"
                         .encode("utf-8"))
        self.term.reconnect_armed = True
        self.log.emit(f"[INFO] {self.session.get('name')}: the remote shell ended "
                      f"(exit status {status})")
        self.connected.emit(False, f"Session ended on {host}")

    def _start_reconnect(self, window: float = 30.0, delay: float = 0.0):
        self._reconnecting = True
        self.term.reconnect_armed = False
        self._reconnect_until = time.time() + delay + window
        self.status.setText("Connection lost — reconnecting…")
        wait = f" in {delay:.0f}s, then" if delay else ""
        self.term.notice(f"\r\n\x1b[33m[connection lost — reconnecting{wait} for "
                         f"{window:.0f}s…]\x1b[0m\r\n".encode("utf-8"))
        if delay:
            QTimer.singleShot(int(delay * 1000), self._try_reconnect)
        else:
            self._try_reconnect()

    def _try_reconnect(self):
        if self._reconnecting and not self._user_closed:
            self._connect()      # fail routes to _on_reconnect_fail while reconnecting

    def _on_reconnect_fail(self, msg):
        if self._user_closed:
            return
        if time.time() < self._reconnect_until:
            self.term.notice(b".")                       # progress dots
            QTimer.singleShot(3000, self._try_reconnect)
            return
        # window elapsed -> stop and let the user retry with Ctrl+R
        self._reconnecting = False
        self.status.setText("SSH connection closed")
        self.term.notice("\r\n\x1b[31m[SSH connection closed]\x1b[0m  press "
                         "\x1b[1mCtrl+R\x1b[0m to reconnect\r\n".encode("utf-8"))
        self.term.reconnect_armed = True
        self.term.setFocus()
        self.connected.emit(False, f"SSH connection closed: {self.session.get('host')}")

    def _manual_reconnect(self):
        if self._user_closed or self._reconnecting:
            return
        self.term.reconnect_armed = False
        self._quick_drops = 0              # asked for: no back-off
        self.term.notice("\r\n\x1b[33m[reconnecting…]\x1b[0m\r\n".encode("utf-8"))
        self._start_reconnect()

    def _on_fail(self, msg):
        self.status.setText("Connect failed")
        self.term.notice(f"\r\n\x1b[31m[connect failed]\x1b[0m {msg}\r\n".encode())
        self.log.emit(f"[ERROR] {self.session['name']}: {msg}")
        self.failed.emit(f"{self.session.get('name')}: {msg}")

    def _send(self, data: bytes):
        """Queue input for the shell; never blocks. Ctrl-C is an interrupt: what
        is still queued (the rest of a paste) is dropped and it goes first."""
        writer = self._writer
        if writer is None:
            return
        if data == b"\x03":
            writer.interrupt(data)
        else:
            writer.send(data)

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
        self.term.notice((line1 + "\r\n" + line2 + "\r\n\r\n").encode("utf-8"))

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
        # a window-change request is an SSH message like any other: it waits for
        # a rekey just the same, so it goes through the writer (latest size only)
        shell, writer = self.shell, self._writer
        if shell is not None and writer is not None:
            writer.call(lambda: shell.resize(cols, rows), key="resize")

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
        self._engine_log.close()          # the handler outlives us on a teardown thread
        self._write_gate.close()
        self._user_closed = True          # stop any auto-reconnect
        self._reconnecting = False
        # a connect still in flight: let it finish, then close what it opened
        qtutil.park_thread(self._ct, orphan={"ok": lambda h, _shell: _close_handler(h)})
        try:
            self.logs_tab.close_tab()
        except Exception:
            pass
        self._drop_reader()
        self._drop_writer()
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
    session_kind = "serial"
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
        self._engine_log = _EngineLog(self.log, session.get("name", ""))
        self._write_gate = qtutil.SignalGate(self.log)   # the writer thread's errors
        self._writer = None            # sends keystrokes / pastes off the GUI thread

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
                cfg, session.get("device", "/dev/ser1"), self._engine_log)
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
        self.term.notice(f"\r\n\x1b[31m[{dev} closed — the port was unplugged or the "
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

        # pyserial blocks until every byte is out: 4 KB at 9600 baud is 4 s.
        # Small chunks, so a Ctrl-C waits for at most ~0.5 s of a paste.
        self._writer = Writer(lambda data: handler.write_bytes(data, safe=False),
                              on_error=self._on_write_error, chunk=512,
                              name="serial-input")
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
                self.term.notice(f"--- {dev} is in use; not opened ---\r\n".encode())
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
        chan.settimeout(0.1)               # the reader's; the writer copes with it
        self.status.setText(f"Serial over SSH — {dev} @ {baud} (native)")
        self.log.emit(f"[OK] serial-over-SSH {dev} (native bridge)")
        self.connected.emit(True, f"Serial over SSH: {dev}")
        self._serial_banner(dev, baud, True)
        self._writer = Writer(_channel_writer(chan), on_error=self._on_write_error,
                              name="serial-bridge-input")
        self._start_reader(_channel_reader(chan))
        # match the remote PTY to our terminal so the conpty doesn't pad the
        # output with stray blank lines.
        self._on_term_resize(self.term.cols, self.term.rows)

    def _on_write_error(self, exc):
        self._write_gate.emit(f"[ERROR] serial write: {exc}")   # (writer thread)

    def _on_term_resize(self, cols, rows):
        # keep the remote bridge's PTY the SAME size as our terminal — an oversized
        # (250x50) conpty was repositioning output across a taller screen, which is
        # what produced the stray blank lines in serial-over-SSH. (Through the
        # writer: an SSH request can wait seconds on a slow or rekeying link.)
        chan, writer = self.bridge_chan, self._writer
        if chan is None or writer is None:
            return

        def resize():
            if not chan.closed:
                chan.resize_pty(width=int(cols), height=int(rows))
        writer.call(resize, key="resize")

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
            self.term.notice(f"\r\n\x1b[31m[{dev} is in use by another "
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
        self.term.notice(f"\r\n\x1b[31m[serial open failed]\x1b[0m {msg}\r\n".encode())
        self.log.emit(f"[ERROR] serial: {msg}")
        self.failed.emit(f"{self.session.get('name')}: {msg}")

    def _send(self, data: bytes):
        """Native input (via_ssh -> raw SSH bridge channel; local -> the port),
        queued for the writer thread: never blocks. Ctrl-C drops what is still
        queued and goes first."""
        writer = self._writer
        if writer is None:
            return
        if data == b"\x03":
            writer.interrupt(data)
        else:
            writer.send(data)

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
        self.term.notice((line1 + "\r\n" + line2 + "\r\n\r\n").encode("utf-8"))

    def close_session(self):
        """Release the port and close the connection — on a background thread,
        because the release is a remote command (seconds on Windows) and used to
        freeze the window on every tab close and once per tab at exit. The main
        window joins these teardowns before the process ends."""
        if self._closed:
            return
        self._closed = True
        self._engine_log.close()          # the handler outlives us on a teardown thread
        self._write_gate.close()
        # an open still in flight: let it finish, then close what it opened
        qtutil.park_thread(self._ct, orphan={"ok": lambda h, *_: _close_handler(h)})
        qtutil.close_jobs(self._jobs)
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.stop()
            qtutil.park_thread(reader)
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()                # a write still blocked ends with the port
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
