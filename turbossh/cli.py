"""
Standalone command-line entry point.

    python -m turbossh run   --host H --user U [--domain CORP] CMD...
    python -m turbossh push  --host H --user U LOCAL REMOTE [--recursive]
    python -m turbossh pull  --host H --user U REMOTE LOCAL [--recursive]
    python -m turbossh info  --host H --user U

Credentials (password never echoed, never stored in plaintext):
    --password           prompt interactively (hidden)
    --password-stdin     read it from standard input (scripts / CI)
    TURBOSSH_PASSWORD    environment variable, if neither flag is given
    --use-stored         read from the OS credential vault
    store-credential     save to the OS vault, then exit
    --key FILE           use a private key instead of a password

Exit codes: 0 success · 1 the operation failed · 2 bad usage / missing
prerequisite · 130 interrupted (Ctrl+C) · `run` returns the remote exit code.
"""

from __future__ import annotations

import os
import re
import sys
import json
import argparse
import subprocess

from . import __version__, paths
from .config import SSHConfig
from .credentials import CredentialStore, prompt_password, Secret
from .exceptions import SSHError
from .results import CommandResult, TransferResult

# NB: .core (and with it paramiko, ~0.5 s to import) is imported inside
# _connect(), not here — this module also hosts the `turbossh-gui` entry point,
# which must not pay for paramiko before the window is on screen.

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class _UsageError(Exception):
    """A problem with how the command was invoked (exit code 2)."""


def _ps_quote(value) -> str:
    """A PowerShell single-quoted literal (an embedded quote is doubled), so a
    path like C:\\Users\\O'Brien cannot break out of the string."""
    return "'" + str(value).replace("'", "''") + "'"


def _setup_script_path() -> str:
    """Absolute path to the bundled OpenSSH setup PowerShell script."""
    return os.path.join(os.path.dirname(__file__), "setup_openssh_server.ps1")


def sshd_result_file() -> str:
    """Where the setup script writes its result (ProgramData = readable by all
    users, so the GUI can poll it even though setup runs elevated)."""
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(base, "turbossh", "sshd-setup-result.txt")


def launch_setup_server(install_pip: bool = False, port: int = 22) -> bool:
    """
    Launch the bundled, OFFLINE OpenSSH-Server setup (for the GUI's "Set up SSH
    server" button). Triggers the UAC prompt and opens a visible window that
    shows progress and the final verification.

    The script + the OpenSSH ZIPs are first copied to a stable temp folder so
    the elevated process (and the install) survive even if the GUI — or, when
    frozen, its PyInstaller temp dir — goes away. Windows only.

    NB: we elevate *explicitly* here (Start-Process -Verb RunAs from a normal,
    interactive powershell) rather than relying on the script's own self-elevation
    via a detached process — a detached/console-less process can't display the
    UAC consent UI, which is why "nothing happened" before.
    """
    if os.name != "nt":
        return False
    import shutil
    import tempfile
    src_script = _setup_script_path()
    src_dir = os.path.dirname(src_script)
    src_openssh = os.path.join(src_dir, "openssh")
    try:
        # A fresh, unpredictable folder per run: the script in it is about to be
        # executed elevated, so it must not sit at a fixed, guessable path that
        # something else could have populated first. Older stages are swept.
        tmp = tempfile.gettempdir()
        for old in os.listdir(tmp):
            if old.startswith("turbossh-sshd-setup"):
                shutil.rmtree(os.path.join(tmp, old), ignore_errors=True)
        stage = tempfile.mkdtemp(prefix="turbossh-sshd-setup-")
        dst_script = os.path.join(stage, "setup_openssh_server.ps1")
        shutil.copy2(src_script, dst_script)
        if os.path.isdir(src_openssh):
            dst_openssh = os.path.join(stage, "openssh")
            if os.path.isdir(dst_openssh):
                shutil.rmtree(dst_openssh, ignore_errors=True)
            shutil.copytree(src_openssh, dst_openssh)
    except Exception:
        return False

    if not os.path.exists(dst_script):
        return False

    # Inner args for the ELEVATED powershell that runs the setup. No -NoExit:
    # the script closes its own window on success and pauses only on failure,
    # and writes a result file the GUI polls.
    inner = ('-NoProfile -ExecutionPolicy Bypass -File '
             f'"{dst_script}" -Port {int(port)}')
    if install_pip:
        inner += " -InstallPip"
    # Write a tiny launcher .ps1 that fires the UAC prompt — running it via
    # `-File` avoids all the Windows quoting pitfalls of passing a complex
    # `-Command` string through subprocess.
    elevate_ps = os.path.join(stage, "elevate.ps1")
    try:
        with open(elevate_ps, "w", encoding="utf-8") as fh:
            fh.write(
                "$ErrorActionPreference='Stop'\n"
                "Start-Process -FilePath 'powershell.exe' -Verb RunAs "
                f"-ArgumentList {_ps_quote(inner)}\n")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", elevate_ps],
            creationflags=_NO_WINDOW)
        return True
    except Exception:
        return False


DOCS_URL = "https://pypi.org/project/turbossh/"


def open_docs(argv=None) -> int:
    """Console entry point (`turbossh-docs`): open the rendered docs in a
    browser, falling back to the README bundled in the package."""
    import webbrowser
    try:
        if webbrowser.open(DOCS_URL):
            print(f"Opened docs: {DOCS_URL}")
            return 0
    except Exception:
        pass
    readme = os.path.join(os.path.dirname(__file__), "README.md")
    if os.path.exists(readme):
        from pathlib import Path
        webbrowser.open(Path(readme).resolve().as_uri())
        print(f"Opened bundled README: {readme}")
    else:
        print(f"Docs: {DOCS_URL}")
    return 0


