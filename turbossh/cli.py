"""
Standalone command-line entry point.

    python -m turbossh run   --host H --user U [--domain CORP] CMD...
    python -m turbossh push  --host H --user U LOCAL REMOTE [--recursive]
    python -m turbossh pull  --host H --user U REMOTE LOCAL [--recursive]
    python -m turbossh info  --host H --user U

Credentials (password never echoed, never stored in plaintext):
    --password           prompt interactively (hidden)
    --use-stored         read from the OS credential vault
    --store-credential   save to the OS vault, then exit
    --key FILE           use a private key instead of a password
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import subprocess

from .config import SSHConfig
from .core import SSHHandler
from .credentials import CredentialStore, prompt_password, Secret
from .exceptions import SSHError
from .results import CommandResult, TransferResult


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
    stage = os.path.join(tempfile.gettempdir(), "turbossh-sshd-setup")
    try:
        os.makedirs(stage, exist_ok=True)
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
                f"-ArgumentList '{inner}'\n")
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", elevate_ps],
            creationflags=CREATE_NO_WINDOW)
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
        webbrowser.open("file://" + readme)
        print(f"Opened bundled README: {readme}")
    else:
        print(f"Docs: {DOCS_URL}")
    return 0


def _gui_exe_path() -> str:
    d = os.path.join(os.path.dirname(__file__), "bin")
    for name in ("turbossh-gui.exe", "turbossh-gui.exe"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(d, "turbossh-gui.exe")


def launch_rdp(host: str, user: str = None, domain: str = None,
               password: str = None) -> bool:
    """Open the native Windows Remote Desktop client (mstsc) for *host*,
    pre-seeding credentials via cmdkey so it doesn't prompt. Windows only."""
    if os.name != "nt":
        return False
    try:
        if user and password:
            full = f"{domain}\\{user}" if domain else user
            subprocess.run(["cmdkey", f"/generic:TERMSRV/{host}",
                            f"/user:{full}", f"/pass:{password}"],
                           capture_output=True, timeout=10)
        subprocess.Popen(["mstsc", f"/v:{host}"])
        return True
    except Exception:
        return False


def _icon_path() -> str:
    return os.path.join(os.path.dirname(__file__), "assets", "icon.ico")


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
    icon_line = (f"  $lnk.IconLocation = '{icon},0';\n"
                 if os.path.exists(icon) else "")
    args_line = f"  $lnk.Arguments = '{args}';\n" if args else ""
    # Desktop + Start-Menu\Programs, both in one PowerShell pass.
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;\n"
        "$dirs = @([Environment]::GetFolderPath('Desktop'),"
        " [Environment]::GetFolderPath('Programs'));\n"
        "foreach ($d in $dirs) {\n"
        "  if (-not (Test-Path $d)) { continue }\n"
        f"  $p = [IO.Path]::Combine($d, '{name}.lnk');\n"
        "  $lnk = $ws.CreateShortcut($p);\n"
        f"  $lnk.TargetPath = '{target}';\n"
        f"{args_line}"
        f"  $lnk.WorkingDirectory = '{workdir}';\n"
        f"{icon_line}"
        "  $lnk.Description = 'TurboSSH - SSH / Serial / SFTP toolkit';\n"
        "  $lnk.Save();\n"
        "}"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       check=True, capture_output=True, timeout=30)
        return True
    except Exception:
        return False


