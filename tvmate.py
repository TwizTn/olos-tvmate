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
import socket
import urllib.parse
import urllib.request
import urllib.error
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
_ACTIVE_PORT = PORT
LAN_PORT = 778
_ACTIVE_LAN_PORT = 0
REMOTE_PORT = 779
_ACTIVE_REMOTE_PORT = 0
_SERVER_INSTANCE_ID = hashlib.sha256(os.urandom(32)).hexdigest()[:20]
_CONFIG_LOCK = threading.RLock()
_FAVORITES_LOCK = threading.RLock()
_RELAY_TOKEN_LOCK = threading.RLock()
_RELAY_TOKENS = {}
_RELAY_TARGET_TOKENS = {}
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
VERSION = "0.777.b465"

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
    "dev_mode": False,
    "allow_lan": False,
    "lan_access_token": "",
    "private_remote_relay": False,
    "start_section": "mylist",
    "setup_complete": False,
    "setup_demo_content": False,
    "steam_wishlist_url": "",
    "steam_wishlist_id": "",
    "steam_wishlist_synced_at": 0,
}

def _local_lan_ip():
    """Return the preferred private/LAN address without sending any data."""
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    finally:
        if probe is not None:
            probe.close()
    try:
        address = socket.gethostbyname(socket.gethostname())
        return address if address and not address.startswith("127.") else ""
    except OSError:
        return ""

def _parse_tailscale_ipv4(output):
    for line in str(output or "").splitlines():
        value = line.strip()
        if _is_tailscale_ipv4(value):
            return value
    return ""

def _ipv4_number(value):
    try:
        packed = socket.inet_aton(str(value or ""))
        return int.from_bytes(packed, "big") if len(packed) == 4 else None
    except OSError:
        return None

def _is_tailscale_ipv4(value):
    number = _ipv4_number(value)
    return number is not None and 0x64400000 <= number <= 0x647fffff

def _unsafe_relay_address(value):
    number = _ipv4_number(value)
    if number is not None:
        ranges = ((0x00000000, 0x00ffffff), (0x0a000000, 0x0affffff),
                  (0x64400000, 0x647fffff), (0x7f000000, 0x7fffffff),
                  (0xa9fe0000, 0xa9feffff), (0xac100000, 0xac1fffff),
                  (0xc0000000, 0xc00000ff), (0xc0a80000, 0xc0a8ffff),
                  (0xc6120000, 0xc613ffff), (0xe0000000, 0xffffffff))
        return any(start <= number <= end for start, end in ranges)
    try:
        packed = socket.inet_pton(socket.AF_INET6, str(value or ""))
        return (packed == b"\x00" * 15 + b"\x01" or packed == b"\x00" * 16 or
                packed[0] == 0xff or (packed[0] & 0xfe) == 0xfc or
                (packed[0] == 0xfe and (packed[1] & 0xc0) == 0x80))
    except OSError:
        return True

def _tailscale_ipv4():
    """Return this device's active Tailscale IPv4 address, if available."""
    candidates = ["tailscale"]
    if sys.platform.startswith("win"):
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
            if root:
                candidates.append(os.path.join(root, "Tailscale", "tailscale.exe"))
    for executable in dict.fromkeys(candidates):
        try:
            result = subprocess.run([executable, "ip", "-4"], capture_output=True,
                                    text=True, timeout=4,
                                    creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                                   if sys.platform.startswith("win") else 0))
            address = _parse_tailscale_ipv4(result.stdout) if result.returncode == 0 else ""
            if address:
                return address
        except (OSError, subprocess.SubprocessError):
            continue
    return ""

def _lan_access_url(cfg, port=PORT, include_token=True):
    address = _local_lan_ip()
    if not address:
        return ""
    url = f"http://{address}:{int(port)}/"
    token = str(cfg.get("lan_access_token") or "")
    if include_token and token:
        url += "?token=" + urllib.parse.quote(token, safe="")
    return url

def _private_remote_url(cfg, port=REMOTE_PORT, include_token=True):
    address = _tailscale_ipv4()
    if not address:
        return ""
    url = f"http://{address}:{int(port)}/"
    token = str(cfg.get("lan_access_token") or "")
    if include_token and token:
        url += "?token=" + urllib.parse.quote(token, safe="")
    return url

def _relay_signing_key(cfg=None):
    cfg = cfg or load_config()
    return str(cfg.get("lan_access_token") or "").encode("utf-8")

def _hmac_sha256(key, message):
    """HMAC-SHA256 without importing modules absent from legacy launchers."""
    block_size = 64
    key = bytes(key)
    if len(key) > block_size:
        key = hashlib.sha256(key).digest()
    key = key.ljust(block_size, b"\0")
    inner = bytes(byte ^ 0x36 for byte in key)
    outer = bytes(byte ^ 0x5c for byte in key)
    return hashlib.sha256(outer + hashlib.sha256(inner + bytes(message)).digest()).hexdigest()

def _relay_token(target, lifetime=6 * 3600, cfg=None):
    key = _relay_signing_key(cfg)
    if not key:
        return ""
    target = str(target)
    expires = int(time.time()) + int(lifetime)
    with _RELAY_TOKEN_LOCK:
        now = int(time.time())
        for old_id, record in list(_RELAY_TOKENS.items()):
            if int(record[1]) < now:
                _RELAY_TOKENS.pop(old_id, None)
                if _RELAY_TARGET_TOKENS.get(str(record[0])) == old_id:
                    _RELAY_TARGET_TOKENS.pop(str(record[0]), None)
        token_id = _RELAY_TARGET_TOKENS.get(target, "") if lifetime > 300 else ""
        record = _RELAY_TOKENS.get(token_id)
        if not record or int(record[1]) < now + 300:
            token_id = os.urandom(24).hex()
            _RELAY_TOKENS[token_id] = (target, expires)
            if lifetime > 300:
                _RELAY_TARGET_TOKENS[target] = token_id
    signature = _hmac_sha256(key, token_id.encode("ascii"))
    return token_id + "." + signature

def _relay_target(token, cfg=None):
    try:
        token_id, signature = str(token or "").rsplit(".", 1)
        key = _relay_signing_key(cfg)
        expected = _hmac_sha256(key, token_id.encode("ascii"))
        if not key or not _secure_equal(signature, expected):
            return ""
        with _RELAY_TOKEN_LOCK:
            record = _RELAY_TOKENS.get(token_id)
            if not record or int(record[1]) < int(time.time()):
                _RELAY_TOKENS.pop(token_id, None)
                return ""
            return str(record[0])
    except (ValueError, TypeError):
        return ""

def _safe_relay_target(target, initial=False, cfg=None):
    """Allow configured-provider URLs and public playlist children; block SSRF."""
    cfg = cfg or load_config()
    parsed = urllib.parse.urlsplit(str(target or ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    provider = urllib.parse.urlsplit(str(cfg.get("xtream_host") or ""))
    if initial:
        return bool(provider.hostname and parsed.hostname.lower() == provider.hostname.lower() and
                    (parsed.port or (443 if parsed.scheme == "https" else 80)) ==
                    (provider.port or (443 if provider.scheme == "https" else 80)))
    if provider.hostname and parsed.hostname.lower() == provider.hostname.lower():
        return True
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443,
                                                                type=socket.SOCK_STREAM)}
        return bool(addresses) and all(not _unsafe_relay_address(address) for address in addresses)
    except (OSError, ValueError):
        return False

class _SafeRelayRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        if not _safe_relay_target(absolute):
            raise urllib.error.HTTPError(absolute, 403, "unsafe relay redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, absolute)

def _secure_equal(left, right):
    """Constant-time token comparison without optional stdlib dependencies."""
    left = str(left or "").encode("utf-8")
    right = str(right or "").encode("utf-8")
    mismatch = len(left) ^ len(right)
    size = max(len(left), len(right))
    for index in range(size):
        mismatch |= (left[index] if index < len(left) else 0) ^ (right[index] if index < len(right) else 0)
    return mismatch == 0

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
    if "tvguide.vg.no" in u: return "vg_tvguide"
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
    ("vg_tvguide", "Norwegian TV guide (VG)"),
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
_XT_CHANNELS_LOCK = threading.Lock()
_XT_MOVIES_LOCK = threading.Lock()
_XT_SERIES_LOCK = threading.Lock()
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

def _rejected_update_version():
    try:
        with open(os.path.join(app_dir(), "update-rejected.txt"), "r", encoding="utf-8") as handle:
            value = handle.readline(200).strip()
        return value if re.fullmatch(r"[0-9A-Za-z._-]+", value) else ""
    except OSError:
        return ""

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
    if remote == _rejected_update_version():
        return (False, remote)
    try:
        newer = _parse_ver(remote) > _parse_ver(VERSION)
    except Exception:
        newer = (remote != VERSION)
    return (newer, remote)

def download_update():
    """Download and validate a new tvmate.py. Return its local path or None."""
    remote_version, expected_sha = _update_manifest()
    if remote_version and remote_version == _rejected_update_version():
        return None
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
    with _XT_CHANNELS_LOCK:
        now = time.time()
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
    with _XT_MOVIES_LOCK:
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
    with _XT_SERIES_LOCK:
        now = time.time()
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
VG_TVGUIDE_CHANNELS = "https://tvguide.vg.no/kanal"
VG_TVGUIDE_SCHEDULE = "https://tvguide.vg.no/kanal/{slug}/{date_key}"
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
_VG_TVGUIDE_TTL = 6 * 3600
_VG_CHANNEL_TTL = 24 * 3600
_VG_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.I | re.S)
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

def _rank_f1_constructor_drivers(drivers, appearances, points):
    ranked = [dict(driver) for driver in drivers]
    for driver in ranked:
        driver["season_appearances"] = int(appearances.get(driver["id"], 0))
        driver["season_points"] = float(points.get(driver["id"], 0))
    ranked.sort(key=lambda driver: (-driver["season_appearances"],
                                    -driver["season_points"], driver["name"].lower()))
    return ranked[:2]

def get_f1_team_drivers(constructor_id, force=False):
    """Current race drivers for a selected F1 constructor, cached for a week."""
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(constructor_id or ""))
    if not safe:
        return []
    filename = f"f1-drivers-{safe}.json"
    if not force:
        disk = _load_timed_data_cache(filename, _F1_TTL)
        if (isinstance(disk, list) and disk and len(disk) <= 2 and
                all("season_appearances" in row for row in disk)):
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
    appearances, points = {}, {}
    try:
        results_data = _f1_api(
            f"{year}/constructors/{safe}/results.json?limit=200")
        for race in (((results_data.get("MRData") or {}).get("RaceTable") or {}).get("Races") or []):
            for result in race.get("Results") or []:
                driver_id = str((result.get("Driver") or {}).get("driverId") or "")
                if driver_id:
                    appearances[driver_id] = appearances.get(driver_id, 0) + 1
    except Exception:
        pass
    try:
        standings_data = _f1_api(f"{year}/driverstandings.json?limit=100")
        lists = ((standings_data.get("MRData") or {}).get("StandingsTable") or {}).get("StandingsLists") or []
        for standing in ((lists[0].get("DriverStandings") or []) if lists else []):
            driver_id = str((standing.get("Driver") or {}).get("driverId") or "")
            try:
                points[driver_id] = float(standing.get("points") or 0)
            except (TypeError, ValueError):
                points[driver_id] = 0.0
    except Exception:
        pass
    # Constructor endpoints include anyone who substituted during the season.
    # Rank by actual season participation (then points) so a one-off substitute
    # cannot evict a regular driver merely because the API returned three rows.
    drivers = _rank_f1_constructor_drivers(drivers, appearances, points)
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

def _racing_weekend_end(value, year):
    """Return the final day of an official F2/F3 weekend date range."""
    text = re.sub(r"\s+", " ", str(value or "").strip().upper())
    match = re.search(r"\b(\d{1,2})\s*(?:-|â€“|â€”)\s*(\d{1,2})\s+([A-Z]{3})\b", text)
    if not match:
        return ""
    try:
        dt = datetime.datetime.strptime(
            f"{match.group(2)} {match.group(3)} {year}", "%d %b %Y")
        return dt.replace(hour=23, minute=59, second=59,
                          tzinfo=datetime.timezone.utc).isoformat()
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
        if isinstance(disk, list) and disk and all(row.get("end") for row in disk):
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
        end = _racing_weekend_end(html.unescape(dates).strip(), year)
        if not start or not end:
            continue
        seen.add(path)
        rows.append({"series": series, "series_name": series.upper(),
                     "race": html.unescape(circuit).strip(), "session": "Race weekend",
                     "circuit": html.unescape(circuit).strip(), "start": start,
                     "end": end,
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

def _team_squad_variant(name):
    """Classify explicit non-senior team markers without guessing from club names."""
    value = str(name or "").lower().strip()
    normalized = re.sub(r"[^a-z0-9Ã¦Ã¸Ã¥]+", " ", value).strip()
    if re.search(r"(?:^|\s)(?:women|womens|ladies|kvinner|damer|femmes|femenin[oa]|w)(?:\s|$)", normalized):
        return "women"
    youth = re.search(r"(?:^|\s)u(?:17|18|19|20|21|23)(?:\s|$)", normalized)
    if youth:
        return youth.group(0).strip()
    if re.search(r"(?:^|\s)(?:reserves?|academy|b|ii)(?:\s|$)", normalized):
        return "reserve"
    return "senior"

def _daily_match_involves_team(match, term, team_id=""):
    home_obj = match.get("home") or {}
    away_obj = match.get("away") or {}
    requested_id = str(team_id or "").strip()
    home_id = str(home_obj.get("id") or "").strip()
    away_id = str(away_obj.get("id") or "").strip()
    if requested_id and (home_id or away_id):
        return requested_id in (home_id, away_id)
    term_l = str(term or "").lower().strip()
    wanted = _expand_terms(term_l)
    wanted_variant = _team_squad_variant(term_l)
    return any(_team_squad_variant(name) == wanted_variant and
               _team_field_matches(name, wanted, term_l)
               for name in (home_obj.get("name"), away_obj.get("name")))

def search_daily_matches(term, team_id=""):
    """Find today's live/upcoming fixtures independent of TV coverage."""
    term_l = str(term or "").lower().strip()
    if not term_l:
        return []
    out = []
    for match in fetch_fotmob_daily_matches():
        home_obj = match.get("home") or {}
        away_obj = match.get("away") or {}
        status = match.get("status") or {}
        if status.get("cancelled"):
            continue
        home = str(home_obj.get("name") or "")
        away = str(away_obj.get("name") or "")
        if not _daily_match_involves_team(match, term_l, team_id):
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

def _vg_next_data(page):
    """Read VG TV Guide's server-rendered Next.js payload defensively."""
    match = _VG_NEXT_DATA_RE.search(str(page or ""))
    if not match:
        raise ValueError("VG TV Guide page did not contain schedule data")
    data = json.loads(html.unescape(match.group(1)))
    props = ((data.get("props") or {}).get("pageProps") or {})
    return props if isinstance(props, dict) else {}

def fetch_vg_channel_catalog():
    """Return VG's public Norwegian channel catalogue (cached for one day)."""
    cached = _load_timed_data_cache("vg-tvguide-channels.json", _VG_CHANNEL_TTL)
    if isinstance(cached, list) and cached:
        return cached
    props = _vg_next_data(http_get_text(VG_TVGUIDE_CHANNELS, timeout=15))
    rows = []
    for channel in props.get("channels") or []:
        if not isinstance(channel, dict):
            continue
        name = str(channel.get("name") or "").strip()
        slug = str(channel.get("slug") or "").strip()
        if name and re.fullmatch(r"[a-z0-9-]+", slug):
            rows.append({"name": name, "slug": slug})
    if rows:
        _save_timed_data_cache("vg-tvguide-channels.json", rows)
    return rows

def _vg_date_key(start):
    """Map a fixture timestamp to VG's today/tomorrow/weekday route."""
    try:
        from zoneinfo import ZoneInfo
        kickoff = datetime.datetime.fromisoformat(
            str(start or "").replace("Z", "+00:00"))
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=datetime.timezone.utc)
        oslo = ZoneInfo("Europe/Oslo")
        target = kickoff.astimezone(oslo).date()
        today = datetime.datetime.now(oslo).date()
    except Exception:
        return ""
    distance = (target - today).days
    if distance < 0 or distance > 6:
        return ""
    if distance == 0:
        return "idag"
    if distance == 1:
        return "imorgen"
    return ("mandag", "tirsdag", "onsdag", "torsdag", "fredag",
            "lÃ¸rdag", "sÃ¸ndag")[target.weekday()]

def fetch_vg_channel_schedule(slug, date_key):
    """Fetch one VG channel/day as small normalized programme records."""
    safe_slug = str(slug or "").strip().lower()
    safe_date = str(date_key or "").strip().lower()
    if (not re.fullmatch(r"[a-z0-9-]+", safe_slug) or
            safe_date not in {"idag", "imorgen", "mandag", "tirsdag",
                              "onsdag", "torsdag", "fredag", "lÃ¸rdag", "sÃ¸ndag"}):
        return []
    filename = "vg-tvguide-%s-%s.json" % (safe_slug, safe_date)
    cached = _load_timed_data_cache(filename, _VG_TVGUIDE_TTL)
    if isinstance(cached, list):
        return cached
    url = VG_TVGUIDE_SCHEDULE.format(
        slug=safe_slug, date_key=urllib.parse.quote(safe_date))
    props = _vg_next_data(http_get_text(url, timeout=15))
    schedule = props.get("initialTvSchedule") or {}
    rows = []
    for listing in schedule.get("listings") or []:
        if not isinstance(listing, dict):
            continue
        title_obj = listing.get("title") or {}
        event_obj = listing.get("sportsEvent") or {}
        episode_obj = listing.get("episode") or {}
        names = [str(value or "").strip() for value in (
            event_obj.get("name") if isinstance(event_obj, dict) else "",
            title_obj.get("title") if isinstance(title_obj, dict) else "",
            episode_obj.get("name") if isinstance(episode_obj, dict) else "")]
        title = " Â· ".join(value for index, value in enumerate(names)
                           if value and value not in names[:index])
        start = str(listing.get("startsAt") or "").strip()
        stop = str(listing.get("endsAt") or "").strip()
        if title and start:
            rows.append({"title": title, "start": start, "stop": stop})
    _save_timed_data_cache(filename, rows)
    return rows

def _vg_channel_key(name):
    value = normalise(str(name or ""))
    value = re.sub(r"^tv2\b", "tv 2", value)
    value = re.sub(r"\bv sport (?:pl|prem league)\b", "v sport premier league", value)
    return re.sub(r"\s+", " ", value).strip()

def _vg_fixture_discoveries(fixtures, channels, cats, x):
    """Confirm exact Norwegian linear channels from VG's public TV guide.

    VG is only corroboration: a row is emitted when the programme contains
    both fixture teams near kickoff and the IPTV channel name maps exactly to
    VG's channel. Network/layout failures are neutral and never block results.
    """
    prepared = []
    requested = {}
    premier_names = {"v sport premier league"} | {
        "v sport premier league " + str(number) for number in range(1, 5)}
    try:
        catalog = fetch_vg_channel_catalog()
    except Exception:
        return {}
    catalog_by_key = {_vg_channel_key(row.get("name")): row for row in catalog}
    for fixture in fixtures or []:
        if not isinstance(fixture, dict):
            continue
        date_key = _vg_date_key(fixture.get("start"))
        if not date_key:
            continue
        try:
            kickoff = datetime.datetime.fromisoformat(str(
                fixture.get("start") or "").replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        desired = {_vg_channel_key(name) for name in
                   ((fixture.get("by_country") or {}).get("NO") or [])}
        if "premier league" in normalise_event_name(fixture.get("league_name") or ""):
            desired.update(premier_names)
        guide_channels = [catalog_by_key[key] for key in desired
                          if key in catalog_by_key and not _is_streaming(key)]
        if not guide_channels:
            continue
        prepared.append((fixture, kickoff, date_key, guide_channels))
        for guide in guide_channels:
            requested[(guide["slug"], date_key)] = guide
    if not requested:
        return {}
    schedules = {}
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(5, len(requested))) as pool:
            jobs = {pool.submit(fetch_vg_channel_schedule, slug, date_key):
                    (slug, date_key) for slug, date_key in requested}
            for future in as_completed(jobs):
                try:
                    schedules[jobs[future]] = future.result()
                except Exception:
                    schedules[jobs[future]] = []
    except Exception:
        return {}
    playlist_by_key = {}
    for channel in channels or []:
        if not isinstance(channel, dict):
            continue
        category = cats.get(channel.get("category_id"), "")
        country = _resolve_channel_country(channel.get("name"), category)
        if country not in (None, "no"):
            continue
        cleaned, _country = _normalise_channel_country_labels(
            channel.get("name"), country)
        playlist_by_key.setdefault(_vg_channel_key(cleaned), []).append(channel)
    found = {}
    for fixture, kickoff, date_key, guide_channels in prepared:
        event_key = _sports_event_key(
            fixture.get("home"), fixture.get("away"), fixture.get("start"))
        for guide in guide_channels:
            exact_title = ""
            for programme in schedules.get((guide["slug"], date_key), []):
                try:
                    start = datetime.datetime.fromisoformat(
                        programme["start"].replace("Z", "+00:00")).timestamp()
                    stop = datetime.datetime.fromisoformat(
                        str(programme.get("stop") or programme["start"]).replace(
                            "Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if start > kickoff + 90 * 60 or stop < kickoff - 60 * 60:
                    continue
                if _fixture_title_has_both_teams(
                        programme.get("title"), fixture.get("home"), fixture.get("away")):
                    exact_title = programme["title"]
                    break
            if not exact_title:
                continue
            for channel in playlist_by_key.get(_vg_channel_key(guide["name"]), []):
                row = {
                    "xtream_name": str(channel.get("name") or ""),
                    "stream_id": channel.get("stream_id"),
                    "category": cats.get(channel.get("category_id"), ""),
                    "logo": channel.get("stream_icon", ""),
                    "quality": quality_tag(str(channel.get("name") or "")),
                    "url": x.stream_url(channel.get("stream_id")),
                    "matched": guide["name"], "score": 1.0,
                    "provider_exact": True, "fixture_match": "exact",
                    "vg_confirmed": True, "epg_confirmed": True,
                    "epg_title": exact_title,
                }
                found.setdefault(event_key, {})[str(channel.get("stream_id"))] = row
    return {key: list(rows.values()) for key, rows in found.items()}

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

def _fixture_candidate_involves_team(item, team_id, team_name="",
                                     trusted_opponent=False):
    """Prove a fixture-shaped object belongs to the requested team.

    Opponent-only rows are safe only when they came from the team's explicit
    fixture list.  A recursive scrape of the wider team payload can contain
    opponent-shaped rows for unrelated competitions and must identify the team
    by id or name instead.
    """
    if not isinstance(item, dict):
        return False
    team_id = str(team_id or "").strip()
    home_obj = item.get("home") if isinstance(item.get("home"), dict) else {}
    away_obj = item.get("away") if isinstance(item.get("away"), dict) else {}
    if (str(home_obj.get("id") or "") == team_id or
            str(away_obj.get("id") or "") == team_id):
        return True
    opponent = item.get("opponent")
    if (trusted_opponent and isinstance(opponent, dict) and
            str(opponent.get("name") or "").strip()):
        return True
    team_alias = _expand_terms(str(team_name or "").lower().strip()) if team_name else set()
    if not team_alias:
        return False
    term_l = str(team_name or "").lower().strip()
    return (_team_field_matches(home_obj.get("name"), team_alias, term_l) or
            _team_field_matches(away_obj.get("name"), team_alias, term_l))

def fetch_team_schedule(team_id, team_name=""):
    """Fetch a team's real FotMob fixture/status feed (not the TV guide)."""
    team_id = str(team_id or "").strip()
    if not team_id:
        return []
    now = time.time()
    cached = _TEAM_FIXTURE_CACHE.get(team_id)
    if cached and now - cached["ts"] < _TEAM_FIXTURE_TTL:
        return [dict(row) for row in cached["fixtures"]]
    disk = _load_timed_data_cache(f"team-fixtures-v2-{team_id}.json", _TEAM_FIXTURE_TTL)
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
    # Keep provenance: opponent-only rows are trusted only inside the team's
    # explicit fixture list, never merely because the recursive payload scrape
    # happened to find an `opponent` object.
    candidates = [(item, True) for item in raw]
    def collect_current(obj):
        if isinstance(obj, dict):
            status = obj.get("status")
            home = obj.get("home")
            away = obj.get("away")
            opponent = obj.get("opponent")
            if (isinstance(status, dict) and
                    ((isinstance(home, dict) and isinstance(away, dict)) or
                     isinstance(opponent, dict))):
                candidates.append((obj, False))
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
                candidates.append((match, False))
                if match.get("id") is not None:
                    daily_status[str(match.get("id"))] = match.get("status") or {}
    except Exception:
        pass
    out = []
    seen_fixtures = set()
    # `collect_current` deliberately scrapes fixture-shaped objects from the
    # whole team payload (FotMob relocates a live match out of allFixtures).
    # That payload also carries unrelated matches, so every candidate must be
    # proven to involve THIS team before it becomes part of the schedule.
    for item, trusted_opponent in candidates:
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
        if not _fixture_candidate_involves_team(
                item, team_id, team_name, trusted_opponent=trusted_opponent):
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
    _save_timed_data_cache(f"team-fixtures-v2-{team_id}.json", base_out)
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
    ["bodÃ¸/glimt", "bodÃ¸ glimt", "bodo glimt", "bodoglimt"],
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
    if _team_squad_variant(a) != _team_squad_variant(b):
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

def _team_field_matches(field, want, term_l=""):
    """True if any wanted alias identifies this team field. Uses word-aware
    matching anchored to the START of the name so a nickname like 'wolves' does
    not match an unrelated club that merely contains the word ('Red Wolves').
    A full multi-word alias may also match anywhere as a whole-word phrase."""
    name = normalise(str(field or "")).strip()
    if not name:
        return False
    words = name.split()
    tl = normalise(term_l).strip()
    for alias in want:
        a = normalise(str(alias)).strip()
        if not a:
            continue
        awords = a.split()
        # Whole-name exact match always counts.
        if name == a:
            return True
        # A short single-word alias (a nickname like "real", "inter", "milan")
        # is collision-prone. Only allow it to anchor the name when the USER
        # actually searched that short term - not when it was pulled in as a
        # secondary alias of a more specific multi-word search.
        short_nick = (len(awords) == 1 and len(a) <= 5)
        if short_nick and tl and tl != a and len(tl.split()) > 1:
            continue
        # Anchored: the team name begins with the alias words. Matches
        # "wolves"->"wolves" / "wolverhampton..." but NOT "red wolves sc".
        if words[:len(awords)] == awords:
            return True
        # A specific multi-word alias (>=2 words) may appear as a contiguous
        # whole-word phrase anywhere; multi-word aliases rarely collide.
        if len(awords) >= 2:
            for i in range(len(words) - len(awords) + 1):
                if words[i:i + len(awords)] == awords:
                    return True
    return False

def search_fixtures(term, countries):
    term_l = term.lower().strip()
    want = _expand_terms(term_l)
    merged, errors = {}, []
    guides, errors = _fetch_country_guides(countries)
    for country, fx in guides:
        for f in fx:
            # Match each TEAM field on its own, using word-aware matching, so a
            # short nickname like "wolves" doesn't match a coincidental
            # substring in an unrelated club ("Chattanooga Red Wolves SC").
            fields = [f.get("home", ""), f.get("away", ""),
                      f.get("home_slug", ""), f.get("away_slug", "")]
            if not any(_team_field_matches(field, want, term_l) for field in fields):
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
_LATIN_FOLD = str.maketrans({"Ã¸": "o", "Ã¦": "ae", "Ã¥": "a",
                            "Ã¶": "o", "Ã¤": "a", "Ã¼": "u",
                            "Ã©": "e", "Ã¨": "e", "Ã¡": "a", "Ã­": "i",
                            "Ã³": "o", "Ãº": "u", "Ã±": "n", "Ã§": "c"})

# Words that carry no identifying power on their own.
_GENERIC = {"sport", "sports", "tv", "play", "channel", "the", "hd", "sd",
            "fhd", "uhd", "4k", "raw", "vip", "gold", "ultra", "premium",
            "fps", "dolby", "audio", "live", "1", "one"}

def normalise(name):
    n = name.lower().translate(_LATIN_FOLD)
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
    n = str(name or "").lower().translate(_LATIN_FOLD)
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
    "dazn", "meo", "stan", "amazon", "prime video", "disney", "espn+", "hbo", "max",
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
    return ("ppv" in c) or ("event" in c) or bool(re.search(
        r"(?<![a-z0-9])play(?![a-z0-9])", c))

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
            # OTT/access-platform listings describe where a fixture is
            # available, not every linear channel carrying that brand.  A
            # bare DAZN or MEO listing must not pull in DAZN La Liga/F1 or
            # MEO-packaged CNN/MTV/Globo.  Explicit PPV/Play/Event *channel
            # names* remain useful, but a broad category cannot make DK2 or a
            # film channel relevant. Fixture-title channels are discovered
            # independently by find_team_channels().
            if (_is_streaming(orig) and not _is_ppv_category(cname)):
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

def _team_form_hit(hay, values):
    """Match normal and compact spellings such as BodÃ¸/Glimt and BodoGlimt."""
    compact_hay = re.sub(r"\s+", "", hay)
    return any(
        re.search(r"(?<![a-z0-9])" + re.escape(value) +
                  r"(?![a-z0-9])", hay) or
        (len(value.split()) >= 2 and len(value.replace(" ", "")) >= 5 and
         value.replace(" ", "") in compact_hay)
        for value in values if value)

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
            return _team_form_hit(hay, values)
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

def _filter_streaming_platform_slots(rows):
    """Do not guess a numbered OTT event slot from the platform name alone."""
    return [row for row in rows
            if not _is_streaming(row.get("matched")) or
            row.get("fixture_match") == "exact"]

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
            matched_forms = [form for form in forms if _team_form_hit(hay, [form])]
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
    "wrc": ("wrc", "world rally", "rally tv", "rally"),
}
_RACING_AVAILABILITY_CACHE = {"key": "", "ts": 0, "availability": {}}
_RACING_AVAILABILITY_TTL = 15 * 60
_SPORTS_EVENT_CHANNEL_CACHE = {}
_SPORTS_EVENT_CHANNEL_TTL = 15 * 60

def _sports_availability_cache_path():
    return os.path.join(data_cache_dir(), "sports-availability.json")

def _sports_cache_signature(cfg, x):
    return "football-v35|" + _vod_cache_key(x) + "|" + str(
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
        return _team_form_hit(hay, forms)
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
    vg_discovered = _vg_fixture_discoveries([fixture], channels, cats, x)
    result = _add_epg_discoveries(result, vg_discovered.get(_sports_event_key(
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
    # An OTT listing (TV 2 Play, Viaplay, DAZN, etc.) proves the platform, not
    # which numbered event slot carries this fixture. Only retain one of those
    # slots when its visible channel title names both teams; cached EPG can
    # independently confirm channels whose catalogue name is generic.
    matches = _filter_streaming_platform_slots(matches)
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

# Grand Prix names use the nationality ("Italian"), provider channel names use
# the country ("ITALY: RACE"). Keep both spellings so either form matches.
_RACE_COUNTRY_ALIASES = {
    "italian": ("italy", "italia"), "dutch": ("netherlands", "nederland", "holland"),
    "british": ("britain", "uk", "england", "great"), "spanish": ("spain", "espana"),
    "belgian": ("belgium",), "austrian": ("austria",), "hungarian": ("hungary",),
    "japanese": ("japan",), "mexican": ("mexico",), "brazilian": ("brazil", "brasil"),
    "canadian": ("canada",), "australian": ("australia",), "french": ("france",),
    "german": ("germany", "deutschland"), "portuguese": ("portugal",),
    "singapore": ("singapore",), "qatar": ("qatar",), "bahrain": ("bahrain",),
    "saudi": ("saudi", "arabia"), "chinese": ("china",), "american": ("usa", "america"),
    "azerbaijan": ("azerbaijan", "baku"), "monaco": ("monaco",), "emilia": ("imola",),
    "swedish": ("sweden", "sverige"), "finnish": ("finland",), "norwegian": ("norway", "norge"),
    "polish": ("poland",), "danish": ("denmark",), "turkish": ("turkey",),
    "argentine": ("argentina",), "chilean": ("chile",), "paraguay": ("paraguay",),
}

def find_racing_channels(event, xtream_channels, cats, x, drivers=()):
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
    # Providers name event channels after the COUNTRY ("ITALY: RACE") while the
    # calendar uses the nationality ("Italian Grand Prix"), so carry both.
    for word in list(event_words):
        for extra in _RACE_COUNTRY_ALIASES.get(word, ()):  # italian -> italy...
            if extra not in event_words:
                event_words.append(extra)
    session = normalise(str(event.get("session") or ""))
    session_terms = set(_distinctive(session.split()))
    session_terms.difference_update({"race", "rally", "weekend"})
    if "qualifying" in session_terms:
        session_terms.add("kvalifisering")
    if "sprint" in session_terms:
        session_terms.add("sprintkval")
    race_name = normalise(str(event.get("race") or ""))
    race_phrases = {race_name}
    if "grand prix" in race_name:
        race_phrases.add(race_name.replace("grand prix", "gp"))
    race_phrases.discard("")
    # A one-word venue is not an event title: "Monza" also occurs in football
    # and other channels. It becomes useful only alongside the series alias.
    strong_race_phrases = {phrase for phrase in race_phrases
                           if len(_distinctive(phrase.split())) >= 2}
    # Surnames of the drivers the user follows in THIS series. Surnames are
    # distinctive enough to identify a per-driver feed; first names are not.
    driver_terms = set()
    for driver in (drivers or ()):
        if not isinstance(driver, dict):
            continue
        if series and str(driver.get("series") or "").lower() not in ("", series):
            continue
        parts = [p for p in normalise(str(driver.get("name") or "")).split() if len(p) >= 4]
        if parts:
            driver_terms.add(parts[-1])
    out = []
    for ch in xtream_channels:
        cname = str(ch.get("name") or "")
        hay = normalise(cname)
        if not hay:
            continue
        # WRC-TV is the callsign of NBC's Washington station. Provider names
        # such as "NBC 4 (WRC) Washington" are local TV/news affiliates, not
        # World Rally Championship channels.
        if (series == "wrc" and re.search(
                r"\b(?:nbc|abc|cbs|fox)\s*\d*\s*\(\s*wrc\s*\)", cname, re.I)):
            continue
        category = cats.get(ch.get("category_id"), "")
        channel_cc = _resolve_channel_country(cname, category)
        cleaned_name, channel_cc = _normalise_channel_country_labels(
            cname, channel_cc)
        padded = " " + hay + " "
        series_hit = any((" " + alias + " ") in padded for alias in aliases if alias)
        event_hits = sum(1 for word in event_words
                         if re.search(r"(?<![a-z0-9])" + re.escape(word) +
                                      r"(?![a-z0-9])", hay))
        event_phrase_hit = any(
            re.search(r"(?<![a-z0-9])" + re.escape(phrase) +
                      r"(?![a-z0-9])", hay)
            for phrase in strong_race_phrases)
        # A country adjective or circuit word alone is weak evidence: "Italian"
        # and "Monza" occur in unrelated news, football and other motorsport.
        # Accept the full race title, or require the requested racing series too.
        event_hit = event_phrase_hit or (series_hit and event_hits >= 1)
        session_hit = bool(session_terms and any(
            re.search(r"(?<![a-z0-9])" + re.escape(word) +
                      r"(?![a-z0-9])", hay) for word in session_terms))
        # Providers often name a per-driver feed after the driver themselves
        # ("Formula 1 PPV Max Verstappen", "F1: ISACK HADJAR | RED BULL | HAD UK").
        # A followed driver's surname counts as event-level evidence when the
        # channel also looks like this series or sits in a PPV/event bucket -
        # the second case matters because those feeds do not always spell the
        # series out in a way the alias list recognises.
        driver_name_hit = bool(driver_terms and any(
            re.search(r"(?<![a-z0-9])" + re.escape(word) +
                      r"(?![a-z0-9])", hay) for word in driver_terms))
        f1_feed_name = re.sub(
            r"\b(vip|gold|raw|dolby|audio|backup|feed)\b", " ", cleaned_name)
        f1_feed_name = re.sub(r"\s+", " ", f1_feed_name).strip()
        norway_f1 = bool(
            series == "f1" and channel_cc == "no" and
            re.fullmatch(r"v sport 1", f1_feed_name))
        formula_ladder_broadcaster = bool(
            series in ("f2", "f3") and
            re.fullmatch(r"f1 tv(?: pro)?", f1_feed_name))
        # Viaplay carries IndyCar in the Nordics too. F1 reliably maps to V Sport 1,
        # but Viaplay spreads IndyCar across whichever V Sport feed fits the slot,
        # so we can't pin it to one channel - we surface Viaplay/V Sport feeds as
        # "possible" instead of a definitive broadcaster match.
        viaplay_event_feed = bool(
            series in ("f1", "f2", "f3", "indycar") and channel_cc == "no" and
            "viaplay" in normalise(cname + " " + category) and
            _is_ppv_category(cname))
        indycar_vsport_no = bool(
            series == "indycar" and channel_cc == "no" and
            re.fullmatch(r"v sport(?: [1-3+])?", f1_feed_name))
        ppv_context = _is_ppv_category(category) or _is_ppv_category(cname)
        event_context = ppv_context or _is_4k_category(category)
        driver_hit = bool(driver_name_hit and (series_hit or event_context))
        if driver_hit:
            event_hit = True
        if not (norway_f1 or formula_ladder_broadcaster or viaplay_event_feed or
                indycar_vsport_no or series_hit or driver_hit or
                (event_hit and event_context)):
            continue
        # Event title beats a dedicated series channel; generic series entries
        # in PPV/Play/Event buckets remain useful but are only possible matches.
        # A generic Nordic V Sport feed proves the rights holder, not which
        # linear slot carries this IndyCar event. Keep it as a possible fallback.
        match_kind = ("broadcaster" if (norway_f1 or formula_ladder_broadcaster) else
                      ("event" if (event_hit or (series_hit and session_hit)) else
                      ("possible" if (ppv_context or viaplay_event_feed or
                                      indycar_vsport_no) else "series")))
        out.append({"xtream_name": cname, "stream_id": ch.get("stream_id"),
                    "category": category, "logo": ch.get("stream_icon", ""),
                    "quality": quality_tag(cname),
                    "match_kind": match_kind, "driver_hit": bool(driver_hit),
                    "url": x.stream_url(ch.get("stream_id"))})
    # Stable unique IDs; a provider can occasionally expose duplicate rows.
    seen, unique = set(), []
    for row in out:
        sid = str(row.get("stream_id"))
        if sid in seen:
            continue
        seen.add(sid); unique.append(row)
    order = {"broadcaster": 0, "event": 1, "series": 2, "possible": 3}
    # A feed named after a driver the user follows is the most useful hit of
    # all, so it sorts ahead of its peers and can never fall past the result
    # cap when a provider carries dozens of per-event PPV channels.
    unique.sort(key=lambda row: (order.get(row.get("match_kind"), 3),
                                 0 if row.get("driver_hit") else 1,
                                 str(row.get("category") or ""),
                                 str(row.get("xtream_name") or "")))
    # The cap keeps the list readable, but a channel named after a driver the
    # user follows is exactly what they came for, so those are never dropped:
    # they take the first slots and the rest of the list fills in behind them.
    driver_rows = [row for row in unique if row.get("driver_hit")]
    if driver_rows:
        others = [row for row in unique if not row.get("driver_hit")]
        return (driver_rows + others)[:30]
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
 .mobiletop,.mobilenav,.mobilemorebackdrop{display:none}
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
 #teamFixtures>.card{flex:0 0 min(480px,92vw);margin:0;scroll-snap-align:start}
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
 .matchfixture .chbtns{gap:4px}.matchfixture .btnplay,.matchfixture .btnvlc{padding-left:8px;padding-right:8px;margin-right:0}
 .fixturechannelresults .racingeventchannel,.matchfixture .chline{display:grid;grid-template-columns:22px minmax(0,1fr);align-items:center;column-gap:8px;row-gap:6px}
 .fixturechannelresults .racingeventchannel>.chn,.matchfixture .chline>.matchchan{grid-column:2;min-width:0;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}
 .fixturechannelresults .racingeventchannel>.chanlogo,.matchfixture .chline>.matchchan>.chanlogo{grid-column:1;grid-row:1}
 .fixturechannelresults .racingeventchannel>.chbtns,.matchfixture .chline>.chbtns{grid-column:2;grid-row:2;justify-content:flex-start}
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
 .mydashsportevents{display:flex;flex-direction:column;align-items:stretch;justify-self:end;width:100%;min-width:0;gap:4px}
 .mydashsporteventline{display:flex;align-items:baseline;justify-content:center;justify-self:end;width:100%;gap:6px;min-width:0;text-align:center}
 .mydashsporteventline .mydashsportnext{flex:0 1 auto}
 .mydashsporteventline .mydashsportcount{flex:0 0 auto;white-space:nowrap}
 .mydashf1names{display:flex;flex-direction:column;gap:5px;min-width:0;align-self:start}
 .mydashf1names .mydashsportname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .mydashf1card .mydashsporteventline{align-self:center}
 .mydashsportrace{padding-top:4px;border-top:1px solid var(--line);color:var(--mut);font-size:11px}
 .mydashsportrace .mydashsportnext{font-size:11px;color:var(--mut)}
 .mydashsportrace .mydashsportcount{font-size:11px}
 .mydashsporteventmeta{font-size:10px;color:var(--mut);line-height:1.25;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
 .mylisttimelinebody{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:14px 16px}
 .mylisttimelinebody .teamfixture{border:0;background:transparent;padding:0}
 .mylisttimelineepisode{display:flex;align-items:center;gap:10px;cursor:pointer}
 .mylisttimelineepisode img{width:46px;height:69px;object-fit:cover;border-radius:5px;flex:0 0 46px} .mylisttimelineposter>img{width:48px;height:68px;object-fit:cover;border-radius:5px;justify-self:center} .mylisttimelineposter{cursor:pointer} .tlavail{margin:0;display:inline-block} .tlunavail{font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:5px;padding:2px 8px} .mylisttimelineaside .btnvlc{margin-top:2px}
 .mylisttimelineavail{align-self:center;justify-self:center}
 .mylisttimelinegame{cursor:pointer}
 .mylisttimelinef1{cursor:pointer}
 .mylisttimelinegame>img{width:72px;height:52px;object-fit:cover;border-radius:6px}
 .mylisttimelinecontent{display:grid;grid-template-columns:50px 72px 150px minmax(0,1fr) 200px 130px 34px;align-items:center;column-gap:20px;min-width:0} .teamfixturerow{display:contents} .mylisttimelinetext.spread{display:contents} .tlfacts{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:2px;min-width:0} .tlfacts>.tleptitle{color:var(--fg)}
 .tlfacts>span{font-size:13.5px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%} .mylisttimelinetext{flex:1;min-width:0} .mylisttimelineaside{display:flex;flex-direction:column;align-items:flex-end;gap:3px;text-align:right;min-width:0} .mylisttimelinecount{font-size:14px;font-weight:650;color:var(--fg);white-space:nowrap;letter-spacing:.1px} .mylisttimelinecount.live{color:#ff8e94} .mylisttimelinechans{font-size:11.5px;color:var(--mut);white-space:nowrap}
 .mylisttimelinecontent>.teamfixture{flex:1;min-width:0} .teamfixturerow .teamfixtureteams{margin-bottom:0;order:2;min-width:0;font-size:19px;font-weight:700;justify-content:center;flex-wrap:nowrap;overflow:hidden} .teamfixturerow .teamfixturecompetition{margin-bottom:0;order:1;min-width:0;font-size:13.5px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap} .teamfixturerow .teamfixturewhen{order:3;min-width:0;font-size:13.5px;white-space:nowrap;text-align:center} .teamfixturerow>.teamfixturetv{order:6;justify-self:center} .teamfixturerow>.mylisttimelineaside{order:5} .teamfixturerow .teamfixturebroadcasts{grid-column:1/-1;width:100%} .mylisttimelinetext.spread .tllead{min-width:0} .mylisttimelinetext.spread .tlheadline{min-width:0;font-size:19px;font-weight:700;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap} .mylisttimelinetext.spread .tlfact{font-size:13.5px;color:var(--mut);white-space:nowrap;flex:1 1 0;min-width:0;overflow:hidden;text-overflow:ellipsis}
 .mylisttimelineart{width:58px;height:58px;flex:0 0 72px;object-fit:contain;border-radius:7px;background:#0d1014;padding:5px;box-sizing:border-box}
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
 button.headerrestart{background:#1e5f8a}
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
 .moviecatalogs.hide{display:none}
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
 .movieposter{position:relative;width:92px;height:138px;flex-shrink:0;border-radius:7px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center;color:#737b89;font-size:30px}
 .movieavail{display:block;margin:8px auto 0;width:fit-content;background:rgba(30,120,60,.94);color:#fff;font-size:10px;font-weight:700;padding:3px 9px;border-radius:5px;letter-spacing:.02em;line-height:1.3}
 .moviecard .movieinfo{display:flex;flex-direction:column;align-items:center;text-align:center}
 .moviecard .movieactions{justify-content:center}
 .moviecard .moviestar{font-size:26px;margin-right:0}
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
 .racingevent{padding:9px 0;border-top:1px solid var(--line);cursor:default}
 .racingevent:hover b{color:var(--acc)}
 .racingevent.haschannels{cursor:pointer}.racingevent.loadingchannels{cursor:wait}.racingevent.loadingchannels:hover b{color:inherit}
 .racingevent:first-of-type{border-top:0}
 .racingeventtop{display:flex;align-items:center;gap:8px}.racingeventtv{margin-left:auto;background:#17351e;border-color:#327443;color:#70d889}.racingeventloading{margin-left:auto;display:inline-flex;align-items:center;gap:6px;color:var(--mut);font-size:10px;white-space:nowrap}.racingeventspinner{width:12px;height:12px;border:2px solid #39424e;border-top-color:var(--acc);border-radius:50%;animation:racingeventspin .7s linear infinite}@keyframes racingeventspin{to{transform:rotate(360deg)}}.racingeventsource{color:var(--mut);text-decoration:none}.racingeventsource:hover{color:var(--acc);text-decoration:underline}.racingeventchannels{margin-top:9px;padding:8px;border:1px solid #294535;border-radius:7px;background:#101814}.racingeventchannels.hide{display:none}.racingeventchannel{display:flex;align-items:center;gap:8px;padding:6px 4px;border-top:1px solid rgba(255,255,255,.055);cursor:pointer;border-radius:5px}.racingeventchannel:hover{background:rgba(255,255,255,.05)}.racingeventchannel:first-child{border-top:0}.racingeventchannel .chn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.racingeventchannel .chbtns{flex:0 0 auto}
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
 @media(max-width:700px){
  html{scroll-padding-bottom:76px}
  body{font-size:15px;padding-top:54px;padding-bottom:calc(72px + env(safe-area-inset-bottom));overflow-x:hidden}
  body>header{display:none!important}
  .mobiletop{position:fixed;display:flex;align-items:center;gap:10px;top:0;left:0;right:0;height:54px;padding:6px 12px;z-index:2490;background:rgba(12,15,20,.97);border-bottom:1px solid var(--line2);box-shadow:0 5px 18px rgba(0,0,0,.25);backdrop-filter:blur(12px)}
  .mobilebrand{display:flex;align-items:center;gap:8px;min-width:0;flex:1}.mobilebrand svg{width:32px;height:32px;flex:0 0 32px}.mobilebrandtext{min-width:0}.mobilebrandtext b{display:block;font-size:14px;line-height:1.1}.mobilepagetitle{display:block;color:var(--mut);font-size:11px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .mobilemoreopen{width:42px;height:42px;min-height:42px;padding:0;border-radius:11px;background:var(--card);border:1px solid var(--line2);color:var(--fg);font-size:22px}
  .mobilenav{position:fixed;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));left:0;right:0;bottom:0;height:calc(64px + env(safe-area-inset-bottom));padding:5px 5px env(safe-area-inset-bottom);z-index:2500;background:rgba(10,13,17,.98);border-top:1px solid var(--line2);box-shadow:0 -8px 24px rgba(0,0,0,.42);backdrop-filter:blur(12px)}
  .mobilenav button{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;min-width:0;min-height:54px;padding:3px 2px;border:0;border-radius:10px;background:transparent;color:var(--mut);font-size:10px;line-height:1.05}
  .mobilenav button .mobileicon{font-size:19px;line-height:1}.mobilenav button.on{background:#16233d;color:#dce9ff;box-shadow:inset 0 0 0 1px #294c82}
  .mobilemorebackdrop{position:fixed;display:flex;align-items:flex-end;inset:0;z-index:2700;background:rgba(0,0,0,.66);padding:0}.mobilemorebackdrop.hide{display:none}
  .mobilemoresheet{width:100%;max-height:82vh;overflow:auto;padding:10px 14px calc(18px + env(safe-area-inset-bottom));border-radius:18px 18px 0 0;border:1px solid var(--line2);border-bottom:0;background:#12161c;box-shadow:0 -20px 70px rgba(0,0,0,.65)}
  .mobilemorehandle{width:42px;height:4px;border-radius:4px;background:#4b5360;margin:2px auto 13px}.mobilemorehead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.mobilemorehead b{font-size:17px}.mobilemoreclose{width:42px;height:42px;padding:0;background:var(--card);color:var(--mut);border:1px solid var(--line2);border-radius:10px;font-size:20px}
  .mobilemoregrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.mobilemoregrid button{display:flex;align-items:center;gap:10px;min-height:52px;padding:10px 12px;text-align:left;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:11px}.mobilemoregrid button span:first-child{font-size:20px}.mobilemoregrid button.on{border-color:#3967a8;background:#16233d}
  .mobilemoretools{display:flex;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}.mobilemoretools button{flex:1}.mobilemoretools .stopbtn{color:#ff8e94;border-color:#6d3035}
  main,main.wide{width:100%;max-width:none;padding:13px 10px 26px!important;overflow:hidden}
  section{min-width:0}
  input,select,textarea{font-size:16px!important;min-height:44px}
  button{min-height:42px;touch-action:manipulation}
  .globaldecor{display:none}
  #teamFixtures{display:block;overflow:visible;padding:0;scroll-snap-type:none}
  #teamFixtures>.card{width:100%;max-width:none;min-width:0;margin:0 0 12px}
  .teamtabs{flex-wrap:nowrap;overflow-x:auto;padding-bottom:4px}
  .teamtab{min-height:42px;white-space:nowrap;padding:8px 13px}
  .matchfixturehead{padding:12px 11px 10px}
  .matchfixtureteamsline{gap:6px}
  .matchfixtureteam{font-size:13px;gap:5px;flex:1}
  .matchfixtureteamlogo{width:27px;height:27px;flex-basis:27px}
  .matchfixtureavailability{font-size:9px;padding:2px 5px}
  .matchfixturebody{padding:9px}
  .bchead{min-height:46px;padding:10px;font-size:13px}
  .chline,.chrow{align-items:flex-start;gap:8px;padding:10px 7px}
  .matchchan,.chname{min-width:0;line-height:1.25}
  .chn,.fixturechanneltitle,.chname{font-size:12.5px;white-space:normal;overflow-wrap:anywhere}
  .chbtns,.chrow>div:last-child{gap:4px;flex-wrap:wrap;justify-content:flex-end}
  .btnplay,.btnvlc,.chbtns button,.favcardbtns button{min-height:36px;padding:6px 9px;font-size:11px;margin:0}
  .chanlogo{width:26px;height:26px;flex-basis:26px}
  .favstar{margin-right:4px;min-width:24px;min-height:32px;display:inline-flex;align-items:center;justify-content:center}
  .mylayout{gap:12px}.mlcats{width:100%}
  .mydash{padding:2px 0 20px}.mylistprofile{margin-bottom:18px}.mydashblock{margin-bottom:22px}
  .mydashgrid,.mydashepisodes,.mydashchannels,.moviegrid,.showgrid,.gamesgrid,.racinggrid,.racingdrivers{grid-template-columns:1fr!important}
  .mylistprofileemblem{width:48px;height:48px;flex-basis:48px}.mylistprofileemblem svg{width:48px;height:48px}.mylistprofilename{font-size:19px}
  .movieswrap,.showswrap,.teamswrap,.racinglayout{display:block}
  .moviefavs,.showfavs,.teamfavs,.racingsidebar{position:static;width:100%;max-width:none;max-height:220px;overflow:auto;margin-bottom:14px;padding:0 0 12px;border-right:0;border-bottom:1px solid var(--line)}
  .settingswrap{padding:4px 0 24px;display:block}
  .settingswrap .brandblock{display:none}
  .settingstabs{flex-wrap:nowrap;overflow-x:auto;margin:0 0 12px;padding-bottom:7px}
  .settingstab{white-space:nowrap;min-height:42px}
  .settingspanels,#settingsProfile .grid2,.settingsgroup .grid2{grid-template-columns:1fr!important}
  .settingsgroup{padding:14px 12px;border-radius:10px}
  .settingsactions{bottom:calc(64px + env(safe-area-inset-bottom));padding:9px}
  .settingsactions .push{width:100%;margin-left:0}
  .tvwrap{height:calc(100vh - 96px);min-height:420px;border-radius:8px}
  .tvrail{width:82px;padding:5px}.tvsrc{font-size:10px;padding:7px 4px}
  .tvchancol,.tvchan{width:190px}.tvplayerslot{left:190px}
  .pmodal,.tvplayerslot.mini{position:fixed;inset:54px 0 calc(64px + env(safe-area-inset-bottom));width:100vw;height:auto;border-radius:0}
  .pbox{border-radius:0;border-left:0;border-right:0}
  .setupoverlay{padding:8px 8px calc(66px + env(safe-area-inset-bottom))}
  .editprofiledialog,.setupwizard{max-height:100%;padding-left:15px;padding-right:15px}
  .updatebanner{bottom:calc(64px + env(safe-area-inset-bottom));top:auto;align-items:stretch;flex-direction:column;padding:10px 12px;text-align:center}
  main *,main.wide *{min-width:0}
  .mydash.layout-timeline{display:block!important;width:100%;max-width:100%}
  .mydash.layout-timeline .mydashteamonly{width:100%;display:grid;grid-template-columns:58px minmax(0,1fr);gap:10px;padding:10px}
  .mydash.layout-timeline .mydashteamonly>img{grid-row:1/3}
  .mydash.layout-timeline .mydashsportinfo,.mydash.layout-timeline .mydashsportsingle,.mydash.layout-timeline .mydashf1names{width:100%}
  .mydash.layout-timeline .mydashsportevent{width:100%;max-width:100%;flex-basis:auto;padding:5px 0 0;grid-column:2}
  .mydashsportsingletop{grid-template-columns:1fr;gap:4px}
  .mydashsporteventline{justify-content:flex-start;text-align:left}
  .mydashsportphotos{grid-column:1}.mydashsportnames{min-width:0}
  .playlistsearch{padding:13px 12px}
  .playlistsearch .row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}
  .ch4,.ch4cats,.ch4group,.ch4col{display:block;width:100%;max-width:100%;height:auto;min-height:0}
  .ch4cats{padding:0}
  #catlist{display:block;max-height:52vh;padding:9px;overflow-y:auto}
  .catitem{min-height:38px;width:100%;padding:7px 3px}
  .ch4group{margin-top:14px;overflow:visible}
  .ch4col{padding:13px 11px;border-left:0!important;border-top:1px solid var(--line)}
  .ch4col:first-child{border-top:0}
  .pcol{max-height:48vh}
  .tvguidehead{height:52px}.tvchancol{width:100%;border-right:0}.tvtimeline{display:none}
  .tvrail{width:92px;flex-basis:92px}.tvguide{width:calc(100% - 92px)}
  .tvchan{width:100%;padding:6px;gap:5px}.tvchan .tvvlc{padding:5px 7px;min-height:38px}.tvchan .tvflag{display:none}.tvchan .favstar{margin-left:auto}
  .tvprog{display:none}.tvrow{height:58px;min-height:58px}.tvplayerslot{left:0}
  .teamswrap,.teamsmain,#teamFixtures,.teamfixturegrid,.topfixturegrid{width:100%;max-width:100%;padding-left:0;padding-right:0}
  .teamfixture,.teamfixturebroadcasts,.matchfixture,.matchfixturebody,.bcastlist,.bcrow{width:100%;max-width:100%}
  .teamfixturebroadcasts{display:block}.teamfixturebroadcasts>.bcrow{margin-bottom:7px}
  .teamfixtureteams{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start}
  .teamfixtureside{overflow:hidden}.teamfixtureside span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .chline{display:grid;grid-template-columns:minmax(0,1fr) auto;width:100%}.chline .matchchan{overflow:hidden}
  .showfavs{max-height:none!important;overflow:visible}
  .showfav{display:grid;grid-template-columns:76px minmax(0,1fr) 42px;align-items:center;gap:12px;min-height:116px;padding:9px 0}
  .showfavposter{width:76px;height:114px}.showfavinfo{padding:0;justify-content:flex-start}.showfavname{text-align:left;font-size:15px}
  .showremove{position:static;grid-column:3;grid-row:1;align-self:center;width:42px;height:42px;padding:0}
  .racinglayout{padding:0}.racingdetailhero{grid-template-columns:90px minmax(0,1fr)}.racingdetailhero>img{width:90px;height:120px}
  .racingdetailpeople{grid-template-columns:1fr}.racingdetailnextgrid{grid-template-columns:1fr}
  .settingswrap,.settingscard,.settingspanels,.settingspanel,.settingsgroup{width:100%;max-width:100%;overflow-wrap:anywhere}
  .settingsgroup input,.settingsgroup select{max-width:100%}
 }
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
  <button type="button" id="restartBtn" class="stopbtn headerstop headerrestart hide" onclick="restartTVMate()" data-i18n="Restart TVMate" title="Restart TVMate (reload edited tvmate.py)">Restart TVMate</button>
  <button type="button" class="stopbtn headerstop" onclick="stopTVMate()" data-i18n="Stop TVMate" title="Stop TVMate">Stop TVMate</button>
  <div class="langsel">
    <button class="langflag on" id="langEN" onclick="setLang('en')" title="English">&#127468;&#127463;</button>
    <button class="langflag" id="langNO" onclick="setLang('no')" title="Norsk">&#127475;&#127476;</button>
  </div>
</header>
<div class="mobiletop">
 <div class="mobilebrand"><svg viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg"><rect x="26" y="58" width="150" height="120" rx="16" fill="#3a2c1f"/><rect x="38" y="70" width="126" height="96" rx="8" fill="#1b3a6b"/><ellipse cx="101" cy="140" rx="44" ry="11" fill="#e7a94e"/><ellipse cx="101" cy="128" rx="42" ry="11" fill="#f0b95e"/><ellipse cx="101" cy="116" rx="40" ry="11" fill="#f5c56e"/></svg><div class="mobilebrandtext"><b>Olo's TVMate</b><span id="mobilePageTitle" class="mobilepagetitle">Profile</span></div></div>
 <button class="mobilemoreopen" type="button" onclick="openMobileMore()" aria-label="More">&#8942;</button>
</div>
<nav class="mobilenav" aria-label="Mobile navigation">
 <button id="mobileNavMylist" onclick="showMylist()"><span class="mobileicon">&#8962;</span><span data-i18n="Profile">Profile</span></button>
 <button id="mobileNavChannels" onclick="showChannels()"><span class="mobileicon">&#9776;</span><span data-i18n="Playlists">Playlists</span></button>
 <button id="mobileNavMytv" onclick="showMytv()"><span class="mobileicon">&#9654;</span><span data-i18n="Live TV">Live TV</span></button>
 <button id="mobileNavTeams" onclick="showTeams()"><span class="mobileicon">&#9917;</span><span data-i18n="Sports">Sports</span></button>
 <button id="mobileNavMore" onclick="openMobileMore()"><span class="mobileicon">&#8226;&#8226;&#8226;</span><span>More</span></button>
</nav>
<div id="mobileMore" class="mobilemorebackdrop hide" onclick="if(event.target===this)closeMobileMore()">
 <div class="mobilemoresheet" role="dialog" aria-modal="true" aria-label="More navigation">
  <div class="mobilemorehandle"></div><div class="mobilemorehead"><b>More</b><button class="mobilemoreclose" onclick="closeMobileMore()">&#10005;</button></div>
  <div class="mobilemoregrid">
   <button data-mobile-target="navMovies" onclick="mobileGo(showMovies)"><span>&#127916;</span><span data-i18n="Movies">Movies</span></button>
   <button data-mobile-target="navShows" onclick="mobileGo(showShows)"><span>&#128250;</span><span data-i18n="Shows">Shows</span></button>
   <button data-mobile-target="navGames" onclick="mobileGo(showGames)"><span>&#127918;</span><span data-i18n="Games">Games</span></button>
   <button data-mobile-target="navRacing" onclick="mobileGo(showRacing)"><span>&#127950;</span><span data-i18n="Racing">Racing</span></button>
   <button data-mobile-target="navSettings" onclick="mobileGo(showSettings)"><span>&#9881;</span><span data-i18n="Settings">Settings</span></button>
   <button data-mobile-target="navMytimeline" onclick="mobileGo(showMytimeline)"><span>&#128336;</span><span data-i18n="Timeline">Timeline</span></button>
  </div>
  <div class="mobilemoretools"><button onclick="setLang('en')">English</button><button onclick="setLang('no')">Norsk</button><button onclick="location.href='/desktop'">Desktop</button><button class="stopbtn" onclick="stopTVMate()" data-i18n="Stop TVMate">Stop TVMate</button></div>
 </div>
</div>
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
          <div><label data-i18n="Profile name">Profile name</label><input id="s_profile" type="text" maxlength="40"></×ß<Ó†òµë(š+myÖçW6÷W&6T'VffW#§G'VRÆWFô6ÆVçWÖ„&6·v&DGW&F–öã£cÆWFô6ÆVçWÖ–ä&6·v&DGW&F–öã£3Ò“°¢G5Æ–W"æGF6„ÖVF–VÆVÖVçB‡f–FVò“·V&Æ—6‚‚“°¢ÆWBf–ÆVCÖfÇ6S°¢G5Æ–W"æöâ†×VwG2äWfVçG2äU%$õ"ÆgVæ7F–öâ‚—¶–b†f–ÆVGÇÇ7F÷VB—&WGW&ã¶f–ÆVC×G'VS·7FGW2‚t6÷VÆBæ÷BÆ’F†—27G&VÒ–âF†R'&÷w6W"âG'’dÄ2âr“·Ò“°¢G5Æ–W"æÆöB‚“°¢6öç7BÆ–VC×G5Æ–W"çÆ’‚“¶–b‡Æ–VBbgÆ–VBæ6F6‚—Æ–VBæ6F6‚‚‚“Óç·Ò“°¢f–FVòæFDWfVçDÆ—7FVæW"‚wÆ––ærrÆgVæ7F–öâöåG5Æ––ær‚—·f–FVòç&VÖ÷fTWfVçDÆ—7FVæW"‚wÆ––ærrÆöåG5Æ––ær“·7FGW2‚rr“·ÒÇ¶öæ6S§G'VWÒ“°¢Ö6F6‚†R—·7FGW2‚t6÷VÆBæ÷BÆ’F†—27G&VÒ–âF†R'&÷w6W"âG'’dÄ2âr“·Ð¢Ð¢gVæ7F–öâ7F'D†Ç2‡7&2Çf–&÷‡’—°¢6ÆV"‚“·V&Æ—6‚‚“¶–b‡7F÷VB—&WGW&ã°¢–b‡v–æF÷rä†Ç2bd†Ç2æ—57W÷'FVB‚’—°¢7FGW2‡f–&÷‡“ò‡W&Ç2ç&VÆ“òu&VÆ––ær6V7W&VÇ’F‡&÷Vv‚†öÖRâââs¢u&÷WF–ær„Å2F‡&÷Vv‚Æö6Â&VÆ’âââr“¢tÆöF–ær„Å2âââr“°¢†Ç3ÖæWr†Ç2‡¶Öæ–fW7DÆöF–æuF–ÖT÷WC£#ÆÆWfVÄÆöF–æuF–ÖT÷WC£#Æg&tÆöF–æuF–ÖT÷WC£#Æ&6´'VffW$ÆVæwFƒ£3ÆÖ„'VffW$ÆVæwFƒ£CWÒ“·V&Æ—6‚‚“°¢†Ç2æöâ„†Ç2äWfVçG2äU%$õ"ÆgVæ7F–öâ†WbÆFF—°¢–b‡7F÷VGÇÂFFæfFÂ—&WGW&ã°¢–b†FFçG—SÓÓÔ†Ç2äW'&÷%G—W2äÔTD”ôU%$õ"bfÖVF–&V6÷fW&–W3Ã"—¶ÖVF–&V6÷fW&–W2²³·7FGW2‚u&V6÷fW&–ær'&÷w6W"Æ–&6²âââr“·G'—¶†Ç2ç&V6÷fW$ÖVF–W'&÷"‚“·&WGW&ã·Ö6F6‚†R—·×Ð¢–b‚f–&÷‡’—·7F'D†Ç2‚rö’÷&÷‡“÷SÒr¶Væ6öFUU$”6ö×öæVçB‡W&Ç2æ†Ç2’ÇG'VR“·&WGW&ã·Ð¢7F'EG2‡G'VR“°¢Ò“°¢†Ç2æöâ„†Ç2äWfVçG2äÔä”dU5Eõ%4TBÆgVæ7F–öâ‚—·7FGW2‚rr“¶6öç7B×f–FVòçÆ’‚“¶–b‡bgæ6F6‚—æ6F6‚‚‚“Óç·Ò“·Ò“°¢†Ç2æÆöE6÷W&6R‡7&2“¶†Ç2æGF6„ÖVF–‡f–FVò“·&WGW&ã°¢Ð¢–b‡f–FVòæ6åÆ•G—R‚vÆ–6F–öâ÷fæBæÆRæ×VwW&Âr’—°¢7FGW2‚tÆöF–ær„Å2âââr“·f–FVòç7&3×7&3°¢6öç7BæF—fTW'&÷#ÖgVæ7F–öâ‚—·f–FVòç&VÖ÷fTWfVçDÆ—7FVæW"‚vW'&÷"rÆæF—fTW'&÷"“¶–b‚f–&÷‡’—7F'D†Ç2‚rö’÷&÷‡“÷SÒr¶Væ6öFUU$”6ö×öæVçB‡W&Ç2æ†Ç2’ÇG'VR“¶VÇ6R7F'EG2‡G'VR“·Ó°¢f–FVòæFDWfVçDÆ—7FVæW"‚vW'&÷"rÆæF—fTW'&÷"Ç¶öæ6S§G'VWÒ“°¢6öç7B×f–FVòçÆ’‚“¶–b‡bgæ6F6‚—æ6F6‚‚‚“Óç·Ò“·&WGW&ã°¢Ð¢7F'EG2‡G'VR“°¢Ð¢7F'D†Ç2‡W&Ç2æ†Ç2ÂW&Ç2ç&VÆ’“°¢&WGW&â·7F÷¦gVæ7F–öâ‚—·7F÷VC×G'VS¶6ÆV"‚“·V&Æ—6‚‚“·G'—·f–FVòçW6R‚“·f–FVòç&VÖ÷fTGG&–'WFR‚w7&2r“·f–FVòæÆöB‚“·Ö6F6‚†R—·××Ó°§Ð¦7–æ2gVæ7F–öâÆ”'&÷w6W"‡6–BÆæÖR—°¢6öç7BÖöFÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wÆ–W$ÖöFÂr“°¢6öç7Bf–FVóÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wf–FVòr“°¢6öç7B×6sÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w×6rr“°¢6öç7B6–D¶W“Õ7G&–ær‡6–B“°¢–b…ö'&÷w6W%VæF–æu6–CÓÓ×6–D¶W’—&WGW&ã°¢òòöæÇ’öæR7G&VÒ6†÷VÆBWfW"&RÆ––ærâ÷Væ–ærF†R÷WÆ–W"Çv—0¢òòFV'2F÷vâç’Æ—fREbÆ–&6²f—'7BÂ÷F†W'v—6R&÷F‚¶VW'Vææ–ærà¢–b…÷GeÆ––ærÓÖçVÆÇÇÇv–æF÷rå÷GeÆ–&6´6öçG&öÆÆW"—·G'—·Ge7F÷‚“·Ö6F6‚†R—·×Ð¢ö'&÷w6W%VæF–æu6–C×6–D¶W“°¢6öç7B&WVW7CÒ²µö'&÷w6W%Æ•&WVW7C°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wF—FÆRr’çFW‡D6öçFVçCÖæÖWÇÂuÆ–W"s°¢×6rçFW‡D6öçFVçCÒtÆöF–ærâââs°¢ÖöFÂæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢6WE÷WÆ–W$Ö‚†fÇ6R“°¢Fö7VÖVçBæ&öG’æ6Æ74Æ—7BæFB‚wGg6V7F–öçÆ’r“°¢7–æ56V7F–öåÆ–W$Æ–÷WB‚“°¢òòvWBF†R†Ç2W&À¢–b†ÖöFÂå÷Æ–&6´6öçG&öÆÆW"—¶ÖöFÂå÷Æ–&6´6öçG&öÆÆW"ç7F÷‚“¶ÖöFÂå÷Æ–&6´6öçG&öÆÆW#ÖçVÆÃ·Ð¢–b…ö†Ç2—·G'—µö†Ç2æFW7G&÷’‚“·Ö6F6‚†R—·Õö†Ç3ÖçVÆÃ·Ö–b…ö×VwG2—¶FW7G&÷”×VwG5Æ–W"…ö×VwG2“µö×VwG3ÖçVÆÃ·Ð¢ÆWBW&Ç3°¢G'—·W&Ç3Öv—B’‚rö’ö†Ç3ö–CÒr¶Væ6öFUU$”6ö×öæVçB‡6–B’“¶–b‡W&Ç2æW'&÷'ÇÂW&Ç2æ†Ç2—F‡&÷ræWrW'&÷"‚w7G&VÒW&Âr“·Ö6F6‚†R—¶–b‡&WVW7CÓÓÕö'&÷w6W%Æ•&WVW7B–×6rçFW‡D6öçFVçCÒt6÷VÆBæ÷B'V–ÆB7G&VÒU$Ââs·&WGW&ã·Öf–æÆÇ—¶–b‡&WVW7CÓÓÕö'&÷w6W%Æ•&WVW7B•ö'&÷w6W%VæF–æu6–CÒrs·Ð¢–b‡&WVW7BÓÕö'&÷w6W%Æ•&WVW7B—&WGW&ã°¢6öç7B6öçG&öÆÆW#×7F'E6Ö'E7G&VÒ‡f–FVòÇW&Ç2Ç3Óæ×6rçFW‡D6öçFVçC×2ÆgVæ7F–öâ†‚ÇB—µö†Ç3Öƒµö×VwG3×C·Ò“°¢ÖöFÂå÷Æ–&6´6öçG&öÆÆW#Ö6öçG&öÆÆW#°§Ð¦gVæ7F–öâÆ–W$gVÆÇ67&VVäVÆVÖVçB‚—°¢&WGW&âFö7VÖVçBægVÆÇ67&VVäVÆVÖVçGÇÆFö7VÖVçBçvV&¶—DgVÆÇ67&VVäVÆVÖVçGÇÆçVÆÃ°§Ð¦gVæ7F–öâFövvÆT'&öF67FW$6æF–FFW2†'Fâ—°¢6öç7B&÷ƒÖ'Fâç&VçDVÆVÖVçBÆW‡G&3Ö&÷ƒö&÷‚çVW'•6VÆV7F÷$ÆÂ‚ræ&66†æW‡G&r“¥µÓ°¢–b‚W‡G&2æÆVæwF‚—&WGW&ã°¢6öç7B÷Væ–æsÖW‡G&5³Òæ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr“°¢W‡G&2æf÷$V6‚†VÃÓæVÂæ6Æ74Æ—7BçFövvÆR‚v†–FRrÂ÷Væ–ær’“°¢'FâçFW‡D6öçFVçCÖ÷Væ–æs÷G"‚u6†÷rfWvW"6†ææVÇ2r“¢‡G"‚u6†÷rÖ÷&R6†ææVÇ2r’²r‚r¶'FâæFF6WBæÖ÷&R²r’r“°§Ð¦gVæ7F–öâ7–æ56V7F–öåÆ–W$Æ–÷WB‚—°¢òòf÷&6RF†RÇ&VG’Ö÷Vâ6V7F–öâFòF÷B—G26öç7G&–æVBÆ–W"Æ–÷WBöà¢òòF†Rf—'7Bg&ÖRâ&Wf–÷W6Ç’F†—2†VæVB&VÆ–&Ç’öæÇ’gFW"æf–vF–æp¢òòv’æB&6²ÂW7V6–ÆÇ’B“#ƒƒà¢fö–BFö7VÖVçBæ&öG’æöfg6WEv–GFƒ°¢&WVW7Dæ–ÖF–öäg&ÖR‚‚“Óç&WVW7Dæ–ÖF–öäg&ÖR‚‚“Óç°¢–b‚&6–æuf–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’—°¢6öç7BG&—fW'3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætG&—fW'2r“°¢–b†G&—fW'2–G&—fW'2æ–ææW$…DÔÃ×&6–ætG&—fW'4‡FÖÂ…÷&6–ætG&—fW%&÷w2Å÷&6–ætWfVçE&÷w2“°¢&VæFW%&6–æu66†VGVÆT6&G2‚“°¢Ð¢v–æF÷ræF—7F6„WfVçB†æWrWfVçB‚w&W6—¦Rr’“°¢Ò’“°§Ð¦gVæ7F–öâ&WVW7EÆ–W$gVÆÇ67&VVâ†VÂ—°¢–b‚VÂ—&WGW&âfÇ6S°¢6öç7BfãÖVÂç&WVW7DgVÆÇ67&VVçÇÆVÂçvV&¶—E&WVW7DgVÆÇ67&VVã°¢–b‚fâ—&WGW&âfÇ6S°¢G'—¶6öç7BÖfâæ6ÆÂ†VÂ“¶–b‡bgæ6F6‚—æ6F6‚‚‚“Óç·Ò“·&WGW&âG'VS·Ö6F6‚†R—·&WGW&âfÇ6S·Ð§Ð¦gVæ7F–öâW†—EÆ–W$gVÆÇ67&VVâ‚—°¢6öç7BfãÖFö7VÖVçBæW†—DgVÆÇ67&VVçÇÆFö7VÖVçBçvV&¶—DW†—DgVÆÇ67&VVã°¢–b‚fâ—&WGW&âfÇ6S°¢G'—¶6öç7BÖfâæ6ÆÂ†Fö7VÖVçB“¶–b‡bgæ6F6‚—æ6F6‚‚‚“Óç·Ò“·&WGW&âG'VS·Ö6F6‚†R—·&WGW&âfÇ6S·Ð§Ð¦gVæ7F–öâ6WE÷WÆ–W$Ö‚†Ö†–Ö—¦VB—°¢6öç7BÖöFÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wÆ–W$ÖöFÂr’Æ'FãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wÖ–ä'Fâr’Æ†—CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wf–FVô†—Br“°¢ÖöFÂæ6Æ74Æ—7BçFövvÆR‚w6V7F–öæÖ‚rÂÖ†–Ö—¦VB“°¢6öç7BÆ&VÃÖÖ†–Ö—¦VCòtW†—BgVÆÇ67&VVâs¢tgVÆÇ67&VVâÆ–W"s°¢–b†'Fâ—¶'FâçF—FÆSÖÆ&VÃ¶'Fâç6WDGG&–'WFR‚v&–ÖÆ&VÂrÆÆ&VÂ“¶'FâçFW‡D6öçFVçCÖÖ†–Ö—¦VCòuÇS#“‚s¢uÇS#“bs·Ð¢–b††—B–†—Bç6WDGG&–'WFR‚v&–ÖÆ&VÂrÆÆ&VÂ“°§Ð¦gVæ7F–öâFövvÆU÷WÆ–W%6—¦R‚—°¢6öç7BÖöFÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wÆ–W$ÖöFÂr“°¢–b‡Æ–W$gVÆÇ67&VVäVÆVÖVçB‚“ÓÓÖÖöFÂ—·6WE÷WÆ–W$Ö‚†fÇ6R“¶W†—EÆ–W$gVÆÇ67&VVâ‚“·&WGW&ã·Ð¢6WE÷WÆ–W$Ö‚‡G'VR“°¢&WVW7EÆ–W$gVÆÇ67&VVâ†ÖöFÂ“°§Ð¦gVæ7F–öâ7–æ5Æ–W$gVÆÇ67&VVäW†—B‚—°¢–b‡Æ–W$gVÆÇ67&VVäVÆVÖVçB‚’—&WGW&ã°¢6öç7BÖöFÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wÆ–W$ÖöFÂr“°¢–b†ÖöFÂbfÖöFÂæ6Æ74Æ—7Bæ6öçF–ç2‚w6V7F–öæÖ‚r’—6WE÷WÆ–W$Ö‚†fÇ6R“°¢6öç7B6Æ÷CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGeÆ–W%6Æ÷Br“°¢–b‡6Æ÷Bbg6Æ÷Bæ6Æ74Æ—7Bæ6öçF–ç2‚w6V7F–öæÖ‚r’bf×—Gef–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’—Ge6WDÖ–æ’‡G'VR“°§Ð¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚vgVÆÇ67&VVæ6†ævRrÇ7–æ5Æ–W$gVÆÇ67&VVäW†—B“°¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚wvV&¶—FgVÆÇ67&VVæ6†ævRrÇ7–æ5Æ–W$gVÆÇ67&VVäW†—B“°¦gVæ7F–öâ6Æ÷6UÆ–W"‚—°¢ö'&÷w6W%Æ•&WVW7B²³µö'&÷w6W%VæF–æu6–CÒrs°¢6öç7BÖöFÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wÆ–W$ÖöFÂr“°¢6öç7Bf–FVóÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wf–FVòr“°¢–b†ÖöFÂå÷Æ–&6´6öçG&öÆÆW"—¶ÖöFÂå÷Æ–&6´6öçG&öÆÆW"ç7F÷‚“¶ÖöFÂå÷Æ–&6´6öçG&öÆÆW#ÖçVÆÃ·Ð¢–b…ö†Ç2—·G'—µö†Ç2æFW7G&÷’‚“·Ö6F6‚†R—·Õö†Ç3ÖçVÆÃ·Ö–b…ö×VwG2—¶FW7G&÷”×VwG5Æ–W"…ö×VwG2“µö×VwG3ÖçVÆÃ·Ð¢f–FVòçW6R‚“·f–FVòç&VÖ÷fTGG&–'WFR‚w7&2r“·f–FVòæÆöB‚“°¢ÖöFÂæ6Æ74Æ—7BæFB‚v†–FRr“°¢ÖöFÂæ6Æ74Æ—7Bç&VÖ÷fR‚w6V7F–öæÖ‚r“°¢6öç7BÆ—fTv“Ò…÷GeÆ––ærÓÖçVÆÇÇÇv–æF÷rå÷GeÆ–&6´6öçG&öÆÆW"’bf×—Gef–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr“°¢–b‚Æ—fTv’–Fö7VÖVçBæ&öG’æ6Æ74Æ—7Bç&VÖ÷fR‚wGg6V7F–öçÆ’r“°§Ð¦7–æ2gVæ7F–öâÆ•dÄ2‡6–BÆ'Fâ—°¢6öç7BöÆCÖ'Fãö'FâçFW‡D6öçFVçC¢rs°¢–b†'Fâ—¶'FâçFW‡D6öçFVçCÒt÷Væ–ærâââs·Ð¢G'—°¢6öç7B£Öv—B’‚rö’÷Æ’rÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡·7G&VÕö–C§6–GÒ—Ò“°¢–b†¢æW'&÷"—¶ÆW'B†¢æW'&÷'ÇÂt6÷VÆBæ÷BÆVæ6‚dÄ2âr“·Ð¢VÇ6R–b†¢çÆ–Æ—7B—·v–æF÷ræÆö6F–öâæ‡&VcÖ¢çÆ–Æ—7C·Ð¢Ö6F6‚†R—¶ÆW'B‚t6÷VÆBæ÷BÆVæ6‚dÄ2âr“·Ð¢–b†'Fâ—·6WEF–ÖV÷WB‚‚“Óç¶'FâçFW‡D6öçFVçCÖöÆC·ÒÃ#“·Ð§Ð¦ÆWBöfdÖ÷f–U6WCÖæWr6WB‚“°¦gVæ7F–öâ6ÆVäÖ÷f–U6V&6…F—FÆR†æÖR—°¢&WGW&â7G&–ær†æÖWÇÂrr¢ç&WÆ6R‚õåÅÇ2¢â³õÅÇ2²ÕÅÇ2²òÂrr¢ç&WÆ6R‚õåÅÇ2¢ç³ÃCÓõÅÇ2¥ÅÇÅÅÇ2¢òÂrr¢ç&WÆ6R‚õÅÇ2¥ÅÂ‚ƒó¥U7ÅT·Ät'Ää÷ÄTçÅ4WÄD·Äd’•ÅÂ•ÅÇ2¢Bö’Ârr¢ç&WÆ6R‚õÅÇ2¥ÅÂ‚ƒó£—Ã#•ÅÆG³'ÕÅÂ•ÅÇ2¢BòÂrr¢çG&–Ò‚“°§Ð¦gVæ7F–öâÖ÷f–T6&B†ÒÇ6†÷u–V"Ç&V6VçBÆF—66÷fW"—°¢6öç7B6–CÖW64GG"…7G&–ær†Òç7G&VÕö–CÓÖçVÆÃòrs¦Òç7G&VÕö–B’’ÂW‡CÖW64GG"†ÒæW‡FVç6–öçÇÂv×Br’Â¶W“Õ7G&–ær†Òæ6FÆöuö–GÇÆÒç7G&VÕö–GÇÂrr“°¢6öç7BfcÕöfdÖ÷f–U6WBæ†2†¶W’“òröâs¢rs°¢6öç7BfeF—FÆSÕöfdÖ÷f–U6WBæ†2†¶W’“÷G"‚u&VÖ÷fRg&öÒff÷&—FW2r“§G"‚tFBFòff÷&—FW2r“°¢òò&V6VçBäBF—66÷fW"6&G2&R&'&÷w6R"6&G3¢6Æ–6¶–ær6V&6†W2–÷W"•E`¢òòf÷"F†BF—FÆRâöæÇ’7GVÂ6V&6‚&W7VÇG2vWBdÄ2÷Æ’'WGFöâà¢6öç7B'&÷w6S×&V6VçGÇÆF—66÷fW#°¢6öç7BF—7Æ”æÖSÖ'&÷w6Sö6ÆVäÖ÷f–U6V&6…F—FÆR†ÒææÖR“¦ÒææÖS°¢6öç7B÷7FW#ÖÒæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†Òæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VçDVÆVÖVçBçFW‡D6öçFVçCÕ7G&–æræg&öÔ6öFUö–çBƒ#s“b’#âs¢rb3#s“c²s°¢ÆWBÖWFÒrs°¢–b‡6†÷u–V"bfÒç–V"–ÖWF³ÖW62†Òç–V"“°¢–b†Òç&F–ær–ÖWF³Ò†ÖWFòrfæ'7²s¢rr’²u&F–æs¢r¶W62†Òç&F–ær“°¢6öç7B6&D6Æ73ÒvÖ÷f–V6&Br²†'&÷w6Sòr&V6VçFÖ÷f–Rs¢rr“°¢6öç7B6&DFFÖ'&÷w6SòrFF×VW'“Ò"r¶W64GG"†6ÆVäÖ÷f–U6V&6…F—FÆR†ÒææÖR’’²r"s¢rs°¢6öç7Bf–Ä&FvSÒ†'&÷w6RbfÒç7G&VÕöf÷VæB“òsÇ7â6Æ73Ò&Ö÷f–Vf–Â"F—FÆSÒ"r¶W64GG"‡G"‚tf–Æ&ÆR–â–÷W"•Ebr’’²r#âb33²r·G"‚tf–Æ&ÆRr’²sÂ÷7ãâs¢rs°¢&WGW&âsÆF—b6Æ73Ò"r¶6&D6Æ72²r"r¶6&DFF²sãÆF—b6Æ73Ò&Ö÷f–W÷7FW"#âr·÷7FW"²sÂöF—cãÆF—b6Æ73Ò&Ö÷f–V–æfò#ãÆF—b6Æ73Ò&Ö÷f–WF—FÆR#âr¶W62†F—7Æ”æÖR’²sÂöF—câp¢²†ÖWFòsÆF—b6Æ73Ò&Ö÷f–VÖWF#âr¶ÖWF²sÂöF—câs¢rr¢²sÆF—b6Æ73Ò&Ö÷f–V7F–öç2#ãÇ7â6Æ73Ò&fg7F"Ö÷f–W7F"r¶fb²r"FFÖ¶W“Ò"r¶W64GG"†¶W’’²r"FFÖ6FÆösÒ"r¶W64GG"†Òæ6FÆöuö–GÇÂrr’²r"FF×6–CÒ"r·6–B²r"FFÖæÖSÒ"r¶W64GG"†ÒææÖWÇÂrr’²r"FFÖW‡CÒ"r¶W‡B²r"FF×–V#Ò"r¶W64GG"†Òç–V'ÇÂrr’²r"FF×&F–æsÒ"r¶W64GG"†Òç&F–æwÇÂrr’²r"FFÖ6÷fW#Ò"r¶W64GG"†Òæ6÷fW'ÇÂrr’²r"F—FÆSÒ"r¶W64GG"†feF—FÆR’²r#âb3“s33³Â÷7ãâp¢²†'&÷w6Sòrs¢†Òç7G&VÕöf÷VæCòsÆ'WGFöâ6Æ73Ò&'FçfÆ2Ö÷f–WfÆ2"FF×6–CÒ"r·6–B²r"FFÖW‡CÒ"r¶W‡B²r#âb3“cSƒ²dÄ3Âö'WGFöãâs¢sÆ'WGFöâ6Æ73Ò&v†÷7B"F—6&ÆVCâr·G"‚tæ÷Bf–Æ&ÆRr’²sÂö'WGFöãâr’’²sÂöF—câp¢¶f–Ä&FvR²sÂöF—cãÂöF—câs°§Ð¦7–æ2gVæ7F–öâÆöE&V6VçDÖ÷f–W2†Æ–Ö—B—°¢Æ–Ö—CÖÆ–Ö—GÇÃ“°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&V6VçDÖ÷f–TÆ—7Br“°¢6öç7BÖ÷&SÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&V6VçDÖ÷f–TÖ÷&Rr“°¢VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äÆöF–ærââãÂ÷7ãâs°¢Ö÷&Ræ6Æ74Æ—7BæFB‚v†–FRr“°¢6öç7B#Öv—B’‚rö’÷&V6VçEöÖ÷f–W3öÆ–Ö—CÒr¶Æ–Ö—B“°¢–b‡G—Vöb"æÆövvVEö–ãÓÓÒv&ööÆVâr—6WDÖ÷f–U&÷f–FW$Æ–÷WB‡"æÆövvVEö–â“°¢–b‡"æW'&÷"—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#ä6÷VÆBæ÷BÆöB&V6VçFÇ’FFVBÖ÷f–W2ãÂ÷7ãâs·&WGW&âfÇ6S·Ð¢–b‚"æÆövvVEö–â—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äÆör–âf–6WGF–æw2f—'7BãÂ÷7ãâs·&WGW&âfÇ6S·Ð¢–b‚"æÖ÷f–W2æÆVæwF‚—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äæò&V6VçBÖ÷f–W2f÷VæBãÂ÷7ãâs·&WGW&âfÇ6S·Ð¢v—BÆöDÖ÷f–Tff÷&—FW2‚“°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&Ö÷f–Vw&–B"7G–ÆSÒ&Ö&v–â×F÷£#âr·"æÖ÷f–W2æÖ†ÓÓæÖ÷f–T6&B†ÒÆfÇ6RÇG'VR’’æ¦ö–â‚rr’²sÂöF—câs°¢–b†Æ–Ö—CÃ3bbg"æ†5öÖ÷&R–Ö÷&Ræ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢&WGW&âG'VS°§Ð¦ÆWBöÖ÷f–T6FÆösÒw÷VÆ"rÅöÖ÷f–T6FÆöt66†S×·Ó°¦gVæ7F–öâ6WDÖ÷f–U&÷f–FW$Æ–÷WB†ÆövvVD–â—°¢6öç7B6FÆöw3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–T6FÆöw2r“°¢–b†6FÆöw2–6FÆöw2æ6Æ74Æ—7BçFövvÆR‚væ÷‡G&VÒrÂÆövvVD–â“°¢6öç7B&Vg&W6ƒÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–U&Vg&W6„'Fâr“°¢–b‡&Vg&W6‚—&Vg&W6‚æ6Æ74Æ—7BçFövvÆR‚v†–FRrÂÆövvVD–â“°§Ð¦7–æ2gVæ7F–öâÆöD6–æVÖWFÖ÷f–W2†6FÆör—°¢öÖ÷f–T6FÆösÕ²w÷VÆ"rÂvæWrrÂvfVGW&VBuÒæ–æ6ÇVFW2†6FÆör“ö6FÆös¢w÷VÆ"s°¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚u¶FFÖÖ÷f–RÖ6FÆöuÒr’æf÷$V6‚†gVæ7F–öâ†'Fâ—¶'Fâæ6Æ74Æ—7BçFövvÆR‚vöârÆ'FâæFF6WBæÖ÷f–T6FÆösÓÓÕöÖ÷f–T6FÆör“·Ò“°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v6–æVÖWFÖ÷f–TÆ—7Br“°¢–b‚VÂ—&WGW&ã°¢6öç7B66†VCÕöÖ÷f–T6FÆöt66†UµöÖ÷f–T6FÆöuÓ°¢–b†66†VB—°¢6WDÖ÷f–U&÷f–FW$Æ–÷WB†66†VBæÆövvVEö–â“°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&Ö÷f–Vw&–B"7G–ÆSÒ&Ö&v–â×F÷£#âr¶66†VBæÖ÷f–W2æÖ†ÓÓæÖ÷f–T6&B†ÒÇG'VRÆfÇ6RÇG'VR’’æ¦ö–â‚rr’²sÂöF—câs°¢&WGW&ã°¢Ð¢VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äÆöF–ærââãÂ÷7ãâs°¢G'—°¢6öç7B#Öv—B’‚rö’öÖ÷f–Uö6FÆösö6FÆösÒr¶Væ6öFUU$”6ö×öæVçB…öÖ÷f–T6FÆör’²rfÆ–Ö—CÓr“°¢–b‡G—Vöb"æÆövvVEö–ãÓÓÒv&ööÆVâr—6WDÖ÷f–U&÷f–FW$Æ–÷WB‡"æÆövvVEö–â“°¢–b‡"æW'&÷"—F‡&÷ræWrW'&÷"‡"æW'&÷"“°¢–b‚"æÖ÷f–W2æÆVæwF‚—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äæòÖ÷f–W2f÷VæBãÂ÷7ãâs·&WGW&ã·Ð¢öÖ÷f–T6FÆöt66†UµöÖ÷f–T6FÆöuÓ×¶Ö÷f–W3§"æÖ÷f–W2ÆÆövvVEö–ã¢"æÆövvVEö–çÓ°¢v—BÆöDÖ÷f–Tff÷&—FW2‚“°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&Ö÷f–Vw&–B"7G–ÆSÒ&Ö&v–â×F÷£#âr·"æÖ÷f–W2æÖ†ÓÓæÖ÷f–T6&B†ÒÇG'VRÆfÇ6RÇG'VR’’æ¦ö–â‚rr’²sÂöF—câs°¢Ö6F6‚†R—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#ä6÷VÆBæ÷BÆöBÖ÷f–R6FÆörãÂ÷7ãâs·Ð§Ð¦7–æ2gVæ7F–öâ6†V6´Ö÷f–W2†'Fâ—°¢6öç7BöÆCÖ'Fâæ–ææW$…DÔÃ¶'FâæF—6&ÆVC×G'VS¶'FâçFW‡D6öçFVçCÒt6†V6¶–ærf÷"æWrÖ÷f–W2âââs°¢G'—°¢6öç7B£Öv—B’‚rö’ö6†V6µöÖ÷f–U÷WFFW2rÇ¶ÖWF†öC¢uõ5BwÒ“°¢–b†¢æW'&÷"—F‡&÷ræWrW'&÷"†¢æW'&÷'ÇÂvÖ÷f–R&Vg&W6‚f–ÆVBr“°¢v—BÆöE&V6VçDÖ÷f–W2ƒ’“°¢–b†¢ææWuöÖ÷f–W3ã—Fö7B‚tf÷VæBr¶¢ææWuöÖ÷f–W2²ræWrÖ÷f–Rr²†¢ææWuöÖ÷f–W3ÓÓÓòrs¢w2r’Ãs“°¢VÇ6RFö7B‚tÖ÷f–RÆ–'&'’—2WFòFFRârÃs“°¢Ö6F6‚†R—·Fö7B‚t6÷VÆBæ÷B&Vg&W6‚Ö÷f–RÆ–'&'’ârÃs“·Ð¢'FâæF—6&ÆVCÖfÇ6S¶'Fâæ–ææW$…DÔÃÖöÆC°§Ð¦7–æ2gVæ7F–öâW‡æE&V6VçDÖ÷f–W2†'Fâ—°¢6öç7BöÆCÖ'FâçFW‡D6öçFVçC¶'FâæF—6&ÆVC×G'VS¶'FâçFW‡D6öçFVçCÒtÆöF–ærâââs°¢v—BÆöE&V6VçDÖ÷f–W2ƒ3b“°¢'FâæF—6&ÆVCÖfÇ6S¶'FâçFW‡D6öçFVçCÖöÆC¶'Fâæ6Æ74Æ—7BæFB‚v†–FRr“°§Ð¦7–æ2gVæ7F–öâÆöDÖ÷f–Tff÷&—FW2‚—°¢6öç7B#Öv—B’‚rö’öff÷&—FW2r“°¢6öç7BÖ÷f–W3×"æÖ÷f–W7ÇÅµÓ°¢öfdÖ÷f–U6WCÖæWr6WB†Ö÷f–W2æÖ†ÓÓå7G&–ær†Òæ6FÆöuö–GÇÆÒç7G&VÕö–B’’“°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–TfdÆ—7Br“°¢–b‚Ö÷f–W2æÆVæwF‚—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äæòff÷&—FRÖ÷f–W2–WBãÂ÷7ãâs·&WGW&ã·Ð¢ÆWBƒÒrs°¢f÷"†6öç7BÒöbÖ÷f–W2—°¢6öç7B¶W“ÖW64GG"…7G&–ær†Òæ6FÆöuö–GÇÆÒç7G&VÕö–B’“°¢6öç7B6ÆVäæÖSÖ6ÆVäÖ÷f–U6V&6…F—FÆR†ÒææÖR“°¢6öç7B÷7FW#ÒsÇ7â6Æ73Ò&Ö÷f–Vfg÷7FW"#âb3#s“c²r²†Òæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†Òæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÂ÷7ãâs°¢‚³ÒsÆF—b6Æ73Ò&Ö÷f–Vfb"FF×VW'“Ò"r¶W64GG"†6ÆVäæÖR’²r#âr·÷7FW"²sÆF—b6Æ73Ò&Ö÷f–Vff–æfò#ãÆF—b6Æ73Ò&Ö÷f–VffæÖR#âr¶W62†6ÆVäæÖR’²sÂöF—cãÂöF—câp¢²sÇ7â6Æ73Ò&fg7F"öâÖ÷f–W&VÖ÷fR"FFÖ¶W“Ò"r¶¶W’²r"F—FÆSÒ%&VÖ÷fRg&öÒff÷&—FW2#âb3“s33³Â÷7ããÂöF—câs°¢Ð¢VÂæ–ææW$…DÔÃÖƒ°§Ð¦7–æ2gVæ7F–öâFövvÆTÖ÷f–Tff÷&—FR†Ö÷f–RÇ7F$VÂ—°¢6öç7B#Öv—Bfe÷7B‡¶7F–öã¢wFövvÆUöÖ÷f–RrÆÖ÷f–S¦Ö÷f–WÒ“°¢öfdÖ÷f–U6WCÖæWr6WB‚‡"æÖ÷f–Uö–G7ÇÅµÒ’æÖ…7G&–ær’“°¢–b…öfdÖ÷f–U6WBæ†2…7G&–ær†Ö÷f–Ræ6FÆöuö–GÇÆÖ÷f–Rç7G&VÕö–B’’•÷&öf–ÆT6öæf–rç6WGWöFVÖõö6öçFVçCÖfÇ6S°¢–b‡7F$VÂ—7F$VÂæ6Æ74Æ—7BçFövvÆR‚vöârÅöfdÖ÷f–U6WBæ†2…7G&–ær†Ö÷f–Ræ6FÆöuö–GÇÆÖ÷f–Rç7G&VÕö–B’’“°¢–b‡7F$VÂ—7F$VÂçF—FÆS×G"…öfdÖ÷f–U6WBæ†2…7G&–ær†Ö÷f–Ræ6FÆöuö–GÇÆÖ÷f–Rç7G&VÕö–B’“òu&VÖ÷fRg&öÒff÷&—FW2s¢tFBFòff÷&—FW2r“°¢v—BÆöDÖ÷f–Tff÷&—FW2‚“°§Ð¦7–æ2gVæ7F–öâ&VÖ÷fTÖ÷f–Tff÷&—FR†¶W’—°¢v—Bfe÷7B‡¶7F–öã¢w&VÖ÷fUöÖ÷f–RrÆff÷&—FUö¶W“¦¶W—Ò“°¢öfdÖ÷f–U6WBæFVÆWFR…7G&–ær†¶W’’“°¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚ræÖ÷f–W7F"r’æf÷$V6‚†VÃÓç¶–b†VÂævWDGG&–'WFR‚vFFÖ¶W’r“ÓÓÕ7G&–ær†¶W’’–VÂæ6Æ74Æ—7Bç&VÖ÷fR‚vöâr“·Ò“°¢v—BÆöDÖ÷f–Tff÷&—FW2‚“°§Ð¦7–æ2gVæ7F–öâ6V&6„Ö÷f–W2‚—°¢6öç7BÒ†Fö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–Ur’çfÇVWÇÂrr’çG&–Ò‚“°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–U&W7VÇG2r“°¢6öç7B6FÆöw3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–T6FÆöw2r“°¢–b‚—¶6FÆöw2æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“¶VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ&Ö&v–â×F÷£G‚#äVçFW"Ö÷f–RF—FÆRãÂöF—câs·&WGW&ã·Ð¢6FÆöw2æ6Æ74Æ—7BæFB‚v†–FRr“°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ&Ö&v–â×F÷£G‚#å6V&6†–ærÖ÷f–W2ââãÂöF—câs°¢6öç7B#Öv—B’‚rö’öÖ÷f–W3÷Òr¶Væ6öFUU$”6ö×öæVçB‡’“°¢6öç7B&6³ÒsÆF—b6Æ73Ò&Ö÷f–W&W7VÇF&6²#ãÆ'WGFöâ6Æ73Ò&v†÷7B"öæ6Æ–6³Ò&&6µFô×”Ö÷f–W2‚’#âb3ƒS“#²r·G"‚t&6²FòÖ÷f–W2r’²sÂö'WGFöããÂöF—câs°¢–b‡"æW'&÷"—¶VÂæ–ææW$…DÔÃÖ&6²²sÆF—b6Æ73Ò&W'"Ö÷f–W&W7VÇG7FGW2#âr¶W62‡"æW'&÷"’²sÂöF—câs·&WGW&ã·Ð¢–b‚"æÖ÷f–W2æÆVæwF‚—¶VÂæ–ææW$…DÔÃÖ&6²²sÆF—b6Æ73Ò&×WFVBÖ÷f–W&W7VÇG7FGW2#äæòÖ÷f–W2f÷VæBf÷"gV÷C²r¶W62‡’²rgV÷C²ãÂöF—câs·&WGW&ã·Ð¢v—BÆöDÖ÷f–Tff÷&—FW2‚“°¢ÆWBƒÖ&6²²sÆF—b6Æ73Ò&×WFVBÖ÷f–W&W7VÇG7FGW2#âr·"æÖ÷f–W2æÆVæwF‚²r&W7VÇBr²‡"æÖ÷f–W2æÆVæwFƒÓÓÓòrs¢w2r’²sÂöF—cãÆF—b6Æ73Ò&Ö÷f–Vw&–B#âs°¢f÷"†6öç7BÒöb"æÖ÷f–W2–‚³ÖÖ÷f–T6&B†ÒÇG'VR“°¢VÂæ–ææW$…DÔÃÖ‚²sÂöF—câs°§Ð¦gVæ7F–öâ&6µFô×”Ö÷f–W2‚—°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–Ur’çfÇVSÒrs°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–U&W7VÇG2r’æ–ææW$…DÔÃÒrs°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–T6FÆöw2r’æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°§Ð¦7–æ2gVæ7F–öâÆ”Ö÷f–UdÄ2‡6–BÆW‡BÆ'Fâ—°¢6öç7BöÆCÖ'FâçFW‡D6öçFVçC¶'FâçFW‡D6öçFVçCÒt÷Væ–ærâââs°¢G'—°¢6öç7B£Öv—B’‚rö’÷Æ•öÖ÷f–RrÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡·7G&VÕö–C§6–BÆW‡FVç6–öã¦W‡GÒ—Ò“°¢–b†¢æW'&÷"–ÆW'B†¢æW'&÷'ÇÂt6÷VÆBæ÷BÆVæ6‚dÄ2âr“°¢Ö6F6‚†R—¶ÆW'B‚t6÷VÆBæ÷BÆVæ6‚dÄ2âr“·Ð¢6WEF–ÖV÷WB‚‚“Óç¶'FâçFW‡D6öçFVçCÖöÆC·ÒÃ#“°§Ð¦ÆWB÷v—6†Æ—7DvÖW3ÕµÒÅ÷v—6†Æ—7DÆ–æ¶VCÖfÇ6S°¦7–æ2gVæ7F–öâÆöE7FVÕ&öf–ÆR‚—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕ&öf–ÆRr“¶–b‚VÂ—&WGW&ã°¢G'—°¢6öç7BÖv—B’‚rö’÷7FVÕ÷&öf–ÆRr“°¢–b‚ÇÂæÆ–æ¶VB—¶VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò'7FV×&öf–ÆVV×G’#äÆ–æ²7FVÒv—6†Æ—7BFò6†÷r–÷W"7FVÒ&öf–ÆR†W&RãÂöF—câs·&WGW&ã·Ð¢–b‚æF—7Æ•öæÖRbbæfF"—¶VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò'7FV×&öf–ÆVV×G’#å7FVÒ&öf–ÆR—2Æ–æ¶VBÂ'WB—G2V&Æ–2&öf–ÆRFWF–Ç2&RVæf–Æ&ÆRãÂöF—câs·&WGW&ã·Ð¢6öç7BfF%7&3×æfF%öÆö6ÇÇÇæfF'ÇÂrrÆfF$fÆÆ&6³Ò‡æfF%öÆö6ÂbgæfF"“÷æfF#¢rs°¢6öç7BfF#ÖfF%7&3òsÆ–Ör6Æ73Ò'7FV×&öf–ÆVfF""7&3Ò"r¶W64GG"†fF%7&2’²r"FFÖfÆÆ&6³Ò"r¶W64GG"†fF$fÆÆ&6²’²r"ÇCÒ""&VfW'&W'öÆ–7“Ò&æò×&VfW'&W""öæW'&÷#Ò&–b‡F†—2æFF6WBæfÆÆ&6²bbF†—2æFF6WBçG&–VB—·F†—2æFF6WBçG&–VCÓ·F†—2ç7&3×F†—2æFF6WBæfÆÆ&6³·ÖVÇ6RF†—2ç&VÖ÷fR‚’#âs¢rs°¢6öç7B&VÃ×ç&VÅöæÖSòsÆF—b6Æ73Ò'7FV×&öf–ÆW&VÂ#âr¶W62‡ç&VÅöæÖR’²sÂöF—câs¢rs°¢6öç7BÆö3×æÆö6F–öãòsÆF—b6Æ73Ò'7FV×&öf–ÆVÆö2#âr¶W62‡æÆö6F–öâ’²sÂöF—câs¢rs°¢6öç7BÆWfVÃÒ‡æÆWfVÂÓ×VæFVf–æVBbgæÆWfVÂÓÖçVÆÂ“òsÇ7â6Æ73Ò'7FVÖÆWfVÂ"F—FÆSÒ%7FVÒÆWfVÂ#âr¶W62‡æÆWfVÂ’²sÂ÷7ãâs¢rs°¢6öç7B–V'3Ò‡ç–V'5÷6W'f–6RÓ×VæFVf–æVBbgç–V'5÷6W'f–6RÓÖçVÆÂ“òsÇ7â6Æ73Ò'7FV×–V'2#âr¶W62‡ç–V'5÷6W'f–6R’²r–V'2öb6W'f–6SÂ÷7ãâs¢rs°¢6öç7B7VÖÖ'“×ç7VÖÖ'“òsÆF—b6Æ73Ò'7FV×&öf–ÆW7VÖÖ'’#âr¶W62‡ç7VÖÖ'’’²sÂöF—câs¢rs°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò'7FV×&öf–ÆV–ææW"#ãÆ6Æ73Ò'7FV×&öf–ÆVÆ–æ²"‡&VcÒ"r¶W64GG"‡ç&öf–ÆU÷W&ÇÇÂr2r’²r"F&vWCÒ%ö&Ææ²"&VÃÒ&æö÷VæW"æ÷&VfW'&W"#ãÆF—b6Æ73Ò'7FV×&öf–ÆV†VB#âr¶fF"²sÆF—cãÆF—b6Æ73Ò'7FV×&öf–ÆVæÖR#âr¶W62‡æF—7Æ•öæÖWÇÂu7FVÒr’²sÂöF—câr·&VÂ¶Æö2²sÂöF—cãÂöF—cãÂöãÆF—b6Æ73Ò'7FV×&öf–ÆVÖWF#âr¶ÆWfVÂ·–V'2²sÂöF—câr·7VÖÖ'’²sÂöF—câs°¢Ö6F6‚†R—¶VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò'7FV×&öf–ÆVV×G’#å7FVÒ&öf–ÆRFWF–Ç2Væf–Æ&ÆRãÂöF—câs·Ð§Ð¦gVæ7F–öâWFFU7FVÕv—6†Æ—7D†VÇ‚—°¢6öç7B†4vÖW3Õ÷v—6†Æ—7DÆ–æ¶VBbe÷v—6†Æ—7DvÖW2æÆVæwFƒãÆVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7D†VÇr’Æf–ÇFW%&÷sÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vvÖUv—6†Æ—7Df–ÇFW%&÷rr“°¢–b†VÂ–VÂæ6Æ74Æ—7BçFövvÆR‚v†–FRrÆ†4vÖW2“°¢–b†f–ÇFW%&÷r–f–ÇFW%&÷ræ6Æ74Æ—7BçFövvÆR‚v†–FRrÂ†4vÖW2“°¢6öç7B6WGF–æw3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7E6WGF–æw2r’ÇV–6³ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7EV–6´'Fâr“°¢–b‡6WGF–æw2bb÷v—6†Æ—7DÆ–æ¶VB—6WGF–æw2æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢–b‡V–6²—V–6²æ6Æ74Æ—7BçFövvÆR‚v†–FRrÂ÷v—6†Æ—7DÆ–æ¶VB“°§Ð¦gVæ7F–öâFövvÆU7FVÕv—6†Æ—7E6WGF–æw2‚—¶6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7E6WGF–æw2r“¶–b†VÂ–VÂæ6Æ74Æ—7BçFövvÆR‚v†–FRr“·Ð¦gVæ7F–öâ&VæFW$vÖUv—6†Æ—7B‚—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vvÖUv—6†Æ—7Br“¶–b‚VÂ—&WGW&ã°¢6öç7B–çWCÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vvÖUv—6†Æ—7Df–ÇFW"r’ÇÕ7G&–ær‚†–çWBbf–çWBçfÇVR—ÇÂrr’çG&–Ò‚’çFôÆ÷vW$66R‚“°¢6öç7BvÖW3×õ÷v—6†Æ—7DvÖW2æf–ÇFW"†sÓå7G&–ær†rææÖWÇÂrr’çFôÆ÷vW$66R‚’æ–æ6ÇVFW2‡’“¥÷v—6†Æ—7DvÖW3°¢–b‚vÖW2æÆVæwF‚—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr²‡òtæòÖF6†–ærv—6†Æ—7BvÖW2âs¢tæò7FVÒv—6†Æ—7BvÖW2–WBâr’²sÂ÷7ãâs·&WGW&ã·Ð¢6öç7Bæ÷sÔFFRææ÷r‚“°¢VÂæ–ææW$…DÔÃÖvÖW2æÖ†sÓç¶6öç7BW&ÃÖrçW&ÇÇÂ‚v‡GG3¢ò÷7F÷&Rç7FV×÷vW&VBæ6öÒöòr¶Væ6öFUU$”6ö×öæVçB…7G&–ær†ræö–GÇÂrr’’²ròr’Ç&VÆV6UG3ÔFFRç'6R†rç&VÆV6VGÇÂrr’Æ6÷VçFF÷vãÔçVÖ&W"æ—4f–æ—FR‡&VÆV6UG2’bg&VÆV6UG3ææ÷s÷&6–æt6÷VçFF÷vâ‡·7F'C¦rç&VÆV6VGÒ“¢rrÇ&VÆV6SÒ†rç&VÆV6U÷FW‡GÇÆ6÷VçFF÷vâ“òsÆF—b6Æ73Ò&vÖV6&G&VÆV6R#âr²†rç&VÆV6U÷FW‡CòsÆF—b6Æ73Ò&Ö÷f–VÖWF#âr¶W62†rç&VÆV6U÷FW‡B’²sÂöF—câs¢rr’²†6÷VçFF÷vãòsÆF—b6Æ73Ò&vÖV6÷VçFF÷vâ#âr¶W62†6÷VçFF÷vâ’²sÂöF—câs¢rr’²sÂöF—câs¢rs·&WGW&âsÆ6Æ73Ò&vÖV6&Bv—6†Æ—7FvÖR"‡&VcÒ"r¶W64GG"‡W&Â’²r"F&vWCÒ%ö&Ææ²"&VÃÒ&æö÷VæW"æ÷&VfW'&W"#âr²†ræ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†ræ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÆF—b6Æ73Ò&vÖV6&F&öG’#ãÆF—b6Æ73Ò&vÖV6&FæÖR#âr¶W62†rææÖWÇÂtvÖRr’²sÂöF—câr·&VÆV6R²sÂöF—cãÂöâs·Ò’æ¦ö–â‚rr“°§Ð¦7–æ2gVæ7F–öâÆöDvÖTff÷&—FW2‚—°¢6öç7B#Öv—B’‚rö’öff÷&—FW2r’Ææ÷sÔFFRææ÷r‚’ÆvÖW3Ò‡"ævÖW7ÇÅµÒ’æf–ÇFW"†sÓærçv—6†Æ—7Eö–×÷'FVB’ÆVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vvÖUv—6†Æ—7Br“°¢vÖW2ç6÷'B‚†Æ"“Óç°¢6öç7BCÔFFRç'6R†ç&VÆV6VGÇÂrr’Æ'CÔFFRç'6R†"ç&VÆV6VGÇÂrr’ÆcÔçVÖ&W"æ—4f–æ—FR†B’bfCææ÷rÆ&cÔçVÖ&W"æ—4f–æ—FR†'B’bf'Cææ÷rÆÔçVÖ&W"æ—4f–æ—FR†B’bfCÃÖæ÷rÆ'ÔçVÖ&W"æ—4f–æ—FR†'B’bf'CÃÖæ÷s°¢–b†bÓÖ&b—&WGW&âcòÓ£°¢–b†bbf&b—&WGW&âBÖ'C°¢–b†ÓÖ'—&WGW&âòÓ£°¢–b†bf'—&WGW&â'BÖC°¢&WGW&â7G&–ær†ææÖWÇÂrr’æÆö6ÆT6ö×&R…7G&–ær†"ææÖWÇÂrr’“°¢Ò“°¢÷v—6†Æ—7DvÖW3ÖvÖW3·&VæFW$vÖUv—6†Æ—7B‚“·WFFU7FVÕv—6†Æ—7D†VÇ‚“°§Ð¦7–æ2gVæ7F–öâÆöE7FVÕv—6†Æ—7E6WGF–ær‚—°¢G'—°¢6öç7B3Öv—B’‚rö’ö6öæf–rr’Æ–çWCÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7Er’Æ'FãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7D'Fâr’Ç7FGW3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7E7FGW2r“°¢6öç7BÆ–æ¶VCÒ7G&–ær†2ç7FVÕ÷v—6†Æ—7E÷W&ÇÇÂrr’çG&–Ò‚“°¢÷v—6†Æ—7DÆ–æ¶VCÖÆ–æ¶VC·WFFU7FVÕv—6†Æ—7D†VÇ‚“°¢–b†–çWB—¶–çWBçfÇVSÖ2ç7FVÕ÷v—6†Æ—7E÷W&ÇÇÂrs¶–çWBç&VDöæÇ“ÖfÇ6S·Ð¢–b†'Fâ—¶'FâçFW‡D6öçFVçC×G"‚u7–æ2v—6†Æ—7Br“¶'Fâç6WDGG&–'WFR‚vFFÖ“†ârÂu7–æ2v—6†Æ—7Br“·Ð¢6öç7B6WGF–æw3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7E6WGF–æw2r“¶–b‡6WGF–æw2bfÆ–æ¶VB—6WGF–æw2æ6Æ74Æ—7BæFB‚v†–FRr“°¢–b†Æ–æ¶VBbg7FGW2bf2ç7FVÕ÷v—6†Æ—7E÷7–æ6VEöB—°¢6öç7BÆö6ÆSÕöÆæsÓÓÒvæòsòvæ"Ôäòs§VæFVf–æVC°¢7FGW2çFW‡D6öçFVçCÒtÆ7B&Vg&W6†VBr¶æWrFFR„çVÖ&W"†2ç7FVÕ÷v—6†Æ—7E÷7–æ6VEöB’£’çFôÆö6ÆU7G&–ær†Æö6ÆR“°¢Ð¢Ö6F6‚†R—·Ð¢ÆöE7FVÕ&öf–ÆR‚“°§Ð¦7–æ2gVæ7F–öâ7–æ57FVÕv—6†Æ—7B†'Fâ—°¢6öç7B–çWCÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7Er’Ç7FGW3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w7FVÕv—6†Æ—7E7FGW2r’ÇfÇVSÒ†–çWBçfÇVWÇÂrr’çG&–Ò‚“°¢–b‚fÇVR—·7FGW2çFW‡D6öçFVçCÒtVçFW"–÷W"V&Æ–27FVÒv—6†Æ—7BU$Ââs·&WGW&ã·Ð¢6öç7BöÆCÖ'FâçFW‡D6öçFVçC¶'FâæF—6&ÆVC×G'VS¶'FâçFW‡D6öçFVçCÒu7–æ6–ærâââs·7FGW2çFW‡D6öçFVçCÒu&VF–ær7FVÒv—6†Æ—7Bâââs°¢G'—°¢6öç7B£Öv—B’‚rö’ö–×÷'E÷7FVÕ÷v—6†Æ—7BrÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡·W&Ã§fÇVWÒ—Ò“°¢–b†¢æW'&÷"—F‡&÷ræWrW'&÷"†¢æW'&÷'ÇÂuv—6†Æ—7B7–æ2f–ÆVBr“°¢v—BÆöDvÖTff÷&—FW2‚“¶ÆöDff÷&—FW2‚“¶v—BÆöE7FVÕv—6†Æ—7E6WGF–ær‚“·7FGW2çFW‡D6öçFVçCÒu7–æ6VBr¶¢æ–×÷'FVB²rvÖW2g&öÒ7FVÒâs°¢Ö6F6‚†R—·7FGW2çFW‡D6öçFVçCÒt6÷VÆBæ÷B7–æ2v—6†Æ—7C¢r¶RæÖW76vS·Ð¢'FâæF—6&ÆVCÖfÇ6S¶–b†'FâçFW‡D6öçFVçCÓÓÒu7–æ6–ærâââr–'FâçFW‡D6öçFVçCÖöÆC°§Ð¦ÆWB÷7FVÕv—6†Æ—7DWFô6†V6¶VCÖfÇ6S°¦7–æ2gVæ7F–öâÖ–&TWFõ&Vg&W6…7FVÕv—6†Æ—7B†2—°¢–b…÷7FVÕv—6†Æ—7DWFô6†V6¶VB—&WGW&ã°¢÷7FVÕv—6†Æ—7DWFô6†V6¶VC×G'VS°¢–b†2bf2ævÖW5öVæ&ÆVCÓÓÖfÇ6R—&WGW&ã°¢6öç7BW&ÃÕ7G&–ær‚†2bf2ç7FVÕ÷v—6†Æ—7E÷W&Â—ÇÂrr’çG&–Ò‚’Ç7–æ6VCÔçVÖ&W"‚†2bf2ç7FVÕ÷v—6†Æ—7E÷7–æ6VEöB—ÇÃ’£°¢–b‚W&ÇÇÂ‡7–æ6VBbdFFRææ÷r‚’×7–æ6VCÃr£#B£c£c£’—&WGW&ã°¢G'—°¢6öç7B£Öv—B’‚rö’ö–×÷'E÷7FVÕ÷v—6†Æ—7BrÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡·W&Ã§W&ÇÒ—Ò“°¢–b†¢æW'&÷"—&WGW&ã°¢–b‚vÖW5f–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’—¶v—BÆöDvÖTff÷&—FW2‚“¶v—BÆöE7FVÕv—6†Æ—7E6WGF–ær‚“·Ð¢–b‚×–Æ—7Ef–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’–ÆöDff÷&—FW2‚“°¢Ö6F6‚†R—·Ð§Ð¦6öç7Bõ$4”äuõ4U$”U3Õµ²vcrÂtf÷&×VÆuÒÅ²vc"rÂtf÷&×VÆ"uÒÅ²vc2rÂtf÷&×VÆ2uÒÅ²v–æG–6"rÂt–æG”6"uÒÅ²wvV2rÂutT2uÒÅ²vf÷&×VÆRrÂtf÷&×VÆRuÒÅ²vÖ÷FöwrÂtÖ÷FôuuÒÅ²ww&2rÂuu$2&ÆÇ’uÕÓ°¦6öç7Bõ$4”äuôÄôtõ3×°¢c¢v‡GG3¢ò÷wwræf÷&×VÆæ6öÒöWF2öFW6–vç2öföÒ×vV'6—FRö–ÖvW2öcöÆövòç7frrÀ¢c#¢v‡GG3¢ò÷WÆöBçv–¶–ÖVF–æ÷&r÷v–¶—VF–ö6öÖÖöç2óbócBôd”ôf÷&×VÆó%ô6†×–öç6†—öÆövõòS#ƒ##bS#’ç7frrÀ¢c3¢v‡GG3¢ò÷WÆöBçv–¶–ÖVF–æ÷&r÷v–¶—VF–ö6öÖÖöç2öBöC’ôd”ôf÷&×VÆó5ô6†×–öç6†—öÆövõòS#ƒ##bS#’ç7frrÀ¢–æG–6#¢v‡GG3¢ò÷wwræ–æG–6"æ6öÒòÒöÖVF–ô–æG”6"ôæWw2õ7FæF&Bó##"ó‚ó‚Ó2Ô”äE”4"ÔÆövòæ§rrÀ¢vV3¢v‡GG3¢ò÷WÆöBçv–¶–ÖVF–æ÷&r÷v–¶—VF–ö6öÖÖöç2óBóF2ôd”õtT5ôÆövõó##BçærrÀ¢f÷&×VÆS¢v‡GG3¢ò÷WÆöBçv–¶–ÖVF–æ÷&r÷v–¶—VF–ö6öÖÖöç2ööBôd”ôf÷&×VÆôUõv÷&ÆEô6†×–öç6†—ôÆövòç7frrÀ¢Ö÷Föw¢v‡GG3¢ò÷WÆöBçv–¶–ÖVF–æ÷&r÷v–¶—VF–ö6öÖÖöç2öböc’ôÖ÷FôuöÆövõòS#ƒ##BS#’ç7frrÀ¢w&3¢v‡GG3¢ò÷wwræ6æWf&ÆÇ’æ6öÒ÷wÖ6öçFVçB÷WÆöG2ó##Róó3C3“c#5ós“cSƒ“s“CSƒuóC3CƒCSsCS“3“#Sc“5öâÓ2ÖSsCC““C3#Ó3'ƒSS‚Óæ§rp§Ó°¦ÆWB÷&6–æu6VÆV7FVCÖæWr6WB…²vcuÒ“°¦ÆWB÷&6–ætG&—fW%&÷w3ÕµÒÅ÷&6–ætWfVçE&÷w3ÕµÒÅ÷&6–ætFWF–Ä¶W“ÒrrÅ÷&6–ætf–Æ&–Æ—G”ÆöF–æsÖfÇ6S°¦gVæ7F–öâ&6–ætWfVçD—4Æ—fR†WfVçBÆæ÷r—°¢æ÷sÖæ÷wÇÄFFRææ÷r‚“¶6öç7B7F'CÖæWrFFR†WfVçBç7F'B’ævWEF–ÖR‚“¶–b‚çVÖ&W"æ—4f–æ—FR‡7F'B’—&WGW&âfÇ6S°¢6öç7BW‡Æ–6—CÖWfVçBæVæCöæWrFFR†WfVçBæVæB’ævWEF–ÖR‚“¤æã°¢–b†WfVçBæÆÅöF’—°¢6öç7B7F'DF“Ö÷6ÆôF”çVÖ&W"†æWrFFR‡7F'B’’ÆVæDF“ÔçVÖ&W"æ—4f–æ—FR†W‡Æ–6—B“ö÷6ÆôF”çVÖ&W"†æWrFFR†W‡Æ–6—B’“§7F'DF’Ææ÷tF“Ö÷6ÆôF”çVÖ&W"†æWrFFR†æ÷r’“°¢&WGW&âæ÷tF“ã×7F'DF’bfæ÷tF“ÃÖVæDF“°¢Ð¢–b„çVÖ&W"æ—4f–æ—FR†W‡Æ–6—B’—&WGW&âæ÷sã×7F'Bbfæ÷sÃÖW‡Æ–6—C°¢ÆWBGW&F–öãÓ"£3c°¢–b†WfVçBæÆÅöF’–GW&F–öãÓ#B£3c°¢VÇ6R–b…7G&–ær†WfVçBç6W76–öçÇÂrr’çFôÆ÷vW$66R‚“ÓÓÒw&6Rr–GW&F–öãÓB£3c°¢&WGW&âæ÷sã×7F'Bbfæ÷sÃ×7F'B¶GW&F–öã°§Ð¦gVæ7F–öâæW‡DG&—fW%&6R†G&—fW"ÆWfVçG2Ææ÷r—°¢æ÷sÖæ÷wÇÄFFRææ÷r‚“°¢&WGW&â†WfVçG7ÇÅµÒ’æf–ÇFW"†SÓå7G&–ær†Rç6W&–W7ÇÂrr“ÓÓÕ7G&–ær†G&—fW"ç6W&–W7ÇÂrr’bb†G&—fW"ç6W&–W2ÓÒvcwÇÅ7G&–ær†Rç6W76–öçÇÂrr’çFôÆ÷vW$66R‚“ÓÓÒw&6Rr’’æÖ†SÓâ‡¶WfVçC¦RÇG3¦æWrFFR†Rç7F'B’ævWEF–ÖR‚’ÆÆ—fS§&6–ætWfVçD—4Æ—fR†RÆæ÷r—Ò’’æf–ÇFW"‡ƒÓäçVÖ&W"æ—4f–æ—FR‡‚çG2’bb‡‚æÆ—fWÇÇ‚çG3ãÖæ÷r’’ç6÷'B‚†Æ"“Óâ†æÆ—fSòÓ£’Ò†"æÆ—fSòÓ£—ÇÆçG2Ö"çG2•³ÓòæWfVçGÇÆçVÆÃ°§Ð¦gVæ7F–öâ&6–ætFWF–Åv†Vâ†WfVçB—°¢–b‚WfVçB—&WGW&ârs°¢6öç7BCÖæWrFFR†WfVçBç7F'B’ÆÆö6ÆSÕöÆæsÓÓÒvæòsòvæ"Ôäòs§VæFVf–æVC°¢&WGW&âWfVçBæÆÅöF“ò†WfVçBæFFU÷FW‡GÇÆBçFôÆö6ÆTFFU7G&–ær†Æö6ÆRÇ·vVV¶F“¢vÆöærrÆF“¢vçVÖW&–2rÆÖöçFƒ¢vÆöærwÒ’“¦BçFôÆö6ÆU7G&–ær†Æö6ÆRÇ·vVV¶F“¢vÆöærrÆF“¢vçVÖW&–2rÆÖöçFƒ¢vÆöærrÆ†÷W#¢s"ÖF–v—BrÆÖ–çWFS¢s"ÖF–v—BwÒ“°§Ð¦gVæ7F–öâ&6–æt6÷VçFF÷vâ†WfVçB—°¢–b‚WfVçB—&WGW&ârs°¢6öç7BF&vWCÖæWrFFR†WfVçBç7F'B’Ææ÷sÖæWrFFR‚’Ç&VÖ–æ–æs×F&vWBÖæ÷s¶–b‚çVÖ&W"æ—4f–æ—FR‡&VÖ–æ–ær’—&WGW&ârs°¢–b‡&VÖ–æ–æsÃÓbg&6–ætWfVçD—4Æ—fR†WfVçBÆæ÷rævWEF–ÖR‚’’—&WGW&âtÄ•dRs°¢–b‡&VÖ–æ–æsÃÓ—&WGW&ârs°¢–b†WfVçBæÆÅöF’—°¢6öç7BF—3ÔÖF‚ç&÷VæB†÷6ÆôF”çVÖ&W"‡F&vWB’Ö÷6ÆôF”çVÖ&W"†æ÷r’“°¢–b†F—3ÓÓÓ—&WGW&âG"‚uFöF’r“°¢–b†F—3ÓÓÓ—&WGW&âG"‚uFöÖ÷'&÷rr“°¢&WGW&âG"‚v–âr’²rr´ÖF‚æÖ‚ƒÆF—2’²rr·G"†F—3ÓÓÓòvF’s¢vF—2r“°¢Ð¢6öç7BÖ–çWFW3ÔÖF‚æÖ‚ƒÄÖF‚æ6V–Â‡&VÖ–æ–æróc’“°¢–b†Ö–çWFW3Ãc—&WGW&âG"‚v–âr’²rr¶Ö–çWFW2²rr·G"†Ö–çWFW3ÓÓÓòvÖ–çWFRs¢vÖ–çWFW2r“°¢–b‡&VÖ–æ–æsÃ#B£3c—¶6öç7B†÷W'3ÔÖF‚æ6V–Â†Ö–çWFW2óc“·&WGW&âG"‚v–âr’²rr¶†÷W'2²rr·G"††÷W'3ÓÓÓòv†÷W"s¢v†÷W'2r“·Ð¢6öç7BF—3ÔÖF‚æÖ‚ƒÄÖF‚ç&÷VæB†÷6ÆôF”çVÖ&W"‡F&vWB’Ö÷6ÆôF”çVÖ&W"†æ÷r’’“°¢&WGW&âG"‚v–âr’²rr¶F—2²rr·G"†F—3ÓÓÓòvF’s¢vF—2r“°§Ð¦gVæ7F–öâ&6–æu6W76–öäÆ&VÂ†WfVçB—°¢ÆWBfÇVSÕ7G&–ær‚†WfVçBbfWfVçBç6W76–öâ—ÇÂrr’ç&WÆ6R‚ò…¶×¥Ò’…´Õ¥Ò’örÂrCC"r’ç&WÆ6R‚õµòÕÒ²örÂrr’ç&WÆ6R‚õÅÇ2²örÂrr’çG&–Ò‚“°¢6öç7B¶W“×fÇVRçFôÆ÷vW$66R‚’ÆÆ&VÇ3×²w&ÆÇ—vVV¶VæBs¢u&ÆÇ’vVV¶VæBrÂw&ÆÇ’vVV¶VæBs¢u&ÆÇ’vVV¶VæBrÂw&6WvVV¶VæBs¢u&6RvVV¶VæBrÂw&6RvVV¶VæBs¢u&6RvVV¶VæBrÂvw&æG&—‡vVV¶VæBs¢tw&æB&—‚vVV¶VæBrÂvw&æB&—‚vVV¶VæBs¢tw&æB&—‚vVV¶VæBwÓ°¢&WGW&âG"†Æ&VÇ5¶¶W•×ÇÇfÇVWÇÂtWfVçBr“°§Ð¦gVæ7F–öâ&6–æu6†÷'DFFR†WfVçB—°¢–b‚WfVçB—&WGW&ârs°¢–b†WfVçBæÆÅöF’bfWfVçBæFFU÷FW‡B—&WGW&â7G&–ær†WfVçBæFFU÷FW‡B’ç&WÆ6R‚õÅÇ2²örÂrr’çG&–Ò‚“°¢6öç7BCÖæWrFFR†WfVçBç7F'B“¶–b„çVÖ&W"æ—4æâ†BævWEF–ÖR‚’’—&WGW&ârs°¢&WGW&âBçFôÆö6ÆU7G&–ær…öÆæsÓÓÒvæòsòvæ"Ôäòs§VæFVf–æVBÇ·vVV¶F“¢w6†÷'BrÆF“¢vçVÖW&–2rÆÖöçFƒ¢w6†÷'BrÆ†÷W#¢s"ÖF–v—BrÆÖ–çWFS¢s"ÖF–v—BrÇF–ÖU¦öæS¢tWW&÷Rô÷6ÆòwÒ“°§Ð¦gVæ7F–öâ&6–æu&öf–ÆTÖWF†WfVçB—°¢–b‚WfVçB—&WGW&ârs°¢6öç7B6W76–öã×&6–æu6W76–öäÆ&VÂ†WfVçB’ÆFFSÖWfVçBæÆÅöF“÷&6–æu6†÷'DFFR†WfVçB“¢rs°¢&WGW&â·6W76–öâÆFFUÒæf–ÇFW"„&ööÆVâ’æ¦ö–â‚r+rr“°§Ð¢òò6V6öæB&÷röb&6–ær&öf–ÆR6&BâÖ—'&÷'2F†Rc6&Bw2&6RÆ–æR6òWfW'¢òò6W&–W2&W6VçG2—G2FWF–ÂF†R6ÖRv“¢Æ&VÂöâF†RÆVgBÂFFRöâF†P¢òò&–v‡BÂ6W&FVBg&öÒF†R†VFÆ–æR'’F—f–FW"à¢òò&–v‡BÖ†æB6öÇVÖâöbF–ÖVÆ–æR6&BâF†R6&G2&RfW'’v–FRÂ6òF†R76P¢òò&WGvVVâF†RF—FÆRæBF†REb&FvR—2W6VBf÷"F†R6÷VçFF÷vâæB†÷rÖç’ö`¢òòF†RW6W"w26†ææVÇ26''’F†RWfVçBà¦gVæ7F–öâF–ÖVÆ–æT6–FR†6÷VçFF÷vâÆ6†ææVÄ6÷VçBÆÆ—fR—°¢6öç7B&—G3ÕµÓ°¢–b†Æ—fR–&—G2çW6‚‚sÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6÷VçBÆ—fR#âr¶W62‡G"‚u&–v‡Bæ÷rr’’²sÂ÷7ãâr“°¢VÇ6R–b†6÷VçFF÷vâ–&—G2çW6‚‚sÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6÷VçB#âr¶W62†6÷VçFF÷vâ’²sÂ÷7ãâr“°¢–b†6†ææVÄ6÷VçB–&—G2çW6‚‚sÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6†ç2#âr¶6†ææVÄ6÷VçB²rr¶W62‡G"†6†ææVÄ6÷VçCÓÓÓòv6†ææVÂs¢v6†ææVÇ2r’’²sÂ÷7ãâr“°¢–b‚&—G2æÆVæwF‚—&WGW&ârs°¢&WGW&âsÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6–FR#âr¶&—G2æ¦ö–â‚rr’²sÂöF—câs°§Ð¢òò6ÖRf7G22&6–æuF–ÖVÆ–æTÖWFÂ'WB26W&FR6öÇVÖç26òv–FRF–ÖVÆ–æP¢òò6&B7&VG2F†VÒ÷WB–ç7FVBöb7F6¶–æröæRFVç6RÆ–æRöâF†RÆVgBà¦gVæ7F–öâ&6–æuF–ÖVÆ–æTf7G2†WfVçBÆ†VFÆ–æR—°¢–b‚WfVçB—&WGW&ârs°¢6öç7B6W&–W3ÖWfVçBç6W&–W5öæÖWÇÂu&6–ærs°¢6öç7B&W7CÕ·&6–æu6W76–öäÆ&VÂ†WfVçB•Ó°¢–b†WfVçBæÆÅöF’—¶6öç7BFFS×&6–æu6†÷'DFFR†WfVçB“¶–b†FFR—&W7BçW6‚†FFR“·Ð¢VÇ6R–b†WfVçBæ6—&7V—BbfWfVçBæ6—&7V—BÓÖWfVçBç&6R—&W7BçW6‚†WfVçBæ6—&7V—B“°¢òò6W&–W26—G2FòF†RÆVgBöbF†R†VFÆ–æS²WfW'—F†–ærVÇ6RföÆÆ÷w2—Bà¢&WGW&âsÇ7â6Æ73Ò'FÆf7BFÆÆVB#âr¶W62‡6W&–W2’²sÂ÷7ãâp¢²sÆ"6Æ73Ò'FÆ†VFÆ–æR#âr²††VFÆ–æWÇÂrr’²sÂö#âp¢²sÆF—b6Æ73Ò'FÆf7G2#âr·&W7Bæf–ÇFW"„&ööÆVâ’æÖ‡ÓâsÇ7ãâr¶W62‡’²sÂ÷7ãâr’æ¦ö–â‚rr’²sÂöF—câs°§Ð¦gVæ7F–öâ&6–æuF–ÖVÆ–æTÖWF†WfVçB—°¢–b‚WfVçB—&WGW&ârs°¢6öç7B'G3Õ¶WfVçBç6W&–W5öæÖWÇÂu&6–ærrÇ&6–æu6W76–öäÆ&VÂ†WfVçB•Ó°¢–b†WfVçBæÆÅöF’—¶6öç7BFFS×&6–æu6†÷'DFFR†WfVçB“¶–b†FFR—'G2çW6‚†FFR“·Ð¢VÇ6R–b†WfVçBæ6—&7V—BbfWfVçBæ6—&7V—BÓÖWfVçBç&6R—'G2çW6‚†WfVçBæ6—&7V—B“°¢&WGW&â'G2æf–ÇFW"„&ööÆVâ’æ¦ö–â‚r+rr“°§Ð¦gVæ7F–öâ&6–æt'DW'&÷"†–Ör—°¢6öç7BfÆÆ&6³Õ7G&–ær†–ÖræFF6WBæfÆÆ&6·ÇÂrr“°¢–b†fÆÆ&6²—¶–ÖræFF6WBæfÆÆ&6³Òrs¶–Örç7&3ÖfÆÆ&6³·&WGW&ã·Ð¢–Örç7G–ÆRæF—7Æ“ÒvæöæRs¶6öç7BÖ&³Ö–ÖrææW‡DVÆVÖVçE6–&Æ–æs¶–b†Ö&²–Ö&²ç7G–ÆRæF—7Æ“ÒvfÆW‚s°§Ð¦gVæ7F–öâ&6–ætWfVçEf—7VÂ†WfVçBÆ6÷VçFF÷vâ—°¢6öç7B6W&–W3Õ7G&–ær‚†WfVçBbfWfVçBç6W&–W2—ÇÂrr’Ç6W&–W4ÆövóÕ7G&–ær…õ$4”äuôÄôtõ5·6W&–W5×ÇÂrr’ÆWfVçDÆövóÒ‡6W&–W3ÓÓÒww&2sõ7G&–ær‚†WfVçBbfWfVçBæ'B—ÇÂrr“¢rr“°¢6öç7B7&3ÖWfVçDÆöv÷ÇÇ6W&–W4Æövó°¢6öç7BfÆÆ&6³ÒsÆF—b6Æ73Ò'&6–ævWfVçFfÆÆ&6²"r²‡7&3òr7G–ÆSÒ&F—7Æ“¦æöæR"s¢rr’²sâr·&6–æu6W&–W4Æövò‡6W&–W2’²sÂöF—câs°¢ÆWB–ÖvSÒrs°¢–b‡7&2—¶6öç7Bf#ÖWfVçDÆövó÷6W&–W4Æövó¢rs¶–ÖvSÒsÆ–Ör7&3Ò"r¶W64GG"‡7&2’²r"FFÖfÆÆ&6³Ò"r¶W64GG"†f"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'&6–æt'DW'&÷"‡F†—2’#âs·Ð¢&WGW&âsÆF—b6Æ73Ò'&6–ævWfVçGf—7VÂ#âr¶–ÖvR¶fÆÆ&6²²†6÷VçFF÷vãòsÆF—b6Æ73Ò'&6–ævFWF–Æ6÷VçFF÷vâ#âr¶W62†6÷VçFF÷vâ’²sÂöF—câs¢rr’²sÂöF—câs°§Ð¦gVæ7F–öâ&6–ætFWF–ÄæW‡B†WfVçB—°¢–b‚WfVçB—&WGW&âsÆF—b6Æ73Ò'&6–ævFWF–ÆæW‡B#ãÆF—b6Æ73Ò'&6–ævFWF–ÆæW‡FÆ&VÂ#âr¶W62‡G"‚tæW‡BWfVçBr’’²sÂöF—cãÇ7â6Æ73Ò&×WFVB#âr¶W62‡G"‚tæòW6öÖ–ær&6Rf÷VæBâr’’²sÂ÷7ããÂöF—câs°¢6öç7B6÷VçFF÷vã×&6–æt6÷VçFF÷vâ†WfVçB“°¢&WGW&âsÆF—b6Æ73Ò'&6–ævFWF–ÆæW‡B#ãÆF—b6Æ73Ò'&6–ævFWF–ÆæW‡FÆ&VÂ#âr¶W62‡G"‚tæW‡BWfVçBr’’²sÂöF—cãÆF—b6Æ73Ò'&6–ævFWF–ÆæW‡Fw&–B#ãÆF—cãÆ#âr¶W62†WfVçBç&6WÇÆWfVçBæ6—&7V—GÇÂu&6Rr’²sÂö#ãÆF—b6Æ73Ò'&6–ævFWF–ÆÖWF#âr¶W62‡&6–ætFWF–Åv†Vâ†WfVçB’’²sÆ'#âr¶W62‡&6–æu6W76–öäÆ&VÂ†WfVçB’’²†WfVçBæ6—&7V—BbfWfVçBæ6—&7V—BÓÖWfVçBç&6SòsÆ'#âr¶W62†WfVçBæ6—&7V—B“¢rr’²sÂöF—cãÂöF—câr·&6–ætWfVçEf—7VÂ†WfVçBÆ6÷VçFF÷vâ’²sÂöF—cãÂöF—câs°§Ð¦gVæ7F–öâ&6–æu6W&–W4Æövò†¶W’—¶6öç7B7&3Õ7G&–ær…õ$4”äuôÄôtõ5¶¶W•×ÇÂrr“·&WGW&â7&3òsÆ–Ör6Æ73Ò'&6–æw6W&–W6Æövò"7&3Ò"r¶W64GG"‡7&2’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rs·Ð¦gVæ7F–öâ&VæFW%&6–æuFVÔ6öçG&öÂ‚—°¢6öç7B6öçG&öÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætcFVÔ6öçG&öÂr’ÆÆ&VÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–æuFVÔÆ&VÂr’Æ'FãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætc6†ö÷6T'Fâr’Ç–6¶W#ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætc–6¶W"r“¶–b‚6öçG&öÇÇÂÆ&VÇÇÂ'Fâ—&WGW&ã°¢6öç7BcÕ÷&6–ætG&—fW%&÷w2æf–ÇFW"†G&—fW#ÓæG&—fW"ç6W&–W3ÓÓÒvcr’ÆcÖöFSÕ÷&6–ætFWF–Ä¶W“ÓÓÒvc×FVÒwÇÂ…÷&6–ætG&—fW%&÷w2æf–æB‡&÷sÓå7G&–ær‡&÷ræ¶W—ÇÂrr“ÓÓÕ7G&–ær…÷&6–ætFWF–Ä¶W—ÇÂrr’—ÇÇ·Ò’ç6W&–W3ÓÓÒvcs°¢–b†cÖöFR—°¢6öç7BG&—fW#Öc³×ÇÇ·ÒÇFVÔ–CÕ7G&–ær†G&—fW"çFVÕö–GÇÂrr“¶6öçG&öÂæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“¶Æ&VÂçFW‡D6öçFVçCÒtf÷&×VÆFVÒs¶'Fâæ6Æ74Æ—7Bç&VÖ÷fR‚w&VFöæÇ’r“¶'Fâç6WDGG&–'WFR‚vöæ6Æ–6²rÂwFövvÆU&6–ætc–6¶W"‚’r“°¢'Fâæ–ææW$…DÔÃÖG&—fW"çFVÓò‚‡FVÔ–CòsÆ–Ör7&3Ò"ö’öc÷FVÕöÆövóö–CÒr¶Væ6öFUU$”6ö×öæVçB‡FVÔ–B’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÇ7ãâr¶W62†G&—fW"çFVÒ’²sÂ÷7ãâr“¢sÇ7ãâr¶W62‡G"‚t6†ö÷6RcFVÒr’’²sÂ÷7ãâs°¢&WGW&ã°¢Ð¢6öç7BG&—fW#Õ÷&6–ætG&—fW%&÷w2æf–æB‡&÷sÓå7G&–ær‡&÷ræ¶W—ÇÂrr“ÓÓÕ7G&–ær…÷&6–ætFWF–Ä¶W—ÇÂrr’“°¢–b‚G&—fW"—¶6öçG&öÂæ6Æ74Æ—7BæFB‚v†–FRr“·&WGW&ã·Ð¢6öçG&öÂæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“¶Æ&VÂçFW‡D6öçFVçCÒ†G&—fW"ç6W&–W5öæÖWÇÂu&6–ærr’²rFVÒs¶'Fâæ6Æ74Æ—7BæFB‚w&VFöæÇ’r“¶'Fâç&VÖ÷fTGG&–'WFR‚vöæ6Æ–6²r“¶–b‡–6¶W"—–6¶W"æ6Æ74Æ—7BæFB‚v†–FRr“°¢6öç7BÆövóÕ7G&–ær†G&—fW"çFVÕöÆöv÷ÇÂrr“¶'Fâæ–ææW$…DÔÃÒ†ÆövóòsÆ–Ör7&3Ò"r¶W64GG"†Æövò’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÇ7ãâr¶W62†G&—fW"çFV×ÇÆG&—fW"ç6W&–W5öæÖWÇÂu&6–ærr’²sÂ÷7ãâs°§Ð¦gVæ7F–öâ&VæFW%&6–ætG&—fW$FWF–Â‚—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætG&—fW$FWF–Âr“¶–b‚VÂ—&WGW&ã°¢6öç7Bæ÷sÔFFRææ÷r‚’ÆcÕ÷&6–ætG&—fW%&÷w2æf–ÇFW"†G&—fW#ÓæG&—fW"ç6W&–W3ÓÓÒvcr“°¢–b…÷&6–ætFWF–Ä¶W“ÓÓÒvc×FVÒrbfcæÆVæwFƒãÓ"—°¢6öç7Bf—'7CÖc³ÒÇFVÔ–CÕ7G&–ær†f—'7BçFVÕö–GÇÂrr’ÆÆ—fSÕ÷&6–ætWfVçE&÷w2æf–ÇFW"†SÓæRç6W&–W3ÓÓÒvcr’ç6öÖR†SÓç&6–ætWfVçD—4Æ—fR†RÆæ÷r’’ÆæW‡CÖæW‡DG&—fW%&6R†f—'7BÅ÷&6–ætWfVçE&÷w2Ææ÷r“°¢6öç7BV÷ÆSÖcç6Æ–6RƒÃ"’æÖ†G&—fW#ÓâsÆF—b6Æ73Ò'&6–ævFWF–ÇW'6öâ#ãÆ–Ör7&3Ò"ö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær†G&—fW"æ¶W—ÇÂrr’’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#ãÆF—cãÆ#âr¶W62†G&—fW"ææÖWÇÂrr’²sÂö#âr²†G&—fW"çW&ÃòsÆF—b6Æ73Ò&Ö÷f–VÖWF#ãÆ‡&VcÒ"r¶W64GG"†G&—fW"çW&Â’²r"F&vWCÒ%ö&Ææ²"&VÃÒ&æö÷VæW"æ÷&VfW'&W"#âr¶W62‡G"‚tG&—fW"&öf–ÆRr’’²r(isÂöãÂöF—câs¢rr’²sÂöF—cãÂöF—câr’æ¦ö–â‚rr“°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò'&6–ævFWF–Æc†W&ò#âr²‡FVÔ–CòsÆ–Ör7&3Ò"ö’öc÷FVÕöÆövóö–CÒr¶Væ6öFUU$”6ö×öæVçB‡FVÔ–B’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÆF—cãÆF—b6Æ73Ò'&6–ævFWF–Ç6W&–W2#äf÷&×VÆr²†Æ—fSòr+rÄ•dRs¢rr’²sÂöF—cãÆƒ#âr¶W62†f—'7BçFV×ÇÂtf÷&×VÆr’²sÂöƒ#ãÆF—b6Æ73Ò'&6–ævFWF–ÇFVÒ#âr¶W62†cç6Æ–6RƒÃ"’æÖ†CÓæBææÖR’æ¦ö–â‚r+rr’’²sÂöF—cãÂöF—cãÂöF—cãÆF—b6Æ73Ò'&6–ævFWF–ÇV÷ÆR#âr·V÷ÆR²sÂöF—câr·&6–ætFWF–ÄæW‡B†æW‡B“°¢&WGW&ã°¢Ð¢6öç7BG&—fW#Õ÷&6–ætG&—fW%&÷w2æf–æB‡&÷sÓå7G&–ær‡&÷ræ¶W—ÇÂrr“ÓÓÕ7G&–ær…÷&6–ætFWF–Ä¶W—ÇÂrr’“°¢–b‚G&—fW"—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr¶W62‡G"‚t6†ö÷6RG&—fW"Fò6VRFWF–Ç2âr’’²sÂ÷7ãâs·&WGW&ã·Ð¢6öç7BÆ—fSÕ÷&6–ætWfVçE&÷w2æf–ÇFW"†SÓå7G&–ær†Rç6W&–W7ÇÂrr“ÓÓÕ7G&–ær†G&—fW"ç6W&–W7ÇÂrr’’ç6öÖR†SÓç&6–ætWfVçD—4Æ—fR†RÆæ÷r’’ÆæW‡CÖæW‡DG&—fW%&6R†G&—fW"Å÷&6–ætWfVçE&÷w2Ææ÷r’Æ6#ÖG&—fW"ç6W&–W3ÓÓÒvc"s°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò'&6–ævFWF–Æ†W&ò#ãÆ–Ör6Æ73Ò"r²†6#òv6"s¢rr’²r"7&3Ò"ö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær†G&—fW"æ¶W—ÇÂrr’’²r"ÇCÒ"r¶W64GG"†G&—fW"ææÖWÇÂrr’²r"ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#ãÆF—cãÆF—b6Æ73Ò'&6–ævFWF–Ç6W&–W2#âr¶W62†G&—fW"ç6W&–W5öæÖWÇÂu&6–ærr’²†Æ—fSòr+rÄ•dRs¢rr’²sÂöF—cãÆƒ#âr¶W62†G&—fW"ææÖWÇÂrr’²sÂöƒ#ãÆF—b6Æ73Ò'&6–ævFWF–ÇFVÒ#âr¶W62†G&—fW"çFV×ÇÂrr’²sÂöF—cãÂöF—cãÂöF—câr·&6–ætFWF–ÄæW‡B†æW‡B’²†G&—fW"çW&ÃòsÆF—b6Æ73Ò'&6–ævFWF–Æ7F–öç2#ãÆ6Æ73Ò&v†÷7B"‡&VcÒ"r¶W64GG"†G&—fW"çW&Â’²r"F&vWCÒ%ö&Ææ²"&VÃÒ&æö÷VæW"æ÷&VfW'&W"#âr¶W62‡G"‚tG&—fW"&öf–ÆRr’’²r(isÂöãÂöF—câs¢rr“°§Ð¦gVæ7F–öâ6†÷u&6–ætG&—fW$FWF–Â†¶W’—°¢÷&6–ætFWF–Ä¶W“Õ7G&–ær†¶W—ÇÂrr“·&VæFW%&6–æuFVÔ6öçG&öÂ‚“·&VæFW%&6–ætG&—fW$FWF–Â‚“°¢6öç7BG&—fW'3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætG&—fW'2r“¶–b†G&—fW'2–G&—fW'2æ–ææW$…DÔÃ×&6–ætG&—fW'4‡FÖÂ…÷&6–ætG&—fW%&÷w2Å÷&6–ætWfVçE&÷w2“°¢&VæFW%&6–æu66†VGVÆT6&G2‚“°§Ð¦gVæ7F–öâ&6–ætG&—fW$‡FÖÂ†G&—fW"ÆWfVçG2—°¢6öç7Bæ÷sÔFFRææ÷r‚’Ç6W&–W4WfVçG3Ò†WfVçG7ÇÅµÒ’æf–ÇFW"†SÓå7G&–ær†Rç6W&–W7ÇÂrr“ÓÓÕ7G&–ær†G&—fW"ç6W&–W7ÇÂrr’’ÆÆ—fS×6W&–W4WfVçG2ç6öÖR†SÓç&6–ætWfVçD—4Æ—fR†RÆæ÷r’’ÆæW‡CÖæW‡DG&—fW%&6R†G&—fW"ÆWfVçG2Ææ÷r’ÆÆö6ÆSÕöÆæsÓÓÒvæòsòvæ"Ôäòs§VæFVf–æVC°¢ÆWBæW‡D‡FÖÃÒsÇ7â6Æ73Ò&×WFVB#âr·G"‚tæòW6öÖ–ær&6Rf÷VæBâr’²sÂ÷7ãâs°¢–b†æW‡B—¶6öç7BCÖæWrFFR†æW‡Bç7F'B’Çv†VãÖæW‡BæÆÅöF“ò†æW‡BæFFU÷FW‡GÇÆBçFôÆö6ÆTFFU7G&–ær†Æö6ÆRÇ¶F“¢vçVÖW&–2rÆÖöçFƒ¢w6†÷'BwÒ’“¦BçFôÆö6ÆU7G&–ær†Æö6ÆRÇ·vVV¶F“¢w6†÷'BrÆF“¢vçVÖW&–2rÆÖöçFƒ¢w6†÷'BrÆ†÷W#¢s"ÖF–v—BrÆÖ–çWFS¢s"ÖF–v—BwÒ“¶æW‡D‡FÖÃÒsÇ7ãâr·G"‚tæW‡B&6Rr’²sÂ÷7ããÆ#âr¶W62†æW‡Bç&6WÇÆæW‡Bæ6—&7V—GÇÂu&6Rr’²sÂö#ãÇ7ãâr¶W62‡v†Vâ’²sÂ÷7ãâs·Ð¢6öç7B¶W“Õ7G&–ær†G&—fW"æ¶W—ÇÂrr’Ç6VÆV7FVCÕ÷&6–ætFWF–Ä¶W“ÓÓÖ¶W“òr6VÆV7FVBs¢rs°¢&WGW&âsÆF—b6Æ73Ò'&6–ævG&—fW"r·6VÆV7FVB²r"FFÖG&—fW"Ö¶W“Ò"r¶W64GG"†¶W’’²r"öæ6Æ–6³Ò'6†÷u&6–ætG&—fW$FWF–Â‡F†—2æFF6WBæG&—fW$¶W’’#ãÆ–Ör7&3Ò"ö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB†¶W’’²r"ÇCÒ"r¶W64GG"†G&—fW"ææÖWÇÂrr’²r"ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âp¢²sÆF—b6Æ73Ò'&6–ævG&—fW&–æfò#ãÆF—b6Æ73Ò'&6–ævG&—fW&æÖR#âr¶W62†G&—fW"ææÖWÇÂrr’²†Æ—fSòsÇ7â6Æ73Ò&G&—fW&Æ—fR#äÄ•dSÂ÷7ãâs¢rr’²sÂöF—cãÆF—b6Æ73Ò'&6–ævG&—fW'FVÒ#âr¶W62†G&—fW"ç6W&–W5öæÖWÇÂrr’²†G&—fW"çFVÓòr+rr¶W62†G&—fW"çFVÒ“¢rr’²sÂöF—cãÆF—b6Æ73Ò'&6–ævG&—fW&æW‡B#âr¶æW‡D‡FÖÂ²sÂöF—cãÂöF—cãÂöF—câs°§Ð¦gVæ7F–öâ&6–ætc—$‡FÖÂ‡—"ÆWfVçG2—°¢6öç7Bæ÷sÔFFRææ÷r‚’Æf—'7C×—%³ÒÇ6W&–W4WfVçG3Ò†WfVçG7ÇÅµÒ’æf–ÇFW"†SÓå7G&–ær†Rç6W&–W7ÇÂrr“ÓÓÒvcr’ÆÆ—fS×6W&–W4WfVçG2ç6öÖR†SÓç&6–ætWfVçD—4Æ—fR†RÆæ÷r’’ÆæW‡CÖæW‡DG&—fW%&6R†f—'7BÆWfVçG2Ææ÷r’ÆÆö6ÆSÕöÆæsÓÓÒvæòsòvæ"Ôäòs§VæFVf–æVC°¢ÆWBæW‡D‡FÖÃÒsÇ7â6Æ73Ò&×WFVB#âr·G"‚tæòW6öÖ–ær&6Rf÷VæBâr’²sÂ÷7ãâs°¢–b†æW‡B—¶6öç7BCÖæWrFFR†æW‡Bç7F'B’Çv†VãÖBçFôÆö6ÆU7G&–ær†Æö6ÆRÇ·vVV¶F“¢w6†÷'BrÆF“¢vçVÖW&–2rÆÖöçFƒ¢w6†÷'BrÆ†÷W#¢s"ÖF–v—BrÆÖ–çWFS¢s"ÖF–v—BwÒ“¶æW‡D‡FÖÃÒsÇ7ãâr·G"‚tæW‡B&6Rr’²sÂ÷7ããÆ#âr¶W62†æW‡Bç&6WÇÆæW‡Bæ6—&7V—GÇÂu&6Rr’²sÂö#ãÇ7ãâr¶W62‡v†Vâ’²sÂ÷7ãâs·Ð¢6öç7B–73×—"æÖ†G&—fW#ÓâsÆF—b6Æ73Ò'&6–ævG&—fW'—'W'6öâ#ãÆ–Ör7&3Ò"ö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær†G&—fW"æ¶W—ÇÂrr’’²r"ÇCÒ"r¶W64GG"†G&—fW"ææÖWÇÂrr’²r"ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#ãÇ7ãâr¶W62†G&—fW"ææÖWÇÂrr’²sÂ÷7ããÂöF—câr’æ¦ö–â‚rr“°¢6öç7BæÖW3×—"æÖ†G&—fW#ÓæG&—fW"ææÖWÇÂrr’æf–ÇFW"„&ööÆVâ’æ¦ö–â‚r+rr’ÇFVÓÖf—'7BçFV×ÇÂtf÷&×VÆs°¢&WGW&âsÆF—b6Æ73Ò'&6–ævG&—fW"&6–ævG&—fW'—"r²…÷&6–ætFWF–Ä¶W“ÓÓÒvc×FVÒsòr6VÆV7FVBs¢rr’²r"FFÖG&—fW"Ö¶W“Ò&c×FVÒ"öæ6Æ–6³Ò'6†÷u&6–ætG&—fW$FWF–Â‡F†—2æFF6WBæG&—fW$¶W’’#ãÆF—b6Æ73Ò'&6–ævG&—fW'—'–72#âr·–72²sÂöF—cãÆF—b6Æ73Ò'&6–ævG&—fW&–æfò#ãÆF—b6Æ73Ò'&6–ævG&—fW&æÖR#âr¶W62‡FVÒ’²†Æ—fSòsÇ7â6Æ73Ò&G&—fW&Æ—fR#äÄ•dSÂ÷7ãâs¢rr’²sÂöF—cãÆF—b6Æ73Ò'&6–ævG&—fW'FVÒ#äf÷&×VÆ+rr¶W62†æÖW2’²sÂöF—cãÆF—b6Æ73Ò'&6–ævG&—fW&æW‡B#âr¶æW‡D‡FÖÂ²sÂöF—cãÂöF—cãÂöF—câs°§Ð¦gVæ7F–öâ&6–ætG&—fW'4‡FÖÂ‡&÷w2ÆWfVçG2—°¢6öç7BÆ—7C×&÷w7ÇÅµÒÆcÖÆ—7Bæf–ÇFW"†G&—fW#ÓæG&—fW"ç6W&–W3ÓÓÒvcr’Æ÷F†W#ÖÆ—7Bæf–ÇFW"†G&—fW#ÓæG&—fW"ç6W&–W2ÓÒvcr’Ç'G3ÕµÓ°¢–b†cæÆVæwFƒãÓ"—'G2çW6‚‡¶¶W“¢vc×FVÒrÆ‡FÖÃ§&6–ætc—$‡FÖÂ†cç6Æ–6RƒÃ"’ÆWfVçG2—Ò“°¢VÇ6Rf÷"†6öç7BG&—fW"öbc—'G2çW6‚‡¶¶W“¥7G&–ær†G&—fW"æ¶W—ÇÂrr’Æ‡FÖÃ§&6–ætG&—fW$‡FÖÂ†G&—fW"ÆWfVçG2—Ò“°¢f÷"†6öç7BG&—fW"öb÷F†W"—'G2çW6‚‡¶¶W“¥7G&–ær†G&—fW"æ¶W—ÇÂrr’Æ‡FÖÃ§&6–ætG&—fW$‡FÖÂ†G&—fW"ÆWfVçG2—Ò“°¢'G2ç6÷'B‚†Æ"“Óâ†æ¶W“ÓÓÕ7G&–ær…÷&6–ætFWF–Ä¶W—ÇÂrr“òÓ£’Ò†"æ¶W“ÓÓÕ7G&–ær…÷&6–ætFWF–Ä¶W—ÇÂrr“òÓ£’“°¢&WGW&â'G2æÖ‡'CÓç'Bæ‡FÖÂ’æ¦ö–â‚rr“°§Ð¦gVæ7F–öâ&6–æt6†ææVÄÆ–æR†6‚—°¢&WGW&âsÆF—b6Æ73Ò'&6–ævWfVçF6†ææVÂ"FF×6–CÒ"r¶W64GG"…7G&–ær†6‚ç7G&VÕö–CÓÖçVÆÃòrs¦6‚ç7G&VÕö–B’’²r"FFÖæÖSÒ"r¶W64GG"†6‚ç‡G&VÕöæÖWÇÂrr’²r#âr¶6†ææVÄÆövò†6‚ÂvÖ–æ’r’²sÇ7â6Æ73Ò&6†â#âr¶W62†6‚ç‡G&VÕöæÖWÇÂt6†ææVÂr’²†6‚çVÆ—G“òsÇ7â6Æ73Ò'Fr#âr¶W62†6‚çVÆ—G’’²sÂ÷7ãâs¢rr’²sÂ÷7ããÇ7â6Æ73Ò&6†'Fç2#âr·Æ–'Fç2†6‚ç7G&VÕö–BÆ6‚ç‡G&VÕöæÖRÆ6‚çW&Â’²sÂ÷7ããÂöF—câs°§Ð¦gVæ7F–öâ&6–æt6†ææVÅ6V7F–öç2†6†ææVÇ2—°¢6öç7BFVf–æ—FSÖ6†ææVÇ2æf–ÇFW"†6ƒÓæ6‚æÖF6…ö¶–æCÓÓÒvWfVçBwÇÆ6‚æÖF6…ö¶–æCÓÓÒv'&öF67FW"r’ç6÷'B‡&VfW'&VD6†ææVÅ6÷'B’ÆFVF–6FVCÖ6†ææVÇ2æf–ÇFW"†6ƒÓæ6‚æÖF6…ö¶–æCÓÓÒw6W&–W2r’ç6÷'B‡&VfW'&VD6†ææVÅ6÷'B’Ç÷76–&ÆSÖ6†ææVÇ2æf–ÇFW"†6ƒÓâ²vWfVçBrÂv'&öF67FW"rÂw6W&–W2uÒæ–æ6ÇVFW2†6‚æÖF6…ö¶–æB’’ç6÷'B‡&VfW'&VD6†ææVÅ6÷'B“°¢ÆWBƒÒrs°¢–b†FVf–æ—FRæÆVæwF‚–‚³ÒsÆF—b6Æ73Ò&×WFVB#âr¶W62‡G"‚t6öæf—&ÖVB&6–ær6†ææVÇ2r’’²sÂöF—câr¶FVf–æ—FRæÖ‡&6–æt6†ææVÄÆ–æR’æ¦ö–â‚rr“°¢–b†FVF–6FVBæÆVæwF‚–‚³ÒsÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ&Ö&v–â×F÷£‡‚#âr¶W62‡G"‚tFVF–6FVB6W&–W26†ææVÇ2r’’²sÂöF—câr¶FVF–6FVBæÖ‡&6–æt6†ææVÄÆ–æR’æ¦ö–â‚rr“°¢–b‡÷76–&ÆRæÆVæwF‚—°¢6öç7Bw&÷W3ÖæWrÖ‚“¶f÷"†6öç7B6‚öb÷76–&ÆR—¶6öç7B6FVv÷'“Õ7G&–ær†6‚æ6FVv÷'—ÇÇG"‚t÷F†W"÷76–&ÆR6†ææVÇ2r’“¶–b‚w&÷W2æ†2†6FVv÷'’’–w&÷W2ç6WB†6FVv÷'’ÅµÒ“¶w&÷W2ævWB†6FVv÷'’’çW6‚†6‚“·Ð¢‚³ÒsÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ&Ö&v–â×F÷£‡‚#âr¶W62‡G"‚u÷76–&ÆR6†ææVÇ2'’6FVv÷'’r’’²sÂöF—câs°¢f÷"†6öç7B¶6FVv÷'’Æ—FV×5Òöbw&÷W2–‚³ÒsÆF—b6Æ73Ò&&7&÷r#ãÆF—b6Æ73Ò&&6†VB#ãÇ7â6Æ73Ò&&6æÖR#âr¶W62†6FVv÷'’’²sÂ÷7ããÇ7â6Æ73Ò&×WFVB#âr¶—FV×2æÆVæwF‚²rr¶W62‡G"†—FV×2æÆVæwFƒÓÓÓòv6†ææVÂs¢v6†ææVÇ2r’’²sÂ÷7ããÇ7â6Æ73Ò&&66†Wg&öâ#âb3“cc#³Â÷7ããÂöF—cãÆF—b6Æ73Ò&&66†ç2†–FR#âr¶—FV×2æÖ‡&6–æt6†ææVÄÆ–æR’æ¦ö–â‚rr’²sÂöF—cãÂöF—câs°¢Ð¢&WGW&âƒ°§Ð¦gVæ7F–öâ&6–ætWfVçD‡FÖÂ†WfVçB—°¢6öç7BG3ÖæWrFFR†WfVçBç7F'B’ÆÆö6ÆSÕöÆæsÓÓÒvæòsòvæ"Ôäòs§VæFVf–æVC°¢6öç7Bv†VãÖWfVçBæÆÅöF“ò†WfVçBæFFU÷FW‡GÇÇG2çFôÆö6ÆTFFU7G&–ær†Æö6ÆRÇ·vVV¶F“¢w6†÷'BrÆF“¢vçVÖW&–2rÆÖöçFƒ¢w6†÷'BwÒ’“§G2çFôÆö6ÆU7G&–ær†Æö6ÆRÇ·vVV¶F“¢w6†÷'BrÆF“¢vçVÖW&–2rÆÖöçFƒ¢w6†÷'BrÆ†÷W#¢s"ÖF–v—BrÆÖ–çWFS¢s"ÖF–v—BwÒ“°¢6öç7B6†ææVÇ3ÖWfVçBæ6†ææVÇ7ÇÅµÒÆÆöF–æsÕ÷&6–ætf–Æ&–Æ—G”ÆöF–ærÆ–æF–6F÷#ÖÆöF–æsòsÇ7â6Æ73Ò'&6–ævWfVçFÆöF–ær#ãÇ7â6Æ73Ò'&6–ævWfVçG7–ææW""&–Ö†–FFVãÒ'G'VR#ãÂ÷7ãâr¶W62‡G"‚t6†V6¶–ær6†ææVÇ2âââr’’²sÂ÷7ãâs¢†6†ææVÇ2æÆVæwFƒòsÇ7â6Æ73Ò&62&6–ævWfVçGGb#åEcÂ÷7ãâs¢rr’ÆFWF–Ç3Ö6†ææVÇ2æÆVæwFƒòsÆF—b6Æ73Ò'&6–ævWfVçF6†ææVÇ2†–FR#âr·&6–æt6†ææVÅ6V7F–öç2†6†ææVÇ2’²sÂöF—câs¢rs°¢6öç7B6÷W&6SÖWfVçBçW&Ãòr+rÆ6Æ73Ò'&6–ævWfVçG6÷W&6R"‡&VcÒ"r¶W64GG"†WfVçBçW&Â’²r"F&vWCÒ%ö&Ææ²"&VÃÒ&æö÷VæW"#âr¶W62‡G"‚tWfVçBvRr’’²r(isÂöâs¢rs°¢&WGW&âsÆF—b6Æ73Ò'&6–ævWfVçBr²†6†ææVÇ2æÆVæwFƒòr†66†ææVÇ2s¢rr’²†ÆöF–æsòrÆöF–æv6†ææVÇ2s¢rr’²r"FFÖWf¶W“Ò"r¶W64GG"‡&6–ætf–Æ&–Æ—G”¶W’†WfVçB’’²r#ãÆF—b6Æ73Ò'&6–ævWfVçGF÷#ãÆ#âr¶W62†WfVçBç&6WÇÆWfVçBæ6—&7V—GÇÂu&6Rr’²sÂö#âr¶–æF–6F÷"²sÂöF—cãÆF—b6Æ73Ò&Ö÷f–VÖWF#âr¶W62‡v†Vâ’²r+rr¶W62‡&6–æu6W76–öäÆ&VÂ†WfVçB’’²†WfVçBæ6—&7V—BbfWfVçBæ6—&7V—BÓÖWfVçBç&6Sòr+rr¶W62†WfVçBæ6—&7V—B“¢rr’·6÷W&6R²sÂöF—câr¶FWF–Ç2²sÂöF—câs°§Ð¦gVæ7F–öâ&6–ætf–Æ&–Æ—G”¶W’†WfVçB—·&WGW&â¶WfVçBç6W&–W7ÇÂrrÆWfVçBç&6WÇÂrrÆWfVçBç6W76–öçÇÂrrÆWfVçBç7F'GÇÂruÒæ¦ö–â‚wÂr“·Ð¦gVæ7F–öâÇ•&6–ætf–Æ&–Æ—G’†ÖÆWfVçG2—¶f÷"†6öç7BWfVçBöb†WfVçG7ÇÅµÒ’–WfVçBæ6†ææVÇ3Ò†ÖbfÖ·&6–ætf–Æ&–Æ—G”¶W’†WfVçB•Ò—ÇÅµÓ·Ð¦gVæ7F–öâ&6–æuf—6–&ÆU6W&–W4WfVçG2†WfVçG2Ç6W&–W2ÆFVfVÇDÆ–Ö—B—°¢6öç7BFF×&÷sÓç&÷ræWfVçGÇÇ&÷rÇ7F××&÷sÓç&÷rçG7ÇÆæWrFFR†FF‡&÷r’ç7F'B’ævWEF–ÖR‚“°¢6öç7B&÷w3Ò†WfVçG7ÇÅµÒ’ç6Æ–6R‚’ç6÷'B‚†Æ"“Óç7F×†’×7F×†"’“°¢–b‡6W&–W2ÓÒvcwÇÂ&÷w2æÆVæwF‚—&WGW&â&÷w2ç6Æ–6RƒÆFVfVÇDÆ–Ö—B“°¢òòc†26WfW&Â6W76–öç2&Vf÷&R7VæF’â¶VWF†R6ö×ÆWFRæV&W7BvVV¶Væ@¢òò6òâV&Ç’×6W76–öâÆ–Ö—B6âæWfW"†–FRVÆ–g––ærÂ7&–çBÂ÷"F†R&6Rà¢6öç7Bf—'7CÖFF‡&÷w5³Ò’ÇvVV¶VæC×&÷w2æf–ÇFW"‡&÷sÓç¶6öç7BWfVçCÖFF‡&÷r“·&WGW&â7G&–ær†WfVçBç&÷VæGÇÂrr“ÓÓÕ7G&–ær†f—'7Bç&÷VæGÇÂrr’be7G&–ær†WfVçBç&6WÇÂrr“ÓÓÕ7G&–ær†f—'7Bç&6WÇÂrr“·Ò“°¢&WGW&â‡vVV¶VæBæÆVæwFƒ÷vVV¶VæC§&÷w2’ç6Æ–6RƒÃb“°§Ð¦gVæ7F–öâ&VæFW%&6–æu66†VGVÆT6&G2‚—°¢6öç7B–æfóÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–æt–æfòr“¶–b‚–æfò—&WGW&ã¶6öç7Bæ÷sÔFFRææ÷r‚’Æw&÷W3ÖæWrÖ‚“°¢òò&VÖVÖ&W"v†–6‚WfVçB6&G2&R7W'&VçFÇ’W‡æFVB6ò&R×&VæFW"†Rærâv†Và¢òò6†ææVÂf–Æ&–Æ—G’'&—fW2’FöW6âwB6öÆÆ6Rv†BF†RW6W"÷VæVBà¢6öç7B÷Vä¶W—3ÖæWr6WB‚“°¢–æfòçVW'•6VÆV7F÷$ÆÂ‚rç&6–ævWfVçBr’æf÷$V6‚†gVæ7F–öâ†VÂ—°¢6öç7B&÷ƒÖVÂçVW'•6VÆV7F÷"‚rç&6–ævWfVçF6†ææVÇ2r“°¢–b†&÷‚bb&÷‚æ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’—¶6öç7B³ÖVÂævWDGG&–'WFR‚vFFÖWf¶W’r“¶–b†²–÷Vä¶W—2æFB†²“·Ð¢Ò“°¢6öç7B6VÆV7FVDG&—fW#Õ÷&6–ætG&—fW%&÷w2æf–æB‡&÷sÓå7G&–ær‡&÷ræ¶W—ÇÂrr“ÓÓÕ7G&–ær…÷&6–ætFWF–Ä¶W—ÇÂrr’“°¢6öç7B6VÆV7FVE6W&–W3Õ÷&6–ætFWF–Ä¶W“ÓÓÒvc×FVÒsòvcs¥7G&–ær‚‡6VÆV7FVDG&—fW"bg6VÆV7FVDG&—fW"ç6W&–W2—ÇÂrr“°¢f÷"†6öç7BWfVçBöb÷&6–ætWfVçE&÷w2—¶6öç7BG3ÖæWrFFR†WfVçBç7F'B’ævWEF–ÖR‚’ÆÆ—fS×&6–ætWfVçD—4Æ—fR†WfVçBÆæ÷r“¶–b‚çVÖ&W"æ—4f–æ—FR‡G2—ÇÂ‚Æ—fRbgG3Ææ÷rÓ#B£3c’–6öçF–çVS¶6öç7B¶W“ÖWfVçBç6W&–W7ÇÂw&6–ærs¶–b‚w&÷W2æ†2†¶W’’–w&÷W2ç6WB†¶W’ÅµÒ“¶w&÷W2ævWB†¶W’’çW6‚†WfVçB“·Ð¢ÆWBƒÒrs¶6öç7B÷&FW&VE6W&–W3Õõ$4”äuõ4U$”U2æf–ÇFW"‡&÷sÓå÷&6–æu6VÆV7FVBæ†2‡&÷u³Ò’’ç6÷'B‚†Æ"“Óâ†³ÓÓÓ×6VÆV7FVE6W&–W3òÓ£’Ò†%³ÓÓÓ×6VÆV7FVE6W&–W3òÓ£’“°¢f÷"†6öç7B&÷röb÷&FW&VE6W&–W2—¶6öç7BWfVçG3×&6–æuf—6–&ÆU6W&–W4WfVçG2†w&÷W2ævWB‡&÷u³Ò—ÇÅµÒÇ&÷u³ÒÃB“¶‚³ÒsÆF—b6Æ73Ò'&6–æv6&B6W&–W2Òr¶W64GG"‡&÷u³Ò’²‡6VÆV7FVE6W&–W3ÓÓ×&÷u³Óòr6VÆV7FVBs¢rr’²r#ãÆƒ3âr·&6–æu6W&–W4Æövò‡&÷u³Ò’²sÇ7ãâr¶W62‡&÷u³Ò’²sÂ÷7ããÂöƒ3âr²†WfVçG2æÆVæwFƒöWfVçG2æÖ‡&6–ætWfVçD‡FÖÂ’æ¦ö–â‚rr“¢sÇ7â6Æ73Ò&×WFVB#âr¶W62‡G"‚tæòW6öÖ–ærWfVçG2f÷VæBâr’’²sÂ÷7ãâr’²sÂöF—câs·Ð¢–æfòæ–ææW$…DÔÃÖ‡ÇÂsÇ7â6Æ73Ò&×WFVB#âr¶W62‡G"‚t6†ö÷6RBÆV7BöæR&6–ær6W&–W2&÷fRâr’’²sÂ÷7ãâs°¢òò&W7F÷&RW‡æFVB7FFRà¢–b†÷Vä¶W—2ç6—¦R––æfòçVW'•6VÆV7F÷$ÆÂ‚rç&6–ævWfVçBr’æf÷$V6‚†gVæ7F–öâ†VÂ—°¢6öç7B³ÖVÂævWDGG&–'WFR‚vFFÖWf¶W’r“°¢–b†²bf÷Vä¶W—2æ†2†²’—¶6öç7B&÷ƒÖVÂçVW'•6VÆV7F÷"‚rç&6–ævWfVçF6†ææVÇ2r“¶–b†&÷‚–&÷‚æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“·Ð¢Ò“°§Ð¦7–æ2gVæ7F–öâÆöE&6–ætf–Æ&–Æ—G’‚—°¢÷&6–ætf–Æ&–Æ—G”ÆöF–æs×G'VS·&VæFW%&6–æu66†VGVÆT6&G2‚“°¢G'—¶6öç7BÖv—B’‚rö’÷&6–æuöf–Æ&–Æ—G’r“¶Ç•&6–ætf–Æ&–Æ—G’†æf–Æ&–Æ—G—ÇÇ·ÒÅ÷&6–ætWfVçE&÷w2“·Ö6F6‚†R—·Öf–æÆÇ—µ÷&6–ætf–Æ&–Æ—G”ÆöF–æsÖfÇ6S·&VæFW%&6–æu66†VGVÆT6&G2‚“·&VæFW%&6–ætG&—fW$FWF–Â‚“¶6öç7BG&—fW'3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætG&—fW'2r“¶–b†G&—fW'2–G&—fW'2æ–ææW$…DÔÃ×&6–ætG&—fW'4‡FÖÂ…÷&6–ætG&—fW%&÷w2Å÷&6–ætWfVçE&÷w2“·Ð§Ð¦7–æ2gVæ7F–öâÆöE&6–ær‚—°¢6öç7BFövvÆW3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–æu6W&–W2r’Æ–æfóÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–æt–æfòr’ÆG&—fW'3ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætG&—fW'2r“°¢–b†G&—fW'2–G&—fW'2æ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr¶W62‡G"‚tÆöF–ærG&—fW'2æBæW‡B&6Râââr’’²sÂ÷7ãâs°¢–b†–æfò––æfòæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr¶W62‡G"‚tÆöF–ær&6–ær66†VGVÆW2âââr’’²sÂ÷7ãâs°¢G'—°¢6öç7B·"ÆEÓÖv—B&öÖ—6RæÆÂ…¶’‚rö’÷&6–ærr’Æ’‚rö’÷&6–æuöG&—fW'2r•Ò“µ÷&6–æu6VÆV7FVCÖæWr6WB‡"ç6VÆV7FVGÇÅµÒ“µ÷&6–ætG&—fW%&÷w3ÖBæG&—fW'7ÇÅµÓµ÷&6–ætWfVçE&÷w3×"æWfVçG7ÇÅµÓ°¢FövvÆW2æ–ææW$…DÔÃÕõ$4”äuõ4U$”U2æÖ‡&÷sÓâsÆ'WGFöâ6Æ73Ò'&6–æwFövvÆRr²…÷&6–æu6VÆV7FVBæ†2‡&÷u³Ò“òröâs¢rr’²r"FFÖ¶W“Ò"r·&÷u³Ò²r"öæ6Æ–6³Ò'FövvÆU&6–æu6W&–W2‡F†—2æFF6WBæ¶W’’#âr¶W62‡&÷u³Ò’²sÂö'WGFöãâr’æ¦ö–â‚rr“°¢6öç7Bc&÷w3Õ÷&6–ætG&—fW%&÷w2æf–ÇFW"‡&÷sÓç&÷rç6W&–W3ÓÓÒvcr’ÇfÆ–D¶W—3ÖæWr6WB…÷&6–ætG&—fW%&÷w2æÖ‡&÷sÓå7G&–ær‡&÷ræ¶W—ÇÂrr’’“¶–b…÷&6–æu6VÆV7FVBæ†2‚vcr’—fÆ–D¶W—2æFB‚vc×FVÒr“°¢–b‚÷&6–ætFWF–Ä¶W—ÇÂfÆ–D¶W—2æ†2…÷&6–ætFWF–Ä¶W’’•÷&6–ætFWF–Ä¶W“Õ÷&6–æu6VÆV7FVBæ†2‚vcr“òvc×FVÒs¥7G&–ær‚…÷&6–ætG&—fW%&÷w5³×ÇÇ·Ò’æ¶W—ÇÂrr“°¢÷&6–ætf–Æ&–Æ—G”ÆöF–æs×G'VS·&VæFW%&6–æuFVÔ6öçG&öÂ‚“·&VæFW%&6–ætG&—fW$FWF–Â‚“¶G&—fW'2æ–ææW$…DÔÃ×&6–ætG&—fW'4‡FÖÂ…÷&6–ætG&—fW%&÷w2Å÷&6–ætWfVçE&÷w2“°¢&VæFW%&6–æu66†VGVÆT6&G2‚“¶ÆöE&6–ætf–Æ&–Æ—G’‚“°¢Ö6F6‚†R—¶G&—fW'2æ–ææW$…DÔÃÒrs¶–æfòæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&W'"#âr¶W62‡G"‚t6÷VÆBæ÷BÆöB&6–ær66†VGVÆW2âr’’²sÂ÷7ãâs·Ð§Ð¦7–æ2gVæ7F–öâFövvÆU&6–æu6W&–W2†¶W’—°¢–b…÷&6–æu6VÆV7FVBæ†2†¶W’’•÷&6–æu6VÆV7FVBæFVÆWFR†¶W’“¶VÇ6R÷&6–æu6VÆV7FVBæFB†¶W’“°¢6öç7B#Öv—B’‚rö’÷&6–æu÷6W&–W2rÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡·6W&–W3¤'&’æg&öÒ…÷&6–æu6VÆV7FVB—Ò—Ò“°¢–b‡"æW'&÷"—·Fö7B‡"æW'&÷"“·&WGW&ã·Ð¢÷&öf–ÆT6öæf–rç&6–æu÷6W&–W3×"ç6W&–W7ÇÅµÓ¶v—BÆöE&6–ær‚“¶ÆöDff÷&—FW2‚“°§Ð¦ÆWB÷6†÷u6V6öç3×·Ó°¦ÆWBö7F—fU6W&–W4–CÖçVÆÃ°¦ÆWBöfe6†÷u6WCÖæWr6WB‚“°¦ÆWBöfe6†÷uF—FÆU6WCÖæWr6WB‚“°¦ÆWBöÆFW7DW—6öFW4ÆöFVCÖfÇ6S°¦gVæ7F–öâÆFW7DW—6öFT6&B†W—°¢6öç7B6÷fW#ÖWæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†Wæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VçDVÆVÖVçBçFW‡D6öçFVçCÕ7G&–æræg&öÔ6öFUö–çBƒ#ƒ#S’#âs¢rb3#ƒ#S²s°¢ÆWB7F–öãÒsÆ'WGFöâ6Æ73Ò&v†÷7B"F—6&ÆVCâr·G"‚tæ÷Bf–Æ&ÆRr’²sÂö'WGFöãâs°¢–b†Wæf–Æ&ÆR—°¢6öç7B6÷W&6W3Ò†Wç6÷W&6W2bfWç6÷W&6W2æÆVæwF‚“öWç6÷W&6W3¥·¶–C¦Wæ–BÆW‡FVç6–öã¦WæW‡FVç6–öâÆÆ&VÃ¢udÄ2wÕÓ°¢6öç7B6÷W&6T'WGFöç3×6÷W&6W2æÖ‡7&3ÓâsÆ'WGFöâ6Æ73Ò&'FçfÆ2ÆFW7FW—6öFWfÆ2"FFÖ–CÒ"r¶W64GG"…7G&–ær‡7&2æ–B’’²r"FFÖW‡CÒ"r¶W64GG"‡7&2æW‡FVç6–öçÇÂv×Br’²r#âb3“cSƒ²r¶W62‡7&2æÆ&VÂ’²sÂö'WGFöãâr’æ¦ö–â‚rr“°¢7F–öã×6÷W&6W2æÆVæwFƒã3òsÆ'WGFöâ6Æ73Ò&'FçfÆ2ÆFW7G6÷W&6VW‡æB#âb3“cSƒ²dÄ3Âö'WGFöããÆF—b6Æ73Ò&ÆFW7G6÷W&6W2†–FR#âr·6÷W&6T'WGFöç2²sÂöF—câs§6÷W&6T'WGFöç3°¢Ð¢&WGW&âsÆF—b6Æ73Ò&Ö÷f–V6&BÆFW7G6†÷v6&B"FF×6W&–W3Ò"r¶W64GG"…7G&–ær†Wç6W&–W5ö–GÇÂrr’’²r"FFÖ6FÆösÒ"r¶W64GG"†Wæ6FÆöuö–GÇÂrr’²r#ãÆF—b6Æ73Ò&Ö÷f–W÷7FW"#âr¶6÷fW"²sÂöF—cãÆF—b6Æ73Ò&Ö÷f–V–æfò#ãÆF—b6Æ73Ò&Ö÷f–WF—FÆR#âr¶W62†Wç6†÷uöæÖR’²sÂöF—câp¢²sÆF—b6Æ73Ò&Ö÷f–VÖWF#å2r¶W62†Wç6V6öâ’²tRr¶W62†WæW—6öFUöçVÒ’²rÒr¶W62†WçF—FÆWÇÂtW—6öFRr’²sÂöF—câp¢²sÆF—b6Æ73Ò&Ö÷f–V7F–öç2#âr¶7F–öâ²sÂöF—cãÂöF—cãÂöF—câs°§Ð¦gVæ7F–öâ÷6ÆôF”çVÖ&W"‡fÇVR—°¢6öç7B'G3ÖæWr–çFÂäFFUF–ÖTf÷&ÖB‚vVâÔ4rÇ·F–ÖU¦öæS¢tWW&÷Rô÷6ÆòrÇ–V#¢vçVÖW&–2rÆÖöçFƒ¢s"ÖF–v—BrÆF“¢s"ÖF–v—BwÒ’æf÷&ÖEFõ'G2‡fÇVR“°¢6öç7B×·Ó·'G2æf÷$V6‚‡ƒÓç¶–b‡‚çG—RÓÒvÆ—FW&Âr—·‚çG—UÓ×'6T–çB‡‚çfÇVRÃ“·Ò“°¢&WGW&âFFRåUD2‡ç–V"ÇæÖöçF‚ÓÇæF’’óƒcC°§Ð¦gVæ7F–öâg&–VæFÇ”—&FFR†W—°¢–b‚Wæ—&FFRbbWæ—'7F×—&WGW&ârs°¢ÆWBF&vWCÖWæ—'7F×öæWrFFR†Wæ—'7F×“¦æWrFFR†Wæ—&FFR²uC#££r“°¢–b„çVÖ&W"æ—4æâ‡F&vWBævWEF–ÖR‚’’—F&vWCÖæWrFFR†Wæ—&FFR²uC#££r“°¢6öç7Bæ÷sÖæWrFFR‚’Â&VÖ–æ–æs×F&vWBÖæ÷s°¢–b‡&VÖ–æ–æsãbg&VÖ–æ–æsÃƒcC—°¢6öç7BÖ–çWFW3ÔÖF‚æÖ‚ƒÄÖF‚æ6V–Â‡&VÖ–æ–æróc’“°¢–b†Ö–çWFW3Ãc—&WGW&âG"‚v–âr’²rr¶Ö–çWFW2²rr·G"†Ö–çWFW3ÓÓÓòvÖ–çWFRs¢vÖ–çWFW2r“°¢6öç7B†÷W'3ÔÖF‚æ6V–Â†Ö–çWFW2óc“°¢&WGW&âG"‚v–âr’²rr¶†÷W'2²rr·G"††÷W'3ÓÓÓòv†÷W"s¢v†÷W'2r“°¢Ð¢6öç7BF—3ÔÖF‚ç&÷VæB†÷6ÆôF”çVÖ&W"‡F&vWB’Ö÷6ÆôF”çVÖ&W"†æ÷r’“°¢–b†F—3ÓÓÓ—&WGW&âG"‚uFöF’r“°¢–b†F—3ÓÓÓ—&WGW&âG"‚uFöÖ÷'&÷rr“°¢6öç7BvVV¶F“×F&vWBçFôÆö6ÆTFFU7G&–ær…öÆæsÓÓÒvæòsòvæ"Ôäòs§VæFVf–æVBÇ·vVV¶F“¢vÆöærrÇF–ÖU¦öæS¢tWW&÷Rô÷6ÆòwÒ“°¢&WGW&âvVV¶F’²rÇS#rr·G"‚v–âr’²rr¶F—2²rr·G"†F—3ÓÓÓòvF’s¢vF—2r“°§Ð¦gVæ7F–öâW6öÖ–ætW—6öFT6&B†W—°¢6öç7B6÷fW#ÖWæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†Wæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VçDVÆVÖVçBçFW‡D6öçFVçCÕ7G&–æræg&öÔ6öFUö–çBƒ#ƒ#S’#âs¢rb3#ƒ#S²s°¢6öç7Bv†VãÖg&–VæFÇ”—&FFR†W“°¢&WGW&âsÆF—b6Æ73Ò&Ö÷f–V6&BÆFW7G6†÷v6&B"FF×6W&–W3Ò"r¶W64GG"…7G&–ær†Wç6W&–W5ö–GÇÂrr’’²r"FFÖ6FÆösÒ"r¶W64GG"†Wæ6FÆöuö–GÇÂrr’²r#ãÆF—b6Æ73Ò&Ö÷f–W÷7FW"#âr¶6÷fW"²sÂöF—cãÆF—b6Æ73Ò&Ö÷f–V–æfò#ãÆF—b6Æ73Ò&Ö÷f–WF—FÆR#âr¶W62†Wç6†÷uöæÖR’²sÂöF—câp¢²sÆF—b6Æ73Ò&Ö÷f–VÖWF#å2r¶W62†Wç6V6öâ’²tRr¶W62†WæW—6öFUöçVÒ’²rÒr¶W62†WçF—FÆWÇÂtW—6öFRr’²sÂöF—câp¢²sÆF—b6Æ73Ò&Ö÷f–V7F–öç2#ãÆ'WGFöâ6Æ73Ò&v†÷7B"F—6&ÆVCâr·G"‚t—'2r’²rr¶W62‡v†Vâ’²sÂö'WGFöããÂöF—cãÂöF—cãÂöF—câs°§Ð¦7–æ2gVæ7F–öâÆöDÆFW7DW—6öFW2†Æ–Ö—BÇ&Vg&W6‚—°¢Æ–Ö—CÖÆ–Ö—GÇÃ“°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÆFW7DW—6öFTÆ—7Br’ÂÖ÷&SÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÆFW7DW—6öFTÖ÷&Rr“°¢6öç7BW6öÖ–æu6V7F–öãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wW6öÖ–ætW—6öFW56V7F–öâr’ÂW6öÖ–ætÆ—7CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wW6öÖ–ætW—6öFTÆ—7Br“°¢VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äÆöF–ærÆFW7BW—6öFW2ââãÂ÷7ãâs¶Ö÷&Ræ6Æ74Æ—7BæFB‚v†–FRr“°¢W6öÖ–æu6V7F–öâæ6Æ74Æ—7BæFB‚v†–FRr“·W6öÖ–ætÆ—7Bæ–ææW$…DÔÃÒrs°¢6öç7B#Öv—B’‚rö’öÆFW7EöW—6öFW3öÆ–Ö—CÒr¶Æ–Ö—B²‡&Vg&W6ƒòrg&Vg&W6ƒÓs¢rr’“°¢–b‡"æW'&÷"—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#ä6÷VÆBæ÷BÆöBÆFW7BW—6öFW2ãÂ÷7ãâs·&WGW&âfÇ6S·Ð¢–b‡"æW—6öFW2æÆVæwF‚–VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&Ö÷f–Vw&–B"7G–ÆSÒ&Ö&v–â×F÷£#âr·"æW—6öFW2æÖ†ÆFW7DW—6öFT6&B’æ¦ö–â‚rr’²sÂöF—câs°¢VÇ6RVÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äæòÆFW7BW—6öFW2f÷VæBf÷"–÷W"ff÷&—FR6†÷w2ãÂ÷7ãâs°¢–b‡"çW6öÖ–ærbg"çW6öÖ–æræÆVæwF‚—°¢W6öÖ–ætÆ—7Bæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&Ö÷f–Vw&–B"7G–ÆSÒ&Ö&v–â×F÷£#âr·"çW6öÖ–æræÖ‡W6öÖ–ætW—6öFT6&B’æ¦ö–â‚rr’²sÂöF—câs°¢W6öÖ–æu6V7F–öâæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢Ð¢–b†Æ–Ö—CÃ3bbg"æ†5öÖ÷&R–Ö÷&Ræ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢öÆFW7DW—6öFW4ÆöFVC×G'VS°¢&WGW&â‡"æW—6öFW2æÆVæwF‡ÇÂ‡"çW6öÖ–ærbg"çW6öÖ–æræÆVæwF‚’“°§Ð¦7–æ2gVæ7F–öâW‡æDÆFW7DW—6öFW2†'Fâ—°¢6öç7BöÆCÖ'FâçFW‡D6öçFVçC¶'FâæF—6&ÆVC×G'VS¶'FâçFW‡D6öçFVçCÒtÆöF–ærâââs°¢v—BÆöDÆFW7DW—6öFW2ƒ3b“°¢'FâæF—6&ÆVCÖfÇ6S¶'FâçFW‡D6öçFVçCÖöÆC¶'Fâæ6Æ74Æ—7BæFB‚v†–FRr“°§Ð¦7–æ2gVæ7F–öâÆ”ÆFW7DW—6öFR†–BÆW‡BÆ'Fâ—°¢6öç7BöÆCÖ'FâçFW‡D6öçFVçC¶'FâçFW‡D6öçFVçCÒt÷Væ–ærâââs°¢G'—°¢6öç7B£Öv—B’‚rö’÷Æ•÷6V6öârÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡¶W—6öFW3¥·¶–C¦–BÆW‡FVç6–öã¦W‡GÕ×Ò—Ò“°¢–b†¢æW'&÷"–ÆW'B†¢æW'&÷'ÇÂt6÷VÆBæ÷BÆVæ6‚dÄ2âr“°¢Ö6F6‚†R—¶ÆW'B‚t6÷VÆBæ÷BÆVæ6‚dÄ2âr“·Ð¢6WEF–ÖV÷WB‚‚“Óç¶'FâçFW‡D6öçFVçCÖöÆC·ÒÃ#“°§Ð¦7–æ2gVæ7F–öâÆöE6†÷tff÷&—FW2‚—°¢6öç7B#Öv—B’‚rö’öff÷&—FW2r’Â6†÷w3×"ç6†÷w7ÇÅµÓ°¢öfe6†÷u6WCÖæWr6WB‡6†÷w2æÖ‡3Óå7G&–ær‡2æ6FÆöuö–GÇÇ2ç6†÷uö¶W—ÇÇ2ç6W&–W5ö–B’’“°¢öfe6†÷uF—FÆU6WCÖæWr6WB‡6†÷w2æÖ‡3Óå7G&–ær‡2ç6†÷uö¶W—ÇÂrr’’æf–ÇFW"„&ööÆVâ’“°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷tfdÆ—7Br“°¢–b‚6†÷w2æÆVæwF‚—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#äæòff÷&—FR6†÷w2–WBãÂ÷7ãâs·&WGW&ã·Ð¢ÆWBƒÒrs°¢f÷"†6öç7B2öb6†÷w2—°¢6öç7B÷7FW#ÒsÇ7â6Æ73Ò'6†÷vfg÷7FW"#âb3#ƒ#S²r²‡2æ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"‡2æ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÂ÷7ãâs°¢6öç7B–G3Ò‡2ç6W&–W5ö–G2bg2ç6W&–W5ö–G2æÆVæwFƒ÷2ç6W&–W5ö–G3¥·2ç6W&–W5ö–EÒ’æ¦ö–â‚rÂr“°¢6öç7B¶W“Õ7G&–ær‡2æ6FÆöuö–GÇÇ2ç6†÷uö¶W—ÇÇ2ç6W&–W5ö–B“°¢‚³ÒsÆF—b6Æ73Ò'6†÷vfb"FF×6W&–W3Ò"r¶W64GG"†–G2’²r"FFÖ6FÆösÒ"r¶W64GG"‡2æ6FÆöuö–GÇÂrr’²r#âr·÷7FW"²sÆF—b6Æ73Ò'6†÷vff–æfò#ãÆF—b6Æ73Ò'6†÷vffæÖR#âr¶W62‡2ææÖR’²sÂöF—cãÂöF—câp¢²sÆ'WGFöâ6Æ73Ò&fg&Ò6†÷w&VÖ÷fR"FFÖ¶W“Ò"r¶W64GG"†¶W’’²r"F—FÆSÒ%&VÖ÷fR#âgF–ÖW3³Âö'WGFöããÂöF—câs°¢Ð¢VÂæ–ææW$…DÔÃÖƒ°§Ð¦7–æ2gVæ7F–öâFövvÆU6†÷tff÷&—FR‡6†÷rÇ7F$VÂ—°¢6öç7B#Öv—Bfe÷7B‡¶7F–öã¢wFövvÆU÷6†÷rrÇ6†÷s§6†÷wÒ“°¢öfe6†÷u6WCÖæWr6WB‚‡"ç6†÷uö–G7ÇÅµÒ’æÖ…7G&–ær’“°¢–b…öfe6†÷u6WBæ†2…7G&–ær‡6†÷ræ6FÆöuö–GÇÇ6†÷rç6†÷uö¶W—ÇÇ6†÷rç6W&–W5ö–B’’•÷&öf–ÆT6öæf–rç6WGWöFVÖõö6öçFVçCÖfÇ6S°¢–b‡7F$VÂ—7F$VÂæ6Æ74Æ—7BçFövvÆR‚vöârÅöfe6†÷u6WBæ†2…7G&–ær‡6†÷ræ6FÆöuö–GÇÇ6†÷rç6†÷uö¶W—ÇÇ6†÷rç6W&–W5ö–B’’“°¢öÆFW7DW—6öFW4ÆöFVCÖfÇ6S°¢v—BÆöE6†÷tff÷&—FW2‚“°§Ð¦7–æ2gVæ7F–öâ&VÖ÷fU6†÷tff÷&—FR‡6†÷t¶W’—°¢v—Bfe÷7B‡¶7F–öã¢w&VÖ÷fU÷6†÷rrÇ6†÷uö¶W“§6†÷t¶W—Ò“°¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rç6†÷w7F"r’æf÷$V6‚†VÃÓç¶–b†VÂævWDGG&–'WFR‚vFFÖ¶W’r“ÓÓÕ7G&–ær‡6†÷t¶W’’–VÂæ6Æ74Æ—7Bç&VÖ÷fR‚vöâr“·Ò“°¢öÆFW7DW—6öFW4ÆöFVCÖfÇ6S°¢v—BÆöE6†÷tff÷&—FW2‚“°§Ð¦7–æ2gVæ7F–öâ6V&6…6†÷w2‚—°¢6öç7BÒ†Fö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷ur’çfÇVWÇÂrr’çG&–Ò‚’ÂVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷u&W7VÇG2r“°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷tFWF–Ç2r’æ–ææW$…DÔÃÒrs°¢6öç7BÆFW7CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÆFW7DW—6öFW56V7F–öâr“°¢–b‚—¶ÆFW7Bæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“¶VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ&Ö&v–â×F÷£G‚#äVçFW"6†÷rF—FÆRãÂöF—câs·&WGW&ã·Ð¢ÆFW7Bæ6Æ74Æ—7BæFB‚v†–FRr“°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ&Ö&v–â×F÷£G‚#å6V&6†–ær–÷W"6†÷w2ââãÂöF—câs°¢6öç7B#Öv—B’‚rö’÷6†÷w3÷Òr¶Væ6öFUU$”6ö×öæVçB‡’“°¢6öç7B&6³ÒsÆF—b6Æ73Ò'6†÷w&W7VÇF&6²#ãÆ'WGFöâ6Æ73Ò&v†÷7B"öæ6Æ–6³Ò&&6µFô×•6†÷w2‚’#âb3ƒS“#²r·G"‚t&6²Fò6†÷w2r’²sÂö'WGFöããÂöF—câs°¢–b‡"æW'&÷"—¶VÂæ–ææW$…DÔÃÖ&6²²sÆF—b6Æ73Ò&W'"#âr¶W62‡"æW'&÷"’²sÂöF—câs·&WGW&ã·Ð¢–b‚"ç6†÷w2æÆVæwF‚—¶VÂæ–ææW$…DÔÃÖ&6²²sÆF—b6Æ73Ò&×WFVB#äæò6†÷w2f÷VæBf÷"gV÷C²r¶W62‡’²rgV÷C²ãÂöF—câs·&WGW&ã·Ð¢v—BÆöE6†÷tff÷&—FW2‚“°¢ÆWBƒÖ&6²²sÆF—b6Æ73Ò'6†÷vw&–B#âs°¢f÷"†6öç7B2öb"ç6†÷w2—°¢6öç7BfcÒ…öfe6†÷u6WBæ†2…7G&–ær‡2æ6FÆöuö–GÇÇ2ç6†÷uö¶W’’—ÇÅöfe6†÷uF—FÆU6WBæ†2…7G&–ær‡2ç6†÷uö¶W’’’“òröâs¢rs°¢6öç7B–G3Ò‡2ç6W&–W5ö–G7ÇÅ·2ç6W&–W5ö–EÒ’æ¦ö–â‚rÂr“°¢6öç7B6÷fW#×2æ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"‡2æ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rs°¢‚³ÒsÆF—b6Æ73Ò'6†÷v6&B"FF×6W&–W3Ò"r¶W64GG"†–G2’²r"FFÖ6FÆösÒ"r¶W64GG"‡2æ6FÆöuö–GÇÂrr’²r#ãÆF—b6Æ73Ò'6†÷w÷7FW"#âb3#ƒ#S²r¶6÷fW"²sÂöF—câp¢²sÆF—cãÆF—b6Æ73Ò'6†÷væÖR#âr¶W62‡2ææÖR’²sÂöF—cãÆF—b6Æ73Ò&Ö÷f–VÖWF"7G–ÆSÒ&Ö&v–â×F÷£w‚#âr²‡2ç–V#öW62‡2ç–V"“¢rr’²‡2ç&F–æsò‚rfæ'7²&F–æs¢r¶W62‡2ç&F–ær’“¢rr’²sÂöF—câp¢²sÇ7â6Æ73Ò&fg7F"6†÷w7F"r¶fb²r"FFÖ¶W“Ò"r¶W64GG"‡2æ6FÆöuö–GÇÇ2ç6†÷uö¶W’’²r"FFÖ6FÆösÒ"r¶W64GG"‡2æ6FÆöuö–GÇÂrr’²r"FF×6†÷rÖ¶W“Ò"r¶W64GG"‡2ç6†÷uö¶W’’²r"FF×6W&–W3Ò"r¶W64GG"…7G&–ær‡2ç6W&–W5ö–CÓÖçVÆÃòrs§2ç6W&–W5ö–B’’²r"FF×6W&–W2Ö–G3Ò"r¶W64GG"†–G2’²r"FFÖæÖSÒ"r¶W64GG"‡2ææÖWÇÂrr’²r"FFÖ6÷fW#Ò"r¶W64GG"‡2æ6÷fW'ÇÂrr’²r"FF×–V#Ò"r¶W64GG"‡2ç–V'ÇÂrr’²r"FF×&F–æsÒ"r¶W64GG"‡2ç&F–æwÇÂrr’²r"F—FÆSÒ$ff÷&—FR#âb3“s33³Â÷7ããÂöF—cãÂöF—câs°¢Ð¢VÂæ–ææW$…DÔÃÖ‚²sÂöF—câs°§Ð¦gVæ7F–öâ&6µFô×•6†÷w2‚—°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷ur’çfÇVSÒrs°¢6†÷u6†÷w2‚“°§Ð¦7–æ2gVæ7F–öâÆöE6†÷r‡6W&–W4–BÇ&Vg&W6‚—°¢–b‚&Vg&W6‚—&VÖVÖ&W$Æö6F–öâ‚w6†÷w2rÇ·6W&–W4–C¥7G&–ær‡6W&–W4–B—Ò“°¢ö7F—fU6W&–W4–C×6W&–W4–C°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚vÆFW7DW—6öFW56V7F–öâr’æ6Æ74Æ—7BæFB‚v†–FRr“°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷tFWF–Ç2r“°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&×WFVB#äÆöF–ær6V6öç2æBW—6öFW2ââãÂöF—câs°¢6öç7B#Öv—B’‚rö’÷6†÷sö–CÒr¶Væ6öFUU$”6ö×öæVçB‡6W&–W4–B’²‡&Vg&W6ƒòrg&Vg&W6ƒÓs¢rr’“°¢–b‡"æW'&÷"—¶VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&W'"#âr¶W62‡"æW'&÷"’²sÂöF—câs·&WGW&âfÇ6S·Ð¢v—BÆöE6†÷tff÷&—FW2‚“°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷u&W7VÇG2r’æ–ææW$…DÔÃÒrs°¢÷6†÷u6V6öç3×·Ó°¢6öç7B†W&ô6÷fW#×"æ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"‡"æ6÷fW"’²r"ÇCÒ""öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rs°¢6öç7B†W&ôfcÒ…öfe6†÷u6WBæ†2…7G&–ær‡"ç6†÷uö¶W’’—ÇÅöfe6†÷uF—FÆU6WBæ†2…7G&–ær‡"ç6†÷uö¶W’’’“òröâs¢rs°¢ÆWBƒÒsÆF—b6Æ73Ò'6†÷v†W&ò#ãÆF—b6Æ73Ò'6†÷v†W&ö'B#âb3#ƒ#S²r¶†W&ô6÷fW"²sÂöF—cãÆF—cãÆƒ#âr¶W62‡"ææÖWÇÂu6†÷rr¢²sÇ7â6Æ73Ò&fg7F"6†÷w7F"r¶†W&ôfb²r"FFÖ¶W“Ò"r¶W64GG"‡"ç6†÷uö¶W’’²r"FF×6W&–W3Ò"r¶W64GG"…7G&–ær‡"ç6W&–W5ö–B’’²r"FF×6W&–W2Ö–G3Ò"r¶W64GG"‚‡"ç6W&–W5ö–G7ÇÅµÒ’æ¦ö–â‚rÂr’’²r"FFÖæÖSÒ"r¶W64GG"‡"ææÖWÇÂu6†÷rr’²r"FFÖ6÷fW#Ò"r¶W64GG"‡"æ6÷fW'ÇÂrr’²r"FF×–V#Ò""FF×&F–æsÒ""F—FÆSÒ$ff÷&—FR#âb3“s33³Â÷7ããÂöƒ#âp¢²sÆF—b6Æ73Ò&×WFVB#âr·"ç6V6öç2æÆVæwF‚²r6V6öâr²‡"ç6V6öç2æÆVæwFƒÓÓÓòrs¢w2r’²sÂöF—cãÂöF—cãÆ'WGFöâ6Æ73Ò&v†÷7B6†÷v&6¶'Fâ"öæ6Æ–6³Ò&&6µFô×•6†÷w2‚’#âb3ƒS“#²r·G"‚t&6²Fò6†÷w2r’²sÂö'WGFöããÂöF—câs°¢f÷"†6öç7B6V6öâöb"ç6V6öç2—°¢÷6†÷u6V6öç5µ7G&–ær‡6V6öâæçVÖ&W"•Ó×·Ó°¢f÷"†6öç7BWöb6V6öâæW—6öFW2–f÷"†6öç7B7&2öb†Wç6÷W&6W7ÇÅµÒ’—°¢–b‚÷6†÷u6V6öç5µ7G&–ær‡6V6öâæçVÖ&W"•Õ·7&2æÆ&VÅÒ•÷6†÷u6V6öç5µ7G&–ær‡6V6öâæçVÖ&W"•Õ·7&2æÆ&VÅÓÕµÓ°¢÷6†÷u6V6öç5µ7G&–ær‡6V6öâæçVÖ&W"•Õ·7&2æÆ&VÅÒçW6‚‡¶–C§7&2æ–BÆW‡FVç6–öã§7&2æW‡FVç6–öâÆW—6öFUöçVÓ¦WæW—6öFUöçV×Ò“°¢Ð¢6öç7B6V6öä6÷fW#×6V6öâæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"‡6V6öâæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rs°¢‚³ÒsÆF—b6Æ73Ò'6V6öæ&Æö6²#ãÆF—b6Æ73Ò'6V6öæÆ–÷WB#ãÆF—b6Æ73Ò'6V6öæ'B#âb3#ƒ#S²r·6V6öä6÷fW"²sÂöF—cãÆF—b6Æ73Ò'6V6öæ6öçFVçB#ãÆF—b6Æ73Ò'6V6öæ†VB#ãÆ#âr¶W62‡6V6öâçF—FÆR’²sÂö#ãÂöF—cãÆF—b6Æ73Ò&W—6öFW2#âs°¢f÷"†ÆWBV“Ó¶V“Ç6V6öâæW—6öFW2æÆVæwFƒ¶V’²²—°¢6öç7BW×6V6öâæW—6öFW5¶V•Ó°¢‚³ÒsÆF—b6Æ73Ò&W—6öFR#ãÆF—b6Æ73Ò&W—6öFVæÖR#ãÆ#äRr¶W62†WæW—6öFUöçVÒ’²sÂö#âr¶W62†WçF—FÆWÇÂtW—6öFRr’²sÂöF—cãÆF—b6Æ73Ò&W—6öFWVÆ—F–W2#âs°¢f÷"†6öç7B7&2öb†Wç6÷W&6W7ÇÅµÒ’–‚³ÒsÆ'WGFöâ6Æ73Ò&'FçfÆ2W—6öFWfÆ2"FF×6V6öãÒ"r¶W64GG"…7G&–ær‡6V6öâæçVÖ&W"’’²r"FFÖW—6öFSÒ"r¶W64GG"…7G&–ær†WæW—6öFUöçVÒ’’²r"FF×6÷W&6SÒ"r¶W64GG"‡7&2æÆ&VÂ’²r#âb3“cSƒ²r¶W62‡7&2æÆ&VÂ’²sÂö'WGFöãâs°¢‚³ÒsÂöF—cãÂöF—câs°¢Ð¢‚³ÒsÂöF—cãÂöF—cãÂöF—cãÂöF—câs°¢Ð¢–b‚"ç6V6öç2æÆVæwF‚–‚³ÒsÆF—b6Æ73Ò&×WFVB#äæòW—6öFW2f÷VæBãÂöF—câs°¢VÂæ–ææW$…DÔÃÖƒ°¢&WGW&âG'VS°§Ð¦7–æ2gVæ7F–öâÆöDW‡FW&æÅ6†÷r†6FÆöt–BÇ&Vg&W6‚—°¢–b‚&Vg&W6‚—&VÖVÖ&W$Æö6F–öâ‚w6†÷w2rÇ¶6FÆöt–C¥7G&–ær†6FÆöt–B—Ò“°¢ö7F—fU6W&–W4–CÖçVÆÃµ÷6†÷u6V6öç3×·Ó°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚vÆFW7DW—6öFW56V7F–öâr’æ6Æ74Æ—7BæFB‚v†–FRr“°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷tFWF–Ç2r“°¢VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&×WFVB#äÆöF–ær6†÷rââãÂöF—câs°¢6öç7B#Öv—B’‚rö’÷6†÷uöW‡FW&æÃö–CÒr¶Væ6öFUU$”6ö×öæVçB†6FÆöt–B’“°¢–b‡"æW'&÷"—¶VÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&W'"#âr¶W62‡"æW'&÷"’²sÂöF—câs·&WGW&âfÇ6S·Ð¢–b‡"ç&÷f–FW%÷6W&–W5ö–G2bg"ç&÷f–FW%÷6W&–W5ö–G2æÆVæwF‚—&WGW&âÆöE6†÷r‡"ç&÷f–FW%÷6W&–W5ö–G2æ¦ö–â‚rÂr’ÇG'VR“°¢v—BÆöE6†÷tff÷&—FW2‚“¶Fö7VÖVçBævWDVÆVÖVçD'”–B‚w6†÷u&W7VÇG2r’æ–ææW$…DÔÃÒrs°¢6öç7B6÷fW#×"æ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"‡"æ6÷fW"’²r"ÇCÒ""öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rrÆ¶W“Õ7G&–ær‡"æ6FÆöuö–GÇÇ"ç6†÷uö¶W’’ÆfcÕöfe6†÷u6WBæ†2†¶W’“òröâs¢rs°¢ÆWBƒÒsÆF—b6Æ73Ò'6†÷v†W&ò#ãÆF—b6Æ73Ò'6†÷v†W&ö'B#âb3#ƒ#S²r¶6÷fW"²sÂöF—cãÆF—cãÆƒ#âr¶W62‡"ææÖWÇÂu6†÷rr¢²sÇ7â6Æ73Ò&fg7F"6†÷w7F"r¶fb²r"FFÖ¶W“Ò"r¶W64GG"†¶W’’²r"FFÖ6FÆösÒ"r¶W64GG"‡"æ6FÆöuö–GÇÂrr’²r"FF×6†÷rÖ¶W“Ò"r¶W64GG"‡"ç6†÷uö¶W—ÇÂrr’²r"FF×6W&–W3Ò""FF×6W&–W2Ö–G3Ò""FFÖæÖSÒ"r¶W64GG"‡"ææÖWÇÂu6†÷rr’²r"FFÖ6÷fW#Ò"r¶W64GG"‡"æ6÷fW'ÇÂrr’²r"FF×–V#Ò"r¶W64GG"‡"ç–V'ÇÂrr’²r"FF×&F–æsÒ"r¶W64GG"‡"ç&F–æwÇÂrr’²r"F—FÆSÒ$ff÷&—FR#âb3“s33³Â÷7ããÂöƒ#âp¢²sÆF—b6Æ73Ò&×WFVB#âr·"ç6V6öç2æÆVæwF‚²r6V6öâr²‡"ç6V6öç2æÆVæwFƒÓÓÓòrs¢w2r’²sÂöF—cãÂöF—cãÆ'WGFöâ6Æ73Ò&v†÷7B6†÷v&6¶'Fâ"öæ6Æ–6³Ò&&6µFô×•6†÷w2‚’#âb3ƒS“#²r·G"‚t&6²Fò6†÷w2r’²sÂö'WGFöããÂöF—câs°¢f÷"†6öç7B6V6öâöb"ç6V6öç2—°¢6öç7B6V6öä6÷fW#×6V6öâæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"‡6V6öâæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rs°¢‚³ÒsÆF—b6Æ73Ò'6V6öæ&Æö6²#ãÆF—b6Æ73Ò'6V6öæÆ–÷WB#ãÆF—b6Æ73Ò'6V6öæ'B#âb3#ƒ#S²r·6V6öä6÷fW"²sÂöF—cãÆF—b6Æ73Ò'6V6öæ6öçFVçB#ãÆF—b6Æ73Ò'6V6öæ†VB#ãÆ#âr¶W62‡6V6öâçF—FÆR’²sÂö#ãÂöF—cãÆF—b6Æ73Ò&W—6öFW2#âs°¢f÷"†6öç7BWöb6V6öâæW—6öFW2–‚³ÒsÆF—b6Æ73Ò&W—6öFR#ãÆF—b6Æ73Ò&W—6öFVæÖR#ãÆ#äRr¶W62†WæW—6öFUöçVÒ’²sÂö#âr¶W62†WçF—FÆWÇÂtW—6öFRr’²sÂöF—cãÆF—b6Æ73Ò&W—6öFWVÆ—F–W2#ãÆ'WGFöâ6Æ73Ò&v†÷7B"F—6&ÆVCâr·G"‚tæ÷Bf–Æ&ÆRr’²sÂö'WGFöããÂöF—cãÂöF—câs°¢‚³ÒsÂöF—cãÂöF—cãÂöF—cãÂöF—câs°¢Ð¢VÂæ–ææW$…DÔÃÖƒ·&WGW&âG'VS°§Ð¦7–æ2gVæ7F–öâ6†V6´ÆÅ6†÷w2†'Fâ—°¢6öç7BöÆCÖ'Fâæ–ææW$…DÔÃ¶'FâæF—6&ÆVC×G'VS¶'FâçFW‡D6öçFVçCÒt6†V6¶–ærÆÂ6†÷w2âââs°¢G'—°¢6öç7B£Öv—B’‚rö’ö6†V6µ÷6†÷u÷WFFW2rÇ¶ÖWF†öC¢uõ5BwÒ“°¢–b†¢æW'&÷"—F‡&÷ræWrW'&÷"†¢æW'&÷'ÇÂv6†V6²f–ÆVBr“°¢–b…ö7F—fU6W&–W4–B–v—BÆöE6†÷r…ö7F—fU6W&–W4–BÇG'VR“°¢v—BÆöE6†÷tff÷&—FW2‚“°¢6öç7BÆFW7CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vÆFW7DW—6öFW56V7F–öâr“°¢–b†ÆFW7BbbÆFW7Bæ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’–v—BÆöDÆFW7DW—6öFW2ƒ’ÇG'VR“°¢–b†¢ææWuöW—6öFW3ã—Fö7B‚tf÷VæBr¶¢ææWuöW—6öFW2²ræWrW—6öFRr²†¢ææWuöW—6öFW3ÓÓÓòrs¢w2r’²rf÷"–÷W"6†÷w2rÃs“°¢VÇ6RFö7B‚u7V66W76gVÆÇ’&Vg&W6†VBÆ–Æ—7G2ÂæòæWrW—6öFW2f÷VæBrÃs“°¢Ö6F6‚†R—·Fö7B‚t6÷VÆBæ÷B&Vg&W6‚6†÷rÆ–Æ—7G2âr“·Ð¢'FâæF—6&ÆVCÖfÇ6S¶'Fâæ–ææW$…DÔÃÖöÆC°§Ð¦7–æ2gVæ7F–öâ6†V6µ6†÷w4öå7F'GW‚—°¢G'—°¢6öç7B£Öv—B’‚rö’ö6†V6µ÷6†÷u÷WFFW2rÇ¶ÖWF†öC¢uõ5BwÒ“°¢–b†¢æW'&÷"—F‡&÷ræWrW'&÷"†¢æW'&÷'ÇÂv6†V6²f–ÆVBr“°¢öÆFW7DW—6öFW4ÆöFVCÖfÇ6S°¢–b†¢ææWuöW—6öFW3ã—Fö7B‚tf÷VæBr¶¢ææWuöW—6öFW2²ræWrW—6öFRr²†¢ææWuöW—6öFW3ÓÓÓòrs¢w2r’²rf÷"–÷W"6†÷w2rÃs“°¢VÇ6RFö7B‚u7V66W76gVÆÇ’&Vg&W6†VBÆ–Æ—7G2ÂæòæWrW—6öFW2f÷VæBrÃs“°¢Ö6F6‚†R—·Fö7B‚t6÷VÆBæ÷B&Vg&W6‚6†÷rÆ–Æ—7G2ârÃs“·Ð§Ð¦7–æ2gVæ7F–öâ&Vg&W6„öå7F'GW‡&Vg&W6„—GbÇ&Vg&W6…7÷'G2—°¢G'—°¢–b‡&Vg&W6„—Gb–v—B&Vg&W6„—Gd6öçFVçB†çVÆÂÇG'VR“°¢–b‡&Vg&W6…7÷'G2–v—B&Vg&W6„÷F†W$6öçFVçB†çVÆÂÇG'VR“°¢Fö7B‚u7F'GW&Vg&W6‚f–æ—6†VBârÃS“°¢Ö6F6‚†R—·Fö7B‚u7F'GW&Vg&W6‚f–ÆVC¢rµ7G&–ær†RbfRæÖW76vWÇÆR’Ãs“·Ð§Ð¦7–æ2gVæ7F–öâÆ”W—6öFUVWVR‡6V6öâÆW—6öFTçVÒÇ6÷W&6RÆ'Fâ—°¢6öç7BW—6öFW3Ò‚…÷6†÷u6V6öç5µ7G&–ær‡6V6öâ•×ÇÇ·Ò•·6÷W&6U×ÇÅµÒ’æf–ÇFW"†WÓäçVÖ&W"†WæW—6öFUöçVÒ“ãÔçVÖ&W"†W—6öFTçVÒ’’ÂöÆCÖ'FâçFW‡D6öçFVçC¶'FâçFW‡D6öçFVçCÒt÷Væ–ærâââs°¢G'—¶6öç7B£Öv—B’‚rö’÷Æ•÷6V6öârÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡¶W—6öFW3¦W—6öFW7Ò—Ò“¶–b†¢æW'&÷"–ÆW'B†¢æW'&÷'ÇÂt6÷VÆBæ÷BÆVæ6‚dÄ2âr“·Ð¢6F6‚†R—¶ÆW'B‚t6÷VÆBæ÷BÆVæ6‚dÄ2âr“·Ð¢6WEF–ÖV÷WB‚‚“Óæ'FâçFW‡D6öçFVçCÖöÆBÃ#“°§Ð¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚v¶W–F÷vârÆgVæ7F–öâ†R—¶–b†Ræ¶W“ÓÓÒtW66Rr–6Æ÷6UÆ–W"‚“·Ò“°¢òòÒÒÒÒff÷&—FW2ò×’Æ—7BÒÒÒÐ¦ÆWBöfd6E6WCÖæWr6WB‚“°¦ÆWBöfd6†å6WCÖæWr6WB‚“°¦7–æ2gVæ7F–öâ&Vg&W6„fe7FFR‚—°¢G'—¶6öç7B#Öv—B’‚rö’öff÷&—FW2r“°¢öfd6E6WCÖæWr6WB‡"æ6FVv÷&–W7ÇÅµÒ“°¢öfd6†å6WCÖæWr6WB‚‡"æ6†ææVÇ7ÇÅµÒ’æÖ†gVæ7F–öâ†2—·&WGW&â7G&–ær†2ç7G&VÕö–B“·Ò’“°¢Ö6F6‚†R—·Ð§Ð¦ÆWBöÆöDff÷&—FW5&öÖ—6SÖçVÆÃ°¦7–æ2gVæ7F–öâöÆöDff÷&—FW4æ÷r‚—°¢6öç7B#Öv—B’‚rö’öff÷&—FW2r“°¢öfd6E6WCÖæWr6WB‡"æ6FVv÷&–W7ÇÅµÒ“°¢öfd6†å6WCÖæWr6WB‚‡"æ6†ææVÇ7ÇÅµÒ’æÖ†gVæ7F–öâ†2—·&WGW&â7G&–ær†2ç7G&VÕö–B“·Ò’“°¢ö×”Æ—7DfdFF×#°¢ö×”Æ—7DÆöFVC×G'VS°¢ö×”Æ—7E6VÆV7FVD6†ææVÇ3Ò‡"æ×–Æ—7Eö6†ææVÇ7ÇÅµÒ’æÖ…7G&–ær’ç6Æ–6RƒÃR“°¢ö×”Æ—7EFVÔÖöÖVçG3ÕµÓµö×”Æ—7DcÖöÖVçG3ÕµÓµö×”Æ—7DÖ÷f–TÖöÖVçG3ÕµÓµö×”Æ—7DvÖTÖöÖVçG3ÕµÓµö×”Æ—7E6†÷tÖöÖVçG3ÕµÓµö×”Æ—7E&6–ætG&—fW'3ÕµÓ°¢&VæFW$×”Æ—7E&öf–ÆR‚“°¢Ç”×”Æ—7DÆ–÷WB‚“°¢&VæFW$×”Æ—7D6†ææVÇ2‚“°¢6öç7B&6–ætFF&öÖ—6SÕöcVæ&ÆVCö’‚rö’÷&6–ærr“¦çVÆÃ°¢6öç7BÆöG3ÕµÓ°¢–b…öfö÷F&ÆÄVæ&ÆVGÇÅöcVæ&ÆVB–ÆöG2çW6‚†ÆöD×”Æ—7EFV×2‡"Ç&6–ætFF&öÖ—6R’“°¢–b…öcVæ&ÆVB–ÆöG2çW6‚†ÆöD×”Æ—7E&6–ær‡&6–ætFF&öÖ—6R’“¶VÇ6Wµö×”Æ—7DcÖöÖVçG3ÕµÓ·66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“·Ð¢ÆöG2çW6‚†ÆöD×”Æ—7DÖ÷f–W2‚’“°¢–b…övÖW4Væ&ÆVB–ÆöD×”Æ—7DvÖW2‡"“¶VÇ6Wµö×”Æ—7DvÖTÖöÖVçG3ÕµÓ·66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“·Ð¢ÆöG2çW6‚†ÆöD×”Æ—7E6†÷w2‚’“°¢v—B&öÖ—6RæÆÅ6WGFÆVB†ÆöG2“°§Ð¦7–æ2gVæ7F–öâÆöDff÷&—FW2‚—°¢–b…öÆöDff÷&—FW5&öÖ—6R—&WGW&âöÆöDff÷&—FW5&öÖ—6S°¢öÆöDff÷&—FW5&öÖ—6SÕöÆöDff÷&—FW4æ÷r‚“°¢G'—·&WGW&âv—BöÆöDff÷&—FW5&öÖ—6S·Öf–æÆÇ—µöÆöDff÷&—FW5&öÖ—6SÖçVÆÃ·Ð§Ð¦ÆWBö×”Æ—7DÆöFVCÖfÇ6RÅö×”Æ—7DfdFF×¶6†ææVÇ3¥µÒÇFV×3¥µÒÆc÷FV×3¥µ×ÒÅö×”Æ—7E6VÆV7FVD6†ææVÇ3ÕµÒÅö×”Æ—7EFVÔÖöÖVçG3ÕµÒÅö×”Æ—7DcÖöÖVçG3ÕµÒÅö×”Æ—7DÖ÷f–TÖöÖVçG3ÕµÒÅö×”Æ—7DvÖTÖöÖVçG3ÕµÒÅö×”Æ—7E6†÷tÖöÖVçG3ÕµÒÅö×”Æ—7E&6–ætG&—fW'3ÕµÓ°¦ÆWBö×•F–ÖVÆ–æTf–ÇFW#ÒvÆÂrÅö×•F–ÖVÆ–æU6WGF–æw3×·&V6VçC§G'VRÆÆ—fS§G'VRÇW6öÖ–æs§G'VRÆÖ…W$6FVv÷'“£ÒÅö×•F–ÖVÆ–æU&Vg4ÆöFVCÖfÇ6S°¦ÆWBö×”Æ—7EF–ÖVÆ–æU&VæFW%VæF–æsÖfÇ6S°¦gVæ7F–öâ66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚—°¢–b…ö×”Æ—7EF–ÖVÆ–æU&VæFW%VæF–ær—&WGW&ã°¢ö×”Æ—7EF–ÖVÆ–æU&VæFW%VæF–æs×G'VS°¢&WVW7Dæ–ÖF–öäg&ÖR‚‚“Óçµö×”Æ—7EF–ÖVÆ–æU&VæFW%VæF–æsÖfÇ6S·&VæFW$×”Æ—7EF–ÖVÆ–æR‚“·Ò“°§Ð¦gVæ7F–öâ&VæFW$×”Æ—7D6†ææVÇ2‚—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v×”Æ—7D6†ææVÇ2r“¶–b‚VÂ—&WGW&ã°¢6öç7B'”–CÖæWrÖ‚…ö×”Æ—7DfdFFæ6†ææVÇ7ÇÅµÒ’æÖ†3Óåµ7G&–ær†2ç7G&VÕö–B’Æ5Ò’“°¢ÆWBƒÒrs°¢f÷"†ÆWB“Ó¶“ÃS¶’²²—°¢6öç7B3Ö'”–BævWB…ö×”Æ—7E6VÆV7FVD6†ææVÇ5¶•×ÇÂrr“°¢–b†2–‚³ÒsÆF—b6Æ73Ò&×–F6†6†ææVÂ"FF×6–CÒ"r¶W64GG"…7G&–ær†2ç7G&VÕö–B’’²r"FFÖæÖSÒ"r¶W64GG"†2ææÖWÇÂrr’²r"F—FÆSÒ"r¶W64GG"‡G"‚uÆ’r’’²r#âr¶6†ææVÄÆövò†2’²sÇ7â6Æ73Ò&×–F6†6†ææVÆæÖR#âr¶W62†2ææÖR’²sÂ÷7ããÆ'WGFöâ6Æ73Ò&'FçfÆ2"FF×6–CÒ"r¶W64GG"…7G&–ær†2ç7G&VÕö–B’’²r#âb3“cSƒ²dÄ3Âö'WGFöããÂöF—câs°¢VÇ6R‚³ÒsÆF—b6Æ73Ò&×–F6†6†ææVÂ×WFVB"öæ6Æ–6³Ò'FövvÆT×”Æ—7D6†ææVÅ–6¶W"‚’#â²r·G"‚t6†ö÷6R6†ææVÇ2r’²sÂöF—câs°¢Ð¢VÂæ–ææW$…DÔÃÖƒ°§Ð¦gVæ7F–öâFövvÆT×”Æ—7D6†ææVÅ–6¶W"‚—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v×”Æ—7D6†ææVÅ–6¶W"r“°¢–b‚VÂæ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’—¶VÂæ6Æ74Æ—7BæFB‚v†–FRr“·&WGW&ã·Ð¢6öç7B6VÆV7FVCÖæWr6WB…ö×”Æ—7E6VÆV7FVD6†ææVÇ2“°¢–b‚…ö×”Æ—7DfdFFæ6†ææVÇ7ÇÅµÒ’æÆVæwF‚—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr·G"‚u7F"6†ææVÇ2f—'7BÂF†Vâ6†ö÷6RWFòf—fR†W&Râr’²sÂ÷7ãâs·Ð¢VÇ6RVÂæ–ææW$…DÔÃÕö×”Æ—7DfdFFæ6†ææVÇ2æÖ†3ÓâsÆÆ&VÂ6Æ73Ò&×–F6†6†ö–6R#ãÆ–çWBG—SÒ&6†V6¶&÷‚"FF×6–CÒ"r¶W64GG"…7G&–ær†2ç7G&VÕö–B’’²r"r²‡6VÆV7FVBæ†2…7G&–ær†2ç7G&VÕö–B’“òv6†V6¶VBs¢rr’²vöæ6†ævSÒ'6WD×”Æ—7D6†ææVÂ‡F†—2æFF6WBç6–BÇF†—2æ6†V6¶VBÇF†—2’#âr¶6†ææVÄÆövò†2ÂvÖ–æ’r’²sÇ7ãâr¶W62†2ææÖR’²sÂ÷7ããÂöÆ&VÃâr’æ¦ö–â‚rr“°¢VÂæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°§Ð¦7–æ2gVæ7F–öâ6WD×”Æ—7D6†ææVÂ‡6–BÆ6†V6¶VBÆ–çWB—°¢6–CÕ7G&–ær‡6–B“¶ÆWB6†÷6VãÕö×”Æ—7E6VÆV7FVD6†ææVÇ2ç6Æ–6R‚“°¢–b†6†V6¶VBbb6†÷6Vâæ–æ6ÇVFW2‡6–B’—°¢–b†6†÷6VâæÆVæwFƒãÓR—¶–çWBæ6†V6¶VCÖfÇ6S·Fö7B‡G"‚t6†ö÷6RWFòf—fR6†ææVÇ2âr’“·&WGW&ã·Ð¢6†÷6VâçW6‚‡6–B“°¢ÖVÇ6R–b‚6†V6¶VB–6†÷6VãÖ6†÷6Vâæf–ÇFW"†–CÓæ–BÓ×6–B“°¢6öç7B#Öv—Bfe÷7B‡¶7F–öã¢w6WEö×–Æ—7Eö6†ææVÇ2rÇ7G&VÕö–G3¦6†÷6VçÒ“°¢ö×”Æ—7E6VÆV7FVD6†ææVÇ3Ö6†÷6Vâç6Æ–6RƒÃR“·&VæFW$×”Æ—7D6†ææVÇ2‚“°§Ð¦7–æ2gVæ7F–öâFövvÆU&6–ætc–6¶W"‚—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætc–6¶W"r“¶–b‚VÂ—&WGW&ã°¢–b‚VÂæ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’—¶VÂæ6Æ74Æ—7BæFB‚v†–FRr“·&WGW&ã·Ð¢VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr·G"‚tÆöF–ærâââr’²sÂ÷7ãâs¶VÂæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢G'—°¢6öç7B#Öv—B’‚rö’öc÷FV×2r’Â6VÆV7FVCÕ7G&–ær‚‚‡"æff÷&—FW7ÇÅµÒ•³×ÇÇ·Ò’æ–GÇÂrr“°¢VÂæ–ææW$…DÔÃÒ‡"çFV×7ÇÅµÒ’æÖ‡FVÓÓâsÆ'WGFöâ6Æ73Ò&×–F6†6†ö–6Rc6†ö–6Rr²…7G&–ær‡FVÒæ–B“ÓÓ×6VÆV7FVCòröâs¢rr’²r"FFÖ–CÒ"r¶W64GG"‡FVÒæ–B’²r"FFÖæÖSÒ"r¶W64GG"‡FVÒææÖR’²r"öæ6Æ–6³Ò'6WE&6–ætcFVÒ‡F†—2æFF6WBæ–BÇF†—2æFF6WBææÖR’#ãÆ–Ör7&3Ò"ö’öc÷FVÕöÆövóö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær‡FVÒæ–GÇÂrr’’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#ãÇ7ãâr¶W62‡FVÒææÖR’²sÂ÷7ããÂö'WGFöãâr’æ¦ö–â‚rr—ÇÂsÇ7â6Æ73Ò&×WFVB#âr·G"‚tæòcFVÒ6VÆV7FVBâr’²sÂ÷7ãâs°¢Ö6F6‚†R—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr·G"‚t6÷VÆBæ÷BÆöBf÷&×VÆ6ÆVæF"âr’²sÂ÷7ãâs·Ð§Ð¦7–æ2gVæ7F–öâ6WE&6–ætcFVÒ†–BÆæÖR—°¢ÆWB7W'&VçCÒrs·G'—¶6öç7B7FFSÖv—B’‚rö’öc÷FV×2r“¶7W'&VçCÕ7G&–ær‚‚‡7FFRæff÷&—FW7ÇÅµÒ•³×ÇÇ·Ò’æ–GÇÂrr“·Ö6F6‚†R—·Ð¢6öç7B6ÆV#Ö7W'&VçCÓÓÕ7G&–ær†–B“°¢v—Bfe÷7B‡¶7F–öã¢w6WEöc÷FVÒrÇFVÓ¦6ÆV#÷·Ó§¶–C¦–BÆæÖS¦æÖW×Ò“°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚w&6–ætc–6¶W"r’æ6Æ74Æ—7BæFB‚v†–FRr“°¢v—BÆöE&6–ær‚“¶ÆöDff÷&—FW2‚“°§Ð¦gVæ7F–öâ×•7÷'EFVÔÖWF†f—‡GW&W2—°¢6öç7B6÷VçG3ÖæWrÖ‚’Ç&÷w3Ò†f—‡GW&W7ÇÅµÒ’æf–ÇFW"†cÓæbbfbæÆVwVUöæÖRbbög&–VæFÂö’çFW7B…7G&–ær†bæÆVwVUöæÖR’’“°¢f÷"†6öç7Bböb&÷w2—¶6öç7BæÖSÕ7G&–ær†bæÆVwVUöæÖWÇÂrr’çG&–Ò‚’Æ¶W“ÖæÖRçFôÆ÷vW$66R‚“¶–b‚¶W’–6öçF–çVS¶6öç7BöÆCÖ6÷VçG2ævWB†¶W’—ÇÇ¶æÖS¦æÖRÆ6÷VçC£Ó¶öÆBæ6÷VçB²³¶6÷VçG2ç6WB†¶W’ÆöÆB“·Ð¢6öç7B&W7CÔ'&’æg&öÒ†6÷VçG2çfÇVW2‚’’ç6÷'B‚†Æ"“Óæ"æ6÷VçBÖæ6÷VçB•³Ó¶–b‚&W7B—&WGW&ârs°¢6öç7BÆ÷sÖ&W7BææÖRçFôÆ÷vW$66R‚“¶ÆWB6÷VçG'“Òrs°¢–b‚÷&VÖ–W"ÆVwVWÆ6†×–öç6†—ÆÆVwVRöæWÆÆVwVRGvòòçFW7B†Æ÷r’–6÷VçG'“ÒtVævÆæBs°¢VÇ6R–b‚öVÆ—FW6W&–VçÆö&÷2òçFW7B†Æ÷r’–6÷VçG'“Òtæ÷'v’s°¢VÇ6R–b‚öÆöÆ–vòçFW7B†Æ÷r’–6÷VçG'“Òu7–âs°¢VÇ6R–b‚ö'VæFW6Æ–vòçFW7B†Æ÷r’–6÷VçG'“ÒtvW&Öç’s°¢VÇ6R–b‚÷6W&–RòçFW7B†Æ÷r’–6÷VçG'“Òt—FÇ’s°¢VÇ6R–b‚öÆ–wVRòçFW7B†Æ÷r’–6÷VçG'“Òtg&æ6Rs°¢VÇ6R–b‚öW&VF—f—6–RòçFW7B†Æ÷r’–6÷VçG'“ÒtæWF†W&ÆæG2s°¢VÇ6R–b‚÷&–ÖV—&Æ–vòçFW7B†Æ÷r’–6÷VçG'“Òu÷'GVvÂs°¢VÇ6R–b‚÷&VÖ–W'6†—òçFW7B†Æ÷r’–6÷VçG'“Òu66÷FÆæBs°¢&WGW&â†6÷VçG'“ö6÷VçG'’²r9rs¢rr’¶&W7BææÖS°§Ð¦gVæ7F–öâ&VæFW$×”Æ—7E7÷'E6†VÆÇ2†ff÷&—FW2—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v×”Æ—7EFV×2r’ÇFV×3Ò†ff÷&—FW2çFV×7ÇÅµÒ“¶–b‚VÂ—&WGW&ã°¢ÆWBƒÒrs°¢–b…öfö÷F&ÆÄVæ&ÆVB–f÷"†6öç7BFVÒöbFV×2—°¢6öç7BæÖSÕ7G&–ær‡G—VöbFVÓÓÓÒw7G&–ærs÷FVÓ§FVÒææÖWÇÂrr’Æ–C×G—VöbFVÓÓÓÒw7G&–ærsòrs¥7G&–ær‡FVÒçFVÕö–GÇÂrr’ÆÆövó×G—VöbFVÓÓÓÒw7G&–ærsòrs¢‡FVÒæÆöv÷ÇÂrr’Ç7&3ÖÆöv÷ÇÂ†–Còrö’÷FVÕöÆövóö–CÒr¶Væ6öFUU$”6ö×öæVçB†–B“¢rr“°¢–b…ö×”Æ—7DÆ–÷WCÓÓÒwF–ÖVÆ–æRr–‚³ÒsÆF—b6Æ73Ò&×–F6‡FVÖöæÇ’"öæ6Æ–6³Ò'6†÷uFV×2‚’#âr²‡7&3òsÆ–Ör7&3Ò"r¶W64GG"‡7&2’²r"ÇCÒ""öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆR#ãÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆWF÷#ãÆF—b6Æ73Ò&×–F6‡7÷'FæÖR#âr¶W62†æÖR’²sÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'FWfVçFÆ–æR#ãÇ7â6Æ73Ò&×–F6‡7÷'FæW‡B×WFVB#âr¶W62‡G"‚tÆöF–ærf—‡GW&Râââr’’²sÂ÷7ããÂöF—cãÂöF—cãÂöF—cãÂöF—câs°¢VÇ6R‚³ÒsÆF—b6Æ73Ò&×–F6†f—‡GW&R#ãÆF—b6Æ73Ò&×–F6‡FVÒ#âr²‡7&3òsÆ–Ör7&3Ò"r¶W64GG"‡7&2’²r"ÇCÒ""öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÇ7ãâr¶W62†æÖR’²sÂ÷7ããÂöF—cãÇ7â6Æ73Ò&×WFVB#âr¶W62‡G"‚tÆöF–ærf—‡GW&Râââr’’²sÂ÷7ããÂöF—câs°¢Ð¢–b…öcVæ&ÆVB—°¢6öç7B6VÆV7FVCÖæWr6WB‚…÷&öf–ÆT6öæf–rç&6–æu÷6W&–W7ÇÅ²vcuÒ’æÖ…7G&–ær’“°¢–b…ö×”Æ—7DÆ–÷WCÓÓÒwF–ÖVÆ–æRr–‚³ÒsÆF—b6Æ73Ò&×–F6‡7÷'G7V&†VB&6–ær#å&6–æsÂöF—câs°¢–b‡6VÆV7FVBæ†2‚vcr’—°¢6öç7BFVÓÒ‚†ff÷&—FW2æc÷FV×7ÇÅµÒ•³×ÇÇ·Ò’ÆæÖS×FVÒææÖWÇÂtf÷&×VÆrÇ7&3×FVÒæÆöv÷ÇÂ‡FVÒæ–Còrö’öc÷FVÕöÆövóö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær‡FVÒæ–B’“¢rr“°¢‚³ÒsÆF—b6Æ73Ò&×–F6‡FVÖöæÇ’×–F6†c6&B"FFÖG&—fW"Ö¶W“Ò&c×FVÒ"öæ6Æ–6³Ò'6†÷u&6–ær‡F†—2æFF6WBæG&—fW$¶W’’#âr²‡7&3òsÆ–Ör7&3Ò"r¶W64GG"‡7&2’²r"ÇCÒ""öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆR#ãÆF—b6Æ73Ò&×–F6‡7÷'FæÖR#âr¶W62†æÖR’²sÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'FÖWF#äf÷&×VÆ+rr¶W62‡G"‚tÆöF–ærG&—fW'2æBæW‡B&6Râââr’’²sÂöF—cãÂöF—cãÂöF—câs°¢Ð¢6öç7BV–6³Õµ²ww&2rÂww&2ÖöÆ—fW"×6öÆ&W&rrÂtöÆ—fW"6öÆ&W&rrÂuu$29rF÷–÷Fv¦öò&6–ærrÆfÇ6UÒÅ²v–æG–6"rÂv–æG–6"ÖFVææ—2Ö†VvW"rÂtFVææ—2†VvW"rÂt–æG”6"9rFÆR6÷–æR&6–ærrÆfÇ6UÒÅ²vc"rÂvc"ÖÖ'F–æ—W2×7FVç6†÷&æRrÂtÖ'F–æ—W27FVç6†÷&æRrÂtf÷&×VÆ"9r&öF–âÖ÷F÷'7÷'BrÇG'VUÕÓ°¢f÷"†6öç7B&÷röbV–6²—¶–b‚6VÆV7FVBæ†2‡&÷u³Ò’–6öçF–çVS¶6öç7B7&3Òrö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB‡&÷u³Ò“¶‚³ÒsÆF—b6Æ73Ò&×–F6‡FVÖöæÇ’"FFÖG&—fW"Ö¶W“Ò"r¶W64GG"‡&÷u³Ò’²r"öæ6Æ–6³Ò'6†÷u&6–ær‡F†—2æFF6WBæG&—fW$¶W’’#ãÆ–Ör6Æ73Ò&G&—fW"r²‡&÷u³EÓòr6"s¢rr’²r"7&3Ò"r·7&2²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#ãÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆR#ãÆF—b6Æ73Ò&×–F6‡7÷'FæÖR#âr¶W62‡&÷u³%Ò’²sÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'FÖWF#âr¶W62‡&÷u³5Ò’²r+rr¶W62‡G"‚tÆöF–æræW‡B&6Râââr’’²sÂöF—cãÂöF—cãÂöF—câs·Ð¢Ð¢VÂæ–ææW$…DÔÃÖ‡ÇÂsÇ7â6Æ73Ò&×WFVB#âr·G"…öcVæ&ÆVCòtæòcFVÒ6VÆV7FVBâs¢tæòff÷&—FRFV×2–WBâr’²sÂ÷7ãâs°§Ð¦7–æ2gVæ7F–öâÆöD×”Æ—7EFV×2†ff÷&—FW2Ç&6–ætFF&öÖ—6R—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v×”Æ—7EFV×2r’ÇFV×3Ò†ff÷&—FW2çFV×7ÇÅµÒ’Ææ÷sÔFFRææ÷r‚“°¢ö×”Æ—7EFVÔÖöÖVçG3ÕµÓ¶ÆWBƒÒrs°¢&VæFW$×”Æ—7E7÷'E6†VÆÇ2†ff÷&—FW2“°¢6öç7B&6–æu&öÖ—6SÕöcVæ&ÆVCõ&öÖ—6RæÆÂ…¶’‚rö’÷&6–æuöG&—fW'2r’Ç&6–ætFF&öÖ—6WÇÆ’‚rö’÷&6–ærr•Ò“¦çVÆÃ°¢–b…öfö÷F&ÆÄVæ&ÆVBbgFV×2æÆVæwF‚—°¢G'—°¢6öç7B#Öv—B’‚rö’ö×•÷FV×2r’Æf—‡GW&W3×"æf—‡GW&W7ÇÅµÓ°¢f÷"†6öç7BFVÒöbFV×2—°¢6öç7BæÖSÕ7G&–ær‡G—VöbFVÓÓÓÒw7G&–ærs÷FVÓ§FVÒææÖWÇÂrr’Æ¶W“ÖæÖRçFôÆ÷vW$66R‚“°¢6öç7BÖ–æSÖf—‡GW&W2æf–ÇFW"†cÓâ†bæff÷&—FU÷FV×7ÇÅµÒ’ç6öÖR†÷væW#Óå7G&–ær†÷væW"’çFôÆ÷vW$66R‚“ÓÓÖ¶W’’“°¢6öç7BÆ—fSÖÖ–æRæf–ÇFW"†cÓæbæ—5öÆ—fR’ç6÷'B‚†Æ"“Óå7G&–ær†ç7F'B’æÆö6ÆT6ö×&R…7G&–ær†"ç7F'B’’•³Ó°¢6öç7BW6öÖ–æsÖÖ–æRæf–ÇFW"†cÓç¶6öç7BG3Öbç7F'CöæWrFFR†bç7F'B’ævWEF–ÖR‚“£·&WGW&âG3ææ÷s·Ò’ç6÷'B‚†Æ"“ÓææWrFFR†ç7F'B’ÖæWrFFR†"ç7F'B’“°¢6öç7BæW‡C×W6öÖ–æu³Ó°¢6öç7Bf—‡GW&SÖÆ—fWÇÆæW‡BÆ–C×G—VöbFVÓÓÓÒw7G&–ærsòrs¥7G&–ær‡FVÒçFVÕö–GÇÂrr’ÆÆövó×G—VöbFVÓÓÓÒw7G&–ærsòrs¢‡FVÒæÆöv÷ÇÂrr’Ç7&3ÖÆöv÷ÇÂ†–Còrö’÷FVÕöÆövóö–CÒr¶Væ6öFUU$”6ö×öæVçB†–B“¢rr“°¢–b†Æ—fR•ö×”Æ—7EFVÔÖöÖVçG2çW6‚‡·FVÓ¦æÖRÆf—‡GW&S¦Æ—fRÆÆ—fS§G'VRÆÆövó§7&2ÇG3¤FFRææ÷r‚—Ò“°¢f÷"†6öç7BgWGW&RöbW6öÖ–ærç6Æ–6RƒÃB’—¶6öç7BG3ÖgWGW&Rç7F'CöæWrFFR†gWGW&Rç7F'B’ævWEF–ÖR‚“£¶–b‡G2•ö×”Æ—7EFVÔÖöÖVçG2çW6‚‡·FVÓ¦æÖRÆf—‡GW&S¦gWGW&RÆÆ—fS¦fÇ6RÆÆövó§7&2ÇG3§G7Ò“·Ð¢–b…ö×”Æ—7DÆ–÷WCÓÓÒwF–ÖVÆ–æRr—°¢6öç7Bf—‡GW&UFW‡CÖf—‡GW&Sò‚†f—‡GW&Ræ†öÖWÇÂrr’²rbr²†f—‡GW&Ræv—ÇÂrr’“§G"‚tæòW6öÖ–ærf—‡GW&Rf÷VæBâr“°¢6öç7B6÷VçFF÷vãÖf—‡GW&Sò†Æ—fSòtÄ•dRs§&6–æt6÷VçFF÷vâ‡·7F'C¦f—‡GW&Rç7F'GÒ’“¢rs°¢6öç7BFVÔÖWFÖ×•7÷'EFVÔÖWF†Ö–æR“°¢‚³ÒsÆF—b6Æ73Ò&×–F6‡FVÖöæÇ’"öæ6Æ–6³Ò'6†÷uFV×2‚’#âr²‡7&3òsÆ–Ör7&3Ò"r¶W64GG"‡7&2’²r"ÇCÒ""öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆR#ãÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆWF÷#ãÆF—b6Æ73Ò&×–F6‡7÷'FæÖR#âr¶W62†æÖR’²sÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'FWfVçG2#ãÆF—b6Æ73Ò&×–F6‡7÷'FWfVçFÆ–æR#ãÇ7â6Æ73Ò&×–F6‡7÷'FæW‡B#âr¶W62†f—‡GW&UFW‡B’²sÂ÷7ãâr²†6÷VçFF÷vãòsÇ7â6Æ73Ò&×–F6‡7÷'F6÷VçB#âr¶W62†6÷VçFF÷vâ’²sÂ÷7ãâs¢rr’²sÂöF—câr²sÂöF—cãÂöF—câr²‡FVÔÖWFòsÆF—b6Æ73Ò&×–F6‡7÷'FÖWF#âr¶W62‡FVÔÖWF’²sÂöF—câs¢rr’²sÂöF—cãÂöF—câs°¢Ð¢VÇ6W¶‚³ÒsÆF—b6Æ73Ò&×–F6†f—‡GW&R#ãÆF—b6Æ73Ò&×–F6‡FVÒ#âr²‡7&3òsÆ–Ör7&3Ò"r¶W64GG"‡7&2’²r"ÇCÒ""öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rr’²sÇ7ãâr¶W62†æÖR’²sÂ÷7ããÂöF—câr²†f—‡GW&S÷FVÔf—‡GW&T6&B†f—‡GW&RÂf—‡GW&Ræ—5öÆ—fR“¢sÇ7â6Æ73Ò&×WFVB#âr·G"‚tæòW6öÖ–ærf—‡GW&Rf÷VæBâr’²sÂ÷7ãâr’²sÂöF—câs·Ð¢Ð¢Ö6F6‚†R—¶‚³ÒsÇ7â6Æ73Ò&×WFVB#âr·G"‚t6÷VÆBæ÷BÆöBFVÒf—‡GW&W2âr’²sÂ÷7ãâs·Ð¢Ð¢–b…öcVæ&ÆVB—°¢G'—°¢6öç7B¶G&—fW$FFÇ&6–ætFFÓÖv—B&6–æu&öÖ—6S°¢–b…ö×”Æ—7DÆ–÷WCÓÓÒwF–ÖVÆ–æRrbb†G&—fW$FFæG&—fW'7ÇÅµÒ’æÆVæwF‚–‚³ÒsÆF—b6Æ73Ò&×–F6‡7÷'G7V&†VB&6–ær#å&6–æsÂöF—câs°¢6öç7BÆÄG&—fW'3ÖG&—fW$FFæG&—fW'7ÇÅµÓµö×”Æ—7E&6–ætG&—fW'3ÖÆÄG&—fW'3°¢6öç7BcG&—fW'3ÖÆÄG&—fW'2æf–ÇFW"†G&—fW#Óå7G&–ær†G&—fW"ç6W&–W7ÇÂrr“ÓÓÒvcr“°¢–b…ö×”Æ—7DÆ–÷WCÓÓÒwF–ÖVÆ–æRr—°¢6öç7B6&G3ÕµÓ°¢–b†cG&—fW'2æÆVæwF‚—¶6öç7BcWfVçG3Ò‡&6–ætFFæWfVçG7ÇÅµÒ’æf–ÇFW"†SÓå7G&–ær†Rç6W&–W7ÇÂrr“ÓÓÒvcr’æÖ†SÓâ‡¶WfVçC¦RÇG3¦æWrFFR†Rç7F'B’ævWEF–ÖR‚’ÆÆ—fS§&6–ætWfVçD—4Æ—fR†RÆæ÷r—Ò’’æf–ÇFW"‡&÷sÓäçVÖ&W"æ—4f–æ—FR‡&÷rçG2’bb‡&÷ræÆ—fWÇÇ&÷rçG3ãÖæ÷r’’ç6÷'B‚†Æ"“Óâ†æÆ—fSòÓ£’Ò†"æÆ—fSòÓ£—ÇÆçG2Ö"çG2’ÆæW‡CÖcWfVçG5³ÓòæWfVçGÇÆçVÆÂÇ&6SÖæW‡DG&—fW%&6R†cG&—fW'5³ÒÇ&6–ætFFæWfVçG7ÇÅµÒÆæ÷r“¶6&G2çW6‚‡¶¶–æC¢vcrÆG&—fW'3¦cG&—fW'2ÆæW‡C¦æW‡BÇ&6S§&6RÇG3¦æW‡CöæWrFFR†æW‡Bç7F'B’ævWEF–ÖR‚“¤–æf–æ—G—Ò“·Ð¢f÷"†6öç7BG&—fW"öbÆÄG&—fW'2—¶–b…7G&–ær†G&—fW"ç6W&–W7ÇÂrr“ÓÓÒvcr–6öçF–çVS¶6öç7BæW‡CÖæW‡DG&—fW%&6R†G&—fW"Ç&6–ætFFæWfVçG7ÇÅµÒÆæ÷r“¶6&G2çW6‚‡¶¶–æC¢vG&—fW"rÆG&—fW#¦G&—fW"ÆæW‡C¦æW‡BÇG3¦æW‡CöæWrFFR†æW‡Bç7F'B’ævWEF–ÖR‚“¤–æf–æ—G—Ò“·Ð¢òò&6–ærföÆÆ÷w2F†R6ÆVæF#¢æV&W7BæW‡BWfVçBf—'7Bâfö÷F&ÆÂFVÐ¢òò6&G2&÷fRFVÆ–&W&FVÇ’&WF–âF†RW6W"w2ff÷&—FRö÷&FW"6WVVæ6Rà¢6&G2ç6÷'B‚†Æ"“Óâ„çVÖ&W"æ—4f–æ—FR†çG2“öçG3¤–æf–æ—G’’Ò„çVÖ&W"æ—4f–æ—FR†"çG2“ö"çG3¤–æf–æ—G’’“°¢f÷"†6öç7B6&Böb6&G2—°¢6öç7BæW‡CÖ6&BææW‡BÆ6÷VçFF÷vãÖæW‡C÷&6–æt6÷VçFF÷vâ†æW‡B“¢rs°¢–b†6&Bæ¶–æCÓÓÒvcr—°¢6öç7BG&—fW'3Ö6&BæG&—fW'2ÆÆ—fSÒ‡&6–ætFFæWfVçG7ÇÅµÒ’æf–ÇFW"†SÓå7G&–ær†Rç6W&–W7ÇÂrr“ÓÓÒvcr’ç6öÖR†SÓç&6–ætWfVçD—4Æ—fR†RÆæ÷r’’ÇFVÓÖG&—fW'5³ÒçFV×ÇÂrrÇ&6TWfVçCÖ6&Bç&6RÇ&6T6÷VçFF÷vã×&6TWfVçC÷&6–æt6÷VçFF÷vâ‡&6TWfVçB“¢rs°¢6öç7B†÷F÷3ÖG&—fW'2ç6Æ–6RƒÃ"’æÖ†G&—fW#ÓâsÆ–Ör6Æ73Ò&G&—fW""7&3Ò"ö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær†G&—fW"æ¶W—ÇÂrr’’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âr’æ¦ö–â‚rr“°¢6öç7BæÖW3ÖG&—fW'2ç6Æ–6RƒÃ"’æÖ†G&—fW#ÓâsÆF—b6Æ73Ò&×–F6‡7÷'FæÖR#âr¶W62†G&—fW"ææÖWÇÂrr’²sÂöF—câr’æ¦ö–â‚rr’Ç6W76–öãÖæW‡Cõ²†æW‡Bç&6WÇÆæW‡Bæ6—&7V—GÇÂrr’Ç&6–æu6W76–öäÆ&VÂ†æW‡B•Òæf–ÇFW"„&ööÆVâ’æ¦ö–â‚rr“§G"‚tæòW6öÖ–ær&6Rf÷VæBâr“°¢‚³ÒsÆF—b6Æ73Ò&×–F6‡FVÖöæÇ’×–F6†c6&B"FFÖG&—fW"Ö¶W“Ò&c×FVÒ"öæ6Æ–6³Ò'6†÷u&6–ær‡F†—2æFF6WBæG&—fW$¶W’’#ãÆF—b6Æ73Ò&×–F6‡7÷'G†÷F÷2#âr·†÷F÷2²sÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆR#ãÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆWF÷#ãÆF—b6Æ73Ò&×–F6†cæÖW2#âr¶æÖW2²sÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'FWfVçG2#ãÆF—b6Æ73Ò&×–F6‡7÷'FWfVçFÆ–æR#ãÇ7â6Æ73Ò&×–F6‡7÷'FæW‡B#âr¶W62‡6W76–öâ’²sÂ÷7ããÇ7â6Æ73Ò&×–F6‡7÷'F6÷VçB#âr¶W62†Æ—fS÷G"‚u&–v‡Bæ÷rr“¢†6÷VçFF÷vçÇÂrr’’²sÂ÷7ããÂöF—câr²sÂöF—cãÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'FÖWF#äf÷&×VÆr²‡FVÓòr9rr¶W62‡FVÒ“¢rr’²sÂöF—cãÂöF—cãÂöF—câs°¢ÖVÇ6W°¢6öç7BG&—fW#Ö6&BæG&—fW"ÆÆ—fSÒ‡&6–ætFFæWfVçG7ÇÅµÒ’æf–ÇFW"†SÓå7G&–ær†Rç6W&–W7ÇÂrr“ÓÓÕ7G&–ær†G&—fW"ç6W&–W7ÇÂrr’’ç6öÖR†SÓç&6–ætWfVçD—4Æ—fR†RÆæ÷r’’Ç7&3Òrö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær†G&—fW"æ¶W—ÇÂrr’“°¢6öç7BÖWFÕ¶G&—fW"ç6W&–W5öæÖWÇÂu&6–ærrÆG&—fW"çFV×ÇÂruÒæf–ÇFW"„&ööÆVâ’æ¦ö–â‚r9rr’ÆæW‡EFW‡CÖæW‡Cò†æW‡Bç&6WÇÆæW‡Bæ6—&7V—GÇÇG"‚tæW‡B&6Rr’“§G"‚tæòW6öÖ–ær&6Rf÷VæBâr’Æ–ÖvT6Æ73ÒvG&—fW"r²…7G&–ær†G&—fW"æ¶W—ÇÂrr“ÓÓÒvc"ÖÖ'F–æ—W2×7FVç6†÷&æRsòr6"s¢rr“°¢‚³ÒsÆF—b6Æ73Ò&×–F6‡FVÖöæÇ’"FFÖG&—fW"Ö¶W“Ò"r¶W64GG"…7G&–ær†G&—fW"æ¶W—ÇÂrr’’²r"öæ6Æ–6³Ò'6†÷u&6–ær‡F†—2æFF6WBæG&—fW$¶W’’#ãÆ–Ör6Æ73Ò"r¶–ÖvT6Æ72²r"7&3Ò"r·7&2²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#ãÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆR#ãÆF—b6Æ73Ò&×–F6‡7÷'G6–ævÆWF÷#ãÆF—b6Æ73Ò&×–F6‡7÷'FæÖR#âr¶W62†G&—fW"ææÖWÇÂrr’²sÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'FWfVçG2#ãÆF—b6Æ73Ò&×–F6‡7÷'FWfVçFÆ–æR#ãÇ7â6Æ73Ò&×–F6‡7÷'FæW‡B#âr¶W62†æW‡EFW‡B’²sÂ÷7ããÇ7â6Æ73Ò&×–F6‡7÷'F6÷VçB#âr¶W62†Æ—fS÷G"‚u&–v‡Bæ÷rr“¢†6÷VçFF÷vçÇÂrr’’²sÂ÷7ããÂöF—câr²sÂöF—cãÂöF—cãÆF—b6Æ73Ò&×–F6‡7÷'FÖWF#âr¶W62†ÖWF’²sÂöF—cãÂöF—cãÂöF—câs°¢Ð¢Ð¢ÖVÇ6Rf÷"†6öç7BG&—fW"öbÆÄG&—fW'2—°¢6öç7BÆ—fSÒ‡&6–ætFFæWfVçG7ÇÅµÒ’æf–ÇFW"†SÓå7G&–ær†Rç6W&–W7ÇÂrr“ÓÓÕ7G&–ær†G&—fW"ç6W&–W7ÇÂrr’’ç6öÖR†SÓç&6–ætWfVçD—4Æ—fR†RÆæ÷r’’Ç7&3Òrö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær†G&—fW"æ¶W—ÇÂrr’“°¢‚³ÒsÆF—b6Æ73Ò&×–F6†f—‡GW&R"FFÖG&—fW"Ö¶W“Ò"r¶W64GG"…7G&–ær†G&—fW"æ¶W—ÇÂrr’’²r"öæ6Æ–6³Ò'6†÷u&6–ær‡F†—2æFF6WBæG&—fW$¶W’’"7G–ÆSÒ&7W'6÷#§ö–çFW"#ãÆF—b6Æ73Ò&×–F6‡FVÒ#ãÆ–Ör7&3Ò"r·7&2²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#ãÇ7ãâr¶W62†G&—fW"ææÖWÇÂrr’²sÂ÷7ããÂöF—cãÇ7â6Æ73Ò&×WFVB#âr¶W62†G&—fW"ç6W&–W5öæÖWÇÂu&6–ærr’²†G&—fW"çFVÓòr+rr¶W62†G&—fW"çFVÒ“¢rr’²sÂ÷7ãâr²†Æ—fSòsÇ7â6Æ73Ò&×–F6‡7÷'F6÷VçB#âr¶W62‡G"‚u&–v‡Bæ÷rr’’²sÂ÷7ãâs¢rr’²sÂöF—câs°¢Ð¢Ö6F6‚†R—·Ð¢Ð¢VÂæ–ææW$…DÔÃÖ‡ÇÂsÇ7â6Æ73Ò&×WFVB#âr·G"…öcVæ&ÆVCòtæòcFVÒ6VÆV7FVBâs¢tæòff÷&—FRFV×2–WBâr’²sÂ÷7ãâs°¢66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“°§Ð¦7–æ2gVæ7F–öâÆöD×”Æ—7E&6–ær‡&6–ætFF&öÖ—6R—°¢ö×”Æ—7DcÖöÖVçG3ÕµÓ°¢G'—°¢6öç7B#Öv—B‡&6–ætFF&öÖ—6WÇÆ’‚rö’÷&6–ærr’’Ææ÷sÔFFRææ÷r‚“°¢6öç7Bw&÷W3ÖæWrÖ‚“°¢f÷"†6öç7BWfVçBöb‡"æWfVçG7ÇÅµÒ’—°¢6öç7B&÷s×¶WfVçC¦WfVçBÇG3¦æWrFFR†WfVçBç7F'B’ævWEF–ÖR‚—ÒÆÆ—fS×&6–ætWfVçD—4Æ—fR†WfVçBÆæ÷r“°¢–b‚çVÖ&W"æ—4f–æ—FR‡&÷rçG2—ÇÂ‚Æ—fRbg&÷rçG3ÃÖæ÷rÓ"£3c’–6öçF–çVS°¢6öç7B¶W“Õ7G&–ær†WfVçBç6W&–W7ÇÂw&6–ærr“°¢–b‚w&÷W2æ†2†¶W’’–w&÷W2ç6WB†¶W’ÅµÒ“°¢w&÷W2ævWB†¶W’’çW6‚‡&÷r“°¢Ð¢òò¶VWF†RF–ÖVÆ–æR&Ææ6VBv†Vâ6WfW&Â6†×–öç6†—2&RVæ&ÆVC ¢òò6W76–öâÖ†Vg’cvVV¶VæB6†÷VÆBæ÷B7&÷vBu$2ôÖ÷FôuöWF2âöfb—Bà¢ö×”Æ—7DcÖöÖVçG3Ô'&’æg&öÒ†w&÷W2æVçG&–W2‚’’æfÆDÖ‚…·6W&–W2Ç&÷w5Ò“Óç&6–æuf—6–&ÆU6W&–W4WfVçG2‡&÷w2ç6÷'B‚†Æ"“ÓæçG2Ö"çG2’Ç6W&–W2Ã2’’ç6÷'B‚†Æ"“ÓæçG2Ö"çG2’ç6Æ–6RƒÃ‚“°¢66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“°¢G'—¶6öç7BÖv—B’‚rö’÷&6–æuöf–Æ&–Æ—G’r“¶f÷"†6öç7B&÷röbö×”Æ—7DcÖöÖVçG2—&÷ræWfVçBæ6†ææVÇ3Ò†æf–Æ&–Æ—G—ÇÇ·Ò•·&6–ætf–Æ&–Æ—G”¶W’‡&÷ræWfVçB•×ÇÅµÓ·Ö6F6‚†R—·Ð¢Ö6F6‚†R—·Ð¢66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“°§Ð¦7–æ2gVæ7F–öâÆöD×”Æ—7DÖ÷f–W2‚—°¢ö×”Æ—7DÖ÷f–TÖöÖVçG3ÕµÓ°¢G'—°¢6öç7B#Öv—B’‚rö’öff÷&—FUöÖ÷f–U÷7FGW2r’Çv–æF÷t×3Ó"£#B£3cÆæ÷sÔFFRææ÷r‚“°¢ö×”Æ—7DÖ÷f–TÖöÖVçG3Ò‡"æÖ÷f–W7ÇÅµÒ’æÖ†Ö÷f–SÓâ‡¶Ö÷f–S¦Ö÷f–RÇG3¤FFRç'6R†Ö÷f–Rç&VÆV6VGÇÂrr—Ò’’æf–ÇFW"‡&÷sÓäçVÖ&W"æ—4f–æ—FR‡&÷rçG2’bdÖF‚æ'2‡&÷rçG2Öæ÷r“Ã×v–æF÷t×2’ç6÷'B‚†Æ"“ÓäÖF‚æ'2†çG2Öæ÷r’ÔÖF‚æ'2†"çG2Öæ÷r’’ç6Æ–6RƒÃB“°¢Ö6F6‚†R—·Ð¢66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“°§Ð¦gVæ7F–öâÆöD×”Æ—7DvÖW2†ff÷&—FW2—°¢6öç7Bæ÷sÔFFRææ÷r‚’Ç&V6VçD7WFöfcÖæ÷rÓr£#B£3c°¢ö×”Æ—7DvÖTÖöÖVçG3Ò†ff÷&—FW2ævÖW7ÇÅµÒ’æf–ÇFW"†vÖSÓævÖRçv—6†Æ—7Eö–×÷'FVB’æÖ†vÖSÓâ‡¶vÖS¦vÖRÇG3¤FFRç'6R†vÖRç&VÆV6VGÇÂrr—Ò’’æf–ÇFW"‡&÷sÓäçVÖ&W"æ—4f–æ—FR‡&÷rçG2’bg&÷rçG3ã×&V6VçD7WFöfb’ç6÷'B‚†Æ"“ÓäÖF‚æ'2†çG2Öæ÷r’ÔÖF‚æ'2†"çG2Öæ÷r’’ç6Æ–6RƒÃB“°¢66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“°§Ð¦gVæ7F–öâ×”Æ—7DW—6öFUv†Vâ‡G2ÇW6öÖ–ær—°¢6öç7BFVÇF×G2ÔFFRææ÷r‚“°¢–b‡W6öÖ–ær—¶6öç7B†÷W'3ÔÖF‚æÖ‚ƒÄÖF‚æ6V–Â†FVÇFó3c’“·&WGW&âG"‚t—'2–âr’²rr¶†÷W'2²rr·G"††÷W'3ÓÓÓòv†÷W"s¢v†÷W'2r“·Ð¢6öç7B†÷W'3ÔÖF‚æÖ‚ƒÄÖF‚ç&÷VæB„ÖF‚æ'2†FVÇF’ó3c’“°¢–b††÷W'3ÃÓ"—&WGW&âG"‚t§W7B&VÆV6VBr“°¢&WGW&âG"‚u&VÆV6VBr’²rr¶†÷W'2²rr·G"††÷W'3ÓÓÓòv†÷W"s¢v†÷W'2r’²rr·G"‚vvòr“°§Ð¦gVæ7F–öâF–ÖVÆ–æUW6öÖ–æuv†Vâ‡G2ÆFFTöæÇ’—°¢6öç7BF&vWCÖæWrFFR‡G2’Ææ÷sÖæWrFFR‚’ÆFVÇF×F&vWBÖæ÷rÆÆö6ÆSÕöÆæsÓÓÒvæòsòvæ"Ôäòs§VæFVf–æVC°¢–b‚çVÖ&W"æ—4f–æ—FR†FVÇF’—&WGW&ârs°¢–b‚FFTöæÇ’bfFVÇFãbfFVÇFÃ#B£3c—°¢6öç7BÖ–çWFW3ÔÖF‚æÖ‚ƒÄÖF‚æ6V–Â†FVÇFóc’“°¢–b†Ö–çWFW3Ãc—&WGW&âG"‚v–âr’²rr¶Ö–çWFW2²rr·G"†Ö–çWFW3ÓÓÓòvÖ–çWFRs¢vÖ–çWFW2r“°¢6öç7B†÷W'3ÔÖF‚æ6V–Â†Ö–çWFW2óc“·&WGW&âG"‚v–âr’²rr¶†÷W'2²rr·G"††÷W'3ÓÓÓòv†÷W"s¢v†÷W'2r“°¢Ð¢6öç7BF”F–fcÔÖF‚ç&÷VæB†÷6ÆôF”çVÖ&W"‡F&vWB’Ö÷6ÆôF”çVÖ&W"†æ÷r’“°¢6öç7BF–ÖS×F&vWBçFôÆö6ÆUF–ÖU7G&–ær†Æö6ÆRÇ¶†÷W#¢s"ÖF–v—BrÆÖ–çWFS¢s"ÖF–v—BrÇF–ÖU¦öæS¢tWW&÷Rô÷6ÆòwÒ“°¢–b†FFTöæÇ’bfF”F–fcÓÓÓ—&WGW&âG"‚uFöF’r“°¢–b†FFTöæÇ’bfF”F–fcÓÓÓ—&WGW&âG"‚uFöÖ÷'&÷rr“°¢–b‚FFTöæÇ’bfF”F–fcÓÓÓ—&WGW&âG"‚uFöF’r’²r+rr·F–ÖS°¢–b‚FFTöæÇ’bfF”F–fcÓÓÓ—&WGW&âG"‚uFöÖ÷'&÷rr’²r+rr·F–ÖS°¢6öç7BFFS×F&vWBçFôÆö6ÆTFFU7G&–ær†Æö6ÆRÇ·vVV¶F“¢w6†÷'BrÆF“¢vçVÖW&–2rÆÖöçFƒ¢w6†÷'BrÇF–ÖU¦öæS¢tWW&÷Rô÷6ÆòwÒ“°¢&WGW&âFFTöæÇ“öFFS¢†FFR²r+rr·F–ÖR“°§Ð¦gVæ7F–öâF–ÖVÆ–æU&VÆV6VEv†Vâ‡G2—°¢6öç7BFVÇFÔÖF‚æÖ‚ƒÄFFRææ÷r‚’ÔçVÖ&W"‡G7ÇÃ’’ÆÖ–çWFW3ÔÖF‚æÖ‚ƒÄÖF‚ç&÷VæB†FVÇFóc’“°¢ÆWBÖ÷VçBÇVæ—C°¢–b†Ö–çWFW3Ãc—¶Ö÷VçCÖÖ–çWFW3·Væ—C×G"†Ö–çWFW3ÓÓÓòvÖ–çWFRs¢vÖ–çWFW2r“·Ð¢VÇ6W¶Ö÷VçCÔÖF‚æÖ‚ƒÄÖF‚ç&÷VæB†Ö–çWFW2óc’“·Væ—C×G"†Ö÷VçCÓÓÓòv†÷W"s¢v†÷W'2r“·Ð¢&WGW&âG"‚u&VÆV6VBr’²…öÆæsÓÓÒvæòsòrf÷"s¢rr’¶Ö÷VçB²rr·Væ—B²rr·G"‚vvòr“°§Ð¦7–æ2gVæ7F–öâÆöD×”Æ—7E6†÷w2‚—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v×”Æ—7E6†÷w2r“°¢ö×”Æ—7E6†÷tÖöÖVçG3ÕµÓ°¢G'—°¢6öç7B#Öv—B’‚rö’öÆFW7EöW—6öFW3öÆ–Ö—CÓ3br’Ææ÷sÔFFRææ÷r‚’Ç&V6VçEv–æF÷sÓ#B£3cÇW6öÖ–æuv–æF÷sÓr£#B£3cÆ6æF–FFW3ÕµÓ°¢f÷"†6öç7BWöb‡"æW—6öFW7ÇÅµÒ’—¶6öç7B—&VCÔçVÖ&W"†Wæ—%÷G7ÇÃ’£ÆFFVCÔçVÖ&W"†WæFFVGÇÃ’£¶6öç7BG3Ò†—&VCãbf—&VCÃÖæ÷rbfæ÷rÖ—&VCÃ×&V6VçEv–æF÷r“ö—&VC¢†FFVGÇÆ—&VB“¶–b‡G2–6æF–FFW2çW6‚‡¶W¦WÇG3§G2ÇW6öÖ–æs¦fÇ6WÒ“·Ð¢f÷"†6öç7BWöb‡"çW6öÖ–æwÇÅµÒ’—¶6öç7BG3ÔçVÖ&W"†Wæ—%÷G7ÇÃ’£ÇÂ†Wæ—'7F×öæWrFFR†Wæ—'7F×’ævWEF–ÖR‚“£“¶–b‡G2–6æF–FFW2çW6‚‡¶W¦WÇG3§G2ÇW6öÖ–æs§G'VWÒ“·Ð¢6öç7BæV&W7CÖæWrÖ‚“°¢f÷"†6öç7B&÷röb6æF–FFW2—¶6öç7BFVÇF×&÷rçG2Öæ÷s¶–b‡&÷rçW6öÖ–ær—¶–b†FVÇFÃÇÆFVÇFçW6öÖ–æuv–æF÷r–6öçF–çVS·ÖVÇ6R–b†FVÇFãÇÄÖF‚æ'2†FVÇF“ç&V6VçEv–æF÷r–6öçF–çVS¶6öç7B¶W“Õ7G&–ær‡&÷ræWç6†÷uöæÖWÇÂrr’çFôÆ÷vW$66R‚’²wÂr²‡&÷rçW6öÖ–æsòwW6öÖ–ærs¢w&V6VçBr“¶6öç7BöÆCÖæV&W7BævWB†¶W’“¶–b‚öÆGÇÄÖF‚æ'2†FVÇF“ÄÖF‚æ'2†öÆBçG2Öæ÷r’–æV&W7Bç6WB†¶W’Ç&÷r“·Ð¢6öç7B&÷w3Ô'&’æg&öÒ†æV&W7BçfÇVW2‚’’ç6÷'B‚†Æ"“ÓäÖF‚æ'2†çG2Öæ÷r’ÔÖF‚æ'2†"çG2Öæ÷r’’ç6Æ–6RƒÃ"“°¢ö×”Æ—7E6†÷tÖöÖVçG3×&÷w3°¢–b‚&÷w2æÆVæwF‚—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr·G"‚tæ÷F†–ær—&–ær6Æ÷6RFòæ÷rg&öÒ–÷W"ff÷&—FR6†÷w2âr’²sÂ÷7ãâs·66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“·&WGW&ã·Ð¢VÂæ–ææW$…DÔÃ×&÷w2æÖ‡&÷sÓç¶6öç7BW×&÷ræWÆ6÷fW#ÖWæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†Wæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rs¶ÆWB7F–öãÒrs°¢–b‚&÷rçW6öÖ–ærbfWæf–Æ&ÆR—¶6öç7B7&3Ò†Wç6÷W&6W2bfWç6÷W&6W2æÆVæwF‚“öWç6÷W&6W5³Ó§¶–C¦Wæ–BÆW‡FVç6–öã¦WæW‡FVç6–öçÓ¶–b‡7&2bg7&2æ–BÖçVÆÂ–7F–öãÒsÆF—b6Æ73Ò&Ö÷f–V7F–öç2#ãÆ'WGFöâ6Æ73Ò&'FçfÆ2ÆFW7FW—6öFWfÆ2"FFÖ–CÒ"r¶W64GG"…7G&–ær‡7&2æ–B’’²r"FFÖW‡CÒ"r¶W64GG"‡7&2æW‡FVç6–öçÇÂv×Br’²r#âb3“cSƒ²dÄ3Âö'WGFöããÂöF—câs·Ð¢&WGW&âsÆF—b6Æ73Ò&×–F6†W—6öFR×–Æ—7G6†÷v6&B"FF×6W&–W3Ò"r¶W64GG"…7G&–ær†Wç6W&–W5ö–GÇÂrr’’²r"FFÖ6FÆösÒ"r¶W64GG"†Wæ6FÆöuö–GÇÂrr’²r#âr¶6÷fW"²sÆF—b6Æ73Ò&×–F6†W—6öFV–æfò#ãÆF—b6Æ73Ò&×–F6†W—6öFVæÖR#âr¶W62†Wç6†÷uöæÖR’²sÂöF—cãÆF—b6Æ73Ò&×–F6‡v†Vâ#âr¶W62†×”Æ—7DW—6öFUv†Vâ‡&÷rçG2Ç&÷rçW6öÖ–ær’’²sÂöF—cãÆF—b6Æ73Ò&Ö÷f–VÖWF#å2r¶W62†Wç6V6öâ’²tRr¶W62†WæW—6öFUöçVÒ’²rÒr¶W62†WçF—FÆWÇÂtW—6öFRr’²sÂöF—câr¶7F–öâ²sÂöF—cãÂöF—câs·Ò’æ¦ö–â‚rr“°¢66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“°¢Ö6F6‚†R—¶VÂæ–ææW$…DÔÃÒsÇ7â6Æ73Ò&×WFVB#âr·G"‚t6÷VÆBæ÷BÆöB–÷W"6†÷w2âr’²sÂ÷7ãâs·66†VGVÆT×”Æ—7EF–ÖVÆ–æU&VæFW"‚“·Ð§Ð¦gVæ7F–öâ×”Æ—7E7÷'D'Gv÷&²†f—‡GW&R—°¢6öç7B–CÕ7G&–ær‚†f—‡GW&Rbff—‡GW&RæÆVwVUö–B—ÇÂrr“°¢–b‚õåÅÆB²BòçFW7B†–B’—&WGW&ârs°¢6öç7BæÖSÕ7G&–ær‚†f—‡GW&Rbff—‡GW&RæÆVwVUöæÖR—ÇÇG"‚u7÷'G2r’“°¢&WGW&âsÆ–Ör6Æ73Ò&×–Æ—7GF–ÖVÆ–æV'B"7&3Ò"ö’öÆVwVUöÆövóö–CÒr¶Væ6öFUU$”6ö×öæVçB†–B’²r"ÇCÒ"r¶W64GG"†æÖR’²r"F—FÆSÒ"r¶W64GG"†æÖR’²r"ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs°§Ð¦gVæ7F–öâ×”Æ—7E&6–æt'Gv÷&²†WfVçB—°¢6öç7B6W&–W3Õ7G&–ær‚†WfVçBbfWfVçBç6W&–W2—ÇÂrr’çFôÆ÷vW$66R‚“°¢ÆWB7&3ÒrrÆæÖSÒrrÆG&—fW#ÖfÇ6RÆ6#ÖfÇ6S°¢–b‡6W&–W3ÓÓÒvcr—°¢6öç7B—#Ò…ö×”Æ—7E&6–ætG&—fW'7ÇÅµÒ’æf–ÇFW"‡&÷sÓå7G&–ær‡&÷rç6W&–W7ÇÂrr’çFôÆ÷vW$66R‚“ÓÓÒvcr’ç6Æ–6RƒÃ"“°¢–b‡—"æÆVæwF‚—&WGW&âsÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æVG&—fW'2"F—FÆSÒ"r¶W64GG"‡—"æÖ‡&÷sÓç&÷rææÖWÇÂrr’æf–ÇFW"„&ööÆVâ’æ¦ö–â‚rbr’’²r#âr·—"æÖ‡&÷sÓâsÆ–Ör7&3Ò"ö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB…7G&–ær‡&÷ræ¶W—ÇÂrr’’²r"ÇCÒ"r¶W64GG"‡&÷rææÖWÇÂrr’²r"ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âr’æ¦ö–â‚rr’²sÂ÷7ãâs°¢&WGW&ârs°¢ÖVÇ6W°¢6öç7BG&—fW'3×·w&3¥²ww&2ÖöÆ—fW"×6öÆ&W&rrÂtöÆ—fW"6öÆ&W&ruÒÆ–æG–6#¥²v–æG–6"ÖFVææ—2Ö†VvW"rÂtFVææ—2†VvW"uÒÆc#¥²vc"ÖÖ'F–æ—W2×7FVç6†÷&æRrÂtÖ'F–æ—W27FVç6†÷&æRu×Ó°¢6öç7B&÷sÖG&—fW'5·6W&–W5Ó°¢–b‡&÷r—·7&3Òrö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB‡&÷u³Ò“¶æÖS×&÷u³Ó¶G&—fW#×G'VS¶6#×6W&–W3ÓÓÒvc"s·Ð¢Ð¢&WGW&â7&3òsÆ–Ör6Æ73Ò&×–Æ—7GF–ÖVÆ–æV'Br²†G&—fW#òrG&—fW"s¢rr’²†6#òr6"s¢rr’²r"7&3Ò"r¶W64GG"‡7&2’²r"ÇCÒ"r¶W64GG"†æÖR’²r"F—FÆSÒ"r¶W64GG"†æÖR’²r"ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rs°§Ð¦gVæ7F–öâ×”Æ—7E&6–ætFWF–Ä¶W’†WfVçB—°¢6öç7B6W&–W3Õ7G&–ær‚†WfVçBbfWfVçBç6W&–W2—ÇÂrr’çFôÆ÷vW$66R‚“°¢–b‡6W&–W3ÓÓÒvcr—&WGW&âvc×FVÒs°¢6öç7BÆöFVCÒ…ö×”Æ—7E&6–ætG&—fW'7ÇÅµÒ’æf–æB‡&÷sÓå7G&–ær‡&÷rç6W&–W7ÇÂrr’çFôÆ÷vW$66R‚“ÓÓ×6W&–W2“°¢–b†ÆöFVBbfÆöFVBæ¶W’—&WGW&â7G&–ær†ÆöFVBæ¶W’“°¢&WGW&â‡·w&3¢ww&2ÖöÆ—fW"×6öÆ&W&rrÆ–æG–6#¢v–æG–6"ÖFVææ—2Ö†VvW"rÆc#¢vc"ÖÖ'F–æ—W2×7FVç6†÷&æRwÒ•·6W&–W5×ÇÂrs°§Ð¦gVæ7F–öâ6WGWFVÖô6÷fW"†Æ&VÂÆ6öÆ÷"—°¢6öç7B7fsÒsÇ7fr†ÖÆç3Ò&‡GG¢ò÷wwrçs2æ÷&ró#÷7fr"v–GFƒÒ#ƒ"†V–v‡CÒ##c#ãÇ&V7Bv–GFƒÒ#ƒ"†V–v‡CÒ##c"'ƒÒ#""f–ÆÃÒ"r¶6öÆ÷"²r"óãÆ6—&6ÆR7ƒÒ#“"7“Ò#“""#Ò#C""f–ÆÃÒ"6fffffc‚"óãÇFW‡BƒÒ#“"“Ò#R"FW‡BÖæ6†÷#Ò&Ö–FFÆR"föçB×6—¦SÒ#S#ï	ù;£Â÷FW‡CãÇFW‡BƒÒ#“"“Ò#“"FW‡BÖæ6†÷#Ò&Ö–FFÆR"föçBÖfÖ–Ç“Ò'6ç2×6W&–b"föçB×6—¦SÒ#‚"föçB×vV–v‡CÒ#s"f–ÆÃÒ"6ffb#âr¶Æ&VÂ²sÂ÷FW‡CãÇFW‡BƒÒ#“"“Ò##b"FW‡BÖæ6†÷#Ò&Ö–FFÆR"föçBÖfÖ–Ç“Ò'6ç2×6W&–b"föçB×6—¦SÒ#"f–ÆÃÒ"6ffffff#åEdÖFRFVÖóÂ÷FW‡CãÂ÷7fsâs°¢&WGW&âvFF¦–ÖvR÷7fr·†ÖÃ¶6†'6WCÕUDbÓ‚Âr¶Væ6öFUU$”6ö×öæVçB‡7fr“°§Ð¦gVæ7F–öâF–ÖVÆ–æTÆöE&Vg2‚—°¢–b…ö×•F–ÖVÆ–æU&Vg4ÆöFVB—&WGW&ãµö×•F–ÖVÆ–æU&Vg4ÆöFVC×G'VS°¢G'—°¢6öç7B¶–æCÖÆö6Å7F÷&vRævWD—FVÒ‚wGfÖFUF–ÖVÆ–æTf–ÇFW"r“¶–b…²vÆÂrÂw6†÷rrÂvÖ÷f–RrÂvvÖRrÂw7÷'BrÂvcuÒæ–æ6ÇVFW2†¶–æB’•ö×•F–ÖVÆ–æTf–ÇFW#Ö¶–æC°¢6öç7B6fVCÔ¥4ôâç'6R†Æö6Å7F÷&vRævWD—FVÒ‚wGfÖFUF–ÖVÆ–æU6WGF–æw2r—ÇÂw·Òr“°¢ö×•F–ÖVÆ–æU6WGF–æw3Ôö&¦V7Bæ76–vâ‡·ÒÅö×•F–ÖVÆ–æU6WGF–æw2Ç6fVGÇÇ·Ò“°¢Ö6F6‚†R—·Ð§Ð¦gVæ7F–öâF–ÖVÆ–æU6fU&Vg2‚—·G'—¶Æö6Å7F÷&vRç6WD—FVÒ‚wGfÖFUF–ÖVÆ–æTf–ÇFW"rÅö×•F–ÖVÆ–æTf–ÇFW"“¶Æö6Å7F÷&vRç6WD—FVÒ‚wGfÖFUF–ÖVÆ–æU6WGF–æw2rÄ¥4ôâç7G&–æv–g’…ö×•F–ÖVÆ–æU6WGF–æw2’“·Ö6F6‚†R—·×Ð¦gVæ7F–öâF–ÖVÆ–æTf–ÇFW$w&÷W†¶–æB—·&WGW&â¶–æCÓÓÒwFVÒsòw7÷'Bs¢†¶–æCÓÓÒvcsòvcs¦¶–æB“·Ð¦gVæ7F–öâF–ÖVÆ–æU6WGF–æw46†ævVB‚—¶6öç7BƒÕö×•F–ÖVÆ–æU6WGF–æw3·&WGW&â‚ç&V6VçBÓ×G'VWÇÇ‚æÆ—fRÓ×G'VWÇÇ‚çW6öÖ–ærÓ×G'VWÇÄçVÖ&W"‡‚æÖ…W$6FVv÷'—ÇÃ’ÓÓ·Ð¦gVæ7F–öâF–ÖVÆ–æT6öçG&öÇ4‡FÖÂ‚—°¢6öç7B¶–æG3Õµ²vÆÂrÂtÆÂuÒÅ²w6†÷rrÂu6†÷w2uÒÅ²vÖ÷f–RrÂtÖ÷f–W2uÒÅ²vvÖRrÂtvÖW2uÒÅ²w7÷'BrÂu7÷'G2uÒÅ²vcrÂu&6–æruÕÓ¶ÆWBƒÒsÆF—b6Æ73Ò&×—F–ÖVÆ–æV6öçG&öÇ2#âs°¢f÷"†6öç7B²öb¶–æG2–‚³ÒsÆ'WGFöâ6Æ73Ò&×—F–ÖVÆ–æVf–ÇFW"r¶µ³Ò²…ö×•F–ÖVÆ–æTf–ÇFW#ÓÓÖµ³Óòröâs¢rr’²r"FFÖ¶–æCÒ"r¶µ³Ò²r"öæ6Æ–6³Ò'6WEF–ÖVÆ–æTf–ÇFW"‡F†—2æFF6WBæ¶–æB’#âr¶W62‡G"†µ³Ò’’²sÂö'WGFöãâs°¢‚³ÒsÆ'WGFöâ6Æ73Ò&×—F–ÖVÆ–æVf–ÇFW"6WGF–æw2r²‡F–ÖVÆ–æU6WGF–æw46†ævVB‚“òr6†ævVBs¢rr’²r"öæ6Æ–6³Ò'FövvÆUF–ÖVÆ–æU6WGF–æw2‡F†—2’#âb3“ƒƒ²r¶W62‡G"‚tf–ÇFW"r’’²sÂö'WGFöãâs°¢‚³ÒsÆF—b6Æ73Ò&×—F–ÖVÆ–æVf–ÇFW'æVÂ†–FR#ãÆƒCâr¶W62‡G"‚uF–ÖVÆ–æR6WGF–æw2r’’²sÂöƒCãÆF—b6Æ73Ò'F–ÖVÆ–æV6†V6·2#âp¢²sÆÆ&VÃãÆ–çWBG—SÒ&6†V6¶&÷‚"FF×6WGF–æsÒ'&V6VçB"r²…ö×•F–ÖVÆ–æU6WGF–æw2ç&V6VçCòv6†V6¶VBs¢rr’²röæ6†ævSÒ'6WEF–ÖVÆ–æU6WGF–ær‡F†—2’#âr¶W62‡G"‚u&V6VçFÇ’r’’²sÂöÆ&VÃâp¢²sÆÆ&VÃãÆ–çWBG—SÒ&6†V6¶&÷‚"FF×6WGF–æsÒ&Æ—fR"r²…ö×•F–ÖVÆ–æU6WGF–æw2æÆ—fSòv6†V6¶VBs¢rr’²röæ6†ævSÒ'6WEF–ÖVÆ–æU6WGF–ær‡F†—2’#âr¶W62‡G"‚tÆ—fRæ÷rr’’²sÂöÆ&VÃâp¢²sÆÆ&VÃãÆ–çWBG—SÒ&6†V6¶&÷‚"FF×6WGF–æsÒ'W6öÖ–ær"r²…ö×•F–ÖVÆ–æU6WGF–æw2çW6öÖ–æsòv6†V6¶VBs¢rr’²röæ6†ævSÒ'6WEF–ÖVÆ–æU6WGF–ær‡F†—2’#âr¶W62‡G"‚uW6öÖ–ærr’’²sÂöÆ&VÃãÂöF—câp¢²sÆÆ&VÃãÇ7ãâr¶W62‡G"‚tÖ†–×VÒW"6FVv÷'’r’’²sÂ÷7ããÇ6VÆV7BFF×6WGF–æsÒ&Ö…W$6FVv÷'’"öæ6†ævSÒ'6WEF–ÖVÆ–æU6WGF–ær‡F†—2’#âp¢µµ³ÂtFVfVÇBuÒÅ³"Âs"uÒÅ³BÂsBuÒÅ³bÂsbuÒÅ³‚Âs‚uÒÅ³"Âs"uÕÒæÖ‡ƒÓâsÆ÷F–öâfÇVSÒ"r·…³Ò²r"r²„çVÖ&W"…ö×•F–ÖVÆ–æU6WGF–æw2æÖ…W$6FVv÷'—ÇÃ“ÓÓ×…³Óòr6VÆV7FVBs¢rr’²sâr¶W62‡G"‡…³Ò’’²sÂö÷F–öãâr’æ¦ö–â‚rr’²sÂ÷6VÆV7CãÂöÆ&VÃâp¢²sÆ'WGFöâ6Æ73Ò'F–ÖVÆ–æVf–ÇFW'&W6WB"öæ6Æ–6³Ò'&W6WEF–ÖVÆ–æU6WGF–æw2‚’#âr¶W62‡G"‚u&W6WBFòFVfVÇBr’’²sÂö'WGFöããÂöF—cãÂöF—câs·&WGW&âƒ°§Ð¦gVæ7F–öâ6WEF–ÖVÆ–æTf–ÇFW"†¶–æB—µö×•F–ÖVÆ–æTf–ÇFW#Õ²vÆÂrÂw6†÷rrÂvÖ÷f–RrÂvvÖRrÂw7÷'BrÂvcuÒæ–æ6ÇVFW2†¶–æB“ö¶–æC¢vÆÂs·F–ÖVÆ–æU6fU&Vg2‚“·&VæFW$×”Æ—7EF–ÖVÆ–æR‚“·Ð¦gVæ7F–öâFövvÆUF–ÖVÆ–æU6WGF–æw2†'Fâ—¶6öç7Bw&Ö'Fâæ6Æ÷6W7B‚ræ×—F–ÖVÆ–æV6öçG&öÇ2r’ÇæVÃ×w&÷w&çVW'•6VÆV7F÷"‚ræ×—F–ÖVÆ–æVf–ÇFW'æVÂr“¦çVÆÃ¶–b‡æVÂ—æVÂæ6Æ74Æ—7BçFövvÆR‚v†–FRr“·Ð¦gVæ7F–öâ6WEF–ÖVÆ–æU6WGF–ær†–çWB—¶6öç7B¶W“Ö–çWBæFF6WBç6WGF–æs¶–b†¶W“ÓÓÒvÖ…W$6FVv÷'’r•ö×•F–ÖVÆ–æU6WGF–æw5¶¶W•ÓÔÖF‚æÖ‚ƒÄçVÖ&W"†–çWBçfÇVWÇÃ’“¶VÇ6Rö×•F–ÖVÆ–æU6WGF–æw5¶¶W•ÓÒ–çWBæ6†V6¶VC·F–ÖVÆ–æU6fU&Vg2‚“·&VæFW$×”Æ—7EF–ÖVÆ–æR‚“·Ð¦gVæ7F–öâ&W6WEF–ÖVÆ–æU6WGF–æw2‚—µö×•F–ÖVÆ–æU6WGF–æw3×·&V6VçC§G'VRÆÆ—fS§G'VRÇW6öÖ–æs§G'VRÆÖ…W$6FVv÷'“£Ó·F–ÖVÆ–æU6fU&Vg2‚“·&VæFW$×”Æ—7EF–ÖVÆ–æR‚“·Ð¦gVæ7F–öâ&VæFW$×”Æ—7EF–ÖVÆ–æR‚—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v×”Æ—7EF–ÖVÆ–æRr’Ç7FæFÆöæSÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v×•F–ÖVÆ–æU7FæFÆöæRr“¶–b‚VÂbb7FæFÆöæR—&WGW&ã°¢6öç7Bæ÷sÔFFRææ÷r‚’ÆÖöÖVçG3ÕµÓ°¢–b…öfö÷F&ÆÄVæ&ÆVB–f÷"†6öç7B&÷röbö×”Æ—7EFVÔÖöÖVçG2–ÖöÖVçG2çW6‚‡¶¶–æC¢wFVÒrÇG3§&÷rçG2ÆÆ—fS§&÷ræÆ—fRÆFF§&÷wÒ“°¢–b…öcVæ&ÆVB–f÷"†6öç7B&÷röbö×”Æ—7DcÖöÖVçG2–ÖöÖVçG2çW6‚‡¶¶–æC¢vcrÇG3§&÷rçG2ÆÆ—fS§&6–ætWfVçD—4Æ—fR‡&÷ræWfVçBÆæ÷r’ÆFF§&÷wÒ“°¢f÷"†6öç7B&÷röbö×”Æ—7DÖ÷f–TÖöÖVçG2–ÖöÖVçG2çW6‚‡¶¶–æC¢vÖ÷f–RrÇG3§&÷rçG2ÆÆ—fS¦fÇ6RÆFF§&÷wÒ“°¢f÷"†6öç7B&÷röbö×”Æ—7DvÖTÖöÖVçG2–ÖöÖVçG2çW6‚‡¶¶–æC¢vvÖRrÇG3§&÷rçG2ÆÆ—fS¦fÇ6RÆFF§&÷wÒ“°¢f÷"†6öç7B&÷röbö×”Æ—7E6†÷tÖöÖVçG2–ÖöÖVçG2çW6‚‡¶¶–æC¢w6†÷rrÇG3§&÷rçG2ÆÆ—fS¦fÇ6RÆFF§&÷wÒ“°¢6öç7B&VÅvF6ƒÒ‚…ö×”Æ—7DfdFFç6†÷w7ÇÅµÒ’æÆVæwF‚²…ö×”Æ—7DfdFFæÖ÷f–W7ÇÅµÒ’æÆVæwF‚“ã°¢–b…÷&öf–ÆT6öæf–rç6WGWöFVÖõö6öçFVçBbb&VÅvF6‚—°¢ÖöÖVçG2çW6‚‡¶¶–æC¢w6†÷rrÇG3¦æ÷rÓb£3cÆÆ—fS¦fÇ6RÆFF§·W6öÖ–æs¦fÇ6RÆW§·6†÷uöæÖS¢tW†×ÆR6†÷rrÇ6V6öã£ÆW—6öFUöçVÓ£ÇF—FÆS¢uvVÆ6öÖRFòEdÖFRrÆ6÷fW#§6WGWFVÖô6÷fW"‚tU„ÕÄR4„õrrÂr3v6C"r’Æf–Æ&ÆS¦fÇ6RÇ6W&–W5ö–C¢rrÆ6FÆöuö–C¢rw××Ò“°¢ÖöÖVçG2çW6‚‡¶¶–æC¢vÖ÷f–RrÇG3¦æ÷r³3b£3cÆÆ—fS¦fÇ6RÆFF§¶Ö÷f–S§¶æÖS¢tW†×ÆRÖ÷f–RrÇ–V#¦æWrFFR‚’ævWDgVÆÅ–V"‚’Æ6÷fW#§6WGWFVÖô6÷fW"‚tU„ÕÄRÔõd”RrÂr3cFs"r’Ç7G&VÕöf÷VæC¦fÇ6W××Ò“°¢Ð¢F–ÖVÆ–æTÆöE&Vg2‚“°¢6öç7B6öçG&öÇ3×F–ÖVÆ–æT6öçG&öÇ4‡FÖÂ‚“°¢ÆWBf–ÇFW&VCÖÖöÖVçG2æf–ÇFW"†ÓÓåö×•F–ÖVÆ–æTf–ÇFW#ÓÓÒvÆÂwÇÇF–ÖVÆ–æTf–ÇFW$w&÷W†Òæ¶–æB“ÓÓÕö×•F–ÖVÆ–æTf–ÇFW"“°¢–b‚ö×•F–ÖVÆ–æU6WGF–æw2ç&V6VçB–f–ÇFW&VCÖf–ÇFW&VBæf–ÇFW"†ÓÓæÒæÆ—fWÇÆÒçG3ãÖæ÷r“°¢–b‚ö×•F–ÖVÆ–æU6WGF–æw2æÆ—fR–f–ÇFW&VCÖf–ÇFW&VBæf–ÇFW"†ÓÓâÒæÆ—fR“°¢–b‚ö×•F–ÖVÆ–æU6WGF–æw2çW6öÖ–ær–f–ÇFW&VCÖf–ÇFW&VBæf–ÇFW"†ÓÓæÒæÆ—fWÇÆÒçG3Ææ÷r“°¢6öç7BÖ…W$6FVv÷'“ÔÖF‚æÖ‚ƒÄçVÖ&W"…ö×•F–ÖVÆ–æU6WGF–æw2æÖ…W$6FVv÷'—ÇÃ’“°¢–b†Ö…W$6FVv÷'’—°¢6öç7B¶VWÖæWr6WB‚’Æw&÷W3ÖæWrÖ‚“°¢f÷"†6öç7BÒöbf–ÇFW&VB—¶6öç7B¶W“×F–ÖVÆ–æTf–ÇFW$w&÷W†Òæ¶–æB“¶–b‚w&÷W2æ†2†¶W’’–w&÷W2ç6WB†¶W’ÅµÒ“¶w&÷W2ævWB†¶W’’çW6‚†Ò“·Ð¢f÷"†6öç7B&÷w2öbw&÷W2çfÇVW2‚’–f÷"†6öç7BÒöb&÷w2ç6÷'B‚†Æ"“ÓäÖF‚æ'2†çG2Öæ÷r’ÔÖF‚æ'2†"çG2Öæ÷r’’ç6Æ–6RƒÆÖ…W$6FVv÷'’’–¶VWæFB†Ò“°¢f–ÇFW&VCÖf–ÇFW&VBæf–ÇFW"†ÓÓæ¶VWæ†2†Ò’“°¢Ð¢–b‚f–ÇFW&VBæÆVæwF‚—¶6öç7BV×G“Ö6öçG&öÇ2²sÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ'FF–æs£'‚#âr·G"‚tæ÷F†–ær†Væ–ær&÷VæBæ÷râr’²sÂöF—câs¶–b†VÂ–VÂæ–ææW$…DÔÃÖV×G“¶–b‡7FæFÆöæR—7FæFÆöæRæ–ææW$…DÔÃÖV×G“·&WGW&ã·Ð¢6öç7B&V6VçCÖf–ÇFW&VBæf–ÇFW"†ÓÓâÒæÆ—fRbfÒçG3Ææ÷r’ç6÷'B‚†Æ"“ÓæçG2Ö"çG2’æÖ†ÓÓäö&¦V7Bæ76–vâ‡·6V7F–öã¢w&V6VçBwÒÆÒ’“°¢6öç7BÆ—fSÖf–ÇFW&VBæf–ÇFW"†ÓÓæÒæÆ—fR’ç6÷'B‚†Æ"“ÓæçG2Ö"çG2’æÖ†ÓÓäö&¦V7Bæ76–vâ‡·6V7F–öã¢vÆ—fRwÒÆÒ’“°¢6öç7BW6öÖ–æsÖf–ÇFW&VBæf–ÇFW"†ÓÓâÒæÆ—fRbfÒçG3ãÖæ÷r’ç6÷'B‚†Æ"“ÓæçG2Ö"çG2’æÖ†ÓÓäö&¦V7Bæ76–vâ‡·6V7F–öã¢wW6öÖ–ærwÒÆÒ’“°¢6öç7B÷&FW&VC×&V6VçBæ6öæ6B†Æ—fRÇW6öÖ–ær“°¢ÆWBƒÖ6öçG&öÇ2²sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æR#âs°¢ÆWB6V7F–öãÒrs°¢f÷"†6öç7BÖöÖVçBöb÷&FW&VB—°¢–b†ÖöÖVçBç6V7F–öâÓ×6V7F–öâ—·6V7F–öãÖÖöÖVçBç6V7F–öã¶6öç7BÆ&VÃ×6V7F–öãÓÓÒw&V6VçBs÷G"‚u&V6VçFÇ’r“¢‡6V7F–öãÓÓÒvÆ—fRs÷G"‚tÆ—fRæ÷rr“§G"‚uW6öÖ–ærr’“¶‚³ÒsÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æW6V7F–öâr·6V7F–öâ²r#âr¶W62†Æ&VÂ’²sÂöF—câs·Ð¢–b†ÖöÖVçBæ¶–æCÓÓÒwFVÒr—°¢6öç7B&÷sÖÖöÖVçBæFFÆc×&÷ræf—‡GW&S°¢6öç7Bv†Vã×&÷ræÆ—fS÷G"‚tÆ—fRæ÷rr“¢†bç7F'C÷F–ÖVÆ–æUW6öÖ–æuv†Vâ‡&÷rçG2ÆfÇ6R“§G"‚tæW‡BÖF6‚r’“°¢‚³ÒsÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æVVçG'’r²‡&÷ræÆ—fSòr—2ÖÆ—fRs¢rr’²r#âr²‡&÷ræÆ—fSòrs¢sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWv†Vâ#âr¶W62‡v†Vâ’²sÂöF—câr’²sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV&öG’×–Æ—7GF–ÖVÆ–æV6öçFVçB#ãÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV¶–æB7÷'B#âr¶W62‡G"‚u7÷'G2r’’²sÂ÷7ãâr¶×”Æ—7E7÷'D'Gv÷&²†b’·FVÔf—‡GW&T6&B†bÇ&÷ræÆ—fRÇG'VR’²sÂöF—cãÂöF—câs°¢ÖVÇ6R–b†ÖöÖVçBæ¶–æCÓÓÒvcr—°¢6öç7B&÷sÖÖöÖVçBæFFÆWfVçC×&÷ræWfVçBÆFFSÖæWrFFR‡&÷rçG2’Çv†VãÖÖöÖVçBæÆ—fS÷G"‚tÆ—fRæ÷rr“§F–ÖVÆ–æUW6öÖ–æuv†Vâ‡&÷rçG2ÂWfVçBæÆÅöF’“°¢6öç7B&6–æuW&ÃÖWfVçBçW&ÇÇÂ‚v‡GG3¢ò÷wwræf÷&×VÆæ6öÒöVâ÷&6–æròr¶FFRævWDgVÆÅ–V"‚’“°¢6öç7Bf–Æ&ÆSÒ†WfVçBæ6†ææVÇ7ÇÅµÒ’æÆVæwFƒòsÇ7â6Æ73Ò&62×–Æ—7GF–ÖVÆ–æVf–Â"F—FÆSÒ"r¶W64GG"‡G"‚t6†ææVÇ2f–Æ&ÆRr’’²r#åEcÂ÷7ãâs¢rs°¢‚³ÒsÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æVVçG'’r²†ÖöÖVçBæÆ—fSòr—2ÖÆ—fRs¢rr’²r#âr²†ÖöÖVçBæÆ—fSòrs¢sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWv†Vâ#âr¶W62‡v†Vâ’²sÂöF—câr’²sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV&öG’×–Æ—7GF–ÖVÆ–æV6öçFVçB×–Æ—7GF–ÖVÆ–æVcr²‚†WfVçBæ6†ææVÇ7ÇÅµÒ’æÆVæwFƒòr†66†ææVÇ2s¢rr’²r"FFÖG&—fW"Ö¶W“Ò"r¶W64GG"†×”Æ—7E&6–ætFWF–Ä¶W’†WfVçB’’²r"FF×W&ÃÒ"r¶W64GG"‡&6–æuW&Â’²r#ãÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV¶–æBc#âr¶W62‡G"‚u&6–ærr’’²sÂ÷7ãâr¶×”Æ—7E&6–æt'Gv÷&²†WfVçB’²sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWFW‡B7&VB#âr·&6–æuF–ÖVÆ–æTf7G2†WfVçBÆW62†WfVçBç&6R’’²sÂöF—câr·F–ÖVÆ–æT6–FR†ÖöÖVçBæÆ—fSòrs§&6–æt6÷VçFF÷vâ†WfVçB’Â†WfVçBæ6†ææVÇ7ÇÅµÒ’æÆVæwF‚ÆÖöÖVçBæÆ—fR’¶f–Æ&ÆR²sÂöF—cãÂöF—câs°¢ÖVÇ6R–b†ÖöÖVçBæ¶–æCÓÓÒvÖ÷f–Rr—°¢6öç7B&÷sÖÖöÖVçBæFFÆÓ×&÷ræÖ÷f–RÆ6÷fW#ÖÒæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†Òæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rrÇv†Vã×&÷rçG3ÄFFRææ÷r‚“÷F–ÖVÆ–æU&VÆV6VEv†Vâ‡&÷rçG2“§F–ÖVÆ–æUW6öÖ–æuv†Vâ‡&÷rçG2ÇG'VR“°¢6öç7B7F–öãÖÒç7G&VÕöf÷VæCòsÆF—b6Æ73Ò&Ö÷f–V7F–öç2#ãÇ7â6Æ73Ò&Ö÷f–VÖWF#âr·G"‚u7G&VÒf÷VæB–âÆ–Æ—7Br’²sÂ÷7ããÆ'WGFöâ6Æ73Ò&'FçfÆ2Ö÷f–WfÆ2"FF×6–CÒ"r¶W64GG"…7G&–ær†Òç7G&VÕö–B’’²r"FFÖW‡CÒ"r¶W64GG"†ÒæW‡FVç6–öçÇÂv×Br’²r#âb3“cSƒ²dÄ3Âö'WGFöããÂöF—câs¢sÆF—b6Æ73Ò&Ö÷f–V7F–öç2#ãÆ'WGFöâ6Æ73Ò&v†÷7B"F—6&ÆVCâr·G"‚tæ÷Bf–Æ&ÆRr’²sÂö'WGFöããÂöF—câs°¢6öç7BÖ÷f–T†VC×&÷rçG3äFFRææ÷r‚“÷&6–æt6÷VçFF÷vâ‡·7F'C¦æWrFFR‡&÷rçG2’çFô•4õ7G&–ær‚—Ò“¢rs°¢6öç7BÖ÷f–U7FFSÖÒç7G&VÕöf÷Væ@¢òsÇ7â6Æ73Ò&Ö÷f–Vf–ÂFÆf–Â#âb33²r¶W62‡G"‚tf–Æ&ÆRr’’²sÂ÷7ãâp¢¢sÇ7â6Æ73Ò'FÇVæf–Â#âr¶W62‡G"‚tæ÷Bf–Æ&ÆRr’’²sÂ÷7ãâs°¢6öç7BÖ÷f–T'FãÖÒç7G&VÕöf÷VæCòsÆ'WGFöâ6Æ73Ò&'FçfÆ2Ö÷f–WfÆ2"FF×6–CÒ"r¶W64GG"…7G&–ær†Òç7G&VÕö–B’’²r"FFÖW‡CÒ"r¶W64GG"†ÒæW‡FVç6–öçÇÂv×Br’²r#âb3“cSƒ²dÄ3Âö'WGFöãâs¢rs°¢‚³ÒsÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æVVçG'’#ãÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWv†Vâ#âr¶W62‡v†Vâ’²sÂöF—cãÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV&öG’×–Æ—7GF–ÖVÆ–æV6öçFVçB×–Æ—7GF–ÖVÆ–æW÷7FW"#ãÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV¶–æBÖ÷f–R#âr¶W62‡G"‚tÖ÷f–Rr’’²sÂ÷7ãâr¶6÷fW"²sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWFW‡B7&VB#ãÇ7â6Æ73Ò'FÆf7BFÆÆVB#âr¶W62†Òç–V'ÇÇG"‚tÖ÷f–Rr’’²sÂ÷7ããÆ"6Æ73Ò'FÆ†VFÆ–æR#âr¶W62†ÒææÖR’²sÂö#ãÆF—b6Æ73Ò'FÆf7G2#âr¶Ö÷f–U7FFR²sÂöF—cãÂöF—cãÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6–FR#âr²†Ö÷f–T†VCòsÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6÷VçB#âr¶W62†Ö÷f–T†VB’²sÂ÷7ãâs¢rr’¶Ö÷f–T'Fâ²sÂöF—cãÂöF—cãÂöF—câs°¢ÖVÇ6R–b†ÖöÖVçBæ¶–æCÓÓÒvvÖRr—°¢6öç7B&÷sÖÖöÖVçBæFFÆs×&÷rævÖRÆ6÷fW#Öræ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†ræ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rrÇv†Vã×&÷rçG3ÄFFRææ÷r‚“÷F–ÖVÆ–æU&VÆV6VEv†Vâ‡&÷rçG2“§F–ÖVÆ–æUW6öÖ–æuv†Vâ‡&÷rçG2ÇG'VR“°¢6öç7BvÖUW&ÃÖrçW&ÇÇÂ‚v‡GG3¢ò÷7F÷&Rç7FV×÷vW&VBæ6öÒöòr¶Væ6öFUU$”6ö×öæVçB…7G&–ær†ræö–GÇÂrr’’²ròr“°¢6öç7BvÖT†VC×&÷rçG3äFFRææ÷r‚“÷&6–æt6÷VçFF÷vâ‡·7F'C¦æWrFFR‡&÷rçG2’çFô•4õ7G&–ær‚—Ò“¢rs°¢‚³ÒsÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æVVçG'’#ãÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWv†Vâ#âr¶W62‡v†Vâ’²sÂöF—cãÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV&öG’×–Æ—7GF–ÖVÆ–æV6öçFVçB×–Æ—7GF–ÖVÆ–æVvÖR"FF×W&ÃÒ"r¶W64GG"†vÖUW&Â’²r#ãÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV¶–æBvÖR#âr¶W62‡G"‚tvÖRr’’²sÂ÷7ãâr¶6÷fW"²sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWFW‡B7&VB#ãÇ7â6Æ73Ò'FÆf7BFÆÆVB#å7FVÓÂ÷7ããÆ"6Æ73Ò'FÆ†VFÆ–æR#âr¶W62†rææÖWÇÂtvÖRr’²sÂö#ãÆF—b6Æ73Ò'FÆf7G2#ãÇ7ãâr¶W62†rç&VÆV6U÷FW‡GÇÂrr’²sÂ÷7ããÂöF—cãÂöF—câr·F–ÖVÆ–æT6–FR†vÖT†VBÃÆfÇ6R’²sÂöF—cãÂöF—câs°¢ÖVÇ6W°¢6öç7B&÷sÖÖöÖVçBæFFÆW×&÷ræWÆ6÷fW#ÖWæ6÷fW#òsÆ–Ör7&3Ò"r¶W64GG"†Wæ6÷fW"’²r"ÇCÒ""ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢rrÆf–Æ&ÆSÒ‚&÷rçW6öÖ–ærbfWæf–Æ&ÆR“òsÇ7â6Æ73Ò&62×–Æ—7GF–ÖVÆ–æVf–Â"F—FÆSÒ$f–Æ&ÆRFòÆ’#âb3“cSC³Â÷7ãâs¢rs°¢6öç7Bv†Vã×&÷rçG3ÄFFRææ÷r‚“÷F–ÖVÆ–æU&VÆV6VEv†Vâ‡&÷rçG2“§F–ÖVÆ–æUW6öÖ–æuv†Vâ‡&÷rçG2ÆfÇ6R“°¢6öç7B6†÷t†VC×&÷rçG3äFFRææ÷r‚“÷&6–æt6÷VçFF÷vâ‡·7F'C¦æWrFFR‡&÷rçG2’çFô•4õ7G&–ær‚—Ò“¢rs°¢6öç7B6V6öäÆ&VÃ×G"‚u6V6öâr’²rr¶W62†Wç6V6öâ“°¢6öç7BW—6öFTÆ&VÃ×G"‚tW—6öFRr’²rr¶W62†WæW—6öFUöçVÒ“°¢‚³ÒsÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æVVçG'’#ãÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWv†Vâ#âr¶W62‡v†Vâ’²sÂöF—cãÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV&öG’×–Æ—7GF–ÖVÆ–æV6öçFVçB×–Æ—7GF–ÖVÆ–æW÷7FW"×–Æ—7G6†÷v6&B"FF×6W&–W3Ò"r¶W64GG"…7G&–ær†Wç6W&–W5ö–GÇÂrr’’²r"FFÖ6FÆösÒ"r¶W64GG"†Wæ6FÆöuö–GÇÂrr’²r#ãÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV¶–æB6†÷r#âr¶W62‡G"‚u6†÷rr’’²sÂ÷7ãâr¶6÷fW"²sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æWFW‡B7&VB#ãÇ7â6Æ73Ò'FÆf7BFÆÆVB#âr¶W62‡6V6öäÆ&VÂ’²sÂ÷7ããÆ"6Æ73Ò'FÆ†VFÆ–æR#âr¶W62†Wç6†÷uöæÖR’²sÂö#ãÆF—b6Æ73Ò'FÆf7G2#ãÇ7â6Æ73Ò'FÆWF—FÆR#âr¶W62†WçF—FÆWÇÇG"‚tW—6öFRr’’²sÂ÷7ããÇ7ãâr¶W62†W—6öFTÆ&VÂ’²sÂ÷7ããÂöF—cãÂöF—câr²‡6†÷t†VCòsÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6–FR#ãÇ7â6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6÷VçB#âr¶W62‡6†÷t†VB’²sÂ÷7ããÂöF—câs¢sÆF—b6Æ73Ò&×–Æ—7GF–ÖVÆ–æV6–FR#ãÂöF—câr’¶f–Æ&ÆR²sÂöF—cãÂöF—câs°¢Ð¢Ð¢6öç7B‡FÖÃÖ‚²sÂöF—câs¶–b†VÂ–VÂæ–ææW$…DÔÃÖ‡FÖÃ¶–b‡7FæFÆöæR—7FæFÆöæRæ–ææW$…DÔÃÖ‡FÖÃ°§Ð¦7–æ2gVæ7F–öâ&VÖ÷fTfd6B†6B—°¢v—Bfe÷7B‡¶7F–öã¢w&VÖ÷fUö6BrÆ6FVv÷'“¦6GÒ“°¢ÆöDff÷&—FW2‚“°§Ð¦7–æ2gVæ7F–öâ&VÖ÷fTfd6†â‡6–B—°¢v—Bfe÷7B‡¶7F–öã¢w&VÖ÷fUö6†ææVÂrÇ7G&VÕö–C§6–GÒ“°¢ÆöDff÷&—FW2‚“°§Ð¦7–æ2gVæ7F–öâFövvÆTfd6†ææVÂ‡6–BÆæÖRÆ6BÇ7F$VÂ—°¢6öç7B#Öv—Bfe÷7B‡¶7F–öã¢wFövvÆUö6†ææVÂrÇ7G&VÕö–C§6–BÆæÖS¦æÖRÆ6FVv÷'“¦6GÒ“°¢6öç7B–G3ÖæWr6WB‚‡"æ6†ææVÅö–G7ÇÅµÒ’æÖ…7G&–ær’“°¢öfd6†å6WCÖ–G3°¢–b‡7F$VÂ—7F$VÂæ6Æ74Æ—7BçFövvÆR‚vöârÆ–G2æ†2…7G&–ær‡6–B’’“°§Ð¦7–æ2gVæ7F–öâfe6VÆV7FVD6G2‚—°¢6öç7B6G3Ô'&’æg&öÒ…÷6VÄ6G2“°¢–b‚6G2æÆVæwF‚—¶ÆW'B‚uF–6²6öÖR6FVv÷&–W2f—'7Bâr“·&WGW&ã·Ð¢v—Bfe÷7B‡¶7F–öã¢vFEö6G2rÆ6FVv÷&–W3¦6G7Ò“°¢Fö7B‚tFFVBr¶6G2æÆVæwF‚²r6FVv÷"r²†6G2æÆVæwFƒÓÓÓòw’s¢v–W2r’²rFò&öf–ÆRr“°§Ð¦7–æ2gVæ7F–öâfeÆ–Æ—7B‚—°¢6öç7B—FV×3Ô'&’æg&öÒ…÷Æ–Æ—7BæVçG&–W2‚’’æÖ†gVæ7F–öâ†·b—·&WGW&â·7G&VÕö–C¦·e³ÒÆæÖS¦·e³ÒææÖRÆ6FVv÷'“¦·e³Òæ6FVv÷'—ÇÂrwÓ·Ò“°¢–b‚—FV×2æÆVæwF‚—¶ÆW'B‚uÆ–Æ—7B—2V×G’âr“·&WGW&ã·Ð¢v—Bfe÷7B‡¶7F–öã¢vFEö6†ææVÇ2rÆ6†ææVÇ3¦—FV×7Ò“°¢Fö7B‚tFFVBr¶—FV×2æÆVæwF‚²r6†ææVÂr²†—FV×2æÆVæwFƒÓÓÓòrs¢w2r’²rFò&öf–ÆRr“°§Ð¦gVæ7F–öâFö7B†×6rÆGW&F–öâ—°¢ÆWBCÖFö7VÖVçBævWDVÆVÖVçD'”–B‚u÷Fö7Br“°¢–b‚B—·CÖFö7VÖVçBæ7&VFTVÆVÖVçB‚vF—br“·Bæ–CÒu÷Fö7Bs·Bç7G–ÆRæ775FW‡CÒw÷6—F–öã¦f—†VC¶&÷GFöÓ£#Gƒ¶ÆVgC£SS·G&ç6f÷&Ó§G&ç6ÆFU‚‚ÓSR“¶&6¶w&÷VæC§f"‚ÒÖ6&C"“¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æS"“¶6öÆ÷#§f"‚ÒÖfr“·FF–æs£‚‡ƒ¶&÷&FW"×&F—W3£‡ƒ·¢Ö–æFWƒ£#¶föçB×6—¦S£G‚s¶Fö7VÖVçBæ&öG’æVæD6†–ÆB‡B“·Ð¢BçFW‡D6öçFVçCÖ×6s·Bç7G–ÆRæ÷6—G“Òss°¢6ÆV%F–ÖV÷WB‡Båö‚“·Båöƒ×6WEF–ÖV÷WB†gVæ7F–öâ‚—·Bç7G–ÆRæ÷6—G“Òss·ÒÆGW&F–öçÇÃ##“°¢Bç7G–ÆRçG&ç6—F–öãÒv÷6—G’ã72s°§Ð¢òòÒÒÒÒ×’EbÒÒÒÐ¦ÆWB÷Ge6÷W&6SÒuõöfeõòs²òòuõöfeõòr÷"6FVv÷'’æÖP¦ÆWB÷Gd6†ææVÇ3ÕµÓ°¦ÆWB÷GeÆ––æsÖçVÆÃ°¦ÆWB÷GeÆ•&WVW7CÓÅ÷GeVæF–æu6–CÒrs°¦7–æ2gVæ7F–öâ–æ—D×—Gb‚—°¢v—B'V–ÆEGe&–Â‚“°¢v—BÆöEGe6÷W&6R‚uõöfeõòr“°§Ð¦7–æ2gVæ7F–öâ'V–ÆEGe&–Â‚—°¢6öç7B#Öv—B’‚rö’öff÷&—FW2r“°¢6öç7B&–ÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGe&–Âr“°¢ÆWBƒÒsÆ'WGFöâ6Æ73Ò'Gg7&2r²…÷Ge6÷W&6SÓÓÒuõöfeõòsòröâs¢rr’²r"FF×7&3Ò%õöfeõò#åÇS#cRr·G"‚tff÷&—FR6†ææVÇ2r’²sÂö'WGFöãâs°¢f÷"†6öç7B2öb‡"æ6FVv÷&–W7ÇÅµÒ’¢‚³ÒsÆ'WGFöâ6Æ73Ò'Gg7&2r²…÷Ge6÷W&6SÓÓÖ3òröâs¢rr’²r"FF×7&3Ò"r¶W64GG"†2’²r#ârµöfÆtf÷"†2’²rr¶W62†2’²sÂö'WGFöãâs°¢&–Âæ–ææW$…DÔÃÖƒ°§Ð¦7–æ2gVæ7F–öâÆöEGe6÷W&6R‡7&2—°¢÷Ge6÷W&6S×7&3°¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rçGg7&2r’æf÷$V6‚†gVæ7F–öâ†"—¶"æ6Æ74Æ—7BçFövvÆR‚vöârÆ"ævWDGG&–'WFR‚vFF×7&2r“ÓÓ×7&2“·Ò“°¢6öç7B&öG“ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGdwV–FT&öG’r“°¢&öG’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ'FF–æs£g‚#äÆöF–ærââãÂöF—câs°¢–b‡7&3ÓÓÒuõöfeõòr—°¢6öç7B#Öv—B’‚rö’öff÷&—FW2r“°¢÷Gd6†ææVÇ3Ò‡"æ6†ææVÇ7ÇÅµÒ’æÖ†gVæ7F–öâ†2—·&WGW&â·7G&VÕö–C¦2ç7G&VÕö–BÆæÖS¦2ææÖRÆ6FVv÷'“¦2æ6FVv÷'—ÇÂrrÇW&Ã¦2çW&ÂÆÆövó¦2æÆöv÷ÇÂrwÓ·Ò“°¢ÖVÇ6W°¢6öç7B#Öv—B’‚rö’ö6†ææVÇ3÷Òf6CÒr¶Væ6öFUU$”6ö×öæVçB‡7&2’“°¢÷Gd6†ææVÇ3Ò‡"æ6†ææVÇ7ÇÅµÒ’æÖ†gVæ7F–öâ†2—·&WGW&â·7G&VÕö–C¦2ç7G&VÕö–BÆæÖS¦2ææÖRÆ6FVv÷'“¦2æ6FVv÷'—ÇÂrrÇW&Ã¦2çW&ÂÆÆövó¦2æÆöv÷ÇÂrwÓ·Ò“°¢Ð¢v—B&Vg&W6„fe7FFR‚“°¢òò&W7F÷&RUrg&öÒF—6²öÖVÖ÷'’öæÇ’âVçFW&–ærÆ—fREb×W7Bæ÷B6–ÆVçFÇ¢òò&Vg&W6‚F†R&÷f–FW#²F†RWFFRUr'WGFöâ&VÖ–ç2F†RæWGv÷&²7F–öâà¢6öç7BWt–G3Õ÷Gd6†ææVÇ2æÖ†gVæ7F–öâ†2—·&WGW&â7G&–ær†2ç7G&VÕö–B“·Ò’æf–ÇFW"„&ööÆVâ“°¢–b†Wt–G2æÆVæwF‚—°¢G'—¶6öç7B£Öv—B’‚rö’öWsö66†VCÓf–G3Òr¶Væ6öFUU$”6ö×öæVçB†Wt–G2æ¦ö–â‚rÂr’’“¶–b‚¢æW'&÷"•÷GdWsÔö&¦V7Bæ76–vâ‡·ÒÅ÷GdWrÆ¢æWwÇÇ·Ò“·Ö6F6‚†R—·Ð¢Ð¢&VæFW%GdwV–FR‚“°¢Ö–&TWFõ&Vg&W6„Wr‚“°§Ð¦ÆWB÷GdWs×·Ó²òò7G&VÕö–BÓâ··F—FÆRÇ7F'E÷G2Ç7F÷÷G7ÒÂââåÐ¦ÆWB÷GdWFôWt6†V6´CÓÅ÷GdWFôWt'W7“ÖfÇ6S°¦7–æ2gVæ7F–öâÖ–&TWFõ&Vg&W6„Wr‚—°¢6öç7Bæ÷sÔFFRææ÷r‚“¶–b…÷GdWFôWt'W7—ÇÆæ÷rÕ÷GdWFôWt6†V6´CÃR£c£—&WGW&ã°¢÷GdWFôWt6†V6´CÖæ÷sµ÷GdWFôWt'W7“×G'VS°¢G'—°¢òòF†R6W'fW"6öçF7G2F†R&÷f–FW"öæÇ’f÷"Ö—76–ær÷"ã"Ö†÷W"ÖöÆB&÷w2à¢6öç7B£Öv—B’‚rö’öWsöff÷&—FW3Ór“°¢–b‚¢æW'&÷"—µ÷GdWsÔö&¦V7Bæ76–vâ‡·ÒÅ÷GdWrÆ¢æWwÇÇ·Ò“¶–b‚×—Gef–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’—&VæFW%GdwV–FR‚“·Ð¢Ö6F6‚†R—·Öf–æÆÇ—µ÷GdWFôWt'W7“ÖfÇ6S·Ð§Ð¢òò¶VWF†RwV–FR6Æö6²Ö÷f–ærWfVâv†VâÆ—fREb—2ÆVgB÷VââF†—2öæÇ¢òò&R×&VæFW'2Ç&VG’66†VBFF²—BæWfW"&Vg&W6†W2Ur÷fW"F†RæWGv÷&²à§6WD–çFW'fÂ†gVæ7F–öâ‚—°¢–b‚×—Gef–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’be÷Gd6†ææVÇ2æÆVæwF‚—&VæFW%GdwV–FR‚“°§ÒÃc£“°¦gVæ7F–öâ&VæFW%GdwV–FR‚—°¢6öç7B†VCÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGeF–ÖT†VBr“°¢6öç7B&öG“ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGdwV–FT&öG’r“°¢òò6–×ÆRF–ÖR†VFW"g&öÒF†R7W'&VçB†Æb†÷W ¢6öç7BCÖæWrFFR‚“¶Bç6WDÖ–çWFW2†BævWDÖ–çWFW2‚“Ã3ó£3ÃÃ“°¢6öç7B&6SÖBævWEF–ÖR‚“°¢6öç7B6Æ÷E7F'CÕµÓ°¢f÷"†ÆWB“Ó¶“ÃS¶’²²—·6Æ÷E7F'BçW6‚†&6R¶’£3£c“·Ð¢6öç7Bæ÷u7CÔÖF‚æÖ‚ƒÄÖF‚æÖ–âƒÂ„FFRææ÷r‚’Ö&6R’òƒR£3£c’£’“°¢†VBæ–ææW$…DÔÃ×6Æ÷E7F'BæÖ†gVæ7F–öâ†×2—¶6öç7BCÖæWrFFR†×2“·&WGW&âsÆF—b6Æ73Ò'GgF–ÖW6Æ÷B#âr²‚sr·BævWD†÷W'2‚’’ç6Æ–6R‚Ó"’²s¢r²‚sr·BævWDÖ–çWFW2‚’’ç6Æ–6R‚Ó"’²sÂöF—câs·Ò’æ¦ö–â‚rr’²sÇ7â6Æ73Ò'Gfæ÷v†VB"7G–ÆSÒ&ÆVgC¢r¶æ÷u7BçFôf—†VBƒ2’²rR#ãÂ÷7ãâs°¢6öç7Bv–å7F'C×6Æ÷E7F'E³ÒÂv–äVæC×6Æ÷E7F'E³EÒ³3£c°¢–b‚÷Gd6†ææVÇ2æÆVæwF‚—¶&öG’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&×WFVB"7G–ÆSÒ'FF–æs£g‚#âr·G"‚tæò6†ææVÇ2†W&Râr’²sÂöF—câs·&WGW&ã·Ð¢ÆWBƒÒrs°¢f÷"†6öç7B2öb÷Gd6†ææVÇ2—°¢6öç7BÆ––æsÒ…÷GeÆ––ærÓÖçVÆÂbe7G&–ær…÷GeÆ––ær“ÓÓÕ7G&–ær†2ç7G&VÕö–B’“òrÆ––ærs¢rs°¢6öç7BfcÕöfd6†å6WBæ†2…7G&–ær†2ç7G&VÕö–B’“òröâs¢rs°¢‚³ÒsÆF—b6Æ73Ò'Gg&÷r"FF×6–CÒ"r¶W64GG"…7G&–ær†2ç7G&VÕö–B’’²r#âp¢²sÆF—b6Æ73Ò'Gf6†âr·Æ––ær²r"FF×6–CÒ"r¶W64GG"…7G&–ær†2ç7G&VÕö–B’’²r#âp¢²…÷Ge6÷W&6SÓÓÒuõöfeõòsòsÇ7â6Æ73Ò'GfG&r"G&vv&ÆSÒ'G'VR"F—FÆSÒ$G&rFò&V÷&FW"#âb3“ssc³Â÷7ãâs¢rr¢²sÆ'WGFöâ6Æ73Ò'GgfÆ2"FF×6–CÒ"r¶W64GG"…7G&–ær†2ç7G&VÕö–B’’²r#ådÄ3Âö'WGFöãâp¢²†2æÆövóö6†ææVÄÆövò†2ÂwGfÆövòr“¢sÇ7â6Æ73Ò'GffÆr#ârµöfÆtf÷"†2æ6FVv÷'—ÇÆ2ææÖR’²sÂ÷7ãâr¢²sÇ7â6Æ73Ò'GfæÖR#âr¶W62†2ææÖR’²sÂ÷7ãâp¢²sÇ7â6Æ73Ò&fg7F"r¶fb²r"FF×6–CÒ"r¶W64GG"…7G&–ær†2ç7G&VÕö–B’’²r"FFÖæÖSÒ"r¶W64GG"†2ææÖR’²r"FFÖ6CÒ"r¶W64GG"†2æ6FVv÷'—ÇÂrr’²r"F—FÆSÒ$ff÷&—FR#åÇS#cSÂ÷7ãâp¢²sÂöF—câp¢²sÆF—b6Æ73Ò'Gg&ör"7G–ÆSÒ"ÒÖæ÷w7C¢r¶æ÷u7BçFôf—†VBƒ2’²rR#âr¶Wt6VÆÄ‡FÖÂ†2ç7G&VÕö–BÇv–å7F'BÇv–äVæB’²sÂöF—cãÂöF—câs°¢Ð¢&öG’æ–ææW$…DÔÃÖƒ°§Ð¦ÆWB÷GdG&u6–CÖçVÆÃ°¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚vG&w7F'BrÆgVæ7F–öâ†R—°¢6öç7B†æFÆSÖRçF&vWBæ6Æ÷6W7B‚rçGfG&rr“°¢–b‚†æFÆWÇÅ÷Ge6÷W&6RÓÒuõöfeõòr—&WGW&ã°¢6öç7B&÷sÖ†æFÆRæ6Æ÷6W7B‚rçGg&÷rr“°¢÷GdG&u6–C×&÷s÷&÷rævWDGG&–'WFR‚vFF×6–Br“¦çVÆÃ°¢–b‚÷GdG&u6–B—&WGW&ã°¢&÷ræ6Æ74Æ—7BæFB‚wGfG&vv–ærr“°¢RæFFG&ç6fW"æVffV7DÆÆ÷vVCÒvÖ÷fRs°¢RæFFG&ç6fW"ç6WDFF‚wFW‡B÷Æ–ârÅ÷GdG&u6–B“°§Ò“°¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚vG&v÷fW"rÆgVæ7F–öâ†R—°¢6öç7B&÷sÖRçF&vWBæ6Æ÷6W7B‚rçGg&÷rr“°¢–b‚÷GdG&u6–GÇÂ&÷wÇÅ÷Ge6÷W&6RÓÒuõöfeõòr—&WGW&ã°¢Rç&WfVçDFVfVÇB‚“°¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rçGg&÷rçGfG&v÷fW"r’æf÷$V6‚‡#Óç"æ6Æ74Æ—7Bç&VÖ÷fR‚wGfG&v÷fW"r’“°¢&÷ræ6Æ74Æ—7BæFB‚wGfG&v÷fW"r“°¢RæFFG&ç6fW"æG&÷VffV7CÒvÖ÷fRs°§Ò“°¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚vG&÷rÆ7–æ2gVæ7F–öâ†R—°¢6öç7B&÷sÖRçF&vWBæ6Æ÷6W7B‚rçGg&÷rr“°¢–b‚÷GdG&u6–GÇÂ&÷wÇÅ÷Ge6÷W&6RÓÒuõöfeõòr—&WGW&ã°¢Rç&WfVçDFVfVÇB‚“°¢6öç7BF&vWE6–C×&÷rævWDGG&–'WFR‚vFF×6–Br“°¢6öç7Bg&öÓÕ÷Gd6†ææVÇ2æf–æD–æFW‚†3Óå7G&–ær†2ç7G&VÕö–B“ÓÓÕ7G&–ær…÷GdG&u6–B’“°¢6öç7BFóÕ÷Gd6†ææVÇ2æf–æD–æFW‚†3Óå7G&–ær†2ç7G&VÕö–B“ÓÓÕ7G&–ær‡F&vWE6–B’“°¢–b†g&öÓãÓbgFóãÓbfg&öÒÓ×Fò—°¢6öç7BÖ÷fVCÕ÷Gd6†ææVÇ2ç7Æ–6R†g&öÒÃ•³Ó°¢÷Gd6†ææVÇ2ç7Æ–6R‡FòÃÆÖ÷fVB“°¢&VæFW%GdwV–FR‚“°¢v—Bfe÷7B‡¶7F–öã¢w&V÷&FW%ö6†ææVÇ2rÇ7G&VÕö–G3¥÷Gd6†ææVÇ2æÖ†3Óæ2ç7G&VÕö–B—Ò“°¢Ð¢÷GdG&u6–CÖçVÆÃ°§Ò“°¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚vG&vVæBrÆgVæ7F–öâ‚—°¢÷GdG&u6–CÖçVÆÃ°¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rçGg&÷rçGfG&vv–ærÂçGg&÷rçGfG&v÷fW"r’æf÷$V6‚‡#Óç"æ6Æ74Æ—7Bç&VÖ÷fR‚wGfG&vv–ærrÂwGfG&v÷fW"r’“°§Ò“°¦gVæ7F–öâWuvÆÄ6Æö6µG2‡fÇVRÆfÆÆ&6²—°¢òò‡G&VÒw2Ur7F'FöVæF7G&–æw2&R66†VGVÆRvÆÂÖ6Æö6²fÇVW2â6öÖP¢òò6W'fW'2Ç6òW‡÷6R7F'E÷F–ÖW7F×2–bF†BvÆÂ6Æö6²vW&RUD3²W6–æp¢òòF†BWö6‚–â'&÷w6W"F†Vâ6†–gG2æ÷'vVv–âÆ—7F–æw2'’³ò³"†÷W'2à¢òò'V–ÆBF†R&r66†VGVÆRF–ÖR–âF†Rf–WvW"w2Æö6ÂF–ÖW¦öæRv†Vâf–Æ&ÆRà¢6öç7B3Õ7G&–ær‡fÇVWÇÂrr’çG&–Ò‚’ÆÓ×2æÖF6‚‚õâ…ÅÆG³GÒ’Ò…ÅÆG³'Ò’Ò…ÅÆG³'Ò•²EÒ…ÅÆG³Ã'Ò“¢…ÅÆG³'Ò’ƒó£¢…ÅÆG³'Ò’“òò“°¢–b†Ò—¶6öç7BCÖæWrFFR„çVÖ&W"†Õ³Ò’ÄçVÖ&W"†Õ³%Ò’ÓÄçVÖ&W"†Õ³5Ò’ÄçVÖ&W"†Õ³EÒ’ÄçVÖ&W"†Õ³UÒ’ÄçVÖ&W"†Õ³e×ÇÃ’“¶6öç7BG3ÖBævWEF–ÖR‚’ó¶–b„çVÖ&W"æ—4f–æ—FR‡G2’—&WGW&âG3·Ð¢&WGW&âçVÖ&W"†fÆÆ&6²—ÇÃ°§Ð¦gVæ7F–öâWt6VÆÄ‡FÖÂ‡6–BÇv–å7F'BÇv–äVæB—°¢6öç7B&öw3Õ÷GdWuµ7G&–ær‡6–B•Ó°¢–b‚&öw7ÇÂ&öw2æÆVæwF‚—&WGW&âsÇ7â6Æ73Ò&WvæöæR×WFVB#âr·G"‚tæò&öw&Ò–æfòr’²sÂ÷7ãâs°¢6öç7Bæ÷u6V3ÔFFRææ÷r‚’óÇw3×v–å7F'BóÇvS×v–äVæBóÇ7ãÔÖF‚æÖ‚ƒÇvR×w2“°¢6öç7BF–ÖVC×&öw2æf–ÇFW"†gVæ7F–öâ‡—°¢6öç7B7F'CÖWuvÆÄ6Æö6µG2‡ç7F'BÇç7F'E÷G2’Ç7F÷ÖWuvÆÄ6Æö6µG2‡æVæBÇç7F÷÷G2—ÇÇ7F'B³ƒ°¢–b‚çF—FÆWÇÂ7F'B—&WGW&âfÇ6S°¢&WGW&â7F÷çw2bg7F'CÇvS°¢Ò’ç6÷'B†gVæ7F–öâ†Æ"—·&WGW&âWuvÆÄ6Æö6µG2†ç7F'BÆç7F'E÷G2’ÖWuvÆÄ6Æö6µG2†"ç7F'BÆ"ç7F'E÷G2“·Ò“°¢–b‚F–ÖVBæÆVæwF‚—°¢òòæWfW"–ââW‡—&VB&öw&ÖÖRFòF†RÆVgBVFvRöbF†R7W'&VçBw&–Bà¢òòöæÇ’vVçV–æVÇ’W6öÖ–ær—FVÒÖ’W6RF†R6ö×7BfÆÆ&6²F—7Æ’à¢6öç7BæW‡C×&öw2æf–ÇFW"‡ÓççF—FÆRbfWuvÆÄ6Æö6µG2‡ç7F'BÇç7F'E÷G2“ã×w2’ç6÷'B‚†Æ"“ÓæWuvÆÄ6Æö6µG2†ç7F'BÆç7F'E÷G2’ÖWuvÆÄ6Æö6µG2†"ç7F'BÆ"ç7F'E÷G2’•³Ó°¢–b‚æW‡B—&WGW&âsÇ7â6Æ73Ò&WvæöæR×WFVB#âr·G"‚tæò&öw&Ò–æfòr’²sÂ÷7ãâs°¢6öç7BæW‡E7F'CÖWuvÆÄ6Æö6µG2†æW‡Bç7F'BÆæW‡Bç7F'E÷G2“¶–b†æW‡E7F'Cã×vR—&WGW&âsÇ7â6Æ73Ò&WvæöæR×WFVB#âr·G"‚tæò&öw&Ò–æfòr’²sÂ÷7ãâs°¢ÆWBFÓÒrs¶–b†æW‡E7F'B—¶6öç7BCÖæWrFFR†æW‡E7F'B£“·FÓÒ‚sr·BævWD†÷W'2‚’’ç6Æ–6R‚Ó"’²s¢r²‚sr·BævWDÖ–çWFW2‚’’ç6Æ–6R‚Ó"“·Ð¢&WGW&âsÇ7â6Æ73Ò&WvfÆÆ&6²#ãÇ7â6Æ73Ò&WwB#âr·FÒ²sÂ÷7ããÇ7â6Æ73Ò&WwF—FÆR#âr¶W62†æW‡BçF—FÆR’²sÂ÷7ããÂ÷7ãâs°¢Ð¢&WGW&âF–ÖVBæÖ†gVæ7F–öâ‡—°¢6öç7B7F'CÖWuvÆÄ6Æö6µG2‡ç7F'BÇç7F'E÷G2’Ç&u7F÷ÖWuvÆÄ6Æö6µG2‡æVæBÇç7F÷÷G2—ÇÇ7F'B³ƒÇ7F÷ÔÖF‚æÖ‚‡7F'B³cÇ&u7F÷“°¢6öç7Bf—6–&ÆU7F'CÔÖF‚æÖ‚‡w2Ç7F'B’Çf—6–&ÆU7F÷ÔÖF‚æÖ–â‡vRÇ7F÷“°¢6öç7BÆVgCÔÖF‚æÖ‚ƒÂ‡f—6–&ÆU7F'B×w2’÷7â£’Çv–GFƒÔÖF‚æÖ‚‚ã‚Â‡f—6–&ÆU7F÷×f—6–&ÆU7F'B’÷7â£“°¢6öç7BÆ—fS×7F'CÃÖæ÷u6V2bg7F÷ææ÷u6V3°¢6öç7BCÖæWrFFR‡7F'B£’ÇFÓÒ‚sr·BævWD†÷W'2‚’’ç6Æ–6R‚Ó"’²s¢r²‚sr·BævWDÖ–çWFW2‚’’ç6Æ–6R‚Ó"“°¢6öç7B6Ç3ÒvWw&örr²†Æ—fSòrÆ—fRs¢rr’²‡v–GFƒÃ#òr6ö×7Bs¢rr“°¢&WGW&âsÇ7â6Æ73Ò"r¶6Ç2²r"7G–ÆSÒ&ÆVgC¢r¶ÆVgBçFôf—†VBƒ2’²rS·v–GFƒ¦6Æ2‚r·v–GF‚çFôf—†VBƒ2’²rRÒ'‚’"F—FÆSÒ"r¶W64GG"‡FÒ²rr·çF—FÆR’²r#ãÇ7â6Æ73Ò&WwB#âr·FÒ²sÂ÷7ããÇ7â6Æ73Ò&WwF—FÆR#âr¶W62‡çF—FÆR’²sÂ÷7ããÂ÷7ãâs°¢Ò’æ¦ö–â‚rr“°§Ð¦gVæ7F–öâGeÆ–W$wV–FR‚—°¢&WGW&âFö7VÖVçBçVW'•6VÆV7F÷"‚r6×—Gef–WrçGfwV–FRr“°§Ð¦gVæ7F–öâGe6WDÖ–æ’†Ö–æ’—°¢6öç7B6Æ÷CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGeÆ–W%6Æ÷Br’ÆwV–FS×GeÆ–W$wV–FR‚“°¢–b‚6Æ÷GÇÂ6Æ÷Bæ6Æ74Æ—7Bæ6öçF–ç2‚vöâr’—&WGW&ã°¢6öç7B–äÆ—fUGcÒ×—Gef–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr“°¢–b†Ö–æ’—°¢–b‡6Æ÷Bç&VçDVÆVÖVçBÓÖFö7VÖVçBæ&öG’–Fö7VÖVçBæ&öG’æVæD6†–ÆB‡6Æ÷B“°¢6Æ÷Bæ6Æ74Æ—7Bç&VÖ÷fR‚w6V7F–öæÖ‚r“°¢6Æ÷Bæ6Æ74Æ—7BæFB‚vÖ–æ’r“°¢ÖVÇ6R–b†–äÆ—fUGb—°¢6Æ÷Bæ6Æ74Æ—7Bç&VÖ÷fR‚vÖ–æ’rÂw6V7F–öæÖ‚r“°¢–b†wV–FRbg6Æ÷Bç&VçDVÆVÖVçBÓÖwV–FR–wV–FRæVæD6†–ÆB‡6Æ÷B“°¢ÖVÇ6W°¢–b‡6Æ÷Bç&VçDVÆVÖVçBÓÖFö7VÖVçBæ&öG’–Fö7VÖVçBæ&öG’æVæD6†–ÆB‡6Æ÷B“°¢6Æ÷Bæ6Æ74Æ—7Bç&VÖ÷fR‚vÖ–æ’r“°¢6Æ÷Bæ6Æ74Æ—7BæFB‚w6V7F–öæÖ‚r“°¢Ð¢6öç7B'Fã×6Æ÷BçVW'•6VÆV7F÷"‚rçGfÖ–æ'Fâr’Æ†—C×6Æ÷BçVW'•6VÆV7F÷"‚rçGgf–FVö†—Br“°¢6öç7BÆ&VÃÖÖ–æ“òtgVÆÇ67&VVâÆ–W"s¢tÖ–æ–Ö—¦RÆ–W"s°¢–b†'Fâ—¶'FâçF—FÆSÖÆ&VÃ¶'Fâç6WDGG&–'WFR‚v&–ÖÆ&VÂrÆÆ&VÂ“¶'FâçFW‡D6öçFVçCÖÖ–æ“òuÇS#“bs¢uÇS#“‚s·Ð¢–b††—B–†—Bç6WDGG&–'WFR‚v&–ÖÆ&VÂrÆÆ&VÂ“°§Ð¦7–æ2gVæ7F–öâGeÆ’‡6–BÆæÖR—°¢6öç7B6Æ÷CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGeÆ–W%6Æ÷Br’ÆwV–FS×GeÆ–W$wV–FR‚“°¢6öç7B6–D¶W“Õ7G&–ær‡6–B“°¢–b…÷GeVæF–æu6–CÓÓ×6–D¶W’—&WGW&ã°¢÷GeVæF–æu6–C×6–D¶W“°¢6öç7B&WVW7CÒ²µ÷GeÆ•&WVW7C°¢6öç7Bv4Ö–æ“×6Æ÷Bæ6Æ74Æ—7Bæ6öçF–ç2‚vÖ–æ’r“°¢òò6–ævÆR×Æ–&6²'VÆS¢7F'F–ærÆ—fREb6Æ÷6W2ç’÷Vâ÷WÆ–W"à¢6öç7BÖöFÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wÆ–W$ÖöFÂr“°¢–b‡ÖöFÂbbÖöFÂæ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr’—·G'—¶6Æ÷6UÆ–W"‚“·Ö6F6‚†R—·×Ð¢÷GeÆ––æs×6–C°¢6Æ÷Bæ6Æ74Æ—7BæFB‚vöâr“°¢–b‚v4Ö–æ’bfwV–FRbg6Æ÷Bç&VçDVÆVÖVçBÓÖwV–FR–wV–FRæVæD6†–ÆB‡6Æ÷B“°¢6Æ÷Bæ–ææW$…DÔÃÒsÆF—b6Æ73Ò'GgÆ–W&&"#ãÇ7ãâr¶W62†æÖWÇÂrr’²sÂ÷7ããÆF—b6Æ73Ò'GgÆ–W&7F–öç2#ãÆ'WGFöâG—SÒ&'WGFöâ"6Æ73Ò'GfÖ–æ'Fâ"F—FÆSÒ$Ö–æ–Ö—¦RÆ–W""&–ÖÆ&VÃÒ$Ö–æ–Ö—¦RÆ–W""öæ6Æ–6³Ò'GeFövvÆTÖ–æ’‚’#âb3ƒc³Âö'WGFöããÆ'WGFöâ6Æ73Ò'6Æ÷6R"öæ6Æ–6³Ò'Ge7F÷‚’#âgF–ÖW3³Âö'WGFöããÂöF—cãÂöF—cãÇf–FVò–CÒ'Gef–FVò"6öçG&öÇ2WF÷Æ’Æ—6–æÆ–æSãÂ÷f–FVóãÆ'WGFöâG—SÒ&'WGFöâ"6Æ73Ò'Ggf–FVö†—B"&–ÖÆ&VÃÒ$Ö–æ–Ö—¦RÆ–W""öæ6Æ–6³Ò'GeFövvÆTÖ–æ’‚’#ãÂö'WGFöãâs°¢Ge6WDÖ–æ’‡v4Ö–æ’“°¢&VæFW%GdwV–FR‚“°¢6öç7Bf–FVóÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGef–FVòr“°¢–b‡v–æF÷rå÷GeÆ–&6´6öçG&öÆÆW"—·v–æF÷rå÷GeÆ–&6´6öçG&öÆÆW"ç7F÷‚“·v–æF÷rå÷GeÆ–&6´6öçG&öÆÆW#ÖçVÆÃ·Ð¢ÆWBW&Ç3°¢G'—·W&Ç3Öv—B’‚rö’ö†Ç3ö–CÒr¶Væ6öFUU$”6ö×öæVçB‡6–B’“¶–b‡W&Ç2æW'&÷'ÇÂW&Ç2æ†Ç2—F‡&÷ræWrW'&÷"‚w7G&VÒW&Âr“·Ö6F6‚†R—·&WGW&ã·Öf–æÆÇ—¶–b‡&WVW7CÓÓÕ÷GeÆ•&WVW7B•÷GeVæF–æu6–CÒrs·Ð¢–b‡&WVW7BÓÕ÷GeÆ•&WVW7B—&WGW&ã°¢v–æF÷rå÷GeÆ–&6´6öçG&öÆÆW#×7F'E6Ö'E7G&VÒ‡f–FVòÇW&Ç2ÆgVæ7F–öâ‡2—°¢6öç7B&#×6Æ÷BçVW'•6VÆV7F÷"‚rçGgÆ–W&&"7âr“¶–b†&"–&"çF—FÆS×7ÇÂrs°¢ÒÆgVæ7F–öâ†‚ÇB—·v–æF÷rå÷Gf†Ç3Öƒ·v–æF÷rå÷Gf×VwG3×C·Ò“°§Ð¦gVæ7F–öâGeFövvÆTÖ–æ’‚—°¢6öç7B6Æ÷CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGeÆ–W%6Æ÷Br“°¢–b‚6Æ÷GÇÂ6Æ÷Bæ6Æ74Æ—7Bæ6öçF–ç2‚vöâr’—&WGW&ã°¢6öç7B–äÆ—fUGcÒ×—Gef–Wræ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr“°¢–b‡6Æ÷Bæ6Æ74Æ—7Bæ6öçF–ç2‚vÖ–æ’r’—°¢–b†–äÆ—fUGb—·Ge6WDÖ–æ’†fÇ6R“·&WGW&ã·Ð¢Ge6WDÖ–æ’†fÇ6R“°¢&WVW7EÆ–W$gVÆÇ67&VVâ‡6Æ÷B“°¢&WGW&ã°¢Ð¢–b‡Æ–W$gVÆÇ67&VVäVÆVÖVçB‚“ÓÓ×6Æ÷B–W†—EÆ–W$gVÆÇ67&VVâ‚“°¢Ge6WDÖ–æ’‡G'VR“°§Ð¦gVæ7F–öâGe7F÷‚—°¢÷GeÆ•&WVW7B²³µ÷GeVæF–æu6–CÒrs°¢÷GeÆ––æsÖçVÆÃ°¢Fö7VÖVçBæ&öG’æ6Æ74Æ—7Bç&VÖ÷fR‚wGg6V7F–öçÆ’r“°¢–b‡v–æF÷rå÷GeÆ–&6´6öçG&öÆÆW"—·v–æF÷rå÷GeÆ–&6´6öçG&öÆÆW"ç7F÷‚“·v–æF÷rå÷GeÆ–&6´6öçG&öÆÆW#ÖçVÆÃ·Ð¢–b‡v–æF÷rå÷Gf†Ç2—·G'—·v–æF÷rå÷Gf†Ç2æFW7G&÷’‚“·Ö6F6‚†R—·×v–æF÷rå÷Gf†Ç3ÖçVÆÃ·Ð¢–b‡v–æF÷rå÷Gf×VwG2—¶FW7G&÷”×VwG5Æ–W"‡v–æF÷rå÷Gf×VwG2“·v–æF÷rå÷Gf×VwG3ÖçVÆÃ·Ð¢6öç7B6Æ÷CÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wGeÆ–W%6Æ÷Br’ÆwV–FS×GeÆ–W$wV–FR‚“°¢6Æ÷Bæ6Æ74Æ—7Bç&VÖ÷fR‚vöârÂvÖ–æ’rÂw6V7F–öæÖ‚r“°¢–b†wV–FRbg6Æ÷Bç&VçDVÆVÖVçBÓÖwV–FR–wV–FRæVæD6†–ÆB‡6Æ÷B“°¢6Æ÷Bæ–ææW$…DÔÃÒrs°¢&VæFW%GdwV–FR‚“°§Ð¦7–æ2gVæ7F–öâWu&Vg&W6‚‚—°¢6öç7B'FãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vWu&Vg&W6‚r“°¢6öç7BöÆCÖ'Fâæ–ææW$…DÔÃ°¢'Fâæ–ææW$…DÔÃÒsÇ7ãâr·G"‚tÆöF–ærUrâââr’²sÂ÷7ãâs¶'FâæF—6&ÆVC×G'VS°¢ÆWBÖöFÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vWtÆöE&öw&W72r“°¢–b‚ÖöFÂ—°¢ÖöFÃÖFö7VÖVçBæ7&VFTVÆVÖVçB‚vF—br“¶ÖöFÂæ–CÒvWtÆöE&öw&W72s¶ÖöFÂæ6Æ74æÖSÒvWvÆöF&6²s°¢ÖöFÂæ–ææW$…DÔÃÒsÆF—b6Æ73Ò&WvÆöF&÷‚#ãÆF—b6Æ73Ò&WvÆöGF—FÆR#âr¶W62‡G"‚uWFF–ærEbwV–FRr’’²sÂöF—cãÆF—b6Æ73Ò&WvÆöG7FvR"–CÒ&WtÆöE7FvR#ãÂöF—cãÆF—b6Æ73Ò&WvÆöF&"#ãÇ7â–CÒ&WtÆöD&"#ãÂ÷7ããÂöF—cãÆF—b6Æ73Ò&WvÆöFÖWF#ãÇ7â–CÒ&WtÆöD6÷VçB#ãòÂ÷7ããÇ7â–CÒ&WtÆöDf÷VæB#ãÂ÷7ããÂöF—cãÂöF—câs°¢Fö7VÖVçBæ&öG’æVæD6†–ÆB†ÖöFÂ“°¢ÖVÇ6RÖöFÂæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢6öç7B7FvSÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vWtÆöE7FvRr’Æ&#ÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vWtÆöD&"r’Æ6÷VçCÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vWtÆöD6÷VçBr’Æf÷VæCÖFö7VÖVçBævWDVÆVÖVçD'”–B‚vWtÆöDf÷VæBr“°¢G'—°¢7FvRçFW‡D6öçFVçC×G"‚tf–æF–ær6†ææVÇ2–â–÷W"ff÷&—FW2âââr“¶6÷VçBçFW‡D6öçFVçCÒrs¶f÷VæBçFW‡D6öçFVçCÒrs¶&"ç7G–ÆRçv–GFƒÒs2Rs°¢6öç7BÆãÖv—B’‚rö’öWu÷F&vWG2r“°¢–b‡ÆâæW'&÷"—F‡&÷ræWrW'&÷"‡ÆâæW'&÷'ÇÂtUrf–ÆVBr“°¢òò÷VÆFRv†BF†RW6W"—2Æöö¶–ærBf—'7BâF†R6ö×ÆWFRff÷&—FRö6FVv÷'¢òòwV–FR7F–ÆÂ&Vg&W6†W2gFW'v&G2Â'WBÆ&vRUræòÆöævW"Ö¶W2F†P¢òò7W'&VçFÇ’÷Vâ6FVv÷'’v—B&V†–æB‡VæG&VG2öbVç&VÆFVB6†ææVÇ2à¢6öç7Bf—6–&ÆT–G3Õ÷Gd6†ææVÇ2æÖ†3Óå7G&–ær†2ç7G&VÕö–GÇÂrr’’æf–ÇFW"„&ööÆVâ’Çf—6–&ÆU6WCÖæWr6WB‡f—6–&ÆT–G2“°¢6öç7BÆææVCÒ‡Æâæ–G7ÇÅµÒ’æÖ…7G&–ær’Æ–G3×f—6–&ÆT–G2æf–ÇFW"†–CÓçÆææVBæ–æ6ÇVFW2†–B’’æ6öæ6B‡ÆææVBæf–ÇFW"†–CÓâf—6–&ÆU6WBæ†2†–B’’“°¢6öç7BF÷FÃÖ–G2æÆVæwFƒ¶ÆWBWFFVCÓÆæôWsÓÆf–ÆVCÓÇ6fTÖöFSÖfÇ6S°¢6÷VçBçFW‡D6öçFVçC×F÷FÂ²rr·G"‚v6†ææVÇ2r“°¢f÷VæBçFW‡D6öçFVçC×G"‚töæR'VÆ²wV–FRF÷væÆöBr“°¢7FvRçFW‡D6öçFVçC×G"‚tF÷væÆöF–æræB&ö6W76–ærF†R&÷f–FW"EbwV–FRâââr“¶&"ç7G–ÆRçv–GFƒÒs‚Rs°¢ÆWBv—E7FWÓ°¢6öç7Bv—DÖW76vW3Õ·G"‚u'6–ær&öw&ÖÖR–æf÷&ÖF–öââââr’ÇG"‚tÖF6†–ærwV–FRFFFòff÷&—FR6†ææVÇ2âââr’ÇG"‚tÆ&vR&÷f–FW"wV–FW2Ö’F¶RÆ—GFÆRv†–ÆRâââr•Ó°¢6öç7Bv—EF–ÖW#×6WD–çFW'fÂ‚‚“Óç·7FvRçFW‡D6öçFVçC×v—DÖW76vW5´ÖF‚æÖ–â‡v—E7FW²²Çv—DÖW76vW2æÆVæwF‚Ó•Ó¶&"ç7G–ÆRçv–GFƒÔÖF‚æÖ–âƒƒ"Ã#‚·v—E7FW£B’²rRs·ÒÃ##“°¢ÆWB£°¢G'—¶£Öv—B’‚rö’öWsöf÷&6SÓfff÷&—FW3Ór“·Öf–æÆÇ—¶6ÆV$–çFW'fÂ‡v—EF–ÖW"“·Ð¢–b†¢æW'&÷"—F‡&÷ræWrW'&÷"†¢æW'&÷'ÇÂtUrf–ÆVBr“°¢÷GdWsÔö&¦V7Bæ76–vâ‡·ÒÅ÷GdWrÆ¢æWwÇÇ·Ò“°¢6öç7B3Ö¢ç7FG7ÇÇ·Ó·WFFVCÔçVÖ&W"‡2çWFFVB—ÇÃ¶æôWsÔçVÖ&W"‡2ææõöFF—ÇÃ¶f–ÆVCÔçVÖ&W"‡2æf–ÆVB—ÇÃ·6fTÖöFSÒ2ç6fUöÖöFS°¢6öç7B'VÆ³ÔçVÖ&W"‡2ç†ÖÇGeöf–ÆÆVB—ÇÃÆfÆÆ&6³ÔçVÖ&W"‡2æfÆÆ&6µ÷WFFVB—ÇÃ°¢6÷VçBçFW‡D6öçFVçC×F÷FÂ²rr·G"‚v6†ææVÇ26†V6¶VBr“°¢f÷VæBçFW‡D6öçFVçC×G"‚u„ÔÅEbr’²rr¶'VÆ²²†fÆÆ&6³ò‚r+rr·G"‚tfÆÆ&6²r’²rr¶fÆÆ&6²“¢rr’²r+rr·G"‚tæòUrr’²rr¶æôWr²†f–ÆVCò‚r+rr·G"‚tf–ÆVBr’²rr¶f–ÆVB“¢rr“¶&"ç7G–ÆRçv–GFƒÒsRs°¢&VæFW%GdwV–FR‚“°¢7FvRçFW‡D6öçFVçC×G"‚uEbwV–FR—2&VG’âr’²rr·WFFVB²rr·G"‚v6†ææVÇ2WFFVBâr“¶&"ç7G–ÆRçv–GFƒÒsRs°¢–b‚F÷FÂ—·Fö7B‡G"‚tæòff÷&—FW2FòÆöBUrf÷"âr’“·Ð¢VÇ6RFö7B‡G"‚tUrÆöFVBr’²s¢r·G"‚uWFFVBr’²rr·WFFVB²r+rr·G"‚tæòUrr’²rr¶æôWr²r+rr·G"‚tf–ÆVBr’²rr¶f–ÆVBÃs“°¢Ö6F6‚†R—·7FvRçFW‡D6öçFVçC×G"‚tUrf–ÆVBr’²s¢rµ7G&–ær†RbfRæÖW76vWÇÆR“¶&"ç7G–ÆRæ&6¶w&÷VæCÒr3†c&C3Rs·Fö7B‡G"‚tUrf–ÆVBr’“¶v—BæWr&öÖ—6R‡&W6öÇfSÓç6WEF–ÖV÷WB‡&W6öÇfRÃ#ƒ’“·Ð¢v—BæWr&öÖ—6R‡&W6öÇfSÓç6WEF–ÖV÷WB‡&W6öÇfRÃcS’“°¢–b†ÖöFÂ–ÖöFÂæ6Æ74Æ—7BæFB‚v†–FRr“°¢'Fâæ–ææW$…DÔÃÖöÆC¶'FâæF—6&ÆVCÖfÇ6S°§Ð¢òòWfVçBFVÆVvF–öã¢ç’6÷’'WGFöâw2FF×W&Â—26÷–VBöâ6Æ–6²à¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚v6Æ–6²rÆgVæ7F–öâ†R—°¢6öç7B6V7W&TW‡æCÖRçF&vWBæ6Æ÷6W7B‚rç6V7W&VÖF6†W‡æBr“°¢–b‡6V7W&TW‡æB—·FövvÆU6V7W&TÖF6†W2‡6V7W&TW‡æB“·&WGW&ã·Ð¢6öç7Bf—‡GW&T6†ææVÅF—FÆSÖRçF&vWBæ6Æ÷6W7B‚ræf—‡GW&V6†ææVÇF—FÆU¶FF×6–EÒr“°¢–b†f—‡GW&T6†ææVÅF—FÆR—·Æ”'&÷w6W"†f—‡GW&T6†ææVÅF—FÆRævWDGG&–'WFR‚vFF×6–Br’Æf—‡GW&T6†ææVÅF—FÆRævWDGG&–'WFR‚vFFÖæÖRr’“·&WGW&ã·Ð¢6öç7BF–ÖVÆ–æUFVÔf—‡GW&SÖRçF&vWBæ6Æ÷6W7B‚rçFVÖf—‡GW&U¶FF×&öf–ÆRÖf—‡GW&SÒ#%Òr“°¢–b‡F–ÖVÆ–æUFVÔf—‡GW&R—·6†÷uFV×2‡F–ÖVÆ–æUFVÔf—‡GW&R“·&WGW&ã·Ð¢6öç7BFVÔf—‡GW&SÖRçF&vWBæ6Æ÷6W7B‚rçFVÖf—‡GW&U¶FFÖf—‡GW&RÖ6&CÒ#%Òr“°¢–b‡FVÔf—‡GW&RbbRçF&vWBæ6Æ÷6W7B‚ræ'FçÆ’Âæ'FçfÆ2Âæ&6†VBr’—¶6öç7BFWF–Ç3×FVÔf—‡GW&RçVW'•6VÆV7F÷"‚rçFVÖf—‡GW&V'&öF67G2r’Æ÷Væ–æsÖFWF–Ç2bfFWF–Ç2æ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr“¶Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚r7FV×5f–WrçFVÖf—‡GW&Rç6VÆV7FVFf—‡GW&Rr’æf÷$V6‚†6&CÓæ6&Bæ6Æ74Æ—7Bç&VÖ÷fR‚w6VÆV7FVFf—‡GW&Rr’“·FVÔf—‡GW&Ræ6Æ74Æ—7BçFövvÆR‚w6VÆV7FVFf—‡GW&RrÂ÷Væ–ær“¶–b†FWF–Ç2–FWF–Ç2æ6Æ74Æ—7BçFövvÆR‚v†–FRr“¶–b†÷Væ–ær–ÆöE7F÷&VDf—‡GW&T6†ææVÇ2‡FVÔf—‡GW&R“·&WGW&ã·Ð¢6öç7BFVÕ&VÖ÷fSÖRçF&vWBæ6Æ÷6W7B‚rçFV×&VÖ÷fRr“°¢–b‡FVÕ&VÖ÷fR—·&VÖ÷fUFVÔff÷&—FR‡FVÕ&VÖ÷fRævWDGG&–'WFR‚vFF×FVÒÖæÖRr’“·&WGW&ã·Ð¢6öç7BFVÔfcÖRçF&vWBæ6Æ÷6W7B‚rçFVÖff—FVÕ¶FF×FVÒ×6V&6…Òr“°¢–b‡FVÔfb—·6VÆV7D×•FVÒ‡FVÔfbævWDGG&–'WFR‚vFF×FVÒ×6V&6‚r—ÇÂrrÇFVÔfbævWDGG&–'WFR‚vFF×FVÒÖ–Br—ÇÂrrÇFVÔfbævWDGG&–'WFR‚vFF×FVÒÖÆövòr—ÇÂrr“·&WGW&ã·Ð¢6öç7BFVÕ7F#ÖRçF&vWBæ6Æ÷6W7B‚rçFV×7F"r“°¢–b‡FVÕ7F"—·FövvÆUFVÔff÷&—FR‡FVÕ7F"ævWDGG&–'WFR‚vFF×FVÒÖæÖRr’ÇFVÕ7F"ÇFVÕ7F"ævWDGG&–'WFR‚vFF×FVÒÖ–Br’“·&WGW&ã·Ð¢6öç7BFVÔf–æDf—‡GW&W3ÖRçF&vWBæ6Æ÷6W7B‚rçFVÖf–æFf—‡GW&W5¶FF×FVÒÖf—‡GW&W5Òr“°¢–b‡FVÔf–æDf—‡GW&W2—¶f–æE7÷'G4f—‡GW&W2‡FVÔf–æDf—‡GW&W2ævWDGG&–'WFR‚vFF×FVÒÖf—‡GW&W2r—ÇÂrrÇFVÔf–æDf—‡GW&W2ævWDGG&–'WFR‚vFF×FVÒÖ–Br—ÇÂrr“·&WGW&ã·Ð¢6öç7BFVÕ6V&6„†—CÖRçF&vWBæ6Æ÷6W7B‚rçFV×6V&6††—E¶FF×FVÒ×6VÆV7EÒr“°¢–b‡FVÕ6V&6„†—B—·6VÆV7D×•FVÒ‡FVÕ6V&6„†—BævWDGG&–'WFR‚vFF×FVÒ×6VÆV7Br—ÇÂrrÇFVÕ6V&6„†—BævWDGG&–'WFR‚vFF×FVÒÖ–Br—ÇÂrrÂrr“·&WGW&ã·Ð¢6öç7B6÷W&6TW‡æCÖRçF&vWBæ6Æ÷6W7B‚ræÆFW7G6÷W&6VW‡æBr“°¢–b‡6÷W&6TW‡æB—¶6öç7B&÷ƒ×6÷W&6TW‡æBç&VçDVÆVÖVçBçVW'•6VÆV7F÷"‚ræÆFW7G6÷W&6W2r“¶–b†&÷‚–&÷‚æ6Æ74Æ—7BçFövvÆR‚v†–FRr“·&WGW&ã·Ð¢6öç7BF–ÖVÆ–æTvÖSÖRçF&vWBæ6Æ÷6W7B‚ræ×–Æ—7GF–ÖVÆ–æVvÖRr“°¢–b‡F–ÖVÆ–æTvÖR—¶6öç7BW&Ã×F–ÖVÆ–æTvÖRævWDGG&–'WFR‚vFF×W&Âr“¶–b‡W&Â—v–æF÷ræ÷Vâ‡W&ÂÂuö&Ææ²rÂvæö÷VæW"r“·&WGW&ã·Ð¢6öç7BF–ÖVÆ–æTcÖRçF&vWBæ6Æ÷6W7B‚ræ×–Æ—7GF–ÖVÆ–æVcr“°¢–b‡F–ÖVÆ–æTc—·6†÷u&6–ær‡F–ÖVÆ–æTcævWDGG&–'WFR‚vFFÖG&—fW"Ö¶W’r—ÇÂrr“·&WGW&ã·Ð¢6öç7B&6–æt6†ãÖRçF&vWBæ6Æ÷6W7B‚rç&6–ævWfVçF6†ææVÂr“°¢–b‡&6–æt6†âbbRçF&vWBæ6Æ÷6W7B‚ræ'FçÆ’Âæ'FçfÆ2Âæ6÷’r’—°¢6öç7B76–C×&6–æt6†âævWDGG&–'WFR‚vFF×6–Br—ÇÂrs°¢–b†76–B—·Æ”'&÷w6W"†76–BÇ&6–æt6†âævWDGG&–'WFR‚vFFÖæÖRr—ÇÂrr“·&WGW&ã·Ð¢Ð¢6öç7B&6–ætWfVçCÖRçF&vWBæ6Æ÷6W7B‚rç&6–ævWfVçBr“°¢–b‡&6–ætWfVçBbbRçF&vWBæ6Æ÷6W7B‚ræ'FçÆ’Âæ'FçfÆ2Âæ&6†VBÂç&6–ævWfVçG6÷W&6RÂç&6–ævWfVçF6†ææVÂr’—¶–b‡&6–ætWfVçBæ6Æ74Æ—7Bæ6öçF–ç2‚vÆöF–æv6†ææVÇ2r’—&WGW&ã¶–b‡&6–ætWfVçBæ6Æ74Æ—7Bæ6öçF–ç2‚v†66†ææVÇ2r’—¶6öç7B&÷ƒ×&6–ætWfVçBçVW'•6VÆV7F÷"‚rç&6–ævWfVçF6†ææVÇ2r“¶–b†&÷‚–&÷‚æ6Æ74Æ—7BçFövvÆR‚v†–FRr“·×&WGW&ã·Ð¢6öç7BÆWcÖRçF&vWBæ6Æ÷6W7B‚ræÆFW7FW—6öFWfÆ2r“°¢–b†ÆWb—·Æ”ÆFW7DW—6öFR†ÆWbævWDGG&–'WFR‚vFFÖ–Br’ÆÆWbævWDGG&–'WFR‚vFFÖW‡Br’ÆÆWb“·&WGW&ã·Ð¢6öç7B×”Æ—7E6†÷sÖRçF&vWBæ6Æ÷6W7B‚ræ×–Æ—7G6†÷v6&Br“°¢–b†×”Æ—7E6†÷r—¶6öç7B6–CÖ×”Æ—7E6†÷rævWDGG&–'WFR‚vFF×6W&–W2r—ÇÂrrÆ6–CÖ×”Æ—7E6†÷rævWDGG&–'WFR‚vFFÖ6FÆörr—ÇÂrs¶–b‡6–B—·6†÷u6†÷w2‚“¶ÆöE6†÷r‡6–B“·&WGW&ã·Ö–b†6–B—·6†÷u6†÷w2‚“¶ÆöDW‡FW&æÅ6†÷r†6–B“·&WGW&ã·×Ð¢6öç7BÆFW7E6†÷sÖRçF&vWBæ6Æ÷6W7B‚ræÆFW7G6†÷v6&Br“°¢–b†ÆFW7E6†÷r—¶6öç7B6–CÖÆFW7E6†÷rævWDGG&–'WFR‚vFF×6W&–W2r—ÇÂrrÆ6–CÖÆFW7E6†÷rævWDGG&–'WFR‚vFFÖ6FÆörr—ÇÂrs¶–b‡6–B—¶ÆöE6†÷r‡6–B“·&WGW&ã·Ö–b†6–B—¶ÆöDW‡FW&æÅ6†÷r†6–B“·&WGW&ã·×Ð¢6öç7B73ÖRçF&vWBæ6Æ÷6W7B‚rç6†÷w7F"r“°¢–b‡72—·FövvÆU6†÷tff÷&—FR‡¶6FÆöuö–C§72ævWDGG&–'WFR‚vFFÖ6FÆörr’Ç6†÷uö¶W“§72ævWDGG&–'WFR‚vFF×6†÷rÖ¶W’r—ÇÇ72ævWDGG&–'WFR‚vFFÖ¶W’r’Ç6W&–W5ö–C§72ævWDGG&–'WFR‚vFF×6W&–W2r—ÇÆçVÆÂÇ6W&–W5ö–G3¢‡72ævWDGG&–'WFR‚vFF×6W&–W2Ö–G2r—ÇÇ72ævWDGG&–'WFR‚vFF×6W&–W2r—ÇÂrr’ç7Æ—B‚rÂr’æf–ÇFW"„&ööÆVâ’ÆæÖS§72ævWDGG&–'WFR‚vFFÖæÖRr’Æ6÷fW#§72ævWDGG&–'WFR‚vFFÖ6÷fW"r’Ç–V#§72ævWDGG&–'WFR‚vFF×–V"r’Ç&F–æs§72ævWDGG&–'WFR‚vFF×&F–ærr—ÒÇ72“·&WGW&ã·Ð¢6öç7B7#ÖRçF&vWBæ6Æ÷6W7B‚rç6†÷w&VÖ÷fRr“°¢–b‡7"—·&VÖ÷fU6†÷tff÷&—FR‡7"ævWDGG&–'WFR‚vFFÖ¶W’r’“·&WGW&ã·Ð¢6öç7B63ÖRçF&vWBæ6Æ÷6W7B‚rç6†÷v6&Br“°¢–b‡62—¶6öç7B–G3×62ævWDGG&–'WFR‚vFF×6W&–W2r—ÇÂrs¶–b†–G2–ÆöE6†÷r†–G2“¶VÇ6R–b‡62ævWDGG&–'WFR‚vFFÖ6FÆörr’–ÆöDW‡FW&æÅ6†÷r‡62ævWDGG&–'WFR‚vFFÖ6FÆörr’“·&WGW&ã·Ð¢6öç7B6cÖRçF&vWBæ6Æ÷6W7B‚rç6†÷vfbr“°¢–b‡6b—¶6öç7B–G3×6bævWDGG&–'WFR‚vFF×6W&–W2r—ÇÂrs¶–b†–G2–ÆöE6†÷r†–G2“¶VÇ6R–b‡6bævWDGG&–'WFR‚vFFÖ6FÆörr’–ÆöDW‡FW&æÅ6†÷r‡6bævWDGG&–'WFR‚vFFÖ6FÆörr’“·&WGW&ã·Ð¢6öç7BWcÖRçF&vWBæ6Æ÷6W7B‚ræW—6öFWfÆ2r“°¢–b†Wb—·Æ”W—6öFUVWVR†WbævWDGG&–'WFR‚vFF×6V6öâr’ÆWbævWDGG&–'WFR‚vFFÖW—6öFRr’ÆWbævWDGG&–'WFR‚vFF×6÷W&6Rr’ÆWb“·&WGW&ã·Ð¢6öç7B×cÖRçF&vWBæ6Æ÷6W7B‚ræÖ÷f–WfÆ2r“°¢–b†×b—·Æ”Ö÷f–UdÄ2†×bævWDGG&–'WFR‚vFF×6–Br’Æ×bævWDGG&–'WFR‚vFFÖW‡Br’Æ×b“·&WGW&ã·Ð¢6öç7B×3ÖRçF&vWBæ6Æ÷6W7B‚ræÖ÷f–W7F"r“°¢–b†×2—·FövvÆTÖ÷f–Tff÷&—FR‡¶6FÆöuö–C¦×2ævWDGG&–'WFR‚vFFÖ6FÆörr’Ç7G&VÕö–C¦×2ævWDGG&–'WFR‚vFF×6–Br’ÆæÖS¦×2ævWDGG&–'WFR‚vFFÖæÖRr’ÆW‡FVç6–öã¦×2ævWDGG&–'WFR‚vFFÖW‡Br’Ç–V#¦×2ævWDGG&–'WFR‚vFF×–V"r’Ç&F–æs¦×2ævWDGG&–'WFR‚vFF×&F–ærr’Æ6÷fW#¦×2ævWDGG&–'WFR‚vFFÖ6÷fW"r—ÒÆ×2“·&WGW&ã·Ð¢6öç7B&V6VçDÖ÷f–SÖRçF&vWBæ6Æ÷6W7B‚rç&V6VçFÖ÷f–Rr“°¢–b‡&V6VçDÖ÷f–R—¶Fö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–Ur’çfÇVS×&V6VçDÖ÷f–RævWDGG&–'WFR‚vFF×VW'’r—ÇÂrs·6V&6„Ö÷f–W2‚“·&WGW&ã·Ð¢6öç7B×#ÖRçF&vWBæ6Æ÷6W7B‚ræÖ÷f–W&VÖ÷fRr“°¢–b†×"—·&VÖ÷fTÖ÷f–Tff÷&—FR†×"ævWDGG&–'WFR‚vFFÖ¶W’r’“·&WGW&ã·Ð¢6öç7Bff÷&—FTÖ÷f–SÖRçF&vWBæ6Æ÷6W7B‚ræÖ÷f–Vfbr“°¢–b†ff÷&—FTÖ÷f–R—¶Fö7VÖVçBævWDVÆVÖVçD'”–B‚vÖ÷f–Ur’çfÇVSÖff÷&—FTÖ÷f–RævWDGG&–'WFR‚vFF×VW'’r—ÇÂrs·6V&6„Ö÷f–W2‚“·&WGW&ã·Ð¢6öç7BGCÖRçF&vWBæ6Æ÷6W7B‚rçFV×F"r“°¢–b‡GB—µö7F—fUFVÓ×'6T–çB‡GBævWDGG&–'WFR‚vFF×FVÒr’Ã—ÇÃ·&VæFW%FVÕ7v—F6‚‚“·&VæFW$7F—fUFVÒ‚“·&WGW&ã·Ð¢6öç7B&ƒÖRçF&vWBæ6Æ÷6W7B‚ræ&6†VBr“°¢–b†&‚—°¢6öç7B&÷sÖ&‚ç&VçDVÆVÖVçC°¢6öç7B&÷ƒ×&÷rçVW'•6VÆV7F÷"‚ræ&66†ç2r“°¢6öç7B÷Væ–æsÖ&÷‚bf&÷‚æ6Æ74Æ—7Bæ6öçF–ç2‚v†–FRr“°¢–b†&÷‚–&÷‚æ6Æ74Æ—7BçFövvÆR‚v†–FRrÂ÷Væ–ær“°¢&÷ræ6Æ74Æ—7BçFövvÆR‚v÷VârÂ÷Væ–ær“°¢&WGW&ã°¢Ð¢6öç7B7&3ÖRçF&vWBæ6Æ÷6W7B‚rçGg7&2r“°¢–b‡7&2—¶ÆöEGe6÷W&6R‡7&2ævWDGG&–'WFR‚vFF×7&2r’“·&WGW&ã·Ð¢6öç7BGgcÖRçF&vWBæ6Æ÷6W7B‚rçGgfÆ2r“°¢–b‡Ggb—·Æ•dÄ2‡GgbævWDGG&–'WFR‚vFF×6–Br’ÇGgb“·&WGW&ã·Ð¢–b†RçF&vWBæ6Æ÷6W7B‚rçGfG&rr’—&WGW&ã°¢6öç7B7CÖRçF&vWBæ6Æ÷6W7B‚ræfg7F"r“°¢–b‡7B—°¢–b‡7Bæ†4GG&–'WFR‚vFFÖff6Br’—·FövvÆTfd6B‡7BævWDGG&–'WFR‚vFFÖff6Br’Ç7B“·&WGW&ã·Ð¢FövvÆTfd6†ææVÂ‡7BævWDGG&–'WFR‚vFF×6–Br’Ç7BævWDGG&–'WFR‚vFFÖæÖRr’Ç7BævWDGG&–'WFR‚vFFÖ6Br’Ç7B“·&WGW&ã°¢Ð¢6öç7BGf3ÖRçF&vWBæ6Æ÷6W7B‚rçGf6†âr“°¢–b‡Gf2—¶6öç7B3Õ÷Gd6†ææVÇ2æf–æB†gVæ7F–öâ‡‚—·&WGW&â7G&–ær‡‚ç7G&VÕö–B“ÓÓ×Gf2ævWDGG&–'WFR‚vFF×6–Br“·Ò“¶–b†2—GeÆ’†2ç7G&VÕö–BÆ2ææÖR“·&WGW&ã·Ð¢6öç7B&ÓÖRçF&vWBæ6Æ÷6W7B‚ræfg&Òr“°¢–b‡&Ò—¶–b‡&Òæ†4GG&–'WFR‚vFFÖ6Br’—&VÖ÷fTfd6B‡&ÒævWDGG&–'WFR‚vFFÖ6Br’“¶VÇ6R&VÖ÷fTfd6†â‡&ÒævWDGG&–'WFR‚vFF×6–Br’“·&WGW&ã·Ð¢6öç7B#ÖRçF&vWBæ6Æ÷6W7B‚ræ'FçÆ’r“°¢–b‡"—·Æ”'&÷w6W"‡"ævWDGG&–'WFR‚vFF×6–Br’Ç"ævWDGG&–'WFR‚vFFÖæÖRr’“·&WGW&ã·Ð¢6öç7Bf#ÖRçF&vWBæ6Æ÷6W7B‚ræ'FçfÆ2r“°¢–b‡f"—·Æ•dÄ2‡f"ævWDGG&–'WFR‚vFF×6–Br’Çf"“·&WGW&ã·Ð¢6öç7B×–6ƒÖRçF&vWBæ6Æ÷6W7B‚ræ×–F6†6†ææVÅ¶FF×6–EÒr“°¢–b†×–6‚—·Æ”'&÷w6W"†×–6‚ævWDGG&–'WFR‚vFF×6–Br’Æ×–6‚ævWDGG&–'WFR‚vFFÖæÖRr’“·&WGW&ã·Ð¢6öç7B#ÖRçF&vWBæ6Æ÷6W7B‚ræ6÷’r“°¢–b‚"—&WGW&ã°¢6öç7BSÖ"ævWDGG&–'WFR‚vFF×W&Âr—ÇÂrs°¢æf–vF÷"æ6Æ—&ö&Bçw&—FUFW‡B‡R’çF†Vâ‚‚“Óç¶"çFW‡D6öçFVçCÒt6÷–VBs·6WEF–ÖV÷WB‚‚“Óæ"çFW‡D6öçFVçC×G"‚t6÷’U$Âr’Ã#“·Ò¢æ6F6‚‚‚“Óç¶"çFW‡D6öçFVçCÒt6÷’f–ÆVBs·6WEF–ÖV÷WB‚‚“Óæ"çFW‡D6öçFVçC×G"‚t6÷’U$Âr’ÃS“·Ò“°§Ò“°¢òòÇ’6fVBÆæwVvP§G'—¶6öç7B6ÃÖÆö6Å7F÷&vRævWD—FVÒ‚wGfÖFUöÆærr“¶–b‡6ÃÓÓÒvæòr—6WDÆær‚væòr“¶VÇ6RÇ”Æær‚“·Ö6F6‚†R—¶Ç”Æær‚“·Ð¢òò÷VâF†RW6W"w2FVfVÇB7F'B6V7F–öà¢†7–æ2gVæ7F–öâ‚—°¢ÆWB7F'CÒv×–Æ—7BrÆ6†V6µ6†÷w3ÖfÇ6RÇ&Vg&W6„—GcÖfÇ6RÇ&Vg&W6…7÷'G3ÖfÇ6RÇ7F'GW6öæf–sÖçVÆÃ°¢G'—¶6öç7B3Öv—B’‚rö’ö6öæf–rr“·7F'GW6öæf–sÖ3·7F'CÖ2ç7F'E÷6V7F–öçÇÂv×–Æ—7Bs¶6†V6µ6†÷w3Ò2æ6†V6µ÷6†÷w5ööå÷7F'GW·&Vg&W6„—GcÒ2ç&Vg&W6…ö—Geööå÷7F'GW·&Vg&W6…7÷'G3Ò2ç&Vg&W6…÷7÷'G5ööå÷7F'GW·6WDÆær†2ç&VfW'&VEöÆæwVvWÇÂvVâr“¶Ç•&öf–ÆT6öæf–r†2“¶–b‡7F'CÓÓÒwFV×2rbböfö÷F&ÆÄVæ&ÆVB—7F'CÒv×–Æ—7Bs¶–b‡7F'CÓÓÒvvÖW2rbbövÖW4Væ&ÆVB—7F'CÒv×–Æ—7Bs¶–b‡7F'CÓÓÒw&6–ærrbböcVæ&ÆVB—7F'CÒv×–Æ—7Bs·Ö6F6‚†R—·Ð¢–b‡7F'CÓÓÒw6V&6‚r—7F'CÒv6†ææVÇ2s²òòÖ–w&FRF†R&VÖ÷fVB6V&6‚6V7F–öà¢–b‡7F'CÓÓÒv×—F–ÖVÆ–æRrbeö×”Æ—7DÆ–÷WCÓÓÒwF–ÖVÆ–æRr—7F'CÒv×–Æ—7Bs°¢6öç7BÖ×¶6†ææVÇ3§6†÷t6†ææVÇ2Æ×—Gc§6†÷t×—GbÆÖ÷f–W3§6†÷tÖ÷f–W2Ç6†÷w3§6†÷u6†÷w2ÆvÖW3§6†÷tvÖW2Ç&6–æs§6†÷u&6–ærÇFV×3§6†÷uFV×2Æ×–Æ—7C§6†÷t×–Æ—7BÆ×—F–ÖVÆ–æS§6†÷t×—F–ÖVÆ–æWÓ°¢†Ö·7F'E×ÇÇ6†÷t×–Æ—7B’‚“°¢†—7F÷'’ç&WÆ6U7FFR‡·GfÖFS§G'VRÇ6V7F–öã§7F'GÒÂrrÂr2r·7F'B“°¢ö†—7F÷'•&VG“×G'VS°¢6öç7B6WGWFöæSÒ‡7F'GW6öæf–rbg7F'GW6öæf–rç6WGWö6ö×ÆWFSÓÓ×G'VR“°¢–b‡7F'GW6öæf–rbb6WGWFöæR—6WEF–ÖV÷WB‚‚“Óæ÷Vå&öf–ÆU6WGW‡G'VRÇ7F'GW6öæf–r’Ã#“°¢–b‡7F'GW6öæf–rbg6WGWFöæR—6WEF–ÖV÷WB‚‚“ÓæÖ–&TWFõ&Vg&W6…7FVÕv—6†Æ—7B‡7F'GW6öæf–r’Ã““°¢–b‡6WGWFöæRbb‡&Vg&W6„—GgÇÇ&Vg&W6…7÷'G7ÇÆ6†V6µ6†÷w2’—6WEF–ÖV÷WB†7–æ2gVæ7F–öâ‚—°¢–b‡&Vg&W6„—GgÇÇ&Vg&W6…7÷'G2–v—B&Vg&W6„öå7F'GW‡&Vg&W6„—GbÇ&Vg&W6…7÷'G2“°¢–b†6†V6µ6†÷w2bb&Vg&W6„—Gb–v—B6†V6µ6†÷w4öå7F'GW‚“°¢ÒÃS“°§Ò’‚“°§v–æF÷ræFDWfVçDÆ—7FVæW"‚w÷7FFRrÆgVæ7F–öâ†Wb—°¢6öç7B7FFSÖWbç7FFS°¢–b‚7FFWÇÂ7FFRçGfÖFR—&WGW&ã°¢6öç7BÖ×·6V&6ƒ§6†÷t6†ææVÇ2Æ6†ææVÇ3§6†÷t6†ææVÇ2Æ×—Gc§6†÷t×—GbÆÖ÷f–W3§6†÷tÖ÷f–W2Ç6†÷w3§6†÷u6†÷w2ÆvÖW3§6†÷tvÖW2Ç&6–æs§6†÷u&6–ærÇFV×3§6†÷uFV×2Æ×–Æ—7C§6†÷t×–Æ—7BÆ×—F–ÖVÆ–æS§6†÷t×—F–ÖVÆ–æRÇ6WGF–æw3§6†÷u6WGF–æw7Ó°¢6öç7BfãÖÖ·7FFRç6V7F–öå×ÇÇ6†÷t×–Æ—7C°¢ö†—7F÷'•&W7F÷&–æs×G'VS°¢G'—°¢fâ‚“°¢–b‡7FFRç6V7F–öãÓÓÒw6†÷w2rbg7FFRç6W&–W4–B–ÆöE6†÷r‡7FFRç6W&–W4–BÇG'VR“°¢VÇ6R–b‡7FFRç6V7F–öãÓÓÒw6†÷w2rbg7FFRæ6FÆöt–B–ÆöDW‡FW&æÅ6†÷r‡7FFRæ6FÆöt–BÇG'VR“°¢Öf–æÆÇ—µö†—7F÷'•&W7F÷&–æsÖfÇ6S·Ð§Ò“°§&Vg&W6…7FGW2‚“°¢òòÒÒÒWFò×WFFRÒÒÐ¦ÆWB÷WFFTÆFW7CÖçVÆÂÅ÷WFFU&öÆÆ&6µf—6–&ÆSÖfÇ6S°¦7–æ2gVæ7F–öâ÷Vä6öæf–tföÆFW"‚—°¢G'—°¢6öç7B£Öv—B’‚rö’ö÷VåöföÆFW"rÇ¶ÖWF†öC¢uõ5BwÒ“°¢–b‚¢æö²—Fö7B‡G"‚t6÷VÆBæ÷B÷VâföÆFW"âr’²†¢çFƒò‚rr¶¢çF‚“¢rr’“°¢Ö6F6‚†R—·Fö7B‡G"‚t6÷VÆBæ÷B÷VâföÆFW"âr’“·Ð§Ð¦gVæ7F–öâ&öf–ÆUF–ÖVÆ–æT&6·W‚—°¢ÆWB6WGF–æw3×·Ó·G'—·6WGF–æw3Ô¥4ôâç'6R†Æö6Å7F÷&vRævWD—FVÒ‚wGfÖFUF–ÖVÆ–æU6WGF–æw2r—ÇÂw·Òr—ÇÇ·Ó·Ö6F6‚†R—·Ð¢&WGW&â¶f–ÇFW#¦Æö6Å7F÷&vRævWD—FVÒ‚wGfÖFUF–ÖVÆ–æTf–ÇFW"r—ÇÂvÆÂrÇ6WGF–æw3§6WGF–æw7Ó°§Ð¦7–æ2gVæ7F–öâW‡÷'E&öf–ÆT&6·W†gVÆÂ—°¢–b†gVÆÂbb6öæf—&Ò‚tgVÆÂ&6·W–æ6ÇVFW2–÷W"‡G&VÒÆöv–ââF÷væÆöBæB7F÷&R—B6V7W&VÇ“òr’—&WGW&ã°¢6öç7B×6sÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&öf–ÆT&6·W×6rr“¶–b†×6r–×6rçFW‡D6öçFVçC×G"‚u&W&–ær&6·Wâââr“°¢G'—°¢6öç7B&6·WÖv—B’‚rö’÷&öf–ÆUö&6·WöW‡÷'BrÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡·G—S¦gVÆÃòvgVÆÂs¢w&öf–ÆRrÇF–ÖVÆ–æS§&öf–ÆUF–ÖVÆ–æT&6·W‚—Ò—Ò“°¢–b†&6·WæW'&÷"—F‡&÷ræWrW'&÷"†&6·WæW'&÷"“°¢6öç7B6fSÕ7G&–ær‚†&6·Wæ6öæf–rbf&6·Wæ6öæf–rç&öf–ÆUöæÖR—ÇÂw&öf–ÆRr’ç&WÆ6R‚õµæ×£Ó•òÕÒ²öv’ÂrÒr’ç&WÆ6R‚õâÒ·ÂÒ²BörÂrr—ÇÂw&öf–ÆRs°¢6öç7B&Æö#ÖæWr&Æö"…´¥4ôâç7G&–æv–g’†&6·WÆçVÆÂÃ"•ÒÇ·G—S¢vÆ–6F–öâö§6öâwÒ’ÆÆ–æ³ÖFö7VÖVçBæ7&VFTVÆVÖVçB‚vr“°¢Æ–æ²æ‡&VcÕU$Âæ7&VFTö&¦V7EU$Â†&Æö"“¶Æ–æ²æF÷væÆöCÒuEdÖFRÒr·6fR²rÒr²†gVÆÃòvgVÆÂs¢w&öf–ÆRr’²rÖ&6·Wæ§6öâs¶Fö7VÖVçBæ&öG’æVæD6†–ÆB†Æ–æ²“¶Æ–æ²æ6Æ–6²‚“¶Æ–æ²ç&VÖ÷fR‚“·6WEF–ÖV÷WB‚‚“ÓåU$Âç&Wfö¶Tö&¦V7EU$Â†Æ–æ²æ‡&Vb’Ã“°¢–b†×6r–×6rçFW‡D6öçFVçC×G"‚t&6·WF÷væÆöFVBâr“°¢Ö6F6‚†R—¶–b†×6r–×6rçFW‡D6öçFVçCÕ7G&–ær†RæÖW76vWÇÆR“·Ð§Ð¦7–æ2gVæ7F–öâ–×÷'E&öf–ÆT&6·W†–çWB—°¢6öç7B×6sÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w&öf–ÆT&6·W×6rr’Æf–ÆSÖ–çWBæf–ÆW2bf–çWBæf–ÆW5³Ó¶–b‚f–ÆR—&WGW&ã°¢G'—°¢–b†f–ÆRç6—¦SãR£#B£#B—F‡&÷ræWrW'&÷"‚t&6·Wf–ÆR—2FöòÆ&vRâr“°¢6öç7B&6·WÔ¥4ôâç'6R†v—Bf–ÆRçFW‡B‚’“°¢–b†&6·Wæf÷&ÖBÓÒvöÆ÷2×GfÖFRÖ&6·Wr—F‡&÷ræWrW'&÷"‚uF†—2—2æ÷BEdÖFR&6·Wf–ÆRâr“°¢6öç7B6÷VçG3Ö&6·Wæff÷&—FW7ÇÇ·ÒÇ7VÖÖ'“Õ²w6†÷w2rÂvÖ÷f–W2rÂvvÖW2rÂwFV×2rÂv6†ææVÇ2uÒæÖ†³Óâ„'&’æ—4'&’†6÷VçG5¶µÒ“ö6÷VçG5¶µÒæÆVæwFƒ£’²rr¶²’æ¦ö–â‚rÂr“°¢6öç7BgVÆÃÖ&6·Wæ&6·W÷G—SÓÓÒvgVÆÂs°¢6öç7Bv&æ–æsÒ†gVÆÃòuF†—2gVÆÂ&6·W6â&WÆ6RF†R7W'&VçB‡G&VÒÆöv–âåÅÆåÅÆâs¢rr’²tÖW&vR&6·W–çFòF†—2&öf–ÆSõÅÆâr·7VÖÖ'“°¢–b‚6öæf—&Ò‡v&æ–ær’—&WGW&ã°¢6öç7B&W7VÇCÖv—B’‚rö’÷&öf–ÆUö&6·Wö–×÷'BrÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡¶&6·W¦&6·WÒ—Ò“°¢–b‡&W7VÇBæW'&÷"—F‡&÷ræWrW'&÷"‡&W7VÇBæW'&÷"“°¢6öç7BF–ÖVÆ–æS×&W7VÇBçF–ÖVÆ–æWÇÇ·Ó°¢–b‡&W7VÇBçG—SÓÓÒvgVÆÂr—°¢–b„ö&¦V7Bç&÷F÷G—Ræ†4÷vå&÷W'G’æ6ÆÂ‡F–ÖVÆ–æRÂvf–ÇFW"r’–Æö6Å7F÷&vRç6WD—FVÒ‚wGfÖFUF–ÖVÆ–æTf–ÇFW"rÅ7G&–ær‡F–ÖVÆ–æRæf–ÇFW'ÇÂvÆÂr’“¶VÇ6RÆö6Å7F÷&vRç&VÖ÷fT—FVÒ‚wGfÖFUF–ÖVÆ–æTf–ÇFW"r“°¢–b„ö&¦V7Bç&÷F÷G—Ræ†4÷vå&÷W'G’æ6ÆÂ‡F–ÖVÆ–æRÂw6WGF–æw2r’–Æö6Å7F÷&vRç6WD—FVÒ‚wGfÖFUF–ÖVÆ–æU6WGF–æw2rÄ¥4ôâç7G&–æv–g’‡F–ÖVÆ–æRç6WGF–æw7ÇÇ·Ò’“¶VÇ6RÆö6Å7F÷&vRç&VÖ÷fT—FVÒ‚wGfÖFUF–ÖVÆ–æU6WGF–æw2r“°¢–b†×6r–×6rçFW‡D6öçFVçC×G"‚tgVÆÂ&6·W&W7F÷&VBâr“·Fö7B‡G"‚tgVÆÂ&6·W&W7F÷&VBâr’“¶Æö6F–öâç&VÆöB‚“·&WGW&ã°¢Ð¢–b‡F–ÖVÆ–æRæf–ÇFW"–Æö6Å7F÷&vRç6WD—FVÒ‚wGfÖFUF–ÖVÆ–æTf–ÇFW"rÇF–ÖVÆ–æRæf–ÇFW"“¶–b‡F–ÖVÆ–æRç6WGF–æw2–Æö6Å7F÷&vRç6WD—FVÒ‚wGfÖFUF–ÖVÆ–æU6WGF–æw2rÄ¥4ôâç7G&–æv–g’‡F–ÖVÆ–æRç6WGF–æw2’“°¢ö×•F–ÖVÆ–æU&Vg4ÆöFVCÖfÇ6Sµö6G4ÆöFVCÖfÇ6SµöÆFW7DW—6öFW4ÆöFVCÖfÇ6Sµö×”Æ—7DÆöFVCÖfÇ6S¶v—BÆöE6WGF–æw2‚“¶v—BÆöDff÷&—FW2‚“·&Vg&W6…7FGW2‚“°¢–b†×6r–×6rçFW‡D6öçFVçC×G"‚t&6·W–×÷'FVBæBÖW&vVBâr“·Fö7B‡G"‚t&6·W–×÷'FVBæBÖW&vVBâr’“°¢Ö6F6‚†R—¶–b†×6r–×6rçFW‡D6öçFVçC×G"‚t6÷VÆBæ÷B–×÷'BF†—2&6·Wâr’²rrµ7G&–ær†RæÖW76vWÇÆR“·Ð¢f–æÆÇ—¶–çWBçfÇVSÒrs·Ð§Ð¦gVæ7F–öâö†VÇF„vò‡G2Ææ÷r—°¢–b‚G2—&WGW&âG"‚væ÷B6†V6¶VB–WBr“°¢6öç7B3ÔÖF‚æÖ‚ƒÄÖF‚æfÆö÷"‚†æ÷r×G2’’“°¢–b‡3Ã“—&WGW&âG"‚v§W7Bæ÷rr“°¢6öç7BÓÔÖF‚æfÆö÷"‡2óc“°¢–b†ÓÃ“—&WGW&âÒ²rr·G"‚vÖ–âvòr“°¢6öç7BƒÔÖF‚æfÆö÷"†Òóc“°¢–b†ƒÃC‚—&WGW&â‚²rr·G"‚v‚vòr“°¢&WGW&âÖF‚æfÆö÷"†‚ó#B’²rr·G"‚vBvòr“°§Ð¦gVæ7F–öâ&VæFW%6÷W&6T†VÇF‚†FF—°¢6öç7BVÃÖFö7VÖVçBævWDVÆVÖVçD'”–B‚w6÷W&6T†VÇF‚r“°¢–b‚VÂ—&WGW&ã°¢6öç7Bæ÷sÖFFææ÷wÇÂ„FFRææ÷r‚’ó“°¢ÆWBƒÒrs°¢†FFç6÷W&6W7ÇÅµÒ’æf÷$V6‚†gVæ7F–öâ‡2—°¢ÆWBF÷CÒvF÷B×Væ¶æ÷vârÆÆ&VÃ×G"‚væ÷B6†V6¶VB–WBr“°¢6öç7B7VVC×2æÆFVæ7•ö×2ÖçVÆÃò‚rÇS#rr²‡2æÆFVæ7•ö×3ãÓò‡2æÆFVæ7•ö×2ó’çFôf—†VBƒ’²w2s§2æÆFVæ7•ö×2²v×2r’“¢rs°¢–b‡2æö³ÓÓ×G'VR—¶F÷CÒvF÷BÖö²s¶Æ&VÃ×G"‚wv÷&¶–ærr’²‡2æ6÷VçBÖçVÆÃò‚rÇS#rr·2æ6÷VçB²rr·G"‚v—FV×2r’“¢rr’·7VVB²rÇS#rrµö†VÇF„vò‡2çG2Ææ÷r“·Ð¢VÇ6R–b‡2æö³ÓÓÖfÇ6R—¶F÷CÒvF÷BÖ&Bs¶Æ&VÃÒ‡2æW'&÷#÷2æW'&÷#§G"‚vf–ÆVBr’’·7VVB²rÇS#rrµö†VÇF„vò‡2çG2Ææ÷r“·Ð¢VÇ6R–b‡2æW'&÷"—¶Æ&VÃ×G"‡2æW'&÷"“·Ð¢‚³ÒsÆF—b6Æ73Ò'7&7&÷r#ãÇ7â6Æ73Ò'7&6F÷Br¶F÷B²r#ãÂ÷7ããÇ7â6Æ73Ò'7&6æÖR#âr¶W62‡2æÆ&VÂ’²sÂ÷7ããÇ7â6Æ73Ò'7&77FB×WFVB#âr¶W62†Æ&VÂ’²sÂ÷7ããÂöF—câs°¢Ò“°¢VÂæ–ææW$…DÔÃÖ‡ÇÂ‚sÇ7â6Æ73Ò&×WFVB#âr·G"‚tæò6÷W&6W2âr’²sÂ÷7ãâr“°§Ð¦7–æ2gVæ7F–öâÆöE6÷W&6T†VÇF‚‚—°¢G'—¶6öç7B£Öv—B’‚rö’÷6÷W&6Uö†VÇF‚r“·&VæFW%6÷W&6T†VÇF‚†¢“·Ö6F6‚†R—·Ð§Ð¦7–æ2gVæ7F–öâFW7E6÷W&6W2†'Fâ—°¢–b†'Fâ—¶'FâæF—6&ÆVC×G'VS¶'FâçFW‡D6öçFVçC×G"‚uFW7F–ær6÷W&6W2âââr“·Ð¢G'—°¢6öç7Bf—'7CÖv—B’‚rö’÷6÷W&6Uö†VÇF‚r’Æ¶W—3Ò†f—'7Bç6÷W&6W7ÇÅµÒ’æÖ‡3Óç2æ¶W’’ÇF÷FÃÖ¶W—2æÆVæwFƒ¶ÆWBæW‡CÓÆFöæSÓ°¢6öç7Bv÷&¶W#Ö7–æ2gVæ7F–öâ‚—·v†–ÆR†æW‡CÇF÷FÂ—¶6öç7B¶W“Ö¶W—5¶æW‡B²µÓ·G'—¶6öç7B£Öv—B’‚rö’÷FW7E÷6÷W&6RrÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡¶¶W“¦¶W—Ò—Ò“¶–b†¢bf¢ç6÷W&6W2—&VæFW%6÷W&6T†VÇF‚‡·6÷W&6W3¦¢ç6÷W&6W2Ææ÷s¤FFRææ÷r‚’óÒ“·Ö6F6‚†R—·ÖFöæR²³¶–b†'Fâ–'FâçFW‡D6öçFVçC×G"‚uFW7F–ær6÷W&6W2âââr’²rr¶FöæR²ròr·F÷FÃ·×Ó°¢v—B&öÖ—6RæÆÂ…·v÷&¶W"‚’Çv÷&¶W"‚’Çv÷&¶W"‚•Ò“°¢v—BÆöE6÷W&6T†VÇF‚‚“°¢Ö6F6‚†R—·Fö7B‡G"‚t6÷VÆBæ÷BFW7B6÷W&6W2âr’“·Ð¢–b†'Fâ—¶'FâæF—6&ÆVCÖfÇ6S¶'FâçFW‡D6öçFVçC×G"‚uFW7BÆÂ6÷W&6W2r“·Ð§Ð¦7–æ2gVæ7F–öâ6†V6´f÷%WFFR†ÖçVÂ—°¢6öç7B'FãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚v6†V6µWFFT'Fâr“°¢–b†ÖçVÂbf'Fâ—¶'FâçFW‡D6öçFVçC×G"‚t6†V6¶–ærâââr“¶'FâæF—6&ÆVC×G'VS·Ð¢G'—°¢6öç7B£Öv—B’‚rö’÷WFFUö6†V6²r“°¢–b†¢æf–Æ&ÆRbf¢æÆFW7B—°¢÷WFFTÆFW7CÖ¢æÆFW7C°¢6öç7BWFFT'FãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFTæ÷t'Fâr“·WFFT'Fâæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“·WFFT'FâçFW‡D6öçFVçC×G"‚uWFFRæ÷rr“·WFFT'FâæF—6&ÆVCÖfÇ6S·WFFT'Fâæöæ6Æ–6³ÖFõWFFTæ÷s°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFTÆFW$'Fâr’æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçC×G"‚uWFFRf–Æ&ÆRr’²s¢br¶¢æÆFW7B²r‚r·G"‚w–÷R†fRr’²rbr¶¢æ7W'&VçB²r’s°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT&ææW"r’æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢ÖVÇ6R–b†¢ç6¶—VEö&E÷fW'6–öâbf¢ç&V¦V7FVE÷fW'6–öâ—°¢–b†ÖçVÂ—Fö7B‚ufW'6–öâr¶¢ç&V¦V7FVE÷fW'6–öâ²r&Wf–÷W6Ç’f–ÆVB7F'GWæB—2&V–ær6¶—VBâõEdÒv–ÆÂv—Bf÷"æWvW"&VÆV6RârÃs“°¢ÖVÇ6R–b†ÖçVÂ—°¢Fö7B‡G"‚u–÷R&RöâF†RÆFW7BfW'6–öâr’²r‡br²†¢æ7W'&VçGÇÂrr’²r’r“°¢Ð¢Ö6F6‚†R—°¢–b†ÖçVÂ—Fö7B‡G"‚t6÷VÆBæ÷B6†V6²f÷"WFFW2â6†V6²–÷W"–çFW&æWB6öææV7F–öââr’“°¢Ð¢–b†ÖçVÂbf'Fâ—¶'FâçFW‡D6öçFVçC×G"‚t6†V6²f÷"WFFW2r“¶'FâæF—6&ÆVCÖfÇ6S·Ð§Ð¦7–æ2gVæ7F–öâFõWFFTæ÷r‚—°¢6öç7B'FãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFTæ÷t'Fâr“°¢'FâçFW‡D6öçFVçC×G"‚tF÷væÆöF–ærâââr“¶'FâæF—6&ÆVC×G'VS°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçCÒtF÷væÆöF–ærbr²…÷WFFTÆFW7GÇÂrr’²ræBfW&–g––ær—G26†V6·7VÒâõEdÒv–ÆÂ¶VW'Vææ–ærGW&–ærF†—27FWâs°¢G'—°¢6öç7B£Öv—B’‚rö’÷WFFUöF÷væÆöBrÇ¶ÖWF†öC¢uõ5BwÒ“°¢–b‚¢æö²—·F‡&÷ræWrW'&÷"‚vFÂr“·Ð¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçCÒufW'6–öâr²…÷WFFTÆFW7GÇÂrr’²rF÷væÆöFVBæBfW&–f–VBâ&W7F'Bæ÷rFò–ç7FÆÂ—CòF†R7W'&VçBfW'6–öâv–ÆÂ&R&6¶VBWf—'7Bâs°¢'FâçFW‡D6öçFVçC×G"‚u&W7F'Bæ÷rr“¶'FâæF—6&ÆVCÖfÇ6S°¢'Fâæöæ6Æ–6³ÖFõWFFU&W7F'C°¢Ö6F6‚†R—°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçC×G"‚uWFFRf–ÆVBâG'’v–âÆFW"âr“°¢'FâçFW‡D6öçFVçC×G"‚uWFFRæ÷rr“¶'FâæF—6&ÆVCÖfÇ6S°¢Ð§Ð¦7–æ2gVæ7F–öâFõWFFU&W7F'B‚—°¢6öç7B'FãÖFö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFTæ÷t'Fâr“°¢'FâçFW‡D6öçFVçC×G"‚u&W7F'F–ærâââr“¶'FâæF—6&ÆVC×G'VS°¢G'—°¢6öç7B£Öv—B’‚rö’÷WFFU÷&W7F'BrÇ¶ÖWF†öC¢uõ5BwÒ“°¢–b†¢ç&VÆVæ6ƒÓÓÖfÇ6R—°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçC×G"‚uWFFR–ç7FÆÆVBâÆV6R6Æ÷6RF†—2v–æF÷ræB÷VâöÆþ(	—2EdÖFRv–ââr“°¢ÖVÇ6W°¢6öç7BW‡V7FVCÕ7G&–ær†¢æW‡V7FVE÷fW'6–öçÇÅ÷WFFTÆFW7GÇÂrr“°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçCÒt–ç7FÆÆ–ærbr¶W‡V7FVB²râõEdÒ—2FW7F–ærF†RæWrfW'6–öâæBv–ÆÂWFöÖF–6ÆÇ’&W7F÷&RF†R&6·W–b7F'GWf–Ç2âs°¢6öç7B7F'FVCÔFFRææ÷r‚“°¢6öç7Bv—Df÷%&W7F'CÖ7–æ2gVæ7F–öâ‚—°¢G'—°¢6öç7B&W7öç6SÖv—BfWF6‚‚rö’÷–ærrÇ¶66†S¢væò×7F÷&RwÒ“°¢6öç7B–æsÖv—B&W7öç6Ræ§6öâ‚“°¢–b‡–ærbg–æræÓÓÒvöÆ÷2×GfÖFRrbb‚W‡V7FVGÇÅ7G&–ær‡–ærçfW'6–öâ“ÓÓÖW‡V7FVB’—°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçCÒuWFFR7V66W76gVÂâõEdÒbrµ7G&–ær‡–ærçfW'6–öçÇÆW‡V7FVB’²r—2'Vææ–ærâs°¢6WEF–ÖV÷WB‚‚“ÓæÆö6F–öâç&VÆöB‚’Ã“·&WGW&ã°¢Ð¢–b‡–ærbg–æræÓÓÒvöÆ÷2×GfÖFRrbfW‡V7FVBbe7G&–ær‡–ærçfW'6–öâ’ÓÖW‡V7FVBbdFFRææ÷r‚’×7F'FVCãCS—°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçCÒuF†RæWrfW'6–öâF–Bæ÷B7F'B6÷'&V7FÇ’âõEdÒ&W7F÷&VBbrµ7G&–ær‡–ærçfW'6–öçÇÂwF†R&Wf–÷W2fW'6–öâr’²rg&öÒ&6·WæBv–ÆÂ6¶—F†R&BWFFRVçF–ÂæWvW"&VÆV6R—2f–Æ&ÆRâs°¢'Fâæ6Æ74Æ—7BæFB‚v†–FRr“¶Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFTÆFW$'Fâr’æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“·&WGW&ã°¢Ð¢Ö6F6‚†R—·Ð¢–b„FFRææ÷r‚’×7F'FVCÃ“—6WEF–ÖV÷WB‡v—Df÷%&W7F'BÃS“°¢VÇ6RFö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçCÒtõEdÒF–Bæ÷B&WGW&âgFW"F†RWFFR6†V6²â6Æ÷6RæB&V÷VâF†R²F†R&6·W&VÖ–ç2f–Æ&ÆR2GfÖFRç’æ&6·Wâs°¢Ó°¢6WEF–ÖV÷WB‡v—Df÷%&W7F'BÃS“°¢Ð¢Ö6F6‚†R—°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçC×G"‚u&W7F'Bf–ÆVBâÆV6R6Æ÷6RæB&V÷VâF†Râr“°¢Ð§Ð¦7–æ2gVæ7F–öâ6†V6µWFFU&V6÷fW'’‚—°¢G'—°¢6öç7B£Öv—B’‚rö’÷WFFU÷7FGW2r“°¢–b‚¢ç&öÆÆ&6²—&WGW&âfÇ6S°¢÷WFFU&öÆÆ&6µf—6–&ÆS×G'VS°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT×6rr’çFW‡D6öçFVçCÖ¢æÖW76vWÇÂtf–ÆVBWFFRv2&öÆÆVB&6²æBF†R&Wf–÷W2õEdÒfW'6–öâv2&W7F÷&VBâs°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFTæ÷t'Fâr’æ6Æ74Æ—7BæFB‚v†–FRr“¶Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFTÆFW$'Fâr’æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT&ææW"r’æ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“·&WGW&âG'VS°¢Ö6F6‚†R—·&WGW&âfÇ6S·Ð§Ð¦gVæ7F–öâF—6Ö—75WFFR‚—¶Fö7VÖVçBævWDVÆVÖVçD'”–B‚wWFFT&ææW"r’æ6Æ74Æ—7BæFB‚v†–FRr“¶–b…÷WFFU&öÆÆ&6µf—6–&ÆR—µ÷WFFU&öÆÆ&6µf—6–&ÆSÖfÇ6S¶’‚rö’÷WFFU÷7FGW5ö6²rÇ¶ÖWF†öC¢uõ5BwÒ’æ6F6‚‚‚“Óç·Ò“·×Ð¦6†V6µWFFU&V6÷fW'’‚’çF†Vâ†f÷VæCÓç¶–b‚f÷VæB–6†V6´f÷%WFFR‚“·Ò“°£Â÷67&—Cà£Âö&öG“ãÂö‡FÖÃà¢""  ¢2–æFWVæFVçB†öæR6Æ–VçBâ—BFVÆ–&W&FVÇ’6†&W2öæÇ’F†R…EE’v—F‚F†P¢2FW6·F÷T“¢æòFW6·F÷Ö&·WÂw&–G2Â6–FV&'2Â&VæFW&W'2Â÷"552&R&WW6VBà¤Ôô$”ÄUõtRÒ"rrsÂFö7G—R‡FÖÃà£Æ‡FÖÂÆæsÒ&æò#ãÆ†VCãÆÖWF6†'6WCÒ'WFbÓ‚#à£ÆÖWFæÖSÒ'f–Ww÷'B"6öçFVçCÒ'v–GFƒÖFWf–6R×v–GF‚Æ–æ—F–Â×66ÆSÓÇf–Ww÷'BÖf—CÖ6÷fW"#à£ÆÖWFæÖSÒ'F†VÖRÖ6öÆ÷""6öçFVçCÒ"3“C"#ãÇF—FÆSäöÆòÖö&–ÆSÂ÷F—FÆSà£Ç7G–ÆSà£§&ö÷G¶6öÆ÷"×66†VÖS¦F&³²ÒÖ&s¢3“C#²Ò×æVÃ¢3#ƒ#²Ò×æVÃ#¢3“##3²ÒÖÆ–æS¢3#s3#CS²Ò×FW‡C¢6cFcvf#²ÒÖ×WFVC¢3“#–V#²ÒÖ&ÇVS¢3FS†Fcs²ÒÖw&VVã¢3CVCCƒ3²Ò×&VC¢6cfSsC²ÒÖ÷&ævS¢6Vc†33S²Ò×6fS¦Vçb‡6fRÖ&VÖ–ç6WBÖ&÷GFöÒÃ‚—Ð¢§¶&÷‚×6—¦–æs¦&÷&FW"Ö&÷‡Ö‡FÖÂÆ&öG—¶Ö&v–ã£¶Ö–âÖ†V–v‡C£S¶&6¶w&÷VæC§f"‚ÒÖ&r“¶6öÆ÷#§f"‚Ò×FW‡B“¶föçBÖfÖ–Ç“§7—7FVÒ×V’ÂÖÆR×7—7FVÒÅ6VvöRT’Ç6ç2×6W&–gÖ'WGFöâÆ–çWBÇ6VÆV7G¶föçC¦–æ†W&—C¶6öÆ÷#¦–æ†W&—GÖ'WGFöç¶7W'6÷#§ö–çFW'Òæ¶Ö–âÖ†V–v‡C£Gfƒ·FF–ærÖ&÷GFöÓ¦6Æ2ƒƒG‚²f"‚Ò×6fR’—ÒçF÷·÷6—F–öã§7F–6·“·F÷£·¢Ö–æFWƒ£#¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶v£'ƒ·FF–æs£G‚gƒ¶&6¶w&÷VæC§&v&ƒ’Ã2Ã‚Âã“B“¶&6¶G&÷Öf–ÇFW#¦&ÇW"ƒg‚“¶&÷&FW"Ö&÷GFöÓ£‚6öÆ–Bf"‚ÒÖÆ–æR—Òæ'&æG¶föçB×6—¦S£#ƒ¶föçB×vV–v‡C£ƒS¶ÆWGFW"×76–æs¢ÒãG‡Òæ'&æB—¶föçB×7G–ÆS¦æ÷&ÖÃ¶6öÆ÷#§f"‚ÒÖ&ÇVR—Òæw&÷w¶fÆWƒ£Òæ–6öæ'FâÂç–ÆÂÂæ7F–öç¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&6¶w&÷VæC§f"‚Ò×æVÃ"“¶&÷&FW"×&F—W3£Gƒ·FF–æs£‚7‡Òæ–6öæ'Fç·v–GFƒ£CGƒ¶†V–v‡C£CGƒ·FF–æs£¶föçB×6—¦S£#‡Òç67&VVç¶F—7Æ“¦æöæS·FF–æs£‡‚W‚3ƒ¶Ö‚×v–GFƒ£cƒƒ¶Ö&v–ã¦WF÷Òç67&VVâæöç¶F—7Æ“¦&Æö6·Òæ†W&÷·FF–æs£‡‚'‚‡‡ÒæW–V'&÷w¶6öÆ÷#§f"‚ÒÖ&ÇVR“¶föçB×6—¦S£'ƒ¶föçB×vV–v‡C£ƒ·FW‡B×G&ç6f÷&Ó§WW&66S¶ÆWGFW"×76–æs£ã'‡Òæ†W&òƒ¶föçB×6—¦S£3ƒ¶Æ–æRÖ†V–v‡C£ãc¶Ö&v–ã£w‚‡ƒ¶ÆWGFW"×76–æs¢Ó‡Òæ×WFVG¶6öÆ÷#§f"‚ÒÖ×WFVB—Òç6V7F–öç¶Ö&v–ã£w‚#g‡Òç6V7F–öæ†VG¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶v£ƒ¶Ö&v–ã£'‚‡Òç6V7F–öæ†VBƒ'¶föçB×6—¦S£wƒ¶Ö&v–ã£Òæ6&G¶&6¶w&÷VæC¦Æ–æV"Öw&F–VçBƒCVFVrÇf"‚Ò×æVÂ’Â3cR“¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&÷&FW"×&F—W3£—ƒ¶÷fW&fÆ÷s¦†–FFVã¶Ö&v–âÖ&÷GFöÓ£‡Òç&÷w¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶v£'ƒ·FF–æs£7‡Òç&÷r²ç&÷w¶&÷&FW"×F÷£‚6öÆ–Bf"‚ÒÖÆ–æR—Òæ'G·v–GFƒ£SGƒ¶†V–v‡C£SGƒ¶fÆWƒ£SGƒ¶&÷&FW"×&F—W3£Gƒ¶&6¶w&÷VæC¢3#&#6¶ö&¦V7BÖf—C¦6÷fW'Òæ'Bç÷7FW'¶†V–v‡C£s‡ƒ¶&÷&FW"×&F—W3£‡ÒçF—FÆW¶föçB×vV–v‡C£sc¶Æ–æRÖ†V–v‡C£ã#WÒæÖWF¶6öÆ÷#§f"‚ÒÖ×WFVB“¶föçB×6—¦S£7ƒ¶Ö&v–â×F÷£Gƒ¶Æ–æRÖ†V–v‡C£ã3WÒçFw¶föçB×6—¦S£ƒ¶föçB×vV–v‡C£ƒ¶6öÆ÷#§f"‚ÒÖw&VVâ“¶&÷&FW#£‚6öÆ–B3#ƒcCC¶&6¶w&÷VæC¢3#S¶&÷&FW"×&F—W3£‡ƒ·FF–æs£G‚w‡ÒæÆ—fW¶6öÆ÷#¢6ffc¶&6¶w&÷VæC¢3–##c3¶&÷&FW"Ö6öÆ÷#¢6F3FCS—Òæ7F–öç7¶F—7Æ“¦fÆWƒ¶v£‡ƒ¶fÆW‚×w&§w&¶Ö&v–â×F÷£—‡Òæ7F–öç·FF–æs£‡‚ƒ¶&÷&FW"×&F—W3£ƒ¶föçB×6—¦S£7‡Òæ7F–öâç&–Ö'—¶&6¶w&÷VæC¢3SS&#s¶&÷&FW"Ö6öÆ÷#¢3#ƒfVF'Òæ7F–öâçfÆ7¶&6¶w&÷VæC¢6“FC3¶&÷&FW"Ö6öÆ÷#¢6Cscs‡Òç6V&6‡¶F—7Æ“¦fÆWƒ¶v£—ƒ¶Ö&v–âÖ&÷GFöÓ£G‡Òç6V&6‚–çWBÂæf–VÆB–çWBÂæf–VÆB6VÆV7G¶Ö–â×v–GFƒ£·v–GFƒ£S¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&6¶w&÷VæC¢3C#“¶&÷&FW"×&F—W3£Wƒ·FF–æs£G‚Wƒ¶÷WFÆ–æS¦æöæWÒç6V&6‚–çWC¦fö7W2Âæf–VÆB–çWC¦fö7W7¶&÷&FW"Ö6öÆ÷#§f"‚ÒÖ&ÇVR—Òç6V&6‚'WGFöç¶&÷&FW#£¶&6¶w&÷VæC§f"‚ÒÖ&ÇVR“¶&÷&FW"×&F—W3£Wƒ·FF–æs£‡ƒ¶föçB×vV–v‡C£ƒÒæ6†—7¶F—7Æ“¦fÆWƒ¶v£‡ƒ¶÷fW&fÆ÷s¦WFó·FF–æs£'‚‚ƒ·67&öÆÆ&"×v–GFƒ¦æöæWÒæ6†—·v†—FR×76S¦æ÷w&¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&6¶w&÷VæC§f"‚Ò×æVÂ“¶&÷&FW"×&F—W3£““—ƒ·FF–æs£—‚7‡Òæ6†—æöç¶&6¶w&÷VæC¢3s3#S“¶&÷&FW"Ö6öÆ÷#¢33Cs†FgÒæV×G—·FF–æs£3‚‡ƒ·FW‡BÖÆ–vã¦6VçFW#¶6öÆ÷#§f"‚ÒÖ×WFVB“¶&÷&FW#£‚F6†VBf"‚ÒÖÆ–æR“¶&÷&FW"×&F—W3£‡‡Òæf—‡GW&W·FF–æs£W‡ÒçFV×7¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶v£‡ƒ¶föçB×vV–v‡C£ƒ#¶föçB×6—¦S£w‡ÒçfW'7W7¶6öÆ÷#§f"‚ÒÖ×WFVB“¶föçB×vV–v‡C£SÒæf—‡GW&RæÖWF¶Ö&v–â×F÷£w‡Òæ6†ææVÆw&÷W¶&÷&FW"×F÷£‚6öÆ–Bf"‚ÒÖÆ–æR“·FF–æs£'‚G‡Òæ6†ææVÆw&÷W7VÖÖ'—¶föçB×vV–v‡C£sS¶7W'6÷#§ö–çFW'Òæ6†ææVÇ¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶v£ƒ·FF–æs£‚Òæ6†ææVÂ²æ6†ææVÇ¶&÷&FW"×F÷£‚6öÆ–B3#&3—Òæ6†ææVÂæ'G·v–GFƒ£3‡ƒ¶†V–v‡C£3‡ƒ¶fÆW‚Ö&6—3£3‡ƒ¶&÷&FW"×&F—W3£—‡ÒçVÆ—G—¶föçB×6—¦S£ƒ¶6öÆ÷#¢6#†36C7Òæw&–G¶F—7Æ“¦w&–C¶w&–B×FV×ÆFRÖ6öÇVÖç3§&WVBƒ"ÆÖ–æÖ‚ƒÃg"’“¶v£‡ÒçF–ÆW¶Ö–â×v–GFƒ£¶&6¶w&÷VæC§f"‚Ò×æVÂ“¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&÷&FW"×&F—W3£‡ƒ·FF–æs£'‡ÒçF–ÆR–Öw·v–GFƒ£S¶7V7B×&F–ó£"ó3¶ö&¦V7BÖf—C¦6÷fW#¶&÷&FW"×&F—W3£'ƒ¶&6¶w&÷VæC¢3#&#6ÒçF–ÆRçF—FÆW¶Ö&v–â×F÷£—ƒ·v†—FR×76S¦æ÷w&¶÷fW&fÆ÷s¦†–FFVã·FW‡BÖ÷fW&fÆ÷s¦VÆÆ—6—7Òæ6FVv÷&–W7¶F—7Æ“¦w&–C¶v£‡‡Òæ6FVv÷'—¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶v£ƒ·v–GFƒ£S·FW‡BÖÆ–vã¦ÆVgC¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&6¶w&÷VæC§f"‚Ò×æVÂ“¶&÷&FW"×&F—W3£Wƒ·FF–æs£7‡Òæ6FVv÷'’7ã¦Æ7BÖ6†–ÆG¶Ö&v–âÖÆVgC¦WFó¶6öÆ÷#§f"‚ÒÖ×WFVB—Òæf–VÆG¶Ö&v–ã£7‚Òæf–VÆBÆ&VÇ¶F—7Æ“¦&Æö6³¶6öÆ÷#§f"‚ÒÖ×WFVB“¶föçB×6—¦S£7ƒ¶Ö&v–ã£w‚7‡Òç6fW·v–GFƒ£S¶&÷&FW#£¶&6¶w&÷VæC§f"‚ÒÖ&ÇVR“¶&÷&FW"×&F—W3£Wƒ·FF–æs£Gƒ¶föçB×vV–v‡C£ƒSÒææ÷F–6W·FF–æs£'‚Gƒ¶&÷&FW"×&F—W3£Gƒ¶&6¶w&÷VæC¢33#3S¶6öÆ÷#¢63–C†c¶Ö&v–âÖ&÷GFöÓ£'‡Òæ&÷GFö×·÷6—F–öã¦f—†VC·¢Ö–æFWƒ£3¶ÆVgC£·&–v‡C£¶&÷GFöÓ£¶†V–v‡C¦6Æ2ƒs'‚²f"‚Ò×6fR’“·FF–æs£w‚‡‚f"‚Ò×6fR“¶F—7Æ“¦w&–C¶w&–B×FV×ÆFRÖ6öÇVÖç3§&WVBƒRÃg"“¶&6¶w&÷VæC§&v&ƒ‚Ã"ÃrÂã“b“¶&6¶G&÷Öf–ÇFW#¦&ÇW"ƒ‡‚“¶&÷&FW"×F÷£‚6öÆ–Bf"‚ÒÖÆ–æR—Òææg¶&÷&FW#£¶&6¶w&÷VæC§G&ç7&VçC¶6öÆ÷#§f"‚ÒÖ×WFVB“¶&÷&FW"×&F—W3£7ƒ¶föçB×6—¦S£ƒ¶föçB×vV–v‡C£s·FF–æs£W‚'‡Òææb'¶F—7Æ“¦&Æö6³¶föçB×6—¦S£#ƒ¶Æ–æRÖ†V–v‡C£#W‡Òææbæöç¶6öÆ÷#¢6ffc¶&6¶w&÷VæC¢3c#“CGÒç6†VWF&6·¶F—7Æ“¦æöæS·÷6—F–öã¦f—†VC·¢Ö–æFWƒ£C¶–ç6WC£¶&6¶w&÷VæC¢3—Òç6†VWF&6²æöç¶F—7Æ“¦&Æö6·Òç6†VWG·÷6—F–öã¦'6öÇWFS¶ÆVgC£·&–v‡C£¶&÷GFöÓ£¶&6¶w&÷VæC¢3#“#3¶&÷&FW"×&F—W3£#G‚#G‚·FF–æs£‚W‚6Æ2ƒ#'‚²f"‚Ò×6fR’“¶Ö‚Ö†V–v‡C£s†Gfƒ¶÷fW&fÆ÷s¦WF÷Òæ†æFÆW·v–GFƒ£C'ƒ¶†V–v‡C£Wƒ¶&÷&FW"×&F—W3£Wƒ¶&6¶w&÷VæC¢3CSc“¶Ö&v–ã£'‚WFòw‡Òç6†VWF—FV×·v–GFƒ£S¶F—7Æ“¦fÆWƒ¶v£7ƒ¶Æ–vâÖ—FV×3¦6VçFW#¶&÷&FW#£¶&÷&FW"Ö&÷GFöÓ£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&6¶w&÷VæC§G&ç7&VçC·FF–æs£W‚‡ƒ·FW‡BÖÆ–vã¦ÆVgC¶föçB×vV–v‡C£sSÒç6†VWF—FVÒ'¶föçB×6—¦S£#'ƒ·v–GFƒ£3‡ÒæÆöFW'·v–GFƒ£#Gƒ¶†V–v‡C£#Gƒ¶&÷&FW#£7‚6öÆ–B33CCSS¶&÷&FW"×F÷Ö6öÆ÷#§f"‚ÒÖ&ÇVR“¶&÷&FW"×&F—W3£SS¶æ–ÖF–öã§7–âã‡2Æ–æV"–æf–æ—FS¶Ö&v–ã£3‚WF÷Ô¶W–g&ÖW27–ç·F÷·G&ç6f÷&Ó§&÷FFRƒ3cFVr—×Ð¢çF–ÆRçF–ÆWÆ—¶7W'6÷#§ö–çFW#·÷6—F–öã§&VÆF—fWÒçF–ÆRçF–ÆWÆ“¦7F—fW·G&ç6f÷&Ó§66ÆR‚ã“‚—ÒçF–ÆV&FvW·÷6—F–öã¦'6öÇWFS·F÷£‡ƒ·&–v‡C£‡ƒ·v–GFƒ£3Gƒ¶†V–v‡C£3Gƒ¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶§W7F–g’Ö6öçFVçC¦6VçFW#¶&6¶w&÷VæC§&v&ƒ#Ãƒ"Ãƒ2Âã“"“¶&÷&FW"×&F—W3£SS¶föçB×6—¦S£Wƒ¶6öÆ÷#¢6ffc·ö–çFW"ÖWfVçG3¦æöæWÐ¢æ×Æ–W'·÷6—F–öã¦f—†VC¶–ç6WC£·¢Ö–æFWƒ£c¶&6¶w&÷VæC¢33¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶§W7F–g’Ö6öçFVçC¦6VçFW#·FF–æs£'‡Òæ×Æ–W"æ†–FW¶F—7Æ“¦æöæWÒæ×Æ–W&&÷‡·v–GFƒ£S¶Ö‚×v–GFƒ£cƒƒ¶&6¶w&÷VæC¢33s¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&÷&FW"×&F—W3£‡ƒ¶÷fW&fÆ÷s¦†–FFVçÒæ×Æ–W&&'¶F—7Æ“¦fÆWƒ¶Æ–vâÖ—FV×3¦6VçFW#¶v£ƒ·FF–æs£‚Gƒ¶&÷&FW"Ö&÷GFöÓ£‚6öÆ–Bf"‚ÒÖÆ–æR—Òæ×Æ–W&&"7ç¶fÆWƒ£¶föçB×vV–v‡C£sS·v†—FR×76S¦æ÷w&¶÷fW&fÆ÷s¦†–FFVã·FW‡BÖ÷fW&fÆ÷s¦VÆÆ—6—7Òæ×Æ–W&6Æ÷6W¶&÷&FW#£¶&6¶w&÷VæC§G&ç7&VçC¶6öÆ÷#§f"‚ÒÖ×WFVB“¶föçB×6—¦S£#gƒ¶Æ–æRÖ†V–v‡C£·FF–æs£G‡Òæ×Æ–W"f–FV÷·v–GFƒ£S¶7V7B×&F–ó£bó“¶&6¶w&÷VæC¢3¶F—7Æ“¦&Æö6·Òæ×Æ–W&×6w·FF–æs£‡‚Gƒ¶6öÆ÷#§f"‚ÒÖ×WFVB“¶föçB×6—¦S£7ƒ¶Ö–âÖ†V–v‡C£‡‡Òæ×Æ–W&7F–öç7¶F—7Æ“¦fÆWƒ¶v£‡ƒ·FF–æs£G‚G‡Òæ×Æ–W&7F–öç2æ7F–öç¶fÆWƒ£·FW‡BÖÆ–vã¦6VçFW'Ð¤ÖVF–†Ö–â×v–GFƒ£s‚—²æ&÷GFö×¶ÆVgC£SS·&–v‡C¦WFó·v–GFƒ£cƒƒ·G&ç6f÷&Ó§G&ç6ÆFU‚‚ÓSR“¶&÷&FW#£‚6öÆ–Bf"‚ÒÖÆ–æR“¶&÷&FW"Ö&÷GFöÓ£¶&÷&FW"×&F—W3£#‚#‚Òæw&–G¶w&–B×FV×ÆFRÖ6öÇVÖç3§&WVBƒ2Ãg"—×Ð£Â÷7G–ÆSãÂö†VCãÆ&öG“ãÆF—b6Æ73Ò&#à£Æ†VFW"6Æ73Ò'F÷#ãÆF—b6Æ73Ò&'&æB#ãÆ“äöÆóÂö“âEcÂöF—cãÆF—b6Æ73Ò&w&÷r#ãÂöF—cãÆ'WGFöâ6Æ73Ò&–6öæ'Fâ"öæ6Æ–6³Ò'&Vg&W6„7W'&VçB‚’"&–ÖÆ&VÃÒ$÷FFW"#î(k³Âö'WGFöããÆ'WGFöâ6Æ73Ò&–6öæ'Fâ"öæ6Æ–6³Ò&÷VäÖ÷&R‚’"&–ÖÆ&VÃÒ$ÖW"#î(
.(
.(
#Âö'WGFöããÂö†VFW#à£ÆÖ–â–CÒ&†öÖR"6Æ73Ò'67&VVâöâ#ãÆF—b6Æ73Ò&†W&ò#ãÆF—b6Æ73Ò&W–V'&÷r#äF–âEdÖFSÂöF—cãÆƒ–CÒ&†VÆÆò#ävöBFsÂöƒãÆF—b6Æ73Ò&×WFVB#äFWBf–·F–w7FRÂF–Ç76WBFVÆVföæVâãÂöF—cãÂöF—cãÇ6V7F–öâ6Æ73Ò'6V7F–öâ#ãÆF—b6Æ73Ò'6V7F–öæ†VB#ãÆƒ#äæW7FR¶×W#Âöƒ#ãÆF—b6Æ73Ò&w&÷r#ãÂöF—cãÆ'WGFöâ6Æ73Ò'–ÆÂ"öæ6Æ–6³Ò&vò‚w7÷'Br’#å6RÆÆSÂö'WGFöããÂöF—cãÆF—b–CÒ&†öÖU7÷'B#ãÆF—b6Æ73Ò&ÆöFW"#ãÂöF—cãÂöF—cãÂ÷6V7F–öããÇ6V7F–öâ6Æ73Ò'6V7F–öâ#ãÆF—b6Æ73Ò'6V7F–öæ†VB#ãÆƒ#äff÷&—GF¶æÆW#Âöƒ#ãÂöF—cãÆF—b–CÒ&†öÖT6†ææVÇ2#ãÂöF—cãÂ÷6V7F–öããÇ6V7F–öâ6Æ73Ò'6V7F–öâ#ãÆF—b6Æ73Ò'6V7F–öæ†VB#ãÆƒ#äç–RW—6öFW#Âöƒ#ãÂöF—cãÆF—b–CÒ&†öÖTW—6öFW2#ãÂöF—cãÂ÷6V7F–öããÂöÖ–ãà£ÆÖ–â–CÒ'Gb"6Æ73Ò'67&VVâ#ãÆF—b6Æ73Ò&†W&ò#ãÆF—b6Æ73Ò&W–V'&÷r#äÆ—fREcÂöF—cãÆƒä¶æÆW#ÂöƒãÂöF—cãÆF—b6Æ73Ò'6V&6‚#ãÆ–çWB–CÒ'Ge"Æ6V†öÆFW#Ò%<;†²WGFW"Vâ¶æÂ#ãÆ'WGFöâöæ6Æ–6³Ò&ÆöEEb‚’#å<;†³Âö'WGFöããÂöF—cãÆF—b–CÒ'Gd6†ææVÇ2#ãÆF—b6Æ73Ò&ÆöFW"#ãÂöF—cãÂöF—cãÂöÖ–ãà£ÆÖ–â–CÒ'7÷'B"6Æ73Ò'67&VVâ#ãÆF—b6Æ73Ò&†W&ò#ãÆF—b6Æ73Ò&W–V'&÷r#å7÷'CÂöF—cãÆƒä¶×W"ör¶æÆW#ÂöƒãÂöF—cãÆF—b6Æ73Ò'6V&6‚#ãÆ–çWB–CÒ'7÷'E"Æ6V†öÆFW#Ò$ÆrÂbæV·2âÆVVG2#ãÆ'WGFöâöæ6Æ–6³Ò'6V&6…7÷'B‚’#å<;†³Âö'WGFöããÂöF—cãÆF—b–CÒ'7÷'E&÷w2#ãÆF—b6Æ73Ò&ÆöFW"#ãÂöF—cãÂöF—cãÂöÖ–ãà£ÆÖ–â–CÒ&Æ–'&'’"6Æ73Ò'67&VVâ#ãÆF—b6Æ73Ò&†W&ò#ãÆF—b6Æ73Ò&W–V'&÷r#ä&–&Æ–÷FV³ÂöF—cãÆƒäf–ÆÖW"ör6W&–W#ÂöƒãÂöF—cãÆF—b6Æ73Ò&6†—2#ãÆ'WGFöâ6Æ73Ò&6†—öâ"FFÖÆ–#Ò&Ö÷f–W2"öæ6Æ–6³Ò&Æ–'&'•F"‡F†—2’#äf–ÆÖW#Âö'WGFöããÆ'WGFöâ6Æ73Ò&6†—"FFÖÆ–#Ò'6†÷w2"öæ6Æ–6³Ò&Æ–'&'•F"‡F†—2’#å6W&–W#Âö'WGFöããÂöF—cãÆF—b6Æ73Ò'6V&6‚#ãÆ–çWB–CÒ&Æ–'&'•"Æ6V†öÆFW#Ò%<;†²#ãÆ'WGFöâöæ6Æ–6³Ò'6V&6„Æ–'&'’‚’#å<;†³Âö'WGFöããÂöF—cãÆF—b–CÒ&Æ–'&'•&÷w2#ãÆF—b6Æ73Ò&ÆöFW"#ãÂöF—cãÂöF—cãÂöÖ–ãà£ÆÖ–â–CÒ'Æ–Æ—7B"6Æ73Ò'67&VVâ#ãÆF—b6Æ73Ò&†W&ò#ãÆF—b6Æ73Ò&W–V'&÷r#åÆ–Æ—7B'V–ÆFW#ÂöF—cãÆƒäf–æâ¶æÆW#ÂöƒãÂöF—cãÆF—b6Æ73Ò'6V&6‚#ãÆ–çWB–CÒ'Æ–Æ—7E"Æ6V†öÆFW#Ò&bæV·2âb7÷'B#ãÆ'WGFöâöæ6Æ–6³Ò'6V&6…Æ–Æ—7B‚’#å<;†³Âö'WGFöããÂöF—cãÆF—b–CÒ'Æ–Æ—7E&÷w2#ãÂöF—cãÇ6V7F–öâ6Æ73Ò'6V7F–öâ#ãÆF—b6Æ73Ò'6V7F–öæ†VB#ãÆƒ#ä¶FVv÷&–W#Âöƒ#ãÂöF—cãÆF—b–CÒ&6FVv÷'•&÷w2#ãÆF—b6Æ73Ò&ÆöFW"#ãÂöF—cãÂöF—cãÂ÷6V7F–öããÂöÖ–ãà£ÆÖ–â–CÒ'&6–ær"6Æ73Ò'67&VVâ#ãÆF—b6Æ73Ò&†W&ò#ãÆF—b6Æ73Ò&W–V'&÷r#å&6–æsÂöF—cãÆƒäÌ;‡örl;‡&W&SÂöƒãÂöF—cãÆF—b–CÒ'&6–æu&÷w2#ãÆF—b6Æ73Ò&ÆöFW"#ãÂöF—cãÂöF—cãÂöÖ–ãà£ÆÖ–â–CÒ&vÖW2"6Æ73Ò'67&VVâ#ãÆF—b6Æ73Ò&†W&ò#ãÆF—b6Æ73Ò&W–V'&÷r#å7–ÆÃÂöF—cãÆƒì9†ç6¶VÆ—7FSÂöƒãÂöF—cãÆF—b–CÒ&vÖU&÷w2#ãÆF—b6Æ73Ò&ÆöFW"#ãÂöF—cãÂöF—cãÂöÖ–ãà£ÆÖ–â–CÒ'6WGF–æw2"6Æ73Ò'67&VVâ#ãÆF—b6Æ73Ò&†W&ò#ãÆF—b6Æ73Ò&W–V'&÷r#ä–æç7F–ÆÆ–ævW#ÂöF—cãÆƒäöÆòEdÖFSÂöƒãÆF—b6Æ73Ò&×WFVB#äÖö&–Çf—6æ–ævVâÆw&W"FR6ÖÖR–æç7F–ÆÆ–ævVæR6öÒ2ÖVâãÂöF—cãÂöF—cãÆF—b–CÒ'6WGF–æw5&÷w2#ãÆF—b6Æ73Ò&ÆöFW"#ãÂöF—cãÂöF—cãÂöÖ–ãà£ÂöF—cà£Ææb6Æ73Ò&&÷GFöÒ#ãÆ'WGFöâ6Æ73Ò&æböâ"FFÖvóÒ&†öÖR#ãÆ#î(È#Âö#ä†¦VÓÂö'WGFöããÆ'WGFöâ6Æ73Ò&æb"FFÖvóÒ'Gb#ãÆ#î)j3Âö#äÆ—fREcÂö'WGFöããÆ'WGFöâ6Æ73Ò&æb"FFÖvóÒ'7÷'B#ãÆ#î)«ÓÂö#å7÷'CÂö'WGFöããÆ'WGFöâ6Æ73Ò&æb"FFÖvóÒ&Æ–'&'’#ãÆ#î)kcÂö#ä&–&Æ–÷FV³Âö'WGFöããÆ'WGFöâ6Æ73Ò&æb"öæ6Æ–6³Ò&÷VäÖ÷&R‚’#ãÆ#î)‹Âö#äÖW#Âö'WGFöããÂöæcà£ÆF—b–CÒ'6†VWF&6²"6Æ73Ò'6†VWF&6²"öæ6Æ–6³Ò&6Æ÷6TÖ÷&R†WfVçB’#ãÆF—b6Æ73Ò'6†VWB#ãÆF—b6Æ73Ò&†æFÆR#ãÂöF—cãÆ'WGFöâ6Æ73Ò'6†VWF—FVÒ"öæ6Æ–6³Ò&vò‚wÆ–Æ—7Br’#ãÆ#î)‹sÂö#åÆ–Æ—7B'V–ÆFW#Âö'WGFöããÆ'WGFöâ6Æ73Ò'6†VWF—FVÒ"öæ6Æ–6³Ò&vò‚w&6–ærr’#ãÆ#ï	øøÂö#å&6–æsÂö'WGFöããÆ'WGFöâ6Æ73Ò'6†VWF—FVÒ"öæ6Æ–6³Ò&vò‚vvÖW2r’#ãÆ#ï	øêãÂö#å7–ÆÃÂö'WGFöããÆ'WGFöâ6Æ73Ò'6†VWF—FVÒ"öæ6Æ–6³Ò&vò‚w6WGF–æw2r’#ãÆ#î)©“Âö#ä–æç7F–ÆÆ–ævW#Âö'WGFöããÆ'WGFöâ6Æ73Ò'6†VWF—FVÒ"öæ6Æ–6³Ò&Æö6F–öâæ‡&VcÒröFW6·F÷r#ãÆ#î(isÂö#ì8WæR2×f—6æ–æsÂö'WGFöããÂöF—cãÂöF—cà£ÆF—b–CÒ&×Æ–W""6Æ73Ò&×Æ–W"†–FR"öæ6Æ–6³Ò&–b†WfVçBçF&vWCÓÓ×F†—2–6Æ÷6UÆ–W"‚’#ãÆF—b6Æ73Ò&×Æ–W&&÷‚#ãÆF—b6Æ73Ò&×Æ–W&&"#ãÇ7â–CÒ&×Æ–W%F—FÆR#å7–ÆÆW#Â÷7ããÆ'WGFöâ6Æ73Ò&×Æ–W&6Æ÷6R"öæ6Æ–6³Ò&6Æ÷6UÆ–W"‚’#âgF–ÖW3³Âö'WGFöããÂöF—cãÇf–FVò–CÒ&×f–FVò"6öçG&öÇ2WF÷Æ’Æ—6–æÆ–æRvV&¶—B×Æ—6–æÆ–æSãÂ÷f–FVóãÆF—b–CÒ&×Æ–W$×6r"6Æ73Ò&×Æ–W&×6r#ãÂöF—cãÆF—b6Æ73Ò&×Æ–W&7F–öç2#ãÆ'WGFöâ6Æ73Ò&7F–öâfÆ2"–CÒ&×Æ–W%fÆ2#ì8WæR’dÄ3Âö'WGFöããÂöF—cãÂöF—cãÂöF—cà£Ç67&—Cà¦6öç7B3×·67&VVã¢v†öÖRrÆÆ–'&'“¢vÖ÷f–W2rÆÆöFVC§·×Ó¶6öç7BCÖ–CÓæFö7VÖVçBævWDVÆVÖVçD'”–B†–B“¶6öç7BW63×3Óå7G&–ær‡3óòrr’ç&WÆ6R‚õ²cÃâ"uÒörÆ3Óâ‡²rbs¢rf×²rÂsÂs¢rfÇC²rÂsâs¢rfwC²rÂr"s¢rgV÷C²rÂ"r#¢rb33“²wÕ¶5Ò’“°¦7–æ2gVæ7F–öâ’‡F‚Æ÷B—¶6öç7B#Öv—BfWF6‚‡F‚Æ÷B“¶ÆWB£×·Ó·G'—¶£Öv—B"æ§6öâ‚—Ö6F6‚†R—·Ö–b‚"æö²—F‡&÷rW'&÷"†¢æW'&÷'ÇÇ"ç7FGW2“·&WGW&â§Ð¦gVæ7F–öâV×G’‡B—·&WGW&âsÆF—b6Æ73Ò&V×G’#âr¶W62‡B’²sÂöF—câwÖgVæ7F–öâ'B‡7&2Æ6Ç3Òrr—·&WGW&â7&3òsÆ–Ör6Æ73Ò&'Br¶6Ç2²r"7&3Ò"r¶W62‡7&2’²r"ÆöF–æsÒ&Æ§’"öæW'&÷#Ò'F†—2ç&VÖ÷fR‚’#âs¢sÆF—b6Æ73Ò&'Br¶6Ç2²r#ãÂöF—câwÐ¦gVæ7F–öâ÷VäÖ÷&R‚—²B‚w6†VWF&6²r’æ6Æ74Æ—7BæFB‚vöâr—ÖgVæ7F–öâ6Æ÷6TÖ÷&R†R—¶–b‚WÇÆRçF&vWCÓÓÒB‚w6†VWF&6²r’’B‚w6†VWF&6²r’æ6Æ74Æ—7Bç&VÖ÷fR‚vöâr—Ð¦gVæ7F–öâvò†–B—¶6Æ÷6TÖ÷&R‚“¶Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rç67&VVâr’æf÷$V6‚‡ƒÓç‚æ6Æ74Æ—7BçFövvÆR‚vöârÇ‚æ–CÓÓÖ–B’“¶Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rææe¶FFÖvõÒr’æf÷$V6‚‡ƒÓç‚æ6Æ74Æ—7BçFövvÆR‚vöârÇ‚æFF6WBævóÓÓÖ–B’“µ2ç67&VVãÖ–C·67&öÆÅFòƒÃ“¶ÆöE67&VVâ†–B—Ð¦Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rææe¶FFÖvõÒr’æf÷$V6‚†#Óæ"æöæ6Æ–6³Ò‚“Óævò†"æFF6WBævò’“°¦gVæ7F–öâf×DFFR‡b—¶–b‚b—&WGW&ârs¶6öç7BCÖæWrFFR‡b“·&WGW&â—4æâ†B“÷c¦BçFôÆö6ÆU7G&–ær‚væ"ÔäòrÇ·vVV¶F“¢w6†÷'BrÆF“¢vçVÖW&–2rÆÖöçFƒ¢w6†÷'BrÆ†÷W#¢s"ÖF–v—BrÆÖ–çWFS¢s"ÖF–v—BwÒ—Ð¦gVæ7F–öâ7G&VÔ7F–öç2†2—¶6öç7B6–CÖ2ç7G&VÕö–C¶–b‡6–CÓÖçVÆÂ—&WGW&ârs·&WGW&âsÆF—b6Æ73Ò&7F–öç2#ãÆ'WGFöâ6Æ73Ò&7F–öâ&–Ö'’"öæ6Æ–6³Ò'Æ’‚r´¥4ôâç7G&–æv–g’…7G&–ær‡6–B’’²r’#î)kb7–ÆÃÂö'WGFöããÆ'WGFöâ6Æ73Ò&7F–öâfÆ2"öæ6Æ–6³Ò'fÆ2‚r´¥4ôâç7G&–æv–g’…7G&–ær‡6–B’’²r’#ådÄ3Âö'WGFöããÂöF—câwÐ¦ÆWBöÖ†Ç3ÖçVÆÂÅö×7G&VÓÖçVÆÃ°¦gVæ7F–öâ6Æ÷6UÆ–W"‚—¶6öç7BÓÒB‚v×Æ–W"r’ÇcÒB‚v×f–FVòr“¶–b…öÖ†Ç2—·G'—µöÖ†Ç2æFW7G&÷’‚—Ö6F6‚†R—·ÕöÖ†Ç3ÖçVÆÇ×G'—·bçW6R‚“·bç&VÖ÷fTGG&–'WFR‚w7&2r“·bæÆöB‚—Ö6F6‚†R—·ÖÒæ6Æ74Æ—7BæFB‚v†–FRr“µö×7G&VÓÖçVÆÇÐ¦7–æ2gVæ7F–öâÆ’†–B—°¢6öç7BÓÒB‚v×Æ–W"r’ÇcÒB‚v×f–FVòr’Æ×6sÒB‚v×Æ–W$×6rr“°¢B‚v×Æ–W%F—FÆRr’çFW‡D6öçFVçCÒu7–ÆÆW"s¶×6rçFW‡D6öçFVçCÒtÆ7FW.(
bs¶Òæ6Æ74Æ—7Bç&VÖ÷fR‚v†–FRr“°¢–b…öÖ†Ç2—·G'—µöÖ†Ç2æFW7G&÷’‚—Ö6F6‚†R—·ÕöÖ†Ç3ÖçVÆÇÐ¢ÆWBW&Ç3°¢G'—·W&Ç3Öv—B’‚rö’ö†Ç3ö–CÒr¶Væ6öFUU$”6ö×öæVçB†–B’“¶–b‡W&Ç2æW'&÷'ÇÂW&Ç2æ†Ç2—F‡&÷r·Ð¢6F6‚†R—¶×6rçFW‡D6öçFVçCÒt·VææR–¶¶R'–vvR7G,;†ÒÕU$Ââs·&WGW&ã·Ð¢ö×7G&VÓÕ7G&–ær†–B“°¢B‚v×Æ–W%fÆ2r’æöæ6Æ–6³ÖgVæ7F–öâ‚—·fÆ2†–B“·Ó°¢7F'DÖö&–ÆU7G&VÒ‡bÇW&Ç2Ç3Óæ×6rçFW‡D6öçFVçC×2“°§Ð¦gVæ7F–öâ7F'DÖö&–ÆU7G&VÒ‡f–FVòÇW&Ç2Ç7FGW2—°¢ÆWB7F÷VCÖfÇ6S°¢gVæ7F–öâf–&÷‡’‡R—·&WGW&ârö’÷&÷‡“÷SÒr¶Væ6öFUU$”6ö×öæVçB‡R“·Ð¢gVæ7F–öâG'”†Ç2‡7&2Ç&÷†–VB—°¢–b‡v–æF÷rä†Ç2bd†Ç2æ—57W÷'FVB‚’—°¢7FGW2‡&÷†–VCòt¶ö&ÆW"F–Î(
bs¢tÆ7FW"„Å>(
br“°¢öÖ†Ç3ÖæWr†Ç2‡¶Öæ–fW7DÆöF–æuF–ÖT÷WC£#Æg&tÆöF–æuF–ÖT÷WC£#Æ&6´'VffW$ÆVæwFƒ£3ÆÖ„'VffW$ÆVæwFƒ£CWÒ“°¢öÖ†Ç2æöâ„†Ç2äWfVçG2äU%$õ"ÆgVæ7F–öâ†WbÆFF—°¢–b‡7F÷VGÇÂFFæfFÂ—&WGW&ã°¢–b‚&÷†–VB—·G'—µöÖ†Ç2æFW7G&÷’‚—Ö6F6‚†R—·ÕöÖ†Ç3ÖçVÆÃ·G'”†Ç2‡f–&÷‡’‡W&Ç2æ†Ç2’ÇG'VR“·&WGW&ã·Ð¢7FGW2‚t·VææR–¶¶R7–ÆÆR’Vââ,;‡bdÄ2âr“°¢Ò“°¢öÖ†Ç2æöâ„†Ç2äWfVçG2äÔä”dU5Eõ%4TBÆgVæ7F–öâ‚—·7FGW2‚rr“¶6öç7B×f–FVòçÆ’‚“¶–b‡bgæ6F6‚—æ6F6‚‚‚“Óç·Ò“·Ò“°¢öÖ†Ç2æÆöE6÷W&6R‡7&2“µöÖ†Ç2æGF6„ÖVF–‡f–FVò“·&WGW&ã°¢Ð¢–b‡f–FVòæ6åÆ•G—R‚vÆ–6F–öâ÷fæBæÆRæ×VwW&Âr’—°¢7FGW2‚tÆ7FW"„Å>(
br“·f–FVòç7&3×7&3°¢f–FVòæFDWfVçDÆ—7FVæW"‚vW'&÷"rÆgVæ7F–öâ‚‚—·f–FVòç&VÖ÷fTWfVçDÆ—7FVæW"‚vW'&÷"rÆ‚“¶–b‚&÷†–VB—G'”†Ç2‡f–&÷‡’‡W&Ç2æ†Ç2’ÇG'VR“¶VÇ6R7FGW2‚t·VææR–¶¶R7–ÆÆR’Vââ,;‡bdÄ2âr“·ÒÇ¶öæ6S§G'VWÒ“°¢6öç7B×f–FVòçÆ’‚“¶–b‡bgæ6F6‚—æ6F6‚‚‚“Óç·Ò“·&WGW&ã°¢Ð¢7FGW2‚t·VææR–¶¶R7–ÆÆR’Vââ,;‡bdÄ2âr“°¢Ð¢G'”†Ç2‡W&Ç2æ†Ç2ÂW&Ç2ç&VÆ’“°§Ð¦7–æ2gVæ7F–öâfÆ2†–B—·G'—¶6öç7B£Öv—B’‚rö’÷Æ’rÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡·7G&VÕö–C¦–GÒ—Ò“¶–b†¢çÆ–Æ—7B—v–æF÷ræÆö6F–öâæ‡&VcÖ¢çÆ–Æ—7C¶VÇ6R–b†¢æW'&÷"—F‡&÷ræWrW'&÷"†¢æW'&÷"—Ö6F6‚†R—¶ÆW'B‚t·VææR–¶¶R7F'FRdÄ2:R2ÖVââr—×Ð¦gVæ7F–öâ6†ææVÅ&÷r†2—·&WGW&âsÆF—b6Æ73Ò'&÷r#âr¶'B†2æÆöv÷ÇÆ2ç7G&VÕö–6öçÇÂ‚rö’ö6†ææVÅöÆövóö–CÒr¶Væ6öFUU$”6ö×öæVçB†2ç7G&VÕö–GÇÂrr’’’²sÆF—b6Æ73Ò&w&÷r#ãÆF—b6Æ73Ò'F—FÆR#âr¶W62†2ææÖWÇÆ2ç‡G&VÕöæÖWÇÂt¶æÂr’²sÂöF—cãÆF—b6Æ73Ò&ÖWF#âr¶W62†2æ6FVv÷'—ÇÆ2æ6FVv÷'•öæÖWÇÂrr’²rÇ7â6Æ73Ò'VÆ—G’#âr¶W62†2çVÆ—G—ÇÂrr’²sÂ÷7ããÂöF—câr·7G&VÔ7F–öç2†2’²sÂöF—cãÂöF—câwÐ¦7–æ2gVæ7F–öâÆöD†öÖR†f÷&6R—·G'—¶6öç7B¶bÆÒÆUÓÖv—B&öÖ—6RæÆÂ…¶’‚rö’öff÷&—FW2r’Æ’‚rö’ö×•÷FV×2r’Æ’‚rö’öÆFW7EöW—6öFW3öÆ–Ö—CÓRr²†f÷&6Sòrg&Vg&W6ƒÓs¢rr’•Ò“¶6öç7Bf—‡GW&W3Ò†Òæf—‡GW&W7ÇÅµÒ’æf–ÇFW"‡ƒÓâ‚æ—5öf–æ—6†VB’ç6Æ–6RƒÃ2“²B‚v†öÖU7÷'Br’æ–ææW$…DÔÃÖf—‡GW&W2æÆVæwFƒöf—‡GW&W2æÖ†cÓæf—‡GW&T6&B†bÆfÇ6R’’æ¦ö–â‚rr“¦V×G’‚t–ævVâ¶öÖÖVæFRff÷&—GF¶×W"âr“²B‚v†öÖT6†ææVÇ2r’æ–ææW$…DÔÃÒ†bæ6†ææVÇ7ÇÅµÒ’ç6Æ–6RƒÃR’æÖ†6†ææVÅ&÷r’æ¦ö–â‚rr—ÇÆV×G’‚tÆVvrF–Âff÷&—GF¶æÆW"’Æ—fREbâr“²B‚v†öÖTW—6öFW2r’æ–ææW$…DÔÃÒ†RæW—6öFW7ÇÅµÒ’ç6Æ–6RƒÃR’æÖ‡ƒÓâsÆF—b6Æ73Ò&6&B#ãÆF—b6Æ73Ò'&÷r#âr¶'B‡‚æ–ÖvWÇÇ‚æ6÷fW"Âw÷7FW"r’²sÆF—b6Æ73Ò&w&÷r#ãÆF—b6Æ73Ò'F—FÆR#âr¶W62‡‚ç6†÷uöæÖWÇÇ‚ææÖWÇÂtW—6öFRr’²sÂöF—cãÆF—b6Æ73Ò&ÖWF#âr¶W62‡‚æW—6öFUöæÖWÇÇ‚çF—FÆWÇÂrr’²sÂöF—cãÂöF—cãÂöF—cãÂöF—câr’æ¦ö–â‚rr—ÇÆV×G’‚t–ævVâç–RW—6öFW"âr—Ö6F6‚†R—²B‚v†öÖU7÷'Br’æ–ææW$…DÔÃÖV×G’‚t·VææR–¶¶RÆ7FR–ææ†öÆBâr—×Ð¦gVæ7F–öâf—‡GW&T6&B†bÆFWF–Ç3×G'VR—¶6öç7BÆ—fSÖbæ—5öÆ—fSòsÇ7â6Æ73Ò'FrÆ—fR#äÄ•dSÂ÷7ãâs¢rs¶ÆWBW‡G&Òrs¶–b†FWF–Ç2—¶6öç7B6V7W&SÖbæÖF6†W7ÇÅµÒÇ÷76–&ÆSÖbçeö†—G7ÇÅµÓ¶–b‡6V7W&RæÆVæwF‚–W‡G&ÒsÆF—b6Æ73Ò&6†ææVÆw&÷W#ãÆ#å6–·&R¶æÇG&VfcÂö#âr·6V7W&Rç6Æ–6RƒÃR’æÖ†6†ææVÅ&÷r’æ¦ö–â‚rr’²sÂöF—câs¶–b‡÷76–&ÆRæÆVæwF‚–W‡G&³ÒsÆFWF–Ç26Æ73Ò&6†ææVÆw&÷W#ãÇ7VÖÖ'“ä×VÆ–vR¶æÆW"‚r·÷76–&ÆRæÆVæwF‚²r“Â÷7VÖÖ'“âr·÷76–&ÆRç6Æ–6RƒÃ3’æÖ†6†ææVÅ&÷r’æ¦ö–â‚rr’²sÂöFWF–Ç3âs·×&WGW&âsÆ'F–6ÆR6Æ73Ò&6&B#ãÆF—b6Æ73Ò&f—‡GW&R#ãÆF—b6Æ73Ò'FV×2#ãÇ7ãâr¶W62†bæ†öÖWÇÂrr’²sÂ÷7ããÇ7â6Æ73Ò'fW'7W2#î(	3Â÷7ããÇ7ãâr¶W62†bæv—ÇÂrr’²sÂ÷7ããÆF—b6Æ73Ò&w&÷r#ãÂöF—câr¶Æ—fR²sÂöF—cãÆF—b6Æ73Ò&ÖWF#âr¶W62†bæÆVwVUöæÖWÇÂrr’²r+rr¶W62†f×DFFR†bç7F'B’’²sÂöF—câr²†FWF–Ç2bbW‡G&òsÆF—b6Æ73Ò&7F–öç2#ãÆ'WGFöâ6Æ73Ò&7F–öâ&–Ö'’"öæ6Æ–6³Ò&6†V6´f—‡GW&R‡F†—2’#äf–æâ¶æÆW#Âö'WGFöããÂöF—câs¢rr’²sÂöF—câr¶W‡G&²sÂö'F–6ÆSâwÐ¦7–æ2gVæ7F–öâÆöE7÷'B‚—·G'—¶6öç7B#Öv—B’‚rö’ö×•÷FV×2r“¶ÆWBgƒÒ‡"æf—‡GW&W7ÇÅµÒ’æf–ÇFW"‡ƒÓâ‚æ—5öf–æ—6†VB“°¢òò6B2W6öÖ–ærf—‡GW&W2W"ff÷W&—FRFVÒ6òöæRFVÒ6âwBfÆööBF†P¢òòÆ—7Bâf—‡GW&W2&RÇ&VG’6÷'FVB'’7F'BF–ÖRÂ6òF†RV&Æ–W7B2v–âà¢6öç7BW%FVÓ×·ÒÆ6VCÕµÓ°¢g‚æf÷$V6‚†gVæ7F–öâ†b—°¢6öç7BFV×3Ò†bæff÷&—FU÷FV×2bfbæff÷&—FU÷FV×2æÆVæwF‚“öbæff÷&—FU÷FV×3¥²†bæ†öÖWÇÂrr•Ó°¢òòf—‡GW&R6÷VçG2F÷v&BV6‚öb—G2FV×3²¶VW—B–bå’FVÒ—2VæFW"2à¢ÆWB¶VWÖfÇ6S°¢FV×2æf÷$V6‚†gVæ7F–öâ‡B—¶6öç7B³Õ7G&–ær‡B’çFôÆ÷vW$66R‚“·W%FVÕ¶µÓÒ‡W%FVÕ¶µ×ÇÃ’³¶–b‡W%FVÕ¶µÓÃÓ2–¶VW×G'VS·Ò“°¢–b†¶VW–6VBçW6‚†b“°¢Ò“°¢B‚w7÷'E&÷w2r’æ–ææW$…DÔÃÖ6VBæÖ†cÓæf—‡GW&T6&B†b’’æ¦ö–â‚rr—ÇÆV×G’‚t–ævVâ¶öÖÖVæFR¶×W"âr—Ö6F6‚†R—²B‚w7÷'E&÷w2r’æ–ææW$…DÔÃÖV×G’‚t·VææR–¶¶RÆ7FR¶×W"âr—×Ð¦7–æ2gVæ7F–öâ6V&6…7÷'B‚—¶6öç7BÒB‚w7÷'Er’çfÇVRçG&–Ò‚“¶–b‚—&WGW&âÆöE7÷'B‚“²B‚w7÷'E&÷w2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&ÆöFW"#ãÂöF—câs·G'—¶6öç7B#Öv—B’‚rö’÷6V&6ƒ÷Òr¶Væ6öFUU$”6ö×öæVçB‡’“²B‚w7÷'E&÷w2r’æ–ææW$…DÔÃÒ‡"æf—‡GW&W7ÇÅµÒ’æÖ†cÓæf—‡GW&T6&B†b’’æ¦ö–â‚rr—ÇÆV×G’‚t–ævVâ¶×W"gVææWBâr—Ö6F6‚†R—²B‚w7÷'E&÷w2r’æ–ææW$…DÔÃÖV×G’‚u<;†¶WBfV–ÆWBâr—×Ð¦7–æ2gVæ7F–öâ6†V6´f—‡GW&R†'Fâ—¶6öç7B6&CÖ'Fâæ6Æ÷6W7B‚ræ6&Br’ÆÆÃÕ²âââB‚w7÷'E&÷w2r’çVW'•6VÆV7F÷$ÆÂ‚ræ6&Br•ÒÆ–GƒÖÆÂæ–æFW„öb†6&B“¶ÆWBFF·G'—¶6öç7B#Öv—B’‚rö’ö×•÷FV×2r“¶FFÒ‡"æf—‡GW&W7ÇÅµÒ’æf–ÇFW"‡ƒÓâ‚æ—5öf–æ—6†VB•¶–G…Ó¶–b‚FF—&WGW&ã¶'FâçFW‡D6öçFVçCÒu6¦V¶¶W.(
bs¶6öç7BƒÖv—B’‚rö’÷7÷'G5öWfVçEö6†ææVÇ2rÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’‡¶f—‡GW&S¦FFÒ—Ò“¶6&Bæ÷WFW$…DÔÃÖf—‡GW&T6&B„ö&¦V7Bæ76–vâ†FFÇ‚’“·Ö6F6‚†R—¶'FâçFW‡D6öçFVçCÒu,;‡b–v¦Vâw×Ð¦7–æ2gVæ7F–öâÆöEEb‚—¶6öç7BÒB‚wGer’çfÇVRçG&–Ò‚“²B‚wGd6†ææVÇ2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&ÆöFW"#ãÂöF—câs·G'—¶ÆWB&÷w3¶–b‡—·&÷w3Ò†v—B’‚rö’ö6†ææVÇ3÷Òr¶Væ6öFUU$”6ö×öæVçB‡’²rf6CÒr’’æ6†ææVÇ7ÇÅµ×ÖVÇ6W·&÷w3Ò†v—B’‚rö’öff÷&—FW2r’’æ6†ææVÇ7ÇÅµ×ÒB‚wGd6†ææVÇ2r’æ–ææW$…DÔÃ×&÷w2ç6Æ–6RƒÃ’æÖ†3ÓâsÆF—b6Æ73Ò&6&B#âr¶6†ææVÅ&÷r†2’²sÂöF—câr’æ¦ö–â‚rr—ÇÆV×G’‡òt–ævVâ¶æÂgVææWBâs¢t–ævVâff÷&—GF¶æÆW"âr—Ö6F6‚†R—²B‚wGd6†ææVÇ2r’æ–ææW$…DÔÃÖV×G’‚t·VææR–¶¶RÆ7FR¶æÆW"âr—×Ð¦gVæ7F–öâÆ–'&'•F"†"—¶Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚u¶FFÖÆ–%Òr’æf÷$V6‚‡ƒÓç‚æ6Æ74Æ—7BçFövvÆR‚vöârÇƒÓÓÖ"’“µ2æÆ–'&'“Ö"æFF6WBæÆ–#¶ÆöDÆ–'&'’‚—Ð¦gVæ7F–öâÖVF–F–ÆR‡‚—°¢6öç7B6–CÒ‡‚ç7G&VÕö–BÖçVÆÂbb‡‚ç7G&VÕöf÷VæBÓÖfÇ6R’“õ7G&–ær‡‚ç7G&VÕö–B“¢rs°¢6öç7Böæ6Æ–6³×6–Cò‚röæ6Æ–6³Ò'Æ’‚r´¥4ôâç7G&–æv–g’‡6–B’²r’"r“¢rs°¢6öç7B6Ç3×6–CòwF–ÆRF–ÆWÆ’s¢wF–ÆRs°¢6öç7B&FvS×6–CòsÆF—b6Æ73Ò'F–ÆV&FvR#âb3“cSC³ÂöF—câs¢rs°¢&WGW&âsÆF—b6Æ73Ò"r¶6Ç2²r"r¶öæ6Æ–6²²sâr²‡‚æ6÷fW#òsÆ–Ör7&3Ò"r¶W62‡‚æ6÷fW"’²r"ÆöF–æsÒ&Æ§’#âs¢sÆF—b7G–ÆSÒ&7V7B×&F–ó£"ó2#ãÂöF—câr’¶&FvR²sÆF—b6Æ73Ò'F—FÆR#âr¶W62‡‚ææÖWÇÇ‚ç6†÷uöæÖWÇÂrr’²sÂöF—cãÆF—b6Æ73Ò&ÖWF#âr¶W62‡‚ç–V'ÇÇ‚æW—6öFUöæÖWÇÂrr’²sÂöF—cãÂöF—câwÐ¦7–æ2gVæ7F–öâÆöDÆ–'&'’‚—·G'—¶ÆWB&÷w3¶–b…2æÆ–'&'“ÓÓÒvÖ÷f–W2r—&÷w3Ò†v—B’‚rö’öÖ÷f–Uö6FÆösö6FÆös×÷VÆ"fÆ–Ö—CÓ‚r’’æÖ÷f–W7ÇÅµÓ¶VÇ6R&÷w3Ò†v—B’‚rö’öff÷&—FW2r’’ç6†÷w7ÇÅµÓ²B‚vÆ–'&'•&÷w2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&w&–B#âr·&÷w2æÖ†ÖVF–F–ÆR’æ¦ö–â‚rr’²sÂöF—câwÇÆV×G’‚t–ævVâ–ææ†öÆBâr—Ö6F6‚†R—²B‚vÆ–'&'•&÷w2r’æ–ææW$…DÔÃÖV×G’‚t·VææR–¶¶RÆ7FR&–&Æ–÷FV¶WBâr—×Ð¦7–æ2gVæ7F–öâ6V&6„Æ–'&'’‚—¶6öç7BÒB‚vÆ–'&'•r’çfÇVRçG&–Ò‚“¶–b‚—&WGW&âÆöDÆ–'&'’‚“²B‚vÆ–'&'•&÷w2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&ÆöFW"#ãÂöF—câs·G'—¶6öç7B#Öv—B’‚…2æÆ–'&'“ÓÓÒvÖ÷f–W2sòrö’öÖ÷f–W3÷Òs¢rö’÷6†÷w3÷Òr’¶Væ6öFUU$”6ö×öæVçB‡’“¶6öç7B&÷w3Õ2æÆ–'&'“ÓÓÒvÖ÷f–W2sò‡"æÖ÷f–W7ÇÅµÒ“¢‡"ç6†÷w7ÇÅµÒ“²B‚vÆ–'&'•&÷w2r’æ–ææW$…DÔÃ×&÷w2æÆVæwFƒòsÆF—b6Æ73Ò&w&–B#âr·&÷w2æÖ†ÖVF–F–ÆR’æ¦ö–â‚rr’²sÂöF—câs¦V×G’‚t–ævVâG&Vfbâr—Ö6F6‚†R—²B‚vÆ–'&'•&÷w2r’æ–ææW$…DÔÃÖV×G’‚u<;†¶WBfV–ÆWBâr—×Ð¦7–æ2gVæ7F–öâÆöEÆ–Æ—7B‚—·G'—¶6öç7B#Öv—B’‚rö’ö6FVv÷&–W2r“²B‚v6FVv÷'•&÷w2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&6FVv÷&–W2#âr²‡"æ6FVv÷&–W7ÇÅµÒ’ç6Æ–6RƒÃS’æÖ†3ÓâsÆ'WGFöâ6Æ73Ò&6FVv÷'’"öæ6Æ–6³Ò&÷Vä6FVv÷'’‚r´¥4ôâç7G&–æv–g’†2ææÖR’²r’#ãÇ7ãâr¶W62†2ææÖR’²sÂ÷7ããÇ7ãâr¶W62†2æ6÷VçB’²sÂ÷7ããÂö'WGFöãâr’æ¦ö–â‚rr’²sÂöF—câwÖ6F6‚†R—²B‚v6FVv÷'•&÷w2r’æ–ææW$…DÔÃÖV×G’‚t·VææR–¶¶RÆ7FR¶FVv÷&–W"âr—×Ð¦7–æ2gVæ7F–öâ6V&6…Æ–Æ—7B‚—¶6öç7BÒB‚wÆ–Æ—7Er’çfÇVRçG&–Ò‚“¶–b‚—&WGW&ã²B‚wÆ–Æ—7E&÷w2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&ÆöFW"#ãÂöF—câs·G'—¶6öç7B#Öv—B’‚rö’ö6†ææVÇ3÷Òr¶Væ6öFUU$”6ö×öæVçB‡’²rf6CÒr“²B‚wÆ–Æ—7E&÷w2r’æ–ææW$…DÔÃÒ‡"æ6†ææVÇ7ÇÅµÒ’ç6Æ–6RƒÃ’æÖ†3ÓâsÆF—b6Æ73Ò&6&B#âr¶6†ææVÅ&÷r†2’²sÂöF—câr’æ¦ö–â‚rr—ÇÆV×G’‚t–ævVâG&Vfbâr—Ö6F6‚†R—²B‚wÆ–Æ—7E&÷w2r’æ–ææW$…DÔÃÖV×G’‚u<;†¶WBfV–ÆWBâr—×Ö7–æ2gVæ7F–öâ÷Vä6FVv÷'’†æÖR—²B‚wÆ–Æ—7E&÷w2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&ÆöFW"#ãÂöF—câs¶6öç7B#Öv—B’‚rö’ö6†ææVÇ3÷Òf6CÒr¶Væ6öFUU$”6ö×öæVçB†æÖR’“²B‚wÆ–Æ—7E&÷w2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò'6V7F–öæ†VB#ãÆƒ#âr¶W62†æÖR’²sÂöƒ#ãÂöF—câr²‡"æ6†ææVÇ7ÇÅµÒ’ç6Æ–6RƒÃ’æÖ†3ÓâsÆF—b6Æ73Ò&6&B#âr¶6†ææVÅ&÷r†2’²sÂöF—câr’æ¦ö–â‚rr“·67&öÆÅFòƒÃ—Ð¦7–æ2gVæ7F–öâÆöE&6–ær‚—·G'—¶6öç7B·"ÆEÓÖv—B&öÖ—6RæÆÂ…¶’‚rö’÷&6–ærr’Æ’‚rö’÷&6–æuöG&—fW'2r•Ò“¶6öç7BWfVçG3×"æWfVçG7ÇÅµÒÆG&—fW'3ÖBæG&—fW'7ÇÅµÓ²B‚w&6–æu&÷w2r’æ–ææW$…DÔÃÖWfVçG2æÖ‡ƒÓâsÆF—b6Æ73Ò&6&B#ãÆF—b6Æ73Ò'&÷r#ãÆF—b6Æ73Ò&w&÷r#ãÆF—b6Æ73Ò'F—FÆR#âr¶W62‡‚ææÖWÇÇ‚æWfVçGÇÇ‚ç6W&–W5öæÖWÇÂtÌ;‡r’²sÂöF—cãÆF—b6Æ73Ò&ÖWF#âr¶W62‡‚ç6W&–W5öæÖWÇÇ‚ç6W&–W7ÇÂrr’²r+rr¶W62†f×DFFR‡‚ç7F'B’’²sÂöF—cãÂöF—câr²‡‚æ—5öÆ—fSòsÇ7â6Æ73Ò'FrÆ—fR#äÄ•dSÂ÷7ãâs¢rr’²sÂöF—cãÂöF—câr’æ¦ö–â‚rr’²†G&—fW'2æÆVæwFƒòsÆF—b6Æ73Ò'6V7F–öæ†VB#ãÆƒ#äl;‡&W&SÂöƒ#ãÂöF—câr¶G&—fW'2æÖ‡ƒÓâsÆF—b6Æ73Ò&6&B#ãÆF—b6Æ73Ò'&÷r#âr¶'B‚rö’÷&6–æuöG&—fW%ö–ÖvSö–CÒr¶Væ6öFUU$”6ö×öæVçB‡‚æ¶W—ÇÂrr’’²sÆF—cãÆF—b6Æ73Ò'F—FÆR#âr¶W62‡‚ææÖR’²sÂöF—cãÆF—b6Æ73Ò&ÖWF#âr¶W62‡‚çFV×ÇÇ‚ç6W&–W5öæÖWÇÂrr’²sÂöF—cãÂöF—cãÂöF—cãÂöF—câr’æ¦ö–â‚rr“¢rr—ÇÆV×G’‚t–ævVâ&6–ævFFâr—Ö6F6‚†R—²B‚w&6–æu&÷w2r’æ–ææW$…DÔÃÖV×G’‚t·VææR–¶¶RÆ7FR&6–ærâr—×Ð¦7–æ2gVæ7F–öâÆöDvÖW2‚—·G'—¶6öç7B#Öv—B’‚rö’öff÷&—FW2r’Ç&÷w3×"ævÖW7ÇÅµÓ²B‚vvÖU&÷w2r’æ–ææW$…DÔÃ×&÷w2æÖ‡ƒÓâsÆF—b6Æ73Ò&6&B#ãÆF—b6Æ73Ò'&÷r#âr¶'B‡‚æ–ÖvWÇÇ‚æÆövò’²sÆF—cãÆF—b6Æ73Ò'F—FÆR#âr¶W62‡‚ææÖWÇÇ‚çF—FÆWÇÂu7–ÆÂr’²sÂöF—cãÆF—b6Æ73Ò&ÖWF#âr¶W62‡‚ç&VÆV6UöFFWÇÇ‚ç7FGW7ÇÂrr’²sÂöF—cãÂöF—cãÂöF—cãÂöF—câr’æ¦ö–â‚rr—ÇÆV×G’‚t–ævVâ7–ÆÂ’;†ç6¶VÆ—7FVââr—Ö6F6‚†R—²B‚vvÖU&÷w2r’æ–ææW$…DÔÃÖV×G’‚t·VææR–¶¶RÆ7FR7–ÆÂâr—×Ð¦7–æ2gVæ7F–öâÆöE6WGF–æw2‚—·G'—¶6öç7B3Öv—B’‚rö’ö6öæf–rr“²B‚w6WGF–æw5&÷w2r’æ–ææW$…DÔÃÒsÆF—b6Æ73Ò&6&B"7G–ÆSÒ'FF–æs£W‚#ãÆF—b6Æ73Ò&f–VÆB#ãÆÆ&VÃå&öf–ÆæfãÂöÆ&VÃãÆ–çWB–CÒ&6ftæÖR"fÇVSÒ"r¶W62†2ç&öf–ÆUöæÖWÇÂtöÆòr’²r#ãÂöF—cãÆF—b6Æ73Ò&f–VÆB#ãÆÆ&VÃå7,:V³ÂöÆ&VÃãÇ6VÆV7B–CÒ&6ftÆær#ãÆ÷F–öâfÇVSÒ&æò#äæ÷'6³Âö÷F–öããÆ÷F–öâfÇVSÒ&Vâ#äVævÆ—6ƒÂö÷F–öããÂ÷6VÆV7CãÂöF—cãÆF—b6Æ73Ò&f–VÆB#ãÆÆ&VÃãÆ–çWB–CÒ&6ftÆâ"G—SÒ&6†V6¶&÷‚"r²†2æÆÆ÷uöÆãòv6†V6¶VBs¢rr’²sâF–ÆÆBÆö¶Âv’Ôf“ÂöÆ&VÃãÂöF—cãÆ'WGFöâ6Æ73Ò'6fR"öæ6Æ–6³Ò'6fU6WGF–æw2‚’#äÆw&SÂö'WGFöããÆF—b6Æ73Ò&ÖWF"7G–ÆSÒ&Ö&v–â×F÷£7‚#åfW'6¦öâõõdU%4”ôåõóÂöF—cãÂöF—câs¶–b‚B‚v6ftÆærr’’B‚v6ftÆærr’çfÇVSÖ2ç&VfW'&VEöÆæwVvWÇÂvæòwÖ6F6‚†R—²B‚w6WGF–æw5&÷w2r’æ–ææW$…DÔÃÖV×G’‚t·VææR–¶¶RÆ7FR–æç7F–ÆÆ–ævW"âr—×Ð¦7–æ2gVæ7F–öâ6fU6WGF–æw2‚—·G'—¶6öç7BöÆCÖv—B’‚rö’ö6öæf–rr“¶öÆBç&öf–ÆUöæÖSÒB‚v6ftæÖRr’çfÇVRçG&–Ò‚—ÇÂtöÆòs¶öÆBç&VfW'&VEöÆæwVvSÒB‚v6ftÆærr’çfÇVS¶öÆBæÆÆ÷uöÆãÒB‚v6ftÆâr’æ6†V6¶VC¶6öç7B#Öv—B’‚rö’ö6öæf–rrÇ¶ÖWF†öC¢uõ5BrÆ†VFW'3§²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öâwÒÆ&öG“¤¥4ôâç7G&–æv–g’†öÆB—Ò“¶ÆW'B‡"æö³ÓÓÖfÇ6Sòt·VææR–¶¶RÆw&Râs¢tÆw&WBâr—Ö6F6‚†R—¶ÆW'B‚t·VææR–¶¶RÆw&Râr—×Ð¦gVæ7F–öâÆöE67&VVâ†–BÆf÷&6R—¶–b…2æÆöFVE¶–EÒbbf÷&6R—&WGW&ãµ2æÆöFVE¶–EÓ×G'VS¶–b†–CÓÓÒv†öÖRr–ÆöD†öÖR†f÷&6R“¶–b†–CÓÓÒwGbr–ÆöEEb‚“¶–b†–CÓÓÒw7÷'Br–ÆöE7÷'B‚“¶–b†–CÓÓÒvÆ–'&'’r–ÆöDÆ–'&'’‚“¶–b†–CÓÓÒwÆ–Æ—7Br–ÆöEÆ–Æ—7B‚“¶–b†–CÓÓÒw&6–ærr–ÆöE&6–ær‚“¶–b†–CÓÓÒvvÖW2r–ÆöDvÖW2‚“¶–b†–CÓÓÒw6WGF–æw2r–ÆöE6WGF–æw2‚—Ð¦gVæ7F–öâ&Vg&W6„7W'&VçB‚—µ2æÆöFVEµ2ç67&VVåÓÖfÇ6S¶ÆöE67&VVâ…2ç67&VVâÇG'VR—Ð¢B‚wGer’æFDWfVçDÆ—7FVæW"‚v¶W–F÷vârÆSÓç¶–b†Ræ¶W“ÓÓÒtVçFW"r–ÆöEEb‚—Ò“²B‚w7÷'Er’æFDWfVçDÆ—7FVæW"‚v¶W–F÷vârÆSÓç¶–b†Ræ¶W“ÓÓÒtVçFW"r—6V&6…7÷'B‚—Ò“²B‚wÆ–Æ—7Er’æFDWfVçDÆ—7FVæW"‚v¶W–F÷vârÆSÓç¶–b†Ræ¶W“ÓÓÒtVçFW"r—6V&6…Æ–Æ—7B‚—Ò“²B‚vÆ–'&'•r’æFDWfVçDÆ—7FVæW"‚v¶W–F÷vârÆSÓç¶–b†Ræ¶W“ÓÓÒtVçFW"r—6V&6„Æ–'&'’‚—Ò“¶ÆöD†öÖR‚“µ2æÆöFVBæ†öÖS×G'VS°£Â÷67&—CãÂö&öG“ãÂö‡FÖÃârrp ¦FVböÖö&–ÆUö'&÷w6W"‡W6W%övVçB“ ¢&WGW&â&ööÂ‡&Rç6V&6‚€¢"&æG&ö–GÆ—†öæWÆ—öGÇv–æF÷w2†öæWÆÖö&–ÆWÆ÷W&Ö–æ—Æ–VÖö&–ÆR"À¢7G"‡W6W%övVçB÷"""’ÂfÆw3×&Rä”täõ$T44R’ ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÐ¢2&WVW7B†æFÆW ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÐ ¥ôÄ5Eô5D•d•E’ÒF–ÖRæÖöæ÷Föæ–2‚¥ô5D•d•E•ôÄô4²ÒF‡&VF–æräÆö6²‚ ¦FVbFW7EöW‡FW&æÅ÷6÷W&6R†¶W’“ ¢""%'VâöæRg&W6‚†VÇF‚&ö&RæB&WGW&âv†WF†W"—Bv2Æ–6&ÆRâ"" ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢6–BÒ7G"†6frævWB‚'7FVÕ÷v—6†Æ—7Eö–B"’÷"""’ç7G&—‚¢&ö&W2Ò°¢&f÷FÖö"#¢ÆÖ&F¢‡GGövWEö§6öâ„dõDÔô%ôD”Å•ôÔD4„U2æf÷&ÖB€¢FFS×F–ÖRç7G&gF–ÖR‚"U’VÒVB"ÂF–ÖRæÆö6ÇF–ÖR‚’’’ÂF–ÖV÷WCÓR’À¢&ÇGb#¢ÆÖ&F¢fWF6…öÇGeöF–Ç’†FFWF–ÖRæFFRçFöF’‚’æ—6öf÷&ÖB‚’’À¢'fu÷GfwV–FR#¢ÆÖ&F¢fWF6…÷fuö6†ææVÅö6FÆör‚’À¢'GfÖ¦R#¢ÆÖ&F¢÷GfÖ¦UöW—6öFU÷66†VGVÆR‚$'&V¶–ær&B"Âf÷&6SÕG'VR’À¢&6–æVÖWF#¢ÆÖ&F¢‡GGövWEö§6öâ€¢&‡GG3¢ò÷c2Ö6–æVÖWFç7G&VÒæ–òö6FÆöröÖ÷f–R÷F÷÷6V&6ƒÖÖG&—‚æ§6öâ"ÂF–ÖV÷WCÓR’À¢&c#¢ÆÖ&F¢vWEöc÷66†VGVÆR†f÷&6SÕG'VR’À¢&c"#¢ÆÖ&F¢vWEöf–÷&6–æu÷vVV¶VæG2‚&c""Âf÷&6SÕG'VR’À¢&c2#¢ÆÖ&F¢vWEöf–÷&6–æu÷vVV¶VæG2‚&c2"Âf÷&6SÕG'VR’À¢&–æG–6"#¢ÆÖ&F¢vWEö–æG–6%÷66†VGVÆR†f÷&6SÕG'VR’À¢'w&2#¢ÆÖ&F¢vWE÷w&5÷66†VGVÆR†f÷&6SÕG'VR’À¢&f÷&×VÆR#¢ÆÖ&F¢vWEöf÷&×VÆU÷66†VGVÆR†f÷&6SÕG'VR’À¢'vV2#¢ÆÖ&F¢vWE÷vV5÷66†VGVÆR†f÷&6SÕG'VR’À¢&Ö÷Föw#¢ÆÖ&F¢vWEöÖ÷Föw÷66†VGVÆR†f÷&6SÕG'VR’À¢Ð¢–b¶W’ÓÒ'‡G&VÒ# ¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢÷&V6÷&E÷6÷W&6R†¶W’ÂæöæRÂW'&÷#Ò$æ÷B6öæf–wW&VB"¢&WGW&â²&¶W’#¢¶W’Â'6¶—VB#¢G'VWÐ¢&ö&RÒÆÖ&F¢‚æÆöv–â‚¢VÆ–b¶W’ÓÒ&Wu÷†ÖÇGb# ¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢÷&V6÷&E÷6÷W&6R†¶W’ÂæöæRÂW'&÷#Ò$æ÷B6öæf–wW&VB"¢&WGW&â²&¶W’#¢¶W’Â'6¶—VB#¢G'VWÐ¢&ö&RÒÆÖ&F¢&ö&U÷†ÖÇGb‡‚¢VÆ–b¶W’ÓÒ'7FVÒ# ¢–bæ÷B&RægVÆÆÖF6‚‡"%ÆG³wÒ"Â6–B“ ¢÷&V6÷&E÷6÷W&6R†¶W’ÂæöæRÂW'&÷#Ò$æ÷B6öæf–wW&VB"¢&WGW&â²&¶W’#¢¶W’Â'6¶—VB#¢G'VWÐ¢&ö&RÒÆÖ&F¢7FVÕ÷V&Æ–5÷&öf–ÆR‡6–BÂf÷&6SÕG'VR¢VÇ6S ¢&ö&RÒ&ö&W2ævWB†¶W’¢–bæ÷B&ö&S ¢÷&V6÷&E÷6÷W&6R†¶W’ÂæöæRÂW'&÷#Ò$æ÷Bf–Æ&ÆR"¢&WGW&â²&¶W’#¢¶W’Â'6¶—VB#¢G'VWÐ¢7F'FVBÒF–ÖRçW&eö6÷VçFW"‚¢G'“ ¢&W7VÇBÒ&ö&R‚¢–b¶W’ÓÒ'‡G&VÒ# ¢ö²ÂFWF–ÂÒ&W7VÇ@¢–bæ÷Bö³ ¢&—6R'VçF–ÖTW'&÷"†FWF–Â÷"&Æöv–âf–ÆVB"¢6÷VçBÒæöæP¢VÇ6S ¢6÷VçBÒÆVâ‡&W7VÇB’–b—6–ç7Fæ6R‡&W7VÇBÂ†Æ—7BÂGWÆRÂF–7B’’VÇ6RæöæP¢ÆFVæ7’Ò–çB‚‡F–ÖRçW&eö6÷VçFW"‚’Ò7F'FVB’¢¢÷&V6÷&E÷6÷W&6R†¶W’ÂG'VRÂ6÷VçCÖ6÷VçBÂÆFVæ7•ö×3ÖÆFVæ7’¢&WGW&â²&¶W’#¢¶W’Â&ö²#¢G'VRÂ&6÷VçB#¢6÷VçBÂ&ÆFVæ7•ö×2#¢ÆFVæ7—Ð¢W†6WBW†6WF–öâ2S ¢ÆFVæ7’Ò–çB‚‡F–ÖRçW&eö6÷VçFW"‚’Ò7F'FVB’¢¢÷&V6÷&E÷6÷W&6R†¶W’ÂfÇ6RÂW'&÷#ÖRÂÆFVæ7•ö×3ÖÆFVæ7’¢&WGW&â²&¶W’#¢¶W’Â&ö²#¢fÇ6RÂ&W'&÷"#¢7G"†R•³£#ÒÂ&ÆFVæ7•ö×2#¢ÆFVæ7—Ð ¦FVböÖ&µöö7F—f—G’‚“ ¢vÆö&ÂôÄ5Eô5D•d•E¢v—F‚ô5D•d•E•ôÄô4³ ¢ôÄ5Eô5D•d•E’ÒF–ÖRæÖöæ÷Föæ–2‚ ¦FVbö–æ7F—fU÷6V6öæG2‚“ ¢v—F‚ô5D•d•E•ôÄô4³ ¢&WGW&âÖ‚ƒãÂF–ÖRæÖöæ÷Föæ–2‚’ÒôÄ5Eô5D•d•E’ ¦6Æ72†æFÆW"„&6T…EE&WVW7D†æFÆW"“ ¢FVbÆöuöÖW76vR‡6VÆbÂ¦“ ¢70 ¢FVb÷6VæB‡6VÆbÂ6öFRÂ&öG’Â7G—SÒ&Æ–6F–öâö§6öâ"Â†VFW'3ÔæöæR“ ¢–b—6–ç7Fæ6R†&öG’Â†F–7BÂÆ—7B’“ ¢&öG’Ò§6öâæGV×2†&öG’¢FFÒ&öG’æVæ6öFR‚'WFbÓ‚"¢G'“ ¢6VÆbç6VæE÷&W7öç6R†6öFR¢6VÆbç6VæEö†VFW"‚$6öçFVçBÕG—R"Â7G—R²#²6†'6WC×WFbÓ‚"¢6VÆbç6VæEö†VFW"‚$66†RÔ6öçG&öÂ"Â&æò×7F÷&R"¢f÷"æÖRÂfÇVR–â††VFW'2÷"·Ò’æ—FV×2‚“ ¢6VÆbç6VæEö†VFW"†æÖRÂfÇVR¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔÆVæwF‚"Â7G"†ÆVâ†FF’’¢6VÆbæVæEö†VFW'2‚¢6VÆbçvf–ÆRçw&—FR†FF¢W†6WB„'&ö¶Vå—TW'&÷"Â6öææV7F–öä&÷'FVDW'&÷"Â6öææV7F–öå&W6WDW'&÷"“ ¢2'&÷w6W'2&÷WF–æVÇ’6æ6VÂö'6öÆWFR’&WVW7G2GW&–ær&VÆöG2À¢2æf–vF–öâæBWFFW2âF†R&W7öç6R†2æ÷v†W&RFòvòà¢&WGW&à ¢FVbö—5öÆö÷&6²‡6VÆb“ ¢&WGW&â7G"‡6VÆbæ6Æ–VçEöFG&W75³Ò’–â‚##rããã"Â#££" ¢FVbö—5÷&—fFU÷&VÖ÷FUöÆ—7FVæW"‡6VÆb“ ¢&WGW&âö—5÷F–Ç66ÆUö—cB‡7G"‡6VÆbç6W'fW"ç6W'fW%öFG&W75³Ò’ ¢FVböWF†÷&—¦UöÆâ‡6VÆbÂ'6VB“ ¢""$WF†÷&—¦R&VÖ÷FRÄâ'&÷w6W'3²Æö6Æ†÷7B&VÖ–ç277v÷&FÆW72â"" ¢–b6VÆbåö—5öÆö÷&6²‚“ ¢&WGW&âG'VP¢6frÒÆöEö6öæf–r‚¢W‡V7FVBÒ7G"†6frævWB‚&Æåö66W75÷Fö¶Vâ"’÷"""¢–bæ÷B†6frævWB‚&ÆÆ÷uöÆâ"’÷"6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"’’÷"æ÷BW‡V7FVC ¢6VÆbå÷6VæBƒC2Â²&W'&÷"#¢%&—fFRFWf–6R66W72—2F—6&ÆVB'Ò¢&WGW&âfÇ6P¢7WÆ–VBÒW&ÆÆ–"ç'6Rç'6U÷2‡'6VBçVW'’’ævWB‚'Fö¶Vâ"Â²"%Ò•³Ð¢6öö¶–W2Ò·Ð¢f÷"'B–â7G"‡6VÆbæ†VFW'2ævWB‚$6öö¶–R"’÷"""’ç7Æ—B‚#²"“ ¢–b#Ò"–â'C ¢æÖRÂfÇVRÒ'Bç7G&—‚’ç7Æ—B‚#Ò"Â¢6öö¶–W5¶æÖUÒÒfÇVP¢fÆ–E÷VW'’Ò&ööÂ‡7WÆ–VB’æB÷6V7W&UöWVÂ‡7WÆ–VBÂW‡V7FVB¢fÆ–Eö6öö¶–RÒ&ööÂ†6öö¶–W2ævWB‚'GfÖFUöÆâ"’’æB÷6V7W&UöWVÂ†6öö¶–W5²'GfÖFUöÆâ%ÒÂW‡V7FVB¢–bfÆ–E÷VW'“ ¢6VÆbå÷6VæBƒ3"Â""Â'FW‡B÷Æ–â"Â°¢$Æö6F–öâ#¢'6VBçF‚÷""ò"À¢%6WBÔ6öö¶–R#¢'GfÖFUöÆãÒ"²W‡V7FVB²#²FƒÒó²‡GGöæÇ“²6ÖU6—FSÕ7G&–7B"À¢Ò¢&WGW&âfÇ6P¢–bfÆ–Eö6öö¶–S ¢&WGW&âG'VP¢6VÆbå÷6VæBƒCÂ²&W'&÷"#¢$÷VâEdÖFRW6–ærF†R&—fFR†öæRÆ–æ²6†÷vâ–â6WGF–æw2'Ò¢&WGW&âfÇ6P ¢FVb÷6VæEö–ÖvUöf–ÆR‡6VÆbÂF‚Â7G—SÔæöæRÂ66†Uö6öçG&öÃÒ'V&Æ–2ÂÖ‚ÖvSÓ3S3cÂ–Ö×WF&ÆR"“ ¢v—F‚÷Vâ‡F‚Â'&""’2c ¢&rÒbç&VB‚¢7G—RÒ7G—R÷"ö–ÖvUö6öçFVçE÷G—R‡&r’÷"&–ÖvRö§Vr ¢6VÆbç6VæE÷&W7öç6Rƒ#¢6VÆbç6VæEö†VFW"‚$6öçFVçBÕG—R"Â7G—R¢6VÆbç6VæEö†VFW"‚$66†RÔ6öçG&öÂ"Â66†Uö6öçG&öÂ¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔÆVæwF‚"Â7G"†ÆVâ‡&r’’¢6VÆbæVæEö†VFW'2‚¢6VÆbçvf–ÆRçw&—FR‡&r ¢FVbövWE÷&6–æuö’‡6VÆbÂF‚Â“ ¢–bF‚ÓÒ"ö’öc÷66†VGVÆR# ¢&WGW&â6VÆbå÷6VæBƒ#Â²&WfVçG2#¢vWEöc÷66†VGVÆR‚—Ò¢–bF‚ÓÒ"ö’÷&6–ær# ¢6frÒÆöEö6öæf–r‚¢6VÆV7FVBÒ¶¶W’f÷"¶W’–â6frævWB‚'&6–æu÷6W&–W2"Â²&c%Ò¢–b¶W’–â‚&c"Â&c""Â&c2"Â&–æG–6""Â'vV2"Â&f÷&×VÆR"Â&Ö÷Föw"Â'w&2"•Ð¢&WGW&â6VÆbå÷6VæBƒ#Â²'6VÆV7FVB#¢6VÆV7FVBÂ&WfVçG2#¢vWE÷&6–æuöWfVçG2‡6VÆV7FVB—Ò¢–bF‚ÓÒ"ö’÷&6–æuöf–Æ&–Æ—G’# ¢6frÒÆöEö6öæf–r‚“²‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&f–Æ&–Æ—G’#¢·ÒÂ&ÆövvVEö–â#¢fÇ6WÒ¢6VÆV7FVBÒ¶¶W’f÷"¶W’–â6frævWB‚'&6–æu÷6W&–W2"Â²&c%Ò¢–b¶W’–âõ$4”äuô4„ääTÅõDU$Õ5Ð¢66†Uö¶W’Ò÷föEö66†Uö¶W’‡‚’²'Â"²"Â"æ¦ö–â‡6VÆV7FVB¢66†VBÒõ$4”äuôd”Ä$”Ä•E•ô44„P¢–b†66†VBævWB‚&¶W’"’ÓÒ66†Uö¶W’æ@¢F–ÖRçF–ÖR‚’ÒfÆöB†66†VBævWB‚'G2"’÷"’Âõ$4”äuôd”Ä$”Ä•E•õEDÂ“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&f–Æ&–Æ—G’#¢66†VBævWB‚&f–Æ&–Æ—G’"’÷"·ÒÀ¢&ÆövvVEö–â#¢G'VWÒ¢WfVçG2ÒvWE÷&6–æuöWfVçG2‡6VÆV7FVB¢G'“ ¢6†ææVÇ2Â6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢W†6WBW†6WF–öã ¢&WGW&â6VÆbå÷6VæBƒ#Â²&f–Æ&–Æ—G’#¢·ÒÂ&ÆövvVEö–â#¢G'VWÒ¢G'“ ¢föÆÆ÷vVEöG&—fW'2ÒvWE÷&6–æuöG&—fW'2‚¢W†6WBW†6WF–öã ¢föÆÆ÷vVEöG&—fW'2ÒµÐ¢æ÷rÒF–ÖRçF–ÖR‚“²f–Æ&–Æ—G’Ò·Ð¢f÷"WfVçB–âWfVçG3 ¢G'“ ¢WG2ÒFFWF–ÖRæFFWF–ÖRæg&öÖ—6öf÷&ÖB‡7G"†WfVçBævWB‚'7F'B"’÷"""’ç&WÆ6R‚%¢"Â"³£"’’çF–ÖW7F×‚¢W†6WBW†6WF–öã ¢6öçF–çVP¢–bWG2Âæ÷rÒ"¢3c÷"WG2âæ÷r²CR¢#B¢3c ¢6öçF–çVP¢†—G2Òf–æE÷&6–æuö6†ææVÇ2†WfVçBÂ6†ææVÇ2Â6G2Â‚ÂföÆÆ÷vVEöG&—fW'2¢–b†—G3 ¢f–Æ&–Æ—G•µ÷&6–æuöWfVçEö¶W’†WfVçB•ÒÒ†—G0¢õ$4”äuôd”Ä$”Ä•E•ô44„RçWFFR‡²&¶W’#¢66†Uö¶W’Â'G2#¢F–ÖRçF–ÖR‚’À¢&f–Æ&–Æ—G’#¢f–Æ&–Æ—G—Ò¢&WGW&â6VÆbå÷6VæBƒ#Â²&f–Æ&–Æ—G’#¢f–Æ&–Æ—G’Â&ÆövvVEö–â#¢G'VWÒ¢–bF‚ÓÒ"ö’÷&6–æuöG&—fW'2# ¢&WGW&â6VÆbå÷6VæBƒ#Â²&G&—fW'2#¢vWE÷&6–æuöG&—fW'2‚—Ò¢–bF‚ÓÒ"ö’÷&6–æuöG&—fW%ö–ÖvR# ¢¶W’Ò‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷B&RægVÆÆÖF6‚‡"%³Ó”Õ¦×¥òÕÒ²"Â¶W’“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&BG&—fW"–B'Ò¢G&—fW"ÒæW‡B‚‡&÷rf÷"&÷r–âvWE÷&6–æuöG&—fW'2‚’–b&÷rævWB‚&¶W’"’ÓÒ¶W’’ÂæöæR¢–ÖvU÷F‚Òö66†U÷&6–æuöG&—fW%÷–7GW&R†G&—fW"÷"·Ò’–bG&—fW"VÇ6R" ¢–bæ÷B–ÖvU÷Fƒ ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&G&—fW"–ÖvRæ÷Bf÷VæB'Ò¢&WGW&â6VÆbå÷6VæEö–ÖvUöf–ÆR†–ÖvU÷F‚¢–bF‚ÓÒ"ö’öc÷FV×2# ¢&WGW&â6VÆbå÷6VæBƒ#Â²'FV×2#¢vWEöc÷FV×2‚’À¢&ff÷&—FW2#¢ÆöEöff÷&—FW2‚’ævWB‚&c÷FV×2"ÂµÒ—Ò¢–bF‚ÓÒ"ö’öc÷FVÕöÆövò# ¢6öç7G'V7F÷%ö–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷B&RægVÆÆÖF6‚‡"%³Ó”Õ¦×¥òÕÒ²"Â6öç7G'V7F÷%ö–B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B6öç7G'V7F÷"–B'Ò¢–ÖvU÷F‚Òö66†UöcöÆövò†6öç7G'V7F÷%ö–B¢–bæ÷B–ÖvU÷Fƒ ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢$cFVÒÆövòæ÷Bf÷VæB'Ò¢&WGW&â6VÆbå÷6VæEö–ÖvUöf–ÆR†–ÖvU÷F‚ ¢FVbFõôtUB‡6VÆb“ ¢RÒW&ÆÆ–"ç'6RçW&Ç'6R‡6VÆbçF‚¢ÒW&ÆÆ–"ç'6Rç'6U÷2‡RçVW'’¢&VÆ•÷F&vWBÒ" ¢–bRçF‚ÓÒ"ö’÷&VÆ’# ¢&VÆ•÷F&vWBÒ÷&VÆ•÷F&vWB‡ævWB‚'B"Â²"%Ò•³Ò¢–bæ÷B&VÆ•÷F&vWB÷"æ÷B÷6fU÷&VÆ•÷F&vWB‡&VÆ•÷F&vWB“ ¢&WGW&â6VÆbå÷6VæBƒC2Â²&W'&÷"#¢&–çfÆ–B÷"W‡—&VB&VÆ’Fö¶Vâ'Ò¢VÆ–bæ÷B6VÆbåöWF†÷&—¦UöÆâ‡R“ ¢&WGW&à¢G'“ ¢–bRçF‚–â‚"ò"Â"ö–æFW‚æ‡FÖÂ"“ ¢6öö¶–RÒ7G"‡6VÆbæ†VFW'2ævWB‚$6öö¶–R"’÷"""¢FW6·F÷ö÷fW'&–FRÒ&ööÂ‡&Rç6V&6‚‡""ƒó¥çÃµÇ2¢—GfÖFU÷f–WsÖFW6·F÷ƒó£·ÂB’"Â6öö¶–R’¢–böÖö&–ÆUö'&÷w6W"‡6VÆbæ†VFW'2ævWB‚%W6W"ÔvVçB"’’æBæ÷BFW6·F÷ö÷fW'&–FS ¢&WGW&â6VÆbå÷6VæBƒ3"Â""Â'FW‡B÷Æ–â"Â²$Æö6F–öâ#¢"öÖö&–ÆR'Ò¢&WGW&â6VÆbå÷6VæBƒ#ÂtRç&WÆ6R‚%õõdU%4”ôåõò"ÂdU%4”ôâ’Â'FW‡Bö‡FÖÂ" ¢–bRçF‚ÓÒ"öÖö&–ÆR# ¢&WGW&â6VÆbå÷6VæBƒ#ÂÔô$”ÄUõtRç&WÆ6R‚%õõdU%4”ôåõò"ÂdU%4”ôâ’Â'FW‡Bö‡FÖÂ"Â°¢%6WBÔ6öö¶–R#¢'GfÖFU÷f–WsÖÖö&–ÆS²FƒÒó²6ÖU6—FSÕ7G&–7B"À¢Ò ¢–bRçF‚ÓÒ"öFW6·F÷# ¢&WGW&â6VÆbå÷6VæBƒ#ÂtRç&WÆ6R‚%õõdU%4”ôåõò"ÂdU%4”ôâ’Â'FW‡Bö‡FÖÂ"Â°¢%6WBÔ6öö¶–R#¢'GfÖFU÷f–WsÖFW6·F÷²FƒÒó²6ÖU6—FSÕ7G&–7B"À¢Ò ¢–bRçF‚–â²"ö’öc÷66†VGVÆR"Â"ö’÷&6–ær"Â"ö’÷&6–æuöf–Æ&–Æ—G’"À¢"ö’÷&6–æuöG&—fW'2"Â"ö’÷&6–æuöG&—fW%ö–ÖvR"À¢"ö’öc÷FV×2"Â"ö’öc÷FVÕöÆövò'Ó ¢&WGW&â6VÆbåövWE÷&6–æuö’‡RçF‚Â ¢–bRçF‚ÓÒ"ö’÷FVÕöÆövò# ¢FVÕö–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷BFVÕö–Bæ—6F–v—B‚“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&BFVÒ–B'Ò¢–bæ÷Bö66†U÷FVÕöÆövò‡FVÕö–B“ ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢'FVÒÆövòæ÷Bf÷VæB'Ò¢&WGW&â6VÆbå÷6VæEö–ÖvUöf–ÆR…÷FVÕöÆövõ÷F‚‡FVÕö–B’Â&–ÖvR÷ær" ¢–bRçF‚ÓÒ"ö’öÆVwVUöÆövò# ¢ÆVwVUö–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷BÆVwVUö–Bæ—6F–v—B‚“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&BÆVwVR–B'Ò¢–bæ÷Bö66†UöÆVwVUöÆövò†ÆVwVUö–B“ ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&ÆVwVRÆövòæ÷Bf÷VæB'Ò¢&WGW&â6VÆbå÷6VæEö–ÖvUöf–ÆR…öÆVwVUöÆövõ÷F‚†ÆVwVUö–B’Â&–ÖvR÷ær" ¢–bRçF‚ÓÒ"ö’ö6†ææVÅöÆövò# ¢7G&VÕö–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷B&RægVÆÆÖF6‚‡"%³Ó”Õ¦×¥òÕÒ²"Â7G&VÕö–B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B7G&VÒ–B'Ò¢–6öå÷W&ÂÒ÷7G&VÕö–6öåöf÷%ö–B‡7G&VÕö–B¢&÷f–FW"Ò÷föEö66†Uö¶W’…‡G&VÒ†ÆöEö6öæf–r‚’’¢F‚Òö66†Uö6†ææVÅöÆövò‡7G&VÕö–BÂ–6öå÷W&ÂÂ&÷f–FW"¢–bæ÷BFƒ ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&6†ææVÂÆövòæ÷Bf÷VæB'Ò¢v—F‚÷Vâ‡F‚Â'&""’2c ¢&rÒbç&VB‚¢7G—RÒö–ÖvUö6öçFVçE÷G—R‡&r¢–bæ÷B7G—S ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&–çfÆ–B6†ææVÂÆövò'Ò¢6VÆbç6VæE÷&W7öç6Rƒ#¢6VÆbç6VæEö†VFW"‚$6öçFVçBÕG—R"Â7G—R¢6VÆbç6VæEö†VFW"‚$66†RÔ6öçG&öÂ"Â'V&Æ–2ÂÖ‚ÖvSÓ3S3cÂ–Ö×WF&ÆR"¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔÆVæwF‚"Â7G"†ÆVâ‡&r’’¢6VÆbæVæEö†VFW'2‚¢6VÆbçvf–ÆRçw&—FR‡&r¢&WGW&à ¢–bRçF‚ÓÒ"ö’÷7FVÕöfF"# ¢7FVÕö–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷B&RægVÆÆÖF6‚‡"%ÆG³wÒ"Â7FVÕö–B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B7FVÒ–B'Ò¢F‚Ò÷7FVÕöfF%÷F‚‡7FVÕö–B¢–bæ÷BF‚÷"æ÷B÷2çF‚æ—6f–ÆR‡F‚“ ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&fF"æ÷B66†VB'Ò¢v—F‚÷Vâ‡F‚Â'&""’2c ¢&rÒbç&VBƒ"¢#B¢#B²¢7G—RÒö–ÖvUö6öçFVçE÷G—R‡&r¢–bæ÷B7G—R÷"ÆVâ‡&r’â"¢#B¢#C ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&–çfÆ–BfF"'Ò¢6VÆbç6VæE÷&W7öç6Rƒ#¢6VÆbç6VæEö†VFW"‚$6öçFVçBÕG—R"Â7G—R¢6VÆbç6VæEö†VFW"‚$66†RÔ6öçG&öÂ"Â'V&Æ–2ÂÖ‚ÖvSÓƒcC"¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔÆVæwF‚"Â7G"†ÆVâ‡&r’’¢6VÆbæVæEö†VFW'2‚¢6VÆbçvf–ÆRçw&—FR‡&r¢&WGW&à ¢–bRçF‚ÓÒ"ö’÷6V6öåö'B# ¢6†÷rÒ‡ævWB‚'6†÷r"Â²"%Ò•³Ò’ç7G&—‚’æÆ÷vW"‚¢6V6öâÒ‡ævWB‚'6V6öâ"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷B&RægVÆÆÖF6‚‡"%¶ÖcÓ•×³gÒ"Â6†÷r’÷"æ÷B&RægVÆÆÖF6‚‡"%ÆB²"Â6V6öâ“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B'Gv÷&²F‚'Ò¢F‚Ò÷2çF‚æ¦ö–â†öF—"‚’Â&'Gv÷&²"Â'GfÖ¦RÒ"²6†÷rÀ¢'6V6öâÒ"²6V6öâ²"æ§r"¢–bæ÷B÷2çF‚æ—6f–ÆR‡F‚“ ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&'Gv÷&²æ÷Bf÷VæB'Ò¢&WGW&â6VÆbå÷6VæEö–ÖvUöf–ÆR‡F‚Â&–ÖvRö§Vr" ¢–bRçF‚ÓÒ"ö’÷7FGW2# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢6÷VçBÒÆVâ…õ…Eô44„U²&6†ææVÇ2%Ò’–b‡‚æ6öæf–wW&VB‚’æBõ…Eô44„U²&6†ææVÇ2%Ò’VÇ6RæöæP¢&WGW&â6VÆbå÷6VæBƒ#Â²&6öæf–wW&VB#¢‚æ6öæf–wW&VB‚’Â&6†ææVÅö6÷VçB#¢6÷VçBÀ¢&ÖF6…÷F‡&W6†öÆB#¢6frævWB‚&ÖF6…÷F‡&W6†öÆB"Âãc"—Ò ¢–bRçF‚ÓÒ"ö’÷6÷W&6Uö†VÇF‚# ¢&WGW&â6VÆbå÷6VæBƒ#Â²'6÷W&6W2#¢6÷W&6Uö†VÇF…÷6æ6†÷B‚’À¢&æ÷r#¢F–ÖRçF–ÖR‚—Ò ¢–bRçF‚ÓÒ"ö’öff÷&—FW2# ¢fbÒÆöEöff÷&—FW2‚¢2Vç&–6‚6†ææVÂff÷&—FW2v—F‚g&W6‚7G&VÒU$À¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢6†ç2ÒµÐ¢f÷"2–âfbævWB‚&6†ææVÇ2"ÂµÒ“ ¢6–BÒ2ævWB‚'7G&VÕö–B"¢6†ç2æVæB‡°¢'7G&VÕö–B#¢6–BÀ¢&æÖR#¢2ævWB‚&æÖR"Â""’À¢&6FVv÷'’#¢2ævWB‚&6FVv÷'’"Â""’À¢&Æövò#¢2ævWB‚&Æövò"’÷"÷7G&VÕö–6öåöf÷%ö–B‡6–B’À¢'W&Â#¢‚ç7G&VÕ÷W&Â‡6–B’–b‡‚æ6öæf–wW&VB‚’æB6–B—2æ÷BæöæR’VÇ6R""À¢Ò¢ff÷&—FU÷6†÷w2ÒµÐ¢f÷"6†÷r–âfbævWB‚'6†÷w2"ÂµÒ“ ¢—FVÒÒF–7B‡6†÷r¢—FVÕ²&æÖR%ÒÒö6ÆVå÷6†÷u÷F—FÆR†—FVÒævWB‚&æÖR"’’÷"—FVÒævWB‚&æÖR"Â""¢—FVÕ²'6†÷uö¶W’%ÒÒ—FVÒævWB‚'6†÷uö¶W’"’÷"÷6†÷uö¶W’†—FVÒævWB‚&æÖR"’’÷"7G"†—FVÒævWB‚'6W&–W5ö–B"Â""’¢—FVÕ²'6W&–W5ö–G2%ÒÒ·6–Bf÷"6–B–â†—FVÒævWB‚'6W&–W5ö–G2"’÷"¶—FVÒævWB‚'6W&–W5ö–B"•Ò’–b6–B—2æ÷BæöæUÐ¢ff÷&—FU÷6†÷w2æVæB†—FVÒ¢6VÆV7FVEö–G2Ò·7G"‡6–B’f÷"6–B–âfbævWB‚&×–Æ—7Eö6†ææVÇ2"ÂµÒ•Õ³£UÐ¢f–Æ&ÆUö–G2Ò·7G"†2ævWB‚'7G&VÕö–B"’’f÷"2–âfbævWB‚&6†ææVÇ2"ÂµÒ—Ð¢6VÆV7FVEö–G2Ò·6–Bf÷"6–B–â6VÆV7FVEö–G2–b6–B–âf–Æ&ÆUö–G5Ð¢&WGW&â6VÆbå÷6VæBƒ#Â²&6FVv÷&–W2#¢fbævWB‚&6FVv÷&–W2"ÂµÒ’À¢&6†ææVÇ2#¢6†ç2À¢&Ö÷f–W2#¢fbævWB‚&Ö÷f–W2"ÂµÒ’À¢'6†÷w2#¢ff÷&—FU÷6†÷w2À¢&vÖW2#¢fbævWB‚&vÖW2"ÂµÒ’À¢'FV×2#¢fbævWB‚'FV×2"ÂµÒ’À¢&c÷FV×2#¢fbævWB‚&c÷FV×2"ÂµÒ’À¢&×–Æ—7Eö6†ææVÇ2#¢6VÆV7FVEö–G7Ò ¢–bRçF‚ÓÒ"ö’öWu÷F&vWG2# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢%‡G&VÒ—2æ÷B6öæf–wW&VB'Ò¢fbÒÆöEöff÷&—FW2‚¢vçFVEö6FVv÷&–W2Ò6WB‡7G"†æÖR’f÷"æÖR–âfbævWB‚&6FVv÷&–W2"ÂµÒ’¢–G2Ò·7G"†6‚ævWB‚'7G&VÕö–B"’’f÷"6‚–âfbævWB‚&6†ææVÇ2"ÂµÒ¢–b6‚ævWB‚'7G&VÕö–B"’—2æ÷BæöæUÐ¢–bvçFVEö6FVv÷&–W3 ¢G'“ ¢6†ææVÇ2Â6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢–G2æW‡FVæB‡7G"†6‚ævWB‚'7G&VÕö–B"’’f÷"6‚–â6†ææVÇ0¢–b6‚ævWB‚'7G&VÕö–B"’—2æ÷BæöæRæ@¢6G2ævWB†6‚ævWB‚&6FVv÷'•ö–B"’Â""’–âvçFVEö6FVv÷&–W2¢W†6WBW†6WF–öã ¢70¢&WGW&â6VÆbå÷6VæBƒ#Â²&–G2#¢Æ—7B†F–7Bæg&öÖ¶W—2†–G2’—Ò ¢–bRçF‚ÓÒ"ö’öWr# ¢2–G3Ö6öÖÖ×6W&FVB7G&VÒ–G3²f÷&6SÓ'—76W266†Rà¢266†VCÓ—2F—6²öÖVÖ÷'’öæÇ’æBæWfW"6öçF7G2F†R&÷f–FW"à¢–G5÷&rÒ‡ævWB‚&–G2"Â²"%Ò•³Ò’ç7G&—‚¢f÷&6RÒævWB‚&f÷&6R"Â²#%Ò•³ÒÓÒ# ¢66†VEööæÇ’ÒævWB‚&66†VB"Â²#%Ò•³ÒÓÒ# ¢ÆÅöff÷&—FW2ÒævWB‚&ff÷&—FW2"Â²#%Ò•³ÒÓÒ# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚’÷"†æ÷B–G5÷&ræBæ÷BÆÅöff÷&—FW2“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B&WVW7B'Ò¢öÆöEöWuöF—6µö66†R‡‚¢–G2Ò·2ç7G&—‚’f÷"2–â–G5÷&rç7Æ—B‚"Â"’–b2ç7G&—‚•Ð¢–bÆÅöff÷&—FW3 ¢fbÒÆöEöff÷&—FW2‚¢vçFVEö6FVv÷&–W2Ò6WB‡7G"†æÖR’f÷"æÖR–âfbævWB‚&6FVv÷&–W2"ÂµÒ’¢–G2æW‡FVæB‡7G"†6‚ævWB‚'7G&VÕö–B"’’f÷"6‚–âfbævWB‚&6†ææVÇ2"ÂµÒ¢–b6‚ævWB‚'7G&VÕö–B"’—2æ÷BæöæR¢–bvçFVEö6FVv÷&–W3 ¢G'“ ¢6†ææVÇ2Â6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢–G2æW‡FVæB‡7G"†6‚ævWB‚'7G&VÕö–B"’’f÷"6‚–â6†ææVÇ0¢–b6‚ævWB‚'7G&VÕö–B"’—2æ÷BæöæRæ@¢6G2ævWB†6‚ævWB‚&6FVv÷'•ö–B"’Â""’–âvçFVEö6FVv÷&–W2¢W†6WBW†6WF–öã ¢70¢27F&ÆRFRÖGWÆ–6F–öâÖGFW'2v†Vâ6†ææVÂ—2F—&V7FÇ¢2ff÷&—FVBæBÇ6ò&VÆöæw2Fòff÷&—FR6FVv÷'’à¢–G2ÒÆ—7B†F–7Bæg&öÖ¶W—2†–G2’¢æ÷rÒF–ÖRçF–ÖR‚¢&W7VÇBÒ·Ð¢FõöfWF6‚ÒµÐ¢7FG2Ò²'WFFVB#¢Â'†ÖÇGeöf–ÆÆVB#¢Â&fÆÆ&6µ÷WFFVB#¢À¢&æõöFF#¢Â&f–ÆVB#¢Ð¢66†Uö6†ævVBÒfÇ6P¢f÷"6–B–â–G3 ¢66†VBÒôUuô44„RævWB‡6–B¢–b66†VBæB66†VEööæÇ“ ¢266†VBÖöæÇ’æf–vF–öâæWfW"6öçF7G2F†R&÷f–FW"â&WF–æV@¢2wV–FRFF&VÖ–ç2W6VgVÂ2âöffÆ–æR÷7FÆRfÆÆ&6²à¢&W7VÇE·6–EÒÒ66†VE²'&öw&ÖÖW2%Ð¢VÆ–b†66†VBæBæ÷Bf÷&6RæB†æ÷rÒ66†VE²'G2%ÒÂôUuõ$Te$U4…õEDÂ¢æB†æ÷B66†VBævWB‚'&öw&ÖÖW2"’÷"öWuö66†Uö†5ö6÷fW&vR†66†VBÂæ÷r’’“ ¢&W7VÇE·6–EÒÒ66†VE²'&öw&ÖÖW2%Ð¢VÆ–bæ÷B66†VEööæÇ“ ¢FõöfWF6‚æVæB‡6–B¢2$”Ô%’4õU$4S¢öæR'VÆ²„ÔÅEbF÷væÆöB6÷fW'2WfW'’6†ææVÂ@¢2öæ6RâÖV6‚vçFVB7G&VÕö–BFò—G2Wuö6†ææVÅö–BÂfWF6‚F†P¢2v†öÆRwV–FRÂæBf–ÆÂ&W7VÇG2âç—F†–ærF†R„ÔÅEbÆ6·2fÆÇ0¢2F‡&÷Vv‚FòF†RW"Ö6†ææVÂ’&VÆ÷rà¢–bFõöfWF6‚æBæ÷B66†VEööæÇ“ ¢G'“ ¢6†ææVÇ2Âö6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢6–E÷FõöWrÒ·Ð¢f÷"6‚–â6†ææVÇ3 ¢76–BÒ7G"†6‚ævWB‚'7G&VÕö–B"’¢V–BÒ7G"†6‚ævWB‚&Wuö6†ææVÅö–B"’÷"""’ç7G&—‚¢–b76–BæBV–C ¢6–E÷FõöWu¶76–EÒÒV–@¢vçFVEöWrÒ·6–E÷FõöWu·5Òf÷"2–âFõöfWF6‚–b2–â6–E÷FõöWwÐ¢–bvçFVEöWs ¢Wuö'•ö6†ææVÂÒfWF6…÷†ÖÇGeöWr‡‚ÂvçFVEöWr¢÷&V6÷&E÷6÷W&6R‚&Wu÷†ÖÇGb"ÂG'VRÂ6÷VçCÖÆVâ†Wuö'•ö6†ææVÂ’¢f–ÆÆVBÒµÐ¢f÷"6–B–âFõöfWF6ƒ ¢V–BÒ6–E÷FõöWrævWB‡6–B¢–bV–BæBV–B–âWuö'•ö6†ææVÃ ¢&öw2ÒWuö'•ö6†ææVÅ¶V–EÐ¢ôUuô44„U·6–EÒÒ²'G2#¢æ÷rÂ'&öw&ÖÖW2#¢&öw7Ð¢66†Uö6†ævVBÒG'VP¢&W7VÇE·6–EÒÒ&öw0¢7FG5²'WFFVB%Ò³Ò¢f–ÆÆVBæVæB‡6–B¢2öæÇ’6†ææVÇ2äõB6÷fW&VB'’„ÔÅEbæVVBF†R6Æ÷rF‚à¢FõöfWF6‚Ò·2f÷"2–âFõöfWF6‚–b2æ÷B–âf–ÆÆVEÐ¢7FG5²'†ÖÇGeöf–ÆÆVB%ÒÒÆVâ†f–ÆÆVB¢W†6WBW†6WF–öâ2S ¢2„ÔÅEbVæf–Æ&ÆR†öffÆ–æRÂ&Æö6¶VBÂ'6RW'&÷"“¢fÆÀ¢2&6²VçF—&VÇ’FòF†RW"Ö6†ææVÂ’&VÆ÷rà¢÷&V6÷&E÷6÷W&6R‚&Wu÷†ÖÇGb"ÂfÇ6RÂW'&÷#ÖR¢7FG5²'†ÖÇGeöW'&÷"%ÒÒ7G"†R•³£#Ð¢2dÄÄ$4³¢‡G&VÒw26†÷'BUrVæGö–çB—2öæR&WVW7BW"6†ææVÂà¢2öæÇ’W6VBf÷"6†ææVÇ2F†R'VÆ²„ÔÅEbF–Bæ÷B6÷fW"à¢27G&–7FÇ’öæRBF–ÖRv—F‚6ÖÆÂW6RWfW'’f÷W"6†ææVÇ3°¢26WfW&Â&÷f–FW'2&V¦V7B÷"6–ÆVçFÇ’F‡&÷GFÆR÷fW&Æ–ær&WVW7G2à¢–bFõöfWF6ƒ ¢7FG5²'6fUöÖöFR%ÒÒG'VP¢f÷"’Â6–B–âVçVÖW&FR‡FõöfWF6‚“ ¢G'“ ¢2&WVW7B×VÇF’ÖF’Æ—7F–ærv–æF÷râ&÷f–FW'2Ö’6F†—2À¢2'WB&WF–æ–ærWfW'—F†–ærF†W’&WGW&âÖ¶W2wV–FR&Vg&W6†W0¢2W6VgVÂf÷"F—2&F†W"F†â§W7BF†R7W'&VçBWfVæ–ærà¢&öw2Ò‚ç6†÷'EöWr‡6–BÂôUuôÄ•5D”äuôÄ”Ô•B¢W†6WBW†6WF–öã ¢&öw2ÒæöæP¢öÆBÒôUuô44„RævWB‡6–B¢–b&öw2—2æöæS ¢7FG5²&f–ÆVB%Ò³Ò¢–böÆBæBöÆBævWB‚'&öw&ÖÖW2"“ ¢&W7VÇE·6–EÒÒöÆE²'&öw&ÖÖW2%Ð¢VÆ–bæ÷B&öw3 ¢7FG5²&æõöFF%Ò³Ò¢–böÆBæBöÆBævWB‚'&öw&ÖÖW2"“ ¢&W7VÇE·6–EÒÒöÆE²'&öw&ÖÖW2%Ð¢VÇ6S ¢ôUuô44„U·6–EÒÒ²'G2#¢æ÷rÂ'&öw&ÖÖW2#¢µ×Ð¢66†Uö6†ævVBÒG'VP¢&W7VÇE·6–EÒÒµÐ¢VÇ6S ¢7FG5²'WFFVB%Ò³Ò¢7FG5²&fÆÆ&6µ÷WFFVB%Ò³Ò¢ôUuô44„U·6–EÒÒ²'G2#¢æ÷rÂ'&öw&ÖÖW2#¢&öw7Ð¢66†Uö6†ævVBÒG'VP¢&W7VÇE·6–EÒÒ&öw0¢–b†’²’RBÓÒ ¢F–ÖRç6ÆVWƒã3R¢–b66†Uö6†ævVC ¢÷6fUöWuöF—6µö66†R‡‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&Wr#¢&W7VÇBÂ'F÷FÂ#¢ÆVâ†–G2’Â'7FG2#¢7FG7Ò ¢–bRçF‚ÓÒ"ö’öWuöFV'Vr# ¢2&WGW&ç2F†R$r&÷f–FW"&W7öç6Rf÷"öæR7G&VÒÂf÷"G&÷V&ÆW6†ö÷F–ærà¢6–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‡‚æ6öæf–wW&VB‚’æB6–B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B&WVW7B'Ò¢F&u÷Ò²'W6W&æÖR#¢‚çW6W"Â'77v÷&B#¢‚ç77v÷&BÀ¢&7F–öâ#¢&vWE÷6†÷'EöWr"Â'7G&VÕö–B#¢7G"‡6–B’Â&Æ–Ö—B#¢#2'Ð¢F&u÷W&ÂÒb'·‚æ&6WÒ÷Æ–W%ö’ç‡ò"²W&ÆÆ–"ç'6RçW&ÆVæ6öFR†F&u÷¢G'“ ¢&rÒ‡GGövWEö§6öâ†F&u÷W&Â¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&W'&÷"#¢7G"†R’Â'W&Â#¢F&u÷W&ÇÒ¢&WGW&â6VÆbå÷6VæBƒ#Â²'&r#¢&rÂ''6VB#¢‚ç6†÷'EöWr‡6–BÂÆ–Ö—CÓ2—Ò ¢–bRçF‚ÓÒ"ö’÷7FVÕ÷&öf–ÆR# ¢6frÒÆöEö6öæf–r‚¢7FVÕö–BÒ7G"†6frævWB‚'7FVÕ÷v—6†Æ—7Eö–B"’÷"""’ç7G&—‚¢–bæ÷B&RægVÆÆÖF6‚‡"%ÆG³wÒ"Â7FVÕö–B“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&Æ–æ¶VB#¢fÇ6WÒ¢G'“ ¢&öf–ÆRÒ7FVÕ÷V&Æ–5÷&öf–ÆR‡7FVÕö–B¢&öf–ÆU²&Æ–æ¶VB%ÒÒG'VP¢&WGW&â6VÆbå÷6VæBƒ#Â&öf–ÆR¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&Æ–æ¶VB#¢G'VRÂ&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’÷WFFUö6†V6²# ¢f–Æ&ÆRÂ&VÖ÷FRÒ6†V6µöf÷%÷WFFR‚¢&V¦V7FVBÒ÷&V¦V7FVE÷WFFU÷fW'6–öâ‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&f–Æ&ÆR#¢f–Æ&ÆRÀ¢&7W'&VçB#¢dU%4”ôâÂ&ÆFW7B#¢&VÖ÷FRÀ¢'6¶—VEö&E÷fW'6–öâ#¢&ööÂ‡&VÖ÷FRæB&VÖ÷FRÓÒ&V¦V7FVB’À¢'&V¦V7FVE÷fW'6–öâ#¢&V¦V7FVGÒ ¢–bRçF‚ÓÒ"ö’÷WFFU÷7FGW2# ¢&W÷'BÒ÷2çF‚æ¦ö–â†öF—"‚’Â'WFFR×&öÆÆ&6²çG‡B"¢G'“ ¢v—F‚÷Vâ‡&W÷'BÂ'""ÂVæ6öF–æsÒ'WFbÓ‚"’2†æFÆS ¢ÖW76vRÒ†æFÆRç&VBƒC“b’ç7G&—‚¢W†6WBõ4W'&÷# ¢ÖW76vRÒ" ¢&WGW&â6VÆbå÷6VæBƒ#Â²'&öÆÆ&6²#¢&ööÂ†ÖW76vR’Â&ÖW76vR#¢ÖW76vWÒ ¢–bRçF‚ÓÒ"ö’ö†Ç2# ¢2&VÖ÷FR6Æ–VçG2&V6V—fRöæÇ’6–væVB&VÆ’U$Ç3²&÷f–FW ¢27&VFVçF–Ç2æBFW7F–æF–öç2æWfW"ÆVfRF†R†öÖR6W'fW"à¢6–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‡‚æ6öæf–wW&VB‚’æB6–B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B&WVW7B'Ò¢–b6VÆbåö—5÷&—fFU÷&VÖ÷FUöÆ—7FVæW"‚’æB6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"“ ¢†Ç5÷W&ÂÂG5÷W&ÂÒ‚æ†Ç5÷W&Â‡6–B’Â‚ç7G&VÕ÷W&Â‡6–B¢–bæ÷B…÷6fU÷&VÆ•÷F&vWB††Ç5÷W&ÂÂ–æ—F–ÃÕG'VRÂ6fsÖ6fr’æ@¢÷6fU÷&VÆ•÷F&vWB‡G5÷W&ÂÂ–æ—F–ÃÕG'VRÂ6fsÖ6fr’“ ¢&WGW&â6VÆbå÷6VæBƒS2Â²&W'&÷"#¢'&÷f–FW"&VÆ’F&vWB&V¦V7FVB'Ò¢†Ç5÷Fö¶VâÒ÷&VÆ•÷Fö¶Vâ††Ç5÷W&ÂÂ6fsÖ6fr¢G5÷Fö¶VâÒ÷&VÆ•÷Fö¶Vâ‡G5÷W&ÂÂ6fsÖ6fr¢–bæ÷B††Ç5÷Fö¶VâæBG5÷Fö¶Vâ“ ¢&WGW&â6VÆbå÷6VæBƒS2Â²&W'&÷"#¢'&—fFR&VÆ’Væf–Æ&ÆR'Ò¢&WGW&â6VÆbå÷6VæBƒ#Â°¢&†Ç2#¢"ö’÷&VÆ“÷CÒ"²W&ÆÆ–"ç'6RçV÷FR††Ç5÷Fö¶VâÂ6fSÒ""’À¢'G2#¢"ö’÷&VÆ“÷CÒ"²W&ÆÆ–"ç'6RçV÷FR‡G5÷Fö¶VâÂ6fSÒ""’À¢'&VÆ’#¢G'VWÒ¢&WGW&â6VÆbå÷6VæBƒ#Â²&†Ç2#¢‚æ†Ç5÷W&Â‡6–B’Â'G2#¢‚ç7G&VÕ÷W&Â‡6–B—Ò ¢–bRçF‚ÓÒ"ö’÷&VÖ÷FU÷fÆ2# ¢6–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢6frÒÆöEö6öæf–r‚“²‚Ò‡G&VÒ†6fr¢–bæ÷B6VÆbåö—5÷&—fFU÷&VÖ÷FUöÆ—7FVæW"‚’÷"æ÷B6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"“ ¢&WGW&â6VÆbå÷6VæBƒC2Â²&W'&÷"#¢'&—fFR&VÆ’—2æ÷B7F—fR'Ò¢–bæ÷B‡‚æ6öæf–wW&VB‚’æB6–B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B&WVW7B'Ò¢F&vWBÒ‚ç7G&VÕ÷W&Â‡6–B¢–bæ÷B÷6fU÷&VÆ•÷F&vWB‡F&vWBÂ–æ—F–ÃÕG'VRÂ6fsÖ6fr“ ¢&WGW&â6VÆbå÷6VæBƒS2Â²&W'&÷"#¢'&÷f–FW"&VÆ’F&vWB&V¦V7FVB'Ò¢Fö¶VâÒ÷&VÆ•÷Fö¶Vâ‡F&vWBÂ6fsÖ6fr¢†÷7BÂ÷'BÒ6VÆbç6W'fW"ç6W'fW%öFG&W75³£%Ð¢&VÆ’Òb&‡GG¢ò÷¶†÷7GÓ§·÷'GÒö’÷&VÆ“÷CÒ"²W&ÆÆ–"ç'6RçV÷FR‡Fö¶VâÂ6fSÒ""¢&öG’Ò"4U…DÓ5UÆâ4U…D”äc¢ÓÄõEdÒ&—fFR&VÆ•Æâ"²&VÆ’²%Æâ ¢&WGW&â6VÆbå÷6VæBƒ#Â&öG’Â&VF–ò÷‚Ö×VwW&Â"Â°¢$6öçFVçBÔF—7÷6—F–öâ#¢vGF6†ÖVçC²f–ÆVæÖSÒ&÷GfÒ×&—fFR×7G&VÒæÓ7R"wÒ ¢–bRçF‚ÓÒ"ö’÷–ær# ¢26†VÆö6Â–FVçF—G’6†V6²W6VBFò&WfVçBGWÆ–6FR ¢2–ç7Fæ6W2&Vv&FÆW72öbv†BF†RÆVæ6†W"æW†R—2æÖVBà¢&WGW&â6VÆbå÷6VæBƒ#Â²&#¢&öÆ÷2×GfÖFR"Â'fW'6–öâ#¢dU%4”ôâÀ¢&–ç7Fæ6R#¢õ4U%dU%ô”å5Dä4Uô”GÒ ¢–bRçF‚–â‚"ö’÷&÷‡’"Â"ö’÷&VÆ’"“ ¢2Æ–v‡GvV–v‡BÖVF–&VÆ’f÷"'&÷w6W"Æ–&6²âF†—2æWfW ¢2G&ç66öFW3¢Æ–Æ—7G2&R&Ww&—GFVâæBÖVF–'—FW2&R7G&VÖV@¢2F‡&÷Vv‚Væ6†ævVB6ò„Å2æBÕTrÕE26âv÷&²&÷VæB4õ%2à¢F&vWBÒ&VÆ•÷F&vWB–bRçF‚ÓÒ"ö’÷&VÆ’"VÇ6RævWB‚'R"Â²"%Ò•³Ð¢–bæ÷BF&vWC ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&æòW&Â'Ò¢–bRçF‚ÓÒ"ö’÷&÷‡’"æB6VÆbåö—5÷&—fFU÷&VÖ÷FUöÆ—7FVæW"‚“ ¢&WGW&â6VÆbå÷6VæBƒC2Â²&W'&÷"#¢'Vç6–væVB&VÖ÷FR&÷‡’F—6&ÆVB'Ò¢'6VE÷F&vWBÒW&ÆÆ–"ç'6RçW&Ç7Æ—B‡F&vWB¢–b'6VE÷F&vWBç66†VÖRæ÷B–â‚&‡GG"Â&‡GG2"“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢'Vç7W÷'FVBW&Â'Ò¢G'“ ¢†VFW'2Ò°¢%W6W"ÔvVçB#¢%dÄ2ó2ãÆ–%dÄ2ó2ã"À¢$66WB#¢"¢ò¢'Ð¢&ævUö†VFW"Ò6VÆbæ†VFW'2ævWB‚%&ævR"¢–b&ævUö†VFW# ¢†VFW'5²%&ævR%ÒÒ&ævUö†VFW ¢&WÒW&ÆÆ–"ç&WVW7Bå&WVW7B‡F&vWBÂ†VFW'3Ö†VFW'2¢÷VæW"Ò‡W&ÆÆ–"ç&WVW7Bæ'V–ÆEö÷VæW"…õ6fU&VÆ•&VF—&V7D†æFÆW"‚’¢–bRçF‚ÓÒ"ö’÷&VÆ’"VÇ6RW&ÆÆ–"ç&WVW7Bæ'V–ÆEö÷VæW"‚’¢v—F‚÷VæW"æ÷Vâ‡&WÂF–ÖV÷WCÓ#’2&W7 ¢VffV7F—fU÷F&vWBÒ7G"‡&W7ævWGW&Â‚’÷"F&vWB¢–bRçF‚ÓÒ"ö’÷&VÆ’"æBæ÷B÷6fU÷&VÆ•÷F&vWB†VffV7F—fU÷F&vWB“ ¢&—6RfÇVTW'&÷"‚'Vç6fR&VÆ’FW7F–æF–öâ"¢7G—RÒ&W7æ†VFW'2ævWB‚$6öçFVçBÕG—R"Â&Æ–6F–öâöö7FWB×7G&VÒ"¢F…öÆ÷rÒW&ÆÆ–"ç'6RçW&Ç7Æ—B†VffV7F—fU÷F&vWB’çF‚æÆ÷vW"‚¢—5÷Æ–Æ—7BÒ‚&×VwW&Â"–â7G—RæÆ÷vW"‚’÷ ¢F…öÆ÷ræVæG7v—F‚‚"æÓ7S‚"’¢–b—5÷Æ–Æ—7C ¢2Æ–Æ—7G2&RF–ç’â&Ww&—FR6VvÖVçG2ÇW2U$“Ò"âââ ¢2GG&–'WFW2W6VB'’Væ7'—F–öâ¶W—2æB–æ—BÖ2à¢&rÒ&W7ç&VBƒB¢#B¢#B²¢–bÆVâ‡&r’âB¢#B¢#C ¢&—6RfÇVTW'&÷"‚'Æ–Æ—7BFöòÆ&vR"¢FW‡BÒ&ræFV6öFR‚'WFbÓ‚"Â'&WÆ6R"¢÷WEöÆ–æW2ÒµÐ ¢FVb&÷‡•÷W&Â†6†–ÆB“ ¢'6öÇWFRÒW&ÆÆ–"ç'6RçW&Æ¦ö–â†VffV7F—fU÷F&vWBÂ6†–ÆB¢–bRçF‚ÓÒ"ö’÷&VÆ’# ¢–bæ÷B÷6fU÷&VÆ•÷F&vWB†'6öÇWFR“ ¢&WGW&â" ¢6†–ÆE÷Fö¶VâÒ÷&VÆ•÷Fö¶Vâ†'6öÇWFR¢&WGW&â"ö’÷&VÆ“÷CÒ"²W&ÆÆ–"ç'6RçV÷FR†6†–ÆE÷Fö¶VâÂ6fSÒ""¢&WGW&â"ö’÷&÷‡“÷SÒ"²W&ÆÆ–"ç'6RçV÷FR†'6öÇWFRÂ6fSÒ"" ¢f÷"Æ–æR–âFW‡Bç7Æ—FÆ–æW2‚“ ¢2ÒÆ–æRç7G&—‚¢–b2æBæ÷B2ç7F'G7v—F‚‚"2"“ ¢÷WEöÆ–æW2æVæB‡&÷‡•÷W&Â‡2’¢VÆ–b2ç7F'G7v—F‚‚"2"’æBuU$“Ò"r–âÆ–æS ¢Æ–æRÒ&Rç7V"€¢"uU$“Ò"…µåÂ%Ò²’"rÀ¢ÆÖ&FÓ¢uU$“Ò"r²&÷‡•÷W&Â†Òæw&÷Wƒ’’²r"rÀ¢Æ–æR¢÷WEöÆ–æW2æVæB†Æ–æR¢VÇ6S ¢÷WEöÆ–æW2æVæB†Æ–æR¢&rÒ‚%Æâ"æ¦ö–â†÷WEöÆ–æW2’²%Æâ"’æVæ6öFR‚'WFbÓ‚"¢6VÆbç6VæE÷&W7öç6Rƒ#¢6VÆbç6VæEö†VFW"‚$6öçFVçBÕG—R"Â&Æ–6F–öâ÷fæBæÆRæ×VwW&Â"¢6VÆbç6VæEö†VFW"‚$66W72Ô6öçG&öÂÔÆÆ÷rÔ÷&–v–â"Â"¢"¢6VÆbç6VæEö†VFW"‚$66†RÔ6öçG&öÂ"Â&æòÖ66†R"¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔÆVæwF‚"Â7G"†ÆVâ‡&r’’¢6VÆbæVæEö†VFW'2‚¢6VÆbçvf–ÆRçw&—FR‡&r¢&WGW&à ¢2ÖVF–—27G&VÖVB2—B'&—fW2–ç7FVBöb&W7ç&VB‚’à¢2F†B—2W76VçF–Âf÷"Æ—fRE2æBfö–G2'VffW&–ær¢2v†öÆRf–FVòÆö6ÆÇ’â'—FW2&RæWfW"FV6öFVB÷&RÖVæ6öFVBà¢7FGW2ÒvWFGG"‡&W7Â'7FGW2"Â#’÷"# ¢6VÆbç6VæE÷&W7öç6R‡7FGW2¢6VÆbç6VæEö†VFW"‚$6öçFVçBÕG—R"Â7G—R¢6VÆbç6VæEö†VFW"‚$66W72Ô6öçG&öÂÔÆÆ÷rÔ÷&–v–â"Â"¢"¢6VÆbç6VæEö†VFW"‚$66†RÔ6öçG&öÂ"Â&æòÖ66†R"¢f÷"†â–â‚$6öçFVçBÔÆVæwF‚"Â$6öçFVçBÕ&ævR"Â$66WBÕ&ævW2"“ ¢‡bÒ&W7æ†VFW'2ævWB††â¢–b‡c ¢6VÆbç6VæEö†VFW"††âÂ‡b¢6VÆbæVæEö†VFW'2‚¢G'“ ¢v†–ÆRG'VS ¢6‡Væ²Ò&W7ç&VBƒcB¢#B¢–bæ÷B6‡Væ³ ¢'&V°¢6VÆbçvf–ÆRçw&—FR†6‡Væ²¢W†6WB„'&ö¶Vå—TW'&÷"Â6öææV7F–öå&W6WDW'&÷"“ ¢70¢&WGW&à¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’ö6öæf–r# ¢6frÒÆöEö6öæf–r‚¢V&Æ–5ö6frÒF–7B†6fr¢V&Æ–5ö6frç÷‚&Æåö66W75÷Fö¶Vâ"ÂæöæR¢V&Æ–5ö6fu²&Æå÷W&Â%ÒÒöÆåö66W75÷W&Â†6frÂô5D•dUôÄåõõ%B÷"Äåõõ%BÂ6VÆbåö—5öÆö÷&6²‚’’–b6frævWB‚&ÆÆ÷uöÆâ"’VÇ6R" ¢V&Æ–5ö6fu²'&—fFU÷&VÖ÷FU÷W&Â%ÒÒ…÷&—fFU÷&VÖ÷FU÷W&Â€¢6frÂô5D•dUõ$TÔõDUõõ%B÷"$TÔõDUõõ%BÂ6VÆbåö—5öÆö÷&6²‚’¢–b6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"’VÇ6R""¢&WGW&â6VÆbå÷6VæBƒ#ÂV&Æ–5ö6fr ¢–bRçF‚ÓÒ"ö’ö'Gv÷&µö66†R# ¢&WGW&â6VÆbå÷6VæBƒ#Â²&'—FW2#¢'Gv÷&µö66†U÷6—¦R‚—Ò ¢–bRçF‚ÓÒ"ö’÷FW7B# ¢ö²Â–æfòÒ‡G&VÒ†ÆöEö6öæf–r‚’’æÆöv–â‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢ö²Â&–æfò#¢–æfò–bö²VÇ6RæöæRÀ¢&W'&÷"#¢æöæR–bö²VÇ6R–æf÷Ò ¢–bRçF‚ÓÒ"ö’÷&VÆöB# ¢6frÒÆöEö6öæf–r‚¢–bæ÷B‡G&VÒ†6fr’æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢fÇ6RÂ&W'&÷"#¢$æ÷B6öæf–wW&VB'Ò¢G'“ ¢6‚ÂòÒvWE÷‡G&VÕö6†ææVÇ2†6frÂf÷&6SÕG'VR¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ&6÷VçB#¢ÆVâ†6‚—Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢fÇ6RÂ&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’ö6FVv÷&–W2# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&6FVv÷&–W2#¢µÒÂ&ÆövvVEö–â#¢fÇ6WÒ¢G'“ ¢6†ææVÇ2Â6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&6FVv÷&–W2#¢µÒÂ&ÆövvVEö–â#¢G'VRÀ¢&W'&÷"#¢7G"†R—Ò¢26÷VçB6†ææVÇ2W"6FVv÷'’†öæÇ’6FVv÷&–W2F†B†fR6†ææVÇ2¢6÷VçG2Ò·Ð¢f÷"6‚–â6†ææVÇ3 ¢6âÒ6G2ævWB†6…²&6FVv÷'•ö–B%ÒÂ""¢–b6ã ¢6÷VçG5¶6åÒÒ6÷VçG2ævWB†6âÂ’²¢÷WBÒ·²&æÖR#¢²Â&6÷VçB#¢gÒf÷"²Âb–à¢6÷'FVB†6÷VçG2æ—FV×2‚’Â¶W“ÖÆÖ&F·c¢·e³ÒæÆ÷vW"‚’•Ð¢&WGW&â6VÆbå÷6VæBƒ#Â²&6FVv÷&–W2#¢÷WBÂ&ÆövvVEö–â#¢G'VWÒ ¢–bRçF‚ÓÒ"ö’ö6†ææVÇ2# ¢2&æ¶VB6FÆöwVR6V&6‚²÷F–öæÂ6FVv÷'’f–ÇFW"à¢FW&ÒÒ‡ævWB‚'"Â²"%Ò•³Ò’ç7G&—‚¢6Eöf–ÇFW"Ò‡ævWB‚&6B"Â²"%Ò•³Ò’ç7G&—‚¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&6†ææVÇ2#¢µÒÂ&ÆövvVEö–â#¢fÇ6RÀ¢'F÷FÂ#¢Ò¢G'“ ¢6†ææVÇ2Â6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&6†ææVÇ2#¢µÒÂ&ÆövvVEö–â#¢G'VRÀ¢&W'&÷"#¢7G"†R’Â'F÷FÂ#¢Ò¢÷WBÒµÐ¢f÷"6‚–â6†ææVÇ3 ¢æÒÒ6…²&æÖR%Ð¢6FæÖRÒ6G2ævWB†6…²&6FVv÷'•ö–B%ÒÂ""¢–b6Eöf–ÇFW"æB6FæÖRÒ6Eöf–ÇFW# ¢6öçF–çVP¢6V&6…÷&æ²Ò…ö6†ææVÅö6FÆöu÷6V&6…÷&æ²†æÒÂ6FæÖRÂFW&Ò¢–bFW&ÒVÇ6RæöæR¢–bFW&ÒæB6V&6…÷&æ²—2æöæS ¢6öçF–çVP¢÷WBæVæB‡°¢&æÖR#¢æÒÀ¢'7G&VÕö–B#¢6…²'7G&VÕö–B%ÒÀ¢&6FVv÷'’#¢6FæÖRÀ¢&Æövò#¢6‚ævWB‚'7G&VÕö–6öâ"Â""’À¢'VÆ—G’#¢VÆ—G•÷Fr†æÒ’À¢'W&Â#¢‚ç7G&VÕ÷W&Â†6…²'7G&VÕö–B%Ò’À¢%÷6V&6…÷&æ²#¢6V&6…÷&æ²À¢Ò¢–bFW&Ó ¢÷WBç6÷'B†¶W“ÖÆÖ&F&÷s¢&÷u²%÷6V&6…÷&æ²%Ò¢f÷"&÷r–â÷WC ¢&÷rç÷‚%÷6V&6…÷&æ²"ÂæöæR¢F÷FÂÒÆVâ†÷WB¢6VBÒ÷WE³£SÐ¢&WGW&â6VÆbå÷6VæBƒ#Â²&6†ææVÇ2#¢6VBÂ&ÆövvVEö–â#¢G'VRÀ¢'F÷FÂ#¢F÷FÂÂ'6†÷vâ#¢ÆVâ†6VB—Ò ¢–bRçF‚ÓÒ"ö’öÖ÷f–Uö6FÆör# ¢6FÆöuöæÖRÒ‡ævWB‚&6FÆör"Â²'÷VÆ"%Ò•³Ò’ç7G&—‚’æÆ÷vW"‚¢G'“ ¢Æ–Ö—BÒÖ‚ƒÂÖ–âƒ3Â–çB‡ævWB‚&Æ–Ö—B"Â²#%Ò•³Ò’’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢Æ–Ö—BÒ ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢G'“ ¢6FÆörÒ6–æVÖWFöÖ÷f–Uö6FÆör†6FÆöuöæÖRÂÆ–Ö—B¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢µÒÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚’À¢&W'&÷"#¢$Ö÷f–R6FÆös¢"²7G"†R—Ò¢&÷f–FW%öÖ÷f–W2ÒµÐ¢–b‚æ6öæf–wW&VB‚“ ¢G'“ ¢&÷f–FW%öÖ÷f–W2ÒvWE÷‡G&VÕöÖ÷f–W2†6fr¢W†6WBW†6WF–öã ¢&÷f–FW%öÖ÷f–W2ÒµÐ¢÷WBÒµÐ¢f÷"ÖWF–â6FÆös ¢æÖRÒ7G"†ÖWFævWB‚&æÖR"’÷"""’ç7G&—‚¢–bæ÷BæÖS ¢6öçF–çVP¢–V"Òö6FÆöu÷–V"†ÖWF¢6÷W&6W2ÒÖF6…÷föE÷6÷W&6W2‡²&æÖR#¢æÖRÂ'–V"#¢–V'ÒÂ&÷f–FW%öÖ÷f–W2¢f—'7BÒ6÷W&6W5³Ò–b6÷W&6W2VÇ6R·Ð¢÷WBæVæB‡²&6FÆöuö–B#¢ÖWFævWB‚&–B"’÷"ÖWFævWB‚&–ÖF%ö–B"’÷"""À¢'7G&VÕö–B#¢f—'7BævWB‚'7G&VÕö–B"’Â&æÖR#¢æÖRÀ¢&W‡FVç6–öâ#¢f—'7BævWB‚&W‡FVç6–öâ"’÷"&×B"Â'–V"#¢–V"À¢'&F–ær#¢ÖWFævWB‚&–ÖF%&F–ær"’÷"""À¢&6÷fW"#¢ÖWFævWB‚'÷7FW""’÷"""Â'6÷W&6W2#¢6÷W&6W2À¢'7G&VÕöf÷VæB#¢&ööÂ‡6÷W&6W2—Ò¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢÷WBÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚’À¢&6FÆör#¢6FÆöuöæÖWÒ ¢–bRçF‚ÓÒ"ö’öÖ÷f–W2# ¢FW&ÒÒ‡ævWB‚'"Â²"%Ò•³Ò’ç7G&—‚¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷BFW&Ó ¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢µÒÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚—Ò¢G'“ ¢6FÆörÒ6–æVÖWF÷6V&6‚‚&Ö÷f–R"ÂFW&Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢µÒÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚’À¢&W'&÷"#¢$Ö÷f–R6FÆös¢"²7G"†R—Ò¢&÷f–FW%öÖ÷f–W2ÒµÐ¢–b‚æ6öæf–wW&VB‚“ ¢G'“ ¢&÷f–FW%öÖ÷f–W2ÒvWE÷‡G&VÕöÖ÷f–W2†6fr¢W†6WBW†6WF–öã ¢&÷f–FW%öÖ÷f–W2ÒµÐ¢÷WBÒµÐ¢f÷"ÖWF–â6FÆös ¢æÖRÒ7G"†ÖWFævWB‚&æÖR"’÷"""’ç7G&—‚¢–bæ÷BæÖS ¢6öçF–çVP¢–V"Òö6FÆöu÷–V"†ÖWF¢6÷W&6W2ÒÖF6…÷föE÷6÷W&6W2‡²&æÖR#¢æÖRÂ'–V"#¢–V'ÒÂ&÷f–FW%öÖ÷f–W2¢f—'7BÒ6÷W&6W5³Ò–b6÷W&6W2VÇ6R·Ð¢÷WBæVæB‡°¢&6FÆöuö–B#¢ÖWFævWB‚&–B"’÷"""À¢'7G&VÕö–B#¢f—'7BævWB‚'7G&VÕö–B"’À¢&æÖR#¢æÖRÀ¢&W‡FVç6–öâ#¢f—'7BævWB‚&W‡FVç6–öâ"’÷"&×B"À¢'–V"#¢–V"À¢'&F–ær#¢ÖWFævWB‚&–ÖF%&F–ær"’÷"""À¢&6÷fW"#¢ÖWFævWB‚'÷7FW""’÷"""À¢'6÷W&6W2#¢6÷W&6W2À¢'7G&VÕöf÷VæB#¢&ööÂ‡6÷W&6W2’À¢Ò¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢÷WBÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚—Ò ¢–bRçF‚ÓÒ"ö’öff÷&—FUöÖ÷f–U÷7FGW2# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢G'“ ¢&÷f–FW%öÖ÷f–W2ÒvWE÷‡G&VÕöÖ÷f–W2†6fr’–b‚æ6öæf–wW&VB‚’VÇ6RµÐ¢W†6WBW†6WF–öã ¢&÷f–FW%öÖ÷f–W2ÒµÐ¢÷WBÒµÐ¢f÷"Ö÷f–R–âÆöEöff÷&—FW2‚’ævWB‚&Ö÷f–W2"ÂµÒ“ ¢&÷rÒF–7B†Ö÷f–R¢6÷W&6W2ÒÖF6…÷föE÷6÷W&6W2‡&÷rÂ&÷f–FW%öÖ÷f–W2¢&÷u²'6÷W&6W2%ÒÒ6÷W&6W0¢&÷u²'7G&VÕöf÷VæB%ÒÒ&ööÂ‡6÷W&6W2¢–b6÷W&6W3 ¢&÷u²'7G&VÕö–B%ÒÒ6÷W&6W5³ÒævWB‚'7G&VÕö–B"¢&÷u²&W‡FVç6–öâ%ÒÒ6÷W&6W5³ÒævWB‚&W‡FVç6–öâ"’÷"&×B ¢÷WBæVæB‡&÷r¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢÷WBÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚—Ò ¢–bRçF‚ÓÒ"ö’÷&V6VçEöÖ÷f–W2# ¢G'“ ¢Æ–Ö—BÒÖ‚ƒÂÖ–âƒ3bÂ–çB‡ævWB‚&Æ–Ö—B"Â²#’%Ò•³Ò’’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢Æ–Ö—BÒ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢µÒÂ&ÆövvVEö–â#¢fÇ6WÒ¢G'“ ¢Ö÷f–W2ÒvWE÷‡G&VÕöÖ÷f–W2†6fr¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢µÒÂ&ÆövvVEö–â#¢G'VRÀ¢&W'&÷"#¢7G"†R—Ò¢F†—5÷–V"ÒF–ÖRæÆö6ÇF–ÖR‚’çFÕ÷–V ¢'•÷–V"Ò·Ð¢ÆÅ÷&÷w2ÒµÐ¢f÷"Ò–âÖ÷f–W3 ¢&u÷–V"Ò""æ¦ö–â‡7G"‡fÇVR÷"""’f÷"fÇVR–à¢†ÒævWB‚'–V""’ÂÒævWB‚'&VÆV6TFFR"’À¢ÒævWB‚'&VÆV6UöFFR"’ÂÒævWB‚&æÖR"’’¢ÖF6‚Ò&Rç6V&6‚‡""ƒó£—Ã#•ÆG³'Ò"Â&u÷–V"¢–V"Ò–çB†ÖF6‚æw&÷Wƒ’’–bÖF6‚VÇ6R ¢G'“ ¢FFVBÒ–çB†fÆöB†ÒævWB‚&FFVB"’÷"’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢FFVBÒ ¢&÷rÒ†FFVBÂÒÂ–V"¢ÆÅ÷&÷w2æVæB‡&÷r¢–b–V# ¢'•÷–V"ç6WFFVfVÇB‡–V"ÂµÒ’æVæB‡&÷r¢f÷"&÷w2–â'•÷–V"çfÇVW2‚“ ¢&÷w2ç6÷'B†¶W“ÖÆÖ&F—FVÓ¢—FVÕ³ÒÂ&WfW'6SÕG'VR¢W6&ÆU÷–V'2Ò·–V"f÷"–V"–â'•÷–V"–b–V"ÃÒF†—5÷–V"²Ð¢F&vWE÷–V"ÒF†—5÷–V"–bF†—5÷–V"–â'•÷–V"VÇ6R€¢Ö‚‡W6&ÆU÷–V'2’–bW6&ÆU÷–V'2VÇ6R¢6æF–FFU÷&÷w2ÒÆ—7B†'•÷–V"ævWB‡F&vWE÷–V"ÂµÒ’¢6æF–FFU÷&÷w2³Ò'•÷–V"ævWB‡F&vWE÷–V"ÒÂµÒ¢–bæ÷B6æF–FFU÷&÷w3 ¢ÆÅ÷&÷w2ç6÷'B†¶W“ÖÆÖ&F—FVÓ¢—FVÕ³ÒÂ&WfW'6SÕG'VR¢6æF–FFU÷&÷w2ÒÆÅ÷&÷w0¢Væ—VU÷&÷w2ÒµÐ¢6VVå÷F—FÆW2Ò6WB‚¢f÷"&÷r–â6æF–FFU÷&÷w3 ¢6ÆVå÷F—FÆRÒö6ÆVå÷6†÷u÷F—FÆR‡&÷u³ÒævWB‚&æÖR"’’÷"7G"€¢&÷u³ÒævWB‚&æÖR"’÷"""¢F—FÆUö¶W’Ò&Rç7V"‡"%µæ×£Ó•Ò²"Â""Â6ÆVå÷F—FÆRæÆ÷vW"‚’¢–bæ÷BF—FÆUö¶W’÷"F—FÆUö¶W’–â6VVå÷F—FÆW3 ¢6öçF–çVP¢6VVå÷F—FÆW2æFB‡F—FÆUö¶W’¢Væ—VU÷&÷w2æVæB‡&÷r¢6†÷6VâÒVæ—VU÷&÷w5³¦Æ–Ö—EÐ¢÷WBÒµÐ¢f÷"öFFVBÂÒÂ–V"–â6†÷6Vã ¢6÷fW"Ò7G"†ÒævWB‚'7G&VÕö–6öâ"’÷"ÒævWB‚&6÷fW""’÷ ¢ÒævWB‚&Ö÷f–Uö–ÖvR"’÷"""’ç7G&—‚¢–bæ÷B6÷fW"ç7F'G7v—F‚‚‚&‡GG¢òò"Â&‡GG3¢òò"’“ ¢6÷fW"Ò" ¢÷WBæVæB‡²'7G&VÕö–B#¢ÒævWB‚'7G&VÕö–B"’À¢&æÖR#¢7G"†ÒævWB‚&æÖR"’÷"""’À¢&W‡FVç6–öâ#¢ÒævWB‚&6öçF–æW%öW‡FVç6–öâ"’÷"&×B"À¢'–V"#¢–V"Â'&F–ær#¢ÒævWB‚'&F–ær"’÷"""À¢&6÷fW"#¢6÷fW'Ò¢&WGW&â6VÆbå÷6VæBƒ#Â²&Ö÷f–W2#¢÷WBÂ&ÆövvVEö–â#¢G'VRÀ¢&6FÆöu÷–V"#¢F&vWE÷–V"À¢&†5öÖ÷&R#¢ÆVâ‡Væ—VU÷&÷w2’âÆ–Ö—GÒ ¢–bRçF‚ÓÒ"ö’÷6†÷w2# ¢FW&ÒÒ‡ævWB‚'"Â²"%Ò•³Ò’ç7G&—‚¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷BFW&Ó ¢&WGW&â6VÆbå÷6VæBƒ#Â²'6†÷w2#¢µÒÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚—Ò¢G'“ ¢6FÆörÒ6–æVÖWF÷6V&6‚‚'6W&–W2"ÂFW&Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²'6†÷w2#¢µÒÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚’À¢&W'&÷"#¢%6†÷r6FÆös¢"²7G"†R—Ò¢&÷f–FW"ÒµÐ¢–b‚æ6öæf–wW&VB‚“ ¢G'“ ¢&÷f–FW"ÒvWE÷‡G&VÕ÷6W&–W2†6fr¢W†6WBW†6WF–öã ¢&÷f–FW"ÒµÐ¢÷WBÒµÐ¢f÷"ÖWF–â6FÆös ¢æÖRÒ7G"†ÖWFævWB‚&æÖR"’÷"""’ç7G&—‚¢–bæ÷BæÖS ¢6öçF–çVP¢¶W’Ò÷6†÷uö¶W’†æÖR¢6–&Æ–æw2Ò·&÷rf÷"&÷r–â&÷f–FW"–b÷6†÷uö¶W’‡&÷rævWB‚&æÖR"’’ÓÒ¶W•Ð¢–G2Ò·&÷rævWB‚'6W&–W5ö–B"’f÷"&÷r–â6–&Æ–æw2–b&÷rævWB‚'6W&–W5ö–B"’—2æ÷BæöæUÐ¢–V"Òö6FÆöu÷–V"†ÖWF¢÷WBæVæB‡²&6FÆöuö–B#¢ÖWFævWB‚&–B"’÷"""Â'6†÷uö¶W’#¢¶W’À¢'6W&–W5ö–B#¢–G5³Ò–b–G2VÇ6RæöæRÂ'6W&–W5ö–G2#¢–G2À¢'&÷f–FW%öf÷VæB#¢&ööÂ†–G2’Â&æÖR#¢æÖRÀ¢&6÷fW"#¢ÖWFævWB‚'÷7FW""’÷"""Â'–V"#¢–V"À¢'&F–ær#¢ÖWFævWB‚&–ÖF%&F–ær"’÷""'Ò¢&WGW&â6VÆbå÷6VæBƒ#Â²'6†÷w2#¢÷WBÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚—Ò ¢–bRçF‚ÓÒ"ö’öÆFW7EöW—6öFW2# ¢G'“ ¢Æ–Ö—BÒÖ‚ƒÂÖ–âƒ3bÂ–çB‡ævWB‚&Æ–Ö—B"Â²#’%Ò•³Ò’’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢Æ–Ö—BÒ¢&Vg&W6…öW‡FW&æÂÒævWB‚'&Vg&W6‚"Â²#%Ò•³ÒÓÒ# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B&Vg&W6…öW‡FW&æÃ ¢66†VBÒöÆöEöÆFW7EöW—6öFW5ö66†R‡‚¢–b66†VB—2æ÷BæöæS ¢66†VE÷&÷w2Ò66†VBævWB‚&W—6öFW2"’÷"µÐ¢&WGW&â6VÆbå÷6VæBƒ#Â°¢&W—6öFW2#¢66†VE÷&÷w5³¦Æ–Ö—EÒÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚’À¢&†5öÖ÷&R#¢ÆVâ†66†VE÷&÷w2’âÆ–Ö—BÀ¢'W6öÖ–ær#¢†66†VBævWB‚'W6öÖ–ær"’÷"µÒ•³£3eÒÀ¢&W'&÷'2#¢–çB†66†VBævWB‚&W'&÷'2"’÷"’À¢&66†VB#¢G'VWÒ¢&÷w2ÒµÐ¢W6öÖ–æu÷&÷w2ÒµÐ¢W'&÷'2Ò ¢–b‚æ6öæf–wW&VB‚“ ¢G'“ ¢6W&–W5ö6FÆörÒvWE÷‡G&VÕ÷6W&–W2†6fr¢W†6WBW†6WF–öã ¢6W&–W5ö6FÆörÒµÐ¢VÇ6S ¢6W&–W5ö6FÆörÒµÐ¢f÷"fe÷6†÷r–âÆöEöff÷&—FW2‚’ævWB‚'6†÷w2"ÂµÒ“ ¢6W&–W5ö–BÒfe÷6†÷rævWB‚'6W&–W5ö–B"¢ff÷&—FUö¶W’Ò7G"†fe÷6†÷rævWB‚'6†÷uö¶W’"’÷"÷6†÷uö¶W’†fe÷6†÷rævWB‚&æÖR"’’¢6–&Æ–æw2Ò·&÷rævWB‚'6W&–W5ö–B"’f÷"&÷r–â6W&–W5ö6FÆöp¢–b÷6†÷uö¶W’‡&÷rævWB‚&æÖR"’’ÓÒff÷&—FUö¶W•Ð¢6–&Æ–æw2Ò·6–Bf÷"6–B–â6–&Æ–æw2–b6–B—2æ÷BæöæUÐ¢–b6–&Æ–æw3 ¢6W&–W5ö–BÒ6–&Æ–æw5³Ð¢–b6W&–W5ö–B—2æöæS ¢G'“ ¢6†÷uöæÖRÒ7G"†fe÷6†÷rævWB‚&æÖR"’÷"%6†÷r"¢F—7Æ•÷6†÷uöæÖRÒö6ÆVå÷6†÷u÷F—FÆR‡6†÷uöæÖR’÷"6†÷uöæÖP¢–V%öÖF6‚Ò&Rç6V&6‚‡""ƒó£—Ã#•ÆG³'Ò"Â7G"†fe÷6†÷rævWB‚'–V""’÷"""’¢6†÷u÷–V"Ò–çB‡–V%öÖF6‚æw&÷Wƒ’’–b–V%öÖF6‚VÇ6R ¢66†VGVÆRÒ÷GfÖ¦UöW—6öFU÷66†VGVÆR‡6†÷uöæÖRÂ6†÷u÷–V"À¢f÷&6S×&Vg&W6…öW‡FW&æÂ¢6÷fW"Ò7G"†fe÷6†÷rævWB‚&6÷fW""’÷"""¢W‡FW&æÂÒ66†VGVÆRævWB‚&ÆFW7B"’÷"·Ð¢W6öÖ–ærÒ66†VGVÆRævWB‚'W6öÖ–ær"’÷"·Ð¢–bW6öÖ–æs ¢G'“ ¢WG2Ò†FFWF–ÖRæFFWF–ÖRæg&öÖ—6öf÷&ÖB‡W6öÖ–æu²&—'7F×%Òç&WÆ6R‚%¢"Â"³£"’’çF–ÖW7F×‚¢–bW6öÖ–ærævWB‚&—'7F×"’VÇ6RF–ÖRæÖ·F–ÖR‡F–ÖRç7G'F–ÖR‡W6öÖ–ærævWB‚&—&FFR"’÷"""Â"U’ÒVÒÒVB"’’¢W†6WB…fÇVTW'&÷"Â÷fW&fÆ÷tW'&÷"ÂG—TW'&÷"“ ¢WG2Ò ¢W6öÖ–æu÷&÷w2æVæB‡²'6†÷uöæÖR#¢F—7Æ•÷6†÷uöæÖRÀ¢'6W&–W5ö–B#¢""Â&6FÆöuö–B#¢fe÷6†÷rævWB‚&6FÆöuö–B"’÷"""À¢&6÷fW"#¢6÷fW"Â'6V6öâ#¢–çB‡W6öÖ–ærævWB‚'6V6öâ"’÷"’À¢&W—6öFUöçVÒ#¢–çB‡W6öÖ–ærævWB‚&W—6öFUöçVÒ"’÷"’À¢'F—FÆR#¢W6öÖ–ærævWB‚'F—FÆR"’÷"$W—6öFR"À¢&—&FFR#¢W6öÖ–ærævWB‚&—&FFR"’÷"""À¢&—'7F×#¢W6öÖ–ærævWB‚&—'7F×"’÷"""Â&—%÷G2#¢WG7Ò¢–bW‡FW&æÃ ¢G'“ ¢WG2Ò†FFWF–ÖRæFFWF–ÖRæg&öÖ—6öf÷&ÖB†W‡FW&æÅ²&—'7F×%Òç&WÆ6R‚%¢"Â"³£"’’çF–ÖW7F×‚¢–bW‡FW&æÂævWB‚&—'7F×"’VÇ6RF–ÖRæÖ·F–ÖR‡F–ÖRç7G'F–ÖR†W‡FW&æÂævWB‚&—&FFR"’÷"""Â"U’ÒVÒÒVB"’’¢W†6WB…fÇVTW'&÷"Â÷fW&fÆ÷tW'&÷"ÂG—TW'&÷"“ ¢WG2Ò ¢–bWG2ãÒF–ÖRçF–ÖR‚’Òƒ3¢#B¢3c“ ¢&÷w2æVæB‡²&–B#¢æöæRÂ'6†÷uöæÖR#¢F—7Æ•÷6†÷uöæÖRÀ¢'6W&–W5ö–B#¢""Â&6FÆöuö–B#¢fe÷6†÷rævWB‚&6FÆöuö–B"’÷"""À¢&6÷fW"#¢6÷fW"Â'6V6öâ#¢–çB†W‡FW&æÂævWB‚'6V6öâ"’÷"’À¢&W—6öFUöçVÒ#¢–çB†W‡FW&æÂævWB‚&W—6öFUöçVÒ"’÷"’À¢'F—FÆR#¢W‡FW&æÂævWB‚'F—FÆR"’÷"$W—6öFR"Â&W‡FVç6–öâ#¢""À¢&FFVB#¢WG2Â&—%÷G2#¢WG2Â&f–Æ&ÆR#¢fÇ6WÒ¢W†6WBW†6WF–öã ¢W'&÷'2³Ò¢6öçF–çVP¢G'“ ¢FFÒ‚ç6W&–W5ö–æfò‡6W&–W5ö–B’÷"·Ð¢–æfòÒFFævWB‚&–æfò"’÷"·Ð¢–bæ÷B—6–ç7Fæ6R†–æfòÂF–7B“ ¢–æfòÒ·Ð¢6†÷uöæÖRÒ7G"†–æfòævWB‚&æÖR"’÷"–æfòævWB‚'F—FÆR"’÷ ¢fe÷6†÷rævWB‚&æÖR"’÷"%6†÷r"¢F—7Æ•÷6†÷uöæÖRÒö6ÆVå÷6†÷u÷F—FÆR‡6†÷uöæÖR’÷"6†÷uöæÖP¢–V%÷FW‡BÒ""æ¦ö–â‡7G"‡fÇVR÷"""’f÷"fÇVR–à¢†fe÷6†÷rævWB‚'–V""’Â–æfòævWB‚'&VÆV6TFFR"’À¢–æfòævWB‚'&VÆV6UöFFR"’Â6†÷uöæÖR’¢–V%öÖF6‚Ò&Rç6V&6‚‡""ƒó£—Ã#•ÆG³'Ò"Â–V%÷FW‡B¢6†÷u÷–V"Ò–çB‡–V%öÖF6‚æw&÷Wƒ’’–b–V%öÖF6‚VÇ6R ¢&uöW—6öFW2ÒFFævWB‚&W—6öFW2"’÷"·Ð¢–b—6–ç7Fæ6R‡&uöW—6öFW2ÂÆ—7B“ ¢w&÷WVBÒ·Ð¢f÷"W–â&uöW—6öFW3 ¢w&÷WVBç6WFFVfVÇB‡7G"†WævWB‚'6V6öâ"’÷"’ÂµÒ’æVæB†W¢&uöW—6öFW2Òw&÷WV@¢6æF–FFW2ÒµÐ¢f÷"6V6öåö¶W’ÂW—6öFW2–â&uöW—6öFW2æ—FV×2‚“ ¢–bæ÷B—6–ç7Fæ6R†W—6öFW2ÂÆ—7B“ ¢6öçF–çVP¢G'“ ¢6V6öåöçVÒÒ–çB‡6V6öåö¶W’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢6V6öåöçVÒÒ ¢f÷"–æFW‚ÂW—6öFR–âVçVÖW&FR†W—6öFW2Â“ ¢G'“ ¢W—6öFUöçVÒÒ–çB†W—6öFRævWB‚&W—6öFUöçVÒ"’÷"–æFW‚¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢W—6öFUöçVÒÒ–æFW€¢6æF–FFW2æVæB‚‡6V6öåöçVÒÂW—6öFUöçVÒÂW—6öFR’¢6÷fW"Ò7G"†fe÷6†÷rævWB‚&6÷fW""’÷"–æfòævWB‚&6÷fW""’÷ ¢–æfòævWB‚&Ö÷f–Uö–ÖvR"’÷"""’ç7G&—‚¢–bæ÷B6÷fW"ç7F'G7v—F‚‚‚&‡GG¢òò"Â&‡GG3¢òò"’“ ¢6÷fW"Ò" ¢&÷f–FW%÷&÷rÒæöæP¢&÷f–FW%ö¶W’Ò‚ÓÂÓ¢–b6æF–FFW3 ¢6V6öåöçVÒÂW—6öFUöçVÒÂW—6öFRÒÖ‚€¢6æF–FFW2Â¶W“ÖÆÖ&F—FVÓ¢†—FVÕ³ÒÂ—FVÕ³Ò’¢&÷f–FW%ö¶W’Ò‡6V6öåöçVÒÂW—6öFUöçVÒ¢G'“ ¢FFVBÒ–çB†fÆöB†W—6öFRævWB‚&FFVB"’÷"’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢FFVBÒ ¢W—6öFUö–æfòÒW—6öFRævWB‚&–æfò"’÷"·Ð¢–bæ÷B—6–ç7Fæ6R†W—6öFUö–æfòÂF–7B“ ¢W—6öFUö–æfòÒ·Ð¢W—6öFUöFFRÒ""æ¦ö–â‡7G"‡fÇVR÷"""’f÷"fÇVR–à¢†W—6öFUö–æfòævWB‚'&VÆV6TFFR"’À¢W—6öFUö–æfòævWB‚'&VÆV6VFFR"’À¢W—6öFUö–æfòævWB‚&—%öFFR"’À¢W—6öFRævWB‚'&VÆV6TFFR"’’¢W—6öFU÷G2Ò ¢FFUöÖF6‚Ò&Rç6V&6‚‡""ƒó£—Ã#•ÆG³'ÒÕÆG³'ÒÕÆG³'Ò"À¢W—6öFUöFFR¢–bFFUöÖF6ƒ ¢G'“ ¢W—6öFU÷G2ÒF–ÖRæÖ·F–ÖR‡F–ÖRç7G'F–ÖR€¢FFUöÖF6‚æw&÷Wƒ’Â"U’ÒVÒÒVB"’¢W†6WB…fÇVTW'&÷"Â÷fW&fÆ÷tW'&÷"“ ¢W—6öFU÷G2Ò ¢–bæ÷BW—6öFU÷G2æBFFVBâ ¢W—6öFU÷G2ÒFFV@¢–bW—6öFRævWB‚&–B"’—2æ÷BæöæS ¢&÷f–FW%÷&÷rÒ°¢&–B#¢W—6öFRævWB‚&–B"’Â'6†÷uöæÖR#¢6†÷uöæÖRÀ¢'6W&–W5ö–B#¢6W&–W5ö–BÂ&6÷fW"#¢6÷fW"À¢'6V6öâ#¢6V6öåöçVÒÂ&W—6öFUöçVÒ#¢W—6öFUöçVÒÀ¢'F—FÆR#¢ö6ÆVåöW—6öFU÷F—FÆR€¢W—6öFRævWB‚'F—FÆR"’÷"b$W—6öFR¶W—6öFUöçV×Ò"À¢6†÷uöæÖR’À¢&W‡FVç6–öâ#¢W—6öFRævWB‚&6öçF–æW%öW‡FVç6–öâ"’÷"&×B"À¢&FFVB#¢W—6öFU÷G2Â&f–Æ&ÆR#¢G'VWÐ¢2ÖW&vRÆÂVÆ—G’÷&÷f–FW"f&–çG2–çFòF†—2öæRÆFW7B6&Bà¢6W&–W5ö–G2Òfe÷6†÷rævWB‚'6W&–W5ö–G2"’÷"·6W&–W5ö–EÐ¢–b6–&Æ–æw3 ¢6W&–W5ö–G2Ò6–&Æ–æw0¢f&–çE÷&÷w2ÒµÐ¢f÷"f&–çEö–B–â6W&–W5ö–G3 ¢G'“ ¢f&–çE÷&÷rÂ÷f&–çEö–æfòÒöÆFW7E÷&÷f–FW%÷f&–çB€¢‚Âf&–çEö–BÂ6†÷uöæÖR¢–bf&–çE÷&÷ræBf&–çE÷&÷rævWB‚&–B"’—2æ÷BæöæS ¢f&–çE÷&÷w2æVæB‡f&–çE÷&÷r¢W†6WBW†6WF–öã ¢6öçF–çVP¢–bf&–çE÷&÷w3 ¢&÷f–FW%ö¶W’ÒÖ‚‡&÷u²&¶W’%Òf÷"&÷r–âf&–çE÷&÷w2¢ÖF6†–ærÒ·&÷rf÷"&÷r–âf&–çE÷&÷w0¢–b&÷u²&¶W’%ÒÓÒ&÷f–FW%ö¶W•Ð¢f—'7BÒÖF6†–æu³Ð¢&÷f–FW%÷&÷rÒ°¢&–B#¢f—'7E²&–B%ÒÂ'6†÷uöæÖR#¢ö6ÆVå÷6†÷u÷F—FÆR‡6†÷uöæÖR’À¢'6W&–W5ö–B#¢6W&–W5ö–BÂ&6÷fW"#¢6÷fW"À¢'6V6öâ#¢f—'7E²'6V6öâ%ÒÀ¢&W—6öFUöçVÒ#¢f—'7E²&W—6öFUöçVÒ%ÒÀ¢'F—FÆR#¢f—'7E²'F—FÆR%ÒÀ¢&W‡FVç6–öâ#¢f—'7E²&W‡FVç6–öâ%ÒÀ¢'6÷W&6W2#¢·²&–B#¢&÷u²&–B%ÒÂ&W‡FVç6–öâ#¢&÷u²&W‡FVç6–öâ%ÒÀ¢&Æ&VÂ#¢&÷u²&Æ&VÂ%×Òf÷"&÷r–âÖF6†–æuÒÀ¢&FFVB#¢Ö‚‡&÷u²&FFVB%Òf÷"&÷r–âÖF6†–ær’À¢&f–Æ&ÆR#¢G'VWÐ¢66†VGVÆRÒ÷GfÖ¦UöW—6öFU÷66†VGVÆR€¢6†÷uöæÖRÂ6†÷u÷–V"Âf÷&6S×&Vg&W6…öW‡FW&æÂ¢W‡FW&æÂÒ66†VGVÆRævWB‚&ÆFW7B"’÷"·Ð¢W6öÖ–ærÒ66†VGVÆRævWB‚'W6öÖ–ær"’÷"·Ð¢–bW6öÖ–æs ¢W6öÖ–æu÷G2Ò ¢G'“ ¢–bW6öÖ–ærævWB‚&—'7F×"“ ¢W6öÖ–æu÷G2ÒFFWF–ÖRæFFWF–ÖRæg&öÖ—6öf÷&ÖB€¢W6öÖ–æu²&—'7F×%Òç&WÆ6R‚%¢"Â"³£"’’çF–ÖW7F×‚¢VÇ6S ¢W6öÖ–æu÷G2ÒF–ÖRæÖ·F–ÖR‡F–ÖRç7G'F–ÖR€¢W6öÖ–ærævWB‚&—&FFR"’÷"""Â"U’ÒVÒÒVB"’¢W†6WB…fÇVTW'&÷"Â÷fW&fÆ÷tW'&÷"ÂG—TW'&÷"“ ¢W6öÖ–æu÷G2Ò ¢W6öÖ–æu÷&÷w2æVæB‡°¢'6†÷uöæÖR#¢F—7Æ•÷6†÷uöæÖRÂ'6W&–W5ö–B#¢6W&–W5ö–BÀ¢&6÷fW"#¢6÷fW"À¢'6V6öâ#¢–çB‡W6öÖ–ærævWB‚'6V6öâ"’÷"’À¢&W—6öFUöçVÒ#¢–çB‡W6öÖ–ærævWB‚&W—6öFUöçVÒ"’÷"’À¢'F—FÆR#¢W6öÖ–ærævWB‚'F—FÆR"’÷"$W—6öFR"À¢&—&FFR#¢W6öÖ–ærævWB‚&—&FFR"’÷"""À¢&—'7F×#¢W6öÖ–ærævWB‚&—'7F×"’÷"""À¢&—%÷G2#¢W6öÖ–æu÷G7Ò¢W‡FW&æÅö¶W’Ò†–çB†W‡FW&æÂævWB‚'6V6öâ"’÷"Ó’À¢–çB†W‡FW&æÂævWB‚&W—6öFUöçVÒ"’÷"Ó’¢W‡FW&æÅ÷G2Ò ¢–bW‡FW&æÂævWB‚&—'7F×"“ ¢G'“ ¢W‡FW&æÅ÷G2ÒFFWF–ÖRæFFWF–ÖRæg&öÖ—6öf÷&ÖB€¢W‡FW&æÅ²&—'7F×%Òç&WÆ6R‚%¢"Â"³£"’’çF–ÖW7F×‚¢W†6WB…fÇVTW'&÷"Â÷fW&fÆ÷tW'&÷"ÂG—TW'&÷"“ ¢W‡FW&æÅ÷G2Ò ¢VÆ–bW‡FW&æÂævWB‚&—&FFR"“ ¢G'“ ¢W‡FW&æÅ÷G2ÒF–ÖRæÖ·F–ÖR‡F–ÖRç7G'F–ÖR€¢W‡FW&æÅ²&—&FFR%ÒÂ"U’ÒVÒÒVB"’¢W†6WB…fÇVTW'&÷"Â÷fW&fÆ÷tW'&÷"“ ¢W‡FW&æÅ÷G2Ò ¢–b&÷f–FW%÷&÷ræB&÷f–FW%ö¶W’ÓÒW‡FW&æÅö¶W’æBW‡FW&æÅ÷G3 ¢&÷f–FW%÷&÷u²&—%÷G2%ÒÒW‡FW&æÅ÷G0¢–bæ÷B&÷f–FW%÷&÷rævWB‚&FFVB"“ ¢&÷f–FW%÷&÷u²&FFVB%ÒÒW‡FW&æÅ÷G0¢7WFöfbÒF–ÖRçF–ÖR‚’Òƒ3¢#B¢c¢c¢–b†W‡FW&æÂæBW‡FW&æÅö¶W’â&÷f–FW%ö¶W’æ@¢W‡FW&æÅ÷G2ãÒ7WFöfb“ ¢&÷w2æVæB‡²&–B#¢æöæRÂ'6†÷uöæÖR#¢F—7Æ•÷6†÷uöæÖRÀ¢'6W&–W5ö–B#¢6W&–W5ö–BÂ&6÷fW"#¢6÷fW"À¢'6V6öâ#¢W‡FW&æÅö¶W•³ÒÀ¢&W—6öFUöçVÒ#¢W‡FW&æÅö¶W•³ÒÀ¢'F—FÆR#¢W‡FW&æÂævWB‚'F—FÆR"’÷"$W—6öFR"À¢&W‡FVç6–öâ#¢""Â&FFVB#¢W‡FW&æÅ÷G2À¢&f–Æ&ÆR#¢fÇ6WÒ¢VÆ–b&÷f–FW%÷&÷ræB&÷f–FW%÷&÷u²&FFVB%ÒãÒ7WFöfc ¢&÷w2æVæB‡&÷f–FW%÷&÷r¢W†6WBW†6WF–öã ¢W'&÷'2³Ò¢&÷w2ç6÷'B†¶W“ÖÆÖ&F—FVÓ¢†—FVÒævWB‚&FFVB"’÷"À¢—FVÒævWB‚'6V6öâ"’÷"À¢—FVÒævWB‚&W—6öFUöçVÒ"’÷"’Â&WfW'6SÕG'VR¢W6öÖ–æu÷&÷w2ç6÷'B†¶W“ÖÆÖ&F—FVÓ¢—FVÒævWB‚&—%÷G2"’÷"¢÷6fUöÆFW7EöW—6öFW5ö66†R‡‚Â&÷w2ÂW6öÖ–æu÷&÷w5³£3eÒÂW'&÷'2¢&WGW&â6VÆbå÷6VæBƒ#Â²&W—6öFW2#¢&÷w5³¦Æ–Ö—EÒÂ&ÆövvVEö–â#¢‚æ6öæf–wW&VB‚’À¢&†5öÖ÷&R#¢ÆVâ‡&÷w2’âÆ–Ö—BÀ¢'W6öÖ–ær#¢W6öÖ–æu÷&÷w5³£3eÒÀ¢&W'&÷'2#¢W'&÷'7Ò ¢–bRçF‚ÓÒ"ö’÷6†÷r# ¢6W&–W5ö–E÷FW‡BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢6W&–W5ö–G2Ò·6–Bç7G&—‚’f÷"6–B–â6W&–W5ö–E÷FW‡Bç7Æ—B‚"Â"’–b6–Bç7G&—‚•Ð¢&Vg&W6‚Ò‡ævWB‚'&Vg&W6‚"Â²#%Ò•³Ò’ÓÒ# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‡‚æ6öæf–wW&VB‚’æB6W&–W5ö–G2“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B&WVW7B'Ò¢2Ww&FRöÆBöæR×6÷W&6Rff÷&—FW2'’F—66÷fW&–ær6–&Æ–ærf&–çG2à¢G'“ ¢6FÆörÒvWE÷‡G&VÕ÷6W&–W2†6fr¢6VÆV7FVBÒæW‡B‚‡&÷rf÷"&÷r–â6FÆöp¢–b7G"‡&÷rævWB‚'6W&–W5ö–B"’’–â6W&–W5ö–G2’ÂæöæR¢6VÆV7FVEö¶W’Ò÷6†÷uö¶W’‚‡6VÆV7FVB÷"·Ò’ævWB‚&æÖR"’¢–b6VÆV7FVEö¶W“ ¢6W&–W5ö–G2Ò·7G"‡&÷rævWB‚'6W&–W5ö–B"’’f÷"&÷r–â6FÆöp¢–b÷6†÷uö¶W’‡&÷rævWB‚&æÖR"’’ÓÒ6VÆV7FVEö¶W•Ð¢W†6WBW†6WF–öã ¢70¢f&–çG2ÒµÐ¢f÷"6W&–W5ö–B–â6W&–W5ö–G3 ¢G'“ ¢FFÒ‚ç6W&–W5ö–æfò‡6W&–W5ö–BÂ&Vg&W6ƒ×&Vg&W6‚’÷"·Ð¢W†6WBW†6WF–öã ¢6öçF–çVP¢–æfòÒFFævWB‚&–æfò"’÷"·Ð¢–bæ÷B—6–ç7Fæ6R†–æfòÂF–7B“ ¢–æfòÒ·Ð¢f&–çG2æVæB‚‡6W&–W5ö–BÂFFÂ–æfò’¢–bæ÷Bf&–çG3 ¢&WGW&â6VÆbå÷6VæBƒ#Â²&W'&÷"#¢$6÷VÆBæ÷BÆöBF†—26†÷râ'Ò¢6W&–W5ö–BÂFFÂ–æfòÒf&–çG5³Ð¢–bæ÷B—6–ç7Fæ6R†–æfòÂF–7B“ ¢–æfòÒ·Ð¢6÷fW"Ò7G"†–æfòævWB‚&6÷fW""’÷"–æfòævWB‚&Ö÷f–Uö–ÖvR"’÷"""’ç7G&—‚¢–bæ÷B6÷fW"ç7F'G7v—F‚‚‚&‡GG¢òò"Â&‡GG3¢òò"’“ ¢6÷fW"Ò" ¢&u÷6†÷uöæÖRÒ–æfòævWB‚&æÖR"’÷"–æfòævWB‚'F—FÆR"’÷"%6†÷r ¢6†÷uöæÖRÒö6ÆVå÷6†÷u÷F—FÆR‡&u÷6†÷uöæÖR’÷"&u÷6†÷uöæÖP¢&VÆV6U÷FW‡BÒ7G"†–æfòævWB‚'&VÆV6TFFR"’÷"–æfòævWB‚'&VÆV6UöFFR"’÷"6†÷uöæÖR¢–V%öÖF6‚Ò&Rç6V&6‚‡""ƒó£—Ã#•ÆG³'Ò"Â&VÆV6U÷FW‡B¢6†÷u÷–V"Ò–V%öÖF6‚æw&÷Wƒ’–b–V%öÖF6‚VÇ6R" ¢Ö¦Uö6÷fW'2Ò÷GfÖ¦U÷6V6öåö6÷fW'2‡6†÷uöæÖRÂ6†÷u÷–V"¢‡G&VÕ÷6V6öåö6÷fW'2Ò·Ð¢f÷"÷f&–çEö–BÂf&–çEöFFÂ÷f&–çEö–æfò–âf&–çG3 ¢&u÷6V6öç2Òf&–çEöFFævWB‚'6V6öç2"’÷"µÐ¢–bæ÷B—6–ç7Fæ6R‡&u÷6V6öç2ÂÆ—7B“ ¢6öçF–çVP¢f÷"ÖWF–â&u÷6V6öç3 ¢–bæ÷B—6–ç7Fæ6R†ÖWFÂF–7B“ ¢6öçF–çVP¢¶W’ÒÖWFævWB‚'6V6öåöçVÖ&W""¢–b¶W’—2æöæS ¢¶W’ÒÖWFævWB‚'6V6öâ"¢–b¶W’—2æöæS ¢ÖF6‚Ò&Rç6V&6‚‡"%ÆB²"Â7G"†ÖWFævWB‚&æÖR"’÷"""’¢¶W’ÒÖF6‚æw&÷Wƒ’–bÖF6‚VÇ6RæöæP¢'BÒ7G"†ÖWFævWB‚&6÷fW""’÷"ÖWFævWB‚&6÷fW%ö&–r"’÷ ¢ÖWFævWB‚&Ö÷f–Uö–ÖvR"’÷"""’ç7G&—‚¢–b¶W’—2æ÷BæöæRæB'Bç7F'G7v—F‚‚‚&‡GG¢òò"Â&‡GG3¢òò"’“ ¢‡G&VÕ÷6V6öåö6÷fW'5·7G"†¶W’•ÒÒ'@¢W—6öFUöÖÒ·Ð¢f÷"÷f&–çEö–BÂf&–çEöFFÂf&–çEö–æfò–âf&–çG3 ¢f&–çEöæÖRÒf&–çEö–æfòævWB‚&æÖR"’÷"f&–çEö–æfòævWB‚'F—FÆR"’÷"6†÷uöæÖP¢Æ&VÂÒ÷6†÷u÷f&–çEöÆ&VÂ‡f&–çEöæÖR¢&uöW—6öFW2Òf&–çEöFFævWB‚&W—6öFW2"’÷"·Ð¢–b—6–ç7Fæ6R‡&uöW—6öFW2ÂÆ—7B“ ¢w&÷WVBÒ·Ð¢f÷"W–â&uöW—6öFW3 ¢w&÷WVBç6WFFVfVÇB‡7G"†WævWB‚'6V6öâ"’÷"’ÂµÒ’æVæB†W¢&uöW—6öFW2Òw&÷WV@¢f÷"6V6öåö¶W’ÂW2–â&uöW—6öFW2æ—FV×2‚“ ¢–bæ÷B—6–ç7Fæ6R†W2ÂÆ—7B“ ¢6öçF–çVP¢f÷"’ÂW–âVçVÖW&FR†W2Â“ ¢W—6öFUöçVÒÒWævWB‚&W—6öFUöçVÒ"’÷"¢¶W’Ò‡7G"‡6V6öåö¶W’’Â7G"†W—6öFUöçVÒ’¢—FVÒÒW—6öFUöÖç6WFFVfVÇB†¶W’Â°¢&W—6öFUöçVÒ#¢W—6öFUöçVÒÀ¢'F—FÆR#¢ö6ÆVåöW—6öFU÷F—FÆR€¢WævWB‚'F—FÆR"’÷"b$W—6öFR¶—Ò"Â6†÷uöæÖR’À¢'6÷W&6W2#¢µ×Ò¢6÷W&6UöÆ&VÂÒÆ&VÀ¢W6VBÒ·7&5²&Æ&VÂ%Òf÷"7&2–â—FVÕ²'6÷W&6W2%×Ð¢–b6÷W&6UöÆ&VÂ–âW6VC ¢7Vff—‚Ò ¢v†–ÆRb'¶Æ&VÇÒ·7Vff—‡Ò"–âW6VC ¢7Vff—‚³Ò¢6÷W&6UöÆ&VÂÒb'¶Æ&VÇÒ·7Vff—‡Ò ¢—FVÕ²'6÷W&6W2%ÒæVæB‡°¢&–B#¢WævWB‚&–B"’Â&Æ&VÂ#¢6÷W&6UöÆ&VÂÀ¢&W‡FVç6–öâ#¢WævWB‚&6öçF–æW%öW‡FVç6–öâ"’÷"&×B'Ò¢6V6öç2ÒµÐ¢6V6öåöçVÖ&W'2Ò6÷'FVB‡¶¶W•³Òf÷"¶W’–âW—6öFUöÖÒÀ¢¶W“ÖÆÖ&FfÇVS¢–çB‡fÇVR’–bfÇVRæ—6F–v—B‚’VÇ6R“““““’¢f÷"6V6öåö¶W’–â6V6öåöçVÖ&W'3 ¢æ÷&ÖÆ—¦VBÒ¶—FVÒf÷"‡6V6öâÂöçVÖ&W"’Â—FVÒ–âW—6öFUöÖæ—FV×2‚¢–b6V6öâÓÒ6V6öåö¶W•Ð¢æ÷&ÖÆ—¦VBç6÷'B†¶W“ÖÆÖ&FW¢–çB†W²&W—6öFUöçVÒ%Ò’–b7G"†W²&W—6öFUöçVÒ%Ò’æ—6F–v—B‚’VÇ6R“““““’¢6V6öç2æVæB‡²&çVÖ&W"#¢6V6öåö¶W’À¢'F—FÆR#¢b%6V6öâ·6V6öåö¶W—Ò"À¢&6÷fW"#¢†Ö¦Uö6÷fW'2ævWB‡7G"‡6V6öåö¶W’’’÷ ¢‡G&VÕ÷6V6öåö6÷fW'2ævWB‡7G"‡6V6öåö¶W’’’÷"6÷fW"’À¢&W—6öFW2#¢æ÷&ÖÆ—¦VGÒ¢6V6öç2ç6÷'B†¶W“ÖÆÖ&F3¢–çB‡5²&çVÖ&W"%Ò’–b7G"‡5²&çVÖ&W"%Ò’æ—6F–v—B‚’VÇ6R“““““’¢&WGW&â6VÆbå÷6VæBƒ#Â²&æÖR#¢6†÷uöæÖRÂ'6†÷uö¶W’#¢÷6†÷uö¶W’‡6†÷uöæÖR’À¢'6W&–W5ö–B#¢6W&–W5ö–G5³ÒÂ'6W&–W5ö–G2#¢6W&–W5ö–G2À¢&6÷fW"#¢6÷fW"Â'6V6öç2#¢6V6öç7Ò ¢–bRçF‚ÓÒ"ö’÷6†÷uöW‡FW&æÂ# ¢6FÆöuö–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢G'“ ¢ÖWFÒ6–æVÖWFöÖWF‚'6W&–W2"Â6FÆöuö–B¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒ#Â²&W'&÷"#¢$6÷VÆBæ÷BÆöB6†÷rÖWFFF¢"²7G"†R—Ò¢–bæ÷BÖWF ¢&WGW&â6VÆbå÷6VæBƒ#Â²&W'&÷"#¢$6÷VÆBæ÷BÆöBF†—26†÷râ'Ò¢6†÷uöæÖRÒ7G"†ÖWFævWB‚&æÖR"’÷"%6†÷r"¢6†÷uö¶W’Ò÷6†÷uö¶W’‡6†÷uöæÖR¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢&÷f–FW%ö–G2ÒµÐ¢–b‚æ6öæf–wW&VB‚“ ¢G'“ ¢&÷f–FW%ö–G2Ò·&÷rævWB‚'6W&–W5ö–B"’f÷"&÷r–âvWE÷‡G&VÕ÷6W&–W2†6fr¢–b÷6†÷uö¶W’‡&÷rævWB‚&æÖR"’’ÓÒ6†÷uö¶W’æ@¢&÷rævWB‚'6W&–W5ö–B"’—2æ÷BæöæUÐ¢W†6WBW†6WF–öã ¢&÷f–FW%ö–G2ÒµÐ¢–b&÷f–FW%ö–G3 ¢&WGW&â6VÆbå÷6VæBƒ#Â²&6FÆöuö–B#¢6FÆöuö–BÀ¢'&÷f–FW%÷6W&–W5ö–G2#¢&÷f–FW%ö–G7Ò¢w&÷WVBÒ·Ð¢f÷"f–FVò–âÖWFævWB‚'f–FV÷2"’÷"µÓ ¢6V6öâÒf–FVòævWB‚'6V6öâ"¢W—6öFRÒf–FVòævWB‚&W—6öFR"¢–b6V6öâ—2æöæR÷"W—6öFR—2æöæS ¢6öçF–çVP¢w&÷WVBç6WFFVfVÇB‡7G"‡6V6öâ’ÂµÒ’æVæB‡°¢&W—6öFUöçVÒ#¢W—6öFRÂ'F—FÆR#¢f–FVòævWB‚&æÖR"’÷"b$W—6öFR¶W—6öFWÒ"À¢'&VÆV6VB#¢f–FVòævWB‚'&VÆV6VB"’÷"""Â'6÷W&6W2#¢µ×Ò¢6V6öç2ÒµÐ¢f÷"6V6öâÂW—6öFW2–âw&÷WVBæ—FV×2‚“ ¢W—6öFW2ç6÷'B†¶W“ÖÆÖ&F&÷s¢–çB‡&÷rævWB‚&W—6öFUöçVÒ"’÷"’¢6V6öç2æVæB‡²&çVÖ&W"#¢6V6öâÂ'F—FÆR#¢b%6V6öâ·6V6öçÒ"À¢&6÷fW"#¢ÖWFævWB‚'÷7FW""’÷"""Â&W—6öFW2#¢W—6öFW7Ò¢6V6öç2ç6÷'B†¶W“ÖÆÖ&F&÷s¢–çB‡&÷u²&çVÖ&W"%Ò’–b7G"‡&÷u²&çVÖ&W"%Ò’æ—6F–v—B‚’VÇ6R“““““’¢&WGW&â6VÆbå÷6VæBƒ#Â²&6FÆöuö–B#¢6FÆöuö–BÂ&æÖR#¢6†÷uöæÖRÀ¢'6†÷uö¶W’#¢6†÷uö¶W’Â'6W&–W5ö–B#¢æöæRÂ'6W&–W5ö–G2#¢µÒÀ¢&6÷fW"#¢ÖWFævWB‚'÷7FW""’÷"""Â'–V"#¢ö6FÆöu÷–V"†ÖWF’À¢'&F–ær#¢ÖWFævWB‚&–ÖF%&F–ær"’÷"""Â'6V6öç2#¢6V6öç7Ò ¢–bRçF‚ÓÒ"ö’÷FVÕ÷6V&6‚# ¢FW&ÒÒ‡ævWB‚'"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷BFW&Ó ¢&WGW&â6VÆbå÷6VæBƒ#Â²'FV×2#¢µ×Ò¢7&5öW'"ÒµÐ¢FW&ÕöÂÒFW&ÒæÆ÷vW"‚¢vçFVBÒöW‡æE÷FW&×2‡FW&ÕöÂ¢f÷VæBÒµÐ¢6VVâÒ6WB‚¢G'“ ¢f÷"FVÒ–â6V&6…öf÷FÖö%÷FV×2‡FW&Ò“ ¢Æ÷rÒ7G"‡FVÒævWB‚&æÖR"’÷"""’æÆ÷vW"‚’ç7G&—‚¢–bÆ÷ræBÆ÷ræ÷B–â6VVã ¢6VVâæFB†Æ÷r¢f÷VæBæVæB‡FVÒ¢W†6WBW†6WF–öâ2S ¢7&5öW'"æVæB†b$f÷DÖö"FVÒ6V&6ƒ¢¶WÒ"¢&WGW&â6VÆbå÷6VæBƒ#Â²'FV×2#¢f÷VæBÂ'6÷W&6UöW'&÷'2#¢7&5öW''Ò ¢–bRçF‚ÓÒ"ö’÷FVÕ÷&öf–ÆR# ¢FVÕöæÖRÒ‡ævWB‚&æÖR"Â²"%Ò•³Ò’ç7G&—‚¢FVÕö–BÒ‡ævWB‚&–B"Â²"%Ò•³Ò’ç7G&—‚¢–bæ÷BFVÕö–BæBFVÕöæÖS ¢FVÕö–BÒ&W6öÇfUöf÷FÖö%÷FVÕö–B‡FVÕöæÖR¢&öf–ÆRÒfWF6…÷FVÕ÷&öf–ÆR‡FVÕö–BÂFVÕöæÖR¢&öf–ÆU²'FVÕö–B%ÒÒFVÕö–@¢&öf–ÆU²&Æövò%ÒÒ÷FVÕöÆövõ÷W&Â‡FVÕö–B’–bFVÕö–BVÇ6R" ¢&WGW&â6VÆbå÷6VæBƒ#Â²'&öf–ÆR#¢&öf–ÆWÒ ¢–bRçF‚ÓÒ"ö’ö×•÷FV×2# ¢6frÒÆöEö6öæf–r‚¢6÷VçG&–W2ÒÆ—7B„dõDÔô%ôdÄÄ$4µô4õTåE$”U2¢feöFFÒÆöEöff÷&—FW2‚¢ff÷&—FW2ÒfeöFFævWB‚'FV×2"ÂµÒ¢ff÷&—FW5ö6†ævVBÒfÇ6P¢ÖW&vVBÒ·Ð¢W'&÷'2ÒµÐ¢f÷"ff÷&—FR–âff÷&—FW3 ¢FVÕöæÖRÒ7G"†ff÷&—FRævWB‚&æÖR"’–b—6–ç7Fæ6R†ff÷&—FRÂF–7B¢VÇ6Rff÷&—FR’ç7G&—‚¢FVÕö–BÒ7G"†ff÷&—FRævWB‚'FVÕö–B"’–b—6–ç7Fæ6R†ff÷&—FRÂF–7B¢VÇ6R""’ç7G&—‚¢–bæ÷BFVÕöæÖS ¢6öçF–çVP¢–bæ÷BFVÕö–C ¢FVÕö–BÒ&W6öÇfUöf÷FÖö%÷FVÕö–B‡FVÕöæÖR¢–bFVÕö–BæB—6–ç7Fæ6R†ff÷&—FRÂF–7B’æBæ÷Bff÷&—FRævWB‚'FVÕö–B"“ ¢ff÷&—FU²'FVÕö–B%ÒÒFVÕö–@¢ff÷&—FW5ö6†ævVBÒG'VP¢–bFVÕö–BæB—6–ç7Fæ6R†ff÷&—FRÂF–7B’æBæ÷Bff÷&—FRævWB‚&Æövò"“ ¢ff÷&—FU²&Æövò%ÒÒ÷FVÕöÆövõ÷W&Â‡FVÕö–B¢ff÷&—FW5ö6†ævVBÒG'VP¢G'“ ¢f—‡GW&W2ÒfWF6…÷FVÕ÷66†VGVÆR‡FVÕö–BÂFVÕöæÖR’–bFVÕö–BVÇ6RµÐ¢W†6WBW†6WF–öâ2S ¢W'&÷'2æVæB†b'·FVÕöæÖWÓ¢¶WÒ"¢f—‡GW&W2ÒµÐ¢2vVV²ÖÆöær66†VGVÆR66†R—2f–æRf÷"gWGW&Rf—‡GW&W2Â'WBÆ—fP¢27FFR×W7B6öÖRg&öÒFöF’w26†÷'BÖÆ—fVBfVVBöâWfW'’&VæFW"à¢G'“ ¢F–Ç•÷FVÒÒ6V&6…öF–Ç•öÖF6†W2‡FVÕöæÖRÂFVÕö–B¢W†6WBW†6WF–öâ2S ¢W'&÷'2æVæB†b'·FVÕöæÖWÒÆ—fR7FGW3¢¶WÒ"¢F–Ç•÷FVÒÒµÐ¢f÷"F–Ç’–âF–Ç•÷FVÓ ¢GWÆ–6FRÒæöæP¢FF’Ò7G"†F–Ç’ævWB‚'7F'B"’÷"""•³£Ð¢f÷"f—‡GW&R–âf—‡GW&W3 ¢–bFF’æB7G"†f—‡GW&RævWB‚'7F'B"’÷"""•³£ÒÒFF“ ¢6öçF–çVP¢†öÖUöö²Ò÷FVÕöæÖW5öWV—fÆVçB†f—‡GW&RævWB‚&†öÖR"’ÂF–Ç’ævWB‚&†öÖR"’¢v•öö²Ò÷FVÕöæÖW5öWV—fÆVçB†f—‡GW&RævWB‚&v’"’ÂF–Ç’ævWB‚&v’"’¢–b†öÖUöö²æBv•öö³ ¢GWÆ–6FRÒf—‡GW&P¢'&V°¢–bGWÆ–6FR—2æöæS ¢f—‡GW&W2æVæB†F–7B†F–Ç’Â7FGW5ö¶æ÷vãÕG'VR’¢VÇ6S ¢GWÆ–6FU²&—5öÆ—fR%ÒÒ&ööÂ†F–Ç’ævWB‚&—5öÆ—fR"’¢GWÆ–6FU²&—5öf–æ—6†VB%ÒÒ&ööÂ†F–Ç’ævWB‚&—5öf–æ—6†VB"’¢GWÆ–6FU²&Æ—fUöÖ–çWFR%ÒÒF–Ç’ævWB‚&Æ—fUöÖ–çWFR"¢GWÆ–6FU²&†öÖUö–B%ÒÒGWÆ–6FRævWB‚&†öÖUö–B"’÷"F–Ç’ævWB‚&†öÖUö–B"Â""¢GWÆ–6FU²&v•ö–B%ÒÒGWÆ–6FRævWB‚&v•ö–B"’÷"F–Ç’ævWB‚&v•ö–B"Â""¢GWÆ–6FU²'7FGW5ö¶æ÷vâ%ÒÒG'VP¢W'&÷'2æW‡FVæB†FE÷&–Ö'•÷GeöÆ—7F–æw2†f—‡GW&W2Â6÷VçG&–W2’¢f÷"f—‡GW&R–âf—‡GW&W3 ¢¶W’Ò'Â"æ¦ö–â‚‡7G"†f—‡GW&RævWB‚&†öÖR"Â""’’æÆ÷vW"‚’À¢7G"†f—‡GW&RævWB‚&v’"Â""’’æÆ÷vW"‚’À¢7G"†f—‡GW&RævWB‚'7F'B"Â""’’’¢&÷rÒÖW&vVBævWB†¶W’¢–b&÷r—2æöæS ¢&÷rÒF–7B†f—‡GW&R¢&÷u²&ff÷&—FU÷FV×2%ÒÒµÐ¢ÖW&vVE¶¶W•ÒÒ&÷p¢VÆ–bf—‡GW&RævWB‚&—5öÆ—fR"“ ¢&÷u²&—5öÆ—fR%ÒÒG'VP¢–bf—‡GW&RævWB‚&—5öf–æ—6†VB"“ ¢&÷u²&—5öf–æ—6†VB%ÒÒG'VP¢–bf—‡GW&RævWB‚&Æ—fUöÖ–çWFR"’—2æ÷BæöæS ¢&÷u²&Æ—fUöÖ–çWFR%ÒÒf—‡GW&RævWB‚&Æ—fUöÖ–çWFR"¢–bFVÕöæÖRæ÷B–â&÷u²&ff÷&—FU÷FV×2%Ó ¢&÷u²&ff÷&—FU÷FV×2%ÒæVæB‡FVÕöæÖR¢–bff÷&—FW5ö6†ævVC ¢6fUöff÷&—FW2†feöFF¢f—‡GW&W2Ò6÷'FVB†ÖW&vVBçfÇVW2‚’Â¶W“ÖÆÖ&F&÷s¢&÷rævWB‚'7F'B"’÷"""¢G'“ ¢F÷öf—‡GW&W2ÒfVGW&VEöF–Ç•öf—‡GW&W2‚¢W'&÷'2æW‡FVæB†FE÷&–Ö'•÷GeöÆ—7F–æw2‡F÷öf—‡GW&W2Â6÷VçG&–W2’¢W†6WBW†6WF–öâ2S ¢F÷öf—‡GW&W2ÒµÐ¢W'&÷'2æVæB†b$f÷DÖö"fVGW&VBf—‡GW&W3¢¶WÒ"¢2‡–G&FRGW&&ÆR6†ææVÂÖF6†W2&Vf÷&RF†RvR&VæFW'2âF†P¢26Æ–VçBÖ’&Vg&W6‚7FÆRVçG&–W2ÆFW"Â'WBæWfW"æVVG2Fð¢2&WÆ6RâÇ&VG’Ö¶æ÷vâÖF6‚v—F‚6†V6¶–ærÆ6V†öÆFW"à¢‚Ò‡G&VÒ†6fr¢–b‚æ6öæf–wW&VB‚“ ¢7F÷&VEöf–Æ&–Æ—G’ÒöÆöE÷7÷'G5öF—6µö66†R†6frÂ‚¢f÷"f—‡GW&R–âf—‡GW&W2²F÷öf—‡GW&W3 ¢7F÷&VBÒ7F÷&VEöf–Æ&–Æ—G’ævWB…÷7÷'G5öWfVçEö¶W’€¢f—‡GW&RævWB‚&†öÖR"’Âf—‡GW&RævWB‚&v’"’À¢f—‡GW&RævWB‚'7F'B"’’¢–b—6–ç7Fæ6R‡7F÷&VBÂF–7B’æB—6–ç7Fæ6R‡7F÷&VBævWB‚'&W7VÇB"’ÂF–7B“ ¢f—‡GW&RçWFFR…öVæf÷&6Uöf—‡GW&U÷6V7W&UöÖF6†W2€¢f—‡GW&RÂ÷7÷'G5÷&W7VÇEöf÷%ö6Æ–VçB‡7F÷&VE²'&W7VÇB%ÒÂ‚’’¢&WGW&â6VÆbå÷6VæBƒ#Â²&f—‡GW&W2#¢f—‡GW&W2À¢'F÷öf—‡GW&W2#¢F÷öf—‡GW&W2À¢'6÷W&6UöW'&÷'2#¢Æ—7B†F–7Bæg&öÖ¶W—2†W'&÷'2’—Ò ¢–bRçF‚ÓÒ"ö’÷6V&6‚# ¢FW&ÒÒ‡ævWB‚'"Â²"%Ò•³Ò’ç7G&—‚¢6VÆV7FVE÷FVÕö–BÒ‡ævWB‚'FVÕö–B"Â²"%Ò•³Ò’ç7G&—‚¢–b6VÆV7FVE÷FVÕö–BæBæ÷B6VÆV7FVE÷FVÕö–Bæ—6F–v—B‚“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&–çfÆ–BFVÒ–B'Ò¢–bæ÷BFW&Ó ¢&WGW&â6VÆbå÷6VæBƒ#Â²&f—‡GW&W2#¢µÒÂ&ÆövvVEö–â#¢fÇ6WÒ¢6frÒÆöEö6öæf–r‚¢6÷VçG&–W2ÒÆ—7B„dõDÔô%ôdÄÄ$4µô4õTåE$”U2¢f—‡GW&W2Â7&5öW'"Â&W6öÇfVE÷FVÕö–BÒ6ö×ÆWFU÷FVÕöf—‡GW&W2€¢FW&ÒÂ6VÆV7FVE÷FVÕö–BÂ6÷VçG&–W2¢–b6VÆV7FVE÷FVÕö–C ¢f—‡GW&W2Ò¶f—‡GW&Rf÷"f—‡GW&R–âf—‡GW&W0¢–b6VÆV7FVE÷FVÕö–B–â°¢7G"†f—‡GW&RævWB‚&†öÖUö–B"’÷"""’À¢7G"†f—‡GW&RævWB‚&v•ö–B"’÷"""—ÕÐ¢‚Ò‡G&VÒ†6fr¢ÆövvVEö–âÒ‚æ6öæf–wW&VB‚¢6†ææVÇ2Â6G2ÒµÒÂ·Ð¢–bÆövvVEö–ã ¢G'“ ¢6†ææVÇ2Â6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢W†6WBW†6WF–öâ2S ¢7&5öW'"æVæB†b%‡G&VÓ¢¶WÒ"¢ÆövvVEö–âÒfÇ6P¢G'“ ¢F‡"ÒfÆöB‡ævWB‚'7G&–7FæW72"Â¶6frævWB‚&ÖF6…÷F‡&W6†öÆB"Âãc"•Ò•³Ò¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢F‡"ÒfÆöB†6frævWB‚&ÖF6…÷F‡&W6†öÆB"Âãc"’÷"ãc"¢F‡"ÒÖ‚ƒãCÂÖ–âƒãƒÂF‡"’¢ÖF6…ö6frÒF–7B†6frÂÖF6…÷F‡&W6†öÆC×F‡"¢eö6G2Òeö6FVv÷&–W2†6†ææVÇ2Â6G2’–bÆövvVEö–âVÇ6RµÐ¢7÷'G5öF—6²ÒöÆöE÷7÷'G5öF—6µö66†R†ÖF6…ö6frÂ‚’–bÆövvVEö–âVÇ6R·Ð¢WuöF—66÷fW&–W2Ò·Ð¢fuöF—66÷fW&–W2Ò·Ð¢–bÆövvVEö–ã ¢öÆöEöWuöF—6µö66†R‡‚¢WuöF—66÷fW&–W2Òö66†VEöWuöF—66÷fW'’€¢f—‡GW&W2Â6†ææVÇ2Â6G2Â‚¢fuöF—66÷fW&–W2Ò÷fuöf—‡GW&UöF—66÷fW&–W2€¢f—‡GW&W2Â6†ææVÇ2Â6G2Â‚¢÷WBÒµÐ¢f÷"b–âf—‡GW&W3 ¢ÖF6†W2ÒµÐ¢eö†—G2ÒµÐ¢7G&VÖ–æuööæÇ’ÒfÇ6P¢–bÆövvVEö–ã ¢F—6µö¶W’Ò÷7÷'G5öWfVçEö¶W’€¢bævWB‚&†öÖR"’ÂbævWB‚&v’"’ÂbævWB‚'7F'B"’¢66†Uö¶W’Ò…÷föEö66†Uö¶W’‡‚’Â7G"‡F‡"’ÂF—6µö¶W’¢66†VBÒõ5õ%E5ôUdTåEô4„ääTÅô44„RævWB†66†Uö¶W’¢–bæ÷B66†VC ¢7F÷&VBÒ7÷'G5öF—6²ævWB†F—6µö¶W’¢–b†—6–ç7Fæ6R‡7F÷&VBÂF–7B’æ@¢—6–ç7Fæ6R‡7F÷&VBævWB‚'&W7VÇB"’ÂF–7B’“ ¢66†VBÒ°¢'G2#¢fÆöB‡7F÷&VBævWB‚'G2"’÷"’À¢'&W7VÇB#¢öVæf÷&6Uöf—‡GW&U÷6V7W&UöÖF6†W2€¢bÂ÷7÷'G5÷&W7VÇEöf÷%ö6Æ–VçB€¢7F÷&VE²'&W7VÇB%ÒÂ‚’’À¢Ð¢–b†66†VBæBF–ÖRçF–ÖR‚’ÒfÆöB†66†VBævWB‚'G2"’÷"’À¢õ5õ%E5ôUdTåEô4„ääTÅõEDÂ“ ¢&W7VÇBÒF–7B†66†VBævWB‚'&W7VÇB"’÷"·Ò¢VÇ6S ¢&W7VÇBÒöÖF6…÷7÷'G5öf—‡GW&Uö6†ææVÇ2€¢bÂÖF6…ö6frÂ6†ææVÇ2Â6G2Â‚¢&W7VÇBÒöFEöWuöF—66÷fW&–W2€¢&W7VÇBÂWuöF—66÷fW&–W2ævWB†F—6µö¶W’ÂµÒ’¢&W7VÇBÒöFEöWuöF—66÷fW&–W2€¢&W7VÇBÂfuöF—66÷fW&–W2ævWB†F—6µö¶W’ÂµÒ’¢&W7VÇBÒöVæf÷&6Uöf—‡GW&U÷6V7W&UöÖF6†W2†bÂ&W7VÇB¢õ5õ%E5ôUdTåEô4„ääTÅô44„U¶66†Uö¶W•ÒÒ°¢'G2#¢F–ÖRçF–ÖR‚’Â'&W7VÇB#¢&W7VÇGÐ¢7÷'G5öF—6µ¶F—6µö¶W•ÒÒ°¢'G2#¢F–ÖRçF–ÖR‚’À¢'&W7VÇB#¢÷7÷'G5÷&W7VÇEöf÷%÷7F÷&vR‡&W7VÇB—Ð¢ÖF6†W2Ò&W7VÇBævWB‚&ÖF6†W2"’÷"µÐ¢eö†—G2Ò&W7VÇBævWB‚'eö†—G2"’÷"µÐ¢2f—‡GW&RöWfVçB6†ææVÇ2&R–æFWVæFVçBöb'&öF67FW ¢2Æ—7F–æw2æB&VÖ–âVÆ–v–&ÆRWfVâv†VâæòwV–FRW†—7G2à¢ÆÅö&67FW'2Ò¶"f÷"æÖW2–âe²&'•ö6÷VçG'’%ÒçfÇVW2‚’f÷""–âæÖW5Ð¢†5öÆ–æV"Òç’†æ÷Bö—5÷7G&VÖ–ær†"’f÷""–âÆÅö&67FW'2¢†5÷7G&VÖ–ærÒç’…ö—5÷7G&VÖ–ær†"’f÷""–âÆÅö&67FW'2¢2&öæÇ’7G&VÖ–ær"ÒæòÆ–æV"'&öF67FW"äBæòæ÷&ÖÂÖF6†W0¢7G&VÖ–æuööæÇ’Ò††5÷7G&VÖ–æræBæ÷B†5öÆ–æV"æBæ÷BÖF6†W2¢÷WBæVæB‡²&†öÖR#¢e²&†öÖR%ÒÂ&v’#¢e²&v’%ÒÂ'7F'B#¢e²'7F'B%ÒÀ¢&†öÖUö–B#¢bævWB‚&†öÖUö–B"Â""’À¢&v•ö–B#¢bævWB‚&v•ö–B"Â""’À¢&'•ö6÷VçG'’#¢e²&'•ö6÷VçG'’%ÒÂ&ÖF6†W2#¢ÖF6†W2À¢&Æ—7F–æu÷6÷W&6R#¢bævWB‚&Æ—7F–æu÷6÷W&6R"Â""’À¢'eö†—G2#¢eö†—G2Â'7G&VÖ–æuööæÇ’#¢7G&VÖ–æuööæÇ’À¢&—5öÆ—fR#¢&ööÂ†bævWB‚&—5öÆ—fR"’’À¢&—5öf–æ—6†VB#¢&ööÂ†bævWB‚&—5öf–æ—6†VB"’’À¢&Æ—fUöÖ–çWFR#¢bævWB‚&Æ—fUöÖ–çWFR"’À¢&ÆVwVUöæÖR#¢bævWB‚&ÆVwVUöæÖR"Â""’À¢&ÆVwVUö–B#¢bævWB‚&ÆVwVUö–B"Â""—Ò¢–bÆövvVEö–ã ¢÷6fU÷7÷'G5öF—6µö66†R†ÖF6…ö6frÂ‚Â7÷'G5öF—6²¢&WGW&â6VÆbå÷6VæBƒ#Â²&f—‡GW&W2#¢÷WBÂ&ÆövvVEö–â#¢ÆövvVEö–âÀ¢'6÷W&6UöW'&÷'2#¢7&5öW'"À¢'eö6FVv÷&–W2#¢eö6G7Ò ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&æ÷Bf÷VæB'Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&W'&÷"#¢7G"†R—Ò ¢FVb÷÷7Eö6÷&Uö’‡6VÆbÂF‚Â–ÆöB“ ¢–bF‚ÓÒ"ö’ö7F—f—G’# ¢öÖ&µöö7F—f—G’‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VWÒ¢–bF‚ÓÒ"ö’÷6‡WFF÷vâ# ¢6VÆbå÷6VæBƒ#Â²&ö²#¢G'VWÒ¢õ5DõôUdTåBç6WB‚¢&WGW&à¢–bF‚ÓÒ"ö’÷&W7F'B# ¢2&VÆVæ6‚EdÖFRv—F†÷WB7v–ærç’f–ÆW2Â6òÆö6ÆÇ’VF—FV@¢2GfÖFRç’—2–6¶VBWöâF†RæW‡B7F'Bâ&WW6W2F†R6ÖP¢2ÆVæ6†W"ÖFWFV7F–öâ'VÆW22F†RWFFW#¢öæÇ’&VÆVæ6‚&VÀ¢2W&ÖæVçBæW†Rò–çFW'&WFW"ÂæWfW"FV×ÖW‡G&7FVB—F†öâà¢G'“ ¢–bæ÷B&ööÂ†ÆöEö6öæf–r‚’ævWB‚&FWeöÖöFR"’“ ¢&WGW&â6VÆbå÷6VæBƒC2Â²&ö²#¢fÇ6RÀ¢&W'&÷"#¢$FWfVÆ÷W"ÖöFR—2F—6&ÆVB'Ò¢ÆVæ6†W%öW†RÒ÷2æVçf—&öâævWB‚%EdÔDUôU„R"¢7W"Ò÷2çF‚æ¦ö–â†öF—"‚’Â'GfÖFRç’"¢&VÆVæ6‚ÒæöæP¢–bÆVæ6†W%öW†RæB÷2çF‚æW†—7G2†ÆVæ6†W%öW†R’æBÆVæ6†W%öW†RæÆ÷vW"‚’æVæG7v—F‚‚"æW†R"“ ¢&VÆVæ6‚Òr"r²ÆVæ6†W%öW†R²r"p¢VÆ–bvWFGG"‡7—2Â&g&÷¦Vâ"ÂfÇ6R’æB÷2çF‚æW†—7G2‡7—2æ&we³Ò“ ¢&VÆVæ6‚Òr"r²7—2æ&we³Ò²r"p¢VÆ–bæ÷BvWFGG"‡7—2Â&g&÷¦Vâ"ÂfÇ6R’æB'FV×"æ÷B–â‡7—2æW†V7WF&ÆR÷"""’æÆ÷vW"‚“ ¢&VÆVæ6‚Òr"r²7—2æW†V7WF&ÆR²r""r²7W"²r"p¢–bæ÷B&VÆVæ6ƒ ¢26âwB6fVÇ’WFò×&VÆVæ6ƒ²§W7B7F÷æBFVÆÂF†R6Æ–VçBà¢6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'&VÆVæ6‚#¢fÇ6WÒ¢õ5DõôUdTåBç6WB‚¢&WGW&à¢–b7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"“ ¢†VÇW"Ò÷2çF‚æ¦ö–â†öF—"‚’Â%÷&W7F'Bæ&B"¢ÆVæ6†W%öæÖRÒ÷2çF‚æ&6VæÖR†ÆVæ6†W%öW†R÷"""¢¶æ÷våöÆVæ6†W"Ò&ööÂ‡&RægVÆÆÖF6‚€¢""ƒó¤õEd×ÄöÆ÷5EdÖFR’ƒó¥Ç2¥Â…ÆBµÂ’“õÂæW†R"À¢ÆVæ6†W%öæÖRÂfÆw3×&Rä”täõ$T44R’¢Æ–æW2Ò²$V6†òöfeÇ%Æâ"À¢v6BöB"r²öF—"‚’²r%Ç%ÆârÀ¢'F–ÖV÷WB÷B"öæö'&V²æçVÅÇ%Æâ%Ð¢–b¶æ÷våöÆVæ6†W# ¢Æ–æW2æW‡FVæB…²wF6¶¶–ÆÂöbö–Ò"r²ÆVæ6†W%öæÖR²r"æçVÂ#âcÇ%ÆârÀ¢'F–ÖV÷WB÷Böæö'&V²æçVÅÇ%Æâ%Ò¢Æ–æW2æW‡FVæB…²w7F'B""r²&VÆVæ6‚²%Ç%Æâ"À¢'F–ÖV÷WB÷B"öæö'&V²æçVÅÇ%Æâ"À¢vFVÂ"Wæc%Ç%ÆâuÒ¢v—F‚÷Vâ††VÇW"Â'r"ÂVæ6öF–æsÒ'WFbÓ‚"ÂæWvÆ–æSÒ""’2c ¢bçw&—FVÆ–æW2†Æ–æW2¢fÆw2ÒvWFGG"‡7V'&ö6W72Â$5$TDUôäõõt”äDõr"Âƒƒ¢7V'&ö6W72å÷Vâ…²&6ÖBæW†R"Â"öB"Â"ö2"Â†VÇW%ÒÀ¢7vCÖöF—"‚’Â7&VF–öæfÆw3ÖfÆw2À¢7FF–ã×7V'&ö6W72äDUdåTÄÂÂ7FF÷WC×7V'&ö6W72äDUdåTÄÂÀ¢7FFW'#×7V'&ö6W72äDUdåTÄÂÂ6Æ÷6UöfG3ÕG'VR¢VÇ6S ¢†VÇW"Ò÷2çF‚æ¦ö–â†öF—"‚’Â%÷&W7F'Bç6‚"¢&öG’Ò"2ö&–â÷6…Æç6ÆVW%Æâ"²&VÆVæ6‚²"eÆç&ÒÒÒÂ"CÂ%Æâ ¢v—F‚÷Vâ††VÇW"Â'r"ÂVæ6öF–æsÒ'WFbÓ‚"’2c ¢bçw&—FR†&öG’¢÷2æ6†ÖöB††VÇW"ÂósSR¢7V'&ö6W72å÷Vâ…²"ö&–â÷6‚"Â†VÇW%ÒÂ7F'EöæWu÷6W76–öãÕG'VR¢6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'&VÆVæ6‚#¢G'VRÀ¢&–ç7Fæ6R#¢õ4U%dU%ô”å5Dä4Uô”GÒ¢FVbö'–R‚“ ¢–×÷'BF–ÖR2÷C²÷Bç6ÆVWƒ“²÷2åöW†—Bƒ¢–×÷'BF‡&VF–ær2÷Fƒ²÷F‚åF‡&VB‡F&vWCÕö'–RÂFVÖöãÕG'VR’ç7F'B‚¢&WGW&à¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&ö²#¢fÇ6RÂ&W'&÷"#¢7G"†R—Ò¢–bF‚ÓÒ"ö’÷FW7Eö7&VFVçF–Ç2# ¢FW7Eö6frÒF–7B„DTdTÅEô4ôäd”r¢FW7Eö6frçWFFR‡²'‡G&VÕö†÷7B#¢7G"‡–ÆöBævWB‚'‡G&VÕö†÷7B"’÷"""’ç7G&—‚’À¢'‡G&VÕ÷÷'B#¢7G"‡–ÆöBævWB‚'‡G&VÕ÷÷'B"’÷"""’ç7G&—‚’À¢'‡G&VÕ÷W6W"#¢7G"‡–ÆöBævWB‚'‡G&VÕ÷W6W""’÷"""’ç7G&—‚’À¢'‡G&VÕ÷72#¢7G"‡–ÆöBævWB‚'‡G&VÕ÷72"’÷"""—Ò¢–bæ÷B‡G&VÒ‡FW7Eö6fr’æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢fÇ6RÂ&W'&÷"#¢$†÷7BÂW6W&æÖRæB77v÷&B&R&WV—&VB'Ò¢ö²Â–æfòÒ‡G&VÒ‡FW7Eö6fr’æÆöv–â‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢ö²Â&–æfò#¢–æfò–bö²VÇ6RæöæRÀ¢&W'&÷"#¢æöæR–bö²VÇ6R–æf÷Ò¢–bF‚ÓÒ"ö’öÖF6…÷7G&–7FæW72# ¢6frÒÆöEö6öæf–r‚¢G'“ ¢7G&–7BÒfÆöB‡–ÆöBævWB‚&ÖF6…÷F‡&W6†öÆB"Â6frævWB‚&ÖF6…÷F‡&W6†öÆB"Âãc"’’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢7G&–7BÒãc ¢7G&–7BÒÖ‚ƒãCÂÖ–âƒãƒÂ7G&–7B’¢6fu²&ÖF6…÷F‡&W6†öÆB%ÒÒ7G&–7@¢6fUö6öæf–r†6fr¢ö6ÆV%÷7÷'G5öWfVçEö6†ææVÅö66†R‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ&ÖF6…÷F‡&W6†öÆB#¢7G&–7GÒ¢–bF‚ÓÒ"ö’÷&6–æu÷6W&–W2# ¢6frÒÆöEö6öæf–r‚¢ÆÆ÷vVBÒ‚&c"Â&c""Â&c2"Â&–æG–6""Â'vV2"Â&f÷&×VÆR"Â&Ö÷Föw"Â'w&2"¢&WVW7FVBÒ–ÆöBævWB‚'6W&–W2"’–b—6–ç7Fæ6R‡–ÆöBævWB‚'6W&–W2"’ÂÆ—7B’VÇ6RµÐ¢6VÆV7FVBÒ¶¶W’f÷"¶W’–âÆÆ÷vVB–b¶W’–â&WVW7FVEÐ¢6fu²'&6–æu÷6W&–W2%ÒÒ6VÆV7FV@¢6fUö6öæf–r†6fr¢ö6ÆV%÷&6–æuöf–Æ&–Æ—G•ö66†R‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'6W&–W2#¢6VÆV7FVGÒ ¢FVbFõõõ5B‡6VÆb“ ¢RÒW&ÆÆ–"ç'6RçW&Ç'6R‡6VÆbçF‚¢–bæ÷B6VÆbåöWF†÷&—¦UöÆâ‡R“ ¢&WGW&à¢ÆVæwF‚Ò–çB‡6VÆbæ†VFW'2ævWB‚$6öçFVçBÔÆVæwF‚"Â’¢–bÆVæwF‚âR¢#B¢#C ¢&WGW&â6VÆbå÷6VæBƒC2Â²&W'&÷"#¢$&6·W÷"&WVW7B—2FöòÆ&vR'Ò¢&rÒ6VÆbç&f–ÆRç&VB†ÆVæwF‚’–bÆVæwF‚VÇ6R"'·Ò ¢G'“ ¢–ÆöBÒ§6öâæÆöG2‡&ræFV6öFR‚'WFbÓ‚"’÷"'·Ò"¢W†6WBW†6WF–öã ¢–ÆöBÒ·Ð¢–bRçF‚–â²"ö’ö7F—f—G’"Â"ö’÷6‡WFF÷vâ"Â"ö’÷&W7F'B"Â"ö’÷FW7Eö7&VFVçF–Ç2"À¢"ö’öÖF6…÷7G&–7FæW72"Â"ö’÷&6–æu÷6W&–W2'Ó ¢&WGW&â6VÆbå÷÷7Eö6÷&Uö’‡RçF‚Â–ÆöB¢–bRçF‚ÓÒ"ö’÷&öf–ÆUö&6·WöW‡÷'B# ¢¶–æBÒ&gVÆÂ"–b–ÆöBævWB‚'G—R"’ÓÒ&gVÆÂ"VÇ6R'&öf–ÆR ¢&WGW&â6VÆbå÷6VæBƒ#Â7&VFU÷&öf–ÆUö&6·W†¶–æBÂ–ÆöBævWB‚'F–ÖVÆ–æR"’’¢–bRçF‚ÓÒ"ö’÷&öf–ÆUö&6·Wö–×÷'B# ¢G'“ ¢&W7VÇBÒ&W7F÷&U÷&öf–ÆUö&6·W‡–ÆöBævWB‚&&6·W"’¢&WGW&â6VÆbå÷6VæBƒ#ÂF–7B‡²&ö²#¢G'VWÒÂ¢§&W7VÇB’¢W†6WB…fÇVTW'&÷"ÂG—TW'&÷"’2S ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢7G"†R—Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&W'&÷"#¢$6÷VÆBæ÷B&W7F÷&R&6·W¢"²7G"†R—Ò¢–bRçF‚ÓÒ"ö’÷7÷'G5öWfVçEö6†ææVÇ2# ¢f—‡GW&RÒ–ÆöBævWB‚&f—‡GW&R"¢–bæ÷B—6–ç7Fæ6R†f—‡GW&RÂF–7B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢$Ö—76–ær7÷'G2f—‡GW&R'Ò¢†öÖRÒ7G"†f—‡GW&RævWB‚&†öÖR"’÷"""’ç7G&—‚•³£cÐ¢v’Ò7G"†f—‡GW&RævWB‚&v’"’÷"""’ç7G&—‚•³£cÐ¢7F'BÒ7G"†f—‡GW&RævWB‚'7F'B"’÷"""’ç7G&—‚•³£cEÐ¢ÆVwVUöæÖRÒ7G"†f—‡GW&RævWB‚&ÆVwVUöæÖR"’÷"""’ç7G&—‚•³£cÐ¢'•ö6÷VçG'’Òf—‡GW&RævWB‚&'•ö6÷VçG'’"¢–bæ÷B†öÖR÷"æ÷Bv’÷"æ÷B—6–ç7Fæ6R†'•ö6÷VçG'’ÂF–7B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢$–çfÆ–B7÷'G2f—‡GW&R'Ò¢6ÆVæVE÷GbÒ·Ð¢f÷"6÷VçG'’ÂæÖW2–âÆ—7B†'•ö6÷VçG'’æ—FV×2‚’•³£#EÓ ¢–bæ÷B—6–ç7Fæ6R†æÖW2ÂÆ—7B“ ¢6öçF–çVP¢6öFRÒ7G"†6÷VçG'’÷"""’ç7G&—‚’çWW"‚•³£EÐ¢6ÆVæVE÷Ge¶6öFUÒÒ·7G"†æÖR÷"""’ç7G&—‚•³£#Ð¢f÷"æÖR–âæÖW5³£3Ò–b7G"†æÖR÷"""’ç7G&—‚•Ð¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢¶W’Ò…÷föEö66†Uö¶W’‡‚’Â7G"†6frævWB‚&ÖF6…÷F‡&W6†öÆB"’÷"ãc"’À¢÷7÷'G5öWfVçEö¶W’††öÖRÂv’Â7F'B’¢66†VBÒõ5õ%E5ôUdTåEô4„ääTÅô44„RævWB†¶W’¢g&W6‚Ò&ööÂ†66†VBæBF–ÖRçF–ÖR‚’ÒfÆöB†66†VBævWB‚'G2"’÷"¢Âõ5õ%E5ôUdTåEô4„ääTÅõEDÂ¢–bg&W6‚æBæ÷B–ÆöBævWB‚&f÷&6R"“ ¢&W7VÇBÒöVæf÷&6Uöf—‡GW&U÷6V7W&UöÖF6†W2€¢²&†öÖR#¢†öÖRÂ&v’#¢v’Â'7F'B#¢7F'BÀ¢&ÆVwVUöæÖR#¢ÆVwVUöæÖRÂ&'•ö6÷VçG'’#¢6ÆVæVE÷GgÒÀ¢66†VBævWB‚'&W7VÇB"’÷"·Ò¢&WGW&â6VÆbå÷6VæBƒ#ÂF–7B‡&W7VÇBÂ66†VCÕG'VR’¢–b–ÆöBævWB‚&66†VEööæÇ’"“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&66†VB#¢fÇ6WÒ¢G'“ ¢&W7VÇBÒf–æE÷7÷'G5öWfVçEö6†ææVÇ2€¢²&†öÖR#¢†öÖRÂ&v’#¢v’Â'7F'B#¢7F'BÀ¢&ÆVwVUöæÖR#¢ÆVwVUöæÖRÂ&'•ö6÷VçG'’#¢6ÆVæVE÷GgÒÂ6fr¢õ5õ%E5ôUdTåEô4„ääTÅô44„U¶¶W•ÒÒ²'G2#¢F–ÖRçF–ÖR‚’Â'&W7VÇB#¢&W7VÇGÐ¢F—6µöVçG&–W2ÒöÆöE÷7÷'G5öF—6µö66†R†6frÂ‚¢F—6µöVçG&–W5µ÷7÷'G5öWfVçEö¶W’††öÖRÂv’Â7F'B•ÒÒ°¢'G2#¢F–ÖRçF–ÖR‚’Â'&W7VÇB#¢÷7÷'G5÷&W7VÇEöf÷%÷7F÷&vR‡&W7VÇB—Ð¢÷6fU÷7÷'G5öF—6µö66†R†6frÂ‚ÂF—6µöVçG&–W2¢&WGW&â6VÆbå÷6VæBƒ#ÂF–7B‡&W7VÇBÂ66†VCÔfÇ6R’¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢%7÷'G26†ææVÂ6V&6ƒ¢"²7G"†R—Ò¢–bRçF‚ÓÒ"ö’÷7÷'G5öf–Æ&–Æ—G’# ¢–æ6öÖ–ærÒ–ÆöBævWB‚&f—‡GW&W2"¢–bæ÷B—6–ç7Fæ6R†–æ6öÖ–ærÂÆ—7B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢$Ö—76–ær7÷'G2f—‡GW&W2'Ò¢6frÒÆöEö6öæf–r‚“²‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&f–Æ&–Æ—G’#¢·ÒÂ&ÆövvVEö–â#¢fÇ6WÒ¢G'“ ¢6†ææVÇ2Â6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢%7÷'G26†ææVÂ6FÆöwVS¢"²7G"†R—Ò¢f–Æ&–Æ—G’Ò·Ó²æ÷rÒF–ÖRçF–ÖR‚¢F—6µöVçG&–W2ÒöÆöE÷7÷'G5öF—6µö66†R†6frÂ‚¢2&WW6RF†RW†—7F–ærUr66†R–âöæR72f÷"F†Rv†öÆRf—‡GW&P¢2&F6‚âF†—2—2F—6²öÖVÖ÷'’ÖöæÇ’æBæWfW"F÷væÆöG2wV–FRFFà¢öÆöEöWuöF—6µö66†R‡‚¢WuöF—66÷fW&–W2Òö66†VEöWuöF—66÷fW'’€¢–æ6öÖ–æu³£cÒÂ6†ææVÇ2Â6G2Â‚¢fuöF—66÷fW&–W2Ò÷fuöf—‡GW&UöF—66÷fW&–W2€¢–æ6öÖ–æu³£cÒÂ6†ææVÇ2Â6G2Â‚¢f÷"&uöf—‡GW&R–â–æ6öÖ–æu³£cÓ ¢–bæ÷B—6–ç7Fæ6R‡&uöf—‡GW&RÂF–7B“ ¢6öçF–çVP¢†öÖRÒ7G"‡&uöf—‡GW&RævWB‚&†öÖR"’÷"""’ç7G&—‚•³£cÐ¢v’Ò7G"‡&uöf—‡GW&RævWB‚&v’"’÷"""’ç7G&—‚•³£cÐ¢7F'BÒ7G"‡&uöf—‡GW&RævWB‚'7F'B"’÷"""’ç7G&—‚•³£cEÐ¢ÆVwVUöæÖRÒ7G"‡&uöf—‡GW&RævWB‚&ÆVwVUöæÖR"’÷"""’ç7G&—‚•³£cÐ¢'•ö6÷VçG'’Ò&uöf—‡GW&RævWB‚&'•ö6÷VçG'’"¢–bæ÷B†öÖR÷"æ÷Bv’÷"æ÷B—6–ç7Fæ6R†'•ö6÷VçG'’ÂF–7B“ ¢6öçF–çVP¢G'“ ¢WfVçE÷G2ÒFFWF–ÖRæFFWF–ÖRæg&öÖ—6öf÷&ÖB‡7F'Bç&WÆ6R‚%¢"Â"³£"’’çF–ÖW7F×‚¢–bWfVçE÷G2Âæ÷rÒb¢3c÷"WfVçE÷G2âæ÷r²CR¢#B¢3c ¢6öçF–çVP¢W†6WBW†6WF–öã ¢6öçF–çVP¢6ÆVæVE÷GbÒ·Ð¢f÷"6÷VçG'’ÂæÖW2–âÆ—7B†'•ö6÷VçG'’æ—FV×2‚’•³£#EÓ ¢–b—6–ç7Fæ6R†æÖW2ÂÆ—7B“ ¢6ÆVæVE÷Ge·7G"†6÷VçG'’÷"""’ç7G&—‚’çWW"‚•³£EÕÒÒ°¢7G"†æÖR÷"""’ç7G&—‚•³£#Òf÷"æÖR–âæÖW5³£3Ð¢–b7G"†æÖR÷"""’ç7G&—‚•Ð¢¶W’Ò…÷föEö66†Uö¶W’‡‚’Â7G"†6frævWB‚&ÖF6…÷F‡&W6†öÆB"’÷"ãc"’À¢÷7÷'G5öWfVçEö¶W’††öÖRÂv’Â7F'B’¢66†VBÒõ5õ%E5ôUdTåEô4„ääTÅô44„RævWB†¶W’¢F—6µö¶W’Ò÷7÷'G5öWfVçEö¶W’††öÖRÂv’Â7F'B¢–bæ÷B66†VC ¢7F÷&VBÒF—6µöVçG&–W2ævWB†F—6µö¶W’¢–b—6–ç7Fæ6R‡7F÷&VBÂF–7B’æB—6–ç7Fæ6R‡7F÷&VBævWB‚'&W7VÇB"’ÂF–7B“ ¢66†VBÒ²'G2#¢fÆöB‡7F÷&VBævWB‚'G2"’÷"’À¢'&W7VÇB#¢÷7÷'G5÷&W7VÇEöf÷%ö6Æ–VçB‡7F÷&VE²'&W7VÇB%ÒÂ‚—Ð¢g&W6‚Ò&ööÂ†66†VBæBæ÷rÒfÆöB†66†VBævWB‚'G2"’÷"’À¢õ5õ%E5ôUdTåEô4„ääTÅõEDÂ¢–bg&W6‚æBæ÷B–ÆöBævWB‚&f÷&6R"“ ¢&W7VÇBÒ66†VBævWB‚'&W7VÇB"’÷"·Ð¢VÇ6S ¢&W7VÇBÒöÖF6…÷7÷'G5öf—‡GW&Uö6†ææVÇ2€¢²&†öÖR#¢†öÖRÂ&v’#¢v’Â'7F'B#¢7F'BÀ¢&ÆVwVUöæÖR#¢ÆVwVUöæÖRÀ¢&'•ö6÷VçG'’#¢6ÆVæVE÷GgÒÂ6frÂ6†ææVÇ2Â6G2Â‚¢õ5õ%E5ôUdTåEô4„ääTÅô44„U¶¶W•ÒÒ²'G2#¢F–ÖRçF–ÖR‚’Â'&W7VÇB#¢&W7VÇGÐ¢&W7VÇBÒöFEöWuöF—66÷fW&–W2€¢&W7VÇBÂWuöF—66÷fW&–W2ævWB†F—6µö¶W’ÂµÒ’¢&W7VÇBÒöFEöWuöF—66÷fW&–W2€¢&W7VÇBÂfuöF—66÷fW&–W2ævWB†F—6µö¶W’ÂµÒ’¢&W7VÇBÒöVæf÷&6Uöf—‡GW&U÷6V7W&UöÖF6†W2€¢²&†öÖR#¢†öÖRÂ&v’#¢v’Â'7F'B#¢7F'BÀ¢&ÆVwVUöæÖR#¢ÆVwVUöæÖRÂ&'•ö6÷VçG'’#¢6ÆVæVE÷GgÒÂ&W7VÇB¢õ5õ%E5ôUdTåEô4„ääTÅô44„U¶¶W•ÒÒ²'G2#¢F–ÖRçF–ÖR‚’Â'&W7VÇB#¢&W7VÇGÐ¢F—6µöVçG&–W5¶F—6µö¶W•ÒÒ²'G2#¢F–ÖRçF–ÖR‚’À¢'&W7VÇB#¢÷7÷'G5÷&W7VÇEöf÷%÷7F÷&vR‡&W7VÇB—Ð¢f–Æ&–Æ—G•²'Â"æ¦ö–â‚††öÖRæÆ÷vW"‚’Âv’æÆ÷vW"‚’Â7F'E³£eÒ’•ÒÒ&W7VÇ@¢÷6fU÷7÷'G5öF—6µö66†R†6frÂ‚ÂF—6µöVçG&–W2¢&WGW&â6VÆbå÷6VæBƒ#Â²&f–Æ&–Æ—G’#¢f–Æ&–Æ—G’Â&ÆövvVEö–â#¢G'VWÒ¢–bRçF‚ÓÒ"ö’ö–×÷'E÷7FVÕ÷v—6†Æ—7B# ¢6frÒÆöEö6öæf–r‚¢6fVE÷W&ÂÒ7G"†6frævWB‚'7FVÕ÷v—6†Æ—7E÷W&Â"’÷"""’ç7G&—‚¢v—6†Æ—7E÷W&ÂÒ7G"‡–ÆöBævWB‚'W&Â"’÷"6fVE÷W&Â’ç7G&—‚¢–bæ÷Bv—6†Æ—7E÷W&Ã ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢$VçFW"7FVÒv—6†Æ—7BU$Â'Ò¢G'“ ¢66†VEö–BÒ7G"†6frævWB‚'7FVÕ÷v—6†Æ—7Eö–B"’÷"""’–b6fVE÷W&ÂÓÒv—6†Æ—7E÷W&ÂVÇ6R" ¢7FVÕö–BÒ66†VEö–B–b&RægVÆÆÖF6‚‡"%ÆG³wÒ"Â66†VEö–B’VÇ6R&W6öÇfU÷7FVÕ÷v—6†Æ—7Eö–B‡v—6†Æ—7E÷W&Â¢–bæ÷B7FVÕö–C ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢$6÷VÆBæ÷B&W6öÇfRF†B7FVÒ&öf–ÆR'Ò¢v—6†Æ—7BÒ7FVÕ÷v—6†Æ—7Eö—FV×2‡7FVÕö–B¢–G2Ò·7G"†—FVÒævWB‚&–B"’’f÷"—FVÒ–âv—6†Æ—7B–b7G"†—FVÒævWB‚&–B"’÷"""’æ—6F–v—B‚•Ð¢ÖWFFFÒ·&÷u²&ö–B%Ó¢&÷rf÷"&÷r–â7FVÕ÷7F÷&Uö—FV×2†–G2—Ð¢&–÷&—F–W2Ò·7G"†—FVÒævWB‚&–B"’“¢–çB†—FVÒævWB‚'&–÷&—G’"’÷"’f÷"—FVÒ–âv—6†Æ—7GÐ¢fbÒÆöEöff÷&—FW2‚¢7W'&VçEö–G2Ò6WB†–G2¢2&VÖ÷fRöæÇ’vÖW2&Wf–÷W6Ç’–×÷'FVBg&öÒF†—2v—6†Æ—7C²ÖçVÂff÷&—FW27F’à¢fe²&vÖW2%ÒÒ¶vÖRf÷"vÖR–âfbævWB‚&vÖW2"ÂµÒ¢–bæ÷B†vÖRævWB‚'v—6†Æ—7Eö–×÷'FVB"’æB7G"†vÖRævWB‚&ö–B"’’æ÷B–â7W'&VçEö–G2•Ð¢'•ö–BÒ·7G"†vÖRævWB‚&ö–B"’“¢vÖRf÷"vÖR–âfe²&vÖW2%×Ð¢f÷"ö–B–â–G3 ¢FWF–Ç2ÒÖWFFFævWB†ö–B¢–bæ÷BFWF–Ç3 ¢6öçF–çVP¢W†—7F–ærÒ'•ö–BævWB†ö–B¢–bW†—7F–ær—2æöæS ¢W†—7F–ærÒ²&ö–B#¢ö–BÂ'v—6†Æ—7Eö–×÷'FVB#¢G'VWÐ¢fe²&vÖW2%ÒæVæB†W†—7F–ær¢'•ö–E¶ö–EÒÒW†—7F–æp¢W†—7F–æu²'v—6†Æ—7Eö–×÷'FVB%ÒÒG'VP¢W†—7F–ærçWFFR‡²&æÖR#¢FWF–Ç2ævWB‚&æÖR"’÷"W†—7F–ærævWB‚&æÖR"’÷"$vÖR"À¢&6÷fW"#¢FWF–Ç2ævWB‚&6÷fW""’÷"W†—7F–ærævWB‚&6÷fW""’÷"""À¢'&VÆV6U÷FW‡B#¢FWF–Ç2ævWB‚'&VÆV6U÷FW‡B"’÷"W†—7F–ærævWB‚'&VÆV6U÷FW‡B"’÷"""À¢'&VÆV6VB#¢FWF–Ç2ævWB‚'&VÆV6VB"’÷"W†—7F–ærævWB‚'&VÆV6VB"’÷"""À¢'W&Â#¢FWF–Ç2ævWB‚'W&Â"’÷"W†—7F–ærævWB‚'W&Â"’÷"""À¢'v—6†Æ—7E÷&–÷&—G’#¢&–÷&—F–W2ævWB†ö–BÂ—Ò¢6fUöff÷&—FW2†fb¢6fu²'7FVÕ÷v—6†Æ—7E÷W&Â%ÒÒv—6†Æ—7E÷W&À¢6fu²'7FVÕ÷v—6†Æ—7Eö–B%ÒÒ7FVÕö–@¢6fu²'7FVÕ÷v—6†Æ—7E÷7–æ6VEöB%ÒÒ–çB‡F–ÖRçF–ÖR‚’¢6fUö6öæf–r†6fr¢G'“ ¢7FVÕ÷V&Æ–5÷&öf–ÆR‡7FVÕö–BÂf÷&6SÕG'VR¢W†6WBW†6WF–öã ¢70¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ&–×÷'FVB#¢ÆVâ†ÖWFFF’À¢'v—6†Æ—7E÷F÷FÂ#¢ÆVâ†–G2’À¢'7–æ6VEöB#¢6fu²'7FVÕ÷v—6†Æ—7E÷7–æ6VEöB%×Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢%7FVÒv—6†Æ—7C¢"²7G"†R—Ò¢–bRçF‚ÓÒ"ö’ö6öæf–r# ¢6frÒÆöEö6öæf–r‚¢Æåö&Vf÷&RÒ&ööÂ†6frævWB‚&ÆÆ÷uöÆâ"’¢&VÖ÷FUö&Vf÷&RÒ&ööÂ†6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"’¢&÷f–FW%ö&Vf÷&RÒGWÆR‡7G"†6frævWB†²’÷"""’f÷"²–à¢‚'‡G&VÕö†÷7B"Â'‡G&VÕ÷÷'B"Â'‡G&VÕ÷W6W""Â'‡G&VÕ÷72"’¢f÷"²–â‚'‡G&VÕö†÷7B"Â'‡G&VÕ÷÷'B"Â'‡G&VÕ÷W6W""Â'‡G&VÕ÷72"À¢'7G&VÕöW‡B"Â&ÖF6…÷F‡&W6†öÆB"Â&6÷VçG&–W2"Â'7F'E÷6V7F–öâ"À¢&6†V6µ÷6†÷w5ööå÷7F'GW"Â'&Vg&W6…ö—Geööå÷7F'GW"Â'&Vg&W6…÷7÷'G5ööå÷7F'GW"Â'&öf–ÆUöæÖR"À¢'&VfW'&VEöÆæwVvR"Â'&öf–ÆUöVÖ&ÆVÒ"Â&×–Æ—7EöÆ–÷WB"Â&fö÷F&ÆÅöVæ&ÆVB"À¢&cöVæ&ÆVB"Â&vÖW5öVæ&ÆVB"Â&FV6÷&F–öç5öVæ&ÆVB"Â&&6¶w&÷VæE÷7G–ÆR"Â'6WGWö6ö×ÆWFR"Â'6WGWöFVÖõö6öçFVçB"Â&WFõ÷6‡WFF÷våöÖ–çWFW2"Â&ÆÆ÷uöÆâ"Â&FWeöÖöFR"Â'&—fFU÷&VÖ÷FU÷&VÆ’"“ ¢–b²–â–ÆöC ¢6fu¶µÒÒ–ÆöE¶µÐ¢–b6frævWB‚'7G&VÕöW‡B"’æ÷B–â‚'G2"Â&Ó7S‚"“ ¢6fu²'7G&VÕöW‡B%ÒÒ'G2 ¢G'“ ¢6fu²&ÖF6…÷F‡&W6†öÆB%ÒÒÖ‚ƒãCÂÖ–âƒãƒÂfÆöB†6frævWB‚&ÖF6…÷F‡&W6†öÆB"Âãc"’’’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢6fu²&ÖF6…÷F‡&W6†öÆB%ÒÒãc ¢&uö6÷VçG&–W2Ò6frævWB‚&6÷VçG&–W2"’–b—6–ç7Fæ6R†6frævWB‚&6÷VçG&–W2"’ÂÆ—7B’VÇ6RµÐ¢6fu²&6÷VçG&–W2%ÒÒÆ—7B†F–7Bæg&öÖ¶W—2‡7G"†6öFR’ç7G&—‚’æÆ÷vW"‚’f÷"6öFR–â&uö6÷VçG&–W0¢–b&RægVÆÆÖF6‚‡"%¶×¤Õ¥×³'Ò"Â7G"†6öFR’ç7G&—‚’’’•³£eÒ÷"²&æò"Â&v""Â'W2%Ð¢–b6frævWB‚'&VfW'&VEöÆæwVvR"’æ÷B–â‚&Vâ"Â&æò"“ ¢6fu²'&VfW'&VEöÆæwVvR%ÒÒ&Vâ ¢–b6frævWB‚&×–Æ—7EöÆ–÷WB"’æ÷B–â‚&&Ææ6VB"Â'7÷FÆ–v‡B"Â'F–ÖVÆ–æR"Â&‡V""“ ¢6fu²&×–Æ—7EöÆ–÷WB%ÒÒ'F–ÖVÆ–æR ¢ÆÆ÷vVE÷7F'G2Ò‚&×–Æ—7B"Â&×—F–ÖVÆ–æR"Â&6†ææVÇ2"Â&×—Gb"Â&Ö÷f–W2"Â'6†÷w2"Â&vÖW2"Â'&6–ær"Â'FV×2"¢–b6frævWB‚'7F'E÷6V7F–öâ"’æ÷B–âÆÆ÷vVE÷7F'G3 ¢6fu²'7F'E÷6V7F–öâ%ÒÒ&×–Æ—7B ¢–b&&6¶w&÷VæE÷7G–ÆR"æ÷B–â–ÆöBæB&FV6÷&F–öç5öVæ&ÆVB"–â–ÆöC ¢6fu²&&6¶w&÷VæE÷7G–ÆR%ÒÒ&fÆöB"–b–ÆöBævWB‚&FV6÷&F–öç5öVæ&ÆVB"’VÇ6R&öfb ¢–b6frævWB‚&&6¶w&÷VæE÷7G–ÆR"’æ÷B–â‚&fÆöB"Â&66–’"Â&öfb"“ ¢6fu²&&6¶w&÷VæE÷7G–ÆR%ÒÒ&fÆöB"–b6frævWB‚&FV6÷&F–öç5öVæ&ÆVB"ÂG'VR’VÇ6R&öfb ¢6fu²&FV6÷&F–öç5öVæ&ÆVB%ÒÒ6fu²&&6¶w&÷VæE÷7G–ÆR%ÒÒ&öfb ¢6fu²&†–FUö6ÖE÷v–æF÷r%ÒÒG'VP¢G'“ ¢6fu²&WFõ÷6‡WFF÷våöÖ–çWFW2%ÒÒÖ‚ƒÂ–çB†6frævWB‚&WFõ÷6‡WFF÷våöÖ–çWFW2"’÷"’¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ ¢6fu²&WFõ÷6‡WFF÷våöÖ–çWFW2%ÒÒ ¢–b6fu²&WFõ÷6‡WFF÷våöÖ–çWFW2%Òæ÷B–âƒÂ3ÂcÂ#Â#C“ ¢6fu²&WFõ÷6‡WFF÷våöÖ–çWFW2%ÒÒ ¢6fu²&6†V6µ÷6†÷w5ööå÷7F'GW%ÒÒ&ööÂ†6frævWB‚&6†V6µ÷6†÷w5ööå÷7F'GW"’¢6fu²'&Vg&W6…ö—Geööå÷7F'GW%ÒÒ&ööÂ†6frævWB‚'&Vg&W6…ö—Geööå÷7F'GW"’¢6fu²'&Vg&W6…÷7÷'G5ööå÷7F'GW%ÒÒ&ööÂ†6frævWB‚'&Vg&W6…÷7÷'G5ööå÷7F'GW"’¢6fu²&ÆÆ÷uöÆâ%ÒÒ&ööÂ†6frævWB‚&ÆÆ÷uöÆâ"’¢6fu²&FWeöÖöFR%ÒÒ&ööÂ†6frævWB‚&FWeöÖöFR"’¢6fu²'&—fFU÷&VÖ÷FU÷&VÆ’%ÒÒ&ööÂ†6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"’’æB6fu²&FWeöÖöFR%Ð¢–b†6fu²&ÆÆ÷uöÆâ%Ò÷"6fu²'&—fFU÷&VÖ÷FU÷&VÆ’%Ò’æBæ÷B7G"†6frævWB‚&Æåö66W75÷Fö¶Vâ"’÷"""“ ¢6fu²&Æåö66W75÷Fö¶Vâ%ÒÒ†6†Æ–"ç6†#Sb†÷2çW&æFöÒƒC‚’’æ†W†F–vW7B‚¢6frç÷‚'&Vg&W6…öÆÅööå÷7F'GW"ÂæöæR¢6frç÷‚'7F'GW÷&Vg&W6…öÖöFR"ÂæöæR¢6fUö6öæf–r†6fr¢&÷f–FW%ögFW"ÒGWÆR‡7G"†6frævWB†²’÷"""’f÷"²–à¢‚'‡G&VÕö†÷7B"Â'‡G&VÕ÷÷'B"Â'‡G&VÕ÷W6W""Â'‡G&VÕ÷72"’¢–b&÷f–FW%ögFW"Ò&÷f–FW%ö&Vf÷&S ¢ö6ÆV%÷&÷f–FW%ö66†W2‚¢–b6fu²&ÆÆ÷uöÆâ%Ó ¢Æåöö²Ò÷7F'EöÆå÷6W'fW"‚¢VÇ6S ¢÷7F÷öÆå÷6W'fW"‚¢Æåöö²ÒfÇ6P¢–b6fu²'&—fFU÷&VÖ÷FU÷&VÆ’%Ó ¢&VÖ÷FUöö²Ò÷7F'E÷&VÖ÷FU÷6W'fW"‚¢VÇ6S ¢÷7F÷÷&VÖ÷FU÷6W'fW"‚¢&VÖ÷FUöö²ÒfÇ6P¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ&ÆÆ÷uöÆâ#¢6fu²&ÆÆ÷uöÆâ%ÒÀ¢&Æå÷W&Â#¢öÆåö66W75÷W&Â†6frÂô5D•dUôÄåõõ%B÷"Äåõõ%B’–bÆåöö²VÇ6R""À¢'&—fFU÷&VÖ÷FU÷&VÆ’#¢6fu²'&—fFU÷&VÖ÷FU÷&VÆ’%ÒÀ¢'&—fFU÷&VÖ÷FU÷W&Â#¢÷&—fFU÷&VÖ÷FU÷W&Â†6frÂô5D•dUõ$TÔõDUõõ%B÷"$TÔõDUõõ%B’–b&VÖ÷FUöö²VÇ6R""À¢'&W7F'E÷&WV—&VB#¢fÇ6WÒ ¢–bRçF‚ÓÒ"ö’ö6ÆV%ö'Gv÷&µö66†R# ¢&ö÷BÒ'Gv÷&µö66†UöF—"‚¢&VÖ÷fVBÒ'Gv÷&µö66†U÷6—¦R‚¢G'“ ¢–b÷2çF‚æ—6F—"‡&ö÷B“ ¢6‡WF–Âç&×G&VR‡&ö÷B¢õEdÔ¤Uô44„Ræ6ÆV"‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'&VÖ÷fVEö'—FW2#¢&VÖ÷fVGÒ¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&ö²#¢fÇ6RÂ&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’÷&W6WEö6öÆE÷7F'B# ¢&VÖ÷fVE÷66†VGVÆW2Ò ¢G'“ ¢ö6ÆV%÷&÷f–FW%ö66†W2‚¢õEeô44„Ræ6ÆV"‚¢õDTÕôd•…EU$Uô44„Ræ6ÆV"‚¢õDTÕõ$ôd”ÄUô44„Ræ6ÆV"‚¢õDTÕô”Eô44„Ræ6ÆV"‚¢ôD”Å•ôÔD4…ô44„RçWFFR‡²&FFR#¢""Â'G2#¢Â&ÖF6†W2#¢µ×Ò¢ôcõ44„TETÄUô44„RçWFFR‡²'G2#¢Â&WfVçG2#¢µ×Ò¢ôcõDTÕ5ô44„RçWFFR‡²'G2#¢Â'FV×2#¢µ×Ò¢ö6ÆV%÷&6–æuöf–Æ&–Æ—G•ö66†R‚¢õEdÔ¤Uô44„Ræ6ÆV"‚¢66†U÷&ö÷BÒFFö66†UöF—"‚¢–b÷2çF‚æ—6F—"†66†U÷&ö÷B“ ¢6‡WF–Âç&×G&VR†66†U÷&ö÷B¢&ö÷BÒ'Gv÷&µö66†UöF—"‚¢–b÷2çF‚æ—6F—"‡&ö÷B“ ¢f÷"&6RÂöF—'2Âf–ÆW2–â÷2çvÆ²‡&ö÷B“ ¢f÷"æÖR–âf–ÆW3 ¢–bæÖRæ÷B–â‚&W—6öFR×66†VGVÆRæ§6öâ"Â&ÆFW7BÖW—6öFRæ§6öâ"À¢&ÆFW7BÖW—6öFW2æ§6öâ"“ ¢6öçF–çVP¢G'“ ¢÷2ç&VÖ÷fR†÷2çF‚æ¦ö–â†&6RÂæÖR’¢&VÖ÷fVE÷66†VGVÆW2³Ò¢W†6WBõ4W'&÷# ¢70¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÀ¢'&VÖ÷fVE÷66†VGVÆW2#¢&VÖ÷fVE÷66†VGVÆW7Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&ö²#¢fÇ6RÂ&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’ö6†V6µ÷6†÷u÷WFFW2# ¢6frÒÆöEö6öæf–r‚¢G'“ ¢&W7VÇBÒ&Vg&W6…öff÷&—FU÷6†÷uöW—6öFW2†6fr¢&WGW&â6VÆbå÷6VæBƒ#ÂF–7B‡²&ö²#¢G'VWÒÂ¢§&W7VÇB’¢W†6WBfÇVTW'&÷"2S ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢7G"†R—Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’ö6†V6µ÷FVÕöf—‡GW&W2# ¢feöFFÒÆöEöff÷&—FW2‚¢ff÷&—FW2ÒfeöFFævWB‚'FV×2"ÂµÒ¢õDTÕôd•…EU$Uô44„Ræ6ÆV"‚¢õDTÕõ$ôd”ÄUô44„Ræ6ÆV"‚¢÷&VÖ÷fUöFFö66†U÷&Vf—‚‚'FVÒÖf—‡GW&W2Ò"¢÷&VÖ÷fUöFFö66†U÷&Vf—‚†b'FVÒ×&öf–ÆR×gµõDTÕõ$ôd”ÄUô44„Uõ44„TÔÒÒ"¢&Vg&W6†VBÒ ¢W'&÷'2ÒµÐ¢6†ævVBÒfÇ6P¢f÷"ff÷&—FR–âff÷&—FW3 ¢FVÕöæÖRÒ7G"†ff÷&—FRævWB‚&æÖR"’–b—6–ç7Fæ6R†ff÷&—FRÂF–7B¢VÇ6Rff÷&—FR’ç7G&—‚¢–bæ÷BFVÕöæÖS ¢6öçF–çVP¢FVÕö–BÒ7G"†ff÷&—FRævWB‚'FVÕö–B"’–b—6–ç7Fæ6R†ff÷&—FRÂF–7B¢VÇ6R""’ç7G&—‚¢–bæ÷BFVÕö–C ¢G'“ ¢FVÕö–BÒ&W6öÇfUöf÷FÖö%÷FVÕö–B‡FVÕöæÖR¢W†6WBW†6WF–öâ2S ¢W'&÷'2æVæB†b'·FVÕöæÖWÓ¢¶WÒ"¢–bæ÷BFVÕö–C ¢W'&÷'2æVæB†b'·FVÕöæÖWÓ¢FVÒæ÷Bf÷VæB"¢6öçF–çVP¢–b—6–ç7Fæ6R†ff÷&—FRÂF–7B’æBæ÷Bff÷&—FRævWB‚'FVÕö–B"“ ¢ff÷&—FU²'FVÕö–B%ÒÒFVÕö–@¢6†ævVBÒG'VP¢G'“ ¢fWF6…÷FVÕ÷66†VGVÆR‡FVÕö–BÂFVÕöæÖR¢&Vg&W6†VB³Ò¢W†6WBW†6WF–öâ2S ¢W'&÷'2æVæB†b'·FVÕöæÖWÓ¢¶WÒ"¢–b6†ævVC ¢6fUöff÷&—FW2†feöFF¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'FV×2#¢&Vg&W6†VBÀ¢&W'&÷'2#¢W'&÷'7Ò ¢–bRçF‚ÓÒ"ö’÷&Vg&W6…öfö÷F&ÆÂ# ¢6frÒÆöEö6öæf–r‚¢G'“ ¢ôD”Å•ôÔD4…ô44„RçWFFR‡²&FFR#¢""Â'G2#¢Â&ÖF6†W2#¢µ×Ò¢õEeô44„Ræ6ÆV"‚¢ôÅEeô44„Ræ6ÆV"‚¢÷&VÖ÷fUöFFö66†U÷&Vf—‚‚&f÷FÖö"ÖF–Ç’"¢÷&VÖ÷fUöFFö66†U÷&Vf—‚‚&ÇGbÖF–Ç’Ò"¢F–Ç’ÒfWF6…öf÷FÖö%öF–Ç•öÖF6†W2‚¢wV–FW2Ò ¢Æ—7F–æu÷6÷W&6RÒ$ÅEb ¢Æ—7F–æuöæ÷F–6RÒ" ¢G'“ ¢fWF6…öÇGeöF–Ç’†FFWF–ÖRæFFRçFöF’‚’æ—6öf÷&ÖB‚’¢wV–FW2Ò¢W†6WBW†6WF–öâ2W†3 ¢Æ—7F–æu÷6÷W&6RÒ$f÷DÖö"fÆÆ&6² ¢Æ—7F–æuöæ÷F–6RÒ$Æ—fR6ö66W"Eb6†ææVÂÆ—7F–æw2Væf–Æ&ÆR(	BW6–ærf÷DÖö"6†ææVÂÆ—7F–æw2 ¢÷&VÖ÷fUöFFö66†U÷&Vf—‚‚'GbÖwV–FRÒ"¢f÷"6÷VçG'’–âdõDÔô%ôdÄÄ$4µô4õTåE$”U3 ¢fWF6…ö6÷VçG'•öf—‡GW&W2†6÷VçG'’¢wV–FW2³Ò¢feöFFÒÆöEöff÷&—FW2‚¢õDTÕôd•…EU$Uô44„Ræ6ÆV"‚¢õDTÕõ$ôd”ÄUô44„Ræ6ÆV"‚¢÷&VÖ÷fUöFFö66†U÷&Vf—‚‚'FVÒÖf—‡GW&W2Ò"¢÷&VÖ÷fUöFFö66†U÷&Vf—‚†b'FVÒ×&öf–ÆR×gµõDTÕõ$ôd”ÄUô44„Uõ44„TÔÒÒ"¢FV×2Ò ¢W'&÷'2ÒµÐ¢6†ævVBÒfÇ6P¢f÷"ff÷&—FR–âfeöFFævWB‚'FV×2"ÂµÒ“ ¢FVÕöæÖRÒ7G"†ff÷&—FRævWB‚&æÖR"’–b—6–ç7Fæ6R†ff÷&—FRÂF–7B’VÇ6Rff÷&—FR’ç7G&—‚¢–bæ÷BFVÕöæÖS ¢6öçF–çVP¢FVÕö–BÒ7G"†ff÷&—FRævWB‚'FVÕö–B"’–b—6–ç7Fæ6R†ff÷&—FRÂF–7B’VÇ6R""’ç7G&—‚¢–bæ÷BFVÕö–C ¢G'“ ¢FVÕö–BÒ&W6öÇfUöf÷FÖö%÷FVÕö–B‡FVÕöæÖR¢W†6WBW†6WF–öâ2S ¢W'&÷'2æVæB†b'·FVÕöæÖWÓ¢¶WÒ"¢–bæ÷BFVÕö–C ¢6öçF–çVP¢–b—6–ç7Fæ6R†ff÷&—FRÂF–7B’æBæ÷Bff÷&—FRævWB‚'FVÕö–B"“ ¢ff÷&—FU²'FVÕö–B%ÒÒFVÕö–@¢6†ævVBÒG'VP¢G'“ ¢fWF6…÷FVÕ÷66†VGVÆR‡FVÕö–BÂFVÕöæÖR¢FV×2³Ò¢W†6WBW†6WF–öâ2S ¢W'&÷'2æVæB†b'·FVÕöæÖWÓ¢¶WÒ"¢–b6†ævVC ¢6fUöff÷&—FW2†feöFF¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'FV×2#¢FV×2Â&wV–FW2#¢wV–FW2À¢&ÖF6†W2#¢ÆVâ†F–Ç’’Â&W'&÷'2#¢W'&÷'2À¢&Æ—7F–æu÷6÷W&6R#¢Æ—7F–æu÷6÷W&6RÀ¢&Æ—7F–æuöæ÷F–6R#¢Æ—7F–æuöæ÷F–6WÒ¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’ö6†V6µöÖ÷f–U÷WFFW2# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢$æ÷B6öæf–wW&VB'Ò¢G'“ ¢&Wf–÷W2Ò…õdôEô44„RævWB‚&Ö÷f–W2"’÷"öÆöE÷föEö6FÆöuö66†R‡‚’¢&Wf–÷W5ö–G2Ò·7G"‡&÷rævWB‚'7G&VÕö–B"’’f÷"&÷r–â&Wf–÷W0¢–b—6–ç7Fæ6R‡&÷rÂF–7B’æB&÷rævWB‚'7G&VÕö–B"’—2æ÷BæöæWÐ¢g&W6‚Ò‚çföE÷7G&V×2‚¢–bæ÷Bg&W6ƒ ¢&—6R'VçF–ÖTW'&÷"‚%&÷f–FW"&WGW&æVBâV×G’Ö÷f–R6FÆör"¢Ö÷f–W2Ò÷6fU÷föEö6FÆöuö66†R‡‚Âg&W6‚¢õdôEô44„RçWFFR‡²'&÷f–FW"#¢÷föEö66†Uö¶W’‡‚’Â'G2#¢F–ÖRçF–ÖR‚’Â&Ö÷f–W2#¢Ö÷f–W7Ò¢g&W6…ö–G2Ò·7G"‡&÷rævWB‚'7G&VÕö–B"’’f÷"&÷r–âÖ÷f–W0¢–b&÷rævWB‚'7G&VÕö–B"’—2æ÷BæöæWÐ¢æWuöÖ÷f–W2ÒÆVâ†g&W6…ö–G2Ò&Wf–÷W5ö–G2’–b&Wf–÷W5ö–G2VÇ6R ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ&Ö÷f–W2#¢ÆVâ†Ö÷f–W2’À¢&æWuöÖ÷f–W2#¢æWuöÖ÷f–W7Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’÷&Vg&W6…÷‡G&VÒ# ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢%‡G&VÒ—2æ÷B6öæf–wW&VB'Ò¢G'“ ¢6†ææVÇ2Âö6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6frÂf÷&6SÕG'VR¢ö6ÆV%÷7÷'G5öWfVçEö6†ææVÅö66†R‚¢Ö÷f–W2ÒvWE÷‡G&VÕöÖ÷f–W2†6frÂf÷&6SÕG'VR¢6†÷w2ÒvWE÷‡G&VÕ÷6W&–W2†6frÂf÷&6SÕG'VR¢W—6öFU÷&W7VÇBÒ&Vg&W6…öff÷&—FU÷6†÷uöW—6öFW2†6fr¢&WGW&â6VÆbå÷6VæBƒ#ÂF–7B‡²&ö²#¢G'VRÀ¢&6†ææVÇ2#¢ÆVâ†6†ææVÇ2’Â&Ö÷f–W2#¢ÆVâ†Ö÷f–W2’À¢'6†÷w2#¢ÆVâ‡6†÷w2—ÒÂ¢¦W—6öFU÷&W7VÇB’¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’÷&Vg&W6…÷&6–ær# ¢6frÒÆöEö6öæf–r‚¢G'“ ¢6VÆV7FVBÒ6frævWB‚'&6–æu÷6W&–W2"Â²&c%Ò¢ö6ÆV%÷&6–æuöf–Æ&–Æ—G•ö66†R‚¢WfVçG2ÒvWE÷&6–æuöWfVçG2‡6VÆV7FVBÂf÷&6SÕG'VR¢–b&c"–â6VÆV7FVC ¢vWEöc÷FV×2†f÷&6SÕG'VR¢vWE÷&6–æuöG&—fW'2†f÷&6SÕG'VR¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'6W&–W2#¢ÆVâ‡6VÆV7FVB’À¢&WfVçG2#¢ÆVâ†WfVçG2—Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒS"Â²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’÷FW7E÷6÷W&6R# ¢¶W’Ò7G"‡–ÆöBævWB‚&¶W’"’÷"""’ç7G&—‚¢–b¶W’æ÷B–âõ4õU$4UôÄ$TÅôÔ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢'Væ¶æ÷vâ6÷W&6R'Ò¢&W7VÇBÒFW7EöW‡FW&æÅ÷6÷W&6R†¶W’¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'&W7VÇB#¢&W7VÇBÀ¢'6÷W&6W2#¢6÷W&6Uö†VÇF…÷6æ6†÷B‚—Ò ¢–bRçF‚ÓÒ"ö’öff÷&—FW2# ¢27F–öç3¢6FVv÷'’ö6†ææVÂöÖ÷f–Rff÷&—FRÖævVÖVçBæB&V÷&FW&–æp¢fbÒÆöEöff÷&—FW2‚¢7BÒ–ÆöBævWB‚&7F–öâ"Â""¢–b7BÓÒ&FEö6G2# ¢f÷"2–â–ÆöBævWB‚&6FVv÷&–W2"ÂµÒ“ ¢–b2æB2æ÷B–âfe²&6FVv÷&–W2%Ó ¢fe²&6FVv÷&–W2%ÒæVæB†2¢VÆ–b7BÓÒ'&VÖ÷fUö6B# ¢fe²&6FVv÷&–W2%ÒÒ¶2f÷"2–âfe²&6FVv÷&–W2%Ò–b2Ò–ÆöBævWB‚&6FVv÷'’"•Ð¢VÆ–b7BÓÒ&FEö6†ææVÇ2# ¢†fRÒ·7G"†2ævWB‚'7G&VÕö–B"’’f÷"2–âfe²&6†ææVÇ2%×Ð¢f÷"6‚–â–ÆöBævWB‚&6†ææVÇ2"ÂµÒ“ ¢6–BÒ7G"†6‚ævWB‚'7G&VÕö–B"’¢–b6–BæB6–Bæ÷B–â†fS ¢fe²&6†ææVÇ2%ÒæVæB‡²'7G&VÕö–B#¢6‚ævWB‚'7G&VÕö–B"’À¢&æÖR#¢6‚ævWB‚&æÖR"Â""’À¢&6FVv÷'’#¢6‚ævWB‚&6FVv÷'’"Â""’À¢&Æövò#¢6‚ævWB‚&Æövò"’÷"÷7G&VÕö–6öåöf÷%ö–B‡6–B—Ò¢†fRæFB‡6–B¢VÆ–b7BÓÒ'FövvÆUö6†ææVÂ# ¢6–BÒ7G"‡–ÆöBævWB‚'7G&VÕö–B"’¢–G‚ÒæW‡B‚†’f÷"’Â2–âVçVÖW&FR†fe²&6†ææVÇ2%Ò’–b7G"†2ævWB‚'7G&VÕö–B"’’ÓÒ6–B’ÂÓ¢–b–G‚ãÒ ¢fe²&6†ææVÇ2%Òç÷†–G‚¢VÇ6S ¢fe²&6†ææVÇ2%ÒæVæB‡²'7G&VÕö–B#¢–ÆöBævWB‚'7G&VÕö–B"’À¢&æÖR#¢–ÆöBævWB‚&æÖR"Â""’À¢&6FVv÷'’#¢–ÆöBævWB‚&6FVv÷'’"Â""’À¢&Æövò#¢–ÆöBævWB‚&Æövò"’÷"÷7G&VÕö–6öåöf÷%ö–B‡6–B—Ò¢VÆ–b7BÓÒ'&VÖ÷fUö6†ææVÂ# ¢6–BÒ7G"‡–ÆöBævWB‚'7G&VÕö–B"’¢fe²&6†ææVÇ2%ÒÒ¶2f÷"2–âfe²&6†ææVÇ2%Ò–b7G"†2ævWB‚'7G&VÕö–B"’’Ò6–EÐ¢VÆ–b7BÓÒ'&V÷&FW%ö6†ææVÇ2# ¢&WVW7FVBÒ·7G"‡6–B’f÷"6–B–â–ÆöBævWB‚'7G&VÕö–G2"ÂµÒ•Ð¢'•ö–BÒ·7G"†2ævWB‚'7G&VÕö–B"’“¢2f÷"2–âfe²&6†ææVÇ2%×Ð¢&V÷&FW&VBÒ¶'•ö–Bç÷‡6–B’f÷"6–B–â&WVW7FVB–b6–B–â'•ö–EÐ¢2&W6W'fRç’6†ææVÇ2FFVB6öæ7W'&VçFÇ’÷"öÖ—GFVB'’âöÆB6Æ–VçBà¢&V÷&FW&VBæW‡FVæB†2f÷"2–âfe²&6†ææVÇ2%Ò–b7G"†2ævWB‚'7G&VÕö–B"’’–â'•ö–B¢fe²&6†ææVÇ2%ÒÒ&V÷&FW&V@¢VÆ–b7BÓÒ'6WEö×–Æ—7Eö6†ææVÇ2# ¢ff÷&—FUö–G2Ò·7G"†2ævWB‚'7G&VÕö–B"’’f÷"2–âfe²&6†ææVÇ2%×Ð¢6†÷6VâÒµÐ¢f÷"6–B–â–ÆöBævWB‚'7G&VÕö–G2"ÂµÒ“ ¢6–BÒ7G"‡6–B¢–b6–B–âff÷&—FUö–G2æB6–Bæ÷B–â6†÷6Vã ¢6†÷6VâæVæB‡6–B¢–bÆVâ†6†÷6Vâ’ãÒS ¢'&V°¢fe²&×–Æ—7Eö6†ææVÇ2%ÒÒ6†÷6Và¢VÆ–b7BÓÒ'FövvÆUöÖ÷f–R# ¢Ö÷f–RÒ–ÆöBævWB‚&Ö÷f–R"’÷"·Ð¢6FÆöuö–BÒ7G"†Ö÷f–RævWB‚&6FÆöuö–B"’÷"""’ç7G&—‚¢–bæ÷B6FÆöuö–BæBÖ÷f–RævWB‚&æÖR"“ ¢G'“ ¢vçFVEöæÖRÒö6ÆVå÷6†÷u÷F—FÆR†Ö÷f–RævWB‚&æÖR"’’÷"7G"†Ö÷f–RævWB‚&æÖR"’¢vçFVE÷–V"Ò7G"†Ö÷f–RævWB‚'–V""’÷"÷&÷f–FW%÷–V"†Ö÷f–R’÷"""¢ÖF6†W2Ò·&÷rf÷"&÷r–â6–æVÖWF÷6V&6‚‚&Ö÷f–R"ÂvçFVEöæÖR¢–b÷6†÷uö¶W’‡&÷rævWB‚&æÖR"’’ÓÒ÷6†÷uö¶W’‡vçFVEöæÖR•Ð¢6†÷6VâÒæW‡B‚‡&÷rf÷"&÷r–âÖF6†W0¢–bvçFVE÷–V"æBö6FÆöu÷–V"‡&÷r’ÓÒvçFVE÷–V"’À¢ÖF6†W5³Ò–bÖF6†W2VÇ6RæöæR¢–b6†÷6Vã ¢6FÆöuö–BÒ7G"†6†÷6VâævWB‚&–B"’÷"""¢Ö÷f–RÒF–7B†Ö÷f–R¢Ö÷f–U²&6FÆöuö–B%ÒÒ6FÆöuö–@¢Ö÷f–U²&æÖR%ÒÒ6†÷6VâævWB‚&æÖR"’÷"vçFVEöæÖP¢Ö÷f–U²'–V"%ÒÒö6FÆöu÷–V"†6†÷6Vâ’÷"vçFVE÷–V ¢Ö÷f–U²&6÷fW"%ÒÒ6†÷6VâævWB‚'÷7FW""’÷"Ö÷f–RævWB‚&6÷fW""’÷"" ¢W†6WBW†6WF–öã ¢70¢6–BÒ7G"†Ö÷f–RævWB‚'7G&VÕö–B"Â""’¢ff÷&—FUö¶W’Ò6FÆöuö–B÷"6–@¢–G‚ÒÓ¢f÷"’ÂW†—7F–ær–âVçVÖW&FR†fe²&Ö÷f–W2%Ò“ ¢6ÖUö–BÒ7G"†W†—7F–ærævWB‚&6FÆöuö–B"’÷"W†—7F–ærævWB‚'7G&VÕö–B"’’ÓÒff÷&—FUö¶W¢6ÖU÷F—FÆRÒ÷6†÷uö¶W’†W†—7F–ærævWB‚&æÖR"’’ÓÒ÷6†÷uö¶W’†Ö÷f–RævWB‚&æÖR"’¢6ÖU÷–V"Ò†æ÷BÖ÷f–RævWB‚'–V""’÷"æ÷BW†—7F–ærævWB‚'–V""’÷ ¢7G"†W†—7F–ærævWB‚'–V""’’ÓÒ7G"†Ö÷f–RævWB‚'–V""’’¢–b6ÖUö–B÷"‡6ÖU÷F—FÆRæB6ÖU÷–V"“ ¢–G‚Ò¢'&V°¢–b–G‚ãÒ ¢fe²&Ö÷f–W2%Òç÷†–G‚¢VÆ–bff÷&—FUö¶W“ ¢&VÆV6VBÒ7G"†Ö÷f–RævWB‚'&VÆV6VB"’÷"""¢–b6FÆöuö–BæBæ÷B&VÆV6VC ¢G'“ ¢&VÆV6VBÒ7G"†6–æVÖWFöÖWF‚&Ö÷f–R"Â6FÆöuö–B’ævWB‚'&VÆV6VB"’÷"""¢W†6WBW†6WF–öã ¢&VÆV6VBÒ" ¢fe²&Ö÷f–W2%ÒæVæB‡°¢&6FÆöuö–B#¢6FÆöuö–BÀ¢'7G&VÕö–B#¢Ö÷f–RævWB‚'7G&VÕö–B"’À¢&æÖR#¢Ö÷f–RævWB‚&æÖR"Â""’À¢&W‡FVç6–öâ#¢Ö÷f–RævWB‚&W‡FVç6–öâ"Â&×B"’À¢'–V"#¢Ö÷f–RævWB‚'–V""Â""’À¢'&F–ær#¢Ö÷f–RævWB‚'&F–ær"Â""’À¢&6÷fW"#¢Ö÷f–RævWB‚&6÷fW""Â""’À¢'&VÆV6VB#¢&VÆV6VBÀ¢Ò¢FVÖõö6frÒÆöEö6öæf–r‚¢–bFVÖõö6frævWB‚'6WGWöFVÖõö6öçFVçB"“ ¢FVÖõö6fu²'6WGWöFVÖõö6öçFVçB%ÒÒfÇ6P¢6fUö6öæf–r†FVÖõö6fr¢VÆ–b7BÓÒ'&VÖ÷fUöÖ÷f–R# ¢6–BÒ7G"‡–ÆöBævWB‚&ff÷&—FUö¶W’"’÷"–ÆöBævWB‚'7G&VÕö–B"Â""’¢fe²&Ö÷f–W2%ÒÒ¶Òf÷"Ò–âfe²&Ö÷f–W2%Ð¢–b7G"†ÒævWB‚&6FÆöuö–B"’÷"ÒævWB‚'7G&VÕö–B"’’Ò6–EÐ¢VÆ–b7BÓÒ'FövvÆU÷6†÷r# ¢6†÷rÒ–ÆöBævWB‚'6†÷r"’÷"·Ð¢6FÆöuö–BÒ7G"‡6†÷rævWB‚&6FÆöuö–B"’÷"""’ç7G&—‚¢F—FÆUö¶W’Ò7G"‡6†÷rævWB‚'6†÷uö¶W’"’÷"÷6†÷uö¶W’‡6†÷rævWB‚&æÖR"’’÷"""¢¶W’Ò7G"†6FÆöuö–B÷"6†÷rævWB‚'6†÷uö¶W’"’÷"÷6†÷uö¶W’‡6†÷rævWB‚&æÖR"’’÷ ¢6†÷rævWB‚'6W&–W5ö–B"Â""’¢–G‚ÒæW‡B‚†’f÷"’Â2–âVçVÖW&FR†fe²'6†÷w2%Ò¢–b‡7G"‡2ævWB‚&6FÆöuö–B"’÷"2ævWB‚'6†÷uö¶W’"’÷"÷6†÷uö¶W’‡2ævWB‚&æÖR"’’÷ ¢2ævWB‚'6W&–W5ö–B"’’ÓÒ¶W’÷ ¢‡F—FÆUö¶W’æB7G"‡2ævWB‚'6†÷uö¶W’"’÷"÷6†÷uö¶W’‡2ævWB‚&æÖR"’’’ÓÒF—FÆUö¶W’’’’ÂÓ¢–b–G‚ãÒ ¢fe²'6†÷w2%Òç÷†–G‚¢VÆ–b¶W“ ¢–G2Ò·6–Bf÷"6–B–â‡6†÷rævWB‚'6W&–W5ö–G2"’÷"·6†÷rævWB‚'6W&–W5ö–B"•Ò’–b6–Bæ÷B–â„æöæRÂ""•Ð¢fe²'6†÷w2%ÒæVæB‡²&6FÆöuö–B#¢6FÆöuö–BÀ¢'6W&–W5ö–B#¢–G5³Ò–b–G2VÇ6RæöæRÀ¢'6W&–W5ö–G2#¢–G2À¢'6†÷uö¶W’#¢F—FÆUö¶W’À¢&æÖR#¢6†÷rævWB‚&æÖR"Â""’À¢&6÷fW"#¢6†÷rævWB‚&6÷fW""Â""’À¢'–V"#¢6†÷rævWB‚'–V""Â""’À¢'&F–ær#¢6†÷rævWB‚'&F–ær"Â""—Ò¢FVÖõö6frÒÆöEö6öæf–r‚¢–bFVÖõö6frævWB‚'6WGWöFVÖõö6öçFVçB"“ ¢FVÖõö6fu²'6WGWöFVÖõö6öçFVçB%ÒÒfÇ6P¢6fUö6öæf–r†FVÖõö6fr¢ö–çfÆ–FFUöÆFW7EöW—6öFW5ö66†R‚¢VÆ–b7BÓÒ'&VÖ÷fU÷6†÷r# ¢¶W’Ò7G"‡–ÆöBævWB‚'6†÷uö¶W’"’÷"–ÆöBævWB‚'6W&–W5ö–B"Â""’¢fe²'6†÷w2%ÒÒ·2f÷"2–âfe²'6†÷w2%Ð¢–b7G"‡2ævWB‚&6FÆöuö–B"’÷"2ævWB‚'6†÷uö¶W’"’÷"÷6†÷uö¶W’‡2ævWB‚&æÖR"’’÷ ¢2ævWB‚'6W&–W5ö–B"’’Ò¶W•Ð¢ö–çfÆ–FFUöÆFW7EöW—6öFW5ö66†R‚¢VÆ–b7BÓÒ'FövvÆU÷FVÒ# ¢FVÒÒ–ÆöBævWB‚'FVÒ"’÷"·Ð¢æÖRÒ7G"‡FVÒævWB‚&æÖR"’÷"""’ç7G&—‚¢–G‚ÒæW‡B‚†’f÷"’Â—FVÒ–âVçVÖW&FR†fe²'FV×2%Ò¢–b7G"†—FVÒævWB‚&æÖR"’–b—6–ç7Fæ6R†—FVÒÂF–7B’VÇ6R—FVÒ’æÆ÷vW"‚¢ÓÒæÖRæÆ÷vW"‚’’ÂÓ¢–b–G‚ãÒ ¢fe²'FV×2%Òç÷†–G‚¢VÆ–bæÖS ¢FVÕö–BÒ7G"‡FVÒævWB‚'FVÕö–B"’÷"""¢fe²'FV×2%ÒæVæB‡²&æÖR#¢æÖRÀ¢'FVÕö–B#¢FVÕö–BÀ¢&Æövò#¢÷FVÕöÆövõ÷W&Â‡FVÕö–B—Ò¢VÆ–b7BÓÒ'&VÖ÷fU÷FVÒ# ¢æÖRÒ7G"‡–ÆöBævWB‚&æÖR"’÷"""’ç7G&—‚’æÆ÷vW"‚¢fe²'FV×2%ÒÒ¶—FVÒf÷"—FVÒ–âfe²'FV×2%Ð¢–b7G"†—FVÒævWB‚&æÖR"’–b—6–ç7Fæ6R†—FVÒÂF–7B’VÇ6R—FVÒ’æÆ÷vW"‚¢ÒæÖUÐ¢VÆ–b7BÓÒ'6WEöc÷FVÒ# ¢FVÒÒ–ÆöBævWB‚'FVÒ"’÷"·Ð¢6öç7G'V7F÷%ö–BÒ&Rç7V"‡"%µãÓ”Õ¦×¥òÕÒ"Â""Â7G"‡FVÒævWB‚&–B"’÷"""’¢æÖRÒ7G"‡FVÒævWB‚&æÖR"’÷"""’ç7G&—‚¢–b6öç7G'V7F÷%ö–BæBæÖS ¢fe²&c÷FV×2%ÒÒ·²&–B#¢6öç7G'V7F÷%ö–BÂ&æÖR#¢æÖRÀ¢&Æövò#¢öcöÆövõ÷W&Â†6öç7G'V7F÷%ö–B—ÕÐ¢VÇ6S ¢fe²&c÷FV×2%ÒÒµÐ¢6fUöff÷&—FW2†fb¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÀ¢&6FVv÷&–W2#¢fe²&6FVv÷&–W2%ÒÀ¢&6†ææVÅö–G2#¢¶2ævWB‚'7G&VÕö–B"’f÷"2–âfe²&6†ææVÇ2%ÕÒÀ¢&Ö÷f–Uö–G2#¢¶ÒævWB‚&6FÆöuö–B"’÷"ÒævWB‚'7G&VÕö–B"’f÷"Ò–âfe²&Ö÷f–W2%ÕÒÀ¢'6†÷uö–G2#¢·2ævWB‚&6FÆöuö–B"’÷"2ævWB‚'6†÷uö¶W’"’÷"÷6†÷uö¶W’‡2ævWB‚&æÖR"’’÷ ¢2ævWB‚'6W&–W5ö–B"’f÷"2–âfe²'6†÷w2%ÕÒÀ¢'FVÕöæÖW2#¢¶—FVÒævWB‚&æÖR"’–b—6–ç7Fæ6R†—FVÒÂF–7B’VÇ6R—FVÐ¢f÷"—FVÒ–âfe²'FV×2%ÕÒÀ¢&vÖUö–G2#¢¶—FVÒævWB‚&ö–B"’f÷"—FVÒ–âfbævWB‚&vÖW2"ÂµÒ•ÒÀ¢&c÷FV×2#¢fbævWB‚&c÷FV×2"ÂµÒ—Ò ¢–bRçF‚ÓÒ"ö’÷WFFUöF÷væÆöB# ¢F‚ÒF÷væÆöE÷WFFR‚¢–bFƒ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VWÒ¢&WGW&â6VÆbå÷6VæBƒSÂ²&ö²#¢fÇ6RÂ&W'&÷"#¢&F÷væÆöBf–ÆVB'Ò ¢–bRçF‚ÓÒ"ö’÷WFFU÷7FGW5ö6²# ¢G'“ ¢÷2ç&VÖ÷fR†÷2çF‚æ¦ö–â†öF—"‚’Â'WFFR×&öÆÆ&6²çG‡B"’¢W†6WBõ4W'&÷# ¢70¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VWÒ ¢–bRçF‚ÓÒ"ö’÷WFFU÷&W7F'B# ¢27vGfÖFUöæWrç’ÓâGfÖFRç’æB&VÆVæ6‚Âf–6ÖÆÂ†VÇW"à¢æWrÒ÷2çF‚æ¦ö–â†öF—"‚’Â'GfÖFUöæWrç’"¢7W"Ò÷2çF‚æ¦ö–â†öF—"‚’Â'GfÖFRç’"¢–bæ÷B÷2çF‚æW†—7G2†æWr“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&ö²#¢fÇ6RÂ&W'&÷"#¢&æòWFFRF÷væÆöFVB'Ò¢G'“ ¢÷&VÖ÷FU÷fW'6–öâÂ&V6÷fW'•÷6†Ò÷WFFUöÖæ–fW7B‚¢W‡V7FVE÷fW'6–öâÒ‡7G"…÷&VÖ÷FU÷fW'6–öâ÷"""’ç7G&—‚¢–b&RægVÆÆÖF6‚‡"%³Ó”Õ¦×¢åòÕÒ²"Â7G"…÷&VÖ÷FU÷fW'6–öâ÷"""’ç7G&—‚’¢VÇ6R""¢2FWFW&Ö–æR†÷rFò&VÆVæ6‚âôäÅ’&VÆVæ6‚F†RW&ÖæVçBÆVæ6†W ¢2æW†RÒæWfW"FV×ÖW‡G&7FVB—F†öâæW†R‡v†–6‚fæ—6†W2’à¢ÆVæ6†W%öW†RÒ÷2æVçf—&öâævWB‚%EdÔDUôU„R"¢&VÆVæ6‚ÒæöæP¢–bÆVæ6†W%öW†RæB÷2çF‚æW†—7G2†ÆVæ6†W%öW†R’æBÆVæ6†W%öW†RæÆ÷vW"‚’æVæG7v—F‚‚"æW†R"“ ¢&VÆVæ6‚Òr"r²ÆVæ6†W%öW†R²r"p¢VÆ–bvWFGG"‡7—2Â&g&÷¦Vâ"ÂfÇ6R’æB÷2çF‚æW†—7G2‡7—2æ&we³Ò“ ¢&VÆVæ6‚Òr"r²7—2æ&we³Ò²r"p¢2–bæ÷B'Vææ–ærg&öÒÆVæ6†W"öW†R†RærâÆ–â—F†öâFWb'Vâ’À¢2&VÆVæ6‚v—F‚F†R–çFW'&WFW"öæÇ’–b—Bw2&VÂÂ7F&ÆRF‚à¢VÆ–bæ÷BvWFGG"‡7—2Â&g&÷¦Vâ"ÂfÇ6R’æB'FV×"æ÷B–â‡7—2æW†V7WF&ÆR÷"""’æÆ÷vW"‚“ ¢&VÆVæ6‚Òr"r²7—2æW†V7WF&ÆR²r""r²7W"²r"p ¢–b7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"“ ¢†VÇW"Ò÷2çF‚æ¦ö–â†öF—"‚’Â%÷WFFRæ&B"¢ÆVæ6†W%öæÖRÒ÷2çF‚æ&6VæÖR†ÆVæ6†W%öW†R÷"""¢¶æ÷våöÆVæ6†W"Ò&ööÂ‡&RægVÆÆÖF6‚€¢""ƒó¤õEd×ÄöÆ÷5EdÖFR’ƒó¥Ç2¥Â…ÆBµÂ’“õÂæW†R"À¢ÆVæ6†W%öæÖRÂfÆw3×&Rä”täõ$T44R’¢Æ–æW2Ò²$V6†òöfeÇ%Æâ"À¢'F—FÆRWFF–ærEdÖFUÇ%Æâ"À¢v6BöB"r²öF—"‚’²r%Ç%ÆârÀ¢&V6†òåÇ%Æâ"À¢&V6†òWFF–ærEdÖFRââåÇ%Æâ"À¢&V6†òÆV6Rv—Bv†–ÆREdÖFR&W7F'G2åÇ%Æâ"À¢'F–ÖV÷WB÷B2öæö'&V²æçVÅÇ%Æâ%Ð¢2çV—F¶w2öÆBöæVf–ÆRÆVæ6†W"6â7W'f—fR—G26†–ÆBæ@¢2&WfVçB6ÆVâ&VÆVæ6‚â¶–ÆÂöæÇ’¶æ÷vâEdÖFR–ÖvRà¢–b¶æ÷våöÆVæ6†W# ¢Æ–æW2æW‡FVæB…²wF6¶¶–ÆÂöbö–Ò"r²ÆVæ6†W%öæÖR°¢r"æçVÂ#âcÇ%ÆârÀ¢'F–ÖV÷WB÷Böæö'&V²æçVÅÇ%Æâ%Ò¢Æ–æW2æW‡FVæB…°¢v6÷’÷’"r²7W"²r""r²7W"²ræ&6·W"æçVÂÇÂv÷Fò&6·Wf–ÆVEÇ%ÆârÀ¢&f÷"öÂRT’–âƒÃÃ#’Fò…Ç%Æâ"À¢rÖ÷fR÷’"r²æWr²r""r²7W"²r"æçVÂ#âcbbv÷FòWFFVEÇ%ÆârÀ¢"F–ÖV÷WB÷Böæö'&V²æçVÅÇ%Æâ"À¢"•Ç%Æâ"À¢&V6†òæ÷&ÖÂWFFRf–ÆVBâG'––ær6ÆVâF÷væÆöBââåÇ%Æâ%Ò¢–b&V6÷fW'•÷6† ¢5÷W&ÂÒUDDUõ45$•EõU$Âç&WÆ6R‚"r"Â"rr"¢5öæWrÒæWrç&WÆ6R‚"r"Â"rr"¢Æ–æW2æW‡FVæB…°¢vFVÂöb÷"r²7W"²r"æçVÂ#âcÇ%ÆârÀ¢vFVÂöb÷"r²æWr²r"æçVÂ#âcÇ%ÆârÀ¢w÷vW'6†VÆÂæW†RÔæõ&öf–ÆRÔW†V7WF–öåöÆ–7’'—72Ô6öÖÖæB"E&öw&W75&VfW&Væ6SÕÂu6–ÆVçFÇ”6öçF–çVUÂs²G'’²–çfö¶RÕvV%&WVW7BÕW6T&6–5'6–ærÕW&’Ârr²5÷W&Â²uÂrÔ÷WDf–ÆRÂrr²5öæWr²uÂs²–b‚„vWBÔf–ÆT†6‚ÔÆv÷&—F†Ò4„#SbÔÆ—FW&ÅF‚Ârr²5öæWr²uÂr’ä†6‚åFôÆ÷vW"‚’ÖæRÂrr²&V6÷fW'•÷6†²uÂr’²F‡&÷rÂv6†V6·7VÒÖ—6ÖF6…ÂrÒÒ6F6‚²&VÖ÷fRÔ—FVÒÔf÷&6RÔW'&÷$7F–öâ6–ÆVçFÇ”6öçF–çVRÔÆ—FW&ÅF‚Ârr²5öæWr²uÂs²W†—BÒ%Ç%ÆârÀ¢&–bW'&÷&ÆWfVÂv÷Fò&V6÷fW&f–ÆVEÇ%Æâ"À¢vÖ÷fR÷’"r²æWr²r""r²7W"²r"æçVÂ#âcÇÂv÷Fò&V6÷fW&f–ÆVEÇ%ÆârÀ¢&v÷FòWFFVEÇ%Æâ%Ò¢VÇ6S ¢Æ–æW2æVæB‚&V6†òWFFRÖæ–fW7B6†V6·7VÒ—2Væf–Æ&ÆRåÇ%Æâ"¢Æ–æW2æW‡FVæB…°¢#¦&6·Wf–ÆVEÇ%Æâ"À¢&V6†ò6÷VÆBæ÷B7&VFRF†RWFFR&6·WâF†RWFFRv26æ6VÆÆVBåÇ%Æâ"À¢&v÷Fò&öÆÆ&6·&VÆVæ6…Ç%Æâ"À¢#§&V6÷fW&f–ÆVEÇ%Æâ"À¢&V6†òWFFR–ç7FÆÆF–öâf–ÆVBâ&W7F÷&–ærF†R&Wf–÷W2fW'6–öâåÇ%Æâ"À¢v–bW†—7B"r²7W"²ræ&6·W"6÷’÷’"r²7W"²ræ&6·W""r²7W"²r"æçVÅÇ%ÆârÀ¢&v÷Fò&öÆÆ&6·&VÆVæ6…Ç%Æâ"À¢#§WFFVEÇ%Æâ"À¢&V6†òWFFR–ç7FÆÆVBâ7F'F–ærF†RæWrfW'6–öâf÷"†VÇF‚6†V6²åÇ%Æâ%Ò¢–b&VÆVæ6ƒ ¢†VÇF…÷W&ÂÒb&‡GG¢òó#rããã§¶–çB…ô5D•dUõõ%B—Òö’÷–ær ¢5ö†VÇF‚Ò‚rE&öw&W75&VfW&Væ6SÕÂu6–ÆVçFÇ”6öçF–çVUÂs²G'’²p¢rF£Ô–çfö¶RÕ&W7DÖWF†öBÕW6T&6–5'6–ærÕF–ÖV÷WE6V2"ÕW&’Ârr°¢†VÇF…÷W&Âç&WÆ6R‚"r"Â"rr"’²s÷WFFRÖ†VÇFƒÓÂs²p¢&–b‚F¢æÖæRvöÆ÷2×GfÖFRrÖ÷"F¢çfW'6–öâÖæRr"°¢W‡V7FVE÷fW'6–öâç&WÆ6R‚"r"Â"rr"’²"r’²W†—BÓ²W†—B ¢wÒ6F6‚²W†—BÒr¢Æ–æW2æW‡FVæB…°¢sâ"r²÷2çF‚æ¦ö–â†öF—"‚’Â'WFFRÖ–â×&öw&W72çG‡B"’²r"V6†òr²W‡V7FVE÷fW'6–öâ²uÇ%ÆârÀ¢w7F'B""r²&VÆVæ6‚²%Ç%Æâ"À¢&V6†òv—F–ærf÷"õEdÒb"²W‡V7FVE÷fW'6–öâ²"Fò&W÷'B†VÇF‡’ââåÇ%Æâ"À¢&f÷"öÂRT’–âƒÃÃ3R’Fò…Ç%Æâ"À¢"F–ÖV÷WB÷Böæö'&V²æçVÅÇ%Æâ"À¢r÷vW'6†VÆÂæW†RÔæõ&öf–ÆRÔW†V7WF–öåöÆ–7’'—72Ô6öÖÖæB"r²5ö†VÇF‚²r%Ç%ÆârÀ¢"–bæ÷BW'&÷&ÆWfVÂv÷Fò†VÇF†öµÇ%Æâ"À¢"•Ç%Æâ"À¢&V6†òæWrfW'6–öâf–ÆVB—G27F'GW†VÇF‚6†V6²â&öÆÆ–ær&6²åÇ%Æâ%Ò¢–b¶æ÷våöÆVæ6†W# ¢Æ–æW2æW‡FVæB…²wF6¶¶–ÆÂöbö–Ò"r²ÆVæ6†W%öæÖR²r"æçVÂ#âcÇ%ÆârÀ¢'F–ÖV÷WB÷Böæö'&V²æçVÅÇ%Æâ%Ò¢Æ–æW2æW‡FVæB…°¢v–bW†—7B"r²7W"²ræ&6·W"6÷’÷’"r²7W"²ræ&6·W""r²7W"²r"æçVÅÇ%ÆârÀ¢sâ"r²÷2çF‚æ¦ö–â†öF—"‚’Â'WFFR×&V¦V7FVBçG‡B"’²r"V6†òr²W‡V7FVE÷fW'6–öâ²uÇ%ÆârÀ¢sâ"r²÷2çF‚æ¦ö–â†öF—"‚’Â'WFFR×&öÆÆ&6²çG‡B"’²r"V6†òõEdÒbr²W‡V7FVE÷fW'6–öâ²rf–ÆVB—G27F'GW†VÇF‚6†V6²âF†R&Wf–÷W2fW'6–öâv2&W7F÷&VBÂæBF†R&BWFFRv–ÆÂ&R6¶—VBVçF–ÂæWvW"&VÆV6R—2f–Æ&ÆRåÇ%ÆârÀ¢&v÷Fò&öÆÆ&6·&VÆVæ6…Ç%Æâ"À¢#¦†VÇF†öµÇ%Æâ"À¢vFVÂöb÷"r²÷2çF‚æ¦ö–â†öF—"‚’Â'WFFRÖ–â×&öw&W72çG‡B"’²r"æçVÂ#âcÇ%ÆârÀ¢vFVÂöb÷"r²÷2çF‚æ¦ö–â†öF—"‚’Â'WFFR×&öÆÆ&6²çG‡B"’²r"æçVÂ#âcÇ%ÆârÀ¢vFVÂöb÷"r²÷2çF‚æ¦ö–â†öF—"‚’Â'WFFR×&V¦V7FVBçG‡B"’²r"æçVÂ#âcÇ%ÆârÀ¢&V6†òõEdÒb"²W‡V7FVE÷fW'6–öâ²"7F'FVB7V66W76gVÆÇ’åÇ%Æâ"À¢&v÷FòFöæUÇ%Æâ"À¢#§&öÆÆ&6·&VÆVæ6…Ç%Æâ"À¢vFVÂöb÷"r²÷2çF‚æ¦ö–â†öF—"‚’Â'WFFRÖ–â×&öw&W72çG‡B"’²r"æçVÂ#âcÇ%ÆârÀ¢&V6†ò7F'F–ærF†R&W7F÷&VBõEdÒfW'6–öâââåÇ%Æâ"À¢w7F'B""r²&VÆVæ6‚²%Ç%Æâ%Ò¢VÇ6S ¢Æ–æW2æW‡FVæB…²#§&öÆÆ&6·&VÆVæ6…Ç%Æâ"Â&v÷FòFöæUÇ%Æâ%Ò¢Æ–æW2æW‡FVæB…²#¦FöæUÇ%Æâ"À¢'F–ÖV÷WB÷B2öæö'&V²æçVÅÇ%Æâ"À¢vFVÂ"Wæc%Ç%ÆâuÒ¢v—F‚÷Vâ††VÇW"Â'r"ÂVæ6öF–æsÒ'WFbÓ‚"ÂæWvÆ–æSÒ""’2c ¢bçw&—FVÆ–æW2†Æ–æW2¢2F†R†VÇW"×W7B7W'f—fRF†—26W'fW"W†—F–ærÂ'WB—BFöW0¢2æ÷BæVVBf—6–&ÆR6öç6öÆRâæWr6öç6öÆRÇ6òvWG0¢2–æ†W&—FVB'’ÆVv7’ÆVæ6†W'2æBW‡÷6W2†&ÖÆW72…EE ¢2F—66öææV7BG&6V&6·2–â6V6öæB6öÖÖæBv–æF÷rà¢fÆw2ÒvWFGG"‡7V'&ö6W72Â$5$TDUôäõõt”äDõr"Âƒƒ¢7V'&ö6W72å÷Vâ…²&6ÖBæW†R"Â"öB"Â"ö2"Â†VÇW%ÒÀ¢7vCÖöF—"‚’Â7&VF–öæfÆw3ÖfÆw2À¢7FF–ã×7V'&ö6W72äDUdåTÄÂÀ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÀ¢7FFW'#×7V'&ö6W72äDUdåTÄÂÀ¢6Æ÷6UöfG3ÕG'VR¢VÇ6S ¢†VÇW"Ò÷2çF‚æ¦ö–â†öF—"‚’Â%÷WFFRç6‚"¢&öG’Ò"2ö&–â÷6…Æç6ÆVW%Ææ7Öbr"²7W"²"rr"²7W"²"æ&6·WuÆæ×bÖbr"²æWr²"rr"²7W"²"uÆâ ¢–b&VÆVæ6ƒ ¢&öG’³Ò&VÆVæ6‚²"eÆâ ¢&öG’³Òw&ÒÒÒ"C%Æâp¢v—F‚÷Vâ††VÇW"Â'r"ÂVæ6öF–æsÒ'WFbÓ‚"’2c ¢bçw&—FR†&öG’¢÷2æ6†ÖöB††VÇW"ÂósSR¢7V'&ö6W72å÷Vâ…²"ö&–â÷6‚"Â†VÇW%ÒÂ7F'EöæWu÷6W76–öãÕG'VR¢FVbö'–R‚“ ¢–×÷'BF–ÖR2÷C²÷Bç6ÆVWƒ“²÷2åöW†—Bƒ¢–×÷'BF‡&VF–ær2÷Fƒ²÷F‚åF‡&VB‡F&vWCÕö'–RÂFVÖöãÕG'VR’ç7F'B‚¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'&VÆVæ6‚#¢&ööÂ‡&VÆVæ6‚’À¢&W‡V7FVE÷fW'6–öâ#¢W‡V7FVE÷fW'6–öâÀ¢&WFöÖF–5÷&öÆÆ&6²#¢&ööÂ‡&VÆVæ6‚æB7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"’—Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&ö²#¢fÇ6RÂ&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’ö÷VåöföÆFW"# ¢föÆFW"ÒöF—"‚¢G'“ ¢–b7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"“ ¢÷2ç7F'Ff–ÆR†föÆFW"’2G—S¢–væ÷&U¶GG"ÖFVf–æVEÐ¢VÆ–b7—2çÆFf÷&ÒÓÒ&F'v–â# ¢7V'&ö6W72å÷Vâ…²&÷Vâ"ÂföÆFW%Ò¢VÇ6S ¢7V'&ö6W72å÷Vâ…²'†FrÖ÷Vâ"ÂföÆFW%Ò¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ'F‚#¢föÆFW'Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&ö²#¢fÇ6RÂ&W'&÷"#¢7G"†R’Â'F‚#¢föÆFW'Ò ¢–bRçF‚ÓÒ"ö’÷Æ’# ¢2ÆVæ6‚dÄ2v—F‚7G&VÒW&Â‡7G&VÕö–BÓâG2W&Â’à¢6–BÒ7G"‡–ÆöBævWB‚'7G&VÕö–B"Â""’’ç7G&—‚¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‡‚æ6öæf–wW&VB‚’æB6–B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B&WVW7B'Ò¢–b6VÆbåö—5÷&—fFU÷&VÖ÷FUöÆ—7FVæW"‚’æB6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"“ ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÀ¢'Æ–Æ—7B#¢"ö’÷&VÖ÷FU÷fÆ3ö–CÒ"°¢W&ÆÆ–"ç'6RçV÷FR‡6–BÂ6fSÒ""—Ò¢W&ÂÒ‚ç7G&VÕ÷W&Â‡6–B¢fÆ2Òöf–æE÷fÆ2‚¢–bæ÷BfÆ3 ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢%dÄ2æ÷Bf÷VæBâ–ç7FÆÂdÄ2÷"W6R6÷’â'Ò¢G'“ ¢7V'&ö6W72å÷Vâ…·fÆ2ÂW&ÅÒÀ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÂ7FFW'#×7V'&ö6W72äDUdåTÄÂ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VWÒ¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’÷Æ•öÖ÷f–R# ¢6–BÒ7G"‡–ÆöBævWB‚'7G&VÕö–B"Â""’’ç7G&—‚¢W‡BÒ7G"‡–ÆöBævWB‚&W‡FVç6–öâ"Â&×B"’’ç7G&—‚¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‡‚æ6öæf–wW&VB‚’æB6–B“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢&&B&WVW7B'Ò¢fÆ2Òöf–æE÷fÆ2‚¢–bæ÷BfÆ3 ¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢%dÄ2æ÷Bf÷VæBâ'Ò¢G'“ ¢7V'&ö6W72å÷Vâ…·fÆ2Â‚æÖ÷f–U÷W&Â‡6–BÂW‡B•ÒÀ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÂ7FFW'#×7V'&ö6W72äDUdåTÄÂ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VWÒ¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’÷Æ•öW—6öFR# ¢W—6öFUö–BÒ7G"‡–ÆöBævWB‚&W—6öFUö–B"Â""’’ç7G&—‚¢W‡BÒ7G"‡–ÆöBævWB‚&W‡FVç6–öâ"Â&×B"’’ç7G&—‚¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢fÆ2Òöf–æE÷fÆ2‚¢–bæ÷B‡‚æ6öæf–wW&VB‚’æBW—6öFUö–BæBfÆ2“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢%dÄ2æ÷Bf÷VæB÷"W—6öFR—2–çfÆ–Bâ'Ò¢G'“ ¢7V'&ö6W72å÷Vâ…·fÆ2Â‚æW—6öFU÷W&Â†W—6öFUö–BÂW‡B•ÒÀ¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÂ7FFW'#×7V'&ö6W72äDUdåTÄÂ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VWÒ¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’÷Æ•÷6V6öâ# ¢W—6öFW2Ò–ÆöBævWB‚&W—6öFW2"’÷"µÐ¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢fÆ2Òöf–æE÷fÆ2‚¢W&Ç2Ò·‚æW—6öFU÷W&Â†WævWB‚&–B"’ÂWævWB‚&W‡FVç6–öâ"Â&×B"’¢f÷"W–âW—6öFW2–bWævWB‚&–B"’—2æ÷BæöæUÐ¢–bæ÷B‡‚æ6öæf–wW&VB‚’æBW&Ç2æBfÆ2“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢%dÄ2æ÷Bf÷VæB÷"6V6öâ—2V×G’â'Ò¢G'“ ¢7V'&ö6W72å÷Vâ…·fÆ5Ò²W&Ç2À¢7FF÷WC×7V'&ö6W72äDUdåTÄÂÂ7FFW'#×7V'&ö6W72äDUdåTÄÂ¢&WGW&â6VÆbå÷6VæBƒ#Â²&ö²#¢G'VRÂ&6÷VçB#¢ÆVâ‡W&Ç2—Ò¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&W'&÷"#¢7G"†R—Ò ¢–bRçF‚ÓÒ"ö’öÓ7R# ¢2'V–ÆBâÓ5Rg&öÒ6VÆV7FVB6FVv÷&–W2æBö÷"7V6–f–27G&VÕö–G2à¢6frÒÆöEö6öæf–r‚¢‚Ò‡G&VÒ†6fr¢–bæ÷B‚æ6öæf–wW&VB‚“ ¢&WGW&â6VÆbå÷6VæBƒCÂ²&W'&÷"#¢$æ÷B6öæf–wW&VB'Ò¢G'“ ¢6†ææVÇ2Â6G2ÒvWE÷‡G&VÕö6†ææVÇ2†6fr¢W†6WBW†6WF–öâ2S ¢&WGW&â6VÆbå÷6VæBƒSÂ²&W'&÷"#¢7G"†R—Ò¢6VÅö6G2Ò6WB‡–ÆöBævWB‚&6FVv÷&–W2"’÷"µÒ¢6VÅö–G2Ò6WB‡7G"†’’f÷"’–â‡–ÆöBævWB‚'7G&VÕö–G2"’÷"µÒ’¢ÖöFRÒ–ÆöBævWB‚&ÖöFR"Â&6FVv÷&–W2"’2&6FVv÷&–W2"÷"&6†ææVÇ2 ¢Æ–æW2Ò²"4U…DÓ5R%Ð¢âÒ ¢f÷"6‚–â6†ææVÇ3 ¢6FæÖRÒ6G2ævWB†6…²&6FVv÷'•ö–B%ÒÂ""¢–æ6ÇVFRÒfÇ6P¢–bÖöFRÓÒ&6†ææVÇ2# ¢–æ6ÇVFRÒ7G"†6…²'7G&VÕö–B%Ò’–â6VÅö–G0¢VÇ6S¢26FVv÷&–W0¢–æ6ÇVFRÒ6FæÖR–â6VÅö6G0¢–bæ÷B–æ6ÇVFS ¢6öçF–çVP¢æÖRÒ6…²&æÖR%Ð¢w'Ò6FæÖRç&WÆ6R‚"Â"Â""¢–6öâÒ7G"†6‚ævWB‚'7G&VÕö–6öâ"’÷"""’ç&WÆ6R‚r"rÂrS#"r¢ÆövõöGG"ÒbrGfrÖÆövóÒ'¶–6öçÒ"r–b–6öâVÇ6R" ¢Æ–æW2æVæB†br4U…D”äc¢Ów&÷W×F—FÆSÒ'¶w'Ò'¶ÆövõöGG'ÒÇ¶æÖWÒr¢Æ–æW2æVæB‡‚ç7G&VÕ÷W&Â†6…²'7G&VÕö–B%Ò’¢â³Ò¢&öG’Ò%Æâ"æ¦ö–â†Æ–æW2’²%Æâ ¢FFÒ&öG’æVæ6öFR‚'WFbÓ‚"¢6VÆbç6VæE÷&W7öç6Rƒ#¢6VÆbç6VæEö†VFW"‚$6öçFVçBÕG—R"Â&VF–ò÷‚Ö×VwW&Ã²6†'6WC×WFbÓ‚"¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔF—7÷6—F–öâ"ÂvGF6†ÖVçC²f–ÆVæÖSÒ'Æ–Æ—7BæÓ7R"r¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔÆVæwF‚"Â7G"†ÆVâ†FF’’¢6VÆbæVæEö†VFW'2‚¢6VÆbçvf–ÆRçw&—FR†FF¢&WGW&à¢&WGW&â6VÆbå÷6VæBƒCBÂ²&W'&÷"#¢&æ÷Bf÷VæB'Ò ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÐ¢2Ö–à¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÐ ¥õ5DõôUdTåBÒF‡&VF–æräWfVçB‚¥ôÄåõ4U%dU"ÒæöæP¥ôÄåõ4U%dU%ôÄô4²ÒF‡&VF–ærå$Æö6²‚¥õ$TÔõDUõ4U%dU"ÒæöæP¥õ$TÔõDUõ4U%dU%ôÄô4²ÒF‡&VF–ærå$Æö6²‚ ¦FVb÷7F'EöÆå÷6W'fW"‚“ ¢""%7F'B÷F–öæÂv’Ôf’66W72gFW"Æö6Æ†÷7B—2Ç&VG’†VÇF‡’â"" ¢vÆö&ÂôÄåõ4U%dU"Âô5D•dUôÄåõõ%@¢v—F‚ôÄåõ4U%dU%ôÄô4³ ¢–bôÄåõ4U%dU"—2æ÷BæöæS ¢&WGW&âG'VP¢6frÒÆöEö6öæf–r‚¢–bæ÷B6frævWB‚&ÆÆ÷uöÆâ"“ ¢&WGW&âfÇ6P¢†÷7BÒöÆö6ÅöÆåö—‚¢–bæ÷B†÷7C ¢6fu²&Æåö&–æEöW'&÷"%ÒÒ$æòÆö6Âv’Ôf’FG&W72v2f÷VæB ¢6fUö6öæf–r†6fr¢&WGW&âfÇ6P¢W'&÷'2ÒµÐ¢f÷"6æF–FFR–â&ævR„Äåõõ%BÂÄåõõ%B²“ ¢G'“ ¢Æå÷6W'fW"ÒF‡&VF–æt…EE6W'fW"‚††÷7BÂ6æF–FFR’Â†æFÆW"¢F‡&VF–æråF‡&VB‡F&vWCÖÆå÷6W'fW"ç6W'fUöf÷&WfW"ÂFVÖöãÕG'VR’ç7F'B‚¢ôÄåõ4U%dU"ÒÆå÷6W'fW ¢ô5D•dUôÄåõõ%BÒ6æF–FFP¢6frç÷‚&Æåö&–æEöW'&÷""ÂæöæR¢6fUö6öæf–r†6fr¢&WGW&âG'VP¢W†6WBõ4W'&÷"2&–æEöW'&÷# ¢W'&÷'2æVæB†b'¶6æF–FFWÓ¢¶&–æEöW'&÷'Ò"¢6fu²&Æåö&–æEöW'&÷"%ÒÒ$6÷VÆBæ÷B7F'Bv’Ôf’66W72‚"²#²"æ¦ö–â†W'&÷'2’²"’ ¢6fUö6öæf–r†6fr¢&WGW&âfÇ6P ¦FVb÷7F÷öÆå÷6W'fW"‚“ ¢vÆö&ÂôÄåõ4U%dU"Âô5D•dUôÄåõõ%@¢v—F‚ôÄåõ4U%dU%ôÄô4³ ¢Æå÷6W'fW"ÒôÄåõ4U%dU ¢ôÄåõ4U%dU"ÒæöæP¢ô5D•dUôÄåõõ%BÒ ¢–bÆå÷6W'fW"—2æ÷BæöæS ¢Æå÷6W'fW"ç6‡WFF÷vâ‚¢Æå÷6W'fW"ç6W'fW%ö6Æ÷6R‚ ¦FVb÷7F'E÷&VÖ÷FU÷6W'fW"‚“ ¢""$&–æBF†RW‡W&–ÖVçFÂ&VÆ’öæÇ’FòF†—2FWf–6Rw2F–Ç66ÆRFG&W72â"" ¢vÆö&Âõ$TÔõDUõ4U%dU"Âô5D•dUõ$TÔõDUõõ%@¢v—F‚õ$TÔõDUõ4U%dU%ôÄô4³ ¢–bõ$TÔõDUõ4U%dU"—2æ÷BæöæS ¢&WGW&âG'VP¢6frÒÆöEö6öæf–r‚¢–bæ÷B†6frævWB‚&FWeöÖöFR"’æB6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"’“ ¢&WGW&âfÇ6P¢†÷7BÒ÷F–Ç66ÆUö—cB‚¢–bæ÷B†÷7C ¢6fu²'&—fFU÷&VÖ÷FUöW'&÷"%ÒÒ%F–Ç66ÆR—2æ÷B6öææV7FVBöâF†—22 ¢6fUö6öæf–r†6fr¢&WGW&âfÇ6P¢W'&÷'2ÒµÐ¢f÷"6æF–FFR–â&ævR…$TÔõDUõõ%BÂ$TÔõDUõõ%B²“ ¢G'“ ¢&VÖ÷FU÷6W'fW"ÒF‡&VF–æt…EE6W'fW"‚††÷7BÂ6æF–FFR’Â†æFÆW"¢F‡&VF–æråF‡&VB‡F&vWC×&VÖ÷FU÷6W'fW"ç6W'fUöf÷&WfW"ÂFVÖöãÕG'VR’ç7F'B‚¢õ$TÔõDUõ4U%dU"Ò&VÖ÷FU÷6W'fW ¢ô5D•dUõ$TÔõDUõõ%BÒ6æF–FFP¢6frç÷‚'&—fFU÷&VÖ÷FUöW'&÷""ÂæöæR¢6fUö6öæf–r†6fr¢&WGW&âG'VP¢W†6WBõ4W'&÷"2&–æEöW'&÷# ¢W'&÷'2æVæB†b'¶6æF–FFWÓ¢¶&–æEöW'&÷'Ò"¢6fu²'&—fFU÷&VÖ÷FUöW'&÷"%ÒÒ$6÷VÆBæ÷B&–æBF†RF–Ç66ÆR&VÆ’‚"²#²"æ¦ö–â†W'&÷'2’²"’ ¢6fUö6öæf–r†6fr¢&WGW&âfÇ6P ¦FVb÷7F÷÷&VÖ÷FU÷6W'fW"‚“ ¢vÆö&Âõ$TÔõDUõ4U%dU"Âô5D•dUõ$TÔõDUõõ%@¢v—F‚õ$TÔõDUõ4U%dU%ôÄô4³ ¢&VÖ÷FU÷6W'fW"Òõ$TÔõDUõ4U%dU ¢õ$TÔõDUõ4U%dU"ÒæöæP¢ô5D•dUõ$TÔõDUõõ%BÒ ¢–b&VÖ÷FU÷6W'fW"—2æ÷BæöæS ¢&VÖ÷FU÷6W'fW"ç6‡WFF÷vâ‚¢&VÖ÷FU÷6W'fW"ç6W'fW%ö6Æ÷6R‚ ¦FVböWFõ÷6‡WFF÷vå÷vF6†För‚“ ¢v†–ÆRæ÷Bõ5DõôUdTåBçv—BƒR“ ¢G'“ ¢6frÒÆöEö6öæf–r‚¢–b†6frævWB‚&FWeöÖöFR"’æB6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"’æ@¢õ$TÔõDUõ4U%dU"—2æöæR“ ¢÷7F'E÷&VÖ÷FU÷6W'fW"‚¢Ö–çWFW2ÒÖ‚ƒÂ–çB†6frævWB‚&WFõ÷6‡WFF÷våöÖ–çWFW2"’÷"’¢–b6frævWB‚&†–FUö6ÖE÷v–æF÷r"’æBÖ–çWFW2æBö–æ7F—fU÷6V6öæG2‚’ãÒÖ–çWFW2¢c ¢õ5DõôUdTåBç6WB‚¢&WGW&à¢W†6WBW†6WF–öã ¢70 ¦FVböVæ&ÆUöç6’‚“ ¢""%GW&âöâå4’6öÆ÷"–âF†Rv–æF÷w26öç6öÆRâ6–æ6R6öÖRVçf—&öæÖVçG0¢†Rærâ6ö×–ÆVBöæVf–ÆRW†W2’7W÷'B6öÆ÷"WfVâv†VâF†R†æFÆRFæ6P¢f–Ç2ÂvRFVfVÇBFòG'VRæB§W7BE%’FòVæ&ÆReB&ö6W76–ærâ"" ¢–bæ÷B7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"“ ¢&WGW&âG'VP¢G'“ ¢–×÷'B7G—W0¢²Ò7G—W2çv–æFÆÂæ¶W&æVÃ3 ¢f÷"†æFÆUö–B–â‚ÓÂÓ"“¢27FF÷WBÂ7FFW' ¢‚Ò²ävWE7FD†æFÆR††æFÆUö–B¢ÖöFRÒ7G—W2æ5÷V–çC3"‚¢–b²ävWD6öç6öÆTÖöFR†‚Â7G—W2æ'—&Vb†ÖöFR’“ ¢²å6WD6öç6öÆTÖöFR†‚ÂÖöFRçfÇVRÂƒB’2Tä$ÄUõd•%ETÅõDU$Ô”äÅõ$ô4U54”äp¢W†6WBW†6WF–öã ¢70¢277VÖR6öÆ÷"v÷&·2‡F†R6öç6öÆR†26†÷vâå4’6öÆ÷"&Vf÷&R’à¢&WGW&âG'VP ¦FVb÷6WEö6öç6öÆU÷f—6–&ÆR‡f—6–&ÆR“ ¢""$GF6‚öFWF6‚F†—2&ö6W72rv–æF÷w26öç6öÆRâæòÖ÷VÇ6Wv†W&Râ"" ¢–bæ÷B7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"“ ¢&WGW&à¢G'“ ¢–×÷'B7G—W0¢²Ò7G—W2çv–æFÆÂæ¶W&æVÃ3 ¢–bæ÷Bf—6–&ÆS ¢25uô„”DR6â&V6öÖRÖ–æ–Ö—¦R÷W&F–öâVæFW"v–æF÷w2FW&Ö–æÂà¢2FWF6†–ær6Æ÷6W2F†R6öç6öÆRf÷"æ÷&ÖÂF÷V&ÆRÖ6Æ–6²ÆVæ6‚à¢²äg&VT6öç6öÆR‚¢&WGW&à¢‡væBÒ²ävWD6öç6öÆUv–æF÷r‚¢2F†RuT’×7V'7—7FVÒöæVf–ÆRÆVæ6†W"6âÆVfRW2GF6†VBFòà¢2–çf—6–&ÆRô6öåE’6öç6öÆRâ6†÷uv–æF÷r6ææ÷BÖ¶RF†BW6&ÆRf÷ ¢2&WG&òÖöFRÂ6òÆVæ6†W"×7F'FVB6W76–öç2FVÆ–&W&FVÇ’&WÆ6R—@¢2v—F‚g&W6‚6öç6öÆRv–æF÷râF—&V7B—F†öâGfÖFRç–'Vç2¶VW ¢2F†V—"W†—7F–ærFW&Ö–æÂà¢–bf—6–&ÆRæB÷2æVçf—&öâævWB‚%EdÔDUôU„R"’æB‡væC ¢²äg&VT6öç6öÆR‚¢‡væBÒæöæP¢–bæ÷B‡væBæB²äÆÆö46öç6öÆR‚“ ¢2&V6öææV7B—F†öâw27FæF&B7G&V×2v†Vâ7v—F6†–ær&6²Fò&WG&ð¢2ÖöFR–âF†R7W'&VçB6W76–öââ&W7F'Bv–ÆÂ&W7F÷&RF†VÒFöòà¢G'“ ¢7—2ç7FF–âÒ÷Vâ‚$4ôä”âB"Â'""ÂVæ6öF–æsÒ'WFbÓ‚"ÂW'&÷'3Ò'&WÆ6R"¢7—2ç7FF÷WBÒ÷Vâ‚$4ôäõUBB"Â'r"ÂVæ6öF–æsÒ'WFbÓ‚"ÂW'&÷'3Ò'&WÆ6R"Â'VffW&–æsÓ¢7—2ç7FFW'"Ò÷Vâ‚$4ôäõUBB"Â'r"ÂVæ6öF–æsÒ'WFbÓ‚"ÂW'&÷'3Ò'&WÆ6R"Â'VffW&–æsÓ¢W†6WBW†6WF–öã ¢70¢‡væBÒ²ävWD6öç6öÆUv–æF÷r‚¢–b‡væC ¢G'“ ¢²å6WD6öç6öÆUF—FÆUr‚$öÆòw2EdÖFRÒ&WG&ò44”’ÖöFR"¢W†6WBW†6WF–öã ¢70¢7G—W2çv–æFÆÂçW6W#3"å6†÷uv–æF÷r†‡væBÂR’25uõ4„õp¢W†6WBW†6WF–öã ¢70 ¦FVböÆVæ6…÷v—F†÷WEö6öç6öÆR‚“ ¢""%&VÆVæ6‚EdÖFR2vVçV–æR6öç6öÆRÖÆW72v–æF÷w2&ö6W72â"" ¢–bæ÷B7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"“ ¢&WGW&âfÇ6P¢G'“ ¢VçbÒ÷2æVçf—&öâæ6÷’‚¢Vçe²%EdÔDUô„”DDTåô4„”ÄB%ÒÒ# ¢–bvWFGG"‡7—2Â&g&÷¦Vâ"ÂfÇ6R“ ¢6ÖBÒ·7—2æW†V7WF&ÆUÒ²7—2æ&we³¥Ð¢VÇ6S ¢6ÖBÒ·7—2æW†V7WF&ÆRÂ÷2çF‚æ'7F‚…õöf–ÆUõò•Ò²7—2æ&we³¥Ð¢fÆw2ÒvWFGG"‡7V'&ö6W72Â$5$TDUôäõõt”äDõr"Âƒƒ¢7V'&ö6W72å÷Vâ†6ÖBÂVçcÖVçbÂ7&VF–öæfÆw3ÖfÆw2À¢7FF–ã×7V'&ö6W72äDUdåTÄÂÂ7FF÷WC×7V'&ö6W72äDUdåTÄÂÀ¢7FFW'#×7V'&ö6W72äDUdåTÄÂÂ6Æ÷6UöfG3ÕG'VR¢&WGW&âG'VP¢W†6WBW†6WF–öã ¢&WGW&âfÇ6P ¦FVbö6Æ÷6UöÆVæ6†W%ö6öç6öÆR‚“ ¢""$6Æ÷6RF†RFVF–6FVBGfÖFRæW†R6öç6öÆRv—F†÷WBF÷V6†–ærW6W"FW&Ö–æÇ2â"" ¢–bæ÷B7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"“ ¢&WGW&à¢2F†RW&ÖæVçBEdÖFRÆVæ6†W"6WG2F†—2âFòæ÷B6Æ÷6R6öç6öÆRv†VâF†P¢267&—Bv27F'FVBÖçVÆÇ’v—F‚—F†öâGfÖFRç–à¢ÆVæ6†W"Ò÷2æVçf—&öâævWB‚%EdÔDUôU„R"Â""’ç7G&—‚¢–bæ÷BÆVæ6†W"÷"æ÷BÆVæ6†W"æÆ÷vW"‚’æVæG7v—F‚‚"æW†R"“ ¢&WGW&à¢G'“ ¢–×÷'B7G—W0¢g&öÒ7G—W2–×÷'Bv–çG—W0¢²Ò7G—W2çv–æFÆÂæ¶W&æVÃ3 ¢ÆVæ6†W%öæ÷&ÒÒ÷2çF‚ææ÷&Ö66R†÷2çF‚æ'7F‚†ÆVæ6†W"’ ¢2–bF†RW&ÖæVçBÆVæ6†W"—2æ÷F†W"&ö6W72GF6†VBFòF†—26ÖP¢26öç6öÆRÂ7F÷F†BW†7BW†V7WF&ÆRâF†—2—2FVÆ–&W&FVÇ’7G&–7FW ¢2F†â¶–ÆÆ–ær&VçB6ÖBæW†R÷FW&Ö–æÂ&ö6W72à¢–G2Ò‡v–çG—W2äEtõ$B¢3"’‚¢6÷VçBÒ²ävWD6öç6öÆU&ö6W74Æ—7B‡–G2ÂÆVâ‡–G2’¢–b6÷VçBâÆVâ‡–G2“ ¢–G2Ò‡v–çG—W2äEtõ$B¢6÷VçB’‚¢6÷VçBÒ²ävWD6öç6öÆU&ö6W74Æ—7B‡–G2ÂÆVâ‡–G2’¢$ô4U55õDU$Ô”äDRÒƒ¢$ô4U55õTU%•ôÄ”Ô•DTEô”ädõ$ÔD”ôâÒƒ ¢f÷"–B–âÆ—7B‡–G2•³¦6÷VçEÓ ¢–bæ÷B–B÷"–BÓÒ÷2ævWG–B‚“ ¢6öçF–çVP¢‡&ö2Ò²ä÷Vå&ö6W72…$ô4U55õDU$Ô”äDRÂ$ô4U55õTU%•ôÄ”Ô•DTEô”ädõ$ÔD”ôâÀ¢fÇ6RÂ–B¢–bæ÷B‡&ö3 ¢6öçF–çVP¢G'“ ¢6—¦RÒv–çG—W2äEtõ$Bƒ3#sc‚¢'VbÒ7G—W2æ7&VFU÷Væ–6öFUö'VffW"‡6—¦RçfÇVR¢–b²åVW'”gVÆÅ&ö6W74–ÖvTæÖUr†‡&ö2ÂÂ'VbÂ7G—W2æ'—&Vb‡6—¦R’“ ¢&ö5öæ÷&ÒÒ÷2çF‚ææ÷&Ö66R†÷2çF‚æ'7F‚†'VbçfÇVR’¢–b&ö5öæ÷&ÒÓÒÆVæ6†W%öæ÷&Ó ¢²åFW&Ö–æFU&ö6W72†‡&ö2Â¢f–æÆÇ“ ¢²ä6Æ÷6T†æFÆR†‡&ö2 ¢‡væBÒ²ävWD6öç6öÆUv–æF÷r‚¢–b‡væC ¢2tÕô4Äõ4R6Æ÷6W2F†RFVF–6FVB6öç6öÆRv–æF÷râg&VT6öç6öÆRÆöæP¢2öæÇ’FWF6†W2—F†öâæB6âÆVfRGfÖFRæW†Rw2V×G’v–æF÷rWà¢7G—W2çv–æFÆÂçW6W#3"å÷7DÖW76vUr†‡væBÂƒÂÂ’2tÕô4Äõ4P¢W†6WBW†6WF–öã ¢70 ¥ôtôÄBÒ%Ã35³“6Ò"2'&–v‡B–VÆÆ÷r‡7—'WvöÆB¥õ$U4UBÒ%Ã35³Ò  ¦FVbö6öÆ÷&VEö&ææW"‡W6Uö6öÆ÷"“ ¢""%&WGW&âF†R&ææW"v—F‚F†Ræ6¶RFvÆ–æR–âvöÆBâ"" ¢–bæ÷BW6Uö6öÆ÷# ¢&WGW&â$ääU ¢÷WBÒµÐ¢f÷"Æ–æR–â$ääU"ç7Æ—B‚%Æâ"“ ¢–b%FV6†æ–6ÆÇ’Eb"–âÆ–æR÷"%7—&—GVÆÇ’æ6¶R"–âÆ–æS ¢26öÆ÷"§W7BF†RFvÆ–æRFW‡BÂ¶VWF†REb'B&Vf÷&R—BVæ6öÆ÷&V@¢–G‚ÒÆ–æRæf–æB‚'â"’–b'â"–âÆ–æRVÇ6RÆ–æRæf–æB‚%7—&—GVÆÇ’"¢–b–G‚â ¢÷WBæVæB†Æ–æU³¦–G…Ò²ôtôÄB²Æ–æU¶–Gƒ¥Ò²õ$U4UB¢VÇ6S ¢÷WBæVæB…ôtôÄB²Æ–æR²õ$U4UB¢VÇ6S ¢÷WBæVæB†Æ–æR¢&WGW&â%Æâ"æ¦ö–â†÷WB ¤TåDU%õ$ôÕE2Ò°¢%&W72VçFW"‡F†Ræ6¶W2&RvWGF–ær6öÆB’"À¢%–÷W"F&ÆRw2&VG’Ò&W72VçFW""À¢$w&–FFÆRw2†÷Bâ&W72VçFW"FòvWBfÆ—–âr"À¢%&VG’Fò6öö²v†Vâ–÷R&Râââ&W72VçFW""À¢%÷vW&VB'’æ6¶W2æBVW7F–öæ&ÆRFV6—6–öç2âââç&W72VçFW""À¥Ð ¦FVböW†—7F–æu÷GfÖFR‡÷'B“ ¢""%&WGW&âG'VRöæÇ’v†VâF†R6W'f–6RÇ&VG’öâ§÷'B¢—2öÆòw2EdÖFRà ¢F†—2FVÆ–&W&FVÇ’¶W—2öfbF†R'Vææ–ærvV"&F†W"F†âÆVæ6†W ¢f–ÆVæÖRâõEdÒæW†RÂöÆõEdÖFRæW†RæBv–æF÷w26÷–W27V6‚0¢õEdÒƒ"’æW†VF†W&Vf÷&RÆÂ6†&RF†R6ÖR6–ævÆRÖ–ç7Fæ6R6†V6²à¢"" ¢&6RÒb&‡GG¢òó#rããã§¶–çB‡÷'B—Ò ¢2æWr'V–ÆG2W‡÷6R6†VW‡Æ–6—B–FVçF—G’VæGö–çBâ¶VWF†R&ö÷@¢2fÆÆ&6²6òæWrÆVæ6†W"Ç6òFWFV7G2âöÆFW"EdÖFRÇ&VG’'Vææ–ærà¢G'“ ¢v—F‚W&ÆÆ–"ç&WVW7BçW&Æ÷Vâ†&6R²"ö’÷–ær"ÂF–ÖV÷WCÓã‚’2&W7 ¢FFÒ§6öâæÆöG2‡&W7ç&VBƒC“b’æFV6öFR‚'WFbÓ‚"Â'&WÆ6R"’¢–b—6–ç7Fæ6R†FFÂF–7B’æBFFævWB‚&"’ÓÒ&öÆ÷2×GfÖFR# ¢&WGW&âG'VP¢W†6WBW†6WF–öã ¢70¢G'“ ¢v—F‚W&ÆÆ–"ç&WVW7BçW&Æ÷Vâ†&6R²"ò"ÂF–ÖV÷WCÓã‚’2&W7 ¢vRÒ&W7ç&VBƒƒ“"’æFV6öFR‚'WFbÓ‚"Â'&WÆ6R"¢&WGW&â#ÇF—FÆSäöÆòw2EdÖFSÂ÷F—FÆSâ"–âvP¢W†6WBW†6WF–öã ¢&WGW&âfÇ6P ¦FVbÖ–â‚“ ¢vÆö&Âô5D•dUõõ%@¢÷'BÒõ%@¢–b"Ò×÷'B"–â7—2æ&wc ¢G'“ ¢÷'BÒ–çB‡7—2æ&we·7—2æ&wbæ–æFW‚‚"Ò×÷'B"’²Ò¢W†6WBW†6WF–öã ¢70¢W&ÂÒb&‡GG¢òöÆö6Æ†÷7C§·÷'GÒ ¢26–ævÆRÖ–ç7Fæ6R6†V6²6öÖW2&Vf÷&RÆVæ6†W"Ö–w&F–öâ÷&VÆVæ6‚Æöv–2à¢26V6öæB6÷’6†÷VÆBæWfW"&WÆ6R÷&W7F'Bç—F†–ærVæFW&æVF‚F†P¢2Ç&VG’×'Vææ–ær²—B§W7B'&–æw2F†RW†—7F–ærT’&6²FòF†RW6W"à¢–böW†—7F–æu÷GfÖFR‡÷'B“ ¢G'“ ¢vV&'&÷w6W"æ÷Vâ‡W&Â¢W†6WBW†6WF–öã ¢70¢&WGW&à¢6frÒÆöEö6öæf–r‚¢2#C#“¢FVÒ66†VGVÆW2W6VBFò–æ6ÇVFRVç&VÆFVBf—‡GW&W267&VBg&öÒF†P¢2f÷DÖö"FVÒ–ÆöBâF†÷6R&BÆ—7G2vW&R66†VBf÷"vVV²Â6òG&÷F†P¢2öÆBÖf÷&ÖBf–ÆW2öæ6S²F†RæWrc"66†W2&Rw&—GFVâ'’F†Rf—†VB6öFRà¢G'“ ¢&ö÷BÒFFö66†UöF—"‚¢–b÷2çF‚æ—6F—"‡&ö÷B“ ¢f÷"æÖR–â÷2æÆ—7FF—"‡&ö÷B“ ¢–bæÖRç7F'G7v—F‚‚'FVÒÖf—‡GW&W2Ò"’æBæ÷BæÖRç7F'G7v—F‚‚'FVÒÖf—‡GW&W2×c"Ò"“ ¢G'“ ¢÷2ç&VÖ÷fR†÷2çF‚æ¦ö–â‡&ö÷BÂæÖR’¢W†6WBõ4W'&÷# ¢70¢W†6WBõ4W'&÷# ¢70¢†–FUö6öç6öÆRÒG'VP¢†–FFVåö6†–ÆBÒ÷2æVçf—&öâævWB‚%EdÔDUô„”DDTåô4„”ÄB"’ÓÒ# ¢–b7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"’æBæ÷B†–FFVåö6†–ÆC ¢2Ö–w&FRâöÆBÆVæ6†W"&Vf÷&R—B6â&VÆVæ6‚—G6VÆb÷"7F'BF†P¢2æ÷&ÖÂ6W'fW"âF†—2Ç6ò6÷fW'26öÆB&ö÷G7G&v†W&Ræò&Wf–÷W0¢2Æö6ÂGfÖFRç’W†—7FVC¢26ööâ2F†RÆVæ6†W"'Vç2F†—27W'&Vç@¢267&—BÂ—B6â&WÆ6R—G6VÆböæ6RæB&W7F'B6ÆVæÇ’à¢–bæ÷BöÆVæ6†W%ö—5ö7W'&VçB‚’æB÷7F'EöÆVæ6†W%öÖ–w&F–öâ‚“ ¢&WGW&à¢2Væ¶æ÷vâ÷&VæÖVBÆVv7’ÆVæ6†W'26ææ÷B&R6fVÇ’f÷&6R×&WÆ6VBà¢2¶VWF†RöÆB†–FFVâÖ6†–ÆBfÆÆ&6²f÷"F†÷6R66W2âF†RfW&–f–VBuT¢2ÆVæ6†W"—2Ç&VG’v–æF÷vÆW72æBæVVG2æòW‡G&6VÆb×&VÆVæ6‚à¢–bæ÷BöÆVæ6†W%ö—5ö7W'&VçB‚’æB†–FUö6öç6öÆS ¢–böÆVæ6…÷v—F†÷WEö6öç6öÆR‚“ ¢ö6Æ÷6UöÆVæ6†W%ö6öç6öÆR‚¢&WGW&à¢27F'BÆö6Æ†÷7BöæÇ’â÷F–öæÂv’Ôf’66W72—2FVÆ–&W&FVÇ’7F'FVBÆFW ¢2öâ—G2÷vâ÷'BÂgFW"F†RFW6·F÷—2Ç&VG’†VÇF‡’à¢6W'fW"ÒæöæP¢&–æEöW'&÷'2ÒµÐ¢f÷"6æF–FFR–â&ævR‡÷'BÂ÷'B²“ ¢G'“ ¢6W'fW"ÒF‡&VF–æt…EE6W'fW"‚‚##rããã"Â6æF–FFR’Â†æFÆW"¢÷'BÒ6æF–FFP¢'&V°¢W†6WBõ4W'&÷"2&–æEöW'&÷# ¢&–æEöW'&÷'2æVæB†b'¶6æF–FFWÓ¢¶&–æEöW'&÷'Ò"¢–b6W'fW"—2æöæS ¢&—6Rõ4W'&÷"‚$6÷VÆBæ÷B÷VâÆö6ÂEdÖFR÷'B‚"²#²"æ¦ö–â†&–æEöW'&÷'2’²"’"¢ô5D•dUõõ%BÒ÷'@¢W&ÂÒb&‡GG¢òöÆö6Æ†÷7C§·÷'GÒ ¢õ5DõôUdTåBæ6ÆV"‚¢öÖ&µöö7F—f—G’‚¢–bæ÷B†–FUö6öç6öÆRæB7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"“ ¢2F†RuT’×7V'7—7FVÒõEdÒÆVæ6†W"–çFVçF–öæÆÇ’7F'G2v—F†÷WB¢26öç6öÆRâ&WG&òÖöFR÷G2&6²–âæB7&VFW2öæR†W&S²ÖçVÂ'Vç0¢2g&öÒâW†—7F–ærFW&Ö–æÂ6–×Ç’¶VWW6–ærF†V—"7W'&VçB6öç6öÆRà¢÷6WEö6öç6öÆU÷f—6–&ÆR…G'VR¢W6Uö6öÆ÷"ÒöVæ&ÆUöç6’‚’–bæ÷B†–FUö6öç6öÆRVÇ6RfÇ6P¢–bæ÷B†–FUö6öç6öÆS ¢G'“ ¢&–çB…ö6öÆ÷&VEö&ææW"‡W6Uö6öÆ÷"’¢W†6WBW†6WF–öã ¢G'“ ¢&–çB„$ääU"¢W†6WBW†6WF–öã ¢70¢&–çB‚""²#Ò"¢Sb¢&–çB†b"öÆòw2EdÖFR—2%Tää”är‡gµdU%4”ôçÒ’"¢&–çB†b"vF6‚†W&RÓâ·W&ÇÒ"¢&–çB‚"FòT•BÓâ6Æ÷6RF†—2v–æF÷r†÷"&W727G&Â´2’"¢&–çB‚""²#Ò"¢Sb¢26W'fRF†R–âF†R&6¶w&÷VæB6òF†R6W'fW"—2&VG’&Vf÷&RvR÷Vâà¢F‡&VF–æråF‡&VB‡F&vWC×6W'fW"ç6W'fUöf÷&WfW"ÂFVÖöãÕG'VR’ç7F'B‚¢–b6frævWB‚&ÆÆ÷uöÆâ"“ ¢F‡&VF–æråF‡&VB‡F&vWCÕ÷7F'EöÆå÷6W'fW"ÂFVÖöãÕG'VR’ç7F'B‚¢–b6frævWB‚&FWeöÖöFR"’æB6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"“ ¢F‡&VF–æråF‡&VB‡F&vWCÕ÷7F'E÷&VÖ÷FU÷6W'fW"ÂFVÖöãÕG'VR’ç7F'B‚¢F‡&VF–æråF‡&VB‡F&vWCÕöWFõ÷6‡WFF÷vå÷vF6†FörÂFVÖöãÕG'VR’ç7F'B‚¢–b†–FUö6öç6öÆS ¢2†–FFVâÖöFR6ææ÷Bv—Bf÷"6öç6öÆR–çWC¢ÆVæ6‚F†RT’–ÖÖVF–FVÇ’à¢G'“ ¢vV&'&÷w6W"æ÷Vâ‡W&Â¢W†6WBW†6WF–öã ¢70¢25$TDUôäõõt”äDõr6†–ÆG&Vâ&RÇ&VG’†–FFVââF†—2—2öæÇ’¢2fÆÆ&6²f÷"ÆFf÷&×2öÆVæ6†W'2v†W&RF†R&VÆVæ6‚v2Væf–Æ&ÆRà¢–bæ÷B†–FFVåö6†–ÆC ¢÷6WEö6öç6öÆU÷f—6–&ÆR„fÇ6R¢VÇ6S ¢2æ÷&ÖÂÖöFR¶VW2F†RfÖ–Æ–"æ6¶R&ö×BæBv—G2f÷"VçFW"à¢–×÷'B&æFöÒ2÷&æ@¢&ö×BÒ÷&æBæ6†ö–6R„TåDU%õ$ôÕE2¢Æ–æRÒ""²…ôtôÄB²&ö×B²õ$U4UB–bW6Uö6öÆ÷"VÇ6R&ö×B¢G'“ ¢–çWB‚%Æâ"²Æ–æR²%Æâ"¢vV&'&÷w6W"æ÷Vâ‡W&Â¢W†6WBW†6WF–öã ¢2æò6öç6öÆR–çWBf–Æ&ÆR†VFvR66R’Ò§W7B÷VâF†R'&÷w6W"à¢G'“ ¢vV&'&÷w6W"æ÷Vâ‡W&Â¢W†6WBW†6WF–öã ¢70¢2¶VW'Vææ–ærVçF–Â7G&Â´2ÂF†R6öç6öÆR6Æ÷6W2Â÷"F†RvV"T’6·2W2Fò7F÷à¢G'“ ¢õ5DõôUdTåBçv—B‚¢W†6WB¶W–&ö&D–çFW''WC ¢&–çB‚%Æâ7F÷–æröÆòw2EdÖFRâ'–R"¢f–æÆÇ“ ¢6W'fW"ç6‡WFF÷vâ‚¢6W'fW"ç6W'fW%ö6Æ÷6R‚¢÷7F÷öÆå÷6W'fW"‚¢÷7F÷÷&VÖ÷FU÷6W'fW"‚ ¦FVb÷E÷6ÆVW‡6V2“ ¢–×÷'BF–ÖR2÷@¢÷Bç6ÆVW‡6V2 ¦FVb'Vå÷6VÆe÷FW7G2‚“ ¢""$f7BÂöffÆ–æR6†V6·2f÷"F†R6ÖÆÂ–V6W2Ö÷7BÆ–¶VÇ’Fò'&V²WFFW2â"" ¢6†V6·2ÒµÐ¢FVb6†V6²†æÖRÂ6öæF—F–öâ“ ¢–bæ÷B6öæF—F–öã ¢&—6R76W'F–öäW'&÷"†æÖR¢6†V6·2æVæB†æÖR¢6†V6²‚'fW'6–öâ÷&FW&–ær"Â÷'6U÷fW"‚#ãssræ#3ƒ‚"’â÷'6U÷fW"‚#ãssræ#3ƒr"’¢6†V6²‚'fW'6–öâWVÆ—G’"Â÷'6U÷fW"‚'cãssræ#3ƒ‚"’ÓÒ÷'6U÷fW"‚#ãssræ#3ƒ‚"’¢66†Uö'W7FVBÒö66†Uö'W7FVE÷W&Â€¢&‡GG3¢ò÷&ræv—F‡V'W6W&6öçFVçBæ6öÒöW†×ÆRööÖ–â÷fW'6–öâçG‡C÷6÷W&6SÖÖçVÂ"À¢##2"¢6†V6²‚'WFFRU$Ç2&W6W'fRVW&–W2æB'—72&rÖ6öçFVçB66†W2"À¢'6÷W&6SÖÖçVÂ"–â66†Uö'W7FVBæB%÷GfÖFSÓ#2"–â66†Uö'W7FVB¢6†V6²‚%F–Ç66ÆRFWFV7F–öâ66WG2öæÇ’4täBFWf–6RFG&W76W2"À¢÷'6U÷F–Ç66ÆUö—cB‚#ãã"ã5Æâ"’ÓÒ#ãã"ã2"æ@¢÷'6U÷F–Ç66ÆUö—cB‚#“"ãc‚ãã…Æâ"’ÓÒ""¢&VÆ•ö6frÒ²&Æåö66W75÷Fö¶Vâ#¢&öffÆ–æR×&VÆ’×FW7B'Ð¢&VÆ•÷6V7&WE÷W&ÂÒ&‡GG3¢ò÷&÷f–FW"æW†×ÆRöÆ—fR÷&—fFR×W6W"÷&—fFR×72ósrçG2 ¢&VÆ•÷FW7E÷Fö¶VâÒ÷&VÆ•÷Fö¶Vâ‡&VÆ•÷6V7&WE÷W&ÂÂÆ–fWF–ÖSÓcÂ6fs×&VÆ•ö6fr¢6†V6²‚'&—fFR&VÆ’Fö¶Vç2&R÷VRæBF×W"&W6—7FçB"À¢&VÆ•÷6V7&WE÷W&Âæ÷B–â&VÆ•÷FW7E÷Fö¶Vâæ@¢'&—fFR×W6W""æ÷B–â&VÆ•÷FW7E÷Fö¶Vâæ@¢÷&VÆ•÷F&vWB‡&VÆ•÷FW7E÷Fö¶VâÂ6fs×&VÆ•ö6fr’ÓÒ&VÆ•÷6V7&WE÷W&Âæ@¢÷&VÆ•÷F&vWB‡&VÆ•÷FW7E÷Fö¶Vâ²'‚"Â6fs×&VÆ•ö6fr’ÓÒ""¢W‡—&VE÷Fö¶VâÒ÷&VÆ•÷Fö¶Vâ‡&VÆ•÷6V7&WE÷W&ÂÂÆ–fWF–ÖSÒÓÂ6fs×&VÆ•ö6fr¢6†V6²‚&W‡—&VB&—fFR&VÆ’Fö¶Vç2f–Â6Æ÷6VB"À¢÷&VÆ•÷F&vWB†W‡—&VE÷Fö¶VâÂ6fs×&VÆ•ö6fr’ÓÒ""¢6†V6²‚'&—fFR&VÆ’&VÖ–ç2†–FFVâæBFWfVÆ÷W"vFVB"À¢v–CÒ'5÷&—fFW&VÆ’"r–âtRæ@¢v–CÒ&FWe6WGF–æw2"6Æ73Ò'6WGF–æw6w&÷W†–FR"r–âtRæ@¢v6fu²'&—fFU÷&VÖ÷FU÷&VÆ’%ÒÒ&ööÂ†6frævWB‚'&—fFU÷&VÖ÷FU÷&VÆ’"’’æB6fu²&FWeöÖöFR%Òp¢–â÷Vâ…õöf–ÆUõòÂ'""ÂVæ6öF–æsÒ'WFbÓ‚"’ç&VB‚’¢6†V6²‚'7÷'G2WfVçB66†R¶W’æ÷&ÖÆ—¦W2FV×2"À¢÷7÷'G5öWfVçEö¶W’‚$ÆVVG2Væ—FVB"Â$ÖâWFB"Â###bÓ‚Ó%C#£3£¢"’ÓÐ¢÷7÷'G5öWfVçEö¶W’‚"ÆVVG2Væ—FVB"Â$ÔâUDB"Â###bÓ‚Ó%C#£3£S•¢"’¢66†VGVÆU÷FW7BÒ°¢²&†öÖR#¢$†V'G2"Â&v’#¢$&Væf–6"Â'7F'B#¢###bÓ‚Ó5Cƒ£CS£¢"À¢&'•ö6÷VçG'’#¢·×ÒÀ¢²&†öÖR#¢$†V'G2"Â&v’#¢$–çfW&æW72"Â'7F'B#¢###bÓ‚ÓeC3££¢"À¢&'•ö6÷VçG'’#¢·×ÕÐ¢ö÷fW&Æ•öf—‡GW&U÷&÷w2‡66†VGVÆU÷FW7BÂ·°¢&†öÖR#¢$†V'BöbÖ–FÆ÷F†–â"Â&v’#¢$&Væf–6"À¢'7F'B#¢###bÓ‚Ó5Cƒ£CS£¢"À¢&'•ö6÷VçG'’#¢²%B#¢²%7÷'BEbR%××ÕÒ¢6†V6²‚%EbÆ—7F–æw2Vç&–6‚v—F†÷WB&VGV6–ærFVÒ66†VGVÆR"À¢ÆVâ‡66†VGVÆU÷FW7B’ÓÒ"æ@¢66†VGVÆU÷FW7E³Õ²&'•ö6÷VçG'’%ÒÓÒ²%B#¢²%7÷'BEbR%×Ò¢ÇGe÷FW7BÒ÷'6UöÇGeöF–Ç’‚rrsÇG"6Æ73Ò&ÖF6‡&÷r#ãÇFCãÆ‡&VcÒ"öÖF6‚÷‚ò#ä†V'G2g2&Væf–6ÂöãÂ÷FCãÇFB–CÒ&6†ææVÇ2#ãÆFFÖ6÷VçG'“Ò%÷'GVvÂ#å7÷'BEcSÂöãÆFFÖ6÷VçG'“Ò%Væ—FVB¶–ævFöÒ#ä†V'G2EcÂöãÂ÷FCãÂ÷G#ârrrÂ###bÓ‚Ó2"¢6†V6²‚$ÅEb'6W"W‡G&7G26†ææVÇ2v—F†÷WB7&VF–ærf—‡GW&W2"À¢ÆVâ†ÇGe÷FW7B’ÓÒæBÇGe÷FW7E³Õ²&†öÖR%ÒÓÒ$†V'G2"æ@¢ÇGe÷FW7E³Õ²&'•ö6÷VçG'’%ÒÓÒ²%B#¢²%7÷'BEcR%ÒÂ%T²#¢²$†V'G2Eb%×Ò¢ÇGeöÆVv7•ö6÷VçG'•÷FW7BÒ÷'6UöÇGeöF–Ç’‚rrsÇG"6Æ73Ò&ÖF6‡&÷r#ãÇFCãÆ‡&VcÒ"öÖF6‚÷‚ò#äæ÷GF–æv†Òf÷&W7Bg2ÆVVG2Væ—FVCÂöãÂ÷FCãÇFB–CÒ&6†ææVÇ2#ãÆ‡&VcÒ"ö6†ææVÇ2÷f–Æ’Öæ÷'v’ò#åf–Æ’æ÷'v“ÂöãÆ‡&VcÒ"ö6†ææVÇ2÷f–Æ’Öf–æÆæBò#åf–Æ’f–æÆæCÂöãÂ÷FCãÂ÷G#ârrrÂ###bÓ‚Ó#""¢6†V6²‚&ÆVv7’ÅEbÆ–æ·2–æfW"6÷VçG&–W2g&öÒ'&öF67FW"æÖW2"À¢ÇGeöÆVv7•ö6÷VçG'•÷FW7E³Õ²&'•ö6÷VçG'’%ÒÓÒ°¢$äò#¢²%f–Æ’æ÷'v’%ÒÂ$d’#¢²%f–Æ’f–æÆæB%×Ò¢ÇGeö7W'&VçE÷FW7BÒ÷'6UöÇGeöF–Ç’‚rrsÇ6V7F–öããÆ‡&VcÒ"öÖF6‚öæ÷GF–æv†ÒÖf÷&W7B×g2ÖÆVVG2ò#äæ÷GF–æv†Òf÷&W7Bg2ÆVVG2Væ—FVCÂöãÆ‡&VcÒ"ö6†ææVÇ2öF¦â×7–âò#äD¤â7–ãÂöãÆ‡&VcÒ"ö6†ææVÇ2÷f–Æ’ÖFVæÖ&²ò#åf–Æ’FVæÖ&³ÂöãÆ‡&VcÒ"ö6†ææVÇ2÷f–Æ’×7vVFVâò#åf–Æ’7vVFVãÂöãÆ‡&VcÒ"ö6†ææVÇ2÷f–Æ’Öæ÷'v’ò#åf–Æ’æ÷'v“ÂöãÂ÷6V7F–öããÇ6V7F–öããÆ‡&VcÒ"öÖF6‚ö÷F†W"ò#ä÷F†W"d2g2VÇ6Rd3ÂöãÂ÷6V7F–öãârrrÂ###bÓ‚Ó#""¢6†V6²‚&7W'&VçBÅEb6&G2GF6‚f–Æ’Fòf÷&W7BÆVVG2"À¢ÆVâ†ÇGeö7W'&VçE÷FW7B’ãÒæ@¢ÇGeö7W'&VçE÷FW7E³Õ²&†öÖR%ÒÓÒ$æ÷GF–æv†Òf÷&W7B"æ@¢²%f–Æ’FVæÖ&²"Â%f–Æ’7vVFVâ"Â%f–Æ’æ÷'v’'Òæ—77V'6WB€¢¶æÖRf÷"æÖW2–âÇGeö7W'&VçE÷FW7E³Õ²&'•ö6÷VçG'’%ÒçfÇVW2‚¢f÷"æÖR–âæÖW7Ò’¢6†V6²‚$f÷DÖö"æ÷GFÒf÷&W7B&'&Wf–F–öâÖF6†W2ÅEbæ÷GF–æv†Òf÷&W7B"À¢÷FVÕöæÖW5öWV—fÆVçB‚$æ÷GFÒf÷&W7B"Â$æ÷GF–æv†Òf÷&W7B"’æ@¢÷FVÕöæÖW5öWV—fÆVçB‚$æ÷GF–æv†Òf÷&W7B"Â$æ÷GFÒf÷&W7B"’¢6†V6²‚'6Væ–÷"ff÷&—FW2Fòæ÷BÖF6‚vöÖVâ÷"&W6W'fR7VG2"À¢æ÷B÷FVÕöæÖW5öWV—fÆVçB‚$'&æâ"Â$'&æâ…r’"’æ@¢æ÷B÷FVÕöæÖW5öWV—fÆVçB‚$'&æâ"Â$'&æâ""’¢6†V6²‚&F–Ç’ff÷&—FW2W6RW†7BFVÒ–G2v†Vâ7WÆ–VB"À¢æ÷BöF–Ç•öÖF6…ö–çföÇfW5÷FVÒ€¢²&†öÖR#¢²&–B#¢#"Â&æÖR#¢$'&æâ…r’'ÒÀ¢&v’#¢²&–B#¢#2Â&æÖR#¢$W7G&–v–Vâr'×ÒÂ$'&æâ"Â##"’æ@¢öF–Ç•öÖF6…ö–çföÇfW5÷FVÒ€¢²&†öÖR#¢²&–B#¢#Â&æÖR#¢$'&æâ'ÒÀ¢&v’#¢²&–B#¢#BÂ&æÖR#¢%ô²'×ÒÂ$'&æâ"Â##"’¢&'&Wf–FVEöf—‡GW&RÒ·²&†öÖR#¢$æ÷GFÒf÷&W7B"Â&v’#¢$ÆVVG2Væ—FVB"À¢'7F'B#¢###bÓ‚Ó#%CC££¢"Â&'•ö6÷VçG'’#¢·×ÕÐ¢ö÷fW&Æ•öf—‡GW&U÷&÷w2†&'&Wf–FVEöf—‡GW&RÂ·°¢&†öÖR#¢$æ÷GF–æv†Òf÷&W7B"Â&v’#¢$ÆVVG2Væ—FVB"À¢'7F'B#¢###bÓ‚Ó#""Â&'•ö6÷VçG'’#¢²$äò#¢²%f–Æ’æ÷'v’%××ÕÒ¢6†V6²‚$f÷&W7B&'&Wf–F–öâ&WF–ç2æ÷'vVv–âÅEbÆ—7F–ær÷fW&Æ’"À¢&'&Wf–FVEöf—‡GW&U³Õ²&'•ö6÷VçG'’%ÒÓÒ²$äò#¢²%f–Æ’æ÷'v’%×Ò¢ÇGeöÖF6…öFWF–Å÷FW7BÒ÷'6UöÇGeöÖF6…öÆ—7F–æw2‚rrp¢Æƒ#ä–çFW&æF–öæÂEcÂöƒ#ãÇF&ÆSà¢ÇG#ãÇFCäæ÷'v“Â÷FCãÇFCãÆ‡&VcÒ"ö6†ææVÇ2÷f–Æ’Öæ÷'v’ò#åf–Æ’æ÷'v“Âöà¢Æ‡&VcÒ"ö6†ææVÇ2÷Gc"×Æ’Öæ÷'v’ò#åEb"Æ“Âöà¢Æ‡&VcÒ"ö6†ææVÇ2÷b×7÷'BÓÖæ÷'v’ò#åb7÷'Bæ÷'v“ÂöãÂ÷FCãÂ÷G#à¢ÇG#ãÇFCå7vVFVãÂ÷FCãÇFCãÆ‡&VcÒ"ö6†ææVÇ2÷f–Æ’×7vVFVâò#åf–Æ’7vVFVãÂöãÂ÷FCãÂ÷G#à¢Â÷F&ÆSãÆƒ#äÖF6‚FWF–Ç3Âöƒ#ârrr¢6†V6²‚$ÅEbÖF6‚vRW‡G&7G26ö×ÆWFRæ÷'vVv–â6†ææVÂ&÷r"À¢ÇGeöÖF6…öFWF–Å÷FW7BÓÒ°¢$äò#¢²%f–Æ’æ÷'v’"Â%Eb"Æ’"Â%b7÷'Bæ÷'v’%ÒÀ¢%4R#¢²%f–Æ’7vVFVâ%×Ò¢6†V6²‚&W‡æFVBÅEb6÷VçG&–W2&V6övæ—¦R&÷6æ–æBæWr¦VÆæB"À¢ö65ög&öÕöæÖR‚$&÷6æ–æB†W'¦Vv÷f–æ"’ÓÒ&&"æ@¢ö65ög&öÕöæÖR‚$æWr¦VÆæB"’ÓÒ&ç¢"¢æVvF—fU÷W&ÂÒ&‡GG3¢ò÷wwræÆ—fW6ö66W'Gbæ6öÒöÖF6‚ööffÆ–æR×6VÆb×FW7Bò ¢ôÅEeôÔD4…ôd”ÅU$U5¶æVvF—fU÷W&ÅÒÒ²'G2#¢F–ÖRçF–ÖR‚’Â&W'&÷"#¢&66†VBf–ÇW&R'Ð¢æVvF—fUö&Æö6¶VBÒfÇ6P¢G'“ ¢fWF6…öÇGeöÖF6…öÆ—7F–æw2†æVvF—fU÷W&Â¢W†6WB'VçF–ÖTW'&÷"2W†3 ¢æVvF—fUö&Æö6¶VBÒ&66†VBf–ÇW&R"–â7G"†W†2¢f–æÆÇ“ ¢ôÅEeôÔD4…ôd”ÅU$U2ç÷†æVvF—fU÷W&ÂÂæöæR¢ôÅEeôÔD4…ôÄô4µ2ç÷†æVvF—fU÷W&ÂÂæöæR¢6†V6²‚&f–ÆVBÅEbÖF6‚&WVW7B—2æVvF—fVÇ’66†VB"ÂæVvF—fUö&Æö6¶VB¢6†V6²‚$ÅEbF–Ç’&÷r&WF–ç2FWF–ÂU$Â"À¢ÇGeö7W'&VçE÷FW7E³ÒævWB‚&ÖF6…÷W&Â"’ÓÐ¢&‡GG3¢ò÷wwræÆ—fW6ö66W'Gbæ6öÒöÖF6‚öæ÷GF–æv†ÒÖf÷&W7B×g2ÖÆVVG2ò"¢e÷7÷'EöW†7BÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%b7÷'Bæ÷'v’%×ÒÀ¢·²&æÖR#¢$äó¢b7÷'B„B"Â'7G&VÕö–B#¢#’Â&6FVv÷'•ö–B#¢&æò'ÕÒÀ¢²&æò#¢$äòÂäõ%t’'ÒÂãc"¢6†V6²‚&6÷VçG'’×7Vff—†VBb7÷'BfVVB—2W†7Bæ÷'vVv–â&÷f–FW""À¢ÆVâ‡e÷7÷'EöW†7B’ÓÒæBe÷7÷'EöW†7E³Õ²'66÷&R%ÒÓÒãæ@¢e÷7÷'EöW†7E³Õ²'&÷f–FW%öW†7B%Ò—2G'VR¢e÷7÷'EöÇGeö6÷VçG'•öæÖRÒÖF6…ö6†ææVÇ2€¢²$ÅEb#¢²%b7÷'Bæ÷'v’%×ÒÀ¢·²&æÖR#¢$äòb5õ%B„Ud2$r„B"Â'7G&VÕö–B#¢#“À¢&6FVv÷'•ö–B#¢&Ö—62'ÕÒÂ²&Ö—62#¢%d•tôÄB'ÒÂãc"¢6†V6²‚$ÅEb6÷VçG'’Ö–âÖæÖRb7÷'B—26V7W&RW†7B&÷f–FW""À¢ÆVâ‡e÷7÷'EöÇGeö6÷VçG'•öæÖR’ÓÒæ@¢e÷7÷'EöÇGeö6÷VçG'•öæÖU³Õ²'66÷&R%ÒÓÒãæ@¢e÷7÷'EöÇGeö6÷VçG'•öæÖU³Õ²'&÷f–FW%öW†7B%Ò—2G'VRæ@¢e÷7÷'EöÇGeö6÷VçG'•öæÖU³Õ²&6÷VçG'’%ÒÓÒ$äò"¢e÷7÷'Eö&&Uö62ÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%b7÷'Bæ÷'v’%×ÒÀ¢·²&æÖR#¢$äòb5õ%B$r"Â'7G&VÕö–B#¢3Â&6FVv÷'•ö–B#¢&æò'ÕÒÀ¢²&æò#¢$äòÂäõ%t’'ÒÂãc"¢6†V6²‚&&&Räò&Vf—‚7F–ÆÂ––VÆG2W†7Bb7÷'B&÷f–FW""À¢ÆVâ‡e÷7÷'Eö&&Uö62’ÓÒæBe÷7÷'Eö&&Uö65³Õ²'66÷&R%ÒÓÒãæ@¢e÷7÷'Eö&&Uö65³Õ²'&÷f–FW%öW†7B%Ò—2G'VR¢f÷"&Vf—…÷f&–çB–â‚$äòb5õ%B$r"Â$äõ"b5õ%B„B"À¢$äõ%t’b5õ%B"Â$äõ$tRb5õ%B"À¢%b5õ%Bäõ"$r"Â%b5õ%Bäõ%t’$r"“ ¢f&–çE÷&÷w2ÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%b7÷'Bæ÷'v’%×ÒÀ¢·²&æÖR#¢&Vf—…÷f&–çBÂ'7G&VÕö–B#¢3Â&6FVv÷'•ö–B#¢&Ö—62'ÕÒÀ¢²&Ö—62#¢%d•tôÄB'ÒÂãc"¢6†V6²†b$æ÷'vVv–âb7÷'B&Vf—‚f&–çBÖF6†W3¢·&Vf—…÷f&–çGÒ"À¢ÆVâ‡f&–çE÷&÷w2’ÓÒæBf&–çE÷&÷w5³Õ²'66÷&R%ÒÓÒãæ@¢f&–çE÷&÷w5³Õ²'&÷f–FW%öW†7B%Ò—2G'VR¢f–Æ•ö&&U÷e÷7÷'BÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%f–Æ’æ÷'v’%×ÒÀ¢·²&æÖR#¢$äòb5õ%B$r"Â'7G&VÕö–B#¢3"À¢&6FVv÷'•ö–B#¢&Ö—62'ÕÒÂ²&Ö—62#¢%d•tôÄB'ÒÂãc"¢6†V6²‚%f–Æ’æ÷'v’W‡æG2g&öÒ&&R×&Vf—‚b7÷'BæÖR"À¢ÆVâ‡f–Æ•ö&&U÷e÷7÷'B’ÓÒæ@¢f–Æ•ö&&U÷e÷7÷'E³Õ²'66÷&R%ÒÓÒã“B¢f–Æ•ö6öFV5÷f&–çG2ÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%f–Æ’„äò’%×ÒÀ¢·²&æÖR#¢$äòb5õ%B„Ud2$r„B"Â'7G&VÕö–B#¢32À¢&6FVv÷'•ö–B#¢&Ö—62'ÒÀ¢²&æÖR#¢$äõ#¢b5õ%B$TÔ”U"ÄTuTR"‚ã#cR"À¢'7G&VÕö–B#¢3BÂ&6FVv÷'•ö–B#¢&Ö—62'ÕÒÀ¢²&Ö—62#¢%d•tôÄB'ÒÂãc"¢6†V6²‚%f–Æ’äòW‡æG26öFV2ÖÆ&VÆÆVBæ÷'vVv–âb7÷'BfVVG2"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–âf–Æ•ö6öFV5÷f&–çG7ÒÓÒ³32Â3GÒ¢6†V6²‚&W†7Bb7÷'B&÷f–FW"6÷'G2†VBöb÷76–&ÆRWfVçB6†ææVÇ2"À¢&6öç7B7W&SÖ6ƒÓæ6‚bf6‚ç&÷f–FW%öW†7CÓÓ×G'VSó3¢"–âtRæ@¢&6†ææVÄÆö6ÆU&–÷&—G’†"’Ö6†ææVÄÆö6ÆU&–÷&—G’†—ÇÇ7W&R†"’×7W&R†’"–âtR¢6†V6²‚&6†ææVÂ&W7VÇG2&W6W'fRäòD²ÂäòÂD²Â5tRÂDTâÂd”âF–W'2"À¢&–b†—4æ÷'vVv–âbf—3F²—&WGW&âs"–âtRæ@¢&–b†—4æ÷'vVv–â—&WGW&âc"–âtRæ@¢&–b†—3F²—&WGW&âS"–âtRæ@¢&–b†—57vVF—6‚—&WGW&âC"–âtRæ@¢&–b†—4Fæ—6‚—&WGW&â3“"–âtRæ@¢&–b†—4f–ææ—6‚—&WGW&â3ƒ"–âtRæ@¢"†æ÷Ææ÷'Ææ÷'v—Ææ÷&vWÆæ÷'vVv–â’"–âtR¢6FÆöu÷&÷w2Ò°¢‚%5tS¢b7÷'B„B"Â%4RÂ5õ%E2"’À¢‚$äó¢b7÷'B"„B"Â$äòÂ5õ%E2"’À¢‚$äõ"e5õ%B$r"Â%d•tôÄB"’À¢‚%T³¢&VÖ–W"7÷'G2"Â%T²Â5õ%E2"’À¢Ð¢6FÆöuöÖF6†W2Ò6÷'FVB€¢‡&÷rf÷"&÷r–â6FÆöu÷&÷w0¢–bö6†ææVÅö6FÆöu÷6V&6…÷&æ²‡&÷u³ÒÂ&÷u³ÒÂ%b7÷'B"’—2æ÷BæöæR’À¢¶W“ÖÆÖ&F&÷s¢ö6†ææVÅö6FÆöu÷6V&6…÷&æ²‡&÷u³ÒÂ&÷u³ÒÂ%b7÷'B"’¢6†V6²‚'Æ–Æ—7Bb7÷'B6V&6‚f–æG26ö×7BæÖW2æB&æ·2æ÷'v’f—'7B"À¢·&÷u³Òf÷"&÷r–â6FÆöuöÖF6†W5ÒÓÐ¢²$äó¢b7÷'B"„B"Â$äõ"e5õ%B$r"Â%5tS¢b7÷'B„B%Ò¢6†V6²‚&f—‡GW&Rf–Æ&–Æ—G’ÆöG2&–÷&—G’&W7VÇG2&öw&W76—fVÇ’"À¢&&F6†W2çW6‚†f—‡GW&W2ç6Æ–6RƒÃ2’’"–âtRæ@¢&f÷"†ÆWB“Ó3¶“Æf—‡GW&W2æÆVæwFƒ¶’³Ó"’"–âtR¢6†V6²‚&÷Væ–ærv—F–ærf—‡GW&RG&–vvW'2F&vWFVB6†ææVÂÆöö·W"À¢'æVÂçFW‡D6öçFVçBæ–æ6ÇVFW2‡G"‚t6†V6¶–ær–÷W"6†ææVÇ2âââr’’"–âtRæ@¢&&öG“¤¥4ôâç7G&–æv–g’‡¶f—‡GW&S¦f—‡GW&WÒ’"–âtR¢6†V6²‚&ÖF6†VB6†ææVÂF—FÆW2Æ’v—F†÷WB6öÆÆ6–ærf—‡GW&W2"À¢&f—‡GW&V6†ææVÇF—FÆU¶FF×6–EÒ"–âtRæ@¢'Æ”'&÷w6W"†f—‡GW&T6†ææVÅF—FÆRævWDGG&–'WFR‚vFF×6–Br’"–âtR¢6†V6²‚'6V7W&R6†ææVÂfÖ–Æ–W26†÷rf—fRVÆ—G’f&–çG2&Vf÷&RW‡ç6–öâ"À¢&“ãÓSòr6V7W&VÖF6†W‡G&†–FRr"–âtRæ@¢&—FV×2æÆVæwF‚ÓR"–âtRæ@¢'6V7W&UVÆ—G•&–÷&—G’†"’×6V7W&UVÆ—G•&–÷&—G’†’"–âtRæ@¢'6V7W&VÖF6†W‡æB"–âtR¢6†V6²‚'7÷'G26&G26†÷rÆöævW"6†ææVÂF—FÆW2æBfö–BGWÆ–6FRÆ–W"ÆVæ6†W2"À¢&Ö–âƒCƒ‚Ã“'gr’"–âtRæ@¢'&VfW'&VDW†7E&÷f–FW"†Ò’"–âtRæ@¢%ö'&÷w6W%VæF–æu6–CÓÓ×6–D¶W’"–âtRæ@¢%÷GeVæF–æu6–CÓÓ×6–D¶W’"–âtR¢6†V6²‚&6ö×7Bf—‡GW&R&÷w2&W6W'fRF—FÆW2æB&V¦V7BVç&VÆFVBõEB6Æ÷G2"À¢&w&–B×FV×ÆFRÖ6öÇVÖç3£#'‚Ö–æÖ‚ƒÃg"’"–âtRæ@¢'&VfW'&VDW†7E&÷f–FW"†6‚’"–âtR¢6†V6²‚'6V7W&R6†÷rÖÖ÷&RW‡æG2öâ—G2f—'7B6Æ–6²"À¢'VW'•6VÆV7F÷$ÆÂ‚rç6V7W&VÖF6†W‡G&¶FF×6V7W&RÖw&÷WÒ"–âtR¢6†V6²‚'÷76–&ÆR6†ææVÂ6FVv÷&–W2W6R6†&VBÆö6ÆR÷&FW&–ær"À¢&w&÷WVE÷76–&ÆT6†ææVÇ2†÷F†W"’"–âtRæ@¢&w&÷WVE÷76–&ÆT6†ææVÇ2‡÷76–&ÆUb’"–âtRæ@¢&6FVv÷'•F–W"†%³Ò’Ö6FVv÷'•F–W"†³Ò’"–âtRæ@¢&6FVv÷'•F–W"†³Ò“ÓÓÓö&W7DÖF6‚†%³Ò’Ö&W7DÖF6‚†³Ò’"–âtRæ@¢$ÖF‚æÖ‚‚ââæ%³ÒæÖ†6†ææVÄÆö6ÆU&–÷&—G’’’"æ÷B–âtR¢6†V6²‚&f—‡GW&R6FVv÷&–W2ÆÆ÷r×VÇF—ÆR÷VâæBWFòÖ÷Vâf—fR÷"fWvW""À¢tRæ6÷VçB‚&6öç7B÷VãÖ—FV×2æÆVæwFƒÃÓR"’ãÒ"æ@¢'VW'•6VÆV7F÷$ÆÂ‚s§66÷Râæ&7&÷ræ÷Vâr’"æ÷B–âtRæ@¢"†÷Vãòr÷Vâs¢rr’"–âtRæ@¢"†÷Vãòrs¢r†–FRr’"–âtR¢6†V6²‚&f—‡GW&RæB6ö×WF—F–öâ&VÆWfæ6R&öÖ÷FR÷76–&ÆR6†ææVÇ2"À¢&gVæ7F–öâ6†ææVÄÖF6…&–÷&—G’†6‚’"–âtRæ@¢&f—‡GW&UöÖF6ƒÓÓÒvW†7Br"–âtRæ@¢&ÆVwVUöÖF6ƒÓÓ×G'VRbb÷f–Æ’ö’"–âtRæ@¢&f—‡GW&UöÖF6ƒÓÓÒw'F–Âr"–âtR¢–æFW†VEö6†ææVÇ2Ò°¢²&æÖR#¢b$æö—6R6†ææVÂ¶—Ò"Â'7G&VÕö–B#¢3²’À¢&6FVv÷'•ö–B#¢&Ö—62'Òf÷"’–â&ævRƒ#•Ò²°¢²&æÖR#¢$äó¢b7÷'B„B"Â'7G&VÕö–B#¢CÀ¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$Ä•dRÂvöÇfW2ÒÖæ6†W7FW"6—G’"Â'7G&VÕö–B#¢C"À¢&6FVv÷'•ö–B#¢'b'ÕÐ¢–æFW†VEö6G2Ò²&Ö—62#¢%d•tôÄB"Â&æò#¢$äòÂ5õ%E2"À¢'b#¢%bUdTåE2'Ð¢–æFW†VEöf—‡GW&RÒ²&†öÖR#¢%vöÇfW2"Â&v’#¢$Öæ6†W7FW"6—G’"À¢&'•ö6÷VçG'’#¢²$äò#¢²%b7÷'Bæ÷'v’%××Ð¢–æFW†VE÷6†÷'FÆ—7BÒ÷7÷'G5öf—‡GW&Uö6†ææVÅ÷6†÷'FÆ—7B€¢–æFW†VEöf—‡GW&RÂ–æFW†VEö6†ææVÇ2Â–æFW†VEö6G2¢6†V6²‚'7÷'G2–æFW‚6†÷'FÆ—7G2'&öF67FW"æBf—‡GW&R6†ææVÇ2"À¢ÆVâ†–æFW†VE÷6†÷'FÆ—7B’ÂÆVâ†–æFW†VEö6†ææVÇ2’æ@¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–â–æFW†VE÷6†÷'FÆ—7GÒÓÒ³CÂC'Ò¢6Æ72ô6ö×WF—F–öåFW7Eƒ ¢7FF–6ÖWF†ö@¢FVb7G&VÕ÷W&Â‡7G&VÕö–B“ ¢&WGW&âb'7G&VÓ§·7G&VÕö–GÒ ¢6ö×WF—F–öåö6†ææVÇ2Ò°¢²&æÖR#¢$äó¢b7÷'B&VÖ–W"ÆVwVRd•äò"Â'7G&VÕö–B#¢CÀ¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢%T²&VÖ–W"ÆVwVR7÷'B"Â'7G&VÕö–B#¢CÀ¢&6FVv÷'•ö–B#¢'V²'ÒÀ¢²&æÖR#¢$äó¢b7÷'B"Â'7G&VÕö–B#¢C"À¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b7÷'B&VÖ–W"ÆVwVR"$räò"Â'7G&VÕö–B#¢C2À¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢%T³¢UÂÄTTE2"Â'7G&VÕö–B#¢CBÀ¢&6FVv÷'•ö–B#¢'V²ÖWÂ'ÒÀ¢²&æÖR#¢%T³¢UÂäõED”ät„Ò"Â'7G&VÕö–B#¢CRÀ¢&6FVv÷'•ö–B#¢'V²ÖWÂ'ÕÐ¢6ö×WF—F–öåöf—‡GW&RÒ²&†öÖR#¢$ÆVVG2Væ—FVB"Â&v’#¢$æ÷GF–æv†Òf÷&W7B"À¢&ÆVwVUöæÖR#¢%&VÖ–W"ÆVwVR"Â&'•ö6÷VçG'’#¢·×Ð¢6ö×WF—F–öå÷6†÷'FÆ—7BÒ÷7÷'G5öf—‡GW&Uö6†ææVÅ÷6†÷'FÆ—7B€¢6ö×WF—F–öåöf—‡GW&RÂ6ö×WF—F–öåö6†ææVÇ2À¢²&æò#¢$äòÂ5õ%E2"Â'V²#¢%T²Â5õ%E2"À¢'V²ÖWÂ#¢%T²ÂUÂ$TÔ”U"ÄTuTRb'Ò¢6ö×WF—F–öå÷&÷w2Òf–æEö6ö×WF—F–öåö6†ææVÇ2€¢6ö×WF—F–öåöf—‡GW&RÂ6ö×WF—F–öå÷6†÷'FÆ—7BÀ¢²&æò#¢$äòÂ5õ%E2"Â'V²#¢%T²Â5õ%E2"À¢'V²ÖWÂ#¢%T²ÂUÂ$TÔ”U"ÄTuTRb'ÒÂô6ö×WF—F–öåFW7E‚‚’¢6†V6²‚%&VÖ–W"ÆVwVRf—‡GW&RFG2æÖVB6ö×WF—F–öâÇFW&æF—fW2"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–â6ö×WF—F–öå÷&÷w7ÒÓÐ¢³CÂCÂC2ÂCBÂCWÒæ@¢ÆÂ‡&÷rævWB‚&ÆVwVUöÖF6‚"’f÷"&÷r–â6ö×WF—F–öå÷&÷w2’¢6ö×WF—F–öå÷6V7W&U÷&÷w2Ò°¢&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚&6ö×WF—F–öå÷6V7W&R"¢f÷"&÷r–â6ö×WF—F–öå÷&÷w7Ð¢6†V6²‚$æ÷'vVv–â&VÖ–W"ÆVwVRfÖ–Ç’—26V7W&Rv—F†÷WB'&öF67FW"FF"À¢6ö×WF—F–öå÷6V7W&U÷&÷w2ævWBƒC’—2G'VRæ@¢6ö×WF—F–öå÷6V7W&U÷&÷w2ævWBƒC2’—2G'VR¢6†V6²‚%T²UÂ6FVv÷'’ÇW2V—F†W"f—‡GW&RFVÒ—26V7W&R"À¢6ö×WF—F–öå÷6V7W&U÷&÷w2ævWBƒCB’—2G'VRæ@¢6ö×WF—F–öå÷6V7W&U÷&÷w2ævWBƒCR’—2G'VRæ@¢6ö×WF—F–öå÷6V7W&U÷&÷w2ævWBƒC’—2fÇ6R¢–çFVw&FVEö6ö×WF—F–öâÒöÖF6…÷7÷'G5öf—‡GW&Uö6†ææVÇ2€¢6ö×WF—F–öåöf—‡GW&RÂ²&ÖF6…÷F‡&W6†öÆB#¢ãc'ÒÂ6ö×WF—F–öåö6†ææVÇ2À¢²&æò#¢$äòÂ5õ%E2"Â'V²#¢%T²Â5õ%E2"À¢'V²ÖWÂ#¢%T²ÂUÂ$TÔ”U"ÄTuTRb'ÒÂô6ö×WF—F–öåFW7E‚‚’¢–çFVw&FVE÷6V7W&RÒ°¢&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚&6ö×WF—F–öå÷6V7W&R"¢f÷"&÷r–â–çFVw&FVEö6ö×WF—F–öå²'eö†—G2%×Ð¢6†V6²‚&–çFVw&FVBÖF6†W"&W6W'fW26V7W&RfÆw2gFW"FVÒÖ†—BFVGWÆ–6F–öâ"À¢–çFVw&FVE÷6V7W&RævWBƒC’—2G'VRæ@¢–çFVw&FVE÷6V7W&RævWBƒC2’—2G'VRæ@¢–çFVw&FVE÷6V7W&RævWBƒCB’—2G'VRæ@¢–çFVw&FVE÷6V7W&RævWBƒCR’—2G'VR¢Væf÷&6VE÷6V7W&RÒöVæf÷&6Uöf—‡GW&U÷6V7W&UöÖF6†W2†6ö×WF—F–öåöf—‡GW&RÂ°¢&ÖF6†W2#¢µÒÂ'eö†—G2#¢°¢²'7G&VÕö–B#¢C#À¢'‡G&VÕöæÖR#¢$äó¢b5õ%B$TÔ”U"ÄTuTR"d•äò"À¢&6FVv÷'’#¢$ä÷Âäõ%t’„Bõ$r'ÒÀ¢²'7G&VÕö–B#¢C#Â'‡G&VÕöæÖR#¢%T³¢UÂÄTTE2"À¢&6FVv÷'’#¢%T·ÂUÂ$TÔ”U"ÄTuTRb'ÒÀ¢²'7G&VÕö–B#¢C#"Â'‡G&VÕöæÖR#¢%T³¢UÂäõED”ät„Ò"À¢&6FVv÷'’#¢%T·ÂUÂ$TÔ”U"ÄTuTRb'Õ×Ò¢Væf÷&6VEö'•ö–BÒ·&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚'6V7W&U÷&V6öâ"¢f÷"&÷r–âVæf÷&6VE÷6V7W&U²'eö†—G2%×Ð¢6†V6²‚&f–æÂ&W7öç6R726V7W&W2äòb7÷'BÂæBT²UÂFVÒ6†ææVÇ2"À¢Væf÷&6VEö'•ö–BÓÒ°¢C#¢&æ÷'v•÷&VÖ–W%öÆVwVR"À¢C#¢'VµöWÅöf—‡GW&U÷FVÒ"À¢C##¢'VµöWÅöf—‡GW&U÷FVÒ'Ò¢v—F‚÷Vâ…õöf–ÆUõòÂ'""ÂVæ6öF–æsÒ'WFbÓ‚"’26÷W&6Uöf–ÆS ¢6÷W&6U÷FW‡BÒ6÷W&6Uöf–ÆRç&VB‚¢6†V6²‚'7÷'G2&Vg&W6‚—2&W6W'fRÆVwVR&Vf÷&R6V7W&RVæf÷&6VÖVçB"À¢6÷W&6U÷FW‡Bæ6÷VçB€¢vÆVwVUöæÖRÒ7G"†f—‡GW&RævWB‚&ÆVwVUöæÖR"’÷"""’ç7G&—‚•³£cÒr’ãÒæ@¢6÷W&6U÷FW‡Bæ6÷VçB€¢vÆVwVUöæÖRÒ7G"‡&uöf—‡GW&RævWB‚&ÆVwVUöæÖR"’÷"""’ç7G&—‚•³£cÒr’ãÒæ@¢6÷W&6U÷FW‡Bæ6÷VçB‚r&ÆVwVUöæÖR#¢ÆVwVUöæÖRr’ãÒB¢66†U÷&V6Æ76–f–VBÒöVæf÷&6Uöf—‡GW&U÷6V7W&UöÖF6†W2€¢6ö×WF—F–öåöf—‡GW&RÂ÷7÷'G5÷&W7VÇEöf÷%ö6Æ–VçB‡°¢&ÖF6†W2#¢µÒÂ'eö†—G2#¢·°¢'7G&VÕö–B#¢C#2À¢'‡G&VÕöæÖR#¢$äó¢b5õ%B$TÔ”U"ÄTuTR2$räò"À¢&6FVv÷'’#¢$ä÷Âäõ%t’„Bõ$r'Õ×ÒÂô6ö×WF—F–öåFW7E‚‚’’¢6†V6²‚&66†VB&VÖ–W"ÆVwVR&÷w2&R&V6Æ76–f–VBv†Vâ&VB"À¢66†U÷&V6Æ76–f–VE²'eö†—G2%Õ³ÒævWB‚'6V7W&U÷&V6öâ"’ÓÐ¢&æ÷'v•÷&VÖ–W%öÆVwVR"¢6†V6²‚&f—‡GW&R&VæFW&–ær–æFWVæFVçFÇ’&V6övæ—¦W2æ÷'vVv–âb7÷'BÂ"À¢&gVæ7F–öâ7W7FöÕ&VÖ–W$ÆVwVU6V7W&R†6‚Æb’"–âtRæ@¢tRæ6÷VçB‚&7W7FöÕ&VÖ–W$ÆVwVU6V7W&R†ÒÆb’"’ãÒ"æ@¢&7W7FöÕ&VÖ–W$ÆVwVU6V7W&R†6‚Æb’"–âtR¢6†V6²‚'7÷'G26V&6‚¶VW2'F–Âb†—G2–â÷76–&ÆR6FVv÷&–W2"À¢&6öç7B÷76–&ÆUcÕµÒ"–âtRæ@¢"†bçeö†—G7ÇÅµÒ’æf–ÇFW"†ÓÓæf—‡GW&T6†ææVÅ&æ²†ÒÆb“ÓÓÓ2"–âtR¢Vç&VÆFVE÷FW7BÒ¶F–7B‡66†VGVÆU÷FW7E³Ò•Ð¢ö÷fW&Æ•öf—‡GW&U÷&÷w2‡Vç&VÆFVE÷FW7BÂ·°¢&†öÖR#¢%÷'FÆæB†V'G2öb–æR"Â&v’#¢$f÷'v&BÖF—6öâ"À¢'7F'B#¢###bÓ‚ÓeCƒ£3£¢"Â&'•ö6÷VçG'’#¢²%U2#¢²$U5â6VÆV7B%××ÕÒ¢6†V6²‚'FVÒ66†VGVÆR÷fW&Æ’6ææ÷BVæB'F–ÂÖæÖRf—‡GW&W2"À¢ÆVâ‡Vç&VÆFVE÷FW7B’ÓÒ¢÷öæVçE÷6†RÒ²'7FGW2#¢²'WF5F–ÖR#¢###bÓ‚ÓeCƒ£3£¢'ÒÀ¢&÷öæVçB#¢²&æÖR#¢$6†GFæööv&VBvöÇfW242'×Ð¢6†V6²‚'&V7W'6—fR÷öæVçB&÷w2&WV—&RFVÒ&÷fVææ6R"À¢æ÷Böf—‡GW&Uö6æF–FFUö–çföÇfW5÷FVÒ€¢÷öæVçE÷6†RÂ#ƒc""Â%vöÇfW&†×FöâvæFW&W'2"ÂfÇ6R’æ@¢öf—‡GW&Uö6æF–FFUö–çföÇfW5÷FVÒ€¢÷öæVçE÷6†RÂ#ƒc""Â%vöÇfW&†×FöâvæFW&W'2"ÂG'VR’¢7W'&VçE÷FW7BÒö7W'&VçEöæE÷W6öÖ–æuöf—‡GW&W2…°¢²&†öÖR#¢$öÆB"Â&v’#¢$Ö’"Â'7F'B#¢###bÓRÓC#££¢'ÒÀ¢²&†öÖR#¢$†V'G2"Â&v’#¢$&Væf–6"Â'7F'B#¢###bÓ‚Ó5Cƒ£CS£¢'ÕÒÀ¢FFWF–ÖRæFFWF–ÖRƒ##bÂ‚Â2Â"ÂG¦–æfóÖFFWF–ÖRçF–ÖW¦öæRçWF2’çF–ÖW7F×‚’¢6†V6²‚&†—7F÷&–6ÂFVÒf—‡GW&W2W†6ÇVFVBg&öÒ6V&6‚"À¢ÆVâ†7W'&VçE÷FW7B’ÓÒæB7W'&VçE÷FW7E³Õ²&†öÖR%ÒÓÒ$†V'G2"¢6†V6²‚&6÷VçG'’–6¶W"W6W2Æ&VÆVB÷'GVvÂ6öFR"À¢%²wBrÂ	ø{_	ø{’rÂu÷'GVvÂuÒ"–âtRæ@¢v–CÒ'5ö62"G—SÒ&†–FFVâ"r–âtR¢÷&–v–æÅö6÷VçG'•öfWF6‚ÒvÆö&Ç2‚•²&fWF6…ö6÷VçG'•öf—‡GW&W2%Ð¢wV–FUöÆö6²ÒF‡&VF–æräÆö6²‚¢wV–FUö7F—fRÒ³Ð¢wV–FU÷V²Ò³Ð¢FVbf¶Uö6÷VçG'•öfWF6‚†6÷VçG'’“ ¢v—F‚wV–FUöÆö6³ ¢wV–FUö7F—fU³Ò³Ò¢wV–FU÷Vµ³ÒÒÖ‚†wV–FU÷Vµ³ÒÂwV–FUö7F—fU³Ò¢F–ÖRç6ÆVWƒã¢v—F‚wV–FUöÆö6³ ¢wV–FUö7F—fU³ÒÓÒ¢&WGW&â·²&6÷VçG'’#¢6÷VçG'—ÕÐ¢G'“ ¢vÆö&Ç2‚•²&fWF6…ö6÷VçG'•öf—‡GW&W2%ÒÒf¶Uö6÷VçG'•öfWF6€¢wV–FU÷&÷w2ÂwV–FUöW'&÷'2ÒöfWF6…ö6÷VçG'•öwV–FW2€¢²&æò"Â&v""Â'W2%ÒÂÖ…÷v÷&¶W'3Ó2¢f–æÆÇ“ ¢vÆö&Ç2‚•²&fWF6…ö6÷VçG'•öf—‡GW&W2%ÒÒ÷&–v–æÅö6÷VçG'•öfWF6€¢6†V6²‚&fÆÆ&6²6÷VçG'’wV–FW2ÆöB6öæ7W'&VçFÇ’–â7F&ÆR÷&FW""À¢æ÷BwV–FUöW'&÷'2æBwV–FU÷Vµ³ÒãÒ"æ@¢¶6÷VçG'’f÷"6÷VçG'’Â÷&÷w2–âwV–FU÷&÷w5ÒÓÒ²&æò"Â&v""Â'W2%Ò¢6†V6²‚&6÷VçG'’wV–FW2fö–B÷F–öæÂ'VæFÆVBÖöGVÆW2"À¢&6öæ7W'&VçBægWGW&W2"æ÷B–â7—2æÖöGVÆW2¢&öf–ÆUö&6·WÒ7&VFU÷&öf–ÆUö&6·W‚'&öf–ÆR"Â²&f–ÇFW"#¢&ÆÂ'Ò¢6†V6²‚'&öf–ÆR&6·WöÖ—G2‡G&VÒ7&VFVçF–Ç2"À¢õ$ôd”ÄUõ4T5$UEô´U•2æ—6F—6¦ö–çB‡&öf–ÆUö&6·W²&6öæf–r%Ò’¢6†V6²‚'&öf–ÆR&6·W&WF–ç2ff÷&—FW2"Â—6–ç7Fæ6R‡&öf–ÆUö&6·W²&ff÷&—FW2%ÒÂF–7B’¢gVÆÅö&6·WÒ7&VFU÷&öf–ÆUö&6·W‚&gVÆÂ"Â²&f–ÇFW"#¢&ÆÂ'Ò¢6†V6²‚&gVÆÂ&6·W–æ6ÇVFW2‡G&VÒ7&VFVçF–Âf–VÆG2"À¢õ$ôd”ÄUõ4T5$UEô´U•2æ—77V'6WB†gVÆÅö&6·W²&6öæf–r%Ò’¢ÖW&vVE÷FW7BÒöÖW&vUöff÷&—FUöÆ—7G2€¢'FV×2"Â·²'FVÕö–B#¢#"Â&æÖR#¢$öÆB'ÕÒÀ¢·²'FVÕö–B#¢#"Â&æÖR#¢%WFFVB'ÒÂ²'FVÕö–B#¢#""Â&æÖR#¢$æWr'ÕÒ¢6†V6²‚&&6·Wff÷&—FW2ÖW&vRæBFVGWÆ–6FR"À¢ÆVâ†ÖW&vVE÷FW7B’ÓÒ"æBÖW&vVE÷FW7E³Õ²&æÖR%ÒÓÒ%WFFVB"¢7W'&VçE÷FW7Eö6frÒF–7B„DTdTÅEô4ôäd”rÂ&öf–ÆUöæÖSÒ$7W'&VçB"À¢‡G&VÕö†÷7CÒ&öÆBæW†×ÆR"¢–æ6öÖ–æu÷FW7Eö6frÒF–7B„DTdTÅEô4ôäd”rÂ&öf–ÆUöæÖSÒ$–×÷'FVB"À¢‡G&VÕö†÷7CÒ&æWræW†×ÆR"¢7W'&VçE÷FW7EöfbÒ¶¶W“¢µÒf÷"¶W’–âôddõ$•DUôÄ•5Eô´U•7Ð¢–æ6öÖ–æu÷FW7EöfbÒ¶¶W“¢µÒf÷"¶W’–âôddõ$•DUôÄ•5Eô´U•7Ð¢7W'&VçE÷FW7Eöfe²&6†ææVÇ2%ÒÒ·²'7G&VÕö–B#¢rÂ&æÖR#¢$öÆB6†ææVÂ'ÕÐ¢–æ6öÖ–æu÷FW7Eöfe²&6†ææVÇ2%ÒÒ·²'7G&VÕö–B#¢‚Â&æÖR#¢$æWr6†ææVÂ'ÕÐ¢&W7F÷&VEö6fu÷FW7BÂ&W7F÷&VEöfe÷FW7BÒ÷&W&Uö&6·W÷&W7F÷&R€¢&gVÆÂ"Â–æ6öÖ–æu÷FW7Eö6frÂ–æ6öÖ–æu÷FW7EöfbÀ¢7W'&VçE÷FW7Eö6frÂ7W'&VçE÷FW7Eöfb¢6†V6²‚&gVÆÂ&6·W&WÆ6W2&÷f–FW"Ö&÷VæBff÷&—FW2"À¢&W7F÷&VEöfe÷FW7E²&6†ææVÇ2%ÒÓÒ–æ6öÖ–æu÷FW7Eöfe²&6†ææVÇ2%Ò¢6†V6²‚&gVÆÂ&6·W&WÆ6W26öæf–wW&F–öâ"À¢&W7F÷&VEö6fu÷FW7E²'&öf–ÆUöæÖR%ÒÓÒ$–×÷'FVB"æ@¢&W7F÷&VEö6fu÷FW7E²'‡G&VÕö†÷7B%ÒÓÒ&æWræW†×ÆR"¢G'“ ¢÷fÆ–FFVEö&6·W÷–ÆöB‡²&f÷&ÖB#¢&öÆ÷2×GfÖFRÖ&6·W"À¢&f÷&ÖE÷fW'6–öâ#¢ã’À¢&&6·W÷G—R#¢&gVÆÂ"Â&6öæf–r#¢·ÒÀ¢&ff÷&—FW2#¢·×Ò¢–çfÆ–Eö&6·W÷&V¦V7FVBÒfÇ6P¢W†6WBfÇVTW'&÷# ¢–çfÆ–Eö&6·W÷&V¦V7FVBÒG'VP¢6†V6²‚&æöâÖ–çFVvW"&6·WfW'6–öâ&V¦V7FVB"Â–çfÆ–Eö&6·W÷&V¦V7FVB¢æ÷rÒFFWF–ÖRæFFWF–ÖRƒ##bÂ‚ÂÂG¦–æfóÖFFWF–ÖRçF–ÖW¦öæRçWF2¢6†V6²‚'&VÆV6VBÖ÷f–R–æ6ÇVFVB"Âö6–æVÖWF÷&VÆV6VEöÖ÷f–R€¢²'&VÆV6VB#¢###bÓ‚ÓC££ã¢'ÒÂæ÷r’¢6†V6²‚&gWGW&RÖ÷f–RW†6ÇVFVB"Âæ÷Bö6–æVÖWF÷&VÆV6VEöÖ÷f–R€¢²'&VÆV6VB#¢###bÓ‚Ó%C££ã¢'ÒÂæ÷r’¢6†V6²‚'VæFFVB7W'&VçB×–V"Ö÷f–RW†6ÇVFVB"Âæ÷Bö6–æVÖWF÷&VÆV6VEöÖ÷f–R€¢²'&VÆV6T–æfò#¢###b'ÒÂæ÷r’¢6†V6²‚&öÆFW"Ö÷f–R–æ6ÇVFVB"Âö6–æVÖWF÷&VÆV6VEöÖ÷f–R€¢²'&VÆV6T–æfò#¢###R'ÒÂæ÷r’¢6×ÆUö6†ææVÇ2Ò°¢²&æÖR#¢$äó¢Eb"7÷'B"Â'7G&VÕö–B#¢Â&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$Ä•dRÂôÄÄôâÄ”Ô54ôÂÒ%$äâÂduEbb2"À¢'7G&VÕö–B#¢"Â&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$Ä•dRÂ%$äâÒ„Ô´ÒÂduEbbR"À¢'7G&VÕö–B#¢2Â&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$%$äâ""Â'7G&VÕö–B#¢BÂ&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$äó¢Eb"Ä’Âb"Â'7G&VÕö–B#¢RÂ&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢%6·’7÷'G2"T„B"Â'7G&VÕö–B#¢bÂ&6FVv÷'•ö–B#¢#F²'ÒÀ¢Ð¢6×ÆUö6G2Ò²&æò#¢$ä÷Âäõ%t’"Â'b#¢$ä÷ÂbUdTåE2"À¢#F²#¢#D²ÂT„B4„ääTÅ2'Ð¢ÆFf÷&Õö–G2Ò·&÷u²'7G&VÕö–B%Òf÷"&÷r–âÖF6…ö6†ææVÇ2€¢²$äò#¢²%Eb"Æ’„äò’%×ÒÂ6×ÆUö6†ææVÇ2Â6×ÆUö6G2ÂãC’—Ð¢6†V6²‚'7G&VÖ–ærÆFf÷&Ò6æF–FFW2&WF–æVB"ÂÆFf÷&Õö–G2ÓÒ³WÒ¢6†V6²‚'Vç&VÆFVBçVÖ&W&VB7G&VÖ–ær6Æ÷G2&R&VÖ÷fVBg&öÒf—‡GW&R&W7VÇG2"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–âöf–ÇFW%÷7G&VÖ–æu÷ÆFf÷&Õ÷6Æ÷G2…°¢²'7G&VÕö–B#¢RÂ&ÖF6†VB#¢%Eb"Æ’„äò’"À¢&f—‡GW&UöÖF6‚#¢&vVæW&–2'ÒÀ¢²'7G&VÕö–B#¢bÂ&ÖF6†VB#¢%Eb"Æ’„äò’"À¢&f—‡GW&UöÖF6‚#¢&W†7B'ÒÀ¢²'7G&VÕö–B#¢rÂ&ÖF6†VB#¢%Eb"7÷'B"À¢&f—‡GW&UöÖF6‚#¢&vVæW&–2'ÒÀ¢Ò•ÒÓÒ³bÂuÒ¢ÆFf÷&Õöæö—6RÒÖF6…ö6†ææVÇ2€¢²%B#¢²$ÔTò%ÒÂ$U2#¢²$D¤â%ÒÂ$D²#¢²%f–Æ’FVæÖ&²%×ÒÀ¢·²&æÖR#¢%GÄÔTó¢4äâõ%ETtÂ"Â'7G&VÕö–B#¢SÀ¢&6FVv÷'•ö–B#¢&ÖVò'ÒÀ¢²&æÖR#¢%GÄÔTó¢ÕEbõ%ETtÂ"Â'7G&VÕö–B#¢S"À¢&6FVv÷'•ö–B#¢&ÖVò'ÒÀ¢²&æÖR#¢%GÄÔTó¢EbtÄô$òõ%ETtÂ"Â'7G&VÕö–B#¢S2À¢&6FVv÷'•ö–B#¢&ÖVò'ÒÀ¢²&æÖR#¢$U3¢D¤âÄÄ”t"Â'7G&VÕö–B#¢SBÀ¢&6FVv÷'•ö–B#¢&F¦â'ÒÀ¢²&æÖR#¢$c¢D¤âc"Â'7G&VÕö–B#¢SRÀ¢&6FVv÷'•ö–B#¢&F¦â'ÒÀ¢²&æÖR#¢$U3¢D¤âb"Â'7G&VÕö–B#¢SbÀ¢&6FVv÷'•ö–B#¢&F¦â×b'ÒÀ¢²&æÖR#¢$D³¢d”Ä’d”ÄÒ5D”ôâ"Â'7G&VÕö–B#¢SrÀ¢&6FVv÷'•ö–B#¢'f–Æ’Öf–ÆÒ'ÒÀ¢²&æÖR#¢$D³¢d”Ä’b"Â'7G&VÕö–B#¢S‚À¢&6FVv÷'•ö–B#¢'f–Æ’×b'ÒÀ¢²&æÖR#¢$D³¢d”Ä’D³""Â'7G&VÕö–B#¢S’À¢&6FVv÷'•ö–B#¢'f–Æ’×b'ÕÒÀ¢²&ÖVò#¢%GÄÔTò"Â&F¦â#¢$U7ÄD¤â"À¢&F¦â×b#¢$U7ÄD¤âb"À¢'f–Æ’Öf–ÆÒ#¢$D·Åd”Ä’d”ÄÒ5D”ôâ"À¢'f–Æ’×b#¢$D·Åd”Ä’b'ÒÂãC’¢6†V6²‚'ÆFf÷&ÒÆ—7F–æw2W†6ÇVFRVç&VÆFVB6¶vR6†ææVÇ2"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–âÆFf÷&Õöæö—6WÒÓÒ³SbÂS‡Ò¢6†V6²‚%f–Æ’FöW2æ÷BÖ¶R6FVv÷'’7FæFÆöæRÆ’'V6¶WB"À¢æ÷Bö—5÷eö6FVv÷'’‚$D·Åd”Ä’d”ÄÒ5D”ôâ"’æ@¢ö—5÷eö6FVv÷'’‚$ä÷ÅEb"Ä’"’æ@¢ö—5÷eö6FVv÷'’‚$D·Åd”Ä’b"’¢&÷f–FW%÷&÷w2ÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%Eb"7÷'B"Â%Eb"Æ’„äò’%×ÒÀ¢6×ÆUö6†ææVÇ2Â6×ÆUö6G2ÂãC’¢&÷f–FW%öW†7BÒ·&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚'&÷f–FW%öW†7B"’f÷"&÷r–â&÷f–FW%÷&÷w7Ð¢6†V6²‚&W†7BÆ–æV"&÷f–FW"&öÖ÷FVB"Â&÷f–FW%öW†7BævWBƒ’—2G'VR¢6†V6²‚'7G&VÖ–ær&÷f–FW"æ÷B&öÖ÷FVB"Â&÷f–FW%öW†7BævWBƒR’—2fÇ6R¢VµóF²ÒÖF6…ö6†ææVÇ2‡²%T²#¢²%6·’7÷'G2"%×ÒÀ¢6×ÆUö6†ææVÇ2Â6×ÆUö6G2ÂãC’¢6†V6²‚&6÷VçG'–ÆW72F²&÷f–FW"&öÖ÷FVB"À¢ÆVâ‡VµóF²’ÓÒæBVµóFµ³ÒævWB‚'&÷f–FW%öW†7B"’—2G'VR¢†öæuö¶öæuóF²ÒÖF6…ö6†ææVÇ2€¢²%T²#¢²%&VÖ–W"7÷'G2"%×ÒÀ¢·²&æÖR#¢$†öæv¶öæräõr&VÖ–W"7÷'G2"D²"Â'7G&VÕö–B#¢“‚À¢&6FVv÷'•ö–B#¢#F²'ÕÒÂ6×ÆUö6G2ÂãC’¢6†V6²‚'w&—GFVâf÷&V–vâ6÷VçG'’&V¦V7FVB–ç6–FRvÆö&ÂF²6FVv÷'’"À¢†öæuö¶öæuóF²ÓÒµÒ¢Væ¶æ÷våóF²ÒÖF6…ö6†ææVÇ2€¢²%T²#¢²%&VÖ–W"7÷'G2"%×ÒÀ¢·²&æÖR#¢%&VÖ–W"7÷'G2"D²"Â'7G&VÕö–B#¢“rÀ¢&6FVv÷'•ö–B#¢#F²'ÕÒÂ6×ÆUö6G2ÂãC’¢6†V6²‚&vÆö&ÂF²6FVv÷'’&VÖ–ç2VÆ–v–&ÆR"ÂÆVâ‡Væ¶æ÷våóF²’ÓÒ¢6&–&&Våö6'FööâÒÖF6…ö6†ææVÇ2€¢²%U2#¢²%U4æWGv÷&²%×ÒÀ¢·²&æÖR#¢$Õ¢4%DôôâäUEtõ$²"Â'7G&VÕö–B#¢“bÀ¢&6FVv÷'•ö–B#¢&7"'ÕÒÀ¢²&7"#¢$5#¢6'&–&Vâ×'ÒÂãc"¢6†V6²‚$5"6FVv÷'’&V¦V7FVBf÷"U2fö÷F&ÆÂ'&öF67FW""À¢6&–&&Våö6'FööâÓÒµÒ¢vÆö&Åö6'FööâÒÖF6…ö6†ææVÇ2€¢²%U2#¢²%U4æWGv÷&²%×ÒÀ¢·²&æÖR#¢$4%DôôâäUEtõ$²"Â'7G&VÕö–B#¢“RÀ¢&6FVv÷'•ö–B#¢#F²'ÕÒÂ6×ÆUö6G2ÂãC¢6†V6²‚$6'FööâæWGv÷&²W†6ÇVFVB&Vv&FÆW72öb6FVv÷'’"À¢vÆö&Åö6'FööâÓÒµÒ¢æöåöfö÷F&ÆÂÒÖF6…ö6†ææVÇ2€¢²%U2#¢²%U4æWGv÷&²%×ÒÀ¢·²&æÖR#¢%U3¢ÔÄ"æWGv÷&·2"Â'7G&VÕö–B#¢“’À¢&6FVv÷'•ö–B#¢'W2×7÷'G2'ÕÒÀ¢²'W2×7÷'G2#¢%U2Â5õ%E2'ÒÂãC¢6†V6²‚&÷F†W"×7÷'BæWGv÷&·2W†6ÇVFVBg&öÒfö÷F&ÆÂ"Âæöåöfö÷F&ÆÂÓÒµÒ¢W7å÷6¶vRÒÖF6…ö6†ææVÇ2€¢²%U2#¢²$U5â6VÆV7B"Â$U5âVæÆ–Ö—FVB%×ÒÀ¢·²&æÖR#¢%U3¢U5âVæÆ–Ö—FVB3B„B"Â'7G&VÕö–B#¢À¢&6FVv÷'•ö–B#¢'W2×7÷'G2'ÒÀ¢²&æÖR#¢##Bós¢¥U5D”4RÄTuTRTäÄ”Ô•DTB"Â'7G&VÕö–B#¢"À¢&6FVv÷'•ö–B#¢##BÓr'ÒÀ¢²&æÖR#¢%$”ÔS¢$4U"4TÄT5B"Â'7G&VÕö–B#¢2À¢&6FVv÷'•ö–B#¢'&–ÖR'ÒÀ¢²&æÖR#¢%U3¢U5âäUu2„B"Â'7G&VÕö–B#¢BÀ¢&6FVv÷'•ö–B#¢'W2×7÷'G2'ÕÒÀ¢²'W2×7÷'G2#¢%U2Â5õ%E2"Â##BÓr#¢##Bór"Â'&–ÖR#¢%$”ÔR'ÒÀ¢ãC¢6†V6²‚&çVÖ&W&VBU5â6¶vRfVVB&WF–æVB"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–âW7å÷6¶vWÒÓÒ³Ò¢f–Æ•öæòÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%f–Æ’æ÷'v’%×ÒÀ¢·²&æÖR#¢$äó¢b7÷'B„B"Â'7G&VÕö–B#¢’Â&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b7÷'BÆ—fRB"Â'7G&VÕö–B#¢Â&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b7÷'B&VÖ–W"ÆVwVR2"Â'7G&VÕö–B#¢Â&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b7÷'BvöÆb"Â'7G&VÕö–B#¢"Â&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢%5tS¢b7÷'BÆ—fR""Â'7G&VÕö–B#¢2Â&6FVv÷'•ö–B#¢'7vR'ÒÀ¢²&æÖR#¢%b7÷'BD²"Â'7G&VÕö–B#¢BÂ&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢%b7÷'BVÇG&„B"Â'7G&VÕö–B#¢RÂ&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢%5tS¢b7÷'BVÇG&„B"Â'7G&VÕö–B#¢bÂ&6FVv÷'•ö–B#¢#F²'ÕÒÀ¢²&æò#¢$äòÂ5õ%E2"Â'7vR#¢%5tRÂ5õ%E2"À¢#F²#¢#D²ÂT„B4„ääTÅ2'ÒÂãc"¢6†V6²‚%f–Æ’æ÷'v’W‡æG2Fòæ÷'vVv–âb7÷'BWfVçBfVVG2"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–âf–Æ•öæ÷ÒÓÒ³’ÂÂÂBÂRÂgÒæ@¢ÆÂ†æ÷B&÷rævWB‚'&÷f–FW%öW†7B"’f÷"&÷r–âf–Æ•öæò’¢&VÖ–W%öÆVwVU÷6V7W&RÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%f–Æ’æ÷'v’%×ÒÀ¢·²&æÖR#¢$äó¢b7÷'B&VÖ–W"ÆVwVRd•äò"Â'7G&VÕö–B#¢“À¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b7÷'B„B"Â'7G&VÕö–B#¢“"À¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢%5tS¢b7÷'B&VÖ–W"ÆVwVR„B"Â'7G&VÕö–B#¢“2À¢&6FVv÷'•ö–B#¢'7vR'ÒÀ¢²&æÖR#¢$äó¢b7÷'BÂ"„Ud2"Â'7G&VÕö–B#¢“RÀ¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b7÷'BUÂ2$r"Â'7G&VÕö–B#¢“bÀ¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b7÷'B&VÒÆVwVRB"Â'7G&VÕö–B#¢“rÀ¢&6FVv÷'•ö–B#¢&æò'ÕÒÀ¢²&æò#¢$äòÂ5õ%E2"Â'7vR#¢%5tRÂ5õ%E2'ÒÂãc"À¢%&VÖ–W"ÆVwVR"¢6V7W&Uö'•ö–BÒ·&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚&6ö×WF—F–öå÷6V7W&R"¢f÷"&÷r–â&VÖ–W%öÆVwVU÷6V7W&WÐ¢6†V6²‚&öæÇ’æ÷'vVv–âb7÷'B&VÖ–W"ÆVwVRfÖ–Ç’vWG27W7FöÒ6V7W&RfÆr"À¢6V7W&Uö'•ö–BÓÒ³“¢G'VRÂ“#¢fÇ6RÂ“S¢G'VRÀ¢“c¢G'VRÂ“s¢G'VWÒ¢æöå÷&VÖ–W%÷6V7W&RÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%f–Æ’æ÷'v’%×ÒÀ¢·²&æÖR#¢$äó¢b7÷'B&VÖ–W"ÆVwVR„B"Â'7G&VÕö–B#¢“BÀ¢&6FVv÷'•ö–B#¢&æò'ÕÒÂ²&æò#¢$äòÂ5õ%E2'ÒÂãc"À¢$d7W"¢6†V6²‚&7W7FöÒb7÷'B6V7W&R'VÆRæWfW"Æ–W2÷WG6–FR&VÖ–W"ÆVwVR"À¢ÆVâ†æöå÷&VÖ–W%÷6V7W&R’ÓÒæ@¢æöå÷&VÖ–W%÷6V7W&U³ÒævWB‚&6ö×WF—F–öå÷6V7W&R"’—2fÇ6R¢6†V6²‚&7W7FöÒ6ö×WF—F–öâ6V7W&RfÆr&V6†W2&÷F‚f—‡GW&R&VæFW"F‡2"À¢tRæ6÷VçB‚&6ö×WF—F–öå÷6V7W&SÓÓ×G'VR"’ãÒ2¢fu÷–ÆöBÒ÷fuöæW‡EöFF€¢sÇ67&—B–CÒ%õôäU…EôDDõò"G—SÒ&Æ–6F–öâö§6öâ#âp¢w²'&÷2#§²'vU&÷2#§²&6†ææVÇ2#¥·²&æÖR#¢%b7÷'B&VÖ–W"ÆVwVR2"Âp¢r'6ÇVr#¢'b×7÷'B×&VÖ–W"ÖÆVwVRÓ2'Õ×××ÓÂ÷67&—Câr¢6†V6²‚%dræW‡Bæ§2wV–FR–ÆöB'6W2v—F†÷WB'&÷w6W"WFöÖF–öâ"À¢fu÷–ÆöBævWB‚&6†ææVÇ2"Â··ÕÒ•³ÒævWB‚'6ÇVr"’ÓÐ¢'b×7÷'B×&VÖ–W"ÖÆVwVRÓ2"¢6†V6²‚%drwV–FR6†ææVÂÆ–6W2ÖFòæ÷'vVv–âÆ–Æ—7B7VÆÆ–ær"À¢÷fuö6†ææVÅö¶W’‚$äó¢b7÷'BÂ2T„B"’ÓÐ¢÷fuö6†ææVÅö¶W’‚%b7÷'B&VÖ–W"ÆVwVR2"’¢f–Æ•öæ÷&F–5óF²ÒÖF6…ö6†ææVÇ2€¢²%4R#¢²%f–Æ’7vVFVâ%ÒÂ$D²#¢²%f–Æ’FVæÖ&²%×ÒÀ¢·²&æÖR#¢%b7÷'BVÇG&„B"Â'7G&VÕö–B#¢rÂ&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢%5tS¢b7÷'BÆ—fR""Â'7G&VÕö–B#¢‚Â&6FVv÷'•ö–B#¢'7vR'ÕÒÀ¢²#F²#¢#D²ÂT„B4„ääTÅ2"Â'7vR#¢%5tRÂ5õ%E2'ÒÂãc"¢6†V6²‚&×VÇF–Æ–æwVÂb7÷'BD²Ö2Fò7vVF—6‚æBFæ—6‚f–Æ’"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–âf–Æ•öæ÷&F–5óF·ÒÓÒ³wÒ¢f–Æ•öf’ÒÖF6…ö6†ææVÇ2€¢²$d’#¢²%f–Æ’f–æÆæB%×ÒÀ¢·²&æÖR#¢$d“¢b7÷'B7VöÖ’"Â'7G&VÕö–B#¢’Â&6FVv÷'•ö–B#¢&f’'ÒÀ¢²&æÖR#¢$d“¢b7÷'B²7VöÖ’"Â'7G&VÕö–B#¢#Â&6FVv÷'•ö–B#¢&f’'ÒÀ¢²&æÖR#¢$d“¢b7÷'Bfö÷F&ÆÂ"Â'7G&VÕö–B#¢#Â&6FVv÷'•ö–B#¢&f’'ÒÀ¢²&æÖR#¢$d“¢b7÷'BÆ—fR"Â'7G&VÕö–B#¢#"Â&6FVv÷'•ö–B#¢&f’'ÒÀ¢²&æÖR#¢$d“¢b7÷'BvöÆb"Â'7G&VÕö–B#¢#2Â&6FVv÷'•ö–B#¢&f’'ÒÀ¢²&æÖR#¢#D³¢b7÷'B"Â'7G&VÕö–B#¢#BÂ&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢#D³¢b7÷'B²"Â'7G&VÕö–B#¢#RÂ&6FVv÷'•ö–B#¢#F²'ÕÒÀ¢²&f’#¢$d’Â5õ%E2"Â#F²#¢#D²ÂT„B4„ääTÅ2'ÒÂãc"¢6†V6²‚%f–Æ’f–æÆæBW‡æG2Fòf–ææ—6‚æB6†&VBD²b7÷'BfVVG2"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–âf–Æ•öf—ÒÓÒ³’Â#Â#Â#"Â#BÂ#WÒ¢Æ–å÷f–Æ’ÒÖF6…ö6†ææVÇ2€¢²$äò#¢²%f–Æ’%ÒÂ$d’#¢²%f–Æ’%×ÒÀ¢·²&æÖR#¢$äó¢b7÷'BÆ—fR2"Â'7G&VÕö–B#¢#bÂ&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$d“¢b7÷'B²7VöÖ’"Â'7G&VÕö–B#¢#rÂ&6FVv÷'•ö–B#¢&f’'ÒÀ¢²&æÖR#¢#D³¢b7÷'B²"Â'7G&VÕö–B#¢#‚Â&6FVv÷'•ö–B#¢#F²'ÕÒÀ¢²&æò#¢$äòÂ5õ%E2"Â&f’#¢$d’Â5õ%E2"À¢#F²#¢#D²ÂT„B4„ääTÅ2'ÒÂãc"¢6†V6²‚&6÷VçG'’×66÷VBÆ–âf–Æ’W‡æG2Fòæ÷&F–2b7÷'BfVVG2"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–âÆ–å÷f–Æ—ÒÓÒ³#bÂ#rÂ#‡Ò¢6†V6²‚&æV&'’ÅEbFFW2–æ6ÇVFRF†—&Bf—‡GW&RgFW"g&–VæFÇ’æB7W"À¢öæV&'•öÇGeöFFW2…°¢²'7F'B#¢###bÓ‚ÓUC#££¢'ÒÀ¢²'7F'B#¢###bÓ‚Ó…C“££¢'ÒÀ¢²'7F'B#¢###bÓ‚Ó#%CC££¢'ÒÀ¢²'7F'B#¢###bÓ’ÓUCC££¢'ÕÒÂ###bÓ‚ÓB"’ÓÐ¢²###bÓ‚ÓR"Â###bÓ‚Ó‚"Â###bÓ‚Ó#"%Ò¢6†V6²‚&FVç6R66†VGVÆW2&WF–âF†RV–v‡F‚æV&'’ÅEbwV–FRFFR"À¢öæV&'•öÇGeöFFW2…°¢²'7F'B#¢b###bÓ‚×¶F“£&GÕCC££¢'Ð¢f÷"F’–â&ævRƒRÂ#2•ÒÂ###bÓ‚ÓB"’ÓÐ¢¶b###bÓ‚×¶F“£&GÒ"f÷"F’–â&ævRƒRÂ#2•Ò¢7÷'E÷Geö6÷VçG'•÷&÷w2ÒÖF6…ö6†ææVÇ2€¢²%B#¢²%7÷'BEbR%×ÒÀ¢·²&æÖR#¢%õ#¢7÷'BEbR"Â'7G&VÕö–B#¢RÂ&6FVv÷'•ö–B#¢'÷"'ÒÀ¢²&æÖR#¢%5tS¢7÷'BEbR"Â'7G&VÕö–B#¢bÂ&6FVv÷'•ö–B#¢'7vR'ÒÀ¢²&æÖR#¢%d•¢7÷'BEbR"Â'7G&VÕö–B#¢rÂ&6FVv÷'•ö–B#¢'f—'ÒÀ¢²&æÖR#¢%dó¢7÷'BEbR"Â'7G&VÕö–B#¢‚Â&6FVv÷'•ö–B#¢'fò'ÕÒÀ¢²'÷"#¢%õ"Â5õ%E2"Â'7vR#¢%5tRÂ5õ%E2"À¢'f—#¢%d•vöÆB"Â'fò#¢%dó¢5õ%E2'ÒÂãC¢6†V6²‚'F‡&VRÖÆWGFW"f÷&V–vâ6÷VçG'’&Vf—†W2&R&V¦V7FVB"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–â7÷'E÷Geö6÷VçG'•÷&÷w7ÒÓÒ³RÂrÂ‡Ò¢6†V6²‚&6÷VçG'’Æ–6W26æöæ–6Æ—¦Rv—F†÷WBG&VF–ærF–W'226÷VçG&–W2"À¢ö65ög&öÕ÷&Vf—‚‚$DTâÂ7÷'B"’ÓÒ&F²"æ@¢ö65ög&öÕ÷&Vf—‚‚$äTC¢7÷'B"’ÓÒ&æÂ"æ@¢ö65ög&öÕ÷&Vf—‚‚%d•¢7÷'B"’—2æöæRæ@¢ö65ög&öÕ÷&Vf—‚‚%dó¢7÷'B"’—2æöæR¢6Æ72õFW7E‡G&VÓ ¢7FF–6ÖWF†ö@¢FVb7G&VÕ÷W&Â‡7G&VÕö–B“ ¢&WGW&â'FW7C¢"²7G"‡7G&VÕö–B¢7÷'G5÷6†&VBÒöÖF6…÷7÷'G5öf—‡GW&Uö6†ææVÇ2€¢²&†öÖR#¢$'&æâ"Â&v’#¢$†Ô¶Ò"Â'7F'B#¢###bÓ‚Ó%C#££¢"À¢&'•ö6÷VçG'’#¢²$äò#¢²%Eb"7÷'B%××ÒÀ¢²&ÖF6…÷F‡&W6†öÆB#¢ãC—ÒÂ6×ÆUö6†ææVÇ2Â6×ÆUö6G2ÂõFW7E‡G&VÒ‚’¢6†V6²‚'7÷'G2'VÆ²ÖF6†W"&WW6W26†&VB6FÆöwVR"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–â7÷'G5÷6†&VE²&ÖF6†W2%×ÒÓÒ³Òæ@¢2–â·&÷u²'7G&VÕö–B%Òf÷"&÷r–â7÷'G5÷6†&VE²'eö†—G2%×Ò¢†V'G5ö6æF–FFW2Òf–æE÷FVÕö6†ææVÇ2€¢²$†V'G2"Â$&Væf–6%ÒÀ¢·²&æÖR#¢$†V'G2Eb"Â'7G&VÕö–B#¢#Â&6FVv÷'•ö–B#¢'f—'ÒÀ¢²&æÖR#¢$†V'G2g2&ævW'2"Â'7G&VÕö–B#¢#"Â&6FVv÷'•ö–B#¢'f—'ÒÀ¢²&æÖR#¢%7VææÜ;‡&RÆ—fR"Â'7G&VÕö–B#¢#2Â&6FVv÷'•ö–B#¢'f—'ÒÀ¢²&æÖR#¢$†÷'6R&6–ær„B"Â'7G&VÕö–B#¢#BÂ&6FVv÷'•ö–B#¢'f—'ÕÒÀ¢²'f—#¢%d•vöÆB'ÒÂõFW7E‡G&VÒ‚’¢6†V6²‚&öæR×FVÒ6†ææVÇ2&VÖ–â÷76–&ÆRv—F†÷WB6FVv÷'’ÖöæÇ’æö—6R"À¢·&÷u²'7G&VÕö–B%Òf÷"&÷r–â†V'G5ö6æF–FFW7ÒÓÒ³#Â#'Ò¢Æ—fUöæö—6RÒÖF6…ö6†ææVÇ2€¢²$ÅEb#¢²$Æ—fR%×ÒÀ¢·²&æÖR#¢$æ÷'v’Æ—fR"Â'7G&VÕö–B#¢#RÂ&6FVv÷'•ö–B#¢'f—'ÒÀ¢²&æÖR#¢$ÕEbÆ—fR„B"Â'7G&VÕö–B#¢#bÂ&6FVv÷'•ö–B#¢'f—'ÕÒÀ¢²'f—#¢%d•vöÆB'ÒÂãC¢6†V6²‚&vVæW&–2Æ—fRÆ&VÂ6ææ÷B7&VFR'&öF67FW"6æF–FFW2"À¢Æ—fUöæö—6RÓÒµÒ¢7F÷&VE÷7÷'G2Ò÷7÷'G5÷&W7VÇEöf÷%÷7F÷&vR‡7÷'G5÷6†&VB¢6†V6²‚'7÷'G2F—6²66†RöÖ—G27&VFVçF–ÂÖ&V&–ærU$Ç2"À¢ÆÂ‚'W&Â"æ÷B–â&÷rf÷"¶W’–â‚&ÖF6†W2"Â'eö†—G2"¢f÷"&÷r–â7F÷&VE÷7÷'G5¶¶W•Ò’¢6†V6²‚'7÷'G2æò×&W7VÇB7FFR&VÖ–ç266†V&ÆR"À¢÷7÷'G5÷&W7VÇEöf÷%÷7F÷&vR‡²&ÆövvVEö–â#¢G'VRÀ¢&f–Æ&–Æ—G•ö6†V6¶VB#¢G'VRÂ&ÖF6†W2#¢µÒÂ'eö†—G2#¢µÐ¢Ò’ævWB‚&f–Æ&–Æ—G•ö6†V6¶VB"’—2G'VR¢öÆEöWu÷FW7BÒF–7B…ôUuô44„R¢G'“ ¢¶–6¶öfe÷FW7BÒFFWF–ÖRæFFWF–ÖRƒ##bÂ‚Â2Â‚ÂCRÀ¢G¦–æfóÖFFWF–ÖRçF–ÖW¦öæRçWF2’çF–ÖW7F×‚¢ôUuô44„Ræ6ÆV"‚¢ôUuô44„U²#sr%ÒÒ²'G2#¢F–ÖRçF–ÖR‚’Â'&öw&ÖÖW2#¢·°¢'F—FÆR#¢$†V'BöbÖ–FÆ÷F†–âb&Væf–6"À¢'7F'E÷G2#¢¶–6¶öfe÷FW7BÒ“Â'7F÷÷G2#¢¶–6¶öfe÷FW7B²s#Õ×Ð¢Wuöf÷VæBÒö66†VEöWuöF—66÷fW'’€¢·²&†öÖR#¢$†V'G2"Â&v’#¢$&Væf–6"À¢'7F'B#¢###bÓ‚Ó5Cƒ£CS£¢'ÕÒÀ¢·²&æÖR#¢%U3¢U5âæWw2„B"Â'7G&VÕö–B#¢srÀ¢&6FVv÷'•ö–B#¢'W2×7÷'G2'ÕÒÀ¢²'W2×7÷'G2#¢%U2Â5õ%E2'ÒÂõFW7E‡G&VÒ‚’¢Wu÷&÷w2ÒWuöf÷VæBævWB…÷7÷'G5öWfVçEö¶W’€¢$†V'G2"Â$&Væf–6"Â###bÓ‚Ó5Cƒ£CS£¢"’ÂµÒ¢6†V6²‚&66†VBUr–æFWVæFVçFÇ’F—66÷fW'2f—‡GW&R6†ææVÂ"À¢ÆVâ†Wu÷&÷w2’ÓÒæBWu÷&÷w5³ÒævWB‚&Wuö6öæf—&ÖVB"’—2G'VR¢6†V6²‚&Ö—76–ær66†VBUr&VÖ–ç2æWWG&Â"À¢ö66†VEöWuöF—66÷fW'’€¢·²&†öÖR#¢$ÆVVG2"Â&v’#¢$Öæ6†W7FW"Væ—FVB"À¢'7F'B#¢###bÓ‚Ó5Cƒ£CS£¢'ÕÒÀ¢µÒÂ·ÒÂõFW7E‡G&VÒ‚’’ÓÒ·Ò¢f–æÆÇ“ ¢ôUuô44„Ræ6ÆV"‚¢ôUuô44„RçWFFR†öÆEöWu÷FW7B¢6Æ72õFW7E&6–æu‡G&VÓ ¢7FF–6ÖWF†ö@¢FVb7G&VÕ÷W&Â‡7G&VÕö–B“ ¢&WGW&â'FW7C¢"²7G"‡7G&VÕö–B¢&6–æu÷&÷w2Òf–æE÷&6–æuö6†ææVÇ2€¢²'6W&–W2#¢&c"Â'&6R#¢$GWF6‚w&æB&—‚"Â&6—&7V—B#¢%¦æGfö÷'B"À¢'6W76–öâ#¢%7&–çBVÆ–g––ær'ÒÀ¢·²&æÖR#¢$cGWF6‚w&æB&—‚D²"Â'7G&VÕö–B#¢#Â&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢%6·’7÷'G2cT„B"Â'7G&VÕö–B#¢#Â&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢$cb"Â'7G&VÕö–B#¢#"Â&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$äó¢b5õ%Bd•$r"Â'7G&VÕö–B#¢#2Â&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b5õ%B""Â'7G&VÕö–B#¢#BÂ&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢d”Ä’bB"Â'7G&VÕö–B#¢#RÂ&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢d”Ä’d”ÄÒ5D”ôâ"Â'7G&VÕö–B#¢#bÂ&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$D³¢d”Ä’D³""Â'7G&VÕö–B#¢#rÂ&6FVv÷'•ö–B#¢&F²'ÒÀ¢²&æÖR#¢$äó¢c5$”åDµdÂ"Â'7G&VÕö–B#¢#‚Â&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢cäTDU$ÄäBu"Â'7G&VÕö–B#¢#’Â&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$#¢…TÅR•DÄ”âD²"Â'7G&VÕö–B#¢3Â&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢%T³¢4U$”RÒÔôå¤D²"Â'7G&VÕö–B#¢3Â&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢$•C¢•DÄ”âd•4„”ärEb"Â'7G&VÕö–B#¢3"Â&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢$f÷&×VÆB—FÆ–â6†×–öç6†—"Â'7G&VÕö–B#¢32À¢&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$—FÆ–âw&æB&—‚b""Â'7G&VÕö–B#¢3BÀ¢&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$cÖöç¦D²"Â'7G&VÕö–B#¢3RÂ&6FVv÷'•ö–B#¢#F²'ÕÒÀ¢F–7B‡6×ÆUö6G2ÂæóÒ$ä÷Âäõ%t’"ÂF³Ò$D·Âd”Ä’"’À¢õFW7E&6–æu‡G&VÒ‚’¢&6–æuö¶–æG2Ò·&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚&ÖF6…ö¶–æB"’f÷"&÷r–â&6–æu÷&÷w7Ð¢&VwVÆ'2Ò÷&æµöcö6öç7G'V7F÷%öG&—fW'2€¢·²&–B#¢&Æw6öâ"Â&æÖR#¢$Æ–ÒÆw6öâ'ÒÀ¢²&–B#¢&†F¦""Â&æÖR#¢$—66²†F¦"'ÒÀ¢²&–B#¢&Ö…÷fW'7FVâ"Â&æÖR#¢$Ö‚fW'7FVâ'ÕÒÀ¢²&Æw6öâ#¢Â&†F¦"#¢2Â&Ö…÷fW'7FVâ#¢GÒÀ¢²&Æw6öâ#¢bÂ&†F¦"#¢sBÂ&Ö…÷fW'7FVâ#¢'Ò¢6†V6²‚'FV×÷&'’c7V'7F—GWFR6ææ÷BF—7Æ6R&VwVÆ"FVÒG&—fW'2"À¢·&÷u²&–B%Òf÷"&÷r–â&VwVÆ'5ÒÓÒ²&Ö…÷fW'7FVâ"Â&†F¦"%Ò¢6†V6²‚'&6–ærWfVçB&öÖ÷FVB"Â&6–æuö¶–æG2ævWBƒ#’ÓÒ&WfVçB"¢6†V6²‚'&6–ær6W&–W26V6öæB"Â&6–æuö¶–æG2ævWBƒ#’ÓÒ'6W&–W2"¢6†V6²‚'&6–ær6FVv÷'’fÆÆ&6²"Â&6–æuö¶–æG2ævWBƒ#"’ÓÒ'÷76–&ÆR"¢6†V6²‚$æ÷'vVv–âb7÷'B—26öæf—&ÖVBc'&öF67FW""À¢&6–æuö¶–æG2ævWBƒ#2’ÓÒ&'&öF67FW""æB#Bæ÷B–â&6–æuö¶–æG2¢6†V6²‚$cf–Æ’fÆÆ&6²&WV—&W2b–âF†R6†ææVÂæÖR"À¢&6–æuö¶–æG2ævWBƒ#R’ÓÒ'÷76–&ÆR"æ@¢#bæ÷B–â&6–æuö¶–æG2æB#ræ÷B–â&6–æuö¶–æG2¢6†V6²‚$GWF6‚uÆ–6W2æB6W76–öâF—FÆW2&RFVf–æ—FR&6–ærÖF6†W2"À¢&6–æuö¶–æG2ævWBƒ#‚’ÓÒ&WfVçB"æB&6–æuö¶–æG2ævWBƒ#’’ÓÒ&WfVçB"¢6†V6²‚'&6–ærWfVçBÖF6†–ær&V¦V7G2æ÷F†W"&6RF—FÆR"À¢æ÷B‡³3Â3Â3"Â32Â3GÒb6WB‡&6–æuö¶–æG2’’¢—FÆ–å÷&÷w2Òf–æE÷&6–æuö6†ææVÇ2€¢²'6W&–W2#¢&c"Â'&6R#¢$—FÆ–âw&æB&—‚"Â&6—&7V—B#¢$Ööç¦"À¢'6W76–öâ#¢%&7F–6R'ÒÀ¢·²&æÖR#¢$#¢…TÅR•DÄ”âD²"Â'7G&VÕö–B#¢3Â&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢%T³¢4U$”RÒÔôå¤D²"Â'7G&VÕö–B#¢3Â&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢$•C¢•DÄ”âd•4„”ärEb"Â'7G&VÕö–B#¢3"Â&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢$f÷&×VÆB—FÆ–â6†×–öç6†—"Â'7G&VÕö–B#¢32À¢&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$—FÆ–âw&æB&—‚b""Â'7G&VÕö–B#¢3BÀ¢&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$cÖöç¦D²"Â'7G&VÕö–B#¢3RÂ&6FVv÷'•ö–B#¢#F²'ÕÒÀ¢6×ÆUö6G2ÂõFW7E&6–æu‡G&VÒ‚’¢—FÆ–åö¶–æG2Ò·&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚&ÖF6…ö¶–æB"’f÷"&÷r–â—FÆ–å÷&÷w7Ð¢6†V6²‚$—FÆ–âuÖF6†–ær&V¦V7G2F¦V7F—fRæB6—&7V—Bv÷&Bæö—6R"À¢æ÷B‡³3Â3Â3"Â37Òb6WB†—FÆ–åö¶–æG2’’æ@¢—FÆ–åö¶–æG2ævWBƒ3B’ÓÒ&WfVçB"æB—FÆ–åö¶–æG2ævWBƒ3R’ÓÒ&WfVçB"¢w&5÷&÷w2Òf–æE÷&6–æuö6†ææVÇ2€¢²'6W&–W2#¢'w&2"Â'&6R#¢%&ÆÇ’FVÂ&wV’"Â&6—&7V—B#¢""À¢'6W76–öâ#¢%&ÆÇ’vVV¶VæB'ÒÀ¢·²&æÖR#¢%&ÆÇ’Eb"Â'7G&VÕö–B#¢CÂ&6FVv÷'•ö–B#¢'&6–ær'ÒÀ¢²&æÖR#¢%$ÄÅ’"Â'7G&VÕö–B#¢CÂ&6FVv÷'•ö–B#¢'&6–ær'ÒÀ¢²&æÖR#¢%&ÆÇ–7&÷72Eb"Â'7G&VÕö–B#¢C"Â&6FVv÷'•ö–B#¢'&6–ær'ÒÀ¢²&æÖR#¢$—FÆ–âf—6†–ærEb"Â'7G&VÕö–B#¢C2À¢&6FVv÷'•ö–B#¢'&6–ær'ÕÒÀ¢F–7B‡6×ÆUö6G2Â&6–æsÒ%&6–ær"’ÂõFW7E&6–æu‡G&VÒ‚’¢w&5ö¶–æG2Ò·&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚&ÖF6…ö¶–æB"’f÷"&÷r–âw&5÷&÷w7Ð¢6†V6²‚%u$2&V6övæ—¦W2&ÆÇ’EbæB7FæFÆöæR&ÆÇ’6†ææVÇ2"À¢w&5ö¶–æG2ævWBƒC’ÓÒ'6W&–W2"æBw&5ö¶–æG2ævWBƒC’ÓÒ'6W&–W2"æ@¢C"æ÷B–âw&5ö¶–æG2æBC2æ÷B–âw&5ö¶–æG2¢w&5ö6ÆÇ6–vå÷&÷w2Òf–æE÷&6–æuö6†ææVÇ2€¢²'6W&–W2#¢'w&2"Â'&6R#¢%&ÆÇ’FVÂ&wV’"Â&6—&7V—B#¢""À¢'6W76–öâ#¢%&ÆÇ’vVV¶VæB'ÒÀ¢·²&æÖR#¢%U3¢ä$2…u$2’t4„”äuDôâD2„B’"Â'7G&VÕö–B#¢CBÀ¢&6FVv÷'•ö–B#¢'W2'ÒÀ¢²&æÖR#¢%U3¢ä$2B…u$2’ÄTU4%U$r„‚’"Â'7G&VÕö–B#¢CRÀ¢&6FVv÷'•ö–B#¢'W2'ÒÀ¢²&æÖR#¢%U3¢ä$2B…u$2’t4„”äuDôâ„’"Â'7G&VÕö–B#¢CbÀ¢&6FVv÷'•ö–B#¢'W2'ÒÀ¢²&æÖR#¢$U3¢$ÄÅ’EbT„B"Â'7G&VÕö–B#¢CrÀ¢&6FVv÷'•ö–B#¢'&6–ær'ÕÒÀ¢F–7B‡6×ÆUö6G2ÂW3Ò%U2Æö6Â"Â&6–æsÒ%&6–ær"’ÂõFW7E&6–æu‡G&VÒ‚’¢w&5ö6ÆÇ6–våö–G2Ò·&÷u²'7G&VÕö–B%Òf÷"&÷r–âw&5ö6ÆÇ6–vå÷&÷w7Ð¢6†V6²‚%u$26ÆÇ6–vâ6öÆÆ—6–öâW†6ÇVFW2v6†–æwFöâä$2ff–Æ–FW2"À¢w&5ö6ÆÇ6–våö–G2ÓÒ³CwÒ¢–æG•÷&÷w2Òf–æE÷&6–æuö6†ææVÇ2€¢²'6W&–W2#¢&–æG–6""Â'&6R#¢$g&VVFöÒ#Sw&æB&—‚öbv6†–æwFöâ"À¢&6—&7V—B#¢%7G&VWG2öbv6†–æwFöâ"Â'6W76–öâ#¢%&6R'ÒÀ¢·²&æÖR#¢$”äE”4"e$TTDôÒ#S"Â'7G&VÕö–B#¢SÀ¢&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$äó¢b5õ%B„B"Â'7G&VÕö–B#¢SÂ&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b5õ%B$TÔ”U"ÄTuTR"Â'7G&VÕö–B#¢S"À¢&6FVv÷'•ö–B#¢&æò'ÒÀ¢²&æÖR#¢$äó¢b5õ%BtôÄb"Â'7G&VÕö–B#¢S2Â&6FVv÷'•ö–B#¢&æò'ÕÒÀ¢6×ÆUö6G2ÂõFW7E&6–æu‡G&VÒ‚’¢–æG•ö¶–æG2Ò·&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚&ÖF6…ö¶–æB"’f÷"&÷r–â–æG•÷&÷w7Ð¢6†V6²‚$–æG”6"¶VW2vVæW&–2b7÷'B÷76–&ÆRæB&V¦V7G27V6–Æ—7BfVVG2"À¢–æG•ö¶–æG2ævWBƒS’ÓÒ&WfVçB"æB–æG•ö¶–æG2ævWBƒS’ÓÒ'÷76–&ÆR"æ@¢S"æ÷B–â–æG•ö¶–æG2æBS2æ÷B–â–æG•ö¶–æG2¢c%÷&÷w2Òf–æE÷&6–æuö6†ææVÇ2€¢²'6W&–W2#¢&c""Â'&6R#¢$Ööç¦"Â&6—&7V—B#¢$Ööç¦"À¢'6W76–öâ#¢$fVGW&R&6R'ÒÀ¢·²&æÖR#¢%T³¢4U$”RÒÔôå¤D²"Â'7G&VÕö–B#¢cÀ¢&6FVv÷'•ö–B#¢#F²'ÒÀ¢²&æÖR#¢$c"Ôôå¤dTEU$R$4R"Â'7G&VÕö–B#¢cÀ¢&6FVv÷'•ö–B#¢'b'ÒÀ¢²&æÖR#¢$f÷&×VÆ""Â'7G&VÕö–B#¢c"Â&6FVv÷'•ö–B#¢'&6–ær'ÒÀ¢²&æÖR#¢$cEb"Â'7G&VÕö–B#¢c2Â&6FVv÷'•ö–B#¢'&6–ær'ÒÀ¢²&æÖR#¢$äó¢d”Ä’bB"Â'7G&VÕö–B#¢cBÀ¢&6FVv÷'•ö–B#¢&æò'ÕÒÀ¢F–7B‡6×ÆUö6G2Â&6–æsÒ%&6–ær"’ÂõFW7E&6–æu‡G&VÒ‚’¢c%ö¶–æG2Ò·&÷u²'7G&VÕö–B%Ó¢&÷rævWB‚&ÖF6…ö¶–æB"’f÷"&÷r–âc%÷&÷w7Ð¢6†V6²‚$c"&WV—&W26W&–W2ÇW2fVçVRæB&WF–ç2ÆFf÷&ÒfÆÆ&6·2"À¢cæ÷B–âc%ö¶–æG2æBc%ö¶–æG2ævWBƒc’ÓÒ&WfVçB"æ@¢c%ö¶–æG2ævWBƒc"’ÓÒ'6W&–W2"æBc%ö¶–æG2ævWBƒc2’ÓÒ&'&öF67FW""æ@¢c%ö¶–æG2ævWBƒcB’ÓÒ'÷76–&ÆR"¢6†V6²‚'&6–ærT’6W&FW26öæf—&ÖVB'&öF67FW'2g&öÒFVF–6FVB6W&–W2"À¢&6‚æÖF6…ö¶–æCÓÓÒvWfVçBwÇÆ6‚æÖF6…ö¶–æCÓÓÒv'&öF67FW"r"–âtRæ@¢$6öæf—&ÖVB&6–ær6†ææVÇ2"–âtR¢6†V6²‚$c"æBc2vVV¶VæG2&WF–âF†V—"f–æÂ6ÆVæF"F’"À¢÷&6–æu÷vVV¶VæEöVæB‚#BÒb4U"Â##b’ç7F'G7v—F‚‚###bÓ’ÓeC#3£S“£S’"’¢6†V6²‚'&öf–ÆR&6–ær&÷w2¶VWWfVçBFWF–Ç2–âöæRÆ–væVB6öÇVÖâ"À¢&×–F6‡7÷'FWfVçG2"–âtRæB&×–F6‡7÷'FWfVçFÖWF"–âtR¢6†V6²‚'&6–ærÆ&VÇ2æ÷&ÖÆ—¦R6ö×7BvVV¶VæB6÷W&6RfÇVW2"À¢'&6–æu6W76–öäÆ&VÂ†WfVçB’"–âtRæ@¢"w&ÆÇ—vVV¶VæBs¢u&ÆÇ’vVV¶VæBr"–âtR¢6†V6²‚&ÆÂÖF’&6–ærW6W26ÆVæF"ÖF’Æ—fRv–æF÷w2"À¢&æ÷tF“ã×7F'DF’bfæ÷tF“ÃÖVæDF’"–âtR¢6†V6²‚&×VÇF’ÖF’&6–ær&VÖ–ç2f—6–&ÆRF‡&÷Vv†÷WBÆ—fRvVV¶VæB"À¢"‡‚æÆ—fWÇÇ‚çG3ãÖæ÷r’"–âtRæ@¢"‚Æ—fRbgG3Ææ÷rÓ#B£3c’"–âtR¢6†V6²‚&GWÆ–6FR'&÷w6W"&WVW7G26†&RöæR–âÖfÆ–v‡B÷W&F–öâ"À¢%ö”–æfÆ–v‡Bæ†2†¶W’’"–âtRæB%ö6öÆW66VE÷7G2"–âtR¢6†V6²‚&6öÆB‡G&VÒ6FÆöwVR&WVW7G2&R6÷W&6RÖÆö6¶VB"À¢ÆÂ†Æö6²–â6÷W&6U÷FW‡Bf÷"Æö6²–à¢‚%õ…Eô4„ääTÅ5ôÄô4²"Â%õ…EôÔõd”U5ôÄô4²"Â%õ…Eõ4U$”U5ôÄô4²"’’¢WfVçEö–G2Ò·&÷u²'7G&VÕö–B%Òf÷"&÷r–âf–æE÷FVÕö6†ææVÇ2€¢²$'&æâ"Â$†Ô¶Ò%ÒÂ6×ÆUö6†ææVÇ2Â6×ÆUö6G2ÂõFW7E‡G&VÒ‚’—Ð¢6†V6²‚&&÷F‚f—‡GW&RFV×2&æ²"Â2–âWfVçEö–G2¢6†V6²‚&öæR×FVÒWfVçB&WF–æVB2÷76–&ÆR"Â"–âWfVçEö–G2¢6†V6²‚'&W6W'fRFVÒW†6ÇVFVB"ÂBæ÷B–âWfVçEö–G2¢&æ¶VBÒ&æµöf—‡GW&Uö6†ææVÇ2…°¢²'‡G&VÕöæÖR#¢%duEbb"Â'7G&VÕö–B#¢Â'66÷&R#¢ã“gÒÀ¢²'‡G&VÕöæÖR#¢$ôÄÄôâÄ”Ô54ôÂÒ%$äâÂduEbb2"À¢'7G&VÕö–B#¢Â'66÷&R#¢ãƒÒÀ¢²'‡G&VÕöæÖR#¢$ôÄÄôâÄ”Ô54ôÂÒ%$äâÂduEbbB"À¢'7G&VÕö–B#¢"Â'66÷&R#¢ã“—ÕÒÂ$'&æâ"Â$†Ô¶Ò"¢2&R×&æ²F†R6ÖR&÷w2f÷"F†RW†7BöÆÆöâf—‡GW&R6W&FVÇ’à¢W†7E÷&æ¶VBÒ&æµöf—‡GW&Uö6†ææVÇ2…°¢²'‡G&VÕöæÖR#¢%duEbb"Â'7G&VÕö–B#¢Â'66÷&R#¢ã“gÒÀ¢²'‡G&VÕöæÖR#¢$ôÄÄôâÄ”Ô54ôÂÒ%$äâÂduEbb2"À¢'7G&VÕö–B#¢Â'66÷&R#¢ãƒÕÒÂ$öÆÆöâÆ–Ö76öÂ"Â$'&æâ"¢6†V6²‚&W†7Bf—‡GW&R6÷'FVBf—'7B"ÂW†7E÷&æ¶VE³Õ²'7G&VÕö–B%ÒÓÒ¢&öFõ÷&æ¶VBÒ&æµöf—‡GW&Uö6†ææVÇ2…°¢²'‡G&VÕöæÖR#¢%6ö66W#¢&öFôvÆ–×Bg2äT2Vr#R#£ÂEc%Æ’äò#2"À¢'7G&VÕö–B#¢2Â'66÷&R#¢ã“gÕÒÂ$&öL;‚ôvÆ–×B"Â$äT2"¢6†V6²‚$&öL;‚vÆ–×B6ö×7B6†ææVÂF—FÆR—2âW†7Bf—‡GW&R"À¢&öFõ÷&æ¶VE³Õ²&f—‡GW&UöÖF6‚%ÒÓÒ&W†7B"æ@¢öf–ÇFW%÷7G&VÖ–æu÷ÆFf÷&Õ÷6Æ÷G2…°¢F–7B†&öFõ÷&æ¶VE³ÒÂÖF6†VCÒ%Eb"Æ’„äò’"•Ò’¢6†V6²‚$&öL;‚vÆ–×B6ö×7BUrF—FÆR6öæf—&×2&÷F‚FV×2"À¢öf—‡GW&U÷F—FÆUö†5ö&÷F…÷FV×2€¢%6ö66W#¢&öFôvÆ–×Bg2äT2"Â$&öL;‚ôvÆ–×B"Â$äT2"’¢6†V6²‚&öæR×FVÒbF—FÆR—2&öÖ÷FVB'WB&VÖ–ç2÷76–&ÆR"À¢&æ¶VE³Õ²&f—‡GW&UöÖF6‚%ÒÓÒ''F–Â"¢6†V6²‚&vVæW&–2b6æF–FFR&VÖ–ç2fÆÆ&6²"À¢&æ¶VE²ÓÕ²&f—‡GW&UöÖF6‚%ÒÓÒ&vVæW&–2"¢6†V6²‚&VÖ&VFFVBvRfW'6–öâ"Â'b"²dU%4”ôâ–âtRç&WÆ6R‚%õõdU%4”ôåõò"ÂdU%4”ôâ’¢6†V6²‚'&6–ærf—‡GW&W2W‡÷6R6†ææVÂÖF6†–ærv—F†÷WB÷Væ–ær6÷W&6RvW2"À¢'&6–ævWfVçG7–ææW""–âtRæ@¢%÷&6–ætf–Æ&–Æ—G”ÆöF–æs×G'VR"–âtRæ@¢'&6–ætWfVçBæ6Æ74Æ—7Bæ6öçF–ç2‚vÆöF–æv6†ææVÇ2r’"–âtRæ@¢&6öç7BW&Ã×&6–ætWfVçBævWDGG&–'WFR‚vFF×W&Âr’"æ÷B–âtRæ@¢'&6–ævWfVçG6÷W&6R"–âtR¢6†V6²‚$cF–ÖVÆ–æRæB66†VGVÆR&WF–âF†R6ö×ÆWFRæV&W7B&6RvVV¶VæB"À¢&gVæ7F–öâ&6–æuf—6–&ÆU6W&–W4WfVçG2"–âtRæ@¢'&WGW&â‡vVV¶VæBæÆVæwFƒ÷vVV¶VæC§&÷w2’ç6Æ–6RƒÃb’"–âtRæ@¢'&6–æuf—6–&ÆU6W&–W4WfVçG2†w&÷W2ævWB‡&÷u³Ò—ÇÅµÒÇ&÷u³ÒÃB’"–âtRæ@¢'&6–æuf—6–&ÆU6W&–W4WfVçG2‡&÷w2ç6÷'B‚†Æ"“ÓæçG2Ö"çG2’Ç6W&–W2Ã2’"–âtR¢6†V6²‚'&W7F'B&WV—&W2FWbÖöFRæBv—G2f÷"æWr&ö6W72–ç7Fæ6R"À¢v–bæ÷B&ööÂ†ÆöEö6öæf–r‚’ævWB‚&FWeöÖöFR"’’r–â6÷W&6U÷FW‡Bæ@¢r&–ç7Fæ6R#¢õ4U%dU%ô”å5Dä4Uô”Br–â6÷W&6U÷FW‡Bæ@¢%7G&–ær‡–æræ–ç7Fæ6R’ÓÖöÆD–ç7Fæ6R"–âtR¢6†V6²‚%v–æF÷w2WFFW2†VÇF‚Ö6†V6²æBWFöÖF–6ÆÇ’&W7F÷&R&6·W"À¢%v—F–ærf÷"õEdÒb"–â6÷W&6U÷FW‡Bæ@¢$æWrfW'6–öâf–ÆVB—G27F'GW†VÇF‚6†V6²â&öÆÆ–ær&6²â"–â6÷W&6U÷FW‡Bæ@¢v6÷’÷’Â"r–â6÷W&6U÷FW‡BæB"æ&6·W"–â6÷W&6U÷FW‡Bæ@¢'WFFR×&öÆÆ&6²çG‡B"–â6÷W&6U÷FW‡B¢6†V6²‚&'&÷w6W"W‡Æ–ç2fW&–f–6F–öâÂ7F'GWFW7F–ærÂæB&öÆÆ&6²"À¢&F÷væÆöFVBæBfW&–f–VB"–âtRæ@¢&WFöÖF–6ÆÇ’&W7F÷&RF†R&6·W–b7F'GWf–Ç2"–âtRæ@¢'&W7F÷&VBb"–âtRæB"ö’÷WFFU÷7FGW2"–âtR¢6†V6²‚&f–ÆVBWFFRfW'6–öâ—2V&çF–æVBVçF–ÂæWvW"&VÆV6R"À¢'WFFR×&V¦V7FVBçG‡B"–â6÷W&6U÷FW‡Bæ@¢'&VÖ÷FRÓÒ÷&V¦V7FVE÷WFFU÷fW'6–öâ‚’"–â6÷W&6U÷FW‡Bæ@¢'6¶—VEö&E÷fW'6–öâ"–â6÷W&6U÷FW‡Bæ@¢'v–ÆÂ6¶—F†R&BWFFRVçF–ÂæWvW"&VÆV6R—2f–Æ&ÆR"–â6÷W&6U÷FW‡B¢6†V6²‚&ÖævVBWFFRf–ÇW&W27F’–â'&÷w6W"v—F‚öæRF—6Ö—727F–öâ"À¢'WFFRÖ–â×&öw&W72çG‡B"–â6÷W&6U÷FW‡Bæ@¢&æBæ÷BÖævVE÷WFFR"–â6÷W&6U÷FW‡Bæ@¢v–CÒ'WFFTÆFW$'Fâ"r–âtRæ@¢&vWDVÆVÖVçD'”–B‚wWFFTæ÷t'Fâr’æ6Æ74Æ—7BæFB‚v†–FRr’"–âtR¢6†V6²‚&Ö÷f–Rff÷&—FRFööÇF—föÆÆ÷w27W'&VçB7FFR"À¢'G"‚u&VÖ÷fRg&öÒff÷&—FW2r’"–âtRæ@¢'7F$VÂçF—FÆS×G"‚"–âtR¢6†V6²‚&Æ—fRfÆÆ&6²—2&÷VæFVBæB&V6VçB&W7VÇG2&VÖ–âf—6–&ÆR"À¢&–b†bæ—5öÆ—fR—&WGW&âÖ–ç3ÃÓS"–âtRæ@¢&Ö–ç3ÃÓ3cbbf—‡GW&T—4Æ—fR†b’"–âtRæ@¢&–b‚ö×•FVÔf—‡GW&W2æÆVæwF‚—W6öÖ–æræ–ææW$…DÔÂ"–âtR¢&WGW&â6†V6·0 ¦–bõöæÖUõòÓÒ%õöÖ–åõò# ¢–b"Ò×6VÆb×FW7B"–â7—2æ&wc ¢76VBÒ'Vå÷6VÆe÷FW7G2‚¢&–çB‚%6VÆb×FW7B76VC¢"²"Â"æ¦ö–â‡76VB’¢VÇ6S ¢G'“ ¢Ö–â‚¢W†6WBW†6WF–öã ¢2F†Ræ÷&ÖÂÆVæ6†W"—26öç6öÆRÖÆW72â&W6W'fR7F'GWf–ÇW&W2–à¢2F†RFFföÆFW"6ò&BæWGv÷&²÷÷'B7FFR—2æWfW"–çf—6–&ÆRà¢G'“ ¢–×÷'BG&6V&6°¢W'&÷%÷F‚Ò÷2çF‚æ¦ö–â†öF—"‚’Â'7F'GWÖW'&÷"çG‡B"¢öFöÖ–5÷w&—FUö'—FW2†W'&÷%÷F‚ÂG&6V&6²æf÷&ÖEöW†2‚’æVæ6öFR‚'WFbÓ‚"’¢ÖævVE÷WFFRÒ÷2çF‚æW†—7G2†÷2çF‚æ¦ö–â†öF—"‚’Â'WFFRÖ–â×&öw&W72çG‡B"’¢–b7—2çÆFf÷&Òç7F'G7v—F‚‚'v–â"’æBæ÷BÖævVE÷WFFS ¢÷2ç7F'Ff–ÆR†W'&÷%÷F‚’2G—S¢–væ÷&U¶GG"ÖFVf–æVEÐ¢W†6WBW†6WF–öã ¢70¢&—6P