def _gui_exe_path() -> str:
    return os.path.join(os.path.dirname(__file__), "bin", "turbossh-gui.exe")


def _store_rdp_credential(host: str, login: str, password: str) -> bool:
    """Store a TERMSRV/<host> generic credential via the Win32 CredWrite API, so
    the password is NOT passed on a command line (as `cmdkey /pass:` would be,
    exposing it to any process listing). Persists for the session only."""
    import ctypes
    from ctypes import wintypes

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_SESSION = 1

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    blob = password.encode("utf-16-le")
    buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL()
    cred.Flags = 0
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = f"TERMSRV/{host}"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = CRED_PERSIST_SESSION
    cred.UserName = login
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    return bool(advapi.CredWriteW(ctypes.byref(cred), 0))


def launch_rdp(host: str, user: str = None, domain: str = None,
               password: str = None) -> bool:
    """Open the native Windows Remote Desktop client (mstsc) for *host*,
    pre-seeding credentials so it doesn't prompt. Windows only. The password is
    stored via the CredWrite API rather than on a command line."""
    if os.name != "nt":
        return False
    try:
        if user and password:
            full = f"{domain}\\{user}" if domain else user
            try:
                _store_rdp_credential(host, full, password)
            except Exception:
                pass                    # mstsc will just prompt for the password
        subprocess.Popen(["mstsc", f"/v:{host}"])
        return True
    except Exception:
        return False


def _icon_path() -> str:
    return os.path.join(os.path.dirname(__file__), "assets", "icon.ico")


def _known_folder(csidl: int, fallback: str) -> str:
    """A Windows shell folder by CSIDL — its REAL location. OneDrive's "known
    folder move" relocates Desktop under the OneDrive folder, so ``~/Desktop``
    is simply wrong on many corporate machines."""
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 \
                and buf.value:
            return buf.value
    except Exception:
        pass
    return fallback


def shortcut_paths(name: str = "TurboSSH") -> list:
    """Where the Desktop and Start-Menu shortcuts live for this user."""
    desktop = _known_folder(0x0010, os.path.join(os.path.expanduser("~"), "Desktop"))
    programs = _known_folder(0x0002, os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu",
        "Programs"))
    return [os.path.join(desktop, f"{name}.lnk"),
            os.path.join(programs, f"{name}.lnk")]


def create_desktop_shortcut(name: str = "TurboSSH") -> bool:
    """
    Create shortcuts to the GUI on the **Desktop and the Start Menu** (Windows
    only), each with the bundled automotive icon. Points at the prebuilt exe if
    present, else at `pythonw -m turbossh.gui`. Idempotent — safe to call on
    every launch. Uses PowerShell's WScript.Shell (no extra dependency).
    """
    if os.name != "nt":
        return False
    import importlib.util
    exe = _gui_exe_path()
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    frozen = bool(getattr(sys, "frozen", False))
    have_pyqt = importlib.util.find_spec("PyQt5") is not None
    if (not frozen) and os.path.exists(pyw) and have_pyqt:
        # FAST path: run from source with the installed PyQt5 — avoids the bundled
        # onefile exe self-extracting ~75 MB on every launch (slow startup).
        target, args, workdir = pyw, "-m turbossh.gui", os.path.dirname(pyw)
    elif os.path.exists(exe):
        target, args, workdir = exe, "", os.path.dirname(exe)
    else:
        target = pyw if os.path.exists(pyw) else sys.executable
        args = "-m turbossh.gui"
        workdir = os.path.dirname(target)
    icon = _icon_path()
    icon_line = (f"  $lnk.IconLocation = {_ps_quote(icon + ',0')};\n"
                 if os.path.exists(icon) else "")
    args_line = f"  $lnk.Arguments = {_ps_quote(args)};\n" if args else ""
    # Desktop + Start-Menu\Programs, both in one PowerShell pass.
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;\n"
        "$dirs = @([Environment]::GetFolderPath('Desktop'),"
        " [Environment]::GetFolderPath('Programs'));\n"
        "foreach ($d in $dirs) {\n"
        "  if (-not (Test-Path $d)) { continue }\n"
        f"  $p = [IO.Path]::Combine($d, {_ps_quote(name + '.lnk')});\n"
        "  $lnk = $ws.CreateShortcut($p);\n"
        f"  $lnk.TargetPath = {_ps_quote(target)};\n"
        f"{args_line}"
        f"  $lnk.WorkingDirectory = {_ps_quote(workdir)};\n"
        f"{icon_line}"
        "  $lnk.Description = 'TurboSSH - SSH / Serial / SFTP toolkit';\n"
        "  $lnk.Save();\n"
        "}"
    )
    try:
        # CREATE_NO_WINDOW: launched from pythonw / the windowed exe there is no
        # console to inherit, so PowerShell would otherwise flash one open
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       check=True, capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
        return True
    except Exception:
        return False


def ensure_shortcuts(name: str = "TurboSSH") -> bool:
    """Create the Desktop / Start-Menu shortcuts if they're missing. Cheap to
    call on every launch (only shells out to PowerShell when one is absent)."""
    if os.name != "nt":
        return False
    try:
        if all(os.path.exists(p) for p in shortcut_paths(name)):
            return True
    except Exception:
        pass
    return create_desktop_shortcut(name)


