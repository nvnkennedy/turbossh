"""Get hold of ffmpeg for the remote-webcam feature.

ffmpeg is too big to bundle in the PyPI wheel (it would push us over the 100 MB
limit), so we fetch a Windows build once from a GitHub release, cache it under
<state dir>/ffmpeg/, and push it to the remote machine over SFTP when needed.
A user-supplied ffmpeg (Settings → ffmpeg path) overrides the download for fully
offline setups.

Both the download and the upload are written to a ``.part`` file and renamed
into place only once complete, and the cached exe carries a size/hash record —
so an interrupted run can never leave a truncated ffmpeg.exe behind that later
passes for a good one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import zipfile

from .. import paths
from ..core import parse_dshow_devices

# BtbN's FFmpeg-Builds "latest" release — a stable, public Windows build URL.
_FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-master-latest-win64-gpl.zip")
REMOTE_MARKER = "turbossh_cam"        # the dir/marker webcam_release matches on
PIN_ENV = "TURBOSSH_FFMPEG_SHA256"    # optional SHA-256 pin (wins over Settings)

_DOWNLOAD_BUDGET = 30 * 60            # seconds for one whole download
# A live download touches its .part at least once a minute (the socket timeout),
# so one untouched for this long belongs to a run that died.
_STALE_PART = 10 * 60
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_download_lock = threading.Lock()


class _Aborted(RuntimeError):
    """Cancelled or out of time — reported as it is, not as a download error."""


def _cache_dir() -> str:
    return paths.state_path("ffmpeg")


def _pinned_sha256() -> str:
    """The SHA-256 a downloaded ffmpeg.exe must have, or "" when not pinned."""
    pin = os.environ.get(PIN_ENV, "").strip()
    if not pin:
        try:
            from . import settings as _s
            pin = (_s.get("ffmpeg_sha256") or "").strip()
        except Exception:
            pin = ""
    return pin.lower()


def _sidecar(exe: str):
    """``(sha256, size)`` recorded next to *exe* when it was installed, or None."""
    try:
        with open(exe + ".sha256", encoding="ascii") as fh:
            digest, size = fh.read().split()[:2]
        return digest.lower(), int(size)
    except (OSError, ValueError):
        return None


def _write_sidecar(exe: str, digest: str, size: int) -> None:
    tmp = f"{exe}.sha256.{uuid.uuid4().hex[:8]}.part"
    try:
        with open(tmp, "w", encoding="ascii") as fh:
            fh.write(f"{digest} {size}\n")
        os.replace(tmp, exe + ".sha256")
    finally:
        _remove(tmp)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _cache_ok(exe: str) -> bool:
    """Is our cached download complete? Deliberately cheap — this runs on every
    scan and every recording — so it compares the file size with the one
    recorded at install time instead of hashing ~130 MB. A configured pin is
    compared with the recorded hash."""
    record = _sidecar(exe)
    if record is None:
        return False
    try:
        if os.path.getsize(exe) != record[1]:
            return False
    except OSError:
        return False
    pin = _pinned_sha256()
    return not pin or pin == record[0]


def cached_ffmpeg() -> str | None:
    """Return an ffmpeg we can use without downloading: an explicit Settings
    path, our cache (only if it is complete), or an ffmpeg already on PATH."""
    try:
        from . import settings as _s
        manual = (_s.get("ffmpeg_path") or "").strip()
        if manual and os.path.exists(manual):
            return manual
    except Exception:
        pass
    p = os.path.join(_cache_dir(), "ffmpeg.exe")
    if _cache_ok(p):
        return p
    return shutil.which("ffmpeg") or None   # already installed system-wide?


def _check(cancel, deadline: float) -> None:
    if cancel is not None and cancel.is_set():
        raise _Aborted("The ffmpeg download was cancelled.")
    if time.monotonic() > deadline:
        raise _Aborted(
            f"The ffmpeg download did not finish within {_DOWNLOAD_BUDGET // 60} "
            "minutes. Check the connection and scan again, or set an ffmpeg path "
            "in Settings → Camera → ffmpeg path.")


def _sweep_parts(cache: str) -> None:
    """Delete what interrupted runs left behind (``*.part``, and the
    ``ffmpeg.zip`` older versions downloaded to). Only files untouched for a
    while: a fresh one may belong to another TurboSSH that is downloading now."""
    try:
        names = os.listdir(cache)
    except OSError:
        return
    cutoff = time.time() - _STALE_PART
    for name in names:
        if name.endswith(".part") or name == "ffmpeg.zip":
            path = os.path.join(cache, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


def _adopt_unrecorded(exe: str) -> bool:
    """An ffmpeg.exe with no size record was cached by an older TurboSSH, which
    could leave a truncated file in place. Keep it only if it actually runs (a
    cut-off exe does not load), and record it so this is not repeated — that
    spares a working cache the 160 MB download, and replaces a broken one."""
    if _sidecar(exe) is not None or not os.path.isfile(exe):
        return False
    try:
        probe = subprocess.run([exe, "-version"], capture_output=True, timeout=20,
                               creationflags=_NO_WINDOW)
        if probe.returncode != 0:
            return False
        digest = _sha256(exe)
        pin = _pinned_sha256()
        if pin and digest != pin:
            return False
        _write_sidecar(exe, digest, os.path.getsize(exe))
    except Exception:
        return False
    return True


def _fetch(dest: str, log, cancel, deadline: float) -> None:
    """Download the release zip to *dest*."""
    log("Downloading ffmpeg (one-time, ~160 MB — please wait)…")
    try:
        req = urllib.request.Request(_FFMPEG_URL, headers={"User-Agent": "turbossh"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            # read1 returns what has arrived instead of waiting for a full
            # block, so a cancel is noticed promptly on a slow link
            read = getattr(r, "read1", None) or r.read
            got = 0
            last = 0
            while True:
                _check(cancel, deadline)
                chunk = read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                mb = got // (1024 * 1024)
                if mb >= last + 5:          # report every ~5 MB
                    last = mb
                    pct = f" ({got * 100 // total}%)" if total else ""
                    log(f"Downloading ffmpeg… {mb} MB{pct}")
        # http.client hands back a short body without complaint when the
        # connection drops, so the length has to be checked here.
        if total and got != total:
            raise OSError(f"the connection dropped after {got} of {total} bytes")
    except _Aborted:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Couldn't download ffmpeg ({exc}). Install ffmpeg (so it's on PATH) "
            f"or set its path in Settings → Camera → ffmpeg path.") from None


def _extract(zip_path: str, dest: str) -> int:
    """Stream ``…/bin/ffmpeg.exe`` out of the zip into *dest* (never the whole
    exe in memory); returns its size. zipfile checks the member's CRC."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            info = next((i for i in z.infolist()
                         if i.filename.endswith("/bin/ffmpeg.exe")), None)
            if info is None:
                raise RuntimeError("the archive has no bin/ffmpeg.exe")
            with z.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        if os.path.getsize(dest) != info.file_size:
            raise RuntimeError("ffmpeg.exe came out incomplete")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(
            f"Couldn't extract ffmpeg: the download is not a valid zip archive "
            f"({exc}). A proxy or firewall may have replaced it — scan again, or "
            f"set an ffmpeg path in Settings → Camera → ffmpeg path.") from None
    except Exception as exc:
        raise RuntimeError(f"Couldn't extract ffmpeg from the download: {exc}") from None
    return info.file_size


