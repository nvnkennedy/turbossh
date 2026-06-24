"""Persisted app settings (theme, fonts, defaults) at ~/.turbossh/settings.json."""

from __future__ import annotations

import os
import json

_DIR = os.path.join(os.path.expanduser("~"), ".turbossh")
_FILE = os.path.join(_DIR, "settings.json")

DEFAULTS = {
    "theme": "dark",            # "dark" | "light"
    "term_font_size": 10,
    "term_font": "Consolas",
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
