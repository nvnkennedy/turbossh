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

__version__ = "1.2.24"

from .config import SSHConfig, FTPConfig
from .core import SSHHandler, ShellSession
from .ftp import FTPHandler
from .pool import SSHPool
from .tunnel import LocalForward, RemoteForward, generate_keypair
from .winrm_bootstrap import (enable_openssh_via_winrm,
                              enable_openssh_via_winrm_offline,
                              winrm_available, WinRMError)
from .serial_handler import (SerialHandler, list_serial_ports, serial_available,
                             SerialError)
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
