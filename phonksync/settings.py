"""Configuration and secret loading.

Playlist IDs may live in the public ``config.json`` file or in GitHub Secrets.
Credentials NEVER live in config files; they are read from environment
variables / GitHub Secrets only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from .errors import ConfigError  # noqa: F401  (re-exported for convenience)


log = logging.getLogger(__name__)

_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")

# Config fields that name a file: relative values are resolved against config.json.
_PATH_FIELDS = (
    "state_path",
    "cache_path",
    "overrides_path",
    "source_cache_path",
    "collection_path",
    "manual_queue_path",
    "version_queue_path",
    "spotify_source_playlists_file",
)


def parse_spotify_playlist_id(value: str) -> str:
    """Accept anything the Spotify apps actually copy.

    A phone shares a very long link: the locale path segment is sometimes there
    (`/intl-de/`), and there are always tracking parameters
    (`?si=…&utm_source=copy-link&pi=…`). It may also arrive HTML-escaped
    (`&amp;`), wrapped in angle brackets, or split by a pasted line break. All of
    those must yield the same 22-character id, never a confusing "invalid id".
    """
    value = (value or "").strip().strip("<>\"' ").strip()
    if not value:
        raise ConfigError("Empty Spotify playlist ID")
    # A share link can arrive URL-encoded by a chat app.
    if "%3A%2F%2F" in value.upper():
        from urllib.parse import unquote

        value = unquote(value)
    value = value.replace("&amp;", "&").replace("\u0026", "&")
    # Paste from a phone often inserts newlines inside the link.
    value = "".join(value.split())
    if value.startswith("spotify:playlist:"):
        value = value.split(":")[-1]
    elif "spotify.link/" in value or "spotify.app.link/" in value:
        raise ConfigError(
            "That is a Spotify *share* link (spotify.link/…), which redirects and "
            "cannot be resolved without the network. Open it once in a browser and "
            "copy the address it lands on (open.spotify.com/playlist/<id>)."
        )
    elif re.search(r"/(track|album|artist|episode|show|user)/", value):
        # A *track* link would otherwise pass as "22 characters" and point the
        # whole run at the wrong object — fail loudly instead of writing there.
        kind = re.search(r"/(track|album|artist|episode|show|user)/", value).group(1)
        raise ConfigError(
            f"That is a Spotify {kind} link, not a playlist link. Open the playlist "
            "itself and copy its link (open.spotify.com/playlist/<22 characters>)."
        )
    elif "open.spotify.com" in value or "/playlist/" in value:
        m = re.search(r"playlist/([A-Za-z0-9]{22})", value)
        if not m:
            # Fall back on the last 22-character token in the URL path.
            m = re.search(r"/([A-Za-z0-9]{22})(?:[/?#]|$)", value)
        if not m:
            raise ConfigError(
                f"Cannot parse Spotify playlist URL: {value}\n"
                "Expected open.spotify.com/playlist/<22 characters>."
            )
        value = m.group(1)
    value = value.split("?")[0].split("#")[0].split("/")[0].strip()
    if not _SPOTIFY_ID_RE.match(value):
        raise ConfigError(
            f"Invalid Spotify playlist ID: {value!r} "
            "(a playlist id is exactly 22 letters and digits; a track, album or "
            "artist link is not a playlist)"
        )
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


# Committed source-list files: the easiest way to set sources without a Secret.
# The first name is the one created in this repo; the rest are forgiving aliases
# (matched case-insensitively) so a corrected spelling or a plain-text export
# still works. Public playlist links carry no secret, so committing them is fine.
_SOURCE_FILE_CANDIDATES = (
    "Scoure playlist.txt",
    "Source playlist.txt",
    "Source playlists.txt",
    "source_playlists.txt",
    "source-playlists.txt",
    "sources.txt",
)


def _read_source_file(path: Path) -> list[str]:
    """Playlist links/IDs from a text file: one per line, or comma/semicolon
    separated. Blank lines and whole-line ``#`` comments are ignored, and a
    UTF-8 BOM (from Windows/phone editors) is tolerated.
    """
    text = path.read_text(encoding="utf-8-sig")
    kept = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    return _split_ids("\n".join(kept))


def _discover_source_file(cfg: "Config", base: Path) -> Path | None:
    """Find the committed source-list file next to ``config.json``.

    An explicit ``spotify_source_playlists_file`` wins, and a missing one is a
    loud error rather than a silent fallback. Otherwise a few common names are
    tried next to the config file, case-insensitively.
    """
    explicit = (cfg.spotify_source_playlists_file or "").strip()
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = base / p
        if not p.exists():
            raise ConfigError(
                f"spotify_source_playlists_file points to {explicit!r}, but no such "
                f"file exists (looked at {p}). Fix the path or clear the setting."
            )
        return p
    try:
        by_lower = {e.name.lower(): e for e in base.iterdir() if e.is_file()}
    except OSError:
        return None
    for name in _SOURCE_FILE_CANDIDATES:
        hit = by_lower.get(name.lower())
        if hit is not None:
            return hit
    return None


def _env_number(name: str, low: float, high: float) -> float | None:
    """Read a numeric env var, clamped; None when unset, empty or invalid.

    Workflow inputs are strings and are very often empty ("" = use config.json),
    so an empty or garbled value must fall back silently instead of crashing a
    run that would otherwise work.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r", name, raw)
        return None
    return max(low, min(high, value))


