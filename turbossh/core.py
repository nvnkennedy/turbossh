"""
The main SSH handler: command execution, interactive shells, sudo, full SFTP
file operations, optional SCP, jump-host chaining, and remote-OS awareness.

Designed for three consumers from one object:
  * test-automation framework  -> raise-on-error (default)
  * standalone CLI script       -> see turbossh.cli
  * PyQt5 GUI                    -> safe=True + log_callback (see turbossh.gui)
"""

from __future__ import annotations

import base64
import hashlib
import os
import shlex
import socket
import time
import logging
import posixpath
import re
import threading
from typing import Callable, Optional, Sequence, Union

import paramiko

from . import _logging, _transfer
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

# Every long-lived serial PowerShell we start begins with this comment line, so
# its -EncodedCommand argument starts with a fixed base64 prefix. That prefix is
# how a "force" open finds OUR stale readers on the remote machine without
# touching any other PowerShell there. 18 characters -> 36 UTF-16 bytes -> exactly
# 48 base64 characters, so it encodes to a stable prefix with no padding.
_SERIAL_PS_TAG = "#turbossh-serial\n\n"


def _ps_b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-16-le")).decode("ascii")


def _serial_ps_tag_b64() -> str:
    return _ps_b64(_SERIAL_PS_TAG)


def _ps_tag_line(text: str) -> str:
    """A ``#text`` comment line padded to a multiple of 3 characters (6 UTF-16
    bytes -> 8 base64 characters), so a header made of such lines still encodes
    to an exact prefix of the -EncodedCommand argument."""
    line = "#" + text
    return line + " " * (-(len(line) + 1) % 3) + "\n"


def _port_key(device: str) -> str:
    """The per-port key in PID-file names and process tags (ASCII letters and
    digits only, so it is the same in a file name, a tag and a shell word)."""
    return "".join(c for c in str(device) if c.isascii() and c.isalnum()) or "port"


def _serial_ps_header(port_key: str, run: Optional[str] = None) -> str:
    """The comment header of a serial PowerShell: the TurboSSH tag, then the
    port, then (for a stream) a per-run id. Its base64 is a prefix of the
    process's command line, so a force open finds the holders of THIS port only
    (it used to kill every TurboSSH serial reader on the machine, all ports), and
    a stream that ends can stop exactly its own reader."""
    head = _SERIAL_PS_TAG + _ps_tag_line("port=" + port_key.upper())
    if run:
        head += _ps_tag_line("run=" + run)
    return head


# PowerShell treats U+2018..U+201B as single quotes too, so a curly apostrophe
# (pasted from Word or Jira) ends a '...' literal just like ' does.
_PS_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"

# Shared PowerShell fragments of the serial scripts ($pf = the port's PID file,
# $dev = the port). They are kept SHORT: Windows OpenSSH's default shell runs a
# command as `cmd /c <command>`, and cmd refuses a line over 8,191 characters
# ("The command line is too long"); an -EncodedCommand costs 8/3 characters per
# script character. So each script carries only the fragments it uses, and the
# default aliases (gc, sleep, ?, %) stand in for long cmdlet names. All
# Windows commands stay under _WIN_CMD_MAX (a test checks every builder).
_WIN_CMD_MAX = 8000

# $P: every process, read once; $A: this process and its ancestors (never
# stopped: under a PowerShell default shell, sshd's wrapper is one of them and
# carries this script's text).
_PS_LOAD = (
    "$P=@(Get-CimInstance Win32_Process);$A=@($PID);$q=$PID;"
    "foreach($i in 1..8){$q=($P|?{$_.ProcessId -eq $q}).ParentProcessId;"
    "if(-not $q){break};$A+=$q};"
)
# TsOurs: is this Win32_Process one of OUR serial PowerShells? Its command line
# must BE "powershell [switches] -EncodedCommand <tagged script>" (any version:
# the tag is the common prefix) — not merely contain the tag: under a
# PowerShell default shell, sshd's wrapper "powershell -c "powershell ...
# -EncodedCommand <tag...>"" carries the same text, and a forced open killed its
# own parent with it. This process and its ancestors ($A) are never matched.
_PS_OURS_FN = (
    "function TsOurs($w){$w.Name -eq 'powershell.exe' -and "
    "$A -notcontains $w.ProcessId -and $w.CommandLine -match "
    "'^\\s*(\"[^\"]*powershell(\\.exe)?\"|[^\\s\"]*powershell(\\.exe)?)"
    "(\\s+-\\w+)*\\s+-EncodedCommand\\s+__ANYTAG__'};"
)


def _ps_kill_tracked(load: bool) -> str:
    """Stop the port's tracked holder — only if it is still a TurboSSH serial
    PowerShell, so a PID Windows has since reused is never touched. $n is 1 if
    it was stopped. *load*: read the process list ($P, $A) here, i.e. only when
    there is a PID to check (a forced script reads it first)."""
    return ("$n=0;$old=[string](gc -LiteralPath $pf -TotalCount 1);"
            "if($old -match '^\\s*(\\d+)\\s*$'){$op=[int]$Matches[1];"
            + (_PS_LOAD if load else "") +
            "if(TsOurs ($P|?{$_.ProcessId -eq $op})){"
            "Stop-Process -Id $op -Force;$n=1;sleep -m 200}};")


# Force: stop every TurboSSH serial PowerShell holding THIS port — the ones
# whose script starts with this port's header (the decoded form of the port
# tag their command line starts with), or (a reader started by 2.0, whose
# header has no port line) names the port. Never this process or its ancestors.
_PS_KILL_PORT = (
    "$P|?{TsOurs $_}|%{if($_.CommandLine -match 'EncodedCommand\\s+(\\S+)'){try{$s="
    "[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]));"
    "if($s.StartsWith(__PORTHEAD__,[StringComparison]::Ordinal) -or "
    "(-not $s.Contains('#port=') -and $s.Contains(__DEVNEEDLE__))){"
    "Stop-Process -Id $_.ProcessId -Force}}catch{}}};sleep -m 300;"
)


def _ps_busy(var: str) -> str:
    """PowerShell: did opening the port fail because another process holds it
    (*var* = the .NET exception)? Access denied, or — from some USB-serial
    drivers — an IOException for a sharing violation / "in use" (by its Win32
    code, so a localized message counts too)."""
    return (f"({var} -is [UnauthorizedAccessException] -or ({var} -is "
            f"[IO.IOException] -and ((5,32,33,170,2404) -contains "
            f"({var}.HResult -band 65535) -or {var}.Message -match "
            f"'in use|being used|sharing violation|busy')))")


# remove the PID file only while it still names this process
_PS_DROP_OWN_PIDFILE = (
    "if((gc -LiteralPath $pf -TotalCount 1) -eq $PID){"
    "Remove-Item -LiteralPath $pf -Force}"
)
# unwrap PowerShell's MethodInvocationException to the .NET exception
_PS_INNER = "$e=$_.Exception;while($e.InnerException){$e=$e.InnerException};"


def _ps_open_port(baudrate, retries: int) -> str:
    """Open $dev as $p (8N1, SerialPort's defaults), retrying; $why is the
    reason it would not open."""
    return (f"$p=New-Object IO.Ports.SerialPort $dev,{int(baudrate)};"
            f"for($i=0;$i -lt {int(retries)};$i++){{try{{$p.Open();break}}catch{{"
            + _PS_INNER + "$why=$e;sleep -m 350}};")

# The ffmpeg kill script for the remote camera. TsHas = case-insensitive
# "contains" (a short name like H would lose to PowerShell's alias for
# Get-History: aliases win over functions).
_PS_FFMPEG_KILL = (
    "$ErrorActionPreference='SilentlyContinue';$ProgressPreference='SilentlyContinue';"
    "$n=0;function TsHas($s,$m){[bool]$s -and $s.IndexOf($m,"
    "[StringComparison]::OrdinalIgnoreCase) -ge 0};"
    "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\"|ForEach-Object{"
    "$c=[string]$_.CommandLine;$x=[string]$_.ExecutablePath;"
    "if(__COND__){try{Stop-Process -Id $_.ProcessId -Force;$n++}catch{}}};"
    # give dshow a moment to let go of the camera before it is opened again
    "if($n){Start-Sleep -Milliseconds 700}"
)

# What may not appear inside "…" of a remote cmd.exe line (camera names, the
# ffmpeg path): a quote ends the argument; under a PowerShell default shell the
# line is passed behind --%, which stops at a | (even inside quotes) and expands
# %VAR% — and cmd.exe expands %VAR% too. Control characters end the line.
_CMD_UNSAFE = re.compile(r'["|%\x00-\x1f]')
# A path keeps its %VAR%: both shells expand it, which is what the user meant
# (ffmpeg="%ProgramFiles%\ffmpeg\bin\ffmpeg.exe" worked in 2.0).
_CMD_UNSAFE_PATH = re.compile(r'["|\x00-\x1f]')


def _cmd_safe(value) -> str:
    """A camera name for "…" on a cmd.exe line (taken literally: no %VAR%)."""
    return _CMD_UNSAFE.sub("", str(value))


def _cmd_safe_path(value) -> str:
    """A program path for "…" on a cmd.exe line (%VAR% is expanded)."""
    return _CMD_UNSAFE_PATH.sub("", str(value))


# How many bytes of a camera ffmpeg's stderr are kept for diagnostics (the
# Camera tab shows up to 1500 characters of it).
_FFMPEG_STDERR_KEEP = 1500
# Outstanding read requests per SFTP download (OpenSSH's sftp uses 64 too); see
# _transfer, which every download goes through.
_SFTP_PREFETCH = _transfer.PREFETCH
_GET_TAKES_PREFETCH_LIMIT = _transfer.GET_TAKES_PREFETCH_LIMIT

_S_IFMT, _S_IFDIR, _S_IFLNK = 0o170000, 0o040000, 0o120000


