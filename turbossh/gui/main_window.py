"""tabbed multi-session main window: ribbon toolbar, Quick-connect + session-tree
sidebar, tabbed sessions (VT100 SSH terminals + SFTP browser, serial consoles,
RDP launch), MultiExec broadcast, and a dark log dock."""

from __future__ import annotations

import os

from PyQt5.QtCore import Qt, QSize, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QIcon, QKeySequence
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QGridLayout, QListWidget, QListWidgetItem, QPushButton,
                             QTabWidget, QDockWidget, QLabel, QLineEdit, QMessageBox,
                             QToolBar, QAction, QStatusBar, QInputDialog, QShortcut,
                             QToolButton, QApplication, QSplitter, QDialog, QFormLayout,
                             QSpinBox, QDialogButtonBox, QCheckBox, QScrollArea)

from . import theme
from .log_panel import LogPanel
from .sessions import SessionStore
from .session_dialog import SessionDialog
from .settings_dialog import SettingsDialog
from .session_widgets import SshSessionWidget, SerialSessionWidget

ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "assets", "icon.ico")


class _RemoteInstallDialog(QDialog):
    """Collects the WinRM details for installing OpenSSH on a remote machine."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Install SSH server on a remote machine")
        self.setMinimumWidth(460)
        from . import theme
        lay = QVBoxLayout(self)
        msg = QLabel("Install OpenSSH Server on another Windows machine over "
                     "WinRM — fully offline (the bundled OpenSSH is pushed to it, "
                     "no downloads). Needs WinRM (port 5985) reachable and a "
                     "local-admin account on that machine.")
        msg.setWordWrap(True); lay.addWidget(msg)
        form = QFormLayout(); lay.addLayout(form)
        self.host = QLineEdit(); self.host.setPlaceholderText("RDP machine IP, e.g. 10.232.9.120")
        self.user = QLineEdit(); self.user.setPlaceholderText("a local-admin user on that machine")
        self.domain = QLineEdit(); self.domain.setPlaceholderText("Windows domain (optional)")
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.Password)
        theme.attach_eye(self.password)
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(22)
        self.port.setToolTip("SSH port to open on the remote machine.")
        form.addRow("Host", self.host)
        form.addRow("User", self.user)
        form.addRow("Domain", self.domain)
        form.addRow("Password", self.password)
        form.addRow("SSH port", self.port)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Install")
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def values(self) -> dict:
        return {"host": self.host.text().strip(), "user": self.user.text().strip(),
                "domain": self.domain.text().strip(),
                "password": self.password.text(), "port": self.port.value()}


class _RemoteInstallThread(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, params, openssh_dir):
        super().__init__()
        self.p = params
        self.openssh_dir = openssh_dir

    def run(self):
        from ..winrm_bootstrap import enable_openssh_via_winrm_offline
        try:
            res = enable_openssh_via_winrm_offline(
                self.p["host"], self.p["user"], self.p["password"], self.openssh_dir,
                domain=self.p.get("domain") or None, ssh_port=self.p.get("port", 22),
                log=self.progress.emit)
            self.done.emit(True, res.get("status", "Running"))
        except Exception as exc:
            self.done.emit(False, f"{type(exc).__name__}: {exc}")


class _BatchSshInstallDialog(QDialog):
    """Pick which previously-used machines to (re)install OpenSSH on, with one
    shared local-admin credential (typical in a domain)."""

    def __init__(self, hosts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Install OpenSSH on your machines")
        self.setMinimumWidth(480)
        from . import theme
        lay = QVBoxLayout(self)
        info = QLabel("Install / refresh the bundled OpenSSH Server (offline) on "
                      "the machines you use, over WinRM. Tick the ones to do and "
                      "enter a local-admin account (one that works on all of them).")
        info.setWordWrap(True); lay.addWidget(info)

        lay.addWidget(QLabel("Machines:"))
        box = QScrollArea(); box.setWidgetResizable(True); box.setMaximumHeight(170)
        inner = QWidget(); il = QVBoxLayout(inner)
        self._checks = []
        for h in hosts:
            cb = QCheckBox(h); cb.setChecked(True)
            il.addWidget(cb); self._checks.append(cb)
        il.addStretch(1)
        box.setWidget(inner); lay.addWidget(box)

        form = QFormLayout(); lay.addLayout(form)
        self.user = QLineEdit(); self.user.setPlaceholderText("local-admin user")
        self.domain = QLineEdit(); self.domain.setPlaceholderText("Windows domain (optional)")
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.Password)
        theme.attach_eye(self.password)
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(22)
        form.addRow("User", self.user)
        form.addRow("Domain", self.domain)
        form.addRow("Password", self.password)
        form.addRow("SSH port", self.port)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Install")
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def selected(self) -> dict:
        return {"hosts": [c.text() for c in self._checks if c.isChecked()],
                "user": self.user.text().strip(), "domain": self.domain.text().strip(),
                "password": self.password.text(), "port": self.port.value()}


class _BatchInstallThread(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(str)               # final summary

    def __init__(self, hosts, creds, openssh_dir):
        super().__init__()
        self.hosts = hosts
        self.creds = creds
        self.openssh_dir = openssh_dir

    def run(self):
        from ..winrm_bootstrap import enable_openssh_via_winrm_offline
        ok, fail = [], []
        for h in self.hosts:
            self.progress.emit(f"=== {h}: installing OpenSSH over WinRM… ===")
            try:
                enable_openssh_via_winrm_offline(
                    h, self.creds["user"], self.creds["password"], self.openssh_dir,
                    domain=self.creds.get("domain") or None,
                    ssh_port=self.creds.get("port", 22), log=self.progress.emit)
                ok.append(h)
                self.progress.emit(f"=== {h}: OK ===")
            except Exception as exc:
                fail.append(h)
                self.progress.emit(f"=== {h}: FAILED — {exc} ===")
        self.done.emit(f"OpenSSH install finished — {len(ok)} ok, {len(fail)} failed."
                       + (f" Failed: {', '.join(fail)}" if fail else ""))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TurboSSH — SSH / Serial / SFTP / RDP terminal")
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(1200, 780)
        # theme is applied app-wide in app.main(); set here too for safety
        from . import settings as settings_mod
        QApplication.instance().setStyleSheet(theme.stylesheet(settings_mod.get("theme")))

        self.store = SessionStore()
        self._tiled = False
        self._tiled_widget = None
        self._build_menubar()
        self._build_ribbon()
        self._build_sidebar()
        self._build_center()
        self._build_log_dock()

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("TurboSSH ready — create or open a session")
        self._install_shortcuts()
        self.refresh_sessions()

    # ---- menu bar ----
    def _build_menubar(self):
        mb = self.menuBar()
        ic = theme.emoji_icon

        m_file = mb.addMenu("&File")
        m_file.addAction(ic("🖥"), "New session…", self.new_session,
                         QKeySequence("Ctrl+N"))
        from . import settings as _s
        if _s.get("camera_enabled"):
            m_file.addAction(ic("📷"), "New camera session…", self.new_camera_session)
        m_file.addAction(ic("💾"), "Save terminal output…", self._save_current_output,
                         QKeySequence("Ctrl+S"))
        m_file.addSeparator()
        m_file.addAction(ic("🖧"), "Set up SSH server on this PC…",
                         self.setup_ssh_server)
        m_file.addSeparator()
        m_file.addAction(ic("⏻", "#f06e60"), "Exit", self.close,
                         QKeySequence("Ctrl+Q"))

        m_edit = mb.addMenu("&Edit")
        m_edit.addAction(ic("📋"), "Copy", self._term_copy,
                         QKeySequence("Ctrl+Shift+C"))
        m_edit.addAction(ic("📥"), "Paste", self._term_paste,
                         QKeySequence("Ctrl+Shift+V"))
        m_edit.addSeparator()
        m_edit.addAction(ic("⤓"), "Scroll to bottom (live)", self._term_to_bottom)
        m_edit.addAction(ic("🧹"), "Clear screen", self._term_clear)

        m_view = mb.addMenu("&View")
        m_view.addAction(ic("📁"), "SFTP", lambda: self._show_inner("SFTP"))
        m_view.addAction(ic("📜"), "Logs", lambda: self._show_inner("Logs"))
        m_view.addAction(ic("🔲"), "Split / unsplit", self.toggle_split)
        m_view.addAction(ic("🧾"), "Show / hide log dock", self.toggle_log)

        m_session = mb.addMenu("&Session")
        m_session.addAction(ic("▶"), "Open / Connect", self.open_selected)
        m_session.addAction(ic("✏"), "Edit…", self.edit_session)
        m_session.addAction(ic("🗑"), "Delete", self.delete_session)

        m_help = mb.addMenu("&Help")
        m_help.addAction(ic("⬆"), "Check for updates…", self.check_updates)
        m_help.addAction(ic("❓"), "Documentation", self._open_docs)

    # --- menu helpers that act on the current session's terminal ---
    def _current_term(self):
        w = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        return getattr(w, "term", None)

    def _save_current_output(self):
        t = self._current_term()
        if t is not None and hasattr(t, "_save_output"):
            t._save_output()
        else:
            QMessageBox.information(self, "Save output",
                                    "Open a session terminal first.")

    def _term_copy(self):
        t = self._current_term()
        if t is not None:
            t._copy()

    def _term_paste(self):
        t = self._current_term()
        if t is not None:
            t._paste()

    def _term_clear(self):
        t = self._current_term()
        if t is not None:
            t.clear()

    def _term_to_bottom(self):
        t = self._current_term()
        if t is not None and hasattr(t, "_to_bottom_update"):
            t._to_bottom_update()

    def setup_ssh_server(self):
        """Offer to install OpenSSH Server either on THIS machine (offline,
        self-elevating) or on a REMOTE machine over WinRM (offline upload)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Set up SSH server")
        box.setText("Install OpenSSH Server (offline, from the bundled package) so "
                    "the machine accepts SSH — e.g. an RDP machine whose COM ports "
                    "you want to reach.\n\nWhere should it be installed?")
        this_btn = box.addButton("This PC", QMessageBox.AcceptRole)
        remote_btn = box.addButton("A remote machine (WinRM)…", QMessageBox.ActionRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(this_btn)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is remote_btn:
            self._setup_ssh_server_remote()
            return
        if clicked is not this_btn:
            return
        self._setup_ssh_server_local()

    def _setup_ssh_server_remote(self):
        """Install OpenSSH on another machine over WinRM (offline upload)."""
        dlg = _RemoteInstallDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        vals = dlg.values()
        if not vals["host"] or not vals["user"]:
            QMessageBox.warning(self, "Missing details",
                                "Enter at least the remote Host and a local-admin User.")
            return
        from ..cli import _setup_script_path
        openssh_dir = os.path.join(os.path.dirname(_setup_script_path()), "openssh")
        self.log_panel.append(f"[..] Installing OpenSSH on {vals['host']} over WinRM "
                              f"(offline upload — this can take a few minutes)…")
        self.statusBar().showMessage(f"Installing OpenSSH on {vals['host']} over WinRM…")
        self._remote_install = _RemoteInstallThread(vals, openssh_dir)
        self._remote_install.progress.connect(
            lambda m: self.log_panel.append(f"    {m}"))
        self._remote_install.done.connect(
            lambda ok, msg: self._on_remote_install_done(ok, msg, vals["host"]))
        self._remote_install.start()

    def _on_remote_install_done(self, ok, msg, host):
        if ok:
            self.log_panel.append(f"[OK] OpenSSH installed on {host} (sshd: {msg}).")
            self.statusBar().showMessage(f"OpenSSH installed on {host}", 8000)
            QMessageBox.information(
                self, "Remote SSH server ready",
                f"✓ OpenSSH Server is installed and running on {host}.\n\n"
                f"You can now create an SSH (or serial-via-RDP) session to it.")
        else:
            self.log_panel.append(f"[ERROR] Remote OpenSSH install on {host} failed: {msg}")
            QMessageBox.warning(
                self, "Remote install failed",
                f"Couldn't install OpenSSH on {host} over WinRM:\n\n{msg}\n\n"
                f"Check that WinRM (port 5985) is enabled on it and the account is a "
                f"local admin. If WinRM isn't available, run TurboSSH (or "
                f"'turbossh-setup') directly on that machine instead.")

    def _setup_ssh_server_local(self):
        """Install + start OpenSSH Server on THIS machine, offline, from the
        bundled package. Self-elevates to Administrator."""
        if os.name != "nt":
            QMessageBox.information(self, "SSH server setup",
                                    "This sets up OpenSSH Server and is Windows-only.")
            return
        r = QMessageBox.question(
            self, "Set up SSH server on this PC",
            "This installs and starts OpenSSH Server on THIS Windows machine "
            "(offline — from the OpenSSH bundled with TurboSSH, no internet "
            "needed) and opens the firewall on port 22.\n\n"
            "Use this on the RDP machine so TurboSSH can reach its COM ports.\n\n"
            "Windows will prompt for Administrator. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if r != QMessageBox.Yes:
            return
        try:
            import time
            from ..cli import launch_setup_server, sshd_result_file
            if launch_setup_server():
                self.log_panel.append("[..] OpenSSH Server setup launched — "
                                      "approve the Administrator prompt.")
                self.statusBar().showMessage("Setting up OpenSSH Server… "
                                             "(approve the Administrator prompt)")
                # poll the result file the elevated script writes when it finishes
                self._sshd_result_path = sshd_result_file()
                self._sshd_launch_t = time.time()
                self._sshd_polls = 0
                self._sshd_timer = QTimer(self)
                self._sshd_timer.timeout.connect(self._poll_sshd_result)
                self._sshd_timer.start(2000)
            else:
                QMessageBox.warning(self, "SSH server setup",
                                    "Couldn't launch the setup. Run "
                                    "'turbossh-setup' from a terminal instead.")
        except Exception as exc:
            QMessageBox.warning(self, "SSH server setup", f"Setup failed: {exc}")

    def _poll_sshd_result(self):
        """Watch for the result file the (elevated) setup writes, then report
        the outcome in the GUI. Times out after ~3 minutes."""
        import os as _os
        self._sshd_polls += 1
        if self._sshd_polls > 90:                      # ~3 min
            self._sshd_timer.stop()
            self.statusBar().showMessage("SSH server setup: no result yet — "
                                         "check the setup window.", 8000)
            return
        path = getattr(self, "_sshd_result_path", "")
        try:
            if not path or not _os.path.exists(path):
                return
            if _os.path.getmtime(path) < self._sshd_launch_t - 2:
                return                                 # stale result from before
            with open(path, "r", encoding="utf-8") as fh:
                info = dict(l.strip().split("=", 1) for l in fh
                            if "=" in l)
        except Exception:
            return
        self._sshd_timer.stop()
        ok = info.get("status") == "OK"
        port = info.get("port", "22")
        if ok:
            self.log_panel.append(f"[OK] OpenSSH Server is running and listening "
                                  f"on port {port}.")
            self.statusBar().showMessage(f"SSH server ready — listening on port {port}",
                                         8000)
            QMessageBox.information(
                self, "SSH server ready",
                f"✓ OpenSSH Server is installed, running, and listening on port "
                f"{port} on this machine.\n\nTurboSSH (and other machines) can now "
                f"SSH in — including to reach its COM ports.")
        else:
            sshd = info.get("sshd", "?")
            self.log_panel.append(f"[ERROR] OpenSSH setup finished but not listening "
                                  f"(sshd={sshd}, port={port}).")
            QMessageBox.warning(
                self, "SSH server not listening",
                f"Setup finished but port {port} isn't listening yet "
                f"(sshd status: {sshd}).\n\nLikely a firewall / Group Policy "
                f"blocking inbound {port}, or the service didn't start. On this "
                f"machine try:\n\n    Restart-Service sshd\n    Get-Service sshd\n\n"
                f"or re-run with force from a terminal:  turbossh-setup -Force")

    # ---- ribbon toolbar ----
    def _build_ribbon(self):
        tb = QToolBar("Ribbon")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(26, 26))
        self.addToolBar(tb)
        from . import settings as _s
        items = [
            ("🖥", "Session", self.new_session),
            ("📁", "SFTP", lambda: self._show_inner("SFTP")),
            ("📜", "Logs", lambda: self._show_inner("Logs")),
            ("🔲", "Split", self.toggle_split),
            ("✏", "Edit", self.edit_session),
            ("🗑", "Delete", self.delete_session),
            ("🧾", "Log dock", self.toggle_log),
            ("🖧", "SSH server", self.setup_ssh_server),
            ("⬆", "Check updates", self.check_updates),
            ("⚙", "Settings", self.show_settings),
            ("❓", "Help", self._open_docs),
        ]
        # Camera is opt-in — only add its ribbon button when enabled in Settings.
        if _s.get("camera_enabled"):
            items.insert(1, ("📷", "Camera", self.new_camera_session))
        for emoji, label, slot in items:
            act = QAction(theme.emoji_icon(emoji), label, self)
            act.triggered.connect(slot)
            tb.addAction(act)
        from PyQt5.QtWidgets import QSizePolicy
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        exit_act = QAction(theme.emoji_icon("⏻", "#f06e60"), "Exit", self)
        exit_act.triggered.connect(self.close)
        tb.addAction(exit_act)
        btn = tb.widgetForAction(exit_act)
        if btn is not None:
            btn.setStyleSheet("color:#f06e60; font-weight:700;")

    def check_updates(self):
        """Manually check PyPI for a newer version (ribbon button). Runs the
        network call off the UI thread; reports the outcome either way. After the
        version result it also offers to install/refresh OpenSSH on your machines."""
        from .updater import check_now
        check_now(self)

    def _collect_remote_hosts(self):
        """Windows machines we've used as SSH gateways / serial-over-SSH hosts /
        jump hosts — the candidates for an OpenSSH (re)install."""
        hosts = []
        try:
            for s in self.store.sessions:
                if s.get("use_jump") and s.get("jhost"):
                    hosts.append(s["jhost"].strip())
                if s.get("type") == "serial" and s.get("via_ssh") and s.get("host"):
                    hosts.append(s["host"].strip())
        except Exception:
            pass
        # de-dup, keep order
        seen, out = set(), []
        for h in hosts:
            if h and h not in seen:
                seen.add(h); out.append(h)
        return out

    def offer_openssh_install(self):
        """Called after a manual update check: offer to install/refresh the
        bundled OpenSSH on the machines this user works with (over WinRM)."""
        if os.name != "nt":
            return
        hosts = self._collect_remote_hosts()
        if not hosts:
            return
        shown = ", ".join(hosts[:6]) + ("…" if len(hosts) > 6 else "")
        r = QMessageBox.question(
            self, "OpenSSH on your machines",
            "TurboSSH bundles OpenSSH Server for offline install.\n\n"
            f"Install or refresh it on the remote machine(s) you use, over WinRM?\n\n{shown}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        dlg = _BatchSshInstallDialog(hosts, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        sel = dlg.selected()
        if not sel["hosts"] or not sel["user"]:
            QMessageBox.warning(self, "Nothing to do",
                                "Pick at least one machine and enter a user.")
            return
        from ..cli import _setup_script_path
        openssh_dir = os.path.join(os.path.dirname(_setup_script_path()), "openssh")
        self.log_panel.append(f"[..] Installing OpenSSH on {len(sel['hosts'])} "
                              f"machine(s) over WinRM…")
        self.statusBar().showMessage("Installing OpenSSH on your machines…")
        self._batch_install = _BatchInstallThread(
            sel["hosts"], sel, openssh_dir)
        self._batch_install.progress.connect(lambda m: self.log_panel.append(f"    {m}"))
        self._batch_install.done.connect(self._on_batch_install_done)
        self._batch_install.start()

    def _on_batch_install_done(self, summary):
        self.log_panel.append(f"[OK] {summary}")
        self.statusBar().showMessage(summary, 10000)
        QMessageBox.information(self, "OpenSSH install finished", summary)

    def _show_inner(self, name):
        """Switch the CURRENT session tab to its Terminal/SFTP/Logs inner tab."""
        w = self.tabs.currentWidget()
        if w is not None and hasattr(w, "inner"):
            for i in range(w.inner.count()):
                if w.inner.tabText(i) == name:
                    w.inner.setCurrentIndex(i)
                    return
        self.log_panel.append(f"[i] Open a session first to use {name}.")

    # ---- sidebar (quick connect + session tree) ----
    def _build_sidebar(self):
        dock = QDockWidget("Sessions", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        panel = QWidget()
        lay = QVBoxLayout(panel)
        self.quick = QLineEdit()
        self.quick.setPlaceholderText("Quick connect…  (filter / Enter to open)")
        self.quick.textChanged.connect(self._filter_sessions)
        self.quick.returnPressed.connect(self._quick_enter)
        lay.addWidget(self.quick)

        self.session_list = QListWidget()
        self.session_list.itemDoubleClicked.connect(lambda _: self.open_selected())
        self.session_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._session_menu)
        lay.addWidget(self.session_list, 1)

        newt = QPushButton("➕  New session"); newt.setProperty("role", "ok")
        newt.clicked.connect(self.new_session)
        showtab = QPushButton("Show / hide log"); showtab.setProperty("role", "ghost")
        showtab.clicked.connect(self.toggle_log)
        lay.addWidget(newt); lay.addWidget(showtab)

        dock.setWidget(panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self._sidebar_dock = dock

    # ---- center tabs with a "+" corner button ----
    def _build_center(self):
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_menu)
        plus = QToolButton()
        plus.setText("  +  ")
        plus.setToolTip("New session")
        plus.clicked.connect(self.new_session)
        self.tabs.setCornerWidget(plus, Qt.TopRightCorner)
        self.setCentralWidget(self.tabs)

    def _tab_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        bar = self.tabs.tabBar()
        idx = bar.tabAt(pos)
        if idx < 0:
            return
        self.tabs.setCurrentIndex(idx)
        m = QMenu(self)
        m.addAction("Close", lambda: self._close_tab(idx))
        m.addAction("Close others", lambda: self._close_others(idx))
        m.addAction("Close to the left", lambda: self._close_side(idx, "left"))
        m.addAction("Close to the right", lambda: self._close_side(idx, "right"))
        m.addSeparator()
        m.addAction("Close all", self._close_all_tabs)
        m.exec_(bar.mapToGlobal(pos))

    def _close_others(self, keep_idx):
        keep = self.tabs.widget(keep_idx)
        for i in range(self.tabs.count() - 1, -1, -1):
            if self.tabs.widget(i) is not keep:
                self._close_tab(i)

    def _close_side(self, idx, side):
        rng = range(idx - 1, -1, -1) if side == "left" \
            else range(self.tabs.count() - 1, idx, -1)
        for i in rng:
            self._close_tab(i)

    def _close_all_tabs(self):
        for i in range(self.tabs.count() - 1, -1, -1):
            self._close_tab(i)

    def _build_log_dock(self):
        self.log_panel = LogPanel()
        dock = QDockWidget("Log", self)
        dock.setWidget(self.log_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self._log_dock = dock

    def _install_shortcuts(self):
        for seq, slot in (("Ctrl+T", self.new_session), ("Ctrl+N", self.new_session),
                          ("Ctrl+W", self._close_current_tab),
                          ("Ctrl+Return", self.open_selected), ("F1", self._open_docs)):
            QShortcut(QKeySequence(seq), self, activated=slot)

    # ---- sessions ----
    def refresh_sessions(self):
        self.session_list.clear()
        for s in self.store.sessions:
            t = s.get("type")
            if t == "serial":
                icon = "📡" if s.get("via_ssh") else "🔌"
                sub = "serial/ssh" if s.get("via_ssh") else "serial"
            elif t == "camera":
                icon = "📷"; sub = "camera"
            else:
                icon = "🖥"; sub = "ssh"
            it = QListWidgetItem(f"{icon}  {s.get('name')}   ·  {sub}")
            it.setData(Qt.UserRole, s.get("name"))
            self.session_list.addItem(it)
        self._filter_sessions(self.quick.text())

    def _filter_sessions(self, text):
        text = (text or "").lower()
        for i in range(self.session_list.count()):
            it = self.session_list.item(i)
            it.setHidden(bool(text) and text not in it.text().lower())

    def _quick_enter(self):
        for i in range(self.session_list.count()):
            it = self.session_list.item(i)
            if not it.isHidden():
                self.session_list.setCurrentItem(it)
                self.open_selected()
                return
        # no match: offer a new session prefilled with the typed host
        host = self.quick.text().strip()
        if host:
            self.new_session(prefill_host=host)

    def _session_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction(theme.emoji_icon("🖥"), "New session…", self.new_session)
        item = self.session_list.itemAt(pos)
        if item is not None:
            self.session_list.setCurrentItem(item)
            menu.addSeparator()
            menu.addAction(theme.emoji_icon("▶"), "Open / Connect", self.open_selected)
            menu.addAction(theme.emoji_icon("✏"), "Edit…", self.edit_session)
            menu.addAction(theme.emoji_icon("📄"), "Duplicate", self._duplicate_session)
            menu.addSeparator()
            menu.addAction(theme.emoji_icon("🗑"), "Delete", self.delete_session)
        menu.exec_(self.session_list.viewport().mapToGlobal(pos))

    def _duplicate_session(self):
        name = self._selected_name()
        if not name:
            return
        s = dict(self.store.get(name) or {})
        if not s:
            return
        s["name"] = s.get("name", "session") + " (copy)"
        self.store.save(s, SessionStore.password(name) or "",
                        SessionStore.jump_password(name) or "")
        self.refresh_sessions()
        self.log_panel.append(f"[OK] Duplicated '{name}'")

    def _selected_name(self):
        it = self.session_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def new_session(self, *_, prefill_host=None, prefer_type=None):
        dlg = SessionDialog(self)
        if prefill_host:
            dlg.host.setText(prefill_host)
        if prefer_type and prefer_type in dlg.rb:
            dlg.rb[prefer_type].setChecked(True)
        if dlg.exec_() == dlg.Accepted:
            s = dlg.result_session()
            if not s["name"]:
                QMessageBox.warning(self, "Session", "Give the session a name.")
                return
            self.store.save(s, dlg.password_value(), dlg.jump_password_value())
            self.refresh_sessions()
            self.log_panel.append(f"[OK] Saved session '{s['name']}'")
            # select it in the list and auto-connect
            for i in range(self.session_list.count()):
                if self.session_list.item(i).data(Qt.UserRole) == s["name"]:
                    self.session_list.setCurrentRow(i)
                    break
            self.open_selected()

    def new_camera_session(self, *_):
        """Open the New-session dialog with the Camera type pre-selected."""
        self.new_session(prefer_type="camera")

    def edit_session(self):
        name = self._selected_name()
        if not name:
            return
        dlg = SessionDialog(self, existing=self.store.get(name))
        if dlg.exec_() == dlg.Accepted:
            self.store.save(dlg.result_session(), dlg.password_value(),
                            dlg.jump_password_value())
            self.refresh_sessions()

    def delete_session(self):
        name = self._selected_name()
        if name and QMessageBox.question(self, "Delete", f"Delete '{name}'?") == QMessageBox.Yes:
            self.store.delete(name)
            self.refresh_sessions()

    def open_selected(self):
        name = self._selected_name()
        if not name:
            QMessageBox.information(self, "Connect", "Select a session first.")
            return
        s = self.store.get(name)
        pw = SessionStore.password(name) or ""
        jpw = SessionStore.jump_password(name) or ""
        kind = s.get("type")
        # a serial port can't be shared — if it's already open, offer to reopen
        if kind == "serial":
            dev = s.get("device")
            for i in range(self.tabs.count()):
                tw = self.tabs.widget(i)
                if isinstance(tw, SerialSessionWidget) and tw.session.get("device") == dev:
                    resp = QMessageBox.question(
                        self, "Serial port in use",
                        f"A serial session for {dev} is already open.\n\n"
                        "Close it and open this one?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if resp != QMessageBox.Yes:
                        return
                    self._close_tab(i)
                    break
        if kind == "camera":
            from .camera_widget import CameraSessionWidget
            w = CameraSessionWidget(s, pw, jpw)
        elif kind == "serial":
            w = SerialSessionWidget(s, pw, jpw)
        else:
            w = SshSessionWidget(s, pw, jpw)
        w.log.connect(self.log_panel.append)
        # connection popups / status (SSH sessions emit connected/failed)
        if hasattr(w, "failed"):
            w.failed.connect(self._on_session_failed)
        if hasattr(w, "connected"):
            w.connected.connect(self._on_session_connected)
        idx = self.tabs.addTab(w, name)
        self.tabs.setCurrentIndex(idx)
        self.log_panel.append(f"Opening '{name}'…")
        self.statusBar().showMessage(f"Connecting to {s.get('host')}…")

    def _on_session_failed(self, msg):
        self.statusBar().showMessage("Connection failed")
        QMessageBox.warning(self, "Connection failed", msg)

    def _on_session_connected(self, ok, msg):
        self.statusBar().showMessage(msg)
        if not ok:
            QMessageBox.information(self, "Connection", msg)

    def toggle_log(self):
        self._log_dock.setVisible(not self._log_dock.isVisible())

    def toggle_split(self):
        """Tile open sessions in a grid (tabbed multi-session multi-view) <-> tabs."""
        from . import theme as _t
        if not self._tiled:
            if self.tabs.count() == 0:
                QMessageBox.information(self, "Split", "Open one or more sessions first.")
                return
            self._tiled_items = []
            while self.tabs.count():
                self._tiled_items.append((self.tabs.widget(0), self.tabs.tabText(0)))
                self.tabs.removeTab(0)                  # widget kept (we hold the ref)
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setSpacing(4)
            grid.setContentsMargins(4, 4, 4, 4)
            n = len(self._tiled_items)
            cols = 1 if n == 1 else 2
            for i, (w, title) in enumerate(self._tiled_items):
                cell = QWidget()
                v = QVBoxLayout(cell); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(2)
                cap = QLabel("  " + title)
                cap.setStyleSheet(f"background:{_t.THEMES['dark']['ribbon']};"
                                  f"color:{_t.ACCENT};padding:4px;border-radius:5px;"
                                  f"font-weight:600;")
                v.addWidget(cap); v.addWidget(w, 1)
                w.setVisible(True); w.show()        # removeTab hid it — re-show
                grid.addWidget(cell, i // cols, i % cols)
            self.tabs.setParent(None)                   # detach so it's not deleted
            self.setCentralWidget(grid_host)
            self._tiled = True
            self.log_panel.append(f"[OK] Tiled {n} session(s). Click Split again for tabs.")
        else:
            for (w, title) in getattr(self, "_tiled_items", []):
                self.tabs.addTab(w, title)              # reparents back to tabs
            self.setCentralWidget(self.tabs)            # deletes the grid host
            self._tiled = False
            self.log_panel.append("[OK] Back to tabbed view.")

    def show_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec_() == dlg.Accepted:
            from . import settings as settings_mod, theme as _t
            cfg = dlg.result_settings()
            settings_mod.save(cfg)
            QApplication.instance().setStyleSheet(_t.stylesheet(cfg["theme"]))
            self.log_panel.append(f"[OK] Settings saved — theme: {cfg['theme']}")

    def _close_tab(self, index):
        w = self.tabs.widget(index)
        try:
            w.close_session()
        except Exception:
            pass
        self.tabs.removeTab(index)

    def _close_current_tab(self):
        i = self.tabs.currentIndex()
        if i >= 0:
            self._close_tab(i)

    def _open_docs(self):
        import webbrowser
        webbrowser.open("https://pypi.org/project/turbossh/")

    def closeEvent(self, event):
        n = self.tabs.count()
        if n > 0:
            resp = QMessageBox.question(
                self, "Quit TurboSSH",
                f"{n} open session(s) will be disconnected and closed.\n\nQuit?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                event.ignore()
                return
        for i in range(self.tabs.count()):
            try:
                self.tabs.widget(i).close_session()
            except Exception:
                pass
        super().closeEvent(event)
