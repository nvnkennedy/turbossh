"""
The main SSH handler: command execution, interactive shells, sudo, full SFTP
file operations, optional SCP, jump-host chaining, and remote-OS awareness.

Designed for three consumers from one object:
  * test-automation framework  -> raise-on-error (default)
  * standalone CLI script       -> see turbossh.cli
  * PyQt5 GUI                    -> safe=True + log_callback (see turbossh.gui)
"""

from __future__ import annotations

import os
import shlex
import socket
import stat
import time
import logging
import posixpath
import threading
from typing import Callable, Optional, Sequence, Union

import paramiko

from . import _logging
from .config import SSHConfig
from .credentials import Secret, mask
from .results import CommandResult, TransferResult, ShellResult, OperationResult
from .exceptions import (
    SSHConnectionError,
    SSHAuthenticationError,
    SSHTimeoutError,
    SSHCommandError,
    SSHTransferError,
    SSHNotConnectedError,
)

# Every long-lived serial PowerShell we start begins with this comment line, so
# its -EncodedCommand argument starts with a fixed base64 prefix. That prefix is
# how a "force" open finds OUR stale readers on the remote machine without
# touching any other PowerShell there. 18 characters -> 36 UTF-16 bytes -> exactly
# 48 base64 characters, so it encodes to a stable prefix with no padding.
_SERIAL_PS_TAG = "#turbossh-serial\n\n"


def _serial_ps_tag_b64() -> str:
    import base64
    return base64.b64encode(_SERIAL_PS_TAG.encode("utf-16-le")).decode("ascii")


try:  # optional SCP support
    from scp import SCPClient
    _HAS_SCP = True
except Exception:  # pragma: no cover
    SCPClient = None
    _HAS_SCP = False


def parse_dshow_devices(text: str) -> list:
    """Parse camera names out of ffmpeg's ``-list_devices`` output. Handles both
    formats: the newer ``"Cam" (video)`` lines and the older section-header style
    (``DirectShow video devices`` … ``DirectShow audio devices``). Used for both
    local and remote enumeration so they behave identically."""
    import re
    cams, in_video = [], False
    for line in (text or "").splitlines():
        low = line.lower()
        if "directshow video devices" in low:
            in_video = True
            continue
        if "directshow audio devices" in low:
            in_video = False
            continue
        if "alternative name" in low:
            continue
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        name = m.group(1)
        if name.startswith("@device"):       # alt-name device path, skip
            continue
        if "(video)" in low or in_video:
            cams.append(name)
    seen, out = set(), []
    for c in cams:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def parse_dshow_modes(text: str) -> list:
    """Parse a DirectShow camera's ``-list_options`` output into capture modes.
    Each line looks like ``vcodec=mjpeg  min s=1280x720 fps=30 max s=1280x720
    fps=30`` (or ``pixel_format=yuyv422 …``). Returns dicts
    ``{codec, w, h, fps}`` using the largest size / highest fps on the line. Used
    to pick a sharp MJPEG capture mode instead of the low-res raw default."""
    import re
    modes = []
    for line in (text or "").splitlines():
        low = line.lower()
        cm = re.search(r'(?:vcodec|pixel_format)=(\S+)', low)
        if not cm:
            continue
        sizes = [(int(a), int(b)) for a, b in re.findall(r's=(\d+)x(\d+)', low)]
        fpss = [float(f) for f in re.findall(r'fps=([\d.]+)', low)]
        if not sizes:
            continue
        w, h = max(sizes, key=lambda s: s[0] * s[1])
        modes.append({"codec": cm.group(1), "w": w, "h": h,
                      "fps": int(max(fpss)) if fpss else 30})
    return modes


# --------------------------------------------------------------------------- #
# Interactive shell session
# --------------------------------------------------------------------------- #
class ShellSession:
    """
    A persistent interactive shell (one PTY channel) for devices/flows that
    need state between commands, prompts, or send/expect interaction.
    """

    def __init__(self, channel: paramiko.Channel, encoding: str = "utf-8"):
        self._chan = channel
        self.encoding = encoding

    @property
    def channel(self) -> paramiko.Channel:
        """The raw paramiko channel (for an interactive terminal widget)."""
        return self._chan

    def resize(self, width: int, height: int) -> None:
        try:
            self._chan.resize_pty(width=width, height=height)
        except Exception:
            pass

    def send(self, data: str) -> None:
        if not data.endswith("\n"):
            data += "\n"
        # sendall: send() may accept only part of a large write
        self._chan.sendall(data)

    def read_until(self, marker: str, timeout: float = 30.0) -> ShellResult:
        """Read output until *marker* appears or *timeout* elapses."""
        start = time.time()
        # Accumulate chunks in a list and join once at the end. Searching a small
        # sliding window (rather than re-scanning the whole growing buffer each
        # recv) keeps a long send/expect exchange linear instead of O(n²).
        parts = []
        window = ""
        overlap = max(0, len(marker) - 1)
        self._chan.settimeout(0.5)
        while time.time() - start < timeout:
            try:
                chunk = self._chan.recv(65536)
                if not chunk:
                    break
                text = chunk.decode(self.encoding, errors="replace")
                parts.append(text)
                window += text
                if marker and marker in window:
                    return ShellResult("".join(parts), marker, False,
                                       time.time() - start)
                # keep only enough trailing chars to catch a marker spanning the
                # boundary of the next chunk
                if overlap and len(window) > overlap:
                    window = window[-overlap:]
                elif not overlap:
                    window = ""
            except socket.timeout:
                continue
        return ShellResult("".join(parts), None, True, time.time() - start)

    def read_available(self) -> str:
        out = ""
        self._chan.settimeout(0.2)
        while self._chan.recv_ready():
            try:
                out += self._chan.recv(65536).decode(self.encoding, errors="replace")
            except socket.timeout:
                break
        return out

    def close(self) -> None:
        try:
            self._chan.close()
        except Exception:
            pass

    def __enter__(self) -> "ShellSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# The handler
