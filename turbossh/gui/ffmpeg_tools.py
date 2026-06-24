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
    """Return the cached/local ffmpeg.exe path if we already have one."""
    try:
        from . import settings as _s
        manual = (_s.get("ffmpeg_path") or "").strip()
        if manual and os.path.exists(manual):
            return manual
    except Exception:
        pass
    p = os.path.join(_CACHE, "ffmpeg.exe")
    return p if os.path.exists(p) else None


def ensure_local_ffmpeg(log=lambda m: None) -> str:
    """Return a local ffmpeg.exe, downloading + caching it on first use.
    Raises RuntimeError if it can't be obtained."""
    have = cached_ffmpeg()
    if have:
        return have
    os.makedirs(_CACHE, exist_ok=True)
    zip_path = os.path.join(_CACHE, "ffmpeg.zip")
    log("Downloading ffmpeg (one-time, ~80 MB)…")
    try:
        req = urllib.request.Request(_FFMPEG_URL, headers={"User-Agent": "turbossh"})
        with urllib.request.urlopen(req, timeout=60) as r, open(zip_path, "wb") as fh:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
    except Exception as exc:
        raise RuntimeError(
            f"Couldn't download ffmpeg ({exc}). On a blocked network, set a local "
            f"ffmpeg.exe path in Settings → Camera.")
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
    webcam_release can find/kill only our process."""
    res = ssh.run('powershell -NoProfile -Command "$env:TEMP"')
    temp = (getattr(res, "text", "") or "").strip().splitlines()
    temp = temp[-1].strip() if temp else r"C:\Windows\Temp"
    remote_dir = temp.rstrip("\\") + "\\" + REMOTE_MARKER
    remote_exe = remote_dir + "\\ffmpeg.exe"
    try:
        if ssh.exists(remote_exe):
            return remote_exe
    except Exception:
        pass
    log("Uploading ffmpeg to the remote machine (one-time)…")
    try:
        ssh.makedirs(remote_dir)
    except Exception:
        pass
    ssh.push(local_ffmpeg, remote_exe)
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


def local_capture_args(ffmpeg: str, camera: str, *, width: int = 640,
                       height: int = 480, fps: int = 15, quality: int = 6) -> list:
    """ffmpeg argv to capture a LOCAL camera and emit MJPEG on stdout."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "dshow",
            "-rtbufsize", "64M", "-i", f"video={camera}",
            "-vf", f"scale={int(width)}:{int(height)}", "-r", str(int(fps)),
            "-f", "mjpeg", "-q:v", str(int(quality)), "-"]
