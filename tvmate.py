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

# --- versioning & auto-update ---
VERSION = "0.777.b55"

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

DEFAULT_CONFIG = {
    "xtream_host": "",
    "xtream_port": "",
    "xtream_user": "",
    "xtream_pass": "",
    "stream_ext": "ts",               # "ts" or "m3u8"
    "match_threshold": 0.62,           # 0..1, higher = stricter
    "countries": ["no", "gb", "us", "es", "de", "it", "fr"],  # NO/UK/US + big-5 league homes
    "check_shows_on_startup": False,
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

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg or {})
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

FAVORITES_PATH = os.path.join(app_dir(), "favorites.json")

def load_favorites():
    if not os.path.exists(FAVORITES_PATH):
        return {"categories": [], "channels": [], "movies": [], "shows": []}
    try:
        with open(FAVORITES_PATH, "r", encoding="utf-8") as f:
            fav = json.load(f) or {}
        return {"categories": list(fav.get("categories", [])),
                "channels": list(fav.get("channels", [])),
                "movies": list(fav.get("movies", [])),
                "shows": list(fav.get("shows", []))}
    except Exception:
        return {"categories": [], "channels": [], "movies": [], "shows": []}

def save_favorites(fav):
    with open(FAVORITES_PATH, "w", encoding="utf-8") as f:
        json.dump(fav, f, indent=2)

# --------------------------------------------------------------------------
# HTTP helpers (stdlib only, read as UTF-8)
# --------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

def http_get_text(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

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
                        "category_id": str(s.get("category_id", ""))})
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
        q = {"username": self.user, "password": self.password,
             "action": "get_series_info", "series_id": str(series_id)}
        if refresh:
            q["_"] = str(int(time.time()))
        return http_get_json(f"{self.base}/player_api.php?" + urllib.parse.urlencode(q), timeout=45)

    def episode_url(self, episode_id, extension="mp4"):
        ext = re.sub(r"[^a-zA-Z0-9]", "", str(extension or "mp4")) or "mp4"
        return f"{self.base}/series/{self.user}/{self.password}/{episode_id}.{ext}"

    def stream_url(self, stream_id):
        return f"{self.base}/live/{self.user}/{self.password}/{stream_id}.{self.ext}"

    def hls_url(self, stream_id):
        return f"{self.base}/live/{self.user}/{self.password}/{stream_id}.m3u8"

    def short_epg(self, stream_id, limit=6):
        """Fetch short EPG for one stream. Titles are base64 in Xtream."""
        import base64, calendar, datetime
        q = {"username": self.user, "password": self.password,
             "action": "get_short_epg", "stream_id": str(stream_id), "limit": str(limit)}
        url = f"{self.base}/player_api.php?" + urllib.parse.urlencode(q)
        try:
            data = http_get_json(url)
        except Exception:
            return []
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

_XT_CACHE = {"ts": 0, "channels": [], "cats": {}}
_VOD_CACHE = {"ts": 0, "movies": []}
_SERIES_CACHE = {"ts": 0, "shows": []}
_TVMAZE_CACHE = {}  # normalized title/year -> {"ts": epoch, "covers": {season:url}}
_XT_TTL = 600
_EPG_CACHE = {}   # stream_id -> {"ts": epoch, "programmes": [...]}
_EPG_TTL = 3600

