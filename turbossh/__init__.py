"""
TurboSSH
========

An SSH / Serial / SFTP / SCP / FTP / RDP terminal & automation toolkit built on
Paramiko — for automotive & embedded work, test automation, and remote ops.
Usable as a Python API, a CLI, or a full-featured PyQt5 GUI.

Quick start
-----------
    from turbossh import SSHHandler, SSHConfig

    with SSHHandler(SSHConfig(host="10.0.0.5", username="root",
                              password="…")) as ssh:
        print(ssh.run("uname -a").text)
        ssh.push("local.txt", "/tmp/remote.txt")
        ssh.pull("/var/log/syslog", "syslog.txt")

Passwords are wrapped in a Secret and never appear in logs or reprs. Raw
Paramiko access is always available via ``ssh.client`` and ``ssh.transport``,
so anything Paramiko can do is possible.
"""

from __future__ import annotations

__version__ = "1.7.0"

# Light modules stay eager — they're cheap to import (no paramiko/pywinrm/pyserial).
from .config import SSHConfig, FTPConfig
from .credentials import Secret, CredentialStore, prompt_password, mask
from .results import CommandResult, TransferResult, ShellResult, OperationResult
from .exceptions import (
    SSHError,
    SSHConnectionError,
    SSHAuthenticationError,
    SSHTimeoutError,
    SSHCommandError,
    SSHTransferError,
    SSHNotConnectedError,
    FTPError,
    CredentialError,
)

# Heavy modules (paramiko / pywinrm / pyserial) are imported LAZILY the first time
# one of their names is accessed (PEP 562). That keeps `import turbossh` — and the
# GUI window — fast: paramiko (~0.5s to import) only loads when you actually open a
# connection, not at startup. Access is transparent: `from turbossh import
# SSHHandler` and `turbossh.SSHHandler` both still work.
import importlib as _importlib

_LAZY_SUBMODULES = {
    "SSHHandler": "core", "ShellSession": "core",
    "FTPHandler": "ftp",
    "SSHPool": "pool",
    "LocalForward": "tunnel", "RemoteForward": "tunnel",
    "generate_keypair": "tunnel",
    "enable_openssh_via_winrm": "winrm_bootstrap",
    "enable_openssh_via_winrm_offline": "winrm_bootstrap",
    "winrm_available": "winrm_bootstrap", "WinRMError": "winrm_bootstrap",
    "SerialHandler": "serial_handler", "list_serial_ports": "serial_handler",
    "serial_available": "serial_handler", "SerialError": "serial_handler",
}


def __getattr__(name):        # PEP 562 module-level lazy attribute access
    modname = _LAZY_SUBMODULES.get(name)
    if modname is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = _importlib.import_module(f".{modname}", __name__)
    value = getattr(module, name)
    globals()[name] = value   # cache so subsequent access is a direct global
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_SUBMODULES))

__all__ = [
    "SSHHandler", "ShellSession", "SSHConfig", "FTPConfig", "FTPHandler",
    "SSHPool", "LocalForward", "RemoteForward", "generate_keypair",
    "enable_openssh_via_winrm", "enable_openssh_via_winrm_offline",
    "winrm_available", "WinRMError",
    "SerialHandler", "list_serial_ports", "serial_available", "SerialError",
    "Secret", "CredentialStore", "prompt_password", "mask",
    "CommandResult", "TransferResult", "ShellResult", "OperationResult",
    "SSHError", "SSHConnectionError", "SSHAuthenticationError",
    "SSHTimeoutError", "SSHCommandError", "SSHTransferError",
    "SSHNotConnectedError", "FTPError", "CredentialError", "__version__",
]