def create_shortcut(argv=None) -> int:
    """Console entry point (`turbossh-shortcut`)."""
    if os.name != "nt":
        print("Shortcut creation is Windows-only.", file=sys.stderr)
        return 2
    if create_desktop_shortcut():
        print("Created 'TurboSSH' shortcuts on your Desktop and Start Menu.")
        return 0
    print("Could not create the shortcut.", file=sys.stderr)
    return 1


def launch_gui(argv=None) -> int:
    """
    Console entry point (`turbossh-gui`): launch the PyQt5 application.

    Runs from SOURCE when PyQt5 is importable — that's instant. Only falls back to
    the bundled onefile exe when PyQt5 isn't available (e.g. Windows ARM64): that
    exe self-extracts ~75 MB to a temp dir on every launch, which is what made
    startup slow even though PyQt5 was installed.
    """
    try:
        import PyQt5  # noqa: F401  (just probing availability)
        have_qt = True
    except ImportError:
        have_qt = False
    if have_qt:
        # outside the try: a missing module INSIDE the GUI is a real error to
        # show, not a reason to fall through to "the GUI needs PyQt5"
        from .gui.app import main as gui_main
        return gui_main()
    exe = _gui_exe_path()
    if os.name == "nt" and os.path.exists(exe):
        args = list(argv) if argv is not None else sys.argv[1:]
        return subprocess.call([exe] + args)
    print("The GUI needs PyQt5 (or the bundled Windows exe). On a platform "
          "with PyQt5 wheels: pip install \"turbossh[gui]\".", file=sys.stderr)
    return 1


def setup_server_main(argv=None) -> int:
    """
    Console entry point (`turbossh-setup`): run the bundled PowerShell script
    that installs & starts OpenSSH Server, opens the firewall, and optionally
    pip-installs the package. Self-elevates to Administrator. Windows only.

    Any extra args are forwarded to the script, e.g.:
        turbossh-setup --InstallPip --Port 22
    """
    if os.name != "nt":
        print("turbossh-setup only runs on Windows (it sets up OpenSSH Server).",
              file=sys.stderr)
        return 2
    script = _setup_script_path()
    if not os.path.exists(script):
        print(f"Bundled setup script not found: {script}", file=sys.stderr)
        return 1
    extra = list(argv) if argv is not None else sys.argv[1:]
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", script] + extra
    print("Launching OpenSSH Server setup (will prompt for Administrator)...")
    return subprocess.call(cmd)


def _resolve_ffmpeg(explicit: str | None = None) -> str | None:
    """Find a local ffmpeg for the camera CLI: an explicit path, one on PATH, or
    the one the GUI cached under ~/.turbossh/ffmpeg. No auto-download in the CLI."""
    import shutil
    if explicit and os.path.exists(explicit):
        return explicit
    onpath = shutil.which("ffmpeg")
    if onpath:
        return onpath
    cached = paths.state_path("ffmpeg", "ffmpeg.exe")
    return cached if os.path.exists(cached) else None


def _add_conn_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    """The options every command that connects takes. ``required`` only records
    whether a target is mandatory (camera-* work locally without one); the check
    itself is in _build_config, because --session can supply the host."""
    g = p.add_argument_group("connection")
    g.add_argument("-s", "--session", default=None, metavar="NAME",
                   help="use a session saved in the GUI: its host, user, key, jump "
                        "host and stored passwords (flags given here override it)")
    g.add_argument("--host", default=None, help="host name or address")
    g.add_argument("--port", type=int, default=None, help="SSH port (default 22)")
    g.add_argument("--user", default=None, help="login user")
    g.add_argument("--domain", default=None, help="e.g. CORP for CORP\\user logins")
    g.add_argument("--key", default=None, help="private key file")
    g.add_argument("--password", action="store_true",
                   help="prompt for password (hidden input)")
    g.add_argument("--password-stdin", action="store_true",
                   help="read the password from standard input (for scripts; "
                        "TURBOSSH_PASSWORD in the environment also works)")
    g.add_argument("--use-stored", action="store_true",
                   help="read password from the OS credential vault")
    g.add_argument("--service", default="turbossh",
                   help="credential-vault namespace")
    g.add_argument("--passwordless", action="store_true",
                   help="keys / agent only; never send a password")
    g.add_argument("--jump-host", default=None,
                   help="connect through this jump host (bastion); its password "
                        "comes from TURBOSSH_JUMP_PASSWORD or the saved session")
    g.add_argument("--jump-user", default=None, help="user on the jump host")
    g.add_argument("--jump-port", type=int, default=None,
                   help="SSH port of the jump host (default 22)")
    g.add_argument("--host-key-policy", default=None,
                   choices=("auto", "reject", "warn", "ignore"),
                   help="auto: accept new keys, reject changed ones (default); "
                        "reject: known hosts only; ignore: lab devices")
    g.add_argument("--legacy", action="store_true",
                   help="also offer old key-exchange / cipher algorithms (old ECUs)")
    g.add_argument("--timeout", type=float, default=None,
                   help="seconds a remote command may run")
    g.add_argument("--no-fast-auth", action="store_true",
                   help="probe keys / agent even when a password is given")
    g.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.set_defaults(_needs_target=required)


