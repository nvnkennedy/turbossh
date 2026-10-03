"""Exception hierarchy for turbossh. Catch SSHError for everything."""

from __future__ import annotations


class SSHError(Exception):
    """Base class for all errors raised by this package."""


class SSHConnectionError(SSHError):
    """Network / TCP / DNS level failure establishing a connection."""


class SSHAuthenticationError(SSHError):
    """Every configured authentication strategy was rejected."""


class SSHTimeoutError(SSHError):
    """An operation exceeded its allotted time.

    For a command timeout, ``result`` holds a CommandResult (exit_code -1) with
    whatever the command had printed before it was cut off.
    """

    def __init__(self, message: str = "", result=None):
        super().__init__(message)
        self.result = result


class SSHCommandError(SSHError):
    """A remote command exited non-zero while check=True."""

    def __init__(self, command: str, result):
        self.command = command
        self.result = result
        stderr = getattr(result, "stderr", "") or ""
        exit_code = getattr(result, "exit_code", "?")
        super().__init__(
            f"Command failed (exit={exit_code}): {command!r}\n"
            f"stderr: {stderr.strip()[:500]}"
        )


class SSHTransferError(SSHError):
    """An SFTP/SCP upload or download failed."""


class SSHNotConnectedError(SSHError):
    """An operation was attempted before connecting (auto_reconnect off)."""


class FTPError(SSHError):
    """An FTP/FTPS operation failed."""


class CredentialError(SSHError):
    """A credential could not be stored or retrieved."""
