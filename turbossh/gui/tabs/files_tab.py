"""SFTP push/pull with browsable file *and* folder pickers."""

from __future__ import annotations

from PyQt5.QtWidgets import (QWidget, QGridLayout, QLabel, QLineEdit, QPushButton,
                             QCheckBox, QFileDialog, QHBoxLayout)


class FilesTab(QWidget):
    title = "Files (SFTP)"

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker = worker
        g = QGridLayout(self)

        self.local = QLineEdit(); self.local.setPlaceholderText("local file or folder")
        self.remote = QLineEdit(); self.remote.setPlaceholderText("remote path")
        self.recursive = QCheckBox("Recursive (folder)")
        self.recursive.toggled.connect(self._sync_recursive)

        browse_file = QPushButton("File…"); browse_file.setProperty("role", "ghost")
        browse_file.clicked.connect(self._browse_file)
        browse_dir = QPushButton("Folder…"); browse_dir.setProperty("role", "ghost")
        browse_dir.clicked.connect(self._browse_dir)
        bbox = QHBoxLayout(); bbox.addWidget(browse_file); bbox.addWidget(browse_dir)
        bwrap = QWidget(); bwrap.setLayout(bbox)

        push = QPushButton("Push  ▲ (upload)"); push.setProperty("role", "ok")
        pull = QPushButton("Pull  ▼ (download)")
        push.clicked.connect(self._push)
        pull.clicked.connect(self._pull)

        g.addWidget(QLabel("Local"), 0, 0); g.addWidget(self.local, 0, 1)
        g.addWidget(bwrap, 0, 2)
        g.addWidget(QLabel("Remote"), 1, 0); g.addWidget(self.remote, 1, 1, 1, 2)
        g.addWidget(self.recursive, 2, 0)
        g.addWidget(push, 2, 1); g.addWidget(pull, 2, 2)

    def _sync_recursive(self, on):
        # folder transfers must be recursive; just a hint, value used directly
        pass

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose local file")
        if path:
            self.local.setText(path)
            self.recursive.setChecked(False)

    def _browse_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Choose local folder")
        if path:
            self.local.setText(path)
            self.recursive.setChecked(True)

    def _push(self):
        self.worker.do_push(self.local.text().strip(), self.remote.text().strip(),
                            self.recursive.isChecked())

    def _pull(self):
        self.worker.do_pull(self.remote.text().strip(), self.local.text().strip(),
                            self.recursive.isChecked())
