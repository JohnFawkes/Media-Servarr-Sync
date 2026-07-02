"""
Media Servarr Sync
------------------
Webhook receiver for Sonarr & Radarr that triggers targeted Plex folder scans.

Logic:
1. Webhooks are immediately placed in a deduplicated Queue.
2. A background worker processes the queue after a configurable delay.
3. (Optional) Rclone VFS cache is cleared/refreshed for the specific path
   when USE_RCLONE=true. Skip entirely if you don't use rclone.
4. Plex is triggered to perform a partial scan with retries on timeout.
5. Health endpoint exposes queue depth, worker status, and recent history.
"""

import os
import time
import json
import re
import threading
import requests
import urllib.parse
import queue
import logging
import signal
import sys
import sqlite3
import ipaddress
import math
import secrets as _secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import plexapi
from waitress import serve
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, make_response, send_file
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from plexapi.server import PlexServer
from plexapi.base import MediaContainer

# ---------------------------------------------------------------------------
# Timezone — resolved before logging so timestamps are correct from line 1
# ---------------------------------------------------------------------------
load_dotenv()

def _resolve_tz() -> timezone:
    tz_name = os.getenv("TZ", "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            print(f"WARNING: Unknown timezone '{tz_name}', falling back to UTC", flush=True)
    return timezone.utc

LOCAL_TZ = _resolve_tz()


def now_local() -> datetime:
    """Return the current time in the configured timezone."""
    return datetime.now(LOCAL_TZ)


# ---------------------------------------------------------------------------
# Logging — timezone-aware timestamps
# ---------------------------------------------------------------------------

class _TZFormatter(logging.Formatter):
    """Logging formatter that stamps records in LOCAL_TZ."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=LOCAL_TZ)
        return dt.strftime(datefmt or "%Y-%m-%dT%H:%M:%S%z")


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_TZFormatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)

app = Flask(__name__)
csrf = CSRFProtect(app)


@app.template_filter('datetimeformat')
def _datetimeformat(ts):
    """Format a Unix timestamp as a local date/time string."""
    try:
        return datetime.fromtimestamp(int(ts), tz=LOCAL_TZ).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return str(ts) if ts else ''

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_duration(duration_str) -> int:
    """Convert a duration string like '30s', '5m', '1h' to seconds."""
    if not duration_str:
        return 0
    s = str(duration_str).strip().lower()
    if s.isdigit():
        return int(s)
    match = re.match(r'^(\d+)([smhd])$', s)
    if not match:
        return 0
    value, unit = match.groups()
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return int(value) * multipliers[unit]


def normalize_path(path: str, is_dir: bool = True) -> str:
    if not path or not isinstance(path, str):
        return ""
    clean = path.strip().replace('\\', '/').rstrip('/')
    return clean + '/' if is_dir else clean


def parse_json_env(env_name: str) -> dict:
    raw = os.getenv(env_name, "{}").strip().strip("'")
    try:
        data = json.loads(raw)
        return {normalize_path(k, is_dir=False).lower(): v for k, v in data.items()}
    except Exception as exc:
        log.error("Failed to parse %s: %s", env_name, exc)
        return {}


def apply_path_mapping(path: str, mapping: dict, label: str, is_dir: bool = True) -> str:
    orig = normalize_path(path, is_dir=False)
    lower = orig.lower()
    for prefix in sorted(mapping.keys(), key=len, reverse=True):
        if lower.startswith(prefix):
            result = normalize_path(str(mapping[prefix]) + orig[len(prefix):], is_dir=is_dir)
            log.debug("[%s] Map: '%s' -> '%s'", label, orig, result)
            return result
    return normalize_path(orig, is_dir=is_dir)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Media server selection — at least one must be enabled. PLEX_ENABLED defaults to
# true so existing installs (which never set this var) keep working unchanged.
PLEX_ENABLED     = os.getenv("PLEX_ENABLED", "true").strip().lower() in ("1", "true", "yes")
JELLYFIN_ENABLED = os.getenv("JELLYFIN_ENABLED", "false").strip().lower() in ("1", "true", "yes")

PLEX_URL        = os.getenv("PLEX_URL", "http://127.0.0.1:32400").rstrip('/')
PLEX_TOKEN      = os.getenv("PLEX_TOKEN", "")
PLEX_TIMEOUT    = parse_duration(os.getenv("PLEX_TIMEOUT", "60")) or 60

JELLYFIN_URL      = os.getenv("JELLYFIN_URL", "http://127.0.0.1:8096").rstrip('/')
JELLYFIN_API_KEY  = os.getenv("JELLYFIN_API_KEY", "")
JELLYFIN_TIMEOUT  = parse_duration(os.getenv("JELLYFIN_TIMEOUT", "60")) or 60

# UI accent theme: "plex" (amber) or "jellyfin" (purple). When only one server type
# is enabled the theme always matches it. When both are enabled, this is just the
# default — the user can switch via the header toggle (persisted in localStorage).
UI_THEME        = os.getenv("UI_THEME", "").strip().lower()

PORT            = int(os.getenv("PORT", "5000"))
WEBHOOK_DELAY   = parse_duration(os.getenv("WEBHOOK_DELAY", "30"))
MINIMUM_AGE     = parse_duration(os.getenv("MINIMUM_AGE", "0"))
HISTORY_DAYS    = int(os.getenv("HISTORY_DAYS", "7"))
SYNC_COOLDOWN   = parse_duration(os.getenv("SYNC_COOLDOWN", "5m"))
MANUAL_USER     = os.getenv("MANUAL_USER", "admin")
MANUAL_PASS     = os.getenv("MANUAL_PASS", "password")
# Used to sign session cookies — set a long random string in your .env
SECRET_KEY      = os.getenv("SECRET_KEY", os.urandom(24).hex())
# Demo mode: enables /login/demo and populates pages with fake data for screenshots
DEMO_MODE       = os.getenv("DEMO_MODE", "false").strip().lower() in ("1", "true", "yes")

# Optional Sonarr/Radarr API credentials — used to look up quality profile names.
# If unset, quality_profile badges are simply omitted.
SONARR_URL     = os.getenv("SONARR_URL", "").rstrip('/')
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
RADARR_URL     = os.getenv("RADARR_URL", "").rstrip('/')
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "")

# Onboarding / offboarding links shown on the invite page
ONBOARD_WIKI_URL    = os.getenv("ONBOARD_WIKI_URL", "").rstrip('/')
ONBOARD_REQUEST_URL = os.getenv("ONBOARD_REQUEST_URL", "").rstrip('/')

# Rclone — set USE_RCLONE=false to skip all rclone calls entirely
USE_RCLONE        = os.getenv("USE_RCLONE", "false").strip().lower() in ("1", "true", "yes")
RCLONE_RC_URL     = os.getenv("RCLONE_RC_URL", "").rstrip('/')
RCLONE_RC_USER    = os.getenv("RCLONE_RC_USER", "")
RCLONE_RC_PASS    = os.getenv("RCLONE_RC_PASS", "")
RCLONE_MOUNT_ROOT = os.getenv("RCLONE_MOUNT_ROOT", "").rstrip('/')

plexapi.TIMEOUT = PLEX_TIMEOUT

# PlexAPI reads PLEXAPI_HEADER_IDENTIFIER from the environment automatically on import.
# We set it explicitly here as well so it's always applied regardless of import order.
PLEX_IDENTIFIER           = os.getenv("PLEXAPI_HEADER_IDENTIFIER", "media-servarr-sync")
plexapi.X_PLEX_IDENTIFIER = PLEX_IDENTIFIER
plexapi.X_PLEX_PRODUCT    = PLEX_IDENTIFIER

PATH_REPLACEMENTS        = parse_json_env("PATH_REPLACEMENTS")
RCLONE_PATH_REPLACEMENTS = parse_json_env("RCLONE_PATH_REPLACEMENTS")
SECTION_MAPPING          = parse_json_env("SECTION_MAPPING")
JELLYFIN_SECTION_MAPPING = parse_json_env("JELLYFIN_SECTION_MAPPING")

# Neither server type enabled — fall back to Plex so a misconfigured .env doesn't
# silently disable all scanning.
if not PLEX_ENABLED and not JELLYFIN_ENABLED:
    log.warning("Neither PLEX_ENABLED nor JELLYFIN_ENABLED is true — defaulting to Plex.")
    PLEX_ENABLED = True

# Effective UI theme: explicit UI_THEME env var wins; otherwise it's implied by
# whichever single server type is enabled; with both enabled it defaults to Plex
# (the user can still switch in the UI).
if UI_THEME not in ("plex", "jellyfin"):
    UI_THEME = "jellyfin" if (JELLYFIN_ENABLED and not PLEX_ENABLED) else "plex"
BOTH_ENABLED = PLEX_ENABLED and JELLYFIN_ENABLED

# Apply secret key now that config is loaded
app.secret_key = SECRET_KEY


@app.context_processor
def _inject_theme_context():
    """Make provider/theme flags available in every template without passing
    them explicitly at each render_template() call site."""
    return {
        "plex_enabled": PLEX_ENABLED,
        "jellyfin_enabled": JELLYFIN_ENABLED,
        "both_enabled": BOTH_ENABLED,
        "ui_theme": UI_THEME,
    }


# ---------------------------------------------------------------------------
# Quality profile lookup (Sonarr / Radarr API)
# ---------------------------------------------------------------------------

# Two-level cache: profile list (id→name) and per-series/movie id→profile_id.
# Both are loaded lazily on the first webhook and held in memory.
_qp_profiles:    dict[str, dict[int, str]] = {}   # arr_type → {profile_id: name}
_qp_series_map:  dict[str, dict[int, int]] = {}   # arr_type → {item_id: profile_id}
_qp_lock = threading.Lock()

_cf_cache: dict[str, list] = {}   # arr_type → [{id, name, ...}, ...]
_cf_lock  = threading.Lock()

# ---------------------------------------------------------------------------
# Geo-IP cache (server-side proxy to ipinfo.io)
# ---------------------------------------------------------------------------
_geo_cache: dict[str, dict] = {}   # ip → {status, city, country, lat, lon, ...}
_geo_cache_lock = threading.Lock()


def _is_private_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return True


def _sanitize_floats(obj):
    """Recursively replace NaN/Infinity float values with None (valid JSON)."""
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _get_quality_profile_name(arr_type: str, item_id: int) -> str:
    """Return the quality-profile name for a Sonarr series or Radarr movie.

    Returns "" if credentials are not configured or the API call fails.
    """
    url = SONARR_URL if arr_type == "sonarr" else RADARR_URL
    key = SONARR_API_KEY if arr_type == "sonarr" else RADARR_API_KEY
    if not url or not key:
        log.debug("[%s] Quality profile lookup skipped — %s_URL / %s_API_KEY not set",
                  arr_type.upper(), arr_type.upper(), arr_type.upper())
        return ""
    if not item_id:
        return ""

    headers = {"X-Api-Key": key}
    label   = arr_type.upper()

    with _qp_lock:
        # Ensure profile list is loaded
        if arr_type not in _qp_profiles:
            try:
                r = requests.get(f"{url}/api/v3/qualityprofile",
                                 headers=headers, timeout=5)
                r.raise_for_status()
                _qp_profiles[arr_type] = {p['id']: p['name'] for p in r.json()}
                log.info("[%s] Loaded %d quality profiles from API", label,
                         len(_qp_profiles[arr_type]))
            except Exception as exc:
                log.warning("[%s] Could not load quality profiles: %s", label, exc)
                _qp_profiles[arr_type] = {}

        if arr_type not in _qp_series_map:
            _qp_series_map[arr_type] = {}

        # Look up this item's profile_id if not yet cached
        if item_id not in _qp_series_map[arr_type]:
            endpoint = "series" if arr_type == "sonarr" else "movie"
            try:
                r = requests.get(f"{url}/api/v3/{endpoint}/{item_id}",
                                 headers=headers, timeout=5)
                r.raise_for_status()
                _qp_series_map[arr_type][item_id] = r.json().get('qualityProfileId', 0)
            except Exception as exc:
                log.warning("[%s] Could not fetch %s/%d for quality profile: %s",
                            label, endpoint, item_id, exc)
                return ""

        profile_id = _qp_series_map[arr_type].get(item_id, 0)
        return _qp_profiles[arr_type].get(profile_id, "") if profile_id else ""


def _refresh_custom_formats(arr_type: str) -> None:
    """Fetch and cache all custom formats defined in Sonarr/Radarr.

    Called on startup and every hour by the scheduler thread so the cache
    stays current as users add/remove custom formats in their arr instances.
    """
    url = SONARR_URL if arr_type == "sonarr" else RADARR_URL
    key = SONARR_API_KEY if arr_type == "sonarr" else RADARR_API_KEY
    if not url or not key:
        return
    try:
        r = requests.get(f"{url}/api/v3/customformat",
                         headers={"X-Api-Key": key}, timeout=10)
        r.raise_for_status()
        formats = r.json()
        with _cf_lock:
            _cf_cache[arr_type] = formats
        log.info("[%s] Custom format cache refreshed — %d formats",
                 arr_type.upper(), len(formats))
    except Exception as exc:
        log.warning("[%s] Could not refresh custom formats: %s", arr_type.upper(), exc)


def _fetch_custom_formats_for_file(arr_type: str, file_id: int) -> list[str]:
    """Return matched custom-format names for a specific episode/movie file.

    Calls /api/v3/episodefile/{id} (Sonarr) or /api/v3/moviefile/{id} (Radarr)
    which includes the resolved customFormats for that file — information not
    reliably present in the webhook payload.

    Returns [] if credentials are not configured or the API call fails.
    """
    if not file_id:
        return []
    url = SONARR_URL if arr_type == "sonarr" else RADARR_URL
    key = SONARR_API_KEY if arr_type == "sonarr" else RADARR_API_KEY
    if not url or not key:
        return []
    endpoint = "episodefile" if arr_type == "sonarr" else "moviefile"
    try:
        r = requests.get(f"{url}/api/v3/{endpoint}/{file_id}",
                         headers={"X-Api-Key": key}, timeout=5)
        r.raise_for_status()
        names = [cf['name'] for cf in r.json().get('customFormats', []) if cf.get('name')]
        log.info("[%s] API custom formats for %s/%d: %r",
                 arr_type.upper(), endpoint, file_id, names)
        return names
    except Exception as exc:
        log.warning("[%s] Could not fetch custom formats for %s/%d: %s",
                    arr_type.upper(), endpoint, file_id, exc)
        return []


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class SyncTask:
    raw_path: str
    rclone_host_path: str
    age_check_path: str
    mapped_folder: str
    label: str
    plex_section_id: Optional[str] = None
    jellyfin_library_id: Optional[str] = None
    episode: str = ""
    quality: str = ""
    custom_formats: str = ""   # JSON-encoded list of format name strings
    quality_profile: str = ""  # e.g. "HD-1080p" from Sonarr/Radarr quality profiles
    queued_at: float = field(default_factory=time.monotonic)

    def __eq__(self, other):
        return isinstance(other, SyncTask) and self.mapped_folder == other.mapped_folder

    def __hash__(self):
        return hash(self.mapped_folder)


class SyncHistory:
    """Thread-safe SQLite-backed sync history with configurable retention."""
    def __init__(self, db_path: str = "/data/sync_history.db", retention_days: int = 7):
        self._db_path = db_path
        self._retention_days = retention_days
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    label TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    duration_s REAL NOT NULL,
                    created_at INTEGER NOT NULL,
                    episode TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON sync_history(created_at DESC)")
            # Migrate existing databases that lack newer columns
            existing = {row[1] for row in conn.execute("PRAGMA table_info(sync_history)")}
            if 'episode' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN episode TEXT")
            if 'quality' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN quality TEXT DEFAULT ''")
            if 'custom_formats' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN custom_formats TEXT DEFAULT ''")
            if 'quality_profile' not in existing:
                conn.execute("ALTER TABLE sync_history ADD COLUMN quality_profile TEXT DEFAULT ''")
            conn.commit()

    def add(self, entry: dict):
        """Add a sync entry and prune old records."""
        with self._lock:
            cutoff = time.time() - (self._retention_days * 86400)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO sync_history
                        (ts, label, path, status, error, duration_s, created_at, episode, quality, custom_formats, quality_profile)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry['ts'],
                    entry['label'],
                    entry['path'],
                    entry['status'],
                    entry.get('error', ''),
                    entry['duration_s'],
                    time.time(),
                    entry.get('episode', ''),
                    entry.get('quality', ''),
                    entry.get('custom_formats', ''),
                    entry.get('quality_profile', ''),
                ))
                # Prune old entries
                conn.execute("DELETE FROM sync_history WHERE created_at < ?", (cutoff,))
                conn.commit()

    def get_recent(self, limit: int = 50, offset: int = 0,
                   search: str = "", status_filter: str = "",
                   quality_filter: str = "", profile_filter: str = "") -> list:
        """Get recent entries with optional path search and status filter."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                conditions, params = [], []
                if search:
                    conditions.append("path LIKE ?")
                    params.append(f"%{search}%")
                if status_filter:
                    conditions.append("status = ?")
                    params.append(status_filter)
                if quality_filter:
                    conditions.append("quality = ?")
                    params.append(quality_filter)
                if profile_filter:
                    conditions.append("quality_profile = ?")
                    params.append(profile_filter)
                query = (
                    "SELECT ts, label, path, status, error, duration_s, episode, quality, custom_formats, quality_profile"
                    " FROM sync_history"
                )
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

    def count(self, search: str = "", status_filter: str = "",
              quality_filter: str = "", profile_filter: str = "") -> int:
        """Get total number of entries matching optional search and status filter."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conditions, params = [], []
                if search:
                    conditions.append("path LIKE ?")
                    params.append(f"%{search}%")
                if status_filter:
                    conditions.append("status = ?")
                    params.append(status_filter)
                if quality_filter:
                    conditions.append("quality = ?")
                    params.append(quality_filter)
                if profile_filter:
                    conditions.append("quality_profile = ?")
                    params.append(profile_filter)
                query = "SELECT COUNT(*) FROM sync_history"
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                cursor = conn.execute(query, params)
                return cursor.fetchone()[0]

    def as_list(self) -> list:
        """For backward compatibility with old code."""
        return self.get_recent(limit=50)

    def get_stats(self) -> dict:
        """Return aggregate statistics for the current retention window."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute("""
                    SELECT
                        COUNT(*)                                              AS total,
                        SUM(CASE WHEN status = 'ok'     THEN 1 ELSE 0 END)  AS successful,
                        SUM(CASE WHEN status = 'error'  THEN 1 ELSE 0 END)  AS failed,
                        SUM(CASE WHEN label  = 'SONARR' THEN 1 ELSE 0 END)  AS sonarr,
                        SUM(CASE WHEN label  = 'RADARR' THEN 1 ELSE 0 END)  AS radarr,
                        SUM(CASE WHEN label  = 'MANUAL' THEN 1 ELSE 0 END)  AS manual,
                        ROUND(AVG(duration_s), 2)                            AS avg_duration_s
                    FROM sync_history
                """).fetchone()
                last = conn.execute("""
                    SELECT ts, status, label, path
                    FROM sync_history
                    ORDER BY created_at DESC
                    LIMIT 1
                """).fetchone()
        return {
            "total":          row[0] or 0,
            "successful":     row[1] or 0,
            "failed":         row[2] or 0,
            "sonarr":         row[3] or 0,
            "radarr":         row[4] or 0,
            "manual":         row[5] or 0,
            "avg_duration_s": row[6] or 0.0,
            "last_sync": {
                "ts":     last[0],
                "status": last[1],
                "label":  last[2],
                "path":   last[3],
            } if last else None,
        }


class InviteDB:
    """SQLite-backed invite system with per-user grant tracking and auto-expiry."""

    def __init__(self, db_path: str = "/data/invites.db"):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT UNIQUE NOT NULL,
                    label TEXT DEFAULT '',
                    section_ids TEXT DEFAULT '[]',
                    allow_sync INTEGER DEFAULT 0,
                    allow_channels INTEGER DEFAULT 1,
                    home_user INTEGER DEFAULT 0,
                    duration_days INTEGER DEFAULT 0,
                    max_uses INTEGER DEFAULT 1,
                    uses INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    link_expires_at INTEGER,
                    status TEXT DEFAULT 'active'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invite_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invite_id INTEGER NOT NULL,
                    plex_username TEXT NOT NULL,
                    accepted_at INTEGER NOT NULL,
                    access_expires_at INTEGER,
                    revoked INTEGER DEFAULT 0,
                    FOREIGN KEY (invite_id) REFERENCES invites(id)
                )
            """)
            # Migrate existing databases that lack newer columns.
            # provider: which media server the invite/grant targets ('plex' or 'jellyfin').
            # provider_user_id: Jellyfin's user Id (needed to delete the account on revoke);
            #   unused/NULL for Plex grants, which are revoked by username via removeFriend().
            existing_invites = {row[1] for row in conn.execute("PRAGMA table_info(invites)")}
            if 'provider' not in existing_invites:
                conn.execute("ALTER TABLE invites ADD COLUMN provider TEXT DEFAULT 'plex'")
            existing_grants = {row[1] for row in conn.execute("PRAGMA table_info(invite_grants)")}
            if 'provider' not in existing_grants:
                conn.execute("ALTER TABLE invite_grants ADD COLUMN provider TEXT DEFAULT 'plex'")
            if 'provider_user_id' not in existing_grants:
                conn.execute("ALTER TABLE invite_grants ADD COLUMN provider_user_id TEXT DEFAULT ''")
            conn.commit()

    def create(self, label: str, section_ids: list, allow_sync: bool,
               allow_channels: bool, home_user: bool, duration_days: int,
               max_uses: int, link_expires_days: int, provider: str = 'plex') -> str:
        token = _secrets.token_urlsafe(16)
        link_exp = int(time.time() + link_expires_days * 86400) if link_expires_days else None
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO invites
                        (token, label, section_ids, allow_sync, allow_channels, home_user,
                         duration_days, max_uses, created_at, link_expires_at, provider)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (token, label, json.dumps(section_ids), int(allow_sync),
                      int(allow_channels), int(home_user), duration_days,
                      max_uses, int(time.time()), link_exp, provider))
                conn.commit()
        return token

    def get(self, token: str) -> Optional[dict]:
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
                return dict(row) if row else None

    def list_all(self) -> list:
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM invites ORDER BY created_at DESC"
                ).fetchall()
                return [dict(r) for r in rows]

    def record_acceptance(self, invite_id: int, username: str, duration_days: int,
                          provider: str = 'plex', provider_user_id: str = ''):
        access_exp = int(time.time() + duration_days * 86400) if duration_days else None
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO invite_grants
                        (invite_id, plex_username, accepted_at, access_expires_at, provider, provider_user_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (invite_id, username, int(time.time()), access_exp, provider, provider_user_id))
                conn.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (invite_id,))
                conn.commit()

    def get_grants(self, invite_id: Optional[int] = None) -> list:
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                if invite_id is not None:
                    rows = conn.execute(
                        "SELECT * FROM invite_grants WHERE invite_id = ? ORDER BY accepted_at DESC",
                        (invite_id,)
                    ).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT g.*, i.label AS invite_label, i.token AS invite_token
                        FROM invite_grants g
                        JOIN invites i ON i.id = g.invite_id
                        ORDER BY g.accepted_at DESC
                    """).fetchall()
                return [dict(r) for r in rows]

    def revoke_invite(self, token: str):
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("UPDATE invites SET status = 'revoked' WHERE token = ?", (token,))
                conn.commit()

    def revoke_grant(self, grant_id: int):
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("UPDATE invite_grants SET revoked = 1 WHERE id = ?", (grant_id,))
                conn.commit()

    def get_expired_grants(self) -> list:
        now = int(time.time())
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT * FROM invite_grants
                    WHERE access_expires_at IS NOT NULL
                      AND access_expires_at < ?
                      AND revoked = 0
                """, (now,)).fetchall()
                return [dict(r) for r in rows]


