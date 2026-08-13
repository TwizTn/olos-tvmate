#!/usr/bin/env python3
"""
Olo's TVMate
============
A tiny, zero-dependency local web app for finding which of YOUR Xtream
channels is showing a given football match.

Flow:
  1. Run this (python fixturefinder.py  OR the compiled .exe).
  2. Your browser opens at http://localhost:777
  3. First run: open Settings, paste your Xtream host/port/user/pass, Save,
     then Test login + Reload channels.
  4. Search a team, e.g. "leeds".
  5. It finds that team's fixtures and the channels showing them (Norway, UK,
     and US listings), matches those channel names against your Xtream list,
     and shows the matching channels with a copyable stream URL for VLC.

Source of match/broadcaster data: Fotmob's public tv-guide pages
(https://www.fotmob.com/en-GB/tv-guide/{no,uk,us}), read from the embedded
Schema.org ld+json. Pages are cached so the app only fetches occasionally,
like a person browsing. Fotmob asks that their site not be used by automated
services; this tool is intended for light, personal, single-user use only.

Nothing here provides video. It matches official broadcaster names against
channels YOU already have via YOUR OWN Xtream subscription and hands you the
link. You are responsible for your provider and your rights.

Standard library only, so it can be frozen with:
    pyinstaller --onefile fixturefinder.py
"""

import os
import re
import sys
import subprocess
import json
import time
import html
import difflib
import threading
import webbrowser
import hashlib
import shutil
import datetime
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Config (stored next to the exe/script)
# --------------------------------------------------------------------------

def app_dir():
    """The app's data home - a hidden per-user folder where config, favorites,
    the downloaded script, and any future data files live. Kept out of sight so
    only the .exe is visible to the user."""
    # 1. If the launcher told us the home folder, use it (single source of truth).
    env_home = os.environ.get("TVMATE_HOME")
    if env_home:
        try:
            os.makedirs(env_home, exist_ok=True)
        except Exception:
            pass
        if os.path.isdir(env_home):
            return env_home
    # 2. Otherwise compute the standard per-OS user-data folder.
    home = _default_data_dir()
    try:
        os.makedirs(home, exist_ok=True)
    except Exception:
        pass
    return home

def _default_data_dir():
    """Standard per-user data folder for each OS."""
    name = "OlosTVMate"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, name)
    elif sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), name)
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        return os.path.join(base, name)

CONFIG_PATH = os.path.join(app_dir(), "config.json")
PORT = 777
_CONFIG_LOCK = threading.RLock()
_FAVORITES_LOCK = threading.RLock()
_CACHE_WRITE_LOCK = threading.RLock()

def _atomic_write_bytes(path, raw):
    """Write a complete file beside its destination, then atomically replace it."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temp = path + ".tmp-" + str(os.getpid()) + "-" + str(threading.get_ident())
    try:
        with open(temp, "wb") as f:
            f.write(raw)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(temp, path)
    finally:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass

def _atomic_write_json(path, value, indent=None, compact=False):
    separators = (",", ":") if compact else None
    raw = json.dumps(value, indent=indent, separators=separators,
                     ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(path, raw)

# --- versioning & auto-update ---
VERSION = "0.777.b353"

BANNER = r'''
  ___  _        _     _______     ____  __      __
 / _ \| | ___  ( )__ |_   _\ \   / /  \/  | __ _| |_ ___
| | | | |/ _ \ // __|  | |  \ \ / /| |\/| |/ _` | __/ _ \
| |_| | | (_) |\__ \   | |   \ V / | |  | | (_| | ||  __/
 \___/|_|\___/ |___/   |_|    \_/  |_|  |_|\__,_|\__\___|

          \ | /
       .----------------.
       |   __________   | o
       |  /          \  | o      ~ Technically a TV app ~
       |  | [______] |  | |
       |  | (======) |  |         Spiritually a pancake.
       |  | (======) |  |
       |  |  \____/  |  |
       |  \__________/  |
       '----------------'
          ||        ||
'''
UPDATE_VERSION_URL = "https://raw.githubusercontent.com/TwizTn/olos-tvmate/main/version.txt"
UPDATE_SCRIPT_URL = "https://raw.githubusercontent.com/TwizTn/olos-tvmate/main/tvmate.py"
UPDATE_LAUNCHER_URL = "https://github.com/TwizTn/olos-tvmate/releases/download/v0.777.b30/OTVM.exe"
UPDATE_LAUNCHER_SHA256 = "6a524de87a9e62f896f42addc56d5146a75073f08b5d68a1bb5fcabf8d8438d1"

DEFAULT_CONFIG = {
    "xtream_host": "",
    "xtream_port": "",
    "xtream_user": "",
    "xtream_pass": "",
    "stream_ext": "ts",               # "ts" or "m3u8"
    "match_threshold": 0.62,           # 0..1, higher = stricter
    "countries": ["no", "gb", "us", "es", "de", "it", "fr"],  # NO/UK/US + big-5 league homes
    "check_shows_on_startup": False,
    "refresh_iptv_on_startup": False,
    "refresh_sports_on_startup": False,
    "profile_name": "",
    "preferred_language": "en",
    "profile_emblem": "tvstack",
    "mylist_layout": "timeline",
    "football_enabled": True,
    "f1_enabled": True,
    "racing_series": ["f1"],
    "games_enabled": True,
    "decorations_enabled": True,
    "background_style": "float",
    "hide_cmd_window": True,
    "auto_shutdown_minutes": 0,
    "start_section": "mylist",
    "setup_complete": False,
    "setup_demo_content": False,
    "steam_wishlist_url": "",
    "steam_wishlist_id": "",
    "steam_wishlist_synced_at": 0,
}

def artwork_cache_dir():
    return os.path.join(app_dir(), "artwork")

def artwork_cache_size():
    total = 0
    root = artwork_cache_dir()
    if not os.path.isdir(root):
        return 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total

FOTMOB_TEAM_LOGO = "https://images.fotmob.com/image_resources/logo/teamlogo/{team_id}.png"
FOTMOB_LEAGUE_LOGO = "https://images.fotmob.com/image_resources/logo/leaguelogo/{league_id}.png"

def _team_logo_url(team_id):
    team_id = str(team_id or "").strip()
    return f"/api/team_logo?id={team_id}" if team_id.isdigit() else ""

def _team_logo_path(team_id):
    return os.path.join(artwork_cache_dir(), f"team-{team_id}.png")

def _cache_team_logo(team_id):
    """Download a FotMob team crest once and reuse it from the artwork cache."""
    team_id = str(team_id or "").strip()
    if not team_id.isdigit():
        return ""
    path = _team_logo_path(team_id)
    if os.path.isfile(path):
        return _team_logo_url(team_id)
    try:
        req = urllib.request.Request(FOTMOB_TEAM_LOGO.format(team_id=team_id),
                                     headers={"User-Agent": "OlosTVMate/" + VERSION})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(2 * 1024 * 1024 + 1)
        if not raw or len(raw) > 2 * 1024 * 1024 or not raw.startswith(b"\x89PNG"):
            return ""
        os.makedirs(artwork_cache_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return _team_logo_url(team_id)
    except Exception:
        return ""

def _league_logo_url(league_id):
    league_id = str(league_id or "").strip()
    return f"/api/league_logo?id={league_id}" if league_id.isdigit() else ""

def _league_logo_path(league_id):
    return os.path.join(artwork_cache_dir(), f"league-{league_id}.png")

def _cache_league_logo(league_id):
    """Download a FotMob competition crest once and reuse it from the artwork cache."""
    league_id = str(league_id or "").strip()
    if not league_id.isdigit():
        return ""
    path = _league_logo_path(league_id)
    if os.path.isfile(path):
        return _league_logo_url(league_id)
    try:
        req = urllib.request.Request(FOTMOB_LEAGUE_LOGO.format(league_id=league_id),
                                     headers={"User-Agent": "OlosTVMate/" + VERSION})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(2 * 1024 * 1024 + 1)
        if not raw or len(raw) > 2 * 1024 * 1024 or not raw.startswith(b"\x89PNG"):
            return ""
        os.makedirs(artwork_cache_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return _league_logo_url(league_id)
    except Exception:
        return ""

def _channel_logo_path(stream_id, provider=""):
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(stream_id or ""))
    safe_provider = re.sub(r"[^0-9A-Za-z_-]", "", str(provider or ""))
    prefix = f"channel-{safe_provider}-" if safe_provider else "channel-"
    return os.path.join(artwork_cache_dir(), f"{prefix}{safe_id}.img") if safe_id else ""

def _image_content_type(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""

def _cache_channel_logo(stream_id, url, provider=""):
    """Cache a provider-supplied Xtream channel icon after validating it as an image."""
    path = _channel_logo_path(stream_id, provider)
    if not path or not str(url or "").startswith(("http://", "https://")):
        return ""
    if os.path.isfile(path):
        return path
    try:
        req = urllib.request.Request(str(url), headers={"User-Agent": "OlosTVMate/" + VERSION})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(1024 * 1024 + 1)
        if not raw or len(raw) > 1024 * 1024 or not _image_content_type(raw):
            return ""
        os.makedirs(artwork_cache_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return path
    except Exception:
        return ""

def _latest_episodes_cache_path():
    return os.path.join(artwork_cache_dir(), "latest-episodes.json")

def _latest_episodes_cache_key(x):
    raw = (str(getattr(x, "base", "")) + "|" + str(getattr(x, "user", ""))).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]

def _load_latest_episodes_cache(x):
    try:
        with open(_latest_episodes_cache_path(), "r", encoding="utf-8") as f:
            cached = json.load(f) or {}
        if cached.get("provider") != _latest_episodes_cache_key(x):
            return None
        if not isinstance(cached.get("episodes"), list) or not isinstance(cached.get("upcoming"), list):
            return None
        return cached
    except Exception:
        return None

def _save_latest_episodes_cache(x, episodes, upcoming, errors=0):
    try:
        with _CACHE_WRITE_LOCK:
            _atomic_write_json(_latest_episodes_cache_path(), {
                "provider": _latest_episodes_cache_key(x),
                "saved_at": int(time.time()), "episodes": episodes,
                "upcoming": upcoming, "errors": int(errors or 0)})
    except Exception:
        pass

def _invalidate_latest_episodes_cache():
    try:
        os.remove(_latest_episodes_cache_path())
    except OSError:
        pass

def data_cache_dir():
    return os.path.join(app_dir(), "cache")

def _vod_catalog_cache_path():
    return os.path.join(data_cache_dir(), "vod-catalog.json")

def _vod_cache_key(x):
    raw = (str(getattr(x, "base", "")) + "|" + str(getattr(x, "user", ""))).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]

def _compact_vod_catalog(movies):
    keep = ("stream_id", "name", "container_extension", "year", "rating",
            "stream_icon", "cover", "movie_image", "added", "releaseDate",
            "release_date")
    return [{key: row.get(key) for key in keep if key in row}
            for row in movies if isinstance(row, dict)]

def _load_vod_catalog_cache(x):
    try:
        with open(_vod_catalog_cache_path(), "r", encoding="utf-8") as f:
            cached = json.load(f) or {}
        if cached.get("provider") != _vod_cache_key(x):
            return []
        movies = cached.get("movies") or []
        return movies if isinstance(movies, list) else []
    except Exception:
        return []

def _save_vod_catalog_cache(x, movies):
    movies = _compact_vod_catalog(movies)
    if not movies:
        return []
    try:
        with _CACHE_WRITE_LOCK:
            _atomic_write_json(_vod_catalog_cache_path(), {
                "provider": _vod_cache_key(x), "saved_at": int(time.time()),
                "movies": movies}, compact=True)
    except Exception:
        pass
    return movies

def _load_timed_data_cache(filename, max_age):
    try:
        with open(os.path.join(data_cache_dir(), filename), "r", encoding="utf-8") as f:
            cached = json.load(f) or {}
        if time.time() - float(cached.get("saved_at") or 0) > max_age:
            return None
        return cached.get("data")
    except Exception:
        return None

def _save_timed_data_cache(filename, data):
    try:
        with _CACHE_WRITE_LOCK:
            _atomic_write_json(os.path.join(data_cache_dir(), filename),
                               {"saved_at": time.time(), "data": data}, compact=True)
    except Exception:
        pass

def _remove_data_cache_prefix(prefix):
    try:
        root = data_cache_dir()
        if not os.path.isdir(root):
            return
        for name in os.listdir(root):
            if name.startswith(prefix):
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass

def load_config():
    with _CONFIG_LOCK:
        if not os.path.exists(CONFIG_PATH):
            return dict(DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg or {})
            # Migrate the former Off/IPTV/Other/Everything selector to independent
            # startup actions without changing existing users' saved behaviour.
            if "refresh_iptv_on_startup" not in (cfg or {}):
                legacy_mode = (cfg or {}).get("startup_refresh_mode")
                merged["refresh_iptv_on_startup"] = legacy_mode in ("iptv", "all") or bool((cfg or {}).get("refresh_all_on_startup"))
            if "refresh_sports_on_startup" not in (cfg or {}):
                legacy_mode = (cfg or {}).get("startup_refresh_mode")
                merged["refresh_sports_on_startup"] = legacy_mode in ("other", "all") or bool((cfg or {}).get("refresh_all_on_startup"))
            if "background_style" not in (cfg or {}):
                merged["background_style"] = "float" if merged.get("decorations_enabled", True) else "off"
            if merged.get("background_style") not in ("float", "ascii", "off"):
                merged["background_style"] = "float"
            merged["decorations_enabled"] = merged["background_style"] != "off"
            # Retro console mode is parked for now. Always use the modern launcher.
            merged["hide_cmd_window"] = True
            return merged
        except Exception:
            return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with _CONFIG_LOCK:
        _atomic_write_json(CONFIG_PATH, cfg, indent=2)

FAVORITES_PATH = os.path.join(app_dir(), "favorites.json")

def load_favorites():
    with _FAVORITES_LOCK:
        if not os.path.exists(FAVORITES_PATH):
            return {"categories": [], "channels": [], "movies": [], "shows": [], "games": [], "teams": [],
                    "f1_teams": [], "mylist_channels": []}
        try:
            with open(FAVORITES_PATH, "r", encoding="utf-8") as f:
                fav = json.load(f) or {}
            return {"categories": list(fav.get("categories", [])),
                "channels": list(fav.get("channels", [])),
                "movies": list(fav.get("movies", [])),
                "shows": list(fav.get("shows", [])),
                "games": list(fav.get("games", [])),
                "teams": list(fav.get("teams", [])),
                "f1_teams": list(fav.get("f1_teams", [])),
                    "mylist_channels": list(fav.get("mylist_channels", []))}
        except Exception:
            return {"categories": [], "channels": [], "movies": [], "shows": [], "games": [], "teams": [],
                    "f1_teams": [], "mylist_channels": []}

def save_favorites(fav):
    with _FAVORITES_LOCK:
        _atomic_write_json(FAVORITES_PATH, fav, indent=2)
_PROFILE_SECRET_KEYS = {"xtream_host", "xtream_port", "xtream_user", "xtream_pass"}
_FAVORITE_LIST_KEYS = ("categories", "channels", "movies", "shows", "games",
                       "teams", "f1_teams", "mylist_channels")

def create_profile_backup(kind="profile", timeline=None):
    """Build a portable JSON backup. Profile backups omit Xtream credentials."""
    full = kind == "full"
    cfg = load_config()
    if not full:
        cfg = {key: value for key, value in cfg.items()
               if key not in _PROFILE_SECRET_KEYS}
    return {
        "format": "olos-tvmate-backup",
        "format_version": 1,
        "backup_type": "full" if full else "profile",
        "app_version": VERSION,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config": cfg,
        "favorites": load_favorites(),
        "timeline": timeline if isinstance(timeline, dict) else {},
    }

def _favorite_identity(kind, item):
    if not isinstance(item, dict):
        return str(item).strip().lower()
    fields = {
        "channels": ("stream_id", "name"),
        "movies": ("catalog_id", "stream_id", "name"),
        "shows": ("catalog_id", "show_key", "series_id", "name"),
        "games": ("app_id", "name"),
        "teams": ("team_id", "name"),
        "f1_teams": ("id", "name"),
    }.get(kind, ("id", "name"))
    for field in fields:
        value = str(item.get(field) or "").strip().lower()
        if value:
            return field + ":" + value
    return json.dumps(item, sort_keys=True, ensure_ascii=False)

def _merge_favorite_lists(kind, current, incoming):
    merged, positions = [], {}
    for item in list(current or []) + list(incoming or []):
        identity = _favorite_identity(kind, item)
        if not identity:
            continue
        if identity in positions:
            index = positions[identity]
            if isinstance(merged[index], dict) and isinstance(item, dict):
                merged[index] = dict(merged[index], **item)
        else:
            positions[identity] = len(merged)
            merged.append(item)
    return merged

def _validated_backup_payload(backup):
    """Validate and normalise the durable parts of an imported backup."""
    if not isinstance(backup, dict) or backup.get("format") != "olos-tvmate-backup":
        raise ValueError("This is not a TVMate backup file")
    version = backup.get("format_version")
    if isinstance(version, bool) or version != 1:
        raise ValueError("This TVMate backup version is not supported")
    kind = str(backup.get("backup_type") or "")
    if kind not in ("profile", "full"):
        raise ValueError("Unknown TVMate backup type")
    incoming_cfg = backup.get("config")
    incoming_fav = backup.get("favorites")
    if not isinstance(incoming_cfg, dict) or not isinstance(incoming_fav, dict):
        raise ValueError("The TVMate backup is incomplete")
    if kind == "full":
        missing = [key for key in _FAVORITE_LIST_KEYS
                   if not isinstance(incoming_fav.get(key), list)]
        if missing:
            raise ValueError("The full backup is missing favorite lists: " +
                             ", ".join(missing))
        required = ("xtream_host", "xtream_user", "xtream_pass")
        if any(not str(incoming_cfg.get(key) or "").strip() for key in required):
            raise ValueError("The full backup has incomplete Xtream credentials")
    timeline = backup.get("timeline", {})
    if not isinstance(timeline, dict):
        raise ValueError("The TVMate timeline settings are invalid")
    return kind, incoming_cfg, incoming_fav, timeline

def _prepare_backup_restore(kind, incoming_cfg, incoming_fav,
                            current_cfg, current_fav):
    """Build restored state without touching disk, ready for one transaction."""
    if kind == "profile":
        restored_cfg = dict(current_cfg)
        restored_cfg.update({key: value for key, value in incoming_cfg.items()
                             if key not in _PROFILE_SECRET_KEYS})
    else:
        restored_cfg = dict(DEFAULT_CONFIG)
        restored_cfg.update(incoming_cfg)
    restored_cfg["hide_cmd_window"] = True
    restored_fav = {}
    for key in _FAVORITE_LIST_KEYS:
        incoming = incoming_fav.get(key, [])
        if not isinstance(incoming, list):
            incoming = []
        if kind == "full":
            restored_fav[key] = list(incoming)
        else:
            restored_fav[key] = _merge_favorite_lists(
                key, current_fav.get(key, []), incoming)
    return restored_cfg, restored_fav

def restore_profile_backup(backup):
    """Restore a validated backup, rolling both durable files back on failure."""
    kind, incoming_cfg, incoming_fav, timeline = _validated_backup_payload(backup)
    current_cfg, current_fav = load_config(), load_favorites()
    restored_cfg, restored_fav = _prepare_backup_restore(
        kind, incoming_cfg, incoming_fav, current_cfg, current_fav)
    snapshot = {
        "format": "olos-tvmate-backup", "format_version": 1,
        "backup_type": "full", "app_version": VERSION,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config": current_cfg, "favorites": current_fav}
    pending_path = os.path.join(app_dir(), "profile-import-pending.json")
    recovery_path = os.path.join(app_dir(), "profile-before-import.json")
    _atomic_write_json(pending_path, snapshot, indent=2)
    try:
        save_config(restored_cfg)
        save_favorites(restored_fav)
    except Exception:
        rollback_errors = []
        try:
            save_config(current_cfg)
        except Exception as e:
            rollback_errors.append("configuration: " + str(e))
        try:
            save_favorites(current_fav)
        except Exception as e:
            rollback_errors.append("favorites: " + str(e))
        if rollback_errors:
            raise RuntimeError("Import failed and rollback was incomplete (" +
                               "; ".join(rollback_errors) + ")")
        try:
            os.remove(pending_path)
        except OSError:
            pass
        raise
    try:
        os.replace(pending_path, recovery_path)
    except OSError:
        # The imported profile is already durable. Keep the pending snapshot
        # as recovery data rather than reporting a false import failure.
        pass
    _clear_provider_caches()
    _clear_racing_availability_cache()
    x = Xtream(restored_cfg)
    return {"type": kind, "timeline": timeline,
            "profile_name": str(restored_cfg.get("profile_name") or ""),
            "xtream_configured": x.configured(),
            "counts": {key: len(restored_fav[key]) for key in
                       ("movies", "shows", "games", "teams", "f1_teams", "channels")}}

# --------------------------------------------------------------------------
# HTTP helpers (stdlib only, read as UTF-8)
# --------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

def _source_key_for_url(url):
    """Map a URL's domain to a known source key (or None if untracked)."""
    u = (url or "").lower()
    if "fotmob.com" in u or "images.fotmob.com" in u: return "fotmob"
    if "tvmaze.com" in u: return "tvmaze"
    if "strem.io" in u or "cinemeta" in u: return "cinemeta"
    if "steam" in u: return "steam"
    if "fiaformula2.com" in u: return "f2"
    if "fiaformula3.com" in u: return "f3"
    if "formula1.com" in u or "ergast" in u or "jolpi" in u: return "f1"
    if "indycar.com" in u: return "indycar"
    if "wrc.com" in u or ("wikipedia.org" in u and "rally" in u): return "wrc"
    if "fiaformulae.com" in u: return "formulae"
    if "fiawec.com" in u: return "wec"
    if "motogp.com" in u: return "motogp"
    return None

def http_get_text(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    key = _source_key_for_url(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
        if key:
            _record_source(key, True)
        return text
    except Exception as e:
        if key:
            _record_source(key, False, error=e)
        raise

def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json"})
    key = _source_key_for_url(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if key:
            _record_source(key, True)
        return data
    except Exception as e:
        if key:
            _record_source(key, False, error=e)
        raise

# --------------------------------------------------------------------------
# Source health: remembers the outcome of the last fetch for each external
# source, so Settings can show a green/red panel. Purely passive (updated as
# the app naturally uses each source) plus an optional "test all" trigger.
# --------------------------------------------------------------------------
_SOURCE_HEALTH = {}          # key -> {label, ok, count, error, ts}
_SOURCE_HEALTH_LOCK = threading.Lock()
# Friendly labels + display order for known sources.
_SOURCE_LABELS = [
    ("xtream",   "IPTV provider (Xtream)"),
    ("fotmob",   "Football (Fotmob)"),
    ("epg_xmltv","TV guide / EPG (XMLTV)"),
    ("tvmaze",   "TV shows (TVMaze)"),
    ("cinemeta", "Movie/series info (Cinemeta)"),
    ("steam",    "Steam profile / wishlist"),
    ("f1",       "Formula 1"),
    ("f2",       "Formula 2"),
    ("f3",       "Formula 3"),
    ("indycar",  "IndyCar"),
    ("wrc",      "WRC (rally)"),
    ("formulae", "Formula E"),
    ("wec",      "WEC (endurance)"),
    ("motogp",   "MotoGP"),
]
_SOURCE_LABEL_MAP = dict(_SOURCE_LABELS)

def _record_source(key, ok, count=None, error="", latency_ms=None):
    """Record the outcome of a source fetch. Safe to call from anywhere."""
    try:
        with _SOURCE_HEALTH_LOCK:
            _SOURCE_HEALTH[key] = {
                "label": _SOURCE_LABEL_MAP.get(key, key),
                "ok": None if ok is None else bool(ok),
                "count": count,
                "error": ("" if ok is True else str(error)[:200]),
                "latency_ms": latency_ms,
                "ts": time.time(),
            }
    except Exception:
        pass

def source_health_snapshot():
    """Return the current health of all known sources, in display order."""
    out = []
    with _SOURCE_HEALTH_LOCK:
        for key, label in _SOURCE_LABELS:
            rec = _SOURCE_HEALTH.get(key)
            if rec:
                out.append(dict(rec, key=key))
            else:
                out.append({"key": key, "label": label, "ok": None,
                            "count": None, "error": "", "latency_ms": None, "ts": 0})
    return out

# --------------------------------------------------------------------------
# Xtream client ("login" = authenticated GET; no session/cookies)
# --------------------------------------------------------------------------

# Detect non-playable "category header" rows that some providers inject into
# the live list as visual separators, e.g.:
#   "###### TV2 PLAY ######", "===== SPORT =====", "----- NORWAY -----",
#   "***** VIP *****", "▶▶▶ MOVIES ◀◀◀", "===| ENGLISH |==="
_HEADER_CHARS = "#=*-_~•·▶◀►◄★☆|>< "
_HEADER_EDGE_RE = re.compile(r"^[\s#=*\-_~•·▶◀►◄★☆|<>]{2,}")

def _is_header_row(name):
    if not name:
        return True
    n = name.strip()
    # Starts (or ends) with a run of >=2 separator characters -> header.
    if _HEADER_EDGE_RE.match(n):
        return True
    if re.search(r"[\s#=*\-_~•·▶◀►◄★☆|<>]{2,}$", n):
        return True
    # Almost entirely decoration characters -> header.
    letters = sum(c.isalnum() for c in n)
    decos = sum(c in _HEADER_CHARS for c in n)
    if letters == 0:
        return True
    if decos >= letters and decos >= 4:
        return True
    return False

class Xtream:
    def __init__(self, cfg):
        host = (cfg.get("xtream_host") or "").strip().rstrip("/")
        port = str(cfg.get("xtream_port") or "").strip()
        if host and not host.startswith(("http://", "https://")):
            host = "http://" + host
        base = host
        if port and not re.search(r":\d+$", base):
            base = f"{base}:{port}"
        self.base = base
        self.user = (cfg.get("xtream_user") or "").strip()
        self.password = (cfg.get("xtream_pass") or "").strip()
        self.ext = (cfg.get("stream_ext") or "ts").strip() or "ts"

    def configured(self):
        return bool(self.base and self.user and self.password)

    def _api(self, action=None):
        q = {"username": self.user, "password": self.password}
        if action:
            q["action"] = action
        return f"{self.base}/player_api.php?" + urllib.parse.urlencode(q)

    def xmltv_url(self):
        """Bulk XMLTV EPG file URL for this provider."""
        q = {"username": self.user, "password": self.password}
        return f"{self.base}/xmltv.php?" + urllib.parse.urlencode(q)

    def login(self):
        try:
            info = http_get_json(self._api())
        except Exception as e:
            return False, f"Could not reach server: {e}"
        ui = (info or {}).get("user_info", {}) or {}
        if ui.get("auth", 0) == 1:
            return True, {"status": ui.get("status"), "exp": ui.get("exp_date"),
                          "active": ui.get("active_cons"), "max": ui.get("max_connections")}
        return False, "Login failed (check username/password)."

    def live_streams(self):
        data = http_get_json(self._api("get_live_streams"))
        out = []
        for s in data or []:
            name = s.get("name", "") or ""
            if _is_header_row(name):
                continue   # skip category separators / non-playable labels
            out.append({"stream_id": s.get("stream_id"),
                        "name": name,
                        "category_id": str(s.get("category_id", "")),
                        "epg_channel_id": str(s.get("epg_channel_id") or "").strip(),
                        "stream_icon": str(s.get("stream_icon") or "").strip()})
        return out

    def categories(self):
        data = http_get_json(self._api("get_live_categories"))
        return {str(c.get("category_id")): c.get("category_name", "")
                for c in (data or [])}

    def vod_streams(self):
        data = http_get_json(self._api("get_vod_streams"), timeout=45)
        return data if isinstance(data, list) else []

    def movie_url(self, stream_id, extension="mp4"):
        ext = re.sub(r"[^a-zA-Z0-9]", "", str(extension or "mp4")) or "mp4"
        return f"{self.base}/movie/{self.user}/{self.password}/{stream_id}.{ext}"

    def series(self):
        data = http_get_json(self._api("get_series"), timeout=45)
        return data if isinstance(data, list) else []

    def series_info(self, series_id, refresh=False):
        cache_key = (self.base, self.user, str(series_id))
        now = time.time()
        cached = _SHOW_INFO_CACHE.get(cache_key)
        if (not refresh and cached and
                now - cached.get("ts", 0) < _SHOW_INFO_TTL):
            return cached.get("data") or {}
        q = {"username": self.user, "password": self.password,
             "action": "get_series_info", "series_id": str(series_id)}
        if refresh:
            q["_"] = str(int(time.time()))
        data = http_get_json(f"{self.base}/player_api.php?" + urllib.parse.urlencode(q), timeout=45)
        _SHOW_INFO_CACHE[cache_key] = {"ts": now, "data": data}
        return data

    def episode_url(self, episode_id, extension="mp4"):
        ext = re.sub(r"[^a-zA-Z0-9]", "", str(extension or "mp4")) or "mp4"
        return f"{self.base}/series/{self.user}/{self.password}/{episode_id}.{ext}"

    def stream_url(self, stream_id):
        return f"{self.base}/live/{self.user}/{self.password}/{stream_id}.{self.ext}"

    def hls_url(self, stream_id):
        return f"{self.base}/live/{self.user}/{self.password}/{stream_id}.m3u8"

    def short_epg(self, stream_id, limit=12):
        """Fetch short EPG for one stream. Titles are base64 in Xtream."""
        import base64, calendar, datetime
        q = {"username": self.user, "password": self.password,
             "action": "get_short_epg", "stream_id": str(stream_id), "limit": str(limit)}
        url = f"{self.base}/player_api.php?" + urllib.parse.urlencode(q)
        # Let transport/provider failures propagate to the EPG cache layer.
        # An empty successful response really means "no EPG"; an exception
        # must not be turned into [] because that would erase good cached data.
        data = http_get_json(url)
        rows = (data or {}).get("epg_listings", []) if isinstance(data, dict) else []

        def _dec(v):
            if not v:
                return ""
            try:
                # only decode if it looks like base64
                dec = base64.b64decode(v).decode("utf-8", "replace")
                return dec
            except Exception:
                return str(v)

        def _to_ts(val):
            """Accept unix seconds (int/str) or 'YYYY-MM-DD HH:MM:SS' -> unix seconds."""
            if val is None or val == "":
                return None
            # numeric unix timestamp
            try:
                iv = int(str(val).strip())
                if iv > 100000000:   # plausible epoch
                    return iv
            except Exception:
                pass
            # date string
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.datetime.strptime(str(val).strip(), fmt)
                    # Xtream date strings are typically UTC
                    return calendar.timegm(dt.timetuple())
                except Exception:
                    continue
            return None

        out = []
        for e in rows:
            start_ts = (_to_ts(e.get("start_timestamp")) or _to_ts(e.get("start_timestamp_"))
                        or _to_ts(e.get("start")))
            stop_ts = (_to_ts(e.get("stop_timestamp")) or _to_ts(e.get("end_timestamp"))
                       or _to_ts(e.get("stop")) or _to_ts(e.get("end")))
            out.append({
                "title": _dec(e.get("title")),
                "desc": _dec(e.get("description")),
                "start": e.get("start"),
                "end": e.get("end") or e.get("stop"),
                "start_ts": start_ts,
                "stop_ts": stop_ts,
            })
        return out

_XT_CACHE = {"provider": "", "ts": 0, "channels": [], "cats": {}}
_VOD_CACHE = {"provider": "", "ts": 0, "movies": []}
_SERIES_CACHE = {"provider": "", "ts": 0, "shows": []}
_SHOW_INFO_CACHE = {}  # (provider,user,series_id) -> {ts,data}
_TVMAZE_CACHE = {}  # normalized title/year -> {"ts": epoch, "covers": {season:url}}
_XT_TTL = 24 * 3600       # catalogs stay local for the session/day; manual refresh overrides
_SHOW_INFO_TTL = 24 * 3600
_EPG_CACHE = {}   # stream_id -> {"ts": epoch, "programmes": [...]}
_EPG_REFRESH_TTL = 12 * 3600       # freshness threshold; manual EPG refresh always overrides
_EPG_DISK_RETENTION = 7 * 24 * 3600  # stale-while-offline fallback for multi-day guide use
_EPG_LISTING_LIMIT = 168              # ask Xtream for several days where the provider supports it
_EPG_DISK_PROVIDER = None

def _clear_provider_caches():
    """Invalidate all in-memory data tied to the configured Xtream account."""
    global _EPG_DISK_PROVIDER
    _XT_CACHE.update({"provider": "", "ts": 0, "channels": [], "cats": {}})
    _VOD_CACHE.update({"provider": "", "ts": 0, "movies": []})
    _SERIES_CACHE.update({"provider": "", "ts": 0, "shows": []})
    _SHOW_INFO_CACHE.clear()
    _EPG_CACHE.clear()
    _EPG_DISK_PROVIDER = None
    try:
        _clear_racing_availability_cache()
        _clear_sports_event_channel_cache()
    except NameError:
        # The helper is declared later in this single-file module. This only
        # matters to import-time tooling; normal requests run after load.
        pass

def _load_epg_disk_cache(x):
    """Hydrate the small EPG cache once per configured provider."""
    global _EPG_DISK_PROVIDER
    provider = _vod_cache_key(x)
    if _EPG_DISK_PROVIDER == provider:
        return
    _EPG_CACHE.clear()
    cached = _load_timed_data_cache("epg-cache.json", _EPG_DISK_RETENTION)
    if isinstance(cached, dict) and cached.get("provider") == provider:
        entries = cached.get("entries") or {}
        if isinstance(entries, dict):
            now = time.time()
            for sid, row in entries.items():
                if (isinstance(row, dict) and isinstance(row.get("programmes"), list)
                        and now - float(row.get("ts") or 0) < _EPG_DISK_RETENTION):
                    _EPG_CACHE[str(sid)] = row
    _EPG_DISK_PROVIDER = provider

def _save_epg_disk_cache(x):
    _save_timed_data_cache("epg-cache.json", {
        "provider": _vod_cache_key(x), "entries": _EPG_CACHE})

def _epg_cache_has_coverage(row, now=None):
    """True when cached guide rows still contain a current or future item.

    Cache age alone is insufficient: some Xtream short-EPG responses contain
    only a few hours, so a recently downloaded row may already be exhausted.
    """
    now = float(now or time.time())
    programmes = row.get("programmes") if isinstance(row, dict) else None
    if not isinstance(programmes, list) or not programmes:
        return False
    for programme in programmes:
        if not isinstance(programme, dict):
            continue
        try:
            start = float(programme.get("start_ts") or 0)
        except (TypeError, ValueError):
            start = 0
        try:
            stop = float(programme.get("stop_ts") or 0)
        except (TypeError, ValueError):
            stop = 0
        if stop > now or start >= now:
            return True
    return False

# --- Bulk XMLTV EPG (one download for all channels) ------------------------
# Many Xtream providers expose xmltv.php which returns the full guide in a
# single ~50MB file. This is dramatically faster and more reliable than making
# one get_short_epg request per channel. We stream-parse it and keep only the
# channels the caller asks for (mapped by the channel's epg_channel_id).
_XMLTV_TS_RE = None

def _xmltv_parse_ts(val):
    """Parse an XMLTV time like '20260808170000 +0000' -> unix seconds."""
    if not val:
        return None
    import calendar
    try:
        s = val.strip()
        # format: YYYYMMDDHHMMSS optionally followed by ' +ZZZZ'
        base = s[:14]
        dt = datetime.datetime.strptime(base, "%Y%m%d%H%M%S")
        offset = 0
        rest = s[14:].strip()
        if rest and (rest[0] in "+-") and len(rest) >= 5:
            sign = 1 if rest[0] == "+" else -1
            hh = int(rest[1:3]); mm = int(rest[3:5])
            offset = sign * (hh * 3600 + mm * 60)
        return calendar.timegm(dt.timetuple()) - offset
    except Exception:
        return None

def fetch_xmltv_epg(x, wanted_epg_ids, timeout=90):
    """Download and parse bulk XMLTV while keeping large payloads off RAM."""
    import xml.etree.ElementTree as _ET
    import gzip as _gzip, tempfile as _tempfile, zlib as _zlib
    wanted = set(str(w) for w in wanted_epg_ids if w)
    if not wanted:
        return {}
    req = urllib.request.Request(x.xmltv_url(), headers={
        "User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    with _tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as xml_file:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in encoding:
                source = _gzip.GzipFile(fileobj=resp)
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    xml_file.write(chunk)
            elif "deflate" in encoding:
                decoder = _zlib.decompressobj()
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    xml_file.write(decoder.decompress(chunk))
                xml_file.write(decoder.flush())
            else:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    xml_file.write(chunk)
        xml_file.seek(0)
        head = xml_file.read(160).lower()
        if b"<html" in head or b"<!doctype" in head:
            raise ValueError("xmltv.php returned HTML, not XML (blocked/redirect)")
        xml_file.seek(0)
        out = {}
        for _event, elem in _ET.iterparse(xml_file, events=("end",)):
            tag = elem.tag.lower()
            if tag == "programme":
                ch = elem.get("channel", "")
                if ch in wanted:
                    title_el = elem.find("title")
                    desc_el = elem.find("desc")
                    out.setdefault(ch, []).append({
                        "title": (title_el.text if title_el is not None else "") or "",
                        "desc": (desc_el.text if desc_el is not None else "") or "",
                        "start": elem.get("start", ""),
                        "end": elem.get("stop", ""),
                        "start_ts": _xmltv_parse_ts(elem.get("start")),
                        "stop_ts": _xmltv_parse_ts(elem.get("stop")),
                    })
                elem.clear()
            elif tag == "channel":
                elem.clear()
    for ch in out:
        out[ch].sort(key=lambda p: p.get("start_ts") or 0)
    return out

def probe_xmltv(x, timeout=20):
    """Cheaply verify that the configured XMLTV endpoint starts as XML."""
    import gzip as _gzip, zlib as _zlib
    req = urllib.request.Request(x.xmltv_url(), headers={
        "User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            head = _gzip.GzipFile(fileobj=resp).read(4096)
        elif "deflate" in encoding:
            head = _zlib.decompressobj().decompress(resp.read(8192), 4096)
        else:
            head = resp.read(4096)
    low = head.lstrip().lower()
    if not (low.startswith(b"<?xml") or low.startswith(b"<tv")):
        raise ValueError("xmltv.php did not return an XMLTV document")
    return True


def _fetch_text(url, timeout=8):
    """Fetch a URL as text, or None on any failure (offline, 404, etc.)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OlosTVMate-Updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None

def _update_manifest():
    """Return the published version and optional SHA-256 from version.txt."""
    text = _fetch_text(UPDATE_VERSION_URL)
    if not text:
        return "", ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    version = lines[0] if lines else ""
    checksum = lines[1].lower() if len(lines) > 1 else ""
    if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        checksum = ""
    return version, checksum

def _parse_ver(v):
    """Turn '0.777.b1' into a comparable tuple. Higher = newer."""
    v = (v or "").strip().lstrip("v").strip()
    # split into numeric-ish and build parts: 0.777.b1 -> (0,777, 'b', 1)
    import re as _re
    nums = _re.findall(r"\d+", v)
    # base numbers plus trailing build number; letters ignored for ordering except build
    try:
        return tuple(int(n) for n in nums)
    except Exception:
        return ()

def check_for_update():
    """Return (update_available, remote_version) comparing GitHub version.txt to VERSION."""
    remote, _checksum = _update_manifest()
    if not remote:
        return (False, None)
    try:
        newer = _parse_ver(remote) > _parse_ver(VERSION)
    except Exception:
        newer = (remote != VERSION)
    return (newer, remote)

def download_update():
    """Download and validate a new tvmate.py. Return its local path or None."""
    remote_version, expected_sha = _update_manifest()
    text = _fetch_text(UPDATE_SCRIPT_URL, timeout=30)
    if not remote_version or not text or len(text.encode("utf-8")) < 100000:
        return None
    try:
        # Normalize line endings: strip any CR so we don't end up with \r\r\n
        # (doubled carriage returns) which makes the Windows console double-space
        # / stretch the ASCII banner. Write with newline="" so Python doesn't
        # translate again.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        marker = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if not marker or marker.group(1).strip() != remote_version:
            return None
        compile(text, "tvmate_new.py", "exec")
        raw = text.encode("utf-8")
        if expected_sha and hashlib.sha256(raw).hexdigest().lower() != expected_sha:
            return None
        dest = os.path.join(app_dir(), "tvmate_new.py")
        _atomic_write_bytes(dest, raw)
        return dest
    except Exception:
        return None

def _file_sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest().lower()
    except Exception:
        return ""

def _download_launcher_update(launcher_exe):
    """Download and verify the current Windows launcher next to the old one."""
    if not sys.platform.startswith("win") or not launcher_exe:
        return ""
    launcher_exe = os.path.abspath(launcher_exe)
    if not os.path.isfile(launcher_exe):
        return ""
    if _file_sha256(launcher_exe) == UPDATE_LAUNCHER_SHA256:
        return ""
    dest = launcher_exe + ".new"
    try:
        req = urllib.request.Request(UPDATE_LAUNCHER_URL,
                                     headers={"User-Agent": "OlosTVMate-Updater/" + VERSION})
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read(20 * 1024 * 1024 + 1)
        if not raw or len(raw) > 20 * 1024 * 1024 or not raw.startswith(b"MZ"):
            return ""
        if hashlib.sha256(raw).hexdigest().lower() != UPDATE_LAUNCHER_SHA256:
            return ""
        with open(dest, "wb") as f:
            f.write(raw)
        return dest
    except Exception:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        return ""

def _schedule_launcher_replacement(launcher_exe, downloaded):
    """Replace a running Windows launcher after it exits, then relaunch it."""
    if not sys.platform.startswith("win") or not launcher_exe or not downloaded:
        return False
    launcher_exe = os.path.abspath(launcher_exe)
    downloaded = os.path.abspath(downloaded)
    if _file_sha256(downloaded) != UPDATE_LAUNCHER_SHA256:
        return False
    folder = os.path.dirname(launcher_exe)
    old_name = os.path.basename(launcher_exe)
    new_name = os.path.basename(downloaded)
    # Keep the forced-kill fallback tightly scoped. Support the current and
    # legacy names plus the numeric suffix browsers add for duplicate
    # downloads (OTVM(2).exe / OTVM (2).exe), but never taskkill an arbitrary
    # renamed executable from the environment.
    if not re.fullmatch(r"(?:OTVM|OlosTVMate)(?:\s*\(\d+\))?\.exe", old_name,
                        flags=re.IGNORECASE):
        return False
    helper = os.path.join(folder, "_tvmate_launcher_update.bat")
    try:
        # The old Nuitka onefile parent can remain alive after the Python app
        # has stopped and keep OTVM.exe locked. Give the normal shutdown a
        # moment, then terminate only the known launcher image before retrying
        # the verified replacement.
        lines = [
            "@echo off\r\n",
            'cd /d "%~dp0"\r\n',
            "setlocal\r\n",
            "timeout /t 3 /nobreak >nul\r\n",
            'taskkill /f /im "' + old_name + '" >nul 2>&1\r\n',
            "timeout /t 1 /nobreak >nul\r\n",
            "for /l %%I in (1,1,30) do (\r\n",
            '  move /y "' + new_name + '" "' + old_name + '" >nul 2>&1 && goto replaced\r\n',
            "  timeout /t 1 /nobreak >nul\r\n",
            ")\r\n",
            "exit /b 1\r\n",
            ":replaced\r\n",
            'start "" "' + old_name + '"\r\n',
            'del "%~f0"\r\n',
        ]
        with open(helper, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.Popen(["cmd.exe", "/d", "/c", helper], cwd=folder,
                         creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        return True
    except Exception:
        return False

def _launcher_is_current():
    launcher_exe = os.environ.get("TVMATE_EXE", "").strip()
    return bool(launcher_exe and os.path.isfile(launcher_exe) and
                _file_sha256(launcher_exe) == UPDATE_LAUNCHER_SHA256)

def _start_launcher_migration():
    """Prepare the one-time old-console -> GUI launcher migration."""
    if not sys.platform.startswith("win"):
        return False
    launcher_exe = os.environ.get("TVMATE_EXE", "").strip()
    if not launcher_exe or not os.path.isfile(launcher_exe):
        return False
    if _file_sha256(launcher_exe) == UPDATE_LAUNCHER_SHA256:
        return False
    downloaded = _download_launcher_update(launcher_exe)
    if downloaded and _schedule_launcher_replacement(launcher_exe, downloaded):
        return True
    return False

def _find_vlc():
    """Locate the VLC executable across common OS install paths."""
    import shutil, os, sys
    # PATH first
    for name in ("vlc", "vlc.exe"):
        p = shutil.which(name)
        if p:
            return p
    candidates = []
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                candidates.append(os.path.join(base, "VideoLAN", "VLC", "vlc.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/VLC.app/Contents/MacOS/VLC")
    else:
        candidates += ["/usr/bin/vlc", "/usr/local/bin/vlc", "/snap/bin/vlc",
                       "/var/lib/flatpak/exports/bin/org.videolan.VLC"]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None

def _stream_icon_for_id(stream_id):
    sid = str(stream_id or "")
    for ch in _XT_CACHE.get("channels", []):
        if str(ch.get("stream_id")) == sid:
            return str(ch.get("stream_icon") or "").strip()
    try:
        for ch in load_favorites().get("channels", []):
            if str(ch.get("stream_id")) == sid:
                return str(ch.get("logo") or "").strip()
    except Exception:
        pass
    return ""

def _sync_favorite_channel_icons(channels):
    """Backfill provider icon URLs into existing favorites without re-favoriting."""
    if not os.path.isfile(FAVORITES_PATH):
        return
    icons = {str(ch.get("stream_id")): str(ch.get("stream_icon") or "").strip()
             for ch in channels if ch.get("stream_icon")}
    if not icons:
        return
    fav = load_favorites()
    changed = False
    for ch in fav.get("channels", []):
        icon = icons.get(str(ch.get("stream_id")))
        if icon and ch.get("logo") != icon:
            ch["logo"] = icon
            changed = True
    if changed:
        save_favorites(fav)

def get_xtream_channels(cfg, force=False):
    now = time.time()
    x = Xtream(cfg)
    provider = _vod_cache_key(x)
    if ((not force) and _XT_CACHE.get("provider") == provider and
            _XT_CACHE["channels"] and (now - _XT_CACHE["ts"] < _XT_TTL)):
        return _XT_CACHE["channels"], _XT_CACHE["cats"]
    if force:
        _clear_racing_availability_cache()
    channels = x.live_streams()
    try:
        cats = x.categories()
    except Exception:
        cats = {}
    _XT_CACHE.update({"provider": provider, "ts": now, "channels": channels, "cats": cats})
    _sync_favorite_channel_icons(channels)
    return channels, cats

def get_xtream_movies(cfg, force=False):
    now = time.time()
    x = Xtream(cfg)
    provider = _vod_cache_key(x)
    if (not force) and _VOD_CACHE.get("provider") == provider and _VOD_CACHE["movies"]:
        return _VOD_CACHE["movies"]
    disk_movies = _load_vod_catalog_cache(x)
    if not force and disk_movies:
        _VOD_CACHE.update({"provider": provider, "ts": now, "movies": disk_movies})
        return disk_movies
    movies = x.vod_streams()
    if movies:
        movies = _save_vod_catalog_cache(x, movies)
    elif disk_movies:
        movies = disk_movies
    _VOD_CACHE.update({"provider": provider, "ts": now, "movies": movies})
    return movies

def get_xtream_series(cfg, force=False):
    now = time.time()
    x = Xtream(cfg)
    provider = _vod_cache_key(x)
    if ((not force) and _SERIES_CACHE.get("provider") == provider and
            _SERIES_CACHE["shows"] and (now - _SERIES_CACHE["ts"] < _XT_TTL)):
        return _SERIES_CACHE["shows"]
    shows = x.series()
    _SERIES_CACHE.update({"provider": provider, "ts": now, "shows": shows})
    return shows

def refresh_favorite_show_episodes(cfg):
    """Refresh favorite-show episode counts and report newly added episodes."""
    x = Xtream(cfg)
    if not x.configured():
        _invalidate_latest_episodes_cache()
        return {"new_episodes": 0, "refreshed": 0, "errors": 0}
    fav = load_favorites()
    try:
        series_catalog = get_xtream_series(cfg)
    except Exception:
        series_catalog = []
    new_episodes = 0
    refreshed = 0
    errors = 0
    for show in fav.get("shows", []):
        series_ids = show.get("series_ids") or [show.get("series_id")]
        series_ids = [sid for sid in series_ids if sid is not None]
        title_key = str(show.get("show_key") or _show_key(show.get("name")))
        siblings = [row.get("series_id") for row in series_catalog
                    if _show_key(row.get("name")) == title_key and row.get("series_id") is not None]
        if siblings:
            series_ids = siblings
            show["series_ids"] = siblings
            show["series_id"] = siblings[0]
        if not series_ids:
            continue
        try:
            counts = []
            for series_id in series_ids:
                data = x.series_info(series_id, refresh=True) or {}
                raw_eps = data.get("episodes") or {}
                if isinstance(raw_eps, dict):
                    counts.append(sum(len(eps) for eps in raw_eps.values()
                                      if isinstance(eps, list)))
                elif isinstance(raw_eps, list):
                    counts.append(len(raw_eps))
            # Variants are alternate sources, not additional episodes.
            count = max(counts) if counts else 0
            previous = show.get("episode_count")
            if previous is not None and count > int(previous):
                new_episodes += count - int(previous)
            show["episode_count"] = count
            show["episodes_checked_at"] = int(time.time())
            refreshed += 1
        except Exception:
            errors += 1
    save_favorites(fav)
    _invalidate_latest_episodes_cache()
    if errors and not refreshed:
        raise RuntimeError("Could not refresh favorite shows")
    return {"new_episodes": new_episodes, "refreshed": refreshed,
            "errors": errors}

def _clean_show_title(name):
    title = str(name or "").strip()
    # Provider prefixes are catalog-defined and can change. Treat the first
    # explicit dash/pipe segment as the source label instead of maintaining a list.
    title = re.sub(r"^.+?\s+-\s+", "", title, count=1)
    title = re.sub(r"^.{1,40}?\s*\|\s*", "", title, count=1)
    title = re.sub(r"\s*\((?:19|20)\d{2}\).*?$", "", title)
    title = re.sub(r"\s*\((?:US|UK|GB|NO|EN|SE|DK|FI)\)\s*$", "", title, flags=re.I)
    return title.strip(" -|")

def _show_key(name):
    return re.sub(r"[^a-z0-9]+", "-", _clean_show_title(name).lower()).strip("-")

def _show_variant_label(name):
    """Short provider/quality label shown on an episode play button."""
    text = str(name or "").strip()
    match = re.match(r"^(.+?)\s+-\s+", text)
    if match:
        return match.group(1).strip()
    match = re.match(r"^(.{1,40}?)\s*\|\s*", text)
    return match.group(1).strip() if match else "PLAY"

def _clean_episode_title(title, show_name):
    """Remove provider/quality clutter while retaining season and episode names."""
    raw = str(title or "").strip()
    clean_show = _clean_show_title(show_name)
    marker = re.search(r"\bS\d{1,2}E\d{1,3}\b.*", raw, flags=re.I)
    if marker:
        suffix = re.sub(r"^S\d{1,2}E\d{1,3}\b", "", marker.group(0),
                        flags=re.I).strip(" -:|")
        if clean_show and suffix:
            return f"{clean_show} - {suffix}"
        return clean_show or suffix or "Episode"
    raw = re.sub(r"^[A-Z0-9+]+(?:-[A-Z0-9+]+)*\s+-\s+", "", raw, flags=re.I)
    return raw or "Episode"

def _latest_provider_variant(x, series_id, fallback_name="Show"):
    """Return the newest provider episode for one series variant."""
    data = x.series_info(series_id) or {}
    info = data.get("info") or {}
    if not isinstance(info, dict):
        info = {}
    variant_name = str(info.get("name") or info.get("title") or fallback_name)
    raw_episodes = data.get("episodes") or {}
    if isinstance(raw_episodes, list):
        grouped = {}
        for episode in raw_episodes:
            grouped.setdefault(str(episode.get("season") or 1), []).append(episode)
        raw_episodes = grouped
    candidates = []
    for season_key, episodes in raw_episodes.items():
        if not isinstance(episodes, list):
            continue
        try:
            season_num = int(season_key)
        except (TypeError, ValueError):
            season_num = 0
        for index, episode in enumerate(episodes, 1):
            try:
                episode_num = int(episode.get("episode_num") or index)
            except (TypeError, ValueError):
                episode_num = index
            candidates.append((season_num, episode_num, episode))
    if not candidates:
        return None, info
    season_num, episode_num, episode = max(candidates, key=lambda item: (item[0], item[1]))
    try:
        added = int(float(episode.get("added") or 0))
    except (TypeError, ValueError):
        added = 0
    episode_info = episode.get("info") or {}
    if not isinstance(episode_info, dict):
        episode_info = {}
    date_text = " ".join(str(value or "") for value in
                         (episode_info.get("releaseDate"), episode_info.get("releasedate"),
                          episode_info.get("air_date"), episode.get("releaseDate")))
    episode_ts = 0
    date_match = re.search(r"(?:19|20)\d{2}-\d{2}-\d{2}", date_text)
    if date_match:
        try:
            episode_ts = time.mktime(time.strptime(date_match.group(0), "%Y-%m-%d"))
        except (ValueError, OverflowError):
            pass
    if not episode_ts and added > 100000000:
        episode_ts = added
    return ({"id": episode.get("id"), "key": (season_num, episode_num),
             "season": season_num, "episode_num": episode_num,
             "title": _clean_episode_title(
                 episode.get("title") or f"Episode {episode_num}", variant_name),
             "extension": episode.get("container_extension") or "mp4",
             "label": _show_variant_label(variant_name), "added": episode_ts}, info)

def _tvmaze_episode_schedule(show_name, year="", force=False):
    """Return latest aired and next 14-day episode from one 24-hour disk cache."""
    clean = _clean_show_title(show_name)
    wanted = re.sub(r"[^a-z0-9]", "", clean.lower())
    wanted_year = str(year or "")[:4]
    digest = hashlib.sha256((wanted + "|" + wanted_year).encode("utf-8")).hexdigest()[:16]
    cache_dir = os.path.join(app_dir(), "artwork", "tvmaze-" + digest)
    cache_path = os.path.join(cache_dir, "episode-schedule.json")
    now = time.time()
    if not force:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f) or {}
            if now - float(cached.get("checked_at") or 0) < 24 * 3600:
                return cached.get("schedule") or {"latest": {}, "upcoming": {}}
        except Exception:
            pass
    schedule = {"latest": {}, "upcoming": {}}
    try:
        results = http_get_json("https://api.tvmaze.com/search/shows?" +
                                urllib.parse.urlencode({"q": clean}), timeout=12)
        best, best_score = None, -1
        for row in results or []:
            show = row.get("show") or {}
            candidate = re.sub(r"[^a-z0-9]", "",
                               str(show.get("name") or "").lower())
            if candidate != wanted:
                continue
            score = float(row.get("score") or 0)
            premiered = str(show.get("premiered") or "")[:4]
            if wanted_year and premiered == wanted_year:
                score += 5
            if score > best_score:
                best, best_score = show, score
        if best and best.get("id") is not None:
            episodes = http_get_json(
                f"https://api.tvmaze.com/shows/{best['id']}/episodes?specials=0",
                timeout=12)
            aired, upcoming = [], []
            today = time.strftime("%Y-%m-%d")
            for ep in episodes or []:
                airdate = str(ep.get("airdate") or "")[:10]
                if not airdate:
                    continue
                season = ep.get("season")
                number = ep.get("number")
                if season is None or number is None:
                    continue
                airstamp = str(ep.get("airstamp") or "")
                air_ts = 0
                if airstamp:
                    try:
                        air_ts = datetime.datetime.fromisoformat(
                            airstamp.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        air_ts = 0
                row = (int(season), int(number), airdate, ep)
                has_aired = air_ts <= now if air_ts else airdate <= today
                within_14_days = air_ts <= now + 14 * 86400 if air_ts else True
                if has_aired:
                    aired.append(row)
                elif within_14_days:
                    upcoming.append(row)
            if aired:
                season, number, airdate, ep = max(
                    aired, key=lambda item: (item[0], item[1], item[2]))
                schedule["latest"] = {
                    "season": season, "episode_num": number,
                    "title": str(ep.get("name") or f"Episode {number}"),
                    "airdate": airdate, "airstamp": str(ep.get("airstamp") or "")}
            if upcoming:
                season, number, airdate, ep = min(
                    upcoming, key=lambda item: (item[2], item[0], item[1]))
                schedule["upcoming"] = {
                    "season": season, "episode_num": number,
                    "title": str(ep.get("name") or f"Episode {number}"),
                    "airdate": airdate, "airstamp": str(ep.get("airstamp") or "")}
    except Exception:
        schedule = {"latest": {}, "upcoming": {}}
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"checked_at": now, "schedule": schedule}, f, indent=2)
    except Exception:
        pass
    return schedule

def _tvmaze_season_covers(show_name, year=""):
    """Best-effort, no-key season artwork lookup. Failures return an empty map."""
    clean = _clean_show_title(show_name)
    wanted_year = str(year or "")[:4]
    key = (re.sub(r"[^a-z0-9]", "", clean.lower()), wanted_year)
    digest = hashlib.sha256((key[0] + "|" + key[1]).encode("utf-8")).hexdigest()[:16]
    art_dir = os.path.join(app_dir(), "artwork", "tvmaze-" + digest)
    manifest_path = os.path.join(art_dir, "manifest.json")
    now = time.time()
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f) or {}
        stored = {}
        for season, filename in (manifest.get("covers") or {}).items():
            if os.path.exists(os.path.join(art_dir, filename)):
                stored[str(season)] = f"/api/season_art?show={digest}&season={season}"
        if stored:
            return stored
        if manifest.get("checked") and now - float(manifest.get("checked_at") or 0) < 24 * 3600:
            return {}
    except Exception:
        pass
    cached = _TVMAZE_CACHE.get(key)
    if cached and now - cached["ts"] < 24 * 3600:
        return cached["covers"]
    covers = {}
    try:
        results = http_get_json("https://api.tvmaze.com/search/shows?" +
                                urllib.parse.urlencode({"q": clean}), timeout=12)
        wanted = key[0]
        best, best_score = None, -1
        for row in results or []:
            show = row.get("show") or {}
            candidate = re.sub(r"[^a-z0-9]", "", str(show.get("name") or "").lower())
            premiered = str(show.get("premiered") or "")[:4]
            score = float(row.get("score") or 0)
            if candidate == wanted:
                score += 10
            if wanted_year and premiered == wanted_year:
                score += 5
            if score > best_score:
                best, best_score = show, score
        if best and best.get("id") is not None:
            seasons = http_get_json(f"https://api.tvmaze.com/shows/{best['id']}/seasons", timeout=12)
            for season in seasons or []:
                number = season.get("number")
                image = season.get("image") or {}
                url = image.get("medium") or image.get("original")
                if number is not None and str(url or "").startswith(("http://", "https://")):
                    covers[str(number)] = url
    except Exception:
        covers = {}
    stored = {}
    filenames = {}
    try:
        os.makedirs(art_dir, exist_ok=True)
        for season, url in covers.items():
            filename = f"season-{season}.jpg"
            path = os.path.join(art_dir, filename)
            if not os.path.exists(path):
                req = urllib.request.Request(url, headers={"User-Agent": "OlosTVMate/" + VERSION})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read(5 * 1024 * 1024 + 1)
                if not raw or len(raw) > 5 * 1024 * 1024:
                    continue
                with open(path, "wb") as f:
                    f.write(raw)
            filenames[str(season)] = filename
            stored[str(season)] = f"/api/season_art?show={digest}&season={season}"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"checked": True, "checked_at": now,
                       "title": clean, "year": wanted_year,
                       "covers": filenames}, f, indent=2)
    except Exception:
        stored = covers
    _TVMAZE_CACHE[key] = {"ts": now, "covers": stored}
    return stored

# --------------------------------------------------------------------------
# Fotmob tv-guide source (Schema.org ld+json embedded in the page)
# --------------------------------------------------------------------------

FOTMOB_TVGUIDE = "https://www.fotmob.com/en-GB/tv-guide/{country}"
FOTMOB_TEAM_API = "https://www.fotmob.com/api/data/teams?id={team_id}&ccode3=NOR"
FOTMOB_TEAM_SEARCH = "https://www.fotmob.com/api/data/search/suggest?term={term}"
FOTMOB_DAILY_MATCHES = "https://www.fotmob.com/api/data/matches?date={date}&ccode3=NOR"

# Fotmob uses ISO-ish slugs; "uk" is commonly typed but the real page is "gb".
_CC_ALIAS = {"uk": "gb", "en": "gb", "gbr": "gb", "usa": "us", "nor": "no"}
_CC_DISPLAY = {"gb": "UK", "us": "US", "no": "NO", "es": "ES", "de": "DE",
               "it": "IT", "fr": "FR"}

def _norm_cc(cc):
    cc = (cc or "").strip().lower()
    return _CC_ALIAS.get(cc, cc)

def _display_cc(cc):
    cc = cc.upper()
    return _CC_DISPLAY.get(cc.lower(), cc)
_TV_CACHE = {}          # country -> {"ts": float, "fixtures": [...]}
_TV_TTL = 6 * 3600      # broadcaster listings persist for 6 hours
_TEAM_FIXTURE_CACHE = {}  # team id -> {"ts": float, "fixtures": [...]}
_TEAM_FIXTURE_TTL = 7 * 24 * 3600  # future schedules persist for 7 days
_TEAM_PROFILE_CACHE = {}  # team id -> {"ts": float, "profile": {...}}
_TEAM_PROFILE_CACHE_SCHEMA = 2  # b257: real FotMob venue/coach/profile paths
_TEAM_ID_CACHE = {}       # normalized favorite name -> FotMob team id
_DAILY_MATCH_CACHE = {"date": "", "ts": 0, "matches": []}
_DAILY_MATCH_TTL = 120    # current/live matches: refresh every 2 minutes

# F1 calendars and constructor lists change far less often than live football.
# Keep them warm for a week; the manual content refresh bypasses this cache.
_F1_SCHEDULE_CACHE = {"ts": 0, "events": []}
_F1_TEAMS_CACHE = {"ts": 0, "teams": []}
_F1_TTL = 7 * 24 * 3600
_CINEMETA_CACHE = {}
_CINEMETA_TTL = 6 * 3600
_CINEMETA_CATALOG_TTL = 24 * 3600

def _cinemeta_released_movie(row, now=None):
    """Exclude known future/undated current-year titles from browse shelves."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    released = str((row or {}).get("released") or "").strip()
    if released:
        try:
            value = datetime.datetime.fromisoformat(released.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.timezone.utc)
            return value <= now
        except (TypeError, ValueError):
            pass
    year = _catalog_year(row)
    if year.isdigit():
        return int(year) < now.year
    return False

def cinemeta_search(kind, term):
    kind = "series" if kind == "series" else "movie"
    term = str(term or "").strip()
    if not term:
        return []
    key = (kind, term.lower())
    cached = _CINEMETA_CACHE.get(key)
    if cached and time.time() - cached["ts"] < _CINEMETA_TTL:
        return cached["data"]
    url = ("https://v3-cinemeta.strem.io/catalog/" + kind + "/top/search=" +
           urllib.parse.quote(term, safe="") + ".json")
    data = http_get_json(url, timeout=15)
    rows = (data.get("metas") or [])[:30]
    _CINEMETA_CACHE[key] = {"ts": time.time(), "data": rows}
    return rows

def cinemeta_movie_catalog(catalog="popular", limit=10):
    """Return a small browseable Cinemeta movie shelf."""
    catalog = str(catalog or "popular").lower()
    limit = max(1, min(30, int(limit or 10)))
    if catalog == "new":
        year = str(time.localtime().tm_year)
        endpoint = "year/genre=" + year
    elif catalog == "featured":
        endpoint = "imdbRating"
    else:
        catalog = "popular"
        endpoint = "top"
    key = ("catalog", "movie", catalog)
    cached = _CINEMETA_CACHE.get(key)
    if cached and time.time() - cached["ts"] < _CINEMETA_CATALOG_TTL:
        return cached["data"][:limit]
    cache_suffix = (year if catalog == "new" else "all") + "-released-v2"
    disk = _load_timed_data_cache(
        f"cinemeta-movie-{catalog}-{cache_suffix}.json", _CINEMETA_CATALOG_TTL)
    if isinstance(disk, list) and disk:
        _CINEMETA_CACHE[key] = {"ts": time.time(), "data": disk}
        return disk[:limit]
    url = "https://v3-cinemeta.strem.io/catalog/movie/" + endpoint + ".json"
    data = http_get_json(url, timeout=15)
    rows = [row for row in (data.get("metas") or [])
            if _cinemeta_released_movie(row)]
    if catalog == "new":
        rows.sort(key=lambda row: str(row.get("released") or ""), reverse=True)
    _CINEMETA_CACHE[key] = {"ts": time.time(), "data": rows}
    if rows:
        _save_timed_data_cache(f"cinemeta-movie-{catalog}-{cache_suffix}.json", rows)
    return rows[:limit]

def cinemeta_meta(kind, catalog_id):
    kind = "series" if kind == "series" else "movie"
    catalog_id = str(catalog_id or "").strip()
    if not re.fullmatch(r"tt\d+", catalog_id):
        return {}
    key = ("meta", kind, catalog_id)
    cached = _CINEMETA_CACHE.get(key)
    if cached and time.time() - cached["ts"] < _CINEMETA_TTL:
        return cached["data"]
    url = f"https://v3-cinemeta.strem.io/meta/{kind}/{catalog_id}.json"
    data = (http_get_json(url, timeout=15).get("meta") or {})
    _CINEMETA_CACHE[key] = {"ts": time.time(), "data": data}
    return data

def _catalog_year(row):
    match = re.search(r"(?:19|20)\d{2}", str(row.get("releaseInfo") or row.get("year") or ""))
    return match.group(0) if match else ""

def _provider_year(row):
    text = " ".join(str(row.get(k) or "") for k in
                    ("year", "releaseDate", "release_date", "name"))
    match = re.search(r"(?:19|20)\d{2}", text)
    return match.group(0) if match else ""

def match_vod_sources(catalog_row, movies):
    wanted = _show_key(catalog_row.get("name"))
    year = str(catalog_row.get("year") or _catalog_year(catalog_row) or "")
    out = []
    for movie in movies or []:
        if _show_key(movie.get("name")) != wanted:
            continue
        provider_year = _provider_year(movie)
        if year and provider_year and year != provider_year:
            continue
        out.append({"stream_id": movie.get("stream_id"),
                    "extension": movie.get("container_extension") or "mp4",
                    "label": _show_variant_label(movie.get("name"))})
    return out

def resolve_steam_wishlist_id(value):
    value = str(value or "").strip()
    numeric = re.search(r"/(?:profiles)/(\d{17})(?:/|$)", value)
    if numeric:
        return numeric.group(1)
    if re.fullmatch(r"\d{17}", value):
        return value
    vanity_match = re.search(r"/(?:wishlist/)?id/([^/?#]+)", value, flags=re.I)
    vanity = urllib.parse.unquote(vanity_match.group(1)) if vanity_match else value
    vanity = vanity.strip(" /")
    if not vanity or "/" in vanity:
        return ""
    page = http_get_text("https://steamcommunity.com/id/" + urllib.parse.quote(vanity, safe=""),
                         timeout=15)
    match = re.search(r'"steamid"\s*:\s*"(\d{17})"', page)
    return match.group(1) if match else ""

def steam_wishlist_items(steam_id):
    steam_id = str(steam_id or "").strip()
    if not re.fullmatch(r"\d{17}", steam_id):
        return []
    data = http_get_json("https://api.steampowered.com/IWishlistService/GetWishlist/v1/?steamid=" +
                         urllib.parse.quote(steam_id), timeout=15)
    return ((data.get("response") or {}).get("items") or [])

def _steam_avatar_path(steam_id):
    safe = re.sub(r"[^0-9]", "", str(steam_id or ""))
    return os.path.join(artwork_cache_dir(), f"steam-avatar-{safe}.img") if safe else ""

def _cache_steam_avatar(steam_id, image_url, force=False):
    path = _steam_avatar_path(steam_id)
    if not path or not str(image_url or "").startswith(("http://", "https://")):
        return ""
    if os.path.isfile(path) and not force:
        return "/api/steam_avatar?id=" + urllib.parse.quote(str(steam_id))
    try:
        req = urllib.request.Request(str(image_url),
                                     headers={"User-Agent": "OlosTVMate/" + VERSION})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(2 * 1024 * 1024 + 1)
        if not raw or len(raw) > 2 * 1024 * 1024 or not _image_content_type(raw):
            return ""
        os.makedirs(artwork_cache_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return "/api/steam_avatar?id=" + urllib.parse.quote(str(steam_id))
    except Exception:
        return ""

def _steam_html_text(fragment, keep_lines=False):
    # Steam XML summaries frequently contain escaped HTML.  Unescape first so
    # encoded <br> tags become real line breaks instead of visible "<br>" text.
    text = html.unescape(str(fragment or ""))
    text = re.sub(r"<br\s*/?>", "\n" if keep_lines else " ", text,
                  flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    if keep_lines:
        return "\n".join(re.sub(r"\s+", " ", line).strip()
                           for line in text.splitlines() if line.strip())
    return re.sub(r"\s+", " ", text).strip()

def steam_public_profile(steam_id, force=False):
    """Fetch a public Steam Community identity without requiring an API key."""
    steam_id = str(steam_id or "").strip()
    if not re.fullmatch(r"\d{17}", steam_id):
        return {}
    cache_name = "steam-profile.json"
    if not force:
        cached = _load_timed_data_cache(cache_name, 7 * 24 * 3600)
        if (isinstance(cached, dict) and cached.get("_v") == 4 and
                str(cached.get("steam_id") or "") == steam_id):
            local_avatar = _cache_steam_avatar(steam_id, cached.get("avatar"), force=False)
            if local_avatar:
                cached["avatar_local"] = local_avatar
            return cached
    profile_url = f"https://steamcommunity.com/profiles/{steam_id}/"
    out = {"_v": 4, "steam_id": steam_id, "profile_url": profile_url}
    try:
        import xml.etree.ElementTree as ET
        xml_text = http_get_text(profile_url + "?xml=1", timeout=15)
        root = ET.fromstring(xml_text)
        def xtext(name):
            node = root.find(name)
            return str(node.text or "").strip() if node is not None else ""
        out.update({"display_name": xtext("steamID"), "real_name": xtext("realname"),
                    "location": xtext("location"),
                    "summary": _steam_html_text(xtext("summary"), keep_lines=True),
                    "avatar": xtext("avatarFull") or xtext("avatarMedium"),
                    "member_since": xtext("memberSince")})
        member_since = out.get("member_since") or ""
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                joined = datetime.datetime.strptime(member_since, fmt).date()
                today = datetime.datetime.now().date()
                years = today.year - joined.year - ((today.month, today.day) <
                                                     (joined.month, joined.day))
                out["years_service"] = max(0, years)
                break
            except ValueError:
                continue
    except Exception:
        pass
    # Steam level and several identity fields are also present in the normal
    # public profile.  Parse these precisely because animated avatar frames sit
    # next to the real avatar and must never be mistaken for the user picture.
    try:
        page = http_get_text(profile_url, timeout=15)
        pdata = re.search(r'g_rgProfileData\s*=\s*(\{.*?\})\s*;', page,
                          flags=re.I | re.S)
        if pdata:
            try:
                profile_data = json.loads(pdata.group(1))
                out["display_name"] = out.get("display_name") or str(profile_data.get("personaname") or "").strip()
                out["summary"] = out.get("summary") or str(profile_data.get("summary") or "").strip()
                out["profile_url"] = str(profile_data.get("url") or out["profile_url"])
            except Exception:
                pass
        level = re.search(r'friendPlayerLevelNum[^>]*>\s*(\d+)\s*<', page, flags=re.I)
        if level:
            out["level"] = int(level.group(1))
        # Keep this scoped to Steam badge descriptions.  Searching the whole
        # page can accidentally join the profile level (e.g. 22) to the next
        # "Years of Service" label.
        for badge in re.findall(r'badge_info_description[^>]*>(.*?)</div>',
                                page, flags=re.I | re.S):
            service = re.search(r'\b(\d{1,2})\s+Years?\s+of\s+Service\b',
                                _steam_html_text(badge), flags=re.I)
            if service:
                out["years_service"] = int(service.group(1))
                break
        if not out.get("display_name"):
            name = re.search(r'actual_persona_name[^>]*>(.*?)<', page, flags=re.I | re.S)
            if name:
                out["display_name"] = html.unescape(re.sub(r"<[^>]+>", "", name.group(1))).strip()
        real_block = re.search(r'header_real_name[^>]*>(.*?)</div>', page,
                               flags=re.I | re.S)
        if real_block:
            real = re.search(r'<bdi>(.*?)</bdi>', real_block.group(1), flags=re.I | re.S)
            if real and not out.get("real_name"):
                out["real_name"] = _steam_html_text(real.group(1))
            # Steam commonly prints the location after the <bdi> real name in
            # this same block (next to the country flag), rather than using a
            # dedicated header_location element.
            if not out.get("location"):
                tail = re.sub(r'^.*?</bdi>', '', real_block.group(1), count=1,
                              flags=re.I | re.S)
                location_text = _steam_html_text(tail)
                if location_text and location_text != out.get("real_name"):
                    out["location"] = location_text
        if not out.get("location"):
            location = re.search(r'header_location[^>]*>(.*?)</div>', page,
                                 flags=re.I | re.S)
            if location:
                out["location"] = _steam_html_text(location.group(1))
        if not out.get("summary"):
            summary = re.search(r'profile_summary[^>]*>(.*?)</div>', page,
                                flags=re.I | re.S)
            if summary:
                out["summary"] = _steam_html_text(summary.group(1), keep_lines=True)
        if not out.get("avatar"):
            avatar = None
            # image_src and og:image are Steam's canonical public-profile
            # portrait metadata and cannot be confused with an animated frame.
            for pattern in (
                r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',
                r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']image_src["\']',
                r'playerAvatarAutoSizeInner[^>]*>[\s\S]{0,500}?<img[^>]+src=["\']([^"\']+)["\']',
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'):
                avatar = re.search(pattern, page, flags=re.I | re.S)
                if avatar:
                    break
            if avatar:
                out["avatar"] = html.unescape(avatar.group(1))
    except Exception:
        pass
    if out.get("years_service") is None:
        try:
            badges = _steam_html_text(http_get_text(profile_url + "badges/1/", timeout=12))
            member = re.search(r'Member since\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})', badges,
                               flags=re.I)
            if member:
                out["member_since"] = member.group(1)
                joined = datetime.datetime.strptime(member.group(1), "%B %d, %Y").date()
                today = datetime.datetime.now().date()
                out["years_service"] = max(0, today.year - joined.year -
                                             ((today.month, today.day) <
                                              (joined.month, joined.day)))
        except Exception:
            pass
    if out.get("avatar"):
        local_avatar = _cache_steam_avatar(steam_id, out.get("avatar"), force=force)
        if local_avatar:
            out["avatar_local"] = local_avatar
    if any(out.get(k) for k in ("display_name", "real_name", "avatar", "location")):
        _save_timed_data_cache(cache_name, out)
    return out

def steam_store_items(app_ids):
    app_ids = [str(app_id) for app_id in app_ids if str(app_id).isdigit()]
    out = []
    for start in range(0, len(app_ids), 50):
        chunk = app_ids[start:start + 50]
        payload = {"ids": [{"appid": int(app_id)} for app_id in chunk],
                   "context": {"language": "english", "country_code": "NO", "steam_realm": 1},
                   "data_request": {"include_assets": True, "include_release": True}}
        url = ("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json=" +
               urllib.parse.quote(json.dumps(payload, separators=(",", ":")), safe=""))
        data = http_get_json(url, timeout=20)
        for item in ((data.get("response") or {}).get("store_items") or []):
            if not item.get("success"):
                continue
            app_id = str(item.get("appid") or item.get("id") or "")
            assets = item.get("assets") or {}
            fmt = str(assets.get("asset_url_format") or "")
            filename = str(assets.get("header") or assets.get("main_capsule") or "")
            cover = ""
            if fmt and filename:
                path = fmt.replace("${FILENAME}", filename)
                cover = "https://shared.fastly.steamstatic.com/store_item_assets/" + path.lstrip("/")
            release_ts = int((item.get("release") or {}).get("steam_release_date") or 0)
            released = ""
            release_text = ""
            if release_ts > 0:
                dt = datetime.datetime.fromtimestamp(release_ts, tz=datetime.timezone.utc)
                # Steam commonly represents a year-only future window as Dec 31.
                # Keep the year visible, but don't schedule a false Timeline event.
                if release_ts > time.time() and dt.month == 12 and dt.day == 31:
                    release_text = str(dt.year)
                else:
                    released = dt.isoformat()
                    release_text = dt.strftime("%d %b, %Y")
            out.append({"app_id": app_id, "name": item.get("name") or "Game",
                        "cover": cover, "released": released, "release_text": release_text,
                        "url": f"https://store.steampowered.com/app/{app_id}/"})
    return out

def _f1_api(path):
    return http_get_json("https://api.jolpi.ca/ergast/f1/" + path.lstrip("/"), timeout=15)

def _f1_iso(date_text, time_text=""):
    date_text = str(date_text or "").strip()
    time_text = str(time_text or "00:00:00Z").strip() or "00:00:00Z"
    if not date_text:
        return ""
    return date_text + "T" + time_text.replace("Z", "+00:00")

def get_f1_schedule(force=False):
    now = time.time()
    if (not force and _F1_SCHEDULE_CACHE["events"] and
            now - _F1_SCHEDULE_CACHE["ts"] < _F1_TTL):
        return _F1_SCHEDULE_CACHE["events"]
    if not force:
        disk = _load_timed_data_cache("f1-schedule.json", _F1_TTL)
        if isinstance(disk, list) and disk and any(row.get("art") for row in disk):
            _F1_SCHEDULE_CACHE.update({"ts": now, "events": disk})
            return disk
    year = datetime.datetime.now().year
    data = _f1_api(f"{year}.json?limit=100")
    races = (((data.get("MRData") or {}).get("RaceTable") or {}).get("Races") or [])
    labels = (("FirstPractice", "Practice 1"), ("SecondPractice", "Practice 2"),
              ("ThirdPractice", "Practice 3"), ("SprintQualifying", "Sprint Qualifying"),
              ("SprintShootout", "Sprint Shootout"), ("Sprint", "Sprint"),
              ("Qualifying", "Qualifying"))
    events = []
    for race in races:
        location = ((race.get("Circuit") or {}).get("Location") or {})
        country = str(location.get("country") or "").strip()
        art = ("https://media.formula1.com/image/upload/c_lfill,w_800/q_auto/"
               "v1740000001/content/dam/fom-website/2018-redesign-assets/"
               "Racehub%20header%20images%2016x9/" +
               urllib.parse.quote(country, safe="") + ".webp") if country else ""
        base = {"round": race.get("round", ""), "race": race.get("raceName", "Formula 1"),
                "circuit": ((race.get("Circuit") or {}).get("circuitName") or ""),
                "art": art}
        for key, label in labels:
            session = race.get(key) or {}
            stamp = _f1_iso(session.get("date"), session.get("time"))
            if stamp:
                events.append(dict(base, session=label, start=stamp))
        stamp = _f1_iso(race.get("date"), race.get("time"))
        if stamp:
            events.append(dict(base, session="Race", start=stamp))
    events.sort(key=lambda row: row.get("start") or "")
    _F1_SCHEDULE_CACHE.update({"ts": now, "events": events})
    _save_timed_data_cache("f1-schedule.json", events)
    return events

def get_f1_teams(force=False):
    now = time.time()
    if (not force and _F1_TEAMS_CACHE["teams"] and
            now - _F1_TEAMS_CACHE["ts"] < _F1_TTL):
        return _F1_TEAMS_CACHE["teams"]
    if not force:
        disk = _load_timed_data_cache("f1-teams.json", _F1_TTL)
        if isinstance(disk, list) and disk:
            _F1_TEAMS_CACHE.update({"ts": now, "teams": disk})
            return disk
    year = datetime.datetime.now().year
    data = _f1_api(f"{year}/constructors.json?limit=100")
    rows = (((data.get("MRData") or {}).get("ConstructorTable") or {}).get("Constructors") or [])
    teams = [{"id": str(row.get("constructorId") or ""), "name": row.get("name") or "",
              "url": row.get("url") or ""} for row in rows if row.get("constructorId")]
    teams.sort(key=lambda row: row["name"].lower())
    _F1_TEAMS_CACHE.update({"ts": now, "teams": teams})
    _save_timed_data_cache("f1-teams.json", teams)
    return teams

def get_f1_team_drivers(constructor_id, force=False):
    """Current race drivers for a selected F1 constructor, cached for a week."""
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(constructor_id or ""))
    if not safe:
        return []
    filename = f"f1-drivers-{safe}.json"
    if not force:
        disk = _load_timed_data_cache(filename, _F1_TTL)
        if isinstance(disk, list) and disk:
            return disk
    year = datetime.datetime.now().year
    data = _f1_api(f"{year}/constructors/{safe}/drivers.json?limit=100")
    rows = (((data.get("MRData") or {}).get("DriverTable") or {}).get("Drivers") or [])
    # Jolpica may also expose reserve/test drivers. Race drivers have a three-letter code.
    drivers = []
    for row in rows:
        if not row.get("driverId") or not row.get("code"):
            continue
        name = (str(row.get("givenName") or "") + " " + str(row.get("familyName") or "")).strip()
        drivers.append({"id": str(row.get("driverId")), "name": name,
                        "code": str(row.get("code") or ""), "team_id": safe,
                        "url": "https://www.formula1.com/en/drivers/" +
                               re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
                        "wiki": str(row.get("url") or "")})
    _save_timed_data_cache(filename, drivers)
    return drivers

def get_racing_drivers(force=False):
    """Personal racing people shown in My Racing/My Sports."""
    cfg, fav = load_config(), load_favorites()
    selected = {str(x).lower() for x in cfg.get("racing_series", ["f1"])}
    drivers = []
    if "f1" in selected:
        for team in fav.get("f1_teams", []):
            if not isinstance(team, dict) or not team.get("id"):
                continue
            try:
                for row in get_f1_team_drivers(team.get("id"), force=force):
                    item = dict(row)
                    item.update({"key": "f1-" + row["id"], "series": "f1",
                                 "series_name": "Formula 1", "team": team.get("name") or ""})
                    drivers.append(item)
            except Exception:
                continue
    if "wrc" in selected:
        drivers.append({"key": "wrc-oliver-solberg", "id": "oliver-solberg",
                        "name": "Oliver Solberg", "series": "wrc", "series_name": "WRC",
                        "team": "Toyota Gazoo Racing", "wiki": "Oliver_Solberg",
                        "team_logo": "https://upload.wikimedia.org/wikipedia/commons/1/15/Toyota_Gazoo_Racing_stacked_logo.svg",
                        "url": "https://www.wrc.com/en"})
    if "indycar" in selected:
        drivers.append({"key": "indycar-dennis-hauger", "id": "dennis-hauger",
                        "name": "Dennis Hauger", "series": "indycar", "series_name": "IndyCar",
                        "team": "Dale Coyne Racing", "wiki": "Dennis_Hauger",
                        "team_logo": "https://upload.wikimedia.org/wikipedia/en/4/45/Dale_coyne_racing_logo.png",
                        "url": "https://www.indycar.com/Drivers/Dennis-Hauger"})
    if "f2" in selected:
        drivers.append({"key": "f2-martinius-stenshorne", "id": "martinius-stenshorne",
                        "name": "Martinius Stenshorne", "series": "f2", "series_name": "Formula 2",
                        "team": "Rodin Motorsport", "wiki": "Martinius_Stenshorne",
                        "team_logo": "https://upload.wikimedia.org/wikipedia/commons/e/e3/Rodin_Motorsport_logo.svg",
                        "url": "https://www.fiaformula2.com/en/drivers"})
    return drivers

def _driver_picture_path(key):
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(key or ""))
    return os.path.join(artwork_cache_dir(), f"racing-driver-{safe}.img") if safe else ""

def _cache_racing_driver_picture(driver):
    """Cache a compact profile image; F1 uses F1's current official driver artwork."""
    path = _driver_picture_path(driver.get("key"))
    if path and os.path.isfile(path):
        return path
    image = ""
    try:
        if driver.get("series") == "f1":
            raw_page = http_get_text("https://www.formula1.com/en/drivers", timeout=20)
            name = str(driver.get("name") or "")
            anchor = 'href="/en/drivers/' + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + '"'
            pos = raw_page.find(anchor)
            segment = raw_page[pos:pos + 14000] if pos >= 0 else ""
            pic = re.search(r'<img\s+src="([^"]+)"\s+alt="' + re.escape(name) + r'"', segment, re.I)
            image = html.unescape(pic.group(1)) if pic else ""
        if not image:
            title = str(driver.get("wiki") or "").strip()
            if title.startswith("http"):
                title = urllib.parse.unquote(urllib.parse.urlparse(title).path.rsplit("/", 1)[-1])
            if title:
                info = http_get_json("https://en.wikipedia.org/api/rest_v1/page/summary/" +
                                     urllib.parse.quote(title, safe=""), timeout=12)
                image = ((info.get("thumbnail") or {}).get("source") or
                         (info.get("originalimage") or {}).get("source") or "")
        if not image:
            return ""
        req = urllib.request.Request(image, headers={"User-Agent": "OlosTVMate/" + VERSION})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(2 * 1024 * 1024 + 1)
        if not raw or len(raw) > 2 * 1024 * 1024 or not _image_content_type(raw):
            return ""
        os.makedirs(artwork_cache_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return path
    except Exception:
        return ""

def _racing_weekend_date(value, year):
    """Turn an official F2/F3 date range such as '04 - 06 SEP' into its first day."""
    m = re.search(r"(\d{1,2})\s*(?:-\s*\d{1,2}\s*)?([A-Za-z]{3})", str(value or ""))
    if not m:
        return ""
    try:
        dt = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%d %b %Y")
        return dt.replace(hour=12, tzinfo=datetime.timezone.utc).isoformat()
    except ValueError:
        return ""

def get_fia_racing_weekends(series, force=False):
    """Read the official FIA F2/F3 season page; weekend dates are cached for a week."""
    series = str(series or "").lower()
    if series not in ("f2", "f3"):
        return []
    filename = f"racing-{series}.json"
    if not force:
        disk = _load_timed_data_cache(filename, _F1_TTL)
        if isinstance(disk, list) and disk:
            return disk
    year = datetime.datetime.now().year
    host = "www.fiaformula2.com" if series == "f2" else "www.fiaformula3.com"
    raw = http_get_text(f"https://{host}/en/racing/{year}", timeout=25)
    pattern = re.compile(
        rf'href="(/en/racing/{year}/[^"?#]+)"[^>]*>([^<]+)</a>'
        r'.{0,700}?class="[^"]*zkX7ha_date[^"]*"[^>]*>([^<]+)</span>', re.I | re.S)
    rows, seen = [], set()
    for match in pattern.finditer(raw):
        path, circuit, dates = match.groups()
        if path in seen:
            continue
        start = _racing_weekend_date(html.unescape(dates).strip(), year)
        if not start:
            continue
        seen.add(path)
        rows.append({"series": series, "series_name": series.upper(),
                     "race": html.unescape(circuit).strip(), "session": "Race weekend",
                     "circuit": html.unescape(circuit).strip(), "start": start,
                     "all_day": True, "date_text": html.unescape(dates).strip(),
                     "url": f"https://{host}{path}"})
    rows.sort(key=lambda row: row.get("start") or "")
    _save_timed_data_cache(filename, rows)
    return rows

def _indycar_et_iso(date_text, time_text, year):
    """Convert IndyCar's ET schedule time to UTC without requiring a timezone package."""
    clean_time = re.sub(r"\s*ET\s*$", "", str(time_text or ""), flags=re.I).strip()
    try:
        local = datetime.datetime.strptime(f"{date_text} {year} {clean_time}", "%b %d %Y %I:%M %p")
    except ValueError:
        return ""
    # US Eastern DST: second Sunday in March through first Sunday in November.
    def sunday(month, first_after):
        d = datetime.date(year, month, first_after)
        return d + datetime.timedelta(days=(6 - d.weekday()) % 7)
    dst_start, dst_end = sunday(3, 8), sunday(11, 1)
    offset = -4 if dst_start <= local.date() < dst_end else -5
    return local.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=offset))).astimezone(datetime.timezone.utc).isoformat()

def get_indycar_schedule(force=False):
    """Read the official IndyCar schedule page and cache race events for a week."""
    if not force:
        disk = _load_timed_data_cache("racing-indycar.json", _F1_TTL)
        if isinstance(disk, list) and disk:
            return disk
    year = datetime.datetime.now().year
    raw = http_get_text("https://www.indycar.com/Schedule", timeout=25)
    header = re.compile(r'class="event-card-header-date"[^>]*>([^<]+)</div>.{0,700}?'
                        r'class="event-card-header-time"[^>]*>([^<]+)</div>', re.I | re.S)
    rows, seen = [], set()
    for match in header.finditer(raw):
        segment = raw[match.end():match.end() + 6500]
        link = re.search(rf'href="(/Schedule/{year}/[^"?#]+)"[^>]*class="event-card-link"', segment, re.I)
        title = re.search(r'class="event-card-title"[^>]*>(.*?)</h3>', segment, re.I | re.S)
        track = re.search(r'class="event-card-track-name"[^>]*>(.*?)</div>', segment, re.I | re.S)
        if not (link and title):
            continue
        path = link.group(1)
        if path in seen:
            continue
        start = _indycar_et_iso(html.unescape(match.group(1)).strip(), html.unescape(match.group(2)).strip(), year)
        if not start:
            continue
        seen.add(path)
        clean = lambda value: html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()
        rows.append({"series": "indycar", "series_name": "IndyCar",
                     "race": clean(title.group(1)), "session": "Race",
                     "circuit": clean(track.group(1)) if track else "", "start": start,
                     "all_day": False, "date_text": clean(match.group(1)) + " · " + clean(match.group(2)),
                     "url": "https://www.indycar.com" + path})
    rows.sort(key=lambda row: row.get("start") or "")
    _save_timed_data_cache("racing-indycar.json", rows)
    return rows

def _wrc_wikipedia_schedule(year):
    """Fallback WRC calendar when WRC.com's intermittently protected page is unavailable."""
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "parse", "page": f"{year} World Rally Championship",
        "prop": "wikitext", "format": "json", "formatversion": "2"})
    data = http_get_json(url, timeout=15)
    text = str(((data.get("parse") or {}).get("wikitext")) or "")
    calendar = text[text.find("==Calendar=="):]
    if "===Calendar changes===" in calendar:
        calendar = calendar.split("===Calendar changes===", 1)[0]
    rows = []
    for block in re.split(r'\n\|-\s*\n', calendar):
        match = re.search(r'^!(\d+)\s*$\n\|([^\n]+)\n\|([^\n]+)\n\|([^\n]+)', block, re.M)
        if not match:
            continue
        start_text, end_text, rally_cell = (html.unescape(match.group(i)).strip()
                                             for i in (2, 3, 4))
        try:
            start_dt = datetime.datetime.strptime(f"{start_text} {year}", "%d %B %Y").replace(
                hour=12, tzinfo=datetime.timezone.utc)
            end_dt = datetime.datetime.strptime(f"{end_text} {year}", "%d %B %Y").replace(
                hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        rally = re.sub(r'\{\{flagicon\|[^}]+\}\}\s*', '', rally_cell, flags=re.I)
        rally = re.sub(r'\[\[[^\]|]+\|([^\]]+)\]\]', r'\1', rally)
        rally = re.sub(r'\[\[([^\]]+)\]\]', r'\1', rally)
        rally = re.sub(r'<[^>]+>', '', rally).strip(" '[]")
        if not rally:
            rally = "WRC Rally"
        rows.append({"series": "wrc", "series_name": "WRC", "race": rally,
                     "session": "Rally weekend", "circuit": "",
                     "start": start_dt.isoformat(), "end": end_dt.isoformat(),
                     "all_day": True,
                     "date_text": f"{start_text} – {end_text} {year}",
                     "url": "https://www.wrc.com/en/calendar"})
    rows.sort(key=lambda row: row.get("start") or "")
    return rows

def get_wrc_schedule(force=False):
    """Read the official WRC calendar and cache rally weekends for a week."""
    stale = _load_timed_data_cache("racing-wrc.json", 370 * 24 * 3600)
    if not force:
        disk = _load_timed_data_cache("racing-wrc.json", _F1_TTL)
        if isinstance(disk, list) and disk and all(row.get("end") for row in disk):
            return disk
    year = datetime.datetime.now().year
    try:
        raw = http_get_text("https://www.wrc.com/en/calendar", timeout=15)
    except Exception:
        raw = ""
    link_re = re.compile(rf'href="(/en/events/[^"?#]*-{year})"', re.I)
    rows, seen = [], set()
    for match in link_re.finditer(raw):
        path = match.group(1)
        if path in seen:
            continue
        # Current WRC cards expose their date semantically as
        # <time datetime="2026-08-27T09:00:00-03:00">27 – 30 August 2026</time>.
        # Keep a generous card slice because its preview images can be large.
        segment = raw[match.start():match.start() + 12000]
        title_match = re.search(r'event-feed-card__title[^>]*>(.*?)</div>', segment,
                                re.I | re.S)
        art_match = re.search(r'event-feed-card__logo[^>]+src=["\']([^"\']+)', segment,
                              re.I | re.S)
        time_match = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\'][^>]*>(.*?)</time>',
                               segment, re.I | re.S)
        if time_match:
            stamp = html.unescape(time_match.group(1)).strip()
            date_text = html.unescape(re.sub(r"<[^>]+>", "", time_match.group(2))).strip()
            try:
                start_dt = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            end_dt = start_dt + datetime.timedelta(days=3)
            range_match = re.search(r'(\d{1,2})\s*[–—-]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})',
                                    date_text)
            if range_match:
                try:
                    end_date = datetime.datetime.strptime(
                        f"{range_match.group(2)} {range_match.group(3)} {range_match.group(4)}",
                        "%d %B %Y").date()
                    end_dt = datetime.datetime.combine(end_date, datetime.time(23, 59, 59),
                                                       tzinfo=start_dt.tzinfo)
                except ValueError:
                    pass
            clean_name = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip() if title_match else ""
        else:
            # Compatibility with the older WRC card markup.
            date_match = re.search(r'(\d{1,2}(?:\s+[A-Z]+)?\s*-\s*\d{1,2}\s+[A-Z]+\s+' + str(year) +
                                   r'|\d{1,2}\s+[A-Z]+\s+' + str(year) + r')', segment)
            if not date_match:
                continue
            date_text = html.unescape(date_match.group(1)).strip()
            first = (re.match(r'(\d{1,2})\s+([A-Z]+)\s*-', date_text) or
                     re.match(r'(\d{1,2})\s*-\s*\d{1,2}\s+([A-Z]+)', date_text) or
                     re.match(r'(\d{1,2})\s+([A-Z]+)', date_text))
            if not first:
                continue
            try:
                start_dt = datetime.datetime.strptime(
                    f"{first.group(1)} {first.group(2).title()} {year}", "%d %B %Y").replace(
                        tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
            end_dt = start_dt + datetime.timedelta(days=3)
            clean_name = ""
        if not clean_name:
            clean_name = path.rsplit("/", 1)[-1].rsplit(f"-{year}", 1)[0]
            clean_name = re.sub(r"^wrc-", "", clean_name).replace("-", " ").title()
        seen.add(path)
        rows.append({"series": "wrc", "series_name": "WRC", "race": clean_name,
                     "session": "Rally weekend", "circuit": "",
                     "start": start_dt.isoformat(), "end": end_dt.isoformat(),
                     "all_day": True, "date_text": date_text,
                     "art": html.unescape(art_match.group(1)) if art_match else "",
                     "url": "https://www.wrc.com" + path})
    rows.sort(key=lambda row: row.get("start") or "")
    # WRC.com can intermittently serve a protected/short response. A successful
    # HTTP request with zero parsed cards is therefore still a source failure.
    if not rows:
        try:
            rows = _wrc_wikipedia_schedule(year)
        except Exception:
            rows = []
    if not rows:
        if isinstance(stale, list) and stale:
            return stale
        return []
    _save_timed_data_cache("racing-wrc.json", rows)
    return rows

def get_formulae_schedule(force=False):
    """Read Formula E's official Schema.org calendar with exact race start times."""
    if not force:
        disk = _load_timed_data_cache("racing-formulae.json", _F1_TTL)
        if isinstance(disk, list) and disk:
            return disk
    raw = http_get_text("https://www.fiaformulae.com/en/calendar", timeout=25)
    rows = []
    for match in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', raw,
                             re.I | re.S):
        try:
            data = json.loads(html.unescape(match.group(1)).strip())
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue
        for entry in data.get("itemListElement", []):
            event = entry.get("item") if isinstance(entry, dict) else {}
            if not isinstance(event, dict) or not event.get("startDate"):
                continue
            location = event.get("location") or {}
            rows.append({"series": "formulae", "series_name": "Formula E",
                         "race": event.get("name") or "Formula E", "session": "Race",
                         "circuit": location.get("name") if isinstance(location, dict) else "",
                         "start": event.get("startDate"), "all_day": False, "date_text": "",
                         "url": "https://www.fiaformulae.com/en/calendar"})
        if rows:
            break
    rows.sort(key=lambda row: row.get("start") or "")
    _save_timed_data_cache("racing-formulae.json", rows)
    return rows

def get_wec_schedule(force=False):
    """Read FIA WEC's official season cards from its public homepage."""
    if not force:
        disk = _load_timed_data_cache("racing-wec.json", _F1_TTL)
        if isinstance(disk, list) and disk:
            return disk
    year = datetime.datetime.now().year
    raw = http_get_text("https://www.fiawec.com/en/", timeout=25)
    link_re = re.compile(rf'href="(/en/race/([^"?#]*-{year}))"', re.I)
    rows, seen = [], set()
    for match in link_re.finditer(raw):
        path, slug = match.groups()
        if path in seen or "prologue" in slug.lower():
            continue
        segment = raw[match.start():match.start() + 1900]
        date_match = re.search(r'<strong[^>]*>(\d{1,2})</strong>\s*<small[^>]*>([A-Za-z]{3})</small>', segment, re.I)
        if not date_match:
            continue
        try:
            start_dt = datetime.datetime.strptime(
                f"{date_match.group(1)} {date_match.group(2)} {year}", "%d %b %Y")
        except ValueError:
            continue
        title = re.sub(rf'-{year}$', '', slug, flags=re.I).replace('-', ' ').title()
        seen.add(path)
        rows.append({"series": "wec", "series_name": "WEC", "race": title,
                     "session": "Race weekend", "circuit": "",
                     "start": start_dt.replace(hour=12, tzinfo=datetime.timezone.utc).isoformat(),
                     "all_day": True, "date_text": start_dt.strftime("%d %b %Y"),
                     "url": "https://www.fiawec.com" + path})
    rows.sort(key=lambda row: row.get("start") or "")
    _save_timed_data_cache("racing-wec.json", rows)
    return rows

def get_motogp_schedule(force=False):
    """Read MotoGP's official public results API for the current season calendar."""
    if not force:
        disk = _load_timed_data_cache("racing-motogp.json", _F1_TTL)
        if isinstance(disk, list) and disk and all(row.get("end") for row in disk):
            return disk
    base = "https://api.pulselive.motogp.com/motogp/v1/results/"
    seasons = http_get_json(base + "seasons")
    year = datetime.datetime.now().year
    season = next((row for row in seasons if int(row.get("year") or 0) == year), None)
    if not season or not season.get("id"):
        return []
    events = http_get_json(base + "events?" + urllib.parse.urlencode({"seasonUuid": season["id"]}))
    rows = []
    for event in events if isinstance(events, list) else []:
        if event.get("test") is True:
            continue
        date_start = str(event.get("date_start") or "")
        if not date_start:
            continue
        circuit = event.get("circuit") or {}
        rows.append({"series": "motogp", "series_name": "MotoGP",
                     "race": event.get("sponsored_name") or event.get("name") or "MotoGP",
                     "session": "Grand Prix weekend",
                     "circuit": circuit.get("name") if isinstance(circuit, dict) else "",
                     "start": date_start + "T12:00:00+00:00", "all_day": True,
                     "end": (str(event.get("date_end") or date_start) + "T23:59:59+00:00"),
                     "date_text": date_start + (" - " + str(event.get("date_end")) if event.get("date_end") else ""),
                     "url": "https://www.motogp.com/en/calendar"})
    rows.sort(key=lambda row: row.get("start") or "")
    _save_timed_data_cache("racing-motogp.json", rows)
    return rows

def get_racing_events(series_ids, force=False):
    out = []
    wanted = {str(item).lower() for item in (series_ids or [])}
    def add(key, loader):
        if key not in wanted:
            return
        try:
            out.extend(loader())
        except Exception:
            # Racing sources are independent. One site being temporarily down
            # must not blank every other enabled championship.
            return
    if "f1" in wanted:
        def f1_rows():
            year = datetime.datetime.now().year
            rows = []
            for event in get_f1_schedule(force=force):
                row = dict(event)
                row.update({"series": "f1", "series_name": "Formula 1", "all_day": False,
                            "url": f"https://www.formula1.com/en/racing/{year}"})
                rows.append(row)
            return rows
        add("f1", f1_rows)
    for key in ("f2", "f3"):
        add(key, lambda key=key: get_fia_racing_weekends(key, force=force))
    add("indycar", lambda: get_indycar_schedule(force=force))
    add("wrc", lambda: get_wrc_schedule(force=force))
    add("formulae", lambda: get_formulae_schedule(force=force))
    add("wec", lambda: get_wec_schedule(force=force))
    add("motogp", lambda: get_motogp_schedule(force=force))
    out.sort(key=lambda row: row.get("start") or "")
    return out

def _f1_logo_path(constructor_id):
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(constructor_id or ""))
    return os.path.join(artwork_cache_dir(), f"f1-{safe}.img") if safe else ""

def _f1_logo_url(constructor_id):
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(constructor_id or ""))
    return f"/api/f1_team_logo?id={safe}" if safe else ""

def _cache_f1_logo(constructor_id):
    path = _f1_logo_path(constructor_id)
    if path and os.path.isfile(path):
        return path
    team = next((row for row in get_f1_teams() if row["id"] == str(constructor_id)), None)
    if not team or not team.get("url"):
        return ""
    try:
        title = urllib.parse.unquote(urllib.parse.urlparse(team["url"]).path.rsplit("/", 1)[-1])
        info = http_get_json("https://en.wikipedia.org/api/rest_v1/page/summary/" +
                             urllib.parse.quote(title, safe=""), timeout=12)
        image = ((info.get("thumbnail") or {}).get("source") or
                 (info.get("originalimage") or {}).get("source") or "")
        if not image:
            return ""
        req = urllib.request.Request(image, headers={"User-Agent": "OlosTVMate/" + VERSION})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(2 * 1024 * 1024 + 1)
        if not raw or len(raw) > 2 * 1024 * 1024 or not _image_content_type(raw):
            return ""
        os.makedirs(artwork_cache_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return path
    except Exception:
        return ""

def _team_id_from_url(url):
    text = str(url or "")
    match = re.search(r"/teams/(\d+)(?:/|$)", text)
    if match:
        return match.group(1)
    match = re.search(r"/teams/[^?#]*?/(\d+)(?:/|$|[?#])", text)
    return match.group(1) if match else ""

def resolve_fotmob_team_id(team_name):
    """Resolve a favorite team name without depending on TV-guide coverage."""
    raw = str(team_name or "").strip()
    key = raw.lower()
    if not key:
        return ""
    if key in _TEAM_ID_CACHE:
        return _TEAM_ID_CACHE[key]
    try:
        data = http_get_json(FOTMOB_TEAM_SEARCH.format(
            term=urllib.parse.quote(raw)), timeout=10)
    except Exception:
        return ""
    wanted = _expand_terms(key)
    candidates = []
    def walk(obj):
        if isinstance(obj, dict):
            name = str(obj.get("name") or obj.get("title") or "").strip()
            team_id = obj.get("id") or obj.get("teamId") or obj.get("team_id")
            kind = str(obj.get("type") or obj.get("entityType") or "").lower()
            if name and team_id is not None and str(team_id).isdigit():
                low = name.lower()
                equivalent = (low == key or low in wanted or key in _expand_terms(low))
                if equivalent:
                    candidates.append((2 if "team" in kind else 1, str(team_id)))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    walk(data)
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    _TEAM_ID_CACHE[key] = candidates[0][1]
    return candidates[0][1]

def search_fotmob_teams(term, limit=12):
    """Search FotMob's real team index, independent of TV coverage."""
    raw = str(term or "").strip()
    if not raw:
        return []
    data = http_get_json(FOTMOB_TEAM_SEARCH.format(
        term=urllib.parse.quote(raw)), timeout=10)
    wanted = _expand_terms(raw.lower())
    out = []
    seen = set()
    def walk(obj):
        if len(out) >= limit:
            return
        if isinstance(obj, dict):
            kind = str(obj.get("type") or obj.get("entityType") or "").lower()
            name = str(obj.get("name") or obj.get("title") or "").strip()
            team_id = obj.get("id") or obj.get("teamId") or obj.get("team_id")
            low = name.lower()
            matches = (raw.lower() in low or low in wanted or
                       any(alias in low or low in alias for alias in wanted
                           if len(alias) >= 3))
            if (kind == "team" and name and team_id is not None and
                    str(team_id).isdigit() and matches and str(team_id) not in seen):
                seen.add(str(team_id))
                out.append({"name": name, "team_id": str(team_id)})
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    walk(data)
    return out

def fetch_fotmob_daily_matches():
    """Today's FotMob match feed, including untelevised live fixtures."""
    day = time.strftime("%Y%m%d", time.localtime())
    now = time.time()
    if (_DAILY_MATCH_CACHE["date"] == day and _DAILY_MATCH_CACHE["matches"] and
            now - _DAILY_MATCH_CACHE["ts"] < _DAILY_MATCH_TTL):
        return _DAILY_MATCH_CACHE["matches"]
    disk = _load_timed_data_cache("fotmob-daily.json", _DAILY_MATCH_TTL)
    if isinstance(disk, dict) and disk.get("date") == day and isinstance(disk.get("matches"), list):
        _DAILY_MATCH_CACHE.update({"date": day, "ts": now, "matches": disk["matches"]})
        return disk["matches"]
    data = http_get_json(FOTMOB_DAILY_MATCHES.format(date=day), timeout=15)
    matches = []
    seen = set()
    def walk(obj, league_name="", league_ccode="", league_id=""):
        if isinstance(obj, dict):
            if isinstance(obj.get("matches"), list) and obj.get("name"):
                league_name = str(obj.get("name") or league_name)
                league_ccode = str(obj.get("ccode") or league_ccode)
                candidate_id = obj.get("id") or obj.get("leagueId")
                if candidate_id is not None and str(candidate_id).isdigit():
                    league_id = str(candidate_id)
            home = obj.get("home")
            away = obj.get("away")
            status = obj.get("status")
            if (isinstance(home, dict) and isinstance(away, dict) and
                    home.get("name") and away.get("name") and isinstance(status, dict)):
                key = str(obj.get("id") or "") + "|" + str(status.get("utcTime") or "")
                if key not in seen:
                    seen.add(key)
                    row = dict(obj)
                    row["_league_name"] = league_name
                    row["_league_ccode"] = league_ccode
                    row["_league_id"] = league_id
                    matches.append(row)
            for value in obj.values():
                walk(value, league_name, league_ccode, league_id)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, league_name, league_ccode, league_id)
    walk(data)
    _DAILY_MATCH_CACHE.update({"date": day, "ts": now, "matches": matches})
    _save_timed_data_cache("fotmob-daily.json", {"date": day, "matches": matches})
    return matches

def featured_daily_fixtures():
    """Today's English Premier League and UEFA Champions League fixtures."""
    out = []
    for match in fetch_fotmob_daily_matches():
        league = str(match.get("_league_name") or "").strip()
        low = league.lower()
        ccode = str(match.get("_league_ccode") or "").upper()
        is_premier_league = low == "premier league" and ccode == "ENG"
        is_champions_league = (low == "champions league" or
                               low.startswith("champions league "))
        if not (is_premier_league or is_champions_league):
            continue
        status = match.get("status") or {}
        if status.get("cancelled"):
            continue
        home = match.get("home") or {}
        away = match.get("away") or {}
        live_time = status.get("liveTime") or {}
        live_minute = None
        try:
            clock = str(live_time.get("long") or "") if isinstance(live_time, dict) else ""
            live_minute = int(clock.split(":", 1)[0]) if clock else None
        except (TypeError, ValueError):
            pass
        out.append({"home": str(home.get("name") or ""),
                    "away": str(away.get("name") or ""),
                    "home_id": str(home.get("id") or ""),
                    "away_id": str(away.get("id") or ""),
                    "start": str(status.get("utcTime") or match.get("startDate") or ""),
                    "is_live": bool((status.get("started") or status.get("ongoing")) and
                                    not status.get("finished")),
                    "is_finished": bool(status.get("finished")),
                    "live_minute": live_minute, "league_name": league,
                    "league_id": str(match.get("_league_id") or ""),
                    "status_known": True, "by_country": {}, "favorite_teams": []})
    return sorted(out, key=lambda row: row.get("start") or "")

def search_daily_matches(term):
    """Find today's live/upcoming fixtures independent of TV coverage."""
    term_l = str(term or "").lower().strip()
    if not term_l:
        return []
    wanted = _expand_terms(term_l)
    out = []
    for match in fetch_fotmob_daily_matches():
        home_obj = match.get("home") or {}
        away_obj = match.get("away") or {}
        status = match.get("status") or {}
        if status.get("cancelled") or status.get("finished"):
            continue
        home = str(home_obj.get("name") or "")
        away = str(away_obj.get("name") or "")
        hay = (home + " " + away).lower()
        if not any(value in hay for value in wanted):
            continue
        start = str(status.get("utcTime") or match.get("startDate") or "")
        is_live = bool(status.get("started") or status.get("ongoing") or status.get("live"))
        live_minute = None
        live_time = status.get("liveTime") or {}
        if isinstance(live_time, dict):
            live_clock = str(live_time.get("long") or "").strip()
            try:
                live_minute = int(live_clock.split(":", 1)[0]) if live_clock else None
            except (ValueError, TypeError):
                live_minute = None
        out.append({"home": home, "away": away, "start": start,
                    "home_id": str(home_obj.get("id") or ""),
                    "away_id": str(away_obj.get("id") or ""),
                    "is_live": is_live, "live_minute": live_minute,
                    "is_finished": bool(status.get("finished")),
                    "league_name": str(match.get("_league_name") or ""),
                    "league_id": str(match.get("_league_id") or ""),
                    "by_country": {}, "all_channels": []})
    return out

_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.I | re.S)

def _iter_sportsevents(obj):
    if isinstance(obj, dict):
        t = obj.get("@type")
        if t == "SportsEvent" or (isinstance(t, list) and "SportsEvent" in t):
            yield obj
        for v in obj.values():
            yield from _iter_sportsevents(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_sportsevents(it)

def _channels_from_event(ev):
    names = []
    be = ev.get("broadcastEvent")
    events = be if isinstance(be, list) else ([be] if be else [])
    for b in events:
        if not isinstance(b, dict):
            continue
        pub = b.get("publishedOn")
        pubs = pub if isinstance(pub, list) else ([pub] if pub else [])
        for p in pubs:
            if isinstance(p, dict) and p.get("name"):
                names.append(str(p["name"]).strip())
    seen, out = set(), []
    for n in names:
        k = n.lower()
        if n and k not in seen:
            seen.add(k)
            out.append(n)
    return out

def fetch_country_fixtures(country):
    country = _norm_cc(country)   # uk -> gb, etc.
    disp = _display_cc(country)
    now = time.time()
    c = _TV_CACHE.get(country)
    if c and (now - c["ts"] < _TV_TTL):
        return c["fixtures"]
    disk = _load_timed_data_cache(f"tv-guide-{country}.json", _TV_TTL)
    if isinstance(disk, list):
        _TV_CACHE[country] = {"ts": now, "fixtures": disk}
        return disk
    page = http_get_text(FOTMOB_TVGUIDE.format(country=country))
    fixtures = []
    for block in _LD_RE.findall(page):
        raw = html.unescape(block.strip())
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for ev in _iter_sportsevents(data):
            home_obj = ev.get("homeTeam") or {}
            away_obj = ev.get("awayTeam") or {}
            home = home_obj.get("name") or ""
            away = away_obj.get("name") or ""
            if not (home or away):
                nm = ev.get("name") or ""
                if " vs " in nm:
                    home, away = [s.strip() for s in nm.split(" vs ", 1)]
            fixtures.append({
                "home": home, "away": away,
                "home_id": _team_id_from_url(home_obj.get("url") or ""),
                "away_id": _team_id_from_url(away_obj.get("url") or ""),
                "home_slug": _slug_name(home_obj.get("url") or ""),
                "away_slug": _slug_name(away_obj.get("url") or ""),
                "start": ev.get("startDate", "") or "",
                "channels": _channels_from_event(ev),
                "country": disp,
                "match_url": ev.get("url") or ev.get("@id") or "",
            })
    _TV_CACHE[country] = {"ts": now, "fixtures": fixtures}
    _save_timed_data_cache(f"tv-guide-{country}.json", fixtures)
    return fixtures

def _team_profile_from_data(data, team_id, team_name=""):
    """Extract stable team facts from FotMob without depending on one response layout."""
    profile = {"team_id": str(team_id or ""), "name": str(team_name or "").strip(),
               "country": "", "league": "", "stadium": "", "coach": ""}
    if not isinstance(data, dict):
        return profile
    def display(value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("name", "title", "label", "shortName"):
                if value.get(key):
                    return str(value[key]).strip()
        return ""
    # FotMob's current team payload keeps the useful identity fields in a few
    # stable nested objects. Read those explicitly first, then let the generic
    # walker below handle older/alternate response layouts.
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    sports_json = details.get("sportsTeamJSONLD") if isinstance(details.get("sportsTeamJSONLD"), dict) else {}
    location = sports_json.get("location") if isinstance(sports_json.get("location"), dict) else {}
    address = location.get("address") if isinstance(location.get("address"), dict) else {}
    venue = overview.get("venue") if isinstance(overview.get("venue"), dict) else {}
    venue_widget = venue.get("widget") if isinstance(venue.get("widget"), dict) else {}
    lineup = overview.get("lastLineupStats") if isinstance(overview.get("lastLineupStats"), dict) else {}
    coach = lineup.get("coach") if isinstance(lineup.get("coach"), dict) else {}
    if details.get("name"):
        profile["name"] = str(details.get("name")).strip()
    if address.get("addressCountry"):
        profile["country"] = str(address.get("addressCountry")).strip()
    elif details.get("country"):
        profile["country"] = str(details.get("country")).strip()
    if details.get("primaryLeagueName"):
        profile["league"] = str(details.get("primaryLeagueName")).strip()
    if venue_widget.get("name"):
        profile["stadium"] = str(venue_widget.get("name")).strip()
    elif location.get("name"):
        profile["stadium"] = str(location.get("name")).strip()
    if coach.get("name"):
        profile["coach"] = str(coach.get("name")).strip()
    # Search profile/overview metadata, but deliberately skip fixture trees so
    # an away stadium or competition is never mistaken for the team's own fact.
    wanted = {
        "country": {"country", "countryname", "ccode"},
        "league": {"primaryleague", "league", "leaguename", "tournament"},
        "stadium": {"stadium", "venue", "ground", "homeground"},
        "coach": {"coach", "headcoach", "manager", "headmanager"},
    }
    skip = {"fixtures", "allfixtures", "matches", "results", "nextmatch", "previousmatch"}
    def walk(obj):
        if isinstance(obj, dict):
            for raw_key, value in obj.items():
                key = re.sub(r"[^a-z]", "", str(raw_key).lower())
                if key in skip:
                    continue
                for field, aliases in wanted.items():
                    if not profile[field] and key in aliases:
                        text = display(value)
                        if text:
                            profile[field] = text
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(obj, list):
            for value in obj[:50]:
                walk(value)
    # Prefer obvious team identity blocks before walking the rest.
    for block_name in ("details", "teamDetails", "profile", "overview"):
        block = data.get(block_name)
        if isinstance(block, dict):
            if not profile["name"]:
                profile["name"] = display(block.get("name"))
            walk(block)
    walk(data)
    return profile

def _remember_team_profile(team_id, team_name, data):
    team_id = str(team_id or "").strip()
    if not team_id:
        return {}
    profile = _team_profile_from_data(data, team_id, team_name)
    _TEAM_PROFILE_CACHE[team_id] = {"ts": time.time(), "profile": profile}
    _save_timed_data_cache(f"team-profile-v{_TEAM_PROFILE_CACHE_SCHEMA}-{team_id}.json", profile)
    return profile

def fetch_team_profile(team_id, team_name=""):
    team_id = str(team_id or "").strip()
    if not team_id:
        return {"team_id": "", "name": str(team_name or "").strip(), "country": "", "league": "", "stadium": "", "coach": ""}
    now = time.time()
    cached = _TEAM_PROFILE_CACHE.get(team_id)
    if cached and now - cached["ts"] < _TEAM_FIXTURE_TTL:
        return dict(cached["profile"])
    disk = _load_timed_data_cache(f"team-profile-v{_TEAM_PROFILE_CACHE_SCHEMA}-{team_id}.json", _TEAM_FIXTURE_TTL)
    if isinstance(disk, dict) and disk:
        _TEAM_PROFILE_CACHE[team_id] = {"ts": now, "profile": disk}
        return dict(disk)
    data = http_get_json(FOTMOB_TEAM_API.format(team_id=urllib.parse.quote(team_id)), timeout=15)
    return _remember_team_profile(team_id, team_name, data)

def fetch_team_schedule(team_id, team_name=""):
    """Fetch a team's real FotMob fixture/status feed (not the TV guide)."""
    team_id = str(team_id or "").strip()
    if not team_id:
        return []
    now = time.time()
    cached = _TEAM_FIXTURE_CACHE.get(team_id)
    if cached and now - cached["ts"] < _TEAM_FIXTURE_TTL:
        return [dict(row) for row in cached["fixtures"]]
    disk = _load_timed_data_cache(f"team-fixtures-{team_id}.json", _TEAM_FIXTURE_TTL)
    # b169 adds competition metadata for timeline artwork. Refresh older
    # schedule caches once instead of waiting for their normal long TTL.
    if (isinstance(disk, list) and disk and
            any(row.get("league_id") for row in disk if isinstance(row, dict))):
        _TEAM_FIXTURE_CACHE[team_id] = {"ts": now, "fixtures": disk}
        return [dict(row) for row in disk]
    data = http_get_json(FOTMOB_TEAM_API.format(team_id=urllib.parse.quote(team_id)), timeout=15)
    _remember_team_profile(team_id, team_name, data)
    fixture_root = data.get("fixtures") or {}
    all_fixtures = fixture_root.get("allFixtures") or {}
    raw = all_fixtures.get("fixtures") or fixture_root.get("fixtures") or []
    if not isinstance(raw, list):
        raw = []
    # FotMob moves an in-progress match into overview/ongoing data, so it can
    # disappear from allFixtures while it is live. Collect fixture-shaped
    # objects from the full response as well, then deduplicate below.
    candidates = list(raw)
    def collect_current(obj):
        if isinstance(obj, dict):
            status = obj.get("status")
            home = obj.get("home")
            away = obj.get("away")
            opponent = obj.get("opponent")
            if (isinstance(status, dict) and
                    ((isinstance(home, dict) and isinstance(away, dict)) or
                     isinstance(opponent, dict))):
                candidates.append(obj)
            for value in obj.values():
                collect_current(value)
        elif isinstance(obj, list):
            for value in obj:
                collect_current(value)
    collect_current(data)
    # The team endpoint can move/drop an ongoing fixture from its normal list.
    # FotMob's daily feed is authoritative for today's live matches.
    daily_status = {}
    try:
        for match in fetch_fotmob_daily_matches():
            home = match.get("home") or {}
            away = match.get("away") or {}
            if str(home.get("id") or "") == team_id or str(away.get("id") or "") == team_id:
                candidates.append(match)
                if match.get("id") is not None:
                    daily_status[str(match.get("id"))] = match.get("status") or {}
    except Exception:
        pass
    out = []
    seen_fixtures = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        home_obj = item.get("home") or {}
        away_obj = item.get("away") or {}
        if not isinstance(home_obj, dict):
            home_obj = {}
        if not isinstance(away_obj, dict):
            away_obj = {}
        home = str(home_obj.get("name") or "").strip()
        away = str(away_obj.get("name") or "").strip()
        # Some team-feed variants expose only the opponent + home/away flag.
        if not (home and away):
            opponent = item.get("opponent") or {}
            if isinstance(opponent, dict) and opponent.get("name") and team_name:
                is_home = item.get("isHome")
                if is_home is None:
                    is_home = item.get("home") is True
                if is_home:
                    home, away = team_name, str(opponent.get("name"))
                else:
                    home, away = str(opponent.get("name")), team_name
        if not (home and away):
            continue
        status = item.get("status") or {}
        if not isinstance(status, dict):
            status = {}
        # Team pages can lag behind the live clock (sometimes returning 0).
        # Prefer today's daily match status for the same fixture.
        current_status = daily_status.get(str(item.get("id") or ""))
        if isinstance(current_status, dict) and current_status.get("ongoing"):
            status = current_status
        start = str(status.get("utcTime") or item.get("startDate") or
                    item.get("start") or "")
        is_live = bool((status.get("started") or status.get("ongoing") or
                        status.get("live")) and not status.get("finished"))
        live_minute = None
        live_time = status.get("liveTime") or {}
        if isinstance(live_time, dict):
            live_clock = str(live_time.get("long") or "").strip()
            try:
                live_minute = int(live_clock.split(":", 1)[0]) if live_clock else None
            except (ValueError, TypeError):
                live_minute = None
        # Some overview objects omit `started`; a non-finished match whose
        # kickoff is in the recent past is still live.
        if (not is_live and status and not status.get("finished") and
                not status.get("cancelled") and start):
            try:
                kickoff = datetime.datetime.fromisoformat(
                    start.replace("Z", "+00:00")).timestamp()
                age = time.time() - kickoff
                if 0 <= age <= 4 * 60 * 60:
                    is_live = True
            except (ValueError, TypeError, OverflowError):
                pass
        fixture_key = (str(item.get("id") or ""), home.lower(), away.lower(), start)
        if fixture_key in seen_fixtures:
            continue
        seen_fixtures.add(fixture_key)
        tournament = item.get("tournament") or {}
        if not isinstance(tournament, dict):
            tournament = {}
        league_name = str(tournament.get("name") or item.get("_league_name") or "").strip()
        league_id = tournament.get("leagueId") or tournament.get("id") or item.get("_league_id") or ""
        league_id = str(league_id) if str(league_id).isdigit() else ""
        out.append({"home": home, "away": away, "start": start,
                    "home_id": str(home_obj.get("id") or ""),
                    "away_id": str(away_obj.get("id") or ""),
                    "is_live": is_live, "live_minute": live_minute,
                    "league_name": league_name, "league_id": league_id,
                    "status_known": bool(status), "by_country": {}})
    # The long-lived cache is the stable schedule only. Live state is overlaid
    # from FotMob's short-lived daily feed when My Teams is rendered.
    base_out = [dict(row, is_live=False, live_minute=None) for row in out]
    _TEAM_FIXTURE_CACHE[team_id] = {"ts": now, "fixtures": base_out}
    _save_timed_data_cache(f"team-fixtures-{team_id}.json", base_out)
    return [dict(row) for row in base_out]

def _slug_name(url):
    """Turn a Fotmob team URL like
    'https://www.fotmob.com/teams/8456/overview/manchester-city' into
    'manchester city' so full names are searchable even when the display
    name is a short form ('Man City')."""
    if not url:
        return ""
    try:
        # last path segment is the slug
        seg = url.rstrip("/").split("/")[-1]
        # strip a trailing #id if present
        seg = seg.split("#")[0].split("?")[0]
        return seg.replace("-", " ").strip().lower()
    except Exception:
        return ""

# Bidirectional alias groups. Each group is a set of equivalent names/nicknames
# for ONE team. A search term maps to a group if it matches any member; we then
# search for all members of that group only (not unrelated teams).
_TEAM_ALIAS_GROUPS = [
    ["manchester city", "man city"],
    ["manchester united", "man utd", "man united"],
    ["wolverhampton wanderers", "wolverhampton", "wolves"],
    ["tottenham hotspur", "tottenham", "spurs"],
    ["paris saint germain", "paris saint-germain", "psg"],
    ["newcastle united", "newcastle", "newcastle utd"],
    ["brighton hove albion", "brighton & hove albion", "brighton"],
    ["west ham united", "west ham"],
    ["sheffield united", "sheffield utd", "sheff utd"],
    ["sheffield wednesday", "sheff wed"],
    ["nottingham forest", "nott'm forest", "notts forest"],
    ["leeds united", "leeds utd", "leeds"],
    ["bayern munich", "bayern munchen", "fc bayern", "bayern"],
    ["borussia dortmund", "dortmund", "bvb"],
    ["borussia monchengladbach", "monchengladbach", "gladbach"],
    ["internazionale", "inter milan", "inter"],
    ["ac milan", "milan"],
    ["atletico madrid", "atletico", "atleti", "atl madrid"],
    ["real madrid", "real"],
    ["fc barcelona", "barcelona", "barca"],
]

def _expand_terms(term_l):
    """Map a search term to the set of names to look for. If the term matches a
    specific alias group, return that group's members. Prefer the MOST SPECIFIC
    match: e.g. 'manchester city' matches only the City group, not United.
    A bare 'manchester' (matches no full member exactly) falls back to matching
    any group whose members contain the term."""
    terms = {term_l}
    # 1. Exact membership: term equals a group member -> just that group.
    for group in _TEAM_ALIAS_GROUPS:
        if term_l in group:
            terms.update(group)
            return terms
    # 2. Term is a substring of a specific member (e.g. 'man city' in nothing,
    #    but 'tottenham' is a prefix of 'tottenham hotspur'): match groups where
    #    the term appears as a WHOLE within a member, preferring specific ones.
    specific = []
    for group in _TEAM_ALIAS_GROUPS:
        for member in group:
            # term is most of a member (e.g. 'manchester city' vs 'man city')
            if term_l == member or (len(term_l) >= 6 and term_l in member and
                                    member.startswith(term_l.split()[0])):
                specific.append(group)
                break
    if specific:
        for g in specific:
            terms.update(g)
        return terms
    # 3. Generic fallback: term is a loose fragment (e.g. 'manchester') that
    #    appears in multiple groups -> include all matching groups.
    for group in _TEAM_ALIAS_GROUPS:
        if any(term_l in member for member in group):
            terms.update(group)
    return terms

def _team_names_equivalent(a, b):
    a = str(a or "").lower().strip()
    b = str(b or "").lower().strip()
    if not (a and b):
        return False
    return a == b or b in _expand_terms(a) or a in _expand_terms(b)

def search_fixtures(term, countries):
    term_l = term.lower().strip()
    want = _expand_terms(term_l)
    merged, errors = {}, []
    # normalise (uk->gb) and dedupe while keeping order
    seen_cc, norm_countries = set(), []
    for c in countries:
        nc = _norm_cc(c)
        if nc and nc not in seen_cc:
            seen_cc.add(nc)
            norm_countries.append(nc)
    for country in norm_countries:
        try:
            fx = fetch_country_fixtures(country)
        except Exception as e:
            errors.append(f"{_display_cc(country)}: {e}")
            continue
        for f in fx:
            hay = " ".join([
                f.get("home", ""), f.get("away", ""),
                f.get("home_slug", ""), f.get("away_slug", "")
            ]).lower()
            if not any(w in hay for w in want):
                continue
            day = (f["start"] or "")[:10]
            key = f"{f['home'].lower()}|{f['away'].lower()}|{day}"
            m = merged.get(key)
            if not m:
                m = {"home": f["home"], "away": f["away"], "start": f["start"],
                     "home_id": f.get("home_id", ""), "away_id": f.get("away_id", ""),
                     "match_url": f["match_url"], "by_country": {}, "all_channels": []}
                merged[key] = m
            if f["channels"]:
                m["by_country"].setdefault(f["country"], [])
                for ch in f["channels"]:
                    if ch not in m["by_country"][f["country"]]:
                        m["by_country"][f["country"]].append(ch)
                    if ch not in m["all_channels"]:
                        m["all_channels"].append(ch)
    out = list(merged.values())
    out.sort(key=lambda m: m["start"] or "")
    return out, errors

def add_tv_listings(fixtures, countries):
    """Overlay configured-country FotMob TV listings onto known fixtures."""
    tv_rows, errors = [], []
    seen_cc = set()
    for country in countries:
        country = _norm_cc(country)
        if not country or country in seen_cc:
            continue
        seen_cc.add(country)
        try:
            tv_rows.extend(fetch_country_fixtures(country))
        except Exception as e:
            errors.append(f"{_display_cc(country)}: {e}")
    for fixture in fixtures:
        fday = str(fixture.get("start") or "")[:10]
        by_country = fixture.setdefault("by_country", {})
        for tvrow in tv_rows:
            if fday and str(tvrow.get("start") or "")[:10] != fday:
                continue
            if not (_team_names_equivalent(fixture.get("home"), tvrow.get("home")) and
                    _team_names_equivalent(fixture.get("away"), tvrow.get("away"))):
                continue
            channels = tvrow.get("channels") or []
            if not channels:
                continue
            cc = str(tvrow.get("country") or "")
            current = by_country.setdefault(cc, [])
            for channel in channels:
                if channel not in current:
                    current.append(channel)
    return errors

# --------------------------------------------------------------------------
# Fuzzy channel matching (handles "COUNTRY: Channel Name HD")
# --------------------------------------------------------------------------

_QUALITY_RE = re.compile(r"\b(4k|uhd|fhd|hd|sd)\b", re.I)
_NOISE_RE = re.compile(r"[^a-z0-9 ]+")
# strip a leading provider tag like "GOLD: ", "SPO: ", "NO| ", "VIP - "
_PREFIX_RE = re.compile(r"^\s*[a-z0-9]{1,5}\s*[:|\-]\s*", re.I)
_PAREN_CC_RE = re.compile(r"\s*\((?:no|uk|us|usa|espanol|espa\w*|[a-z]{2,3})\)\s*$", re.I)
_HASH_RE = re.compile(r"#+")                 # "###### SPORT ######"
_FPS_RE = re.compile(r"\b\d{2,3}\s*fps\b", re.I)

# Words that carry no identifying power on their own.
_GENERIC = {"sport", "sports", "tv", "play", "channel", "the", "hd", "sd",
            "fhd", "uhd", "4k", "raw", "vip", "gold", "ultra", "premium",
            "fps", "dolby", "audio", "1", "one"}

def normalise(name):
    n = name.lower()
    n = _HASH_RE.sub(" ", n)          # remove ### decoration
    n = _PREFIX_RE.sub("", n)         # drop leading provider tag
    n = _PAREN_CC_RE.sub("", n)       # drop trailing "(NO)"
    n = _FPS_RE.sub(" ", n)           # drop "50fps"/"60fps"
    n = _QUALITY_RE.sub(" ", n)       # ignore quality words
    n = n.replace("&", "and")
    n = _NOISE_RE.sub(" ", n)         # strip superscripts/symbols/punct
    n = re.sub(r"\s+", " ", n).strip()
    return n

def normalise_event_name(name):
    """Normalize an event channel without stripping its leading team name."""
    n = str(name or "").lower()
    n = _HASH_RE.sub(" ", n)
    n = _PAREN_CC_RE.sub("", n)
    n = _FPS_RE.sub(" ", n)
    n = _QUALITY_RE.sub(" ", n)
    n = n.replace("&", "and")
    n = _NOISE_RE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()

def _distinctive(words):
    """The identifying words of a name (generic terms removed)."""
    return [w for w in words if w not in _GENERIC and len(w) > 1]

def quality_tag(name):
    m = _QUALITY_RE.search(name)
    return m.group(1).upper() if m else ""

def _trailing_num(s):
    m = re.search(r"(\d+)\s*$", s)
    return m.group(1) if m else None

def _score(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()

def _numbers_conflict(a, b):
    na, nb = _trailing_num(a), _trailing_num(b)
    return na is not None and nb is not None and na != nb

# Broadcaster names that are streaming/OTT platforms, not linear TV channels.
# Fotmob names these distinctively (e.g. "TV 2 Play (NO)", "Viaplay", "Apple TV").
_STREAMING_HINTS = (
    "play", "viaplay", "app", "apple tv", "peacock", "paramount", "fubo",
    "dazn", "amazon", "prime video", "disney", "espn+", "hbo", "max",
    "netflix", "fanatiz", "stream", "youtube", "skyshowtime", "discovery+",
    "tv2 play", "tv 2 play", "nrk tv", "vg+", "vg tv",
)

# A shared word such as "Network" or "Sports" must never make a channel for
# another sport a football broadcaster candidate.
_NON_FOOTBALL_CHANNEL_RE = re.compile(
    r"(?<![a-z0-9])(mlb|nfl|nba|nhl|baseball|basketball|ice hockey|cricket|"
    r"cartoon network|nickelodeon|disney channel|disney junior|boomerang)(?![a-z0-9])",
    re.I)

def _is_streaming(name):
    n = (name or "").lower()
    return any(h in n for h in _STREAMING_HINTS)

def _is_ppv_category(catname):
    c = (catname or "").lower()
    return ("ppv" in c) or ("play" in c) or ("event" in c)

def _is_4k_category(catname):
    """True for provider buckets that collect UHD channels across countries."""
    return bool(re.search(r"(?<![a-z0-9])(4k|uhd)(?![a-z0-9])",
                          str(catname or "").lower()))

# Known country codes that may appear as a channel prefix. If a channel's
# prefix is one of these AND it isn't the broadcast's country, the channel is
# the wrong country and must be rejected. Provider tiers (GOLD/SPO/VIP/...)
# are NOT in this set, so they pass through.
_COUNTRY_CODES = {
    "no", "uk", "gb", "us", "usa", "dk", "se", "fi", "de", "at", "nl", "fr",
    "it", "es", "pt", "ie", "be", "ch", "pl", "cz", "sk", "hu", "ro", "bg",
    "gr", "hr", "si", "rs", "ba", "bh", "mk", "al", "tr", "ru", "ua", "ar",
    "sa", "ir", "in", "pk", "ca", "au", "br", "mx", "asia", "afr", "ex",
    "yu", "ex-yu", "lt", "lv", "ee", "is", "lu", "mt", "cy", "cr",
}
_CC_PREFIX_RE = re.compile(r"^\s*([a-z]{2,4})\s*[:|\-]", re.I)
_COUNTRY_NAME_ALIASES = {
    "norway": "no", "norge": "no", "norwegian": "no",
    "united kingdom": "uk", "great britain": "gb", "britain": "gb",
    "england": "uk", "english": "uk",
    "united states": "us", "america": "us", "american": "us",
    "ireland": "ie", "irish": "ie", "spain": "es", "spanish": "es",
    "germany": "de", "german": "de", "italy": "it", "italian": "it",
    "france": "fr", "french": "fr", "portugal": "pt", "portuguese": "pt",
    "netherlands": "nl", "dutch": "nl", "belgium": "be", "belgian": "be",
    "denmark": "dk", "danish": "dk", "sweden": "se", "swedish": "se",
    "finland": "fi", "finnish": "fi", "canada": "ca", "canadian": "ca",
    "australia": "au", "australian": "au", "brazil": "br", "brazilian": "br",
    "mexico": "mx", "mexican": "mx", "india": "in", "indian": "in",
    "hong kong": "hk", "hongkong": "hk",
    "singapore": "sg", "malaysia": "my", "indonesia": "id",
    "philippines": "ph", "thailand": "th", "vietnam": "vn",
}
_COUNTRY_CODES.update({"hk", "sg", "my", "id", "ph", "th", "vn"})

# broadcaster-country -> the channel prefix codes that count as "same country"
_COUNTRY_MATCH = {
    "NO": {"no"},
    "UK": {"uk", "gb"},
    "US": {"us", "usa"},
    "ES": {"es"},
    "DE": {"de"},
    "IT": {"it"},
    "FR": {"fr"},
}

def _cc_from_prefix(text):
    """Extract a recognised country code from a leading 'XX:' or 'XX|' prefix,
    else None. Works on both channel names and category names."""
    m = _CC_PREFIX_RE.match(text or "")
    if not m:
        return None
    code = m.group(1).lower()
    return code if code in _COUNTRY_CODES else None

def _cc_from_name(text):
    """Recognise an explicitly written country/region in a provider label."""
    value = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    if not value:
        return None
    for alias, code in sorted(_COUNTRY_NAME_ALIASES.items(),
                              key=lambda item: len(item[0]), reverse=True):
        if re.search(r"(?<![a-z0-9])" + re.escape(alias) +
                     r"(?![a-z0-9])", value):
            return code
    return None

def _resolve_channel_country(name, category):
    """Determine a channel's country. Prefer the CATEGORY prefix (e.g.
    'NO| NORWAY HD/RAW') since providers group channels by country there and
    it's far more consistent than the name prefix. Fall back to the name
    prefix ('NO:'), else None (unknown -> not country-filtered)."""
    return (_cc_from_prefix(category) or _cc_from_name(category) or
            _cc_from_prefix(name) or _cc_from_name(name))

def match_channels(by_country, xtream_channels, cats, threshold):
    """`by_country`: {COUNTRY: [broadcaster names]}. A channel is only eligible
    to match a broadcaster from country C if the channel's own country prefix
    is not a *different* recognised country."""
    # Build (broadcaster, country, normtokens) list.
    srcs = []
    for country, names in (by_country or {}).items():
        allowed = _COUNTRY_MATCH.get(country.upper(), {country.lower()})
        for s in names:
            ns = normalise(s)
            toks = set(ns.split())
            if toks:
                srcs.append((s, country.upper(), allowed, ns, toks))
    rows = []
    for ch in xtream_channels:
        cname = ch["name"]
        category = cats.get(ch["category_id"], "")
        if (_NON_FOOTBALL_CHANNEL_RE.search(cname) or
                _NON_FOOTBALL_CHANNEL_RE.search(category)):
            continue
        xn = normalise(cname)
        if not xn:
            continue
        xset = set(xn.split())
        ch_cc = _resolve_channel_country(cname, category)  # category first, then name
        best, best_src, best_country, best_exact_provider = 0.0, "", "", False
        for orig, bcountry, allowed, sn, sset in srcs:
            # Country rule: if the channel HAS a recognised country prefix and
            # it isn't in this broadcaster's allowed set -> skip (wrong country).
            if ch_cc is not None and ch_cc not in allowed:
                continue
            if _numbers_conflict(xn, sn):
                continue
            # Generic tokens such as "tv" or "sport" must never be enough
            # to establish a match (VG TV must not match VGN TV). At the same
            # time compact brand spellings are equivalent: VG TV == VGTV.
            sid = set(_distinctive(sn.split()))
            xid = set(_distinctive(xn.split()))
            scompact = re.sub(r"\s+", "", sn)
            xcompact = re.sub(r"\s+", "", xn)
            compact_exact = bool(scompact and scompact == xcompact)
            # A complete broadcaster brand may be embedded in a longer event
            # channel name ("... | VGTV PPV 3"). Do not accept the reverse:
            # a short generic channel such as "TV 2" is not "TV 2 Play".
            compact_contained = len(scompact) >= 4 and scompact in xcompact
            inter = xid & sid
            if compact_exact:
                s = 1.0
            elif compact_contained:
                s = 0.96
            else:
                if not inter:
                    continue
                cover_b = len(inter) / max(1, len(sid))
                cover_c = len(inter) / max(1, len(xid))
                s = _score(xn, sn)
                if sid and sid <= xid:
                    s = max(s, 0.8 + 0.2 * cover_c)
                else:
                    s = max(s, cover_b * cover_c)
            if s > best:
                best, best_src, best_country = s, orig, bcountry
                # A prominent provider result must be a named linear channel,
                # an exact normalized name, and carry the provider's country.
                # Streaming platforms such as TV 2 Play remain category-only.
                best_exact_provider = bool(
                    compact_exact and (ch_cc in allowed or
                                       (ch_cc is None and _is_4k_category(category))) and
                    not _is_streaming(orig))
        best = round(max(0.0, min(1.0, best)), 3)
        if best >= threshold:
            rows.append({"xtream_name": cname, "stream_id": ch["stream_id"],
                         "category": category,
                         "logo": ch.get("stream_icon", ""),
                         "quality": quality_tag(cname),
                         "matched": best_src, "country": best_country,
                         "score": best, "provider_exact": best_exact_provider})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows

def rank_fixture_channels(rows, home, away):
    """Put exact fixture channels first without removing generic PPV slots."""
    def forms(team):
        raw = str(team or "").lower().strip()
        values = set()
        for alias in _expand_terms(raw):
            cleaned = normalise(alias)
            if len(cleaned) >= 3:
                values.add(cleaned)
        cleaned = normalise(raw)
        if len(cleaned) >= 3:
            values.add(cleaned)
        return values
    home_forms, away_forms = forms(home), forms(away)
    ranked = []
    for index, original in enumerate(rows):
        row = dict(original)
        hay = normalise_event_name(row.get("xtream_name", ""))
        def hit(values):
            return any(re.search(r"(?<![a-z0-9])" + re.escape(value) +
                                 r"(?![a-z0-9])", hay) for value in values)
        home_hit, away_hit = hit(home_forms), hit(away_forms)
        row["fixture_match"] = "exact" if home_hit and away_hit else (
            "partial" if home_hit or away_hit else "generic")
        # A one-team event title usually belongs to a different fixture; rank
        # unknown/generic PPV slots above it because those may carry this game.
        priority = 2 if row["fixture_match"] == "exact" else (
            1 if row["fixture_match"] == "generic" else 0)
        ranked.append((priority, float(row.get("score") or 0), index, row))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in ranked]

def find_team_channels(team_terms, xtream_channels, cats, x):
    """Find plausible match-specific PPV/event channels.

    Event-named channels must contain both fixture teams. A channel whose
    complete identity is one team name is retained as a possible club channel.
    """
    side_forms = []
    for team in team_terms:
        raw = str(team or "").lower().strip()
        if len(raw) < 3:
            continue
        forms = set()
        for alias in _expand_terms(raw):
            cleaned = normalise(alias)
            if len(cleaned) >= 3:
                forms.add(cleaned)
        cleaned = normalise(raw)
        if len(cleaned) >= 3:
            forms.add(cleaned)
        if forms:
            side_forms.append(forms)
    out = []
    for ch in xtream_channels:
        cname = ch["name"]
        hay = normalise_event_name(cname)
        category = cats.get(ch["category_id"], "")
        hits = 0
        team_branded = False
        hay_tokens = set(hay.split())
        for forms in side_forms:
            matched_forms = [form for form in forms
                             if re.search(r"(?<![a-z0-9])" + re.escape(form) +
                                          r"(?![a-z0-9])", hay)]
            if matched_forms:
                hits += 1
                if any(set(form.split()) <= hay_tokens and
                       all(token in _GENERIC for token in
                           (hay_tokens - set(form.split())))
                       for form in matched_forms):
                    team_branded = True
        strong_event_name = hits >= 2
        if strong_event_name or team_branded:
            out.append({
                "xtream_name": cname, "stream_id": ch["stream_id"],
                "category": category,
                "logo": ch.get("stream_icon", ""),
                "quality": quality_tag(cname),
                "url": x.stream_url(ch["stream_id"]),
            })
    return out

_RACING_CHANNEL_TERMS = {
    "f1": ("f1", "formula 1", "formula one"),
    "f2": ("f2", "formula 2"),
    "f3": ("f3", "formula 3"),
    "indycar": ("indycar", "indy car"),
    "wec": ("wec", "world endurance"),
    "formulae": ("formula e", "formulae"),
    "motogp": ("motogp", "moto gp"),
    "wrc": ("wrc", "world rally"),
}
_RACING_AVAILABILITY_CACHE = {"key": "", "ts": 0, "availability": {}}
_RACING_AVAILABILITY_TTL = 15 * 60
_SPORTS_EVENT_CHANNEL_CACHE = {}
_SPORTS_EVENT_CHANNEL_TTL = 15 * 60

def _sports_availability_cache_path():
    return os.path.join(data_cache_dir(), "sports-availability.json")

def _sports_cache_signature(cfg, x):
    return "football-v5|" + _vod_cache_key(x) + "|" + str(
        cfg.get("match_threshold") or 0.62)

def _sports_result_for_storage(result):
    clean = dict(result or {})
    for key in ("matches", "ppv_hits"):
        clean[key] = [{k: v for k, v in dict(row).items() if k != "url"}
                      for row in (clean.get(key) or []) if isinstance(row, dict)]
    return clean

def _sports_result_for_client(result, x):
    hydrated = dict(result or {})
    for key in ("matches", "ppv_hits"):
        rows = []
        for stored in hydrated.get(key) or []:
            row = dict(stored)
            if row.get("stream_id") is not None:
                row["url"] = x.stream_url(row["stream_id"])
            rows.append(row)
        hydrated[key] = rows
    return hydrated

def _load_sports_disk_cache(cfg, x):
    try:
        with open(_sports_availability_cache_path(), "r", encoding="utf-8") as f:
            cached = json.load(f) or {}
        if cached.get("signature") != _sports_cache_signature(cfg, x):
            return {}
        entries = cached.get("entries") or {}
        return entries if isinstance(entries, dict) else {}
    except Exception:
        return {}

def _save_sports_disk_cache(cfg, x, entries):
    try:
        # Finished fixtures naturally disappear as fresh schedules replace the
        # cache; cap the file as an additional guard against indefinite growth.
        ordered = sorted(entries.items(), key=lambda item: float(
            (item[1] or {}).get("ts") or 0), reverse=True)[:500]
        with _CACHE_WRITE_LOCK:
            _atomic_write_json(_sports_availability_cache_path(), {
                "signature": _sports_cache_signature(cfg, x),
                "entries": dict(ordered)}, compact=True)
    except Exception:
        pass

def _clear_racing_availability_cache():
    _RACING_AVAILABILITY_CACHE.update({"key": "", "ts": 0, "availability": {}})

def _clear_sports_event_channel_cache():
    _SPORTS_EVENT_CHANNEL_CACHE.clear()
    try:
        os.remove(_sports_availability_cache_path())
    except OSError:
        pass

def _sports_event_key(home, away, start):
    return "|".join((normalise(str(home or "")), normalise(str(away or "")),
                     str(start or "")[:16]))

def find_sports_event_channels(fixture, cfg):
    """Match channels for one selected football fixture only."""
    x = Xtream(cfg)
    if not x.configured():
        return {"logged_in": False, "matches": [], "ppv_hits": []}
    channels, cats = get_xtream_channels(cfg)
    return _match_sports_fixture_channels(fixture, cfg, channels, cats, x)

def _match_sports_fixture_channels(fixture, cfg, channels, cats, x):
    """Match one fixture using an already-loaded Xtream catalogue."""
    threshold = max(0.40, min(0.80, float(cfg.get("match_threshold", 0.62) or 0.62)))
    matches = rank_fixture_channels(
        match_channels(fixture.get("by_country") or {}, channels, cats, threshold),
        fixture.get("home"), fixture.get("away"))
    for row in matches:
        row["url"] = x.stream_url(row["stream_id"])
    hits = find_team_channels([fixture.get("home", ""), fixture.get("away", "")],
                              channels, cats, x)
    have = {str(row.get("stream_id")) for row in matches}
    ppv_hits = [row for row in hits if str(row.get("stream_id")) not in have]
    return {"logged_in": True, "availability_checked": True,
            "matches": matches, "ppv_hits": ppv_hits}

def _racing_event_key(event):
    return "|".join(str(event.get(k) or "") for k in
                    ("series", "race", "session", "start"))

def find_racing_channels(event, xtream_channels, cats, x):
    """Find dedicated racing or event-named channels already in Xtream.

    Event-name-only hits require a PPV/Play/Event context; dedicated series
    names such as Sky Sports F1 / WRC are strong enough on their own.
    """
    series = str(event.get("series") or "").lower()
    aliases = tuple(normalise(v) for v in _RACING_CHANNEL_TERMS.get(series, ()))
    event_words = []
    ignored = {"grand", "prix", "race", "rally", "weekend", "circuit",
               "practice", "qualifying", "sprint", "round", "del", "de"}
    for value in (event.get("race"), event.get("circuit")):
        for word in _distinctive(normalise(str(value or "")).split()):
            if word not in ignored and len(word) >= 3 and word not in event_words:
                event_words.append(word)
    out = []
    for ch in xtream_channels:
        cname = str(ch.get("name") or "")
        hay = normalise(cname)
        if not hay:
            continue
        category = cats.get(ch.get("category_id"), "")
        padded = " " + hay + " "
        series_hit = any((" " + alias + " ") in padded for alias in aliases if alias)
        event_hits = sum(1 for word in event_words
                         if re.search(r"(?<![a-z0-9])" + re.escape(word) +
                                      r"(?![a-z0-9])", hay))
        # One distinctive place/event word is enough inside an explicit PPV
        # category (e.g. "Dutch Grand Prix" need not also say Zandvoort).
        event_hit = event_hits >= 1
        ppv_context = _is_ppv_category(category) or _is_ppv_category(cname)
        event_context = ppv_context or _is_4k_category(category)
        if not series_hit and not (event_hit and event_context):
            continue
        # Event title beats a dedicated series channel; generic series entries
        # in PPV/Play/Event buckets remain useful but are only possible matches.
        match_kind = ("event" if event_hit else
                      ("possible" if ppv_context else "series"))
        out.append({"xtream_name": cname, "stream_id": ch.get("stream_id"),
                    "category": category, "logo": ch.get("stream_icon", ""),
                    "quality": quality_tag(cname),
                    "match_kind": match_kind,
                    "url": x.stream_url(ch.get("stream_id"))})
    # Stable unique IDs; a provider can occasionally expose duplicate rows.
    seen, unique = set(), []
    for row in out:
        sid = str(row.get("stream_id"))
        if sid in seen:
            continue
        seen.add(sid); unique.append(row)
    order = {"event": 0, "series": 1, "possible": 2}
    unique.sort(key=lambda row: (order.get(row.get("match_kind"), 3),
                                 str(row.get("category") or ""),
                                 str(row.get("xtream_name") or "")))
    return unique[:30]

def ppv_categories(xtream_channels, cats):
    """Return [{'category':name,'count':n}] for PPV/Play categories present
    in the user's channel list, so the UI can point the user there."""
    counts = {}
    for ch in xtream_channels:
        cat = cats.get(ch["category_id"], "")
        if _is_ppv_category(cat):
            counts[cat] = counts.get(cat, 0) + 1
    return [{"category": k, "count": v} for k, v in
            sorted(counts.items(), key=lambda kv: -kv[1])]

# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Olo's TVMate</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.6.17/dist/hls.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.8.1/dist/mpegts.min.js"></script>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 240 240'%3E%3Crect x='26' y='58' width='150' height='120' rx='16' fill='%233a2c1f' stroke='%23241a12' stroke-width='4'/%3E%3Crect x='38' y='70' width='126' height='96' rx='8' fill='%231b3a6b'/%3E%3Cellipse cx='101' cy='140' rx='44' ry='11' fill='%23e7a94e'/%3E%3Cellipse cx='101' cy='128' rx='42' ry='11' fill='%23f0b95e'/%3E%3Cellipse cx='101' cy='116' rx='40' ry='11' fill='%23f5c56e'/%3E%3Crect x='86' y='86' width='30' height='14' rx='5' fill='%23ffd77a'/%3E%3Ccircle cx='192' cy='86' r='8' fill='%232a2a2a'/%3E%3Ccircle cx='192' cy='112' r='8' fill='%232a2a2a'/%3E%3Crect x='150' y='40' width='4' height='24' fill='%23241a12'/%3E%3Crect x='118' y='40' width='4' height='24' fill='%23241a12' transform='rotate(-28 120 52)'/%3E%3C/svg%3E">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0f1115;--card:#181b22;--card2:#1e222b;--fg:#e6e8ee;--mut:#8a90a0;--acc:#4f8cff;--line:#262a34;--line2:#313747}
 *{box-sizing:border-box}
 body{margin:0;font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
 header{padding:12px 22px;border-bottom:1px solid var(--line);display:flex;gap:17px;align-items:center;position:sticky;top:0;z-index:220;background:rgba(15,17,21,.94);backdrop-filter:blur(12px);box-shadow:0 5px 18px rgba(0,0,0,.16)}
 .slogan{position:static;transform:none;margin-left:auto;margin-right:auto;min-width:0;overflow:hidden;text-overflow:ellipsis;font-size:13px;font-style:italic;color:var(--mut);white-space:nowrap;letter-spacing:.2px;pointer-events:none}
 #status{margin-left:0;flex:0 0 auto}
 header h1{font-size:16px;margin:0;font-weight:600}
 header a{color:var(--mut);text-decoration:none;font-size:14px;cursor:pointer;padding:4px 0 3px;border-bottom:2px solid transparent;flex:0 0 auto;transition:color .13s,border-color .13s}
 header a:hover{color:var(--fg)}
 header a.on{color:var(--fg);border-bottom-color:var(--acc)}
 .langsel{display:flex;gap:6px;margin-left:14px}
 .headerstop{font-size:12px;padding:5px 10px;margin-left:4px;white-space:nowrap;flex:0 0 auto}
 .langflag{background:none;border:1px solid transparent;border-radius:6px;padding:2px 6px;font-size:17px;line-height:1;cursor:pointer;opacity:.45;filter:grayscale(.5);transition:all .12s}
 .langflag:hover{opacity:.85;filter:none}
 .langflag.on{opacity:1;filter:none;border-color:var(--line2);background:var(--card2)}
 @media(max-width:1500px){header{gap:12px;padding-left:16px;padding-right:16px}header a{font-size:13px}.slogan{font-size:11px}}
 @media(max-width:1100px){header{overflow-x:auto;scrollbar-width:none}header::-webkit-scrollbar{display:none}.slogan{display:none}.langsel{margin-left:auto}}
 .updatebanner{position:fixed;top:0;left:0;right:0;background:#16233d;border-bottom:1px solid var(--acc);padding:10px 18px;display:flex;align-items:center;gap:12px;justify-content:center;z-index:300;font-size:14px;box-shadow:0 4px 14px rgba(0,0,0,.4)}
 .updatebanner button{font-size:13px;padding:5px 14px}
 .pmodal{position:fixed;top:clamp(110px,18vh,210px);left:auto;right:4px;bottom:auto;width:min(1040px,calc(100vw - 8px));height:min(650px,68vh);background:transparent;display:block;z-index:400}
 .pmodal.hide{display:none}
 .pmodal.sectionmax{top:0;left:0;right:0;bottom:0;width:100vw;height:100vh} .pmodal.sectionmax .pbox{border:0;border-radius:0;box-shadow:none}
 @media(min-width:1800px) and (max-width:2199px){.tvplayerslot.mini,.pmodal:not(.sectionmax){width:min(1040px,38vw);height:min(650px,23.75vw,68vh)}}
 @media(min-width:2200px){.tvplayerslot.mini,.pmodal:not(.sectionmax){width:min(1040px,40vw);height:min(650px,25vw,68vh)}}
 .pbox{position:relative;background:#0c0e12;border:1px solid var(--line);border-radius:12px;width:100%;height:100%;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5)}
 .teamtabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
 .teamtab{background:var(--card);border:1px solid var(--line);color:var(--mut);padding:8px 16px;border-radius:8px;cursor:pointer;font-size:14px}
 .teamtab.on{background:var(--acc);border-color:var(--acc);color:#08131f;font-weight:600}
 .teamtab:hover{filter:brightness(1.1)}
 #teamFixtures{display:flex;gap:12px;align-items:flex-start;overflow-x:auto;padding:2px 1px 10px;scrollbar-color:#48515f transparent;scrollbar-width:thin;scroll-snap-type:x proximity}
 #teamFixtures>.card{flex:0 0 min(410px,88vw);margin:0;scroll-snap-align:start}
 .matchfixture{padding:0!important;overflow:hidden;border-color:var(--line2)!important;background:linear-gradient(180deg,#181c23,#15181e)!important}
 .matchfixturehead{padding:13px 14px 12px;border-bottom:1px solid var(--line);background:rgba(27,32,41,.75)}
 .matchfixtureteamsline{display:flex;align-items:center;gap:9px;min-width:0}.matchfixtureteam{display:flex;align-items:center;gap:7px;min-width:0;font-size:14px;font-weight:650}.matchfixtureteam span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .matchfixtureteamlogo{width:30px;height:30px;flex:0 0 30px;object-fit:contain;filter:drop-shadow(0 2px 3px rgba(0,0,0,.28))}.matchfixtureversus{color:var(--mut);font-size:11px;flex:0 0 auto}
 .matchfixturemeta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px;color:var(--mut);font-size:11px}.matchfixturemeta .live,.matchfixturemeta .soon,.matchfixturemeta .ended{margin:0}
 .matchfixtureavailability{margin-left:auto;border:1px solid #315f3b;background:#142219;color:#82d492;border-radius:999px;padding:2px 7px;font-size:10px;white-space:nowrap}.matchfixtureavailability.none{border-color:var(--line2);background:var(--card);color:var(--mut)}
 .matchfixturebody{padding:10px 12px 12px}.matchfixturebody>.muted:first-child{display:block;padding:8px 2px}
 .bcastlist{margin-top:10px;display:flex;flex-direction:column;gap:6px}
 .bcrow{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--card)}
 .bchead{padding:9px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;font-size:14px;user-select:none}
 .bchead:hover{background:var(--card2)}
 .bcrow.open .bchead{border-bottom:1px solid var(--line);background:var(--card2)}
 .bcname{font-weight:500;color:var(--fg)}
.exphint{margin-left:auto;font-size:12px}
 .bcchevron{margin-left:auto;color:var(--mut);font-size:13px;transition:transform .13s}.bcrow.open .bcchevron{transform:rotate(180deg)}
 .bcchans{display:flex;flex-direction:column}
 .chline{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-top:1px solid var(--line)}
 .chline:first-child{border-top:0}
 .chanlogo{width:28px;height:28px;object-fit:contain;flex:0 0 28px;border-radius:4px}
 .chanlogo.tvlogo{width:26px;height:26px;flex-basis:26px}
 .chanlogo.mini{width:22px;height:22px;flex-basis:22px}
 .matchchan{display:flex;align-items:center;min-width:0;flex:1}
 .matchchan .favstar{display:inline-block;color:#78808e;margin-right:9px}
 .matchchan .favstar.on{color:#f5c542}
 .chn{font-size:13px}
 .chbtns{display:flex;gap:6px;flex-shrink:0}
 .pbar{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--line);font-size:14px;font-weight:500}
 .pclose{background:none;border:0;color:var(--mut);font-size:24px;line-height:1;cursor:pointer;padding:0 4px}
 .pclose:hover{color:var(--fg);filter:none}
 #pVideo{width:100%;height:auto;max-height:none;flex:1;min-height:0;background:#000;display:block;object-fit:cover}
 .btnplay{background:var(--acc);border:0;color:#fff;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer;margin-right:5px}
 .btnvlc{background:#e8701a;border:0;color:#fff;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer;margin-right:5px}
 .btnplay:hover,.btnvlc:hover{filter:brightness(1.1)}
 .favstar{flex-shrink:0;cursor:pointer;color:var(--line2);font-size:17px;line-height:1;margin-right:9px;transition:color .1s;user-select:none}
 .favstar:hover{color:#e8b84a}
 .favstar.on{color:#f5c542}
 .favrm{background:none;border:1px solid var(--line2);color:var(--mut);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer}
 .favrm:hover{border-color:#ff7676;color:#ff7676}
 .favcat{display:flex;align-items:center;gap:8px;padding:8px 10px;font-size:13px;cursor:pointer;border-radius:7px;margin-bottom:2px;transition:background .1s}
 .favcat:hover{background:var(--card2)}
 .favcat.active{background:#1b2540;box-shadow:inset 0 0 0 1px #33406b}
 .mylayout{display:flex;gap:20px;align-items:flex-start}
 .mlcats{width:280px;flex-shrink:0}
 .mlchans{flex:1;min-width:0}
 .favgrid{display:flex;flex-direction:column;gap:8px}
 .favcard{border:1px solid var(--line);border-radius:9px;padding:10px 14px;background:var(--card);display:flex;align-items:center;gap:12px}
 .favcardname{font-size:13px;font-weight:500;line-height:1.3;word-break:break-word;flex:1;min-width:0}
 .favcardbtns{display:flex;flex-shrink:0;gap:5px}
 .favcardbtns button{font-size:11px;padding:4px 9px}
 @media(max-width:900px){.mylayout{flex-direction:column}.mlcats{width:100%}}
 .mydash{max-width:1400px;margin:0 auto;padding:4px 8px 36px}
 .mylistprofile{display:flex;align-items:center;gap:14px;margin:2px 0 26px;padding-bottom:16px;border-bottom:1px solid var(--line)}
 .mylistprofileemblem{width:64px;height:64px;flex:0 0 64px;display:flex;align-items:center;justify-content:center}
 .mylistprofileemblem svg{width:64px;height:64px;display:block}
 .mylistprofilename{font-size:22px;font-weight:650}
 .editprofilebtn{padding:5px 9px;font-size:11px;white-space:nowrap}
 .mydashblock{margin-bottom:30px}
 .mydashhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
 .mydashgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:10px}
 .mydashteam{display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:14px;font-weight:650}
 .mydashteam img{width:32px;height:32px;object-fit:contain}
 .mydashfixture{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:12px;min-width:0}
 .mydashfixture .teamfixture{border:0;background:transparent;padding:0}
 .mydashepisodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
 .mydashepisode{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:10px;display:flex;gap:10px;min-height:125px;cursor:pointer}
 .mydashepisode:hover{border-color:var(--line2)}
 .mydashepisode img{width:72px;height:108px;object-fit:cover;border-radius:6px;flex:0 0 72px}
 .mydashepisodeinfo{display:flex;flex-direction:column;min-width:0;flex:1}
 .mydashepisodename{font-size:14px;font-weight:650;margin-bottom:6px}
 .mydashwhen{font-size:11px;color:var(--acc);margin-bottom:5px}
 .mydashchannels{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
 .mydashchannel{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:12px;min-height:68px;display:flex;align-items:center;justify-content:space-between;gap:10px}
 .mydashchannelname{font-size:14px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .mydashchooser{margin-top:10px;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px}
 .mydashchoice{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;cursor:pointer}
 .mydashchoice:hover{background:var(--card2)}
 .f1chooser{grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
 .f1choice{border:0;color:var(--fg);background:transparent;text-align:left;width:100%}
 .f1choice.on{background:rgba(202,44,44,.12);box-shadow:inset 0 0 0 1px rgba(218,72,72,.45)}
 .f1choice img{width:28px;height:28px;object-fit:contain;flex:0 0 28px}
 .mydash.layout-spotlight{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(280px,.8fr);gap:0 26px}
 .mydash.layout-spotlight #myListProfile{grid-column:1/-1}
 .mydash.layout-spotlight #myListShowsBlock{grid-column:1;grid-row:2}
 .mydash.layout-spotlight #myListTeamsBlock{grid-column:2;grid-row:2/4}
 .mydash.layout-spotlight #myListChannelsBlock{grid-column:1;grid-row:3}
 .mydash.layout-spotlight #myListTeams{grid-template-columns:1fr}
 .mydash.layout-spotlight .mydashepisodes{grid-template-columns:repeat(2,minmax(0,1fr))}
 .mydash.layout-spotlight .mydashepisode:first-child{grid-column:1/-1;position:relative;overflow:hidden;min-height:230px;padding:0;align-items:flex-end;background:var(--card)}
 .mydash.layout-spotlight .mydashepisode:first-child>img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.38;filter:saturate(.8)}
 .mydash.layout-spotlight .mydashepisode:first-child:after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,rgba(10,12,16,.96) 0%,rgba(10,12,16,.48) 48%,rgba(10,12,16,.05) 100%);pointer-events:none}
 .mydash.layout-spotlight .mydashepisode:first-child .mydashepisodeinfo{position:relative;z-index:1;align-self:flex-end;padding:18px 20px;justify-content:flex-end;min-height:150px}
 .mydash.layout-spotlight .mydashepisode:first-child .mydashepisodename{font-size:19px;margin-bottom:5px}
 .mydash.layout-spotlight .mydashepisode:not(:first-child){min-height:112px;padding:9px}
 .mydash.layout-spotlight .mydashepisode:not(:first-child)>img{width:58px;height:87px;flex-basis:58px}
 .mydash.layout-spotlight .mydashfixture{background:transparent;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:12px 0}
 .mydash.layout-spotlight .mydashfixture:last-child{border-bottom:0}
 .mydash.layout-spotlight .mydashchannels{grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
 .mydash.layout-spotlight .mydashchannel{background:transparent;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:8px 0;min-height:54px}
 .mydash.layout-spotlight .mydashchannel .btnvlc{padding:4px 7px;font-size:11px}
 .mydash.layout-hub{display:grid;grid-template-columns:220px minmax(0,1fr);gap:0 28px}
 .mydash.layout-hub #myListProfile{grid-column:1;grid-row:1/5;align-self:start;flex-direction:column;align-items:flex-start;border-bottom:0;border-right:1px solid var(--line);padding:8px 24px 20px 0;min-height:330px}
 .mydash.layout-hub #myListTeamsBlock,.mydash.layout-hub #myListShowsBlock,.mydash.layout-hub #myListChannelsBlock{grid-column:2}
 .mydash.layout-hub .mydashgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .mydash.layout-hub .mydashepisodes{grid-template-columns:repeat(3,minmax(0,1fr))}
 .mydash.layout-timeline{display:grid;width:100%;max-width:2200px;grid-template-columns:clamp(460px,31vw,650px) minmax(0,1fr);grid-template-rows:auto auto 1fr;gap:0 30px;align-items:start}
 .mydash.layout-timeline #myListProfile{grid-column:1;grid-row:1;flex-direction:row;align-items:center;gap:12px;margin:0 0 20px;padding:4px 0 18px}
 .mydash.layout-timeline #myListProfile .mylistprofileemblem{width:52px;height:52px;flex-basis:52px}
 .mydash.layout-timeline #myListProfile .mylistprofileemblem svg{width:52px;height:52px}
 .mydash.layout-timeline #myListProfile .mylistprofilename{font-size:20px;line-height:1.15;min-width:0;overflow-wrap:anywhere}
 .mydash.layout-timeline #myListTeamsBlock{grid-column:1;grid-row:2;margin-bottom:24px}
 .mydash.layout-timeline #myListChannelsBlock{grid-column:1;grid-row:3;margin-bottom:0}
 .mydash.layout-timeline #myListTimelineBlock{grid-column:2;grid-row:1/4;margin:0;min-width:0}
 .mydash.layout-timeline #myListTeams{grid-template-columns:1fr;gap:0}
 .mydash.layout-timeline .mydashfixture{background:transparent;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:10px 0}
 .mydash.layout-timeline .mydashteamonly{display:flex;align-items:center;gap:14px;border:1px solid var(--line);background:var(--card);border-radius:9px;padding:10px 12px;min-height:82px;margin-bottom:8px;cursor:pointer;transition:border-color .12s,background .12s}
 .mydash.layout-timeline .mydashteamonly:hover{border-color:var(--line2);background:var(--card2)}
 .mydash.layout-timeline .mydashteamonly img{width:58px;height:58px;object-fit:contain;flex:0 0 58px}
 .mydash.layout-timeline .mydashteamonly img.driver{width:58px;height:74px;object-fit:cover;object-position:top center;flex-basis:58px;border-radius:6px}
 .mydash.layout-timeline .mydashteamonly img.driver.car{object-fit:contain;object-position:center;background:#0d1014;padding:3px;box-sizing:border-box}
 .mydashsportinfo{display:flex;flex-direction:column;justify-content:center;gap:3px;min-width:0;flex:1}
 .mydashsportname{font-size:14px;font-weight:700;line-height:1.2}
 .mydashsportmeta{font-size:11px;color:var(--mut);line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .mydashsportevent{display:flex;flex-direction:column;justify-content:center;gap:3px;width:205px;flex:0 0 205px;min-height:58px;padding-left:8px;min-width:0}
 .mydashsportevent .series{font-size:11px;color:var(--mut);line-height:1.2}
 .mydashsportevent .team{font-size:11px;color:var(--mut);line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .mydashsportnext{font-size:13px;color:var(--fg);font-weight:650;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .mydashsportcount{font-size:13px;color:var(--acc);line-height:1.25}
 .mydashsportphotos{display:flex;align-items:center;gap:5px;flex:0 0 auto}
 .mydashsportphotos img.driver{width:52px;height:74px;flex-basis:52px}
 .mydashsportnames{display:flex;flex-direction:column;gap:7px;min-width:115px;flex:1}
 .mydashsportnames .mydashsportname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .mydashsportsingle{display:flex;flex-direction:column;justify-content:center;gap:6px;min-width:0;flex:1}
 .mydashsportsingletop{display:grid;grid-template-columns:minmax(120px,1fr) minmax(200px,260px);align-items:center;gap:16px;min-width:0}
 .mydashsportsingletop .mydashsportname,.mydashsportsingletop .mydashsportnext{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .mydashsporteventline{display:flex;align-items:baseline;justify-content:center;justify-self:end;width:100%;gap:6px;min-width:0;text-align:center}
 .mydashsporteventline .mydashsportnext{flex:0 1 auto}
 .mydashsporteventline .mydashsportcount{flex:0 0 auto;white-space:nowrap}
 .mydashf1names{display:flex;flex-direction:column;gap:5px;min-width:0;align-self:start}
 .mydashf1names .mydashsportname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .mydashf1card .mydashsporteventline{align-self:center}
 .mydashsportheading{font-size:10px;font-weight:750;letter-spacing:.75px;text-transform:uppercase}
 .mydashsportheading.sport{color:#70c987}
 .mydashsportsubhead{font-size:10px;font-weight:750;letter-spacing:.75px;text-transform:uppercase;margin:15px 0 8px;padding-left:2px}
 .mydashsportsubhead.racing{color:#ef7777}
 .mydashliveheading{cursor:pointer}
 .mydashliveheading:hover{color:var(--acc)}
 .mydash.layout-timeline .mydashchannels{grid-template-columns:1fr;gap:8px}
 .mydash.layout-timeline .mydashchannel{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-height:70px;display:grid;grid-template-columns:44px minmax(0,1fr) auto;grid-template-rows:1fr;gap:10px;align-items:center;transition:border-color .12s,background .12s}
 .mydash.layout-timeline .mydashchannel:hover{border-color:var(--line2);background:var(--card2)}
 .mydash.layout-timeline .mydashchannel .chanlogo{grid-column:1;grid-row:1;width:44px;height:44px;object-fit:contain}
 .mydash.layout-timeline .mydashchannelname{grid-column:2;grid-row:1;font-size:12px;line-height:1.3;white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .mydash.layout-timeline .mydashchannel .btnvlc{grid-column:3;grid-row:1;justify-self:end;padding:5px 9px;font-size:11px;margin:0}
 .mydash.layout-timeline .mydashchannel.muted{display:flex;align-items:center;justify-content:center;text-align:center;font-size:10px}
 .mytimelinecontrols{position:relative;display:flex;align-items:center;justify-content:flex-end;gap:14px;min-height:26px;margin:-30px 0 14px;padding-right:2px}
 .mytimelinefilter{appearance:none;background:transparent;border:0;border-bottom:2px solid transparent;border-radius:0;color:var(--mut);padding:4px 1px 5px;font:inherit;font-size:11px;font-weight:650;cursor:pointer}
 .mytimelinefilter:hover{color:var(--fg)}
 .mytimelinefilter.on.all{color:var(--fg);border-bottom-color:var(--acc)}
 .mytimelinefilter.on.show{color:#e5a25f;border-bottom-color:#c8752c}
 .mytimelinefilter.on.movie{color:#72aee8;border-bottom-color:#3e82c5}
 .mytimelinefilter.on.game{color:#b695e8;border-bottom-color:#7651b7}
 .mytimelinefilter.on.sport{color:#70c987;border-bottom-color:#38a85d}
 .mytimelinefilter.on.f1{color:#ef7777;border-bottom-color:#d83a3a}
 .mytimelinefilter.settings{margin-left:18px;color:var(--mut)}
 .mytimelinefilter.settings.changed{color:var(--fg);border-bottom-color:var(--acc)}
 .mytimelinefilterpanel{position:absolute;z-index:30;right:0;top:32px;width:270px;background:var(--card2);border:1px solid var(--line2);border-radius:9px;padding:12px 14px;box-shadow:0 14px 38px #0009}
 .mytimelinefilterpanel h4{margin:0 0 10px;font-size:12px;color:var(--fg)}
 .mytimelinefilterpanel label{display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--mut);font-size:11px;padding:5px 0}
 .mytimelinefilterpanel .timelinechecks{display:flex;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:6px}
 .mytimelinefilterpanel .timelinechecks label{justify-content:flex-start;gap:5px;padding:2px 0}
 .mytimelinefilterpanel select{min-width:112px;background:var(--bg);color:var(--fg);border:1px solid var(--line2);border-radius:6px;padding:5px 7px}
 .timelinefilterreset{width:100%;margin-top:8px;background:transparent;border:1px solid var(--line2);color:var(--mut);border-radius:6px;padding:6px;cursor:pointer}
 @media(max-width:900px){.mytimelinecontrols{margin:0 0 14px;justify-content:flex-start;gap:10px;flex-wrap:wrap}.mytimelinefilter.settings{margin-left:0}.mytimelinefilterpanel{left:0;right:auto}}
 .mylisttimeline{border-left:1px solid var(--line2);margin-left:9px;padding-left:22px;display:flex;flex-direction:column;gap:0}
 .mylisttimelinesection{position:relative;margin:2px 0 11px;font-size:10px;font-weight:750;letter-spacing:.8px;text-transform:uppercase;color:var(--mut)}
 .mylisttimelinesection:before{content:"";position:absolute;left:-27px;top:4px;width:9px;height:9px;border-radius:50%;background:var(--line2);box-shadow:0 0 0 3px var(--bg)}
 .mylisttimelinesection.live{margin:2px 0 11px;background:transparent;border:0;border-radius:0;padding:0;color:#ff6570;font-size:10px;box-shadow:none}
 .mylisttimelinesection.live:before{left:-27px;top:4px;background:#e44752;box-shadow:0 0 0 3px var(--bg),0 0 10px #e44752}
 .mylisttimelinesection.upcoming{color:var(--acc)}
 .mylisttimelineentry{position:relative;padding:0 0 20px;min-width:0}
 .mylisttimelineentry.is-live .mylisttimelinebody{border-color:#713039;background:linear-gradient(90deg,rgba(62,19,25,.32),var(--card) 28%);box-shadow:inset 3px 0 0 #d43b46}
 .mylisttimelineentry.is-live:before{background:#e44752;box-shadow:0 0 0 3px var(--bg),0 0 9px rgba(228,71,82,.7)}
 .mylisttimelineentry:before{content:"";position:absolute;left:-27px;top:8px;width:9px;height:9px;border-radius:50%;background:var(--acc)}
 .mylisttimelinewhen{font-size:11px;color:var(--acc);margin-bottom:4px;font-weight:600;text-transform:uppercase;letter-spacing:.3px}
 .mylisttimelinebody{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:11px 13px}
 .mylisttimelinebody .teamfixture{border:0;background:transparent;padding:0}
 .mylisttimelineepisode{display:flex;align-items:center;gap:10px;cursor:pointer}
 .mylisttimelineepisode img{width:46px;height:69px;object-fit:cover;border-radius:5px;flex:0 0 46px}
 .mylisttimelineavail{margin-left:auto;align-self:center;flex:0 0 auto}
 .mylisttimelinegame{display:flex;align-items:center;gap:10px;cursor:pointer}
 .mylisttimelinef1{cursor:pointer}
 .mylisttimelinegame>img{width:112px;height:52px;object-fit:cover;border-radius:6px;flex:0 0 112px}
 .mylisttimelinecontent{display:flex;align-items:center;gap:12px;min-width:0}
 .mylisttimelinecontent>.teamfixture{flex:1;min-width:0}
 .mylisttimelineart{width:58px;height:58px;flex:0 0 58px;object-fit:contain;border-radius:7px;background:#0d1014;padding:5px;box-sizing:border-box}
 .mylisttimelineart.driver{height:72px;object-fit:cover;object-position:center top;padding:0}
 .mylisttimelineart.driver.car{object-fit:contain;object-position:center;padding:3px}
 .mylisttimelinedrivers{width:72px;height:72px;flex:0 0 72px;display:flex;align-items:flex-end;justify-content:center;gap:2px;overflow:hidden;border-radius:7px;background:#0d1014;padding:3px 2px 0;box-sizing:border-box}
 .mylisttimelinedrivers img{width:34px;height:68px;object-fit:contain;object-position:center bottom;min-width:0}
 .mylisttimelinekind{flex:0 0 50px;width:50px;color:var(--mut);font-size:10px;font-weight:650;letter-spacing:.7px;text-transform:uppercase;text-align:center;padding:8px 0 5px;align-self:center;border-bottom:2px solid transparent}
 .mylisttimelinekind.sport{color:#70c987;border-bottom-color:#38a85d}
 .mylisttimelinekind.show{color:#e5a25f;border-bottom-color:#c8752c}
 .mylisttimelinekind.movie{color:#72aee8;border-bottom-color:#3e82c5}
 .mylisttimelinekind.f1{color:#ef7777;border-bottom-color:#d83a3a}
 .mylisttimelinekind.game{color:#b695e8;border-bottom-color:#7651b7}
 .mytimelinepage{max-width:1120px;margin:0 auto;padding:4px 0 28px}
 .mytimelinepage>.colh{margin-bottom:18px}
 @media(min-width:2200px){
   header{padding:16px 28px;gap:20px}
   header h1{font-size:17px}
   header a{font-size:15px}
   .slogan{font-size:14px}
   .mydash{max-width:1720px;padding:14px 12px 44px}
   .mydash.layout-timeline{grid-template-columns:650px minmax(0,1fr);gap:0 44px}
   .mydash.layout-timeline #myListProfile{gap:15px;margin-bottom:24px;padding-bottom:20px}
   .mydash.layout-timeline #myListProfile .mylistprofileemblem{width:62px;height:62px;flex-basis:62px}
   .mydash.layout-timeline #myListProfile .mylistprofileemblem svg{width:62px;height:62px}
   .mydash.layout-timeline #myListProfile .mylistprofilename{font-size:23px}
   .mydash.layout-timeline .mydashteamonly{gap:16px;padding:12px 14px;min-height:94px}
   .mydash.layout-timeline .mydashteamonly img{width:66px;height:66px;flex-basis:66px}
   .mydash.layout-timeline .mydashteamonly img.driver{width:66px;height:84px;flex-basis:66px}
   .mydash.layout-timeline .mydashsportname{font-size:15px}
   .mydash.layout-timeline .mydashsportmeta,.mydash.layout-timeline .mydashsportevent .series,.mydash.layout-timeline .mydashsportevent .team{font-size:12px}
   .mydash.layout-timeline .mydashsportnext,.mydash.layout-timeline .mydashsportcount{font-size:14px}
   .mydash.layout-timeline .mydashsportevent{width:225px;flex-basis:225px;padding-left:10px}
   .mydash.layout-timeline .mydashsportphotos img.driver{width:58px;height:84px;flex-basis:58px}
   .mydash.layout-timeline .mydashchannels{gap:10px}
   .mydash.layout-timeline .mydashchannel{min-height:82px;padding:12px 14px;grid-template-columns:50px minmax(0,1fr) auto;gap:12px}
   .mydash.layout-timeline .mydashchannel .chanlogo{width:50px;height:50px}
   .mydash.layout-timeline .mydashchannelname{font-size:13px}
   .mydash.layout-timeline .mydashchannel .btnvlc{font-size:11px;padding:5px 9px}
   .mylisttimeline{padding-left:28px}
   .mylisttimelineentry{padding-bottom:24px}
   .mylisttimelineentry:before{left:-33px;width:10px;height:10px}
   .mylisttimelinewhen{font-size:12px;margin-bottom:6px}
   .mylisttimelinebody{border-radius:10px;padding:14px 16px}
   .mylisttimelineepisode{gap:13px}
   .mylisttimelineepisode img{width:54px;height:81px;flex-basis:54px}
   .mylisttimelinegame{gap:13px}
   .mylisttimelinegame>img{width:130px;height:60px;flex-basis:130px}
   .mylisttimelinecontent{gap:15px}
   .mylisttimelineart{width:68px;height:68px;flex-basis:68px}
   .mylisttimelineart.driver{height:84px}
   .mylisttimelinedrivers{width:84px;height:84px;flex-basis:84px}
   .mylisttimelinedrivers img{width:40px;height:80px}
   .mylisttimelinekind{flex-basis:58px;width:58px;font-size:11px;padding:9px 0 6px}
 }
 @media(max-width:900px){.mydashchannels{grid-template-columns:repeat(2,minmax(0,1fr))}}
 @media(max-width:900px){.mydash.layout-spotlight,.mydash.layout-hub,.mydash.layout-timeline{display:block}.mydash.layout-hub #myListProfile,.mydash.layout-timeline #myListProfile{display:flex;flex-direction:row;align-items:center;border-right:0;border-bottom:1px solid var(--line);padding:0 0 16px;min-height:0}.mydash.layout-hub .mydashepisodes{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}.mydash.layout-timeline #myListTimelineBlock{margin-top:28px}}
 /* My TV */
 .tvwrap{display:flex;align-items:stretch;border:1px solid var(--line);border-radius:11px;overflow:hidden;height:min(82vh,920px);min-height:520px;background:#0d1015;box-shadow:0 12px 34px rgba(0,0,0,.12)}
 .tvrail{width:136px;flex-shrink:0;border-right:1px solid var(--line);overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:6px;background:#11151b;scrollbar-width:thin}
 .tvsrc{padding:8px 9px;font-size:11.5px;border:1px solid var(--line2);border-radius:7px;cursor:pointer;text-align:center;color:var(--mut);background:none;transition:all .1s;line-height:1.25}
 .tvsrc:hover{border-color:var(--acc);color:var(--fg)}
 .tvsrc.on{border-color:var(--acc);background:#16233d;color:#cfe0ff}
 .tvguide{flex:1;min-width:0;display:flex;flex-direction:column;overflow:hidden;position:relative}
 .tvguidehead{display:flex;flex-shrink:0;border-bottom:1px solid #343a46;background:#14181f;height:44px;padding-right:10px;box-shadow:0 2px 8px rgba(0,0,0,.12);z-index:4}
 .tvchancol{width:286px;flex-shrink:0;border-right:1px solid #343a46;padding:6px 8px;display:flex;align-items:center}
 .tvchancol button{width:100%}
 .tvtimeline{flex:1;display:flex;overflow:hidden;position:relative;min-width:0}
 .tvtimeslot{flex:1 1 20%;min-width:0;border-right:1px solid #343a46;padding:0 10px;display:flex;align-items:center;font-size:10.5px;color:#8f98a8;font-variant-numeric:tabular-nums;letter-spacing:.02em}
 .tvnowhead{position:absolute;top:0;bottom:0;width:2px;background:#e1535c;z-index:3;pointer-events:none;box-shadow:0 0 7px rgba(225,83,92,.38)}
 .tvnowhead:before{content:"";position:absolute;top:4px;left:-3px;width:8px;height:8px;border-radius:50%;background:#e1535c}
 .tvplayerslot{position:absolute;top:0;right:0;left:286px;bottom:0;background:#000;z-index:20;display:none}
 .tvplayerslot.on{display:block}
 .tvplayerslot.mini{position:fixed;top:clamp(110px,18vh,210px);left:auto;right:4px;bottom:auto;width:min(1040px,calc(100vw - 8px));height:min(650px,68vh);z-index:120;border:1px solid #46505e;border-radius:10px;overflow:hidden;box-shadow:0 18px 55px rgba(0,0,0,.6)}
 .tvplayerslot.mini .tvplayerbar{background:#111720}
 .tvplayerslot.sectionmax{position:fixed;top:0;left:0;right:0;bottom:0;width:100vw;height:100vh;z-index:500;border:0;border-radius:0;overflow:hidden;box-shadow:none}
 .tvplayeractions{display:flex;align-items:center;gap:6px}
 .tvminbtn{background:#202733;border:1px solid #3a4554;color:#dce5f2;border-radius:6px;padding:3px 8px;font-size:12px;line-height:1.1;cursor:pointer}
 .tvminbtn:hover{border-color:#6d86a8;filter:none}
 .tvvideohit{position:absolute;left:0;right:0;top:34px;bottom:46px;z-index:2;border:0;border-radius:0;padding:0;background:transparent;cursor:zoom-out}
 .tvvideohit:hover,.tvvideohit:active{background:transparent;filter:none;transform:none}
 .tvplayerslot.mini .tvvideohit{cursor:zoom-in}
 .tvguidebody{flex:1;overflow-y:auto;position:relative;scrollbar-gutter:stable}
 .tvchan{width:286px;flex-shrink:0;border-right:1px solid #303642;display:flex;align-items:center;gap:8px;padding:7px 10px;cursor:pointer;font-size:12px;transition:background .1s;background:#11151b}
 .tvchan:hover{background:var(--card2)}
 .tvchan.playing{background:#16233d}
 .tvchan .tvvlc{flex-shrink:0;background:#e8701a;border:0;color:#fff;border-radius:5px;padding:3px 7px;font-size:11px;cursor:pointer;order:0}
 .tvchan .tvflag{flex-shrink:0;font-size:15px;width:20px;text-align:center}
 .tvchan .tvname{flex:1;min-width:0;line-height:1.2;word-break:break-word;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .tvchan .favstar{margin-right:0}
 .tvdrag{display:inline-flex;align-items:center;color:#737b89;cursor:grab;font-size:16px;line-height:1;user-select:none}
 .tvdrag:active{cursor:grabbing}
 .tvprog{flex:1;position:relative;min-width:0;overflow:hidden;background:repeating-linear-gradient(90deg,transparent 0,transparent calc(20% - 1px),rgba(55,62,75,.44) calc(20% - 1px),rgba(55,62,75,.44) 20%)}
 .tvrow:nth-child(even) .tvprog{background-color:rgba(255,255,255,.008)}
 .tvprog:after{content:"";position:absolute;top:0;bottom:0;left:var(--nowpct,-20%);width:2px;background:rgba(225,83,92,.66);z-index:6;pointer-events:none}
 .epgnone{position:absolute;inset:0;display:flex;align-items:center;padding:0 11px;font-size:10.5px;color:#515966;font-style:italic;opacity:.8}
 .epgprog{position:absolute;top:5px;bottom:5px;display:flex;align-items:center;gap:6px;min-width:3px;padding:4px 8px;border:1px solid #303744;border-radius:6px;background:#171c24;color:#bec5d0;overflow:hidden;white-space:nowrap;z-index:2;transition:border-color .12s,background .12s}
 .epgprog:hover{border-color:#546174;background:#1c232e;color:#e3e7ed;z-index:5}
 .epgprog.live{border-color:#3f7651;background:linear-gradient(90deg,#183022,#19251f);color:#f0f6f2;font-weight:600;box-shadow:inset 3px 0 0 #58b573}
 .epgprog.live:after{content:"NOW";font-size:8px;line-height:1;border:1px solid #3c7e51;border-radius:4px;color:#6ed78a;padding:2px 3px;margin-left:auto;flex:0 0 auto}
 .epgprog .epgt{color:#7aa9ef;font-size:10px;flex:0 0 auto;font-variant-numeric:tabular-nums}
 .epgprog .epgtitle{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
 .epgprog.compact{padding-left:5px;padding-right:5px}.epgprog.compact .epgt{display:none}
 .epgfallback{position:absolute;inset:0;display:flex;align-items:center;gap:7px;padding:0 11px;overflow:hidden;color:#7d8593;white-space:nowrap}.epgfallback .epgtitle{overflow:hidden;text-overflow:ellipsis}
 .epgloadback{position:fixed;inset:0;z-index:130;background:rgba(5,7,10,.68);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;padding:20px}
 .epgloadbox{width:min(440px,calc(100vw - 32px));background:#12171f;border:1px solid #3b4655;border-radius:13px;padding:22px 24px;box-shadow:0 24px 70px rgba(0,0,0,.48)}
 .epgloadtitle{font-size:18px;font-weight:700;margin-bottom:7px}.epgloadstage{color:#aab4c1;font-size:12px;min-height:19px}
 .epgloadbar{height:8px;border-radius:99px;background:#252d38;overflow:hidden;margin:16px 0 9px}.epgloadbar>span{display:block;height:100%;width:0;background:linear-gradient(90deg,#0b55bc,#4b9cff);border-radius:inherit;transition:width .2s ease}
 .epgloadmeta{display:flex;justify-content:space-between;gap:12px;color:#7f8a99;font-size:11px;font-variant-numeric:tabular-nums}
 .tvrow{display:flex;border-bottom:1px solid #292f3a;height:50px;min-height:50px;align-items:stretch;transition:background .12s}.tvrow:hover{background:rgba(255,255,255,.016)}
 .tvrow.tvdragging{opacity:.4}
 .tvrow.tvdragover{box-shadow:inset 0 2px 0 var(--acc)}
 @media(max-width:1100px){.tvrail{width:110px}.tvchancol,.tvchan{width:230px}.tvplayerslot{left:230px}.tvchan{gap:6px;padding-left:7px;padding-right:7px}.tvchan .tvvlc{padding:3px 5px}.tvtimeslot{padding-left:7px}}
 @media(max-width:760px){.tvrail{width:92px;padding:6px}.tvsrc{padding:7px 5px;font-size:10.5px}.tvchancol,.tvchan{width:205px}.tvplayerslot{left:205px}.tvwrap{height:78vh;min-height:440px}.tvchan .tvname{font-size:11px}}
 /* player fills the timeline area when active */
 #tvPlayerSlot .tvplayerbar{display:flex;align-items:center;justify-content:space-between;padding:6px 12px;background:#0c0e12;font-size:13px}
 #tvVideo{width:100%;height:calc(100% - 34px);background:#000;display:block;object-fit:cover;cursor:zoom-out}
 .favcat .chname{flex:1;min-width:0}
 .favcat .chev{color:var(--acc);font-size:12px;flex-shrink:0}
 main{max-width:960px;margin:0 auto;padding:26px 22px 42px;position:relative;z-index:1}
 main.wide{max-width:none;padding:26px 30px 44px;transition:padding-right .18s ease}
 @media(min-width:1800px) and (max-width:2199px){body.tvsectionplay main.wide{padding-right:calc(min(1040px,38vw) + 70px)}}
 @media(min-width:2200px){body.tvsectionplay main.wide{padding-right:calc(min(1040px,40vw) + 70px)}}
 @media(min-width:1800px) and (max-width:2199px){
   .tvplayerslot.mini,.pmodal:not(.sectionmax){width:min(1040px,38vw);height:min(650px,23.75vw,68vh)}
   body.tvsectionplay .mydash.layout-timeline{grid-template-columns:minmax(330px,40%) minmax(0,1fr);gap:0 20px}
   body.tvsectionplay .mydashsportsingletop{grid-template-columns:minmax(100px,1fr) minmax(150px,1fr);gap:10px}
   body.tvsectionplay .movieswrap,body.tvsectionplay .showswrap{grid-template-columns:190px minmax(0,1fr);gap:18px}
   body.tvsectionplay .gameslayout{grid-template-columns:minmax(250px,300px) minmax(0,1fr);gap:18px}
   body.tvsectionplay .teamswrap,body.tvsectionplay .racinglayout{grid-template-columns:minmax(250px,300px) minmax(0,1fr);gap:18px;padding-left:0;padding-right:0}
   body.tvsectionplay .moviegrid{grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
   body.tvsectionplay .showgrid{grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
   body.tvsectionplay .gamegrid{grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
   body.tvsectionplay .racinggrid{grid-template-columns:1fr}
 }
 @media(min-width:2200px){.tvplayerslot.mini,.pmodal:not(.sectionmax){width:min(1040px,40vw);height:min(650px,25vw,68vh)}}
 input[type=checkbox]{accent-color:var(--acc);width:16px;height:16px;cursor:pointer}
 .row{display:flex;gap:8px}
 input,select,button{font:inherit}
 input[type=text],input[type=password],select{background:#101318;border:1px solid var(--line2);color:var(--fg);border-radius:8px;padding:9px 12px;transition:border-color .13s,box-shadow .13s,background .13s}
 input[type=text]:focus,input[type=password]:focus,select:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px rgba(79,140,255,.11);background:#0d1116}
 input[type=text]{flex:1}
 button{background:var(--acc);border:0;color:#fff;border-radius:8px;padding:9px 15px;cursor:pointer;font-weight:500;transition:filter .12s,background .12s,border-color .12s,transform .12s}
 button:hover{filter:brightness(1.08)}
 button:active:not(:disabled){transform:translateY(1px)}
 button:disabled{opacity:.48;cursor:not-allowed;filter:none}
 button:focus-visible,a:focus-visible,.favstar:focus-visible{outline:2px solid #76a7ff;outline-offset:2px}
 button.stopbtn{background:#7a1f26;color:#fff}
 button.ghost{background:var(--card2);border:1px solid var(--line2);color:var(--fg);font-weight:400}
 button.ghost:hover{border-color:var(--acc);filter:none}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
 .muted{color:var(--mut);font-size:13px}
 table{width:100%;border-collapse:collapse;margin-top:6px}
 th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}
 th{color:var(--mut);font-weight:500}
 .tag{display:inline-block;background:#22262f;border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:12px;color:var(--mut);margin-left:4px}
 .cc{display:inline-block;background:#1d2b1f;border:1px solid #2b4a30;color:#8fce9a;border-radius:6px;padding:0 6px;font-size:11px;margin-right:4px}
 .url{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--mut);word-break:break-all;margin-top:3px}
 .copy{background:#3a3f49;border:1px solid #555c68;color:#f4f4f4;padding:4px 10px;border-radius:6px;font-size:12px;cursor:pointer}
 .copy:hover{background:#4a505c}
 .err{color:#ff7676}
 label{display:block;margin:10px 0 4px;color:var(--mut);font-size:13px}
 .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .hide{display:none}
 .bcast{margin:2px 0 10px}
 .live{background:#3b1114;border:1px solid #7a1f26;color:#ff6b74;border-radius:6px;padding:1px 7px;font-size:12px;font-weight:600}
 .ended{background:#22262f;border:1px solid var(--line);color:var(--mut);border-radius:6px;padding:1px 7px;font-size:12px}
 .soon{background:#132a1a;border:1px solid #24512f;color:#7fd79a;border-radius:6px;padding:1px 7px;font-size:12px}
 .split{display:flex;gap:20px;align-items:flex-start}
 .col{flex:1;min-width:0}
 .playlistsearch{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin:0 0 18px;padding:15px 18px;border:1px solid var(--line);border-radius:10px;background:rgba(18,22,28,.72)}
 .playlistsearch .col{min-width:0}.playlistsearch .col+.col{border-left:1px solid var(--line);padding-left:22px}.playlistsearch .row{margin-bottom:7px}
 .colh{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 10px;font-weight:600}
 .srchealth{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
 .srcrow{display:grid;grid-template-columns:9px minmax(130px,.8fr) minmax(0,1.2fr);align-items:center;gap:9px;padding:10px 11px;font-size:13px;border:1px solid rgba(255,255,255,.055);border-radius:8px;background:rgba(255,255,255,.018)}
 .srcdot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
 .dot-ok{background:#3fb950}
 .dot-bad{background:#f85149}
 .dot-unknown{background:#6e7681}
 .srcname{min-width:0;font-weight:500}
 .srcstat{font-size:12px;overflow-wrap:anywhere;line-height:1.35}
 .sectionsearch{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:stretch}.sectionsearch input{min-height:42px}.sectionsearch button{min-width:82px}
 *{scrollbar-color:#4e5868 #171b22;scrollbar-width:thin}
 *::-webkit-scrollbar{width:10px;height:10px}*::-webkit-scrollbar-track{background:#171b22;border-radius:8px}*::-webkit-scrollbar-thumb{background:#4e5868;border:2px solid #171b22;border-radius:8px}*::-webkit-scrollbar-thumb:hover{background:#69778b}
 main>section:not(.hide){animation:viewfade .14s ease-out}@keyframes viewfade{from{opacity:.55;transform:translateY(2px)}to{opacity:1;transform:none}}
 @media(prefers-reduced-motion:reduce){*,*:before,*:after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
 @media(max-width:760px){.split{flex-direction:column}.playlistsearch{grid-template-columns:1fr}.playlistsearch .col+.col{border-left:0;border-top:1px solid var(--line);padding:16px 0 0}}
 @media(max-width:760px){#teamFixtures{width:calc(100vw - 44px)}#teamFixtures>.card{flex-basis:min(280px,calc(100vw - 56px))}}
 .chname{flex:1;min-width:0;font-size:13.5px;word-break:break-word;line-height:1.35}
 .ch4{display:flex;gap:0;align-items:stretch;flex-wrap:nowrap;height:82vh;min-height:480px}
 .ch4group{position:relative;display:flex;align-items:stretch;border:1px solid var(--line);border-radius:12px;padding:0;margin-left:20px;align-self:stretch;overflow:hidden}
 .ch4col{position:relative;z-index:1;width:250px;flex-shrink:0;display:flex;flex-direction:column;padding:14px 16px}
 .ch4col+.ch4col{border-left:1px solid var(--line)}
 .ch4group .colh{border-bottom:1px solid var(--line);padding-bottom:10px;display:flex;align-items:center;gap:8px}
 .clrbtn{margin-left:auto;background:none;border:1px solid var(--line2);color:var(--mut);border-radius:6px;padding:2px 9px;font-size:11px;font-weight:400;cursor:pointer;text-transform:none;letter-spacing:0}
 .clrbtn:hover{border-color:#ff7676;color:#ff7676;filter:none}
 .plbtns{margin-top:12px;padding-top:12px;flex-wrap:wrap}
 .ch4cats{flex-shrink:0;align-self:stretch;display:flex;flex-direction:column;padding:0 20px 0 0;min-height:0}
 input.catsearch[type=text]{width:100%;height:38px;min-height:38px;flex:0 0 38px;margin-bottom:10px;background:var(--bg);border:1px solid var(--line2);color:var(--fg);border-radius:8px;padding:8px 11px;font-size:13px}
 .catsearch:focus{outline:none;border-color:var(--acc)}
 #catlist{border:1px solid var(--line);border-radius:9px;padding:12px;background:var(--bg);flex:1;min-height:0;overflow-y:auto;display:grid;grid-template-columns:repeat(4,190px);grid-auto-flow:column;grid-template-rows:repeat(var(--catrows,20),auto);gap:2px 14px;align-content:start}
 .colh{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin:0 0 12px;font-weight:600;display:flex;align-items:center;gap:6px}
 .colh .muted{text-transform:none;letter-spacing:0;font-weight:400}
 .catitem{display:flex;align-items:center;gap:7px;padding:3px 2px;font-size:11.5px;cursor:pointer;border:0;background:none}
 .catitem:hover .cn{color:var(--fg)}
 .catitem .tick{flex-shrink:0;width:16px;height:16px;border:2px solid var(--line2);border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:11px;color:transparent;transition:all .1s}
 .catitem.on .tick{border-color:var(--acc);background:var(--acc);color:#fff}
 .catitem .flag{flex-shrink:0;font-size:13px;line-height:1;width:18px;text-align:center}
 .catitem .cn{flex:1;min-width:0;color:var(--mut);line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:color .1s}
 .catitem.on .cn{color:#cfe0ff}
 .catitem .cnt{display:none}
 .catitem .pc{color:#5a6070;font-size:10px;font-variant-numeric:tabular-nums}
 .catitem.on .pc{color:#7f93bd}
 .pcol{flex:1;min-height:60px;max-height:70vh;overflow:auto}
 .selcat{display:flex;align-items:center;gap:8px;padding:8px 10px;font-size:13px;cursor:pointer;border-radius:7px;margin-bottom:2px;transition:background .1s}
 .selcat:hover{background:var(--card2)}
 .selcat.active{background:#1b2540;box-shadow:inset 0 0 0 1px #33406b}
 .selcat .cnt{margin-left:auto;color:var(--mut);font-size:11px;font-variant-numeric:tabular-nums}
 .selcat .chev{color:var(--acc);font-size:12px}
 .plitem{display:flex;align-items:center;gap:8px;padding:6px 8px;font-size:13px;border-radius:6px}
 .plitem:hover{background:var(--card2)}
 .plitem .x{color:var(--mut);cursor:pointer;flex-shrink:0;font-size:12px;line-height:1}
 .plitem .x:hover{color:#ff7676}
 .chrow{display:flex;align-items:center;gap:10px;padding:8px;border-radius:7px;transition:background .1s}
 .chrow:hover{background:var(--card2)}
 .chrow+.chrow{border-top:1px solid var(--line)}
 @media(max-width:900px){.ch4{height:auto;min-height:0;flex-wrap:wrap}.ch4cats{height:70vh;flex:1 1 100%}.ch4group{margin:20px 0 0;width:100%}.ch4col{width:100%}#catlist{grid-template-columns:repeat(2,1fr)}}
 .footline{border-top:1px solid var(--line);margin:30px calc(50% - 50vw) 0;width:100vw}
 /* floating pancakes on the search page side margins */
 .pancakes{position:fixed;top:70px;bottom:0;width:calc((100vw - 960px)/2);pointer-events:none;overflow:hidden;z-index:0}
 .pancakes.left{left:0}
 .pancakes.right{right:0}
 @media(max-width:1200px){.pancakes{display:none}}
 .pcake{position:absolute;opacity:.5;animation:floaty linear infinite}
 @keyframes floaty{0%{transform:translateY(0) rotate(0deg)}50%{transform:translateY(-22px) rotate(4deg)}100%{transform:translateY(0) rotate(0deg)}}
 .globaldecor{position:fixed;inset:64px 0 0;pointer-events:none;overflow:hidden;z-index:0;opacity:.17}
 .globaldecor .pcake{opacity:.75}
 .globaldecor.asciibg{opacity:1;font-family:Consolas,"Courier New",monospace;color:#7894bd}
 .asciimotif{position:absolute;margin:0;white-space:pre;font:600 clamp(8px,.58vw,13px)/1.08 Consolas,"Courier New",monospace;letter-spacing:.02em;color:#7894bd;opacity:.075;text-shadow:0 0 18px rgba(57,110,184,.12);user-select:none}
 .asciimotif.a1{left:2.5%;top:9%;transform:rotate(-2deg)}.asciimotif.a2{right:2.2%;top:32%;transform:scale(.82) rotate(2deg);opacity:.055}.asciimotif.a3{left:7%;bottom:7%;transform:scale(.7);opacity:.045}
 @media(max-width:1050px){.asciimotif.a2,.asciimotif.a3{display:none}.asciimotif.a1{opacity:.04}}
 /* settings branding block */
 .brandblock{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;width:220px;flex-shrink:0}
 .brandblock .bname{font-size:22px;font-weight:600;color:var(--fg)}
 .brandblock .btag{font-size:13px;color:var(--mut)}
 .settingswrap{display:grid;grid-template-columns:minmax(0,1fr);gap:14px;align-items:start;max-width:1580px;margin:0 auto;padding:18px 20px 42px}
 .settingswrap .brandblock{width:auto;display:grid;grid-template-columns:58px minmax(0,1fr) auto;grid-template-rows:auto auto;column-gap:14px;row-gap:2px;align-items:center;justify-content:stretch;text-align:left;padding:13px 18px;border:1px solid var(--line2);border-radius:12px;background:linear-gradient(100deg,rgba(24,30,39,.96),rgba(15,18,23,.84))}
 .settingswrap .brandblock svg{width:58px;height:58px;grid-row:1/3}
 .settingswrap .brandblock .bname{font-size:18px;grid-column:2;align-self:end}
 .settingswrap .brandblock .btag{grid-column:2;align-self:start}
 .settingswrap .brandblock .btag:last-child{grid-column:3;grid-row:1/3;align-self:center;margin:0!important;padding:5px 9px;border:1px solid var(--line);border-radius:999px;background:var(--bg)}
 .settingswrap .settingscard{width:100%;max-width:none;min-width:0;background:none;border:0;padding:0;margin:0}
 .settingstabs{display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin-bottom:16px;padding:0 0 9px;border-bottom:1px solid var(--line)}
 .settingstab{border:0;border-radius:6px 6px 0 0;background:transparent;color:var(--mut);padding:9px 13px;box-shadow:none}
 .settingstab:hover{background:var(--card2);color:var(--fg)}
 .settingstab.on{color:var(--fg);background:var(--card);box-shadow:inset 0 -2px var(--acc)}
 .settingspanels{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}
 .settingspanel{display:contents}
 .settingsgroup[data-settings-panel="profile"],.settingsgroup[data-settings-panel="general"],.settingsgroup[data-settings-panel="maintenance"],.settingsgroup[data-settings-panel="health"]{grid-column:1/-1}
 .settingspanel input[type=text],.settingspanel input[type=password]{width:100%}
 #settingsProfile .grid2{grid-template-columns:1fr 1fr}
 #settingsProfile select{width:100%}
 .settingsgroup{border:1px solid var(--line2);border-radius:12px;background:linear-gradient(180deg,var(--card),#11151b);padding:18px;box-shadow:0 8px 24px rgba(0,0,0,.08)}
 .settingsgroup .colh{margin:0 -2px 13px;padding-bottom:9px;border-bottom:1px solid rgba(255,255,255,.07);color:#aeb9c9}.settingsgroup .colh+.muted{margin-top:-5px;margin-bottom:14px;line-height:1.45}
 .settingschecks{display:grid;gap:9px}.settingscheck{display:flex;align-items:flex-start;gap:9px;padding:8px 9px;border-radius:8px;background:rgba(255,255,255,.018)}.settingscheck input{width:auto;margin:2px 0 0}.settingscheck span{line-height:1.35}
 .settingsdisplay{display:grid;grid-template-columns:1fr;gap:12px;margin-top:13px}
 .settingsactions{position:sticky;bottom:10px;z-index:4;display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:14px;padding:11px 13px;border:1px solid var(--line2);border-radius:11px;background:rgba(17,21,27,.96);box-shadow:0 12px 30px rgba(0,0,0,.3);backdrop-filter:blur(10px)}.settingsactions .push{margin-left:auto}
 .emblempicker{display:flex;gap:7px;flex-wrap:wrap;margin-top:5px}
 .emblemchoice{width:48px;height:48px;padding:5px;border:1px solid var(--line2);border-radius:9px;background:var(--bg);opacity:.65}
 .emblemchoice:hover{opacity:1;border-color:var(--acc)}
 .emblemchoice.on{opacity:1;border:2px solid var(--acc);background:#16233d}
 .emblemchoice svg{width:100%;height:100%;display:block}
 #settingsSetup .row{flex-wrap:wrap}
 .settingshealthgroup{grid-column:1/-1}.settingshealthgroup>.row{justify-content:flex-end}
 .settingsgroup[hidden]{display:none!important}
 @media(min-width:1800px) and (max-width:2199px){body.tvsectionplay #settingsView .settingswrap{padding-left:8px;padding-right:8px}body.tvsectionplay #settingsView .settingspanels{grid-template-columns:1fr}body.tvsectionplay #settingsView .settingsgroup .grid2{grid-template-columns:1fr 1fr}body.tvsectionplay #settingsView .srcrow{grid-template-columns:9px minmax(110px,.8fr) minmax(0,1.2fr)}}
 @media(max-width:1150px){.settingswrap{max-width:900px}.settingspanels{grid-template-columns:1fr}.srchealth{grid-template-columns:1fr}}
 @media(max-width:650px){#settingsProfile .grid2{grid-template-columns:1fr}.settingswrap{padding:8px}.settingsgroup{padding:13px}.settingswrap .brandblock{grid-template-columns:48px 1fr}.settingswrap .brandblock svg{width:48px;height:48px}.settingswrap .brandblock .btag:last-child{display:none}.settingsactions .muted{display:none}.settingstabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.settingstab{text-align:left}}
 /* playlist builder logo */
 .pancakes-pl{position:absolute;inset:0;pointer-events:none;overflow:hidden;z-index:0}
 .churl{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:var(--mut);word-break:break-all}
 .movieswrap{display:grid;grid-template-columns:230px minmax(0,1fr);gap:24px;width:100%}
 .moviefavs{padding:0 16px 0 0;max-height:calc(100vh - 96px);overflow-y:auto;position:sticky;top:78px;border-right:1px solid var(--line)}
 .moviefavlist{display:flex;flex-direction:column;gap:8px}
 .moviefav{position:relative;display:flex;gap:9px;align-items:flex-start;padding:8px 0;border-bottom:1px solid var(--line);min-height:100px;cursor:pointer}
 .moviefav:hover .moviefavname{color:var(--acc)}
 .moviefavposter{position:relative;width:64px;height:96px;flex-shrink:0;border-radius:5px;background:#20242c;display:flex;align-items:center;justify-content:center;overflow:hidden;color:#737b89;font-size:24px}
 .moviefavposter img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 .moviefavinfo{min-width:0;flex:1;display:flex;justify-content:center;padding:8px 6px 31px 0}
 .moviefavname{width:100%;font-size:14px;font-weight:600;line-height:1.35;text-align:center;word-break:break-word}
 .movieremove{position:absolute;right:3px;bottom:10px;margin:0;font-size:20px}
 .moviesmain{width:100%;max-width:1500px;min-width:0;margin:0 auto}
 .moviecatalogs{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:24px;margin-top:20px;align-items:start}
 .moviecatalogs.noxtream{grid-template-columns:minmax(0,1200px);justify-content:center}
 .moviecatalogs.noxtream #recentMoviesSection{display:none}
 .moviecatalogs.noxtream .moviecatalogcolumn+.moviecatalogcolumn{padding-left:0;border-left:0}
 .moviecatalogs.noxtream .moviegrid{grid-template-columns:repeat(3,minmax(0,1fr))}
 .moviecatalogcolumn{min-width:0}
 .moviecatalogcolumn+.moviecatalogcolumn{padding-left:24px;border-left:1px solid var(--line)}
 .moviecataloghead{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:42px;margin-bottom:12px}
 .moviecataloghead .colh{margin:0}
 .moviecatalogtabs{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
 .moviecatalogtab{background:transparent;color:var(--mut);border-color:var(--line2);box-shadow:none}
 .moviecatalogtab.on{background:var(--card2);color:var(--fg);border-color:var(--acc)}
 .moviecatalogcolumn .moviegrid{grid-template-columns:1fr}
 .moviegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:16px}
 .moviecard{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-height:150px;transition:border-color .13s,background .13s,transform .13s}
 .recentmovie{cursor:pointer}
 .recentmovie:hover{border-color:#496b9f;background:var(--card2);transform:translateY(-1px)}
 .latestshowcard{cursor:pointer}
 .latestshowcard:hover{border-color:var(--acc)}
 .movieposter{width:92px;height:138px;flex-shrink:0;border-radius:7px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center;color:#737b89;font-size:30px}
 .movieposter img{width:100%;height:100%;object-fit:cover;display:block}
 .movieinfo{display:flex;flex:1;min-width:0;flex-direction:column;gap:9px}
 .movietitle{font-weight:600;line-height:1.3}
 .gameswrap{display:grid;grid-template-columns:230px minmax(0,1fr);gap:24px;width:100%}
 .gamefavs{max-height:82vh;overflow-y:auto}
 .gamefav{position:relative;padding:8px 0 30px;border-bottom:1px solid var(--line)}
 .gamefav img{width:100%;max-width:190px;aspect-ratio:460/215;object-fit:cover;border-radius:6px;display:block;margin-bottom:7px}
 .gamefavname{font-size:13px;font-weight:600;line-height:1.3}
 .gameslayout{display:grid;grid-template-columns:380px minmax(0,1fr);gap:24px;width:100%;max-width:1600px;margin:0 auto;align-items:start}
 .gamesmain{width:100%;max-width:none;min-width:0;margin:0}
 .steamprofile{position:sticky;top:76px;background:linear-gradient(145deg,#1b2838 0%,#172331 52%,#101822 100%);border:0;border-top:2px solid #66c0f4;border-radius:3px;padding:22px;min-height:320px;box-shadow:0 14px 38px rgba(0,0,0,.25)}
 .steamprofileempty{color:var(--mut);font-size:12px;line-height:1.5}
 .steamprofilehead{display:flex;align-items:center;gap:16px}
 .steamprofileavatar{width:132px;height:132px;object-fit:cover;border-radius:2px;flex:0 0 132px;background:#202936;border:2px solid #57a5d3;box-shadow:0 0 0 3px #0d151e}
 .steamprofilename{font-size:24px;font-weight:500;line-height:1.15;color:#fff}
 .steamprofilereal{font-size:14px;color:#bac4cf;margin-top:6px}
 .steamprofileloc{font-size:12px;color:#8f98a0;margin-top:5px;line-height:1.4}
 .steamprofilemeta{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:18px}
 .steamlevel{display:inline-flex;align-items:center;justify-content:center;min-width:40px;height:40px;padding:0 9px;border:2px solid #7f65bb;border-radius:50%;font-size:13px;font-weight:700;color:#ddd0ff}
 .steamyears{font-size:12px;color:#c7d5e0;border-left:1px solid #34536b;padding-left:11px}
 .steamprofilesummary{margin-top:17px;padding-top:14px;border-top:1px solid #314452;font-size:12.5px;line-height:1.6;color:#c7d5e0;white-space:pre-line;max-height:220px;overflow:auto;scrollbar-width:thin}
 .steamprofilelink{display:block;color:inherit;text-decoration:none}.steamprofilelink:hover .steamprofilename{color:#66c0f4}
 .gamegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:10px;margin-top:14px}
 .gamecard{background:linear-gradient(135deg,#1b2838,#16202c);border:1px solid #2a3f51;border-radius:3px;padding:8px;min-width:0;box-shadow:0 4px 14px rgba(0,0,0,.14)}
 .wishlistgame{display:block;color:inherit;text-decoration:none;cursor:pointer;transition:border-color .12s,background .12s}
 .wishlistgame:hover{border-color:#66c0f4;background:linear-gradient(135deg,#22384b,#1b2d3d);box-shadow:0 0 0 1px rgba(102,192,244,.12)}
 .wishlisthelp{margin-top:18px;padding:16px 18px;border:1px solid var(--line);border-radius:9px;background:var(--card);max-width:720px;color:var(--mut)}
 .wishlisthelp b{color:var(--fg)}
 .wishlisthelp ul{margin:10px 0 0;padding-left:20px}
 .wishlisthelp li{margin:5px 0}
 .wishlisthelp a{color:var(--acc);text-decoration:none}
 .wishlisthelpsection{margin-top:16px;padding-top:14px;border-top:1px solid var(--line)}
 .wishlistexample{display:block;margin-top:10px;padding:8px 10px;border-radius:6px;background:#0f1115;color:#a9c8ff;font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}
 .gamecard img{width:100%;aspect-ratio:460/215;object-fit:cover;border-radius:2px;background:#20242c;display:block}
 .gamecardbody{display:flex;align-items:center;gap:10px;margin-top:9px;min-height:38px}
 .gamecardname{font-weight:600;line-height:1.3;flex:1;min-width:0}
 .gameshead{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:10px;padding:11px 13px;background:linear-gradient(90deg,#1b2838,#131d27);border-bottom:1px solid #31516a}.gameshead .colh{margin:0;color:#c7d5e0}.gamesheadactions{display:flex;gap:7px;align-items:center}.gamesheadactions .ghost{border-color:#34556d;background:#182838;color:#c7d5e0}.gamesheadactions .ghost:hover{border-color:#66c0f4;color:#fff}
 .gameswishlistsettings{margin:10px 0 14px;padding:12px;border:1px solid #2c4559;border-radius:3px;background:#172431}
 .gamecardrelease{margin-left:auto;text-align:right;flex:0 0 auto}.gamecountdown{display:inline-block;margin-top:4px;color:#70b3ff;background:#102038;border:1px solid #274e7d;border-radius:5px;padding:2px 7px;font-size:11px;font-weight:700;white-space:nowrap}
 @media(max-width:900px){.gameslayout{grid-template-columns:270px minmax(0,1fr);gap:22px}.steamprofile{padding:17px}.steamprofileavatar{width:82px;height:82px;flex-basis:82px}.steamprofilename{font-size:19px}}
 @media(max-width:820px){.gameslayout{grid-template-columns:1fr}.steamprofile{position:static;max-width:none;min-height:0}.steamprofileinner{display:grid;grid-template-columns:auto minmax(0,1fr);column-gap:16px}.steamprofilesummary{grid-column:1/-1}.steamprofilemeta{align-self:end}}
 .moviemeta{font-size:12px;color:var(--mut)}
 .movieactions{display:flex;gap:7px;margin-top:auto;flex-wrap:wrap}
 .movieresultback{text-align:center;margin-top:14px;margin-bottom:24px}
 .movieresultstatus{margin-bottom:4px}
 .latestsourceexpand{min-width:108px;padding:9px 15px;font-size:inherit;margin-right:0}
 .latestsources{display:flex;gap:7px;flex-wrap:wrap;width:100%}
 .showswrap{display:grid;grid-template-columns:230px minmax(0,1fr);gap:24px;width:100%}
 .showrefresh{position:fixed;top:68px;right:18px;z-index:30;font-size:12px;padding:7px 13px;box-shadow:0 5px 18px rgba(0,0,0,.28);border:1px solid rgba(255,255,255,.08)}
 .showfavs{padding-right:16px;max-height:calc(100vh - 96px);overflow-y:auto;position:sticky;top:78px;border-right:1px solid var(--line)}
 .showfavlist{display:flex;flex-direction:column;gap:8px}
 .showfav{position:relative;display:flex;gap:9px;align-items:flex-start;padding:8px 0;border-bottom:1px solid var(--line);cursor:pointer;min-height:100px}
 .showfav:hover .showfavname{color:var(--acc)}
 .showfavposter{position:relative;width:64px;height:96px;flex-shrink:0;border-radius:5px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center}
 .showfavposter img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 .showfavinfo{min-width:0;flex:1;display:flex;justify-content:center;padding:8px 6px 27px 0}
 .showfavname{width:100%;font-size:14px;font-weight:600;line-height:1.35;text-align:center}
 .showremove{position:absolute;right:0;bottom:8px;padding:3px 7px}
 .showsmain{width:100%;max-width:1250px;min-width:0;margin:0 auto}
 .showgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;margin-top:16px}
 .showcard{display:flex;gap:10px;min-height:140px;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--card);cursor:pointer;transition:border-color .13s,background .13s,transform .13s}
 .showcard:hover{border-color:#496b9f;background:var(--card2);transform:translateY(-1px)}
 .showposter{position:relative;width:82px;height:123px;flex-shrink:0;border-radius:6px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center;font-size:28px;color:#737b89}
 .showposter img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 .showname{font-weight:600;line-height:1.3}
 .showdetails{margin-top:16px}
 .showhero{display:flex;align-items:flex-end;gap:18px;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--line)}
 .showheroart{position:relative;width:150px;height:225px;flex-shrink:0;border-radius:9px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center;font-size:38px}
 .showheroart img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 .showhero h2{font-size:28px;margin:0 0 8px;display:flex;align-items:center;gap:10px}
 .showhero h2 .favstar{font-size:22px;margin-right:0}
 .showbackbtn{margin-left:auto;align-self:center;white-space:nowrap}
 .showresultback{text-align:center;margin-top:14px;margin-bottom:24px}
 .seasonblock{margin-bottom:16px;border-top:1px solid var(--line);padding-top:10px}
 .seasonlayout{display:flex;gap:14px;align-items:flex-start}
 .seasonart{position:relative;width:105px;height:158px;flex-shrink:0;border-radius:7px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center;font-size:30px;color:#737b89}
 .seasonart img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 .seasoncontent{min-width:0;flex:1}
 .seasonhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
 .episodes{display:flex;gap:8px;overflow-x:auto;padding:0 0 12px;scrollbar-color:#697487 #20242c;scrollbar-width:auto}
 .episodes::-webkit-scrollbar{height:12px}
 .episodes::-webkit-scrollbar-track{background:#20242c;border-radius:8px}
 .episodes::-webkit-scrollbar-thumb{background:#697487;border:2px solid #20242c;border-radius:8px}
 .episodes::-webkit-scrollbar-thumb:hover{background:var(--acc)}
 .episode{flex:0 0 calc((100% - 72px)/10);min-width:100px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:7px}
 .episodename{font-size:12px;line-height:1.3;min-height:31px}
.episode .btnvlc{margin-top:auto}
 .episodequalities{display:flex;flex-wrap:wrap;gap:5px;margin-top:auto}
 .episodequalities .btnvlc{flex:1 1 auto;padding-left:7px;padding-right:7px;font-size:11px}
 .teamswrap{display:grid;grid-template-columns:minmax(320px,380px) minmax(0,1250px);gap:32px;width:100%;padding:0 18px;align-items:start}
 .teamfavs{padding-right:18px;max-height:calc(100vh - 96px);overflow-y:auto;position:sticky;top:78px;border-right:1px solid var(--line)}
 .teamfavlist{display:flex;flex-direction:column;gap:4px}
 .teamfavitem{position:relative;padding:8px 34px 8px 8px;border-bottom:1px solid var(--line);font-size:14px;font-weight:600;display:flex;align-items:center;gap:10px;min-height:48px}
 .teamfavitem[data-team-search]{cursor:pointer;border-radius:7px;transition:background .12s,border-color .12s}.teamfavitem[data-team-search]:hover{background:var(--card2);border-bottom-color:var(--line2)}
 .teamfavitem.selected{background:rgba(31,73,124,.17);border-bottom-color:#33577c}
 .teamfavlogo{width:34px;height:34px;flex:0 0 34px;object-fit:contain}
 .teamfavname{min-width:0;line-height:1.25}
 .teamfavitem .teamremove{position:absolute;right:8px;top:50%;transform:translateY(-50%);margin:0}
 .teamsmain{width:100%;max-width:1250px;min-width:0;margin:0}
 .teamprofiledetail{min-height:230px;margin-bottom:17px}
 .teamprofilehero{display:flex;align-items:center;gap:15px;margin:0 0 16px}
 .teamprofilebadge{width:104px;height:104px;flex:0 0 104px;border-radius:18px;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 50% 42%,rgba(72,96,132,.34),rgba(20,24,31,.76) 70%);border:1px solid var(--line2);box-shadow:inset 0 0 25px rgba(255,255,255,.025)}
 .teamprofilebadge img{width:82px;height:82px;object-fit:contain;filter:drop-shadow(0 5px 8px rgba(0,0,0,.32))}
 .teamprofileidentity{min-width:0}.teamprofileidentity h2{margin:0 0 5px;font-size:22px;line-height:1.12}.teamprofileidentity .muted{font-size:12px}
 .teamprofilefacts{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:0 0 14px}
 .teamprofilefact{min-width:0;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:rgba(24,27,34,.55)}
 .teamprofilefact span{display:block;color:var(--mut);font-size:9px;letter-spacing:.65px;text-transform:uppercase;margin-bottom:4px}.teamprofilefact b{display:block;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .teamprofilenext{border-top:1px solid var(--line);padding-top:12px}.teamprofilenextlabel{color:var(--mut);font-size:9px;letter-spacing:.7px;text-transform:uppercase;margin-bottom:6px}.teamprofilenext b{display:block;font-size:13px;margin-bottom:3px}.teamprofilenext .muted{font-size:11px}
 .teamfavdivider{height:1px;background:var(--line);margin:0 0 17px}
 .teammatchfinder{background:linear-gradient(180deg,rgba(24,28,36,.76),rgba(18,21,27,.58));border:1px solid var(--line);border-radius:12px;padding:17px 18px;margin:0 0 22px}
 .matchfinderhead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:12px}.matchfindertitle{font-size:15px;font-weight:650;color:var(--fg)}.matchfindersub{font-size:11px;color:var(--mut);margin-top:2px}
 .matchfindercontrols{display:flex;align-items:center;justify-content:space-between;gap:15px;margin-top:8px}.matchfindercontrols .matchstrict{margin:0}.matchfinderhint{font-size:10px;color:var(--mut)}
 .sportssearchrow{grid-template-columns:minmax(0,7fr) auto minmax(150px,3fr)}.sportschannelsearch{width:100%;white-space:nowrap}
.matchresultslabel{font-size:9px;letter-spacing:.75px;text-transform:uppercase;color:var(--mut);margin:13px 0 7px}
 .sportssearchback{text-align:center;margin:14px 0 2px}.sportssearchback button{padding:7px 16px}
 .teammatchresults:empty{display:none}
 .teammatchresults{margin-top:14px}
 .teammatchresults #teamFixtures>.card{background:var(--card2)}
 .teammatchresults #teamFixtures>.card.selectedfixture{border-color:#3d7950;box-shadow:0 0 0 1px rgba(67,140,87,.2)}
 .teamsearchresults{margin-top:10px}.teamsearchresults:empty{display:none}.teamsearchchips{display:flex;gap:8px;flex-wrap:wrap}
 .teamsearchhit{display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;transition:border-color .13s,background .13s}.teamsearchhit:hover{border-color:var(--line2);background:var(--card2)}
 .teamsearchhit[data-team-select]{cursor:pointer}.teamsearchlogo{width:25px;height:25px;object-fit:contain;flex:0 0 25px}
 .teamsearchhit .teamfindfixtures{margin-left:auto;padding:5px 9px;font-size:11px;white-space:nowrap}
 .teamfixturegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
 .topfixturegrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
 .topfixturemore{text-align:center;margin:14px 0 4px}
 @media(max-width:1150px){.topfixturegrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
 @media(max-width:720px){.topfixturegrid{grid-template-columns:1fr}}
 .teamupcominggroup{margin-bottom:24px}
 .teamupcomingname{font-size:14px;font-weight:600;margin:0 0 9px;color:var(--fg)}
 .teamfixture{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:12px;transition:border-color .13s,background .13s,transform .13s}
 .teamfixture.hastv{cursor:pointer}
 .teamfixture.hastv:hover{border-color:#397348;background:#172019;transform:translateY(-1px)}
 .teamfixture.livefixture{border-color:#7a1f26;background:#171012}
 .teamfixtureteams{font-size:15px;font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
 .teamfixtureside{display:inline-flex;align-items:center;gap:6px;min-width:0}
 .teamfixturelogo{width:24px;height:24px;flex:0 0 24px;object-fit:contain}
 .teamfixturevs{color:var(--mut);font-weight:400}
 .teamfixtureowner{font-size:11px;color:var(--acc);margin-top:7px}
 .teamfixturecompetition{font-size:11px;color:var(--mut);margin-bottom:4px}
 .teamfixturetv{margin-left:auto}
 .teamfixturebroadcasts{margin-top:10px;padding-top:9px;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:6px}
 .teamfixture.selectedfixture{border-color:#3d7950;box-shadow:0 0 0 1px rgba(67,140,87,.2)}
 .teamfixturebroadcasts.hide{display:none}
 .matchstrict{display:flex;align-items:center;gap:9px;margin-top:9px;color:var(--mut);font-size:11px}
 .matchstrict input[type=range]{width:170px;accent-color:var(--acc);cursor:pointer}
 .matchstrictvalue{min-width:30px;color:var(--fg);font-variant-numeric:tabular-nums}
 #recentMovieList>.muted,#latestEpisodeList>.muted,#upcomingEpisodeList>.muted,#teamUpcomingList>.muted,#gameWishlist>.muted{display:block;padding:22px 14px;border:1px dashed var(--line2);border-radius:10px;text-align:center;background:rgba(24,27,34,.45)}
 @media(max-width:1100px){.moviecatalogs{grid-template-columns:1fr}.moviecatalogcolumn+.moviecatalogcolumn{padding:20px 0 0;border-left:0;border-top:1px solid var(--line)}}
 @media(max-width:850px){.moviecatalogs.noxtream .moviegrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
 @media(max-width:600px){.moviecatalogs.noxtream .moviegrid{grid-template-columns:1fr}}
 @media(max-width:860px){.movieswrap,.showswrap,.teamswrap{grid-template-columns:1fr;gap:20px}.moviefavs,.showfavs,.teamfavs{position:static;max-height:260px;padding:0 0 15px;border-right:0;border-bottom:1px solid var(--line)}.showrefresh{position:static;float:right;margin:-3px 0 12px 10px}.moviesmain,.showsmain,.teamsmain{clear:both}.sectionsearch{grid-template-columns:minmax(0,1fr) auto}.matchfindercontrols{align-items:flex-start;flex-direction:column}main.wide{padding-left:18px;padding-right:18px}}
 @media(max-width:560px){main,main.wide{padding:18px 12px 34px}.sectionsearch,.sportssearchrow{grid-template-columns:1fr}.sectionsearch button{width:100%}.moviegrid,.showgrid,.teamfixturegrid{grid-template-columns:1fr}.showhero{align-items:flex-start}.showheroart{width:110px;height:165px}.showhero h2{font-size:21px}}
 .racinglayout{display:grid;grid-template-columns:minmax(320px,480px) minmax(0,1250px);gap:32px;width:100%;padding:0 18px;align-items:start}
 .racingwrap{width:100%;min-width:0;margin:0}
 .racingsidebar{min-width:0}
 .racingteamcontrol{width:100%;display:flex;flex-direction:column;align-items:flex-start;margin:0 0 16px}
 .racingteamcontrol.hide{display:none}
 .racingteamselect{display:flex;align-items:center;gap:10px;min-width:190px;justify-content:flex-start;text-align:left}
 .racingteamselect img{width:34px;height:34px;object-fit:contain;flex:0 0 34px}
 .racingteamselect.readonly{cursor:default;pointer-events:none;background:var(--card);color:var(--fg)}
 .racingteampicker{display:grid;grid-template-columns:1fr;gap:7px;width:100%;margin-top:8px;padding:8px}
 .racingteampicker.hide{display:none}
 .racingteampicker .f1choice{display:flex;align-items:center;gap:8px;justify-content:flex-start;text-align:left;min-width:0}
 .racingteampicker .f1choice img{width:28px;height:28px;object-fit:contain;flex:0 0 28px}
 .racingseries{display:flex;flex-wrap:wrap;gap:9px;margin:14px 0 22px}
 .racingtoggle{background:var(--card);border:1px solid var(--line2);color:var(--mut);padding:8px 14px;border-radius:8px;cursor:pointer}
 .racingtoggle.on{border-color:#d83a3a;background:#2a1719;color:#f39a9a}
 .racinggrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
 .racingdrivers{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:0 0 24px}
 .racingdriver{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;display:grid;grid-template-columns:72px minmax(0,1fr);gap:12px;min-height:118px;cursor:pointer;transition:border-color .12s,background .12s,transform .12s}
 .racingdriver:hover{border-color:#8a4549;background:#171416;transform:translateY(-1px)}
 .racingdriver.selected{border-color:#a84a50;background:#1a1416}
 .racingdriver img{width:72px;height:94px;object-fit:cover;object-position:top center;border-radius:7px;background:var(--card2)}
 .racingdriver.racingdriverpair{grid-template-columns:154px minmax(0,1fr)}
 .racingdriverpairpics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}
 .racingdriverpairperson{min-width:0;text-align:center}
 .racingdriverpairperson img{display:block;width:72px;height:82px;object-fit:cover;object-position:top center;margin:0 auto 4px}
 .racingdriverpairperson span{display:block;font-size:10px;line-height:1.15;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .racingdriverinfo{display:flex;flex-direction:column;min-width:0}
 .racingdrivername{font-size:16px;font-weight:650;line-height:1.25}
 .racingdriverteam{font-size:11px;color:var(--mut);margin-top:3px}
 .racingdrivernext{border-top:1px solid var(--line);margin-top:auto;padding-top:8px;font-size:11px;color:var(--mut);line-height:1.35}
 .racingdrivernext b{display:block;color:var(--fg);font-size:12px;margin-top:2px}
 .racingdetail{border-top:1px solid var(--line);padding-top:16px;min-height:390px}
 .racingdetailhero{display:grid;grid-template-columns:132px minmax(0,1fr);gap:16px;align-items:center}
 .racingdetailhero>img{width:132px;height:168px;object-fit:cover;object-position:top center;border-radius:10px;background:var(--card);border:1px solid var(--line)}
 .racingdetailhero>img.car{object-fit:contain;object-position:center;padding:5px}
 .racingdetailseries{font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#ef7777;margin-bottom:5px}
 .racingdetail h2{font-size:22px;line-height:1.15;margin:0 0 5px}
 .racingdetailteam{color:var(--mut);font-size:12px;line-height:1.4}
 .racingdetailnext{margin-top:18px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
 .racingdetailnextlabel{font-size:10px;color:var(--mut);letter-spacing:.7px;text-transform:uppercase;margin-bottom:6px}
 .racingdetailnextgrid{display:grid;grid-template-columns:minmax(0,1fr) 180px;gap:14px;align-items:start}
 .racingeventvisual{display:flex;flex-direction:column;align-items:center;gap:8px;min-width:0}
 .racingeventvisual img{display:block;width:180px;height:102px;object-fit:contain;border-radius:8px;background:#0b0d10;padding:5px;box-sizing:border-box}
 .racingeventfallback{width:180px;height:102px;display:flex;align-items:center;justify-content:center;border-radius:8px;background:#0b0d10}
 .racingeventfallback .racingserieslogo{width:150px;height:74px}
 .racingdetailcountdown{display:inline-block;color:#f08b8b;background:#271416;border:1px solid #6f292e;border-radius:6px;padding:3px 8px;font-size:11px;font-weight:700;margin:0}
 .racingdetailnext b{display:block;font-size:16px;margin-bottom:5px}
 .racingdetailmeta{font-size:12px;color:var(--mut);line-height:1.5}
 .racingdetailactions{display:flex;gap:8px;margin-top:14px}
 .racingdetailactions a{text-decoration:none}
 .racingdetailf1hero{display:flex;align-items:center;gap:14px;margin-bottom:15px}
 .racingdetailf1hero>img{width:100px;height:68px;object-fit:contain;background:var(--card);border-radius:9px;padding:7px}
 .racingdetailpeople{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
 .racingdetailperson{display:flex;align-items:center;gap:9px;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:8px;min-width:0}
 .racingdetailperson img{width:48px;height:62px;object-fit:cover;object-position:top center;border-radius:6px;flex:0 0 48px}
 .racingdetailperson b{font-size:12px;line-height:1.25}
 @media(max-width:440px){.racingdetailnextgrid{grid-template-columns:1fr}.racingeventvisual{align-items:flex-start}}
 .driverlive{display:inline-block;background:#102c19;border:1px solid #2f7e48;color:#70d18a;border-radius:6px;padding:1px 6px;font-size:10px;font-weight:700;margin-left:7px;vertical-align:1px}
 .mydashdriverlive{margin-left:auto}
 .racingcard{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
 .racingcard h3{margin:0 0 10px;font-size:16px;display:flex;align-items:center;gap:10px}
 .racingcard h3>span{position:relative;padding-bottom:6px}
 .racingcard.selected h3>span:after{content:"";position:absolute;left:0;right:0;bottom:0;width:100%;height:2px;border-radius:2px;background:var(--series-accent,#747b86);opacity:.92}
 .racingcard.series-f1{--series-accent:#050505}.racingcard.series-f1.selected h3>span:after{box-shadow:0 1px 0 rgba(255,255,255,.16)}
 .racingcard.series-f2{--series-accent:#20aee5}.racingcard.series-f3{--series-accent:#e86c32}.racingcard.series-indycar{--series-accent:#d8212a}
 .racingcard.series-wec{--series-accent:#7da65a}.racingcard.series-formulae{--series-accent:#19a7b8}.racingcard.series-motogp{--series-accent:#b7bcc4}.racingcard.series-wrc{--series-accent:#f06a22}
 .racingserieslogo{display:block;width:58px;height:28px;object-fit:contain;object-position:left center;flex:0 0 auto;border:0;background:transparent}
 .racingevent{padding:9px 0;border-top:1px solid var(--line);cursor:pointer}
 .racingevent:hover b{color:var(--acc)}
 .racingevent:first-of-type{border-top:0}
 .racingeventtop{display:flex;align-items:center;gap:8px}.racingeventtv{margin-left:auto;background:#17351e;border-color:#327443;color:#70d889}.racingeventchannels{margin-top:9px;padding:8px;border:1px solid #294535;border-radius:7px;background:#101814}.racingeventchannels.hide{display:none}.racingeventchannel{display:flex;align-items:center;gap:8px;padding:6px 4px;border-top:1px solid rgba(255,255,255,.055)}.racingeventchannel:first-child{border-top:0}.racingeventchannel .chn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.racingeventchannel .chbtns{flex:0 0 auto}
 @media(max-width:1600px){.racinglayout{grid-template-columns:320px minmax(0,1fr);gap:24px}}
 @media(max-width:1000px){.racinglayout{grid-template-columns:1fr}.racingsidebar{max-width:520px}.racinggrid{grid-template-columns:1fr}.racingdetail{min-height:0}}
 .setupoverlay{position:fixed;inset:0;z-index:3000;background:rgba(5,7,10,.84);backdrop-filter:blur(10px);display:flex;align-items:center;justify-content:center;padding:24px}
 .setupoverlay.hide{display:none}
 .setupwizard{width:min(860px,100%);max-height:calc(100vh - 48px);overflow:auto;background:linear-gradient(160deg,#11161d 0%,#0d1116 100%);border:1px solid #4a515d;border-radius:18px;box-shadow:0 28px 100px rgba(0,0,0,.64);padding:0}
 .editprofiledialog{width:min(620px,100%);max-height:calc(100vh - 48px);overflow:auto;background:#101318;border:1px solid var(--line2);border-radius:14px;box-shadow:0 24px 80px rgba(0,0,0,.55);padding:22px 24px}.editprofiledialog h2{margin:0 0 5px}.editprofileactions{display:flex;align-items:center;gap:8px;margin-top:20px;padding-top:15px;border-top:1px solid var(--line)}.editprofileactions .spacer{flex:1}
 .setupbrand{display:flex;align-items:center;gap:11px;padding:20px 28px 13px}.setupbrand svg{width:42px;height:42px}.setupbrand b{font-size:18px}.setupstepmeta{margin-left:auto;color:#7f8a99;font-size:11px;letter-spacing:.75px;text-transform:uppercase}
 .setupsteps{display:flex;gap:7px;margin:0 28px 27px}.setupdot{height:4px;flex:1;border-radius:5px;background:#242b35;transition:background .18s}.setupdot.on{background:linear-gradient(90deg,#0b55c8,#2b80ff)}
 .setupstep{padding:0 28px 4px;min-height:330px}.setupstep.hide{display:none}.setupstep h2{font-size:25px;margin:0 0 8px;letter-spacing:-.2px}.setupintro{color:var(--mut);margin-bottom:22px;line-height:1.55;max-width:700px}
 .setupfields{display:grid;grid-template-columns:1fr 1fr;gap:14px}.setupfields .full{grid-column:1/-1}.setupfields input,.setupfields select{width:100%}
 .setupemblems{display:flex;gap:8px;flex-wrap:wrap}.setupemblems .emblemchoice{width:58px;height:58px}
 .setupfeatures{display:grid;grid-template-columns:1fr 1fr;gap:11px}.setupfeature{display:flex;align-items:center;gap:11px;border:1px solid var(--line);background:rgba(20,25,32,.9);border-radius:11px;padding:15px;cursor:pointer;transition:border-color .15s,background .15s,transform .15s}.setupfeature:hover{border-color:#566276;transform:translateY(-1px)}.setupfeature:has(input:checked){border-color:#2869c9;background:#12223a}.setupfeature input{width:auto;margin:0}.setupfeature b{display:block}.setupfeature small{display:block;color:var(--mut);margin-top:3px;line-height:1.4}
 .setupoptional{border:1px solid var(--line);background:rgba(20,25,32,.72);border-radius:11px;padding:14px 15px;margin-top:15px;color:var(--mut);font-size:12px;line-height:1.5}
 .setupchoices{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.setupchoice{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;padding:9px 13px}.setupchoice.on{border-color:#2768d8;background:#102347;color:#dbeaff}
 .setupsearch{display:grid;grid-template-columns:1fr auto;gap:8px;margin:10px 0}.setupresults{display:grid;gap:7px;margin-top:8px;max-height:210px;overflow:auto}.setupresult{display:flex;align-items:center;gap:10px;border:1px solid var(--line);background:var(--card);border-radius:8px;padding:9px 11px}.setupresult img{width:38px;height:50px;object-fit:cover;border-radius:5px}.setupresult .grow{flex:1}.setupresult button{min-width:64px}.setupselected{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0 14px}.setupchip{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;padding:6px 10px;background:var(--card)}
 .setupstartergrid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.setupstartergrid h3{margin:0 0 7px}.setupfinishopts{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:18px}.setupfinishopt{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:16px}.setupfinishopt b{display:block;margin-bottom:5px}.setupfinishopt button{margin-top:13px}
 .setupactions{display:flex;align-items:center;gap:9px;margin:22px 28px 0;padding:17px 0 23px;border-top:1px solid var(--line)}.setupactions .setupspacer{flex:1}.setupactions button{min-width:94px}
 .setuptest{font-size:12px;color:var(--mut);min-height:18px;margin-top:8px}.setuptest.ok{color:#70d18a}.setuptest.err{color:#ff7676}
 @media(max-width:650px){.setupfields,.setupfeatures,.setupstartergrid,.setupfinishopts{grid-template-columns:1fr}.setupfields .full{grid-column:1}.setupbrand{padding:17px 18px 12px}.setupsteps{margin:0 18px 22px}.setupstep{padding:0 18px;min-height:0}.setupactions{margin:20px 18px 0;flex-wrap:wrap}}
</style></head><body>
<div id="globalDecor" class="globaldecor"></div>
<header>
  <h1><svg width="38" height="38" viewBox="0 0 240 240" style="vertical-align:-11px;margin-right:8px" xmlns="http://www.w3.org/2000/svg"><rect x="26" y="58" width="150" height="120" rx="16" fill="#3a2c1f" stroke="#241a12" stroke-width="4"/><rect x="38" y="70" width="126" height="96" rx="8" fill="#1b3a6b"/><ellipse cx="101" cy="140" rx="44" ry="11" fill="#e7a94e"/><ellipse cx="101" cy="139" rx="44" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><ellipse cx="101" cy="128" rx="42" ry="11" fill="#f0b95e"/><ellipse cx="101" cy="127" rx="42" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><ellipse cx="101" cy="116" rx="40" ry="11" fill="#f5c56e"/><ellipse cx="101" cy="115" rx="40" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><path d="M64 110 q6 12 14 4 q6 12 16 3 q7 12 16 3 q7 11 15 2 q6 10 12 3 l0 6 q-6 6 -12 2 q-8 8 -15 1 q-8 8 -16 1 q-8 8 -16 0 q-8 7 -14 -3 z" fill="#a8541f"/><rect x="86" y="86" width="30" height="14" rx="5" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.5"/><circle cx="192" cy="86" r="8" fill="#2a2a2a"/><circle cx="192" cy="112" r="8" fill="#2a2a2a"/><rect x="186" y="132" width="12" height="30" rx="3" fill="#2a2a2a"/><rect x="52" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="136" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="150" y="40" width="4" height="24" fill="#241a12"/><rect x="118" y="40" width="4" height="24" fill="#241a12" transform="rotate(-28 120 52)"/><circle cx="152" cy="38" r="6" fill="#f5c56e"/><circle cx="116" cy="34" r="6" fill="#f5c56e"/></svg>Olo's TVMate</h1>
  <a id="navMylist" onclick="showMylist()" data-i18n="Profile">Profile</a>
  <a id="navMytimeline" class="hide" onclick="showMytimeline()" data-i18n="Timeline">Timeline</a>
  <a id="navChannels" onclick="showChannels()" data-i18n="Playlists">Playlists</a>
  <a id="navMytv" onclick="showMytv()" data-i18n="Live TV">Live TV</a>
  <a id="navMovies" onclick="showMovies()" data-i18n="Movies">Movies</a>
  <a id="navShows" onclick="showShows()" data-i18n="Shows">Shows</a>
  <a id="navGames" onclick="showGames()" data-i18n="Games">Games</a>
  <a id="navRacing" onclick="showRacing()" data-i18n="Racing">Racing</a>
  <a id="navTeams" onclick="showTeams()" data-i18n="Sports">Sports</a>
  <a id="navSettings" onclick="showSettings()" data-i18n="Settings">Settings</a>
  <span id="slogan" class="slogan"></span>
  <span id="status" class="muted"></span>
  <button type="button" class="stopbtn headerstop" onclick="stopTVMate()" data-i18n="Stop TVMate" title="Stop TVMate">Stop TVMate</button>
  <div class="langsel">
    <button class="langflag on" id="langEN" onclick="setLang('en')" title="English">&#127468;&#127463;</button>
    <button class="langflag" id="langNO" onclick="setLang('no')" title="Norsk">&#127475;&#127476;</button>
  </div>
</header>
<div id="profileSetupOverlay" class="setupoverlay hide">
  <div class="setupwizard" role="dialog" aria-modal="true" aria-labelledby="setupTitle">
    <div class="setupbrand"><span id="setupBrandEmblem"></span><b>Olo's TVMate</b><span id="setupStepMeta" class="setupstepmeta"></span></div>
    <div id="setupProgress" class="setupsteps"></div>
    <div class="setupstep" data-key="profile">
      <h2 id="setupTitle" data-i18n="Welcome to TVMate">Welcome to TVMate</h2>
      <div class="setupintro" data-i18n="Let's make it yours. Everything here can be changed later from Settings or Edit Profile.">Let's make it yours. Everything here can be changed later from Settings or Edit Profile.</div>
      <div class="setupfields">
        <div><label data-i18n="Profile name">Profile name</label><input id="setupName" type="text" placeholder="Your name" data-i18n-ph="Your name"></div>
        <div><label data-i18n="Preferred language">Preferred language</label><select id="setupLang" onchange="setLang(this.value)"><option value="en">English</option><option value="no">Norsk</option></select></div>
        <div class="full"><label data-i18n="Pick an emblem">Pick an emblem</label><div id="setupEmblems" class="setupemblems"></div></div>
        <div class="full"><label data-i18n="Background style">Background style</label><select id="setupBackground"><option value="float" data-i18n="Floating pancakes & TVs">Floating pancakes &amp; TVs</option><option value="ascii">ASCII TVMate</option><option value="off" data-i18n="Off">Off</option></select></div>
      </div>
    </div>
    <div class="setupstep hide" data-key="follow">
      <h2 data-i18n="What do you want to follow?">What do you want to follow?</h2>
      <div class="setupintro" data-i18n="Movies and shows are always available. Turn the extra sections on or off here.">Movies and shows are always available. Turn the extra sections on or off here.</div>
      <div class="setupfeatures">
        <label class="setupfeature"><input id="setupFootball" type="checkbox"><span><b data-i18n="Football">Football</b><small data-i18n="Matchfinder, Sports and fixtures">Matchfinder, Sports and fixtures</small></span></label>
        <label class="setupfeature"><input id="setupRacing" type="checkbox"><span><b data-i18n="Racing">Racing</b><small data-i18n="Racing schedules and followed drivers">Racing schedules and followed drivers</small></span></label>
        <label class="setupfeature"><input id="setupGames" type="checkbox"><span><b data-i18n="Games">Games</b><small data-i18n="Steam wishlist and game releases">Steam wishlist and game releases</small></span></label>
      </div>
    </div>
    <div class="setupstep hide" data-key="football">
      <h2 data-i18n="Choose your football teams">Choose your football teams</h2>
      <div class="setupintro" data-i18n="Pick the teams you want fixtures for. You can add or remove teams later in Sports.">Pick the teams you want fixtures for. You can add or remove teams later in Sports.</div>
      <div id="setupTeamSelected" class="setupselected"></div>
      <div class="setupsearch"><input id="setupTeamQuery" type="text" placeholder="Search for a team, e.g. Brann" data-i18n-ph="Search for a team, e.g. Brann" onkeydown="if(event.key==='Enter')setupSearchTeams()"><button type="button" onclick="setupSearchTeams()" data-i18n="Search">Search</button></div>
      <div id="setupTeamResults" class="setupresults"></div>
    </div>
    <div class="setupstep hide" data-key="racing">
      <h2 data-i18n="Choose your racing">Choose your racing</h2>
      <div class="setupintro" data-i18n="Select the series you want in Racing and your timeline.">Select the series you want in Racing and your timeline.</div>
      <div id="setupRacingSeries" class="setupchoices"></div>
      <div id="setupF1TeamWrap" class="setupfields">
        <div class="full"><label data-i18n="Favorite Formula 1 team">Favorite Formula 1 team</label><select id="setupF1TeamSelect" onchange="setupSelectF1Team(this.value)"><option value="" data-i18n="Choose a Formula 1 team">Choose a Formula 1 team</option></select></div>
      </div>
    </div>
    <div class="setupstep hide" data-key="content">
      <h2 data-i18n="Add something to watch">Add something to watch</h2>
      <div class="setupintro" data-i18n="Optional: add a favorite show or movie now, or let TVMate add demo items so you can see what Profile looks like.">Optional: add a favorite show or movie now, or let TVMate add demo items so you can see what Profile looks like.</div>
      <div class="setupstartergrid">
        <div><h3 data-i18n="Shows">Shows</h3><div class="setupsearch"><input id="setupShowQuery" type="text" placeholder="Search shows" data-i18n-ph="Search shows" onkeydown="if(event.key==='Enter')setupSearchContent('show')"><button type="button" onclick="setupSearchContent('show')" data-i18n="Search">Search</button></div><div id="setupShowResults" class="setupresults"></div></div>
        <div><h3 data-i18n="Movies">Movies</h3><div class="setupsearch"><input id="setupMovieQuery" type="text" placeholder="Search movies" data-i18n-ph="Search movies" onkeydown="if(event.key==='Enter')setupSearchContent('movie')"><button type="button" onclick="setupSearchContent('movie')" data-i18n="Search">Search</button></div><div id="setupMovieResults" class="setupresults"></div></div>
      </div>
      <div class="setupoptional" data-i18n="If you don't add anything yet, TVMate will add a couple of demo items. They disappear permanently when you favorite your first real movie or show.">If you don't add anything yet, TVMate will add a couple of demo items. They disappear permanently when you favorite your first real movie or show.</div>
    </div>
    <div class="setupstep hide" data-key="launch">
      <h2 data-i18n="How should TVMate open?">How should TVMate open?</h2>
      <div class="setupintro" data-i18n="TVMate opens straight in your browser. Use Stop TVMate in the top-right when you want to shut the app down.">TVMate opens straight in your browser. Use Stop TVMate in the top-right when you want to shut the app down.</div>
      <div class="setupfeatures"><div class="setupfeature"><span><b data-i18n="Modern TVMate">Modern TVMate</b><small data-i18n="TVMate opens straight in your browser with no CMD window.">TVMate opens straight in your browser with no CMD window.</small></span></div></div>
      <div id="setupBookmarkHelp" class="setupoptional"><b style="display:block;color:var(--fg);margin-bottom:6px" data-i18n="Bookmark TVMate">Bookmark TVMate</b><span data-i18n="Press Ctrl+D to bookmark TVMate for an easy way back.">Press Ctrl+D to bookmark TVMate for an easy way back.</span><div class="row" style="margin-top:10px"><code id="setupLocalUrl"></code><button type="button" class="ghost" onclick="copySetupLocalUrl(this)" data-i18n="Copy address">Copy address</button></div></div>
      <div id="setupAutoShutdownWrap" class="setupoptional"><b style="display:block;color:var(--fg);margin-bottom:7px" data-i18n="Auto shutdown when inactive">Auto shutdown when inactive</b><label><span data-i18n="Stop TVMate after">Stop TVMate after</span> <select id="setupAutoShutdown" style="width:auto;margin-left:7px"><option value="0" data-i18n="Keep running — uses approximately three crumbs and your calculator works harder">Keep running — uses approximately three crumbs and your calculator works harder</option><option value="30">30 minutes</option><option value="60">1 hour</option><option value="120">2 hours</option><option value="240">4 hours</option></select></label><div style="margin-top:6px" data-i18n="Activity in TVMate resets the timer.">Activity in TVMate resets the timer.</div></div>
    </div>
    <div class="setupstep hide" data-key="finish">
      <h2 data-i18n="You're ready">You're ready</h2>
      <div class="setupintro" data-i18n="TVMate is set up around what you follow. A streaming login is only needed for your own channels, movies and shows.">TVMate is set up around what you follow. A streaming login is only needed for your own channels, movies and shows.</div>
      <div class="setupfinishopts"><div class="setupfinishopt"><b data-i18n="Want playback too?">Want playback too?</b><span class="muted" data-i18n="Set up your Xtream login for your own channels, movies and shows.">Set up your Xtream login for your own channels, movies and shows.</span><br><button type="button" class="ghost" onclick="finishProfileSetup(this,true)" data-i18n="Set up Xtream">Set up Xtream</button></div><div class="setupfinishopt"><b data-i18n="All done">All done</b><span class="muted" data-i18n="Head straight to Profile and start using TVMate.">Head straight to Profile and start using TVMate.</span><br><button type="button" onclick="finishProfileSetup(this,false)" data-i18n="Let's go, I'm ready">Let's go, I'm ready</button></div></div>
    </div>
    <div class="setupactions">
      <button id="setupSkip" type="button" class="ghost" onclick="skipProfileSetup()" data-i18n="Skip setup">Skip setup</button>
      <div class="setupspacer"></div>
      <button id="setupBack" type="button" class="ghost hide" onclick="setupStep(-1)" data-i18n="Back">Back</button>
      <button id="setupNext" type="button" onclick="setupStep(1)" data-i18n="Next">Next</button>
    </div>
  </div>
</div>
<div id="editProfileOverlay" class="setupoverlay hide" onclick="if(event.target===this)closeEditProfile()">
  <div class="editprofiledialog" role="dialog" aria-modal="true" aria-labelledby="editProfileTitle">
    <h2 id="editProfileTitle" data-i18n="Edit Profile">Edit Profile</h2>
    <div class="setupintro" data-i18n="Your everyday TVMate preferences. Run the setup guide to change what you follow.">Your everyday TVMate preferences. Run the setup guide to change what you follow.</div>
    <div class="setupfields">
      <div><label data-i18n="Profile name">Profile name</label><input id="ep_name" type="text"></div>
      <div><label data-i18n="Preferred language">Preferred language</label><select id="ep_lang"><option value="en">English</option><option value="no">Norsk</option></select></div>
      <div class="full"><label data-i18n="Emblem">Emblem</label><div id="ep_emblems" class="setupemblems"></div></div>
      <div><label data-i18n="Default start section">Default start section</label><select id="ep_start"><option value="mylist" data-i18n="Profile">Profile</option><option value="mytimeline" data-i18n="Timeline">Timeline</option><option value="channels" data-i18n="Playlists">Playlists</option><option value="mytv" data-i18n="Live TV">Live TV</option><option value="movies" data-i18n="Movies">Movies</option><option value="shows" data-i18n="Shows">Shows</option><option value="games" data-i18n="Games">Games</option><option value="racing" data-i18n="Racing">Racing</option><option value="teams" data-i18n="Sports">Sports</option></select></div>
      <div><label data-i18n="Profile layout">Profile layout</label><select id="ep_layout"><option value="timeline">Now Timeline</option><option value="balanced">Balanced</option><option value="spotlight">Spotlight</option><option value="hub">Profile Hub</option></select></div>
      <label class="setupfeature full"><input id="ep_checkshows" type="checkbox"><span><b data-i18n="Check favorite shows on startup">Check favorite shows on startup</b><small data-i18n="Look for newly available episodes when TVMate starts.">Look for newly available episodes when TVMate starts.</small></span></label>
      <label class="setupfeature full"><input id="ep_refreshiptv" type="checkbox"><span><b data-i18n="Refresh IPTV & EPG on startup">Refresh IPTV &amp; EPG on startup</b><small data-i18n="Refresh Xtream channels, movies, shows and TV guide data.">Refresh Xtream channels, movies, shows and TV guide data.</small></span></label>
      <label class="setupfeature full"><input id="ep_refreshsports" type="checkbox"><span><b data-i18n="Refresh sports, racing & games on startup">Refresh sports, racing &amp; games on startup</b><small data-i18n="Refresh matches, regional TV listings, racing and your Steam wishlist.">Refresh matches, regional TV listings, racing and your Steam wishlist.</small></span></label>
      <div class="full"><label data-i18n="Background style">Background style</label><select id="ep_background"><option value="float" data-i18n="Floating pancakes & TVs">Floating pancakes &amp; TVs</option><option value="ascii">ASCII TVMate</option><option value="off" data-i18n="Off">Off</option></select></div>
    </div>
    <div class="editprofileactions"><button type="button" class="ghost" onclick="runSetupGuideFromProfile()" data-i18n="Run setup guide">Run setup guide</button><div class="spacer"></div><button type="button" class="ghost" onclick="closeEditProfile()" data-i18n="Cancel">Cancel</button><button type="button" onclick="saveEditProfile(this)" data-i18n="Save">Save</button></div>
  </div>
</div>
<main>
  <section id="channelsView" class="hide">
    <div class="playlistsearch">
      <div class="col">
        <div class="colh"><span data-i18n="Find Channels">Find Channels</span><button type="button" class="clrbtn" onclick="resetPlaylistSearch()" data-i18n="Reset">Reset</button></div>
        <div class="row"><input id="cq" type="text" placeholder="Find a channel, e.g. tv2 play" data-i18n-ph="Find a channel, e.g. tv2 play" onkeydown="if(event.key==='Enter')doChannelSearch('cq','cresults')"><button onclick="doChannelSearch('cq','cresults')" data-i18n="Search">Find</button></div>
        <div id="cresults"></div>
      </div>
      <div class="col">
        <div class="colh"><span data-i18n="Find Categories">Find Categories</span><button type="button" class="clrbtn" onclick="resetPlaylistSearch()" data-i18n="Reset">Reset</button></div>
        <div class="row"><input id="catq" type="text" placeholder="Search a category, e.g. Norway" data-i18n-ph="Search a category, e.g. Norway" onkeydown="if(event.key==='Enter')doCategorySearch()"><button onclick="doCategorySearch()" data-i18n="Search">Find</button></div>
        <div id="catresults"></div>
      </div>
    </div>
    <div class="ch4">
      <div class="ch4cats">
        <div class="colh"><span data-i18n="All Categories">All Categories</span></div>
        <input id="catfilter" type="text" placeholder="Filter categories..." data-i18n-ph="Filter categories..." oninput="renderCatList()" class="catsearch">
        <div id="catlist"></div>
      </div>

      <div class="ch4group">
      <div class="pancakes-pl" id="pcakePL"></div>
      <div class="ch4col">
        <div class="colh"><span data-i18n="Selected categories">Selected categories</span><button class="clrbtn" onclick="clearSelectedCats()" data-i18n="Clear">Clear</button></div>
        <div id="selcats" class="pcol"><span class="muted" data-i18n="Tick categories on the left.">Tick categories on the left.</span></div>
        <div class="row plbtns" style="flex-direction:column;align-items:stretch;gap:6px">
          <button class="ghost" onclick="favSelectedCats()"><span data-i18n="★ Add to Favorites">&#9733; Add to Favorites</span></button>
          <button onclick="buildM3U('categories')" data-i18n="Make Playlist (Categories)">Make Playlist (Categories)</button>
        </div>
      </div>

      <div class="ch4col">
        <div class="colh" id="ccHead"><span data-i18n="Filter Channels">Filter Channels</span></div>
        <div id="ccList" class="pcol"><span class="muted" data-i18n="Click a selected category to see its channels.">Click a selected category to see its channels.</span></div>
        <div class="row plbtns">
          <button class="ghost" onclick="ccTick(true)" data-i18n="Tick all">Tick all</button>
          <button class="ghost" onclick="ccTick(false)" data-i18n="Untick all">Untick all</button>
          <button onclick="addTickedToPlaylist()"><span data-i18n="Add ticked">Add ticked</span> &raquo;</button>
        </div>
      </div>

      <div class="ch4col">
        <div class="colh"><span data-i18n="Playlist">Playlist</span> <span id="plCount" class="muted"></span><button class="clrbtn" onclick="clearPlaylist()" data-i18n="Clear">Clear</button></div>
        <div id="plList" class="pcol"><span class="muted" data-i18n="Ticked channels land here.">Ticked channels land here.</span></div>
        <div class="row plbtns" style="flex-direction:column;align-items:stretch;gap:6px">
          <button class="ghost" onclick="favPlaylist()"><span data-i18n="★ Add to Favorites">&#9733; Add to Favorites</span></button>
          <button onclick="buildPlaylistM3U()" data-i18n="Make Playlist (Channels)">Make Playlist (Channels)</button>
        </div>
      </div>
      </div>
    </div>
    <div class="footline"></div>
  </section>

  <section id="mytvView" class="hide">
    <div class="tvwrap">
      <div class="tvrail" id="tvRail"></div>
      <div class="tvguide">
        <div class="tvguidehead">
          <div class="tvchancol"><button class="ghost" id="epgRefresh" onclick="epgRefresh()" title="Reload EPG"><span data-i18n="EPG Refresh">&#128197; EPG</span></button></div>
          <div class="tvtimeline" id="tvTimeHead"></div>
        </div>
        <div class="tvguidebody" id="tvGuideBody"></div>
        <div class="tvplayerslot" id="tvPlayerSlot"></div>
      </div>
    </div>
  </section>

  <section id="mylistView" class="hide">
    <div class="mydash">
      <div id="myListProfile" class="mylistprofile"></div>
      <div class="mydashblock" id="myListTeamsBlock">
        <div class="mydashhead" id="myListSportHeading"><div class="mydashsportheading sport">Sport</div></div>
        <div id="myListTeams" class="mydashgrid"><span class="muted">Loading...</span></div>
      </div>
      <div class="mydashblock" id="myListShowsBlock">
        <div class="mydashhead"><div class="colh" data-i18n="Shows">Shows</div></div>
        <div id="myListShows" class="mydashepisodes"><span class="muted">Loading...</span></div>
      </div>
      <div class="mydashblock hide" id="myListTimelineBlock">
        <div class="mydashhead"><div class="colh" data-i18n="Now & Next">Now &amp; Next</div></div>
        <div id="myListTimeline"></div>
      </div>
      <div class="mydashblock" id="myListChannelsBlock">
        <div class="mydashhead"><div class="colh mydashliveheading" onclick="toggleMyListChannelPicker()" title="Choose channels" data-i18n="Live TV">Live TV</div></div>
        <div id="myListChannels" class="mydashchannels"></div>
        <div id="myListChannelPicker" class="mydashchooser hide"></div>
      </div>
    </div>
  </section>

  <section id="mytimelineView" class="hide">
    <div class="mytimelinepage">
      <h2 class="colh" data-i18n="Timeline">Timeline</h2>
      <div id="myTimelineStandalone"></div>
    </div>
  </section>

  <section id="moviesView" class="hide">
    <button id="movieRefreshBtn" class="showrefresh" onclick="checkMovies(this)">&#8635; <span data-i18n="Check for new movies">Check for new movies</span></button>
    <div class="movieswrap">
      <aside class="moviefavs">
        <div class="colh">&#9733; <span data-i18n="Favorite Movies">Favorite Movies</span></div>
        <div id="movieFavList" class="moviefavlist"><span class="muted">No favorite movies yet.</span></div>
      </aside>
      <div class="moviesmain">
        <h2 class="colh" data-i18n="Movies">Movies</h2>
        <div class="row sectionsearch">
          <input id="movieQ" type="text" placeholder="Search your movies..." data-i18n-ph="Search your movies..." onkeydown="if(event.key==='Enter')searchMovies()">
          <button onclick="searchMovies()" data-i18n="Search">Search</button>
        </div>
        <div id="movieCatalogs" class="moviecatalogs">
          <section id="recentMoviesSection" class="moviecatalogcolumn">
            <header class="moviecataloghead"><div class="colh" data-i18n="Recently Added">Recently Added</div></header>
            <div id="recentMovieList"><span class="muted">Loading...</span></div>
            <div style="text-align:center;margin-top:14px"><button id="recentMovieMore" class="ghost hide" onclick="expandRecentMovies(this)" data-i18n="See what else is new">See what else is new</button></div>
          </section>
          <section class="moviecatalogcolumn">
            <header class="moviecataloghead">
              <div class="colh" data-i18n="Discover Movies">Discover Movies</div>
              <nav class="moviecatalogtabs" aria-label="Movie catalog">
                <button class="moviecatalogtab on" data-movie-catalog="popular" onclick="loadCinemetaMovies('popular')" data-i18n="Popular">Popular</button>
                <button class="moviecatalogtab" data-movie-catalog="new" onclick="loadCinemetaMovies('new')" data-i18n="New Releases">New Releases</button>
                <button class="moviecatalogtab" data-movie-catalog="featured" onclick="loadCinemetaMovies('featured')" data-i18n="Featured">Featured</button>
              </nav>
            </header>
            <div id="cinemetaMovieList"><span class="muted">Loading...</span></div>
          </section>
        </div>
        <div id="movieResults"></div>
      </div>
    </div>
  </section>

  <section id="showsView" class="hide">
    <button id="showRefreshBtn" class="showrefresh" onclick="checkAllShows(this)">&#8635; <span data-i18n="Check for new episodes">Check for new episodes</span></button>
    <div class="showswrap">
      <aside class="showfavs">
        <div class="colh">&#9733; <span data-i18n="Favorite Shows">Favorite Shows</span></div>
        <div id="showFavList" class="showfavlist"><span class="muted">No favorite shows yet.</span></div>
      </aside>
      <div class="showsmain">
        <h2 class="colh" data-i18n="Shows">Shows</h2>
        <div class="row sectionsearch">
          <input id="showQ" type="text" placeholder="Search your shows..." data-i18n-ph="Search your shows..." onkeydown="if(event.key==='Enter')searchShows()">
          <button onclick="searchShows()" data-i18n="Search">Search</button>
        </div>
        <div id="latestEpisodesSection">
          <div class="colh" style="margin-top:20px" data-i18n="Your Latest Episodes">Your Latest Episodes</div>
          <div id="latestEpisodeList"><span class="muted">Loading...</span></div>
          <div style="text-align:center;margin-top:14px"><button id="latestEpisodeMore" class="ghost hide" onclick="expandLatestEpisodes(this)" data-i18n="See more latest episodes">See more latest episodes</button></div>
          <div id="upcomingEpisodesSection" class="hide">
            <div class="colh" style="margin-top:24px" data-i18n="Upcoming Episodes">Upcoming Episodes</div>
            <div id="upcomingEpisodeList"></div>
          </div>
        </div>
        <div id="showResults"></div>
        <div id="showDetails" class="showdetails"></div>
      </div>
    </div>
  </section>

  <section id="gamesView" class="hide">
    <div class="gameslayout">
      <aside id="steamProfile" class="steamprofile"><div class="steamprofileempty">Steam profile</div></aside>
      <div class="gamesmain">
        <div class="gameshead">
          <div><h2 class="colh" data-i18n="Games">Games</h2><div id="steamWishlistStatus" class="moviemeta"></div></div>
          <div class="gamesheadactions"><button id="steamWishlistQuickBtn" class="ghost" onclick="syncSteamWishlist(this)">&#8635; <span data-i18n="Refresh wishlist">Refresh wishlist</span></button><button class="ghost" onclick="toggleSteamWishlistSettings()">&#9881; <span data-i18n="Wishlist settings">Wishlist settings</span></button></div>
        </div>
        <div id="steamWishlistSettings" class="gameswishlistsettings hide"><div class="row sectionsearch"><input id="steamWishlistQ" type="text" placeholder="Steam wishlist URL..." data-i18n-ph="Steam wishlist URL..."><button id="steamWishlistBtn" class="ghost" onclick="syncSteamWishlist(this)" data-i18n="Sync wishlist">Sync wishlist</button></div></div>
        <div id="steamWishlistHelp" class="wishlisthelp hide">
          <b>Steps to Make Your Wishlist Public</b>
          <ul>
            <li>Open the <b>Steam</b> app or website.</li>
            <li>Click your <b>Profile name</b> at the top right and select <b>Profile</b>.</li>
            <li>Click the <b>Edit Profile</b> button on the right side.</li>
            <li>Select <b>Privacy Settings</b> from the menu on the left.</li>
            <li>Set <b>My Profile</b> and <b>Game details</b> to <b>Public</b>.</li>
          </ul>
          <div class="wishlisthelpsection">
            <b>How to Share Your Wishlist Link</b>
            <ul>
              <li>Go back to your profile page.</li>
              <li>Hover over <b>Store</b> and click <b>Wishlist</b>.</li>
              <li>Copy the URL from your browser or the top right of the wishlist page and paste it into TVMate.</li>
            </ul>
            <span class="wishlistexample">https://store.steampowered.com/wishlist/profiles/76561198000000000/</span>
          </div>
        </div>
        <div id="gameWishlistFilterRow" class="row hide" style="margin-top:14px">
          <input id="gameWishlistFilter" type="text" placeholder="Filter wishlist..." data-i18n-ph="Filter wishlist..." oninput="renderGameWishlist()">
        </div>
        <div id="gameWishlist" class="gamegrid" style="margin-top:18px"></div>
      </div>
    </div>
  </section>

  <section id="racingView" class="hide">
    <div class="racinglayout">
      <aside class="racingsidebar">
        <div id="racingF1TeamControl" class="racingteamcontrol f1Feature">
          <div id="racingTeamLabel" class="colh">Formula 1 Team</div>
          <button id="racingF1ChooseBtn" class="ghost racingteamselect" onclick="toggleRacingF1Picker()"><span data-i18n="Choose F1 team">Choose F1 team</span></button>
          <div id="racingF1Picker" class="mydashchooser racingteampicker hide"></div>
        </div>
        <div id="racingDriverDetail" class="racingdetail"><span class="muted">Choose a driver to see details.</span></div>
      </aside>
      <div class="racingwrap">
        <h2 class="colh" data-i18n="Racing">Racing</h2>
        <div class="muted" data-i18n="Choose the racing series you want to follow.">Choose the racing series you want to follow.</div>
        <div id="racingSeries" class="racingseries"></div>
        <div id="racingDrivers" class="racingdrivers"></div>
        <div id="racingInfo" class="racinggrid"><span class="muted">Loading...</span></div>
      </div>
    </div>
  </section>

  <section id="teamsView" class="hide">
    <button id="teamRefreshBtn" class="showrefresh" onclick="checkTeamFixtures(this)">&#8635; <span data-i18n="Refresh fixtures">Refresh fixtures</span></button>
    <div class="teamswrap">
      <aside class="teamfavs">
        <div id="teamProfileDetail" class="teamprofiledetail"><span class="muted" data-i18n="Choose a team to see details.">Choose a team to see details.</span></div>
        <div class="teamfavdivider"></div>
        <div class="colh">&#9733; <span data-i18n="Favorite Teams">Favorite Teams</span></div>
        <div id="teamFavList" class="teamfavlist"><span class="muted">No favorite teams yet.</span></div>
      </aside>
      <div class="teamsmain">
        <h2 class="colh" data-i18n="Sports">Sports</h2>
        <div class="teammatchfinder">
          <div class="matchfinderhead"><div><div class="matchfindertitle" data-i18n="Find a match">Find a match</div><div class="matchfindersub" data-i18n="Search for a team, then choose Find fixtures when you want Matchfinder and TV results.">Search for a team, then choose Find fixtures when you want Matchfinder and TV results.</div></div></div>
          <div class="row sectionsearch sportssearchrow">
            <input id="q" type="text" placeholder="Search a team, e.g. Leeds" data-i18n-ph="Search a team, e.g. Leeds" onkeydown="if(event.key==='Enter')searchTeamHub()">
            <button onclick="searchTeamHub()" data-i18n="Find team">Find team</button>
            <button class="ghost sportschannelsearch" onclick="openSportsChannelSearch()" data-i18n="Search channels">Search channels</button>
          </div>
          <div class="matchfindercontrols"><div class="matchstrict"><span data-i18n="Match strictness">Match strictness</span>
              <input id="matchStrict" type="range" min="0.40" max="0.80" step="0.01" value="0.62" oninput="document.getElementById('matchStrictValue').textContent=this.value" onchange="saveMatchStrictness(this.value)">
              <span id="matchStrictValue" class="matchstrictvalue">0.62</span>
            </div><span class="matchfinderhint" data-i18n="Lower strictness only if a known channel is being missed.">Lower strictness only if a known channel is being missed.</span>
          </div>
          <div id="sportsSearchBack" class="sportssearchback hide"><button type="button" class="ghost" onclick="clearSportsSearch()">&#8592; <span data-i18n="Back to Sports">Back to Sports</span></button></div>
          <div id="teamSearchResults" class="teamsearchresults"></div>
          <div id="results" class="teammatchresults"></div>
        </div>
        <div id="teamLiveSection" class="hide">
          <div class="colh" style="margin-top:22px" data-i18n="Live Matches">Live Matches</div>
          <div id="teamLiveList" class="teamfixturegrid"></div>
        </div>
        <div id="teamTopSection" class="hide">
          <div class="colh" style="margin-top:22px" data-i18n="Today's Top Fixtures">Today's Top Fixtures</div>
          <div id="teamTopList"></div>
        </div>
        <div class="colh" style="margin-top:22px" data-i18n="Upcoming Fixtures">Upcoming Fixtures</div>
        <div id="teamUpcomingList"><span class="muted">Loading...</span></div>
      </div>
    </div>
  </section>

  <section id="settingsView" class="hide">
    <div class="settingswrap">
    <div class="brandblock">
      <svg width="120" height="120" viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg"><rect x="26" y="58" width="150" height="120" rx="16" fill="#3a2c1f" stroke="#241a12" stroke-width="4"/><rect x="38" y="70" width="126" height="96" rx="8" fill="#1b3a6b"/><ellipse cx="101" cy="140" rx="44" ry="11" fill="#e7a94e"/><ellipse cx="101" cy="139" rx="44" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><ellipse cx="101" cy="128" rx="42" ry="11" fill="#f0b95e"/><ellipse cx="101" cy="127" rx="42" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><ellipse cx="101" cy="116" rx="40" ry="11" fill="#f5c56e"/><ellipse cx="101" cy="115" rx="40" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><path d="M64 110 q6 12 14 4 q6 12 16 3 q7 12 16 3 q7 11 15 2 q6 10 12 3 l0 6 q-6 6 -12 2 q-8 8 -15 1 q-8 8 -16 1 q-8 8 -16 0 q-8 7 -14 -3 z" fill="#a8541f"/><rect x="86" y="86" width="30" height="14" rx="5" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.5"/><circle cx="192" cy="86" r="8" fill="#2a2a2a"/><circle cx="192" cy="112" r="8" fill="#2a2a2a"/><rect x="186" y="132" width="12" height="30" rx="3" fill="#2a2a2a"/><rect x="52" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="136" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="150" y="40" width="4" height="24" fill="#241a12"/><rect x="118" y="40" width="4" height="24" fill="#241a12" transform="rotate(-28 120 52)"/><circle cx="152" cy="38" r="6" fill="#f5c56e"/><circle cx="116" cy="34" r="6" fill="#f5c56e"/></svg>
      <div class="bname">Olo's TVMate</div>
      <div class="btag">Find your match. Build your playlist.</div>
      <div class="btag" style="opacity:.6;font-size:11px;margin-top:4px">v__VERSION__</div>
    </div>
    <div class="card settingscard">
      <nav class="settingstabs" aria-label="Settings categories">
        <button type="button" class="settingstab on" data-settings-tab="profile" onclick="setSettingsTab('profile')" data-i18n="Profile">Profile</button>
        <button type="button" class="settingstab" data-settings-tab="general" onclick="setSettingsTab('general')" data-i18n="General">General</button>
        <button type="button" class="settingstab" data-settings-tab="iptv" onclick="setSettingsTab('iptv')" data-i18n="IPTV & EPG">IPTV &amp; EPG</button>
        <button type="button" class="settingstab" data-settings-tab="maintenance" onclick="setSettingsTab('maintenance')" data-i18n="Maintenance">Maintenance</button>
        <button type="button" class="settingstab" data-settings-tab="health" onclick="setSettingsTab('health')" data-i18n="Health">Health</button>
      </nav>
      <div class="settingspanels">
      <div id="settingsProfile" class="settingspanel">
       <div class="settingsgroup" data-settings-panel="profile">
        <div class="colh" data-i18n="Profile">Profile</div>
        <div class="muted" data-i18n="Personalize TVMate and choose what opens when the app starts.">Personalize TVMate and choose what opens when the app starts.</div>
        <div class="grid2">
          <div><label data-i18n="Profile name">Profile name</label><input id="s_profile" type="text" maxlength="40"></div>
          <div><label data-i18n="Profile emblem">Profile emblem</label><div id="s_emblems" class="emblempicker"></div></div>
          <div><label data-i18n="Profile layout">Profile layout</label>
            <select id="s_mylistlayout"><option value="balanced" data-i18n="Balanced">Balanced</option><option value="spotlight" data-i18n="Spotlight">Spotlight</option><option value="timeline" data-i18n="Now Timeline">Now Timeline</option><option value="hub" data-i18n="Profile Hub">Profile Hub</option></select>
            <div class="muted" style="margin-top:6px" data-i18n="Changes the arrangement of your Profile page only.">Changes the arrangement of your Profile page only.</div></div>
          <div><label data-i18n="Preferred language">Preferred language</label>
            <select id="s_lang"><option value="en">English</option><option value="no">Norsk</option></select></div>
          <div><label data-i18n="Default start section">Default start section</label>
            <select id="s_start"><option value="mylist">Profile</option><option id="startTimelineOption" value="mytimeline">Timeline</option><option value="channels">Playlists</option><option value="mytv">Live TV</option><option value="movies">Movies</option><option value="shows">Shows</option><option id="startGamesOption" value="games">Games</option><option id="startRacingOption" value="racing">Racing</option><option value="teams">Sports</option></select></div>
          <div><label data-i18n="Background style">Background style</label><select id="s_background"><option value="float" data-i18n="Floating pancakes & TVs">Floating pancakes &amp; TVs</option><option value="ascii">ASCII TVMate</option><option value="off" data-i18n="Off">Off</option></select></div>
        </div>
        <div style="margin-top:14px"><button type="button" class="ghost" onclick="openProfileSetup(false)" data-i18n="Run setup guide">Run setup guide</button></div>
       </div>
       <div class="settingsgroup" data-settings-panel="profile">
        <div class="colh" data-i18n="Backup & Import">Backup &amp; Import</div>
        <div class="muted" data-i18n="Download a portable backup or merge one into this profile. Caches and artwork are never included.">Download a portable backup or merge one into this profile. Caches and artwork are never included.</div>
        <div class="row settingsrefreshbuttons" style="margin-top:13px">
          <button type="button" class="ghost" onclick="exportProfileBackup(false)" data-i18n="Export profile">Export profile</button>
          <button type="button" class="ghost" onclick="exportProfileBackup(true)" data-i18n="Export full backup">Export full backup</button>
          <button type="button" onclick="document.getElementById('profileImportFile').click()" data-i18n="Import backup">Import backup</button>
          <input id="profileImportFile" type="file" accept="application/json,.json" class="hide" onchange="importProfileBackup(this)">
        </div>
        <div class="muted" style="margin-top:9px" data-i18n="Profile backup keeps credentials out. Full backup includes Xtream credentials; store it securely.">Profile backup keeps credentials out. Full backup includes Xtream credentials; store it securely.</div>
        <div id="profileBackupMsg" class="muted" style="margin-top:9px"></div>
       </div>
       <div class="settingsgroup" data-settings-panel="general" hidden>
        <div class="colh" data-i18n="Content startup">Content startup</div>
        <div class="muted" data-i18n="Choose which content TVMate updates automatically after it opens.">Choose which content TVMate updates automatically after it opens.</div>
        <div class="settingschecks">
        <label class="settingscheck">
          <input id="s_checkshows" type="checkbox" style="width:auto;margin:0">
          <span data-i18n="Check favorite shows on startup">Check favorite shows on startup</span>
        </label>
        <label class="settingscheck">
          <input id="s_refreshiptv" type="checkbox" style="width:auto;margin:0">
          <span data-i18n="Refresh IPTV & EPG on startup">Refresh IPTV &amp; EPG on startup</span>
        </label>
        <label class="settingscheck">
          <input id="s_refreshsports" type="checkbox" style="width:auto;margin:0">
          <span data-i18n="Refresh sports, racing & games on startup">Refresh sports, racing &amp; games on startup</span>
        </label>
        </div>
       </div>
       <div class="settingsgroup" data-settings-panel="general" hidden>
        <div class="colh" data-i18n="Features & Display">Features &amp; Display</div>
        <div class="muted" data-i18n="Show or hide optional sections. Disabling one also skips it during external-content refreshes.">Show or hide optional sections. Disabling one also skips it during external-content refreshes.</div>
        <div class="settingschecks">
        <label class="settingscheck">
          <input id="s_football" type="checkbox" style="width:auto;margin:0">
          <span data-i18n="Show football features">Show football features</span>
        </label>
        <label class="settingscheck">
          <input id="s_f1" type="checkbox" style="width:auto;margin:0">
          <span data-i18n="Show racing features">Show racing features</span>
        </label>
        <label class="settingscheck">
          <input id="s_games" type="checkbox" style="width:auto;margin:0">
          <span data-i18n="Show game features">Show game features</span>
        </label>
        </div>
       </div>
      </div>
      <div id="settingsSetup" class="settingspanel">
      <div class="settingsgroup" data-settings-panel="iptv" hidden>
      <div class="colh" data-i18n="Connection">Connection</div>
      <div class="muted" data-i18n="Your Xtream login stays in your local config.json and is only sent to your own provider.">Your Xtream login stays in your local config.json and is only sent to your own provider.</div>
      <div class="grid2">
        <div><label data-i18n="Username">Username</label><input id="s_user" type="text"></div>
        <div><label data-i18n="Password">Password</label><input id="s_pass" type="password"></div>
        <div><label data-i18n="Host (e.g. http://example.com:8080)">Host (e.g. http://example.com:8080)</label><input id="s_host" type="text"></div>
        <div style="display:flex;align-items:flex-end;gap:8px;flex-wrap:wrap"><button class="ghost" onclick="testLogin()" data-i18n="Test login">Test login</button></div>
      </div>
      <div id="s_connmsg" class="muted" style="margin-top:10px"></div>
      </div>
      <div class="settingsgroup" data-settings-panel="iptv" hidden>
      <div class="colh" data-i18n="Data & Refresh">Data &amp; Refresh</div>
      <div class="muted" data-i18n="Choose exactly which TVMate data should be updated.">Choose exactly which TVMate data should be updated.</div>
      <div class="row settingsrefreshbuttons" style="margin-top:13px">
        <button class="ghost" onclick="refreshIptvContent(this).catch(()=>{})" data-i18n="Refresh IPTV & EPG">Refresh IPTV &amp; EPG</button>
      </div>
      </div>
      <div class="settingsgroup" data-settings-panel="general" hidden>
      <div class="colh" data-i18n="Sports Search">Sports Search</div>
      <div class="muted" data-i18n="Choose which regional TV listings Sports Search uses to find broadcasters for matches.">Choose which regional TV listings Sports Search uses to find broadcasters for matches.</div>
      <label data-i18n="TV listings countries (comma separated, e.g. no, uk, us)">TV listings countries (comma separated, e.g. no, uk, us)</label>
      <input id="s_cc" type="text">
      <div class="muted" style="margin-top:7px" data-i18n="Enter country codes for the TV guides you want searched. Adjust match strictness from the Sports page.">Enter country codes for the TV guides you want searched. Adjust match strictness from the Sports page.</div>
      </div>
      <div class="settingsgroup" data-settings-panel="iptv" hidden>
      <div class="colh" data-i18n="Playback">Playback</div>
      <div class="muted" data-i18n="Choose the stream URL format requested from your IPTV provider. TS is the normal default; use M3U8 if your provider works better with HLS.">Choose the stream URL format requested from your IPTV provider. TS is the normal default; use M3U8 if your provider works better with HLS.</div>
      <div style="margin-top:13px"><label data-i18n="Stream extension">Stream extension</label><select id="s_ext"><option value="ts">ts</option><option value="m3u8">m3u8</option></select></div>
      </div>
      <div class="settingsgroup" data-settings-panel="maintenance" hidden>
      <div class="colh" data-i18n="Maintenance">Maintenance</div>
      <div class="muted" data-i18n="Control automatic updates of local data and manage TVMate's local files.">Control automatic updates of local data and manage TVMate's local files.</div>
      <div style="margin-top:13px"><label data-i18n="Auto shutdown when inactive">Auto shutdown when inactive</label><select id="s_autoshutdown"><option value="0" data-i18n="Keep running — uses approximately three crumbs and your calculator works harder">Keep running — uses approximately three crumbs and your calculator works harder</option><option value="30">30 minutes</option><option value="60">1 hour</option><option value="120">2 hours</option><option value="240">4 hours</option></select></div>
      <div class="muted" style="margin-top:7px" data-i18n="Stops the local TVMate server after no interaction or playback. Active video keeps TVMate awake.">Stops the local TVMate server after no interaction or playback. Active video keeps TVMate awake.</div>
      <div class="row settingsrefreshbuttons" style="margin-top:14px"><button class="ghost" onclick="refreshOtherContent(this).catch(()=>{})" data-i18n="Refresh sports, racing & games">Refresh sports, racing &amp; games</button><button onclick="refreshEverything(this).catch(()=>{})" data-i18n="Refresh everything">Refresh everything</button></div>
      <div class="muted" style="margin-top:14px"><span data-i18n="Artwork cache">Artwork cache</span>: <b id="s_artsize" data-i18n="Checking...">Checking...</b></div>
      <div class="row" style="margin-top:14px"><button class="ghost" onclick="clearArtworkCache()" data-i18n="Clear artwork cache">Clear artwork cache</button><button class="ghost" onclick="openConfigFolder()" data-i18n="Open config folder">Open config folder</button></div>
      <div class="row" style="margin-top:10px"><button class="ghost" onclick="checkForUpdate(true)" id="checkUpdateBtn" data-i18n="Check for updates">Check for updates</button></div>
      <div id="s_msg" class="muted" style="margin-top:10px"></div>
      </div>
      <div class="settingsgroup settingshealthgroup" data-settings-panel="health" hidden>
        <div class="colh" data-i18n="Source health">Source health</div>
        <div class="muted" style="margin-bottom:8px" data-i18n="Shows whether the external data sources responded last time they were used.">Shows whether the external data sources responded last time they were used.</div>
        <div id="sourceHealth" class="srchealth"></div>
        <div class="row" style="margin-top:10px"><button class="ghost" onclick="testSources(this)" id="testSrcBtn" data-i18n="Test all sources">Test all sources</button></div>
      </div>
      <div id="devSettings" class="settingsgroup hide" data-settings-panel="maintenance" hidden>
        <div class="colh" data-i18n="Developer tools">Developer tools</div>
        <div class="muted" data-i18n="Testing controls that clear temporary performance data.">Testing controls that clear temporary performance data.</div>
        <div class="row" style="margin-top:10px">
          <button class="ghost" onclick="resetColdStart(this)" data-i18n="Reset for cold-start test">Reset for cold-start test</button>
        </div>
      </div>
      </div>
      </div>
      <div class="settingsactions"><span id="s_refreshmsg" class="muted"></span><span class="muted" data-i18n="Changes are kept locally on this device.">Changes are kept locally on this device.</span><button class="push" onclick="saveSettings()" data-i18n="Save">Save changes</button></div>
    </div>
    </div>
  </section>
</main>
<div id="updateBanner" class="updatebanner hide">
  <span id="updateMsg"></span>
  <button onclick="doUpdateNow()" id="updateNowBtn" data-i18n="Update now">Update now</button>
  <button class="ghost" onclick="dismissUpdate()" data-i18n="Later">Later</button>
</div>
<div id="playerModal" class="pmodal hide" onclick="if(event.target===this)closePlayer()">
  <div class="pbox">
    <div class="pbar"><span id="pTitle" data-i18n="Player">Player</span><div class="tvplayeractions"><button type="button" class="tvminbtn" id="pMinBtn" title="Maximize player" aria-label="Maximize player" onclick="togglePopupPlayerSize()">&#8598;</button><button class="pclose" onclick="closePlayer()">&times;</button></div></div>
    <video id="pVideo" controls autoplay playsinline></video>
    <button type="button" class="tvvideohit" id="pVideoHit" aria-label="Maximize player" onclick="togglePopupPlayerSize()"></button>
    <div id="pMsg" class="muted" style="padding:8px 12px"></div>
  </div>
</div>
<script>
// --- pancake decorations (inline SVG, no assets) ---
const SVG_STACK='<svg viewBox="0 0 100 70" xmlns="http://www.w3.org/2000/svg"><ellipse cx="50" cy="52" rx="34" ry="9" fill="#e7a94e"/><ellipse cx="50" cy="51" rx="34" ry="9" fill="none" stroke="#b9762d" stroke-width="1.6"/><ellipse cx="50" cy="42" rx="32" ry="9" fill="#f0b95e"/><ellipse cx="50" cy="41" rx="32" ry="9" fill="none" stroke="#b9762d" stroke-width="1.6"/><ellipse cx="50" cy="32" rx="30" ry="9" fill="#f5c56e"/><ellipse cx="50" cy="31" rx="30" ry="9" fill="none" stroke="#b9762d" stroke-width="1.6"/><path d="M22 26 q6 10 12 3 q6 10 14 2 q6 10 14 2 q6 9 12 2 l0 5 q-6 5 -12 1 q-8 7 -14 0 q-8 7 -14 0 q-7 6 -12 -3 z" fill="#a8541f"/><rect x="38" y="12" width="24" height="11" rx="4" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.3"/></svg>';
const SVG_ONE='<svg viewBox="0 0 80 34" xmlns="http://www.w3.org/2000/svg"><ellipse cx="40" cy="20" rx="32" ry="10" fill="#f2bd63"/><ellipse cx="40" cy="19" rx="32" ry="10" fill="none" stroke="#b9762d" stroke-width="1.6"/><rect x="28" y="8" width="22" height="10" rx="4" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.3"/></svg>';
const SVG_TV='<svg viewBox="0 0 240 210" xmlns="http://www.w3.org/2000/svg"><rect x="26" y="58" width="150" height="120" rx="16" fill="#3a2c1f" stroke="#241a12" stroke-width="4"/><rect x="38" y="70" width="126" height="96" rx="8" fill="#1b3a6b"/><ellipse cx="101" cy="140" rx="44" ry="11" fill="#e7a94e"/><ellipse cx="101" cy="128" rx="42" ry="11" fill="#f0b95e"/><ellipse cx="101" cy="116" rx="40" ry="11" fill="#f5c56e"/><path d="M64 110 q6 12 14 4 q6 12 16 3 q7 12 16 3 q7 11 15 2 q6 10 12 3 l0 6 q-6 6 -12 2 q-8 8 -15 1 q-8 8 -16 1 q-8 8 -16 0 q-8 7 -14 -3 z" fill="#a8541f"/><rect x="86" y="86" width="30" height="14" rx="5" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.5"/><circle cx="192" cy="86" r="8" fill="#2a2a2a"/><circle cx="192" cy="112" r="8" fill="#2a2a2a"/><rect x="186" y="132" width="12" height="30" rx="3" fill="#2a2a2a"/><rect x="52" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="136" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="150" y="40" width="4" height="24" fill="#241a12"/><rect x="118" y="40" width="4" height="24" fill="#241a12" transform="rotate(-28 120 52)"/><circle cx="152" cy="38" r="6" fill="#f5c56e"/><circle cx="116" cy="34" r="6" fill="#f5c56e"/></svg>';
const _PROFILE_EMBLEMS={
  tvstack:SVG_TV,
  stack:'<svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="45" fill="#182744" stroke="#4f8cff" stroke-width="4"/><ellipse cx="50" cy="67" rx="29" ry="8" fill="#e7a94e"/><ellipse cx="50" cy="57" rx="27" ry="8" fill="#f0b95e"/><ellipse cx="50" cy="47" rx="25" ry="8" fill="#f5c56e"/><rect x="40" y="31" width="20" height="10" rx="4" fill="#ffd77a"/></svg>',
  shield:'<svg viewBox="0 0 100 100"><path d="M50 7 86 20v27c0 23-14 38-36 47C28 85 14 70 14 47V20z" fill="#17233d" stroke="#4f8cff" stroke-width="4"/><ellipse cx="50" cy="63" rx="23" ry="7" fill="#e7a94e"/><ellipse cx="50" cy="53" rx="21" ry="7" fill="#f5c56e"/><circle cx="50" cy="35" r="7" fill="#ffd77a"/></svg>',
  night:'<svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="44" fill="#111a2b" stroke="#68799e" stroke-width="3"/><path d="M68 21a28 28 0 1 0 9 51A34 34 0 0 1 68 21z" fill="#f1c76b"/><ellipse cx="45" cy="69" rx="22" ry="7" fill="#e7a94e"/><ellipse cx="45" cy="60" rx="20" ry="7" fill="#f5c56e"/></svg>',
  bolt:'<svg viewBox="0 0 100 100"><rect x="9" y="9" width="82" height="82" rx="22" fill="#17325c" stroke="#4f8cff" stroke-width="4"/><path d="m57 18-29 42h20l-6 24 30-43H52z" fill="#ffd469" stroke="#e3a93d" stroke-width="2"/></svg>'
};
function makePancakes(el,count){
  const kinds=[SVG_STACK,SVG_ONE,SVG_TV];
  const widths=[70,64,72];
  let html='';
  const n=count||7;
  for(let i=0;i<n;i++){
    const k=i%3;
    const w=widths[k]*(0.7+Math.random()*0.7);
    const top=4+Math.random()*90;
    const left=2+Math.random()*90;
    const dur=(9+Math.random()*8).toFixed(1);
    const delay=(-Math.random()*8).toFixed(1);
    html+='<div class="pcake" style="width:'+w.toFixed(0)+'px;top:'+top.toFixed(1)+'%;left:'+left.toFixed(1)+'%;animation-duration:'+dur+'s;animation-delay:'+delay+'s">'+kinds[k]+'</div>';
  }
  el.innerHTML=html;
}
let _backgroundStyle='float',_decorationsEnabled=true;
const _ASCII_TV='&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&#92; | /<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;.----------------.<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;__________&nbsp;&nbsp;&nbsp;| o<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;/&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&#92;&nbsp;&nbsp;| o<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;| [______] |&nbsp;&nbsp;| |<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;| (======) |&nbsp;&nbsp;|<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;| (======) |&nbsp;&nbsp;|<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;|&nbsp;&nbsp;&#92;____/&nbsp;&nbsp;|&nbsp;&nbsp;|<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&#92;__________/&nbsp;&nbsp;|<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&#39;----------------&#39;<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;||&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;||';
function applyBackgroundStyle(style){
  _backgroundStyle=['float','ascii','off'].includes(style)?style:'float';
  _decorationsEnabled=_backgroundStyle!=='off';
  const global=document.getElementById('globalDecor');
  const l=document.getElementById('pcakeL'),r=document.getElementById('pcakeR'),pl=document.getElementById('pcakePL');
  if(l)l.innerHTML='';if(r)r.innerHTML='';if(pl)pl.innerHTML='';
  if(!global)return;
  global.innerHTML='';global.classList.toggle('asciibg',_backgroundStyle==='ascii');
  if(_backgroundStyle==='off')return;
  if(_backgroundStyle==='ascii'){
    global.innerHTML='<pre class="asciimotif a1">'+_ASCII_TV+'</pre><pre class="asciimotif a2">'+_ASCII_TV+'</pre><pre class="asciimotif a3">'+_ASCII_TV+'</pre>';
    return;
  }
  makePancakes(global,18);
}
function initPancakes(){applyBackgroundStyle(_backgroundStyle);}
function initPlPancakes(){applyBackgroundStyle(_backgroundStyle);}
function setNav(id){['navChannels','navMylist','navMytimeline','navMytv','navMovies','navShows','navGames','navRacing','navTeams','navSettings'].forEach(function(n){const el=document.getElementById(n);if(el)el.classList.toggle('on',n===id);});}
const _SLOGANS={
  search:["Find the match. Pick a channel. Pour the syrup."],
  mytv:["A little syrup makes channel surfing sweeter.","Fixtures, flicks & fluffy stacks.","Streaming with suspicious amounts of syrup."],
  movies:["Movie night, now serving pancakes."],
  shows:["One more episode. One more pancake."],
  channels:["Putting the \u201Cpan\u201D in channel planning.","Plan your viewing. Prepare your pancakes."],
  settings:["Powered by pancakes and questionable decisions.","Built with code, football, and pancake batter."],
  mylist:["Curate your channels. Butter generously.","Pancakes on standby."]
};
function setSlogan(section){
  const el=document.getElementById('slogan');
  if(!el)return;
  const list=_SLOGANS[section]||[];
  if(!list.length){el.textContent='';return;}
  el.textContent=list[Math.floor(Math.random()*list.length)];
}
let _lang='en';
const _I18N={
  "Search":"Søk","Playlist Builder":"Lag spilleliste","Playlists":"Spillelister","Timeline":"Tidslinje","My List":"Min liste","My Profile":"Min profil","Edit Profile":"Rediger profil","My Timeline":"Min tidslinje","My TV":"Live TV","My Movies":"Mine filmer","My Shows":"Mine serier","My Games":"Mine spill","My Racing":"Min racing","My Teams":"Mine lag","Favorite Movies":"Favorittfilmer","Favorite Shows":"Favorittserier","Favorite Games":"Favorittspill","Favorite Teams":"Favorittlag","Settings":"Innstillinger","Stop TVMate":"Stopp TVMate",
  "Welcome to TVMate":"Velkommen til TVMate","Let's make it yours. Everything here can be changed later from Settings or Edit Profile.":"La oss gjøre TVMate til ditt. Alt her kan endres senere i Innstillinger eller Rediger profil.","Your name":"Navnet ditt","Pick an emblem":"Velg et emblem","Emblem":"Emblem",
  "Optional: add a favorite show or movie now, or let TVMate add demo items so you can see what My Profile looks like.":"Valgfritt: legg til en favorittserie eller film nå, eller la TVMate legge til demo-innhold så du kan se hvordan Min profil ser ut.","Optional: add a favorite show or movie now, or let TVMate add demo items so you can see what Profile looks like.":"Valgfritt: legg til en favorittserie eller film nå, eller la TVMate legge til demo-innhold så du kan se hvordan Profil ser ut.","If you don't add anything yet, TVMate will add a couple of demo items. They disappear permanently when you favorite your first real movie or show.":"Hvis du ikke legger til noe ennå, legger TVMate inn et par demo-elementer. De forsvinner permanent når du favorittmerker din første ekte film eller serie.",
  "How should TVMate open?":"Hvordan skal TVMate åpnes?","TVMate opens straight in your browser. Use Stop TVMate in the top-right when you want to shut the app down.":"TVMate åpnes rett i nettleseren. Bruk Stopp TVMate øverst til høyre når du vil avslutte appen.","Modern TVMate":"Moderne TVMate","TVMate opens straight in your browser with no CMD window.":"Tvmate åpnes rett i nettleseren","Bookmark TVMate":"Bokmerk TVMate","Press Ctrl+D to bookmark TVMate for an easy way back.":"Trykk Ctrl+D for å bokmerke TVMate, så finner du enkelt tilbake.","Copy address":"Kopier adresse","Stop TVMate after":"Stopp TVMate etter","Activity in TVMate resets the timer.":"Aktivitet i TVMate nullstiller tidsuret.",
  "You're ready":"Du er klar","TVMate is set up around what you follow. A streaming login is only needed for your own channels, movies and shows.":"TVMate er satt opp rundt det du følger. Strømmeinnlogging trengs bare for dine egne kanaler, filmer og serier.","Want playback too?":"Vil du også spille av?","Set up your Xtream login for your own channels, movies and shows.":"Sett opp Xtream-innlogging for dine egne kanaler, filmer og serier.","Set up Xtream":"Sett opp Xtream","All done":"Alt klart","Head straight to My Profile and start using TVMate.":"Gå rett til Min profil og begynn å bruke TVMate.","Head straight to Profile and start using TVMate.":"Gå rett til Profil og begynn å bruke TVMate.","Let's go, I'm ready":"Kjør på, jeg er klar",
  "Background style":"Bakgrunnsstil","Floating pancakes & TVs":"Flytende pannekaker og TV-er","Off":"Av","What do you want to follow?":"Hva vil du følge?","Movies and shows are always available. Turn the extra sections on or off here.":"Filmer og serier er alltid tilgjengelige. Slå ekstraseksjonene av eller på her.",
  "Football":"Fotball","Games":"Spill","Movies":"Filmer","Matchfinder, My Teams and fixtures":"Kampfinner, Mine lag og kamper","Matchfinder, Sports and fixtures":"Kampfinner, Sport og kamper","My Racing, schedules and followed drivers":"Min racing, terminlister og førere du følger","Racing schedules and followed drivers":"Racingterminlister og førere du følger","Steam wishlist and game releases":"Steam-ønskeliste og spillanseringer",
  "Choose your football teams":"Velg fotballagene dine","Pick the teams you want fixtures for. You can add or remove teams later in My Teams.":"Velg lagene du vil se kamper for. Du kan legge til eller fjerne lag senere i Mine lag.","Pick the teams you want fixtures for. You can add or remove teams later in Sports.":"Velg lagene du vil se kamper for. Du kan legge til eller fjerne lag senere under Sport.","Search for a team, e.g. Brann":"Søk etter et lag, f.eks. Brann",
  "Choose your racing":"Velg racing","Select the series you want on My Racing and your timeline.":"Velg seriene du vil ha i Min racing og på tidslinjen.","Select the series you want in Racing and your timeline.":"Velg seriene du vil ha i Racing og på tidslinjen.","Favorite Formula 1 team":"Favorittlag i Formel 1","Choose a Formula 1 team":"Velg et Formel 1-lag","Add something to watch":"Legg til noe å se på","Search shows":"Søk etter serier","Search movies":"Søk etter filmer",
  "Skip setup":"Hopp over oppsett","Back":"Tilbake","Next":"Neste","Run setup guide":"Kjør oppsettsveiviseren","Cancel":"Avbryt","Step":"Trinn","of":"av","Copied":"Kopiert","Copy this TVMate address:":"Kopier denne TVMate-adressen:",
  "Enter a profile name to continue.":"Skriv inn et profilnavn for å fortsette.","Enter a profile name.":"Skriv inn et profilnavn.","Profile saved.":"Profilen er lagret.","Could not save profile.":"Kunne ikke lagre profilen.","No favorite teams selected yet.":"Ingen favorittlag er valgt ennå.","Searching...":"Søker...","Add":"Legg til","No teams found.":"Fant ingen lag.","Could not search teams.":"Kunne ikke søke etter lag.","Favorite":"Favoritt","No results found.":"Fant ingen resultater.","Could not search.":"Kunne ikke søke.","Added":"Lagt til","Item":"Element","added to favorites.":"lagt til i favoritter.","Could not add favorite.":"Kunne ikke legge til favoritt.",
  "Live Matches":"Direktekamper","Today's Top Fixtures":"Dagens toppkamper","Upcoming Fixtures":"Kommende kamper","Show more matches":"Vis flere kamper","Show fewer matches":"Vis færre kamper","Search for a team...":"Søk etter et lag...","Find team or match":"Finn lag eller kamp","Refresh fixtures":"Oppdater kamper",
  "Find a match":"Finn en kamp","Search a team to find its fixtures, TV coverage and matching channels.":"Søk etter et lag for å finne kamper, TV-dekning og matchende kanaler.","Search for a team, then choose Find fixtures when you want Matchfinder and TV results.":"Søk etter et lag, og velg deretter Finn kamper når du vil bruke Kampfinner og se TV-resultater.","Find team":"Finn lag","Search channels":"Søk kanaler","Find fixtures":"Finn kamper","Refresh channel matches":"Oppdater kanaltreff","Refreshing channel matches...":"Oppdaterer kanaltreff...","Loading channel matches...":"Laster kanaltreff...","Channel matches refreshed.":"Kanaltreff er oppdatert.","Lower strictness only if a known channel is being missed.":"Senk treffnøyaktigheten bare hvis en kjent kanal ikke blir funnet.","Matches":"Kamper","Best team/event matches":"Beste lag-/arrangementstreff","Definite channel matches":"Sikre kanaltreff","Best match":"Beste treff","Show more channels":"Vis flere kanaler","Show fewer channels":"Vis færre kanaler","TV listed":"TV oppført","No TV":"Ingen TV","No matching channels":"Ingen matchende kanaler","channel":"kanal","channels":"kanaler",
  "Back to Sports":"Tilbake til Sport","No TV listings for this fixture.":"Ingen TV-oversikt for denne kampen.","Available channels":"Tilgjengelige kanaler","TV listings":"TV-oversikt","No channels in your list match this broadcaster.":"Ingen kanaler i listen din matcher denne TV-leverandøren.","Other TV providers":"Andre TV-leverandører",
  "Teams":"Lag","My Sports":"Min sport","Shows":"Serier","Show":"Serie","Sports":"Sport","Movie":"Film","Formula 1":"Formel 1","Racing":"Racing","Choose F1 team":"Velg F1-lag","Live TV":"Live TV","Find Channels":"Finn kanaler","Find Categories":"Finn kategorier","Choose channels":"Velg kanaler","Empty channel slot":"Tom kanalplass","Choose a team to see details.":"Velg et lag for å se detaljer.","Home ground":"Hjemmebane","Head coach":"Hovedtrener","League":"Liga","Country":"Land",
  "Choose up to four channels.":"Velg opptil fire kanaler.","Star channels first, then choose up to four here.":"Favorittmerk kanaler først, og velg deretter opptil fire her.",
  "Choose up to five channels.":"Velg opptil fem kanaler.","Star channels first, then choose up to five here.":"Favorittmerk kanaler først, og velg deretter opptil fem her.",
  "No favorite teams yet.":"Ingen favorittlag ennå.","No upcoming fixture found.":"Ingen kommende kamp funnet.","Could not load team fixtures.":"Kunne ikke laste lagkamper.",
  "No F1 team selected.":"Ingen F1-lag valgt.","Could not load Formula 1 calendar.":"Kunne ikke laste Formel 1-kalenderen.",
  "Nothing airing close to now from your favorite shows.":"Ingenting sendes nær nåtid fra favorittseriene dine.","Could not load your shows.":"Kunne ikke laste seriene dine.",
  "Airs in":"Sendes om","Released":"Utgitt","Releases":"Lanseres","Just released":"Nettopp utgitt","ago":"siden","Stream found in playlist":"Strøm funnet i spillelisten",
  "Live now":"Direkte nå","Next match":"Neste kamp","Next race":"Neste løp","No upcoming race found.":"Ingen kommende løp funnet.",
  "Choose a driver to see details.":"Velg en fører for å se detaljer.","Driver profile":"Førerprofil","Loading drivers and next race...":"Laster førere og neste løp...","Loading racing schedules...":"Laster racingterminlister...","Loading fixture...":"Laster kamp...","Loading next race...":"Laster neste løp...","Nothing happening around now.":"Ingenting skjer rundt nå.","Play":"Spill av","No upcoming events found.":"Ingen kommende arrangementer funnet.","Choose at least one racing series above.":"Velg minst én racingserie ovenfor.","Could not load racing schedules.":"Kunne ikke laste racingterminlistene.","Definite event matches":"Sikre arrangementstreff","Dedicated series channels":"Dedikerte seriekanaler","Possible channels by category":"Mulige kanaler etter kategori","Other possible channels":"Andre mulige kanaler",
  "Recently":"Nylig","Upcoming":"Kommende","Right now":"Akkurat nå",
  "Favorite Channels":"Favorittkanaler","EPG Refresh":"Oppdater EPG","Channels":"Kanaler",
  "All Categories":"Alle kategorier","Selected categories":"Valgte kategorier","Filter Channels":"Kanaler","Playlist":"Spilleliste",
  "Add to Favorites":"Legg til favoritter","Tick all":"Velg alle","Untick all":"Fjern alle","Add ticked":"Legg til valgte",
  "Make Playlist (Categories)":"Lag spilleliste (kategorier)","Make Playlist (Channels)":"Lag spilleliste (kanaler)","Clear":"Fjern","Reset":"Nullstill",
  "Ticked channels land here.":"Valgte kanaler vises her.",
  "Click a selected category to see its channels.":"Trykk på en kategori for å vise kanaler",
  "Tick categories on the left.":"Velg kategorier på venstre side.",
  "Favorite Categories":"Favorittkategorier","Categories":"Kategorier",
  "Matchfinder - Get Live / Next Match":"Kampfinner - Live / Neste kamp",
  "Match strictness":"Treffnøyaktighet",
  "Save":"Lagre","Reload channels":"Last inn kanaler","Test login":"Test innlogging",
  "Connection":"Tilkobling","Preferences":"Innstillinger","Search Options":"Søkealternativer","General":"Generelt","Content":"Innhold","Playback":"Avspilling","Maintenance":"Vedlikehold","Health":"Status","Content startup":"Innhold ved oppstart","Sports Search":"Sportssøk",
  "Personalize TVMate and choose what opens when the app starts.":"Tilpass TVMate og velg hva som åpnes når appen starter.","Checks your favorite series for newly available episodes after TVMate opens.":"Ser etter nylig tilgjengelige episoder i favorittseriene dine etter at TVMate åpnes.","Show or hide optional sections. Disabling one also skips it during external-content refreshes.":"Vis eller skjul valgfrie seksjoner. Deaktiverte seksjoner hoppes også over ved oppdatering av eksternt innhold.",
  "Choose which regional TV listings Sports Search uses to find broadcasters for matches.":"Velg hvilke regionale TV-oversikter Sportssøk bruker for å finne kanaler som viser kampene.","TV listings countries (comma separated, e.g. no, uk, us)":"Land for TV-oversikter (kommaseparert, f.eks. no, uk, us)","Enter country codes for the TV guides you want searched. Adjust match strictness from the Sports page.":"Skriv inn landskodene for TV-guidene du vil søke i. Juster treffnøyaktigheten på Sports-siden.",
  "Choose the stream URL format requested from your IPTV provider. TS is the normal default; use M3U8 if your provider works better with HLS.":"Velg strømformatet som forespørres fra IPTV-leverandøren. TS er vanlig standard; bruk M3U8 hvis leverandøren fungerer bedre med HLS.","Control automatic updates of local data and manage TVMate's local files.":"Styr automatiske oppdateringer av lokale data og administrer TVMates lokale filer.","Choose which content TVMate updates automatically after it opens.":"Velg hvilket innhold TVMate oppdaterer automatisk etter oppstart.","Refresh IPTV & EPG on startup":"Oppdater IPTV og EPG ved oppstart","Refresh sports, racing & games on startup":"Oppdater sport, racing og spill ved oppstart","Refresh Xtream channels, movies, shows and TV guide data.":"Oppdater Xtream-kanaler, filmer, serier og TV-guide.","Refresh matches, regional TV listings, racing and your Steam wishlist.":"Oppdater kamper, regionale TV-oversikter, racing og Steam-ønskelisten din.","Stops the local TVMate server after no interaction or playback. Active video keeps TVMate awake.":"Stopper den lokale TVMate-serveren når det ikke har vært aktivitet eller avspilling. Aktiv video holder TVMate våken.","Testing...":"Tester...","Login successful.":"Innloggingen fungerte.","Login failed.":"Innloggingen mislyktes.",
  "Profile":"Profil","Setup":"Oppsett","Profile name":"Profilnavn","Profile emblem":"Profilemblem","Backup & Import":"Sikkerhetskopi og import","Download a portable backup or merge one into this profile. Caches and artwork are never included.":"Last ned en flyttbar sikkerhetskopi eller slå en sammen med denne profilen. Mellomlager og omslagskunst tas aldri med.","Export profile":"Eksporter profil","Export full backup":"Eksporter full sikkerhetskopi","Import backup":"Importer sikkerhetskopi","Profile backup keeps credentials out. Full backup includes Xtream credentials; store it securely.":"Profilsikkerhetskopien utelater innlogging. Full sikkerhetskopi inkluderer Xtream-innlogging; oppbevar den sikkert.","Backup downloaded.":"Sikkerhetskopien er lastet ned.","Backup imported and merged.":"Sikkerhetskopien er importert og slått sammen.","Full backup restored.":"Full sikkerhetskopi er gjenopprettet.","Could not import this backup.":"Kunne ikke importere denne sikkerhetskopien.",
  "Balanced":"Balansert","Spotlight":"Fremhevet","Now Timeline":"Nå-tidslinje","Profile Hub":"Profiloversikt","Changes the arrangement of your Profile page only.":"Endrer bare oppsettet på profilsiden din.",
  "My List layout":"Min liste-oppsett","My Profile layout":"Min profil-oppsett","Now & Next":"Nå og neste",
  "Features & Display":"Funksjoner og visning","Show football features":"Vis fotballfunksjoner","Show Formula 1 features":"Vis Formel 1-funksjoner","Show racing features":"Vis racingfunksjoner","Show game features":"Vis spillfunksjoner","Animated background decorations":"Animerte bakgrunnsdekorasjoner","Choose the racing series you want to follow.":"Velg racingseriene du vil følge.",
  "Preferred language":"Foretrukket språk",
  "Profile layout":"Profiloppsett","Your everyday TVMate preferences. Run the setup guide to change what you follow.":"Dine vanlige TVMate-innstillinger. Kjør oppsettsveiviseren for å endre hva du følger.","Look for newly available episodes when TVMate starts.":"Se etter nylig tilgjengelige episoder når TVMate starter.","Refresh channels, movies, shows and episode data when TVMate starts.":"Oppdater kanaler, filmer, serier og episodedata når TVMate starter.",
  "Startup":"Oppstart","Your Xtream login stays in your local config.json and is only sent to your own provider.":"Xtream-innloggingen lagres lokalt i config.json og sendes bare til din egen leverandør.","Auto shutdown when inactive":"Avslutt automatisk ved inaktivitet","Keep running — uses approximately three crumbs and your calculator works harder":"Fortsett å kjøre — bruker omtrent tre smuler, og kalkulatoren din jobber hardere","Changes are kept locally on this device.":"Endringer lagres lokalt på denne enheten.",
  "Recently Added":"Nylig lagt til","See what else is new":"Se hva mer som er nytt","Discover Movies":"Oppdag filmer","Popular":"Populært","New Releases":"Nye utgivelser","Featured":"Fremhevet",
  "Check for new movies":"Se etter nye filmer",
  "Back to My Movies":"Tilbake til Mine filmer","Back to Movies":"Tilbake til Filmer",
  "Your Latest Episodes":"Dine nyeste episoder","See more latest episodes":"Se flere nyeste episoder",
  "Back to My Shows":"Tilbake til Mine serier","Back to Shows":"Tilbake til Serier",
  "Upcoming Episodes":"Kommende episoder","Airs":"Sendes",
  "Today":"i dag","Tomorrow":"i morgen","in":"om","day":"dag","days":"dager",
  "hour":"time","hours":"timer","minute":"minutt","minutes":"minutter",
  "Not available":"Ikke tilgjengelig",
  "Maintenance & Playback":"Vedlikehold og avspilling","Refresh all content":"Oppdater alt innhold",
  "Data & Refresh":"Data og oppdatering","Choose exactly which TVMate data should be updated.":"Velg nøyaktig hvilke TVMate-data som skal oppdateres.",
  "Refresh IPTV & EPG":"Oppdater IPTV og EPG","Refresh sports, racing & games":"Oppdater sport, racing og spill","Refresh everything":"Oppdater alt",
  "Startup refresh":"Oppdatering ved oppstart","IPTV & EPG":"IPTV og EPG","Other content":"Annet innhold","Everything":"Alt",
  "Check favorite shows on startup":"Se etter nye episoder i favorittserier ved oppstart",
  "Refresh all content on startup":"Oppdater alt innhold ved oppstart",
  "Artwork cache":"Mellomlagret omslagskunst","Clear artwork cache":"Tøm omslagskunst",
  "Reset for cold-start test":"Nullstill for kaldstarttest",
  "Developer tools":"Utviklerverktøy",
  "Testing controls that clear temporary performance data.":"Testverktøy som tømmer midlertidige ytelsesdata.",
  "Remove":"Fjern","Copy URL":"Kopier URL",
  "Match strictness (0.40–0.80)":"Treffnøyaktighet (0.40–0.80)",
  "Listings countries (comma separated: no, uk, us)":"Land for TV-guide (kommaseparert: no, uk, us)",
  "No channels here.":"Ingen kanaler her.","No program info":"Ingen programinfo",
  "channels":"kanaler","channels checked":"kanaler kontrollert","channels updated.":"kanaler oppdatert.",
  "One bulk guide download":"Én samlet guide-nedlasting","Downloading and processing the provider TV guide...":"Laster ned og behandler leverandørens TV-guide...",
  "Parsing programme information...":"Behandler programinformasjon...","Matching guide data to favorite channels...":"Kobler guidedata til favorittkanaler...",
  "Large provider guides may take a little while...":"Store guider fra leverandøren kan ta litt tid...","Fallback":"Reserveløsning",
  "Loading EPG...":"Laster EPG...","EPG loaded":"EPG lastet","EPG failed":"EPG feilet","Loading...":"Laster...",
  "Updated":"Oppdatert","No EPG":"Ingen EPG","Failed":"Feilet",
  "No favorites to load EPG for.":"Ingen favoritter å laste EPG for.",
  "Updating TV guide":"Oppdaterer TV-guide","Finding channels in your favorites...":"Finner kanaler i favorittene dine...",
  "Loading programme information...":"Laster programinformasjon...","TV guide is ready.":"TV-guiden er klar.","with programme data":"med programdata",
  "Compatibility mode: loading one channel at a time...":"Kompatibilitetsmodus: laster én kanal om gangen...",
  "Retrying this batch one channel at a time...":"Prøver denne gruppen på nytt, én kanal om gangen...","channels could not be refreshed.":"kanaler kunne ikke oppdateres.",
  "Channels available":"Kanaler tilgjengelig",
  "Update available":"Oppdatering tilgjengelig","you have":"du har","Downloading...":"Laster ned...",
  "Update downloaded. Restart now to finish updating?":"Oppdatering lastet ned. Start på nytt for å fullføre?",
  "Restart now":"Start på nytt","Update now":"Oppdater nå","Later":"Senere","Player":"Spiller","Restarting...":"Starter på nytt...",
  "Updating... this window will reload shortly.":"Oppdaterer... vinduet lastes inn på nytt snart.",
  "Update failed. Try again later.":"Oppdatering feilet. Prøv igjen senere.",
  "Restart failed. Please close and reopen the app.":"Omstart feilet. Lukk og åpne appen igjen.",
  "Update installed. Please close this window and open Olo’s TVMate again.":"Oppdatering installert. Lukk dette vinduet og åpne Olo’s TVMate igjen.",
  "Check for updates":"Se etter oppdateringer","Checking...":"Sjekker...",
  "You are on the latest version":"Du har den nyeste versjonen",
  "Could not check for updates. Check your internet connection.":"Kunne ikke sjekke for oppdateringer. Sjekk internettforbindelsen.",
  "Open config folder":"Åpne konfigurasjonsmappe","Could not open folder.":"Kunne ikke åpne mappen.",
  "Source health":"Kildestatus","Test all sources":"Test alle kilder","Testing sources...":"Tester kilder...",
  "Shows whether the external data sources responded last time they were used.":"Viser om de eksterne datakildene svarte sist de ble brukt.",
  "not checked yet":"ikke sjekket enda","Not configured":"Ikke konfigurert","just now":"akkurat nå","min ago":"min siden","h ago":"t siden","d ago":"d siden",
  "working":"fungerer","failed":"feilet","items":"elementer","No sources.":"Ingen kilder.","Could not test sources.":"Kunne ikke teste kilder.",
  "Host (e.g. http://example.com:8080)":"Vert (f.eks. http://example.com:777)",
  "Username":"Brukernavn","Password":"Passord","Stream extension":"Strøm-format",
  "Default start section":"Standard oppstartseksjon","Search a team, e.g. Leeds":"Søk etter lag, f.eks. Leeds",
  "Find a channel, e.g. tv2 play":"Finn en kanal, f.eks. tv2 play",
  "Search a category, e.g. Norway":"Søk kategori, f.eks. Norge","Filter categories...":"Filtrer kategorier...",
  "Search your movies...":"Søk i filmene dine...",
  "Search your shows...":"Søk i seriene dine...",
  "Search Steam games...":"Søk etter Steam-spill...","Steam wishlist URL...":"Steam-ønskeliste-URL...","Filter wishlist...":"Filtrer ønskeliste...","Sync wishlist":"Synkroniser ønskeliste","Refresh wishlist":"Oppdater ønskeliste","Wishlist settings":"Ønskelisteinnstillinger","Game":"Spill",
  "Check for new episodes":"Se etter nye episoder",
  "★ Add to Favorites":"★ Legg til favoritter","★ Favorite Channels":"★ Favorittkanaler"
};
function tr(s){ if(_lang==='no'&&_I18N[s])return _I18N[s]; return s; }
function applyLang(){
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    const key=el.getAttribute('data-i18n');
    el.textContent=tr(key);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(function(el){
    const key=el.getAttribute('data-i18n-ph');
    el.setAttribute('placeholder',tr(key));
  });
}
function setLang(l){
  _lang=l;
  document.getElementById('langEN').classList.toggle('on',l==='en');
  document.getElementById('langNO').classList.toggle('on',l==='no');
  applyLang();
  try{localStorage.setItem('tvmate_lang',l);}catch(e){}
}
function hideAll(keepMytv){
  // Live TV playback persists across sections. Capture the source state first,
  // then move the player only after the Live TV view has been hidden.
  const hasTvPlayback=(_tvPlaying!==null||window._tvPlaybackController);
  const popupPlayer=document.getElementById('playerModal');
  const hasPopupPlayback=!!(popupPlayer&&!popupPlayer.classList.contains('hide'));
  const hasPlayback=!!(hasTvPlayback||hasPopupPlayback);
  const leavingLiveTv=!!(!keepMytv&&hasTvPlayback&&!mytvView.classList.contains('hide'));
  settingsView.classList.add('hide');channelsView.classList.add('hide');mylistView.classList.add('hide');mytimelineView.classList.add('hide');mytvView.classList.add('hide');moviesView.classList.add('hide');showsView.classList.add('hide');gamesView.classList.add('hide');racingView.classList.add('hide');teamsView.classList.add('hide');updateProfileName(_profileConfig.profile_name);
  if(!keepMytv&&hasPlayback){
    if(leavingLiveTv)tvSetMini(true);
    document.body.classList.add('tvsectionplay');
  }else{
    document.body.classList.remove('tvsectionplay');
  }
}
let _historyReady=false,_historyRestoring=false,_historySection='';
function rememberLocation(section,extra){
  _historySection=section;
  if(!_historyReady||_historyRestoring)return;
  const state=Object.assign({tvmate:true,section:section},extra||{});
  history.pushState(state,'','#'+section);
}
function showMytv(){rememberLocation('mytv');hideAll(true);mytvView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navMytv');setSlogan('mytv');initMytv();}
function showMovies(){rememberLocation('movies');hideAll();moviesView.classList.remove('hide');document.getElementById('movieCatalogs').classList.remove('hide');document.getElementById('movieResults').innerHTML='';document.querySelector('main').classList.add('wide');setNav('navMovies');setSlogan('movies');loadMovieFavorites();loadRecentMovies();loadCinemetaMovies(_movieCatalog);}
function showShows(){rememberLocation('shows');_activeSeriesId=null;_showSeasons={};hideAll();showsView.classList.remove('hide');document.getElementById('latestEpisodesSection').classList.remove('hide');document.getElementById('showResults').innerHTML='';document.getElementById('showDetails').innerHTML='';document.querySelector('main').classList.add('wide');setNav('navShows');setSlogan('shows');loadShowFavorites();if(!_latestEpisodesLoaded)loadLatestEpisodes();}
function showGames(){if(!_gamesEnabled){showMylist();return;}rememberLocation('games');hideAll();gamesView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navGames');setSlogan('movies');loadGameFavorites();loadSteamWishlistSetting();}
function showRacing(driverKey){if(!_f1Enabled){showMylist();return;}if(driverKey)_racingDetailKey=String(driverKey);rememberLocation('racing');hideAll();racingView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navRacing');setSlogan('mylist');loadRacing();}
function showTeams(target){if(!_footballEnabled){showMylist();return;}rememberLocation('teams');hideAll();teamsView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navTeams');setSlogan('search');if(!target)clearSportsSearch();const loading=loadMyTeams();if(target)Promise.resolve(loading).finally(()=>openMyTeamsFixture(target));}
function showMylist(){rememberLocation('mylist');hideAll();mylistView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navMylist');setSlogan('mylist');if(_myListLoaded){renderMyListProfile();applyMyListLayout();renderMyListChannels();renderMyListTimeline();}else loadFavorites();}
function showMytimeline(){rememberLocation('mytimeline');hideAll();mytimelineView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navMytimeline');setSlogan('mylist');loadFavorites();}
// Backward compatibility for old bookmarks/configs that still point at Search.
// Search now lives inside Playlists.
function showSearch(){showChannels();}
function showChannels(){rememberLocation('channels');hideAll();channelsView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navChannels');setSlogan('channels');loadCategories();initPlPancakes();}
function openSportsChannelSearch(){showChannels();setTimeout(function(){const input=document.getElementById('cq');if(input)input.focus();},60);}
let _settingsTab='profile';
function setSettingsTab(tab){
  const migrated={content:'general',playback:'iptv'};
  tab=migrated[tab]||tab;
  const allowed=['profile','iptv','general','maintenance','health'];
  _settingsTab=allowed.includes(tab)?tab:'profile';
  document.querySelectorAll('#settingsView [data-settings-panel]').forEach(function(panel){panel.hidden=panel.dataset.settingsPanel!==_settingsTab||panel.classList.contains('hide');});
  document.querySelectorAll('#settingsView [data-settings-tab]').forEach(function(button){const active=button.dataset.settingsTab===_settingsTab;button.classList.toggle('on',active);button.setAttribute('aria-selected',String(active));});
  try{localStorage.setItem('tvmateSettingsTab',_settingsTab);}catch(e){}
  if(_settingsTab==='health')loadSourceHealth();
}
function showSettings(){rememberLocation('settings');loadSettings();hideAll();settingsView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navSettings');setSlogan('settings');let saved='profile';try{saved=localStorage.getItem('tvmateSettingsTab')||'profile';}catch(e){}setSettingsTab(saved);}
function updateProfileName(name){
  // Profile identity lives inside My Profile now. The permanent top-right
  // action is Stop TVMate, so profile names no longer occupy header space.
}
let _profileConfig={profile_name:'',profile_emblem:'tvstack',mylist_layout:'timeline',football_enabled:true,f1_enabled:true,racing_series:['f1'],games_enabled:true,decorations_enabled:true,background_style:'float'};
let _selectedEmblem='tvstack',_footballEnabled=true,_f1Enabled=true,_gamesEnabled=true,_myListLayout='timeline';
function profileEmblemSvg(key){return _PROFILE_EMBLEMS[key]||_PROFILE_EMBLEMS.tvstack;}
function renderEmblemPicker(){
  const el=document.getElementById('s_emblems');if(!el)return;
  el.innerHTML=Object.keys(_PROFILE_EMBLEMS).map(key=>'<button type="button" class="emblemchoice'+(key===_selectedEmblem?' on':'')+'" data-key="'+key+'" onclick="selectProfileEmblem(this.dataset.key)" title="'+key+'">'+profileEmblemSvg(key)+'</button>').join('');
}
function selectProfileEmblem(key){if(!_PROFILE_EMBLEMS[key])return;_selectedEmblem=key;renderEmblemPicker();}
let _setupIndex=0,_setupEmblem='tvstack',_setupFirstRun=false,_setupTeams=[],_setupInitialTeams=[],_setupRacingSeries=new Set(['f1']),_setupF1Team=null,_setupContentResults={show:[],movie:[]},_setupDemoContent=false;
function renderSetupEmblems(){
  const el=document.getElementById('setupEmblems');if(!el)return;
  el.innerHTML=Object.keys(_PROFILE_EMBLEMS).map(key=>'<button type="button" class="emblemchoice'+(key===_setupEmblem?' on':'')+'" data-key="'+key+'" onclick="selectSetupEmblem(this.dataset.key)" title="'+key+'">'+profileEmblemSvg(key)+'</button>').join('');
  const brand=document.getElementById('setupBrandEmblem');if(brand)brand.innerHTML=profileEmblemSvg(_setupEmblem);
}
function selectSetupEmblem(key){if(!_PROFILE_EMBLEMS[key])return;_setupEmblem=key;renderSetupEmblems();}
function setupStepKeys(){
  const keys=['profile','follow'];if(setupFootball.checked)keys.push('football');if(setupRacing.checked)keys.push('racing');keys.push('content','launch','finish');return keys;
}
function renderSetupStep(){
  const keys=setupStepKeys();_setupIndex=Math.max(0,Math.min(keys.length-1,_setupIndex));const active=keys[_setupIndex];
  document.querySelectorAll('.setupstep').forEach(el=>el.classList.toggle('hide',el.dataset.key!==active));
  const total=keys.length,progress=document.getElementById('setupProgress');
  if(progress)progress.innerHTML=Array.from({length:total},(_,i)=>'<span class="setupdot'+(i<=_setupIndex?' on':'')+'"></span>').join('');
  const meta=document.getElementById('setupStepMeta');if(meta)meta.textContent=tr('Step')+' '+(_setupIndex+1)+' '+tr('of')+' '+total;
  setupBack.classList.toggle('hide',_setupIndex===0);setupNext.classList.toggle('hide',active==='finish');
  setupSkip.classList.toggle('hide',!_setupFirstRun||_setupIndex===0);
  if(active==='football')renderSetupTeams();if(active==='racing')renderSetupRacing();if(active==='launch')renderSetupLaunchHelp();
}
function setupLocalAddress(){return location.protocol+'//localhost'+(location.port?':'+location.port:'');}
function renderSetupLaunchHelp(){const url=document.getElementById('setupLocalUrl');if(url)url.textContent=setupLocalAddress();}
async function copySetupLocalUrl(btn){const value=setupLocalAddress();try{await navigator.clipboard.writeText(value);const old=btn.textContent;btn.textContent=tr('Copied');setTimeout(()=>btn.textContent=old,1200);}catch(e){prompt(tr('Copy this TVMate address:'),value);}}
function setupStep(delta){
  const keys=setupStepKeys(),active=keys[_setupIndex];
  if(delta>0&&active==='profile'&&!setupName.value.trim()){
    setupName.focus();toast(tr('Enter a profile name to continue.'));return;
  }
  _setupIndex=Math.max(0,Math.min(keys.length-1,_setupIndex+delta));renderSetupStep();
}
async function openProfileSetup(firstRun,cfg){
  _setupFirstRun=!!firstRun;_setupIndex=0;
  let c=cfg||null;try{if(!c)c=await api('/api/config');}catch(e){c={};}
  c=c||{};setupName.value=c.profile_name||'';setupLang.value=c.preferred_language||'en';setupBackground.value=['float','ascii','off'].includes(c.background_style)?c.background_style:(c.decorations_enabled===false?'off':'float');
  _setupEmblem=_PROFILE_EMBLEMS[c.profile_emblem]?c.profile_emblem:'tvstack';renderSetupEmblems();
  setupFootball.checked=c.football_enabled!==false;setupRacing.checked=c.f1_enabled!==false;setupGames.checked=c.games_enabled!==false;_setupDemoContent=_setupFirstRun?true:!!c.setup_demo_content;
  setupAutoShutdown.value=String(c.auto_shutdown_minutes||0);
  _setupRacingSeries=new Set(c.racing_series||['f1']);_setupTeams=[];_setupInitialTeams=[];_setupF1Team=null;_setupContentResults={show:[],movie:[]};
  setupTeamResults.innerHTML='';setupShowResults.innerHTML='';setupMovieResults.innerHTML='';
  try{const fav=await api('/api/favorites');_setupTeams=(fav.teams||[]).map(t=>typeof t==='string'?{name:t,team_id:''}:{name:t.name||'',team_id:String(t.team_id||'')}).filter(t=>t.name);_setupInitialTeams=_setupTeams.map(t=>Object.assign({},t));_setupF1Team=(fav.f1_teams||[])[0]||null;if((fav.shows||[]).length||(fav.movies||[]).length)_setupDemoContent=false;}catch(e){}
  profileSetupOverlay.classList.remove('hide');renderSetupStep();setTimeout(()=>setupName.focus(),50);
}
function closeProfileSetup(){profileSetupOverlay.classList.add('hide');}
async function skipProfileSetup(){
  const body={profile_name:setupName.value.trim(),preferred_language:setupLang.value,profile_emblem:_setupEmblem,background_style:setupBackground.value,decorations_enabled:setupBackground.value!=='off',setup_complete:true};
  if(!body.profile_name){_setupIndex=0;renderSetupStep();setupName.focus();toast(tr('Enter a profile name to continue.'));return;}
  try{
    const r=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok)throw new Error('save failed');setLang(body.preferred_language);applyProfileConfig(body);closeProfileSetup();toast(tr('Profile saved.'));
  }catch(e){toast('Could not save profile.');}
}
function renderSetupTeams(){
  setupTeamSelected.innerHTML=_setupTeams.length?_setupTeams.map((t,i)=>'<span class="setupchip">'+esc(t.name)+' <button type="button" class="ghost" style="padding:1px 5px" data-i="'+i+'" onclick="setupRemoveTeam(Number(this.dataset.i))">&times;</button></span>').join(''):'<span class="muted">'+esc(tr('No favorite teams selected yet.'))+'</span>';
}
function setupRemoveTeam(i){_setupTeams.splice(i,1);renderSetupTeams();}
async function setupSearchTeams(){
  const q=setupTeamQuery.value.trim();if(!q){setupTeamResults.innerHTML='';return;}setupTeamResults.innerHTML='<span class="muted">'+esc(tr('Searching...'))+'</span>';
  try{const r=await api('/api/team_search?q='+encodeURIComponent(q));setupTeamResults.innerHTML=(r.teams||[]).map((t,i)=>{const team=typeof t==='string'?{name:t,team_id:''}:t;return '<div class="setupresult"><div class="grow"><b>'+esc(team.name||'')+'</b></div><button type="button" class="ghost" data-i="'+i+'" onclick="setupAddTeamFromResult(Number(this.dataset.i))">'+esc(tr('Add'))+'</button></div>';}).join('')||'<span class="muted">'+esc(tr('No teams found.'))+'</span>';_setupTeamSearchRows=r.teams||[];}catch(e){setupTeamResults.innerHTML='<span class="err">'+esc(tr('Could not search teams.'))+'</span>';}
}
let _setupTeamSearchRows=[];
function setupAddTeamFromResult(i){const raw=_setupTeamSearchRows[i];if(!raw)return;const t=typeof raw==='string'?{name:raw,team_id:''}:{name:raw.name||'',team_id:String(raw.team_id||'')};if(t.name&&!_setupTeams.some(x=>x.name.toLowerCase()===t.name.toLowerCase()))_setupTeams.push(t);renderSetupTeams();}
function renderSetupRacing(){
  setupRacingSeries.innerHTML=_RACING_SERIES.map(row=>'<button type="button" class="setupchoice'+(_setupRacingSeries.has(row[0])?' on':'')+'" data-key="'+row[0]+'" onclick="setupToggleRacing(this.dataset.key)">'+esc(row[1])+'</button>').join('');
  setupF1TeamWrap.classList.toggle('hide',!_setupRacingSeries.has('f1'));if(_setupRacingSeries.has('f1'))loadSetupF1Teams();
}
function setupToggleRacing(key){if(_setupRacingSeries.has(key))_setupRacingSeries.delete(key);else _setupRacingSeries.add(key);renderSetupRacing();}
async function loadSetupF1Teams(){
  if(setupF1TeamSelect.dataset.loaded==='1'){setupF1TeamSelect.value=String((_setupF1Team||{}).id||'');return;}
  try{const r=await api('/api/f1_teams');setupF1TeamSelect.innerHTML='<option value="">'+esc(tr('Choose a Formula 1 team'))+'</option>'+(r.teams||[]).map(t=>'<option value="'+escAttr(String(t.id||''))+'" data-name="'+escAttr(t.name||'')+'">'+esc(t.name||'')+'</option>').join('');setupF1TeamSelect.dataset.loaded='1';setupF1TeamSelect.value=String((_setupF1Team||{}).id||'');}catch(e){}
}
function setupSelectF1Team(id){const opt=Array.from(setupF1TeamSelect.options).find(o=>o.value===id);_setupF1Team=id?{id:id,name:(opt&&opt.dataset.name)||((opt&&opt.textContent)||'')}:null;}
async function setupSearchContent(kind){
  const isShow=kind==='show',input=isShow?setupShowQuery:setupMovieQuery,out=isShow?setupShowResults:setupMovieResults,q=input.value.trim();if(!q){out.innerHTML='';return;}out.innerHTML='<span class="muted">'+esc(tr('Searching...'))+'</span>';
  try{const r=await api((isShow?'/api/shows?q=':'/api/movies?q=')+encodeURIComponent(q)),rows=isShow?(r.shows||[]):(r.movies||[]);_setupContentResults[kind]=rows;out.innerHTML=rows.slice(0,8).map((item,i)=>'<div class="setupresult">'+(item.cover?'<img src="'+escAttr(item.cover)+'" alt="" onerror="this.remove()">':'')+'<div class="grow"><b>'+esc(item.name||'')+'</b><div class="muted">'+esc(item.year||'')+'</div></div><button type="button" class="ghost" data-kind="'+kind+'" data-i="'+i+'" onclick="setupFavoriteContent(this.dataset.kind,Number(this.dataset.i),this)">'+esc(tr('Favorite'))+'</button></div>').join('')||'<span class="muted">'+esc(tr('No results found.'))+'</span>';}catch(e){out.innerHTML='<span class="err">'+esc(tr('Could not search.'))+'</span>';}
}
async function setupFavoriteContent(kind,i,btn){
  const item=(_setupContentResults[kind]||[])[i];if(!item)return;btn.disabled=true;
  try{if(kind==='show')await favPost({action:'toggle_show',show:item});else await favPost({action:'toggle_movie',movie:item});_setupDemoContent=false;btn.textContent=tr('Added');toast((item.name||tr('Item'))+' '+tr('added to favorites.'));}catch(e){btn.disabled=false;toast(tr('Could not add favorite.'));}
}
async function syncSetupTeams(){
  const before=new Map(_setupInitialTeams.map(t=>[t.name.toLowerCase(),t])),after=new Map(_setupTeams.map(t=>[t.name.toLowerCase(),t]));
  for(const [key,t] of before)if(!after.has(key))await favPost({action:'toggle_team',team:t});
  for(const [key,t] of after)if(!before.has(key))await favPost({action:'toggle_team',team:t});
}
async function finishProfileSetup(btn,openXtream){
  const body={profile_name:setupName.value.trim(),preferred_language:setupLang.value,profile_emblem:_setupEmblem,background_style:setupBackground.value,decorations_enabled:setupBackground.value!=='off',
    football_enabled:setupFootball.checked,f1_enabled:setupRacing.checked,games_enabled:setupGames.checked,setup_demo_content:_setupDemoContent,hide_cmd_window:true,auto_shutdown_minutes:Number(setupAutoShutdown.value||0),setup_complete:true};
  if(!body.profile_name){_setupIndex=0;renderSetupStep();setupName.focus();toast(tr('Enter a profile name to continue.'));return;}
  btn.disabled=true;btn.textContent='Saving...';
  try{
    const r=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error('save failed');
    if(body.football_enabled)await syncSetupTeams();
    if(body.f1_enabled){await api('/api/racing_series',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({series:Array.from(_setupRacingSeries)})});if(_setupRacingSeries.has('f1'))await favPost({action:'set_f1_team',team:_setupF1Team||{}});}
    setLang(body.preferred_language);applyProfileConfig(Object.assign({},body,{racing_series:Array.from(_setupRacingSeries)}));closeProfileSetup();toast(tr('Profile saved.'));
    if(openXtream){showSettings();setSettingsTab('iptv');}else showMylist();
  }catch(e){toast('Could not save profile.');}
  btn.disabled=false;btn.textContent=openXtream?'Set up Xtream':"Let's go, I'm ready";
}
function renderMyListProfile(){
  const el=document.getElementById('myListProfile');if(!el)return;
  const name=String(_profileConfig.profile_name||'').trim()||tr('Profile');
  el.innerHTML='<div class="mylistprofileemblem">'+profileEmblemSvg(_profileConfig.profile_emblem)+'</div><div class="mylistprofilename">'+esc(name)+'</div><button type="button" class="ghost editprofilebtn" onclick="openEditProfile()" data-i18n="Edit Profile">'+esc(tr('Edit Profile'))+'</button>';
}
let _editProfileEmblem='tvstack';
function renderEditProfileEmblems(){const el=document.getElementById('ep_emblems');if(!el)return;el.innerHTML=Object.keys(_PROFILE_EMBLEMS).map(key=>'<button type="button" class="emblemchoice'+(key===_editProfileEmblem?' on':'')+'" data-key="'+key+'" onclick="selectEditProfileEmblem(this.dataset.key)" title="'+key+'">'+profileEmblemSvg(key)+'</button>').join('');}
function selectEditProfileEmblem(key){if(!_PROFILE_EMBLEMS[key])return;_editProfileEmblem=key;renderEditProfileEmblems();}
async function openEditProfile(){
  let c={};try{c=await api('/api/config');}catch(e){c=_profileConfig||{};}
  ep_name.value=c.profile_name||'';ep_lang.value=c.preferred_language||'en';_editProfileEmblem=_PROFILE_EMBLEMS[c.profile_emblem]?c.profile_emblem:'tvstack';renderEditProfileEmblems();
  ep_start.value=c.start_section||'mylist';ep_layout.value=['timeline','balanced','spotlight','hub'].includes(c.mylist_layout)?c.mylist_layout:'timeline';ep_checkshows.checked=!!c.check_shows_on_startup;ep_refreshiptv.checked=!!c.refresh_iptv_on_startup;ep_refreshsports.checked=!!c.refresh_sports_on_startup;ep_background.value=['float','ascii','off'].includes(c.background_style)?c.background_style:(c.decorations_enabled===false?'off':'float');
  editProfileOverlay.classList.remove('hide');setTimeout(()=>ep_name.focus(),30);
}
function closeEditProfile(){editProfileOverlay.classList.add('hide');}
function runSetupGuideFromProfile(){closeEditProfile();openProfileSetup(false);}
async function saveEditProfile(btn){
  const body={profile_name:ep_name.value.trim(),preferred_language:ep_lang.value,profile_emblem:_editProfileEmblem,start_section:ep_start.value,mylist_layout:ep_layout.value,check_shows_on_startup:ep_checkshows.checked,refresh_iptv_on_startup:ep_refreshiptv.checked,refresh_sports_on_startup:ep_refreshsports.checked,background_style:ep_background.value,decorations_enabled:ep_background.value!=='off'};
  if(!body.profile_name){ep_name.focus();toast(tr('Enter a profile name.'));return;}if(body.mylist_layout==='timeline'&&body.start_section==='mytimeline')body.start_section='mylist';if(!_gamesEnabled&&body.start_section==='games')body.start_section='mylist';if(!_f1Enabled&&body.start_section==='racing')body.start_section='mylist';if(!_footballEnabled&&body.start_section==='teams')body.start_section='mylist';
  const old=btn.textContent;btn.disabled=true;btn.textContent='Saving...';
  try{const r=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error('save failed');setLang(body.preferred_language);applyProfileConfig(body);closeEditProfile();toast(tr('Profile saved.'));}catch(e){toast(tr('Could not save profile.'));}
  btn.disabled=false;btn.textContent=old;
}
function applyMyListLayout(){
  const dash=document.querySelector('#mylistView .mydash');if(!dash)return;
  const allowed=['balanced','spotlight','timeline','hub'];
  _myListLayout=allowed.includes(_profileConfig.mylist_layout)?_profileConfig.mylist_layout:'timeline';
  dash.classList.remove('layout-balanced','layout-spotlight','layout-timeline','layout-hub');
  dash.classList.add('layout-'+_myListLayout);
  const timeline=document.getElementById('myListTimelineBlock');
  const shows=document.getElementById('myListShowsBlock'),teams=document.getElementById('myListTeamsBlock');
  if(timeline)timeline.classList.toggle('hide',_myListLayout!=='timeline');
  if(shows)shows.classList.toggle('hide',_myListLayout==='timeline');
  if(teams)teams.classList.toggle('hide',!(_footballEnabled||_f1Enabled));
  const timelineNav=document.getElementById('navMytimeline'),timelineStart=document.getElementById('startTimelineOption');
  if(timelineNav)timelineNav.classList.toggle('hide',_myListLayout==='timeline');
  if(timelineStart)timelineStart.classList.toggle('hide',_myListLayout==='timeline');
  renderMyListTimeline();
}
function applyProfileConfig(c){
  const prevFootball=_footballEnabled,prevRacing=_f1Enabled,prevGames=_gamesEnabled;
  _profileConfig=Object.assign({},_profileConfig,c||{});
  _selectedEmblem=_PROFILE_EMBLEMS[_profileConfig.profile_emblem]?_profileConfig.profile_emblem:'tvstack';
  _myListLayout=['balanced','spotlight','timeline','hub'].includes(_profileConfig.mylist_layout)?_profileConfig.mylist_layout:'timeline';
  _footballEnabled=_profileConfig.football_enabled!==false;
  _f1Enabled=_profileConfig.f1_enabled!==false;
  _gamesEnabled=_profileConfig.games_enabled!==false;
  const featureChanged=prevFootball!==_footballEnabled||prevRacing!==_f1Enabled||prevGames!==_gamesEnabled;
  if(featureChanged)_myListLoaded=false;
  if(!_footballEnabled)_myListTeamMoments=[];
  if(!_f1Enabled){_myListF1Moments=[];_myListRacingDrivers=[];}
  if(!_gamesEnabled)_myListGameMoments=[];
  const nav=document.getElementById('navTeams'),teamBlock=document.getElementById('myListTeamsBlock'),sportHeading=document.getElementById('myListSportHeading'),gamesNav=document.getElementById('navGames'),gamesStart=document.getElementById('startGamesOption'),racingNav=document.getElementById('navRacing'),racingStart=document.getElementById('startRacingOption');
  if(nav)nav.classList.toggle('hide',!_footballEnabled);
  if(teamBlock)teamBlock.classList.toggle('hide',!(_footballEnabled||_f1Enabled));
  if(sportHeading)sportHeading.classList.toggle('hide',!_footballEnabled);
  if(gamesNav)gamesNav.classList.toggle('hide',!_gamesEnabled);
  if(gamesStart)gamesStart.classList.toggle('hide',!_gamesEnabled);
  if(racingNav)racingNav.classList.toggle('hide',!_f1Enabled);
  if(racingStart)racingStart.classList.toggle('hide',!_f1Enabled);

  document.querySelectorAll('.f1Feature').forEach(el=>el.classList.toggle('hide',!_f1Enabled));
  updateProfileName(_profileConfig.profile_name);
  applyBackgroundStyle(_profileConfig.background_style||(_profileConfig.decorations_enabled===false?'off':'float'));
  renderMyListProfile();
  applyMyListLayout();
}

let _favTeamSet=new Set(),_favTeamRows=[],_myTeamFixtures=[],_sportsVisibleFixtures=[],_sportsAvailabilityTimer=null,_selectedTeamName='',_selectedTeamRow=null,_selectedTeamProfile=null,_teamProfileReq=0,_teamDeepLink=null,_fixtureSearchTeamId='';
function sportsFixtureKey(f){return [String(f.home||'').trim().toLowerCase(),String(f.away||'').trim().toLowerCase(),String(f.start||'').slice(0,16)].join('|');}
function favoriteTeamRow(t){return {name:String(typeof t==='string'?t:(t.name||'')),team_id:String(typeof t==='string'?'':(t.team_id||'')),logo:String(typeof t==='string'?'':(t.logo||''))};}
function renderTeamFavoriteRail(){
  const rail=document.getElementById('teamFavList');if(!rail)return;
  if(!_favTeamRows.length){rail.innerHTML='<span class="muted">'+esc(tr('No favorite teams yet.'))+'</span>';return;}
  rail.innerHTML=_favTeamRows.map(t=>{const src=t.logo||(t.team_id?'/api/team_logo?id='+encodeURIComponent(t.team_id):''),selected=String(t.name).toLowerCase()===String(_selectedTeamName).toLowerCase();return '<div class="teamfavitem'+(selected?' selected':'')+'" data-team-search="'+escAttr(t.name)+'" data-team-id="'+escAttr(t.team_id)+'" data-team-logo="'+escAttr(t.logo)+'">'+(src?'<img class="teamfavlogo" src="'+escAttr(src)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'<span class="teamfavname">'+esc(t.name)+'</span><span class="favstar on teamremove" data-team-name="'+escAttr(t.name)+'" title="Remove from favorites">&#9733;</span></div>';}).join('');
}
function selectedTeamNextFixture(){
  const name=String(_selectedTeamName||'');if(!name)return null;const now=Date.now();
  return _myTeamFixtures.filter(f=>{const belongs=(f.favorite_teams||[]).some(owner=>_teamNamesEquivalentForUi(owner,name))||_teamNamesEquivalentForUi(f.home,name)||_teamNamesEquivalentForUi(f.away,name);if(!belongs)return false;const ts=f.start?new Date(f.start).getTime():0;return f.is_live||(ts&&ts>now-3*3600000);}).sort((a,b)=>{if(!!a.is_live!==!!b.is_live)return a.is_live?-1:1;return new Date(a.start||0)-new Date(b.start||0);})[0]||null;
}
function renderSelectedTeamProfile(profile){
  const el=document.getElementById('teamProfileDetail');if(!el)return;const row=_favTeamRows.find(t=>String(t.name).toLowerCase()===String(_selectedTeamName).toLowerCase())||(_selectedTeamRow&&String(_selectedTeamRow.name).toLowerCase()===String(_selectedTeamName).toLowerCase()?_selectedTeamRow:null);if(!row){el.innerHTML='<span class="muted">'+esc(tr('Choose a team to see details.'))+'</span>';return;}
  profile=profile||_selectedTeamProfile||{};const src=profile.logo||row.logo||(row.team_id?'/api/team_logo?id='+encodeURIComponent(row.team_id):''),meta=[profile.country,profile.league].filter(Boolean).join(' · ');
  const facts=[['Home ground',profile.stadium||'—'],['Head coach',profile.coach||'—'],['League',profile.league||'—'],['Country',profile.country||'—']];
  const next=selectedTeamNextFixture();let nextHtml='<div class="teamprofilenext"><div class="teamprofilenextlabel">'+esc(tr('Next match'))+'</div><span class="muted">'+esc(tr('No upcoming fixture found.'))+'</span></div>';
  if(next){const kick=next.start?new Date(next.start):null,when=next.is_live?tr('Live now'):(kick&&!Number.isNaN(kick.getTime())?kick.toLocaleString(_lang==='no'?'nb-NO':undefined,{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}):'');nextHtml='<div class="teamprofilenext"><div class="teamprofilenextlabel">'+esc(next.is_live?tr('Live now'):tr('Next match'))+'</div><b>'+esc(next.home||'')+' v '+esc(next.away||'')+'</b><span class="muted">'+esc(when)+(next.league_name?' · '+esc(next.league_name):'')+'</span></div>';}
  el.innerHTML='<div class="teamprofilehero"><div class="teamprofilebadge">'+(src?'<img src="'+escAttr(src)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'</div><div class="teamprofileidentity"><h2>'+esc(profile.name||row.name)+'</h2><div class="muted">'+esc(meta||tr('Football'))+'</div></div></div><div class="teamprofilefacts">'+facts.map(f=>'<div class="teamprofilefact"><span>'+esc(tr(f[0]))+'</span><b title="'+escAttr(f[1])+'">'+esc(f[1])+'</b></div>').join('')+'</div>'+nextHtml;
}
async function loadSelectedTeamProfile(row){
  row=row||_favTeamRows.find(t=>String(t.name).toLowerCase()===String(_selectedTeamName).toLowerCase());if(!row)return;const req=++_teamProfileReq;_selectedTeamProfile={name:row.name,team_id:row.team_id,logo:row.logo};renderSelectedTeamProfile(_selectedTeamProfile);
  try{const r=await api('/api/team_profile?id='+encodeURIComponent(row.team_id||'')+'&name='+encodeURIComponent(row.name));if(req!==_teamProfileReq)return;_selectedTeamProfile=Object.assign({},r.profile||{}, {logo:(r.profile||{}).logo||row.logo});renderSelectedTeamProfile(_selectedTeamProfile);}catch(e){}
}
function selectMyTeam(name,teamId,logo){
  // Selecting a team is navigation, not a Matchfinder search. Keep the two
  // actions separate so browsing favorite/team-result cards stays instant and
  // does not unexpectedly replace the explicit search results below.
  _selectedTeamName=String(name||'');const row=_favTeamRows.find(t=>String(t.name).toLowerCase()===_selectedTeamName.toLowerCase())||{name:_selectedTeamName,team_id:String(teamId||''),logo:String(logo||'')};_selectedTeamRow=row;renderTeamFavoriteRail();loadSelectedTeamProfile(row);
}
function teamFixtureCard(f,live,deepLink){
  const kick=f.start?new Date(f.start):null;
  const when=kick&&!Number.isNaN(kick.getTime())?kick.toLocaleString(_lang==='no'?'nb-NO':undefined,{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}):'';
  let status='';
  if(live&&kick){
    const estimated=Math.max(0,Math.floor((Date.now()-kick.getTime())/60000));
    const hasClock=f.live_minute!==null&&f.live_minute!==undefined&&Number.isFinite(Number(f.live_minute));
    const mins=hasClock?Number(f.live_minute):estimated;
    status='<span class="live">&#9679; LIVE '+mins+' min</span>';
  }
  const owners=(f.favorite_teams||[]).join(', ');
  const query=(f.home||'')+' '+(f.away||''),matchQuery=(f.favorite_teams||[])[0]||f.home||f.away||'';
  const homeLogo=f.home_id?'<img class="teamfixturelogo" src="/api/team_logo?id='+encodeURIComponent(f.home_id)+'" alt="" loading="lazy" onerror="this.remove()">':'';
  const awayLogo=f.away_id?'<img class="teamfixturelogo" src="/api/team_logo?id='+encodeURIComponent(f.away_id)+'" alt="" loading="lazy" onerror="this.remove()">':'';
  const competition=f.league_name?'<div class="teamfixturecompetition">'+esc(f.league_name)+'</div>':'';
  const knownChannels=[...(f.matches||[]),...(f.ppv_hits||[])],hasChannels=knownChannels.length>0;
  const channelHtml=(hasChannels||f.availability_checked)?fixtureStoredChannelsHtml(Object.assign({logged_in:true},f)):'<span class="muted">'+esc(tr('Checking your channels...'))+'</span>';
  const details='<div class="teamfixturebroadcasts hide"><div class="fixturechannelresults" style="width:100%">'+channelHtml+'</div></div>';
  const fixtureAttrs=' data-fixture-card="1"'+(deepLink?' data-profile-fixture="1"':'')+' data-event-key="'+escAttr(sportsFixtureKey(f))+'" data-home="'+escAttr(f.home||'')+'" data-away="'+escAttr(f.away||'')+'" data-start="'+escAttr(f.start||'')+'" data-search="'+escAttr(matchQuery)+'"';
  return '<div class="teamfixture hastv'+(live?' livefixture':'')+(hasChannels?' haschannels':'')+'"'+fixtureAttrs+'><div class="teamfixtureteams"><span class="teamfixtureside">'+homeLogo+esc(f.home)+'</span><span class="teamfixturevs">v</span><span class="teamfixtureside">'+awayLogo+esc(f.away)+'</span>'
    +(hasChannels?'<span class="cc teamfixturetv">TV</span>':'')+'</div>'
    +competition+'<div class="muted">'+esc(when)+' '+status+'</div>'+(owners?'<div class="teamfixtureowner">'+esc(owners)+'</div>':'')+details+'</div>';
}
async function loadMyTeams(){
  const fav=await api('/api/favorites'), teams=fav.teams||[];
  _favTeamSet=new Set(teams.map(t=>String(typeof t==='string'?t:t.name).toLowerCase()));
  _favTeamRows=teams.map(favoriteTeamRow).filter(t=>t.name);
  if(_favTeamRows.length&&!_favTeamRows.some(t=>String(t.name).toLowerCase()===String(_selectedTeamName).toLowerCase()))_selectedTeamName=_favTeamRows[0].name;
  if(!_favTeamRows.length)_selectedTeamName='';
  renderTeamFavoriteRail();
  const selectedRow=_favTeamRows.find(t=>String(t.name).toLowerCase()===String(_selectedTeamName).toLowerCase());_selectedTeamRow=selectedRow||null;if(selectedRow)loadSelectedTeamProfile(selectedRow);else renderSelectedTeamProfile(null);
  const upcoming=document.getElementById('teamUpcomingList'), liveList=document.getElementById('teamLiveList'), liveSection=document.getElementById('teamLiveSection');
  const topSection=document.getElementById('teamTopSection'),topList=document.getElementById('teamTopList');
  if(!teams.length){liveSection.classList.add('hide');upcoming.innerHTML='<span class="muted">Add a favorite team to see its fixtures.</span>';}
  upcoming.innerHTML='<span class="muted">Loading fixtures...</span>';
  const r=await api('/api/my_teams');
  if(r.error){upcoming.innerHTML='<span class="err">'+esc(r.error)+'</span>';return;}
  _myTeamFixtures=r.fixtures||[];_sportsVisibleFixtures=[..._myTeamFixtures,...(r.top_fixtures||[])];renderSelectedTeamProfile(_selectedTeamProfile);
  const live=[], future=[];
  for(const f of (r.fixtures||[])){
    const ts=f.start?new Date(f.start).getTime():0, mins=ts?(Date.now()-ts)/60000:null;
    if(f.is_live||(!f.is_finished&&mins!==null&&mins>=0&&mins<=240))live.push(f);
    else if(mins!==null&&mins<0)future.push(f);
  }
  if(live.length){liveList.innerHTML=live.map(f=>teamFixtureCard(f,true)).join('');liveSection.classList.remove('hide');}
  else{liveList.innerHTML='';liveSection.classList.add('hide');}
  const topFixtures=r.top_fixtures||[];
  if(topFixtures.length){
    topList.innerHTML='<div class="topfixturegrid">'+topFixtures.map((f,i)=>'<div class="topfixtureitem'+(i>=12?' topfixtureextra hide':'')+'">'+teamFixtureCard(f,!!f.is_live)+'</div>').join('')+'</div>'
      +(topFixtures.length>12?'<div class="topfixturemore"><button class="ghost" onclick="toggleTopFixtures(this)">'+tr('Show more matches')+'</button></div>':'');
    topSection.classList.remove('hide');
  }else{topList.innerHTML='';topSection.classList.add('hide');}
  let upcomingHtml='';
  for(const team of teams){
    const name=String(typeof team==='string'?team:team.name||''), key=name.toLowerCase();
    const teamFixtures=future.filter(f=>(f.favorite_teams||[]).some(owner=>String(owner).toLowerCase()===key)).slice(0,4);
    if(!teamFixtures.length)continue;
    upcomingHtml+='<div class="teamupcominggroup"><div class="teamupcomingname">'+esc(name)+'</div><div class="teamfixturegrid">'+teamFixtures.map(f=>teamFixtureCard(f,false)).join('')+'</div></div>';
  }
  upcoming.innerHTML=upcomingHtml||(teams.length?'<span class="muted">No upcoming fixtures found.</span>':'<span class="muted">Add a favorite team to see its fixtures.</span>');
  loadSportsAvailability(false);
}

function applySportsAvailability(availability){
  const map=availability||{};
  for(const fixture of _sportsVisibleFixtures){const found=map[sportsFixtureKey(fixture)];if(found)Object.assign(fixture,found);}
  for(const card of document.querySelectorAll('#teamsView .teamfixture[data-event-key]')){
    const result=map[card.getAttribute('data-event-key')||''];if(!result)continue;
    const channels=[...(result.matches||[]),...(result.ppv_hits||[])],top=card.querySelector('.teamfixtureteams'),old=top&&top.querySelector('.teamfixturetv');
    card.classList.toggle('haschannels',channels.length>0);
    if(channels.length&&!old)top.insertAdjacentHTML('beforeend','<span class="cc teamfixturetv">TV</span>');else if(!channels.length&&old)old.remove();
    const panel=card.querySelector('.fixturechannelresults');if(panel)panel.innerHTML=fixtureStoredChannelsHtml(result);
  }
}
async function loadSportsAvailability(force){
  if(_sportsAvailabilityTimer){clearTimeout(_sportsAvailabilityTimer);_sportsAvailabilityTimer=null;}
  const unique=new Map();for(const f of _sportsVisibleFixtures){if(f&&f.home&&f.away)unique.set(sportsFixtureKey(f),f);}
  if(unique.size){try{const r=await api('/api/sports_availability',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fixtures:Array.from(unique.values()),force:!!force})});if(!r.error){if(r.logged_in===false){const unavailable={};for(const [key] of unique)unavailable[key]={logged_in:false,matches:[],ppv_hits:[]};applySportsAvailability(unavailable);}else applySportsAvailability(r.availability||{});}}catch(e){}}
  _sportsAvailabilityTimer=setTimeout(()=>{if(!document.getElementById('teamsView').classList.contains('hide'))loadSportsAvailability(true);},15*60*1000);
}
async function checkTeamFixtures(btn){
  const old=btn.innerHTML;
  btn.disabled=true;btn.textContent='Refreshing fixtures...';
  try{
    const j=await api('/api/check_team_fixtures',{method:'POST'});
    if(j.error)throw new Error(j.error||'Refresh failed');
    await loadMyTeams();
    toast('Successfully refreshed favorite-team fixtures.',7000);
  }catch(e){toast('Could not refresh team fixtures.',7000);}
  finally{btn.disabled=false;btn.innerHTML=old;applyLang();}
}
function toggleTopFixtures(btn){
  const extras=document.querySelectorAll('#teamTopList .topfixtureextra');
  if(!extras.length)return;
  const opening=extras[0].classList.contains('hide');
  extras.forEach(el=>el.classList.toggle('hide',!opening));
  btn.textContent=tr(opening?'Show fewer matches':'Show more matches');
}
async function searchTeams(){
  const q=(document.getElementById('q').value||'').trim(), el=document.getElementById('teamSearchResults');
  if(!q){el.innerHTML='';return;}
  el.innerHTML='<span class="muted">Searching FotMob...</span>';
  const r=await api('/api/team_search?q='+encodeURIComponent(q));
  if(r.error){el.innerHTML='<span class="err">'+esc(r.error)+'</span>';return;}
  if(!r.teams.length){el.innerHTML='<span class="muted">No team found in current FotMob listings.</span>';return;}
  el.innerHTML='<div class="matchresultslabel">'+esc(tr('Teams'))+'</div><div class="teamsearchchips">'+r.teams.map(team=>{const name=typeof team==='string'?team:team.name,id=typeof team==='string'?'':(team.team_id||''),logo=id?'/api/team_logo?id='+encodeURIComponent(String(id)):'';return '<div class="teamsearchhit" data-team-select="'+escAttr(name)+'" data-team-id="'+escAttr(id)+'">'+(logo?'<img class="teamsearchlogo" src="'+escAttr(logo)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'<span>'+esc(name)+'</span><button type="button" class="ghost teamfindfixtures" data-team-fixtures="'+escAttr(name)+'" data-team-id="'+escAttr(id)+'">'+esc(tr('Find fixtures'))+'</button><span class="favstar teamstar'+(_favTeamSet.has(name.toLowerCase())?' on':'')+'" data-team-name="'+escAttr(name)+'" data-team-id="'+escAttr(id)+'" title="Favorite">&#9733;</span></div>';}).join('')+'</div>';
}
async function searchTeamHub(query){
  const input=document.getElementById('q');if(query!==undefined&&input)input.value=String(query||'');
  if(!input||!input.value.trim()){clearSportsSearch();return;}
  const back=document.getElementById('sportsSearchBack');if(back)back.classList.remove('hide');
  _teamDeepLink=null;
  _fixtureSearchTeamId='';
  const results=document.getElementById('results');if(results)results.innerHTML='';
  await searchTeams();
}
async function findSportsFixtures(name,teamId){
  const input=document.getElementById('q');if(input)input.value=String(name||'');
  if(!String(name||'').trim())return;
  const back=document.getElementById('sportsSearchBack');if(back)back.classList.remove('hide');
  _teamDeepLink=null;_fixtureSearchTeamId=String(teamId||'');selectMyTeam(name,teamId||'','');
  await doSearch();
}
function clearSportsSearch(){
  const input=document.getElementById('q'),teams=document.getElementById('teamSearchResults'),results=document.getElementById('results'),back=document.getElementById('sportsSearchBack');
  if(input)input.value='';if(teams)teams.innerHTML='';if(results)results.innerHTML='';if(back)back.classList.add('hide');
  _searchData=null;_teamGroups=[];_activeTeam=0;_teamDeepLink=null;_fixtureSearchTeamId='';
}
function openMyTeamsFixture(target){
  if(!target)return;
  const read=(name)=>target instanceof Element?target.getAttribute(name):target[name.replace(/^data-/,'').replace(/-([a-z])/g,(_,c)=>c.toUpperCase())];
  _teamDeepLink={home:String(read('data-home')||target.home||''),away:String(read('data-away')||target.away||''),start:String(read('data-start')||target.start||'')};
  const query=String(read('data-search')||target.search||target.owner||_teamDeepLink.home||_teamDeepLink.away||'');
  _selectedTeamName=query;const favorite=_favTeamRows.find(t=>_teamNamesEquivalentForUi(t.name,query));if(favorite){_selectedTeamRow=favorite;renderTeamFavoriteRail();loadSelectedTeamProfile(favorite);}
  _fixtureSearchTeamId=String((favorite&&favorite.team_id)||'');
  const input=document.getElementById('q'),results=document.getElementById('results'),teams=document.getElementById('teamSearchResults'),back=document.getElementById('sportsSearchBack');
  if(input)input.value='';if(results)results.innerHTML='';if(teams)teams.innerHTML='';if(back)back.classList.add('hide');
  const cards=Array.from(document.querySelectorAll('#teamsView .teamfixture[data-fixture-card="1"]'));
  const selected=cards.find(card=>_teamNamesEquivalentForUi(card.getAttribute('data-home'),_teamDeepLink.home)&&_teamNamesEquivalentForUi(card.getAttribute('data-away'),_teamDeepLink.away)&&(!_teamDeepLink.start||!card.getAttribute('data-start')||Math.abs(new Date(card.getAttribute('data-start'))-new Date(_teamDeepLink.start))<6*3600000));
  cards.forEach(card=>card.classList.toggle('selectedfixture',card===selected));
  if(selected){const details=selected.querySelector('.teamfixturebroadcasts');if(details)details.classList.remove('hide');selected.scrollIntoView({behavior:'smooth',block:'center'});}
}
function fixtureStoredChannelsHtml(f){
  if(!f.logged_in)return '<span class="muted">'+esc(tr('Log in via Settings first.'))+'</span>';
  const all=[...(f.matches||[]),...(f.ppv_hits||[])],seen=new Set(),definite=[],other=[];
  for(const ch of all){const id=String(ch.stream_id||'');if(!id||seen.has(id))continue;seen.add(id);if(fixtureChannelRank(ch,f)===3||ch.provider_exact===true)definite.push(ch);else other.push(ch);}
  definite.sort(preferredChannelSort);other.sort(preferredChannelSort);
  const line=ch=>'<div class="racingeventchannel">'+channelLogo(ch,'mini')+'<span class="chn">'+esc(ch.xtream_name||'Channel')+(ch.quality?'<span class="tag">'+esc(ch.quality)+'</span>':'')+'</span><span class="chbtns">'+playbtns(ch.stream_id,ch.xtream_name,ch.url)+'</span></div>';
  let h='';if(definite.length)h+='<div class="muted">'+esc(tr('Definite channel matches'))+'</div>'+definite.map(line).join('');
  if(other.length){const groups=new Map();for(const ch of other){const key=String(ch.category||tr('Other possible channels'));if(!groups.has(key))groups.set(key,[]);groups.get(key).push(ch);}h+='<div class="muted" style="margin-top:8px">'+esc(tr('Possible channels by category'))+'</div>';for(const [name,items] of groups)h+='<div class="bcrow"><div class="bchead"><span class="bcname">'+esc(name)+'</span><span class="muted">'+items.length+' '+esc(tr(items.length===1?'channel':'channels'))+'</span><span class="bcchevron">&#9662;</span></div><div class="bcchans hide">'+items.map(line).join('')+'</div></div>';}
  return h||'<span class="muted">'+esc(tr('No matching channels'))+'</span>';
}
async function loadStoredFixtureChannels(card){
  const panel=card&&card.querySelector('.fixturechannelresults');if(!card||!panel||panel.innerHTML.trim())return;
  const fixture={home:card.getAttribute('data-home')||'',away:card.getAttribute('data-away')||'',start:card.getAttribute('data-start')||'',by_country:{}};
  try{const result=await api('/api/sports_event_channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fixture:fixture,cached_only:true})});if(result.cached){Object.assign(fixture,result);panel.innerHTML=fixtureStoredChannelsHtml(fixture);}}catch(e){}
}
async function toggleTeamFavorite(name,star,teamId){
  const r=await favPost({action:'toggle_team',team:{name:name,team_id:teamId||''}});
  _favTeamSet=new Set((r.team_names||[]).map(name=>String(name).toLowerCase()));
  if(star)star.classList.toggle('on',_favTeamSet.has(String(name).toLowerCase()));
  await loadMyTeams();
}
async function removeTeamFavorite(name){
  await favPost({action:'remove_team',name:name});
  _favTeamSet.delete(String(name).toLowerCase());
  await loadMyTeams();
}

let _catsLoaded=false;
let _allCats=[];
// Build icons from Unicode code points instead of embedding emoji bytes in the
// source. This keeps flags intact if a Windows editor repacks the script.
const _COUNTRY_CODES={
  no:'NO',se:'SE',dk:'DK',fi:'FI',uk:'GB',gb:'GB',us:'US',ca:'CA',de:'DE',fr:'FR',
  it:'IT',es:'ES',pt:'PT',nl:'NL',be:'BE',ch:'CH',at:'AT',ie:'IE',pl:'PL',gr:'GR',
  tr:'TR',ru:'RU',ua:'UA',ro:'RO',bg:'BG',hr:'HR',si:'SI',rs:'RS',cz:'CZ',sk:'SK',
  hu:'HU',al:'AL',ba:'BA',mk:'MK',in:'IN',pk:'PK',ir:'IR',sa:'SA',eg:'EG',il:'IL',
  br:'BR',mx:'MX',au:'AU',ag:'AF'
};
const _ICONS={ar:0x1f310,afr:0x1f30d,asia:0x1f30f,ex:0x1f310,'ex-yu':0x1f310,
  am:0x1f310,mena:0x1f310,'4k':0x1f4fa,uhd:0x1f4fa,ppv:0x1f3ab,
  vip:0x2b50,sport:0x26bd,sports:0x26bd};
function _countryFlag(code){
  return String.fromCodePoint(...code.split('').map(c=>0x1f1e6+c.charCodeAt(0)-65));
}
function _flagFor(name){
  const m=(name||'').match(/^\\s*([a-z0-9-]{1,5})\\s*\\|/i);
  if(!m)return String.fromCodePoint(0x1f310);
  const key=m[1].toLowerCase();
  if(_COUNTRY_CODES[key])return _countryFlag(_COUNTRY_CODES[key]);
  return String.fromCodePoint(_ICONS[key]||0x1f310);
}
let _selCats=new Set();
let _activeCat=null;
let _ccChannels=[];      // channels currently shown in Category Channels
let _playlist=new Map(); // stream_id -> {name,url,category}

async function loadCategories(force){
  if(_catsLoaded&&!force)return;
  const r=await api('/api/categories');
  if(!r.logged_in){document.getElementById('catlist').innerHTML='<span class="muted">Log in via Settings first.</span>';return;}
  if(r.error){document.getElementById('catlist').innerHTML='<span class="err">'+esc(r.error)+'</span>';return;}
  _allCats=r.categories||[];
  _catsLoaded=true;
  renderCatList();renderSelected();renderPlaylist();
}
function renderCatList(){
  const fEl=document.getElementById('catfilter');
  const f=(fEl?fEl.value:'').toLowerCase();
  const shown=_allCats.filter(function(c){return !f||c.name.toLowerCase().indexOf(f)>=0;});
  let html='';
  for(const c of shown){
    const on=_selCats.has(c.name)?' on':'';
    html+='<div class="catitem'+on+'" onclick="toggleCat(this.getAttribute(\\'data-c\\'))" data-c="'+escAttr(c.name)+'">'
      +'<span class="tick">\u2713</span>'
      +'<span class="flag">'+_flagFor(c.name)+'</span>'
      +'<span class="cn">'+esc(c.name)+' <span class="pc">'+c.count+'</span></span></div>';
  }
  const box=document.getElementById('catlist');
  const rows=Math.ceil(shown.length/4)||1;
  box.style.setProperty('--catrows',rows);
  box.innerHTML=html||'<span class="muted">No categories match.</span>';
}
function toggleCat(name){
  if(_selCats.has(name))_selCats.delete(name); else _selCats.add(name);
  if(!_selCats.has(_activeCat))_activeCat=null;
  renderCatList();renderSelected();
}
function renderSelected(){
  const sel=Array.from(_selCats);
  const box=document.getElementById('selcats');
  if(!sel.length){box.innerHTML='<span class="muted">Tick categories on the left.</span>';return;}
  const byName={};for(const c of _allCats)byName[c.name]=c.count;
  let html='';
  for(const s of sel){
    const active=(s===_activeCat)?' active':'';
    html+='<div class="selcat'+active+'" onclick="openCategory(this.getAttribute(\\'data-c\\'))" data-c="'+escAttr(s)+'">'
      +'<span>'+esc(s)+'</span><span class="cnt">'+(byName[s]||0)+'</span><span class="chev">\u203A</span></div>';
  }
  box.innerHTML=html;
}

async function openCategory(cat){
  _activeCat=cat;
  renderSelected();
  document.getElementById('ccHead').textContent=tr('Filter Channels');
  const el=document.getElementById('ccList');
  el.innerHTML='<span class="muted">Loading...</span>';
  const r=await api('/api/channels?q=&cat='+encodeURIComponent(cat));
  if(!r.logged_in||!r.channels){el.innerHTML='<span class="muted">Could not load.</span>';return;}
  _ccChannels=r.channels;
  document.getElementById('ccHead').innerHTML=tr('Filter Channels')+' <span class="muted">('+esc(cat)+')</span>';
  renderCC();
}
function renderCC(){
  const el=document.getElementById('ccList');
  if(!_ccChannels.length){el.innerHTML='<span class="muted">No channels in this category.</span>';return;}
  let html='';
  for(const c of _ccChannels){
    const inpl=_playlist.has(String(c.stream_id))?' checked':'';
    html+='<label class="chrow"><input type="checkbox" class="ccck" data-sid="'+escAttr(String(c.stream_id))+'"'+inpl+'>'
      +channelLogo(c,'mini')+'<span class="chname">'+esc(c.name)+(c.quality?'<span class="tag">'+esc(c.quality)+'</span>':'')+'</span></label>';
  }
  el.innerHTML=html;
}
function ccTick(on){document.querySelectorAll('.ccck').forEach(function(c){c.checked=on;});}

function addTickedToPlaylist(){
  const ticked=new Set(Array.from(document.querySelectorAll('.ccck:checked')).map(function(c){return c.getAttribute('data-sid');}));
  for(const c of _ccChannels){
    const sid=String(c.stream_id);
    if(ticked.has(sid))_playlist.set(sid,{name:c.name,url:c.url,category:c.category,logo:c.logo||''});
  }
  renderPlaylist();
}
function renderPlaylist(){
  const el=document.getElementById('plList');
  const cnt=document.getElementById('plCount');
  cnt.textContent=_playlist.size?('('+_playlist.size+')'):'';
  if(!_playlist.size){el.innerHTML='<span class="muted">Ticked channels land here.</span>';return;}
  let html='';
  for(const [sid,c] of _playlist){
    html+='<div class="plitem"><span class="x" onclick="plRemove(\\''+escAttr(sid)+'\\')">\u2715</span>'
      +channelLogo({stream_id:sid,logo:c.logo},'mini')+'<div class="chname">'+esc(c.name)+'</div></div>';
  }
  el.innerHTML=html;
}
function plRemove(sid){
  _playlist.delete(sid);renderPlaylist();
  // also untick in the CC list if visible
  document.querySelectorAll('.ccck').forEach(function(c){if(c.getAttribute('data-sid')===sid)c.checked=false;});
}
function clearPlaylist(){_playlist.clear();renderPlaylist();ccTick(false);}
function clearSelectedCats(){_selCats.clear();_activeCat=null;renderCatList();renderSelected();document.getElementById('ccList').innerHTML='<span class="muted">Click a selected category to see its channels.</span>';document.getElementById('ccHead').textContent=tr('Filter Channels');}

async function buildM3U(mode){
  const cats=Array.from(_selCats);
  if(!cats.length){alert('Tick at least one category on the left first.');return;}
  const resp=await fetch('/api/m3u',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:'categories',categories:cats})});
  if(!resp.ok){alert('Failed to build M3U.');return;}
  const blob=await resp.blob();
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download='categories.m3u';document.body.appendChild(a);a.click();
  setTimeout(function(){URL.revokeObjectURL(url);a.remove();},500);
}

async function buildPlaylistM3U(){
  if(!_playlist.size){alert('Playlist is empty. Tick channels and click "Add ticked".');return;}
  const ids=Array.from(_playlist.keys());
  const resp=await fetch('/api/m3u',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:'channels',stream_ids:ids})});
  if(!resp.ok){alert('Failed to build M3U.');return;}
  const blob=await resp.blob();
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download='playlist.m3u';document.body.appendChild(a);a.click();
  setTimeout(function(){URL.revokeObjectURL(url);a.remove();},500);
}

// Reset only the temporary Playlist searches; keep the user's builder selections intact.
function resetPlaylistSearch(){
  const cq=document.getElementById('cq'),catq=document.getElementById('catq');
  const cr=document.getElementById('cresults'),ctr=document.getElementById('catresults');
  if(cq)cq.value='';if(catq)catq.value='';if(cr)cr.innerHTML='';if(ctr)ctr.innerHTML='';
  const builder=document.querySelector('#channelsView .ch4');
  if(builder)builder.scrollIntoView({behavior:'smooth',block:'start'});
}

// Right-column channel search on the Search page (simple version)
async function doCategorySearch(){
  const q=(document.getElementById('catq').value||'').trim().toLowerCase();
  const el=document.getElementById('catresults');
  el.innerHTML='<div class="muted">'+tr('Loading...')+'</div>';
  const r=await api('/api/categories');
  if(!r.logged_in){el.innerHTML='<div class="muted">Log in via <a onclick="showSettings()" style="color:var(--acc);cursor:pointer">Settings</a> to search categories.</div>';return;}
  let cats=(r.categories||[]);
  if(q)cats=cats.filter(function(c){return c.name.toLowerCase().indexOf(q)>=0;});
  if(!cats.length){el.innerHTML='<div class="muted">No categories found'+(q?' for "'+esc(q)+'"':'')+'.</div>';return;}
  await refreshFavState();
  let h='<div class="muted" style="margin:6px 0">'+cats.length+' '+tr('Categories').toLowerCase()+'</div>';
  for(const c of cats){
    const fav=_favCatSet.has(c.name)?' on':'';
    h+='<div class="chrow">'
      +'<span class="favstar'+fav+'" data-favcat="'+escAttr(c.name)+'" title="Favorite">\u2605</span>'
      +'<span class="chname">'+_flagFor(c.name)+' '+esc(c.name)+' <span class="muted">'+c.count+'</span></span></div>';
  }
  el.innerHTML=h;
}
async function toggleFavCat(name,starEl){
  let r;
  if(_favCatSet.has(name)){r=await favPost({action:'remove_cat',category:name});_favCatSet.delete(name);}
  else{r=await favPost({action:'add_cats',categories:[name]});_favCatSet.add(name);}
  if(starEl)starEl.classList.toggle('on',_favCatSet.has(name));
}
async function doChannelSearch(inputId, targetId){
  const q=document.getElementById(inputId).value.trim();
  const el=document.getElementById(targetId);
  el.innerHTML='<span class="muted">Searching your channels...</span>';
  const r=await api('/api/channels?q='+encodeURIComponent(q)+'&cat=');
  if(r.error){el.innerHTML='<span class="err">'+esc(r.error)+'</span>';return;}
  if(!r.logged_in){el.innerHTML='<div class="muted">Log in via <a onclick="showSettings()" style="color:var(--acc);cursor:pointer">Settings</a> to search your channels.</div>';return;}
  if(!r.channels.length){el.innerHTML='<div class="muted">No channels found'+(q?' for "'+esc(q)+'"':'')+'.</div>';return;}
  await refreshFavState();
  let html='<div class="muted" style="margin:6px 0">'+r.shown+(r.total>r.shown?(' of '+r.total+' (type more to narrow)'):'')+' channel'+(r.total===1?'':'s')+'</div>';
  for(const c of r.channels){
    const fav=_favChanSet.has(String(c.stream_id))?' on':'';
    html+='<div class="chrow">'
      +'<span class="favstar'+fav+'" data-sid="'+escAttr(String(c.stream_id))+'" data-name="'+escAttr(c.name)+'" data-cat="'+escAttr(c.category||'')+'" title="Favorite">&#9733;</span>'
      +channelLogo(c)+'<div class="chname">'+esc(c.name)+(c.quality?'<span class="tag">'+esc(c.quality)+'</span>':'')
      +(c.category?' <span class="muted">'+esc(c.category)+'</span>':'')
      +'<div class="churl">'+esc(c.url)+'</div></div>'
      +'<div style="display:flex;flex-shrink:0">'+playbtns(c.stream_id,c.name,c.url)+'</div></div>';
  }
  el.innerHTML=html;
}
async function api(p,o){
  const r=await fetch(p,o);let j;
  try{j=await r.json();}catch(e){return {error:'Invalid server response',status:r.status};}
  if(!j||typeof j!=='object')j={data:j};
  if(!r.ok&&!j.error)j.error='HTTP '+r.status;
  j._httpStatus=r.status;j._httpOk=r.ok;
  return j;
}
async function favPost(body){return api('/api/favorites',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
async function refreshStatus(){
  const s=await api('/api/status');
  status.innerHTML=!s.configured?'<span class="err">Not configured &mdash; open Settings</span>'
    :(s.channel_count!=null?s.channel_count+' channels loaded':'configured');
  const slider=document.getElementById('matchStrict'), value=document.getElementById('matchStrictValue');
  if(slider&&s.match_threshold!=null){slider.value=Number(s.match_threshold).toFixed(2);value.textContent=slider.value;}
}
async function saveMatchStrictness(value){
  const strict=Math.max(0.40,Math.min(0.80,parseFloat(value)||0.62));
  try{await api('/api/match_strictness',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({match_threshold:strict})});}catch(e){}
}
async function loadSettings(){
  const c=await api('/api/config');
  s_host.value=c.xtream_host||'';
  s_user.value=c.xtream_user||'';s_pass.value=c.xtream_pass||'';
  s_ext.value=c.stream_ext||'ts';
  s_cc.value=(c.countries||['no','uk','us']).join(', ');
  s_start.value=c.start_section||'mylist';
  s_checkshows.checked=!!c.check_shows_on_startup;
  s_refreshiptv.checked=!!c.refresh_iptv_on_startup;
  s_refreshsports.checked=!!c.refresh_sports_on_startup;
  s_profile.value=c.profile_name||'';
  s_lang.value=c.preferred_language||'en';
  _selectedEmblem=_PROFILE_EMBLEMS[c.profile_emblem]?c.profile_emblem:'tvstack';renderEmblemPicker();
  s_mylistlayout.value=['balanced','spotlight','timeline','hub'].includes(c.mylist_layout)?c.mylist_layout:'timeline';
  s_football.checked=c.football_enabled!==false;
  s_f1.checked=c.f1_enabled!==false;
  s_games.checked=c.games_enabled!==false;
  s_background.value=['float','ascii','off'].includes(c.background_style)?c.background_style:(c.decorations_enabled===false?'off':'float');
  s_autoshutdown.value=String(c.auto_shutdown_minutes||0);
  loadArtworkCacheSize();
}
async function saveSettings(){
  const body={xtream_host:s_host.value,xtream_user:s_user.value,
    xtream_pass:s_pass.value,stream_ext:s_ext.value,
    countries:s_cc.value.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean),
    start_section:s_start.value,check_shows_on_startup:s_checkshows.checked,
    refresh_iptv_on_startup:s_refreshiptv.checked,refresh_sports_on_startup:s_refreshsports.checked,
    profile_name:s_profile.value.trim(),preferred_language:s_lang.value,
    profile_emblem:_selectedEmblem,mylist_layout:s_mylistlayout.value,football_enabled:s_football.checked,
    f1_enabled:s_f1.checked,games_enabled:s_games.checked,background_style:s_background.value,decorations_enabled:s_background.value!=='off',hide_cmd_window:true,auto_shutdown_minutes:Number(s_autoshutdown.value||0)};
  if(!body.games_enabled&&body.start_section==='games')body.start_section='mylist';
  if(!body.f1_enabled&&body.start_section==='racing')body.start_section='mylist';
  if(!body.football_enabled&&body.start_section==='teams')body.start_section='mylist';
  const r=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  s_msg.textContent=r.ok?'Saved.':'Error saving.';
  if(r.ok){setLang(body.preferred_language);applyProfileConfig(body);toast('Saved.');if(!body.football_enabled&&!teamsView.classList.contains('hide'))showMylist();if(!body.games_enabled&&!gamesView.classList.contains('hide'))showMylist();if(!body.f1_enabled&&!racingView.classList.contains('hide'))showMylist();}
  refreshStatus();
}
function formatBytes(n){
  n=Number(n)||0;if(n<1024)return n+' B';
  const units=['KB','MB','GB'];let i=-1;
  do{n/=1024;i++;}while(n>=1024&&i<units.length-1);
  return n.toFixed(n>=10?1:2)+' '+units[i];
}
async function loadArtworkCacheSize(){
  try{const r=await api('/api/artwork_cache');s_artsize.textContent=formatBytes(r.bytes);}
  catch(e){s_artsize.textContent='Unavailable';}
}
async function clearArtworkCache(){
  if(!confirm('Clear all downloaded show artwork?'))return;
  try{
    const r=await api('/api/clear_artwork_cache',{method:'POST'});
    if(!r.ok)throw new Error(r.error||'clear failed');
    s_artsize.textContent='0 B';toast('Artwork cache cleared.');
  }catch(e){toast('Could not clear artwork cache.');}
}
async function stopTVMate(){
  if(!confirm('Stop TVMate? Streaming links and the local TVMate page will stop until you start the app again.'))return;
  try{
    const j=await api('/api/shutdown',{method:'POST'});if(j.error||!j.ok)throw new Error('shutdown failed');
    document.body.innerHTML='<div style="min-height:100vh;display:grid;place-items:center;background:#0d1013;color:#e7e7e7;font-family:system-ui,sans-serif"><div style="text-align:center"><div style="font-size:54px">📺</div><h1>TVMate has stopped</h1><p style="color:#999">You can close this tab. Start TVMate again whenever you are ready.</p></div></div>';
  }catch(e){toast('Could not stop TVMate.');}
}
let _lastActivityPing=0;
function markTVMateActivity(){const now=Date.now();if(now-_lastActivityPing<20000)return;_lastActivityPing=now;fetch('/api/activity',{method:'POST',keepalive:true}).catch(()=>{});}
document.addEventListener('pointerdown',markTVMateActivity,{passive:true});
document.addEventListener('keydown',markTVMateActivity,{passive:true});
document.addEventListener('touchstart',markTVMateActivity,{passive:true});
document.addEventListener('visibilitychange',function(){if(!document.hidden)markTVMateActivity();});
setInterval(function(){
  const popup=document.getElementById('pVideo'),live=document.getElementById('tvVideo');
  if((popup&&!popup.paused&&!popup.ended)||(live&&!live.paused&&!live.ended))markTVMateActivity();
},30000);
markTVMateActivity();
async function resetColdStart(btn){
  if(!confirm('Clear performance caches and reload TVMate for a cold-start test?'))return;
  const old=btn.textContent;btn.disabled=true;btn.textContent='Resetting...';
  try{
    const r=await api('/api/reset_cold_start',{method:'POST'});
    if(!r.ok)throw new Error(r.error||'reset failed');
    toast('Cold-start caches cleared. Reloading...',1800);
    setTimeout(()=>location.reload(),1100);
  }catch(e){
    toast('Could not reset cold-start caches.');btn.disabled=false;btn.textContent=old;
  }
}
let _devSequence='';
document.addEventListener('keydown',function(e){
  const settings=document.getElementById('settingsView');
  if(!settings||settings.classList.contains('hide')){_devSequence='';return;}
  if(e.key==='7')_devSequence=(_devSequence+'7').slice(-3);else _devSequence='';
  if(_devSequence==='777'){
    document.getElementById('devSettings').classList.remove('hide');
    _devSequence='';setSettingsTab('maintenance');toast('Developer tools unlocked.');
  }
});
async function testLogin(){
  const msg=document.getElementById('s_connmsg');msg.textContent=tr('Testing...');
  const r=await api('/api/test_credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({xtream_host:s_host.value,xtream_user:s_user.value,xtream_pass:s_pass.value})});
  msg.innerHTML=r.ok?('<span class="ok">'+tr('Login successful.')+'</span>'):('<span class="err">'+esc(r.error||tr('Login failed.'))+'</span>');
}
function refreshMessage(text){const el=document.getElementById('s_refreshmsg');if(el)el.textContent=text;}
async function withRefreshButton(btn,label,work){
  const old=btn?btn.textContent:'';if(btn){btn.disabled=true;btn.textContent=label;}
  try{return await work();}catch(e){if(btn){const message='Refresh failed: '+String(e&&e.message||e);refreshMessage(message);toast(message,7000);}throw e;}finally{if(btn){btn.disabled=false;btn.textContent=old;applyLang();}}
}
async function refreshIptvContent(btn,quiet){
  return withRefreshButton(btn,'Refreshing IPTV...',async function(){
    refreshMessage('Refreshing Xtream channels, movies, shows and episodes...');
    const catalog=await api('/api/refresh_xtream',{method:'POST'});if(catalog.error||!catalog.ok)throw new Error(catalog.error||'IPTV refresh failed');
    refreshMessage('IPTV catalogues ready. Updating EPG...');
    const epg=await api('/api/epg?force=1&favorites=1');if(epg.error)throw new Error(epg.error||'EPG refresh failed');
    _tvEpg=Object.assign({},_tvEpg,epg.epg||{});_latestEpisodesLoaded=false;refreshStatus();
    if(!mytvView.classList.contains('hide'))renderTvGuide();
    const stats=epg.stats||{},summary='IPTV: '+catalog.channels+' channels, '+catalog.movies+' movies, '+catalog.shows+' shows · EPG: '+(stats.updated||0)+' channels';
    refreshMessage(summary);if(!quiet)toast(summary,7000);return {catalog:catalog,epg:epg,summary:summary};
  });
}
async function refreshOtherContent(btn,quiet){
  return withRefreshButton(btn,'Refreshing content...',async function(){
    const c=await api('/api/config'),parts=[],failures=[];refreshMessage('Refreshing sports, racing and games...');
    if(c.f1_enabled!==false){const racing=await api('/api/refresh_racing',{method:'POST'});if(!racing.error&&racing.ok)parts.push((racing.series||0)+' racing series');else failures.push('racing');}
    if(c.football_enabled!==false){const sports=await api('/api/refresh_football',{method:'POST'});if(!sports.error&&sports.ok)parts.push((sports.matches||0)+' matches, '+(sports.teams||0)+' teams, '+(sports.guides||0)+' TV guides');else failures.push('sports');}
    if(c.games_enabled!==false&&String(c.steam_wishlist_url||'').trim()){
      const steam=await api('/api/import_steam_wishlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:c.steam_wishlist_url})});if(!steam.error&&steam.ok)parts.push((steam.imported||0)+' Steam games');else failures.push('games');
    }
    const summary='Sports, racing & games: '+(parts.length?parts.join(' · '):'nothing enabled')+(failures.length?' · Failed: '+failures.join(', '):'');refreshMessage(summary);if(!quiet)toast(summary,7000);return {summary:summary,failures:failures};
  });
}
async function refreshEverything(btn,quiet){
  return withRefreshButton(btn,'Refreshing everything...',async function(){
    const c=await api('/api/config'),parts=[],failures=[];
    const run=async function(label,work){try{return await work();}catch(e){failures.push(label);return null;}};
    const xtreamConfigured=!!(String(c.xtream_host||'').trim()&&String(c.xtream_user||'').trim()&&String(c.xtream_pass||'').trim());
    refreshMessage('Refreshing enabled content...');
    if(xtreamConfigured){
      const catalog=await run('IPTV',async function(){const r=await api('/api/refresh_xtream',{method:'POST'});if(r.error||!r.ok)throw new Error(r.error||'IPTV refresh failed');return r;});
      if(catalog)parts.push('IPTV '+catalog.channels+' channels, '+catalog.movies+' movies, '+catalog.shows+' shows');
      const currentId=(_tvPlaying&&(_tvPlaying.stream_id||_tvPlaying.id||_tvPlaying))||'';
      const epg=await run('EPG',async function(){const r=await api('/api/epg?force=1&favorites=1'+(currentId?'&ids='+encodeURIComponent(String(currentId)):''));if(r.error)throw new Error(r.error);return r;});
      if(epg)parts.push('EPG '+((epg.stats&&epg.stats.updated)||0)+' channels');
    }else parts.push('IPTV skipped');
    if(c.f1_enabled!==false){
      const racing=await run('Racing',async function(){const r=await api('/api/refresh_racing',{method:'POST'});if(r.error||!r.ok)throw new Error(r.error||'racing refresh failed');return r;});
      if(racing)parts.push('Racing '+racing.series+' series');
    }
    if(c.football_enabled!==false){
      const football=await run('Sports',async function(){const r=await api('/api/refresh_football',{method:'POST'});if(r.error||!r.ok)throw new Error(r.error||'sports refresh failed');return r;});
      if(football)parts.push('Sports '+football.teams+' teams, '+football.guides+' guides');
    }
    if(c.games_enabled!==false&&String(c.steam_wishlist_url||'').trim()){
      const steam=await run('Steam',async function(){const r=await api('/api/import_steam_wishlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:c.steam_wishlist_url})});if(r.error||!r.ok)throw new Error(r.error||'Steam refresh failed');return r;});
      if(steam)parts.push('Steam '+steam.imported+' games');
    }
    const episodes=await run('Show schedules',async function(){const r=await api('/api/latest_episodes?refresh=1&limit=9');if(r.error)throw new Error(r.error);return r;});
    if(episodes){_latestEpisodesLoaded=false;parts.push('Show schedules updated');}
    for(const category of ['popular','new','featured']){
      const movies=await run('Cinemeta '+category,async function(){const r=await api('/api/movie_catalog?catalog='+category+'&limit=10');if(r.error)throw new Error(r.error);return r;});
      if(movies)_movieCatalogCache[category]={movies:movies.movies||[],logged_in:!!movies.logged_in};
    }
    parts.push('Cinemeta cache checked');
    refreshStatus();if(!mytvView.classList.contains('hide'))renderTvGuide();
    const summary=parts.join(' · ')+(failures.length?' · Failed: '+failures.join(', '):'');
    refreshMessage(summary);if(!quiet)toast(failures.length?'Refresh finished with some errors.':'Everything refreshed successfully.',7000);return {summary:summary,failures:failures};
  });
}
let _searchData=null;   // {fixtures, logged_in, ppv_categories}
let _teamGroups=[];      // [{team, fixtures:[...]}]
let _activeTeam=0;

async function doSearch(){
  if(!_footballEnabled)return;
  const q=document.getElementById('q').value.trim(),resultEl=document.getElementById('results');
  if(!resultEl)return;
  resultEl.innerHTML='';
  if(!q)return;
  resultEl.innerHTML='<span class="muted">Searching listings...</span>';
  const strict=document.getElementById('matchStrict').value;
  const r=await api('/api/search?q='+encodeURIComponent(q)+'&strictness='+encodeURIComponent(strict)+(_fixtureSearchTeamId?'&team_id='+encodeURIComponent(_fixtureSearchTeamId):''));
  if(r.error){resultEl.innerHTML='<span class="err">'+r.error+'</span>';return;}
  _searchData=r;
  if(r.logged_in)await refreshFavState();
  let head='';
  if(r.source_errors&&r.source_errors.length)
    head+='<div class="muted err">Some listings failed: '+r.source_errors.join('; ')+'</div>';
  if(!r.fixtures.length){resultEl.innerHTML=head+'<div class="muted">No current or upcoming match found for "'+esc(q)+'".</div>';return;}
  // Group fixtures by the team that matches the query (home or away).
  _teamGroups=groupByTeam(r.fixtures,q);
  _activeTeam=0;
  resultEl.innerHTML=head+'<div class="matchresultslabel">'+esc(tr('Matches'))+' · '+r.fixtures.length+'</div><div id="teamSwitch"></div><div id="teamFixtures"></div>';
  renderTeamSwitch();
  renderActiveTeam();
}

function groupByTeam(fixtures,q){
  // Determine, per fixture, which side matched the query; group under that team name.
  const ql=q.toLowerCase();
  const groups={};      // teamName -> [fixtures]
  const order=[];
  for(const f of fixtures){
    // pick the team whose name/slug best contains the query; default home
    let team=f.home, other=f.away;
    const h=(f.home||'').toLowerCase(), a=(f.away||'').toLowerCase();
    // crude: if away contains the query fragment and home doesn't, group under away
    const hHit=h.includes(ql)||wordsOverlap(ql,h);
    const aHit=a.includes(ql)||wordsOverlap(ql,a);
    if(aHit&&!hHit){team=f.away;}
    if(!groups[team]){groups[team]=[];order.push(team);}
    groups[team].push(f);
  }
  return order.map(function(t){return {team:t,fixtures:groups[t]};});
}
function wordsOverlap(q,name){
  const qs=q.split(/\\s+/).filter(Boolean);
  return qs.some(function(w){return w.length>=3&&name.includes(w);});
}

function renderTeamSwitch(){
  const el=document.getElementById('teamSwitch');
  if(!el)return;
  if(_teamGroups.length<2){el.innerHTML='';return;}   // only show when 2+ teams
  let h='<div class="teamtabs">';
  _teamGroups.forEach(function(g,i){
    h+='<button class="teamtab'+(i===_activeTeam?' on':'')+'" data-team="'+i+'">'+esc(g.team)+' <span class="muted">('+g.fixtures.length+')</span></button>';
  });
  h+='</div>';
  el.innerHTML=h;
}

function renderActiveTeam(){
  const el=document.getElementById('teamFixtures');
  if(!el)return;
  const g=_teamGroups[_activeTeam];
  if(!g){el.innerHTML='';return;}
  let html='';
  const rows=g.fixtures.slice().sort((a,b)=>Number(fixtureMatchesDeepLink(b))-Number(fixtureMatchesDeepLink(a)));
  rows.forEach(function(f,fi){
    html+=renderFixtureCard(f,fi);
  });
  el.innerHTML=html;
}

function fixtureMatchesDeepLink(f){
  if(!_teamDeepLink||!f)return false;
  const teams=_teamNamesEquivalentForUi(f.home,_teamDeepLink.home)&&_teamNamesEquivalentForUi(f.away,_teamDeepLink.away);
  if(!teams)return false;
  if(!_teamDeepLink.start||!f.start)return true;
  const a=new Date(f.start).getTime(),b=new Date(_teamDeepLink.start).getTime();return Number.isFinite(a)&&Number.isFinite(b)?Math.abs(a-b)<6*3600000:true;
}
function _teamNamesEquivalentForUi(a,b){const clean=s=>String(s||'').toLowerCase().replace(/[^a-z0-9æøå]+/g,' ').trim();const x=clean(a),y=clean(b);return !!x&&!!y&&(x===y||x.includes(y)||y.includes(x));}
function fixtureChannelRank(m,f){
  const clean=s=>String(s||'').toLowerCase().replace(/[^a-z0-9æøå]+/g,' ').replace(/\\s+/g,' ').trim();
  const name=clean(m&&m.xtream_name),home=clean(f&&f.home),away=clean(f&&f.away);
  const homeHit=!!home&&name.includes(home),awayHit=!!away&&name.includes(away);
  // Visible two-team text is definitive and must override stale/mistaken
  // backend metadata before the broadcaster list is sorted.
  if(homeHit&&awayHit)return 3;
  if(m&&m.fixture_match==='exact')return 3;
  if(m&&m.fixture_match==='generic')return 2;
  if(m&&m.fixture_match==='partial')return 1;
  return homeHit||awayHit?1:2;
}
function channelLocalePriority(ch){
  const text=[ch&&ch.category,ch&&ch.xtream_name,ch&&ch.quality].filter(Boolean).join(' ');
  const is4k=/\\b(4k|uhd)\\b/i.test(text);
  const isNorwegian=/(^|[^a-z0-9])(no|norway|norge)([^a-z0-9]|$)/i.test(text);
  return (is4k?2:0)+(isNorwegian?1:0);
}
function preferredChannelSort(a,b){
  return channelLocalePriority(b)-channelLocalePriority(a)||Number(b.score||0)-Number(a.score||0)||String(a.xtream_name||'').localeCompare(String(b.xtream_name||''));
}

function renderFixtureCard(f,fi){
  const when=f.start?new Date(f.start).toLocaleString(_lang==='no'?'nb-NO':undefined,{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}):'';
  let badge='';
  if(f.start){
    const kick=new Date(f.start);
    const mins=Math.floor((Date.now()-kick.getTime())/60000);
    const sameDay=kick.toDateString()===new Date().toDateString();
    if(f.is_live||(!f.is_finished&&mins>=0&&mins<=240)){
      const hasClock=f.live_minute!==null&&f.live_minute!==undefined&&Number.isFinite(Number(f.live_minute));
      const liveMins=hasClock?Number(f.live_minute):Math.max(0,mins);
      badge=' <span class="live">\u25CF LIVE '+(hasClock?'':'~')+liveMins+"'</span>";
    }
    else if(f.is_finished||(mins>240&&(mins<360||sameDay)))badge=' <span class="ended">ended / earlier today</span>';
    else if(mins<0&&mins>-60)badge=' <span class="soon">starts in '+(-mins)+" min</span>";
  }
  const rows=[];
  for(const cc of Object.keys(f.by_country||{}))for(const b of f.by_country[cc])rows.push({cc:cc,bcast:b});
  rows.sort(function(x,y){const xp=x.cc.toUpperCase()==='NO'?1:0,yp=y.cc.toUpperCase()==='NO'?1:0;return yp-xp||(x.cc===y.cc?x.bcast.localeCompare(y.bcast):x.cc.localeCompare(y.cc));});
  // Pull every definite fixture-title hit into one visible section before
  // the broader broadcaster/provider categories. Keep those categories broad.
  const strictSeen=new Set(),strictResults=[];
  [...(f.ppv_hits||[]),...(f.matches||[]).filter(m=>fixtureChannelRank(m,f)===3||m.provider_exact===true)].forEach(function(m){
    const key=String(m.stream_id||'');
    if(key&&!strictSeen.has(key)){strictSeen.add(key);strictResults.push(m);}
  });
  strictResults.sort(preferredChannelSort);
  const matchedIds=new Set([...(f.matches||[]),...(f.ppv_hits||[])].map(m=>String(m.stream_id||'' )).filter(Boolean));
  const availText=matchedIds.size?(matchedIds.size+' '+tr(matchedIds.size===1?'channel':'channels')):(rows.length?tr('TV listed'):tr('No TV'));
  const availClass=(matchedIds.size||rows.length)?'':' none';
  const homeLogo=f.home_id?'<img class="matchfixtureteamlogo" src="/api/team_logo?id='+encodeURIComponent(String(f.home_id))+'" alt="" loading="lazy" onerror="this.remove()">':'';
  const awayLogo=f.away_id?'<img class="matchfixtureteamlogo" src="/api/team_logo?id='+encodeURIComponent(String(f.away_id))+'" alt="" loading="lazy" onerror="this.remove()">':'';
  let html='<div class="card matchfixture'+(fixtureMatchesDeepLink(f)?' selectedfixture':'')+'"><div class="matchfixturehead"><div class="matchfixtureteamsline"><div class="matchfixtureteam">'+homeLogo+'<span>'+esc(f.home)+'</span></div><span class="matchfixtureversus">v</span><div class="matchfixtureteam">'+awayLogo+'<span>'+esc(f.away)+'</span></div></div><div class="matchfixturemeta"><span>'+esc(when)+'</span>'+(badge||'')+'<span class="matchfixtureavailability'+availClass+'">'+esc(availText)+'</span></div></div><div class="matchfixturebody">';
  if(!_searchData.logged_in){
    html+='<div class="muted">Log in via <a onclick="showSettings()" style="color:var(--acc);cursor:pointer">Settings</a> to see which of your Xtream channels match.</div></div></div>';
    return html;
  }
  // Exact fixture/team event hits are the strongest results, so show them
  // before broader linear-broadcaster suggestions.
  if(strictResults.length){
    html+='<div class="muted" style="margin-bottom:8px">'+esc(tr('Definite channel matches'))+':</div><div class="bcastlist besteventmatches">';
    for(const [ppvIndex,m] of strictResults.entries()){
      const fav=_favChanSet.has(String(m.stream_id))?' on':'';
      html+='<div class="chline'+(ppvIndex>=7?' ppvextra hide':'')+'"><span class="matchchan"><span class="favstar'+fav+'" data-sid="'+escAttr(String(m.stream_id))+'" data-name="'+escAttr(m.xtream_name)+'" data-cat="'+escAttr(m.category||'')+'" title="Favorite">&#9733;</span>'
        +channelLogo(m,'mini')+'<span class="chn">'+esc(m.xtream_name)+(m.quality?'<span class="tag">'+esc(m.quality)+'</span>':'')+'</span></span>'
        +'<span class="chbtns">'+playbtns(m.stream_id,m.xtream_name,m.url)+'</span></div>';
    }
    html+='</div>';
    if(strictResults.length>7)html+='<button class="ghost ppvexpand" onclick="togglePpv(this)" data-more="'+(strictResults.length-7)+'">Show '+(strictResults.length-7)+' more</button>';
  }
  // Broadcaster rows are sorted country then broadcaster.
  if(rows.length){
    html+='<div class="bcastlist">';
    rows.forEach(function(row,ri){
      // channels matched to this broadcaster
      const chans=(f.matches||[]).filter(function(m){return m.matched===row.bcast&&(!m.country||m.country===row.cc.toUpperCase());}).sort(function(a,b){const rank=fixtureChannelRank(b,f)-fixtureChannelRank(a,f);return rank||preferredChannelSort(a,b);});
      const rid='f'+fi+'b'+ri;
      html+='<div class="bcrow" data-exp="'+rid+'">'
        +'<div class="bchead"><span class="cc">'+esc(row.cc)+'</span> <span class="bcname">'+esc(row.bcast)+'</span>'
        +' <span class="muted exphint">'+(chans.length?(chans.length+' '+tr(chans.length===1?'channel':'channels')):tr('No matching channels'))+'</span><span class="bcchevron">&#9662;</span></div>'
        +'<div class="bcchans hide" id="'+rid+'">';
      if(chans.length){
        for(const [channelIndex,m] of chans.entries()){
          const fav=_favChanSet.has(String(m.stream_id))?' on':'';
          html+='<div class="chline'+(channelIndex>=10?' bcchanextra hide':'')+'"><span class="matchchan"><span class="favstar'+fav+'" data-sid="'+escAttr(String(m.stream_id))+'" data-name="'+escAttr(m.xtream_name)+'" data-cat="'+escAttr(m.category||'')+'" title="Favorite">&#9733;</span>'
            +channelLogo(m,'mini')+'<span class="chn">'+esc(m.xtream_name)+(fixtureChannelRank(m,f)===3?'<span class="tag">'+esc(tr('Best match'))+'</span>':'')+(m.quality?'<span class="tag">'+esc(m.quality)+'</span>':'')+'</span></span>'
            +'<span class="chbtns">'+playbtns(m.stream_id,m.xtream_name,m.url)+'</span></div>';
        }
        if(chans.length>10)html+='<button class="ghost bcchanexpand" onclick="toggleBroadcasterCandidates(this)" data-more="'+(chans.length-10)+'">'+esc(tr('Show more channels'))+' ('+(chans.length-10)+')</button>';
      }else{
        html+='<div class="muted" style="padding:6px 10px">No channels in your list match this broadcaster.</div>';
      }
      html+='</div></div>';
    });
    html+='</div>';
  }
  if(!rows.length&&!strictResults.length){
    if(!Object.keys(f.by_country||{}).length)html+='<div class="muted">No TV channels found.</div>';
    else html+='<div class="muted">No Xtream channels matched. Try lowering strictness.</div>';
  }
  html+='</div></div>';
  return html;
}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function togglePpv(btn){
  const list=btn.previousElementSibling, extras=list?list.querySelectorAll('.ppvextra'):[];
  if(!extras.length)return;
  const opening=extras[0].classList.contains('hide');
  extras.forEach(el=>el.classList.toggle('hide',!opening));
  btn.textContent=opening?'Show less':'Show '+btn.dataset.more+' more';
}
function escAttr(s){return esc(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function channelLogo(c,extra){
  if(!c||!c.logo||c.stream_id===undefined||c.stream_id===null)return '';
  return '<img class="chanlogo'+(extra?' '+extra:'')+'" src="/api/channel_logo?id='+encodeURIComponent(String(c.stream_id))+'" alt="" loading="lazy" onerror="this.remove()">';
}
function playbtns(sid,name,url,showCopy){
  const s=escAttr(String(sid)), n=escAttr(name||''), u=escAttr(url);
  return '<button class="btnplay" data-sid="'+s+'" data-name="'+n+'">&#9658; Play</button>'
    +'<button class="btnvlc" data-sid="'+s+'">&#9658; VLC</button>'
    +(showCopy?'<button class="copy" data-url="'+u+'">'+tr('Copy URL')+'</button>':'');
}
let _hls=null,_mpegts=null;
function destroyMpegtsPlayer(p){
  if(!p)return;
  try{p.pause();}catch(e){}try{p.unload();}catch(e){}try{p.detachMediaElement();}catch(e){}try{p.destroy();}catch(e){}
}
function startSmartStream(video,urls,setStatus,setEngine){
  let stopped=false,hls=null,tsPlayer=null,mediaRecoveries=0;
  function status(s){if(!stopped&&setStatus)setStatus(s||'');}
  function clear(){if(hls){try{hls.destroy();}catch(e){}hls=null;}if(tsPlayer){destroyMpegtsPlayer(tsPlayer);tsPlayer=null;}}
  function publish(){if(setEngine)setEngine(hls,tsPlayer);}
  function startTs(reason){
    clear();publish();
    if(stopped)return;
    if(!(window.mpegts&&mpegts.isSupported&&mpegts.isSupported())||!urls.ts){status('Could not play this stream in the browser. Try VLC.');return;}
    status(reason?'HLS unavailable — trying MPEG-TS...':'Trying MPEG-TS...');
    try{
      tsPlayer=mpegts.createPlayer({type:'mpegts',isLive:true,url:'/api/proxy?u='+encodeURIComponent(urls.ts)},
        {enableWorker:true,lazyLoad:false,autoCleanupSourceBuffer:true,autoCleanupMaxBackwardDuration:60,autoCleanupMinBackwardDuration:30});
      tsPlayer.attachMediaElement(video);publish();
      let failed=false;
      tsPlayer.on(mpegts.Events.ERROR,function(){if(failed||stopped)return;failed=true;status('Could not play this stream in the browser. Try VLC.');});
      tsPlayer.load();
      const played=tsPlayer.play();if(played&&played.catch)played.catch(()=>{});
      video.addEventListener('playing',function onTsPlaying(){video.removeEventListener('playing',onTsPlaying);status('');},{once:true});
    }catch(e){status('Could not play this stream in the browser. Try VLC.');}
  }
  function startHls(src,viaProxy){
    clear();publish();if(stopped)return;
    if(window.Hls&&Hls.isSupported()){
      status(viaProxy?'Routing HLS through local relay...':'Loading HLS...');
      hls=new Hls({manifestLoadingTimeOut:12000,levelLoadingTimeOut:12000,fragLoadingTimeOut:20000,backBufferLength:30,maxBufferLength:45});publish();
      hls.on(Hls.Events.ERROR,function(ev,data){
        if(stopped||!data.fatal)return;
        if(data.type===Hls.ErrorTypes.MEDIA_ERROR&&mediaRecoveries<2){mediaRecoveries++;status('Recovering browser playback...');try{hls.recoverMediaError();return;}catch(e){}}
        if(!viaProxy){startHls('/api/proxy?u='+encodeURIComponent(urls.hls),true);return;}
        startTs(true);
      });
      hls.on(Hls.Events.MANIFEST_PARSED,function(){status('');const p=video.play();if(p&&p.catch)p.catch(()=>{});});
      hls.loadSource(src);hls.attachMedia(video);return;
    }
    if(video.canPlayType('application/vnd.apple.mpegurl')){
      status('Loading HLS...');video.src=src;
      const nativeError=function(){video.removeEventListener('error',nativeError);if(!viaProxy)startHls('/api/proxy?u='+encodeURIComponent(urls.hls),true);else startTs(true);};
      video.addEventListener('error',nativeError,{once:true});
      const p=video.play();if(p&&p.catch)p.catch(()=>{});return;
    }
    startTs(true);
  }
  startHls(urls.hls,false);
  return {stop:function(){stopped=true;clear();publish();try{video.pause();video.removeAttribute('src');video.load();}catch(e){}}};
}
async function playBrowser(sid,name){
  const modal=document.getElementById('playerModal');
  const video=document.getElementById('pVideo');
  const msg=document.getElementById('pMsg');
  document.getElementById('pTitle').textContent=name||'Player';
  msg.textContent='Loading...';
  modal.classList.remove('hide');
  setPopupPlayerMax(false);
  document.body.classList.add('tvsectionplay');
  syncSectionPlayerLayout();
  // get the hls url
  if(modal._playbackController){modal._playbackController.stop();modal._playbackController=null;}
  if(_hls){try{_hls.destroy();}catch(e){}_hls=null;}if(_mpegts){destroyMpegtsPlayer(_mpegts);_mpegts=null;}
  let urls;
  try{urls=await api('/api/hls?id='+encodeURIComponent(sid));if(urls.error||!urls.hls)throw new Error('stream url');}catch(e){msg.textContent='Could not build stream URL.';return;}
  const controller=startSmartStream(video,urls,s=>msg.textContent=s,function(h,t){_hls=h;_mpegts=t;});
  modal._playbackController=controller;
}
function playerFullscreenElement(){
  return document.fullscreenElement||document.webkitFullscreenElement||null;
}
function toggleBroadcasterCandidates(btn){
  const box=btn.parentElement,extras=box?box.querySelectorAll('.bcchanextra'):[];
  if(!extras.length)return;
  const opening=extras[0].classList.contains('hide');
  extras.forEach(el=>el.classList.toggle('hide',!opening));
  btn.textContent=opening?tr('Show fewer channels'):(tr('Show more channels')+' ('+btn.dataset.more+')');
}
function syncSectionPlayerLayout(){
  // Force the already-open section to adopt its constrained player layout on
  // the first frame. Previously this happened reliably only after navigating
  // away and back, especially at 1920x1080.
  void document.body.offsetWidth;
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    if(!racingView.classList.contains('hide')){
      const drivers=document.getElementById('racingDrivers');
      if(drivers)drivers.innerHTML=racingDriversHtml(_racingDriverRows,_racingEventRows);
      renderRacingScheduleCards();
    }
    window.dispatchEvent(new Event('resize'));
  }));
}
function requestPlayerFullscreen(el){
  if(!el)return false;
  const fn=el.requestFullscreen||el.webkitRequestFullscreen;
  if(!fn)return false;
  try{const p=fn.call(el);if(p&&p.catch)p.catch(()=>{});return true;}catch(e){return false;}
}
function exitPlayerFullscreen(){
  const fn=document.exitFullscreen||document.webkitExitFullscreen;
  if(!fn)return false;
  try{const p=fn.call(document);if(p&&p.catch)p.catch(()=>{});return true;}catch(e){return false;}
}
function setPopupPlayerMax(maximized){
  const modal=document.getElementById('playerModal'),btn=document.getElementById('pMinBtn'),hit=document.getElementById('pVideoHit');
  modal.classList.toggle('sectionmax',!!maximized);
  const label=maximized?'Exit fullscreen':'Fullscreen player';
  if(btn){btn.title=label;btn.setAttribute('aria-label',label);btn.textContent=maximized?'\u2198':'\u2196';}
  if(hit)hit.setAttribute('aria-label',label);
}
function togglePopupPlayerSize(){
  const modal=document.getElementById('playerModal');
  if(playerFullscreenElement()===modal){setPopupPlayerMax(false);exitPlayerFullscreen();return;}
  setPopupPlayerMax(true);
  requestPlayerFullscreen(modal);
}
function syncPlayerFullscreenExit(){
  if(playerFullscreenElement())return;
  const modal=document.getElementById('playerModal');
  if(modal&&modal.classList.contains('sectionmax'))setPopupPlayerMax(false);
  const slot=document.getElementById('tvPlayerSlot');
  if(slot&&slot.classList.contains('sectionmax')&&mytvView.classList.contains('hide'))tvSetMini(true);
}
document.addEventListener('fullscreenchange',syncPlayerFullscreenExit);
document.addEventListener('webkitfullscreenchange',syncPlayerFullscreenExit);
function closePlayer(){
  const modal=document.getElementById('playerModal');
  const video=document.getElementById('pVideo');
  if(modal._playbackController){modal._playbackController.stop();modal._playbackController=null;}
  if(_hls){try{_hls.destroy();}catch(e){}_hls=null;}if(_mpegts){destroyMpegtsPlayer(_mpegts);_mpegts=null;}
  video.pause();video.removeAttribute('src');video.load();
  modal.classList.add('hide');
  modal.classList.remove('sectionmax');
  const liveAway=(_tvPlaying!==null||window._tvPlaybackController)&&mytvView.classList.contains('hide');
  if(!liveAway)document.body.classList.remove('tvsectionplay');
}
async function playVLC(sid,btn){
  const old=btn?btn.textContent:'';
  if(btn){btn.textContent='Opening...';}
  try{
    const j=await api('/api/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stream_id:sid})});
    if(j.error){alert(j.error||'Could not launch VLC.');}
  }catch(e){alert('Could not launch VLC.');}
  if(btn){setTimeout(()=>{btn.textContent=old;},1200);}
}
let _favMovieSet=new Set();
function cleanMovieSearchTitle(name){
  return String(name||'')
    .replace(/^\\s*.+?\\s+-\\s+/,'')
    .replace(/^\\s*.{1,40}?\\s*\\|\\s*/,'')
    .replace(/\\s*\\((?:US|UK|GB|NO|EN|SE|DK|FI)\\)\\s*$/i,'')
    .replace(/\\s*\\((?:19|20)\\d{2}\\)\\s*$/,'')
    .trim();
}
function movieCard(m,showYear,recent){
  const sid=escAttr(String(m.stream_id==null?'':m.stream_id)), ext=escAttr(m.extension||'mp4'), key=String(m.catalog_id||m.stream_id||'');
  const fav=_favMovieSet.has(key)?' on':'';
  const displayName=recent?cleanMovieSearchTitle(m.name):m.name;
  const poster=m.cover?'<img src="'+escAttr(m.cover)+'" alt="" loading="lazy" onerror="this.parentElement.textContent=String.fromCodePoint(127916)">':'&#127916;';
  let meta='';
  if(showYear&&m.year)meta+=esc(m.year);
  if(m.rating)meta+=(meta?' &nbsp; ':'')+'Rating: '+esc(m.rating);
  const cardClass='moviecard'+(recent?' recentmovie':'');
  const cardData=recent?' data-query="'+escAttr(cleanMovieSearchTitle(m.name))+'"':'';
  return '<div class="'+cardClass+'"'+cardData+'><div class="movieposter">'+poster+'</div><div class="movieinfo"><div class="movietitle">'+esc(displayName)+'</div>'
    +(meta?'<div class="moviemeta">'+meta+'</div>':'')
    +'<div class="movieactions"><span class="favstar moviestar'+fav+'" data-key="'+escAttr(key)+'" data-catalog="'+escAttr(m.catalog_id||'')+'" data-sid="'+sid+'" data-name="'+escAttr(m.name||'')+'" data-ext="'+ext+'" data-year="'+escAttr(m.year||'')+'" data-rating="'+escAttr(m.rating||'')+'" data-cover="'+escAttr(m.cover||'')+'" title="Favorite">&#9733;</span>'
    +(recent?'':(m.stream_found?'<button class="btnvlc movievlc" data-sid="'+sid+'" data-ext="'+ext+'">&#9658; VLC</button>':'<button class="ghost" disabled>'+tr('Not available')+'</button>'))+'</div></div></div>';
}
async function loadRecentMovies(limit){
  limit=limit||9;
  const el=document.getElementById('recentMovieList');
  const more=document.getElementById('recentMovieMore');
  el.innerHTML='<span class="muted">Loading...</span>';
  more.classList.add('hide');
  const r=await api('/api/recent_movies?limit='+limit);
  if(typeof r.logged_in==='boolean')setMovieProviderLayout(r.logged_in);
  if(r.error){el.innerHTML='<span class="muted">Could not load recently added movies.</span>';return false;}
  if(!r.logged_in){el.innerHTML='<span class="muted">Log in via Settings first.</span>';return false;}
  if(!r.movies.length){el.innerHTML='<span class="muted">No recent movies found.</span>';return false;}
  await loadMovieFavorites();
  el.innerHTML='<div class="moviegrid" style="margin-top:0">'+r.movies.map(m=>movieCard(m,false,true)).join('')+'</div>';
  if(limit<36&&r.has_more)more.classList.remove('hide');
  return true;
}
let _movieCatalog='popular',_movieCatalogCache={};
function setMovieProviderLayout(loggedIn){
  const catalogs=document.getElementById('movieCatalogs');
  if(catalogs)catalogs.classList.toggle('noxtream',!loggedIn);
  const refresh=document.getElementById('movieRefreshBtn');
  if(refresh)refresh.classList.toggle('hide',!loggedIn);
}
async function loadCinemetaMovies(catalog){
  _movieCatalog=['popular','new','featured'].includes(catalog)?catalog:'popular';
  document.querySelectorAll('[data-movie-catalog]').forEach(function(btn){btn.classList.toggle('on',btn.dataset.movieCatalog===_movieCatalog);});
  const el=document.getElementById('cinemetaMovieList');
  if(!el)return;
  const cached=_movieCatalogCache[_movieCatalog];
  if(cached){
    setMovieProviderLayout(cached.logged_in);
    el.innerHTML='<div class="moviegrid" style="margin-top:0">'+cached.movies.map(m=>movieCard(m,true,false)).join('')+'</div>';
    return;
  }
  el.innerHTML='<span class="muted">Loading...</span>';
  try{
    const r=await api('/api/movie_catalog?catalog='+encodeURIComponent(_movieCatalog)+'&limit=10');
    if(typeof r.logged_in==='boolean')setMovieProviderLayout(r.logged_in);
    if(r.error)throw new Error(r.error);
    if(!r.movies.length){el.innerHTML='<span class="muted">No movies found.</span>';return;}
    _movieCatalogCache[_movieCatalog]={movies:r.movies,logged_in:!!r.logged_in};
    await loadMovieFavorites();
    el.innerHTML='<div class="moviegrid" style="margin-top:0">'+r.movies.map(m=>movieCard(m,true,false)).join('')+'</div>';
  }catch(e){el.innerHTML='<span class="muted">Could not load movie catalog.</span>';}
}
async function checkMovies(btn){
  const old=btn.innerHTML;btn.disabled=true;btn.textContent='Checking for new movies...';
  try{
    const j=await api('/api/check_movie_updates',{method:'POST'});
    if(j.error)throw new Error(j.error||'movie refresh failed');
    await loadRecentMovies(9);
    if(j.new_movies>0)toast('Found '+j.new_movies+' new movie'+(j.new_movies===1?'':'s'),7000);
    else toast('Movie library is up to date.',7000);
  }catch(e){toast('Could not refresh movie library.',7000);}
  btn.disabled=false;btn.innerHTML=old;
}
async function expandRecentMovies(btn){
  const old=btn.textContent;btn.disabled=true;btn.textContent='Loading...';
  await loadRecentMovies(36);
  btn.disabled=false;btn.textContent=old;btn.classList.add('hide');
}
async function loadMovieFavorites(){
  const r=await api('/api/favorites');
  const movies=r.movies||[];
  _favMovieSet=new Set(movies.map(m=>String(m.catalog_id||m.stream_id)));
  const el=document.getElementById('movieFavList');
  if(!movies.length){el.innerHTML='<span class="muted">No favorite movies yet.</span>';return;}
  let h='';
  for(const m of movies){
    const key=escAttr(String(m.catalog_id||m.stream_id));
    const cleanName=cleanMovieSearchTitle(m.name);
    const poster='<span class="moviefavposter">&#127916;'+(m.cover?'<img src="'+escAttr(m.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'</span>';
    h+='<div class="moviefav" data-query="'+escAttr(cleanName)+'">'+poster+'<div class="moviefavinfo"><div class="moviefavname">'+esc(cleanName)+'</div></div>'
      +'<span class="favstar on movieremove" data-key="'+key+'" title="Remove from favorites">&#9733;</span></div>';
  }
  el.innerHTML=h;
}
async function toggleMovieFavorite(movie,starEl){
  const r=await favPost({action:'toggle_movie',movie:movie});
  _favMovieSet=new Set((r.movie_ids||[]).map(String));
  if(_favMovieSet.has(String(movie.catalog_id||movie.stream_id)))_profileConfig.setup_demo_content=false;
  if(starEl)starEl.classList.toggle('on',_favMovieSet.has(String(movie.catalog_id||movie.stream_id)));
  await loadMovieFavorites();
}
async function removeMovieFavorite(key){
  await favPost({action:'remove_movie',favorite_key:key});
  _favMovieSet.delete(String(key));
  document.querySelectorAll('.moviestar').forEach(el=>{if(el.getAttribute('data-key')===String(key))el.classList.remove('on');});
  await loadMovieFavorites();
}
async function searchMovies(){
  const q=(document.getElementById('movieQ').value||'').trim();
  const el=document.getElementById('movieResults');
  const catalogs=document.getElementById('movieCatalogs');
  if(!q){catalogs.classList.remove('hide');el.innerHTML='<div class="muted" style="margin-top:14px">Enter a movie title.</div>';return;}
  catalogs.classList.add('hide');
  el.innerHTML='<div class="muted" style="margin-top:14px">Searching movies...</div>';
  const r=await api('/api/movies?q='+encodeURIComponent(q));
  const back='<div class="movieresultback"><button class="ghost" onclick="backToMyMovies()">&#8592; '+tr('Back to Movies')+'</button></div>';
  if(r.error){el.innerHTML=back+'<div class="err movieresultstatus">'+esc(r.error)+'</div>';return;}
  if(!r.movies.length){el.innerHTML=back+'<div class="muted movieresultstatus">No movies found for &quot;'+esc(q)+'&quot;.</div>';return;}
  await loadMovieFavorites();
  let h=back+'<div class="muted movieresultstatus">'+r.movies.length+' result'+(r.movies.length===1?'':'s')+'</div><div class="moviegrid">';
  for(const m of r.movies)h+=movieCard(m,true);
  el.innerHTML=h+'</div>';
}
function backToMyMovies(){
  document.getElementById('movieQ').value='';
  document.getElementById('movieResults').innerHTML='';
  document.getElementById('movieCatalogs').classList.remove('hide');
}
async function playMovieVLC(sid,ext,btn){
  const old=btn.textContent;btn.textContent='Opening...';
  try{
    const j=await api('/api/play_movie',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stream_id:sid,extension:ext})});
    if(j.error)alert(j.error||'Could not launch VLC.');
  }catch(e){alert('Could not launch VLC.');}
  setTimeout(()=>{btn.textContent=old;},1200);
}
let _wishlistGames=[],_wishlistLinked=false;
async function loadSteamProfile(){
  const el=document.getElementById('steamProfile');if(!el)return;
  try{
    const p=await api('/api/steam_profile');
    if(!p||!p.linked){el.innerHTML='<div class="steamprofileempty">Link a Steam wishlist to show your Steam profile here.</div>';return;}
    if(!p.display_name&&!p.avatar){el.innerHTML='<div class="steamprofileempty">Steam profile is linked, but its public profile details are unavailable.</div>';return;}
    const avatarSrc=p.avatar_local||p.avatar||'',avatarFallback=(p.avatar_local&&p.avatar)?p.avatar:'';
    const avatar=avatarSrc?'<img class="steamprofileavatar" src="'+escAttr(avatarSrc)+'" data-fallback="'+escAttr(avatarFallback)+'" alt="" referrerpolicy="no-referrer" onerror="if(this.dataset.fallback&&!this.dataset.tried){this.dataset.tried=1;this.src=this.dataset.fallback;}else this.remove()">':'';
    const real=p.real_name?'<div class="steamprofilereal">'+esc(p.real_name)+'</div>':'';
    const loc=p.location?'<div class="steamprofileloc">'+esc(p.location)+'</div>':'';
    const level=(p.level!==undefined&&p.level!==null)?'<span class="steamlevel" title="Steam level">'+esc(p.level)+'</span>':'';
    const years=(p.years_service!==undefined&&p.years_service!==null)?'<span class="steamyears">'+esc(p.years_service)+' years of service</span>':'';
    const summary=p.summary?'<div class="steamprofilesummary">'+esc(p.summary)+'</div>':'';
    el.innerHTML='<div class="steamprofileinner"><a class="steamprofilelink" href="'+escAttr(p.profile_url||'#')+'" target="_blank" rel="noopener noreferrer"><div class="steamprofilehead">'+avatar+'<div><div class="steamprofilename">'+esc(p.display_name||'Steam')+'</div>'+real+loc+'</div></div></a><div class="steamprofilemeta">'+level+years+'</div>'+summary+'</div>';
  }catch(e){el.innerHTML='<div class="steamprofileempty">Steam profile details unavailable.</div>';}
}
function updateSteamWishlistHelp(){
  const hasGames=_wishlistLinked&&_wishlistGames.length>0,el=document.getElementById('steamWishlistHelp'),filterRow=document.getElementById('gameWishlistFilterRow');
  if(el)el.classList.toggle('hide',hasGames);
  if(filterRow)filterRow.classList.toggle('hide',!hasGames);
  const settings=document.getElementById('steamWishlistSettings'),quick=document.getElementById('steamWishlistQuickBtn');
  if(settings&&!_wishlistLinked)settings.classList.remove('hide');
  if(quick)quick.classList.toggle('hide',!_wishlistLinked);
}
function toggleSteamWishlistSettings(){const el=document.getElementById('steamWishlistSettings');if(el)el.classList.toggle('hide');}
function renderGameWishlist(){
  const el=document.getElementById('gameWishlist');if(!el)return;
  const input=document.getElementById('gameWishlistFilter'),q=String((input&&input.value)||'').trim().toLowerCase();
  const games=q?_wishlistGames.filter(g=>String(g.name||'').toLowerCase().includes(q)):_wishlistGames;
  if(!games.length){el.innerHTML='<span class="muted">'+(q?'No matching wishlist games.':'No Steam wishlist games yet.')+'</span>';return;}
  const now=Date.now();
  el.innerHTML=games.map(g=>{const url=g.url||('https://store.steampowered.com/app/'+encodeURIComponent(String(g.app_id||''))+'/'),releaseTs=Date.parse(g.released||''),countdown=Number.isFinite(releaseTs)&&releaseTs>now?racingCountdown({start:g.released}):'',release=(g.release_text||countdown)?'<div class="gamecardrelease">'+(g.release_text?'<div class="moviemeta">'+esc(g.release_text)+'</div>':'')+(countdown?'<div class="gamecountdown">'+esc(countdown)+'</div>':'')+'</div>':'';return '<a class="gamecard wishlistgame" href="'+escAttr(url)+'" target="_blank" rel="noopener noreferrer">'+(g.cover?'<img src="'+escAttr(g.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'<div class="gamecardbody"><div class="gamecardname">'+esc(g.name||'Game')+'</div>'+release+'</div></a>';}).join('');
}
async function loadGameFavorites(){
  const r=await api('/api/favorites'),now=Date.now(),games=(r.games||[]).filter(g=>g.wishlist_imported),el=document.getElementById('gameWishlist');
  games.sort((a,b)=>{
    const at=Date.parse(a.released||''),bt=Date.parse(b.released||''),af=Number.isFinite(at)&&at>now,bf=Number.isFinite(bt)&&bt>now,ap=Number.isFinite(at)&&at<=now,bp=Number.isFinite(bt)&&bt<=now;
    if(af!==bf)return af?-1:1;
    if(af&&bf)return at-bt;
    if(ap!==bp)return ap?-1:1;
    if(ap&&bp)return bt-at;
    return String(a.name||'').localeCompare(String(b.name||''));
  });
  _wishlistGames=games;renderGameWishlist();updateSteamWishlistHelp();
}
async function loadSteamWishlistSetting(){
  try{
    const c=await api('/api/config'),input=document.getElementById('steamWishlistQ'),btn=document.getElementById('steamWishlistBtn'),status=document.getElementById('steamWishlistStatus');
    const linked=!!String(c.steam_wishlist_url||'').trim();
    _wishlistLinked=linked;updateSteamWishlistHelp();
    if(input){input.value=c.steam_wishlist_url||'';input.readOnly=false;}
    if(btn){btn.textContent=tr('Sync wishlist');btn.setAttribute('data-i18n','Sync wishlist');}
    const settings=document.getElementById('steamWishlistSettings');if(settings&&linked)settings.classList.add('hide');
    if(linked&&status&&c.steam_wishlist_synced_at){
      const locale=_lang==='no'?'nb-NO':undefined;
      status.textContent='Last refreshed '+new Date(Number(c.steam_wishlist_synced_at)*1000).toLocaleString(locale);
    }
  }catch(e){}
  loadSteamProfile();
}
async function syncSteamWishlist(btn){
  const input=document.getElementById('steamWishlistQ'),status=document.getElementById('steamWishlistStatus'),value=(input.value||'').trim();
  if(!value){status.textContent='Enter your public Steam wishlist URL.';return;}
  const old=btn.textContent;btn.disabled=true;btn.textContent='Syncing...';status.textContent='Reading Steam wishlist...';
  try{
    const j=await api('/api/import_steam_wishlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:value})});
    if(j.error)throw new Error(j.error||'Wishlist sync failed');
    await loadGameFavorites();loadFavorites();await loadSteamWishlistSetting();status.textContent='Synced '+j.imported+' games from Steam.';
  }catch(e){status.textContent='Could not sync wishlist: '+e.message;}
  btn.disabled=false;if(btn.textContent==='Syncing...')btn.textContent=old;
}
let _steamWishlistAutoChecked=false;
async function maybeAutoRefreshSteamWishlist(c){
  if(_steamWishlistAutoChecked)return;
  _steamWishlistAutoChecked=true;
  if(c&&c.games_enabled===false)return;
  const url=String((c&&c.steam_wishlist_url)||'').trim(),synced=Number((c&&c.steam_wishlist_synced_at)||0)*1000;
  if(!url||(synced&&Date.now()-synced<7*24*60*60*1000))return;
  try{
    const j=await api('/api/import_steam_wishlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url})});
    if(j.error)return;
    if(!gamesView.classList.contains('hide')){await loadGameFavorites();await loadSteamWishlistSetting();}
    if(!mylistView.classList.contains('hide'))loadFavorites();
  }catch(e){}
}
const _RACING_SERIES=[['f1','Formula 1'],['f2','Formula 2'],['f3','Formula 3'],['indycar','IndyCar'],['wec','WEC'],['formulae','Formula E'],['motogp','MotoGP'],['wrc','WRC Rally']];
const _RACING_LOGOS={
  f1:'https://www.formula1.com/etc/designs/fom-website/images/f1_logo.svg',
  f2:'https://upload.wikimedia.org/wikipedia/commons/6/64/FIA_Formula_2_Championship_logo_%282026%29.svg',
  f3:'https://upload.wikimedia.org/wikipedia/commons/d/d9/FIA_Formula_3_Championship_logo_%282026%29.svg',
  indycar:'https://www.indycar.com/-/media/IndyCar/News/Standard/2022/08/08-03-INDYCAR-Logo.jpg',
  wec:'https://upload.wikimedia.org/wikipedia/commons/4/4c/FIA_WEC_Logo_2024.png',
  formulae:'https://upload.wikimedia.org/wikipedia/commons/a/a4/FIA_Formula_E_World_Championship_Logo.svg',
  motogp:'https://upload.wikimedia.org/wikipedia/commons/f/f9/MotoGP_logo_%282024%29.svg',
  wrc:'https://www.canevarally.com/wp-content/uploads/2025/01/311439623_179650897941587_4314845745939251693_n-3-e1714140994320-1302x558-1.jpg'
};
let _racingSelected=new Set(['f1']);
let _racingDriverRows=[],_racingEventRows=[],_racingDetailKey='';
function racingEventIsLive(event,now){
  now=now||Date.now();const start=new Date(event.start).getTime();if(!Number.isFinite(start))return false;
  const explicit=event.end?new Date(event.end).getTime():NaN;
  if(Number.isFinite(explicit))return now>=start-(event.all_day?12*3600000:0)&&now<=explicit;
  let duration=2*3600000;
  if(event.all_day)duration=24*3600000;
  else if(String(event.session||'').toLowerCase()==='race')duration=4*3600000;
  return now>=start&&now<=start+duration;
}
function nextDriverRace(driver,events,now){
  now=now||Date.now();
  return (events||[]).filter(e=>String(e.series||'')===String(driver.series||'')&&(driver.series!=='f1'||String(e.session||'').toLowerCase()==='race')).map(e=>({event:e,ts:new Date(e.start).getTime()})).filter(x=>Number.isFinite(x.ts)&&x.ts>=now-6*3600000).sort((a,b)=>a.ts-b.ts)[0]?.event||null;
}
function racingDetailWhen(event){
  if(!event)return '';
  const d=new Date(event.start),locale=_lang==='no'?'nb-NO':undefined;
  return event.all_day?(event.date_text||d.toLocaleDateString(locale,{weekday:'long',day:'numeric',month:'long'})):d.toLocaleString(locale,{weekday:'long',day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'});
}
function racingCountdown(event){
  if(!event)return '';
  const target=new Date(event.start),now=new Date(),remaining=target-now;if(!Number.isFinite(remaining))return '';
  if(remaining<=0&&racingEventIsLive(event,now.getTime()))return 'LIVE';
  if(remaining<=0)return '';
  const minutes=Math.max(1,Math.ceil(remaining/60000));
  if(minutes<60)return tr('in')+' '+minutes+' '+tr(minutes===1?'minute':'minutes');
  if(remaining<24*3600000){const hours=Math.ceil(minutes/60);return tr('in')+' '+hours+' '+tr(hours===1?'hour':'hours');}
  const days=Math.max(1,Math.round(osloDayNumber(target)-osloDayNumber(now)));
  return tr('in')+' '+days+' '+tr(days===1?'day':'days');
}
function racingArtError(img){
  const fallback=String(img.dataset.fallback||'');
  if(fallback){img.dataset.fallback='';img.src=fallback;return;}
  img.style.display='none';const mark=img.nextElementSibling;if(mark)mark.style.display='flex';
}
function racingEventVisual(event,countdown){
  const series=String((event&&event.series)||''),seriesLogo=String(_RACING_LOGOS[series]||''),eventLogo=(series==='wrc'?String((event&&event.art)||''):'');
  const src=eventLogo||seriesLogo;
  const fallback='<div class="racingeventfallback"'+(src?' style="display:none"':'')+'>'+racingSeriesLogo(series)+'</div>';
  let image='';
  if(src){const fb=eventLogo?seriesLogo:'';image='<img src="'+escAttr(src)+'" data-fallback="'+escAttr(fb)+'" alt="" loading="lazy" onerror="racingArtError(this)">';}
  return '<div class="racingeventvisual">'+image+fallback+(countdown?'<div class="racingdetailcountdown">'+esc(countdown)+'</div>':'')+'</div>';
}
function racingDetailNext(event){
  if(!event)return '<div class="racingdetailnext"><div class="racingdetailnextlabel">'+esc(tr('Next race'))+'</div><span class="muted">'+esc(tr('No upcoming race found.'))+'</span></div>';
  const countdown=racingCountdown(event);
  return '<div class="racingdetailnext"><div class="racingdetailnextlabel">'+esc(tr('Next race'))+'</div><div class="racingdetailnextgrid"><div><b>'+esc(event.race||event.circuit||'Race')+'</b><div class="racingdetailmeta">'+esc(racingDetailWhen(event))+(event.session?'<br>'+esc(event.session):'')+(event.circuit&&event.circuit!==event.race?'<br>'+esc(event.circuit):'')+'</div></div>'+racingEventVisual(event,countdown)+'</div></div>';
}
function racingSeriesLogo(key){const src=String(_RACING_LOGOS[key]||'');return src?'<img class="racingserieslogo" src="'+escAttr(src)+'" alt="" loading="lazy" onerror="this.remove()">':'';}
function renderRacingTeamControl(){
  const control=document.getElementById('racingF1TeamControl'),label=document.getElementById('racingTeamLabel'),btn=document.getElementById('racingF1ChooseBtn'),picker=document.getElementById('racingF1Picker');if(!control||!label||!btn)return;
  const f1=_racingDriverRows.filter(driver=>driver.series==='f1'),f1Mode=_racingDetailKey==='f1-team'||(_racingDriverRows.find(row=>String(row.key||'')===String(_racingDetailKey||''))||{}).series==='f1';
  if(f1Mode){
    const driver=f1[0]||{},teamId=String(driver.team_id||'');control.classList.remove('hide');label.textContent='Formula 1 Team';btn.classList.remove('readonly');btn.setAttribute('onclick','toggleRacingF1Picker()');
    btn.innerHTML=driver.team?((teamId?'<img src="/api/f1_team_logo?id='+encodeURIComponent(teamId)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'<span>'+esc(driver.team)+'</span>'):'<span>'+esc(tr('Choose F1 team'))+'</span>';
    return;
  }
  const driver=_racingDriverRows.find(row=>String(row.key||'')===String(_racingDetailKey||''));
  if(!driver){control.classList.add('hide');return;}
  control.classList.remove('hide');label.textContent=(driver.series_name||'Racing')+' Team';btn.classList.add('readonly');btn.removeAttribute('onclick');if(picker)picker.classList.add('hide');
  const logo=String(driver.team_logo||'');btn.innerHTML=(logo?'<img src="'+escAttr(logo)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'<span>'+esc(driver.team||driver.series_name||'Racing')+'</span>';
}
function renderRacingDriverDetail(){
  const el=document.getElementById('racingDriverDetail');if(!el)return;
  const now=Date.now(),f1=_racingDriverRows.filter(driver=>driver.series==='f1');
  if(_racingDetailKey==='f1-team'&&f1.length>=2){
    const first=f1[0],teamId=String(first.team_id||''),live=_racingEventRows.filter(e=>e.series==='f1').some(e=>racingEventIsLive(e,now)),next=nextDriverRace(first,_racingEventRows,now);
    const people=f1.slice(0,2).map(driver=>'<div class="racingdetailperson"><img src="/api/racing_driver_image?id='+encodeURIComponent(String(driver.key||''))+'" alt="" loading="lazy" onerror="this.remove()"><div><b>'+esc(driver.name||'')+'</b>'+(driver.url?'<div class="moviemeta"><a href="'+escAttr(driver.url)+'" target="_blank" rel="noopener noreferrer">'+esc(tr('Driver profile'))+' ↗</a></div>':'')+'</div></div>').join('');
    el.innerHTML='<div class="racingdetailf1hero">'+(teamId?'<img src="/api/f1_team_logo?id='+encodeURIComponent(teamId)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'<div><div class="racingdetailseries">Formula 1'+(live?' · LIVE':'')+'</div><h2>'+esc(first.team||'Formula 1')+'</h2><div class="racingdetailteam">'+esc(f1.slice(0,2).map(d=>d.name).join(' · '))+'</div></div></div><div class="racingdetailpeople">'+people+'</div>'+racingDetailNext(next);
    return;
  }
  const driver=_racingDriverRows.find(row=>String(row.key||'')===String(_racingDetailKey||''));
  if(!driver){el.innerHTML='<span class="muted">'+esc(tr('Choose a driver to see details.'))+'</span>';return;}
  const live=_racingEventRows.filter(e=>String(e.series||'')===String(driver.series||'')).some(e=>racingEventIsLive(e,now)),next=nextDriverRace(driver,_racingEventRows,now),car=driver.series==='f2';
  el.innerHTML='<div class="racingdetailhero"><img class="'+(car?'car':'')+'" src="/api/racing_driver_image?id='+encodeURIComponent(String(driver.key||''))+'" alt="'+escAttr(driver.name||'')+'" loading="lazy" onerror="this.remove()"><div><div class="racingdetailseries">'+esc(driver.series_name||'Racing')+(live?' · LIVE':'')+'</div><h2>'+esc(driver.name||'')+'</h2><div class="racingdetailteam">'+esc(driver.team||'')+'</div></div></div>'+racingDetailNext(next)+(driver.url?'<div class="racingdetailactions"><a class="ghost" href="'+escAttr(driver.url)+'" target="_blank" rel="noopener noreferrer">'+esc(tr('Driver profile'))+' ↗</a></div>':'');
}
function showRacingDriverDetail(key){
  _racingDetailKey=String(key||'');renderRacingTeamControl();renderRacingDriverDetail();
  const drivers=document.getElementById('racingDrivers');if(drivers)drivers.innerHTML=racingDriversHtml(_racingDriverRows,_racingEventRows);
  renderRacingScheduleCards();
}
function racingDriverHtml(driver,events){
  const now=Date.now(),seriesEvents=(events||[]).filter(e=>String(e.series||'')===String(driver.series||'')),live=seriesEvents.some(e=>racingEventIsLive(e,now)),next=nextDriverRace(driver,events,now),locale=_lang==='no'?'nb-NO':undefined;
  let nextHtml='<span class="muted">'+tr('No upcoming race found.')+'</span>';
  if(next){const d=new Date(next.start),when=next.all_day?(next.date_text||d.toLocaleDateString(locale,{day:'numeric',month:'short'})):d.toLocaleString(locale,{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});nextHtml='<span>'+tr('Next race')+'</span><b>'+esc(next.race||next.circuit||'Race')+'</b><span>'+esc(when)+'</span>';}
  const key=String(driver.key||''),selected=_racingDetailKey===key?' selected':'';
  return '<div class="racingdriver'+selected+'" data-driver-key="'+escAttr(key)+'" onclick="showRacingDriverDetail(this.dataset.driverKey)"><img src="/api/racing_driver_image?id='+encodeURIComponent(key)+'" alt="'+escAttr(driver.name||'')+'" loading="lazy" onerror="this.remove()">'
    +'<div class="racingdriverinfo"><div class="racingdrivername">'+esc(driver.name||'')+(live?'<span class="driverlive">LIVE</span>':'')+'</div><div class="racingdriverteam">'+esc(driver.series_name||'')+(driver.team?' · '+esc(driver.team):'')+'</div><div class="racingdrivernext">'+nextHtml+'</div></div></div>';
}
function racingF1PairHtml(pair,events){
  const now=Date.now(),first=pair[0],seriesEvents=(events||[]).filter(e=>String(e.series||'')==='f1'),live=seriesEvents.some(e=>racingEventIsLive(e,now)),next=nextDriverRace(first,events,now),locale=_lang==='no'?'nb-NO':undefined;
  let nextHtml='<span class="muted">'+tr('No upcoming race found.')+'</span>';
  if(next){const d=new Date(next.start),when=d.toLocaleString(locale,{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});nextHtml='<span>'+tr('Next race')+'</span><b>'+esc(next.race||next.circuit||'Race')+'</b><span>'+esc(when)+'</span>';}
  const pics=pair.map(driver=>'<div class="racingdriverpairperson"><img src="/api/racing_driver_image?id='+encodeURIComponent(String(driver.key||''))+'" alt="'+escAttr(driver.name||'')+'" loading="lazy" onerror="this.remove()"><span>'+esc(driver.name||'')+'</span></div>').join('');
  const names=pair.map(driver=>driver.name||'').filter(Boolean).join(' · '),team=first.team||'Formula 1';
  return '<div class="racingdriver racingdriverpair'+(_racingDetailKey==='f1-team'?' selected':'')+'" data-driver-key="f1-team" onclick="showRacingDriverDetail(this.dataset.driverKey)"><div class="racingdriverpairpics">'+pics+'</div><div class="racingdriverinfo"><div class="racingdrivername">'+esc(team)+(live?'<span class="driverlive">LIVE</span>':'')+'</div><div class="racingdriverteam">Formula 1 · '+esc(names)+'</div><div class="racingdrivernext">'+nextHtml+'</div></div></div>';
}
function racingDriversHtml(rows,events){
  const list=rows||[],f1=list.filter(driver=>driver.series==='f1'),other=list.filter(driver=>driver.series!=='f1'),parts=[];
  if(f1.length>=2)parts.push({key:'f1-team',html:racingF1PairHtml(f1.slice(0,2),events)});
  else for(const driver of f1)parts.push({key:String(driver.key||''),html:racingDriverHtml(driver,events)});
  for(const driver of other)parts.push({key:String(driver.key||''),html:racingDriverHtml(driver,events)});
  parts.sort((a,b)=>(a.key===String(_racingDetailKey||'')?-1:0)-(b.key===String(_racingDetailKey||'')?-1:0));
  return parts.map(part=>part.html).join('');
}
function racingChannelLine(ch){
  return '<div class="racingeventchannel">'+channelLogo(ch,'mini')+'<span class="chn">'+esc(ch.xtream_name||'Channel')+(ch.quality?'<span class="tag">'+esc(ch.quality)+'</span>':'')+'</span><span class="chbtns">'+playbtns(ch.stream_id,ch.xtream_name,ch.url)+'</span></div>';
}
function racingChannelSections(channels){
  const definite=channels.filter(ch=>ch.match_kind==='event').sort(preferredChannelSort),dedicated=channels.filter(ch=>ch.match_kind==='series').sort(preferredChannelSort),possible=channels.filter(ch=>ch.match_kind!=='event'&&ch.match_kind!=='series').sort(preferredChannelSort);
  let h='';
  if(definite.length)h+='<div class="muted">'+esc(tr('Definite event matches'))+'</div>'+definite.map(racingChannelLine).join('');
  if(dedicated.length)h+='<div class="muted" style="margin-top:8px">'+esc(tr('Dedicated series channels'))+'</div>'+dedicated.map(racingChannelLine).join('');
  if(possible.length){
    const groups=new Map();for(const ch of possible){const category=String(ch.category||tr('Other possible channels'));if(!groups.has(category))groups.set(category,[]);groups.get(category).push(ch);}
    h+='<div class="muted" style="margin-top:8px">'+esc(tr('Possible channels by category'))+'</div>';
    for(const [category,items] of groups)h+='<div class="bcrow"><div class="bchead"><span class="bcname">'+esc(category)+'</span><span class="muted">'+items.length+' '+esc(tr(items.length===1?'channel':'channels'))+'</span><span class="bcchevron">&#9662;</span></div><div class="bcchans hide">'+items.map(racingChannelLine).join('')+'</div></div>';
  }
  return h;
}
function racingEventHtml(event){
  const ts=new Date(event.start),locale=_lang==='no'?'nb-NO':undefined;
  const when=event.all_day?(event.date_text||ts.toLocaleDateString(locale,{weekday:'short',day:'numeric',month:'short'})):ts.toLocaleString(locale,{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
  const channels=event.channels||[],tv=channels.length?'<span class="cc racingeventtv">TV</span>':'',details=channels.length?'<div class="racingeventchannels hide">'+racingChannelSections(channels)+'</div>':'';
  return '<div class="racingevent'+(channels.length?' haschannels':'')+'" data-url="'+escAttr(event.url||'')+'"><div class="racingeventtop"><b>'+esc(event.race||event.circuit||'Race')+'</b>'+tv+'</div><div class="moviemeta">'+esc(when)+(event.session?' · '+esc(event.session):'')+(event.circuit&&event.circuit!==event.race?' · '+esc(event.circuit):'')+'</div>'+details+'</div>';
}
function racingAvailabilityKey(event){return [event.series||'',event.race||'',event.session||'',event.start||''].join('|');}
function applyRacingAvailability(map,events){for(const event of (events||[]))event.channels=(map&&map[racingAvailabilityKey(event)])||[];}
function renderRacingScheduleCards(){
  const info=document.getElementById('racingInfo');if(!info)return;const now=Date.now(),groups=new Map();
  const selectedDriver=_racingDriverRows.find(row=>String(row.key||'')===String(_racingDetailKey||''));
  const selectedSeries=_racingDetailKey==='f1-team'?'f1':String((selectedDriver&&selectedDriver.series)||'');
  for(const event of _racingEventRows){const ts=new Date(event.start).getTime();if(!Number.isFinite(ts)||ts<now-24*3600000)continue;const key=event.series||'racing';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(event);}
  let h='';const orderedSeries=_RACING_SERIES.filter(row=>_racingSelected.has(row[0])).sort((a,b)=>(a[0]===selectedSeries?-1:0)-(b[0]===selectedSeries?-1:0));
  for(const row of orderedSeries){const events=(groups.get(row[0])||[]).slice(0,4);h+='<div class="racingcard series-'+escAttr(row[0])+(selectedSeries===row[0]?' selected':'')+'"><h3>'+racingSeriesLogo(row[0])+'<span>'+esc(row[1])+'</span></h3>'+(events.length?events.map(racingEventHtml).join(''):'<span class="muted">'+esc(tr('No upcoming events found.'))+'</span>')+'</div>';}
  info.innerHTML=h||'<span class="muted">'+esc(tr('Choose at least one racing series above.'))+'</span>';
}
async function loadRacingAvailability(){
  try{const a=await api('/api/racing_availability');applyRacingAvailability(a.availability||{},_racingEventRows);renderRacingScheduleCards();renderRacingDriverDetail();const drivers=document.getElementById('racingDrivers');if(drivers)drivers.innerHTML=racingDriversHtml(_racingDriverRows,_racingEventRows);}catch(e){}
}
async function loadRacing(){
  const toggles=document.getElementById('racingSeries'),info=document.getElementById('racingInfo'),drivers=document.getElementById('racingDrivers');
  if(drivers)drivers.innerHTML='<span class="muted">'+esc(tr('Loading drivers and next race...'))+'</span>';
  if(info)info.innerHTML='<span class="muted">'+esc(tr('Loading racing schedules...'))+'</span>';
  try{
    const [r,d]=await Promise.all([api('/api/racing'),api('/api/racing_drivers')]);_racingSelected=new Set(r.selected||[]);_racingDriverRows=d.drivers||[];_racingEventRows=r.events||[];
    toggles.innerHTML=_RACING_SERIES.map(row=>'<button class="racingtoggle'+(_racingSelected.has(row[0])?' on':'')+'" data-key="'+row[0]+'" onclick="toggleRacingSeries(this.dataset.key)">'+esc(row[1])+'</button>').join('');
    const f1Rows=_racingDriverRows.filter(row=>row.series==='f1'),validKeys=new Set(_racingDriverRows.map(row=>String(row.key||'')));if(_racingSelected.has('f1'))validKeys.add('f1-team');
    if(!_racingDetailKey||!validKeys.has(_racingDetailKey))_racingDetailKey=_racingSelected.has('f1')?'f1-team':String((_racingDriverRows[0]||{}).key||'');
    renderRacingTeamControl();renderRacingDriverDetail();drivers.innerHTML=racingDriversHtml(_racingDriverRows,_racingEventRows);
    renderRacingScheduleCards();loadRacingAvailability();
  }catch(e){drivers.innerHTML='';info.innerHTML='<span class="err">'+esc(tr('Could not load racing schedules.'))+'</span>';}
}
async function toggleRacingSeries(key){
  if(_racingSelected.has(key))_racingSelected.delete(key);else _racingSelected.add(key);
  const r=await api('/api/racing_series',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({series:Array.from(_racingSelected)})});
  if(r.error){toast(r.error);return;}
  _profileConfig.racing_series=r.series||[];await loadRacing();loadFavorites();
}
let _showSeasons={};
let _activeSeriesId=null;
let _favShowSet=new Set();
let _favShowTitleSet=new Set();
let _latestEpisodesLoaded=false;
function latestEpisodeCard(ep){
  const cover=ep.cover?'<img src="'+escAttr(ep.cover)+'" alt="" loading="lazy" onerror="this.parentElement.textContent=String.fromCodePoint(128250)">':'&#128250;';
  let action='<button class="ghost" disabled>'+tr('Not available')+'</button>';
  if(ep.available){
    const sources=(ep.sources&&ep.sources.length)?ep.sources:[{id:ep.id,extension:ep.extension,label:'VLC'}];
    const sourceButtons=sources.map(src=>'<button class="btnvlc latestepisodevlc" data-id="'+escAttr(String(src.id))+'" data-ext="'+escAttr(src.extension||'mp4')+'">&#9658; '+esc(src.label)+'</button>').join('');
    action=sources.length>3?'<button class="btnvlc latestsourceexpand">&#9658; VLC</button><div class="latestsources hide">'+sourceButtons+'</div>':sourceButtons;
  }
  return '<div class="moviecard latestshowcard" data-series="'+escAttr(String(ep.series_id||''))+'" data-catalog="'+escAttr(ep.catalog_id||'')+'"><div class="movieposter">'+cover+'</div><div class="movieinfo"><div class="movietitle">'+esc(ep.show_name)+'</div>'
    +'<div class="moviemeta">S'+esc(ep.season)+'E'+esc(ep.episode_num)+' - '+esc(ep.title||'Episode')+'</div>'
    +'<div class="movieactions">'+action+'</div></div></div>';
}
function osloDayNumber(value){
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Oslo',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(value);
  const p={};parts.forEach(x=>{if(x.type!=='literal')p[x.type]=parseInt(x.value,10);});
  return Date.UTC(p.year,p.month-1,p.day)/86400000;
}
function friendlyAirdate(ep){
  if(!ep.airdate&&!ep.airstamp)return '';
  let target=ep.airstamp?new Date(ep.airstamp):new Date(ep.airdate+'T12:00:00');
  if(Number.isNaN(target.getTime()))target=new Date(ep.airdate+'T12:00:00');
  const now=new Date(), remaining=target-now;
  if(remaining>0&&remaining<86400000){
    const minutes=Math.max(1,Math.ceil(remaining/60000));
    if(minutes<60)return tr('in')+' '+minutes+' '+tr(minutes===1?'minute':'minutes');
    const hours=Math.ceil(minutes/60);
    return tr('in')+' '+hours+' '+tr(hours===1?'hour':'hours');
  }
  const days=Math.round(osloDayNumber(target)-osloDayNumber(now));
  if(days===0)return tr('Today');
  if(days===1)return tr('Tomorrow');
  const weekday=target.toLocaleDateString(_lang==='no'?'nb-NO':undefined,{weekday:'long',timeZone:'Europe/Oslo'});
  return weekday+' \u00b7 '+tr('in')+' '+days+' '+tr(days===1?'day':'days');
}
function upcomingEpisodeCard(ep){
  const cover=ep.cover?'<img src="'+escAttr(ep.cover)+'" alt="" loading="lazy" onerror="this.parentElement.textContent=String.fromCodePoint(128250)">':'&#128250;';
  const when=friendlyAirdate(ep);
  return '<div class="moviecard latestshowcard" data-series="'+escAttr(String(ep.series_id||''))+'" data-catalog="'+escAttr(ep.catalog_id||'')+'"><div class="movieposter">'+cover+'</div><div class="movieinfo"><div class="movietitle">'+esc(ep.show_name)+'</div>'
    +'<div class="moviemeta">S'+esc(ep.season)+'E'+esc(ep.episode_num)+' - '+esc(ep.title||'Episode')+'</div>'
    +'<div class="movieactions"><button class="ghost" disabled>'+tr('Airs')+' '+esc(when)+'</button></div></div></div>';
}
async function loadLatestEpisodes(limit,refresh){
  limit=limit||9;
  const el=document.getElementById('latestEpisodeList'), more=document.getElementById('latestEpisodeMore');
  const upcomingSection=document.getElementById('upcomingEpisodesSection'), upcomingList=document.getElementById('upcomingEpisodeList');
  el.innerHTML='<span class="muted">Loading latest episodes...</span>';more.classList.add('hide');
  upcomingSection.classList.add('hide');upcomingList.innerHTML='';
  const r=await api('/api/latest_episodes?limit='+limit+(refresh?'&refresh=1':''));
  if(r.error){el.innerHTML='<span class="muted">Could not load latest episodes.</span>';return false;}
  if(r.episodes.length)el.innerHTML='<div class="moviegrid" style="margin-top:0">'+r.episodes.map(latestEpisodeCard).join('')+'</div>';
  else el.innerHTML='<span class="muted">No latest episodes found for your favorite shows.</span>';
  if(r.upcoming&&r.upcoming.length){
    upcomingList.innerHTML='<div class="moviegrid" style="margin-top:0">'+r.upcoming.map(upcomingEpisodeCard).join('')+'</div>';
    upcomingSection.classList.remove('hide');
  }
  if(limit<36&&r.has_more)more.classList.remove('hide');
  _latestEpisodesLoaded=true;
  return !!(r.episodes.length||(r.upcoming&&r.upcoming.length));
}
async function expandLatestEpisodes(btn){
  const old=btn.textContent;btn.disabled=true;btn.textContent='Loading...';
  await loadLatestEpisodes(36);
  btn.disabled=false;btn.textContent=old;btn.classList.add('hide');
}
async function playLatestEpisode(id,ext,btn){
  const old=btn.textContent;btn.textContent='Opening...';
  try{
    const j=await api('/api/play_season',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({episodes:[{id:id,extension:ext}]})});
    if(j.error)alert(j.error||'Could not launch VLC.');
  }catch(e){alert('Could not launch VLC.');}
  setTimeout(()=>{btn.textContent=old;},1200);
}
async function loadShowFavorites(){
  const r=await api('/api/favorites'), shows=r.shows||[];
  _favShowSet=new Set(shows.map(s=>String(s.catalog_id||s.show_key||s.series_id)));
  _favShowTitleSet=new Set(shows.map(s=>String(s.show_key||'')).filter(Boolean));
  const el=document.getElementById('showFavList');
  if(!shows.length){el.innerHTML='<span class="muted">No favorite shows yet.</span>';return;}
  let h='';
  for(const s of shows){
    const poster='<span class="showfavposter">&#128250;'+(s.cover?'<img src="'+escAttr(s.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'</span>';
    const ids=(s.series_ids&&s.series_ids.length?s.series_ids:[s.series_id]).join(',');
    const key=String(s.catalog_id||s.show_key||s.series_id);
    h+='<div class="showfav" data-series="'+escAttr(ids)+'" data-catalog="'+escAttr(s.catalog_id||'')+'">'+poster+'<div class="showfavinfo"><div class="showfavname">'+esc(s.name)+'</div></div>'
      +'<button class="favrm showremove" data-key="'+escAttr(key)+'" title="Remove">&times;</button></div>';
  }
  el.innerHTML=h;
}
async function toggleShowFavorite(show,starEl){
  const r=await favPost({action:'toggle_show',show:show});
  _favShowSet=new Set((r.show_ids||[]).map(String));
  if(_favShowSet.has(String(show.catalog_id||show.show_key||show.series_id)))_profileConfig.setup_demo_content=false;
  if(starEl)starEl.classList.toggle('on',_favShowSet.has(String(show.catalog_id||show.show_key||show.series_id)));
  _latestEpisodesLoaded=false;
  await loadShowFavorites();
}
async function removeShowFavorite(showKey){
  await favPost({action:'remove_show',show_key:showKey});
  document.querySelectorAll('.showstar').forEach(el=>{if(el.getAttribute('data-key')===String(showKey))el.classList.remove('on');});
  _latestEpisodesLoaded=false;
  await loadShowFavorites();
}
async function searchShows(){
  const q=(document.getElementById('showQ').value||'').trim(), el=document.getElementById('showResults');
  document.getElementById('showDetails').innerHTML='';
  const latest=document.getElementById('latestEpisodesSection');
  if(!q){latest.classList.remove('hide');el.innerHTML='<div class="muted" style="margin-top:14px">Enter a show title.</div>';return;}
  latest.classList.add('hide');
  el.innerHTML='<div class="muted" style="margin-top:14px">Searching your shows...</div>';
  const r=await api('/api/shows?q='+encodeURIComponent(q));
  const back='<div class="showresultback"><button class="ghost" onclick="backToMyShows()">&#8592; '+tr('Back to Shows')+'</button></div>';
  if(r.error){el.innerHTML=back+'<div class="err">'+esc(r.error)+'</div>';return;}
  if(!r.shows.length){el.innerHTML=back+'<div class="muted">No shows found for &quot;'+esc(q)+'&quot;.</div>';return;}
  await loadShowFavorites();
  let h=back+'<div class="showgrid">';
  for(const s of r.shows){
    const fav=(_favShowSet.has(String(s.catalog_id||s.show_key))||_favShowTitleSet.has(String(s.show_key)))?' on':'';
    const ids=(s.series_ids||[s.series_id]).join(',');
    const cover=s.cover?'<img src="'+escAttr(s.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'';
    h+='<div class="showcard" data-series="'+escAttr(ids)+'" data-catalog="'+escAttr(s.catalog_id||'')+'"><div class="showposter">&#128250;'+cover+'</div>'
      +'<div><div class="showname">'+esc(s.name)+'</div><div class="moviemeta" style="margin-top:7px">'+(s.year?esc(s.year):'')+(s.rating?(' &nbsp; Rating: '+esc(s.rating)):'')+'</div>'
      +'<span class="favstar showstar'+fav+'" data-key="'+escAttr(s.catalog_id||s.show_key)+'" data-catalog="'+escAttr(s.catalog_id||'')+'" data-show-key="'+escAttr(s.show_key)+'" data-series="'+escAttr(String(s.series_id==null?'':s.series_id))+'" data-series-ids="'+escAttr(ids)+'" data-name="'+escAttr(s.name||'')+'" data-cover="'+escAttr(s.cover||'')+'" data-year="'+escAttr(s.year||'')+'" data-rating="'+escAttr(s.rating||'')+'" title="Favorite">&#9733;</span></div></div>';
  }
  el.innerHTML=h+'</div>';
}
function backToMyShows(){
  document.getElementById('showQ').value='';
  showShows();
}
async function loadShow(seriesId,refresh){
  if(!refresh)rememberLocation('shows',{seriesId:String(seriesId)});
  _activeSeriesId=seriesId;
  document.getElementById('latestEpisodesSection').classList.add('hide');
  const el=document.getElementById('showDetails');
  el.innerHTML='<div class="muted">Loading seasons and episodes...</div>';
  const r=await api('/api/show?id='+encodeURIComponent(seriesId)+(refresh?'&refresh=1':''));
  if(r.error){el.innerHTML='<div class="err">'+esc(r.error)+'</div>';return false;}
  await loadShowFavorites();
  document.getElementById('showResults').innerHTML='';
  _showSeasons={};
  const heroCover=r.cover?'<img src="'+escAttr(r.cover)+'" alt="" onerror="this.remove()">':'';
  const heroFav=(_favShowSet.has(String(r.show_key))||_favShowTitleSet.has(String(r.show_key)))?' on':'';
  let h='<div class="showhero"><div class="showheroart">&#128250;'+heroCover+'</div><div><h2>'+esc(r.name||'Show')
    +'<span class="favstar showstar'+heroFav+'" data-key="'+escAttr(r.show_key)+'" data-series="'+escAttr(String(r.series_id))+'" data-series-ids="'+escAttr((r.series_ids||[]).join(','))+'" data-name="'+escAttr(r.name||'Show')+'" data-cover="'+escAttr(r.cover||'')+'" data-year="" data-rating="" title="Favorite">&#9733;</span></h2>'
    +'<div class="muted">'+r.seasons.length+' season'+(r.seasons.length===1?'':'s')+'</div></div><button class="ghost showbackbtn" onclick="backToMyShows()">&#8592; '+tr('Back to Shows')+'</button></div>';
  for(const season of r.seasons){
    _showSeasons[String(season.number)]={};
    for(const ep of season.episodes)for(const src of (ep.sources||[])){
      if(!_showSeasons[String(season.number)][src.label])_showSeasons[String(season.number)][src.label]=[];
      _showSeasons[String(season.number)][src.label].push({id:src.id,extension:src.extension,episode_num:ep.episode_num});
    }
    const seasonCover=season.cover?'<img src="'+escAttr(season.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'';
    h+='<div class="seasonblock"><div class="seasonlayout"><div class="seasonart">&#128250;'+seasonCover+'</div><div class="seasoncontent"><div class="seasonhead"><b>'+esc(season.title)+'</b></div><div class="episodes">';
    for(let ei=0;ei<season.episodes.length;ei++){
      const ep=season.episodes[ei];
      h+='<div class="episode"><div class="episodename"><b>E'+esc(ep.episode_num)+'</b> '+esc(ep.title||'Episode')+'</div><div class="episodequalities">';
      for(const src of (ep.sources||[]))h+='<button class="btnvlc episodevlc" data-season="'+escAttr(String(season.number))+'" data-episode="'+escAttr(String(ep.episode_num))+'" data-source="'+escAttr(src.label)+'">&#9658; '+esc(src.label)+'</button>';
      h+='</div></div>';
    }
    h+='</div></div></div></div>';
  }
  if(!r.seasons.length)h+='<div class="muted">No episodes found.</div>';
  el.innerHTML=h;
  return true;
}
async function loadExternalShow(catalogId,refresh){
  if(!refresh)rememberLocation('shows',{catalogId:String(catalogId)});
  _activeSeriesId=null;_showSeasons={};
  document.getElementById('latestEpisodesSection').classList.add('hide');
  const el=document.getElementById('showDetails');
  el.innerHTML='<div class="muted">Loading show...</div>';
  const r=await api('/api/show_external?id='+encodeURIComponent(catalogId));
  if(r.error){el.innerHTML='<div class="err">'+esc(r.error)+'</div>';return false;}
  if(r.provider_series_ids&&r.provider_series_ids.length)return loadShow(r.provider_series_ids.join(','),true);
  await loadShowFavorites();document.getElementById('showResults').innerHTML='';
  const cover=r.cover?'<img src="'+escAttr(r.cover)+'" alt="" onerror="this.remove()">':'',key=String(r.catalog_id||r.show_key),fav=_favShowSet.has(key)?' on':'';
  let h='<div class="showhero"><div class="showheroart">&#128250;'+cover+'</div><div><h2>'+esc(r.name||'Show')
    +'<span class="favstar showstar'+fav+'" data-key="'+escAttr(key)+'" data-catalog="'+escAttr(r.catalog_id||'')+'" data-show-key="'+escAttr(r.show_key||'')+'" data-series="" data-series-ids="" data-name="'+escAttr(r.name||'Show')+'" data-cover="'+escAttr(r.cover||'')+'" data-year="'+escAttr(r.year||'')+'" data-rating="'+escAttr(r.rating||'')+'" title="Favorite">&#9733;</span></h2>'
    +'<div class="muted">'+r.seasons.length+' season'+(r.seasons.length===1?'':'s')+'</div></div><button class="ghost showbackbtn" onclick="backToMyShows()">&#8592; '+tr('Back to Shows')+'</button></div>';
  for(const season of r.seasons){
    const seasonCover=season.cover?'<img src="'+escAttr(season.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'';
    h+='<div class="seasonblock"><div class="seasonlayout"><div class="seasonart">&#128250;'+seasonCover+'</div><div class="seasoncontent"><div class="seasonhead"><b>'+esc(season.title)+'</b></div><div class="episodes">';
    for(const ep of season.episodes)h+='<div class="episode"><div class="episodename"><b>E'+esc(ep.episode_num)+'</b> '+esc(ep.title||'Episode')+'</div><div class="episodequalities"><button class="ghost" disabled>'+tr('Not available')+'</button></div></div>';
    h+='</div></div></div></div>';
  }
  el.innerHTML=h;return true;
}
async function checkAllShows(btn){
  const old=btn.innerHTML;btn.disabled=true;btn.textContent='Checking all shows...';
  try{
    const j=await api('/api/check_show_updates',{method:'POST'});
    if(j.error)throw new Error(j.error||'check failed');
    if(_activeSeriesId)await loadShow(_activeSeriesId,true);
    await loadShowFavorites();
    const latest=document.getElementById('latestEpisodesSection');
    if(latest&&!latest.classList.contains('hide'))await loadLatestEpisodes(9,true);
    if(j.new_episodes>0)toast('Found '+j.new_episodes+' new episode'+(j.new_episodes===1?'':'s')+' for your shows',7000);
    else toast('Successfully refreshed playlists, no new episodes found',7000);
  }catch(e){toast('Could not refresh show playlists.');}
  btn.disabled=false;btn.innerHTML=old;
}
async function checkShowsOnStartup(){
  try{
    const j=await api('/api/check_show_updates',{method:'POST'});
    if(j.error)throw new Error(j.error||'check failed');
    _latestEpisodesLoaded=false;
    if(j.new_episodes>0)toast('Found '+j.new_episodes+' new episode'+(j.new_episodes===1?'':'s')+' for your shows',7000);
    else toast('Successfully refreshed playlists, no new episodes found',7000);
  }catch(e){toast('Could not refresh show playlists.',7000);}
}
async function refreshOnStartup(refreshIptv,refreshSports){
  try{
    if(refreshIptv)await refreshIptvContent(null,true);
    if(refreshSports)await refreshOtherContent(null,true);
    toast('Startup refresh finished.',5000);
  }catch(e){toast('Startup refresh failed: '+String(e&&e.message||e),7000);}
}
async function playEpisodeQueue(season,episodeNum,source,btn){
  const episodes=((_showSeasons[String(season)]||{})[source]||[]).filter(ep=>Number(ep.episode_num)>=Number(episodeNum)), old=btn.textContent;btn.textContent='Opening...';
  try{const j=await api('/api/play_season',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({episodes:episodes})});if(j.error)alert(j.error||'Could not launch VLC.');}
  catch(e){alert('Could not launch VLC.');}
  setTimeout(()=>btn.textContent=old,1200);
}
document.addEventListener('keydown',function(e){if(e.key==='Escape')closePlayer();});
// ---- favorites / My List ----
let _favCatSet=new Set();
let _favChanSet=new Set();
async function refreshFavState(){
  try{const r=await api('/api/favorites');
    _favCatSet=new Set(r.categories||[]);
    _favChanSet=new Set((r.channels||[]).map(function(c){return String(c.stream_id);}));
  }catch(e){}
}
async function loadFavorites(){
  const r=await api('/api/favorites');
  _favCatSet=new Set(r.categories||[]);
  _favChanSet=new Set((r.channels||[]).map(function(c){return String(c.stream_id);}));
  _myListFavData=r;
  _myListLoaded=true;
  _myListSelectedChannels=(r.mylist_channels||[]).map(String).slice(0,5);
  _myListTeamMoments=[];_myListF1Moments=[];_myListMovieMoments=[];_myListGameMoments=[];_myListShowMoments=[];_myListRacingDrivers=[];
  renderMyListProfile();
  applyMyListLayout();
  renderMyListChannels();
  const racingDataPromise=_f1Enabled?api('/api/racing'):null;
  if(_footballEnabled||_f1Enabled)loadMyListTeams(r,racingDataPromise);
  if(_f1Enabled)loadMyListRacing(racingDataPromise);else{_myListF1Moments=[];scheduleMyListTimelineRender();}
  loadMyListMovies();
  if(_gamesEnabled)loadMyListGames(r);else{_myListGameMoments=[];scheduleMyListTimelineRender();}
  loadMyListShows();
}
let _myListLoaded=false,_myListFavData={channels:[],teams:[],f1_teams:[]},_myListSelectedChannels=[],_myListTeamMoments=[],_myListF1Moments=[],_myListMovieMoments=[],_myListGameMoments=[],_myListShowMoments=[],_myListRacingDrivers=[];
let _myTimelineFilter='all',_myTimelineSettings={recent:true,live:true,upcoming:true,maxPerCategory:0},_myTimelinePrefsLoaded=false;
let _myListTimelineRenderPending=false;
function scheduleMyListTimelineRender(){
  if(_myListTimelineRenderPending)return;
  _myListTimelineRenderPending=true;
  requestAnimationFrame(()=>{_myListTimelineRenderPending=false;renderMyListTimeline();});
}
function renderMyListChannels(){
  const el=document.getElementById('myListChannels');if(!el)return;
  const byId=new Map((_myListFavData.channels||[]).map(c=>[String(c.stream_id),c]));
  let h='';
  for(let i=0;i<5;i++){
    const c=byId.get(_myListSelectedChannels[i]||'');
    if(c)h+='<div class="mydashchannel" data-sid="'+escAttr(String(c.stream_id))+'" data-name="'+escAttr(c.name||'')+'" title="'+escAttr(tr('Play'))+'">'+channelLogo(c)+'<span class="mydashchannelname">'+esc(c.name)+'</span><button class="btnvlc" data-sid="'+escAttr(String(c.stream_id))+'">&#9658; VLC</button></div>';
    else h+='<div class="mydashchannel muted" onclick="toggleMyListChannelPicker()">+ '+tr('Choose channels')+'</div>';
  }
  el.innerHTML=h;
}
function toggleMyListChannelPicker(){
  const el=document.getElementById('myListChannelPicker');
  if(!el.classList.contains('hide')){el.classList.add('hide');return;}
  const selected=new Set(_myListSelectedChannels);
  if(!(_myListFavData.channels||[]).length){el.innerHTML='<span class="muted">'+tr('Star channels first, then choose up to five here.')+'</span>';}
  else el.innerHTML=_myListFavData.channels.map(c=>'<label class="mydashchoice"><input type="checkbox" data-sid="'+escAttr(String(c.stream_id))+'" '+(selected.has(String(c.stream_id))?'checked ':'')+'onchange="setMyListChannel(this.dataset.sid,this.checked,this)">'+channelLogo(c,'mini')+'<span>'+esc(c.name)+'</span></label>').join('');
  el.classList.remove('hide');
}
async function setMyListChannel(sid,checked,input){
  sid=String(sid);let chosen=_myListSelectedChannels.slice();
  if(checked&&!chosen.includes(sid)){
    if(chosen.length>=5){input.checked=false;toast(tr('Choose up to five channels.'));return;}
    chosen.push(sid);
  }else if(!checked)chosen=chosen.filter(id=>id!==sid);
  const r=await favPost({action:'set_mylist_channels',stream_ids:chosen});
  _myListSelectedChannels=chosen.slice(0,5);renderMyListChannels();
}
async function toggleRacingF1Picker(){
  const el=document.getElementById('racingF1Picker');if(!el)return;
  if(!el.classList.contains('hide')){el.classList.add('hide');return;}
  el.innerHTML='<span class="muted">'+tr('Loading...')+'</span>';el.classList.remove('hide');
  try{
    const r=await api('/api/f1_teams'), selected=String(((r.favorites||[])[0]||{}).id||'');
    el.innerHTML=(r.teams||[]).map(team=>'<button class="mydashchoice f1choice'+(String(team.id)===selected?' on':'')+'" data-id="'+escAttr(team.id)+'" data-name="'+escAttr(team.name)+'" onclick="setRacingF1Team(this.dataset.id,this.dataset.name)"><img src="/api/f1_team_logo?id='+encodeURIComponent(String(team.id||''))+'" alt="" loading="lazy" onerror="this.remove()"><span>'+esc(team.name)+'</span></button>').join('')||'<span class="muted">'+tr('No F1 team selected.')+'</span>';
  }catch(e){el.innerHTML='<span class="muted">'+tr('Could not load Formula 1 calendar.')+'</span>';}
}
async function setRacingF1Team(id,name){
  let current='';try{const state=await api('/api/f1_teams');current=String(((state.favorites||[])[0]||{}).id||'');}catch(e){}
  const clear=current===String(id);
  await favPost({action:'set_f1_team',team:clear?{}:{id:id,name:name}});
  document.getElementById('racingF1Picker').classList.add('hide');
  await loadRacing();loadFavorites();
}
function mySportTeamMeta(fixtures){
  const counts=new Map(),rows=(fixtures||[]).filter(f=>f&&f.league_name&&!/friendl/i.test(String(f.league_name)));
  for(const f of rows){const name=String(f.league_name||'').trim(),key=name.toLowerCase();if(!key)continue;const old=counts.get(key)||{name:name,count:0};old.count++;counts.set(key,old);}
  const best=Array.from(counts.values()).sort((a,b)=>b.count-a.count)[0];if(!best)return '';
  const low=best.name.toLowerCase();let country='';
  if(/premier league|championship|league one|league two/.test(low))country='England';
  else if(/eliteserien|obos/.test(low))country='Norway';
  else if(/la ?liga/.test(low))country='Spain';
  else if(/bundesliga/.test(low))country='Germany';
  else if(/serie a/.test(low))country='Italy';
  else if(/ligue 1/.test(low))country='France';
  else if(/eredivisie/.test(low))country='Netherlands';
  else if(/primeira liga/.test(low))country='Portugal';
  else if(/premiership/.test(low))country='Scotland';
  return (country?country+' × ':'')+best.name;
}
function renderMyListSportShells(favorites){
  const el=document.getElementById('myListTeams'),teams=(favorites.teams||[]);if(!el)return;
  let h='';
  if(_footballEnabled)for(const team of teams){
    const name=String(typeof team==='string'?team:team.name||''),id=typeof team==='string'?'':String(team.team_id||''),logo=typeof team==='string'?'':(team.logo||''),src=logo||(id?'/api/team_logo?id='+encodeURIComponent(id):'');
    if(_myListLayout==='timeline')h+='<div class="mydashteamonly" onclick="showTeams()">'+(src?'<img src="'+escAttr(src)+'" alt="" onerror="this.remove()">':'')+'<div class="mydashsportsingle"><div class="mydashsportsingletop"><div class="mydashsportname">'+esc(name)+'</div><div class="mydashsporteventline"><span class="mydashsportnext muted">'+esc(tr('Loading fixture...'))+'</span></div></div></div></div>';
    else h+='<div class="mydashfixture"><div class="mydashteam">'+(src?'<img src="'+escAttr(src)+'" alt="" onerror="this.remove()">':'')+'<span>'+esc(name)+'</span></div><span class="muted">'+esc(tr('Loading fixture...'))+'</span></div>';
  }
  if(_f1Enabled){
    const selected=new Set((_profileConfig.racing_series||['f1']).map(String));
    if(_myListLayout==='timeline')h+='<div class="mydashsportsubhead racing">Racing</div>';
    if(selected.has('f1')){
      const team=((favorites.f1_teams||[])[0]||{}),name=team.name||'Formula 1',src=team.logo||(team.id?'/api/f1_team_logo?id='+encodeURIComponent(String(team.id)):'');
      h+='<div class="mydashteamonly mydashf1card" data-driver-key="f1-team" onclick="showRacing(this.dataset.driverKey)">'+(src?'<img src="'+escAttr(src)+'" alt="" onerror="this.remove()">':'')+'<div class="mydashsportsingle"><div class="mydashsportname">'+esc(name)+'</div><div class="mydashsportmeta">Formula 1 · '+esc(tr('Loading drivers and next race...'))+'</div></div></div>';
    }
    const quick=[['wrc','wrc-oliver-solberg','Oliver Solberg','WRC × Toyota Gazoo Racing',false],['indycar','indycar-dennis-hauger','Dennis Hauger','IndyCar × Dale Coyne Racing',false],['f2','f2-martinius-stenshorne','Martinius Stenshorne','Formula 2 × Rodin Motorsport',true]];
    for(const row of quick){if(!selected.has(row[0]))continue;const src='/api/racing_driver_image?id='+encodeURIComponent(row[1]);h+='<div class="mydashteamonly" data-driver-key="'+escAttr(row[1])+'" onclick="showRacing(this.dataset.driverKey)"><img class="driver'+(row[4]?' car':'')+'" src="'+src+'" alt="" loading="lazy" onerror="this.remove()"><div class="mydashsportsingle"><div class="mydashsportname">'+esc(row[2])+'</div><div class="mydashsportmeta">'+esc(row[3])+' · '+esc(tr('Loading next race...'))+'</div></div></div>';}
  }
  el.innerHTML=h||'<span class="muted">'+tr(_f1Enabled?'No F1 team selected.':'No favorite teams yet.')+'</span>';
}
async function loadMyListTeams(favorites,racingDataPromise){
  const el=document.getElementById('myListTeams'),teams=(favorites.teams||[]),now=Date.now();
  _myListTeamMoments=[];let h='';
  renderMyListSportShells(favorites);
  const racingPromise=_f1Enabled?Promise.all([api('/api/racing_drivers'),racingDataPromise||api('/api/racing')]):null;
  if(_footballEnabled&&teams.length){
    try{
      const r=await api('/api/my_teams'),fixtures=r.fixtures||[];
      for(const team of teams){
        const name=String(typeof team==='string'?team:team.name||''),key=name.toLowerCase();
        const mine=fixtures.filter(f=>(f.favorite_teams||[]).some(owner=>String(owner).toLowerCase()===key));
        const live=mine.filter(f=>f.is_live).sort((a,b)=>String(a.start).localeCompare(String(b.start)))[0];
        const upcoming=mine.filter(f=>{const ts=f.start?new Date(f.start).getTime():0;return ts>now;}).sort((a,b)=>new Date(a.start)-new Date(b.start));
        const next=upcoming[0];
        const fixture=live||next,id=typeof team==='string'?'':String(team.team_id||''),logo=typeof team==='string'?'':(team.logo||''),src=logo||(id?'/api/team_logo?id='+encodeURIComponent(id):'');
        if(live)_myListTeamMoments.push({team:name,fixture:live,live:true,logo:src,ts:Date.now()});
        for(const future of upcoming.slice(0,4)){const ts=future.start?new Date(future.start).getTime():0;if(ts)_myListTeamMoments.push({team:name,fixture:future,live:false,logo:src,ts:ts});}
        if(_myListLayout==='timeline'){
          const fixtureText=fixture?((fixture.home||'')+' v '+(fixture.away||'')):tr('No upcoming fixture found.');
          const countdown=fixture?(live?'LIVE':racingCountdown({start:fixture.start})):'';
          const teamMeta=mySportTeamMeta(mine);
          h+='<div class="mydashteamonly" onclick="showTeams()">'+(src?'<img src="'+escAttr(src)+'" alt="" onerror="this.remove()">':'')+'<div class="mydashsportsingle"><div class="mydashsportsingletop"><div class="mydashsportname">'+esc(name)+'</div><div class="mydashsporteventline"><span class="mydashsportnext">'+esc(fixtureText)+'</span>'+(countdown?'<span class="mydashsportcount">'+esc(countdown)+'</span>':'')+'</div></div>'+(teamMeta?'<div class="mydashsportmeta">'+esc(teamMeta)+'</div>':'')+'</div></div>';
        }
        else{h+='<div class="mydashfixture"><div class="mydashteam">'+(src?'<img src="'+escAttr(src)+'" alt="" onerror="this.remove()">':'')+'<span>'+esc(name)+'</span></div>'+(fixture?teamFixtureCard(fixture,!!fixture.is_live):'<span class="muted">'+tr('No upcoming fixture found.')+'</span>')+'</div>';}
      }
    }catch(e){h+='<span class="muted">'+tr('Could not load team fixtures.')+'</span>';}
  }
  if(_f1Enabled){
    try{
      const [driverData,racingData]=await racingPromise;
      if(_myListLayout==='timeline'&&(driverData.drivers||[]).length)h+='<div class="mydashsportsubhead racing">Racing</div>';
      const allDrivers=driverData.drivers||[];_myListRacingDrivers=allDrivers;
      const f1Drivers=allDrivers.filter(driver=>String(driver.series||'')==='f1');
      if(_myListLayout==='timeline'){
        const cards=[];
        if(f1Drivers.length){const next=nextDriverRace(f1Drivers[0],racingData.events||[],now);cards.push({kind:'f1',drivers:f1Drivers,next:next,ts:next?new Date(next.start).getTime():Infinity});}
        for(const driver of allDrivers){if(String(driver.series||'')==='f1')continue;const next=nextDriverRace(driver,racingData.events||[],now);cards.push({kind:'driver',driver:driver,next:next,ts:next?new Date(next.start).getTime():Infinity});}
        // Racing follows the calendar: nearest next event first. Football team
        // cards above deliberately retain the user's favorite/order sequence.
        cards.sort((a,b)=>(Number.isFinite(a.ts)?a.ts:Infinity)-(Number.isFinite(b.ts)?b.ts:Infinity));
        for(const card of cards){
          const next=card.next,countdown=next?racingCountdown(next):'';
          if(card.kind==='f1'){
            const drivers=card.drivers,live=(racingData.events||[]).filter(e=>String(e.series||'')==='f1').some(e=>racingEventIsLive(e,now)),team=drivers[0].team||'';
            const photos=drivers.slice(0,2).map(driver=>'<img class="driver" src="/api/racing_driver_image?id='+encodeURIComponent(String(driver.key||''))+'" alt="" loading="lazy" onerror="this.remove()">').join('');
            const names=drivers.slice(0,2).map(driver=>'<div class="mydashsportname">'+esc(driver.name||'')+'</div>').join(''),race=next?(next.race||next.circuit||tr('Next race')):tr('No upcoming race found.');
            h+='<div class="mydashteamonly mydashf1card" data-driver-key="f1-team" onclick="showRacing(this.dataset.driverKey)"><div class="mydashsportphotos">'+photos+'</div><div class="mydashsportsingle"><div class="mydashsportsingletop"><div class="mydashf1names">'+names+'</div><div class="mydashsporteventline"><span class="mydashsportnext">'+esc(race)+'</span><span class="mydashsportcount">'+esc(live?tr('Right now'):(countdown||''))+'</span></div></div><div class="mydashsportmeta">Formula 1'+(team?' × '+esc(team):'')+'</div></div></div>';
          }else{
            const driver=card.driver,live=(racingData.events||[]).filter(e=>String(e.series||'')===String(driver.series||'')).some(e=>racingEventIsLive(e,now)),src='/api/racing_driver_image?id='+encodeURIComponent(String(driver.key||''));
            const meta=[driver.series_name||'Racing',driver.team||''].filter(Boolean).join(' × '),nextText=next?(next.race||next.circuit||tr('Next race')):tr('No upcoming race found.'),imageClass='driver'+(String(driver.key||'')==='f2-martinius-stenshorne'?' car':'');
            h+='<div class="mydashteamonly" data-driver-key="'+escAttr(String(driver.key||''))+'" onclick="showRacing(this.dataset.driverKey)"><img class="'+imageClass+'" src="'+src+'" alt="" loading="lazy" onerror="this.remove()"><div class="mydashsportsingle"><div class="mydashsportsingletop"><div class="mydashsportname">'+esc(driver.name||'')+'</div><div class="mydashsporteventline"><span class="mydashsportnext">'+esc(nextText)+'</span><span class="mydashsportcount">'+esc(live?tr('Right now'):(countdown||''))+'</span></div></div><div class="mydashsportmeta">'+esc(meta)+'</div></div></div>';
          }
        }
      }else for(const driver of allDrivers){
        const live=(racingData.events||[]).filter(e=>String(e.series||'')===String(driver.series||'')).some(e=>racingEventIsLive(e,now)),src='/api/racing_driver_image?id='+encodeURIComponent(String(driver.key||''));
        h+='<div class="mydashfixture" data-driver-key="'+escAttr(String(driver.key||''))+'" onclick="showRacing(this.dataset.driverKey)" style="cursor:pointer"><div class="mydashteam"><img src="'+src+'" alt="" loading="lazy" onerror="this.remove()"><span>'+esc(driver.name||'')+'</span></div><span class="muted">'+esc(driver.series_name||'Racing')+(driver.team?' · '+esc(driver.team):'')+'</span>'+(live?'<span class="mydashsportcount">'+esc(tr('Right now'))+'</span>':'')+'</div>';
      }
    }catch(e){}
  }
  el.innerHTML=h||'<span class="muted">'+tr(_f1Enabled?'No F1 team selected.':'No favorite teams yet.')+'</span>';
  scheduleMyListTimelineRender();
}
async function loadMyListRacing(racingDataPromise){
  _myListF1Moments=[];
  try{
    const r=await (racingDataPromise||api('/api/racing')),now=Date.now();
    const groups=new Map();
    for(const event of (r.events||[])){
      const row={event:event,ts:new Date(event.start).getTime()},live=racingEventIsLive(event,now);
      if(!Number.isFinite(row.ts)||(!live&&row.ts<=now-2*3600000))continue;
      const key=String(event.series||'racing');
      if(!groups.has(key))groups.set(key,[]);
      groups.get(key).push(row);
    }
    // Keep the timeline balanced when several championships are enabled:
    // a session-heavy F1 weekend should not crowd WRC/MotoGP/etc. off it.
    _myListF1Moments=Array.from(groups.values()).flatMap(rows=>rows.sort((a,b)=>a.ts-b.ts).slice(0,3)).sort((a,b)=>a.ts-b.ts).slice(0,18);
    scheduleMyListTimelineRender();
    try{const a=await api('/api/racing_availability');for(const row of _myListF1Moments)row.event.channels=(a.availability||{})[racingAvailabilityKey(row.event)]||[];}catch(e){}
  }catch(e){}
  scheduleMyListTimelineRender();
}
async function loadMyListMovies(){
  _myListMovieMoments=[];
  try{
    const r=await api('/api/favorite_movie_status'),windowMs=2*24*3600000,now=Date.now();
    _myListMovieMoments=(r.movies||[]).map(movie=>({movie:movie,ts:Date.parse(movie.released||'')})).filter(row=>Number.isFinite(row.ts)&&Math.abs(row.ts-now)<=windowMs).sort((a,b)=>Math.abs(a.ts-now)-Math.abs(b.ts-now)).slice(0,4);
  }catch(e){}
  scheduleMyListTimelineRender();
}
function loadMyListGames(favorites){
  const now=Date.now(),recentCutoff=now-7*24*3600000;
  _myListGameMoments=(favorites.games||[]).filter(game=>game.wishlist_imported).map(game=>({game:game,ts:Date.parse(game.released||'')})).filter(row=>Number.isFinite(row.ts)&&row.ts>=recentCutoff).sort((a,b)=>Math.abs(a.ts-now)-Math.abs(b.ts-now)).slice(0,4);
  scheduleMyListTimelineRender();
}
function myListEpisodeWhen(ts,upcoming){
  const delta=ts-Date.now();
  if(upcoming){const hours=Math.max(1,Math.ceil(delta/3600000));return tr('Airs in')+' '+hours+' '+tr(hours===1?'hour':'hours');}
  const hours=Math.max(0,Math.round(Math.abs(delta)/3600000));
  if(hours<=12)return tr('Just released');
  return tr('Released')+' '+hours+' '+tr(hours===1?'hour':'hours')+' '+tr('ago');
}
function timelineUpcomingWhen(ts,dateOnly){
  const target=new Date(ts),now=new Date(),delta=target-now,locale=_lang==='no'?'nb-NO':undefined;
  if(!Number.isFinite(delta))return '';
  if(!dateOnly&&delta>0&&delta<24*3600000){
    const minutes=Math.max(1,Math.ceil(delta/60000));
    if(minutes<60)return tr('in')+' '+minutes+' '+tr(minutes===1?'minute':'minutes');
    const hours=Math.ceil(minutes/60);return tr('in')+' '+hours+' '+tr(hours===1?'hour':'hours');
  }
  const dayDiff=Math.round(osloDayNumber(target)-osloDayNumber(now));
  const time=target.toLocaleTimeString(locale,{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Oslo'});
  if(!dateOnly&&dayDiff===0)return tr('Today')+' · '+time;
  if(!dateOnly&&dayDiff===1)return tr('Tomorrow')+' · '+time;
  const date=target.toLocaleDateString(locale,{weekday:'short',day:'numeric',month:'short',timeZone:'Europe/Oslo'});
  return dateOnly?date:(date+' · '+time);
}
function timelineReleasedWhen(ts){
  const delta=Math.max(0,Date.now()-Number(ts||0)),minutes=Math.max(1,Math.round(delta/60000));
  let amount,unit;
  if(minutes<60){amount=minutes;unit=tr(minutes===1?'minute':'minutes');}
  else{amount=Math.max(1,Math.round(minutes/60));unit=tr(amount===1?'hour':'hours');}
  return tr('Released')+(_lang==='no'?' for ':' ')+amount+' '+unit+' '+tr('ago');
}
async function loadMyListShows(){
  const el=document.getElementById('myListShows');
  _myListShowMoments=[];
  try{
    const r=await api('/api/latest_episodes?limit=36'),now=Date.now(),recentWindow=24*3600000,upcomingWindow=7*24*3600000,candidates=[];
    for(const ep of (r.episodes||[])){const aired=Number(ep.air_ts||0)*1000,added=Number(ep.added||0)*1000;const ts=(aired>0&&aired<=now&&now-aired<=recentWindow)?aired:(added||aired);if(ts)candidates.push({ep:ep,ts:ts,upcoming:false});}
    for(const ep of (r.upcoming||[])){const ts=Number(ep.air_ts||0)*1000||(ep.airstamp?new Date(ep.airstamp).getTime():0);if(ts)candidates.push({ep:ep,ts:ts,upcoming:true});}
    const nearest=new Map();
    for(const row of candidates){const delta=row.ts-now;if(row.upcoming){if(delta<0||delta>upcomingWindow)continue;}else if(delta>0||Math.abs(delta)>recentWindow)continue;const key=String(row.ep.show_name||'').toLowerCase()+'|'+(row.upcoming?'upcoming':'recent');const old=nearest.get(key);if(!old||Math.abs(delta)<Math.abs(old.ts-now))nearest.set(key,row);}
    const rows=Array.from(nearest.values()).sort((a,b)=>Math.abs(a.ts-now)-Math.abs(b.ts-now)).slice(0,12);
    _myListShowMoments=rows;
    if(!rows.length){el.innerHTML='<span class="muted">'+tr('Nothing airing close to now from your favorite shows.')+'</span>';scheduleMyListTimelineRender();return;}
    el.innerHTML=rows.map(row=>{const ep=row.ep,cover=ep.cover?'<img src="'+escAttr(ep.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'';let action='';
      if(!row.upcoming&&ep.available){const src=(ep.sources&&ep.sources.length)?ep.sources[0]:{id:ep.id,extension:ep.extension};if(src&&src.id!=null)action='<div class="movieactions"><button class="btnvlc latestepisodevlc" data-id="'+escAttr(String(src.id))+'" data-ext="'+escAttr(src.extension||'mp4')+'">&#9658; VLC</button></div>';}
      return '<div class="mydashepisode mylistshowcard" data-series="'+escAttr(String(ep.series_id||''))+'" data-catalog="'+escAttr(ep.catalog_id||'')+'">'+cover+'<div class="mydashepisodeinfo"><div class="mydashepisodename">'+esc(ep.show_name)+'</div><div class="mydashwhen">'+esc(myListEpisodeWhen(row.ts,row.upcoming))+'</div><div class="moviemeta">S'+esc(ep.season)+'E'+esc(ep.episode_num)+' - '+esc(ep.title||'Episode')+'</div>'+action+'</div></div>';}).join('');
    scheduleMyListTimelineRender();
  }catch(e){el.innerHTML='<span class="muted">'+tr('Could not load your shows.')+'</span>';scheduleMyListTimelineRender();}
}
function myListSportArtwork(fixture){
  const id=String((fixture&&fixture.league_id)||'');
  if(!/^\\d+$/.test(id))return '';
  const name=String((fixture&&fixture.league_name)||tr('Sports'));
  return '<img class="mylisttimelineart" src="/api/league_logo?id='+encodeURIComponent(id)+'" alt="'+escAttr(name)+'" title="'+escAttr(name)+'" loading="lazy" onerror="this.remove()">';
}
function myListRacingArtwork(event){
  const series=String((event&&event.series)||'').toLowerCase();
  let src='',name='',driver=false,car=false;
  if(series==='f1'){
    const pair=(_myListRacingDrivers||[]).filter(row=>String(row.series||'').toLowerCase()==='f1').slice(0,2);
    if(pair.length)return '<span class="mylisttimelinedrivers" title="'+escAttr(pair.map(row=>row.name||'').filter(Boolean).join(' & '))+'">'+pair.map(row=>'<img src="/api/racing_driver_image?id='+encodeURIComponent(String(row.key||''))+'" alt="'+escAttr(row.name||'')+'" loading="lazy" onerror="this.remove()">').join('')+'</span>';
    return '';
  }else{
    const drivers={wrc:['wrc-oliver-solberg','Oliver Solberg'],indycar:['indycar-dennis-hauger','Dennis Hauger'],f2:['f2-martinius-stenshorne','Martinius Stenshorne']};
    const row=drivers[series];
    if(row){src='/api/racing_driver_image?id='+encodeURIComponent(row[0]);name=row[1];driver=true;car=series==='f2';}
  }
  return src?'<img class="mylisttimelineart'+(driver?' driver':'')+(car?' car':'')+'" src="'+escAttr(src)+'" alt="'+escAttr(name)+'" title="'+escAttr(name)+'" loading="lazy" onerror="this.remove()">':'';
}
function myListRacingDetailKey(event){
  const series=String((event&&event.series)||'').toLowerCase();
  if(series==='f1')return 'f1-team';
  const loaded=(_myListRacingDrivers||[]).find(row=>String(row.series||'').toLowerCase()===series);
  if(loaded&&loaded.key)return String(loaded.key);
  return ({wrc:'wrc-oliver-solberg',indycar:'indycar-dennis-hauger',f2:'f2-martinius-stenshorne'})[series]||'';
}
function setupDemoCover(label,color){
  const svg='<svg xmlns="http://www.w3.org/2000/svg" width="180" height="260"><rect width="180" height="260" rx="12" fill="'+color+'"/><circle cx="90" cy="92" r="42" fill="#ffffff18"/><text x="90" y="105" text-anchor="middle" font-size="50">📺</text><text x="90" y="190" text-anchor="middle" font-family="sans-serif" font-size="18" font-weight="700" fill="#fff">'+label+'</text><text x="90" y="216" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#ffffffaa">TVMate demo</text></svg>';
  return 'data:image/svg+xml;charset=UTF-8,'+encodeURIComponent(svg);
}
function timelineLoadPrefs(){
  if(_myTimelinePrefsLoaded)return;_myTimelinePrefsLoaded=true;
  try{
    const kind=localStorage.getItem('tvmateTimelineFilter');if(['all','show','movie','game','sport','f1'].includes(kind))_myTimelineFilter=kind;
    const saved=JSON.parse(localStorage.getItem('tvmateTimelineSettings')||'{}');
    _myTimelineSettings=Object.assign({},_myTimelineSettings,saved||{});
  }catch(e){}
}
function timelineSavePrefs(){try{localStorage.setItem('tvmateTimelineFilter',_myTimelineFilter);localStorage.setItem('tvmateTimelineSettings',JSON.stringify(_myTimelineSettings));}catch(e){}}
function timelineFilterGroup(kind){return kind==='team'?'sport':(kind==='f1'?'f1':kind);}
function timelineSettingsChanged(){const x=_myTimelineSettings;return x.recent!==true||x.live!==true||x.upcoming!==true||Number(x.maxPerCategory||0)!==0;}
function timelineControlsHtml(){
  const kinds=[['all','All'],['show','Shows'],['movie','Movies'],['game','Games'],['sport','Sports'],['f1','Racing']];let h='<div class="mytimelinecontrols">';
  for(const k of kinds)h+='<button class="mytimelinefilter '+k[0]+(_myTimelineFilter===k[0]?' on':'')+'" data-kind="'+k[0]+'" onclick="setTimelineFilter(this.dataset.kind)">'+esc(tr(k[1]))+'</button>';
  h+='<button class="mytimelinefilter settings'+(timelineSettingsChanged()?' changed':'')+'" onclick="toggleTimelineSettings(this)">&#9881; '+esc(tr('Filter'))+'</button>';
  h+='<div class="mytimelinefilterpanel hide"><h4>'+esc(tr('Timeline settings'))+'</h4><div class="timelinechecks">'
    +'<label><input type="checkbox" data-setting="recent" '+(_myTimelineSettings.recent?'checked':'')+' onchange="setTimelineSetting(this)"> '+esc(tr('Recently'))+'</label>'
    +'<label><input type="checkbox" data-setting="live" '+(_myTimelineSettings.live?'checked':'')+' onchange="setTimelineSetting(this)"> '+esc(tr('Live now'))+'</label>'
    +'<label><input type="checkbox" data-setting="upcoming" '+(_myTimelineSettings.upcoming?'checked':'')+' onchange="setTimelineSetting(this)"> '+esc(tr('Upcoming'))+'</label></div>'
    +'<label><span>'+esc(tr('Maximum per category'))+'</span><select data-setting="maxPerCategory" onchange="setTimelineSetting(this)">'
    +[[0,'App default'],[2,'2'],[4,'4'],[6,'6'],[8,'8'],[12,'12']].map(x=>'<option value="'+x[0]+'"'+(Number(_myTimelineSettings.maxPerCategory||0)===x[0]?' selected':'')+'>'+esc(tr(x[1]))+'</option>').join('')+'</select></label>'
    +'<button class="timelinefilterreset" onclick="resetTimelineSettings()">'+esc(tr('Reset to default'))+'</button></div></div>';return h;
}
function setTimelineFilter(kind){_myTimelineFilter=['all','show','movie','game','sport','f1'].includes(kind)?kind:'all';timelineSavePrefs();renderMyListTimeline();}
function toggleTimelineSettings(btn){const wrap=btn.closest('.mytimelinecontrols'),panel=wrap?wrap.querySelector('.mytimelinefilterpanel'):null;if(panel)panel.classList.toggle('hide');}
function setTimelineSetting(input){const key=input.dataset.setting;if(key==='maxPerCategory')_myTimelineSettings[key]=Math.max(0,Number(input.value||0));else _myTimelineSettings[key]=!!input.checked;timelineSavePrefs();renderMyListTimeline();}
function resetTimelineSettings(){_myTimelineSettings={recent:true,live:true,upcoming:true,maxPerCategory:0};timelineSavePrefs();renderMyListTimeline();}
function renderMyListTimeline(){
  const el=document.getElementById('myListTimeline'),standalone=document.getElementById('myTimelineStandalone');if(!el&&!standalone)return;
  const now=Date.now(),moments=[];
  if(_footballEnabled)for(const row of _myListTeamMoments)moments.push({kind:'team',ts:row.ts,live:row.live,data:row});
  if(_f1Enabled)for(const row of _myListF1Moments)moments.push({kind:'f1',ts:row.ts,live:racingEventIsLive(row.event,now),data:row});
  for(const row of _myListMovieMoments)moments.push({kind:'movie',ts:row.ts,live:false,data:row});
  for(const row of _myListGameMoments)moments.push({kind:'game',ts:row.ts,live:false,data:row});
  for(const row of _myListShowMoments)moments.push({kind:'show',ts:row.ts,live:false,data:row});
  const realWatch=((_myListFavData.shows||[]).length+(_myListFavData.movies||[]).length)>0;
  if(_profileConfig.setup_demo_content&&!realWatch){
    moments.push({kind:'show',ts:now-6*3600000,live:false,data:{upcoming:false,ep:{show_name:'Example Show',season:1,episode_num:1,title:'Welcome to TVMate',cover:setupDemoCover('EXAMPLE SHOW','#7a3d12'),available:false,series_id:'',catalog_id:''}}});
    moments.push({kind:'movie',ts:now+36*3600000,live:false,data:{movie:{name:'Example Movie',year:new Date().getFullYear(),cover:setupDemoCover('EXAMPLE MOVIE','#164a72'),stream_found:false}}});
  }
  timelineLoadPrefs();
  const controls=timelineControlsHtml();
  let filtered=moments.filter(m=>_myTimelineFilter==='all'||timelineFilterGroup(m.kind)===_myTimelineFilter);
  if(!_myTimelineSettings.recent)filtered=filtered.filter(m=>m.live||m.ts>=now);
  if(!_myTimelineSettings.live)filtered=filtered.filter(m=>!m.live);
  if(!_myTimelineSettings.upcoming)filtered=filtered.filter(m=>m.live||m.ts<now);
  const maxPerCategory=Math.max(0,Number(_myTimelineSettings.maxPerCategory||0));
  if(maxPerCategory){
    const keep=new Set(),groups=new Map();
    for(const m of filtered){const key=timelineFilterGroup(m.kind);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(m);}
    for(const rows of groups.values())for(const m of rows.sort((a,b)=>Math.abs(a.ts-now)-Math.abs(b.ts-now)).slice(0,maxPerCategory))keep.add(m);
    filtered=filtered.filter(m=>keep.has(m));
  }
  if(!filtered.length){const empty=controls+'<div class="muted" style="padding:12px 0">'+tr('Nothing happening around now.')+'</div>';if(el)el.innerHTML=empty;if(standalone)standalone.innerHTML=empty;return;}
  const recent=filtered.filter(m=>!m.live&&m.ts<now).sort((a,b)=>a.ts-b.ts).map(m=>Object.assign({section:'recent'},m));
  const live=filtered.filter(m=>m.live).sort((a,b)=>a.ts-b.ts).map(m=>Object.assign({section:'live'},m));
  const upcoming=filtered.filter(m=>!m.live&&m.ts>=now).sort((a,b)=>a.ts-b.ts).map(m=>Object.assign({section:'upcoming'},m));
  const ordered=recent.concat(live,upcoming);
  let h=controls+'<div class="mylisttimeline">';
  let section='';
  for(const moment of ordered){
    if(moment.section!==section){section=moment.section;const label=section==='recent'?tr('Recently'):(section==='live'?tr('Live now'):tr('Upcoming'));h+='<div class="mylisttimelinesection '+section+'">'+esc(label)+'</div>';}
    if(moment.kind==='team'){
      const row=moment.data,f=row.fixture;
      const when=row.live?tr('Live now'):(f.start?timelineUpcomingWhen(row.ts,false):tr('Next match'));
      h+='<div class="mylisttimelineentry'+(row.live?' is-live':'')+'">'+(row.live?'':'<div class="mylisttimelinewhen">'+esc(when)+'</div>')+'<div class="mylisttimelinebody mylisttimelinecontent"><span class="mylisttimelinekind sport">'+esc(tr('Sports'))+'</span>'+myListSportArtwork(f)+teamFixtureCard(f,row.live,true)+'</div></div>';
    }else if(moment.kind==='f1'){
      const row=moment.data,event=row.event,date=new Date(row.ts),when=moment.live?tr('Live now'):timelineUpcomingWhen(row.ts,!!event.all_day);
      const racingUrl=event.url||('https://www.formula1.com/en/racing/'+date.getFullYear()),series=event.series_name||'Formula 1';
      const available=(event.channels||[]).length?'<span class="cc mylisttimelineavail" title="'+escAttr(tr('Channels available'))+'">TV</span>':'';
      h+='<div class="mylisttimelineentry'+(moment.live?' is-live':'')+'">'+(moment.live?'':'<div class="mylisttimelinewhen">'+esc(when)+'</div>')+'<div class="mylisttimelinebody mylisttimelinecontent mylisttimelinef1'+((event.channels||[]).length?' haschannels':'')+'" data-driver-key="'+escAttr(myListRacingDetailKey(event))+'" data-url="'+escAttr(racingUrl)+'"><span class="mylisttimelinekind f1">'+esc(tr('Racing'))+'</span>'+myListRacingArtwork(event)+'<div><b>'+esc(event.race)+'</b><div class="moviemeta">'+esc(series)+' · '+esc(event.session)+(event.circuit&&event.circuit!==event.race?' · '+esc(event.circuit):'')+'</div></div>'+available+'</div></div>';
    }else if(moment.kind==='movie'){
      const row=moment.data,m=row.movie,cover=m.cover?'<img src="'+escAttr(m.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'',when=row.ts<Date.now()?timelineReleasedWhen(row.ts):timelineUpcomingWhen(row.ts,true);
      const action=m.stream_found?'<div class="movieactions"><span class="moviemeta">'+tr('Stream found in playlist')+'</span><button class="btnvlc movievlc" data-sid="'+escAttr(String(m.stream_id))+'" data-ext="'+escAttr(m.extension||'mp4')+'">&#9658; VLC</button></div>':'<div class="movieactions"><button class="ghost" disabled>'+tr('Not available')+'</button></div>';
      h+='<div class="mylisttimelineentry"><div class="mylisttimelinewhen">'+esc(when)+'</div><div class="mylisttimelinebody mylisttimelineepisode"><span class="mylisttimelinekind movie">'+esc(tr('Movie'))+'</span>'+cover+'<div><b>'+esc(m.name)+'</b><div class="moviemeta">'+esc(m.year||'')+'</div>'+action+'</div></div></div>';
    }else if(moment.kind==='game'){
      const row=moment.data,g=row.game,cover=g.cover?'<img src="'+escAttr(g.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'',when=row.ts<Date.now()?timelineReleasedWhen(row.ts):timelineUpcomingWhen(row.ts,true);
      const gameUrl=g.url||('https://store.steampowered.com/app/'+encodeURIComponent(String(g.app_id||''))+'/');
      h+='<div class="mylisttimelineentry"><div class="mylisttimelinewhen">'+esc(when)+'</div><div class="mylisttimelinebody mylisttimelinegame" data-url="'+escAttr(gameUrl)+'"><span class="mylisttimelinekind game">'+esc(tr('Game'))+'</span>'+cover+'<div><b>'+esc(g.name||'Game')+'</b><div class="moviemeta">'+esc(g.release_text||'')+'</div></div></div></div>';
    }else{
      const row=moment.data,ep=row.ep,cover=ep.cover?'<img src="'+escAttr(ep.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'',available=(!row.upcoming&&ep.available)?'<span class="cc mylisttimelineavail" title="Available to play">&#9654;</span>':'';
      const when=row.ts<Date.now()?timelineReleasedWhen(row.ts):timelineUpcomingWhen(row.ts,false);
      h+='<div class="mylisttimelineentry"><div class="mylisttimelinewhen">'+esc(when)+'</div><div class="mylisttimelinebody mylisttimelineepisode mylistshowcard" data-series="'+escAttr(String(ep.series_id||''))+'" data-catalog="'+escAttr(ep.catalog_id||'')+'"><span class="mylisttimelinekind show">'+esc(tr('Show'))+'</span>'+cover+'<div><b>'+esc(ep.show_name)+'</b><div class="moviemeta">S'+esc(ep.season)+'E'+esc(ep.episode_num)+' - '+esc(ep.title||'Episode')+'</div></div>'+available+'</div></div>';
    }
  }
  const html=h+'</div>';if(el)el.innerHTML=html;if(standalone)standalone.innerHTML=html;
}
async function removeFavCat(cat){
  await favPost({action:'remove_cat',category:cat});
  loadFavorites();
}
async function removeFavChan(sid){
  await favPost({action:'remove_channel',stream_id:sid});
  loadFavorites();
}
async function toggleFavChannel(sid,name,cat,starEl){
  const r=await favPost({action:'toggle_channel',stream_id:sid,name:name,category:cat});
  const ids=new Set((r.channel_ids||[]).map(String));
  _favChanSet=ids;
  if(starEl)starEl.classList.toggle('on',ids.has(String(sid)));
}
async function favSelectedCats(){
  const cats=Array.from(_selCats);
  if(!cats.length){alert('Tick some categories first.');return;}
  await favPost({action:'add_cats',categories:cats});
  toast('Added '+cats.length+' categor'+(cats.length===1?'y':'ies')+' to Profile');
}
async function favPlaylist(){
  const items=Array.from(_playlist.entries()).map(function(kv){return {stream_id:kv[0],name:kv[1].name,category:kv[1].category||''};});
  if(!items.length){alert('Playlist is empty.');return;}
  await favPost({action:'add_channels',channels:items});
  toast('Added '+items.length+' channel'+(items.length===1?'':'s')+' to Profile');
}
function toast(msg,duration){
  let t=document.getElementById('_toast');
  if(!t){t=document.createElement('div');t.id='_toast';t.style.cssText='position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--card2);border:1px solid var(--line2);color:var(--fg);padding:10px 18px;border-radius:8px;z-index:200;font-size:14px';document.body.appendChild(t);}
  t.textContent=msg;t.style.opacity='1';
  clearTimeout(t._h);t._h=setTimeout(function(){t.style.opacity='0';},duration||2200);
  t.style.transition='opacity .3s';
}
// ---- My TV ----
let _tvSource='__fav__';   // '__fav__' or a category name
let _tvChannels=[];
let _tvPlaying=null;
async function initMytv(){
  await buildTvRail();
  await loadTvSource('__fav__');
}
async function buildTvRail(){
  const r=await api('/api/favorites');
  const rail=document.getElementById('tvRail');
  let h='<button class="tvsrc'+(_tvSource==='__fav__'?' on':'')+'" data-src="__fav__">\u2605 '+tr('Favorite Channels')+'</button>';
  for(const c of (r.categories||[]))
    h+='<button class="tvsrc'+(_tvSource===c?' on':'')+'" data-src="'+escAttr(c)+'">'+_flagFor(c)+' '+esc(c)+'</button>';
  rail.innerHTML=h;
}
async function loadTvSource(src){
  _tvSource=src;
  document.querySelectorAll('.tvsrc').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-src')===src);});
  const body=document.getElementById('tvGuideBody');
  body.innerHTML='<div class="muted" style="padding:16px">Loading...</div>';
  if(src==='__fav__'){
    const r=await api('/api/favorites');
    _tvChannels=(r.channels||[]).map(function(c){return {stream_id:c.stream_id,name:c.name,category:c.category||'',url:c.url,logo:c.logo||''};});
  }else{
    const r=await api('/api/channels?q=&cat='+encodeURIComponent(src));
    _tvChannels=(r.channels||[]).map(function(c){return {stream_id:c.stream_id,name:c.name,category:c.category||'',url:c.url,logo:c.logo||''};});
  }
  await refreshFavState();
  // Restore EPG from disk/memory only. Entering Live TV must not silently
  // refresh the provider; the Update EPG button remains the network action.
  const epgIds=_tvChannels.map(function(c){return String(c.stream_id);}).filter(Boolean);
  if(epgIds.length){
    try{const j=await api('/api/epg?cached=1&ids='+encodeURIComponent(epgIds.join(',')));if(!j.error)_tvEpg=Object.assign({},_tvEpg,j.epg||{});}catch(e){}
  }
  renderTvGuide();
  maybeAutoRefreshEpg();
}
let _tvEpg={};   // stream_id -> [{title,start_ts,stop_ts},...]
let _tvAutoEpgCheckAt=0,_tvAutoEpgBusy=false;
async function maybeAutoRefreshEpg(){
  const now=Date.now();if(_tvAutoEpgBusy||now-_tvAutoEpgCheckAt<15*60*1000)return;
  _tvAutoEpgCheckAt=now;_tvAutoEpgBusy=true;
  try{
    // The server contacts the provider only for missing or >12-hour-old rows.
    const j=await api('/api/epg?favorites=1');
    if(!j.error){_tvEpg=Object.assign({},_tvEpg,j.epg||{});if(!mytvView.classList.contains('hide'))renderTvGuide();}
  }catch(e){}finally{_tvAutoEpgBusy=false;}
}
// Keep the guide clock moving even when Live TV is left open. This only
// re-renders already cached data; it never refreshes EPG over the network.
setInterval(function(){
  if(!mytvView.classList.contains('hide')&&_tvChannels.length)renderTvGuide();
},60*1000);
function renderTvGuide(){
  const head=document.getElementById('tvTimeHead');
  const body=document.getElementById('tvGuideBody');
  // simple time header from the current half hour
  const d=new Date();d.setMinutes(d.getMinutes()<30?0:30,0,0);
  const base=d.getTime();
  const slotStart=[];
  for(let i=0;i<5;i++){slotStart.push(base+i*30*60000);}
  const nowPct=Math.max(0,Math.min(100,(Date.now()-base)/(5*30*60000)*100));
  head.innerHTML=slotStart.map(function(ms){const t=new Date(ms);return '<div class="tvtimeslot">'+('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2)+'</div>';}).join('')+'<span class="tvnowhead" style="left:'+nowPct.toFixed(3)+'%"></span>';
  const winStart=slotStart[0], winEnd=slotStart[4]+30*60000;
  if(!_tvChannels.length){body.innerHTML='<div class="muted" style="padding:16px">'+tr('No channels here.')+'</div>';return;}
  let h='';
  for(const c of _tvChannels){
    const playing=(_tvPlaying!==null&&String(_tvPlaying)===String(c.stream_id))?' playing':'';
    const fav=_favChanSet.has(String(c.stream_id))?' on':'';
    h+='<div class="tvrow" data-sid="'+escAttr(String(c.stream_id))+'">'
      +'<div class="tvchan'+playing+'" data-sid="'+escAttr(String(c.stream_id))+'">'
      +(_tvSource==='__fav__'?'<span class="tvdrag" draggable="true" title="Drag to reorder">&#9776;</span>':'')
      +'<button class="tvvlc" data-sid="'+escAttr(String(c.stream_id))+'">VLC</button>'
      +(c.logo?channelLogo(c,'tvlogo'):'<span class="tvflag">'+_flagFor(c.category||c.name)+'</span>')
      +'<span class="tvname">'+esc(c.name)+'</span>'
      +'<span class="favstar'+fav+'" data-sid="'+escAttr(String(c.stream_id))+'" data-name="'+escAttr(c.name)+'" data-cat="'+escAttr(c.category||'')+'" title="Favorite">\u2605</span>'
      +'</div>'
      +'<div class="tvprog" style="--nowpct:'+nowPct.toFixed(3)+'%">'+epgCellHtml(c.stream_id,winStart,winEnd)+'</div></div>';
  }
  body.innerHTML=h;
}
let _tvDragSid=null;
document.addEventListener('dragstart',function(e){
  const handle=e.target.closest('.tvdrag');
  if(!handle||_tvSource!=='__fav__')return;
  const row=handle.closest('.tvrow');
  _tvDragSid=row?row.getAttribute('data-sid'):null;
  if(!_tvDragSid)return;
  row.classList.add('tvdragging');
  e.dataTransfer.effectAllowed='move';
  e.dataTransfer.setData('text/plain',_tvDragSid);
});
document.addEventListener('dragover',function(e){
  const row=e.target.closest('.tvrow');
  if(!_tvDragSid||!row||_tvSource!=='__fav__')return;
  e.preventDefault();
  document.querySelectorAll('.tvrow.tvdragover').forEach(r=>r.classList.remove('tvdragover'));
  row.classList.add('tvdragover');
  e.dataTransfer.dropEffect='move';
});
document.addEventListener('drop',async function(e){
  const row=e.target.closest('.tvrow');
  if(!_tvDragSid||!row||_tvSource!=='__fav__')return;
  e.preventDefault();
  const targetSid=row.getAttribute('data-sid');
  const from=_tvChannels.findIndex(c=>String(c.stream_id)===String(_tvDragSid));
  const to=_tvChannels.findIndex(c=>String(c.stream_id)===String(targetSid));
  if(from>=0&&to>=0&&from!==to){
    const moved=_tvChannels.splice(from,1)[0];
    _tvChannels.splice(to,0,moved);
    renderTvGuide();
    await favPost({action:'reorder_channels',stream_ids:_tvChannels.map(c=>c.stream_id)});
  }
  _tvDragSid=null;
});
document.addEventListener('dragend',function(){
  _tvDragSid=null;
  document.querySelectorAll('.tvrow.tvdragging,.tvrow.tvdragover').forEach(r=>r.classList.remove('tvdragging','tvdragover'));
});
function epgWallClockTs(value,fallback){
  // Xtream's EPG `start`/`end` strings are schedule wall-clock values. Some
  // servers also expose start_timestamp as if that wall clock were UTC; using
  // that epoch in a browser then shifts Norwegian listings by +1/+2 hours.
  // Build the raw schedule time in the viewer's local timezone when available.
  const s=String(value||'').trim(),m=s.match(/^(\\d{4})-(\\d{2})-(\\d{2})[ T](\\d{1,2}):(\\d{2})(?::(\\d{2}))?/);
  if(m){const d=new Date(Number(m[1]),Number(m[2])-1,Number(m[3]),Number(m[4]),Number(m[5]),Number(m[6]||0));const ts=d.getTime()/1000;if(Number.isFinite(ts))return ts;}
  return Number(fallback)||0;
}
function epgCellHtml(sid,winStart,winEnd){
  const progs=_tvEpg[String(sid)];
  if(!progs||!progs.length)return '<span class="epgnone muted">'+tr('No program info')+'</span>';
  const nowSec=Date.now()/1000,ws=winStart/1000,we=winEnd/1000,span=Math.max(1,we-ws);
  const timed=progs.filter(function(p){
    const start=epgWallClockTs(p.start,p.start_ts),stop=epgWallClockTs(p.end,p.stop_ts)||start+1800;
    if(!p.title||!start)return false;
    return stop>ws&&start<we;
  }).sort(function(a,b){return epgWallClockTs(a.start,a.start_ts)-epgWallClockTs(b.start,b.start_ts);});
  if(!timed.length){
    // Never pin an expired programme to the left edge of the current grid.
    // Only a genuinely upcoming item may use the compact fallback display.
    const next=progs.filter(p=>p.title&&epgWallClockTs(p.start,p.start_ts)>=ws).sort((a,b)=>epgWallClockTs(a.start,a.start_ts)-epgWallClockTs(b.start,b.start_ts))[0];
    if(!next)return '<span class="epgnone muted">'+tr('No program info')+'</span>';
    const nextStart=epgWallClockTs(next.start,next.start_ts);if(nextStart>=we)return '<span class="epgnone muted">'+tr('No program info')+'</span>';
    let tm='';if(nextStart){const t=new Date(nextStart*1000);tm=('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2);}
    return '<span class="epgfallback"><span class="epgt">'+tm+'</span><span class="epgtitle">'+esc(next.title)+'</span></span>';
  }
  return timed.map(function(p){
    const start=epgWallClockTs(p.start,p.start_ts),rawStop=epgWallClockTs(p.end,p.stop_ts)||start+1800,stop=Math.max(start+60,rawStop);
    const visibleStart=Math.max(ws,start),visibleStop=Math.min(we,stop);
    const left=Math.max(0,(visibleStart-ws)/span*100),width=Math.max(.8,(visibleStop-visibleStart)/span*100);
    const live=start<=nowSec&&stop>nowSec;
    const t=new Date(start*1000),tm=('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2);
    const cls='epgprog'+(live?' live':'')+(width<12?' compact':'');
    return '<span class="'+cls+'" style="left:'+left.toFixed(3)+'%;width:calc('+width.toFixed(3)+'% - 2px)" title="'+escAttr(tm+' '+p.title)+'"><span class="epgt">'+tm+'</span><span class="epgtitle">'+esc(p.title)+'</span></span>';
  }).join('');
}
function tvPlayerGuide(){
  return document.querySelector('#mytvView .tvguide');
}
function tvSetMini(mini){
  const slot=document.getElementById('tvPlayerSlot'),guide=tvPlayerGuide();
  if(!slot||!slot.classList.contains('on'))return;
  const inLiveTv=!mytvView.classList.contains('hide');
  if(mini){
    if(slot.parentElement!==document.body)document.body.appendChild(slot);
    slot.classList.remove('sectionmax');
    slot.classList.add('mini');
  }else if(inLiveTv){
    slot.classList.remove('mini','sectionmax');
    if(guide&&slot.parentElement!==guide)guide.appendChild(slot);
  }else{
    if(slot.parentElement!==document.body)document.body.appendChild(slot);
    slot.classList.remove('mini');
    slot.classList.add('sectionmax');
  }
  const btn=slot.querySelector('.tvminbtn'),hit=slot.querySelector('.tvvideohit');
  const label=mini?'Fullscreen player':'Minimize player';
  if(btn){btn.title=label;btn.setAttribute('aria-label',label);btn.textContent=mini?'\u2196':'\u2198';}
  if(hit)hit.setAttribute('aria-label',label);
}
async function tvPlay(sid,name){
  const slot=document.getElementById('tvPlayerSlot'),guide=tvPlayerGuide();
  const wasMini=slot.classList.contains('mini');
  _tvPlaying=sid;
  slot.classList.add('on');
  if(!wasMini&&guide&&slot.parentElement!==guide)guide.appendChild(slot);
  slot.innerHTML='<div class="tvplayerbar"><span>'+esc(name||'')+'</span><div class="tvplayeractions"><button type="button" class="tvminbtn" title="Minimize player" aria-label="Minimize player" onclick="tvToggleMini()">&#8600;</button><button class="pclose" onclick="tvStop()">&times;</button></div></div><video id="tvVideo" controls autoplay playsinline></video><button type="button" class="tvvideohit" aria-label="Minimize player" onclick="tvToggleMini()"></button>';
  tvSetMini(wasMini);
  renderTvGuide();
  const video=document.getElementById('tvVideo');
  let urls;
  try{urls=await api('/api/hls?id='+encodeURIComponent(sid));if(urls.error||!urls.hls)throw new Error('stream url');}catch(e){return;}
  if(window._tvPlaybackController){window._tvPlaybackController.stop();window._tvPlaybackController=null;}
  window._tvPlaybackController=startSmartStream(video,urls,function(s){
    const bar=slot.querySelector('.tvplayerbar span');if(bar)bar.title=s||'';
  },function(h,t){window._tvhls=h;window._tvmpegts=t;});
}
function tvToggleMini(){
  const slot=document.getElementById('tvPlayerSlot');
  if(!slot||!slot.classList.contains('on'))return;
  const inLiveTv=!mytvView.classList.contains('hide');
  if(slot.classList.contains('mini')){
    if(inLiveTv){tvSetMini(false);return;}
    tvSetMini(false);
    requestPlayerFullscreen(slot);
    return;
  }
  if(playerFullscreenElement()===slot)exitPlayerFullscreen();
  tvSetMini(true);
}
function tvStop(){
  _tvPlaying=null;
  document.body.classList.remove('tvsectionplay');
  if(window._tvPlaybackController){window._tvPlaybackController.stop();window._tvPlaybackController=null;}
  if(window._tvhls){try{window._tvhls.destroy();}catch(e){}window._tvhls=null;}
  if(window._tvmpegts){destroyMpegtsPlayer(window._tvmpegts);window._tvmpegts=null;}
  const slot=document.getElementById('tvPlayerSlot'),guide=tvPlayerGuide();
  slot.classList.remove('on','mini','sectionmax');
  if(guide&&slot.parentElement!==guide)guide.appendChild(slot);
  slot.innerHTML='';
  renderTvGuide();
}
async function epgRefresh(){
  const btn=document.getElementById('epgRefresh');
  const old=btn.innerHTML;
  btn.innerHTML='<span>'+tr('Loading EPG...')+'</span>';btn.disabled=true;
  let modal=document.getElementById('epgLoadProgress');
  if(!modal){
    modal=document.createElement('div');modal.id='epgLoadProgress';modal.className='epgloadback';
    modal.innerHTML='<div class="epgloadbox"><div class="epgloadtitle">'+esc(tr('Updating TV guide'))+'</div><div class="epgloadstage" id="epgLoadStage"></div><div class="epgloadbar"><span id="epgLoadBar"></span></div><div class="epgloadmeta"><span id="epgLoadCount">0 / 0</span><span id="epgLoadFound"></span></div></div>';
    document.body.appendChild(modal);
  }else modal.classList.remove('hide');
  const stage=document.getElementById('epgLoadStage'),bar=document.getElementById('epgLoadBar'),count=document.getElementById('epgLoadCount'),found=document.getElementById('epgLoadFound');
  try{
    stage.textContent=tr('Finding channels in your favorites...');count.textContent='';found.textContent='';bar.style.width='3%';
    const plan=await api('/api/epg_targets');
    if(plan.error)throw new Error(plan.error||'EPG failed');
    // Populate what the user is looking at first. The complete favorite/category
    // guide still refreshes afterwards, but a large EPG no longer makes the
    // currently open category wait behind hundreds of unrelated channels.
    const visibleIds=_tvChannels.map(c=>String(c.stream_id||'')).filter(Boolean),visibleSet=new Set(visibleIds);
    const planned=(plan.ids||[]).map(String),ids=visibleIds.filter(id=>planned.includes(id)).concat(planned.filter(id=>!visibleSet.has(id)));
    const total=ids.length;let updated=0,noEpg=0,failed=0,safeMode=false;
    count.textContent=total+' '+tr('channels');
    found.textContent=tr('One bulk guide download');
    stage.textContent=tr('Downloading and processing the provider TV guide...');bar.style.width='18%';
    let waitStep=0;
    const waitMessages=[tr('Parsing programme information...'),tr('Matching guide data to favorite channels...'),tr('Large provider guides may take a little while...')];
    const waitTimer=setInterval(()=>{stage.textContent=waitMessages[Math.min(waitStep++,waitMessages.length-1)];bar.style.width=Math.min(82,28+waitStep*14)+'%';},2200);
    let j;
    try{j=await api('/api/epg?force=1&favorites=1');}finally{clearInterval(waitTimer);}
    if(j.error)throw new Error(j.error||'EPG failed');
    _tvEpg=Object.assign({},_tvEpg,j.epg||{});
    const s=j.stats||{};updated=Number(s.updated)||0;noEpg=Number(s.no_data)||0;failed=Number(s.failed)||0;safeMode=!!s.safe_mode;
    const bulk=Number(s.xmltv_filled)||0,fallback=Number(s.fallback_updated)||0;
    count.textContent=total+' '+tr('channels checked');
    found.textContent=tr('XMLTV')+' '+bulk+(fallback?(' · '+tr('Fallback')+' '+fallback):'')+' · '+tr('No EPG')+' '+noEpg+(failed?(' · '+tr('Failed')+' '+failed):'');bar.style.width='100%';
    renderTvGuide();
    stage.textContent=tr('TV guide is ready.')+' '+updated+' '+tr('channels updated.');bar.style.width='100%';
    if(!total){toast(tr('No favorites to load EPG for.'));}
    else toast(tr('EPG loaded')+': '+tr('Updated')+' '+updated+' · '+tr('No EPG')+' '+noEpg+' · '+tr('Failed')+' '+failed,7000);
  }catch(e){stage.textContent=tr('EPG failed')+': '+String(e&&e.message||e);bar.style.background='#8f2d35';toast(tr('EPG failed'));await new Promise(resolve=>setTimeout(resolve,2800));}
  await new Promise(resolve=>setTimeout(resolve,650));
  if(modal)modal.classList.add('hide');
  btn.innerHTML=old;btn.disabled=false;
}
// Event delegation: any Copy button's data-url is copied on click.
document.addEventListener('click',function(e){
  const timelineTeamFixture=e.target.closest('.teamfixture[data-profile-fixture="1"]');
  if(timelineTeamFixture){showTeams(timelineTeamFixture);return;}
  const teamFixture=e.target.closest('.teamfixture[data-fixture-card="1"]');
  if(teamFixture&&!e.target.closest('.btnplay,.btnvlc,.bchead')){const details=teamFixture.querySelector('.teamfixturebroadcasts'),opening=details&&details.classList.contains('hide');document.querySelectorAll('#teamsView .teamfixture.selectedfixture').forEach(card=>card.classList.remove('selectedfixture'));teamFixture.classList.toggle('selectedfixture',!!opening);if(details)details.classList.toggle('hide');if(opening)loadStoredFixtureChannels(teamFixture);return;}
  const teamRemove=e.target.closest('.teamremove');
  if(teamRemove){removeTeamFavorite(teamRemove.getAttribute('data-team-name'));return;}
  const teamFav=e.target.closest('.teamfavitem[data-team-search]');
  if(teamFav){selectMyTeam(teamFav.getAttribute('data-team-search')||'',teamFav.getAttribute('data-team-id')||'',teamFav.getAttribute('data-team-logo')||'');return;}
  const teamStar=e.target.closest('.teamstar');
  if(teamStar){toggleTeamFavorite(teamStar.getAttribute('data-team-name'),teamStar,teamStar.getAttribute('data-team-id'));return;}
  const teamFindFixtures=e.target.closest('.teamfindfixtures[data-team-fixtures]');
  if(teamFindFixtures){findSportsFixtures(teamFindFixtures.getAttribute('data-team-fixtures')||'',teamFindFixtures.getAttribute('data-team-id')||'');return;}
  const teamSearchHit=e.target.closest('.teamsearchhit[data-team-select]');
  if(teamSearchHit){selectMyTeam(teamSearchHit.getAttribute('data-team-select')||'',teamSearchHit.getAttribute('data-team-id')||'','');return;}
  const sourceExpand=e.target.closest('.latestsourceexpand');
  if(sourceExpand){const box=sourceExpand.parentElement.querySelector('.latestsources');if(box)box.classList.toggle('hide');return;}
  const timelineGame=e.target.closest('.mylisttimelinegame');
  if(timelineGame){const url=timelineGame.getAttribute('data-url');if(url)window.open(url,'_blank','noopener');return;}
  const timelineF1=e.target.closest('.mylisttimelinef1');
  if(timelineF1){showRacing(timelineF1.getAttribute('data-driver-key')||'');return;}
  const racingEvent=e.target.closest('.racingevent');
  if(racingEvent&&!e.target.closest('.btnplay,.btnvlc,.bchead')){if(racingEvent.classList.contains('haschannels')){const box=racingEvent.querySelector('.racingeventchannels');if(box)box.classList.toggle('hide');return;}const url=racingEvent.getAttribute('data-url');if(url)window.open(url,'_blank','noopener');return;}
  const lev=e.target.closest('.latestepisodevlc');
  if(lev){playLatestEpisode(lev.getAttribute('data-id'),lev.getAttribute('data-ext'),lev);return;}
  const myListShow=e.target.closest('.mylistshowcard');
  if(myListShow){const sid=myListShow.getAttribute('data-series')||'',cid=myListShow.getAttribute('data-catalog')||'';if(sid){showShows();loadShow(sid);return;}if(cid){showShows();loadExternalShow(cid);return;}}
  const latestShow=e.target.closest('.latestshowcard');
  if(latestShow){const sid=latestShow.getAttribute('data-series')||'',cid=latestShow.getAttribute('data-catalog')||'';if(sid){loadShow(sid);return;}if(cid){loadExternalShow(cid);return;}}
  const ss=e.target.closest('.showstar');
  if(ss){toggleShowFavorite({catalog_id:ss.getAttribute('data-catalog'),show_key:ss.getAttribute('data-show-key')||ss.getAttribute('data-key'),series_id:ss.getAttribute('data-series')||null,series_ids:(ss.getAttribute('data-series-ids')||ss.getAttribute('data-series')||'').split(',').filter(Boolean),name:ss.getAttribute('data-name'),cover:ss.getAttribute('data-cover'),year:ss.getAttribute('data-year'),rating:ss.getAttribute('data-rating')},ss);return;}
  const sr=e.target.closest('.showremove');
  if(sr){removeShowFavorite(sr.getAttribute('data-key'));return;}
  const sc=e.target.closest('.showcard');
  if(sc){const ids=sc.getAttribute('data-series')||'';if(ids)loadShow(ids);else if(sc.getAttribute('data-catalog'))loadExternalShow(sc.getAttribute('data-catalog'));return;}
  const sf=e.target.closest('.showfav');
  if(sf){const ids=sf.getAttribute('data-series')||'';if(ids)loadShow(ids);else if(sf.getAttribute('data-catalog'))loadExternalShow(sf.getAttribute('data-catalog'));return;}
  const ev=e.target.closest('.episodevlc');
  if(ev){playEpisodeQueue(ev.getAttribute('data-season'),ev.getAttribute('data-episode'),ev.getAttribute('data-source'),ev);return;}
  const mv=e.target.closest('.movievlc');
  if(mv){playMovieVLC(mv.getAttribute('data-sid'),mv.getAttribute('data-ext'),mv);return;}
  const ms=e.target.closest('.moviestar');
  if(ms){toggleMovieFavorite({catalog_id:ms.getAttribute('data-catalog'),stream_id:ms.getAttribute('data-sid'),name:ms.getAttribute('data-name'),extension:ms.getAttribute('data-ext'),year:ms.getAttribute('data-year'),rating:ms.getAttribute('data-rating'),cover:ms.getAttribute('data-cover')},ms);return;}
  const recentMovie=e.target.closest('.recentmovie');
  if(recentMovie){document.getElementById('movieQ').value=recentMovie.getAttribute('data-query')||'';searchMovies();return;}
  const mr=e.target.closest('.movieremove');
  if(mr){removeMovieFavorite(mr.getAttribute('data-key'));return;}
  const favoriteMovie=e.target.closest('.moviefav');
  if(favoriteMovie){document.getElementById('movieQ').value=favoriteMovie.getAttribute('data-query')||'';searchMovies();return;}
  const tt=e.target.closest('.teamtab');
  if(tt){_activeTeam=parseInt(tt.getAttribute('data-team'),10)||0;renderTeamSwitch();renderActiveTeam();return;}
  const bh=e.target.closest('.bchead');
  if(bh){
    const row=bh.parentElement;
    const box=row.querySelector('.bcchans');
    const opening=box&&box.classList.contains('hide');
    const scope=row.parentElement;if(scope)scope.querySelectorAll(':scope > .bcrow.open').forEach(other=>{if(other!==row){other.classList.remove('open');const otherBox=other.querySelector('.bcchans');if(otherBox)otherBox.classList.add('hide');}});
    if(box)box.classList.toggle('hide',!opening);
    row.classList.toggle('open',!!opening);
    return;
  }
  const src=e.target.closest('.tvsrc');
  if(src){loadTvSource(src.getAttribute('data-src'));return;}
  const tvv=e.target.closest('.tvvlc');
  if(tvv){playVLC(tvv.getAttribute('data-sid'),tvv);return;}
  if(e.target.closest('.tvdrag'))return;
  const st=e.target.closest('.favstar');
  if(st){
    if(st.hasAttribute('data-favcat')){toggleFavCat(st.getAttribute('data-favcat'),st);return;}
    toggleFavChannel(st.getAttribute('data-sid'),st.getAttribute('data-name'),st.getAttribute('data-cat'),st);return;
  }
  const tvc=e.target.closest('.tvchan');
  if(tvc){const c=_tvChannels.find(function(x){return String(x.stream_id)===tvc.getAttribute('data-sid');});if(c)tvPlay(c.stream_id,c.name);return;}
  const rm=e.target.closest('.favrm');
  if(rm){if(rm.hasAttribute('data-cat'))removeFavCat(rm.getAttribute('data-cat'));else removeFavChan(rm.getAttribute('data-sid'));return;}
  const pb=e.target.closest('.btnplay');
  if(pb){playBrowser(pb.getAttribute('data-sid'),pb.getAttribute('data-name'));return;}
  const vb=e.target.closest('.btnvlc');
  if(vb){playVLC(vb.getAttribute('data-sid'),vb);return;}
  const mych=e.target.closest('.mydashchannel[data-sid]');
  if(mych){playBrowser(mych.getAttribute('data-sid'),mych.getAttribute('data-name'));return;}
  const b=e.target.closest('.copy');
  if(!b)return;
  const u=b.getAttribute('data-url')||'';
  navigator.clipboard.writeText(u).then(()=>{b.textContent='Copied';setTimeout(()=>b.textContent=tr('Copy URL'),1200);})
    .catch(()=>{b.textContent='Copy failed';setTimeout(()=>b.textContent=tr('Copy URL'),1500);});
});
// apply saved language
try{const sl=localStorage.getItem('tvmate_lang');if(sl==='no')setLang('no');else applyLang();}catch(e){applyLang();}
// open the user's default start section
(async function(){
  let start='mylist',checkShows=false,refreshIptv=false,refreshSports=false,startupConfig=null;
  try{const c=await api('/api/config');startupConfig=c;start=c.start_section||'mylist';checkShows=!!c.check_shows_on_startup;refreshIptv=!!c.refresh_iptv_on_startup;refreshSports=!!c.refresh_sports_on_startup;setLang(c.preferred_language||'en');applyProfileConfig(c);if(start==='teams'&&!_footballEnabled)start='mylist';if(start==='games'&&!_gamesEnabled)start='mylist';if(start==='racing'&&!_f1Enabled)start='mylist';}catch(e){}
  if(start==='search')start='channels'; // migrate the removed Search section
  if(start==='mytimeline'&&_myListLayout==='timeline')start='mylist';
  const map={channels:showChannels,mytv:showMytv,movies:showMovies,shows:showShows,games:showGames,racing:showRacing,teams:showTeams,mylist:showMylist,mytimeline:showMytimeline};
  (map[start]||showMylist)();
  history.replaceState({tvmate:true,section:start},'','#'+start);
  _historyReady=true;
  const setupDone=!!(startupConfig&&startupConfig.setup_complete===true);
  if(startupConfig&&!setupDone)setTimeout(()=>openProfileSetup(true,startupConfig),120);
  if(startupConfig&&setupDone)setTimeout(()=>maybeAutoRefreshSteamWishlist(startupConfig),900);
  if(setupDone&&(refreshIptv||refreshSports||checkShows))setTimeout(async function(){
    if(refreshIptv||refreshSports)await refreshOnStartup(refreshIptv,refreshSports);
    if(checkShows&&!refreshIptv)await checkShowsOnStartup();
  },500);
})();
window.addEventListener('popstate',function(ev){
  const state=ev.state;
  if(!state||!state.tvmate)return;
  const map={search:showChannels,channels:showChannels,mytv:showMytv,movies:showMovies,shows:showShows,games:showGames,racing:showRacing,teams:showTeams,mylist:showMylist,mytimeline:showMytimeline,settings:showSettings};
  const fn=map[state.section]||showMylist;
  _historyRestoring=true;
  try{
    fn();
    if(state.section==='shows'&&state.seriesId)loadShow(state.seriesId,true);
    else if(state.section==='shows'&&state.catalogId)loadExternalShow(state.catalogId,true);
  }finally{_historyRestoring=false;}
});
refreshStatus();
// --- auto-update ---
let _updateLatest=null;
async function openConfigFolder(){
  try{
    const j=await api('/api/open_folder',{method:'POST'});
    if(!j.ok)toast(tr('Could not open folder.')+(j.path?(' '+j.path):''));
  }catch(e){toast(tr('Could not open folder.'));}
}
function profileTimelineBackup(){
  let settings={};try{settings=JSON.parse(localStorage.getItem('tvmateTimelineSettings')||'{}')||{};}catch(e){}
  return {filter:localStorage.getItem('tvmateTimelineFilter')||'all',settings:settings};
}
async function exportProfileBackup(full){
  if(full&&!confirm('Full backup includes your Xtream login. Download and store it securely?'))return;
  const msg=document.getElementById('profileBackupMsg');if(msg)msg.textContent=tr('Preparing backup...');
  try{
    const backup=await api('/api/profile_backup_export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:full?'full':'profile',timeline:profileTimelineBackup()})});
    if(backup.error)throw new Error(backup.error);
    const safe=String((backup.config&&backup.config.profile_name)||'profile').replace(/[^a-z0-9_-]+/gi,'-').replace(/^-+|-+$/g,'')||'profile';
    const blob=new Blob([JSON.stringify(backup,null,2)],{type:'application/json'}),link=document.createElement('a');
    link.href=URL.createObjectURL(blob);link.download='TVMate-'+safe+'-'+(full?'full':'profile')+'-backup.json';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(link.href),1000);
    if(msg)msg.textContent=tr('Backup downloaded.');
  }catch(e){if(msg)msg.textContent=String(e.message||e);}
}
async function importProfileBackup(input){
  const msg=document.getElementById('profileBackupMsg'),file=input.files&&input.files[0];if(!file)return;
  try{
    if(file.size>5*1024*1024)throw new Error('Backup file is too large.');
    const backup=JSON.parse(await file.text());
    if(backup.format!=='olos-tvmate-backup')throw new Error('This is not a TVMate backup file.');
    const counts=backup.favorites||{},summary=['shows','movies','games','teams','channels'].map(k=>(Array.isArray(counts[k])?counts[k].length:0)+' '+k).join(', ');
    const full=backup.backup_type==='full';
    const warning=(full?'This full backup can replace the current Xtream login.\\n\\n':'')+'Merge backup into this profile?\\n'+summary;
    if(!confirm(warning))return;
    const result=await api('/api/profile_backup_import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({backup:backup})});
    if(result.error)throw new Error(result.error);
    const timeline=result.timeline||{};
    if(result.type==='full'){
      if(Object.prototype.hasOwnProperty.call(timeline,'filter'))localStorage.setItem('tvmateTimelineFilter',String(timeline.filter||'all'));else localStorage.removeItem('tvmateTimelineFilter');
      if(Object.prototype.hasOwnProperty.call(timeline,'settings'))localStorage.setItem('tvmateTimelineSettings',JSON.stringify(timeline.settings||{}));else localStorage.removeItem('tvmateTimelineSettings');
      if(msg)msg.textContent=tr('Full backup restored.');toast(tr('Full backup restored.'));location.reload();return;
    }
    if(timeline.filter)localStorage.setItem('tvmateTimelineFilter',timeline.filter);if(timeline.settings)localStorage.setItem('tvmateTimelineSettings',JSON.stringify(timeline.settings));
    _myTimelinePrefsLoaded=false;_catsLoaded=false;_latestEpisodesLoaded=false;_myListLoaded=false;await loadSettings();await loadFavorites();refreshStatus();
    if(msg)msg.textContent=tr('Backup imported and merged.');toast(tr('Backup imported and merged.'));
  }catch(e){if(msg)msg.textContent=tr('Could not import this backup.')+' '+String(e.message||e);}
  finally{input.value='';}
}
function _healthAgo(ts,now){
  if(!ts)return tr('not checked yet');
  const s=Math.max(0,Math.floor((now-ts)));
  if(s<90)return tr('just now');
  const m=Math.floor(s/60);
  if(m<90)return m+' '+tr('min ago');
  const h=Math.floor(m/60);
  if(h<48)return h+' '+tr('h ago');
  return Math.floor(h/24)+' '+tr('d ago');
}
function renderSourceHealth(data){
  const el=document.getElementById('sourceHealth');
  if(!el)return;
  const now=data.now||(Date.now()/1000);
  let h='';
  (data.sources||[]).forEach(function(s){
    let dot='dot-unknown',label=tr('not checked yet');
    const speed=s.latency_ms!=null?(' \u00b7 '+(s.latency_ms>=1000?(s.latency_ms/1000).toFixed(1)+'s':s.latency_ms+'ms')):'';
    if(s.ok===true){dot='dot-ok';label=tr('working')+(s.count!=null?(' \u00b7 '+s.count+' '+tr('items')):'')+speed+' \u00b7 '+_healthAgo(s.ts,now);}
    else if(s.ok===false){dot='dot-bad';label=(s.error?s.error:tr('failed'))+speed+' \u00b7 '+_healthAgo(s.ts,now);}
    else if(s.error){label=tr(s.error);}
    h+='<div class="srcrow"><span class="srcdot '+dot+'"></span><span class="srcname">'+esc(s.label)+'</span><span class="srcstat muted">'+esc(label)+'</span></div>';
  });
  el.innerHTML=h||('<span class="muted">'+tr('No sources.')+'</span>');
}
async function loadSourceHealth(){
  try{const j=await api('/api/source_health');renderSourceHealth(j);}catch(e){}
}
async function testSources(btn){
  if(btn){btn.disabled=true;btn.textContent=tr('Testing sources...');}
  try{
    const first=await api('/api/source_health'),keys=(first.sources||[]).map(s=>s.key),total=keys.length;let next=0,done=0;
    const worker=async function(){while(next<total){const key=keys[next++];try{const j=await api('/api/test_source',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key})});if(j&&j.sources)renderSourceHealth({sources:j.sources,now:Date.now()/1000});}catch(e){}done++;if(btn)btn.textContent=tr('Testing sources...')+' '+done+'/'+total;}};
    await Promise.all([worker(),worker(),worker()]);
    await loadSourceHealth();
  }catch(e){toast(tr('Could not test sources.'));}
  if(btn){btn.disabled=false;btn.textContent=tr('Test all sources');}
}
async function checkForUpdate(manual){
  const btn=document.getElementById('checkUpdateBtn');
  if(manual&&btn){btn.textContent=tr('Checking...');btn.disabled=true;}
  try{
    const j=await api('/api/update_check');
    if(j.available&&j.latest){
      _updateLatest=j.latest;
      document.getElementById('updateMsg').textContent=tr('Update available')+': v'+j.latest+' ('+tr('you have')+' v'+j.current+')';
      document.getElementById('updateBanner').classList.remove('hide');
    }else if(manual){
      toast(tr('You are on the latest version')+' (v'+(j.current||'')+')');
    }
  }catch(e){
    if(manual)toast(tr('Could not check for updates. Check your internet connection.'));
  }
  if(manual&&btn){btn.textContent=tr('Check for updates');btn.disabled=false;}
}
async function doUpdateNow(){
  const btn=document.getElementById('updateNowBtn');
  btn.textContent=tr('Downloading...');btn.disabled=true;
  try{
    const j=await api('/api/update_download',{method:'POST'});
    if(!j.ok){throw new Error('dl');}
    document.getElementById('updateMsg').textContent=tr('Update downloaded. Restart now to finish updating?');
    btn.textContent=tr('Restart now');btn.disabled=false;
    btn.onclick=doUpdateRestart;
  }catch(e){
    document.getElementById('updateMsg').textContent=tr('Update failed. Try again later.');
    btn.textContent=tr('Update now');btn.disabled=false;
  }
}
async function doUpdateRestart(){
  const btn=document.getElementById('updateNowBtn');
  btn.textContent=tr('Restarting...');btn.disabled=true;
  try{
    const j=await api('/api/update_restart',{method:'POST'});
    if(j.relaunch===false){
      document.getElementById('updateMsg').textContent=tr('Update installed. Please close this window and open Olo’s TVMate again.');
    }else{
      document.getElementById('updateMsg').textContent=tr('Updating... this window will reload shortly.');
      const started=Date.now();
      const waitForRestart=async function(){
        try{
          const response=await fetch('/api/ping',{cache:'no-store'});
          const ping=await response.json();
          if(ping&&ping.app==='olos-tvmate'){location.reload();return;}
        }catch(e){}
        if(Date.now()-started<60000)setTimeout(waitForRestart,1500);
        else document.getElementById('updateMsg').textContent=tr('Restart failed. Please close and reopen the app.');
      };
      setTimeout(waitForRestart,5000);
    }
  }catch(e){
    document.getElementById('updateMsg').textContent=tr('Restart failed. Please close and reopen the app.');
  }
}
function dismissUpdate(){document.getElementById('updateBanner').classList.add('hide');}
checkForUpdate();
</script>
</body></html>
"""

# --------------------------------------------------------------------------
# Request handler
# --------------------------------------------------------------------------

_LAST_ACTIVITY = time.monotonic()
_ACTIVITY_LOCK = threading.Lock()

def test_external_source(key):
    """Run one fresh health probe and return whether it was applicable."""
    cfg = load_config()
    x = Xtream(cfg)
    sid = str(cfg.get("steam_wishlist_id") or "").strip()
    probes = {
        "fotmob": lambda: http_get_json(FOTMOB_DAILY_MATCHES.format(
            date=time.strftime("%Y%m%d", time.localtime())), timeout=15),
        "tvmaze": lambda: _tvmaze_episode_schedule("Breaking Bad", force=True),
        "cinemeta": lambda: http_get_json(
            "https://v3-cinemeta.strem.io/catalog/movie/top/search=matrix.json", timeout=15),
        "f1": lambda: get_f1_schedule(force=True),
        "f2": lambda: get_fia_racing_weekends("f2", force=True),
        "f3": lambda: get_fia_racing_weekends("f3", force=True),
        "indycar": lambda: get_indycar_schedule(force=True),
        "wrc": lambda: get_wrc_schedule(force=True),
        "formulae": lambda: get_formulae_schedule(force=True),
        "wec": lambda: get_wec_schedule(force=True),
        "motogp": lambda: get_motogp_schedule(force=True),
    }
    if key == "xtream":
        if not x.configured():
            _record_source(key, None, error="Not configured")
            return {"key": key, "skipped": True}
        probe = lambda: x.login()
    elif key == "epg_xmltv":
        if not x.configured():
            _record_source(key, None, error="Not configured")
            return {"key": key, "skipped": True}
        probe = lambda: probe_xmltv(x)
    elif key == "steam":
        if not re.fullmatch(r"\d{17}", sid):
            _record_source(key, None, error="Not configured")
            return {"key": key, "skipped": True}
        probe = lambda: steam_public_profile(sid, force=True)
    else:
        probe = probes.get(key)
    if not probe:
        _record_source(key, None, error="Not available")
        return {"key": key, "skipped": True}
    started = time.perf_counter()
    try:
        result = probe()
        if key == "xtream":
            ok, detail = result
            if not ok:
                raise RuntimeError(detail or "login failed")
            count = None
        else:
            count = len(result) if isinstance(result, (list, tuple, dict)) else None
        latency = int((time.perf_counter() - started) * 1000)
        _record_source(key, True, count=count, latency_ms=latency)
        return {"key": key, "ok": True, "count": count, "latency_ms": latency}
    except Exception as e:
        latency = int((time.perf_counter() - started) * 1000)
        _record_source(key, False, error=e, latency_ms=latency)
        return {"key": key, "ok": False, "error": str(e)[:200], "latency_ms": latency}

def _mark_app_activity():
    global _LAST_ACTIVITY
    with _ACTIVITY_LOCK:
        _LAST_ACTIVITY = time.monotonic()

def _inactive_seconds():
    with _ACTIVITY_LOCK:
        return max(0.0, time.monotonic() - _LAST_ACTIVITY)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_image_file(self, path, ctype=None, cache_control="public, max-age=31536000, immutable"):
        with open(path, "rb") as f:
            raw = f.read()
        ctype = ctype or _image_content_type(raw) or "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _get_racing_api(self, path, q):
        if path == "/api/f1_schedule":
            return self._send(200, {"events": get_f1_schedule()})
        if path == "/api/racing":
            cfg = load_config()
            selected = [key for key in cfg.get("racing_series", ["f1"])
                        if key in ("f1", "f2", "f3", "indycar", "wec", "formulae", "motogp", "wrc")]
            return self._send(200, {"selected": selected, "events": get_racing_events(selected)})
        if path == "/api/racing_availability":
            cfg = load_config(); x = Xtream(cfg)
            if not x.configured():
                return self._send(200, {"availability": {}, "logged_in": False})
            selected = [key for key in cfg.get("racing_series", ["f1"])
                        if key in _RACING_CHANNEL_TERMS]
            cache_key = _vod_cache_key(x) + "|" + ",".join(selected)
            cached = _RACING_AVAILABILITY_CACHE
            if (cached.get("key") == cache_key and
                    time.time() - float(cached.get("ts") or 0) < _RACING_AVAILABILITY_TTL):
                return self._send(200, {"availability": cached.get("availability") or {},
                                        "logged_in": True})
            events = get_racing_events(selected)
            try:
                channels, cats = get_xtream_channels(cfg)
            except Exception:
                return self._send(200, {"availability": {}, "logged_in": True})
            now = time.time(); availability = {}
            for event in events:
                try:
                    ets = datetime.datetime.fromisoformat(str(event.get("start") or "").replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if ets < now - 12 * 3600 or ets > now + 45 * 24 * 3600:
                    continue
                hits = find_racing_channels(event, channels, cats, x)
                if hits:
                    availability[_racing_event_key(event)] = hits
            _RACING_AVAILABILITY_CACHE.update({"key": cache_key, "ts": time.time(),
                                               "availability": availability})
            return self._send(200, {"availability": availability, "logged_in": True})
        if path == "/api/racing_drivers":
            return self._send(200, {"drivers": get_racing_drivers()})
        if path == "/api/racing_driver_image":
            key = (q.get("id", [""])[0]).strip()
            if not re.fullmatch(r"[0-9A-Za-z_-]+", key):
                return self._send(400, {"error": "bad driver id"})
            driver = next((row for row in get_racing_drivers() if row.get("key") == key), None)
            image_path = _cache_racing_driver_picture(driver or {}) if driver else ""
            if not image_path:
                return self._send(404, {"error": "driver image not found"})
            return self._send_image_file(image_path)
        if path == "/api/f1_teams":
            return self._send(200, {"teams": get_f1_teams(),
                                    "favorites": load_favorites().get("f1_teams", [])})
        if path == "/api/f1_team_logo":
            constructor_id = (q.get("id", [""])[0]).strip()
            if not re.fullmatch(r"[0-9A-Za-z_-]+", constructor_id):
                return self._send(400, {"error": "bad constructor id"})
            image_path = _cache_f1_logo(constructor_id)
            if not image_path:
                return self._send(404, {"error": "F1 team logo not found"})
            return self._send_image_file(image_path)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in {"/api/f1_schedule", "/api/racing", "/api/racing_availability",
                           "/api/racing_drivers", "/api/racing_driver_image",
                           "/api/f1_teams", "/api/f1_team_logo"}:
                return self._get_racing_api(u.path, q)

            if u.path == "/api/team_logo":
                team_id = (q.get("id", [""])[0]).strip()
                if not team_id.isdigit():
                    return self._send(400, {"error": "bad team id"})
                if not _cache_team_logo(team_id):
                    return self._send(404, {"error": "team logo not found"})
                return self._send_image_file(_team_logo_path(team_id), "image/png")

            if u.path == "/api/league_logo":
                league_id = (q.get("id", [""])[0]).strip()
                if not league_id.isdigit():
                    return self._send(400, {"error": "bad league id"})
                if not _cache_league_logo(league_id):
                    return self._send(404, {"error": "league logo not found"})
                return self._send_image_file(_league_logo_path(league_id), "image/png")

            if u.path == "/api/channel_logo":
                stream_id = (q.get("id", [""])[0]).strip()
                if not re.fullmatch(r"[0-9A-Za-z_-]+", stream_id):
                    return self._send(400, {"error": "bad stream id"})
                icon_url = _stream_icon_for_id(stream_id)
                provider = _vod_cache_key(Xtream(load_config()))
                path = _cache_channel_logo(stream_id, icon_url, provider)
                if not path:
                    return self._send(404, {"error": "channel logo not found"})
                with open(path, "rb") as f:
                    raw = f.read()
                ctype = _image_content_type(raw)
                if not ctype:
                    return self._send(404, {"error": "invalid channel logo"})
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if u.path == "/api/steam_avatar":
                steam_id = (q.get("id", [""])[0]).strip()
                if not re.fullmatch(r"\d{17}", steam_id):
                    return self._send(400, {"error": "bad steam id"})
                path = _steam_avatar_path(steam_id)
                if not path or not os.path.isfile(path):
                    return self._send(404, {"error": "avatar not cached"})
                with open(path, "rb") as f:
                    raw = f.read(2 * 1024 * 1024 + 1)
                ctype = _image_content_type(raw)
                if not ctype or len(raw) > 2 * 1024 * 1024:
                    return self._send(404, {"error": "invalid avatar"})
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if u.path == "/api/season_art":
                show = (q.get("show", [""])[0]).strip().lower()
                season = (q.get("season", [""])[0]).strip()
                if not re.fullmatch(r"[a-f0-9]{16}", show) or not re.fullmatch(r"\d+", season):
                    return self._send(400, {"error": "bad artwork path"})
                path = os.path.join(app_dir(), "artwork", "tvmaze-" + show,
                                    "season-" + season + ".jpg")
                if not os.path.isfile(path):
                    return self._send(404, {"error": "artwork not found"})
                return self._send_image_file(path, "image/jpeg")

            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE.replace("__VERSION__", VERSION), "text/html")

            if u.path == "/api/status":
                cfg = load_config()
                x = Xtream(cfg)
                count = len(_XT_CACHE["channels"]) if (x.configured() and _XT_CACHE["channels"]) else None
                return self._send(200, {"configured": x.configured(), "channel_count": count,
                                        "match_threshold": cfg.get("match_threshold", 0.62)})

            if u.path == "/api/source_health":
                return self._send(200, {"sources": source_health_snapshot(),
                                        "now": time.time()})

            if u.path == "/api/favorites":
                fav = load_favorites()
                # enrich channel favorites with a fresh stream URL
                cfg = load_config()
                x = Xtream(cfg)
                chans = []
                for c in fav.get("channels", []):
                    sid = c.get("stream_id")
                    chans.append({
                        "stream_id": sid,
                        "name": c.get("name", ""),
                        "category": c.get("category", ""),
                        "logo": c.get("logo") or _stream_icon_for_id(sid),
                        "url": x.stream_url(sid) if (x.configured() and sid is not None) else "",
                    })
                favorite_shows = []
                for show in fav.get("shows", []):
                    item = dict(show)
                    item["name"] = _clean_show_title(item.get("name")) or item.get("name", "")
                    item["show_key"] = item.get("show_key") or _show_key(item.get("name")) or str(item.get("series_id", ""))
                    item["series_ids"] = [sid for sid in (item.get("series_ids") or [item.get("series_id")]) if sid is not None]
                    favorite_shows.append(item)
                selected_ids = [str(sid) for sid in fav.get("mylist_channels", [])][:5]
                available_ids = {str(c.get("stream_id")) for c in fav.get("channels", [])}
                selected_ids = [sid for sid in selected_ids if sid in available_ids]
                return self._send(200, {"categories": fav.get("categories", []),
                                        "channels": chans,
                                        "movies": fav.get("movies", []),
                                        "shows": favorite_shows,
                                        "games": fav.get("games", []),
                                        "teams": fav.get("teams", []),
                                        "f1_teams": fav.get("f1_teams", []),
                                        "mylist_channels": selected_ids})

            if u.path == "/api/epg_targets":
                cfg = load_config()
                x = Xtream(cfg)
                if not x.configured():
                    return self._send(400, {"error": "Xtream is not configured"})
                fav = load_favorites()
                wanted_categories = set(str(name) for name in fav.get("categories", []))
                ids = [str(ch.get("stream_id")) for ch in fav.get("channels", [])
                       if ch.get("stream_id") is not None]
                if wanted_categories:
                    try:
                        channels, cats = get_xtream_channels(cfg)
                        ids.extend(str(ch.get("stream_id")) for ch in channels
                                   if ch.get("stream_id") is not None and
                                   cats.get(ch.get("category_id"), "") in wanted_categories)
                    except Exception:
                        pass
                return self._send(200, {"ids": list(dict.fromkeys(ids))})

            if u.path == "/api/epg":
                # ids=comma-separated stream ids; force=1 bypasses cache.
                # cached=1 is disk/memory only and never contacts the provider.
                ids_raw = (q.get("ids", [""])[0]).strip()
                force = q.get("force", ["0"])[0] == "1"
                cached_only = q.get("cached", ["0"])[0] == "1"
                all_favorites = q.get("favorites", ["0"])[0] == "1"
                cfg = load_config()
                x = Xtream(cfg)
                if not x.configured() or (not ids_raw and not all_favorites):
                    return self._send(400, {"error": "bad request"})
                _load_epg_disk_cache(x)
                ids = [s.strip() for s in ids_raw.split(",") if s.strip()]
                if all_favorites:
                    fav = load_favorites()
                    wanted_categories = set(str(name) for name in fav.get("categories", []))
                    ids.extend(str(ch.get("stream_id")) for ch in fav.get("channels", [])
                               if ch.get("stream_id") is not None)
                    if wanted_categories:
                        try:
                            channels, cats = get_xtream_channels(cfg)
                            ids.extend(str(ch.get("stream_id")) for ch in channels
                                       if ch.get("stream_id") is not None and
                                       cats.get(ch.get("category_id"), "") in wanted_categories)
                        except Exception:
                            pass
                # Stable de-duplication matters when a channel is directly
                # favorited and also belongs to a favorite category.
                ids = list(dict.fromkeys(ids))
                now = time.time()
                result = {}
                to_fetch = []
                stats = {"updated": 0, "xmltv_filled": 0, "fallback_updated": 0,
                         "no_data": 0, "failed": 0}
                cache_changed = False
                for sid in ids:
                    cached = _EPG_CACHE.get(sid)
                    if cached and cached_only:
                        # Cached-only navigation never contacts the provider. Retained
                        # guide data remains useful as an offline/stale fallback.
                        result[sid] = cached["programmes"]
                    elif (cached and not force and (now - cached["ts"] < _EPG_REFRESH_TTL)
                          and (not cached.get("programmes") or _epg_cache_has_coverage(cached, now))):
                        result[sid] = cached["programmes"]
                    elif not cached_only:
                        to_fetch.append(sid)
                # PRIMARY SOURCE: one bulk XMLTV download covers every channel at
                # once. Map each wanted stream_id to its epg_channel_id, fetch the
                # whole guide, and fill results. Anything the XMLTV lacks falls
                # through to the per-channel API below.
                if to_fetch and not cached_only:
                    try:
                        channels, _cats = get_xtream_channels(cfg)
                        sid_to_epg = {}
                        for ch in channels:
                            csid = str(ch.get("stream_id"))
                            eid = str(ch.get("epg_channel_id") or "").strip()
                            if csid and eid:
                                sid_to_epg[csid] = eid
                        wanted_epg = {sid_to_epg[s] for s in to_fetch if s in sid_to_epg}
                        if wanted_epg:
                            epg_by_channel = fetch_xmltv_epg(x, wanted_epg)
                            _record_source("epg_xmltv", True, count=len(epg_by_channel))
                            filled = []
                            for sid in to_fetch:
                                eid = sid_to_epg.get(sid)
                                if eid and eid in epg_by_channel:
                                    progs = epg_by_channel[eid]
                                    _EPG_CACHE[sid] = {"ts": now, "programmes": progs}
                                    cache_changed = True
                                    result[sid] = progs
                                    stats["updated"] += 1
                                    filled.append(sid)
                            # Only channels NOT covered by XMLTV need the slow path.
                            to_fetch = [s for s in to_fetch if s not in filled]
                            stats["xmltv_filled"] = len(filled)
                    except Exception as e:
                        # XMLTV unavailable (offline, blocked, parse error): fall
                        # back entirely to the per-channel API below.
                        _record_source("epg_xmltv", False, error=e)
                        stats["xmltv_error"] = str(e)[:120]
                # FALLBACK: Xtream's short EPG endpoint is one request per channel.
                # Only used for channels the bulk XMLTV did not cover.
                # Strictly one at a time with a small pause every four channels;
                # several providers reject or silently throttle overlapping requests.
                if to_fetch:
                    stats["safe_mode"] = True
                    for i, sid in enumerate(to_fetch):
                        try:
                            # Request a multi-day listing window. Providers may cap this,
                            # but retaining everything they return makes guide refreshes
                            # useful for days rather than just the current evening.
                            progs = x.short_epg(sid, _EPG_LISTING_LIMIT)
                        except Exception:
                            progs = None
                        old = _EPG_CACHE.get(sid)
                        if progs is None:
                            stats["failed"] += 1
                            if old and old.get("programmes"):
                                result[sid] = old["programmes"]
                        elif not progs:
                            stats["no_data"] += 1
                            if old and old.get("programmes"):
                                result[sid] = old["programmes"]
                            else:
                                _EPG_CACHE[sid] = {"ts": now, "programmes": []}
                                cache_changed = True
                                result[sid] = []
                        else:
                            stats["updated"] += 1
                            stats["fallback_updated"] += 1
                            _EPG_CACHE[sid] = {"ts": now, "programmes": progs}
                            cache_changed = True
                            result[sid] = progs
                        if (i + 1) % 4 == 0:
                            time.sleep(0.35)
                if cache_changed:
                    _save_epg_disk_cache(x)
                return self._send(200, {"epg": result, "total": len(ids), "stats": stats})

            if u.path == "/api/epg_debug":
                # Returns the RAW provider response for one stream, for troubleshooting.
                sid = (q.get("id", [""])[0]).strip()
                cfg = load_config()
                x = Xtream(cfg)
                if not (x.configured() and sid):
                    return self._send(400, {"error": "bad request"})
                dbg_q = {"username": x.user, "password": x.password,
                         "action": "get_short_epg", "stream_id": str(sid), "limit": "3"}
                dbg_url = f"{x.base}/player_api.php?" + urllib.parse.urlencode(dbg_q)
                try:
                    raw = http_get_json(dbg_url)
                except Exception as e:
                    return self._send(200, {"error": str(e), "url": dbg_url})
                return self._send(200, {"raw": raw, "parsed": x.short_epg(sid, limit=3)})

            if u.path == "/api/steam_profile":
                cfg = load_config()
                steam_id = str(cfg.get("steam_wishlist_id") or "").strip()
                if not re.fullmatch(r"\d{17}", steam_id):
                    return self._send(200, {"linked": False})
                try:
                    profile = steam_public_profile(steam_id)
                    profile["linked"] = True
                    return self._send(200, profile)
                except Exception as e:
                    return self._send(200, {"linked": True, "error": str(e)})

            if u.path == "/api/update_check":
                available, remote = check_for_update()
                return self._send(200, {"available": available,
                                        "current": VERSION, "latest": remote})

            if u.path == "/api/hls":
                # Build the HLS url for a stream_id and return it (for direct-try).
                sid = (q.get("id", [""])[0]).strip()
                cfg = load_config()
                x = Xtream(cfg)
                if not (x.configured() and sid):
                    return self._send(400, {"error": "bad request"})
                return self._send(200, {"hls": x.hls_url(sid), "ts": x.stream_url(sid)})

            if u.path == "/api/ping":
                # Cheap local identity check used to prevent duplicate app
                # instances regardless of what the launcher .exe is named.
                return self._send(200, {"app": "olos-tvmate", "version": VERSION})

            if u.path == "/api/proxy":
                # Lightweight media relay for browser playback.  This never
                # transcodes: playlists are rewritten and media bytes are streamed
                # through unchanged so HLS and MPEG-TS can work around CORS.
                target = q.get("u", [""])[0]
                if not target:
                    return self._send(400, {"error": "no url"})
                parsed_target = urllib.parse.urlsplit(target)
                if parsed_target.scheme not in ("http", "https"):
                    return self._send(400, {"error": "unsupported url"})
                try:
                    headers = {
                        "User-Agent": "VLC/3.0 LibVLC/3.0",
                        "Accept": "*/*"}
                    range_header = self.headers.get("Range")
                    if range_header:
                        headers["Range"] = range_header
                    req = urllib.request.Request(target, headers=headers)
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        ctype = resp.headers.get("Content-Type", "application/octet-stream")
                        path_low = parsed_target.path.lower()
                        is_playlist = ("mpegurl" in ctype.lower() or
                                       path_low.endswith(".m3u8"))
                        if is_playlist:
                            # Playlists are tiny.  Rewrite segments plus URI="..."
                            # attributes used by encryption keys and init maps.
                            raw = resp.read(4 * 1024 * 1024 + 1)
                            if len(raw) > 4 * 1024 * 1024:
                                raise ValueError("playlist too large")
                            text = raw.decode("utf-8", "replace")
                            out_lines = []

                            def proxy_url(child):
                                absolute = urllib.parse.urljoin(target, child)
                                return "/api/proxy?u=" + urllib.parse.quote(absolute, safe="")

                            for line in text.splitlines():
                                s = line.strip()
                                if s and not s.startswith("#"):
                                    out_lines.append(proxy_url(s))
                                elif s.startswith("#") and 'URI="' in line:
                                    line = re.sub(
                                        r'URI="([^\"]+)"',
                                        lambda m: 'URI="' + proxy_url(m.group(1)) + '"',
                                        line)
                                    out_lines.append(line)
                                else:
                                    out_lines.append(line)
                            raw = ("\n".join(out_lines) + "\n").encode("utf-8")
                            self.send_response(200)
                            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Content-Length", str(len(raw)))
                            self.end_headers()
                            self.wfile.write(raw)
                            return

                        # Media is streamed as it arrives instead of resp.read().
                        # That is essential for live TS and avoids buffering a
                        # whole video locally.  Bytes are never decoded/re-encoded.
                        status = getattr(resp, "status", 200) or 200
                        self.send_response(status)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Cache-Control", "no-cache")
                        for hn in ("Content-Length", "Content-Range", "Accept-Ranges"):
                            hv = resp.headers.get(hn)
                            if hv:
                                self.send_header(hn, hv)
                        self.end_headers()
                        try:
                            while True:
                                chunk = resp.read(64 * 1024)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        return
                except Exception as e:
                    return self._send(502, {"error": str(e)})

            if u.path == "/api/config":
                return self._send(200, load_config())

            if u.path == "/api/artwork_cache":
                return self._send(200, {"bytes": artwork_cache_size()})

            if u.path == "/api/test":
                ok, info = Xtream(load_config()).login()
                return self._send(200, {"ok": ok, "info": info if ok else None,
                                        "error": None if ok else info})

            if u.path == "/api/reload":
                cfg = load_config()
                if not Xtream(cfg).configured():
                    return self._send(200, {"ok": False, "error": "Not configured"})
                try:
                    ch, _ = get_xtream_channels(cfg, force=True)
                    return self._send(200, {"ok": True, "count": len(ch)})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})

            if u.path == "/api/categories":
                cfg = load_config()
                x = Xtream(cfg)
                if not x.configured():
                    return self._send(200, {"categories": [], "logged_in": False})
                try:
                    channels, cats = get_xtream_channels(cfg)
                except Exception as e:
                    return self._send(200, {"categories": [], "logged_in": True,
                                            "error": str(e)})
                # count channels per category (only categories that have channels)
                counts = {}
                for ch in channels:
                    cn = cats.get(ch["category_id"], "")
                    if cn:
                        counts[cn] = counts.get(cn, 0) + 1
                out = [{"name": k, "count": v} for k, v in
                       sorted(counts.items(), key=lambda kv: kv[0].lower())]
                return self._send(200, {"categories": out, "logged_in": True})

            if u.path == "/api/channels":
                # Substring channel search + optional category filter.
                term = (q.get("q", [""])[0]).strip().lower()
                cat_filter = (q.get("cat", [""])[0]).strip()
                cfg = load_config()
                x = Xtream(cfg)
                if not x.configured():
                    return self._send(200, {"channels": [], "logged_in": False,
                                            "total": 0})
                try:
                    channels, cats = get_xtream_channels(cfg)
                except Exception as e:
                    return self._send(200, {"channels": [], "logged_in": True,
                                            "error": str(e), "total": 0})
                words = [w for w in term.split() if w]
                out = []
                for ch in channels:
                    nm = ch["name"]
                    low = nm.lower()
                    catname = cats.get(ch["category_id"], "")
                    if cat_filter and catname != cat_filter:
                        continue
                    if words and not all(w in low for w in words):
                        continue
                    out.append({
                        "name": nm,
                        "stream_id": ch["stream_id"],
                        "category": catname,
                        "logo": ch.get("stream_icon", ""),
                        "quality": quality_tag(nm),
                        "url": x.stream_url(ch["stream_id"]),
                    })
                total = len(out)
                capped = out[:500]
                return self._send(200, {"channels": capped, "logged_in": True,
                                        "total": total, "shown": len(capped)})

            if u.path == "/api/movie_catalog":
                catalog_name = (q.get("catalog", ["popular"])[0]).strip().lower()
                try:
                    limit = max(1, min(30, int(q.get("limit", ["10"])[0])))
                except (TypeError, ValueError):
                    limit = 10
                cfg = load_config()
                x = Xtream(cfg)
                try:
                    catalog = cinemeta_movie_catalog(catalog_name, limit)
                except Exception as e:
                    return self._send(200, {"movies": [], "logged_in": x.configured(),
                                            "error": "Movie catalog: " + str(e)})
                provider_movies = []
                if x.configured():
                    try:
                        provider_movies = get_xtream_movies(cfg)
                    except Exception:
                        provider_movies = []
                out = []
                for meta in catalog:
                    name = str(meta.get("name") or "").strip()
                    if not name:
                        continue
                    year = _catalog_year(meta)
                    sources = match_vod_sources({"name": name, "year": year}, provider_movies)
                    first = sources[0] if sources else {}
                    out.append({"catalog_id": meta.get("id") or meta.get("imdb_id") or "",
                                "stream_id": first.get("stream_id"), "name": name,
                                "extension": first.get("extension") or "mp4", "year": year,
                                "rating": meta.get("imdbRating") or "",
                                "cover": meta.get("poster") or "", "sources": sources,
                                "stream_found": bool(sources)})
                return self._send(200, {"movies": out, "logged_in": x.configured(),
                                        "catalog": catalog_name})

            if u.path == "/api/movies":
                term = (q.get("q", [""])[0]).strip()
                cfg = load_config()
                x = Xtream(cfg)
                if not term:
                    return self._send(200, {"movies": [], "logged_in": x.configured()})
                try:
                    catalog = cinemeta_search("movie", term)
                except Exception as e:
                    return self._send(200, {"movies": [], "logged_in": x.configured(),
                                            "error": "Movie catalog: " + str(e)})
                provider_movies = []
                if x.configured():
                    try:
                        provider_movies = get_xtream_movies(cfg)
                    except Exception:
                        provider_movies = []
                out = []
                for meta in catalog:
                    name = str(meta.get("name") or "").strip()
                    if not name:
                        continue
                    year = _catalog_year(meta)
                    sources = match_vod_sources({"name": name, "year": year}, provider_movies)
                    first = sources[0] if sources else {}
                    out.append({
                        "catalog_id": meta.get("id") or "",
                        "stream_id": first.get("stream_id"),
                        "name": name,
                        "extension": first.get("extension") or "mp4",
                        "year": year,
                        "rating": meta.get("imdbRating") or "",
                        "cover": meta.get("poster") or "",
                        "sources": sources,
                        "stream_found": bool(sources),
                    })
                return self._send(200, {"movies": out, "logged_in": x.configured()})

            if u.path == "/api/favorite_movie_status":
                cfg = load_config()
                x = Xtream(cfg)
                try:
                    provider_movies = get_xtream_movies(cfg) if x.configured() else []
                except Exception:
                    provider_movies = []
                out = []
                for movie in load_favorites().get("movies", []):
                    row = dict(movie)
                    sources = match_vod_sources(row, provider_movies)
                    row["sources"] = sources
                    row["stream_found"] = bool(sources)
                    if sources:
                        row["stream_id"] = sources[0].get("stream_id")
                        row["extension"] = sources[0].get("extension") or "mp4"
                    out.append(row)
                return self._send(200, {"movies": out, "logged_in": x.configured()})

            if u.path == "/api/recent_movies":
                try:
                    limit = max(1, min(36, int(q.get("limit", ["9"])[0])))
                except (TypeError, ValueError):
                    limit = 9
                cfg = load_config()
                x = Xtream(cfg)
                if not x.configured():
                    return self._send(200, {"movies": [], "logged_in": False})
                try:
                    movies = get_xtream_movies(cfg)
                except Exception as e:
                    return self._send(200, {"movies": [], "logged_in": True,
                                            "error": str(e)})
                this_year = time.localtime().tm_year
                by_year = {}
                all_rows = []
                for m in movies:
                    raw_year = " ".join(str(value or "") for value in
                                        (m.get("year"), m.get("releaseDate"),
                                         m.get("release_date"), m.get("name")))
                    match = re.search(r"(?:19|20)\d{2}", raw_year)
                    year = int(match.group(0)) if match else 0
                    try:
                        added = int(float(m.get("added") or 0))
                    except (TypeError, ValueError):
                        added = 0
                    row = (added, m, year)
                    all_rows.append(row)
                    if year:
                        by_year.setdefault(year, []).append(row)
                for rows in by_year.values():
                    rows.sort(key=lambda item: item[0], reverse=True)
                usable_years = [year for year in by_year if year <= this_year + 1]
                target_year = this_year if this_year in by_year else (
                    max(usable_years) if usable_years else 0)
                candidate_rows = list(by_year.get(target_year, []))
                candidate_rows += by_year.get(target_year - 1, [])
                if not candidate_rows:
                    all_rows.sort(key=lambda item: item[0], reverse=True)
                    candidate_rows = all_rows
                unique_rows = []
                seen_titles = set()
                for row in candidate_rows:
                    clean_title = _clean_show_title(row[1].get("name")) or str(
                        row[1].get("name") or "")
                    title_key = re.sub(r"[^a-z0-9]+", "", clean_title.lower())
                    if not title_key or title_key in seen_titles:
                        continue
                    seen_titles.add(title_key)
                    unique_rows.append(row)
                chosen = unique_rows[:limit]
                out = []
                for _added, m, year in chosen:
                    cover = str(m.get("stream_icon") or m.get("cover") or
                                m.get("movie_image") or "").strip()
                    if not cover.startswith(("http://", "https://")):
                        cover = ""
                    out.append({"stream_id": m.get("stream_id"),
                                "name": str(m.get("name") or ""),
                                "extension": m.get("container_extension") or "mp4",
                                "year": year, "rating": m.get("rating") or "",
                                "cover": cover})
                return self._send(200, {"movies": out, "logged_in": True,
                                        "catalog_year": target_year,
                                        "has_more": len(unique_rows) > limit})

            if u.path == "/api/shows":
                term = (q.get("q", [""])[0]).strip()
                cfg = load_config()
                x = Xtream(cfg)
                if not term:
                    return self._send(200, {"shows": [], "logged_in": x.configured()})
                try:
                    catalog = cinemeta_search("series", term)
                except Exception as e:
                    return self._send(200, {"shows": [], "logged_in": x.configured(),
                                            "error": "Show catalog: " + str(e)})
                provider = []
                if x.configured():
                    try:
                        provider = get_xtream_series(cfg)
                    except Exception:
                        provider = []
                out = []
                for meta in catalog:
                    name = str(meta.get("name") or "").strip()
                    if not name:
                        continue
                    key = _show_key(name)
                    siblings = [row for row in provider if _show_key(row.get("name")) == key]
                    ids = [row.get("series_id") for row in siblings if row.get("series_id") is not None]
                    year = _catalog_year(meta)
                    out.append({"catalog_id": meta.get("id") or "", "show_key": key,
                                "series_id": ids[0] if ids else None, "series_ids": ids,
                                "provider_found": bool(ids), "name": name,
                                "cover": meta.get("poster") or "", "year": year,
                                "rating": meta.get("imdbRating") or ""})
                return self._send(200, {"shows": out, "logged_in": x.configured()})

            if u.path == "/api/latest_episodes":
                try:
                    limit = max(1, min(36, int(q.get("limit", ["9"])[0])))
                except (TypeError, ValueError):
                    limit = 9
                refresh_external = q.get("refresh", ["0"])[0] == "1"
                cfg = load_config()
                x = Xtream(cfg)
                if not refresh_external:
                    cached = _load_latest_episodes_cache(x)
                    if cached is not None:
                        cached_rows = cached.get("episodes") or []
                        return self._send(200, {
                            "episodes": cached_rows[:limit], "logged_in": x.configured(),
                            "has_more": len(cached_rows) > limit,
                            "upcoming": (cached.get("upcoming") or [])[:36],
                            "errors": int(cached.get("errors") or 0),
                            "cached": True})
                rows = []
                upcoming_rows = []
                errors = 0
                if x.configured():
                    try:
                        series_catalog = get_xtream_series(cfg)
                    except Exception:
                        series_catalog = []
                else:
                    series_catalog = []
                for fav_show in load_favorites().get("shows", []):
                    series_id = fav_show.get("series_id")
                    favorite_key = str(fav_show.get("show_key") or _show_key(fav_show.get("name")))
                    siblings = [row.get("series_id") for row in series_catalog
                                if _show_key(row.get("name")) == favorite_key]
                    siblings = [sid for sid in siblings if sid is not None]
                    if siblings:
                        series_id = siblings[0]
                    if series_id is None:
                        try:
                            show_name = str(fav_show.get("name") or "Show")
                            display_show_name = _clean_show_title(show_name) or show_name
                            year_match = re.search(r"(?:19|20)\d{2}", str(fav_show.get("year") or ""))
                            show_year = int(year_match.group(0)) if year_match else 0
                            schedule = _tvmaze_episode_schedule(show_name, show_year,
                                                                 force=refresh_external)
                            cover = str(fav_show.get("cover") or "")
                            external = schedule.get("latest") or {}
                            upcoming = schedule.get("upcoming") or {}
                            if upcoming:
                                try:
                                    uts = (datetime.datetime.fromisoformat(upcoming["airstamp"].replace("Z", "+00:00")).timestamp()
                                           if upcoming.get("airstamp") else time.mktime(time.strptime(upcoming.get("airdate") or "", "%Y-%m-%d")))
                                except (ValueError, OverflowError, TypeError):
                                    uts = 0
                                upcoming_rows.append({"show_name": display_show_name,
                                    "series_id": "", "catalog_id": fav_show.get("catalog_id") or "",
                                    "cover": cover, "season": int(upcoming.get("season") or 0),
                                    "episode_num": int(upcoming.get("episode_num") or 0),
                                    "title": upcoming.get("title") or "Episode",
                                    "airdate": upcoming.get("airdate") or "",
                                    "airstamp": upcoming.get("airstamp") or "", "air_ts": uts})
                            if external:
                                try:
                                    ets = (datetime.datetime.fromisoformat(external["airstamp"].replace("Z", "+00:00")).timestamp()
                                           if external.get("airstamp") else time.mktime(time.strptime(external.get("airdate") or "", "%Y-%m-%d")))
                                except (ValueError, OverflowError, TypeError):
                                    ets = 0
                                if ets >= time.time() - (30 * 24 * 3600):
                                    rows.append({"id": None, "show_name": display_show_name,
                                        "series_id": "", "catalog_id": fav_show.get("catalog_id") or "",
                                        "cover": cover, "season": int(external.get("season") or 0),
                                        "episode_num": int(external.get("episode_num") or 0),
                                        "title": external.get("title") or "Episode", "extension": "",
                                        "added": ets, "air_ts": ets, "available": False})
                        except Exception:
                            errors += 1
                        continue
                    try:
                        data = x.series_info(series_id) or {}
                        info = data.get("info") or {}
                        if not isinstance(info, dict):
                            info = {}
                        show_name = str(info.get("name") or info.get("title") or
                                        fav_show.get("name") or "Show")
                        display_show_name = _clean_show_title(show_name) or show_name
                        year_text = " ".join(str(value or "") for value in
                                             (fav_show.get("year"), info.get("releaseDate"),
                                              info.get("release_date"), show_name))
                        year_match = re.search(r"(?:19|20)\d{2}", year_text)
                        show_year = int(year_match.group(0)) if year_match else 0
                        raw_episodes = data.get("episodes") or {}
                        if isinstance(raw_episodes, list):
                            grouped = {}
                            for ep in raw_episodes:
                                grouped.setdefault(str(ep.get("season") or 1), []).append(ep)
                            raw_episodes = grouped
                        candidates = []
                        for season_key, episodes in raw_episodes.items():
                            if not isinstance(episodes, list):
                                continue
                            try:
                                season_num = int(season_key)
                            except (TypeError, ValueError):
                                season_num = 0
                            for index, episode in enumerate(episodes, 1):
                                try:
                                    episode_num = int(episode.get("episode_num") or index)
                                except (TypeError, ValueError):
                                    episode_num = index
                                candidates.append((season_num, episode_num, episode))
                        cover = str(fav_show.get("cover") or info.get("cover") or
                                    info.get("movie_image") or "").strip()
                        if not cover.startswith(("http://", "https://")):
                            cover = ""
                        provider_row = None
                        provider_key = (-1, -1)
                        if candidates:
                            season_num, episode_num, episode = max(
                                candidates, key=lambda item: (item[0], item[1]))
                            provider_key = (season_num, episode_num)
                            try:
                                added = int(float(episode.get("added") or 0))
                            except (TypeError, ValueError):
                                added = 0
                            episode_info = episode.get("info") or {}
                            if not isinstance(episode_info, dict):
                                episode_info = {}
                            episode_date = " ".join(str(value or "") for value in
                                                    (episode_info.get("releaseDate"),
                                                     episode_info.get("releasedate"),
                                                     episode_info.get("air_date"),
                                                     episode.get("releaseDate")))
                            episode_ts = 0
                            date_match = re.search(r"(?:19|20)\d{2}-\d{2}-\d{2}",
                                                   episode_date)
                            if date_match:
                                try:
                                    episode_ts = time.mktime(time.strptime(
                                        date_match.group(0), "%Y-%m-%d"))
                                except (ValueError, OverflowError):
                                    episode_ts = 0
                            if not episode_ts and added > 100000000:
                                episode_ts = added
                            if episode.get("id") is not None:
                                provider_row = {
                                    "id": episode.get("id"), "show_name": show_name,
                                    "series_id": series_id, "cover": cover,
                                    "season": season_num, "episode_num": episode_num,
                                    "title": _clean_episode_title(
                                        episode.get("title") or f"Episode {episode_num}",
                                        show_name),
                                    "extension": episode.get("container_extension") or "mp4",
                                    "added": episode_ts, "available": True}
                        # Merge all quality/provider variants into this one latest card.
                        series_ids = fav_show.get("series_ids") or [series_id]
                        if siblings:
                            series_ids = siblings
                        variant_rows = []
                        for variant_id in series_ids:
                            try:
                                variant_row, _variant_info = _latest_provider_variant(
                                    x, variant_id, show_name)
                                if variant_row and variant_row.get("id") is not None:
                                    variant_rows.append(variant_row)
                            except Exception:
                                continue
                        if variant_rows:
                            provider_key = max(row["key"] for row in variant_rows)
                            matching = [row for row in variant_rows
                                        if row["key"] == provider_key]
                            first = matching[0]
                            provider_row = {
                                "id": first["id"], "show_name": _clean_show_title(show_name),
                                "series_id": series_id, "cover": cover,
                                "season": first["season"],
                                "episode_num": first["episode_num"],
                                "title": first["title"],
                                "extension": first["extension"],
                                "sources": [{"id": row["id"], "extension": row["extension"],
                                             "label": row["label"]} for row in matching],
                                "added": max(row["added"] for row in matching),
                                "available": True}
                        schedule = _tvmaze_episode_schedule(
                            show_name, show_year, force=refresh_external)
                        external = schedule.get("latest") or {}
                        upcoming = schedule.get("upcoming") or {}
                        if upcoming:
                            upcoming_ts = 0
                            try:
                                if upcoming.get("airstamp"):
                                    upcoming_ts = datetime.datetime.fromisoformat(
                                        upcoming["airstamp"].replace("Z", "+00:00")).timestamp()
                                else:
                                    upcoming_ts = time.mktime(time.strptime(
                                        upcoming.get("airdate") or "", "%Y-%m-%d"))
                            except (ValueError, OverflowError, TypeError):
                                upcoming_ts = 0
                            upcoming_rows.append({
                                "show_name": display_show_name, "series_id": series_id,
                                "cover": cover,
                                "season": int(upcoming.get("season") or 0),
                                "episode_num": int(upcoming.get("episode_num") or 0),
                                "title": upcoming.get("title") or "Episode",
                                "airdate": upcoming.get("airdate") or "",
                                "airstamp": upcoming.get("airstamp") or "",
                                "air_ts": upcoming_ts})
                        external_key = (int(external.get("season") or -1),
                                        int(external.get("episode_num") or -1))
                        external_ts = 0
                        if external.get("airstamp"):
                            try:
                                external_ts = datetime.datetime.fromisoformat(
                                    external["airstamp"].replace("Z", "+00:00")).timestamp()
                            except (ValueError, OverflowError, TypeError):
                                external_ts = 0
                        elif external.get("airdate"):
                            try:
                                external_ts = time.mktime(time.strptime(
                                    external["airdate"], "%Y-%m-%d"))
                            except (ValueError, OverflowError):
                                external_ts = 0
                        if provider_row and provider_key == external_key and external_ts:
                            provider_row["air_ts"] = external_ts
                            if not provider_row.get("added"):
                                provider_row["added"] = external_ts
                        cutoff = time.time() - (30 * 24 * 60 * 60)
                        if (external and external_key > provider_key and
                                external_ts >= cutoff):
                            rows.append({"id": None, "show_name": display_show_name,
                                         "series_id": series_id, "cover": cover,
                                         "season": external_key[0],
                                         "episode_num": external_key[1],
                                         "title": external.get("title") or "Episode",
                                         "extension": "", "added": external_ts,
                                         "available": False})
                        elif provider_row and provider_row["added"] >= cutoff:
                            rows.append(provider_row)
                    except Exception:
                        errors += 1
                rows.sort(key=lambda item: (item.get("added") or 0,
                                             item.get("season") or 0,
                                             item.get("episode_num") or 0), reverse=True)
                upcoming_rows.sort(key=lambda item: item.get("air_ts") or 0)
                _save_latest_episodes_cache(x, rows, upcoming_rows[:36], errors)
                return self._send(200, {"episodes": rows[:limit], "logged_in": x.configured(),
                                        "has_more": len(rows) > limit,
                                        "upcoming": upcoming_rows[:36],
                                        "errors": errors})

            if u.path == "/api/show":
                series_id_text = (q.get("id", [""])[0]).strip()
                series_ids = [sid.strip() for sid in series_id_text.split(",") if sid.strip()]
                refresh = (q.get("refresh", ["0"])[0]) == "1"
                cfg = load_config()
                x = Xtream(cfg)
                if not (x.configured() and series_ids):
                    return self._send(400, {"error": "bad request"})
                # Upgrade old one-source favorites by discovering sibling variants.
                try:
                    catalog = get_xtream_series(cfg)
                    selected = next((row for row in catalog
                                     if str(row.get("series_id")) in series_ids), None)
                    selected_key = _show_key((selected or {}).get("name"))
                    if selected_key:
                        series_ids = [str(row.get("series_id")) for row in catalog
                                      if _show_key(row.get("name")) == selected_key]
                except Exception:
                    pass
                variants = []
                for series_id in series_ids:
                    try:
                        data = x.series_info(series_id, refresh=refresh) or {}
                    except Exception:
                        continue
                    info = data.get("info") or {}
                    if not isinstance(info, dict):
                        info = {}
                    variants.append((series_id, data, info))
                if not variants:
                    return self._send(200, {"error": "Could not load this show."})
                series_id, data, info = variants[0]
                if not isinstance(info, dict):
                    info = {}
                cover = str(info.get("cover") or info.get("movie_image") or "").strip()
                if not cover.startswith(("http://", "https://")):
                    cover = ""
                raw_show_name = info.get("name") or info.get("title") or "Show"
                show_name = _clean_show_title(raw_show_name) or raw_show_name
                release_text = str(info.get("releaseDate") or info.get("release_date") or show_name)
                year_match = re.search(r"(?:19|20)\d{2}", release_text)
                show_year = year_match.group(0) if year_match else ""
                maze_covers = _tvmaze_season_covers(show_name, show_year)
                xtream_season_covers = {}
                for _variant_id, variant_data, _variant_info in variants:
                    raw_seasons = variant_data.get("seasons") or []
                    if not isinstance(raw_seasons, list):
                        continue
                    for meta in raw_seasons:
                        if not isinstance(meta, dict):
                            continue
                        key = meta.get("season_number")
                        if key is None:
                            key = meta.get("season")
                        if key is None:
                            match = re.search(r"\d+", str(meta.get("name") or ""))
                            key = match.group(0) if match else None
                        art = str(meta.get("cover") or meta.get("cover_big") or
                                  meta.get("movie_image") or "").strip()
                        if key is not None and art.startswith(("http://", "https://")):
                            xtream_season_covers[str(key)] = art
                episode_map = {}
                for _variant_id, variant_data, variant_info in variants:
                    variant_name = variant_info.get("name") or variant_info.get("title") or show_name
                    label = _show_variant_label(variant_name)
                    raw_episodes = variant_data.get("episodes") or {}
                    if isinstance(raw_episodes, list):
                        grouped = {}
                        for ep in raw_episodes:
                            grouped.setdefault(str(ep.get("season") or 1), []).append(ep)
                        raw_episodes = grouped
                    for season_key, eps in raw_episodes.items():
                        if not isinstance(eps, list):
                            continue
                        for i, ep in enumerate(eps, 1):
                            episode_num = ep.get("episode_num") or i
                            key = (str(season_key), str(episode_num))
                            item = episode_map.setdefault(key, {
                                "episode_num": episode_num,
                                "title": _clean_episode_title(
                                    ep.get("title") or f"Episode {i}", show_name),
                                "sources": []})
                            source_label = label
                            used = {src["label"] for src in item["sources"]}
                            if source_label in used:
                                suffix = 2
                                while f"{label} {suffix}" in used:
                                    suffix += 1
                                source_label = f"{label} {suffix}"
                            item["sources"].append({
                                "id": ep.get("id"), "label": source_label,
                                "extension": ep.get("container_extension") or "mp4"})
                seasons = []
                season_numbers = sorted({key[0] for key in episode_map},
                    key=lambda value: int(value) if value.isdigit() else 999999)
                for season_key in season_numbers:
                    normalized = [item for (season, _number), item in episode_map.items()
                                  if season == season_key]
                    normalized.sort(key=lambda ep: int(ep["episode_num"]) if str(ep["episode_num"]).isdigit() else 999999)
                    seasons.append({"number": season_key,
                                    "title": f"Season {season_key}",
                                    "cover": (maze_covers.get(str(season_key)) or
                                              xtream_season_covers.get(str(season_key)) or cover),
                                    "episodes": normalized})
                seasons.sort(key=lambda s: int(s["number"]) if str(s["number"]).isdigit() else 999999)
                return self._send(200, {"name": show_name, "show_key": _show_key(show_name),
                                        "series_id": series_ids[0], "series_ids": series_ids,
                                        "cover": cover, "seasons": seasons})

            if u.path == "/api/show_external":
                catalog_id = (q.get("id", [""])[0]).strip()
                try:
                    meta = cinemeta_meta("series", catalog_id)
                except Exception as e:
                    return self._send(200, {"error": "Could not load show metadata: " + str(e)})
                if not meta:
                    return self._send(200, {"error": "Could not load this show."})
                show_name = str(meta.get("name") or "Show")
                show_key = _show_key(show_name)
                cfg = load_config()
                x = Xtream(cfg)
                provider_ids = []
                if x.configured():
                    try:
                        provider_ids = [row.get("series_id") for row in get_xtream_series(cfg)
                                        if _show_key(row.get("name")) == show_key and
                                        row.get("series_id") is not None]
                    except Exception:
                        provider_ids = []
                if provider_ids:
                    return self._send(200, {"catalog_id": catalog_id,
                                            "provider_series_ids": provider_ids})
                grouped = {}
                for video in meta.get("videos") or []:
                    season = video.get("season")
                    episode = video.get("episode")
                    if season is None or episode is None:
                        continue
                    grouped.setdefault(str(season), []).append({
                        "episode_num": episode, "title": video.get("name") or f"Episode {episode}",
                        "released": video.get("released") or "", "sources": []})
                seasons = []
                for season, episodes in grouped.items():
                    episodes.sort(key=lambda row: int(row.get("episode_num") or 0))
                    seasons.append({"number": season, "title": f"Season {season}",
                                    "cover": meta.get("poster") or "", "episodes": episodes})
                seasons.sort(key=lambda row: int(row["number"]) if str(row["number"]).isdigit() else 999999)
                return self._send(200, {"catalog_id": catalog_id, "name": show_name,
                    "show_key": show_key, "series_id": None, "series_ids": [],
                    "cover": meta.get("poster") or "", "year": _catalog_year(meta),
                    "rating": meta.get("imdbRating") or "", "seasons": seasons})

            if u.path == "/api/team_search":
                term = (q.get("q", [""])[0]).strip()
                if not term:
                    return self._send(200, {"teams": []})
                cfg = load_config()
                countries = cfg.get("countries") or ["no", "uk", "us"]
                fixtures, src_err = search_fixtures(term, countries)
                term_l = term.lower()
                wanted = _expand_terms(term_l)
                found = []
                seen = set()
                try:
                    for team in search_fotmob_teams(term):
                        low = str(team.get("name") or "").lower().strip()
                        if low and low not in seen:
                            seen.add(low)
                            found.append(team)
                except Exception as e:
                    src_err.append(f"FotMob team search: {e}")
                for fixture in fixtures:
                    sides = ((fixture.get("home", ""), fixture.get("home_id", "")),
                             (fixture.get("away", ""), fixture.get("away_id", "")))
                    for name, team_id in sides:
                        low = name.lower().strip()
                        matches = (term_l in low or low in wanted or
                                   any(alias in low or low in alias for alias in wanted
                                       if len(alias) >= 3))
                        if matches and low and low not in seen:
                            seen.add(low)
                            found.append({"name": name, "team_id": team_id})
                return self._send(200, {"teams": found, "source_errors": src_err})

            if u.path == "/api/team_profile":
                team_name = (q.get("name", [""])[0]).strip()
                team_id = (q.get("id", [""])[0]).strip()
                if not team_id and team_name:
                    team_id = resolve_fotmob_team_id(team_name)
                profile = fetch_team_profile(team_id, team_name)
                profile["team_id"] = team_id
                profile["logo"] = _team_logo_url(team_id) if team_id else ""
                return self._send(200, {"profile": profile})

            if u.path == "/api/my_teams":
                cfg = load_config()
                countries = cfg.get("countries") or ["no", "uk", "us"]
                fav_data = load_favorites()
                favorites = fav_data.get("teams", [])
                favorites_changed = False
                merged = {}
                errors = []
                for favorite in favorites:
                    team_name = str(favorite.get("name") if isinstance(favorite, dict)
                                    else favorite).strip()
                    team_id = str(favorite.get("team_id") if isinstance(favorite, dict)
                                  else "").strip()
                    if not team_name:
                        continue
                    tv_fixtures, src_err = search_fixtures(team_name, countries)
                    errors.extend(src_err)
                    if not team_id:
                        wanted = _expand_terms(team_name.lower())
                        for fixture in tv_fixtures:
                            for side_name, side_id in (
                                    (fixture.get("home", ""), fixture.get("home_id", "")),
                                    (fixture.get("away", ""), fixture.get("away_id", ""))):
                                low = side_name.lower().strip()
                                if side_id and (team_name.lower() in low or low in wanted or
                                                any(alias in low or low in alias
                                                    for alias in wanted if len(alias) >= 3)):
                                    team_id = str(side_id)
                                    break
                            if team_id:
                                break
                    if not team_id:
                        team_id = resolve_fotmob_team_id(team_name)
                    if team_id and isinstance(favorite, dict) and not favorite.get("team_id"):
                        favorite["team_id"] = team_id
                        favorites_changed = True
                    if team_id and isinstance(favorite, dict) and not favorite.get("logo"):
                        favorite["logo"] = _team_logo_url(team_id)
                        favorites_changed = True
                    try:
                        fixtures = fetch_team_schedule(team_id, team_name) if team_id else []
                    except Exception as e:
                        errors.append(f"{team_name}: {e}")
                        fixtures = []
                    # Fall back to TV-guide fixtures if the team feed is unavailable.
                    if not fixtures:
                        fixtures = [dict(row, is_live=False, status_known=False)
                                    for row in tv_fixtures]
                    # A week-long schedule cache is fine for future fixtures, but live
                    # state must come from today's short-lived feed on every render.
                    try:
                        daily_team = search_daily_matches(team_name)
                    except Exception as e:
                        errors.append(f"{team_name} live status: {e}")
                        daily_team = []
                    for daily in daily_team:
                        duplicate = None
                        dday = str(daily.get("start") or "")[:10]
                        for fixture in fixtures:
                            if dday and str(fixture.get("start") or "")[:10] != dday:
                                continue
                            home_ok = (normalise(fixture.get("home", "")) == normalise(daily.get("home", "")) or
                                       daily.get("home", "").lower() in _expand_terms(fixture.get("home", "").lower()))
                            away_ok = (normalise(fixture.get("away", "")) == normalise(daily.get("away", "")) or
                                       daily.get("away", "").lower() in _expand_terms(fixture.get("away", "").lower()))
                            if home_ok and away_ok:
                                duplicate = fixture
                                break
                        if duplicate is None:
                            fixtures.append(dict(daily, status_known=True))
                        else:
                            duplicate["is_live"] = bool(daily.get("is_live"))
                            duplicate["is_finished"] = bool(daily.get("is_finished"))
                            duplicate["live_minute"] = daily.get("live_minute")
                            duplicate["home_id"] = duplicate.get("home_id") or daily.get("home_id", "")
                            duplicate["away_id"] = duplicate.get("away_id") or daily.get("away_id", "")
                            duplicate["status_known"] = True
                    # Overlay broadcaster listings without using them as the fixture source.
                    for fixture in fixtures:
                        fday = str(fixture.get("start") or "")[:10]
                        for tvrow in tv_fixtures:
                            if fday and str(tvrow.get("start") or "")[:10] != fday:
                                continue
                            home_ok = (normalise(fixture.get("home", "")) == normalise(tvrow.get("home", "")) or
                                       tvrow.get("home", "").lower() in _expand_terms(fixture.get("home", "").lower()))
                            away_ok = (normalise(fixture.get("away", "")) == normalise(tvrow.get("away", "")) or
                                       tvrow.get("away", "").lower() in _expand_terms(fixture.get("away", "").lower()))
                            if home_ok and away_ok:
                                fixture["by_country"] = tvrow.get("by_country", {})
                                break
                    for fixture in fixtures:
                        key = "|".join((str(fixture.get("home", "")).lower(),
                                        str(fixture.get("away", "")).lower(),
                                        str(fixture.get("start", ""))))
                        row = merged.get(key)
                        if row is None:
                            row = dict(fixture)
                            row["favorite_teams"] = []
                            merged[key] = row
                        elif fixture.get("is_live"):
                            row["is_live"] = True
                        if fixture.get("is_finished"):
                            row["is_finished"] = True
                        if fixture.get("live_minute") is not None:
                            row["live_minute"] = fixture.get("live_minute")
                        if team_name not in row["favorite_teams"]:
                            row["favorite_teams"].append(team_name)
                if favorites_changed:
                    save_favorites(fav_data)
                fixtures = sorted(merged.values(), key=lambda row: row.get("start") or "")
                try:
                    top_fixtures = featured_daily_fixtures()
                    errors.extend(add_tv_listings(top_fixtures, countries))
                except Exception as e:
                    top_fixtures = []
                    errors.append(f"FotMob featured fixtures: {e}")
                # Hydrate durable channel matches before the page renders. The
                # client may refresh stale entries later, but never needs to
                # replace an already-known match with a Checking placeholder.
                x = Xtream(cfg)
                if x.configured():
                    stored_availability = _load_sports_disk_cache(cfg, x)
                    for fixture in fixtures + top_fixtures:
                        stored = stored_availability.get(_sports_event_key(
                            fixture.get("home"), fixture.get("away"),
                            fixture.get("start")))
                        if isinstance(stored, dict) and isinstance(stored.get("result"), dict):
                            fixture.update(_sports_result_for_client(stored["result"], x))
                return self._send(200, {"fixtures": fixtures,
                                        "top_fixtures": top_fixtures,
                                        "source_errors": list(dict.fromkeys(errors))})

            if u.path == "/api/search":
                term = (q.get("q", [""])[0]).strip()
                selected_team_id = (q.get("team_id", [""])[0]).strip()
                if selected_team_id and not selected_team_id.isdigit():
                    return self._send(400, {"error": "invalid team id"})
                if not term:
                    return self._send(200, {"fixtures": [], "logged_in": False})
                cfg = load_config()
                countries = cfg.get("countries") or ["no", "uk", "us"]
                fixtures, src_err = search_fixtures(term, countries)
                try:
                    daily_fixtures = search_daily_matches(term)
                    for daily in daily_fixtures:
                        duplicate = None
                        dday = str(daily.get("start") or "")[:10]
                        for fixture in fixtures:
                            if dday and str(fixture.get("start") or "")[:10] != dday:
                                continue
                            if (_team_names_equivalent(daily.get("home"), fixture.get("home")) and
                                    _team_names_equivalent(daily.get("away"), fixture.get("away"))):
                                duplicate = fixture
                                break
                        if duplicate is not None:
                            duplicate["is_live"] = daily.get("is_live", False)
                            if daily.get("live_minute") is not None:
                                duplicate["live_minute"] = daily.get("live_minute")
                            duplicate["home_id"] = duplicate.get("home_id") or daily.get("home_id", "")
                            duplicate["away_id"] = duplicate.get("away_id") or daily.get("away_id", "")
                        else:
                            fixtures.append(daily)
                    fixtures.sort(key=lambda row: row.get("start") or "")
                except Exception as e:
                    src_err.append(f"FotMob matches: {e}")
                if selected_team_id:
                    fixtures = [fixture for fixture in fixtures
                                if selected_team_id in {
                                    str(fixture.get("home_id") or ""),
                                    str(fixture.get("away_id") or "")}]
                x = Xtream(cfg)
                logged_in = x.configured()
                channels, cats = [], {}
                if logged_in:
                    try:
                        channels, cats = get_xtream_channels(cfg)
                    except Exception as e:
                        src_err.append(f"Xtream: {e}")
                        logged_in = False
                try:
                    thr = float(q.get("strictness", [cfg.get("match_threshold", 0.62)])[0])
                except (TypeError, ValueError):
                    thr = float(cfg.get("match_threshold", 0.62) or 0.62)
                thr = max(0.40, min(0.80, thr))
                ppv_cats = ppv_categories(channels, cats) if logged_in else []
                out = []
                for f in fixtures:
                    matches = []
                    ppv_hits = []
                    streaming_only = False
                    if logged_in:
                        rows = rank_fixture_channels(
                            match_channels(f["by_country"], channels, cats, thr),
                            f.get("home"), f.get("away"))
                        for r in rows:
                            r["url"] = x.stream_url(r["stream_id"])
                        matches = rows
                        # Streaming/PPV fallback: if broadcasters are streaming
                        # services, try to find channels named after the teams.
                        all_bcasters = [b for names in f["by_country"].values() for b in names]
                        has_linear = any(not _is_streaming(b) for b in all_bcasters)
                        has_streaming = any(_is_streaming(b) for b in all_bcasters)
                        if has_streaming:
                            hits = find_team_channels([f["home"], f["away"]], channels, cats, x)
                            # avoid duplicating channels already matched
                            have = {m["stream_id"] for m in matches}
                            ppv_hits = [h for h in hits if h["stream_id"] not in have]
                        # "only streaming" = no linear broadcaster AND no normal matches
                        streaming_only = (has_streaming and not has_linear and not matches)
                    out.append({"home": f["home"], "away": f["away"], "start": f["start"],
                                "home_id": f.get("home_id", ""),
                                "away_id": f.get("away_id", ""),
                                "by_country": f["by_country"], "matches": matches,
                                "ppv_hits": ppv_hits, "streaming_only": streaming_only,
                                "is_live": bool(f.get("is_live")),
                                "live_minute": f.get("live_minute")})
                return self._send(200, {"fixtures": out, "logged_in": logged_in,
                                        "source_errors": src_err,
                                        "ppv_categories": ppv_cats})

            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def _post_core_api(self, path, payload):
        if path == "/api/activity":
            _mark_app_activity()
            return self._send(200, {"ok": True})
        if path == "/api/shutdown":
            self._send(200, {"ok": True})
            _STOP_EVENT.set()
            return
        if path == "/api/test_credentials":
            test_cfg = dict(DEFAULT_CONFIG)
            test_cfg.update({"xtream_host": str(payload.get("xtream_host") or "").strip(),
                             "xtream_port": str(payload.get("xtream_port") or "").strip(),
                             "xtream_user": str(payload.get("xtream_user") or "").strip(),
                             "xtream_pass": str(payload.get("xtream_pass") or "")})
            if not Xtream(test_cfg).configured():
                return self._send(200, {"ok": False, "error": "Host, username and password are required"})
            ok, info = Xtream(test_cfg).login()
            return self._send(200, {"ok": ok, "info": info if ok else None,
                                    "error": None if ok else info})
        if path == "/api/match_strictness":
            cfg = load_config()
            try:
                strict = float(payload.get("match_threshold", cfg.get("match_threshold", 0.62)))
            except (TypeError, ValueError):
                strict = 0.62
            strict = max(0.40, min(0.80, strict))
            cfg["match_threshold"] = strict
            save_config(cfg)
            _clear_sports_event_channel_cache()
            return self._send(200, {"ok": True, "match_threshold": strict})
        if path == "/api/racing_series":
            cfg = load_config()
            allowed = ("f1", "f2", "f3", "indycar", "wec", "formulae", "motogp", "wrc")
            requested = payload.get("series") if isinstance(payload.get("series"), list) else []
            selected = [key for key in allowed if key in requested]
            cfg["racing_series"] = selected
            save_config(cfg)
            _clear_racing_availability_cache()
            return self._send(200, {"ok": True, "series": selected})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        if length > 5 * 1024 * 1024:
            return self._send(413, {"error": "Backup or request is too large"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        if u.path in {"/api/activity", "/api/shutdown", "/api/test_credentials",
                      "/api/match_strictness", "/api/racing_series"}:
            return self._post_core_api(u.path, payload)
        if u.path == "/api/profile_backup_export":
            kind = "full" if payload.get("type") == "full" else "profile"
            return self._send(200, create_profile_backup(kind, payload.get("timeline")))
        if u.path == "/api/profile_backup_import":
            try:
                result = restore_profile_backup(payload.get("backup"))
                return self._send(200, dict({"ok": True}, **result))
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": "Could not restore backup: " + str(e)})
        if u.path == "/api/sports_event_channels":
            fixture = payload.get("fixture")
            if not isinstance(fixture, dict):
                return self._send(400, {"error": "Missing sports fixture"})
            home = str(fixture.get("home") or "").strip()[:160]
            away = str(fixture.get("away") or "").strip()[:160]
            start = str(fixture.get("start") or "").strip()[:64]
            by_country = fixture.get("by_country")
            if not home or not away or not isinstance(by_country, dict):
                return self._send(400, {"error": "Invalid sports fixture"})
            cleaned_tv = {}
            for country, names in list(by_country.items())[:24]:
                if not isinstance(names, list):
                    continue
                code = str(country or "").strip().upper()[:4]
                cleaned_tv[code] = [str(name or "").strip()[:120]
                                    for name in names[:30] if str(name or "").strip()]
            cfg = load_config()
            x = Xtream(cfg)
            key = (_vod_cache_key(x), str(cfg.get("match_threshold") or 0.62),
                   _sports_event_key(home, away, start))
            cached = _SPORTS_EVENT_CHANNEL_CACHE.get(key)
            fresh = bool(cached and time.time() - float(cached.get("ts") or 0)
                         < _SPORTS_EVENT_CHANNEL_TTL)
            if fresh and not payload.get("force"):
                return self._send(200, dict(cached.get("result") or {}, cached=True))
            if payload.get("cached_only"):
                return self._send(200, {"cached": False})
            try:
                result = find_sports_event_channels(
                    {"home": home, "away": away, "start": start,
                     "by_country": cleaned_tv}, cfg)
                _SPORTS_EVENT_CHANNEL_CACHE[key] = {"ts": time.time(), "result": result}
                disk_entries = _load_sports_disk_cache(cfg, x)
                disk_entries[_sports_event_key(home, away, start)] = {
                    "ts": time.time(), "result": _sports_result_for_storage(result)}
                _save_sports_disk_cache(cfg, x, disk_entries)
                return self._send(200, dict(result, cached=False))
            except Exception as e:
                return self._send(502, {"error": "Sports channel search: " + str(e)})
        if u.path == "/api/sports_availability":
            incoming = payload.get("fixtures")
            if not isinstance(incoming, list):
                return self._send(400, {"error": "Missing sports fixtures"})
            cfg = load_config(); x = Xtream(cfg)
            if not x.configured():
                return self._send(200, {"availability": {}, "logged_in": False})
            try:
                channels, cats = get_xtream_channels(cfg)
            except Exception as e:
                return self._send(502, {"error": "Sports channel catalogue: " + str(e)})
            availability = {}; now = time.time()
            disk_entries = _load_sports_disk_cache(cfg, x)
            for raw_fixture in incoming[:160]:
                if not isinstance(raw_fixture, dict):
                    continue
                home = str(raw_fixture.get("home") or "").strip()[:160]
                away = str(raw_fixture.get("away") or "").strip()[:160]
                start = str(raw_fixture.get("start") or "").strip()[:64]
                by_country = raw_fixture.get("by_country")
                if not home or not away or not isinstance(by_country, dict):
                    continue
                try:
                    event_ts = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
                    if event_ts < now - 6 * 3600 or event_ts > now + 45 * 24 * 3600:
                        continue
                except Exception:
                    continue
                cleaned_tv = {}
                for country, names in list(by_country.items())[:24]:
                    if isinstance(names, list):
                        cleaned_tv[str(country or "").strip().upper()[:4]] = [
                            str(name or "").strip()[:120] for name in names[:30]
                            if str(name or "").strip()]
                key = (_vod_cache_key(x), str(cfg.get("match_threshold") or 0.62),
                       _sports_event_key(home, away, start))
                cached = _SPORTS_EVENT_CHANNEL_CACHE.get(key)
                disk_key = _sports_event_key(home, away, start)
                if not cached:
                    stored = disk_entries.get(disk_key)
                    if isinstance(stored, dict) and isinstance(stored.get("result"), dict):
                        cached = {"ts": float(stored.get("ts") or 0),
                                  "result": _sports_result_for_client(stored["result"], x)}
                fresh = bool(cached and now - float(cached.get("ts") or 0) <
                             _SPORTS_EVENT_CHANNEL_TTL)
                if fresh and not payload.get("force"):
                    result = cached.get("result") or {}
                else:
                    result = _match_sports_fixture_channels(
                        {"home": home, "away": away, "start": start,
                         "by_country": cleaned_tv}, cfg, channels, cats, x)
                    _SPORTS_EVENT_CHANNEL_CACHE[key] = {"ts": time.time(), "result": result}
                    disk_entries[disk_key] = {"ts": time.time(),
                                              "result": _sports_result_for_storage(result)}
                availability["|".join((home.lower(), away.lower(), start[:16]))] = result
            _save_sports_disk_cache(cfg, x, disk_entries)
            return self._send(200, {"availability": availability, "logged_in": True})
        if u.path == "/api/import_steam_wishlist":
            cfg = load_config()
            saved_url = str(cfg.get("steam_wishlist_url") or "").strip()
            wishlist_url = str(payload.get("url") or saved_url).strip()
            if not wishlist_url:
                return self._send(400, {"error": "Enter a Steam wishlist URL"})
            try:
                cached_id = str(cfg.get("steam_wishlist_id") or "") if saved_url == wishlist_url else ""
                steam_id = cached_id if re.fullmatch(r"\d{17}", cached_id) else resolve_steam_wishlist_id(wishlist_url)
                if not steam_id:
                    return self._send(400, {"error": "Could not resolve that Steam profile"})
                wishlist = steam_wishlist_items(steam_id)
                ids = [str(item.get("appid")) for item in wishlist if str(item.get("appid") or "").isdigit()]
                metadata = {row["app_id"]: row for row in steam_store_items(ids)}
                priorities = {str(item.get("appid")): int(item.get("priority") or 0) for item in wishlist}
                fav = load_favorites()
                current_ids = set(ids)
                # Remove only games previously imported from this wishlist; manual favorites stay.
                fav["games"] = [game for game in fav.get("games", [])
                                if not (game.get("wishlist_imported") and str(game.get("app_id")) not in current_ids)]
                by_id = {str(game.get("app_id")): game for game in fav["games"]}
                for app_id in ids:
                    details = metadata.get(app_id)
                    if not details:
                        continue
                    existing = by_id.get(app_id)
                    if existing is None:
                        existing = {"app_id": app_id, "wishlist_imported": True}
                        fav["games"].append(existing)
                        by_id[app_id] = existing
                    existing["wishlist_imported"] = True
                    existing.update({"name": details.get("name") or existing.get("name") or "Game",
                                     "cover": details.get("cover") or existing.get("cover") or "",
                                     "release_text": details.get("release_text") or existing.get("release_text") or "",
                                     "released": details.get("released") or existing.get("released") or "",
                                     "url": details.get("url") or existing.get("url") or "",
                                     "wishlist_priority": priorities.get(app_id, 0)})
                save_favorites(fav)
                cfg["steam_wishlist_url"] = wishlist_url
                cfg["steam_wishlist_id"] = steam_id
                cfg["steam_wishlist_synced_at"] = int(time.time())
                save_config(cfg)
                try:
                    steam_public_profile(steam_id, force=True)
                except Exception:
                    pass
                return self._send(200, {"ok": True, "imported": len(metadata),
                                        "wishlist_total": len(ids),
                                        "synced_at": cfg["steam_wishlist_synced_at"]})
            except Exception as e:
                return self._send(502, {"error": "Steam wishlist: " + str(e)})
        if u.path == "/api/config":
            cfg = load_config()
            provider_before = tuple(str(cfg.get(k) or "") for k in
                                    ("xtream_host", "xtream_port", "xtream_user", "xtream_pass"))
            for k in ("xtream_host", "xtream_port", "xtream_user", "xtream_pass",
                      "stream_ext", "match_threshold", "countries", "start_section",
                      "check_shows_on_startup", "refresh_iptv_on_startup", "refresh_sports_on_startup", "profile_name",
                      "preferred_language", "profile_emblem", "mylist_layout", "football_enabled",
                      "f1_enabled", "games_enabled", "decorations_enabled", "background_style", "setup_complete", "setup_demo_content", "auto_shutdown_minutes"):
                if k in payload:
                    cfg[k] = payload[k]
            if cfg.get("stream_ext") not in ("ts", "m3u8"):
                cfg["stream_ext"] = "ts"
            try:
                cfg["match_threshold"] = max(0.40, min(0.80, float(cfg.get("match_threshold", 0.62))))
            except (TypeError, ValueError):
                cfg["match_threshold"] = 0.62
            raw_countries = cfg.get("countries") if isinstance(cfg.get("countries"), list) else []
            cfg["countries"] = list(dict.fromkeys(str(code).strip().lower() for code in raw_countries
                                                   if re.fullmatch(r"[a-zA-Z]{2}", str(code).strip())))[:16] or ["no", "gb", "us"]
            if cfg.get("preferred_language") not in ("en", "no"):
                cfg["preferred_language"] = "en"
            if cfg.get("mylist_layout") not in ("balanced", "spotlight", "timeline", "hub"):
                cfg["mylist_layout"] = "timeline"
            allowed_starts = ("mylist", "mytimeline", "channels", "mytv", "movies", "shows", "games", "racing", "teams")
            if cfg.get("start_section") not in allowed_starts:
                cfg["start_section"] = "mylist"
            if "background_style" not in payload and "decorations_enabled" in payload:
                cfg["background_style"] = "float" if payload.get("decorations_enabled") else "off"
            if cfg.get("background_style") not in ("float", "ascii", "off"):
                cfg["background_style"] = "float" if cfg.get("decorations_enabled", True) else "off"
            cfg["decorations_enabled"] = cfg["background_style"] != "off"
            cfg["hide_cmd_window"] = True
            try:
                cfg["auto_shutdown_minutes"] = max(0, int(cfg.get("auto_shutdown_minutes") or 0))
            except (TypeError, ValueError):
                cfg["auto_shutdown_minutes"] = 0
            if cfg["auto_shutdown_minutes"] not in (0, 30, 60, 120, 240):
                cfg["auto_shutdown_minutes"] = 0
            cfg["check_shows_on_startup"] = bool(cfg.get("check_shows_on_startup"))
            cfg["refresh_iptv_on_startup"] = bool(cfg.get("refresh_iptv_on_startup"))
            cfg["refresh_sports_on_startup"] = bool(cfg.get("refresh_sports_on_startup"))
            cfg.pop("refresh_all_on_startup", None)
            cfg.pop("startup_refresh_mode", None)
            save_config(cfg)
            provider_after = tuple(str(cfg.get(k) or "") for k in
                                   ("xtream_host", "xtream_port", "xtream_user", "xtream_pass"))
            if provider_after != provider_before:
                _clear_provider_caches()
            return self._send(200, {"ok": True})

        if u.path == "/api/clear_artwork_cache":
            root = artwork_cache_dir()
            removed = artwork_cache_size()
            try:
                if os.path.isdir(root):
                    shutil.rmtree(root)
                _TVMAZE_CACHE.clear()
                return self._send(200, {"ok": True, "removed_bytes": removed})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})

        if u.path == "/api/reset_cold_start":
            removed_schedules = 0
            try:
                _clear_provider_caches()
                _TV_CACHE.clear()
                _TEAM_FIXTURE_CACHE.clear()
                _TEAM_PROFILE_CACHE.clear()
                _TEAM_ID_CACHE.clear()
                _DAILY_MATCH_CACHE.update({"date": "", "ts": 0, "matches": []})
                _F1_SCHEDULE_CACHE.update({"ts": 0, "events": []})
                _F1_TEAMS_CACHE.update({"ts": 0, "teams": []})
                _clear_racing_availability_cache()
                _TVMAZE_CACHE.clear()
                cache_root = data_cache_dir()
                if os.path.isdir(cache_root):
                    shutil.rmtree(cache_root)
                root = artwork_cache_dir()
                if os.path.isdir(root):
                    for base, _dirs, files in os.walk(root):
                        for name in files:
                            if name not in ("episode-schedule.json", "latest-episode.json",
                                            "latest-episodes.json"):
                                continue
                            try:
                                os.remove(os.path.join(base, name))
                                removed_schedules += 1
                            except OSError:
                                pass
                return self._send(200, {"ok": True,
                                        "removed_schedules": removed_schedules})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})

        if u.path == "/api/check_show_updates":
            cfg = load_config()
            try:
                result = refresh_favorite_show_episodes(cfg)
                return self._send(200, dict({"ok": True}, **result))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(502, {"error": str(e)})

        if u.path == "/api/check_team_fixtures":
            fav_data = load_favorites()
            favorites = fav_data.get("teams", [])
            _TEAM_FIXTURE_CACHE.clear()
            _TEAM_PROFILE_CACHE.clear()
            _remove_data_cache_prefix("team-fixtures-")
            _remove_data_cache_prefix(f"team-profile-v{_TEAM_PROFILE_CACHE_SCHEMA}-")
            refreshed = 0
            errors = []
            changed = False
            for favorite in favorites:
                team_name = str(favorite.get("name") if isinstance(favorite, dict)
                                else favorite).strip()
                if not team_name:
                    continue
                team_id = str(favorite.get("team_id") if isinstance(favorite, dict)
                              else "").strip()
                if not team_id:
                    try:
                        team_id = resolve_fotmob_team_id(team_name)
                    except Exception as e:
                        errors.append(f"{team_name}: {e}")
                if not team_id:
                    errors.append(f"{team_name}: team not found")
                    continue
                if isinstance(favorite, dict) and not favorite.get("team_id"):
                    favorite["team_id"] = team_id
                    changed = True
                try:
                    fetch_team_schedule(team_id, team_name)
                    refreshed += 1
                except Exception as e:
                    errors.append(f"{team_name}: {e}")
            if changed:
                save_favorites(fav_data)
            return self._send(200, {"ok": True, "teams": refreshed,
                                    "errors": errors})

        if u.path == "/api/refresh_football":
            cfg = load_config()
            try:
                _DAILY_MATCH_CACHE.update({"date": "", "ts": 0, "matches": []})
                _TV_CACHE.clear()
                _remove_data_cache_prefix("fotmob-daily")
                _remove_data_cache_prefix("tv-guide-")
                daily = fetch_fotmob_daily_matches()
                guides = 0
                for country in cfg.get("countries", ["no", "gb", "us"]):
                    fetch_country_fixtures(country)
                    guides += 1
                fav_data = load_favorites()
                _TEAM_FIXTURE_CACHE.clear()
                _TEAM_PROFILE_CACHE.clear()
                _remove_data_cache_prefix("team-fixtures-")
                _remove_data_cache_prefix(f"team-profile-v{_TEAM_PROFILE_CACHE_SCHEMA}-")
                teams = 0
                errors = []
                changed = False
                for favorite in fav_data.get("teams", []):
                    team_name = str(favorite.get("name") if isinstance(favorite, dict) else favorite).strip()
                    if not team_name:
                        continue
                    team_id = str(favorite.get("team_id") if isinstance(favorite, dict) else "").strip()
                    if not team_id:
                        try:
                            team_id = resolve_fotmob_team_id(team_name)
                        except Exception as e:
                            errors.append(f"{team_name}: {e}")
                    if not team_id:
                        continue
                    if isinstance(favorite, dict) and not favorite.get("team_id"):
                        favorite["team_id"] = team_id
                        changed = True
                    try:
                        fetch_team_schedule(team_id, team_name)
                        teams += 1
                    except Exception as e:
                        errors.append(f"{team_name}: {e}")
                if changed:
                    save_favorites(fav_data)
                return self._send(200, {"ok": True, "teams": teams, "guides": guides,
                                        "matches": len(daily), "errors": errors})
            except Exception as e:
                return self._send(502, {"error": str(e)})

        if u.path == "/api/check_movie_updates":
            cfg = load_config()
            x = Xtream(cfg)
            if not x.configured():
                return self._send(400, {"error": "Not configured"})
            try:
                previous = (_VOD_CACHE.get("movies") or _load_vod_catalog_cache(x))
                previous_ids = {str(row.get("stream_id")) for row in previous
                                if isinstance(row, dict) and row.get("stream_id") is not None}
                fresh = x.vod_streams()
                if not fresh:
                    raise RuntimeError("Provider returned an empty movie catalog")
                movies = _save_vod_catalog_cache(x, fresh)
                _VOD_CACHE.update({"provider": _vod_cache_key(x), "ts": time.time(), "movies": movies})
                fresh_ids = {str(row.get("stream_id")) for row in movies
                             if row.get("stream_id") is not None}
                new_movies = len(fresh_ids - previous_ids) if previous_ids else 0
                return self._send(200, {"ok": True, "movies": len(movies),
                                        "new_movies": new_movies})
            except Exception as e:
                return self._send(502, {"error": str(e)})

        if u.path == "/api/refresh_xtream":
            cfg = load_config()
            x = Xtream(cfg)
            if not x.configured():
                return self._send(400, {"error": "Xtream is not configured"})
            try:
                channels, _cats = get_xtream_channels(cfg, force=True)
                _clear_sports_event_channel_cache()
                movies = get_xtream_movies(cfg, force=True)
                shows = get_xtream_series(cfg, force=True)
                episode_result = refresh_favorite_show_episodes(cfg)
                return self._send(200, dict({"ok": True,
                    "channels": len(channels), "movies": len(movies),
                    "shows": len(shows)}, **episode_result))
            except Exception as e:
                return self._send(502, {"error": str(e)})

        if u.path == "/api/refresh_racing":
            cfg = load_config()
            try:
                selected = cfg.get("racing_series", ["f1"])
                _clear_racing_availability_cache()
                events = get_racing_events(selected, force=True)
                if "f1" in selected:
                    get_f1_teams(force=True)
                get_racing_drivers(force=True)
                return self._send(200, {"ok": True, "series": len(selected),
                                        "events": len(events)})
            except Exception as e:
                return self._send(502, {"error": str(e)})

        if u.path == "/api/test_source":
            key = str(payload.get("key") or "").strip()
            if key not in _SOURCE_LABEL_MAP:
                return self._send(400, {"error": "unknown source"})
            result = test_external_source(key)
            return self._send(200, {"ok": True, "result": result,
                                    "sources": source_health_snapshot()})

        if u.path == "/api/favorites":
            # actions: category/channel/movie favorite management and reordering
            fav = load_favorites()
            act = payload.get("action", "")
            if act == "add_cats":
                for c in payload.get("categories", []):
                    if c and c not in fav["categories"]:
                        fav["categories"].append(c)
            elif act == "remove_cat":
                fav["categories"] = [c for c in fav["categories"] if c != payload.get("category")]
            elif act == "add_channels":
                have = {str(c.get("stream_id")) for c in fav["channels"]}
                for ch in payload.get("channels", []):
                    sid = str(ch.get("stream_id"))
                    if sid and sid not in have:
                        fav["channels"].append({"stream_id": ch.get("stream_id"),
                                                "name": ch.get("name", ""),
                                                "category": ch.get("category", ""),
                                                "logo": ch.get("logo") or _stream_icon_for_id(sid)})
                        have.add(sid)
            elif act == "toggle_channel":
                sid = str(payload.get("stream_id"))
                idx = next((i for i, c in enumerate(fav["channels"]) if str(c.get("stream_id")) == sid), -1)
                if idx >= 0:
                    fav["channels"].pop(idx)
                else:
                    fav["channels"].append({"stream_id": payload.get("stream_id"),
                                            "name": payload.get("name", ""),
                                            "category": payload.get("category", ""),
                                            "logo": payload.get("logo") or _stream_icon_for_id(sid)})
            elif act == "remove_channel":
                sid = str(payload.get("stream_id"))
                fav["channels"] = [c for c in fav["channels"] if str(c.get("stream_id")) != sid]
            elif act == "reorder_channels":
                requested = [str(sid) for sid in payload.get("stream_ids", [])]
                by_id = {str(c.get("stream_id")): c for c in fav["channels"]}
                reordered = [by_id.pop(sid) for sid in requested if sid in by_id]
                # Preserve any channels added concurrently or omitted by an old client.
                reordered.extend(c for c in fav["channels"] if str(c.get("stream_id")) in by_id)
                fav["channels"] = reordered
            elif act == "set_mylist_channels":
                favorite_ids = {str(c.get("stream_id")) for c in fav["channels"]}
                chosen = []
                for sid in payload.get("stream_ids", []):
                    sid = str(sid)
                    if sid in favorite_ids and sid not in chosen:
                        chosen.append(sid)
                    if len(chosen) >= 5:
                        break
                fav["mylist_channels"] = chosen
            elif act == "toggle_movie":
                movie = payload.get("movie") or {}
                catalog_id = str(movie.get("catalog_id") or "").strip()
                if not catalog_id and movie.get("name"):
                    try:
                        wanted_name = _clean_show_title(movie.get("name")) or str(movie.get("name"))
                        wanted_year = str(movie.get("year") or _provider_year(movie) or "")
                        matches = [row for row in cinemeta_search("movie", wanted_name)
                                   if _show_key(row.get("name")) == _show_key(wanted_name)]
                        chosen = next((row for row in matches
                                       if wanted_year and _catalog_year(row) == wanted_year),
                                      matches[0] if matches else None)
                        if chosen:
                            catalog_id = str(chosen.get("id") or "")
                            movie = dict(movie)
                            movie["catalog_id"] = catalog_id
                            movie["name"] = chosen.get("name") or wanted_name
                            movie["year"] = _catalog_year(chosen) or wanted_year
                            movie["cover"] = chosen.get("poster") or movie.get("cover") or ""
                    except Exception:
                        pass
                sid = str(movie.get("stream_id", ""))
                favorite_key = catalog_id or sid
                idx = -1
                for i, existing in enumerate(fav["movies"]):
                    same_id = str(existing.get("catalog_id") or existing.get("stream_id")) == favorite_key
                    same_title = _show_key(existing.get("name")) == _show_key(movie.get("name"))
                    same_year = (not movie.get("year") or not existing.get("year") or
                                 str(existing.get("year")) == str(movie.get("year")))
                    if same_id or (same_title and same_year):
                        idx = i
                        break
                if idx >= 0:
                    fav["movies"].pop(idx)
                elif favorite_key:
                    released = str(movie.get("released") or "")
                    if catalog_id and not released:
                        try:
                            released = str(cinemeta_meta("movie", catalog_id).get("released") or "")
                        except Exception:
                            released = ""
                    fav["movies"].append({
                        "catalog_id": catalog_id,
                        "stream_id": movie.get("stream_id"),
                        "name": movie.get("name", ""),
                        "extension": movie.get("extension", "mp4"),
                        "year": movie.get("year", ""),
                        "rating": movie.get("rating", ""),
                        "cover": movie.get("cover", ""),
                        "released": released,
                    })
                    demo_cfg = load_config()
                    if demo_cfg.get("setup_demo_content"):
                        demo_cfg["setup_demo_content"] = False
                        save_config(demo_cfg)
            elif act == "remove_movie":
                sid = str(payload.get("favorite_key") or payload.get("stream_id", ""))
                fav["movies"] = [m for m in fav["movies"]
                                 if str(m.get("catalog_id") or m.get("stream_id")) != sid]
            elif act == "toggle_show":
                show = payload.get("show") or {}
                catalog_id = str(show.get("catalog_id") or "").strip()
                title_key = str(show.get("show_key") or _show_key(show.get("name")) or "")
                key = str(catalog_id or show.get("show_key") or _show_key(show.get("name")) or
                          show.get("series_id", ""))
                idx = next((i for i, s in enumerate(fav["shows"])
                            if (str(s.get("catalog_id") or s.get("show_key") or _show_key(s.get("name")) or
                                    s.get("series_id")) == key or
                                (title_key and str(s.get("show_key") or _show_key(s.get("name"))) == title_key))), -1)
                if idx >= 0:
                    fav["shows"].pop(idx)
                elif key:
                    ids = [sid for sid in (show.get("series_ids") or [show.get("series_id")]) if sid not in (None, "")]
                    fav["shows"].append({"catalog_id": catalog_id,
                                          "series_id": ids[0] if ids else None,
                                          "series_ids": ids,
                                          "show_key": title_key,
                                          "name": show.get("name", ""),
                                          "cover": show.get("cover", ""),
                                          "year": show.get("year", ""),
                                          "rating": show.get("rating", "")})
                    demo_cfg = load_config()
                    if demo_cfg.get("setup_demo_content"):
                        demo_cfg["setup_demo_content"] = False
                        save_config(demo_cfg)
                _invalidate_latest_episodes_cache()
            elif act == "remove_show":
                key = str(payload.get("show_key") or payload.get("series_id", ""))
                fav["shows"] = [s for s in fav["shows"]
                                if str(s.get("catalog_id") or s.get("show_key") or _show_key(s.get("name")) or
                                       s.get("series_id")) != key]
                _invalidate_latest_episodes_cache()
            elif act == "toggle_team":
                team = payload.get("team") or {}
                name = str(team.get("name") or "").strip()
                idx = next((i for i, item in enumerate(fav["teams"])
                            if str(item.get("name") if isinstance(item, dict) else item).lower()
                            == name.lower()), -1)
                if idx >= 0:
                    fav["teams"].pop(idx)
                elif name:
                    team_id = str(team.get("team_id") or "")
                    fav["teams"].append({"name": name,
                                         "team_id": team_id,
                                         "logo": _team_logo_url(team_id)})
            elif act == "remove_team":
                name = str(payload.get("name") or "").strip().lower()
                fav["teams"] = [item for item in fav["teams"]
                                if str(item.get("name") if isinstance(item, dict) else item).lower()
                                != name]
            elif act == "set_f1_team":
                team = payload.get("team") or {}
                constructor_id = re.sub(r"[^0-9A-Za-z_-]", "", str(team.get("id") or ""))
                name = str(team.get("name") or "").strip()
                if constructor_id and name:
                    fav["f1_teams"] = [{"id": constructor_id, "name": name,
                                         "logo": _f1_logo_url(constructor_id)}]
                else:
                    fav["f1_teams"] = []
            save_favorites(fav)
            return self._send(200, {"ok": True,
                                    "categories": fav["categories"],
                                    "channel_ids": [c.get("stream_id") for c in fav["channels"]],
                                    "movie_ids": [m.get("catalog_id") or m.get("stream_id") for m in fav["movies"]],
                                    "show_ids": [s.get("catalog_id") or s.get("show_key") or _show_key(s.get("name")) or
                                                 s.get("series_id") for s in fav["shows"]],
                                    "team_names": [item.get("name") if isinstance(item, dict) else item
                                                   for item in fav["teams"]],
                                    "game_ids": [item.get("app_id") for item in fav.get("games", [])],
                                    "f1_teams": fav.get("f1_teams", [])})

        if u.path == "/api/update_download":
            path = download_update()
            if path:
                return self._send(200, {"ok": True})
            return self._send(500, {"ok": False, "error": "download failed"})

        if u.path == "/api/update_restart":
            # Swap tvmate_new.py -> tvmate.py and relaunch, via a small helper.
            new = os.path.join(app_dir(), "tvmate_new.py")
            cur = os.path.join(app_dir(), "tvmate.py")
            if not os.path.exists(new):
                return self._send(400, {"ok": False, "error": "no update downloaded"})
            try:
                _remote_version, recovery_sha = _update_manifest()
                # Determine how to relaunch. ONLY relaunch the permanent launcher
                # .exe - never a temp-extracted python.exe (which vanishes).
                launcher_exe = os.environ.get("TVMATE_EXE")
                relaunch = None
                if launcher_exe and os.path.exists(launcher_exe) and launcher_exe.lower().endswith(".exe"):
                    relaunch = '"' + launcher_exe + '"'
                elif getattr(sys, "frozen", False) and os.path.exists(sys.argv[0]):
                    relaunch = '"' + sys.argv[0] + '"'
                # If not running from a launcher/exe (e.g. plain python dev run),
                # relaunch with the interpreter only if it's a real, stable path.
                elif not getattr(sys, "frozen", False) and "temp" not in (sys.executable or "").lower():
                    relaunch = '"' + sys.executable + '" "' + cur + '"'

                if sys.platform.startswith("win"):
                    helper = os.path.join(app_dir(), "_update.bat")
                    launcher_name = os.path.basename(launcher_exe or "")
                    known_launcher = bool(re.fullmatch(
                        r"(?:OTVM|OlosTVMate)(?:\s*\(\d+\))?\.exe",
                        launcher_name, flags=re.IGNORECASE))
                    lines = ["@echo off\r\n",
                             "title Updating TVMate\r\n",
                             'cd /d "' + app_dir() + '"\r\n',
                             "echo.\r\n",
                             "echo Updating TVMate...\r\n",
                             "echo Please wait while TVMate restarts.\r\n",
                             "timeout /t 3 /nobreak >nul\r\n"]
                    # Nuitka's old onefile launcher can survive its child and
                    # prevent a clean relaunch. Kill only a known TVMate image.
                    if known_launcher:
                        lines.extend(['taskkill /f /im "' + launcher_name +
                                      '" >nul 2>&1\r\n',
                                      "timeout /t 1 /nobreak >nul\r\n"])
                    lines.extend([
                             'copy /y "' + cur + '" "' + cur + '.backup" >nul\r\n',
                             "for /l %%I in (1,1,20) do (\r\n",
                             '  move /y "' + new + '" "' + cur + '" >nul 2>&1 && goto updated\r\n',
                             "  timeout /t 1 /nobreak >nul\r\n",
                             ")\r\n",
                             "echo Normal update failed. Trying a clean download...\r\n"])
                    if recovery_sha:
                        ps_url = UPDATE_SCRIPT_URL.replace("'", "''")
                        ps_new = new.replace("'", "''")
                        lines.extend([
                             'del /f /q "' + cur + '" >nul 2>&1\r\n',
                             'del /f /q "' + new + '" >nul 2>&1\r\n',
                             'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference=\'SilentlyContinue\'; try { Invoke-WebRequest -UseBasicParsing -Uri \'' + ps_url + '\' -OutFile \'' + ps_new + '\'; if ((Get-FileHash -Algorithm SHA256 -LiteralPath \'' + ps_new + '\').Hash.ToLower() -ne \'' + recovery_sha + '\') { throw \'checksum mismatch\' } } catch { Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath \'' + ps_new + '\'; exit 1 }"\r\n',
                             "if errorlevel 1 goto recoverfailed\r\n",
                             'move /y "' + new + '" "' + cur + '" >nul 2>&1 || goto recoverfailed\r\n',
                             "goto updated\r\n"])
                    else:
                        lines.append("echo Update manifest checksum is unavailable.\r\n")
                    lines.extend([
                             ":recoverfailed\r\n",
                             "echo Clean download failed. Restoring the previous version.\r\n",
                             'if exist "' + cur + '.backup" copy /y "' + cur + '.backup" "' + cur + '" >nul\r\n',
                             "goto relaunch\r\n",
                             ":updated\r\n",
                             "echo Update installed successfully.\r\n",
                             ":relaunch\r\n"])
                    if relaunch:
                        lines.extend(["echo Starting TVMate...\r\n",
                                      'start "" ' + relaunch + "\r\n"])
                    lines.extend(["timeout /t 3 /nobreak >nul\r\n",
                                  'del "%~f0"\r\n'])
                    with open(helper, "w", encoding="utf-8", newline="") as f:
                        f.writelines(lines)
                    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
                    subprocess.Popen(["cmd.exe", "/d", "/c", helper],
                                     cwd=app_dir(), creationflags=flags,
                                     close_fds=True)
                else:
                    helper = os.path.join(app_dir(), "_update.sh")
                    body = "#!/bin/sh\nsleep 2\ncp -f '" + cur + "' '" + cur + ".backup'\nmv -f '" + new + "' '" + cur + "'\n"
                    if relaunch:
                        body += relaunch + " &\n"
                    body += 'rm -- "$0"\n'
                    with open(helper, "w", encoding="utf-8") as f:
                        f.write(body)
                    os.chmod(helper, 0o755)
                    subprocess.Popen(["/bin/sh", helper], start_new_session=True)
                def _bye():
                    import time as _t; _t.sleep(1); os._exit(0)
                import threading as _th; _th.Thread(target=_bye, daemon=True).start()
                return self._send(200, {"ok": True, "relaunch": bool(relaunch)})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})

        if u.path == "/api/open_folder":
            folder = app_dir()
            try:
                if sys.platform.startswith("win"):
                    os.startfile(folder)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
                return self._send(200, {"ok": True, "path": folder})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e), "path": folder})

        if u.path == "/api/play":
            # Launch VLC with a stream url (stream_id -> ts url).
            sid = str(payload.get("stream_id", "")).strip()
            cfg = load_config()
            x = Xtream(cfg)
            if not (x.configured() and sid):
                return self._send(400, {"error": "bad request"})
            url = x.stream_url(sid)
            vlc = _find_vlc()
            if not vlc:
                return self._send(404, {"error": "VLC not found. Install VLC or use Copy."})
            try:
                subprocess.Popen([vlc, url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/play_movie":
            sid = str(payload.get("stream_id", "")).strip()
            ext = str(payload.get("extension", "mp4")).strip()
            cfg = load_config()
            x = Xtream(cfg)
            if not (x.configured() and sid):
                return self._send(400, {"error": "bad request"})
            vlc = _find_vlc()
            if not vlc:
                return self._send(404, {"error": "VLC not found."})
            try:
                subprocess.Popen([vlc, x.movie_url(sid, ext)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/play_episode":
            episode_id = str(payload.get("episode_id", "")).strip()
            ext = str(payload.get("extension", "mp4")).strip()
            cfg = load_config()
            x = Xtream(cfg)
            vlc = _find_vlc()
            if not (x.configured() and episode_id and vlc):
                return self._send(400, {"error": "VLC not found or episode is invalid."})
            try:
                subprocess.Popen([vlc, x.episode_url(episode_id, ext)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/play_season":
            episodes = payload.get("episodes") or []
            cfg = load_config()
            x = Xtream(cfg)
            vlc = _find_vlc()
            urls = [x.episode_url(ep.get("id"), ep.get("extension", "mp4"))
                    for ep in episodes if ep.get("id") is not None]
            if not (x.configured() and urls and vlc):
                return self._send(400, {"error": "VLC not found or season is empty."})
            try:
                subprocess.Popen([vlc] + urls,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._send(200, {"ok": True, "count": len(urls)})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/m3u":
            # Build an M3U from selected categories and/or specific stream_ids.
            cfg = load_config()
            x = Xtream(cfg)
            if not x.configured():
                return self._send(400, {"error": "Not configured"})
            try:
                channels, cats = get_xtream_channels(cfg)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            sel_cats = set(payload.get("categories") or [])
            sel_ids = set(str(i) for i in (payload.get("stream_ids") or []))
            mode = payload.get("mode", "categories")  # "categories" or "channels"
            lines = ["#EXTM3U"]
            n = 0
            for ch in channels:
                catname = cats.get(ch["category_id"], "")
                include = False
                if mode == "channels":
                    include = str(ch["stream_id"]) in sel_ids
                else:  # categories
                    include = catname in sel_cats
                if not include:
                    continue
                name = ch["name"]
                grp = catname.replace(",", " ")
                icon = str(ch.get("stream_icon") or "").replace('"', '%22')
                logo_attr = f' tvg-logo="{icon}"' if icon else ""
                lines.append(f'#EXTINF:-1 group-title="{grp}"{logo_attr},{name}')
                lines.append(x.stream_url(ch["stream_id"]))
                n += 1
            body = "\n".join(lines) + "\n"
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="playlist.m3u"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return self._send(404, {"error": "not found"})

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

_STOP_EVENT = threading.Event()

def _auto_shutdown_watchdog():
    while not _STOP_EVENT.wait(15):
        try:
            cfg = load_config()
            minutes = max(0, int(cfg.get("auto_shutdown_minutes") or 0))
            if cfg.get("hide_cmd_window") and minutes and _inactive_seconds() >= minutes * 60:
                _STOP_EVENT.set()
                return
        except Exception:
            pass

def _enable_ansi():
    """Turn on ANSI color in the Windows console. Since some environments
    (e.g. compiled onefile exes) support color even when the handle dance
    fails, we default to True and just TRY to enable VT processing."""
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # stdout, stderr
            h = k.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass
    # Assume color works (the console has shown ANSI color before).
    return True

def _set_console_visible(visible):
    """Attach/detach this process' Windows console. No-op elsewhere."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        if not visible:
            # SW_HIDE can become a minimize operation under Windows Terminal.
            # Detaching closes the console for a normal double-click launch.
            k.FreeConsole()
            return
        hwnd = k.GetConsoleWindow()
        # The GUI-subsystem onefile launcher can leave us attached to an
        # invisible/ConPTY console.  ShowWindow cannot make that usable for
        # Retro mode, so launcher-started sessions deliberately replace it
        # with a fresh console window.  Direct `python tvmate.py` runs keep
        # their existing terminal.
        if visible and os.environ.get("TVMATE_EXE") and hwnd:
            k.FreeConsole()
            hwnd = None
        if not hwnd and k.AllocConsole():
            # Reconnect Python's standard streams when switching back to retro
            # mode in the current session. A restart will restore them too.
            try:
                sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            except Exception:
                pass
            hwnd = k.GetConsoleWindow()
        if hwnd:
            try:
                k.SetConsoleTitleW("Olo's TVMate - Retro ASCII mode")
            except Exception:
                pass
            ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
    except Exception:
        pass

def _launch_without_console():
    """Relaunch TVMate as a genuine console-less Windows process."""
    if not sys.platform.startswith("win"):
        return False
    try:
        env = os.environ.copy()
        env["TVMATE_HIDDEN_CHILD"] = "1"
        if getattr(sys, "frozen", False):
            cmd = [sys.executable] + sys.argv[1:]
        else:
            cmd = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.Popen(cmd, env=env, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        return True
    except Exception:
        return False

def _close_launcher_console():
    """Close the dedicated tvmate.exe console without touching user terminals."""
    if not sys.platform.startswith("win"):
        return
    # The permanent TVMate launcher sets this.  Do not close a console when the
    # script was started manually with `python tvmate.py`.
    launcher = os.environ.get("TVMATE_EXE", "").strip()
    if not launcher or not launcher.lower().endswith(".exe"):
        return
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        launcher_norm = os.path.normcase(os.path.abspath(launcher))

        # If the permanent launcher is another process attached to this same
        # console, stop that exact executable.  This is deliberately stricter
        # than killing a parent cmd.exe/terminal process.
        pids = (wintypes.DWORD * 32)()
        count = k.GetConsoleProcessList(pids, len(pids))
        if count > len(pids):
            pids = (wintypes.DWORD * count)()
            count = k.GetConsoleProcessList(pids, len(pids))
        PROCESS_TERMINATE = 0x0001
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        for pid in list(pids)[:count]:
            if not pid or pid == os.getpid():
                continue
            hproc = k.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
                                  False, pid)
            if not hproc:
                continue
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if k.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                    proc_norm = os.path.normcase(os.path.abspath(buf.value))
                    if proc_norm == launcher_norm:
                        k.TerminateProcess(hproc, 0)
            finally:
                k.CloseHandle(hproc)

        hwnd = k.GetConsoleWindow()
        if hwnd:
            # WM_CLOSE closes the dedicated console window.  FreeConsole alone
            # only detaches Python and can leave tvmate.exe's empty window up.
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    except Exception:
        pass

_GOLD = "\033[93m"   # bright yellow (syrup gold)
_RESET = "\033[0m"

def _colored_banner(use_color):
    """Return the banner with the pancake tagline in gold."""
    if not use_color:
        return BANNER
    out = []
    for line in BANNER.split("\n"):
        if "Technically a TV app" in line or "Spiritually a pancake" in line:
            # color just the tagline text, keep the TV art before it uncolored
            idx = line.find("~") if "~" in line else line.find("Spiritually")
            if idx > 0:
                out.append(line[:idx] + _GOLD + line[idx:] + _RESET)
            else:
                out.append(_GOLD + line + _RESET)
        else:
            out.append(line)
    return "\n".join(out)

ENTER_PROMPTS = [
    "Press Enter (the pancakes are getting cold)",
    "Your table's ready - press Enter",
    "Griddle's hot. Press Enter to get flippin'",
    "Ready to cook when you are... press Enter",
    "Powered by pancakes and questionable decisions....press Enter",
]

def _existing_tvmate(port):
    """Return True only when the service already on *port* is Olo's TVMate.

    This deliberately keys off the running web app rather than a launcher
    filename.  OTVM.exe, OloTVMate.exe and Windows copies such as
    ``OTVM (2).exe`` therefore all share the same single-instance check.
    """
    base = f"http://127.0.0.1:{int(port)}"
    # New builds expose a cheap explicit identity endpoint.  Keep the root
    # fallback so a new launcher also detects an older TVMate already running.
    try:
        with urllib.request.urlopen(base + "/api/ping", timeout=0.8) as resp:
            data = json.loads(resp.read(4096).decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("app") == "olos-tvmate":
            return True
    except Exception:
        pass
    try:
        with urllib.request.urlopen(base + "/", timeout=0.8) as resp:
            page = resp.read(8192).decode("utf-8", "replace")
        return "<title>Olo's TVMate</title>" in page
    except Exception:
        return False

def main():
    port = PORT
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except Exception:
            pass
    url = f"http://localhost:{port}"
    # Single-instance check comes before launcher migration/relaunch logic.
    # A second copy should never replace/restart anything underneath the
    # already-running app; it just brings the existing UI back to the user.
    if _existing_tvmate(port):
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return
    cfg = load_config()
    hide_console = True
    hidden_child = os.environ.get("TVMATE_HIDDEN_CHILD") == "1"
    if sys.platform.startswith("win") and not hidden_child:
        # Migrate an old launcher before it can relaunch itself or start the
        # normal server. This also covers a cold bootstrap where no previous
        # local tvmate.py existed: as soon as the launcher runs this current
        # script, it can replace itself once and restart cleanly.
        if not _launcher_is_current() and _start_launcher_migration():
            return
        # Unknown/renamed legacy launchers cannot be safely force-replaced.
        # Keep the old hidden-child fallback for those cases. The verified GUI
        # launcher is already windowless and needs no extra self-relaunch.
        if not _launcher_is_current() and hide_console:
            if _launch_without_console():
                _close_launcher_console()
                return
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    _STOP_EVENT.clear()
    _mark_app_activity()
    if not hide_console and sys.platform.startswith("win"):
        # The GUI-subsystem OTVM launcher intentionally starts without a
        # console.  Retro mode opts back in and creates one here; manual runs
        # from an existing terminal simply keep using their current console.
        _set_console_visible(True)
    use_color = _enable_ansi() if not hide_console else False
    if not hide_console:
        try:
            print(_colored_banner(use_color))
        except Exception:
            try:
                print(BANNER)
            except Exception:
                pass
        print("  " + "=" * 56)
        print(f"   Olo's TVMate is RUNNING   (v{VERSION})")
        print(f"     Watch here ->   {url}")
        print("     To QUIT    ->   close this window   (or press Ctrl+C)")
        print("  " + "=" * 56)
    # Serve the app in the background so the server is ready before we open.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_auto_shutdown_watchdog, daemon=True).start()
    if hide_console:
        # Hidden mode cannot wait for console input: launch the UI immediately.
        try:
            webbrowser.open(url)
        except Exception:
            pass
        # CREATE_NO_WINDOW children are already hidden. This is only a
        # fallback for platforms/launchers where the relaunch was unavailable.
        if not hidden_child:
            _set_console_visible(False)
    else:
        # Normal mode keeps the familiar pancake prompt and waits for Enter.
        import random as _rnd
        prompt = _rnd.choice(ENTER_PROMPTS)
        line = "  " + (_GOLD + prompt + _RESET if use_color else prompt)
        try:
            input("\n" + line + "\n")
            webbrowser.open(url)
        except Exception:
            # No console input available (edge case) - just open the browser.
            try:
                webbrowser.open(url)
            except Exception:
                pass
    # Keep running until Ctrl+C, the console closes, or the web UI asks us to stop.
    try:
        _STOP_EVENT.wait()
    except KeyboardInterrupt:
        print("\n  Stopping Olo's TVMate. Bye!")
    finally:
        server.shutdown()

def _t_sleep(sec):
    import time as _t
    _t.sleep(sec)

def run_self_tests():
    """Fast, offline checks for the small pieces most likely to break updates."""
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
    check("version ordering", _parse_ver("0.777.b353") > _parse_ver("0.777.b352"))
    check("version equality", _parse_ver("v0.777.b353") == _parse_ver("0.777.b353"))
    check("sports event cache key normalizes teams",
          _sports_event_key("Leeds United", "Man Utd", "2026-08-12T20:30:00Z") ==
          _sports_event_key(" leeds united ", "MAN UTD", "2026-08-12T20:30:59Z"))
    profile_backup = create_profile_backup("profile", {"filter": "all"})
    check("profile backup omits Xtream credentials",
          _PROFILE_SECRET_KEYS.isdisjoint(profile_backup["config"]))
    check("profile backup retains favorites", isinstance(profile_backup["favorites"], dict))
    full_backup = create_profile_backup("full", {"filter": "all"})
    check("full backup includes Xtream credential fields",
          _PROFILE_SECRET_KEYS.issubset(full_backup["config"]))
    merged_test = _merge_favorite_lists(
        "teams", [{"team_id": "1", "name": "Old"}],
        [{"team_id": "1", "name": "Updated"}, {"team_id": "2", "name": "New"}])
    check("backup favorites merge and deduplicate",
          len(merged_test) == 2 and merged_test[0]["name"] == "Updated")
    current_test_cfg = dict(DEFAULT_CONFIG, profile_name="Current",
                            xtream_host="old.example")
    incoming_test_cfg = dict(DEFAULT_CONFIG, profile_name="Imported",
                             xtream_host="new.example")
    current_test_fav = {key: [] for key in _FAVORITE_LIST_KEYS}
    incoming_test_fav = {key: [] for key in _FAVORITE_LIST_KEYS}
    current_test_fav["channels"] = [{"stream_id": 7, "name": "Old channel"}]
    incoming_test_fav["channels"] = [{"stream_id": 8, "name": "New channel"}]
    restored_cfg_test, restored_fav_test = _prepare_backup_restore(
        "full", incoming_test_cfg, incoming_test_fav,
        current_test_cfg, current_test_fav)
    check("full backup replaces provider-bound favorites",
          restored_fav_test["channels"] == incoming_test_fav["channels"])
    check("full backup replaces configuration",
          restored_cfg_test["profile_name"] == "Imported" and
          restored_cfg_test["xtream_host"] == "new.example")
    try:
        _validated_backup_payload({"format": "olos-tvmate-backup",
                                   "format_version": 1.9,
                                   "backup_type": "full", "config": {},
                                   "favorites": {}})
        invalid_backup_rejected = False
    except ValueError:
        invalid_backup_rejected = True
    check("non-integer backup version rejected", invalid_backup_rejected)
    now = datetime.datetime(2026, 8, 11, tzinfo=datetime.timezone.utc)
    check("released movie included", _cinemeta_released_movie(
        {"released": "2026-08-10T00:00:00.000Z"}, now))
    check("future movie excluded", not _cinemeta_released_movie(
        {"released": "2026-08-12T00:00:00.000Z"}, now))
    check("undated current-year movie excluded", not _cinemeta_released_movie(
        {"releaseInfo": "2026"}, now))
    check("older movie included", _cinemeta_released_movie(
        {"releaseInfo": "2025"}, now))
    sample_channels = [
        {"name": "NO: TV 2 Sport 1", "stream_id": 1, "category_id": "no"},
        {"name": "LIVE | APOLLON LIMASSOL - BRANN | VGTV PPV 3",
         "stream_id": 2, "category_id": "ppv"},
        {"name": "LIVE | BRANN - HAMKAM | VGTV PPV 5",
         "stream_id": 3, "category_id": "ppv"},
        {"name": "BRANN 2", "stream_id": 4, "category_id": "ppv"},
        {"name": "NO: TV 2 PLAY | PPV 1", "stream_id": 5, "category_id": "ppv"},
        {"name": "Sky Sports 2 UHD", "stream_id": 6, "category_id": "4k"},
    ]
    sample_cats = {"no": "NO| NORWAY", "ppv": "NO| PPV EVENTS",
                   "4k": "4K | UHD CHANNELS"}
    platform_ids = {row["stream_id"] for row in match_channels(
        {"NO": ["TV 2 Play (NO)"]}, sample_channels, sample_cats, 0.49)}
    check("streaming platform candidates retained", platform_ids == {5})
    provider_rows = match_channels(
        {"NO": ["TV 2 Sport 1", "TV 2 Play (NO)"]},
        sample_channels, sample_cats, 0.49)
    provider_exact = {row["stream_id"]: row.get("provider_exact") for row in provider_rows}
    check("exact linear provider promoted", provider_exact.get(1) is True)
    check("streaming provider not promoted", provider_exact.get(5) is False)
    uk_4k = match_channels({"UK": ["Sky Sports 2"]},
                           sample_channels, sample_cats, 0.49)
    check("countryless 4k provider promoted",
          len(uk_4k) == 1 and uk_4k[0].get("provider_exact") is True)
    hong_kong_4k = match_channels(
        {"UK": ["Premier Sports 2"]},
        [{"name": "Hongkong NOW Premier Sports 2 4K", "stream_id": 98,
          "category_id": "4k"}], sample_cats, 0.49)
    check("written foreign country rejected inside global 4k category",
          hong_kong_4k == [])
    unknown_4k = match_channels(
        {"UK": ["Premier Sports 2"]},
        [{"name": "Premier Sports 2 4K", "stream_id": 97,
          "category_id": "4k"}], sample_cats, 0.49)
    check("global 4k category remains eligible", len(unknown_4k) == 1)
    caribbean_cartoon = match_channels(
        {"US": ["USA Network"]},
        [{"name": "AMP: CARTOON NETWORK", "stream_id": 96,
          "category_id": "cr"}],
        {"cr": "CR: carribean amp"}, 0.62)
    check("CR category rejected for US football broadcaster",
          caribbean_cartoon == [])
    global_cartoon = match_channels(
        {"US": ["USA Network"]},
        [{"name": "CARTOON NETWORK", "stream_id": 95,
          "category_id": "4k"}], sample_cats, 0.40)
    check("Cartoon Network excluded regardless of category",
          global_cartoon == [])
    non_football = match_channels(
        {"US": ["USA Network"]},
        [{"name": "US: MLB Networks", "stream_id": 99,
          "category_id": "us-sports"}],
        {"us-sports": "US | SPORTS"}, 0.40)
    check("other-sport networks excluded from football", non_football == [])
    class _TestXtream:
        @staticmethod
        def stream_url(stream_id):
            return "test:" + str(stream_id)
    sports_shared = _match_sports_fixture_channels(
        {"home": "Brann", "away": "HamKam", "start": "2026-08-12T20:00:00Z",
         "by_country": {"NO": ["TV 2 Sport 1"]}},
        {"match_threshold": 0.49}, sample_channels, sample_cats, _TestXtream())
    check("sports bulk matcher reuses shared catalogue",
          {row["stream_id"] for row in sports_shared["matches"]} == {1} and
          3 in {row["stream_id"] for row in sports_shared["ppv_hits"]})
    stored_sports = _sports_result_for_storage(sports_shared)
    check("sports disk cache omits credential-bearing URLs",
          all("url" not in row for key in ("matches", "ppv_hits")
              for row in stored_sports[key]))
    check("sports no-result state remains cacheable",
          _sports_result_for_storage({"logged_in": True,
              "availability_checked": True, "matches": [], "ppv_hits": []
          }).get("availability_checked") is True)
    class _TestRacingXtream:
        @staticmethod
        def stream_url(stream_id):
            return "test:" + str(stream_id)
    racing_rows = find_racing_channels(
        {"series": "f1", "race": "Dutch Grand Prix", "circuit": "Zandvoort"},
        [{"name": "F1 Dutch Grand Prix 4K", "stream_id": 20, "category_id": "4k"},
         {"name": "Sky Sports F1 UHD", "stream_id": 21, "category_id": "4k"},
         {"name": "F1 PPV 1", "stream_id": 22, "category_id": "ppv"}],
        sample_cats, _TestRacingXtream())
    racing_kinds = {row["stream_id"]: row.get("match_kind") for row in racing_rows}
    check("racing event promoted", racing_kinds.get(20) == "event")
    check("racing series second", racing_kinds.get(21) == "series")
    check("racing category fallback", racing_kinds.get(22) == "possible")
    event_ids = {row["stream_id"] for row in find_team_channels(
        ["Brann", "HamKam"], sample_channels, sample_cats, _TestXtream())}
    check("both fixture teams rank", 3 in event_ids)
    check("one-team event excluded", 2 not in event_ids)
    check("reserve team excluded", 4 not in event_ids)
    ranked = rank_fixture_channels([
        {"xtream_name": "VGTV PPV 1", "stream_id": 10, "score": 0.96},
        {"xtream_name": "APOLLON LIMASSOL - BRANN | VGTV PPV 3",
         "stream_id": 11, "score": 0.80},
        {"xtream_name": "APOLLON LIMASSOL - BRANN | VGTV PPV 4",
         "stream_id": 12, "score": 0.99}], "Brann", "HamKam")
    # Re-rank the same rows for the exact Apollon fixture separately.
    exact_ranked = rank_fixture_channels([
        {"xtream_name": "VGTV PPV 1", "stream_id": 10, "score": 0.96},
        {"xtream_name": "APOLLON LIMASSOL - BRANN | VGTV PPV 3",
         "stream_id": 11, "score": 0.80}], "Apollon Limassol", "Brann")
    check("exact fixture sorted first", exact_ranked[0]["stream_id"] == 11)
    check("generic PPV candidate retained", ranked[0]["fixture_match"] == "generic")
    check("wrong one-team event ranked last", ranked[-1]["fixture_match"] == "partial")
    check("embedded page version", "v" + VERSION in PAGE.replace("__VERSION__", VERSION))
    check("live fallback requires explicit finished state",
          "!f.is_finished&&mins!==null&&mins>=0&&mins<=240" in PAGE)
    return checks

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        passed = run_self_tests()
        print("Self-test passed: " + ", ".join(passed))
    else:
        main()
