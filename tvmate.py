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
VERSION = "0.777.b401"

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
    if "livesoccertv.com" in u: return "ltv"
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
    ("ltv",      "Channel listings (Live Soccer TV)"),
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
#   "***** VIP *****", "â–¶â–¶â–¶ MOVIES â—€â—€â—€", "===| ENGLISH |==="
_HEADER_CHARS = "#=*-_~â€¢Â·â–¶â—€â–ºâ—„â˜…â˜†|>< "
_HEADER_EDGE_RE = re.compile(r"^[\s#=*\-_~â€¢Â·â–¶â—€â–ºâ—„â˜…â˜†|<>]{2,}")

def _is_header_row(name):
    if not name:
        return True
    n = name.strip()
    # Starts (or ends) with a run of >=2 separator characters -> header.
    if _HEADER_EDGE_RE.match(n):
        return True
    if re.search(r"[\s#=*\-_~â€¢Â·â–¶â—€â–ºâ—„â˜…â˜†|<>]{2,}$", n):
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

_XT_CACHE = {"provider": "", "ts": 0, "channels": [], "cats": {},
             "sports_index": {}}
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
    _XT_CACHE.update({"provider": "", "ts": 0, "channels": [], "cats": {},
                      "sports_index": {}})
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


def _cache_busted_url(url, nonce=None):
    """Add a unique query value without discarding an existing URL query."""
    parts = urllib.parse.urlsplit(str(url or ""))
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("_tvmate", str(nonce if nonce is not None else time.time_ns())))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                   urllib.parse.urlencode(query), parts.fragment))

def _fetch_text(url, timeout=8, cache_bust=False):
    """Fetch a URL as text, or None on any failure (offline, 404, etc.)."""
    try:
        if cache_bust:
            url = _cache_busted_url(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": "OlosTVMate-Updater/" + VERSION,
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
            "Accept": "text/plain, */*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None

def _update_manifest():
    """Return the published version and optional SHA-256 from version.txt."""
    text = _fetch_text(UPDATE_VERSION_URL, cache_bust=True)
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
    text = _fetch_text(UPDATE_SCRIPT_URL, timeout=30, cache_bust=True)
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
    sports_index = _build_sports_channel_index(channels, cats)
    _XT_CACHE.update({"provider": provider, "ts": now, "channels": channels,
                      "cats": cats, "sports_index": sports_index})
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
LTV_DAILY_SCHEDULE = "https://www.livesoccertv.com/schedules/{date}/"
FOTMOB_FALLBACK_COUNTRIES = ("no", "gb", "us", "pt", "ie", "es", "de",
                             "it", "fr", "nl", "be", "dk", "se")

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
_LTV_TTL = 26 * 3600    # daily listing; date-keyed cache survives the whole day
_LTV_CACHE = {}         # date -> {ts, rows}; FotMob remains the fixture source
_LTV_CACHE_LOCK = threading.Lock()
_LTV_DATE_LOCKS = {}
_LTV_MATCH_CACHE = {}   # match URL -> complete international broadcaster map
_LTV_MATCH_LOCKS = {}
_LTV_MATCH_FAILURES = {}
_LTV_MATCH_FAILURE_TTL = 15 * 60
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
                     "all_day": False, "date_text": clean(match.group(1)) + " Â· " + clean(match.group(2)),
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
                     "date_text": f"{start_text} â€“ {end_text} {year}",
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
        # <time datetime="2026-08-27T09:00:00-03:00">27 â€“ 30 August 2026</time>.
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
            range_match = re.search(r'(\d{1,2})\s*[â€“â€”-]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})',
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
        if status.get("cancelled"):
            continue
        home = str(home_obj.get("name") or "")
        away = str(away_obj.get("name") or "")
        hay = (home + " " + away).lower()
        if not any(value in hay for value in wanted):
            continue
        start = str(status.get("utcTime") or match.get("startDate") or "")
        is_finished = bool(status.get("finished"))
        is_live = bool((status.get("started") or status.get("ongoing") or
                        status.get("live")) and not is_finished)
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
                    "is_finished": is_finished,
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

def _plain_html(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ",
                  str(value or "")))).strip()

def _ltv_country(fragment):
    """Best-effort country metadata carried beside an LTV channel link."""
    low = str(fragment or "").lower()
    for pattern in (r'data-country=["\']([^"\']+)',
                    r'(?:flag[-_/]|/flags?/)([a-z]{2,3})(?:\.|[-_/"\'])'):
        match = re.search(pattern, low)
        if match:
            raw = match.group(1)
            code = _COUNTRY_NAME_ALIASES.get(raw, raw)
            return _display_cc(_norm_cc(code))
    return "LTV"

def _ltv_listing_country(attrs, name):
    """Use link metadata first, then a country written in the listing name."""
    country = _ltv_country(attrs)
    if country == "LTV":
        named_cc = _cc_from_name(name)
        if named_cc:
            country = _display_cc(named_cc)
    return country

def _ltv_match_url(attrs):
    match = re.search(r'href=["\']([^"\']*/match/[^"\']*)', str(attrs or ""), re.I)
    if not match:
        return ""
    path = html.unescape(match.group(1)).strip()
    return urllib.parse.urljoin("https://www.livesoccertv.com/", path)

def _parse_ltv_match_listings(page):
    """Parse the country table on one LTV match page."""
    text = str(page or "")
    marker = re.search(r'International\s+(?:TV|Coverage)', text, re.I)
    if marker:
        text = text[marker.end():]
    end = re.search(r'(?:Content disclaimer|Match Details|Head to Head)', text, re.I)
    if end:
        text = text[:end.start()]
    by_country = {}
    for row in re.finditer(r'<tr\b[^>]*>(.*?)</tr>', text, re.I | re.S):
        cells = re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row.group(1), re.I | re.S)
        if len(cells) < 2:
            continue
        cc = _cc_from_name(_plain_html(cells[0]))
        if not cc:
            continue
        display = _display_cc(cc)
        for link in re.finditer(r'<a\b([^>]*)>(.*?)</a>', " ".join(cells[1:]), re.I | re.S):
            name = _plain_html(link.group(2))
            if not name or name == "â€¦":
                continue
            current = by_country.setdefault(display, [])
            if name not in current:
                current.append(name)
    return by_country

def fetch_ltv_match_listings(match_url):
    """Fetch and cache one complete international match listing."""
    url = str(match_url or "").strip()
    if not re.match(r'^https://(?:www\.)?livesoccertv\.com/match/', url, re.I):
        return {}
    now = time.time()
    with _LTV_CACHE_LOCK:
        cached = _LTV_MATCH_CACHE.get(url)
        failed = _LTV_MATCH_FAILURES.get(url)
        match_lock = _LTV_MATCH_LOCKS.setdefault(url, threading.Lock())
    if isinstance(cached, dict):
        return cached
    if failed and now - float(failed.get("ts") or 0) < _LTV_MATCH_FAILURE_TTL:
        raise RuntimeError(str(failed.get("error") or "temporarily unavailable"))
    with match_lock:
        with _LTV_CACHE_LOCK:
            cached = _LTV_MATCH_CACHE.get(url)
            failed = _LTV_MATCH_FAILURES.get(url)
        if isinstance(cached, dict):
            return cached
        if failed and time.time() - float(failed.get("ts") or 0) < _LTV_MATCH_FAILURE_TTL:
            raise RuntimeError(str(failed.get("error") or "temporarily unavailable"))
        cache_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        disk = _load_timed_data_cache(f"ltv-match-v2-{cache_id}.json", _LTV_TTL)
        if isinstance(disk, dict) and disk:
            with _LTV_CACHE_LOCK:
                _LTV_MATCH_CACHE[url] = disk
                _LTV_MATCH_FAILURES.pop(url, None)
            return disk
        try:
            rows = _parse_ltv_match_listings(http_get_text(url, timeout=15))
            if not rows:
                raise RuntimeError("returned no international match listings")
        except Exception as exc:
            message = f"match detail unavailable: {exc}"
            with _LTV_CACHE_LOCK:
                _LTV_MATCH_FAILURES[url] = {"ts": time.time(), "error": message}
            raise RuntimeError(message) from exc
        with _LTV_CACHE_LOCK:
            _LTV_MATCH_CACHE[url] = rows
            _LTV_MATCH_FAILURES.pop(url, None)
        _save_timed_data_cache(f"ltv-match-v2-{cache_id}.json", rows)
        return rows

def _parse_ltv_daily(page, date):
    """Parse LTV's public daily table. It enriches existing FotMob rows only."""
    rows = []
    for match in re.finditer(r'<tr\b[^>]*class=["\'][^"\']*matchrow[^"\']*["\'][^>]*>(.*?)</tr>',
                             page or "", re.I | re.S):
        body = match.group(1)
        game = ""
        game_link = re.search(r'<a\b[^>]*(?:/match/|class=["\'][^"\']*(?:match|game))[^>]*>(.*?)</a>',
                              body, re.I | re.S)
        match_url = ""
        if game_link:
            game = _plain_html(game_link.group(1))
            match_url = _ltv_match_url(game_link.group(0))
        if not game:
            anchors = re.findall(r'<a\b[^>]*>(.*?)</a>', body, re.I | re.S)
            game = next((_plain_html(a) for a in anchors
                         if re.search(r'\s(?:vs?\.?|â€“|â€”|-)\s', _plain_html(a), re.I)), "")
        teams = re.split(r'\s+(?:vs?\.?|â€“|â€”)\s+', game, maxsplit=1, flags=re.I)
        if len(teams) != 2:
            continue
        channel_area = re.search(r'<[^>]+id=["\']channels["\'][^>]*>(.*)', body, re.I | re.S)
        area = channel_area.group(1) if channel_area else body
        by_country = {}
        for link in re.finditer(r'<a\b([^>]*)>(.*?)</a>', area, re.I | re.S):
            name = _plain_html(link.group(2))
            attrs = link.group(1)
            if not name or name == "â€¦" or "/match/" in attrs.lower():
                continue
            cc = _ltv_listing_country(attrs, name)
            by_country.setdefault(cc, [])
            if name not in by_country[cc]:
                by_country[cc].append(name)
        if by_country:
            rows.append({"home": teams[0].strip(), "away": teams[1].strip(),
                         "start": str(date), "by_country": by_country,
                         "match_url": match_url})
    if rows:
        return rows
    # Current LTV pages render match cards/rows without the legacy `matchrow`
    # class. Use each /match/ anchor as the boundary and collect only the
    # channel links that follow it before the next match starts.
    anchors = list(re.finditer(
        r'<a\b([^>]*href=["\'][^"\']*/match/[^"\']*["\'][^>]*)>(.*?)</a>',
        page or "", re.I | re.S))
    for index, match in enumerate(anchors):
        game = _plain_html(match.group(2))
        match_url = _ltv_match_url(match.group(1))
        teams = re.split(r'\s+(?:vs?\.?|â€“|â€”)\s+', game, maxsplit=1, flags=re.I)
        if len(teams) != 2:
            continue
        end = anchors[index + 1].start() if index + 1 < len(anchors) else min(
            len(page or ""), match.end() + 30000)
        area = (page or "")[match.end():end]
        by_country = {}
        for link in re.finditer(r'<a\b([^>]*)>(.*?)</a>', area, re.I | re.S):
            name = _plain_html(link.group(2))
            attrs = link.group(1)
            attrs_low = attrs.lower()
            if ("/channels/" not in attrs_low and "data-country=" not in attrs_low):
                continue
            if (not name or name == "â€¦" or "/match/" in attrs_low or
                    re.search(r'\b(?:details?|preview|lineups?|tickets?)\b', name, re.I)):
                continue
            cc = _ltv_listing_country(attrs, name)
            by_country.setdefault(cc, [])
            if name not in by_country[cc]:
                by_country[cc].append(name)
        if by_country:
            rows.append({"home": teams[0].strip(), "away": teams[1].strip(),
                         "start": str(date), "by_country": by_country,
                         "match_url": match_url})
    return rows

def fetch_ltv_daily(date):
    """Fetch at most one LTV schedule per date and reuse it through that day."""
    date = str(date or "")[:10]
    now = time.time()
    with _LTV_CACHE_LOCK:
        cached = _LTV_CACHE.get(date)
        date_lock = _LTV_DATE_LOCKS.setdefault(date, threading.Lock())
    if cached:
        return cached["rows"]
    # Search/profile requests can overlap in the threaded local server. Only
    # one of them may download a particular day's guide; the rest reuse it.
    with date_lock:
        with _LTV_CACHE_LOCK:
            cached = _LTV_CACHE.get(date)
        if cached:
            return cached["rows"]
        disk = _load_timed_data_cache(f"ltv-daily-v4-{date}.json", _LTV_TTL)
        if isinstance(disk, list) and disk:
            with _LTV_CACHE_LOCK:
                _LTV_CACHE[date] = {"ts": now, "rows": disk}
            return disk
        page = http_get_text(LTV_DAILY_SCHEDULE.format(date=date), timeout=15)
        rows = _parse_ltv_daily(page, date)
        if not rows:
            raise RuntimeError("Live Soccer TV returned no readable listings")
        with _LTV_CACHE_LOCK:
            _LTV_CACHE[date] = {"ts": now, "rows": rows}
        _save_timed_data_cache(f"ltv-daily-v4-{date}.json", rows)
        return rows

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
    ["hearts", "heart of midlothian"],
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
    ["nottingham forest", "nottm forest", "nott'm forest", "nottm. forest",
     "notts forest"],
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

def _fetch_country_guides(countries, max_workers=6):
    """Fetch regional guides using modules available in the bundled runtime."""
    seen, normalized = set(), []
    for value in countries or []:
        country = _norm_cc(value)
        if country and country not in seen:
            seen.add(country)
            normalized.append(country)
    if not normalized:
        return [], []
    results = [None] * len(normalized)
    next_index = [0]
    index_lock = threading.Lock()
    def worker():
        while True:
            with index_lock:
                if next_index[0] >= len(normalized):
                    return
                index = next_index[0]
                next_index[0] += 1
            country = normalized[index]
            try:
                results[index] = (country, fetch_country_fixtures(country), None)
            except Exception as exc:
                results[index] = (country, None, exc)
    workers = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(len(normalized), max(1, int(max_workers or 1))))]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    rows, errors = [], []
    for country, fixtures, error in results:
        if error is None:
            rows.append((country, fixtures))
        else:
            errors.append(f"{_display_cc(country)}: {error}")
    return rows, errors

def search_fixtures(term, countries):
    term_l = term.lower().strip()
    want = _expand_terms(term_l)
    merged, errors = {}, []
    guides, errors = _fetch_country_guides(countries)
    for country, fx in guides:
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
    guides, errors = _fetch_country_guides(countries)
    for _country, rows in guides:
        tv_rows.extend(rows)
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

def _nearby_ltv_dates(fixtures, today=None, limit=8, horizon_days=14):
    """Choose a bounded set of nearby fixture dates for daily LTV guides."""
    all_dates = sorted({str(row.get("start") or "")[:10] for row in fixtures
                        if str(row.get("start") or "")[:10]})
    today = str(today or datetime.date.today().isoformat())[:10]
    today_date = datetime.date.fromisoformat(today)
    nearby = []
    for day in all_dates:
        try:
            delta = (datetime.date.fromisoformat(day) - today_date).days
        except ValueError:
            continue
        if 0 <= delta <= horizon_days:
            nearby.append(day)
    return nearby[:limit]

def add_primary_tv_listings(fixtures, countries):
    """Use LTV for channels only; fall back to FotMob per missing fixture."""
    errors, ltv_rows, failed_dates = [], [], set()
    today = datetime.date.today().isoformat()
    # Cover the nearby fixture cards, not merely the first two dates. A cup or
    # friendly between league games must not prevent the next televised league
    # fixture from receiving its listings. Eight date guides inside two weeks
    # remains bounded while covering teams with dense friendly/cup schedules.
    dates = _nearby_ltv_dates(fixtures, today)
    # The guides are independent. Download uncached dates concurrently so four
    # nearby fixtures cost one network timeout rather than four in sequence.
    results, failures = {}, {}
    def load_day(day):
        try:
            results[day] = fetch_ltv_daily(day)
        except Exception as exc:
            failures[day] = exc
    workers = [threading.Thread(target=load_day, args=(day,), daemon=True)
               for day in dates]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    for day in dates:
        if day in results:
            ltv_rows.extend(results[day])
        elif day in failures:
            failed_dates.add(day)
            errors.append("Live Soccer TV channel listings unavailable â€” "
                          f"using FotMob channel listings ({failures[day]})")
    missing, matched = [], []
    for fixture in fixtures:
        fday = str(fixture.get("start") or "")[:10]
        found = None
        for row in ltv_rows:
            if fday != str(row.get("start") or "")[:10]:
                continue
            if (_team_names_equivalent(fixture.get("home"), row.get("home")) and
                    _team_names_equivalent(fixture.get("away"), row.get("away"))):
                found = row
                break
        if found:
            matched.append((fixture, found))
        else:
            missing.append(fixture)
    # Fetch only the relevant match pages, never every match in a daily guide.
    # Bound the batch and run it concurrently so several favorite fixtures cost
    # one timeout. Remaining fixtures retain their useful daily listing.
    detail_urls = list(dict.fromkeys(
        found.get("match_url") for _fixture, found in matched
        if found.get("match_url")))[:10]
    detail_results, detail_failures = {}, {}
    def load_detail(url):
        try:
            detail_results[url] = fetch_ltv_match_listings(url)
        except Exception as exc:
            detail_failures[url] = exc
    detail_workers = [threading.Thread(target=load_detail, args=(url,), daemon=True)
                      for url in detail_urls]
    for worker in detail_workers:
        worker.start()
    for worker in detail_workers:
        worker.join()
    for url in detail_urls:
        if url in detail_failures:
            errors.append(f"Live Soccer TV match listings unavailable ({detail_failures[url]})")
    for fixture, found in matched:
        detailed = detail_results.get(found.get("match_url")) or {}
        fixture["by_country"] = dict(detailed or found.get("by_country") or {})
        fixture["listing_source"] = "LTV"
    if missing:
        fallback_errors = add_tv_listings(missing, countries)
        errors.extend(fallback_errors)
        for fixture in missing:
            if fixture.get("by_country"):
                fixture["listing_source"] = "FotMob fallback"
    return errors

def _overlay_fixture_rows(fixtures, overlay_rows, append_missing=False):
    """Overlay status/listing data without allowing it to remove fixtures."""
    for overlay in overlay_rows or []:
        oday = str(overlay.get("start") or "")[:10]
        duplicate = None
        for fixture in fixtures:
            if oday and str(fixture.get("start") or "")[:10] != oday:
                continue
            if (_team_names_equivalent(fixture.get("home"), overlay.get("home")) and
                    _team_names_equivalent(fixture.get("away"), overlay.get("away"))):
                duplicate = fixture
                break
        if duplicate is None:
            if append_missing:
                fixtures.append(dict(overlay))
            continue
        for key in ("home_id", "away_id", "league_name", "league_id"):
            duplicate[key] = duplicate.get(key) or overlay.get(key, "")
        if overlay.get("by_country"):
            target = duplicate.setdefault("by_country", {})
            for country, names in overlay.get("by_country", {}).items():
                current = target.setdefault(country, [])
                for name in names or []:
                    if name not in current:
                        current.append(name)
        if overlay.get("status_known") or "is_live" in overlay:
            duplicate["is_live"] = bool(overlay.get("is_live"))
            duplicate["is_finished"] = bool(overlay.get("is_finished"))
            duplicate["live_minute"] = overlay.get("live_minute")
            duplicate["status_known"] = bool(overlay.get("status_known", True))
    return fixtures

def _current_and_upcoming_fixtures(fixtures, now=None):
    """Exclude completed historical rows while retaining live/recent fixtures."""
    now = float(now if now is not None else time.time())
    kept = []
    for fixture in fixtures:
        if fixture.get("is_live"):
            kept.append(fixture)
            continue
        try:
            kickoff = datetime.datetime.fromisoformat(str(
                fixture.get("start") or "").replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if kickoff >= now - 6 * 3600:
            kept.append(fixture)
    return kept

def complete_team_fixtures(term, team_id, countries):
    """Return FotMob's team schedule; listing sources only enrich it."""
    errors = []
    if not team_id:
        try:
            team_id = resolve_fotmob_team_id(term)
        except Exception as e:
            errors.append(f"FotMob team lookup: {e}")
            team_id = ""
    try:
        fixtures = fetch_team_schedule(team_id, term) if team_id else []
    except Exception as e:
        errors.append(f"{term}: {e}")
        fixtures = []
    fixtures = [dict(row) for row in fixtures]
    try:
        _overlay_fixture_rows(fixtures, search_daily_matches(term),
                              append_missing=True)
    except Exception as e:
        errors.append(f"{term} live status: {e}")
    for fixture in fixtures:
        fixture.setdefault("by_country", {})
    fixtures = _current_and_upcoming_fixtures(fixtures)
    errors.extend(add_primary_tv_listings(fixtures, countries))
    fixtures.sort(key=lambda row: row.get("start") or "")
    return fixtures, errors, str(team_id or "")

# --------------------------------------------------------------------------
# Fuzzy channel matching (handles "COUNTRY: Channel Name HD")
# --------------------------------------------------------------------------

_QUALITY_RE = re.compile(r"\b(4k|uhd|fhd|hd|sd)\b", re.I)
_CHANNEL_TECH_RE = re.compile(
    r"\b(raw|hevc|h\.?26[45]|x26[45]|avc|mpeg[24]?|hdr10?|sdr)\b", re.I)
_NOISE_RE = re.compile(r"[^a-z0-9 ]+")
# strip a leading provider tag like "GOLD: ", "SPO: ", "NO| ", "VIP - "
_PREFIX_RE = re.compile(r"^\s*[a-z0-9]{1,5}\s*[:|\-]\s*", re.I)
_PAREN_CC_RE = re.compile(r"\s*\((?:no|uk|us|usa|espanol|espa\w*|[a-z]{2,3})\)\s*$", re.I)
_HASH_RE = re.compile(r"#+")                 # "###### SPORT ######"
_FPS_RE = re.compile(r"\b\d{2,3}\s*fps\b", re.I)

# Words that carry no identifying power on their own.
_GENERIC = {"sport", "sports", "tv", "play", "channel", "the", "hd", "sd",
            "fhd", "uhd", "4k", "raw", "vip", "gold", "ultra", "premium",
            "fps", "dolby", "audio", "live", "1", "one"}

def normalise(name):
    n = name.lower()
    n = _HASH_RE.sub(" ", n)          # remove ### decoration
    n = _PREFIX_RE.sub("", n)         # drop leading provider tag
    n = _PAREN_CC_RE.sub("", n)       # drop trailing "(NO)"
    n = _FPS_RE.sub(" ", n)           # drop "50fps"/"60fps"
    n = _QUALITY_RE.sub(" ", n)       # ignore quality words
    n = _CHANNEL_TECH_RE.sub(" ", n)  # ignore codec/source variants
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
    n = _CHANNEL_TECH_RE.sub(" ", n)
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

# FotMob sometimes reports access-package names rather than one fixed channel.
# Providers commonly expose the package's simultaneous event feeds as numbered
# channels (for example ESPN Unlimited 1..40). Match the complete package stem,
# never one generic word such as "ESPN", "Select", or "Unlimited" by itself.
_NUMBERED_FEED_PACKAGES = {"espn select", "espn unlimited"}

# A shared word such as "Network" or "Sports" must never make a channel for
# another sport a football broadcaster candidate.
_NON_FOOTBALL_CHANNEL_RE = re.compile(
    r"(?<![a-z0-9])(mlb|nfl|nba|nhl|baseball|basketball|ice hockey|cricket|"
    r"horse racing|motor ?sports?|auto motor|cartoon network|nickelodeon|"
    r"disney channel|disney junior|boomerang)(?![a-z0-9])",
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
    "austria": "at", "austrian": "at", "switzerland": "ch", "swiss": "ch",
    "poland": "pl", "polish": "pl", "czech republic": "cz", "czechia": "cz",
    "slovakia": "sk", "slovak": "sk", "hungary": "hu", "hungarian": "hu",
    "romania": "ro", "romanian": "ro", "bulgaria": "bg", "bulgarian": "bg",
    "greece": "gr", "greek": "gr", "croatia": "hr", "croatian": "hr",
    "slovenia": "si", "slovenian": "si", "serbia": "rs", "serbian": "rs",
    "bosnia and herzegovina": "ba", "bosnia": "ba", "montenegro": "me",
    "north macedonia": "mk", "macedonia": "mk", "albania": "al",
    "turkey": "tr", "turkiye": "tr", "russia": "ru", "ukraine": "ua",
    "argentina": "ar", "argentinian": "ar", "saudi arabia": "sa",
    "united arab emirates": "ae", "qatar": "qa", "israel": "il",
    "new zealand": "nz", "south africa": "za", "japan": "jp",
    "south korea": "kr", "korea republic": "kr", "china": "cn",
    "chile": "cl", "colombia": "co", "peru": "pe", "uruguay": "uy",
    "paraguay": "py", "bolivia": "bo", "venezuela": "ve",
    "costa rica": "cr", "puerto rico": "pr", "panama": "pa",
    "lithuania": "lt", "latvia": "lv", "estonia": "ee", "iceland": "is",
    "luxembourg": "lu", "malta": "mt", "cyprus": "cy",
    "hong kong": "hk", "hongkong": "hk",
    "singapore": "sg", "malaysia": "my", "indonesia": "id",
    "philippines": "ph", "thailand": "th", "vietnam": "vn",
}
_COUNTRY_CODES.update({"hk", "sg", "my", "id", "ph", "th", "vn", "me",
                       "ae", "qa", "il", "nz", "za", "jp", "kr", "cn",
                       "cl", "co", "pe", "uy", "py", "bo", "ve", "pr", "pa"})

# IPTV providers frequently use three-letter or provider-specific country
# prefixes. Canonicalise only known country labels; tier names such as VIP,
# VO, GOLD and 4K deliberately remain unknown and therefore eligible.
_COUNTRY_CODE_ALIASES = {
    "nor": "no", "norge": "no", "dnk": "dk", "den": "dk",
    "swe": "se", "fin": "fi", "gbr": "gb", "eng": "gb",
    "irl": "ie", "prt": "pt", "por": "pt", "ptg": "pt",
    "esp": "es", "spa": "es", "deu": "de", "ger": "de",
    "fra": "fr", "ita": "it", "nld": "nl", "ned": "nl",
    "bel": "be", "che": "ch", "sui": "ch", "aut": "at",
    "pol": "pl", "cze": "cz", "svk": "sk", "hun": "hu",
    "rou": "ro", "rom": "ro", "bgr": "bg", "gre": "gr",
    "grc": "gr", "hrv": "hr", "srb": "rs", "bih": "ba",
    "mkd": "mk", "alb": "al", "tur": "tr", "rus": "ru",
    "ukr": "ua", "ltu": "lt", "lva": "lv", "est": "ee",
    "isl": "is", "lux": "lu", "mlt": "mt", "cyp": "cy",
    "usa": "us", "can": "ca", "aus": "au", "bra": "br",
    "mex": "mx", "arg": "ar", "ind": "in", "pak": "pk",
    "hkg": "hk", "sgp": "sg", "mys": "my", "idn": "id",
    "phl": "ph", "tha": "th", "vnm": "vn",
}

def _canonical_cc(code):
    code = str(code or "").strip().lower()
    return _COUNTRY_CODE_ALIASES.get(code, code)

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
    raw = m.group(1).lower()
    code = _canonical_cc(raw)
    return code if raw in _COUNTRY_CODE_ALIASES or code in _COUNTRY_CODES else None

def _cc_from_name(text):
    """Recognise an explicitly written country/region in a provider label."""
    value = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    if not value:
        return None
    for alias, code in sorted(_COUNTRY_NAME_ALIASES.items(),
                              key=lambda item: len(item[0]), reverse=True):
        if re.search(r"(?<![a-z0-9])" + re.escape(alias) +
                     r"(?![a-z0-9])", value):
            return _canonical_cc(code)
    return None

def _resolve_channel_country(name, category):
    """Determine a channel's country. Prefer the CATEGORY prefix (e.g.
    'NO| NORWAY HD/RAW') since providers group channels by country there and
    it's far more consistent than the name prefix. Fall back to the name
    prefix ('NO:'), else None (unknown -> not country-filtered)."""
    return (_cc_from_prefix(category) or _cc_from_name(category) or
            _cc_from_prefix(name) or _cc_from_name(name))

def _strip_bare_channel_country(name, resolved_cc=None):
    """Normalize bare IPTV country prefixes such as ``NO V SPORT 1``."""
    value = normalise(str(name or ""))
    if " " not in value:
        return value, resolved_cc
    first, rest = value.split(" ", 1)
    inferred = None
    canonical = _canonical_cc(first)
    if first in _COUNTRY_CODE_ALIASES or canonical in _COUNTRY_CODES:
        inferred = canonical
    elif (first in _COUNTRY_NAME_ALIASES and
          re.match(r"^v\s+sport(?:\s|$)", rest)):
        inferred = _canonical_cc(_COUNTRY_NAME_ALIASES[first])
    if inferred and (resolved_cc is None or inferred == resolved_cc):
        return rest.strip(), resolved_cc or inferred
    return value, resolved_cc

def _normalise_channel_country_labels(name, resolved_cc=None):
    """Remove matching country labels around provider quality/noise suffixes."""
    value, resolved_cc = _strip_bare_channel_country(name, resolved_cc)
    value = re.sub(r"\s+raw$", "", value).strip()
    suffixes = {
        "no": ("no", "nor", "norway", "norge", "norwegian"),
        "se": ("se", "swe", "sweden", "swedish"),
        "dk": ("dk", "dnk", "den", "denmark", "danish"),
        "fi": ("fi", "fin", "finland", "finnish"),
    }.get(_canonical_cc(resolved_cc), ())
    if not resolved_cc:
        inferred = re.search(
            r"\s+(no|nor|norway|norge|norwegian)$", value)
        if inferred:
            resolved_cc = "no"
            suffixes = ("no", "nor", "norway", "norge", "norwegian")
    for suffix in suffixes:
        value = re.sub(r"\s+" + re.escape(suffix) + r"$", "", value).strip()
    value = re.sub(r"\s+raw$", "", value).strip()
    return value, resolved_cc

def _build_sports_channel_index(channels, cats):
    """Prepare reusable normalized names and token lookups for sports matching."""
    token_index, event_token_index, compact_index = {}, {}, {}
    compact_values = []
    for index, channel in enumerate(channels):
        name = str(channel.get("name") or "")
        match_name = normalise(name)
        event_name = normalise_event_name(name)
        category = cats.get(channel.get("category_id"), "")
        compact = re.sub(r"\s+", "", match_name)
        channel["_match_norm"] = match_name
        channel["_event_norm"] = event_name
        channel["_match_category"] = category
        channel["_match_tokens"] = frozenset(match_name.split())
        channel["_event_tokens"] = frozenset(event_name.split())
        channel["_match_compact"] = compact
        compact_values.append(compact)
        if compact:
            compact_index.setdefault(compact, set()).add(index)
        for token in channel["_match_tokens"]:
            token_index.setdefault(token, set()).add(index)
        for token in channel["_event_tokens"]:
            event_token_index.setdefault(token, set()).add(index)
    return {"channels": channels, "tokens": token_index,
            "event_tokens": event_token_index, "compact": compact_index,
            "compact_values": compact_values}

def _sports_channel_index(channels, cats):
    cached = _XT_CACHE.get("sports_index") or {}
    if cached.get("channels") is channels:
        return cached
    return _build_sports_channel_index(channels, cats)

def _viaplay_listing(name, country):
    """Accept the common country spellings used by TV-guide providers."""
    value = normalise(str(name or ""))
    aliases = {
        "NO": {"no", "nor", "norway", "norge", "norwegian"},
        "SE": {"se", "swe", "sweden", "swedish"},
        "DK": {"dk", "dnk", "denmark", "danish"},
        "FI": {"fi", "fin", "finland", "finnish"},
    }.get(str(country or "").upper(), set())
    return value == "viaplay" or value in {"viaplay " + alias for alias in aliases}

def _sports_fixture_channel_shortlist(fixture, channels, cats):
    """Use cheap indexed terms to avoid fuzzy-scoring the full IPTV catalogue."""
    if not channels:
        return []
    index = _sports_channel_index(channels, cats)
    selected = set()
    broadcaster_compacts = set()
    for country, names in (fixture.get("by_country") or {}).items():
        suffixes = {"NO": ("norway", "norge", "norwegian"),
                    "SE": ("sweden", "swedish"),
                    "DK": ("denmark", "danish"),
                    "FI": ("finland", "finnish")}.get(str(country).upper(), ())
        for name in names or []:
            normalized = normalise(str(name or ""))
            for suffix in suffixes:
                normalized = re.sub(r"\s+" + re.escape(suffix) + r"$", "", normalized)
            compact = re.sub(r"\s+", "", normalized)
            if compact:
                broadcaster_compacts.add(compact)
                selected.update(index["compact"].get(compact, ()))
            for token in _distinctive(normalized.split()):
                selected.update(index["tokens"].get(token, ()))
            if _viaplay_listing(normalized, country):
                selected.update(index["tokens"].get("sport", ()))
    if broadcaster_compacts:
        for channel_index, compact in enumerate(index["compact_values"]):
            if any(source in compact for source in broadcaster_compacts):
                selected.add(channel_index)
    for team in (fixture.get("home"), fixture.get("away")):
        for alias in _expand_terms(str(team or "").lower().strip()):
            normalized = normalise(alias)
            terms = _distinctive(normalized.split()) or [
                token for token in normalized.split() if len(token) >= 3]
            for token in terms:
                selected.update(index["event_tokens"].get(token, ()))
    league = normalise_event_name(fixture.get("league_name") or "")
    for token in _distinctive(league.split()):
        if len(token) >= 4:
            selected.update(index["event_tokens"].get(token, ()))
    if not selected:
        return channels
    return [channels[channel_index] for channel_index in sorted(selected)]

def _channel_catalog_search_rank(name, category, term):
    """Return a stable relevance key for Playlist Builder channel search.

    Search channel names as users see them, while ignoring provider prefixes
    and quality labels. Norwegian matches come first, then exact/leading
    phrases beat loose token matches. ``None`` means that the channel does not
    match the query.
    """
    query = normalise_event_name(term)
    if not query:
        return (0, 0, 0, str(name or "").lower())
    channel_cc = _resolve_channel_country(name, category)
    candidate, channel_cc = _normalise_channel_country_labels(name, channel_cc)
    if not candidate:
        return None
    query_tokens = query.split()
    candidate_tokens = candidate.split()
    query_compact = "".join(query_tokens)
    candidate_compact = "".join(candidate_tokens)
    if candidate == query:
        tier, position = 0, 0
    elif candidate.startswith(query + " "):
        tier, position = 1, 0
    else:
        phrase = re.search(r"(?<![a-z0-9])" + re.escape(query) +
                           r"(?![a-z0-9])", candidate)
        if phrase:
            tier, position = 2, phrase.start()
        elif query_compact and query_compact in candidate_compact:
            tier, position = 3, candidate_compact.find(query_compact)
        elif all(any(token.startswith(w) for token in candidate_tokens)
                 for w in query_tokens):
            tier, position = 4, 0
        else:
            return None
    country_rank = 0 if channel_cc == "no" else (1 if channel_cc is None else 2)
    return (country_rank, tier, position, len(candidate), str(name or "").lower())

def _viaplay_norway_linear_feed(name):
    """True for the Norwegian linear feeds represented by Viaplay Norway.

    Include V Sport, numbered V Sport, V Sport Live and the dedicated Premier
    League feeds. Do not pull in Golf, Motor or unrelated V Sport channels.
    """
    value = re.sub(r"(?<![a-z0-9])ultra(?![a-z0-9])", " ", normalise(name))
    value = re.sub(
        r"\b(vip|gold|raw|dolby|audio|backup|feed|no|nor|norway|norge|norwegian)\b",
        " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return bool(re.fullmatch(
        r"v sport(?: (?:live|premier league|prem league|epl|pl))?"
        r"(?: [1-9]\d*)?", value))

def _norway_premier_league_feed(name):
    """Recognize the Norwegian V Sport PL family regardless of suffix noise."""
    value = re.sub(r"\s+", " ", normalise(name)).strip()
    return bool(re.search(
        r"(?<![a-z0-9])v sport(?: premier league| prem league| epl| pl)"
        r"(?: [1-9]\d*)?(?![a-z0-9])", value))

def _viaplay_finland_linear_feed(name):
    """Finnish V Sport feeds that can carry Viaplay football/events."""
    value = re.sub(r"(?<![a-z0-9])ultra(?![a-z0-9])", " ", normalise(name))
    value = re.sub(r"\s+", " ", value).strip()
    return bool(re.fullmatch(
        r"v sport(?: (?:live|football|premium))?(?: [1-9]\d*)?(?: suomi)?",
        value))

def match_channels(by_country, xtream_channels, cats, threshold, league_name=""):
    """`by_country`: {COUNTRY: [broadcaster names]}. A channel is only eligible
    to match a broadcaster from country C if the channel's own country prefix
    is not a *different* recognised country."""
    # Build (broadcaster, country, normtokens) list.
    srcs = []
    for country, names in (by_country or {}).items():
        for s in names:
            source_country = country.upper()
            inferred_country = _cc_from_name(s)
            if source_country == "LTV" and inferred_country:
                source_country = _display_cc(inferred_country)
            canonical_country = _canonical_cc(source_country)
            allowed = (None if source_country == "LTV" else
                       {_canonical_cc(code) for code in
                        _COUNTRY_MATCH.get(source_country, {canonical_country})})
            ns, _source_cc = _normalise_channel_country_labels(
                s, inferred_country or (canonical_country if allowed is not None else None))
            # LTV often appends the already-separate country to a linear feed
            # ("V Sport 1 Norway"). IPTV catalogues normally express it in the
            # category/prefix instead ("NO: V Sport 1"). Compare channel names
            # without that redundant suffix so the linear feed is exact.
            suffixes = {"NO": ("norway", "norge", "norwegian"),
                        "SE": ("sweden", "swedish"),
                        "DK": ("denmark", "danish"),
                        "FI": ("finland", "finnish")}.get(source_country, ())
            for suffix in suffixes:
                ns = re.sub(r"\s+" + re.escape(suffix) + r"$", "", ns)
            toks = set(ns.split())
            if toks:
                srcs.append((s, source_country, allowed, ns, toks))
    rows = []
    for ch in xtream_channels:
        cname = ch["name"]
        category = ch.get("_match_category", cats.get(ch["category_id"], ""))
        if (_NON_FOOTBALL_CHANNEL_RE.search(cname) or
                _NON_FOOTBALL_CHANNEL_RE.search(category)):
            continue
        xn = ch.get("_match_norm") or normalise(cname)
        if not xn:
            continue
        ch_cc = _resolve_channel_country(cname, category)  # category first, then name
        xn, ch_cc = _normalise_channel_country_labels(cname, ch_cc)
        xset = set(xn.split())
        best, best_src, best_country, best_exact_provider = 0.0, "", "", False
        best_competition_secure = False
        for orig, bcountry, allowed, sn, sset in srcs:
            viaplay_no_feed = (
                bcountry == "NO" and _viaplay_listing(sn, bcountry) and
                (_viaplay_norway_linear_feed(xn) or
                 _norway_premier_league_feed(xn)))
            viaplay_fi_feed = (bcountry == "FI" and _viaplay_listing(sn, bcountry) and
                               _viaplay_finland_linear_feed(xn))
            nordic_viaplay = (
                bcountry in {"NO", "SE", "DK", "FI"} and
                _viaplay_listing(sn, bcountry))
            shared_4k_feed = (nordic_viaplay and _is_4k_category(category) and
                              _viaplay_norway_linear_feed(xn))
            # Country rule: if the channel HAS a recognised country prefix and
            # it isn't in this broadcaster's allowed set -> skip (wrong country).
            if (allowed is not None and ch_cc is not None and ch_cc not in allowed and
                    not shared_4k_feed):
                continue
            if viaplay_no_feed or viaplay_fi_feed or shared_4k_feed:
                # LTV identifies the streaming platform, while Norwegian TV
                # providers expose its simultaneous events on these linear
                # feeds. Keep them possible until team text or EPG confirms one.
                if 0.94 > best:
                    best, best_src, best_country = 0.94, orig, bcountry
                    best_exact_provider = False
                    best_competition_secure = bool(
                        bcountry == "NO" and ch_cc == "no" and
                        normalise_event_name(league_name) == "premier league" and
                        _norway_premier_league_feed(xn))
                continue
            if _numbers_conflict(xn, sn):
                continue
            # Generic tokens such as "tv" or "sport" must never be enough
            # to establish a match (VG TV must not match VGN TV). At the same
            # time compact brand spellings are equivalent: VG TV == VGTV.
            sid = set(_distinctive(sn.split()))
            xid = set(_distinctive(xn.split()))
            numbered_package = sn in _NUMBERED_FEED_PACKAGES
            if numbered_package and not (sid and sid <= xid):
                continue
            scompact = re.sub(r"\s+", "", sn)
            xcompact = re.sub(r"\s+", "", xn)
            compact_exact = bool(scompact and scompact == xcompact)
            # A complete broadcaster brand may be embedded in a longer event
            # channel name ("... | VGTV PPV 3"). Do not accept the reverse:
            # a short generic channel such as "TV 2" is not "TV 2 Play".
            # A generic source label such as "Live" must not match every
            # longer channel carrying that word. Exact generic names can still
            # match themselves; containment requires a distinctive brand.
            compact_contained = (bool(sid) or len(sn.split()) >= 2) and \
                                len(scompact) >= 4 and scompact in xcompact
            inter = xid & sid
            if numbered_package:
                s = 1.0 if compact_exact else 0.96
            elif compact_exact:
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
                best_competition_secure = False
                # A prominent provider result must be a named linear channel,
                # an exact normalized name, and carry the provider's country.
                # Streaming platforms such as TV 2 Play remain category-only.
                best_exact_provider = bool(
                    compact_exact and ((allowed is None and ch_cc is not None) or
                                       (allowed is not None and ch_cc in allowed) or
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
            rows[-1]["competition_secure"] = best_competition_secure
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
        # Exact two-team titles are strongest. A one-team title is useful but
        # remains possible; generic PPV slots follow it as fallback choices.
        priority = 3 if row["fixture_match"] == "exact" else (
            2 if row["fixture_match"] == "partial" else 1)
        ranked.append((priority, float(row.get("score") or 0), index, row))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in ranked]

def find_team_channels(team_terms, xtream_channels, cats, x):
    """Find plausible match-specific PPV/event channels.

    Both fixture teams make a definite event candidate. One fixture team is
    still a possible candidate, including a club channel or a differently
    named fixture that could be re-assigned by the provider.
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
        hay = ch.get("_event_norm") or normalise_event_name(cname)
        category = ch.get("_match_category", cats.get(ch["category_id"], ""))
        hits = 0
        reserve_team = False
        for forms in side_forms:
            if any(re.fullmatch(re.escape(form) + r"\s+[2-9]\d*", hay)
                   for form in forms):
                reserve_team = True
            matched_forms = [form for form in forms
                             if re.search(r"(?<![a-z0-9])" + re.escape(form) +
                                          r"(?![a-z0-9])", hay)]
            if matched_forms:
                hits += 1
        if hits >= 1 and not reserve_team:
            out.append({
                "xtream_name": cname, "stream_id": ch["stream_id"],
                "category": category,
                "logo": ch.get("stream_icon", ""),
                "quality": quality_tag(cname),
                "url": x.stream_url(ch["stream_id"]),
                "fixture_match": "exact" if hits >= 2 else "partial",
            })
    return out

def find_competition_channels(fixture, xtream_channels, cats, x):
    """Return possible channels explicitly named for the fixture competition."""
    league = normalise_event_name(fixture.get("league_name") or "")
    distinctive = _distinctive(league.split())
    if not league or not distinctive:
        return []
    phrase = re.compile(r"(?<![a-z0-9])" + re.escape(league) + r"(?![a-z0-9])")
    out = []
    generic_team_words = {"united", "city", "town", "rovers", "county",
                          "athletic", "wanderers", "forest", "football", "club"}
    side_forms = []
    for team in (fixture.get("home"), fixture.get("away")):
        full = {normalise(value) for value in _expand_terms(
            str(team or "").lower().strip()) if len(normalise(value)) >= 3}
        short = {word for form in full for word in form.split()
                 if len(word) >= 5 and word not in generic_team_words}
        side_forms.append((full, short))
    for ch in xtream_channels:
        cname = str(ch.get("name") or "")
        category = ch.get("_match_category", cats.get(ch.get("category_id"), ""))
        if (_NON_FOOTBALL_CHANNEL_RE.search(cname) or
                _NON_FOOTBALL_CHANNEL_RE.search(category)):
            continue
        channel_cc = _resolve_channel_country(cname, category)
        cleaned_name, channel_cc = _normalise_channel_country_labels(
            cname, channel_cc)
        hay = normalise_event_name(cname + " " + category)
        category_hay = normalise_event_name(category)
        norway_family = bool(
            league == "premier league" and channel_cc == "no" and
            _norway_premier_league_feed(cleaned_name))
        league_context = bool(
            phrase.search(hay) or
            (league == "premier league" and
             re.search(r"(?<![a-z0-9])epl(?![a-z0-9])", hay)) or
            norway_family)
        if not league_context:
            continue
        name_hay = normalise_event_name(cname)
        side_hits = 0
        for full, short in side_forms:
            if (any(re.search(r"(?<![a-z0-9])" + re.escape(form) +
                              r"(?![a-z0-9])", name_hay) for form in full) or
                    any(re.search(r"(?<![a-z0-9])" + re.escape(word) +
                                  r"(?![a-z0-9])", name_hay) for word in short)):
                side_hits += 1
        uk_epl_team = bool(
            league == "premier league" and channel_cc in {"uk", "gb"} and
            side_hits >= 1 and
            (phrase.search(category_hay) or
             re.search(r"(?<![a-z0-9])epl(?![a-z0-9])", category_hay)))
        competition_secure = norway_family or uk_epl_team
        out.append({
            "xtream_name": cname, "stream_id": ch.get("stream_id"),
            "category": category, "logo": ch.get("stream_icon", ""),
            "quality": quality_tag(cname), "url": x.stream_url(ch.get("stream_id")),
            "fixture_match": ("exact" if side_hits >= 2 else
                              ("partial" if side_hits == 1 else "league")),
            "league_match": True,
            "competition_secure": competition_secure,
            "matched": fixture.get("league_name") or league,
        })
    return out

def _enforce_fixture_secure_matches(fixture, result):
    """Apply explicit secure rules after every channel-discovery path merges."""
    result = dict(result or {})
    premier_league = normalise_event_name(
        (fixture or {}).get("league_name") or "") == "premier league"
    generic_team_words = {"united", "city", "town", "rovers", "county",
                          "athletic", "wanderers", "forest", "football", "club"}
    team_forms = []
    for team in ((fixture or {}).get("home"), (fixture or {}).get("away")):
        full = {normalise(value) for value in _expand_terms(
            str(team or "").lower().strip()) if len(normalise(value)) >= 3}
        short = {word for form in full for word in form.split()
                 if len(word) >= 5 and word not in generic_team_words}
        team_forms.append((full, short))
    for key in ("matches", "ppv_hits"):
        rows = []
        for original in result.get(key) or []:
            row = dict(original)
            name = str(row.get("xtream_name") or "")
            category = str(row.get("category") or "")
            channel_cc = _resolve_channel_country(name, category)
            cleaned_name, channel_cc = _normalise_channel_country_labels(
                name, channel_cc)
            name_hay = normalise_event_name(name)
            category_hay = normalise_event_name(category)
            team_hit = any(
                any(re.search(r"(?<![a-z0-9])" + re.escape(form) +
                              r"(?![a-z0-9])", name_hay) for form in full) or
                any(re.search(r"(?<![a-z0-9])" + re.escape(word) +
                              r"(?![a-z0-9])", name_hay) for word in short)
                for full, short in team_forms)
            if (premier_league and channel_cc == "no" and
                    _norway_premier_league_feed(cleaned_name)):
                row["competition_secure"] = True
                row["secure_reason"] = "norway_premier_league"
            elif (premier_league and channel_cc in {"uk", "gb"} and team_hit and
                  (re.search(r"(?<![a-z0-9])epl(?![a-z0-9])", category_hay) or
                   re.search(r"(?<![a-z0-9])premier league(?![a-z0-9])",
                             category_hay))):
                row["competition_secure"] = True
                row["secure_reason"] = "uk_epl_fixture_team"
            rows.append(row)
        result[key] = rows
    return result

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
    return "football-v30|" + _vod_cache_key(x) + "|" + str(
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

def _fixture_title_has_both_teams(title, home, away):
    """True when an EPG title contains both fixture sides or known aliases."""
    hay = normalise_event_name(title)
    if not hay:
        return False
    def side_hit(team):
        forms = {normalise(value) for value in _expand_terms(
            str(team or "").lower().strip())}
        return any(value and re.search(r"(?<![a-z0-9])" + re.escape(value) +
                                       r"(?![a-z0-9])", hay)
                   for value in forms)
    return side_hit(home) and side_hit(away)

def _cached_epg_discovery(fixtures, channels, cats, x):
    """Discover fixture channels from already-cached EPG in one shared pass.

    This function never contacts the provider. Missing/no-info EPG is neutral.
    """
    prepared = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        try:
            kickoff = datetime.datetime.fromisoformat(str(
                fixture.get("start") or "").replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        prepared.append((_sports_event_key(fixture.get("home"), fixture.get("away"),
                                            fixture.get("start")), fixture, kickoff))
    if not prepared or not _EPG_CACHE:
        return {}
    channel_by_sid = {str(ch.get("stream_id")): ch for ch in channels
                      if isinstance(ch, dict) and ch.get("stream_id") is not None}
    found = {}
    for sid, cached in _EPG_CACHE.items():
        channel = channel_by_sid.get(str(sid))
        programmes = cached.get("programmes") if isinstance(cached, dict) else None
        if not channel or not isinstance(programmes, list):
            continue
        for programme in programmes:
            if not isinstance(programme, dict):
                continue
            title = str(programme.get("title") or "").strip()
            try:
                start = float(programme.get("start_ts") or 0)
                stop = float(programme.get("stop_ts") or 0) or start + 3 * 3600
            except (TypeError, ValueError):
                continue
            if not title or not start:
                continue
            for key, fixture, kickoff in prepared:
                # Allow pre-match coverage and provider schedule rounding, but
                # never compare unrelated programmes elsewhere in the guide.
                if start > kickoff + 90 * 60 or stop < kickoff - 45 * 60:
                    continue
                if not _fixture_title_has_both_teams(
                        title, fixture.get("home"), fixture.get("away")):
                    continue
                row = {
                    "xtream_name": str(channel.get("name") or ""),
                    "stream_id": channel.get("stream_id"),
                    "category": cats.get(channel.get("category_id"), ""),
                    "logo": channel.get("stream_icon", ""),
                    "quality": quality_tag(str(channel.get("name") or "")),
                    "url": x.stream_url(channel.get("stream_id")),
                    "matched": title, "score": 1.0,
                    "provider_exact": True, "fixture_match": "exact",
                    "epg_confirmed": True, "epg_title": title,
                }
                bucket = found.setdefault(key, {})
                bucket[str(channel.get("stream_id"))] = row
    return {key: list(rows.values()) for key, rows in found.items()}

def _add_epg_discoveries(result, rows):
    """Union independently discovered EPG channels into a sports result."""
    merged = dict(result or {})
    matches = [dict(row) for row in merged.get("matches") or []]
    ppv = [dict(row) for row in merged.get("ppv_hits") or []]
    locations = {str(row.get("stream_id")): (bucket, index)
                 for bucket in (matches, ppv) for index, row in enumerate(bucket)}
    for epg_row in rows or []:
        sid = str(epg_row.get("stream_id"))
        existing = locations.get(sid)
        if existing:
            bucket, index = existing
            bucket[index] = dict(bucket[index], **epg_row)
        else:
            matches.append(dict(epg_row))
            locations[sid] = (matches, len(matches) - 1)
    matches.sort(key=lambda row: (not row.get("epg_confirmed"),
                                  -float(row.get("score") or 0)))
    merged["matches"], merged["ppv_hits"] = matches, ppv
    return merged

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
    _load_epg_disk_cache(x)
    result = _match_sports_fixture_channels(fixture, cfg, channels, cats, x)
    discovered = _cached_epg_discovery([fixture], channels, cats, x)
    result = _add_epg_discoveries(result, discovered.get(_sports_event_key(
        fixture.get("home"), fixture.get("away"), fixture.get("start")), []))
    return _enforce_fixture_secure_matches(fixture, result)

def _match_sports_fixture_channels(fixture, cfg, channels, cats, x):
    """Match one fixture using an already-loaded Xtream catalogue."""
    threshold = max(0.40, min(0.80, float(cfg.get("match_threshold", 0.62) or 0.62)))
    candidates = _sports_fixture_channel_shortlist(fixture, channels, cats)
    matches = rank_fixture_channels(
        match_channels(fixture.get("by_country") or {}, candidates, cats, threshold,
                       fixture.get("league_name") or ""),
        fixture.get("home"), fixture.get("away"))
    # Preserve fuzzy compatibility for unusual broadcaster spellings that have
    # no useful indexed term. This slower path runs only when a guide exists and
    # the shortlist found no broadcaster match.
    if (not matches and fixture.get("by_country") and
            len(candidates) < len(channels)):
        matches = rank_fixture_channels(
            match_channels(fixture.get("by_country") or {}, channels, cats, threshold,
                           fixture.get("league_name") or ""),
            fixture.get("home"), fixture.get("away"))
    for row in matches:
        row["url"] = x.stream_url(row["stream_id"])
        league = normalise_event_name(fixture.get("league_name") or "")
        hay = normalise_event_name(str(row.get("xtream_name") or "") + " " +
                                   str(row.get("category") or ""))
        row["league_match"] = bool(league and re.search(
            r"(?<![a-z0-9])" + re.escape(league) + r"(?![a-z0-9])", hay))
    hits = find_team_channels([fixture.get("home", ""), fixture.get("away", "")],
                              candidates, cats, x)
    have = {str(row.get("stream_id")) for row in matches}
    ppv_hits = [row for row in hits if str(row.get("stream_id")) not in have]
    ppv_by_id = {str(row.get("stream_id")): row for row in ppv_hits}
    for row in find_competition_channels(fixture, candidates, cats, x):
        sid = str(row.get("stream_id"))
        if sid in have:
            continue
        if sid in ppv_by_id:
            ppv_by_id[sid].update({
                key: value for key, value in row.items()
                if value not in (None, "", False)})
        else:
            ppv_hits.append(row)
            ppv_by_id[sid] = row
    return _enforce_fixture_secure_matches(fixture, {
        "logged_in": True, "availability_checked": True,
        "matches": matches, "ppv_hits": ppv_hits})

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
 .chn{font-size:13px}.fixturechanneltitle{cursor:pointer}.fixturechanneltitle:hover{color:var(--acc)}
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
 .countrygrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px;margin-top:13px}.countrychoice{display:flex;align-items:center;gap:9px;padding:10px 11px;border:1px solid var(--line);border-radius:9px;background:var(--card);cursor:pointer;user-select:none}.countrychoice:hover{border-color:#4a586c}.countrychoice.on{border-color:#2768d8;background:#102347}.countrychoice input{width:auto;margin:0}.countryflag{font-size:18px}.countryname{min-width:0;flex:1}.countrycode{font-size:10px;color:var(--muted);font-weight:700}.countrychoice.unsupported{border-color:#765c2b;background:#241e13}
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
      <div id="setupAutoShutdownWrap" class="setupoptional"><b style="display:block;color:var(--fg);margin-bottom:7px" data-i18n="Auto shutdown when inactive">Auto shutdown when inactive</b><label><span data-i18n="Stop TVMate after">Stop TVMate after</span> <select id="setupAutoShutdown" style="width:auto;margin-left:7px"><option value="0" data-i18n="Keep running â€” uses approximately three crumbs and your calculator works harder">Keep running â€” uses approximately three crumbs and your calculator works harder</option><option value="30">30 minutes</option><option value="60">1 hour</option><option value="120">2 hours</option><option value="240">4 hours</option></select></label><div style="margin-top:6px" data-i18n="Activity in TVMate resets the timer.">Activity in TVMate resets the timer.</div></div>
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
          <button class="ghost" onclick="favSelectedCats()"><span data-i18n="â˜… Add to Favorites">&#9733; Add to Favorites</span></button>
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
          <button class="ghost" onclick="favPlaylist()"><span data-i18n="â˜… Add to Favorites">&#9733; Add to Favorites</span></button>
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
      <div class="muted">FotMob provides fixtures and team details. Live Soccer TV supplies worldwide channel listings, with FotMob listings used automatically as fallback.</div>
      <input id="s_cc" type="hidden">
      </div>
      <div class="settingsgroup" data-settings-panel="iptv" hidden>
      <div class="colh" data-i18n="Playback">Playback</div>
      <div class="muted" data-i18n="Choose the stream URL format requested from your IPTV provider. TS is the normal default; use M3U8 if your provider works better with HLS.">Choose the stream URL format requested from your IPTV provider. TS is the normal default; use M3U8 if your provider works better with HLS.</div>
      <div style="margin-top:13px"><label data-i18n="Stream extension">Stream extension</label><select id="s_ext"><option value="ts">ts</option><option value="m3u8">m3u8</option></select></div>
      </div>
      <div class="settingsgroup" data-settings-panel="maintenance" hidden>
      <div class="colh" data-i18n="Maintenance">Maintenance</div>
      <div class="muted" data-i18n="Control automatic updates of local data and manage TVMate's local files.">Control automatic updates of local data and manage TVMate's local files.</div>
      <div style="margin-top:13px"><label data-i18n="Auto shutdown when inactive">Auto shutdown when inactive</label><select id="s_autoshutdown"><option value="0" data-i18n="Keep running â€” uses approximately three crumbs and your calculator works harder">Keep running â€” uses approximately three crumbs and your calculator works harder</option><option value="30">30 minutes</option><option value="60">1 hour</option><option value="120">2 hours</option><option value="240">4 hours</option></select></div>
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
  "Search":"SÃ¸k","Playlist Builder":"Lag spilleliste","Playlists":"Spillelister","Timeline":"Tidslinje","My List":"Min liste","My Profile":"Min profil","Edit Profile":"Rediger profil","My Timeline":"Min tidslinje","My TV":"Live TV","My Movies":"Mine filmer","My Shows":"Mine serier","My Games":"Mine spill","My Racing":"Min racing","My Teams":"Mine lag","Favorite Movies":"Favorittfilmer","Favorite Shows":"Favorittserier","Favorite Games":"Favorittspill","Favorite Teams":"Favorittlag","Settings":"Innstillinger","Stop TVMate":"Stopp TVMate",
  "Welcome to TVMate":"Velkommen til TVMate","Let's make it yours. Everything here can be changed later from Settings or Edit Profile.":"La oss gjÃ¸re TVMate til ditt. Alt her kan endres senere i Innstillinger eller Rediger profil.","Your name":"Navnet ditt","Pick an emblem":"Velg et emblem","Emblem":"Emblem",
  "Optional: add a favorite show or movie now, or let TVMate add demo items so you can see what My Profile looks like.":"Valgfritt: legg til en favorittserie eller film nÃ¥, eller la TVMate legge til demo-innhold sÃ¥ du kan se hvordan Min profil ser ut.","Optional: add a favorite show or movie now, or let TVMate add demo items so you can see what Profile looks like.":"Valgfritt: legg til en favorittserie eller film nÃ¥, eller la TVMate legge til demo-innhold sÃ¥ du kan se hvordan Profil ser ut.","If you don't add anything yet, TVMate will add a couple of demo items. They disappear permanently when you favorite your first real movie or show.":"Hvis du ikke legger til noe ennÃ¥, legger TVMate inn et par demo-elementer. De forsvinner permanent nÃ¥r du favorittmerker din fÃ¸rste ekte film eller serie.",
  "How should TVMate open?":"Hvordan skal TVMate Ã¥pnes?","TVMate opens straight in your browser. Use Stop TVMate in the top-right when you want to shut the app down.":"TVMate Ã¥pnes rett i nettleseren. Bruk Stopp TVMate Ã¸verst til hÃ¸yre nÃ¥r du vil avslutte appen.","Modern TVMate":"Moderne TVMate","TVMate opens straight in your browser with no CMD window.":"Tvmate Ã¥pnes rett i nettleseren","Bookmark TVMate":"Bokmerk TVMate","Press Ctrl+D to bookmark TVMate for an easy way back.":"Trykk Ctrl+D for Ã¥ bokmerke TVMate, sÃ¥ finner du enkelt tilbake.","Copy address":"Kopier adresse","Stop TVMate after":"Stopp TVMate etter","Activity in TVMate resets the timer.":"Aktivitet i TVMate nullstiller tidsuret.",
  "You're ready":"Du er klar","TVMate is set up around what you follow. A streaming login is only needed for your own channels, movies and shows.":"TVMate er satt opp rundt det du fÃ¸lger. StrÃ¸mmeinnlogging trengs bare for dine egne kanaler, filmer og serier.","Want playback too?":"Vil du ogsÃ¥ spille av?","Set up your Xtream login for your own channels, movies and shows.":"Sett opp Xtream-innlogging for dine egne kanaler, filmer og serier.","Set up Xtream":"Sett opp Xtream","All done":"Alt klart","Head straight to My Profile and start using TVMate.":"GÃ¥ rett til Min profil og begynn Ã¥ bruke TVMate.","Head straight to Profile and start using TVMate.":"GÃ¥ rett til Profil og begynn Ã¥ bruke TVMate.","Let's go, I'm ready":"KjÃ¸r pÃ¥, jeg er klar",
  "Background style":"Bakgrunnsstil","Floating pancakes & TVs":"Flytende pannekaker og TV-er","Off":"Av","What do you want to follow?":"Hva vil du fÃ¸lge?","Movies and shows are always available. Turn the extra sections on or off here.":"Filmer og serier er alltid tilgjengelige. SlÃ¥ ekstraseksjonene av eller pÃ¥ her.",
  "Football":"Fotball","Games":"Spill","Movies":"Filmer","Matchfinder, My Teams and fixtures":"Kampfinner, Mine lag og kamper","Matchfinder, Sports and fixtures":"Kampfinner, Sport og kamper","My Racing, schedules and followed drivers":"Min racing, terminlister og fÃ¸rere du fÃ¸lger","Racing schedules and followed drivers":"Racingterminlister og fÃ¸rere du fÃ¸lger","Steam wishlist and game releases":"Steam-Ã¸nskeliste og spillanseringer",
  "Choose your football teams":"Velg fotballagene dine","Pick the teams you want fixtures for. You can add or remove teams later in My Teams.":"Velg lagene du vil se kamper for. Du kan legge til eller fjerne lag senere i Mine lag.","Pick the teams you want fixtures for. You can add or remove teams later in Sports.":"Velg lagene du vil se kamper for. Du kan legge til eller fjerne lag senere under Sport.","Search for a team, e.g. Brann":"SÃ¸k etter et lag, f.eks. Brann",
  "Choose your racing":"Velg racing","Select the series you want on My Racing and your timeline.":"Velg seriene du vil ha i Min racing og pÃ¥ tidslinjen.","Select the series you want in Racing and your timeline.":"Velg seriene du vil ha i Racing og pÃ¥ tidslinjen.","Favorite Formula 1 team":"Favorittlag i Formel 1","Choose a Formula 1 team":"Velg et Formel 1-lag","Add something to watch":"Legg til noe Ã¥ se pÃ¥","Search shows":"SÃ¸k etter serier","Search movies":"SÃ¸k etter filmer",
  "Skip setup":"Hopp over oppsett","Back":"Tilbake","Next":"Neste","Run setup guide":"KjÃ¸r oppsettsveiviseren","Cancel":"Avbryt","Step":"Trinn","of":"av","Copied":"Kopiert","Copy this TVMate address:":"Kopier denne TVMate-adressen:",
  "Enter a profile name to continue.":"Skriv inn et profilnavn for Ã¥ fortsette.","Enter a profile name.":"Skriv inn et profilnavn.","Profile saved.":"Profilen er lagret.","Could not save profile.":"Kunne ikke lagre profilen.","No favorite teams selected yet.":"Ingen favorittlag er valgt ennÃ¥.","Searching...":"SÃ¸ker...","Add":"Legg til","No teams found.":"Fant ingen lag.","Could not search teams.":"Kunne ikke sÃ¸ke etter lag.","Favorite":"Favoritt","No results found.":"Fant ingen resultater.","Could not search.":"Kunne ikke sÃ¸ke.","Added":"Lagt til","Item":"Element","added to favorites.":"lagt til i favoritter.","Could not add favorite.":"Kunne ikke legge til favoritt.",
  "Live Matches":"Direktekamper","Today's Top Fixtures":"Dagens toppkamper","Upcoming Fixtures":"Kommende kamper","Show more matches":"Vis flere kamper","Show fewer matches":"Vis fÃ¦rre kamper","Search for a team...":"SÃ¸k etter et lag...","Find team or match":"Finn lag eller kamp","Refresh fixtures":"Oppdater kamper",
  "Find a match":"Finn en kamp","Search a team to find its fixtures, TV coverage and matching channels.":"SÃ¸k etter et lag for Ã¥ finne kamper, TV-dekning og matchende kanaler.","Search for a team, then choose Find fixtures when you want Matchfinder and TV results.":"SÃ¸k etter et lag, og velg deretter Finn kamper nÃ¥r du vil bruke Kampfinner og se TV-resultater.","Find team":"Finn lag","Search channels":"SÃ¸k kanaler","Find fixtures":"Finn kamper","Refresh channel matches":"Oppdater kanaltreff","Refreshing channel matches...":"Oppdaterer kanaltreff...","Loading channel matches...":"Laster kanaltreff...","Channel matches refreshed.":"Kanaltreff er oppdatert.","Lower strictness only if a known channel is being missed.":"Senk treffnÃ¸yaktigheten bare hvis en kjent kanal ikke blir funnet.","Matches":"Kamper","Best team/event matches":"Beste lag-/arrangementstreff","Definite channel matches":"Sikre kanaltreff","Best match":"Beste treff","Show more channels":"Vis flere kanaler","Show fewer channels":"Vis fÃ¦rre kanaler","TV listed":"TV oppfÃ¸rt","No TV":"Ingen TV","No matching channels":"Ingen matchende kanaler","channel":"kanal","channels":"kanaler",
  "Back to Sports":"Tilbake til Sport","No TV listings for this fixture.":"Ingen TV-oversikt for denne kampen.","Available channels":"Tilgjengelige kanaler","TV listings":"TV-oversikt","No channels in your list match this broadcaster.":"Ingen kanaler i listen din matcher denne TV-leverandÃ¸ren.","Other TV providers":"Andre TV-leverandÃ¸rer","Other broadcaster listings":"Andre TV-oppfÃ¸ringer",
  "Teams":"Lag","My Sports":"Min sport","Shows":"Serier","Show":"Serie","Sports":"Sport","Movie":"Film","Formula 1":"Formel 1","Racing":"Racing","Choose F1 team":"Velg F1-lag","Live TV":"Live TV","Find Channels":"Finn kanaler","Find Categories":"Finn kategorier","Choose channels":"Velg kanaler","Empty channel slot":"Tom kanalplass","Choose a team to see details.":"Velg et lag for Ã¥ se detaljer.","Home ground":"Hjemmebane","Head coach":"Hovedtrener","League":"Liga","Country":"Land",
  "Choose up to four channels.":"Velg opptil fire kanaler.","Star channels first, then choose up to four here.":"Favorittmerk kanaler fÃ¸rst, og velg deretter opptil fire her.",
  "Choose up to five channels.":"Velg opptil fem kanaler.","Star channels first, then choose up to five here.":"Favorittmerk kanaler fÃ¸rst, og velg deretter opptil fem her.",
  "No favorite teams yet.":"Ingen favorittlag ennÃ¥.","No upcoming fixture found.":"Ingen kommende kamp funnet.","Could not load team fixtures.":"Kunne ikke laste lagkamper.",
  "No F1 team selected.":"Ingen F1-lag valgt.","Could not load Formula 1 calendar.":"Kunne ikke laste Formel 1-kalenderen.",
  "Nothing airing close to now from your favorite shows.":"Ingenting sendes nÃ¦r nÃ¥tid fra favorittseriene dine.","Could not load your shows.":"Kunne ikke laste seriene dine.",
  "Airs in":"Sendes om","Released":"Utgitt","Releases":"Lanseres","Just released":"Nettopp utgitt","ago":"siden","Stream found in playlist":"StrÃ¸m funnet i spillelisten",
  "Live now":"Direkte nÃ¥","Next match":"Neste kamp","Next race":"Neste lÃ¸p","No upcoming race found.":"Ingen kommende lÃ¸p funnet.",
  "Choose a driver to see details.":"Velg en fÃ¸rer for Ã¥ se detaljer.","Driver profile":"FÃ¸rerprofil","Loading drivers and next race...":"Laster fÃ¸rere og neste lÃ¸p...","Loading racing schedules...":"Laster racingterminlister...","Loading fixture...":"Laster kamp...","Loading next race...":"Laster neste lÃ¸p...","Nothing happening around now.":"Ingenting skjer rundt nÃ¥.","Play":"Spill av","No upcoming events found.":"Ingen kommende arrangementer funnet.","Choose at least one racing series above.":"Velg minst Ã©n racingserie ovenfor.","Could not load racing schedules.":"Kunne ikke laste racingterminlistene.","Definite event matches":"Sikre arrangementstreff","Dedicated series channels":"Dedikerte seriekanaler","Possible channels by category":"Mulige kanaler etter kategori","Other possible channels":"Andre mulige kanaler",
  "Recently":"Nylig","Upcoming":"Kommende","Right now":"Akkurat nÃ¥",
  "Favorite Channels":"Favorittkanaler","EPG Refresh":"Oppdater EPG","Channels":"Kanaler",
  "All Categories":"Alle kategorier","Selected categories":"Valgte kategorier","Filter Channels":"Kanaler","Playlist":"Spilleliste",
  "Add to Favorites":"Legg til favoritter","Tick all":"Velg alle","Untick all":"Fjern alle","Add ticked":"Legg til valgte",
  "Make Playlist (Categories)":"Lag spilleliste (kategorier)","Make Playlist (Channels)":"Lag spilleliste (kanaler)","Clear":"Fjern","Reset":"Nullstill",
  "Ticked channels land here.":"Valgte kanaler vises her.",
  "Click a selected category to see its channels.":"Trykk pÃ¥ en kategori for Ã¥ vise kanaler",
  "Tick categories on the left.":"Velg kategorier pÃ¥ venstre side.",
  "Favorite Categories":"Favorittkategorier","Categories":"Kategorier",
  "Matchfinder - Get Live / Next Match":"Kampfinner - Live / Neste kamp",
  "Match strictness":"TreffnÃ¸yaktighet",
  "Save":"Lagre","Reload channels":"Last inn kanaler","Test login":"Test innlogging",
  "Connection":"Tilkobling","Preferences":"Innstillinger","Search Options":"SÃ¸kealternativer","General":"Generelt","Content":"Innhold","Playback":"Avspilling","Maintenance":"Vedlikehold","Health":"Status","Content startup":"Innhold ved oppstart","Sports Search":"SportssÃ¸k",
  "Personalize TVMate and choose what opens when the app starts.":"Tilpass TVMate og velg hva som Ã¥pnes nÃ¥r appen starter.","Checks your favorite series for newly available episodes after TVMate opens.":"Ser etter nylig tilgjengelige episoder i favorittseriene dine etter at TVMate Ã¥pnes.","Show or hide optional sections. Disabling one also skips it during external-content refreshes.":"Vis eller skjul valgfrie seksjoner. Deaktiverte seksjoner hoppes ogsÃ¥ over ved oppdatering av eksternt innhold.",
  "Choose which regional TV listings Sports Search uses to find broadcasters for matches.":"Velg hvilke regionale TV-oversikter SportssÃ¸k bruker for Ã¥ finne kanaler som viser kampene.","Select the regional guides to search. Countries add broadcaster information but never limit which fixtures are shown.":"Velg regionale TV-guider. Landene legger til kanalinformasjon, men begrenser aldri hvilke kamper som vises.","Unsupported saved code":"Ikke stÃ¸ttet lagret kode",
  "Choose the stream URL format requested from your IPTV provider. TS is the normal default; use M3U8 if your provider works better with HLS.":"Velg strÃ¸mformatet som forespÃ¸rres fra IPTV-leverandÃ¸ren. TS er vanlig standard; bruk M3U8 hvis leverandÃ¸ren fungerer bedre med HLS.","Control automatic updates of local data and manage TVMate's local files.":"Styr automatiske oppdateringer av lokale data og administrer TVMates lokale filer.","Choose which content TVMate updates automatically after it opens.":"Velg hvilket innhold TVMate oppdaterer automatisk etter oppstart.","Refresh IPTV & EPG on startup":"Oppdater IPTV og EPG ved oppstart","Refresh sports, racing & games on startup":"Oppdater sport, racing og spill ved oppstart","Refresh Xtream channels, movies, shows and TV guide data.":"Oppdater Xtream-kanaler, filmer, serier og TV-guide.","Refresh matches, regional TV listings, racing and your Steam wishlist.":"Oppdater kamper, regionale TV-oversikter, racing og Steam-Ã¸nskelisten din.","Stops the local TVMate server after no interaction or playback. Active video keeps TVMate awake.":"Stopper den lokale TVMate-serveren nÃ¥r det ikke har vÃ¦rt aktivitet eller avspilling. Aktiv video holder TVMate vÃ¥ken.","Testing...":"Tester...","Login successful.":"Innloggingen fungerte.","Login failed.":"Innloggingen mislyktes.",
  "Profile":"Profil","Setup":"Oppsett","Profile name":"Profilnavn","Profile emblem":"Profilemblem","Backup & Import":"Sikkerhetskopi og import","Download a portable backup or merge one into this profile. Caches and artwork are never included.":"Last ned en flyttbar sikkerhetskopi eller slÃ¥ en sammen med denne profilen. Mellomlager og omslagskunst tas aldri med.","Export profile":"Eksporter profil","Export full backup":"Eksporter full sikkerhetskopi","Import backup":"Importer sikkerhetskopi","Profile backup keeps credentials out. Full backup includes Xtream credentials; store it securely.":"Profilsikkerhetskopien utelater innlogging. Full sikkerhetskopi inkluderer Xtream-innlogging; oppbevar den sikkert.","Backup downloaded.":"Sikkerhetskopien er lastet ned.","Backup imported and merged.":"Sikkerhetskopien er importert og slÃ¥tt sammen.","Full backup restored.":"Full sikkerhetskopi er gjenopprettet.","Could not import this backup.":"Kunne ikke importere denne sikkerhetskopien.",
  "Balanced":"Balansert","Spotlight":"Fremhevet","Now Timeline":"NÃ¥-tidslinje","Profile Hub":"Profiloversikt","Changes the arrangement of your Profile page only.":"Endrer bare oppsettet pÃ¥ profilsiden din.",
  "My List layout":"Min liste-oppsett","My Profile layout":"Min profil-oppsett","Now & Next":"NÃ¥ og neste",
  "Features & Display":"Funksjoner og visning","Show football features":"Vis fotballfunksjoner","Show Formula 1 features":"Vis Formel 1-funksjoner","Show racing features":"Vis racingfunksjoner","Show game features":"Vis spillfunksjoner","Animated background decorations":"Animerte bakgrunnsdekorasjoner","Choose the racing series you want to follow.":"Velg racingseriene du vil fÃ¸lge.",
  "Preferred language":"Foretrukket sprÃ¥k",
  "Profile layout":"Profiloppsett","Your everyday TVMate preferences. Run the setup guide to change what you follow.":"Dine vanlige TVMate-innstillinger. KjÃ¸r oppsettsveiviseren for Ã¥ endre hva du fÃ¸lger.","Look for newly available episodes when TVMate starts.":"Se etter nylig tilgjengelige episoder nÃ¥r TVMate starter.","Refresh channels, movies, shows and episode data when TVMate starts.":"Oppdater kanaler, filmer, serier og episodedata nÃ¥r TVMate starter.",
  "Startup":"Oppstart","Your Xtream login stays in your local config.json and is only sent to your own provider.":"Xtream-innloggingen lagres lokalt i config.json og sendes bare til din egen leverandÃ¸r.","Auto shutdown when inactive":"Avslutt automatisk ved inaktivitet","Keep running â€” uses approximately three crumbs and your calculator works harder":"Fortsett Ã¥ kjÃ¸re â€” bruker omtrent tre smuler, og kalkulatoren din jobber hardere","Changes are kept locally on this device.":"Endringer lagres lokalt pÃ¥ denne enheten.",
  "Recently Added":"Nylig lagt til","See what else is new":"Se hva mer som er nytt","Discover Movies":"Oppdag filmer","Popular":"PopulÃ¦rt","New Releases":"Nye utgivelser","Featured":"Fremhevet",
  "Check for new movies":"Se etter nye filmer",
  "Back to My Movies":"Tilbake til Mine filmer","Back to Movies":"Tilbake til Filmer",
  "Your Latest Episodes":"Dine nyeste episoder","See more latest episodes":"Se flere nyeste episoder",
  "Back to My Shows":"Tilbake til Mine serier","Back to Shows":"Tilbake til Serier",
  "Upcoming Episodes":"Kommende episoder","Airs":"Sendes",
  "Today":"i dag","Tomorrow":"i morgen","in":"om","day":"dag","days":"dager",
  "hour":"time","hours":"timer","minute":"minutt","minutes":"minutter",
  "Not available":"Ikke tilgjengelig",
  "Maintenance & Playback":"Vedlikehold og avspilling","Refresh all content":"Oppdater alt innhold",
  "Data & Refresh":"Data og oppdatering","Choose exactly which TVMate data should be updated.":"Velg nÃ¸yaktig hvilke TVMate-data som skal oppdateres.",
  "Refresh IPTV & EPG":"Oppdater IPTV og EPG","Refresh sports, racing & games":"Oppdater sport, racing og spill","Refresh everything":"Oppdater alt",
  "Startup refresh":"Oppdatering ved oppstart","IPTV & EPG":"IPTV og EPG","Other content":"Annet innhold","Everything":"Alt",
  "Check favorite shows on startup":"Se etter nye episoder i favorittserier ved oppstart",
  "Refresh all content on startup":"Oppdater alt innhold ved oppstart",
  "Artwork cache":"Mellomlagret omslagskunst","Clear artwork cache":"TÃ¸m omslagskunst",
  "Reset for cold-start test":"Nullstill for kaldstarttest",
  "Developer tools":"UtviklerverktÃ¸y",
  "Testing controls that clear temporary performance data.":"TestverktÃ¸y som tÃ¸mmer midlertidige ytelsesdata.",
  "Remove":"Fjern","Copy URL":"Kopier URL",
  "Match strictness (0.40â€“0.80)":"TreffnÃ¸yaktighet (0.40â€“0.80)",
  "Listings countries (comma separated: no, uk, us)":"Land for TV-guide (kommaseparert: no, uk, us)",
  "No channels here.":"Ingen kanaler her.","No program info":"Ingen programinfo",
  "channels":"kanaler","channels checked":"kanaler kontrollert","channels updated.":"kanaler oppdatert.",
  "One bulk guide download":"Ã‰n samlet guide-nedlasting","Downloading and processing the provider TV guide...":"Laster ned og behandler leverandÃ¸rens TV-guide...",
  "Parsing programme information...":"Behandler programinformasjon...","Matching guide data to favorite channels...":"Kobler guidedata til favorittkanaler...",
  "Large provider guides may take a little while...":"Store guider fra leverandÃ¸ren kan ta litt tid...","Fallback":"ReservelÃ¸sning",
  "Loading EPG...":"Laster EPG...","EPG loaded":"EPG lastet","EPG failed":"EPG feilet","Loading...":"Laster...",
  "Updated":"Oppdatert","No EPG":"Ingen EPG","Failed":"Feilet",
  "No favorites to load EPG for.":"Ingen favoritter Ã¥ laste EPG for.",
  "Updating TV guide":"Oppdaterer TV-guide","Finding channels in your favorites...":"Finner kanaler i favorittene dine...",
  "Loading programme information...":"Laster programinformasjon...","TV guide is ready.":"TV-guiden er klar.","with programme data":"med programdata",
  "Compatibility mode: loading one channel at a time...":"Kompatibilitetsmodus: laster Ã©n kanal om gangen...",
  "Retrying this batch one channel at a time...":"PrÃ¸ver denne gruppen pÃ¥ nytt, Ã©n kanal om gangen...","channels could not be refreshed.":"kanaler kunne ikke oppdateres.",
  "Channels available":"Kanaler tilgjengelig",
  "Update available":"Oppdatering tilgjengelig","you have":"du har","Downloading...":"Laster ned...",
  "Update downloaded. Restart now to finish updating?":"Oppdatering lastet ned. Start pÃ¥ nytt for Ã¥ fullfÃ¸re?",
  "Restart now":"Start pÃ¥ nytt","Update now":"Oppdater nÃ¥","Later":"Senere","Player":"Spiller","Restarting...":"Starter pÃ¥ nytt...",
  "Updating... this window will reload shortly.":"Oppdaterer... vinduet lastes inn pÃ¥ nytt snart.",
  "Update failed. Try again later.":"Oppdatering feilet. PrÃ¸v igjen senere.",
  "Restart failed. Please close and reopen the app.":"Omstart feilet. Lukk og Ã¥pne appen igjen.",
  "Update installed. Please close this window and open Oloâ€™s TVMate again.":"Oppdatering installert. Lukk dette vinduet og Ã¥pne Oloâ€™s TVMate igjen.",
  "Check for updates":"Se etter oppdateringer","Checking...":"Sjekker...",
  "You are on the latest version":"Du har den nyeste versjonen",
  "Could not check for updates. Check your internet connection.":"Kunne ikke sjekke for oppdateringer. Sjekk internettforbindelsen.",
  "Open config folder":"Ã…pne konfigurasjonsmappe","Could not open folder.":"Kunne ikke Ã¥pne mappen.",
  "Source health":"Kildestatus","Test all sources":"Test alle kilder","Testing sources...":"Tester kilder...",
  "Shows whether the external data sources responded last time they were used.":"Viser om de eksterne datakildene svarte sist de ble brukt.",
  "not checked yet":"ikke sjekket enda","Not configured":"Ikke konfigurert","just now":"akkurat nÃ¥","min ago":"min siden","h ago":"t siden","d ago":"d siden",
  "working":"fungerer","failed":"feilet","items":"elementer","No sources.":"Ingen kilder.","Could not test sources.":"Kunne ikke teste kilder.",
  "Host (e.g. http://example.com:8080)":"Vert (f.eks. http://example.com:777)",
  "Username":"Brukernavn","Password":"Passord","Stream extension":"StrÃ¸m-format",
  "Default start section":"Standard oppstartseksjon","Search a team, e.g. Leeds":"SÃ¸k etter lag, f.eks. Leeds",
  "Find a channel, e.g. tv2 play":"Finn en kanal, f.eks. tv2 play",
  "Search a category, e.g. Norway":"SÃ¸k kategori, f.eks. Norge","Filter categories...":"Filtrer kategorier...",
  "Search your movies...":"SÃ¸k i filmene dine...",
  "Search your shows...":"SÃ¸k i seriene dine...",
  "Search Steam games...":"SÃ¸k etter Steam-spill...","Steam wishlist URL...":"Steam-Ã¸nskeliste-URL...","Filter wishlist...":"Filtrer Ã¸nskeliste...","Sync wishlist":"Synkroniser Ã¸nskeliste","Refresh wishlist":"Oppdater Ã¸nskeliste","Wishlist settings":"Ã˜nskelisteinnstillinger","Game":"Spill",
  "Check for new episodes":"Se etter nye episoder",
  "â˜… Add to Favorites":"â˜… Legg til favoritter","â˜… Favorite Channels":"â˜… Favorittkanaler"
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
  setupFootball.checked=c.football_enabled!==false;setupRacing.checked=c.f1_enabled!==false;s×Mtñ¼­zÊ&ŠÛ^u´é}Í•ÑÕÁÅQ•…µññíõô¤íô(€€€Í•Ñ1…¹œ¡‰½‘ä¹ÁÉ•™•ÉÉ•‘}±…¹Õ…”¤í…ÁÁ±åAÉ½™¥±•½¹™¥œ¡=‰©•Ð¹…ÍÍ¥¸¡íô±‰½‘ä±íÉ…¥¹}Í•É¥•ÌéÉÉ…ä¹™É½´¡}Í•ÑÕÁI…¥¹M•É¥•Ì¥ô¤¤í±½Í•AÉ½™¥±•M•ÑÕÀ ¤íÑ½…ÍÐ¡ÑÈ AÉ½™¥±”Í…Ù•¸œ¤¤ì(€€€¥˜¡½Á•¹aÑÉ•…´¥íÍ¡½ÝM•ÑÑ¥¹Ì ¤íÍ•ÑM•ÑÑ¥¹ÍQ…ˆ ¥ÁÑØœ¤íõ•±Í”Í¡½Ý5å±¥ÍÐ ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ ½Õ±¹½ÐÍ…Ù”ÁÉ½™¥±”¸œ¤íô(€‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½Á•¹aÑÉ•…´üM•ÐÕÀaÑÉ•…´œè‰1•ÐÌ¼°$´É•…‘äˆì)ô)™Õ¹Ñ¥½¸É•¹‘•É5å1¥ÍÑAÉ½™¥±” ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑAÉ½™¥±”œ¤í¥˜ …•°¥É•ÑÕÉ¸ì(€½¹ÍÐ¹…µ”õMÑÉ¥¹œ¡}ÁÉ½™¥±•½¹™¥œ¹ÁÉ½™¥±•}¹…µ•ñðœœ¤¹ÑÉ¥´ ¥ññÑÈ AÉ½™¥±”œ¤ì(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÁÉ½™¥±••µ‰±•´ˆøœ­ÁÉ½™¥±•µ‰±•µMÙœ¡}ÁÉ½™¥±•½¹™¥œ¹ÁÉ½™¥±•}•µ‰±•´¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÁÉ½™¥±•¹…µ”ˆøœ­•ÍŒ¡¹…µ”¤¬œð½‘¥Øøñ‰ÕÑÑ½¸ÑåÁ”ô‰‰ÕÑÑ½¸ˆ±…ÍÌô‰¡½ÍÐ•‘¥ÑÁÉ½™¥±•‰Ñ¸ˆ½¹±¥¬ô‰½Á•¹‘¥ÑAÉ½™¥±” ¤ˆ‘…Ñ„µ¤Äá¸ô‰‘¥ÐAÉ½™¥±”ˆøœ­•ÍŒ¡ÑÈ ‘¥ÐAÉ½™¥±”œ¤¤¬œð½‰ÕÑÑ½¸øœì)ô)±•Ð}•‘¥ÑAÉ½™¥±•µ‰±•´ôÑÙÍÑ…¬œì)™Õ¹Ñ¥½¸É•¹‘•É‘¥ÑAÉ½™¥±•µ‰±•µÌ ¥í½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% •Á}•µ‰±•µÌœ¤í¥˜ …•°¥É•ÑÕÉ¸í•°¹¥¹¹•É!Q50õ=‰©•Ð¹­•åÌ¡}AI=%1}5	15L¤¹µ…À¡­•äôøœñ‰ÕÑÑ½¸ÑåÁ”ô‰‰ÕÑÑ½¸ˆ±…ÍÌô‰•µ‰±•µ¡½¥”œ¬¡­•äôôõ}•‘¥ÑAÉ½™¥±•µ‰±•´üœ½¸œèœœ¤¬œˆ‘…Ñ„µ­•äôˆœ­­•ä¬œˆ½¹±¥¬ô‰Í•±•Ñ‘¥ÑAÉ½™¥±•µ‰±•´¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹­•ä¤ˆÑ¥Ñ±”ôˆœ­­•ä¬œˆøœ­ÁÉ½™¥±•µ‰±•µMÙœ¡­•ä¤¬œð½‰ÕÑÑ½¸øœ¤¹©½¥¸ œœ¤íô)™Õ¹Ñ¥½¸Í•±•Ñ‘¥ÑAÉ½™¥±•µ‰±•´¡­•ä¥í¥˜ …}AI=%1}5	15Mm­•åt¥É•ÑÕÉ¸í}•‘¥ÑAÉ½™¥±•µ‰±•´õ­•äíÉ•¹‘•É‘¥ÑAÉ½™¥±•µ‰±•µÌ ¤íô)…Íå¹Œ™Õ¹Ñ¥½¸½Á•¹‘¥ÑAÉ½™¥±” ¥ì(€±•ÐŒõíôíÑÉåíŒõ…Ý…¥Ð…Á¤ œ½…Á¤½½¹™¥œœ¤íõ…Ñ ¡”¥íŒõ}ÁÉ½™¥±•½¹™¥ññíôíô(€•Á}¹…µ”¹Ù…±Õ”õŒ¹ÁÉ½™¥±•}¹…µ•ñðœœí•Á}±…¹œ¹Ù…±Õ”õŒ¹ÁÉ•™•ÉÉ•‘}±…¹Õ…•ñð•¸œí}•‘¥ÑAÉ½™¥±•µ‰±•´õ}AI=%1}5	15MmŒ¹ÁÉ½™¥±•}•µ‰±•µtýŒ¹ÁÉ½™¥±•}•µ‰±•´èÑÙÍÑ…¬œíÉ•¹‘•É‘¥ÑAÉ½™¥±•µ‰±•µÌ ¤ì(€•Á}ÍÑ…ÉÐ¹Ù…±Õ”õŒ¹ÍÑ…ÉÑ}Í•Ñ¥½¹ñðµå±¥ÍÐœí•Á}±…å½ÕÐ¹Ù…±Õ”õlÑ¥µ•±¥¹”œ°‰…±…¹•œ°ÍÁ½Ñ±¥¡Ðœ°¡Õˆt¹¥¹±Õ‘•Ì¡Œ¹µå±¥ÍÑ}±…å½ÕÐ¤ýŒ¹µå±¥ÍÑ}±…å½ÕÐèÑ¥µ•±¥¹”œí•Á}¡•­Í¡½ÝÌ¹¡•­•ô„…Œ¹¡•­}Í¡½ÝÍ}½¹}ÍÑ…ÉÑÕÀí•Á}É•™É•Í¡¥ÁÑØ¹¡•­•ô„…Œ¹É•™É•Í¡}¥ÁÑÙ}½¹}ÍÑ…ÉÑÕÀí•Á}É•™É•Í¡ÍÁ½ÉÑÌ¹¡•­•ô„…Œ¹É•™É•Í¡}ÍÁ½ÉÑÍ}½¹}ÍÑ…ÉÑÕÀí•Á}‰…­É½Õ¹¹Ù…±Õ”õl™±½…Ðœ°…Í¥¤œ°½™˜t¹¥¹±Õ‘•Ì¡Œ¹‰…­É½Õ¹‘}ÍÑå±”¤ýŒ¹‰…­É½Õ¹‘}ÍÑå±”è¡Œ¹‘•½É…Ñ¥½¹Í}•¹…‰±•ôôõ™…±Í”ü½™˜œè™±½…Ðœ¤ì(€•‘¥ÑAÉ½™¥±•=Ù•É±…ä¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤íÍ•ÑQ¥µ•½ÕÐ  ¤ôù•Á}¹…µ”¹™½ÕÌ ¤°ÌÀ¤ì)ô)™Õ¹Ñ¥½¸±½Í•‘¥ÑAÉ½™¥±” ¥í•‘¥ÑAÉ½™¥±•=Ù•É±…ä¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íô)™Õ¹Ñ¥½¸ÉÕ¹M•ÑÕÁÕ¥‘•É½µAÉ½™¥±” ¥í±½Í•‘¥ÑAÉ½™¥±” ¤í½Á•¹AÉ½™¥±•M•ÑÕÀ¡™…±Í”¤íô)…Íå¹Œ™Õ¹Ñ¥½¸Í…Ù•‘¥ÑAÉ½™¥±”¡‰Ñ¸¥ì(€½¹ÍÐ‰½‘äõíÁÉ½™¥±•}¹…µ”é•Á}¹…µ”¹Ù…±Õ”¹ÑÉ¥´ ¤±ÁÉ•™•ÉÉ•‘}±…¹Õ…”é•Á}±…¹œ¹Ù…±Õ”±ÁÉ½™¥±•}•µ‰±•´é}•‘¥ÑAÉ½™¥±•µ‰±•´±ÍÑ…ÉÑ}Í•Ñ¥½¸é•Á}ÍÑ…ÉÐ¹Ù…±Õ”±µå±¥ÍÑ}±…å½ÕÐé•Á}±…å½ÕÐ¹Ù…±Õ”±¡•­}Í¡½ÝÍ}½¹}ÍÑ…ÉÑÕÀé•Á}¡•­Í¡½ÝÌ¹¡•­•±É•™É•Í¡}¥ÁÑÙ}½¹}ÍÑ…ÉÑÕÀé•Á}É•™É•Í¡¥ÁÑØ¹¡•­•±É•™É•Í¡}ÍÁ½ÉÑÍ}½¹}ÍÑ…ÉÑÕÀé•Á}É•™É•Í¡ÍÁ½ÉÑÌ¹¡•­•±‰…­É½Õ¹‘}ÍÑå±”é•Á}‰…­É½Õ¹¹Ù…±Õ”±‘•½É…Ñ¥½¹Í}•¹…‰±•é•Á}‰…­É½Õ¹¹Ù…±Õ”„ôô½™˜ôì(€¥˜ …‰½‘ä¹ÁÉ½™¥±•}¹…µ”¥í•Á}¹…µ”¹™½ÕÌ ¤íÑ½…ÍÐ¡ÑÈ ¹Ñ•È„ÁÉ½™¥±”¹…µ”¸œ¤¤íÉ•ÑÕÉ¸íõ¥˜¡‰½‘ä¹µå±¥ÍÑ}±…å½ÕÐôôôÑ¥µ•±¥¹”œ˜™‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôôôµåÑ¥µ•±¥¹”œ¥‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôµå±¥ÍÐœí¥˜ …}…µ•Í¹…‰±•˜™‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôôô…µ•Ìœ¥‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôµå±¥ÍÐœí¥˜ …}˜Å¹…‰±•˜™‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôôôÉ…¥¹œœ¥‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôµå±¥ÍÐœí¥˜ …}™½½Ñ‰…±±¹…‰±•˜™‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôôôÑ•…µÌœ¥‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôµå±¥ÍÐœì(€½¹ÍÐ½±õ‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðí‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐôM…Ù¥¹œ¸¸¸œì(€ÑÉåí½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½½¹™¥œœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡‰½‘ä¥ô¤í¥˜ …È¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È Í…Ù”™…¥±•œ¤íÍ•Ñ1…¹œ¡‰½‘ä¹ÁÉ•™•ÉÉ•‘}±…¹Õ…”¤í…ÁÁ±åAÉ½™¥±•½¹™¥œ¡‰½‘ä¤í±½Í•‘¥ÑAÉ½™¥±” ¤íÑ½…ÍÐ¡ÑÈ AÉ½™¥±”Í…Ù•¸œ¤¤íõ…Ñ ¡”¥íÑ½…ÍÐ¡ÑÈ ½Õ±¹½ÐÍ…Ù”ÁÉ½™¥±”¸œ¤¤íô(€‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±ì)ô)™Õ¹Ñ¥½¸…ÁÁ±å5å1¥ÍÑ1…å½ÕÐ ¥ì(€½¹ÍÐ‘…Í õ‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½È œµå±¥ÍÑY¥•Ü€¹µå‘…Í œ¤í¥˜ …‘…Í ¥É•ÑÕÉ¸ì(€½¹ÍÐ…±±½Ý•õl‰…±…¹•œ°ÍÁ½Ñ±¥¡Ðœ°Ñ¥µ•±¥¹”œ°¡Õˆtì(€}µå1¥ÍÑ1…å½ÕÐõ…±±½Ý•¹¥¹±Õ‘•Ì¡}ÁÉ½™¥±•½¹™¥œ¹µå±¥ÍÑ}±…å½ÕÐ¤ý}ÁÉ½™¥±•½¹™¥œ¹µå±¥ÍÑ}±…å½ÕÐèÑ¥µ•±¥¹”œì(€‘…Í ¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ±…å½ÕÐµ‰…±…¹•œ°±…å½ÕÐµÍÁ½Ñ±¥¡Ðœ°±…å½ÕÐµÑ¥µ•±¥¹”œ°±…å½ÕÐµ¡Õˆœ¤ì(€‘…Í ¹±…ÍÍ1¥ÍÐ¹…‘ ±…å½ÕÐ´œ­}µå1¥ÍÑ1…å½ÕÐ¤ì(€½¹ÍÐÑ¥µ•±¥¹”õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑQ¥µ•±¥¹•	±½¬œ¤ì(€½¹ÍÐÍ¡½ÝÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑM¡½ÝÍ	±½¬œ¤±Ñ•…µÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑQ•…µÍ	±½¬œ¤ì(€¥˜¡Ñ¥µ•±¥¹”¥Ñ¥µ•±¥¹”¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ±}µå1¥ÍÑ1…å½ÕÐ„ôôÑ¥µ•±¥¹”œ¤ì(€¥˜¡Í¡½ÝÌ¥Í¡½ÝÌ¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ±}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ¤ì(€¥˜¡Ñ•…µÌ¥Ñ•…µÌ¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°„¡}™½½Ñ‰…±±¹…‰±•‘ññ}˜Å¹…‰±•¤¤ì(€½¹ÍÐÑ¥µ•±¥¹•9…Øõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ¹…Ù5åÑ¥µ•±¥¹”œ¤±Ñ¥µ•±¥¹•MÑ…ÉÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ…ÉÑQ¥µ•±¥¹•=ÁÑ¥½¸œ¤ì(€¥˜¡Ñ¥µ•±¥¹•9…Ø¥Ñ¥µ•±¥¹•9…Ø¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ±}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ¤ì(€¥˜¡Ñ¥µ•±¥¹•MÑ…ÉÐ¥Ñ¥µ•±¥¹•MÑ…ÉÐ¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ±}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ¤ì(€É•¹‘•É5å1¥ÍÑQ¥µ•±¥¹” ¤ì)ô)™Õ¹Ñ¥½¸…ÁÁ±åAÉ½™¥±•½¹™¥œ¡Œ¥ì(€½¹ÍÐÁÉ•Ù½½Ñ‰…±°õ}™½½Ñ‰…±±¹…‰±•±ÁÉ•ÙI…¥¹œõ}˜Å¹…‰±•±ÁÉ•Ù…µ•Ìõ}…µ•Í¹…‰±•ì(€}ÁÉ½™¥±•½¹™¥œõ=‰©•Ð¹…ÍÍ¥¸¡íô±}ÁÉ½™¥±•½¹™¥œ±ññíô¤ì(€}Í•±•Ñ•‘µ‰±•´õ}AI=%1}5	15Mm}ÁÉ½™¥±•½¹™¥œ¹ÁÉ½™¥±•}•µ‰±•µtý}ÁÉ½™¥±•½¹™¥œ¹ÁÉ½™¥±•}•µ‰±•´èÑÙÍÑ…¬œì(€}µå1¥ÍÑ1…å½ÕÐõl‰…±…¹•œ°ÍÁ½Ñ±¥¡Ðœ°Ñ¥µ•±¥¹”œ°¡Õˆt¹¥¹±Õ‘•Ì¡}ÁÉ½™¥±•½¹™¥œ¹µå±¥ÍÑ}±…å½ÕÐ¤ý}ÁÉ½™¥±•½¹™¥œ¹µå±¥ÍÑ}±…å½ÕÐèÑ¥µ•±¥¹”œì(€}™½½Ñ‰…±±¹…‰±•õ}ÁÉ½™¥±•½¹™¥œ¹™½½Ñ‰…±±}•¹…‰±•„ôõ™…±Í”ì(€}˜Å¹…‰±•õ}ÁÉ½™¥±•½¹™¥œ¹˜Å}•¹…‰±•„ôõ™…±Í”ì(€}…µ•Í¹…‰±•õ}ÁÉ½™¥±•½¹™¥œ¹…µ•Í}•¹…‰±•„ôõ™…±Í”ì(€½¹ÍÐ™•…ÑÕÉ•¡…¹•õÁÉ•Ù½½Ñ‰…±°„ôõ}™½½Ñ‰…±±¹…‰±•‘ññÁÉ•ÙI…¥¹œ„ôõ}˜Å¹…‰±•‘ññÁÉ•Ù…µ•Ì„ôõ}…µ•Í¹…‰±•ì(€¥˜¡™•…ÑÕÉ•¡…¹•¥}µå1¥ÍÑ1½…‘•õ™…±Í”ì(€¥˜ …}™½½Ñ‰…±±¹…‰±•¥}µå1¥ÍÑQ•…µ5½µ•¹ÑÌõmtì(€¥˜ …}˜Å¹…‰±•¥í}µå1¥ÍÑÅ5½µ•¹ÑÌõmtí}µå1¥ÍÑI…¥¹É¥Ù•ÉÌõmtíô(€¥˜ …}…µ•Í¹…‰±•¥}µå1¥ÍÑ…µ•5½µ•¹ÑÌõmtì(€½¹ÍÐ¹…Øõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ¹…ÙQ•…µÌœ¤±Ñ•…µ	±½¬õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑQ•…µÍ	±½¬œ¤±ÍÁ½ÉÑ!•…‘¥¹œõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑMÁ½ÉÑ!•…‘¥¹œœ¤±…µ•Í9…Øõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ¹…Ù…µ•Ìœ¤±…µ•ÍMÑ…ÉÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ…ÉÑ…µ•Í=ÁÑ¥½¸œ¤±É…¥¹9…Øõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ¹…ÙI…¥¹œœ¤±É…¥¹MÑ…ÉÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ…ÉÑI…¥¹=ÁÑ¥½¸œ¤ì(€¥˜¡¹…Ø¥¹…Ø¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…}™½½Ñ‰…±±¹…‰±•¤ì(€¥˜¡Ñ•…µ	±½¬¥Ñ•…µ	±½¬¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°„¡}™½½Ñ‰…±±¹…‰±•‘ññ}˜Å¹…‰±•¤¤ì(€¥˜¡ÍÁ½ÉÑ!•…‘¥¹œ¥ÍÁ½ÉÑ!•…‘¥¹œ¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…}™½½Ñ‰…±±¹…‰±•¤ì(€¥˜¡…µ•Í9…Ø¥…µ•Í9…Ø¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…}…µ•Í¹…‰±•¤ì(€¥˜¡…µ•ÍMÑ…ÉÐ¥…µ•ÍMÑ…ÉÐ¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…}…µ•Í¹…‰±•¤ì(€¥˜¡É…¥¹9…Ø¥É…¥¹9…Ø¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…}˜Å¹…‰±•¤ì(€¥˜¡É…¥¹MÑ…ÉÐ¥É…¥¹MÑ…ÉÐ¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…}˜Å¹…‰±•¤ì((€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹˜Å•…ÑÕÉ”œ¤¹™½É… ¡•°ôù•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…}˜Å¹…‰±•¤¤ì(€ÕÁ‘…Ñ•AÉ½™¥±•9…µ”¡}ÁÉ½™¥±•½¹™¥œ¹ÁÉ½™¥±•}¹…µ”¤ì(€…ÁÁ±å	…­É½Õ¹‘MÑå±”¡}ÁÉ½™¥±•½¹™¥œ¹‰…­É½Õ¹‘}ÍÑå±•ñð¡}ÁÉ½™¥±•½¹™¥œ¹‘•½É…Ñ¥½¹Í}•¹…‰±•ôôõ™…±Í”ü½™˜œè™±½…Ðœ¤¤ì(€É•¹‘•É5å1¥ÍÑAÉ½™¥±” ¤ì(€…ÁÁ±å5å1¥ÍÑ1…å½ÕÐ ¤ì)ô()±•Ð}™…ÙQ•…µM•Ðõ¹•ÜM•Ð ¤±}™…ÙQ•…µI½ÝÌõmt±}µåQ•…µ¥áÑÕÉ•Ìõmt±}ÍÁ½ÉÑÍY¥Í¥‰±•¥áÑÕÉ•Ìõmt±}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåQ¥µ•Èõ¹Õ±°±}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåI•ÅÕ•ÍÐôÀ±}Í•±•Ñ•‘Q•…µ9…µ”ôœœ±}Í•±•Ñ•‘Q•…µI½Üõ¹Õ±°±}Í•±•Ñ•‘Q•…µAÉ½™¥±”õ¹Õ±°±}Ñ•…µAÉ½™¥±•I•ÄôÀ±}Ñ•…µ••Á1¥¹¬õ¹Õ±°±}™¥áÑÕÉ•M•…É¡Q•…µ%ôœœì)™Õ¹Ñ¥½¸ÍÁ½ÉÑÍ¥áÑÕÉ•-•ä¡˜¥íÉ•ÑÕÉ¸mMÑÉ¥¹œ¡˜¹¡½µ•ñðœœ¤¹ÑÉ¥´ ¤¹Ñ½1½Ý•É…Í” ¤±MÑÉ¥¹œ¡˜¹…Ý…åñðœœ¤¹ÑÉ¥´ ¤¹Ñ½1½Ý•É…Í” ¤±MÑÉ¥¹œ¡˜¹ÍÑ…ÉÑñðœœ¤¹Í±¥” À°ÄØ¥t¹©½¥¸ ðœ¤íô)™Õ¹Ñ¥½¸™¥áÑÕÉ•±…ÁÍ•‘5¥¹ÕÑ•Ì¡˜¥í½¹ÍÐÑÌõ˜˜™˜¹ÍÑ…ÉÐý¹•Ü…Ñ”¡˜¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤é9…8íÉ•ÑÕÉ¸9Õµ‰•È¹¥Í¥¹¥Ñ”¡ÑÌ¤ü¡…Ñ”¹¹½Ü ¤µÑÌ¤¼ØÀÀÀÀé¹Õ±°íô)™Õ¹Ñ¥½¸™¥áÑÕÉ•%Í1¥Ù”¡˜¥ì(€¥˜ …™ññ˜¹¥Í}™¥¹¥Í¡•¥É•ÑÕÉ¸™…±Í”í½¹ÍÐµ¥¹Ìõ™¥áÑÕÉ•±…ÁÍ•‘5¥¹ÕÑ•Ì¡˜¤í¥˜¡µ¥¹Ìôôõ¹Õ±±ññµ¥¹ÌðÀ¥É•ÑÕÉ¸™…±Í”ì(€€¼¼9•Ù•È±•Ð„ÍÑ…±”ÁÉ½Ù¥‘•È™±…œ½È­¥­½™˜•ÍÑ¥µ…Ñ”É•…Ñ”€ÏŠLÐ¡½ÕÈ…µ•Ì¸(€¥˜¡˜¹¥Í}±¥Ù”¥É•ÑÕÉ¸µ¥¹ÌðôÄÔÀì(€É•ÑÕÉ¸€…˜¹ÍÑ…ÑÕÍ}­¹½Ý¸˜™µ¥¹ÌðôÄÌÔì)ô)™Õ¹Ñ¥½¸™¥áÑÕÉ•%ÍI••¹Ð¡˜¥í½¹ÍÐµ¥¹Ìõ™¥áÑÕÉ•±…ÁÍ•‘5¥¹ÕÑ•Ì¡˜¤íÉ•ÑÕÉ¸µ¥¹Ì„ôõ¹Õ±°˜™µ¥¹ÌøôÀ˜™µ¥¹ÌðôÌØÀ˜˜…™¥áÑÕÉ•%Í1¥Ù”¡˜¤íô)™Õ¹Ñ¥½¸™…Ù½É¥Ñ•Q•…µI½Ü¡Ð¥íÉ•ÑÕÉ¸í¹…µ”éMÑÉ¥¹œ¡ÑåÁ•½˜ÐôôôÍÑÉ¥¹œœýÐè¡Ð¹¹…µ•ñðœœ¤¤±Ñ•…µ}¥éMÑÉ¥¹œ¡ÑåÁ•½˜ÐôôôÍÑÉ¥¹œœüœœè¡Ð¹Ñ•…µ}¥‘ñðœœ¤¤±±½¼éMÑÉ¥¹œ¡ÑåÁ•½˜ÐôôôÍÑÉ¥¹œœüœœè¡Ð¹±½½ñðœœ¤¥ôíô)™Õ¹Ñ¥½¸É•¹‘•ÉQ•…µ…Ù½É¥Ñ•I…¥° ¥ì(€½¹ÍÐÉ…¥°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µ…Ù1¥ÍÐœ¤í¥˜ …É…¥°¥É•ÑÕÉ¸ì(€¥˜ …}™…ÙQ•…µI½ÝÌ¹±•¹Ñ ¥íÉ…¥°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 9¼™…Ù½É¥Ñ”Ñ•…µÌå•Ð¸œ¤¤¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€É…¥°¹¥¹¹•É!Q50õ}™…ÙQ•…µI½ÝÌ¹µ…À¡Ðôùí½¹ÍÐÍÉŒõÐ¹±½½ñð¡Ð¹Ñ•…µ}¥üœ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Ð¹Ñ•…µ}¥¤èœœ¤±Í•±•Ñ•õMÑÉ¥¹œ¡Ð¹¹…µ”¤¹Ñ½1½Ý•É…Í” ¤ôôõMÑÉ¥¹œ¡}Í•±•Ñ•‘Q•…µ9…µ”¤¹Ñ½1½Ý•É…Í” ¤íÉ•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰Ñ•…µ™…Ù¥Ñ•´œ¬¡Í•±•Ñ•üœÍ•±•Ñ•œèœœ¤¬œˆ‘…Ñ„µÑ•…´µÍ•…É ôˆœ­•ÍÑÑÈ¡Ð¹¹…µ”¤¬œˆ‘…Ñ„µÑ•…´µ¥ôˆœ­•ÍÑÑÈ¡Ð¹Ñ•…µ}¥¤¬œˆ‘…Ñ„µÑ•…´µ±½¼ôˆœ­•ÍÑÑÈ¡Ð¹±½¼¤¬œˆøœ¬¡ÍÉŒüœñ¥µœ±…ÍÌô‰Ñ•…µ™…Ù±½¼ˆÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñÍÁ…¸±…ÍÌô‰Ñ•…µ™…Ù¹…µ”ˆøœ­•ÍŒ¡Ð¹¹…µ”¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…È½¸Ñ•…µÉ•µ½Ù”ˆ‘…Ñ„µÑ•…´µ¹…µ”ôˆœ­•ÍÑÑÈ¡Ð¹¹…µ”¤¬œˆÑ¥Ñ±”ô‰I•µ½Ù”™É½´™…Ù½É¥Ñ•Ìˆø˜ŒäÜÌÌìð½ÍÁ…¸øð½‘¥Øøœíô¤¹©½¥¸ œœ¤ì)ô)™Õ¹Ñ¥½¸Í•±•Ñ•‘Q•…µ9•áÑ¥áÑÕÉ” ¥ì(€½¹ÍÐ¹…µ”õMÑÉ¥¹œ¡}Í•±•Ñ•‘Q•…µ9…µ•ñðœœ¤í¥˜ …¹…µ”¥É•ÑÕÉ¸¹Õ±°í½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤ì(€É•ÑÕÉ¸}µåQ•…µ¥áÑÕÉ•Ì¹™¥±Ñ•È¡˜ôùí½¹ÍÐ‰•±½¹Ìô¡˜¹™…Ù½É¥Ñ•}Ñ•…µÍññmt¤¹Í½µ”¡½Ý¹•Èôù}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡½Ý¹•È±¹…µ”¤¥ññ}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡˜¹¡½µ”±¹…µ”¥ññ}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡˜¹…Ý…ä±¹…µ”¤í¥˜ …‰•±½¹Ì¥É•ÑÕÉ¸™…±Í”í½¹ÍÐÑÌõ˜¹ÍÑ…ÉÐý¹•Ü…Ñ”¡˜¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤èÀíÉ•ÑÕÉ¸™¥áÑÕÉ•%Í1¥Ù”¡˜¥ññ™¥áÑÕÉ•%ÍI••¹Ð¡˜¥ñð¡ÑÌ˜™ÑÌù¹½Ü¤íô¤¹Í½ÉÐ ¡„±ˆ¤ôùí½¹ÍÐÉ…¹¬õ˜ôù™¥áÑÕÉ•%Í1¥Ù”¡˜¤üÀè¡™¥áÑÕÉ•%ÍI••¹Ð¡˜¤üÄèÈ¤±õÉ…¹¬¡„¤µÉ…¹¬¡ˆ¤í¥˜¡¥É•ÑÕÉ¸í¥˜¡É…¹¬¡„¤ôôôÄ¥É•ÑÕÉ¸¹•Ü…Ñ”¡ˆ¹ÍÑ…ÉÑñðÀ¤µ¹•Ü…Ñ”¡„¹ÍÑ…ÉÑñðÀ¤íÉ•ÑÕÉ¸¹•Ü…Ñ”¡„¹ÍÑ…ÉÑñðÀ¤µ¹•Ü…Ñ”¡ˆ¹ÍÑ…ÉÑñðÀ¤íô¥lÁuññ¹Õ±°ì)ô)™Õ¹Ñ¥½¸É•¹‘•ÉM•±•Ñ•‘Q•…µAÉ½™¥±”¡ÁÉ½™¥±”¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µAÉ½™¥±••Ñ…¥°œ¤í¥˜ …•°¥É•ÑÕÉ¸í½¹ÍÐÉ½Üõ}™…ÙQ•…µI½ÝÌ¹™¥¹¡ÐôùMÑÉ¥¹œ¡Ð¹¹…µ”¤¹Ñ½1½Ý•É…Í” ¤ôôõMÑÉ¥¹œ¡}Í•±•Ñ•‘Q•…µ9…µ”¤¹Ñ½1½Ý•É…Í” ¤¥ñð¡}Í•±•Ñ•‘Q•…µI½Ü˜™MÑÉ¥¹œ¡}Í•±•Ñ•‘Q•…µI½Ü¹¹…µ”¤¹Ñ½1½Ý•É…Í” ¤ôôõMÑÉ¥¹œ¡}Í•±•Ñ•‘Q•…µ9…µ”¤¹Ñ½1½Ý•É…Í” ¤ý}Í•±•Ñ•‘Q•…µI½Üé¹Õ±°¤í¥˜ …É½Ü¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ ¡½½Í”„Ñ•…´Ñ¼Í•”‘•Ñ…¥±Ì¸œ¤¤¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€ÁÉ½™¥±”õÁÉ½™¥±•ññ}Í•±•Ñ•‘Q•…µAÉ½™¥±•ññíôí½¹ÍÐÍÉŒõÁÉ½™¥±”¹±½½ññÉ½Ü¹±½½ñð¡É½Ü¹Ñ•…µ}¥üœ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡É½Ü¹Ñ•…µ}¥¤èœœ¤±µ•Ñ„õmÁÉ½™¥±”¹½Õ¹ÑÉä±ÁÉ½™¥±”¹±•…Õ•t¹™¥±Ñ•È¡	½½±•…¸¤¹©½¥¸ œƒ
Ü€œ¤ì(€½¹ÍÐ™…ÑÌõml!½µ”É½Õ¹œ±ÁÉ½™¥±”¹ÍÑ…‘¥ÕµñðŸŠPt±l!•…½… œ±ÁÉ½™¥±”¹½…¡ñðŸŠPt±l1•…Õ”œ±ÁÉ½™¥±”¹±•…Õ•ñðŸŠPt±l½Õ¹ÑÉäœ±ÁÉ½™¥±”¹½Õ¹ÑÉåñðŸŠPutì(€½¹ÍÐ¹•áÐõÍ•±•Ñ•‘Q•…µ9•áÑ¥áÑÕÉ” ¤í±•Ð¹•áÑ!Ñµ°ôœñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•¹•áÐˆøñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•¹•áÑ±…‰•°ˆøœ­•ÍŒ¡ÑÈ 9•áÐµ…Ñ œ¤¤¬œð½‘¥ØøñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 9¼ÕÁ½µ¥¹œ™¥áÑÕÉ”™½Õ¹¸œ¤¤¬œð½ÍÁ…¸øð½‘¥Øøœì(€¥˜¡¹•áÐ¥í½¹ÍÐ­¥¬õ¹•áÐ¹ÍÑ…ÉÐý¹•Ü…Ñ”¡¹•áÐ¹ÍÑ…ÉÐ¤é¹Õ±°±±¥Ù”õ™¥áÑÕÉ•%Í1¥Ù”¡¹•áÐ¤±É••¹Ðõ™¥áÑÕÉ•%ÍI••¹Ð¡¹•áÐ¤±Ý¡•¸õ±¥Ù”ýÑÈ 1¥Ù”¹½Üœ¤è¡­¥¬˜˜…9Õµ‰•È¹¥Í9…8¡­¥¬¹•ÑQ¥µ” ¤¤ý­¥¬¹Ñ½1½…±•MÑÉ¥¹œ¡}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•±íÝ••­‘…äèÍ¡½ÉÐœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐœ±¡½ÕÈèœÈµ‘¥¥Ðœ±µ¥¹ÕÑ”èœÈµ‘¥¥Ðô¤èœœ¤í¹•áÑ!Ñµ°ôœñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•¹•áÐˆøñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•¹•áÑ±…‰•°ˆøœ­•ÍŒ¡±¥Ù”ýÑÈ 1¥Ù”¹½Üœ¤è¡É••¹ÐüI••¹Ðµ…Ñ œéÑÈ 9•áÐµ…Ñ œ¤¤¤¬œð½‘¥Øøñˆøœ­•ÍŒ¡¹•áÐ¹¡½µ•ñðœœ¤¬œØ€œ­•ÍŒ¡¹•áÐ¹…Ý…åñðœœ¤¬œð½ˆøñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡Ý¡•¸¤¬¡É••¹Ðüœƒ
Ü•¹‘•œèœœ¤¬¡¹•áÐ¹±•…Õ•}¹…µ”üœƒ
Ü€œ­•ÍŒ¡¹•áÐ¹±•…Õ•}¹…µ”¤èœœ¤¬œð½ÍÁ…¸øð½‘¥Øøœíô(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•¡•É¼ˆøñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•‰…‘”ˆøœ¬¡ÍÉŒüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•¥‘•¹Ñ¥Ñäˆøñ Èøœ­•ÍŒ¡ÁÉ½™¥±”¹¹…µ•ññÉ½Ü¹¹…µ”¤¬œð½ Èøñ‘¥Ø±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡µ•Ñ…ññÑÈ ½½Ñ‰…±°œ¤¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•™…ÑÌˆøœ­™…ÑÌ¹µ…À¡˜ôøœñ‘¥Ø±…ÍÌô‰Ñ•…µÁÉ½™¥±•™…ÐˆøñÍÁ…¸øœ­•ÍŒ¡ÑÈ¡™lÁt¤¤¬œð½ÍÁ…¸øñˆÑ¥Ñ±”ôˆœ­•ÍÑÑÈ¡™lÅt¤¬œˆøœ­•ÍŒ¡™lÅt¤¬œð½ˆøð½‘¥Øøœ¤¹©½¥¸ œœ¤¬œð½‘¥Øøœ­¹•áÑ!Ñµ°ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘M•±•Ñ•‘Q•…µAÉ½™¥±”¡É½Ü¥ì(€É½ÜõÉ½Ýññ}™…ÙQ•…µI½ÝÌ¹™¥¹¡ÐôùMÑÉ¥¹œ¡Ð¹¹…µ”¤¹Ñ½1½Ý•É…Í” ¤ôôõMÑÉ¥¹œ¡}Í•±•Ñ•‘Q•…µ9…µ”¤¹Ñ½1½Ý•É…Í” ¤¤í¥˜ …É½Ü¥É•ÑÕÉ¸í½¹ÍÐÉ•Äô¬­}Ñ•…µAÉ½™¥±•I•Äí}Í•±•Ñ•‘Q•…µAÉ½™¥±”õí¹…µ”éÉ½Ü¹¹…µ”±Ñ•…µ}¥éÉ½Ü¹Ñ•…µ}¥±±½¼éÉ½Ü¹±½½ôíÉ•¹‘•ÉM•±•Ñ•‘Q•…µAÉ½™¥±”¡}Í•±•Ñ•‘Q•…µAÉ½™¥±”¤ì(€ÑÉåí½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½Ñ•…µ}ÁÉ½™¥±”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡É½Ü¹Ñ•…µ}¥‘ñðœœ¤¬œ™¹…µ”ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡É½Ü¹¹…µ”¤¤í¥˜¡É•Ä„ôõ}Ñ•…µAÉ½™¥±•I•Ä¥É•ÑÕÉ¸í}Í•±•Ñ•‘Q•…µAÉ½™¥±”õ=‰©•Ð¹…ÍÍ¥¸¡íô±È¹ÁÉ½™¥±•ññíô°í±½¼è¡È¹ÁÉ½™¥±•ññíô¤¹±½½ññÉ½Ü¹±½½ô¤íÉ•¹‘•ÉM•±•Ñ•‘Q•…µAÉ½™¥±”¡}Í•±•Ñ•‘Q•…µAÉ½™¥±”¤íõ…Ñ ¡”¥íô)ô)™Õ¹Ñ¥½¸Í•±•Ñ5åQ•…´¡¹…µ”±Ñ•…µ%±±½¼¥ì(€€¼¼M•±•Ñ¥¹œ„Ñ•…´¥Ì¹…Ù¥…Ñ¥½¸°¹½Ð„5…Ñ¡™¥¹‘•ÈÍ•…É ¸-••ÀÑ¡”ÑÝ¼(€€¼¼…Ñ¥½¹ÌÍ•Á…É…Ñ”Í¼‰É½ÝÍ¥¹œ™…Ù½É¥Ñ”½Ñ•…´µÉ•ÍÕ±Ð…É‘ÌÍÑ…åÌ¥¹ÍÑ…¹Ð…¹(€€¼¼‘½•Ì¹½ÐÕ¹•áÁ•Ñ•‘±äÉ•Á±…”Ñ¡”•áÁ±¥¥ÐÍ•…É É•ÍÕ±ÑÌ‰•±½Ü¸(€}Í•±•Ñ•‘Q•…µ9…µ”õMÑÉ¥¹œ¡¹…µ•ñðœœ¤í½¹ÍÐÉ½Üõ}™…ÙQ•…µI½ÝÌ¹™¥¹¡ÐôùMÑÉ¥¹œ¡Ð¹¹…µ”¤¹Ñ½1½Ý•É…Í” ¤ôôõ}Í•±•Ñ•‘Q•…µ9…µ”¹Ñ½1½Ý•É…Í” ¤¥ññí¹…µ”é}Í•±•Ñ•‘Q•…µ9…µ”±Ñ•…µ}¥éMÑÉ¥¹œ¡Ñ•…µ%‘ñðœœ¤±±½¼éMÑÉ¥¹œ¡±½½ñðœœ¥ôí}Í•±•Ñ•‘Q•…µI½ÜõÉ½ÜíÉ•¹‘•ÉQ•…µ…Ù½É¥Ñ•I…¥° ¤í±½…‘M•±•Ñ•‘Q•…µAÉ½™¥±”¡É½Ü¤ì)ô)™Õ¹Ñ¥½¸Ñ•…µ¥áÑÕÉ•…É¡˜±±¥Ù”±‘••Á1¥¹¬¥ì(€½¹ÍÐ­¥¬õ˜¹ÍÑ…ÉÐý¹•Ü…Ñ”¡˜¹ÍÑ…ÉÐ¤é¹Õ±°ì(€½¹ÍÐÝ¡•¸õ­¥¬˜˜…9Õµ‰•È¹¥Í9…8¡­¥¬¹•ÑQ¥µ” ¤¤ý­¥¬¹Ñ½1½…±•MÑÉ¥¹œ¡}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•±íÝ••­‘…äèÍ¡½ÉÐœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐœ±¡½ÕÈèœÈµ‘¥¥Ðœ±µ¥¹ÕÑ”èœÈµ‘¥¥Ðô¤èœœì(€±•ÐÍÑ…ÑÕÌôœœì(€¥˜¡±¥Ù”˜™­¥¬¥ì(€€€½¹ÍÐ•ÍÑ¥µ…Ñ•õ5…Ñ ¹µ…à À±5…Ñ ¹™±½½È ¡…Ñ”¹¹½Ü ¤µ­¥¬¹•ÑQ¥µ” ¤¤¼ØÀÀÀÀ¤¤ì(€€€½¹ÍÐ¡…Í±½¬õ˜¹±¥Ù•}µ¥¹ÕÑ”„ôõ¹Õ±°˜™˜¹±¥Ù•}µ¥¹ÕÑ”„ôõÕ¹‘•™¥¹•˜™9Õµ‰•È¹¥Í¥¹¥Ñ”¡9Õµ‰•È¡˜¹±¥Ù•}µ¥¹ÕÑ”¤¤ì(€€€½¹ÍÐµ¥¹Ìõ¡…Í±½¬ý9Õµ‰•È¡˜¹±¥Ù•}µ¥¹ÕÑ”¤é•ÍÑ¥µ…Ñ•ì(€€€ÍÑ…ÑÕÌôœñÍÁ…¸±…ÍÌô‰±¥Ù”ˆø˜ŒäØÜäì1%Y€œ­µ¥¹Ì¬œµ¥¸ð½ÍÁ…¸øœì(€õ•±Í”¥˜¡™¥áÑÕÉ•%ÍI••¹Ð¡˜¤¥ÍÑ…ÑÕÌôœñÍÁ…¸±…ÍÌô‰•¹‘•ˆù•¹‘•€¼•…É±¥•ÈÑ½‘…äð½ÍÁ…¸øœì(€½¹ÍÐ½Ý¹•ÉÌô¡˜¹™…Ù½É¥Ñ•}Ñ•…µÍññmt¤¹©½¥¸ œ°€œ¤ì(€½¹ÍÐÅÕ•Éäô¡˜¹¡½µ•ñðœœ¤¬œ€œ¬¡˜¹…Ý…åñðœœ¤±µ…Ñ¡EÕ•Éäô¡˜¹™…Ù½É¥Ñ•}Ñ•…µÍññmt¥lÁuññ˜¹¡½µ•ññ˜¹…Ý…åñðœœì(€½¹ÍÐ¡½µ•1½¼õ˜¹¡½µ•}¥üœñ¥µœ±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•±½¼ˆÍÉŒôˆ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡˜¹¡½µ•}¥¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€½¹ÍÐ…Ý…å1½¼õ˜¹…Ý…å}¥üœñ¥µœ±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•±½¼ˆÍÉŒôˆ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡˜¹…Ý…å}¥¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€½¹ÍÐ½µÁ•Ñ¥Ñ¥½¸õ˜¹±•…Õ•}¹…µ”üœñ‘¥Ø±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•½µÁ•Ñ¥Ñ¥½¸ˆøœ­•ÍŒ¡˜¹±•…Õ•}¹…µ”¤¬œð½‘¥Øøœèœœì(€½¹ÍÐ­¹½Ý¹¡…¹¹•±Ìõl¸¸¸¡˜¹µ…Ñ¡•Íññmt¤°¸¸¸¡˜¹ÁÁÙ}¡¥ÑÍññmt¥t±¡…Í¡…¹¹•±Ìõ­¹½Ý¹¡…¹¹•±Ì¹±•¹Ñ øÀì(€½¹ÍÐ¡…¹¹•±!Ñµ°ô¡¡…Í¡…¹¹•±Íññ˜¹…Ù…¥±…‰¥±¥Ñå}¡•­•¤ý™¥áÑÕÉ•MÑ½É•‘¡…¹¹•±Í!Ñµ°¡=‰©•Ð¹…ÍÍ¥¸¡í±½•‘}¥¸éÑÉÕ•ô±˜¤¤èœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ ¡•­¥¹œå½ÕÈ¡…¹¹•±Ì¸¸¸œ¤¤¬œð½ÍÁ…¸øœì(€½¹ÍÐ‘•Ñ…¥±Ìôœñ‘¥Ø±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•‰É½…‘…ÍÑÌ¡¥‘”ˆøñ‘¥Ø±…ÍÌô‰™¥áÑÕÉ•¡…¹¹•±É•ÍÕ±ÑÌˆÍÑå±”ô‰Ý¥‘Ñ èÄÀÀ”ˆøœ­¡…¹¹•±!Ñµ°¬œð½‘¥Øøð½‘¥Øøœì(€½¹ÍÐ™¥áÑÕÉ•ÑÑÉÌôœ‘…Ñ„µ™¥áÑÕÉ”µ…ÉôˆÄˆœ¬¡‘••Á1¥¹¬üœ‘…Ñ„µÁÉ½™¥±”µ™¥áÑÕÉ”ôˆÄˆœèœœ¤¬œ‘…Ñ„µ•Ù•¹Ðµ­•äôˆœ­•ÍÑÑÈ¡ÍÁ½ÉÑÍ¥áÑÕÉ•-•ä¡˜¤¤¬œˆ‘…Ñ„µ¡½µ”ôˆœ­•ÍÑÑÈ¡˜¹¡½µ•ñðœœ¤¬œˆ‘…Ñ„µ…Ý…äôˆœ­•ÍÑÑÈ¡˜¹…Ý…åñðœœ¤¬œˆ‘…Ñ„µÍÑ…ÉÐôˆœ­•ÍÑÑÈ¡˜¹ÍÑ…ÉÑñðœœ¤¬œˆ‘…Ñ„µÍ•…É ôˆœ­•ÍÑÑÈ¡µ…Ñ¡EÕ•Éä¤¬œˆœì(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰Ñ•…µ™¥áÑÕÉ”¡…ÍÑØœ¬¡±¥Ù”üœ±¥Ù•™¥áÑÕÉ”œèœœ¤¬¡¡…Í¡…¹¹•±Ìüœ¡…Í¡…¹¹•±Ìœèœœ¤¬œˆœ­™¥áÑÕÉ•ÑÑÉÌ¬œøñ‘¥Ø±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•Ñ•…µÌˆøñÍÁ…¸±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•Í¥‘”ˆøœ­¡½µ•1½¼­•ÍŒ¡˜¹¡½µ”¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•ÙÌˆùØð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•Í¥‘”ˆøœ­…Ý…å1½¼­•ÍŒ¡˜¹…Ý…ä¤¬œð½ÍÁ…¸øœ(€€€€¬¡¡…Í¡…¹¹•±ÌüœñÍÁ…¸±…ÍÌô‰ŒÑ•…µ™¥áÑÕÉ•ÑØˆùQXð½ÍÁ…¸øœèœœ¤¬œð½‘¥Øøœ(€€€€­½µÁ•Ñ¥Ñ¥½¸¬œñ‘¥Ø±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡Ý¡•¸¤¬œ€œ­ÍÑ…ÑÕÌ¬œð½‘¥Øøœ¬¡½Ý¹•ÉÌüœñ‘¥Ø±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•½Ý¹•Èˆøœ­•ÍŒ¡½Ý¹•ÉÌ¤¬œð½‘¥Øøœèœœ¤­‘•Ñ…¥±Ì¬œð½‘¥Øøœì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘5åQ•…µÌ ¥ì(€½¹ÍÐ™…Øõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ¤°Ñ•…µÌõ™…Ø¹Ñ•…µÍññmtì(€}™…ÙQ•…µM•Ðõ¹•ÜM•Ð¡Ñ•…µÌ¹µ…À¡ÐôùMÑÉ¥¹œ¡ÑåÁ•½˜ÐôôôÍÑÉ¥¹œœýÐéÐ¹¹…µ”¤¹Ñ½1½Ý•É…Í” ¤¤¤ì(€}™…ÙQ•…µI½ÝÌõÑ•…µÌ¹µ…À¡™…Ù½É¥Ñ•Q•…µI½Ü¤¹™¥±Ñ•È¡ÐôùÐ¹¹…µ”¤ì(€¥˜¡}™…ÙQ•…µI½ÝÌ¹±•¹Ñ ˜˜…}™…ÙQ•…µI½ÝÌ¹Í½µ”¡ÐôùMÑÉ¥¹œ¡Ð¹¹…µ”¤¹Ñ½1½Ý•É…Í” ¤ôôõMÑÉ¥¹œ¡}Í•±•Ñ•‘Q•…µ9…µ”¤¹Ñ½1½Ý•É…Í” ¤¤¥}Í•±•Ñ•‘Q•…µ9…µ”õ}™…ÙQ•…µI½ÝÍlÁt¹¹…µ”ì(€¥˜ …}™…ÙQ•…µI½ÝÌ¹±•¹Ñ ¥}Í•±•Ñ•‘Q•…µ9…µ”ôœœì(€É•¹‘•ÉQ•…µ…Ù½É¥Ñ•I…¥° ¤ì(€½¹ÍÐÍ•±•Ñ•‘I½Üõ}™…ÙQ•…µI½ÝÌ¹™¥¹¡ÐôùMÑÉ¥¹œ¡Ð¹¹…µ”¤¹Ñ½1½Ý•É…Í” ¤ôôõMÑÉ¥¹œ¡}Í•±•Ñ•‘Q•…µ9…µ”¤¹Ñ½1½Ý•É…Í” ¤¤í}Í•±•Ñ•‘Q•…µI½ÜõÍ•±•Ñ•‘I½Ýññ¹Õ±°í¥˜¡Í•±•Ñ•‘I½Ü¥±½…‘M•±•Ñ•‘Q•…µAÉ½™¥±”¡Í•±•Ñ•‘I½Ü¤í•±Í”É•¹‘•ÉM•±•Ñ•‘Q•…µAÉ½™¥±”¡¹Õ±°¤ì(€½¹ÍÐÕÁ½µ¥¹œõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µUÁ½µ¥¹1¥ÍÐœ¤°±¥Ù•1¥ÍÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µ1¥Ù•1¥ÍÐœ¤°±¥Ù•M•Ñ¥½¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µ1¥Ù•M•Ñ¥½¸œ¤ì(€½¹ÍÐÑ½ÁM•Ñ¥½¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µQ½ÁM•Ñ¥½¸œ¤±Ñ½Á1¥ÍÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µQ½Á1¥ÍÐœ¤ì(€¥˜ …Ñ•…µÌ¹±•¹Ñ ¥í±¥Ù•M•Ñ¥½¸¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íÕÁ½µ¥¹œ¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù‘„™…Ù½É¥Ñ”Ñ•…´Ñ¼Í•”¥ÑÌ™¥áÑÕÉ•Ì¸ð½ÍÁ…¸øœíô(€¥˜ …}µåQ•…µ¥áÑÕÉ•Ì¹±•¹Ñ ¥ÕÁ½µ¥¹œ¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù1½…‘¥¹œ™¥áÑÕÉ•Ì¸¸¸ð½ÍÁ…¸øœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½µå}Ñ•…µÌœ¤ì(€¥˜¡È¹•ÉÉ½È¥íÕÁ½µ¥¹œ¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡È¹•ÉÉ½È¤¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€}µåQ•…µ¥áÑÕÉ•ÌõÈ¹™¥áÑÕÉ•Íññmtí}ÍÁ½ÉÑÍY¥Í¥‰±•¥áÑÕÉ•Ìõl¸¸¹}µåQ•…µ¥áÑÕÉ•Ì°¸¸¸¡È¹Ñ½Á}™¥áÑÕÉ•Íññmt¥tíÉ•¹‘•ÉM•±•Ñ•‘Q•…µAÉ½™¥±”¡}Í•±•Ñ•‘Q•…µAÉ½™¥±”¤ì(€½¹ÍÐ±¥Ù”õmt°É••¹Ðõmt°™ÕÑÕÉ”õmtì(€™½È¡½¹ÍÐ˜½˜€¡È¹™¥áÑÕÉ•Íññmt¤¥ì(€€€½¹ÍÐÑÌõ˜¹ÍÑ…ÉÐý¹•Ü…Ñ”¡˜¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤èÀ°µ¥¹ÌõÑÌü¡…Ñ”¹¹½Ü ¤µÑÌ¤¼ØÀÀÀÀé¹Õ±°ì(€€€¥˜¡™¥áÑÕÉ•%Í1¥Ù”¡˜¤¥±¥Ù”¹ÁÕÍ ¡˜¤ì(€€€•±Í”¥˜¡™¥áÑÕÉ•%ÍI••¹Ð¡˜¤¥É••¹Ð¹ÁÕÍ ¡˜¤ì(€€€•±Í”¥˜¡µ¥¹Ì„ôõ¹Õ±°˜™µ¥¹ÌðÀ¥™ÕÑÕÉ”¹ÁÕÍ ¡˜¤ì(€ô(€¥˜¡±¥Ù”¹±•¹Ñ ¥í±¥Ù•1¥ÍÐ¹¥¹¹•É!Q50õ±¥Ù”¹µ…À¡˜ôùÑ•…µ¥áÑÕÉ•…É¡˜±ÑÉÕ”¤¤¹©½¥¸ œœ¤í±¥Ù•M•Ñ¥½¸¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤íô(€•±Í•í±¥Ù•1¥ÍÐ¹¥¹¹•É!Q50ôœœí±¥Ù•M•Ñ¥½¸¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íô(€½¹ÍÐÑ½Á¥áÑÕÉ•ÌõÈ¹Ñ½Á}™¥áÑÕÉ•Íññmtì(€¥˜¡Ñ½Á¥áÑÕÉ•Ì¹±•¹Ñ ¥ì(€€€Ñ½Á1¥ÍÐ¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰Ñ½Á™¥áÑÕÉ•É¥ˆøœ­Ñ½Á¥áÑÕÉ•Ì¹µ…À ¡˜±¤¤ôøœñ‘¥Ø±…ÍÌô‰Ñ½Á™¥áÑÕÉ•¥Ñ•´œ¬¡¤øôÄÈüœÑ½Á™¥áÑÕÉ••áÑÉ„¡¥‘”œèœœ¤¬œˆøœ­Ñ•…µ¥áÑÕÉ•…É¡˜±™¥áÑÕÉ•%Í1¥Ù”¡˜¤¤¬œð½‘¥Øøœ¤¹©½¥¸ œœ¤¬œð½‘¥Øøœ(€€€€€€¬¡Ñ½Á¥áÑÕÉ•Ì¹±•¹Ñ øÄÈüœñ‘¥Ø±…ÍÌô‰Ñ½Á™¥áÑÕÉ•µ½É”ˆøñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐˆ½¹±¥¬ô‰Ñ½±•Q½Á¥áÑÕÉ•Ì¡Ñ¡¥Ì¤ˆøœ­ÑÈ M¡½Üµ½É”µ…Ñ¡•Ìœ¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøœèœœ¤ì(€€€Ñ½ÁM•Ñ¥½¸¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€õ•±Í•íÑ½Á1¥ÍÐ¹¥¹¹•É!Q50ôœœíÑ½ÁM•Ñ¥½¸¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íô(€±•ÐÕÁ½µ¥¹!Ñµ°ôœœì(€™½È¡½¹ÍÐÑ•…´½˜Ñ•…µÌ¥ì(€€€½¹ÍÐ¹…µ”õMÑÉ¥¹œ¡ÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœýÑ•…´éÑ•…´¹¹…µ•ñðœœ¤°­•äõ¹…µ”¹Ñ½1½Ý•É…Í” ¤ì(€€€½¹ÍÐÉ••¹Ñ¥áÑÕÉ•ÌõÉ••¹Ð¹™¥±Ñ•È¡˜ôø¡˜¹™…Ù½É¥Ñ•}Ñ•…µÍññmt¤¹Í½µ”¡½Ý¹•ÈôùMÑÉ¥¹œ¡½Ý¹•È¤¹Ñ½1½Ý•É…Í” ¤ôôõ­•ä¤¤¹Í½ÉÐ ¡„±ˆ¤ôù¹•Ü…Ñ”¡ˆ¹ÍÑ…ÉÐ¤µ¹•Ü…Ñ”¡„¹ÍÑ…ÉÐ¤¤ì(€€€½¹ÍÐ™ÕÑÕÉ•¥áÑÕÉ•Ìõ™ÕÑÕÉ”¹™¥±Ñ•È¡˜ôø¡˜¹™…Ù½É¥Ñ•}Ñ•…µÍññmt¤¹Í½µ”¡½Ý¹•ÈôùMÑÉ¥¹œ¡½Ý¹•È¤¹Ñ½1½Ý•É…Í” ¤ôôõ­•ä¤¤ì(€€€½¹ÍÐÑ•…µ¥áÑÕÉ•Ìõl¸¸¹É••¹Ñ¥áÑÕÉ•Ì°¸¸¹™ÕÑÕÉ•¥áÑÕÉ•Ít¹Í±¥” À°Ð¤ì(€€€¥˜ …Ñ•…µ¥áÑÕÉ•Ì¹±•¹Ñ ¥½¹Ñ¥¹Õ”ì(€€€ÕÁ½µ¥¹!Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰Ñ•…µÕÁ½µ¥¹É½ÕÀˆøñ‘¥Ø±…ÍÌô‰Ñ•…µÕÁ½µ¥¹¹…µ”ˆøœ­•ÍŒ¡¹…µ”¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰Ñ•…µ™¥áÑÕÉ•É¥ˆøœ­Ñ•…µ¥áÑÕÉ•Ì¹µ…À¡˜ôùÑ•…µ¥áÑÕÉ•…É¡˜±™¥áÑÕÉ•%Í1¥Ù”¡˜¤¤¤¹©½¥¸ œœ¤¬œð½‘¥Øøð½‘¥Øøœì(€ô(€ÕÁ½µ¥¹œ¹¥¹¹•É!Q50õÕÁ½µ¥¹!Ñµ±ñð¡Ñ•…µÌ¹±•¹Ñ üœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼ÕÁ½µ¥¹œ™¥áÑÕÉ•Ì™½Õ¹¸ð½ÍÁ…¸øœèœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù‘„™…Ù½É¥Ñ”Ñ•…´Ñ¼Í•”¥ÑÌ™¥áÑÕÉ•Ì¸ð½ÍÁ…¸øœ¤ì(€±½…‘MÁ½ÉÑÍÙ…¥±…‰¥±¥Ñä¡™…±Í”¤ì)ô()™Õ¹Ñ¥½¸…ÁÁ±åMÁ½ÉÑÍÙ…¥±…‰¥±¥Ñä¡…Ù…¥±…‰¥±¥Ñä¥ì(€½¹ÍÐµ…Àõ…Ù…¥±…‰¥±¥Ñåññíôì(€™½È¡½¹ÍÐ™¥áÑÕÉ”½˜}ÍÁ½ÉÑÍY¥Í¥‰±•¥áÑÕÉ•Ì¥í½¹ÍÐ™½Õ¹õµ…ÁmÍÁ½ÉÑÍ¥áÑÕÉ•-•ä¡™¥áÑÕÉ”¥tí¥˜¡™½Õ¹¥=‰©•Ð¹…ÍÍ¥¸¡™¥áÑÕÉ”±™½Õ¹¤íô(€™½È¡½¹ÍÐ…É½˜‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œÑ•…µÍY¥•Ü€¹Ñ•…µ™¥áÑÕÉ•m‘…Ñ„µ•Ù•¹Ðµ­•åtœ¤¥ì(€€€½¹ÍÐÉ•ÍÕ±Ðõµ…Ám…É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ•Ù•¹Ðµ­•äœ¥ñðœtí¥˜ …É•ÍÕ±Ð¥½¹Ñ¥¹Õ”ì(€€€½¹ÍÐ¡…¹¹•±Ìõl¸¸¸¡É•ÍÕ±Ð¹µ…Ñ¡•Íññmt¤°¸¸¸¡É•ÍÕ±Ð¹ÁÁÙ}¡¥ÑÍññmt¥t±Ñ½Àõ…É¹ÅÕ•ÉåM•±•Ñ½È œ¹Ñ•…µ™¥áÑÕÉ•Ñ•…µÌœ¤±½±õÑ½À˜™Ñ½À¹ÅÕ•ÉåM•±•Ñ½È œ¹Ñ•…µ™¥áÑÕÉ•ÑØœ¤ì(€€€…É¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡…Í¡…¹¹•±Ìœ±¡…¹¹•±Ì¹±•¹Ñ øÀ¤ì(€€€¥˜¡¡…¹¹•±Ì¹±•¹Ñ ˜˜…½±¥Ñ½À¹¥¹Í•ÉÑ‘©…•¹Ñ!Q50 ‰•™½É••¹œ°œñÍÁ…¸±…ÍÌô‰ŒÑ•…µ™¥áÑÕÉ•ÑØˆùQXð½ÍÁ…¸øœ¤í•±Í”¥˜ …¡…¹¹•±Ì¹±•¹Ñ ˜™½±¥½±¹É•µ½Ù” ¤ì(€€€½¹ÍÐÁ…¹•°õ…É¹ÅÕ•ÉåM•±•Ñ½È œ¹™¥áÑÕÉ•¡…¹¹•±É•ÍÕ±ÑÌœ¤í¥˜¡Á…¹•°¥Á…¹•°¹¥¹¹•É!Q50õ™¥áÑÕÉ•MÑ½É•‘¡…¹¹•±Í!Ñµ°¡É•ÍÕ±Ð¤ì(€ô)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘MÁ½ÉÑÍÙ…¥±…‰¥±¥Ñä¡™½É”¥ì(€¥˜¡}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåQ¥µ•È¥í±•…ÉQ¥µ•½ÕÐ¡}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåQ¥µ•È¤í}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåQ¥µ•Èõ¹Õ±°íô(€½¹ÍÐÕ¹¥ÅÕ”õ¹•Ü5…À ¤í™½È¡½¹ÍÐ˜½˜}ÍÁ½ÉÑÍY¥Í¥‰±•¥áÑÕÉ•Ì¥í¥˜¡˜˜™˜¹¡½µ”˜™˜¹…Ý…ä¥Õ¹¥ÅÕ”¹Í•Ð¡ÍÁ½ÉÑÍ¥áÑÕÉ•-•ä¡˜¤±˜¤íô(€½¹ÍÐÉ•ÅÕ•ÍÐô¬­}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåI•ÅÕ•ÍÐì(€¥˜¡Õ¹¥ÅÕ”¹Í¥é”¥ì(€€€½¹ÍÐÁÉ¥½É¥Ñäõ˜ôùí½¹ÍÐµ¥¹Ìõ™¥áÑÕÉ•±…ÁÍ•‘5¥¹ÕÑ•Ì¡˜¤±Í•±•Ñ•ô¡˜¹™…Ù½É¥Ñ•}Ñ•…µÍññmt¤¹Í½µ”¡¹…µ”ôù}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡¹…µ”±}Í•±•Ñ•‘Q•…µ9…µ”¤¥ññ}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡˜¹¡½µ”±}Í•±•Ñ•‘Q•…µ9…µ”¥ññ}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡˜¹…Ý…ä±}Í•±•Ñ•‘Q•…µ9…µ”¤í¥˜¡™¥áÑÕÉ•%Í1¥Ù”¡˜¤¥É•ÑÕÉ¸€Àí¥˜¡Í•±•Ñ•˜˜¡µ¥¹Ìôôõ¹Õ±±ññµ¥¹ÌðÀ¤¥É•ÑÕÉ¸€Äí¥˜¡™¥áÑÕÉ•%ÍI••¹Ð¡˜¤¥É•ÑÕÉ¸€Èí¥˜¡µ¥¹Ì„ôõ¹Õ±°˜™µ¥¹ÌðÀ¥É•ÑÕÉ¸€ÌíÉ•ÑÕÉ¸€Ðíôì(€€€½¹ÍÐ™¥áÑÕÉ•ÌõÉÉ…ä¹™É½´¡Õ¹¥ÅÕ”¹Ù…±Õ•Ì ¤¤¹Í½ÉÐ ¡„±ˆ¤ôùÁÉ¥½É¥Ñä¡„¤µÁÉ¥½É¥Ñä¡ˆ¥ññ5…Ñ ¹…‰Ì¡™¥áÑÕÉ•±…ÁÍ•‘5¥¹ÕÑ•Ì¡„¥ñðÀ¤µ5…Ñ ¹…‰Ì¡™¥áÑÕÉ•±…ÁÍ•‘5¥¹ÕÑ•Ì¡ˆ¥ñðÀ¤¤ì(€€€½¹ÍÐ‰…Ñ¡•Ìõmtí¥˜¡™¥áÑÕÉ•Ì¹±•¹Ñ ¥‰…Ñ¡•Ì¹ÁÕÍ ¡™¥áÑÕÉ•Ì¹Í±¥” À°Ì¤¤í™½È¡±•Ð¤ôÌí¤ñ™¥áÑÕÉ•Ì¹±•¹Ñ í¤¬ôÄÈ¥‰…Ñ¡•Ì¹ÁÕÍ ¡™¥áÑÕÉ•Ì¹Í±¥”¡¤±¤¬ÄÈ¤¤ì(€€€™½È¡½¹ÍÐ‰…Ñ ½˜‰…Ñ¡•Ì¥ì(€€€€€¥˜¡É•ÅÕ•ÍÐ„ôõ}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåI•ÅÕ•ÍÐ¥‰É•…¬ì(€€€€€ÑÉåí½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½ÍÁ½ÉÑÍ}…Ù…¥±…‰¥±¥Ñäœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡í™¥áÑÕÉ•Ìé‰…Ñ ±™½É”è„…™½É•ô¥ô¤í¥˜¡É•ÅÕ•ÍÐ„ôõ}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåI•ÅÕ•ÍÐ¥‰É•…¬í¥˜ …È¹•ÉÉ½È¥í¥˜¡È¹±½•‘}¥¸ôôõ™…±Í”¥í½¹ÍÐÕ¹…Ù…¥±…‰±”õíôí™½È¡½¹ÍÐ˜½˜‰…Ñ ¥Õ¹…Ù…¥±…‰±•mÍÁ½ÉÑÍ¥áÑÕÉ•-•ä¡˜¥tõí±½•‘}¥¸é™…±Í”±µ…Ñ¡•Ìémt±ÁÁÙ}¡¥ÑÌémuôí…ÁÁ±åMÁ½ÉÑÍÙ…¥±…‰¥±¥Ñä¡Õ¹…Ù…¥±…‰±”¤í‰É•…¬íõ…ÁÁ±åMÁ½ÉÑÍÙ…¥±…‰¥±¥Ñä¡È¹…Ù…¥±…‰¥±¥Ñåññíô¤íõõ…Ñ ¡”¥íô(€€€€€…Ý…¥Ð¹•ÜAÉ½µ¥Í”¡É•Í½±Ù”ôùÍ•ÑQ¥µ•½ÕÐ¡É•Í½±Ù”°À¤¤ì(€€€ô(€ô(€}ÍÁ½ÉÑÍÙ…¥±…‰¥±¥ÑåQ¥µ•ÈõÍ•ÑQ¥µ•½ÕÐ  ¤ôùí¥˜ …‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µÍY¥•Üœ¤¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥±½…‘MÁ½ÉÑÍÙ…¥±…‰¥±¥Ñä¡ÑÉÕ”¤íô°ÄÔ¨ØÀ¨ÄÀÀÀ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸¡•­Q•…µ¥áÑÕÉ•Ì¡‰Ñ¸¥ì(€½¹ÍÐ½±õ‰Ñ¸¹¥¹¹•É!Q50ì(€‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐôI•™É•Í¡¥¹œ™¥áÑÕÉ•Ì¸¸¸œì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½¡•­}Ñ•…µ}™¥áÑÕÉ•Ìœ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡¨¹•ÉÉ½ÉñðI•™É•Í ™…¥±•œ¤ì(€€€…Ý…¥Ð±½…‘5åQ•…µÌ ¤ì(€€€Ñ½…ÍÐ MÕ•ÍÍ™Õ±±äÉ•™É•Í¡•™…Ù½É¥Ñ”µÑ•…´™¥áÑÕÉ•Ì¸œ°ÜÀÀÀ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ ½Õ±¹½ÐÉ•™É•Í Ñ•…´™¥áÑÕÉ•Ì¸œ°ÜÀÀÀ¤íô(€™¥¹…±±åí‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹¥¹¹•É!Q50õ½±í…ÁÁ±å1…¹œ ¤íô)ô)™Õ¹Ñ¥½¸Ñ½±•Q½Á¥áÑÕÉ•Ì¡‰Ñ¸¥ì(€½¹ÍÐ•áÑÉ…Ìõ‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œÑ•…µQ½Á1¥ÍÐ€¹Ñ½Á™¥áÑÕÉ••áÑÉ„œ¤ì(€¥˜ …•áÑÉ…Ì¹±•¹Ñ ¥É•ÑÕÉ¸ì(€½¹ÍÐ½Á•¹¥¹œõ•áÑÉ…ÍlÁt¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤ì(€•áÑÉ…Ì¹™½É… ¡•°ôù•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…½Á•¹¥¹œ¤¤ì(€‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ¡½Á•¹¥¹œüM¡½Ü™•Ý•Èµ…Ñ¡•ÌœèM¡½Üµ½É”µ…Ñ¡•Ìœ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Í•…É¡Q•…µÌ ¥ì(€½¹ÍÐÄô¡‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Äœ¤¹Ù…±Õ•ñðœœ¤¹ÑÉ¥´ ¤°•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µM•…É¡I•ÍÕ±ÑÌœ¤ì(€¥˜ …Ä¥í•°¹¥¹¹•É!Q50ôœœíÉ•ÑÕÉ¸íô(€•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆùM•…É¡¥¹œ½Ñ5½ˆ¸¸¸ð½ÍÁ…¸øœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½Ñ•…µ}Í•…É ýÄôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Ä¤¤ì(€¥˜¡È¹•ÉÉ½È¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡È¹•ÉÉ½È¤¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€¥˜ …È¹Ñ•…µÌ¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼Ñ•…´™½Õ¹¥¸ÕÉÉ•¹Ð½Ñ5½ˆ±¥ÍÑ¥¹Ì¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µ…Ñ¡É•ÍÕ±ÑÍ±…‰•°ˆøœ­•ÍŒ¡ÑÈ Q•…µÌœ¤¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰Ñ•…µÍ•…É¡¡¥ÁÌˆøœ­È¹Ñ•…µÌ¹µ…À¡Ñ•…´ôùí½¹ÍÐ¹…µ”õÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœýÑ•…´éÑ•…´¹¹…µ”±¥õÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœüœœè¡Ñ•…´¹Ñ•…µ}¥‘ñðœœ¤±±½¼õ¥üœ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡¥¤¤èœœíÉ•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰Ñ•…µÍ•…É¡¡¥Ðˆ‘…Ñ„µÑ•…´µÍ•±•Ðôˆœ­•ÍÑÑÈ¡¹…µ”¤¬œˆ‘…Ñ„µÑ•…´µ¥ôˆœ­•ÍÑÑÈ¡¥¤¬œˆøœ¬¡±½¼üœñ¥µœ±…ÍÌô‰Ñ•…µÍ•…É¡±½¼ˆÍÉŒôˆœ­•ÍÑÑÈ¡±½¼¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñÍÁ…¸øœ­•ÍŒ¡¹…µ”¤¬œð½ÍÁ…¸øñ‰ÕÑÑ½¸ÑåÁ”ô‰‰ÕÑÑ½¸ˆ±…ÍÌô‰¡½ÍÐÑ•…µ™¥¹‘™¥áÑÕÉ•Ìˆ‘…Ñ„µÑ•…´µ™¥áÑÕÉ•Ìôˆœ­•ÍÑÑÈ¡¹…µ”¤¬œˆ‘…Ñ„µÑ•…´µ¥ôˆœ­•ÍÑÑÈ¡¥¤¬œˆøœ­•ÍŒ¡ÑÈ ¥¹™¥áÑÕÉ•Ìœ¤¤¬œð½‰ÕÑÑ½¸øñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…ÈÑ•…µÍÑ…Èœ¬¡}™…ÙQ•…µM•Ð¹¡…Ì¡¹…µ”¹Ñ½1½Ý•É…Í” ¤¤üœ½¸œèœœ¤¬œˆ‘…Ñ„µÑ•…´µ¹…µ”ôˆœ­•ÍÑÑÈ¡¹…µ”¤¬œˆ‘…Ñ„µÑ•…´µ¥ôˆœ­•ÍÑÑÈ¡¥¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øð½‘¥Øøœíô¤¹©½¥¸ œœ¤¬œð½‘¥Øøœì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Í•…É¡Q•…µ!Õˆ¡ÅÕ•Éä¥ì(€½¹ÍÐ¥¹ÁÕÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Äœ¤í¥˜¡ÅÕ•Éä„ôõÕ¹‘•™¥¹•˜™¥¹ÁÕÐ¥¥¹ÁÕÐ¹Ù…±Õ”õMÑÉ¥¹œ¡ÅÕ•Éåñðœœ¤ì(€¥˜ …¥¹ÁÕÑñð…¥¹ÁÕÐ¹Ù…±Õ”¹ÑÉ¥´ ¤¥í±•…ÉMÁ½ÉÑÍM•…É  ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ‰…¬õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÁ½ÉÑÍM•…É¡	…¬œ¤í¥˜¡‰…¬¥‰…¬¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€}Ñ•…µ••Á1¥¹¬õ¹Õ±°ì(€}™¥áÑÕÉ•M•…É¡Q•…µ%ôœœì(€½¹ÍÐÉ•ÍÕ±ÑÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É•ÍÕ±ÑÌœ¤í¥˜¡É•ÍÕ±ÑÌ¥É•ÍÕ±ÑÌ¹¥¹¹•É!Q50ôœœì(€…Ý…¥ÐÍ•…É¡Q•…µÌ ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸™¥¹‘MÁ½ÉÑÍ¥áÑÕÉ•Ì¡¹…µ”±Ñ•…µ%¥ì(€½¹ÍÐ¥¹ÁÕÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Äœ¤í¥˜¡¥¹ÁÕÐ¥¥¹ÁÕÐ¹Ù…±Õ”õMÑÉ¥¹œ¡¹…µ•ñðœœ¤ì(€¥˜ …MÑÉ¥¹œ¡¹…µ•ñðœœ¤¹ÑÉ¥´ ¤¥É•ÑÕÉ¸ì(€½¹ÍÐ‰…¬õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÁ½ÉÑÍM•…É¡	…¬œ¤í¥˜¡‰…¬¥‰…¬¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€}Ñ•…µ••Á1¥¹¬õ¹Õ±°í}™¥áÑÕÉ•M•…É¡Q•…µ%õMÑÉ¥¹œ¡Ñ•…µ%‘ñðœœ¤íÍ•±•Ñ5åQ•…´¡¹…µ”±Ñ•…µ%‘ñðœœ°œœ¤ì(€…Ý…¥Ð‘½M•…É  ¤ì)ô)™Õ¹Ñ¥½¸±•…ÉMÁ½ÉÑÍM•…É  ¥ì(€½¹ÍÐ¥¹ÁÕÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Äœ¤±Ñ•…µÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µM•…É¡I•ÍÕ±ÑÌœ¤±É•ÍÕ±ÑÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É•ÍÕ±ÑÌœ¤±‰…¬õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÁ½ÉÑÍM•…É¡	…¬œ¤ì(€¥˜¡¥¹ÁÕÐ¥¥¹ÁÕÐ¹Ù…±Õ”ôœœí¥˜¡Ñ•…µÌ¥Ñ•…µÌ¹¥¹¹•É!Q50ôœœí¥˜¡É•ÍÕ±ÑÌ¥É•ÍÕ±ÑÌ¹¥¹¹•É!Q50ôœœí¥˜¡‰…¬¥‰…¬¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€}Í•…É¡…Ñ„õ¹Õ±°í}Ñ•…µÉ½ÕÁÌõmtí}…Ñ¥Ù•Q•…´ôÀí}Ñ•…µ••Á1¥¹¬õ¹Õ±°í}™¥áÑÕÉ•M•…É¡Q•…µ%ôœœì)ô)™Õ¹Ñ¥½¸½Á•¹5åQ•…µÍ¥áÑÕÉ”¡Ñ…É•Ð¥ì(€¥˜ …Ñ…É•Ð¥É•ÑÕÉ¸ì(€½¹ÍÐÉ•…ô¡¹…µ”¤ôùÑ…É•Ð¥¹ÍÑ…¹•½˜±•µ•¹ÐýÑ…É•Ð¹•ÑÑÑÉ¥‰ÕÑ”¡¹…µ”¤éÑ…É•Ñm¹…µ”¹É•Á±…” ½y‘…Ñ„´¼°œœ¤¹É•Á±…” ¼´¡m„µét¤½œ°¡|±Œ¤ôùŒ¹Ñ½UÁÁ•É…Í” ¤¥tì(€}Ñ•…µ••Á1¥¹¬õí¡½µ”éMÑÉ¥¹œ¡É•… ‘…Ñ„µ¡½µ”œ¥ññÑ…É•Ð¹¡½µ•ñðœœ¤±…Ý…äéMÑÉ¥¹œ¡É•… ‘…Ñ„µ…Ý…äœ¥ññÑ…É•Ð¹…Ý…åñðœœ¤±ÍÑ…ÉÐéMÑÉ¥¹œ¡É•… ‘…Ñ„µÍÑ…ÉÐœ¥ññÑ…É•Ð¹ÍÑ…ÉÑñðœœ¥ôì(€½¹ÍÐÅÕ•ÉäõMÑÉ¥¹œ¡É•… ‘…Ñ„µÍ•…É œ¥ññÑ…É•Ð¹Í•…É¡ññÑ…É•Ð¹½Ý¹•Éññ}Ñ•…µ••Á1¥¹¬¹¡½µ•ññ}Ñ•…µ••Á1¥¹¬¹…Ý…åñðœœ¤ì(€}Í•±•Ñ•‘Q•…µ9…µ”õÅÕ•Éäí½¹ÍÐ™…Ù½É¥Ñ”õ}™…ÙQ•…µI½ÝÌ¹™¥¹¡Ðôù}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡Ð¹¹…µ”±ÅÕ•Éä¤¤í¥˜¡™…Ù½É¥Ñ”¥í}Í•±•Ñ•‘Q•…µI½Üõ™…Ù½É¥Ñ”íÉ•¹‘•ÉQ•…µ…Ù½É¥Ñ•I…¥° ¤í±½…‘M•±•Ñ•‘Q•…µAÉ½™¥±”¡™…Ù½É¥Ñ”¤íô(€}™¥áÑÕÉ•M•…É¡Q•…µ%õMÑÉ¥¹œ ¡™…Ù½É¥Ñ”˜™™…Ù½É¥Ñ”¹Ñ•…µ}¥¥ñðœœ¤ì(€½¹ÍÐ¥¹ÁÕÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Äœ¤±É•ÍÕ±ÑÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É•ÍÕ±ÑÌœ¤±Ñ•…µÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µM•…É¡I•ÍÕ±ÑÌœ¤±‰…¬õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÁ½ÉÑÍM•…É¡	…¬œ¤ì(€¥˜¡¥¹ÁÕÐ¥¥¹ÁÕÐ¹Ù…±Õ”ôœœí¥˜¡É•ÍÕ±ÑÌ¥É•ÍÕ±ÑÌ¹¥¹¹•É!Q50ôœœí¥˜¡Ñ•…µÌ¥Ñ•…µÌ¹¥¹¹•É!Q50ôœœí¥˜¡‰…¬¥‰…¬¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€½¹ÍÐ…É‘ÌõÉÉ…ä¹™É½´¡‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œÑ•…µÍY¥•Ü€¹Ñ•…µ™¥áÑÕÉ•m‘…Ñ„µ™¥áÑÕÉ”µ…ÉôˆÄ‰tœ¤¤ì(€½¹ÍÐÍ•±•Ñ•õ…É‘Ì¹™¥¹¡…Éôù}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡…É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¡½µ”œ¤±}Ñ•…µ••Á1¥¹¬¹¡½µ”¤˜™}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡…É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ý…äœ¤±}Ñ•…µ••Á1¥¹¬¹…Ý…ä¤˜˜ …}Ñ•…µ••Á1¥¹¬¹ÍÑ…ÉÑñð……É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍÑ…ÉÐœ¥ññ5…Ñ ¹…‰Ì¡¹•Ü…Ñ”¡…É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍÑ…ÉÐœ¤¤µ¹•Ü…Ñ”¡}Ñ•…µ••Á1¥¹¬¹ÍÑ…ÉÐ¤¤ðØ¨ÌØÀÀÀÀÀ¤¤ì(€…É‘Ì¹™½É… ¡…Éôù…É¹±…ÍÍ1¥ÍÐ¹Ñ½±” Í•±•Ñ•‘™¥áÑÕÉ”œ±…ÉôôõÍ•±•Ñ•¤¤ì(€¥˜¡Í•±•Ñ•¥í½¹ÍÐ‘•Ñ…¥±ÌõÍ•±•Ñ•¹ÅÕ•ÉåM•±•Ñ½È œ¹Ñ•…µ™¥áÑÕÉ•‰É½…‘…ÍÑÌœ¤í¥˜¡‘•Ñ…¥±Ì¥‘•Ñ…¥±Ì¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤íÍ•±•Ñ•¹ÍÉ½±±%¹Ñ½Y¥•Ü¡í‰•¡…Ù¥½ÈèÍµ½½Ñ œ±‰±½¬è•¹Ñ•Èô¤íô)ô)™Õ¹Ñ¥½¸™¥áÑÕÉ•MÑ½É•‘¡…¹¹•±Í!Ñµ°¡˜¥ì(€¥˜ …˜¹±½•‘}¥¸¥É•ÑÕÉ¸€œñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 1½œ¥¸Ù¥„M•ÑÑ¥¹Ì™¥ÉÍÐ¸œ¤¤¬œð½ÍÁ…¸øœì(€½¹ÍÐ…±°õl¸¸¸¡˜¹µ…Ñ¡•Íññmt¤°¸¸¸¡˜¹ÁÁÙ}¡¥ÑÍññmt¥t±Í••¸õ¹•ÜM•Ð ¤±‘•™¥¹¥Ñ”õmt±½Ñ¡•Èõmtì(€™½È¡½¹ÍÐ ½˜…±°¥í½¹ÍÐ¥õMÑÉ¥¹œ¡ ¹ÍÑÉ•…µ}¥‘ñðœœ¤í¥˜ …¥‘ññÍ••¸¹¡…Ì¡¥¤¥½¹Ñ¥¹Õ”íÍ••¸¹…‘¡¥¤í¥˜¡™¥áÑÕÉ•¡…¹¹•±I…¹¬¡ ±˜¤ôôôÍññ ¹ÁÉ½Ù¥‘•É}•á…ÐôôõÑÉÕ•ññ ¹½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ôôõÑÉÕ•ññÕÍÑ½µAÉ•µ¥•É1•…Õ•M•ÕÉ”¡ ±˜¤¥‘•™¥¹¥Ñ”¹ÁÕÍ ¡ ¤í•±Í”½Ñ¡•È¹ÁÕÍ ¡ ¤íô(€‘•™¥¹¥Ñ”¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤í½Ñ¡•È¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤ì(€½¹ÍÐ±¥¹”õ ôøœñ‘¥Ø±…ÍÌô‰É…¥¹•Ù•¹Ñ¡…¹¹•°ˆøœ­¡…¹¹•±1½¼¡ °µ¥¹¤œ¤¬œñÍÁ…¸±…ÍÌô‰¡¸™¥áÑÕÉ•¡…¹¹•±Ñ¥Ñ±”ˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡ ¹ÍÑÉ•…µ}¥‘ñðœœ¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡ ¹áÑÉ•…µ}¹…µ•ñð¡…¹¹•°œ¤¬œˆøœ­•ÍŒ¡ ¹áÑÉ•…µ}¹…µ•ñð¡…¹¹•°œ¤¬¡ ¹ÅÕ…±¥ÑäüœñÍÁ…¸±…ÍÌô‰Ñ…œˆøœ­•ÍŒ¡ ¹ÅÕ…±¥Ñä¤¬œð½ÍÁ…¸øœèœœ¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰¡‰Ñ¹Ìˆøœ­Á±…å‰Ñ¹Ì¡ ¹ÍÑÉ•…µ}¥± ¹áÑÉ•…µ}¹…µ”± ¹ÕÉ°¤¬œð½ÍÁ…¸øð½‘¥Øøœì(€±•Ð ôœœí¥˜¡‘•™¥¹¥Ñ”¹±•¹Ñ ¥ ¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ •™¥¹¥Ñ”¡…¹¹•°µ…Ñ¡•Ìœ¤¤¬œð½‘¥Øøœ­Í•ÕÉ•5…Ñ¡É½ÕÁÍ!Ñµ°¡‘•™¥¹¥Ñ”±±¥¹”°ÍÑ½É•œ­5…Ñ ¹É…¹‘½´ ¤¹Ñ½MÑÉ¥¹œ ÌØ¤¹Í±¥” È¤¤ì(€¥˜¡½Ñ¡•È¹±•¹Ñ ¥í ¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèáÁàˆøœ­•ÍŒ¡ÑÈ A½ÍÍ¥‰±”¡…¹¹•±Ì‰ä…Ñ•½Éäœ¤¤¬œð½‘¥Øøœí™½È¡½¹ÍÐm¹…µ”±¥Ñ•µÍt½˜É½ÕÁ•‘A½ÍÍ¥‰±•¡…¹¹•±Ì¡½Ñ¡•È¤¥ ¬ôœñ‘¥Ø±…ÍÌô‰‰É½Üˆøñ‘¥Ø±…ÍÌô‰‰¡•…ˆøñÍÁ…¸±…ÍÌô‰‰¹…µ”ˆøœ­•ÍŒ¡¹…µ”¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­¥Ñ•µÌ¹±•¹Ñ ¬œ€œ­•ÍŒ¡ÑÈ¡¥Ñ•µÌ¹±•¹Ñ ôôôÄü¡…¹¹•°œè¡…¹¹•±Ìœ¤¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰‰¡•ÙÉ½¸ˆø˜ŒäØØÈìð½ÍÁ…¸øð½‘¥Øøñ‘¥Ø±…ÍÌô‰‰¡…¹Ì¡¥‘”ˆøœ­¥Ñ•µÌ¹µ…À¡±¥¹”¤¹©½¥¸ œœ¤¬œð½‘¥Øøð½‘¥Øøœíô(€É•ÑÕÉ¸¡ñðœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 9¼µ…Ñ¡¥¹œ¡…¹¹•±Ìœ¤¤¬œð½ÍÁ…¸øœì)ô()™Õ¹Ñ¥½¸Í•ÕÉ•¡…¹¹•±…µ¥±ä¡ ¥íÉ•ÑÕÉ¸MÑÉ¥¹œ¡ ˜™ ¹áÑÉ•…µ}¹…µ•ñð¡…¹¹•°œ¤¹Ñ½1½Ý•É…Í” ¤¹É•Á±…” ½yqqÌ©m„µèÀ´åuìÄ°áõqqÌ©léñqpµuqqÌ¨¼°œœ¤¹É•Á±…” ½yqqÌ¨¡¹½ñ¹½Éñ¹½ÉÝ…åñ¹½É•ñ¹½ÉÝ•¥…¸¥qqÌ¬¼°œœ¤¹É•Á±…” ½qqˆ Ñ­ñÕ¡‘ñ™¡‘ñ™Õ±±qqÌ©¡‘ñ¡‘ñÍ‘ñÉ…Ýñ¡•Ùñ¡qp¸üÈÙlÐÕuñ…ÙðÔÁqqÌ©™ÁÍðØÁqqÌ©™ÁÍñÙ¥Áñ½±‘ñ‘½±‰åñ…Õ‘¥½ñ¹½ñ¹½Éñ¹½ÉÝ…åñ¹½É•ñ¹½ÉÝ•¥…¸¥qqˆ½œ°œ€œ¤¹É•Á±…” ½my„µèÀ´åt¬½œ°œ€œ¤¹É•Á±…” ½qqÌ¬½œ°œ€œ¤¹ÑÉ¥´ ¥ññMÑÉ¥¹œ¡ ˜™ ¹áÑÉ•…µ}¹…µ•ñð¡…¹¹•°œ¤¹Ñ½1½Ý•É…Í” ¤íô)™Õ¹Ñ¥½¸Í•ÕÉ•EÕ…±¥ÑåAÉ¥½É¥Ñä¡ ¥í½¹ÍÐ¸õMÑÉ¥¹œ¡ ˜™ ¹áÑÉ•…µ}¹…µ•ñðœœ¤¹Ñ½1½Ý•É…Í” ¤í±•ÐÍ½É”ôÀí¥˜ ½qqˆ Ñ­ñÕ¡¥qqˆ¼¹Ñ•ÍÐ¡¸¤¥Í½É”ôÜÀÀí•±Í”¥˜ ½qqˆ¡™¡‘ñ™Õ±±qqÌ©¡¥qqˆ¼¹Ñ•ÍÐ¡¸¤¥Í½É”ôØÀÀí•±Í”¥˜ ½qqˆ¡¡•Ùñ¡qp¸üÈØÔ¥qqˆ¼¹Ñ•ÍÐ¡¸¤¥Í½É”ôÔÔÀí•±Í”¥˜ ½qq‰¡‘qqˆ¼¹Ñ•ÍÐ¡¸¤¥Í½É”ôÔÀÀí•±Í”¥˜ ½qq‰É…Ýqqˆ¼¹Ñ•ÍÐ¡¸¤¥Í½É”ôÐÀÀí•±Í”¥˜ ½qq‰Í‘qqˆ¼¹Ñ•ÍÐ¡¸¤¥Í½É”ôÄÀÀí¥˜ ½qqˆ ÔÁðØÀ¥qqÌ©™ÁÍqqˆ¼¹Ñ•ÍÐ¡¸¤¥Í½É”¬ôÄÀíÉ•ÑÕÉ¸Í½É”íô)™Õ¹Ñ¥½¸Í•ÕÉ•5…Ñ¡É½ÕÁÍ!Ñµ°¡É½ÝÌ±±¥¹”±ÁÉ•™¥à¥ì(€½¹ÍÐÉ½ÕÁÌõ¹•Ü5…À ¤í™½È¡½¹ÍÐ ½˜É½ÝÌ¹Í±¥” ¤¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤¥í½¹ÍÐ™…µ¥±äõÍ•ÕÉ•¡…¹¹•±…µ¥±ä¡ ¤í¥˜ …É½ÕÁÌ¹¡…Ì¡™…µ¥±ä¤¥É½ÕÁÌ¹Í•Ð¡™…µ¥±ä±mt¤íÉ½ÕÁÌ¹•Ð¡™…µ¥±ä¤¹ÁÕÍ ¡ ¤íô(€±•Ð¡Ñµ°ôœœ±É½ÕÁ%¹‘•àôÀí™½È¡½¹ÍÐ¥Ñ•µÌ½˜É½ÕÁÌ¹Ù…±Õ•Ì ¤¥í¥Ñ•µÌ¹Í½ÉÐ ¡„±ˆ¤ôù¡…¹¹•±1½…±•AÉ¥½É¥Ñä¡ˆ¤µ¡…¹¹•±1½…±•AÉ¥½É¥Ñä¡„¥ññÍ•ÕÉ•EÕ…±¥ÑåAÉ¥½É¥Ñä¡ˆ¤µÍ•ÕÉ•EÕ…±¥ÑåAÉ¥½É¥Ñä¡„¥ññÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¡„±ˆ¤¤í½¹ÍÐ¥õÁÉ•™¥à¬œœ­É½ÕÁ%¹‘•à¬¬í¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰Í•ÕÉ•µ…Ñ¡É½ÕÀˆøœí™½È¡½¹ÍÐm¤±¡t½˜¥Ñ•µÌ¹•¹ÑÉ¥•Ì ¤¥¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰Í•ÕÉ•µ…Ñ¡Ù…É¥…¹Ðœ¬¡¤øôÔüœÍ•ÕÉ•µ…Ñ¡•áÑÉ„¡¥‘”œèœœ¤¬œˆ‘…Ñ„µÍ•ÕÉ”µÉ½ÕÀôˆœ­¥¬œˆøœ­±¥¹”¡ ¤¬œð½‘¥Øøœí¥˜¡¥Ñ•µÌ¹±•¹Ñ øÔ¥¡Ñµ°¬ôœñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐÍ•ÕÉ•µ…Ñ¡•áÁ…¹ˆ‘…Ñ„µÍ•ÕÉ”µÑ…É•Ðôˆœ­¥¬œˆ‘…Ñ„µµ½É”ôˆœ¬¡¥Ñ•µÌ¹±•¹Ñ ´Ô¤¬œˆøœ­•ÍŒ¡ÑÈ M¡½Üµ½É”¡…¹¹•±Ìœ¤¤¬œ€ œ¬¡¥Ñ•µÌ¹±•¹Ñ ´Ô¤¬œ¤ð½‰ÕÑÑ½¸øœí¡Ñµ°¬ôœð½‘¥Øøœíô(€É•ÑÕÉ¸¡Ñµ°ì)ô)™Õ¹Ñ¥½¸Ñ½±•M•ÕÉ•5…Ñ¡•Ì¡‰Ñ¸¥í½¹ÍÐ¥õ‰Ñ¸¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•ÕÉ”µÑ…É•Ðœ¤±•áÑÉ…Ìõ‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹Í•ÕÉ•µ…Ñ¡•áÑÉ…m‘…Ñ„µÍ•ÕÉ”µÉ½ÕÀôˆœ­¥¬œ‰tœ¤í¥˜ …•áÑÉ…Ì¹±•¹Ñ ¥É•ÑÕÉ¸í½¹ÍÐ½Á•¹¥¹œõ•áÑÉ…ÍlÁt¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤í•áÑÉ…Ì¹™½É… ¡•°ôù•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…½Á•¹¥¹œ¤¤í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½Á•¹¥¹œýÑÈ M¡½Ü™•Ý•Èµ…Ñ¡•Ìœ¤è¡ÑÈ M¡½Üµ½É”¡…¹¹•±Ìœ¤¬œ€ œ­‰Ñ¸¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µµ½É”œ¤¬œ¤œ¤íô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘MÑ½É•‘¥áÑÕÉ•¡…¹¹•±Ì¡…É¥ì(€½¹ÍÐÁ…¹•°õ…É˜™…É¹ÅÕ•ÉåM•±•Ñ½È œ¹™¥áÑÕÉ•¡…¹¹•±É•ÍÕ±ÑÌœ¤í¥˜ ……É‘ñð…Á…¹•°¥É•ÑÕÉ¸ì(€½¹ÍÐÝ…¥Ñ¥¹œõÁ…¹•°¹Ñ•áÑ½¹Ñ•¹Ð¹¥¹±Õ‘•Ì¡ÑÈ ¡•­¥¹œå½ÕÈ¡…¹¹•±Ì¸¸¸œ¤¤í¥˜¡Á…¹•°¹¥¹¹•É!Q50¹ÑÉ¥´ ¤˜˜…Ý…¥Ñ¥¹œ¥É•ÑÕÉ¸ì(€½¹ÍÐ•Ù•¹Ñ-•äõ…É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ•Ù•¹Ðµ­•äœ¥ñðœœ±Í½ÕÉ”õ}ÍÁ½ÉÑÍY¥Í¥‰±•¥áÑÕÉ•Ì¹™¥¹¡˜ôùÍÁ½ÉÑÍ¥áÑÕÉ•-•ä¡˜¤ôôõ•Ù•¹Ñ-•ä¥ññíôì(€½¹ÍÐ™¥áÑÕÉ”õ=‰©•Ð¹…ÍÍ¥¸¡íô±Í½ÕÉ”±í¡½µ”é…É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¡½µ”œ¥ññÍ½ÕÉ”¹¡½µ•ñðœœ±…Ý…äé…É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ý…äœ¥ññÍ½ÕÉ”¹…Ý…åñðœœ±ÍÑ…ÉÐé…É¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍÑ…ÉÐœ¥ññÍ½ÕÉ”¹ÍÑ…ÉÑñðœœ±‰å}½Õ¹ÑÉäéÍ½ÕÉ”¹‰å}½Õ¹ÑÉåññíõô¤ì(€ÑÉåí½¹ÍÐÉ•ÍÕ±Ðõ…Ý…¥Ð…Á¤ œ½…Á¤½ÍÁ½ÉÑÍ}•Ù•¹Ñ}¡…¹¹•±Ìœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡í™¥áÑÕÉ”é™¥áÑÕÉ•ô¥ô¤í¥˜ …É•ÍÕ±Ð¹•ÉÉ½È¥í=‰©•Ð¹…ÍÍ¥¸¡™¥áÑÕÉ”±É•ÍÕ±Ð¤í…ÁÁ±åMÁ½ÉÑÍÙ…¥±…‰¥±¥Ñä¡ím•Ù•¹Ñ-•åtéÉ•ÍÕ±Ñô¤íÁ…¹•°¹¥¹¹•É!Q50õ™¥áÑÕÉ•MÑ½É•‘¡…¹¹•±Í!Ñµ°¡™¥áÑÕÉ”¤íõõ…Ñ ¡”¥íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸Ñ½±•Q•…µ…Ù½É¥Ñ”¡¹…µ”±ÍÑ…È±Ñ•…µ%¥ì(€½¹ÍÐÈõ…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÑ½±•}Ñ•…´œ±Ñ•…´éí¹…µ”é¹…µ”±Ñ•…µ}¥éÑ•…µ%‘ñðœõô¤ì(€}™…ÙQ•…µM•Ðõ¹•ÜM•Ð ¡È¹Ñ•…µ}¹…µ•Íññmt¤¹µ…À¡¹…µ”ôùMÑÉ¥¹œ¡¹…µ”¤¹Ñ½1½Ý•É…Í” ¤¤¤ì(€¥˜¡ÍÑ…È¥ÍÑ…È¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½¸œ±}™…ÙQ•…µM•Ð¹¡…Ì¡MÑÉ¥¹œ¡¹…µ”¤¹Ñ½1½Ý•É…Í” ¤¤¤ì(€…Ý…¥Ð±½…‘5åQ•…µÌ ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•µ½Ù•Q•…µ…Ù½É¥Ñ”¡¹…µ”¥ì(€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÉ•µ½Ù•}Ñ•…´œ±¹…µ”é¹…µ•ô¤ì(€}™…ÙQ•…µM•Ð¹‘•±•Ñ”¡MÑÉ¥¹œ¡¹…µ”¤¹Ñ½1½Ý•É…Í” ¤¤ì(€…Ý…¥Ð±½…‘5åQ•…µÌ ¤ì)ô()±•Ð}…ÑÍ1½…‘•õ™…±Í”ì)±•Ð}…±±…ÑÌõmtì(¼¼	Õ¥±¥½¹Ì™É½´U¹¥½‘”½‘”Á½¥¹ÑÌ¥¹ÍÑ•…½˜•µ‰•‘‘¥¹œ•µ½©¤‰åÑ•Ì¥¸Ñ¡”(¼¼Í½ÕÉ”¸Q¡¥Ì­••ÁÌ™±…Ì¥¹Ñ…Ð¥˜„]¥¹‘½ÝÌ•‘¥Ñ½ÈÉ•Á…­ÌÑ¡”ÍÉ¥ÁÐ¸)½¹ÍÐ}=U9QIe}=Lõì(€¹¼è9<œ±Í”èMœ±‘¬è,œ±™¤è$œ±Õ¬èœ±ˆèœ±ÕÌèULœ±„èœ±‘”èœ±™ÈèHœ°(€¥Ðè%Pœ±•ÌèLœ±ÁÐèAPœ±¹°è90œ±‰”è	œ± è œ±…ÐèPœ±¥”è%œ±Á°èA0œ±ÈèHœ°(€ÑÈèQHœ±ÉÔèITœ±Õ„èUœ±É¼èI<œ±‰œè	œ±¡Èè!Hœ±Í¤èM$œ±ÉÌèILœ±èèhœ±Í¬èM,œ°(€¡Ôè!Tœ±…°è0œ±‰„è	œ±µ¬è5,œ±¥¸è%8œ±Á¬èA,œ±¥Èè%Hœ±Í„èMœ±•œèœ±¥°è%0œ°(€‰Èè	Hœ±µàè5`œ±…ÔèTœ±…œèœ)ôì)½¹ÍÐ}%=9Lõí…ÈèÁàÅ˜ÌÄÀ±…™ÈèÁàÅ˜ÌÁ±…Í¥„èÁàÅ˜ÌÁ˜±•àèÁàÅ˜ÌÄÀ°•àµåÔœèÁàÅ˜ÌÄÀ°(€…´èÁàÅ˜ÌÄÀ±µ•¹„èÁàÅ˜ÌÄÀ°œÑ¬œèÁàÅ˜Ñ™„±Õ¡èÁàÅ˜Ñ™„±ÁÁØèÁàÅ˜Í…ˆ°(€Ù¥ÀèÁàÉˆÔÀ±ÍÁ½ÉÐèÁàÈÙ‰±ÍÁ½ÉÑÌèÁàÈÙ‰‘ôì)™Õ¹Ñ¥½¸}½Õ¹ÑÉå±…œ¡½‘”¥ì(€É•ÑÕÉ¸MÑÉ¥¹œ¹™É½µ½‘•A½¥¹Ð ¸¸¹½‘”¹ÍÁ±¥Ð œœ¤¹µ…À¡ŒôøÁàÅ˜Å”Ø­Œ¹¡…É½‘•Ð À¤´ØÔ¤¤ì)ô)™Õ¹Ñ¥½¸}™±…½È¡¹…µ”¥ì(€½¹ÍÐ´ô¡¹…µ•ñðœœ¤¹µ…Ñ  ½yqqÌ¨¡m„µèÀ´äµuìÄ°Õô¥qqÌ©qqð½¤¤ì(€¥˜ …´¥É•ÑÕÉ¸MÑÉ¥¹œ¹™É½µ½‘•A½¥¹Ð ÁàÅ˜ÌÄÀ¤ì(€½¹ÍÐ­•äõµlÅt¹Ñ½1½Ý•É…Í” ¤ì(€¥˜¡}=U9QIe}=Mm­•åt¥É•ÑÕÉ¸}½Õ¹ÑÉå±…œ¡}=U9QIe}=Mm­•åt¤ì(€É•ÑÕÉ¸MÑÉ¥¹œ¹™É½µ½‘•A½¥¹Ð¡}%=9Mm­•åuñðÁàÅ˜ÌÄÀ¤ì)ô)±•Ð}Í•±…ÑÌõ¹•ÜM•Ð ¤ì)±•Ð}…Ñ¥Ù•…Ðõ¹Õ±°ì)±•Ð}¡…¹¹•±Ìõmtì€€€€€€¼¼¡…¹¹•±ÌÕÉÉ•¹Ñ±äÍ¡½Ý¸¥¸…Ñ•½Éä¡…¹¹•±Ì)±•Ð}Á±…å±¥ÍÐõ¹•Ü5…À ¤ì€¼¼ÍÑÉ•…µ}¥€´øí¹…µ”±ÕÉ°±…Ñ•½Éåô()…Íå¹Œ™Õ¹Ñ¥½¸±½…‘…Ñ•½É¥•Ì¡™½É”¥ì(€¥˜¡}…ÑÍ1½…‘•˜˜…™½É”¥É•ÑÕÉ¸ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½…Ñ•½É¥•Ìœ¤ì(€¥˜ …È¹±½•‘}¥¸¥í‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …Ñ±¥ÍÐœ¤¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù1½œ¥¸Ù¥„M•ÑÑ¥¹Ì™¥ÉÍÐ¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€¥˜¡È¹•ÉÉ½È¥í‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …Ñ±¥ÍÐœ¤¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡È¹•ÉÉ½È¤¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€}…±±…ÑÌõÈ¹…Ñ•½É¥•Íññmtì(€}…ÑÍ1½…‘•õÑÉÕ”ì(€É•¹‘•É…Ñ1¥ÍÐ ¤íÉ•¹‘•ÉM•±•Ñ• ¤íÉ•¹‘•ÉA±…å±¥ÍÐ ¤ì)ô)™Õ¹Ñ¥½¸É•¹‘•É…Ñ1¥ÍÐ ¥ì(€½¹ÍÐ™°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …Ñ™¥±Ñ•Èœ¤ì(€½¹ÍÐ˜ô¡™°ý™°¹Ù…±Õ”èœœ¤¹Ñ½1½Ý•É…Í” ¤ì(€½¹ÍÐÍ¡½Ý¸õ}…±±…ÑÌ¹™¥±Ñ•È¡™Õ¹Ñ¥½¸¡Œ¥íÉ•ÑÕÉ¸€…™ññŒ¹¹…µ”¹Ñ½1½Ý•É…Í” ¤¹¥¹‘•á=˜¡˜¤øôÀíô¤ì(€±•Ð¡Ñµ°ôœœì(€™½È¡½¹ÍÐŒ½˜Í¡½Ý¸¥ì(€€€½¹ÍÐ½¸õ}Í•±…ÑÌ¹¡…Ì¡Œ¹¹…µ”¤üœ½¸œèœœì(€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰…Ñ¥Ñ•´œ­½¸¬œˆ½¹±¥¬ô‰Ñ½±•…Ð¡Ñ¡¥Ì¹•ÑÑÑÉ¥‰ÕÑ”¡qp‘…Ñ„µqpœ¤¤ˆ‘…Ñ„µŒôˆœ­•ÍÑÑÈ¡Œ¹¹…µ”¤¬œˆøœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰Ñ¥¬ˆùqÔÈÜÄÌð½ÍÁ…¸øœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰™±…œˆøœ­}™±…½È¡Œ¹¹…µ”¤¬œð½ÍÁ…¸øœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰¸ˆøœ­•ÍŒ¡Œ¹¹…µ”¤¬œ€ñÍÁ…¸±…ÍÌô‰ÁŒˆøœ­Œ¹½Õ¹Ð¬œð½ÍÁ…¸øð½ÍÁ…¸øð½‘¥Øøœì(€ô(€½¹ÍÐ‰½àõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …Ñ±¥ÍÐœ¤ì(€½¹ÍÐÉ½ÝÌõ5…Ñ ¹•¥°¡Í¡½Ý¸¹±•¹Ñ ¼Ð¥ñðÄì(€‰½à¹ÍÑå±”¹Í•ÑAÉ½Á•ÉÑä œ´µ…ÑÉ½ÝÌœ±É½ÝÌ¤ì(€‰½à¹¥¹¹•É!Q50õ¡Ñµ±ñðœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼…Ñ•½É¥•Ìµ…Ñ ¸ð½ÍÁ…¸øœì)ô)™Õ¹Ñ¥½¸Ñ½±•…Ð¡¹…µ”¥ì(€¥˜¡}Í•±…ÑÌ¹¡…Ì¡¹…µ”¤¥}Í•±…ÑÌ¹‘•±•Ñ”¡¹…µ”¤ì•±Í”}Í•±…ÑÌ¹…‘¡¹…µ”¤ì(€¥˜ …}Í•±…ÑÌ¹¡…Ì¡}…Ñ¥Ù•…Ð¤¥}…Ñ¥Ù•…Ðõ¹Õ±°ì(€É•¹‘•É…Ñ1¥ÍÐ ¤íÉ•¹‘•ÉM•±•Ñ• ¤ì)ô)™Õ¹Ñ¥½¸É•¹‘•ÉM•±•Ñ• ¥ì(€½¹ÍÐÍ•°õÉÉ…ä¹™É½´¡}Í•±…ÑÌ¤ì(€½¹ÍÐ‰½àõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í•±…ÑÌœ¤ì(€¥˜ …Í•°¹±•¹Ñ ¥í‰½à¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆùQ¥¬…Ñ•½É¥•Ì½¸Ñ¡”±•™Ð¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€½¹ÍÐ‰å9…µ”õíôí™½È¡½¹ÍÐŒ½˜}…±±…ÑÌ¥‰å9…µ•mŒ¹¹…µ•tõŒ¹½Õ¹Ðì(€±•Ð¡Ñµ°ôœœì(€™½È¡½¹ÍÐÌ½˜Í•°¥ì(€€€½¹ÍÐ…Ñ¥Ù”ô¡Ìôôõ}…Ñ¥Ù•…Ð¤üœ…Ñ¥Ù”œèœœì(€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰Í•±…Ðœ­…Ñ¥Ù”¬œˆ½¹±¥¬ô‰½Á•¹…Ñ•½Éä¡Ñ¡¥Ì¹•ÑÑÑÉ¥‰ÕÑ”¡qp‘…Ñ„µqpœ¤¤ˆ‘…Ñ„µŒôˆœ­•ÍÑÑÈ¡Ì¤¬œˆøœ(€€€€€€¬œñÍÁ…¸øœ­•ÍŒ¡Ì¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰¹Ðˆøœ¬¡‰å9…µ•mÍuñðÀ¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰¡•ØˆùqÔÈÀÍð½ÍÁ…¸øð½‘¥Øøœì(€ô(€‰½à¹¥¹¹•É!Q50õ¡Ñµ°ì)ô()…Íå¹Œ™Õ¹Ñ¥½¸½Á•¹…Ñ•½Éä¡…Ð¥ì(€}…Ñ¥Ù•…Ðõ…Ðì(€É•¹‘•ÉM•±•Ñ• ¤ì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% !•…œ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ¥±Ñ•È¡…¹¹•±Ìœ¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% 1¥ÍÐœ¤ì(€•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù1½…‘¥¹œ¸¸¸ð½ÍÁ…¸øœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½¡…¹¹•±ÌýÄô™…Ðôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡…Ð¤¤ì(€¥˜ …È¹±½•‘}¥¹ñð…È¹¡…¹¹•±Ì¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù½Õ±¹½Ð±½…¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€}¡…¹¹•±ÌõÈ¹¡…¹¹•±Ìì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% !•…œ¤¹¥¹¹•É!Q50õÑÈ ¥±Ñ•È¡…¹¹•±Ìœ¤¬œ€ñÍÁ…¸±…ÍÌô‰µÕÑ•ˆø œ­•ÍŒ¡…Ð¤¬œ¤ð½ÍÁ…¸øœì(€É•¹‘•É ¤ì)ô)™Õ¹Ñ¥½¸É•¹‘•É ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% 1¥ÍÐœ¤ì(€¥˜ …}¡…¹¹•±Ì¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼¡…¹¹•±Ì¥¸Ñ¡¥Ì…Ñ•½Éä¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€±•Ð¡Ñµ°ôœœì(€™½È¡½¹ÍÐŒ½˜}¡…¹¹•±Ì¥ì(€€€½¹ÍÐ¥¹Á°õ}Á±…å±¥ÍÐ¹¡…Ì¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤üœ¡•­•œèœœì(€€€¡Ñµ°¬ôœñ±…‰•°±…ÍÌô‰¡É½Üˆøñ¥¹ÁÕÐÑåÁ”ô‰¡•­‰½àˆ±…ÍÌô‰¬ˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆœ­¥¹Á°¬œøœ(€€€€€€­¡…¹¹•±1½¼¡Œ°µ¥¹¤œ¤¬œñÍÁ…¸±…ÍÌô‰¡¹…µ”ˆøœ­•ÍŒ¡Œ¹¹…µ”¤¬¡Œ¹ÅÕ…±¥ÑäüœñÍÁ…¸±…ÍÌô‰Ñ…œˆøœ­•ÍŒ¡Œ¹ÅÕ…±¥Ñä¤¬œð½ÍÁ…¸øœèœœ¤¬œð½ÍÁ…¸øð½±…‰•°øœì(€ô(€•°¹¥¹¹•É!Q50õ¡Ñµ°ì)ô)™Õ¹Ñ¥½¸Q¥¬¡½¸¥í‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹¬œ¤¹™½É… ¡™Õ¹Ñ¥½¸¡Œ¥íŒ¹¡•­•õ½¸íô¤íô()™Õ¹Ñ¥½¸…‘‘Q¥­•‘Q½A±…å±¥ÍÐ ¥ì(€½¹ÍÐÑ¥­•õ¹•ÜM•Ð¡ÉÉ…ä¹™É½´¡‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹¬é¡•­•œ¤¤¹µ…À¡™Õ¹Ñ¥½¸¡Œ¥íÉ•ÑÕÉ¸Œ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤íô¤¤ì(€™½È¡½¹ÍÐŒ½˜}¡…¹¹•±Ì¥ì(€€€½¹ÍÐÍ¥õMÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤ì(€€€¥˜¡Ñ¥­•¹¡…Ì¡Í¥¤¥}Á±…å±¥ÍÐ¹Í•Ð¡Í¥±í¹…µ”éŒ¹¹…µ”±ÕÉ°éŒ¹ÕÉ°±…Ñ•½ÉäéŒ¹…Ñ•½Éä±±½¼éŒ¹±½½ñðœô¤ì(€ô(€É•¹‘•ÉA±…å±¥ÍÐ ¤ì)ô)™Õ¹Ñ¥½¸É•¹‘•ÉA±…å±¥ÍÐ ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á±1¥ÍÐœ¤ì(€½¹ÍÐ¹Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á±½Õ¹Ðœ¤ì(€¹Ð¹Ñ•áÑ½¹Ñ•¹Ðõ}Á±…å±¥ÍÐ¹Í¥é”ü œ œ­}Á±…å±¥ÍÐ¹Í¥é”¬œ¤œ¤èœœì(€¥˜ …}Á±…å±¥ÍÐ¹Í¥é”¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆùQ¥­•¡…¹¹•±Ì±…¹¡•É”¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€±•Ð¡Ñµ°ôœœì(€™½È¡½¹ÍÐmÍ¥±t½˜}Á±…å±¥ÍÐ¥ì(€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰Á±¥Ñ•´ˆøñÍÁ…¸±…ÍÌô‰àˆ½¹±¥¬ô‰Á±I•µ½Ù”¡qpœœ­•ÍÑÑÈ¡Í¥¤¬qpœ¤ˆùqÔÈÜÄÔð½ÍÁ…¸øœ(€€€€€€­¡…¹¹•±1½¼¡íÍÑÉ•…µ}¥éÍ¥±±½¼éŒ¹±½½ô°µ¥¹¤œ¤¬œñ‘¥Ø±…ÍÌô‰¡¹…µ”ˆøœ­•ÍŒ¡Œ¹¹…µ”¤¬œð½‘¥Øøð½‘¥Øøœì(€ô(€•°¹¥¹¹•É!Q50õ¡Ñµ°ì)ô)™Õ¹Ñ¥½¸Á±I•µ½Ù”¡Í¥¥ì(€}Á±…å±¥ÍÐ¹‘•±•Ñ”¡Í¥¤íÉ•¹‘•ÉA±…å±¥ÍÐ ¤ì(€€¼¼…±Í¼Õ¹Ñ¥¬¥¸Ñ¡”±¥ÍÐ¥˜Ù¥Í¥‰±”(€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹¬œ¤¹™½É… ¡™Õ¹Ñ¥½¸¡Œ¥í¥˜¡Œ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤ôôõÍ¥¥Œ¹¡•­•õ™…±Í”íô¤ì)ô)™Õ¹Ñ¥½¸±•…ÉA±…å±¥ÍÐ ¥í}Á±…å±¥ÍÐ¹±•…È ¤íÉ•¹‘•ÉA±…å±¥ÍÐ ¤íQ¥¬¡™…±Í”¤íô)™Õ¹Ñ¥½¸±•…ÉM•±•Ñ•‘…ÑÌ ¥í}Í•±…ÑÌ¹±•…È ¤í}…Ñ¥Ù•…Ðõ¹Õ±°íÉ•¹‘•É…Ñ1¥ÍÐ ¤íÉ•¹‘•ÉM•±•Ñ• ¤í‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% 1¥ÍÐœ¤¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù±¥¬„Í•±•Ñ•…Ñ•½ÉäÑ¼Í•”¥ÑÌ¡…¹¹•±Ì¸ð½ÍÁ…¸øœí‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% !•…œ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ¥±Ñ•È¡…¹¹•±Ìœ¤íô()…Íå¹Œ™Õ¹Ñ¥½¸‰Õ¥±‘4ÍT¡µ½‘”¥ì(€½¹ÍÐ…ÑÌõÉÉ…ä¹™É½´¡}Í•±…ÑÌ¤ì(€¥˜ ……ÑÌ¹±•¹Ñ ¥í…±•ÉÐ Q¥¬…Ð±•…ÍÐ½¹”…Ñ•½Éä½¸Ñ¡”±•™Ð™¥ÉÍÐ¸œ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÉ•ÍÀõ…Ý…¥Ð™•Ñ  œ½…Á¤½´ÍÔœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô°(€€€‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íµ½‘”è…Ñ•½É¥•Ìœ±…Ñ•½É¥•Ìé…ÑÍô¥ô¤ì(€¥˜ …É•ÍÀ¹½¬¥í…±•ÉÐ …¥±•Ñ¼‰Õ¥±4ÍT¸œ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ‰±½ˆõ…Ý…¥ÐÉ•ÍÀ¹‰±½ˆ ¤ì(€½¹ÍÐÕÉ°õUI0¹É•…Ñ•=‰©•ÑUI0¡‰±½ˆ¤ì(€½¹ÍÐ„õ‘½Õµ•¹Ð¹É•…Ñ•±•µ•¹Ð „œ¤ì(€„¹¡É•˜õÕÉ°í„¹‘½Ý¹±½…ô…Ñ•½É¥•Ì¹´ÍÔœí‘½Õµ•¹Ð¹‰½‘ä¹…ÁÁ•¹‘¡¥±¡„¤í„¹±¥¬ ¤ì(€Í•ÑQ¥µ•½ÕÐ¡™Õ¹Ñ¥½¸ ¥íUI0¹É•Ù½­•=‰©•ÑUI0¡ÕÉ°¤í„¹É•µ½Ù” ¤íô°ÔÀÀ¤ì)ô()…Íå¹Œ™Õ¹Ñ¥½¸‰Õ¥±‘A±…å±¥ÍÑ4ÍT ¥ì(€¥˜ …}Á±…å±¥ÍÐ¹Í¥é”¥í…±•ÉÐ A±…å±¥ÍÐ¥Ì•µÁÑä¸Q¥¬¡…¹¹•±Ì…¹±¥¬€‰‘Ñ¥­•ˆ¸œ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ¥‘ÌõÉÉ…ä¹™É½´¡}Á±…å±¥ÍÐ¹­•åÌ ¤¤ì(€½¹ÍÐÉ•ÍÀõ…Ý…¥Ð™•Ñ  œ½…Á¤½´ÍÔœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô°(€€€‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íµ½‘”è¡…¹¹•±Ìœ±ÍÑÉ•…µ}¥‘Ìé¥‘Íô¥ô¤ì(€¥˜ …É•ÍÀ¹½¬¥í…±•ÉÐ …¥±•Ñ¼‰Õ¥±4ÍT¸œ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ‰±½ˆõ…Ý…¥ÐÉ•ÍÀ¹‰±½ˆ ¤ì(€½¹ÍÐÕÉ°õUI0¹É•…Ñ•=‰©•ÑUI0¡‰±½ˆ¤ì(€½¹ÍÐ„õ‘½Õµ•¹Ð¹É•…Ñ•±•µ•¹Ð „œ¤ì(€„¹¡É•˜õÕÉ°í„¹‘½Ý¹±½…ôÁ±…å±¥ÍÐ¹´ÍÔœí‘½Õµ•¹Ð¹‰½‘ä¹…ÁÁ•¹‘¡¥±¡„¤í„¹±¥¬ ¤ì(€Í•ÑQ¥µ•½ÕÐ¡™Õ¹Ñ¥½¸ ¥íUI0¹É•Ù½­•=‰©•ÑUI0¡ÕÉ°¤í„¹É•µ½Ù” ¤íô°ÔÀÀ¤ì)ô((¼¼I•Í•Ð½¹±äÑ¡”Ñ•µÁ½É…ÉäA±…å±¥ÍÐÍ•…É¡•Ìì­••ÀÑ¡”ÕÍ•ÈÌ‰Õ¥±‘•ÈÍ•±•Ñ¥½¹Ì¥¹Ñ…Ð¸)™Õ¹Ñ¥½¸É•Í•ÑA±…å±¥ÍÑM•…É  ¥ì(€½¹ÍÐÄõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Äœ¤±…ÑÄõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …ÑÄœ¤ì(€½¹ÍÐÈõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É•ÍÕ±ÑÌœ¤±ÑÈõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …ÑÉ•ÍÕ±ÑÌœ¤ì(€¥˜¡Ä¥Ä¹Ù…±Õ”ôœœí¥˜¡…ÑÄ¥…ÑÄ¹Ù…±Õ”ôœœí¥˜¡È¥È¹¥¹¹•É!Q50ôœœí¥˜¡ÑÈ¥ÑÈ¹¥¹¹•É!Q50ôœœì(€½¹ÍÐ‰Õ¥±‘•Èõ‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½È œ¡…¹¹•±ÍY¥•Ü€¹ Ðœ¤ì(€¥˜¡‰Õ¥±‘•È¥‰Õ¥±‘•È¹ÍÉ½±±%¹Ñ½Y¥•Ü¡í‰•¡…Ù¥½ÈèÍµ½½Ñ œ±‰±½¬èÍÑ…ÉÐô¤ì)ô((¼¼I¥¡Ðµ½±Õµ¸¡…¹¹•°Í•…É ½¸Ñ¡”M•…É Á…”€¡Í¥µÁ±”Ù•ÉÍ¥½¸¤)…Íå¹Œ™Õ¹Ñ¥½¸‘½…Ñ•½ÉåM•…É  ¥ì(€½¹ÍÐÄô¡‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …ÑÄœ¤¹Ù…±Õ•ñðœœ¤¹ÑÉ¥´ ¤¹Ñ½1½Ý•É…Í” ¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …ÑÉ•ÍÕ±ÑÌœ¤ì(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ 1½…‘¥¹œ¸¸¸œ¤¬œð½‘¥Øøœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½…Ñ•½É¥•Ìœ¤ì(€¥˜ …È¹±½•‘}¥¸¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù1½œ¥¸Ù¥„€ñ„½¹±¥¬ô‰Í¡½ÝM•ÑÑ¥¹Ì ¤ˆÍÑå±”ô‰½±½ÈéÙ…È ´µ…Œ¤íÕÉÍ½ÈéÁ½¥¹Ñ•ÈˆùM•ÑÑ¥¹Ìð½„øÑ¼Í•…É …Ñ•½É¥•Ì¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€±•Ð…ÑÌô¡È¹…Ñ•½É¥•Íññmt¤ì(€¥˜¡Ä¥…ÑÌõ…ÑÌ¹™¥±Ñ•È¡™Õ¹Ñ¥½¸¡Œ¥íÉ•ÑÕÉ¸Œ¹¹…µ”¹Ñ½1½Ý•É…Í” ¤¹¥¹‘•á=˜¡Ä¤øôÀíô¤ì(€¥˜ ……ÑÌ¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù9¼…Ñ•½É¥•Ì™½Õ¹œ¬¡Äüœ™½È€ˆœ­•ÍŒ¡Ä¤¬œˆœèœœ¤¬œ¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€…Ý…¥ÐÉ•™É•Í¡…ÙMÑ…Ñ” ¤ì(€±•Ð ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸èÙÁà€Àˆøœ­…ÑÌ¹±•¹Ñ ¬œ€œ­ÑÈ …Ñ•½É¥•Ìœ¤¹Ñ½1½Ý•É…Í” ¤¬œð½‘¥Øøœì(€™½È¡½¹ÍÐŒ½˜…ÑÌ¥ì(€€€½¹ÍÐ™…Øõ}™…Ù…ÑM•Ð¹¡…Ì¡Œ¹¹…µ”¤üœ½¸œèœœì(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰¡É½Üˆøœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µ™…Ù…Ðôˆœ­•ÍÑÑÈ¡Œ¹¹…µ”¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆùqÔÈØÀÔð½ÍÁ…¸øœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰¡¹…µ”ˆøœ­}™±…½È¡Œ¹¹…µ”¤¬œ€œ­•ÍŒ¡Œ¹¹…µ”¤¬œ€ñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­Œ¹½Õ¹Ð¬œð½ÍÁ…¸øð½ÍÁ…¸øð½‘¥Øøœì(€ô(€•°¹¥¹¹•É!Q50õ ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Ñ½±•…Ù…Ð¡¹…µ”±ÍÑ…É°¥ì(€±•ÐÈì(€¥˜¡}™…Ù…ÑM•Ð¹¡…Ì¡¹…µ”¤¥íÈõ…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÉ•µ½Ù•}…Ðœ±…Ñ•½Éäé¹…µ•ô¤í}™…Ù…ÑM•Ð¹‘•±•Ñ”¡¹…µ”¤íô(€•±Í•íÈõ…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸è…‘‘}…ÑÌœ±…Ñ•½É¥•Ìém¹…µ•uô¤í}™…Ù…ÑM•Ð¹…‘¡¹…µ”¤íô(€¥˜¡ÍÑ…É°¥ÍÑ…É°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½¸œ±}™…Ù…ÑM•Ð¹¡…Ì¡¹…µ”¤¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸‘½¡…¹¹•±M•…É ¡¥¹ÁÕÑ%°Ñ…É•Ñ%¥ì(€½¹ÍÐÄõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å%¡¥¹ÁÕÑ%¤¹Ù…±Õ”¹ÑÉ¥´ ¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å%¡Ñ…É•Ñ%¤ì(€•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆùM•…É¡¥¹œå½ÕÈ¡…¹¹•±Ì¸¸¸ð½ÍÁ…¸øœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½¡…¹¹•±ÌýÄôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Ä¤¬œ™…Ðôœ¤ì(€¥˜¡È¹•ÉÉ½È¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡È¹•ÉÉ½È¤¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€¥˜ …È¹±½•‘}¥¸¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù1½œ¥¸Ù¥„€ñ„½¹±¥¬ô‰Í¡½ÝM•ÑÑ¥¹Ì ¤ˆÍÑå±”ô‰½±½ÈéÙ…È ´µ…Œ¤íÕÉÍ½ÈéÁ½¥¹Ñ•ÈˆùM•ÑÑ¥¹Ìð½„øÑ¼Í•…É å½ÕÈ¡…¹¹•±Ì¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€¥˜ …È¹¡…¹¹•±Ì¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù9¼¡…¹¹•±Ì™½Õ¹œ¬¡Äüœ™½È€ˆœ­•ÍŒ¡Ä¤¬œˆœèœœ¤¬œ¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€…Ý…¥ÐÉ•™É•Í¡…ÙMÑ…Ñ” ¤ì(€±•Ð¡Ñµ°ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸èÙÁà€Àˆøœ­È¹Í¡½Ý¸¬¡È¹Ñ½Ñ…°ùÈ¹Í¡½Ý¸ü œ½˜€œ­È¹Ñ½Ñ…°¬œ€¡ÑåÁ”µ½É”Ñ¼¹…ÉÉ½Ü¤œ¤èœœ¤¬œ¡…¹¹•°œ¬¡È¹Ñ½Ñ…°ôôôÄüœœèÌœ¤¬œð½‘¥Øøœì(€™½È¡½¹ÍÐŒ½˜È¹¡…¹¹•±Ì¥ì(€€€½¹ÍÐ™…Øõ}™…Ù¡…¹M•Ð¹¡…Ì¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤üœ½¸œèœœì(€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰¡É½Üˆøœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡Œ¹¹…µ”¤¬œˆ‘…Ñ„µ…Ðôˆœ­•ÍÑÑÈ¡Œ¹…Ñ•½Éåñðœœ¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øœ(€€€€€€­¡…¹¹•±1½¼¡Œ¤¬œñ‘¥Ø±…ÍÌô‰¡¹…µ”ˆøœ­•ÍŒ¡Œ¹¹…µ”¤¬¡Œ¹ÅÕ…±¥ÑäüœñÍÁ…¸±…ÍÌô‰Ñ…œˆøœ­•ÍŒ¡Œ¹ÅÕ…±¥Ñä¤¬œð½ÍÁ…¸øœèœœ¤(€€€€€€¬¡Œ¹…Ñ•½Éäüœ€ñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡Œ¹…Ñ•½Éä¤¬œð½ÍÁ…¸øœèœœ¤(€€€€€€¬œñ‘¥Ø±…ÍÌô‰¡ÕÉ°ˆøœ­•ÍŒ¡Œ¹ÕÉ°¤¬œð½‘¥Øøð½‘¥Øøœ(€€€€€€¬œñ‘¥ØÍÑå±”ô‰‘¥ÍÁ±…äé™±•àí™±•àµÍ¡É¥¹¬èÀˆøœ­Á±…å‰Ñ¹Ì¡Œ¹ÍÑÉ•…µ}¥±Œ¹¹…µ”±Œ¹ÕÉ°¤¬œð½‘¥Øøð½‘¥Øøœì(€ô(€•°¹¥¹¹•É!Q50õ¡Ñµ°ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸…Á¤¡À±¼¥ì(€½¹ÍÐÈõ…Ý…¥Ð™•Ñ ¡À±¼¤í±•Ð¨ì(€ÑÉåí¨õ…Ý…¥ÐÈ¹©Í½¸ ¤íõ…Ñ ¡”¥íÉ•ÑÕÉ¸í•ÉÉ½Èè%¹Ù…±¥Í•ÉÙ•ÈÉ•ÍÁ½¹Í”œ±ÍÑ…ÑÕÌéÈ¹ÍÑ…ÑÕÍôíô(€¥˜ …©ññÑåÁ•½˜¨„ôô½‰©•Ðœ¥¨õí‘…Ñ„é©ôì(€¥˜ …È¹½¬˜˜…¨¹•ÉÉ½È¥¨¹•ÉÉ½Èô!QQ@€œ­È¹ÍÑ…ÑÕÌì(€¨¹}¡ÑÑÁMÑ…ÑÕÌõÈ¹ÍÑ…ÑÕÌí¨¹}¡ÑÑÁ=¬õÈ¹½¬ì(€É•ÑÕÉ¸¨ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸™…ÙA½ÍÐ¡‰½‘ä¥íÉ•ÑÕÉ¸…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡‰½‘ä¥ô¤íô)…Íå¹Œ™Õ¹Ñ¥½¸É•™É•Í¡MÑ…ÑÕÌ ¥ì(€½¹ÍÐÌõ…Ý…¥Ð…Á¤ œ½…Á¤½ÍÑ…ÑÕÌœ¤ì(€ÍÑ…ÑÕÌ¹¥¹¹•É!Q50ô…Ì¹½¹™¥ÕÉ•üœñÍÁ…¸±…ÍÌô‰•ÉÈˆù9½Ð½¹™¥ÕÉ•€™µ‘…Í ì½Á•¸M•ÑÑ¥¹Ìð½ÍÁ…¸øœ(€€€€è¡Ì¹¡…¹¹•±}½Õ¹Ð„õ¹Õ±°ýÌ¹¡…¹¹•±}½Õ¹Ð¬œ¡…¹¹•±Ì±½…‘•œè½¹™¥ÕÉ•œ¤ì(€½¹ÍÐÍ±¥‘•Èõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ…Ñ¡MÑÉ¥Ðœ¤°Ù…±Õ”õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ…Ñ¡MÑÉ¥ÑY…±Õ”œ¤ì(€¥˜¡Í±¥‘•È˜™Ì¹µ…Ñ¡}Ñ¡É•Í¡½±„õ¹Õ±°¥íÍ±¥‘•È¹Ù…±Õ”õ9Õµ‰•È¡Ì¹µ…Ñ¡}Ñ¡É•Í¡½±¤¹Ñ½¥á• È¤íÙ…±Õ”¹Ñ•áÑ½¹Ñ•¹ÐõÍ±¥‘•È¹Ù…±Õ”íô)ô)½¹ÍÐ}QY}U%}=U9QI%Lõl(€l¹¼œ°ŸÂ~ÏÂ~Ðœ°9½ÉÝ…ät±lˆœ°ŸÂ~³Â~œœ°U¹¥Ñ•-¥¹‘½´t±lÕÌœ°ŸÂ~ëÂ~àœ°U¹¥Ñ•MÑ…Ñ•Ìt°(€lÁÐœ°ŸÂ~×Â~äœ°A½ÉÑÕ…°t±l¥”œ°ŸÂ~»Â~¨œ°%É•±…¹t±l•Ìœ°ŸÂ~«Â~àœ°MÁ…¥¸t°(€l‘”œ°ŸÂ~§Â~¨œ°•Éµ…¹ät±l¥Ðœ°ŸÂ~»Â~äœ°%Ñ…±ät±l™Èœ°ŸÂ~¯Â~Üœ°É…¹”t°(€l¹°œ°ŸÂ~ÏÂ~Äœ°9•Ñ¡•É±…¹‘Ìt±l‰”œ°ŸÂ~ŸÂ~¨œ°	•±¥Õ´t±l‘¬œ°ŸÂ~§Â~Àœ°•¹µ…É¬t°(€lÍ”œ°ŸÂ~ãÂ~¨œ°MÝ•‘•¸t±l™¤œ°ŸÂ~¯Â~¸œ°¥¹±…¹t±l…Ðœ°ŸÂ~›Â~äœ°ÕÍÑÉ¥„t°(€l œ°ŸÂ~£Â~´œ°MÝ¥Ñé•É±…¹t±lÁ°œ°ŸÂ~×Â~Äœ°A½±…¹t±l„œ°ŸÂ~£Â~˜œ°…¹…‘„t°(€l…Ôœ°ŸÂ~›Â~èœ°ÕÍÑÉ…±¥„t±l‰Èœ°ŸÂ~ŸÂ~Üœ°	É…é¥°t±lµàœ°ŸÂ~ËÂ~ôœ°5•á¥¼t)tì)™Õ¹Ñ¥½¸Í•±•Ñ•‘QÙÕ¥‘•½Õ¹ÑÉ¥•Ì ¥í½¹ÍÐØõMÑÉ¥¹œ¡Í}Œ¹Ù…±Õ•ñðœœ¤¹ÍÁ±¥Ð œ°œ¤¹µ…À¡ØôùØ¹ÑÉ¥´ ¤¹Ñ½1½Ý•É…Í” ¤¤¹™¥±Ñ•È¡	½½±•…¸¤íÉ•ÑÕÉ¸Ø¹±•¹Ñ ýØél¹¼œ°ˆœ°ÕÌœ°ÁÐœ°¥”œ°•Ìœ°‘”œ°¥Ðœ°™Èœ°¹°œ°‰”œ°‘¬œ°Í”tíô)™Õ¹Ñ¥½¸É•¹‘•É½Õ¹ÑÉåA¥­•È¡Ù…±Õ•Ì¥ì(€½¹ÍÐÍ•±•Ñ•õ¹•ÜM•Ð ¡Ù…±Õ•Íññmt¤¹µ…À¡ØôùMÑÉ¥¹œ¡Ø¤¹Ñ½1½Ý•É…Í” ¤ôôôÕ¬œüˆœéMÑÉ¥¹œ¡Ø¤¹Ñ½1½Ý•É…Í” ¤¤¤ì(€½¹ÍÐ­¹½Ý¸õ¹•ÜM•Ð¡}QY}U%}=U9QI%L¹µ…À¡É½ÜôùÉ½ÝlÁt¤¤ì(€½¹ÍÐÉ½ÝÌõ}QY}U%}=U9QI%L¹µ…À¡É½Üôø¡í½‘”éÉ½ÝlÁt±™±…œéÉ½ÝlÅt±¹…µ”éÉ½ÝlÉt±Õ¹ÍÕÁÁ½ÉÑ•é™…±Í•ô¤¤ì(€™½È¡½¹ÍÐ½‘”½˜Í•±•Ñ•¥¥˜ …­¹½Ý¸¹¡…Ì¡½‘”¤¥É½ÝÌ¹ÁÕÍ ¡í½‘”é½‘”±™±…œèŸŠjƒ¾â<œ±¹…µ”éÑÈ U¹ÍÕÁÁ½ÉÑ•Í…Ù•½‘”œ¤±Õ¹ÍÕÁÁ½ÉÑ•éÑÉÕ•ô¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ½Õ¹ÑÉåA¥­•Èœ¤í¥˜ …•°¥É•ÑÕÉ¸ì(€•°¹¥¹¹•É!Q50õÉ½ÝÌ¹µ…À¡É½Üôøœñ±…‰•°±…ÍÌô‰½Õ¹ÑÉå¡½¥”œ¬¡Í•±•Ñ•¹¡…Ì¡É½Ü¹½‘”¤üœ½¸œèœœ¤¬¡É½Ü¹Õ¹ÍÕÁÁ½ÉÑ•üœÕ¹ÍÕÁÁ½ÉÑ•œèœœ¤¬œˆøñ¥¹ÁÕÐÑåÁ”ô‰¡•­‰½àˆÙ…±Õ”ôˆœ­•ÍÑÑÈ¡É½Ü¹½‘”¤¬œˆœ¬¡Í•±•Ñ•¹¡…Ì¡É½Ü¹½‘”¤üœ¡•­•œèœœ¤¬œ½¹¡…¹”ô‰Íå¹½Õ¹ÑÉåA¥­•È ¤ˆøñÍÁ…¸±…ÍÌô‰½Õ¹ÑÉå™±…œˆøœ­É½Ü¹™±…œ¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰½Õ¹ÑÉå¹…µ”ˆøœ­•ÍŒ¡É½Ü¹¹…µ”¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰½Õ¹ÑÉå½‘”ˆøœ­•ÍŒ¡É½Ü¹½‘”¹Ñ½UÁÁ•É…Í” ¤¤¬œð½ÍÁ…¸øð½±…‰•°øœ¤¹©½¥¸ œœ¤ì(€Íå¹½Õ¹ÑÉåA¥­•È ¤ì)ô)™Õ¹Ñ¥½¸Íå¹½Õ¹ÑÉåA¥­•È ¥ì(€½¹ÍÐ¡•­•õÉÉ…ä¹™É½´¡‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ½Õ¹ÑÉåA¥­•È¥¹ÁÕÐé¡•­•œ¤¤¹µ…À¡¥¹ÁÕÐôù¥¹ÁÕÐ¹Ù…±Õ”¤ì(€Í}Œ¹Ù…±Õ”õ¡•­•¹©½¥¸ œ°œ¤ì(€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ½Õ¹ÑÉåA¥­•È€¹½Õ¹ÑÉå¡½¥”œ¤¹™½É… ¡±…‰•°ôù±…‰•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½¸œ±±…‰•°¹ÅÕ•ÉåM•±•Ñ½È ¥¹ÁÕÐœ¤¹¡•­•¤¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Í…Ù•5…Ñ¡MÑÉ¥Ñ¹•ÍÌ¡Ù…±Õ”¥ì(€½¹ÍÐÍÑÉ¥Ðõ5…Ñ ¹µ…à À¸ÐÀ±5…Ñ ¹µ¥¸ À¸àÀ±Á…ÉÍ•±½…Ð¡Ù…±Õ”¥ñðÀ¸ØÈ¤¤ì(€ÑÉåí…Ý…¥Ð…Á¤ œ½…Á¤½µ…Ñ¡}ÍÑÉ¥Ñ¹•ÍÌœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íµ…Ñ¡}Ñ¡É•Í¡½±éÍÑÉ¥Ñô¥ô¤íõ…Ñ ¡”¥íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘M•ÑÑ¥¹Ì ¥ì(€½¹ÍÐŒõ…Ý…¥Ð…Á¤ œ½…Á¤½½¹™¥œœ¤ì(€Í}¡½ÍÐ¹Ù…±Õ”õŒ¹áÑÉ•…µ}¡½ÍÑñðœœì(€Í}ÕÍ•È¹Ù…±Õ”õŒ¹áÑÉ•…µ}ÕÍ•ÉñðœœíÍ}Á…ÍÌ¹Ù…±Õ”õŒ¹áÑÉ•…µ}Á…ÍÍñðœœì(€Í}•áÐ¹Ù…±Õ”õŒ¹ÍÑÉ•…µ}•áÑñðÑÌœì(€Í}Œ¹Ù…±Õ”ô¡Œ¹½Õ¹ÑÉ¥•Íññl¹¼œ°ˆœ°ÕÌœ°ÁÐœ°¥”œ°•Ìœ°‘”œ°¥Ðœ°™Èœ°¹°œ°‰”œ°‘¬œ°Í”t¤¹©½¥¸ œ°œ¤ì(€É•¹‘•É½Õ¹ÑÉåA¥­•È¡Œ¹½Õ¹ÑÉ¥•ÍññÍ•±•Ñ•‘QÙÕ¥‘•½Õ¹ÑÉ¥•Ì ¤¤ì(€Í}ÍÑ…ÉÐ¹Ù…±Õ”õŒ¹ÍÑ…ÉÑ}Í•Ñ¥½¹ñðµå±¥ÍÐœì(€Í}¡•­Í¡½ÝÌ¹¡•­•ô„…Œ¹¡•­}Í¡½ÝÍ}½¹}ÍÑ…ÉÑÕÀì(€Í}É•™É•Í¡¥ÁÑØ¹¡•­•ô„…Œ¹É•™É•Í¡}¥ÁÑÙ}½¹}ÍÑ…ÉÑÕÀì(€Í}É•™É•Í¡ÍÁ½ÉÑÌ¹¡•­•ô„…Œ¹É•™É•Í¡}ÍÁ½ÉÑÍ}½¹}ÍÑ…ÉÑÕÀì(€Í}ÁÉ½™¥±”¹Ù…±Õ”õŒ¹ÁÉ½™¥±•}¹…µ•ñðœœì(€Í}±…¹œ¹Ù…±Õ”õŒ¹ÁÉ•™•ÉÉ•‘}±…¹Õ…•ñð•¸œì(€}Í•±•Ñ•‘µ‰±•´õ}AI=%1}5	15MmŒ¹ÁÉ½™¥±•}•µ‰±•µtýŒ¹ÁÉ½™¥±•}•µ‰±•´èÑÙÍÑ…¬œíÉ•¹‘•Éµ‰±•µA¥­•È ¤ì(€Í}µå±¥ÍÑ±…å½ÕÐ¹Ù…±Õ”õl‰…±…¹•œ°ÍÁ½Ñ±¥¡Ðœ°Ñ¥µ•±¥¹”œ°¡Õˆt¹¥¹±Õ‘•Ì¡Œ¹µå±¥ÍÑ}±…å½ÕÐ¤ýŒ¹µå±¥ÍÑ}±…å½ÕÐèÑ¥µ•±¥¹”œì(€Í}™½½Ñ‰…±°¹¡•­•õŒ¹™½½Ñ‰…±±}•¹…‰±•„ôõ™…±Í”ì(€Í}˜Ä¹¡•­•õŒ¹˜Å}•¹…‰±•„ôõ™…±Í”ì(€Í}…µ•Ì¹¡•­•õŒ¹…µ•Í}•¹…‰±•„ôõ™…±Í”ì(€Í}‰…­É½Õ¹¹Ù…±Õ”õl™±½…Ðœ°…Í¥¤œ°½™˜t¹¥¹±Õ‘•Ì¡Œ¹‰…­É½Õ¹‘}ÍÑå±”¤ýŒ¹‰…­É½Õ¹‘}ÍÑå±”è¡Œ¹‘•½É…Ñ¥½¹Í}•¹…‰±•ôôõ™…±Í”ü½™˜œè™±½…Ðœ¤ì(€Í}…ÕÑ½Í¡ÕÑ‘½Ý¸¹Ù…±Õ”õMÑÉ¥¹œ¡Œ¹…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•ÍñðÀ¤ì(€±½…‘ÉÑÝ½É­…¡•M¥é” ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Í…Ù•M•ÑÑ¥¹Ì ¥ì(€½¹ÍÐ‰½‘äõíáÑÉ•…µ}¡½ÍÐéÍ}¡½ÍÐ¹Ù…±Õ”±áÑÉ•…µ}ÕÍ•ÈéÍ}ÕÍ•È¹Ù…±Õ”°(€€€áÑÉ•…µ}Á…ÍÌéÍ}Á…ÍÌ¹Ù…±Õ”±ÍÑÉ•…µ}•áÐéÍ}•áÐ¹Ù…±Õ”°(€€€½Õ¹ÑÉ¥•ÌéÍ•±•Ñ•‘QÙÕ¥‘•½Õ¹ÑÉ¥•Ì ¤°(€€€ÍÑ…ÉÑ}Í•Ñ¥½¸éÍ}ÍÑ…ÉÐ¹Ù…±Õ”±¡•­}Í¡½ÝÍ}½¹}ÍÑ…ÉÑÕÀéÍ}¡•­Í¡½ÝÌ¹¡•­•°(€€€É•™É•Í¡}¥ÁÑÙ}½¹}ÍÑ…ÉÑÕÀéÍ}É•™É•Í¡¥ÁÑØ¹¡•­•±É•™É•Í¡}ÍÁ½ÉÑÍ}½¹}ÍÑ…ÉÑÕÀéÍ}É•™É•Í¡ÍÁ½ÉÑÌ¹¡•­•°(€€€ÁÉ½™¥±•}¹…µ”éÍ}ÁÉ½™¥±”¹Ù…±Õ”¹ÑÉ¥´ ¤±ÁÉ•™•ÉÉ•‘}±…¹Õ…”éÍ}±…¹œ¹Ù…±Õ”°(€€€ÁÉ½™¥±•}•µ‰±•´é}Í•±•Ñ•‘µ‰±•´±µå±¥ÍÑ}±…å½ÕÐéÍ}µå±¥ÍÑ±…å½ÕÐ¹Ù…±Õ”±™½½Ñ‰…±±}•¹…‰±•éÍ}™½½Ñ‰…±°¹¡•­•°(€€€˜Å}•¹…‰±•éÍ}˜Ä¹¡•­•±…µ•Í}•¹…‰±•éÍ}…µ•Ì¹¡•­•±‰…­É½Õ¹‘}ÍÑå±”éÍ}‰…­É½Õ¹¹Ù…±Õ”±‘•½É…Ñ¥½¹Í}•¹…‰±•éÍ}‰…­É½Õ¹¹Ù…±Õ”„ôô½™˜œ±¡¥‘•}µ‘}Ý¥¹‘½ÜéÑÉÕ”±…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•Ìé9Õµ‰•È¡Í}…ÕÑ½Í¡ÕÑ‘½Ý¸¹Ù…±Õ•ñðÀ¥ôì(€¥˜ …‰½‘ä¹…µ•Í}•¹…‰±•˜™‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôôô…µ•Ìœ¥‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôµå±¥ÍÐœì(€¥˜ …‰½‘ä¹˜Å}•¹…‰±•˜™‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôôôÉ…¥¹œœ¥‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôµå±¥ÍÐœì(€¥˜ …‰½‘ä¹™½½Ñ‰…±±}•¹…‰±•˜™‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôôôÑ•…µÌœ¥‰½‘ä¹ÍÑ…ÉÑ}Í•Ñ¥½¸ôµå±¥ÍÐœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½½¹™¥œœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡‰½‘ä¥ô¤ì(€Í}µÍœ¹Ñ•áÑ½¹Ñ•¹ÐõÈ¹½¬üM…Ù•¸œèÉÉ½ÈÍ…Ù¥¹œ¸œì(€¥˜¡È¹½¬¥íÍ•Ñ1…¹œ¡‰½‘ä¹ÁÉ•™•ÉÉ•‘}±…¹Õ…”¤í…ÁÁ±åAÉ½™¥±•½¹™¥œ¡‰½‘ä¤íÑ½…ÍÐ M…Ù•¸œ¤í¥˜ …‰½‘ä¹™½½Ñ‰…±±}•¹…‰±•˜˜…Ñ•…µÍY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥Í¡½Ý5å±¥ÍÐ ¤í¥˜ …‰½‘ä¹…µ•Í}•¹…‰±•˜˜……µ•ÍY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥Í¡½Ý5å±¥ÍÐ ¤í¥˜ …‰½‘ä¹˜Å}•¹…‰±•˜˜…É…¥¹Y¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥Í¡½Ý5å±¥ÍÐ ¤íô(€É•™É•Í¡MÑ…ÑÕÌ ¤ì)ô)™Õ¹Ñ¥½¸™½Éµ…Ñ	åÑ•Ì¡¸¥ì(€¸õ9Õµ‰•È¡¸¥ñðÀí¥˜¡¸ðÄÀÈÐ¥É•ÑÕÉ¸¸¬œœì(€½¹ÍÐÕ¹¥ÑÌõl-œ°5œ°tí±•Ð¤ô´Äì(€‘½í¸¼ôÄÀÈÐí¤¬¬íõÝ¡¥±”¡¸øôÄÀÈÐ˜™¤ñÕ¹¥ÑÌ¹±•¹Ñ ´Ä¤ì(€É•ÑÕÉ¸¸¹Ñ½¥á•¡¸øôÄÀüÄèÈ¤¬œ€œ­Õ¹¥ÑÍm¥tì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘ÉÑÝ½É­…¡•M¥é” ¥ì(€ÑÉåí½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½…ÉÑÝ½É­}…¡”œ¤íÍ}…ÉÑÍ¥é”¹Ñ•áÑ½¹Ñ•¹Ðõ™½Éµ…Ñ	åÑ•Ì¡È¹‰åÑ•Ì¤íô(€…Ñ ¡”¥íÍ}…ÉÑÍ¥é”¹Ñ•áÑ½¹Ñ•¹ÐôU¹…Ù…¥±…‰±”œíô)ô)…Íå¹Œ™Õ¹Ñ¥½¸±•…ÉÉÑÝ½É­…¡” ¥ì(€¥˜ …½¹™¥É´ ±•…È…±°‘½Ý¹±½…‘•Í¡½Ü…ÉÑÝ½É¬üœ¤¥É•ÑÕÉ¸ì(€ÑÉåì(€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½±•…É}…ÉÑÝ½É­}…¡”œ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜ …È¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½Éñð±•…È™…¥±•œ¤ì(€€€Í}…ÉÑÍ¥é”¹Ñ•áÑ½¹Ñ•¹ÐôœÀœíÑ½…ÍÐ ÉÑÝ½É¬…¡”±•…É•¸œ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ ½Õ±¹½Ð±•…È…ÉÑÝ½É¬…¡”¸œ¤íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸ÍÑ½ÁQY5…Ñ” ¥ì(€¥˜ …½¹™¥É´ MÑ½ÀQY5…Ñ”üMÑÉ•…µ¥¹œ±¥¹­Ì…¹Ñ¡”±½…°QY5…Ñ”Á…”Ý¥±°ÍÑ½ÀÕ¹Ñ¥°å½ÔÍÑ…ÉÐÑ¡”…ÁÀ……¥¸¸œ¤¥É•ÑÕÉ¸ì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½Í¡ÕÑ‘½Ý¸œ±íµ•Ñ¡½èA=MPô¤í¥˜¡¨¹•ÉÉ½Éñð…¨¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È Í¡ÕÑ‘½Ý¸™…¥±•œ¤ì(€€€‘½Õµ•¹Ð¹‰½‘ä¹¥¹¹•É!Q50ôœñ‘¥ØÍÑå±”ô‰µ¥¸µ¡•¥¡ÐèÄÀÁÙ í‘¥ÍÁ±…äéÉ¥íÁ±…”µ¥Ñ•µÌé•¹Ñ•Èí‰…­É½Õ¹èŒÁÄÀÄÌí½±½Èè”Ý”Ý”Üí™½¹Ðµ™…µ¥±äéÍåÍÑ•´µÕ¤±Í…¹ÌµÍ•É¥˜ˆøñ‘¥ØÍÑå±”ô‰Ñ•áÐµ…±¥¸é•¹Ñ•Èˆøñ‘¥ØÍÑå±”ô‰™½¹ÐµÍ¥é”èÔÑÁàˆûÂ~Nèð½‘¥Øøñ ÄùQY5…Ñ”¡…ÌÍÑ½ÁÁ•ð½ ÄøñÀÍÑå±”ô‰½±½ÈèŒäääˆùe½Ô…¸±½Í”Ñ¡¥ÌÑ…ˆ¸MÑ…ÉÐQY5…Ñ”……¥¸Ý¡•¹•Ù•Èå½Ô…É”É•…‘ä¸ð½Àøð½‘¥Øøð½‘¥Øøœì(€õ…Ñ ¡”¥íÑ½…ÍÐ ½Õ±¹½ÐÍÑ½ÀQY5…Ñ”¸œ¤íô)ô)±•Ð}±…ÍÑÑ¥Ù¥ÑåA¥¹œôÀì)™Õ¹Ñ¥½¸µ…É­QY5…Ñ•Ñ¥Ù¥Ñä ¥í½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤í¥˜¡¹½Üµ}±…ÍÑÑ¥Ù¥ÑåA¥¹œðÈÀÀÀÀ¥É•ÑÕÉ¸í}±…ÍÑÑ¥Ù¥ÑåA¥¹œõ¹½Üí™•Ñ  œ½…Á¤½…Ñ¥Ù¥Ñäœ±íµ•Ñ¡½èA=MPœ±­••Á…±¥Ù”éÑÉÕ•ô¤¹…Ñ   ¤ôùíô¤íô)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È Á½¥¹Ñ•É‘½Ý¸œ±µ…É­QY5…Ñ•Ñ¥Ù¥Ñä±íÁ…ÍÍ¥Ù”éÑÉÕ•ô¤ì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ­•å‘½Ý¸œ±µ…É­QY5…Ñ•Ñ¥Ù¥Ñä±íÁ…ÍÍ¥Ù”éÑÉÕ•ô¤ì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È Ñ½Õ¡ÍÑ…ÉÐœ±µ…É­QY5…Ñ•Ñ¥Ù¥Ñä±íÁ…ÍÍ¥Ù”éÑÉÕ•ô¤ì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È Ù¥Í¥‰¥±¥Ñå¡…¹”œ±™Õ¹Ñ¥½¸ ¥í¥˜ …‘½Õµ•¹Ð¹¡¥‘‘•¸¥µ…É­QY5…Ñ•Ñ¥Ù¥Ñä ¤íô¤ì)Í•Ñ%¹Ñ•ÉÙ…°¡™Õ¹Ñ¥½¸ ¥ì(€½¹ÍÐÁ½ÁÕÀõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÁY¥‘•¼œ¤±±¥Ù”õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙY¥‘•¼œ¤ì(€¥˜ ¡Á½ÁÕÀ˜˜…Á½ÁÕÀ¹Á…ÕÍ•˜˜…Á½ÁÕÀ¹•¹‘•¥ñð¡±¥Ù”˜˜…±¥Ù”¹Á…ÕÍ•˜˜…±¥Ù”¹•¹‘•¤¥µ…É­QY5…Ñ•Ñ¥Ù¥Ñä ¤ì)ô°ÌÀÀÀÀ¤ì)µ…É­QY5…Ñ•Ñ¥Ù¥Ñä ¤ì)…Íå¹Œ™Õ¹Ñ¥½¸É•Í•Ñ½±‘MÑ…ÉÐ¡‰Ñ¸¥ì(€¥˜ …½¹™¥É´ ±•…ÈÁ•É™½Éµ…¹”…¡•Ì…¹É•±½…QY5…Ñ”™½È„½±µÍÑ…ÉÐÑ•ÍÐüœ¤¥É•ÑÕÉ¸ì(€½¹ÍÐ½±õ‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðí‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐôI•Í•ÑÑ¥¹œ¸¸¸œì(€ÑÉåì(€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½É•Í•Ñ}½±‘}ÍÑ…ÉÐœ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜ …È¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½ÉñðÉ•Í•Ð™…¥±•œ¤ì(€€€Ñ½…ÍÐ ½±µÍÑ…ÉÐ…¡•Ì±•…É•¸I•±½…‘¥¹œ¸¸¸œ°ÄàÀÀ¤ì(€€€Í•ÑQ¥µ•½ÕÐ  ¤ôù±½…Ñ¥½¸¹É•±½… ¤°ÄÄÀÀ¤ì(€õ…Ñ ¡”¥ì(€€€Ñ½…ÍÐ ½Õ±¹½ÐÉ•Í•Ð½±µÍÑ…ÉÐ…¡•Ì¸œ¤í‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±ì(€ô)ô)±•Ð}‘•ÙM•ÅÕ•¹”ôœœì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ­•å‘½Ý¸œ±™Õ¹Ñ¥½¸¡”¥ì(€½¹ÍÐÍ•ÑÑ¥¹Ìõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í•ÑÑ¥¹ÍY¥•Üœ¤ì(€¥˜ …Í•ÑÑ¥¹ÍññÍ•ÑÑ¥¹Ì¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥í}‘•ÙM•ÅÕ•¹”ôœœíÉ•ÑÕÉ¸íô(€¥˜¡”¹­•äôôôœÜœ¥}‘•ÙM•ÅÕ•¹”ô¡}‘•ÙM•ÅÕ•¹”¬œÜœ¤¹Í±¥” ´Ì¤í•±Í”}‘•ÙM•ÅÕ•¹”ôœœì(€¥˜¡}‘•ÙM•ÅÕ•¹”ôôôœÜÜÜœ¥ì(€€€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ‘•ÙM•ÑÑ¥¹Ìœ¤¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€€€}‘•ÙM•ÅÕ•¹”ôœœíÍ•ÑM•ÑÑ¥¹ÍQ…ˆ µ…¥¹Ñ•¹…¹”œ¤íÑ½…ÍÐ •Ù•±½Á•ÈÑ½½±ÌÕ¹±½­•¸œ¤ì(€ô)ô¤ì)…Íå¹Œ™Õ¹Ñ¥½¸Ñ•ÍÑ1½¥¸ ¥ì(€½¹ÍÐµÍœõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í}½¹¹µÍœœ¤íµÍœ¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ Q•ÍÑ¥¹œ¸¸¸œ¤ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½Ñ•ÍÑ}É•‘•¹Ñ¥…±Ìœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íáÑÉ•…µ}¡½ÍÐéÍ}¡½ÍÐ¹Ù…±Õ”±áÑÉ•…µ}ÕÍ•ÈéÍ}ÕÍ•È¹Ù…±Õ”±áÑÉ•…µ}Á…ÍÌéÍ}Á…ÍÌ¹Ù…±Õ•ô¥ô¤ì(€µÍœ¹¥¹¹•É!Q50õÈ¹½¬ü œñÍÁ…¸±…ÍÌô‰½¬ˆøœ­ÑÈ 1½¥¸ÍÕ•ÍÍ™Õ°¸œ¤¬œð½ÍÁ…¸øœ¤è œñÍÁ…¸±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡È¹•ÉÉ½ÉññÑÈ 1½¥¸™…¥±•¸œ¤¤¬œð½ÍÁ…¸øœ¤ì)ô)™Õ¹Ñ¥½¸É•™É•Í¡5•ÍÍ…”¡Ñ•áÐ¥í½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í}É•™É•Í¡µÍœœ¤í¥˜¡•°¥•°¹Ñ•áÑ½¹Ñ•¹ÐõÑ•áÐíô)…Íå¹Œ™Õ¹Ñ¥½¸Ý¥Ñ¡I•™É•Í¡	ÕÑÑ½¸¡‰Ñ¸±±…‰•°±Ý½É¬¥ì(€½¹ÍÐ½±õ‰Ñ¸ý‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðèœœí¥˜¡‰Ñ¸¥í‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ±…‰•°íô(€ÑÉåíÉ•ÑÕÉ¸…Ý…¥ÐÝ½É¬ ¤íõ…Ñ ¡”¥í¥˜¡‰Ñ¸¥í½¹ÍÐµ•ÍÍ…”ôI•™É•Í ™…¥±•è€œ­MÑÉ¥¹œ¡”˜™”¹µ•ÍÍ…•ññ”¤íÉ•™É•Í¡5•ÍÍ…”¡µ•ÍÍ…”¤íÑ½…ÍÐ¡µ•ÍÍ…”°ÜÀÀÀ¤íõÑ¡É½Ü”íõ™¥¹…±±åí¥˜¡‰Ñ¸¥í‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±í…ÁÁ±å1…¹œ ¤íõô)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•™É•Í¡%ÁÑÙ½¹Ñ•¹Ð¡‰Ñ¸±ÅÕ¥•Ð¥ì(€É•ÑÕÉ¸Ý¥Ñ¡I•™É•Í¡	ÕÑÑ½¸¡‰Ñ¸°I•™É•Í¡¥¹œ%AQX¸¸¸œ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥ì(€€€É•™É•Í¡5•ÍÍ…” I•™É•Í¡¥¹œaÑÉ•…´¡…¹¹•±Ì°µ½Ù¥•Ì°Í¡½ÝÌ…¹•Á¥Í½‘•Ì¸¸¸œ¤ì(€€€½¹ÍÐ…Ñ…±½œõ…Ý…¥Ð…Á¤ œ½…Á¤½É•™É•Í¡}áÑÉ•…´œ±íµ•Ñ¡½èA=MPô¤í¥˜¡…Ñ…±½œ¹•ÉÉ½Éñð……Ñ…±½œ¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È¡…Ñ…±½œ¹•ÉÉ½Éñð%AQXÉ•™É•Í ™…¥±•œ¤ì(€€€É•™É•Í¡5•ÍÍ…” %AQX…Ñ…±½Õ•ÌÉ•…‘ä¸UÁ‘…Ñ¥¹œA¸¸¸œ¤ì(€€€½¹ÍÐ•Áœõ…Ý…¥Ð…Á¤ œ½…Á¤½•Áœý™½É”ôÄ™™…Ù½É¥Ñ•ÌôÄœ¤í¥˜¡•Áœ¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡•Áœ¹•ÉÉ½ÉñðAÉ•™É•Í ™…¥±•œ¤ì(€€€}ÑÙÁœõ=‰©•Ð¹…ÍÍ¥¸¡íô±}ÑÙÁœ±•Áœ¹•Áññíô¤í}±…Ñ•ÍÑÁ¥Í½‘•Í1½…‘•õ™…±Í”íÉ•™É•Í¡MÑ…ÑÕÌ ¤ì(€€€¥˜ …µåÑÙY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥É•¹‘•ÉQÙÕ¥‘” ¤ì(€€€½¹ÍÐÍÑ…ÑÌõ•Áœ¹ÍÑ…ÑÍññíô±ÍÕµµ…Éäô%AQXè€œ­…Ñ…±½œ¹¡…¹¹•±Ì¬œ¡…¹¹•±Ì°€œ­…Ñ…±½œ¹µ½Ù¥•Ì¬œµ½Ù¥•Ì°€œ­…Ñ…±½œ¹Í¡½ÝÌ¬œÍ¡½ÝÌƒ
ÜAè€œ¬¡ÍÑ…ÑÌ¹ÕÁ‘…Ñ•‘ñðÀ¤¬œ¡…¹¹•±Ìœì(€€€É•™É•Í¡5•ÍÍ…”¡ÍÕµµ…Éä¤í¥˜ …ÅÕ¥•Ð¥Ñ½…ÍÐ¡ÍÕµµ…Éä°ÜÀÀÀ¤íÉ•ÑÕÉ¸í…Ñ…±½œé…Ñ…±½œ±•Áœé•Áœ±ÍÕµµ…ÉäéÍÕµµ…Éåôì(€ô¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•™É•Í¡=Ñ¡•É½¹Ñ•¹Ð¡‰Ñ¸±ÅÕ¥•Ð¥ì(€É•ÑÕÉ¸Ý¥Ñ¡I•™É•Í¡	ÕÑÑ½¸¡‰Ñ¸°I•™É•Í¡¥¹œ½¹Ñ•¹Ð¸¸¸œ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥ì(€€€½¹ÍÐŒõ…Ý…¥Ð…Á¤ œ½…Á¤½½¹™¥œœ¤±Á…ÉÑÌõmt±™…¥±ÕÉ•ÌõmtíÉ•™É•Í¡5•ÍÍ…” I•™É•Í¡¥¹œÍÁ½ÉÑÌ°É…¥¹œ…¹…µ•Ì¸¸¸œ¤ì(€€€¥˜¡Œ¹˜Å}•¹…‰±•„ôõ™…±Í”¥í½¹ÍÐÉ…¥¹œõ…Ý…¥Ð…Á¤ œ½…Á¤½É•™É•Í¡}É…¥¹œœ±íµ•Ñ¡½èA=MPô¤í¥˜ …É…¥¹œ¹•ÉÉ½È˜™É…¥¹œ¹½¬¥Á…ÉÑÌ¹ÁÕÍ  ¡É…¥¹œ¹Í•É¥•ÍñðÀ¤¬œÉ…¥¹œÍ•É¥•Ìœ¤í•±Í”™…¥±ÕÉ•Ì¹ÁÕÍ  É…¥¹œœ¤íô(€€€¥˜¡Œ¹™½½Ñ‰…±±}•¹…‰±•„ôõ™…±Í”¥í½¹ÍÐÍÁ½ÉÑÌõ…Ý…¥Ð…Á¤ œ½…Á¤½É•™É•Í¡}™½½Ñ‰…±°œ±íµ•Ñ¡½èA=MPô¤í¥˜ …ÍÁ½ÉÑÌ¹•ÉÉ½È˜™ÍÁ½ÉÑÌ¹½¬¥íÁ…ÉÑÌ¹ÁÕÍ  ¡ÍÁ½ÉÑÌ¹µ…Ñ¡•ÍñðÀ¤¬œµ…Ñ¡•Ì°€œ¬¡ÍÁ½ÉÑÌ¹Ñ•…µÍñðÀ¤¬œÑ•…µÌ°€œ¬¡ÍÁ½ÉÑÌ¹Õ¥‘•ÍñðÀ¤¬œQXÕ¥‘•Ìœ¤í¥˜¡ÍÁ½ÉÑÌ¹±¥ÍÑ¥¹}¹½Ñ¥”¥Ñ½…ÍÐ¡ÍÁ½ÉÑÌ¹±¥ÍÑ¥¹}¹½Ñ¥”°ÜÀÀÀ¤íõ•±Í”™…¥±ÕÉ•Ì¹ÁÕÍ  ÍÁ½ÉÑÌœ¤íô(€€€¥˜¡Œ¹…µ•Í}•¹…‰±•„ôõ™…±Í”˜™MÑÉ¥¹œ¡Œ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ±ñðœœ¤¹ÑÉ¥´ ¤¥ì(€€€€€½¹ÍÐÍÑ•…´õ…Ý…¥Ð…Á¤ œ½…Á¤½¥µÁ½ÉÑ}ÍÑ•…µ}Ý¥Í¡±¥ÍÐœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íÕÉ°éŒ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ±ô¥ô¤í¥˜ …ÍÑ•…´¹•ÉÉ½È˜™ÍÑ•…´¹½¬¥Á…ÉÑÌ¹ÁÕÍ  ¡ÍÑ•…´¹¥µÁ½ÉÑ•‘ñðÀ¤¬œMÑ•…´…µ•Ìœ¤í•±Í”™…¥±ÕÉ•Ì¹ÁÕÍ  …µ•Ìœ¤ì(€€€ô(€€€½¹ÍÐÍÕµµ…ÉäôMÁ½ÉÑÌ°É…¥¹œ€˜…µ•Ìè€œ¬¡Á…ÉÑÌ¹±•¹Ñ ýÁ…ÉÑÌ¹©½¥¸ œƒ
Ü€œ¤è¹½Ñ¡¥¹œ•¹…‰±•œ¤¬¡™…¥±ÕÉ•Ì¹±•¹Ñ üœƒ
Ü…¥±•è€œ­™…¥±ÕÉ•Ì¹©½¥¸ œ°€œ¤èœœ¤íÉ•™É•Í¡5•ÍÍ…”¡ÍÕµµ…Éä¤í¥˜ …ÅÕ¥•Ð¥Ñ½…ÍÐ¡ÍÕµµ…Éä°ÜÀÀÀ¤íÉ•ÑÕÉ¸íÍÕµµ…ÉäéÍÕµµ…Éä±™…¥±ÕÉ•Ìé™…¥±ÕÉ•Íôì(€ô¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•™É•Í¡Ù•ÉåÑ¡¥¹œ¡‰Ñ¸±ÅÕ¥•Ð¥ì(€É•ÑÕÉ¸Ý¥Ñ¡I•™É•Í¡	ÕÑÑ½¸¡‰Ñ¸°I•™É•Í¡¥¹œ•Ù•ÉåÑ¡¥¹œ¸¸¸œ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥ì(€€€½¹ÍÐŒõ…Ý…¥Ð…Á¤ œ½…Á¤½½¹™¥œœ¤±Á…ÉÑÌõmt±™…¥±ÕÉ•Ìõmtì(€€€½¹ÍÐÉÕ¸õ…Íå¹Œ™Õ¹Ñ¥½¸¡±…‰•°±Ý½É¬¥íÑÉåíÉ•ÑÕÉ¸…Ý…¥ÐÝ½É¬ ¤íõ…Ñ ¡”¥í™…¥±ÕÉ•Ì¹ÁÕÍ ¡±…‰•°¤íÉ•ÑÕÉ¸¹Õ±°íõôì(€€€½¹ÍÐáÑÉ•…µ½¹™¥ÕÉ•ô„„¡MÑÉ¥¹œ¡Œ¹áÑÉ•…µ}¡½ÍÑñðœœ¤¹ÑÉ¥´ ¤˜™MÑÉ¥¹œ¡Œ¹áÑÉ•…µ}ÕÍ•Éñðœœ¤¹ÑÉ¥´ ¤˜™MÑÉ¥¹œ¡Œ¹áÑÉ•…µ}Á…ÍÍñðœœ¤¹ÑÉ¥´ ¤¤ì(€€€É•™É•Í¡5•ÍÍ…” I•™É•Í¡¥¹œ•¹…‰±•½¹Ñ•¹Ð¸¸¸œ¤ì(€€€¥˜¡áÑÉ•…µ½¹™¥ÕÉ•¥ì(€€€€€½¹ÍÐ…Ñ…±½œõ…Ý…¥ÐÉÕ¸ %AQXœ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥í½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½É•™É•Í¡}áÑÉ•…´œ±íµ•Ñ¡½èA=MPô¤í¥˜¡È¹•ÉÉ½Éñð…È¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½Éñð%AQXÉ•™É•Í ™…¥±•œ¤íÉ•ÑÕÉ¸Èíô¤ì(€€€€€¥˜¡…Ñ…±½œ¥Á…ÉÑÌ¹ÁÕÍ  %AQX€œ­…Ñ…±½œ¹¡…¹¹•±Ì¬œ¡…¹¹•±Ì°€œ­…Ñ…±½œ¹µ½Ù¥•Ì¬œµ½Ù¥•Ì°€œ­…Ñ…±½œ¹Í¡½ÝÌ¬œÍ¡½ÝÌœ¤ì(€€€€€½¹ÍÐÕÉÉ•¹Ñ%ô¡}ÑÙA±…å¥¹œ˜˜¡}ÑÙA±…å¥¹œ¹ÍÑÉ•…µ}¥‘ññ}ÑÙA±…å¥¹œ¹¥‘ññ}ÑÙA±…å¥¹œ¤¥ñðœœì(€€€€€½¹ÍÐ•Áœõ…Ý…¥ÐÉÕ¸ Aœ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥í½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½•Áœý™½É”ôÄ™™…Ù½É¥Ñ•ÌôÄœ¬¡ÕÉÉ•¹Ñ%üœ™¥‘Ìôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡ÕÉÉ•¹Ñ%¤¤èœœ¤¤í¥˜¡È¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½È¤íÉ•ÑÕÉ¸Èíô¤ì(€€€€€¥˜¡•Áœ¥Á…ÉÑÌ¹ÁÕÍ  A€œ¬ ¡•Áœ¹ÍÑ…ÑÌ˜™•Áœ¹ÍÑ…ÑÌ¹ÕÁ‘…Ñ•¥ñðÀ¤¬œ¡…¹¹•±Ìœ¤ì(€€€õ•±Í”Á…ÉÑÌ¹ÁÕÍ  %AQXÍ­¥ÁÁ•œ¤ì(€€€¥˜¡Œ¹˜Å}•¹…‰±•„ôõ™…±Í”¥ì(€€€€€½¹ÍÐÉ…¥¹œõ…Ý…¥ÐÉÕ¸ I…¥¹œœ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥í½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½É•™É•Í¡}É…¥¹œœ±íµ•Ñ¡½èA=MPô¤í¥˜¡È¹•ÉÉ½Éñð…È¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½ÉñðÉ…¥¹œÉ•™É•Í ™…¥±•œ¤íÉ•ÑÕÉ¸Èíô¤ì(€€€€€¥˜¡É…¥¹œ¥Á…ÉÑÌ¹ÁÕÍ  I…¥¹œ€œ­É…¥¹œ¹Í•É¥•Ì¬œÍ•É¥•Ìœ¤ì(€€€ô(€€€¥˜¡Œ¹™½½Ñ‰…±±}•¹…‰±•„ôõ™…±Í”¥ì(€€€€€½¹ÍÐ™½½Ñ‰…±°õ…Ý…¥ÐÉÕ¸ MÁ½ÉÑÌœ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥í½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½É•™É•Í¡}™½½Ñ‰…±°œ±íµ•Ñ¡½èA=MPô¤í¥˜¡È¹•ÉÉ½Éñð…È¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½ÉñðÍÁ½ÉÑÌÉ•™É•Í ™…¥±•œ¤íÉ•ÑÕÉ¸Èíô¤ì(€€€€€¥˜¡™½½Ñ‰…±°¥íÁ…ÉÑÌ¹ÁÕÍ  MÁ½ÉÑÌ€œ­™½½Ñ‰…±°¹Ñ•…µÌ¬œÑ•…µÌ°€œ­™½½Ñ‰…±°¹Õ¥‘•Ì¬œÕ¥‘•Ìœ¤í¥˜¡™½½Ñ‰…±°¹±¥ÍÑ¥¹}¹½Ñ¥”¥Ñ½…ÍÐ¡™½½Ñ‰…±°¹±¥ÍÑ¥¹}¹½Ñ¥”°ÜÀÀÀ¤íô(€€€ô(€€€¥˜¡Œ¹…µ•Í}•¹…‰±•„ôõ™…±Í”˜™MÑÉ¥¹œ¡Œ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ±ñðœœ¤¹ÑÉ¥´ ¤¥ì(€€€€€½¹ÍÐÍÑ•…´õ…Ý…¥ÐÉÕ¸ MÑ•…´œ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥í½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½¥µÁ½ÉÑ}ÍÑ•…µ}Ý¥Í¡±¥ÍÐœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íÕÉ°éŒ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ±ô¥ô¤í¥˜¡È¹•ÉÉ½Éñð…È¹½¬¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½ÉñðMÑ•…´É•™É•Í ™…¥±•œ¤íÉ•ÑÕÉ¸Èíô¤ì(€€€€€¥˜¡ÍÑ•…´¥Á…ÉÑÌ¹ÁÕÍ  MÑ•…´€œ­ÍÑ•…´¹¥µÁ½ÉÑ•¬œ…µ•Ìœ¤ì(€€€ô(€€€½¹ÍÐ•Á¥Í½‘•Ìõ…Ý…¥ÐÉÕ¸ M¡½ÜÍ¡•‘Õ±•Ìœ±…Íå¹Œ™Õ¹Ñ¥½¸ ¥í½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½±…Ñ•ÍÑ}•Á¥Í½‘•ÌýÉ•™É•Í ôÄ™±¥µ¥Ðôäœ¤í¥˜¡È¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½È¤íÉ•ÑÕÉ¸Èíô¤ì(€€€¥˜¡•Á¥Í½‘•Ì¥í}±…Ñ•ÍÑÁ¥Í½‘•Í1½…‘•õ™…±Í”íÁ…ÉÑÌ¹ÁÕÍ  M¡½ÜÍ¡•‘Õ±•ÌÕÁ‘…Ñ•œ¤íô(€€€™½È¡½¹ÍÐ…Ñ•½Éä½˜lÁ½ÁÕ±…Èœ°¹•Üœ°™•…ÑÕÉ•t¥ì(€€€€€½¹ÍÐµ½Ù¥•Ìõ…Ý…¥ÐÉÕ¸ ¥¹•µ•Ñ„€œ­…Ñ•½Éä±…Íå¹Œ™Õ¹Ñ¥½¸ ¥í½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½µ½Ù¥•}…Ñ…±½œý…Ñ…±½œôœ­…Ñ•½Éä¬œ™±¥µ¥ÐôÄÀœ¤í¥˜¡È¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½È¤íÉ•ÑÕÉ¸Èíô¤ì(€€€€€¥˜¡µ½Ù¥•Ì¥}µ½Ù¥•…Ñ…±½…¡•m…Ñ•½Éåtõíµ½Ù¥•Ìéµ½Ù¥•Ì¹µ½Ù¥•Íññmt±±½•‘}¥¸è„…µ½Ù¥•Ì¹±½•‘}¥¹ôì(€€€ô(€€€Á…ÉÑÌ¹ÁÕÍ  ¥¹•µ•Ñ„…¡”¡•­•œ¤ì(€€€É•™É•Í¡MÑ…ÑÕÌ ¤í¥˜ …µåÑÙY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥É•¹‘•ÉQÙÕ¥‘” ¤ì(€€€½¹ÍÐÍÕµµ…ÉäõÁ…ÉÑÌ¹©½¥¸ œƒ
Ü€œ¤¬¡™…¥±ÕÉ•Ì¹±•¹Ñ üœƒ
Ü…¥±•è€œ­™…¥±ÕÉ•Ì¹©½¥¸ œ°€œ¤èœœ¤ì(€€€É•™É•Í¡5•ÍÍ…”¡ÍÕµµ…Éä¤í¥˜ …ÅÕ¥•Ð¥Ñ½…ÍÐ¡™…¥±ÕÉ•Ì¹±•¹Ñ üI•™É•Í ™¥¹¥Í¡•Ý¥Ñ Í½µ”•ÉÉ½ÉÌ¸œèÙ•ÉåÑ¡¥¹œÉ•™É•Í¡•ÍÕ•ÍÍ™Õ±±ä¸œ°ÜÀÀÀ¤íÉ•ÑÕÉ¸íÍÕµµ…ÉäéÍÕµµ…Éä±™…¥±ÕÉ•Ìé™…¥±ÕÉ•Íôì(€ô¤ì)ô)±•Ð}Í•…É¡…Ñ„õ¹Õ±°ì€€€¼¼í™¥áÑÕÉ•Ì°±½•‘}¥¸°ÁÁÙ}…Ñ•½É¥•Íô)±•Ð}Ñ•…µÉ½ÕÁÌõmtì€€€€€€¼¼míÑ•…´°™¥áÑÕÉ•Ìél¸¸¹uõt)±•Ð}…Ñ¥Ù•Q•…´ôÀì()…Íå¹Œ™Õ¹Ñ¥½¸‘½M•…É  ¥ì(€¥˜ …}™½½Ñ‰…±±¹…‰±•¥É•ÑÕÉ¸ì(€½¹ÍÐÄõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Äœ¤¹Ù…±Õ”¹ÑÉ¥´ ¤±É•ÍÕ±Ñ°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É•ÍÕ±ÑÌœ¤ì(€¥˜ …É•ÍÕ±Ñ°¥É•ÑÕÉ¸ì(€É•ÍÕ±Ñ°¹¥¹¹•É!Q50ôœœì(€¥˜ …Ä¥É•ÑÕÉ¸ì(€É•ÍÕ±Ñ°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆùM•…É¡¥¹œ±¥ÍÑ¥¹Ì¸¸¸ð½ÍÁ…¸øœì(€½¹ÍÐÍÑÉ¥Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ…Ñ¡MÑÉ¥Ðœ¤¹Ù…±Õ”ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½Í•…É ýÄôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Ä¤¬œ™ÍÑÉ¥Ñ¹•ÍÌôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡ÍÑÉ¥Ð¤¬¡}™¥áÑÕÉ•M•…É¡Q•…µ%üœ™Ñ•…µ}¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡}™¥áÑÕÉ•M•…É¡Q•…µ%¤èœœ¤¤ì(€¥˜¡È¹•ÉÉ½È¥íÉ•ÍÕ±Ñ°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰•ÉÈˆøœ­È¹•ÉÉ½È¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€}Í•…É¡…Ñ„õÈì(€¥˜¡È¹±½•‘}¥¸¥…Ý…¥ÐÉ•™É•Í¡…ÙMÑ…Ñ” ¤ì(€±•Ð¡•…ôœœì(€¥˜¡È¹Í½ÕÉ•}•ÉÉ½ÉÌ˜™È¹Í½ÕÉ•}•ÉÉ½ÉÌ¹±•¹Ñ ¤(€€€¡•…¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ••ÉÈˆùM½µ”±¥ÍÑ¥¹Ì™…¥±•è€œ­È¹Í½ÕÉ•}•ÉÉ½ÉÌ¹©½¥¸ œì€œ¤¬œð½‘¥Øøœì(€¥˜ …È¹™¥áÑÕÉ•Ì¹±•¹Ñ ¥íÉ•ÍÕ±Ñ°¹¥¹¹•É!Q50õ¡•…¬œñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù9¼ÕÉÉ•¹Ð½ÈÕÁ½µ¥¹œµ…Ñ ™½Õ¹™½È€ˆœ­•ÍŒ¡Ä¤¬œˆ¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€€¼¼É½ÕÀ™¥áÑÕÉ•Ì‰äÑ¡”Ñ•…´Ñ¡…Ðµ…Ñ¡•ÌÑ¡”ÅÕ•Éä€¡¡½µ”½È…Ý…ä¤¸(€}Ñ•…µÉ½ÕÁÌõÉ½ÕÁ	åQ•…´¡È¹™¥áÑÕÉ•Ì±Ä¤ì(€}…Ñ¥Ù•Q•…´ôÀì(€É•ÍÕ±Ñ°¹¥¹¹•É!Q50õ¡•…¬œñ‘¥Ø±…ÍÌô‰µ…Ñ¡É•ÍÕ±ÑÍ±…‰•°ˆøœ­•ÍŒ¡ÑÈ 5…Ñ¡•Ìœ¤¤¬œƒ
Ü€œ­È¹™¥áÑÕÉ•Ì¹±•¹Ñ ¬œð½‘¥Øøñ‘¥Ø¥ô‰Ñ•…µMÝ¥Ñ ˆøð½‘¥Øøñ‘¥Ø¥ô‰Ñ•…µ¥áÑÕÉ•Ìˆøð½‘¥Øøœì(€É•¹‘•ÉQ•…µMÝ¥Ñ  ¤ì(€É•¹‘•ÉÑ¥Ù•Q•…´ ¤ì)ô()™Õ¹Ñ¥½¸É½ÕÁ	åQ•…´¡™¥áÑÕÉ•Ì±Ä¥ì(€€¼¼•Ñ•Éµ¥¹”°Á•È™¥áÑÕÉ”°Ý¡¥ Í¥‘”µ…Ñ¡•Ñ¡”ÅÕ•ÉäìÉ½ÕÀÕ¹‘•ÈÑ¡…ÐÑ•…´¹…µ”¸(€½¹ÍÐÅ°õÄ¹Ñ½1½Ý•É…Í” ¤ì(€½¹ÍÐÉ½ÕÁÌõíôì€€€€€€¼¼Ñ•…µ9…µ”€´øm™¥áÑÕÉ•Ít(€½¹ÍÐ½É‘•Èõmtì(€™½È¡½¹ÍÐ˜½˜™¥áÑÕÉ•Ì¥ì(€€€€¼¼Á¥¬Ñ¡”Ñ•…´Ý¡½Í”¹…µ”½Í±Õœ‰•ÍÐ½¹Ñ…¥¹ÌÑ¡”ÅÕ•Éäì‘•™…Õ±Ð¡½µ”(€€€±•ÐÑ•…´õ˜¹¡½µ”°½Ñ¡•Èõ˜¹…Ý…äì(€€€½¹ÍÐ ô¡˜¹¡½µ•ñðœœ¤¹Ñ½1½Ý•É…Í” ¤°„ô¡˜¹…Ý…åñðœœ¤¹Ñ½1½Ý•É…Í” ¤ì(€€€€¼¼ÉÕ‘”è¥˜…Ý…ä½¹Ñ…¥¹ÌÑ¡”ÅÕ•Éä™É…µ•¹Ð…¹¡½µ”‘½•Í¸Ð°É½ÕÀÕ¹‘•È…Ý…ä(€€€½¹ÍÐ¡!¥Ðõ ¹¥¹±Õ‘•Ì¡Å°¥ññÝ½É‘Í=Ù•É±…À¡Å°± ¤ì(€€€½¹ÍÐ…!¥Ðõ„¹¥¹±Õ‘•Ì¡Å°¥ññÝ½É‘Í=Ù•É±…À¡Å°±„¤ì(€€€¥˜¡…!¥Ð˜˜…¡!¥Ð¥íÑ•…´õ˜¹…Ý…äíô(€€€¥˜ …É½ÕÁÍmÑ•…µt¥íÉ½ÕÁÍmÑ•…µtõmtí½É‘•È¹ÁÕÍ ¡Ñ•…´¤íô(€€€É½ÕÁÍmÑ•…µt¹ÁÕÍ ¡˜¤ì(€ô(€É•ÑÕÉ¸½É‘•È¹µ…À¡™Õ¹Ñ¥½¸¡Ð¥íÉ•ÑÕÉ¸íÑ•…´éÐ±™¥áÑÕÉ•ÌéÉ½ÕÁÍmÑuôíô¤ì)ô)™Õ¹Ñ¥½¸Ý½É‘Í=Ù•É±…À¡Ä±¹…µ”¥ì(€½¹ÍÐÅÌõÄ¹ÍÁ±¥Ð ½qqÌ¬¼¤¹™¥±Ñ•È¡	½½±•…¸¤ì(€É•ÑÕÉ¸ÅÌ¹Í½µ”¡™Õ¹Ñ¥½¸¡Ü¥íÉ•ÑÕÉ¸Ü¹±•¹Ñ øôÌ˜™¹…µ”¹¥¹±Õ‘•Ì¡Ü¤íô¤ì)ô()™Õ¹Ñ¥½¸É•¹‘•ÉQ•…µMÝ¥Ñ  ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µMÝ¥Ñ œ¤ì(€¥˜ …•°¥É•ÑÕÉ¸ì(€¥˜¡}Ñ•…µÉ½ÕÁÌ¹±•¹Ñ ðÈ¥í•°¹¥¹¹•É!Q50ôœœíÉ•ÑÕÉ¸íô€€€¼¼½¹±äÍ¡½ÜÝ¡•¸€È¬Ñ•…µÌ(€±•Ð ôœñ‘¥Ø±…ÍÌô‰Ñ•…µÑ…‰Ìˆøœì(€}Ñ•…µÉ½ÕÁÌ¹™½É… ¡™Õ¹Ñ¥½¸¡œ±¤¥ì(€€€ ¬ôœñ‰ÕÑÑ½¸±…ÍÌô‰Ñ•…µÑ…ˆœ¬¡¤ôôõ}…Ñ¥Ù•Q•…´üœ½¸œèœœ¤¬œˆ‘…Ñ„µÑ•…´ôˆœ­¤¬œˆøœ­•ÍŒ¡œ¹Ñ•…´¤¬œ€ñÍÁ…¸±…ÍÌô‰µÕÑ•ˆø œ­œ¹™¥áÑÕÉ•Ì¹±•¹Ñ ¬œ¤ð½ÍÁ…¸øð½‰ÕÑÑ½¸øœì(€ô¤ì(€ ¬ôœð½‘¥Øøœì(€•°¹¥¹¹•É!Q50õ ì)ô()™Õ¹Ñ¥½¸É•¹‘•ÉÑ¥Ù•Q•…´ ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Ñ•…µ¥áÑÕÉ•Ìœ¤ì(€¥˜ …•°¥É•ÑÕÉ¸ì(€½¹ÍÐœõ}Ñ•…µÉ½ÕÁÍm}…Ñ¥Ù•Q•…µtì(€¥˜ …œ¥í•°¹¥¹¹•É!Q50ôœœíÉ•ÑÕÉ¸íô(€±•Ð¡Ñµ°ôœœì(€½¹ÍÐÉ½ÝÌõœ¹™¥áÑÕÉ•Ì¹Í±¥” ¤¹Í½ÉÐ ¡„±ˆ¤ôù9Õµ‰•È¡™¥áÑÕÉ•5…Ñ¡•Í••Á1¥¹¬¡ˆ¤¤µ9Õµ‰•È¡™¥áÑÕÉ•5…Ñ¡•Í••Á1¥¹¬¡„¤¤¤ì(€É½ÝÌ¹™½É… ¡™Õ¹Ñ¥½¸¡˜±™¤¥ì(€€€¡Ñµ°¬õÉ•¹‘•É¥áÑÕÉ•…É¡˜±™¤¤ì(€ô¤ì(€•°¹¥¹¹•É!Q50õ¡Ñµ°ì)ô()™Õ¹Ñ¥½¸™¥áÑÕÉ•5…Ñ¡•Í••Á1¥¹¬¡˜¥ì(€¥˜ …}Ñ•…µ••Á1¥¹­ñð…˜¥É•ÑÕÉ¸™…±Í”ì(€½¹ÍÐÑ•…µÌõ}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡˜¹¡½µ”±}Ñ•…µ••Á1¥¹¬¹¡½µ”¤˜™}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡˜¹…Ý…ä±}Ñ•…µ••Á1¥¹¬¹…Ý…ä¤ì(€¥˜ …Ñ•…µÌ¥É•ÑÕÉ¸™…±Í”ì(€¥˜ …}Ñ•…µ••Á1¥¹¬¹ÍÑ…ÉÑñð…˜¹ÍÑ…ÉÐ¥É•ÑÕÉ¸ÑÉÕ”ì(€½¹ÍÐ„õ¹•Ü…Ñ”¡˜¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤±ˆõ¹•Ü…Ñ”¡}Ñ•…µ••Á1¥¹¬¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤íÉ•ÑÕÉ¸9Õµ‰•È¹¥Í¥¹¥Ñ”¡„¤˜™9Õµ‰•È¹¥Í¥¹¥Ñ”¡ˆ¤ý5…Ñ ¹…‰Ì¡„µˆ¤ðØ¨ÌØÀÀÀÀÀéÑÉÕ”ì)ô)™Õ¹Ñ¥½¸}Ñ•…µ9…µ•ÍÅÕ¥Ù…±•¹Ñ½ÉU¤¡„±ˆ¥í½¹ÍÐ±•…¸õÌôùMÑÉ¥¹œ¡Íñðœœ¤¹Ñ½1½Ý•É…Í” ¤¹É•Á±…” ½my„µèÀ´ç›ã•t¬½œ°œ€œ¤¹ÑÉ¥´ ¤í½¹ÍÐàõ±•…¸¡„¤±äõ±•…¸¡ˆ¤íÉ•ÑÕÉ¸€„…à˜˜„…ä˜˜¡àôôõåññà¹¥¹±Õ‘•Ì¡ä¥ññä¹¥¹±Õ‘•Ì¡à¤¤íô)™Õ¹Ñ¥½¸™¥áÑÕÉ•¡…¹¹•±I…¹¬¡´±˜¥ì(€½¹ÍÐ±•…¸õÌôùMÑÉ¥¹œ¡Íñðœœ¤¹Ñ½1½Ý•É…Í” ¤¹É•Á±…” ½my„µèÀ´ç›ã•t¬½œ°œ€œ¤¹É•Á±…” ½qqÌ¬½œ°œ€œ¤¹ÑÉ¥´ ¤ì(€½¹ÍÐ¹…µ”õ±•…¸¡´˜™´¹áÑÉ•…µ}¹…µ”¤±¡½µ”õ±•…¸¡˜˜™˜¹¡½µ”¤±…Ý…äõ±•…¸¡˜˜™˜¹…Ý…ä¤ì(€½¹ÍÐ¡½µ•!¥Ðô„…¡½µ”˜™¹…µ”¹¥¹±Õ‘•Ì¡¡½µ”¤±…Ý…å!¥Ðô„……Ý…ä˜™¹…µ”¹¥¹±Õ‘•Ì¡…Ý…ä¤ì(€€¼¼Y¥Í¥‰±”ÑÝ¼µÑ•…´Ñ•áÐ¥Ì‘•™¥¹¥Ñ¥Ù”…¹µÕÍÐ½Ù•ÉÉ¥‘”ÍÑ…±”½µ¥ÍÑ…­•¸(€€¼¼‰…­•¹µ•Ñ…‘…Ñ„‰•™½É”Ñ¡”‰É½…‘…ÍÑ•È±¥ÍÐ¥ÌÍ½ÉÑ•¸(€¥˜¡¡½µ•!¥Ð˜™…Ý…å!¥Ð¥É•ÑÕÉ¸€Ìì(€¥˜¡´˜™´¹™¥áÑÕÉ•}µ…Ñ ôôô•á…Ðœ¥É•ÑÕÉ¸€Ìì(€¥˜¡´˜™´¹™¥áÑÕÉ•}µ…Ñ ôôôÁ…ÉÑ¥…°œ¥É•ÑÕÉ¸€Èì(€¥˜¡´˜™´¹™¥áÑÕÉ•}µ…Ñ ôôô•¹•É¥Œœ¥É•ÑÕÉ¸€Äì(€É•ÑÕÉ¸¡½µ•!¥Ñññ…Ý…å!¥ÐüÈèÄì)ô)™Õ¹Ñ¥½¸ÕÍÑ½µAÉ•µ¥•É1•…Õ•M•ÕÉ”¡ ±˜¥ì(€¥˜ „½qq‰ÁÉ•µ¥•ÉqqÌ­±•…Õ•qqˆ½¤¹Ñ•ÍÐ¡MÑÉ¥¹œ¡˜˜™˜¹±•…Õ•}¹…µ•ñðœœ¤¤¥É•ÑÕÉ¸™…±Í”ì(€½¹ÍÐ¹…µ”õMÑÉ¥¹œ¡ ˜™ ¹áÑÉ•…µ}¹…µ•ñðœœ¤±…Ñ•½ÉäõMÑÉ¥¹œ¡ ˜™ ¹…Ñ•½Éåñðœœ¤ì(€½¹ÍÐ¹½ÉÝ•¥…¸ô¼¡yñmy„µèÀ´åt¤¡¹½ñ¹½Éñ¹½ÉÝ…åñ¹½É•ñ¹½ÉÝ•¥…¸¤¡my„µèÀ´åuð¤½¤¹Ñ•ÍÐ¡…Ñ•½Éä¤ì(€É•ÑÕÉ¸¹½ÉÝ•¥…¸˜˜½qq‰ÙqqÌ©ÍÁ½ÉÑqqÌ¬ üéÁÉ•µ¥•ÉqqÌ­±•…Õ•ñÁÉ•µqqÌ­±•…Õ•ñ•Á±ñÁ°¤ üéqqÌ­qq¬¤ýqqˆ½¤¹Ñ•ÍÐ¡¹…µ”¤ì)ô)™Õ¹Ñ¥½¸¡…¹¹•±1½…±•AÉ¥½É¥Ñä¡ ¥ì(€½¹ÍÐÑ•áÐõm ˜™ ¹…Ñ•½Éä± ˜™ ¹áÑÉ•…µ}¹…µ”± ˜™ ¹ÅÕ…±¥Ñåt¹™¥±Ñ•È¡	½½±•…¸¤¹©½¥¸ œ€œ¤ì(€½¹ÍÐ¥ÌÑ¬ô½qqˆ Ñ­ñÕ¡¥qqˆ½¤¹Ñ•ÍÐ¡Ñ•áÐ¤ì(€½¹ÍÐ¥Í9½ÉÝ•¥…¸ô¼¡yñmy„µèÀ´åt¤¡¹½ñ¹½Éñ¹½ÉÝ…åñ¹½É•ñ¹½ÉÝ•¥…¸¤¡my„µèÀ´åuð¤½¤¹Ñ•ÍÐ¡Ñ•áÐ¤ì(€½¹ÍÐ¥ÍMÝ•‘¥Í ô¼¡yñmy„µèÀ´åt¤¡Í•ñÍÝ•ñÍÝ•‘•¹ñÍÝ•‘¥Í ¤¡my„µèÀ´åuð¤½¤¹Ñ•ÍÐ¡Ñ•áÐ¤ì(€½¹ÍÐ¥Í…¹¥Í ô¼¡yñmy„µèÀ´åt¤¡‘­ñ‘•¹ñ‘¹­ñ‘•¹µ…É­ñ‘…¹¥Í ¤¡my„µèÀ´åuð¤½¤¹Ñ•ÍÐ¡Ñ•áÐ¤ì(€½¹ÍÐ¥Í¥¹¹¥Í ô¼¡yñmy„µèÀ´åt¤¡™¥ñ™¥¹ñ™¥¹±…¹‘ñ™¥¹¹¥Í ¤¡my„µèÀ´åuð¤½¤¹Ñ•ÍÐ¡Ñ•áÐ¤ì(€¥˜¡¥Í9½ÉÝ•¥…¸˜™¥ÌÑ¬¥É•ÑÕÉ¸€ÜÀÀì(€¥˜¡¥Í9½ÉÝ•¥…¸¥É•ÑÕÉ¸€ØÀÀì(€¥˜¡¥ÌÑ¬¥É•ÑÕÉ¸€ÔÀÀì(€¥˜¡¥ÍMÝ•‘¥Í ¥É•ÑÕÉ¸€ÐÀÀì(€¥˜¡¥Í…¹¥Í ¥É•ÑÕÉ¸€ÌäÀì(€¥˜¡¥Í¥¹¹¥Í ¥É•ÑÕÉ¸€ÌàÀì(€É•ÑÕÉ¸€ÌÀÀì)ô)™Õ¹Ñ¥½¸ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¡„±ˆ¥ì(€€¼¼¸•á…Ð¹…µ•±¥¹•…È‰É½…‘…ÍÑ•È¥ÌÑ¡”ÍÑÉ½¹•ÍÐÕÍ•™Õ°Í¥¹…°¥¸Ñ¡¥Ì(€€¼¼±¥ÍÐ¸-••À¥Ð…¡•…½˜•¹•É¥Œ™¥áÑÕÉ”½AAX…¹‘¥‘…Ñ•Ì¥¹Í¥‘”Ñ¡”Í…µ”(€€¼¼±½…±”Ñ¥•È¸A½¹™¥Éµ…Ñ¥½¸¥ÌÑ¡”¹•áÐµ‰•ÍÐÍ¥¹…°¸(€½¹ÍÐÍÕÉ”õ ôù ˜™ ¹ÁÉ½Ù¥‘•É}•á…ÐôôõÑÉÕ”üÌè¡ ˜™ ¹•Á}½¹™¥Éµ•ôôõÑÉÕ”üÈè¡ ˜™ ¹½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ôôõÑÉÕ”üÄèÀ¤¤ì(€É•ÑÕÉ¸¡…¹¹•±1½…±•AÉ¥½É¥Ñä¡ˆ¤µ¡…¹¹•±1½…±•AÉ¥½É¥Ñä¡„¥ññÍÕÉ”¡ˆ¤µÍÕÉ”¡„¥ññ¡…¹¹•±5…Ñ¡AÉ¥½É¥Ñä¡ˆ¤µ¡…¹¹•±5…Ñ¡AÉ¥½É¥Ñä¡„¥ññ9Õµ‰•È¡ˆ¹Í½É•ñðÀ¤µ9Õµ‰•È¡„¹Í½É•ñðÀ¥ññMÑÉ¥¹œ¡„¹áÑÉ•…µ}¹…µ•ñðœœ¤¹±½…±•½µÁ…É”¡MÑÉ¥¹œ¡ˆ¹áÑÉ•…µ}¹…µ•ñðœœ¤¤ì)ô)™Õ¹Ñ¥½¸¡…¹¹•±5…Ñ¡AÉ¥½É¥Ñä¡ ¥í¥˜¡ ˜™ ¹™¥áÑÕÉ•}µ…Ñ ôôô•á…Ðœ¥É•ÑÕÉ¸€ÔÀÀí¥˜¡ ˜™ ¹±•…Õ•}µ…Ñ ôôõÑÉÕ”˜˜½Ù¥…Á±…ä½¤¹Ñ•ÍÐ¡MÑÉ¥¹œ¡ ¹µ…Ñ¡•‘ñðœœ¤¤¥É•ÑÕÉ¸€ÐÀÀí¥˜¡ ˜™ ¹™¥áÑÕÉ•}µ…Ñ ôôôÁ…ÉÑ¥…°œ¥É•ÑÕÉ¸€ÌÀÀí¥˜¡ ˜™ ¹±•…Õ•}µ…Ñ ôôõÑÉÕ”¥É•ÑÕÉ¸€ÈÀÀíÉ•ÑÕÉ¸5…Ñ ¹É½Õ¹¡9Õµ‰•È¡ ˜™ ¹Í½É•ñðÀ¤¨ÄÀÀ¤íô)™Õ¹Ñ¥½¸É½ÕÁ•‘A½ÍÍ¥‰±•¡…¹¹•±Ì¡É½ÝÌ¥ì(€½¹ÍÐÉ½ÕÁÌõ¹•Ü5…À ¤í™½È¡½¹ÍÐ ½˜É½ÝÌ¹Í±¥” ¤¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤¥í½¹ÍÐ…Ñ•½ÉäõMÑÉ¥¹œ¡ ¹…Ñ•½ÉåññÑÈ =Ñ¡•ÈÁ½ÍÍ¥‰±”¡…¹¹•±Ìœ¤¤í¥˜ …É½ÕÁÌ¹¡…Ì¡…Ñ•½Éä¤¥É½ÕÁÌ¹Í•Ð¡…Ñ•½Éä±mt¤íÉ½ÕÁÌ¹•Ð¡…Ñ•½Éä¤¹ÁÕÍ ¡ ¤íô(€½¹ÍÐ…Ñ•½ÉåQ¥•Èõ…Ñ•½Éäôùí½¹ÍÐÑ•áÐõMÑÉ¥¹œ¡…Ñ•½Éåñðœœ¤í¥˜ ¼¡yñmy„µèÀ´åt¤¡¹½ñ¹½Éñ¹½ÉÝ…åñ¹½É•ñ¹½ÉÝ•¥…¸¤¡my„µèÀ´åuð¤½¤¹Ñ•ÍÐ¡Ñ•áÐ¤¥É•ÑÕÉ¸€ÐÀÀí¥˜ ¼¡yñmy„µèÀ´åt¤¡Í•ñÍÝ•ñÍÝ•‘•¹ñÍÝ•‘¥Í ¤¡my„µèÀ´åuð¤½¤¹Ñ•ÍÐ¡Ñ•áÐ¤¥É•ÑÕÉ¸€ÌÀÀí¥˜ ¼¡yñmy„µèÀ´åt¤¡‘­ñ‘•¹ñ‘¹­ñ‘•¹µ…É­ñ‘…¹¥Í ¤¡my„µèÀ´åuð¤½¤¹Ñ•ÍÐ¡Ñ•áÐ¤¥É•ÑÕÉ¸€ÈÀÀí¥˜ ½qqˆ Ñ­ñÕ¡¥qqˆ½¤¹Ñ•ÍÐ¡Ñ•áÐ¤¥É•ÑÕÉ¸€ÄÀÀíÉ•ÑÕÉ¸€Àíôì(€½¹ÍÐ‰•ÍÑ5…Ñ õ¥Ñ•µÌôù5…Ñ ¹µ…à À°¸¸¹¥Ñ•µÌ¹µ…À¡¡…¹¹•±5…Ñ¡AÉ¥½É¥Ñä¤¤ì(€É•ÑÕÉ¸ÉÉ…ä¹™É½´¡É½ÕÁÌ¹•¹ÑÉ¥•Ì ¤¤¹Í½ÉÐ ¡„±ˆ¤ôù…Ñ•½ÉåQ¥•È¡‰lÁt¤µ…Ñ•½ÉåQ¥•È¡…lÁt¥ñð¡…Ñ•½ÉåQ¥•È¡…lÁt¤ôôôÀý‰•ÍÑ5…Ñ ¡‰lÅt¤µ‰•ÍÑ5…Ñ ¡…lÅt¤èÀ¥ññMÑÉ¥¹œ¡…lÁt¤¹±½…±•½µÁ…É”¡MÑÉ¥¹œ¡‰lÁt¤¤¤ì)ô()™Õ¹Ñ¥½¸É•¹‘•É¥áÑÕÉ•…É¡˜±™¤¥ì(€½¹ÍÐÝ¡•¸õ˜¹ÍÑ…ÉÐý¹•Ü…Ñ”¡˜¹ÍÑ…ÉÐ¤¹Ñ½1½…±•MÑÉ¥¹œ¡}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•±íÝ••­‘…äèÍ¡½ÉÐœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐœ±¡½ÕÈèœÈµ‘¥¥Ðœ±µ¥¹ÕÑ”èœÈµ‘¥¥Ðô¤èœœì(€±•Ð‰…‘”ôœœì(€¥˜¡˜¹ÍÑ…ÉÐ¥ì(€€€½¹ÍÐ­¥¬õ¹•Ü…Ñ”¡˜¹ÍÑ…ÉÐ¤ì(€€€½¹ÍÐµ¥¹Ìõ5…Ñ ¹™±½½È ¡…Ñ”¹¹½Ü ¤µ­¥¬¹•ÑQ¥µ” ¤¤¼ØÀÀÀÀ¤ì(€€€½¹ÍÐÍ…µ•…äõ­¥¬¹Ñ½…Ñ•MÑÉ¥¹œ ¤ôôõ¹•Ü…Ñ” ¤¹Ñ½…Ñ•MÑÉ¥¹œ ¤ì(€€€¥˜¡™¥áÑÕÉ•%Í1¥Ù”¡˜¤¥ì(€€€€€½¹ÍÐ¡…Í±½¬õ˜¹±¥Ù•}µ¥¹ÕÑ”„ôõ¹Õ±°˜™˜¹±¥Ù•}µ¥¹ÕÑ”„ôõÕ¹‘•™¥¹•˜™9Õµ‰•È¹¥Í¥¹¥Ñ”¡9Õµ‰•È¡˜¹±¥Ù•}µ¥¹ÕÑ”¤¤ì(€€€€€½¹ÍÐ±¥Ù•5¥¹Ìõ¡…Í±½¬ý9Õµ‰•È¡˜¹±¥Ù•}µ¥¹ÕÑ”¤é5…Ñ ¹µ…à À±µ¥¹Ì¤ì(€€€€€‰…‘”ôœ€ñÍÁ…¸±…ÍÌô‰±¥Ù”ˆùqÔÈÕ1%Y€œ¬¡¡…Í±½¬üœœèøœ¤­±¥Ù•5¥¹Ì¬ˆœð½ÍÁ…¸øˆì(€€€ô(€€€•±Í”¥˜¡™¥áÑÕÉ•%ÍI••¹Ð¡˜¥ñð¡µ¥¹ÌøÄÔÀ˜˜¡µ¥¹ÌðÌØÁññÍ…µ•…ä¤¤¥‰…‘”ôœ€ñÍÁ…¸±…ÍÌô‰•¹‘•ˆù•¹‘•€¼•…É±¥•ÈÑ½‘…äð½ÍÁ…¸øœì(€€€•±Í”¥˜¡µ¥¹ÌðÀ˜™µ¥¹Ìø´ØÀ¥‰…‘”ôœ€ñÍÁ…¸±…ÍÌô‰Í½½¸ˆùÍÑ…ÉÑÌ¥¸€œ¬ µµ¥¹Ì¤¬ˆµ¥¸ð½ÍÁ…¸øˆì(€ô(€½¹ÍÐÉ½ÝÌõmtì(€™½È¡½¹ÍÐŒ½˜=‰©•Ð¹­•åÌ¡˜¹‰å}½Õ¹ÑÉåññíô¤¥™½È¡½¹ÍÐˆ½˜˜¹‰å}½Õ¹ÑÉåmt¥É½ÝÌ¹ÁÕÍ ¡íŒéŒ±‰…ÍÐé‰ô¤ì(€É½ÝÌ¹Í½ÉÐ¡™Õ¹Ñ¥½¸¡à±ä¥í½¹ÍÐáÀõà¹Œ¹Ñ½UÁÁ•É…Í” ¤ôôô9<œüÄèÀ±åÀõä¹Œ¹Ñ½UÁÁ•É…Í” ¤ôôô9<œüÄèÀíÉ•ÑÕÉ¸åÀµáÁñð¡à¹Œôôõä¹Œýà¹‰…ÍÐ¹±½…±•½µÁ…É”¡ä¹‰…ÍÐ¤éà¹Œ¹±½…±•½µÁ…É”¡ä¹Œ¤¤íô¤ì(€€¼¼AÕ±°•Ù•Éä‘•™¥¹¥Ñ”™¥áÑÕÉ”µÑ¥Ñ±”¡¥Ð¥¹Ñ¼½¹”Ù¥Í¥‰±”Í•Ñ¥½¸‰•™½É”(€€¼¼Ñ¡”‰É½…‘•È‰É½…‘…ÍÑ•È½ÁÉ½Ù¥‘•È…Ñ•½É¥•Ì¸-••ÀÑ¡½Í”…Ñ•½É¥•Ì‰É½…¸(€½¹ÍÐÍÑÉ¥ÑM••¸õ¹•ÜM•Ð ¤±ÍÑÉ¥ÑI•ÍÕ±ÑÌõmtì(€l¸¸¸¡˜¹ÁÁÙ}¡¥ÑÍññmt¤¹™¥±Ñ•È¡´ôù™¥áÑÕÉ•¡…¹¹•±I…¹¬¡´±˜¤ôôôÍññ´¹ÁÉ½Ù¥‘•É}•á…ÐôôõÑÉÕ•ññ´¹•Á}½¹™¥Éµ•ôôõÑÉÕ•ññ´¹½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ôôõÑÉÕ•ññÕÍÑ½µAÉ•µ¥•É1•…Õ•M•ÕÉ”¡´±˜¤¤°¸¸¸¡˜¹µ…Ñ¡•Íññmt¤¹™¥±Ñ•È¡´ôù™¥áÑÕÉ•¡…¹¹•±I…¹¬¡´±˜¤ôôôÍññ´¹ÁÉ½Ù¥‘•É}•á…ÐôôõÑÉÕ•ññ´¹•Á}½¹™¥Éµ•ôôõÑÉÕ•ññ´¹½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ôôõÑÉÕ•ññÕÍÑ½µAÉ•µ¥•É1•…Õ•M•ÕÉ”¡´±˜¤¥t¹™½É… ¡™Õ¹Ñ¥½¸¡´¥ì(€€€½¹ÍÐ­•äõMÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥‘ñðœœ¤ì(€€€¥˜¡­•ä˜˜…ÍÑÉ¥ÑM••¸¹¡…Ì¡­•ä¤¥íÍÑÉ¥ÑM••¸¹…‘¡­•ä¤íÍÑÉ¥ÑI•ÍÕ±ÑÌ¹ÁÕÍ ¡´¤íô(€ô¤ì(€ÍÑÉ¥ÑI•ÍÕ±ÑÌ¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤ì(€½¹ÍÐÁ½ÍÍ¥‰±•AÁØõmtì(€™½È¡½¹ÍÐ´½˜€¡˜¹ÁÁÙ}¡¥ÑÍññmt¤¥ì(€€€½¹ÍÐ­•äõMÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥‘ñðœœ¤ì(€€€¥˜¡­•ä˜˜…ÍÑÉ¥ÑM••¸¹¡…Ì¡­•ä¤¥íÍÑÉ¥ÑM••¸¹…‘¡­•ä¤íÁ½ÍÍ¥‰±•AÁØ¹ÁÕÍ ¡´¤íô(€ô(€Á½ÍÍ¥‰±•AÁØ¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤ì(€½¹ÍÐµ…Ñ¡•‘%‘Ìõ¹•ÜM•Ð¡l¸¸¸¡˜¹µ…Ñ¡•Íññmt¤°¸¸¸¡˜¹ÁÁÙ}¡¥ÑÍññmt¥t¹µ…À¡´ôùMÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥‘ñðœœ€¤¤¹™¥±Ñ•È¡	½½±•…¸¤¤ì(€½¹ÍÐ…Ù…¥±Q•áÐõµ…Ñ¡•‘%‘Ì¹Í¥é”ü¡µ…Ñ¡•‘%‘Ì¹Í¥é”¬œ€œ­ÑÈ¡µ…Ñ¡•‘%‘Ì¹Í¥é”ôôôÄü¡…¹¹•°œè¡…¹¹•±Ìœ¤¤è¡É½ÝÌ¹±•¹Ñ ýÑÈ QX±¥ÍÑ•œ¤éÑÈ 9¼QXœ¤¤ì(€½¹ÍÐ…Ù…¥±±…ÍÌô¡µ…Ñ¡•‘%‘Ì¹Í¥é•ññÉ½ÝÌ¹±•¹Ñ ¤üœœèœ¹½¹”œì(€½¹ÍÐ¡½µ•1½¼õ˜¹¡½µ•}¥üœñ¥µœ±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•Ñ•…µ±½¼ˆÍÉŒôˆ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡˜¹¡½µ•}¥¤¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€½¹ÍÐ…Ý…å1½¼õ˜¹…Ý…å}¥üœñ¥µœ±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•Ñ•…µ±½¼ˆÍÉŒôˆ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡˜¹…Ý…å}¥¤¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€±•Ð¡Ñµ°ôœñ‘¥Ø±…ÍÌô‰…Éµ…Ñ¡™¥áÑÕÉ”œ¬¡™¥áÑÕÉ•5…Ñ¡•Í••Á1¥¹¬¡˜¤üœÍ•±•Ñ•‘™¥áÑÕÉ”œèœœ¤¬œˆøñ‘¥Ø±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•¡•…ˆøñ‘¥Ø±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•Ñ•…µÍ±¥¹”ˆøñ‘¥Ø±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•Ñ•…´ˆøœ­¡½µ•1½¼¬œñÍÁ…¸øœ­•ÍŒ¡˜¹¡½µ”¤¬œð½ÍÁ…¸øð½‘¥ØøñÍÁ…¸±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•Ù•ÉÍÕÌˆùØð½ÍÁ…¸øñ‘¥Ø±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•Ñ•…´ˆøœ­…Ý…å1½¼¬œñÍÁ…¸øœ­•ÍŒ¡˜¹…Ý…ä¤¬œð½ÍÁ…¸øð½‘¥Øøð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•µ•Ñ„ˆøñÍÁ…¸øœ­•ÍŒ¡Ý¡•¸¤¬œð½ÍÁ…¸øœ¬¡‰…‘•ñðœœ¤¬œñÍÁ…¸±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•…Ù…¥±…‰¥±¥Ñäœ­…Ù…¥±±…ÍÌ¬œˆøœ­•ÍŒ¡…Ù…¥±Q•áÐ¤¬œð½ÍÁ…¸øð½‘¥Øøð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ…Ñ¡™¥áÑÕÉ•‰½‘äˆøœì(€¥˜ …}Í•…É¡…Ñ„¹±½•‘}¥¸¥ì(€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù1½œ¥¸Ù¥„€ñ„½¹±¥¬ô‰Í¡½ÝM•ÑÑ¥¹Ì ¤ˆÍÑå±”ô‰½±½ÈéÙ…È ´µ…Œ¤íÕÉÍ½ÈéÁ½¥¹Ñ•ÈˆùM•ÑÑ¥¹Ìð½„øÑ¼Í•”Ý¡¥ ½˜å½ÕÈaÑÉ•…´¡…¹¹•±Ìµ…Ñ ¸ð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€€€É•ÑÕÉ¸¡Ñµ°ì(€ô(€€¼¼á…Ð™¥áÑÕÉ”½Ñ•…´•Ù•¹Ð¡¥ÑÌ…É”Ñ¡”ÍÑÉ½¹•ÍÐÉ•ÍÕ±ÑÌ°Í¼Í¡½ÜÑ¡•´(€€¼¼‰•™½É”‰É½…‘•È±¥¹•…Èµ‰É½…‘…ÍÑ•ÈÍÕ•ÍÑ¥½¹Ì¸(€¥˜¡ÍÑÉ¥ÑI•ÍÕ±ÑÌ¹±•¹Ñ ¥ì(€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µ‰½ÑÑ½´èáÁàˆøœ­•ÍŒ¡ÑÈ •™¥¹¥Ñ”¡…¹¹•°µ…Ñ¡•Ìœ¤¤¬œèð½‘¥Øøñ‘¥Ø±…ÍÌô‰‰…ÍÑ±¥ÍÐ‰•ÍÑ•Ù•¹Ñµ…Ñ¡•Ìˆøœì(€€€½¹ÍÐÍÑÉ¥Ñ1¥¹”õ´ôùì(€€€€€½¹ÍÐ™…Øõ}™…Ù¡…¹M•Ð¹¡…Ì¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤üœ½¸œèœœì(€€€€€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰¡±¥¹”ˆøñÍÁ…¸±…ÍÌô‰µ…Ñ¡¡…¸ˆøñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡´¹áÑÉ•…µ}¹…µ”¤¬œˆ‘…Ñ„µ…Ðôˆœ­•ÍÑÑÈ¡´¹…Ñ•½Éåñðœœ¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øœ(€€€€€€€€­¡…¹¹•±1½¼¡´°µ¥¹¤œ¤¬œñÍÁ…¸±…ÍÌô‰¡¸™¥áÑÕÉ•¡…¹¹•±Ñ¥Ñ±”ˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡´¹áÑÉ•…µ}¹…µ”¤¬œˆøœ­•ÍŒ¡´¹áÑÉ•…µ}¹…µ”¤¬¡´¹ÅÕ…±¥ÑäüœñÍÁ…¸±…ÍÌô‰Ñ…œˆøœ­•ÍŒ¡´¹ÅÕ…±¥Ñä¤¬œð½ÍÁ…¸øœèœœ¤¬œð½ÍÁ…¸øð½ÍÁ…¸øœ(€€€€€€€€¬œñÍÁ…¸±…ÍÌô‰¡‰Ñ¹Ìˆøœ­Á±…å‰Ñ¹Ì¡´¹ÍÑÉ•…µ}¥±´¹áÑÉ•…µ}¹…µ”±´¹ÕÉ°¤¬œð½ÍÁ…¸øð½‘¥Øøœì(€€€ôì(€€€¡Ñµ°¬õÍ•ÕÉ•5…Ñ¡É½ÕÁÍ!Ñµ°¡ÍÑÉ¥ÑI•ÍÕ±ÑÌ±ÍÑÉ¥Ñ1¥¹”°™¥áÑÕÉ”œ­™¤¤ì(€€€¡Ñµ°¬ôœð½‘¥Øøœì(€ô(€€¼¼	É½…‘…ÍÑ•ÈÉ½ÝÌ…É”Í½ÉÑ•½Õ¹ÑÉäÑ¡•¸‰É½…‘…ÍÑ•È¸(€¥˜¡É½ÝÌ¹±•¹Ñ ¥ì(€€€½¹ÍÐÕ¹µ…Ñ¡•‘1¥ÍÑ¥¹ÌõÉ½ÝÌ¹™¥±Ñ•È¡É½Üôø„¡˜¹µ…Ñ¡•Íññmt¤¹Í½µ”¡´ôù´¹µ…Ñ¡•ôôõÉ½Ü¹‰…ÍÐ˜˜ …´¹½Õ¹ÑÉåññ´¹½Õ¹ÑÉäôôõÉ½Ü¹Œ¹Ñ½UÁÁ•É…Í” ¤¤¤¤ì(€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰‰…ÍÑ±¥ÍÐˆøœì(€€€É½ÝÌ¹™½É… ¡™Õ¹Ñ¥½¸¡É½Ü±É¤¥ì(€€€€€€¼¼¡…¹¹•±Ìµ…Ñ¡•Ñ¼Ñ¡¥Ì‰É½…‘…ÍÑ•È(€€€€€½¹ÍÐ¡…¹Ìô¡˜¹µ…Ñ¡•Íññmt¤¹™¥±Ñ•È¡™Õ¹Ñ¥½¸¡´¥íÉ•ÑÕÉ¸´¹µ…Ñ¡•ôôõÉ½Ü¹‰…ÍÐ˜˜ …´¹½Õ¹ÑÉåññ´¹½Õ¹ÑÉäôôõÉ½Ü¹Œ¹Ñ½UÁÁ•É…Í” ¤¤íô¤¹Í½ÉÐ¡™Õ¹Ñ¥½¸¡„±ˆ¥í½¹ÍÐÉ…¹¬õ™¥áÑÕÉ•¡…¹¹•±I…¹¬¡ˆ±˜¤µ™¥áÑÕÉ•¡…¹¹•±I…¹¬¡„±˜¤íÉ•ÑÕÉ¸É…¹­ññÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¡„±ˆ¤íô¤ì(€€€€€¥˜ …¡…¹Ì¹±•¹Ñ ¥É•ÑÕÉ¸ì(€€€€€½¹ÍÐÉ¥ô˜œ­™¤¬ˆœ­É¤ì(€€€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰‰É½Üˆ‘…Ñ„µ•áÀôˆœ­É¥¬œˆøœ(€€€€€€€€¬œñ‘¥Ø±…ÍÌô‰‰¡•…ˆøñÍÁ…¸±…ÍÌô‰Œˆøœ­•ÍŒ¡É½Ü¹Œ¤¬œð½ÍÁ…¸ø€ñÍÁ…¸±…ÍÌô‰‰¹…µ”ˆøœ­•ÍŒ¡É½Ü¹‰…ÍÐ¤¬œð½ÍÁ…¸øœ(€€€€€€€€¬œ€ñÍÁ…¸±…ÍÌô‰µÕÑ••áÁ¡¥¹Ðˆøœ¬¡¡…¹Ì¹±•¹Ñ ü¡¡…¹Ì¹±•¹Ñ ¬œ€œ­ÑÈ¡¡…¹Ì¹±•¹Ñ ôôôÄü¡…¹¹•°œè¡…¹¹•±Ìœ¤¤éÑÈ 9¼µ…Ñ¡¥¹œ¡…¹¹•±Ìœ¤¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰‰¡•ÙÉ½¸ˆø˜ŒäØØÈìð½ÍÁ…¸øð½‘¥Øøœ(€€€€€€€€¬œñ‘¥Ø±…ÍÌô‰‰¡…¹Ì¡¥‘”ˆ¥ôˆœ­É¥¬œˆøœì(€€€€€¥˜¡¡…¹Ì¹±•¹Ñ ¥ì(€€€€€€€™½È¡½¹ÍÐm¡…¹¹•±%¹‘•à±µt½˜¡…¹Ì¹•¹ÑÉ¥•Ì ¤¥ì(€€€€€€€€€½¹ÍÐ™…Øõ}™…Ù¡…¹M•Ð¹¡…Ì¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤üœ½¸œèœœì(€€€€€€€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰¡±¥¹”œ¬¡¡…¹¹•±%¹‘•àøôÄÀüœ‰¡…¹•áÑÉ„¡¥‘”œèœœ¤¬œˆøñÍÁ…¸±…ÍÌô‰µ…Ñ¡¡…¸ˆøñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡´¹áÑÉ•…µ}¹…µ”¤¬œˆ‘…Ñ„µ…Ðôˆœ­•ÍÑÑÈ¡´¹…Ñ•½Éåñðœœ¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øœ(€€€€€€€€€€€€­¡…¹¹•±1½¼¡´°µ¥¹¤œ¤¬œñÍÁ…¸±…ÍÌô‰¡¸™¥áÑÕÉ•¡…¹¹•±Ñ¥Ñ±”ˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡´¹áÑÉ•…µ}¹…µ”¤¬œˆøœ­•ÍŒ¡´¹áÑÉ•…µ}¹…µ”¤¬¡™¥áÑÕÉ•¡…¹¹•±I…¹¬¡´±˜¤ôôôÌüœñÍÁ…¸±…ÍÌô‰Ñ…œˆøœ­•ÍŒ¡ÑÈ 	•ÍÐµ…Ñ œ¤¤¬œð½ÍÁ…¸øœèœœ¤¬¡´¹ÅÕ…±¥ÑäüœñÍÁ…¸±…ÍÌô‰Ñ…œˆøœ­•ÍŒ¡´¹ÅÕ…±¥Ñä¤¬œð½ÍÁ…¸øœèœœ¤¬œð½ÍÁ…¸øð½ÍÁ…¸øœ(€€€€€€€€€€€€¬œñÍÁ…¸±…ÍÌô‰¡‰Ñ¹Ìˆøœ­Á±…å‰Ñ¹Ì¡´¹ÍÑÉ•…µ}¥±´¹áÑÉ•…µ}¹…µ”±´¹ÕÉ°¤¬œð½ÍÁ…¸øð½‘¥Øøœì(€€€€€€€ô(€€€€€€€¥˜¡¡…¹Ì¹±•¹Ñ øÄÀ¥¡Ñµ°¬ôœñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐ‰¡…¹•áÁ…¹ˆ½¹±¥¬ô‰Ñ½±•	É½…‘…ÍÑ•É…¹‘¥‘…Ñ•Ì¡Ñ¡¥Ì¤ˆ‘…Ñ„µµ½É”ôˆœ¬¡¡…¹Ì¹±•¹Ñ ´ÄÀ¤¬œˆøœ­•ÍŒ¡ÑÈ M¡½Üµ½É”¡…¹¹•±Ìœ¤¤¬œ€ œ¬¡¡…¹Ì¹±•¹Ñ ´ÄÀ¤¬œ¤ð½‰ÕÑÑ½¸øœì(€€€€€ô(€€€€€¡Ñµ°¬ôœð½‘¥Øøð½‘¥Øøœì(€€€ô¤ì(€€€¥˜¡Õ¹µ…Ñ¡•‘1¥ÍÑ¥¹Ì¹±•¹Ñ ¥ì(€€€€€½¹ÍÐÉ¥ô˜œ­™¤¬Õ¹µ…Ñ¡•œì(€€€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰‰É½Üˆ‘…Ñ„µ•áÀôˆœ­É¥¬œˆøñ‘¥Ø±…ÍÌô‰‰¡•…ˆøñÍÁ…¸±…ÍÌô‰‰¹…µ”ˆøœ­•ÍŒ¡ÑÈ =Ñ¡•È‰É½…‘…ÍÑ•È±¥ÍÑ¥¹Ìœ¤¤¬œð½ÍÁ…¸ø€ñÍÁ…¸±…ÍÌô‰µÕÑ••áÁ¡¥¹Ðˆøœ­Õ¹µ…Ñ¡•‘1¥ÍÑ¥¹Ì¹±•¹Ñ ¬œƒ
Ü€œ­•ÍŒ¡˜¹±¥ÍÑ¥¹}Í½ÕÉ•ñðœœ¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰‰¡•ÙÉ½¸ˆø˜ŒäØØÈìð½ÍÁ…¸øð½‘¥Øøñ‘¥Ø±…ÍÌô‰‰¡…¹Ì¡¥‘”ˆ¥ôˆœ­É¥¬œˆøœì(€€€€€Õ¹µ…Ñ¡•‘1¥ÍÑ¥¹Ì¹™½É… ¡É½Üôùí¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰¡±¥¹”ˆøñÍÁ…¸±…ÍÌô‰µ…Ñ¡¡…¸ˆøñÍÁ…¸±…ÍÌô‰Œˆøœ­•ÍŒ¡É½Ü¹Œ¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰¡¸ˆøœ­•ÍŒ¡É½Ü¹‰…ÍÐ¤¬œð½ÍÁ…¸øð½ÍÁ…¸øð½‘¥Øøœíô¤ì(€€€€€¡Ñµ°¬ôœð½‘¥Øøð½‘¥Øøœì(€€€ô(€€€¡Ñµ°¬ôœð½‘¥Øøœì(€ô(€¥˜¡Á½ÍÍ¥‰±•AÁØ¹±•¹Ñ ¥ì(€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèáÁàˆøœ­•ÍŒ¡ÑÈ A½ÍÍ¥‰±”¡…¹¹•±Ì‰ä…Ñ•½Éäœ¤¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰‰…ÍÑ±¥ÍÐˆøœì(€€€±•ÐÁ½ÍÍ¥‰±•%¹‘•àôÀì(€€€™½È¡½¹ÍÐm…Ñ•½Éä±¥Ñ•µÍt½˜É½ÕÁ•‘A½ÍÍ¥‰±•¡…¹¹•±Ì¡Á½ÍÍ¥‰±•AÁØ¤¥ì(€€€€€½¹ÍÐÉ¥ô˜œ­™¤¬Àœ¬¡Á½ÍÍ¥‰±•%¹‘•à¬¬¤ì(€€€€€¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰‰É½Üˆ‘…Ñ„µ•áÀôˆœ­É¥¬œˆøñ‘¥Ø±…ÍÌô‰‰¡•…ˆøñÍÁ…¸±…ÍÌô‰‰¹…µ”ˆøœ­•ÍŒ¡…Ñ•½Éä¤¬œð½ÍÁ…¸ø€ñÍÁ…¸±…ÍÌô‰µÕÑ••áÁ¡¥¹Ðˆøœ­¥Ñ•µÌ¹±•¹Ñ ¬œ€œ­•ÍŒ¡ÑÈ¡¥Ñ•µÌ¹±•¹Ñ ôôôÄü¡…¹¹•°œè¡…¹¹•±Ìœ¤¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰‰¡•ÙÉ½¸ˆø˜ŒäØØÈìð½ÍÁ…¸øð½‘¥Øøñ‘¥Ø±…ÍÌô‰‰¡…¹Ì¡¥‘”ˆ¥ôˆœ­É¥¬œˆøœì(€€€€€™½È¡½¹ÍÐ´½˜¥Ñ•µÌ¥í½¹ÍÐ™…Øõ}™…Ù¡…¹M•Ð¹¡…Ì¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤üœ½¸œèœœí¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰¡±¥¹”ˆøñÍÁ…¸±…ÍÌô‰µ…Ñ¡¡…¸ˆøñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡´¹áÑÉ•…µ}¹…µ”¤¬œˆ‘…Ñ„µ…Ðôˆœ­•ÍÑÑÈ¡´¹…Ñ•½Éåñðœœ¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øœ­¡…¹¹•±1½¼¡´°µ¥¹¤œ¤¬œñÍÁ…¸±…ÍÌô‰¡¸™¥áÑÕÉ•¡…¹¹•±Ñ¥Ñ±”ˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡´¹áÑÉ•…µ}¹…µ”¤¬œˆøœ­•ÍŒ¡´¹áÑÉ•…µ}¹…µ”¤¬¡´¹ÅÕ…±¥ÑäüœñÍÁ…¸±…ÍÌô‰Ñ…œˆøœ­•ÍŒ¡´¹ÅÕ…±¥Ñä¤¬œð½ÍÁ…¸øœèœœ¤¬œð½ÍÁ…¸øð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰¡‰Ñ¹Ìˆøœ­Á±…å‰Ñ¹Ì¡´¹ÍÑÉ•…µ}¥±´¹áÑÉ•…µ}¹…µ”±´¹ÕÉ°¤¬œð½ÍÁ…¸øð½‘¥Øøœíô(€€€€€¡Ñµ°¬ôœð½‘¥Øøð½‘¥Øøœì(€€€ô(€€€¡Ñµ°¬ôœð½‘¥Øøœì(€ô(€¥˜ …É½ÝÌ¹±•¹Ñ ˜˜…ÍÑÉ¥ÑI•ÍÕ±ÑÌ¹±•¹Ñ ˜˜…Á½ÍÍ¥‰±•AÁØ¹±•¹Ñ ¥ì(€€€¥˜ …=‰©•Ð¹­•åÌ¡˜¹‰å}½Õ¹ÑÉåññíô¤¹±•¹Ñ ¥¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù9¼QX¡…¹¹•±Ì™½Õ¹¸ð½‘¥Øøœì(€€€•±Í”¡Ñµ°¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù9¼aÑÉ•…´¡…¹¹•±Ìµ…Ñ¡•¸QÉä±½Ý•É¥¹œÍÑÉ¥Ñ¹•ÍÌ¸ð½‘¥Øøœì(€ô(€¡Ñµ°¬ôœð½‘¥Øøð½‘¥Øøœì(€É•ÑÕÉ¸¡Ñµ°ì)ô)™Õ¹Ñ¥½¸•ÍŒ¡Ì¥íÉ•ÑÕÉ¸MÑÉ¥¹œ¡Ìôõ¹Õ±°üœœéÌ¤¹É•Á±…” ¼˜½œ°œ™…µÀìœ¤¹É•Á±…” ¼ð½œ°œ™±Ðìœ¤¹É•Á±…” ¼ø½œ°œ™Ðìœ¤íô)™Õ¹Ñ¥½¸Ñ½±•AÁØ¡‰Ñ¸¥ì(€½¹ÍÐ±¥ÍÐõ‰Ñ¸¹ÁÉ•Ù¥½ÕÍ±•µ•¹ÑM¥‰±¥¹œ°•áÑÉ…Ìõ±¥ÍÐý±¥ÍÐ¹ÅÕ•ÉåM•±•Ñ½É±° œ¹ÁÁÙ•áÑÉ„œ¤émtì(€¥˜ …•áÑÉ…Ì¹±•¹Ñ ¥É•ÑÕÉ¸ì(€½¹ÍÐ½Á•¹¥¹œõ•áÑÉ…ÍlÁt¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤ì(€•áÑÉ…Ì¹™½É… ¡•°ôù•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…½Á•¹¥¹œ¤¤ì(€‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½Á•¹¥¹œüM¡½Ü±•ÍÌœèM¡½Ü€œ­‰Ñ¸¹‘…Ñ…Í•Ð¹µ½É”¬œµ½É”œì)ô)™Õ¹Ñ¥½¸•ÍÑÑÈ¡Ì¥íÉ•ÑÕÉ¸•ÍŒ¡Ì¤¹É•Á±…” ¼ˆ½œ°œ™ÅÕ½Ðìœ¤¹É•Á±…” ¼œ½œ°œ˜ŒÌäìœ¤íô)™Õ¹Ñ¥½¸¡…¹¹•±1½¼¡Œ±•áÑÉ„¥ì(€¥˜ …ñð…Œ¹±½½ññŒ¹ÍÑÉ•…µ}¥ôôõÕ¹‘•™¥¹•‘ññŒ¹ÍÑÉ•…µ}¥ôôõ¹Õ±°¥É•ÑÕÉ¸€œœì(€É•ÑÕÉ¸€œñ¥µœ±…ÍÌô‰¡…¹±½¼œ¬¡•áÑÉ„üœ€œ­•áÑÉ„èœœ¤¬œˆÍÉŒôˆ½…Á¤½¡…¹¹•±}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœì)ô)™Õ¹Ñ¥½¸Á±…å‰Ñ¹Ì¡Í¥±¹…µ”±ÕÉ°±Í¡½Ý½Áä¥ì(€½¹ÍÐÌõ•ÍÑÑÈ¡MÑÉ¥¹œ¡Í¥¤¤°¸õ•ÍÑÑÈ¡¹…µ•ñðœœ¤°Ôõ•ÍÑÑÈ¡ÕÉ°¤ì(€É•ÑÕÉ¸€œñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Á±…äˆ‘…Ñ„µÍ¥ôˆœ­Ì¬œˆ‘…Ñ„µ¹…µ”ôˆœ­¸¬œˆø˜ŒäØÔàìA±…äð½‰ÕÑÑ½¸øœ(€€€€¬œñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Ù±Œˆ‘…Ñ„µÍ¥ôˆœ­Ì¬œˆø˜ŒäØÔàìY1ð½‰ÕÑÑ½¸øœ(€€€€¬¡Í¡½Ý½Áäüœñ‰ÕÑÑ½¸±…ÍÌô‰½Áäˆ‘…Ñ„µÕÉ°ôˆœ­Ô¬œˆøœ­ÑÈ ½ÁäUI0œ¤¬œð½‰ÕÑÑ½¸øœèœœ¤ì)ô)±•Ð}¡±Ìõ¹Õ±°±}µÁ•ÑÌõ¹Õ±°ì)™Õ¹Ñ¥½¸‘•ÍÑÉ½å5Á•ÑÍA±…å•È¡À¥ì(€¥˜ …À¥É•ÑÕÉ¸ì(€ÑÉåíÀ¹Á…ÕÍ” ¤íõ…Ñ ¡”¥íõÑÉåíÀ¹Õ¹±½… ¤íõ…Ñ ¡”¥íõÑÉåíÀ¹‘•Ñ…¡5•‘¥…±•µ•¹Ð ¤íõ…Ñ ¡”¥íõÑÉåíÀ¹‘•ÍÑÉ½ä ¤íõ…Ñ ¡”¥íô)ô)™Õ¹Ñ¥½¸ÍÑ…ÉÑMµ…ÉÑMÑÉ•…´¡Ù¥‘•¼±ÕÉ±Ì±Í•ÑMÑ…ÑÕÌ±Í•Ñ¹¥¹”¥ì(€±•ÐÍÑ½ÁÁ•õ™…±Í”±¡±Ìõ¹Õ±°±ÑÍA±…å•Èõ¹Õ±°±µ•‘¥…I•½Ù•É¥•ÌôÀì(€™Õ¹Ñ¥½¸ÍÑ…ÑÕÌ¡Ì¥í¥˜ …ÍÑ½ÁÁ•˜™Í•ÑMÑ…ÑÕÌ¥Í•ÑMÑ…ÑÕÌ¡Íñðœœ¤íô(€™Õ¹Ñ¥½¸±•…È ¥í¥˜¡¡±Ì¥íÑÉåí¡±Ì¹‘•ÍÑÉ½ä ¤íõ…Ñ ¡”¥íõ¡±Ìõ¹Õ±°íõ¥˜¡ÑÍA±…å•È¥í‘•ÍÑÉ½å5Á•ÑÍA±…å•È¡ÑÍA±…å•È¤íÑÍA±…å•Èõ¹Õ±°íõô(€™Õ¹Ñ¥½¸ÁÕ‰±¥Í  ¥í¥˜¡Í•Ñ¹¥¹”¥Í•Ñ¹¥¹”¡¡±Ì±ÑÍA±…å•È¤íô(€™Õ¹Ñ¥½¸ÍÑ…ÉÑQÌ¡É•…Í½¸¥ì(€€€±•…È ¤íÁÕ‰±¥Í  ¤ì(€€€¥˜¡ÍÑ½ÁÁ•¥É•ÑÕÉ¸ì(€€€¥˜ „¡Ý¥¹‘½Ü¹µÁ•ÑÌ˜™µÁ•ÑÌ¹¥ÍMÕÁÁ½ÉÑ•˜™µÁ•ÑÌ¹¥ÍMÕÁÁ½ÉÑ• ¤¥ñð…ÕÉ±Ì¹ÑÌ¥íÍÑ…ÑÕÌ ½Õ±¹½ÐÁ±…äÑ¡¥ÌÍÑÉ•…´¥¸Ñ¡”‰É½ÝÍ•È¸QÉäY1¸œ¤íÉ•ÑÕÉ¸íô(€€€ÍÑ…ÑÕÌ¡É•…Í½¸ü!1LÕ¹…Ù…¥±…‰±”ƒŠPÑÉå¥¹œ5AµQL¸¸¸œèQÉå¥¹œ5AµQL¸¸¸œ¤ì(€€€ÑÉåì(€€€€€ÑÍA±…å•ÈõµÁ•ÑÌ¹É•…Ñ•A±…å•È¡íÑåÁ”èµÁ•ÑÌœ±¥Í1¥Ù”éÑÉÕ”±ÕÉ°èœ½…Á¤½ÁÉ½áäýÔôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡ÕÉ±Ì¹ÑÌ¥ô°(€€€€€€€í•¹…‰±•]½É­•ÈéÑÉÕ”±±…éå1½…é™…±Í”±…ÕÑ½±•…¹ÕÁM½ÕÉ•	Õ™™•ÈéÑÉÕ”±…ÕÑ½±•…¹ÕÁ5…á	…­Ý…É‘ÕÉ…Ñ¥½¸èØÀ±…ÕÑ½±•…¹ÕÁ5¥¹	…­Ý…É‘ÕÉ…Ñ¥½¸èÌÁô¤ì(€€€€€ÑÍA±…å•È¹…ÑÑ…¡5•‘¥…±•µ•¹Ð¡Ù¥‘•¼¤íÁÕ‰±¥Í  ¤ì(€€€€€±•Ð™…¥±•õ™…±Í”ì(€€€€€ÑÍA±…å•È¹½¸¡µÁ•ÑÌ¹Ù•¹ÑÌ¹II=H±™Õ¹Ñ¥½¸ ¥í¥˜¡™…¥±•‘ññÍÑ½ÁÁ•¥É•ÑÕÉ¸í™…¥±•õÑÉÕ”íÍÑ…ÑÕÌ ½Õ±¹½ÐÁ±…äÑ¡¥ÌÍÑÉ•…´¥¸Ñ¡”‰É½ÝÍ•È¸QÉäY1¸œ¤íô¤ì(€€€€€ÑÍA±…å•È¹±½… ¤ì(€€€€€½¹ÍÐÁ±…å•õÑÍA±…å•È¹Á±…ä ¤í¥˜¡Á±…å•˜™Á±…å•¹…Ñ ¥Á±…å•¹…Ñ   ¤ôùíô¤ì(€€€€€Ù¥‘•¼¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È Á±…å¥¹œœ±™Õ¹Ñ¥½¸½¹QÍA±…å¥¹œ ¥íÙ¥‘•¼¹É•µ½Ù•Ù•¹Ñ1¥ÍÑ•¹•È Á±…å¥¹œœ±½¹QÍA±…å¥¹œ¤íÍÑ…ÑÕÌ œœ¤íô±í½¹”éÑÉÕ•ô¤ì(€€€õ…Ñ ¡”¥íÍÑ…ÑÕÌ ½Õ±¹½ÐÁ±…äÑ¡¥ÌÍÑÉ•…´¥¸Ñ¡”‰É½ÝÍ•È¸QÉäY1¸œ¤íô(€ô(€™Õ¹Ñ¥½¸ÍÑ…ÉÑ!±Ì¡ÍÉŒ±Ù¥…AÉ½áä¥ì(€€€±•…È ¤íÁÕ‰±¥Í  ¤í¥˜¡ÍÑ½ÁÁ•¥É•ÑÕÉ¸ì(€€€¥˜¡Ý¥¹‘½Ü¹!±Ì˜™!±Ì¹¥ÍMÕÁÁ½ÉÑ• ¤¥ì(€€€€€ÍÑ…ÑÕÌ¡Ù¥…AÉ½áäüI½ÕÑ¥¹œ!1LÑ¡É½Õ ±½…°É•±…ä¸¸¸œè1½…‘¥¹œ!1L¸¸¸œ¤ì(€€€€€¡±Ìõ¹•Ü!±Ì¡íµ…¹¥™•ÍÑ1½…‘¥¹Q¥µ•=ÕÐèÄÈÀÀÀ±±•Ù•±1½…‘¥¹Q¥µ•=ÕÐèÄÈÀÀÀ±™É…1½…‘¥¹Q¥µ•=ÕÐèÈÀÀÀÀ±‰…­	Õ™™•É1•¹Ñ èÌÀ±µ…á	Õ™™•É1•¹Ñ èÐÕô¤íÁÕ‰±¥Í  ¤ì(€€€€€¡±Ì¹½¸¡!±Ì¹Ù•¹ÑÌ¹II=H±™Õ¹Ñ¥½¸¡•Ø±‘…Ñ„¥ì(€€€€€€€¥˜¡ÍÑ½ÁÁ•‘ñð…‘…Ñ„¹™…Ñ…°¥É•ÑÕÉ¸ì(€€€€€€€¥˜¡‘…Ñ„¹ÑåÁ”ôôõ!±Ì¹ÉÉ½ÉQåÁ•Ì¹5%}II=H˜™µ•‘¥…I•½Ù•É¥•ÌðÈ¥íµ•‘¥…I•½Ù•É¥•Ì¬¬íÍÑ…ÑÕÌ I•½Ù•É¥¹œ‰É½ÝÍ•ÈÁ±…å‰…¬¸¸¸œ¤íÑÉåí¡±Ì¹É•½Ù•É5•‘¥…ÉÉ½È ¤íÉ•ÑÕÉ¸íõ…Ñ ¡”¥íõô(€€€€€€€¥˜ …Ù¥…AÉ½áä¥íÍÑ…ÉÑ!±Ì œ½…Á¤½ÁÉ½áäýÔôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡ÕÉ±Ì¹¡±Ì¤±ÑÉÕ”¤íÉ•ÑÕÉ¸íô(€€€€€€€ÍÑ…ÉÑQÌ¡ÑÉÕ”¤ì(€€€€€ô¤ì(€€€€€¡±Ì¹½¸¡!±Ì¹Ù•¹ÑÌ¹59%MQ}AIM±™Õ¹Ñ¥½¸ ¥íÍÑ…ÑÕÌ œœ¤í½¹ÍÐÀõÙ¥‘•¼¹Á±…ä ¤í¥˜¡À˜™À¹…Ñ ¥À¹…Ñ   ¤ôùíô¤íô¤ì(€€€€€¡±Ì¹±½…‘M½ÕÉ”¡ÍÉŒ¤í¡±Ì¹…ÑÑ…¡5•‘¥„¡Ù¥‘•¼¤íÉ•ÑÕÉ¸ì(€€€ô(€€€¥˜¡Ù¥‘•¼¹…¹A±…åQåÁ” …ÁÁ±¥…Ñ¥½¸½Ù¹¹…ÁÁ±”¹µÁ•ÕÉ°œ¤¥ì(€€€€€ÍÑ…ÑÕÌ 1½…‘¥¹œ!1L¸¸¸œ¤íÙ¥‘•¼¹ÍÉŒõÍÉŒì(€€€€€½¹ÍÐ¹…Ñ¥Ù•ÉÉ½Èõ™Õ¹Ñ¥½¸ ¥íÙ¥‘•¼¹É•µ½Ù•Ù•¹Ñ1¥ÍÑ•¹•È •ÉÉ½Èœ±¹…Ñ¥Ù•ÉÉ½È¤í¥˜ …Ù¥…AÉ½áä¥ÍÑ…ÉÑ!±Ì œ½…Á¤½ÁÉ½áäýÔôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡ÕÉ±Ì¹¡±Ì¤±ÑÉÕ”¤í•±Í”ÍÑ…ÉÑQÌ¡ÑÉÕ”¤íôì(€€€€€Ù¥‘•¼¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È •ÉÉ½Èœ±¹…Ñ¥Ù•ÉÉ½È±í½¹”éÑÉÕ•ô¤ì(€€€€€½¹ÍÐÀõÙ¥‘•¼¹Á±…ä ¤í¥˜¡À˜™À¹…Ñ ¥À¹…Ñ   ¤ôùíô¤íÉ•ÑÕÉ¸ì(€€€ô(€€€ÍÑ…ÉÑQÌ¡ÑÉÕ”¤ì(€ô(€ÍÑ…ÉÑ!±Ì¡ÕÉ±Ì¹¡±Ì±™…±Í”¤ì(€É•ÑÕÉ¸íÍÑ½Àé™Õ¹Ñ¥½¸ ¥íÍÑ½ÁÁ•õÑÉÕ”í±•…È ¤íÁÕ‰±¥Í  ¤íÑÉåíÙ¥‘•¼¹Á…ÕÍ” ¤íÙ¥‘•¼¹É•µ½Ù•ÑÑÉ¥‰ÕÑ” ÍÉŒœ¤íÙ¥‘•¼¹±½… ¤íõ…Ñ ¡”¥íõõôì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Á±…å	É½ÝÍ•È¡Í¥±¹…µ”¥ì(€½¹ÍÐµ½‘…°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á±…å•É5½‘…°œ¤ì(€½¹ÍÐÙ¥‘•¼õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÁY¥‘•¼œ¤ì(€½¹ÍÐµÍœõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á5Íœœ¤ì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÁQ¥Ñ±”œ¤¹Ñ•áÑ½¹Ñ•¹Ðõ¹…µ•ñðA±…å•Èœì(€µÍœ¹Ñ•áÑ½¹Ñ•¹Ðô1½…‘¥¹œ¸¸¸œì(€µ½‘…°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€Í•ÑA½ÁÕÁA±…å•É5…à¡™…±Í”¤ì(€‘½Õµ•¹Ð¹‰½‘ä¹±…ÍÍ1¥ÍÐ¹…‘ ÑÙÍ•Ñ¥½¹Á±…äœ¤ì(€Íå¹M•Ñ¥½¹A±…å•É1…å½ÕÐ ¤ì(€€¼¼•ÐÑ¡”¡±ÌÕÉ°(€¥˜¡µ½‘…°¹}Á±…å‰…­½¹ÑÉ½±±•È¥íµ½‘…°¹}Á±…å‰…­½¹ÑÉ½±±•È¹ÍÑ½À ¤íµ½‘…°¹}Á±…å‰…­½¹ÑÉ½±±•Èõ¹Õ±°íô(€¥˜¡}¡±Ì¥íÑÉåí}¡±Ì¹‘•ÍÑÉ½ä ¤íõ…Ñ ¡”¥íõ}¡±Ìõ¹Õ±°íõ¥˜¡}µÁ•ÑÌ¥í‘•ÍÑÉ½å5Á•ÑÍA±…å•È¡}µÁ•ÑÌ¤í}µÁ•ÑÌõ¹Õ±°íô(€±•ÐÕÉ±Ìì(€ÑÉåíÕÉ±Ìõ…Ý…¥Ð…Á¤ œ½…Á¤½¡±Ìý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Í¥¤¤í¥˜¡ÕÉ±Ì¹•ÉÉ½Éñð…ÕÉ±Ì¹¡±Ì¥Ñ¡É½Ü¹•ÜÉÉ½È ÍÑÉ•…´ÕÉ°œ¤íõ…Ñ ¡”¥íµÍœ¹Ñ•áÑ½¹Ñ•¹Ðô½Õ±¹½Ð‰Õ¥±ÍÑÉ•…´UI0¸œíÉ•ÑÕÉ¸íô(€½¹ÍÐ½¹ÑÉ½±±•ÈõÍÑ…ÉÑMµ…ÉÑMÑÉ•…´¡Ù¥‘•¼±ÕÉ±Ì±ÌôùµÍœ¹Ñ•áÑ½¹Ñ•¹ÐõÌ±™Õ¹Ñ¥½¸¡ ±Ð¥í}¡±Ìõ í}µÁ•ÑÌõÐíô¤ì(€µ½‘…°¹}Á±…å‰…­½¹ÑÉ½±±•Èõ½¹ÑÉ½±±•Èì)ô)™Õ¹Ñ¥½¸Á±…å•ÉÕ±±ÍÉ••¹±•µ•¹Ð ¥ì(€É•ÑÕÉ¸‘½Õµ•¹Ð¹™Õ±±ÍÉ••¹±•µ•¹Ñññ‘½Õµ•¹Ð¹Ý•‰­¥ÑÕ±±ÍÉ••¹±•µ•¹Ñññ¹Õ±°ì)ô)™Õ¹Ñ¥½¸Ñ½±•	É½…‘…ÍÑ•É…¹‘¥‘…Ñ•Ì¡‰Ñ¸¥ì(€½¹ÍÐ‰½àõ‰Ñ¸¹Á…É•¹Ñ±•µ•¹Ð±•áÑÉ…Ìõ‰½àý‰½à¹ÅÕ•ÉåM•±•Ñ½É±° œ¹‰¡…¹•áÑÉ„œ¤émtì(€¥˜ …•áÑÉ…Ì¹±•¹Ñ ¥É•ÑÕÉ¸ì(€½¹ÍÐ½Á•¹¥¹œõ•áÑÉ…ÍlÁt¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤ì(€•áÑÉ…Ì¹™½É… ¡•°ôù•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…½Á•¹¥¹œ¤¤ì(€‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½Á•¹¥¹œýÑÈ M¡½Ü™•Ý•È¡…¹¹•±Ìœ¤è¡ÑÈ M¡½Üµ½É”¡…¹¹•±Ìœ¤¬œ€ œ­‰Ñ¸¹‘…Ñ…Í•Ð¹µ½É”¬œ¤œ¤ì)ô)™Õ¹Ñ¥½¸Íå¹M•Ñ¥½¹A±…å•É1…å½ÕÐ ¥ì(€€¼¼½É”Ñ¡”…±É•…‘äµ½Á•¸Í•Ñ¥½¸Ñ¼…‘½ÁÐ¥ÑÌ½¹ÍÑÉ…¥¹•Á±…å•È±…å½ÕÐ½¸(€€¼¼Ñ¡”™¥ÉÍÐ™É…µ”¸AÉ•Ù¥½ÕÍ±äÑ¡¥Ì¡…ÁÁ•¹•É•±¥…‰±ä½¹±ä…™Ñ•È¹…Ù¥…Ñ¥¹œ(€€¼¼…Ý…ä…¹‰…¬°•ÍÁ•¥…±±ä…Ð€ÄäÈÁàÄÀàÀ¸(€Ù½¥‘½Õµ•¹Ð¹‰½‘ä¹½™™Í•Ñ]¥‘Ñ ì(€É•ÅÕ•ÍÑ¹¥µ…Ñ¥½¹É…µ”  ¤ôùÉ•ÅÕ•ÍÑ¹¥µ…Ñ¥½¹É…µ”  ¤ôùì(€€€¥˜ …É…¥¹Y¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥ì(€€€€€½¹ÍÐ‘É¥Ù•ÉÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹É¥Ù•ÉÌœ¤ì(€€€€€¥˜¡‘É¥Ù•ÉÌ¥‘É¥Ù•ÉÌ¹¥¹¹•É!Q50õÉ…¥¹É¥Ù•ÉÍ!Ñµ°¡}É…¥¹É¥Ù•ÉI½ÝÌ±}É…¥¹Ù•¹ÑI½ÝÌ¤ì(€€€€€É•¹‘•ÉI…¥¹M¡•‘Õ±•…É‘Ì ¤ì(€€€ô(€€€Ý¥¹‘½Ü¹‘¥ÍÁ…Ñ¡Ù•¹Ð¡¹•ÜÙ•¹Ð É•Í¥é”œ¤¤ì(€ô¤¤ì)ô)™Õ¹Ñ¥½¸É•ÅÕ•ÍÑA±…å•ÉÕ±±ÍÉ••¸¡•°¥ì(€¥˜ …•°¥É•ÑÕÉ¸™…±Í”ì(€½¹ÍÐ™¸õ•°¹É•ÅÕ•ÍÑÕ±±ÍÉ••¹ññ•°¹Ý•‰­¥ÑI•ÅÕ•ÍÑÕ±±ÍÉ••¸ì(€¥˜ …™¸¥É•ÑÕÉ¸™…±Í”ì(€ÑÉåí½¹ÍÐÀõ™¸¹…±°¡•°¤í¥˜¡À˜™À¹…Ñ ¥À¹…Ñ   ¤ôùíô¤íÉ•ÑÕÉ¸ÑÉÕ”íõ…Ñ ¡”¥íÉ•ÑÕÉ¸™…±Í”íô)ô)™Õ¹Ñ¥½¸•á¥ÑA±…å•ÉÕ±±ÍÉ••¸ ¥ì(€½¹ÍÐ™¸õ‘½Õµ•¹Ð¹•á¥ÑÕ±±ÍÉ••¹ññ‘½Õµ•¹Ð¹Ý•‰­¥Ñá¥ÑÕ±±ÍÉ••¸ì(€¥˜ …™¸¥É•ÑÕÉ¸™…±Í”ì(€ÑÉåí½¹ÍÐÀõ™¸¹…±°¡‘½Õµ•¹Ð¤í¥˜¡À˜™À¹…Ñ ¥À¹…Ñ   ¤ôùíô¤íÉ•ÑÕÉ¸ÑÉÕ”íõ…Ñ ¡”¥íÉ•ÑÕÉ¸™…±Í”íô)ô)™Õ¹Ñ¥½¸Í•ÑA½ÁÕÁA±…å•É5…à¡µ…á¥µ¥é•¥ì(€½¹ÍÐµ½‘…°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á±…å•É5½‘…°œ¤±‰Ñ¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á5¥¹	Ñ¸œ¤±¡¥Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÁY¥‘•½!¥Ðœ¤ì(€µ½‘…°¹±…ÍÍ1¥ÍÐ¹Ñ½±” Í•Ñ¥½¹µ…àœ°„…µ…á¥µ¥é•¤ì(€½¹ÍÐ±…‰•°õµ…á¥µ¥é•üá¥Ð™Õ±±ÍÉ••¸œèÕ±±ÍÉ••¸Á±…å•Èœì(€¥˜¡‰Ñ¸¥í‰Ñ¸¹Ñ¥Ñ±”õ±…‰•°í‰Ñ¸¹Í•ÑÑÑÉ¥‰ÕÑ” …É¥„µ±…‰•°œ±±…‰•°¤í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõµ…á¥µ¥é•üqÔÈÄäàœèqÔÈÄäØœíô(€¥˜¡¡¥Ð¥¡¥Ð¹Í•ÑÑÑÉ¥‰ÕÑ” …É¥„µ±…‰•°œ±±…‰•°¤ì)ô)™Õ¹Ñ¥½¸Ñ½±•A½ÁÕÁA±…å•ÉM¥é” ¥ì(€½¹ÍÐµ½‘…°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á±…å•É5½‘…°œ¤ì(€¥˜¡Á±…å•ÉÕ±±ÍÉ••¹±•µ•¹Ð ¤ôôõµ½‘…°¥íÍ•ÑA½ÁÕÁA±…å•É5…à¡™…±Í”¤í•á¥ÑA±…å•ÉÕ±±ÍÉ••¸ ¤íÉ•ÑÕÉ¸íô(€Í•ÑA½ÁÕÁA±…å•É5…à¡ÑÉÕ”¤ì(€É•ÅÕ•ÍÑA±…å•ÉÕ±±ÍÉ••¸¡µ½‘…°¤ì)ô)™Õ¹Ñ¥½¸Íå¹A±…å•ÉÕ±±ÍÉ••¹á¥Ð ¥ì(€¥˜¡Á±…å•ÉÕ±±ÍÉ••¹±•µ•¹Ð ¤¥É•ÑÕÉ¸ì(€½¹ÍÐµ½‘…°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á±…å•É5½‘…°œ¤ì(€¥˜¡µ½‘…°˜™µ½‘…°¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì Í•Ñ¥½¹µ…àœ¤¥Í•ÑA½ÁÕÁA±…å•É5…à¡™…±Í”¤ì(€½¹ÍÐÍ±½Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙA±…å•ÉM±½Ðœ¤ì(€¥˜¡Í±½Ð˜™Í±½Ð¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì Í•Ñ¥½¹µ…àœ¤˜™µåÑÙY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥ÑÙM•Ñ5¥¹¤¡ÑÉÕ”¤ì)ô)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ™Õ±±ÍÉ••¹¡…¹”œ±Íå¹A±…å•ÉÕ±±ÍÉ••¹á¥Ð¤ì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È Ý•‰­¥Ñ™Õ±±ÍÉ••¹¡…¹”œ±Íå¹A±…å•ÉÕ±±ÍÉ••¹á¥Ð¤ì)™Õ¹Ñ¥½¸±½Í•A±…å•È ¥ì(€½¹ÍÐµ½‘…°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Á±…å•É5½‘…°œ¤ì(€½¹ÍÐÙ¥‘•¼õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÁY¥‘•¼œ¤ì(€¥˜¡µ½‘…°¹}Á±…å‰…­½¹ÑÉ½±±•È¥íµ½‘…°¹}Á±…å‰…­½¹ÑÉ½±±•È¹ÍÑ½À ¤íµ½‘…°¹}Á±…å‰…­½¹ÑÉ½±±•Èõ¹Õ±°íô(€¥˜¡}¡±Ì¥íÑÉåí}¡±Ì¹‘•ÍÑÉ½ä ¤íõ…Ñ ¡”¥íõ}¡±Ìõ¹Õ±°íõ¥˜¡}µÁ•ÑÌ¥í‘•ÍÑÉ½å5Á•ÑÍA±…å•È¡}µÁ•ÑÌ¤í}µÁ•ÑÌõ¹Õ±°íô(€Ù¥‘•¼¹Á…ÕÍ” ¤íÙ¥‘•¼¹É•µ½Ù•ÑÑÉ¥‰ÕÑ” ÍÉŒœ¤íÙ¥‘•¼¹±½… ¤ì(€µ½‘…°¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€µ½‘…°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” Í•Ñ¥½¹µ…àœ¤ì(€½¹ÍÐ±¥Ù•Ý…äô¡}ÑÙA±…å¥¹œ„ôõ¹Õ±±ññÝ¥¹‘½Ü¹}ÑÙA±…å‰…­½¹ÑÉ½±±•È¤˜™µåÑÙY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤ì(€¥˜ …±¥Ù•Ý…ä¥‘½Õµ•¹Ð¹‰½‘ä¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ÑÙÍ•Ñ¥½¹Á±…äœ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Á±…åY1¡Í¥±‰Ñ¸¥ì(€½¹ÍÐ½±õ‰Ñ¸ý‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðèœœì(€¥˜¡‰Ñ¸¥í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðô=Á•¹¥¹œ¸¸¸œíô(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½Á±…äœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íÍÑÉ•…µ}¥éÍ¥‘ô¥ô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥í…±•ÉÐ¡¨¹•ÉÉ½Éñð½Õ±¹½Ð±…Õ¹ Y1¸œ¤íô(€õ…Ñ ¡”¥í…±•ÉÐ ½Õ±¹½Ð±…Õ¹ Y1¸œ¤íô(€¥˜¡‰Ñ¸¥íÍ•ÑQ¥µ•½ÕÐ  ¤ôùí‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±íô°ÄÈÀÀ¤íô)ô)±•Ð}™…Ù5½Ù¥•M•Ðõ¹•ÜM•Ð ¤ì)™Õ¹Ñ¥½¸±•…¹5½Ù¥•M•…É¡Q¥Ñ±”¡¹…µ”¥ì(€É•ÑÕÉ¸MÑÉ¥¹œ¡¹…µ•ñðœœ¤(€€€€¹É•Á±…” ½yqqÌ¨¸¬ýqqÌ¬µqqÌ¬¼°œœ¤(€€€€¹É•Á±…” ½yqqÌ¨¹ìÄ°ÐÁôýqqÌ©qqñqqÌ¨¼°œœ¤(€€€€¹É•Á±…” ½qqÌ©qp  üéUMñU-ñ	ñ9=ñ9ñMñ-ñ$¥qp¥qqÌ¨½¤°œœ¤(€€€€¹É•Á±…” ½qqÌ©qp  üèÄåðÈÀ¥qq‘ìÉõqp¥qqÌ¨¼°œœ¤(€€€€¹ÑÉ¥´ ¤ì)ô)™Õ¹Ñ¥½¸µ½Ù¥•…É¡´±Í¡½Ýe•…È±É••¹Ð¥ì(€½¹ÍÐÍ¥õ•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥ôõ¹Õ±°üœœé´¹ÍÑÉ•…µ}¥¤¤°•áÐõ•ÍÑÑÈ¡´¹•áÑ•¹Í¥½¹ñðµÀÐœ¤°­•äõMÑÉ¥¹œ¡´¹…Ñ…±½}¥‘ññ´¹ÍÑÉ•…µ}¥‘ñðœœ¤ì(€½¹ÍÐ™…Øõ}™…Ù5½Ù¥•M•Ð¹¡…Ì¡­•ä¤üœ½¸œèœœì(€½¹ÍÐ‘¥ÍÁ±…å9…µ”õÉ••¹Ðý±•…¹5½Ù¥•M•…É¡Q¥Ñ±”¡´¹¹…µ”¤é´¹¹…µ”ì(€½¹ÍÐÁ½ÍÑ•Èõ´¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡´¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹Á…É•¹Ñ±•µ•¹Ð¹Ñ•áÑ½¹Ñ•¹ÐõMÑÉ¥¹œ¹™É½µ½‘•A½¥¹Ð ÄÈÜäÄØ¤ˆøœèœ˜ŒÄÈÜäÄØìœì(€±•Ðµ•Ñ„ôœœì(€¥˜¡Í¡½Ýe•…È˜™´¹å•…È¥µ•Ñ„¬õ•ÍŒ¡´¹å•…È¤ì(€¥˜¡´¹É…Ñ¥¹œ¥µ•Ñ„¬ô¡µ•Ñ„üœ€™¹‰ÍÀì€œèœœ¤¬I…Ñ¥¹œè€œ­•ÍŒ¡´¹É…Ñ¥¹œ¤ì(€½¹ÍÐ…É‘±…ÍÌôµ½Ù¥•…Éœ¬¡É••¹ÐüœÉ••¹Ñµ½Ù¥”œèœœ¤ì(€½¹ÍÐ…É‘…Ñ„õÉ••¹Ðüœ‘…Ñ„µÅÕ•Éäôˆœ­•ÍÑÑÈ¡±•…¹5½Ù¥•M•…É¡Q¥Ñ±”¡´¹¹…µ”¤¤¬œˆœèœœì(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌôˆœ­…É‘±…ÍÌ¬œˆœ­…É‘…Ñ„¬œøñ‘¥Ø±…ÍÌô‰µ½Ù¥•Á½ÍÑ•Èˆøœ­Á½ÍÑ•È¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ½Ù¥•¥¹™¼ˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•Ñ¥Ñ±”ˆøœ­•ÍŒ¡‘¥ÍÁ±…å9…µ”¤¬œð½‘¥Øøœ(€€€€¬¡µ•Ñ„üœñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆøœ­µ•Ñ„¬œð½‘¥Øøœèœœ¤(€€€€¬œñ‘¥Ø±…ÍÌô‰µ½Ù¥•…Ñ¥½¹ÌˆøñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…Èµ½Ù¥•ÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µ­•äôˆœ­•ÍÑÑÈ¡­•ä¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡´¹…Ñ…±½}¥‘ñðœœ¤¬œˆ‘…Ñ„µÍ¥ôˆœ­Í¥¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡´¹¹…µ•ñðœœ¤¬œˆ‘…Ñ„µ•áÐôˆœ­•áÐ¬œˆ‘…Ñ„µå•…Èôˆœ­•ÍÑÑÈ¡´¹å•…Éñðœœ¤¬œˆ‘…Ñ„µÉ…Ñ¥¹œôˆœ­•ÍÑÑÈ¡´¹É…Ñ¥¹ñðœœ¤¬œˆ‘…Ñ„µ½Ù•Èôˆœ­•ÍÑÑÈ¡´¹½Ù•Éñðœœ¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øœ(€€€€¬¡É••¹Ðüœœè¡´¹ÍÑÉ•…µ}™½Õ¹üœñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Ù±Œµ½Ù¥•Ù±Œˆ‘…Ñ„µÍ¥ôˆœ­Í¥¬œˆ‘…Ñ„µ•áÐôˆœ­•áÐ¬œˆø˜ŒäØÔàìY1ð½‰ÕÑÑ½¸øœèœñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐˆ‘¥Í…‰±•øœ­ÑÈ 9½Ð…Ù…¥±…‰±”œ¤¬œð½‰ÕÑÑ½¸øœ¤¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘I••¹Ñ5½Ù¥•Ì¡±¥µ¥Ð¥ì(€±¥µ¥Ðõ±¥µ¥Ññðäì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É••¹Ñ5½Ù¥•1¥ÍÐœ¤ì(€½¹ÍÐµ½É”õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É••¹Ñ5½Ù¥•5½É”œ¤ì(€•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù1½…‘¥¹œ¸¸¸ð½ÍÁ…¸øœì(€µ½É”¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½É••¹Ñ}µ½Ù¥•Ìý±¥µ¥Ðôœ­±¥µ¥Ð¤ì(€¥˜¡ÑåÁ•½˜È¹±½•‘}¥¸ôôô‰½½±•…¸œ¥Í•Ñ5½Ù¥•AÉ½Ù¥‘•É1…å½ÕÐ¡È¹±½•‘}¥¸¤ì(€¥˜¡È¹•ÉÉ½È¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù½Õ±¹½Ð±½…É••¹Ñ±ä…‘‘•µ½Ù¥•Ì¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸™…±Í”íô(€¥˜ …È¹±½•‘}¥¸¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù1½œ¥¸Ù¥„M•ÑÑ¥¹Ì™¥ÉÍÐ¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸™…±Í”íô(€¥˜ …È¹µ½Ù¥•Ì¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼É••¹Ðµ½Ù¥•Ì™½Õ¹¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸™…±Í”íô(€…Ý…¥Ð±½…‘5½Ù¥•…Ù½É¥Ñ•Ì ¤ì(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µ½Ù¥•É¥ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÀˆøœ­È¹µ½Ù¥•Ì¹µ…À¡´ôùµ½Ù¥•…É¡´±™…±Í”±ÑÉÕ”¤¤¹©½¥¸ œœ¤¬œð½‘¥Øøœì(€¥˜¡±¥µ¥ÐðÌØ˜™È¹¡…Í}µ½É”¥µ½É”¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€É•ÑÕÉ¸ÑÉÕ”ì)ô)±•Ð}µ½Ù¥•…Ñ…±½œôÁ½ÁÕ±…Èœ±}µ½Ù¥•…Ñ…±½…¡”õíôì)™Õ¹Ñ¥½¸Í•Ñ5½Ù¥•AÉ½Ù¥‘•É1…å½ÕÐ¡±½•‘%¸¥ì(€½¹ÍÐ…Ñ…±½Ìõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•…Ñ…±½Ìœ¤ì(€¥˜¡…Ñ…±½Ì¥…Ñ…±½Ì¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¹½áÑÉ•…´œ°…±½•‘%¸¤ì(€½¹ÍÐÉ•™É•Í õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•I•™É•Í¡	Ñ¸œ¤ì(€¥˜¡É•™É•Í ¥É•™É•Í ¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…±½•‘%¸¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘¥¹•µ•Ñ…5½Ù¥•Ì¡…Ñ…±½œ¥ì(€}µ½Ù¥•…Ñ…±½œõlÁ½ÁÕ±…Èœ°¹•Üœ°™•…ÑÕÉ•t¹¥¹±Õ‘•Ì¡…Ñ…±½œ¤ý…Ñ…±½œèÁ½ÁÕ±…Èœì(€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° m‘…Ñ„µµ½Ù¥”µ…Ñ…±½tœ¤¹™½É… ¡™Õ¹Ñ¥½¸¡‰Ñ¸¥í‰Ñ¸¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½¸œ±‰Ñ¸¹‘…Ñ…Í•Ð¹µ½Ù¥•…Ñ…±½œôôõ}µ½Ù¥•…Ñ…±½œ¤íô¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ¥¹•µ•Ñ…5½Ù¥•1¥ÍÐœ¤ì(€¥˜ …•°¥É•ÑÕÉ¸ì(€½¹ÍÐ…¡•õ}µ½Ù¥•…Ñ…±½…¡•m}µ½Ù¥•…Ñ…±½tì(€¥˜¡…¡•¥ì(€€€Í•Ñ5½Ù¥•AÉ½Ù¥‘•É1…å½ÕÐ¡…¡•¹±½•‘}¥¸¤ì(€€€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µ½Ù¥•É¥ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÀˆøœ­…¡•¹µ½Ù¥•Ì¹µ…À¡´ôùµ½Ù¥•…É¡´±ÑÉÕ”±™…±Í”¤¤¹©½¥¸ œœ¤¬œð½‘¥Øøœì(€€€É•ÑÕÉ¸ì(€ô(€•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù1½…‘¥¹œ¸¸¸ð½ÍÁ…¸øœì(€ÑÉåì(€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½µ½Ù¥•}…Ñ…±½œý…Ñ…±½œôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡}µ½Ù¥•…Ñ…±½œ¤¬œ™±¥µ¥ÐôÄÀœ¤ì(€€€¥˜¡ÑåÁ•½˜È¹±½•‘}¥¸ôôô‰½½±•…¸œ¥Í•Ñ5½Ù¥•AÉ½Ù¥‘•É1…å½ÕÐ¡È¹±½•‘}¥¸¤ì(€€€¥˜¡È¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡È¹•ÉÉ½È¤ì(€€€¥˜ …È¹µ½Ù¥•Ì¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼µ½Ù¥•Ì™½Õ¹¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€€€}µ½Ù¥•…Ñ…±½…¡•m}µ½Ù¥•…Ñ…±½tõíµ½Ù¥•ÌéÈ¹µ½Ù¥•Ì±±½•‘}¥¸è„…È¹±½•‘}¥¹ôì(€€€…Ý…¥Ð±½…‘5½Ù¥•…Ù½É¥Ñ•Ì ¤ì(€€€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µ½Ù¥•É¥ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÀˆøœ­È¹µ½Ù¥•Ì¹µ…À¡´ôùµ½Ù¥•…É¡´±ÑÉÕ”±™…±Í”¤¤¹©½¥¸ œœ¤¬œð½‘¥Øøœì(€õ…Ñ ¡”¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù½Õ±¹½Ð±½…µ½Ù¥”…Ñ…±½œ¸ð½ÍÁ…¸øœíô)ô)…Íå¹Œ™Õ¹Ñ¥½¸¡•­5½Ù¥•Ì¡‰Ñ¸¥ì(€½¹ÍÐ½±õ‰Ñ¸¹¥¹¹•É!Q50í‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðô¡•­¥¹œ™½È¹•Üµ½Ù¥•Ì¸¸¸œì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½¡•­}µ½Ù¥•}ÕÁ‘…Ñ•Ìœ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡¨¹•ÉÉ½Éñðµ½Ù¥”É•™É•Í ™…¥±•œ¤ì(€€€…Ý…¥Ð±½…‘I••¹Ñ5½Ù¥•Ì ä¤ì(€€€¥˜¡¨¹¹•Ý}µ½Ù¥•ÌøÀ¥Ñ½…ÍÐ ½Õ¹€œ­¨¹¹•Ý}µ½Ù¥•Ì¬œ¹•Üµ½Ù¥”œ¬¡¨¹¹•Ý}µ½Ù¥•ÌôôôÄüœœèÌœ¤°ÜÀÀÀ¤ì(€€€•±Í”Ñ½…ÍÐ 5½Ù¥”±¥‰É…Éä¥ÌÕÀÑ¼‘…Ñ”¸œ°ÜÀÀÀ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ ½Õ±¹½ÐÉ•™É•Í µ½Ù¥”±¥‰É…Éä¸œ°ÜÀÀÀ¤íô(€‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹¥¹¹•É!Q50õ½±ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸•áÁ…¹‘I••¹Ñ5½Ù¥•Ì¡‰Ñ¸¥ì(€½¹ÍÐ½±õ‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðí‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðô1½…‘¥¹œ¸¸¸œì(€…Ý…¥Ð±½…‘I••¹Ñ5½Ù¥•Ì ÌØ¤ì(€‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±í‰Ñ¸¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘5½Ù¥•…Ù½É¥Ñ•Ì ¥ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ¤ì(€½¹ÍÐµ½Ù¥•ÌõÈ¹µ½Ù¥•Íññmtì(€}™…Ù5½Ù¥•M•Ðõ¹•ÜM•Ð¡µ½Ù¥•Ì¹µ…À¡´ôùMÑÉ¥¹œ¡´¹…Ñ…±½}¥‘ññ´¹ÍÑÉ•…µ}¥¤¤¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•…Ù1¥ÍÐœ¤ì(€¥˜ …µ½Ù¥•Ì¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼™…Ù½É¥Ñ”µ½Ù¥•Ìå•Ð¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€±•Ð ôœœì(€™½È¡½¹ÍÐ´½˜µ½Ù¥•Ì¥ì(€€€½¹ÍÐ­•äõ•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹…Ñ…±½}¥‘ññ´¹ÍÑÉ•…µ}¥¤¤ì(€€€½¹ÍÐ±•…¹9…µ”õ±•…¹5½Ù¥•M•…É¡Q¥Ñ±”¡´¹¹…µ”¤ì(€€€½¹ÍÐÁ½ÍÑ•ÈôœñÍÁ…¸±…ÍÌô‰µ½Ù¥•™…ÙÁ½ÍÑ•Èˆø˜ŒÄÈÜäÄØìœ¬¡´¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡´¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œð½ÍÁ…¸øœì(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µ½Ù¥•™…Øˆ‘…Ñ„µÅÕ•Éäôˆœ­•ÍÑÑÈ¡±•…¹9…µ”¤¬œˆøœ­Á½ÍÑ•È¬œñ‘¥Ø±…ÍÌô‰µ½Ù¥•™…Ù¥¹™¼ˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•™…Ù¹…µ”ˆøœ­•ÍŒ¡±•…¹9…µ”¤¬œð½‘¥Øøð½‘¥Øøœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…È½¸µ½Ù¥•É•µ½Ù”ˆ‘…Ñ„µ­•äôˆœ­­•ä¬œˆÑ¥Ñ±”ô‰I•µ½Ù”™É½´™…Ù½É¥Ñ•Ìˆø˜ŒäÜÌÌìð½ÍÁ…¸øð½‘¥Øøœì(€ô(€•°¹¥¹¹•É!Q50õ ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Ñ½±•5½Ù¥•…Ù½É¥Ñ”¡µ½Ù¥”±ÍÑ…É°¥ì(€½¹ÍÐÈõ…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÑ½±•}µ½Ù¥”œ±µ½Ù¥”éµ½Ù¥•ô¤ì(€}™…Ù5½Ù¥•M•Ðõ¹•ÜM•Ð ¡È¹µ½Ù¥•}¥‘Íññmt¤¹µ…À¡MÑÉ¥¹œ¤¤ì(€¥˜¡}™…Ù5½Ù¥•M•Ð¹¡…Ì¡MÑÉ¥¹œ¡µ½Ù¥”¹…Ñ…±½}¥‘ññµ½Ù¥”¹ÍÑÉ•…µ}¥¤¤¥}ÁÉ½™¥±•½¹™¥œ¹Í•ÑÕÁ}‘•µ½}½¹Ñ•¹Ðõ™…±Í”ì(€¥˜¡ÍÑ…É°¥ÍÑ…É°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½¸œ±}™…Ù5½Ù¥•M•Ð¹¡…Ì¡MÑÉ¥¹œ¡µ½Ù¥”¹…Ñ…±½}¥‘ññµ½Ù¥”¹ÍÑÉ•…µ}¥¤¤¤ì(€…Ý…¥Ð±½…‘5½Ù¥•…Ù½É¥Ñ•Ì ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•µ½Ù•5½Ù¥•…Ù½É¥Ñ”¡­•ä¥ì(€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÉ•µ½Ù•}µ½Ù¥”œ±™…Ù½É¥Ñ•}­•äé­•åô¤ì(€}™…Ù5½Ù¥•M•Ð¹‘•±•Ñ”¡MÑÉ¥¹œ¡­•ä¤¤ì(€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹µ½Ù¥•ÍÑ…Èœ¤¹™½É… ¡•°ôùí¥˜¡•°¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ­•äœ¤ôôõMÑÉ¥¹œ¡­•ä¤¥•°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ½¸œ¤íô¤ì(€…Ý…¥Ð±½…‘5½Ù¥•…Ù½É¥Ñ•Ì ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Í•…É¡5½Ù¥•Ì ¥ì(€½¹ÍÐÄô¡‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•Dœ¤¹Ù…±Õ•ñðœœ¤¹ÑÉ¥´ ¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•I•ÍÕ±ÑÌœ¤ì(€½¹ÍÐ…Ñ…±½Ìõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•…Ñ…±½Ìœ¤ì(€¥˜ …Ä¥í…Ñ…±½Ì¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÄÑÁàˆù¹Ñ•È„µ½Ù¥”Ñ¥Ñ±”¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€…Ñ…±½Ì¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÄÑÁàˆùM•…É¡¥¹œµ½Ù¥•Ì¸¸¸ð½‘¥Øøœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½µ½Ù¥•ÌýÄôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Ä¤¤ì(€½¹ÍÐ‰…¬ôœñ‘¥Ø±…ÍÌô‰µ½Ù¥•É•ÍÕ±Ñ‰…¬ˆøñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐˆ½¹±¥¬ô‰‰…­Q½5å5½Ù¥•Ì ¤ˆø˜ŒàÔäÈì€œ­ÑÈ 	…¬Ñ¼5½Ù¥•Ìœ¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøœì(€¥˜¡È¹•ÉÉ½È¥í•°¹¥¹¹•É!Q50õ‰…¬¬œñ‘¥Ø±…ÍÌô‰•ÉÈµ½Ù¥•É•ÍÕ±ÑÍÑ…ÑÕÌˆøœ­•ÍŒ¡È¹•ÉÉ½È¤¬œð½‘¥ØøœíÉ•ÑÕÉ¸íô(€¥˜ …È¹µ½Ù¥•Ì¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50õ‰…¬¬œñ‘¥Ø±…ÍÌô‰µÕÑ•µ½Ù¥•É•ÍÕ±ÑÍÑ…ÑÕÌˆù9¼µ½Ù¥•Ì™½Õ¹™½È€™ÅÕ½Ðìœ­•ÍŒ¡Ä¤¬œ™ÅÕ½Ðì¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€…Ý…¥Ð±½…‘5½Ù¥•…Ù½É¥Ñ•Ì ¤ì(€±•Ð õ‰…¬¬œñ‘¥Ø±…ÍÌô‰µÕÑ•µ½Ù¥•É•ÍÕ±ÑÍÑ…ÑÕÌˆøœ­È¹µ½Ù¥•Ì¹±•¹Ñ ¬œÉ•ÍÕ±Ðœ¬¡È¹µ½Ù¥•Ì¹±•¹Ñ ôôôÄüœœèÌœ¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ½Ù¥•É¥ˆøœì(€™½È¡½¹ÍÐ´½˜È¹µ½Ù¥•Ì¥ ¬õµ½Ù¥•…É¡´±ÑÉÕ”¤ì(€•°¹¥¹¹•É!Q50õ ¬œð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸‰…­Q½5å5½Ù¥•Ì ¥ì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•Dœ¤¹Ù…±Õ”ôœœì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•I•ÍÕ±ÑÌœ¤¹¥¹¹•É!Q50ôœœì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•…Ñ…±½Ìœ¤¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Á±…å5½Ù¥•Y1¡Í¥±•áÐ±‰Ñ¸¥ì(€½¹ÍÐ½±õ‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðí‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðô=Á•¹¥¹œ¸¸¸œì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½Á±…å}µ½Ù¥”œ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íÍÑÉ•…µ}¥éÍ¥±•áÑ•¹Í¥½¸é•áÑô¥ô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥…±•ÉÐ¡¨¹•ÉÉ½Éñð½Õ±¹½Ð±…Õ¹ Y1¸œ¤ì(€õ…Ñ ¡”¥í…±•ÉÐ ½Õ±¹½Ð±…Õ¹ Y1¸œ¤íô(€Í•ÑQ¥µ•½ÕÐ  ¤ôùí‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±íô°ÄÈÀÀ¤ì)ô)±•Ð}Ý¥Í¡±¥ÍÑ…µ•Ìõmt±}Ý¥Í¡±¥ÍÑ1¥¹­•õ™…±Í”ì)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘MÑ•…µAÉ½™¥±” ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µAÉ½™¥±”œ¤í¥˜ …•°¥É•ÑÕÉ¸ì(€ÑÉåì(€€€½¹ÍÐÀõ…Ý…¥Ð…Á¤ œ½…Á¤½ÍÑ•…µ}ÁÉ½™¥±”œ¤ì(€€€¥˜ …Áñð…À¹±¥¹­•¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±••µÁÑäˆù1¥¹¬„MÑ•…´Ý¥Í¡±¥ÍÐÑ¼Í¡½Üå½ÕÈMÑ•…´ÁÉ½™¥±”¡•É”¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€€€¥˜ …À¹‘¥ÍÁ±…å}¹…µ”˜˜…À¹…Ù…Ñ…È¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±••µÁÑäˆùMÑ•…´ÁÉ½™¥±”¥Ì±¥¹­•°‰ÕÐ¥ÑÌÁÕ‰±¥ŒÁÉ½™¥±”‘•Ñ…¥±Ì…É”Õ¹…Ù…¥±…‰±”¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€€€½¹ÍÐ…Ù…Ñ…ÉMÉŒõÀ¹…Ù…Ñ…É}±½…±ññÀ¹…Ù…Ñ…Éñðœœ±…Ù…Ñ…É…±±‰…¬ô¡À¹…Ù…Ñ…É}±½…°˜™À¹…Ù…Ñ…È¤ýÀ¹…Ù…Ñ…Èèœœì(€€€½¹ÍÐ…Ù…Ñ…Èõ…Ù…Ñ…ÉMÉŒüœñ¥µœ±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•…Ù…Ñ…ÈˆÍÉŒôˆœ­•ÍÑÑÈ¡…Ù…Ñ…ÉMÉŒ¤¬œˆ‘…Ñ„µ™…±±‰…¬ôˆœ­•ÍÑÑÈ¡…Ù…Ñ…É…±±‰…¬¤¬œˆ…±ÐôˆˆÉ•™•ÉÉ•ÉÁ½±¥äô‰¹¼µÉ•™•ÉÉ•Èˆ½¹•ÉÉ½Èô‰¥˜¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹™…±±‰…¬˜˜…Ñ¡¥Ì¹‘…Ñ…Í•Ð¹ÑÉ¥•¥íÑ¡¥Ì¹‘…Ñ…Í•Ð¹ÑÉ¥•ôÄíÑ¡¥Ì¹ÍÉŒõÑ¡¥Ì¹‘…Ñ…Í•Ð¹™…±±‰…¬íõ•±Í”Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€€€½¹ÍÐÉ•…°õÀ¹É•…±}¹…µ”üœñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•É•…°ˆøœ­•ÍŒ¡À¹É•…±}¹…µ”¤¬œð½‘¥Øøœèœœì(€€€½¹ÍÐ±½ŒõÀ¹±½…Ñ¥½¸üœñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•±½Œˆøœ­•ÍŒ¡À¹±½…Ñ¥½¸¤¬œð½‘¥Øøœèœœì(€€€½¹ÍÐ±•Ù•°ô¡À¹±•Ù•°„ôõÕ¹‘•™¥¹•˜™À¹±•Ù•°„ôõ¹Õ±°¤üœñÍÁ…¸±…ÍÌô‰ÍÑ•…µ±•Ù•°ˆÑ¥Ñ±”ô‰MÑ•…´±•Ù•°ˆøœ­•ÍŒ¡À¹±•Ù•°¤¬œð½ÍÁ…¸øœèœœì(€€€½¹ÍÐå•…ÉÌô¡À¹å•…ÉÍ}Í•ÉÙ¥”„ôõÕ¹‘•™¥¹•˜™À¹å•…ÉÍ}Í•ÉÙ¥”„ôõ¹Õ±°¤üœñÍÁ…¸±…ÍÌô‰ÍÑ•…µå•…ÉÌˆøœ­•ÍŒ¡À¹å•…ÉÍ}Í•ÉÙ¥”¤¬œå•…ÉÌ½˜Í•ÉÙ¥”ð½ÍÁ…¸øœèœœì(€€€½¹ÍÐÍÕµµ…ÉäõÀ¹ÍÕµµ…Éäüœñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•ÍÕµµ…Éäˆøœ­•ÍŒ¡À¹ÍÕµµ…Éä¤¬œð½‘¥Øøœèœœì(€€€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•¥¹¹•Èˆøñ„±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•±¥¹¬ˆ¡É•˜ôˆœ­•ÍÑÑÈ¡À¹ÁÉ½™¥±•}ÕÉ±ñðœŒœ¤¬œˆÑ…É•Ðô‰}‰±…¹¬ˆÉ•°ô‰¹½½Á•¹•È¹½É•™•ÉÉ•Èˆøñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•¡•…ˆøœ­…Ù…Ñ…È¬œñ‘¥Øøñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•¹…µ”ˆøœ­•ÍŒ¡À¹‘¥ÍÁ±…å}¹…µ•ñðMÑ•…´œ¤¬œð½‘¥Øøœ­É•…°­±½Œ¬œð½‘¥Øøð½‘¥Øøð½„øñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±•µ•Ñ„ˆøœ­±•Ù•°­å•…ÉÌ¬œð½‘¥Øøœ­ÍÕµµ…Éä¬œð½‘¥Øøœì(€õ…Ñ ¡”¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰ÍÑ•…µÁÉ½™¥±••µÁÑäˆùMÑ•…´ÁÉ½™¥±”‘•Ñ…¥±ÌÕ¹…Ù…¥±…‰±”¸ð½‘¥Øøœíô)ô)™Õ¹Ñ¥½¸ÕÁ‘…Ñ•MÑ•…µ]¥Í¡±¥ÍÑ!•±À ¥ì(€½¹ÍÐ¡…Í…µ•Ìõ}Ý¥Í¡±¥ÍÑ1¥¹­•˜™}Ý¥Í¡±¥ÍÑ…µ•Ì¹±•¹Ñ øÀ±•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑ!•±Àœ¤±™¥±Ñ•ÉI½Üõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …µ•]¥Í¡±¥ÍÑ¥±Ñ•ÉI½Üœ¤ì(€¥˜¡•°¥•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ±¡…Í…µ•Ì¤ì(€¥˜¡™¥±Ñ•ÉI½Ü¥™¥±Ñ•ÉI½Ü¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…¡…Í…µ•Ì¤ì(€½¹ÍÐÍ•ÑÑ¥¹Ìõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑM•ÑÑ¥¹Ìœ¤±ÅÕ¥¬õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑEÕ¥­	Ñ¸œ¤ì(€¥˜¡Í•ÑÑ¥¹Ì˜˜…}Ý¥Í¡±¥ÍÑ1¥¹­•¥Í•ÑÑ¥¹Ì¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€¥˜¡ÅÕ¥¬¥ÅÕ¥¬¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…}Ý¥Í¡±¥ÍÑ1¥¹­•¤ì)ô)™Õ¹Ñ¥½¸Ñ½±•MÑ•…µ]¥Í¡±¥ÍÑM•ÑÑ¥¹Ì ¥í½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑM•ÑÑ¥¹Ìœ¤í¥˜¡•°¥•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ¤íô)™Õ¹Ñ¥½¸É•¹‘•É…µ•]¥Í¡±¥ÍÐ ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …µ•]¥Í¡±¥ÍÐœ¤í¥˜ …•°¥É•ÑÕÉ¸ì(€½¹ÍÐ¥¹ÁÕÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …µ•]¥Í¡±¥ÍÑ¥±Ñ•Èœ¤±ÄõMÑÉ¥¹œ ¡¥¹ÁÕÐ˜™¥¹ÁÕÐ¹Ù…±Õ”¥ñðœœ¤¹ÑÉ¥´ ¤¹Ñ½1½Ý•É…Í” ¤ì(€½¹ÍÐ…µ•ÌõÄý}Ý¥Í¡±¥ÍÑ…µ•Ì¹™¥±Ñ•È¡œôùMÑÉ¥¹œ¡œ¹¹…µ•ñðœœ¤¹Ñ½1½Ý•É…Í” ¤¹¥¹±Õ‘•Ì¡Ä¤¤é}Ý¥Í¡±¥ÍÑ…µ•Ìì(€¥˜ ……µ•Ì¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ¬¡Äü9¼µ…Ñ¡¥¹œÝ¥Í¡±¥ÍÐ…µ•Ì¸œè9¼MÑ•…´Ý¥Í¡±¥ÍÐ…µ•Ìå•Ð¸œ¤¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤ì(€•°¹¥¹¹•É!Q50õ…µ•Ì¹µ…À¡œôùí½¹ÍÐÕÉ°õœ¹ÕÉ±ñð ¡ÑÑÁÌè¼½ÍÑ½É”¹ÍÑ•…µÁ½Ý•É•¹½´½…ÁÀ¼œ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡œ¹…ÁÁ}¥‘ñðœœ¤¤¬œ¼œ¤±É•±•…Í•QÌõ…Ñ”¹Á…ÉÍ”¡œ¹É•±•…Í•‘ñðœœ¤±½Õ¹Ñ‘½Ý¸õ9Õµ‰•È¹¥Í¥¹¥Ñ”¡É•±•…Í•QÌ¤˜™É•±•…Í•QÌù¹½ÜýÉ…¥¹½Õ¹Ñ‘½Ý¸¡íÍÑ…ÉÐéœ¹É•±•…Í•‘ô¤èœœ±É•±•…Í”ô¡œ¹É•±•…Í•}Ñ•áÑññ½Õ¹Ñ‘½Ý¸¤üœñ‘¥Ø±…ÍÌô‰…µ•…É‘É•±•…Í”ˆøœ¬¡œ¹É•±•…Í•}Ñ•áÐüœñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆøœ­•ÍŒ¡œ¹É•±•…Í•}Ñ•áÐ¤¬œð½‘¥Øøœèœœ¤¬¡½Õ¹Ñ‘½Ý¸üœñ‘¥Ø±…ÍÌô‰…µ•½Õ¹Ñ‘½Ý¸ˆøœ­•ÍŒ¡½Õ¹Ñ‘½Ý¸¤¬œð½‘¥Øøœèœœ¤¬œð½‘¥ØøœèœœíÉ•ÑÕÉ¸€œñ„±…ÍÌô‰…µ•…ÉÝ¥Í¡±¥ÍÑ…µ”ˆ¡É•˜ôˆœ­•ÍÑÑÈ¡ÕÉ°¤¬œˆÑ…É•Ðô‰}‰±…¹¬ˆÉ•°ô‰¹½½Á•¹•È¹½É•™•ÉÉ•Èˆøœ¬¡œ¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡œ¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñ‘¥Ø±…ÍÌô‰…µ•…É‘‰½‘äˆøñ‘¥Ø±…ÍÌô‰…µ•…É‘¹…µ”ˆøœ­•ÍŒ¡œ¹¹…µ•ñð…µ”œ¤¬œð½‘¥Øøœ­É•±•…Í”¬œð½‘¥Øøð½„øœíô¤¹©½¥¸ œœ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘…µ•…Ù½É¥Ñ•Ì ¥ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ¤±¹½Üõ…Ñ”¹¹½Ü ¤±…µ•Ìô¡È¹…µ•Íññmt¤¹™¥±Ñ•È¡œôùœ¹Ý¥Í¡±¥ÍÑ}¥µÁ½ÉÑ•¤±•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% …µ•]¥Í¡±¥ÍÐœ¤ì(€…µ•Ì¹Í½ÉÐ ¡„±ˆ¤ôùì(€€€½¹ÍÐ…Ðõ…Ñ”¹Á…ÉÍ”¡„¹É•±•…Í•‘ñðœœ¤±‰Ðõ…Ñ”¹Á…ÉÍ”¡ˆ¹É•±•…Í•‘ñðœœ¤±…˜õ9Õµ‰•È¹¥Í¥¹¥Ñ”¡…Ð¤˜™…Ðù¹½Ü±‰˜õ9Õµ‰•È¹¥Í¥¹¥Ñ”¡‰Ð¤˜™‰Ðù¹½Ü±…Àõ9Õµ‰•È¹¥Í¥¹¥Ñ”¡…Ð¤˜™…Ððõ¹½Ü±‰Àõ9Õµ‰•È¹¥Í¥¹¥Ñ”¡‰Ð¤˜™‰Ððõ¹½Üì(€€€¥˜¡…˜„ôõ‰˜¥É•ÑÕÉ¸…˜ü´ÄèÄì(€€€¥˜¡…˜˜™‰˜¥É•ÑÕÉ¸…Ðµ‰Ðì(€€€¥˜¡…À„ôõ‰À¥É•ÑÕÉ¸…Àü´ÄèÄì(€€€¥˜¡…À˜™‰À¥É•ÑÕÉ¸‰Ðµ…Ðì(€€€É•ÑÕÉ¸MÑÉ¥¹œ¡„¹¹…µ•ñðœœ¤¹±½…±•½µÁ…É”¡MÑÉ¥¹œ¡ˆ¹¹…µ•ñðœœ¤¤ì(€ô¤ì(€}Ý¥Í¡±¥ÍÑ…µ•Ìõ…µ•ÌíÉ•¹‘•É…µ•]¥Í¡±¥ÍÐ ¤íÕÁ‘…Ñ•MÑ•…µ]¥Í¡±¥ÍÑ!•±À ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘MÑ•…µ]¥Í¡±¥ÍÑM•ÑÑ¥¹œ ¥ì(€ÑÉåì(€€€½¹ÍÐŒõ…Ý…¥Ð…Á¤ œ½…Á¤½½¹™¥œœ¤±¥¹ÁÕÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑDœ¤±‰Ñ¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑ	Ñ¸œ¤±ÍÑ…ÑÕÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑMÑ…ÑÕÌœ¤ì(€€€½¹ÍÐ±¥¹­•ô„…MÑÉ¥¹œ¡Œ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ±ñðœœ¤¹ÑÉ¥´ ¤ì(€€€}Ý¥Í¡±¥ÍÑ1¥¹­•õ±¥¹­•íÕÁ‘…Ñ•MÑ•…µ]¥Í¡±¥ÍÑ!•±À ¤ì(€€€¥˜¡¥¹ÁÕÐ¥í¥¹ÁÕÐ¹Ù…±Õ”õŒ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ±ñðœœí¥¹ÁÕÐ¹É•…‘=¹±äõ™…±Í”íô(€€€¥˜¡‰Ñ¸¥í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ Må¹ŒÝ¥Í¡±¥ÍÐœ¤í‰Ñ¸¹Í•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¤Äá¸œ°Må¹ŒÝ¥Í¡±¥ÍÐœ¤íô(€€€½¹ÍÐÍ•ÑÑ¥¹Ìõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑM•ÑÑ¥¹Ìœ¤í¥˜¡Í•ÑÑ¥¹Ì˜™±¥¹­•¥Í•ÑÑ¥¹Ì¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€€€¥˜¡±¥¹­•˜™ÍÑ…ÑÕÌ˜™Œ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}Íå¹•‘}…Ð¥ì(€€€€€½¹ÍÐ±½…±”õ}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•ì(€€€€€ÍÑ…ÑÕÌ¹Ñ•áÑ½¹Ñ•¹Ðô1…ÍÐÉ•™É•Í¡•€œ­¹•Ü…Ñ”¡9Õµ‰•È¡Œ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}Íå¹•‘}…Ð¤¨ÄÀÀÀ¤¹Ñ½1½…±•MÑÉ¥¹œ¡±½…±”¤ì(€€€ô(€õ…Ñ ¡”¥íô(€±½…‘MÑ•…µAÉ½™¥±” ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Íå¹MÑ•…µ]¥Í¡±¥ÍÐ¡‰Ñ¸¥ì(€½¹ÍÐ¥¹ÁÕÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑDœ¤±ÍÑ…ÑÕÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÍÑ•…µ]¥Í¡±¥ÍÑMÑ…ÑÕÌœ¤±Ù…±Õ”ô¡¥¹ÁÕÐ¹Ù…±Õ•ñðœœ¤¹ÑÉ¥´ ¤ì(€¥˜ …Ù…±Õ”¥íÍÑ…ÑÕÌ¹Ñ•áÑ½¹Ñ•¹Ðô¹Ñ•Èå½ÕÈÁÕ‰±¥ŒMÑ•…´Ý¥Í¡±¥ÍÐUI0¸œíÉ•ÑÕÉ¸íô(€½¹ÍÐ½±õ‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðí‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐôMå¹¥¹œ¸¸¸œíÍÑ…ÑÕÌ¹Ñ•áÑ½¹Ñ•¹ÐôI•…‘¥¹œMÑ•…´Ý¥Í¡±¥ÍÐ¸¸¸œì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½¥µÁ½ÉÑ}ÍÑ•…µ}Ý¥Í¡±¥ÍÐœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íÕÉ°éÙ…±Õ•ô¥ô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡¨¹•ÉÉ½Éñð]¥Í¡±¥ÍÐÍå¹Œ™…¥±•œ¤ì(€€€…Ý…¥Ð±½…‘…µ•…Ù½É¥Ñ•Ì ¤í±½…‘…Ù½É¥Ñ•Ì ¤í…Ý…¥Ð±½…‘MÑ•…µ]¥Í¡±¥ÍÑM•ÑÑ¥¹œ ¤íÍÑ…ÑÕÌ¹Ñ•áÑ½¹Ñ•¹ÐôMå¹•€œ­¨¹¥µÁ½ÉÑ•¬œ…µ•Ì™É½´MÑ•…´¸œì(€õ…Ñ ¡”¥íÍÑ…ÑÕÌ¹Ñ•áÑ½¹Ñ•¹Ðô½Õ±¹½ÐÍå¹ŒÝ¥Í¡±¥ÍÐè€œ­”¹µ•ÍÍ…”íô(€‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í¥˜¡‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐôôôMå¹¥¹œ¸¸¸œ¥‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±ì)ô)±•Ð}ÍÑ•…µ]¥Í¡±¥ÍÑÕÑ½¡•­•õ™…±Í”ì)…Íå¹Œ™Õ¹Ñ¥½¸µ…å‰•ÕÑ½I•™É•Í¡MÑ•…µ]¥Í¡±¥ÍÐ¡Œ¥ì(€¥˜¡}ÍÑ•…µ]¥Í¡±¥ÍÑÕÑ½¡•­•¥É•ÑÕÉ¸ì(€}ÍÑ•…µ]¥Í¡±¥ÍÑÕÑ½¡•­•õÑÉÕ”ì(€¥˜¡Œ˜™Œ¹…µ•Í}•¹…‰±•ôôõ™…±Í”¥É•ÑÕÉ¸ì(€½¹ÍÐÕÉ°õMÑÉ¥¹œ ¡Œ˜™Œ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ°¥ñðœœ¤¹ÑÉ¥´ ¤±Íå¹•õ9Õµ‰•È ¡Œ˜™Œ¹ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}Íå¹•‘}…Ð¥ñðÀ¤¨ÄÀÀÀì(€¥˜ …ÕÉ±ñð¡Íå¹•˜™…Ñ”¹¹½Ü ¤µÍå¹•ðÜ¨ÈÐ¨ØÀ¨ØÀ¨ÄÀÀÀ¤¥É•ÑÕÉ¸ì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½¥µÁ½ÉÑ}ÍÑ•…µ}Ý¥Í¡±¥ÍÐœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íÕÉ°éÕÉ±ô¥ô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥É•ÑÕÉ¸ì(€€€¥˜ ……µ•ÍY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥í…Ý…¥Ð±½…‘…µ•…Ù½É¥Ñ•Ì ¤í…Ý…¥Ð±½…‘MÑ•…µ]¥Í¡±¥ÍÑM•ÑÑ¥¹œ ¤íô(€€€¥˜ …µå±¥ÍÑY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥±½…‘…Ù½É¥Ñ•Ì ¤ì(€õ…Ñ ¡”¥íô)ô)½¹ÍÐ}I%9}MI%Lõml˜Äœ°½ÉµÕ±„€Ät±l˜Èœ°½ÉµÕ±„€Èt±l˜Ìœ°½ÉµÕ±„€Ìt±l¥¹‘å…Èœ°%¹‘å…Èt±lÝ•Œœ°]t±l™½ÉµÕ±…”œ°½ÉµÕ±„t±lµ½Ñ½Àœ°5½Ñ½@t±lÝÉŒœ°]II…±±äutì)½¹ÍÐ}I%9}1==Lõì(€˜Äè¡ÑÑÁÌè¼½ÝÝÜ¹™½ÉµÕ±„Ä¹½´½•ÑŒ½‘•Í¥¹Ì½™½´µÝ•‰Í¥Ñ”½¥µ…•Ì½˜Å}±½¼¹ÍÙœœ°(€˜Èè¡ÑÑÁÌè¼½ÕÁ±½…¹Ý¥­¥µ•‘¥„¹½Éœ½Ý¥­¥Á•‘¥„½½µµ½¹Ì¼Ø¼ØÐ½%}½ÉµÕ±…|É}¡…µÁ¥½¹Í¡¥Á}±½½|”ÈàÈÀÈØ”Èä¹ÍÙœœ°(€˜Ìè¡ÑÑÁÌè¼½ÕÁ±½…¹Ý¥­¥µ•‘¥„¹½Éœ½Ý¥­¥Á•‘¥„½½µµ½¹Ì½½ä½%}½ÉµÕ±…|Í}¡…µÁ¥½¹Í¡¥Á}±½½|”ÈàÈÀÈØ”Èä¹ÍÙœœ°(€¥¹‘å…Èè¡ÑÑÁÌè¼½ÝÝÜ¹¥¹‘å…È¹½´¼´½µ•‘¥„½%¹‘å…È½9•ÝÌ½MÑ…¹‘…É¼ÈÀÈÈ¼Àà¼Àà´ÀÌµ%9eHµ1½¼¹©Áœœ°(€Ý•Œè¡ÑÑÁÌè¼½ÕÁ±½…¹Ý¥­¥µ•‘¥„¹½Éœ½Ý¥­¥Á•‘¥„½½µµ½¹Ì¼Ð¼ÑŒ½%}]}1½½|ÈÀÈÐ¹Á¹œœ°(€™½ÉµÕ±…”è¡ÑÑÁÌè¼½ÕÁ±½…¹Ý¥­¥µ•‘¥„¹½Éœ½Ý¥­¥Á•‘¥„½½µµ½¹Ì½„½„Ð½%}½ÉµÕ±…}}]½É±‘}¡…µÁ¥½¹Í¡¥Á}1½¼¹ÍÙœœ°(€µ½Ñ½Àè¡ÑÑÁÌè¼½ÕÁ±½…¹Ý¥­¥µ•‘¥„¹½Éœ½Ý¥­¥Á•‘¥„½½µµ½¹Ì½˜½˜ä½5½Ñ½A}±½½|”ÈàÈÀÈÐ”Èä¹ÍÙœœ°(€ÝÉŒè¡ÑÑÁÌè¼½ÝÝÜ¹…¹•Ù…É…±±ä¹½´½ÝÀµ½¹Ñ•¹Ð½ÕÁ±½…‘Ì¼ÈÀÈÔ¼ÀÄ¼ÌÄÄÐÌäØÈÍ|ÄÜäØÔÀàäÜäÐÄÔàÝ|ÐÌÄÐàÐÔÜÐÔäÌäÈÔÄØäÍ}¸´Ìµ”ÄÜÄÐÄÐÀääÐÌÈÀ´ÄÌÀÉàÔÔà´Ä¹©Áœœ)ôì)±•Ð}É…¥¹M•±•Ñ•õ¹•ÜM•Ð¡l˜Ät¤ì)±•Ð}É…¥¹É¥Ù•ÉI½ÝÌõmt±}É…¥¹Ù•¹ÑI½ÝÌõmt±}É…¥¹•Ñ…¥±-•äôœœì)™Õ¹Ñ¥½¸É…¥¹Ù•¹Ñ%Í1¥Ù”¡•Ù•¹Ð±¹½Ü¥ì(€¹½Üõ¹½Ýññ…Ñ”¹¹½Ü ¤í½¹ÍÐÍÑ…ÉÐõ¹•Ü…Ñ”¡•Ù•¹Ð¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤í¥˜ …9Õµ‰•È¹¥Í¥¹¥Ñ”¡ÍÑ…ÉÐ¤¥É•ÑÕÉ¸™…±Í”ì(€½¹ÍÐ•áÁ±¥¥Ðõ•Ù•¹Ð¹•¹ý¹•Ü…Ñ”¡•Ù•¹Ð¹•¹¤¹•ÑQ¥µ” ¤é9…8ì(€¥˜¡9Õµ‰•È¹¥Í¥¹¥Ñ”¡•áÁ±¥¥Ð¤¥É•ÑÕÉ¸¹½ÜøõÍÑ…ÉÐ´¡•Ù•¹Ð¹…±±}‘…äüÄÈ¨ÌØÀÀÀÀÀèÀ¤˜™¹½Üðõ•áÁ±¥¥Ðì(€±•Ð‘ÕÉ…Ñ¥½¸ôÈ¨ÌØÀÀÀÀÀì(€¥˜¡•Ù•¹Ð¹…±±}‘…ä¥‘ÕÉ…Ñ¥½¸ôÈÐ¨ÌØÀÀÀÀÀì(€•±Í”¥˜¡MÑÉ¥¹œ¡•Ù•¹Ð¹Í•ÍÍ¥½¹ñðœœ¤¹Ñ½1½Ý•É…Í” ¤ôôôÉ…”œ¥‘ÕÉ…Ñ¥½¸ôÐ¨ÌØÀÀÀÀÀì(€É•ÑÕÉ¸¹½ÜøõÍÑ…ÉÐ˜™¹½ÜðõÍÑ…ÉÐ­‘ÕÉ…Ñ¥½¸ì)ô)™Õ¹Ñ¥½¸¹•áÑÉ¥Ù•ÉI…”¡‘É¥Ù•È±•Ù•¹ÑÌ±¹½Ü¥ì(€¹½Üõ¹½Ýññ…Ñ”¹¹½Ü ¤ì(€É•ÑÕÉ¸€¡•Ù•¹ÑÍññmt¤¹™¥±Ñ•È¡”ôùMÑÉ¥¹œ¡”¹Í•É¥•Íñðœœ¤ôôõMÑÉ¥¹œ¡‘É¥Ù•È¹Í•É¥•Íñðœœ¤˜˜¡‘É¥Ù•È¹Í•É¥•Ì„ôô˜ÄññMÑÉ¥¹œ¡”¹Í•ÍÍ¥½¹ñðœœ¤¹Ñ½1½Ý•É…Í” ¤ôôôÉ…”œ¤¤¹µ…À¡”ôø¡í•Ù•¹Ðé”±ÑÌé¹•Ü…Ñ”¡”¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¥ô¤¤¹™¥±Ñ•È¡àôù9Õµ‰•È¹¥Í¥¹¥Ñ”¡à¹ÑÌ¤˜™à¹ÑÌøõ¹½Ü´Ø¨ÌØÀÀÀÀÀ¤¹Í½ÉÐ ¡„±ˆ¤ôù„¹ÑÌµˆ¹ÑÌ¥lÁtü¹•Ù•¹Ñññ¹Õ±°ì)ô)™Õ¹Ñ¥½¸É…¥¹•Ñ…¥±]¡•¸¡•Ù•¹Ð¥ì(€¥˜ …•Ù•¹Ð¥É•ÑÕÉ¸€œœì(€½¹ÍÐõ¹•Ü…Ñ”¡•Ù•¹Ð¹ÍÑ…ÉÐ¤±±½…±”õ}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•ì(€É•ÑÕÉ¸•Ù•¹Ð¹…±±}‘…äü¡•Ù•¹Ð¹‘…Ñ•}Ñ•áÑññ¹Ñ½1½…±•…Ñ•MÑÉ¥¹œ¡±½…±”±íÝ••­‘…äè±½¹œœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ è±½¹œô¤¤é¹Ñ½1½…±•MÑÉ¥¹œ¡±½…±”±íÝ••­‘…äè±½¹œœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ è±½¹œœ±¡½ÕÈèœÈµ‘¥¥Ðœ±µ¥¹ÕÑ”èœÈµ‘¥¥Ðô¤ì)ô)™Õ¹Ñ¥½¸É…¥¹½Õ¹Ñ‘½Ý¸¡•Ù•¹Ð¥ì(€¥˜ …•Ù•¹Ð¥É•ÑÕÉ¸€œœì(€½¹ÍÐÑ…É•Ðõ¹•Ü…Ñ”¡•Ù•¹Ð¹ÍÑ…ÉÐ¤±¹½Üõ¹•Ü…Ñ” ¤±É•µ…¥¹¥¹œõÑ…É•Ðµ¹½Üí¥˜ …9Õµ‰•È¹¥Í¥¹¥Ñ”¡É•µ…¥¹¥¹œ¤¥É•ÑÕÉ¸€œœì(€¥˜¡É•µ…¥¹¥¹œðôÀ˜™É…¥¹Ù•¹Ñ%Í1¥Ù”¡•Ù•¹Ð±¹½Ü¹•ÑQ¥µ” ¤¤¥É•ÑÕÉ¸€1%Yœì(€¥˜¡É•µ…¥¹¥¹œðôÀ¥É•ÑÕÉ¸€œœì(€½¹ÍÐµ¥¹ÕÑ•Ìõ5…Ñ ¹µ…à Ä±5…Ñ ¹•¥°¡É•µ…¥¹¥¹œ¼ØÀÀÀÀ¤¤ì(€¥˜¡µ¥¹ÕÑ•ÌðØÀ¥É•ÑÕÉ¸ÑÈ ¥¸œ¤¬œ€œ­µ¥¹ÕÑ•Ì¬œ€œ­ÑÈ¡µ¥¹ÕÑ•ÌôôôÄüµ¥¹ÕÑ”œèµ¥¹ÕÑ•Ìœ¤ì(€¥˜¡É•µ…¥¹¥¹œðÈÐ¨ÌØÀÀÀÀÀ¥í½¹ÍÐ¡½ÕÉÌõ5…Ñ ¹•¥°¡µ¥¹ÕÑ•Ì¼ØÀ¤íÉ•ÑÕÉ¸ÑÈ ¥¸œ¤¬œ€œ­¡½ÕÉÌ¬œ€œ­ÑÈ¡¡½ÕÉÌôôôÄü¡½ÕÈœè¡½ÕÉÌœ¤íô(€½¹ÍÐ‘…åÌõ5…Ñ ¹µ…à Ä±5…Ñ ¹É½Õ¹¡½Í±½…å9Õµ‰•È¡Ñ…É•Ð¤µ½Í±½…å9Õµ‰•È¡¹½Ü¤¤¤ì(€É•ÑÕÉ¸ÑÈ ¥¸œ¤¬œ€œ­‘…åÌ¬œ€œ­ÑÈ¡‘…åÌôôôÄü‘…äœè‘…åÌœ¤ì)ô)™Õ¹Ñ¥½¸É…¥¹ÉÑÉÉ½È¡¥µœ¥ì(€½¹ÍÐ™…±±‰…¬õMÑÉ¥¹œ¡¥µœ¹‘…Ñ…Í•Ð¹™…±±‰…­ñðœœ¤ì(€¥˜¡™…±±‰…¬¥í¥µœ¹‘…Ñ…Í•Ð¹™…±±‰…¬ôœœí¥µœ¹ÍÉŒõ™…±±‰…¬íÉ•ÑÕÉ¸íô(€¥µœ¹ÍÑå±”¹‘¥ÍÁ±…äô¹½¹”œí½¹ÍÐµ…É¬õ¥µœ¹¹•áÑ±•µ•¹ÑM¥‰±¥¹œí¥˜¡µ…É¬¥µ…É¬¹ÍÑå±”¹‘¥ÍÁ±…äô™±•àœì)ô)™Õ¹Ñ¥½¸É…¥¹Ù•¹ÑY¥ÍÕ…°¡•Ù•¹Ð±½Õ¹Ñ‘½Ý¸¥ì(€½¹ÍÐÍ•É¥•ÌõMÑÉ¥¹œ ¡•Ù•¹Ð˜™•Ù•¹Ð¹Í•É¥•Ì¥ñðœœ¤±Í•É¥•Í1½¼õMÑÉ¥¹œ¡}I%9}1==MmÍ•É¥•Íuñðœœ¤±•Ù•¹Ñ1½¼ô¡Í•É¥•ÌôôôÝÉŒœýMÑÉ¥¹œ ¡•Ù•¹Ð˜™•Ù•¹Ð¹…ÉÐ¥ñðœœ¤èœœ¤ì(€½¹ÍÐÍÉŒõ•Ù•¹Ñ1½½ññÍ•É¥•Í1½¼ì(€½¹ÍÐ™…±±‰…¬ôœñ‘¥Ø±…ÍÌô‰É…¥¹•Ù•¹Ñ™…±±‰…¬ˆœ¬¡ÍÉŒüœÍÑå±”ô‰‘¥ÍÁ±…äé¹½¹”ˆœèœœ¤¬œøœ­É…¥¹M•É¥•Í1½¼¡Í•É¥•Ì¤¬œð½‘¥Øøœì(€±•Ð¥µ…”ôœœì(€¥˜¡ÍÉŒ¥í½¹ÍÐ™ˆõ•Ù•¹Ñ1½¼ýÍ•É¥•Í1½¼èœœí¥µ…”ôœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ‘…Ñ„µ™…±±‰…¬ôˆœ­•ÍÑÑÈ¡™ˆ¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰É…¥¹ÉÑÉÉ½È¡Ñ¡¥Ì¤ˆøœíô(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰É…¥¹•Ù•¹ÑÙ¥ÍÕ…°ˆøœ­¥µ…”­™…±±‰…¬¬¡½Õ¹Ñ‘½Ý¸üœñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±½Õ¹Ñ‘½Ý¸ˆøœ­•ÍŒ¡½Õ¹Ñ‘½Ý¸¤¬œð½‘¥Øøœèœœ¤¬œð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸É…¥¹•Ñ…¥±9•áÐ¡•Ù•¹Ð¥ì(€¥˜ …•Ù•¹Ð¥É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±¹•áÐˆøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±¹•áÑ±…‰•°ˆøœ­•ÍŒ¡ÑÈ 9•áÐÉ…”œ¤¤¬œð½‘¥ØøñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 9¼ÕÁ½µ¥¹œÉ…”™½Õ¹¸œ¤¤¬œð½ÍÁ…¸øð½‘¥Øøœì(€½¹ÍÐ½Õ¹Ñ‘½Ý¸õÉ…¥¹½Õ¹Ñ‘½Ý¸¡•Ù•¹Ð¤ì(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±¹•áÐˆøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±¹•áÑ±…‰•°ˆøœ­•ÍŒ¡ÑÈ 9•áÐÉ…”œ¤¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±¹•áÑÉ¥ˆøñ‘¥Øøñˆøœ­•ÍŒ¡•Ù•¹Ð¹É…•ññ•Ù•¹Ð¹¥ÉÕ¥ÑñðI…”œ¤¬œð½ˆøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±µ•Ñ„ˆøœ­•ÍŒ¡É…¥¹•Ñ…¥±]¡•¸¡•Ù•¹Ð¤¤¬¡•Ù•¹Ð¹Í•ÍÍ¥½¸üœñ‰Èøœ­•ÍŒ¡•Ù•¹Ð¹Í•ÍÍ¥½¸¤èœœ¤¬¡•Ù•¹Ð¹¥ÉÕ¥Ð˜™•Ù•¹Ð¹¥ÉÕ¥Ð„ôõ•Ù•¹Ð¹É…”üœñ‰Èøœ­•ÍŒ¡•Ù•¹Ð¹¥ÉÕ¥Ð¤èœœ¤¬œð½‘¥Øøð½‘¥Øøœ­É…¥¹Ù•¹ÑY¥ÍÕ…°¡•Ù•¹Ð±½Õ¹Ñ‘½Ý¸¤¬œð½‘¥Øøð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸É…¥¹M•É¥•Í1½¼¡­•ä¥í½¹ÍÐÍÉŒõMÑÉ¥¹œ¡}I%9}1==Mm­•åuñðœœ¤íÉ•ÑÕÉ¸ÍÉŒüœñ¥µœ±…ÍÌô‰É…¥¹Í•É¥•Í±½¼ˆÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœíô)™Õ¹Ñ¥½¸É•¹‘•ÉI…¥¹Q•…µ½¹ÑÉ½° ¥ì(€½¹ÍÐ½¹ÑÉ½°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹ÅQ•…µ½¹ÑÉ½°œ¤±±…‰•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹Q•…µ1…‰•°œ¤±‰Ñ¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹Å¡½½Í•	Ñ¸œ¤±Á¥­•Èõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹ÅA¥­•Èœ¤í¥˜ …½¹ÑÉ½±ñð…±…‰•±ñð…‰Ñ¸¥É•ÑÕÉ¸ì(€½¹ÍÐ˜Äõ}É…¥¹É¥Ù•ÉI½ÝÌ¹™¥±Ñ•È¡‘É¥Ù•Èôù‘É¥Ù•È¹Í•É¥•Ìôôô˜Äœ¤±˜Å5½‘”õ}É…¥¹•Ñ…¥±-•äôôô˜ÄµÑ•…´ñð¡}É…¥¹É¥Ù•ÉI½ÝÌ¹™¥¹¡É½ÜôùMÑÉ¥¹œ¡É½Ü¹­•åñðœœ¤ôôõMÑÉ¥¹œ¡}É…¥¹•Ñ…¥±-•åñðœœ¤¥ññíô¤¹Í•É¥•Ìôôô˜Äœì(€¥˜¡˜Å5½‘”¥ì(€€€½¹ÍÐ‘É¥Ù•Èõ˜ÅlÁuññíô±Ñ•…µ%õMÑÉ¥¹œ¡‘É¥Ù•È¹Ñ•…µ}¥‘ñðœœ¤í½¹ÑÉ½°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤í±…‰•°¹Ñ•áÑ½¹Ñ•¹Ðô½ÉµÕ±„€ÄQ•…´œí‰Ñ¸¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” É•…‘½¹±äœ¤í‰Ñ¸¹Í•ÑÑÑÉ¥‰ÕÑ” ½¹±¥¬œ°Ñ½±•I…¥¹ÅA¥­•È ¤œ¤ì(€€€‰Ñ¸¹¥¹¹•É!Q50õ‘É¥Ù•È¹Ñ•…´ü ¡Ñ•…µ%üœñ¥µœÍÉŒôˆ½…Á¤½˜Å}Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Ñ•…µ%¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñÍÁ…¸øœ­•ÍŒ¡‘É¥Ù•È¹Ñ•…´¤¬œð½ÍÁ…¸øœ¤èœñÍÁ…¸øœ­•ÍŒ¡ÑÈ ¡½½Í”ÄÑ•…´œ¤¤¬œð½ÍÁ…¸øœì(€€€É•ÑÕÉ¸ì(€ô(€½¹ÍÐ‘É¥Ù•Èõ}É…¥¹É¥Ù•ÉI½ÝÌ¹™¥¹¡É½ÜôùMÑÉ¥¹œ¡É½Ü¹­•åñðœœ¤ôôõMÑÉ¥¹œ¡}É…¥¹•Ñ…¥±-•åñðœœ¤¤ì(€¥˜ …‘É¥Ù•È¥í½¹ÑÉ½°¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íÉ•ÑÕÉ¸íô(€½¹ÑÉ½°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤í±…‰•°¹Ñ•áÑ½¹Ñ•¹Ðô¡‘É¥Ù•È¹Í•É¥•Í}¹…µ•ñðI…¥¹œœ¤¬œQ•…´œí‰Ñ¸¹±…ÍÍ1¥ÍÐ¹…‘ É•…‘½¹±äœ¤í‰Ñ¸¹É•µ½Ù•ÑÑÉ¥‰ÕÑ” ½¹±¥¬œ¤í¥˜¡Á¥­•È¥Á¥­•È¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€½¹ÍÐ±½¼õMÑÉ¥¹œ¡‘É¥Ù•È¹Ñ•…µ}±½½ñðœœ¤í‰Ñ¸¹¥¹¹•É!Q50ô¡±½¼üœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡±½¼¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñÍÁ…¸øœ­•ÍŒ¡‘É¥Ù•È¹Ñ•…µññ‘É¥Ù•È¹Í•É¥•Í}¹…µ•ñðI…¥¹œœ¤¬œð½ÍÁ…¸øœì)ô)™Õ¹Ñ¥½¸É•¹‘•ÉI…¥¹É¥Ù•É•Ñ…¥° ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹É¥Ù•É•Ñ…¥°œ¤í¥˜ …•°¥É•ÑÕÉ¸ì(€½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤±˜Äõ}É…¥¹É¥Ù•ÉI½ÝÌ¹™¥±Ñ•È¡‘É¥Ù•Èôù‘É¥Ù•È¹Í•É¥•Ìôôô˜Äœ¤ì(€¥˜¡}É…¥¹•Ñ…¥±-•äôôô˜ÄµÑ•…´œ˜™˜Ä¹±•¹Ñ øôÈ¥ì(€€€½¹ÍÐ™¥ÉÍÐõ˜ÅlÁt±Ñ•…µ%õMÑÉ¥¹œ¡™¥ÉÍÐ¹Ñ•…µ}¥‘ñðœœ¤±±¥Ù”õ}É…¥¹Ù•¹ÑI½ÝÌ¹™¥±Ñ•È¡”ôù”¹Í•É¥•Ìôôô˜Äœ¤¹Í½µ”¡”ôùÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡”±¹½Ü¤¤±¹•áÐõ¹•áÑÉ¥Ù•ÉI…”¡™¥ÉÍÐ±}É…¥¹Ù•¹ÑI½ÝÌ±¹½Ü¤ì(€€€½¹ÍÐÁ•½Á±”õ˜Ä¹Í±¥” À°È¤¹µ…À¡‘É¥Ù•Èôøœñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±Á•ÉÍ½¸ˆøñ¥µœÍÉŒôˆ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøñ‘¥Øøñˆøœ­•ÍŒ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œð½ˆøœ¬¡‘É¥Ù•È¹ÕÉ°üœñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆøñ„¡É•˜ôˆœ­•ÍÑÑÈ¡‘É¥Ù•È¹ÕÉ°¤¬œˆÑ…É•Ðô‰}‰±…¹¬ˆÉ•°ô‰¹½½Á•¹•È¹½É•™•ÉÉ•Èˆøœ­•ÍŒ¡ÑÈ É¥Ù•ÈÁÉ½™¥±”œ¤¤¬œƒŠ\ð½„øð½‘¥Øøœèœœ¤¬œð½‘¥Øøð½‘¥Øøœ¤¹©½¥¸ œœ¤ì(€€€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±˜Å¡•É¼ˆøœ¬¡Ñ•…µ%üœñ¥µœÍÉŒôˆ½…Á¤½˜Å}Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Ñ•…µ%¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñ‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±Í•É¥•Ìˆù½ÉµÕ±„€Äœ¬¡±¥Ù”üœƒ
Ü1%Yœèœœ¤¬œð½‘¥Øøñ Èøœ­•ÍŒ¡™¥ÉÍÐ¹Ñ•…µñð½ÉµÕ±„€Äœ¤¬œð½ Èøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±Ñ•…´ˆøœ­•ÍŒ¡˜Ä¹Í±¥” À°È¤¹µ…À¡ôù¹¹…µ”¤¹©½¥¸ œƒ
Ü€œ¤¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±Á•½Á±”ˆøœ­Á•½Á±”¬œð½‘¥Øøœ­É…¥¹•Ñ…¥±9•áÐ¡¹•áÐ¤ì(€€€É•ÑÕÉ¸ì(€ô(€½¹ÍÐ‘É¥Ù•Èõ}É…¥¹É¥Ù•ÉI½ÝÌ¹™¥¹¡É½ÜôùMÑÉ¥¹œ¡É½Ü¹­•åñðœœ¤ôôõMÑÉ¥¹œ¡}É…¥¹•Ñ…¥±-•åñðœœ¤¤ì(€¥˜ …‘É¥Ù•È¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ ¡½½Í”„‘É¥Ù•ÈÑ¼Í•”‘•Ñ…¥±Ì¸œ¤¤¬œð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€½¹ÍÐ±¥Ù”õ}É…¥¹Ù•¹ÑI½ÝÌ¹™¥±Ñ•È¡”ôùMÑÉ¥¹œ¡”¹Í•É¥•Íñðœœ¤ôôõMÑÉ¥¹œ¡‘É¥Ù•È¹Í•É¥•Íñðœœ¤¤¹Í½µ”¡”ôùÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡”±¹½Ü¤¤±¹•áÐõ¹•áÑÉ¥Ù•ÉI…”¡‘É¥Ù•È±}É…¥¹Ù•¹ÑI½ÝÌ±¹½Ü¤±…Èõ‘É¥Ù•È¹Í•É¥•Ìôôô˜Èœì(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±¡•É¼ˆøñ¥µœ±…ÍÌôˆœ¬¡…Èü…Èœèœœ¤¬œˆÍÉŒôˆ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤¤¬œˆ…±Ðôˆœ­•ÍÑÑÈ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøñ‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±Í•É¥•Ìˆøœ­•ÍŒ¡‘É¥Ù•È¹Í•É¥•Í}¹…µ•ñðI…¥¹œœ¤¬¡±¥Ù”üœƒ
Ü1%Yœèœœ¤¬œð½‘¥Øøñ Èøœ­•ÍŒ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œð½ Èøñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±Ñ•…´ˆøœ­•ÍŒ¡‘É¥Ù•È¹Ñ•…µñðœœ¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœ­É…¥¹•Ñ…¥±9•áÐ¡¹•áÐ¤¬¡‘É¥Ù•È¹ÕÉ°üœñ‘¥Ø±…ÍÌô‰É…¥¹‘•Ñ…¥±…Ñ¥½¹Ìˆøñ„±…ÍÌô‰¡½ÍÐˆ¡É•˜ôˆœ­•ÍÑÑÈ¡‘É¥Ù•È¹ÕÉ°¤¬œˆÑ…É•Ðô‰}‰±…¹¬ˆÉ•°ô‰¹½½Á•¹•È¹½É•™•ÉÉ•Èˆøœ­•ÍŒ¡ÑÈ É¥Ù•ÈÁÉ½™¥±”œ¤¤¬œƒŠ\ð½„øð½‘¥Øøœèœœ¤ì)ô)™Õ¹Ñ¥½¸Í¡½ÝI…¥¹É¥Ù•É•Ñ…¥°¡­•ä¥ì(€}É…¥¹•Ñ…¥±-•äõMÑÉ¥¹œ¡­•åñðœœ¤íÉ•¹‘•ÉI…¥¹Q•…µ½¹ÑÉ½° ¤íÉ•¹‘•ÉI…¥¹É¥Ù•É•Ñ…¥° ¤ì(€½¹ÍÐ‘É¥Ù•ÉÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹É¥Ù•ÉÌœ¤í¥˜¡‘É¥Ù•ÉÌ¥‘É¥Ù•ÉÌ¹¥¹¹•É!Q50õÉ…¥¹É¥Ù•ÉÍ!Ñµ°¡}É…¥¹É¥Ù•ÉI½ÝÌ±}É…¥¹Ù•¹ÑI½ÝÌ¤ì(€É•¹‘•ÉI…¥¹M¡•‘Õ±•…É‘Ì ¤ì)ô)™Õ¹Ñ¥½¸É…¥¹É¥Ù•É!Ñµ°¡‘É¥Ù•È±•Ù•¹ÑÌ¥ì(€½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤±Í•É¥•ÍÙ•¹ÑÌô¡•Ù•¹ÑÍññmt¤¹™¥±Ñ•È¡”ôùMÑÉ¥¹œ¡”¹Í•É¥•Íñðœœ¤ôôõMÑÉ¥¹œ¡‘É¥Ù•È¹Í•É¥•Íñðœœ¤¤±±¥Ù”õÍ•É¥•ÍÙ•¹ÑÌ¹Í½µ”¡”ôùÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡”±¹½Ü¤¤±¹•áÐõ¹•áÑÉ¥Ù•ÉI…”¡‘É¥Ù•È±•Ù•¹ÑÌ±¹½Ü¤±±½…±”õ}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•ì(€±•Ð¹•áÑ!Ñµ°ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ 9¼ÕÁ½µ¥¹œÉ…”™½Õ¹¸œ¤¬œð½ÍÁ…¸øœì(€¥˜¡¹•áÐ¥í½¹ÍÐõ¹•Ü…Ñ”¡¹•áÐ¹ÍÑ…ÉÐ¤±Ý¡•¸õ¹•áÐ¹…±±}‘…äü¡¹•áÐ¹‘…Ñ•}Ñ•áÑññ¹Ñ½1½…±•…Ñ•MÑÉ¥¹œ¡±½…±”±í‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐô¤¤é¹Ñ½1½…±•MÑÉ¥¹œ¡±½…±”±íÝ••­‘…äèÍ¡½ÉÐœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐœ±¡½ÕÈèœÈµ‘¥¥Ðœ±µ¥¹ÕÑ”èœÈµ‘¥¥Ðô¤í¹•áÑ!Ñµ°ôœñÍÁ…¸øœ­ÑÈ 9•áÐÉ…”œ¤¬œð½ÍÁ…¸øñˆøœ­•ÍŒ¡¹•áÐ¹É…•ññ¹•áÐ¹¥ÉÕ¥ÑñðI…”œ¤¬œð½ˆøñÍÁ…¸øœ­•ÍŒ¡Ý¡•¸¤¬œð½ÍÁ…¸øœíô(€½¹ÍÐ­•äõMÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤±Í•±•Ñ•õ}É…¥¹•Ñ…¥±-•äôôõ­•äüœÍ•±•Ñ•œèœœì(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•Èœ­Í•±•Ñ•¬œˆ‘…Ñ„µ‘É¥Ù•Èµ­•äôˆœ­•ÍÑÑÈ¡­•ä¤¬œˆ½¹±¥¬ô‰Í¡½ÝI…¥¹É¥Ù•É•Ñ…¥°¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹‘É¥Ù•É-•ä¤ˆøñ¥µœÍÉŒôˆ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡­•ä¤¬œˆ…±Ðôˆœ­•ÍÑÑÈ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœ(€€€€¬œñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•É¥¹™¼ˆøñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•É¹…µ”ˆøœ­•ÍŒ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬¡±¥Ù”üœñÍÁ…¸±…ÍÌô‰‘É¥Ù•É±¥Ù”ˆù1%Yð½ÍÁ…¸øœèœœ¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•ÉÑ•…´ˆøœ­•ÍŒ¡‘É¥Ù•È¹Í•É¥•Í}¹…µ•ñðœœ¤¬¡‘É¥Ù•È¹Ñ•…´üœƒ
Ü€œ­•ÍŒ¡‘É¥Ù•È¹Ñ•…´¤èœœ¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•É¹•áÐˆøœ­¹•áÑ!Ñµ°¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸É…¥¹ÅA…¥É!Ñµ°¡Á…¥È±•Ù•¹ÑÌ¥ì(€½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤±™¥ÉÍÐõÁ…¥ÉlÁt±Í•É¥•ÍÙ•¹ÑÌô¡•Ù•¹ÑÍññmt¤¹™¥±Ñ•È¡”ôùMÑÉ¥¹œ¡”¹Í•É¥•Íñðœœ¤ôôô˜Äœ¤±±¥Ù”õÍ•É¥•ÍÙ•¹ÑÌ¹Í½µ”¡”ôùÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡”±¹½Ü¤¤±¹•áÐõ¹•áÑÉ¥Ù•ÉI…”¡™¥ÉÍÐ±•Ù•¹ÑÌ±¹½Ü¤±±½…±”õ}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•ì(€±•Ð¹•áÑ!Ñµ°ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ 9¼ÕÁ½µ¥¹œÉ…”™½Õ¹¸œ¤¬œð½ÍÁ…¸øœì(€¥˜¡¹•áÐ¥í½¹ÍÐõ¹•Ü…Ñ”¡¹•áÐ¹ÍÑ…ÉÐ¤±Ý¡•¸õ¹Ñ½1½…±•MÑÉ¥¹œ¡±½…±”±íÝ••­‘…äèÍ¡½ÉÐœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐœ±¡½ÕÈèœÈµ‘¥¥Ðœ±µ¥¹ÕÑ”èœÈµ‘¥¥Ðô¤í¹•áÑ!Ñµ°ôœñÍÁ…¸øœ­ÑÈ 9•áÐÉ…”œ¤¬œð½ÍÁ…¸øñˆøœ­•ÍŒ¡¹•áÐ¹É…•ññ¹•áÐ¹¥ÉÕ¥ÑñðI…”œ¤¬œð½ˆøñÍÁ…¸øœ­•ÍŒ¡Ý¡•¸¤¬œð½ÍÁ…¸øœíô(€½¹ÍÐÁ¥ÌõÁ…¥È¹µ…À¡‘É¥Ù•Èôøœñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•ÉÁ…¥ÉÁ•ÉÍ½¸ˆøñ¥µœÍÉŒôˆ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤¤¬œˆ…±Ðôˆœ­•ÍÑÑÈ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøñÍÁ…¸øœ­•ÍŒ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œð½ÍÁ…¸øð½‘¥Øøœ¤¹©½¥¸ œœ¤ì(€½¹ÍÐ¹…µ•ÌõÁ…¥È¹µ…À¡‘É¥Ù•Èôù‘É¥Ù•È¹¹…µ•ñðœœ¤¹™¥±Ñ•È¡	½½±•…¸¤¹©½¥¸ œƒ
Ü€œ¤±Ñ•…´õ™¥ÉÍÐ¹Ñ•…µñð½ÉµÕ±„€Äœì(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•ÈÉ…¥¹‘É¥Ù•ÉÁ…¥Èœ¬¡}É…¥¹•Ñ…¥±-•äôôô˜ÄµÑ•…´œüœÍ•±•Ñ•œèœœ¤¬œˆ‘…Ñ„µ‘É¥Ù•Èµ­•äô‰˜ÄµÑ•…´ˆ½¹±¥¬ô‰Í¡½ÝI…¥¹É¥Ù•É•Ñ…¥°¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹‘É¥Ù•É-•ä¤ˆøñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•ÉÁ…¥ÉÁ¥Ìˆøœ­Á¥Ì¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•É¥¹™¼ˆøñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•É¹…µ”ˆøœ­•ÍŒ¡Ñ•…´¤¬¡±¥Ù”üœñÍÁ…¸±…ÍÌô‰‘É¥Ù•É±¥Ù”ˆù1%Yð½ÍÁ…¸øœèœœ¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•ÉÑ•…´ˆù½ÉµÕ±„€Äƒ
Ü€œ­•ÍŒ¡¹…µ•Ì¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰É…¥¹‘É¥Ù•É¹•áÐˆøœ­¹•áÑ!Ñµ°¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸É…¥¹É¥Ù•ÉÍ!Ñµ°¡É½ÝÌ±•Ù•¹ÑÌ¥ì(€½¹ÍÐ±¥ÍÐõÉ½ÝÍññmt±˜Äõ±¥ÍÐ¹™¥±Ñ•È¡‘É¥Ù•Èôù‘É¥Ù•È¹Í•É¥•Ìôôô˜Äœ¤±½Ñ¡•Èõ±¥ÍÐ¹™¥±Ñ•È¡‘É¥Ù•Èôù‘É¥Ù•È¹Í•É¥•Ì„ôô˜Äœ¤±Á…ÉÑÌõmtì(€¥˜¡˜Ä¹±•¹Ñ øôÈ¥Á…ÉÑÌ¹ÁÕÍ ¡í­•äè˜ÄµÑ•…´œ±¡Ñµ°éÉ…¥¹ÅA…¥É!Ñµ°¡˜Ä¹Í±¥” À°È¤±•Ù•¹ÑÌ¥ô¤ì(€•±Í”™½È¡½¹ÍÐ‘É¥Ù•È½˜˜Ä¥Á…ÉÑÌ¹ÁÕÍ ¡í­•äéMÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤±¡Ñµ°éÉ…¥¹É¥Ù•É!Ñµ°¡‘É¥Ù•È±•Ù•¹ÑÌ¥ô¤ì(€™½È¡½¹ÍÐ‘É¥Ù•È½˜½Ñ¡•È¥Á…ÉÑÌ¹ÁÕÍ ¡í­•äéMÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤±¡Ñµ°éÉ…¥¹É¥Ù•É!Ñµ°¡‘É¥Ù•È±•Ù•¹ÑÌ¥ô¤ì(€Á…ÉÑÌ¹Í½ÉÐ ¡„±ˆ¤ôø¡„¹­•äôôõMÑÉ¥¹œ¡}É…¥¹•Ñ…¥±-•åñðœœ¤ü´ÄèÀ¤´¡ˆ¹­•äôôõMÑÉ¥¹œ¡}É…¥¹•Ñ…¥±-•åñðœœ¤ü´ÄèÀ¤¤ì(€É•ÑÕÉ¸Á…ÉÑÌ¹µ…À¡Á…ÉÐôùÁ…ÉÐ¹¡Ñµ°¤¹©½¥¸ œœ¤ì)ô)™Õ¹Ñ¥½¸É…¥¹¡…¹¹•±1¥¹”¡ ¥ì(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰É…¥¹•Ù•¹Ñ¡…¹¹•°ˆøœ­¡…¹¹•±1½¼¡ °µ¥¹¤œ¤¬œñÍÁ…¸±…ÍÌô‰¡¸ˆøœ­•ÍŒ¡ ¹áÑÉ•…µ}¹…µ•ñð¡…¹¹•°œ¤¬¡ ¹ÅÕ…±¥ÑäüœñÍÁ…¸±…ÍÌô‰Ñ…œˆøœ­•ÍŒ¡ ¹ÅÕ…±¥Ñä¤¬œð½ÍÁ…¸øœèœœ¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰¡‰Ñ¹Ìˆøœ­Á±…å‰Ñ¹Ì¡ ¹ÍÑÉ•…µ}¥± ¹áÑÉ•…µ}¹…µ”± ¹ÕÉ°¤¬œð½ÍÁ…¸øð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸É…¥¹¡…¹¹•±M•Ñ¥½¹Ì¡¡…¹¹•±Ì¥ì(€½¹ÍÐ‘•™¥¹¥Ñ”õ¡…¹¹•±Ì¹™¥±Ñ•È¡ ôù ¹µ…Ñ¡}­¥¹ôôô•Ù•¹Ðœ¤¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤±‘•‘¥…Ñ•õ¡…¹¹•±Ì¹™¥±Ñ•È¡ ôù ¹µ…Ñ¡}­¥¹ôôôÍ•É¥•Ìœ¤¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤±Á½ÍÍ¥‰±”õ¡…¹¹•±Ì¹™¥±Ñ•È¡ ôù ¹µ…Ñ¡}­¥¹„ôô•Ù•¹Ðœ˜™ ¹µ…Ñ¡}­¥¹„ôôÍ•É¥•Ìœ¤¹Í½ÉÐ¡ÁÉ•™•ÉÉ•‘¡…¹¹•±M½ÉÐ¤ì(€±•Ð ôœœì(€¥˜¡‘•™¥¹¥Ñ”¹±•¹Ñ ¥ ¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ •™¥¹¥Ñ”•Ù•¹Ðµ…Ñ¡•Ìœ¤¤¬œð½‘¥Øøœ­‘•™¥¹¥Ñ”¹µ…À¡É…¥¹¡…¹¹•±1¥¹”¤¹©½¥¸ œœ¤ì(€¥˜¡‘•‘¥…Ñ•¹±•¹Ñ ¥ ¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèáÁàˆøœ­•ÍŒ¡ÑÈ •‘¥…Ñ•Í•É¥•Ì¡…¹¹•±Ìœ¤¤¬œð½‘¥Øøœ­‘•‘¥…Ñ•¹µ…À¡É…¥¹¡…¹¹•±1¥¹”¤¹©½¥¸ œœ¤ì(€¥˜¡Á½ÍÍ¥‰±”¹±•¹Ñ ¥ì(€€€½¹ÍÐÉ½ÕÁÌõ¹•Ü5…À ¤í™½È¡½¹ÍÐ ½˜Á½ÍÍ¥‰±”¥í½¹ÍÐ…Ñ•½ÉäõMÑÉ¥¹œ¡ ¹…Ñ•½ÉåññÑÈ =Ñ¡•ÈÁ½ÍÍ¥‰±”¡…¹¹•±Ìœ¤¤í¥˜ …É½ÕÁÌ¹¡…Ì¡…Ñ•½Éä¤¥É½ÕÁÌ¹Í•Ð¡…Ñ•½Éä±mt¤íÉ½ÕÁÌ¹•Ð¡…Ñ•½Éä¤¹ÁÕÍ ¡ ¤íô(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèáÁàˆøœ­•ÍŒ¡ÑÈ A½ÍÍ¥‰±”¡…¹¹•±Ì‰ä…Ñ•½Éäœ¤¤¬œð½‘¥Øøœì(€€€™½È¡½¹ÍÐm…Ñ•½Éä±¥Ñ•µÍt½˜É½ÕÁÌ¥ ¬ôœñ‘¥Ø±…ÍÌô‰‰É½Üˆøñ‘¥Ø±…ÍÌô‰‰¡•…ˆøñÍÁ…¸±…ÍÌô‰‰¹…µ”ˆøœ­•ÍŒ¡…Ñ•½Éä¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­¥Ñ•µÌ¹±•¹Ñ ¬œ€œ­•ÍŒ¡ÑÈ¡¥Ñ•µÌ¹±•¹Ñ ôôôÄü¡…¹¹•°œè¡…¹¹•±Ìœ¤¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰‰¡•ÙÉ½¸ˆø˜ŒäØØÈìð½ÍÁ…¸øð½‘¥Øøñ‘¥Ø±…ÍÌô‰‰¡…¹Ì¡¥‘”ˆøœ­¥Ñ•µÌ¹µ…À¡É…¥¹¡…¹¹•±1¥¹”¤¹©½¥¸ œœ¤¬œð½‘¥Øøð½‘¥Øøœì(€ô(€É•ÑÕÉ¸ ì)ô)™Õ¹Ñ¥½¸É…¥¹Ù•¹Ñ!Ñµ°¡•Ù•¹Ð¥ì(€½¹ÍÐÑÌõ¹•Ü…Ñ”¡•Ù•¹Ð¹ÍÑ…ÉÐ¤±±½…±”õ}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•ì(€½¹ÍÐÝ¡•¸õ•Ù•¹Ð¹…±±}‘…äü¡•Ù•¹Ð¹‘…Ñ•}Ñ•áÑññÑÌ¹Ñ½1½…±•…Ñ•MÑÉ¥¹œ¡±½…±”±íÝ••­‘…äèÍ¡½ÉÐœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐô¤¤éÑÌ¹Ñ½1½…±•MÑÉ¥¹œ¡±½…±”±íÝ••­‘…äèÍ¡½ÉÐœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐœ±¡½ÕÈèœÈµ‘¥¥Ðœ±µ¥¹ÕÑ”èœÈµ‘¥¥Ðô¤ì(€½¹ÍÐ¡…¹¹•±Ìõ•Ù•¹Ð¹¡…¹¹•±Íññmt±ÑØõ¡…¹¹•±Ì¹±•¹Ñ üœñÍÁ…¸±…ÍÌô‰ŒÉ…¥¹•Ù•¹ÑÑØˆùQXð½ÍÁ…¸øœèœœ±‘•Ñ…¥±Ìõ¡…¹¹•±Ì¹±•¹Ñ üœñ‘¥Ø±…ÍÌô‰É…¥¹•Ù•¹Ñ¡…¹¹•±Ì¡¥‘”ˆøœ­É…¥¹¡…¹¹•±M•Ñ¥½¹Ì¡¡…¹¹•±Ì¤¬œð½‘¥Øøœèœœì(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰É…¥¹•Ù•¹Ðœ¬¡¡…¹¹•±Ì¹±•¹Ñ üœ¡…Í¡…¹¹•±Ìœèœœ¤¬œˆ‘…Ñ„µÕÉ°ôˆœ­•ÍÑÑÈ¡•Ù•¹Ð¹ÕÉ±ñðœœ¤¬œˆøñ‘¥Ø±…ÍÌô‰É…¥¹•Ù•¹ÑÑ½Àˆøñˆøœ­•ÍŒ¡•Ù•¹Ð¹É…•ññ•Ù•¹Ð¹¥ÉÕ¥ÑñðI…”œ¤¬œð½ˆøœ­ÑØ¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆøœ­•ÍŒ¡Ý¡•¸¤¬¡•Ù•¹Ð¹Í•ÍÍ¥½¸üœƒ
Ü€œ­•ÍŒ¡•Ù•¹Ð¹Í•ÍÍ¥½¸¤èœœ¤¬¡•Ù•¹Ð¹¥ÉÕ¥Ð˜™•Ù•¹Ð¹¥ÉÕ¥Ð„ôõ•Ù•¹Ð¹É…”üœƒ
Ü€œ­•ÍŒ¡•Ù•¹Ð¹¥ÉÕ¥Ð¤èœœ¤¬œð½‘¥Øøœ­‘•Ñ…¥±Ì¬œð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸É…¥¹Ù…¥±…‰¥±¥Ñå-•ä¡•Ù•¹Ð¥íÉ•ÑÕÉ¸m•Ù•¹Ð¹Í•É¥•Íñðœœ±•Ù•¹Ð¹É…•ñðœœ±•Ù•¹Ð¹Í•ÍÍ¥½¹ñðœœ±•Ù•¹Ð¹ÍÑ…ÉÑñðœt¹©½¥¸ ðœ¤íô)™Õ¹Ñ¥½¸…ÁÁ±åI…¥¹Ù…¥±…‰¥±¥Ñä¡µ…À±•Ù•¹ÑÌ¥í™½È¡½¹ÍÐ•Ù•¹Ð½˜€¡•Ù•¹ÑÍññmt¤¥•Ù•¹Ð¹¡…¹¹•±Ìô¡µ…À˜™µ…ÁmÉ…¥¹Ù…¥±…‰¥±¥Ñå-•ä¡•Ù•¹Ð¥t¥ññmtíô)™Õ¹Ñ¥½¸É•¹‘•ÉI…¥¹M¡•‘Õ±•…É‘Ì ¥ì(€½¹ÍÐ¥¹™¼õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹%¹™¼œ¤í¥˜ …¥¹™¼¥É•ÑÕÉ¸í½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤±É½ÕÁÌõ¹•Ü5…À ¤ì(€½¹ÍÐÍ•±•Ñ•‘É¥Ù•Èõ}É…¥¹É¥Ù•ÉI½ÝÌ¹™¥¹¡É½ÜôùMÑÉ¥¹œ¡É½Ü¹­•åñðœœ¤ôôõMÑÉ¥¹œ¡}É…¥¹•Ñ…¥±-•åñðœœ¤¤ì(€½¹ÍÐÍ•±•Ñ•‘M•É¥•Ìõ}É…¥¹•Ñ…¥±-•äôôô˜ÄµÑ•…´œü˜ÄœéMÑÉ¥¹œ ¡Í•±•Ñ•‘É¥Ù•È˜™Í•±•Ñ•‘É¥Ù•È¹Í•É¥•Ì¥ñðœœ¤ì(€™½È¡½¹ÍÐ•Ù•¹Ð½˜}É…¥¹Ù•¹ÑI½ÝÌ¥í½¹ÍÐÑÌõ¹•Ü…Ñ”¡•Ù•¹Ð¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤í¥˜ …9Õµ‰•È¹¥Í¥¹¥Ñ”¡ÑÌ¥ññÑÌñ¹½Ü´ÈÐ¨ÌØÀÀÀÀÀ¥½¹Ñ¥¹Õ”í½¹ÍÐ­•äõ•Ù•¹Ð¹Í•É¥•ÍñðÉ…¥¹œœí¥˜ …É½ÕÁÌ¹¡…Ì¡­•ä¤¥É½ÕÁÌ¹Í•Ð¡­•ä±mt¤íÉ½ÕÁÌ¹•Ð¡­•ä¤¹ÁÕÍ ¡•Ù•¹Ð¤íô(€±•Ð ôœœí½¹ÍÐ½É‘•É•‘M•É¥•Ìõ}I%9}MI%L¹™¥±Ñ•È¡É½Üôù}É…¥¹M•±•Ñ•¹¡…Ì¡É½ÝlÁt¤¤¹Í½ÉÐ ¡„±ˆ¤ôø¡…lÁtôôõÍ•±•Ñ•‘M•É¥•Ìü´ÄèÀ¤´¡‰lÁtôôõÍ•±•Ñ•‘M•É¥•Ìü´ÄèÀ¤¤ì(€™½È¡½¹ÍÐÉ½Ü½˜½É‘•É•‘M•É¥•Ì¥í½¹ÍÐ•Ù•¹ÑÌô¡É½ÕÁÌ¹•Ð¡É½ÝlÁt¥ññmt¤¹Í±¥” À°Ð¤í ¬ôœñ‘¥Ø±…ÍÌô‰É…¥¹…ÉÍ•É¥•Ì´œ­•ÍÑÑÈ¡É½ÝlÁt¤¬¡Í•±•Ñ•‘M•É¥•ÌôôõÉ½ÝlÁtüœÍ•±•Ñ•œèœœ¤¬œˆøñ Ìøœ­É…¥¹M•É¥•Í1½¼¡É½ÝlÁt¤¬œñÍÁ…¸øœ­•ÍŒ¡É½ÝlÅt¤¬œð½ÍÁ…¸øð½ Ìøœ¬¡•Ù•¹ÑÌ¹±•¹Ñ ý•Ù•¹ÑÌ¹µ…À¡É…¥¹Ù•¹Ñ!Ñµ°¤¹©½¥¸ œœ¤èœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 9¼ÕÁ½µ¥¹œ•Ù•¹ÑÌ™½Õ¹¸œ¤¤¬œð½ÍÁ…¸øœ¤¬œð½‘¥Øøœíô(€¥¹™¼¹¥¹¹•É!Q50õ¡ñðœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ ¡½½Í”…Ð±•…ÍÐ½¹”É…¥¹œÍ•É¥•Ì…‰½Ù”¸œ¤¤¬œð½ÍÁ…¸øœì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘I…¥¹Ù…¥±…‰¥±¥Ñä ¥ì(€ÑÉåí½¹ÍÐ„õ…Ý…¥Ð…Á¤ œ½…Á¤½É…¥¹}…Ù…¥±…‰¥±¥Ñäœ¤í…ÁÁ±åI…¥¹Ù…¥±…‰¥±¥Ñä¡„¹…Ù…¥±…‰¥±¥Ñåññíô±}É…¥¹Ù•¹ÑI½ÝÌ¤íÉ•¹‘•ÉI…¥¹M¡•‘Õ±•…É‘Ì ¤íÉ•¹‘•ÉI…¥¹É¥Ù•É•Ñ…¥° ¤í½¹ÍÐ‘É¥Ù•ÉÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹É¥Ù•ÉÌœ¤í¥˜¡‘É¥Ù•ÉÌ¥‘É¥Ù•ÉÌ¹¥¹¹•É!Q50õÉ…¥¹É¥Ù•ÉÍ!Ñµ°¡}É…¥¹É¥Ù•ÉI½ÝÌ±}É…¥¹Ù•¹ÑI½ÝÌ¤íõ…Ñ ¡”¥íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘I…¥¹œ ¥ì(€½¹ÍÐÑ½±•Ìõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹M•É¥•Ìœ¤±¥¹™¼õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹%¹™¼œ¤±‘É¥Ù•ÉÌõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹É¥Ù•ÉÌœ¤ì(€¥˜¡‘É¥Ù•ÉÌ¥‘É¥Ù•ÉÌ¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 1½…‘¥¹œ‘É¥Ù•ÉÌ…¹¹•áÐÉ…”¸¸¸œ¤¤¬œð½ÍÁ…¸øœì(€¥˜¡¥¹™¼¥¥¹™¼¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 1½…‘¥¹œÉ…¥¹œÍ¡•‘Õ±•Ì¸¸¸œ¤¤¬œð½ÍÁ…¸øœì(€ÑÉåì(€€€½¹ÍÐmÈ±‘tõ…Ý…¥ÐAÉ½µ¥Í”¹…±°¡m…Á¤ œ½…Á¤½É…¥¹œœ¤±…Á¤ œ½…Á¤½É…¥¹}‘É¥Ù•ÉÌœ¥t¤í}É…¥¹M•±•Ñ•õ¹•ÜM•Ð¡È¹Í•±•Ñ•‘ññmt¤í}É…¥¹É¥Ù•ÉI½ÝÌõ¹‘É¥Ù•ÉÍññmtí}É…¥¹Ù•¹ÑI½ÝÌõÈ¹•Ù•¹ÑÍññmtì(€€€Ñ½±•Ì¹¥¹¹•É!Q50õ}I%9}MI%L¹µ…À¡É½Üôøœñ‰ÕÑÑ½¸±…ÍÌô‰É…¥¹Ñ½±”œ¬¡}É…¥¹M•±•Ñ•¹¡…Ì¡É½ÝlÁt¤üœ½¸œèœœ¤¬œˆ‘…Ñ„µ­•äôˆœ­É½ÝlÁt¬œˆ½¹±¥¬ô‰Ñ½±•I…¥¹M•É¥•Ì¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹­•ä¤ˆøœ­•ÍŒ¡É½ÝlÅt¤¬œð½‰ÕÑÑ½¸øœ¤¹©½¥¸ œœ¤ì(€€€½¹ÍÐ˜ÅI½ÝÌõ}É…¥¹É¥Ù•ÉI½ÝÌ¹™¥±Ñ•È¡É½ÜôùÉ½Ü¹Í•É¥•Ìôôô˜Äœ¤±Ù…±¥‘-•åÌõ¹•ÜM•Ð¡}É…¥¹É¥Ù•ÉI½ÝÌ¹µ…À¡É½ÜôùMÑÉ¥¹œ¡É½Ü¹­•åñðœœ¤¤¤í¥˜¡}É…¥¹M•±•Ñ•¹¡…Ì ˜Äœ¤¥Ù…±¥‘-•åÌ¹…‘ ˜ÄµÑ•…´œ¤ì(€€€¥˜ …}É…¥¹•Ñ…¥±-•åñð…Ù…±¥‘-•åÌ¹¡…Ì¡}É…¥¹•Ñ…¥±-•ä¤¥}É…¥¹•Ñ…¥±-•äõ}É…¥¹M•±•Ñ•¹¡…Ì ˜Äœ¤ü˜ÄµÑ•…´œéMÑÉ¥¹œ ¡}É…¥¹É¥Ù•ÉI½ÝÍlÁuññíô¤¹­•åñðœœ¤ì(€€€É•¹‘•ÉI…¥¹Q•…µ½¹ÑÉ½° ¤íÉ•¹‘•ÉI…¥¹É¥Ù•É•Ñ…¥° ¤í‘É¥Ù•ÉÌ¹¥¹¹•É!Q50õÉ…¥¹É¥Ù•ÉÍ!Ñµ°¡}É…¥¹É¥Ù•ÉI½ÝÌ±}É…¥¹Ù•¹ÑI½ÝÌ¤ì(€€€É•¹‘•ÉI…¥¹M¡•‘Õ±•…É‘Ì ¤í±½…‘I…¥¹Ù…¥±…‰¥±¥Ñä ¤ì(€õ…Ñ ¡”¥í‘É¥Ù•ÉÌ¹¥¹¹•É!Q50ôœœí¥¹™¼¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡ÑÈ ½Õ±¹½Ð±½…É…¥¹œÍ¡•‘Õ±•Ì¸œ¤¤¬œð½ÍÁ…¸øœíô)ô)…Íå¹Œ™Õ¹Ñ¥½¸Ñ½±•I…¥¹M•É¥•Ì¡­•ä¥ì(€¥˜¡}É…¥¹M•±•Ñ•¹¡…Ì¡­•ä¤¥}É…¥¹M•±•Ñ•¹‘•±•Ñ”¡­•ä¤í•±Í”}É…¥¹M•±•Ñ•¹…‘¡­•ä¤ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½É…¥¹}Í•É¥•Ìœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íÍ•É¥•ÌéÉÉ…ä¹™É½´¡}É…¥¹M•±•Ñ•¥ô¥ô¤ì(€¥˜¡È¹•ÉÉ½È¥íÑ½…ÍÐ¡È¹•ÉÉ½È¤íÉ•ÑÕÉ¸íô(€}ÁÉ½™¥±•½¹™¥œ¹É…¥¹}Í•É¥•ÌõÈ¹Í•É¥•Íññmtí…Ý…¥Ð±½…‘I…¥¹œ ¤í±½…‘…Ù½É¥Ñ•Ì ¤ì)ô)±•Ð}Í¡½ÝM•…Í½¹Ìõíôì)±•Ð}…Ñ¥Ù•M•É¥•Í%õ¹Õ±°ì)±•Ð}™…ÙM¡½ÝM•Ðõ¹•ÜM•Ð ¤ì)±•Ð}™…ÙM¡½ÝQ¥Ñ±•M•Ðõ¹•ÜM•Ð ¤ì)±•Ð}±…Ñ•ÍÑÁ¥Í½‘•Í1½…‘•õ™…±Í”ì)™Õ¹Ñ¥½¸±…Ñ•ÍÑÁ¥Í½‘•…É¡•À¥ì(€½¹ÍÐ½Ù•Èõ•À¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡•À¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹Á…É•¹Ñ±•µ•¹Ð¹Ñ•áÑ½¹Ñ•¹ÐõMÑÉ¥¹œ¹™É½µ½‘•A½¥¹Ð ÄÈàÈÔÀ¤ˆøœèœ˜ŒÄÈàÈÔÀìœì(€±•Ð…Ñ¥½¸ôœñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐˆ‘¥Í…‰±•øœ­ÑÈ 9½Ð…Ù…¥±…‰±”œ¤¬œð½‰ÕÑÑ½¸øœì(€¥˜¡•À¹…Ù…¥±…‰±”¥ì(€€€½¹ÍÐÍ½ÕÉ•Ìô¡•À¹Í½ÕÉ•Ì˜™•À¹Í½ÕÉ•Ì¹±•¹Ñ ¤ý•À¹Í½ÕÉ•Ìémí¥é•À¹¥±•áÑ•¹Í¥½¸é•À¹•áÑ•¹Í¥½¸±±…‰•°èY1õtì(€€€½¹ÍÐÍ½ÕÉ•	ÕÑÑ½¹ÌõÍ½ÕÉ•Ì¹µ…À¡ÍÉŒôøœñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Ù±Œ±…Ñ•ÍÑ•Á¥Í½‘•Ù±Œˆ‘…Ñ„µ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡ÍÉŒ¹¥¤¤¬œˆ‘…Ñ„µ•áÐôˆœ­•ÍÑÑÈ¡ÍÉŒ¹•áÑ•¹Í¥½¹ñðµÀÐœ¤¬œˆø˜ŒäØÔàì€œ­•ÍŒ¡ÍÉŒ¹±…‰•°¤¬œð½‰ÕÑÑ½¸øœ¤¹©½¥¸ œœ¤ì(€€€…Ñ¥½¸õÍ½ÕÉ•Ì¹±•¹Ñ øÌüœñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Ù±Œ±…Ñ•ÍÑÍ½ÕÉ••áÁ…¹ˆø˜ŒäØÔàìY1ð½‰ÕÑÑ½¸øñ‘¥Ø±…ÍÌô‰±…Ñ•ÍÑÍ½ÕÉ•Ì¡¥‘”ˆøœ­Í½ÕÉ•	ÕÑÑ½¹Ì¬œð½‘¥ØøœéÍ½ÕÉ•	ÕÑÑ½¹Ìì(€ô(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰µ½Ù¥•…É±…Ñ•ÍÑÍ¡½Ý…Éˆ‘…Ñ„µÍ•É¥•Ìôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡•À¹Í•É¥•Í}¥‘ñðœœ¤¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡•À¹…Ñ…±½}¥‘ñðœœ¤¬œˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•Á½ÍÑ•Èˆøœ­½Ù•È¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ½Ù¥•¥¹™¼ˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•Ñ¥Ñ±”ˆøœ­•ÍŒ¡•À¹Í¡½Ý}¹…µ”¤¬œð½‘¥Øøœ(€€€€¬œñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆùLœ­•ÍŒ¡•À¹Í•…Í½¸¤¬œ­•ÍŒ¡•À¹•Á¥Í½‘•}¹Õ´¤¬œ€´€œ­•ÍŒ¡•À¹Ñ¥Ñ±•ñðÁ¥Í½‘”œ¤¬œð½‘¥Øøœ(€€€€¬œñ‘¥Ø±…ÍÌô‰µ½Ù¥•…Ñ¥½¹Ìˆøœ­…Ñ¥½¸¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸½Í±½…å9Õµ‰•È¡Ù…±Õ”¥ì(€½¹ÍÐÁ…ÉÑÌõ¹•Ü%¹Ñ°¹…Ñ•Q¥µ•½Éµ…Ð •¸µœ±íÑ¥µ•i½¹”èÕÉ½Á”½=Í±¼œ±å•…Èè¹Õµ•É¥Œœ±µ½¹Ñ èœÈµ‘¥¥Ðœ±‘…äèœÈµ‘¥¥Ðô¤¹™½Éµ…ÑQ½A…ÉÑÌ¡Ù…±Õ”¤ì(€½¹ÍÐÀõíôíÁ…ÉÑÌ¹™½É… ¡àôùí¥˜¡à¹ÑåÁ”„ôô±¥Ñ•É…°œ¥Ámà¹ÑåÁ•tõÁ…ÉÍ•%¹Ð¡à¹Ù…±Õ”°ÄÀ¤íô¤ì(€É•ÑÕÉ¸…Ñ”¹UQ¡À¹å•…È±À¹µ½¹Ñ ´Ä±À¹‘…ä¤¼àØÐÀÀÀÀÀì)ô)™Õ¹Ñ¥½¸™É¥•¹‘±å¥É‘…Ñ”¡•À¥ì(€¥˜ …•À¹…¥É‘…Ñ”˜˜…•À¹…¥ÉÍÑ…µÀ¥É•ÑÕÉ¸€œœì(€±•ÐÑ…É•Ðõ•À¹…¥ÉÍÑ…µÀý¹•Ü…Ñ”¡•À¹…¥ÉÍÑ…µÀ¤é¹•Ü…Ñ”¡•À¹…¥É‘…Ñ”¬PÄÈèÀÀèÀÀœ¤ì(€¥˜¡9Õµ‰•È¹¥Í9…8¡Ñ…É•Ð¹•ÑQ¥µ” ¤¤¥Ñ…É•Ðõ¹•Ü…Ñ”¡•À¹…¥É‘…Ñ”¬PÄÈèÀÀèÀÀœ¤ì(€½¹ÍÐ¹½Üõ¹•Ü…Ñ” ¤°É•µ…¥¹¥¹œõÑ…É•Ðµ¹½Üì(€¥˜¡É•µ…¥¹¥¹œøÀ˜™É•µ…¥¹¥¹œðàØÐÀÀÀÀÀ¥ì(€€€½¹ÍÐµ¥¹ÕÑ•Ìõ5…Ñ ¹µ…à Ä±5…Ñ ¹•¥°¡É•µ…¥¹¥¹œ¼ØÀÀÀÀ¤¤ì(€€€¥˜¡µ¥¹ÕÑ•ÌðØÀ¥É•ÑÕÉ¸ÑÈ ¥¸œ¤¬œ€œ­µ¥¹ÕÑ•Ì¬œ€œ­ÑÈ¡µ¥¹ÕÑ•ÌôôôÄüµ¥¹ÕÑ”œèµ¥¹ÕÑ•Ìœ¤ì(€€€½¹ÍÐ¡½ÕÉÌõ5…Ñ ¹•¥°¡µ¥¹ÕÑ•Ì¼ØÀ¤ì(€€€É•ÑÕÉ¸ÑÈ ¥¸œ¤¬œ€œ­¡½ÕÉÌ¬œ€œ­ÑÈ¡¡½ÕÉÌôôôÄü¡½ÕÈœè¡½ÕÉÌœ¤ì(€ô(€½¹ÍÐ‘…åÌõ5…Ñ ¹É½Õ¹¡½Í±½…å9Õµ‰•È¡Ñ…É•Ð¤µ½Í±½…å9Õµ‰•È¡¹½Ü¤¤ì(€¥˜¡‘…åÌôôôÀ¥É•ÑÕÉ¸ÑÈ Q½‘…äœ¤ì(€¥˜¡‘…åÌôôôÄ¥É•ÑÕÉ¸ÑÈ Q½µ½ÉÉ½Üœ¤ì(€½¹ÍÐÝ••­‘…äõÑ…É•Ð¹Ñ½1½…±•…Ñ•MÑÉ¥¹œ¡}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•±íÝ••­‘…äè±½¹œœ±Ñ¥µ•i½¹”èÕÉ½Á”½=Í±¼ô¤ì(€É•ÑÕÉ¸Ý••­‘…ä¬œqÔÀÁˆÜ€œ­ÑÈ ¥¸œ¤¬œ€œ­‘…åÌ¬œ€œ­ÑÈ¡‘…åÌôôôÄü‘…äœè‘…åÌœ¤ì)ô)™Õ¹Ñ¥½¸ÕÁ½µ¥¹Á¥Í½‘•…É¡•À¥ì(€½¹ÍÐ½Ù•Èõ•À¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡•À¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹Á…É•¹Ñ±•µ•¹Ð¹Ñ•áÑ½¹Ñ•¹ÐõMÑÉ¥¹œ¹™É½µ½‘•A½¥¹Ð ÄÈàÈÔÀ¤ˆøœèœ˜ŒÄÈàÈÔÀìœì(€½¹ÍÐÝ¡•¸õ™É¥•¹‘±å¥É‘…Ñ”¡•À¤ì(€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰µ½Ù¥•…É±…Ñ•ÍÑÍ¡½Ý…Éˆ‘…Ñ„µÍ•É¥•Ìôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡•À¹Í•É¥•Í}¥‘ñðœœ¤¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡•À¹…Ñ…±½}¥‘ñðœœ¤¬œˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•Á½ÍÑ•Èˆøœ­½Ù•È¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ½Ù¥•¥¹™¼ˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•Ñ¥Ñ±”ˆøœ­•ÍŒ¡•À¹Í¡½Ý}¹…µ”¤¬œð½‘¥Øøœ(€€€€¬œñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆùLœ­•ÍŒ¡•À¹Í•…Í½¸¤¬œ­•ÍŒ¡•À¹•Á¥Í½‘•}¹Õ´¤¬œ€´€œ­•ÍŒ¡•À¹Ñ¥Ñ±•ñðÁ¥Í½‘”œ¤¬œð½‘¥Øøœ(€€€€¬œñ‘¥Ø±…ÍÌô‰µ½Ù¥•…Ñ¥½¹Ìˆøñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐˆ‘¥Í…‰±•øœ­ÑÈ ¥ÉÌœ¤¬œ€œ­•ÍŒ¡Ý¡•¸¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøð½‘¥Øøð½‘¥Øøœì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘1…Ñ•ÍÑÁ¥Í½‘•Ì¡±¥µ¥Ð±É•™É•Í ¥ì(€±¥µ¥Ðõ±¥µ¥Ññðäì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ±…Ñ•ÍÑÁ¥Í½‘•1¥ÍÐœ¤°µ½É”õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ±…Ñ•ÍÑÁ¥Í½‘•5½É”œ¤ì(€½¹ÍÐÕÁ½µ¥¹M•Ñ¥½¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ½µ¥¹Á¥Í½‘•ÍM•Ñ¥½¸œ¤°ÕÁ½µ¥¹1¥ÍÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ½µ¥¹Á¥Í½‘•1¥ÍÐœ¤ì(€•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù1½…‘¥¹œ±…Ñ•ÍÐ•Á¥Í½‘•Ì¸¸¸ð½ÍÁ…¸øœíµ½É”¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€ÕÁ½µ¥¹M•Ñ¥½¸¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íÕÁ½µ¥¹1¥ÍÐ¹¥¹¹•É!Q50ôœœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½±…Ñ•ÍÑ}•Á¥Í½‘•Ìý±¥µ¥Ðôœ­±¥µ¥Ð¬¡É•™É•Í üœ™É•™É•Í ôÄœèœœ¤¤ì(€¥˜¡È¹•ÉÉ½È¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù½Õ±¹½Ð±½…±…Ñ•ÍÐ•Á¥Í½‘•Ì¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸™…±Í”íô(€¥˜¡È¹•Á¥Í½‘•Ì¹±•¹Ñ ¥•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µ½Ù¥•É¥ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÀˆøœ­È¹•Á¥Í½‘•Ì¹µ…À¡±…Ñ•ÍÑÁ¥Í½‘•…É¤¹©½¥¸ œœ¤¬œð½‘¥Øøœì(€•±Í”•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼±…Ñ•ÍÐ•Á¥Í½‘•Ì™½Õ¹™½Èå½ÕÈ™…Ù½É¥Ñ”Í¡½ÝÌ¸ð½ÍÁ…¸øœì(€¥˜¡È¹ÕÁ½µ¥¹œ˜™È¹ÕÁ½µ¥¹œ¹±•¹Ñ ¥ì(€€€ÕÁ½µ¥¹1¥ÍÐ¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µ½Ù¥•É¥ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÀˆøœ­È¹ÕÁ½µ¥¹œ¹µ…À¡ÕÁ½µ¥¹Á¥Í½‘•…É¤¹©½¥¸ œœ¤¬œð½‘¥Øøœì(€€€ÕÁ½µ¥¹M•Ñ¥½¸¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€ô(€¥˜¡±¥µ¥ÐðÌØ˜™È¹¡…Í}µ½É”¥µ½É”¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€}±…Ñ•ÍÑÁ¥Í½‘•Í1½…‘•õÑÉÕ”ì(€É•ÑÕÉ¸€„„¡È¹•Á¥Í½‘•Ì¹±•¹Ñ¡ñð¡È¹ÕÁ½µ¥¹œ˜™È¹ÕÁ½µ¥¹œ¹±•¹Ñ ¤¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸•áÁ…¹‘1…Ñ•ÍÑÁ¥Í½‘•Ì¡‰Ñ¸¥ì(€½¹ÍÐ½±õ‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðí‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðô1½…‘¥¹œ¸¸¸œì(€…Ý…¥Ð±½…‘1…Ñ•ÍÑÁ¥Í½‘•Ì ÌØ¤ì(€‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±í‰Ñ¸¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Á±…å1…Ñ•ÍÑÁ¥Í½‘”¡¥±•áÐ±‰Ñ¸¥ì(€½¹ÍÐ½±õ‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðí‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðô=Á•¹¥¹œ¸¸¸œì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½Á±…å}Í•…Í½¸œ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡í•Á¥Í½‘•Ìémí¥é¥±•áÑ•¹Í¥½¸é•áÑõuô¥ô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥…±•ÉÐ¡¨¹•ÉÉ½Éñð½Õ±¹½Ð±…Õ¹ Y1¸œ¤ì(€õ…Ñ ¡”¥í…±•ÉÐ ½Õ±¹½Ð±…Õ¹ Y1¸œ¤íô(€Í•ÑQ¥µ•½ÕÐ  ¤ôùí‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±íô°ÄÈÀÀ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘M¡½Ý…Ù½É¥Ñ•Ì ¥ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ¤°Í¡½ÝÌõÈ¹Í¡½ÝÍññmtì(€}™…ÙM¡½ÝM•Ðõ¹•ÜM•Ð¡Í¡½ÝÌ¹µ…À¡ÌôùMÑÉ¥¹œ¡Ì¹…Ñ…±½}¥‘ññÌ¹Í¡½Ý}­•åññÌ¹Í•É¥•Í}¥¤¤¤ì(€}™…ÙM¡½ÝQ¥Ñ±•M•Ðõ¹•ÜM•Ð¡Í¡½ÝÌ¹µ…À¡ÌôùMÑÉ¥¹œ¡Ì¹Í¡½Ý}­•åñðœœ¤¤¹™¥±Ñ•È¡	½½±•…¸¤¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½Ý…Ù1¥ÍÐœ¤ì(€¥˜ …Í¡½ÝÌ¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆù9¼™…Ù½É¥Ñ”Í¡½ÝÌå•Ð¸ð½ÍÁ…¸øœíÉ•ÑÕÉ¸íô(€±•Ð ôœœì(€™½È¡½¹ÍÐÌ½˜Í¡½ÝÌ¥ì(€€€½¹ÍÐÁ½ÍÑ•ÈôœñÍÁ…¸±…ÍÌô‰Í¡½Ý™…ÙÁ½ÍÑ•Èˆø˜ŒÄÈàÈÔÀìœ¬¡Ì¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡Ì¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œð½ÍÁ…¸øœì(€€€½¹ÍÐ¥‘Ìô¡Ì¹Í•É¥•Í}¥‘Ì˜™Ì¹Í•É¥•Í}¥‘Ì¹±•¹Ñ ýÌ¹Í•É¥•Í}¥‘ÌémÌ¹Í•É¥•Í}¥‘t¤¹©½¥¸ œ°œ¤ì(€€€½¹ÍÐ­•äõMÑÉ¥¹œ¡Ì¹…Ñ…±½}¥‘ññÌ¹Í¡½Ý}­•åññÌ¹Í•É¥•Í}¥¤ì(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰Í¡½Ý™…Øˆ‘…Ñ„µÍ•É¥•Ìôˆœ­•ÍÑÑÈ¡¥‘Ì¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡Ì¹…Ñ…±½}¥‘ñðœœ¤¬œˆøœ­Á½ÍÑ•È¬œñ‘¥Ø±…ÍÌô‰Í¡½Ý™…Ù¥¹™¼ˆøñ‘¥Ø±…ÍÌô‰Í¡½Ý™…Ù¹…µ”ˆøœ­•ÍŒ¡Ì¹¹…µ”¤¬œð½‘¥Øøð½‘¥Øøœ(€€€€€€¬œñ‰ÕÑÑ½¸±…ÍÌô‰™…ÙÉ´Í¡½ÝÉ•µ½Ù”ˆ‘…Ñ„µ­•äôˆœ­•ÍÑÑÈ¡­•ä¤¬œˆÑ¥Ñ±”ô‰I•µ½Ù”ˆø™Ñ¥µ•Ììð½‰ÕÑÑ½¸øð½‘¥Øøœì(€ô(€•°¹¥¹¹•É!Q50õ ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Ñ½±•M¡½Ý…Ù½É¥Ñ”¡Í¡½Ü±ÍÑ…É°¥ì(€½¹ÍÐÈõ…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÑ½±•}Í¡½Üœ±Í¡½ÜéÍ¡½Ýô¤ì(€}™…ÙM¡½ÝM•Ðõ¹•ÜM•Ð ¡È¹Í¡½Ý}¥‘Íññmt¤¹µ…À¡MÑÉ¥¹œ¤¤ì(€¥˜¡}™…ÙM¡½ÝM•Ð¹¡…Ì¡MÑÉ¥¹œ¡Í¡½Ü¹…Ñ…±½}¥‘ññÍ¡½Ü¹Í¡½Ý}­•åññÍ¡½Ü¹Í•É¥•Í}¥¤¤¥}ÁÉ½™¥±•½¹™¥œ¹Í•ÑÕÁ}‘•µ½}½¹Ñ•¹Ðõ™…±Í”ì(€¥˜¡ÍÑ…É°¥ÍÑ…É°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½¸œ±}™…ÙM¡½ÝM•Ð¹¡…Ì¡MÑÉ¥¹œ¡Í¡½Ü¹…Ñ…±½}¥‘ññÍ¡½Ü¹Í¡½Ý}­•åññÍ¡½Ü¹Í•É¥•Í}¥¤¤¤ì(€}±…Ñ•ÍÑÁ¥Í½‘•Í1½…‘•õ™…±Í”ì(€…Ý…¥Ð±½…‘M¡½Ý…Ù½É¥Ñ•Ì ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•µ½Ù•M¡½Ý…Ù½É¥Ñ”¡Í¡½Ý-•ä¥ì(€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÉ•µ½Ù•}Í¡½Üœ±Í¡½Ý}­•äéÍ¡½Ý-•åô¤ì(€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹Í¡½ÝÍÑ…Èœ¤¹™½É… ¡•°ôùí¥˜¡•°¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ­•äœ¤ôôõMÑÉ¥¹œ¡Í¡½Ý-•ä¤¥•°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ½¸œ¤íô¤ì(€}±…Ñ•ÍÑÁ¥Í½‘•Í1½…‘•õ™…±Í”ì(€…Ý…¥Ð±½…‘M¡½Ý…Ù½É¥Ñ•Ì ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Í•…É¡M¡½ÝÌ ¥ì(€½¹ÍÐÄô¡‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½ÝDœ¤¹Ù…±Õ•ñðœœ¤¹ÑÉ¥´ ¤°•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½ÝI•ÍÕ±ÑÌœ¤ì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½Ý•Ñ…¥±Ìœ¤¹¥¹¹•É!Q50ôœœì(€½¹ÍÐ±…Ñ•ÍÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ±…Ñ•ÍÑÁ¥Í½‘•ÍM•Ñ¥½¸œ¤ì(€¥˜ …Ä¥í±…Ñ•ÍÐ¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÄÑÁàˆù¹Ñ•È„Í¡½ÜÑ¥Ñ±”¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€±…Ñ•ÍÐ¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÄÑÁàˆùM•…É¡¥¹œå½ÕÈÍ¡½ÝÌ¸¸¸ð½‘¥Øøœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½Í¡½ÝÌýÄôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Ä¤¤ì(€½¹ÍÐ‰…¬ôœñ‘¥Ø±…ÍÌô‰Í¡½ÝÉ•ÍÕ±Ñ‰…¬ˆøñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐˆ½¹±¥¬ô‰‰…­Q½5åM¡½ÝÌ ¤ˆø˜ŒàÔäÈì€œ­ÑÈ 	…¬Ñ¼M¡½ÝÌœ¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøœì(€¥˜¡È¹•ÉÉ½È¥í•°¹¥¹¹•É!Q50õ‰…¬¬œñ‘¥Ø±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡È¹•ÉÉ½È¤¬œð½‘¥ØøœíÉ•ÑÕÉ¸íô(€¥˜ …È¹Í¡½ÝÌ¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50õ‰…¬¬œñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù9¼Í¡½ÝÌ™½Õ¹™½È€™ÅÕ½Ðìœ­•ÍŒ¡Ä¤¬œ™ÅÕ½Ðì¸ð½‘¥ØøœíÉ•ÑÕÉ¸íô(€…Ý…¥Ð±½…‘M¡½Ý…Ù½É¥Ñ•Ì ¤ì(€±•Ð õ‰…¬¬œñ‘¥Ø±…ÍÌô‰Í¡½ÝÉ¥ˆøœì(€™½È¡½¹ÍÐÌ½˜È¹Í¡½ÝÌ¥ì(€€€½¹ÍÐ™…Øô¡}™…ÙM¡½ÝM•Ð¹¡…Ì¡MÑÉ¥¹œ¡Ì¹…Ñ…±½}¥‘ññÌ¹Í¡½Ý}­•ä¤¥ññ}™…ÙM¡½ÝQ¥Ñ±•M•Ð¹¡…Ì¡MÑÉ¥¹œ¡Ì¹Í¡½Ý}­•ä¤¤¤üœ½¸œèœœì(€€€½¹ÍÐ¥‘Ìô¡Ì¹Í•É¥•Í}¥‘ÍññmÌ¹Í•É¥•Í}¥‘t¤¹©½¥¸ œ°œ¤ì(€€€½¹ÍÐ½Ù•ÈõÌ¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡Ì¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰Í¡½Ý…Éˆ‘…Ñ„µÍ•É¥•Ìôˆœ­•ÍÑÑÈ¡¥‘Ì¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡Ì¹…Ñ…±½}¥‘ñðœœ¤¬œˆøñ‘¥Ø±…ÍÌô‰Í¡½ÝÁ½ÍÑ•Èˆø˜ŒÄÈàÈÔÀìœ­½Ù•È¬œð½‘¥Øøœ(€€€€€€¬œñ‘¥Øøñ‘¥Ø±…ÍÌô‰Í¡½Ý¹…µ”ˆøœ­•ÍŒ¡Ì¹¹…µ”¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆÍÑå±”ô‰µ…É¥¸µÑ½ÀèÝÁàˆøœ¬¡Ì¹å•…Èý•ÍŒ¡Ì¹å•…È¤èœœ¤¬¡Ì¹É…Ñ¥¹œü œ€™¹‰ÍÀìI…Ñ¥¹œè€œ­•ÍŒ¡Ì¹É…Ñ¥¹œ¤¤èœœ¤¬œð½‘¥Øøœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…ÈÍ¡½ÝÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µ­•äôˆœ­•ÍÑÑÈ¡Ì¹…Ñ…±½}¥‘ññÌ¹Í¡½Ý}­•ä¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡Ì¹…Ñ…±½}¥‘ñðœœ¤¬œˆ‘…Ñ„µÍ¡½Üµ­•äôˆœ­•ÍÑÑÈ¡Ì¹Í¡½Ý}­•ä¤¬œˆ‘…Ñ„µÍ•É¥•Ìôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Ì¹Í•É¥•Í}¥ôõ¹Õ±°üœœéÌ¹Í•É¥•Í}¥¤¤¬œˆ‘…Ñ„µÍ•É¥•Ìµ¥‘Ìôˆœ­•ÍÑÑÈ¡¥‘Ì¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡Ì¹¹…µ•ñðœœ¤¬œˆ‘…Ñ„µ½Ù•Èôˆœ­•ÍÑÑÈ¡Ì¹½Ù•Éñðœœ¤¬œˆ‘…Ñ„µå•…Èôˆœ­•ÍÑÑÈ¡Ì¹å•…Éñðœœ¤¬œˆ‘…Ñ„µÉ…Ñ¥¹œôˆœ­•ÍÑÑÈ¡Ì¹É…Ñ¥¹ñðœœ¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øð½‘¥Øøð½‘¥Øøœì(€ô(€•°¹¥¹¹•É!Q50õ ¬œð½‘¥Øøœì)ô)™Õ¹Ñ¥½¸‰…­Q½5åM¡½ÝÌ ¥ì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½ÝDœ¤¹Ù…±Õ”ôœœì(€Í¡½ÝM¡½ÝÌ ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘M¡½Ü¡Í•É¥•Í%±É•™É•Í ¥ì(€¥˜ …É•™É•Í ¥É•µ•µ‰•É1½…Ñ¥½¸ Í¡½ÝÌœ±íÍ•É¥•Í%éMÑÉ¥¹œ¡Í•É¥•Í%¥ô¤ì(€}…Ñ¥Ù•M•É¥•Í%õÍ•É¥•Í%ì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ±…Ñ•ÍÑÁ¥Í½‘•ÍM•Ñ¥½¸œ¤¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½Ý•Ñ…¥±Ìœ¤ì(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù1½…‘¥¹œÍ•…Í½¹Ì…¹•Á¥Í½‘•Ì¸¸¸ð½‘¥Øøœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½Í¡½Üý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Í•É¥•Í%¤¬¡É•™É•Í üœ™É•™É•Í ôÄœèœœ¤¤ì(€¥˜¡È¹•ÉÉ½È¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡È¹•ÉÉ½È¤¬œð½‘¥ØøœíÉ•ÑÕÉ¸™…±Í”íô(€…Ý…¥Ð±½…‘M¡½Ý…Ù½É¥Ñ•Ì ¤ì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½ÝI•ÍÕ±ÑÌœ¤¹¥¹¹•É!Q50ôœœì(€}Í¡½ÝM•…Í½¹Ìõíôì(€½¹ÍÐ¡•É½½Ù•ÈõÈ¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡È¹½Ù•È¤¬œˆ…±Ðôˆˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€½¹ÍÐ¡•É½…Øô¡}™…ÙM¡½ÝM•Ð¹¡…Ì¡MÑÉ¥¹œ¡È¹Í¡½Ý}­•ä¤¥ññ}™…ÙM¡½ÝQ¥Ñ±•M•Ð¹¡…Ì¡MÑÉ¥¹œ¡È¹Í¡½Ý}­•ä¤¤¤üœ½¸œèœœì(€±•Ð ôœñ‘¥Ø±…ÍÌô‰Í¡½Ý¡•É¼ˆøñ‘¥Ø±…ÍÌô‰Í¡½Ý¡•É½…ÉÐˆø˜ŒÄÈàÈÔÀìœ­¡•É½½Ù•È¬œð½‘¥Øøñ‘¥Øøñ Èøœ­•ÍŒ¡È¹¹…µ•ñðM¡½Üœ¤(€€€€¬œñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…ÈÍ¡½ÝÍÑ…Èœ­¡•É½…Ø¬œˆ‘…Ñ„µ­•äôˆœ­•ÍÑÑÈ¡È¹Í¡½Ý}­•ä¤¬œˆ‘…Ñ„µÍ•É¥•Ìôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡È¹Í•É¥•Í}¥¤¤¬œˆ‘…Ñ„µÍ•É¥•Ìµ¥‘Ìôˆœ­•ÍÑÑÈ ¡È¹Í•É¥•Í}¥‘Íññmt¤¹©½¥¸ œ°œ¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡È¹¹…µ•ñðM¡½Üœ¤¬œˆ‘…Ñ„µ½Ù•Èôˆœ­•ÍÑÑÈ¡È¹½Ù•Éñðœœ¤¬œˆ‘…Ñ„µå•…Èôˆˆ‘…Ñ„µÉ…Ñ¥¹œôˆˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øð½ Èøœ(€€€€¬œñ‘¥Ø±…ÍÌô‰µÕÑ•ˆøœ­È¹Í•…Í½¹Ì¹±•¹Ñ ¬œÍ•…Í½¸œ¬¡È¹Í•…Í½¹Ì¹±•¹Ñ ôôôÄüœœèÌœ¤¬œð½‘¥Øøð½‘¥Øøñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐÍ¡½Ý‰…­‰Ñ¸ˆ½¹±¥¬ô‰‰…­Q½5åM¡½ÝÌ ¤ˆø˜ŒàÔäÈì€œ­ÑÈ 	…¬Ñ¼M¡½ÝÌœ¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøœì(€™½È¡½¹ÍÐÍ•…Í½¸½˜È¹Í•…Í½¹Ì¥ì(€€€}Í¡½ÝM•…Í½¹ÍmMÑÉ¥¹œ¡Í•…Í½¸¹¹Õµ‰•È¥tõíôì(€€€™½È¡½¹ÍÐ•À½˜Í•…Í½¸¹•Á¥Í½‘•Ì¥™½È¡½¹ÍÐÍÉŒ½˜€¡•À¹Í½ÕÉ•Íññmt¤¥ì(€€€€€¥˜ …}Í¡½ÝM•…Í½¹ÍmMÑÉ¥¹œ¡Í•…Í½¸¹¹Õµ‰•È¥umÍÉŒ¹±…‰•±t¥}Í¡½ÝM•…Í½¹ÍmMÑÉ¥¹œ¡Í•…Í½¸¹¹Õµ‰•È¥umÍÉŒ¹±…‰•±tõmtì(€€€€€}Í¡½ÝM•…Í½¹ÍmMÑÉ¥¹œ¡Í•…Í½¸¹¹Õµ‰•È¥umÍÉŒ¹±…‰•±t¹ÁÕÍ ¡í¥éÍÉŒ¹¥±•áÑ•¹Í¥½¸éÍÉŒ¹•áÑ•¹Í¥½¸±•Á¥Í½‘•}¹Õ´é•À¹•Á¥Í½‘•}¹Õµô¤ì(€€€ô(€€€½¹ÍÐÍ•…Í½¹½Ù•ÈõÍ•…Í½¸¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡Í•…Í½¸¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰Í•…Í½¹‰±½¬ˆøñ‘¥Ø±…ÍÌô‰Í•…Í½¹±…å½ÕÐˆøñ‘¥Ø±…ÍÌô‰Í•…Í½¹…ÉÐˆø˜ŒÄÈàÈÔÀìœ­Í•…Í½¹½Ù•È¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰Í•…Í½¹½¹Ñ•¹Ðˆøñ‘¥Ø±…ÍÌô‰Í•…Í½¹¡•…ˆøñˆøœ­•ÍŒ¡Í•…Í½¸¹Ñ¥Ñ±”¤¬œð½ˆøð½‘¥Øøñ‘¥Ø±…ÍÌô‰•Á¥Í½‘•Ìˆøœì(€€€™½È¡±•Ð•¤ôÀí•¤ñÍ•…Í½¸¹•Á¥Í½‘•Ì¹±•¹Ñ í•¤¬¬¥ì(€€€€€½¹ÍÐ•ÀõÍ•…Í½¸¹•Á¥Í½‘•Ím•¥tì(€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰•Á¥Í½‘”ˆøñ‘¥Ø±…ÍÌô‰•Á¥Í½‘•¹…µ”ˆøñˆùœ­•ÍŒ¡•À¹•Á¥Í½‘•}¹Õ´¤¬œð½ˆø€œ­•ÍŒ¡•À¹Ñ¥Ñ±•ñðÁ¥Í½‘”œ¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰•Á¥Í½‘•ÅÕ…±¥Ñ¥•Ìˆøœì(€€€€€™½È¡½¹ÍÐÍÉŒ½˜€¡•À¹Í½ÕÉ•Íññmt¤¥ ¬ôœñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Ù±Œ•Á¥Í½‘•Ù±Œˆ‘…Ñ„µÍ•…Í½¸ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Í•…Í½¸¹¹Õµ‰•È¤¤¬œˆ‘…Ñ„µ•Á¥Í½‘”ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡•À¹•Á¥Í½‘•}¹Õ´¤¤¬œˆ‘…Ñ„µÍ½ÕÉ”ôˆœ­•ÍÑÑÈ¡ÍÉŒ¹±…‰•°¤¬œˆø˜ŒäØÔàì€œ­•ÍŒ¡ÍÉŒ¹±…‰•°¤¬œð½‰ÕÑÑ½¸øœì(€€€€€ ¬ôœð½‘¥Øøð½‘¥Øøœì(€€€ô(€€€ ¬ôœð½‘¥Øøð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€ô(€¥˜ …È¹Í•…Í½¹Ì¹±•¹Ñ ¥ ¬ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù9¼•Á¥Í½‘•Ì™½Õ¹¸ð½‘¥Øøœì(€•°¹¥¹¹•É!Q50õ ì(€É•ÑÕÉ¸ÑÉÕ”ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘áÑ•É¹…±M¡½Ü¡…Ñ…±½%±É•™É•Í ¥ì(€¥˜ …É•™É•Í ¥É•µ•µ‰•É1½…Ñ¥½¸ Í¡½ÝÌœ±í…Ñ…±½%éMÑÉ¥¹œ¡…Ñ…±½%¥ô¤ì(€}…Ñ¥Ù•M•É¥•Í%õ¹Õ±°í}Í¡½ÝM•…Í½¹Ìõíôì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ±…Ñ•ÍÑÁ¥Í½‘•ÍM•Ñ¥½¸œ¤¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½Ý•Ñ…¥±Ìœ¤ì(€•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆù1½…‘¥¹œÍ¡½Ü¸¸¸ð½‘¥Øøœì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½Í¡½Ý}•áÑ•É¹…°ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡…Ñ…±½%¤¤ì(€¥˜¡È¹•ÉÉ½È¥í•°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰•ÉÈˆøœ­•ÍŒ¡È¹•ÉÉ½È¤¬œð½‘¥ØøœíÉ•ÑÕÉ¸™…±Í”íô(€¥˜¡È¹ÁÉ½Ù¥‘•É}Í•É¥•Í}¥‘Ì˜™È¹ÁÉ½Ù¥‘•É}Í•É¥•Í}¥‘Ì¹±•¹Ñ ¥É•ÑÕÉ¸±½…‘M¡½Ü¡È¹ÁÉ½Ù¥‘•É}Í•É¥•Í}¥‘Ì¹©½¥¸ œ°œ¤±ÑÉÕ”¤ì(€…Ý…¥Ð±½…‘M¡½Ý…Ù½É¥Ñ•Ì ¤í‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í¡½ÝI•ÍÕ±ÑÌœ¤¹¥¹¹•É!Q50ôœœì(€½¹ÍÐ½Ù•ÈõÈ¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡È¹½Ù•È¤¬œˆ…±Ðôˆˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ±­•äõMÑÉ¥¹œ¡È¹…Ñ…±½}¥‘ññÈ¹Í¡½Ý}­•ä¤±™…Øõ}™…ÙM¡½ÝM•Ð¹¡…Ì¡­•ä¤üœ½¸œèœœì(€±•Ð ôœñ‘¥Ø±…ÍÌô‰Í¡½Ý¡•É¼ˆøñ‘¥Ø±…ÍÌô‰Í¡½Ý¡•É½…ÉÐˆø˜ŒÄÈàÈÔÀìœ­½Ù•È¬œð½‘¥Øøñ‘¥Øøñ Èøœ­•ÍŒ¡È¹¹…µ•ñðM¡½Üœ¤(€€€€¬œñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…ÈÍ¡½ÝÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µ­•äôˆœ­•ÍÑÑÈ¡­•ä¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡È¹…Ñ…±½}¥‘ñðœœ¤¬œˆ‘…Ñ„µÍ¡½Üµ­•äôˆœ­•ÍÑÑÈ¡È¹Í¡½Ý}­•åñðœœ¤¬œˆ‘…Ñ„µÍ•É¥•Ìôˆˆ‘…Ñ„µÍ•É¥•Ìµ¥‘Ìôˆˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡È¹¹…µ•ñðM¡½Üœ¤¬œˆ‘…Ñ„µ½Ù•Èôˆœ­•ÍÑÑÈ¡È¹½Ù•Éñðœœ¤¬œˆ‘…Ñ„µå•…Èôˆœ­•ÍÑÑÈ¡È¹å•…Éñðœœ¤¬œˆ‘…Ñ„µÉ…Ñ¥¹œôˆœ­•ÍÑÑÈ¡È¹É…Ñ¥¹ñðœœ¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆø˜ŒäÜÌÌìð½ÍÁ…¸øð½ Èøœ(€€€€¬œñ‘¥Ø±…ÍÌô‰µÕÑ•ˆøœ­È¹Í•…Í½¹Ì¹±•¹Ñ ¬œÍ•…Í½¸œ¬¡È¹Í•…Í½¹Ì¹±•¹Ñ ôôôÄüœœèÌœ¤¬œð½‘¥Øøð½‘¥Øøñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐÍ¡½Ý‰…­‰Ñ¸ˆ½¹±¥¬ô‰‰…­Q½5åM¡½ÝÌ ¤ˆø˜ŒàÔäÈì€œ­ÑÈ 	…¬Ñ¼M¡½ÝÌœ¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøœì(€™½È¡½¹ÍÐÍ•…Í½¸½˜È¹Í•…Í½¹Ì¥ì(€€€½¹ÍÐÍ•…Í½¹½Ù•ÈõÍ•…Í½¸¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡Í•…Í½¸¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰Í•…Í½¹‰±½¬ˆøñ‘¥Ø±…ÍÌô‰Í•…Í½¹±…å½ÕÐˆøñ‘¥Ø±…ÍÌô‰Í•…Í½¹…ÉÐˆø˜ŒÄÈàÈÔÀìœ­Í•…Í½¹½Ù•È¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰Í•…Í½¹½¹Ñ•¹Ðˆøñ‘¥Ø±…ÍÌô‰Í•…Í½¹¡•…ˆøñˆøœ­•ÍŒ¡Í•…Í½¸¹Ñ¥Ñ±”¤¬œð½ˆøð½‘¥Øøñ‘¥Ø±…ÍÌô‰•Á¥Í½‘•Ìˆøœì(€€€™½È¡½¹ÍÐ•À½˜Í•…Í½¸¹•Á¥Í½‘•Ì¥ ¬ôœñ‘¥Ø±…ÍÌô‰•Á¥Í½‘”ˆøñ‘¥Ø±…ÍÌô‰•Á¥Í½‘•¹…µ”ˆøñˆùœ­•ÍŒ¡•À¹•Á¥Í½‘•}¹Õ´¤¬œð½ˆø€œ­•ÍŒ¡•À¹Ñ¥Ñ±•ñðÁ¥Í½‘”œ¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰•Á¥Í½‘•ÅÕ…±¥Ñ¥•Ìˆøñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐˆ‘¥Í…‰±•øœ­ÑÈ 9½Ð…Ù…¥±…‰±”œ¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøð½‘¥Øøœì(€€€ ¬ôœð½‘¥Øøð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€ô(€•°¹¥¹¹•É!Q50õ íÉ•ÑÕÉ¸ÑÉÕ”ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸¡•­±±M¡½ÝÌ¡‰Ñ¸¥ì(€½¹ÍÐ½±õ‰Ñ¸¹¥¹¹•É!Q50í‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðô¡•­¥¹œ…±°Í¡½ÝÌ¸¸¸œì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½¡•­}Í¡½Ý}ÕÁ‘…Ñ•Ìœ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡¨¹•ÉÉ½Éñð¡•¬™…¥±•œ¤ì(€€€¥˜¡}…Ñ¥Ù•M•É¥•Í%¥…Ý…¥Ð±½…‘M¡½Ü¡}…Ñ¥Ù•M•É¥•Í%±ÑÉÕ”¤ì(€€€…Ý…¥Ð±½…‘M¡½Ý…Ù½É¥Ñ•Ì ¤ì(€€€½¹ÍÐ±…Ñ•ÍÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ±…Ñ•ÍÑÁ¥Í½‘•ÍM•Ñ¥½¸œ¤ì(€€€¥˜¡±…Ñ•ÍÐ˜˜…±…Ñ•ÍÐ¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥…Ý…¥Ð±½…‘1…Ñ•ÍÑÁ¥Í½‘•Ì ä±ÑÉÕ”¤ì(€€€¥˜¡¨¹¹•Ý}•Á¥Í½‘•ÌøÀ¥Ñ½…ÍÐ ½Õ¹€œ­¨¹¹•Ý}•Á¥Í½‘•Ì¬œ¹•Ü•Á¥Í½‘”œ¬¡¨¹¹•Ý}•Á¥Í½‘•ÌôôôÄüœœèÌœ¤¬œ™½Èå½ÕÈÍ¡½ÝÌœ°ÜÀÀÀ¤ì(€€€•±Í”Ñ½…ÍÐ MÕ•ÍÍ™Õ±±äÉ•™É•Í¡•Á±…å±¥ÍÑÌ°¹¼¹•Ü•Á¥Í½‘•Ì™½Õ¹œ°ÜÀÀÀ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ ½Õ±¹½ÐÉ•™É•Í Í¡½ÜÁ±…å±¥ÍÑÌ¸œ¤íô(€‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹¥¹¹•É!Q50õ½±ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸¡•­M¡½ÝÍ=¹MÑ…ÉÑÕÀ ¥ì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½¡•­}Í¡½Ý}ÕÁ‘…Ñ•Ìœ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜¡¨¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡¨¹•ÉÉ½Éñð¡•¬™…¥±•œ¤ì(€€€}±…Ñ•ÍÑÁ¥Í½‘•Í1½…‘•õ™…±Í”ì(€€€¥˜¡¨¹¹•Ý}•Á¥Í½‘•ÌøÀ¥Ñ½…ÍÐ ½Õ¹€œ­¨¹¹•Ý}•Á¥Í½‘•Ì¬œ¹•Ü•Á¥Í½‘”œ¬¡¨¹¹•Ý}•Á¥Í½‘•ÌôôôÄüœœèÌœ¤¬œ™½Èå½ÕÈÍ¡½ÝÌœ°ÜÀÀÀ¤ì(€€€•±Í”Ñ½…ÍÐ MÕ•ÍÍ™Õ±±äÉ•™É•Í¡•Á±…å±¥ÍÑÌ°¹¼¹•Ü•Á¥Í½‘•Ì™½Õ¹œ°ÜÀÀÀ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ ½Õ±¹½ÐÉ•™É•Í Í¡½ÜÁ±…å±¥ÍÑÌ¸œ°ÜÀÀÀ¤íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•™É•Í¡=¹MÑ…ÉÑÕÀ¡É•™É•Í¡%ÁÑØ±É•™É•Í¡MÁ½ÉÑÌ¥ì(€ÑÉåì(€€€¥˜¡É•™É•Í¡%ÁÑØ¥…Ý…¥ÐÉ•™É•Í¡%ÁÑÙ½¹Ñ•¹Ð¡¹Õ±°±ÑÉÕ”¤ì(€€€¥˜¡É•™É•Í¡MÁ½ÉÑÌ¥…Ý…¥ÐÉ•™É•Í¡=Ñ¡•É½¹Ñ•¹Ð¡¹Õ±°±ÑÉÕ”¤ì(€€€Ñ½…ÍÐ MÑ…ÉÑÕÀÉ•™É•Í ™¥¹¥Í¡•¸œ°ÔÀÀÀ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ MÑ…ÉÑÕÀÉ•™É•Í ™…¥±•è€œ­MÑÉ¥¹œ¡”˜™”¹µ•ÍÍ…•ññ”¤°ÜÀÀÀ¤íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸Á±…åÁ¥Í½‘•EÕ•Õ”¡Í•…Í½¸±•Á¥Í½‘•9Õ´±Í½ÕÉ”±‰Ñ¸¥ì(€½¹ÍÐ•Á¥Í½‘•Ìô ¡}Í¡½ÝM•…Í½¹ÍmMÑÉ¥¹œ¡Í•…Í½¸¥uññíô¥mÍ½ÕÉ•uññmt¤¹™¥±Ñ•È¡•Àôù9Õµ‰•È¡•À¹•Á¥Í½‘•}¹Õ´¤øõ9Õµ‰•È¡•Á¥Í½‘•9Õ´¤¤°½±õ‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðí‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðô=Á•¹¥¹œ¸¸¸œì(€ÑÉåí½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½Á±…å}Í•…Í½¸œ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡í•Á¥Í½‘•Ìé•Á¥Í½‘•Íô¥ô¤í¥˜¡¨¹•ÉÉ½È¥…±•ÉÐ¡¨¹•ÉÉ½Éñð½Õ±¹½Ð±…Õ¹ Y1¸œ¤íô(€…Ñ ¡”¥í…±•ÉÐ ½Õ±¹½Ð±…Õ¹ Y1¸œ¤íô(€Í•ÑQ¥µ•½ÕÐ  ¤ôù‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõ½±°ÄÈÀÀ¤ì)ô)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ­•å‘½Ý¸œ±™Õ¹Ñ¥½¸¡”¥í¥˜¡”¹­•äôôôÍ…Á”œ¥±½Í•A±…å•È ¤íô¤ì(¼¼€´´´´™…Ù½É¥Ñ•Ì€¼5ä1¥ÍÐ€´´´´)±•Ð}™…Ù…ÑM•Ðõ¹•ÜM•Ð ¤ì)±•Ð}™…Ù¡…¹M•Ðõ¹•ÜM•Ð ¤ì)…Íå¹Œ™Õ¹Ñ¥½¸É•™É•Í¡…ÙMÑ…Ñ” ¥ì(€ÑÉåí½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ¤ì(€€€}™…Ù…ÑM•Ðõ¹•ÜM•Ð¡È¹…Ñ•½É¥•Íññmt¤ì(€€€}™…Ù¡…¹M•Ðõ¹•ÜM•Ð ¡È¹¡…¹¹•±Íññmt¤¹µ…À¡™Õ¹Ñ¥½¸¡Œ¥íÉ•ÑÕÉ¸MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤íô¤¤ì(€õ…Ñ ¡”¥íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘…Ù½É¥Ñ•Ì ¥ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ¤ì(€}™…Ù…ÑM•Ðõ¹•ÜM•Ð¡È¹…Ñ•½É¥•Íññmt¤ì(€}™…Ù¡…¹M•Ðõ¹•ÜM•Ð ¡È¹¡…¹¹•±Íññmt¤¹µ…À¡™Õ¹Ñ¥½¸¡Œ¥íÉ•ÑÕÉ¸MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤íô¤¤ì(€}µå1¥ÍÑ…Ù…Ñ„õÈì(€}µå1¥ÍÑ1½…‘•õÑÉÕ”ì(€}µå1¥ÍÑM•±•Ñ•‘¡…¹¹•±Ìô¡È¹µå±¥ÍÑ}¡…¹¹•±Íññmt¤¹µ…À¡MÑÉ¥¹œ¤¹Í±¥” À°Ô¤ì(€}µå1¥ÍÑQ•…µ5½µ•¹ÑÌõmtí}µå1¥ÍÑÅ5½µ•¹ÑÌõmtí}µå1¥ÍÑ5½Ù¥•5½µ•¹ÑÌõmtí}µå1¥ÍÑ…µ•5½µ•¹ÑÌõmtí}µå1¥ÍÑM¡½Ý5½µ•¹ÑÌõmtí}µå1¥ÍÑI…¥¹É¥Ù•ÉÌõmtì(€É•¹‘•É5å1¥ÍÑAÉ½™¥±” ¤ì(€…ÁÁ±å5å1¥ÍÑ1…å½ÕÐ ¤ì(€É•¹‘•É5å1¥ÍÑ¡…¹¹•±Ì ¤ì(€½¹ÍÐÉ…¥¹…Ñ…AÉ½µ¥Í”õ}˜Å¹…‰±•ý…Á¤ œ½…Á¤½É…¥¹œœ¤é¹Õ±°ì(€¥˜¡}™½½Ñ‰…±±¹…‰±•‘ññ}˜Å¹…‰±•¥±½…‘5å1¥ÍÑQ•…µÌ¡È±É…¥¹…Ñ…AÉ½µ¥Í”¤ì(€¥˜¡}˜Å¹…‰±•¥±½…‘5å1¥ÍÑI…¥¹œ¡É…¥¹…Ñ…AÉ½µ¥Í”¤í•±Í•í}µå1¥ÍÑÅ5½µ•¹ÑÌõmtíÍ¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤íô(€±½…‘5å1¥ÍÑ5½Ù¥•Ì ¤ì(€¥˜¡}…µ•Í¹…‰±•¥±½…‘5å1¥ÍÑ…µ•Ì¡È¤í•±Í•í}µå1¥ÍÑ…µ•5½µ•¹ÑÌõmtíÍ¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤íô(€±½…‘5å1¥ÍÑM¡½ÝÌ ¤ì)ô)±•Ð}µå1¥ÍÑ1½…‘•õ™…±Í”±}µå1¥ÍÑ…Ù…Ñ„õí¡…¹¹•±Ìémt±Ñ•…µÌémt±˜Å}Ñ•…µÌémuô±}µå1¥ÍÑM•±•Ñ•‘¡…¹¹•±Ìõmt±}µå1¥ÍÑQ•…µ5½µ•¹ÑÌõmt±}µå1¥ÍÑÅ5½µ•¹ÑÌõmt±}µå1¥ÍÑ5½Ù¥•5½µ•¹ÑÌõmt±}µå1¥ÍÑ…µ•5½µ•¹ÑÌõmt±}µå1¥ÍÑM¡½Ý5½µ•¹ÑÌõmt±}µå1¥ÍÑI…¥¹É¥Ù•ÉÌõmtì)±•Ð}µåQ¥µ•±¥¹•¥±Ñ•Èô…±°œ±}µåQ¥µ•±¥¹•M•ÑÑ¥¹ÌõíÉ••¹ÐéÑÉÕ”±±¥Ù”éÑÉÕ”±ÕÁ½µ¥¹œéÑÉÕ”±µ…áA•É…Ñ•½ÉäèÁô±}µåQ¥µ•±¥¹•AÉ•™Í1½…‘•õ™…±Í”ì)±•Ð}µå1¥ÍÑQ¥µ•±¥¹•I•¹‘•ÉA•¹‘¥¹œõ™…±Í”ì)™Õ¹Ñ¥½¸Í¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¥ì(€¥˜¡}µå1¥ÍÑQ¥µ•±¥¹•I•¹‘•ÉA•¹‘¥¹œ¥É•ÑÕÉ¸ì(€}µå1¥ÍÑQ¥µ•±¥¹•I•¹‘•ÉA•¹‘¥¹œõÑÉÕ”ì(€É•ÅÕ•ÍÑ¹¥µ…Ñ¥½¹É…µ”  ¤ôùí}µå1¥ÍÑQ¥µ•±¥¹•I•¹‘•ÉA•¹‘¥¹œõ™…±Í”íÉ•¹‘•É5å1¥ÍÑQ¥µ•±¥¹” ¤íô¤ì)ô)™Õ¹Ñ¥½¸É•¹‘•É5å1¥ÍÑ¡…¹¹•±Ì ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑ¡…¹¹•±Ìœ¤í¥˜ …•°¥É•ÑÕÉ¸ì(€½¹ÍÐ‰å%õ¹•Ü5…À ¡}µå1¥ÍÑ…Ù…Ñ„¹¡…¹¹•±Íññmt¤¹µ…À¡ŒôùmMÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤±t¤¤ì(€±•Ð ôœœì(€™½È¡±•Ð¤ôÀí¤ðÔí¤¬¬¥ì(€€€½¹ÍÐŒõ‰å%¹•Ð¡}µå1¥ÍÑM•±•Ñ•‘¡…¹¹•±Ím¥uñðœœ¤ì(€€€¥˜¡Œ¥ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡¡…¹¹•°ˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡Œ¹¹…µ•ñðœœ¤¬œˆÑ¥Ñ±”ôˆœ­•ÍÑÑÈ¡ÑÈ A±…äœ¤¤¬œˆøœ­¡…¹¹•±1½¼¡Œ¤¬œñÍÁ…¸±…ÍÌô‰µå‘…Í¡¡…¹¹•±¹…µ”ˆøœ­•ÍŒ¡Œ¹¹…µ”¤¬œð½ÍÁ…¸øñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Ù±Œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆø˜ŒäØÔàìY1ð½‰ÕÑÑ½¸øð½‘¥Øøœì(€€€•±Í” ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡¡…¹¹•°µÕÑ•ˆ½¹±¥¬ô‰Ñ½±•5å1¥ÍÑ¡…¹¹•±A¥­•È ¤ˆø¬€œ­ÑÈ ¡½½Í”¡…¹¹•±Ìœ¤¬œð½‘¥Øøœì(€ô(€•°¹¥¹¹•É!Q50õ ì)ô)™Õ¹Ñ¥½¸Ñ½±•5å1¥ÍÑ¡…¹¹•±A¥­•È ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑ¡…¹¹•±A¥­•Èœ¤ì(€¥˜ …•°¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥í•°¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÍ•±•Ñ•õ¹•ÜM•Ð¡}µå1¥ÍÑM•±•Ñ•‘¡…¹¹•±Ì¤ì(€¥˜ „¡}µå1¥ÍÑ…Ù…Ñ„¹¡…¹¹•±Íññmt¤¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ MÑ…È¡…¹¹•±Ì™¥ÉÍÐ°Ñ¡•¸¡½½Í”ÕÀÑ¼™¥Ù”¡•É”¸œ¤¬œð½ÍÁ…¸øœíô(€•±Í”•°¹¥¹¹•É!Q50õ}µå1¥ÍÑ…Ù…Ñ„¹¡…¹¹•±Ì¹µ…À¡Œôøœñ±…‰•°±…ÍÌô‰µå‘…Í¡¡½¥”ˆøñ¥¹ÁÕÐÑåÁ”ô‰¡•­‰½àˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆ€œ¬¡Í•±•Ñ•¹¡…Ì¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤ü¡•­•€œèœœ¤¬½¹¡…¹”ô‰Í•Ñ5å1¥ÍÑ¡…¹¹•°¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹Í¥±Ñ¡¥Ì¹¡•­•±Ñ¡¥Ì¤ˆøœ­¡…¹¹•±1½¼¡Œ°µ¥¹¤œ¤¬œñÍÁ…¸øœ­•ÍŒ¡Œ¹¹…µ”¤¬œð½ÍÁ…¸øð½±…‰•°øœ¤¹©½¥¸ œœ¤ì(€•°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Í•Ñ5å1¥ÍÑ¡…¹¹•°¡Í¥±¡•­•±¥¹ÁÕÐ¥ì(€Í¥õMÑÉ¥¹œ¡Í¥¤í±•Ð¡½Í•¸õ}µå1¥ÍÑM•±•Ñ•‘¡…¹¹•±Ì¹Í±¥” ¤ì(€¥˜¡¡•­•˜˜…¡½Í•¸¹¥¹±Õ‘•Ì¡Í¥¤¥ì(€€€¥˜¡¡½Í•¸¹±•¹Ñ øôÔ¥í¥¹ÁÕÐ¹¡•­•õ™…±Í”íÑ½…ÍÐ¡ÑÈ ¡½½Í”ÕÀÑ¼™¥Ù”¡…¹¹•±Ì¸œ¤¤íÉ•ÑÕÉ¸íô(€€€¡½Í•¸¹ÁÕÍ ¡Í¥¤ì(€õ•±Í”¥˜ …¡•­•¥¡½Í•¸õ¡½Í•¸¹™¥±Ñ•È¡¥ôù¥„ôõÍ¥¤ì(€½¹ÍÐÈõ…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÍ•Ñ}µå±¥ÍÑ}¡…¹¹•±Ìœ±ÍÑÉ•…µ}¥‘Ìé¡½Í•¹ô¤ì(€}µå1¥ÍÑM•±•Ñ•‘¡…¹¹•±Ìõ¡½Í•¸¹Í±¥” À°Ô¤íÉ•¹‘•É5å1¥ÍÑ¡…¹¹•±Ì ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Ñ½±•I…¥¹ÅA¥­•È ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹ÅA¥­•Èœ¤í¥˜ …•°¥É•ÑÕÉ¸ì(€¥˜ …•°¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥í•°¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íÉ•ÑÕÉ¸íô(€•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ 1½…‘¥¹œ¸¸¸œ¤¬œð½ÍÁ…¸øœí•°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€ÑÉåì(€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½˜Å}Ñ•…µÌœ¤°Í•±•Ñ•õMÑÉ¥¹œ  ¡È¹™…Ù½É¥Ñ•Íññmt¥lÁuññíô¤¹¥‘ñðœœ¤ì(€€€•°¹¥¹¹•É!Q50ô¡È¹Ñ•…µÍññmt¤¹µ…À¡Ñ•…´ôøœñ‰ÕÑÑ½¸±…ÍÌô‰µå‘…Í¡¡½¥”˜Å¡½¥”œ¬¡MÑÉ¥¹œ¡Ñ•…´¹¥¤ôôõÍ•±•Ñ•üœ½¸œèœœ¤¬œˆ‘…Ñ„µ¥ôˆœ­•ÍÑÑÈ¡Ñ•…´¹¥¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡Ñ•…´¹¹…µ”¤¬œˆ½¹±¥¬ô‰Í•ÑI…¥¹ÅQ•…´¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹¥±Ñ¡¥Ì¹‘…Ñ…Í•Ð¹¹…µ”¤ˆøñ¥µœÍÉŒôˆ½…Á¤½˜Å}Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡Ñ•…´¹¥‘ñðœœ¤¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøñÍÁ…¸øœ­•ÍŒ¡Ñ•…´¹¹…µ”¤¬œð½ÍÁ…¸øð½‰ÕÑÑ½¸øœ¤¹©½¥¸ œœ¥ñðœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ 9¼ÄÑ•…´Í•±•Ñ•¸œ¤¬œð½ÍÁ…¸øœì(€õ…Ñ ¡”¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ ½Õ±¹½Ð±½…½ÉµÕ±„€Ä…±•¹‘…È¸œ¤¬œð½ÍÁ…¸øœíô)ô)…Íå¹Œ™Õ¹Ñ¥½¸Í•ÑI…¥¹ÅQ•…´¡¥±¹…µ”¥ì(€±•ÐÕÉÉ•¹ÐôœœíÑÉåí½¹ÍÐÍÑ…Ñ”õ…Ý…¥Ð…Á¤ œ½…Á¤½˜Å}Ñ•…µÌœ¤íÕÉÉ•¹ÐõMÑÉ¥¹œ  ¡ÍÑ…Ñ”¹™…Ù½É¥Ñ•Íññmt¥lÁuññíô¤¹¥‘ñðœœ¤íõ…Ñ ¡”¥íô(€½¹ÍÐ±•…ÈõÕÉÉ•¹ÐôôõMÑÉ¥¹œ¡¥¤ì(€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÍ•Ñ}˜Å}Ñ•…´œ±Ñ•…´é±•…Èýíôéí¥é¥±¹…µ”é¹…µ•õô¤ì(€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% É…¥¹ÅA¥­•Èœ¤¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€…Ý…¥Ð±½…‘I…¥¹œ ¤í±½…‘…Ù½É¥Ñ•Ì ¤ì)ô)™Õ¹Ñ¥½¸µåMÁ½ÉÑQ•…µ5•Ñ„¡™¥áÑÕÉ•Ì¥ì(€½¹ÍÐ½Õ¹ÑÌõ¹•Ü5…À ¤±É½ÝÌô¡™¥áÑÕÉ•Íññmt¤¹™¥±Ñ•È¡˜ôù˜˜™˜¹±•…Õ•}¹…µ”˜˜„½™É¥•¹‘°½¤¹Ñ•ÍÐ¡MÑÉ¥¹œ¡˜¹±•…Õ•}¹…µ”¤¤¤ì(€™½È¡½¹ÍÐ˜½˜É½ÝÌ¥í½¹ÍÐ¹…µ”õMÑÉ¥¹œ¡˜¹±•…Õ•}¹…µ•ñðœœ¤¹ÑÉ¥´ ¤±­•äõ¹…µ”¹Ñ½1½Ý•É…Í” ¤í¥˜ …­•ä¥½¹Ñ¥¹Õ”í½¹ÍÐ½±õ½Õ¹ÑÌ¹•Ð¡­•ä¥ññí¹…µ”é¹…µ”±½Õ¹ÐèÁôí½±¹½Õ¹Ð¬¬í½Õ¹ÑÌ¹Í•Ð¡­•ä±½±¤íô(€½¹ÍÐ‰•ÍÐõÉÉ…ä¹™É½´¡½Õ¹ÑÌ¹Ù…±Õ•Ì ¤¤¹Í½ÉÐ ¡„±ˆ¤ôùˆ¹½Õ¹Ðµ„¹½Õ¹Ð¥lÁtí¥˜ …‰•ÍÐ¥É•ÑÕÉ¸€œœì(€½¹ÍÐ±½Üõ‰•ÍÐ¹¹…µ”¹Ñ½1½Ý•É…Í” ¤í±•Ð½Õ¹ÑÉäôœœì(€¥˜ ½ÁÉ•µ¥•È±•…Õ•ñ¡…µÁ¥½¹Í¡¥Áñ±•…Õ”½¹•ñ±•…Õ”ÑÝ¼¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäô¹±…¹œì(€•±Í”¥˜ ½•±¥Ñ•Í•É¥•¹ñ½‰½Ì¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäô9½ÉÝ…äœì(€•±Í”¥˜ ½±„€ý±¥„¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäôMÁ…¥¸œì(€•±Í”¥˜ ½‰Õ¹‘•Í±¥„¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäô•Éµ…¹äœì(€•±Í”¥˜ ½Í•É¥”„¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäô%Ñ…±äœì(€•±Í”¥˜ ½±¥Õ”€Ä¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäôÉ…¹”œì(€•±Í”¥˜ ½•É•‘¥Ù¥Í¥”¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäô9•Ñ¡•É±…¹‘Ìœì(€•±Í”¥˜ ½ÁÉ¥µ•¥É„±¥„¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäôA½ÉÑÕ…°œì(€•±Í”¥˜ ½ÁÉ•µ¥•ÉÍ¡¥À¼¹Ñ•ÍÐ¡±½Ü¤¥½Õ¹ÑÉäôM½Ñ±…¹œì(€É•ÑÕÉ¸€¡½Õ¹ÑÉäý½Õ¹ÑÉä¬œƒ\€œèœœ¤­‰•ÍÐ¹¹…µ”ì)ô)™Õ¹Ñ¥½¸É•¹‘•É5å1¥ÍÑMÁ½ÉÑM¡•±±Ì¡™…Ù½É¥Ñ•Ì¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑQ•…µÌœ¤±Ñ•…µÌô¡™…Ù½É¥Ñ•Ì¹Ñ•…µÍññmt¤í¥˜ …•°¥É•ÑÕÉ¸ì(€±•Ð ôœœì(€¥˜¡}™½½Ñ‰…±±¹…‰±•¥™½È¡½¹ÍÐÑ•…´½˜Ñ•…µÌ¥ì(€€€½¹ÍÐ¹…µ”õMÑÉ¥¹œ¡ÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœýÑ•…´éÑ•…´¹¹…µ•ñðœœ¤±¥õÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœüœœéMÑÉ¥¹œ¡Ñ•…´¹Ñ•…µ}¥‘ñðœœ¤±±½¼õÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœüœœè¡Ñ•…´¹±½½ñðœœ¤±ÍÉŒõ±½½ñð¡¥üœ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡¥¤èœœ¤ì(€€€¥˜¡}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ¥ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…µ½¹±äˆ½¹±¥¬ô‰Í¡½ÝQ•…µÌ ¤ˆøœ¬¡ÍÉŒüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±”ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±•Ñ½Àˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹…µ”ˆøœ­•ÍŒ¡¹…µ”¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ•Ù•¹Ñ±¥¹”ˆøñÍÁ…¸±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹•áÐµÕÑ•ˆøœ­•ÍŒ¡ÑÈ 1½…‘¥¹œ™¥áÑÕÉ”¸¸¸œ¤¤¬œð½ÍÁ…¸øð½‘¥Øøð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€€€•±Í” ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡™¥áÑÕÉ”ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…´ˆøœ¬¡ÍÉŒüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñÍÁ…¸øœ­•ÍŒ¡¹…µ”¤¬œð½ÍÁ…¸øð½‘¥ØøñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡ÑÈ 1½…‘¥¹œ™¥áÑÕÉ”¸¸¸œ¤¤¬œð½ÍÁ…¸øð½‘¥Øøœì(€ô(€¥˜¡}˜Å¹…‰±•¥ì(€€€½¹ÍÐÍ•±•Ñ•õ¹•ÜM•Ð ¡}ÁÉ½™¥±•½¹™¥œ¹É…¥¹}Í•É¥•Íññl˜Ät¤¹µ…À¡MÑÉ¥¹œ¤¤ì(€€€¥˜¡}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ¥ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍÕ‰¡•…É…¥¹œˆùI…¥¹œð½‘¥Øøœì(€€€¥˜¡Í•±•Ñ•¹¡…Ì ˜Äœ¤¥ì(€€€€€½¹ÍÐÑ•…´ô ¡™…Ù½É¥Ñ•Ì¹˜Å}Ñ•…µÍññmt¥lÁuññíô¤±¹…µ”õÑ•…´¹¹…µ•ñð½ÉµÕ±„€Äœ±ÍÉŒõÑ•…´¹±½½ñð¡Ñ•…´¹¥üœ½…Á¤½˜Å}Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡Ñ•…´¹¥¤¤èœœ¤ì(€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…µ½¹±äµå‘…Í¡˜Å…Éˆ‘…Ñ„µ‘É¥Ù•Èµ­•äô‰˜ÄµÑ•…´ˆ½¹±¥¬ô‰Í¡½ÝI…¥¹œ¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹‘É¥Ù•É-•ä¤ˆøœ¬¡ÍÉŒüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±”ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹…µ”ˆøœ­•ÍŒ¡¹…µ”¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑµ•Ñ„ˆù½ÉµÕ±„€Äƒ
Ü€œ­•ÍŒ¡ÑÈ 1½…‘¥¹œ‘É¥Ù•ÉÌ…¹¹•áÐÉ…”¸¸¸œ¤¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€€€ô(€€€½¹ÍÐÅÕ¥¬õmlÝÉŒœ°ÝÉŒµ½±¥Ù•ÈµÍ½±‰•Éœœ°=±¥Ù•ÈM½±‰•Éœœ°]Iƒ\Q½å½Ñ„…é½¼I…¥¹œœ±™…±Í•t±l¥¹‘å…Èœ°¥¹‘å…Èµ‘•¹¹¥Ìµ¡…Õ•Èœ°•¹¹¥Ì!…Õ•Èœ°%¹‘å…Èƒ\…±”½å¹”I…¥¹œœ±™…±Í•t±l˜Èœ°˜Èµµ…ÉÑ¥¹¥ÕÌµÍÑ•¹Í¡½É¹”œ°5…ÉÑ¥¹¥ÕÌMÑ•¹Í¡½É¹”œ°½ÉµÕ±„€Èƒ\I½‘¥¸5½Ñ½ÉÍÁ½ÉÐœ±ÑÉÕ•utì(€€€™½È¡½¹ÍÐÉ½Ü½˜ÅÕ¥¬¥í¥˜ …Í•±•Ñ•¹¡…Ì¡É½ÝlÁt¤¥½¹Ñ¥¹Õ”í½¹ÍÐÍÉŒôœ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡É½ÝlÅt¤í ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…µ½¹±äˆ‘…Ñ„µ‘É¥Ù•Èµ­•äôˆœ­•ÍÑÑÈ¡É½ÝlÅt¤¬œˆ½¹±¥¬ô‰Í¡½ÝI…¥¹œ¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹‘É¥Ù•É-•ä¤ˆøñ¥µœ±…ÍÌô‰‘É¥Ù•Èœ¬¡É½ÝlÑtüœ…Èœèœœ¤¬œˆÍÉŒôˆœ­ÍÉŒ¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±”ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹…µ”ˆøœ­•ÍŒ¡É½ÝlÉt¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑµ•Ñ„ˆøœ­•ÍŒ¡É½ÝlÍt¤¬œƒ
Ü€œ­•ÍŒ¡ÑÈ 1½…‘¥¹œ¹•áÐÉ…”¸¸¸œ¤¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœíô(€ô(€•°¹¥¹¹•É!Q50õ¡ñðœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ¡}˜Å¹…‰±•ü9¼ÄÑ•…´Í•±•Ñ•¸œè9¼™…Ù½É¥Ñ”Ñ•…µÌå•Ð¸œ¤¬œð½ÍÁ…¸øœì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘5å1¥ÍÑQ•…µÌ¡™…Ù½É¥Ñ•Ì±É…¥¹…Ñ…AÉ½µ¥Í”¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑQ•…µÌœ¤±Ñ•…µÌô¡™…Ù½É¥Ñ•Ì¹Ñ•…µÍññmt¤±¹½Üõ…Ñ”¹¹½Ü ¤ì(€}µå1¥ÍÑQ•…µ5½µ•¹ÑÌõmtí±•Ð ôœœì(€É•¹‘•É5å1¥ÍÑMÁ½ÉÑM¡•±±Ì¡™…Ù½É¥Ñ•Ì¤ì(€½¹ÍÐÉ…¥¹AÉ½µ¥Í”õ}˜Å¹…‰±•ýAÉ½µ¥Í”¹…±°¡m…Á¤ œ½…Á¤½É…¥¹}‘É¥Ù•ÉÌœ¤±É…¥¹…Ñ…AÉ½µ¥Í•ññ…Á¤ œ½…Á¤½É…¥¹œœ¥t¤é¹Õ±°ì(€¥˜¡}™½½Ñ‰…±±¹…‰±•˜™Ñ•…µÌ¹±•¹Ñ ¥ì(€€€ÑÉåì(€€€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½µå}Ñ•…µÌœ¤±™¥áÑÕÉ•ÌõÈ¹™¥áÑÕÉ•Íññmtì(€€€€€™½È¡½¹ÍÐÑ•…´½˜Ñ•…µÌ¥ì(€€€€€€€½¹ÍÐ¹…µ”õMÑÉ¥¹œ¡ÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœýÑ•…´éÑ•…´¹¹…µ•ñðœœ¤±­•äõ¹…µ”¹Ñ½1½Ý•É…Í” ¤ì(€€€€€€€½¹ÍÐµ¥¹”õ™¥áÑÕÉ•Ì¹™¥±Ñ•È¡˜ôø¡˜¹™…Ù½É¥Ñ•}Ñ•…µÍññmt¤¹Í½µ”¡½Ý¹•ÈôùMÑÉ¥¹œ¡½Ý¹•È¤¹Ñ½1½Ý•É…Í” ¤ôôõ­•ä¤¤ì(€€€€€€€½¹ÍÐ±¥Ù”õµ¥¹”¹™¥±Ñ•È¡˜ôù˜¹¥Í}±¥Ù”¤¹Í½ÉÐ ¡„±ˆ¤ôùMÑÉ¥¹œ¡„¹ÍÑ…ÉÐ¤¹±½…±•½µÁ…É”¡MÑÉ¥¹œ¡ˆ¹ÍÑ…ÉÐ¤¤¥lÁtì(€€€€€€€½¹ÍÐÕÁ½µ¥¹œõµ¥¹”¹™¥±Ñ•È¡˜ôùí½¹ÍÐÑÌõ˜¹ÍÑ…ÉÐý¹•Ü…Ñ”¡˜¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤èÀíÉ•ÑÕÉ¸ÑÌù¹½Üíô¤¹Í½ÉÐ ¡„±ˆ¤ôù¹•Ü…Ñ”¡„¹ÍÑ…ÉÐ¤µ¹•Ü…Ñ”¡ˆ¹ÍÑ…ÉÐ¤¤ì(€€€€€€€½¹ÍÐ¹•áÐõÕÁ½µ¥¹lÁtì(€€€€€€€½¹ÍÐ™¥áÑÕÉ”õ±¥Ù•ññ¹•áÐ±¥õÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœüœœéMÑÉ¥¹œ¡Ñ•…´¹Ñ•…µ}¥‘ñðœœ¤±±½¼õÑåÁ•½˜Ñ•…´ôôôÍÑÉ¥¹œœüœœè¡Ñ•…´¹±½½ñðœœ¤±ÍÉŒõ±½½ñð¡¥üœ½…Á¤½Ñ•…µ}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡¥¤èœœ¤ì(€€€€€€€¥˜¡±¥Ù”¥}µå1¥ÍÑQ•…µ5½µ•¹ÑÌ¹ÁÕÍ ¡íÑ•…´é¹…µ”±™¥áÑÕÉ”é±¥Ù”±±¥Ù”éÑÉÕ”±±½¼éÍÉŒ±ÑÌé…Ñ”¹¹½Ü ¥ô¤ì(€€€€€€€™½È¡½¹ÍÐ™ÕÑÕÉ”½˜ÕÁ½µ¥¹œ¹Í±¥” À°Ð¤¥í½¹ÍÐÑÌõ™ÕÑÕÉ”¹ÍÑ…ÉÐý¹•Ü…Ñ”¡™ÕÑÕÉ”¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤èÀí¥˜¡ÑÌ¥}µå1¥ÍÑQ•…µ5½µ•¹ÑÌ¹ÁÕÍ ¡íÑ•…´é¹…µ”±™¥áÑÕÉ”é™ÕÑÕÉ”±±¥Ù”é™…±Í”±±½¼éÍÉŒ±ÑÌéÑÍô¤íô(€€€€€€€¥˜¡}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ¥ì(€€€€€€€€€½¹ÍÐ™¥áÑÕÉ•Q•áÐõ™¥áÑÕÉ”ü ¡™¥áÑÕÉ”¹¡½µ•ñðœœ¤¬œØ€œ¬¡™¥áÑÕÉ”¹…Ý…åñðœœ¤¤éÑÈ 9¼ÕÁ½µ¥¹œ™¥áÑÕÉ”™½Õ¹¸œ¤ì(€€€€€€€€€½¹ÍÐ½Õ¹Ñ‘½Ý¸õ™¥áÑÕÉ”ü¡±¥Ù”ü1%YœéÉ…¥¹½Õ¹Ñ‘½Ý¸¡íÍÑ…ÉÐé™¥áÑÕÉ”¹ÍÑ…ÉÑô¤¤èœœì(€€€€€€€€€½¹ÍÐÑ•…µ5•Ñ„õµåMÁ½ÉÑQ•…µ5•Ñ„¡µ¥¹”¤ì(€€€€€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…µ½¹±äˆ½¹±¥¬ô‰Í¡½ÝQ•…µÌ ¤ˆøœ¬¡ÍÉŒüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±”ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±•Ñ½Àˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹…µ”ˆøœ­•ÍŒ¡¹…µ”¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ•Ù•¹Ñ±¥¹”ˆøñÍÁ…¸±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹•áÐˆøœ­•ÍŒ¡™¥áÑÕÉ•Q•áÐ¤¬œð½ÍÁ…¸øœ¬¡½Õ¹Ñ‘½Ý¸üœñÍÁ…¸±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ½Õ¹Ðˆøœ­•ÍŒ¡½Õ¹Ñ‘½Ý¸¤¬œð½ÍÁ…¸øœèœœ¤¬œð½‘¥Øøð½‘¥Øøœ¬¡Ñ•…µ5•Ñ„üœñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑµ•Ñ„ˆøœ­•ÍŒ¡Ñ•…µ5•Ñ„¤¬œð½‘¥Øøœèœœ¤¬œð½‘¥Øøð½‘¥Øøœì(€€€€€€€ô(€€€€€€€•±Í•í ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡™¥áÑÕÉ”ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…´ˆøœ¬¡ÍÉŒüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ¤¬œñÍÁ…¸øœ­•ÍŒ¡¹…µ”¤¬œð½ÍÁ…¸øð½‘¥Øøœ¬¡™¥áÑÕÉ”ýÑ•…µ¥áÑÕÉ•…É¡™¥áÑÕÉ”°„…™¥áÑÕÉ”¹¥Í}±¥Ù”¤èœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ 9¼ÕÁ½µ¥¹œ™¥áÑÕÉ”™½Õ¹¸œ¤¬œð½ÍÁ…¸øœ¤¬œð½‘¥Øøœíô(€€€€€ô(€€€õ…Ñ ¡”¥í ¬ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ ½Õ±¹½Ð±½…Ñ•…´™¥áÑÕÉ•Ì¸œ¤¬œð½ÍÁ…¸øœíô(€ô(€¥˜¡}˜Å¹…‰±•¥ì(€€€ÑÉåì(€€€€€½¹ÍÐm‘É¥Ù•É…Ñ„±É…¥¹…Ñ…tõ…Ý…¥ÐÉ…¥¹AÉ½µ¥Í”ì(€€€€€¥˜¡}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ˜˜¡‘É¥Ù•É…Ñ„¹‘É¥Ù•ÉÍññmt¤¹±•¹Ñ ¥ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍÕ‰¡•…É…¥¹œˆùI…¥¹œð½‘¥Øøœì(€€€€€½¹ÍÐ…±±É¥Ù•ÉÌõ‘É¥Ù•É…Ñ„¹‘É¥Ù•ÉÍññmtí}µå1¥ÍÑI…¥¹É¥Ù•ÉÌõ…±±É¥Ù•ÉÌì(€€€€€½¹ÍÐ˜ÅÉ¥Ù•ÉÌõ…±±É¥Ù•ÉÌ¹™¥±Ñ•È¡‘É¥Ù•ÈôùMÑÉ¥¹œ¡‘É¥Ù•È¹Í•É¥•Íñðœœ¤ôôô˜Äœ¤ì(€€€€€¥˜¡}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ¥ì(€€€€€€€½¹ÍÐ…É‘Ìõmtì(€€€€€€€¥˜¡˜ÅÉ¥Ù•ÉÌ¹±•¹Ñ ¥í½¹ÍÐ¹•áÐõ¹•áÑÉ¥Ù•ÉI…”¡˜ÅÉ¥Ù•ÉÍlÁt±É…¥¹…Ñ„¹•Ù•¹ÑÍññmt±¹½Ü¤í…É‘Ì¹ÁÕÍ ¡í­¥¹è˜Äœ±‘É¥Ù•ÉÌé˜ÅÉ¥Ù•ÉÌ±¹•áÐé¹•áÐ±ÑÌé¹•áÐý¹•Ü…Ñ”¡¹•áÐ¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤é%¹™¥¹¥Ñåô¤íô(€€€€€€€™½È¡½¹ÍÐ‘É¥Ù•È½˜…±±É¥Ù•ÉÌ¥í¥˜¡MÑÉ¥¹œ¡‘É¥Ù•È¹Í•É¥•Íñðœœ¤ôôô˜Äœ¥½¹Ñ¥¹Õ”í½¹ÍÐ¹•áÐõ¹•áÑÉ¥Ù•ÉI…”¡‘É¥Ù•È±É…¥¹…Ñ„¹•Ù•¹ÑÍññmt±¹½Ü¤í…É‘Ì¹ÁÕÍ ¡í­¥¹è‘É¥Ù•Èœ±‘É¥Ù•Èé‘É¥Ù•È±¹•áÐé¹•áÐ±ÑÌé¹•áÐý¹•Ü…Ñ”¡¹•áÐ¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¤é%¹™¥¹¥Ñåô¤íô(€€€€€€€€¼¼I…¥¹œ™½±±½ÝÌÑ¡”…±•¹‘…Èè¹•…É•ÍÐ¹•áÐ•Ù•¹Ð™¥ÉÍÐ¸½½Ñ‰…±°Ñ•…´(€€€€€€€€¼¼…É‘Ì…‰½Ù”‘•±¥‰•É…Ñ•±äÉ•Ñ…¥¸Ñ¡”ÕÍ•ÈÌ™…Ù½É¥Ñ”½½É‘•ÈÍ•ÅÕ•¹”¸(€€€€€€€…É‘Ì¹Í½ÉÐ ¡„±ˆ¤ôø¡9Õµ‰•È¹¥Í¥¹¥Ñ”¡„¹ÑÌ¤ý„¹ÑÌé%¹™¥¹¥Ñä¤´¡9Õµ‰•È¹¥Í¥¹¥Ñ”¡ˆ¹ÑÌ¤ýˆ¹ÑÌé%¹™¥¹¥Ñä¤¤ì(€€€€€€€™½È¡½¹ÍÐ…É½˜…É‘Ì¥ì(€€€€€€€€€½¹ÍÐ¹•áÐõ…É¹¹•áÐ±½Õ¹Ñ‘½Ý¸õ¹•áÐýÉ…¥¹½Õ¹Ñ‘½Ý¸¡¹•áÐ¤èœœì(€€€€€€€€€¥˜¡…É¹­¥¹ôôô˜Äœ¥ì(€€€€€€€€€€€½¹ÍÐ‘É¥Ù•ÉÌõ…É¹‘É¥Ù•ÉÌ±±¥Ù”ô¡É…¥¹…Ñ„¹•Ù•¹ÑÍññmt¤¹™¥±Ñ•È¡”ôùMÑÉ¥¹œ¡”¹Í•É¥•Íñðœœ¤ôôô˜Äœ¤¹Í½µ”¡”ôùÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡”±¹½Ü¤¤±Ñ•…´õ‘É¥Ù•ÉÍlÁt¹Ñ•…µñðœœì(€€€€€€€€€€€½¹ÍÐÁ¡½Ñ½Ìõ‘É¥Ù•ÉÌ¹Í±¥” À°È¤¹µ…À¡‘É¥Ù•Èôøœñ¥µœ±…ÍÌô‰‘É¥Ù•ÈˆÍÉŒôˆ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœ¤¹©½¥¸ œœ¤ì(€€€€€€€€€€€½¹ÍÐ¹…µ•Ìõ‘É¥Ù•ÉÌ¹Í±¥” À°È¤¹µ…À¡‘É¥Ù•Èôøœñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹…µ”ˆøœ­•ÍŒ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œð½‘¥Øøœ¤¹©½¥¸ œœ¤±É…”õ¹•áÐü¡¹•áÐ¹É…•ññ¹•áÐ¹¥ÉÕ¥ÑññÑÈ 9•áÐÉ…”œ¤¤éÑÈ 9¼ÕÁ½µ¥¹œÉ…”™½Õ¹¸œ¤ì(€€€€€€€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…µ½¹±äµå‘…Í¡˜Å…Éˆ‘…Ñ„µ‘É¥Ù•Èµ­•äô‰˜ÄµÑ•…´ˆ½¹±¥¬ô‰Í¡½ÝI…¥¹œ¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹‘É¥Ù•É-•ä¤ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÁ¡½Ñ½Ìˆøœ­Á¡½Ñ½Ì¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±”ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±•Ñ½Àˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡˜Å¹…µ•Ìˆøœ­¹…µ•Ì¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ•Ù•¹Ñ±¥¹”ˆøñÍÁ…¸±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹•áÐˆøœ­•ÍŒ¡É…”¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ½Õ¹Ðˆøœ­•ÍŒ¡±¥Ù”ýÑÈ I¥¡Ð¹½Üœ¤è¡½Õ¹Ñ‘½Ý¹ñðœœ¤¤¬œð½ÍÁ…¸øð½‘¥Øøð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑµ•Ñ„ˆù½ÉµÕ±„€Äœ¬¡Ñ•…´üœƒ\€œ­•ÍŒ¡Ñ•…´¤èœœ¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€€€€€€€€€õ•±Í•ì(€€€€€€€€€€€½¹ÍÐ‘É¥Ù•Èõ…É¹‘É¥Ù•È±±¥Ù”ô¡É…¥¹…Ñ„¹•Ù•¹ÑÍññmt¤¹™¥±Ñ•È¡”ôùMÑÉ¥¹œ¡”¹Í•É¥•Íñðœœ¤ôôõMÑÉ¥¹œ¡‘É¥Ù•È¹Í•É¥•Íñðœœ¤¤¹Í½µ”¡”ôùÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡”±¹½Ü¤¤±ÍÉŒôœ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤¤ì(€€€€€€€€€€€½¹ÍÐµ•Ñ„õm‘É¥Ù•È¹Í•É¥•Í}¹…µ•ñðI…¥¹œœ±‘É¥Ù•È¹Ñ•…µñðœt¹™¥±Ñ•È¡	½½±•…¸¤¹©½¥¸ œƒ\€œ¤±¹•áÑQ•áÐõ¹•áÐü¡¹•áÐ¹É…•ññ¹•áÐ¹¥ÉÕ¥ÑññÑÈ 9•áÐÉ…”œ¤¤éÑÈ 9¼ÕÁ½µ¥¹œÉ…”™½Õ¹¸œ¤±¥µ…•±…ÍÌô‘É¥Ù•Èœ¬¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤ôôô˜Èµµ…ÉÑ¥¹¥ÕÌµÍÑ•¹Í¡½É¹”œüœ…Èœèœœ¤ì(€€€€€€€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…µ½¹±äˆ‘…Ñ„µ‘É¥Ù•Èµ­•äôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤¤¬œˆ½¹±¥¬ô‰Í¡½ÝI…¥¹œ¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹‘É¥Ù•É-•ä¤ˆøñ¥µœ±…ÍÌôˆœ­¥µ…•±…ÍÌ¬œˆÍÉŒôˆœ­ÍÉŒ¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±”ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑÍ¥¹±•Ñ½Àˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹…µ”ˆøœ­•ÍŒ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ•Ù•¹Ñ±¥¹”ˆøñÍÁ…¸±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ¹•áÐˆøœ­•ÍŒ¡¹•áÑQ•áÐ¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ½Õ¹Ðˆøœ­•ÍŒ¡±¥Ù”ýÑÈ I¥¡Ð¹½Üœ¤è¡½Õ¹Ñ‘½Ý¹ñðœœ¤¤¬œð½ÍÁ…¸øð½‘¥Øøð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑµ•Ñ„ˆøœ­•ÍŒ¡µ•Ñ„¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€€€€€€€€€ô(€€€€€€€ô(€€€€€õ•±Í”™½È¡½¹ÍÐ‘É¥Ù•È½˜…±±É¥Ù•ÉÌ¥ì(€€€€€€€½¹ÍÐ±¥Ù”ô¡É…¥¹…Ñ„¹•Ù•¹ÑÍññmt¤¹™¥±Ñ•È¡”ôùMÑÉ¥¹œ¡”¹Í•É¥•Íñðœœ¤ôôõMÑÉ¥¹œ¡‘É¥Ù•È¹Í•É¥•Íñðœœ¤¤¹Í½µ”¡”ôùÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡”±¹½Ü¤¤±ÍÉŒôœ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤¤ì(€€€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå‘…Í¡™¥áÑÕÉ”ˆ‘…Ñ„µ‘É¥Ù•Èµ­•äôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡‘É¥Ù•È¹­•åñðœœ¤¤¬œˆ½¹±¥¬ô‰Í¡½ÝI…¥¹œ¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹‘É¥Ù•É-•ä¤ˆÍÑå±”ô‰ÕÉÍ½ÈéÁ½¥¹Ñ•Èˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ñ•…´ˆøñ¥µœÍÉŒôˆœ­ÍÉŒ¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøñÍÁ…¸øœ­•ÍŒ¡‘É¥Ù•È¹¹…µ•ñðœœ¤¬œð½ÍÁ…¸øð½‘¥ØøñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­•ÍŒ¡‘É¥Ù•È¹Í•É¥•Í}¹…µ•ñðI…¥¹œœ¤¬¡‘É¥Ù•È¹Ñ•…´üœƒ
Ü€œ­•ÍŒ¡‘É¥Ù•È¹Ñ•…´¤èœœ¤¬œð½ÍÁ…¸øœ¬¡±¥Ù”üœñÍÁ…¸±…ÍÌô‰µå‘…Í¡ÍÁ½ÉÑ½Õ¹Ðˆøœ­•ÍŒ¡ÑÈ I¥¡Ð¹½Üœ¤¤¬œð½ÍÁ…¸øœèœœ¤¬œð½‘¥Øøœì(€€€€€ô(€€€õ…Ñ ¡”¥íô(€ô(€•°¹¥¹¹•É!Q50õ¡ñðœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ¡}˜Å¹…‰±•ü9¼ÄÑ•…´Í•±•Ñ•¸œè9¼™…Ù½É¥Ñ”Ñ•…µÌå•Ð¸œ¤¬œð½ÍÁ…¸øœì(€Í¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘5å1¥ÍÑI…¥¹œ¡É…¥¹…Ñ…AÉ½µ¥Í”¥ì(€}µå1¥ÍÑÅ5½µ•¹ÑÌõmtì(€ÑÉåì(€€€½¹ÍÐÈõ…Ý…¥Ð€¡É…¥¹…Ñ…AÉ½µ¥Í•ññ…Á¤ œ½…Á¤½É…¥¹œœ¤¤±¹½Üõ…Ñ”¹¹½Ü ¤ì(€€€½¹ÍÐÉ½ÕÁÌõ¹•Ü5…À ¤ì(€€€™½È¡½¹ÍÐ•Ù•¹Ð½˜€¡È¹•Ù•¹ÑÍññmt¤¥ì(€€€€€½¹ÍÐÉ½Üõí•Ù•¹Ðé•Ù•¹Ð±ÑÌé¹•Ü…Ñ”¡•Ù•¹Ð¹ÍÑ…ÉÐ¤¹•ÑQ¥µ” ¥ô±±¥Ù”õÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡•Ù•¹Ð±¹½Ü¤ì(€€€€€¥˜ …9Õµ‰•È¹¥Í¥¹¥Ñ”¡É½Ü¹ÑÌ¥ñð …±¥Ù”˜™É½Ü¹ÑÌðõ¹½Ü´È¨ÌØÀÀÀÀÀ¤¥½¹Ñ¥¹Õ”ì(€€€€€½¹ÍÐ­•äõMÑÉ¥¹œ¡•Ù•¹Ð¹Í•É¥•ÍñðÉ…¥¹œœ¤ì(€€€€€¥˜ …É½ÕÁÌ¹¡…Ì¡­•ä¤¥É½ÕÁÌ¹Í•Ð¡­•ä±mt¤ì(€€€€€É½ÕÁÌ¹•Ð¡­•ä¤¹ÁÕÍ ¡É½Ü¤ì(€€€ô(€€€€¼¼-••ÀÑ¡”Ñ¥µ•±¥¹”‰…±…¹•Ý¡•¸Í•Ù•É…°¡…µÁ¥½¹Í¡¥ÁÌ…É”•¹…‰±•è(€€€€¼¼„Í•ÍÍ¥½¸µ¡•…ÙäÄÝ••­•¹Í¡½Õ±¹½ÐÉ½Ý]I½5½Ñ½@½•ÑŒ¸½™˜¥Ð¸(€€€}µå1¥ÍÑÅ5½µ•¹ÑÌõÉÉ…ä¹™É½´¡É½ÕÁÌ¹Ù…±Õ•Ì ¤¤¹™±…Ñ5…À¡É½ÝÌôùÉ½ÝÌ¹Í½ÉÐ ¡„±ˆ¤ôù„¹ÑÌµˆ¹ÑÌ¤¹Í±¥” À°Ì¤¤¹Í½ÉÐ ¡„±ˆ¤ôù„¹ÑÌµˆ¹ÑÌ¤¹Í±¥” À°Äà¤ì(€€€Í¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤ì(€€€ÑÉåí½¹ÍÐ„õ…Ý…¥Ð…Á¤ œ½…Á¤½É…¥¹}…Ù…¥±…‰¥±¥Ñäœ¤í™½È¡½¹ÍÐÉ½Ü½˜}µå1¥ÍÑÅ5½µ•¹ÑÌ¥É½Ü¹•Ù•¹Ð¹¡…¹¹•±Ìô¡„¹…Ù…¥±…‰¥±¥Ñåññíô¥mÉ…¥¹Ù…¥±…‰¥±¥Ñå-•ä¡É½Ü¹•Ù•¹Ð¥uññmtíõ…Ñ ¡”¥íô(€õ…Ñ ¡”¥íô(€Í¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘5å1¥ÍÑ5½Ù¥•Ì ¥ì(€}µå1¥ÍÑ5½Ù¥•5½µ•¹ÑÌõmtì(€ÑÉåì(€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•}µ½Ù¥•}ÍÑ…ÑÕÌœ¤±Ý¥¹‘½Ý5ÌôÈ¨ÈÐ¨ÌØÀÀÀÀÀ±¹½Üõ…Ñ”¹¹½Ü ¤ì(€€€}µå1¥ÍÑ5½Ù¥•5½µ•¹ÑÌô¡È¹µ½Ù¥•Íññmt¤¹µ…À¡µ½Ù¥”ôø¡íµ½Ù¥”éµ½Ù¥”±ÑÌé…Ñ”¹Á…ÉÍ”¡µ½Ù¥”¹É•±•…Í•‘ñðœœ¥ô¤¤¹™¥±Ñ•È¡É½Üôù9Õµ‰•È¹¥Í¥¹¥Ñ”¡É½Ü¹ÑÌ¤˜™5…Ñ ¹…‰Ì¡É½Ü¹ÑÌµ¹½Ü¤ðõÝ¥¹‘½Ý5Ì¤¹Í½ÉÐ ¡„±ˆ¤ôù5…Ñ ¹…‰Ì¡„¹ÑÌµ¹½Ü¤µ5…Ñ ¹…‰Ì¡ˆ¹ÑÌµ¹½Ü¤¤¹Í±¥” À°Ð¤ì(€õ…Ñ ¡”¥íô(€Í¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤ì)ô)™Õ¹Ñ¥½¸±½…‘5å1¥ÍÑ…µ•Ì¡™…Ù½É¥Ñ•Ì¥ì(€½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤±É••¹ÑÕÑ½™˜õ¹½Ü´Ü¨ÈÐ¨ÌØÀÀÀÀÀì(€}µå1¥ÍÑ…µ•5½µ•¹ÑÌô¡™…Ù½É¥Ñ•Ì¹…µ•Íññmt¤¹™¥±Ñ•È¡…µ”ôù…µ”¹Ý¥Í¡±¥ÍÑ}¥µÁ½ÉÑ•¤¹µ…À¡…µ”ôø¡í…µ”é…µ”±ÑÌé…Ñ”¹Á…ÉÍ”¡…µ”¹É•±•…Í•‘ñðœœ¥ô¤¤¹™¥±Ñ•È¡É½Üôù9Õµ‰•È¹¥Í¥¹¥Ñ”¡É½Ü¹ÑÌ¤˜™É½Ü¹ÑÌøõÉ••¹ÑÕÑ½™˜¤¹Í½ÉÐ ¡„±ˆ¤ôù5…Ñ ¹…‰Ì¡„¹ÑÌµ¹½Ü¤µ5…Ñ ¹…‰Ì¡ˆ¹ÑÌµ¹½Ü¤¤¹Í±¥” À°Ð¤ì(€Í¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤ì)ô)™Õ¹Ñ¥½¸µå1¥ÍÑÁ¥Í½‘•]¡•¸¡ÑÌ±ÕÁ½µ¥¹œ¥ì(€½¹ÍÐ‘•±Ñ„õÑÌµ…Ñ”¹¹½Ü ¤ì(€¥˜¡ÕÁ½µ¥¹œ¥í½¹ÍÐ¡½ÕÉÌõ5…Ñ ¹µ…à Ä±5…Ñ ¹•¥°¡‘•±Ñ„¼ÌØÀÀÀÀÀ¤¤íÉ•ÑÕÉ¸ÑÈ ¥ÉÌ¥¸œ¤¬œ€œ­¡½ÕÉÌ¬œ€œ­ÑÈ¡¡½ÕÉÌôôôÄü¡½ÕÈœè¡½ÕÉÌœ¤íô(€½¹ÍÐ¡½ÕÉÌõ5…Ñ ¹µ…à À±5…Ñ ¹É½Õ¹¡5…Ñ ¹…‰Ì¡‘•±Ñ„¤¼ÌØÀÀÀÀÀ¤¤ì(€¥˜¡¡½ÕÉÌðôÄÈ¥É•ÑÕÉ¸ÑÈ )ÕÍÐÉ•±•…Í•œ¤ì(€É•ÑÕÉ¸ÑÈ I•±•…Í•œ¤¬œ€œ­¡½ÕÉÌ¬œ€œ­ÑÈ¡¡½ÕÉÌôôôÄü¡½ÕÈœè¡½ÕÉÌœ¤¬œ€œ­ÑÈ …¼œ¤ì)ô)™Õ¹Ñ¥½¸Ñ¥µ•±¥¹•UÁ½µ¥¹]¡•¸¡ÑÌ±‘…Ñ•=¹±ä¥ì(€½¹ÍÐÑ…É•Ðõ¹•Ü…Ñ”¡ÑÌ¤±¹½Üõ¹•Ü…Ñ” ¤±‘•±Ñ„õÑ…É•Ðµ¹½Ü±±½…±”õ}±…¹œôôô¹¼œü¹ˆµ9<œéÕ¹‘•™¥¹•ì(€¥˜ …9Õµ‰•È¹¥Í¥¹¥Ñ”¡‘•±Ñ„¤¥É•ÑÕÉ¸€œœì(€¥˜ …‘…Ñ•=¹±ä˜™‘•±Ñ„øÀ˜™‘•±Ñ„ðÈÐ¨ÌØÀÀÀÀÀ¥ì(€€€½¹ÍÐµ¥¹ÕÑ•Ìõ5…Ñ ¹µ…à Ä±5…Ñ ¹•¥°¡‘•±Ñ„¼ØÀÀÀÀ¤¤ì(€€€¥˜¡µ¥¹ÕÑ•ÌðØÀ¥É•ÑÕÉ¸ÑÈ ¥¸œ¤¬œ€œ­µ¥¹ÕÑ•Ì¬œ€œ­ÑÈ¡µ¥¹ÕÑ•ÌôôôÄüµ¥¹ÕÑ”œèµ¥¹ÕÑ•Ìœ¤ì(€€€½¹ÍÐ¡½ÕÉÌõ5…Ñ ¹•¥°¡µ¥¹ÕÑ•Ì¼ØÀ¤íÉ•ÑÕÉ¸ÑÈ ¥¸œ¤¬œ€œ­¡½ÕÉÌ¬œ€œ­ÑÈ¡¡½ÕÉÌôôôÄü¡½ÕÈœè¡½ÕÉÌœ¤ì(€ô(€½¹ÍÐ‘…å¥™˜õ5…Ñ ¹É½Õ¹¡½Í±½…å9Õµ‰•È¡Ñ…É•Ð¤µ½Í±½…å9Õµ‰•È¡¹½Ü¤¤ì(€½¹ÍÐÑ¥µ”õÑ…É•Ð¹Ñ½1½…±•Q¥µ•MÑÉ¥¹œ¡±½…±”±í¡½ÕÈèœÈµ‘¥¥Ðœ±µ¥¹ÕÑ”èœÈµ‘¥¥Ðœ±Ñ¥µ•i½¹”èÕÉ½Á”½=Í±¼ô¤ì(€¥˜ …‘…Ñ•=¹±ä˜™‘…å¥™˜ôôôÀ¥É•ÑÕÉ¸ÑÈ Q½‘…äœ¤¬œƒ
Ü€œ­Ñ¥µ”ì(€¥˜ …‘…Ñ•=¹±ä˜™‘…å¥™˜ôôôÄ¥É•ÑÕÉ¸ÑÈ Q½µ½ÉÉ½Üœ¤¬œƒ
Ü€œ­Ñ¥µ”ì(€½¹ÍÐ‘…Ñ”õÑ…É•Ð¹Ñ½1½…±•…Ñ•MÑÉ¥¹œ¡±½…±”±íÝ••­‘…äèÍ¡½ÉÐœ±‘…äè¹Õµ•É¥Œœ±µ½¹Ñ èÍ¡½ÉÐœ±Ñ¥µ•i½¹”èÕÉ½Á”½=Í±¼ô¤ì(€É•ÑÕÉ¸‘…Ñ•=¹±äý‘…Ñ”è¡‘…Ñ”¬œƒ
Ü€œ­Ñ¥µ”¤ì)ô)™Õ¹Ñ¥½¸Ñ¥µ•±¥¹•I•±•…Í•‘]¡•¸¡ÑÌ¥ì(€½¹ÍÐ‘•±Ñ„õ5…Ñ ¹µ…à À±…Ñ”¹¹½Ü ¤µ9Õµ‰•È¡ÑÍñðÀ¤¤±µ¥¹ÕÑ•Ìõ5…Ñ ¹µ…à Ä±5…Ñ ¹É½Õ¹¡‘•±Ñ„¼ØÀÀÀÀ¤¤ì(€±•Ð…µ½Õ¹Ð±Õ¹¥Ðì(€¥˜¡µ¥¹ÕÑ•ÌðØÀ¥í…µ½Õ¹Ðõµ¥¹ÕÑ•ÌíÕ¹¥ÐõÑÈ¡µ¥¹ÕÑ•ÌôôôÄüµ¥¹ÕÑ”œèµ¥¹ÕÑ•Ìœ¤íô(€•±Í•í…µ½Õ¹Ðõ5…Ñ ¹µ…à Ä±5…Ñ ¹É½Õ¹¡µ¥¹ÕÑ•Ì¼ØÀ¤¤íÕ¹¥ÐõÑÈ¡…µ½Õ¹ÐôôôÄü¡½ÕÈœè¡½ÕÉÌœ¤íô(€É•ÑÕÉ¸ÑÈ I•±•…Í•œ¤¬¡}±…¹œôôô¹¼œüœ™½È€œèœ€œ¤­…µ½Õ¹Ð¬œ€œ­Õ¹¥Ð¬œ€œ­ÑÈ …¼œ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘5å1¥ÍÑM¡½ÝÌ ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑM¡½ÝÌœ¤ì(€}µå1¥ÍÑM¡½Ý5½µ•¹ÑÌõmtì(€ÑÉåì(€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½±…Ñ•ÍÑ}•Á¥Í½‘•Ìý±¥µ¥ÐôÌØœ¤±¹½Üõ…Ñ”¹¹½Ü ¤±É••¹Ñ]¥¹‘½ÜôÈÐ¨ÌØÀÀÀÀÀ±ÕÁ½µ¥¹]¥¹‘½ÜôÜ¨ÈÐ¨ÌØÀÀÀÀÀ±…¹‘¥‘…Ñ•Ìõmtì(€€€™½È¡½¹ÍÐ•À½˜€¡È¹•Á¥Í½‘•Íññmt¤¥í½¹ÍÐ…¥É•õ9Õµ‰•È¡•À¹…¥É}ÑÍñðÀ¤¨ÄÀÀÀ±…‘‘•õ9Õµ‰•È¡•À¹…‘‘•‘ñðÀ¤¨ÄÀÀÀí½¹ÍÐÑÌô¡…¥É•øÀ˜™…¥É•ðõ¹½Ü˜™¹½Üµ…¥É•ðõÉ••¹Ñ]¥¹‘½Ü¤ý…¥É•è¡…‘‘•‘ññ…¥É•¤í¥˜¡ÑÌ¥…¹‘¥‘…Ñ•Ì¹ÁÕÍ ¡í•Àé•À±ÑÌéÑÌ±ÕÁ½µ¥¹œé™…±Í•ô¤íô(€€€™½È¡½¹ÍÐ•À½˜€¡È¹ÕÁ½µ¥¹ññmt¤¥í½¹ÍÐÑÌõ9Õµ‰•È¡•À¹…¥É}ÑÍñðÀ¤¨ÄÀÀÁñð¡•À¹…¥ÉÍÑ…µÀý¹•Ü…Ñ”¡•À¹…¥ÉÍÑ…µÀ¤¹•ÑQ¥µ” ¤èÀ¤í¥˜¡ÑÌ¥…¹‘¥‘…Ñ•Ì¹ÁÕÍ ¡í•Àé•À±ÑÌéÑÌ±ÕÁ½µ¥¹œéÑÉÕ•ô¤íô(€€€½¹ÍÐ¹•…É•ÍÐõ¹•Ü5…À ¤ì(€€€™½È¡½¹ÍÐÉ½Ü½˜…¹‘¥‘…Ñ•Ì¥í½¹ÍÐ‘•±Ñ„õÉ½Ü¹ÑÌµ¹½Üí¥˜¡É½Ü¹ÕÁ½µ¥¹œ¥í¥˜¡‘•±Ñ„ðÁññ‘•±Ñ„ùÕÁ½µ¥¹]¥¹‘½Ü¥½¹Ñ¥¹Õ”íõ•±Í”¥˜¡‘•±Ñ„øÁññ5…Ñ ¹…‰Ì¡‘•±Ñ„¤ùÉ••¹Ñ]¥¹‘½Ü¥½¹Ñ¥¹Õ”í½¹ÍÐ­•äõMÑÉ¥¹œ¡É½Ü¹•À¹Í¡½Ý}¹…µ•ñðœœ¤¹Ñ½1½Ý•É…Í” ¤¬ðœ¬¡É½Ü¹ÕÁ½µ¥¹œüÕÁ½µ¥¹œœèÉ••¹Ðœ¤í½¹ÍÐ½±õ¹•…É•ÍÐ¹•Ð¡­•ä¤í¥˜ …½±‘ññ5…Ñ ¹…‰Ì¡‘•±Ñ„¤ñ5…Ñ ¹…‰Ì¡½±¹ÑÌµ¹½Ü¤¥¹•…É•ÍÐ¹Í•Ð¡­•ä±É½Ü¤íô(€€€½¹ÍÐÉ½ÝÌõÉÉ…ä¹™É½´¡¹•…É•ÍÐ¹Ù…±Õ•Ì ¤¤¹Í½ÉÐ ¡„±ˆ¤ôù5…Ñ ¹…‰Ì¡„¹ÑÌµ¹½Ü¤µ5…Ñ ¹…‰Ì¡ˆ¹ÑÌµ¹½Ü¤¤¹Í±¥” À°ÄÈ¤ì(€€€}µå1¥ÍÑM¡½Ý5½µ•¹ÑÌõÉ½ÝÌì(€€€¥˜ …É½ÝÌ¹±•¹Ñ ¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ 9½Ñ¡¥¹œ…¥É¥¹œ±½Í”Ñ¼¹½Ü™É½´å½ÕÈ™…Ù½É¥Ñ”Í¡½ÝÌ¸œ¤¬œð½ÍÁ…¸øœíÍ¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤íÉ•ÑÕÉ¸íô(€€€•°¹¥¹¹•É!Q50õÉ½ÝÌ¹µ…À¡É½Üôùí½¹ÍÐ•ÀõÉ½Ü¹•À±½Ù•Èõ•À¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡•À¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœí±•Ð…Ñ¥½¸ôœœì(€€€€€¥˜ …É½Ü¹ÕÁ½µ¥¹œ˜™•À¹…Ù…¥±…‰±”¥í½¹ÍÐÍÉŒô¡•À¹Í½ÕÉ•Ì˜™•À¹Í½ÕÉ•Ì¹±•¹Ñ ¤ý•À¹Í½ÕÉ•ÍlÁtéí¥é•À¹¥±•áÑ•¹Í¥½¸é•À¹•áÑ•¹Í¥½¹ôí¥˜¡ÍÉŒ˜™ÍÉŒ¹¥„õ¹Õ±°¥…Ñ¥½¸ôœñ‘¥Ø±…ÍÌô‰µ½Ù¥•…Ñ¥½¹Ìˆøñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Ù±Œ±…Ñ•ÍÑ•Á¥Í½‘•Ù±Œˆ‘…Ñ„µ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡ÍÉŒ¹¥¤¤¬œˆ‘…Ñ„µ•áÐôˆœ­•ÍÑÑÈ¡ÍÉŒ¹•áÑ•¹Í¥½¹ñðµÀÐœ¤¬œˆø˜ŒäØÔàìY1ð½‰ÕÑÑ½¸øð½‘¥Øøœíô(€€€€€É•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰µå‘…Í¡•Á¥Í½‘”µå±¥ÍÑÍ¡½Ý…Éˆ‘…Ñ„µÍ•É¥•Ìôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡•À¹Í•É¥•Í}¥‘ñðœœ¤¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡•À¹…Ñ…±½}¥‘ñðœœ¤¬œˆøœ­½Ù•È¬œñ‘¥Ø±…ÍÌô‰µå‘…Í¡•Á¥Í½‘•¥¹™¼ˆøñ‘¥Ø±…ÍÌô‰µå‘…Í¡•Á¥Í½‘•¹…µ”ˆøœ­•ÍŒ¡•À¹Í¡½Ý}¹…µ”¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå‘…Í¡Ý¡•¸ˆøœ­•ÍŒ¡µå1¥ÍÑÁ¥Í½‘•]¡•¸¡É½Ü¹ÑÌ±É½Ü¹ÕÁ½µ¥¹œ¤¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆùLœ­•ÍŒ¡•À¹Í•…Í½¸¤¬œ­•ÍŒ¡•À¹•Á¥Í½‘•}¹Õ´¤¬œ€´€œ­•ÍŒ¡•À¹Ñ¥Ñ±•ñðÁ¥Í½‘”œ¤¬œð½‘¥Øøœ­…Ñ¥½¸¬œð½‘¥Øøð½‘¥Øøœíô¤¹©½¥¸ œœ¤ì(€€€Í¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤ì(€õ…Ñ ¡”¥í•°¹¥¹¹•É!Q50ôœñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ ½Õ±¹½Ð±½…å½ÕÈÍ¡½ÝÌ¸œ¤¬œð½ÍÁ…¸øœíÍ¡•‘Õ±•5å1¥ÍÑQ¥µ•±¥¹•I•¹‘•È ¤íô)ô)™Õ¹Ñ¥½¸µå1¥ÍÑMÁ½ÉÑÉÑÝ½É¬¡™¥áÑÕÉ”¥ì(€½¹ÍÐ¥õMÑÉ¥¹œ ¡™¥áÑÕÉ”˜™™¥áÑÕÉ”¹±•…Õ•}¥¥ñðœœ¤ì(€¥˜ „½yqq¬¼¹Ñ•ÍÐ¡¥¤¥É•ÑÕÉ¸€œœì(€½¹ÍÐ¹…µ”õMÑÉ¥¹œ ¡™¥áÑÕÉ”˜™™¥áÑÕÉ”¹±•…Õ•}¹…µ”¥ññÑÈ MÁ½ÉÑÌœ¤¤ì(€É•ÑÕÉ¸€œñ¥µœ±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•…ÉÐˆÍÉŒôˆ½…Á¤½±•…Õ•}±½¼ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡¥¤¬œˆ…±Ðôˆœ­•ÍÑÑÈ¡¹…µ”¤¬œˆÑ¥Ñ±”ôˆœ­•ÍÑÑÈ¡¹…µ”¤¬œˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœì)ô)™Õ¹Ñ¥½¸µå1¥ÍÑI…¥¹ÉÑÝ½É¬¡•Ù•¹Ð¥ì(€½¹ÍÐÍ•É¥•ÌõMÑÉ¥¹œ ¡•Ù•¹Ð˜™•Ù•¹Ð¹Í•É¥•Ì¥ñðœœ¤¹Ñ½1½Ý•É…Í” ¤ì(€±•ÐÍÉŒôœœ±¹…µ”ôœœ±‘É¥Ù•Èõ™…±Í”±…Èõ™…±Í”ì(€¥˜¡Í•É¥•Ìôôô˜Äœ¥ì(€€€½¹ÍÐÁ…¥Èô¡}µå1¥ÍÑI…¥¹É¥Ù•ÉÍññmt¤¹™¥±Ñ•È¡É½ÜôùMÑÉ¥¹œ¡É½Ü¹Í•É¥•Íñðœœ¤¹Ñ½1½Ý•É…Í” ¤ôôô˜Äœ¤¹Í±¥” À°È¤ì(€€€¥˜¡Á…¥È¹±•¹Ñ ¥É•ÑÕÉ¸€œñÍÁ…¸±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•‘É¥Ù•ÉÌˆÑ¥Ñ±”ôˆœ­•ÍÑÑÈ¡Á…¥È¹µ…À¡É½ÜôùÉ½Ü¹¹…µ•ñðœœ¤¹™¥±Ñ•È¡	½½±•…¸¤¹©½¥¸ œ€˜€œ¤¤¬œˆøœ­Á…¥È¹µ…À¡É½Üôøœñ¥µœÍÉŒôˆ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡É½Ü¹­•åñðœœ¤¤¬œˆ…±Ðôˆœ­•ÍÑÑÈ¡É½Ü¹¹…µ•ñðœœ¤¬œˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœ¤¹©½¥¸ œœ¤¬œð½ÍÁ…¸øœì(€€€É•ÑÕÉ¸€œœì(€õ•±Í•ì(€€€½¹ÍÐ‘É¥Ù•ÉÌõíÝÉŒélÝÉŒµ½±¥Ù•ÈµÍ½±‰•Éœœ°=±¥Ù•ÈM½±‰•Éœt±¥¹‘å…Èél¥¹‘å…Èµ‘•¹¹¥Ìµ¡…Õ•Èœ°•¹¹¥Ì!…Õ•Èt±˜Èél˜Èµµ…ÉÑ¥¹¥ÕÌµÍÑ•¹Í¡½É¹”œ°5…ÉÑ¥¹¥ÕÌMÑ•¹Í¡½É¹”uôì(€€€½¹ÍÐÉ½Üõ‘É¥Ù•ÉÍmÍ•É¥•Ítì(€€€¥˜¡É½Ü¥íÍÉŒôœ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡É½ÝlÁt¤í¹…µ”õÉ½ÝlÅtí‘É¥Ù•ÈõÑÉÕ”í…ÈõÍ•É¥•Ìôôô˜Èœíô(€ô(€É•ÑÕÉ¸ÍÉŒüœñ¥µœ±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•…ÉÐœ¬¡‘É¥Ù•Èüœ‘É¥Ù•Èœèœœ¤¬¡…Èüœ…Èœèœœ¤¬œˆÍÉŒôˆœ­•ÍÑÑÈ¡ÍÉŒ¤¬œˆ…±Ðôˆœ­•ÍÑÑÈ¡¹…µ”¤¬œˆÑ¥Ñ±”ôˆœ­•ÍÑÑÈ¡¹…µ”¤¬œˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœì)ô)™Õ¹Ñ¥½¸µå1¥ÍÑI…¥¹•Ñ…¥±-•ä¡•Ù•¹Ð¥ì(€½¹ÍÐÍ•É¥•ÌõMÑÉ¥¹œ ¡•Ù•¹Ð˜™•Ù•¹Ð¹Í•É¥•Ì¥ñðœœ¤¹Ñ½1½Ý•É…Í” ¤ì(€¥˜¡Í•É¥•Ìôôô˜Äœ¥É•ÑÕÉ¸€˜ÄµÑ•…´œì(€½¹ÍÐ±½…‘•ô¡}µå1¥ÍÑI…¥¹É¥Ù•ÉÍññmt¤¹™¥¹¡É½ÜôùMÑÉ¥¹œ¡É½Ü¹Í•É¥•Íñðœœ¤¹Ñ½1½Ý•É…Í” ¤ôôõÍ•É¥•Ì¤ì(€¥˜¡±½…‘•˜™±½…‘•¹­•ä¥É•ÑÕÉ¸MÑÉ¥¹œ¡±½…‘•¹­•ä¤ì(€É•ÑÕÉ¸€¡íÝÉŒèÝÉŒµ½±¥Ù•ÈµÍ½±‰•Éœœ±¥¹‘å…Èè¥¹‘å…Èµ‘•¹¹¥Ìµ¡…Õ•Èœ±˜Èè˜Èµµ…ÉÑ¥¹¥ÕÌµÍÑ•¹Í¡½É¹”ô¥mÍ•É¥•Íuñðœœì)ô)™Õ¹Ñ¥½¸Í•ÑÕÁ•µ½½Ù•È¡±…‰•°±½±½È¥ì(€½¹ÍÐÍÙœôœñÍÙœáµ±¹Ìô‰¡ÑÑÀè¼½ÝÝÜ¹ÜÌ¹½Éœ¼ÈÀÀÀ½ÍÙœˆÝ¥‘Ñ ôˆÄàÀˆ¡•¥¡ÐôˆÈØÀˆøñÉ•ÐÝ¥‘Ñ ôˆÄàÀˆ¡•¥¡ÐôˆÈØÀˆÉàôˆÄÈˆ™¥±°ôˆœ­½±½È¬œˆ¼øñ¥É±”àôˆäÀˆäôˆäÈˆÈôˆÐÈˆ™¥±°ôˆ™™™™™˜Äàˆ¼øñÑ•áÐàôˆäÀˆäôˆÄÀÔˆÑ•áÐµ…¹¡½Èô‰µ¥‘‘±”ˆ™½¹ÐµÍ¥é”ôˆÔÀˆûÂ~Nèð½Ñ•áÐøñÑ•áÐàôˆäÀˆäôˆÄäÀˆÑ•áÐµ…¹¡½Èô‰µ¥‘‘±”ˆ™½¹Ðµ™…µ¥±äô‰Í…¹ÌµÍ•É¥˜ˆ™½¹ÐµÍ¥é”ôˆÄàˆ™½¹ÐµÝ•¥¡ÐôˆÜÀÀˆ™¥±°ôˆ™™˜ˆøœ­±…‰•°¬œð½Ñ•áÐøñÑ•áÐàôˆäÀˆäôˆÈÄØˆÑ•áÐµ…¹¡½Èô‰µ¥‘‘±”ˆ™½¹Ðµ™…µ¥±äô‰Í…¹ÌµÍ•É¥˜ˆ™½¹ÐµÍ¥é”ôˆÄÄˆ™¥±°ôˆ™™™™™™…„ˆùQY5…Ñ”‘•µ¼ð½Ñ•áÐøð½ÍÙœøœì(€É•ÑÕÉ¸€‘…Ñ„é¥µ…”½ÍÙœ­áµ°í¡…ÉÍ•ÐõUQ´à°œ­•¹½‘•UI%½µÁ½¹•¹Ð¡ÍÙœ¤ì)ô)™Õ¹Ñ¥½¸Ñ¥µ•±¥¹•1½…‘AÉ•™Ì ¥ì(€¥˜¡}µåQ¥µ•±¥¹•AÉ•™Í1½…‘•¥É•ÑÕÉ¸í}µåQ¥µ•±¥¹•AÉ•™Í1½…‘•õÑÉÕ”ì(€ÑÉåì(€€€½¹ÍÐ­¥¹õ±½…±MÑ½É…”¹•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•¥±Ñ•Èœ¤í¥˜¡l…±°œ°Í¡½Üœ°µ½Ù¥”œ°…µ”œ°ÍÁ½ÉÐœ°˜Ät¹¥¹±Õ‘•Ì¡­¥¹¤¥}µåQ¥µ•±¥¹•¥±Ñ•Èõ­¥¹ì(€€€½¹ÍÐÍ…Ù•õ)M=8¹Á…ÉÍ”¡±½…±MÑ½É…”¹•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•M•ÑÑ¥¹Ìœ¥ñðíôœ¤ì(€€€}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ìõ=‰©•Ð¹…ÍÍ¥¸¡íô±}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì±Í…Ù•‘ññíô¤ì(€õ…Ñ ¡”¥íô)ô)™Õ¹Ñ¥½¸Ñ¥µ•±¥¹•M…Ù•AÉ•™Ì ¥íÑÉåí±½…±MÑ½É…”¹Í•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•¥±Ñ•Èœ±}µåQ¥µ•±¥¹•¥±Ñ•È¤í±½…±MÑ½É…”¹Í•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•M•ÑÑ¥¹Ìœ±)M=8¹ÍÑÉ¥¹¥™ä¡}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¤¤íõ…Ñ ¡”¥íõô)™Õ¹Ñ¥½¸Ñ¥µ•±¥¹•¥±Ñ•ÉÉ½ÕÀ¡­¥¹¥íÉ•ÑÕÉ¸­¥¹ôôôÑ•…´œüÍÁ½ÉÐœè¡­¥¹ôôô˜Äœü˜Äœé­¥¹¤íô)™Õ¹Ñ¥½¸Ñ¥µ•±¥¹•M•ÑÑ¥¹Í¡…¹• ¥í½¹ÍÐàõ}µåQ¥µ•±¥¹•M•ÑÑ¥¹ÌíÉ•ÑÕÉ¸à¹É••¹Ð„ôõÑÉÕ•ññà¹±¥Ù”„ôõÑÉÕ•ññà¹ÕÁ½µ¥¹œ„ôõÑÉÕ•ññ9Õµ‰•È¡à¹µ…áA•É…Ñ•½ÉåñðÀ¤„ôôÀíô)™Õ¹Ñ¥½¸Ñ¥µ•±¥¹•½¹ÑÉ½±Í!Ñµ° ¥ì(€½¹ÍÐ­¥¹‘Ìõml…±°œ°±°t±lÍ¡½Üœ°M¡½ÝÌt±lµ½Ù¥”œ°5½Ù¥•Ìt±l…µ”œ°…µ•Ìt±lÍÁ½ÉÐœ°MÁ½ÉÑÌt±l˜Äœ°I…¥¹œutí±•Ð ôœñ‘¥Ø±…ÍÌô‰µåÑ¥µ•±¥¹•½¹ÑÉ½±Ìˆøœì(€™½È¡½¹ÍÐ¬½˜­¥¹‘Ì¥ ¬ôœñ‰ÕÑÑ½¸±…ÍÌô‰µåÑ¥µ•±¥¹•™¥±Ñ•È€œ­­lÁt¬¡}µåQ¥µ•±¥¹•¥±Ñ•Èôôõ­lÁtüœ½¸œèœœ¤¬œˆ‘…Ñ„µ­¥¹ôˆœ­­lÁt¬œˆ½¹±¥¬ô‰Í•ÑQ¥µ•±¥¹•¥±Ñ•È¡Ñ¡¥Ì¹‘…Ñ…Í•Ð¹­¥¹¤ˆøœ­•ÍŒ¡ÑÈ¡­lÅt¤¤¬œð½‰ÕÑÑ½¸øœì(€ ¬ôœñ‰ÕÑÑ½¸±…ÍÌô‰µåÑ¥µ•±¥¹•™¥±Ñ•ÈÍ•ÑÑ¥¹Ìœ¬¡Ñ¥µ•±¥¹•M•ÑÑ¥¹Í¡…¹• ¤üœ¡…¹•œèœœ¤¬œˆ½¹±¥¬ô‰Ñ½±•Q¥µ•±¥¹•M•ÑÑ¥¹Ì¡Ñ¡¥Ì¤ˆø˜ŒäààÄì€œ­•ÍŒ¡ÑÈ ¥±Ñ•Èœ¤¤¬œð½‰ÕÑÑ½¸øœì(€ ¬ôœñ‘¥Ø±…ÍÌô‰µåÑ¥µ•±¥¹•™¥±Ñ•ÉÁ…¹•°¡¥‘”ˆøñ Ðøœ­•ÍŒ¡ÑÈ Q¥µ•±¥¹”Í•ÑÑ¥¹Ìœ¤¤¬œð½ Ðøñ‘¥Ø±…ÍÌô‰Ñ¥µ•±¥¹•¡•­Ìˆøœ(€€€€¬œñ±…‰•°øñ¥¹ÁÕÐÑåÁ”ô‰¡•­‰½àˆ‘…Ñ„µÍ•ÑÑ¥¹œô‰É••¹Ðˆ€œ¬¡}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¹É••¹Ðü¡•­•œèœœ¤¬œ½¹¡…¹”ô‰Í•ÑQ¥µ•±¥¹•M•ÑÑ¥¹œ¡Ñ¡¥Ì¤ˆø€œ­•ÍŒ¡ÑÈ I••¹Ñ±äœ¤¤¬œð½±…‰•°øœ(€€€€¬œñ±…‰•°øñ¥¹ÁÕÐÑåÁ”ô‰¡•­‰½àˆ‘…Ñ„µÍ•ÑÑ¥¹œô‰±¥Ù”ˆ€œ¬¡}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¹±¥Ù”ü¡•­•œèœœ¤¬œ½¹¡…¹”ô‰Í•ÑQ¥µ•±¥¹•M•ÑÑ¥¹œ¡Ñ¡¥Ì¤ˆø€œ­•ÍŒ¡ÑÈ 1¥Ù”¹½Üœ¤¤¬œð½±…‰•°øœ(€€€€¬œñ±…‰•°øñ¥¹ÁÕÐÑåÁ”ô‰¡•­‰½àˆ‘…Ñ„µÍ•ÑÑ¥¹œô‰ÕÁ½µ¥¹œˆ€œ¬¡}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¹ÕÁ½µ¥¹œü¡•­•œèœœ¤¬œ½¹¡…¹”ô‰Í•ÑQ¥µ•±¥¹•M•ÑÑ¥¹œ¡Ñ¡¥Ì¤ˆø€œ­•ÍŒ¡ÑÈ UÁ½µ¥¹œœ¤¤¬œð½±…‰•°øð½‘¥Øøœ(€€€€¬œñ±…‰•°øñÍÁ…¸øœ­•ÍŒ¡ÑÈ 5…á¥µÕ´Á•È…Ñ•½Éäœ¤¤¬œð½ÍÁ…¸øñÍ•±•Ð‘…Ñ„µÍ•ÑÑ¥¹œô‰µ…áA•É…Ñ•½Éäˆ½¹¡…¹”ô‰Í•ÑQ¥µ•±¥¹•M•ÑÑ¥¹œ¡Ñ¡¥Ì¤ˆøœ(€€€€­mlÀ°ÁÀ‘•™…Õ±Ðt±lÈ°œÈt±lÐ°œÐt±lØ°œØt±là°œàt±lÄÈ°œÄÈut¹µ…À¡àôøœñ½ÁÑ¥½¸Ù…±Õ”ôˆœ­álÁt¬œˆœ¬¡9Õµ‰•È¡}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¹µ…áA•É…Ñ•½ÉåñðÀ¤ôôõálÁtüœÍ•±•Ñ•œèœœ¤¬œøœ­•ÍŒ¡ÑÈ¡álÅt¤¤¬œð½½ÁÑ¥½¸øœ¤¹©½¥¸ œœ¤¬œð½Í•±•Ðøð½±…‰•°øœ(€€€€¬œñ‰ÕÑÑ½¸±…ÍÌô‰Ñ¥µ•±¥¹•™¥±Ñ•ÉÉ•Í•Ðˆ½¹±¥¬ô‰É•Í•ÑQ¥µ•±¥¹•M•ÑÑ¥¹Ì ¤ˆøœ­•ÍŒ¡ÑÈ I•Í•ÐÑ¼‘•™…Õ±Ðœ¤¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøð½‘¥ØøœíÉ•ÑÕÉ¸ ì)ô)™Õ¹Ñ¥½¸Í•ÑQ¥µ•±¥¹•¥±Ñ•È¡­¥¹¥í}µåQ¥µ•±¥¹•¥±Ñ•Èõl…±°œ°Í¡½Üœ°µ½Ù¥”œ°…µ”œ°ÍÁ½ÉÐœ°˜Ät¹¥¹±Õ‘•Ì¡­¥¹¤ý­¥¹è…±°œíÑ¥µ•±¥¹•M…Ù•AÉ•™Ì ¤íÉ•¹‘•É5å1¥ÍÑQ¥µ•±¥¹” ¤íô)™Õ¹Ñ¥½¸Ñ½±•Q¥µ•±¥¹•M•ÑÑ¥¹Ì¡‰Ñ¸¥í½¹ÍÐÝÉ…Àõ‰Ñ¸¹±½Í•ÍÐ œ¹µåÑ¥µ•±¥¹•½¹ÑÉ½±Ìœ¤±Á…¹•°õÝÉ…ÀýÝÉ…À¹ÅÕ•ÉåM•±•Ñ½È œ¹µåÑ¥µ•±¥¹•™¥±Ñ•ÉÁ…¹•°œ¤é¹Õ±°í¥˜¡Á…¹•°¥Á…¹•°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ¤íô)™Õ¹Ñ¥½¸Í•ÑQ¥µ•±¥¹•M•ÑÑ¥¹œ¡¥¹ÁÕÐ¥í½¹ÍÐ­•äõ¥¹ÁÕÐ¹‘…Ñ…Í•Ð¹Í•ÑÑ¥¹œí¥˜¡­•äôôôµ…áA•É…Ñ•½Éäœ¥}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ím­•åtõ5…Ñ ¹µ…à À±9Õµ‰•È¡¥¹ÁÕÐ¹Ù…±Õ•ñðÀ¤¤í•±Í”}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ím­•åtô„…¥¹ÁÕÐ¹¡•­•íÑ¥µ•±¥¹•M…Ù•AÉ•™Ì ¤íÉ•¹‘•É5å1¥ÍÑQ¥µ•±¥¹” ¤íô)™Õ¹Ñ¥½¸É•Í•ÑQ¥µ•±¥¹•M•ÑÑ¥¹Ì ¥í}µåQ¥µ•±¥¹•M•ÑÑ¥¹ÌõíÉ••¹ÐéÑÉÕ”±±¥Ù”éÑÉÕ”±ÕÁ½µ¥¹œéÑÉÕ”±µ…áA•É…Ñ•½ÉäèÁôíÑ¥µ•±¥¹•M…Ù•AÉ•™Ì ¤íÉ•¹‘•É5å1¥ÍÑQ¥µ•±¥¹” ¤íô)™Õ¹Ñ¥½¸É•¹‘•É5å1¥ÍÑQ¥µ•±¥¹” ¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µå1¥ÍÑQ¥µ•±¥¹”œ¤±ÍÑ…¹‘…±½¹”õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µåQ¥µ•±¥¹•MÑ…¹‘…±½¹”œ¤í¥˜ …•°˜˜…ÍÑ…¹‘…±½¹”¥É•ÑÕÉ¸ì(€½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤±µ½µ•¹ÑÌõmtì(€¥˜¡}™½½Ñ‰…±±¹…‰±•¥™½È¡½¹ÍÐÉ½Ü½˜}µå1¥ÍÑQ•…µ5½µ•¹ÑÌ¥µ½µ•¹ÑÌ¹ÁÕÍ ¡í­¥¹èÑ•…´œ±ÑÌéÉ½Ü¹ÑÌ±±¥Ù”éÉ½Ü¹±¥Ù”±‘…Ñ„éÉ½Ýô¤ì(€¥˜¡}˜Å¹…‰±•¥™½È¡½¹ÍÐÉ½Ü½˜}µå1¥ÍÑÅ5½µ•¹ÑÌ¥µ½µ•¹ÑÌ¹ÁÕÍ ¡í­¥¹è˜Äœ±ÑÌéÉ½Ü¹ÑÌ±±¥Ù”éÉ…¥¹Ù•¹Ñ%Í1¥Ù”¡É½Ü¹•Ù•¹Ð±¹½Ü¤±‘…Ñ„éÉ½Ýô¤ì(€™½È¡½¹ÍÐÉ½Ü½˜}µå1¥ÍÑ5½Ù¥•5½µ•¹ÑÌ¥µ½µ•¹ÑÌ¹ÁÕÍ ¡í­¥¹èµ½Ù¥”œ±ÑÌéÉ½Ü¹ÑÌ±±¥Ù”é™…±Í”±‘…Ñ„éÉ½Ýô¤ì(€™½È¡½¹ÍÐÉ½Ü½˜}µå1¥ÍÑ…µ•5½µ•¹ÑÌ¥µ½µ•¹ÑÌ¹ÁÕÍ ¡í­¥¹è…µ”œ±ÑÌéÉ½Ü¹ÑÌ±±¥Ù”é™…±Í”±‘…Ñ„éÉ½Ýô¤ì(€™½È¡½¹ÍÐÉ½Ü½˜}µå1¥ÍÑM¡½Ý5½µ•¹ÑÌ¥µ½µ•¹ÑÌ¹ÁÕÍ ¡í­¥¹èÍ¡½Üœ±ÑÌéÉ½Ü¹ÑÌ±±¥Ù”é™…±Í”±‘…Ñ„éÉ½Ýô¤ì(€½¹ÍÐÉ•…±]…Ñ ô ¡}µå1¥ÍÑ…Ù…Ñ„¹Í¡½ÝÍññmt¤¹±•¹Ñ ¬¡}µå1¥ÍÑ…Ù…Ñ„¹µ½Ù¥•Íññmt¤¹±•¹Ñ ¤øÀì(€¥˜¡}ÁÉ½™¥±•½¹™¥œ¹Í•ÑÕÁ}‘•µ½}½¹Ñ•¹Ð˜˜…É•…±]…Ñ ¥ì(€€€µ½µ•¹ÑÌ¹ÁÕÍ ¡í­¥¹èÍ¡½Üœ±ÑÌé¹½Ü´Ø¨ÌØÀÀÀÀÀ±±¥Ù”é™…±Í”±‘…Ñ„éíÕÁ½µ¥¹œé™…±Í”±•ÀéíÍ¡½Ý}¹…µ”èá…µÁ±”M¡½Üœ±Í•…Í½¸èÄ±•Á¥Í½‘•}¹Õ´èÄ±Ñ¥Ñ±”è]•±½µ”Ñ¼QY5…Ñ”œ±½Ù•ÈéÍ•ÑÕÁ•µ½½Ù•È a5A1M!=\œ°œŒÝ„ÍÄÈœ¤±…Ù…¥±…‰±”é™…±Í”±Í•É¥•Í}¥èœœ±…Ñ…±½}¥èœõõô¤ì(€€€µ½µ•¹ÑÌ¹ÁÕÍ ¡í­¥¹èµ½Ù¥”œ±ÑÌé¹½Ü¬ÌØ¨ÌØÀÀÀÀÀ±±¥Ù”é™…±Í”±‘…Ñ„éíµ½Ù¥”éí¹…µ”èá…µÁ±”5½Ù¥”œ±å•…Èé¹•Ü…Ñ” ¤¹•ÑÕ±±e•…È ¤±½Ù•ÈéÍ•ÑÕÁ•µ½½Ù•È a5A15=Y%œ°œŒÄØÑ„ÜÈœ¤±ÍÑÉ•…µ}™½Õ¹é™…±Í•õõô¤ì(€ô(€Ñ¥µ•±¥¹•1½…‘AÉ•™Ì ¤ì(€½¹ÍÐ½¹ÑÉ½±ÌõÑ¥µ•±¥¹•½¹ÑÉ½±Í!Ñµ° ¤ì(€±•Ð™¥±Ñ•É•õµ½µ•¹ÑÌ¹™¥±Ñ•È¡´ôù}µåQ¥µ•±¥¹•¥±Ñ•Èôôô…±°ññÑ¥µ•±¥¹•¥±Ñ•ÉÉ½ÕÀ¡´¹­¥¹¤ôôõ}µåQ¥µ•±¥¹•¥±Ñ•È¤ì(€¥˜ …}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¹É••¹Ð¥™¥±Ñ•É•õ™¥±Ñ•É•¹™¥±Ñ•È¡´ôù´¹±¥Ù•ññ´¹ÑÌøõ¹½Ü¤ì(€¥˜ …}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¹±¥Ù”¥™¥±Ñ•É•õ™¥±Ñ•É•¹™¥±Ñ•È¡´ôø…´¹±¥Ù”¤ì(€¥˜ …}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¹ÕÁ½µ¥¹œ¥™¥±Ñ•É•õ™¥±Ñ•É•¹™¥±Ñ•È¡´ôù´¹±¥Ù•ññ´¹ÑÌñ¹½Ü¤ì(€½¹ÍÐµ…áA•É…Ñ•½Éäõ5…Ñ ¹µ…à À±9Õµ‰•È¡}µåQ¥µ•±¥¹•M•ÑÑ¥¹Ì¹µ…áA•É…Ñ•½ÉåñðÀ¤¤ì(€¥˜¡µ…áA•É…Ñ•½Éä¥ì(€€€½¹ÍÐ­••Àõ¹•ÜM•Ð ¤±É½ÕÁÌõ¹•Ü5…À ¤ì(€€€™½È¡½¹ÍÐ´½˜™¥±Ñ•É•¥í½¹ÍÐ­•äõÑ¥µ•±¥¹•¥±Ñ•ÉÉ½ÕÀ¡´¹­¥¹¤í¥˜ …É½ÕÁÌ¹¡…Ì¡­•ä¤¥É½ÕÁÌ¹Í•Ð¡­•ä±mt¤íÉ½ÕÁÌ¹•Ð¡­•ä¤¹ÁÕÍ ¡´¤íô(€€€™½È¡½¹ÍÐÉ½ÝÌ½˜É½ÕÁÌ¹Ù…±Õ•Ì ¤¥™½È¡½¹ÍÐ´½˜É½ÝÌ¹Í½ÉÐ ¡„±ˆ¤ôù5…Ñ ¹…‰Ì¡„¹ÑÌµ¹½Ü¤µ5…Ñ ¹…‰Ì¡ˆ¹ÑÌµ¹½Ü¤¤¹Í±¥” À±µ…áA•É…Ñ•½Éä¤¥­••À¹…‘¡´¤ì(€€€™¥±Ñ•É•õ™¥±Ñ•É•¹™¥±Ñ•È¡´ôù­••À¹¡…Ì¡´¤¤ì(€ô(€¥˜ …™¥±Ñ•É•¹±•¹Ñ ¥í½¹ÍÐ•µÁÑäõ½¹ÑÉ½±Ì¬œñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰Á…‘‘¥¹œèÄÉÁà€Àˆøœ­ÑÈ 9½Ñ¡¥¹œ¡…ÁÁ•¹¥¹œ…É½Õ¹¹½Ü¸œ¤¬œð½‘¥Øøœí¥˜¡•°¥•°¹¥¹¹•É!Q50õ•µÁÑäí¥˜¡ÍÑ…¹‘…±½¹”¥ÍÑ…¹‘…±½¹”¹¥¹¹•É!Q50õ•µÁÑäíÉ•ÑÕÉ¸íô(€½¹ÍÐÉ••¹Ðõ™¥±Ñ•É•¹™¥±Ñ•È¡´ôø…´¹±¥Ù”˜™´¹ÑÌñ¹½Ü¤¹Í½ÉÐ ¡„±ˆ¤ôù„¹ÑÌµˆ¹ÑÌ¤¹µ…À¡´ôù=‰©•Ð¹…ÍÍ¥¸¡íÍ•Ñ¥½¸èÉ••¹Ðô±´¤¤ì(€½¹ÍÐ±¥Ù”õ™¥±Ñ•É•¹™¥±Ñ•È¡´ôù´¹±¥Ù”¤¹Í½ÉÐ ¡„±ˆ¤ôù„¹ÑÌµˆ¹ÑÌ¤¹µ…À¡´ôù=‰©•Ð¹…ÍÍ¥¸¡íÍ•Ñ¥½¸è±¥Ù”ô±´¤¤ì(€½¹ÍÐÕÁ½µ¥¹œõ™¥±Ñ•É•¹™¥±Ñ•È¡´ôø…´¹±¥Ù”˜™´¹ÑÌøõ¹½Ü¤¹Í½ÉÐ ¡„±ˆ¤ôù„¹ÑÌµˆ¹ÑÌ¤¹µ…À¡´ôù=‰©•Ð¹…ÍÍ¥¸¡íÍ•Ñ¥½¸èÕÁ½µ¥¹œô±´¤¤ì(€½¹ÍÐ½É‘•É•õÉ••¹Ð¹½¹…Ð¡±¥Ù”±ÕÁ½µ¥¹œ¤ì(€±•Ð õ½¹ÑÉ½±Ì¬œñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹”ˆøœì(€±•ÐÍ•Ñ¥½¸ôœœì(€™½È¡½¹ÍÐµ½µ•¹Ð½˜½É‘•É•¥ì(€€€¥˜¡µ½µ•¹Ð¹Í•Ñ¥½¸„ôõÍ•Ñ¥½¸¥íÍ•Ñ¥½¸õµ½µ•¹Ð¹Í•Ñ¥½¸í½¹ÍÐ±…‰•°õÍ•Ñ¥½¸ôôôÉ••¹ÐœýÑÈ I••¹Ñ±äœ¤è¡Í•Ñ¥½¸ôôô±¥Ù”œýÑÈ 1¥Ù”¹½Üœ¤éÑÈ UÁ½µ¥¹œœ¤¤í ¬ôœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•Í•Ñ¥½¸€œ­Í•Ñ¥½¸¬œˆøœ­•ÍŒ¡±…‰•°¤¬œð½‘¥Øøœíô(€€€¥˜¡µ½µ•¹Ð¹­¥¹ôôôÑ•…´œ¥ì(€€€€€½¹ÍÐÉ½Üõµ½µ•¹Ð¹‘…Ñ„±˜õÉ½Ü¹™¥áÑÕÉ”ì(€€€€€½¹ÍÐÝ¡•¸õÉ½Ü¹±¥Ù”ýÑÈ 1¥Ù”¹½Üœ¤è¡˜¹ÍÑ…ÉÐýÑ¥µ•±¥¹•UÁ½µ¥¹]¡•¸¡É½Ü¹ÑÌ±™…±Í”¤éÑÈ 9•áÐµ…Ñ œ¤¤ì(€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹••¹ÑÉäœ¬¡É½Ü¹±¥Ù”üœ¥Ìµ±¥Ù”œèœœ¤¬œˆøœ¬¡É½Ü¹±¥Ù”üœœèœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•Ý¡•¸ˆøœ­•ÍŒ¡Ý¡•¸¤¬œð½‘¥Øøœ¤¬œñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•‰½‘äµå±¥ÍÑÑ¥µ•±¥¹•½¹Ñ•¹ÐˆøñÍÁ…¸±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•­¥¹ÍÁ½ÉÐˆøœ­•ÍŒ¡ÑÈ MÁ½ÉÑÌœ¤¤¬œð½ÍÁ…¸øœ­µå1¥ÍÑMÁ½ÉÑÉÑÝ½É¬¡˜¤­Ñ•…µ¥áÑÕÉ•…É¡˜±É½Ü¹±¥Ù”±ÑÉÕ”¤¬œð½‘¥Øøð½‘¥Øøœì(€€€õ•±Í”¥˜¡µ½µ•¹Ð¹­¥¹ôôô˜Äœ¥ì(€€€€€½¹ÍÐÉ½Üõµ½µ•¹Ð¹‘…Ñ„±•Ù•¹ÐõÉ½Ü¹•Ù•¹Ð±‘…Ñ”õ¹•Ü…Ñ”¡É½Ü¹ÑÌ¤±Ý¡•¸õµ½µ•¹Ð¹±¥Ù”ýÑÈ 1¥Ù”¹½Üœ¤éÑ¥µ•±¥¹•UÁ½µ¥¹]¡•¸¡É½Ü¹ÑÌ°„…•Ù•¹Ð¹…±±}‘…ä¤ì(€€€€€½¹ÍÐÉ…¥¹UÉ°õ•Ù•¹Ð¹ÕÉ±ñð ¡ÑÑÁÌè¼½ÝÝÜ¹™½ÉµÕ±„Ä¹½´½•¸½É…¥¹œ¼œ­‘…Ñ”¹•ÑÕ±±e•…È ¤¤±Í•É¥•Ìõ•Ù•¹Ð¹Í•É¥•Í}¹…µ•ñð½ÉµÕ±„€Äœì(€€€€€½¹ÍÐ…Ù…¥±…‰±”ô¡•Ù•¹Ð¹¡…¹¹•±Íññmt¤¹±•¹Ñ üœñÍÁ…¸±…ÍÌô‰Œµå±¥ÍÑÑ¥µ•±¥¹•…Ù…¥°ˆÑ¥Ñ±”ôˆœ­•ÍÑÑÈ¡ÑÈ ¡…¹¹•±Ì…Ù…¥±…‰±”œ¤¤¬œˆùQXð½ÍÁ…¸øœèœœì(€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹••¹ÑÉäœ¬¡µ½µ•¹Ð¹±¥Ù”üœ¥Ìµ±¥Ù”œèœœ¤¬œˆøœ¬¡µ½µ•¹Ð¹±¥Ù”üœœèœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•Ý¡•¸ˆøœ­•ÍŒ¡Ý¡•¸¤¬œð½‘¥Øøœ¤¬œñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•‰½‘äµå±¥ÍÑÑ¥µ•±¥¹•½¹Ñ•¹Ðµå±¥ÍÑÑ¥µ•±¥¹•˜Äœ¬ ¡•Ù•¹Ð¹¡…¹¹•±Íññmt¤¹±•¹Ñ üœ¡…Í¡…¹¹•±Ìœèœœ¤¬œˆ‘…Ñ„µ‘É¥Ù•Èµ­•äôˆœ­•ÍÑÑÈ¡µå1¥ÍÑI…¥¹•Ñ…¥±-•ä¡•Ù•¹Ð¤¤¬œˆ‘…Ñ„µÕÉ°ôˆœ­•ÍÑÑÈ¡É…¥¹UÉ°¤¬œˆøñÍÁ…¸±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•­¥¹˜Äˆøœ­•ÍŒ¡ÑÈ I…¥¹œœ¤¤¬œð½ÍÁ…¸øœ­µå1¥ÍÑI…¥¹ÉÑÝ½É¬¡•Ù•¹Ð¤¬œñ‘¥Øøñˆøœ­•ÍŒ¡•Ù•¹Ð¹É…”¤¬œð½ˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆøœ­•ÍŒ¡Í•É¥•Ì¤¬œƒ
Ü€œ­•ÍŒ¡•Ù•¹Ð¹Í•ÍÍ¥½¸¤¬¡•Ù•¹Ð¹¥ÉÕ¥Ð˜™•Ù•¹Ð¹¥ÉÕ¥Ð„ôõ•Ù•¹Ð¹É…”üœƒ
Ü€œ­•ÍŒ¡•Ù•¹Ð¹¥ÉÕ¥Ð¤èœœ¤¬œð½‘¥Øøð½‘¥Øøœ­…Ù…¥±…‰±”¬œð½‘¥Øøð½‘¥Øøœì(€€€õ•±Í”¥˜¡µ½µ•¹Ð¹­¥¹ôôôµ½Ù¥”œ¥ì(€€€€€½¹ÍÐÉ½Üõµ½µ•¹Ð¹‘…Ñ„±´õÉ½Ü¹µ½Ù¥”±½Ù•Èõ´¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡´¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ±Ý¡•¸õÉ½Ü¹ÑÌñ…Ñ”¹¹½Ü ¤ýÑ¥µ•±¥¹•I•±•…Í•‘]¡•¸¡É½Ü¹ÑÌ¤éÑ¥µ•±¥¹•UÁ½µ¥¹]¡•¸¡É½Ü¹ÑÌ±ÑÉÕ”¤ì(€€€€€½¹ÍÐ…Ñ¥½¸õ´¹ÍÑÉ•…µ}™½Õ¹üœñ‘¥Ø±…ÍÌô‰µ½Ù¥•…Ñ¥½¹ÌˆøñÍÁ…¸±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆøœ­ÑÈ MÑÉ•…´™½Õ¹¥¸Á±…å±¥ÍÐœ¤¬œð½ÍÁ…¸øñ‰ÕÑÑ½¸±…ÍÌô‰‰Ñ¹Ù±Œµ½Ù¥•Ù±Œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡´¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ•áÐôˆœ­•ÍÑÑÈ¡´¹•áÑ•¹Í¥½¹ñðµÀÐœ¤¬œˆø˜ŒäØÔàìY1ð½‰ÕÑÑ½¸øð½‘¥Øøœèœñ‘¥Ø±…ÍÌô‰µ½Ù¥•…Ñ¥½¹Ìˆøñ‰ÕÑÑ½¸±…ÍÌô‰¡½ÍÐˆ‘¥Í…‰±•øœ­ÑÈ 9½Ð…Ù…¥±…‰±”œ¤¬œð½‰ÕÑÑ½¸øð½‘¥Øøœì(€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹••¹ÑÉäˆøñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•Ý¡•¸ˆøœ­•ÍŒ¡Ý¡•¸¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•‰½‘äµå±¥ÍÑÑ¥µ•±¥¹••Á¥Í½‘”ˆøñÍÁ…¸±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•­¥¹µ½Ù¥”ˆøœ­•ÍŒ¡ÑÈ 5½Ù¥”œ¤¤¬œð½ÍÁ…¸øœ­½Ù•È¬œñ‘¥Øøñˆøœ­•ÍŒ¡´¹¹…µ”¤¬œð½ˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆøœ­•ÍŒ¡´¹å•…Éñðœœ¤¬œð½‘¥Øøœ­…Ñ¥½¸¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€€€õ•±Í”¥˜¡µ½µ•¹Ð¹­¥¹ôôô…µ”œ¥ì(€€€€€½¹ÍÐÉ½Üõµ½µ•¹Ð¹‘…Ñ„±œõÉ½Ü¹…µ”±½Ù•Èõœ¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡œ¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ±Ý¡•¸õÉ½Ü¹ÑÌñ…Ñ”¹¹½Ü ¤ýÑ¥µ•±¥¹•I•±•…Í•‘]¡•¸¡É½Ü¹ÑÌ¤éÑ¥µ•±¥¹•UÁ½µ¥¹]¡•¸¡É½Ü¹ÑÌ±ÑÉÕ”¤ì(€€€€€½¹ÍÐ…µ•UÉ°õœ¹ÕÉ±ñð ¡ÑÑÁÌè¼½ÍÑ½É”¹ÍÑ•…µÁ½Ý•É•¹½´½…ÁÀ¼œ­•¹½‘•UI%½µÁ½¹•¹Ð¡MÑÉ¥¹œ¡œ¹…ÁÁ}¥‘ñðœœ¤¤¬œ¼œ¤ì(€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹••¹ÑÉäˆøñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•Ý¡•¸ˆøœ­•ÍŒ¡Ý¡•¸¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•‰½‘äµå±¥ÍÑÑ¥µ•±¥¹•…µ”ˆ‘…Ñ„µÕÉ°ôˆœ­•ÍÑÑÈ¡…µ•UÉ°¤¬œˆøñÍÁ…¸±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•­¥¹…µ”ˆøœ­•ÍŒ¡ÑÈ …µ”œ¤¤¬œð½ÍÁ…¸øœ­½Ù•È¬œñ‘¥Øøñˆøœ­•ÍŒ¡œ¹¹…µ•ñð…µ”œ¤¬œð½ˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆøœ­•ÍŒ¡œ¹É•±•…Í•}Ñ•áÑñðœœ¤¬œð½‘¥Øøð½‘¥Øøð½‘¥Øøð½‘¥Øøœì(€€€õ•±Í•ì(€€€€€½¹ÍÐÉ½Üõµ½µ•¹Ð¹‘…Ñ„±•ÀõÉ½Ü¹•À±½Ù•Èõ•À¹½Ù•Èüœñ¥µœÍÉŒôˆœ­•ÍÑÑÈ¡•À¹½Ù•È¤¬œˆ…±Ðôˆˆ±½…‘¥¹œô‰±…éäˆ½¹•ÉÉ½Èô‰Ñ¡¥Ì¹É•µ½Ù” ¤ˆøœèœœ±…Ù…¥±…‰±”ô …É½Ü¹ÕÁ½µ¥¹œ˜™•À¹…Ù…¥±…‰±”¤üœñÍÁ…¸±…ÍÌô‰Œµå±¥ÍÑÑ¥µ•±¥¹•…Ù…¥°ˆÑ¥Ñ±”ô‰Ù…¥±…‰±”Ñ¼Á±…äˆø˜ŒäØÔÐìð½ÍÁ…¸øœèœœì(€€€€€½¹ÍÐÝ¡•¸õÉ½Ü¹ÑÌñ…Ñ”¹¹½Ü ¤ýÑ¥µ•±¥¹•I•±•…Í•‘]¡•¸¡É½Ü¹ÑÌ¤éÑ¥µ•±¥¹•UÁ½µ¥¹]¡•¸¡É½Ü¹ÑÌ±™…±Í”¤ì(€€€€€ ¬ôœñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹••¹ÑÉäˆøñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•Ý¡•¸ˆøœ­•ÍŒ¡Ý¡•¸¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•‰½‘äµå±¥ÍÑÑ¥µ•±¥¹••Á¥Í½‘”µå±¥ÍÑÍ¡½Ý…Éˆ‘…Ñ„µÍ•É¥•Ìôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡•À¹Í•É¥•Í}¥‘ñðœœ¤¤¬œˆ‘…Ñ„µ…Ñ…±½œôˆœ­•ÍÑÑÈ¡•À¹…Ñ…±½}¥‘ñðœœ¤¬œˆøñÍÁ…¸±…ÍÌô‰µå±¥ÍÑÑ¥µ•±¥¹•­¥¹Í¡½Üˆøœ­•ÍŒ¡ÑÈ M¡½Üœ¤¤¬œð½ÍÁ…¸øœ­½Ù•È¬œñ‘¥Øøñˆøœ­•ÍŒ¡•À¹Í¡½Ý}¹…µ”¤¬œð½ˆøñ‘¥Ø±…ÍÌô‰µ½Ù¥•µ•Ñ„ˆùLœ­•ÍŒ¡•À¹Í•…Í½¸¤¬œ­•ÍŒ¡•À¹•Á¥Í½‘•}¹Õ´¤¬œ€´€œ­•ÍŒ¡•À¹Ñ¥Ñ±•ñðÁ¥Í½‘”œ¤¬œð½‘¥Øøð½‘¥Øøœ­…Ù…¥±…‰±”¬œð½‘¥Øøð½‘¥Øøœì(€€€ô(€ô(€½¹ÍÐ¡Ñµ°õ ¬œð½‘¥Øøœí¥˜¡•°¥•°¹¥¹¹•É!Q50õ¡Ñµ°í¥˜¡ÍÑ…¹‘…±½¹”¥ÍÑ…¹‘…±½¹”¹¥¹¹•É!Q50õ¡Ñµ°ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•µ½Ù•…Ù…Ð¡…Ð¥ì(€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÉ•µ½Ù•}…Ðœ±…Ñ•½Éäé…Ñô¤ì(€±½…‘…Ù½É¥Ñ•Ì ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸É•µ½Ù•…Ù¡…¸¡Í¥¥ì(€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÉ•µ½Ù•}¡…¹¹•°œ±ÍÑÉ•…µ}¥éÍ¥‘ô¤ì(€±½…‘…Ù½É¥Ñ•Ì ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸Ñ½±•…Ù¡…¹¹•°¡Í¥±¹…µ”±…Ð±ÍÑ…É°¥ì(€½¹ÍÐÈõ…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÑ½±•}¡…¹¹•°œ±ÍÑÉ•…µ}¥éÍ¥±¹…µ”é¹…µ”±…Ñ•½Éäé…Ñô¤ì(€½¹ÍÐ¥‘Ìõ¹•ÜM•Ð ¡È¹¡…¹¹•±}¥‘Íññmt¤¹µ…À¡MÑÉ¥¹œ¤¤ì(€}™…Ù¡…¹M•Ðõ¥‘Ìì(€¥˜¡ÍÑ…É°¥ÍÑ…É°¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½¸œ±¥‘Ì¹¡…Ì¡MÑÉ¥¹œ¡Í¥¤¤¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸™…ÙM•±•Ñ•‘…ÑÌ ¥ì(€½¹ÍÐ…ÑÌõÉÉ…ä¹™É½´¡}Í•±…ÑÌ¤ì(€¥˜ ……ÑÌ¹±•¹Ñ ¥í…±•ÉÐ Q¥¬Í½µ”…Ñ•½É¥•Ì™¥ÉÍÐ¸œ¤íÉ•ÑÕÉ¸íô(€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸è…‘‘}…ÑÌœ±…Ñ•½É¥•Ìé…ÑÍô¤ì(€Ñ½…ÍÐ ‘‘•€œ­…ÑÌ¹±•¹Ñ ¬œ…Ñ•½Èœ¬¡…ÑÌ¹±•¹Ñ ôôôÄüäœè¥•Ìœ¤¬œÑ¼AÉ½™¥±”œ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸™…ÙA±…å±¥ÍÐ ¥ì(€½¹ÍÐ¥Ñ•µÌõÉÉ…ä¹™É½´¡}Á±…å±¥ÍÐ¹•¹ÑÉ¥•Ì ¤¤¹µ…À¡™Õ¹Ñ¥½¸¡­Ø¥íÉ•ÑÕÉ¸íÍÑÉ•…µ}¥é­ÙlÁt±¹…µ”é­ÙlÅt¹¹…µ”±…Ñ•½Éäé­ÙlÅt¹…Ñ•½Éåñðœôíô¤ì(€¥˜ …¥Ñ•µÌ¹±•¹Ñ ¥í…±•ÉÐ A±…å±¥ÍÐ¥Ì•µÁÑä¸œ¤íÉ•ÑÕÉ¸íô(€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸è…‘‘}¡…¹¹•±Ìœ±¡…¹¹•±Ìé¥Ñ•µÍô¤ì(€Ñ½…ÍÐ ‘‘•€œ­¥Ñ•µÌ¹±•¹Ñ ¬œ¡…¹¹•°œ¬¡¥Ñ•µÌ¹±•¹Ñ ôôôÄüœœèÌœ¤¬œÑ¼AÉ½™¥±”œ¤ì)ô)™Õ¹Ñ¥½¸Ñ½…ÍÐ¡µÍœ±‘ÕÉ…Ñ¥½¸¥ì(€±•ÐÐõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% }Ñ½…ÍÐœ¤ì(€¥˜ …Ð¥íÐõ‘½Õµ•¹Ð¹É•…Ñ•±•µ•¹Ð ‘¥Øœ¤íÐ¹¥ô}Ñ½…ÍÐœíÐ¹ÍÑå±”¹ÍÍQ•áÐôÁ½Í¥Ñ¥½¸é™¥á•í‰½ÑÑ½´èÈÑÁàí±•™ÐèÔÀ”íÑÉ…¹Í™½É´éÑÉ…¹Í±…Ñ•` ´ÔÀ”¤í‰…­É½Õ¹éÙ…È ´µ…ÉÈ¤í‰½É‘•ÈèÅÁàÍ½±¥Ù…È ´µ±¥¹”È¤í½±½ÈéÙ…È ´µ™œ¤íÁ…‘‘¥¹œèÄÁÁà€ÄáÁàí‰½É‘•ÈµÉ…‘¥ÕÌèáÁàíèµ¥¹‘•àèÈÀÀí™½¹ÐµÍ¥é”èÄÑÁàœí‘½Õµ•¹Ð¹‰½‘ä¹…ÁÁ•¹‘¡¥±¡Ð¤íô(€Ð¹Ñ•áÑ½¹Ñ•¹ÐõµÍœíÐ¹ÍÑå±”¹½Á…¥ÑäôœÄœì(€±•…ÉQ¥µ•½ÕÐ¡Ð¹} ¤íÐ¹} õÍ•ÑQ¥µ•½ÕÐ¡™Õ¹Ñ¥½¸ ¥íÐ¹ÍÑå±”¹½Á…¥ÑäôœÀœíô±‘ÕÉ…Ñ¥½¹ñðÈÈÀÀ¤ì(€Ð¹ÍÑå±”¹ÑÉ…¹Í¥Ñ¥½¸ô½Á…¥Ñä€¸ÍÌœì)ô(¼¼€´´´´5äQX€´´´´)±•Ð}ÑÙM½ÕÉ”ô}}™…Ù}|œì€€€¼¼€}}™…Ù}|œ½È„…Ñ•½Éä¹…µ”)±•Ð}ÑÙ¡…¹¹•±Ìõmtì)±•Ð}ÑÙA±…å¥¹œõ¹Õ±°ì)…Íå¹Œ™Õ¹Ñ¥½¸¥¹¥Ñ5åÑØ ¥ì(€…Ý…¥Ð‰Õ¥±‘QÙI…¥° ¤ì(€…Ý…¥Ð±½…‘QÙM½ÕÉ” }}™…Ù}|œ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸‰Õ¥±‘QÙI…¥° ¥ì(€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ¤ì(€½¹ÍÐÉ…¥°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙI…¥°œ¤ì(€±•Ð ôœñ‰ÕÑÑ½¸±…ÍÌô‰ÑÙÍÉŒœ¬¡}ÑÙM½ÕÉ”ôôô}}™…Ù}|œüœ½¸œèœœ¤¬œˆ‘…Ñ„µÍÉŒô‰}}™…Ù}|ˆùqÔÈØÀÔ€œ­ÑÈ …Ù½É¥Ñ”¡…¹¹•±Ìœ¤¬œð½‰ÕÑÑ½¸øœì(€™½È¡½¹ÍÐŒ½˜€¡È¹…Ñ•½É¥•Íññmt¤¤(€€€ ¬ôœñ‰ÕÑÑ½¸±…ÍÌô‰ÑÙÍÉŒœ¬¡}ÑÙM½ÕÉ”ôôõŒüœ½¸œèœœ¤¬œˆ‘…Ñ„µÍÉŒôˆœ­•ÍÑÑÈ¡Œ¤¬œˆøœ­}™±…½È¡Œ¤¬œ€œ­•ÍŒ¡Œ¤¬œð½‰ÕÑÑ½¸øœì(€É…¥°¹¥¹¹•É!Q50õ ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘QÙM½ÕÉ”¡ÍÉŒ¥ì(€}ÑÙM½ÕÉ”õÍÉŒì(€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹ÑÙÍÉŒœ¤¹™½É… ¡™Õ¹Ñ¥½¸¡ˆ¥íˆ¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½¸œ±ˆ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍÉŒœ¤ôôõÍÉŒ¤íô¤ì(€½¹ÍÐ‰½‘äõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙÕ¥‘•	½‘äœ¤ì(€‰½‘ä¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰Á…‘‘¥¹œèÄÙÁàˆù1½…‘¥¹œ¸¸¸ð½‘¥Øøœì(€¥˜¡ÍÉŒôôô}}™…Ù}|œ¥ì(€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½™…Ù½É¥Ñ•Ìœ¤ì(€€€}ÑÙ¡…¹¹•±Ìô¡È¹¡…¹¹•±Íññmt¤¹µ…À¡™Õ¹Ñ¥½¸¡Œ¥íÉ•ÑÕÉ¸íÍÑÉ•…µ}¥éŒ¹ÍÑÉ•…µ}¥±¹…µ”éŒ¹¹…µ”±…Ñ•½ÉäéŒ¹…Ñ•½Éåñðœœ±ÕÉ°éŒ¹ÕÉ°±±½¼éŒ¹±½½ñðœôíô¤ì(€õ•±Í•ì(€€€½¹ÍÐÈõ…Ý…¥Ð…Á¤ œ½…Á¤½¡…¹¹•±ÌýÄô™…Ðôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡ÍÉŒ¤¤ì(€€€}ÑÙ¡…¹¹•±Ìô¡È¹¡…¹¹•±Íññmt¤¹µ…À¡™Õ¹Ñ¥½¸¡Œ¥íÉ•ÑÕÉ¸íÍÑÉ•…µ}¥éŒ¹ÍÑÉ•…µ}¥±¹…µ”éŒ¹¹…µ”±…Ñ•½ÉäéŒ¹…Ñ•½Éåñðœœ±ÕÉ°éŒ¹ÕÉ°±±½¼éŒ¹±½½ñðœôíô¤ì(€ô(€…Ý…¥ÐÉ•™É•Í¡…ÙMÑ…Ñ” ¤ì(€€¼¼I•ÍÑ½É”A™É½´‘¥Í¬½µ•µ½Éä½¹±ä¸¹Ñ•É¥¹œ1¥Ù”QXµÕÍÐ¹½ÐÍ¥±•¹Ñ±ä(€€¼¼É•™É•Í Ñ¡”ÁÉ½Ù¥‘•ÈìÑ¡”UÁ‘…Ñ”A‰ÕÑÑ½¸É•µ…¥¹ÌÑ¡”¹•ÑÝ½É¬…Ñ¥½¸¸(€½¹ÍÐ•Á%‘Ìõ}ÑÙ¡…¹¹•±Ì¹µ…À¡™Õ¹Ñ¥½¸¡Œ¥íÉ•ÑÕÉ¸MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤íô¤¹™¥±Ñ•È¡	½½±•…¸¤ì(€¥˜¡•Á%‘Ì¹±•¹Ñ ¥ì(€€€ÑÉåí½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½•Áœý…¡•ôÄ™¥‘Ìôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡•Á%‘Ì¹©½¥¸ œ°œ¤¤¤í¥˜ …¨¹•ÉÉ½È¥}ÑÙÁœõ=‰©•Ð¹…ÍÍ¥¸¡íô±}ÑÙÁœ±¨¹•Áññíô¤íõ…Ñ ¡”¥íô(€ô(€É•¹‘•ÉQÙÕ¥‘” ¤ì(€µ…å‰•ÕÑ½I•™É•Í¡Áœ ¤ì)ô)±•Ð}ÑÙÁœõíôì€€€¼¼ÍÑÉ•…µ}¥€´ømíÑ¥Ñ±”±ÍÑ…ÉÑ}ÑÌ±ÍÑ½Á}ÑÍô°¸¸¹t)±•Ð}ÑÙÕÑ½Á¡•­ÐôÀ±}ÑÙÕÑ½Á	ÕÍäõ™…±Í”ì)…Íå¹Œ™Õ¹Ñ¥½¸µ…å‰•ÕÑ½I•™É•Í¡Áœ ¥ì(€½¹ÍÐ¹½Üõ…Ñ”¹¹½Ü ¤í¥˜¡}ÑÙÕÑ½Á	ÕÍåññ¹½Üµ}ÑÙÕÑ½Á¡•­ÐðÄÔ¨ØÀ¨ÄÀÀÀ¥É•ÑÕÉ¸ì(€}ÑÙÕÑ½Á¡•­Ðõ¹½Üí}ÑÙÕÑ½Á	ÕÍäõÑÉÕ”ì(€ÑÉåì(€€€€¼¼Q¡”Í•ÉÙ•È½¹Ñ…ÑÌÑ¡”ÁÉ½Ù¥‘•È½¹±ä™½Èµ¥ÍÍ¥¹œ½È€øÄÈµ¡½ÕÈµ½±É½ÝÌ¸(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½•Áœý™…Ù½É¥Ñ•ÌôÄœ¤ì(€€€¥˜ …¨¹•ÉÉ½È¥í}ÑÙÁœõ=‰©•Ð¹…ÍÍ¥¸¡íô±}ÑÙÁœ±¨¹•Áññíô¤í¥˜ …µåÑÙY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤¥É•¹‘•ÉQÙÕ¥‘” ¤íô(€õ…Ñ ¡”¥íõ™¥¹…±±åí}ÑÙÕÑ½Á	ÕÍäõ™…±Í”íô)ô(¼¼-••ÀÑ¡”Õ¥‘”±½¬µ½Ù¥¹œ•Ù•¸Ý¡•¸1¥Ù”QX¥Ì±•™Ð½Á•¸¸Q¡¥Ì½¹±ä(¼¼É”µÉ•¹‘•ÉÌ…±É•…‘ä…¡•‘…Ñ„ì¥Ð¹•Ù•ÈÉ•™É•Í¡•ÌA½Ù•ÈÑ¡”¹•ÑÝ½É¬¸)Í•Ñ%¹Ñ•ÉÙ…°¡™Õ¹Ñ¥½¸ ¥ì(€¥˜ …µåÑÙY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤˜™}ÑÙ¡…¹¹•±Ì¹±•¹Ñ ¥É•¹‘•ÉQÙÕ¥‘” ¤ì)ô°ØÀ¨ÄÀÀÀ¤ì)™Õ¹Ñ¥½¸É•¹‘•ÉQÙÕ¥‘” ¥ì(€½¹ÍÐ¡•…õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙQ¥µ•!•…œ¤ì(€½¹ÍÐ‰½‘äõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙÕ¥‘•	½‘äœ¤ì(€€¼¼Í¥µÁ±”Ñ¥µ”¡•…‘•È™É½´Ñ¡”ÕÉÉ•¹Ð¡…±˜¡½ÕÈ(€½¹ÍÐõ¹•Ü…Ñ” ¤í¹Í•Ñ5¥¹ÕÑ•Ì¡¹•Ñ5¥¹ÕÑ•Ì ¤ðÌÀüÀèÌÀ°À°À¤ì(€½¹ÍÐ‰…Í”õ¹•ÑQ¥µ” ¤ì(€½¹ÍÐÍ±½ÑMÑ…ÉÐõmtì(€™½È¡±•Ð¤ôÀí¤ðÔí¤¬¬¥íÍ±½ÑMÑ…ÉÐ¹ÁÕÍ ¡‰…Í”­¤¨ÌÀ¨ØÀÀÀÀ¤íô(€½¹ÍÐ¹½ÝAÐõ5…Ñ ¹µ…à À±5…Ñ ¹µ¥¸ ÄÀÀ°¡…Ñ”¹¹½Ü ¤µ‰…Í”¤¼ Ô¨ÌÀ¨ØÀÀÀÀ¤¨ÄÀÀ¤¤ì(€¡•…¹¥¹¹•É!Q50õÍ±½ÑMÑ…ÉÐ¹µ…À¡™Õ¹Ñ¥½¸¡µÌ¥í½¹ÍÐÐõ¹•Ü…Ñ”¡µÌ¤íÉ•ÑÕÉ¸€œñ‘¥Ø±…ÍÌô‰ÑÙÑ¥µ•Í±½Ðˆøœ¬ œÀœ­Ð¹•Ñ!½ÕÉÌ ¤¤¹Í±¥” ´È¤¬œèœ¬ œÀœ­Ð¹•Ñ5¥¹ÕÑ•Ì ¤¤¹Í±¥” ´È¤¬œð½‘¥Øøœíô¤¹©½¥¸ œœ¤¬œñÍÁ…¸±…ÍÌô‰ÑÙ¹½Ý¡•…ˆÍÑå±”ô‰±•™Ðèœ­¹½ÝAÐ¹Ñ½¥á• Ì¤¬œ”ˆøð½ÍÁ…¸øœì(€½¹ÍÐÝ¥¹MÑ…ÉÐõÍ±½ÑMÑ…ÉÑlÁt°Ý¥¹¹õÍ±½ÑMÑ…ÉÑlÑt¬ÌÀ¨ØÀÀÀÀì(€¥˜ …}ÑÙ¡…¹¹•±Ì¹±•¹Ñ ¥í‰½‘ä¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰µÕÑ•ˆÍÑå±”ô‰Á…‘‘¥¹œèÄÙÁàˆøœ­ÑÈ 9¼¡…¹¹•±Ì¡•É”¸œ¤¬œð½‘¥ØøœíÉ•ÑÕÉ¸íô(€±•Ð ôœœì(€™½È¡½¹ÍÐŒ½˜}ÑÙ¡…¹¹•±Ì¥ì(€€€½¹ÍÐÁ±…å¥¹œô¡}ÑÙA±…å¥¹œ„ôõ¹Õ±°˜™MÑÉ¥¹œ¡}ÑÙA±…å¥¹œ¤ôôõMÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤üœÁ±…å¥¹œœèœœì(€€€½¹ÍÐ™…Øõ}™…Ù¡…¹M•Ð¹¡…Ì¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤üœ½¸œèœœì(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰ÑÙÉ½Üˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆøœ(€€€€€€¬œñ‘¥Ø±…ÍÌô‰ÑÙ¡…¸œ­Á±…å¥¹œ¬œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆøœ(€€€€€€¬¡}ÑÙM½ÕÉ”ôôô}}™…Ù}|œüœñÍÁ…¸±…ÍÌô‰ÑÙ‘É…œˆ‘É……‰±”ô‰ÑÉÕ”ˆÑ¥Ñ±”ô‰É…œÑ¼É•½É‘•Èˆø˜ŒäÜÜØìð½ÍÁ…¸øœèœœ¤(€€€€€€¬œñ‰ÕÑÑ½¸±…ÍÌô‰ÑÙÙ±Œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆùY1ð½‰ÕÑÑ½¸øœ(€€€€€€¬¡Œ¹±½¼ý¡…¹¹•±1½¼¡Œ°ÑÙ±½¼œ¤èœñÍÁ…¸±…ÍÌô‰ÑÙ™±…œˆøœ­}™±…½È¡Œ¹…Ñ•½ÉåññŒ¹¹…µ”¤¬œð½ÍÁ…¸øœ¤(€€€€€€¬œñÍÁ…¸±…ÍÌô‰ÑÙ¹…µ”ˆøœ­•ÍŒ¡Œ¹¹…µ”¤¬œð½ÍÁ…¸øœ(€€€€€€¬œñÍÁ…¸±…ÍÌô‰™…ÙÍÑ…Èœ­™…Ø¬œˆ‘…Ñ„µÍ¥ôˆœ­•ÍÑÑÈ¡MÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤¤¬œˆ‘…Ñ„µ¹…µ”ôˆœ­•ÍÑÑÈ¡Œ¹¹…µ”¤¬œˆ‘…Ñ„µ…Ðôˆœ­•ÍÑÑÈ¡Œ¹…Ñ•½Éåñðœœ¤¬œˆÑ¥Ñ±”ô‰…Ù½É¥Ñ”ˆùqÔÈØÀÔð½ÍÁ…¸øœ(€€€€€€¬œð½‘¥Øøœ(€€€€€€¬œñ‘¥Ø±…ÍÌô‰ÑÙÁÉ½œˆÍÑå±”ôˆ´µ¹½ÝÁÐèœ­¹½ÝAÐ¹Ñ½¥á• Ì¤¬œ”ˆøœ­•Á•±±!Ñµ°¡Œ¹ÍÑÉ•…µ}¥±Ý¥¹MÑ…ÉÐ±Ý¥¹¹¤¬œð½‘¥Øøð½‘¥Øøœì(€ô(€‰½‘ä¹¥¹¹•É!Q50õ ì)ô)±•Ð}ÑÙÉ…M¥õ¹Õ±°ì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ‘É…ÍÑ…ÉÐœ±™Õ¹Ñ¥½¸¡”¥ì(€½¹ÍÐ¡…¹‘±”õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹ÑÙ‘É…œœ¤ì(€¥˜ …¡…¹‘±•ññ}ÑÙM½ÕÉ”„ôô}}™…Ù}|œ¥É•ÑÕÉ¸ì(€½¹ÍÐÉ½Üõ¡…¹‘±”¹±½Í•ÍÐ œ¹ÑÙÉ½Üœ¤ì(€}ÑÙÉ…M¥õÉ½ÜýÉ½Ü¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤é¹Õ±°ì(€¥˜ …}ÑÙÉ…M¥¥É•ÑÕÉ¸ì(€É½Ü¹±…ÍÍ1¥ÍÐ¹…‘ ÑÙ‘É…¥¹œœ¤ì(€”¹‘…Ñ…QÉ…¹Í™•È¹•™™•Ñ±±½Ý•ôµ½Ù”œì(€”¹‘…Ñ…QÉ…¹Í™•È¹Í•Ñ…Ñ„ Ñ•áÐ½Á±…¥¸œ±}ÑÙÉ…M¥¤ì)ô¤ì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ‘É…½Ù•Èœ±™Õ¹Ñ¥½¸¡”¥ì(€½¹ÍÐÉ½Üõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹ÑÙÉ½Üœ¤ì(€¥˜ …}ÑÙÉ…M¥‘ñð…É½Ýññ}ÑÙM½ÕÉ”„ôô}}™…Ù}|œ¥É•ÑÕÉ¸ì(€”¹ÁÉ•Ù•¹Ñ•™…Õ±Ð ¤ì(€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹ÑÙÉ½Ü¹ÑÙ‘É…½Ù•Èœ¤¹™½É… ¡ÈôùÈ¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ÑÙ‘É…½Ù•Èœ¤¤ì(€É½Ü¹±…ÍÍ1¥ÍÐ¹…‘ ÑÙ‘É…½Ù•Èœ¤ì(€”¹‘…Ñ…QÉ…¹Í™•È¹‘É½Á™™•Ðôµ½Ù”œì)ô¤ì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ‘É½Àœ±…Íå¹Œ™Õ¹Ñ¥½¸¡”¥ì(€½¹ÍÐÉ½Üõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹ÑÙÉ½Üœ¤ì(€¥˜ …}ÑÙÉ…M¥‘ñð…É½Ýññ}ÑÙM½ÕÉ”„ôô}}™…Ù}|œ¥É•ÑÕÉ¸ì(€”¹ÁÉ•Ù•¹Ñ•™…Õ±Ð ¤ì(€½¹ÍÐÑ…É•ÑM¥õÉ½Ü¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤ì(€½¹ÍÐ™É½´õ}ÑÙ¡…¹¹•±Ì¹™¥¹‘%¹‘•à¡ŒôùMÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤ôôõMÑÉ¥¹œ¡}ÑÙÉ…M¥¤¤ì(€½¹ÍÐÑ¼õ}ÑÙ¡…¹¹•±Ì¹™¥¹‘%¹‘•à¡ŒôùMÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥¤ôôõMÑÉ¥¹œ¡Ñ…É•ÑM¥¤¤ì(€¥˜¡™É½´øôÀ˜™Ñ¼øôÀ˜™™É½´„ôõÑ¼¥ì(€€€½¹ÍÐµ½Ù•õ}ÑÙ¡…¹¹•±Ì¹ÍÁ±¥”¡™É½´°Ä¥lÁtì(€€€}ÑÙ¡…¹¹•±Ì¹ÍÁ±¥”¡Ñ¼°À±µ½Ù•¤ì(€€€É•¹‘•ÉQÙÕ¥‘” ¤ì(€€€…Ý…¥Ð™…ÙA½ÍÐ¡í…Ñ¥½¸èÉ•½É‘•É}¡…¹¹•±Ìœ±ÍÑÉ•…µ}¥‘Ìé}ÑÙ¡…¹¹•±Ì¹µ…À¡ŒôùŒ¹ÍÑÉ•…µ}¥¥ô¤ì(€ô(€}ÑÙÉ…M¥õ¹Õ±°ì)ô¤ì)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ‘É…•¹œ±™Õ¹Ñ¥½¸ ¥ì(€}ÑÙÉ…M¥õ¹Õ±°ì(€‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œ¹ÑÙÉ½Ü¹ÑÙ‘É…¥¹œ°¹ÑÙÉ½Ü¹ÑÙ‘É…½Ù•Èœ¤¹™½É… ¡ÈôùÈ¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ÑÙ‘É…¥¹œœ°ÑÙ‘É…½Ù•Èœ¤¤ì)ô¤ì)™Õ¹Ñ¥½¸•Á]…±±±½­QÌ¡Ù…±Õ”±™…±±‰…¬¥ì(€€¼¼aÑÉ•…´ÌAÍÑ…ÉÑ€½•¹‘€ÍÑÉ¥¹Ì…É”Í¡•‘Õ±”Ý…±°µ±½¬Ù…±Õ•Ì¸M½µ”(€€¼¼Í•ÉÙ•ÉÌ…±Í¼•áÁ½Í”ÍÑ…ÉÑ}Ñ¥µ•ÍÑ…µÀ…Ì¥˜Ñ¡…ÐÝ…±°±½¬Ý•É”UQìÕÍ¥¹œ(€€¼¼Ñ¡…Ð•Á½ ¥¸„‰É½ÝÍ•ÈÑ¡•¸Í¡¥™ÑÌ9½ÉÝ•¥…¸±¥ÍÑ¥¹Ì‰ä€¬Ä¼¬È¡½ÕÉÌ¸(€€¼¼	Õ¥±Ñ¡”É…ÜÍ¡•‘Õ±”Ñ¥µ”¥¸Ñ¡”Ù¥•Ý•ÈÌ±½…°Ñ¥µ•é½¹”Ý¡•¸…Ù…¥±…‰±”¸(€½¹ÍÐÌõMÑÉ¥¹œ¡Ù…±Õ•ñðœœ¤¹ÑÉ¥´ ¤±´õÌ¹µ…Ñ  ½x¡qq‘ìÑô¤´¡qq‘ìÉô¤´¡qq‘ìÉô¥lQt¡qq‘ìÄ°Éô¤è¡qq‘ìÉô¤ üèè¡qq‘ìÉô¤¤ü¼¤ì(€¥˜¡´¥í½¹ÍÐõ¹•Ü…Ñ”¡9Õµ‰•È¡µlÅt¤±9Õµ‰•È¡µlÉt¤´Ä±9Õµ‰•È¡µlÍt¤±9Õµ‰•È¡µlÑt¤±9Õµ‰•È¡µlÕt¤±9Õµ‰•È¡µlÙuñðÀ¤¤í½¹ÍÐÑÌõ¹•ÑQ¥µ” ¤¼ÄÀÀÀí¥˜¡9Õµ‰•È¹¥Í¥¹¥Ñ”¡ÑÌ¤¥É•ÑÕÉ¸ÑÌíô(€É•ÑÕÉ¸9Õµ‰•È¡™…±±‰…¬¥ñðÀì)ô)™Õ¹Ñ¥½¸•Á•±±!Ñµ°¡Í¥±Ý¥¹MÑ…ÉÐ±Ý¥¹¹¥ì(€½¹ÍÐÁÉ½Ìõ}ÑÙÁmMÑÉ¥¹œ¡Í¥¥tì(€¥˜ …ÁÉ½Íñð…ÁÉ½Ì¹±•¹Ñ ¥É•ÑÕÉ¸€œñÍÁ…¸±…ÍÌô‰•Á¹½¹”µÕÑ•ˆøœ­ÑÈ 9¼ÁÉ½É…´¥¹™¼œ¤¬œð½ÍÁ…¸øœì(€½¹ÍÐ¹½ÝM•Œõ…Ñ”¹¹½Ü ¤¼ÄÀÀÀ±ÝÌõÝ¥¹MÑ…ÉÐ¼ÄÀÀÀ±Ý”õÝ¥¹¹¼ÄÀÀÀ±ÍÁ…¸õ5…Ñ ¹µ…à Ä±Ý”µÝÌ¤ì(€½¹ÍÐÑ¥µ•õÁÉ½Ì¹™¥±Ñ•È¡™Õ¹Ñ¥½¸¡À¥ì(€€€½¹ÍÐÍÑ…ÉÐõ•Á]…±±±½­QÌ¡À¹ÍÑ…ÉÐ±À¹ÍÑ…ÉÑ}ÑÌ¤±ÍÑ½Àõ•Á]…±±±½­QÌ¡À¹•¹±À¹ÍÑ½Á}ÑÌ¥ññÍÑ…ÉÐ¬ÄàÀÀì(€€€¥˜ …À¹Ñ¥Ñ±•ñð…ÍÑ…ÉÐ¥É•ÑÕÉ¸™…±Í”ì(€€€É•ÑÕÉ¸ÍÑ½ÀùÝÌ˜™ÍÑ…ÉÐñÝ”ì(€ô¤¹Í½ÉÐ¡™Õ¹Ñ¥½¸¡„±ˆ¥íÉ•ÑÕÉ¸•Á]…±±±½­QÌ¡„¹ÍÑ…ÉÐ±„¹ÍÑ…ÉÑ}ÑÌ¤µ•Á]…±±±½­QÌ¡ˆ¹ÍÑ…ÉÐ±ˆ¹ÍÑ…ÉÑ}ÑÌ¤íô¤ì(€¥˜ …Ñ¥µ•¹±•¹Ñ ¥ì(€€€€¼¼9•Ù•ÈÁ¥¸…¸•áÁ¥É•ÁÉ½É…µµ”Ñ¼Ñ¡”±•™Ð•‘”½˜Ñ¡”ÕÉÉ•¹ÐÉ¥¸(€€€€¼¼=¹±ä„•¹Õ¥¹•±äÕÁ½µ¥¹œ¥Ñ•´µ…äÕÍ”Ñ¡”½µÁ…Ð™…±±‰…¬‘¥ÍÁ±…ä¸(€€€½¹ÍÐ¹•áÐõÁÉ½Ì¹™¥±Ñ•È¡ÀôùÀ¹Ñ¥Ñ±”˜™•Á]…±±±½­QÌ¡À¹ÍÑ…ÉÐ±À¹ÍÑ…ÉÑ}ÑÌ¤øõÝÌ¤¹Í½ÉÐ ¡„±ˆ¤ôù•Á]…±±±½­QÌ¡„¹ÍÑ…ÉÐ±„¹ÍÑ…ÉÑ}ÑÌ¤µ•Á]…±±±½­QÌ¡ˆ¹ÍÑ…ÉÐ±ˆ¹ÍÑ…ÉÑ}ÑÌ¤¥lÁtì(€€€¥˜ …¹•áÐ¥É•ÑÕÉ¸€œñÍÁ…¸±…ÍÌô‰•Á¹½¹”µÕÑ•ˆøœ­ÑÈ 9¼ÁÉ½É…´¥¹™¼œ¤¬œð½ÍÁ…¸øœì(€€€½¹ÍÐ¹•áÑMÑ…ÉÐõ•Á]…±±±½­QÌ¡¹•áÐ¹ÍÑ…ÉÐ±¹•áÐ¹ÍÑ…ÉÑ}ÑÌ¤í¥˜¡¹•áÑMÑ…ÉÐøõÝ”¥É•ÑÕÉ¸€œñÍÁ…¸±…ÍÌô‰•Á¹½¹”µÕÑ•ˆøœ­ÑÈ 9¼ÁÉ½É…´¥¹™¼œ¤¬œð½ÍÁ…¸øœì(€€€±•ÐÑ´ôœœí¥˜¡¹•áÑMÑ…ÉÐ¥í½¹ÍÐÐõ¹•Ü…Ñ”¡¹•áÑMÑ…ÉÐ¨ÄÀÀÀ¤íÑ´ô œÀœ­Ð¹•Ñ!½ÕÉÌ ¤¤¹Í±¥” ´È¤¬œèœ¬ œÀœ­Ð¹•Ñ5¥¹ÕÑ•Ì ¤¤¹Í±¥” ´È¤íô(€€€É•ÑÕÉ¸€œñÍÁ…¸±…ÍÌô‰•Á™…±±‰…¬ˆøñÍÁ…¸±…ÍÌô‰•ÁÐˆøœ­Ñ´¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰•ÁÑ¥Ñ±”ˆøœ­•ÍŒ¡¹•áÐ¹Ñ¥Ñ±”¤¬œð½ÍÁ…¸øð½ÍÁ…¸øœì(€ô(€É•ÑÕÉ¸Ñ¥µ•¹µ…À¡™Õ¹Ñ¥½¸¡À¥ì(€€€½¹ÍÐÍÑ…ÉÐõ•Á]…±±±½­QÌ¡À¹ÍÑ…ÉÐ±À¹ÍÑ…ÉÑ}ÑÌ¤±É…ÝMÑ½Àõ•Á]…±±±½­QÌ¡À¹•¹±À¹ÍÑ½Á}ÑÌ¥ññÍÑ…ÉÐ¬ÄàÀÀ±ÍÑ½Àõ5…Ñ ¹µ…à¡ÍÑ…ÉÐ¬ØÀ±É…ÝMÑ½À¤ì(€€€½¹ÍÐÙ¥Í¥‰±•MÑ…ÉÐõ5…Ñ ¹µ…à¡ÝÌ±ÍÑ…ÉÐ¤±Ù¥Í¥‰±•MÑ½Àõ5…Ñ ¹µ¥¸¡Ý”±ÍÑ½À¤ì(€€€½¹ÍÐ±•™Ðõ5…Ñ ¹µ…à À°¡Ù¥Í¥‰±•MÑ…ÉÐµÝÌ¤½ÍÁ…¸¨ÄÀÀ¤±Ý¥‘Ñ õ5…Ñ ¹µ…à ¸à°¡Ù¥Í¥‰±•MÑ½ÀµÙ¥Í¥‰±•MÑ…ÉÐ¤½ÍÁ…¸¨ÄÀÀ¤ì(€€€½¹ÍÐ±¥Ù”õÍÑ…ÉÐðõ¹½ÝM•Œ˜™ÍÑ½Àù¹½ÝM•Œì(€€€½¹ÍÐÐõ¹•Ü…Ñ”¡ÍÑ…ÉÐ¨ÄÀÀÀ¤±Ñ´ô œÀœ­Ð¹•Ñ!½ÕÉÌ ¤¤¹Í±¥” ´È¤¬œèœ¬ œÀœ­Ð¹•Ñ5¥¹ÕÑ•Ì ¤¤¹Í±¥” ´È¤ì(€€€½¹ÍÐ±Ìô•ÁÁÉ½œœ¬¡±¥Ù”üœ±¥Ù”œèœœ¤¬¡Ý¥‘Ñ ðÄÈüœ½µÁ…Ðœèœœ¤ì(€€€É•ÑÕÉ¸€œñÍÁ…¸±…ÍÌôˆœ­±Ì¬œˆÍÑå±”ô‰±•™Ðèœ­±•™Ð¹Ñ½¥á• Ì¤¬œ”íÝ¥‘Ñ é…±Œ œ­Ý¥‘Ñ ¹Ñ½¥á• Ì¤¬œ”€´€ÉÁà¤ˆÑ¥Ñ±”ôˆœ­•ÍÑÑÈ¡Ñ´¬œ€œ­À¹Ñ¥Ñ±”¤¬œˆøñÍÁ…¸±…ÍÌô‰•ÁÐˆøœ­Ñ´¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰•ÁÑ¥Ñ±”ˆøœ­•ÍŒ¡À¹Ñ¥Ñ±”¤¬œð½ÍÁ…¸øð½ÍÁ…¸øœì(€ô¤¹©½¥¸ œœ¤ì)ô)™Õ¹Ñ¥½¸ÑÙA±…å•ÉÕ¥‘” ¥ì(€É•ÑÕÉ¸‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½È œµåÑÙY¥•Ü€¹ÑÙÕ¥‘”œ¤ì)ô)™Õ¹Ñ¥½¸ÑÙM•Ñ5¥¹¤¡µ¥¹¤¥ì(€½¹ÍÐÍ±½Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙA±…å•ÉM±½Ðœ¤±Õ¥‘”õÑÙA±…å•ÉÕ¥‘” ¤ì(€¥˜ …Í±½Ññð…Í±½Ð¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ½¸œ¤¥É•ÑÕÉ¸ì(€½¹ÍÐ¥¹1¥Ù•QØô…µåÑÙY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤ì(€¥˜¡µ¥¹¤¥ì(€€€¥˜¡Í±½Ð¹Á…É•¹Ñ±•µ•¹Ð„ôõ‘½Õµ•¹Ð¹‰½‘ä¥‘½Õµ•¹Ð¹‰½‘ä¹…ÁÁ•¹‘¡¥±¡Í±½Ð¤ì(€€€Í±½Ð¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” Í•Ñ¥½¹µ…àœ¤ì(€€€Í±½Ð¹±…ÍÍ1¥ÍÐ¹…‘ µ¥¹¤œ¤ì(€õ•±Í”¥˜¡¥¹1¥Ù•QØ¥ì(€€€Í±½Ð¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” µ¥¹¤œ°Í•Ñ¥½¹µ…àœ¤ì(€€€¥˜¡Õ¥‘”˜™Í±½Ð¹Á…É•¹Ñ±•µ•¹Ð„ôõÕ¥‘”¥Õ¥‘”¹…ÁÁ•¹‘¡¥±¡Í±½Ð¤ì(€õ•±Í•ì(€€€¥˜¡Í±½Ð¹Á…É•¹Ñ±•µ•¹Ð„ôõ‘½Õµ•¹Ð¹‰½‘ä¥‘½Õµ•¹Ð¹‰½‘ä¹…ÁÁ•¹‘¡¥±¡Í±½Ð¤ì(€€€Í±½Ð¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” µ¥¹¤œ¤ì(€€€Í±½Ð¹±…ÍÍ1¥ÍÐ¹…‘ Í•Ñ¥½¹µ…àœ¤ì(€ô(€½¹ÍÐ‰Ñ¸õÍ±½Ð¹ÅÕ•ÉåM•±•Ñ½È œ¹ÑÙµ¥¹‰Ñ¸œ¤±¡¥ÐõÍ±½Ð¹ÅÕ•ÉåM•±•Ñ½È œ¹ÑÙÙ¥‘•½¡¥Ðœ¤ì(€½¹ÍÐ±…‰•°õµ¥¹¤üÕ±±ÍÉ••¸Á±…å•Èœè5¥¹¥µ¥é”Á±…å•Èœì(€¥˜¡‰Ñ¸¥í‰Ñ¸¹Ñ¥Ñ±”õ±…‰•°í‰Ñ¸¹Í•ÑÑÑÉ¥‰ÕÑ” …É¥„µ±…‰•°œ±±…‰•°¤í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹Ðõµ¥¹¤üqÔÈÄäØœèqÔÈÄäàœíô(€¥˜¡¡¥Ð¥¡¥Ð¹Í•ÑÑÑÉ¥‰ÕÑ” …É¥„µ±…‰•°œ±±…‰•°¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸ÑÙA±…ä¡Í¥±¹…µ”¥ì(€½¹ÍÐÍ±½Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙA±…å•ÉM±½Ðœ¤±Õ¥‘”õÑÙA±…å•ÉÕ¥‘” ¤ì(€½¹ÍÐÝ…Í5¥¹¤õÍ±½Ð¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì µ¥¹¤œ¤ì(€}ÑÙA±…å¥¹œõÍ¥ì(€Í±½Ð¹±…ÍÍ1¥ÍÐ¹…‘ ½¸œ¤ì(€¥˜ …Ý…Í5¥¹¤˜™Õ¥‘”˜™Í±½Ð¹Á…É•¹Ñ±•µ•¹Ð„ôõÕ¥‘”¥Õ¥‘”¹…ÁÁ•¹‘¡¥±¡Í±½Ð¤ì(€Í±½Ð¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰ÑÙÁ±…å•É‰…ÈˆøñÍÁ…¸øœ­•ÍŒ¡¹…µ•ñðœœ¤¬œð½ÍÁ…¸øñ‘¥Ø±…ÍÌô‰ÑÙÁ±…å•É…Ñ¥½¹Ìˆøñ‰ÕÑÑ½¸ÑåÁ”ô‰‰ÕÑÑ½¸ˆ±…ÍÌô‰ÑÙµ¥¹‰Ñ¸ˆÑ¥Ñ±”ô‰5¥¹¥µ¥é”Á±…å•Èˆ…É¥„µ±…‰•°ô‰5¥¹¥µ¥é”Á±…å•Èˆ½¹±¥¬ô‰ÑÙQ½±•5¥¹¤ ¤ˆø˜ŒàØÀÀìð½‰ÕÑÑ½¸øñ‰ÕÑÑ½¸±…ÍÌô‰Á±½Í”ˆ½¹±¥¬ô‰ÑÙMÑ½À ¤ˆø™Ñ¥µ•Ììð½‰ÕÑÑ½¸øð½‘¥Øøð½‘¥ØøñÙ¥‘•¼¥ô‰ÑÙY¥‘•¼ˆ½¹ÑÉ½±Ì…ÕÑ½Á±…äÁ±…åÍ¥¹±¥¹”øð½Ù¥‘•¼øñ‰ÕÑÑ½¸ÑåÁ”ô‰‰ÕÑÑ½¸ˆ±…ÍÌô‰ÑÙÙ¥‘•½¡¥Ðˆ…É¥„µ±…‰•°ô‰5¥¹¥µ¥é”Á±…å•Èˆ½¹±¥¬ô‰ÑÙQ½±•5¥¹¤ ¤ˆøð½‰ÕÑÑ½¸øœì(€ÑÙM•Ñ5¥¹¤¡Ý…Í5¥¹¤¤ì(€É•¹‘•ÉQÙÕ¥‘” ¤ì(€½¹ÍÐÙ¥‘•¼õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙY¥‘•¼œ¤ì(€±•ÐÕÉ±Ìì(€ÑÉåíÕÉ±Ìõ…Ý…¥Ð…Á¤ œ½…Á¤½¡±Ìý¥ôœ­•¹½‘•UI%½µÁ½¹•¹Ð¡Í¥¤¤í¥˜¡ÕÉ±Ì¹•ÉÉ½Éñð…ÕÉ±Ì¹¡±Ì¥Ñ¡É½Ü¹•ÜÉÉ½È ÍÑÉ•…´ÕÉ°œ¤íõ…Ñ ¡”¥íÉ•ÑÕÉ¸íô(€¥˜¡Ý¥¹‘½Ü¹}ÑÙA±…å‰…­½¹ÑÉ½±±•È¥íÝ¥¹‘½Ü¹}ÑÙA±…å‰…­½¹ÑÉ½±±•È¹ÍÑ½À ¤íÝ¥¹‘½Ü¹}ÑÙA±…å‰…­½¹ÑÉ½±±•Èõ¹Õ±°íô(€Ý¥¹‘½Ü¹}ÑÙA±…å‰…­½¹ÑÉ½±±•ÈõÍÑ…ÉÑMµ…ÉÑMÑÉ•…´¡Ù¥‘•¼±ÕÉ±Ì±™Õ¹Ñ¥½¸¡Ì¥ì(€€€½¹ÍÐ‰…ÈõÍ±½Ð¹ÅÕ•ÉåM•±•Ñ½È œ¹ÑÙÁ±…å•É‰…ÈÍÁ…¸œ¤í¥˜¡‰…È¥‰…È¹Ñ¥Ñ±”õÍñðœœì(€ô±™Õ¹Ñ¥½¸¡ ±Ð¥íÝ¥¹‘½Ü¹}ÑÙ¡±Ìõ íÝ¥¹‘½Ü¹}ÑÙµÁ•ÑÌõÐíô¤ì)ô)™Õ¹Ñ¥½¸ÑÙQ½±•5¥¹¤ ¥ì(€½¹ÍÐÍ±½Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙA±…å•ÉM±½Ðœ¤ì(€¥˜ …Í±½Ññð…Í±½Ð¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ½¸œ¤¥É•ÑÕÉ¸ì(€½¹ÍÐ¥¹1¥Ù•QØô…µåÑÙY¥•Ü¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤ì(€¥˜¡Í±½Ð¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì µ¥¹¤œ¤¥ì(€€€¥˜¡¥¹1¥Ù•QØ¥íÑÙM•Ñ5¥¹¤¡™…±Í”¤íÉ•ÑÕÉ¸íô(€€€ÑÙM•Ñ5¥¹¤¡™…±Í”¤ì(€€€É•ÅÕ•ÍÑA±…å•ÉÕ±±ÍÉ••¸¡Í±½Ð¤ì(€€€É•ÑÕÉ¸ì(€ô(€¥˜¡Á±…å•ÉÕ±±ÍÉ••¹±•µ•¹Ð ¤ôôõÍ±½Ð¥•á¥ÑA±…å•ÉÕ±±ÍÉ••¸ ¤ì(€ÑÙM•Ñ5¥¹¤¡ÑÉÕ”¤ì)ô)™Õ¹Ñ¥½¸ÑÙMÑ½À ¥ì(€}ÑÙA±…å¥¹œõ¹Õ±°ì(€‘½Õµ•¹Ð¹‰½‘ä¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ÑÙÍ•Ñ¥½¹Á±…äœ¤ì(€¥˜¡Ý¥¹‘½Ü¹}ÑÙA±…å‰…­½¹ÑÉ½±±•È¥íÝ¥¹‘½Ü¹}ÑÙA±…å‰…­½¹ÑÉ½±±•È¹ÍÑ½À ¤íÝ¥¹‘½Ü¹}ÑÙA±…å‰…­½¹ÑÉ½±±•Èõ¹Õ±°íô(€¥˜¡Ý¥¹‘½Ü¹}ÑÙ¡±Ì¥íÑÉåíÝ¥¹‘½Ü¹}ÑÙ¡±Ì¹‘•ÍÑÉ½ä ¤íõ…Ñ ¡”¥íõÝ¥¹‘½Ü¹}ÑÙ¡±Ìõ¹Õ±°íô(€¥˜¡Ý¥¹‘½Ü¹}ÑÙµÁ•ÑÌ¥í‘•ÍÑÉ½å5Á•ÑÍA±…å•È¡Ý¥¹‘½Ü¹}ÑÙµÁ•ÑÌ¤íÝ¥¹‘½Ü¹}ÑÙµÁ•ÑÌõ¹Õ±°íô(€½¹ÍÐÍ±½Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÑÙA±…å•ÉM±½Ðœ¤±Õ¥‘”õÑÙA±…å•ÉÕ¥‘” ¤ì(€Í±½Ð¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ½¸œ°µ¥¹¤œ°Í•Ñ¥½¹µ…àœ¤ì(€¥˜¡Õ¥‘”˜™Í±½Ð¹Á…É•¹Ñ±•µ•¹Ð„ôõÕ¥‘”¥Õ¥‘”¹…ÁÁ•¹‘¡¥±¡Í±½Ð¤ì(€Í±½Ð¹¥¹¹•É!Q50ôœœì(€É•¹‘•ÉQÙÕ¥‘” ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸•ÁI•™É•Í  ¥ì(€½¹ÍÐ‰Ñ¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% •ÁI•™É•Í œ¤ì(€½¹ÍÐ½±õ‰Ñ¸¹¥¹¹•É!Q50ì(€‰Ñ¸¹¥¹¹•É!Q50ôœñÍÁ…¸øœ­ÑÈ 1½…‘¥¹œA¸¸¸œ¤¬œð½ÍÁ…¸øœí‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”ì(€±•Ðµ½‘…°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% •Á1½…‘AÉ½É•ÍÌœ¤ì(€¥˜ …µ½‘…°¥ì(€€€µ½‘…°õ‘½Õµ•¹Ð¹É•…Ñ•±•µ•¹Ð ‘¥Øœ¤íµ½‘…°¹¥ô•Á1½…‘AÉ½É•ÍÌœíµ½‘…°¹±…ÍÍ9…µ”ô•Á±½…‘‰…¬œì(€€€µ½‘…°¹¥¹¹•É!Q50ôœñ‘¥Ø±…ÍÌô‰•Á±½…‘‰½àˆøñ‘¥Ø±…ÍÌô‰•Á±½…‘Ñ¥Ñ±”ˆøœ­•ÍŒ¡ÑÈ UÁ‘…Ñ¥¹œQXÕ¥‘”œ¤¤¬œð½‘¥Øøñ‘¥Ø±…ÍÌô‰•Á±½…‘ÍÑ…”ˆ¥ô‰•Á1½…‘MÑ…”ˆøð½‘¥Øøñ‘¥Ø±…ÍÌô‰•Á±½…‘‰…ÈˆøñÍÁ…¸¥ô‰•Á1½…‘	…Èˆøð½ÍÁ…¸øð½‘¥Øøñ‘¥Ø±…ÍÌô‰•Á±½…‘µ•Ñ„ˆøñÍÁ…¸¥ô‰•Á1½…‘½Õ¹ÐˆøÀ€¼€Àð½ÍÁ…¸øñÍÁ…¸¥ô‰•Á1½…‘½Õ¹ˆøð½ÍÁ…¸øð½‘¥Øøð½‘¥Øøœì(€€€‘½Õµ•¹Ð¹‰½‘ä¹…ÁÁ•¹‘¡¥±¡µ½‘…°¤ì(€õ•±Í”µ½‘…°¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€½¹ÍÐÍÑ…”õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% •Á1½…‘MÑ…”œ¤±‰…Èõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% •Á1½…‘	…Èœ¤±½Õ¹Ðõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% •Á1½…‘½Õ¹Ðœ¤±™½Õ¹õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% •Á1½…‘½Õ¹œ¤ì(€ÑÉåì(€€€ÍÑ…”¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ¥¹‘¥¹œ¡…¹¹•±Ì¥¸å½ÕÈ™…Ù½É¥Ñ•Ì¸¸¸œ¤í½Õ¹Ð¹Ñ•áÑ½¹Ñ•¹Ðôœœí™½Õ¹¹Ñ•áÑ½¹Ñ•¹Ðôœœí‰…È¹ÍÑå±”¹Ý¥‘Ñ ôœÌ”œì(€€€½¹ÍÐÁ±…¸õ…Ý…¥Ð…Á¤ œ½…Á¤½•Á}Ñ…É•ÑÌœ¤ì(€€€¥˜¡Á±…¸¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡Á±…¸¹•ÉÉ½ÉñðA™…¥±•œ¤ì(€€€€¼¼A½ÁÕ±…Ñ”Ý¡…ÐÑ¡”ÕÍ•È¥Ì±½½­¥¹œ…Ð™¥ÉÍÐ¸Q¡”½µÁ±•Ñ”™…Ù½É¥Ñ”½…Ñ•½Éä(€€€€¼¼Õ¥‘”ÍÑ¥±°É•™É•Í¡•Ì…™Ñ•ÉÝ…É‘Ì°‰ÕÐ„±…É”A¹¼±½¹•Èµ…­•ÌÑ¡”(€€€€¼¼ÕÉÉ•¹Ñ±ä½Á•¸…Ñ•½ÉäÝ…¥Ð‰•¡¥¹¡Õ¹‘É•‘Ì½˜Õ¹É•±…Ñ•¡…¹¹•±Ì¸(€€€½¹ÍÐÙ¥Í¥‰±•%‘Ìõ}ÑÙ¡…¹¹•±Ì¹µ…À¡ŒôùMÑÉ¥¹œ¡Œ¹ÍÑÉ•…µ}¥‘ñðœœ¤¤¹™¥±Ñ•È¡	½½±•…¸¤±Ù¥Í¥‰±•M•Ðõ¹•ÜM•Ð¡Ù¥Í¥‰±•%‘Ì¤ì(€€€½¹ÍÐÁ±…¹¹•ô¡Á±…¸¹¥‘Íññmt¤¹µ…À¡MÑÉ¥¹œ¤±¥‘ÌõÙ¥Í¥‰±•%‘Ì¹™¥±Ñ•È¡¥ôùÁ±…¹¹•¹¥¹±Õ‘•Ì¡¥¤¤¹½¹…Ð¡Á±…¹¹•¹™¥±Ñ•È¡¥ôø…Ù¥Í¥‰±•M•Ð¹¡…Ì¡¥¤¤¤ì(€€€½¹ÍÐÑ½Ñ…°õ¥‘Ì¹±•¹Ñ í±•ÐÕÁ‘…Ñ•ôÀ±¹½ÁœôÀ±™…¥±•ôÀ±Í…™•5½‘”õ™…±Í”ì(€€€½Õ¹Ð¹Ñ•áÑ½¹Ñ•¹ÐõÑ½Ñ…°¬œ€œ­ÑÈ ¡…¹¹•±Ìœ¤ì(€€€™½Õ¹¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ =¹”‰Õ±¬Õ¥‘”‘½Ý¹±½…œ¤ì(€€€ÍÑ…”¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ½Ý¹±½…‘¥¹œ…¹ÁÉ½•ÍÍ¥¹œÑ¡”ÁÉ½Ù¥‘•ÈQXÕ¥‘”¸¸¸œ¤í‰…È¹ÍÑå±”¹Ý¥‘Ñ ôœÄà”œì(€€€±•ÐÝ…¥ÑMÑ•ÀôÀì(€€€½¹ÍÐÝ…¥Ñ5•ÍÍ…•ÌõmÑÈ A…ÉÍ¥¹œÁÉ½É…µµ”¥¹™½Éµ…Ñ¥½¸¸¸¸œ¤±ÑÈ 5…Ñ¡¥¹œÕ¥‘”‘…Ñ„Ñ¼™…Ù½É¥Ñ”¡…¹¹•±Ì¸¸¸œ¤±ÑÈ 1…É”ÁÉ½Ù¥‘•ÈÕ¥‘•Ìµ…äÑ…­”„±¥ÑÑ±”Ý¡¥±”¸¸¸œ¥tì(€€€½¹ÍÐÝ…¥ÑQ¥µ•ÈõÍ•Ñ%¹Ñ•ÉÙ…°  ¤ôùíÍÑ…”¹Ñ•áÑ½¹Ñ•¹ÐõÝ…¥Ñ5•ÍÍ…•Ím5…Ñ ¹µ¥¸¡Ý…¥ÑMÑ•À¬¬±Ý…¥Ñ5•ÍÍ…•Ì¹±•¹Ñ ´Ä¥tí‰…È¹ÍÑå±”¹Ý¥‘Ñ õ5…Ñ ¹µ¥¸ àÈ°Èà­Ý…¥ÑMÑ•À¨ÄÐ¤¬œ”œíô°ÈÈÀÀ¤ì(€€€±•Ð¨ì(€€€ÑÉåí¨õ…Ý…¥Ð…Á¤ œ½…Á¤½•Áœý™½É”ôÄ™™…Ù½É¥Ñ•ÌôÄœ¤íõ™¥¹…±±åí±•…É%¹Ñ•ÉÙ…°¡Ý…¥ÑQ¥µ•È¤íô(€€€¥˜¡¨¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡¨¹•ÉÉ½ÉñðA™…¥±•œ¤ì(€€€}ÑÙÁœõ=‰©•Ð¹…ÍÍ¥¸¡íô±}ÑÙÁœ±¨¹•Áññíô¤ì(€€€½¹ÍÐÌõ¨¹ÍÑ…ÑÍññíôíÕÁ‘…Ñ•õ9Õµ‰•È¡Ì¹ÕÁ‘…Ñ•¥ñðÀí¹½Áœõ9Õµ‰•È¡Ì¹¹½}‘…Ñ„¥ñðÀí™…¥±•õ9Õµ‰•È¡Ì¹™…¥±•¥ñðÀíÍ…™•5½‘”ô„…Ì¹Í…™•}µ½‘”ì(€€€½¹ÍÐ‰Õ±¬õ9Õµ‰•È¡Ì¹áµ±ÑÙ}™¥±±•¥ñðÀ±™…±±‰…¬õ9Õµ‰•È¡Ì¹™…±±‰…­}ÕÁ‘…Ñ•¥ñðÀì(€€€½Õ¹Ð¹Ñ•áÑ½¹Ñ•¹ÐõÑ½Ñ…°¬œ€œ­ÑÈ ¡…¹¹•±Ì¡•­•œ¤ì(€€€™½Õ¹¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ a51QXœ¤¬œ€œ­‰Õ±¬¬¡™…±±‰…¬ü œƒ
Ü€œ­ÑÈ …±±‰…¬œ¤¬œ€œ­™…±±‰…¬¤èœœ¤¬œƒ
Ü€œ­ÑÈ 9¼Aœ¤¬œ€œ­¹½Áœ¬¡™…¥±•ü œƒ
Ü€œ­ÑÈ …¥±•œ¤¬œ€œ­™…¥±•¤èœœ¤í‰…È¹ÍÑå±”¹Ý¥‘Ñ ôœÄÀÀ”œì(€€€É•¹‘•ÉQÙÕ¥‘” ¤ì(€€€ÍÑ…”¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ QXÕ¥‘”¥ÌÉ•…‘ä¸œ¤¬œ€œ­ÕÁ‘…Ñ•¬œ€œ­ÑÈ ¡…¹¹•±ÌÕÁ‘…Ñ•¸œ¤í‰…È¹ÍÑå±”¹Ý¥‘Ñ ôœÄÀÀ”œì(€€€¥˜ …Ñ½Ñ…°¥íÑ½…ÍÐ¡ÑÈ 9¼™…Ù½É¥Ñ•ÌÑ¼±½…A™½È¸œ¤¤íô(€€€•±Í”Ñ½…ÍÐ¡ÑÈ A±½…‘•œ¤¬œè€œ­ÑÈ UÁ‘…Ñ•œ¤¬œ€œ­ÕÁ‘…Ñ•¬œƒ
Ü€œ­ÑÈ 9¼Aœ¤¬œ€œ­¹½Áœ¬œƒ
Ü€œ­ÑÈ …¥±•œ¤¬œ€œ­™…¥±•°ÜÀÀÀ¤ì(€õ…Ñ ¡”¥íÍÑ…”¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ A™…¥±•œ¤¬œè€œ­MÑÉ¥¹œ¡”˜™”¹µ•ÍÍ…•ññ”¤í‰…È¹ÍÑå±”¹‰…­É½Õ¹ôœŒá˜ÉÌÔœíÑ½…ÍÐ¡ÑÈ A™…¥±•œ¤¤í…Ý…¥Ð¹•ÜAÉ½µ¥Í”¡É•Í½±Ù”ôùÍ•ÑQ¥µ•½ÕÐ¡É•Í½±Ù”°ÈàÀÀ¤¤íô(€…Ý…¥Ð¹•ÜAÉ½µ¥Í”¡É•Í½±Ù”ôùÍ•ÑQ¥µ•½ÕÐ¡É•Í½±Ù”°ØÔÀ¤¤ì(€¥˜¡µ½‘…°¥µ½‘…°¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤ì(€‰Ñ¸¹¥¹¹•É!Q50õ½±í‰Ñ¸¹‘¥Í…‰±•õ™…±Í”ì)ô(¼¼Ù•¹Ð‘•±•…Ñ¥½¸è…¹ä½Áä‰ÕÑÑ½¸Ì‘…Ñ„µÕÉ°¥Ì½Á¥•½¸±¥¬¸)‘½Õµ•¹Ð¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È ±¥¬œ±™Õ¹Ñ¥½¸¡”¥ì(€½¹ÍÐÍ•ÕÉ•áÁ…¹õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Í•ÕÉ•µ…Ñ¡•áÁ…¹œ¤ì(€¥˜¡Í•ÕÉ•áÁ…¹¥íÑ½±•M•ÕÉ•5…Ñ¡•Ì¡Í•ÕÉ•áÁ…¹¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ™¥áÑÕÉ•¡…¹¹•±Q¥Ñ±”õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹™¥áÑÕÉ•¡…¹¹•±Ñ¥Ñ±•m‘…Ñ„µÍ¥‘tœ¤ì(€¥˜¡™¥áÑÕÉ•¡…¹¹•±Q¥Ñ±”¥íÁ±…å	É½ÝÍ•È¡™¥áÑÕÉ•¡…¹¹•±Q¥Ñ±”¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤±™¥áÑÕÉ•¡…¹¹•±Q¥Ñ±”¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¹…µ”œ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ¥µ•±¥¹•Q•…µ¥áÑÕÉ”õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Ñ•…µ™¥áÑÕÉ•m‘…Ñ„µÁÉ½™¥±”µ™¥áÑÕÉ”ôˆÄ‰tœ¤ì(€¥˜¡Ñ¥µ•±¥¹•Q•…µ¥áÑÕÉ”¥íÍ¡½ÝQ•…µÌ¡Ñ¥µ•±¥¹•Q•…µ¥áÑÕÉ”¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ•…µ¥áÑÕÉ”õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Ñ•…µ™¥áÑÕÉ•m‘…Ñ„µ™¥áÑÕÉ”µ…ÉôˆÄ‰tœ¤ì(€¥˜¡Ñ•…µ¥áÑÕÉ”˜˜…”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹‰Ñ¹Á±…ä°¹‰Ñ¹Ù±Œ°¹‰¡•…œ¤¥í½¹ÍÐ‘•Ñ…¥±ÌõÑ•…µ¥áÑÕÉ”¹ÅÕ•ÉåM•±•Ñ½È œ¹Ñ•…µ™¥áÑÕÉ•‰É½…‘…ÍÑÌœ¤±½Á•¹¥¹œõ‘•Ñ…¥±Ì˜™‘•Ñ…¥±Ì¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤í‘½Õµ•¹Ð¹ÅÕ•ÉåM•±•Ñ½É±° œÑ•…µÍY¥•Ü€¹Ñ•…µ™¥áÑÕÉ”¹Í•±•Ñ•‘™¥áÑÕÉ”œ¤¹™½É… ¡…Éôù…É¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” Í•±•Ñ•‘™¥áÑÕÉ”œ¤¤íÑ•…µ¥áÑÕÉ”¹±…ÍÍ1¥ÍÐ¹Ñ½±” Í•±•Ñ•‘™¥áÑÕÉ”œ°„…½Á•¹¥¹œ¤í¥˜¡‘•Ñ…¥±Ì¥‘•Ñ…¥±Ì¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ¤í¥˜¡½Á•¹¥¹œ¥±½…‘MÑ½É•‘¥áÑÕÉ•¡…¹¹•±Ì¡Ñ•…µ¥áÑÕÉ”¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ•…µI•µ½Ù”õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Ñ•…µÉ•µ½Ù”œ¤ì(€¥˜¡Ñ•…µI•µ½Ù”¥íÉ•µ½Ù•Q•…µ…Ù½É¥Ñ”¡Ñ•…µI•µ½Ù”¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µ¹…µ”œ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ•…µ…Øõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Ñ•…µ™…Ù¥Ñ•µm‘…Ñ„µÑ•…´µÍ•…É¡tœ¤ì(€¥˜¡Ñ•…µ…Ø¥íÍ•±•Ñ5åQ•…´¡Ñ•…µ…Ø¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µÍ•…É œ¥ñðœœ±Ñ•…µ…Ø¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µ¥œ¥ñðœœ±Ñ•…µ…Ø¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µ±½¼œ¥ñðœœ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ•…µMÑ…Èõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Ñ•…µÍÑ…Èœ¤ì(€¥˜¡Ñ•…µMÑ…È¥íÑ½±•Q•…µ…Ù½É¥Ñ”¡Ñ•…µMÑ…È¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µ¹…µ”œ¤±Ñ•…µMÑ…È±Ñ•…µMÑ…È¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µ¥œ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ•…µ¥¹‘¥áÑÕÉ•Ìõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Ñ•…µ™¥¹‘™¥áÑÕÉ•Ím‘…Ñ„µÑ•…´µ™¥áÑÕÉ•Ítœ¤ì(€¥˜¡Ñ•…µ¥¹‘¥áÑÕÉ•Ì¥í™¥¹‘MÁ½ÉÑÍ¥áÑÕÉ•Ì¡Ñ•…µ¥¹‘¥áÑÕÉ•Ì¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µ™¥áÑÕÉ•Ìœ¥ñðœœ±Ñ•…µ¥¹‘¥áÑÕÉ•Ì¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µ¥œ¥ñðœœ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ•…µM•…É¡!¥Ðõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Ñ•…µÍ•…É¡¡¥Ñm‘…Ñ„µÑ•…´µÍ•±•Ñtœ¤ì(€¥˜¡Ñ•…µM•…É¡!¥Ð¥íÍ•±•Ñ5åQ•…´¡Ñ•…µM•…É¡!¥Ð¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µÍ•±•Ðœ¥ñðœœ±Ñ•…µM•…É¡!¥Ð¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´µ¥œ¥ñðœœ°œœ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÍ½ÕÉ•áÁ…¹õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹±…Ñ•ÍÑÍ½ÕÉ••áÁ…¹œ¤ì(€¥˜¡Í½ÕÉ•áÁ…¹¥í½¹ÍÐ‰½àõÍ½ÕÉ•áÁ…¹¹Á…É•¹Ñ±•µ•¹Ð¹ÅÕ•ÉåM•±•Ñ½È œ¹±…Ñ•ÍÑÍ½ÕÉ•Ìœ¤í¥˜¡‰½à¥‰½à¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ¥µ•±¥¹•…µ”õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹µå±¥ÍÑÑ¥µ•±¥¹•…µ”œ¤ì(€¥˜¡Ñ¥µ•±¥¹•…µ”¥í½¹ÍÐÕÉ°õÑ¥µ•±¥¹•…µ”¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÕÉ°œ¤í¥˜¡ÕÉ°¥Ý¥¹‘½Ü¹½Á•¸¡ÕÉ°°}‰±…¹¬œ°¹½½Á•¹•Èœ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑ¥µ•±¥¹•Äõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹µå±¥ÍÑÑ¥µ•±¥¹•˜Äœ¤ì(€¥˜¡Ñ¥µ•±¥¹•Ä¥íÍ¡½ÝI…¥¹œ¡Ñ¥µ•±¥¹•Ä¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ‘É¥Ù•Èµ­•äœ¥ñðœœ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÉ…¥¹Ù•¹Ðõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹É…¥¹•Ù•¹Ðœ¤ì(€¥˜¡É…¥¹Ù•¹Ð˜˜…”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹‰Ñ¹Á±…ä°¹‰Ñ¹Ù±Œ°¹‰¡•…œ¤¥í¥˜¡É…¥¹Ù•¹Ð¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡…Í¡…¹¹•±Ìœ¤¥í½¹ÍÐ‰½àõÉ…¥¹Ù•¹Ð¹ÅÕ•ÉåM•±•Ñ½È œ¹É…¥¹•Ù•¹Ñ¡…¹¹•±Ìœ¤í¥˜¡‰½à¥‰½à¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ¤íÉ•ÑÕÉ¸íõ½¹ÍÐÕÉ°õÉ…¥¹Ù•¹Ð¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÕÉ°œ¤í¥˜¡ÕÉ°¥Ý¥¹‘½Ü¹½Á•¸¡ÕÉ°°}‰±…¹¬œ°¹½½Á•¹•Èœ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ±•Øõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹±…Ñ•ÍÑ•Á¥Í½‘•Ù±Œœ¤ì(€¥˜¡±•Ø¥íÁ±…å1…Ñ•ÍÑÁ¥Í½‘”¡±•Ø¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¥œ¤±±•Ø¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ•áÐœ¤±±•Ø¤íÉ•ÑÕÉ¸íô(€½¹ÍÐµå1¥ÍÑM¡½Üõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹µå±¥ÍÑÍ¡½Ý…Éœ¤ì(€¥˜¡µå1¥ÍÑM¡½Ü¥í½¹ÍÐÍ¥õµå1¥ÍÑM¡½Ü¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•É¥•Ìœ¥ñðœœ±¥õµå1¥ÍÑM¡½Ü¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ñ…±½œœ¥ñðœœí¥˜¡Í¥¥íÍ¡½ÝM¡½ÝÌ ¤í±½…‘M¡½Ü¡Í¥¤íÉ•ÑÕÉ¸íõ¥˜¡¥¥íÍ¡½ÝM¡½ÝÌ ¤í±½…‘áÑ•É¹…±M¡½Ü¡¥¤íÉ•ÑÕÉ¸íõô(€½¹ÍÐ±…Ñ•ÍÑM¡½Üõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹±…Ñ•ÍÑÍ¡½Ý…Éœ¤ì(€¥˜¡±…Ñ•ÍÑM¡½Ü¥í½¹ÍÐÍ¥õ±…Ñ•ÍÑM¡½Ü¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•É¥•Ìœ¥ñðœœ±¥õ±…Ñ•ÍÑM¡½Ü¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ñ…±½œœ¥ñðœœí¥˜¡Í¥¥í±½…‘M¡½Ü¡Í¥¤íÉ•ÑÕÉ¸íõ¥˜¡¥¥í±½…‘áÑ•É¹…±M¡½Ü¡¥¤íÉ•ÑÕÉ¸íõô(€½¹ÍÐÍÌõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Í¡½ÝÍÑ…Èœ¤ì(€¥˜¡ÍÌ¥íÑ½±•M¡½Ý…Ù½É¥Ñ”¡í…Ñ…±½}¥éÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ñ…±½œœ¤±Í¡½Ý}­•äéÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¡½Üµ­•äœ¥ññÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ­•äœ¤±Í•É¥•Í}¥éÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•É¥•Ìœ¥ññ¹Õ±°±Í•É¥•Í}¥‘Ìè¡ÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•É¥•Ìµ¥‘Ìœ¥ññÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•É¥•Ìœ¥ñðœœ¤¹ÍÁ±¥Ð œ°œ¤¹™¥±Ñ•È¡	½½±•…¸¤±¹…µ”éÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¹…µ”œ¤±½Ù•ÈéÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ½Ù•Èœ¤±å•…ÈéÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µå•…Èœ¤±É…Ñ¥¹œéÍÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÉ…Ñ¥¹œœ¥ô±ÍÌ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÍÈõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Í¡½ÝÉ•µ½Ù”œ¤ì(€¥˜¡ÍÈ¥íÉ•µ½Ù•M¡½Ý…Ù½É¥Ñ”¡ÍÈ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ­•äœ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÍŒõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Í¡½Ý…Éœ¤ì(€¥˜¡ÍŒ¥í½¹ÍÐ¥‘ÌõÍŒ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•É¥•Ìœ¥ñðœœí¥˜¡¥‘Ì¥±½…‘M¡½Ü¡¥‘Ì¤í•±Í”¥˜¡ÍŒ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ñ…±½œœ¤¥±½…‘áÑ•É¹…±M¡½Ü¡ÍŒ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ñ…±½œœ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÍ˜õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Í¡½Ý™…Øœ¤ì(€¥˜¡Í˜¥í½¹ÍÐ¥‘ÌõÍ˜¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•É¥•Ìœ¥ñðœœí¥˜¡¥‘Ì¥±½…‘M¡½Ü¡¥‘Ì¤í•±Í”¥˜¡Í˜¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ñ…±½œœ¤¥±½…‘áÑ•É¹…±M¡½Ü¡Í˜¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ñ…±½œœ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ•Øõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹•Á¥Í½‘•Ù±Œœ¤ì(€¥˜¡•Ø¥íÁ±…åÁ¥Í½‘•EÕ•Õ”¡•Ø¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ•…Í½¸œ¤±•Ø¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ•Á¥Í½‘”œ¤±•Ø¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ½ÕÉ”œ¤±•Ø¤íÉ•ÑÕÉ¸íô(€½¹ÍÐµØõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹µ½Ù¥•Ù±Œœ¤ì(€¥˜¡µØ¥íÁ±…å5½Ù¥•Y1¡µØ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤±µØ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ•áÐœ¤±µØ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐµÌõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹µ½Ù¥•ÍÑ…Èœ¤ì(€¥˜¡µÌ¥íÑ½±•5½Ù¥•…Ù½É¥Ñ”¡í…Ñ…±½}¥éµÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ñ…±½œœ¤±ÍÑÉ•…µ}¥éµÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤±¹…µ”éµÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¹…µ”œ¤±•áÑ•¹Í¥½¸éµÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ•áÐœ¤±å•…ÈéµÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µå•…Èœ¤±É…Ñ¥¹œéµÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÉ…Ñ¥¹œœ¤±½Ù•ÈéµÌ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ½Ù•Èœ¥ô±µÌ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÉ••¹Ñ5½Ù¥”õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹É••¹Ñµ½Ù¥”œ¤ì(€¥˜¡É••¹Ñ5½Ù¥”¥í‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•Dœ¤¹Ù…±Õ”õÉ••¹Ñ5½Ù¥”¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÅÕ•Éäœ¥ñðœœíÍ•…É¡5½Ù¥•Ì ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐµÈõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹µ½Ù¥•É•µ½Ù”œ¤ì(€¥˜¡µÈ¥íÉ•µ½Ù•5½Ù¥•…Ù½É¥Ñ”¡µÈ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ­•äœ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ™…Ù½É¥Ñ•5½Ù¥”õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹µ½Ù¥•™…Øœ¤ì(€¥˜¡™…Ù½É¥Ñ•5½Ù¥”¥í‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% µ½Ù¥•Dœ¤¹Ù…±Õ”õ™…Ù½É¥Ñ•5½Ù¥”¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÅÕ•Éäœ¥ñðœœíÍ•…É¡5½Ù¥•Ì ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑÐõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹Ñ•…µÑ…ˆœ¤ì(€¥˜¡ÑÐ¥í}…Ñ¥Ù•Q•…´õÁ…ÉÍ•%¹Ð¡ÑÐ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÑ•…´œ¤°ÄÀ¥ñðÀíÉ•¹‘•ÉQ•…µMÝ¥Ñ  ¤íÉ•¹‘•ÉÑ¥Ù•Q•…´ ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐ‰ õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹‰¡•…œ¤ì(€¥˜¡‰ ¥ì(€€€½¹ÍÐÉ½Üõ‰ ¹Á…É•¹Ñ±•µ•¹Ðì(€€€½¹ÍÐ‰½àõÉ½Ü¹ÅÕ•ÉåM•±•Ñ½È œ¹‰¡…¹Ìœ¤ì(€€€½¹ÍÐ½Á•¹¥¹œõ‰½à˜™‰½à¹±…ÍÍ1¥ÍÐ¹½¹Ñ…¥¹Ì ¡¥‘”œ¤ì(€€€½¹ÍÐÍ½Á”õÉ½Ü¹Á…É•¹Ñ±•µ•¹Ðí¥˜¡Í½Á”¥Í½Á”¹ÅÕ•ÉåM•±•Ñ½É±° œéÍ½Á”€ø€¹‰É½Ü¹½Á•¸œ¤¹™½É… ¡½Ñ¡•Èôùí¥˜¡½Ñ¡•È„ôõÉ½Ü¥í½Ñ¡•È¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ½Á•¸œ¤í½¹ÍÐ½Ñ¡•É	½àõ½Ñ¡•È¹ÅÕ•ÉåM•±•Ñ½È œ¹‰¡…¹Ìœ¤í¥˜¡½Ñ¡•É	½à¥½Ñ¡•É	½à¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íõô¤ì(€€€¥˜¡‰½à¥‰½à¹±…ÍÍ1¥ÍÐ¹Ñ½±” ¡¥‘”œ°…½Á•¹¥¹œ¤ì(€€€É½Ü¹±…ÍÍ1¥ÍÐ¹Ñ½±” ½Á•¸œ°„…½Á•¹¥¹œ¤ì(€€€É•ÑÕÉ¸ì(€ô(€½¹ÍÐÍÉŒõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹ÑÙÍÉŒœ¤ì(€¥˜¡ÍÉŒ¥í±½…‘QÙM½ÕÉ”¡ÍÉŒ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍÉŒœ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÑÙØõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹ÑÙÙ±Œœ¤ì(€¥˜¡ÑÙØ¥íÁ±…åY1¡ÑÙØ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤±ÑÙØ¤íÉ•ÑÕÉ¸íô(€¥˜¡”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹ÑÙ‘É…œœ¤¥É•ÑÕÉ¸ì(€½¹ÍÐÍÐõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹™…ÙÍÑ…Èœ¤ì(€¥˜¡ÍÐ¥ì(€€€¥˜¡ÍÐ¹¡…ÍÑÑÉ¥‰ÕÑ” ‘…Ñ„µ™…Ù…Ðœ¤¥íÑ½±•…Ù…Ð¡ÍÐ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ™…Ù…Ðœ¤±ÍÐ¤íÉ•ÑÕÉ¸íô(€€€Ñ½±•…Ù¡…¹¹•°¡ÍÐ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤±ÍÐ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¹…µ”œ¤±ÍÐ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ðœ¤±ÍÐ¤íÉ•ÑÕÉ¸ì(€ô(€½¹ÍÐÑÙŒõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹ÑÙ¡…¸œ¤ì(€¥˜¡ÑÙŒ¥í½¹ÍÐŒõ}ÑÙ¡…¹¹•±Ì¹™¥¹¡™Õ¹Ñ¥½¸¡à¥íÉ•ÑÕÉ¸MÑÉ¥¹œ¡à¹ÍÑÉ•…µ}¥¤ôôõÑÙŒ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤íô¤í¥˜¡Œ¥ÑÙA±…ä¡Œ¹ÍÑÉ•…µ}¥±Œ¹¹…µ”¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÉ´õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹™…ÙÉ´œ¤ì(€¥˜¡É´¥í¥˜¡É´¹¡…ÍÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ðœ¤¥É•µ½Ù•…Ù…Ð¡É´¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ…Ðœ¤¤í•±Í”É•µ½Ù•…Ù¡…¸¡É´¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÁˆõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹‰Ñ¹Á±…äœ¤ì(€¥˜¡Áˆ¥íÁ±…å	É½ÝÍ•È¡Áˆ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤±Áˆ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¹…µ”œ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐÙˆõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹‰Ñ¹Ù±Œœ¤ì(€¥˜¡Ùˆ¥íÁ±…åY1¡Ùˆ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤±Ùˆ¤íÉ•ÑÕÉ¸íô(€½¹ÍÐµå õ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹µå‘…Í¡¡…¹¹•±m‘…Ñ„µÍ¥‘tœ¤ì(€¥˜¡µå ¥íÁ±…å	É½ÝÍ•È¡µå ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤±µå ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µ¹…µ”œ¤¤íÉ•ÑÕÉ¸íô(€½¹ÍÐˆõ”¹Ñ…É•Ð¹±½Í•ÍÐ œ¹½Áäœ¤ì(€¥˜ …ˆ¥É•ÑÕÉ¸ì(€½¹ÍÐÔõˆ¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÕÉ°œ¥ñðœœì(€¹…Ù¥…Ñ½È¹±¥Á‰½…É¹ÝÉ¥Ñ•Q•áÐ¡Ô¤¹Ñ¡•¸  ¤ôùíˆ¹Ñ•áÑ½¹Ñ•¹Ðô½Á¥•œíÍ•ÑQ¥µ•½ÕÐ  ¤ôùˆ¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ½ÁäUI0œ¤°ÄÈÀÀ¤íô¤(€€€€¹…Ñ   ¤ôùíˆ¹Ñ•áÑ½¹Ñ•¹Ðô½Áä™…¥±•œíÍ•ÑQ¥µ•½ÕÐ  ¤ôùˆ¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ½ÁäUI0œ¤°ÄÔÀÀ¤íô¤ì)ô¤ì(¼¼…ÁÁ±äÍ…Ù•±…¹Õ…”)ÑÉåí½¹ÍÐÍ°õ±½…±MÑ½É…”¹•Ñ%Ñ•´ ÑÙµ…Ñ•}±…¹œœ¤í¥˜¡Í°ôôô¹¼œ¥Í•Ñ1…¹œ ¹¼œ¤í•±Í”…ÁÁ±å1…¹œ ¤íõ…Ñ ¡”¥í…ÁÁ±å1…¹œ ¤íô(¼¼½Á•¸Ñ¡”ÕÍ•ÈÌ‘•™…Õ±ÐÍÑ…ÉÐÍ•Ñ¥½¸(¡…Íå¹Œ™Õ¹Ñ¥½¸ ¥ì(€±•ÐÍÑ…ÉÐôµå±¥ÍÐœ±¡•­M¡½ÝÌõ™…±Í”±É•™É•Í¡%ÁÑØõ™…±Í”±É•™É•Í¡MÁ½ÉÑÌõ™…±Í”±ÍÑ…ÉÑÕÁ½¹™¥œõ¹Õ±°ì(€ÑÉåí½¹ÍÐŒõ…Ý…¥Ð…Á¤ œ½…Á¤½½¹™¥œœ¤íÍÑ…ÉÑÕÁ½¹™¥œõŒíÍÑ…ÉÐõŒ¹ÍÑ…ÉÑ}Í•Ñ¥½¹ñðµå±¥ÍÐœí¡•­M¡½ÝÌô„…Œ¹¡•­}Í¡½ÝÍ}½¹}ÍÑ…ÉÑÕÀíÉ•™É•Í¡%ÁÑØô„…Œ¹É•™É•Í¡}¥ÁÑÙ}½¹}ÍÑ…ÉÑÕÀíÉ•™É•Í¡MÁ½ÉÑÌô„…Œ¹É•™É•Í¡}ÍÁ½ÉÑÍ}½¹}ÍÑ…ÉÑÕÀíÍ•Ñ1…¹œ¡Œ¹ÁÉ•™•ÉÉ•‘}±…¹Õ…•ñð•¸œ¤í…ÁÁ±åAÉ½™¥±•½¹™¥œ¡Œ¤í¥˜¡ÍÑ…ÉÐôôôÑ•…µÌœ˜˜…}™½½Ñ‰…±±¹…‰±•¥ÍÑ…ÉÐôµå±¥ÍÐœí¥˜¡ÍÑ…ÉÐôôô…µ•Ìœ˜˜…}…µ•Í¹…‰±•¥ÍÑ…ÉÐôµå±¥ÍÐœí¥˜¡ÍÑ…ÉÐôôôÉ…¥¹œœ˜˜…}˜Å¹…‰±•¥ÍÑ…ÉÐôµå±¥ÍÐœíõ…Ñ ¡”¥íô(€¥˜¡ÍÑ…ÉÐôôôÍ•…É œ¥ÍÑ…ÉÐô¡…¹¹•±Ìœì€¼¼µ¥É…Ñ”Ñ¡”É•µ½Ù•M•…É Í•Ñ¥½¸(€¥˜¡ÍÑ…ÉÐôôôµåÑ¥µ•±¥¹”œ˜™}µå1¥ÍÑ1…å½ÕÐôôôÑ¥µ•±¥¹”œ¥ÍÑ…ÉÐôµå±¥ÍÐœì(€½¹ÍÐµ…Àõí¡…¹¹•±ÌéÍ¡½Ý¡…¹¹•±Ì±µåÑØéÍ¡½Ý5åÑØ±µ½Ù¥•ÌéÍ¡½Ý5½Ù¥•Ì±Í¡½ÝÌéÍ¡½ÝM¡½ÝÌ±…µ•ÌéÍ¡½Ý…µ•Ì±É…¥¹œéÍ¡½ÝI…¥¹œ±Ñ•…µÌéÍ¡½ÝQ•…µÌ±µå±¥ÍÐéÍ¡½Ý5å±¥ÍÐ±µåÑ¥µ•±¥¹”éÍ¡½Ý5åÑ¥µ•±¥¹•ôì(€€¡µ…ÁmÍÑ…ÉÑuññÍ¡½Ý5å±¥ÍÐ¤ ¤ì(€¡¥ÍÑ½Éä¹É•Á±…•MÑ…Ñ”¡íÑÙµ…Ñ”éÑÉÕ”±Í•Ñ¥½¸éÍÑ…ÉÑô°œœ°œŒœ­ÍÑ…ÉÐ¤ì(€}¡¥ÍÑ½ÉåI•…‘äõÑÉÕ”ì(€½¹ÍÐÍ•ÑÕÁ½¹”ô„„¡ÍÑ…ÉÑÕÁ½¹™¥œ˜™ÍÑ…ÉÑÕÁ½¹™¥œ¹Í•ÑÕÁ}½µÁ±•Ñ”ôôõÑÉÕ”¤ì(€¥˜¡ÍÑ…ÉÑÕÁ½¹™¥œ˜˜…Í•ÑÕÁ½¹”¥Í•ÑQ¥µ•½ÕÐ  ¤ôù½Á•¹AÉ½™¥±•M•ÑÕÀ¡ÑÉÕ”±ÍÑ…ÉÑÕÁ½¹™¥œ¤°ÄÈÀ¤ì(€¥˜¡ÍÑ…ÉÑÕÁ½¹™¥œ˜™Í•ÑÕÁ½¹”¥Í•ÑQ¥µ•½ÕÐ  ¤ôùµ…å‰•ÕÑ½I•™É•Í¡MÑ•…µ]¥Í¡±¥ÍÐ¡ÍÑ…ÉÑÕÁ½¹™¥œ¤°äÀÀ¤ì(€¥˜¡Í•ÑÕÁ½¹”˜˜¡É•™É•Í¡%ÁÑÙññÉ•™É•Í¡MÁ½ÉÑÍññ¡•­M¡½ÝÌ¤¥Í•ÑQ¥µ•½ÕÐ¡…Íå¹Œ™Õ¹Ñ¥½¸ ¥ì(€€€¥˜¡É•™É•Í¡%ÁÑÙññÉ•™É•Í¡MÁ½ÉÑÌ¥…Ý…¥ÐÉ•™É•Í¡=¹MÑ…ÉÑÕÀ¡É•™É•Í¡%ÁÑØ±É•™É•Í¡MÁ½ÉÑÌ¤ì(€€€¥˜¡¡•­M¡½ÝÌ˜˜…É•™É•Í¡%ÁÑØ¥…Ý…¥Ð¡•­M¡½ÝÍ=¹MÑ…ÉÑÕÀ ¤ì(€ô°ÔÀÀ¤ì)ô¤ ¤ì)Ý¥¹‘½Ü¹…‘‘Ù•¹Ñ1¥ÍÑ•¹•È Á½ÁÍÑ…Ñ”œ±™Õ¹Ñ¥½¸¡•Ø¥ì(€½¹ÍÐÍÑ…Ñ”õ•Ø¹ÍÑ…Ñ”ì(€¥˜ …ÍÑ…Ñ•ñð…ÍÑ…Ñ”¹ÑÙµ…Ñ”¥É•ÑÕÉ¸ì(€½¹ÍÐµ…ÀõíÍ•…É éÍ¡½Ý¡…¹¹•±Ì±¡…¹¹•±ÌéÍ¡½Ý¡…¹¹•±Ì±µåÑØéÍ¡½Ý5åÑØ±µ½Ù¥•ÌéÍ¡½Ý5½Ù¥•Ì±Í¡½ÝÌéÍ¡½ÝM¡½ÝÌ±…µ•ÌéÍ¡½Ý…µ•Ì±É…¥¹œéÍ¡½ÝI…¥¹œ±Ñ•…µÌéÍ¡½ÝQ•…µÌ±µå±¥ÍÐéÍ¡½Ý5å±¥ÍÐ±µåÑ¥µ•±¥¹”éÍ¡½Ý5åÑ¥µ•±¥¹”±Í•ÑÑ¥¹ÌéÍ¡½ÝM•ÑÑ¥¹Íôì(€½¹ÍÐ™¸õµ…ÁmÍÑ…Ñ”¹Í•Ñ¥½¹uññÍ¡½Ý5å±¥ÍÐì(€}¡¥ÍÑ½ÉåI•ÍÑ½É¥¹œõÑÉÕ”ì(€ÑÉåì(€€€™¸ ¤ì(€€€¥˜¡ÍÑ…Ñ”¹Í•Ñ¥½¸ôôôÍ¡½ÝÌœ˜™ÍÑ…Ñ”¹Í•É¥•Í%¥±½…‘M¡½Ü¡ÍÑ…Ñ”¹Í•É¥•Í%±ÑÉÕ”¤ì(€€€•±Í”¥˜¡ÍÑ…Ñ”¹Í•Ñ¥½¸ôôôÍ¡½ÝÌœ˜™ÍÑ…Ñ”¹…Ñ…±½%¥±½…‘áÑ•É¹…±M¡½Ü¡ÍÑ…Ñ”¹…Ñ…±½%±ÑÉÕ”¤ì(€õ™¥¹…±±åí}¡¥ÍÑ½ÉåI•ÍÑ½É¥¹œõ™…±Í”íô)ô¤ì)É•™É•Í¡MÑ…ÑÕÌ ¤ì(¼¼€´´´…ÕÑ¼µÕÁ‘…Ñ”€´´´)±•Ð}ÕÁ‘…Ñ•1…Ñ•ÍÐõ¹Õ±°ì)…Íå¹Œ™Õ¹Ñ¥½¸½Á•¹½¹™¥½±‘•È ¥ì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½½Á•¹}™½±‘•Èœ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜ …¨¹½¬¥Ñ½…ÍÐ¡ÑÈ ½Õ±¹½Ð½Á•¸™½±‘•È¸œ¤¬¡¨¹Á…Ñ ü œ€œ­¨¹Á…Ñ ¤èœœ¤¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ¡ÑÈ ½Õ±¹½Ð½Á•¸™½±‘•È¸œ¤¤íô)ô)™Õ¹Ñ¥½¸ÁÉ½™¥±•Q¥µ•±¥¹•	…­ÕÀ ¥ì(€±•ÐÍ•ÑÑ¥¹ÌõíôíÑÉåíÍ•ÑÑ¥¹Ìõ)M=8¹Á…ÉÍ”¡±½…±MÑ½É…”¹•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•M•ÑÑ¥¹Ìœ¥ñðíôœ¥ññíôíõ…Ñ ¡”¥íô(€É•ÑÕÉ¸í™¥±Ñ•Èé±½…±MÑ½É…”¹•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•¥±Ñ•Èœ¥ñð…±°œ±Í•ÑÑ¥¹ÌéÍ•ÑÑ¥¹Íôì)ô)…Íå¹Œ™Õ¹Ñ¥½¸•áÁ½ÉÑAÉ½™¥±•	…­ÕÀ¡™Õ±°¥ì(€¥˜¡™Õ±°˜˜…½¹™¥É´ Õ±°‰…­ÕÀ¥¹±Õ‘•Ìå½ÕÈaÑÉ•…´±½¥¸¸½Ý¹±½……¹ÍÑ½É”¥ÐÍ•ÕÉ•±äüœ¤¥É•ÑÕÉ¸ì(€½¹ÍÐµÍœõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÁÉ½™¥±•	…­ÕÁ5Íœœ¤í¥˜¡µÍœ¥µÍœ¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ AÉ•Á…É¥¹œ‰…­ÕÀ¸¸¸œ¤ì(€ÑÉåì(€€€½¹ÍÐ‰…­ÕÀõ…Ý…¥Ð…Á¤ œ½…Á¤½ÁÉ½™¥±•}‰…­ÕÁ}•áÁ½ÉÐœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡íÑåÁ”é™Õ±°ü™Õ±°œèÁÉ½™¥±”œ±Ñ¥µ•±¥¹”éÁÉ½™¥±•Q¥µ•±¥¹•	…­ÕÀ ¥ô¥ô¤ì(€€€¥˜¡‰…­ÕÀ¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡‰…­ÕÀ¹•ÉÉ½È¤ì(€€€½¹ÍÐÍ…™”õMÑÉ¥¹œ ¡‰…­ÕÀ¹½¹™¥œ˜™‰…­ÕÀ¹½¹™¥œ¹ÁÉ½™¥±•}¹…µ”¥ñðÁÉ½™¥±”œ¤¹É•Á±…” ½my„µèÀ´å|µt¬½¤°œ´œ¤¹É•Á±…” ½x´­ð´¬½œ°œœ¥ñðÁÉ½™¥±”œì(€€€½¹ÍÐ‰±½ˆõ¹•Ü	±½ˆ¡m)M=8¹ÍÑÉ¥¹¥™ä¡‰…­ÕÀ±¹Õ±°°È¥t±íÑåÁ”è…ÁÁ±¥…Ñ¥½¸½©Í½¸ô¤±±¥¹¬õ‘½Õµ•¹Ð¹É•…Ñ•±•µ•¹Ð „œ¤ì(€€€±¥¹¬¹¡É•˜õUI0¹É•…Ñ•=‰©•ÑUI0¡‰±½ˆ¤í±¥¹¬¹‘½Ý¹±½…ôQY5…Ñ”´œ­Í…™”¬œ´œ¬¡™Õ±°ü™Õ±°œèÁÉ½™¥±”œ¤¬œµ‰…­ÕÀ¹©Í½¸œí‘½Õµ•¹Ð¹‰½‘ä¹…ÁÁ•¹‘¡¥±¡±¥¹¬¤í±¥¹¬¹±¥¬ ¤í±¥¹¬¹É•µ½Ù” ¤íÍ•ÑQ¥µ•½ÕÐ  ¤ôùUI0¹É•Ù½­•=‰©•ÑUI0¡±¥¹¬¹¡É•˜¤°ÄÀÀÀ¤ì(€€€¥˜¡µÍœ¥µÍœ¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ 	…­ÕÀ‘½Ý¹±½…‘•¸œ¤ì(€õ…Ñ ¡”¥í¥˜¡µÍœ¥µÍœ¹Ñ•áÑ½¹Ñ•¹ÐõMÑÉ¥¹œ¡”¹µ•ÍÍ…•ññ”¤íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸¥µÁ½ÉÑAÉ½™¥±•	…­ÕÀ¡¥¹ÁÕÐ¥ì(€½¹ÍÐµÍœõ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÁÉ½™¥±•	…­ÕÁ5Íœœ¤±™¥±”õ¥¹ÁÕÐ¹™¥±•Ì˜™¥¹ÁÕÐ¹™¥±•ÍlÁtí¥˜ …™¥±”¥É•ÑÕÉ¸ì(€ÑÉåì(€€€¥˜¡™¥±”¹Í¥é”øÔ¨ÄÀÈÐ¨ÄÀÈÐ¥Ñ¡É½Ü¹•ÜÉÉ½È 	…­ÕÀ™¥±”¥ÌÑ½¼±…É”¸œ¤ì(€€€½¹ÍÐ‰…­ÕÀõ)M=8¹Á…ÉÍ”¡…Ý…¥Ð™¥±”¹Ñ•áÐ ¤¤ì(€€€¥˜¡‰…­ÕÀ¹™½Éµ…Ð„ôô½±½ÌµÑÙµ…Ñ”µ‰…­ÕÀœ¥Ñ¡É½Ü¹•ÜÉÉ½È Q¡¥Ì¥Ì¹½Ð„QY5…Ñ”‰…­ÕÀ™¥±”¸œ¤ì(€€€½¹ÍÐ½Õ¹ÑÌõ‰…­ÕÀ¹™…Ù½É¥Ñ•Íññíô±ÍÕµµ…ÉäõlÍ¡½ÝÌœ°µ½Ù¥•Ìœ°…µ•Ìœ°Ñ•…µÌœ°¡…¹¹•±Ìt¹µ…À¡¬ôø¡ÉÉ…ä¹¥ÍÉÉ…ä¡½Õ¹ÑÍm­t¤ý½Õ¹ÑÍm­t¹±•¹Ñ èÀ¤¬œ€œ­¬¤¹©½¥¸ œ°€œ¤ì(€€€½¹ÍÐ™Õ±°õ‰…­ÕÀ¹‰…­ÕÁ}ÑåÁ”ôôô™Õ±°œì(€€€½¹ÍÐÝ…É¹¥¹œô¡™Õ±°üQ¡¥Ì™Õ±°‰…­ÕÀ…¸É•Á±…”Ñ¡”ÕÉÉ•¹ÐaÑÉ•…´±½¥¸¹qq¹qq¸œèœœ¤¬5•É”‰…­ÕÀ¥¹Ñ¼Ñ¡¥ÌÁÉ½™¥±”ýqq¸œ­ÍÕµµ…Éäì(€€€¥˜ …½¹™¥É´¡Ý…É¹¥¹œ¤¥É•ÑÕÉ¸ì(€€€½¹ÍÐÉ•ÍÕ±Ðõ…Ý…¥Ð…Á¤ œ½…Á¤½ÁÉ½™¥±•}‰…­ÕÁ}¥µÁ½ÉÐœ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡í‰…­ÕÀé‰…­ÕÁô¥ô¤ì(€€€¥˜¡É•ÍÕ±Ð¹•ÉÉ½È¥Ñ¡É½Ü¹•ÜÉÉ½È¡É•ÍÕ±Ð¹•ÉÉ½È¤ì(€€€½¹ÍÐÑ¥µ•±¥¹”õÉ•ÍÕ±Ð¹Ñ¥µ•±¥¹•ññíôì(€€€¥˜¡É•ÍÕ±Ð¹ÑåÁ”ôôô™Õ±°œ¥ì(€€€€€¥˜¡=‰©•Ð¹ÁÉ½Ñ½ÑåÁ”¹¡…Í=Ý¹AÉ½Á•ÉÑä¹…±°¡Ñ¥µ•±¥¹”°™¥±Ñ•Èœ¤¥±½…±MÑ½É…”¹Í•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•¥±Ñ•Èœ±MÑÉ¥¹œ¡Ñ¥µ•±¥¹”¹™¥±Ñ•Éñð…±°œ¤¤í•±Í”±½…±MÑ½É…”¹É•µ½Ù•%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•¥±Ñ•Èœ¤ì(€€€€€¥˜¡=‰©•Ð¹ÁÉ½Ñ½ÑåÁ”¹¡…Í=Ý¹AÉ½Á•ÉÑä¹…±°¡Ñ¥µ•±¥¹”°Í•ÑÑ¥¹Ìœ¤¥±½…±MÑ½É…”¹Í•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•M•ÑÑ¥¹Ìœ±)M=8¹ÍÑÉ¥¹¥™ä¡Ñ¥µ•±¥¹”¹Í•ÑÑ¥¹Íññíô¤¤í•±Í”±½…±MÑ½É…”¹É•µ½Ù•%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•M•ÑÑ¥¹Ìœ¤ì(€€€€€¥˜¡µÍœ¥µÍœ¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ Õ±°‰…­ÕÀÉ•ÍÑ½É•¸œ¤íÑ½…ÍÐ¡ÑÈ Õ±°‰…­ÕÀÉ•ÍÑ½É•¸œ¤¤í±½…Ñ¥½¸¹É•±½… ¤íÉ•ÑÕÉ¸ì(€€€ô(€€€¥˜¡Ñ¥µ•±¥¹”¹™¥±Ñ•È¥±½…±MÑ½É…”¹Í•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•¥±Ñ•Èœ±Ñ¥µ•±¥¹”¹™¥±Ñ•È¤í¥˜¡Ñ¥µ•±¥¹”¹Í•ÑÑ¥¹Ì¥±½…±MÑ½É…”¹Í•Ñ%Ñ•´ ÑÙµ…Ñ•Q¥µ•±¥¹•M•ÑÑ¥¹Ìœ±)M=8¹ÍÑÉ¥¹¥™ä¡Ñ¥µ•±¥¹”¹Í•ÑÑ¥¹Ì¤¤ì(€€€}µåQ¥µ•±¥¹•AÉ•™Í1½…‘•õ™…±Í”í}…ÑÍ1½…‘•õ™…±Í”í}±…Ñ•ÍÑÁ¥Í½‘•Í1½…‘•õ™…±Í”í}µå1¥ÍÑ1½…‘•õ™…±Í”í…Ý…¥Ð±½…‘M•ÑÑ¥¹Ì ¤í…Ý…¥Ð±½…‘…Ù½É¥Ñ•Ì ¤íÉ•™É•Í¡MÑ…ÑÕÌ ¤ì(€€€¥˜¡µÍœ¥µÍœ¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ 	…­ÕÀ¥µÁ½ÉÑ•…¹µ•É•¸œ¤íÑ½…ÍÐ¡ÑÈ 	…­ÕÀ¥µÁ½ÉÑ•…¹µ•É•¸œ¤¤ì(€õ…Ñ ¡”¥í¥˜¡µÍœ¥µÍœ¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ½Õ±¹½Ð¥µÁ½ÉÐÑ¡¥Ì‰…­ÕÀ¸œ¤¬œ€œ­MÑÉ¥¹œ¡”¹µ•ÍÍ…•ññ”¤íô(€™¥¹…±±åí¥¹ÁÕÐ¹Ù…±Õ”ôœœíô)ô)™Õ¹Ñ¥½¸}¡•…±Ñ¡¼¡ÑÌ±¹½Ü¥ì(€¥˜ …ÑÌ¥É•ÑÕÉ¸ÑÈ ¹½Ð¡•­•å•Ðœ¤ì(€½¹ÍÐÌõ5…Ñ ¹µ…à À±5…Ñ ¹™±½½È ¡¹½ÜµÑÌ¤¤¤ì(€¥˜¡ÌðäÀ¥É•ÑÕÉ¸ÑÈ ©ÕÍÐ¹½Üœ¤ì(€½¹ÍÐ´õ5…Ñ ¹™±½½È¡Ì¼ØÀ¤ì(€¥˜¡´ðäÀ¥É•ÑÕÉ¸´¬œ€œ­ÑÈ µ¥¸…¼œ¤ì(€½¹ÍÐ õ5…Ñ ¹™±½½È¡´¼ØÀ¤ì(€¥˜¡ ðÐà¥É•ÑÕÉ¸ ¬œ€œ­ÑÈ  …¼œ¤ì(€É•ÑÕÉ¸5…Ñ ¹™±½½È¡ ¼ÈÐ¤¬œ€œ­ÑÈ …¼œ¤ì)ô)™Õ¹Ñ¥½¸É•¹‘•ÉM½ÕÉ•!•…±Ñ ¡‘…Ñ„¥ì(€½¹ÍÐ•°õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% Í½ÕÉ•!•…±Ñ œ¤ì(€¥˜ …•°¥É•ÑÕÉ¸ì(€½¹ÍÐ¹½Üõ‘…Ñ„¹¹½Ýñð¡…Ñ”¹¹½Ü ¤¼ÄÀÀÀ¤ì(€±•Ð ôœœì(€€¡‘…Ñ„¹Í½ÕÉ•Íññmt¤¹™½É… ¡™Õ¹Ñ¥½¸¡Ì¥ì(€€€±•Ð‘½Ðô‘½ÐµÕ¹­¹½Ý¸œ±±…‰•°õÑÈ ¹½Ð¡•­•å•Ðœ¤ì(€€€½¹ÍÐÍÁ••õÌ¹±…Ñ•¹å}µÌ„õ¹Õ±°ü œqÔÀÁˆÜ€œ¬¡Ì¹±…Ñ•¹å}µÌøôÄÀÀÀü¡Ì¹±…Ñ•¹å}µÌ¼ÄÀÀÀ¤¹Ñ½¥á• Ä¤¬ÌœéÌ¹±…Ñ•¹å}µÌ¬µÌœ¤¤èœœì(€€€¥˜¡Ì¹½¬ôôõÑÉÕ”¥í‘½Ðô‘½Ðµ½¬œí±…‰•°õÑÈ Ý½É­¥¹œœ¤¬¡Ì¹½Õ¹Ð„õ¹Õ±°ü œqÔÀÁˆÜ€œ­Ì¹½Õ¹Ð¬œ€œ­ÑÈ ¥Ñ•µÌœ¤¤èœœ¤­ÍÁ••¬œqÔÀÁˆÜ€œ­}¡•…±Ñ¡¼¡Ì¹ÑÌ±¹½Ü¤íô(€€€•±Í”¥˜¡Ì¹½¬ôôõ™…±Í”¥í‘½Ðô‘½Ðµ‰…œí±…‰•°ô¡Ì¹•ÉÉ½ÈýÌ¹•ÉÉ½ÈéÑÈ ™…¥±•œ¤¤­ÍÁ••¬œqÔÀÁˆÜ€œ­}¡•…±Ñ¡¼¡Ì¹ÑÌ±¹½Ü¤íô(€€€•±Í”¥˜¡Ì¹•ÉÉ½È¥í±…‰•°õÑÈ¡Ì¹•ÉÉ½È¤íô(€€€ ¬ôœñ‘¥Ø±…ÍÌô‰ÍÉÉ½ÜˆøñÍÁ…¸±…ÍÌô‰ÍÉ‘½Ð€œ­‘½Ð¬œˆøð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰ÍÉ¹…µ”ˆøœ­•ÍŒ¡Ì¹±…‰•°¤¬œð½ÍÁ…¸øñÍÁ…¸±…ÍÌô‰ÍÉÍÑ…ÐµÕÑ•ˆøœ­•ÍŒ¡±…‰•°¤¬œð½ÍÁ…¸øð½‘¥Øøœì(€ô¤ì(€•°¹¥¹¹•É!Q50õ¡ñð œñÍÁ…¸±…ÍÌô‰µÕÑ•ˆøœ­ÑÈ 9¼Í½ÕÉ•Ì¸œ¤¬œð½ÍÁ…¸øœ¤ì)ô)…Íå¹Œ™Õ¹Ñ¥½¸±½…‘M½ÕÉ•!•…±Ñ  ¥ì(€ÑÉåí½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½Í½ÕÉ•}¡•…±Ñ œ¤íÉ•¹‘•ÉM½ÕÉ•!•…±Ñ ¡¨¤íõ…Ñ ¡”¥íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸Ñ•ÍÑM½ÕÉ•Ì¡‰Ñ¸¥ì(€¥˜¡‰Ñ¸¥í‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ Q•ÍÑ¥¹œÍ½ÕÉ•Ì¸¸¸œ¤íô(€ÑÉåì(€€€½¹ÍÐ™¥ÉÍÐõ…Ý…¥Ð…Á¤ œ½…Á¤½Í½ÕÉ•}¡•…±Ñ œ¤±­•åÌô¡™¥ÉÍÐ¹Í½ÕÉ•Íññmt¤¹µ…À¡ÌôùÌ¹­•ä¤±Ñ½Ñ…°õ­•åÌ¹±•¹Ñ í±•Ð¹•áÐôÀ±‘½¹”ôÀì(€€€½¹ÍÐÝ½É­•Èõ…Íå¹Œ™Õ¹Ñ¥½¸ ¥íÝ¡¥±”¡¹•áÐñÑ½Ñ…°¥í½¹ÍÐ­•äõ­•åÍm¹•áÐ¬­tíÑÉåí½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½Ñ•ÍÑ}Í½ÕÉ”œ±íµ•Ñ¡½èA=MPœ±¡•…‘•ÉÌéì½¹Ñ•¹ÐµQåÁ”œè…ÁÁ±¥…Ñ¥½¸½©Í½¸ô±‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡í­•äé­•åô¥ô¤í¥˜¡¨˜™¨¹Í½ÕÉ•Ì¥É•¹‘•ÉM½ÕÉ•!•…±Ñ ¡íÍ½ÕÉ•Ìé¨¹Í½ÕÉ•Ì±¹½Üé…Ñ”¹¹½Ü ¤¼ÄÀÀÁô¤íõ…Ñ ¡”¥íõ‘½¹”¬¬í¥˜¡‰Ñ¸¥‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ Q•ÍÑ¥¹œÍ½ÕÉ•Ì¸¸¸œ¤¬œ€œ­‘½¹”¬œ¼œ­Ñ½Ñ…°íõôì(€€€…Ý…¥ÐAÉ½µ¥Í”¹…±°¡mÝ½É­•È ¤±Ý½É­•È ¤±Ý½É­•È ¥t¤ì(€€€…Ý…¥Ð±½…‘M½ÕÉ•!•…±Ñ  ¤ì(€õ…Ñ ¡”¥íÑ½…ÍÐ¡ÑÈ ½Õ±¹½ÐÑ•ÍÐÍ½ÕÉ•Ì¸œ¤¤íô(€¥˜¡‰Ñ¸¥í‰Ñ¸¹‘¥Í…‰±•õ™…±Í”í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ Q•ÍÐ…±°Í½ÕÉ•Ìœ¤íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸¡•­½ÉUÁ‘…Ñ”¡µ…¹Õ…°¥ì(€½¹ÍÐ‰Ñ¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ¡•­UÁ‘…Ñ•	Ñ¸œ¤ì(€¥˜¡µ…¹Õ…°˜™‰Ñ¸¥í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ¡•­¥¹œ¸¸¸œ¤í‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”íô(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½ÕÁ‘…Ñ•}¡•¬œ¤ì(€€€¥˜¡¨¹…Ù…¥±…‰±”˜™¨¹±…Ñ•ÍÐ¥ì(€€€€€}ÕÁ‘…Ñ•1…Ñ•ÍÐõ¨¹±…Ñ•ÍÐì(€€€€€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•5Íœœ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ UÁ‘…Ñ”…Ù…¥±…‰±”œ¤¬œèØœ­¨¹±…Ñ•ÍÐ¬œ€ œ­ÑÈ å½Ô¡…Ù”œ¤¬œØœ­¨¹ÕÉÉ•¹Ð¬œ¤œì(€€€€€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•	…¹¹•Èœ¤¹±…ÍÍ1¥ÍÐ¹É•µ½Ù” ¡¥‘”œ¤ì(€€€õ•±Í”¥˜¡µ…¹Õ…°¥ì(€€€€€Ñ½…ÍÐ¡ÑÈ e½Ô…É”½¸Ñ¡”±…Ñ•ÍÐÙ•ÉÍ¥½¸œ¤¬œ€¡Øœ¬¡¨¹ÕÉÉ•¹Ññðœœ¤¬œ¤œ¤ì(€€€ô(€õ…Ñ ¡”¥ì(€€€¥˜¡µ…¹Õ…°¥Ñ½…ÍÐ¡ÑÈ ½Õ±¹½Ð¡•¬™½ÈÕÁ‘…Ñ•Ì¸¡•¬å½ÕÈ¥¹Ñ•É¹•Ð½¹¹•Ñ¥½¸¸œ¤¤ì(€ô(€¥˜¡µ…¹Õ…°˜™‰Ñ¸¥í‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ¡•¬™½ÈÕÁ‘…Ñ•Ìœ¤í‰Ñ¸¹‘¥Í…‰±•õ™…±Í”íô)ô)…Íå¹Œ™Õ¹Ñ¥½¸‘½UÁ‘…Ñ•9½Ü ¥ì(€½¹ÍÐ‰Ñ¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•9½Ý	Ñ¸œ¤ì(€‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ ½Ý¹±½…‘¥¹œ¸¸¸œ¤í‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”ì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½ÕÁ‘…Ñ•}‘½Ý¹±½…œ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜ …¨¹½¬¥íÑ¡É½Ü¹•ÜÉÉ½È ‘°œ¤íô(€€€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•5Íœœ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ UÁ‘…Ñ”‘½Ý¹±½…‘•¸I•ÍÑ…ÉÐ¹½ÜÑ¼™¥¹¥Í ÕÁ‘…Ñ¥¹œüœ¤ì(€€€‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ I•ÍÑ…ÉÐ¹½Üœ¤í‰Ñ¸¹‘¥Í…‰±•õ™…±Í”ì(€€€‰Ñ¸¹½¹±¥¬õ‘½UÁ‘…Ñ•I•ÍÑ…ÉÐì(€õ…Ñ ¡”¥ì(€€€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•5Íœœ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ UÁ‘…Ñ”™…¥±•¸QÉä……¥¸±…Ñ•È¸œ¤ì(€€€‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ UÁ‘…Ñ”¹½Üœ¤í‰Ñ¸¹‘¥Í…‰±•õ™…±Í”ì(€ô)ô)…Íå¹Œ™Õ¹Ñ¥½¸‘½UÁ‘…Ñ•I•ÍÑ…ÉÐ ¥ì(€½¹ÍÐ‰Ñ¸õ‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•9½Ý	Ñ¸œ¤ì(€‰Ñ¸¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ I•ÍÑ…ÉÑ¥¹œ¸¸¸œ¤í‰Ñ¸¹‘¥Í…‰±•õÑÉÕ”ì(€ÑÉåì(€€€½¹ÍÐ¨õ…Ý…¥Ð…Á¤ œ½…Á¤½ÕÁ‘…Ñ•}É•ÍÑ…ÉÐœ±íµ•Ñ¡½èA=MPô¤ì(€€€¥˜¡¨¹É•±…Õ¹ ôôõ™…±Í”¥ì(€€€€€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•5Íœœ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ UÁ‘…Ñ”¥¹ÍÑ…±±•¸A±•…Í”±½Í”Ñ¡¥ÌÝ¥¹‘½Ü…¹½Á•¸=±¿ŠeÌQY5…Ñ”……¥¸¸œ¤ì(€€€õ•±Í•ì(€€€€€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•5Íœœ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ UÁ‘…Ñ¥¹œ¸¸¸Ñ¡¥ÌÝ¥¹‘½ÜÝ¥±°É•±½…Í¡½ÉÑ±ä¸œ¤ì(€€€€€½¹ÍÐÍÑ…ÉÑ•õ…Ñ”¹¹½Ü ¤ì(€€€€€½¹ÍÐÝ…¥Ñ½ÉI•ÍÑ…ÉÐõ…Íå¹Œ™Õ¹Ñ¥½¸ ¥ì(€€€€€€€ÑÉåì(€€€€€€€€€½¹ÍÐÉ•ÍÁ½¹Í”õ…Ý…¥Ð™•Ñ  œ½…Á¤½Á¥¹œœ±í…¡”è¹¼µÍÑ½É”ô¤ì(€€€€€€€€€½¹ÍÐÁ¥¹œõ…Ý…¥ÐÉ•ÍÁ½¹Í”¹©Í½¸ ¤ì(€€€€€€€€€¥˜¡Á¥¹œ˜™Á¥¹œ¹…ÁÀôôô½±½ÌµÑÙµ…Ñ”œ¥í±½…Ñ¥½¸¹É•±½… ¤íÉ•ÑÕÉ¸íô(€€€€€€€õ…Ñ ¡”¥íô(€€€€€€€¥˜¡…Ñ”¹¹½Ü ¤µÍÑ…ÉÑ•ðØÀÀÀÀ¥Í•ÑQ¥µ•½ÕÐ¡Ý…¥Ñ½ÉI•ÍÑ…ÉÐ°ÄÔÀÀ¤ì(€€€€€€€•±Í”‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•5Íœœ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ I•ÍÑ…ÉÐ™…¥±•¸A±•…Í”±½Í”…¹É•½Á•¸Ñ¡”…ÁÀ¸œ¤ì(€€€€€ôì(€€€€€Í•ÑQ¥µ•½ÕÐ¡Ý…¥Ñ½ÉI•ÍÑ…ÉÐ°ÔÀÀÀ¤ì(€€€ô(€õ…Ñ ¡”¥ì(€€€‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•5Íœœ¤¹Ñ•áÑ½¹Ñ•¹ÐõÑÈ I•ÍÑ…ÉÐ™…¥±•¸A±•…Í”±½Í”…¹É•½Á•¸Ñ¡”…ÁÀ¸œ¤ì(€ô)ô)™Õ¹Ñ¥½¸‘¥Íµ¥ÍÍUÁ‘…Ñ” ¥í‘½Õµ•¹Ð¹•Ñ±•µ•¹Ñ	å% ÕÁ‘…Ñ•	…¹¹•Èœ¤¹±…ÍÍ1¥ÍÐ¹…‘ ¡¥‘”œ¤íô)¡•­½ÉUÁ‘…Ñ” ¤ì(ð½ÍÉ¥ÁÐø(ð½‰½‘äøð½¡Ñµ°ø(ˆˆˆ((Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´(ŒI•ÅÕ•ÍÐ¡…¹‘±•È(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´()}1MQ}Q%Y%Qd€ôÑ¥µ”¹µ½¹½Ñ½¹¥Œ ¤)}Q%Y%Qe}1=,€ôÑ¡É•…‘¥¹œ¹1½¬ ¤()‘•˜Ñ•ÍÑ}•áÑ•É¹…±}Í½ÕÉ”¡­•ä¤è(€€€€ˆˆ‰IÕ¸½¹”™É•Í ¡•…±Ñ ÁÉ½‰”…¹É•ÑÕÉ¸Ý¡•Ñ¡•È¥ÐÝ…Ì…ÁÁ±¥…‰±”¸ˆˆˆ(€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€à€ôaÑÉ•…´¡™œ¤(€€€Í¥€ôÍÑÈ¡™œ¹•Ð ‰ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€ÁÉ½‰•Ì€ôì(€€€€€€€€‰™½Ñµ½ˆˆè±…µ‰‘„è¡ÑÑÁ}•Ñ}©Í½¸¡=Q5=	}%1e}5Q!L¹™½Éµ…Ð (€€€€€€€€€€€‘…Ñ”õÑ¥µ”¹ÍÑÉ™Ñ¥µ” ˆ•d•´•ˆ°Ñ¥µ”¹±½…±Ñ¥µ” ¤¤¤°Ñ¥µ•½ÕÐôÄÔ¤°(€€€€€€€€‰±ÑØˆè±…µ‰‘„è™•Ñ¡}±ÑÙ}‘…¥±ä¡‘…Ñ•Ñ¥µ”¹‘…Ñ”¹Ñ½‘…ä ¤¹¥Í½™½Éµ…Ð ¤¤°(€€€€€€€€‰ÑÙµ…é”ˆè±…µ‰‘„è}ÑÙµ…é•}•Á¥Í½‘•}Í¡•‘Õ±” ‰	É•…­¥¹œ	…ˆ°™½É”õQÉÕ”¤°(€€€€€€€€‰¥¹•µ•Ñ„ˆè±…µ‰‘„è¡ÑÑÁ}•Ñ}©Í½¸ (€€€€€€€€€€€€‰¡ÑÑÁÌè¼½ØÌµ¥¹•µ•Ñ„¹ÍÑÉ•´¹¥¼½…Ñ…±½œ½µ½Ù¥”½Ñ½À½Í•…É õµ…ÑÉ¥à¹©Í½¸ˆ°Ñ¥µ•½ÕÐôÄÔ¤°(€€€€€€€€‰˜Äˆè±…µ‰‘„è•Ñ}˜Å}Í¡•‘Õ±”¡™½É”õQÉÕ”¤°(€€€€€€€€‰˜Èˆè±…µ‰‘„è•Ñ}™¥…}É…¥¹}Ý••­•¹‘Ì ‰˜Èˆ°™½É”õQÉÕ”¤°(€€€€€€€€‰˜Ìˆè±…µ‰‘„è•Ñ}™¥…}É…¥¹}Ý••­•¹‘Ì ‰˜Ìˆ°™½É”õQÉÕ”¤°(€€€€€€€€‰¥¹‘å…Èˆè±…µ‰‘„è•Ñ}¥¹‘å…É}Í¡•‘Õ±”¡™½É”õQÉÕ”¤°(€€€€€€€€‰ÝÉŒˆè±…µ‰‘„è•Ñ}ÝÉ}Í¡•‘Õ±”¡™½É”õQÉÕ”¤°(€€€€€€€€‰™½ÉµÕ±…”ˆè±…µ‰‘„è•Ñ}™½ÉµÕ±…•}Í¡•‘Õ±”¡™½É”õQÉÕ”¤°(€€€€€€€€‰Ý•Œˆè±…µ‰‘„è•Ñ}Ý•}Í¡•‘Õ±”¡™½É”õQÉÕ”¤°(€€€€€€€€‰µ½Ñ½Àˆè±…µ‰‘„è•Ñ}µ½Ñ½Á}Í¡•‘Õ±”¡™½É”õQÉÕ”¤°(€€€ô(€€€¥˜­•ä€ôô€‰áÑÉ•…´ˆè(€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€}É•½É‘}Í½ÕÉ”¡­•ä°9½¹”°•ÉÉ½Èô‰9½Ð½¹™¥ÕÉ•ˆ¤(€€€€€€€€€€€É•ÑÕÉ¸ì‰­•äˆè­•ä°€‰Í­¥ÁÁ•ˆèQÉÕ•ô(€€€€€€€ÁÉ½‰”€ô±…µ‰‘„èà¹±½¥¸ ¤(€€€•±¥˜­•ä€ôô€‰•Á}áµ±ÑØˆè(€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€}É•½É‘}Í½ÕÉ”¡­•ä°9½¹”°•ÉÉ½Èô‰9½Ð½¹™¥ÕÉ•ˆ¤(€€€€€€€€€€€É•ÑÕÉ¸ì‰­•äˆè­•ä°€‰Í­¥ÁÁ•ˆèQÉÕ•ô(€€€€€€€ÁÉ½‰”€ô±…µ‰‘„èÁÉ½‰•}áµ±ÑØ¡à¤(€€€•±¥˜­•ä€ôô€‰ÍÑ•…´ˆè(€€€€€€€¥˜¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰q‘ìÄÝôˆ°Í¥¤è(€€€€€€€€€€€}É•½É‘}Í½ÕÉ”¡­•ä°9½¹”°•ÉÉ½Èô‰9½Ð½¹™¥ÕÉ•ˆ¤(€€€€€€€€€€€É•ÑÕÉ¸ì‰­•äˆè­•ä°€‰Í­¥ÁÁ•ˆèQÉÕ•ô(€€€€€€€ÁÉ½‰”€ô±…µ‰‘„èÍÑ•…µ}ÁÕ‰±¥}ÁÉ½™¥±”¡Í¥°™½É”õQÉÕ”¤(€€€•±Í”è(€€€€€€€ÁÉ½‰”€ôÁÉ½‰•Ì¹•Ð¡­•ä¤(€€€¥˜¹½ÐÁÉ½‰”è(€€€€€€€}É•½É‘}Í½ÕÉ”¡­•ä°9½¹”°•ÉÉ½Èô‰9½Ð…Ù…¥±…‰±”ˆ¤(€€€€€€€É•ÑÕÉ¸ì‰­•äˆè­•ä°€‰Í­¥ÁÁ•ˆèQÉÕ•ô(€€€ÍÑ…ÉÑ•€ôÑ¥µ”¹Á•É™}½Õ¹Ñ•È ¤(€€€ÑÉäè(€€€€€€€É•ÍÕ±Ð€ôÁÉ½‰” ¤(€€€€€€€¥˜­•ä€ôô€‰áÑÉ•…´ˆè(€€€€€€€€€€€½¬°‘•Ñ…¥°€ôÉ•ÍÕ±Ð(€€€€€€€€€€€¥˜¹½Ð½¬è(€€€€€€€€€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È¡‘•Ñ…¥°½È€‰±½¥¸™…¥±•ˆ¤(€€€€€€€€€€€½Õ¹Ð€ô9½¹”(€€€€€€€•±Í”è(€€€€€€€€€€€½Õ¹Ð€ô±•¸¡É•ÍÕ±Ð¤¥˜¥Í¥¹ÍÑ…¹”¡É•ÍÕ±Ð°€¡±¥ÍÐ°ÑÕÁ±”°‘¥Ð¤¤•±Í”9½¹”(€€€€€€€±…Ñ•¹ä€ô¥¹Ð ¡Ñ¥µ”¹Á•É™}½Õ¹Ñ•È ¤€´ÍÑ…ÉÑ•¤€¨€ÄÀÀÀ¤(€€€€€€€}É•½É‘}Í½ÕÉ”¡­•ä°QÉÕ”°½Õ¹Ðõ½Õ¹Ð°±…Ñ•¹å}µÌõ±…Ñ•¹ä¤(€€€€€€€É•ÑÕÉ¸ì‰­•äˆè­•ä°€‰½¬ˆèQÉÕ”°€‰½Õ¹Ðˆè½Õ¹Ð°€‰±…Ñ•¹å}µÌˆè±…Ñ•¹åô(€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€±…Ñ•¹ä€ô¥¹Ð ¡Ñ¥µ”¹Á•É™}½Õ¹Ñ•È ¤€´ÍÑ…ÉÑ•¤€¨€ÄÀÀÀ¤(€€€€€€€}É•½É‘}Í½ÕÉ”¡­•ä°…±Í”°•ÉÉ½Èõ”°±…Ñ•¹å}µÌõ±…Ñ•¹ä¤(€€€€€€€É•ÑÕÉ¸ì‰­•äˆè­•ä°€‰½¬ˆè…±Í”°€‰•ÉÉ½ÈˆèÍÑÈ¡”¥lèÈÀÁt°€‰±…Ñ•¹å}µÌˆè±…Ñ•¹åô()‘•˜}µ…É­}…ÁÁ}…Ñ¥Ù¥Ñä ¤è(€€€±½‰…°}1MQ}Q%Y%Qd(€€€Ý¥Ñ }Q%Y%Qe}1=,è(€€€€€€€}1MQ}Q%Y%Qd€ôÑ¥µ”¹µ½¹½Ñ½¹¥Œ ¤()‘•˜}¥¹…Ñ¥Ù•}Í•½¹‘Ì ¤è(€€€Ý¥Ñ }Q%Y%Qe}1=,è(€€€€€€€É•ÑÕÉ¸µ…à À¸À°Ñ¥µ”¹µ½¹½Ñ½¹¥Œ ¤€´}1MQ}Q%Y%Qd¤()±…ÍÌ!…¹‘±•È¡	…Í•!QQAI•ÅÕ•ÍÑ!…¹‘±•È¤è(€€€‘•˜±½}µ•ÍÍ…”¡Í•±˜°€©„¤è(€€€€€€€Á…ÍÌ((€€€‘•˜}Í•¹¡Í•±˜°½‘”°‰½‘ä°ÑåÁ”ô‰…ÁÁ±¥…Ñ¥½¸½©Í½¸ˆ¤è(€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡‰½‘ä°€¡‘¥Ð°±¥ÍÐ¤¤è(€€€€€€€€€€€‰½‘ä€ô©Í½¸¹‘ÕµÁÌ¡‰½‘ä¤(€€€€€€€‘…Ñ„€ô‰½‘ä¹•¹½‘” ‰ÕÑ˜´àˆ¤(€€€€€€€ÑÉäè(€€€€€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í”¡½‘”¤(€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°ÑåÁ”€¬€ˆì¡…ÉÍ•ÐõÕÑ˜´àˆ¤(€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰…¡”µ½¹ÑÉ½°ˆ°€‰¹¼µÍÑ½É”ˆ¤(€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°ÍÑÈ¡±•¸¡‘…Ñ„¤¤¤(€€€€€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤(€€€€€€€€€€€Í•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡‘…Ñ„¤(€€€€€€€•á•ÁÐ€¡	É½­•¹A¥Á•ÉÉ½È°½¹¹•Ñ¥½¹‰½ÉÑ•‘ÉÉ½È°½¹¹•Ñ¥½¹I•Í•ÑÉÉ½È¤è(€€€€€€€€€€€€Œ	É½ÝÍ•ÉÌÉ½ÕÑ¥¹•±ä…¹•°½‰Í½±•Ñ”A$É•ÅÕ•ÍÑÌ‘ÕÉ¥¹œÉ•±½…‘Ì°(€€€€€€€€€€€€Œ¹…Ù¥…Ñ¥½¸…¹…ÁÀÕÁ‘…Ñ•Ì¸Q¡”É•ÍÁ½¹Í”¡…Ì¹½Ý¡•É”Ñ¼¼¸(€€€€€€€€€€€É•ÑÕÉ¸((€€€‘•˜}Í•¹‘}¥µ…•}™¥±”¡Í•±˜°Á…Ñ °ÑåÁ”õ9½¹”°…¡•}½¹ÑÉ½°ô‰ÁÕ‰±¥Œ°µ…àµ…”ôÌÄÔÌØÀÀÀ°¥µµÕÑ…‰±”ˆ¤è(€€€€€€€Ý¥Ñ ½Á•¸¡Á…Ñ °€‰Éˆˆ¤…Ì˜è(€€€€€€€€€€€É…Ü€ô˜¹É•… ¤(€€€€€€€ÑåÁ”€ôÑåÁ”½È}¥µ…•}½¹Ñ•¹Ñ}ÑåÁ”¡É…Ü¤½È€‰¥µ…”½©Á•œˆ(€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í” ÈÀÀ¤(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°ÑåÁ”¤(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰…¡”µ½¹ÑÉ½°ˆ°…¡•}½¹ÑÉ½°¤(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°ÍÑÈ¡±•¸¡É…Ü¤¤¤(€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤(€€€€€€€Í•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡É…Ü¤((€€€‘•˜}•Ñ}É…¥¹}…Á¤¡Í•±˜°Á…Ñ °Ä¤è(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½˜Å}Í¡•‘Õ±”ˆè(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰•Ù•¹ÑÌˆè•Ñ}˜Å}Í¡•‘Õ±” ¥ô¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½É…¥¹œˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€Í•±•Ñ•€ôm­•ä™½È­•ä¥¸™œ¹•Ð ‰É…¥¹}Í•É¥•Ìˆ°l‰˜Ä‰t¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜­•ä¥¸€ ‰˜Äˆ°€‰˜Èˆ°€‰˜Ìˆ°€‰¥¹‘å…Èˆ°€‰Ý•Œˆ°€‰™½ÉµÕ±…”ˆ°€‰µ½Ñ½Àˆ°€‰ÝÉŒˆ¥t(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰Í•±•Ñ•ˆèÍ•±•Ñ•°€‰•Ù•¹ÑÌˆè•Ñ}É…¥¹}•Ù•¹ÑÌ¡Í•±•Ñ•¥ô¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½É…¥¹}…Ù…¥±…‰¥±¥Ñäˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤ìà€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ù…¥±…‰¥±¥Ñäˆèíô°€‰±½•‘}¥¸ˆè…±Í•ô¤(€€€€€€€€€€€Í•±•Ñ•€ôm­•ä™½È­•ä¥¸™œ¹•Ð ‰É…¥¹}Í•É¥•Ìˆ°l‰˜Ä‰t¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜­•ä¥¸}I%9}!991}QI5Mt(€€€€€€€€€€€…¡•}­•ä€ô}Ù½‘}…¡•}­•ä¡à¤€¬€‰ðˆ€¬€ˆ°ˆ¹©½¥¸¡Í•±•Ñ•¤(€€€€€€€€€€€…¡•€ô}I%9}Y%1	%1%Qe}!(€€€€€€€€€€€¥˜€¡…¡•¹•Ð ‰­•äˆ¤€ôô…¡•}­•ä…¹(€€€€€€€€€€€€€€€€€€€Ñ¥µ”¹Ñ¥µ” ¤€´™±½…Ð¡…¡•¹•Ð ‰ÑÌˆ¤½È€À¤€ð}I%9}Y%1	%1%Qe}QQ0¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ù…¥±…‰¥±¥Ñäˆè…¡•¹•Ð ‰…Ù…¥±…‰¥±¥Ñäˆ¤½Èíô°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±½•‘}¥¸ˆèQÉÕ•ô¤(€€€€€€€€€€€•Ù•¹ÑÌ€ô•Ñ}É…¥¹}•Ù•¹ÑÌ¡Í•±•Ñ•¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ù…¥±…‰¥±¥Ñäˆèíô°€‰±½•‘}¥¸ˆèQÉÕ•ô¤(€€€€€€€€€€€¹½Ü€ôÑ¥µ”¹Ñ¥µ” ¤ì…Ù…¥±…‰¥±¥Ñä€ôíô(€€€€€€€€€€€™½È•Ù•¹Ð¥¸•Ù•¹ÑÌè(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€•ÑÌ€ô‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡ÍÑÈ¡•Ù•¹Ð¹•Ð ‰ÍÑ…ÉÐˆ¤½È€ˆˆ¤¹É•Á±…” ‰hˆ°€ˆ¬ÀÀèÀÀˆ¤¤¹Ñ¥µ•ÍÑ…µÀ ¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€¥˜•ÑÌ€ð¹½Ü€´€ÄÈ€¨€ÌØÀÀ½È•ÑÌ€ø¹½Ü€¬€ÐÔ€¨€ÈÐ€¨€ÌØÀÀè(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€¡¥ÑÌ€ô™¥¹‘}É…¥¹}¡…¹¹•±Ì¡•Ù•¹Ð°¡…¹¹•±Ì°…ÑÌ°à¤(€€€€€€€€€€€€€€€¥˜¡¥ÑÌè(€€€€€€€€€€€€€€€€€€€…Ù…¥±…‰¥±¥Ñåm}É…¥¹}•Ù•¹Ñ}­•ä¡•Ù•¹Ð¥t€ô¡¥ÑÌ(€€€€€€€€€€€}I%9}Y%1	%1%Qe}!¹ÕÁ‘…Ñ”¡ì‰­•äˆè…¡•}­•ä°€‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ù…¥±…‰¥±¥Ñäˆè…Ù…¥±…‰¥±¥Ñåô¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ù…¥±…‰¥±¥Ñäˆè…Ù…¥±…‰¥±¥Ñä°€‰±½•‘}¥¸ˆèQÉÕ•ô¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½É…¥¹}‘É¥Ù•ÉÌˆè(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰‘É¥Ù•ÉÌˆè•Ñ}É…¥¹}‘É¥Ù•ÉÌ ¥ô¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ˆè(€€€€€€€€€€€­•ä€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€¥˜¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰lÀ´åµi„µé|µt¬ˆ°­•ä¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…‘É¥Ù•È¥‰ô¤(€€€€€€€€€€€‘É¥Ù•È€ô¹•áÐ ¡É½Ü™½ÈÉ½Ü¥¸•Ñ}É…¥¹}‘É¥Ù•ÉÌ ¤¥˜É½Ü¹•Ð ‰­•äˆ¤€ôô­•ä¤°9½¹”¤(€€€€€€€€€€€¥µ…•}Á…Ñ €ô}…¡•}É…¥¹}‘É¥Ù•É}Á¥ÑÕÉ”¡‘É¥Ù•È½Èíô¤¥˜‘É¥Ù•È•±Í”€ˆˆ(€€€€€€€€€€€¥˜¹½Ð¥µ…•}Á…Ñ è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰‘É¥Ù•È¥µ…”¹½Ð™½Õ¹‰ô¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹‘}¥µ…•}™¥±”¡¥µ…•}Á…Ñ ¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½˜Å}Ñ•…µÌˆè(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰Ñ•…µÌˆè•Ñ}˜Å}Ñ•…µÌ ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰™…Ù½É¥Ñ•Ìˆè±½…‘}™…Ù½É¥Ñ•Ì ¤¹•Ð ‰˜Å}Ñ•…µÌˆ°mt¥ô¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½˜Å}Ñ•…µ}±½¼ˆè(€€€€€€€€€€€½¹ÍÑÉÕÑ½É}¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€¥˜¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰lÀ´åµi„µé|µt¬ˆ°½¹ÍÑÉÕÑ½É}¥¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…½¹ÍÑÉÕÑ½È¥‰ô¤(€€€€€€€€€€€¥µ…•}Á…Ñ €ô}…¡•}˜Å}±½¼¡½¹ÍÑÉÕÑ½É}¥¤(€€€€€€€€€€€¥˜¹½Ð¥µ…•}Á…Ñ è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰ÄÑ•…´±½¼¹½Ð™½Õ¹‰ô¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹‘}¥µ…•}™¥±”¡¥µ…•}Á…Ñ ¤((€€€‘•˜‘½}P¡Í•±˜¤è(€€€€€€€Ô€ôÕÉ±±¥ˆ¹Á…ÉÍ”¹ÕÉ±Á…ÉÍ”¡Í•±˜¹Á…Ñ ¤(€€€€€€€Ä€ôÕÉ±±¥ˆ¹Á…ÉÍ”¹Á…ÉÍ•}ÅÌ¡Ô¹ÅÕ•Éä¤(€€€€€€€ÑÉäè(€€€€€€€€€€€¥˜Ô¹Á…Ñ ¥¸ìˆ½…Á¤½˜Å}Í¡•‘Õ±”ˆ°€ˆ½…Á¤½É…¥¹œˆ°€ˆ½…Á¤½É…¥¹}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆ½…Á¤½É…¥¹}‘É¥Ù•ÉÌˆ°€ˆ½…Á¤½É…¥¹}‘É¥Ù•É}¥µ…”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆ½…Á¤½˜Å}Ñ•…µÌˆ°€ˆ½…Á¤½˜Å}Ñ•…µ}±½¼‰ôè(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}•Ñ}É…¥¹}…Á¤¡Ô¹Á…Ñ °Ä¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Ñ•…µ}±½¼ˆè(€€€€€€€€€€€€€€€Ñ•…µ}¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¥¹¥Í‘¥¥Ð ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…Ñ•…´¥‰ô¤(€€€€€€€€€€€€€€€¥˜¹½Ð}…¡•}Ñ•…µ}±½¼¡Ñ•…µ}¥¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰Ñ•…´±½¼¹½Ð™½Õ¹‰ô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹‘}¥µ…•}™¥±”¡}Ñ•…µ}±½½}Á…Ñ ¡Ñ•…µ}¥¤°€‰¥µ…”½Á¹œˆ¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½±•…Õ•}±½¼ˆè(€€€€€€€€€€€€€€€±•…Õ•}¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½Ð±•…Õ•}¥¹¥Í‘¥¥Ð ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…±•…Õ”¥‰ô¤(€€€€€€€€€€€€€€€¥˜¹½Ð}…¡•}±•…Õ•}±½¼¡±•…Õ•}¥¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰±•…Õ”±½¼¹½Ð™½Õ¹‰ô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹‘}¥µ…•}™¥±”¡}±•…Õ•}±½½}Á…Ñ ¡±•…Õ•}¥¤°€‰¥µ…”½Á¹œˆ¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½¡…¹¹•±}±½¼ˆè(€€€€€€€€€€€€€€€ÍÑÉ•…µ}¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰lÀ´åµi„µé|µt¬ˆ°ÍÑÉ•…µ}¥¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…ÍÑÉ•…´¥‰ô¤(€€€€€€€€€€€€€€€¥½¹}ÕÉ°€ô}ÍÑÉ•…µ}¥½¹}™½É}¥¡ÍÑÉ•…µ}¥¤(€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•È€ô}Ù½‘}…¡•}­•ä¡aÑÉ•…´¡±½…‘}½¹™¥œ ¤¤¤(€€€€€€€€€€€€€€€Á…Ñ €ô}…¡•}¡…¹¹•±}±½¼¡ÍÑÉ•…µ}¥°¥½¹}ÕÉ°°ÁÉ½Ù¥‘•È¤(€€€€€€€€€€€€€€€¥˜¹½ÐÁ…Ñ è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰¡…¹¹•°±½¼¹½Ð™½Õ¹‰ô¤(€€€€€€€€€€€€€€€Ý¥Ñ ½Á•¸¡Á…Ñ °€‰Éˆˆ¤…Ì˜è(€€€€€€€€€€€€€€€€€€€É…Ü€ô˜¹É•… ¤(€€€€€€€€€€€€€€€ÑåÁ”€ô}¥µ…•}½¹Ñ•¹Ñ}ÑåÁ”¡É…Ü¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑåÁ”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰¥¹Ù…±¥¡…¹¹•°±½¼‰ô¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í” ÈÀÀ¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°ÑåÁ”¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰…¡”µ½¹ÑÉ½°ˆ°€‰ÁÕ‰±¥Œ°µ…àµ…”ôÌÄÔÌØÀÀÀ°¥µµÕÑ…‰±”ˆ¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°ÍÑÈ¡±•¸¡É…Ü¤¤¤(€€€€€€€€€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤(€€€€€€€€€€€€€€€Í•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡É…Ü¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÍÑ•…µ}…Ù…Ñ…Èˆè(€€€€€€€€€€€€€€€ÍÑ•…µ}¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰q‘ìÄÝôˆ°ÍÑ•…µ}¥¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…ÍÑ•…´¥‰ô¤(€€€€€€€€€€€€€€€Á…Ñ €ô}ÍÑ•…µ}…Ù…Ñ…É}Á…Ñ ¡ÍÑ•…µ}¥¤(€€€€€€€€€€€€€€€¥˜¹½ÐÁ…Ñ ½È¹½Ð½Ì¹Á…Ñ ¹¥Í™¥±”¡Á…Ñ ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰…Ù…Ñ…È¹½Ð…¡•‰ô¤(€€€€€€€€€€€€€€€Ý¥Ñ ½Á•¸¡Á…Ñ °€‰Éˆˆ¤…Ì˜è(€€€€€€€€€€€€€€€€€€€É…Ü€ô˜¹É•… È€¨€ÄÀÈÐ€¨€ÄÀÈÐ€¬€Ä¤(€€€€€€€€€€€€€€€ÑåÁ”€ô}¥µ…•}½¹Ñ•¹Ñ}ÑåÁ”¡É…Ü¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑåÁ”½È±•¸¡É…Ü¤€ø€È€¨€ÄÀÈÐ€¨€ÄÀÈÐè(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰¥¹Ù…±¥…Ù…Ñ…È‰ô¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í” ÈÀÀ¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°ÑåÁ”¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰…¡”µ½¹ÑÉ½°ˆ°€‰ÁÕ‰±¥Œ°µ…àµ…”ôàØÐÀÀˆ¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°ÍÑÈ¡±•¸¡É…Ü¤¤¤(€€€€€€€€€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤(€€€€€€€€€€€€€€€Í•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡É…Ü¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Í•…Í½¹}…ÉÐˆè(€€€€€€€€€€€€€€€Í¡½Ü€ô€¡Ä¹•Ð ‰Í¡½Üˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤¹±½Ý•È ¤(€€€€€€€€€€€€€€€Í•…Í½¸€ô€¡Ä¹•Ð ‰Í•…Í½¸ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰m„µ˜À´åuìÄÙôˆ°Í¡½Ü¤½È¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰q¬ˆ°Í•…Í½¸¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰……ÉÑÝ½É¬Á…Ñ ‰ô¤(€€€€€€€€€€€€€€€Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡…ÁÁ}‘¥È ¤°€‰…ÉÑÝ½É¬ˆ°€‰ÑÙµ…é”´ˆ€¬Í¡½Ü°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•…Í½¸´ˆ€¬Í•…Í½¸€¬€ˆ¹©Áœˆ¤(€€€€€€€€€€€€€€€¥˜¹½Ð½Ì¹Á…Ñ ¹¥Í™¥±”¡Á…Ñ ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰…ÉÑÝ½É¬¹½Ð™½Õ¹‰ô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹‘}¥µ…•}™¥±”¡Á…Ñ °€‰¥µ…”½©Á•œˆ¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ ¥¸€ ˆ¼ˆ°€ˆ½¥¹‘•à¹¡Ñµ°ˆ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°A¹É•Á±…” ‰}}YIM%=9}|ˆ°YIM%=8¤°€‰Ñ•áÐ½¡Ñµ°ˆ¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÍÑ…ÑÕÌˆè(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€½Õ¹Ð€ô±•¸¡}aQ}!l‰¡…¹¹•±Ì‰t¤¥˜€¡à¹½¹™¥ÕÉ• ¤…¹}aQ}!l‰¡…¹¹•±Ì‰t¤•±Í”9½¹”(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¹™¥ÕÉ•ˆèà¹½¹™¥ÕÉ• ¤°€‰¡…¹¹•±}½Õ¹Ðˆè½Õ¹Ð°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µ…Ñ¡}Ñ¡É•Í¡½±ˆè™œ¹•Ð ‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ°€À¸ØÈ¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Í½ÕÉ•}¡•…±Ñ ˆè(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰Í½ÕÉ•ÌˆèÍ½ÕÉ•}¡•…±Ñ¡}Í¹…ÁÍ¡½Ð ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹½ÜˆèÑ¥µ”¹Ñ¥µ” ¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½™…Ù½É¥Ñ•Ìˆè(€€€€€€€€€€€€€€€™…Ø€ô±½…‘}™…Ù½É¥Ñ•Ì ¤(€€€€€€€€€€€€€€€€Œ•¹É¥ ¡…¹¹•°™…Ù½É¥Ñ•ÌÝ¥Ñ „™É•Í ÍÑÉ•…´UI0(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¡…¹Ì€ômt(€€€€€€€€€€€€€€€™½ÈŒ¥¸™…Ø¹•Ð ‰¡…¹¹•±Ìˆ°mt¤è(€€€€€€€€€€€€€€€€€€€Í¥€ôŒ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤(€€€€€€€€€€€€€€€€€€€¡…¹Ì¹…ÁÁ•¹¡ì(€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}¥ˆèÍ¥°(€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆèŒ¹•Ð ‰¹…µ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ•½ÉäˆèŒ¹•Ð ‰…Ñ•½Éäˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰±½¼ˆèŒ¹•Ð ‰±½¼ˆ¤½È}ÍÑÉ•…µ}¥½¹}™½É}¥¡Í¥¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰ÕÉ°ˆèà¹ÍÑÉ•…µ}ÕÉ°¡Í¥¤¥˜€¡à¹½¹™¥ÕÉ• ¤…¹Í¥¥Ì¹½Ð9½¹”¤•±Í”€ˆˆ°(€€€€€€€€€€€€€€€€€€€ô¤(€€€€€€€€€€€€€€€™…Ù½É¥Ñ•}Í¡½ÝÌ€ômt(€€€€€€€€€€€€€€€™½ÈÍ¡½Ü¥¸™…Ø¹•Ð ‰Í¡½ÝÌˆ°mt¤è(€€€€€€€€€€€€€€€€€€€¥Ñ•´€ô‘¥Ð¡Í¡½Ü¤(€€€€€€€€€€€€€€€€€€€¥Ñ•µl‰¹…µ”‰t€ô}±•…¹}Í¡½Ý}Ñ¥Ñ±”¡¥Ñ•´¹•Ð ‰¹…µ”ˆ¤¤½È¥Ñ•´¹•Ð ‰¹…µ”ˆ°€ˆˆ¤(€€€€€€€€€€€€€€€€€€€¥Ñ•µl‰Í¡½Ý}­•ä‰t€ô¥Ñ•´¹•Ð ‰Í¡½Ý}­•äˆ¤½È}Í¡½Ý}­•ä¡¥Ñ•´¹•Ð ‰¹…µ”ˆ¤¤½ÈÍÑÈ¡¥Ñ•´¹•Ð ‰Í•É¥•Í}¥ˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€€€€€¥Ñ•µl‰Í•É¥•Í}¥‘Ì‰t€ômÍ¥™½ÈÍ¥¥¸€¡¥Ñ•´¹•Ð ‰Í•É¥•Í}¥‘Ìˆ¤½Èm¥Ñ•´¹•Ð ‰Í•É¥•Í}¥ˆ¥t¤¥˜Í¥¥Ì¹½Ð9½¹•t(€€€€€€€€€€€€€€€€€€€™…Ù½É¥Ñ•}Í¡½ÝÌ¹…ÁÁ•¹¡¥Ñ•´¤(€€€€€€€€€€€€€€€Í•±•Ñ•‘}¥‘Ì€ômÍÑÈ¡Í¥¤™½ÈÍ¥¥¸™…Ø¹•Ð ‰µå±¥ÍÑ}¡…¹¹•±Ìˆ°mt¥ulèÕt(€€€€€€€€€€€€€€€…Ù…¥±…‰±•}¥‘Ì€ôíÍÑÈ¡Œ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½ÈŒ¥¸™…Ø¹•Ð ‰¡…¹¹•±Ìˆ°mt¥ô(€€€€€€€€€€€€€€€Í•±•Ñ•‘}¥‘Ì€ômÍ¥™½ÈÍ¥¥¸Í•±•Ñ•‘}¥‘Ì¥˜Í¥¥¸…Ù…¥±…‰±•}¥‘Ít(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ñ•½É¥•Ìˆè™…Ø¹•Ð ‰…Ñ•½É¥•Ìˆ°mt¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¡…¹¹•±Ìˆè¡…¹Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µ½Ù¥•Ìˆè™…Ø¹•Ð ‰µ½Ù¥•Ìˆ°mt¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í¡½ÝÌˆè™…Ù½É¥Ñ•}Í¡½ÝÌ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…µ•Ìˆè™…Ø¹•Ð ‰…µ•Ìˆ°mt¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ•…µÌˆè™…Ø¹•Ð ‰Ñ•…µÌˆ°mt¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰˜Å}Ñ•…µÌˆè™…Ø¹•Ð ‰˜Å}Ñ•…µÌˆ°mt¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µå±¥ÍÑ}¡…¹¹•±ÌˆèÍ•±•Ñ•‘}¥‘Íô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½•Á}Ñ…É•ÑÌˆè(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰aÑÉ•…´¥Ì¹½Ð½¹™¥ÕÉ•‰ô¤(€€€€€€€€€€€€€€€™…Ø€ô±½…‘}™…Ù½É¥Ñ•Ì ¤(€€€€€€€€€€€€€€€Ý…¹Ñ•‘}…Ñ•½É¥•Ì€ôÍ•Ð¡ÍÑÈ¡¹…µ”¤™½È¹…µ”¥¸™…Ø¹•Ð ‰…Ñ•½É¥•Ìˆ°mt¤¤(€€€€€€€€€€€€€€€¥‘Ì€ômÍÑÈ¡ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½È ¥¸™…Ø¹•Ð ‰¡…¹¹•±Ìˆ°mt¤(€€€€€€€€€€€€€€€€€€€€€€¥˜ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¥Ì¹½Ð9½¹•t(€€€€€€€€€€€€€€€¥˜Ý…¹Ñ•‘}…Ñ•½É¥•Ìè(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€€€€€¥‘Ì¹•áÑ•¹¡ÍÑÈ¡ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½È ¥¸¡…¹¹•±Ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¥Ì¹½Ð9½¹”…¹(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…ÑÌ¹•Ð¡ ¹•Ð ‰…Ñ•½Éå}¥ˆ¤°€ˆˆ¤¥¸Ý…¹Ñ•‘}…Ñ•½É¥•Ì¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰¥‘Ìˆè±¥ÍÐ¡‘¥Ð¹™É½µ­•åÌ¡¥‘Ì¤¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½•Áœˆè(€€€€€€€€€€€€€€€€Œ¥‘Ìõ½µµ„µÍ•Á…É…Ñ•ÍÑÉ•…´¥‘Ìì™½É”ôÄ‰åÁ…ÍÍ•Ì…¡”¸(€€€€€€€€€€€€€€€€Œ…¡•ôÄ¥Ì‘¥Í¬½µ•µ½Éä½¹±ä…¹¹•Ù•È½¹Ñ…ÑÌÑ¡”ÁÉ½Ù¥‘•È¸(€€€€€€€€€€€€€€€¥‘Í}É…Ü€ô€¡Ä¹•Ð ‰¥‘Ìˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€™½É”€ôÄ¹•Ð ‰™½É”ˆ°lˆÀ‰t¥lÁt€ôô€ˆÄˆ(€€€€€€€€€€€€€€€…¡•‘}½¹±ä€ôÄ¹•Ð ‰…¡•ˆ°lˆÀ‰t¥lÁt€ôô€ˆÄˆ(€€€€€€€€€€€€€€€…±±}™…Ù½É¥Ñ•Ì€ôÄ¹•Ð ‰™…Ù½É¥Ñ•Ìˆ°lˆÀ‰t¥lÁt€ôô€ˆÄˆ(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤½È€¡¹½Ð¥‘Í}É…Ü…¹¹½Ð…±±}™…Ù½É¥Ñ•Ì¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…É•ÅÕ•ÍÐ‰ô¤(€€€€€€€€€€€€€€€}±½…‘}•Á}‘¥Í­}…¡”¡à¤(€€€€€€€€€€€€€€€¥‘Ì€ômÌ¹ÍÑÉ¥À ¤™½ÈÌ¥¸¥‘Í}É…Ü¹ÍÁ±¥Ð ˆ°ˆ¤¥˜Ì¹ÍÑÉ¥À ¥t(€€€€€€€€€€€€€€€¥˜…±±}™…Ù½É¥Ñ•Ìè(€€€€€€€€€€€€€€€€€€€™…Ø€ô±½…‘}™…Ù½É¥Ñ•Ì ¤(€€€€€€€€€€€€€€€€€€€Ý…¹Ñ•‘}…Ñ•½É¥•Ì€ôÍ•Ð¡ÍÑÈ¡¹…µ”¤™½È¹…µ”¥¸™…Ø¹•Ð ‰…Ñ•½É¥•Ìˆ°mt¤¤(€€€€€€€€€€€€€€€€€€€¥‘Ì¹•áÑ•¹¡ÍÑÈ¡ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½È ¥¸™…Ø¹•Ð ‰¡…¹¹•±Ìˆ°mt¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¥Ì¹½Ð9½¹”¤(€€€€€€€€€€€€€€€€€€€¥˜Ý…¹Ñ•‘}…Ñ•½É¥•Ìè(€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥‘Ì¹•áÑ•¹¡ÍÑÈ¡ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½È ¥¸¡…¹¹•±Ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¥Ì¹½Ð9½¹”…¹(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…ÑÌ¹•Ð¡ ¹•Ð ‰…Ñ•½Éå}¥ˆ¤°€ˆˆ¤¥¸Ý…¹Ñ•‘}…Ñ•½É¥•Ì¤(€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€€ŒMÑ…‰±”‘”µ‘ÕÁ±¥…Ñ¥½¸µ…ÑÑ•ÉÌÝ¡•¸„¡…¹¹•°¥Ì‘¥É•Ñ±ä(€€€€€€€€€€€€€€€€Œ™…Ù½É¥Ñ•…¹…±Í¼‰•±½¹ÌÑ¼„™…Ù½É¥Ñ”…Ñ•½Éä¸(€€€€€€€€€€€€€€€¥‘Ì€ô±¥ÍÐ¡‘¥Ð¹™É½µ­•åÌ¡¥‘Ì¤¤(€€€€€€€€€€€€€€€¹½Ü€ôÑ¥µ”¹Ñ¥µ” ¤(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ôíô(€€€€€€€€€€€€€€€Ñ½}™•Ñ €ômt(€€€€€€€€€€€€€€€ÍÑ…ÑÌ€ôì‰ÕÁ‘…Ñ•ˆè€À°€‰áµ±ÑÙ}™¥±±•ˆè€À°€‰™…±±‰…­}ÕÁ‘…Ñ•ˆè€À°(€€€€€€€€€€€€€€€€€€€€€€€€€‰¹½}‘…Ñ„ˆè€À°€‰™…¥±•ˆè€Áô(€€€€€€€€€€€€€€€…¡•}¡…¹•€ô…±Í”(€€€€€€€€€€€€€€€™½ÈÍ¥¥¸¥‘Ìè(€€€€€€€€€€€€€€€€€€€…¡•€ô}A}!¹•Ð¡Í¥¤(€€€€€€€€€€€€€€€€€€€¥˜…¡•…¹…¡•‘}½¹±äè(€€€€€€€€€€€€€€€€€€€€€€€€Œ…¡•µ½¹±ä¹…Ù¥…Ñ¥½¸¹•Ù•È½¹Ñ…ÑÌÑ¡”ÁÉ½Ù¥‘•È¸I•Ñ…¥¹•(€€€€€€€€€€€€€€€€€€€€€€€€ŒÕ¥‘”‘…Ñ„É•µ…¥¹ÌÕÍ•™Õ°…Ì…¸½™™±¥¹”½ÍÑ…±”™…±±‰…¬¸(€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑmÍ¥‘t€ô…¡•‘l‰ÁÉ½É…µµ•Ì‰t(€€€€€€€€€€€€€€€€€€€•±¥˜€¡…¡•…¹¹½Ð™½É”…¹€¡¹½Ü€´…¡•‘l‰ÑÌ‰t€ð}A}IIM!}QQ0¤(€€€€€€€€€€€€€€€€€€€€€€€€€…¹€¡¹½Ð…¡•¹•Ð ‰ÁÉ½É…µµ•Ìˆ¤½È}•Á}…¡•}¡…Í}½Ù•É…”¡…¡•°¹½Ü¤¤¤è(€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑmÍ¥‘t€ô…¡•‘l‰ÁÉ½É…µµ•Ì‰t(€€€€€€€€€€€€€€€€€€€•±¥˜¹½Ð…¡•‘}½¹±äè(€€€€€€€€€€€€€€€€€€€€€€€Ñ½}™•Ñ ¹…ÁÁ•¹¡Í¥¤(€€€€€€€€€€€€€€€€ŒAI%5IdM=UIè½¹”‰Õ±¬a51QX‘½Ý¹±½…½Ù•ÉÌ•Ù•Éä¡…¹¹•°…Ð(€€€€€€€€€€€€€€€€Œ½¹”¸5…À•… Ý…¹Ñ•ÍÑÉ•…µ}¥Ñ¼¥ÑÌ•Á}¡…¹¹•±}¥°™•Ñ Ñ¡”(€€€€€€€€€€€€€€€€ŒÝ¡½±”Õ¥‘”°…¹™¥±°É•ÍÕ±ÑÌ¸¹åÑ¡¥¹œÑ¡”a51QX±…­Ì™…±±Ì(€€€€€€€€€€€€€€€€ŒÑ¡É½Õ Ñ¼Ñ¡”Á•Èµ¡…¹¹•°A$‰•±½Ü¸(€€€€€€€€€€€€€€€¥˜Ñ½}™•Ñ …¹¹½Ð…¡•‘}½¹±äè(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€¡…¹¹•±Ì°}…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€€€€€Í¥‘}Ñ½}•Áœ€ôíô(€€€€€€€€€€€€€€€€€€€€€€€™½È ¥¸¡…¹¹•±Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í¥€ôÍÑÈ¡ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•¥€ôÍÑÈ¡ ¹•Ð ‰•Á}¡…¹¹•±}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜Í¥…¹•¥è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Í¥‘}Ñ½}•ÁmÍ¥‘t€ô•¥(€€€€€€€€€€€€€€€€€€€€€€€Ý…¹Ñ•‘}•Áœ€ôíÍ¥‘}Ñ½}•ÁmÍt™½ÈÌ¥¸Ñ½}™•Ñ ¥˜Ì¥¸Í¥‘}Ñ½}•Áô(€€€€€€€€€€€€€€€€€€€€€€€¥˜Ý…¹Ñ•‘}•Áœè(€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á}‰å}¡…¹¹•°€ô™•Ñ¡}áµ±ÑÙ}•Áœ¡à°Ý…¹Ñ•‘}•Áœ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€}É•½É‘}Í½ÕÉ” ‰•Á}áµ±ÑØˆ°QÉÕ”°½Õ¹Ðõ±•¸¡•Á}‰å}¡…¹¹•°¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€™¥±±•€ômt(€€€€€€€€€€€€€€€€€€€€€€€€€€€™½ÈÍ¥¥¸Ñ½}™•Ñ è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•¥€ôÍ¥‘}Ñ½}•Áœ¹•Ð¡Í¥¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜•¥…¹•¥¥¸•Á}‰å}¡…¹¹•°è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ì€ô•Á}‰å}¡…¹¹•±m•¥‘t(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€}A}!mÍ¥‘t€ôì‰ÑÌˆè¹½Ü°€‰ÁÉ½É…µµ•ÌˆèÁÉ½Íô(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…¡•}¡…¹•€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑmÍ¥‘t€ôÁÉ½Ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÍl‰ÕÁ‘…Ñ•‰t€¬ô€Ä(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€™¥±±•¹…ÁÁ•¹¡Í¥¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€Œ=¹±ä¡…¹¹•±Ì9=P½Ù•É•‰äa51QX¹••Ñ¡”Í±½ÜÁ…Ñ ¸(€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñ½}™•Ñ €ômÌ™½ÈÌ¥¸Ñ½}™•Ñ ¥˜Ì¹½Ð¥¸™¥±±•‘t(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÍl‰áµ±ÑÙ}™¥±±•‰t€ô±•¸¡™¥±±•¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€€€€€€Œa51QXÕ¹…Ù…¥±…‰±”€¡½™™±¥¹”°‰±½­•°Á…ÉÍ”•ÉÉ½È¤è™…±°(€€€€€€€€€€€€€€€€€€€€€€€€Œ‰…¬•¹Ñ¥É•±äÑ¼Ñ¡”Á•Èµ¡…¹¹•°A$‰•±½Ü¸(€€€€€€€€€€€€€€€€€€€€€€€}É•½É‘}Í½ÕÉ” ‰•Á}áµ±ÑØˆ°…±Í”°•ÉÉ½Èõ”¤(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÍl‰áµ±ÑÙ}•ÉÉ½È‰t€ôÍÑÈ¡”¥lèÄÈÁt(€€€€€€€€€€€€€€€€Œ11	,èaÑÉ•…´ÌÍ¡½ÉÐA•¹‘Á½¥¹Ð¥Ì½¹”É•ÅÕ•ÍÐÁ•È¡…¹¹•°¸(€€€€€€€€€€€€€€€€Œ=¹±äÕÍ•™½È¡…¹¹•±ÌÑ¡”‰Õ±¬a51QX‘¥¹½Ð½Ù•È¸(€€€€€€€€€€€€€€€€ŒMÑÉ¥Ñ±ä½¹”…Ð„Ñ¥µ”Ý¥Ñ „Íµ…±°Á…ÕÍ”•Ù•Éä™½ÕÈ¡…¹¹•±Ìì(€€€€€€€€€€€€€€€€ŒÍ•Ù•É…°ÁÉ½Ù¥‘•ÉÌÉ•©•Ð½ÈÍ¥±•¹Ñ±äÑ¡É½ÑÑ±”½Ù•É±…ÁÁ¥¹œÉ•ÅÕ•ÍÑÌ¸(€€€€€€€€€€€€€€€¥˜Ñ½}™•Ñ è(€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÍl‰Í…™•}µ½‘”‰t€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€™½È¤°Í¥¥¸•¹Õµ•É…Ñ”¡Ñ½}™•Ñ ¤è(€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€ŒI•ÅÕ•ÍÐ„µÕ±Ñ¤µ‘…ä±¥ÍÑ¥¹œÝ¥¹‘½Ü¸AÉ½Ù¥‘•ÉÌµ…ä…ÀÑ¡¥Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€Œ‰ÕÐÉ•Ñ…¥¹¥¹œ•Ù•ÉåÑ¡¥¹œÑ¡•äÉ•ÑÕÉ¸µ…­•ÌÕ¥‘”É•™É•Í¡•Ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€ŒÕÍ•™Õ°™½È‘…åÌÉ…Ñ¡•ÈÑ¡…¸©ÕÍÐÑ¡”ÕÉÉ•¹Ð•Ù•¹¥¹œ¸(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ì€ôà¹Í¡½ÉÑ}•Áœ¡Í¥°}A}1%MQ%9}1%5%P¤(€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ì€ô9½¹”(€€€€€€€€€€€€€€€€€€€€€€€½±€ô}A}!¹•Ð¡Í¥¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜ÁÉ½Ì¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÍl‰™…¥±•‰t€¬ô€Ä(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜½±…¹½±¹•Ð ‰ÁÉ½É…µµ•Ìˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑmÍ¥‘t€ô½±‘l‰ÁÉ½É…µµ•Ì‰t(€€€€€€€€€€€€€€€€€€€€€€€•±¥˜¹½ÐÁÉ½Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÍl‰¹½}‘…Ñ„‰t€¬ô€Ä(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜½±…¹½±¹•Ð ‰ÁÉ½É…µµ•Ìˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑmÍ¥‘t€ô½±‘l‰ÁÉ½É…µµ•Ì‰t(€€€€€€€€€€€€€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€}A}!mÍ¥‘t€ôì‰ÑÌˆè¹½Ü°€‰ÁÉ½É…µµ•Ìˆèmuô(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…¡•}¡…¹•€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑmÍ¥‘t€ômt(€€€€€€€€€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÍl‰ÕÁ‘…Ñ•‰t€¬ô€Ä(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÍl‰™…±±‰…­}ÕÁ‘…Ñ•‰t€¬ô€Ä(€€€€€€€€€€€€€€€€€€€€€€€€€€€}A}!mÍ¥‘t€ôì‰ÑÌˆè¹½Ü°€‰ÁÉ½É…µµ•ÌˆèÁÉ½Íô(€€€€€€€€€€€€€€€€€€€€€€€€€€€…¡•}¡…¹•€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑmÍ¥‘t€ôÁÉ½Ì(€€€€€€€€€€€€€€€€€€€€€€€¥˜€¡¤€¬€Ä¤€”€Ð€ôô€Àè(€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñ¥µ”¹Í±••À À¸ÌÔ¤(€€€€€€€€€€€€€€€¥˜…¡•}¡…¹•è(€€€€€€€€€€€€€€€€€€€}Í…Ù•}•Á}‘¥Í­}…¡”¡à¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰•ÁœˆèÉ•ÍÕ±Ð°€‰Ñ½Ñ…°ˆè±•¸¡¥‘Ì¤°€‰ÍÑ…ÑÌˆèÍÑ…ÑÍô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½•Á}‘•‰Õœˆè(€€€€€€€€€€€€€€€€ŒI•ÑÕÉ¹ÌÑ¡”I\ÁÉ½Ù¥‘•ÈÉ•ÍÁ½¹Í”™½È½¹”ÍÑÉ•…´°™½ÈÑÉ½Õ‰±•Í¡½½Ñ¥¹œ¸(€€€€€€€€€€€€€€€Í¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½Ð€¡à¹½¹™¥ÕÉ• ¤…¹Í¥¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…É•ÅÕ•ÍÐ‰ô¤(€€€€€€€€€€€€€€€‘‰}Ä€ôì‰ÕÍ•É¹…µ”ˆèà¹ÕÍ•È°€‰Á…ÍÍÝ½Éˆèà¹Á…ÍÍÝ½É°(€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ¥½¸ˆè€‰•Ñ}Í¡½ÉÑ}•Áœˆ°€‰ÍÑÉ•…µ}¥ˆèÍÑÈ¡Í¥¤°€‰±¥µ¥Ðˆè€ˆÌ‰ô(€€€€€€€€€€€€€€€‘‰}ÕÉ°€ô˜‰íà¹‰…Í•ô½Á±…å•É}…Á¤¹Á¡Àüˆ€¬ÕÉ±±¥ˆ¹Á…ÉÍ”¹ÕÉ±•¹½‘”¡‘‰}Ä¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€É…Ü€ô¡ÑÑÁ}•Ñ}©Í½¸¡‘‰}ÕÉ°¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¤°€‰ÕÉ°ˆè‘‰}ÕÉ±ô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰É…ÜˆèÉ…Ü°€‰Á…ÉÍ•ˆèà¹Í¡½ÉÑ}•Áœ¡Í¥°±¥µ¥ÐôÌ¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÍÑ•…µ}ÁÉ½™¥±”ˆè(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€ÍÑ•…µ}¥€ôÍÑÈ¡™œ¹•Ð ‰ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰q‘ìÄÝôˆ°ÍÑ•…µ}¥¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰±¥¹­•ˆè…±Í•ô¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€ÁÉ½™¥±”€ôÍÑ•…µ}ÁÕ‰±¥}ÁÉ½™¥±”¡ÍÑ•…µ}¥¤(€€€€€€€€€€€€€€€€€€€ÁÉ½™¥±•l‰±¥¹­•‰t€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ÁÉ½™¥±”¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰±¥¹­•ˆèQÉÕ”°€‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÕÁ‘…Ñ•}¡•¬ˆè(€€€€€€€€€€€€€€€…Ù…¥±…‰±”°É•µ½Ñ”€ô¡•­}™½É}ÕÁ‘…Ñ” ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ù…¥±…‰±”ˆè…Ù…¥±…‰±”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÕÉÉ•¹ÐˆèYIM%=8°€‰±…Ñ•ÍÐˆèÉ•µ½Ñ•ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½¡±Ìˆè(€€€€€€€€€€€€€€€€Œ	Õ¥±Ñ¡”!1LÕÉ°™½È„ÍÑÉ•…µ}¥…¹É•ÑÕÉ¸¥Ð€¡™½È‘¥É•ÐµÑÉä¤¸(€€€€€€€€€€€€€€€Í¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½Ð€¡à¹½¹™¥ÕÉ• ¤…¹Í¥¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…É•ÅÕ•ÍÐ‰ô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰¡±Ìˆèà¹¡±Í}ÕÉ°¡Í¥¤°€‰ÑÌˆèà¹ÍÑÉ•…µ}ÕÉ°¡Í¥¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Á¥¹œˆè(€€€€€€€€€€€€€€€€Œ¡•…À±½…°¥‘•¹Ñ¥Ñä¡•¬ÕÍ•Ñ¼ÁÉ•Ù•¹Ð‘ÕÁ±¥…Ñ”…ÁÀ(€€€€€€€€€€€€€€€€Œ¥¹ÍÑ…¹•ÌÉ•…É‘±•ÍÌ½˜Ý¡…ÐÑ¡”±…Õ¹¡•È€¹•á”¥Ì¹…µ•¸(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…ÁÀˆè€‰½±½ÌµÑÙµ…Ñ”ˆ°€‰Ù•ÉÍ¥½¸ˆèYIM%=9ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÁÉ½áäˆè(€€€€€€€€€€€€€€€€Œ1¥¡ÑÝ•¥¡Ðµ•‘¥„É•±…ä™½È‰É½ÝÍ•ÈÁ±…å‰…¬¸€Q¡¥Ì¹•Ù•È(€€€€€€€€€€€€€€€€ŒÑÉ…¹Í½‘•ÌèÁ±…å±¥ÍÑÌ…É”É•ÝÉ¥ÑÑ•¸…¹µ•‘¥„‰åÑ•Ì…É”ÍÑÉ•…µ•(€€€€€€€€€€€€€€€€ŒÑ¡É½Õ Õ¹¡…¹•Í¼!1L…¹5AµQL…¸Ý½É¬…É½Õ¹=IL¸(€€€€€€€€€€€€€€€Ñ…É•Ð€ôÄ¹•Ð ‰Ôˆ°lˆ‰t¥lÁt(€€€€€€€€€€€€€€€¥˜¹½ÐÑ…É•Ðè(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰¹¼ÕÉ°‰ô¤(€€€€€€€€€€€€€€€Á…ÉÍ•‘}Ñ…É•Ð€ôÕÉ±±¥ˆ¹Á…ÉÍ”¹ÕÉ±ÍÁ±¥Ð¡Ñ…É•Ð¤(€€€€€€€€€€€€€€€¥˜Á…ÉÍ•‘}Ñ…É•Ð¹Í¡•µ”¹½Ð¥¸€ ‰¡ÑÑÀˆ°€‰¡ÑÑÁÌˆ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰Õ¹ÍÕÁÁ½ÉÑ•ÕÉ°‰ô¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€¡•…‘•ÉÌ€ôì(€€€€€€€€€€€€€€€€€€€€€€€€‰UÍ•Èµ•¹Ðˆè€‰Y1¼Ì¸À1¥‰Y1¼Ì¸Àˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰•ÁÐˆè€ˆ¨¼¨‰ô(€€€€€€€€€€€€€€€€€€€É…¹•}¡•…‘•È€ôÍ•±˜¹¡•…‘•ÉÌ¹•Ð ‰I…¹”ˆ¤(€€€€€€€€€€€€€€€€€€€¥˜É…¹•}¡•…‘•Èè(€€€€€€€€€€€€€€€€€€€€€€€¡•…‘•ÉÍl‰I…¹”‰t€ôÉ…¹•}¡•…‘•È(€€€€€€€€€€€€€€€€€€€É•Ä€ôÕÉ±±¥ˆ¹É•ÅÕ•ÍÐ¹I•ÅÕ•ÍÐ¡Ñ…É•Ð°¡•…‘•ÉÌõ¡•…‘•ÉÌ¤(€€€€€€€€€€€€€€€€€€€Ý¥Ñ ÕÉ±±¥ˆ¹É•ÅÕ•ÍÐ¹ÕÉ±½Á•¸¡É•Ä°Ñ¥µ•½ÕÐôÈÀ¤…ÌÉ•ÍÀè(€€€€€€€€€€€€€€€€€€€€€€€ÑåÁ”€ôÉ•ÍÀ¹¡•…‘•ÉÌ¹•Ð ‰½¹Ñ•¹ÐµQåÁ”ˆ°€‰…ÁÁ±¥…Ñ¥½¸½½Ñ•ÐµÍÑÉ•…´ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€Á…Ñ¡}±½Ü€ôÁ…ÉÍ•‘}Ñ…É•Ð¹Á…Ñ ¹±½Ý•È ¤(€€€€€€€€€€€€€€€€€€€€€€€¥Í}Á±…å±¥ÍÐ€ô€ ‰µÁ•ÕÉ°ˆ¥¸ÑåÁ”¹±½Ý•È ¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Á…Ñ¡}±½Ü¹•¹‘ÍÝ¥Ñ  ˆ¹´ÍÔàˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í}Á±…å±¥ÍÐè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€ŒA±…å±¥ÍÑÌ…É”Ñ¥¹ä¸€I•ÝÉ¥Ñ”Í•µ•¹ÑÌÁ±ÕÌUI$ôˆ¸¸¸ˆ(€€€€€€€€€€€€€€€€€€€€€€€€€€€€Œ…ÑÑÉ¥‰ÕÑ•ÌÕÍ•‰ä•¹ÉåÁÑ¥½¸­•åÌ…¹¥¹¥Ðµ…ÁÌ¸(€€€€€€€€€€€€€€€€€€€€€€€€€€€É…Ü€ôÉ•ÍÀ¹É•… Ð€¨€ÄÀÈÐ€¨€ÄÀÈÐ€¬€Ä¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜±•¸¡É…Ü¤€ø€Ð€¨€ÄÀÈÐ€¨€ÄÀÈÐè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰Á±…å±¥ÍÐÑ½¼±…É”ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñ•áÐ€ôÉ…Ü¹‘•½‘” ‰ÕÑ˜´àˆ°€‰É•Á±…”ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€½ÕÑ}±¥¹•Ì€ômt((€€€€€€€€€€€€€€€€€€€€€€€€€€€‘•˜ÁÉ½áå}ÕÉ°¡¡¥±¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…‰Í½±ÕÑ”€ôÕÉ±±¥ˆ¹Á…ÉÍ”¹ÕÉ±©½¥¸¡Ñ…É•Ð°¡¥±¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸€ˆ½…Á¤½ÁÉ½áäýÔôˆ€¬ÕÉ±±¥ˆ¹Á…ÉÍ”¹ÅÕ½Ñ”¡…‰Í½±ÕÑ”°Í…™”ôˆˆ¤((€€€€€€€€€€€€€€€€€€€€€€€€€€€™½È±¥¹”¥¸Ñ•áÐ¹ÍÁ±¥Ñ±¥¹•Ì ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ì€ô±¥¹”¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜Ì…¹¹½ÐÌ¹ÍÑ…ÉÑÍÝ¥Ñ  ˆŒˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½ÕÑ}±¥¹•Ì¹…ÁÁ•¹¡ÁÉ½áå}ÕÉ°¡Ì¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•±¥˜Ì¹ÍÑ…ÉÑÍÝ¥Ñ  ˆŒˆ¤…¹€UI$ôˆœ¥¸±¥¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€±¥¹”€ôÉ”¹ÍÕˆ (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÈUI$ôˆ¡myp‰t¬¤ˆœ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€±…µ‰‘„´è€UI$ôˆœ€¬ÁÉ½áå}ÕÉ°¡´¹É½ÕÀ Ä¤¤€¬€œˆœ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€±¥¹”¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½ÕÑ}±¥¹•Ì¹…ÁÁ•¹¡±¥¹”¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½ÕÑ}±¥¹•Ì¹…ÁÁ•¹¡±¥¹”¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€É…Ü€ô€ ‰q¸ˆ¹©½¥¸¡½ÕÑ}±¥¹•Ì¤€¬€‰q¸ˆ¤¹•¹½‘” ‰ÕÑ˜´àˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í” ÈÀÀ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°€‰…ÁÁ±¥…Ñ¥½¸½Ù¹¹…ÁÁ±”¹µÁ•ÕÉ°ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰•ÍÌµ½¹ÑÉ½°µ±±½Üµ=É¥¥¸ˆ°€ˆ¨ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰…¡”µ½¹ÑÉ½°ˆ°€‰¹¼µ…¡”ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°ÍÑÈ¡±•¸¡É…Ü¤¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡É…Ü¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸((€€€€€€€€€€€€€€€€€€€€€€€€Œ5•‘¥„¥ÌÍÑÉ•…µ•…Ì¥Ð…ÉÉ¥Ù•Ì¥¹ÍÑ•…½˜É•ÍÀ¹É•… ¤¸(€€€€€€€€€€€€€€€€€€€€€€€€ŒQ¡…Ð¥Ì•ÍÍ•¹Ñ¥…°™½È±¥Ù”QL…¹…Ù½¥‘Ì‰Õ™™•É¥¹œ„(€€€€€€€€€€€€€€€€€€€€€€€€ŒÝ¡½±”Ù¥‘•¼±½…±±ä¸€	åÑ•Ì…É”¹•Ù•È‘•½‘•½É”µ•¹½‘•¸(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÕÌ€ô•Ñ…ÑÑÈ¡É•ÍÀ°€‰ÍÑ…ÑÕÌˆ°€ÈÀÀ¤½È€ÈÀÀ(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í”¡ÍÑ…ÑÕÌ¤(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°ÑåÁ”¤(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰•ÍÌµ½¹ÑÉ½°µ±±½Üµ=É¥¥¸ˆ°€ˆ¨ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰…¡”µ½¹ÑÉ½°ˆ°€‰¹¼µ…¡”ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€™½È¡¸¥¸€ ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°€‰½¹Ñ•¹ÐµI…¹”ˆ°€‰•ÁÐµI…¹•Ìˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€¡Ø€ôÉ•ÍÀ¹¡•…‘•ÉÌ¹•Ð¡¡¸¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¡Øè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È¡¡¸°¡Ø¤(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤(€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€Ý¡¥±”QÉÕ”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¡Õ¹¬€ôÉ•ÍÀ¹É•… ØÐ€¨€ÄÀÈÐ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¡Õ¹¬è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•…¬(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡¡Õ¹¬¤(€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡	É½­•¹A¥Á•ÉÉ½È°½¹¹•Ñ¥½¹I•Í•ÑÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½½¹™¥œˆè(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°±½…‘}½¹™¥œ ¤¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½…ÉÑÝ½É­}…¡”ˆè(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰‰åÑ•Ìˆè…ÉÑÝ½É­}…¡•}Í¥é” ¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Ñ•ÍÐˆè(€€€€€€€€€€€€€€€½¬°¥¹™¼€ôaÑÉ•…´¡±½…‘}½¹™¥œ ¤¤¹±½¥¸ ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆè½¬°€‰¥¹™¼ˆè¥¹™¼¥˜½¬•±Í”9½¹”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½Èˆè9½¹”¥˜½¬•±Í”¥¹™½ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½É•±½…ˆè(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€¥˜¹½ÐaÑÉ•…´¡™œ¤¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½Èˆè€‰9½Ð½¹™¥ÕÉ•‰ô¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€ °|€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ°™½É”õQÉÕ”¤(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰½Õ¹Ðˆè±•¸¡ ¥ô¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½…Ñ•½É¥•Ìˆè(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ñ•½É¥•Ìˆèmt°€‰±½•‘}¥¸ˆè…±Í•ô¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ñ•½É¥•Ìˆèmt°€‰±½•‘}¥¸ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤(€€€€€€€€€€€€€€€€Œ½Õ¹Ð¡…¹¹•±ÌÁ•È…Ñ•½Éä€¡½¹±ä…Ñ•½É¥•ÌÑ¡…Ð¡…Ù”¡…¹¹•±Ì¤(€€€€€€€€€€€€€€€½Õ¹ÑÌ€ôíô(€€€€€€€€€€€€€€€™½È ¥¸¡…¹¹•±Ìè(€€€€€€€€€€€€€€€€€€€¸€ô…ÑÌ¹•Ð¡¡l‰…Ñ•½Éå}¥‰t°€ˆˆ¤(€€€€€€€€€€€€€€€€€€€¥˜¸è(€€€€€€€€€€€€€€€€€€€€€€€½Õ¹ÑÍm¹t€ô½Õ¹ÑÌ¹•Ð¡¸°€À¤€¬€Ä(€€€€€€€€€€€€€€€½ÕÐ€ômì‰¹…µ”ˆè¬°€‰½Õ¹ÐˆèÙô™½È¬°Ø¥¸(€€€€€€€€€€€€€€€€€€€€€€Í½ÉÑ•¡½Õ¹ÑÌ¹¥Ñ•µÌ ¤°­•äõ±…µ‰‘„­Øè­ÙlÁt¹±½Ý•È ¤¥t(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ñ•½É¥•Ìˆè½ÕÐ°€‰±½•‘}¥¸ˆèQÉÕ•ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½¡…¹¹•±Ìˆè(€€€€€€€€€€€€€€€€ŒI…¹­•…Ñ…±½Õ”Í•…É €¬½ÁÑ¥½¹…°…Ñ•½Éä™¥±Ñ•È¸(€€€€€€€€€€€€€€€Ñ•É´€ô€¡Ä¹•Ð ‰Äˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€…Ñ}™¥±Ñ•È€ô€¡Ä¹•Ð ‰…Ðˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰¡…¹¹•±Ìˆèmt°€‰±½•‘}¥¸ˆè…±Í”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ½Ñ…°ˆè€Áô¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰¡…¹¹•±Ìˆèmt°€‰±½•‘}¥¸ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½ÈˆèÍÑÈ¡”¤°€‰Ñ½Ñ…°ˆè€Áô¤(€€€€€€€€€€€€€€€½ÕÐ€ômt(€€€€€€€€€€€€€€€™½È ¥¸¡…¹¹•±Ìè(€€€€€€€€€€€€€€€€€€€¹´€ô¡l‰¹…µ”‰t(€€€€€€€€€€€€€€€€€€€…Ñ¹…µ”€ô…ÑÌ¹•Ð¡¡l‰…Ñ•½Éå}¥‰t°€ˆˆ¤(€€€€€€€€€€€€€€€€€€€¥˜…Ñ}™¥±Ñ•È…¹…Ñ¹…µ”€„ô…Ñ}™¥±Ñ•Èè(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€Í•…É¡}É…¹¬€ô€¡}¡…¹¹•±}…Ñ…±½}Í•…É¡}É…¹¬¡¹´°…Ñ¹…µ”°Ñ•É´¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜Ñ•É´•±Í”9½¹”¤(€€€€€€€€€€€€€€€€€€€¥˜Ñ•É´…¹Í•…É¡}É…¹¬¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡ì(€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆè¹´°(€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè¡l‰ÍÑÉ•…µ}¥‰t°(€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ•½Éäˆè…Ñ¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€‰±½¼ˆè ¹•Ð ‰ÍÑÉ•…µ}¥½¸ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰ÅÕ…±¥ÑäˆèÅÕ…±¥Ñå}Ñ…œ¡¹´¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰ÕÉ°ˆèà¹ÍÑÉ•…µ}ÕÉ°¡¡l‰ÍÑÉ•…µ}¥‰t¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰}Í•…É¡}É…¹¬ˆèÍ•…É¡}É…¹¬°(€€€€€€€€€€€€€€€€€€€ô¤(€€€€€€€€€€€€€€€¥˜Ñ•É´è(€€€€€€€€€€€€€€€€€€€½ÕÐ¹Í½ÉÐ¡­•äõ±…µ‰‘„É½ÜèÉ½Ýl‰}Í•…É¡}É…¹¬‰t¤(€€€€€€€€€€€€€€€™½ÈÉ½Ü¥¸½ÕÐè(€€€€€€€€€€€€€€€€€€€É½Ü¹Á½À ‰}Í•…É¡}É…¹¬ˆ°9½¹”¤(€€€€€€€€€€€€€€€Ñ½Ñ…°€ô±•¸¡½ÕÐ¤(€€€€€€€€€€€€€€€…ÁÁ•€ô½ÕÑlèÔÀÁt(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰¡…¹¹•±Ìˆè…ÁÁ•°€‰±½•‘}¥¸ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ½Ñ…°ˆèÑ½Ñ…°°€‰Í¡½Ý¸ˆè±•¸¡…ÁÁ•¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½µ½Ù¥•}…Ñ…±½œˆè(€€€€€€€€€€€€€€€…Ñ…±½}¹…µ”€ô€¡Ä¹•Ð ‰…Ñ…±½œˆ°l‰Á½ÁÕ±…È‰t¥lÁt¤¹ÍÑÉ¥À ¤¹±½Ý•È ¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€±¥µ¥Ð€ôµ…à Ä°µ¥¸ ÌÀ°¥¹Ð¡Ä¹•Ð ‰±¥µ¥Ðˆ°lˆÄÀ‰t¥lÁt¤¤¤(€€€€€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€±¥µ¥Ð€ô€ÄÀ(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€…Ñ…±½œ€ô¥¹•µ•Ñ…}µ½Ù¥•}…Ñ…±½œ¡…Ñ…±½}¹…µ”°±¥µ¥Ð¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆèmt°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½Èˆè€‰5½Ù¥”…Ñ…±½œè€ˆ€¬ÍÑÈ¡”¥ô¤(€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}µ½Ù¥•Ì€ômt(€€€€€€€€€€€€€€€¥˜à¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}µ½Ù¥•Ì€ô•Ñ}áÑÉ•…µ}µ½Ù¥•Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}µ½Ù¥•Ì€ômt(€€€€€€€€€€€€€€€½ÕÐ€ômt(€€€€€€€€€€€€€€€™½Èµ•Ñ„¥¸…Ñ…±½œè(€€€€€€€€€€€€€€€€€€€¹…µ”€ôÍÑÈ¡µ•Ñ„¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¹…µ”è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€å•…È€ô}…Ñ…±½}å•…È¡µ•Ñ„¤(€€€€€€€€€€€€€€€€€€€Í½ÕÉ•Ì€ôµ…Ñ¡}Ù½‘}Í½ÕÉ•Ì¡ì‰¹…µ”ˆè¹…µ”°€‰å•…Èˆèå•…Éô°ÁÉ½Ù¥‘•É}µ½Ù¥•Ì¤(€€€€€€€€€€€€€€€€€€€™¥ÉÍÐ€ôÍ½ÕÉ•ÍlÁt¥˜Í½ÕÉ•Ì•±Í”íô(€€€€€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡ì‰…Ñ…±½}¥ˆèµ•Ñ„¹•Ð ‰¥ˆ¤½Èµ•Ñ„¹•Ð ‰¥µ‘‰}¥ˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè™¥ÉÍÐ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤°€‰¹…µ”ˆè¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•¹Í¥½¸ˆè™¥ÉÍÐ¹•Ð ‰•áÑ•¹Í¥½¸ˆ¤½È€‰µÀÐˆ°€‰å•…Èˆèå•…È°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É…Ñ¥¹œˆèµ•Ñ„¹•Ð ‰¥µ‘‰I…Ñ¥¹œˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆèµ•Ñ„¹•Ð ‰Á½ÍÑ•Èˆ¤½È€ˆˆ°€‰Í½ÕÉ•ÌˆèÍ½ÕÉ•Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}™½Õ¹ˆè‰½½°¡Í½ÕÉ•Ì¥ô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆè½ÕÐ°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ…±½œˆè…Ñ…±½}¹…µ•ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½µ½Ù¥•Ìˆè(€€€€€€€€€€€€€€€Ñ•É´€ô€¡Ä¹•Ð ‰Äˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•É´è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆèmt°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¥ô¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€…Ñ…±½œ€ô¥¹•µ•Ñ…}Í•…É  ‰µ½Ù¥”ˆ°Ñ•É´¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆèmt°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½Èˆè€‰5½Ù¥”…Ñ…±½œè€ˆ€¬ÍÑÈ¡”¥ô¤(€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}µ½Ù¥•Ì€ômt(€€€€€€€€€€€€€€€¥˜à¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}µ½Ù¥•Ì€ô•Ñ}áÑÉ•…µ}µ½Ù¥•Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}µ½Ù¥•Ì€ômt(€€€€€€€€€€€€€€€½ÕÐ€ômt(€€€€€€€€€€€€€€€™½Èµ•Ñ„¥¸…Ñ…±½œè(€€€€€€€€€€€€€€€€€€€¹…µ”€ôÍÑÈ¡µ•Ñ„¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¹…µ”è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€å•…È€ô}…Ñ…±½}å•…È¡µ•Ñ„¤(€€€€€€€€€€€€€€€€€€€Í½ÕÉ•Ì€ôµ…Ñ¡}Ù½‘}Í½ÕÉ•Ì¡ì‰¹…µ”ˆè¹…µ”°€‰å•…Èˆèå•…Éô°ÁÉ½Ù¥‘•É}µ½Ù¥•Ì¤(€€€€€€€€€€€€€€€€€€€™¥ÉÍÐ€ôÍ½ÕÉ•ÍlÁt¥˜Í½ÕÉ•Ì•±Í”íô(€€€€€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡ì(€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ…±½}¥ˆèµ•Ñ„¹•Ð ‰¥ˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè™¥ÉÍÐ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆè¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•¹Í¥½¸ˆè™¥ÉÍÐ¹•Ð ‰•áÑ•¹Í¥½¸ˆ¤½È€‰µÀÐˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰å•…Èˆèå•…È°(€€€€€€€€€€€€€€€€€€€€€€€€‰É…Ñ¥¹œˆèµ•Ñ„¹•Ð ‰¥µ‘‰I…Ñ¥¹œˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆèµ•Ñ„¹•Ð ‰Á½ÍÑ•Èˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•ÌˆèÍ½ÕÉ•Ì°(€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}™½Õ¹ˆè‰½½°¡Í½ÕÉ•Ì¤°(€€€€€€€€€€€€€€€€€€€ô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆè½ÕÐ°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½™…Ù½É¥Ñ•}µ½Ù¥•}ÍÑ…ÑÕÌˆè(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}µ½Ù¥•Ì€ô•Ñ}áÑÉ•…µ}µ½Ù¥•Ì¡™œ¤¥˜à¹½¹™¥ÕÉ• ¤•±Í”mt(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}µ½Ù¥•Ì€ômt(€€€€€€€€€€€€€€€½ÕÐ€ômt(€€€€€€€€€€€€€€€™½Èµ½Ù¥”¥¸±½…‘}™…Ù½É¥Ñ•Ì ¤¹•Ð ‰µ½Ù¥•Ìˆ°mt¤è(€€€€€€€€€€€€€€€€€€€É½Ü€ô‘¥Ð¡µ½Ù¥”¤(€€€€€€€€€€€€€€€€€€€Í½ÕÉ•Ì€ôµ…Ñ¡}Ù½‘}Í½ÕÉ•Ì¡É½Ü°ÁÉ½Ù¥‘•É}µ½Ù¥•Ì¤(€€€€€€€€€€€€€€€€€€€É½Ýl‰Í½ÕÉ•Ì‰t€ôÍ½ÕÉ•Ì(€€€€€€€€€€€€€€€€€€€É½Ýl‰ÍÑÉ•…µ}™½Õ¹‰t€ô‰½½°¡Í½ÕÉ•Ì¤(€€€€€€€€€€€€€€€€€€€¥˜Í½ÕÉ•Ìè(€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰ÍÑÉ•…µ}¥‰t€ôÍ½ÕÉ•ÍlÁt¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰•áÑ•¹Í¥½¸‰t€ôÍ½ÕÉ•ÍlÁt¹•Ð ‰•áÑ•¹Í¥½¸ˆ¤½È€‰µÀÐˆ(€€€€€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡É½Ü¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆè½ÕÐ°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½É••¹Ñ}µ½Ù¥•Ìˆè(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€±¥µ¥Ð€ôµ…à Ä°µ¥¸ ÌØ°¥¹Ð¡Ä¹•Ð ‰±¥µ¥Ðˆ°lˆä‰t¥lÁt¤¤¤(€€€€€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€±¥µ¥Ð€ô€ä(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆèmt°€‰±½•‘}¥¸ˆè…±Í•ô¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€µ½Ù¥•Ì€ô•Ñ}áÑÉ•…µ}µ½Ù¥•Ì¡™œ¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆèmt°€‰±½•‘}¥¸ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤(€€€€€€€€€€€€€€€Ñ¡¥Í}å•…È€ôÑ¥µ”¹±½…±Ñ¥µ” ¤¹Ñµ}å•…È(€€€€€€€€€€€€€€€‰å}å•…È€ôíô(€€€€€€€€€€€€€€€…±±}É½ÝÌ€ômt(€€€€€€€€€€€€€€€™½È´¥¸µ½Ù¥•Ìè(€€€€€€€€€€€€€€€€€€€É…Ý}å•…È€ô€ˆ€ˆ¹©½¥¸¡ÍÑÈ¡Ù…±Õ”½È€ˆˆ¤™½ÈÙ…±Õ”¥¸(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¡´¹•Ð ‰å•…Èˆ¤°´¹•Ð ‰É•±•…Í•…Ñ”ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€´¹•Ð ‰É•±•…Í•}‘…Ñ”ˆ¤°´¹•Ð ‰¹…µ”ˆ¤¤¤(€€€€€€€€€€€€€€€€€€€µ…Ñ €ôÉ”¹Í•…É ¡Èˆ üèÄåðÈÀ¥q‘ìÉôˆ°É…Ý}å•…È¤(€€€€€€€€€€€€€€€€€€€å•…È€ô¥¹Ð¡µ…Ñ ¹É½ÕÀ À¤¤¥˜µ…Ñ •±Í”€À(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€…‘‘•€ô¥¹Ð¡™±½…Ð¡´¹•Ð ‰…‘‘•ˆ¤½È€À¤¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€…‘‘•€ô€À(€€€€€€€€€€€€€€€€€€€É½Ü€ô€¡…‘‘•°´°å•…È¤(€€€€€€€€€€€€€€€€€€€…±±}É½ÝÌ¹…ÁÁ•¹¡É½Ü¤(€€€€€€€€€€€€€€€€€€€¥˜å•…Èè(€€€€€€€€€€€€€€€€€€€€€€€‰å}å•…È¹Í•Ñ‘•™…Õ±Ð¡å•…È°mt¤¹…ÁÁ•¹¡É½Ü¤(€€€€€€€€€€€€€€€™½ÈÉ½ÝÌ¥¸‰å}å•…È¹Ù…±Õ•Ì ¤è(€€€€€€€€€€€€€€€€€€€É½ÝÌ¹Í½ÉÐ¡­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÁt°É•Ù•ÉÍ”õQÉÕ”¤(€€€€€€€€€€€€€€€ÕÍ…‰±•}å•…ÉÌ€ômå•…È™½Èå•…È¥¸‰å}å•…È¥˜å•…È€ðôÑ¡¥Í}å•…È€¬€Åt(€€€€€€€€€€€€€€€Ñ…É•Ñ}å•…È€ôÑ¡¥Í}å•…È¥˜Ñ¡¥Í}å•…È¥¸‰å}å•…È•±Í”€ (€€€€€€€€€€€€€€€€€€€µ…à¡ÕÍ…‰±•}å•…ÉÌ¤¥˜ÕÍ…‰±•}å•…ÉÌ•±Í”€À¤(€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•}É½ÝÌ€ô±¥ÍÐ¡‰å}å•…È¹•Ð¡Ñ…É•Ñ}å•…È°mt¤¤(€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•}É½ÝÌ€¬ô‰å}å•…È¹•Ð¡Ñ…É•Ñ}å•…È€´€Ä°mt¤(€€€€€€€€€€€€€€€¥˜¹½Ð…¹‘¥‘…Ñ•}É½ÝÌè(€€€€€€€€€€€€€€€€€€€…±±}É½ÝÌ¹Í½ÉÐ¡­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•µlÁt°É•Ù•ÉÍ”õQÉÕ”¤(€€€€€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•}É½ÝÌ€ô…±±}É½ÝÌ(€€€€€€€€€€€€€€€Õ¹¥ÅÕ•}É½ÝÌ€ômt(€€€€€€€€€€€€€€€Í••¹}Ñ¥Ñ±•Ì€ôÍ•Ð ¤(€€€€€€€€€€€€€€€™½ÈÉ½Ü¥¸…¹‘¥‘…Ñ•}É½ÝÌè(€€€€€€€€€€€€€€€€€€€±•…¹}Ñ¥Ñ±”€ô}±•…¹}Í¡½Ý}Ñ¥Ñ±”¡É½ÝlÅt¹•Ð ‰¹…µ”ˆ¤¤½ÈÍÑÈ (€€€€€€€€€€€€€€€€€€€€€€€É½ÝlÅt¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤(€€€€€€€€€€€€€€€€€€€Ñ¥Ñ±•}­•ä€ôÉ”¹ÍÕˆ¡È‰my„µèÀ´åt¬ˆ°€ˆˆ°±•…¹}Ñ¥Ñ±”¹±½Ý•È ¤¤(€€€€€€€€€€€€€€€€€€€¥˜¹½ÐÑ¥Ñ±•}­•ä½ÈÑ¥Ñ±•}­•ä¥¸Í••¹}Ñ¥Ñ±•Ìè(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€Í••¹}Ñ¥Ñ±•Ì¹…‘¡Ñ¥Ñ±•}­•ä¤(€€€€€€€€€€€€€€€€€€€Õ¹¥ÅÕ•}É½ÝÌ¹…ÁÁ•¹¡É½Ü¤(€€€€€€€€€€€€€€€¡½Í•¸€ôÕ¹¥ÅÕ•}É½ÝÍlé±¥µ¥Ñt(€€€€€€€€€€€€€€€½ÕÐ€ômt(€€€€€€€€€€€€€€€™½È}…‘‘•°´°å•…È¥¸¡½Í•¸è(€€€€€€€€€€€€€€€€€€€½Ù•È€ôÍÑÈ¡´¹•Ð ‰ÍÑÉ•…µ}¥½¸ˆ¤½È´¹•Ð ‰½Ù•Èˆ¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€´¹•Ð ‰µ½Ù¥•}¥µ…”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€¥˜¹½Ð½Ù•È¹ÍÑ…ÉÑÍÝ¥Ñ   ‰¡ÑÑÀè¼¼ˆ°€‰¡ÑÑÁÌè¼¼ˆ¤¤è(€€€€€€€€€€€€€€€€€€€€€€€½Ù•È€ô€ˆˆ(€€€€€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡ì‰ÍÑÉ•…µ}¥ˆè´¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆèÍÑÈ¡´¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•¹Í¥½¸ˆè´¹•Ð ‰½¹Ñ…¥¹•É}•áÑ•¹Í¥½¸ˆ¤½È€‰µÀÐˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰å•…Èˆèå•…È°€‰É…Ñ¥¹œˆè´¹•Ð ‰É…Ñ¥¹œˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆè½Ù•Éô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰µ½Ù¥•Ìˆè½ÕÐ°€‰±½•‘}¥¸ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ…±½}å•…ÈˆèÑ…É•Ñ}å•…È°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¡…Í}µ½É”ˆè±•¸¡Õ¹¥ÅÕ•}É½ÝÌ¤€ø±¥µ¥Ñô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Í¡½ÝÌˆè(€€€€€€€€€€€€€€€Ñ•É´€ô€¡Ä¹•Ð ‰Äˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•É´è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰Í¡½ÝÌˆèmt°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¥ô¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€…Ñ…±½œ€ô¥¹•µ•Ñ…}Í•…É  ‰Í•É¥•Ìˆ°Ñ•É´¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰Í¡½ÝÌˆèmt°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½Èˆè€‰M¡½Ü…Ñ…±½œè€ˆ€¬ÍÑÈ¡”¥ô¤(€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•È€ômt(€€€€€€€€€€€€€€€¥˜à¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•È€ô•Ñ}áÑÉ•…µ}Í•É¥•Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•È€ômt(€€€€€€€€€€€€€€€½ÕÐ€ômt(€€€€€€€€€€€€€€€™½Èµ•Ñ„¥¸…Ñ…±½œè(€€€€€€€€€€€€€€€€€€€¹…µ”€ôÍÑÈ¡µ•Ñ„¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¹…µ”è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€­•ä€ô}Í¡½Ý}­•ä¡¹…µ”¤(€€€€€€€€€€€€€€€€€€€Í¥‰±¥¹Ì€ômÉ½Ü™½ÈÉ½Ü¥¸ÁÉ½Ù¥‘•È¥˜}Í¡½Ý}­•ä¡É½Ü¹•Ð ‰¹…µ”ˆ¤¤€ôô­•åt(€€€€€€€€€€€€€€€€€€€¥‘Ì€ômÉ½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¤™½ÈÉ½Ü¥¸Í¥‰±¥¹Ì¥˜É½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¤¥Ì¹½Ð9½¹•t(€€€€€€€€€€€€€€€€€€€å•…È€ô}…Ñ…±½}å•…È¡µ•Ñ„¤(€€€€€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡ì‰…Ñ…±½}¥ˆèµ•Ñ„¹•Ð ‰¥ˆ¤½È€ˆˆ°€‰Í¡½Ý}­•äˆè­•ä°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥ˆè¥‘ÍlÁt¥˜¥‘Ì•±Í”9½¹”°€‰Í•É¥•Í}¥‘Ìˆè¥‘Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÁÉ½Ù¥‘•É}™½Õ¹ˆè‰½½°¡¥‘Ì¤°€‰¹…µ”ˆè¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆèµ•Ñ„¹•Ð ‰Á½ÍÑ•Èˆ¤½È€ˆˆ°€‰å•…Èˆèå•…È°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É…Ñ¥¹œˆèµ•Ñ„¹•Ð ‰¥µ‘‰I…Ñ¥¹œˆ¤½È€ˆ‰ô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰Í¡½ÝÌˆè½ÕÐ°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½±…Ñ•ÍÑ}•Á¥Í½‘•Ìˆè(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€±¥µ¥Ð€ôµ…à Ä°µ¥¸ ÌØ°¥¹Ð¡Ä¹•Ð ‰±¥µ¥Ðˆ°lˆä‰t¥lÁt¤¤¤(€€€€€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€±¥µ¥Ð€ô€ä(€€€€€€€€€€€€€€€É•™É•Í¡}•áÑ•É¹…°€ôÄ¹•Ð ‰É•™É•Í ˆ°lˆÀ‰t¥lÁt€ôô€ˆÄˆ(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÉ•™É•Í¡}•áÑ•É¹…°è(€€€€€€€€€€€€€€€€€€€…¡•€ô}±½…‘}±…Ñ•ÍÑ}•Á¥Í½‘•Í}…¡”¡à¤(€€€€€€€€€€€€€€€€€€€¥˜…¡•¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€…¡•‘}É½ÝÌ€ô…¡•¹•Ð ‰•Á¥Í½‘•Ìˆ¤½Èmt(€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•Ìˆè…¡•‘}É½ÝÍlé±¥µ¥Ñt°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¡…Í}µ½É”ˆè±•¸¡…¡•‘}É½ÝÌ¤€ø±¥µ¥Ð°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÕÁ½µ¥¹œˆè€¡…¡•¹•Ð ‰ÕÁ½µ¥¹œˆ¤½Èmt¥lèÌÙt°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½ÉÌˆè¥¹Ð¡…¡•¹•Ð ‰•ÉÉ½ÉÌˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…¡•ˆèQÉÕ•ô¤(€€€€€€€€€€€€€€€É½ÝÌ€ômt(€€€€€€€€€€€€€€€ÕÁ½µ¥¹}É½ÝÌ€ômt(€€€€€€€€€€€€€€€•ÉÉ½ÉÌ€ô€À(€€€€€€€€€€€€€€€¥˜à¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€Í•É¥•Í}…Ñ…±½œ€ô•Ñ}áÑÉ•…µ}Í•É¥•Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€Í•É¥•Í}…Ñ…±½œ€ômt(€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€Í•É¥•Í}…Ñ…±½œ€ômt(€€€€€€€€€€€€€€€™½È™…Ù}Í¡½Ü¥¸±½…‘}™…Ù½É¥Ñ•Ì ¤¹•Ð ‰Í¡½ÝÌˆ°mt¤è(€€€€€€€€€€€€€€€€€€€Í•É¥•Í}¥€ô™…Ù}Í¡½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¤(€€€€€€€€€€€€€€€€€€€™…Ù½É¥Ñ•}­•ä€ôÍÑÈ¡™…Ù}Í¡½Ü¹•Ð ‰Í¡½Ý}­•äˆ¤½È}Í¡½Ý}­•ä¡™…Ù}Í¡½Ü¹•Ð ‰¹…µ”ˆ¤¤¤(€€€€€€€€€€€€€€€€€€€Í¥‰±¥¹Ì€ômÉ½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¤™½ÈÉ½Ü¥¸Í•É¥•Í}…Ñ…±½œ(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜}Í¡½Ý}­•ä¡É½Ü¹•Ð ‰¹…µ”ˆ¤¤€ôô™…Ù½É¥Ñ•}­•åt(€€€€€€€€€€€€€€€€€€€Í¥‰±¥¹Ì€ômÍ¥™½ÈÍ¥¥¸Í¥‰±¥¹Ì¥˜Í¥¥Ì¹½Ð9½¹•t(€€€€€€€€€€€€€€€€€€€¥˜Í¥‰±¥¹Ìè(€€€€€€€€€€€€€€€€€€€€€€€Í•É¥•Í}¥€ôÍ¥‰±¥¹ÍlÁt(€€€€€€€€€€€€€€€€€€€¥˜Í•É¥•Í}¥¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í¡½Ý}¹…µ”€ôÍÑÈ¡™…Ù}Í¡½Ü¹•Ð ‰¹…µ”ˆ¤½È€‰M¡½Üˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘¥ÍÁ±…å}Í¡½Ý}¹…µ”€ô}±•…¹}Í¡½Ý}Ñ¥Ñ±”¡Í¡½Ý}¹…µ”¤½ÈÍ¡½Ý}¹…µ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€å•…É}µ…Ñ €ôÉ”¹Í•…É ¡Èˆ üèÄåðÈÀ¥q‘ìÉôˆ°ÍÑÈ¡™…Ù}Í¡½Ü¹•Ð ‰å•…Èˆ¤½È€ˆˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í¡½Ý}å•…È€ô¥¹Ð¡å•…É}µ…Ñ ¹É½ÕÀ À¤¤¥˜å•…É}µ…Ñ •±Í”€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í¡•‘Õ±”€ô}ÑÙµ…é•}•Á¥Í½‘•}Í¡•‘Õ±”¡Í¡½Ý}¹…µ”°Í¡½Ý}å•…È°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€™½É”õÉ•™É•Í¡}•áÑ•É¹…°¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€½Ù•È€ôÍÑÈ¡™…Ù}Í¡½Ü¹•Ð ‰½Ù•Èˆ¤½È€ˆˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…°€ôÍ¡•‘Õ±”¹•Ð ‰±…Ñ•ÍÐˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹œ€ôÍ¡•‘Õ±”¹•Ð ‰ÕÁ½µ¥¹œˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÕÁ½µ¥¹œè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÑÌ€ô€¡‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡ÕÁ½µ¥¹l‰…¥ÉÍÑ…µÀ‰t¹É•Á±…” ‰hˆ°€ˆ¬ÀÀèÀÀˆ¤¤¹Ñ¥µ•ÍÑ…µÀ ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÕÁ½µ¥¹œ¹•Ð ‰…¥ÉÍÑ…µÀˆ¤•±Í”Ñ¥µ”¹µ­Ñ¥µ”¡Ñ¥µ”¹ÍÑÉÁÑ¥µ”¡ÕÁ½µ¥¹œ¹•Ð ‰…¥É‘…Ñ”ˆ¤½È€ˆˆ°€ˆ•d´•´´•ˆ¤¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡Y…±Õ•ÉÉ½È°=Ù•É™±½ÝÉÉ½È°QåÁ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹}É½ÝÌ¹…ÁÁ•¹¡ì‰Í¡½Ý}¹…µ”ˆè‘¥ÍÁ±…å}Í¡½Ý}¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥ˆè€ˆˆ°€‰…Ñ…±½}¥ˆè™…Ù}Í¡½Ü¹•Ð ‰…Ñ…±½}¥ˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆè½Ù•È°€‰Í•…Í½¸ˆè¥¹Ð¡ÕÁ½µ¥¹œ¹•Ð ‰Í•…Í½¸ˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•}¹Õ´ˆè¥¹Ð¡ÕÁ½µ¥¹œ¹•Ð ‰•Á¥Í½‘•}¹Õ´ˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆèÕÁ½µ¥¹œ¹•Ð ‰Ñ¥Ñ±”ˆ¤½È€‰Á¥Í½‘”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…¥É‘…Ñ”ˆèÕÁ½µ¥¹œ¹•Ð ‰…¥É‘…Ñ”ˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…¥ÉÍÑ…µÀˆèÕÁ½µ¥¹œ¹•Ð ‰…¥ÉÍÑ…µÀˆ¤½È€ˆˆ°€‰…¥É}ÑÌˆèÕÑÍô¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜•áÑ•É¹…°è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•ÑÌ€ô€¡‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡•áÑ•É¹…±l‰…¥ÉÍÑ…µÀ‰t¹É•Á±…” ‰hˆ°€ˆ¬ÀÀèÀÀˆ¤¤¹Ñ¥µ•ÍÑ…µÀ ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜•áÑ•É¹…°¹•Ð ‰…¥ÉÍÑ…µÀˆ¤•±Í”Ñ¥µ”¹µ­Ñ¥µ”¡Ñ¥µ”¹ÍÑÉÁÑ¥µ”¡•áÑ•É¹…°¹•Ð ‰…¥É‘…Ñ”ˆ¤½È€ˆˆ°€ˆ•d´•´´•ˆ¤¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡Y…±Õ•ÉÉ½È°=Ù•É™±½ÝÉÉ½È°QåÁ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•ÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜•ÑÌ€øôÑ¥µ”¹Ñ¥µ” ¤€´€ ÌÀ€¨€ÈÐ€¨€ÌØÀÀ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É½ÝÌ¹…ÁÁ•¹¡ì‰¥ˆè9½¹”°€‰Í¡½Ý}¹…µ”ˆè‘¥ÍÁ±…å}Í¡½Ý}¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥ˆè€ˆˆ°€‰…Ñ…±½}¥ˆè™…Ù}Í¡½Ü¹•Ð ‰…Ñ…±½}¥ˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆè½Ù•È°€‰Í•…Í½¸ˆè¥¹Ð¡•áÑ•É¹…°¹•Ð ‰Í•…Í½¸ˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•}¹Õ´ˆè¥¹Ð¡•áÑ•É¹…°¹•Ð ‰•Á¥Í½‘•}¹Õ´ˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè•áÑ•É¹…°¹•Ð ‰Ñ¥Ñ±”ˆ¤½È€‰Á¥Í½‘”ˆ°€‰•áÑ•¹Í¥½¸ˆè€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…‘‘•ˆè•ÑÌ°€‰…¥É}ÑÌˆè•ÑÌ°€‰…Ù…¥±…‰±”ˆè…±Í•ô¤(€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ€¬ô€Ä(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ„€ôà¹Í•É¥•Í}¥¹™¼¡Í•É¥•Í}¥¤½Èíô(€€€€€€€€€€€€€€€€€€€€€€€¥¹™¼€ô‘…Ñ„¹•Ð ‰¥¹™¼ˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡¥¹™¼°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥¹™¼€ôíô(€€€€€€€€€€€€€€€€€€€€€€€Í¡½Ý}¹…µ”€ôÍÑÈ¡¥¹™¼¹•Ð ‰¹…µ”ˆ¤½È¥¹™¼¹•Ð ‰Ñ¥Ñ±”ˆ¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€™…Ù}Í¡½Ü¹•Ð ‰¹…µ”ˆ¤½È€‰M¡½Üˆ¤(€€€€€€€€€€€€€€€€€€€€€€€‘¥ÍÁ±…å}Í¡½Ý}¹…µ”€ô}±•…¹}Í¡½Ý}Ñ¥Ñ±”¡Í¡½Ý}¹…µ”¤½ÈÍ¡½Ý}¹…µ”(€€€€€€€€€€€€€€€€€€€€€€€å•…É}Ñ•áÐ€ô€ˆ€ˆ¹©½¥¸¡ÍÑÈ¡Ù…±Õ”½È€ˆˆ¤™½ÈÙ…±Õ”¥¸(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¡™…Ù}Í¡½Ü¹•Ð ‰å•…Èˆ¤°¥¹™¼¹•Ð ‰É•±•…Í•…Ñ”ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥¹™¼¹•Ð ‰É•±•…Í•}‘…Ñ”ˆ¤°Í¡½Ý}¹…µ”¤¤(€€€€€€€€€€€€€€€€€€€€€€€å•…É}µ…Ñ €ôÉ”¹Í•…É ¡Èˆ üèÄåðÈÀ¥q‘ìÉôˆ°å•…É}Ñ•áÐ¤(€€€€€€€€€€€€€€€€€€€€€€€Í¡½Ý}å•…È€ô¥¹Ð¡å•…É}µ…Ñ ¹É½ÕÀ À¤¤¥˜å•…É}µ…Ñ •±Í”€À(€€€€€€€€€€€€€€€€€€€€€€€É…Ý}•Á¥Í½‘•Ì€ô‘…Ñ„¹•Ð ‰•Á¥Í½‘•Ìˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡É…Ý}•Á¥Í½‘•Ì°±¥ÍÐ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½ÕÁ•€ôíô(€€€€€€€€€€€€€€€€€€€€€€€€€€€™½È•À¥¸É…Ý}•Á¥Í½‘•Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É½ÕÁ•¹Í•Ñ‘•™…Õ±Ð¡ÍÑÈ¡•À¹•Ð ‰Í•…Í½¸ˆ¤½È€Ä¤°mt¤¹…ÁÁ•¹¡•À¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€É…Ý}•Á¥Í½‘•Ì€ôÉ½ÕÁ•(€€€€€€€€€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•Ì€ômt(€€€€€€€€€€€€€€€€€€€€€€€™½ÈÍ•…Í½¹}­•ä°•Á¥Í½‘•Ì¥¸É…Ý}•Á¥Í½‘•Ì¹¥Ñ•µÌ ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡•Á¥Í½‘•Ì°±¥ÍÐ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•…Í½¹}¹Õ´€ô¥¹Ð¡Í•…Í½¹}­•ä¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•…Í½¹}¹Õ´€ô€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€™½È¥¹‘•à°•Á¥Í½‘”¥¸•¹Õµ•É…Ñ”¡•Á¥Í½‘•Ì°€Ä¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}¹Õ´€ô¥¹Ð¡•Á¥Í½‘”¹•Ð ‰•Á¥Í½‘•}¹Õ´ˆ¤½È¥¹‘•à¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}¹Õ´€ô¥¹‘•à(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•Ì¹…ÁÁ•¹ ¡Í•…Í½¹}¹Õ´°•Á¥Í½‘•}¹Õ´°•Á¥Í½‘”¤¤(€€€€€€€€€€€€€€€€€€€€€€€½Ù•È€ôÍÑÈ¡™…Ù}Í¡½Ü¹•Ð ‰½Ù•Èˆ¤½È¥¹™¼¹•Ð ‰½Ù•Èˆ¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥¹™¼¹•Ð ‰µ½Ù¥•}¥µ…”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð½Ù•È¹ÍÑ…ÉÑÍÝ¥Ñ   ‰¡ÑÑÀè¼¼ˆ°€‰¡ÑÑÁÌè¼¼ˆ¤¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€½Ù•È€ô€ˆˆ(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}É½Ü€ô9½¹”(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}­•ä€ô€ ´Ä°€´Ä¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜…¹‘¥‘…Ñ•Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•…Í½¹}¹Õ´°•Á¥Í½‘•}¹Õ´°•Á¥Í½‘”€ôµ…à (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•Ì°­•äõ±…µ‰‘„¥Ñ•´è€¡¥Ñ•µlÁt°¥Ñ•µlÅt¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}­•ä€ô€¡Í•…Í½¹}¹Õ´°•Á¥Í½‘•}¹Õ´¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…‘‘•€ô¥¹Ð¡™±½…Ð¡•Á¥Í½‘”¹•Ð ‰…‘‘•ˆ¤½È€À¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…‘‘•€ô€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}¥¹™¼€ô•Á¥Í½‘”¹•Ð ‰¥¹™¼ˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡•Á¥Í½‘•}¥¹™¼°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}¥¹™¼€ôíô(€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}‘…Ñ”€ô€ˆ€ˆ¹©½¥¸¡ÍÑÈ¡Ù…±Õ”½È€ˆˆ¤™½ÈÙ…±Õ”¥¸(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¡•Á¥Í½‘•}¥¹™¼¹•Ð ‰É•±•…Í•…Ñ”ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}¥¹™¼¹•Ð ‰É•±•…Í•‘…Ñ”ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}¥¹™¼¹•Ð ‰…¥É}‘…Ñ”ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘”¹•Ð ‰É•±•…Í•…Ñ”ˆ¤¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}ÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ•}µ…Ñ €ôÉ”¹Í•…É ¡Èˆ üèÄåðÈÀ¥q‘ìÉôµq‘ìÉôµq‘ìÉôˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}‘…Ñ”¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜‘…Ñ•}µ…Ñ è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}ÑÌ€ôÑ¥µ”¹µ­Ñ¥µ”¡Ñ¥µ”¹ÍÑÉÁÑ¥µ” (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ•}µ…Ñ ¹É½ÕÀ À¤°€ˆ•d´•´´•ˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡Y…±Õ•ÉÉ½È°=Ù•É™±½ÝÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}ÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð•Á¥Í½‘•}ÑÌ…¹…‘‘•€ø€ÄÀÀÀÀÀÀÀÀè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}ÑÌ€ô…‘‘•(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜•Á¥Í½‘”¹•Ð ‰¥ˆ¤¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}É½Ü€ôì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¥ˆè•Á¥Í½‘”¹•Ð ‰¥ˆ¤°€‰Í¡½Ý}¹…µ”ˆèÍ¡½Ý}¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥ˆèÍ•É¥•Í}¥°€‰½Ù•Èˆè½Ù•È°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•…Í½¸ˆèÍ•…Í½¹}¹Õ´°€‰•Á¥Í½‘•}¹Õ´ˆè•Á¥Í½‘•}¹Õ´°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè}±•…¹}•Á¥Í½‘•}Ñ¥Ñ±” (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘”¹•Ð ‰Ñ¥Ñ±”ˆ¤½È˜‰Á¥Í½‘”í•Á¥Í½‘•}¹Õµôˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Í¡½Ý}¹…µ”¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•¹Í¥½¸ˆè•Á¥Í½‘”¹•Ð ‰½¹Ñ…¥¹•É}•áÑ•¹Í¥½¸ˆ¤½È€‰µÀÐˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…‘‘•ˆè•Á¥Í½‘•}ÑÌ°€‰…Ù…¥±…‰±”ˆèQÉÕ•ô(€€€€€€€€€€€€€€€€€€€€€€€€Œ5•É”…±°ÅÕ…±¥Ñä½ÁÉ½Ù¥‘•ÈÙ…É¥…¹ÑÌ¥¹Ñ¼Ñ¡¥Ì½¹”±…Ñ•ÍÐ…É¸(€€€€€€€€€€€€€€€€€€€€€€€Í•É¥•Í}¥‘Ì€ô™…Ù}Í¡½Ü¹•Ð ‰Í•É¥•Í}¥‘Ìˆ¤½ÈmÍ•É¥•Í}¥‘t(€€€€€€€€€€€€€€€€€€€€€€€¥˜Í¥‰±¥¹Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•É¥•Í}¥‘Ì€ôÍ¥‰±¥¹Ì(€€€€€€€€€€€€€€€€€€€€€€€Ù…É¥…¹Ñ}É½ÝÌ€ômt(€€€€€€€€€€€€€€€€€€€€€€€™½ÈÙ…É¥…¹Ñ}¥¥¸Í•É¥•Í}¥‘Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ù…É¥…¹Ñ}É½Ü°}Ù…É¥…¹Ñ}¥¹™¼€ô}±…Ñ•ÍÑ}ÁÉ½Ù¥‘•É}Ù…É¥…¹Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€à°Ù…É¥…¹Ñ}¥°Í¡½Ý}¹…µ”¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜Ù…É¥…¹Ñ}É½Ü…¹Ù…É¥…¹Ñ}É½Ü¹•Ð ‰¥ˆ¤¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ù…É¥…¹Ñ}É½ÝÌ¹…ÁÁ•¹¡Ù…É¥…¹Ñ}É½Ü¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€¥˜Ù…É¥…¹Ñ}É½ÝÌè(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}­•ä€ôµ…à¡É½Ýl‰­•ä‰t™½ÈÉ½Ü¥¸Ù…É¥…¹Ñ}É½ÝÌ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ…Ñ¡¥¹œ€ômÉ½Ü™½ÈÉ½Ü¥¸Ù…É¥…¹Ñ}É½ÝÌ(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜É½Ýl‰­•ä‰t€ôôÁÉ½Ù¥‘•É}­•åt(€€€€€€€€€€€€€€€€€€€€€€€€€€€™¥ÉÍÐ€ôµ…Ñ¡¥¹lÁt(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}É½Ü€ôì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¥ˆè™¥ÉÍÑl‰¥‰t°€‰Í¡½Ý}¹…µ”ˆè}±•…¹}Í¡½Ý}Ñ¥Ñ±”¡Í¡½Ý}¹…µ”¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥ˆèÍ•É¥•Í}¥°€‰½Ù•Èˆè½Ù•È°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•…Í½¸ˆè™¥ÉÍÑl‰Í•…Í½¸‰t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•}¹Õ´ˆè™¥ÉÍÑl‰•Á¥Í½‘•}¹Õ´‰t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè™¥ÉÍÑl‰Ñ¥Ñ±”‰t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•¹Í¥½¸ˆè™¥ÉÍÑl‰•áÑ•¹Í¥½¸‰t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•Ìˆèmì‰¥ˆèÉ½Ýl‰¥‰t°€‰•áÑ•¹Í¥½¸ˆèÉ½Ýl‰•áÑ•¹Í¥½¸‰t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±…‰•°ˆèÉ½Ýl‰±…‰•°‰uô™½ÈÉ½Ü¥¸µ…Ñ¡¥¹t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…‘‘•ˆèµ…à¡É½Ýl‰…‘‘•‰t™½ÈÉ½Ü¥¸µ…Ñ¡¥¹œ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ù…¥±…‰±”ˆèQÉÕ•ô(€€€€€€€€€€€€€€€€€€€€€€€Í¡•‘Õ±”€ô}ÑÙµ…é•}•Á¥Í½‘•}Í¡•‘Õ±” (€€€€€€€€€€€€€€€€€€€€€€€€€€€Í¡½Ý}¹…µ”°Í¡½Ý}å•…È°™½É”õÉ•™É•Í¡}•áÑ•É¹…°¤(€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…°€ôÍ¡•‘Õ±”¹•Ð ‰±…Ñ•ÍÐˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹œ€ôÍ¡•‘Õ±”¹•Ð ‰ÕÁ½µ¥¹œˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€€€€€¥˜ÕÁ½µ¥¹œè(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹}ÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÕÁ½µ¥¹œ¹•Ð ‰…¥ÉÍÑ…µÀˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹}ÑÌ€ô‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹l‰…¥ÉÍÑ…µÀ‰t¹É•Á±…” ‰hˆ°€ˆ¬ÀÀèÀÀˆ¤¤¹Ñ¥µ•ÍÑ…µÀ ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹}ÑÌ€ôÑ¥µ”¹µ­Ñ¥µ”¡Ñ¥µ”¹ÍÑÉÁÑ¥µ” (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹œ¹•Ð ‰…¥É‘…Ñ”ˆ¤½È€ˆˆ°€ˆ•d´•´´•ˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡Y…±Õ•ÉÉ½È°=Ù•É™±½ÝÉÉ½È°QåÁ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹}ÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÁ½µ¥¹}É½ÝÌ¹…ÁÁ•¹¡ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í¡½Ý}¹…µ”ˆè‘¥ÍÁ±…å}Í¡½Ý}¹…µ”°€‰Í•É¥•Í}¥ˆèÍ•É¥•Í}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆè½Ù•È°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•…Í½¸ˆè¥¹Ð¡ÕÁ½µ¥¹œ¹•Ð ‰Í•…Í½¸ˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•}¹Õ´ˆè¥¹Ð¡ÕÁ½µ¥¹œ¹•Ð ‰•Á¥Í½‘•}¹Õ´ˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆèÕÁ½µ¥¹œ¹•Ð ‰Ñ¥Ñ±”ˆ¤½È€‰Á¥Í½‘”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…¥É‘…Ñ”ˆèÕÁ½µ¥¹œ¹•Ð ‰…¥É‘…Ñ”ˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…¥ÉÍÑ…µÀˆèÕÁ½µ¥¹œ¹•Ð ‰…¥ÉÍÑ…µÀˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…¥É}ÑÌˆèÕÁ½µ¥¹}ÑÍô¤(€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±}­•ä€ô€¡¥¹Ð¡•áÑ•É¹…°¹•Ð ‰Í•…Í½¸ˆ¤½È€´Ä¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥¹Ð¡•áÑ•É¹…°¹•Ð ‰•Á¥Í½‘•}¹Õ´ˆ¤½È€´Ä¤¤(€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±}ÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€¥˜•áÑ•É¹…°¹•Ð ‰…¥ÉÍÑ…µÀˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±}ÑÌ€ô‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±l‰…¥ÉÍÑ…µÀ‰t¹É•Á±…” ‰hˆ°€ˆ¬ÀÀèÀÀˆ¤¤¹Ñ¥µ•ÍÑ…µÀ ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡Y…±Õ•ÉÉ½È°=Ù•É™±½ÝÉÉ½È°QåÁ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±}ÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€•±¥˜•áÑ•É¹…°¹•Ð ‰…¥É‘…Ñ”ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±}ÑÌ€ôÑ¥µ”¹µ­Ñ¥µ”¡Ñ¥µ”¹ÍÑÉÁÑ¥µ” (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±l‰…¥É‘…Ñ”‰t°€ˆ•d´•´´•ˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ€¡Y…±Õ•ÉÉ½È°=Ù•É™±½ÝÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±}ÑÌ€ô€À(€€€€€€€€€€€€€€€€€€€€€€€¥˜ÁÉ½Ù¥‘•É}É½Ü…¹ÁÉ½Ù¥‘•É}­•ä€ôô•áÑ•É¹…±}­•ä…¹•áÑ•É¹…±}ÑÌè(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}É½Ýl‰…¥É}ÑÌ‰t€ô•áÑ•É¹…±}ÑÌ(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½ÐÁÉ½Ù¥‘•É}É½Ü¹•Ð ‰…‘‘•ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}É½Ýl‰…‘‘•‰t€ô•áÑ•É¹…±}ÑÌ(€€€€€€€€€€€€€€€€€€€€€€€ÕÑ½™˜€ôÑ¥µ”¹Ñ¥µ” ¤€´€ ÌÀ€¨€ÈÐ€¨€ØÀ€¨€ØÀ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜€¡•áÑ•É¹…°…¹•áÑ•É¹…±}­•ä€øÁÉ½Ù¥‘•É}­•ä…¹(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•áÑ•É¹…±}ÑÌ€øôÕÑ½™˜¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½ÝÌ¹…ÁÁ•¹¡ì‰¥ˆè9½¹”°€‰Í¡½Ý}¹…µ”ˆè‘¥ÍÁ±…å}Í¡½Ý}¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥ˆèÍ•É¥•Í}¥°€‰½Ù•Èˆè½Ù•È°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•…Í½¸ˆè•áÑ•É¹…±}­•ålÁt°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•}¹Õ´ˆè•áÑ•É¹…±}­•ålÅt°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè•áÑ•É¹…°¹•Ð ‰Ñ¥Ñ±”ˆ¤½È€‰Á¥Í½‘”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•¹Í¥½¸ˆè€ˆˆ°€‰…‘‘•ˆè•áÑ•É¹…±}ÑÌ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ù…¥±…‰±”ˆè…±Í•ô¤(€€€€€€€€€€€€€€€€€€€€€€€•±¥˜ÁÉ½Ù¥‘•É}É½Ü…¹ÁÉ½Ù¥‘•É}É½Ýl‰…‘‘•‰t€øôÕÑ½™˜è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½ÝÌ¹…ÁÁ•¹¡ÁÉ½Ù¥‘•É}É½Ü¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ€¬ô€Ä(€€€€€€€€€€€€€€€É½ÝÌ¹Í½ÉÐ¡­•äõ±…µ‰‘„¥Ñ•´è€¡¥Ñ•´¹•Ð ‰…‘‘•ˆ¤½È€À°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥Ñ•´¹•Ð ‰Í•…Í½¸ˆ¤½È€À°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥Ñ•´¹•Ð ‰•Á¥Í½‘•}¹Õ´ˆ¤½È€À¤°É•Ù•ÉÍ”õQÉÕ”¤(€€€€€€€€€€€€€€€ÕÁ½µ¥¹}É½ÝÌ¹Í½ÉÐ¡­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•´¹•Ð ‰…¥É}ÑÌˆ¤½È€À¤(€€€€€€€€€€€€€€€}Í…Ù•}±…Ñ•ÍÑ}•Á¥Í½‘•Í}…¡”¡à°É½ÝÌ°ÕÁ½µ¥¹}É½ÝÍlèÌÙt°•ÉÉ½ÉÌ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰•Á¥Í½‘•ÌˆèÉ½ÝÍlé±¥µ¥Ñt°€‰±½•‘}¥¸ˆèà¹½¹™¥ÕÉ• ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¡…Í}µ½É”ˆè±•¸¡É½ÝÌ¤€ø±¥µ¥Ð°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÕÁ½µ¥¹œˆèÕÁ½µ¥¹}É½ÝÍlèÌÙt°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½ÉÌˆè•ÉÉ½ÉÍô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Í¡½Üˆè(€€€€€€€€€€€€€€€Í•É¥•Í}¥‘}Ñ•áÐ€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€Í•É¥•Í}¥‘Ì€ômÍ¥¹ÍÑÉ¥À ¤™½ÈÍ¥¥¸Í•É¥•Í}¥‘}Ñ•áÐ¹ÍÁ±¥Ð ˆ°ˆ¤¥˜Í¥¹ÍÑÉ¥À ¥t(€€€€€€€€€€€€€€€É•™É•Í €ô€¡Ä¹•Ð ‰É•™É•Í ˆ°lˆÀ‰t¥lÁt¤€ôô€ˆÄˆ(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜¹½Ð€¡à¹½¹™¥ÕÉ• ¤…¹Í•É¥•Í}¥‘Ì¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…É•ÅÕ•ÍÐ‰ô¤(€€€€€€€€€€€€€€€€ŒUÁÉ…‘”½±½¹”µÍ½ÕÉ”™…Ù½É¥Ñ•Ì‰ä‘¥Í½Ù•É¥¹œÍ¥‰±¥¹œÙ…É¥…¹ÑÌ¸(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€…Ñ…±½œ€ô•Ñ}áÑÉ•…µ}Í•É¥•Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€Í•±•Ñ•€ô¹•áÐ ¡É½Ü™½ÈÉ½Ü¥¸…Ñ…±½œ(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÍÑÈ¡É½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¤¤¥¸Í•É¥•Í}¥‘Ì¤°9½¹”¤(€€€€€€€€€€€€€€€€€€€Í•±•Ñ•‘}­•ä€ô}Í¡½Ý}­•ä ¡Í•±•Ñ•½Èíô¤¹•Ð ‰¹…µ”ˆ¤¤(€€€€€€€€€€€€€€€€€€€¥˜Í•±•Ñ•‘}­•äè(€€€€€€€€€€€€€€€€€€€€€€€Í•É¥•Í}¥‘Ì€ômÍÑÈ¡É½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¤¤™½ÈÉ½Ü¥¸…Ñ…±½œ(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜}Í¡½Ý}­•ä¡É½Ü¹•Ð ‰¹…µ”ˆ¤¤€ôôÍ•±•Ñ•‘}­•åt(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€Ù…É¥…¹ÑÌ€ômt(€€€€€€€€€€€€€€€™½ÈÍ•É¥•Í}¥¥¸Í•É¥•Í}¥‘Ìè(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€‘…Ñ„€ôà¹Í•É¥•Í}¥¹™¼¡Í•É¥•Í}¥°É•™É•Í õÉ•™É•Í ¤½Èíô(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€¥¹™¼€ô‘…Ñ„¹•Ð ‰¥¹™¼ˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡¥¹™¼°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€¥¹™¼€ôíô(€€€€€€€€€€€€€€€€€€€Ù…É¥…¹ÑÌ¹…ÁÁ•¹ ¡Í•É¥•Í}¥°‘…Ñ„°¥¹™¼¤¤(€€€€€€€€€€€€€€€¥˜¹½ÐÙ…É¥…¹ÑÌè(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰•ÉÉ½Èˆè€‰½Õ±¹½Ð±½…Ñ¡¥ÌÍ¡½Ü¸‰ô¤(€€€€€€€€€€€€€€€Í•É¥•Í}¥°‘…Ñ„°¥¹™¼€ôÙ…É¥…¹ÑÍlÁt(€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡¥¹™¼°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€¥¹™¼€ôíô(€€€€€€€€€€€€€€€½Ù•È€ôÍÑÈ¡¥¹™¼¹•Ð ‰½Ù•Èˆ¤½È¥¹™¼¹•Ð ‰µ½Ù¥•}¥µ…”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½Ð½Ù•È¹ÍÑ…ÉÑÍÝ¥Ñ   ‰¡ÑÑÀè¼¼ˆ°€‰¡ÑÑÁÌè¼¼ˆ¤¤è(€€€€€€€€€€€€€€€€€€€½Ù•È€ô€ˆˆ(€€€€€€€€€€€€€€€É…Ý}Í¡½Ý}¹…µ”€ô¥¹™¼¹•Ð ‰¹…µ”ˆ¤½È¥¹™¼¹•Ð ‰Ñ¥Ñ±”ˆ¤½È€‰M¡½Üˆ(€€€€€€€€€€€€€€€Í¡½Ý}¹…µ”€ô}±•…¹}Í¡½Ý}Ñ¥Ñ±”¡É…Ý}Í¡½Ý}¹…µ”¤½ÈÉ…Ý}Í¡½Ý}¹…µ”(€€€€€€€€€€€€€€€É•±•…Í•}Ñ•áÐ€ôÍÑÈ¡¥¹™¼¹•Ð ‰É•±•…Í•…Ñ”ˆ¤½È¥¹™¼¹•Ð ‰É•±•…Í•}‘…Ñ”ˆ¤½ÈÍ¡½Ý}¹…µ”¤(€€€€€€€€€€€€€€€å•…É}µ…Ñ €ôÉ”¹Í•…É ¡Èˆ üèÄåðÈÀ¥q‘ìÉôˆ°É•±•…Í•}Ñ•áÐ¤(€€€€€€€€€€€€€€€Í¡½Ý}å•…È€ôå•…É}µ…Ñ ¹É½ÕÀ À¤¥˜å•…É}µ…Ñ •±Í”€ˆˆ(€€€€€€€€€€€€€€€µ…é•}½Ù•ÉÌ€ô}ÑÙµ…é•}Í•…Í½¹}½Ù•ÉÌ¡Í¡½Ý}¹…µ”°Í¡½Ý}å•…È¤(€€€€€€€€€€€€€€€áÑÉ•…µ}Í•…Í½¹}½Ù•ÉÌ€ôíô(€€€€€€€€€€€€€€€™½È}Ù…É¥…¹Ñ}¥°Ù…É¥…¹Ñ}‘…Ñ„°}Ù…É¥…¹Ñ}¥¹™¼¥¸Ù…É¥…¹ÑÌè(€€€€€€€€€€€€€€€€€€€É…Ý}Í•…Í½¹Ì€ôÙ…É¥…¹Ñ}‘…Ñ„¹•Ð ‰Í•…Í½¹Ìˆ¤½Èmt(€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡É…Ý}Í•…Í½¹Ì°±¥ÍÐ¤è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€™½Èµ•Ñ„¥¸É…Ý}Í•…Í½¹Ìè(€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡µ•Ñ„°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€­•ä€ôµ•Ñ„¹•Ð ‰Í•…Í½¹}¹Õµ‰•Èˆ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜­•ä¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€­•ä€ôµ•Ñ„¹•Ð ‰Í•…Í½¸ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜­•ä¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ…Ñ €ôÉ”¹Í•…É ¡È‰q¬ˆ°ÍÑÈ¡µ•Ñ„¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€­•ä€ôµ…Ñ ¹É½ÕÀ À¤¥˜µ…Ñ •±Í”9½¹”(€€€€€€€€€€€€€€€€€€€€€€€…ÉÐ€ôÍÑÈ¡µ•Ñ„¹•Ð ‰½Ù•Èˆ¤½Èµ•Ñ„¹•Ð ‰½Ù•É}‰¥œˆ¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€µ•Ñ„¹•Ð ‰µ½Ù¥•}¥µ…”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜­•ä¥Ì¹½Ð9½¹”…¹…ÉÐ¹ÍÑ…ÉÑÍÝ¥Ñ   ‰¡ÑÑÀè¼¼ˆ°€‰¡ÑÑÁÌè¼¼ˆ¤¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€áÑÉ•…µ}Í•…Í½¹}½Ù•ÉÍmÍÑÈ¡­•ä¥t€ô…ÉÐ(€€€€€€€€€€€€€€€•Á¥Í½‘•}µ…À€ôíô(€€€€€€€€€€€€€€€™½È}Ù…É¥…¹Ñ}¥°Ù…É¥…¹Ñ}‘…Ñ„°Ù…É¥…¹Ñ}¥¹™¼¥¸Ù…É¥…¹ÑÌè(€€€€€€€€€€€€€€€€€€€Ù…É¥…¹Ñ}¹…µ”€ôÙ…É¥…¹Ñ}¥¹™¼¹•Ð ‰¹…µ”ˆ¤½ÈÙ…É¥…¹Ñ}¥¹™¼¹•Ð ‰Ñ¥Ñ±”ˆ¤½ÈÍ¡½Ý}¹…µ”(€€€€€€€€€€€€€€€€€€€±…‰•°€ô}Í¡½Ý}Ù…É¥…¹Ñ}±…‰•°¡Ù…É¥…¹Ñ}¹…µ”¤(€€€€€€€€€€€€€€€€€€€É…Ý}•Á¥Í½‘•Ì€ôÙ…É¥…¹Ñ}‘…Ñ„¹•Ð ‰•Á¥Í½‘•Ìˆ¤½Èíô(€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡É…Ý}•Á¥Í½‘•Ì°±¥ÍÐ¤è(€€€€€€€€€€€€€€€€€€€€€€€É½ÕÁ•€ôíô(€€€€€€€€€€€€€€€€€€€€€€€™½È•À¥¸É…Ý}•Á¥Í½‘•Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½ÕÁ•¹Í•Ñ‘•™…Õ±Ð¡ÍÑÈ¡•À¹•Ð ‰Í•…Í½¸ˆ¤½È€Ä¤°mt¤¹…ÁÁ•¹¡•À¤(€€€€€€€€€€€€€€€€€€€€€€€É…Ý}•Á¥Í½‘•Ì€ôÉ½ÕÁ•(€€€€€€€€€€€€€€€€€€€™½ÈÍ•…Í½¹}­•ä°•ÁÌ¥¸É…Ý}•Á¥Í½‘•Ì¹¥Ñ•µÌ ¤è(€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡•ÁÌ°±¥ÍÐ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€™½È¤°•À¥¸•¹Õµ•É…Ñ”¡•ÁÌ°€Ä¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•}¹Õ´€ô•À¹•Ð ‰•Á¥Í½‘•}¹Õ´ˆ¤½È¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€­•ä€ô€¡ÍÑÈ¡Í•…Í½¹}­•ä¤°ÍÑÈ¡•Á¥Í½‘•}¹Õ´¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥Ñ•´€ô•Á¥Í½‘•}µ…À¹Í•Ñ‘•™…Õ±Ð¡­•ä°ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•}¹Õ´ˆè•Á¥Í½‘•}¹Õ´°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè}±•…¹}•Á¥Í½‘•}Ñ¥Ñ±” (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•À¹•Ð ‰Ñ¥Ñ±”ˆ¤½È˜‰Á¥Í½‘”í¥ôˆ°Í¡½Ý}¹…µ”¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•Ìˆèmuô¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í½ÕÉ•}±…‰•°€ô±…‰•°(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÕÍ•€ôíÍÉl‰±…‰•°‰t™½ÈÍÉŒ¥¸¥Ñ•µl‰Í½ÕÉ•Ì‰uô(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜Í½ÕÉ•}±…‰•°¥¸ÕÍ•è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÕ™™¥à€ô€È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ý¡¥±”˜‰í±…‰•±ôíÍÕ™™¥áôˆ¥¸ÕÍ•è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÕ™™¥à€¬ô€Ä(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Í½ÕÉ•}±…‰•°€ô˜‰í±…‰•±ôíÍÕ™™¥áôˆ(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥Ñ•µl‰Í½ÕÉ•Ì‰t¹…ÁÁ•¹¡ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¥ˆè•À¹•Ð ‰¥ˆ¤°€‰±…‰•°ˆèÍ½ÕÉ•}±…‰•°°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•¹Í¥½¸ˆè•À¹•Ð ‰½¹Ñ…¥¹•É}•áÑ•¹Í¥½¸ˆ¤½È€‰µÀÐ‰ô¤(€€€€€€€€€€€€€€€Í•…Í½¹Ì€ômt(€€€€€€€€€€€€€€€Í•…Í½¹}¹Õµ‰•ÉÌ€ôÍ½ÉÑ•¡í­•ålÁt™½È­•ä¥¸•Á¥Í½‘•}µ…Áô°(€€€€€€€€€€€€€€€€€€€­•äõ±…µ‰‘„Ù…±Õ”è¥¹Ð¡Ù…±Õ”¤¥˜Ù…±Õ”¹¥Í‘¥¥Ð ¤•±Í”€ääääää¤(€€€€€€€€€€€€€€€™½ÈÍ•…Í½¹}­•ä¥¸Í•…Í½¹}¹Õµ‰•ÉÌè(€€€€€€€€€€€€€€€€€€€¹½Éµ…±¥é•€ôm¥Ñ•´™½È€¡Í•…Í½¸°}¹Õµ‰•È¤°¥Ñ•´¥¸•Á¥Í½‘•}µ…À¹¥Ñ•µÌ ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜Í•…Í½¸€ôôÍ•…Í½¹}­•åt(€€€€€€€€€€€€€€€€€€€¹½Éµ…±¥é•¹Í½ÉÐ¡­•äõ±…µ‰‘„•Àè¥¹Ð¡•Ál‰•Á¥Í½‘•}¹Õ´‰t¤¥˜ÍÑÈ¡•Ál‰•Á¥Í½‘•}¹Õ´‰t¤¹¥Í‘¥¥Ð ¤•±Í”€ääääää¤(€€€€€€€€€€€€€€€€€€€Í•…Í½¹Ì¹…ÁÁ•¹¡ì‰¹Õµ‰•ÈˆèÍ•…Í½¹}­•ä°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè˜‰M•…Í½¸íÍ•…Í½¹}­•åôˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆè€¡µ…é•}½Ù•ÉÌ¹•Ð¡ÍÑÈ¡Í•…Í½¹}­•ä¤¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€áÑÉ•…µ}Í•…Í½¹}½Ù•ÉÌ¹•Ð¡ÍÑÈ¡Í•…Í½¹}­•ä¤¤½È½Ù•È¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•Ìˆè¹½Éµ…±¥é•‘ô¤(€€€€€€€€€€€€€€€Í•…Í½¹Ì¹Í½ÉÐ¡­•äõ±…µ‰‘„Ìè¥¹Ð¡Íl‰¹Õµ‰•È‰t¤¥˜ÍÑÈ¡Íl‰¹Õµ‰•È‰t¤¹¥Í‘¥¥Ð ¤•±Í”€ääääää¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰¹…µ”ˆèÍ¡½Ý}¹…µ”°€‰Í¡½Ý}­•äˆè}Í¡½Ý}­•ä¡Í¡½Ý}¹…µ”¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥ˆèÍ•É¥•Í}¥‘ÍlÁt°€‰Í•É¥•Í}¥‘ÌˆèÍ•É¥•Í}¥‘Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆè½Ù•È°€‰Í•…Í½¹ÌˆèÍ•…Í½¹Íô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Í¡½Ý}•áÑ•É¹…°ˆè(€€€€€€€€€€€€€€€…Ñ…±½}¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€µ•Ñ„€ô¥¹•µ•Ñ…}µ•Ñ„ ‰Í•É¥•Ìˆ°…Ñ…±½}¥¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰•ÉÉ½Èˆè€‰½Õ±¹½Ð±½…Í¡½Üµ•Ñ…‘…Ñ„è€ˆ€¬ÍÑÈ¡”¥ô¤(€€€€€€€€€€€€€€€¥˜¹½Ðµ•Ñ„è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰•ÉÉ½Èˆè€‰½Õ±¹½Ð±½…Ñ¡¥ÌÍ¡½Ü¸‰ô¤(€€€€€€€€€€€€€€€Í¡½Ý}¹…µ”€ôÍÑÈ¡µ•Ñ„¹•Ð ‰¹…µ”ˆ¤½È€‰M¡½Üˆ¤(€€€€€€€€€€€€€€€Í¡½Ý}­•ä€ô}Í¡½Ý}­•ä¡Í¡½Ý}¹…µ”¤(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}¥‘Ì€ômt(€€€€€€€€€€€€€€€¥˜à¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}¥‘Ì€ômÉ½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¤™½ÈÉ½Ü¥¸•Ñ}áÑÉ•…µ}Í•É¥•Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜}Í¡½Ý}­•ä¡É½Ü¹•Ð ‰¹…µ”ˆ¤¤€ôôÍ¡½Ý}­•ä…¹(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¤¥Ì¹½Ð9½¹•t(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½Ù¥‘•É}¥‘Ì€ômt(€€€€€€€€€€€€€€€¥˜ÁÉ½Ù¥‘•É}¥‘Ìè(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ñ…±½}¥ˆè…Ñ…±½}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÁÉ½Ù¥‘•É}Í•É¥•Í}¥‘ÌˆèÁÉ½Ù¥‘•É}¥‘Íô¤(€€€€€€€€€€€€€€€É½ÕÁ•€ôíô(€€€€€€€€€€€€€€€™½ÈÙ¥‘•¼¥¸µ•Ñ„¹•Ð ‰Ù¥‘•½Ìˆ¤½Èmtè(€€€€€€€€€€€€€€€€€€€Í•…Í½¸€ôÙ¥‘•¼¹•Ð ‰Í•…Í½¸ˆ¤(€€€€€€€€€€€€€€€€€€€•Á¥Í½‘”€ôÙ¥‘•¼¹•Ð ‰•Á¥Í½‘”ˆ¤(€€€€€€€€€€€€€€€€€€€¥˜Í•…Í½¸¥Ì9½¹”½È•Á¥Í½‘”¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€É½ÕÁ•¹Í•Ñ‘•™…Õ±Ð¡ÍÑÈ¡Í•…Í½¸¤°mt¤¹…ÁÁ•¹¡ì(€€€€€€€€€€€€€€€€€€€€€€€€‰•Á¥Í½‘•}¹Õ´ˆè•Á¥Í½‘”°€‰Ñ¥Ñ±”ˆèÙ¥‘•¼¹•Ð ‰¹…µ”ˆ¤½È˜‰Á¥Í½‘”í•Á¥Í½‘•ôˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰É•±•…Í•ˆèÙ¥‘•¼¹•Ð ‰É•±•…Í•ˆ¤½È€ˆˆ°€‰Í½ÕÉ•Ìˆèmuô¤(€€€€€€€€€€€€€€€Í•…Í½¹Ì€ômt(€€€€€€€€€€€€€€€™½ÈÍ•…Í½¸°•Á¥Í½‘•Ì¥¸É½ÕÁ•¹¥Ñ•µÌ ¤è(€€€€€€€€€€€€€€€€€€€•Á¥Í½‘•Ì¹Í½ÉÐ¡­•äõ±…µ‰‘„É½Üè¥¹Ð¡É½Ü¹•Ð ‰•Á¥Í½‘•}¹Õ´ˆ¤½È€À¤¤(€€€€€€€€€€€€€€€€€€€Í•…Í½¹Ì¹…ÁÁ•¹¡ì‰¹Õµ‰•ÈˆèÍ•…Í½¸°€‰Ñ¥Ñ±”ˆè˜‰M•…Í½¸íÍ•…Í½¹ôˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆèµ•Ñ„¹•Ð ‰Á½ÍÑ•Èˆ¤½È€ˆˆ°€‰•Á¥Í½‘•Ìˆè•Á¥Í½‘•Íô¤(€€€€€€€€€€€€€€€Í•…Í½¹Ì¹Í½ÉÐ¡­•äõ±…µ‰‘„É½Üè¥¹Ð¡É½Ýl‰¹Õµ‰•È‰t¤¥˜ÍÑÈ¡É½Ýl‰¹Õµ‰•È‰t¤¹¥Í‘¥¥Ð ¤•±Í”€ääääää¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ñ…±½}¥ˆè…Ñ…±½}¥°€‰¹…µ”ˆèÍ¡½Ý}¹…µ”°(€€€€€€€€€€€€€€€€€€€€‰Í¡½Ý}­•äˆèÍ¡½Ý}­•ä°€‰Í•É¥•Í}¥ˆè9½¹”°€‰Í•É¥•Í}¥‘Ìˆèmt°(€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆèµ•Ñ„¹•Ð ‰Á½ÍÑ•Èˆ¤½È€ˆˆ°€‰å•…Èˆè}…Ñ…±½}å•…È¡µ•Ñ„¤°(€€€€€€€€€€€€€€€€€€€€‰É…Ñ¥¹œˆèµ•Ñ„¹•Ð ‰¥µ‘‰I…Ñ¥¹œˆ¤½È€ˆˆ°€‰Í•…Í½¹ÌˆèÍ•…Í½¹Íô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Ñ•…µ}Í•…É ˆè(€€€€€€€€€€€€€€€Ñ•É´€ô€¡Ä¹•Ð ‰Äˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•É´è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰Ñ•…µÌˆèmuô¤(€€€€€€€€€€€€€€€ÍÉ}•ÉÈ€ômt(€€€€€€€€€€€€€€€Ñ•Éµ}°€ôÑ•É´¹±½Ý•È ¤(€€€€€€€€€€€€€€€Ý…¹Ñ•€ô}•áÁ…¹‘}Ñ•ÉµÌ¡Ñ•Éµ}°¤(€€€€€€€€€€€€€€€™½Õ¹€ômt(€€€€€€€€€€€€€€€Í••¸€ôÍ•Ð ¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€™½ÈÑ•…´¥¸Í•…É¡}™½Ñµ½‰}Ñ•…µÌ¡Ñ•É´¤è(€€€€€€€€€€€€€€€€€€€€€€€±½Ü€ôÍÑÈ¡Ñ•…´¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤¹±½Ý•È ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜±½Ü…¹±½Ü¹½Ð¥¸Í••¸è(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í••¸¹…‘¡±½Ü¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€™½Õ¹¹…ÁÁ•¹¡Ñ•…´¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€ÍÉ}•ÉÈ¹…ÁÁ•¹¡˜‰½Ñ5½ˆÑ•…´Í•…É èí•ôˆ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰Ñ•…µÌˆè™½Õ¹°€‰Í½ÕÉ•}•ÉÉ½ÉÌˆèÍÉ}•ÉÉô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Ñ•…µ}ÁÉ½™¥±”ˆè(€€€€€€€€€€€€€€€Ñ•…µ}¹…µ”€ô€¡Ä¹•Ð ‰¹…µ”ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€Ñ•…µ}¥€ô€¡Ä¹•Ð ‰¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¥…¹Ñ•…µ}¹…µ”è(€€€€€€€€€€€€€€€€€€€Ñ•…µ}¥€ôÉ•Í½±Ù•}™½Ñµ½‰}Ñ•…µ}¥¡Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô™•Ñ¡}Ñ•…µ}ÁÉ½™¥±”¡Ñ•…µ}¥°Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€ÁÉ½™¥±•l‰Ñ•…µ}¥‰t€ôÑ•…µ}¥(€€€€€€€€€€€€€€€ÁÉ½™¥±•l‰±½¼‰t€ô}Ñ•…µ}±½½}ÕÉ°¡Ñ•…µ}¥¤¥˜Ñ•…µ}¥•±Í”€ˆˆ(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰ÁÉ½™¥±”ˆèÁÉ½™¥±•ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½µå}Ñ•…µÌˆè(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€½Õ¹ÑÉ¥•Ì€ô±¥ÍÐ¡=Q5=	}11	-}=U9QI%L¤(€€€€€€€€€€€€€€€™…Ù}‘…Ñ„€ô±½…‘}™…Ù½É¥Ñ•Ì ¤(€€€€€€€€€€€€€€€™…Ù½É¥Ñ•Ì€ô™…Ù}‘…Ñ„¹•Ð ‰Ñ•…µÌˆ°mt¤(€€€€€€€€€€€€€€€™…Ù½É¥Ñ•Í}¡…¹•€ô…±Í”(€€€€€€€€€€€€€€€µ•É•€ôíô(€€€€€€€€€€€€€€€•ÉÉ½ÉÌ€ômt(€€€€€€€€€€€€€€€™½È™…Ù½É¥Ñ”¥¸™…Ù½É¥Ñ•Ìè(€€€€€€€€€€€€€€€€€€€Ñ•…µ}¹…µ”€ôÍÑÈ¡™…Ù½É¥Ñ”¹•Ð ‰¹…µ”ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•±Í”™…Ù½É¥Ñ”¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€Ñ•…µ}¥€ôÍÑÈ¡™…Ù½É¥Ñ”¹•Ð ‰Ñ•…µ}¥ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•±Í”€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¹…µ”è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¥è(€€€€€€€€€€€€€€€€€€€€€€€Ñ•…µ}¥€ôÉ•Í½±Ù•}™½Ñµ½‰}Ñ•…µ}¥¡Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€€€€€¥˜Ñ•…µ}¥…¹¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤…¹¹½Ð™…Ù½É¥Ñ”¹•Ð ‰Ñ•…µ}¥ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€™…Ù½É¥Ñ•l‰Ñ•…µ}¥‰t€ôÑ•…µ}¥(€€€€€€€€€€€€€€€€€€€€€€€™…Ù½É¥Ñ•Í}¡…¹•€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€¥˜Ñ•…µ}¥…¹¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤…¹¹½Ð™…Ù½É¥Ñ”¹•Ð ‰±½¼ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€™…Ù½É¥Ñ•l‰±½¼‰t€ô}Ñ•…µ}±½½}ÕÉ°¡Ñ•…µ}¥¤(€€€€€€€€€€€€€€€€€€€€€€€™…Ù½É¥Ñ•Í}¡…¹•€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ•Ì€ô™•Ñ¡}Ñ•…µ}Í¡•‘Õ±”¡Ñ•…µ}¥°Ñ•…µ}¹…µ”¤¥˜Ñ•…µ}¥•±Í”mt(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡˜‰íÑ•…µ}¹…µ•ôèí•ôˆ¤(€€€€€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ•Ì€ômt(€€€€€€€€€€€€€€€€€€€€ŒÝ••¬µ±½¹œÍ¡•‘Õ±”…¡”¥Ì™¥¹”™½È™ÕÑÕÉ”™¥áÑÕÉ•Ì°‰ÕÐ±¥Ù”(€€€€€€€€€€€€€€€€€€€€ŒÍÑ…Ñ”µÕÍÐ½µ”™É½´Ñ½‘…äÌÍ¡½ÉÐµ±¥Ù•™••½¸•Ù•ÉäÉ•¹‘•È¸(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€‘…¥±å}Ñ•…´€ôÍ•…É¡}‘…¥±å}µ…Ñ¡•Ì¡Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡˜‰íÑ•…µ}¹…µ•ô±¥Ù”ÍÑ…ÑÕÌèí•ôˆ¤(€€€€€€€€€€€€€€€€€€€€€€€‘…¥±å}Ñ•…´€ômt(€€€€€€€€€€€€€€€€€€€™½È‘…¥±ä¥¸‘…¥±å}Ñ•…´è(€€€€€€€€€€€€€€€€€€€€€€€‘ÕÁ±¥…Ñ”€ô9½¹”(€€€€€€€€€€€€€€€€€€€€€€€‘‘…ä€ôÍÑÈ¡‘…¥±ä¹•Ð ‰ÍÑ…ÉÐˆ¤½È€ˆˆ¥lèÄÁt(€€€€€€€€€€€€€€€€€€€€€€€™½È™¥áÑÕÉ”¥¸™¥áÑÕÉ•Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜‘‘…ä…¹ÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰ÍÑ…ÉÐˆ¤½È€ˆˆ¥lèÄÁt€„ô‘‘…äè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€¡½µ•}½¬€ô€¡¹½Éµ…±¥Í”¡™¥áÑÕÉ”¹•Ð ‰¡½µ”ˆ°€ˆˆ¤¤€ôô¹½Éµ…±¥Í”¡‘…¥±ä¹•Ð ‰¡½µ”ˆ°€ˆˆ¤¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…¥±ä¹•Ð ‰¡½µ”ˆ°€ˆˆ¤¹±½Ý•È ¤¥¸}•áÁ…¹‘}Ñ•ÉµÌ¡™¥áÑÕÉ”¹•Ð ‰¡½µ”ˆ°€ˆˆ¤¹±½Ý•È ¤¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€…Ý…å}½¬€ô€¡¹½Éµ…±¥Í”¡™¥áÑÕÉ”¹•Ð ‰…Ý…äˆ°€ˆˆ¤¤€ôô¹½Éµ…±¥Í”¡‘…¥±ä¹•Ð ‰…Ý…äˆ°€ˆˆ¤¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘…¥±ä¹•Ð ‰…Ý…äˆ°€ˆˆ¤¹±½Ý•È ¤¥¸}•áÁ…¹‘}Ñ•ÉµÌ¡™¥áÑÕÉ”¹•Ð ‰…Ý…äˆ°€ˆˆ¤¹±½Ý•È ¤¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¡½µ•}½¬…¹…Ý…å}½¬è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÕÁ±¥…Ñ”€ô™¥áÑÕÉ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•…¬(€€€€€€€€€€€€€€€€€€€€€€€¥˜‘ÕÁ±¥…Ñ”¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ•Ì¹…ÁÁ•¹¡‘¥Ð¡‘…¥±ä°ÍÑ…ÑÕÍ}­¹½Ý¸õQÉÕ”¤¤(€€€€€€€€€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÕÁ±¥…Ñ•l‰¥Í}±¥Ù”‰t€ô‰½½°¡‘…¥±ä¹•Ð ‰¥Í}±¥Ù”ˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÕÁ±¥…Ñ•l‰¥Í}™¥¹¥Í¡•‰t€ô‰½½°¡‘…¥±ä¹•Ð ‰¥Í}™¥¹¥Í¡•ˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÕÁ±¥…Ñ•l‰±¥Ù•}µ¥¹ÕÑ”‰t€ô‘…¥±ä¹•Ð ‰±¥Ù•}µ¥¹ÕÑ”ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÕÁ±¥…Ñ•l‰¡½µ•}¥‰t€ô‘ÕÁ±¥…Ñ”¹•Ð ‰¡½µ•}¥ˆ¤½È‘…¥±ä¹•Ð ‰¡½µ•}¥ˆ°€ˆˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÕÁ±¥…Ñ•l‰…Ý…å}¥‰t€ô‘ÕÁ±¥…Ñ”¹•Ð ‰…Ý…å}¥ˆ¤½È‘…¥±ä¹•Ð ‰…Ý…å}¥ˆ°€ˆˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÕÁ±¥…Ñ•l‰ÍÑ…ÑÕÍ}­¹½Ý¸‰t€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹•áÑ•¹¡…‘‘}ÁÉ¥µ…Éå}ÑÙ}±¥ÍÑ¥¹Ì¡™¥áÑÕÉ•Ì°½Õ¹ÑÉ¥•Ì¤¤(€€€€€€€€€€€€€€€€€€€™½È™¥áÑÕÉ”¥¸™¥áÑÕÉ•Ìè(€€€€€€€€€€€€€€€€€€€€€€€­•ä€ô€‰ðˆ¹©½¥¸ ¡ÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰¡½µ”ˆ°€ˆˆ¤¤¹±½Ý•È ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰…Ý…äˆ°€ˆˆ¤¤¹±½Ý•È ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰ÍÑ…ÉÐˆ°€ˆˆ¤¤¤¤(€€€€€€€€€€€€€€€€€€€€€€€É½Ü€ôµ•É•¹•Ð¡­•ä¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜É½Ü¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½Ü€ô‘¥Ð¡™¥áÑÕÉ”¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰™…Ù½É¥Ñ•}Ñ•…µÌ‰t€ômt(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ•É•‘m­•åt€ôÉ½Ü(€€€€€€€€€€€€€€€€€€€€€€€•±¥˜™¥áÑÕÉ”¹•Ð ‰¥Í}±¥Ù”ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰¥Í}±¥Ù”‰t€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€€€€€¥˜™¥áÑÕÉ”¹•Ð ‰¥Í}™¥¹¥Í¡•ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰¥Í}™¥¹¥Í¡•‰t€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€€€€€¥˜™¥áÑÕÉ”¹•Ð ‰±¥Ù•}µ¥¹ÕÑ”ˆ¤¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰±¥Ù•}µ¥¹ÕÑ”‰t€ô™¥áÑÕÉ”¹•Ð ‰±¥Ù•}µ¥¹ÕÑ”ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜Ñ•…µ}¹…µ”¹½Ð¥¸É½Ýl‰™…Ù½É¥Ñ•}Ñ•…µÌ‰tè(€€€€€€€€€€€€€€€€€€€€€€€€€€€É½Ýl‰™…Ù½É¥Ñ•}Ñ•…µÌ‰t¹…ÁÁ•¹¡Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€¥˜™…Ù½É¥Ñ•Í}¡…¹•è(€€€€€€€€€€€€€€€€€€€Í…Ù•}™…Ù½É¥Ñ•Ì¡™…Ù}‘…Ñ„¤(€€€€€€€€€€€€€€€™¥áÑÕÉ•Ì€ôÍ½ÉÑ•¡µ•É•¹Ù…±Õ•Ì ¤°­•äõ±…µ‰‘„É½ÜèÉ½Ü¹•Ð ‰ÍÑ…ÉÐˆ¤½È€ˆˆ¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€Ñ½Á}™¥áÑÕÉ•Ì€ô™•…ÑÕÉ•‘}‘…¥±å}™¥áÑÕÉ•Ì ¤(€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹•áÑ•¹¡…‘‘}ÁÉ¥µ…Éå}ÑÙ}±¥ÍÑ¥¹Ì¡Ñ½Á}™¥áÑÕÉ•Ì°½Õ¹ÑÉ¥•Ì¤¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€Ñ½Á}™¥áÑÕÉ•Ì€ômt(€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡˜‰½Ñ5½ˆ™•…ÑÕÉ•™¥áÑÕÉ•Ìèí•ôˆ¤(€€€€€€€€€€€€€€€€Œ!å‘É…Ñ”‘ÕÉ…‰±”¡…¹¹•°µ…Ñ¡•Ì‰•™½É”Ñ¡”Á…”É•¹‘•ÉÌ¸Q¡”(€€€€€€€€€€€€€€€€Œ±¥•¹Ðµ…äÉ•™É•Í ÍÑ…±”•¹ÑÉ¥•Ì±…Ñ•È°‰ÕÐ¹•Ù•È¹••‘ÌÑ¼(€€€€€€€€€€€€€€€€ŒÉ•Á±…”…¸…±É•…‘äµ­¹½Ý¸µ…Ñ Ý¥Ñ „¡•­¥¹œÁ±…•¡½±‘•È¸(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€¥˜à¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€€€€€ÍÑ½É•‘}…Ù…¥±…‰¥±¥Ñä€ô}±½…‘}ÍÁ½ÉÑÍ}‘¥Í­}…¡”¡™œ°à¤(€€€€€€€€€€€€€€€€€€€™½È™¥áÑÕÉ”¥¸™¥áÑÕÉ•Ì€¬Ñ½Á}™¥áÑÕÉ•Ìè(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ½É•€ôÍÑ½É•‘}…Ù…¥±…‰¥±¥Ñä¹•Ð¡}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä (€€€€€€€€€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ”¹•Ð ‰¡½µ”ˆ¤°™¥áÑÕÉ”¹•Ð ‰…Ý…äˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ”¹•Ð ‰ÍÑ…ÉÐˆ¤¤¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡ÍÑ½É•°‘¥Ð¤…¹¥Í¥¹ÍÑ…¹”¡ÍÑ½É•¹•Ð ‰É•ÍÕ±Ðˆ¤°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ”¹ÕÁ‘…Ñ”¡}•¹™½É•}™¥áÑÕÉ•}Í•ÕÉ•}µ…Ñ¡•Ì (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ”°}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}±¥•¹Ð¡ÍÑ½É•‘l‰É•ÍÕ±Ð‰t°à¤¤¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰™¥áÑÕÉ•Ìˆè™¥áÑÕÉ•Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ½Á}™¥áÑÕÉ•ÌˆèÑ½Á}™¥áÑÕÉ•Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}•ÉÉ½ÉÌˆè±¥ÍÐ¡‘¥Ð¹™É½µ­•åÌ¡•ÉÉ½ÉÌ¤¥ô¤((€€€€€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Í•…É ˆè(€€€€€€€€€€€€€€€Ñ•É´€ô€¡Ä¹•Ð ‰Äˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€Í•±•Ñ•‘}Ñ•…µ}¥€ô€¡Ä¹•Ð ‰Ñ•…µ}¥ˆ°lˆ‰t¥lÁt¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜Í•±•Ñ•‘}Ñ•…µ}¥…¹¹½ÐÍ•±•Ñ•‘}Ñ•…µ}¥¹¥Í‘¥¥Ð ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰¥¹Ù…±¥Ñ•…´¥‰ô¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•É´è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰™¥áÑÕÉ•Ìˆèmt°€‰±½•‘}¥¸ˆè…±Í•ô¤(€€€€€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€½Õ¹ÑÉ¥•Ì€ô±¥ÍÐ¡=Q5=	}11	-}=U9QI%L¤(€€€€€€€€€€€€€€€™¥áÑÕÉ•Ì°ÍÉ}•ÉÈ°É•Í½±Ù•‘}Ñ•…µ}¥€ô½µÁ±•Ñ•}Ñ•…µ}™¥áÑÕÉ•Ì (€€€€€€€€€€€€€€€€€€€Ñ•É´°Í•±•Ñ•‘}Ñ•…µ}¥°½Õ¹ÑÉ¥•Ì¤(€€€€€€€€€€€€€€€¥˜Í•±•Ñ•‘}Ñ•…µ}¥è(€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ•Ì€ôm™¥áÑÕÉ”™½È™¥áÑÕÉ”¥¸™¥áÑÕÉ•Ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜Í•±•Ñ•‘}Ñ•…µ}¥¥¸ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰¡½µ•}¥ˆ¤½È€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰…Ý…å}¥ˆ¤½È€ˆˆ¥õt(€€€€€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€€€€€±½•‘}¥¸€ôà¹½¹™¥ÕÉ• ¤(€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ômt°íô(€€€€€€€€€€€€€€€¥˜±½•‘}¥¸è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€€€€€ÍÉ}•ÉÈ¹…ÁÁ•¹¡˜‰aÑÉ•…´èí•ôˆ¤(€€€€€€€€€€€€€€€€€€€€€€€±½•‘}¥¸€ô…±Í”(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€Ñ¡È€ô™±½…Ð¡Ä¹•Ð ‰ÍÑÉ¥Ñ¹•ÍÌˆ°m™œ¹•Ð ‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ°€À¸ØÈ¥t¥lÁt¤(€€€€€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€€€€€Ñ¡È€ô™±½…Ð¡™œ¹•Ð ‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ°€À¸ØÈ¤½È€À¸ØÈ¤(€€€€€€€€€€€€€€€Ñ¡È€ôµ…à À¸ÐÀ°µ¥¸ À¸àÀ°Ñ¡È¤¤(€€€€€€€€€€€€€€€µ…Ñ¡}™œ€ô‘¥Ð¡™œ°µ…Ñ¡}Ñ¡É•Í¡½±õÑ¡È¤(€€€€€€€€€€€€€€€ÁÁÙ}…ÑÌ€ôÁÁÙ}…Ñ•½É¥•Ì¡¡…¹¹•±Ì°…ÑÌ¤¥˜±½•‘}¥¸•±Í”mt(€€€€€€€€€€€€€€€ÍÁ½ÉÑÍ}‘¥Í¬€ô}±½…‘}ÍÁ½ÉÑÍ}‘¥Í­}…¡”¡µ…Ñ¡}™œ°à¤¥˜±½•‘}¥¸•±Í”íô(€€€€€€€€€€€€€€€•Á}‘¥Í½Ù•É¥•Ì€ôíô(€€€€€€€€€€€€€€€¥˜±½•‘}¥¸è(€€€€€€€€€€€€€€€€€€€}±½…‘}•Á}‘¥Í­}…¡”¡à¤(€€€€€€€€€€€€€€€€€€€•Á}‘¥Í½Ù•É¥•Ì€ô}…¡•‘}•Á}‘¥Í½Ù•Éä (€€€€€€€€€€€€€€€€€€€€€€€™¥áÑÕÉ•Ì°¡…¹¹•±Ì°…ÑÌ°à¤(€€€€€€€€€€€€€€€½ÕÐ€ômt(€€€€€€€€€€€€€€€™½È˜¥¸™¥áÑÕÉ•Ìè(€€€€€€€€€€€€€€€€€€€µ…Ñ¡•Ì€ômt(€€€€€€€€€€€€€€€€€€€ÁÁÙ}¡¥ÑÌ€ômt(€€€€€€€€€€€€€€€€€€€ÍÑÉ•…µ¥¹}½¹±ä€ô…±Í”(€€€€€€€€€€€€€€€€€€€¥˜±½•‘}¥¸è(€€€€€€€€€€€€€€€€€€€€€€€‘¥Í­}­•ä€ô}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä (€€€€€€€€€€€€€€€€€€€€€€€€€€€˜¹•Ð ‰¡½µ”ˆ¤°˜¹•Ð ‰…Ý…äˆ¤°˜¹•Ð ‰ÍÑ…ÉÐˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€…¡•}­•ä€ô€¡}Ù½‘}…¡•}­•ä¡à¤°ÍÑÈ¡Ñ¡È¤°‘¥Í­}­•ä¤(€€€€€€€€€€€€€€€€€€€€€€€…¡•€ô}MA=IQM}Y9Q}!991}!¹•Ð¡…¡•}­•ä¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð…¡•è(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ½É•€ôÍÁ½ÉÑÍ}‘¥Í¬¹•Ð¡‘¥Í­}­•ä¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜€¡¥Í¥¹ÍÑ…¹”¡ÍÑ½É•°‘¥Ð¤…¹(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥Í¥¹ÍÑ…¹”¡ÍÑ½É•¹•Ð ‰É•ÍÕ±Ðˆ¤°‘¥Ð¤¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…¡•€ôì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÑÌˆè™±½…Ð¡ÍÑ½É•¹•Ð ‰ÑÌˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±Ðˆè}•¹™½É•}™¥áÑÕÉ•}Í•ÕÉ•}µ…Ñ¡•Ì (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€˜°}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}±¥•¹Ð (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ½É•‘l‰É•ÍÕ±Ð‰t°à¤¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€€€€€€€€€¥˜€¡…¡•…¹Ñ¥µ”¹Ñ¥µ” ¤€´™±½…Ð¡…¡•¹•Ð ‰ÑÌˆ¤½È€À¤€ð(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€}MA=IQM}Y9Q}!991}QQ0¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô‘¥Ð¡…¡•¹•Ð ‰É•ÍÕ±Ðˆ¤½Èíô¤(€€€€€€€€€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô}µ…Ñ¡}ÍÁ½ÉÑÍ}™¥áÑÕÉ•}¡…¹¹•±Ì (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€˜°µ…Ñ¡}™œ°¡…¹¹•±Ì°…ÑÌ°à¤(€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô}…‘‘}•Á}‘¥Í½Ù•É¥•Ì (€€€€€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð°•Á}‘¥Í½Ù•É¥•Ì¹•Ð¡‘¥Í­}­•ä°mt¤¤(€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô}•¹™½É•}™¥áÑÕÉ•}Í•ÕÉ•}µ…Ñ¡•Ì¡˜°É•ÍÕ±Ð¤(€€€€€€€€€€€€€€€€€€€€€€€}MA=IQM}Y9Q}!991}!m…¡•}­•åt€ôì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°€‰É•ÍÕ±ÐˆèÉ•ÍÕ±Ñô(€€€€€€€€€€€€€€€€€€€€€€€ÍÁ½ÉÑÍ}‘¥Í­m‘¥Í­}­•åt€ôì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±Ðˆè}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}ÍÑ½É…”¡É•ÍÕ±Ð¥ô(€€€€€€€€€€€€€€€€€€€€€€€µ…Ñ¡•Ì€ôÉ•ÍÕ±Ð¹•Ð ‰µ…Ñ¡•Ìˆ¤½Èmt(€€€€€€€€€€€€€€€€€€€€€€€ÁÁÙ}¡¥ÑÌ€ôÉ•ÍÕ±Ð¹•Ð ‰ÁÁÙ}¡¥ÑÌˆ¤½Èmt(€€€€€€€€€€€€€€€€€€€€€€€€Œ¥áÑÕÉ”½•Ù•¹Ð¡…¹¹•±Ì…É”¥¹‘•Á•¹‘•¹Ð½˜‰É½…‘…ÍÑ•È(€€€€€€€€€€€€€€€€€€€€€€€€Œ±¥ÍÑ¥¹Ì…¹É•µ…¥¸•±¥¥‰±”•Ù•¸Ý¡•¸¹¼Õ¥‘”•á¥ÍÑÌ¸(€€€€€€€€€€€€€€€€€€€€€€€…±±}‰…ÍÑ•ÉÌ€ômˆ™½È¹…µ•Ì¥¸™l‰‰å}½Õ¹ÑÉä‰t¹Ù…±Õ•Ì ¤™½Èˆ¥¸¹…µ•Ít(€€€€€€€€€€€€€€€€€€€€€€€¡…Í}±¥¹•…È€ô…¹ä¡¹½Ð}¥Í}ÍÑÉ•…µ¥¹œ¡ˆ¤™½Èˆ¥¸…±±}‰…ÍÑ•ÉÌ¤(€€€€€€€€€€€€€€€€€€€€€€€¡…Í}ÍÑÉ•…µ¥¹œ€ô…¹ä¡}¥Í}ÍÑÉ•…µ¥¹œ¡ˆ¤™½Èˆ¥¸…±±}‰…ÍÑ•ÉÌ¤(€€€€€€€€€€€€€€€€€€€€€€€€Œ€‰½¹±äÍÑÉ•…µ¥¹œˆ€ô¹¼±¥¹•…È‰É½…‘…ÍÑ•È9¹¼¹½Éµ…°µ…Ñ¡•Ì(€€€€€€€€€€€€€€€€€€€€€€€ÍÑÉ•…µ¥¹}½¹±ä€ô€¡¡…Í}ÍÑÉ•…µ¥¹œ…¹¹½Ð¡…Í}±¥¹•…È…¹¹½Ðµ…Ñ¡•Ì¤(€€€€€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡ì‰¡½µ”ˆè™l‰¡½µ”‰t°€‰…Ý…äˆè™l‰…Ý…ä‰t°€‰ÍÑ…ÉÐˆè™l‰ÍÑ…ÉÐ‰t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¡½µ•}¥ˆè˜¹•Ð ‰¡½µ•}¥ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ý…å}¥ˆè˜¹•Ð ‰…Ý…å}¥ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰‰å}½Õ¹ÑÉäˆè™l‰‰å}½Õ¹ÑÉä‰t°€‰µ…Ñ¡•Ìˆèµ…Ñ¡•Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±¥ÍÑ¥¹}Í½ÕÉ”ˆè˜¹•Ð ‰±¥ÍÑ¥¹}Í½ÕÉ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÁÁÙ}¡¥ÑÌˆèÁÁÙ}¡¥ÑÌ°€‰ÍÑÉ•…µ¥¹}½¹±äˆèÍÑÉ•…µ¥¹}½¹±ä°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¥Í}±¥Ù”ˆè‰½½°¡˜¹•Ð ‰¥Í}±¥Ù”ˆ¤¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¥Í}™¥¹¥Í¡•ˆè‰½½°¡˜¹•Ð ‰¥Í}™¥¹¥Í¡•ˆ¤¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±¥Ù•}µ¥¹ÕÑ”ˆè˜¹•Ð ‰±¥Ù•}µ¥¹ÕÑ”ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±•…Õ•}¹…µ”ˆè˜¹•Ð ‰±•…Õ•}¹…µ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±•…Õ•}¥ˆè˜¹•Ð ‰±•…Õ•}¥ˆ°€ˆˆ¥ô¤(€€€€€€€€€€€€€€€¥˜±½•‘}¥¸è(€€€€€€€€€€€€€€€€€€€}Í…Ù•}ÍÁ½ÉÑÍ}‘¥Í­}…¡”¡µ…Ñ¡}™œ°à°ÍÁ½ÉÑÍ}‘¥Í¬¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰™¥áÑÕÉ•Ìˆè½ÕÐ°€‰±½•‘}¥¸ˆè±½•‘}¥¸°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}•ÉÉ½ÉÌˆèÍÉ}•ÉÈ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÁÁÙ}…Ñ•½É¥•ÌˆèÁÁÙ}…ÑÍô¤((€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰¹½Ð™½Õ¹‰ô¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€‘•˜}Á½ÍÑ}½É•}…Á¤¡Í•±˜°Á…Ñ °Á…å±½…¤è(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½…Ñ¥Ù¥Ñäˆè(€€€€€€€€€€€}µ…É­}…ÁÁ}…Ñ¥Ù¥Ñä ¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ•ô¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½Í¡ÕÑ‘½Ý¸ˆè(€€€€€€€€€€€Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ•ô¤(€€€€€€€€€€€}MQ=A}Y9P¹Í•Ð ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½Ñ•ÍÑ}É•‘•¹Ñ¥…±Ìˆè(€€€€€€€€€€€Ñ•ÍÑ}™œ€ô‘¥Ð¡U1Q}=9%¤(€€€€€€€€€€€Ñ•ÍÑ}™œ¹ÕÁ‘…Ñ”¡ì‰áÑÉ•…µ}¡½ÍÐˆèÍÑÈ¡Á…å±½…¹•Ð ‰áÑÉ•…µ}¡½ÍÐˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰áÑÉ•…µ}Á½ÉÐˆèÍÑÈ¡Á…å±½…¹•Ð ‰áÑÉ•…µ}Á½ÉÐˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰áÑÉ•…µ}ÕÍ•ÈˆèÍÑÈ¡Á…å±½…¹•Ð ‰áÑÉ•…µ}ÕÍ•Èˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰áÑÉ•…µ}Á…ÍÌˆèÍÑÈ¡Á…å±½…¹•Ð ‰áÑÉ•…µ}Á…ÍÌˆ¤½È€ˆˆ¥ô¤(€€€€€€€€€€€¥˜¹½ÐaÑÉ•…´¡Ñ•ÍÑ}™œ¤¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½Èˆè€‰!½ÍÐ°ÕÍ•É¹…µ”…¹Á…ÍÍÝ½É…É”É•ÅÕ¥É•‰ô¤(€€€€€€€€€€€½¬°¥¹™¼€ôaÑÉ•…´¡Ñ•ÍÑ}™œ¤¹±½¥¸ ¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆè½¬°€‰¥¹™¼ˆè¥¹™¼¥˜½¬•±Í”9½¹”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½Èˆè9½¹”¥˜½¬•±Í”¥¹™½ô¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½µ…Ñ¡}ÍÑÉ¥Ñ¹•ÍÌˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÍÑÉ¥Ð€ô™±½…Ð¡Á…å±½…¹•Ð ‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ°™œ¹•Ð ‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ°€À¸ØÈ¤¤¤(€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€ÍÑÉ¥Ð€ô€À¸ØÈ(€€€€€€€€€€€ÍÑÉ¥Ð€ôµ…à À¸ÐÀ°µ¥¸ À¸àÀ°ÍÑÉ¥Ð¤¤(€€€€€€€€€€€™l‰µ…Ñ¡}Ñ¡É•Í¡½±‰t€ôÍÑÉ¥Ð(€€€€€€€€€€€Í…Ù•}½¹™¥œ¡™œ¤(€€€€€€€€€€€}±•…É}ÍÁ½ÉÑÍ}•Ù•¹Ñ}¡…¹¹•±}…¡” ¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰µ…Ñ¡}Ñ¡É•Í¡½±ˆèÍÑÉ¥Ñô¤(€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½É…¥¹}Í•É¥•Ìˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€…±±½Ý•€ô€ ‰˜Äˆ°€‰˜Èˆ°€‰˜Ìˆ°€‰¥¹‘å…Èˆ°€‰Ý•Œˆ°€‰™½ÉµÕ±…”ˆ°€‰µ½Ñ½Àˆ°€‰ÝÉŒˆ¤(€€€€€€€€€€€É•ÅÕ•ÍÑ•€ôÁ…å±½…¹•Ð ‰Í•É¥•Ìˆ¤¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…¹•Ð ‰Í•É¥•Ìˆ¤°±¥ÍÐ¤•±Í”mt(€€€€€€€€€€€Í•±•Ñ•€ôm­•ä™½È­•ä¥¸…±±½Ý•¥˜­•ä¥¸É•ÅÕ•ÍÑ•‘t(€€€€€€€€€€€™l‰É…¥¹}Í•É¥•Ì‰t€ôÍ•±•Ñ•(€€€€€€€€€€€Í…Ù•}½¹™¥œ¡™œ¤(€€€€€€€€€€€}±•…É}É…¥¹}…Ù…¥±…‰¥±¥Ñå}…¡” ¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰Í•É¥•ÌˆèÍ•±•Ñ•‘ô¤((€€€‘•˜‘½}A=MP¡Í•±˜¤è(€€€€€€€Ô€ôÕÉ±±¥ˆ¹Á…ÉÍ”¹ÕÉ±Á…ÉÍ”¡Í•±˜¹Á…Ñ ¤(€€€€€€€±•¹Ñ €ô¥¹Ð¡Í•±˜¹¡•…‘•ÉÌ¹•Ð ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°€À¤¤(€€€€€€€¥˜±•¹Ñ €ø€Ô€¨€ÄÀÈÐ€¨€ÄÀÈÐè(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÄÌ°ì‰•ÉÉ½Èˆè€‰	…­ÕÀ½ÈÉ•ÅÕ•ÍÐ¥ÌÑ½¼±…É”‰ô¤(€€€€€€€É…Ü€ôÍ•±˜¹É™¥±”¹É•…¡±•¹Ñ ¤¥˜±•¹Ñ •±Í”ˆ‰íôˆ(€€€€€€€ÑÉäè(€€€€€€€€€€€Á…å±½…€ô©Í½¸¹±½…‘Ì¡É…Ü¹‘•½‘” ‰ÕÑ˜´àˆ¤½È€‰íôˆ¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€Á…å±½…€ôíô(€€€€€€€¥˜Ô¹Á…Ñ ¥¸ìˆ½…Á¤½…Ñ¥Ù¥Ñäˆ°€ˆ½…Á¤½Í¡ÕÑ‘½Ý¸ˆ°€ˆ½…Á¤½Ñ•ÍÑ}É•‘•¹Ñ¥…±Ìˆ°(€€€€€€€€€€€€€€€€€€€€€€ˆ½…Á¤½µ…Ñ¡}ÍÑÉ¥Ñ¹•ÍÌˆ°€ˆ½…Á¤½É…¥¹}Í•É¥•Ì‰ôè(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Á½ÍÑ}½É•}…Á¤¡Ô¹Á…Ñ °Á…å±½…¤(€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÁÉ½™¥±•}‰…­ÕÁ}•áÁ½ÉÐˆè(€€€€€€€€€€€­¥¹€ô€‰™Õ±°ˆ¥˜Á…å±½…¹•Ð ‰ÑåÁ”ˆ¤€ôô€‰™Õ±°ˆ•±Í”€‰ÁÉ½™¥±”ˆ(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°É•…Ñ•}ÁÉ½™¥±•}‰…­ÕÀ¡­¥¹°Á…å±½…¹•Ð ‰Ñ¥µ•±¥¹”ˆ¤¤¤(€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÁÉ½™¥±•}‰…­ÕÁ}¥µÁ½ÉÐˆè(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ôÉ•ÍÑ½É•}ÁÉ½™¥±•}‰…­ÕÀ¡Á…å±½…¹•Ð ‰‰…­ÕÀˆ¤¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°‘¥Ð¡ì‰½¬ˆèQÉÕ•ô°€¨©É•ÍÕ±Ð¤¤(€€€€€€€€€€€•á•ÁÐ€¡Y…±Õ•ÉÉ½È°QåÁ•ÉÉ½È¤…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰•ÉÉ½Èˆè€‰½Õ±¹½ÐÉ•ÍÑ½É”‰…­ÕÀè€ˆ€¬ÍÑÈ¡”¥ô¤(€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÍÁ½ÉÑÍ}•Ù•¹Ñ}¡…¹¹•±Ìˆè(€€€€€€€€€€€™¥áÑÕÉ”€ôÁ…å±½…¹•Ð ‰™¥áÑÕÉ”ˆ¤(€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡™¥áÑÕÉ”°‘¥Ð¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰5¥ÍÍ¥¹œÍÁ½ÉÑÌ™¥áÑÕÉ”‰ô¤(€€€€€€€€€€€¡½µ”€ôÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰¡½µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄØÁt(€€€€€€€€€€€…Ý…ä€ôÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰…Ý…äˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄØÁt(€€€€€€€€€€€ÍÑ…ÉÐ€ôÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰ÍÑ…ÉÐˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèØÑt(€€€€€€€€€€€±•…Õ•}¹…µ”€ôÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰±•…Õ•}¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄØÁt(€€€€€€€€€€€‰å}½Õ¹ÑÉä€ô™¥áÑÕÉ”¹•Ð ‰‰å}½Õ¹ÑÉäˆ¤(€€€€€€€€€€€¥˜¹½Ð¡½µ”½È¹½Ð…Ý…ä½È¹½Ð¥Í¥¹ÍÑ…¹”¡‰å}½Õ¹ÑÉä°‘¥Ð¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰%¹Ù…±¥ÍÁ½ÉÑÌ™¥áÑÕÉ”‰ô¤(€€€€€€€€€€€±•…¹•‘}ÑØ€ôíô(€€€€€€€€€€€™½È½Õ¹ÑÉä°¹…µ•Ì¥¸±¥ÍÐ¡‰å}½Õ¹ÑÉä¹¥Ñ•µÌ ¤¥lèÈÑtè(€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡¹…µ•Ì°±¥ÍÐ¤è(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€½‘”€ôÍÑÈ¡½Õ¹ÑÉä½È€ˆˆ¤¹ÍÑÉ¥À ¤¹ÕÁÁ•È ¥lèÑt(€€€€€€€€€€€€€€€±•…¹•‘}ÑÙm½‘•t€ômÍÑÈ¡¹…µ”½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄÈÁt(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€™½È¹…µ”¥¸¹…µ•ÍlèÌÁt¥˜ÍÑÈ¡¹…µ”½È€ˆˆ¤¹ÍÑÉ¥À ¥t(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€­•ä€ô€¡}Ù½‘}…¡•}­•ä¡à¤°ÍÑÈ¡™œ¹•Ð ‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ¤½È€À¸ØÈ¤°(€€€€€€€€€€€€€€€€€€}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä¡¡½µ”°…Ý…ä°ÍÑ…ÉÐ¤¤(€€€€€€€€€€€…¡•€ô}MA=IQM}Y9Q}!991}!¹•Ð¡­•ä¤(€€€€€€€€€€€™É•Í €ô‰½½°¡…¡•…¹Ñ¥µ”¹Ñ¥µ” ¤€´™±½…Ð¡…¡•¹•Ð ‰ÑÌˆ¤½È€À¤(€€€€€€€€€€€€€€€€€€€€€€€€€ð}MA=IQM}Y9Q}!991}QQ0¤(€€€€€€€€€€€¥˜™É•Í …¹¹½ÐÁ…å±½…¹•Ð ‰™½É”ˆ¤è(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô}•¹™½É•}™¥áÑÕÉ•}Í•ÕÉ•}µ…Ñ¡•Ì (€€€€€€€€€€€€€€€€€€€ì‰¡½µ”ˆè¡½µ”°€‰…Ý…äˆè…Ý…ä°€‰ÍÑ…ÉÐˆèÍÑ…ÉÐ°(€€€€€€€€€€€€€€€€€€€€€‰±•…Õ•}¹…µ”ˆè±•…Õ•}¹…µ”°€‰‰å}½Õ¹ÑÉäˆè±•…¹•‘}ÑÙô°(€€€€€€€€€€€€€€€€€€€…¡•¹•Ð ‰É•ÍÕ±Ðˆ¤½Èíô¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°‘¥Ð¡É•ÍÕ±Ð°…¡•õQÉÕ”¤¤(€€€€€€€€€€€¥˜Á…å±½…¹•Ð ‰…¡•‘}½¹±äˆ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…¡•ˆè…±Í•ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô™¥¹‘}ÍÁ½ÉÑÍ}•Ù•¹Ñ}¡…¹¹•±Ì (€€€€€€€€€€€€€€€€€€€ì‰¡½µ”ˆè¡½µ”°€‰…Ý…äˆè…Ý…ä°€‰ÍÑ…ÉÐˆèÍÑ…ÉÐ°(€€€€€€€€€€€€€€€€€€€€€‰±•…Õ•}¹…µ”ˆè±•…Õ•}¹…µ”°€‰‰å}½Õ¹ÑÉäˆè±•…¹•‘}ÑÙô°™œ¤(€€€€€€€€€€€€€€€}MA=IQM}Y9Q}!991}!m­•åt€ôì‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°€‰É•ÍÕ±ÐˆèÉ•ÍÕ±Ñô(€€€€€€€€€€€€€€€‘¥Í­}•¹ÑÉ¥•Ì€ô}±½…‘}ÍÁ½ÉÑÍ}‘¥Í­}…¡”¡™œ°à¤(€€€€€€€€€€€€€€€‘¥Í­}•¹ÑÉ¥•Ím}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä¡¡½µ”°…Ý…ä°ÍÑ…ÉÐ¥t€ôì(€€€€€€€€€€€€€€€€€€€€‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°€‰É•ÍÕ±Ðˆè}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}ÍÑ½É…”¡É•ÍÕ±Ð¥ô(€€€€€€€€€€€€€€€}Í…Ù•}ÍÁ½ÉÑÍ}‘¥Í­}…¡”¡™œ°à°‘¥Í­}•¹ÑÉ¥•Ì¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°‘¥Ð¡É•ÍÕ±Ð°…¡•õ…±Í”¤¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½Èˆè€‰MÁ½ÉÑÌ¡…¹¹•°Í•…É è€ˆ€¬ÍÑÈ¡”¥ô¤(€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÍÁ½ÉÑÍ}…Ù…¥±…‰¥±¥Ñäˆè(€€€€€€€€€€€¥¹½µ¥¹œ€ôÁ…å±½…¹•Ð ‰™¥áÑÕÉ•Ìˆ¤(€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡¥¹½µ¥¹œ°±¥ÍÐ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰5¥ÍÍ¥¹œÍÁ½ÉÑÌ™¥áÑÕÉ•Ì‰ô¤(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤ìà€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ù…¥±…‰¥±¥Ñäˆèíô°€‰±½•‘}¥¸ˆè…±Í•ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½Èˆè€‰MÁ½ÉÑÌ¡…¹¹•°…Ñ…±½Õ”è€ˆ€¬ÍÑÈ¡”¥ô¤(€€€€€€€€€€€…Ù…¥±…‰¥±¥Ñä€ôíôì¹½Ü€ôÑ¥µ”¹Ñ¥µ” ¤(€€€€€€€€€€€‘¥Í­}•¹ÑÉ¥•Ì€ô}±½…‘}ÍÁ½ÉÑÍ}‘¥Í­}…¡”¡™œ°à¤(€€€€€€€€€€€€ŒI•ÕÍ”Ñ¡”•á¥ÍÑ¥¹œA…¡”¥¸½¹”Á…ÍÌ™½ÈÑ¡”Ý¡½±”™¥áÑÕÉ”(€€€€€€€€€€€€Œ‰…Ñ ¸Q¡¥Ì¥Ì‘¥Í¬½µ•µ½Éäµ½¹±ä…¹¹•Ù•È‘½Ý¹±½…‘ÌÕ¥‘”‘…Ñ„¸(€€€€€€€€€€€}±½…‘}•Á}‘¥Í­}…¡”¡à¤(€€€€€€€€€€€•Á}‘¥Í½Ù•É¥•Ì€ô}…¡•‘}•Á}‘¥Í½Ù•Éä (€€€€€€€€€€€€€€€¥¹½µ¥¹lèÄØÁt°¡…¹¹•±Ì°…ÑÌ°à¤(€€€€€€€€€€€™½ÈÉ…Ý}™¥áÑÕÉ”¥¸¥¹½µ¥¹lèÄØÁtè(€€€€€€€€€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡É…Ý}™¥áÑÕÉ”°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€¡½µ”€ôÍÑÈ¡É…Ý}™¥áÑÕÉ”¹•Ð ‰¡½µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄØÁt(€€€€€€€€€€€€€€€…Ý…ä€ôÍÑÈ¡É…Ý}™¥áÑÕÉ”¹•Ð ‰…Ý…äˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄØÁt(€€€€€€€€€€€€€€€ÍÑ…ÉÐ€ôÍÑÈ¡É…Ý}™¥áÑÕÉ”¹•Ð ‰ÍÑ…ÉÐˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèØÑt(€€€€€€€€€€€€€€€±•…Õ•}¹…µ”€ôÍÑÈ¡É…Ý}™¥áÑÕÉ”¹•Ð ‰±•…Õ•}¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄØÁt(€€€€€€€€€€€€€€€‰å}½Õ¹ÑÉä€ôÉ…Ý}™¥áÑÕÉ”¹•Ð ‰‰å}½Õ¹ÑÉäˆ¤(€€€€€€€€€€€€€€€¥˜¹½Ð¡½µ”½È¹½Ð…Ý…ä½È¹½Ð¥Í¥¹ÍÑ…¹”¡‰å}½Õ¹ÑÉä°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€•Ù•¹Ñ}ÑÌ€ô‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡ÍÑ…ÉÐ¹É•Á±…” ‰hˆ°€ˆ¬ÀÀèÀÀˆ¤¤¹Ñ¥µ•ÍÑ…µÀ ¤(€€€€€€€€€€€€€€€€€€€¥˜•Ù•¹Ñ}ÑÌ€ð¹½Ü€´€Ø€¨€ÌØÀÀ½È•Ù•¹Ñ}ÑÌ€ø¹½Ü€¬€ÐÔ€¨€ÈÐ€¨€ÌØÀÀè(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€±•…¹•‘}ÑØ€ôíô(€€€€€€€€€€€€€€€™½È½Õ¹ÑÉä°¹…µ•Ì¥¸±¥ÍÐ¡‰å}½Õ¹ÑÉä¹¥Ñ•µÌ ¤¥lèÈÑtè(€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡¹…µ•Ì°±¥ÍÐ¤è(€€€€€€€€€€€€€€€€€€€€€€€±•…¹•‘}ÑÙmÍÑÈ¡½Õ¹ÑÉä½È€ˆˆ¤¹ÍÑÉ¥À ¤¹ÕÁÁ•È ¥lèÑut€ôl(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡¹…µ”½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄÈÁt™½È¹…µ”¥¸¹…µ•ÍlèÌÁt(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÍÑÈ¡¹…µ”½È€ˆˆ¤¹ÍÑÉ¥À ¥t(€€€€€€€€€€€€€€€­•ä€ô€¡}Ù½‘}…¡•}­•ä¡à¤°ÍÑÈ¡™œ¹•Ð ‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ¤½È€À¸ØÈ¤°(€€€€€€€€€€€€€€€€€€€€€€}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä¡¡½µ”°…Ý…ä°ÍÑ…ÉÐ¤¤(€€€€€€€€€€€€€€€…¡•€ô}MA=IQM}Y9Q}!991}!¹•Ð¡­•ä¤(€€€€€€€€€€€€€€€‘¥Í­}­•ä€ô}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä¡¡½µ”°…Ý…ä°ÍÑ…ÉÐ¤(€€€€€€€€€€€€€€€¥˜¹½Ð…¡•è(€€€€€€€€€€€€€€€€€€€ÍÑ½É•€ô‘¥Í­}•¹ÑÉ¥•Ì¹•Ð¡‘¥Í­}­•ä¤(€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡ÍÑ½É•°‘¥Ð¤…¹¥Í¥¹ÍÑ…¹”¡ÍÑ½É•¹•Ð ‰É•ÍÕ±Ðˆ¤°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€…¡•€ôì‰ÑÌˆè™±½…Ð¡ÍÑ½É•¹•Ð ‰ÑÌˆ¤½È€À¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±Ðˆè}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}±¥•¹Ð¡ÍÑ½É•‘l‰É•ÍÕ±Ð‰t°à¥ô(€€€€€€€€€€€€€€€™É•Í €ô‰½½°¡…¡•…¹¹½Ü€´™±½…Ð¡…¡•¹•Ð ‰ÑÌˆ¤½È€À¤€ð(€€€€€€€€€€€€€€€€€€€€€€€€€€€€}MA=IQM}Y9Q}!991}QQ0¤(€€€€€€€€€€€€€€€¥˜™É•Í …¹¹½ÐÁ…å±½…¹•Ð ‰™½É”ˆ¤è(€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô…¡•¹•Ð ‰É•ÍÕ±Ðˆ¤½Èíô(€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô}µ…Ñ¡}ÍÁ½ÉÑÍ}™¥áÑÕÉ•}¡…¹¹•±Ì (€€€€€€€€€€€€€€€€€€€€€€€ì‰¡½µ”ˆè¡½µ”°€‰…Ý…äˆè…Ý…ä°€‰ÍÑ…ÉÐˆèÍÑ…ÉÐ°(€€€€€€€€€€€€€€€€€€€€€€€€€‰±•…Õ•}¹…µ”ˆè±•…Õ•}¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€‰‰å}½Õ¹ÑÉäˆè±•…¹•‘}ÑÙô°™œ°¡…¹¹•±Ì°…ÑÌ°à¤(€€€€€€€€€€€€€€€€€€€}MA=IQM}Y9Q}!991}!m­•åt€ôì‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°€‰É•ÍÕ±ÐˆèÉ•ÍÕ±Ñô(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô}…‘‘}•Á}‘¥Í½Ù•É¥•Ì (€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð°•Á}‘¥Í½Ù•É¥•Ì¹•Ð¡‘¥Í­}­•ä°mt¤¤(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ô}•¹™½É•}™¥áÑÕÉ•}Í•ÕÉ•}µ…Ñ¡•Ì (€€€€€€€€€€€€€€€€€€€ì‰¡½µ”ˆè¡½µ”°€‰…Ý…äˆè…Ý…ä°€‰ÍÑ…ÉÐˆèÍÑ…ÉÐ°(€€€€€€€€€€€€€€€€€€€€€‰±•…Õ•}¹…µ”ˆè±•…Õ•}¹…µ”°€‰‰å}½Õ¹ÑÉäˆè±•…¹•‘}ÑÙô°É•ÍÕ±Ð¤(€€€€€€€€€€€€€€€}MA=IQM}Y9Q}!991}!m­•åt€ôì‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°€‰É•ÍÕ±ÐˆèÉ•ÍÕ±Ñô(€€€€€€€€€€€€€€€‘¥Í­}•¹ÑÉ¥•Ím‘¥Í­}­•åt€ôì‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±Ðˆè}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}ÍÑ½É…”¡É•ÍÕ±Ð¥ô(€€€€€€€€€€€€€€€…Ù…¥±…‰¥±¥Ñål‰ðˆ¹©½¥¸ ¡¡½µ”¹±½Ý•È ¤°…Ý…ä¹±½Ý•È ¤°ÍÑ…ÉÑlèÄÙt¤¥t€ôÉ•ÍÕ±Ð(€€€€€€€€€€€}Í…Ù•}ÍÁ½ÉÑÍ}‘¥Í­}…¡”¡™œ°à°‘¥Í­}•¹ÑÉ¥•Ì¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰…Ù…¥±…‰¥±¥Ñäˆè…Ù…¥±…‰¥±¥Ñä°€‰±½•‘}¥¸ˆèQÉÕ•ô¤(€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½¥µÁ½ÉÑ}ÍÑ•…µ}Ý¥Í¡±¥ÍÐˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€Í…Ù•‘}ÕÉ°€ôÍÑÈ¡™œ¹•Ð ‰ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ°ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€Ý¥Í¡±¥ÍÑ}ÕÉ°€ôÍÑÈ¡Á…å±½…¹•Ð ‰ÕÉ°ˆ¤½ÈÍ…Ù•‘}ÕÉ°¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€¥˜¹½ÐÝ¥Í¡±¥ÍÑ}ÕÉ°è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰¹Ñ•È„MÑ•…´Ý¥Í¡±¥ÍÐUI0‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€…¡•‘}¥€ôÍÑÈ¡™œ¹•Ð ‰ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}¥ˆ¤½È€ˆˆ¤¥˜Í…Ù•‘}ÕÉ°€ôôÝ¥Í¡±¥ÍÑ}ÕÉ°•±Í”€ˆˆ(€€€€€€€€€€€€€€€ÍÑ•…µ}¥€ô…¡•‘}¥¥˜É”¹™Õ±±µ…Ñ ¡È‰q‘ìÄÝôˆ°…¡•‘}¥¤•±Í”É•Í½±Ù•}ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}¥¡Ý¥Í¡±¥ÍÑ}ÕÉ°¤(€€€€€€€€€€€€€€€¥˜¹½ÐÍÑ•…µ}¥è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰½Õ±¹½ÐÉ•Í½±Ù”Ñ¡…ÐMÑ•…´ÁÉ½™¥±”‰ô¤(€€€€€€€€€€€€€€€Ý¥Í¡±¥ÍÐ€ôÍÑ•…µ}Ý¥Í¡±¥ÍÑ}¥Ñ•µÌ¡ÍÑ•…µ}¥¤(€€€€€€€€€€€€€€€¥‘Ì€ômÍÑÈ¡¥Ñ•´¹•Ð ‰…ÁÁ¥ˆ¤¤™½È¥Ñ•´¥¸Ý¥Í¡±¥ÍÐ¥˜ÍÑÈ¡¥Ñ•´¹•Ð ‰…ÁÁ¥ˆ¤½È€ˆˆ¤¹¥Í‘¥¥Ð ¥t(€€€€€€€€€€€€€€€µ•Ñ…‘…Ñ„€ôíÉ½Ýl‰…ÁÁ}¥‰tèÉ½Ü™½ÈÉ½Ü¥¸ÍÑ•…µ}ÍÑ½É•}¥Ñ•µÌ¡¥‘Ì¥ô(€€€€€€€€€€€€€€€ÁÉ¥½É¥Ñ¥•Ì€ôíÍÑÈ¡¥Ñ•´¹•Ð ‰…ÁÁ¥ˆ¤¤è¥¹Ð¡¥Ñ•´¹•Ð ‰ÁÉ¥½É¥Ñäˆ¤½È€À¤™½È¥Ñ•´¥¸Ý¥Í¡±¥ÍÑô(€€€€€€€€€€€€€€€™…Ø€ô±½…‘}™…Ù½É¥Ñ•Ì ¤(€€€€€€€€€€€€€€€ÕÉÉ•¹Ñ}¥‘Ì€ôÍ•Ð¡¥‘Ì¤(€€€€€€€€€€€€€€€€ŒI•µ½Ù”½¹±ä…µ•ÌÁÉ•Ù¥½ÕÍ±ä¥µÁ½ÉÑ•™É½´Ñ¡¥ÌÝ¥Í¡±¥ÍÐìµ…¹Õ…°™…Ù½É¥Ñ•ÌÍÑ…ä¸(€€€€€€€€€€€€€€€™…Ùl‰…µ•Ì‰t€ôm…µ”™½È…µ”¥¸™…Ø¹•Ð ‰…µ•Ìˆ°mt¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½Ð€¡…µ”¹•Ð ‰Ý¥Í¡±¥ÍÑ}¥µÁ½ÉÑ•ˆ¤…¹ÍÑÈ¡…µ”¹•Ð ‰…ÁÁ}¥ˆ¤¤¹½Ð¥¸ÕÉÉ•¹Ñ}¥‘Ì¥t(€€€€€€€€€€€€€€€‰å}¥€ôíÍÑÈ¡…µ”¹•Ð ‰…ÁÁ}¥ˆ¤¤è…µ”™½È…µ”¥¸™…Ùl‰…µ•Ì‰uô(€€€€€€€€€€€€€€€™½È…ÁÁ}¥¥¸¥‘Ìè(€€€€€€€€€€€€€€€€€€€‘•Ñ…¥±Ì€ôµ•Ñ…‘…Ñ„¹•Ð¡…ÁÁ}¥¤(€€€€€€€€€€€€€€€€€€€¥˜¹½Ð‘•Ñ…¥±Ìè(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€•á¥ÍÑ¥¹œ€ô‰å}¥¹•Ð¡…ÁÁ}¥¤(€€€€€€€€€€€€€€€€€€€¥˜•á¥ÍÑ¥¹œ¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€•á¥ÍÑ¥¹œ€ôì‰…ÁÁ}¥ˆè…ÁÁ}¥°€‰Ý¥Í¡±¥ÍÑ}¥µÁ½ÉÑ•ˆèQÉÕ•ô(€€€€€€€€€€€€€€€€€€€€€€€™…Ùl‰…µ•Ì‰t¹…ÁÁ•¹¡•á¥ÍÑ¥¹œ¤(€€€€€€€€€€€€€€€€€€€€€€€‰å}¥‘m…ÁÁ}¥‘t€ô•á¥ÍÑ¥¹œ(€€€€€€€€€€€€€€€€€€€•á¥ÍÑ¥¹l‰Ý¥Í¡±¥ÍÑ}¥µÁ½ÉÑ•‰t€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€•á¥ÍÑ¥¹œ¹ÕÁ‘…Ñ”¡ì‰¹…µ”ˆè‘•Ñ…¥±Ì¹•Ð ‰¹…µ”ˆ¤½È•á¥ÍÑ¥¹œ¹•Ð ‰¹…µ”ˆ¤½È€‰…µ”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆè‘•Ñ…¥±Ì¹•Ð ‰½Ù•Èˆ¤½È•á¥ÍÑ¥¹œ¹•Ð ‰½Ù•Èˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•±•…Í•}Ñ•áÐˆè‘•Ñ…¥±Ì¹•Ð ‰É•±•…Í•}Ñ•áÐˆ¤½È•á¥ÍÑ¥¹œ¹•Ð ‰É•±•…Í•}Ñ•áÐˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•±•…Í•ˆè‘•Ñ…¥±Ì¹•Ð ‰É•±•…Í•ˆ¤½È•á¥ÍÑ¥¹œ¹•Ð ‰É•±•…Í•ˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÕÉ°ˆè‘•Ñ…¥±Ì¹•Ð ‰ÕÉ°ˆ¤½È•á¥ÍÑ¥¹œ¹•Ð ‰ÕÉ°ˆ¤½È€ˆˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ý¥Í¡±¥ÍÑ}ÁÉ¥½É¥ÑäˆèÁÉ¥½É¥Ñ¥•Ì¹•Ð¡…ÁÁ}¥°€À¥ô¤(€€€€€€€€€€€€€€€Í…Ù•}™…Ù½É¥Ñ•Ì¡™…Ø¤(€€€€€€€€€€€€€€€™l‰ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}ÕÉ°‰t€ôÝ¥Í¡±¥ÍÑ}ÕÉ°(€€€€€€€€€€€€€€€™l‰ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}¥‰t€ôÍÑ•…µ}¥(€€€€€€€€€€€€€€€™l‰ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}Íå¹•‘}…Ð‰t€ô¥¹Ð¡Ñ¥µ”¹Ñ¥µ” ¤¤(€€€€€€€€€€€€€€€Í…Ù•}½¹™¥œ¡™œ¤(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€ÍÑ•…µ}ÁÕ‰±¥}ÁÉ½™¥±”¡ÍÑ•…µ}¥°™½É”õQÉÕ”¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰¥µÁ½ÉÑ•ˆè±•¸¡µ•Ñ…‘…Ñ„¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ý¥Í¡±¥ÍÑ}Ñ½Ñ…°ˆè±•¸¡¥‘Ì¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Íå¹•‘}…Ðˆè™l‰ÍÑ•…µ}Ý¥Í¡±¥ÍÑ}Íå¹•‘}…Ð‰uô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½Èˆè€‰MÑ•…´Ý¥Í¡±¥ÍÐè€ˆ€¬ÍÑÈ¡”¥ô¤(€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½½¹™¥œˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€ÁÉ½Ù¥‘•É}‰•™½É”€ôÑÕÁ±”¡ÍÑÈ¡™œ¹•Ð¡¬¤½È€ˆˆ¤™½È¬¥¸(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ ‰áÑÉ•…µ}¡½ÍÐˆ°€‰áÑÉ•…µ}Á½ÉÐˆ°€‰áÑÉ•…µ}ÕÍ•Èˆ°€‰áÑÉ•…µ}Á…ÍÌˆ¤¤(€€€€€€€€€€€™½È¬¥¸€ ‰áÑÉ•…µ}¡½ÍÐˆ°€‰áÑÉ•…µ}Á½ÉÐˆ°€‰áÑÉ•…µ}ÕÍ•Èˆ°€‰áÑÉ•…µ}Á…ÍÌˆ°(€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}•áÐˆ°€‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ°€‰½Õ¹ÑÉ¥•Ìˆ°€‰ÍÑ…ÉÑ}Í•Ñ¥½¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€‰¡•­}Í¡½ÝÍ}½¹}ÍÑ…ÉÑÕÀˆ°€‰É•™É•Í¡}¥ÁÑÙ}½¹}ÍÑ…ÉÑÕÀˆ°€‰É•™É•Í¡}ÍÁ½ÉÑÍ}½¹}ÍÑ…ÉÑÕÀˆ°€‰ÁÉ½™¥±•}¹…µ”ˆ°(€€€€€€€€€€€€€€€€€€€€€€‰ÁÉ•™•ÉÉ•‘}±…¹Õ…”ˆ°€‰ÁÉ½™¥±•}•µ‰±•´ˆ°€‰µå±¥ÍÑ}±…å½ÕÐˆ°€‰™½½Ñ‰…±±}•¹…‰±•ˆ°(€€€€€€€€€€€€€€€€€€€€€€‰˜Å}•¹…‰±•ˆ°€‰…µ•Í}•¹…‰±•ˆ°€‰‘•½É…Ñ¥½¹Í}•¹…‰±•ˆ°€‰‰…­É½Õ¹‘}ÍÑå±”ˆ°€‰Í•ÑÕÁ}½µÁ±•Ñ”ˆ°€‰Í•ÑÕÁ}‘•µ½}½¹Ñ•¹Ðˆ°€‰…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•Ìˆ¤è(€€€€€€€€€€€€€€€¥˜¬¥¸Á…å±½…è(€€€€€€€€€€€€€€€€€€€™m­t€ôÁ…å±½…‘m­t(€€€€€€€€€€€¥˜™œ¹•Ð ‰ÍÑÉ•…µ}•áÐˆ¤¹½Ð¥¸€ ‰ÑÌˆ°€‰´ÍÔàˆ¤è(€€€€€€€€€€€€€€€™l‰ÍÑÉ•…µ}•áÐ‰t€ô€‰ÑÌˆ(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€™l‰µ…Ñ¡}Ñ¡É•Í¡½±‰t€ôµ…à À¸ÐÀ°µ¥¸ À¸àÀ°™±½…Ð¡™œ¹•Ð ‰µ…Ñ¡}Ñ¡É•Í¡½±ˆ°€À¸ØÈ¤¤¤¤(€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€™l‰µ…Ñ¡}Ñ¡É•Í¡½±‰t€ô€À¸ØÈ(€€€€€€€€€€€É…Ý}½Õ¹ÑÉ¥•Ì€ô™œ¹•Ð ‰½Õ¹ÑÉ¥•Ìˆ¤¥˜¥Í¥¹ÍÑ…¹”¡™œ¹•Ð ‰½Õ¹ÑÉ¥•Ìˆ¤°±¥ÍÐ¤•±Í”mt(€€€€€€€€€€€™l‰½Õ¹ÑÉ¥•Ì‰t€ô±¥ÍÐ¡‘¥Ð¹™É½µ­•åÌ¡ÍÑÈ¡½‘”¤¹ÍÑÉ¥À ¤¹±½Ý•È ¤™½È½‘”¥¸É…Ý}½Õ¹ÑÉ¥•Ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰m„µéµiuìÉôˆ°ÍÑÈ¡½‘”¤¹ÍÑÉ¥À ¤¤¤¥lèÄÙt½Èl‰¹¼ˆ°€‰ˆˆ°€‰ÕÌ‰t(€€€€€€€€€€€¥˜™œ¹•Ð ‰ÁÉ•™•ÉÉ•‘}±…¹Õ…”ˆ¤¹½Ð¥¸€ ‰•¸ˆ°€‰¹¼ˆ¤è(€€€€€€€€€€€€€€€™l‰ÁÉ•™•ÉÉ•‘}±…¹Õ…”‰t€ô€‰•¸ˆ(€€€€€€€€€€€¥˜™œ¹•Ð ‰µå±¥ÍÑ}±…å½ÕÐˆ¤¹½Ð¥¸€ ‰‰…±…¹•ˆ°€‰ÍÁ½Ñ±¥¡Ðˆ°€‰Ñ¥µ•±¥¹”ˆ°€‰¡Õˆˆ¤è(€€€€€€€€€€€€€€€™l‰µå±¥ÍÑ}±…å½ÕÐ‰t€ô€‰Ñ¥µ•±¥¹”ˆ(€€€€€€€€€€€…±±½Ý•‘}ÍÑ…ÉÑÌ€ô€ ‰µå±¥ÍÐˆ°€‰µåÑ¥µ•±¥¹”ˆ°€‰¡…¹¹•±Ìˆ°€‰µåÑØˆ°€‰µ½Ù¥•Ìˆ°€‰Í¡½ÝÌˆ°€‰…µ•Ìˆ°€‰É…¥¹œˆ°€‰Ñ•…µÌˆ¤(€€€€€€€€€€€¥˜™œ¹•Ð ‰ÍÑ…ÉÑ}Í•Ñ¥½¸ˆ¤¹½Ð¥¸…±±½Ý•‘}ÍÑ…ÉÑÌè(€€€€€€€€€€€€€€€™l‰ÍÑ…ÉÑ}Í•Ñ¥½¸‰t€ô€‰µå±¥ÍÐˆ(€€€€€€€€€€€¥˜€‰‰…­É½Õ¹‘}ÍÑå±”ˆ¹½Ð¥¸Á…å±½……¹€‰‘•½É…Ñ¥½¹Í}•¹…‰±•ˆ¥¸Á…å±½…è(€€€€€€€€€€€€€€€™l‰‰…­É½Õ¹‘}ÍÑå±”‰t€ô€‰™±½…Ðˆ¥˜Á…å±½…¹•Ð ‰‘•½É…Ñ¥½¹Í}•¹…‰±•ˆ¤•±Í”€‰½™˜ˆ(€€€€€€€€€€€¥˜™œ¹•Ð ‰‰…­É½Õ¹‘}ÍÑå±”ˆ¤¹½Ð¥¸€ ‰™±½…Ðˆ°€‰…Í¥¤ˆ°€‰½™˜ˆ¤è(€€€€€€€€€€€€€€€™l‰‰…­É½Õ¹‘}ÍÑå±”‰t€ô€‰™±½…Ðˆ¥˜™œ¹•Ð ‰‘•½É…Ñ¥½¹Í}•¹…‰±•ˆ°QÉÕ”¤•±Í”€‰½™˜ˆ(€€€€€€€€€€€™l‰‘•½É…Ñ¥½¹Í}•¹…‰±•‰t€ô™l‰‰…­É½Õ¹‘}ÍÑå±”‰t€„ô€‰½™˜ˆ(€€€€€€€€€€€™l‰¡¥‘•}µ‘}Ý¥¹‘½Ü‰t€ôQÉÕ”(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€™l‰…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•Ì‰t€ôµ…à À°¥¹Ð¡™œ¹•Ð ‰…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•Ìˆ¤½È€À¤¤(€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€€€€€€€€€™l‰…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•Ì‰t€ô€À(€€€€€€€€€€€¥˜™l‰…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•Ì‰t¹½Ð¥¸€ À°€ÌÀ°€ØÀ°€ÄÈÀ°€ÈÐÀ¤è(€€€€€€€€€€€€€€€™l‰…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•Ì‰t€ô€À(€€€€€€€€€€€™l‰¡•­}Í¡½ÝÍ}½¹}ÍÑ…ÉÑÕÀ‰t€ô‰½½°¡™œ¹•Ð ‰¡•­}Í¡½ÝÍ}½¹}ÍÑ…ÉÑÕÀˆ¤¤(€€€€€€€€€€€™l‰É•™É•Í¡}¥ÁÑÙ}½¹}ÍÑ…ÉÑÕÀ‰t€ô‰½½°¡™œ¹•Ð ‰É•™É•Í¡}¥ÁÑÙ}½¹}ÍÑ…ÉÑÕÀˆ¤¤(€€€€€€€€€€€™l‰É•™É•Í¡}ÍÁ½ÉÑÍ}½¹}ÍÑ…ÉÑÕÀ‰t€ô‰½½°¡™œ¹•Ð ‰É•™É•Í¡}ÍÁ½ÉÑÍ}½¹}ÍÑ…ÉÑÕÀˆ¤¤(€€€€€€€€€€€™œ¹Á½À ‰É•™É•Í¡}…±±}½¹}ÍÑ…ÉÑÕÀˆ°9½¹”¤(€€€€€€€€€€€™œ¹Á½À ‰ÍÑ…ÉÑÕÁ}É•™É•Í¡}µ½‘”ˆ°9½¹”¤(€€€€€€€€€€€Í…Ù•}½¹™¥œ¡™œ¤(€€€€€€€€€€€ÁÉ½Ù¥‘•É}…™Ñ•È€ôÑÕÁ±”¡ÍÑÈ¡™œ¹•Ð¡¬¤½È€ˆˆ¤™½È¬¥¸(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ ‰áÑÉ•…µ}¡½ÍÐˆ°€‰áÑÉ•…µ}Á½ÉÐˆ°€‰áÑÉ•…µ}ÕÍ•Èˆ°€‰áÑÉ•…µ}Á…ÍÌˆ¤¤(€€€€€€€€€€€¥˜ÁÉ½Ù¥‘•É}…™Ñ•È€„ôÁÉ½Ù¥‘•É}‰•™½É”è(€€€€€€€€€€€€€€€}±•…É}ÁÉ½Ù¥‘•É}…¡•Ì ¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ•ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½±•…É}…ÉÑÝ½É­}…¡”ˆè(€€€€€€€€€€€É½½Ð€ô…ÉÑÝ½É­}…¡•}‘¥È ¤(€€€€€€€€€€€É•µ½Ù•€ô…ÉÑÝ½É­}…¡•}Í¥é” ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¥˜½Ì¹Á…Ñ ¹¥Í‘¥È¡É½½Ð¤è(€€€€€€€€€€€€€€€€€€€Í¡ÕÑ¥°¹ÉµÑÉ•”¡É½½Ð¤(€€€€€€€€€€€€€€€}QY5i}!¹±•…È ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰É•µ½Ù•‘}‰åÑ•ÌˆèÉ•µ½Ù•‘ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½É•Í•Ñ}½±‘}ÍÑ…ÉÐˆè(€€€€€€€€€€€É•µ½Ù•‘}Í¡•‘Õ±•Ì€ô€À(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€}±•…É}ÁÉ½Ù¥‘•É}…¡•Ì ¤(€€€€€€€€€€€€€€€}QY}!¹±•…È ¤(€€€€€€€€€€€€€€€}Q5}%aQUI}!¹±•…È ¤(€€€€€€€€€€€€€€€}Q5}AI=%1}!¹±•…È ¤(€€€€€€€€€€€€€€€}Q5}%}!¹±•…È ¤(€€€€€€€€€€€€€€€}%1e}5Q!}!¹ÕÁ‘…Ñ”¡ì‰‘…Ñ”ˆè€ˆˆ°€‰ÑÌˆè€À°€‰µ…Ñ¡•Ìˆèmuô¤(€€€€€€€€€€€€€€€}Å}M!U1}!¹ÕÁ‘…Ñ”¡ì‰ÑÌˆè€À°€‰•Ù•¹ÑÌˆèmuô¤(€€€€€€€€€€€€€€€}Å}Q5M}!¹ÕÁ‘…Ñ”¡ì‰ÑÌˆè€À°€‰Ñ•…µÌˆèmuô¤(€€€€€€€€€€€€€€€}±•…É}É…¥¹}…Ù…¥±…‰¥±¥Ñå}…¡” ¤(€€€€€€€€€€€€€€€}QY5i}!¹±•…È ¤(€€€€€€€€€€€€€€€…¡•}É½½Ð€ô‘…Ñ…}…¡•}‘¥È ¤(€€€€€€€€€€€€€€€¥˜½Ì¹Á…Ñ ¹¥Í‘¥È¡…¡•}É½½Ð¤è(€€€€€€€€€€€€€€€€€€€Í¡ÕÑ¥°¹ÉµÑÉ•”¡…¡•}É½½Ð¤(€€€€€€€€€€€€€€€É½½Ð€ô…ÉÑÝ½É­}…¡•}‘¥È ¤(€€€€€€€€€€€€€€€¥˜½Ì¹Á…Ñ ¹¥Í‘¥È¡É½½Ð¤è(€€€€€€€€€€€€€€€€€€€™½È‰…Í”°}‘¥ÉÌ°™¥±•Ì¥¸½Ì¹Ý…±¬¡É½½Ð¤è(€€€€€€€€€€€€€€€€€€€€€€€™½È¹…µ”¥¸™¥±•Ìè(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¹…µ”¹½Ð¥¸€ ‰•Á¥Í½‘”µÍ¡•‘Õ±”¹©Í½¸ˆ°€‰±…Ñ•ÍÐµ•Á¥Í½‘”¹©Í½¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±…Ñ•ÍÐµ•Á¥Í½‘•Ì¹©Í½¸ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½Ì¹É•µ½Ù”¡½Ì¹Á…Ñ ¹©½¥¸¡‰…Í”°¹…µ”¤¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€É•µ½Ù•‘}Í¡•‘Õ±•Ì€¬ô€Ä(€€€€€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐ=MÉÉ½Èè(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•µ½Ù•‘}Í¡•‘Õ±•ÌˆèÉ•µ½Ù•‘}Í¡•‘Õ±•Íô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½¡•­}Í¡½Ý}ÕÁ‘…Ñ•Ìˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ôÉ•™É•Í¡}™…Ù½É¥Ñ•}Í¡½Ý}•Á¥Í½‘•Ì¡™œ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°‘¥Ð¡ì‰½¬ˆèQÉÕ•ô°€¨©É•ÍÕ±Ð¤¤(€€€€€€€€€€€•á•ÁÐY…±Õ•ÉÉ½È…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½¡•­}Ñ•…µ}™¥áÑÕÉ•Ìˆè(€€€€€€€€€€€™…Ù}‘…Ñ„€ô±½…‘}™…Ù½É¥Ñ•Ì ¤(€€€€€€€€€€€™…Ù½É¥Ñ•Ì€ô™…Ù}‘…Ñ„¹•Ð ‰Ñ•…µÌˆ°mt¤(€€€€€€€€€€€}Q5}%aQUI}!¹±•…È ¤(€€€€€€€€€€€}Q5}AI=%1}!¹±•…È ¤(€€€€€€€€€€€}É•µ½Ù•}‘…Ñ…}…¡•}ÁÉ•™¥à ‰Ñ•…´µ™¥áÑÕÉ•Ì´ˆ¤(€€€€€€€€€€€}É•µ½Ù•}‘…Ñ…}…¡•}ÁÉ•™¥à¡˜‰Ñ•…´µÁÉ½™¥±”µÙí}Q5}AI=%1}!}M!5ô´ˆ¤(€€€€€€€€€€€É•™É•Í¡•€ô€À(€€€€€€€€€€€•ÉÉ½ÉÌ€ômt(€€€€€€€€€€€¡…¹•€ô…±Í”(€€€€€€€€€€€™½È™…Ù½É¥Ñ”¥¸™…Ù½É¥Ñ•Ìè(€€€€€€€€€€€€€€€Ñ•…µ}¹…µ”€ôÍÑÈ¡™…Ù½É¥Ñ”¹•Ð ‰¹…µ”ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•±Í”™…Ù½É¥Ñ”¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¹…µ”è(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€Ñ•…µ}¥€ôÍÑÈ¡™…Ù½É¥Ñ”¹•Ð ‰Ñ•…µ}¥ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€•±Í”€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¥è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€Ñ•…µ}¥€ôÉ•Í½±Ù•}™½Ñµ½‰}Ñ•…µ}¥¡Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡˜‰íÑ•…µ}¹…µ•ôèí•ôˆ¤(€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¥è(€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡˜‰íÑ•…µ}¹…µ•ôèÑ•…´¹½Ð™½Õ¹ˆ¤(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤…¹¹½Ð™…Ù½É¥Ñ”¹•Ð ‰Ñ•…µ}¥ˆ¤è(€€€€€€€€€€€€€€€€€€€™…Ù½É¥Ñ•l‰Ñ•…µ}¥‰t€ôÑ•…µ}¥(€€€€€€€€€€€€€€€€€€€¡…¹•€ôQÉÕ”(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€™•Ñ¡}Ñ•…µ}Í¡•‘Õ±”¡Ñ•…µ}¥°Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€€€€€É•™É•Í¡•€¬ô€Ä(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡˜‰íÑ•…µ}¹…µ•ôèí•ôˆ¤(€€€€€€€€€€€¥˜¡…¹•è(€€€€€€€€€€€€€€€Í…Ù•}™…Ù½É¥Ñ•Ì¡™…Ù}‘…Ñ„¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰Ñ•…µÌˆèÉ•™É•Í¡•°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•ÉÉ½ÉÌˆè•ÉÉ½ÉÍô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½É•™É•Í¡}™½½Ñ‰…±°ˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€}%1e}5Q!}!¹ÕÁ‘…Ñ”¡ì‰‘…Ñ”ˆè€ˆˆ°€‰ÑÌˆè€À°€‰µ…Ñ¡•Ìˆèmuô¤(€€€€€€€€€€€€€€€}QY}!¹±•…È ¤(€€€€€€€€€€€€€€€}1QY}!¹±•…È ¤(€€€€€€€€€€€€€€€}É•µ½Ù•}‘…Ñ…}…¡•}ÁÉ•™¥à ‰™½Ñµ½ˆµ‘…¥±äˆ¤(€€€€€€€€€€€€€€€}É•µ½Ù•}‘…Ñ…}…¡•}ÁÉ•™¥à ‰±ÑØµ‘…¥±ä´ˆ¤(€€€€€€€€€€€€€€€‘…¥±ä€ô™•Ñ¡}™½Ñµ½‰}‘…¥±å}µ…Ñ¡•Ì ¤(€€€€€€€€€€€€€€€Õ¥‘•Ì€ô€À(€€€€€€€€€€€€€€€±¥ÍÑ¥¹}Í½ÕÉ”€ô€‰1QXˆ(€€€€€€€€€€€€€€€±¥ÍÑ¥¹}¹½Ñ¥”€ô€ˆˆ(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€™•Ñ¡}±ÑÙ}‘…¥±ä¡‘…Ñ•Ñ¥µ”¹‘…Ñ”¹Ñ½‘…ä ¤¹¥Í½™½Éµ…Ð ¤¤(€€€€€€€€€€€€€€€€€€€Õ¥‘•Ì€ô€Ä(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€€€€€€€€€€€€€±¥ÍÑ¥¹}Í½ÕÉ”€ô€‰½Ñ5½ˆ™…±±‰…¬ˆ(€€€€€€€€€€€€€€€€€€€±¥ÍÑ¥¹}¹½Ñ¥”€ô€‰1¥Ù”M½•ÈQX¡…¹¹•°±¥ÍÑ¥¹ÌÕ¹…Ù…¥±…‰±”ƒŠPÕÍ¥¹œ½Ñ5½ˆ¡…¹¹•°±¥ÍÑ¥¹Ìˆ(€€€€€€€€€€€€€€€€€€€}É•µ½Ù•}‘…Ñ…}…¡•}ÁÉ•™¥à ‰ÑØµÕ¥‘”´ˆ¤(€€€€€€€€€€€€€€€€€€€™½È½Õ¹ÑÉä¥¸=Q5=	}11	-}=U9QI%Lè(€€€€€€€€€€€€€€€€€€€€€€€™•Ñ¡}½Õ¹ÑÉå}™¥áÑÕÉ•Ì¡½Õ¹ÑÉä¤(€€€€€€€€€€€€€€€€€€€€€€€Õ¥‘•Ì€¬ô€Ä(€€€€€€€€€€€€€€€™…Ù}‘…Ñ„€ô±½…‘}™…Ù½É¥Ñ•Ì ¤(€€€€€€€€€€€€€€€}Q5}%aQUI}!¹±•…È ¤(€€€€€€€€€€€€€€€}Q5}AI=%1}!¹±•…È ¤(€€€€€€€€€€€€€€€}É•µ½Ù•}‘…Ñ…}…¡•}ÁÉ•™¥à ‰Ñ•…´µ™¥áÑÕÉ•Ì´ˆ¤(€€€€€€€€€€€€€€€}É•µ½Ù•}‘…Ñ…}…¡•}ÁÉ•™¥à¡˜‰Ñ•…´µÁÉ½™¥±”µÙí}Q5}AI=%1}!}M!5ô´ˆ¤(€€€€€€€€€€€€€€€Ñ•…µÌ€ô€À(€€€€€€€€€€€€€€€•ÉÉ½ÉÌ€ômt(€€€€€€€€€€€€€€€¡…¹•€ô…±Í”(€€€€€€€€€€€€€€€™½È™…Ù½É¥Ñ”¥¸™…Ù}‘…Ñ„¹•Ð ‰Ñ•…µÌˆ°mt¤è(€€€€€€€€€€€€€€€€€€€Ñ•…µ}¹…µ”€ôÍÑÈ¡™…Ù½É¥Ñ”¹•Ð ‰¹…µ”ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤•±Í”™…Ù½É¥Ñ”¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¹…µ”è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€Ñ•…µ}¥€ôÍÑÈ¡™…Ù½É¥Ñ”¹•Ð ‰Ñ•…µ}¥ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤•±Í”€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¥è(€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñ•…µ}¥€ôÉ•Í½±Ù•}™½Ñµ½‰}Ñ•…µ}¥¡Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡˜‰íÑ•…µ}¹…µ•ôèí•ôˆ¤(€€€€€€€€€€€€€€€€€€€¥˜¹½ÐÑ•…µ}¥è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡™…Ù½É¥Ñ”°‘¥Ð¤…¹¹½Ð™…Ù½É¥Ñ”¹•Ð ‰Ñ•…µ}¥ˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€™…Ù½É¥Ñ•l‰Ñ•…µ}¥‰t€ôÑ•…µ}¥(€€€€€€€€€€€€€€€€€€€€€€€¡…¹•€ôQÉÕ”(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€™•Ñ¡}Ñ•…µ}Í¡•‘Õ±”¡Ñ•…µ}¥°Ñ•…µ}¹…µ”¤(€€€€€€€€€€€€€€€€€€€€€€€Ñ•…µÌ€¬ô€Ä(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡˜‰íÑ•…µ}¹…µ•ôèí•ôˆ¤(€€€€€€€€€€€€€€€¥˜¡…¹•è(€€€€€€€€€€€€€€€€€€€Í…Ù•}™…Ù½É¥Ñ•Ì¡™…Ù}‘…Ñ„¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰Ñ•…µÌˆèÑ•…µÌ°€‰Õ¥‘•ÌˆèÕ¥‘•Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µ…Ñ¡•Ìˆè±•¸¡‘…¥±ä¤°€‰•ÉÉ½ÉÌˆè•ÉÉ½ÉÌ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±¥ÍÑ¥¹}Í½ÕÉ”ˆè±¥ÍÑ¥¹}Í½ÕÉ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±¥ÍÑ¥¹}¹½Ñ¥”ˆè±¥ÍÑ¥¹}¹½Ñ¥•ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½¡•­}µ½Ù¥•}ÕÁ‘…Ñ•Ìˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰9½Ð½¹™¥ÕÉ•‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÁÉ•Ù¥½ÕÌ€ô€¡}Y=}!¹•Ð ‰µ½Ù¥•Ìˆ¤½È}±½…‘}Ù½‘}…Ñ…±½}…¡”¡à¤¤(€€€€€€€€€€€€€€€ÁÉ•Ù¥½ÕÍ}¥‘Ì€ôíÍÑÈ¡É½Ü¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½ÈÉ½Ü¥¸ÁÉ•Ù¥½ÕÌ(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡É½Ü°‘¥Ð¤…¹É½Ü¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¥Ì¹½Ð9½¹•ô(€€€€€€€€€€€€€€€™É•Í €ôà¹Ù½‘}ÍÑÉ•…µÌ ¤(€€€€€€€€€€€€€€€¥˜¹½Ð™É•Í è(€€€€€€€€€€€€€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È ‰AÉ½Ù¥‘•ÈÉ•ÑÕÉ¹•…¸•µÁÑäµ½Ù¥”…Ñ…±½œˆ¤(€€€€€€€€€€€€€€€µ½Ù¥•Ì€ô}Í…Ù•}Ù½‘}…Ñ…±½}…¡”¡à°™É•Í ¤(€€€€€€€€€€€€€€€}Y=}!¹ÕÁ‘…Ñ”¡ì‰ÁÉ½Ù¥‘•Èˆè}Ù½‘}…¡•}­•ä¡à¤°€‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°€‰µ½Ù¥•Ìˆèµ½Ù¥•Íô¤(€€€€€€€€€€€€€€€™É•Í¡}¥‘Ì€ôíÍÑÈ¡É½Ü¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½ÈÉ½Ü¥¸µ½Ù¥•Ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜É½Ü¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¥Ì¹½Ð9½¹•ô(€€€€€€€€€€€€€€€¹•Ý}µ½Ù¥•Ì€ô±•¸¡™É•Í¡}¥‘Ì€´ÁÉ•Ù¥½ÕÍ}¥‘Ì¤¥˜ÁÉ•Ù¥½ÕÍ}¥‘Ì•±Í”€À(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰µ½Ù¥•Ìˆè±•¸¡µ½Ù¥•Ì¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹•Ý}µ½Ù¥•Ìˆè¹•Ý}µ½Ù¥•Íô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½É•™É•Í¡}áÑÉ•…´ˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰aÑÉ•…´¥Ì¹½Ð½¹™¥ÕÉ•‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¡…¹¹•±Ì°}…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ°™½É”õQÉÕ”¤(€€€€€€€€€€€€€€€}±•…É}ÍÁ½ÉÑÍ}•Ù•¹Ñ}¡…¹¹•±}…¡” ¤(€€€€€€€€€€€€€€€µ½Ù¥•Ì€ô•Ñ}áÑÉ•…µ}µ½Ù¥•Ì¡™œ°™½É”õQÉÕ”¤(€€€€€€€€€€€€€€€Í¡½ÝÌ€ô•Ñ}áÑÉ•…µ}Í•É¥•Ì¡™œ°™½É”õQÉÕ”¤(€€€€€€€€€€€€€€€•Á¥Í½‘•}É•ÍÕ±Ð€ôÉ•™É•Í¡}™…Ù½É¥Ñ•}Í¡½Ý}•Á¥Í½‘•Ì¡™œ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°‘¥Ð¡ì‰½¬ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€‰¡…¹¹•±Ìˆè±•¸¡¡…¹¹•±Ì¤°€‰µ½Ù¥•Ìˆè±•¸¡µ½Ù¥•Ì¤°(€€€€€€€€€€€€€€€€€€€€‰Í¡½ÝÌˆè±•¸¡Í¡½ÝÌ¥ô°€¨©•Á¥Í½‘•}É•ÍÕ±Ð¤¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½É•™É•Í¡}É…¥¹œˆè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€Í•±•Ñ•€ô™œ¹•Ð ‰É…¥¹}Í•É¥•Ìˆ°l‰˜Ä‰t¤(€€€€€€€€€€€€€€€}±•…É}É…¥¹}…Ù…¥±…‰¥±¥Ñå}…¡” ¤(€€€€€€€€€€€€€€€•Ù•¹ÑÌ€ô•Ñ}É…¥¹}•Ù•¹ÑÌ¡Í•±•Ñ•°™½É”õQÉÕ”¤(€€€€€€€€€€€€€€€¥˜€‰˜Äˆ¥¸Í•±•Ñ•è(€€€€€€€€€€€€€€€€€€€•Ñ}˜Å}Ñ•…µÌ¡™½É”õQÉÕ”¤(€€€€€€€€€€€€€€€•Ñ}É…¥¹}‘É¥Ù•ÉÌ¡™½É”õQÉÕ”¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰Í•É¥•Ìˆè±•¸¡Í•±•Ñ•¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•Ù•¹ÑÌˆè±•¸¡•Ù•¹ÑÌ¥ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÈ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Ñ•ÍÑ}Í½ÕÉ”ˆè(€€€€€€€€€€€­•ä€ôÍÑÈ¡Á…å±½…¹•Ð ‰­•äˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€¥˜­•ä¹½Ð¥¸}M=UI}1	1}5@è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰Õ¹­¹½Ý¸Í½ÕÉ”‰ô¤(€€€€€€€€€€€É•ÍÕ±Ð€ôÑ•ÍÑ}•áÑ•É¹…±}Í½ÕÉ”¡­•ä¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰É•ÍÕ±ÐˆèÉ•ÍÕ±Ð°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•ÌˆèÍ½ÕÉ•}¡•…±Ñ¡}Í¹…ÁÍ¡½Ð ¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½™…Ù½É¥Ñ•Ìˆè(€€€€€€€€€€€€Œ…Ñ¥½¹Ìè…Ñ•½Éä½¡…¹¹•°½µ½Ù¥”™…Ù½É¥Ñ”µ…¹…•µ•¹Ð…¹É•½É‘•É¥¹œ(€€€€€€€€€€€™…Ø€ô±½…‘}™…Ù½É¥Ñ•Ì ¤(€€€€€€€€€€€…Ð€ôÁ…å±½…¹•Ð ‰…Ñ¥½¸ˆ°€ˆˆ¤(€€€€€€€€€€€¥˜…Ð€ôô€‰…‘‘}…ÑÌˆè(€€€€€€€€€€€€€€€™½ÈŒ¥¸Á…å±½…¹•Ð ‰…Ñ•½É¥•Ìˆ°mt¤è(€€€€€€€€€€€€€€€€€€€¥˜Œ…¹Œ¹½Ð¥¸™…Ùl‰…Ñ•½É¥•Ì‰tè(€€€€€€€€€€€€€€€€€€€€€€€™…Ùl‰…Ñ•½É¥•Ì‰t¹…ÁÁ•¹¡Œ¤(€€€€€€€€€€€•±¥˜…Ð€ôô€‰É•µ½Ù•}…Ðˆè(€€€€€€€€€€€€€€€™…Ùl‰…Ñ•½É¥•Ì‰t€ômŒ™½ÈŒ¥¸™…Ùl‰…Ñ•½É¥•Ì‰t¥˜Œ€„ôÁ…å±½…¹•Ð ‰…Ñ•½Éäˆ¥t(€€€€€€€€€€€•±¥˜…Ð€ôô€‰…‘‘}¡…¹¹•±Ìˆè(€€€€€€€€€€€€€€€¡…Ù”€ôíÍÑÈ¡Œ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½ÈŒ¥¸™…Ùl‰¡…¹¹•±Ì‰uô(€€€€€€€€€€€€€€€™½È ¥¸Á…å±½…¹•Ð ‰¡…¹¹•±Ìˆ°mt¤è(€€€€€€€€€€€€€€€€€€€Í¥€ôÍÑÈ¡ ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤(€€€€€€€€€€€€€€€€€€€¥˜Í¥…¹Í¥¹½Ð¥¸¡…Ù”è(€€€€€€€€€€€€€€€€€€€€€€€™…Ùl‰¡…¹¹•±Ì‰t¹…ÁÁ•¹¡ì‰ÍÑÉ•…µ}¥ˆè ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆè ¹•Ð ‰¹…µ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ•½Éäˆè ¹•Ð ‰…Ñ•½Éäˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±½¼ˆè ¹•Ð ‰±½¼ˆ¤½È}ÍÑÉ•…µ}¥½¹}™½É}¥¡Í¥¥ô¤(€€€€€€€€€€€€€€€€€€€€€€€¡…Ù”¹…‘¡Í¥¤(€€€€€€€€€€€•±¥˜…Ð€ôô€‰Ñ½±•}¡…¹¹•°ˆè(€€€€€€€€€€€€€€€Í¥€ôÍÑÈ¡Á…å±½…¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤(€€€€€€€€€€€€€€€¥‘à€ô¹•áÐ ¡¤™½È¤°Œ¥¸•¹Õµ•É…Ñ”¡™…Ùl‰¡…¹¹•±Ì‰t¤¥˜ÍÑÈ¡Œ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤€ôôÍ¥¤°€´Ä¤(€€€€€€€€€€€€€€€¥˜¥‘à€øô€Àè(€€€€€€€€€€€€€€€€€€€™…Ùl‰¡…¹¹•±Ì‰t¹Á½À¡¥‘à¤(€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€™…Ùl‰¡…¹¹•±Ì‰t¹…ÁÁ•¹¡ì‰ÍÑÉ•…µ}¥ˆèÁ…å±½…¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆèÁ…å±½…¹•Ð ‰¹…µ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ•½ÉäˆèÁ…å±½…¹•Ð ‰…Ñ•½Éäˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±½¼ˆèÁ…å±½…¹•Ð ‰±½¼ˆ¤½È}ÍÑÉ•…µ}¥½¹}™½É}¥¡Í¥¥ô¤(€€€€€€€€€€€•±¥˜…Ð€ôô€‰É•µ½Ù•}¡…¹¹•°ˆè(€€€€€€€€€€€€€€€Í¥€ôÍÑÈ¡Á…å±½…¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤(€€€€€€€€€€€€€€€™…Ùl‰¡…¹¹•±Ì‰t€ômŒ™½ÈŒ¥¸™…Ùl‰¡…¹¹•±Ì‰t¥˜ÍÑÈ¡Œ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤€„ôÍ¥‘t(€€€€€€€€€€€•±¥˜…Ð€ôô€‰É•½É‘•É}¡…¹¹•±Ìˆè(€€€€€€€€€€€€€€€É•ÅÕ•ÍÑ•€ômÍÑÈ¡Í¥¤™½ÈÍ¥¥¸Á…å±½…¹•Ð ‰ÍÑÉ•…µ}¥‘Ìˆ°mt¥t(€€€€€€€€€€€€€€€‰å}¥€ôíÍÑÈ¡Œ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤èŒ™½ÈŒ¥¸™…Ùl‰¡…¹¹•±Ì‰uô(€€€€€€€€€€€€€€€É•½É‘•É•€ôm‰å}¥¹Á½À¡Í¥¤™½ÈÍ¥¥¸É•ÅÕ•ÍÑ•¥˜Í¥¥¸‰å}¥‘t(€€€€€€€€€€€€€€€€ŒAÉ•Í•ÉÙ”…¹ä¡…¹¹•±Ì…‘‘•½¹ÕÉÉ•¹Ñ±ä½È½µ¥ÑÑ•‰ä…¸½±±¥•¹Ð¸(€€€€€€€€€€€€€€€É•½É‘•É•¹•áÑ•¹¡Œ™½ÈŒ¥¸™…Ùl‰¡…¹¹•±Ì‰t¥˜ÍÑÈ¡Œ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤¥¸‰å}¥¤(€€€€€€€€€€€€€€€™…Ùl‰¡…¹¹•±Ì‰t€ôÉ•½É‘•É•(€€€€€€€€€€€•±¥˜…Ð€ôô€‰Í•Ñ}µå±¥ÍÑ}¡…¹¹•±Ìˆè(€€€€€€€€€€€€€€€™…Ù½É¥Ñ•}¥‘Ì€ôíÍÑÈ¡Œ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤™½ÈŒ¥¸™…Ùl‰¡…¹¹•±Ì‰uô(€€€€€€€€€€€€€€€¡½Í•¸€ômt(€€€€€€€€€€€€€€€™½ÈÍ¥¥¸Á…å±½…¹•Ð ‰ÍÑÉ•…µ}¥‘Ìˆ°mt¤è(€€€€€€€€€€€€€€€€€€€Í¥€ôÍÑÈ¡Í¥¤(€€€€€€€€€€€€€€€€€€€¥˜Í¥¥¸™…Ù½É¥Ñ•}¥‘Ì…¹Í¥¹½Ð¥¸¡½Í•¸è(€€€€€€€€€€€€€€€€€€€€€€€¡½Í•¸¹…ÁÁ•¹¡Í¥¤(€€€€€€€€€€€€€€€€€€€¥˜±•¸¡¡½Í•¸¤€øô€Ôè(€€€€€€€€€€€€€€€€€€€€€€€‰É•…¬(€€€€€€€€€€€€€€€™…Ùl‰µå±¥ÍÑ}¡…¹¹•±Ì‰t€ô¡½Í•¸(€€€€€€€€€€€•±¥˜…Ð€ôô€‰Ñ½±•}µ½Ù¥”ˆè(€€€€€€€€€€€€€€€µ½Ù¥”€ôÁ…å±½…¹•Ð ‰µ½Ù¥”ˆ¤½Èíô(€€€€€€€€€€€€€€€…Ñ…±½}¥€ôÍÑÈ¡µ½Ù¥”¹•Ð ‰…Ñ…±½}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜¹½Ð…Ñ…±½}¥…¹µ½Ù¥”¹•Ð ‰¹…µ”ˆ¤è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€Ý…¹Ñ•‘}¹…µ”€ô}±•…¹}Í¡½Ý}Ñ¥Ñ±”¡µ½Ù¥”¹•Ð ‰¹…µ”ˆ¤¤½ÈÍÑÈ¡µ½Ù¥”¹•Ð ‰¹…µ”ˆ¤¤(€€€€€€€€€€€€€€€€€€€€€€€Ý…¹Ñ•‘}å•…È€ôÍÑÈ¡µ½Ù¥”¹•Ð ‰å•…Èˆ¤½È}ÁÉ½Ù¥‘•É}å•…È¡µ½Ù¥”¤½È€ˆˆ¤(€€€€€€€€€€€€€€€€€€€€€€€µ…Ñ¡•Ì€ômÉ½Ü™½ÈÉ½Ü¥¸¥¹•µ•Ñ…}Í•…É  ‰µ½Ù¥”ˆ°Ý…¹Ñ•‘}¹…µ”¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜}Í¡½Ý}­•ä¡É½Ü¹•Ð ‰¹…µ”ˆ¤¤€ôô}Í¡½Ý}­•ä¡Ý…¹Ñ•‘}¹…µ”¥t(€€€€€€€€€€€€€€€€€€€€€€€¡½Í•¸€ô¹•áÐ ¡É½Ü™½ÈÉ½Ü¥¸µ…Ñ¡•Ì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜Ý…¹Ñ•‘}å•…È…¹}…Ñ…±½}å•…È¡É½Ü¤€ôôÝ…¹Ñ•‘}å•…È¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€µ…Ñ¡•ÍlÁt¥˜µ…Ñ¡•Ì•±Í”9½¹”¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜¡½Í•¸è(€€€€€€€€€€€€€€€€€€€€€€€€€€€…Ñ…±½}¥€ôÍÑÈ¡¡½Í•¸¹•Ð ‰¥ˆ¤½È€ˆˆ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ½Ù¥”€ô‘¥Ð¡µ½Ù¥”¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ½Ù¥•l‰…Ñ…±½}¥‰t€ô…Ñ…±½}¥(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ½Ù¥•l‰¹…µ”‰t€ô¡½Í•¸¹•Ð ‰¹…µ”ˆ¤½ÈÝ…¹Ñ•‘}¹…µ”(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ½Ù¥•l‰å•…È‰t€ô}…Ñ…±½}å•…È¡¡½Í•¸¤½ÈÝ…¹Ñ•‘}å•…È(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ½Ù¥•l‰½Ù•È‰t€ô¡½Í•¸¹•Ð ‰Á½ÍÑ•Èˆ¤½Èµ½Ù¥”¹•Ð ‰½Ù•Èˆ¤½È€ˆˆ(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€Í¥€ôÍÑÈ¡µ½Ù¥”¹•Ð ‰ÍÑÉ•…µ}¥ˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€™…Ù½É¥Ñ•}­•ä€ô…Ñ…±½}¥½ÈÍ¥(€€€€€€€€€€€€€€€¥‘à€ô€´Ä(€€€€€€€€€€€€€€€™½È¤°•á¥ÍÑ¥¹œ¥¸•¹Õµ•É…Ñ”¡™…Ùl‰µ½Ù¥•Ì‰t¤è(€€€€€€€€€€€€€€€€€€€Í…µ•}¥€ôÍÑÈ¡•á¥ÍÑ¥¹œ¹•Ð ‰…Ñ…±½}¥ˆ¤½È•á¥ÍÑ¥¹œ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤€ôô™…Ù½É¥Ñ•}­•ä(€€€€€€€€€€€€€€€€€€€Í…µ•}Ñ¥Ñ±”€ô}Í¡½Ý}­•ä¡•á¥ÍÑ¥¹œ¹•Ð ‰¹…µ”ˆ¤¤€ôô}Í¡½Ý}­•ä¡µ½Ù¥”¹•Ð ‰¹…µ”ˆ¤¤(€€€€€€€€€€€€€€€€€€€Í…µ•}å•…È€ô€¡¹½Ðµ½Ù¥”¹•Ð ‰å•…Èˆ¤½È¹½Ð•á¥ÍÑ¥¹œ¹•Ð ‰å•…Èˆ¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡•á¥ÍÑ¥¹œ¹•Ð ‰å•…Èˆ¤¤€ôôÍÑÈ¡µ½Ù¥”¹•Ð ‰å•…Èˆ¤¤¤(€€€€€€€€€€€€€€€€€€€¥˜Í…µ•}¥½È€¡Í…µ•}Ñ¥Ñ±”…¹Í…µ•}å•…È¤è(€€€€€€€€€€€€€€€€€€€€€€€¥‘à€ô¤(€€€€€€€€€€€€€€€€€€€€€€€‰É•…¬(€€€€€€€€€€€€€€€¥˜¥‘à€øô€Àè(€€€€€€€€€€€€€€€€€€€™…Ùl‰µ½Ù¥•Ì‰t¹Á½À¡¥‘à¤(€€€€€€€€€€€€€€€•±¥˜™…Ù½É¥Ñ•}­•äè(€€€€€€€€€€€€€€€€€€€É•±•…Í•€ôÍÑÈ¡µ½Ù¥”¹•Ð ‰É•±•…Í•ˆ¤½È€ˆˆ¤(€€€€€€€€€€€€€€€€€€€¥˜…Ñ…±½}¥…¹¹½ÐÉ•±•…Í•è(€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€É•±•…Í•€ôÍÑÈ¡¥¹•µ•Ñ…}µ•Ñ„ ‰µ½Ù¥”ˆ°…Ñ…±½}¥¤¹•Ð ‰É•±•…Í•ˆ¤½È€ˆˆ¤(€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€€€€€€€€€É•±•…Í•€ô€ˆˆ(€€€€€€€€€€€€€€€€€€€™…Ùl‰µ½Ù¥•Ì‰t¹…ÁÁ•¹¡ì(€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ…±½}¥ˆè…Ñ…±½}¥°(€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}¥ˆèµ½Ù¥”¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆèµ½Ù¥”¹•Ð ‰¹…µ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•¹Í¥½¸ˆèµ½Ù¥”¹•Ð ‰•áÑ•¹Í¥½¸ˆ°€‰µÀÐˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰å•…Èˆèµ½Ù¥”¹•Ð ‰å•…Èˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰É…Ñ¥¹œˆèµ½Ù¥”¹•Ð ‰É…Ñ¥¹œˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•Èˆèµ½Ù¥”¹•Ð ‰½Ù•Èˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€‰É•±•…Í•ˆèÉ•±•…Í•°(€€€€€€€€€€€€€€€€€€€ô¤(€€€€€€€€€€€€€€€€€€€‘•µ½}™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€€€€€¥˜‘•µ½}™œ¹•Ð ‰Í•ÑÕÁ}‘•µ½}½¹Ñ•¹Ðˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€‘•µ½}™l‰Í•ÑÕÁ}‘•µ½}½¹Ñ•¹Ð‰t€ô…±Í”(€€€€€€€€€€€€€€€€€€€€€€€Í…Ù•}½¹™¥œ¡‘•µ½}™œ¤(€€€€€€€€€€€•±¥˜…Ð€ôô€‰É•µ½Ù•}µ½Ù¥”ˆè(€€€€€€€€€€€€€€€Í¥€ôÍÑÈ¡Á…å±½…¹•Ð ‰™…Ù½É¥Ñ•}­•äˆ¤½ÈÁ…å±½…¹•Ð ‰ÍÑÉ•…µ}¥ˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€™…Ùl‰µ½Ù¥•Ì‰t€ôm´™½È´¥¸™…Ùl‰µ½Ù¥•Ì‰t(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÍÑÈ¡´¹•Ð ‰…Ñ…±½}¥ˆ¤½È´¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤¤€„ôÍ¥‘t(€€€€€€€€€€€•±¥˜…Ð€ôô€‰Ñ½±•}Í¡½Üˆè(€€€€€€€€€€€€€€€Í¡½Ü€ôÁ…å±½…¹•Ð ‰Í¡½Üˆ¤½Èíô(€€€€€€€€€€€€€€€…Ñ…±½}¥€ôÍÑÈ¡Í¡½Ü¹•Ð ‰…Ñ…±½}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€Ñ¥Ñ±•}­•ä€ôÍÑÈ¡Í¡½Ü¹•Ð ‰Í¡½Ý}­•äˆ¤½È}Í¡½Ý}­•ä¡Í¡½Ü¹•Ð ‰¹…µ”ˆ¤¤½È€ˆˆ¤(€€€€€€€€€€€€€€€­•ä€ôÍÑÈ¡…Ñ…±½}¥½ÈÍ¡½Ü¹•Ð ‰Í¡½Ý}­•äˆ¤½È}Í¡½Ý}­•ä¡Í¡½Ü¹•Ð ‰¹…µ”ˆ¤¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€Í¡½Ü¹•Ð ‰Í•É¥•Í}¥ˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€¥‘à€ô¹•áÐ ¡¤™½È¤°Ì¥¸•¹Õµ•É…Ñ”¡™…Ùl‰Í¡½ÝÌ‰t¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜€¡ÍÑÈ¡Ì¹•Ð ‰…Ñ…±½}¥ˆ¤½ÈÌ¹•Ð ‰Í¡½Ý}­•äˆ¤½È}Í¡½Ý}­•ä¡Ì¹•Ð ‰¹…µ”ˆ¤¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ì¹•Ð ‰Í•É¥•Í}¥ˆ¤¤€ôô­•ä½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¡Ñ¥Ñ±•}­•ä…¹ÍÑÈ¡Ì¹•Ð ‰Í¡½Ý}­•äˆ¤½È}Í¡½Ý}­•ä¡Ì¹•Ð ‰¹…µ”ˆ¤¤¤€ôôÑ¥Ñ±•}­•ä¤¤¤°€´Ä¤(€€€€€€€€€€€€€€€¥˜¥‘à€øô€Àè(€€€€€€€€€€€€€€€€€€€™…Ùl‰Í¡½ÝÌ‰t¹Á½À¡¥‘à¤(€€€€€€€€€€€€€€€•±¥˜­•äè(€€€€€€€€€€€€€€€€€€€¥‘Ì€ômÍ¥™½ÈÍ¥¥¸€¡Í¡½Ü¹•Ð ‰Í•É¥•Í}¥‘Ìˆ¤½ÈmÍ¡½Ü¹•Ð ‰Í•É¥•Í}¥ˆ¥t¤¥˜Í¥¹½Ð¥¸€¡9½¹”°€ˆˆ¥t(€€€€€€€€€€€€€€€€€€€™…Ùl‰Í¡½ÝÌ‰t¹…ÁÁ•¹¡ì‰…Ñ…±½}¥ˆè…Ñ…±½}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥ˆè¥‘ÍlÁt¥˜¥‘Ì•±Í”9½¹”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í•É¥•Í}¥‘Ìˆè¥‘Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í¡½Ý}­•äˆèÑ¥Ñ±•}­•ä°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆèÍ¡½Ü¹•Ð ‰¹…µ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ù•ÈˆèÍ¡½Ü¹•Ð ‰½Ù•Èˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰å•…ÈˆèÍ¡½Ü¹•Ð ‰å•…Èˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É…Ñ¥¹œˆèÍ¡½Ü¹•Ð ‰É…Ñ¥¹œˆ°€ˆˆ¥ô¤(€€€€€€€€€€€€€€€€€€€‘•µ½}™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€€€€€€€€€¥˜‘•µ½}™œ¹•Ð ‰Í•ÑÕÁ}‘•µ½}½¹Ñ•¹Ðˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€‘•µ½}™l‰Í•ÑÕÁ}‘•µ½}½¹Ñ•¹Ð‰t€ô…±Í”(€€€€€€€€€€€€€€€€€€€€€€€Í…Ù•}½¹™¥œ¡‘•µ½}™œ¤(€€€€€€€€€€€€€€€}¥¹Ù…±¥‘…Ñ•}±…Ñ•ÍÑ}•Á¥Í½‘•Í}…¡” ¤(€€€€€€€€€€€•±¥˜…Ð€ôô€‰É•µ½Ù•}Í¡½Üˆè(€€€€€€€€€€€€€€€­•ä€ôÍÑÈ¡Á…å±½…¹•Ð ‰Í¡½Ý}­•äˆ¤½ÈÁ…å±½…¹•Ð ‰Í•É¥•Í}¥ˆ°€ˆˆ¤¤(€€€€€€€€€€€€€€€™…Ùl‰Í¡½ÝÌ‰t€ômÌ™½ÈÌ¥¸™…Ùl‰Í¡½ÝÌ‰t(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÍÑÈ¡Ì¹•Ð ‰…Ñ…±½}¥ˆ¤½ÈÌ¹•Ð ‰Í¡½Ý}­•äˆ¤½È}Í¡½Ý}­•ä¡Ì¹•Ð ‰¹…µ”ˆ¤¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ì¹•Ð ‰Í•É¥•Í}¥ˆ¤¤€„ô­•åt(€€€€€€€€€€€€€€€}¥¹Ù…±¥‘…Ñ•}±…Ñ•ÍÑ}•Á¥Í½‘•Í}…¡” ¤(€€€€€€€€€€€•±¥˜…Ð€ôô€‰Ñ½±•}Ñ•…´ˆè(€€€€€€€€€€€€€€€Ñ•…´€ôÁ…å±½…¹•Ð ‰Ñ•…´ˆ¤½Èíô(€€€€€€€€€€€€€€€¹…µ”€ôÍÑÈ¡Ñ•…´¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥‘à€ô¹•áÐ ¡¤™½È¤°¥Ñ•´¥¸•¹Õµ•É…Ñ”¡™…Ùl‰Ñ•…µÌ‰t¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÍÑÈ¡¥Ñ•´¹•Ð ‰¹…µ”ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤•±Í”¥Ñ•´¤¹±½Ý•È ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€ôô¹…µ”¹±½Ý•È ¤¤°€´Ä¤(€€€€€€€€€€€€€€€¥˜¥‘à€øô€Àè(€€€€€€€€€€€€€€€€€€€™…Ùl‰Ñ•…µÌ‰t¹Á½À¡¥‘à¤(€€€€€€€€€€€€€€€•±¥˜¹…µ”è(€€€€€€€€€€€€€€€€€€€Ñ•…µ}¥€ôÍÑÈ¡Ñ•…´¹•Ð ‰Ñ•…µ}¥ˆ¤½È€ˆˆ¤(€€€€€€€€€€€€€€€€€€€™…Ùl‰Ñ•…µÌ‰t¹…ÁÁ•¹¡ì‰¹…µ”ˆè¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ•…µ}¥ˆèÑ•…µ}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±½¼ˆè}Ñ•…µ}±½½}ÕÉ°¡Ñ•…µ}¥¥ô¤(€€€€€€€€€€€•±¥˜…Ð€ôô€‰É•µ½Ù•}Ñ•…´ˆè(€€€€€€€€€€€€€€€¹…µ”€ôÍÑÈ¡Á…å±½…¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤¹±½Ý•È ¤(€€€€€€€€€€€€€€€™…Ùl‰Ñ•…µÌ‰t€ôm¥Ñ•´™½È¥Ñ•´¥¸™…Ùl‰Ñ•…µÌ‰t(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜ÍÑÈ¡¥Ñ•´¹•Ð ‰¹…µ”ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤•±Í”¥Ñ•´¤¹±½Ý•È ¤(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€„ô¹…µ•t(€€€€€€€€€€€•±¥˜…Ð€ôô€‰Í•Ñ}˜Å}Ñ•…´ˆè(€€€€€€€€€€€€€€€Ñ•…´€ôÁ…å±½…¹•Ð ‰Ñ•…´ˆ¤½Èíô(€€€€€€€€€€€€€€€½¹ÍÑÉÕÑ½É}¥€ôÉ”¹ÍÕˆ¡È‰mxÀ´åµi„µé|µtˆ°€ˆˆ°ÍÑÈ¡Ñ•…´¹•Ð ‰¥ˆ¤½È€ˆˆ¤¤(€€€€€€€€€€€€€€€¹…µ”€ôÍÑÈ¡Ñ•…´¹•Ð ‰¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜½¹ÍÑÉÕÑ½É}¥…¹¹…µ”è(€€€€€€€€€€€€€€€€€€€™…Ùl‰˜Å}Ñ•…µÌ‰t€ômì‰¥ˆè½¹ÍÑÉÕÑ½É}¥°€‰¹…µ”ˆè¹…µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±½¼ˆè}˜Å}±½½}ÕÉ°¡½¹ÍÑÉÕÑ½É}¥¥õt(€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€™…Ùl‰˜Å}Ñ•…µÌ‰t€ômt(€€€€€€€€€€€Í…Ù•}™…Ù½É¥Ñ•Ì¡™…Ø¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ•½É¥•Ìˆè™…Ùl‰…Ñ•½É¥•Ì‰t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¡…¹¹•±}¥‘ÌˆèmŒ¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤™½ÈŒ¥¸™…Ùl‰¡…¹¹•±Ì‰ut°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µ½Ù¥•}¥‘Ìˆèm´¹•Ð ‰…Ñ…±½}¥ˆ¤½È´¹•Ð ‰ÍÑÉ•…µ}¥ˆ¤™½È´¥¸™…Ùl‰µ½Ù¥•Ì‰ut°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Í¡½Ý}¥‘ÌˆèmÌ¹•Ð ‰…Ñ…±½}¥ˆ¤½ÈÌ¹•Ð ‰Í¡½Ý}­•äˆ¤½È}Í¡½Ý}­•ä¡Ì¹•Ð ‰¹…µ”ˆ¤¤½È(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ì¹•Ð ‰Í•É¥•Í}¥ˆ¤™½ÈÌ¥¸™…Ùl‰Í¡½ÝÌ‰ut°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ•…µ}¹…µ•Ìˆèm¥Ñ•´¹•Ð ‰¹…µ”ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤•±Í”¥Ñ•´(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€™½È¥Ñ•´¥¸™…Ùl‰Ñ•…µÌ‰ut°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…µ•}¥‘Ìˆèm¥Ñ•´¹•Ð ‰…ÁÁ}¥ˆ¤™½È¥Ñ•´¥¸™…Ø¹•Ð ‰…µ•Ìˆ°mt¥t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰˜Å}Ñ•…µÌˆè™…Ø¹•Ð ‰˜Å}Ñ•…µÌˆ°mt¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÕÁ‘…Ñ•}‘½Ý¹±½…ˆè(€€€€€€€€€€€Á…Ñ €ô‘½Ý¹±½…‘}ÕÁ‘…Ñ” ¤(€€€€€€€€€€€¥˜Á…Ñ è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ•ô¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½Èˆè€‰‘½Ý¹±½…™…¥±•‰ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½ÕÁ‘…Ñ•}É•ÍÑ…ÉÐˆè(€€€€€€€€€€€€ŒMÝ…ÀÑÙµ…Ñ•}¹•Ü¹Áä€´øÑÙµ…Ñ”¹Áä…¹É•±…Õ¹ °Ù¥„„Íµ…±°¡•±Á•È¸(€€€€€€€€€€€¹•Ü€ô½Ì¹Á…Ñ ¹©½¥¸¡…ÁÁ}‘¥È ¤°€‰ÑÙµ…Ñ•}¹•Ü¹Áäˆ¤(€€€€€€€€€€€ÕÈ€ô½Ì¹Á…Ñ ¹©½¥¸¡…ÁÁ}‘¥È ¤°€‰ÑÙµ…Ñ”¹Áäˆ¤(€€€€€€€€€€€¥˜¹½Ð½Ì¹Á…Ñ ¹•á¥ÍÑÌ¡¹•Ü¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½Èˆè€‰¹¼ÕÁ‘…Ñ”‘½Ý¹±½…‘•‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€}É•µ½Ñ•}Ù•ÉÍ¥½¸°É•½Ù•Éå}Í¡„€ô}ÕÁ‘…Ñ•}µ…¹¥™•ÍÐ ¤(€€€€€€€€€€€€€€€€Œ•Ñ•Éµ¥¹”¡½ÜÑ¼É•±…Õ¹ ¸=91dÉ•±…Õ¹ Ñ¡”Á•Éµ…¹•¹Ð±…Õ¹¡•È(€€€€€€€€€€€€€€€€Œ€¹•á”€´¹•Ù•È„Ñ•µÀµ•áÑÉ…Ñ•ÁåÑ¡½¸¹•á”€¡Ý¡¥ Ù…¹¥Í¡•Ì¤¸(€€€€€€€€€€€€€€€±…Õ¹¡•É}•á”€ô½Ì¹•¹Ù¥É½¸¹•Ð ‰QY5Q}aˆ¤(€€€€€€€€€€€€€€€É•±…Õ¹ €ô9½¹”(€€€€€€€€€€€€€€€¥˜±…Õ¹¡•É}•á”…¹½Ì¹Á…Ñ ¹•á¥ÍÑÌ¡±…Õ¹¡•É}•á”¤…¹±…Õ¹¡•É}•á”¹±½Ý•È ¤¹•¹‘ÍÝ¥Ñ  ˆ¹•á”ˆ¤è(€€€€€€€€€€€€€€€€€€€É•±…Õ¹ €ô€œˆœ€¬±…Õ¹¡•É}•á”€¬€œˆœ(€€€€€€€€€€€€€€€•±¥˜•Ñ…ÑÑÈ¡ÍåÌ°€‰™É½é•¸ˆ°…±Í”¤…¹½Ì¹Á…Ñ ¹•á¥ÍÑÌ¡ÍåÌ¹…ÉÙlÁt¤è(€€€€€€€€€€€€€€€€€€€É•±…Õ¹ €ô€œˆœ€¬ÍåÌ¹…ÉÙlÁt€¬€œˆœ(€€€€€€€€€€€€€€€€Œ%˜¹½ÐÉÕ¹¹¥¹œ™É½´„±…Õ¹¡•È½•á”€¡”¹œ¸Á±…¥¸ÁåÑ¡½¸‘•ØÉÕ¸¤°(€€€€€€€€€€€€€€€€ŒÉ•±…Õ¹ Ý¥Ñ Ñ¡”¥¹Ñ•ÉÁÉ•Ñ•È½¹±ä¥˜¥ÐÌ„É•…°°ÍÑ…‰±”Á…Ñ ¸(€€€€€€€€€€€€€€€•±¥˜¹½Ð•Ñ…ÑÑÈ¡ÍåÌ°€‰™É½é•¸ˆ°…±Í”¤…¹€‰Ñ•µÀˆ¹½Ð¥¸€¡ÍåÌ¹•á•ÕÑ…‰±”½È€ˆˆ¤¹±½Ý•È ¤è(€€€€€€€€€€€€€€€€€€€É•±…Õ¹ €ô€œˆœ€¬ÍåÌ¹•á•ÕÑ…‰±”€¬€œˆ€ˆœ€¬ÕÈ€¬€œˆœ((€€€€€€€€€€€€€€€¥˜ÍåÌ¹Á±…Ñ™½É´¹ÍÑ…ÉÑÍÝ¥Ñ  ‰Ý¥¸ˆ¤è(€€€€€€€€€€€€€€€€€€€¡•±Á•È€ô½Ì¹Á…Ñ ¹©½¥¸¡…ÁÁ}‘¥È ¤°€‰}ÕÁ‘…Ñ”¹‰…Ðˆ¤(€€€€€€€€€€€€€€€€€€€±…Õ¹¡•É}¹…µ”€ô½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡±…Õ¹¡•É}•á”½È€ˆˆ¤(€€€€€€€€€€€€€€€€€€€­¹½Ý¹}±…Õ¹¡•È€ô‰½½°¡É”¹™Õ±±µ…Ñ  (€€€€€€€€€€€€€€€€€€€€€€€Èˆ üé=QY5ñ=±½ÍQY5…Ñ”¤ üéqÌ©p¡q­p¤¤ýp¹•á”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€±…Õ¹¡•É}¹…µ”°™±…ÌõÉ”¹%9=IM¤¤(€€€€€€€€€€€€€€€€€€€±¥¹•Ì€ôl‰•¡¼½™™qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”UÁ‘…Ñ¥¹œQY5…Ñ•qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½€ˆœ€¬…ÁÁ}‘¥È ¤€¬€œ‰qÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•¡¼¹qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•¡¼UÁ‘…Ñ¥¹œQY5…Ñ”¸¸¹qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•¡¼A±•…Í”Ý…¥ÐÝ¡¥±”QY5…Ñ”É•ÍÑ…ÉÑÌ¹qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•½ÕÐ€½Ð€Ì€½¹½‰É•…¬€ù¹Õ±qÉq¸‰t(€€€€€€€€€€€€€€€€€€€€Œ9Õ¥Ñ­„Ì½±½¹•™¥±”±…Õ¹¡•È…¸ÍÕÉÙ¥Ù”¥ÑÌ¡¥±…¹(€€€€€€€€€€€€€€€€€€€€ŒÁÉ•Ù•¹Ð„±•…¸É•±…Õ¹ ¸-¥±°½¹±ä„­¹½Ý¸QY5…Ñ”¥µ…”¸(€€€€€€€€€€€€€€€€€€€¥˜­¹½Ý¹}±…Õ¹¡•Èè(€€€€€€€€€€€€€€€€€€€€€€€±¥¹•Ì¹•áÑ•¹¡lÑ…Í­­¥±°€½˜€½¥´€ˆœ€¬±…Õ¹¡•É}¹…µ”€¬(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€œˆ€ù¹Õ°€Èø˜ÅqÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•½ÕÐ€½Ð€Ä€½¹½‰É•…¬€ù¹Õ±qÉq¸‰t¤(€€€€€€€€€€€€€€€€€€€±¥¹•Ì¹•áÑ•¹¡l(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½Áä€½ä€ˆœ€¬ÕÈ€¬€œˆ€ˆœ€¬ÕÈ€¬€œ¹‰…­ÕÀˆ€ù¹Õ±qÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰™½È€½°€”•$¥¸€ Ä°Ä°ÈÀ¤‘¼€¡qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€œ€µ½Ù”€½ä€ˆœ€¬¹•Ü€¬€œˆ€ˆœ€¬ÕÈ€¬€œˆ€ù¹Õ°€Èø˜Ä€˜˜½Ñ¼ÕÁ‘…Ñ•‘qÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆ€Ñ¥µ•½ÕÐ€½Ð€Ä€½¹½‰É•…¬€ù¹Õ±qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆ¥qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•¡¼9½Éµ…°ÕÁ‘…Ñ”™…¥±•¸QÉå¥¹œ„±•…¸‘½Ý¹±½…¸¸¹qÉq¸‰t¤(€€€€€€€€€€€€€€€€€€€¥˜É•½Ù•Éå}Í¡„è(€€€€€€€€€€€€€€€€€€€€€€€ÁÍ}ÕÉ°€ôUAQ}MI%AQ}UI0¹É•Á±…” ˆœˆ°€ˆœœˆ¤(€€€€€€€€€€€€€€€€€€€€€€€ÁÍ}¹•Ü€ô¹•Ü¹É•Á±…” ˆœˆ°€ˆœœˆ¤(€€€€€€€€€€€€€€€€€€€€€€€±¥¹•Ì¹•áÑ•¹¡l(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘•°€½˜€½Ä€ˆœ€¬ÕÈ€¬€œˆ€ù¹Õ°€Èø˜ÅqÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘•°€½˜€½Ä€ˆœ€¬¹•Ü€¬€œˆ€ù¹Õ°€Èø˜ÅqÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Á½Ý•ÉÍ¡•±°¹•á”€µ9½AÉ½™¥±”€µá•ÕÑ¥½¹A½±¥ä	åÁ…ÍÌ€µ½µµ…¹€ˆ‘AÉ½É•ÍÍAÉ•™•É•¹”õpM¥±•¹Ñ±å½¹Ñ¥¹Õ•pœìÑÉäì%¹Ù½­”µ]•‰I•ÅÕ•ÍÐ€µUÍ•	…Í¥A…ÉÍ¥¹œ€µUÉ¤pœœ€¬ÁÍ}ÕÉ°€¬€pœ€µ=ÕÑ¥±”pœœ€¬ÁÍ}¹•Ü€¬€pœì¥˜€ ¡•Ðµ¥±•!…Í €µ±½É¥Ñ¡´M!ÈÔØ€µ1¥Ñ•É…±A…Ñ pœœ€¬ÁÍ}¹•Ü€¬€pœ¤¹!…Í ¹Q½1½Ý•È ¤€µ¹”pœœ€¬É•½Ù•Éå}Í¡„€¬€pœ¤ìÑ¡É½Üp¡•­ÍÕ´µ¥Íµ…Ñ¡pœôô…Ñ ìI•µ½Ù”µ%Ñ•´€µ½É”€µÉÉ½ÉÑ¥½¸M¥±•¹Ñ±å½¹Ñ¥¹Õ”€µ1¥Ñ•É…±A…Ñ pœœ€¬ÁÍ}¹•Ü€¬€pœì•á¥Ð€Äô‰qÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¥˜•ÉÉ½É±•Ù•°€Ä½Ñ¼É•½Ù•É™…¥±•‘qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€µ½Ù”€½ä€ˆœ€¬¹•Ü€¬€œˆ€ˆœ€¬ÕÈ€¬€œˆ€ù¹Õ°€Èø˜Äñð½Ñ¼É•½Ù•É™…¥±•‘qÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ñ¼ÕÁ‘…Ñ•‘qÉq¸‰t¤(€€€€€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ‰•¡¼UÁ‘…Ñ”µ…¹¥™•ÍÐ¡•­ÍÕ´¥ÌÕ¹…Ù…¥±…‰±”¹qÉq¸ˆ¤(€€€€€€€€€€€€€€€€€€€±¥¹•Ì¹•áÑ•¹¡l(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆéÉ•½Ù•É™…¥±•‘qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•¡¼±•…¸‘½Ý¹±½…™…¥±•¸I•ÍÑ½É¥¹œÑ¡”ÁÉ•Ù¥½ÕÌÙ•ÉÍ¥½¸¹qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€¥˜•á¥ÍÐ€ˆœ€¬ÕÈ€¬€œ¹‰…­ÕÀˆ½Áä€½ä€ˆœ€¬ÕÈ€¬€œ¹‰…­ÕÀˆ€ˆœ€¬ÕÈ€¬€œˆ€ù¹Õ±qÉq¸œ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰½Ñ¼É•±…Õ¹¡qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆéÕÁ‘…Ñ•‘qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•¡¼UÁ‘…Ñ”¥¹ÍÑ…±±•ÍÕ•ÍÍ™Õ±±ä¹qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆéÉ•±…Õ¹¡qÉq¸‰t¤(€€€€€€€€€€€€€€€€€€€¥˜É•±…Õ¹ è(€€€€€€€€€€€€€€€€€€€€€€€±¥¹•Ì¹•áÑ•¹¡l‰•¡¼MÑ…ÉÑ¥¹œQY5…Ñ”¸¸¹qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÉÐ€ˆˆ€œ€¬É•±…Õ¹ €¬€‰qÉq¸‰t¤(€€€€€€€€€€€€€€€€€€€±¥¹•Ì¹•áÑ•¹¡l‰Ñ¥µ•½ÕÐ€½Ð€Ì€½¹½‰É•…¬€ù¹Õ±qÉq¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‘•°€ˆ•ù˜À‰qÉq¸t¤(€€€€€€€€€€€€€€€€€€€Ý¥Ñ ½Á•¸¡¡•±Á•È°€‰Üˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ°¹•Ý±¥¹”ôˆˆ¤…Ì˜è(€€€€€€€€€€€€€€€€€€€€€€€˜¹ÝÉ¥Ñ•±¥¹•Ì¡±¥¹•Ì¤(€€€€€€€€€€€€€€€€€€€€ŒQ¡”¡•±Á•ÈµÕÍÐÍÕÉÙ¥Ù”Ñ¡¥ÌÍ•ÉÙ•È•á¥Ñ¥¹œ°‰ÕÐ¥Ð‘½•Ì(€€€€€€€€€€€€€€€€€€€€Œ¹½Ð¹••„Ù¥Í¥‰±”½¹Í½±”¸¹•Ü½¹Í½±”…±Í¼•ÑÌ(€€€€€€€€€€€€€€€€€€€€Œ¥¹¡•É¥Ñ•‰ä±•…ä±…Õ¹¡•ÉÌ…¹•áÁ½Í•Ì¡…Éµ±•ÍÌ!QQ@(€€€€€€€€€€€€€€€€€€€€Œ‘¥Í½¹¹•ÐÑÉ…•‰…­Ì¥¸„Í•½¹½µµ…¹Ý¥¹‘½Ü¸(€€€€€€€€€€€€€€€€€€€™±…Ì€ô•Ñ…ÑÑÈ¡ÍÕ‰ÁÉ½•ÍÌ°€‰IQ}9=}]%9=\ˆ°€ÁàÀàÀÀÀÀÀÀ¤(€€€€€€€€€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡l‰µ¹•á”ˆ°€ˆ½ˆ°€ˆ½Œˆ°¡•±Á•Ét°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ýõ…ÁÁ}‘¥È ¤°É•…Ñ¥½¹™±…Ìõ™±…Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘¥¸õÍÕ‰ÁÉ½•ÍÌ¹Y9U10°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€±½Í•}™‘ÌõQÉÕ”¤(€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€¡•±Á•È€ô½Ì¹Á…Ñ ¹©½¥¸¡…ÁÁ}‘¥È ¤°€‰}ÕÁ‘…Ñ”¹Í ˆ¤(€€€€€€€€€€€€€€€€€€€‰½‘ä€ô€ˆŒ„½‰¥¸½Í¡q¹Í±••À€Éq¹À€µ˜€œˆ€¬ÕÈ€¬€ˆœ€œˆ€¬ÕÈ€¬€ˆ¹‰…­ÕÀq¹µØ€µ˜€œˆ€¬¹•Ü€¬€ˆœ€œˆ€¬ÕÈ€¬€ˆq¸ˆ(€€€€€€€€€€€€€€€€€€€¥˜É•±…Õ¹ è(€€€€€€€€€€€€€€€€€€€€€€€‰½‘ä€¬ôÉ•±…Õ¹ €¬€ˆ€™q¸ˆ(€€€€€€€€€€€€€€€€€€€‰½‘ä€¬ô€É´€´´€ˆÀ‰q¸œ(€€€€€€€€€€€€€€€€€€€Ý¥Ñ ½Á•¸¡¡•±Á•È°€‰Üˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤…Ì˜è(€€€€€€€€€€€€€€€€€€€€€€€˜¹ÝÉ¥Ñ”¡‰½‘ä¤(€€€€€€€€€€€€€€€€€€€½Ì¹¡µ½¡¡•±Á•È°€Á¼ÜÔÔ¤(€€€€€€€€€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡lˆ½‰¥¸½Í ˆ°¡•±Á•Ét°ÍÑ…ÉÑ}¹•Ý}Í•ÍÍ¥½¸õQÉÕ”¤(€€€€€€€€€€€€€€€‘•˜}‰å” ¤è(€€€€€€€€€€€€€€€€€€€¥µÁ½ÉÐÑ¥µ”…Ì}Ðì}Ð¹Í±••À Ä¤ì½Ì¹}•á¥Ð À¤(€€€€€€€€€€€€€€€¥µÁ½ÉÐÑ¡É•…‘¥¹œ…Ì}Ñ ì}Ñ ¹Q¡É•…¡Ñ…É•Ðõ}‰å”°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰É•±…Õ¹ ˆè‰½½°¡É•±…Õ¹ ¥ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½½Á•¹}™½±‘•Èˆè(€€€€€€€€€€€™½±‘•È€ô…ÁÁ}‘¥È ¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¥˜ÍåÌ¹Á±…Ñ™½É´¹ÍÑ…ÉÑÍÝ¥Ñ  ‰Ý¥¸ˆ¤è(€€€€€€€€€€€€€€€€€€€½Ì¹ÍÑ…ÉÑ™¥±”¡™½±‘•È¤€€ŒÑåÁ”è¥¹½É•m…ÑÑÈµ‘•™¥¹•‘t(€€€€€€€€€€€€€€€•±¥˜ÍåÌ¹Á±…Ñ™½É´€ôô€‰‘…ÉÝ¥¸ˆè(€€€€€€€€€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡l‰½Á•¸ˆ°™½±‘•Ét¤(€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡l‰á‘œµ½Á•¸ˆ°™½±‘•Ét¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰Á…Ñ ˆè™½±‘•Éô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰½¬ˆè…±Í”°€‰•ÉÉ½ÈˆèÍÑÈ¡”¤°€‰Á…Ñ ˆè™½±‘•Éô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Á±…äˆè(€€€€€€€€€€€€Œ1…Õ¹ Y1Ý¥Ñ „ÍÑÉ•…´ÕÉ°€¡ÍÑÉ•…µ}¥€´øÑÌÕÉ°¤¸(€€€€€€€€€€€Í¥€ôÍÑÈ¡Á…å±½…¹•Ð ‰ÍÑÉ•…µ}¥ˆ°€ˆˆ¤¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€¥˜¹½Ð€¡à¹½¹™¥ÕÉ• ¤…¹Í¥¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…É•ÅÕ•ÍÐ‰ô¤(€€€€€€€€€€€ÕÉ°€ôà¹ÍÑÉ•…µ}ÕÉ°¡Í¥¤(€€€€€€€€€€€Ù±Œ€ô}™¥¹‘}Ù±Œ ¤(€€€€€€€€€€€¥˜¹½ÐÙ±Œè(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰Y1¹½Ð™½Õ¹¸%¹ÍÑ…±°Y1½ÈÕÍ”½Áä¸‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡mÙ±Œ°ÕÉ±t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ•ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Á±…å}µ½Ù¥”ˆè(€€€€€€€€€€€Í¥€ôÍÑÈ¡Á…å±½…¹•Ð ‰ÍÑÉ•…µ}¥ˆ°€ˆˆ¤¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€•áÐ€ôÍÑÈ¡Á…å±½…¹•Ð ‰•áÑ•¹Í¥½¸ˆ°€‰µÀÐˆ¤¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€¥˜¹½Ð€¡à¹½¹™¥ÕÉ• ¤…¹Í¥¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰‰…É•ÅÕ•ÍÐ‰ô¤(€€€€€€€€€€€Ù±Œ€ô}™¥¹‘}Ù±Œ ¤(€€€€€€€€€€€¥˜¹½ÐÙ±Œè(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰Y1¹½Ð™½Õ¹¸‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡mÙ±Œ°à¹µ½Ù¥•}ÕÉ°¡Í¥°•áÐ¥t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ•ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Á±…å}•Á¥Í½‘”ˆè(€€€€€€€€€€€•Á¥Í½‘•}¥€ôÍÑÈ¡Á…å±½…¹•Ð ‰•Á¥Í½‘•}¥ˆ°€ˆˆ¤¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€•áÐ€ôÍÑÈ¡Á…å±½…¹•Ð ‰•áÑ•¹Í¥½¸ˆ°€‰µÀÐˆ¤¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€Ù±Œ€ô}™¥¹‘}Ù±Œ ¤(€€€€€€€€€€€¥˜¹½Ð€¡à¹½¹™¥ÕÉ• ¤…¹•Á¥Í½‘•}¥…¹Ù±Œ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰Y1¹½Ð™½Õ¹½È•Á¥Í½‘”¥Ì¥¹Ù…±¥¸‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡mÙ±Œ°à¹•Á¥Í½‘•}ÕÉ°¡•Á¥Í½‘•}¥°•áÐ¥t°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ•ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½Á±…å}Í•…Í½¸ˆè(€€€€€€€€€€€•Á¥Í½‘•Ì€ôÁ…å±½…¹•Ð ‰•Á¥Í½‘•Ìˆ¤½Èmt(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€Ù±Œ€ô}™¥¹‘}Ù±Œ ¤(€€€€€€€€€€€ÕÉ±Ì€ômà¹•Á¥Í½‘•}ÕÉ°¡•À¹•Ð ‰¥ˆ¤°•À¹•Ð ‰•áÑ•¹Í¥½¸ˆ°€‰µÀÐˆ¤¤(€€€€€€€€€€€€€€€€€€€™½È•À¥¸•Á¥Í½‘•Ì¥˜•À¹•Ð ‰¥ˆ¤¥Ì¹½Ð9½¹•t(€€€€€€€€€€€¥˜¹½Ð€¡à¹½¹™¥ÕÉ• ¤…¹ÕÉ±Ì…¹Ù±Œ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰Y1¹½Ð™½Õ¹½ÈÍ•…Í½¸¥Ì•µÁÑä¸‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡mÙ±t€¬ÕÉ±Ì°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÈÀÀ°ì‰½¬ˆèQÉÕ”°€‰½Õ¹Ðˆè±•¸¡ÕÉ±Ì¥ô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤((€€€€€€€¥˜Ô¹Á…Ñ €ôô€ˆ½…Á¤½´ÍÔˆè(€€€€€€€€€€€€Œ	Õ¥±…¸4ÍT™É½´Í•±•Ñ•…Ñ•½É¥•Ì…¹½½ÈÍÁ•¥™¥ŒÍÑÉ•…µ}¥‘Ì¸(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€à€ôaÑÉ•…´¡™œ¤(€€€€€€€€€€€¥˜¹½Ðà¹½¹™¥ÕÉ• ¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÀ°ì‰•ÉÉ½Èˆè€‰9½Ð½¹™¥ÕÉ•‰ô¤(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¡…¹¹•±Ì°…ÑÌ€ô•Ñ}áÑÉ•…µ}¡…¹¹•±Ì¡™œ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÔÀÀ°ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥ô¤(€€€€€€€€€€€Í•±}…ÑÌ€ôÍ•Ð¡Á…å±½…¹•Ð ‰…Ñ•½É¥•Ìˆ¤½Èmt¤(€€€€€€€€€€€Í•±}¥‘Ì€ôÍ•Ð¡ÍÑÈ¡¤¤™½È¤¥¸€¡Á…å±½…¹•Ð ‰ÍÑÉ•…µ}¥‘Ìˆ¤½Èmt¤¤(€€€€€€€€€€€µ½‘”€ôÁ…å±½…¹•Ð ‰µ½‘”ˆ°€‰…Ñ•½É¥•Ìˆ¤€€Œ€‰…Ñ•½É¥•Ìˆ½È€‰¡…¹¹•±Ìˆ(€€€€€€€€€€€±¥¹•Ì€ôlˆaQ4ÍT‰t(€€€€€€€€€€€¸€ô€À(€€€€€€€€€€€™½È ¥¸¡…¹¹•±Ìè(€€€€€€€€€€€€€€€…Ñ¹…µ”€ô…ÑÌ¹•Ð¡¡l‰…Ñ•½Éå}¥‰t°€ˆˆ¤(€€€€€€€€€€€€€€€¥¹±Õ‘”€ô…±Í”(€€€€€€€€€€€€€€€¥˜µ½‘”€ôô€‰¡…¹¹•±Ìˆè(€€€€€€€€€€€€€€€€€€€¥¹±Õ‘”€ôÍÑÈ¡¡l‰ÍÑÉ•…µ}¥‰t¤¥¸Í•±}¥‘Ì(€€€€€€€€€€€€€€€•±Í”è€€Œ…Ñ•½É¥•Ì(€€€€€€€€€€€€€€€€€€€¥¹±Õ‘”€ô…Ñ¹…µ”¥¸Í•±}…ÑÌ(€€€€€€€€€€€€€€€¥˜¹½Ð¥¹±Õ‘”è(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€¹…µ”€ô¡l‰¹…µ”‰t(€€€€€€€€€€€€€€€ÉÀ€ô…Ñ¹…µ”¹É•Á±…” ˆ°ˆ°€ˆ€ˆ¤(€€€€€€€€€€€€€€€¥½¸€ôÍÑÈ¡ ¹•Ð ‰ÍÑÉ•…µ}¥½¸ˆ¤½È€ˆˆ¤¹É•Á±…” œˆœ°€œ”ÈÈœ¤(€€€€€€€€€€€€€€€±½½}…ÑÑÈ€ô˜œÑÙœµ±½¼ô‰í¥½¹ôˆœ¥˜¥½¸•±Í”€ˆˆ(€€€€€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹¡˜œaQ%9è´ÄÉ½ÕÀµÑ¥Ñ±”ô‰íÉÁô‰í±½½}…ÑÑÉô±í¹…µ•ôœ¤(€€€€€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹¡à¹ÍÑÉ•…µ}ÕÉ°¡¡l‰ÍÑÉ•…µ}¥‰t¤¤(€€€€€€€€€€€€€€€¸€¬ô€Ä(€€€€€€€€€€€‰½‘ä€ô€‰q¸ˆ¹©½¥¸¡±¥¹•Ì¤€¬€‰q¸ˆ(€€€€€€€€€€€‘…Ñ„€ô‰½‘ä¹•¹½‘” ‰ÕÑ˜´àˆ¤(€€€€€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í” ÈÀÀ¤(€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°€‰…Õ‘¥¼½àµµÁ•ÕÉ°ì¡…ÉÍ•ÐõÕÑ˜´àˆ¤(€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ¥ÍÁ½Í¥Ñ¥½¸ˆ°€…ÑÑ…¡µ•¹Ðì™¥±•¹…µ”ô‰Á±…å±¥ÍÐ¹´ÍÔˆœ¤(€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°ÍÑÈ¡±•¸¡‘…Ñ„¤¤¤(€€€€€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤(€€€€€€€€€€€Í•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡‘…Ñ„¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•¹ ÐÀÐ°ì‰•ÉÉ½Èˆè€‰¹½Ð™½Õ¹‰ô¤((Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´(Œ5…¥¸(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´()}MQ=A}Y9P€ôÑ¡É•…‘¥¹œ¹Ù•¹Ð ¤()‘•˜}…ÕÑ½}Í¡ÕÑ‘½Ý¹}Ý…Ñ¡‘½œ ¤è(€€€Ý¡¥±”¹½Ð}MQ=A}Y9P¹Ý…¥Ð ÄÔ¤è(€€€€€€€ÑÉäè(€€€€€€€€€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€€€€€€€€€µ¥¹ÕÑ•Ì€ôµ…à À°¥¹Ð¡™œ¹•Ð ‰…ÕÑ½}Í¡ÕÑ‘½Ý¹}µ¥¹ÕÑ•Ìˆ¤½È€À¤¤(€€€€€€€€€€€¥˜™œ¹•Ð ‰¡¥‘•}µ‘}Ý¥¹‘½Üˆ¤…¹µ¥¹ÕÑ•Ì…¹}¥¹…Ñ¥Ù•}Í•½¹‘Ì ¤€øôµ¥¹ÕÑ•Ì€¨€ØÀè(€€€€€€€€€€€€€€€}MQ=A}Y9P¹Í•Ð ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€Á…ÍÌ()‘•˜}•¹…‰±•}…¹Í¤ ¤è(€€€€ˆˆ‰QÕÉ¸½¸9M$½±½È¥¸Ñ¡”]¥¹‘½ÝÌ½¹Í½±”¸M¥¹”Í½µ”•¹Ù¥É½¹µ•¹ÑÌ(€€€€¡”¹œ¸½µÁ¥±•½¹•™¥±”•á•Ì¤ÍÕÁÁ½ÉÐ½±½È•Ù•¸Ý¡•¸Ñ¡”¡…¹‘±”‘…¹”(€€€™…¥±Ì°Ý”‘•™…Õ±ÐÑ¼QÉÕ”…¹©ÕÍÐQIdÑ¼•¹…‰±”YPÁÉ½•ÍÍ¥¹œ¸ˆˆˆ(€€€¥˜¹½ÐÍåÌ¹Á±…Ñ™½É´¹ÍÑ…ÉÑÍÝ¥Ñ  ‰Ý¥¸ˆ¤è(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€ÑÉäè(€€€€€€€¥µÁ½ÉÐÑåÁ•Ì(€€€€€€€¬€ôÑåÁ•Ì¹Ý¥¹‘±°¹­•É¹•°ÌÈ(€€€€€€€™½È¡…¹‘±•}¥¥¸€ ´ÄÄ°€´ÄÈ¤è€€ŒÍÑ‘½ÕÐ°ÍÑ‘•ÉÈ(€€€€€€€€€€€ €ô¬¹•ÑMÑ‘!…¹‘±”¡¡…¹‘±•}¥¤(€€€€€€€€€€€µ½‘”€ôÑåÁ•Ì¹}Õ¥¹ÐÌÈ ¤(€€€€€€€€€€€¥˜¬¹•Ñ½¹Í½±•5½‘”¡ °ÑåÁ•Ì¹‰åÉ•˜¡µ½‘”¤¤è(€€€€€€€€€€€€€€€¬¹M•Ñ½¹Í½±•5½‘”¡ °µ½‘”¹Ù…±Õ”ð€ÁàÀÀÀÐ¤€€Œ9	1}Y%IQU1}QI5%91}AI=MM%9(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€Á…ÍÌ(€€€€ŒÍÍÕµ”½±½ÈÝ½É­Ì€¡Ñ¡”½¹Í½±”¡…ÌÍ¡½Ý¸9M$½±½È‰•™½É”¤¸(€€€É•ÑÕÉ¸QÉÕ”()‘•˜}Í•Ñ}½¹Í½±•}Ù¥Í¥‰±”¡Ù¥Í¥‰±”¤è(€€€€ˆˆ‰ÑÑ… ½‘•Ñ… Ñ¡¥ÌÁÉ½•ÍÌœ]¥¹‘½ÝÌ½¹Í½±”¸9¼µ½À•±Í•Ý¡•É”¸ˆˆˆ(€€€¥˜¹½ÐÍåÌ¹Á±…Ñ™½É´¹ÍÑ…ÉÑÍÝ¥Ñ  ‰Ý¥¸ˆ¤è(€€€€€€€É•ÑÕÉ¸(€€€ÑÉäè(€€€€€€€¥µÁ½ÉÐÑåÁ•Ì(€€€€€€€¬€ôÑåÁ•Ì¹Ý¥¹‘±°¹­•É¹•°ÌÈ(€€€€€€€¥˜¹½ÐÙ¥Í¥‰±”è(€€€€€€€€€€€€ŒM]}!%…¸‰•½µ”„µ¥¹¥µ¥é”½Á•É…Ñ¥½¸Õ¹‘•È]¥¹‘½ÝÌQ•Éµ¥¹…°¸(€€€€€€€€€€€€Œ•Ñ…¡¥¹œ±½Í•ÌÑ¡”½¹Í½±”™½È„¹½Éµ…°‘½Õ‰±”µ±¥¬±…Õ¹ ¸(€€€€€€€€€€€¬¹É••½¹Í½±” ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¡Ý¹€ô¬¹•Ñ½¹Í½±•]¥¹‘½Ü ¤(€€€€€€€€ŒQ¡”U$µÍÕ‰ÍåÍÑ•´½¹•™¥±”±…Õ¹¡•È…¸±•…Ù”ÕÌ…ÑÑ…¡•Ñ¼…¸(€€€€€€€€Œ¥¹Ù¥Í¥‰±”½½¹AQd½¹Í½±”¸€M¡½Ý]¥¹‘½Ü…¹¹½Ðµ…­”Ñ¡…ÐÕÍ…‰±”™½È(€€€€€€€€ŒI•ÑÉ¼µ½‘”°Í¼±…Õ¹¡•ÈµÍÑ…ÉÑ•Í•ÍÍ¥½¹Ì‘•±¥‰•É…Ñ•±äÉ•Á±…”¥Ð(€€€€€€€€ŒÝ¥Ñ „™É•Í ½¹Í½±”Ý¥¹‘½Ü¸€¥É•ÐÁåÑ¡½¸ÑÙµ…Ñ”¹Áå€ÉÕ¹Ì­••À(€€€€€€€€ŒÑ¡•¥È•á¥ÍÑ¥¹œÑ•Éµ¥¹…°¸(€€€€€€€¥˜Ù¥Í¥‰±”…¹½Ì¹•¹Ù¥É½¸¹•Ð ‰QY5Q}aˆ¤…¹¡Ý¹è(€€€€€€€€€€€¬¹É••½¹Í½±” ¤(€€€€€€€€€€€¡Ý¹€ô9½¹”(€€€€€€€¥˜¹½Ð¡Ý¹…¹¬¹±±½½¹Í½±” ¤è(€€€€€€€€€€€€ŒI•½¹¹•ÐAåÑ¡½¸ÌÍÑ…¹‘…ÉÍÑÉ•…µÌÝ¡•¸ÍÝ¥Ñ¡¥¹œ‰…¬Ñ¼É•ÑÉ¼(€€€€€€€€€€€€Œµ½‘”¥¸Ñ¡”ÕÉÉ•¹ÐÍ•ÍÍ¥½¸¸É•ÍÑ…ÉÐÝ¥±°É•ÍÑ½É”Ñ¡•´Ñ½¼¸(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÍåÌ¹ÍÑ‘¥¸€ô½Á•¸ ‰=9%8ˆ°€‰Èˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ°•ÉÉ½ÉÌô‰É•Á±…”ˆ¤(€€€€€€€€€€€€€€€ÍåÌ¹ÍÑ‘½ÕÐ€ô½Á•¸ ‰=9=UPˆ°€‰Üˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ°•ÉÉ½ÉÌô‰É•Á±…”ˆ°‰Õ™™•É¥¹œôÄ¤(€€€€€€€€€€€€€€€ÍåÌ¹ÍÑ‘•ÉÈ€ô½Á•¸ ‰=9=UPˆ°€‰Üˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ°•ÉÉ½ÉÌô‰É•Á±…”ˆ°‰Õ™™•É¥¹œôÄ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€¡Ý¹€ô¬¹•Ñ½¹Í½±•]¥¹‘½Ü ¤(€€€€€€€¥˜¡Ý¹è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¬¹M•Ñ½¹Í½±•Q¥Ñ±•\ ‰=±¼ÌQY5…Ñ”€´I•ÑÉ¼M%$µ½‘”ˆ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€ÑåÁ•Ì¹Ý¥¹‘±°¹ÕÍ•ÈÌÈ¹M¡½Ý]¥¹‘½Ü¡¡Ý¹°€Ô¤€€ŒM]}M!=\(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€Á…ÍÌ()‘•˜}±…Õ¹¡}Ý¥Ñ¡½ÕÑ}½¹Í½±” ¤è(€€€€ˆˆ‰I•±…Õ¹ QY5…Ñ”…Ì„•¹Õ¥¹”½¹Í½±”µ±•ÍÌ]¥¹‘½ÝÌÁÉ½•ÍÌ¸ˆˆˆ(€€€¥˜¹½ÐÍåÌ¹Á±…Ñ™½É´¹ÍÑ…ÉÑÍÝ¥Ñ  ‰Ý¥¸ˆ¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€ÑÉäè(€€€€€€€•¹Ø€ô½Ì¹•¹Ù¥É½¸¹½Áä ¤(€€€€€€€•¹Ùl‰QY5Q}!%9}!%1‰t€ô€ˆÄˆ(€€€€€€€¥˜•Ñ…ÑÑÈ¡ÍåÌ°€‰™É½é•¸ˆ°…±Í”¤è(€€€€€€€€€€€µ€ômÍåÌ¹•á•ÕÑ…‰±•t€¬ÍåÌ¹…ÉÙlÄét(€€€€€€€•±Í”è(€€€€€€€€€€€µ€ômÍåÌ¹•á•ÕÑ…‰±”°½Ì¹Á…Ñ ¹…‰ÍÁ…Ñ ¡}}™¥±•}|¥t€¬ÍåÌ¹…ÉÙlÄét(€€€€€€€™±…Ì€ô•Ñ…ÑÑÈ¡ÍÕ‰ÁÉ½•ÍÌ°€‰IQ}9=}]%9=\ˆ°€ÁàÀàÀÀÀÀÀÀ¤(€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡µ°•¹Øõ•¹Ø°É•…Ñ¥½¹™±…Ìõ™±…Ì°(€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘¥¸õÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°(€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°±½Í•}™‘ÌõQÉÕ”¤(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€É•ÑÕÉ¸…±Í”()‘•˜}±½Í•}±…Õ¹¡•É}½¹Í½±” ¤è(€€€€ˆˆ‰±½Í”Ñ¡”‘•‘¥…Ñ•ÑÙµ…Ñ”¹•á”½¹Í½±”Ý¥Ñ¡½ÕÐÑ½Õ¡¥¹œÕÍ•ÈÑ•Éµ¥¹…±Ì¸ˆˆˆ(€€€¥˜¹½ÐÍåÌ¹Á±…Ñ™½É´¹ÍÑ…ÉÑÍÝ¥Ñ  ‰Ý¥¸ˆ¤è(€€€€€€€É•ÑÕÉ¸(€€€€ŒQ¡”Á•Éµ…¹•¹ÐQY5…Ñ”±…Õ¹¡•ÈÍ•ÑÌÑ¡¥Ì¸€¼¹½Ð±½Í”„½¹Í½±”Ý¡•¸Ñ¡”(€€€€ŒÍÉ¥ÁÐÝ…ÌÍÑ…ÉÑ•µ…¹Õ…±±äÝ¥Ñ ÁåÑ¡½¸ÑÙµ…Ñ”¹Áå€¸(€€€±…Õ¹¡•È€ô½Ì¹•¹Ù¥É½¸¹•Ð ‰QY5Q}aˆ°€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½Ð±…Õ¹¡•È½È¹½Ð±…Õ¹¡•È¹±½Ý•È ¤¹•¹‘ÍÝ¥Ñ  ˆ¹•á”ˆ¤è(€€€€€€€É•ÑÕÉ¸(€€€ÑÉäè(€€€€€€€¥µÁ½ÉÐÑåÁ•Ì(€€€€€€€™É½´ÑåÁ•Ì¥µÁ½ÉÐÝ¥¹ÑåÁ•Ì(€€€€€€€¬€ôÑåÁ•Ì¹Ý¥¹‘±°¹­•É¹•°ÌÈ(€€€€€€€±…Õ¹¡•É}¹½É´€ô½Ì¹Á…Ñ ¹¹½Éµ…Í”¡½Ì¹Á…Ñ ¹…‰ÍÁ…Ñ ¡±…Õ¹¡•È¤¤((€€€€€€€€Œ%˜Ñ¡”Á•Éµ…¹•¹Ð±…Õ¹¡•È¥Ì…¹½Ñ¡•ÈÁÉ½•ÍÌ…ÑÑ…¡•Ñ¼Ñ¡¥ÌÍ…µ”(€€€€€€€€Œ½¹Í½±”°ÍÑ½ÀÑ¡…Ð•á…Ð•á•ÕÑ…‰±”¸€Q¡¥Ì¥Ì‘•±¥‰•É…Ñ•±äÍÑÉ¥Ñ•È(€€€€€€€€ŒÑ¡…¸­¥±±¥¹œ„Á…É•¹Ðµ¹•á”½Ñ•Éµ¥¹…°ÁÉ½•ÍÌ¸(€€€€€€€Á¥‘Ì€ô€¡Ý¥¹ÑåÁ•Ì¹]=I€¨€ÌÈ¤ ¤(€€€€€€€½Õ¹Ð€ô¬¹•Ñ½¹Í½±•AÉ½•ÍÍ1¥ÍÐ¡Á¥‘Ì°±•¸¡Á¥‘Ì¤¤(€€€€€€€¥˜½Õ¹Ð€ø±•¸¡Á¥‘Ì¤è(€€€€€€€€€€€Á¥‘Ì€ô€¡Ý¥¹ÑåÁ•Ì¹]=I€¨½Õ¹Ð¤ ¤(€€€€€€€€€€€½Õ¹Ð€ô¬¹•Ñ½¹Í½±•AÉ½•ÍÍ1¥ÍÐ¡Á¥‘Ì°±•¸¡Á¥‘Ì¤¤(€€€€€€€AI=MM}QI5%9Q€ô€ÁàÀÀÀÄ(€€€€€€€AI=MM}EUIe}1%5%Q}%9=I5Q%=8€ô€ÁàÄÀÀÀ(€€€€€€€™½ÈÁ¥¥¸±¥ÍÐ¡Á¥‘Ì¥lé½Õ¹Ñtè(€€€€€€€€€€€¥˜¹½ÐÁ¥½ÈÁ¥€ôô½Ì¹•ÑÁ¥ ¤è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€¡ÁÉ½Œ€ô¬¹=Á•¹AÉ½•ÍÌ¡AI=MM}QI5%9QðAI=MM}EUIe}1%5%Q}%9=I5Q%=8°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€…±Í”°Á¥¤(€€€€€€€€€€€¥˜¹½Ð¡ÁÉ½Œè(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€Í¥é”€ôÝ¥¹ÑåÁ•Ì¹]=I ÌÈÜØà¤(€€€€€€€€€€€€€€€‰Õ˜€ôÑåÁ•Ì¹É•…Ñ•}Õ¹¥½‘•}‰Õ™™•È¡Í¥é”¹Ù…±Õ”¤(€€€€€€€€€€€€€€€¥˜¬¹EÕ•ÉåÕ±±AÉ½•ÍÍ%µ…•9…µ•\¡¡ÁÉ½Œ°€À°‰Õ˜°ÑåÁ•Ì¹‰åÉ•˜¡Í¥é”¤¤è(€€€€€€€€€€€€€€€€€€€ÁÉ½}¹½É´€ô½Ì¹Á…Ñ ¹¹½Éµ…Í”¡½Ì¹Á…Ñ ¹…‰ÍÁ…Ñ ¡‰Õ˜¹Ù…±Õ”¤¤(€€€€€€€€€€€€€€€€€€€¥˜ÁÉ½}¹½É´€ôô±…Õ¹¡•É}¹½É´è(€€€€€€€€€€€€€€€€€€€€€€€¬¹Q•Éµ¥¹…Ñ•AÉ½•ÍÌ¡¡ÁÉ½Œ°€À¤(€€€€€€€€€€€™¥¹…±±äè(€€€€€€€€€€€€€€€¬¹±½Í•!…¹‘±”¡¡ÁÉ½Œ¤((€€€€€€€¡Ý¹€ô¬¹•Ñ½¹Í½±•]¥¹‘½Ü ¤(€€€€€€€¥˜¡Ý¹è(€€€€€€€€€€€€Œ]5}1=M±½Í•ÌÑ¡”‘•‘¥…Ñ•½¹Í½±”Ý¥¹‘½Ü¸€É••½¹Í½±”…±½¹”(€€€€€€€€€€€€Œ½¹±ä‘•Ñ…¡•ÌAåÑ¡½¸…¹…¸±•…Ù”ÑÙµ…Ñ”¹•á”Ì•µÁÑäÝ¥¹‘½ÜÕÀ¸(€€€€€€€€€€€ÑåÁ•Ì¹Ý¥¹‘±°¹ÕÍ•ÈÌÈ¹A½ÍÑ5•ÍÍ…•\¡¡Ý¹°€ÁàÀÀÄÀ°€À°€À¤€€Œ]5}1=M(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€Á…ÍÌ()}=1€ô€‰pÀÌÍläÍ´ˆ€€€Œ‰É¥¡Ðå•±±½Ü€¡ÍåÉÕÀ½±¤)}IMP€ô€‰pÀÌÍlÁ´ˆ()‘•˜}½±½É•‘}‰…¹¹•È¡ÕÍ•}½±½È¤è(€€€€ˆˆ‰I•ÑÕÉ¸Ñ¡”‰…¹¹•ÈÝ¥Ñ Ñ¡”Á…¹…­”Ñ…±¥¹”¥¸½±¸ˆˆˆ(€€€¥˜¹½ÐÕÍ•}½±½Èè(€€€€€€€É•ÑÕÉ¸	99H(€€€½ÕÐ€ômt(€€€™½È±¥¹”¥¸	99H¹ÍÁ±¥Ð ‰q¸ˆ¤è(€€€€€€€¥˜€‰Q•¡¹¥…±±ä„QX…ÁÀˆ¥¸±¥¹”½È€‰MÁ¥É¥ÑÕ…±±ä„Á…¹…­”ˆ¥¸±¥¹”è(€€€€€€€€€€€€Œ½±½È©ÕÍÐÑ¡”Ñ…±¥¹”Ñ•áÐ°­••ÀÑ¡”QX…ÉÐ‰•™½É”¥ÐÕ¹½±½É•(€€€€€€€€€€€¥‘à€ô±¥¹”¹™¥¹ ‰øˆ¤¥˜€‰øˆ¥¸±¥¹”•±Í”±¥¹”¹™¥¹ ‰MÁ¥É¥ÑÕ…±±äˆ¤(€€€€€€€€€€€¥˜¥‘à€ø€Àè(€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡±¥¹•lé¥‘át€¬}=1€¬±¥¹•m¥‘àét€¬}IMP¤(€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡}=1€¬±¥¹”€¬}IMP¤(€€€€€€€•±Í”è(€€€€€€€€€€€½ÕÐ¹…ÁÁ•¹¡±¥¹”¤(€€€É•ÑÕÉ¸€‰q¸ˆ¹©½¥¸¡½ÕÐ¤()9QI}AI=5AQL€ôl(€€€€‰AÉ•ÍÌ¹Ñ•È€¡Ñ¡”Á…¹…­•Ì…É”•ÑÑ¥¹œ½±¤ˆ°(€€€€‰e½ÕÈÑ…‰±”ÌÉ•…‘ä€´ÁÉ•ÍÌ¹Ñ•Èˆ°(€€€€‰É¥‘‘±”Ì¡½Ð¸AÉ•ÍÌ¹Ñ•ÈÑ¼•Ð™±¥ÁÁ¥¸œˆ°(€€€€‰I•…‘äÑ¼½½¬Ý¡•¸å½Ô…É”¸¸¸ÁÉ•ÍÌ¹Ñ•Èˆ°(€€€€‰A½Ý•É•‰äÁ…¹…­•Ì…¹ÅÕ•ÍÑ¥½¹…‰±”‘•¥Í¥½¹Ì¸¸¸¹ÁÉ•ÍÌ¹Ñ•Èˆ°)t()‘•˜}•á¥ÍÑ¥¹}ÑÙµ…Ñ”¡Á½ÉÐ¤è(€€€€ˆˆ‰I•ÑÕÉ¸QÉÕ”½¹±äÝ¡•¸Ñ¡”Í•ÉÙ¥”…±É•…‘ä½¸€©Á½ÉÐ¨¥Ì=±¼ÌQY5…Ñ”¸((€€€Q¡¥Ì‘•±¥‰•É…Ñ•±ä­•åÌ½™˜Ñ¡”ÉÕ¹¹¥¹œÝ•ˆ…ÁÀÉ…Ñ¡•ÈÑ¡…¸„±…Õ¹¡•È(€€€™¥±•¹…µ”¸€=QY4¹•á”°=±½QY5…Ñ”¹•á”…¹]¥¹‘½ÝÌ½Á¥•ÌÍÕ …Ì(€€€=QY4€ È¤¹•á•€Ñ¡•É•™½É”…±°Í¡…É”Ñ¡”Í…µ”Í¥¹±”µ¥¹ÍÑ…¹”¡•¬¸(€€€€ˆˆˆ(€€€‰…Í”€ô˜‰¡ÑÑÀè¼¼ÄÈÜ¸À¸À¸Äéí¥¹Ð¡Á½ÉÐ¥ôˆ(€€€€Œ9•Ü‰Õ¥±‘Ì•áÁ½Í”„¡•…À•áÁ±¥¥Ð¥‘•¹Ñ¥Ñä•¹‘Á½¥¹Ð¸€-••ÀÑ¡”É½½Ð(€€€€Œ™…±±‰…¬Í¼„¹•Ü±…Õ¹¡•È…±Í¼‘•Ñ•ÑÌ…¸½±‘•ÈQY5…Ñ”…±É•…‘äÉÕ¹¹¥¹œ¸(€€€ÑÉäè(€€€€€€€Ý¥Ñ ÕÉ±±¥ˆ¹É•ÅÕ•ÍÐ¹ÕÉ±½Á•¸¡‰…Í”€¬€ˆ½…Á¤½Á¥¹œˆ°Ñ¥µ•½ÕÐôÀ¸à¤…ÌÉ•ÍÀè(€€€€€€€€€€€‘…Ñ„€ô©Í½¸¹±½…‘Ì¡É•ÍÀ¹É•… ÐÀäØ¤¹‘•½‘” ‰ÕÑ˜´àˆ°€‰É•Á±…”ˆ¤¤(€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡‘…Ñ„°‘¥Ð¤…¹‘…Ñ„¹•Ð ‰…ÁÀˆ¤€ôô€‰½±½ÌµÑÙµ…Ñ”ˆè(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€Á…ÍÌ(€€€ÑÉäè(€€€€€€€Ý¥Ñ ÕÉ±±¥ˆ¹É•ÅÕ•ÍÐ¹ÕÉ±½Á•¸¡‰…Í”€¬€ˆ¼ˆ°Ñ¥µ•½ÕÐôÀ¸à¤…ÌÉ•ÍÀè(€€€€€€€€€€€Á…”€ôÉ•ÍÀ¹É•… àÄäÈ¤¹‘•½‘” ‰ÕÑ˜´àˆ°€‰É•Á±…”ˆ¤(€€€€€€€É•ÑÕÉ¸€ˆñÑ¥Ñ±”ù=±¼ÌQY5…Ñ”ð½Ñ¥Ñ±”øˆ¥¸Á…”(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€É•ÑÕÉ¸…±Í”()‘•˜µ…¥¸ ¤è(€€€Á½ÉÐ€ôA=IP(€€€¥˜€ˆ´µÁ½ÉÐˆ¥¸ÍåÌ¹…ÉØè(€€€€€€€ÑÉäè(€€€€€€€€€€€Á½ÉÐ€ô¥¹Ð¡ÍåÌ¹…ÉÙmÍåÌ¹…ÉØ¹¥¹‘•à ˆ´µÁ½ÉÐˆ¤€¬€Åt¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€Á…ÍÌ(€€€ÕÉ°€ô˜‰¡ÑÑÀè¼½±½…±¡½ÍÐéíÁ½ÉÑôˆ(€€€€ŒM¥¹±”µ¥¹ÍÑ…¹”¡•¬½µ•Ì‰•™½É”±…Õ¹¡•Èµ¥É…Ñ¥½¸½É•±…Õ¹ ±½¥Œ¸(€€€€ŒÍ•½¹½ÁäÍ¡½Õ±¹•Ù•ÈÉ•Á±…”½É•ÍÑ…ÉÐ…¹åÑ¡¥¹œÕ¹‘•É¹•…Ñ Ñ¡”(€€€€Œ…±É•…‘äµÉÕ¹¹¥¹œ…ÁÀì¥Ð©ÕÍÐ‰É¥¹ÌÑ¡”•á¥ÍÑ¥¹œU$‰…¬Ñ¼Ñ¡”ÕÍ•È¸(€€€¥˜}•á¥ÍÑ¥¹}ÑÙµ…Ñ”¡Á½ÉÐ¤è(€€€€€€€ÑÉäè(€€€€€€€€€€€Ý•‰‰É½ÝÍ•È¹½Á•¸¡ÕÉ°¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€Á…ÍÌ(€€€€€€€É•ÑÕÉ¸(€€€™œ€ô±½…‘}½¹™¥œ ¤(€€€¡¥‘•}½¹Í½±”€ôQÉÕ”(€€€¡¥‘‘•¹}¡¥±€ô½Ì¹•¹Ù¥É½¸¹•Ð ‰QY5Q}!%9}!%1ˆ¤€ôô€ˆÄˆ(€€€¥˜ÍåÌ¹Á±…Ñ™½É´¹ÍÑ…ÉÑÍÝ¥Ñ  ‰Ý¥¸ˆ¤…¹¹½Ð¡¥‘‘•¹}¡¥±è(€€€€€€€€Œ5¥É…Ñ”…¸½±±…Õ¹¡•È‰•™½É”¥Ð…¸É•±…Õ¹ ¥ÑÍ•±˜½ÈÍÑ…ÉÐÑ¡”(€€€€€€€€Œ¹½Éµ…°Í•ÉÙ•È¸Q¡¥Ì…±Í¼½Ù•ÉÌ„½±‰½½ÑÍÑÉ…ÀÝ¡•É”¹¼ÁÉ•Ù¥½ÕÌ(€€€€€€€€Œ±½…°ÑÙµ…Ñ”¹Áä•á¥ÍÑ•è…ÌÍ½½¸…ÌÑ¡”±…Õ¹¡•ÈÉÕ¹ÌÑ¡¥ÌÕÉÉ•¹Ð(€€€€€€€€ŒÍÉ¥ÁÐ°¥Ð…¸É•Á±…”¥ÑÍ•±˜½¹”…¹É•ÍÑ…ÉÐ±•…¹±ä¸(€€€€€€€¥˜¹½Ð}±…Õ¹¡•É}¥Í}ÕÉÉ•¹Ð ¤…¹}ÍÑ…ÉÑ}±…Õ¹¡•É}µ¥É…Ñ¥½¸ ¤è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€€ŒU¹­¹½Ý¸½É•¹…µ•±•…ä±…Õ¹¡•ÉÌ…¹¹½Ð‰”Í…™•±ä™½É”µÉ•Á±…•¸(€€€€€€€€Œ-••ÀÑ¡”½±¡¥‘‘•¸µ¡¥±™…±±‰…¬™½ÈÑ¡½Í”…Í•Ì¸Q¡”Ù•É¥™¥•U$(€€€€€€€€Œ±…Õ¹¡•È¥Ì…±É•…‘äÝ¥¹‘½Ý±•ÍÌ…¹¹••‘Ì¹¼•áÑÉ„Í•±˜µÉ•±…Õ¹ ¸(€€€€€€€¥˜¹½Ð}±…Õ¹¡•É}¥Í}ÕÉÉ•¹Ð ¤…¹¡¥‘•}½¹Í½±”è(€€€€€€€€€€€¥˜}±…Õ¹¡}Ý¥Ñ¡½ÕÑ}½¹Í½±” ¤è(€€€€€€€€€€€€€€€}±½Í•}±…Õ¹¡•É}½¹Í½±” ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸(€€€Í•ÉÙ•È€ôQ¡É•…‘¥¹!QQAM•ÉÙ•È  ˆÄÈÜ¸À¸À¸Äˆ°Á½ÉÐ¤°!…¹‘±•È¤(€€€}MQ=A}Y9P¹±•…È ¤(€€€}µ…É­}…ÁÁ}…Ñ¥Ù¥Ñä ¤(€€€¥˜¹½Ð¡¥‘•}½¹Í½±”…¹ÍåÌ¹Á±…Ñ™½É´¹ÍÑ…ÉÑÍÝ¥Ñ  ‰Ý¥¸ˆ¤è(€€€€€€€€ŒQ¡”U$µÍÕ‰ÍåÍÑ•´=QY4±…Õ¹¡•È¥¹Ñ•¹Ñ¥½¹…±±äÍÑ…ÉÑÌÝ¥Ñ¡½ÕÐ„(€€€€€€€€Œ½¹Í½±”¸€I•ÑÉ¼µ½‘”½ÁÑÌ‰…¬¥¸…¹É•…Ñ•Ì½¹”¡•É”ìµ…¹Õ…°ÉÕ¹Ì(€€€€€€€€Œ™É½´…¸•á¥ÍÑ¥¹œÑ•Éµ¥¹…°Í¥µÁ±ä­••ÀÕÍ¥¹œÑ¡•¥ÈÕÉÉ•¹Ð½¹Í½±”¸(€€€€€€€}Í•Ñ}½¹Í½±•}Ù¥Í¥‰±”¡QÉÕ”¤(€€€ÕÍ•}½±½È€ô}•¹…‰±•}…¹Í¤ ¤¥˜¹½Ð¡¥‘•}½¹Í½±”•±Í”…±Í”(€€€¥˜¹½Ð¡¥‘•}½¹Í½±”è(€€€€€€€ÑÉäè(€€€€€€€€€€€ÁÉ¥¹Ð¡}½±½É•‘}‰…¹¹•È¡ÕÍ•}½±½È¤¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡	99H¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€ÁÉ¥¹Ð ˆ€€ˆ€¬€ˆôˆ€¨€ÔØ¤(€€€€€€€ÁÉ¥¹Ð¡˜ˆ€€=±¼ÌQY5…Ñ”¥ÌIU99%9€€€¡ÙíYIM%=9ô¤ˆ¤(€€€€€€€ÁÉ¥¹Ð¡˜ˆ€€€€]…Ñ ¡•É”€´ø€€íÕÉ±ôˆ¤(€€€€€€€ÁÉ¥¹Ð ˆ€€€€Q¼EU%P€€€€´ø€€±½Í”Ñ¡¥ÌÝ¥¹‘½Ü€€€¡½ÈÁÉ•ÍÌÑÉ°­¤ˆ¤(€€€€€€€ÁÉ¥¹Ð ˆ€€ˆ€¬€ˆôˆ€¨€ÔØ¤(€€€€ŒM•ÉÙ”Ñ¡”…ÁÀ¥¸Ñ¡”‰…­É½Õ¹Í¼Ñ¡”Í•ÉÙ•È¥ÌÉ•…‘ä‰•™½É”Ý”½Á•¸¸(€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ÐõÍ•ÉÙ•È¹Í•ÉÙ•}™½É•Ù•È°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤(€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•Ðõ}…ÕÑ½}Í¡ÕÑ‘½Ý¹}Ý…Ñ¡‘½œ°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤(€€€¥˜¡¥‘•}½¹Í½±”è(€€€€€€€€Œ!¥‘‘•¸µ½‘”…¹¹½ÐÝ…¥Ð™½È½¹Í½±”¥¹ÁÕÐè±…Õ¹ Ñ¡”U$¥µµ•‘¥…Ñ•±ä¸(€€€€€€€ÑÉäè(€€€€€€€€€€€Ý•‰‰É½ÝÍ•È¹½Á•¸¡ÕÉ°¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€Á…ÍÌ(€€€€€€€€ŒIQ}9=}]%9=\¡¥±‘É•¸…É”…±É•…‘ä¡¥‘‘•¸¸Q¡¥Ì¥Ì½¹±ä„(€€€€€€€€Œ™…±±‰…¬™½ÈÁ±…Ñ™½ÉµÌ½±…Õ¹¡•ÉÌÝ¡•É”Ñ¡”É•±…Õ¹ Ý…ÌÕ¹…Ù…¥±…‰±”¸(€€€€€€€¥˜¹½Ð¡¥‘‘•¹}¡¥±è(€€€€€€€€€€€}Í•Ñ}½¹Í½±•}Ù¥Í¥‰±”¡…±Í”¤(€€€•±Í”è(€€€€€€€€Œ9½Éµ…°µ½‘”­••ÁÌÑ¡”™…µ¥±¥…ÈÁ…¹…­”ÁÉ½µÁÐ…¹Ý…¥ÑÌ™½È¹Ñ•È¸(€€€€€€€¥µÁ½ÉÐÉ…¹‘½´…Ì}É¹(€€€€€€€ÁÉ½µÁÐ€ô}É¹¹¡½¥”¡9QI}AI=5AQL¤(€€€€€€€±¥¹”€ô€ˆ€€ˆ€¬€¡}=1€¬ÁÉ½µÁÐ€¬}IMP¥˜ÕÍ•}½±½È•±Í”ÁÉ½µÁÐ¤(€€€€€€€ÑÉäè(€€€€€€€€€€€¥¹ÁÕÐ ‰q¸ˆ€¬±¥¹”€¬€‰q¸ˆ¤(€€€€€€€€€€€Ý•‰‰É½ÝÍ•È¹½Á•¸¡ÕÉ°¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€Œ9¼½¹Í½±”¥¹ÁÕÐ…Ù…¥±…‰±”€¡•‘”…Í”¤€´©ÕÍÐ½Á•¸Ñ¡”‰É½ÝÍ•È¸(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€Ý•‰‰É½ÝÍ•È¹½Á•¸¡ÕÉ°¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€Á…ÍÌ(€€€€Œ-••ÀÉÕ¹¹¥¹œÕ¹Ñ¥°ÑÉ°­°Ñ¡”½¹Í½±”±½Í•Ì°½ÈÑ¡”Ý•ˆU$…Í­ÌÕÌÑ¼ÍÑ½À¸(€€€ÑÉäè(€€€€€€€}MQ=A}Y9P¹Ý…¥Ð ¤(€€€•á•ÁÐ-•å‰½…É‘%¹Ñ•ÉÉÕÁÐè(€€€€€€€ÁÉ¥¹Ð ‰q¸€MÑ½ÁÁ¥¹œ=±¼ÌQY5…Ñ”¸	å”„ˆ¤(€€€™¥¹…±±äè(€€€€€€€Í•ÉÙ•È¹Í¡ÕÑ‘½Ý¸ ¤()‘•˜}Ñ}Í±••À¡Í•Œ¤è(€€€¥µÁ½ÉÐÑ¥µ”…Ì}Ð(€€€}Ð¹Í±••À¡Í•Œ¤()‘•˜ÉÕ¹}Í•±™}Ñ•ÍÑÌ ¤è(€€€€ˆˆ‰…ÍÐ°½™™±¥¹”¡•­Ì™½ÈÑ¡”Íµ…±°Á¥••Ìµ½ÍÐ±¥­•±äÑ¼‰É•…¬ÕÁ‘…Ñ•Ì¸ˆˆˆ(€€€¡•­Ì€ômt(€€€‘•˜¡•¬¡¹…µ”°½¹‘¥Ñ¥½¸¤è(€€€€€€€¥˜¹½Ð½¹‘¥Ñ¥½¸è(€€€€€€€€€€€É…¥Í”ÍÍ•ÉÑ¥½¹ÉÉ½È¡¹…µ”¤(€€€€€€€¡•­Ì¹…ÁÁ•¹¡¹…µ”¤(€€€¡•¬ ‰Ù•ÉÍ¥½¸½É‘•É¥¹œˆ°}Á…ÉÍ•}Ù•È ˆÀ¸ÜÜÜ¹ˆÌààˆ¤€ø}Á…ÉÍ•}Ù•È ˆÀ¸ÜÜÜ¹ˆÌàÜˆ¤¤(€€€¡•¬ ‰Ù•ÉÍ¥½¸•ÅÕ…±¥Ñäˆ°}Á…ÉÍ•}Ù•È ‰ØÀ¸ÜÜÜ¹ˆÌààˆ¤€ôô}Á…ÉÍ•}Ù•È ˆÀ¸ÜÜÜ¹ˆÌààˆ¤¤(€€€…¡•}‰ÕÍÑ•€ô}…¡•}‰ÕÍÑ•‘}ÕÉ° (€€€€€€€€‰¡ÑÑÁÌè¼½É…Ü¹¥Ñ¡Õ‰ÕÍ•É½¹Ñ•¹Ð¹½´½•á…µÁ±”½…ÁÀ½µ…¥¸½Ù•ÉÍ¥½¸¹ÑáÐýÍ½ÕÉ”õµ…¹Õ…°ˆ°(€€€€€€€€ˆÄÈÌˆ¤(€€€¡•¬ ‰ÕÁ‘…Ñ”UI1ÌÁÉ•Í•ÉÙ”ÅÕ•É¥•Ì…¹‰åÁ…ÍÌÉ…Üµ½¹Ñ•¹Ð…¡•Ìˆ°(€€€€€€€€€€‰Í½ÕÉ”õµ…¹Õ…°ˆ¥¸…¡•}‰ÕÍÑ•…¹€‰}ÑÙµ…Ñ”ôÄÈÌˆ¥¸…¡•}‰ÕÍÑ•¤(€€€¡•¬ ‰ÍÁ½ÉÑÌ•Ù•¹Ð…¡”­•ä¹½Éµ…±¥é•ÌÑ•…µÌˆ°(€€€€€€€€€}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä ‰1••‘ÌU¹¥Ñ•ˆ°€‰5…¸UÑˆ°€ˆÈÀÈØ´Àà´ÄÉPÈÀèÌÀèÀÁhˆ¤€ôô(€€€€€€€€€}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä ˆ±••‘ÌÕ¹¥Ñ•€ˆ°€‰58UQˆ°€ˆÈÀÈØ´Àà´ÄÉPÈÀèÌÀèÔåhˆ¤¤(€€€Í¡•‘Õ±•}Ñ•ÍÐ€ôl(€€€€€€€ì‰¡½µ”ˆè€‰!•…ÉÑÌˆ°€‰…Ý…äˆè€‰	•¹™¥„ˆ°€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÍPÄàèÐÔèÀÁhˆ°(€€€€€€€€€‰‰å}½Õ¹ÑÉäˆèíõô°(€€€€€€€ì‰¡½µ”ˆè€‰!•…ÉÑÌˆ°€‰…Ý…äˆè€‰%¹Ù•É¹•ÍÌˆ°€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÙPÄÌèÀÀèÀÁhˆ°(€€€€€€€€€‰‰å}½Õ¹ÑÉäˆèíõõt(€€€}½Ù•É±…å}™¥áÑÕÉ•}É½ÝÌ¡Í¡•‘Õ±•}Ñ•ÍÐ°mì(€€€€€€€€‰¡½µ”ˆè€‰!•…ÉÐ½˜5¥‘±½Ñ¡¥…¸ˆ°€‰…Ý…äˆè€‰	•¹™¥„ˆ°(€€€€€€€€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÍPÄàèÐÔèÀÁhˆ°(€€€€€€€€‰‰å}½Õ¹ÑÉäˆèì‰APˆèl‰MÁ½ÉÐQX€Ô‰uõõt¤(€€€¡•¬ ‰QX±¥ÍÑ¥¹Ì•¹É¥ Ý¥Ñ¡½ÕÐÉ•‘Õ¥¹œÑ•…´Í¡•‘Õ±”ˆ°(€€€€€€€€€±•¸¡Í¡•‘Õ±•}Ñ•ÍÐ¤€ôô€È…¹(€€€€€€€€€Í¡•‘Õ±•}Ñ•ÍÑlÁul‰‰å}½Õ¹ÑÉä‰t€ôôì‰APˆèl‰MÁ½ÉÐQX€Ô‰uô¤(€€€±ÑÙ}Ñ•ÍÐ€ô}Á…ÉÍ•}±ÑÙ}‘…¥±ä œœœñÑÈ±…ÍÌô‰µ…Ñ¡É½ÜˆøñÑøñ„¡É•˜ôˆ½µ…Ñ ½à¼ˆù!•…ÉÑÌÙÌ	•¹™¥„ð½„øð½ÑøñÑ¥ô‰¡…¹¹•±Ìˆøñ„‘…Ñ„µ½Õ¹ÑÉäô‰A½ÉÑÕ…°ˆùMÁ½ÉÐQXÔð½„øñ„‘…Ñ„µ½Õ¹ÑÉäô‰U¹¥Ñ•-¥¹‘½´ˆù!•…ÉÑÌQXð½„øð½Ñøð½ÑÈøœœœ°€ˆÈÀÈØ´Àà´ÄÌˆ¤(€€€¡•¬ ‰1QXÁ…ÉÍ•È•áÑÉ…ÑÌ¡…¹¹•±ÌÝ¥Ñ¡½ÕÐÉ•…Ñ¥¹œ™¥áÑÕÉ•Ìˆ°(€€€€€€€€€±•¸¡±ÑÙ}Ñ•ÍÐ¤€ôô€Ä…¹±ÑÙ}Ñ•ÍÑlÁul‰¡½µ”‰t€ôô€‰!•…ÉÑÌˆ…¹(€€€€€€€€€±ÑÙ}Ñ•ÍÑlÁul‰‰å}½Õ¹ÑÉä‰t€ôôì‰APˆèl‰MÁ½ÉÐQXÔ‰t°€‰U,ˆèl‰!•…ÉÑÌQX‰uô¤(€€€±ÑÙ}±•…å}½Õ¹ÑÉå}Ñ•ÍÐ€ô}Á…ÉÍ•}±ÑÙ}‘…¥±ä œœœñÑÈ±…ÍÌô‰µ…Ñ¡É½ÜˆøñÑøñ„¡É•˜ôˆ½µ…Ñ ½à¼ˆù9½ÑÑ¥¹¡…´½É•ÍÐÙÌ1••‘ÌU¹¥Ñ•ð½„øð½ÑøñÑ¥ô‰¡…¹¹•±Ìˆøñ„¡É•˜ôˆ½¡…¹¹•±Ì½Ù¥…Á±…äµ¹½ÉÝ…ä¼ˆùY¥…Á±…ä9½ÉÝ…äð½„øñ„¡É•˜ôˆ½¡…¹¹•±Ì½Ù¥…Á±…äµ™¥¹±…¹¼ˆùY¥…Á±…ä¥¹±…¹ð½„øð½Ñøð½ÑÈøœœœ°€ˆÈÀÈØ´Àà´ÈÈˆ¤(€€€¡•¬ ‰±•…ä1QX±¥¹­Ì¥¹™•È½Õ¹ÑÉ¥•Ì™É½´‰É½…‘…ÍÑ•È¹…µ•Ìˆ°(€€€€€€€€€±ÑÙ}±•…å}½Õ¹ÑÉå}Ñ•ÍÑlÁul‰‰å}½Õ¹ÑÉä‰t€ôôì(€€€€€€€€€€€€€€‰9<ˆèl‰Y¥…Á±…ä9½ÉÝ…ä‰t°€‰$ˆèl‰Y¥…Á±…ä¥¹±…¹‰uô¤(€€€±ÑÙ}ÕÉÉ•¹Ñ}Ñ•ÍÐ€ô}Á…ÉÍ•}±ÑÙ}‘…¥±ä œœœñÍ•Ñ¥½¸øñ„¡É•˜ôˆ½µ…Ñ ½¹½ÑÑ¥¹¡…´µ™½É•ÍÐµÙÌµ±••‘Ì¼ˆù9½ÑÑ¥¹¡…´½É•ÍÐÙÌ1••‘ÌU¹¥Ñ•ð½„øñ„¡É•˜ôˆ½¡…¹¹•±Ì½‘…é¸µÍÁ…¥¸¼ˆùi8MÁ…¥¸ð½„øñ„¡É•˜ôˆ½¡…¹¹•±Ì½Ù¥…Á±…äµ‘•¹µ…É¬¼ˆùY¥…Á±…ä•¹µ…É¬ð½„øñ„¡É•˜ôˆ½¡…¹¹•±Ì½Ù¥…Á±…äµÍÝ•‘•¸¼ˆùY¥…Á±…äMÝ•‘•¸ð½„øñ„¡É•˜ôˆ½¡…¹¹•±Ì½Ù¥…Á±…äµ¹½ÉÝ…ä¼ˆùY¥…Á±…ä9½ÉÝ…äð½„øð½Í•Ñ¥½¸øñÍ•Ñ¥½¸øñ„¡É•˜ôˆ½µ…Ñ ½½Ñ¡•È¼ˆù=Ñ¡•ÈÙÌ±Í”ð½„øð½Í•Ñ¥½¸øœœœ°€ˆÈÀÈØ´Àà´ÈÈˆ¤(€€€¡•¬ ‰ÕÉÉ•¹Ð1QX…É‘Ì…ÑÑ… Y¥…Á±…äÑ¼½É•ÍÐ1••‘Ìˆ°(€€€€€€€€€±•¸¡±ÑÙ}ÕÉÉ•¹Ñ}Ñ•ÍÐ¤€øô€Ä…¹(€€€€€€€€€±ÑÙ}ÕÉÉ•¹Ñ}Ñ•ÍÑlÁul‰¡½µ”‰t€ôô€‰9½ÑÑ¥¹¡…´½É•ÍÐˆ…¹(€€€€€€€€€ì‰Y¥…Á±…ä•¹µ…É¬ˆ°€‰Y¥…Á±…äMÝ•‘•¸ˆ°€‰Y¥…Á±…ä9½ÉÝ…ä‰ô¹¥ÍÍÕ‰Í•Ð (€€€€€€€€€€€€€í¹…µ”™½È¹…µ•Ì¥¸±ÑÙ}ÕÉÉ•¹Ñ}Ñ•ÍÑlÁul‰‰å}½Õ¹ÑÉä‰t¹Ù…±Õ•Ì ¤(€€€€€€€€€€€€€€™½È¹…µ”¥¸¹…µ•Íô¤¤(€€€¡•¬ ‰½Ñ5½ˆ9½ÑÑ´½É•ÍÐ…‰‰É•Ù¥…Ñ¥½¸µ…Ñ¡•Ì1QX9½ÑÑ¥¹¡…´½É•ÍÐˆ°(€€€€€€€€€}Ñ•…µ}¹…µ•Í}•ÅÕ¥Ù…±•¹Ð ‰9½ÑÑ´½É•ÍÐˆ°€‰9½ÑÑ¥¹¡…´½É•ÍÐˆ¤…¹(€€€€€€€€€}Ñ•…µ}¹…µ•Í}•ÅÕ¥Ù…±•¹Ð ‰9½ÑÑ¥¹¡…´½É•ÍÐˆ°€‰9½ÑÑ´½É•ÍÐˆ¤¤(€€€…‰‰É•Ù¥…Ñ•‘}™¥áÑÕÉ”€ômì‰¡½µ”ˆè€‰9½ÑÑ´½É•ÍÐˆ°€‰…Ý…äˆè€‰1••‘ÌU¹¥Ñ•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÈÉPÄÐèÀÀèÀÁhˆ°€‰‰å}½Õ¹ÑÉäˆèíõõt(€€€}½Ù•É±…å}™¥áÑÕÉ•}É½ÝÌ¡…‰‰É•Ù¥…Ñ•‘}™¥áÑÕÉ”°mì(€€€€€€€€‰¡½µ”ˆè€‰9½ÑÑ¥¹¡…´½É•ÍÐˆ°€‰…Ý…äˆè€‰1••‘ÌU¹¥Ñ•ˆ°(€€€€€€€€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÈÈˆ°€‰‰å}½Õ¹ÑÉäˆèì‰9<ˆèl‰Y¥…Á±…ä9½ÉÝ…ä‰uõõt¤(€€€¡•¬ ‰½É•ÍÐ…‰‰É•Ù¥…Ñ¥½¸É•Ñ…¥¹Ì9½ÉÝ•¥…¸1QX±¥ÍÑ¥¹œ½Ù•É±…äˆ°(€€€€€€€€€…‰‰É•Ù¥…Ñ•‘}™¥áÑÕÉ•lÁul‰‰å}½Õ¹ÑÉä‰t€ôôì‰9<ˆèl‰Y¥…Á±…ä9½ÉÝ…ä‰uô¤(€€€±ÑÙ}µ…Ñ¡}‘•Ñ…¥±}Ñ•ÍÐ€ô}Á…ÉÍ•}±ÑÙ}µ…Ñ¡}±¥ÍÑ¥¹Ì œœœ(€€€€€€ñ Èù%¹Ñ•É¹…Ñ¥½¹…°QXð½ ÈøñÑ…‰±”ø(€€€€€€ñÑÈøñÑù9½ÉÝ…äð½ÑøñÑøñ„¡É•˜ôˆ½¡…¹¹•±Ì½Ù¥…Á±…äµ¹½ÉÝ…ä¼ˆùY¥…Á±…ä9½ÉÝ…äð½„ø(€€€€€€ñ„¡É•˜ôˆ½¡…¹¹•±Ì½ÑØÈµÁ±…äµ¹½ÉÝ…ä¼ˆùQX€ÈA±…äð½„ø(€€€€€€ñ„¡É•˜ôˆ½¡…¹¹•±Ì½ØµÍÁ½ÉÐ´Äµ¹½ÉÝ…ä¼ˆùXMÁ½ÉÐ€Ä9½ÉÝ…äð½„øð½Ñøð½ÑÈø(€€€€€€ñÑÈøñÑùMÝ•‘•¸ð½ÑøñÑøñ„¡É•˜ôˆ½¡…¹¹•±Ì½Ù¥…Á±…äµÍÝ•‘•¸¼ˆùY¥…Á±…äMÝ•‘•¸ð½„øð½Ñøð½ÑÈø(€€€€€€ð½Ñ…‰±”øñ Èù5…Ñ •Ñ…¥±Ìð½ Èøœœœ¤(€€€¡•¬ ‰1QXµ…Ñ Á…”•áÑÉ…ÑÌ½µÁ±•Ñ”9½ÉÝ•¥…¸¡…¹¹•°É½Üˆ°(€€€€€€€€€±ÑÙ}µ…Ñ¡}‘•Ñ…¥±}Ñ•ÍÐ€ôôì(€€€€€€€€€€€€€€‰9<ˆèl‰Y¥…Á±…ä9½ÉÝ…äˆ°€‰QX€ÈA±…äˆ°€‰XMÁ½ÉÐ€Ä9½ÉÝ…ä‰t°(€€€€€€€€€€€€€€‰Mˆèl‰Y¥…Á±…äMÝ•‘•¸‰uô¤(€€€¡•¬ ‰•áÁ…¹‘•1QX½Õ¹ÑÉ¥•ÌÉ•½¹¥é”	½Í¹¥„…¹9•Üi•…±…¹ˆ°(€€€€€€€€€}}™É½µ}¹…µ” ‰	½Í¹¥„…¹!•Éé•½Ù¥¹„ˆ¤€ôô€‰‰„ˆ…¹(€€€€€€€€€}}™É½µ}¹…µ” ‰9•Üi•…±…¹ˆ¤€ôô€‰¹èˆ¤(€€€¹•…Ñ¥Ù•}ÕÉ°€ô€‰¡ÑÑÁÌè¼½ÝÝÜ¹±¥Ù•Í½•ÉÑØ¹½´½µ…Ñ ½½™™±¥¹”µÍ•±˜µÑ•ÍÐ¼ˆ(€€€}1QY}5Q!}%1UIMm¹•…Ñ¥Ù•}ÕÉ±t€ôì‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°€‰•ÉÉ½Èˆè€‰…¡•™…¥±ÕÉ”‰ô(€€€¹•…Ñ¥Ù•}‰±½­•€ô…±Í”(€€€ÑÉäè(€€€€€€€™•Ñ¡}±ÑÙ}µ…Ñ¡}±¥ÍÑ¥¹Ì¡¹•…Ñ¥Ù•}ÕÉ°¤(€€€•á•ÁÐIÕ¹Ñ¥µ•ÉÉ½È…Ì•áŒè(€€€€€€€¹•…Ñ¥Ù•}‰±½­•€ô€‰…¡•™…¥±ÕÉ”ˆ¥¸ÍÑÈ¡•áŒ¤(€€€™¥¹…±±äè(€€€€€€€}1QY}5Q!}%1UIL¹Á½À¡¹•…Ñ¥Ù•}ÕÉ°°9½¹”¤(€€€€€€€}1QY}5Q!}1=-L¹Á½À¡¹•…Ñ¥Ù•}ÕÉ°°9½¹”¤(€€€¡•¬ ‰™…¥±•1QXµ…Ñ É•ÅÕ•ÍÐ¥Ì¹•…Ñ¥Ù•±ä…¡•ˆ°¹•…Ñ¥Ù•}‰±½­•¤(€€€¡•¬ ‰1QX‘…¥±äÉ½ÜÉ•Ñ…¥¹Ì‘•Ñ…¥°UI0ˆ°(€€€€€€€€€±ÑÙ}ÕÉÉ•¹Ñ}Ñ•ÍÑlÁt¹•Ð ‰µ…Ñ¡}ÕÉ°ˆ¤€ôô(€€€€€€€€€€‰¡ÑÑÁÌè¼½ÝÝÜ¹±¥Ù•Í½•ÉÑØ¹½´½µ…Ñ ½¹½ÑÑ¥¹¡…´µ™½É•ÍÐµÙÌµ±••‘Ì¼ˆ¤(€€€Ù}ÍÁ½ÉÑ}•á…Ð€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰XMÁ½ÉÐ€Ä9½ÉÝ…ä‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐ€Ä!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈä°€‰…Ñ•½Éå}¥ˆè€‰¹¼‰õt°(€€€€€€€ì‰¹¼ˆè€‰9<ð9=I]d‰ô°€À¸ØÈ¤(€€€¡•¬ ‰½Õ¹ÑÉäµÍÕ™™¥á•XMÁ½ÉÐ™••¥Ì•á…Ð9½ÉÝ•¥…¸ÁÉ½Ù¥‘•Èˆ°(€€€€€€€€€±•¸¡Ù}ÍÁ½ÉÑ}•á…Ð¤€ôô€Ä…¹Ù}ÍÁ½ÉÑ}•á…ÑlÁul‰Í½É”‰t€ôô€Ä¸À…¹(€€€€€€€€€Ù}ÍÁ½ÉÑ}•á…ÑlÁul‰ÁÉ½Ù¥‘•É}•á…Ð‰t¥ÌQÉÕ”¤(€€€Ù}ÍÁ½ÉÑ}±ÑÙ}½Õ¹ÑÉå}¹…µ”€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰1QXˆèl‰XMÁ½ÉÐ€Ä9½ÉÝ…ä‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<XMA=IP€Ä!YI\!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈäÄ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰µ¥ÍŒ‰õt°ì‰µ¥ÍŒˆè€‰Y%@=1‰ô°€À¸ØÈ¤(€€€¡•¬ ‰1QX½Õ¹ÑÉäµ¥¸µ¹…µ”XMÁ½ÉÐ€Ä¥Ì„Í•ÕÉ”•á…ÐÁÉ½Ù¥‘•Èˆ°(€€€€€€€€€±•¸¡Ù}ÍÁ½ÉÑ}±ÑÙ}½Õ¹ÑÉå}¹…µ”¤€ôô€Ä…¹(€€€€€€€€€Ù}ÍÁ½ÉÑ}±ÑÙ}½Õ¹ÑÉå}¹…µ•lÁul‰Í½É”‰t€ôô€Ä¸À…¹(€€€€€€€€€Ù}ÍÁ½ÉÑ}±ÑÙ}½Õ¹ÑÉå}¹…µ•lÁul‰ÁÉ½Ù¥‘•É}•á…Ð‰t¥ÌQÉÕ”…¹(€€€€€€€€€Ù}ÍÁ½ÉÑ}±ÑÙ}½Õ¹ÑÉå}¹…µ•lÁul‰½Õ¹ÑÉä‰t€ôô€‰9<ˆ¤(€€€Ù}ÍÁ½ÉÑ}‰…É•}Œ€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰XMÁ½ÉÐ€Ä9½ÉÝ…ä‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<XMA=IP€ÄI\ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÌÀ°€‰…Ñ•½Éå}¥ˆè€‰¹¼‰õt°(€€€€€€€ì‰¹¼ˆè€‰9<ð9=I]d‰ô°€À¸ØÈ¤(€€€¡•¬ ‰‰…É”9<ÁÉ•™¥àÍÑ¥±°å¥•±‘Ì•á…ÐXMÁ½ÉÐ€ÄÁÉ½Ù¥‘•Èˆ°(€€€€€€€€€±•¸¡Ù}ÍÁ½ÉÑ}‰…É•}Œ¤€ôô€Ä…¹Ù}ÍÁ½ÉÑ}‰…É•}lÁul‰Í½É”‰t€ôô€Ä¸À…¹(€€€€€€€€€Ù}ÍÁ½ÉÑ}‰…É•}lÁul‰ÁÉ½Ù¥‘•É}•á…Ð‰t¥ÌQÉÕ”¤(€€€™½ÈÁÉ•™¥á}Ù…É¥…¹Ð¥¸€ ‰9<XMA=IP€ÄI\ˆ°€‰9=HXMA=IP€Ä!ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‰9=I]dXMA=IP€Äˆ°€‰9=IXMA=IP€Äˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‰XMA=IP€Ä9=HI\ˆ°€‰XMA=IP€Ä9=I]dI\ˆ¤è(€€€€€€€Ù…É¥…¹Ñ}É½ÝÌ€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€€€€€ì‰9<ˆèl‰XMÁ½ÉÐ€Ä9½ÉÝ…ä‰uô°(€€€€€€€€€€€mì‰¹…µ”ˆèÁÉ•™¥á}Ù…É¥…¹Ð°€‰ÍÑÉ•…µ}¥ˆè€ÄÌÄ°€‰…Ñ•½Éå}¥ˆè€‰µ¥ÍŒ‰õt°(€€€€€€€€€€€ì‰µ¥ÍŒˆè€‰Y%@=1‰ô°€À¸ØÈ¤(€€€€€€€¡•¬¡˜‰9½ÉÝ•¥…¸XMÁ½ÉÐÁÉ•™¥àÙ…É¥…¹Ðµ…Ñ¡•ÌèíÁÉ•™¥á}Ù…É¥…¹Ñôˆ°(€€€€€€€€€€€€€±•¸¡Ù…É¥…¹Ñ}É½ÝÌ¤€ôô€Ä…¹Ù…É¥…¹Ñ}É½ÝÍlÁul‰Í½É”‰t€ôô€Ä¸À…¹(€€€€€€€€€€€€€Ù…É¥…¹Ñ}É½ÝÍlÁul‰ÁÉ½Ù¥‘•É}•á…Ð‰t¥ÌQÉÕ”¤(€€€Ù¥…Á±…å}‰…É•}Ù}ÍÁ½ÉÐ€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰Y¥…Á±…ä9½ÉÝ…ä‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<XMA=IP€ÄI\ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÌÈ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰µ¥ÍŒ‰õt°ì‰µ¥ÍŒˆè€‰Y%@=1‰ô°€À¸ØÈ¤(€€€¡•¬ ‰Y¥…Á±…ä9½ÉÝ…ä•áÁ…¹‘Ì™É½´‰…É”µÁÉ•™¥àXMÁ½ÉÐ¹…µ”ˆ°(€€€€€€€€€±•¸¡Ù¥…Á±…å}‰…É•}Ù}ÍÁ½ÉÐ¤€ôô€Ä…¹(€€€€€€€€€Ù¥…Á±…å}‰…É•}Ù}ÍÁ½ÉÑlÁul‰Í½É”‰t€ôô€À¸äÐ¤(€€€Ù¥…Á±…å}½‘•}Ù…É¥…¹ÑÌ€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰Y¥…Á±…ä€¡9<¤‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<XMA=IP€Ä!YI\!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÌÌ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰µ¥ÍŒ‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰9=HèXMA=IPAI5%H1U€È ¸ÈØÔˆ°(€€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè€ÄÌÐ°€‰…Ñ•½Éå}¥ˆè€‰µ¥ÍŒ‰õt°(€€€€€€€ì‰µ¥ÍŒˆè€‰Y%@=1‰ô°€À¸ØÈ¤(€€€¡•¬ ‰Y¥…Á±…ä9<•áÁ…¹‘Ì½‘•Œµ±…‰•±±•9½ÉÝ•¥…¸XMÁ½ÉÐ™••‘Ìˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸Ù¥…Á±…å}½‘•}Ù…É¥…¹ÑÍô€ôôìÄÌÌ°€ÄÌÑô¤(€€€¡•¬ ‰•á…ÐXMÁ½ÉÐÁÉ½Ù¥‘•ÈÍ½ÉÑÌ…¡•…½˜Á½ÍÍ¥‰±”•Ù•¹Ð¡…¹¹•±Ìˆ°(€€€€€€€€€€‰½¹ÍÐÍÕÉ”õ ôù ˜™ ¹ÁÉ½Ù¥‘•É}•á…ÐôôõÑÉÕ”üÌèˆ¥¸A…¹(€€€€€€€€€€‰¡…¹¹•±1½…±•AÉ¥½É¥Ñä¡ˆ¤µ¡…¹¹•±1½…±•AÉ¥½É¥Ñä¡„¥ññÍÕÉ”¡ˆ¤µÍÕÉ”¡„¤ˆ¥¸A¤(€€€¡•¬ ‰¡…¹¹•°É•ÍÕ±ÑÌÁÉ•Í•ÉÙ”9<€Ñ,°9<°€Ñ,°M]°8°%8Ñ¥•ÉÌˆ°(€€€€€€€€€€‰¥˜¡¥Í9½ÉÝ•¥…¸˜™¥ÌÑ¬¥É•ÑÕÉ¸€ÜÀÀˆ¥¸A…¹(€€€€€€€€€€‰¥˜¡¥Í9½ÉÝ•¥…¸¥É•ÑÕÉ¸€ØÀÀˆ¥¸A…¹(€€€€€€€€€€‰¥˜¡¥ÌÑ¬¥É•ÑÕÉ¸€ÔÀÀˆ¥¸A…¹(€€€€€€€€€€‰¥˜¡¥ÍMÝ•‘¥Í ¥É•ÑÕÉ¸€ÐÀÀˆ¥¸A…¹(€€€€€€€€€€‰¥˜¡¥Í…¹¥Í ¥É•ÑÕÉ¸€ÌäÀˆ¥¸A…¹(€€€€€€€€€€‰¥˜¡¥Í¥¹¹¥Í ¥É•ÑÕÉ¸€ÌàÀˆ¥¸A…¹(€€€€€€€€€€ˆ¡¹½ñ¹½Éñ¹½ÉÝ…åñ¹½É•ñ¹½ÉÝ•¥…¸¤ˆ¥¸A¤(€€€…Ñ…±½}É½ÝÌ€ôl(€€€€€€€€ ‰M]èXMÁ½ÉÐ€Ä!ˆ°€‰MðMA=IQLˆ¤°(€€€€€€€€ ‰9<èXMÁ½ÉÐ€È!ˆ°€‰9<ðMA=IQLˆ¤°(€€€€€€€€ ‰9=HYMA=IP€ÄI\ˆ°€‰Y%@=1ˆ¤°(€€€€€€€€ ‰U,èAÉ•µ¥•ÈMÁ½ÉÑÌ€Äˆ°€‰U,ðMA=IQLˆ¤°(€€€t(€€€…Ñ…±½}µ…Ñ¡•Ì€ôÍ½ÉÑ• (€€€€€€€€¡É½Ü™½ÈÉ½Ü¥¸…Ñ…±½}É½ÝÌ(€€€€€€€€¥˜}¡…¹¹•±}…Ñ…±½}Í•…É¡}É…¹¬¡É½ÝlÁt°É½ÝlÅt°€‰XMÁ½ÉÐˆ¤¥Ì¹½Ð9½¹”¤°(€€€€€€€­•äõ±…µ‰‘„É½Üè}¡…¹¹•±}…Ñ…±½}Í•…É¡}É…¹¬¡É½ÝlÁt°É½ÝlÅt°€‰XMÁ½ÉÐˆ¤¤(€€€¡•¬ ‰Á±…å±¥ÍÐXMÁ½ÉÐÍ•…É ™¥¹‘Ì½µÁ…Ð¹…µ•Ì…¹É…¹­Ì9½ÉÝ…ä™¥ÉÍÐˆ°(€€€€€€€€€mÉ½ÝlÁt™½ÈÉ½Ü¥¸…Ñ…±½}µ…Ñ¡•Ít€ôô(€€€€€€€€€l‰9<èXMÁ½ÉÐ€È!ˆ°€‰9=HYMA=IP€ÄI\ˆ°€‰M]èXMÁ½ÉÐ€Ä!‰t¤(€€€¡•¬ ‰™¥áÑÕÉ”…Ù…¥±…‰¥±¥Ñä±½…‘ÌÁÉ¥½É¥ÑäÉ•ÍÕ±ÑÌÁÉ½É•ÍÍ¥Ù•±äˆ°(€€€€€€€€€€‰‰…Ñ¡•Ì¹ÁÕÍ ¡™¥áÑÕÉ•Ì¹Í±¥” À°Ì¤¤ˆ¥¸A…¹(€€€€€€€€€€‰™½È¡±•Ð¤ôÌí¤ñ™¥áÑÕÉ•Ì¹±•¹Ñ í¤¬ôÄÈ¤ˆ¥¸A¤(€€€¡•¬ ‰½Á•¹¥¹œ„Ý…¥Ñ¥¹œ™¥áÑÕÉ”ÑÉ¥•ÉÌ„Ñ…É•Ñ•¡…¹¹•°±½½­ÕÀˆ°(€€€€€€€€€€‰Á…¹•°¹Ñ•áÑ½¹Ñ•¹Ð¹¥¹±Õ‘•Ì¡ÑÈ ¡•­¥¹œå½ÕÈ¡…¹¹•±Ì¸¸¸œ¤¤ˆ¥¸A…¹(€€€€€€€€€€‰‰½‘äé)M=8¹ÍÑÉ¥¹¥™ä¡í™¥áÑÕÉ”é™¥áÑÕÉ•ô¤ˆ¥¸A¤(€€€¡•¬ ‰µ…Ñ¡•¡…¹¹•°Ñ¥Ñ±•ÌÁ±…äÝ¥Ñ¡½ÕÐ½±±…ÁÍ¥¹œ™¥áÑÕÉ•Ìˆ°(€€€€€€€€€€‰™¥áÑÕÉ•¡…¹¹•±Ñ¥Ñ±•m‘…Ñ„µÍ¥‘tˆ¥¸A…¹(€€€€€€€€€€‰Á±…å	É½ÝÍ•È¡™¥áÑÕÉ•¡…¹¹•±Q¥Ñ±”¹•ÑÑÑÉ¥‰ÕÑ” ‘…Ñ„µÍ¥œ¤ˆ¥¸A¤(€€€¡•¬ ‰Í•ÕÉ”¡…¹¹•°™…µ¥±¥•ÌÍ¡½Ü™¥Ù”ÅÕ…±¥ÑäÙ…É¥…¹ÑÌ‰•™½É”•áÁ…¹Í¥½¸ˆ°(€€€€€€€€€€‰¤øôÔüœÍ•ÕÉ•µ…Ñ¡•áÑÉ„¡¥‘”œˆ¥¸A…¹(€€€€€€€€€€‰¥Ñ•µÌ¹±•¹Ñ ´Ôˆ¥¸A…¹(€€€€€€€€€€‰Í•ÕÉ•EÕ…±¥ÑåAÉ¥½É¥Ñä¡ˆ¤µÍ•ÕÉ•EÕ…±¥ÑåAÉ¥½É¥Ñä¡„¤ˆ¥¸A…¹(€€€€€€€€€€‰Í•ÕÉ•µ…Ñ¡•áÁ…¹ˆ¥¸A¤(€€€¡•¬ ‰Í•ÕÉ”Í¡½Üµµ½É”•áÁ…¹‘Ì½¸¥ÑÌ™¥ÉÍÐ±¥¬ˆ°(€€€€€€€€€€‰ÅÕ•ÉåM•±•Ñ½É±° œ¹Í•ÕÉ•µ…Ñ¡•áÑÉ…m‘…Ñ„µÍ•ÕÉ”µÉ½ÕÀôˆ¥¸A¤(€€€¡•¬ ‰Á½ÍÍ¥‰±”¡…¹¹•°…Ñ•½É¥•ÌÕÍ”Í¡…É•±½…±”½É‘•É¥¹œˆ°(€€€€€€€€€€‰É½ÕÁ•‘A½ÍÍ¥‰±•¡…¹¹•±Ì¡½Ñ¡•È¤ˆ¥¸A…¹(€€€€€€€€€€‰É½ÕÁ•‘A½ÍÍ¥‰±•¡…¹¹•±Ì¡Á½ÍÍ¥‰±•AÁØ¤ˆ¥¸A…¹(€€€€€€€€€€‰…Ñ•½ÉåQ¥•È¡‰lÁt¤µ…Ñ•½ÉåQ¥•È¡…lÁt¤ˆ¥¸A…¹(€€€€€€€€€€‰…Ñ•½ÉåQ¥•È¡…lÁt¤ôôôÀý‰•ÍÑ5…Ñ ¡‰lÅt¤µ‰•ÍÑ5…Ñ ¡…lÅt¤ˆ¥¸A…¹(€€€€€€€€€€‰5…Ñ ¹µ…à ¸¸¹‰lÅt¹µ…À¡¡…¹¹•±1½…±•AÉ¥½É¥Ñä¤¤ˆ¹½Ð¥¸A¤(€€€¡•¬ ‰™¥áÑÕÉ”…¹½µÁ•Ñ¥Ñ¥½¸É•±•Ù…¹”ÁÉ½µ½Ñ”Á½ÍÍ¥‰±”¡…¹¹•±Ìˆ°(€€€€€€€€€€‰™Õ¹Ñ¥½¸¡…¹¹•±5…Ñ¡AÉ¥½É¥Ñä¡ ¤ˆ¥¸A…¹(€€€€€€€€€€‰™¥áÑÕÉ•}µ…Ñ ôôô•á…Ðœˆ¥¸A…¹(€€€€€€€€€€‰±•…Õ•}µ…Ñ ôôõÑÉÕ”˜˜½Ù¥…Á±…ä½¤ˆ¥¸A…¹(€€€€€€€€€€‰™¥áÑÕÉ•}µ…Ñ ôôôÁ…ÉÑ¥…°œˆ¥¸A¤(€€€¥¹‘•á•‘}¡…¹¹•±Ì€ôl(€€€€€€€ì‰¹…µ”ˆè˜‰9½¥Í”¡…¹¹•°í¥ôˆ°€‰ÍÑÉ•…µ}¥ˆè€ÌÀÀÀ€¬¤°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰µ¥ÍŒ‰ô™½È¤¥¸É…¹” ÄÈÀ¥t€¬l(€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐ€Ä!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÐÀÀÄ°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰1%Yð]½±Ù•Ì€´5…¹¡•ÍÑ•È¥Ñäˆ°€‰ÍÑÉ•…µ}¥ˆè€ÐÀÀÈ°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰ÁÁØ‰õt(€€€¥¹‘•á•‘}…ÑÌ€ôì‰µ¥ÍŒˆè€‰Y%@=1ˆ°€‰¹¼ˆè€‰9<ðMA=IQLˆ°(€€€€€€€€€€€€€€€€€€€€‰ÁÁØˆè€‰AAXY9QL‰ô(€€€¥¹‘•á•‘}™¥áÑÕÉ”€ôì‰¡½µ”ˆè€‰]½±Ù•Ìˆ°€‰…Ý…äˆè€‰5…¹¡•ÍÑ•È¥Ñäˆ°(€€€€€€€€€€€€€€€€€€€€€€€‰‰å}½Õ¹ÑÉäˆèì‰9<ˆèl‰XMÁ½ÉÐ€Ä9½ÉÝ…ä‰uõô(€€€¥¹‘•á•‘}Í¡½ÉÑ±¥ÍÐ€ô}ÍÁ½ÉÑÍ}™¥áÑÕÉ•}¡…¹¹•±}Í¡½ÉÑ±¥ÍÐ (€€€€€€€¥¹‘•á•‘}™¥áÑÕÉ”°¥¹‘•á•‘}¡…¹¹•±Ì°¥¹‘•á•‘}…ÑÌ¤(€€€¡•¬ ‰ÍÁ½ÉÑÌ¥¹‘•àÍ¡½ÉÑ±¥ÍÑÌ‰É½…‘…ÍÑ•È…¹™¥áÑÕÉ”¡…¹¹•±Ìˆ°(€€€€€€€€€±•¸¡¥¹‘•á•‘}Í¡½ÉÑ±¥ÍÐ¤€ð±•¸¡¥¹‘•á•‘}¡…¹¹•±Ì¤…¹(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸¥¹‘•á•‘}Í¡½ÉÑ±¥ÍÑô€ôôìÐÀÀÄ°€ÐÀÀÉô¤(€€€±…ÍÌ}½µÁ•Ñ¥Ñ¥½¹Q•ÍÑ`è(€€€€€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€€€€€‘•˜ÍÑÉ•…µ}ÕÉ°¡ÍÑÉ•…µ}¥¤è(€€€€€€€€€€€É•ÑÕÉ¸˜‰ÍÑÉ•…´éíÍÑÉ•…µ}¥‘ôˆ(€€€½µÁ•Ñ¥Ñ¥½¹}¡…¹¹•±Ì€ôl(€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐAÉ•µ¥•È1•…Õ”€ÄY%@9<ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÐÀÄÀ°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰U,AÉ•µ¥•È1•…Õ”MÁ½ÉÐ€Äˆ°€‰ÍÑÉ•…µ}¥ˆè€ÐÀÄÄ°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰Õ¬‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐ€Äˆ°€‰ÍÑÉ•…µ}¥ˆè€ÐÀÄÈ°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐAÉ•µ¥•È1•…Õ”€ÈI\9<ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÐÀÄÌ°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰U,èA01Lˆ°€‰ÍÑÉ•…µ}¥ˆè€ÐÀÄÐ°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰Õ¬µ•Á°‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰U,èA09=QQ%9!4ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÐÀÄÔ°(€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰Õ¬µ•Á°‰õt(€€€½µÁ•Ñ¥Ñ¥½¹}™¥áÑÕÉ”€ôì‰¡½µ”ˆè€‰1••‘ÌU¹¥Ñ•ˆ°€‰…Ý…äˆè€‰9½ÑÑ¥¹¡…´½É•ÍÐˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‰±•…Õ•}¹…µ”ˆè€‰AÉ•µ¥•È1•…Õ”ˆ°€‰‰å}½Õ¹ÑÉäˆèíõô(€€€½µÁ•Ñ¥Ñ¥½¹}Í¡½ÉÑ±¥ÍÐ€ô}ÍÁ½ÉÑÍ}™¥áÑÕÉ•}¡…¹¹•±}Í¡½ÉÑ±¥ÍÐ (€€€€€€€½µÁ•Ñ¥Ñ¥½¹}™¥áÑÕÉ”°½µÁ•Ñ¥Ñ¥½¹}¡…¹¹•±Ì°(€€€€€€€ì‰¹¼ˆè€‰9<ðMA=IQLˆ°€‰Õ¬ˆè€‰U,ðMA=IQLˆ°(€€€€€€€€€‰Õ¬µ•Á°ˆè€‰U,ðA0AI5%H1UAAX‰ô¤(€€€½µÁ•Ñ¥Ñ¥½¹}É½ÝÌ€ô™¥¹‘}½µÁ•Ñ¥Ñ¥½¹}¡…¹¹•±Ì (€€€€€€€½µÁ•Ñ¥Ñ¥½¹}™¥áÑÕÉ”°½µÁ•Ñ¥Ñ¥½¹}Í¡½ÉÑ±¥ÍÐ°(€€€€€€€ì‰¹¼ˆè€‰9<ðMA=IQLˆ°€‰Õ¬ˆè€‰U,ðMA=IQLˆ°(€€€€€€€€€‰Õ¬µ•Á°ˆè€‰U,ðA0AI5%H1UAAX‰ô°}½µÁ•Ñ¥Ñ¥½¹Q•ÍÑ` ¤¤(€€€¡•¬ ‰AÉ•µ¥•È1•…Õ”™¥áÑÕÉ”…‘‘Ì¹…µ•½µÁ•Ñ¥Ñ¥½¸…±Ñ•É¹…Ñ¥Ù•Ìˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸½µÁ•Ñ¥Ñ¥½¹}É½ÝÍô€ôô(€€€€€€€€€ìÐÀÄÀ°€ÐÀÄÄ°€ÐÀÄÌ°€ÐÀÄÐ°€ÐÀÄÕô…¹(€€€€€€€€€…±°¡É½Ü¹•Ð ‰±•…Õ•}µ…Ñ ˆ¤™½ÈÉ½Ü¥¸½µÁ•Ñ¥Ñ¥½¹}É½ÝÌ¤¤(€€€½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ•}É½ÝÌ€ôì(€€€€€€€É½Ýl‰ÍÑÉ•…µ}¥‰tèÉ½Ü¹•Ð ‰½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ˆ¤(€€€€€€€™½ÈÉ½Ü¥¸½µÁ•Ñ¥Ñ¥½¹}É½ÝÍô(€€€¡•¬ ‰9½ÉÝ•¥…¸AÉ•µ¥•È1•…Õ”™…µ¥±ä¥ÌÍ•ÕÉ”Ý¥Ñ¡½ÕÐ‰É½…‘…ÍÑ•È‘…Ñ„ˆ°(€€€€€€€€€½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ•}É½ÝÌ¹•Ð ÐÀÄÀ¤¥ÌQÉÕ”…¹(€€€€€€€€€½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ•}É½ÝÌ¹•Ð ÐÀÄÌ¤¥ÌQÉÕ”¤(€€€¡•¬ ‰U,A0…Ñ•½ÉäÁ±ÕÌ•¥Ñ¡•È™¥áÑÕÉ”Ñ•…´¥ÌÍ•ÕÉ”ˆ°(€€€€€€€€€½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ•}É½ÝÌ¹•Ð ÐÀÄÐ¤¥ÌQÉÕ”…¹(€€€€€€€€€½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ•}É½ÝÌ¹•Ð ÐÀÄÔ¤¥ÌQÉÕ”…¹(€€€€€€€€€½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ•}É½ÝÌ¹•Ð ÐÀÄÄ¤¥Ì…±Í”¤(€€€¥¹Ñ•É…Ñ•‘}½µÁ•Ñ¥Ñ¥½¸€ô}µ…Ñ¡}ÍÁ½ÉÑÍ}™¥áÑÕÉ•}¡…¹¹•±Ì (€€€€€€€½µÁ•Ñ¥Ñ¥½¹}™¥áÑÕÉ”°ì‰µ…Ñ¡}Ñ¡É•Í¡½±ˆè€À¸ØÉô°½µÁ•Ñ¥Ñ¥½¹}¡…¹¹•±Ì°(€€€€€€€ì‰¹¼ˆè€‰9<ðMA=IQLˆ°€‰Õ¬ˆè€‰U,ðMA=IQLˆ°(€€€€€€€€€‰Õ¬µ•Á°ˆè€‰U,ðA0AI5%H1UAAX‰ô°}½µÁ•Ñ¥Ñ¥½¹Q•ÍÑ` ¤¤(€€€¥¹Ñ•É…Ñ•‘}Í•ÕÉ”€ôì(€€€€€€€É½Ýl‰ÍÑÉ•…µ}¥‰tèÉ½Ü¹•Ð ‰½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ˆ¤(€€€€€€€™½ÈÉ½Ü¥¸¥¹Ñ•É…Ñ•‘}½µÁ•Ñ¥Ñ¥½¹l‰ÁÁÙ}¡¥ÑÌ‰uô(€€€¡•¬ ‰¥¹Ñ•É…Ñ•µ…Ñ¡•ÈÁÉ•Í•ÉÙ•ÌÍ•ÕÉ”™±…Ì…™Ñ•ÈÑ•…´µ¡¥Ð‘•‘ÕÁ±¥…Ñ¥½¸ˆ°(€€€€€€€€€¥¹Ñ•É…Ñ•‘}Í•ÕÉ”¹•Ð ÐÀÄÀ¤¥ÌQÉÕ”…¹(€€€€€€€€€¥¹Ñ•É…Ñ•‘}Í•ÕÉ”¹•Ð ÐÀÄÌ¤¥ÌQÉÕ”…¹(€€€€€€€€€¥¹Ñ•É…Ñ•‘}Í•ÕÉ”¹•Ð ÐÀÄÐ¤¥ÌQÉÕ”…¹(€€€€€€€€€¥¹Ñ•É…Ñ•‘}Í•ÕÉ”¹•Ð ÐÀÄÔ¤¥ÌQÉÕ”¤(€€€•¹™½É•‘}Í•ÕÉ”€ô}•¹™½É•}™¥áÑÕÉ•}Í•ÕÉ•}µ…Ñ¡•Ì¡½µÁ•Ñ¥Ñ¥½¹}™¥áÑÕÉ”°ì(€€€€€€€€‰µ…Ñ¡•Ìˆèmt°€‰ÁÁÙ}¡¥ÑÌˆèl(€€€€€€€€€€€ì‰ÍÑÉ•…µ}¥ˆè€ÐÀÈÀ°(€€€€€€€€€€€€€‰áÑÉ•…µ}¹…µ”ˆè€‰9<èXMA=IPAI5%H1U€ÈY%@9<ˆ°(€€€€€€€€€€€€€‰…Ñ•½Éäˆè€‰9=ð9=I]d!½I\‰ô°(€€€€€€€€€€€ì‰ÍÑÉ•…µ}¥ˆè€ÐÀÈÄ°€‰áÑÉ•…µ}¹…µ”ˆè€‰U,èA01Lˆ°(€€€€€€€€€€€€€‰…Ñ•½Éäˆè€‰U-ðA0AI5%H1UAAX‰ô°(€€€€€€€€€€€ì‰ÍÑÉ•…µ}¥ˆè€ÐÀÈÈ°€‰áÑÉ•…µ}¹…µ”ˆè€‰U,èA09=QQ%9!4ˆ°(€€€€€€€€€€€€€‰…Ñ•½Éäˆè€‰U-ðA0AI5%H1UAAX‰õuô¤(€€€•¹™½É•‘}‰å}¥€ôíÉ½Ýl‰ÍÑÉ•…µ}¥‰tèÉ½Ü¹•Ð ‰Í•ÕÉ•}É•…Í½¸ˆ¤(€€€€€€€€€€€€€€€€€€€€€™½ÈÉ½Ü¥¸•¹™½É•‘}Í•ÕÉ•l‰ÁÁÙ}¡¥ÑÌ‰uô(€€€¡•¬ ‰™¥¹…°É•ÍÁ½¹Í”Á…ÍÌÍ•ÕÉ•Ì9<XMÁ½ÉÐA0…¹U,A0Ñ•…´¡…¹¹•±Ìˆ°(€€€€€€€€€•¹™½É•‘}‰å}¥€ôôì(€€€€€€€€€€€€€€ÐÀÈÀè€‰¹½ÉÝ…å}ÁÉ•µ¥•É}±•…Õ”ˆ°(€€€€€€€€€€€€€€ÐÀÈÄè€‰Õ­}•Á±}™¥áÑÕÉ•}Ñ•…´ˆ°(€€€€€€€€€€€€€€ÐÀÈÈè€‰Õ­}•Á±}™¥áÑÕÉ•}Ñ•…´‰ô¤(€€€Ý¥Ñ ½Á•¸¡}}™¥±•}|°€‰Èˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤…ÌÍ½ÕÉ•}™¥±”è(€€€€€€€Í½ÕÉ•}Ñ•áÐ€ôÍ½ÕÉ•}™¥±”¹É•… ¤(€€€¡•¬ ‰ÍÁ½ÉÑÌÉ•™É•Í A%ÌÁÉ•Í•ÉÙ”±•…Õ”‰•™½É”Í•ÕÉ”•¹™½É•µ•¹Ðˆ°(€€€€€€€€€Í½ÕÉ•}Ñ•áÐ¹½Õ¹Ð (€€€€€€€€€€€€€€±•…Õ•}¹…µ”€ôÍÑÈ¡™¥áÑÕÉ”¹•Ð ‰±•…Õ•}¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄØÁtœ¤€øô€Ä…¹(€€€€€€€€€Í½ÕÉ•}Ñ•áÐ¹½Õ¹Ð (€€€€€€€€€€€€€€±•…Õ•}¹…µ”€ôÍÑÈ¡É…Ý}™¥áÑÕÉ”¹•Ð ‰±•…Õ•}¹…µ”ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¥lèÄØÁtœ¤€øô€Ä…¹(€€€€€€€€€Í½ÕÉ•}Ñ•áÐ¹½Õ¹Ð œ‰±•…Õ•}¹…µ”ˆè±•…Õ•}¹…µ”œ¤€øô€Ð¤(€€€…¡•}É•±…ÍÍ¥™¥•€ô}•¹™½É•}™¥áÑÕÉ•}Í•ÕÉ•}µ…Ñ¡•Ì (€€€€€€€½µÁ•Ñ¥Ñ¥½¹}™¥áÑÕÉ”°}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}±¥•¹Ð¡ì(€€€€€€€€€€€€‰µ…Ñ¡•Ìˆèmt°€‰ÁÁÙ}¡¥ÑÌˆèmì(€€€€€€€€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè€ÐÀÈÌ°(€€€€€€€€€€€€€€€€‰áÑÉ•…µ}¹…µ”ˆè€‰9<èXMA=IPAI5%H1U€ÌI\9<ˆ°(€€€€€€€€€€€€€€€€‰…Ñ•½Éäˆè€‰9=ð9=I]d!½I\‰õuô°}½µÁ•Ñ¥Ñ¥½¹Q•ÍÑ` ¤¤¤(€€€¡•¬ ‰…¡•AÉ•µ¥•È1•…Õ”É½ÝÌ…É”É•±…ÍÍ¥™¥•Ý¡•¸É•…ˆ°(€€€€€€€€€…¡•}É•±…ÍÍ¥™¥•‘l‰ÁÁÙ}¡¥ÑÌ‰ulÁt¹•Ð ‰Í•ÕÉ•}É•…Í½¸ˆ¤€ôô(€€€€€€€€€€‰¹½ÉÝ…å}ÁÉ•µ¥•É}±•…Õ”ˆ¤(€€€¡•¬ ‰™¥áÑÕÉ”É•¹‘•É¥¹œ¥¹‘•Á•¹‘•¹Ñ±äÉ•½¹¥é•Ì9½ÉÝ•¥…¸XMÁ½ÉÐA0ˆ°(€€€€€€€€€€‰™Õ¹Ñ¥½¸ÕÍÑ½µAÉ•µ¥•É1•…Õ•M•ÕÉ”¡ ±˜¤ˆ¥¸A…¹(€€€€€€€€€A¹½Õ¹Ð ‰ÕÍÑ½µAÉ•µ¥•É1•…Õ•M•ÕÉ”¡´±˜¤ˆ¤€øô€È…¹(€€€€€€€€€€‰ÕÍÑ½µAÉ•µ¥•É1•…Õ•M•ÕÉ”¡ ±˜¤ˆ¥¸A¤(€€€¡•¬ ‰ÍÁ½ÉÑÌÍ•…É ­••ÁÌÁ…ÉÑ¥…°AAX¡¥ÑÌ¥¸Á½ÍÍ¥‰±”…Ñ•½É¥•Ìˆ°(€€€€€€€€€€‰½¹ÍÐÁ½ÍÍ¥‰±•AÁØõmtˆ¥¸A…¹(€€€€€€€€€€ˆ¡˜¹ÁÁÙ}¡¥ÑÍññmt¤¹™¥±Ñ•È¡´ôù™¥áÑÕÉ•¡…¹¹•±I…¹¬¡´±˜¤ôôôÌˆ¥¸A¤(€€€Õ¹É•±…Ñ•‘}Ñ•ÍÐ€ôm‘¥Ð¡Í¡•‘Õ±•}Ñ•ÍÑlÁt¥t(€€€}½Ù•É±…å}™¥áÑÕÉ•}É½ÝÌ¡Õ¹É•±…Ñ•‘}Ñ•ÍÐ°mì(€€€€€€€€‰¡½µ”ˆè€‰A½ÉÑ±…¹!•…ÉÑÌ½˜A¥¹”ˆ°€‰…Ý…äˆè€‰½ÉÝ…É5…‘¥Í½¸ˆ°(€€€€€€€€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÙPÄàèÌÀèÀÁhˆ°€‰‰å}½Õ¹ÑÉäˆèì‰ULˆèl‰MA8M•±•Ð‰uõõt¤(€€€¡•¬ ‰Ñ•…´Í¡•‘Õ±”½Ù•É±…ä…¹¹½Ð…ÁÁ•¹Á…ÉÑ¥…°µ¹…µ”™¥áÑÕÉ•Ìˆ°(€€€€€€€€€±•¸¡Õ¹É•±…Ñ•‘}Ñ•ÍÐ¤€ôô€Ä¤(€€€ÕÉÉ•¹Ñ}Ñ•ÍÐ€ô}ÕÉÉ•¹Ñ}…¹‘}ÕÁ½µ¥¹}™¥áÑÕÉ•Ì¡l(€€€€€€€ì‰¡½µ”ˆè€‰=±ˆ°€‰…Ý…äˆè€‰5…äˆ°€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´ÀÔ´ÀÅPÄÈèÀÀèÀÁh‰ô°(€€€€€€€ì‰¡½µ”ˆè€‰!•…ÉÑÌˆ°€‰…Ý…äˆè€‰	•¹™¥„ˆ°€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÍPÄàèÐÔèÀÁh‰õt°(€€€€€€€‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ” ÈÀÈØ°€à°€ÄÌ°€ÄÈ°Ñé¥¹™¼õ‘…Ñ•Ñ¥µ”¹Ñ¥µ•é½¹”¹ÕÑŒ¤¹Ñ¥µ•ÍÑ…µÀ ¤¤(€€€¡•¬ ‰¡¥ÍÑ½É¥…°Ñ•…´™¥áÑÕÉ•Ì•á±Õ‘•™É½´Í•…É ˆ°(€€€€€€€€€±•¸¡ÕÉÉ•¹Ñ}Ñ•ÍÐ¤€ôô€Ä…¹ÕÉÉ•¹Ñ}Ñ•ÍÑlÁul‰¡½µ”‰t€ôô€‰!•…ÉÑÌˆ¤(€€€¡•¬ ‰½Õ¹ÑÉäÁ¥­•ÈÕÍ•Ì±…‰•±•A½ÉÑÕ…°½‘”ˆ°(€€€€€€€€€€‰lÁÐœ°ŸÂ~×Â~äœ°A½ÉÑÕ…°tˆ¥¸A…¹(€€€€€€€€€€¥ô‰Í}ŒˆÑåÁ”ô‰¡¥‘‘•¸ˆœ¥¸A¤(€€€½É¥¥¹…±}½Õ¹ÑÉå}™•Ñ €ô±½‰…±Ì ¥l‰™•Ñ¡}½Õ¹ÑÉå}™¥áÑÕÉ•Ì‰t(€€€Õ¥‘•}±½¬€ôÑ¡É•…‘¥¹œ¹1½¬ ¤(€€€Õ¥‘•}…Ñ¥Ù”€ôlÁt(€€€Õ¥‘•}Á•…¬€ôlÁt(€€€‘•˜™…­•}½Õ¹ÑÉå}™•Ñ ¡½Õ¹ÑÉä¤è(€€€€€€€Ý¥Ñ Õ¥‘•}±½¬è(€€€€€€€€€€€Õ¥‘•}…Ñ¥Ù•lÁt€¬ô€Ä(€€€€€€€€€€€Õ¥‘•}Á•…­lÁt€ôµ…à¡Õ¥‘•}Á•…­lÁt°Õ¥‘•}…Ñ¥Ù•lÁt¤(€€€€€€€Ñ¥µ”¹Í±••À À¸ÀÄ¤(€€€€€€€Ý¥Ñ Õ¥‘•}±½¬è(€€€€€€€€€€€Õ¥‘•}…Ñ¥Ù•lÁt€´ô€Ä(€€€€€€€É•ÑÕÉ¸mì‰½Õ¹ÑÉäˆè½Õ¹ÑÉåõt(€€€ÑÉäè(€€€€€€€±½‰…±Ì ¥l‰™•Ñ¡}½Õ¹ÑÉå}™¥áÑÕÉ•Ì‰t€ô™…­•}½Õ¹ÑÉå}™•Ñ (€€€€€€€Õ¥‘•}É½ÝÌ°Õ¥‘•}•ÉÉ½ÉÌ€ô}™•Ñ¡}½Õ¹ÑÉå}Õ¥‘•Ì (€€€€€€€€€€€l‰¹¼ˆ°€‰ˆˆ°€‰ÕÌ‰t°µ…á}Ý½É­•ÉÌôÌ¤(€€€™¥¹…±±äè(€€€€€€€±½‰…±Ì ¥l‰™•Ñ¡}½Õ¹ÑÉå}™¥áÑÕÉ•Ì‰t€ô½É¥¥¹…±}½Õ¹ÑÉå}™•Ñ (€€€¡•¬ ‰™…±±‰…¬½Õ¹ÑÉäÕ¥‘•Ì±½…½¹ÕÉÉ•¹Ñ±ä¥¸ÍÑ…‰±”½É‘•Èˆ°(€€€€€€€€€¹½ÐÕ¥‘•}•ÉÉ½ÉÌ…¹Õ¥‘•}Á•…­lÁt€øô€È…¹(€€€€€€€€€m½Õ¹ÑÉä™½È½Õ¹ÑÉä°}É½ÝÌ¥¸Õ¥‘•}É½ÝÍt€ôôl‰¹¼ˆ°€‰ˆˆ°€‰ÕÌ‰t¤(€€€¡•¬ ‰½Õ¹ÑÉäÕ¥‘•Ì…Ù½¥½ÁÑ¥½¹…°‰Õ¹‘±•µ½‘Õ±•Ìˆ°(€€€€€€€€€€‰½¹ÕÉÉ•¹Ð¹™ÕÑÕÉ•Ìˆ¹½Ð¥¸ÍåÌ¹µ½‘Õ±•Ì¤(€€€ÁÉ½™¥±•}‰…­ÕÀ€ôÉ•…Ñ•}ÁÉ½™¥±•}‰…­ÕÀ ‰ÁÉ½™¥±”ˆ°ì‰™¥±Ñ•Èˆè€‰…±°‰ô¤(€€€¡•¬ ‰ÁÉ½™¥±”‰…­ÕÀ½µ¥ÑÌaÑÉ•…´É•‘•¹Ñ¥…±Ìˆ°(€€€€€€€€€}AI=%1}MIQ}-eL¹¥Í‘¥Í©½¥¹Ð¡ÁÉ½™¥±•}‰…­ÕÁl‰½¹™¥œ‰t¤¤(€€€¡•¬ ‰ÁÉ½™¥±”‰…­ÕÀÉ•Ñ…¥¹Ì™…Ù½É¥Ñ•Ìˆ°¥Í¥¹ÍÑ…¹”¡ÁÉ½™¥±•}‰…­ÕÁl‰™…Ù½É¥Ñ•Ì‰t°‘¥Ð¤¤(€€€™Õ±±}‰…­ÕÀ€ôÉ•…Ñ•}ÁÉ½™¥±•}‰…­ÕÀ ‰™Õ±°ˆ°ì‰™¥±Ñ•Èˆè€‰…±°‰ô¤(€€€¡•¬ ‰™Õ±°‰…­ÕÀ¥¹±Õ‘•ÌaÑÉ•…´É•‘•¹Ñ¥…°™¥•±‘Ìˆ°(€€€€€€€€€}AI=%1}MIQ}-eL¹¥ÍÍÕ‰Í•Ð¡™Õ±±}‰…­ÕÁl‰½¹™¥œ‰t¤¤(€€€µ•É•‘}Ñ•ÍÐ€ô}µ•É•}™…Ù½É¥Ñ•}±¥ÍÑÌ (€€€€€€€€‰Ñ•…µÌˆ°mì‰Ñ•…µ}¥ˆè€ˆÄˆ°€‰¹…µ”ˆè€‰=±‰õt°(€€€€€€€mì‰Ñ•…µ}¥ˆè€ˆÄˆ°€‰¹…µ”ˆè€‰UÁ‘…Ñ•‰ô°ì‰Ñ•…µ}¥ˆè€ˆÈˆ°€‰¹…µ”ˆè€‰9•Ü‰õt¤(€€€¡•¬ ‰‰…­ÕÀ™…Ù½É¥Ñ•Ìµ•É”…¹‘•‘ÕÁ±¥…Ñ”ˆ°(€€€€€€€€€±•¸¡µ•É•‘}Ñ•ÍÐ¤€ôô€È…¹µ•É•‘}Ñ•ÍÑlÁul‰¹…µ”‰t€ôô€‰UÁ‘…Ñ•ˆ¤(€€€ÕÉÉ•¹Ñ}Ñ•ÍÑ}™œ€ô‘¥Ð¡U1Q}=9%°ÁÉ½™¥±•}¹…µ”ô‰ÕÉÉ•¹Ðˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€áÑÉ•…µ}¡½ÍÐô‰½±¹•á…µÁ±”ˆ¤(€€€¥¹½µ¥¹}Ñ•ÍÑ}™œ€ô‘¥Ð¡U1Q}=9%°ÁÉ½™¥±•}¹…µ”ô‰%µÁ½ÉÑ•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€áÑÉ•…µ}¡½ÍÐô‰¹•Ü¹•á…µÁ±”ˆ¤(€€€ÕÉÉ•¹Ñ}Ñ•ÍÑ}™…Ø€ôí­•äèmt™½È­•ä¥¸}Y=I%Q}1%MQ}-eMô(€€€¥¹½µ¥¹}Ñ•ÍÑ}™…Ø€ôí­•äèmt™½È­•ä¥¸}Y=I%Q}1%MQ}-eMô(€€€ÕÉÉ•¹Ñ}Ñ•ÍÑ}™…Ùl‰¡…¹¹•±Ì‰t€ômì‰ÍÑÉ•…µ}¥ˆè€Ü°€‰¹…µ”ˆè€‰=±¡…¹¹•°‰õt(€€€¥¹½µ¥¹}Ñ•ÍÑ}™…Ùl‰¡…¹¹•±Ì‰t€ômì‰ÍÑÉ•…µ}¥ˆè€à°€‰¹…µ”ˆè€‰9•Ü¡…¹¹•°‰õt(€€€É•ÍÑ½É•‘}™}Ñ•ÍÐ°É•ÍÑ½É•‘}™…Ù}Ñ•ÍÐ€ô}ÁÉ•Á…É•}‰…­ÕÁ}É•ÍÑ½É” (€€€€€€€€‰™Õ±°ˆ°¥¹½µ¥¹}Ñ•ÍÑ}™œ°¥¹½µ¥¹}Ñ•ÍÑ}™…Ø°(€€€€€€€ÕÉÉ•¹Ñ}Ñ•ÍÑ}™œ°ÕÉÉ•¹Ñ}Ñ•ÍÑ}™…Ø¤(€€€¡•¬ ‰™Õ±°‰…­ÕÀÉ•Á±…•ÌÁÉ½Ù¥‘•Èµ‰½Õ¹™…Ù½É¥Ñ•Ìˆ°(€€€€€€€€€É•ÍÑ½É•‘}™…Ù}Ñ•ÍÑl‰¡…¹¹•±Ì‰t€ôô¥¹½µ¥¹}Ñ•ÍÑ}™…Ùl‰¡…¹¹•±Ì‰t¤(€€€¡•¬ ‰™Õ±°‰…­ÕÀÉ•Á±…•Ì½¹™¥ÕÉ…Ñ¥½¸ˆ°(€€€€€€€€€É•ÍÑ½É•‘}™}Ñ•ÍÑl‰ÁÉ½™¥±•}¹…µ”‰t€ôô€‰%µÁ½ÉÑ•ˆ…¹(€€€€€€€€€É•ÍÑ½É•‘}™}Ñ•ÍÑl‰áÑÉ•…µ}¡½ÍÐ‰t€ôô€‰¹•Ü¹•á…µÁ±”ˆ¤(€€€ÑÉäè(€€€€€€€}Ù…±¥‘…Ñ•‘}‰…­ÕÁ}Á…å±½…¡ì‰™½Éµ…Ðˆè€‰½±½ÌµÑÙµ…Ñ”µ‰…­ÕÀˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰™½Éµ…Ñ}Ù•ÉÍ¥½¸ˆè€Ä¸ä°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰‰…­ÕÁ}ÑåÁ”ˆè€‰™Õ±°ˆ°€‰½¹™¥œˆèíô°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰™…Ù½É¥Ñ•Ìˆèíõô¤(€€€€€€€¥¹Ù…±¥‘}‰…­ÕÁ}É•©•Ñ•€ô…±Í”(€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€¥¹Ù…±¥‘}‰…­ÕÁ}É•©•Ñ•€ôQÉÕ”(€€€¡•¬ ‰¹½¸µ¥¹Ñ••È‰…­ÕÀÙ•ÉÍ¥½¸É•©•Ñ•ˆ°¥¹Ù…±¥‘}‰…­ÕÁ}É•©•Ñ•¤(€€€¹½Ü€ô‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ” ÈÀÈØ°€à°€ÄÄ°Ñé¥¹™¼õ‘…Ñ•Ñ¥µ”¹Ñ¥µ•é½¹”¹ÕÑŒ¤(€€€¡•¬ ‰É•±•…Í•µ½Ù¥”¥¹±Õ‘•ˆ°}¥¹•µ•Ñ…}É•±•…Í•‘}µ½Ù¥” (€€€€€€€ì‰É•±•…Í•ˆè€ˆÈÀÈØ´Àà´ÄÁPÀÀèÀÀèÀÀ¸ÀÀÁh‰ô°¹½Ü¤¤(€€€¡•¬ ‰™ÕÑÕÉ”µ½Ù¥”•á±Õ‘•ˆ°¹½Ð}¥¹•µ•Ñ…}É•±•…Í•‘}µ½Ù¥” (€€€€€€€ì‰É•±•…Í•ˆè€ˆÈÀÈØ´Àà´ÄÉPÀÀèÀÀèÀÀ¸ÀÀÁh‰ô°¹½Ü¤¤(€€€¡•¬ ‰Õ¹‘…Ñ•ÕÉÉ•¹Ðµå•…Èµ½Ù¥”•á±Õ‘•ˆ°¹½Ð}¥¹•µ•Ñ…}É•±•…Í•‘}µ½Ù¥” (€€€€€€€ì‰É•±•…Í•%¹™¼ˆè€ˆÈÀÈØ‰ô°¹½Ü¤¤(€€€¡•¬ ‰½±‘•Èµ½Ù¥”¥¹±Õ‘•ˆ°}¥¹•µ•Ñ…}É•±•…Í•‘}µ½Ù¥” (€€€€€€€ì‰É•±•…Í•%¹™¼ˆè€ˆÈÀÈÔ‰ô°¹½Ü¤¤(€€€Í…µÁ±•}¡…¹¹•±Ì€ôl(€€€€€€€ì‰¹…µ”ˆè€‰9<èQX€ÈMÁ½ÉÐ€Äˆ°€‰ÍÑÉ•…µ}¥ˆè€Ä°€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰1%YðA=11=81%5MM=0€´	I98ðYQXAAX€Ìˆ°(€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè€È°€‰…Ñ•½Éå}¥ˆè€‰ÁÁØ‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰1%Yð	I98€´!5-4ðYQXAAX€Ôˆ°(€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè€Ì°€‰…Ñ•½Éå}¥ˆè€‰ÁÁØ‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰	I98€Èˆ°€‰ÍÑÉ•…µ}¥ˆè€Ð°€‰…Ñ•½Éå}¥ˆè€‰ÁÁØ‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰9<èQX€ÈA1dðAAX€Äˆ°€‰ÍÑÉ•…µ}¥ˆè€Ô°€‰…Ñ•½Éå}¥ˆè€‰ÁÁØ‰ô°(€€€€€€€ì‰¹…µ”ˆè€‰M­äMÁ½ÉÑÌ€ÈU!ˆ°€‰ÍÑÉ•…µ}¥ˆè€Ø°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰ô°(€€€t(€€€Í…µÁ±•}…ÑÌ€ôì‰¹¼ˆè€‰9=ð9=I]dˆ°€‰ÁÁØˆè€‰9=ðAAXY9QLˆ°(€€€€€€€€€€€€€€€€€€€ˆÑ¬ˆè€ˆÑ,ðU!!991L‰ô(€€€Á±…Ñ™½Éµ}¥‘Ì€ôíÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸µ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰QX€ÈA±…ä€¡9<¤‰uô°Í…µÁ±•}¡…¹¹•±Ì°Í…µÁ±•}…ÑÌ°€À¸Ðä¥ô(€€€¡•¬ ‰ÍÑÉ•…µ¥¹œÁ±…Ñ™½É´…¹‘¥‘…Ñ•ÌÉ•Ñ…¥¹•ˆ°Á±…Ñ™½Éµ}¥‘Ì€ôôìÕô¤(€€€ÁÉ½Ù¥‘•É}É½ÝÌ€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰QX€ÈMÁ½ÉÐ€Äˆ°€‰QX€ÈA±…ä€¡9<¤‰uô°(€€€€€€€Í…µÁ±•}¡…¹¹•±Ì°Í…µÁ±•}…ÑÌ°€À¸Ðä¤(€€€ÁÉ½Ù¥‘•É}•á…Ð€ôíÉ½Ýl‰ÍÑÉ•…µ}¥‰tèÉ½Ü¹•Ð ‰ÁÉ½Ù¥‘•É}•á…Ðˆ¤™½ÈÉ½Ü¥¸ÁÉ½Ù¥‘•É}É½ÝÍô(€€€¡•¬ ‰•á…Ð±¥¹•…ÈÁÉ½Ù¥‘•ÈÁÉ½µ½Ñ•ˆ°ÁÉ½Ù¥‘•É}•á…Ð¹•Ð Ä¤¥ÌQÉÕ”¤(€€€¡•¬ ‰ÍÑÉ•…µ¥¹œÁÉ½Ù¥‘•È¹½ÐÁÉ½µ½Ñ•ˆ°ÁÉ½Ù¥‘•É}•á…Ð¹•Ð Ô¤¥Ì…±Í”¤(€€€Õ­|Ñ¬€ôµ…Ñ¡}¡…¹¹•±Ì¡ì‰U,ˆèl‰M­äMÁ½ÉÑÌ€È‰uô°(€€€€€€€€€€€€€€€€€€€€€€€€€€Í…µÁ±•}¡…¹¹•±Ì°Í…µÁ±•}…ÑÌ°€À¸Ðä¤(€€€¡•¬ ‰½Õ¹ÑÉå±•ÍÌ€Ñ¬ÁÉ½Ù¥‘•ÈÁÉ½µ½Ñ•ˆ°(€€€€€€€€€±•¸¡Õ­|Ñ¬¤€ôô€Ä…¹Õ­|Ñ­lÁt¹•Ð ‰ÁÉ½Ù¥‘•É}•á…Ðˆ¤¥ÌQÉÕ”¤(€€€¡½¹}­½¹|Ñ¬€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰U,ˆèl‰AÉ•µ¥•ÈMÁ½ÉÑÌ€È‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰!½¹­½¹œ9=\AÉ•µ¥•ÈMÁ½ÉÑÌ€È€Ñ,ˆ°€‰ÍÑÉ•…µ}¥ˆè€äà°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰õt°Í…µÁ±•}…ÑÌ°€À¸Ðä¤(€€€¡•¬ ‰ÝÉ¥ÑÑ•¸™½É•¥¸½Õ¹ÑÉäÉ•©•Ñ•¥¹Í¥‘”±½‰…°€Ñ¬…Ñ•½Éäˆ°(€€€€€€€€€¡½¹}­½¹|Ñ¬€ôômt¤(€€€Õ¹­¹½Ý¹|Ñ¬€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰U,ˆèl‰AÉ•µ¥•ÈMÁ½ÉÑÌ€È‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰AÉ•µ¥•ÈMÁ½ÉÑÌ€È€Ñ,ˆ°€‰ÍÑÉ•…µ}¥ˆè€äÜ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰õt°Í…µÁ±•}…ÑÌ°€À¸Ðä¤(€€€¡•¬ ‰±½‰…°€Ñ¬…Ñ•½ÉäÉ•µ…¥¹Ì•±¥¥‰±”ˆ°±•¸¡Õ¹­¹½Ý¹|Ñ¬¤€ôô€Ä¤(€€€…É¥‰‰•…¹}…ÉÑ½½¸€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰ULˆèl‰UM9•ÑÝ½É¬‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰5@èIQ==89Q]=I,ˆ°€‰ÍÑÉ•…µ}¥ˆè€äØ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰È‰õt°(€€€€€€€ì‰Èˆè€‰Hè…ÉÉ¥‰•…¸…µÀ‰ô°€À¸ØÈ¤(€€€¡•¬ ‰H…Ñ•½ÉäÉ•©•Ñ•™½ÈUL™½½Ñ‰…±°‰É½…‘…ÍÑ•Èˆ°(€€€€€€€€€…É¥‰‰•…¹}…ÉÑ½½¸€ôômt¤(€€€±½‰…±}…ÉÑ½½¸€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰ULˆèl‰UM9•ÑÝ½É¬‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰IQ==89Q]=I,ˆ°€‰ÍÑÉ•…µ}¥ˆè€äÔ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰õt°Í…µÁ±•}…ÑÌ°€À¸ÐÀ¤(€€€¡•¬ ‰…ÉÑ½½¸9•ÑÝ½É¬•á±Õ‘•É•…É‘±•ÍÌ½˜…Ñ•½Éäˆ°(€€€€€€€€€±½‰…±}…ÉÑ½½¸€ôômt¤(€€€¹½¹}™½½Ñ‰…±°€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰ULˆèl‰UM9•ÑÝ½É¬‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰ULè519•ÑÝ½É­Ìˆ°€‰ÍÑÉ•…µ}¥ˆè€ää°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰ÕÌµÍÁ½ÉÑÌ‰õt°(€€€€€€€ì‰ÕÌµÍÁ½ÉÑÌˆè€‰ULðMA=IQL‰ô°€À¸ÐÀ¤(€€€¡•¬ ‰½Ñ¡•ÈµÍÁ½ÉÐ¹•ÑÝ½É­Ì•á±Õ‘•™É½´™½½Ñ‰…±°ˆ°¹½¹}™½½Ñ‰…±°€ôômt¤(€€€•ÍÁ¹}Á…­…”€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰ULˆèl‰MA8M•±•Ðˆ°€‰MA8U¹±¥µ¥Ñ•‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰ULèMA8U¹±¥µ¥Ñ•€ÌÐ!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀÄ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰ÕÌµÍÁ½ÉÑÌ‰ô°(€€€€€€€€ì‰¹…µ”ˆè€ˆÈÐ¼Üè)UMQ%1UU91%5%Qˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀÈ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€ˆÈÐ´Ü‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰AI%5èIHM1Pˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀÌ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰ÁÉ¥µ”‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰ULèMA89]L!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀÐ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰ÕÌµÍÁ½ÉÑÌ‰õt°(€€€€€€€ì‰ÕÌµÍÁ½ÉÑÌˆè€‰ULðMA=IQLˆ°€ˆÈÐ´Üˆè€ˆÈÐ¼Üˆ°€‰ÁÉ¥µ”ˆè€‰AI%5‰ô°(€€€€€€€€À¸ÐÀ¤(€€€¡•¬ ‰¹Õµ‰•É•MA8Á…­…”™••É•Ñ…¥¹•ˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸•ÍÁ¹}Á…­…•ô€ôôìÄÀÅô¤(€€€Ù¥…Á±…å}¹¼€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰Y¥…Á±…ä9½ÉÝ…ä‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐ€Ä!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀä°€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐ1¥Ù”€Ðˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄÀ°€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐAÉ•µ¥•È1•…Õ”€Ìˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄÄ°€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐ½±˜ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄÈ°€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰M]èXMÁ½ÉÐ1¥Ù”€Èˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄÌ°€‰…Ñ•½Éå}¥ˆè€‰ÍÝ”‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰XMÁ½ÉÐ€Ñ,ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄÐ°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰XMÁ½ÉÐU±ÑÉ„!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄÔ°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰M]èXMÁ½ÉÐU±ÑÉ„!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄØ°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰õt°(€€€€€€€ì‰¹¼ˆè€‰9<ðMA=IQLˆ°€‰ÍÝ”ˆè€‰M]ðMA=IQLˆ°(€€€€€€€€€ˆÑ¬ˆè€ˆÑ,ðU!!991L‰ô°€À¸ØÈ¤(€€€¡•¬ ‰Y¥…Á±…ä9½ÉÝ…ä•áÁ…¹‘ÌÑ¼9½ÉÝ•¥…¸XMÁ½ÉÐ•Ù•¹Ð™••‘Ìˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸Ù¥…Á±…å}¹½ô€ôôìÄÀä°€ÄÄÀ°€ÄÄÄ°€ÄÄÐ°€ÄÄÔ°€ÄÄÙô…¹(€€€€€€€€€…±°¡¹½ÐÉ½Ü¹•Ð ‰ÁÉ½Ù¥‘•É}•á…Ðˆ¤™½ÈÉ½Ü¥¸Ù¥…Á±…å}¹¼¤¤(€€€ÁÉ•µ¥•É}±•…Õ•}Í•ÕÉ”€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰Y¥…Á±…ä9½ÉÝ…ä‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐAÉ•µ¥•È1•…Õ”€ÄY%@9<ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀäÄ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐ€Ä!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀäÈ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰M]èXMÁ½ÉÐAÉ•µ¥•È1•…Õ”€Ä!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀäÌ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰ÍÝ”‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐA0€È!Yˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀäÔ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐA0€ÌI\ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀäØ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐAÉ•´1•…Õ”€Ðˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀäÜ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰õt°(€€€€€€€ì‰¹¼ˆè€‰9<ðMA=IQLˆ°€‰ÍÝ”ˆè€‰M]ðMA=IQL‰ô°€À¸ØÈ°(€€€€€€€€‰AÉ•µ¥•È1•…Õ”ˆ¤(€€€Í•ÕÉ•}‰å}¥€ôíÉ½Ýl‰ÍÑÉ•…µ}¥‰tèÉ½Ü¹•Ð ‰½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ˆ¤(€€€€€€€€€€€€€€€€€€€™½ÈÉ½Ü¥¸ÁÉ•µ¥•É}±•…Õ•}Í•ÕÉ•ô(€€€¡•¬ ‰½¹±ä9½ÉÝ•¥…¸XMÁ½ÉÐAÉ•µ¥•È1•…Õ”™…µ¥±ä•ÑÌÕÍÑ½´Í•ÕÉ”™±…œˆ°(€€€€€€€€€Í•ÕÉ•}‰å}¥€ôôìÄÀäÄèQÉÕ”°€ÄÀäÈè…±Í”°€ÄÀäÔèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÄÀäØèQÉÕ”°€ÄÀäÜèQÉÕ•ô¤(€€€¹½¹}ÁÉ•µ¥•É}Í•ÕÉ”€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰Y¥…Á±…ä9½ÉÝ…ä‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐAÉ•µ¥•È1•…Õ”€Ä!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀäÐ°(€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰¹¼‰õt°ì‰¹¼ˆè€‰9<ðMA=IQL‰ô°€À¸ØÈ°(€€€€€€€€‰ÕÀˆ¤(€€€¡•¬ ‰ÕÍÑ½´XMÁ½ÉÐÍ•ÕÉ”ÉÕ±”¹•Ù•È…ÁÁ±¥•Ì½ÕÑÍ¥‘”AÉ•µ¥•È1•…Õ”ˆ°(€€€€€€€€€±•¸¡¹½¹}ÁÉ•µ¥•É}Í•ÕÉ”¤€ôô€Ä…¹(€€€€€€€€€¹½¹}ÁÉ•µ¥•É}Í•ÕÉ•lÁt¹•Ð ‰½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ˆ¤¥Ì…±Í”¤(€€€¡•¬ ‰ÕÍÑ½´½µÁ•Ñ¥Ñ¥½¸Í•ÕÉ”™±…œÉ•…¡•Ì‰½Ñ ™¥áÑÕÉ”É•¹‘•ÈÁ…Ñ¡Ìˆ°(€€€€€€€€€A¹½Õ¹Ð ‰½µÁ•Ñ¥Ñ¥½¹}Í•ÕÉ”ôôõÑÉÕ”ˆ¤€øô€Ì¤(€€€Ù¥…Á±…å}¹½É‘¥|Ñ¬€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰Mˆèl‰Y¥…Á±…äMÝ•‘•¸‰t°€‰,ˆèl‰Y¥…Á±…ä•¹µ…É¬‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰XMÁ½ÉÐU±ÑÉ„!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄÜ°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰M]èXMÁ½ÉÐ1¥Ù”€Èˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄà°€‰…Ñ•½Éå}¥ˆè€‰ÍÝ”‰õt°(€€€€€€€ìˆÑ¬ˆè€ˆÑ,ðU!!991Lˆ°€‰ÍÝ”ˆè€‰M]ðMA=IQL‰ô°€À¸ØÈ¤(€€€¡•¬ ‰µÕ±Ñ¥±¥¹Õ…°XMÁ½ÉÐ€Ñ,µ…ÁÌÑ¼MÝ•‘¥Í …¹…¹¥Í Y¥…Á±…äˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸Ù¥…Á±…å}¹½É‘¥|Ñ­ô€ôôìÄÄÝô¤(€€€Ù¥…Á±…å}™¤€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰$ˆèl‰Y¥…Á±…ä¥¹±…¹‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰$èXMÁ½ÉÐ€ÄMÕ½µ¤ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÄä°€‰…Ñ•½Éå}¥ˆè€‰™¤‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰$èXMÁ½ÉÐ¬MÕ½µ¤ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈÀ°€‰…Ñ•½Éå}¥ˆè€‰™¤‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰$èXMÁ½ÉÐ½½Ñ‰…±°ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈÄ°€‰…Ñ•½Éå}¥ˆè€‰™¤‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰$èXMÁ½ÉÐ1¥Ù”ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈÈ°€‰…Ñ•½Éå}¥ˆè€‰™¤‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰$èXMÁ½ÉÐ½±˜ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈÌ°€‰…Ñ•½Éå}¥ˆè€‰™¤‰ô°(€€€€€€€€ì‰¹…µ”ˆè€ˆÑ,èXMÁ½ÉÐˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈÐ°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰ô°(€€€€€€€€ì‰¹…µ”ˆè€ˆÑ,èXMÁ½ÉÐ¬ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈÔ°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰õt°(€€€€€€€ì‰™¤ˆè€‰$ðMA=IQLˆ°€ˆÑ¬ˆè€ˆÑ,ðU!!991L‰ô°€À¸ØÈ¤(€€€¡•¬ ‰Y¥…Á±…ä¥¹±…¹•áÁ…¹‘ÌÑ¼¥¹¹¥Í …¹Í¡…É•€Ñ,XMÁ½ÉÐ™••‘Ìˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸Ù¥…Á±…å}™¥ô€ôôìÄÄä°€ÄÈÀ°€ÄÈÄ°€ÄÈÈ°€ÄÈÐ°€ÄÈÕô¤(€€€Á±…¥¹}Ù¥…Á±…ä€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰9<ˆèl‰Y¥…Á±…ä‰t°€‰$ˆèl‰Y¥…Á±…ä‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9<èXMÁ½ÉÐ1¥Ù”€Ìˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈØ°€‰…Ñ•½Éå}¥ˆè€‰¹¼‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰$èXMÁ½ÉÐ¬MÕ½µ¤ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈÜ°€‰…Ñ•½Éå}¥ˆè€‰™¤‰ô°(€€€€€€€€ì‰¹…µ”ˆè€ˆÑ,èXMÁ½ÉÐ¬ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÈà°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰õt°(€€€€€€€ì‰¹¼ˆè€‰9<ðMA=IQLˆ°€‰™¤ˆè€‰$ðMA=IQLˆ°(€€€€€€€€€ˆÑ¬ˆè€ˆÑ,ðU!!991L‰ô°€À¸ØÈ¤(€€€¡•¬ ‰½Õ¹ÑÉäµÍ½Á•Á±…¥¸Y¥…Á±…ä•áÁ…¹‘ÌÑ¼9½É‘¥ŒXMÁ½ÉÐ™••‘Ìˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸Á±…¥¹}Ù¥…Á±…åô€ôôìÄÈØ°€ÄÈÜ°€ÄÈáô¤(€€€¡•¬ ‰¹•…É‰ä1QX‘…Ñ•Ì¥¹±Õ‘”Ñ¡¥É™¥áÑÕÉ”…™Ñ•È™É¥•¹‘±ä…¹ÕÀˆ°(€€€€€€€€€}¹•…É‰å}±ÑÙ}‘…Ñ•Ì¡l(€€€€€€€€€€€€€ì‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÕPÄÈèÀÀèÀÁh‰ô°(€€€€€€€€€€€€€ì‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄáPÄäèÀÀèÀÁh‰ô°(€€€€€€€€€€€€€ì‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÈÉPÄÐèÀÀèÀÁh‰ô°(€€€€€€€€€€€€€ì‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àä´ÀÕPÄÐèÀÀèÀÁh‰õt°€ˆÈÀÈØ´Àà´ÄÐˆ¤€ôô(€€€€€€€€€lˆÈÀÈØ´Àà´ÄÔˆ°€ˆÈÀÈØ´Àà´Äàˆ°€ˆÈÀÈØ´Àà´ÈÈ‰t¤(€€€¡•¬ ‰‘•¹Í”Í¡•‘Õ±•ÌÉ•Ñ…¥¸Ñ¡”•¥¡Ñ ¹•…É‰ä1QXÕ¥‘”‘…Ñ”ˆ°(€€€€€€€€€}¹•…É‰å}±ÑÙ}‘…Ñ•Ì¡l(€€€€€€€€€€€€€ì‰ÍÑ…ÉÐˆè˜ˆÈÀÈØ´Ààµí‘…äèÀÉ‘õPÄÐèÀÀèÀÁh‰ô(€€€€€€€€€€€€€™½È‘…ä¥¸É…¹” ÄÔ°€ÈÌ¥t°€ˆÈÀÈØ´Àà´ÄÐˆ¤€ôô(€€€€€€€€€m˜ˆÈÀÈØ´Ààµí‘…äèÀÉ‘ôˆ™½È‘…ä¥¸É…¹” ÄÔ°€ÈÌ¥t¤(€€€ÍÁ½ÉÑ}ÑÙ}½Õ¹ÑÉå}É½ÝÌ€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰APˆèl‰MÁ½ÉÐQX€Ô‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰A=HèMÁ½ÉÐQX€Ôˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀÔ°€‰…Ñ•½Éå}¥ˆè€‰Á½È‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰M]èMÁ½ÉÐQX€Ôˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀØ°€‰…Ñ•½Éå}¥ˆè€‰ÍÝ”‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰Y%@èMÁ½ÉÐQX€Ôˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀÜ°€‰…Ñ•½Éå}¥ˆè€‰Ù¥À‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰Y<èMÁ½ÉÐQX€Ôˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀà°€‰…Ñ•½Éå}¥ˆè€‰Ù¼‰õt°(€€€€€€€ì‰Á½Èˆè€‰A=HðMA=IQLˆ°€‰ÍÝ”ˆè€‰M]ðMA=IQLˆ°(€€€€€€€€€‰Ù¥Àˆè€‰Y%@½±ˆ°€‰Ù¼ˆè€‰Y<èMA=IQL‰ô°€À¸ÐÀ¤(€€€¡•¬ ‰Ñ¡É•”µ±•ÑÑ•È™½É•¥¸½Õ¹ÑÉäÁÉ•™¥á•Ì…É”É•©•Ñ•ˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸ÍÁ½ÉÑ}ÑÙ}½Õ¹ÑÉå}É½ÝÍô€ôôìÄÀÔ°€ÄÀÜ°€ÄÀáô¤(€€€¡•¬ ‰½Õ¹ÑÉä…±¥…Í•Ì…¹½¹¥…±¥é”Ý¥Ñ¡½ÕÐÑÉ•…Ñ¥¹œÑ¥•ÉÌ…Ì½Õ¹ÑÉ¥•Ìˆ°(€€€€€€€€€}}™É½µ}ÁÉ•™¥à ‰8ðMÁ½ÉÐˆ¤€ôô€‰‘¬ˆ…¹(€€€€€€€€€}}™É½µ}ÁÉ•™¥à ‰9èMÁ½ÉÐˆ¤€ôô€‰¹°ˆ…¹(€€€€€€€€€}}™É½µ}ÁÉ•™¥à ‰Y%@èMÁ½ÉÐˆ¤¥Ì9½¹”…¹(€€€€€€€€€}}™É½µ}ÁÉ•™¥à ‰Y<èMÁ½ÉÐˆ¤¥Ì9½¹”¤(€€€±…ÍÌ}Q•ÍÑaÑÉ•…´è(€€€€€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€€€€€‘•˜ÍÑÉ•…µ}ÕÉ°¡ÍÑÉ•…µ}¥¤è(€€€€€€€€€€€É•ÑÕÉ¸€‰Ñ•ÍÐèˆ€¬ÍÑÈ¡ÍÑÉ•…µ}¥¤(€€€ÍÁ½ÉÑÍ}Í¡…É•€ô}µ…Ñ¡}ÍÁ½ÉÑÍ}™¥áÑÕÉ•}¡…¹¹•±Ì (€€€€€€€ì‰¡½µ”ˆè€‰	É…¹¸ˆ°€‰…Ý…äˆè€‰!…µ-…´ˆ°€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÉPÈÀèÀÀèÀÁhˆ°(€€€€€€€€€‰‰å}½Õ¹ÑÉäˆèì‰9<ˆèl‰QX€ÈMÁ½ÉÐ€Ä‰uõô°(€€€€€€€ì‰µ…Ñ¡}Ñ¡É•Í¡½±ˆè€À¸Ðåô°Í…µÁ±•}¡…¹¹•±Ì°Í…µÁ±•}…ÑÌ°}Q•ÍÑaÑÉ•…´ ¤¤(€€€¡•¬ ‰ÍÁ½ÉÑÌ‰Õ±¬µ…Ñ¡•ÈÉ•ÕÍ•ÌÍ¡…É•…Ñ…±½Õ”ˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸ÍÁ½ÉÑÍ}Í¡…É•‘l‰µ…Ñ¡•Ì‰uô€ôôìÅô…¹(€€€€€€€€€€Ì¥¸íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸ÍÁ½ÉÑÍ}Í¡…É•‘l‰ÁÁÙ}¡¥ÑÌ‰uô¤(€€€¡•…ÉÑÍ}…¹‘¥‘…Ñ•Ì€ô™¥¹‘}Ñ•…µ}¡…¹¹•±Ì (€€€€€€€l‰!•…ÉÑÌˆ°€‰	•¹™¥„‰t°(€€€€€€€mì‰¹…µ”ˆè€‰!•…ÉÑÌQXˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÀÄ°€‰…Ñ•½Éå}¥ˆè€‰Ù¥À‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰!•…ÉÑÌÙÌI…¹•ÉÌˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÀÈ°€‰…Ñ•½Éå}¥ˆè€‰Ù¥À‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰MÕ¹¹·áÉ”1¥Ù”ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÀÌ°€‰…Ñ•½Éå}¥ˆè€‰Ù¥À‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰!½ÉÍ”I…¥¹œ!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÀÐ°€‰…Ñ•½Éå}¥ˆè€‰Ù¥À‰õt°(€€€€€€€ì‰Ù¥Àˆè€‰Y%@½±‰ô°}Q•ÍÑaÑÉ•…´ ¤¤(€€€¡•¬ ‰½¹”µÑ•…´¡…¹¹•±ÌÉ•µ…¥¸Á½ÍÍ¥‰±”Ý¥Ñ¡½ÕÐ…Ñ•½Éäµ½¹±ä¹½¥Í”ˆ°(€€€€€€€€€íÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸¡•…ÉÑÍ}…¹‘¥‘…Ñ•Íô€ôôìÈÀÄ°€ÈÀÉô¤(€€€±¥Ù•}¹½¥Í”€ôµ…Ñ¡}¡…¹¹•±Ì (€€€€€€€ì‰1QXˆèl‰1¥Ù”‰uô°(€€€€€€€mì‰¹…µ”ˆè€‰9½ÉÝ…ä1¥Ù”ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÀÔ°€‰…Ñ•½Éå}¥ˆè€‰Ù¥À‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰5QX1¥Ù”!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÀØ°€‰…Ñ•½Éå}¥ˆè€‰Ù¥À‰õt°(€€€€€€€ì‰Ù¥Àˆè€‰Y%@½±‰ô°€À¸ÐÀ¤(€€€¡•¬ ‰•¹•É¥Œ±¥Ù”±…‰•°…¹¹½ÐÉ•…Ñ”‰É½…‘…ÍÑ•È…¹‘¥‘…Ñ•Ìˆ°(€€€€€€€€€±¥Ù•}¹½¥Í”€ôômt¤(€€€ÍÑ½É•‘}ÍÁ½ÉÑÌ€ô}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}ÍÑ½É…”¡ÍÁ½ÉÑÍ}Í¡…É•¤(€€€¡•¬ ‰ÍÁ½ÉÑÌ‘¥Í¬…¡”½µ¥ÑÌÉ•‘•¹Ñ¥…°µ‰•…É¥¹œUI1Ìˆ°(€€€€€€€€€…±° ‰ÕÉ°ˆ¹½Ð¥¸É½Ü™½È­•ä¥¸€ ‰µ…Ñ¡•Ìˆ°€‰ÁÁÙ}¡¥ÑÌˆ¤(€€€€€€€€€€€€€™½ÈÉ½Ü¥¸ÍÑ½É•‘}ÍÁ½ÉÑÍm­•åt¤¤(€€€¡•¬ ‰ÍÁ½ÉÑÌ¹¼µÉ•ÍÕ±ÐÍÑ…Ñ”É•µ…¥¹Ì…¡•…‰±”ˆ°(€€€€€€€€€}ÍÁ½ÉÑÍ}É•ÍÕ±Ñ}™½É}ÍÑ½É…”¡ì‰±½•‘}¥¸ˆèQÉÕ”°(€€€€€€€€€€€€€€‰…Ù…¥±…‰¥±¥Ñå}¡•­•ˆèQÉÕ”°€‰µ…Ñ¡•Ìˆèmt°€‰ÁÁÙ}¡¥ÑÌˆèmt(€€€€€€€€€ô¤¹•Ð ‰…Ù…¥±…‰¥±¥Ñå}¡•­•ˆ¤¥ÌQÉÕ”¤(€€€½±‘}•Á}Ñ•ÍÐ€ô‘¥Ð¡}A}!¤(€€€ÑÉäè(€€€€€€€­¥­½™™}Ñ•ÍÐ€ô‘…Ñ•Ñ¥µ”¹‘…Ñ•Ñ¥µ” ÈÀÈØ°€à°€ÄÌ°€Äà°€ÐÔ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñé¥¹™¼õ‘…Ñ•Ñ¥µ”¹Ñ¥µ•é½¹”¹ÕÑŒ¤¹Ñ¥µ•ÍÑ…µÀ ¤(€€€€€€€}A}!¹±•…È ¤(€€€€€€€}A}!lˆÜÜ‰t€ôì‰ÑÌˆèÑ¥µ”¹Ñ¥µ” ¤°€‰ÁÉ½É…µµ•Ìˆèmì(€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè€‰!•…ÉÐ½˜5¥‘±½Ñ¡¥…¸Ø	•¹™¥„ˆ°(€€€€€€€€€€€€‰ÍÑ…ÉÑ}ÑÌˆè­¥­½™™}Ñ•ÍÐ€´€äÀÀ°€‰ÍÑ½Á}ÑÌˆè­¥­½™™}Ñ•ÍÐ€¬€ÜÈÀÁõuô(€€€€€€€•Á}™½Õ¹€ô}…¡•‘}•Á}‘¥Í½Ù•Éä (€€€€€€€€€€€mì‰¡½µ”ˆè€‰!•…ÉÑÌˆ°€‰…Ý…äˆè€‰	•¹™¥„ˆ°(€€€€€€€€€€€€€€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÍPÄàèÐÔèÀÁh‰õt°(€€€€€€€€€€€mì‰¹…µ”ˆè€‰ULèMA89•ÝÌ!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÜÜ°(€€€€€€€€€€€€€€‰…Ñ•½Éå}¥ˆè€‰ÕÌµÍÁ½ÉÑÌ‰õt°(€€€€€€€€€€€ì‰ÕÌµÍÁ½ÉÑÌˆè€‰ULðMA=IQL‰ô°}Q•ÍÑaÑÉ•…´ ¤¤(€€€€€€€•Á}É½ÝÌ€ô•Á}™½Õ¹¹•Ð¡}ÍÁ½ÉÑÍ}•Ù•¹Ñ}­•ä (€€€€€€€€€€€€‰!•…ÉÑÌˆ°€‰	•¹™¥„ˆ°€ˆÈÀÈØ´Àà´ÄÍPÄàèÐÔèÀÁhˆ¤°mt¤(€€€€€€€¡•¬ ‰…¡•A¥¹‘•Á•¹‘•¹Ñ±ä‘¥Í½Ù•ÉÌ™¥áÑÕÉ”¡…¹¹•°ˆ°(€€€€€€€€€€€€€±•¸¡•Á}É½ÝÌ¤€ôô€Ä…¹•Á}É½ÝÍlÁt¹•Ð ‰•Á}½¹™¥Éµ•ˆ¤¥ÌQÉÕ”¤(€€€€€€€¡•¬ ‰µ¥ÍÍ¥¹œ…¡•AÉ•µ…¥¹Ì¹•ÕÑÉ…°ˆ°(€€€€€€€€€€€€€}…¡•‘}•Á}‘¥Í½Ù•Éä (€€€€€€€€€€€€€€€€€mì‰¡½µ”ˆè€‰1••‘Ìˆ°€‰…Ý…äˆè€‰5…¹¡•ÍÑ•ÈU¹¥Ñ•ˆ°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÉÐˆè€ˆÈÀÈØ´Àà´ÄÍPÄàèÐÔèÀÁh‰õt°(€€€€€€€€€€€€€€€€€mt°íô°}Q•ÍÑaÑÉ•…´ ¤¤€ôôíô¤(€€€™¥¹…±±äè(€€€€€€€}A}!¹±•…È ¤(€€€€€€€}A}!¹ÕÁ‘…Ñ”¡½±‘}•Á}Ñ•ÍÐ¤(€€€±…ÍÌ}Q•ÍÑI…¥¹aÑÉ•…´è(€€€€€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€€€€€‘•˜ÍÑÉ•…µ}ÕÉ°¡ÍÑÉ•…µ}¥¤è(€€€€€€€€€€€É•ÑÕÉ¸€‰Ñ•ÍÐèˆ€¬ÍÑÈ¡ÍÑÉ•…µ}¥¤(€€€É…¥¹}É½ÝÌ€ô™¥¹‘}É…¥¹}¡…¹¹•±Ì (€€€€€€€ì‰Í•É¥•Ìˆè€‰˜Äˆ°€‰É…”ˆè€‰ÕÑ É…¹AÉ¥àˆ°€‰¥ÉÕ¥Ðˆè€‰i…¹‘Ù½½ÉÐ‰ô°(€€€€€€€mì‰¹…µ”ˆè€‰ÄÕÑ É…¹AÉ¥à€Ñ,ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÀ°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰M­äMÁ½ÉÑÌÄU!ˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÄ°€‰…Ñ•½Éå}¥ˆè€ˆÑ¬‰ô°(€€€€€€€€ì‰¹…µ”ˆè€‰ÄAAX€Äˆ°€‰ÍÑÉ•…µ}¥ˆè€ÈÈ°€‰…Ñ•½Éå}¥ˆè€‰ÁÁØ‰õt°(€€€€€€€Í…µÁ±•}…ÑÌ°}Q•ÍÑI…¥¹aÑÉ•…´ ¤¤(€€€É…¥¹}­¥¹‘Ì€ôíÉ½Ýl‰ÍÑÉ•…µ}¥‰tèÉ½Ü¹•Ð ‰µ…Ñ¡}­¥¹ˆ¤™½ÈÉ½Ü¥¸É…¥¹}É½ÝÍô(€€€¡•¬ ‰É…¥¹œ•Ù•¹ÐÁÉ½µ½Ñ•ˆ°É…¥¹}­¥¹‘Ì¹•Ð ÈÀ¤€ôô€‰•Ù•¹Ðˆ¤(€€€¡•¬ ‰É…¥¹œÍ•É¥•ÌÍ•½¹ˆ°É…¥¹}­¥¹‘Ì¹•Ð ÈÄ¤€ôô€‰Í•É¥•Ìˆ¤(€€€¡•¬ ‰É…¥¹œ…Ñ•½Éä™…±±‰…¬ˆ°É…¥¹}­¥¹‘Ì¹•Ð ÈÈ¤€ôô€‰Á½ÍÍ¥‰±”ˆ¤(€€€•Ù•¹Ñ}¥‘Ì€ôíÉ½Ýl‰ÍÑÉ•…µ}¥‰t™½ÈÉ½Ü¥¸™¥¹‘}Ñ•…µ}¡…¹¹•±Ì (€€€€€€€l‰	É…¹¸ˆ°€‰!…µ-…´‰t°Í…µÁ±•}¡…¹¹•±Ì°Í…µÁ±•}…ÑÌ°}Q•ÍÑaÑÉ•…´ ¤¥ô(€€€¡•¬ ‰‰½Ñ ™¥áÑÕÉ”Ñ•…µÌÉ…¹¬ˆ°€Ì¥¸•Ù•¹Ñ}¥‘Ì¤(€€€¡•¬ ‰½¹”µÑ•…´•Ù•¹ÐÉ•Ñ…¥¹•…ÌÁ½ÍÍ¥‰±”ˆ°€È¥¸•Ù•¹Ñ}¥‘Ì¤(€€€¡•¬ ‰É•Í•ÉÙ”Ñ•…´•á±Õ‘•ˆ°€Ð¹½Ð¥¸•Ù•¹Ñ}¥‘Ì¤(€€€É…¹­•€ôÉ…¹­}™¥áÑÕÉ•}¡…¹¹•±Ì¡l(€€€€€€€ì‰áÑÉ•…µ}¹…µ”ˆè€‰YQXAAX€Äˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀ°€‰Í½É”ˆè€À¸äÙô°(€€€€€€€ì‰áÑÉ•…µ}¹…µ”ˆè€‰A=11=81%5MM=0€´	I98ðYQXAAX€Ìˆ°(€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè€ÄÄ°€‰Í½É”ˆè€À¸àÁô°(€€€€€€€ì‰áÑÉ•…µ}¹…µ”ˆè€‰A=11=81%5MM=0€´	I98ðYQXAAX€Ðˆ°(€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè€ÄÈ°€‰Í½É”ˆè€À¸äåõt°€‰	É…¹¸ˆ°€‰!…µ-…´ˆ¤(€€€€ŒI”µÉ…¹¬Ñ¡”Í…µ”É½ÝÌ™½ÈÑ¡”•á…ÐÁ½±±½¸™¥áÑÕÉ”Í•Á…É…Ñ•±ä¸(€€€•á…Ñ}É…¹­•€ôÉ…¹­}™¥áÑÕÉ•}¡…¹¹•±Ì¡l(€€€€€€€ì‰áÑÉ•…µ}¹…µ”ˆè€‰YQXAAX€Äˆ°€‰ÍÑÉ•…µ}¥ˆè€ÄÀ°€‰Í½É”ˆè€À¸äÙô°(€€€€€€€ì‰áÑÉ•…µ}¹…µ”ˆè€‰A=11=81%5MM=0€´	I98ðYQXAAX€Ìˆ°(€€€€€€€€€‰ÍÑÉ•…µ}¥ˆè€ÄÄ°€‰Í½É”ˆè€À¸àÁõt°€‰Á½±±½¸1¥µ…ÍÍ½°ˆ°€‰	É…¹¸ˆ¤(€€€¡•¬ ‰•á…Ð™¥áÑÕÉ”Í½ÉÑ•™¥ÉÍÐˆ°•á…Ñ}É…¹­•‘lÁul‰ÍÑÉ•…µ}¥‰t€ôô€ÄÄ¤(€€€¡•¬ ‰½¹”µÑ•…´AAXÑ¥Ñ±”¥ÌÁÉ½µ½Ñ•‰ÕÐÉ•µ…¥¹ÌÁ½ÍÍ¥‰±”ˆ°(€€€€€€€€€É…¹­•‘lÁul‰™¥áÑÕÉ•}µ…Ñ ‰t€ôô€‰Á…ÉÑ¥…°ˆ¤(€€€¡•¬ ‰•¹•É¥ŒAAX…¹‘¥‘…Ñ”É•µ…¥¹Ì„™…±±‰…¬ˆ°(€€€€€€€€€É…¹­•‘l´Åul‰™¥áÑÕÉ•}µ…Ñ ‰t€ôô€‰•¹•É¥Œˆ¤(€€€¡•¬ ‰•µ‰•‘‘•Á…”Ù•ÉÍ¥½¸ˆ°€‰Øˆ€¬YIM%=8¥¸A¹É•Á±…” ‰}}YIM%=9}|ˆ°YIM%=8¤¤(€€€¡•¬ ‰±¥Ù”™…±±‰…¬¥Ì‰½Õ¹‘•…¹É••¹ÐÉ•ÍÕ±ÑÌÉ•µ…¥¸Ù¥Í¥‰±”ˆ°(€€€€€€€€€€‰¥˜¡˜¹¥Í}±¥Ù”¥É•ÑÕÉ¸µ¥¹ÌðôÄÔÀˆ¥¸A…¹(€€€€€€€€€€‰µ¥¹ÌðôÌØÀ˜˜…™¥áÑÕÉ•%Í1¥Ù”¡˜¤ˆ¥¸A…¹(€€€€€€€€€€‰¥˜ …}µåQ•…µ¥áÑÕÉ•Ì¹±•¹Ñ ¥ÕÁ½µ¥¹œ¹¥¹¹•É!Q50ˆ¥¸A¤(€€€É•ÑÕÉ¸¡•­Ì()¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè(€€€¥˜€ˆ´µÍ•±˜µÑ•ÍÐˆ¥¸ÍåÌ¹…ÉØè(€€€€€€€Á…ÍÍ•€ôÉÕ¹}Í•±™}Ñ•ÍÑÌ ¤(€€€€€€€ÁÉ¥¹Ð ‰M•±˜µÑ•ÍÐÁ…ÍÍ•è€ˆ€¬€ˆ°€ˆ¹©½¥¸¡Á…ÍÍ•¤¤(€€€•±Í”è(€€€€€€€µ…¥¸ ¤