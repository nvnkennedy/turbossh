"""Persisted app settings (theme, fonts, defaults) in <state dir>/settings.json.

Written atomically, and merged: ``save`` re-reads the file and writes only the
keys this process changed, so two TurboSSH processes (the desktop app and one
hosted in another application, or two windows) never wipe out each other's
saved machines or jump host. A file that cannot be read is never written over:
a damaged one only once a copy of it is kept (see ``load_problem``), a locked
one not at all (see ``save_problem``)."""

from __future__ import annotations

import copy
import json
import os
import time

from .. import paths
from .._jsonstore import atomic_write_json, read_json, set_aside

_DIR = paths.state_dir()
_FILE = paths.state_path("settings.json")

DEFAULTS = {
    "theme": "dark",            # any name in theme.THEME_NAMES
    "term_font_size": 11,
    # crisp modern monospace (ships with Win10/11); falls back to Consolas/Courier
    "term_font": "Cascadia Mono",
    "compact_ribbon": False,    # icons-only ribbon (no text) when True
    # MobaXterm-style keyword colouring in the terminal: tints plain words like
    # error/warning/success. Never overrides the server's own ANSI colours.
    "highlight_keywords": True,
    # in-widget scrollback (lines kept in memory for wheel-scrolling). pyte costs
    # ~16 KB/line, so this is deliberately modest; the FULL session is always
    # teed to disk, so "Save all output" is unlimited regardless of this value.
    "term_scrollback": 10000,
    "default_baud": 115200,
    # Camera is off until asked for (View → Camera). While off there is no Camera
    # button, menu entry or Home card, and nothing camera-related runs.
    "camera_enabled": False,
    # Camera (remote webcam over SSH via ffmpeg). ffmpeg_path is an optional local
    # ffmpeg.exe to use instead of the auto-fetched one (fully offline).
    "ffmpeg_path": "",
    # optional SHA-256 the downloaded ffmpeg.exe must match (pins the build)
    "ffmpeg_sha256": "",
    # ask before pasting several lines into a shell that would run them at once
    "confirm_multiline_paste": True,
    # where the file browser's local pane opens (remembered from last time)
    "local_browser_dir": "",
    "open_docs_first_run": True,
    "make_shortcut_first_run": True,
    # shared jump host (the RDP/Windows machine) — entered once in Settings and
    # reused by every session that uses "Via jump host". Password -> OS vault.
    "jump_host": "",
    "jump_user": "",
    "jump_domain": "",
    # saved machines (RDP / Windows boxes you use often). Each is
    # {name, host, user, domain}. They populate the host dropdowns everywhere:
    # the SSH/serial session dialog and the Camera panel's Remote source.
    "machines": [],
}

_KR_JUMP = ("turbossh-sessions", "::jump-default")


def jump_password() -> str:
    try:
        import keyring
        return keyring.get_password(*_KR_JUMP) or ""
    except Exception:
        return ""


def set_jump_password(value: str):
    try:
        import keyring
        if value:
            keyring.set_password(_KR_JUMP[0], _KR_JUMP[1], value)
        else:
            try:
                keyring.delete_password(_KR_JUMP[0], _KR_JUMP[1])
            except Exception:
                pass
    except Exception:
        pass


# In-memory cache of the merged settings. Populated from disk on first read and
# refreshed on every save(). Without it, load()/get() re-read + re-parse the JSON
# file on EVERY call — and theme.emoji_icon() calls get() for every icon, so
# building a menu/ribbon/tab fired a burst of disk reads and made the UI sluggish.
_cache: dict | None = None
# Set when settings.json existed but could not be read (it was copied aside, or,
# for a passing lock — the problem's ``transient`` — left as it was), or when the
# last save failed — so the app can say so instead of failing silently. Either way
# save() re-reads the file first and never writes the defaults in memory over it.
load_problem: str | None = None
save_problem: str | None = None
# Changes a save could not write yet (the file was locked): written with the next
# save that can read the file, so nothing the user chose is dropped on the way.
_unsaved: dict = {}


def _read_from_disk() -> dict:
    global load_problem
    data = copy.deepcopy(DEFAULTS)
    saved, problem = read_json(_FILE, dict)
    if problem:
        load_problem = problem
    if saved:
        data.update(saved)
    return data