# --------------------------------------------------------------------------- #
class SSHHandler:
    """High-level SSH + SFTP + SCP handler. See package README for recipes."""

    _POLICIES = {
        "auto": paramiko.AutoAddPolicy,
        "reject": paramiko.RejectPolicy,
        "warn": paramiko.WarningPolicy,
    }

    def __init__(
        self,
        config: SSHConfig,
        *,
        log_callback: Optional[Callable[[str], None]] = None,
        logger: Optional[logging.Logger] = None,
        safe: bool = False,
        quiet: bool = False,
    ):
        """
        :param quiet: when True, suppress the INFO narration (connect/command
                      lines) and emit only warnings and errors. Command results
                      are returned in CommandResult.stdout regardless.
        """
        self.config = config
        self._safe_default = safe
        self._log_callback = log_callback
        self._quiet = quiet
        self.log = logger or self._build_logger()

        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._jump: Optional["SSHHandler"] = None
        self._bootstrap_attempted = False
        # Serialises connect / reconnect / disconnect, so two threads that find
        # the link down don't both reconnect (and leak one of the clients).
        self._conn_lock = threading.RLock()
        self._remote_os: Optional[str] = (
            None if config.remote_os == "auto" else config.remote_os
        )

    # ------------------------------------------------------------------ #
    # Logging (secret-aware)
    # ------------------------------------------------------------------ #
    def _build_logger(self) -> logging.Logger:
        # One console handler lives on the package logger (and only when the
        # application hasn't set up logging itself). ``quiet`` is applied per
        # instance in _emit, never by changing a logger other handlers share.
        _logging.ensure_console_handler()
        return _logging.get_logger(self.config.host)

    def _secrets(self) -> list:
        s = [self.config.password, self.config.key_passphrase]
        if self.config.jump_host:
            s += [self.config.jump_host.password, self.config.jump_host.key_passphrase]
        return [x for x in s if x]

    def _emit(self, level: int, msg: str) -> None:
        safe_msg = mask(msg, *self._secrets())
        if not (getattr(self, "_quiet", False) and level < logging.WARNING):
            self.log.log(level, safe_msg)
        if self._log_callback:
            try:
                self._log_callback(f"[{logging.getLevelName(level)}] {safe_msg}")
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Safe-mode wrapper
    # ------------------------------------------------------------------ #
    def _guard(self, action: str, fn, *args, safe: Optional[bool] = None, **kwargs):
        use_safe = self._safe_default if safe is None else safe
        if not use_safe:
            return fn(*args, **kwargs)
        try:
            return OperationResult(True, action, value=fn(*args, **kwargs))
        except Exception as exc:
            self._emit(logging.ERROR, f"{action} failed: {exc}")
            return OperationResult(False, action, error=exc)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        t = self._client.get_transport()
        return bool(t and t.is_active())

    @property
    def client(self) -> paramiko.SSHClient:
        """The underlying paramiko SSHClient (for anything not wrapped here)."""
        self._ensure_connected()
        return self._client

    @property
    def transport(self) -> paramiko.Transport:
        return self.client.get_transport()

    def _ensure_connected(self) -> None:
        if self.is_connected:
            return
        with self._conn_lock:
            if self.is_connected:          # another thread reconnected meanwhile
                return
            if self.config.auto_reconnect:
                self._emit(logging.WARNING, "Session not active; reconnecting…")
                self._connect_with_retries()
            else:
                raise SSHNotConnectedError("Not connected (auto_reconnect disabled).")

    # ------------------------------------------------------------------ #
    # Connect / disconnect
    # ------------------------------------------------------------------ #
    def connect(self, *, safe: Optional[bool] = None):
        return self._guard("connect", self._connect_with_retries, safe=safe)

    # Legacy KEX / host-key / cipher / MAC names old embedded and automotive SSH
    # servers still need, each with the Transport table that says whether this
    # Paramiko build can actually speak it (newer releases dropped some — offering
    # a name the Transport has no implementation for fails mid-handshake).
    _LEGACY_ALGORITHMS = {
        "_preferred_kex": ("_kex_info", ("diffie-hellman-group14-sha1",
                                         "diffie-hellman-group1-sha1",
                                         "diffie-hellman-group-exchange-sha1")),
        "_preferred_keys": ("_key_info", ("ssh-rsa", "ssh-dss")),
        "_preferred_ciphers": ("_cipher_info", ("aes128-cbc", "aes256-cbc",
                                                "3des-cbc", "blowfish-cbc")),
        "_preferred_macs": ("_mac_info", ("hmac-sha1", "hmac-md5")),
    }

    @classmethod
    def _legacy_preferences(cls) -> dict:
        """The Transport preference tuples extended with the legacy names this
        Paramiko supports."""
        from paramiko.transport import Transport
        out = {}
        for attr, (info_attr, names) in cls._LEGACY_ALGORITHMS.items():
            current = tuple(getattr(Transport, attr, ()))
            known = getattr(Transport, info_attr, None)
            extra = tuple(n for n in names if n not in current
                          and (known is None or n in known))
            out[attr] = current + extra
        return out

    @classmethod
    def _legacy_transport_class(cls):
        """A Transport subclass that also offers the legacy algorithms — used for
        ONE connection, so enabling them for an old ECU doesn't weaken every
        other connection in the process."""
        from paramiko.transport import Transport
        return type("LegacyTransport", (Transport,), cls._legacy_preferences())

    @staticmethod
    def _supports_transport_factory() -> bool:
        import inspect
        try:
            return "transport_factory" in inspect.signature(
                paramiko.SSHClient.connect).parameters
        except (TypeError, ValueError):
            return False

    @classmethod
    def _apply_legacy_algorithms(cls):
        """Fallback for Paramiko < 3.2 (no per-connection ``transport_factory``):
        extend the Transport class itself. Process-wide and idempotent."""
        try:
            from paramiko.transport import Transport
            for attr, merged in cls._legacy_preferences().items():
                setattr(Transport, attr, merged)
        except Exception:
            pass

    def _make_client(self) -> paramiko.SSHClient:
        if (self.config.enable_legacy_algorithms
                and not self._supports_transport_factory()):
            self._apply_legacy_algorithms()
        client = paramiko.SSHClient()
        if self.config.host_key_policy == "ignore":
            # Lab mode: don't load any known_hosts and accept whatever key the
            # server presents - even a CHANGED one. Use for reimaged / DHCP
            # devices whose host keys rotate (avoids BadHostKeyException).
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            return client
        if self.config.known_hosts:
            try:
                client.load_host_keys(os.path.expanduser(self.config.known_hosts))
            except FileNotFoundError:
                self._emit(logging.WARNING,
                           f"known_hosts not found: {self.config.known_hosts}")
        else:
            client.load_system_host_keys()
        policy = self._POLICIES.get(self.config.host_key_policy, paramiko.AutoAddPolicy)
        client.set_missing_host_key_policy(policy())
        return client

    def _open_jump_channel(self):
        """Return a NEW direct-tcpip channel to the target through the jump host.

        The jump connection is made once and reused: every auth strategy and
        every retry needs its own channel (a failed attempt closes the one it
        used), but reconnecting the jump host each time leaked a connection per
        attempt."""
        if self._jump is None or not self._jump.is_connected:
            self._close_jump()
            self._jump = SSHHandler(
                self.config.jump_host,
                log_callback=self._log_callback,
                logger=self.log,
                quiet=self._quiet,
            )
            self._jump._connect_with_retries()
            self._emit(logging.INFO, f"Tunnelling through jump host "
                                     f"{self.config.jump_host.host}.")
        return self._jump.transport.open_channel(
            "direct-tcpip",
            dest_addr=(self.config.host, self.config.port),
            src_addr=("127.0.0.1", 0),
            timeout=self.config.connect_timeout,
        )

    def _close_jump(self) -> None:
        if self._jump is not None:
            try:
                self._jump.disconnect()
            except Exception:
                pass
            self._jump = None

    def _build_connect_kwargs(self, *, empty_password: bool, sock) -> dict:
        cfg = self.config
        pw = cfg.password.reveal() if isinstance(cfg.password, Secret) else cfg.password
        passphrase = (cfg.key_passphrase.reveal()
                      if isinstance(cfg.key_passphrase, Secret) else cfg.key_passphrase)
        has_password = pw is not None and pw != ""

        kwargs: dict = {
            "hostname": cfg.host,
            "port": cfg.port,
            "username": cfg.auth_username,
            "timeout": cfg.connect_timeout,
            "auth_timeout": cfg.auth_timeout,
            "banner_timeout": cfg.banner_timeout,
            "compress": cfg.compress,
            "allow_agent": cfg.allow_agent,
            "look_for_keys": cfg.look_for_keys,
        }
        if sock is not None:
            kwargs["sock"] = sock
        if cfg.disabled_algorithms:
            kwargs["disabled_algorithms"] = cfg.disabled_algorithms
        if cfg.enable_legacy_algorithms and self._supports_transport_factory():
            kwargs["transport_factory"] = self._legacy_transport_class()

        keys = cfg.normalized_key_files()
        if keys:
            kwargs["key_filename"] = keys
        if passphrase:
            kwargs["passphrase"] = passphrase

        if cfg.passwordless:
            kwargs["allow_agent"] = True
            kwargs["look_for_keys"] = True
        elif empty_password or pw == "":
            # Explicit empty-password authentication. An empty string means
            # "log in with a blank password" (PermitEmptyPasswords hosts) - send
            # it directly and don't waste the first attempt probing keys/agent,
            # which would otherwise fail with "No authentication methods
            # available" before the empty password is ever tried.
            kwargs["password"] = ""
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False
        elif has_password:
            kwargs["password"] = pw
            # Performance: with a password and no explicit key, skip the slow
            # agent/key probing that otherwise runs first (and can trip the
            # server's MaxAuthTries -> "Too many authentication failures").
            if cfg.fast_auth and not keys:
                kwargs["allow_agent"] = False
                kwargs["look_for_keys"] = False
        return kwargs

    def _attempt_strategies(self, open_sock=None) -> None:
        """Try each auth strategy on its own connection. ``open_sock`` (jump
        hosts) is called per attempt, because a rejected attempt closes the
        channel it ran over."""
        cfg = self.config
        last_auth: Optional[Exception] = None
        supplied = (cfg.password.reveal() if isinstance(cfg.password, Secret)
                    else cfg.password)
        strategies = [False]
        # The blank-password fallback is for accounts where NO password was given
        # (lab targets with an empty root password). After a real password was
        # rejected it would only add a second failed login per connect — which
        # halves the distance to an account lockout — and with password="" the
        # primary attempt already is the blank one.
        if cfg.allow_empty_password and not cfg.passwordless and supplied is None:
            strategies.append(True)

        for empty in strategies:
            sock = open_sock() if open_sock else None
            client = self._make_client()
            kwargs = self._build_connect_kwargs(empty_password=empty, sock=sock)
            label = "empty-password" if empty else "primary"
            try:
                self._emit(logging.DEBUG, f"Auth attempt ({label}) as "
                                          f"{cfg.auth_username}…")
                client.connect(**kwargs)
                self._client = client
                self._post_connect()
                self._emit(logging.INFO,
                           f"Connected to {cfg.auth_username}@{cfg.host}:{cfg.port} "
                           f"({label}).")
                return
            except paramiko.AuthenticationException as exc:
                last_auth = exc
                client.close()
                self._emit(logging.DEBUG, f"Auth strategy '{label}' rejected.")
            except paramiko.SSHException as exc:
                client.close()
                # "No authentication methods available" means paramiko had no
                # credential the server would accept (no password/key/agent).
                # That's an auth problem, not a transport one - try the next
                # strategy, and if none are left raise a clear, guided error.
                if "no authentication methods" in str(exc).lower():
                    last_auth = exc
                    self._emit(logging.DEBUG,
                               f"Auth strategy '{label}': {exc}")
                    continue
                raise exc
            except OSError as exc:
                client.close()
                raise exc
            except EOFError as exc:
                # Paramiko surfaces a peer that hangs up mid-handshake as a bare
                # EOFError; turn it into the connection error it is, so it is
                # retried and reported through our own exception types.
                client.close()
                raise paramiko.SSHException(
                    "the server closed the connection during the SSH handshake"
                ) from exc
        if last_auth and "no authentication methods" in str(last_auth).lower():
            raise SSHAuthenticationError(
                f"No usable credentials for {cfg.auth_username}@{cfg.host}. "
                f"Provide one of: password=..., key_filename=..., "
                f"passwordless=True (use your SSH key/agent), or password='' for a "
                f"blank-password account (the server must allow PermitEmptyPasswords)."
            )
        raise SSHAuthenticationError(
            f"All authentication strategies failed for "
            f"{cfg.auth_username}@{cfg.host}: {last_auth}"
        )

    def _connect_with_retries(self) -> "SSHHandler":
        with self._conn_lock:
            try:
                return self._connect_locked()
            except BaseException:
                self._close_jump()      # never leave a jump connection behind
                raise

    def _connect_locked(self) -> "SSHHandler":
        cfg = self.config
        attempt, delay, last_exc = 0, cfg.retry_backoff, None
        opener = self._open_jump_channel if cfg.jump_host else None
        while attempt < max(1, cfg.max_retries):
            attempt += 1
            try:
                self._attempt_strategies(opener)
                return self
            except SSHAuthenticationError:
                raise  # retrying won't help
            except socket.timeout as exc:
                last_exc = exc
                self._emit(logging.WARNING,
                           f"Connect timeout (attempt {attempt}/{cfg.max_retries}).")
            except (OSError, paramiko.SSHException) as exc:
                last_exc = exc
                self._emit(logging.WARNING,
                           f"Connect error (attempt {attempt}/{cfg.max_retries}): {exc}")
            if attempt < cfg.max_retries:
                time.sleep(delay)
                delay *= cfg.retry_backoff

        # Optional self-heal: if SSH is down but WinRM is up, enable sshd and retry.
        # (Not through a jump host: these probes run from THIS machine.)
        if (cfg.auto_bootstrap_via_winrm and not self._bootstrap_attempted
                and not cfg.jump_host
                and not self._port_open(cfg.host, cfg.port, 3.0)
                and self._port_open(cfg.host, cfg.winrm_port, 3.0)):
            self._bootstrap_attempted = True
            self._emit(logging.WARNING,
                       f"SSH unreachable but WinRM ({cfg.winrm_port}) is open; "
                       f"attempting to enable sshd via WinRM…")
            from .winrm_bootstrap import enable_openssh_via_winrm
            enable_openssh_via_winrm(
                cfg.host, cfg.username, cfg.password, domain=cfg.domain,
                winrm_port=cfg.winrm_port, use_ssl=cfg.winrm_use_ssl,
                verify_ssl=cfg.winrm_verify_ssl,
                transport=cfg.winrm_transport, ssh_port=cfg.port,
                log=lambda m: self._emit(logging.INFO, m),
            )
            for _ in range(10):
                if self._port_open(cfg.host, cfg.port, 2.0):
                    break
                time.sleep(1.0)
            return self._connect_locked()  # one more pass now that sshd is up

        hint = self._connection_hint(last_exc) if cfg.diagnose_on_failure else ""
        if isinstance(last_exc, socket.timeout):
            raise SSHTimeoutError(
                f"Timed out connecting to {cfg.host}:{cfg.port}.{hint}") from last_exc
        raise SSHConnectionError(
            f"Could not connect to {cfg.host}:{cfg.port}: {last_exc}.{hint}") \
            from last_exc

    # ------------------------------------------------------------------ #
    # Connectivity diagnosis (turns "errno None" into something actionable)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 3.0) -> bool:
        """True if a TCP connection to host:port can be established."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _connection_hint(self, last_exc=None) -> str:
        """A short, actionable explanation appended to connect failures."""
        host, port = self.config.host, self.config.port
        if self.config.jump_host:
            # The port probes below run from THIS machine, which says nothing
            # about a target that is only reachable from the jump host.
            jump = self.config.jump_host.host
            return (f" The target is reached through jump host {jump}; check that "
                    f"{jump} itself can reach {host}:{port} and that an SSH server "
                    f"is listening there.")
        probe_to = min(self.config.connect_timeout, 3.0)
        if self._port_open(host, port, probe_to):
            # Port is open but the connection still failed. If it was a banner /
            # protocol error, the thing on that port isn't speaking SSH — that's
            # the single most common cause of "Error reading SSH protocol banner".
            msg = str(last_exc or "").lower()
            if "banner" in msg or "protocol" in msg or "eof" in msg:
                user = self.config.auth_username or "user"
                return (f" TurboSSH reached {host}:{port} but it never sent an SSH "
                        f"banner — so that port is open but is NOT an SSH server. "
                        f"Most likely OpenSSH Server is not installed or not running "
                        f"on {host} (Windows has no SSH server by default). On that "
                        f"machine: install + start it — TurboSSH can do it for you via "
                        f"the 'Set up SSH server' button or `turbossh-setup`; or "
                        f"manually: Add-WindowsCapability -Online -Name "
                        f"OpenSSH.Server~~~~0.0.1.0 ; Start-Service sshd ; "
                        f"Set-Service sshd -StartupType Automatic. Otherwise {port} is "
                        f"the wrong port or a firewall is intercepting it. Confirm "
                        f"with:  ssh -p {port} {user}@{host}")
            return ""  # SSH port is actually open; failure was something else
        rdp_open = self._port_open(host, 3389, 2.0)
        if rdp_open:
            return (f" Port {port} is closed but RDP (3389) is open - '{host}' is "
                    f"up but has no SSH server listening. Enable OpenSSH Server on "
                    f"it (PowerShell: Add-WindowsCapability -Online -Name "
                    f"OpenSSH.Server~~~~0.0.1.0; Start-Service sshd), set a "
                    f"different port= if sshd runs elsewhere, or run your script "
                    f"directly on that machine.")
        return (f" Neither SSH ({port}) nor RDP (3389) is reachable on '{host}' - "
                f"check the IP address, firewall/VPN, and that you're on a network "
                f"that can route to it.")

    def diagnose(self) -> dict:
        """
        Proactively probe reachability without attempting a full SSH login.
        Returns a dict you can act on (and logs a readable summary). Useful as a
        pre-flight check before connect().
        """
        host, port = self.config.host, self.config.port
        ssh_open = self._port_open(host, port, min(self.config.connect_timeout, 3.0))
        rdp_open = self._port_open(host, 3389, 2.0)
        if ssh_open:
            verdict = f"SSH port {port} is reachable — connect() should proceed to auth."
            reachable = True
        elif rdp_open:
            verdict = (f"SSH port {port} CLOSED but RDP (3389) OPEN - host is up with "
                       f"no SSH server. Enable OpenSSH Server on it, or run on it.")
            reachable = False
        else:
            verdict = (f"Neither {port} nor 3389 reachable - host unreachable "
                       f"(IP/firewall/VPN/route).")
            reachable = False
        result = {"host": host, "port": port, "ssh_open": ssh_open,
                  "rdp_open": rdp_open, "reachable": reachable, "verdict": verdict}
        self._emit(logging.INFO, f"diagnose: {verdict}")
        return result

    # ------------------------------------------------------------------ #
    # Port forwarding (tunnels) and key management — full Paramiko parity
    # ------------------------------------------------------------------ #
    def forward_local(self, remote_host: str, remote_port: int, *,
                      local_port: int = 0, local_host: str = "127.0.0.1",
                      safe: Optional[bool] = None):
        """`ssh -L`: expose remote_host:remote_port on a local port (through this
        connection / jump). Returns a LocalForward handle (.local_port, .close())."""
        from .tunnel import LocalForward

        def _do():
            self._ensure_connected()
            t = LocalForward(self.transport, remote_host, remote_port,
                             local_port=local_port, local_host=local_host)
            self._emit(logging.INFO, f"Local forward {t.local_host}:{t.local_port}"
                                     f" -> {remote_host}:{remote_port}")
            return t
        return self._guard("forward_local", _do, safe=safe)

    def forward_remote(self, remote_port: int, local_host: str, local_port: int,
                       *, safe: Optional[bool] = None):
        """`ssh -R`: expose local_host:local_port on a remote port. Returns a
        RemoteForward handle."""
        from .tunnel import RemoteForward

        def _do():
            self._ensure_connected()
            t = RemoteForward(self.transport, remote_port, local_host, local_port)
            self._emit(logging.INFO, f"Remote forward remote:{t.remote_port} -> "
                                     f"{local_host}:{local_port}")
            return t
        return self._guard("forward_remote", _do, safe=safe)

    def copy_id(self, public_key: Optional[str] = None,
                public_key_file: Optional[str] = None, *,
                safe: Optional[bool] = None):
        """`ssh-copy-id`: append a public key to the remote ~/.ssh/authorized_keys
        for passwordless login. Pass a key string or a .pub file path."""
        def _do():
            self._ensure_connected()
            pub = public_key
            if public_key_file:
                with open(os.path.expanduser(public_key_file), encoding="utf-8") as fh:
                    pub = fh.read().strip()
            if not pub:
                raise SSHTransferError("Provide public_key or public_key_file.")
            pub = pub.replace("'", "'\\''")
            cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys"
                   f" && (grep -qxF '{pub}' ~/.ssh/authorized_keys || "
                   f"echo '{pub}' >> ~/.ssh/authorized_keys); "
                   "chmod 600 ~/.ssh/authorized_keys")
            res = self._run(cmd, timeout=20, check=False, get_pty=False,
                            environment=None, encoding="utf-8")
            return res.ok
        return self._guard("copy_id", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # WinRM bootstrap — enable sshd on a Windows host that has no SSH yet
    # ------------------------------------------------------------------ #
    def bootstrap_sshd_via_winrm(self, *, set_powershell_default: bool = False,
                                 safe: Optional[bool] = None):
        """
        Use WinRM (which must be reachable) to install/start OpenSSH Server on the
        target and open the firewall, so subsequent connect() works. The account
        must be a local admin on the target. Returns the bootstrap result dict.
        """
        from .winrm_bootstrap import enable_openssh_via_winrm

        cfg = self.config

        def _do():
            self._emit(logging.INFO,
                       f"Bootstrapping sshd on {cfg.host} via WinRM "
                       f"(port {cfg.winrm_port})…")
            res = enable_openssh_via_winrm(
                cfg.host, cfg.username, cfg.password, domain=cfg.domain,
                winrm_port=cfg.winrm_port, use_ssl=cfg.winrm_use_ssl,
                verify_ssl=cfg.winrm_verify_ssl,
                transport=cfg.winrm_transport, ssh_port=cfg.port,
                set_powershell_default=set_powershell_default,
                log=lambda m: self._emit(logging.INFO, m),
            )
            # wait briefly for the listener to come up
            for _ in range(10):
                if self._port_open(cfg.host, cfg.port, 2.0):
                    break
                time.sleep(1.0)
            return res

        return self._guard("bootstrap_sshd_via_winrm", _do, safe=safe)

    def _post_connect(self) -> None:
        t = self._client.get_transport()
        if t and self.config.keepalive_interval > 0:
            t.set_keepalive(self.config.keepalive_interval)
        self._sftp = None

    def disconnect(self) -> None:
        with self._conn_lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._emit(logging.INFO, f"Disconnected from {self.config.host}.")
                self._client = None
            self._close_jump()

    close = disconnect

    # ------------------------------------------------------------------ #
    # Remote OS awareness
    # ------------------------------------------------------------------ #
    def detect_os(self) -> str:
        """Return 'windows' or 'linux' (cached). Runs one or two probe commands.

        The old probe was ``uname -s || ver``, which is a *parse error* when the
        remote default shell is PowerShell (``||`` isn't a valid operator there) —
        so a Windows host whose OpenSSH default shell is PowerShell was misdetected
        as Linux. We now probe ``uname -s`` alone (positively identifying the Unix
        family), and only if that doesn't look Unixy do we run ``cmd /c ver``,
        which reports Windows from cmd *and* PowerShell default shells alike.
        """
        if self._remote_os:
            return self._remote_os
        self._ensure_connected()
        probe = self._run("uname -s", timeout=10, check=False,
                          get_pty=False, environment=None, encoding="utf-8")
        out = (probe.stdout or "").strip().lower()
        unixy = ("linux", "darwin", "bsd", "qnx", "sunos", "aix", "hp-ux",
                 "cygwin", "msys", "mingw", "unix")
        if probe.ok and any(k in out for k in unixy):
            self._remote_os = "linux"
        else:
            # uname failed or was unrecognised — probe explicitly for Windows in a
            # way that works whether the default shell is cmd.exe or PowerShell.
            win = self._run("cmd /c ver", timeout=10, check=False, get_pty=False,
                            environment=None, encoding="utf-8")
            text = (win.stdout + win.stderr).lower()
            self._remote_os = "windows" if ("windows" in text or "microsoft" in text) \
                else "linux"
        self._emit(logging.DEBUG, f"Detected remote OS: {self._remote_os}")
        return self._remote_os

    @property
    def is_windows(self) -> bool:
        return self.detect_os() == "windows"

    @property
    def known_os(self) -> Optional[str]:
        """The remote OS if it is already known ('windows' / 'linux'), else None.
        Never touches the network — safe to call from a GUI thread."""
        return self._remote_os

    # ------------------------------------------------------------------ #
    # Command execution
    # ------------------------------------------------------------------ #
    def run(self, command: str, *, timeout: Optional[float] = None,
            check: bool = False, get_pty: bool = False,
            environment: Optional[dict] = None, encoding: str = "utf-8",
            safe: Optional[bool] = None):
        """Execute a command. Returns CommandResult (or OperationResult if safe)."""
        return self._guard("run", self._run, command, timeout=timeout, check=check,
                           get_pty=get_pty, environment=environment,
                           encoding=encoding, safe=safe)

    def _run(self, command, *, timeout, check, get_pty, environment, encoding):
        eff_timeout = timeout if timeout is not None else self.config.command_timeout
        result = self._exec(command, timeout=eff_timeout, get_pty=get_pty,
                            environment=environment, encoding=encoding)
        lvl = logging.INFO if result.ok else logging.WARNING
        self._emit(lvl, f"exit={result.exit_code} ({result.duration:.2f}s)")
        if check and not result.ok:
            raise SSHCommandError(command, result)
        return result

    # After the exit status arrives, output is drained until the remote closes
    # the stream. A child the command left in the background can hold that
    # stream open forever, so stop this long after the last byte.
    _EXIT_DRAIN_GRACE = 1.0
    # sudo prints its prompt before any command output; only look this far for it.
    _PROMPT_SCAN = 8192

    def _exec(self, command, *, timeout, get_pty, environment, encoding,
              prompt=None, label=None) -> CommandResult:
        """Run *command* on its own channel and return a CommandResult.

        stdout and stderr are drained WHILE waiting for the exit status, and the
        whole call is bounded by *timeout* (None = no limit). Waiting for the exit
        status first — ``recv_exit_status()`` then ``read()`` — never times out,
        and deadlocks as soon as the output exceeds the SSH channel window: the
        remote blocks writing while we block waiting for it to exit.

        On timeout the channel is closed and :class:`SSHTimeoutError` carries the
        partial output in ``.result``.

        ``prompt=(marker, reply)``: the first time *marker* appears in the output,
        *reply* is written to the command's stdin (see :meth:`sudo`).
        ``label`` is what gets logged / reported instead of the raw command.
        """
        shown = label or command
        transport = self.transport                # ensures we are connected
        self._emit(logging.INFO, f"$ {shown}")
        started_at, start = time.time(), time.monotonic()
        deadline = None if not timeout else start + timeout
        out, err = bytearray(), bytearray()

        def result(code: int, note: str = "") -> CommandResult:
            stderr_text = err.decode(encoding, errors="replace")
            if note:
                stderr_text = (stderr_text.rstrip() + "\n" + note).strip()
            return CommandResult(shown, code, out.decode(encoding, errors="replace"),
                                 stderr_text, time.monotonic() - start,
                                 host=self.config.host, started_at=started_at)

        chan = None
        try:
            open_timeout = min(t for t in (timeout, self.config.connect_timeout,
                                           3600.0) if t)
            chan = transport.open_session(timeout=open_timeout)
            if get_pty:
                chan.get_pty()
            if environment:
                chan.update_environment(environment)
            chan.exec_command(command)

            replied, rejected = False, False
            exit_seen = None          # when the exit status was first observed
            idle = 0.001
            while True:
                progressed = False
                while chan.recv_ready():
                    data = chan.recv(65536)
                    if not data:
                        break
                    out += data
                    progressed = True
                while chan.recv_stderr_ready():
                    data = chan.recv_stderr(65536)
                    if not data:
                        break
                    err += data
                    progressed = True

                if prompt and progressed:
                    marker, reply = prompt
                    seen = (out[:self._PROMPT_SCAN].count(marker)
                            + err[:self._PROMPT_SCAN].count(marker))
                    if seen and not replied:
                        replied = True
                        chan.sendall(reply)
                        if not get_pty:
                            # nothing more to say: give the command EOF on stdin
                            # (and a re-prompt after a wrong reply fails at once
                            # instead of waiting for input that never comes)
                            chan.shutdown_write()
                    elif seen > 1 and get_pty:
                        rejected = True     # asked again: the reply was refused
                        break

                if chan.exit_status_ready():
                    drained = not chan.recv_ready() and not chan.recv_stderr_ready()
                    if drained and (chan.eof_received or chan.closed):
                        break
                    now = time.monotonic()
                    if progressed or exit_seen is None:
                        exit_seen = now
                    elif now - exit_seen > self._EXIT_DRAIN_GRACE:
                        break

                if deadline is not None and time.monotonic() > deadline:
                    raise SSHTimeoutError(
                        f"Command timed out after {timeout}s: {shown!r}",
                        result=result(-1))
                if progressed:
                    idle = 0.001
                else:
                    time.sleep(idle)
                    idle = min(idle * 2, 0.05)

            if rejected:
                return result(-1, "the reply to the password prompt was not accepted")
            # exit_status_ready() is also true for a channel that closed without
            # reporting a status (killed by a signal, link dropped): that reads -1.
            return result(chan.recv_exit_status())
        except SSHTimeoutError:
            raise
        except (paramiko.SSHException, OSError, EOFError) as exc:
            raise SSHCommandError(shown, result(-1, str(exc))) from exc
        finally:
            if chan is not None:
                try:
                    chan.close()
                except Exception:
                    pass

    def run_many(self, commands: Sequence[str], *, stop_on_error: bool = True,
                 timeout: Optional[float] = None, check: bool = False,
                 get_pty: bool = False, environment: Optional[dict] = None,
                 encoding: str = "utf-8") -> list[CommandResult]:
        """Run several commands in order. ``check=True`` raises SSHCommandError on
        the first non-zero exit; ``stop_on_error`` (default) stops the batch on the
        first failure without raising."""
        results = []
        for cmd in commands:
            res = self._run(cmd, timeout=timeout, check=check, get_pty=get_pty,
                            environment=environment, encoding=encoding)
            results.append(res)
            if stop_on_error and not res.ok:
                self._emit(logging.WARNING,
                           f"Stopping batch: {cmd!r} exited {res.exit_code}.")
                break
        return results

    # ------------------------------------------------------------------ #
    # Continuous / streaming output (tail -f, slog2info -w, journalctl -f…)
    # ------------------------------------------------------------------ #
    def iter_lines(self, command: str, *, timeout: Optional[float] = None,
                   stop_event=None, get_pty: bool = False, encoding: str = "utf-8",
                   idle_poll: float = 0.4, combine_stderr: bool = True):
        """
        Run a (possibly never-ending) command and yield its stdout **line by
        line, live** as it arrives. Ideal for `slog2info -w`, `tail -f`,
        `journalctl -f`, `dmesg -w`, etc.

        Stop by: breaking out of the loop, setting ``stop_event`` (a
        threading.Event), or ``timeout`` seconds elapsing. The channel is always
        closed on exit.

        >>> for line in ssh.iter_lines("tail -f /var/log/syslog"):
        ...     print(line)
        """
        chan = self.transport.open_session(timeout=self.config.connect_timeout)
        if get_pty:
            chan.get_pty()
        # merge stderr into stdout so error messages and tools that log to
        # stderr (slog2info, dmesg, many QNX utilities) are actually shown —
        # otherwise the stream looks empty and the command seems "not to run".
        # (Disabled for serial_stream: PowerShell's stderr is CLIXML noise.)
        if combine_stderr:
            chan.set_combine_stderr(True)
        chan.exec_command(command)
        chan.settimeout(idle_poll)
        self._emit(logging.INFO, f"$ {command}  (streaming)")
        start = time.time()
        buf = b""
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if timeout and (time.time() - start) > timeout:
                    break
                try:
                    data = chan.recv(65536)
                    if not data:
                        break  # EOF: command ended
                    buf += data
                    parts = buf.split(b"\n")
                    buf = parts.pop()
                    for ln in parts:
                        yield ln.decode(encoding, errors="replace")
                except socket.timeout:
                    if chan.exit_status_ready() and not chan.recv_ready():
                        break
                    continue
            if buf:
                yield buf.decode(encoding, errors="replace")
        finally:
            try:
                chan.close()
            except Exception:
                pass

    def stream(self, command: str, *, on_line=None, on_match=None, match=None,
               stop_on_match: bool = False, save_to: Optional[str] = None,
               append: bool = True, clean: bool = True,
               timeout: Optional[float] = None, combine_stderr: bool = True,
               stop_event=None, encoding: str = "utf-8", safe: Optional[bool] = None):
        """
        Consume a streaming command with built-in matching and file logging.

        :param on_line:       callback(line) for every line (e.g. a GUI signal).
        :param match:         regex (str/compiled); matching lines are collected
                              and trigger on_match.
        :param stop_on_match: stop as soon as a line matches (send/expect style).
        :param save_to:       local file path to tee every line into.
        :param append:        append (default) vs overwrite the save file.
        :returns:             dict with 'lines' (count) and 'matches' (list).
        """
        import re
        from .results import strip_ansi

        def _do():
            pat = re.compile(match) if isinstance(match, str) else match
            matches, count = [], 0
            fh = None
            if save_to:
                target = os.path.abspath(os.path.expanduser(save_to))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                fh = open(target, "a" if append else "w", encoding=encoding)
            try:
                for line in self.iter_lines(command, timeout=timeout,
                                            stop_event=stop_event, encoding=encoding,
                                            combine_stderr=combine_stderr):
                    if clean:
                        line = strip_ansi(line)
                    count += 1
                    if fh:
                        fh.write(line + "\n")
                        fh.flush()
                    if on_line:
                        on_line(line)
                    if pat and pat.search(line):
                        matches.append(line)
                        if on_match:
                            on_match(line)
                        if stop_on_match:
                            break
                return {"lines": count, "matches": matches,
                        "matched": bool(matches), "saved_to": save_to}
            finally:
                if fh:
                    fh.close()

        return self._guard("stream", _do, safe=safe)

    @staticmethod
    def _ps_encode(script: str) -> str:
        """Encode a PowerShell script for `powershell -EncodedCommand` (UTF-16LE
        base64) - sidesteps all quoting issues over SSH -> cmd -> powershell."""
        import base64
        return base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    @staticmethod
    def _ps_sq(value: str) -> str:
        """Escape a value for embedding inside a single-quoted PowerShell string
        (a single quote is doubled). Prevents a device/port name that contains a
        quote from breaking out of the command."""
        return str(value).replace("'", "''")

    def serial_stream(self, device: str = "/dev/ttyUSB0", *, baudrate: int = 115200,
                      mode: str = "auto", on_line=None, on_match=None, match=None,
                      stop_on_match: bool = False, save_to: Optional[str] = None,
                      clean: bool = True, timeout: Optional[float] = None,
                      stop_event=None, configure: bool = True,
                      safe: Optional[bool] = None):
        """
        Stream a serial port attached to the **remote** host, over SSH — so it
        works through a jump host (laptop -> RDP machine -> target). Same live
        match + save-to-file model as :meth:`stream`.

        ``mode``:
          * "auto"    - "COMx" -> windows, otherwise linux (default)
          * "windows" - read a COM port via a PowerShell SerialPort reader
          * "linux"   - set speed with ``stty`` then ``cat`` the device file

        >>> # Windows COM port on the remote machine:
        >>> ssh.serial_stream("COM5", baudrate=115200, match="login:",
        ...                   save_to="console.log", on_line=print)
        >>> # Linux device file on the remote machine:
        >>> ssh.serial_stream("/dev/ttyUSB0", baudrate=115200, on_line=print)
        """
        is_windows = (mode == "windows" or
                      (mode == "auto" and device.upper().startswith("COM")))
        if is_windows:
            # Force UTF-8 console output so our UTF-8 reader doesn't get mojibake,
            # and read whatever bytes are available (robust to any line ending)
            # rather than ReadLine (which hangs if the device's EOL differs).
            # Track our PID in a per-port file (same scheme as serial_bridge) so
            # serial_release() can kill this reader — closing the SSH exec channel
            # does NOT terminate a no-PTY child on Windows OpenSSH, which otherwise
            # left the COM port locked after the stream ended.
            dev = self._ps_sq(device)
            safekey = "".join(c for c in device if c.isalnum()) or "port"
            ps = (
                "$ErrorActionPreference='SilentlyContinue';"
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                f"$pf=Join-Path $env:TEMP 'turbossh_ser_{safekey}.pid';"
                "Set-Content -LiteralPath $pf -Value $PID;"
                f"$p=New-Object System.IO.Ports.SerialPort '{dev}',{int(baudrate)},"
                "'None',8,'One';"
                "$p.Encoding=[System.Text.Encoding]::UTF8;"
                # say so and stop if the port will not open — otherwise the loop
                # below just spins on a closed port and the stream looks empty
                "try{$p.Open()}catch{"
                "Remove-Item -LiteralPath $pf -Force -ErrorAction SilentlyContinue;"
                "[Console]::Out.WriteLine('[serial] cannot open the port: '"
                "+$_.Exception.Message);[Console]::Out.Flush();exit 2};"
                "try{while($true){"
                "if($p.BytesToRead -gt 0){"
                "$s=$p.ReadExisting();[Console]::Out.Write($s);[Console]::Out.Flush()}"
                "else{Start-Sleep -Milliseconds 30}}}"
                "finally{try{$p.Close()}catch{};"
                "Remove-Item -LiteralPath $pf -Force -ErrorAction SilentlyContinue}"
            )
            cmd = ("powershell -NoProfile -EncodedCommand "
                   + self._ps_encode(_SERIAL_PS_TAG + ps))
        else:
            dev = shlex.quote(device)
            if configure:
                cmd = (f"stty -F {dev} {int(baudrate)} raw -echo -echoe -echok "
                       f"2>/dev/null; cat {dev}")
            else:
                cmd = f"cat {dev}"
        return self.stream(cmd, on_line=on_line, on_match=on_match, match=match,
                           stop_on_match=stop_on_match, save_to=save_to, clean=clean,
                           timeout=timeout, stop_event=stop_event,
                           combine_stderr=False, safe=safe)

    def serial_write(self, device: str, data: str, *, baudrate: int = 115200,
                     mode: str = "auto", newline: bool = True,
                     safe: Optional[bool] = None):
        """
        Write a line to a serial port on the remote host (over SSH/jump).
        Opens, writes, and closes the port (don't run while serial_stream holds
        it open on Windows - the port can't be shared).
        """
        is_windows = (mode == "windows" or
                      (mode == "auto" and device.upper().startswith("COM")))
        if is_windows:
            payload = data.replace("'", "''")
            meth = "WriteLine" if newline else "Write"
            ps = (
                f"$p=New-Object System.IO.Ports.SerialPort '{self._ps_sq(device)}',"
                f"{int(baudrate)},'None',8,'One';"
                f"$p.Open();$p.{meth}('{payload}');Start-Sleep -Milliseconds 200;"
                "$p.Close()"
            )
            cmd = f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}"
            return self.run(cmd, safe=safe)
        payload = data + ("\n" if newline else "")
        dev = shlex.quote(device)
        return self.run(f"printf %s {shlex.quote(payload)} > {dev}", safe=safe)

    def serial_bridge(self, device: str, *, baudrate: int = 115200,
                      mode: str = "auto", force: bool = False):
        """
        Open a **raw, bidirectional, interactive** serial bridge over SSH and
        return the paramiko channel. Bytes you ``.send()`` go straight to the
        port; bytes the port emits come back via ``.recv()`` — char-by-char, so
        it behaves like a NATIVE serial terminal (Tab-completion, Ctrl-C, arrows
        all work through the device's own shell).

        A **PTY is allocated** (like an interactive shell) and the remote console
        is put in RAW mode (no line buffering / no echo) — without this, Windows
        OpenSSH buffers stdin and keystrokes never reach the port (only output
        works). Closing the channel ends the bridge and releases the port; the
        PTY teardown also kills the process if the SSH link drops unexpectedly.

        ``force=True`` clears a stale holder of the port first (kills the tracked
        PID and any other turbossh serial PowerShell — recognised by the tag our
        serial scripts start with, so no unrelated PowerShell on that machine is
        touched), then retries — use it only after the user confirms taking a
        port that's already in use.
        """
        self._ensure_connected()
        is_windows = (mode == "windows" or
                      (mode == "auto" and device.upper().startswith("COM")))
        if is_windows:
            safe = "".join(c for c in device if c.isalnum()) or "port"
            forceblock = (
                "Get-CimInstance Win32_Process|Where-Object{"
                "$_.Name -eq 'powershell.exe' -and $_.ProcessId -ne $PID -and "
                "$_.CommandLine -like '*__TAG__*'}|ForEach-Object{"
                "try{Stop-Process -Id $_.ProcessId -Force}catch{}};"
                "Start-Sleep -Milliseconds 300;"
            ) if force else ""
            retries = "15" if force else "3"
            ps = (
                "$ErrorActionPreference='SilentlyContinue';"
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "$dev='__DEV__';"
                "$pf=Join-Path $env:TEMP 'turbossh_ser___SAFE__.pid';"
                # always clear our own tracked stale holder for this port
                "if(Test-Path $pf){$old=Get-Content $pf;"
                "if($old){$pp=Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue;"
                "if($pp -and $pp.ProcessName -eq 'powershell'){"
                "try{Stop-Process -Id ([int]$old) -Force}catch{};Start-Sleep -Milliseconds 200}}};"
                "__FORCEKILL__"
                "$p=New-Object System.IO.Ports.SerialPort $dev,__BAUD__,'None',8,'One';"
                "$p.ReadTimeout=50;$p.WriteTimeout=500;$ok=$false;"
                "for($i=0;$i -lt __RETRIES__;$i++){try{$p.Open();$ok=$true;break}"
                "catch{Start-Sleep -Milliseconds 350}};"
                "if(-not $ok){[Console]::Out.Write('BUSY:'+$dev);[Console]::Out.Flush();exit 2};"
                "Set-Content -LiteralPath $pf -Value $PID;"
                # RAW console input: no line buffering, no echo, Ctrl-C as a byte
                "try{$t=Add-Type -Name TSio -Namespace TS -PassThru -MemberDefinition "
                "'[DllImport(\"kernel32.dll\")] public static extern System.IntPtr GetStdHandle(int n);"
                "[DllImport(\"kernel32.dll\")] public static extern bool GetConsoleMode(System.IntPtr h,out uint m);"
                "[DllImport(\"kernel32.dll\")] public static extern bool SetConsoleMode(System.IntPtr h,uint m);';"
                "$h=$t::GetStdHandle(-10);$m=0;"
                "if($t::GetConsoleMode($h,[ref]$m)){$t::SetConsoleMode($h,($m -band 0xFFFFFFF8))|Out-Null}}catch{};"
                "$out=[Console]::OpenStandardOutput();"
                "$in=[Console]::OpenStandardInput();$ib=New-Object byte[] 4096;"
                "$ar=$in.BeginRead($ib,0,4096,$null,$null);"
                "try{while($true){"
                "try{if($p.BytesToRead -gt 0){$s=$p.ReadExisting();"
                "if($s){$b=[System.Text.Encoding]::UTF8.GetBytes($s);"
                "$out.Write($b,0,$b.Length);$out.Flush()}}}catch{};"
                "if($ar.IsCompleted){try{$n=$in.EndRead($ar)}catch{$n=0};"
                "if($n -le 0){break};try{$p.Write($ib,0,$n)}catch{};"
                "$ar=$in.BeginRead($ib,0,4096,$null,$null)};"
                "Start-Sleep -Milliseconds 5}}"
                "finally{try{$p.Close()}catch{};"
                "Remove-Item -LiteralPath $pf -Force -ErrorAction SilentlyContinue}"
            )
            ps = (ps.replace("__FORCEKILL__", forceblock)
                    .replace("__RETRIES__", retries)
                    .replace("__DEV__", self._ps_sq(device))
                    .replace("__BAUD__", str(int(baudrate)))
                    .replace("__SAFE__", safe)
                    .replace("__TAG__", _serial_ps_tag_b64()))
            cmd = ("powershell -NoProfile -EncodedCommand "
                   + self._ps_encode(_SERIAL_PS_TAG + ps))
        else:
            dev = shlex.quote(device)
            fk = f"fuser -k {dev} 2>/dev/null; " if force else ""
            cmd = (f"{fk}"
                   f"stty -F {dev} {int(baudrate)} raw -echo 2>/dev/null; "
                   f"cat {dev} & __r=$!; cat > {dev}; kill $__r 2>/dev/null")
        chan = self.transport.open_session(timeout=self.config.connect_timeout)
        # PTY -> stdin delivered interactively (input works). Wide so the remote
        # conpty doesn't hard-wrap the device's output.
        chan.get_pty(term="xterm", width=250, height=50)
        chan.exec_command(cmd)
        return chan

    def serial_in_use(self, device: str, *, mode: str = "auto",
                      safe: Optional[bool] = None) -> bool:
        """Return True if *device* is currently in use (can't be opened). Used to
        ask the user before forcibly taking a port that's already busy."""
        is_windows = (mode == "windows" or
                      (mode == "auto" and device.upper().startswith("COM")))
        if is_windows:
            ps = (f"$p=New-Object System.IO.Ports.SerialPort '{self._ps_sq(device)}',"
                  "9600;try{$p.Open();$p.Close();'FREE'}catch{'BUSY'}")
            cmd = f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}"
            res = self.run(cmd, timeout=15, safe=False)
        else:
            dev = shlex.quote(device)
            res = self.run(f"fuser {dev} >/dev/null 2>&1 && echo BUSY || echo FREE",
                           timeout=15, safe=False)
        text = getattr(res, "text", "") or ""
        return "BUSY" in text

    def serial_release(self, device: str, *, mode: str = "auto",
                       safe: Optional[bool] = None):
        """
        Forcibly release a serial port opened by :meth:`serial_bridge` — kill the
        bridge process (tracked in a per-port PID file) so the OS frees the port.
        Call this on close: relying on stdin-EOF alone can leave the remote
        PowerShell orphaned (Windows OpenSSH), keeping the port locked.
        """
        is_windows = (mode == "windows" or
                      (mode == "auto" and device.upper().startswith("COM")))
        if is_windows:
            safekey = "".join(c for c in device if c.isalnum()) or "port"
            ps = (
                "$pf=Join-Path $env:TEMP 'turbossh_ser_" + safekey + ".pid';"
                "if(Test-Path $pf){$o=Get-Content $pf;if($o){"
                "$pp=Get-Process -Id ([int]$o) -ErrorAction SilentlyContinue;"
                "if($pp -and $pp.ProcessName -eq 'powershell'){"
                "try{Stop-Process -Id ([int]$o) -Force}catch{}}};"
                "Remove-Item $pf -Force -ErrorAction SilentlyContinue};'released'"
            )
            cmd = f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}"
            return self.run(cmd, timeout=15, safe=safe)
        dev = shlex.quote(device)
        return self.run(f"fuser -k {dev} 2>/dev/null; true", timeout=15, safe=safe)

    def remote_serial_ports(self, *, safe: Optional[bool] = None):
        """
        Enumerate the serial ports on the **remote** host (over SSH) so a user
        can pick the right one without guessing — ideal for the "serial via the
        RDP machine" case, where the ports live on that machine, not the laptop.

        Auto-detects the remote OS:
          * Windows (the usual RDP box) -> COM ports + friendly names, via
            ``Win32_PnPEntity`` and ``SerialPort.GetPortNames()``.
          * Linux / QNX -> the usual ``/dev`` serial device nodes.

        Returns a list of ``{"device": "COM6", "description": "USB Serial (COM6)"}``
        dicts (sorted, de-duplicated).

        >>> for p in ssh.remote_serial_ports():
        ...     print(p["device"], "-", p["description"])
        """
        def _do():
            self._ensure_connected()
            ports: list[dict] = []

            # --- Windows COM ports (friendly names where available) ---
            ps = (
                "$ErrorActionPreference='SilentlyContinue';"
                "$d=[ordered]@{};"
                "Get-CimInstance Win32_PnPEntity | "
                "Where-Object { $_.Name -match '\\((COM\\d+)\\)' } | "
                "ForEach-Object { if ($_.Name -match '\\((COM\\d+)\\)') "
                "{ $d[$matches[1]] = $_.Name } };"
                "foreach ($p in [System.IO.Ports.SerialPort]::GetPortNames()) "
                "{ if (-not $d.Contains($p)) { $d[$p] = $p } };"
                "$d.GetEnumerator() | ForEach-Object { \"$($_.Key)|$($_.Value)\" }"
            )
            win = self._run(
                f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}",
                timeout=20, check=False, get_pty=False, environment=None,
                encoding="utf-8")
            if win.ok and win.stdout.strip():
                for line in win.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    dev, _, desc = line.partition("|")
                    dev = dev.strip()
                    if dev:
                        ports.append({"device": dev,
                                      "description": (desc.strip() or dev)})

            # --- Linux / QNX device nodes (fallback if no COM ports) ---
            if not ports:
                lin = self._run(
                    "for d in /dev/ser* /dev/ttyUSB* /dev/ttyACM* /dev/ttyS*; "
                    "do [ -e \"$d\" ] && echo \"$d\"; done 2>/dev/null",
                    timeout=20, check=False, get_pty=False, environment=None,
                    encoding="utf-8")
                if lin.ok:
                    for line in lin.stdout.splitlines():
                        d = line.strip()
                        if d:
                            ports.append({"device": d, "description": d})

            # de-dup, keep order
            seen, out = set(), []
            for p in ports:
                if p["device"] not in seen:
                    seen.add(p["device"]); out.append(p)
            return out

        return self._guard("remote_serial_ports", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Remote webcam (ffmpeg/dshow on the remote -> MJPEG over SSH)
    # ------------------------------------------------------------------ #
    def list_cameras(self, *, ffmpeg: str = "ffmpeg", safe: Optional[bool] = None):
        """List DirectShow video capture devices on the remote (Windows) host.
        ``ffmpeg`` is the remote ffmpeg path. Returns a list of camera names."""
        def _do():
            self._ensure_connected()
            # ffmpeg prints the device list to stderr; -list_devices then exits.
            cmd = (f'"{ffmpeg}" -hide_banner -list_devices true -f dshow -i dummy')
            r = self._run(cmd, timeout=20, check=False, get_pty=False,
                          environment=None, encoding="utf-8")
            return parse_dshow_devices((r.stdout or "") + "\n" + (r.stderr or ""))
        return self._guard("list_cameras", _do, safe=safe)

    @staticmethod
    def _wait_for_jpeg(chan, timeout):
        """Read from *chan* until a JPEG SOI (FFD8) appears; return the bytes read
        so far (to be replayed into the decoder), or None if no JPEG marker shows
        up within *timeout*. This PROVES the transport actually delivers decodable
        video — so we never hand back a 'connected but silent' channel."""
        try:
            chan.settimeout(0.3)
        except Exception:
            pass
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                d = chan.recv(65536)
            except socket.timeout:
                continue
            except Exception:
                break
            if not d:
                if getattr(chan, "eof_received", False) or chan.closed:
                    break
                continue
            buf += d
            if b"\xff\xd8" in buf:
                return buf
            if len(buf) > 4 * 1024 * 1024:    # bytes flowing but no JPEG -> mangled
                break
        return None

    def webcam_channel(self, camera: str, *, ffmpeg: str = "ffmpeg",
                       width: int = 640, height: int = 480, fps: int = 15,
                       quality: int = 6, force: bool = False):
        """Stream the remote webcam as MJPEG and return a paramiko channel to read
        JPEG frames from with ``.recv()``; close it to stop. The returned channel
        carries a ``_prebuf`` (bytes already read while verifying) that the reader
        must replay first, and ``_ffmpeg_exec`` (the ffmpeg process channel, for
        stderr/teardown).

        Transport is **self-selecting and verified**: we try a binary-clean SSH
        direct-tcpip TUNNEL first (ffmpeg serves MJPEG on a loopback TCP port), and
        only keep it if real JPEG frames actually arrive. If the tunnel stays
        silent we fall back to reading ffmpeg's STDOUT over the exec channel. Either
        way we return only a channel that is provably delivering video.

        Capture is in the camera's DEFAULT mode (raw — reliably opens over SSH; a
        specific `-vcodec` gave "Could not set video options" on some cams) and is
        only ever scaled DOWN (`min(iw,W)`, never up; upscaling + full-fps encoding
        overran the capture buffer). The ffmpeg path carries the ``turbossh_cam``
        marker so :meth:`webcam_release` / ``force`` can target only our process.
        """
        import random
        self._ensure_connected()
        # dshow device names sit inside double quotes (`-i video="Name"`); strip any
        # stray double quote so a crafted name can't break out of the argument.
        camera = str(camera).replace('"', "")
        marker = "turbossh_cam"
        transport = self._client.get_transport()
        # ALWAYS clear a stale ffmpeg first. Closing an SSH exec channel doesn't
        # kill the (no-PTY) child on Windows, so a previous attempt can leave an
        # ffmpeg holding the camera -> next open fails "device in use / I/O error".
        # force=True clears ALL ffmpeg; otherwise only our own (turbossh_cam path).
        filt = "$_.Name -eq 'ffmpeg.exe'"
        if not force:
            filt += (f" -and ($_.CommandLine -like '*{marker}*' "
                     f"-or $_.ExecutablePath -like '*{marker}*')")
        forcekill = (
            'powershell -NoProfile -Command "Get-CimInstance Win32_Process | '
            f'Where-Object {{ {filt} }} | ForEach-Object {{ try {{ Stop-Process '
            '-Id $_.ProcessId -Force } catch {} }; '
            'Start-Sleep -Milliseconds 700" & ')
        # Pick a SHARP capture mode. The raw default is only 640x480 -> blurry when
        # shown larger. Most webcams expose higher-res MJPEG modes; enumerate them
        # and take the largest MJPEG at/below the requested width, then stream it
        # through UNCHANGED (`-c:v copy`): native resolution, no re-encode (so no
        # quality loss) and tiny CPU/bandwidth. Fall back to the raw default +
        # downscale + light re-encode if the camera has no MJPEG mode.
        chosen = None
        try:
            lst = self._run(f'"{ffmpeg}" -hide_banner -list_options true -f dshow '
                            f'-i video="{camera}"', timeout=20, check=False,
                            get_pty=False, environment=None, encoding="utf-8")
            mj = [m for m in parse_dshow_modes((lst.stdout or "") + "\n" +
                                               (lst.stderr or "")) if "jpeg" in m["codec"]]
            if mj:
                le = [m for m in mj if m["w"] <= int(width)]
                chosen = (max(le, key=lambda m: m["w"] * m["h"]) if le
                          else min(mj, key=lambda m: m["w"] * m["h"]))
        except Exception:
            chosen = None
        if chosen:
            # use the mode's OWN framerate — dshow needs an exact (size, fps) match,
            # so a reduced -framerate on a 30-only mode fails "could not set options".
            cfps = chosen["fps"] or 30
            cap_base = (f'"{ffmpeg}" -hide_banner -loglevel error -f dshow '
                        f'-rtbufsize 100M -vcodec mjpeg '
                        f'-video_size {chosen["w"]}x{chosen["h"]} -framerate {cfps} '
                        f'-i video="{camera}" -an -c:v copy -f mjpeg ')
        else:
            cap_base = (f'"{ffmpeg}" -hide_banner -loglevel error -f dshow -rtbufsize 200M '
                        f'-i video="{camera}" -an '
                        f"-vf \"scale='min(iw,{int(width)})':-2\" "
                        f'-r {int(fps)} -f mjpeg -q:v 3 ')

        # --- Transport A: binary-clean direct-tcpip tunnel (verified) ---
        for _ in range(2):
            port = random.randint(20001, 60000)
            cmd = (forcekill + cap_base +
                   f'"tcp://127.0.0.1:{port}?listen=1&listen_timeout=30000"')
            exec_chan = transport.open_session()
            exec_chan.exec_command(cmd)
            data_chan = None
            for _ in range(75):                         # ~15s for ffmpeg to bind
                if exec_chan.exit_status_ready():
                    break
                try:
                    data_chan = transport.open_channel(
                        "direct-tcpip", ("127.0.0.1", port), ("127.0.0.1", 0))
                    break
                except Exception:
                    time.sleep(0.2)
            if data_chan is not None:
                prebuf = self._wait_for_jpeg(data_chan, 7.0)
                if prebuf is not None:                  # tunnel really delivers
                    data_chan._ffmpeg_exec = exec_chan
                    data_chan._prebuf = prebuf
                    return data_chan
                try: data_chan.close()
                except Exception: pass
            try: exec_chan.close()
            except Exception: pass

        # --- Transport B: ffmpeg stdout over the exec channel (verified fallback) ---
        for _ in range(2):
            chan = transport.open_session()
            chan.exec_command(forcekill + cap_base + '-')   # MJPEG -> stdout
            prebuf = self._wait_for_jpeg(chan, 7.0)
            if prebuf is not None:
                chan._prebuf = prebuf                       # exec chan IS the data chan
                return chan
            try: chan.close()
            except Exception: pass

        # The last attempt's ffmpeg is still running (closing an exec channel
        # does not kill it on Windows) — release the camera before giving up.
        try:
            self.webcam_release(safe=True)
        except Exception:
            pass
        raise SSHConnectionError(
            "The remote camera opens, but no video frames came through over SSH "
            "(tried both a forwarded tunnel and stdout). That usually means another "
            "app on that machine holds the camera, or Windows camera privacy is "
            "blocking it. Close other camera apps and check Settings -> Privacy -> "
            "Camera -> 'Let desktop apps access your camera'.")

    def webcam_release(self, *, ffmpeg_marker: str = "turbossh_cam",
                       safe: Optional[bool] = None):
        """Kill the remote ffmpeg started by :meth:`webcam_channel` (matched by
        the pushed-ffmpeg path marker) so the camera is released cleanly."""
        ps = (
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "
            f"'ffmpeg.exe' -and ($_.CommandLine -like '*{ffmpeg_marker}*' "
            f"-or $_.ExecutablePath -like '*{ffmpeg_marker}*') }} | "
            "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} };"
            "Start-Sleep -Milliseconds 300;'released'")
        cmd = f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}"
        return self.run(cmd, timeout=15, safe=safe)

    def sudo(self, command: str, password: Optional[Union[str, Secret]] = None,
             *, timeout: Optional[float] = None, check: bool = False,
             get_pty: bool = False, safe: Optional[bool] = None):
        """Run a command via ``sudo -S``.

        The password is sent only if — and when — sudo actually asks for it: sudo
        is given a private prompt marker, and the password is written once that
        marker appears. So on a root / NOPASSWD account, where sudo never asks,
        the password is never written at all (it used to be typed into a PTY up
        front, which echoed it into the command's output and fed it to the
        command's own stdin). A rejected password fails promptly instead of
        waiting forever on a second prompt.

        ``get_pty=True`` is for hosts whose sudoers has ``requiretty``; stderr is
        merged into stdout there. ``timeout`` defaults to ``command_timeout``.
        If the command itself reads stdin and sudo did not prompt, redirect it
        (``... < /dev/null``).
        """
        pw = password if password is not None else self.config.password
        raw = pw.reveal() if isinstance(pw, Secret) else (pw or "")

        def _do():
            marker = f"[sudo-{os.urandom(6).hex()}]"
            eff_timeout = timeout if timeout is not None else self.config.command_timeout
            result = self._exec(
                f"sudo -S -p '{marker}' {command}", timeout=eff_timeout,
                get_pty=get_pty, environment=None, encoding="utf-8",
                prompt=(marker.encode("ascii"), (raw + "\n").encode("utf-8")),
                label=f"sudo {command}")
            # the prompt marker is ours, not part of the command's output
            result.stdout = result.stdout.replace(marker, "")
            result.stderr = result.stderr.replace(marker, "")
            lvl = logging.INFO if result.ok else logging.WARNING
            self._emit(lvl, f"exit={result.exit_code} ({result.duration:.2f}s)")
            if check and not result.ok:
                raise SSHCommandError(f"sudo {command}", result)
            return result

        return self._guard("sudo", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Interactive shell
    # ------------------------------------------------------------------ #
    def open_shell(self, *, term: str = "xterm", width: int = 200,
                   height: int = 50, safe: Optional[bool] = None):
        """Open a persistent interactive ShellSession (PTY)."""
        def _do():
            self._ensure_connected()
            chan = self._client.invoke_shell(term=term, width=width, height=height)
            return ShellSession(chan)
        return self._guard("open_shell", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # SFTP — full operation surface
    # ------------------------------------------------------------------ #
    def sftp(self) -> paramiko.SFTPClient:
        """Return the live SFTPClient (opened lazily, reused; reopened if the
        SFTP subsystem channel died while the connection stayed up).

        One SFTPClient must not be used from two threads at once — Paramiko can
        hand one thread's reply to the other, which then waits forever. A worker
        thread should open its own (``handler.client.open_sftp()``) and pass it
        to :meth:`push` / :meth:`pull` as ``sftp=``.
        """
        self._ensure_connected()
        with self._conn_lock:
            if self._sftp is not None:
                chan = self._sftp.get_channel()
                if chan is None or chan.closed:
                    self._sftp = None
            if self._sftp is None:
                self._sftp = self._client.open_sftp()
            return self._sftp

    # How many symlinked folders deep a recursive walk follows. A link loop is
    # normally caught by comparing real paths, but that relies on the server's
    # realpath resolving links; this bounds the walk on servers where it doesn't.
    _MAX_LINK_HOPS = 8

    def _entry_kind(self, sftp, entry, path: str):
        """``(kind, is_link)`` for a listdir_attr entry: kind is 'dir' or 'file'
        after following a symlink (one extra stat), or None for a dangling link.
        Tolerates servers that send no st_mode."""
        mode = entry.st_mode
        is_link = mode is not None and stat.S_ISLNK(mode)
        if is_link:
            try:
                mode = sftp.stat(path).st_mode
            except IOError:
                return None, True
        if mode is None:
            return "file", is_link
        return ("dir" if stat.S_ISDIR(mode) else "file"), is_link

    @staticmethod
    def _real_dir(sftp, path: str) -> str:
        try:
            return sftp.normalize(path)
        except IOError:
            return path

    @staticmethod
    def _rnorm(path: str) -> str:
        return path.replace("\\", "/")

    def _remote_is_dir(self, sftp, path: str) -> bool:
        try:
            return stat.S_ISDIR(sftp.stat(path).st_mode)
        except IOError:
            return False

    def _remote_exists(self, sftp, path: str) -> bool:
        try:
            sftp.stat(path)
            return True
        except IOError:
            return False

    def _mkdir_p(self, sftp, remote_dir: str) -> None:
        remote_dir = self._rnorm(remote_dir)
        if remote_dir in ("", "/", "."):
            return
        is_abs = remote_dir.startswith("/")
        parts = [p for p in remote_dir.split("/") if p]
        current = ""
        for part in parts:
            current = (f"/{part}" if is_abs else part) if current == "" \
                else f"{current}/{part}"
            if not self._remote_exists(sftp, current):
                try:
                    sftp.mkdir(current)
                except IOError:
                    pass

    # thin pass-throughs (paramiko parity) -----------------------------
    def listdir(self, path: str = ".", *, safe=None):
        return self._guard("listdir", lambda: self.sftp().listdir(self._rnorm(path)),
                           safe=safe)

    def listdir_attr(self, path: str = ".", *, safe=None):
        return self._guard("listdir_attr",
                           lambda: self.sftp().listdir_attr(self._rnorm(path)), safe=safe)

    def stat(self, path: str, *, safe=None):
        return self._guard("stat", lambda: self.sftp().stat(self._rnorm(path)), safe=safe)

    def lstat(self, path: str, *, safe=None):
        return self._guard("lstat", lambda: self.sftp().lstat(self._rnorm(path)),
                           safe=safe)

    def exists(self, path: str, *, safe=None):
        return self._guard("exists",
                           lambda: self._remote_exists(self.sftp(), self._rnorm(path)),
                           safe=safe)

    def isdir(self, path: str, *, safe=None):
        return self._guard("isdir",
                           lambda: self._remote_is_dir(self.sftp(), self._rnorm(path)),
                           safe=safe)

    def mkdir(self, path: str, mode: int = 0o777, *, safe=None):
        return self._guard("mkdir",
                           lambda: self.sftp().mkdir(self._rnorm(path), mode), safe=safe)

    def makedirs(self, path: str, *, safe=None):
        return self._guard("makedirs", lambda: self._mkdir_p(self.sftp(), path),
                           safe=safe)

    def rmdir(self, path: str, *, safe=None):
        return self._guard("rmdir", lambda: self.sftp().rmdir(self._rnorm(path)),
                           safe=safe)

    def remove(self, path: str, *, safe=None):
        return self._guard("remove", lambda: self.sftp().remove(self._rnorm(path)),
                           safe=safe)

    def rename(self, old: str, new: str, *, safe=None):
        def _do():
            sftp, src, dst = self.sftp(), self._rnorm(old), self._rnorm(new)
            try:
                return sftp.posix_rename(src, dst)
            except IOError:
                # posix-rename is an OpenSSH extension; fall back to the plain
                # SFTP rename for servers without it (a genuine failure — no
                # permission, missing source — fails again here and is raised).
                return sftp.rename(src, dst)
        return self._guard("rename", _do, safe=safe)

    def chmod(self, path: str, mode: int, *, safe=None):
        return self._guard("chmod", lambda: self.sftp().chmod(self._rnorm(path), mode),
                           safe=safe)

    def chown(self, path: str, uid: int, gid: int, *, safe=None):
        return self._guard("chown",
                           lambda: self.sftp().chown(self._rnorm(path), uid, gid),
                           safe=safe)

    def symlink(self, source: str, dest: str, *, safe=None):
        return self._guard("symlink",
                           lambda: self.sftp().symlink(source, self._rnorm(dest)),
                           safe=safe)

    def readlink(self, path: str, *, safe=None):
        return self._guard("readlink", lambda: self.sftp().readlink(self._rnorm(path)),
                           safe=safe)

    def open(self, path: str, mode: str = "r", bufsize: int = -1, *, safe=None):
        """Open a remote file object (paramiko SFTPFile)."""
        return self._guard("open",
                           lambda: self.sftp().open(self._rnorm(path), mode, bufsize),
                           safe=safe)

    def read_text(self, path: str, encoding: str = "utf-8", *, safe=None):
        def _do():
            with self.sftp().open(self._rnorm(path), "r") as fh:
                return fh.read().decode(encoding, errors="replace")
        return self._guard("read_text", _do, safe=safe)

    def write_text(self, path: str, data: str, encoding: str = "utf-8", *, safe=None):
        def _do():
            with self.sftp().open(self._rnorm(path), "w") as fh:
                fh.write(data.encode(encoding))
            return len(data)
        return self._guard("write_text", _do, safe=safe)

    def walk(self, remote_path: str, _seen: Optional[set] = None, _links: int = 0):
        """Generator like os.walk over a remote tree (dirpath, dirs, files).
        Symlinked directories are followed once (a link loop is not re-entered);
        dangling links are skipped."""
        sftp = self.sftp()
        remote_path = self._rnorm(remote_path)
        seen = set() if _seen is None else _seen
        real = self._real_dir(sftp, remote_path)
        if real in seen:
            return
        seen.add(real)
        dirs, files, linked = [], [], set()
        for entry in sftp.listdir_attr(remote_path):
            kind, is_link = self._entry_kind(
                sftp, entry, posixpath.join(remote_path, entry.filename))
            if kind == "dir":
                if is_link and _links >= self._MAX_LINK_HOPS:
                    continue                # a link loop the server didn't resolve
                dirs.append(entry.filename)
                if is_link:
                    linked.add(entry.filename)
            elif kind == "file":
                files.append(entry.filename)
        yield remote_path, dirs, files
        for d in dirs:
            yield from self.walk(posixpath.join(remote_path, d), seen,
                                 _links + (1 if d in linked else 0))

    # ------------------------------------------------------------------ #
    # Push / Pull (SFTP) with TransferResult
    # ------------------------------------------------------------------ #
    def push(self, local_path: str, remote_path: str, *, recursive: bool = False,
             make_dirs: bool = True, callback=None, sftp=None,
             safe: Optional[bool] = None):
        """Upload a file or directory (SFTP). Returns TransferResult.

        ``callback(bytes_done, bytes_total)`` is called per file as it moves;
        raising from it aborts the transfer. ``sftp`` lets a worker thread use
        its own SFTP client instead of the shared one (see :meth:`sftp`)."""
        return self._guard("push", self._push, local_path, remote_path,
                           recursive=recursive, make_dirs=make_dirs,
                           callback=callback, sftp=sftp, safe=safe)

    def _push(self, local_path, remote_path, *, recursive, make_dirs, callback,
              sftp=None):
        local_path = os.path.expanduser(local_path)
        remote_path = self._rnorm(remote_path)
        sftp = sftp or self.sftp()
        if not os.path.exists(local_path):
            raise SSHTransferError(f"Local path does not exist: {local_path}")
        start = time.time()
        try:
            if os.path.isdir(local_path):
                if not recursive:
                    raise SSHTransferError(f"{local_path} is a directory; "
                                           f"pass recursive=True.")
                size, count = self._push_dir(sftp, local_path, remote_path, callback)
            else:
                if make_dirs:
                    parent = posixpath.dirname(remote_path)
                    if parent:
                        self._mkdir_p(sftp, parent)
                self._emit(logging.INFO, f"PUSH {local_path} -> {remote_path}")
                sftp.put(local_path, remote_path, callback=callback)
                size, count = os.path.getsize(local_path), 1
        except SSHTransferError:
            raise
        except (IOError, OSError, paramiko.SSHException) as exc:
            raise SSHTransferError(f"Failed to push {local_path} -> {remote_path}: "
                                   f"{exc}") from exc
        return TransferResult(local_path, remote_path, "push", "sftp", size,
                              time.time() - start, count)

    def _push_dir(self, sftp, local_dir, remote_dir, callback):
        self._mkdir_p(sftp, remote_dir)
        total, count = 0, 0
        for entry in os.listdir(local_dir):
            lpath = os.path.join(local_dir, entry)
            rpath = posixpath.join(remote_dir, entry)
            if os.path.isdir(lpath):
                s, c = self._push_dir(sftp, lpath, rpath, callback)
                total += s
                count += c
            else:
                self._emit(logging.INFO, f"PUSH {lpath} -> {rpath}")
                sftp.put(lpath, rpath, callback=callback)
                total += os.path.getsize(lpath)
                count += 1
        return total, count

    def pull(self, remote_path: str, local_path: str, *, recursive: bool = False,
             make_dirs: bool = True, callback=None, sftp=None,
             safe: Optional[bool] = None):
        """Download a file or directory (SFTP). Returns TransferResult.

        ``callback`` and ``sftp`` work as in :meth:`push`. A file that fails or is
        aborted part-way is removed rather than left half-written."""
        return self._guard("pull", self._pull, remote_path, local_path,
                           recursive=recursive, make_dirs=make_dirs,
                           callback=callback, sftp=sftp, safe=safe)

    def _get_file(self, sftp, remote_path, local_path, callback) -> None:
        """sftp.get that never leaves a partial / empty local file behind."""
        existed = os.path.exists(local_path)
        try:
            sftp.get(remote_path, local_path, callback=callback)
        except BaseException:
            if not existed and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass
            raise

    def _pull(self, remote_path, local_path, *, recursive, make_dirs, callback,
              sftp=None):
        remote_path = self._rnorm(remote_path)
        local_path = os.path.expanduser(local_path)
        sftp = sftp or self.sftp()
        start = time.time()
        # Verify the remote path exists FIRST, so a missing source never leaves
        # behind an empty local file (sftp.get opens the local file before fetch).
        if not self._remote_exists(sftp, remote_path):
            raise SSHTransferError(f"Remote path does not exist: {remote_path}")
        try:
            if self._remote_is_dir(sftp, remote_path):
                if not recursive:
                    raise SSHTransferError(f"{remote_path} is a directory; "
                                           f"pass recursive=True.")
                size, count = self._pull_dir(sftp, remote_path, local_path, callback)
            else:
                if make_dirs:
                    parent = os.path.dirname(local_path)
                    if parent and not os.path.exists(parent):
                        os.makedirs(parent, exist_ok=True)
                self._emit(logging.INFO, f"PULL {remote_path} -> {local_path}")
                self._get_file(sftp, remote_path, local_path, callback)
                size, count = os.path.getsize(local_path), 1
        except SSHTransferError:
            raise
        except (IOError, OSError, paramiko.SSHException) as exc:
            raise SSHTransferError(f"Failed to pull {remote_path} -> {local_path}: "
                                   f"{exc}") from exc
        return TransferResult(remote_path, local_path, "pull", "sftp", size,
                              time.time() - start, count)

    def _pull_dir(self, sftp, remote_dir, local_dir, callback, _seen=None,
                  _links=0):
        seen = set() if _seen is None else _seen
        real = self._real_dir(sftp, remote_dir)
        if real in seen:                    # a symlink loop: already copied
            return 0, 0
        seen.add(real)
        os.makedirs(local_dir, exist_ok=True)
        total, count = 0, 0
        for entry in sftp.listdir_attr(remote_dir):
            rpath = posixpath.join(remote_dir, entry.filename)
            lpath = os.path.join(local_dir, entry.filename)
            kind, is_link = self._entry_kind(sftp, entry, rpath)
            if kind is None:
                self._emit(logging.WARNING, f"Skipping dangling link {rpath}")
            elif kind == "dir":
                hops = _links + (1 if is_link else 0)
                if hops > self._MAX_LINK_HOPS:
                    self._emit(logging.WARNING,
                               f"Skipping {rpath}: too many nested symlinked "
                               f"folders (a link loop?)")
                    continue
                s, c = self._pull_dir(sftp, rpath, lpath, callback, seen, hops)
                total += s
                count += c
            else:
                self._emit(logging.INFO, f"PULL {rpath} -> {lpath}")
                self._get_file(sftp, rpath, lpath, callback)
                total += os.path.getsize(lpath)
                count += 1
        return total, count

    # ------------------------------------------------------------------ #
    # SCP (optional, via the 'scp' package)
    # ------------------------------------------------------------------ #
    @staticmethod
    def scp_available() -> bool:
        return _HAS_SCP

    def _scp_client(self, callback=None):
        if not _HAS_SCP:
            raise SSHTransferError(
                "SCP support needs the 'scp' package. Install it with: pip install scp "
                "(SFTP push/pull already works without it).")
        self._ensure_connected()
        progress = (lambda fn, sz, sent: callback(sent, sz)) if callback else None
        return SCPClient(self.transport, progress=progress)

    def scp_push(self, local_path: str, remote_path: str, *, recursive: bool = False,
                 callback=None, safe: Optional[bool] = None):
        """Upload via the SCP protocol (alternative to SFTP push)."""
        def _do():
            start = time.time()
            try:
                with self._scp_client(callback) as scp:
                    scp.put(os.path.expanduser(local_path), remote_path,
                            recursive=recursive)
            except SSHTransferError:
                raise
            except Exception as exc:
                raise SSHTransferError(f"SCP push {local_path} -> {remote_path} "
                                       f"failed: {exc}") from exc
            size = (os.path.getsize(os.path.expanduser(local_path))
                    if os.path.isfile(os.path.expanduser(local_path)) else 0)
            return TransferResult(local_path, remote_path, "push", "scp", size,
                                  time.time() - start)
        return self._guard("scp_push", _do, safe=safe)

    def scp_pull(self, remote_path: str, local_path: str, *, recursive: bool = False,
                 callback=None, safe: Optional[bool] = None):
        """Download via the SCP protocol (alternative to SFTP pull)."""
        def _do():
            start = time.time()
            try:
                with self._scp_client(callback) as scp:
                    scp.get(remote_path, os.path.expanduser(local_path),
                            recursive=recursive)
            except SSHTransferError:
                raise
            except Exception as exc:
                raise SSHTransferError(f"SCP pull {remote_path} -> {local_path} "
                                       f"failed: {exc}") from exc
            lp = os.path.expanduser(local_path)
            size = os.path.getsize(lp) if os.path.isfile(lp) else 0
            return TransferResult(remote_path, local_path, "pull", "scp", size,
                                  time.time() - start)
        return self._guard("scp_pull", _do, safe=safe)

    # ------------------------------------------------------------------ #
    # Context manager
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "SSHHandler":
        result = self.connect()
        if isinstance(result, OperationResult) and not result.success:
            raise result.error or SSHConnectionError("connect failed")
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        state = "connected" if self.is_connected else "disconnected"
        return (f"<SSHHandler {self.config.auth_username}@{self.config.host}:"
                f"{self.config.port} {state}>")
