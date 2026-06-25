"""Get hold of ffmpeg for the remote-webcam feature.

ffmpeg is too big to bundle in the PyPI wheel (it would push us over the 100 MB
limit), so we fetch a Windows build once from a GitHub release, cache it under
~/.turbossh/ffmpeg/, and push it to the remote machine over SFTP when needed.
A user-supplied ffmpeg (Settings → ffmpeg path) overrides the download for fully
offline setups.
"""

from __future__ import annotations

import os
import zipfile
import subprocess
import urllib.request

from ..core import parse_dshow_devices

_CACHE = os.path.join(os.path.expanduser("~"), ".turbossh", "ffmpeg")
# BtbN's FFmpeg-Builds "latest" release — a stable, public Windows build URL.
_FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-master-latest-win64-gpl.zip")
REMOTE_MARKER = "turbossh_cam"        # the dir/marker webcam_release matches on


def cached_ffmpeg() -> str | None:
    """Return an ffmpeg we can use without downloading: an explicit Settings
    path, our cache, or an ffmpeg already on PATH."""
    try:
        from . import settings as _s
        manual = (_s.get("ffmpeg_path") or "").strip()
        if manual and os.path.exists(manual):
            return manual
    except Exception:
        pass
    p = os.path.join(_CACHE, "ffmpeg.exe")
    if os.path.exists(p):
        return p
    import shutil
    on_path = shutil.which("ffmpeg")        # already installed system-wide?
    return on_path or None


def ensure_local_ffmpeg(log=lambda m: None) -> str:
    """Return a local ffmpeg.exe, downloading + caching it on first use.
    Raises RuntimeError if it can't be obtained."""
    have = cached_ffmpeg()
    if have:
        return have
    os.makedirs(_CACHE, exist_ok=True)
    zip_path = os.path.join(_CACHE, "ffmpeg.zip")
    log("Downloading ffmpeg (one-time, ~160 MB — please wait)…")
    try:
        req = urllib.request.Request(_FFMPEG_URL, headers={"User-Agent": "turbossh"})
        with urllib.request.urlopen(req, timeout=60) as r, open(zip_path, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            last = 0
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                mb = got // (1024 * 1024)
                if mb >= last + 5:          # report every ~5 MB
                    last = mb
                    pct = f" ({got * 100 // total}%)" if total else ""
                    log(f"Downloading ffmpeg… {mb} MB{pct}")
    except Exception as exc:
        raise RuntimeError(
            f"Couldn't download ffmpeg ({exc}). Install ffmpeg (so it's on PATH) "
            f"or set its path in Settings → Camera → ffmpeg path.")
    log("Extracting ffmpeg…")
    try:
        with zipfile.ZipFile(zip_path) as z:
            member = next(n for n in z.namelist() if n.endswith("/bin/ffmpeg.exe"))
            with z.open(member) as src, open(os.path.join(_CACHE, "ffmpeg.exe"), "wb") as dst:
                dst.write(src.read())
    except Exception as exc:
        raise RuntimeError(f"Couldn't extract ffmpeg from the download: {exc}")
    finally:
        try:
            os.remove(zip_path)
        except Exception:
            pass
    out = os.path.join(_CACHE, "ffmpeg.exe")
    if not os.path.exists(out):
        raise RuntimeError("ffmpeg.exe not found after extraction.")
    return out


def ensure_remote_ffmpeg(ssh, local_ffmpeg: str, log=lambda m: None) -> str:
    """Make sure ffmpeg is on the remote host (push it via SFTP if missing) and
    return the remote path. Pushed under a dir containing REMOTE_MARKER so
    webcam_release can find/kill only our process.

    NOTE: every SSH call here passes ``safe=False`` so we get RAW results (a real
    bool from ``exists``, a CommandResult with ``.text`` from ``run``). The handler
    is created in safe mode, where those calls would otherwise return an
    OperationResult that is *truthy on success regardless of the value* — which
    made ``if ssh.exists(...)`` always true, so ffmpeg was never actually uploaded
    and the remote camera could never be listed."""
    res = ssh.run('powershell -NoProfile -Command "$env:TEMP"', safe=False, timeout=20)
    lines = (getattr(res, "text", "") or "").strip().splitlines()
    temp = (lines[-1].strip() if lines else "") or r"C:\Windows\Temp"
    remote_dir = temp.rstrip("\\") + "\\" + REMOTE_MARKER
    remote_exe = remote_dir + "\\ffmpeg.exe"
    try:
        if ssh.exists(remote_exe, safe=False):
            return remote_exe
    except Exception:
        pass
    log("Uploading ffmpeg to the remote machine (one-time, ~160 MB)…")
    try:
        ssh.makedirs(remote_dir, safe=False)
    except Exception:
        pass
    ssh.push(local_ffmpeg, remote_exe, safe=False)
    if not ssh.exists(remote_exe, safe=False):
        raise RuntimeError("ffmpeg failed to upload to the remote machine "
                           f"({remote_exe}).")
    return remote_exe


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def list_local_cameras(ffmpeg: str) -> list:
    """List DirectShow cameras on THIS machine (Windows)."""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=_NO_WINDOW)
        return parse_dshow_devices((p.stdout or "") + "\n" + (p.stderr or ""))
    except Exception:
        return []


def local_capture_args(ffmpeg: str, camera: str, *, width: int = 1280,
                       height: int = 720, fps: int = 25, quality: int = 6) -> list:
    """ffmpeg argv to capture a LOCAL camera and emit MJPEG on stdout, low-delay."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-f", "dshow", "-rtbufsize", "16M", "-i", f"video={camera}",
            "-an", "-vf", f"scale={int(width)}:{int(height)}", "-r", str(int(fps)),
            "-f", "mjpeg", "-q:v", str(int(quality)), "-"]
