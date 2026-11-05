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
    youtube_target_playlist: str = ""

    max_tracks_per_run: int = 0  # 0 = unlimited
    dry_run_track_limit: int = 200  # dry-run only: max source tracks to read (0 = unlimited)
    max_searches_per_run: int = 80
    youtube_daily_quota_units: int = 9500
    search_cache_ttl_days: int = 14
    search_results_per_query: int = 10
    duration_tolerance_seconds: int = 7
    exact_duration_tolerance_seconds: int = 3
    retry_unmatched_after_days: int = 7
    telegram_per_track: bool = True
    dry_run: bool = False
    require_youtube_match_for_spotify: bool = False
    youtube_region_code: str = "US"
    state_path: str = "data/sync-state.json"
    overrides_path: str = "data/track-overrides.json"
    cache_path: str = ".cache/yt-cache.json"

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
        if os.environ.get("YOUTUBE_TARGET_PLAYLIST"):
            cfg.youtube_target_playlist = os.environ["YOUTUBE_TARGET_PLAYLIST"]
        if os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}:
            cfg.dry_run = True
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
        if not self.youtube_target_playlist:
            raise ConfigError("youtube_target_playlist is not configured")
        if self.spotify_target_playlist in self.spotify_source_playlists:
            raise ConfigError("Target Spotify playlist must not also be a source playlist")
        if self.dry_run_track_limit < 0:
            raise ConfigError("dry_run_track_limit must be 0 (unlimited) or a positive integer")


@dataclass
class Secrets:
    spotify_cookie: str = ""
    youtube_client_json: str = ""
    youtube_refresh_token: str = ""
    youtube_token_json: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            spotify_cookie=os.environ.get("SPOTIFY_COOKIE", ""),
            youtube_client_json=os.environ.get("YT_CLIENT_JSON", ""),
            youtube_refresh_token=os.environ.get("YT_REFRESH_TOKEN", ""),
            youtube_token_json=os.environ.get("YT_TOKEN_JSON", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        )

    def validate(self, require_telegram: bool = False) -> None:
        missing = []
        if not self.spotify_cookie:
            missing.append("SPOTIFY_COOKIE")
        if not (self.youtube_client_json or self.youtube_token_json):
            missing.append("YT_CLIENT_JSON (or YT_TOKEN_JSON)")
        if not (self.youtube_refresh_token or self.youtube_token_json):
            missing.append("YT_REFRESH_TOKEN (or YT_TOKEN_JSON)")
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
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