def _download(cache: str, log, cancel, deadline: float) -> str:
    os.makedirs(cache, exist_ok=True)
    exe = os.path.join(cache, "ffmpeg.exe")
    # unique names: another TurboSSH process may be downloading at the same time
    tag = uuid.uuid4().hex[:12]
    zip_part = os.path.join(cache, f"ffmpeg.zip.{tag}.part")
    exe_part = os.path.join(cache, f"ffmpeg.exe.{tag}.part")
    try:
        _fetch(zip_part, log, cancel, deadline)
        _check(cancel, deadline)
        log("Extracting ffmpeg…")
        size = _extract(zip_part, exe_part)
        digest = _sha256(exe_part)
        pin = _pinned_sha256()
        if pin and digest != pin:
            raise RuntimeError(
                "The downloaded ffmpeg.exe does not match the pinned SHA-256 and "
                f"was discarded (expected {pin}, got {digest}). Correct the pin "
                f"(Settings, or {PIN_ENV}) or set an ffmpeg path in Settings → "
                "Camera → ffmpeg path.")
        try:
            os.replace(exe_part, exe)       # complete and verified -> into place
        except OSError as exc:
            raise RuntimeError(f"Couldn't install the downloaded ffmpeg: {exc}") from None
        _write_sidecar(exe, digest, size)
    finally:
        _remove(zip_part)
        _remove(exe_part)
    return exe


