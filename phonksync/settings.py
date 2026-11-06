"""Configuration and secret loading.

Playlist IDs may live in the public ``config.json`` file or in GitHub Secrets.
Credentials NEVER live in config files; they are read from environment
variables / GitHub Secrets only.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from .errors import ConfigError  # noqa: F401  (re-exported for convenience)


_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_spotify_playlist_id(value: str) -> str:
    """Accept a raw ID, spotify: URI, or open.spotify.com URL."""
    value = (value or "").strip()
    if not value:
        raise ConfigError("Empty Spotify playlist ID")
    if value.startswith("spotify:playlist:"):
        value = value.split(":")[-1]
    elif "open.spotify.com" in value:
        m = re.search(r"playlist/([A-Za-z0-9]{22})", value)
        if not m:
            raise ConfigError(f"Cannot parse Spotify playlist URL: {value}")
        value = m.group(1)
    value = value.split("?")[0]
    if not _SPOTIFY_ID_RE.match(value):
        raise ConfigError(f"Invalid Spotify playlist ID: {value!r}")
    return value


def parse_youtube_playlist_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ConfigError("Empty YouTube playlist ID")
    if "list=" in value:
        m = re.search(r"list=([A-Za-z0-9_-]+)", value)
        if not m:
            raise ConfigError(f"Cannot parse YouTube playlist URL: {value}")
        value = m.group(1)
    if not re.match(r"^[A-Za-z0-9_-]{12,}$", value):
        raise ConfigError(f"Invalid YouTube playlist ID: {value!r}")
    return value


def _split_ids(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[\n,;]+", raw or "") if p.strip()]


@dataclass
class Config:
    spotify_source_playlists: list[str] = field(default_factory=list)
    spotify_target_playlist: str = ""
    youtube_enabled: bool = False  # YouTube sync is removed/disabled by default
    youtube_target_playlist: str = ""

    max_tracks_per_run: int = 0  # 0 = unlimited
    dry_run_track_limit: int = 200  # dry-run only: max source tracks to read (0 = unlimited)
    max_searches_per_run: int = 200  # free searches do not count; paid ones are budgeted
    youtube_max_searches_per_day: int = 0  # 0 = no app cap; per-key units still enforced
    youtube_free_search: bool = True  # search youtube.com first: 0 API units
    youtube_free_search_pause: float = 1.0  # politeness gap between free page loads
    youtube_max_inserts_per_day: int = 40  # 40 x 50 = 2000 units -> 8000 + 2000 = 10000 exactly
    youtube_daily_quota_units: int = 9800
    search_cache_ttl_days: int = 14
    search_results_per_query: int = 10
    duration_tolerance_seconds: int = 7
    exact_duration_tolerance_seconds: int = 3
    retry_unmatched_after_days: int = 7
    telegram_per_track: bool = True
    dry_run: bool = False
    require_youtube_match_for_spotify: bool = False
    youtube_region_code: str = "US"
    spotify_write_mode: str = "queue"  # queue | direct  (queue = far fewer calls)
    spotify_request_delay_seconds: float = 2.0  # gap between every Spotify API call
    spotify_proxy_url: str = ""  # CORS proxy for Spotify calls (clean egress IP)
    spotify_web_fallback: bool = True  # read public playlists from the HTML page on 429
    spotify_collection_fallback: bool = True  # use a saved collection when the API is blocked
    spotify_queue_flush_size: int = 40
    manual_queue_path: str = "data/manual-queue.txt"
    collection_path: str = "data/collection.json"
    source_cache_path: str = "data/source-cache.json"
    source_cache_ttl_hours: int = 12  # reuse a playlist read while it is unchanged
    spotify_cache_allow_stale: bool = True  # replay an old read instead of calling Spotify
    spotify_cache_max_age_days: int = 30  # how old a replayed read may be
    state_path: str = "data/sync-state.json"
    overrides_path: str = "data/track-overrides.json"
    cache_path: str = "data/yt-cache.json"

    @classmethod
    def load(cls, path: str | Path = "config.json") -> "Config":
        path = Path(path)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
        cfg = cls()
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

        # Environment / secret overrides.
        env_sources = os.environ.get("SPOTIFY_SOURCE_PLAYLISTS", "")
        if env_sources:
            cfg.spotify_source_playlists = _split_ids(env_sources)
        if os.environ.get("SPOTIFY_TARGET_PLAYLIST"):
            cfg.spotify_target_playlist = os.environ["SPOTIFY_TARGET_PLAYLIST"]
        env_yt_enabled = os.environ.get("YOUTUBE_ENABLED", "").lower()
        if env_yt_enabled in {"1", "true", "yes"}:
            cfg.youtube_enabled = True
        elif env_yt_enabled in {"0", "false", "no"}:
            cfg.youtube_enabled = False
        if os.environ.get("YOUTUBE_TARGET_PLAYLIST"):
            cfg.youtube_target_playlist = os.environ["YOUTUBE_TARGET_PLAYLIST"]
        if os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}:
            cfg.dry_run = True
        if os.environ.get("MAX_SEARCHES_PER_RUN"):
            cfg.max_searches_per_run = int(os.environ["MAX_SEARCHES_PER_RUN"])
        if "YOUTUBE_FREE_SEARCH" in os.environ:
            cfg.youtube_free_search = os.environ["YOUTUBE_FREE_SEARCH"].strip().lower() in {
                "1", "true", "yes",
            }
        if os.environ.get("MAX_INSERTS_PER_DAY"):
            cfg.youtube_max_inserts_per_day = int(os.environ["MAX_INSERTS_PER_DAY"])
        if os.environ.get("MAX_TRACKS_PER_RUN"):
            cfg.max_tracks_per_run = int(os.environ["MAX_TRACKS_PER_RUN"])

        cfg.spotify_source_playlists = [
            parse_spotify_playlist_id(p) for p in cfg.spotify_source_playlists
        ]
        # Deduplicate while preserving order.
        cfg.spotify_source_playlists = list(dict.fromkeys(cfg.spotify_source_playlists))
        if cfg.spotify_target_playlist:
            cfg.spotify_target_playlist = parse_spotify_playlist_id(cfg.spotify_target_playlist)
        if cfg.youtube_target_playlist:
            cfg.youtube_target_playlist = parse_youtube_playlist_id(cfg.youtube_target_playlist)
        return cfg

    def validate(self, require_telegram: bool = False) -> None:
        if not self.spotify_source_playlists:
            raise ConfigError(
                "No source Spotify playlists configured. Set spotify_source_playlists "
                "in config.json or the SPOTIFY_SOURCE_PLAYLISTS secret."
            )
        if not self.spotify_target_playlist:
            raise ConfigError("spotify_target_playlist is not configured")
        if self.youtube_enabled and not self.youtube_target_playlist:
            raise ConfigError("youtube_target_playlist is not configured (YouTube sync is enabled)")
        if self.spotify_target_playlist in self.spotify_source_playlists:
            raise ConfigError("Target Spotify playlist must not also be a source playlist")
        if self.dry_run_track_limit < 0:
            raise ConfigError("dry_run_track_limit must be 0 (unlimited) or a positive integer")


@dataclass
class Secrets:
    spotify_cookie: str = ""
    spotify_spare_cookies: list[str] = field(default_factory=list)
    youtube_client_json: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    youtube_token_json: str = ""
    youtube_access_token: str = ""  # short-lived (1h) token pasted by hand
    youtube_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        # Support common aliases so users don't have to rename secrets.
        # YT_TOKEN_JSON <- YOUTUBE_OAUTH_TOKEN_JSON / YOUTUBE_TOKEN_JSON / YT_OAUTH_TOKEN_JSON
        def _first(*keys: str) -> str:
            for k in keys:
                v = os.environ.get(k, "")
                if v:
                    return v
            return ""

        # SPOTIFY_COOKIE_2 / _3 / _4 (or SPOTIFY_SPARE_COOKIES, comma separated)
        spares: list[str] = []
        for key in ("SPOTIFY_COOKIE_2", "SPOTIFY_COOKIE_3", "SPOTIFY_COOKIE_4"):
            if os.environ.get(key, "").strip():
                spares.append(os.environ[key])
        for key in ("SPOTIFY_SPARE_COOKIES", "SPOTIFY_COOKIES"):
            raw = os.environ.get(key, "").strip()
            if raw:
                spares.extend(p for p in raw.split("|||") if p.strip())

        return cls(
            spotify_cookie=os.environ.get("SPOTIFY_COOKIE", ""),
            spotify_spare_cookies=spares,
            youtube_client_json=_first("YT_CLIENT_JSON", "YOUTUBE_CLIENT_JSON", "YOUTUBE_OAUTH_CLIENT_JSON"),
            youtube_client_id=_first("YT_CLIENT_ID", "YOUTUBE_CLIENT_ID", "GOOGLE_CLIENT_ID"),
            youtube_client_secret=_first("YT_CLIENT_SECRET", "YOUTUBE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"),
            youtube_refresh_token=_first("YT_REFRESH_TOKEN", "YOUTUBE_REFRESH_TOKEN"),
            youtube_token_json=_first(
                "YT_TOKEN_JSON",
                "YT_OAUTH_TOKEN_JSON",
                "YOUTUBE_OAUTH_TOKEN_JSON",
                "YOUTUBE_TOKEN_JSON",
            ),
            youtube_access_token=_first(
                "YT_ACCESS_TOKEN",
                "YOUTUBE_ACCESS_TOKEN",
                "YOUTUBE_OAUTH_TOKEN",
                "YT_OAUTH_TOKEN",
            ),
            youtube_api_key=_first(
                "YOUTUBE_API_KEY",
                "YT_API_KEY",
                "YOUTUBE_DATA_API_KEY",
                "GOOGLE_API_KEY",
            ),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        )

    def validate(self, require_telegram: bool = False, require_youtube: bool = False) -> None:
        missing = []
        if not self.spotify_cookie:
            missing.append("SPOTIFY_COOKIE")
        if require_youtube:
            # Read-only search needs an API key; playlist writes need OAuth.
            has_oauth = bool(
                (
                    self.youtube_client_json
                    or self.youtube_token_json
                    or (self.youtube_client_id and self.youtube_client_secret)
                )
                and (self.youtube_refresh_token or self.youtube_token_json)
            )
            if not (self.youtube_api_key or has_oauth):
                missing.append(
                    "YOUTUBE_API_KEY (read-only search) or YT_CLIENT_JSON + YT_REFRESH_TOKEN"
                )
        if require_telegram and not (self.telegram_bot_token and self.telegram_chat_id):
            missing.append("TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
        if missing:
            raise ConfigError("Missing required secrets: " + ", ".join(missing))


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader for local runs. Does not override existing env vars."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip one layer of outer quotes (handles \"sp_dc=...; ...\" pasted with quotes).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
            # .env values are often double-wrapped: '\"...\"'
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