history = SyncHistory(db_path="/data/sync_history.db", retention_days=HISTORY_DAYS)
invite_db = InviteDB(db_path="/data/invites.db")
sync_queue: queue.Queue = queue.Queue()
_in_flight: dict = {}           # mapped_folder -> SyncTask, currently queued or being processed
_cooldown: dict = {}            # mapped_folder -> expiry monotonic timestamp, recently completed
_in_flight_lock = threading.Lock()


def _prune_cooldown():
    """Remove expired cooldown entries. Must be called with _in_flight_lock held."""
    now = time.monotonic()
    expired = [k for k, v in _cooldown.items() if now >= v]
    for k in expired:
        del _cooldown[k]
_worker_alive = threading.Event()
_worker_alive.set()


# ---------------------------------------------------------------------------
# Plex connection (lazy, with reconnect)
# ---------------------------------------------------------------------------

_plex: Optional[PlexServer] = None
_plex_lock = threading.Lock()


def get_plex() -> Optional[PlexServer]:
    global _plex
    with _plex_lock:
        if _plex is None:
            try:
                _plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=PLEX_TIMEOUT)
                log.info("Connected to Plex: %s (timeout=%ds)", _plex.friendlyName, PLEX_TIMEOUT)
            except Exception as exc:
                log.error("Could not connect to Plex: %s", exc)
        return _plex


def invalidate_plex():
    """Force a reconnect on the next call."""
    global _plex
    with _plex_lock:
        _plex = None


# ---------------------------------------------------------------------------
# Jellyfin connection
# ---------------------------------------------------------------------------

