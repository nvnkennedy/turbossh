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
from typing import Callable, Optional, Sequence, Union

import paramiko

from .config import SSHConfig
from .credentials import Secret, mask
from .results import CommandResult, TransferResult, ShellResult, OperationResult
from .exceptions import (
    SSHError,
    SSHConnectionError,
    SSHAuthenticationError,
    SSHTimeoutError,
    SSHCommandError,
    SSHTransferError,
    SSHNotConnectedError,
)

try:  # optional SCP support
    from scp import SCPClient
    _HAS_SCP = True
except Exception:  # pragma: no cover
    SCPClient = None
    _HAS_SCP = False


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
        self._chan.send(data)

    def read_until(self, marker: str, timeout: float = 30.0) -> ShellResult:
        """Read output until *marker* appears or *timeout* elapses."""
        start = time.time()
        buf = ""
        self._chan.settimeout(0.5)
        while time.time() - start < timeout:
            try:
                chunk = self._chan.recv(65536)
                if not chunk:
                    break
                buf += chunk.decode(self.encoding, errors="replace")
                if marker in buf:
                    return ShellResult(buf, marker, False, time.time() - start)
            except socket.timeout:
                continue
        return ShellResult(buf, None, True, time.time() - start)

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
        self._remote_os: Optional[str] = (
            None if config.remote_os == "auto" else config.remote_os
        )

    # ------------------------------------------------------------------ #
    # Logging (secret-aware)
    # ------------------------------------------------------------------ #
    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"turbossh.{self.config.host}")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            logger.addHandler(handler)
        # set the level every time so quiet= is honored even if the per-host
        # logger was created by an earlier handler instance
        logger.setLevel(logging.WARNING if self._quiet else logging.INFO)
        return logger

    def _secrets(self) -> list:
        s = [self.config.password, self.config.key_passphrase]
        if self.config.jump_host:
            s += [self.config.jump_host.password, self.config.jump_host.key_passphrase]
        return [x for x in s if x]

    def _emit(self, level: int, msg: str) -> None:
        safe_msg = mask(msg, *self._secrets())
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

    @staticmethod
    def _apply_legacy_algorithms():
        """Extend Paramiko's preferred algorithm lists with legacy KEX/ciphers/
        host-keys so very old embedded/automotive SSH servers can negotiate.
        Process-wide and idempotent."""
        try:
            from paramiko.transport import Transport
            extra = {
                "_preferred_kex": ("diffie-hellman-group14-sha1",
                                   "diffie-hellman-group1-sha1",
                                   "diffie-hellman-group-exchange-sha1"),
                "_preferred_keys": ("ssh-rsa", "ssh-dss"),
                "_preferred_ciphers": ("aes128-cbc", "aes256-cbc", "3des-cbc",
                                       "blowfish-cbc"),
                "_preferred_macs": ("hmac-sha1", "hmac-md5"),
            }
            for attr, vals in extra.items():
                cur = tuple(getattr(Transport, attr, ()))
                merged = cur + tuple(v for v in vals if v not in cur)
                setattr(Transport, attr, merged)
        except Exception:
            pass

    def _make_client(self) -> paramiko.SSHClient:
        if self.config.enable_legacy_algorithms:
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
        """Connect the jump host and return a direct-tcpip channel to target."""
        self._jump = SSHHandler(
            self.config.jump_host,
            log_callback=self._log_callback,
            logger=self.log,
        )
        self._jump._connect_with_retries()
        self._emit(logging.INFO, f"Tunnelling through jump host "
                                 f"{self.config.jump_host.host}.")
        return self._jump.transport.open_channel(
            "direct-tcpip",
            dest_addr=(self.config.host, self.config.port),
            src_addr=("127.0.0.1", 0),
        )

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

    def _attempt_strategies(self, sock) -> None:
        cfg = self.config
        last_auth: Optional[Exception] = None
        strategies = [False]
        if cfg.allow_empty_password and not cfg.passwordless:
            strategies.append(True)

        for empty in strategies:
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
        cfg = self.config
        attempt, delay, last_exc = 0, cfg.retry_backoff, None
        while attempt < max(1, cfg.max_retries):
            attempt += 1
            sock = None
            try:
                if cfg.jump_host:
                    sock = self._open_jump_channel()
                self._attempt_strategies(sock)
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
        if (cfg.auto_bootstrap_via_winrm and not self._bootstrap_attempted
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
                transport=cfg.winrm_transport, ssh_port=cfg.port,
                log=lambda m: self._emit(logging.INFO, m),
            )
            for _ in range(10):
                if self._port_open(cfg.host, cfg.port, 2.0):
                    break
                time.sleep(1.0)
            return self._connect_with_retries()  # one more pass now that sshd is up

        hint = self._connection_hint() if cfg.diagnose_on_failure else ""
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

    def _connection_hint(self) -> str:
        """A short, actionable explanation appended to connect failures."""
        host, port = self.config.host, self.config.port
        probe_to = min(self.config.connect_timeout, 3.0)
        if self._port_open(host, port, probe_to):
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
        if self._jump is not None:
            self._jump.disconnect()
            self._jump = None

    close = disconnect

    # ------------------------------------------------------------------ #
    # Remote OS awareness
    # ------------------------------------------------------------------ #
    def detect_os(self) -> str:
        """Return 'windows' or 'linux' (cached). Runs one probe command."""
        if self._remote_os:
            return self._remote_os
        self._ensure_connected()
        probe = self._run("uname -s || ver", timeout=10, check=False,
                          get_pty=False, environment=None, encoding="utf-8")
        text = (probe.stdout + probe.stderr).lower()
        self._remote_os = "windows" if ("windows" in text or "microsoft" in text) \
            else "linux"
        self._emit(logging.DEBUG, f"Detected remote OS: {self._remote_os}")
        return self._remote_os

    @property
    def is_windows(self) -> bool:
        return self.detect_os() == "windows"

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
        self._ensure_connected()
        eff_timeout = timeout if timeout is not None else self.config.command_timeout
        self._emit(logging.INFO, f"$ {command}")
        start = time.time()
        try:
            _, stdout, stderr = self._client.exec_command(
                command, timeout=eff_timeout, get_pty=get_pty, environment=environment)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode(encoding, errors="replace")
            err = stderr.read().decode(encoding, errors="replace")
        except socket.timeout as exc:
            raise SSHTimeoutError(
                f"Command timed out after {eff_timeout}s: {command!r}") from exc
        except paramiko.SSHException as exc:
            res = CommandResult(command, -1, "", str(exc), time.time() - start,
                                host=self.config.host)
            raise SSHCommandError(command, res) from exc

        result = CommandResult(command, exit_code, out, err, time.time() - start,
                               host=self.config.host)
        lvl = logging.INFO if result.ok else logging.WARNING
        self._emit(lvl, f"exit={result.exit_code} ({result.duration:.2f}s)")
        if check and not result.ok:
            raise SSHCommandError(command, result)
        return result

    def run_many(self, commands: Sequence[str], *, stop_on_error: bool = True,
                 **kwargs) -> list[CommandResult]:
        results = []
        for cmd in commands:
            res = self._run(cmd, timeout=kwargs.get("timeout"), check=False,
                            get_pty=kwargs.get("get_pty", False),
                            environment=kwargs.get("environment"),
                            encoding=kwargs.get("encoding", "utf-8"))
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
        self._ensure_connected()
        chan = self._client.get_transport().open_session()
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
            fh = open(save_to, "a" if append else "w", encoding=encoding) \
                if save_to else None
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
            ps = (
                "$ErrorActionPreference='SilentlyContinue';"
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                f"$p=New-Object System.IO.Ports.SerialPort '{device}',{int(baudrate)},"
                "'None',8,'One';"
                "$p.Encoding=[System.Text.Encoding]::UTF8;"
                "$p.Open();"
                "while($true){"
                "if($p.BytesToRead -gt 0){"
                "$s=$p.ReadExisting();[Console]::Out.Write($s);[Console]::Out.Flush()}"
                "else{Start-Sleep -Milliseconds 30}}"
            )
            cmd = f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}"
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
                f"$p=New-Object System.IO.Ports.SerialPort '{device}',{int(baudrate)},"
                "'None',8,'One';"
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
        PID and any other turbossh serial PowerShell), then retries — use it only
        after the user confirms taking a port that's already in use.
        """
        self._ensure_connected()
        is_windows = (mode == "windows" or
                      (mode == "auto" and device.upper().startswith("COM")))
        if is_windows:
            safe = "".join(c for c in device if c.isalnum()) or "port"
            forceblock = (
                "Get-CimInstance Win32_Process|Where-Object{"
                "$_.Name -eq 'powershell.exe' -and $_.ProcessId -ne $PID -and "
                "$_.CommandLine -like '*EncodedCommand*'}|ForEach-Object{"
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
                    .replace("__DEV__", device)
                    .replace("__BAUD__", str(int(baudrate)))
                    .replace("__SAFE__", safe))
            cmd = f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}"
        else:
            dev = shlex.quote(device)
            fk = f"fuser -k {dev} 2>/dev/null; " if force else ""
            cmd = (f"{fk}"
                   f"stty -F {dev} {int(baudrate)} raw -echo 2>/dev/null; "
                   f"cat {dev} & __r=$!; cat > {dev}; kill $__r 2>/dev/null")
        chan = self._client.get_transport().open_session()
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
            ps = (f"$p=New-Object System.IO.Ports.SerialPort '{device}',9600;"
                  "try{$p.Open();$p.Close();'FREE'}catch{'BUSY'}")
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
            text = (r.stdout or "") + "\n" + (r.stderr or "")
            cams, in_video = [], False
            import re
            for line in text.splitlines():
                if "(video)" in line:
                    m = re.search(r'"([^"]+)"', line)
                    if m:
                        cams.append(m.group(1))
                elif "(audio)" in line:
                    in_video = False
            return cams
        return self._guard("list_cameras", _do, safe=safe)

    def webcam_channel(self, camera: str, *, ffmpeg: str = "ffmpeg",
                       width: int = 640, height: int = 480, fps: int = 15,
                       quality: int = 6, force: bool = False):
        """Open a raw SSH channel streaming the remote webcam as MJPEG (a series
        of JPEG frames) on stdout. Read frames with ``.recv()``; close the channel
        to stop. NO PTY (binary stream). ``force`` first clears a stale turbossh
        ffmpeg holding the camera. Returns the paramiko channel."""
        self._ensure_connected()
        marker = "turbossh_cam"
        # Run ffmpeg directly so the channel's process IS ffmpeg; its command line
        # carries the marker (the pushed-ffmpeg path), so webcam_release can find
        # and kill only our process. dshow capture -> rescale -> MJPEG to stdout.
        cap = (f'"{ffmpeg}" -hide_banner -loglevel error -f dshow '
               f'-rtbufsize 64M -i video="{camera}" '
               f'-vf scale={int(width)}:{int(height)} -r {int(fps)} '
               f'-f mjpeg -q:v {int(quality)} -')
        if force:
            kill = (
                "powershell -NoProfile -Command \"Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -eq 'ffmpeg.exe' -and $_.CommandLine -like "
                f"'*{marker}*' }} | ForEach-Object {{ try{{ Stop-Process -Id "
                "$_.ProcessId -Force }catch{} }; Start-Sleep -Milliseconds 400\" & ")
            cmd = kill + cap
        else:
            cmd = cap
        chan = self._client.get_transport().open_session()
        chan.exec_command(cmd)
        return chan

    def webcam_release(self, *, ffmpeg_marker: str = "turbossh_cam",
                       safe: Optional[bool] = None):
        """Kill the remote ffmpeg started by :meth:`webcam_channel` (matched by
        the pushed-ffmpeg path marker) so the camera is released cleanly."""
        ps = (
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "
            f"'ffmpeg.exe' -and $_.CommandLine -like '*{ffmpeg_marker}*' }} | "
            "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} };"
            "'released'")
        cmd = f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}"
        return self.run(cmd, timeout=15, safe=safe)

    def sudo(self, command: str, password: Optional[Union[str, Secret]] = None,
             *, timeout: Optional[float] = None, check: bool = False,
             safe: Optional[bool] = None):
        """Run a command via ``sudo -S`` feeding the password on stdin."""
        pw = password if password is not None else self.config.password
        raw = pw.reveal() if isinstance(pw, Secret) else (pw or "")

        def _do():
            self._ensure_connected()
            full = f"sudo -S -p '' {command}"
            self._emit(logging.INFO, f"$ sudo {command}")
            start = time.time()
            stdin, stdout, stderr = self._client.exec_command(full, timeout=timeout,
                                                              get_pty=True)
            if raw:
                stdin.write(raw + "\n")
                stdin.flush()
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            result = CommandResult(f"sudo {command}", exit_code, out, err,
                                   time.time() - start, host=self.config.host)
            if check and not result.ok:
                raise SSHCommandError(command, result)
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
        """Return the live SFTPClient (opened lazily, reused)."""
        self._ensure_connected()
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

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
        return self._guard("rename",
                           lambda: self.sftp().posix_rename(self._rnorm(old),
                                                            self._rnorm(new)), safe=safe)

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

    def walk(self, remote_path: str):
        """Generator like os.walk over a remote tree (dirpath, dirs, files)."""
        sftp = self.sftp()
        remote_path = self._rnorm(remote_path)
        dirs, files = [], []
        for entry in sftp.listdir_attr(remote_path):
            (dirs if stat.S_ISDIR(entry.st_mode) else files).append(entry.filename)
        yield remote_path, dirs, files
        for d in dirs:
            yield from self.walk(posixpath.join(remote_path, d))

    # ------------------------------------------------------------------ #
    # Push / Pull (SFTP) with TransferResult
    # ------------------------------------------------------------------ #
    def push(self, local_path: str, remote_path: str, *, recursive: bool = False,
             make_dirs: bool = True, callback=None, safe: Optional[bool] = None):
        """Upload a file or directory (SFTP). Returns TransferResult."""
        return self._guard("push", self._push, local_path, remote_path,
                           recursive=recursive, make_dirs=make_dirs,
                           callback=callback, safe=safe)

    def _push(self, local_path, remote_path, *, recursive, make_dirs, callback):
        local_path = os.path.expanduser(local_path)
        remote_path = self._rnorm(remote_path)
        sftp = self.sftp()
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
             make_dirs: bool = True, callback=None, safe: Optional[bool] = None):
        """Download a file or directory (SFTP). Returns TransferResult."""
        return self._guard("pull", self._pull, remote_path, local_path,
                           recursive=recursive, make_dirs=make_dirs,
                           callback=callback, safe=safe)

    def _pull(self, remote_path, local_path, *, recursive, make_dirs, callback):
        remote_path = self._rnorm(remote_path)
        local_path = os.path.expanduser(local_path)
        sftp = self.sftp()
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
                existed = os.path.exists(local_path)
                try:
                    sftp.get(remote_path, local_path, callback=callback)
                except BaseException:
                    # don't leave a partial/empty local file behind on failure
                    if not existed and os.path.exists(local_path):
                        try:
                            os.remove(local_path)
                        except OSError:
                            pass
                    raise
                size, count = os.path.getsize(local_path), 1
        except SSHTransferError:
            raise
        except (IOError, OSError, paramiko.SSHException) as exc:
            raise SSHTransferError(f"Failed to pull {remote_path} -> {local_path}: "
                                   f"{exc}") from exc
        return TransferResult(remote_path, local_path, "pull", "sftp", size,
                              time.time() - start, count)

    def _pull_dir(self, sftp, remote_dir, local_dir, callback):
        os.makedirs(local_dir, exist_ok=True)
        total, count = 0, 0
        for entry in sftp.listdir_attr(remote_dir):
            rpath = posixpath.join(remote_dir, entry.filename)
            lpath = os.path.join(local_dir, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                s, c = self._pull_dir(sftp, rpath, lpath, callback)
                total += s
                count += c
            else:
                self._emit(logging.INFO, f"PULL {rpath} -> {lpath}")
                sftp.get(rpath, lpath, callback=callback)
                total += entry.st_size or 0
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
            with self._scp_client(callback) as scp:
                scp.put(os.path.expanduser(local_path), remote_path,
                        recursive=recursive)
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
            with self._scp_client(callback) as scp:
                scp.get(remote_path, os.path.expanduser(local_path),
                        recursive=recursive)
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
