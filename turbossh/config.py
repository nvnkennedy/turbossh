"""Connection/behaviour configuration objects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Sequence, Union

from .credentials import Secret, coerce_secret


@dataclass
class SSHConfig:
    """
    Everything needed to open and drive an SSH connection.

    Domain accounts (Windows / RDP hosts running OpenSSH Server)
    -----------------------------------------------------------
    Set ``domain`` and ``username`` separately and let the handler build the
    ``DOMAIN\\user`` login string for you::

        SSHConfig(host="10.0.0.5", domain="CORP", username="myuser",
                  password=<Secret or str>)

    Setting them separately avoids the classic trap where a literal Python
    string ``"CORP\\myuser"`` turns an escape sequence into a control character
    (e.g. ``\\n`` -> newline). The password is
    stored as a :class:`Secret`, so it never appears in logs or reprs.
    """

    host: str
    port: int = 22
    username: Optional[str] = None
    domain: Optional[str] = None          # e.g. "CORP" -> login "CORP\\username"

    # --- authentication (any combination; tried in a smart order) ---
    password: Optional[Union[str, Secret]] = None
    key_filename: Optional[Union[str, Sequence[str]]] = None
    key_passphrase: Optional[Union[str, Secret]] = None
    allow_agent: bool = True
    look_for_keys: bool = True
    allow_empty_password: bool = True     # accounts with a blank password
    passwordless: bool = False            # force key/agent only

    # --- connection behaviour ---
    connect_timeout: float = 15.0
    auth_timeout: float = 20.0
    banner_timeout: float = 20.0
    command_timeout: Optional[float] = None
    keepalive_interval: int = 30

    # --- performance ---
    compress: bool = False                # enable on slow/high-latency links
    fast_auth: bool = True                # skip key probing when a password is
                                          # supplied -> much faster, avoids
                                          # "Too many authentication failures"

    # --- algorithm control (legacy / embedded / automotive ECUs) ---
    enable_legacy_algorithms: bool = False  # allow old KEX/ciphers/host-keys that
                                            # modern OpenSSH/Paramiko drop, so old
                                            # embedded SSH servers can connect
    disabled_algorithms: Optional[dict] = None  # paramiko passthrough, e.g.
                                                # {"pubkeys": ["rsa-sha2-512"]}

    # --- resilience ---
    max_retries: int = 3
    retry_backoff: float = 2.0
    auto_reconnect: bool = True
    diagnose_on_failure: bool = True      # on connect failure, probe SSH/RDP ports
                                          # and append an actionable hint to the error

    # --- WinRM bootstrap (auto-enable sshd on Windows hosts with no SSH yet) ---
    auto_bootstrap_via_winrm: bool = False  # if connect fails & SSH port closed but
                                            # WinRM reachable, enable sshd then retry
    winrm_port: int = 5985
    winrm_use_ssl: bool = False
    winrm_transport: str = "ntlm"           # ntlm works with domain creds, no Kerberos

    # --- host key policy ---
    # "auto"   -> add unknown host keys, but reject a CHANGED key
    # "ignore" -> accept any key incl. changed ones (lab / reimaged / DHCP hosts)
    # "reject" -> strict; only keys already in known_hosts
    # "warn"   -> warn on unknown keys
    host_key_policy: str = "auto"
    known_hosts: Optional[str] = None

    # --- jump host / bastion (ProxyJump) ---
    jump_host: Optional["SSHConfig"] = None

    # --- remote OS hint: "auto" | "linux" | "windows" ---
    remote_os: str = "auto"

    def __post_init__(self):
        # Normalize secrets so they are never plain strings internally.
        self.password = coerce_secret(self.password)
        self.key_passphrase = coerce_secret(self.key_passphrase)

    @property
    def auth_username(self) -> Optional[str]:
        """Login string, combining domain if present (``DOMAIN\\user``)."""
        if self.domain and self.username:
            return f"{self.domain}\\{self.username}"
        return self.username

    def normalized_key_files(self) -> list[str]:
        if not self.key_filename:
            return []
        keys = self.key_filename
        if isinstance(keys, (list, tuple)):
            return [os.path.expanduser(k) for k in keys]
        return [os.path.expanduser(keys)]

    def __repr__(self) -> str:  # never leak the password
        return (
            f"SSHConfig(host={self.host!r}, port={self.port}, "
            f"user={self.auth_username!r}, password={self.password!r}, "
            f"jump_host={'yes' if self.jump_host else 'no'})"
        )


@dataclass
class FTPConfig:
    """Configuration for the (non-SSH) FTP/FTPS handler."""

    host: str
    port: int = 21
    username: str = "anonymous"
    password: Optional[Union[str, Secret]] = ""
    use_tls: bool = False                 # FTPS (explicit TLS) when True
    passive: bool = True
    timeout: float = 30.0
    encoding: str = "utf-8"

    def __post_init__(self):
        self.password = coerce_secret(self.password)
