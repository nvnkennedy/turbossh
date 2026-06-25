"""Persisted app settings (theme, fonts, defaults) at ~/.turbossh/settings.json."""

from __future__ import annotations

import os
import json

_DIR = os.path.join(os.path.expanduser("~"), ".turbossh")
_FILE = os.path.join(_DIR, "settings.json")

DEFAULTS = {
    "theme": "dark",            # "dark" | "light"
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
    # Camera (remote webcam over SSH via ffmpeg). Off by default — when off the
    # Camera button/menu are hidden entirely. ffmpeg_path is an optional local
    # ffmpeg.exe to use instead of the auto-fetched one (fully offline).
    "camera_enabled": False,
    "ffmpeg_path": "",
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


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(_FILE, "r", encoding="utf-8") as fh:
            data.update(json.load(fh) or {})
    except Exception:
        pass
    return data


def save(data: dict) -> None:
    try:
        os.makedirs(_DIR, exist_ok=True)
        merged = dict(DEFAULTS)
        merged.update(data)
        with open(_FILE, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
    except Exception:
        pass


def get(key):
    return load().get(key, DEFAULTS.get(key))


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