@dataclass
class Config:
    spotify_source_playlists: list[str] = field(default_factory=list)
    # Optional committed text file of source playlist links (no Secret needed).
    # Empty = auto-detect a common name next to config.json (e.g. the repo's
    # "Scoure playlist.txt"). A relative value resolves against config.json.
    spotify_source_playlists_file: str = ""
    # Set by load(): where the source list actually came from (shown by `check`).
    spotify_source_playlists_origin: str = ""
    spotify_target_playlist: str = ""
    youtube_enabled: bool = False  # YouTube sync is removed/disabled by default
    youtube_target_playlist: str = ""

    max_tracks_per_run: int = 0  # 0 = unlimited
    # Writes-only run: add the remembered matches, search for nothing new.
    youtube_writes_only: bool = False
    dry_run_track_limit: int = 200  # dry-run only: max source tracks to read (0 = unlimited)
    max_searches_per_run: int = 200  # free searches do not count; paid ones are budgeted
    youtube_max_searches_per_day: int = 0  # 0 = no app cap; per-key units still enforced
    youtube_free_search: bool = True  # search youtube.com first: 0 API units
    youtube_free_search_pause: float = 1.0  # politeness gap between free page loads
    # Local pacing cap. With several usable OAuth accounts it is applied PER
    # ACCOUNT (195 x 50 = 9750 units, leaving a little headroom for reads).
    # 0 disables the local cap; Google's real quotaExceeded still fails over.
    youtube_max_inserts_per_day: int = 195
    youtube_daily_quota_units: int = 9800
    search_cache_ttl_days: int = 14
    search_results_per_query: int = 10
    duration_tolerance_seconds: int = 7
    exact_duration_tolerance_seconds: int = 3
    retry_unmatched_after_days: int = 7
    telegram_per_track: bool = True
    dry_run: bool = False
    require_youtube_match_for_spotify: bool = False
    # One song, one version: source playlists overlap ("Song - Slowed" here,
    # "Song - Ultra Slowed" there). The first version added wins.
    dedupe_versions: bool = True
    # Keep the two playlists growing together: write YouTube first, so a
    # YouTube budget stop cannot leave Spotify running ahead of it.
    spotify_youtube_parity: bool = True
    youtube_region_code: str = "US"
    spotify_write_mode: str = "queue"  # queue | direct  (queue = far fewer calls)
    spotify_request_delay_seconds: float = 2.0  # gap between every Spotify API call
    # Cheap reads must not eat the whole run: after this many requests (counted
    # after reading a playlist) the remaining reads use the web-player route,
    # which costs no rate-limit budget.
    spotify_max_requests_per_run: int = 40
    # Pre-work (reads + target playlist load) may not take longer than this; past
    # it the run continues with whatever it already has.
    startup_time_budget_seconds: int = 420
    spotify_proxy_url: str = ""  # CORS proxy for Spotify calls (clean egress IP)
    spotify_web_fallback: bool = True  # read public playlists from the HTML page on 429
    spotify_collection_fallback: bool = True  # use a saved collection when the API is blocked
    spotify_queue_flush_size: int = 40
    manual_queue_path: str = "data/manual-queue.txt"
    version_queue_path: str = "data/version-duplicates.txt"
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
        # Relative data paths belong next to the config file, not to whatever
        # directory the command was run from (a --config elsewhere used to write
        # the cache/state into the current directory instead).
        base = path.parent.resolve()
        if path.exists():
            for attr in _PATH_FIELDS:
                current = getattr(cfg, attr, "")
                if not isinstance(current, str) or not current or Path(current).is_absolute():
                    continue
                setattr(cfg, attr, str(base / current))

        # Source playlists, first non-empty wins:
        #   1. SPOTIFY_SOURCE_PLAYLISTS env/secret — a local or one-off override.
        #   2. a committed text file (explicit path or auto-detected name) — the
        #      easy, no-Secret way; the workflow no longer injects the secret, so
        #      this file is authoritative in CI.
        #   3. the inline config.json list.
        env_sources = os.environ.get("SPOTIFY_SOURCE_PLAYLISTS", "")
        source_file = _discover_source_file(cfg, base)
        file_ids = _read_source_file(source_file) if source_file is not None else []
        if env_sources:
            cfg.spotify_source_playlists = _split_ids(env_sources)
            cfg.spotify_source_playlists_origin = "secret: SPOTIFY_SOURCE_PLAYLISTS"
        elif file_ids:
            cfg.spotify_source_playlists = file_ids
            cfg.spotify_source_playlists_origin = f"file: {source_file.name}"
            log.info("Read %d source playlist(s) from %s", len(file_ids), source_file.name)
        elif cfg.spotify_source_playlists:
            cfg.spotify_source_playlists_origin = "config.json"
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
        pause = _env_number("YOUTUBE_FREE_SEARCH_PAUSE", 0.0, 10.0)
        if pause is not None:
            cfg.youtube_free_search_pause = pause
        per_query = _env_number("SEARCH_RESULTS_PER_QUERY", 1, 50)
        if per_query is not None:
            cfg.search_results_per_query = int(per_query)
        if "YOUTUBE_FREE_SEARCH" in os.environ:
            cfg.youtube_free_search = os.environ["YOUTUBE_FREE_SEARCH"].strip().lower() in {
                "1", "true", "yes",
            }
        if os.environ.get("MAX_INSERTS_PER_DAY"):
            cfg.youtube_max_inserts_per_day = int(os.environ["MAX_INSERTS_PER_DAY"])
        if os.environ.get("YOUTUBE_WRITES_ONLY", "").strip().lower() in {"1", "true", "yes"}:
            cfg.youtube_writes_only = True
        if os.environ.get("MAX_TRACKS_PER_RUN"):
            cfg.max_tracks_per_run = int(os.environ["MAX_TRACKS_PER_RUN"])
        # A dry run must respect the same cap: "first 200" is a default, not a rule.
        if cfg.dry_run:
            if cfg.max_tracks_per_run:
                cfg.dry_run_track_limit = min(
                    cfg.dry_run_track_limit or cfg.max_tracks_per_run,
                    cfg.max_tracks_per_run,
                )
            elif os.environ.get("DRY_RUN_TRACK_LIMIT"):
                cfg.dry_run_track_limit = int(os.environ["DRY_RUN_TRACK_LIMIT"])

        # Accept the short name too: people save the secret as PROXY_URL.
        # Only the config object is touched (engine copies it into the env), so a
        # `check` run cannot leave the process pointed at a proxy.
        if not os.environ.get("SPOTIFY_PROXY_URL", "").strip():
            short = os.environ.get("PROXY_URL", "").strip()
            if short and not cfg.spotify_proxy_url:
                cfg.spotify_proxy_url = short

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
                "No source Spotify playlists configured. Add their links to the "
                "committed 'Scoure playlist.txt' file (one per line), set "
                "spotify_source_playlists in config.json, or the "
                "SPOTIFY_SOURCE_PLAYLISTS secret."
            )
        if not self.spotify_target_playlist:
            raise ConfigError("spotify_target_playlist is not configured")
        if self.youtube_enabled and not self.youtube_target_playlist:
            raise ConfigError("youtube_target_playlist is not configured (YouTube sync is enabled)")
        if self.spotify_target_playlist in self.spotify_source_playlists:
            raise ConfigError("Target Spotify playlist must not also be a source playlist")
        if self.dry_run_track_limit < 0:
            raise ConfigError("dry_run_track_limit must be 0 (unlimited) or a positive integer")