def _file_type(mode) -> int:
    """The file-type bits of an SFTP st_mode. stat.S_ISDIR / S_ISLNK raise
    OverflowError on Windows for modes above 0xFFFF (QNX servers send those) and
    TypeError for None (servers that send no mode)."""
    try:
        return int(mode or 0) & _S_IFMT
    except (TypeError, ValueError):
        return 0


_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                 | {f"{p}{n}" for p in ("COM", "LPT")
                    for n in list("0123456789") + ["\u00b9", "\u00b2", "\u00b3"]})


def _safe_local_name(name: str, windows: Optional[bool] = None) -> str:
    """A remote file name made safe to create on this machine. On Windows the
    characters it forbids (``<>:"/\\|?*`` and control characters) become ``_``
    (a ':' would otherwise write an NTFS alternate stream and report success),
    trailing dots and spaces (which Windows silently drops) become ``_``, and a
    reserved device name (CON, NUL, COM1, LPT1.txt …) gets a ``_`` prefix.
    Everywhere, a separator or a '.' / '..' name can never leave the folder."""
    if windows is None:
        windows = os.name == "nt"
    bad = '<>:"/\\|?*' if windows else "/"
    out = "".join("_" if (c in bad or c == "\0" or (windows and ord(c) < 32))
                  else c for c in str(name))
    if windows:
        kept = out.rstrip(" .")
        out = kept + "_" * (len(out) - len(kept))
        if out.split(".")[0].rstrip(" ").upper() in _WIN_RESERVED:
            out = "_" + out
    if out in ("", ".", ".."):
        out = "_" * max(1, len(out))
    return out


_DIGEST_SALT = os.urandom(16)      # per process: a digest says nothing elsewhere


def _secret_digest(value) -> Optional[str]:
    raw = value.reveal() if isinstance(value, Secret) else value
    if raw is None:
        return None
    return hashlib.sha256(_DIGEST_SALT + str(raw).encode(
        "utf-8", "surrogatepass")).hexdigest()


def _link_key(cfg) -> Optional[tuple]:
    """What a connection is made with: the target, the login and how it
    authenticates, and the jump host (secrets as digests only). connect()
    keeps a live connection only while the config still says the same."""
    if cfg is None:
        return None
    return (cfg.host, cfg.port, cfg.auth_username, _secret_digest(cfg.password),
            tuple(cfg.normalized_key_files()), _secret_digest(cfg.key_passphrase),
            bool(cfg.passwordless), bool(cfg.allow_agent), bool(cfg.look_for_keys),
            bool(cfg.enable_legacy_algorithms), repr(cfg.disabled_algorithms),
            _link_key(cfg.jump_host))


def _close_soon(sftp) -> None:
    """Close an SFTP client without making anyone wait for it: on a stalled
    link, closing a channel waits for room in the send buffer — for ever, or
    until the transport is closed. One whose channel is closed already (its
    transport was closed) is closed here; it sends nothing."""
    if sftp is None:
        return
    try:
        chan = sftp.get_channel()
    except Exception:
        chan = None

    def close():
        try:
            sftp.close()
        except Exception:
            pass
    if chan is None or getattr(chan, "closed", True):
        close()
    else:
        threading.Thread(target=close, name="turbossh-sftp-close",
                         daemon=True).start()


_KNOWN_HOSTS_LOCK = threading.Lock()


class _RememberHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """AutoAddPolicy for a configured known_hosts file that APPENDS the new key.

    Paramiko's AutoAddPolicy calls save_host_keys(), which re-reads the file
    (raising on lines it cannot parse, e.g. @cert-authority) and then rewrites
    it from memory — dropping every line it skipped, and with parallel connects
    (SSHPool) one thread truncating the file while another writes it."""

    def __init__(self, path: str, warn: Callable[[str], None]):
        self.path, self.warn = path, warn

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        line = f"{hostname} {key.get_name()} {key.get_base64()}"
        with _KNOWN_HOSTS_LOCK:
            try:
                try:
                    with open(self.path, encoding="utf-8", errors="replace") as fh:
                        existing = fh.read()
                except FileNotFoundError:
                    existing = ""
                if line in (ln.strip() for ln in existing.splitlines()):
                    return                  # a parallel connect just added it
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    if existing and not existing.endswith("\n"):
                        fh.write("\n")
                    fh.write(line + "\n")
            except OSError as exc:
                self.warn(f"Could not remember the host key of {hostname} in "
                          f"{self.path}: {exc}")