def _fetch_text(url, timeout=8):
    """Fetch a URL as text, or None on any failure (offline, 404, etc.)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OlosTVMate-Updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None

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
    remote = _fetch_text(UPDATE_VERSION_URL)
    if not remote:
        return (False, None)
    remote = remote.strip().splitlines()[0].strip() if remote.strip() else ""
    if not remote:
        return (False, None)
    try:
        newer = _parse_ver(remote) > _parse_ver(VERSION)
    except Exception:
        newer = (remote != VERSION)
    return (newer, remote)

def download_update():
    """Download the new tvmate.py to a temp file next to the current script. Return path or None."""
    text = _fetch_text(UPDATE_SCRIPT_URL, timeout=30)
    if not text or "def main(" not in text and "PORT" not in text:
        return None
    try:
        # Normalize line endings: strip any CR so we don't end up with \r\r\n
        # (doubled carriage returns) which makes the Windows console double-space
        # / stretch the ASCII banner. Write with newline="" so Python doesn't
        # translate again.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        dest = os.path.join(app_dir(), "tvmate_new.py")
        with open(dest, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return dest
    except Exception:
        return None

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

def get_xtream_channels(cfg, force=False):
    now = time.time()
    if (not force) and _XT_CACHE["channels"] and (now - _XT_CACHE["ts"] < _XT_TTL):
        return _XT_CACHE["channels"], _XT_CACHE["cats"]
    x = Xtream(cfg)
    channels = x.live_streams()
    try:
        cats = x.categories()
    except Exception:
        cats = {}
    _XT_CACHE.update({"ts": now, "channels": channels, "cats": cats})
    return channels, cats

def get_xtream_movies(cfg, force=False):
    now = time.time()
    if (not force) and _VOD_CACHE["movies"] and (now - _VOD_CACHE["ts"] < _XT_TTL):
        return _VOD_CACHE["movies"]
    movies = Xtream(cfg).vod_streams()
    _VOD_CACHE.update({"ts": now, "movies": movies})
    return movies

def get_xtream_series(cfg, force=False):
    now = time.time()
    if (not force) and _SERIES_CACHE["shows"] and (now - _SERIES_CACHE["ts"] < _XT_TTL):
        return _SERIES_CACHE["shows"]
    shows = Xtream(cfg).series()
    _SERIES_CACHE.update({"ts": now, "shows": shows})
    return shows

def refresh_favorite_show_episodes(cfg):
    """Refresh favorite-show episode counts and report newly added episodes."""
    x = Xtream(cfg)
    if not x.configured():
        raise ValueError("Not configured")
    fav = load_favorites()
    new_episodes = 0
    refreshed = 0
    errors = 0
    for show in fav.get("shows", []):
        series_id = show.get("series_id")
        if series_id is None:
            continue
        try:
            data = x.series_info(series_id, refresh=True) or {}
            raw_eps = data.get("episodes") or {}
            if isinstance(raw_eps, dict):
                count = sum(len(eps) for eps in raw_eps.values()
                            if isinstance(eps, list))
            elif isinstance(raw_eps, list):
                count = len(raw_eps)
            else:
                count = 0
            previous = show.get("episode_count")
            if previous is not None and count > int(previous):
                new_episodes += count - int(previous)
            show["episode_count"] = count
            show["episodes_checked_at"] = int(time.time())
            refreshed += 1
        except Exception:
            errors += 1
    save_favorites(fav)
    if errors and not refreshed:
        raise RuntimeError("Could not refresh favorite shows")
    return {"new_episodes": new_episodes, "refreshed": refreshed,
            "errors": errors}

def _clean_show_title(name):
    title = str(name or "").strip()
    # Provider prefixes: "EN -", "A+ -", "4K-A+ -", etc.
    title = re.sub(r"^[A-Z0-9+]+(?:-[A-Z0-9+]+)*\s+-\s+", "", title, flags=re.I)
    title = re.sub(r"^[A-Z]{2,5}\s*[|:]\s*", "", title, flags=re.I)
    title = re.sub(r"\s*\((?:19|20)\d{2}\).*?$", "", title)
    title = re.sub(r"\s*\((?:US|UK|GB|NO|EN|SE|DK|FI)\)\s*$", "", title, flags=re.I)
    return title.strip(" -|")

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
_TV_TTL = 900           # 15 min

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
                "home_slug": _slug_name(home_obj.get("url") or ""),
                "away_slug": _slug_name(away_obj.get("url") or ""),
                "start": ev.get("startDate", "") or "",
                "channels": _channels_from_event(ev),
                "country": disp,
                "match_url": ev.get("url") or ev.get("@id") or "",
            })
    _TV_CACHE[country] = {"ts": now, "fixtures": fixtures}
    return fixtures

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

def _is_streaming(name):
    n = (name or "").lower()
    return any(h in n for h in _STREAMING_HINTS)

def _is_ppv_category(catname):
    c = (catname or "").lower()
    return ("ppv" in c) or ("play" in c)

# Known country codes that may appear as a channel prefix. If a channel's
# prefix is one of these AND it isn't the broadcast's country, the channel is
# the wrong country and must be rejected. Provider tiers (GOLD/SPO/VIP/...)
# are NOT in this set, so they pass through.
_COUNTRY_CODES = {
    "no", "uk", "gb", "us", "usa", "dk", "se", "fi", "de", "at", "nl", "fr",
    "it", "es", "pt", "ie", "be", "ch", "pl", "cz", "sk", "hu", "ro", "bg",
    "gr", "hr", "si", "rs", "ba", "bh", "mk", "al", "tr", "ru", "ua", "ar",
    "sa", "ir", "in", "pk", "ca", "au", "br", "mx", "asia", "afr", "ex",
    "yu", "ex-yu", "lt", "lv", "ee", "is", "lu", "mt", "cy",
}
_CC_PREFIX_RE = re.compile(r"^\s*([a-z]{2,4})\s*[:|\-]", re.I)

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

def _channel_cc(name):
    """Country code from a channel NAME prefix (or None)."""
    return _cc_from_prefix(name)

def _resolve_channel_country(name, category):
    """Determine a channel's country. Prefer the CATEGORY prefix (e.g.
    'NO| NORWAY HD/RAW') since providers group channels by country there and
    it's far more consistent than the name prefix. Fall back to the name
    prefix ('NO:'), else None (unknown -> not country-filtered)."""
    return _cc_from_prefix(category) or _cc_from_prefix(name)

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
        xn = normalise(cname)
        if not xn:
            continue
        xset = set(xn.split())
        category = cats.get(ch["category_id"], "")
        ch_cc = _resolve_channel_country(cname, category)  # category first, then name
        best, best_src, best_country = 0.0, "", ""
        for orig, bcountry, allowed, sn, sset in srcs:
            # Country rule: if the channel HAS a recognised country prefix and
            # it isn't in this broadcaster's allowed set -> skip (wrong country).
            if ch_cc is not None and ch_cc not in allowed:
                continue
            if _numbers_conflict(xn, sn):
                continue
            inter = xset & sset
            if not inter:
                continue
            cover_b = len(inter) / len(sset)
            cover_c = len(inter) / max(1, len(xset))
            s = _score(xn, sn)
            if sset <= xset:
                s = max(s, 0.8 + 0.2 * cover_c)
            else:
                s = max(s, cover_b * cover_c)
            if s > best:
                best, best_src, best_country = s, orig, bcountry
        best = round(max(0.0, min(1.0, best)), 3)
        if best >= threshold:
            rows.append({"xtream_name": cname, "stream_id": ch["stream_id"],
                         "category": category,
                         "quality": quality_tag(cname),
                         "matched": best_src, "country": best_country,
                         "score": best})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows

def find_team_channels(team_terms, xtream_channels, cats, x):
    """Find channels whose NAME contains a fixture team name (for PPV/event
    channels named after the teams). Returns list of channel dicts w/ url."""
    terms = [t.lower() for t in team_terms if t and len(t) >= 3]
    out = []
    for ch in xtream_channels:
        low = ch["name"].lower()
        if any(t in low for t in terms):
            out.append({
                "xtream_name": ch["name"], "stream_id": ch["stream_id"],
                "category": cats.get(ch["category_id"], ""),
                "quality": quality_tag(ch["name"]),
                "url": x.stream_url(ch["stream_id"]),
            })
    return out

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
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 240 240'%3E%3Crect x='26' y='58' width='150' height='120' rx='16' fill='%233a2c1f' stroke='%23241a12' stroke-width='4'/%3E%3Crect x='38' y='70' width='126' height='96' rx='8' fill='%231b3a6b'/%3E%3Cellipse cx='101' cy='140' rx='44' ry='11' fill='%23e7a94e'/%3E%3Cellipse cx='101' cy='128' rx='42' ry='11' fill='%23f0b95e'/%3E%3Cellipse cx='101' cy='116' rx='40' ry='11' fill='%23f5c56e'/%3E%3Crect x='86' y='86' width='30' height='14' rx='5' fill='%23ffd77a'/%3E%3Ccircle cx='192' cy='86' r='8' fill='%232a2a2a'/%3E%3Ccircle cx='192' cy='112' r='8' fill='%232a2a2a'/%3E%3Crect x='150' y='40' width='4' height='24' fill='%23241a12'/%3E%3Crect x='118' y='40' width='4' height='24' fill='%23241a12' transform='rotate(-28 120 52)'/%3E%3C/svg%3E">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0f1115;--card:#181b22;--card2:#1e222b;--fg:#e6e8ee;--mut:#8a90a0;--acc:#4f8cff;--line:#262a34;--line2:#313747}
 *{box-sizing:border-box}
 body{margin:0;font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
 header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:center;position:relative}
 .slogan{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-size:13px;font-style:italic;color:var(--mut);white-space:nowrap;letter-spacing:.2px;pointer-events:none}
 #status{margin-left:auto}
 header h1{font-size:16px;margin:0;font-weight:600}
 header a{color:var(--mut);text-decoration:none;font-size:14px;cursor:pointer;padding:2px 0;border-bottom:2px solid transparent}
 header a:hover{color:var(--fg)}
 header a.on{color:var(--fg);border-bottom-color:var(--acc)}
 .langsel{display:flex;gap:6px;margin-left:14px}
 .langflag{background:none;border:1px solid transparent;border-radius:6px;padding:2px 6px;font-size:17px;line-height:1;cursor:pointer;opacity:.45;filter:grayscale(.5);transition:all .12s}
 .langflag:hover{opacity:.85;filter:none}
 .langflag.on{opacity:1;filter:none;border-color:var(--line2);background:var(--card2)}
 .updatebanner{position:fixed;top:0;left:0;right:0;background:#16233d;border-bottom:1px solid var(--acc);padding:10px 18px;display:flex;align-items:center;gap:12px;justify-content:center;z-index:300;font-size:14px;box-shadow:0 4px 14px rgba(0,0,0,.4)}
 .updatebanner button{font-size:13px;padding:5px 14px}
 .pmodal{position:fixed;inset:0;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;z-index:400}
 .pmodal.hide{display:none}
 .pbox{background:#0c0e12;border:1px solid var(--line);border-radius:12px;width:min(880px,92vw);overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5)}
 .teamtabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
 .teamtab{background:var(--card);border:1px solid var(--line);color:var(--mut);padding:8px 16px;border-radius:8px;cursor:pointer;font-size:14px}
 .teamtab.on{background:var(--acc);border-color:var(--acc);color:#08131f;font-weight:600}
 .teamtab:hover{filter:brightness(1.1)}
 .bcastlist{margin-top:10px;display:flex;flex-direction:column;gap:6px}
 .bcrow{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--card)}
 .bchead{padding:9px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;font-size:14px;user-select:none}
 .bchead:hover{background:var(--card2)}
 .bcrow.open .bchead{border-bottom:1px solid var(--line);background:var(--card2)}
 .bcname{font-weight:500;color:var(--fg)}
 .exphint{margin-left:auto;font-size:12px}
 .bcchans{display:flex;flex-direction:column}
 .chline{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-top:1px solid var(--line)}
 .chline:first-child{border-top:0}
 .matchchan{display:flex;align-items:center;min-width:0;flex:1}
 .matchchan .favstar{display:inline-block;color:#78808e;margin-right:9px}
 .matchchan .favstar.on{color:#f5c542}
 .chn{font-size:13px}
 .chbtns{display:flex;gap:6px;flex-shrink:0}
 .pbar{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--line);font-size:14px;font-weight:500}
 .pclose{background:none;border:0;color:var(--mut);font-size:24px;line-height:1;cursor:pointer;padding:0 4px}
 .pclose:hover{color:var(--fg);filter:none}
 #pVideo{width:100%;max-height:70vh;background:#000;display:block}
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
 /* My TV */
 .tvwrap{display:flex;align-items:stretch;border:1px solid var(--line);border-radius:10px;overflow:hidden;height:82vh}
 .tvrail{width:150px;flex-shrink:0;border-right:1px solid var(--line);overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:6px;background:var(--card)}
 .tvsrc{padding:8px 10px;font-size:12px;border:1px solid var(--line2);border-radius:7px;cursor:pointer;text-align:center;color:var(--mut);background:none;transition:all .1s}
 .tvsrc:hover{border-color:var(--acc);color:var(--fg)}
 .tvsrc.on{border-color:var(--acc);background:#16233d;color:#cfe0ff}
 .tvguide{flex:1;min-width:0;display:flex;flex-direction:column;overflow:hidden;position:relative}
 .tvguidehead{display:flex;flex-shrink:0;border-bottom:1px solid var(--line);background:var(--card);height:46px}
 .tvchancol{width:270px;flex-shrink:0;border-right:1px solid var(--line);padding:6px 8px;display:flex;align-items:center}
 .tvchancol button{width:100%}
 .tvtimeline{flex:1;display:flex;overflow:hidden}
 .tvtimeslot{flex:1;min-width:150px;border-right:1px solid var(--line);padding:0 12px;display:flex;align-items:center;font-size:12px;color:var(--mut)}
 .tvplayerslot{position:absolute;top:0;right:0;left:270px;bottom:0;background:#000;z-index:5;display:none}
 .tvplayerslot.on{display:block}
 .tvguidebody{flex:1;overflow-y:auto;position:relative}
 .tvchan{width:270px;flex-shrink:0;border-right:1px solid var(--line);display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:pointer;font-size:12.5px;transition:background .1s}
 .tvchan:hover{background:var(--card2)}
 .tvchan.playing{background:#16233d}
 .tvchan .tvvlc{flex-shrink:0;background:#e8701a;border:0;color:#fff;border-radius:5px;padding:3px 7px;font-size:11px;cursor:pointer;order:0}
 .tvchan .tvflag{flex-shrink:0;font-size:15px;width:20px;text-align:center}
 .tvchan .tvname{flex:1;min-width:0;line-height:1.2;word-break:break-word}
 .tvchan .favstar{margin-right:0}
 .tvdrag{display:inline-flex;align-items:center;color:#737b89;cursor:grab;font-size:16px;line-height:1;user-select:none}
 .tvdrag:active{cursor:grabbing}
 .tvprog{flex:1;padding:8px 12px;display:flex;align-items:center;flex-wrap:wrap;gap:2px;font-size:12px;color:var(--mut);min-width:0;overflow:hidden}
 .epgnone{font-size:12px}
 .epgprog{white-space:nowrap;color:var(--mut)}
 .epgprog.live{color:var(--fg);font-weight:500}
 .epgprog .epgt{color:var(--acc);font-size:11px;margin-right:2px}
 .epgsep{color:var(--line2);margin:0 7px}
 .tvrow{display:flex;border-bottom:1px solid var(--line);min-height:50px;align-items:stretch}
 .tvrow.tvdragging{opacity:.4}
 .tvrow.tvdragover{box-shadow:inset 0 2px 0 var(--acc)}
 /* player fills the timeline area when active */
 #tvPlayerSlot .tvplayerbar{display:flex;align-items:center;justify-content:space-between;padding:6px 12px;background:#0c0e12;font-size:13px}
 #tvVideo{width:100%;height:calc(100% - 34px);background:#000;display:block;object-fit:contain}
 .favcat .chname{flex:1;min-width:0}
 .favcat .chev{color:var(--acc);font-size:12px;flex-shrink:0}
 main{max-width:960px;margin:0 auto;padding:22px}
 main.wide{max-width:none;padding:22px 30px}
 input[type=checkbox]{accent-color:var(--acc);width:16px;height:16px;cursor:pointer}
 .row{display:flex;gap:8px}
 input,select,button{font:inherit}
 input[type=text],input[type=password],select{background:var(--bg);border:1px solid var(--line2);color:var(--fg);border-radius:8px;padding:9px 12px}
 input[type=text]:focus,input[type=password]:focus{outline:none;border-color:var(--acc)}
 input[type=text]{flex:1}
 button{background:var(--acc);border:0;color:#fff;border-radius:8px;padding:9px 15px;cursor:pointer;font-weight:500}
 button:hover{filter:brightness(1.08)}
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
 .colh{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 10px;font-weight:600}
 @media(max-width:760px){.split{flex-direction:column}}
 .chname{flex:1;min-width:0;font-size:13.5px;word-break:break-word;line-height:1.35}
 .ch4{display:flex;gap:0;align-items:stretch;flex-wrap:nowrap}
 .ch4group{position:relative;display:flex;align-items:stretch;border:1px solid var(--line);border-radius:12px;padding:0;margin-left:20px;align-self:stretch;overflow:hidden}
 .ch4col{position:relative;z-index:1;width:250px;flex-shrink:0;display:flex;flex-direction:column;padding:14px 16px}
 .ch4col+.ch4col{border-left:1px solid var(--line)}
 .ch4group .colh{border-bottom:1px solid var(--line);padding-bottom:10px;display:flex;align-items:center;gap:8px}
 .clrbtn{margin-left:auto;background:none;border:1px solid var(--line2);color:var(--mut);border-radius:6px;padding:2px 9px;font-size:11px;font-weight:400;cursor:pointer;text-transform:none;letter-spacing:0}
 .clrbtn:hover{border-color:#ff7676;color:#ff7676;filter:none}
 .plbtns{margin-top:12px;padding-top:12px;flex-wrap:wrap}
 .ch4cats{flex-shrink:0;align-self:flex-start;display:flex;flex-direction:column;padding:0 20px 0 0}
 .catsearch{width:100%;margin-bottom:10px;background:var(--bg);border:1px solid var(--line2);color:var(--fg);border-radius:8px;padding:8px 11px;font-size:13px}
 .catsearch:focus{outline:none;border-color:var(--acc)}
 #catlist{border:1px solid var(--line);border-radius:9px;padding:12px;background:var(--bg);max-height:74vh;overflow-y:auto;display:grid;grid-template-columns:repeat(4,190px);grid-auto-flow:column;grid-template-rows:repeat(var(--catrows,20),auto);gap:2px 14px;align-content:start}
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
 @media(max-width:900px){.ch4{flex-wrap:wrap}.ch4cats{flex:1 1 100%}.ch4col{width:100%}#catlist{grid-template-columns:repeat(2,1fr)}}
 .footline{border-top:1px solid var(--line);margin:30px calc(50% - 50vw) 0;width:100vw}
 /* floating pancakes on the search page side margins */
 .pancakes{position:fixed;top:70px;bottom:0;width:calc((100vw - 960px)/2);pointer-events:none;overflow:hidden;z-index:0}
 .pancakes.left{left:0}
 .pancakes.right{right:0}
 @media(max-width:1200px){.pancakes{display:none}}
 .pcake{position:absolute;opacity:.5;animation:floaty linear infinite}
 @keyframes floaty{0%{transform:translateY(0) rotate(0deg)}50%{transform:translateY(-22px) rotate(4deg)}100%{transform:translateY(0) rotate(0deg)}}
 #searchView{position:relative;z-index:1}
 /* settings branding block */
 .brandblock{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;width:220px;flex-shrink:0}
 .brandblock .bname{font-size:22px;font-weight:600;color:var(--fg)}
 .brandblock .btag{font-size:13px;color:var(--mut)}
 .settingswrap{display:flex;gap:40px;align-items:center;justify-content:center}
 .settingswrap .card{flex:1;min-width:320px;max-width:640px}
 /* playlist builder logo */
 .pancakes-pl{position:absolute;inset:0;pointer-events:none;overflow:hidden;z-index:0}
 .churl{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:var(--mut);word-break:break-all}
 .movieswrap{display:grid;grid-template-columns:230px minmax(0,1fr);gap:24px;width:100%}
 .moviefavs{padding:0;max-height:82vh;overflow-y:auto}
 .moviefavlist{display:flex;flex-direction:column;gap:8px}
 .moviefav{display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
 .moviefavposter{position:relative;width:74px;height:110px;flex-shrink:0;border-radius:6px;background:#20242c;display:flex;align-items:center;justify-content:center;overflow:hidden;color:#737b89;font-size:24px}
 .moviefavposter img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 .moviefavinfo{min-width:0;flex:1}
 .moviefavname{font-size:12px;line-height:1.3;word-break:break-word;margin-bottom:5px}
 .moviefavbtns{display:flex;gap:5px}
 .moviefavbtns button{padding:2px 6px;font-size:10px}
 .moviesmain{width:100%;max-width:900px;min-width:0;margin:0 auto}
 .moviegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:16px}
 .moviecard{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-height:150px}
 .movieposter{width:92px;height:138px;flex-shrink:0;border-radius:7px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center;color:#737b89;font-size:30px}
 .movieposter img{width:100%;height:100%;object-fit:cover;display:block}
 .movieinfo{display:flex;flex:1;min-width:0;flex-direction:column;gap:9px}
 .movietitle{font-weight:600;line-height:1.3}
 .moviemeta{font-size:12px;color:var(--mut)}
 .movieactions{display:flex;gap:7px;margin-top:auto}
 .showswrap{display:grid;grid-template-columns:230px minmax(0,1fr);gap:24px;width:100%}
 .showrefresh{position:fixed;top:68px;right:18px;z-index:30;font-size:12px;padding:7px 13px}
 .showfavs{max-height:82vh;overflow-y:auto}
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
 .showcard{display:flex;gap:10px;min-height:140px;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--card);cursor:pointer}
 .showcard:hover{border-color:var(--acc)}
 .showposter{position:relative;width:82px;height:123px;flex-shrink:0;border-radius:6px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center;font-size:28px;color:#737b89}
 .showposter img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 .showname{font-weight:600;line-height:1.3}
 .showdetails{margin-top:16px}
 .showhero{display:flex;align-items:flex-end;gap:18px;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--line)}
 .showheroart{position:relative;width:150px;height:225px;flex-shrink:0;border-radius:9px;overflow:hidden;background:#20242c;display:flex;align-items:center;justify-content:center;font-size:38px}
 .showheroart img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 .showhero h2{font-size:28px;margin:0 0 8px;display:flex;align-items:center;gap:10px}
 .showhero h2 .favstar{font-size:22px;margin-right:0}
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
</style></head><body>
<header>
  <h1><svg width="38" height="38" viewBox="0 0 240 240" style="vertical-align:-11px;margin-right:8px" xmlns="http://www.w3.org/2000/svg"><rect x="26" y="58" width="150" height="120" rx="16" fill="#3a2c1f" stroke="#241a12" stroke-width="4"/><rect x="38" y="70" width="126" height="96" rx="8" fill="#1b3a6b"/><ellipse cx="101" cy="140" rx="44" ry="11" fill="#e7a94e"/><ellipse cx="101" cy="139" rx="44" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><ellipse cx="101" cy="128" rx="42" ry="11" fill="#f0b95e"/><ellipse cx="101" cy="127" rx="42" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><ellipse cx="101" cy="116" rx="40" ry="11" fill="#f5c56e"/><ellipse cx="101" cy="115" rx="40" ry="11" fill="none" stroke="#b9762d" stroke-width="2"/><path d="M64 110 q6 12 14 4 q6 12 16 3 q7 12 16 3 q7 11 15 2 q6 10 12 3 l0 6 q-6 6 -12 2 q-8 8 -15 1 q-8 8 -16 1 q-8 8 -16 0 q-8 7 -14 -3 z" fill="#a8541f"/><rect x="86" y="86" width="30" height="14" rx="5" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.5"/><circle cx="192" cy="86" r="8" fill="#2a2a2a"/><circle cx="192" cy="112" r="8" fill="#2a2a2a"/><rect x="186" y="132" width="12" height="30" rx="3" fill="#2a2a2a"/><rect x="52" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="136" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="150" y="40" width="4" height="24" fill="#241a12"/><rect x="118" y="40" width="4" height="24" fill="#241a12" transform="rotate(-28 120 52)"/><circle cx="152" cy="38" r="6" fill="#f5c56e"/><circle cx="116" cy="34" r="6" fill="#f5c56e"/></svg>Olo's TVMate</h1>
  <a id="navSearch" onclick="showSearch()" data-i18n="Search">Search</a>
  <a id="navChannels" onclick="showChannels()" data-i18n="Playlist Builder">Playlist Builder</a>
  <a id="navMytv" onclick="showMytv()" data-i18n="My TV">My TV</a>
  <a id="navMovies" onclick="showMovies()" data-i18n="My Movies">My Movies</a>
  <a id="navShows" onclick="showShows()" data-i18n="My Shows">My Shows</a>
  <a id="navMylist" onclick="showMylist()" data-i18n="My List">My List</a>
  <a id="navSettings" onclick="showSettings()" data-i18n="Settings">Settings</a>
  <span id="slogan" class="slogan"></span>
  <span id="status" class="muted"></span>
  <div class="langsel">
    <button class="langflag on" id="langEN" onclick="setLang('en')" title="English">&#127468;&#127463;</button>
    <button class="langflag" id="langNO" onclick="setLang('no')" title="Norsk">&#127475;&#127476;</button>
  </div>
</header>
<main>
  <section id="searchView">
    <div class="pancakes left" id="pcakeL"></div>
    <div class="pancakes right" id="pcakeR"></div>
    <div class="split">
      <div class="col">
        <h2 class="colh" data-i18n="Matchfinder - Get Live / Next Match">Matchfinder - Get Live / Next Match</h2>
        <div class="row">
          <input id="q" type="text" placeholder="Search a team, e.g. Leeds" data-i18n-ph="Search a team, e.g. Leeds" onkeydown="if(event.key==='Enter')doSearch()">
          <button onclick="doSearch()" data-i18n="Search">Search</button>
        </div>
        <div id="results"></div>
      </div>
      <div class="col">
        <h2 class="colh" data-i18n="Channels">Channels</h2>
        <div class="row">
          <input id="cq" type="text" placeholder="Find a channel, e.g. tv2 play" data-i18n-ph="Find a channel, e.g. tv2 play" onkeydown="if(event.key==='Enter')doChannelSearch('cq','cresults')">
          <button onclick="doChannelSearch('cq','cresults')" data-i18n="Search">Find</button>
        </div>
        <div id="cresults"></div>
      </div>
      <div class="col">
        <h2 class="colh" data-i18n="Categories">Categories</h2>
        <div class="row">
          <input id="catq" type="text" placeholder="Search a category, e.g. Norway" data-i18n-ph="Search a category, e.g. Norway" onkeydown="if(event.key==='Enter')doCategorySearch()">
          <button onclick="doCategorySearch()" data-i18n="Search">Find</button>
        </div>
        <div id="catresults"></div>
      </div>
    </div>
  </section>

  <section id="channelsView" class="hide">
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
    <div class="mylayout">
      <div class="mlcats">
        <div class="colh" data-i18n="Favorite Categories">Favorite Categories</div>
        <div id="favCats" class="pcol" style="max-height:78vh"><span class="muted">No favorite categories yet.</span></div>
      </div>
      <div class="mlchans">
        <div class="colh"><span data-i18n="Favorite Channels">Favorite Channels</span> <span id="favChCount" class="muted"></span></div>
        <div id="favChans" class="favgrid"><span class="muted">No favorite channels yet. Star channels in Search.</span></div>
      </div>
    </div>
  </section>

  <section id="moviesView" class="hide">
    <div class="movieswrap">
      <aside class="moviefavs">
        <div class="colh">&#9733; <span data-i18n="Favorite Movies">Favorite Movies</span></div>
        <div id="movieFavList" class="moviefavlist"><span class="muted">No favorite movies yet.</span></div>
      </aside>
      <div class="moviesmain">
        <h2 class="colh" data-i18n="My Movies">My Movies</h2>
        <div class="row">
          <input id="movieQ" type="text" placeholder="Search your movies..." data-i18n-ph="Search your movies..." onkeydown="if(event.key==='Enter')searchMovies()">
          <button onclick="searchMovies()" data-i18n="Search">Search</button>
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
        <h2 class="colh" data-i18n="My Shows">My Shows</h2>
        <div class="row">
          <input id="showQ" type="text" placeholder="Search your shows..." data-i18n-ph="Search your shows..." onkeydown="if(event.key==='Enter')searchShows()">
          <button onclick="searchShows()" data-i18n="Search">Search</button>
        </div>
        <div id="showResults"></div>
        <div id="showDetails" class="showdetails"></div>
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
    <div class="card">
      <div class="muted">Your Xtream login is stored locally in config.json next to the app. It's only ever sent to your own provider.</div>
      <div class="colh" style="margin-top:16px" data-i18n="Connection">Connection</div>
      <div class="grid2">
        <div><label data-i18n="Username">Username</label><input id="s_user" type="text"></div>
        <div><label data-i18n="Password">Password</label><input id="s_pass" type="password"></div>
        <div><label data-i18n="Host (e.g. http://example.com:8080)">Host (e.g. http://example.com:8080)</label><input id="s_host" type="text"></div>
        <div style="display:flex;align-items:flex-end"><button class="ghost" onclick="testLogin()" data-i18n="Test login">Test login</button></div>
      </div>
      <div class="colh" style="margin-top:18px" data-i18n="Preferences">Preferences</div>
      <div class="grid2">
        <div><label data-i18n="Match strictness (0.40–0.80)">Match strictness (0.40&ndash;0.80)</label><input id="s_thr" type="text"></div>
        <div><label data-i18n="Default start section">Default start section</label>
          <select id="s_start"><option value="search">Search</option><option value="channels">Playlist Builder</option><option value="mytv">My TV</option><option value="movies">My Movies</option><option value="shows">My Shows</option><option value="mylist">My List</option></select></div>
      </div>
      <label data-i18n="Listings countries (comma separated: no, uk, us)">Listings countries (comma separated: no, uk, us)</label>
      <input id="s_cc" type="text">
      <label style="display:flex;align-items:center;gap:8px;margin-top:14px">
        <input id="s_checkshows" type="checkbox" style="width:auto;margin:0">
        <span data-i18n="Check favorite shows on startup">Check favorite shows on startup</span>
      </label>
      <div class="colh" style="margin-top:18px" data-i18n="Maintenance & Playback">Maintenance &amp; Playback</div>
      <div class="grid2">
        <div><label data-i18n="Stream extension">Stream extension</label>
          <select id="s_ext"><option value="ts">ts</option><option value="m3u8">m3u8</option></select></div>
      </div>
      <div class="row" style="margin-top:14px;justify-content:space-between">
        <span class="muted"><span data-i18n="Artwork cache">Artwork cache</span>: <b id="s_artsize">Checking...</b></span>
        <button class="ghost" onclick="clearArtworkCache()" data-i18n="Clear artwork cache">Clear artwork cache</button>
      </div>
      <div class="row" style="margin-top:14px">
        <button onclick="saveSettings()" data-i18n="Save">Save</button>
        <button class="ghost" onclick="refreshAllContent(this)" data-i18n="Refresh all content">Refresh all content</button>
        <button class="ghost" onclick="checkForUpdate(true)" id="checkUpdateBtn" data-i18n="Check for updates">Check for updates</button>
        <button class="ghost" onclick="openConfigFolder()" data-i18n="Open config folder">Open config folder</button>
      </div>
      <div id="s_msg" class="muted" style="margin-top:10px"></div>
    </div>
    </div>
  </section>
</main>
<div id="updateBanner" class="updatebanner hide">
  <span id="updateMsg"></span>
  <button onclick="doUpdateNow()" id="updateNowBtn">Update now</button>
  <button class="ghost" onclick="dismissUpdate()">Later</button>
</div>
<div id="playerModal" class="pmodal hide" onclick="if(event.target===this)closePlayer()">
  <div class="pbox">
    <div class="pbar"><span id="pTitle">Player</span><button class="pclose" onclick="closePlayer()">&times;</button></div>
    <video id="pVideo" controls autoplay playsinline></video>
    <div id="pMsg" class="muted" style="padding:8px 12px"></div>
  </div>
</div>
<script>
// --- pancake decorations (inline SVG, no assets) ---
const SVG_STACK='<svg viewBox="0 0 100 70" xmlns="http://www.w3.org/2000/svg"><ellipse cx="50" cy="52" rx="34" ry="9" fill="#e7a94e"/><ellipse cx="50" cy="51" rx="34" ry="9" fill="none" stroke="#b9762d" stroke-width="1.6"/><ellipse cx="50" cy="42" rx="32" ry="9" fill="#f0b95e"/><ellipse cx="50" cy="41" rx="32" ry="9" fill="none" stroke="#b9762d" stroke-width="1.6"/><ellipse cx="50" cy="32" rx="30" ry="9" fill="#f5c56e"/><ellipse cx="50" cy="31" rx="30" ry="9" fill="none" stroke="#b9762d" stroke-width="1.6"/><path d="M22 26 q6 10 12 3 q6 10 14 2 q6 10 14 2 q6 9 12 2 l0 5 q-6 5 -12 1 q-8 7 -14 0 q-8 7 -14 0 q-7 6 -12 -3 z" fill="#a8541f"/><rect x="38" y="12" width="24" height="11" rx="4" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.3"/></svg>';
const SVG_ONE='<svg viewBox="0 0 80 34" xmlns="http://www.w3.org/2000/svg"><ellipse cx="40" cy="20" rx="32" ry="10" fill="#f2bd63"/><ellipse cx="40" cy="19" rx="32" ry="10" fill="none" stroke="#b9762d" stroke-width="1.6"/><rect x="28" y="8" width="22" height="10" rx="4" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.3"/></svg>';
const SVG_TV='<svg viewBox="0 0 240 210" xmlns="http://www.w3.org/2000/svg"><rect x="26" y="58" width="150" height="120" rx="16" fill="#3a2c1f" stroke="#241a12" stroke-width="4"/><rect x="38" y="70" width="126" height="96" rx="8" fill="#1b3a6b"/><ellipse cx="101" cy="140" rx="44" ry="11" fill="#e7a94e"/><ellipse cx="101" cy="128" rx="42" ry="11" fill="#f0b95e"/><ellipse cx="101" cy="116" rx="40" ry="11" fill="#f5c56e"/><path d="M64 110 q6 12 14 4 q6 12 16 3 q7 12 16 3 q7 11 15 2 q6 10 12 3 l0 6 q-6 6 -12 2 q-8 8 -15 1 q-8 8 -16 1 q-8 8 -16 0 q-8 7 -14 -3 z" fill="#a8541f"/><rect x="86" y="86" width="30" height="14" rx="5" fill="#ffd77a" stroke="#e0a83e" stroke-width="1.5"/><circle cx="192" cy="86" r="8" fill="#2a2a2a"/><circle cx="192" cy="112" r="8" fill="#2a2a2a"/><rect x="186" y="132" width="12" height="30" rx="3" fill="#2a2a2a"/><rect x="52" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="136" y="178" width="14" height="20" rx="3" fill="#241a12"/><rect x="150" y="40" width="4" height="24" fill="#241a12"/><rect x="118" y="40" width="4" height="24" fill="#241a12" transform="rotate(-28 120 52)"/><circle cx="152" cy="38" r="6" fill="#f5c56e"/><circle cx="116" cy="34" r="6" fill="#f5c56e"/></svg>';
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
function initPancakes(){
  const l=document.getElementById('pcakeL'), r=document.getElementById('pcakeR');
  if(l)makePancakes(l);
  if(r)makePancakes(r);
}
function initPlPancakes(){
  const pl=document.getElementById('pcakePL');
  if(pl){makePancakes(pl,10);pl.style.opacity='0.13';}
}
function setNav(id){['navSearch','navChannels','navMylist','navMytv','navMovies','navShows','navSettings'].forEach(function(n){document.getElementById(n).classList.toggle('on',n===id);});}
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
  "Search":"Søk","Playlist Builder":"Lag spilleliste","My List":"Min liste","My TV":"Live TV","My Movies":"Mine filmer","My Shows":"Mine serier","Favorite Movies":"Favorittfilmer","Favorite Shows":"Favorittserier","Settings":"Innstillinger",
  "Favorite Channels":"Favorittkanaler","EPG Refresh":"Oppdater EPG","Channels":"Kanaler",
  "All Categories":"Alle kategorier","Selected categories":"Valgte kategorier","Filter Channels":"Kanaler","Playlist":"Spilleliste",
  "Add to Favorites":"Legg til favoritter","Tick all":"Velg alle","Untick all":"Fjern alle","Add ticked":"Legg til valgte",
  "Make Playlist (Categories)":"Lag spilleliste (kategorier)","Make Playlist (Channels)":"Lag spilleliste (kanaler)","Clear":"Fjern",
  "Ticked channels land here.":"Valgte kanaler vises her.",
  "Click a selected category to see its channels.":"Trykk på en kategori for å vise kanaler",
  "Tick categories on the left.":"Velg kategorier på venstre side.",
  "Favorite Categories":"Favorittkategorier","Categories":"Kategorier",
  "Matchfinder - Get Live / Next Match":"Kampfinner - Live / Neste kamp",
  "Save":"Lagre","Reload channels":"Last inn kanaler","Test login":"Test innlogging",
  "Connection":"Tilkobling","Preferences":"Innstillinger",
  "Maintenance & Playback":"Vedlikehold og avspilling","Refresh all content":"Oppdater alt innhold",
  "Check favorite shows on startup":"Se etter nye episoder i favorittserier ved oppstart",
  "Artwork cache":"Mellomlagret omslagskunst","Clear artwork cache":"Tøm omslagskunst",
  "Remove":"Fjern","Copy URL":"Kopier URL",
  "Match strictness (0.40–0.80)":"Treffnøyaktighet (0.40–0.80)",
  "Listings countries (comma separated: no, uk, us)":"Land for TV-guide (kommaseparert: no, uk, us)",
  "No channels here.":"Ingen kanaler her.","No program info":"Ingen programinfo",
  "Loading EPG...":"Laster EPG...","EPG loaded":"EPG lastet","EPG failed":"EPG feilet","Loading...":"Laster...",
  "No favorites to load EPG for.":"Ingen favoritter å laste EPG for.",
  "Update available":"Oppdatering tilgjengelig","you have":"du har","Downloading...":"Laster ned...",
  "Update downloaded. Restart now to finish updating?":"Oppdatering lastet ned. Start på nytt for å fullføre?",
  "Restart now":"Start på nytt","Update now":"Oppdater nå","Restarting...":"Starter på nytt...",
  "Updating... this window will reload shortly.":"Oppdaterer... vinduet lastes inn på nytt snart.",
  "Update failed. Try again later.":"Oppdatering feilet. Prøv igjen senere.",
  "Restart failed. Please close and reopen the app.":"Omstart feilet. Lukk og åpne appen igjen.",
  "Update installed. Please close this window and open Olo’s TVMate again.":"Oppdatering installert. Lukk dette vinduet og åpne Olo’s TVMate igjen.",
  "Check for updates":"Se etter oppdateringer","Checking...":"Sjekker...",
  "You are on the latest version":"Du har den nyeste versjonen",
  "Could not check for updates. Check your internet connection.":"Kunne ikke sjekke for oppdateringer. Sjekk internettforbindelsen.",
  "Open config folder":"Åpne konfigurasjonsmappe","Could not open folder.":"Kunne ikke åpne mappen.",
  "Host (e.g. http://example.com:8080)":"Vert (f.eks. http://example.com:777)",
  "Username":"Brukernavn","Password":"Passord","Stream extension":"Strøm-format",
  "Default start section":"Standard oppstartseksjon","Search a team, e.g. Leeds":"Søk etter lag, f.eks. Leeds",
  "Find a channel, e.g. tv2 play":"Finn en kanal, f.eks. tv2 play",
  "Search a category, e.g. Norway":"Søk kategori, f.eks. Norge","Filter categories...":"Filtrer kategorier...",
  "Search your movies...":"Søk i filmene dine...",
  "Search your shows...":"Søk i seriene dine...",
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
function hideAll(){searchView.classList.add('hide');settingsView.classList.add('hide');channelsView.classList.add('hide');mylistView.classList.add('hide');mytvView.classList.add('hide');moviesView.classList.add('hide');showsView.classList.add('hide');}
function showMytv(){hideAll();mytvView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navMytv');setSlogan('mytv');initMytv();}
function showMovies(){hideAll();moviesView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navMovies');setSlogan('movies');loadMovieFavorites();}
function showShows(){hideAll();showsView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navShows');setSlogan('shows');loadShowFavorites();}
function showMylist(){hideAll();mylistView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navMylist');setSlogan('mylist');loadFavorites();}
function showSearch(){hideAll();searchView.classList.remove('hide');document.querySelector('main').classList.remove('wide');setNav('navSearch');setSlogan('search');initPancakes();}
function showChannels(){hideAll();channelsView.classList.remove('hide');document.querySelector('main').classList.add('wide');setNav('navChannels');setSlogan('channels');loadCategories();initPlPancakes();}
function showSettings(){loadSettings();hideAll();settingsView.classList.remove('hide');document.querySelector('main').classList.remove('wide');setNav('navSettings');setSlogan('settings');}

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
  const m=(name||'').match(/^\s*([a-z0-9-]{1,5})\s*\|/i);
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
      +'<span class="chname">'+esc(c.name)+(c.quality?'<span class="tag">'+esc(c.quality)+'</span>':'')+'</span></label>';
  }
  el.innerHTML=html;
}
function ccTick(on){document.querySelectorAll('.ccck').forEach(function(c){c.checked=on;});}

function addTickedToPlaylist(){
  const ticked=new Set(Array.from(document.querySelectorAll('.ccck:checked')).map(function(c){return c.getAttribute('data-sid');}));
  for(const c of _ccChannels){
    const sid=String(c.stream_id);
    if(ticked.has(sid))_playlist.set(sid,{name:c.name,url:c.url,category:c.category});
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
      +'<div class="chname">'+esc(c.name)+'</div></div>';
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
      +'<div class="chname">'+esc(c.name)+(c.quality?'<span class="tag">'+esc(c.quality)+'</span>':'')
      +(c.category?' <span class="muted">'+esc(c.category)+'</span>':'')
      +'<div class="churl">'+esc(c.url)+'</div></div>'
      +'<div style="display:flex;flex-shrink:0">'+playbtns(c.stream_id,c.name,c.url)+'</div></div>';
  }
  el.innerHTML=html;
}
async function api(p,o){const r=await fetch(p,o);return r.json();}
async function favPost(body){const r=await fetch('/api/favorites',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return r.json();}
async function refreshStatus(){
  const s=await api('/api/status');
  status.innerHTML=!s.configured?'<span class="err">Not configured &mdash; open Settings</span>'
    :(s.channel_count!=null?s.channel_count+' channels loaded':'configured');
}
async function loadSettings(){
  const c=await api('/api/config');
  s_host.value=c.xtream_host||'';
  s_user.value=c.xtream_user||'';s_pass.value=c.xtream_pass||'';
  s_ext.value=c.stream_ext||'ts';s_thr.value=c.match_threshold||0.55;
  s_cc.value=(c.countries||['no','uk','us']).join(', ');
  s_start.value=c.start_section||'search';
  s_checkshows.checked=!!c.check_shows_on_startup;
  loadArtworkCacheSize();
}
async function saveSettings(){
  const body={xtream_host:s_host.value,xtream_user:s_user.value,
    xtream_pass:s_pass.value,stream_ext:s_ext.value,match_threshold:parseFloat(s_thr.value)||0.55,
    countries:s_cc.value.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean),
    start_section:s_start.value,check_shows_on_startup:s_checkshows.checked};
  const r=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  s_msg.textContent=r.ok?'Saved.':'Error saving.';refreshStatus();
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
async function testLogin(){s_msg.textContent='Testing...';
  const r=await api('/api/test');
  s_msg.innerHTML=r.ok?('OK &mdash; '+JSON.stringify(r.info)):('<span class="err">'+r.error+'</span>');}
async function refreshAllContent(btn){
  const old=btn.textContent;btn.disabled=true;btn.textContent='Refreshing...';s_msg.textContent='Refreshing channels, movies, shows and episodes...';
  try{
    const r=await api('/api/refresh_all',{method:'POST'});
    if(!r.ok)throw new Error(r.error||'refresh failed');
    s_msg.textContent='Refreshed '+r.channels+' channels, '+r.movies+' movies and '+r.shows+' shows.';
    if(r.new_episodes>0)toast('Found '+r.new_episodes+' new episode'+(r.new_episodes===1?'':'s')+' for your shows',7000);
    else toast('Successfully refreshed all content, no new episodes found',7000);
    refreshStatus();
  }catch(e){s_msg.textContent='Error: '+e.message;toast('Could not refresh all content.',7000);}
  btn.disabled=false;btn.textContent=old;
}
let _searchData=null;   // {fixtures, logged_in, ppv_categories}
let _teamGroups=[];      // [{team, fixtures:[...]}]
let _activeTeam=0;

async function doSearch(){
  const q=document.getElementById('q').value.trim();
  results.innerHTML='';
  if(!q)return;
  results.innerHTML='<span class="muted">Searching listings...</span>';
  const r=await api('/api/search?q='+encodeURIComponent(q));
  if(r.error){results.innerHTML='<span class="err">'+r.error+'</span>';return;}
  _searchData=r;
  if(r.logged_in)await refreshFavState();
  let head='';
  if(r.source_errors&&r.source_errors.length)
    head+='<div class="muted err">Some listings failed: '+r.source_errors.join('; ')+'</div>';
  if(!r.fixtures.length){results.innerHTML=head+'<div class="muted">No <b>televised</b> match found for "'+esc(q)+'" in the next ~week across your listings countries. The team may still be playing &mdash; untelevised games and matches only on broadcasters outside your selected countries will not appear here.</div>';return;}
  // Group fixtures by the team that matches the query (home or away).
  _teamGroups=groupByTeam(r.fixtures,q);
  _activeTeam=0;
  results.innerHTML=head+'<div id="teamSwitch"></div><div id="teamFixtures"></div>';
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
  const qs=q.split(/\s+/).filter(Boolean);
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
  g.fixtures.forEach(function(f,fi){
    html+=renderFixtureCard(f,fi);
  });
  el.innerHTML=html;
}

function renderFixtureCard(f,fi){
  const when=f.start?new Date(f.start).toLocaleString():'';
  let badge='';
  if(f.start){
    const kick=new Date(f.start);
    const mins=Math.floor((Date.now()-kick.getTime())/60000);
    const sameDay=kick.toDateString()===new Date().toDateString();
    if(mins>=0&&mins<=140)badge=' <span class="live">\u25CF LIVE ~'+mins+"'</span>";
    else if(mins>140&&(mins<360||sameDay))badge=' <span class="ended">ended / earlier today</span>';
    else if(mins<0&&mins>-60)badge=' <span class="soon">starts in '+(-mins)+" min</span>";
  }
  let html='<div class="card"><b>'+esc(f.home)+' v '+esc(f.away)+'</b> <span class="muted">'+when+'</span>'+badge;
  if(!_searchData.logged_in){
    html+='<div class="muted">Log in via <a onclick="showSettings()" style="color:var(--acc);cursor:pointer">Settings</a> to see which of your Xtream channels match.</div></div>';
    return html;
  }
  // Build broadcaster rows, sorted country then broadcaster.
  const rows=[];
  for(const cc of Object.keys(f.by_country||{})){
    for(const b of f.by_country[cc]){
      rows.push({cc:cc,bcast:b});
    }
  }
  rows.sort(function(x,y){return x.cc===y.cc?x.bcast.localeCompare(y.bcast):x.cc.localeCompare(y.cc);});
  if(rows.length){
    html+='<div class="bcastlist">';
    rows.forEach(function(row,ri){
      // channels matched to this broadcaster
      const chans=(f.matches||[]).filter(function(m){return m.matched===row.bcast&&(!m.country||m.country===row.cc.toUpperCase());});
      const rid='f'+fi+'b'+ri;
      html+='<div class="bcrow" data-exp="'+rid+'">'
        +'<div class="bchead"><span class="cc">'+esc(row.cc)+'</span> <span class="bcname">'+esc(row.bcast)+'</span>'
        +' <span class="muted exphint">'+(chans.length?('click to expand ('+chans.length+')'):'no matching channels')+'</span></div>'
        +'<div class="bcchans hide" id="'+rid+'">';
      if(chans.length){
        for(const m of chans){
          const fav=_favChanSet.has(String(m.stream_id))?' on':'';
          html+='<div class="chline"><span class="matchchan"><span class="favstar'+fav+'" data-sid="'+escAttr(String(m.stream_id))+'" data-name="'+escAttr(m.xtream_name)+'" data-cat="'+escAttr(m.category||'')+'" title="Favorite">&#9733;</span>'
            +'<span class="chn">'+esc(m.xtream_name)+(m.quality?'<span class="tag">'+esc(m.quality)+'</span>':'')+'</span></span>'
            +'<span class="chbtns">'+playbtns(m.stream_id,m.xtream_name,m.url)+'</span></div>';
        }
      }else{
        html+='<div class="muted" style="padding:6px 10px">No channels in your list match this broadcaster.</div>';
      }
      html+='</div></div>';
    });
    html+='</div>';
  }
  // PPV / streaming fallbacks (kept from before)
  if(f.ppv_hits&&f.ppv_hits.length){
    html+='<div class="muted" style="margin-top:8px">Possible PPV/event channels:</div><div class="bcastlist">';
    for(const m of f.ppv_hits){
      const fav=_favChanSet.has(String(m.stream_id))?' on':'';
      html+='<div class="chline"><span class="matchchan"><span class="favstar'+fav+'" data-sid="'+escAttr(String(m.stream_id))+'" data-name="'+escAttr(m.xtream_name)+'" data-cat="'+escAttr(m.category||'')+'" title="Favorite">&#9733;</span>'
        +'<span class="chn">'+esc(m.xtream_name)+(m.quality?'<span class="tag">'+esc(m.quality)+'</span>':'')+'</span></span>'
        +'<span class="chbtns">'+playbtns(m.stream_id,m.xtream_name,m.url)+'</span></div>';
    }
    html+='</div>';
  }
  if(!rows.length&&(!f.ppv_hits||!f.ppv_hits.length)){
    html+='<div class="muted">No Xtream channels matched. Try lowering strictness in Settings.</div>';
  }
  html+='</div>';
  return html;
}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function escAttr(s){return esc(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function playbtns(sid,name,url,showCopy){
  const s=escAttr(String(sid)), n=escAttr(name||''), u=escAttr(url);
  return '<button class="btnplay" data-sid="'+s+'" data-name="'+n+'">&#9658; Play</button>'
    +'<button class="btnvlc" data-sid="'+s+'">&#9658; VLC</button>'
    +(showCopy?'<button class="copy" data-url="'+u+'">'+tr('Copy URL')+'</button>':'');
}
let _hls=null;
async function playBrowser(sid,name){
  const modal=document.getElementById('playerModal');
  const video=document.getElementById('pVideo');
  const msg=document.getElementById('pMsg');
  document.getElementById('pTitle').textContent=name||'Player';
  msg.textContent='Loading...';
  modal.classList.remove('hide');
  // get the hls url
  let hls;
  try{const r=await fetch('/api/hls?id='+encodeURIComponent(sid));const j=await r.json();hls=j.hls;}catch(e){msg.textContent='Could not build stream URL.';return;}
  const proxied='/api/proxy?u='+encodeURIComponent(hls);
  function start(src,viaProxy){
    if(_hls){_hls.destroy();_hls=null;}
    if(window.Hls&&Hls.isSupported()){
      _hls=new Hls({manifestLoadingTimeOut:12000});
      let failed=false;
      _hls.on(Hls.Events.ERROR,function(ev,data){
        if(data.fatal){
          if(!viaProxy&&!failed){failed=true;msg.textContent='Direct blocked \u2014 routing through local proxy...';start(proxied,true);}
          else{msg.textContent='Could not play this stream in the browser. Try VLC.';}
        }
      });
      _hls.on(Hls.Events.MANIFEST_PARSED,function(){msg.textContent='';video.play().catch(()=>{});});
      _hls.loadSource(src);_hls.attachMedia(video);
    }else if(video.canPlayType('application/vnd.apple.mpegurl')){
      video.src=src;msg.textContent='';video.play().catch(()=>{});
    }else{msg.textContent='Your browser cannot play HLS. Try VLC.';}
  }
  start(hls,false);
}
function closePlayer(){
  const modal=document.getElementById('playerModal');
  const video=document.getElementById('pVideo');
  if(_hls){_hls.destroy();_hls=null;}
  video.pause();video.removeAttribute('src');video.load();
  modal.classList.add('hide');
}
async function playVLC(sid,btn){
  const old=btn?btn.textContent:'';
  if(btn){btn.textContent='Opening...';}
  try{
    const r=await fetch('/api/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stream_id:sid})});
    const j=await r.json();
    if(!r.ok||j.error){alert(j.error||'Could not launch VLC.');}
  }catch(e){alert('Could not launch VLC.');}
  if(btn){setTimeout(()=>{btn.textContent=old;},1200);}
}
let _favMovieSet=new Set();
async function loadMovieFavorites(){
  const r=await api('/api/favorites');
  const movies=r.movies||[];
  _favMovieSet=new Set(movies.map(m=>String(m.stream_id)));
  const el=document.getElementById('movieFavList');
  if(!movies.length){el.innerHTML='<span class="muted">No favorite movies yet.</span>';return;}
  let h='';
  for(const m of movies){
    const sid=escAttr(String(m.stream_id)), ext=escAttr(m.extension||'mp4');
    const poster='<span class="moviefavposter">&#127916;'+(m.cover?'<img src="'+escAttr(m.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'</span>';
    h+='<div class="moviefav">'+poster+'<div class="moviefavinfo"><div class="moviefavname">'+esc(m.name)+'</div>'
      +'<div class="moviefavbtns"><button class="btnvlc movievlc" data-sid="'+sid+'" data-ext="'+ext+'">VLC</button>'
      +'<button class="favrm movieremove" data-sid="'+sid+'">&times;</button></div></div></div>';
  }
  el.innerHTML=h;
}
async function toggleMovieFavorite(movie,starEl){
  const r=await favPost({action:'toggle_movie',movie:movie});
  _favMovieSet=new Set((r.movie_ids||[]).map(String));
  if(starEl)starEl.classList.toggle('on',_favMovieSet.has(String(movie.stream_id)));
  await loadMovieFavorites();
}
async function removeMovieFavorite(sid){
  await favPost({action:'remove_movie',stream_id:sid});
  _favMovieSet.delete(String(sid));
  document.querySelectorAll('.moviestar').forEach(el=>{if(el.getAttribute('data-sid')===String(sid))el.classList.remove('on');});
  await loadMovieFavorites();
}
async function searchMovies(){
  const q=(document.getElementById('movieQ').value||'').trim();
  const el=document.getElementById('movieResults');
  if(!q){el.innerHTML='<div class="muted" style="margin-top:14px">Enter a movie title.</div>';return;}
  el.innerHTML='<div class="muted" style="margin-top:14px">Searching your movie library...</div>';
  const r=await api('/api/movies?q='+encodeURIComponent(q));
  if(r.error){el.innerHTML='<div class="err" style="margin-top:14px">'+esc(r.error)+'</div>';return;}
  if(!r.logged_in){el.innerHTML='<div class="muted" style="margin-top:14px">Log in via Settings first.</div>';return;}
  if(!r.movies.length){el.innerHTML='<div class="muted" style="margin-top:14px">No movies found for &quot;'+esc(q)+'&quot;.</div>';return;}
  await loadMovieFavorites();
  let h='<div class="muted" style="margin-top:12px">'+r.movies.length+' result'+(r.movies.length===1?'':'s')+'</div><div class="moviegrid">';
  for(const m of r.movies){
    const sid=escAttr(String(m.stream_id)), ext=escAttr(m.extension||'mp4');
    const fav=_favMovieSet.has(String(m.stream_id))?' on':'';
    const poster=m.cover?'<img src="'+escAttr(m.cover)+'" alt="" loading="lazy" onerror="this.parentElement.textContent=String.fromCodePoint(127916)">':'&#127916;';
    h+='<div class="moviecard"><div class="movieposter">'+poster+'</div><div class="movieinfo"><div class="movietitle">'+esc(m.name)+'</div>'
      +'<div class="moviemeta">'+(m.year?esc(m.year):'')+(m.rating?(' &nbsp; Rating: '+esc(m.rating)):'')+'</div>'
      +'<div class="movieactions"><span class="favstar moviestar'+fav+'" data-sid="'+sid+'" data-name="'+escAttr(m.name||'')+'" data-ext="'+ext+'" data-year="'+escAttr(m.year||'')+'" data-rating="'+escAttr(m.rating||'')+'" data-cover="'+escAttr(m.cover||'')+'" title="Favorite">&#9733;</span>'
      +'<button class="btnvlc movievlc" data-sid="'+sid+'" data-ext="'+ext+'">&#9658; VLC</button></div></div></div>';
  }
  el.innerHTML=h+'</div>';
}
async function playMovieVLC(sid,ext,btn){
  const old=btn.textContent;btn.textContent='Opening...';
  try{
    const r=await fetch('/api/play_movie',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stream_id:sid,extension:ext})});
    const j=await r.json();if(!r.ok||j.error)alert(j.error||'Could not launch VLC.');
  }catch(e){alert('Could not launch VLC.');}
  setTimeout(()=>{btn.textContent=old;},1200);
}
let _showSeasons={};
let _activeSeriesId=null;
let _favShowSet=new Set();
async function loadShowFavorites(){
  const r=await api('/api/favorites'), shows=r.shows||[];
  _favShowSet=new Set(shows.map(s=>String(s.series_id)));
  const el=document.getElementById('showFavList');
  if(!shows.length){el.innerHTML='<span class="muted">No favorite shows yet.</span>';return;}
  let h='';
  for(const s of shows){
    const poster='<span class="showfavposter">&#128250;'+(s.cover?'<img src="'+escAttr(s.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'')+'</span>';
    h+='<div class="showfav" data-series="'+escAttr(String(s.series_id))+'">'+poster+'<div class="showfavinfo"><div class="showfavname">'+esc(s.name)+'</div></div>'
      +'<button class="favrm showremove" data-series="'+escAttr(String(s.series_id))+'" title="Remove">&times;</button></div>';
  }
  el.innerHTML=h;
}
async function toggleShowFavorite(show,starEl){
  const r=await favPost({action:'toggle_show',show:show});
  _favShowSet=new Set((r.show_ids||[]).map(String));
  if(starEl)starEl.classList.toggle('on',_favShowSet.has(String(show.series_id)));
  await loadShowFavorites();
}
async function removeShowFavorite(seriesId){
  await favPost({action:'remove_show',series_id:seriesId});
  document.querySelectorAll('.showstar').forEach(el=>{if(el.getAttribute('data-series')===String(seriesId))el.classList.remove('on');});
  await loadShowFavorites();
}
async function searchShows(){
  const q=(document.getElementById('showQ').value||'').trim(), el=document.getElementById('showResults');
  document.getElementById('showDetails').innerHTML='';
  if(!q){el.innerHTML='<div class="muted" style="margin-top:14px">Enter a show title.</div>';return;}
  el.innerHTML='<div class="muted" style="margin-top:14px">Searching your shows...</div>';
  const r=await api('/api/shows?q='+encodeURIComponent(q));
  if(r.error){el.innerHTML='<div class="err" style="margin-top:14px">'+esc(r.error)+'</div>';return;}
  if(!r.logged_in){el.innerHTML='<div class="muted" style="margin-top:14px">Log in via Settings first.</div>';return;}
  if(!r.shows.length){el.innerHTML='<div class="muted" style="margin-top:14px">No shows found for &quot;'+esc(q)+'&quot;.</div>';return;}
  await loadShowFavorites();
  let h='<div class="showgrid">';
  for(const s of r.shows){
    const fav=_favShowSet.has(String(s.series_id))?' on':'';
    const cover=s.cover?'<img src="'+escAttr(s.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'';
    h+='<div class="showcard" data-series="'+escAttr(String(s.series_id))+'"><div class="showposter">&#128250;'+cover+'</div>'
      +'<div><div class="showname">'+esc(s.name)+'</div><div class="moviemeta" style="margin-top:7px">'+(s.year?esc(s.year):'')+(s.rating?(' &nbsp; Rating: '+esc(s.rating)):'')+'</div>'
      +'<span class="favstar showstar'+fav+'" data-series="'+escAttr(String(s.series_id))+'" data-name="'+escAttr(s.name||'')+'" data-cover="'+escAttr(s.cover||'')+'" data-year="'+escAttr(s.year||'')+'" data-rating="'+escAttr(s.rating||'')+'" title="Favorite">&#9733;</span></div></div>';
  }
  el.innerHTML=h+'</div>';
}
async function loadShow(seriesId,refresh){
  _activeSeriesId=seriesId;
  const el=document.getElementById('showDetails');
  el.innerHTML='<div class="muted">Loading seasons and episodes...</div>';
  const r=await api('/api/show?id='+encodeURIComponent(seriesId)+(refresh?'&refresh=1':''));
  if(r.error){el.innerHTML='<div class="err">'+esc(r.error)+'</div>';return false;}
  await loadShowFavorites();
  document.getElementById('showResults').innerHTML='';
  _showSeasons={};
  const heroCover=r.cover?'<img src="'+escAttr(r.cover)+'" alt="" onerror="this.remove()">':'';
  const heroFav=_favShowSet.has(String(seriesId))?' on':'';
  let h='<div class="showhero"><div class="showheroart">&#128250;'+heroCover+'</div><div><h2>'+esc(r.name||'Show')
    +'<span class="favstar showstar'+heroFav+'" data-series="'+escAttr(String(seriesId))+'" data-name="'+escAttr(r.name||'Show')+'" data-cover="'+escAttr(r.cover||'')+'" data-year="" data-rating="" title="Favorite">&#9733;</span></h2>'
    +'<div class="muted">'+r.seasons.length+' season'+(r.seasons.length===1?'':'s')+'</div></div></div>';
  for(const season of r.seasons){
    _showSeasons[String(season.number)]=season.episodes;
    const seasonCover=season.cover?'<img src="'+escAttr(season.cover)+'" alt="" loading="lazy" onerror="this.remove()">':'';
    h+='<div class="seasonblock"><div class="seasonlayout"><div class="seasonart">&#128250;'+seasonCover+'</div><div class="seasoncontent"><div class="seasonhead"><b>'+esc(season.title)+'</b></div><div class="episodes">';
    for(let ei=0;ei<season.episodes.length;ei++){
      const ep=season.episodes[ei];
      h+='<div class="episode"><div class="episodename"><b>E'+esc(ep.episode_num)+'</b> '+esc(ep.title||'Episode')+'</div>'
        +'<button class="btnvlc episodevlc" data-season="'+escAttr(String(season.number))+'" data-index="'+ei+'">&#9658; VLC</button></div>';
    }
    h+='</div></div></div></div>';
  }
  if(!r.seasons.length)h+='<div class="muted">No episodes found.</div>';
  el.innerHTML=h;
  return true;
}
async function checkAllShows(btn){
  const old=btn.innerHTML;btn.disabled=true;btn.textContent='Checking all shows...';
  try{
    const r=await fetch('/api/check_show_updates',{method:'POST'}), j=await r.json();
    if(!r.ok||j.error)throw new Error(j.error||'check failed');
    if(_activeSeriesId)await loadShow(_activeSeriesId,true);
    await loadShowFavorites();
    if(j.new_episodes>0)toast('Found '+j.new_episodes+' new episode'+(j.new_episodes===1?'':'s')+' for your shows',7000);
    else toast('Successfully refreshed playlists, no new episodes found',7000);
  }catch(e){toast('Could not refresh show playlists.');}
  btn.disabled=false;btn.innerHTML=old;
}
async function checkShowsOnStartup(){
  try{
    const r=await fetch('/api/check_show_updates',{method:'POST'}), j=await r.json();
    if(!r.ok||j.error)throw new Error(j.error||'check failed');
    if(j.new_episodes>0)toast('Found '+j.new_episodes+' new episode'+(j.new_episodes===1?'':'s')+' for your shows',7000);
    else toast('Successfully refreshed playlists, no new episodes found',7000);
  }catch(e){toast('Could not refresh show playlists.',7000);}
}
async function playEpisodeQueue(season,index,btn){
  const episodes=(_showSeasons[String(season)]||[]).slice(parseInt(index,10)||0), old=btn.textContent;btn.textContent='Opening...';
  try{const r=await fetch('/api/play_season',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({episodes:episodes})});const j=await r.json();if(!r.ok||j.error)alert(j.error||'Could not launch VLC.');}
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
  const fc=document.getElementById('favCats');
  if(!r.categories||!r.categories.length){fc.innerHTML='<span class="muted">No favorite categories yet.</span>';}
  else{
    let h='';
    for(const c of r.categories)
      h+='<div class="chrow"><span class="chname">'+_flagFor(c)+' '+esc(c)+'</span>'
        +'<button class="favrm" data-cat="'+escAttr(c)+'">'+tr('Remove')+'</button></div>';
    fc.innerHTML=h;
  }
  const fch=document.getElementById('favChans');
  const cnt=document.getElementById('favChCount');
  cnt.textContent=(r.channels&&r.channels.length)?('('+r.channels.length+')'):'';
  if(!r.channels||!r.channels.length){fch.innerHTML='<span class="muted">No favorite channels yet. Star channels in Search.</span>';}
  else{
    let h='';
    for(const c of r.channels){
      h+='<div class="favcard"><div class="favcardname">'+esc(c.name)+(c.category?' <span class="muted" style="font-size:11px">'+esc(c.category)+'</span>':'')+'</div>'
        +'<div class="favcardbtns">'+playbtns(c.stream_id,c.name,c.url,true)
        +'<button class="favrm" data-sid="'+escAttr(String(c.stream_id))+'">'+tr('Remove')+'</button></div></div>';
    }
    fch.innerHTML=h;
  }
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
  toast('Added '+cats.length+' categor'+(cats.length===1?'y':'ies')+' to My List');
}
async function favPlaylist(){
  const items=Array.from(_playlist.entries()).map(function(kv){return {stream_id:kv[0],name:kv[1].name,category:kv[1].category||''};});
  if(!items.length){alert('Playlist is empty.');return;}
  await favPost({action:'add_channels',channels:items});
  toast('Added '+items.length+' channel'+(items.length===1?'':'s')+' to My List');
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
  loadTvSource('__fav__');
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
    _tvChannels=(r.channels||[]).map(function(c){return {stream_id:c.stream_id,name:c.name,category:c.category||'',url:c.url};});
  }else{
    const r=await api('/api/channels?q=&cat='+encodeURIComponent(src));
    _tvChannels=(r.channels||[]).map(function(c){return {stream_id:c.stream_id,name:c.name,category:c.category||'',url:c.url};});
  }
  await refreshFavState();
  renderTvGuide();
}
function renderTvChannels(){
  renderTvGuide();
}
let _tvEpg={};   // stream_id -> [{title,start_ts,stop_ts},...]
function renderTvGuide(){
  const head=document.getElementById('tvTimeHead');
  const body=document.getElementById('tvGuideBody');
  // simple time header from the current half hour
  const d=new Date();d.setMinutes(d.getMinutes()<30?0:30,0,0);
  const base=d.getTime();
  const slotStart=[];
  for(let i=0;i<5;i++){slotStart.push(base+i*30*60000);}
  head.innerHTML=slotStart.map(function(ms){const t=new Date(ms);return '<div class="tvtimeslot">'+('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2)+'</div>';}).join('');
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
      +'<span class="tvflag">'+_flagFor(c.category||c.name)+'</span>'
      +'<span class="tvname">'+esc(c.name)+'</span>'
      +'<span class="favstar'+fav+'" data-sid="'+escAttr(String(c.stream_id))+'" data-name="'+escAttr(c.name)+'" data-cat="'+escAttr(c.category||'')+'" title="Favorite">\u2605</span>'
      +'</div>'
      +'<div class="tvprog">'+epgCellHtml(c.stream_id,winStart,winEnd)+'</div></div>';
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
function epgCellHtml(sid,winStart,winEnd){
  const progs=_tvEpg[String(sid)];
  if(!progs||!progs.length)return '<span class="epgnone muted">'+tr('No program info')+'</span>';
  const nowSec=Date.now()/1000;
  // show current + upcoming programmes (drop ones that already ended)
  const upcoming=progs.filter(function(p){return p.title && (!p.stop_ts || p.stop_ts>nowSec);})
                      .sort(function(a,b){return (a.start_ts||0)-(b.start_ts||0);});
  const list=(upcoming.length?upcoming:progs).slice(0,4);
  const parts=list.map(function(p){
    const live=(p.start_ts&&p.stop_ts&&p.start_ts<=nowSec&&p.stop_ts>nowSec);
    let tm='';
    if(p.start_ts){const t=new Date(p.start_ts*1000);tm=('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2)+' ';}
    const cls=live?'epgprog live':'epgprog';
    return '<span class="'+cls+'"><span class="epgt">'+tm+'</span>'+esc(p.title)+'</span>';
  });
  return parts.join('<span class="epgsep">\u2022</span>');
}
async function tvPlay(sid,name){
  _tvPlaying=sid;
  const slot=document.getElementById('tvPlayerSlot');
  slot.classList.add('on');
  slot.innerHTML='<div class="tvplayerbar"><span>'+esc(name||'')+'</span><button class="pclose" onclick="tvStop()">&times;</button></div><video id="tvVideo" controls autoplay playsinline></video>';
  renderTvGuide();
  const video=document.getElementById('tvVideo');
  let hls;
  try{const r=await fetch('/api/hls?id='+encodeURIComponent(sid));const j=await r.json();hls=j.hls;}catch(e){return;}
  const proxied='/api/proxy?u='+encodeURIComponent(hls);
  if(window._tvhls){window._tvhls.destroy();window._tvhls=null;}
  function start(src,viaProxy){
    if(window.Hls&&Hls.isSupported()){
      window._tvhls=new Hls();
      let failed=false;
      window._tvhls.on(Hls.Events.ERROR,function(ev,data){
        if(data.fatal){if(!viaProxy&&!failed){failed=true;start(proxied,true);}}
      });
      window._tvhls.loadSource(src);window._tvhls.attachMedia(video);
    }else if(video.canPlayType('application/vnd.apple.mpegurl')){video.src=src;video.play().catch(()=>{});}
  }
  start(hls,false);
}
function tvStop(){
  _tvPlaying=null;
  if(window._tvhls){window._tvhls.destroy();window._tvhls=null;}
  const slot=document.getElementById('tvPlayerSlot');
  slot.classList.remove('on');slot.innerHTML='';
  renderTvGuide();
}
async function epgRefresh(){
  const btn=document.getElementById('epgRefresh');
  const old=btn.innerHTML;
  btn.innerHTML='<span>'+tr('Loading EPG...')+'</span>';btn.disabled=true;
  try{
    // favorite channels only (category channels can be hundreds - too slow)
    const fav=await api('/api/favorites');
    const ids=(fav.channels||[]).map(function(c){return String(c.stream_id);});
    if(!ids.length){toast(tr('No favorites to load EPG for.'));btn.innerHTML=old;btn.disabled=false;return;}
    const r=await fetch('/api/epg?force=1&ids='+encodeURIComponent(ids.join(',')));
    const j=await r.json();
    _tvEpg=Object.assign({},_tvEpg,j.epg||{});
    renderTvGuide();
    let withData=ids.filter(function(k){return _tvEpg[k]&&_tvEpg[k].length;}).length;
    toast(tr('EPG loaded')+' ('+withData+'/'+ids.length+')');
  }catch(e){toast(tr('EPG failed'));}
  btn.innerHTML=old;btn.disabled=false;
}
// Event delegation: any Copy button's data-url is copied on click.
document.addEventListener('click',function(e){
  const ss=e.target.closest('.showstar');
  if(ss){toggleShowFavorite({series_id:ss.getAttribute('data-series'),name:ss.getAttribute('data-name'),cover:ss.getAttribute('data-cover'),year:ss.getAttribute('data-year'),rating:ss.getAttribute('data-rating')},ss);return;}
  const sr=e.target.closest('.showremove');
  if(sr){removeShowFavorite(sr.getAttribute('data-series'));return;}
  const sc=e.target.closest('.showcard');
  if(sc){loadShow(sc.getAttribute('data-series'));return;}
  const sf=e.target.closest('.showfav');
  if(sf){loadShow(sf.getAttribute('data-series'));return;}
  const ev=e.target.closest('.episodevlc');
  if(ev){playEpisodeQueue(ev.getAttribute('data-season'),ev.getAttribute('data-index'),ev);return;}
  const mv=e.target.closest('.movievlc');
  if(mv){playMovieVLC(mv.getAttribute('data-sid'),mv.getAttribute('data-ext'),mv);return;}
  const ms=e.target.closest('.moviestar');
  if(ms){toggleMovieFavorite({stream_id:ms.getAttribute('data-sid'),name:ms.getAttribute('data-name'),extension:ms.getAttribute('data-ext'),year:ms.getAttribute('data-year'),rating:ms.getAttribute('data-rating'),cover:ms.getAttribute('data-cover')},ms);return;}
  const mr=e.target.closest('.movieremove');
  if(mr){removeMovieFavorite(mr.getAttribute('data-sid'));return;}
  const tt=e.target.closest('.teamtab');
  if(tt){_activeTeam=parseInt(tt.getAttribute('data-team'),10)||0;renderTeamSwitch();renderActiveTeam();return;}
  const bh=e.target.closest('.bchead');
  if(bh){
    const row=bh.parentElement;
    const box=row.querySelector('.bcchans');
    if(box)box.classList.toggle('hide');
    row.classList.toggle('open');
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
  let start='search',checkShows=false;
  try{const c=await api('/api/config');start=c.start_section||'search';checkShows=!!c.check_shows_on_startup;}catch(e){}
  const map={search:showSearch,channels:showChannels,mytv:showMytv,movies:showMovies,shows:showShows,mylist:showMylist};
  (map[start]||showSearch)();
  if(checkShows)setTimeout(checkShowsOnStartup,500);
})();
initPancakes();
refreshStatus();
// --- auto-update ---
let _updateLatest=null;
async function openConfigFolder(){
  try{
    const r=await fetch('/api/open_folder',{method:'POST'});
    const j=await r.json();
    if(!j.ok)toast(tr('Could not open folder.')+(j.path?(' '+j.path):''));
  }catch(e){toast(tr('Could not open folder.'));}
}
async function checkForUpdate(manual){
  const btn=document.getElementById('checkUpdateBtn');
  if(manual&&btn){btn.textContent=tr('Checking...');btn.disabled=true;}
  try{
    const r=await fetch('/api/update_check');
    const j=await r.json();
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
    const r=await fetch('/api/update_download',{method:'POST'});
    const j=await r.json();
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
    const r=await fetch('/api/update_restart',{method:'POST'});
    const j=await r.json();
    if(j.relaunch===false){
      document.getElementById('updateMsg').textContent=tr('Update installed. Please close this window and open Olo\u2019s TVMate again.');
    }else{
      document.getElementById('updateMsg').textContent=tr('Updating... this window will reload shortly.');
      setTimeout(function(){location.reload();},6000);
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

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/season_art":
                show = (q.get("show", [""])[0]).strip().lower()
                season = (q.get("season", [""])[0]).strip()
                if not re.fullmatch(r"[a-f0-9]{16}", show) or not re.fullmatch(r"\d+", season):
                    return self._send(400, {"error": "bad artwork path"})
                path = os.path.join(app_dir(), "artwork", "tvmaze-" + show,
                                    "season-" + season + ".jpg")
                if not os.path.isfile(path):
                    return self._send(404, {"error": "artwork not found"})
                with open(path, "rb") as f:
                    raw = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE.replace("__VERSION__", VERSION), "text/html")

            if u.path == "/api/status":
                cfg = load_config()
                x = Xtream(cfg)
                count = len(_XT_CACHE["channels"]) if (x.configured() and _XT_CACHE["channels"]) else None
                return self._send(200, {"configured": x.configured(), "channel_count": count})

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
                        "url": x.stream_url(sid) if (x.configured() and sid is not None) else "",
                    })
                return self._send(200, {"categories": fav.get("categories", []),
                                        "channels": chans,
                                        "movies": fav.get("movies", []),
                                        "shows": fav.get("shows", [])})

            if u.path == "/api/epg":
                # ids=comma-separated stream ids; force=1 to bypass cache
                ids_raw = (q.get("ids", [""])[0]).strip()
                force = q.get("force", ["0"])[0] == "1"
                cfg = load_config()
                x = Xtream(cfg)
                if not x.configured() or not ids_raw:
                    return self._send(400, {"error": "bad request"})
                ids = [s for s in ids_raw.split(",") if s.strip()]
                now = time.time()
                result = {}
                to_fetch = []
                for sid in ids:
                    cached = _EPG_CACHE.get(sid)
                    if cached and not force and (now - cached["ts"] < _EPG_TTL):
                        result[sid] = cached["programmes"]
                    else:
                        to_fetch.append(sid)
                # throttle: fetch in small batches with a short pause
                import time as _t
                for i, sid in enumerate(to_fetch):
                    progs = x.short_epg(sid, limit=6)
                    _EPG_CACHE[sid] = {"ts": now, "programmes": progs}
                    result[sid] = progs
                    if (i + 1) % 4 == 0:
                        _t.sleep(0.35)   # brief pause every 4 requests
                return self._send(200, {"epg": result})

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

            if u.path == "/api/proxy":
                # Proxy an HLS playlist or segment through the local server so the
                # in-browser player works even when the provider blocks CORS.
                target = q.get("u", [""])[0]
                if not target:
                    return self._send(400, {"error": "no url"})
                try:
                    req = urllib.request.Request(target, headers={
                        "User-Agent": "VLC/3.0 LibVLC/3.0",
                        "Accept": "*/*"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        ctype = resp.headers.get("Content-Type", "application/octet-stream")
                        raw = resp.read()
                    # If it's an m3u8 playlist, rewrite child URLs to go via proxy too
                    low = target.lower()
                    if "mpegurl" in ctype.lower() or ".m3u8" in low:
                        text = raw.decode("utf-8", "replace")
                        base = target.rsplit("/", 1)[0] + "/"
                        out_lines = []
                        for line in text.splitlines():
                            s = line.strip()
                            if s and not s.startswith("#"):
                                seg = s if s.startswith(("http://", "https://")) else base + s
                                out_lines.append("/api/proxy?u=" + urllib.parse.quote(seg, safe=""))
                            else:
                                out_lines.append(line)
                        raw = ("\n".join(out_lines) + "\n").encode("utf-8")
                        ctype = "application/vnd.apple.mpegurl"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
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
                        "quality": quality_tag(nm),
                        "url": x.stream_url(ch["stream_id"]),
                    })
                total = len(out)
                capped = out[:500]
                return self._send(200, {"channels": capped, "logged_in": True,
                                        "total": total, "shown": len(capped)})

            if u.path == "/api/movies":
                term = (q.get("q", [""])[0]).strip().lower()
                cfg = load_config()
                x = Xtream(cfg)
                if not x.configured():
                    return self._send(200, {"movies": [], "logged_in": False})
                if not term:
                    return self._send(200, {"movies": [], "logged_in": True})
                try:
                    movies = get_xtream_movies(cfg)
                except Exception as e:
                    return self._send(200, {"movies": [], "logged_in": True,
                                            "error": str(e)})
                words = [w for w in term.split() if w]
                out = []
                for m in movies:
                    name = str(m.get("name") or "")
                    low = name.lower()
                    if not all(w in low for w in words):
                        continue
                    cover = str(m.get("stream_icon") or m.get("cover") or
                                m.get("movie_image") or "").strip()
                    if not cover.startswith(("http://", "https://")):
                        cover = ""
                    out.append({
                        "stream_id": m.get("stream_id"),
                        "name": name,
                        "extension": m.get("container_extension") or "mp4",
                        "year": m.get("year") or "",
                        "rating": m.get("rating") or "",
                        "cover": cover,
                    })
                    if len(out) >= 100:
                        break
                return self._send(200, {"movies": out, "logged_in": True})

            if u.path == "/api/shows":
                term = (q.get("q", [""])[0]).strip().lower()
                cfg = load_config()
                x = Xtream(cfg)
                if not x.configured():
                    return self._send(200, {"shows": [], "logged_in": False})
                if not term:
                    return self._send(200, {"shows": [], "logged_in": True})
                try:
                    shows = get_xtream_series(cfg)
                except Exception as e:
                    return self._send(200, {"shows": [], "logged_in": True,
                                            "error": str(e)})
                words = [w for w in term.split() if w]
                out = []
                for s in shows:
                    name = str(s.get("name") or "")
                    if not all(w in name.lower() for w in words):
                        continue
                    cover = str(s.get("cover") or s.get("stream_icon") or "").strip()
                    if not cover.startswith(("http://", "https://")):
                        cover = ""
                    out.append({"series_id": s.get("series_id"), "name": name,
                                "cover": cover, "year": s.get("year") or "",
                                "rating": s.get("rating") or ""})
                    if len(out) >= 100:
                        break
                return self._send(200, {"shows": out, "logged_in": True})

            if u.path == "/api/show":
                series_id = (q.get("id", [""])[0]).strip()
                refresh = (q.get("refresh", ["0"])[0]) == "1"
                cfg = load_config()
                x = Xtream(cfg)
                if not (x.configured() and series_id):
                    return self._send(400, {"error": "bad request"})
                try:
                    data = x.series_info(series_id, refresh=refresh) or {}
                except Exception as e:
                    return self._send(200, {"error": str(e)})
                info = data.get("info") or {}
                if not isinstance(info, dict):
                    info = {}
                cover = str(info.get("cover") or info.get("movie_image") or "").strip()
                if not cover.startswith(("http://", "https://")):
                    cover = ""
                show_name = info.get("name") or info.get("title") or "Show"
                release_text = str(info.get("releaseDate") or info.get("release_date") or show_name)
                year_match = re.search(r"(?:19|20)\d{2}", release_text)
                show_year = year_match.group(0) if year_match else ""
                maze_covers = _tvmaze_season_covers(show_name, show_year)
                xtream_season_covers = {}
                raw_seasons = data.get("seasons") or []
                if isinstance(raw_seasons, list):
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
                raw_episodes = data.get("episodes") or {}
                if isinstance(raw_episodes, list):
                    grouped = {}
                    for ep in raw_episodes:
                        grouped.setdefault(str(ep.get("season") or 1), []).append(ep)
                    raw_episodes = grouped
                seasons = []
                for season_key, eps in raw_episodes.items():
                    if not isinstance(eps, list):
                        continue
                    normalized = []
                    for i, ep in enumerate(eps, 1):
                        normalized.append({
                            "id": ep.get("id"),
                            "episode_num": ep.get("episode_num") or i,
                            "title": _clean_episode_title(ep.get("title") or f"Episode {i}", show_name),
                            "extension": ep.get("container_extension") or "mp4",
                        })
                    normalized.sort(key=lambda ep: int(ep["episode_num"]) if str(ep["episode_num"]).isdigit() else 999999)
                    seasons.append({"number": season_key,
                                    "title": f"Season {season_key}",
                                    "cover": (maze_covers.get(str(season_key)) or
                                              xtream_season_covers.get(str(season_key)) or cover),
                                    "episodes": normalized})
                seasons.sort(key=lambda s: int(s["number"]) if str(s["number"]).isdigit() else 999999)
                return self._send(200, {"name": show_name,
                                        "cover": cover, "seasons": seasons})

            if u.path == "/api/search":
                term = (q.get("q", [""])[0]).strip()
                if not term:
                    return self._send(200, {"fixtures": [], "logged_in": False})
                cfg = load_config()
                countries = cfg.get("countries") or ["no", "uk", "us"]
                fixtures, src_err = search_fixtures(term, countries)
                x = Xtream(cfg)
                logged_in = x.configured()
                channels, cats = [], {}
                if logged_in:
                    try:
                        channels, cats = get_xtream_channels(cfg)
                    except Exception as e:
                        src_err.append(f"Xtream: {e}")
                        logged_in = False
                thr = float(cfg.get("match_threshold", 0.55) or 0.55)
                ppv_cats = ppv_categories(channels, cats) if logged_in else []
                out = []
                for f in fixtures:
                    matches = []
                    ppv_hits = []
                    streaming_only = False
                    if logged_in:
                        rows = match_channels(f["by_country"], channels, cats, thr)
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
                                "by_country": f["by_country"], "matches": matches,
                                "ppv_hits": ppv_hits, "streaming_only": streaming_only})
                return self._send(200, {"fixtures": out, "logged_in": logged_in,
                                        "source_errors": src_err,
                                        "ppv_categories": ppv_cats})

            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        if u.path == "/api/config":
            cfg = load_config()
            for k in ("xtream_host", "xtream_port", "xtream_user", "xtream_pass",
                      "stream_ext", "match_threshold", "countries", "start_section",
                      "check_shows_on_startup"):
                if k in payload:
                    cfg[k] = payload[k]
            save_config(cfg)
            _XT_CACHE.update({"ts": 0, "channels": [], "cats": {}})
            _VOD_CACHE.update({"ts": 0, "movies": []})
            _SERIES_CACHE.update({"ts": 0, "shows": []})
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

        if u.path == "/api/check_show_updates":
            cfg = load_config()
            try:
                result = refresh_favorite_show_episodes(cfg)
                return self._send(200, dict({"ok": True}, **result))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(502, {"error": str(e)})

        if u.path == "/api/refresh_all":
            cfg = load_config()
            x = Xtream(cfg)
            if not x.configured():
                return self._send(400, {"error": "Not configured"})
            try:
                channels, _cats = get_xtream_channels(cfg, force=True)
                movies = get_xtream_movies(cfg, force=True)
                shows = get_xtream_series(cfg, force=True)
                episode_result = refresh_favorite_show_episodes(cfg)
                return self._send(200, dict({"ok": True,
                    "channels": len(channels), "movies": len(movies),
                    "shows": len(shows)}, **episode_result))
            except Exception as e:
                return self._send(502, {"error": str(e)})

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
                                                "category": ch.get("category", "")})
                        have.add(sid)
            elif act == "toggle_channel":
                sid = str(payload.get("stream_id"))
                idx = next((i for i, c in enumerate(fav["channels"]) if str(c.get("stream_id")) == sid), -1)
                if idx >= 0:
                    fav["channels"].pop(idx)
                else:
                    fav["channels"].append({"stream_id": payload.get("stream_id"),
                                            "name": payload.get("name", ""),
                                            "category": payload.get("category", "")})
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
            elif act == "toggle_movie":
                movie = payload.get("movie") or {}
                sid = str(movie.get("stream_id", ""))
                idx = next((i for i, m in enumerate(fav["movies"])
                            if str(m.get("stream_id")) == sid), -1)
                if idx >= 0:
                    fav["movies"].pop(idx)
                elif sid:
                    fav["movies"].append({
                        "stream_id": movie.get("stream_id"),
                        "name": movie.get("name", ""),
                        "extension": movie.get("extension", "mp4"),
                        "year": movie.get("year", ""),
                        "rating": movie.get("rating", ""),
                        "cover": movie.get("cover", ""),
                    })
            elif act == "remove_movie":
                sid = str(payload.get("stream_id", ""))
                fav["movies"] = [m for m in fav["movies"]
                                 if str(m.get("stream_id")) != sid]
            elif act == "toggle_show":
                show = payload.get("show") or {}
                sid = str(show.get("series_id", ""))
                idx = next((i for i, s in enumerate(fav["shows"])
                            if str(s.get("series_id")) == sid), -1)
                if idx >= 0:
                    fav["shows"].pop(idx)
                elif sid:
                    fav["shows"].append({"series_id": show.get("series_id"),
                                          "name": show.get("name", ""),
                                          "cover": show.get("cover", ""),
                                          "year": show.get("year", ""),
                                          "rating": show.get("rating", "")})
            elif act == "remove_show":
                sid = str(payload.get("series_id", ""))
                fav["shows"] = [s for s in fav["shows"]
                                if str(s.get("series_id")) != sid]
            save_favorites(fav)
            return self._send(200, {"ok": True,
                                    "categories": fav["categories"],
                                    "channel_ids": [c.get("stream_id") for c in fav["channels"]],
                                    "movie_ids": [m.get("stream_id") for m in fav["movies"]],
                                    "show_ids": [s.get("series_id") for s in fav["shows"]]})

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
                    lines = ["@echo off\r\n",
                             "timeout /t 2 /nobreak >nul\r\n",
                             'move /y "' + new + '" "' + cur + '" >nul\r\n']
                    if relaunch:
                        lines.append('start "" ' + relaunch + "\r\n")
                    lines.append('del "%~f0"\r\n')
                    with open(helper, "w", encoding="utf-8") as f:
                        f.writelines(lines)
                    subprocess.Popen(["cmd", "/c", helper], creationflags=0x00000008)
                else:
                    helper = os.path.join(app_dir(), "_update.sh")
                    body = "#!/bin/sh\nsleep 2\nmv -f '" + new + "' '" + cur + "'\n"
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
                lines.append(f'#EXTINF:-1 group-title="{grp}",{name}')
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

def main():
    port = PORT
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except Exception:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    use_color = _enable_ansi()
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
    # Rotating pancake-themed prompt; open the browser when the user presses Enter.
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
    # Keep running until the window is closed / Ctrl+C.
    try:
        while True:
            _t_sleep(3600)
    except KeyboardInterrupt:
        print("\n  Stopping Olo's TVMate. Bye!")
        server.shutdown()

def _t_sleep(sec):
    import time as _t
    _t.sleep(sec)

if __name__ == "__main__":
    main()
