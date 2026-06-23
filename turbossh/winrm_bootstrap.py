"""
WinRM bootstrap: enable an SSH server on a remote Windows host that currently
has *no* SSH (port 22 closed) but *does* have WinRM (port 5985/5986) reachable.

This solves the chicken-and-egg problem: you cannot start sshd over SSH when SSH
is down, but if WinRM is up the script can use it as the bootstrap channel —
install OpenSSH Server, start the service, open the firewall — after which the
normal SSHHandler connects.

Requirements
------------
    pip install "turbossh[winrm]"        # pulls in pywinrm (+ NTLM transport)

The connecting account must be a **local administrator** on the target (installing
a Windows capability, creating a service, and adding a firewall rule all need
elevation). Domain accounts use NTLM by default, which works over plain WinRM
without Kerberos setup.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .credentials import Secret, mask
from .exceptions import SSHError

try:
    import winrm  # pywinrm
    _HAS_WINRM = True
except Exception:  # pragma: no cover
    winrm = None
    _HAS_WINRM = False


class WinRMError(SSHError):
    """A WinRM bootstrap operation failed."""


# PowerShell that idempotently enables OpenSSH Server. {extra} is an optional
# block (e.g. setting the default shell). Designed to be safe to re-run.
_ENABLE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {{
    $cap = Get-WindowsCapability -Online | Where-Object {{ $_.Name -like 'OpenSSH.Server*' }}
    if ($cap -and $cap.State -ne 'Installed') {{
        Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    }}
    Set-Service -Name sshd -StartupType Automatic
    Start-Service sshd
    if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort {ssh_port} | Out-Null
    }}
    {extra}
    'STATUS:' + (Get-Service sshd).Status
}} catch {{
    'ERROR:' + $_.Exception.Message
}}
"""

_SET_PS_DEFAULT = (
    r"New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell "
    r"-Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' "
    r"-PropertyType String -Force | Out-Null"
)


def enable_openssh_via_winrm(
    host: str,
    username: str,
    password,
    *,
    domain: Optional[str] = None,
    winrm_port: int = 5985,
    use_ssl: bool = False,
    transport: str = "ntlm",
    ssh_port: int = 22,
    set_powershell_default: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Connect over WinRM and enable OpenSSH Server on *host*.

    :returns: dict with keys ``ok``, ``status``, ``stdout``, ``stderr``.
    :raises WinRMError: if pywinrm is missing or the WinRM call fails.
    """
    if not _HAS_WINRM:
        raise WinRMError(
            "WinRM bootstrap needs pywinrm. Install it with: "
            'pip install "turbossh[winrm]"'
        )

    raw_pw = password.reveal() if isinstance(password, Secret) else password
    login = f"{domain}\\{username}" if domain else username
    scheme = "https" if use_ssl else "http"
    endpoint = f"{scheme}://{host}:{winrm_port}/wsman"

    def _log(msg: str):
        if log:
            log(mask(msg, password if isinstance(password, Secret) else Secret(raw_pw)))

    _log(f"WinRM -> {endpoint} as {login} (transport={transport})")

    try:
        session = winrm.Session(
            endpoint,
            auth=(login, raw_pw),
            transport=transport,
            server_cert_validation="ignore" if use_ssl else "validate",
        )
        extra = _SET_PS_DEFAULT if set_powershell_default else ""
        script = _ENABLE_SCRIPT.format(ssh_port=ssh_port, extra=extra)
        result = session.run_ps(script)
    except Exception as exc:  # pragma: no cover - needs a live host
        raise WinRMError(f"WinRM connection/exec to {host} failed: {exc}") from exc

    out = (result.std_out or b"").decode("utf-8", errors="replace").strip()
    err = (result.std_err or b"").decode("utf-8", errors="replace").strip()
    ok = result.status_code == 0 and "STATUS:" in out and "ERROR:" not in out
    status = ""
    for line in out.splitlines():
        if line.startswith("STATUS:"):
            status = line.split(":", 1)[1].strip()
        if line.startswith("ERROR:"):
            err = (err + "\n" + line).strip()
            ok = False

    _log(f"WinRM bootstrap {'OK' if ok else 'FAILED'} (sshd status: {status or 'n/a'})")
    if not ok:
        raise WinRMError(
            f"OpenSSH Server enable on {host} did not succeed. "
            f"stdout: {out[:400]} stderr: {err[:400]} "
            f"(is the account a local admin? is the OpenSSH.Server capability "
            f"source/Windows Update reachable?)"
        )
    return {"ok": ok, "status": status, "stdout": out, "stderr": err}


def winrm_available() -> bool:
    return _HAS_WINRM


def _smb_copy(host, login, password, local_path, remote_winpath, log) -> bool:
    """Copy a file to the remote host's admin share (C$) over SMB, authenticating
    with `net use`. Much faster than chunked WinRM. Returns True on success,
    False if SMB isn't usable (so the caller falls back). Windows only."""
    import os
    import shutil
    import subprocess
    if os.name != "nt":
        return False
    # remote_winpath is like 'C:\\Windows\\Temp\\turbossh_ossh\\openssh.zip'
    drive, rest = remote_winpath.split(":", 1)
    unc = rf"\\{host}\{drive}${rest}"                      # \\host\C$\Windows\...
    ipc = rf"\\{host}\IPC$"
    creationflags = 0x08000000                             # CREATE_NO_WINDOW
    try:
        subprocess.run(["net", "use", ipc, "/delete", "/y"],
                       capture_output=True, creationflags=creationflags)
    except Exception:
        pass
    r = subprocess.run(["net", "use", ipc, password, "/user:" + login],
                       capture_output=True, text=True, timeout=30,
                       creationflags=creationflags)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "net use failed").strip()[:160])
    try:
        os.makedirs(os.path.dirname(unc), exist_ok=True)
        shutil.copyfile(local_path, unc)
        log("  copied OpenSSH to the remote host over SMB (fast path)")
        return True
    finally:
        try:
            subprocess.run(["net", "use", ipc, "/delete", "/y"],
                           capture_output=True, creationflags=creationflags)
        except Exception:
            pass