def _jellyfin_headers() -> dict:
    return {"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "application/json"}


def get_jellyfin_info() -> Optional[dict]:
    """Return Jellyfin /System/Info as a dict, or None if unreachable."""
    if not JELLYFIN_ENABLED or not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return None
    try:
        r = requests.get(f"{JELLYFIN_URL}/System/Info",
                          headers=_jellyfin_headers(), timeout=JELLYFIN_TIMEOUT)
        if r.ok:
            return r.json()
        log.error("Jellyfin /System/Info returned %d", r.status_code)
    except requests.RequestException as exc:
        log.error("Could not connect to Jellyfin: %s", exc)
    return None


def jellyfin_scan_path(path: str, label: str) -> None:
    """Targeted scan: notify Jellyfin's real-time monitor that a path changed.

    Jellyfin has no direct 'scan this folder' endpoint like Plex's
    library.update(path=...). The equivalent mechanism is /Library/Media/Updated,
    which is the same API its own file-system watcher uses internally — passing
    a path queues an incremental (partial) library scan for that path only.
    """
    try:
        r = requests.post(
            f"{JELLYFIN_URL}/Library/Media/Updated",
            headers=_jellyfin_headers(),
            json={"Updates": [{"Path": path, "UpdateType": "Modified"}]},
            timeout=JELLYFIN_TIMEOUT,
        )
        if not r.ok:
            raise RuntimeError(f"Jellyfin returned {r.status_code}: {r.text[:200]}")
        log.info("[%s] [JELLYFIN] Targeted scan requested for '%s'", label, path)
    except requests.RequestException as exc:
        raise RuntimeError(f"Jellyfin connection error: {exc}") from exc


def jellyfin_full_library_scan(library_id: str) -> None:
    """Trigger a full recursive rescan of one Jellyfin library (CollectionFolder ItemId)."""
    r = requests.post(
        f"{JELLYFIN_URL}/Items/{library_id}/Refresh",
        headers=_jellyfin_headers(),
        params={"Recursive": "true", "ImageRefreshMode": "Default", "MetadataRefreshMode": "Default"},
        timeout=JELLYFIN_TIMEOUT,
    )
    r.raise_for_status()


def jellyfin_list_libraries() -> list:
    """Return Jellyfin libraries as [{id, title, type}, ...]."""
    r = requests.get(f"{JELLYFIN_URL}/Library/VirtualFolders",
                      headers=_jellyfin_headers(), timeout=JELLYFIN_TIMEOUT)
    r.raise_for_status()
    return [
        {"id": lib.get("ItemId", ""), "title": lib.get("Name", ""), "type": lib.get("CollectionType") or "mixed"}
        for lib in r.json()
    ]


def jellyfin_sessions() -> list:
    """Return active Jellyfin playback sessions, normalized to the same shape
    used for Plex sessions by /api/sessions (see api_sessions()).

    thumb_key is prefixed "jellyfin:<itemId>" so /api/thumb can tell it apart
    from a Plex library key and proxy it through Jellyfin's Images API instead.
    """
    r = requests.get(f"{JELLYFIN_URL}/Sessions", headers=_jellyfin_headers(), timeout=JELLYFIN_TIMEOUT)
    r.raise_for_status()

    result = []
    for s in r.json():
        item = s.get('NowPlayingItem')
        if not item:
            continue  # session exists but nothing is actively playing

        play_state = s.get('PlayState', {}) or {}
        transcode = s.get('TranscodingInfo')

        media_type = 'episode' if item.get('Type') == 'Episode' else \
                     'movie' if item.get('Type') == 'Movie' else (item.get('Type') or 'unknown').lower()

        season_ep = None
        if media_type == 'episode':
            season = item.get('ParentIndexNumber')
            episode = item.get('IndexNumber')
            if season is not None and episode is not None:
                season_ep = f"S{int(season):02d}E{int(episode):02d}"

        position = play_state.get('PositionTicks', 0) or 0
        runtime = item.get('RunTimeTicks', 0) or 0
        progress_pct = round((position / runtime * 100), 1) if runtime > 0 else 0

        if transcode:
            video_direct = transcode.get('IsVideoDirect', False)
            audio_direct = transcode.get('AudioDirect', transcode.get('IsAudioDirect', False))
            video_label = 'Direct Stream' if video_direct else 'Transcode'
            audio_label = 'Direct Stream' if audio_direct else 'Transcode'
            stream_type = video_label if video_label == audio_label else f"{video_label} · Audio {audio_label}"
        else:
            play_method = play_state.get('PlayMethod', 'DirectPlay')
            stream_type = {'DirectPlay': 'Direct Play', 'DirectStream': 'Direct Stream',
                           'Transcode': 'Transcode'}.get(play_method, play_method)

        video_resolution = None
        bitrate_kbps = None
        streams = item.get('MediaStreams', [])
        for ms in streams:
            if ms.get('Type') == 'Video':
                h = ms.get('Height')
                if h:
                    video_resolution = f"{h}p"
                if ms.get('BitRate'):
                    bitrate_kbps = round(ms['BitRate'] / 1000)
                break
        if not bitrate_kbps and transcode and transcode.get('Bitrate'):
            bitrate_kbps = round(transcode['Bitrate'] / 1000)

        thumb_item_id = (item.get('SeriesId') if media_type == 'episode' and item.get('SeriesId')
                         else item.get('Id'))

        result.append({
            'session_id':       str(s.get('Id', '')),
            'rating_key':       str(item.get('Id', '')),
            'plex_item_key':    '',
            'session_source':   'jellyfin',
            'type':             media_type,
            'title':            item.get('Name', ''),
            'year':             item.get('ProductionYear'),
            'show_title':       item.get('SeriesName'),
            'season_episode':   season_ep,
            'thumb_key':        f"jellyfin:{thumb_item_id}" if thumb_item_id else None,
            'progress_pct':     progress_pct,
            'view_offset_ms':   round(position / 10000) if position else 0,
            'duration_ms':      round(runtime / 10000) if runtime else 0,
            'state':            'paused' if play_state.get('IsPaused') else 'playing',
            'stream_type':      stream_type,
            'video_resolution': video_resolution,
            'bitrate_kbps':     bitrate_kbps,
            'user':             s.get('UserName', 'Unknown'),
            'player_device':    s.get('DeviceName', ''),
            'player_platform':  s.get('Client', ''),
            'player_product':   s.get('ApplicationVersion', ''),
            'player_address':   s.get('RemoteEndPoint', ''),
            'player_remote_address': s.get('RemoteEndPoint', ''),
        })
    return result


# ---------------------------------------------------------------------------
# Jellyfin user management (invites)
# ---------------------------------------------------------------------------
#
# Jellyfin has no "friend"/account-linking concept like Plex — inviting
# someone means creating an actual local Jellyfin user account on their
# behalf (the same approach used by third-party invite tools such as
# Wizarr), then restricting that account's library access via its Policy.

def jellyfin_create_user(username: str, password: str) -> dict:
    """Create a new local Jellyfin user. Returns the created user dict (includes 'Id')."""
    r = requests.post(f"{JELLYFIN_URL}/Users/New",
                       headers=_jellyfin_headers(),
                       json={"Name": username, "Password": password},
                       timeout=JELLYFIN_TIMEOUT)
    r.raise_for_status()
    return r.json()


def jellyfin_set_user_library_access(user_id: str, library_ids: list, allow_downloads: bool = False) -> None:
    """Restrict a Jellyfin user's library access. An empty library_ids list means
    'all libraries' (EnableAllFolders=True), matching the Plex invite UI's
    'leave unchecked to grant access to everything' behaviour.

    There is no standalone GET for a user's Policy (405) — the current policy
    is read from the full user object and posted back with our changes merged in.
    """
    r = requests.get(f"{JELLYFIN_URL}/Users/{user_id}", headers=_jellyfin_headers(), timeout=JELLYFIN_TIMEOUT)
    r.raise_for_status()
    policy = r.json().get('Policy', {})

    policy['EnableAllFolders'] = not bool(library_ids)
    policy['EnabledFolders'] = list(library_ids) if library_ids else []
    policy['EnableContentDownloading'] = allow_downloads
    policy['IsAdministrator'] = False

    r = requests.post(f"{JELLYFIN_URL}/Users/{user_id}/Policy",
                       headers=_jellyfin_headers(), json=policy, timeout=JELLYFIN_TIMEOUT)
    r.raise_for_status()


def jellyfin_delete_user(user_id: str) -> None:
    """Delete a local Jellyfin user account (used to revoke invite access)."""
    r = requests.delete(f"{JELLYFIN_URL}/Users/{user_id}", headers=_jellyfin_headers(), timeout=JELLYFIN_TIMEOUT)
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Rclone
# ---------------------------------------------------------------------------

def rclone_vfs_refresh(host_path: str, label: str):
    """Clear and async-refresh the rclone VFS cache for the given path.
    No-op when USE_RCLONE is false."""
    if not USE_RCLONE:
        return
    if not RCLONE_RC_URL:
        log.warning("[%s] [RCLONE] USE_RCLONE=true but RCLONE_RC_URL is not set — skipping.", label)
        return

    auth = (RCLONE_RC_USER, RCLONE_RC_PASS) if RCLONE_RC_USER else None
    full = host_path.rstrip('/')
    root = RCLONE_MOUNT_ROOT.rstrip('/')

    if root and full.startswith(root):
        target = full[len(root):].lstrip('/')
    else:
        target = full
        if root:
            log.warning("[%s] [RCLONE] Path '%s' is not under mount root '%s'", label, full, root)

    try:
        log.info("[%s] [RCLONE] Forget: '%s'", label, target)
        requests.post(f"{RCLONE_RC_URL}/vfs/forget",
                      json={"dir": target}, auth=auth, timeout=15)

        log.info("[%s] [RCLONE] Refresh (async): '%s'", label, target)
        res = requests.post(f"{RCLONE_RC_URL}/vfs/refresh",
                            json={"dir": target, "recursive": True, "_async": True},
                            auth=auth, timeout=15)
        if res.ok:
            log.info("[%s] [RCLONE] Queued job %s", label, res.json().get('jobid'))
        else:
            log.error("[%s] [RCLONE] Error %d: %s", label, res.status_code, res.text)
    except requests.RequestException as exc:
        log.error("[%s] [RCLONE] Connection error: %s", label, exc)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def sync_worker():
    log.info("Sync worker started")
    while _worker_alive.is_set():
        try:
            task: SyncTask = sync_queue.get(timeout=1)
        except queue.Empty:
            continue

        start = time.monotonic()
        status = "ok"
        error_msg = ""

        try:
            # Optional delay (e.g. wait for Sonarr to finish writing)
            if WEBHOOK_DELAY > 0:
                elapsed = time.monotonic() - task.queued_at
                remaining = WEBHOOK_DELAY - elapsed
                if remaining > 0:
                    log.info("[%s] [DELAY] Waiting %.0fs before processing...", task.label, remaining)
                    time.sleep(remaining)

            # Rclone VFS cache
            rclone_vfs_refresh(task.rclone_host_path, task.label)

            # Minimum file age check
            if MINIMUM_AGE > 0:
                check = task.age_check_path.rstrip('/')
                if os.path.exists(check):
                    age = time.time() - os.path.getmtime(check)
                    wait = MINIMUM_AGE - age
                    if wait > 0:
                        log.info("[%s] [AGE] File too young, waiting %ds...", task.label, int(wait))
                        time.sleep(wait)
                else:
                    log.warning("[%s] [AGE] Path not visible in container: %s", task.label, check)

            # Scan each enabled, matched target independently — a failure on one
            # provider doesn't prevent the other from being attempted.
            target_errors = []

            if PLEX_ENABLED and task.plex_section_id:
                try:
                    plex_instance = get_plex()
                    if not plex_instance:
                        raise RuntimeError("Plex not connected")
                    for attempt in range(1, 4):
                        try:
                            library = plex_instance.library.sectionByID(task.plex_section_id)
                            log.info("[%s] [SCAN] Attempt %d/3 → %s", task.label, attempt, task.mapped_folder)
                            library.update(path=task.mapped_folder)

                            time.sleep(20)
                            item = _find_plex_item(plex_instance, library, task)

                            if item:
                                log.info("[%s] [METADATA] Found '%s', analyzing...", task.label, item.title)
                                item.analyze()
                            else:
                                log.warning("[%s] [METADATA] Item not found in library DB.", task.label)
                            break

                        except Exception as exc:
                            if "timeout" in str(exc).lower() and attempt < 3:
                                log.warning("[%s] [PLEX] Timeout on attempt %d, retrying in 10s...", task.label, attempt)
                                # Reconnect in case the connection went stale
                                invalidate_plex()
                                plex_instance = get_plex()
                                time.sleep(10)
                            else:
                                raise
                except Exception as exc:
                    target_errors.append(f"Plex: {exc}")

            if JELLYFIN_ENABLED and task.jellyfin_library_id:
                try:
                    jellyfin_scan_path(task.mapped_folder, task.label)
                except Exception as exc:
                    target_errors.append(f"Jellyfin: {exc}")

            if target_errors:
                raise RuntimeError("; ".join(target_errors))

        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            log.error("[%s] [ERROR] %s", task.label, exc)
        finally:
            with _in_flight_lock:
                _in_flight.pop(task.mapped_folder, None)
                if SYNC_COOLDOWN > 0:
                    _cooldown[task.mapped_folder] = time.monotonic() + SYNC_COOLDOWN

            duration = round(time.monotonic() - start, 1)
            history.add({
                "ts": now_local().isoformat(),
                "label": task.label,
                "path": task.mapped_folder,
                "status": status,
                "error": error_msg,
                "duration_s": duration,
                "episode": task.episode,
                "quality": task.quality,
                "custom_formats": task.custom_formats,
                "quality_profile": task.quality_profile,
            })
            sync_queue.task_done()

    log.info("Sync worker stopped")


def custom_format_refresh_scheduler() -> None:
    """Background thread: refresh the custom-format cache from Sonarr/Radarr every hour.

    Runs an initial population 15 seconds after startup (giving the arrs time
    to be reachable), then repeats every hour.  Shutdown is honoured within one
    second via the shared _worker_alive event.
    """
    log.info("Custom format scheduler started")
    # Short initial delay so the arrs are likely up before we hit their API
    deadline = time.monotonic() + 15
    while _worker_alive.is_set() and time.monotonic() < deadline:
        time.sleep(1)

    while _worker_alive.is_set():
        for arr_type in ('sonarr', 'radarr'):
            _refresh_custom_formats(arr_type)
        # Wait 1 hour, checking shutdown signal each second
        deadline = time.monotonic() + 3600
        while _worker_alive.is_set() and time.monotonic() < deadline:
            time.sleep(1)

    log.info("Custom format scheduler stopped")


def invite_expiry_scheduler() -> None:
    """Background thread: auto-revoke expired Plex/Jellyfin access grants once per hour."""
    log.info("Invite expiry scheduler started")
    # Initial delay before first check
    deadline = time.monotonic() + 60
    while _worker_alive.is_set() and time.monotonic() < deadline:
        time.sleep(1)
    while _worker_alive.is_set():
        try:
            expired = invite_db.get_expired_grants()
            for grant in expired:
                provider = grant.get('provider') or 'plex'
                try:
                    if provider == 'jellyfin':
                        if grant.get('provider_user_id'):
                            jellyfin_delete_user(grant['provider_user_id'])
                            log.info("[INVITE] Auto-revoked expired Jellyfin access for '%s'", grant['plex_username'])
                    else:
                        plex_instance = get_plex()
                        if plex_instance:
                            account = plex_instance.myPlexAccount()
                            account.removeFriend(grant['plex_username'])
                            log.info("[INVITE] Auto-revoked expired Plex access for '%s'", grant['plex_username'])
                    invite_db.revoke_grant(grant['id'])
                except Exception as exc:
                    log.warning("[INVITE] Error auto-revoking %s access for '%s': %s",
                                provider, grant.get('plex_username'), exc)
        except Exception as exc:
            log.error("[INVITE] Expiry scheduler error: %s", exc)
        deadline = time.monotonic() + 3600
        while _worker_alive.is_set() and time.monotonic() < deadline:
            time.sleep(1)
    log.info("Invite expiry scheduler stopped")


def _find_plex_item(plex_instance, library, task: SyncTask):
    """Try to locate the newly-scanned item in Plex via path query then title search."""
    search_path = task.mapped_folder.rstrip('/')
    folder_name = os.path.basename(search_path)
    clean_title = re.sub(r'\s*[\(\{\[].*', '', folder_name).strip()

    for i in range(6):
        log.info("[%s] [METADATA] Lookup %d/6 for '%s'", task.label, i + 1, clean_title)
        try:
            encoded = urllib.parse.quote(search_path)
            xml = plex_instance.query(f"/library/sections/{task.plex_section_id}/all?path={encoded}")
            container = MediaContainer(plex_instance, xml)
            if container.metadata:
                return container.metadata[0]
        except Exception as exc:
            if "timeout" in str(exc).lower():
                raise

        # Fallback: title search + path match
        try:
            for res in library.search(title=clean_title):
                locs = res.locations if hasattr(res, 'locations') else []
                if not locs and hasattr(res, 'media'):
                    locs = [p.file for m in res.media for p in m.parts]
                if any(search_path in loc for loc in locs):
                    return res
        except Exception:
            pass

        time.sleep(20)

    return None


# ---------------------------------------------------------------------------
# Webhook processing
# ---------------------------------------------------------------------------

def _merge_episode_counts(existing: str, incoming: str) -> str:
    """Accumulate episode info when duplicate webhooks arrive for the same folder.

    Supports two storage formats:
    - Rich: JSON list of {"f": filename, "q": quality, "cf": [formats]} dicts
    - Legacy: JSON list of filename strings, or plain "N episodes" count string

    Rich objects from different webhooks are merged by episode key (SxxExx).
    If either side is rich, the result is always rich.
    """
    def _ep_key(name: str) -> str | None:
        m = re.search(r'[Ss](\d+)[Ee](\d+)', name)
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else None

    def _parse(ep: str) -> tuple:
        """Return (rich_list, plain_list, count_only)."""
        if not ep:
            return [], [], 0
        try:
            parsed = json.loads(ep)
            if isinstance(parsed, list) and parsed:
                if isinstance(parsed[0], dict):
                    return parsed, [], len(parsed)
                return [], [x for x in parsed if isinstance(x, str)], len(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
        m = re.match(r'^(\d+) episodes?$', ep.strip())
        if m:
            return [], [], int(m.group(1))
        return [], [ep], 1  # single filename string

    ex_rich, ex_plain, ex_count = _parse(existing)
    in_rich, in_plain, in_count = _parse(incoming)

    # If either side carries rich objects, merge as rich
    if ex_rich or in_rich:
        merged: list = list(ex_rich)
        seen = {_ep_key(e['f']): i for i, e in enumerate(merged) if _ep_key(e['f'])}
        # Promote any plain strings from the other side to minimal rich objects
        for name in ex_plain:
            if not any(e['f'] == name for e in merged):
                merged.append({"f": name, "q": "", "cf": []})
        for obj in in_rich:
            k = _ep_key(obj['f'])
            if k and k in seen:
                pass  # already have this episode — keep first-seen metadata
            elif not any(e['f'] == obj['f'] for e in merged):
                merged.append(obj)
        for name in in_plain:
            if not any(e['f'] == name for e in merged):
                merged.append({"f": name, "q": "", "cf": []})
        return json.dumps(merged)

    # Legacy path: plain filename lists / counts
    if ex_plain or in_plain:
        seen_keys = {_ep_key(ep) for ep in ex_plain}
        merged_plain = list(ex_plain)
        for ep in in_plain:
            k = _ep_key(ep)
            if k is None or k not in seen_keys:
                merged_plain.append(ep)
                if k:
                    seen_keys.add(k)
        if len(merged_plain) == 1:
            return merged_plain[0]
        return json.dumps(merged_plain)

    total = ex_count + in_count
    return f"{total} episodes" if total != 1 else (existing or incoming)


def _parse_episode_field(ep_str: str) -> tuple:
    """Parse a stored episode field into (display_str, episode_list, is_rich).

    Returns:
        display_str  — human-readable label ("2 episodes", filename, or "")
        episode_list — list of rich dicts {"f", "q", "cf"} or plain filename strings;
                       empty when only a count is known
        is_rich      — True when episode_list contains rich dicts with per-file metadata
    """
    if not ep_str:
        return "", [], False
    try:
        parsed = json.loads(ep_str)
        if isinstance(parsed, list) and parsed:
            if isinstance(parsed[0], dict):
                # Rich format: [{"f": filename, "q": quality, "cf": [formats]}, ...]
                display = parsed[0]['f'] if len(parsed) == 1 else f"{len(parsed)} episodes"
                return display, parsed, True
            # Legacy plain-string list
            if len(parsed) == 1:
                return parsed[0], [], False
            return f"{len(parsed)} episodes", parsed, False
    except (json.JSONDecodeError, ValueError):
        pass
    return ep_str, [], False


def _extract_file_meta(file_obj: dict) -> tuple:
    """Return (quality_name, custom_format_names) from an episodeFile / movieFile dict.

    Sonarr/Radarr webhooks send episodeFile.quality as a plain string (e.g. "WEBDL-1080p").
    The API object form {quality: {name: "..."}} is also handled for completeness.
    Note: customFormats are at the top-level payload, not inside the file object — callers
    must extract those separately from the raw event dict.
    """
    quality = ""
    q = file_obj.get('quality')
    if isinstance(q, str):
        quality = q                          # webhook plain-string form
    elif isinstance(q, dict):
        inner = q.get('quality', {})
        if isinstance(inner, dict):
            quality = inner.get('name', '') or ''
        elif isinstance(inner, str):
            quality = inner

    return quality, []


def _merge_qualities(existing: str, incoming: str) -> str:
    """Union two ' / '-separated quality strings, preserving order."""
    seen = []
    for q in (existing or '').split(' / ') + (incoming or '').split(' / '):
        q = q.strip()
        if q and q not in seen:
            seen.append(q)
    return ' / '.join(seen)


def _merge_custom_formats(existing: str, incoming: str) -> str:
    """Union two JSON-encoded custom-format name lists, preserving order."""
    def _to_list(s: str) -> list:
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, str)]
        except (json.JSONDecodeError, ValueError):
            pass
        return []

    merged = list(_to_list(existing))
    seen = set(merged)
    for fmt in _to_list(incoming):
        if fmt not in seen:
            merged.append(fmt)
            seen.add(fmt)
    return json.dumps(merged) if merged else ""


