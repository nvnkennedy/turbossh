"""Persisted app settings (theme, fonts, defaults) in <state dir>/settings.json.

Written atomically, and merged: ``save`` re-reads the file and writes only the
keys this process sets, so two TurboSSH processes (the desktop app and one
hosted in another application, or two windows) never wipe out each other's
saved machines or jump host. What a running window shows (``LIVE``: its theme,
whether the Camera is on) stays this process's own: another process saving a
different one does not switch it under the user. A file that cannot be read is
never written over: a damaged one only once a copy of it is kept (see
``load_problem``), a locked one not at all (see ``save_problem``)."""

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
# What the running window shows. This process's value is the one that counts for
# it, even when another TurboSSH process saved a different one: taking that one
# in switched the window's theme, or closed its Camera tabs, under the user. So
# merging in what the file holds never changes these, and saving one writes it
# whenever the file differs (an explicit "dark" while the file says "light").
LIVE = ("theme", "camera_enabled")


def _read_from_disk() -> dict:
    global load_problem
    data = copy.deepcopy(DEFAULTS)
    saved, problem = read_json(_FILE, dict)
    if problem:
        load_problem = problem
    if saved:
        data.update(saved)
    return data


def _view() -> dict:
    """This process's settings: the cache itself (not a copy), loaded once."""
    global _cache
    if _cache is None:
        _cache = _read_from_disk()
    return _cache


def load() -> dict:
    """Return the full settings (defaults + saved), from the in-memory cache."""
    # a deep copy, so callers can't mutate the cache (nested lists included)
    return copy.deepcopy(_view())


def _merged(saved: dict, view: dict) -> dict:
    """What this process sees once it has read *saved* (the file): the file on
    top of the defaults, except what its window shows (``LIVE``) and its own
    changes that are not written yet."""
    merged = copy.deepcopy(DEFAULTS)
    merged.update(copy.deepcopy(saved))
    for key in LIVE:
        if key in view:
            merged[key] = copy.deepcopy(view[key])
    merged.update(copy.deepcopy(_unsaved))
    return merged


def save(data: dict, force=()) -> None:
    """Set the settings in *data* (a partial dict: what the caller sets).

    Written on top of the file as it is NOW, so what another TurboSSH process
    saved meanwhile stays. A key is written when this process changes it (its
    value differs from what this process had), and a ``LIVE`` key also whenever
    the file holds another value: ``set_theme("dark")`` in a window that shows
    dark must save dark, even if another process saved light since. A key passed
    back unchanged (``cfg = load(); cfg[k] = v; save(cfg)``) is not written, so
    that pattern does not undo another process's changes either.

    Afterwards this process also sees what the others saved, except its
    ``LIVE`` settings: a theme or Camera switch saved by another process does not
    switch this window (it used to, on any save at all).

    Keys in *force* are written like ``LIVE`` ones: whenever the file holds
    another value (see :func:`set_shared_jump`)."""
    global _cache, save_problem, load_problem
    view = _view()
    wanted = {key: copy.deepcopy(value) for key, value in data.items()
              if key in LIVE or key in force or key not in view or view[key] != value}
    view.update(copy.deepcopy(wanted))   # the running app has it at once
    _unsaved.update(wanted)
    on_disk, problem = _read_for_save()
    if problem is not None:
        save_problem = problem           # kept in _unsaved for the next save
        return
    if on_disk:
        load_problem = None          # whatever went wrong at start-up, it reads now
    changes = {key: value for key, value in _unsaved.items()
               if key not in on_disk or on_disk[key] != value}
    try:
        if changes:
            on_disk.update(copy.deepcopy(changes))
            atomic_write_json(_FILE, on_disk)
    except Exception as exc:
        save_problem = f"settings could not be saved: {exc}"
    else:
        save_problem = None
        _unsaved.clear()
    _cache = _merged(on_disk, view)


def refresh() -> dict:
    """Take in what other TurboSSH processes saved since this one last read the
    file, as a save does, and return the settings (``load()``). The Settings
    dialog opens with this: it showed the machines and jump host this process
    read long ago, and saving them wrote that old list over newer ones. What the
    window shows (``LIVE``) and changes not written yet stay this process's. A
    file that cannot be read just now changes nothing."""
    global _cache
    view = _view()
    saved = _peek()
    if saved is not None:
        _cache = _merged(saved, view)
    return load()


def _peek():
    """The saved settings as the file holds them now; None when it cannot be read
    just now. Nothing is copied aside or reported: that is for load and save."""
    try:
        raw = _read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


_JUMP_KEYS = ("jump_host", "jump_user", "jump_domain")


def set_shared_jump(host: str, user: str, domain: str, password: str):
    """Save the shared jump host, user and domain AND its password, together:
    the password (OS vault) belongs to that host and user (settings.json), and
    saving one without the other paired a host with another host's password.
    The three are written whatever this process had cached (another process
    may have saved another jump host since). If settings.json cannot be written
    now, nothing is changed — not the vault, and not later either — and the
    reason is returned (None: saved)."""
    view = _view()
    before = {key: copy.deepcopy(view.get(key)) for key in _JUMP_KEYS}
    save({"jump_host": host, "jump_user": user, "jump_domain": domain},
         force=_JUMP_KEYS)
    if save_problem is not None:
        problem = save_problem
        for key in _JUMP_KEYS:           # not written later without its password
            _unsaved.pop(key, None)
            if before[key] is None:
                view.pop(key, None)
            else:
                view[key] = before[key]
        return problem
    set_jump_password(password)
    return None


def shared_jump() -> dict:
    """The shared jump host in Settings as saved NOW: ``{"host", "user",
    "domain", "password"}``. Read afresh because the password always is (every
    TurboSSH process shares the OS vault): a jump host another process changed
    must not be paired here with the password it saved for its new one."""
    cfg = load()
    saved = _peek()
    if saved is not None:
        cfg = _merged(saved, cfg)
    return {"host": str(cfg.get("jump_host") or "").strip(),
            "user": str(cfg.get("jump_user") or "").strip(),
            "domain": str(cfg.get("jump_domain") or "").strip(),
            "password": jump_password()}


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
    """Drop the cache and re-read from disk, ``LIVE`` settings included (rarely
    needed: :func:`refresh` takes in other processes' changes without switching
    this window's theme or Camera). Changes not written yet stay."""
    global _cache
    _cache = _read_from_disk()
    _cache.update(copy.deepcopy(_unsaved))
    return load()


def get(key):
    """Return one setting from the cache (no disk hit on the hot path)."""
    return _view().get(key, DEFAULTS.get(key))


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