# Decode the uploaded base64 (chunked-upload fallback) into the zip file.
_DECODE_B64 = (
    r"$d=Join-Path $env:TEMP 'turbossh_ossh';"
    r"[IO.File]::WriteAllBytes((Join-Path $d 'openssh.zip'),"
    r"[Convert]::FromBase64String(((Get-Content -LiteralPath (Join-Path $d 'openssh.b64')) -join '')));"
    r"'OK'")

# PowerShell that installs OpenSSH Server from an already-present ZIP — fully
# OFFLINE (no Add-WindowsCapability / Windows Update), then repairs host keys,
# starts the service and opens the firewall. {zip} and {port} are substituted.
_INSTALL_FROM_ZIP = r"""
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$zip = '{zip}'
try {{
    $ex = Join-Path $env:TEMP 'turbossh_ossh_x'
    if (Test-Path $ex) {{ Remove-Item $ex -Recurse -Force }}
    Expand-Archive -LiteralPath $zip -DestinationPath $ex -Force
    $src = Get-ChildItem $ex -Directory | Select-Object -First 1
    if (-not $src) {{ $src = Get-Item $ex }}
    $dest = Join-Path $env:ProgramFiles 'OpenSSH'
    if (-not (Test-Path $dest)) {{ New-Item -ItemType Directory -Path $dest | Out-Null }}
    Copy-Item (Join-Path $src.FullName '*') $dest -Recurse -Force
    $inst = Join-Path $dest 'install-sshd.ps1'
    if (Test-Path $inst) {{ & powershell -NoProfile -ExecutionPolicy Bypass -File $inst | Out-Null }}
    $kg = Join-Path $dest 'ssh-keygen.exe'
    if ((-not (Test-Path (Join-Path $env:ProgramData 'ssh\ssh_host_ed25519_key'))) -and (Test-Path $kg)) {{ & $kg -A | Out-Null }}
    $fp = Join-Path $dest 'FixHostFilePermissions.ps1'
    if (Test-Path $fp) {{ $ConfirmPreference='None'; & $fp -Confirm:$false | Out-Null }}
    Set-Service -Name sshd -StartupType Automatic
    try {{ Restart-Service sshd -ErrorAction Stop }} catch {{ Start-Service sshd }}
    $r = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    if (-not $r) {{
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort {port} -Profile Any | Out-Null
    }} else {{
        Enable-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
        Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Any -ErrorAction SilentlyContinue
    }}
    Remove-Item (Join-Path $env:TEMP 'turbossh_ossh') -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $ex -Recurse -Force -ErrorAction SilentlyContinue
    'STATUS:' + (Get-Service sshd).Status
}} catch {{ 'ERROR:' + $_.Exception.Message }}
"""