def enqueue_sync(raw_path: str, label: str, episode: str = "",
                 quality: str = "", custom_formats: str = "", quality_profile: str = ""):
    """Validate, map, and enqueue a sync task. Returns (response_dict, http_status)."""
    if not raw_path:
        return {"status": "skipped", "reason": "empty path"}, 200

    mapped_folder  = apply_path_mapping(raw_path, PATH_REPLACEMENTS, label, is_dir=True)
    rclone_path    = apply_path_mapping(raw_path, RCLONE_PATH_REPLACEMENTS, label, is_dir=False) \
                     if USE_RCLONE else ""
    age_check_path = mapped_folder

    # Section mapping — each enabled provider is matched independently against
    # its own path-prefix map. A path only needs to match at least one enabled
    # provider's mapping to be queued; matching providers are scanned, others skipped.
    comp = mapped_folder.rstrip('/').lower()

    plex_section_id = None
    if PLEX_ENABLED:
        plex_section_id = next(
            (SECTION_MAPPING[p] for p in sorted(SECTION_MAPPING, key=len, reverse=True) if comp.startswith(p)),
            None
        )

    jellyfin_library_id = None
    if JELLYFIN_ENABLED:
        jellyfin_library_id = next(
            (JELLYFIN_SECTION_MAPPING[p] for p in sorted(JELLYFIN_SECTION_MAPPING, key=len, reverse=True) if comp.startswith(p)),
            None
        )

    if not plex_section_id and not jellyfin_library_id:
        log.warning("[%s] [SKIP] No section mapping for '%s'", label, mapped_folder)
        return {"status": "skipped", "reason": "no section mapping"}, 200

    task = SyncTask(
        plex_section_id=plex_section_id,
        jellyfin_library_id=jellyfin_library_id,
        raw_path=raw_path,
        rclone_host_path=rclone_path,
        age_check_path=age_check_path,
        mapped_folder=mapped_folder,
        label=label,
        episode=episode,
        quality=quality,
        custom_formats=custom_formats,
        quality_profile=quality_profile,
    )

    # Deduplication: if same folder already queued or in cooldown, skip re-queuing
    with _in_flight_lock:
        if mapped_folder in _in_flight:
            existing_task = _in_flight[mapped_folder]
            if episode:
                merged = _merge_episode_counts(existing_task.episode, episode)
                existing_task.episode = merged
                log.info("[%s] [DEDUP] Already queued: %s — merged episode info to '%s'",
                         label, mapped_folder, merged)
            else:
                log.info("[%s] [DEDUP] Already queued: %s", label, mapped_folder)
            # Quality: union all distinct values across deduplicated events
            if quality:
                existing_task.quality = _merge_qualities(existing_task.quality, quality)
            # Profile: keep first non-empty value (profile is per-series, not per-file)
            if not existing_task.quality_profile and quality_profile:
                existing_task.quality_profile = quality_profile
            # Custom formats: union
            if custom_formats:
                existing_task.custom_formats = _merge_custom_formats(
                    existing_task.custom_formats, custom_formats)
            return {"status": "deduplicated"}, 200

        if SYNC_COOLDOWN > 0:
            _prune_cooldown()
            expiry = _cooldown.get(mapped_folder, 0)
            if time.monotonic() < expiry:
                log.info("[%s] [COOLDOWN] Recently synced, dropping follow-up event: %s", label, mapped_folder)
                return {"status": "deduplicated"}, 200

        _in_flight[mapped_folder] = task

    sync_queue.put(task)
    log.info("[%s] [QUEUE] Added (depth=%d): %s", label, sync_queue.qsize(), mapped_folder)
    return {"status": "queued"}, 200


def process_webhook(data: dict, instance_type: str):
    if not data:
        return jsonify({"error": "No payload"}), 400

    event = data.get('eventType', '')

    if event == "Test":
        label_up = instance_type.upper()
        file_obj  = data.get('episodeFile') or data.get('movieFile') or {}
        raw_qual  = file_obj.get('quality', 'NOT PRESENT in episodeFile/movieFile')
        raw_cf    = data.get('customFormats', 'NOT PRESENT at top level')
        log.info("[%s] Test webhook received — quality=%r  customFormats=%r  payload_keys=%s",
                 label_up, raw_qual, raw_cf, sorted(data.keys()))
        return jsonify({"status": "test_success"}), 200

    # Skip events where no useful scan can be performed:
    #   Grab              — file is queued in the download client, not on disk yet
    #   EpisodeFileDelete / MovieFileDelete — the deleted file's path ends up in
    #                       `episodeFile`, which would record the OLD filename in
    #                       history; upgrades are covered by the subsequent Download event
    #   SeriesDelete / MovieDelete — entire series/movie removed; Plex scheduled scans
    #                       will eventually catch this, a targeted partial scan won't help
    _SKIP = {
        'Grab',
        'EpisodeFileDelete', 'EpisodeFileDeleted', 'SeriesDelete',   # Sonarr
        'MovieFileDelete',   'MovieFileDeleted',   'MovieDelete',     # Radarr
    }
    if event in _SKIP:
        log.info("[%s] Skipping event type '%s' (no scan needed)", instance_type.upper(), event)
        return jsonify({"status": "skipped", "reason": "event type not handled"}), 200

    label = instance_type.upper()
    log.info("[%s] Processing event type '%s'", label, event)
    raw_path = ""
    episode = ""
    quality = ""
    custom_formats_list: list = []
    _episode_filename = ""        # single filename; used to build rich episode object after cf lookup
    _episode_files_meta: list = []  # [{f, q}] per file for batch episodeFiles events

    if 'movie' in data:
        raw_path = data['movie'].get('folderPath', '')
        mf = data.get('movieFile', {})
        if mf:
            quality, _ = _extract_file_meta(mf)
            rp = mf.get('relativePath', '')
            if rp:
                _episode_filename = rp.replace('\\', '/').split('/')[-1]
                episode = _episode_filename

    elif 'series' in data:
        series_path = data['series'].get('path', '')
        raw_path = series_path  # always scan the show root, never the season subfolder

        # Sonarr uses different keys depending on event type:
        #   episodeFile         — single episode download/delete
        #   episodeFiles        — batch/season-pack download
        #   renamedEpisodeFiles — rename events
        episode_files = []

        # Build the set of OLD filenames being replaced so we can discard stale episode info.
        # On upgrade events Sonarr populates `deletedFiles` with the file(s) that were replaced.
        # In some Sonarr versions the `episodeFile` field in the Download webhook can transiently
        # point to the old file before the rename completes; filtering against deletedFiles guards
        # against recording the replaced filename in sync history.
        _deleted_filenames: set = set()
        if data.get('isUpgrade'):
            for df in data.get('deletedFiles', []):
                dfn = df.get('relativePath', '').replace('\\', '/').split('/')[-1]
                if dfn:
                    _deleted_filenames.add(dfn)

        ef = data.get('episodeFile', {})
        if ef:
            rp = ef.get('relativePath', '')
            if rp:
                fn = rp.replace('\\', '/').split('/')[-1]
                if fn not in _deleted_filenames:
                    episode_files = [fn]
                    quality, _ = _extract_file_meta(ef)
                    _episode_filename = fn   # single known file — used for rich episode object
                else:
                    log.info("[%s] episodeFile '%s' matches a deletedFile — discarding stale episode info",
                             label, fn)

        if not episode_files:
            efs = data.get('episodeFiles', [])
            if efs:
                _quals = []
                for _f in efs:
                    _fn = _f.get('relativePath', '').replace('\\', '/').split('/')[-1]
                    _q, _ = _extract_file_meta(_f)
                    if _fn:
                        episode_files.append(_fn)
                        _episode_files_meta.append({"f": _fn, "q": _q})
                    if _q and _q not in _quals:
                        _quals.append(_q)
                quality = " / ".join(_quals)

        if not episode_files:
            refs = data.get('renamedEpisodeFiles', [])
            if refs:
                _quals = []
                for _f in refs:
                    _fn = _f.get('relativePath', '').replace('\\', '/').split('/')[-1]
                    _q, _ = _extract_file_meta(_f)
                    if _fn:
                        episode_files.append(_fn)
                        _episode_files_meta.append({"f": _fn, "q": _q})
                    if _q and _q not in _quals:
                        _quals.append(_q)
                quality = " / ".join(_quals)

        # episode placeholder — upgraded to a rich object after custom_formats are resolved below
        if len(episode_files) == 1:
            episode = episode_files[0]
        elif len(episode_files) > 1:
            episode = json.dumps(episode_files)   # fallback; replaced with rich object below if meta available

    # Log raw webhook fields for operator visibility.
    _raw_file = data.get('episodeFile') or data.get('movieFile') or {}
    _raw_qual = _raw_file.get('quality', '<MISSING>')
    _raw_cf   = data.get('customFormats', '<MISSING>')
    log.info("[%s] Raw webhook fields — episodeFile/movieFile.quality=%r  top-level customFormats=%r",
             label, _raw_qual, _raw_cf)

    # Resolve the file ID so we can query the arr API for accurate custom formats.
    # Webhooks sometimes omit customFormats or only include a subset; the
    # /episodefile/{id} and /moviefile/{id} endpoints always return the full
    # evaluated list for that file.
    _file_id = 0
    if data.get('movieFile'):
        _file_id = data['movieFile'].get('id', 0)
    elif data.get('episodeFile'):
        _file_id = data['episodeFile'].get('id', 0)
    elif data.get('episodeFiles'):
        _file_id = data['episodeFiles'][0].get('id', 0)
    elif data.get('renamedEpisodeFiles'):
        _file_id = data['renamedEpisodeFiles'][0].get('id', 0)

    api_cf = _fetch_custom_formats_for_file(instance_type, _file_id)
    if api_cf:
        custom_formats_list = api_cf
    else:
        # Fall back to whatever the webhook included (may be empty or partial)
        for cf in data.get('customFormats', []):
            if isinstance(cf, dict):
                name = cf.get('name', '')
            elif isinstance(cf, str):
                name = cf
            else:
                name = ''
            if name and name not in custom_formats_list:
                custom_formats_list.append(name)

    if quality or custom_formats_list:
        log.info("[%s] Captured quality=%r custom_formats=%r", label, quality, custom_formats_list)

    # Quality profile — not present in the webhook payload; requires an API round-trip.
    item_id = 0
    if 'movie' in data:
        item_id = data['movie'].get('id', 0)
    elif 'series' in data:
        item_id = data['series'].get('id', 0)
    quality_profile = _get_quality_profile_name(instance_type, item_id)

    custom_formats = json.dumps(custom_formats_list) if custom_formats_list else ""

    # Upgrade episode info to rich objects so per-file quality/formats are preserved.
    # Batch events (episodeFiles/renamedEpisodeFiles): each file gets its own quality;
    # custom formats are shared across the batch (all files from the same release).
    # Single-file events: straightforward one-item rich list.
    if _episode_files_meta and (quality or custom_formats_list):
        episode = json.dumps([
            {"f": m["f"], "q": m["q"], "cf": custom_formats_list}
            for m in _episode_files_meta
        ])
    elif _episode_filename and (quality or custom_formats_list):
        episode = json.dumps([{"f": _episode_filename, "q": quality, "cf": custom_formats_list}])

    result, status = enqueue_sync(raw_path, label, episode=episode,
                                  quality=quality, custom_formats=custom_formats,
                                  quality_profile=quality_profile)
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Demo mode — fake data for screenshots / public demos
# ---------------------------------------------------------------------------