def _json_text(value: Any) -> str:
    """A JSON blob may arrive as text OR as an already-parsed object.

    Pasting the client JSON *inside* the accounts list makes it a nested object;
    str() on that produces Python repr with single quotes, which is not JSON and
    blew up the whole run. Re-serialize it instead.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value or "")


def _normalize_oauth_account(item: dict[str, Any]) -> dict[str, str]:
    """Accept every shape a real Google JSON download comes in.

    * {"client_json": "<text or object>", "refresh_token": "1//…"}
    * {"installed": {…}} / {"web": {…}} with a refresh_token next to it — the
      client JSON *is* the item, which is what you get by pasting the download
      and adding one line.
    * a flat token.json: {"client_id": …, "client_secret": …, "refresh_token": …}
    """
    section = item.get("installed") or item.get("web")
    entry = {
        "client_id": str(item.get("client_id", "") or ""),
        "client_secret": str(item.get("client_secret", "") or ""),
        "refresh_token": str(item.get("refresh_token", "") or ""),
        "client_json": _json_text(item.get("client_json")),
        "token_json": _json_text(item.get("token_json")),
    }
    if isinstance(section, dict):
        # The download itself is the account: keep it whole so token_uri and
        # every other field survive, and take the token from beside it.
        entry["client_json"] = entry["client_json"] or json.dumps(item)
        entry["client_id"] = entry["client_id"] or str(section.get("client_id", "") or "")
        entry["client_secret"] = (
            entry["client_secret"] or str(section.get("client_secret", "") or "")
        )
        entry["refresh_token"] = (
            entry["refresh_token"] or str(section.get("refresh_token", "") or "")
        )
    return entry


def _extra_youtube_oauth(first) -> list[dict[str, str]]:
    """Collect OAuth accounts 2..N, in order, from Secrets.

    Two shapes, so the user can pick whichever is easier in the GitHub UI:

    * numbered secrets — YT_CLIENT_ID_2 / YT_CLIENT_SECRET_2 /
      YT_REFRESH_TOKEN_2 (also YT_CLIENT_JSON_2 or YT_TOKEN_JSON_2), _3, _4 …
    * one secret holding a JSON list — YOUTUBE_OAUTH_ACCOUNTS

    A number is only used when it actually carries a credential, so a gap in
    the numbering does not hide the accounts after it.
    """
    accounts: list[dict[str, str]] = []
    raw = first("YOUTUBE_OAUTH_ACCOUNTS", "YT_OAUTH_ACCOUNTS")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"YOUTUBE_OAUTH_ACCOUNTS is not valid JSON: {exc}"
            ) from exc
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            raise ConfigError(
                "YOUTUBE_OAUTH_ACCOUNTS must be a JSON list of accounts"
            )
        for item in parsed:
            if isinstance(item, dict):
                accounts.append(_normalize_oauth_account(item))
    for n in range(2, 11):
        entry = {
            "client_id": first(f"YT_CLIENT_ID_{n}", f"YOUTUBE_CLIENT_ID_{n}"),
            "client_secret": first(f"YT_CLIENT_SECRET_{n}", f"YOUTUBE_CLIENT_SECRET_{n}"),
            "refresh_token": first(f"YT_REFRESH_TOKEN_{n}", f"YOUTUBE_REFRESH_TOKEN_{n}"),
            "client_json": first(f"YT_CLIENT_JSON_{n}", f"YOUTUBE_CLIENT_JSON_{n}"),
            "token_json": first(
                f"YT_TOKEN_JSON_{n}", f"YOUTUBE_OAUTH_TOKEN_JSON_{n}",
                f"YT_OAUTH_TOKEN_JSON_{n}",
            ),
        }
        if any(entry.values()):
            accounts.append(entry)
    return accounts


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
    youtube_cookie: str = ""  # signed-in music.youtube.com Cookie header
    youtube_authuser: str = "0"
    youtube_api_key: str = ""
    # Spare OAuth accounts (different Google Cloud projects = a fresh daily
    # quota each). Only used after the previous account returns quotaExceeded.
    youtube_extra_oauth: list[dict[str, str]] = field(default_factory=list)
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

        extra_oauth = _extra_youtube_oauth(_first)

        return cls(
            spotify_cookie=os.environ.get("SPOTIFY_COOKIE", ""),
            spotify_spare_cookies=spares,
            youtube_client_json=_first("YT_CLIENT_JSON", "YOUTUBE_CLIENT_JSON", "YOUTUBE_OAUTH_CLIENT_JSON"),
            youtube_client_id=_first("YT_CLIENT_ID", "YOUTUBE_CLIENT_ID", "GOOGLE_CLIENT_ID"),
            youtube_client_secret=_first("YT_CLIENT_SECRET", "YOUTUBE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"),
            youtube_refresh_token=_first("YT_REFRESH_TOKEN", "YOUTUBE_REFRESH_TOKEN"),
            youtube_cookie=_first("YT_COOKIE", "YOUTUBE_COOKIE", "YTMUSIC_COOKIE"),
            youtube_authuser=_first("YT_AUTHUSER", "YOUTUBE_AUTHUSER") or "0",
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
            youtube_extra_oauth=extra_oauth,
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        )

    def token_problems(self) -> list[str]:
        """Catch swapped/expired-shape tokens before a run starts."""
        from .oauth_setup import check_token_types

        problems = check_token_types(
            self.youtube_refresh_token, self.youtube_access_token
        )
        # Use the runtime parser, not the presence of a secret NAME: the client
        # secret may already be inside YOUTUBE_OAUTH_TOKEN_JSON_2. Never print
        # any credential value in these diagnostics.
        from .errors import AuthError
        from .youtube import YouTubeClient

        for n, extra in enumerate(self.youtube_extra_oauth, start=2):
            try:
                bundle = YouTubeClient.oauth_bundle(
                    client_json=extra.get("client_json", ""),
                    client_id=extra.get("client_id", ""),
                    client_secret=extra.get("client_secret", ""),
                    refresh_token=extra.get("refresh_token", ""),
                    token_json=extra.get("token_json", ""),
                )
            except AuthError as exc:
                problems.append(f"OAuth account #{n}: {exc}")
                continue
            token = bundle["refresh_token"].strip()
            if not token:
                if any(bundle.get(key) for key in ("client_id", "client_secret")):
                    problems.append(
                        f"OAuth account #{n}: has a client JSON but no refresh token. "
                        "Add the refresh_token beside it (or set "
                        f"YT_REFRESH_TOKEN_{n}) — the client JSON alone cannot add videos."
                    )
                continue
            for problem in check_token_types(token, ""):
                problems.append(f"OAuth account #{n}: {problem}")
            if not bundle["client_id"]:
                problems.append(
                    f"OAuth account #{n}: has a refresh token but no OAuth client. "
                    f"Add YT_CLIENT_JSON_{n} (the JSON you downloaded for that "
                    f"project), or YT_CLIENT_ID_{n} + YT_CLIENT_SECRET_{n}. "
                    "A refresh token only works with the client that issued it."
                )
            elif not bundle["client_secret"]:
                problems.append(
                    f"OAuth account #{n}: has a refresh token but no client secret. "
                    f"Add YT_CLIENT_SECRET_{n} from the SAME OAuth client, or "
                    "include client_secret in that account's JSON. A client ID "
                    "or a JSON secret name alone does not supply the secret."
                )
        return problems

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
                    or self.youtube_refresh_token  # the token alone is enough
                    or (self.youtube_client_id and self.youtube_client_secret)
                )
                and (self.youtube_refresh_token or self.youtube_token_json)
            )
            has_cookie = bool(self.youtube_cookie)
            if not (self.youtube_api_key or has_oauth or has_cookie):
                missing.append(
                    "YOUTUBE_API_KEY (read-only search) or YT_COOKIE "
                    "(writes, no Console) or YT_CLIENT_JSON + YT_REFRESH_TOKEN"
                )
        if require_telegram and not (self.telegram_bot_token and self.telegram_chat_id):
            missing.append("TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
        if missing:
            raise ConfigError("Missing required secrets: " + ", ".join(missing))


def load_dotenv(path: str | Path = ".env") -> None:  # noqa: D401
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
