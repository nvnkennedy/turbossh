"""
The whole TurboSSH GUI inside your own PyQt5 application.

    pip install "turbossh[gui]"
    python examples/embed_demo.py

A small host window with a page of its own and TurboSSH on a second tab. It shows
the parts you would normally use: TurboSSH's commands on the host's toolbar, its
menus in the host's menu bar, a connection opened from code, and a clean shutdown.
EMBEDDING.md is the full reference.
"""

import sys

from PyQt5.QtWidgets import (QApplication, QFormLayout, QLabel, QLineEdit, QMainWindow,
                             QPushButton, QTabWidget, QWidget)

from turbossh.gui import TurboSSHWidget


class HostWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("My test bench")
        self.resize(1200, 800)

        # TurboSSH's menus go in OUR menu bar, so it does not need its own.
        self.turbo = TurboSSHWidget(self, menu_bar=False)
        for menu in self.turbo.menus():
            self.menuBar().addMenu(menu)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._bench_page(), "Bench")
        self.tabs.addTab(self.turbo, "TurboSSH")
        self.setCentralWidget(self.tabs)

        # Any TurboSSH command can sit on our toolbar. action_names() lists them.
        toolbar = self.addToolBar("Bench")
        for name in ("session", "local", "split", "settings"):
            toolbar.addAction(self.turbo.action(name))

        self.turbo.session_opened.connect(
            lambda s: self.statusBar().showMessage(f"Opened {s.name}", 4000))
        self.turbo.log_message.connect(print)

    def _bench_page(self):
        """The host's own content, and a connection started from it."""
        page = QWidget()
        form = QFormLayout(page)
        self.host = QLineEdit("10.0.0.5")
        self.user = QLineEdit("root")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        go = QPushButton("Connect in the TurboSSH tab")
        go.clicked.connect(self.connect_target)
        form.addRow(QLabel("A page of the host application."))
        form.addRow("Host", self.host)
        form.addRow("User", self.user)
        form.addRow("Password", self.password)
        form.addRow(go)
        return page

    def connect_target(self):
        session = self.turbo.connect_ssh(
            self.host.text().strip(), username=self.user.text().strip(),
            password=self.password.text())
        session.connected.connect(lambda: session.send_line("uname -a"))
        session.failed.connect(lambda why: self.statusBar().showMessage(why, 8000))
        self.tabs.setCurrentWidget(self.turbo)
        session.activate()

    def closeEvent(self, event):
        if not self.turbo.confirm_close():   # "N sessions will be closed. Continue?"
            event.ignore()
            return
        # wait: the process is about to end, and remote COM ports must be released
        self.turbo.shutdown(wait=6.0)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = HostWindow()
    window.show()
    sys.exit(app.exec_())