_DEMO_INVITES = [
    {
        "id": 1, "token": "demoABC123", "label": "Friends & Family",
        "section_ids": '["1","2"]', "section_names": ["TV Shows", "Movies"],
        "allow_sync": 0, "allow_channels": 1, "home_user": 0,
        "duration_days": 0, "max_uses": 10, "uses": 3,
        "created_at": 1736000000, "link_expires_at": None, "status": "active",
        "link_expired": False, "max_reached": False, "is_active": True,
        "grants": [
            {"id": 1, "invite_id": 1, "plex_username": "alice", "accepted_at": 1736100000, "access_expires_at": None, "revoked": 0},
            {"id": 2, "invite_id": 1, "plex_username": "bob",   "accepted_at": 1736200000, "access_expires_at": None, "revoked": 0},
            {"id": 3, "invite_id": 1, "plex_username": "charlie","accepted_at": 1736300000,"access_expires_at": None, "revoked": 0},
        ],
    },
    {
        "id": 2, "token": "demoXYZ789", "label": "30-day trial",
        "section_ids": '["2"]', "section_names": ["Movies"],
        "allow_sync": 0, "allow_channels": 0, "home_user": 0,
        "duration_days": 30, "max_uses": 1, "uses": 1,
        "created_at": 1735000000, "link_expires_at": 1738000000, "status": "active",
        "link_expired": True, "max_reached": True, "is_active": False,
        "grants": [
            {"id": 4, "invite_id": 2, "plex_username": "diana", "accepted_at": 1735100000, "access_expires_at": 1737700000, "revoked": 0},
        ],
    },
]

_DEMO_SESSIONS = [
    {
        "user": "john", "title": "Ozymandias", "show": "Breaking Bad",
        "episode": "S05 · E14", "type": "episode", "player": "Plex for Apple TV",
        "state": "playing", "progress_pct": 42,
        "duration_str": "47:12", "position_str": "19:50",
        "quality": "1080p", "stream_type": "Direct Play", "transcode": False,
        "player_address": "192.168.1.42", "player_remote_address": "104.18.22.55",
        "thumb_key": "/demo/breaking-bad",
    },
    {
        "user": "sarah", "title": "Dune: Part Two", "show": None,
        "episode": None, "type": "movie", "player": "Plex Web",
        "state": "playing", "progress_pct": 68,
        "duration_str": "2:46:00", "position_str": "1:52:53",
        "quality": "4K", "stream_type": "Direct Stream", "transcode": False,
        "player_address": "192.168.1.77", "player_remote_address": "81.2.69.142",
        "thumb_key": "/demo/dune-part-two",
    },
    {
        "user": "mike", "title": "Napkins", "show": "The Bear",
        "episode": "S03 · E01", "type": "episode", "player": "Plex for Android",
        "state": "paused", "progress_pct": 15,
        "duration_str": "39:04", "position_str": "5:51",
        "quality": "720p", "stream_type": "Transcode", "transcode": True,
        "player_address": "192.168.1.15", "player_remote_address": "203.0.113.42",
        "thumb_key": "/demo/the-bear",
    },
]

# TMDB poster paths for demo sessions (w185 size)
_DEMO_POSTERS: dict[str, str] = {
    "/demo/breaking-bad":  "https://image.tmdb.org/t/p/w185/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
    "/demo/dune-part-two": "https://image.tmdb.org/t/p/w185/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg",
    "/demo/the-bear":      "https://image.tmdb.org/t/p/w185/sHFlbKS3WLqMnp9t2ghADIJFnuQ.jpg",
}

# Fake geo data keyed by demo remote IP — returned directly to avoid hitting ipinfo.io
_DEMO_GEO: dict[str, dict] = {
    "104.18.22.55": {"status": "success", "query": "104.18.22.55", "city": "New York", "regionName": "New York", "country": "United States", "countryCode": "US", "lat": 40.7128, "lon": -74.0060},
    "81.2.69.142":  {"status": "success", "query": "81.2.69.142",  "city": "London",   "regionName": "England",  "country": "United Kingdom", "countryCode": "GB", "lat": 51.5074, "lon": -0.1278},
    "203.0.113.42": {"status": "success", "query": "203.0.113.42", "city": "Sydney",   "regionName": "New South Wales", "country": "Australia", "countryCode": "AU", "lat": -33.8688, "lon": 151.2093},
}