def ensure_shortcuts(name: str = "TurboSSH") -> bool:
    """Create the Desktop / Start-Menu shortcuts if they're missing. Cheap to
    call on every launch (only shells out to PowerShell when one is absent)."""
    if os.name != "nt":
        return False
    try:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop", f"{name}.lnk")
        programs = os.path.join(
            os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", f"{name}.lnk")
        if os.path.exists(desktop) and os.path.exists(programs):
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
        from .gui.app import main as gui_main
        return gui_main()
    except ImportError:
        pass
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
    print(f"Launching OpenSSH Server setup (will prompt for Administrator)...")
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
    cached = os.path.join(os.path.expanduser("~"), ".turbossh", "ffmpeg", "ffmpeg.exe")
    return cached if os.path.exists(cached) else None


def _add_conn_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--host", required=required)
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--user", required=required)
    p.add_argument("--domain", default=None, help="e.g. CORP for CORP\\user logins")
    p.add_argument("--key", default=None, help="private key file")
    p.add_argument("--password", action="store_true",
                   help="prompt for password (hidden input)")
    p.add_argument("--use-stored", action="store_true",
                   help="read password from the OS credential vault")
    p.add_argument("--service", default="turbossh",
                   help="credential-vault namespace")
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--no-fast-auth", action="store_true")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def _resolve_password(args, login_user: str) -> Secret | None:
    if args.use_stored:
        store = CredentialStore(args.service)
        sec = store.get(login_user)
        if sec is None:
            print(f"No stored credential for {login_user!r} in service "
                  f"{args.service!r}.", file=sys.stderr)
            sys.exit(2)
        return sec
    if args.password:
        return prompt_password(f"Password for {login_user}: ")
    return None


def _build_config(args) -> SSHConfig:
    login_user = f"{args.domain}\\{args.user}" if args.domain else args.user
    return SSHConfig(
        host=args.host, port=args.port, username=args.user, domain=args.domain,
        password=_resolve_password(args, login_user),
        key_filename=args.key, command_timeout=args.timeout,
        fast_auth=not args.no_fast_auth,
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
    parser = argparse.ArgumentParser(prog="turbossh",
                                     description="Extensive SSH/SFTP/SCP handler CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # store-credential (no connection)
    sc = sub.add_parser("store-credential", help="save a password to the OS vault")
    sc.add_argument("--user", required=True)
    sc.add_argument("--domain", default=None)
    sc.add_argument("--service", default="turbossh")

    p_run = sub.add_parser("run", help="execute a remote command")
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

    p_lsr = sub.add_parser("list-serial", help="list available serial/COM ports")

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

    # setup-server takes no connection args and runs the bundled PowerShell script
    if argv is None:
        _argv = sys.argv[1:]
    else:
        _argv = list(argv)
    if _argv and _argv[0] == "setup-server":
        forward = []
        rest = _argv[1:]
        if "--install-pip" in rest:
            forward.append("-InstallPip")
        if "--port" in rest:
            i = rest.index("--port")
            if i + 1 < len(rest):
                forward += ["-Port", rest[i + 1]]
        return setup_server_main(forward)

    args = parser.parse_args(argv)

    # store-credential is handled without a connection
    if args.cmd == "store-credential":
        login_user = f"{args.domain}\\{args.user}" if args.domain else args.user
        store = CredentialStore(args.service)
        store.set(login_user, prompt_password(f"Password to store for {login_user}: "))
        print(f"Stored credential for {login_user!r} in service {args.service!r}.")
        return 0

    # serial commands need no SSH connection
    if args.cmd == "list-serial":
        from .serial_handler import list_serial_ports
        for p in list_serial_ports():
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
            return 0

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
        if args.host:
            try:
                with SSHHandler(_build_config(args)) as ssh:
                    cams = ssh.list_cameras(ffmpeg=args.remote_ffmpeg)
            except SSHError as exc:
                print(f"ERROR: {exc}", file=sys.stderr); return 1
            where = f"on {args.host}"
        else:
            ff = _resolve_ffmpeg(args.ffmpeg)
            if not ff:
                print("ffmpeg not found. Install it (on PATH) or pass --ffmpeg PATH.",
                      file=sys.stderr); return 2
            r = subprocess.run([ff, "-hide_banner", "-list_devices", "true",
                                "-f", "dshow", "-i", "dummy"],
                               capture_output=True, text=True)
            cams = parse_dshow_devices((r.stdout or "") + "\n" + (r.stderr or ""))
            where = "on this machine"
        if not cams:
            print(f"No cameras found {where}.", file=sys.stderr); return 0
        for c in cams:
            print(c)
        return 0

    if args.cmd == "camera-grab":
        snapshot = args.seconds <= 0
        if args.host:
            try:
                with SSHHandler(_build_config(args)) as ssh:
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
                    ssh.run(cmd, timeout=max(40.0, args.seconds + 25))
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
            if subprocess.run(cmd).returncode != 0:
                print("ffmpeg failed to capture.", file=sys.stderr); return 1
        print(f"Saved {args.out}")
        return 0

    try:
        with SSHHandler(_build_config(args)) as ssh:
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
                    pass
            elif args.cmd == "scan-ports":
                ports = ssh.remote_serial_ports()
                if not ports:
                    print("No serial ports found on the remote host.", file=sys.stderr)
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
                    pass
            elif args.cmd == "forward":
                import time as _time
                fwd = ssh.forward_local(args.to_host, args.to_port,
                                        local_port=args.local_port)
                lp = getattr(fwd, "local_port", args.local_port)
                print(f"Forwarding localhost:{lp} -> {args.to_host}:{args.to_port} "
                      f"via {args.host}  (Ctrl+C to stop)...", file=sys.stderr)
                try:
                    while True:
                        _time.sleep(1)
                except KeyboardInterrupt:
                    pass
                finally:
                    try:
                        fwd.close()
                    except Exception:
                        pass
        return 0
    except SSHError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
