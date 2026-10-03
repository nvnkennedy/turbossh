"""Persisted app settings (theme, fonts, defaults) in <state dir>/settings.json.

Written atomically; an unreadable file is copied aside (see ``load_problem``)
rather than silently replaced by defaults on the next save."""

from __future__ import annotations

import copy

from .. import paths
from .._jsonstore import atomic_write_json, read_json

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
# Set when settings.json existed but could not be read (it was copied aside), or
# when the last save failed — so the app can say so instead of failing silently.
load_problem: str | None = None
save_problem: str | None = None


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
    # Start from the CURRENT config (defaults + whatever is already saved), not
    # bare DEFAULTS — so saving a partial dict (e.g. only the settings a dialog
    # exposes) preserves keys it didn't touch (compact_ribbon, …).
    global _cache, save_problem
    merged = load()
    merged.update(data)
    try:
        atomic_write_json(_FILE, merged)
        save_problem = None
    except Exception as exc:
        save_problem = f"settings could not be saved: {exc}"
    # Keep the in-memory copy authoritative even if the write failed, so the
    # running app stays consistent (e.g. a live theme switch still takes effect).
    _cache = merged


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