class _StderrTail:
    """Reads a channel's stderr on a background thread while the channel is
    open, keeping only the last *keep* bytes.

    stderr shares the channel's flow-control window with stdout: left unread,
    ~2 MB of it (ffmpeg repeating "real-time buffer too full" over a long
    session) stalls the channel and the picture freezes. The channel's
    ``recv_stderr_ready`` / ``recv_stderr`` are redirected to the kept tail, so
    code that reads stderr after a failure (the Camera tab's diagnosis) still
    gets the latest messages."""

    def __init__(self, chan, keep: int = _FFMPEG_STDERR_KEEP):
        cls = type(chan)
        self._chan, self._keep = chan, keep
        self._ready = lambda: cls.recv_stderr_ready(chan)
        self._recv = lambda n: cls.recv_stderr(chan, n)
        self._buf = bytearray()
        self._lock = threading.Lock()
        chan.recv_stderr_ready = self.ready
        chan.recv_stderr = self.recv
        chan._stderr_tail = self
        threading.Thread(target=self._run, name="turbossh-stderr",
                         daemon=True).start()

    def _pump(self) -> bool:
        got = False
        with self._lock:
            try:
                while self._ready():
                    data = self._recv(65536)
                    if not data:
                        break
                    self._buf += data
                    got = True
            except Exception:
                pass
            extra = len(self._buf) - self._keep
            if extra > 0:
                del self._buf[:extra]
                nl = self._buf.find(b"\n")         # start on a whole line
                if 0 <= nl < len(self._buf) - 1:
                    del self._buf[:nl + 1]
        return got

    def _run(self) -> None:
        chan = self._chan
        while not chan.closed:
            if not self._pump():
                if chan.eof_received:
                    break
                time.sleep(0.2)
        self._pump()                     # whatever arrived just before the end

    def ready(self) -> bool:
        self._pump()
        with self._lock:
            return bool(self._buf)

    def recv(self, nbytes: int) -> bytes:
        self._pump()
        with self._lock:
            out = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
        return out

    def text(self) -> str:
        """The kept stderr, without consuming it."""
        self._pump()
        with self._lock:
            return self._buf.decode("utf-8", errors="replace")


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
        # what the connection was made with (see _link_key)
        self._made_with: Optional[tuple] = None
        self._bootstrap_attempted = False
        # Serialises connect / reconnect / disconnect, so two threads that find
        # the link down don't both reconnect (and leak one of the clients).
        self._conn_lock = threading.RLock()
        # Set by disconnect(sticky=True), cleared by connect(): a worker still
        # running after a teardown must not auto-reconnect (nobody would close
        # that connection).
        self._closed = False
        self._kh_warned = False
        self._remote_os: Optional[str] = (
            None if config.remote_os == "auto" else config.remote_os
        )
        self._shell: Optional[str] = None    # remote Windows default shell (cached)
        # markers of the camera streams THIS handler started (see webcam_release)
        self._cam_tokens: set = set()
        self._cam_lock = threading.Lock()

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
    # self._client is read ONCE into a local everywhere below: a disconnect() on
    # another thread can set it to None between two reads.
    @property
    def is_connected(self) -> bool:
        client = self._client
        if client is None:
            return False
        t = client.get_transport()
        return bool(t and t.is_active())

    @property
    def client(self) -> paramiko.SSHClient:
        """The underlying paramiko SSHClient (for anything not wrapped here)."""
        self._ensure_connected()
        client = self._client
        if client is None:
            raise SSHNotConnectedError(f"Disconnected from {self.config.host}.")
        return client

    @property
    def transport(self) -> paramiko.Transport:
        t = self.client.get_transport()
        if t is None:
            raise SSHNotConnectedError(f"Disconnected from {self.config.host}.")
        return t

    def _ensure_connected(self) -> None:
        if self.is_connected:
            return
        with self._conn_lock:
            if self.is_connected:          # another thread reconnected meanwhile
                return
            if self._closed:
                raise SSHNotConnectedError(
                    f"The connection to {self.config.host} was closed with "
                    f"disconnect(sticky=True); call connect() to open it again.")
            if self.config.auto_reconnect:
                self._emit(logging.WARNING, "Session not active; reconnecting…")
                self._connect_with_retries()
            else:
                raise SSHNotConnectedError("Not connected (auto_reconnect disabled).")

    # ------------------------------------------------------------------ #
    # Connect / disconnect
    # ------------------------------------------------------------------ #
    def connect(self, *, safe: Optional[bool] = None):
        """Connect (with retries).

        On a handler that already looks connected, the link is checked first
        (one keepalive request, answered within a few seconds by a live server):
        a live one is kept — a second connection used to be opened and the first
        leaked (``ssh.connect()`` followed by ``with ssh:``) — and a dead one is
        closed and replaced. After a DUT reboot the old TCP link still looks
        open until something is sent on it, so ``ssh.run("reboot"); wait;
        ssh.connect()`` opens a fresh connection, as it did in 2.0."""
        return self._guard("connect", self._connect_explicit, safe=safe)

    # The least time (s) connect() gives a connected server to show it is alive;
    # the connect timeout, when longer. A slow VPN with bulk data in flight can
    # hold the answer back for seconds.
    _LIVENESS_TIMEOUT = 10.0

    def _connect_explicit(self) -> "SSHHandler":
        with self._conn_lock:
            self._closed = False
            client = self._client
            if client is not None and self.is_connected:
                if self._made_with != _link_key(self.config):
                    # ``ssh.config.host = new_ip; ssh.connect()`` means the new
                    # target (2.0 always reconnected)
                    self._emit(logging.INFO,
                               f"The connection settings changed; connecting to "
                               f"{self.config.host} again…")
                    if not self._jump_is_current():
                        self._close_jump()           # first: see _drop_dead_link
                    self._drop_dead_link(client, check_jump=False)
                elif self._link_alive(client.get_transport(), self._liveness_timeout()):
                    return self
                else:
                    self._emit(logging.WARNING,
                               f"The connection to {self.config.host} no longer "
                               f"answers (the target rebooted, or the link dropped); "
                               f"opening a new one…")
                    self._drop_dead_link(client)
            return self._connect_with_retries()

    def _liveness_timeout(self) -> float:
        return max(self._LIVENESS_TIMEOUT, self.config.connect_timeout or 0.0)

    @staticmethod
    def _received(transport):
        """What this transport has received so far (Paramiko's own packet and
        byte counters; None where this Paramiko has other names for them).
        Any change means a packet arrived — the server is alive."""
        p = getattr(transport, "packetizer", None)
        return (getattr(p, "_Packetizer__received_packets", None),
                getattr(p, "_Packetizer__received_bytes", None))

    @classmethod
    def _link_alive(cls, transport, timeout: float) -> bool:
        """True if the server shows within *timeout* that it is alive.

        The probe is a session channel: every SSH server answers that, with
        CONFIRMATION or OPEN_FAILURE (both mean alive; the channel is closed
        again at once). A keepalive request does not do: Paramiko ignores a
        server's SSH_MSG_UNIMPLEMENTED reply, and that live link looked dead.
        Any packet that arrives meanwhile counts as well, so an answer queued
        behind bulk data on a slow link is not needed. The probe runs on a
        helper thread; on a dead link it ends when the transport is closed."""
        if transport is None or not transport.is_active():
            return False
        answered = threading.Event()

        def probe():
            try:
                chan = transport.open_session(timeout=timeout)
            except paramiko.ChannelException:      # OPEN_FAILURE: an answer
                answered.set()
                return
            except Exception:                      # closed, or no answer
                return
            answered.set()
            try:
                chan.close()
            except Exception:
                pass
        before = cls._received(transport)
        threading.Thread(target=probe, name="turbossh-liveness", daemon=True).start()
        deadline = time.monotonic() + timeout
        while True:
            if not transport.is_active():
                return False
            if answered.is_set():
                return True
            now = cls._received(transport)
            if now != before and None not in now:
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            answered.wait(min(0.1, left))

    def _drop_dead_link(self, client, check_jump: bool = True) -> None:
        """Close a connection that stopped answering (and with it its SFTP
        session), and the jump-host connection too if that one is dead as well.

        Nothing is sent on the dead link: with its send buffer full (another
        thread uploading), closing a channel waits for room that never comes,
        and this runs under the connection lock. Closing the transport first
        stops it (the writer gets an error), and the channels go with it. A
        dead jump connection is closed before the target's, whose socket is a
        channel through it."""
        jump = self._jump
        if check_jump and jump is not None:
            jclient = jump._client
            alive = jclient is not None and self._link_alive(
                jclient.get_transport(), self._liveness_timeout())
            if not alive:
                self._close_jump()
        if self._client is client:
            self._client = None
        try:
            client.close()
        except Exception:
            pass
        self._forget_shared_sftp()            # its channel closed with the transport

    @staticmethod
    def _request(chan, timeout: Optional[float], fn, *args, **kwargs):
        """Run a channel request (get_pty / exec_command / invoke_shell /
        invoke_subsystem) bounded by *timeout*. Paramiko waits for the server's
        reply with no limit, so a link that went silent (a VPN drop) hung the
        caller until TCP noticed. Closing the channel wakes that wait."""
        if not timeout:
            return fn(*args, **kwargs)
        fired = threading.Event()

        def expire():
            fired.set()
            try:
                chan.close()
            except Exception:
                pass
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            return fn(*args, **kwargs)
        except (paramiko.SSHException, EOFError, OSError) as exc:
            if fired.is_set():
                raise SSHTimeoutError(f"The server did not answer within "
                                      f"{timeout:g}s.") from exc
            raise
        finally:
            timer.cancel()

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
        # Loaded into the client's own HostKeys (never with load_host_keys, which
        # arms Paramiko's save-on-add; see _RememberHostKeyPolicy). Nothing here
        # may fail the connect: a known_hosts line Paramiko can't parse used to
        # make EVERY connect raise InvalidHostKey.
        configured = self.config.known_hosts
        path = os.path.expanduser(configured or "~/.ssh/known_hosts")
        try:
            self._load_known_hosts(client.get_host_keys(), path)
        except FileNotFoundError:
            if configured:
                self._emit(logging.WARNING, f"known_hosts not found: {configured}")
        except Exception as exc:
            self._emit(logging.WARNING, f"Could not read known_hosts {path}: {exc}")
        if self.config.host_key_policy == "auto" and configured:
            client.set_missing_host_key_policy(_RememberHostKeyPolicy(
                path, lambda m: self._emit(logging.WARNING, m)))
            return client
        policy = self._POLICIES.get(self.config.host_key_policy, paramiko.AutoAddPolicy)
        client.set_missing_host_key_policy(policy())
        return client

    def _load_known_hosts(self, keys: paramiko.HostKeys, path: str) -> None:
        """Read a known_hosts file line by line, skipping what Paramiko can't use
        (@cert-authority / @revoked markers, corrupt keys) instead of failing."""
        from paramiko.hostkeys import HostKeyEntry
        skipped = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("@"):
                    skipped.append(lineno)
                    continue
                try:
                    entry = HostKeyEntry.from_line(line, lineno)
                except Exception:
                    skipped.append(lineno)
                    continue
                if entry is not None:            # None: a key type Paramiko lacks
                    for name in entry.hostnames:
                        keys.add(name, entry.key.get_name(), entry.key)
        if skipped and not self._kh_warned:
            self._kh_warned = True
            self._emit(logging.WARNING,
                       f"known_hosts {path}: ignored {len(skipped)} line(s) this "
                       f"SSH library can't use (e.g. @cert-authority / @revoked) "
                       f"— first at line {skipped[0]}.")

    def _jump_is_current(self) -> bool:
        """True if the jump connection there is one made with the jump-host
        settings the config has now."""
        jump, cfg = self._jump, self.config.jump_host
        return (jump is not None and cfg is not None
                and jump._made_with == _link_key(cfg))

    def _open_jump_channel(self):
        """Return a NEW direct-tcpip channel to the target through the jump host.

        The jump connection is made once and reused: every auth strategy and
        every retry needs its own channel (a failed attempt closes the one it
        used), but reconnecting the jump host each time leaked a connection per
        attempt. One made with other settings (the config's jump host was
        changed) is replaced."""
        if (self._jump is None or not self._jump.is_connected
                or not self._jump_is_current()):
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
                self._install_client(client)
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
                **self._winrm_timeout(),
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
    def _winrm_timeout(self) -> dict:
        """``command_timeout`` for the WinRM install, when SSHConfig sets one."""
        limit = getattr(self.config, "winrm_command_timeout", None)
        return {} if limit is None else {"command_timeout": float(limit)}

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
                **self._winrm_timeout(),
            )
            # wait briefly for the listener to come up
            for _ in range(10):
                if self._port_open(cfg.host, cfg.port, 2.0):
                    break
                time.sleep(1.0)
            return res

        return self._guard("bootstrap_sshd_via_winrm", _do, safe=safe)

    def _install_client(self, client: paramiko.SSHClient) -> None:
        """Make *client* the connection, closing the one it replaces (a reconnect
        after the link died) and with it that connection's SFTP session. The old
        transport is closed first, so nothing is sent on a link that may have
        stalled (see _drop_dead_link)."""
        old, self._client = self._client, client
        self._made_with = _link_key(self.config)
        if old is not None and old is not client:
            try:
                old.close()
            except Exception:
                pass
        self._drop_shared_sftp()

    def _forget_shared_sftp(self, only=None):
        """Forget the shared SFTP client — or only if it is *only* — and return
        it (None if nothing was forgotten)."""
        with self._conn_lock:
            sftp = self._sftp
            if sftp is None or (only is not None and sftp is not only):
                return None
            self._sftp = None
        return sftp

    def _drop_shared_sftp(self, only=None) -> None:
        """Forget (and close) the shared SFTP client — or only if it is *only*.
        Closing a channel waits for room in the link's send buffer, which on a
        stalled link (a timed-out client, a peer that stopped reading) never
        comes: an SFTP client whose channel is still open is closed on a helper
        thread, so no caller waits on it."""
        _close_soon(self._forget_shared_sftp(only))

    def _post_connect(self) -> None:
        t = self._client.get_transport()
        if t and self.config.keepalive_interval > 0:
            t.set_keepalive(self.config.keepalive_interval)

    def disconnect(self, sticky: bool = False) -> None:
        """Close the connection. A later call reconnects by itself, as always,
        when ``auto_reconnect`` is on (a script that disconnects before a DUT
        power cycle reuses the handler afterwards).

        ``sticky=True`` is for disposing of the handler for good: until the next
        explicit connect(), nothing reconnects it — a worker thread still running
        after a GUI tab closed gets SSHNotConnectedError instead of opening a
        connection nobody would ever close.

        Nothing is sent on the link on the way (a link whose peer stopped
        reading kept this waiting for ever): the jump connection is closed
        first, then the transport, and the channels — the shared SFTP
        session's too — close with it."""
        with self._conn_lock:
            self._closed = bool(sticky)
            self._close_jump()
            client, self._client = self._client, None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
                self._emit(logging.INFO, f"Disconnected from {self.config.host}.")
            self._drop_shared_sftp()

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

    def _run(self, command, *, timeout, check, get_pty, environment, encoding,
             expected=()):
        """*expected*: exit codes besides 0 that are an answer, not a failure
        (logged as information, not as a warning)."""
        eff_timeout = timeout if timeout is not None else self.config.command_timeout
        result = self._exec(command, timeout=eff_timeout, get_pty=get_pty,
                            environment=environment, encoding=encoding)
        lvl = (logging.INFO if result.ok or result.exit_code in expected
               else logging.WARNING)
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
                self._request(chan, open_timeout, chan.get_pty)
            if environment:
                chan.update_environment(environment)
            self._request(chan, open_timeout, chan.exec_command, command)

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
        to = self.config.connect_timeout
        chan = self.transport.open_session(timeout=to)
        try:
            if get_pty:
                self._request(chan, to, chan.get_pty)
            # merge stderr into stdout so error messages and tools that log to
            # stderr (slog2info, dmesg, many QNX utilities) are actually shown —
            # otherwise the stream looks empty and the command seems "not to
            # run". (Disabled for serial_stream: PowerShell's stderr is CLIXML.)
            if combine_stderr:
                chan.set_combine_stderr(True)
            self._request(chan, to, chan.exec_command, command)
        except BaseException:
            chan.close()
            raise
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
        return _ps_b64(script)

    @staticmethod
    def _ps_sq(value: str) -> str:
        """Escape a value for embedding inside a single-quoted PowerShell string.
        PowerShell ends such a string at ANY of the quotes U+0027 and
        U+2018..U+201B and reads a doubled one as that character, so each is
        doubled: a curly apostrophe pasted from Word or Jira can't break out."""
        return "".join(c + c if c in _PS_SINGLE_QUOTES else c for c in str(value))

    @staticmethod
    def _is_windows_port(device: str, mode: str) -> bool:
        return mode == "windows" or (mode == "auto"
                                     and str(device).upper().startswith("COM"))

    # Where the Linux / QNX serial bridge records its reader / writer PIDs: a
    # folder of the user's own, ${TMPDIR:-/tmp}/turbossh-<uid> (made 0700). A
    # fixed name in /tmp could be planted by another user — as a link, so the
    # bridge would write over a file of ours, or with PIDs for release to kill.
    # None = that folder; tests point it elsewhere.
    _POSIX_PID_DIR: Optional[str] = None

    def _posix_pid_setup(self, device: str) -> str:
        """sh: set $__pf to this port's PID file — only in a folder that is ours
        (a directory, not a link, owned by us); otherwise $__pf is empty and
        the bridge runs untracked rather than trust someone else's folder."""
        folder = (shlex.quote(self._POSIX_PID_DIR) if self._POSIX_PID_DIR
                  else '"${TMPDIR:-/tmp}/turbossh-$(id -u)"')
        name = f"turbossh_ser_{_port_key(device)}.pid"
        return (f"__d={folder}; "
                "[ -L \"$__d\" ] || mkdir -m 700 \"$__d\" 2>/dev/null; "
                "if [ -d \"$__d\" ] && [ ! -L \"$__d\" ] && [ -O \"$__d\" ]; then "
                f"__pf=\"$__d/{name}\"; [ -L \"$__pf\" ] && rm -f \"$__pf\"; "
                "else __pf=; fi; ")

    @staticmethod
    def _posix_kill_tracked() -> str:
        """sh: kill the processes listed in "$__pf" (the two cats of an earlier
        bridge on this port) and remove it. On Linux a PID is killed only while
        /proc says it is still a cat, so a PID reused since is left alone.
        $__n counts the ones stopped."""
        return ("__n=0; for __p in $(cat \"$__pf\" 2>/dev/null); do "
                "case $__p in ''|*[!0-9]*) continue;; esac; "
                "if [ -r /proc/$__p/cmdline ]; then "
                "case $(tr '\\000' ' ' < /proc/$__p/cmdline 2>/dev/null) in "
                "cat*|*/cat*) ;; *) continue;; esac; fi; "
                "kill $__p 2>/dev/null && __n=$((__n+1)); done; "
                "[ -n \"$__pf\" ] && rm -f \"$__pf\"; ")

    def _win_serial_script(self, device: str, body: str, *, force: bool,
                           tracked: bool = True) -> str:
        """The common head of a Windows serial script, then *body*: the PID
        file ($pf), the port ($dev), and — *tracked* — the port's tracked holder
        stopped if it is still ours; *force* also stops every TurboSSH serial
        reader of the port (see _PS_KILL_PORT)."""
        key = _port_key(device)
        needle = "'" + self._ps_sq(device) + "'"          # as a 2.0 script has it
        head = '"' + _serial_ps_header(key).replace("\n", "`n") + '"'
        ps = ("$ErrorActionPreference='SilentlyContinue';"
              f"$dev='{self._ps_sq(device)}';"
              f"$pf=Join-Path $env:TEMP turbossh_ser_{key}.pid;")
        if force:
            ps += _PS_LOAD
        if tracked or force:
            ps += _PS_OURS_FN.replace("__ANYTAG__", _serial_ps_tag_b64())
        if tracked:
            ps += _ps_kill_tracked(load=not force)
        if force:
            ps += (_PS_KILL_PORT.replace("__PORTHEAD__", head)
                   .replace("__DEVNEEDLE__", "'" + self._ps_sq(needle) + "'"))
        return ps + body

    @staticmethod
    def _ps_command(script: str) -> str:
        return f"powershell -NoProfile -EncodedCommand {_ps_b64(script)}"

    def serial_stream(self, device: str = "/dev/ttyUSB0", *, baudrate: int = 115200,
                      mode: str = "auto", on_line=None, on_match=None, match=None,
                      stop_on_match: bool = False, save_to: Optional[str] = None,
                      clean: bool = True, timeout: Optional[float] = None,
                      stop_event=None, configure: bool = True,
                      safe: Optional[bool] = None, force: bool = False):
        """
        Stream a serial port attached to the **remote** host, over SSH — so it
        works through a jump host (laptop -> RDP machine -> target). Same live
        match + save-to-file model as :meth:`stream`.

        ``mode``:
          * "auto"    - "COMx" -> windows, otherwise linux (default)
          * "windows" - read a COM port via a PowerShell SerialPort reader
          * "linux"   - set speed with ``stty`` then ``cat`` the device file

        A port that another program — or a TurboSSH serial session in the GUI —
        holds is not taken: the stream reports "[serial] cannot open …" and
        ends. ``force=True`` (Windows) first stops a stale TurboSSH reader of
        this port (one an earlier stream or session left behind), as
        :meth:`serial_bridge` does with ``force=True``. The stream's own reader
        is stopped when it ends.

        >>> # Windows COM port on the remote machine:
        >>> ssh.serial_stream("COM5", baudrate=115200, match="login:",
        ...                   save_to="console.log", on_line=print)
        >>> # Linux device file on the remote machine:
        >>> ssh.serial_stream("/dev/ttyUSB0", baudrate=115200, on_line=print)
        """
        run = None
        if self._is_windows_port(device, mode):
            # Force UTF-8 console output so our UTF-8 reader doesn't get mojibake,
            # and read whatever bytes are available (robust to any line ending)
            # rather than ReadLine (which hangs if the device's EOL differs).
            # Same port lifecycle as serial_bridge: the PID file is written only
            # once the port is open and removed only while it still names this
            # reader, and the reader is stopped when the stream ends. (The PID
            # file used to be written before Open() and deleted on failure: a
            # retry on a busy port erased the record of the real holder, which
            # then kept the port until a reboot.) The tracked holder is touched
            # only with force: it may be a LIVE bridge — the GUI's serial tab on
            # this port — and a script's stream used to kill it.
            run = os.urandom(6).hex()
            body = (
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                + _ps_open_port(baudrate, 15 if force else 3) +
                "$p.Encoding=[Text.Encoding]::UTF8;"
                # say so and stop if the port will not open — otherwise the loop
                # below just spins on a closed port and the stream looks empty
                "if(-not $p.IsOpen){$m='[serial] cannot open '+$dev+': '+$why.Message;"
                "if(" + _ps_busy("$why") + "){$m+=' (it is in use; force=True stops "
                "a stale TurboSSH reader of it first)'};"
                "[Console]::Out.WriteLine($m);[Console]::Out.Flush();exit 2};"
                "Set-Content -LiteralPath $pf -Value $PID;"
                "try{while(1){if($p.BytesToRead -gt 0){"
                "[Console]::Out.Write($p.ReadExisting());[Console]::Out.Flush()}"
                "else{sleep -m 30}}}"
                "finally{try{$p.Close()}catch{};" + _PS_DROP_OWN_PIDFILE + "}"
            )
            ps = self._win_serial_script(device, body, force=force, tracked=force)
            cmd = self._ps_command(_serial_ps_header(_port_key(device), run) + ps)
        else:
            dev = shlex.quote(device)
            if configure:
                cmd = (f"stty -F {dev} {int(baudrate)} raw -echo -echoe -echok "
                       f"2>/dev/null; cat {dev}")
            else:
                cmd = f"cat {dev}"
        try:
            return self.stream(cmd, on_line=on_line, on_match=on_match, match=match,
                               stop_on_match=stop_on_match, save_to=save_to,
                               clean=clean, timeout=timeout, stop_event=stop_event,
                               combine_stderr=False, safe=safe)
        finally:
            if run is not None:
                self._serial_stop_run(device, run)

    def _serial_stop_run_cmd(self, device: str, run: str) -> str:
        key = _port_key(device)
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$pf=Join-Path $env:TEMP turbossh_ser_{key}.pid;"
            "$own=[string](gc -LiteralPath $pf -TotalCount 1);"
            "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\"|"
            "?{$_.ProcessId -ne $PID -and $_.CommandLine -like '*__RUNTAG__*'}|%{"
            "Stop-Process -Id $_.ProcessId -Force;"
            "if($own.Trim() -eq [string]$_.ProcessId){"
            "Remove-Item -LiteralPath $pf -Force}};'stopped'"
        ).replace("__RUNTAG__", _ps_b64(_serial_ps_header(key, run)))
        return self._ps_command(ps)

    def _serial_stop_run(self, device: str, run: str) -> None:
        """Stop the Windows reader of one serial_stream run (found by the run id
        in its command line, never another holder of the port) and drop the PID
        file if it names that reader. Skipped when the link is gone: a forced
        open (``force=True``) frees the port then."""
        if not self.is_connected:
            return
        try:
            self._run(self._serial_stop_run_cmd(device, run), timeout=15,
                      check=False, get_pty=False, environment=None, encoding="utf-8")
        except Exception as exc:
            self._emit(logging.DEBUG, f"Stopping the serial reader failed: {exc}")

    # Bytes per serial_write command: their base64 travels inside the encoded
    # script (8/3 characters each), and the command must stay under cmd's limit.
    _SERIAL_WRITE_CHUNK = 1536

    def _serial_write_cmds(self, device: str, raw: bytes, baudrate: int) -> list:
        cmds = []
        step = self._SERIAL_WRITE_CHUNK
        for start in range(0, max(1, len(raw)), step):
            chunk = raw[start:start + step]
            # time for the bytes to leave the port (10 bits a byte) before Close
            drain = 200 + len(chunk) * 10000 // max(1, int(baudrate))
            ps = (
                "$ErrorActionPreference='Stop';"
                f"try{{$b=[Convert]::FromBase64String("
                f"'{base64.b64encode(chunk).decode('ascii')}');"
                f"$p=New-Object IO.Ports.SerialPort '{self._ps_sq(device)}',"
                f"{int(baudrate)};$p.WriteTimeout=5000;$p.Open();"
                f"try{{$p.Write($b,0,$b.Length);Start-Sleep -Milliseconds {drain}}}"
                "finally{$p.Close()}}"
                "catch{" + _PS_INNER +
                "[Console]::Error.WriteLine('serial write failed: '+$e.Message);exit 3}"
            )
            cmds.append(self._ps_command(ps))
        return cmds

    def serial_write(self, device: str, data: str, *, baudrate: int = 115200,
                     mode: str = "auto", newline: bool = True,
                     safe: Optional[bool] = None):
        """
        Write a line to a serial port on the remote host (over SSH/jump).
        Opens, writes, and closes the port (don't run while serial_stream holds
        it open on Windows - the port can't be shared).

        A port that can't be opened or written raises :class:`SSHCommandError`
        with the reason in its stderr (``safe=True``: a failed OperationResult);
        it used to report success with nothing sent. On Windows, data longer
        than a command line can carry is written in several steps, in order.
        """
        raw = data + ("\n" if newline else "")
        if self._is_windows_port(device, mode):
            # The bytes travel base64-encoded (no payload can break the quoting)
            # and are written as UTF-8 (SerialPort's default encoding is ASCII,
            # which turned every non-ASCII character into '?').
            cmds = self._serial_write_cmds(device, raw.encode("utf-8"), baudrate)
        else:
            dev = shlex.quote(device)
            # `> dev` on a missing device would create a plain file there instead
            cmds = [f"if [ -c {dev} ]; then printf %s {shlex.quote(raw)} > {dev}; "
                    f"else echo 'serial write failed: '{dev}' is not a serial "
                    f"device' >&2; exit 2; fi"]
        if len(cmds) == 1:
            return self.run(cmds[0], timeout=60, check=True, safe=safe)

        def _do():
            res = None
            for cmd in cmds:
                res = self.run(cmd, timeout=60, check=True, safe=False)
            return res
        return self._guard("serial_write", _do, safe=safe)

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

        ``force=True`` clears a stale holder of the port first, then retries:
        on Windows the tracked PID and any other TurboSSH serial PowerShell
        holding THIS port (recognised by the port tag its command line starts
        with, so readers of other ports and unrelated PowerShells are never
        touched); on Linux / QNX ``fuser -k`` on the device. Use it only after
        the user confirms taking a port that's already in use.
        """
        self._ensure_connected()
        if force and self._is_windows_port(device, mode):
            # The forced stop is serial_release(force=True), run first as a
            # command of its own: in one script with the bridge, the command
            # line was longer than cmd.exe takes (8,191), and every forced open
            # under a cmd default shell failed. The bridge then retries the
            # open for a few seconds while the port is let go.
            try:
                self._run(self._serial_release_cmd(device, mode, True), timeout=30,
                          check=False, get_pty=False, environment=None,
                          encoding="utf-8")
            except (SSHCommandError, SSHTimeoutError) as exc:
                self._emit(logging.WARNING, f"Stopping the holders of {device} "
                                            f"failed: {exc}")
        cmd = self._serial_bridge_cmd(device, baudrate, mode, force)
        to = self.config.connect_timeout
        chan = self.transport.open_session(timeout=to)
        try:
            # PTY -> stdin delivered interactively (input works). Wide so the
            # remote conpty doesn't hard-wrap the device's output.
            self._request(chan, to, chan.get_pty, term="xterm", width=250, height=50)
            self._request(chan, to, chan.exec_command, cmd)
        except BaseException:
            chan.close()
            raise
        return chan

    def _serial_bridge_cmd(self, device: str, baudrate: int, mode: str,
                           force: bool) -> str:
        if self._is_windows_port(device, mode):
            body = (
                "$u=[Text.Encoding]::UTF8;[Console]::OutputEncoding=$u;"
                + _ps_open_port(baudrate, 15 if force else 3) +
                "$p.WriteTimeout=500;"
                # BUSY only when another process holds the port (access denied,
                # or a driver's "in use" / sharing violation); a missing or
                # unplugged port says what is wrong instead
                "if(-not $p.IsOpen){[Console]::Out.Write($(if(" + _ps_busy("$why") +
                "){'BUSY:'+$dev}else{'[serial] cannot open '+$dev+': '+$why.Message}));"
                "[Console]::Out.Flush();exit 2};"
                "Set-Content -LiteralPath $pf -Value $PID;"
                # RAW console input: no line buffering, no echo, Ctrl-C as a byte
                "try{$t=Add-Type -Name TSio -Namespace TS -PassThru -MemberDefinition "
                "'[DllImport(\"kernel32\")]public static extern IntPtr GetStdHandle(int n);"
                "[DllImport(\"kernel32\")]public static extern bool GetConsoleMode(IntPtr h,out int m);"
                "[DllImport(\"kernel32\")]public static extern bool SetConsoleMode(IntPtr h,int m);';"
                "$h=$t::GetStdHandle(-10);$m=0;"
                "if($t::GetConsoleMode($h,[ref]$m)){$t::SetConsoleMode($h,$m -band -8)|Out-Null}}catch{};"
                "$out=[Console]::OpenStandardOutput();"
                "$in=[Console]::OpenStandardInput();$ib=New-Object byte[] 4096;"
                "$ar=$in.BeginRead($ib,0,4096,$null,$null);"
                "try{while(1){"
                "try{if($p.BytesToRead -gt 0){$s=$p.ReadExisting();"
                "if($s){$b=$u.GetBytes($s);"
                "$out.Write($b,0,$b.Length);$out.Flush()}}}catch{};"
                "if($ar.IsCompleted){try{$k=$in.EndRead($ar)}catch{$k=0};"
                "if($k -le 0){break};try{$p.Write($ib,0,$k)}catch{};"
                "$ar=$in.BeginRead($ib,0,4096,$null,$null)};"
                "sleep -m 5}}"
                "finally{try{$p.Close()}catch{};" + _PS_DROP_OWN_PIDFILE + "}"
            )
            # always clears our own tracked stale holder of this port (a forced
            # open has stopped every holder of ours first: see serial_bridge)
            ps = self._win_serial_script(device, body, force=False)
            return self._ps_command(_serial_ps_header(_port_key(device)) + ps)
        dev = shlex.quote(device)
        baud = int(baudrate)
        fk = f"fuser -k {dev} 2>/dev/null; " if force else ""
        # The PTY (needed for interactive input) starts out cooked: it echoed
        # every keystroke a second time, held input back until Enter, and made
        # Ctrl-C a SIGINT that killed the bridge. So stdin goes raw as well
        # (-isig: Ctrl-C is just byte 0x03 for the device) — but keeps the two
        # translations 2.0's cooked PTY did and terminals rely on: output
        # LF -> CR LF (opost onlcr; without it a device printing a bare LF
        # staircases) and input Enter, CR -> LF (icrnl). They are
        # set on their own, so an stty without one of them still goes raw.
        # QNX's stty has no -F; it configures the device given as stdin. The
        # two cats' PIDs are recorded (in a folder of the user's own) so
        # serial_release() stops exactly this bridge (it used to `fuser -k`
        # every process holding the device). The writer reads a copy of stdin
        # on fd 3: a background job's stdin is /dev/null in a non-interactive
        # shell. __drop removes the PID file only while it still names this
        # bridge.
        return (
            self._posix_pid_setup(device)
            + self._posix_kill_tracked() + fk +
            f"stty -F {dev} {baud} raw -echo 2>/dev/null || "
            f"stty {baud} raw -echo < {dev} 2>/dev/null; "
            "stty raw -echo -isig 2>/dev/null || stty raw -echo 2>/dev/null; "
            "stty icrnl opost onlcr 2>/dev/null; "
            "__drop() { [ \"$(cat \"$__pf\" 2>/dev/null)\" = \"$__r $__w\" ] "
            "&& rm -f \"$__pf\"; }; "
            "exec 3<&0; "
            f"cat {dev} & __r=$!; cat <&3 > {dev} & __w=$!; "
            "[ -n \"$__pf\" ] && echo \"$__r $__w\" > \"$__pf\"; "
            "trap 'kill $__r $__w 2>/dev/null; __drop; exit' HUP INT TERM; "
            "wait $__w; kill $__r 2>/dev/null; __drop"
        )

    def _serial_in_use_cmd(self, device: str) -> str:
        # BUSY: access denied, or a driver's "in use" / sharing violation
        # (some USB-serial drivers report a held port as an IOException)
        ps = (f"$p=New-Object IO.Ports.SerialPort '{self._ps_sq(device)}',9600;"
              "try{$p.Open();$p.Close();'FREE'}catch{" + _PS_INNER +
              "if(" + _ps_busy("$e") + "){'BUSY'}"
              "else{'UNAVAILABLE: '+$e.Message}}")
        return self._ps_command(ps)

    def serial_in_use(self, device: str, *, mode: str = "auto",
                      safe: Optional[bool] = None) -> bool:
        """Return True if *device* is currently in use (another process holds
        it). Used to ask the user before forcibly taking a port that's already
        busy. A missing or unplugged port is not "in use": opening it then
        reports the real reason."""
        if self._is_windows_port(device, mode):
            res = self.run(self._serial_in_use_cmd(device), timeout=15, safe=False)
        else:
            dev = shlex.quote(device)
            res = self.run(f"fuser {dev} >/dev/null 2>&1 && echo BUSY || echo FREE",
                           timeout=15, safe=False)
        text = getattr(res, "text", "") or ""
        return any(line.strip() == "BUSY" for line in text.splitlines())

    # What serial_release says when it had nothing of its own to stop.
    _NOTHING_TRACKED = ("nothing to release: no TurboSSH bridge of this port is "
                        "recorded on that machine, so nothing was stopped; "
                        "force=True stops whatever TurboSSH holds it")

    def _serial_release_cmd(self, device: str, mode: str, force: bool) -> str:
        if self._is_windows_port(device, mode):
            outcome = ("'released'" if force else
                       "if($n){'released'}else{[Console]::Error.WriteLine("
                       f"'{self._NOTHING_TRACKED}');exit 1}}")
            return self._ps_command(self._win_serial_script(
                device, "Remove-Item -LiteralPath $pf -Force;" + outcome, force=force))
        dev = shlex.quote(device)
        outcome = (f"fuser -k {dev} 2>/dev/null; echo released" if force else
                   "if [ \"$__n\" -gt 0 ]; then echo released; else "
                   f"echo {shlex.quote(self._NOTHING_TRACKED)} >&2; exit 1; fi")
        return self._posix_pid_setup(device) + self._posix_kill_tracked() + outcome

    def serial_release(self, device: str, *, mode: str = "auto", force: bool = False,
                       safe: Optional[bool] = None):
        """
        Forcibly release a serial port opened by :meth:`serial_bridge` — kill the
        bridge process (tracked in a per-port PID file) so the OS frees the port.
        Call this on close: relying on stdin-EOF alone can leave the remote
        PowerShell orphaned (Windows OpenSSH), keeping the port locked.

        Only TurboSSH's own bridge is stopped. ``force=True`` also stops any other
        holder: on Windows every TurboSSH serial reader of this port, on Linux /
        QNX every process holding the device (``fuser -k``).

        Returns the command's result. Without ``force``, when no bridge of this
        port was tracked (none left running, or one this machine has no record
        of), nothing is stopped and it says so: exit status 1 and a hint to use
        ``force=True`` in its stderr (it used to report "released"). That is an
        answer, not an error: it is logged as information (a GUI tab whose
        bridge ended by itself releases nothing on close).
        """
        return self._guard("serial_release", self._run,
                           self._serial_release_cmd(device, mode, force), timeout=15,
                           check=False, get_pty=False, environment=None,
                           encoding="utf-8", expected=(1,), safe=safe)

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
    def _remote_shell(self) -> str:
        """The remote Windows OpenSSH default shell: 'powershell' or 'cmd'
        (cached). bootstrap_sshd_via_winrm(set_powershell_default=True) makes it
        PowerShell, where cmd.exe syntax ("quoted exe" args, A & B) is a parse
        error. Anything not positively PowerShell counts as cmd."""
        if self._shell is None:
            try:
                r = self._run("echo $PSHOME", timeout=15, check=False, get_pty=False,
                              environment=None, encoding="utf-8")
            except SSHTimeoutError:
                return "cmd"                       # unknown: don't cache a guess
            out = (r.stdout or "").strip().lower()
            self._shell = ("powershell" if r.ok and "powershell" in out
                           and "$pshome" not in out else "cmd")
            self._emit(logging.DEBUG, f"Remote default shell: {self._shell}")
        return self._shell

    def _cmd_line(self, command: str) -> str:
        """A cmd.exe command line, made to run under the remote default shell.

        A line that starts with a quote gets a harmless first command: handed to
        ``cmd /c`` as is, cmd strips the first AND the last quote of such a line
        (``"exe" -i video="Cam"`` became ``exe" -i video="Cam``). Under PowerShell
        the line goes to ``cmd /s /c`` behind PowerShell's stop-parsing token
        (``--%``), which passes it on verbatim."""
        if command.startswith('"'):
            command = "ver >nul & " + command
        if self._remote_shell() == "powershell":
            return f'cmd /s /c --% "{command}"'
        return command

    def list_cameras(self, *, ffmpeg: str = "ffmpeg", safe: Optional[bool] = None):
        """List DirectShow video capture devices on the remote (Windows) host.
        ``ffmpeg`` is the remote ffmpeg path. Returns a list of camera names."""
        def _do():
            self._ensure_connected()
            exe = _cmd_safe_path(ffmpeg)
            # ffmpeg prints the device list to stderr; -list_devices then exits.
            cmd = self._cmd_line(f'"{exe}" -hide_banner -list_devices true '
                                 f'-f dshow -i dummy')
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

    def _webcam_kill_cmd(self, *, tokens=(), camera: Optional[str] = None,
                         marker: Optional[str] = None, report: bool = False) -> str:
        """An encoded PowerShell (works under either default shell) that stops
        the remote ffmpeg processes whose command line carries one of *tokens*
        (streams this handler started), names *camera* (whoever holds it), or
        whose command line / path contains *marker*. Silent unless *report*: it
        also runs ahead of an ffmpeg whose stdout is the video."""
        conds = [f"(TsHas $c '{self._ps_sq(t)}')" for t in tokens]
        if camera:
            needle = self._ps_sq('video="%s"' % camera)
            conds.append(f"(TsHas $c '{needle}')")
        if marker:
            m = self._ps_sq(marker)
            conds.append(f"(TsHas $c '{m}') -or (TsHas $x '{m}')")
        ps = _PS_FFMPEG_KILL.replace("__COND__", " -or ".join(conds) or "$false")
        if report:
            ps += ";'released '+$n"
        return f"powershell -NoProfile -EncodedCommand {self._ps_encode(ps)}"

    def _webcam_kill(self, **what):
        return self._run(self._webcam_kill_cmd(report=True, **what), timeout=30,
                         check=False, get_pty=False, environment=None,
                         encoding="utf-8")

    def webcam_channel(self, camera: str, *, ffmpeg: str = "ffmpeg",
                       width: int = 640, height: int = 480, fps: int = 15,
                       quality: int = 6, force: bool = False):
        """Stream the remote webcam as MJPEG and return a paramiko channel to read
        JPEG frames from with ``.recv()``; close it to stop. The returned channel
        carries a ``_prebuf`` (bytes already read while verifying) that the reader
        must replay first, and ``_ffmpeg_exec`` (the ffmpeg process channel, for
        stderr/teardown). ffmpeg's stderr is read continuously in the background
        (unread, it stalls the channel after ~2 MB and the picture freezes); its
        latest ~1.5 KB stay readable with ``recv_stderr`` on the ffmpeg channel.

        Transport is **self-selecting and verified**: we try a binary-clean SSH
        direct-tcpip TUNNEL first (ffmpeg serves MJPEG on a loopback TCP port), and
        only keep it if real JPEG frames actually arrive. If the tunnel stays
        silent we fall back to reading ffmpeg's STDOUT over the exec channel. Either
        way we return only a channel that is provably delivering video.

        Capture is in the camera's DEFAULT mode (raw — reliably opens over SSH; a
        specific `-vcodec` gave "Could not set video options" on some cams) and is
        only ever scaled DOWN (`min(iw,W)`, never up; upscaling + full-fps encoding
        overran the capture buffer).

        Every stream carries its own marker, so :meth:`webcam_release` stops only
        the streams of THIS handler (two Camera tabs on one machine used to kill
        each other's streams). An ffmpeg this handler left running is cleared
        first; ``force=True`` also stops any ffmpeg on that machine holding THIS
        camera (a stuck one from an earlier session).
        """
        self._ensure_connected()
        # dshow device names sit inside double quotes (`-i video="Name"`): a
        # quote would end the argument, and a | or %VAR% would break the line
        # under a PowerShell default shell (see _cmd_safe), so they are dropped.
        # The ffmpeg path keeps its %VAR% (both shells expand it, as in 2.0).
        camera = _cmd_safe(camera)
        exe = _cmd_safe_path(ffmpeg)
        to = self.config.connect_timeout
        token = f"turbossh_cam_{os.urandom(5).hex()}"
        # Clear a stale ffmpeg first: closing an SSH exec channel doesn't kill the
        # (no-PTY) child on Windows, so an earlier stream can still hold the camera.
        # The kill runs on the remote right before ffmpeg, in the same command.
        with self._cam_lock:
            earlier = list(self._cam_tokens)
            self._cam_tokens.add(token)
        first_kill = (self._webcam_kill_cmd(tokens=earlier,
                                            camera=camera if force else None)
                      if earlier or force else "")
        try:
            return self._webcam_open(camera, exe, token, width, fps, to, first_kill,
                                     earlier)
        except BaseException:
            # the last attempt's ffmpeg is still running: release the camera
            if self.is_connected:
                try:
                    self._webcam_kill(tokens=[token])
                except Exception:
                    pass
            self._forget_cam_tokens([token])
            raise

    def _forget_cam_tokens(self, tokens) -> None:
        with self._cam_lock:
            self._cam_tokens.difference_update(tokens)

    def _webcam_open(self, camera, exe, token, width, fps, to, first_kill, earlier):
        import random
        transport = self.transport
        # Pick a SHARP capture mode. The raw default is only 640x480 -> blurry when
        # shown larger. Most webcams expose higher-res MJPEG modes; enumerate them
        # and take the largest MJPEG at/below the requested width, then stream it
        # through UNCHANGED (`-c:v copy`): native resolution, no re-encode (so no
        # quality loss) and tiny CPU/bandwidth. Fall back to the raw default +
        # downscale + light re-encode if the camera has no MJPEG mode.
        chosen = None
        try:
            lst = self._run(self._cmd_line(f'"{exe}" -hide_banner -list_options true '
                                           f'-f dshow -i video="{camera}"'),
                            timeout=20, check=False, get_pty=False, environment=None,
                            encoding="utf-8")
            mj = [m for m in parse_dshow_modes((lst.stdout or "") + "\n" +
                                               (lst.stderr or "")) if "jpeg" in m["codec"]]
            if mj:
                le = [m for m in mj if m["w"] <= int(width)]
                chosen = (max(le, key=lambda m: m["w"] * m["h"]) if le
                          else min(mj, key=lambda m: m["w"] * m["h"]))
        except Exception:
            chosen = None
        # `-metadata comment=<token>` does nothing to an MJPEG stream; it is how
        # this stream's process is found again (webcam_release / the next start).
        if chosen:
            # use the mode's OWN framerate — dshow needs an exact (size, fps) match,
            # so a reduced -framerate on a 30-only mode fails "could not set options".
            cfps = chosen["fps"] or 30
            cap_base = (f'"{exe}" -hide_banner -loglevel error -f dshow '
                        f'-rtbufsize 100M -vcodec mjpeg '
                        f'-video_size {chosen["w"]}x{chosen["h"]} -framerate {cfps} '
                        f'-i video="{camera}" -an -c:v copy -metadata comment={token} '
                        f'-f mjpeg ')
        else:
            cap_base = (f'"{exe}" -hide_banner -loglevel error -f dshow -rtbufsize 200M '
                        f'-i video="{camera}" -an '
                        f"-vf \"scale='min(iw,{int(width)})':-2\" "
                        f'-r {int(fps)} -metadata comment={token} -f mjpeg -q:v 3 ')

        kill = [first_kill]

        def start(command):
            # cmd.exe syntax: the kill (an encoded PowerShell) runs first, then
            # ffmpeg — on a retry the previous attempt's ffmpeg, which outlives
            # its channel on Windows and would keep the camera busy
            prefix = f"{kill[0]} & " if kill[0] else ""
            kill[0] = self._webcam_kill_cmd(tokens=[token])
            chan = transport.open_session(timeout=to)
            try:
                self._request(chan, to, chan.exec_command,
                              self._cmd_line(prefix + command))
            except BaseException:
                chan.close()
                raise
            self._forget_cam_tokens(earlier)      # stopped by the first start
            _StderrTail(chan)
            return chan

        # --- Transport A: binary-clean direct-tcpip tunnel (verified) ---
        for _ in range(2):
            port = random.randint(20001, 60000)
            exec_chan = start(cap_base +
                              f'"tcp://127.0.0.1:{port}?listen=1&listen_timeout=30000"')
            data_chan = None
            bind_until = time.monotonic() + 15           # ~15s for ffmpeg to bind
            while time.monotonic() < bind_until:
                if exec_chan.exit_status_ready():
                    break
                try:
                    data_chan = transport.open_channel(
                        "direct-tcpip", ("127.0.0.1", port), ("127.0.0.1", 0),
                        timeout=max(1.0, min(to, bind_until - time.monotonic())))
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
            chan = start(cap_base + '-')                # MJPEG -> stdout
            prebuf = self._wait_for_jpeg(chan, 7.0)
            if prebuf is not None:
                chan._prebuf = prebuf                       # exec chan IS the data chan
                return chan
            try: chan.close()
            except Exception: pass

        raise SSHConnectionError(
            "The remote camera opens, but no video frames came through over SSH "
            "(tried both a forwarded tunnel and stdout). That usually means another "
            "app on that machine holds the camera, or Windows camera privacy is "
            "blocking it. Close other camera apps and check Settings -> Privacy -> "
            "Camera -> 'Let desktop apps access your camera'. If an earlier "
            "TurboSSH session left ffmpeg running there, "
            "webcam_channel(..., force=True) stops whatever holds this camera "
            "first (webcam_release(force=True): every TurboSSH camera ffmpeg).")

    def webcam_release(self, *, ffmpeg_marker: Optional[str] = None,
                       force: bool = False, safe: Optional[bool] = None):
        """Stop the remote ffmpeg of the streams :meth:`webcam_channel` started
        on THIS handler, so the camera is released cleanly — and nothing else:
        each Camera tab has its own handler, and releasing used to kill every
        TurboSSH camera stream on the machine (the other tabs' too).

        ``force=True`` (or an explicit ``ffmpeg_marker``) is the machine-wide
        clean-up: every ffmpeg whose path or command line contains the marker
        (default ``turbossh_cam``, the folder the GUI pushes ffmpeg to). A
        handler that started no stream (a fresh one, e.g. in a clean-up script)
        has nothing of its own to stop: it does nothing and says so — in the
        log, and in the result, whose exit status is 1 (``res.ok`` is False)
        with the hint to use ``force=True`` in its stderr. (It used to report
        "released 0" as a success, and a clean-up script took the camera for
        freed.)"""
        def _do():
            with self._cam_lock:
                tokens = list(self._cam_tokens)
            wide = force or ffmpeg_marker is not None
            if not tokens and not wide:
                hint = (f"webcam_release(): this handler started no camera stream "
                        f"on {self.config.host}, so nothing was stopped. To stop a "
                        f"camera ffmpeg an earlier session left running there, call "
                        f"webcam_release(force=True).")
                self._emit(logging.WARNING, hint)
                return CommandResult("webcam_release", 1, "released 0\n", hint, 0.0,
                                     host=self.config.host)
            res = self._webcam_kill(
                tokens=tokens,
                marker=(ffmpeg_marker or "turbossh_cam") if wide else None)
            if res.ok:
                self._forget_cam_tokens(tokens)
            return res
        return self._guard("webcam_release", _do, safe=safe)

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
            # SSHClient.invoke_shell would wait up to an hour for the channel and
            # without limit for the PTY / shell replies
            to = self.config.connect_timeout
            chan = self.transport.open_session(timeout=to)
            try:
                self._request(chan, to, chan.get_pty, term=term, width=width,
                              height=height)
                self._request(chan, to, chan.invoke_shell)
            except BaseException:
                chan.close()
                raise
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
        thread should open its own (:meth:`open_sftp`) and pass it to
        :meth:`push` / :meth:`pull` as ``sftp=``.

        Every request on it is bounded by ``config.sftp_timeout``. A target with
        no SFTP server raises :class:`SSHTransferError`.
        """
        for _ in range(3):
            self._ensure_connected()
            with self._conn_lock:
                cur, client = self._sftp, self._client
                if cur is not None:
                    chan = cur.get_channel()
                    if chan is not None and not chan.closed:
                        return cur
                    self._sftp = None
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            if client is None:
                continue
            # opened OUTSIDE the lock: it is several round trips, and holding the
            # lock blocked connect / disconnect (and every reconnect) meanwhile
            new = self._new_sftp(client)
            with self._conn_lock:
                if self._client is client and self._sftp is None:
                    self._sftp = new
                    return new
            _close_soon(new)          # another thread won, or the link was replaced
        raise SSHNotConnectedError(f"Not connected to {self.config.host}.")

    def open_sftp(self) -> paramiko.SFTPClient:
        """A NEW SFTP client of the caller's own (close it when done) — for a
        worker thread; same timeouts and errors as :meth:`sftp`."""
        return self._new_sftp(self.client)

    def _new_sftp(self, client: paramiko.SSHClient) -> paramiko.SFTPClient:
        t = client.get_transport()
        if t is None or not t.is_active():
            raise SSHNotConnectedError(f"Disconnected from {self.config.host}.")
        to = self.config.connect_timeout
        try:
            chan = t.open_session(timeout=to)
            try:
                self._request(chan, to, chan.invoke_subsystem, "sftp")
                # bounds every SFTP request, the version handshake below included
                # (Paramiko's default is to wait for ever on a silent link)
                chan.settimeout(self.config.sftp_timeout or None)
                return paramiko.SFTPClient(chan)
            except BaseException:
                chan.close()
                raise
        except SSHError:
            raise
        except (paramiko.SSHException, EOFError, OSError) as exc:
            raise SSHTransferError(
                f"Could not start SFTP on {self.config.host}: {exc or type(exc).__name__}"
                f" (is an SFTP server enabled on that machine?)") from exc

    # How many symlinked folders deep a recursive walk follows. A link loop is
    # normally caught by comparing real paths, but that relies on the server's
    # realpath resolving links; this bounds the walk on servers where it doesn't.
    _MAX_LINK_HOPS = 8

    # A request that timed out is not "no such file" (socket.timeout is an
    # IOError too): it is raised, so it fails the operation as what it is — and
    # the caller drops the client, which is out of step with the server.
    def _entry_kind(self, sftp, entry, path: str):
        """``(kind, is_link)`` for a listdir_attr entry: kind is 'dir' or 'file'
        after following a symlink (one extra stat), or None for a dangling link.
        Tolerates servers that send no st_mode, or one above 0xFFFF (QNX)."""
        mode = entry.st_mode
        is_link = _file_type(mode) == _S_IFLNK
        if is_link:
            try:
                mode = sftp.stat(path).st_mode
            except socket.timeout:
                raise
            except IOError:
                return None, True
        return ("dir" if _file_type(mode) == _S_IFDIR else "file"), is_link

    @staticmethod
    def _real_dir(sftp, path: str) -> str:
        try:
            return sftp.normalize(path)
        except socket.timeout:
            raise
        except IOError:
            return path

    @staticmethod
    def _rnorm(path: str) -> str:
        return path.replace("\\", "/")

    def _remote_is_dir(self, sftp, path: str) -> bool:
        try:
            return _file_type(sftp.stat(path).st_mode) == _S_IFDIR
        except socket.timeout:
            raise
        except IOError:
            return False

    def _remote_exists(self, sftp, path: str) -> bool:
        try:
            sftp.stat(path)
            return True
        except socket.timeout:
            raise
        except IOError:
            return False

    def _mkdir_p(self, sftp, remote_dir: str) -> bool:
        """Make *remote_dir* and its parents. True if the folder itself was made
        here (so it is empty)."""
        remote_dir = self._rnorm(remote_dir)
        if remote_dir in ("", "/", "."):
            return False
        is_abs = remote_dir.startswith("/")
        parts = [p for p in remote_dir.split("/") if p]
        current, made = "", False
        for part in parts:
            current = (f"/{part}" if is_abs else part) if current == "" \
                else f"{current}/{part}"
            made = False
            if not self._remote_exists(sftp, current):
                try:
                    sftp.mkdir(current)
                    made = True
                except socket.timeout:
                    raise
                except IOError:
                    pass                 # made meanwhile; a real problem shows next
        return made

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
        def _do():
            self._mkdir_p(self.sftp(), path)
        return self._guard("makedirs", _do, safe=safe)

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
    # Every file goes through turbossh._transfer, the one policy the file browser
    # and the remote editor use too: written under a temporary name next to its
    # target and renamed into place once complete (an interrupted push or pull
    # over an existing file used to destroy the good copy), keeping the old
    # file's mode and owner — and written in place, as 2.0 did, where replacing
    # would change what the file is (hard links, devices) or cannot be done.
    def push(self, local_path: str, remote_path: str, *, recursive: bool = False,
             make_dirs: bool = True, callback=None, sftp=None,
             safe: Optional[bool] = None):
        """Upload a file or directory (SFTP). Returns TransferResult.

        ``callback(bytes_done, bytes_total)`` is called per file as it moves;
        raising from it aborts the transfer. ``sftp`` lets a worker thread use
        its own SFTP client instead of the shared one (see :meth:`sftp`). A file
        that fails or is aborted part-way leaves the remote file as it was.

        An existing file is replaced with its permissions, owner and group. It
        is written in place instead (and the log says why) when it has more than
        one hard link, is not a regular file (``/dev/null``, a FIFO, a device),
        sits in a folder that is not writable, would lose its owner, or there is
        no room for a second copy. A symbolic link is followed: the file it
        points at is replaced."""
        return self._guard("push", self._push, local_path, remote_path,
                           recursive=recursive, make_dirs=make_dirs,
                           callback=callback, sftp=sftp, safe=safe)

    def _transfer_failed(self, exc, sftp, shared: bool, what: str):
        """The SSHTransferError for a failed push / pull. A request that timed out
        leaves the shared SFTP client mid-conversation, so it is dropped (the
        next call opens a fresh one)."""
        if shared and sftp is not None and isinstance(exc, socket.timeout):
            self._drop_shared_sftp(only=sftp)
        return SSHTransferError(f"Failed to {what}: {exc or type(exc).__name__}")

    def _push(self, local_path, remote_path, *, recursive, make_dirs, callback,
              sftp=None):
        local_path = os.path.expanduser(local_path)
        remote_path = self._rnorm(remote_path)
        if not os.path.exists(local_path):
            raise SSHTransferError(f"Local path does not exist: {local_path}")
        start = time.time()
        shared = sftp is None
        try:
            # inside the try: a target without an SFTP server, or a link that
            # dies here, is a transfer error like any other
            sftp = sftp or self.sftp()
            if os.path.isdir(local_path):
                if not recursive:
                    raise SSHTransferError(f"{local_path} is a directory; "
                                           f"pass recursive=True.")
                size, count = self._push_dir(sftp, local_path, remote_path, callback)
            else:
                existing = _transfer.UNKNOWN
                if make_dirs:
                    parent = posixpath.dirname(remote_path)
                    if parent and self._mkdir_p(sftp, parent):
                        existing = None              # a new, empty folder
                self._emit(logging.INFO, f"PUSH {local_path} -> {remote_path}")
                self._put_file(sftp, local_path, remote_path, callback, existing)
                size, count = os.path.getsize(local_path), 1
        except SSHError:
            raise
        except (IOError, OSError, EOFError, paramiko.SSHException) as exc:
            raise self._transfer_failed(
                exc, sftp, shared, f"push {local_path} -> {remote_path}") from exc
        return TransferResult(local_path, remote_path, "push", "sftp", size,
                              time.time() - start, count)

    @staticmethod
    def _remote_listing(sftp, folder: str):
        """{name: entry} of a remote folder, or None if it cannot be listed. One
        listing says what every file of a folder upload replaces (and its hard
        links); an lstat per file cost a round trip each."""
        try:
            return {e.filename: e for e in sftp.listdir_attr(folder)}
        except socket.timeout:
            raise
        except IOError:
            return None

    def _push_dir(self, sftp, local_dir, remote_dir, callback, there=_transfer.UNKNOWN):
        """Upload a folder. *there* is the listing of *remote_dir* ({} for a
        folder made by this push), looked up when not known."""
        if there is _transfer.UNKNOWN:
            there = {} if self._mkdir_p(sftp, remote_dir) else \
                self._remote_listing(sftp, remote_dir)
        total, count = 0, 0
        for entry in os.listdir(local_dir):
            lpath = os.path.join(local_dir, entry)
            rpath = posixpath.join(remote_dir, entry)
            known = there.get(entry) if there is not None else _transfer.UNKNOWN
            if os.path.isdir(lpath):
                sub = _transfer.UNKNOWN
                if known is None:                    # not there yet: make it
                    try:
                        sftp.mkdir(rpath)
                        sub = {}
                    except socket.timeout:
                        raise
                    except IOError:
                        pass
                elif (known is not _transfer.UNKNOWN
                      and _file_type(known.st_mode) == _S_IFDIR):
                    sub = self._remote_listing(sftp, rpath)
                s, c = self._push_dir(sftp, lpath, rpath, callback, sub)
                total += s
                count += c
            else:
                self._emit(logging.INFO, f"PUSH {lpath} -> {rpath}")
                self._put_file(sftp, lpath, rpath, callback, known)
                total += os.path.getsize(lpath)
                count += 1
        return total, count

    def _put_file(self, sftp, local, remote, callback,
                  existing=_transfer.UNKNOWN) -> None:
        how = _transfer.put_file(sftp, local, remote, callback=callback,
                                 existing=existing)
        if how:
            self._emit(logging.INFO, f"{remote} was written {how}.")

    def pull(self, remote_path: str, local_path: str, *, recursive: bool = False,
             make_dirs: bool = True, callback=None, sftp=None,
             safe: Optional[bool] = None):
        """Download a file or directory (SFTP). Returns TransferResult.

        ``callback`` and ``sftp`` work as in :meth:`push`. A file that fails or is
        aborted part-way leaves no partial file behind and an existing local file
        as it was. A recursive pull never writes outside *local_path*: remote
        names that aren't valid here (``a:b``, ``CON``, a trailing dot on
        Windows) are saved under a safe name, with a warning."""
        return self._guard("pull", self._pull, remote_path, local_path,
                           recursive=recursive, make_dirs=make_dirs,
                           callback=callback, sftp=sftp, safe=safe)

    @staticmethod
    def _sftp_get(sftp, remote: str, local: str, callback) -> None:
        _transfer.sftp_get(sftp, remote, local, callback)

    def _get_file(self, sftp, remote_path, local_path, callback) -> None:
        """Download to ``.<name>.<n>.tsp`` next to *local_path* and move it into
        place only when complete (keeping the old file's permissions): a failed
        or aborted download leaves an existing file untouched and no partial
        file behind. Through a symlink, the file it points at is replaced."""
        _, how = _transfer.get_file(sftp, remote_path, local_path, callback)
        if how:
            self._emit(logging.INFO, f"{local_path} was written {how}.")

    def _pull(self, remote_path, local_path, *, recursive, make_dirs, callback,
              sftp=None):
        remote_path = self._rnorm(remote_path)
        local_path = os.path.expanduser(local_path)
        start = time.time()
        shared = sftp is None
        try:
            sftp = sftp or self.sftp()
            # Verify the remote path exists FIRST, so a missing source never
            # leaves anything behind locally.
            if not self._remote_exists(sftp, remote_path):
                raise SSHTransferError(f"Remote path does not exist: {remote_path}")
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
        except SSHError:
            raise
        except (IOError, OSError, EOFError, paramiko.SSHException) as exc:
            raise self._transfer_failed(
                exc, sftp, shared, f"pull {remote_path} -> {local_path}") from exc
        return TransferResult(remote_path, local_path, "pull", "sftp", size,
                              time.time() - start, count)

    @staticmethod
    def _inside(root: str, path: str) -> bool:
        try:
            common = os.path.commonpath([root, os.path.abspath(path)])
        except ValueError:                  # another drive
            return False
        return os.path.normcase(common) == os.path.normcase(root)

    def _local_names(self, entries, remote_dir: str) -> dict:
        """{remote name: local name} for one folder listing. A name that isn't
        valid here gets a safe one (see _safe_local_name) that collides with no
        other name in the folder."""
        taken = {os.path.normcase(e.filename) for e in entries}
        out = {}
        for e in entries:
            name = _safe_local_name(e.filename)
            if name != e.filename:
                base, n = name, 1
                while os.path.normcase(name) in taken:
                    name, n = f"{base}~{n}", n + 1
                taken.add(os.path.normcase(name))
                self._emit(logging.WARNING,
                           f"Saving {posixpath.join(remote_dir, e.filename)!r} as "
                           f"{name!r}: that name is not valid on this machine.")
            out[e.filename] = name
        return out

    def _pull_dir(self, sftp, remote_dir, local_dir, callback, _seen=None,
                  _links=0, _root=None):
        seen = set() if _seen is None else _seen
        root = _root or os.path.abspath(local_dir)
        real = self._real_dir(sftp, remote_dir)
        if real in seen:                    # a symlink loop: already copied
            return 0, 0
        seen.add(real)
        os.makedirs(local_dir, exist_ok=True)
        total, count = 0, 0
        entries = sftp.listdir_attr(remote_dir)
        names = self._local_names(entries, remote_dir)
        for entry in entries:
            rpath = posixpath.join(remote_dir, entry.filename)
            lpath = os.path.join(local_dir, names[entry.filename])
            if not self._inside(root, lpath):     # never write outside the target
                self._emit(logging.WARNING,
                           f"Skipping {rpath!r}: it would be written outside {root}")
                continue
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
                s, c = self._pull_dir(sftp, rpath, lpath, callback, seen, hops, root)
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