def _vault_key(login_user: str, host: str | None) -> str:
    """Vault entry name. Host-specific, because `root` on two machines is two
    different passwords."""
    return f"{login_user}@{host}" if host else login_user


def _resolve_password(args, login_user: str, host: str | None) -> Secret | None:
    if args.use_stored:
        store = CredentialStore(args.service)
        # per-host entry first, then the legacy user-only entry
        sec = store.get(_vault_key(login_user, host)) or store.get(login_user)
        if sec is None:
            raise _UsageError(
                f"No stored credential for {login_user!r} on {host!r} in "
                f"service {args.service!r}. Save one with: turbossh "
                f"store-credential --user {login_user} --host {host}")
        return sec
    if args.password:
        return prompt_password(f"Password for {login_user}: ")
    if getattr(args, "password_stdin", False):
        return Secret(sys.stdin.readline().rstrip("\r\n"))
    env = os.environ.get("TURBOSSH_PASSWORD")
    if env is not None:
        return Secret(env)
    return None


def _saved_session(name: str) -> dict:
    """A session saved in the GUI, by name."""
    from .gui.sessions import SessionStore      # no Qt in there
    session = SessionStore().get(name)
    if session is None:
        raise _UsageError(f"No saved session named {name!r}. "
                          f"`turbossh sessions` lists them.")
    if session.get("type") == "serial" and not session.get("via_ssh"):
        raise _UsageError(f"{name!r} is a local serial session, not an SSH one.")
    return session


def _connect(args):
    """An SSHHandler for the parsed connection arguments. Imports paramiko only
    now, so commands that need no connection start instantly."""
    from .core import SSHHandler
    config = _build_config(args)
    args.host = config.host             # so messages can name it with --session
    return SSHHandler(config)


def _build_config(args) -> SSHConfig:
    """SSHConfig from the flags, on top of the saved session if one is named."""
    base, name = {}, getattr(args, "session", None)
    if name:
        base = _saved_session(name)
    host = args.host or base.get("host")
    if not host:
        raise _UsageError("no target: give --host (or --session NAME).")
    user = args.user or base.get("user") or None
    domain = args.domain or base.get("domain") or None
    login_user = f"{domain}\\{user}" if domain else user
    password = _resolve_password(args, login_user, host)
    jump_password = os.environ.get("TURBOSSH_JUMP_PASSWORD") or None
    if name:
        from .gui.sessions import SessionStore
        if password is None and not args.passwordless:
            stored = SessionStore.password(name)
            password = Secret(stored) if stored else None
        jump_password = jump_password or SessionStore.jump_password(name) or None
    policy = args.host_key_policy or (
        ("ignore" if base.get("ignore_hostkey", True) else "auto") if name else "auto")
    legacy = bool(args.legacy or base.get("legacy"))
    jump = None
    jump_host = args.jump_host or (base.get("jhost") if base.get("use_jump") else None)
    if jump_host:
        jump = SSHConfig(
            host=jump_host, port=args.jump_port or base.get("jport") or 22,
            username=args.jump_user or base.get("juser") or None,
            domain=base.get("jdomain") or None, password=jump_password,
            host_key_policy=policy, enable_legacy_algorithms=legacy)
    return SSHConfig(
        host=host, port=args.port or base.get("port") or 22, username=user,
        domain=domain, password=password,
        key_filename=args.key or base.get("key_file") or None,
        command_timeout=args.timeout, fast_auth=not args.no_fast_auth,
        passwordless=args.passwordless, jump_host=jump, host_key_policy=policy,
        enable_legacy_algorithms=legacy,
    )


def _output(args, obj) -> None:
    if args.json:
        if isinstance(obj, (CommandResult, TransferResult)):
            print(json.dumps(obj.as_dict(), default=str, indent=2))
        else:
            print(json.dumps(obj, default=str, indent=2))
    else:
        print(obj)