def ensure_local_ffmpeg(log=lambda m: None, cancel=None) -> str:
    """Return a local ffmpeg.exe, downloading + caching it on first use.

    ``cancel`` is an optional ``threading.Event``; setting it stops a download in
    progress. Raises RuntimeError if ffmpeg can't be obtained, the download was
    cancelled, or it took longer than 30 minutes."""
    cache = _cache_dir()
    _sweep_parts(cache)
    have = cached_ffmpeg()
    if have:
        return have
    deadline = time.monotonic() + _DOWNLOAD_BUDGET
    # One download at a time: a second caller (another Camera tab, a re-scan)
    # waits for the first and then simply finds the cache filled.
    if not _download_lock.acquire(blocking=False):
        log("Waiting for the ffmpeg download that is already running…")
        while not _download_lock.acquire(timeout=0.25):
            _check(cancel, deadline)
    try:
        have = cached_ffmpeg()
        if have:
            return have
        exe = os.path.join(cache, "ffmpeg.exe")
        if _adopt_unrecorded(exe):
            return exe
        _check(cancel, deadline)
        return _download(cache, log, cancel, deadline)
    finally:
        _download_lock.release()


def _remote_size(ssh, path: str):
    try:
        return ssh.stat(path, safe=False).st_size
    except Exception:
        return None


def _replace_remote(ssh, part: str, dest: str) -> None:
    """Move the finished upload into place. posix-rename replaces the old file
    in one step; the plain SFTP rename (Windows OpenSSH, older servers) refuses
    to overwrite, so the old file is removed first — after releasing a camera
    ffmpeg of ours that may still be running from it (Windows locks a running
    exe)."""
    try:
        ssh.rename(part, dest, safe=False)
        return
    except Exception:
        pass
    try:
        ssh.webcam_release(safe=True)
    except Exception:
        pass
    try:
        ssh.remove(dest, safe=False)
    except Exception:
        pass
    ssh.rename(part, dest, safe=False)


def ensure_remote_ffmpeg(ssh, local_ffmpeg: str, log=lambda m: None, *,
                         remote_dir: str | None = None) -> str:
    """Make sure ffmpeg is on the remote host (push it via SFTP if missing or
    incomplete) and return the remote path. Pushed under a dir containing
    REMOTE_MARKER so webcam_release can find/kill only our process;
    ``remote_dir`` overrides that folder.

    NOTE: every SSH call here passes ``safe=False`` so we get RAW results (a real
    stat from ``stat``, a CommandResult with ``.text`` from ``run``). The handler
    is created in safe mode, where those calls would otherwise return an
    OperationResult that is *truthy on success regardless of the value* — which
    made ``if ssh.exists(...)`` always true, so ffmpeg was never actually uploaded
    and the remote camera could never be listed."""
    if remote_dir is None:
        res = ssh.run('powershell -NoProfile -Command "$env:TEMP"', safe=False, timeout=20)
        lines = (getattr(res, "text", "") or "").strip().splitlines()
        temp = (lines[-1].strip() if lines else "") or r"C:\Windows\Temp"
        remote_dir = temp.rstrip("\\") + "\\" + REMOTE_MARKER
    sep = "\\" if "\\" in remote_dir else "/"
    remote_exe = remote_dir.rstrip("\\/") + sep + "ffmpeg.exe"
    want = os.path.getsize(local_ffmpeg)
    # Same size as ours = a finished upload. A bare "it exists" check would
    # reuse a file cut off by an interrupted upload for ever.
    if _remote_size(ssh, remote_exe) == want:
        return remote_exe
    log("Uploading ffmpeg to the remote machine (one-time, ~160 MB)…")
    try:
        ssh.makedirs(remote_dir, safe=False)
    except Exception:
        pass
    part = remote_exe + ".part"
    ssh.push(local_ffmpeg, part, safe=False)
    got = _remote_size(ssh, part)
    if got != want:
        try:
            ssh.remove(part, safe=False)
        except Exception:
            pass
        raise RuntimeError("ffmpeg failed to upload to the remote machine "
                           f"({remote_exe}): {got or 0} of {want} bytes arrived.")
    try:
        _replace_remote(ssh, part, remote_exe)
    except Exception as exc:
        raise RuntimeError("ffmpeg was uploaded but could not be moved into place "
                           f"on the remote machine ({remote_exe}): {exc}") from None
    if _remote_size(ssh, remote_exe) != want:
        raise RuntimeError("ffmpeg failed to upload to the remote machine "
                           f"({remote_exe}).")
    return remote_exe


def list_local_cameras(ffmpeg: str) -> list:
    """List DirectShow cameras on THIS machine (Windows)."""
    try:
        # ffmpeg prints device names as UTF-8; the locale codec (text=True)
        # garbles non-ASCII camera names, which then fail to open.
        p = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, encoding="utf-8", errors="replace",
                           timeout=20, creationflags=_NO_WINDOW)
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