def enable_openssh_via_winrm_offline(
    host: str,
    username: str,
    password,
    openssh_dir: str,
    *,
    domain: Optional[str] = None,
    winrm_port: int = 5985,
    use_ssl: bool = False,
    transport: str = "ntlm",
    ssh_port: int = 22,
    chunk: int = 2200,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Install OpenSSH Server on a remote Windows host **fully offline, over WinRM**,
    by uploading the bundled OpenSSH ZIP (base64, chunked) and extracting/installing
    it remotely. For locked-down networks where Add-WindowsCapability/Windows
    Update is blocked — the chicken-and-egg solved without any downloads.

    ``openssh_dir`` is the folder holding the bundled ZIPs (OpenSSH-ARM64.zip /
    -Win64 / -Win32); the matching one is chosen from the *remote* CPU arch.
    The connecting account must be a local admin on the target, and WinRM
    (5985/5986) must be reachable. Returns ``{ok, status, stdout, stderr}``.
    """
    import base64
    import os

    if not _HAS_WINRM:
        raise WinRMError('WinRM bootstrap needs pywinrm: pip install "turbossh[winrm]"')

    raw_pw = password.reveal() if isinstance(password, Secret) else password
    login = f"{domain}\\{username}" if domain else username
    scheme = "https" if use_ssl else "http"
    endpoint = f"{scheme}://{host}:{winrm_port}/wsman"

    def _log(msg: str):
        if log:
            log(msg)

    try:
        session = winrm.Session(
            endpoint, auth=(login, raw_pw), transport=transport,
            server_cert_validation="ignore" if use_ssl else "validate")

        # pick the ZIP that matches the REMOTE machine's architecture
        ra = session.run_ps(
            "[System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()")
        arch = (ra.std_out or b"").decode("utf-8", "replace").strip()
        name = {"Arm64": "OpenSSH-ARM64.zip", "X64": "OpenSSH-Win64.zip",
                "X86": "OpenSSH-Win32.zip"}.get(arch, "OpenSSH-Win64.zip")
        zip_path = os.path.join(openssh_dir, name)
        if not os.path.exists(zip_path):
            raise WinRMError(f"Bundled OpenSSH ZIP not found: {zip_path}")
        _log(f"WinRM -> {endpoint} as {login}; remote arch {arch or '?'} -> {name}")

        # Get the remote TEMP dir + prepare staging.
        rt = session.run_ps(r"$d=Join-Path $env:TEMP 'turbossh_ossh';"
                            r"New-Item -ItemType Directory -Force -Path $d|Out-Null;$d")
        remote_temp = (rt.std_out or b"").decode("utf-8", "replace").strip().splitlines()
        remote_temp = remote_temp[-1] if remote_temp else r"C:\Windows\Temp\turbossh_ossh"

        used_smb = False
        # --- fast path: copy the ZIP over the admin share (SMB) -------------- #
        try:
            used_smb = _smb_copy(host, login, raw_pw, zip_path,
                                 remote_temp + r"\openssh.zip", _log)
        except Exception as exc:
            _log(f"  SMB copy unavailable ({exc}); falling back to WinRM upload")

        if used_smb:
            remote_zip = remote_temp + r"\openssh.zip"
        else:
            # --- fallback: base64 chunked upload over WinRM ----------------- #
            with open(zip_path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            chunks = [b64[i:i + chunk] for i in range(0, len(b64), chunk)]
            _log(f"Uploading over WinRM ({len(b64)//1024} KB base64, "
                 f"{len(chunks)} chunks — this is slow; SMB would be faster)…")
            session.run_ps(r"Set-Content -LiteralPath (Join-Path $env:TEMP "
                           r"'turbossh_ossh\openssh.b64') -Value '' -NoNewline; 'OK'")
            for i, c in enumerate(chunks):
                ps = (r"Add-Content -LiteralPath (Join-Path $env:TEMP "
                      r"'turbossh_ossh\openssh.b64') -Value '" + c + r"' -NoNewline")
                r = session.run_ps(ps)
                if r.status_code != 0:
                    raise WinRMError(
                        "upload chunk %d failed: %s" %
                        (i, (r.std_err or b"").decode("utf-8", "replace")[:300]))
                if log and (i % 100 == 0 or i == len(chunks) - 1):
                    _log(f"  uploaded {i + 1}/{len(chunks)} chunks")
            dr = session.run_ps(_DECODE_B64)
            if "OK" not in (dr.std_out or b"").decode("utf-8", "replace"):
                raise WinRMError("failed to reassemble the uploaded ZIP remotely")
            remote_zip = remote_temp + r"\openssh.zip"

        _log("Transfer done — extracting + installing on the remote host…")
        result = session.run_ps(
            _INSTALL_FROM_ZIP.format(zip=remote_zip, port=ssh_port))
    except WinRMError:
        raise
    except Exception as exc:
        raise WinRMError(f"WinRM connection/exec to {host} failed: {exc}") from exc

    out = (result.std_out or b"").decode("utf-8", "replace").strip()
    err = (result.std_err or b"").decode("utf-8", "replace").strip()
    status = ""
    ok = False
    for line in out.splitlines():
        if line.startswith("STATUS:"):
            status = line.split(":", 1)[1].strip()
            ok = status.lower() == "running"
        if line.startswith("ERROR:"):
            err = (err + "\n" + line).strip()
    _log(f"Remote OpenSSH install {'OK' if ok else 'FAILED'} (sshd: {status or 'n/a'})")
    if not ok:
        raise WinRMError(f"Remote install on {host} did not succeed. "
                         f"out: {out[:300]} err: {err[:300]}")
    return {"ok": ok, "status": status, "stdout": out, "stderr": err}