def main(argv=None) -> int:
    """CLI entry point. Never ends in a traceback for an expected failure."""
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except _UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except SSHError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, re.error) as exc:
        # a bad --match regex, an unwritable --save path, a local file error …
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """The whole command line. CLI.md documents exactly this (a test compares
    the two), so a new command or flag belongs in both."""
    parser = argparse.ArgumentParser(
        prog="turbossh", description="Extensive SSH/SFTP/SCP handler CLI",
        epilog="Exit codes: 0 ok, 1 failed, 2 usage, 130 interrupted; `run` "
               "returns the remote command's exit code.")
    parser.add_argument("-V", "--version", action="version",
                        version=f"turbossh {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # store-credential (no connection)
    sc = sub.add_parser("store-credential", help="save a password to the OS vault")
    sc.add_argument("--user", required=True)
    sc.add_argument("--domain", default=None)
    sc.add_argument("--host", default=None,
                    help="store it for this host only (recommended); without it "
                         "the entry is shared by every host for that user")
    sc.add_argument("--service", default="turbossh")

    p_run = sub.add_parser(
        "run", help="execute a remote command (arguments are joined with "
                    "spaces, as ssh does - quote the whole command to keep "
                    "its spacing)")
    _add_conn_args(p_run)
    p_run.add_argument("command", nargs=argparse.REMAINDER)

    p_push = sub.add_parser("push", help="upload a file/dir (SFTP)")
    _add_conn_args(p_push)
    p_push.add_argument("local")
    p_push.add_argument("remote")
    p_push.add_argument("--recursive", action="store_true")

    p_pull = sub.add_parser("pull", help="download a file/dir (SFTP)")
    _add_conn_args(p_pull)
    p_pull.add_argument("remote")
    p_pull.add_argument("local")
    p_pull.add_argument("--recursive", action="store_true")

    p_info = sub.add_parser("info", help="connect and report remote OS")
    _add_conn_args(p_info)

    p_stream = sub.add_parser("stream",
                              help="run a continuous command (tail -f / slog2info) "
                                   "and print lines live, with optional match + save")
    _add_conn_args(p_stream)
    p_stream.add_argument("command", nargs=argparse.REMAINDER)
    p_stream.add_argument("--match", default=None, help="regex to flag matching lines")
    p_stream.add_argument("--save", default=None, help="tee output to this local file")
    p_stream.add_argument("--stop-on-match", action="store_true")

    p_ls = sub.add_parser("list-serial", help="list available serial/COM ports")
    p_ls.add_argument("--json", action="store_true",
                      help="emit machine-readable JSON")

    p_ser = sub.add_parser("serial-monitor",
                           help="monitor a serial/COM port live, with match + save")
    p_ser.add_argument("--port", required=True, help="e.g. COM5 or /dev/ttyUSB0")
    p_ser.add_argument("--baud", type=int, default=115200)
    p_ser.add_argument("--match", default=None, help="regex to flag matching lines")
    p_ser.add_argument("--save", default=None, help="tee output to this local file")
    p_ser.add_argument("--stop-on-match", action="store_true")
    p_ser.add_argument("--timeout", type=float, default=None)

    # --- monitor a serial port on the REMOTE host (over SSH / jump) ---
    p_rser = sub.add_parser("serial-ssh",
                            help="monitor a serial port on the REMOTE host over SSH")
    _add_conn_args(p_rser)
    p_rser.add_argument("--device", required=True,
                        help="remote port, e.g. COM4 or /dev/ser1")
    p_rser.add_argument("--baud", type=int, default=115200)
    p_rser.add_argument("--match", default=None, help="regex to flag matching lines")
    p_rser.add_argument("--save", default=None, help="tee output to this local file")
    p_rser.add_argument("--stop-on-match", action="store_true")
    p_rser.add_argument("--send", default=None,
                        help="write this line to the port before monitoring")

    # --- scan serial ports on the REMOTE host ---
    p_scan = sub.add_parser("scan-ports",
                            help="list serial ports on the REMOTE host (over SSH)")
    _add_conn_args(p_scan)

    # --- install OpenSSH Server on a REMOTE Windows host over WinRM (offline) ---
    p_inst = sub.add_parser(
        "install-ssh-remote",
        help="install OpenSSH Server on a remote Windows host over WinRM (offline)")
    p_inst.add_argument("--host", required=True)
    p_inst.add_argument("--user", required=True, help="a local-admin user on the target")
    p_inst.add_argument("--domain", default=None)
    p_inst.add_argument("--ssh-port", type=int, default=22)
    p_inst.add_argument("--winrm-port", type=int, default=5985)

    # --- local port forward (ssh -L) through this connection / jump ---
    p_fwd = sub.add_parser("forward",
                           help="local port-forward (ssh -L) through this connection")
    _add_conn_args(p_fwd)
    p_fwd.add_argument("--local-port", type=int, required=True,
                       help="port to open on this machine")
    p_fwd.add_argument("--to-host", required=True,
                       help="host reachable from the SSH server")
    p_fwd.add_argument("--to-port", type=int, required=True)

    # --- camera: list / grab (local, or --host for a remote Windows machine) ---
    p_caml = sub.add_parser("camera-list",
                            help="list cameras on THIS machine, or on --host (remote)")
    _add_conn_args(p_caml, required=False)
    p_caml.add_argument("--ffmpeg", default=None, help="local ffmpeg path (else PATH/cache)")
    p_caml.add_argument("--remote-ffmpeg", default="ffmpeg",
                        help="ffmpeg path on the remote host (default: on its PATH)")

    p_camg = sub.add_parser("camera-grab",
                            help="save a snapshot (.jpg) or short clip (.mp4 with "
                                 "--seconds) from a camera (local or --host)")
    _add_conn_args(p_camg, required=False)
    p_camg.add_argument("--camera", required=True, help="camera name (see camera-list)")
    p_camg.add_argument("--out", required=True, help="output file (.jpg, or .mp4 with --seconds)")
    p_camg.add_argument("--seconds", type=float, default=0.0,
                        help="record this many seconds (0 = single snapshot)")
    p_camg.add_argument("--width", type=int, default=1280)
    p_camg.add_argument("--fps", type=int, default=25)
    p_camg.add_argument("--force", action="store_true",
                        help="first kill any stale ffmpeg holding the camera on "
                             "the remote host (use if the grab says the camera is "
                             "in use / no frames)")
    p_camg.add_argument("--ffmpeg", default=None, help="local ffmpeg path (else PATH/cache)")
    p_camg.add_argument("--remote-ffmpeg", default="ffmpeg",
                        help="ffmpeg path on the remote host")

    p_setup = sub.add_parser("setup-server",
                             help="install & start OpenSSH Server on THIS Windows "
                                  "machine (self-elevates to Administrator)")
    p_setup.add_argument("--install-pip", action="store_true",
                         help="also pip-install turbossh[winrm]")
    p_setup.add_argument("--port", type=int, default=22)

    # --- more remote commands ---
    p_sudo = sub.add_parser("sudo", help="execute a remote command through sudo "
                                         "(the password is sent only if sudo asks)")
    _add_conn_args(p_sudo)
    p_sudo.add_argument("--pty", action="store_true",
                        help="allocate a terminal (hosts whose sudoers has requiretty)")
    p_sudo.add_argument("command", nargs=argparse.REMAINDER)

    p_rls = sub.add_parser("ls", help="list a remote folder (SFTP)")
    _add_conn_args(p_rls)
    p_rls.add_argument("path", nargs="?", default=".")

    p_cid = sub.add_parser("copy-id", help="install a public key on the remote "
                                           "host for password-less logins")
    _add_conn_args(p_cid)
    p_cid.add_argument("--public-key", default=None, metavar="FILE",
                       help="the .pub file (default: ~/.ssh/id_ed25519.pub, "
                            "then ~/.ssh/id_rsa.pub)")

    # --- no connection needed ---
    p_diag = sub.add_parser("diagnose", help="check whether a host's SSH port is "
                                             "reachable, without logging in")
    p_diag.add_argument("--host", required=True)
    p_diag.add_argument("--port", type=int, default=22)
    p_diag.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")

    p_sess = sub.add_parser("sessions", help="list the sessions saved in the GUI "
                                             "(usable with --session NAME)")
    p_sess.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")

    p_key = sub.add_parser("keygen", help="generate an RSA key pair (FILE and FILE.pub)")
    p_key.add_argument("path", help="private-key file to write")
    p_key.add_argument("--bits", type=int, default=3072)
    p_key.add_argument("--comment", default="turbossh")
    p_key.add_argument("--passphrase-stdin", action="store_true",
                       help="read a passphrase for the key from standard input")
    p_key.add_argument("--force", action="store_true",
                       help="overwrite an existing key file")

    p_upd = sub.add_parser("check-update", help="compare the installed version "
                                                "with the latest on PyPI")
    p_upd.add_argument("--json", action="store_true",
                       help="emit machine-readable JSON")

    sub.add_parser("gui", help="open the graphical app (same as turbossh-gui)")
    sub.add_parser("docs", help="open the documentation (same as turbossh-docs)")
    sub.add_parser("shortcut", help="create Desktop / Start-Menu shortcuts "
                                    "(same as turbossh-shortcut; Windows)")
    sub.add_parser("version", help="print the version (same as --version)")
    return parser


def _main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "version":
        print(f"turbossh {__version__}")
        return 0
    if args.cmd == "gui":
        return launch_gui([])
    if args.cmd == "docs":
        return open_docs([])
    if args.cmd == "shortcut":
        return create_shortcut([])

    if args.cmd == "sessions":
        from .gui.sessions import SessionStore
        rows = [{"name": s.get("name"), "type": s.get("type", "ssh"),
                 "host": s.get("host") or "", "port": s.get("port", 22),
                 "user": s.get("user") or "",
                 "jump_host": s.get("jhost") if s.get("use_jump") else "",
                 "device": s.get("device") if s.get("type") == "serial" else ""}
                for s in SessionStore().sessions]
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("No saved sessions. Create one in the GUI (turbossh gui).",
                  file=sys.stderr)
        else:
            for r in rows:
                target = (f"{r['user']}@{r['host']}:{r['port']}" if r["host"]
                          else r["device"])
                via = f"  via {r['jump_host']}" if r["jump_host"] else ""
                print(f"{r['name']:24} {r['type']:7} {target}{via}")
        return 0

    if args.cmd == "diagnose":
        from .core import SSHHandler
        report = SSHHandler(SSHConfig(host=args.host, port=args.port),
                            quiet=True).diagnose()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(report["verdict"])
        return 0 if report["reachable"] else 1

    if args.cmd == "keygen":
        target = os.path.expanduser(args.path)
        if os.path.exists(target) and not args.force:
            raise _UsageError(f"{target} already exists (pass --force to replace it).")
        passphrase = (sys.stdin.readline().rstrip("\r\n") or None) \
            if args.passphrase_stdin else None
        from .tunnel import generate_keypair
        public = generate_keypair(target, bits=args.bits, passphrase=passphrase,
                                  comment=args.comment)
        print(f"Wrote {target} and {target}.pub", file=sys.stderr)
        print(public)
        return 0

    if args.cmd == "check-update":
        from . import update
        latest = update.latest_version()
        newer = update.is_newer(latest)
        if args.json:
            print(json.dumps({"installed": __version__, "latest": latest,
                              "update_available": newer}, indent=2))
        elif latest is None:
            print("Could not reach PyPI to check for updates.", file=sys.stderr)
        elif newer:
            print(f"turbossh {latest} is available (installed: {__version__}). "
                  f"Update with: pip install -U turbossh")
        else:
            print(f"turbossh {__version__} is the latest version.")
        return 1 if latest is None else 0

    # setup-server takes no connection args and runs the bundled PowerShell script
    if args.cmd == "setup-server":
        forward = ["-Port", str(args.port)]
        if args.install_pip:
            forward.append("-InstallPip")
        return setup_server_main(forward)

    # store-credential is handled without a connection
    if args.cmd == "store-credential":
        login_user = f"{args.domain}\\{args.user}" if args.domain else args.user
        key = _vault_key(login_user, args.host)
        store = CredentialStore(args.service)
        store.set(key, prompt_password(f"Password to store for {key}: "))
        print(f"Stored credential for {key!r} in service {args.service!r}.")
        return 0

    # serial commands need no SSH connection
    if args.cmd == "list-serial":
        from .serial_handler import list_serial_ports
        ports = list_serial_ports()
        if args.json:
            print(json.dumps(ports, indent=2))
        else:
            for p in ports:
                print(f"{p['device']:12} {p['description']}")
        return 0

    if args.cmd == "serial-monitor":
        from .serial_handler import SerialHandler
        print(f"Monitoring {args.port} @ {args.baud} (Ctrl+C to stop)...",
              file=sys.stderr)
        try:
            with SerialHandler(args.port, baudrate=args.baud, quiet=True) as ser:
                res = ser.stream(on_line=print, match=args.match,
                                 stop_on_match=args.stop_on_match,
                                 save_to=args.save, timeout=args.timeout)
            if args.match:
                print(f"\n[{len(res['matches'])} matched lines]", file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            return 130

    # install-ssh-remote uses WinRM (not SSH) — handle before the SSH connection
    if args.cmd == "install-ssh-remote":
        from .winrm_bootstrap import enable_openssh_via_winrm_offline
        pw = prompt_password(f"Password for {args.user}@{args.host}: ")
        openssh_dir = os.path.join(os.path.dirname(_setup_script_path()), "openssh")
        try:
            res = enable_openssh_via_winrm_offline(
                args.host, args.user, pw.reveal(), openssh_dir,
                domain=args.domain, ssh_port=args.ssh_port,
                winrm_port=args.winrm_port,
                log=lambda m: print(m, file=sys.stderr))
            print(f"OpenSSH installed on {args.host} (sshd: {res.get('status')}).")
            return 0
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    # camera-list / camera-grab handle their own connection (local needs none)
    if args.cmd == "camera-list":
        from .core import parse_dshow_devices
        if args.host or args.session:
            with _connect(args) as ssh:
                cams = ssh.list_cameras(ffmpeg=args.remote_ffmpeg)
            where = f"on {args.host}"
        else:
            ff = _resolve_ffmpeg(args.ffmpeg)
            if not ff:
                print("ffmpeg not found. Install it (on PATH) or pass --ffmpeg PATH.",
                      file=sys.stderr); return 2
            r = subprocess.run([ff, "-hide_banner", "-list_devices", "true",
                                "-f", "dshow", "-i", "dummy"],
                               capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
            cams = parse_dshow_devices((r.stdout or "") + "\n" + (r.stderr or ""))
            where = "on this machine"
        if args.json:
            print(json.dumps(cams, indent=2))
            return 0
        if not cams:
            print(f"No cameras found {where}.", file=sys.stderr); return 0
        for c in cams:
            print(c)
        return 0

    if args.cmd == "camera-grab":
        snapshot = args.seconds <= 0
        if args.host or args.session:
            try:
                with _connect(args) as ssh:
                    if args.force:
                        # clear any stale ffmpeg holding the camera (turbossh_cam
                        # stream or a previous turbossh_grab) before opening it
                        try:
                            ssh.webcam_release(ffmpeg_marker="turbossh", safe=True)
                        except Exception:
                            pass
                    lines = (ssh.run('powershell -NoProfile -Command "$env:TEMP"')
                             .text or "").splitlines()
                    rtmp = (lines[-1].strip() if lines else r"C:\Windows\Temp")
                    rpath = rtmp.rstrip("\\") + "\\turbossh_grab" + \
                        (".jpg" if snapshot else ".mp4")
                    if snapshot:
                        # warm the camera up (~1.5s) and keep the last frame —
                        # `-frames:v 1` alone often grabs before any frame arrives.
                        cmd = (f'"{args.remote_ffmpeg}" -y -hide_banner -loglevel error '
                               f'-f dshow -i video="{args.camera}" -t 1.5 -update 1 '
                               f'"{rpath}"')
                    else:
                        cmd = (f'"{args.remote_ffmpeg}" -y -hide_banner -loglevel error '
                               f'-f dshow -i video="{args.camera}" -t {args.seconds} -an '
                               f"-vf \"scale='min(iw,{args.width})':-2\" -r {args.fps} "
                               f'-c:v libx264 -preset veryfast -pix_fmt yuv420p "{rpath}"')
                    grab = ssh.run(cmd, timeout=max(40.0, args.seconds + 25))
                    if not grab.ok:
                        # report ffmpeg's own error, not a later "file not found"
                        raise SSHError(
                            f"remote ffmpeg failed (exit {grab.exit_code}): "
                            f"{(grab.stderr or grab.stdout).strip()[:400]}")
                    ssh.pull(rpath, args.out)
                    try:
                        ssh.run(f'cmd /c del "{rpath}"', timeout=10)
                    except Exception:
                        pass
            except SSHError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                if not args.force:
                    print("If the camera is in use, retry with --force to close any "
                          "stale ffmpeg holding it first.", file=sys.stderr)
                return 1
        else:
            ff = _resolve_ffmpeg(args.ffmpeg)
            if not ff:
                print("ffmpeg not found. Install it (on PATH) or pass --ffmpeg PATH.",
                      file=sys.stderr); return 2
            if snapshot:
                # warm the camera up (~1.5s) and keep the last frame — `-frames:v 1`
                # alone often grabs before the webcam delivers anything.
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "dshow",
                       "-i", f"video={args.camera}", "-t", "1.5", "-update", "1", args.out]
            else:
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "dshow",
                       "-i", f"video={args.camera}", "-t", str(args.seconds), "-an",
                       "-vf", f"scale='min(iw,{args.width})':-2", "-r", str(args.fps),
                       "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                       args.out]
            if subprocess.run(cmd, timeout=max(60.0, args.seconds + 30)).returncode != 0:
                print("ffmpeg failed to capture.", file=sys.stderr); return 1
        print(f"Saved {args.out}")
        return 0

    rc = 0
    with _connect(args) as ssh:
        if args.cmd == "run":
            cmd = " ".join(args.command).strip()
            if not cmd:
                print("No command given.", file=sys.stderr)
                return 2
            res = ssh.run(cmd, timeout=args.timeout)
            if not args.json:
                if res.stdout:
                    sys.stdout.write(res.stdout)
                if res.stderr:
                    sys.stderr.write(res.stderr)
                return res.exit_code
            _output(args, res)
            return res.exit_code
        elif args.cmd == "push":
            _output(args, ssh.push(args.local, args.remote,
                                   recursive=args.recursive))
        elif args.cmd == "pull":
            _output(args, ssh.pull(args.remote, args.local,
                                   recursive=args.recursive))
        elif args.cmd == "info":
            _output(args, {"host": args.host, "remote_os": ssh.detect_os(),
                           "connected": ssh.is_connected})
        elif args.cmd == "stream":
            cmd = " ".join(args.command).strip()
            if not cmd:
                print("No command given.", file=sys.stderr)
                return 2
            print(f"Streaming '{cmd}' (Ctrl+C to stop)...", file=sys.stderr)
            try:
                res = ssh.stream(cmd, on_line=print, match=args.match,
                                 stop_on_match=args.stop_on_match,
                                 save_to=args.save)
                if args.match:
                    print(f"\n[{len(res['matches'])} matched lines]",
                          file=sys.stderr)
            except KeyboardInterrupt:
                rc = 130
        elif args.cmd == "scan-ports":
            ports = ssh.remote_serial_ports()
            if args.json:
                print(json.dumps(ports, indent=2))
            else:
                if not ports:
                    print("No serial ports found on the remote host.",
                          file=sys.stderr)
                for p in ports:
                    print(f"{p['device']:10} {p.get('description', '')}")
        elif args.cmd == "serial-ssh":
            if args.send:
                ssh.serial_write(args.device, args.send, baudrate=args.baud)
            print(f"Monitoring {args.device} @ {args.baud} on {args.host} "
                  f"(Ctrl+C to stop)...", file=sys.stderr)
            try:
                res = ssh.serial_stream(args.device, baudrate=args.baud,
                                        on_line=print, match=args.match,
                                        stop_on_match=args.stop_on_match,
                                        save_to=args.save)
                if args.match:
                    print(f"\n[{len(res['matches'])} matched lines]",
                          file=sys.stderr)
            except KeyboardInterrupt:
                rc = 130
            finally:
                # Closing the channel doesn't kill the remote reader on Windows
                # OpenSSH, which would leave the COM port locked — release it.
                try:
                    ssh.serial_release(args.device, safe=True)
                except Exception:
                    pass
        elif args.cmd == "forward":
            import time as _time
            fwd = ssh.forward_local(args.to_host, args.to_port,
                                    local_port=args.local_port)
            lp = getattr(fwd, "local_port", args.local_port)
            print(f"Forwarding localhost:{lp} -> {args.to_host}:{args.to_port} "
                  f"via {args.host}  (Ctrl+C to stop)...", file=sys.stderr)
            try:
                while ssh.is_connected:
                    _time.sleep(1)
                print(f"The SSH connection to {args.host} dropped; forward "
                      f"closed.", file=sys.stderr)
                rc = 1
            except KeyboardInterrupt:
                rc = 130
            finally:
                try:
                    fwd.close()
                except Exception:
                    pass
        elif args.cmd == "sudo":
            cmd = " ".join(args.command).strip()
            if not cmd:
                print("No command given.", file=sys.stderr)
                return 2
            res = ssh.sudo(cmd, timeout=args.timeout, get_pty=args.pty)
            if args.json:
                _output(args, res)
            else:
                if res.stdout:
                    sys.stdout.write(res.stdout)
                if res.stderr:
                    sys.stderr.write(res.stderr)
            return res.exit_code
        elif args.cmd == "ls":
            import stat as _stat
            import time as _time
            rows = []
            for entry in ssh.listdir_attr(args.path):
                mode = entry.st_mode or 0
                rows.append({"name": entry.filename, "is_dir": _stat.S_ISDIR(mode),
                             "size": entry.st_size or 0, "mtime": entry.st_mtime or 0,
                             "mode": _stat.filemode(mode) if mode else ""})
            rows.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(r["mtime"]))
                    print(f"{r['mode']:10} {r['size']:>12} {when}  "
                          f"{r['name']}{'/' if r['is_dir'] else ''}")
        elif args.cmd == "copy-id":
            public = args.public_key
            if not public:
                for candidate in ("~/.ssh/id_ed25519.pub", "~/.ssh/id_rsa.pub"):
                    if os.path.exists(os.path.expanduser(candidate)):
                        public = candidate
                        break
            if not public:
                raise _UsageError("no public key found; pass --public-key FILE "
                                  "(create one with: turbossh keygen FILE).")
            if not ssh.copy_id(public_key_file=public):
                print("The remote host did not accept the key install.",
                      file=sys.stderr)
                return 1
            print(f"Installed {os.path.expanduser(public)} on {args.host}.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