def _demo_history() -> list:
    """Return fake sync history entries for demo/screenshot mode."""
    from datetime import timedelta
    now = now_local()
    raw = [
        {
            "ts": (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "SONARR", "status": "ok", "error": "", "duration_s": 3.2,
            "path": "/media/tv/Breaking Bad (2008)/Season 5/",
            "episode": "Breaking.Bad.S05E14.Ozymandias.1080p.BluRay.mkv",
            "quality": "Bluray-1080p", "custom_formats": '["HDR", "DV"]',
            "quality_profile": "HD-1080p Remux",
        },
        {
            "ts": (now - timedelta(minutes=18)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "SONARR", "status": "ok", "error": "", "duration_s": 4.1,
            "path": "/media/tv/The Bear (2022)/Season 3/",
            "episode": '[{"f":"The.Bear.S03E01.Napkins.1080p.WEB-DL.mkv","q":"WEB-DL-1080p","cf":["HLG","ATMOS"]},{"f":"The.Bear.S03E02.Bolognese.1080p.WEB-DL.mkv","q":"WEB-DL-1080p","cf":["HLG"]},{"f":"The.Bear.S03E03.Doors.And.Windows.720p.WEB-DL.mkv","q":"WEB-DL-720p","cf":[]}]',
            "quality": "WEB-DL-1080p", "custom_formats": '["HLG"]',
            "quality_profile": "WEB-1080p",
        },
        {
            "ts": (now - timedelta(hours=1, minutes=5)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "RADARR", "status": "ok", "error": "", "duration_s": 5.7,
            "path": "/media/movies/Dune Part Two (2024)/",
            "episode": "Dune.Part.Two.2024.2160p.BluRay.REMUX.HEVC.DV.HDR10Plus.TrueHD.Atmos.7.1.mkv",
            "quality": "Bluray-2160p",
            "custom_formats": '["HDR10+", "DV", "Remux"]',
            "quality_profile": "UHD Remux",
        },
        {
            "ts": (now - timedelta(hours=2, minutes=32)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "SONARR", "status": "ok", "error": "", "duration_s": 2.8,
            "path": "/media/tv/Severance (2022)/Season 2/",
            "episode": "Severance.S02E04.Woes.Hollow.1080p.ATVP.WEB-DL.mkv",
            "quality": "WEB-DL-1080p", "custom_formats": '["ATMOS"]',
            "quality_profile": "WEB-1080p",
        },
        {
            "ts": (now - timedelta(hours=4, minutes=17)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "RADARR", "status": "error",
            "error": ("ReadTimeout: Jellyfin did not respond within 60s"
                      if (JELLYFIN_ENABLED and not PLEX_ENABLED)
                      else "ReadTimeout: Plex did not respond within 60s"),
            "duration_s": 60.0,
            "path": "/media/movies/Avatar The Way of Water (2022)/",
            "episode": "", "quality": "Bluray-2160p",
            "custom_formats": '["HDR10", "DV"]', "quality_profile": "UHD Remux",
        },
        {
            "ts": (now - timedelta(hours=6, minutes=44)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "SONARR", "status": "ok", "error": "", "duration_s": 3.5,
            "path": "/media/tv/Shogun (2024)/Season 1/",
            "episode": "Shogun.S01E05.Crimson.Sky.1080p.DSNP.WEB-DL.mkv",
            "quality": "WEB-DL-1080p", "custom_formats": '[]',
            "quality_profile": "WEB-1080p",
        },
        {
            "ts": (now - timedelta(hours=9, minutes=11)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "RADARR", "status": "ok", "error": "", "duration_s": 4.3,
            "path": "/media/movies/Oppenheimer (2023)/",
            "episode": "Oppenheimer.2023.1080p.BluRay.REMUX.AVC.TrueHD.Atmos.7.1.mkv",
            "quality": "Bluray-1080p",
            "custom_formats": '["ATMOS", "TrueHD"]', "quality_profile": "HD-1080p Remux",
        },
        {
            "ts": (now - timedelta(hours=11, minutes=58)).strftime("%Y-%m-%dT%H:%M:%S"),
            "label": "MANUAL", "status": "ok", "error": "", "duration_s": 2.1,
            "path": "/media/tv/The Last of Us (2023)/Season 1/",
            "episode": "The.Last.of.Us.S01E01.When.Youre.Lost.in.the.Darkness.1080p.WEB-DL.mkv",
            "quality": "WEB-DL-1080p", "custom_formats": '[]', "quality_profile": "",
        },
    ]
    for item in raw:
        display, ep_list, ep_rich = _parse_episode_field(item.get('episode', ''))
        item['episode_display'] = display
        item['episode_list'] = ep_list
        item['episode_rich'] = ep_rich
        cf_raw = item.get('custom_formats', '') or ''
        try:
            item['custom_format_list'] = json.loads(cf_raw) if cf_raw else []
        except (json.JSONDecodeError, ValueError):
            item['custom_format_list'] = []
    return raw


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/webhook/sonarr', methods=['POST'])
@csrf.exempt
def webhook_sonarr():
    return process_webhook(request.get_json(silent=True) or {}, "sonarr")


@app.route('/webhook/radarr', methods=['POST'])
@csrf.exempt
def webhook_radarr():
    return process_webhook(request.get_json(silent=True) or {}, "radarr")


@app.route('/login/demo')
def login_demo():
    if not DEMO_MODE:
        return redirect(url_for('login'))
    session.permanent = False
    session['authenticated'] = True
    session['demo'] = True
    return redirect(url_for('manual_webhook'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == MANUAL_USER and password == MANUAL_PASS:
            session.permanent = False
            session['authenticated'] = True
            return redirect(url_for('manual_webhook'))
        error = "Invalid username or password."
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/', methods=['GET', 'POST'])
@requires_auth
def manual_webhook():
    message = ""
    msg_class = "info"

    if request.method == 'POST':
        raw_path = request.form.get('path', '').strip()
        if raw_path:
            result, _ = enqueue_sync(raw_path, "MANUAL")
            status = result.get("status")
            if status == "queued":
                message = f"✓ Sync queued for: {result.get('path', raw_path)}"
                msg_class = "success"
            elif status == "deduplicated":
                message = f"⚠ Already in queue: {raw_path}"
                msg_class = "warn"
            else:
                message = f"✗ {result.get('reason', 'Unknown error')} for: {raw_path}"
                msg_class = "error"
        else:
            message = "✗ No path provided."
            msg_class = "error"

    # Filters
    search_q = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    quality_filter = request.args.get('quality', '').strip()
    profile_filter = request.args.get('profile', '').strip()
    if status_filter not in ('ok', 'error', ''):
        status_filter = ''

    # Pagination
    page = max(1, int(request.args.get('page', 1)))
    per_page = 25
    offset = (page - 1) * per_page

    if session.get('demo'):
        recent = _demo_history()
        total_count = len(recent)
        total_pages = 1
    else:
        recent = history.get_recent(limit=per_page, offset=offset, search=search_q,
                                    status_filter=status_filter, quality_filter=quality_filter,
                                    profile_filter=profile_filter)
        total_count = history.count(search=search_q, status_filter=status_filter,
                                    quality_filter=quality_filter, profile_filter=profile_filter)
        total_pages = (total_count + per_page - 1) // per_page

    for item in recent:
        display, ep_list, ep_rich = _parse_episode_field(item.get('episode', ''))
        item['episode_display'] = display
        item['episode_list'] = ep_list
        item['episode_rich'] = ep_rich
        cf_raw = item.get('custom_formats', '') or ''
        try:
            item['custom_format_list'] = json.loads(cf_raw) if cf_raw else []
        except (json.JSONDecodeError, ValueError):
            item['custom_format_list'] = []

    def _qs(**kw):
        return urllib.parse.urlencode([(k, v) for k, v in kw.items() if v])

    # search_qs: preserves q + quality + profile (used by status pills)
    search_qs    = _qs(q=search_q, quality=quality_filter, profile=profile_filter)
    # filter_qs: all active filters (used by pagination)
    filter_qs    = _qs(q=search_q, status=status_filter, quality=quality_filter, profile=profile_filter)
    # no_quality_qs: all filters except quality (used by quality tag links + clear quality pill)
    no_quality_qs = _qs(q=search_q, status=status_filter, profile=profile_filter)
    # no_profile_qs: all filters except profile (used by profile tag links + clear profile pill)
    no_profile_qs = _qs(q=search_q, status=status_filter, quality=quality_filter)

    return render_template(
        'manual_ui.html',
        message=message,
        msg_class=msg_class,
        history=recent,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        retention_days=HISTORY_DAYS,
        search_q=search_q,
        status_filter=status_filter,
        quality_filter=quality_filter,
        profile_filter=profile_filter,
        search_qs=search_qs,
        filter_qs=filter_qs,
        no_quality_qs=no_quality_qs,
        no_profile_qs=no_profile_qs,
        plex_url=PLEX_URL,
        jellyfin_url=JELLYFIN_URL,
        demo=session.get('demo', False),
    )


@app.route('/api/server-stats', methods=['GET'])
@requires_auth
def api_server_stats():
    """Return Plex server CPU, RAM, and bandwidth stats."""
    if session.get('demo'):
        import math
        t = time.time()
        # Produce smoothly varying fake values using sine waves so each poll looks different
        host_cpu  = round(18 + 12 * math.sin(t / 13) + 5 * math.sin(t / 7), 1)
        plex_cpu  = round(8  +  6 * math.sin(t / 17) + 3 * math.sin(t / 5), 1)
        host_ram  = round(54 +  8 * math.sin(t / 23) + 4 * math.sin(t / 11), 1)
        plex_ram  = round(22 +  5 * math.sin(t / 19) + 2 * math.sin(t / 9),  1)
        lan_bytes = int((820 + 180 * math.sin(t / 11)) * 1024 * 1024)
        wan_bytes = int((340 +  90 * math.sin(t / 7))  * 1024 * 1024)
        return jsonify({
            "resources": {
                "host_cpu_pct":     host_cpu,
                "process_cpu_pct":  plex_cpu,
                "host_ram_pct":     host_ram,
                "process_ram_pct":  plex_ram,
                "at": int(t),
            },
            "bandwidth": {
                "lan_bytes":   lan_bytes,
                "wan_bytes":   wan_bytes,
                "total_bytes": lan_bytes + wan_bytes,
            },
        })
    plex = get_plex()
    if plex is None:
        return jsonify({"error": "Plex unavailable"}), 503

    _plex_headers = {"Accept": "application/json", "X-Plex-Token": PLEX_TOKEN}
    result = {"resources": None, "bandwidth": None}
    try:
        r = requests.get(
            f"{PLEX_URL}/statistics/resources",
            headers=_plex_headers,
            params={"timespan": 6},
            timeout=5,
        )
        if r.ok:
            mc = r.json().get("MediaContainer", {})
            # Plex nests StatisticsResources under a Device list
            items = mc.get("StatisticsResources", [])
            if not items:
                for dev in mc.get("Device", []):
                    items.extend(dev.get("StatisticsResources", []))
            log.debug("/statistics/resources returned %d items; keys=%s", len(items), list(mc.keys()))
            if items:
                latest = items[-1]
                result["resources"] = {
                    "host_cpu_pct":     round(float(latest.get("hostCpuUtilization", 0)), 1),
                    "process_cpu_pct":  round(float(latest.get("processCpuUtilization", 0)), 1),
                    "host_ram_pct":     round(float(latest.get("hostMemoryUtilization", 0)), 1),
                    "process_ram_pct":  round(float(latest.get("processMemoryUtilization", 0)), 1),
                    "at":               latest.get("timespan", 0),
                }
        else:
            log.debug("/statistics/resources HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("Failed to fetch /statistics/resources: %s", exc)

    try:
        r = requests.get(
            f"{PLEX_URL}/statistics/bandwidth",
            headers=_plex_headers,
            params={"timespan": 6},
            timeout=5,
        )
        if r.ok:
            items = r.json().get("MediaContainer", {}).get("StatisticsBandwidth", [])
            lan_bytes = sum(int(i.get("bytes", 0)) for i in items if i.get("lan") is True)
            wan_bytes = sum(int(i.get("bytes", 0)) for i in items if i.get("lan") is False)
            total_bytes = lan_bytes + wan_bytes
            result["bandwidth"] = {
                "lan_bytes": lan_bytes,
                "wan_bytes": wan_bytes,
                "total_bytes": total_bytes,
            }
    except Exception as exc:
        log.debug("Failed to fetch /statistics/bandwidth: %s", exc)

    return jsonify(result)


@app.route('/health', methods=['GET'])
def health():
    plex_ok = get_plex() is not None if PLEX_ENABLED else None
    jellyfin_ok = get_jellyfin_info() is not None if JELLYFIN_ENABLED else None

    enabled_ok = [ok for ok in (plex_ok, jellyfin_ok) if ok is not None]
    all_ok = bool(enabled_ok) and all(enabled_ok)

    resp = {
        "status": "ok" if all_ok else "degraded",
        "plex_enabled": PLEX_ENABLED,
        "plex_connected": plex_ok,
        "jellyfin_enabled": JELLYFIN_ENABLED,
        "jellyfin_connected": jellyfin_ok,
        "rclone_enabled": USE_RCLONE,
        "queue_depth": sync_queue.qsize(),
        "worker_alive": _worker_alive.is_set(),
    }
    return jsonify(resp), 200 if all_ok else 207


_plex_update_cache: dict = {"ts": 0, "data": None}
_PLEX_UPDATE_TTL = 3600  # 1 hour


@app.route('/api/plex-update', methods=['GET'])
@requires_auth
def api_plex_update():
    """Check whether a Plex Media Server update is available (cached 1 h)."""
    now = time.time()
    if now - _plex_update_cache["ts"] < _PLEX_UPDATE_TTL and _plex_update_cache["data"] is not None:
        return jsonify(_plex_update_cache["data"])

    plex = get_plex()
    if plex is None:
        return jsonify({"available": False, "error": "plex_unavailable"}), 503

    try:
        releases = plex.query("/updater/status")
        # The /updater/status response is an XML MediaContainer; PlexAPI parses
        # it into an ElementTree element. The update info lives in <Release> children.
        available = False
        version = ""
        release_notes_url = ""
        if releases is not None:
            for rel in releases:
                if rel.tag == "Release":
                    available = True
                    version = rel.attrib.get("version", "")
                    release_notes_url = rel.attrib.get("fixed", "")
                    break
        data = {"available": available, "version": version, "release_notes_url": release_notes_url}
    except Exception as exc:
        log.debug("Plex update check failed: %s", exc)
        data = {"available": False}

    _plex_update_cache["ts"] = now
    _plex_update_cache["data"] = data
    return jsonify(data)


@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Aggregate stats endpoint — designed for Homepage customapi widget.

    Example Homepage widget config:
        widget:
          type: customapi
          url: http://<host>:5000/api/stats
          refreshInterval: 30000
          mappings:
            - field: syncs.total    label: Total    format: number
            - field: syncs.ok       label: Success  format: number
            - field: syncs.failed   label: Failed   format: number
            - field: queue.depth    label: Queued   format: number
    """
    stats = history.get_stats()
    last  = stats["last_sync"]
    with _in_flight_lock:
        in_flight_count = len(_in_flight)
    return jsonify({
        "syncs": {
            "total":          stats["total"],
            "ok":             stats["successful"],
            "failed":         stats["failed"],
            "sonarr":         stats["sonarr"],
            "radarr":         stats["radarr"],
            "manual":         stats["manual"],
            "avg_duration_s": stats["avg_duration_s"],
        },
        "queue": {
            "depth":     sync_queue.qsize(),
            "in_flight": in_flight_count,
        },
        "worker": {
            "alive": _worker_alive.is_set(),
        },
        "last_sync": {
            "at":     last["ts"]     if last else None,
            "status": last["status"] if last else None,
            "label":  last["label"]  if last else None,
            "path":   last["path"]   if last else None,
        },
        "retention_days": HISTORY_DAYS,
    })



# ---------------------------------------------------------------------------
# Sessions / Now Playing API
# ---------------------------------------------------------------------------

@app.route('/api/sessions', methods=['GET'])
@requires_auth
def api_sessions():
    """Return current Plex and/or Jellyfin sessions for the Now Playing dashboard."""
    if session.get('demo'):
        # Demo player labels/session_source reflect whichever provider(s) are
        # actually enabled, so Jellyfin-only screenshots don't show "Plex for ..."
        demo_source = 'jellyfin' if (JELLYFIN_ENABLED and not PLEX_ENABLED) else 'plex'
        demo_result = []
        for s in _DEMO_SESSIONS:
            player_label = s['player'].replace('Plex', 'Jellyfin') if demo_source == 'jellyfin' else s['player']
            demo_result.append({
                'session_id': f"demo-{s['user']}",
                'rating_key': '', 'plex_item_key': '', 'session_source': demo_source, 'type': s['type'],
                'title': s['title'], 'year': None,
                'show_title': s.get('show'), 'season_episode': s.get('episode'),
                'thumb_key': s.get('thumb_key'),
                'progress_pct': s['progress_pct'],
                'view_offset_ms': 0, 'duration_ms': 0,
                'state': s['state'], 'stream_type': s['stream_type'],
                'video_resolution': s['quality'].replace('p','') if s['quality'].endswith('p') else s['quality'],
                'bitrate_kbps': None,
                'user': s['user'],
                'player_device': player_label, 'player_platform': '', 'player_product': '',
                'player_address': s.get('player_address', ''), 'player_remote_address': s.get('player_remote_address', ''),
            })
        return jsonify({'sessions': demo_result, 'machine_id': '', 'jellyfin_server_id': 'demo-jellyfin-server'})

    result = []
    errors = []
    machine_id = ''
    jellyfin_server_id = ''

    if JELLYFIN_ENABLED:
        try:
            result.extend(jellyfin_sessions())
            jf_info = get_jellyfin_info()
            if jf_info:
                jellyfin_server_id = jf_info.get('Id', '')
        except Exception as exc:
            log.error("Error fetching Jellyfin sessions: %s", exc)
            errors.append('jellyfin')

    if not PLEX_ENABLED:
        if errors and not result:
            return jsonify({'error': 'Failed to retrieve sessions.', 'sessions': []}), 500
        return jsonify({'sessions': result, 'machine_id': machine_id, 'jellyfin_server_id': jellyfin_server_id})

    plex_instance = get_plex()
    if not plex_instance:
        if result:
            # Jellyfin sessions are still valid even though Plex is down
            return jsonify({'sessions': result, 'machine_id': machine_id, 'jellyfin_server_id': jellyfin_server_id})
        return jsonify({"error": "Plex not connected", "sessions": []}), 503
    try:
        sessions_data = plex_instance.sessions()
        machine_id = getattr(plex_instance, 'machineIdentifier', '')
        for s in sessions_data:
            players = getattr(s, 'players', [])
            player = players[0] if players else None
            transcode_sessions = getattr(s, 'transcodeSessions', [])
            ts = transcode_sessions[0] if transcode_sessions else None

            media_type = getattr(s, 'type', 'unknown')

            season_ep = None
            if media_type == 'episode':
                season = getattr(s, 'parentIndex', None)
                episode = getattr(s, 'index', None)
                if season and episode:
                    season_ep = f"S{int(season):02d}E{int(episode):02d}"

            # Prefer show poster for episodes
            if media_type == 'episode':
                thumb_key = getattr(s, 'grandparentThumb', None) or getattr(s, 'thumb', None)
            else:
                thumb_key = getattr(s, 'thumb', None)

            view_offset = getattr(s, 'viewOffset', 0) or 0
            duration = getattr(s, 'duration', 0) or 0
            progress_pct = round((view_offset / duration * 100), 1) if duration > 0 else 0

            if ts:
                vd = getattr(ts, 'videoDecision', 'directplay')
                ad = getattr(ts, 'audioDecision', 'directplay')
                hw = vd == 'transcode' and (getattr(ts, 'transcodeHwEncoding', False) or getattr(ts, 'transcodeHwRequested', False))
                _DEC = {'directplay': 'Direct Play', 'copy': 'Direct Stream', 'transcode': 'Transcode'}
                video_label = 'HW Transcode' if hw else _DEC.get(vd, vd.title() if vd else 'Direct Play')
                audio_label = _DEC.get(ad, ad.title() if ad else 'Direct Play')
                stream_type = video_label if video_label == audio_label else f"{video_label} · Audio {audio_label}"
            else:
                stream_type = 'Direct Play'

            media_parts = getattr(s, 'media', [])
            video_resolution = None
            bitrate = None
            if media_parts:
                video_resolution = getattr(media_parts[0], 'videoResolution', None)
                bitrate = getattr(media_parts[0], 'bitrate', None)

            usernames = getattr(s, 'usernames', [])
            username = usernames[0] if usernames else getattr(s, 'username', 'Unknown')

            player_address = getattr(player, 'address', '') or '' if player else ''
            # PlexAPI's PlexClient._loadData doesn't map remotePublicAddress, so
            # getattr() always returns ''. Fall back to reading the raw XML attrib.
            _raw_remote = (
                getattr(player, 'remotePublicAddress', None)
                or (player._data.attrib.get('remotePublicAddress') if player and hasattr(player, '_data') else None)
                or ''
            )
            player_remote = _raw_remote if player else ''

            result.append({
                'session_id':       str(getattr(s, 'sessionKey', id(s))),
                'rating_key':       str(getattr(s, 'ratingKey', '')),
                'plex_item_key':    getattr(s, 'key', ''),
                'session_source':   'plex',
                'type':             media_type,
                'title':            getattr(s, 'title', ''),
                'year':             getattr(s, 'year', None),
                'show_title':       getattr(s, 'grandparentTitle', None),
                'season_episode':   season_ep,
                'thumb_key':        thumb_key,
                'progress_pct':     progress_pct,
                'view_offset_ms':   view_offset,
                'duration_ms':      duration,
                'state':            (getattr(player, 'state', None)
                                    or (player._data.attrib.get('state') if player and hasattr(player, '_data') else None)
                                    or 'unknown'),
                'stream_type':      stream_type,
                'video_resolution': video_resolution,
                'bitrate_kbps':     bitrate,
                'user':             username,
                'player_device':    getattr(player, 'title', getattr(player, 'device', '')) if player else '',
                'player_platform':  getattr(player, 'platform', '') if player else '',
                'player_product':   getattr(player, 'product', '') if player else '',
                'player_address':   player_address,
                'player_remote_address': player_remote,
            })
        return jsonify({'sessions': result, 'machine_id': machine_id, 'jellyfin_server_id': jellyfin_server_id})
    except Exception as exc:
        log.error("Error fetching Plex sessions: %s", exc)
        if result:
            # Jellyfin sessions gathered above are still valid even though Plex failed
            return jsonify({'sessions': result, 'machine_id': machine_id, 'jellyfin_server_id': jellyfin_server_id})
        return jsonify({'error': 'Failed to retrieve sessions.', 'sessions': []}), 500


_JELLYFIN_ITEM_ID_RE = re.compile(r'^[a-fA-F0-9]{32}$')


def _proxy_jellyfin_thumb(item_id: str, width: int, height: int):
    """Proxy a Jellyfin item's primary image. Shares the response validation
    (content-type whitelist + magic-byte check) used for the Plex path."""
    if not _JELLYFIN_ITEM_ID_RE.fullmatch(item_id):
        return '', 403
    try:
        resp = requests.get(
            f"{JELLYFIN_URL}/Items/{item_id}/Images/Primary",
            params={'maxWidth': width, 'maxHeight': height, 'quality': 90},
            headers=_jellyfin_headers(),
            timeout=10,
        )
        if resp.ok:
            # Map the response Content-Type to a known-safe literal from a
            # whitelist.  This breaks any taint originating from the user-
            # supplied 'item_id' parameter flowing into the response mimetype,
            # since the value assigned to ct is always one of our own strings.
            _SAFE_CT = {
                'image/jpeg', 'image/png', 'image/gif',
                'image/webp', 'image/bmp', 'image/tiff',
            }
            raw_ct = resp.headers.get('Content-Type', '').split(';')[0].strip()
            ct = raw_ct if raw_ct in _SAFE_CT else 'image/jpeg'
            body = resp.content
            # Validate response starts with a known image magic-byte signature.
            # This breaks the taint chain — if Jellyfin returns anything other
            # than a real image (e.g. an HTML error page), we refuse to forward it.
            _IMAGE_MAGIC = (
                b'\xff\xd8\xff',        # JPEG
                b'\x89PNG\r\n',         # PNG
                b'GIF8',                # GIF
                b'RIFF',                # WebP (RIFF....WEBP)
                b'BM',                  # BMP
            )
            if not any(body.startswith(magic) for magic in _IMAGE_MAGIC):
                return '', 502
            # make_response is neither a file-path sink (send_file) nor an
            # HTML sink (raw tuple return), so CodeQL's path-injection rule
            # is not triggered.  Content-Type is set explicitly from our
            # already-sanitised whitelist value.
            response = make_response(body)
            response.content_type = ct
            response.headers['Cache-Control'] = 'public, max-age=3600'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response
        return '', 404
    except requests.RequestException as exc:
        log.error("Jellyfin thumb proxy error: %s", exc)
        return '', 502


@app.route('/api/thumb')
@requires_auth
def api_thumb():
    """Proxy Plex/Jellyfin thumbnails so tokens never appear in the browser."""
    key = request.args.get('key', '').strip()
    if not key:
        return '', 404
    if session.get('demo') and key in _DEMO_POSTERS:
        try:
            r = requests.get(_DEMO_POSTERS[key], timeout=8)
            if r.ok:
                resp = make_response(r.content)
                resp.headers['Content-Type'] = r.headers.get('Content-Type', 'image/jpeg')
                resp.headers['Cache-Control'] = 'public, max-age=86400'
                return resp
        except Exception:
            pass
        return '', 502
    try:
        width  = max(1, min(int(request.args.get('w', '80')),  2000))
        height = max(1, min(int(request.args.get('h', '120')), 2000))
    except (ValueError, TypeError):
        return '', 400
    if key.startswith('jellyfin:'):
        if not JELLYFIN_ENABLED:
            return '', 403
        return _proxy_jellyfin_thumb(key[len('jellyfin:'):], width, height)
    if not (key.startswith('/library/') or key.startswith('/photo/')):
        return '', 403
    # Restrict to safe path characters only — prevents injection via crafted keys.
    if not re.fullmatch(r'[/\w.\-]+', key):
        return '', 403
    try:
        resp = requests.get(
            PLEX_URL.rstrip('/') + '/photo/:/transcode',
            params={
                'url': key,
                'width': width,
                'height': height,
                'minSize': 1,
                'upscale': 1,
                'X-Plex-Token': PLEX_TOKEN,
            },
            timeout=10,
        )
        if resp.ok:
            # Map the response Content-Type to a known-safe literal from a
            # whitelist.  This breaks any taint originating from the user-
            # supplied 'key' parameter flowing into send_file's mimetype arg,
            # since the value assigned to ct is always one of our own strings.
            _SAFE_CT = {
                'image/jpeg', 'image/png', 'image/gif',
                'image/webp', 'image/bmp', 'image/tiff',
            }
            raw_ct = resp.headers.get('Content-Type', '').split(';')[0].strip()
            ct = raw_ct if raw_ct in _SAFE_CT else 'image/jpeg'
            body = resp.content
            # Validate response starts with a known image magic-byte signature.
            # This breaks the taint chain — if Plex returns anything other than
            # a real image (e.g. an HTML error page), we refuse to forward it.
            _IMAGE_MAGIC = (
                b'\xff\xd8\xff',        # JPEG
                b'\x89PNG\r\n',         # PNG
                b'GIF8',                # GIF
                b'RIFF',                # WebP (RIFF....WEBP)
                b'BM',                  # BMP
            )
            if not any(body.startswith(magic) for magic in _IMAGE_MAGIC):
                return '', 502
            # make_response is neither a file-path sink (send_file) nor an
            # HTML sink (raw tuple return), so CodeQL's path-injection rule
            # is not triggered.  Content-Type is set explicitly from our
            # already-sanitised whitelist value.
            response = make_response(body)
            response.content_type = ct
            response.headers['Cache-Control'] = 'public, max-age=3600'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response
        return '', 404
    except Exception as exc:
        log.warning("Thumb proxy error: %s", exc)
        return '', 404


@app.route('/api/scan/library', methods=['POST'])
@csrf.exempt
@requires_auth
def api_scan_library():
    """Trigger a full library scan on a specific Plex section or Jellyfin library.

    section_id is provider-prefixed (e.g. "plex:1" or "jellyfin:<itemId>") so a
    single dropdown can offer libraries from either/both enabled servers.
    """
    data = request.get_json(silent=True) or {}
    raw_id = str(data.get('section_id', '')).strip()
    if not raw_id:
        return jsonify({'error': 'section_id required'}), 400

    provider, _, section_id = raw_id.partition(':')
    if not section_id:
        # Back-compat: bare IDs (no prefix) are treated as Plex, matching old clients
        provider, section_id = 'plex', raw_id

    if provider == 'plex':
        if not PLEX_ENABLED:
            return jsonify({'error': 'Plex is not enabled'}), 400
        plex_instance = get_plex()
        if not plex_instance:
            return jsonify({'error': 'Plex not connected'}), 503
        try:
            library = plex_instance.library.sectionByID(int(section_id))
            library.update()
            log.info("[MANUAL] Full scan triggered for Plex library '%s' (section %s)", library.title, section_id)
            return jsonify({'status': 'ok', 'library': library.title})
        except Exception as exc:
            log.error("[MANUAL] Full scan error for Plex section %s: %s", section_id, exc)
            return jsonify({'error': 'Failed to trigger full scan'}), 500

    elif provider == 'jellyfin':
        if not JELLYFIN_ENABLED:
            return jsonify({'error': 'Jellyfin is not enabled'}), 400
        try:
            jellyfin_full_library_scan(section_id)
            log.info("[MANUAL] Full scan triggered for Jellyfin library %s", section_id)
            return jsonify({'status': 'ok', 'library': section_id})
        except Exception as exc:
            log.error("[MANUAL] Full scan error for Jellyfin library %s: %s", section_id, exc)
            return jsonify({'error': 'Failed to trigger full scan'}), 500

    return jsonify({'error': 'Unknown provider'}), 400


@app.route('/api/libraries')
@requires_auth
def api_libraries():
    """Return the combined list of Plex sections and/or Jellyfin libraries.

    Each entry's id is provider-prefixed ("plex:<id>" / "jellyfin:<id>") so
    /api/scan/library can route the scan request to the right server.
    """
    if session.get('demo'):
        demo_libs = []
        if PLEX_ENABLED:
            demo_libs += [
                {'id': 'plex:1', 'title': 'TV Shows', 'type': 'show', 'provider': 'plex'},
                {'id': 'plex:2', 'title': 'Movies', 'type': 'movie', 'provider': 'plex'},
            ]
        if JELLYFIN_ENABLED:
            demo_libs += [
                {'id': 'jellyfin:1', 'title': 'TV Shows', 'type': 'tvshows', 'provider': 'jellyfin'},
                {'id': 'jellyfin:2', 'title': 'Movies', 'type': 'movies', 'provider': 'jellyfin'},
            ]
        return jsonify({'libraries': demo_libs})

    libs = []
    errors = []

    if PLEX_ENABLED:
        plex_instance = get_plex()
        if plex_instance:
            try:
                libs.extend({'id': f'plex:{s.key}', 'title': s.title, 'type': s.type, 'provider': 'plex'}
                            for s in plex_instance.library.sections())
            except Exception as exc:
                log.error("[LIBRARIES] Failed to fetch Plex library sections: %s", exc)
                errors.append('plex')
        else:
            errors.append('plex')

    if JELLYFIN_ENABLED:
        try:
            libs.extend({'id': f"jellyfin:{lib['id']}", 'title': lib['title'], 'type': lib['type'], 'provider': 'jellyfin'}
                        for lib in jellyfin_list_libraries())
        except Exception as exc:
            log.error("[LIBRARIES] Failed to fetch Jellyfin libraries: %s", exc)
            errors.append('jellyfin')

    if not libs and errors:
        return jsonify({'error': 'Failed to fetch libraries', 'libraries': []}), 503
    return jsonify({'libraries': libs})


_TILE_CACHE_DIR = "/data/tile_cache"
_TILE_CACHE_REAL = os.path.realpath(_TILE_CACHE_DIR)
_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
_TILE_MAX_AGE = 86400  # 24 h


@app.route('/api/maptile/<int:z>/<int:x>/<int:y>.png')
@requires_auth
def api_maptile(z, x, y):
    """Proxy and cache OpenStreetMap tiles server-side (24 h disk cache)."""
    if not (0 <= z <= 19 and 0 <= x < 2**z and 0 <= y < 2**z):
        return '', 400

    # Resolve the cache path and confirm it stays within _TILE_CACHE_DIR.
    # os.path.realpath + startswith is CodeQL's recognised path-injection sanitizer.
    candidate = os.path.realpath(os.path.join(_TILE_CACHE_DIR, str(z), str(x), f"{y}.png"))
    if not candidate.startswith(_TILE_CACHE_REAL + os.sep):
        return '', 400
    cache_path = candidate

    # Serve from disk cache if the file exists and is fresh.
    if os.path.isfile(cache_path) and (time.time() - os.path.getmtime(cache_path)) < _TILE_MAX_AGE:
        return send_file(cache_path, mimetype='image/png',
                         max_age=_TILE_MAX_AGE, conditional=True)

    url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    try:
        r = requests.get(url, timeout=10, headers={'User-Agent': 'media-servarr-sync/1.0'})
        if not r.ok:
            return '', r.status_code
        data = r.content
        if data[:8] != _PNG_MAGIC:
            log.debug("Map tile response is not a valid PNG %s/%s/%s", z, x, y)
            return '', 502
        # Write validated PNG to disk; all responses are served from the
        # cache file, decoupling the HTTP response from upstream content.
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, 'wb') as fh:
            fh.write(data)
        return send_file(cache_path, mimetype='image/png',
                         max_age=_TILE_MAX_AGE, conditional=True)
    except Exception as exc:
        log.debug("Map tile fetch failed %s/%s/%s: %s", z, x, y, exc)
        return '', 502


@app.route('/api/geoip')
@requires_auth
def api_geoip():
    """Proxy IP geolocation via ipinfo.io with server-side caching."""
    ip = request.args.get('ip', '').strip()
    if not ip:
        return jsonify({'error': 'no ip'}), 400
    if session.get('demo') and ip in _DEMO_GEO:
        return jsonify(_DEMO_GEO[ip])
    # Normalize and validate the IP; reject if private/invalid.
    try:
        safe_ip = str(ipaddress.ip_address(ip))
    except ValueError:
        return jsonify({'error': 'invalid ip'}), 400
    addr = ipaddress.ip_address(safe_ip)
    if not addr.is_global:
        return jsonify({'private': True, 'ip': safe_ip})
    now = time.time()
    with _geo_cache_lock:
        cached = _geo_cache.get(safe_ip)
        if cached:
            if cached.get('_error'):
                # Return cached failure for 5 minutes to avoid hammering the API
                if now - cached.get('_ts', 0) < 300:
                    return jsonify({'error': 'Geolocation lookup failed.'}), 500
            elif now - cached.get('_ts', 0) < 86400:
                out = dict(cached)
                out.pop('_ts', None)
                return jsonify(_sanitize_floats(out))
    try:
        r = requests.get(f'https://ipinfo.io/{safe_ip}/json', timeout=5)
        raw = r.json()
        # Normalize ipinfo.io response to ip-api.com field names expected by the frontend
        lat, lon = None, None
        if raw.get('loc'):
            try:
                lat, lon = (float(x) for x in raw['loc'].split(',', 1))
            except ValueError:
                pass
        data = {
            'status':     'success',
            'query':      safe_ip,
            'city':       raw.get('city', ''),
            'country':    raw.get('country', ''),
            'countryCode': raw.get('country', ''),
            'regionName': raw.get('region', ''),
            'lat':        lat,
            'lon':        lon,
            'isp':        raw.get('org', ''),
            'org':        raw.get('org', ''),
            '_ts':        now,
        }
        with _geo_cache_lock:
            _geo_cache[safe_ip] = data
        out = dict(data)
        out.pop('_ts', None)
        return jsonify(_sanitize_floats(out))
    except Exception as exc:
        log.warning("GeoIP lookup failed for ip=%r: %s", request.args.get('ip'), exc)
        with _geo_cache_lock:
            _geo_cache[safe_ip] = {'_error': True, '_ts': now}
        return jsonify({'error': 'Geolocation lookup failed.'}), 500


# ---------------------------------------------------------------------------
# Invite management routes (admin)
# ---------------------------------------------------------------------------

def _invite_validity(invite: dict) -> Optional[str]:
    """Return an error string if the invite cannot be used, else None."""
    if not invite:
        return 'This invite link is not valid.'
    if invite.get('status') == 'revoked':
        return 'This invite link has been revoked.'
    le = invite.get('link_expires_at')
    if le and int(time.time()) > le:
        return 'This invite link has expired.'
    max_uses = invite.get('max_uses', 0)
    if max_uses > 0 and invite.get('uses', 0) >= max_uses:
        return 'This invite has reached its maximum number of uses.'
    return None


def _plex_section_names(plex_instance, section_ids: list) -> list[str]:
    try:
        all_sections = {str(s.key): s.title for s in plex_instance.library.sections()}
        if section_ids:
            return [all_sections.get(str(sid), str(sid)) for sid in section_ids]
        return list(all_sections.values())
    except Exception:
        return []


def _jellyfin_section_names(section_ids: list) -> list[str]:
    try:
        all_libs = {lib['id']: lib['title'] for lib in jellyfin_list_libraries()}
        if section_ids:
            return [all_libs.get(str(sid), str(sid)) for sid in section_ids]
        return list(all_libs.values())
    except Exception:
        return []


def _section_names(provider: str, plex_instance, section_ids: list) -> list[str]:
    if provider == 'jellyfin':
        return _jellyfin_section_names(section_ids)
    return _plex_section_names(plex_instance, section_ids) if plex_instance else []


@app.route('/invites', methods=['GET'])
@requires_auth
def invites_page():
    if not PLEX_ENABLED and not JELLYFIN_ENABLED:
        return redirect(url_for('manual_webhook'))
    if session.get('demo'):
        demo_libs = []
        if PLEX_ENABLED:
            demo_libs += [{"id": "1", "title": "TV Shows", "type": "show", "provider": "plex"},
                          {"id": "2", "title": "Movies", "type": "movie", "provider": "plex"}]
        if JELLYFIN_ENABLED:
            demo_libs += [{"id": "jf1", "title": "TV Shows", "type": "tvshows", "provider": "jellyfin"},
                          {"id": "jf2", "title": "Movies", "type": "movies", "provider": "jellyfin"}]
        return render_template('invites.html', invites=_DEMO_INVITES,
                               libraries=demo_libs,
                               plex_error=None, demo=True)

    plex_instance = get_plex() if PLEX_ENABLED else None
    libraries  = []
    plex_error = None
    if PLEX_ENABLED:
        if plex_instance:
            try:
                libraries += [
                    {'id': str(s.key), 'title': s.title, 'type': s.type, 'provider': 'plex'}
                    for s in plex_instance.library.sections()
                ]
            except Exception as exc:
                plex_error = str(exc)
        else:
            plex_error = 'Could not connect to Plex'
    jellyfin_error = None
    if JELLYFIN_ENABLED:
        try:
            libraries += [
                {'id': lib['id'], 'title': lib['title'], 'type': lib['type'], 'provider': 'jellyfin'}
                for lib in jellyfin_list_libraries()
            ]
        except Exception as exc:
            jellyfin_error = str(exc)

    invites    = invite_db.list_all()
    all_grants = invite_db.get_grants()
    grants_by_invite: dict = {}
    for g in all_grants:
        grants_by_invite.setdefault(g['invite_id'], []).append(g)

    now = int(time.time())
    lib_map = {(lib['provider'], str(lib['id'])): lib['title'] for lib in libraries}
    for inv in invites:
        inv['grants'] = grants_by_invite.get(inv['id'], [])
        provider = inv.get('provider') or 'plex'
        try:
            inv['section_ids'] = json.loads(inv.get('section_ids', '[]') or '[]')
        except Exception:
            inv['section_ids'] = []
        inv['section_names'] = [lib_map.get((provider, str(sid)), str(sid)) for sid in inv['section_ids']]
        le = inv.get('link_expires_at')
        inv['link_expired']  = bool(le and now > le)
        inv['max_reached']   = inv.get('max_uses', 0) > 0 and inv.get('uses', 0) >= inv.get('max_uses', 0)
        inv['is_active']     = inv.get('status') == 'active' and not inv['link_expired'] and not inv['max_reached']

    return render_template('invites.html', invites=invites, libraries=libraries,
                           plex_error=plex_error, jellyfin_error=jellyfin_error,
                           demo=session.get('demo', False))


@app.route('/invites/create', methods=['POST'])
@requires_auth
def create_invite():
    label             = request.form.get('label', '').strip()
    provider          = request.form.get('provider', 'plex').strip()
    if provider not in ('plex', 'jellyfin'):
        provider = 'plex'
    # Section checkboxes are namespaced per-provider ("plex_sections" / "jellyfin_sections")
    # so stray selections from the hidden provider's library grid are never submitted.
    section_ids       = request.form.getlist(f'{provider}_sections')
    allow_sync        = 'allow_sync' in request.form
    allow_channels    = 'allow_channels' in request.form
    home_user         = 'home_user' in request.form
    duration_days     = int(request.form.get('duration_days', '0') or '0')
    max_uses          = int(request.form.get('max_uses', '1') or '1')
    link_expires_days = int(request.form.get('link_expires_days', '7') or '7')
    invite_db.create(label, section_ids, allow_sync, allow_channels, home_user,
                     duration_days, max_uses, link_expires_days, provider=provider)
    return redirect(url_for('invites_page'))


@app.route('/invites/revoke/<token>', methods=['POST'])
@requires_auth
def revoke_invite(token):
    invite_db.revoke_invite(token)
    return redirect(url_for('invites_page'))


@app.route('/invites/revoke_grant/<int:grant_id>', methods=['POST'])
@requires_auth
def revoke_grant(grant_id):
    all_grants = invite_db.get_grants()
    matched = next((g for g in all_grants if g['id'] == grant_id), None)
    if matched:
        provider = matched.get('provider') or 'plex'
        try:
            if provider == 'jellyfin':
                if matched.get('provider_user_id'):
                    jellyfin_delete_user(matched['provider_user_id'])
                    log.info("[INVITE] Manually revoked Jellyfin access for '%s'", matched['plex_username'])
            else:
                plex_instance = get_plex()
                if plex_instance:
                    account = plex_instance.myPlexAccount()
                    account.removeFriend(matched['plex_username'])
                    log.info("[INVITE] Manually revoked Plex access for '%s'", matched['plex_username'])
        except Exception as exc:
            log.warning("[INVITE] Error revoking %s access for '%s': %s",
                        provider, matched.get('plex_username'), exc)
        invite_db.revoke_grant(grant_id)
    return redirect(url_for('invites_page'))


# ---------------------------------------------------------------------------
# Public invite / onboarding routes
# ---------------------------------------------------------------------------

def _onboard_render(invite: dict, token: str, step: str, error: Optional[str] = None,
                    section_names: Optional[list] = None, username: str = ''):
    """Render invite_onboard.html with all provider-aware context filled in."""
    provider = (invite or {}).get('provider') or 'plex'
    if provider == 'jellyfin':
        server_name = ''
        jf_info = get_jellyfin_info()
        if jf_info:
            server_name = jf_info.get('ServerName', '')
        server_url = JELLYFIN_URL
    else:
        plex_instance = get_plex()
        server_name = getattr(plex_instance, 'friendlyName', '') if plex_instance else ''
        server_url = PLEX_URL
    return render_template(
        'invite_onboard.html',
        invite=invite, section_names=section_names or [], step=step,
        token=token, error=error, provider=provider,
        server_name=server_name, server_url=server_url, username=username,
        onboard_wiki_url=ONBOARD_WIKI_URL,
        onboard_request_url=ONBOARD_REQUEST_URL,
    )


@app.route('/invite/<token>', methods=['GET'])
def invite_onboard(token):
    invite = invite_db.get(token)
    err = _invite_validity(invite)
    if err:
        return _onboard_render(invite, token, 'error', error=err)

    section_ids   = json.loads(invite.get('section_ids', '[]') or '[]')
    section_names = _section_names(invite.get('provider') or 'plex',
                                   get_plex() if PLEX_ENABLED else None, section_ids)
    step = request.args.get('step', 'welcome')
    return _onboard_render(invite, token, step, section_names=section_names)


@app.route('/invite/<token>/accept', methods=['POST'])
def accept_invite(token):
    invite = invite_db.get(token)
    err = _invite_validity(invite)
    if err:
        return _onboard_render(invite, token, 'error', error=err)

    provider = invite.get('provider') or 'plex'

    if provider == 'jellyfin':
        return _accept_jellyfin_invite(invite, token)
    return _accept_plex_invite(invite, token)


def _accept_plex_invite(invite: dict, token: str):
    plex_username = request.form.get('username', '').strip()
    plex_instance  = get_plex()

    if not plex_username:
        return _onboard_render(invite, token, 'accept',
                               error='Please enter your Plex username or email.')
    if not plex_instance:
        return _onboard_render(invite, token, 'accept', username=plex_username,
                               error='Server error: cannot connect to Plex. Please try again later.')
    try:
        account       = plex_instance.myPlexAccount()
        section_ids   = json.loads(invite.get('section_ids', '[]') or '[]')
        all_lib       = plex_instance.library.sections()
        sections      = [s for s in all_lib if str(s.key) in [str(sid) for sid in section_ids]] \
                        if section_ids else None
        section_names = _plex_section_names(plex_instance, section_ids)

        account.inviteFriend(
            user=plex_username,
            server=plex_instance,
            sections=sections,
            allowSync=bool(invite.get('allow_sync')),
            allowCameraUpload=False,
            allowChannels=bool(invite.get('allow_channels')),
        )
        invite_db.record_acceptance(invite['id'], plex_username, invite.get('duration_days', 0),
                                    provider='plex')
        log.info("[INVITE] '%s' accepted Plex invite '%s'", plex_username, invite.get('label', token))
        return _onboard_render(invite, token, 'done', section_names=section_names, username=plex_username)
    except Exception as exc:
        err = str(exc)
        log.error("[INVITE] Plex acceptance error for '%s': %s", plex_username, exc)
        if 'already' in err.lower() or 'exist' in err.lower():
            user_err = f"'{plex_username}' may already have access, or has already been invited."
        elif 'not found' in err.lower() or 'invalid' in err.lower() or '404' in err:
            user_err = f"Plex account '{plex_username}' not found. Please check your username or email."
        else:
            user_err = "Could not process the invite. Please try again or contact the server owner."
        return _onboard_render(invite, token, 'accept', error=user_err, username=plex_username)


def _accept_jellyfin_invite(invite: dict, token: str):
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')

    if not username or not password:
        return _onboard_render(invite, token, 'accept', username=username,
                               error='Please choose a username and password.')
    if len(password) < 6:
        return _onboard_render(invite, token, 'accept', username=username,
                               error='Password must be at least 6 characters.')
    try:
        section_ids   = json.loads(invite.get('section_ids', '[]') or '[]')
        section_names = _jellyfin_section_names(section_ids)

        user = jellyfin_create_user(username, password)
        user_id = user.get('Id', '')
        jellyfin_set_user_library_access(user_id, section_ids, allow_downloads=bool(invite.get('allow_sync')))

        invite_db.record_acceptance(invite['id'], username, invite.get('duration_days', 0),
                                    provider='jellyfin', provider_user_id=user_id)
        log.info("[INVITE] '%s' accepted Jellyfin invite '%s'", username, invite.get('label', token))
        return _onboard_render(invite, token, 'done', section_names=section_names, username=username)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        log.error("[INVITE] Jellyfin acceptance error for '%s': %s", username, exc)
        if status == 400:
            user_err = f"'{username}' is already taken. Please choose a different username."
        else:
            user_err = "Could not process the invite. Please try again or contact the server owner."
        return _onboard_render(invite, token, 'accept', error=user_err, username=username)
    except Exception as exc:
        log.error("[INVITE] Jellyfin acceptance error for '%s': %s", username, exc)
        return _onboard_render(invite, token, 'accept', username=username,
                               error='Could not process the invite. Please try again or contact the server owner.')


# ---------------------------------------------------------------------------
# PWA – manifest + service worker (served at root scope)
# ---------------------------------------------------------------------------

@app.route('/manifest.json')
def pwa_manifest():
    """Return the PWA web app manifest."""
    manifest = {
        "name": "Media Servarr Sync",
        "short_name": "ServarrSync",
        "description": "Sonarr/Radarr → Plex webhook sync dashboard",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d0d0f",
        "theme_color": "#e5a00d",
        "icons": [
            {
                "src": "/static/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any"
            },
            {
                "src": "/static/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "maskable"
            }
        ]
    }
    resp = jsonify(manifest)
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp


@app.route('/sw.js')
def service_worker():
    """Serve the PWA service worker at root scope (required for full-origin control)."""
    js = """\
// Media Servarr Sync – minimal service worker for PWA installability
// Network-only: no caching so real-time data (webhooks, now-playing) is always fresh.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
"""
    resp = make_response(js)
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _handle_shutdown(signum, frame):
    log.info("Shutdown signal received, stopping worker...")
    _worker_alive.clear()
    sys.exit(0)


_REDACT_RE = re.compile(r'token|pass|secret|key|credential|auth', re.IGNORECASE)


def _log_env():
    """Log all environment variables, redacting sensitive ones."""
    log.info("=== Environment ===")
    for name, value in sorted(os.environ.items()):
        display = "***REDACTED***" if _REDACT_RE.search(name) else value
        log.info("  %-40s = %s", name, display)
    log.info("===================")


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Suppress werkzeug's "Running on ..." banner lines
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    log.info("=== Media Servarr Sync starting ===")
    log.info("Rclone integration: %s", "ENABLED" if USE_RCLONE else "DISABLED (set USE_RCLONE=true to enable)")
    log.info("Sonarr API: %s", SONARR_URL if SONARR_URL and SONARR_API_KEY else "NOT CONFIGURED (set SONARR_URL + SONARR_API_KEY for quality-profile badges)")
    log.info("Radarr API: %s", RADARR_URL if RADARR_URL and RADARR_API_KEY else "NOT CONFIGURED (set RADARR_URL + RADARR_API_KEY for quality-profile badges)")
    _log_env()

    worker_thread = threading.Thread(target=sync_worker, daemon=True, name="sync-worker")
    worker_thread.start()

    cf_thread = threading.Thread(target=custom_format_refresh_scheduler, daemon=True, name="cf-scheduler")
    cf_thread.start()

    invite_thread = threading.Thread(target=invite_expiry_scheduler, daemon=True, name="invite-expiry")
    invite_thread.start()

    log.info("Webhook receiver active on port %d", PORT)
    serve(app, host='0.0.0.0', port=PORT, asyncore_use_poll=True)
