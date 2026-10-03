"""Shared fixtures. Everything here runs before any ``turbossh`` import, so the
suite can never read or write the developer's real ``~/.turbossh`` and never
needs a display, a device, or the network."""

from __future__ import annotations

import logging
import os
import sys
import tempfile

_TEST_HOME = tempfile.mkdtemp(prefix="turbossh-tests-")
os.environ["TURBOSSH_HOME"] = os.path.join(_TEST_HOME, ".turbossh")
os.environ["HOME"] = _TEST_HOME
os.environ["USERPROFILE"] = _TEST_HOME
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# test the working tree, not whatever turbossh is installed in site-packages
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

logging.getLogger("paramiko").setLevel(logging.CRITICAL)


@pytest.fixture(autouse=True, scope="session")
def _memory_keyring():
    """Route keyring to an in-memory backend for the whole run, so no test can
    read or write the developer's real credential vault."""
    try:
        import keyring
        from keyring.backend import KeyringBackend
    except Exception:
        yield None
        return

    class Memory(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self.store = {}

        def get_password(self, service, username):
            return self.store.get((service, username))

        def set_password(self, service, username, password):
            self.store[(service, username)] = password

        def delete_password(self, service, username):
            if (service, username) not in self.store:
                from keyring.errors import PasswordDeleteError
                raise PasswordDeleteError("not found")
            del self.store[(service, username)]

    previous = keyring.get_keyring()
    backend = Memory()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture()
def ssh_server(tmp_path):
    """A loopback SSH server (see sshserver.py); ``.server`` scripts it."""
    from sshserver import LoopbackSSH
    root = tmp_path / "remote"
    root.mkdir()
    srv = LoopbackSSH(root)
    srv.root = root
    yield srv
    srv.close()


@pytest.fixture()
def make_config(ssh_server):
    """Build an SSHConfig for the loopback server (fast-failing by default)."""
    from turbossh import SSHConfig

    def _make(**overrides):
        opts = dict(host="127.0.0.1", port=ssh_server.port, username="u",
                    password="good", host_key_policy="ignore", max_retries=1,
                    diagnose_on_failure=False, allow_agent=False,
                    look_for_keys=False)
        opts.update(overrides)
        return SSHConfig(**opts)
    return _make


@pytest.fixture()
def ssh(make_config):
    """A connected, quiet SSHHandler on the loopback server."""
    from turbossh import SSHHandler
    handler = SSHHandler(make_config(), quiet=True)
    handler.connect()
    yield handler
    handler.disconnect()


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole run (GUI tests skip without PyQt5)."""
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