def load() -> dict:
    """Return the full settings (defaults + saved), from the in-memory cache."""
    global _cache
    if _cache is None:
        _cache = _read_from_disk()
    # a deep copy, so callers can't mutate the cache (nested lists included)
    return copy.deepcopy(_cache)


def save(data: dict) -> None:
    """Change the settings in *data* (a partial dict is fine).

    Only the keys whose value differs from what this process last saw are
    written, on top of the file as it is NOW. Callers often pass the whole dict
    back (``cfg = load(); cfg[k] = v; save(cfg)``, the Settings dialog's every
    field): writing all of it would undo whatever another TurboSSH process saved
    since this one read the file."""
    global _cache, save_problem, load_problem
    seen = load()
    changed = {key: copy.deepcopy(value) for key, value in data.items()
               if key not in seen or seen[key] != value}
    _unsaved.update(changed)
    on_disk, problem = _read_for_save()
    if problem is not None:
        save_problem = problem
        # the running app stays consistent (a live theme switch still applies)
        _cache.update(copy.deepcopy(changed))
        return
    if on_disk:
        load_problem = None          # whatever went wrong at start-up, it reads now
    on_disk.update(copy.deepcopy(_unsaved))
    try:
        atomic_write_json(_FILE, on_disk)
    except Exception as exc:
        save_problem = f"settings could not be saved: {exc}"
    else:
        save_problem = None
        _unsaved.clear()
    merged = copy.deepcopy(DEFAULTS)
    merged.update(on_disk)           # what other processes saved meanwhile, too
    _cache = merged


def _read_bytes() -> bytes:
    """The file's bytes, riding out a short-lived lock (an antivirus or indexer
    holding it, another process saving)."""
    for attempt in range(3):
        try:
            with open(_FILE, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            raise
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def _read_for_save():
    """``(the saved settings on disk now, None)``, or ``(None, why the file must
    not be written over)``. A damaged file is replaced only once its content is
    kept in a ``.corrupt-*`` copy; one that cannot be read at all (locked by
    another program, also when start-up could not read it: the defaults in
    memory then are not what it holds) is left alone."""
    try:
        raw = _read_bytes()
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return None, (f"settings.json could not be read ({exc}), so it was not "
                      f"written over: the change lasts until TurboSSH closes "
                      f"unless a later save can read it")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):   # UnicodeDecodeError is a ValueError
        value = None
    if isinstance(value, dict):
        return value, None
    if _kept_aside(raw) or set_aside(_FILE):
        return {}, None
    return None, ("settings.json is damaged and could not be copied aside, so it "
                  "was not written over")


def _kept_aside(raw: bytes) -> bool:
    """Whether a ``.corrupt-*`` copy of exactly this content exists already
    (start-up made one when it could not read the file): no copy per save."""
    folder, base = os.path.split(_FILE)
    try:
        names = [n for n in os.listdir(folder) if n.startswith(base + ".corrupt-")]
    except OSError:
        return False
    for name in names:
        try:
            with open(os.path.join(folder, name), "rb") as fh:
                if fh.read() == raw:
                    return True
        except OSError:
            pass
    return False


def reload() -> dict:
    """Drop the cache and re-read from disk (rarely needed; the app owns the
    file, so the cache is normally authoritative)."""
    global _cache
    _cache = None
    return load()


def get(key):
    """Return one setting from the cache (no disk hit on the hot path)."""
    global _cache
    if _cache is None:
        _cache = _read_from_disk()
    return _cache.get(key, DEFAULTS.get(key))


def machines() -> list:
    """Saved machines, cleaned: each {name, host, user, domain} with a non-empty
    host. Used to fill the host dropdowns in the session dialog and Camera panel."""
    out = []
    for m in (load().get("machines") or []):
        if not isinstance(m, dict):
            continue
        host = (m.get("host") or "").strip()
        if not host:
            continue
        out.append({"name": (m.get("name") or "").strip(), "host": host,
                    "user": (m.get("user") or "").strip(),
                    "domain": (m.get("domain") or "").strip()})
    return out
