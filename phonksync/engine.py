"""Sync orchestrator.

Default mode is Spotify-only: read every source Spotify playlist and copy the
exact tracks into one target Spotify playlist. YouTube sync is optional and
disabled by default; when enabled, it also finds the exact same recording on
YouTube through the 31-method pipeline and adds it to a YouTube playlist.
The JSON state DB (committed back to the repo by CI) guarantees a track is
never added twice.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cache import SearchCache, SourceCache
from .db import StateDB
from .errors import AuthError, QuotaExceeded, RateLimited
from .matcher import Matcher
from .models import Track
from .normalize import song_family
from .overrides import Overrides
from .settings import Config, ConfigError, Secrets
from .spotify import PublicPlaylistReader, SpotifyClient
from .telegram import TelegramLogger, escape
from .youtube import YouTubeClient


def _same_instant(first: str, second: str, tolerance: float = 5.0) -> bool:
    """True when two ISO timestamps are within a few seconds of each other."""
    try:
        a = datetime.fromisoformat(first)
        b = datetime.fromisoformat(second)
    except (TypeError, ValueError):
        return False
    return abs((a - b).total_seconds()) <= tolerance


def _human_wait(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    started_at: str = ""
    finished_at: str = ""
    sources_read: int = 0
    unique_tracks: int = 0
    already_synced: int = 0
    added: int = 0
    unmatched: int = 0
    budget_skips: int = 0
    deferred: int = 0
    duplicates: int = 0
    version_duplicates: int = 0
    oauth_switches: int = 0
    oauth_accounts: int = 1
    errors: list[str] = field(default_factory=list)
    method_wins: dict[str, int] = field(default_factory=dict)
    quota_stopped: bool = False
    quota_reason: str = ""
    partial_sources: int = 0
    cached_sources: int = 0
    queued: int = 0
    queued_flushes: int = 0
    web_player_writes: int = 0  # written via the web player during a Web API cooldown
    manual_queued: int = 0
    parked_spotify: int = 0  # still waiting (Spotify refused again)
    parked_saved: int = 0    # parked by an earlier run, added in this one
    parked_repaired: int = 0  # state-file entries fixed (parked but marked added)
    parked_already_there: int = 0  # parked, but really in the playlist all along
    spotify_note: str = ""   # why Spotify refused the writes (one line)
    spotify_note_writes: bool = False  # the note is about a refused *write*
    spotify_writes_missing: int = 0    # claimed added, but not in the playlist
    spotify_playlist_empty: bool = False  # the target playlist read as empty
    skipped: int = 0
    web_reads: int = 0
    stale_reads: int = 0
    backend: str = ""
    rate_limited_skips: int = 0
    free_searches: int = 0
    free_pages: int = 0
    skipped_by_choice: int = 0
    youtube_readonly: int = 0
    skipped_sources: int = 0
    pending_youtube_flushed: int = 0
    youtube_write_problem: str = ""
    youtube_inserted: int = 0
    added_tracks: list[str] = field(default_factory=list)
    failed_tracks: list[str] = field(default_factory=list)
    daily_details: list[str] = field(default_factory=list)
    cooldown_until: float = 0.0
    cooldown_hint_left: float = 0.0
    elapsed_seconds: float = 0.0
    # (display name, seconds, methods tried) for every matched track
    track_times: list[tuple[str, float, int]] = field(default_factory=list)
    candidate_pool: int = 0  # near-misses kept for a later, reviewed add
    youtube_auth_failed: bool = False  # credential refused before any work
    setup_failed: bool = False  # nothing could run at all
    free_matches: int = 0
    spotify_throttled: bool = False
    searches_used: int = 0
    inserted: int = 0
    search_mode: str = ""
    write_mode: str = ""
    dry_run: bool = False
    source_read_limit: int = 0
    max_tracks_per_run: int = 0
    # Where the songs went: id + human name, so the message is a receipt you
    # can click instead of a wall of counters.
    spotify_target_id: str = ""
    spotify_target_name: str = ""
    youtube_target_id: str = ""
    youtube_target_name: str = ""
    youtube_enabled: bool = False
    youtube_daily_cap: int = 0

    def skipped_total(self) -> int:
        """Only explicit user skips; known synced songs and versions are not work."""
        return self.skipped_by_choice

    def failed_total(self) -> int:
        """Unmatched plus errored — the two ways a song did not make it."""
        return self.unmatched + self.skipped

    def status(self) -> tuple[str, str]:
        """Headline for the short message.

        Truthful by construction: SUCCESS is only printed when nothing was
        left behind. A throttled *read* that fell back to the web player is
        not a failure, but a parked write or an unmatched song is PARTIAL.
        """
        if self.setup_failed or (self.errors and not self.added):
            return "❌", "FAILED"
        if self.dry_run:
            return "🧪", "DRY RUN"
        if (
            self.failed_total() or self.errors or self.spotify_writes_missing
            or self.youtube_write_problem or self.quota_stopped
            or self.parked_spotify or self.manual_queued or self.rate_limited_skips
            or self.deferred or self.budget_skips
        ):
            return "⚠️", "PARTIAL"
        return "✅", "SUCCESS"

    def summary_text(self) -> str:
        """The short, clickable end-of-run message (Telegram + Actions log).

        Style: status, where it went, one line of counts, then only the few
        details that change what you do next. The full diagnostic version is
        still available as `verbose_summary_text()`.
        """
        emoji, word = self.status()
        lines = [f"{emoji} <b>SYNC: {word}</b>"]
        if self.spotify_target_id:
            name = f" — {escape(self.spotify_target_name)}" if self.spotify_target_name else ""
            lines.append(f"🎵 Spotify playlist{name}")
            lines.append(f"https://open.spotify.com/playlist/{self.spotify_target_id}")
        if self.youtube_target_id:
            name = f" — {escape(self.youtube_target_name)}" if self.youtube_target_name else ""
            lines.append(f"▶️ YouTube playlist{name}")
            lines.append(f"https://www.youtube.com/playlist?list={self.youtube_target_id}")

        added_word = "Would add" if self.dry_run else "Added"
        failed = self.failed_total()
        lines.append(
            f"➕ {added_word}: <b>{self.added}</b> | ⏭ Skipped: {self.skipped_total()} | "
            f"{'❌' if failed else '✅'} Failed: {failed}"
        )

        # Counts read from sources, not a report of the existing DB backlog.
        context = f"{self.unique_tracks} tracks · {self.sources_read} playlist(s)"
        if self.skipped_by_choice:
            context += f" · {self.skipped_by_choice} skipped by choice"
        lines.append(f"🎧 {context}")

        if self.youtube_enabled or self.youtube_inserted or self.inserted or self.free_matches:
            bits = [f"{self.youtube_inserted} added"]
            if self.youtube_inserted:
                bits[0] += " (confirmed by Google)"
            if self.inserted:
                bits.append(
                    f"today {self.inserted}"
                    + (f"/{self.youtube_daily_cap}" if self.youtube_daily_cap else "")
                )
            if self.free_matches:
                bits.append(f"{self.free_matches} matched with 0 quota")
            lines.append("▶️ YouTube: " + " · ".join(bits))

        if self.elapsed_seconds >= 1:
            lines.append(f"⏱ Took {_human_wait(self.elapsed_seconds)}")

        # ---- only the lines that change what you do next ----
        if self.youtube_write_problem:
            lines.append(f"⚠️ YouTube: {self.youtube_write_problem}")
            if self.youtube_readonly:
                lines.append(
                    f"📝 {self.youtube_readonly} match(es) remembered — added once this is fixed"
                )
        if self.oauth_switches:
            lines.append(
                f"🔁 YouTube OAuth: switched account {self.oauth_switches} time(s) "
                "(daily target or quota) — the run continued"
            )
        if self.quota_stopped:
            lines.append(f"⚠️ Stopped early: {escape(self.quota_reason or 'YouTube quota budget reached')}")
        if self.spotify_throttled:
            if self.web_player_writes:
                lines.append(
                    f"⚠️ Spotify API rate limited (429); {self.web_player_writes} "
                    "track(s) were added through the web player"
                )
            elif not self.inserted and not self.added and not self.spotify_note_writes:
                lines.append(
                    "⚠️ Spotify rate limited (429) — the web player route was used "
                    "instead; nothing to fix"
                )
            else:
                lines.append(
                    "⚠️ Spotify rate limited (429): wrote what it could — "
                    "run again to continue"
                )
        elif self.web_player_writes:
            lines.append(
                f"🌐 Added {self.web_player_writes} track(s) through the web player "
                "(Spotify Web API was cooling down)"
            )
        if self.spotify_writes_missing:
            lines.append(
                f"🚨 {self.spotify_writes_missing} track(s) accepted by Spotify but "
                "are NOT in the target playlist — kept in the backlog"
            )
            if self.spotify_playlist_empty:
                lines.append("⚠️ The target playlist reads as EMPTY — check SPOTIFY_TARGET_PLAYLIST")
        if self.spotify_note:
            lines.append(self.spotify_note)
        if self.parked_saved:
            lines.append(f"♻️ Added {self.parked_saved} track(s) an earlier run had to park")
        if self.parked_already_there:
            lines.append(
                f"✅ {self.parked_already_there} parked track(s) were already in the "
                "playlist — counted, not added twice"
            )
        if self.manual_queued:
            lines.append(f"📋 Parked for the next run: {self.manual_queued} track(s)")
        if self.parked_spotify and not self.parked_repaired:
            lines.append(
                f"📋 Still parked: {self.parked_spotify} track(s) — the next run adds them"
            )
        if self.queued:
            lines.append(
                f"🧺 Spotify writes via queue: {self.queued} tracks "
                f"({self.queued_flushes} batch save(s))"
            )
        if self.rate_limited_skips:
            lines.append(
                f"⏸ Skipped due to Spotify 429: {self.rate_limited_skips} — resume point saved"
            )
        if self.budget_skips:
            lines.append(f"⏸ Out of search budget: {self.budget_skips} (retried next run)")
        if self.deferred:
            lines.append(f"🕒 Deferred (retried later): {self.deferred}")
        if self.skipped:
            lines.append(f"⚠️ Errored: {self.skipped} (retried next run)")
        if self.failed_tracks:
            lines.append(f"<b>Not added</b> ({len(self.failed_tracks)}):")
            lines.extend(f"  • {line}" for line in self.failed_tracks[:5])
            if len(self.failed_tracks) > 5:
                lines.append(f"  • …and {len(self.failed_tracks) - 5} more (see the full log)")
        if self.candidate_pool:
            lines.append(
                f"❓ Near-miss candidates kept for review: {self.candidate_pool} "
                "(never added automatically)"
            )
        if self.dry_run:
            if self.source_read_limit:
                cap = self.max_tracks_per_run
                lines.append(
                    f"🧪 DRY RUN — previewed {self.source_read_limit} track(s)"
                    + (f" (your limit: {cap})" if cap and cap <= self.source_read_limit else "")
                    + "; nothing was written"
                )
            else:
                lines.append("🧪 DRY RUN — nothing was written")
        if self.setup_failed:
            lines.append(
                "🛑 <b>Nothing could run</b> — the setup itself was refused "
                "(credential, playlist id or config)"
            )
        if self.errors:
            lines.append(f"🛑 Errors: {len(self.errors)}")
            lines.extend(escape(e)[:200] for e in self.errors[:3])
        if self.cooldown_until:
            lines.append(
                "ℹ️ Next run: "
                + datetime.fromtimestamp(self.cooldown_until, timezone.utc).strftime("%H:%M UTC")
            )
        return "\n".join(lines)

    def verbose_summary_text(self) -> str:
        """The full diagnostic view (every counter, route and note).

        Kept for debugging: `PHONK_VERBOSE_SUMMARY=1` sends this instead, and it
        is always written to the log at DEBUG level. Added songs are deliberately
        not listed: the playlist is the receipt.
        """
        head = "🧪 Dry run finished" if self.dry_run else "📊 Sync run finished"
        added_word = "Would add" if self.dry_run else "Added"
        lines = [
            f"<b>━━ {head} ━━</b>",
            f"✅ {added_word}: <b>{self.added}</b>"
            + (f" (YouTube inserts: {self.youtube_inserted})" if self.youtube_inserted else ""),
            f"⏭ Skipped by choice: {self.skipped_by_choice}",
            # A zero count is not a failure. The old red ❌ next to
            # "Failed / unmatched: 0" made a clean run read like a broken one.
            f"{'❌' if self.unmatched else '✅'} Failed / unmatched: {self.unmatched}",
        ]
        if self.oauth_switches:
            lines.append(
                f"🔁 YouTube OAuth: switched account {self.oauth_switches} time(s) "
                f"({self.oauth_accounts} accounts configured; daily target or quota) — the run continued"
            )
        if self.deferred:
            lines.append(f"🕒 Deferred (retried later): {self.deferred}")
        if self.budget_skips:
            lines.append(f"⏸ Out of search budget: {self.budget_skips} (retried next run)")
        if self.skipped:
            lines.append(f"⚠️ Errored: {self.skipped} (retried next run)")
        lines.append(
            f"🎵 {self.sources_read} playlist(s), {self.unique_tracks} tracks read"
        )
        if self.failed_tracks:
            lines.append(f"<b>Not added</b> ({len(self.failed_tracks)}):")
            lines.extend(f"  • {line}" for line in self.failed_tracks)
        if self.pending_youtube_flushed:
            lines.append(
                f"✍️ Added {self.pending_youtube_flushed} previously matched video(s) "
                "that were waiting for YouTube auth"
            )
        if self.youtube_write_problem:
            lines.append("<b>⚠️ YouTube</b>")
            lines.append(f"  {self.youtube_write_problem}")
            if self.youtube_readonly:
                lines.append(
                    f"  📝 {self.youtube_readonly} match(es) remembered, added "
                    "automatically once this is fixed"
                )
        if self.method_wins:
            wins = ", ".join(f"{k}={v}" for k, v in sorted(self.method_wins.items()))
            lines.append(f"🏆 Methods: {wins}")
        if self.free_matches or self.free_searches:
            pages = f" + {self.free_pages} video pages" if self.free_pages else ""
            lines.append(
                f"🆓 Free YouTube: {self.free_searches} search(es){pages} (0 quota), "
                f"matches: {self.free_matches}"
            )
            if self.unique_tracks and self.free_pages > 6 * self.unique_tracks:
                lines.append(
                    "💡 That is a lot of pages — a smaller `results_per_query` "
                    "(or the workflow input `results_per_query`) cuts the run time"
                )
        if self.write_mode:
            lines.append(f"✍️ YouTube writes: {self.write_mode}")
        if self.youtube_inserted:
            # The proof that the credential can actually write, not just refresh.
            lines.append(
                f"✅ YouTube inserts confirmed by Google: {self.youtube_inserted}"
            )

        if self.searches_used:
            lines.append(
                f"🔎 Searches this run: {self.searches_used} (mode: {self.search_mode or 'n/a'})"
            )
        if self.queued:
            lines.append(
                f"🧺 Spotify writes via queue: {self.queued} tracks "
                f"({self.queued_flushes} batch save(s))"
            )
        if self.rate_limited_skips:
            lines.append(
                f"⏸ Skipped due to Spotify 429: {self.rate_limited_skips} — "
                "resume point saved, continue next run"
            )
        if self.web_player_writes:
            lines.append(
                f"🌐 Added {self.web_player_writes} track(s) through the web player "
                "while the Spotify Web API was cooling down"
            )
        if self.manual_queued:
            lines.append(
                f"📋 Parked for the next run: {self.manual_queued} track(s) — queue "
                "full or Spotify slow-down (`phonksync manual`)"
            )
        if self.parked_saved:
            lines.append(
                f"♻️ Added {self.parked_saved} track(s) an earlier run had to park"
            )
        if self.parked_already_there:
            lines.append(
                f"✅ {self.parked_already_there} parked track(s) were already in the "
                "playlist — counted, not added twice"
            )
        if self.parked_spotify and not self.parked_repaired:
            lines.append(
                f"📋 Still parked: {self.parked_spotify} track(s) — Spotify asked to "
                "slow down; the next run adds them"
            )
        if self.parked_repaired and not self.parked_saved and self.parked_spotify:
            lines.append(
                f"🩹 Re-queued {self.parked_repaired} track(s) an older run parked but "
                "recorded as added (still throttled — next run adds them)"
            )
        if self.backend:
            lines.append(f"🔀 Spotify backend: {self.backend}")
        if self.web_reads:
            lines.append(f"🌐 Reads without the Web API (throttled): {self.web_reads}")
        if self.cached_sources:
            lines.append(f"♻️ Cached playlist reads reused: {self.cached_sources}")
        if self.stale_reads:
            lines.append(
                f"🗄 Stale cache reads replayed: {self.stale_reads} "
                "(no Spotify calls used — refresh by running with a clear cache)"
            )
        # Three different stories share the phrase "Spotify 429": a read that fell
        # back to the web player (harmless), a write that was refused (the log
        # said "Added: 5" while the playlist stayed empty), and a stalled run.
        if self.spotify_throttled:
            if not self.inserted and not self.added and not self.spotify_note_writes:
                lines.append(
                    "⚠️ Spotify's Web API was throttled (429) — the run used the web "
                    "player route instead. Nothing to fix; the next run continues."
                )
            else:
                lines.append(
                    "⚠️ Spotify rate limited (429): wrote what it could — "
                    "wait a few minutes and run again to continue"
                )
        if self.spotify_writes_missing:
            lines.append(
                f"🚨 {self.spotify_writes_missing} track(s) were accepted by Spotify but "
                "are NOT in the target playlist — kept in the backlog, never marked as "
                "added"
            )
            if self.spotify_playlist_empty:
                lines.append(
                    "⚠️ The target playlist reads as EMPTY — check "
                    "SPOTIFY_TARGET_PLAYLIST (wrong id?) before anything else"
                )
        if self.spotify_note:
            lines.append(self.spotify_note)
        if self.quota_stopped:
            lines.append(f"⚠️ Stopped early: {escape(self.quota_reason or 'YouTube quota budget reached')}")
        elif self.inserted:
            lines.append("ℹ️ Daily insert total so far: " + str(self.inserted))
        if self.dry_run:
            if self.source_read_limit:
                cap = self.max_tracks_per_run
                lines.append(
                    f"🧪 DRY RUN — previewed {self.source_read_limit} track(s)"
                    + (f" (your limit: {cap})" if cap and cap <= self.source_read_limit else "")
                    + "; nothing was written"
                )
            else:
                lines.append("🧪 DRY RUN — nothing was written")
        if self.elapsed_seconds >= 30:
            per = ""
            if self.unique_tracks:
                per = f" ({self.elapsed_seconds / self.unique_tracks:.0f}s per track)"
            lines.append(
                f"⏱ Took {_human_wait(self.elapsed_seconds)} for "
                f"{self.unique_tracks} track(s){per}"
            )
        if self.candidate_pool:
            lines.append(
                f"❓ Near-miss candidates kept for review: {self.candidate_pool} "
                "(never added automatically — `phonksync pending`)"
            )
        if self.track_times:
            slowest = sorted(self.track_times, key=lambda t: t[1], reverse=True)[:3]
            slowest = [t for t in slowest if t[1] >= 5]
            if slowest:
                parts = ", ".join(
                    f"{escape(name.split(' — ')[0])} {secs:.0f}s/{methods} methods"
                    for name, secs, methods in slowest
                )
                lines.append(f"🐌 Slowest: {parts}")
        if self.setup_failed:
            lines.append(
                "🛑 <b>Nothing could run</b> — the setup itself was refused "
                "(credential, playlist id or config). The reason is above."
            )
        if self.errors:
            lines.append(f"🛑 Errors: {len(self.errors)}")
            lines.extend(escape(e)[:200] for e in self.errors[:5])
        if self.daily_details:
            lines.append("")
            lines.append("<b>Details</b>")
            lines.extend(self.daily_details)
        if self.cooldown_until:
            lines.append(
                "ℹ️ Next run: "
                + datetime.fromtimestamp(
                    self.cooldown_until, timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
            )
        return "\n".join(line for line in lines if line is not None)


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class SyncEngine:
    def __init__(self, config: Config, secrets: Secrets):
        self.config = config
        self.secrets = secrets
        self.telegram = TelegramLogger(
            secrets.telegram_bot_token, secrets.telegram_chat_id
        )
        self.state = StateDB(config.state_path)
        if "REQUEST_DELAY_SECONDS" not in os.environ:
            os.environ["REQUEST_DELAY_SECONDS"] = str(config.spotify_request_delay_seconds)
        if config.spotify_proxy_url and "SPOTIFY_PROXY_URL" not in os.environ:
            os.environ["SPOTIFY_PROXY_URL"] = config.spotify_proxy_url
        # A bot account is never Premium: start on the web-player route (no
        # Premium-only queue, no Web API write budget) instead of discovering it
        # from the first 403/429. SPOTIFY_BACKEND=rest forces the old behaviour.
        backend = os.environ.get("SPOTIFY_BACKEND", "").strip().lower()
        self.spotify = SpotifyClient(
            secrets.spotify_cookie,
            extra_cookies=secrets.spotify_spare_cookies,
            prefer_web_player=backend not in {"rest", "web-api", "api"},
        )
        self._free_matches = 0
        self.public_spotify = PublicPlaylistReader() if config.spotify_web_fallback else None
        self.source_cache = SourceCache(
            config.source_cache_path,
            ttl_hours=config.source_cache_ttl_hours,
            allow_stale=config.spotify_cache_allow_stale,
            stale_days=config.spotify_cache_max_age_days,
        )

        # YouTube sync is optional and disabled by default (Spotify-only mode).
        self.youtube = None
        self.matcher = None
        self.cache = None
        self.overrides = None
        if config.youtube_enabled:
            self.overrides = Overrides(config.overrides_path)
            self.cache = SearchCache(config.cache_path, ttl_days=config.search_cache_ttl_days)
            self.youtube = YouTubeClient(
                client_json=secrets.youtube_client_json,
                client_id=secrets.youtube_client_id,
                client_secret=secrets.youtube_client_secret,
                refresh_token=secrets.youtube_refresh_token,
                token_json=secrets.youtube_token_json,
                api_key=secrets.youtube_api_key,
                daily_quota_units=config.youtube_daily_quota_units,
                free_search=config.youtube_free_search,
                free_search_pause=config.youtube_free_search_pause,
                access_token=secrets.youtube_access_token,
                cookie=secrets.youtube_cookie,
                authuser=secrets.youtube_authuser,
                max_searches_per_run=config.max_searches_per_run,
                max_searches_per_day=config.youtube_max_searches_per_day,
                max_inserts_per_day=config.youtube_max_inserts_per_day,
                quota_state=self.state.quota,
                region_code=config.youtube_region_code,
                extra_oauth=secrets.youtube_extra_oauth,
            )
            self.matcher = Matcher(
                self.youtube,
                self.cache,
                self.overrides,
                results_per_query=config.search_results_per_query,
                duration_tolerance=config.duration_tolerance_seconds,
                exact_duration_tolerance=config.exact_duration_tolerance_seconds,
            )

    # ------------------------------------------------------------------ run

    def _preflight_youtube(self, report: RunReport) -> None:
        """Prove the write path once, before any track spends a search.

        Two real failures this prevents:
          * an OAuth client id without its secret (a half-filled pair) — every
            insert would fail after its 50 quota units were already counted;
          * a swapped/expired-shape token (ya29… in YT_REFRESH_TOKEN).
        """
        yt = self.youtube
        if yt is None or not getattr(yt, "can_write", False):
            return
        try:
            yt._ensure_token()
        except AuthError as exc:
            report.youtube_write_problem = str(exc)
            report.youtube_auth_failed = True
            self._detail(
                report,
                "✍️ YouTube writes are paused for this run — the credential was "
                f"refused before a single track was processed ({escape(str(exc))})",
            )
            log.warning("YouTube credential refused: %s", exc)
            return
        except Exception as exc:  # never let the preflight kill a run
            log.warning("YouTube preflight skipped: %s", exc)
            return
        used = getattr(yt, "client_used", "") or getattr(yt, "client_id", "")
        if used:
            self._detail(
                report,
                f"✍️ YouTube writes: OAuth via client <code>{escape(used[:22])}…</code> "
                "(the token works with this client)",
            )

    def _youtube_write_problem(self) -> str:
        """Why writes are off, in terms of the credential actually missing."""
        yt = self.youtube
        if yt is None:
            return ""
        if getattr(yt, "can_write", True):
            return ""
        mode = getattr(yt, "write_mode", "")
        if mode.startswith("manual"):
            return ""  # writes DO work, just for ~an hour
        diag = getattr(yt, "cookie_diag", None)
        detail = diag() if callable(diag) else ""
        if detail and "no __Secure-3PAPISID" in detail:
            return (
                f"YT_COOKIE is set but unusable — {detail}. Copy the whole "
                "Cookie header from a signed-in music.youtube.com tab "
                "(`phonksync youtube-auth --cookie-help`)."
            )
        if not getattr(yt, "cookie", ""):
            return (
                "YT_COOKIE is not set at all (the YouTube search key is not a "
                "write credential). Add it under Settings → Secrets → Actions, "
                "then run again — matches are already remembered."
            )
        # A cookie IS set; the problem is inside it.
        return (
            f"YT_COOKIE is set but unusable — {detail or 'no usable SAPISID'}. "
            "Re-copy the whole Cookie request header from a signed-in "
            "music.youtube.com tab."
        )

    def _warn_if_youtube_writes_are_off(self) -> None:
        """Say it once, at the start, when matches cannot be added to YouTube."""
        problem = self._youtube_write_problem()
        report = getattr(self, "_current_report", None)
        if report is not None:
            report.youtube_write_problem = problem
        if not problem:
            return
        log.warning(
            "YouTube writes are OFF (record-only mode): matches are stored in state "
            "so nothing is lost, but no video is added to the playlist. %s",
            problem,
        )
        self.telegram.warn(
            "✍️ <b>YouTube writes are OFF</b> — matches are remembered and will be "
            "added automatically on the first run after this is fixed; nothing is "
            f"lost.\nReason: {escape(problem)}"
        )

    def run(self) -> RunReport:
        cfg = self.config
        self._current_report = RunReport(
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            dry_run=cfg.dry_run,
        )
        report = self._current_report
        mode = "DRY RUN" if cfg.dry_run else "live"
        if cfg.dry_run and cfg.dry_run_track_limit:
            mode += f" (first {cfg.dry_run_track_limit} source tracks)"
        targets = "Spotify + YouTube" if cfg.youtube_enabled else "Spotify only"
        if cfg.youtube_enabled and self.youtube is not None:
            mode = getattr(self.youtube, "write_mode", "")
            if not getattr(self.youtube, "can_write", True) and not mode.startswith("manual"):
                targets += (
                    " (read-only: search works, playlist adds need YT_COOKIE or "
                    "YT_REFRESH_TOKEN)"
                )
            elif mode.startswith("manual"):
                targets += f" ({mode})"
        self._detail(
            report,
            f"🎧 Started: {len(cfg.spotify_source_playlists)} source playlist(s) → "
            f"{escape(targets)} (mode: {escape(mode)})",
        )

        self._warn_if_youtube_writes_are_off()
        # Why did a run that looked live write nothing? A missing/blank secret
        # and a real dry run produce the same silence, so name the inputs.
        self._detail(report, self._write_capability_line())



        # A remembered cooldown is a *hint*, never a wall. The run always happens:
        # a hint only means "do not spend Web API calls yet" — the web-player
        # route (which is not rate limited) does the reading instead.
        self._cooldown_hint_left = 0.0
        cooldown_left = self.state.spotify_cooldown_until() - time.time()
        if cooldown_left > 0:
            self._cooldown_hint_left = cooldown_left
            report.cooldown_hint_left = cooldown_left
            self._detail(
                report,
                f"⏩ Spotify asked to slow down ({_human_wait(cooldown_left)} left) — "
                "reading through the web-player route, no Web API calls this run",
            )

        self._prework_started = time.time()
        try:
            tracks = self._collect_source_tracks(report)
            target_spotify_ids = None
            if self._web_api_budget_left():
                try:
                    target_spotify_ids = self.spotify.playlist_track_ids(
                        cfg.spotify_target_playlist
                    )
                except RateLimited:
                    report.spotify_throttled = True  # Spotify reported throttling
            if target_spotify_ids is None:
                # Our own request budget is not a Spotify 429. Both cases use
                # the same fallback, but only a real 429 gets a warning.
                web_target = self._read_pathfinder(
                    cfg.spotify_target_playlist, report
                ) or self._read_public_playlist(cfg.spotify_target_playlist, report, quiet=True)
                if web_target:
                    target_spotify_ids = {t.id for t in web_target}
                else:
                    log.warning("Could not read the target playlist — using state")
                    target_spotify_ids = {
                        k for k, v in self.state.data["synced"].items()
                        if v.get("spotify_added")
                    }
            target_youtube_ids = (
                self.youtube.playlist_video_ids(cfg.youtube_target_playlist)
                if cfg.youtube_enabled
                else set()
            )
        except (AuthError, ConfigError) as exc:
            report.errors.append(str(exc))
            report.setup_failed = True
            # Printed as well as sent: without a Telegram secret the console is
            # the only place this can appear, and the step exits 1 anyway.
            print(f"❌ Setup failed: {exc}", file=sys.stderr)
            self.telegram.error(f"Setup failed: {escape(str(exc))}")
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._send_single(report)
            return report
        except RateLimited as exc:
            report.errors.append(str(exc))
            report.spotify_throttled = True
            report.quota_reason = str(exc)
            self._spotify_reason = str(exc)
            report.spotify_note = self._spotify_diagnosis()
            report.spotify_note_writes = True
            self.telegram.error(f"Spotify rate limit: {escape(str(exc))}")
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._send_single(report)
            return report

        if not cfg.dry_run:
            self._preflight_youtube(report)
            self._flush_pending_youtube(report)
            # The parked backlog goes first: it is one batch, it needs no search,
            # and a Spotify slow-down later in the run must not swallow it again.
            report.parked_repaired = self._repair_parked_bucket()
            report.parked_already_there = self._reconcile_parked(target_spotify_ids)
            self._retry_pending_spotify(report)

        if cfg.youtube_writes_only:
            # Nothing is searched: the run only turns remembered matches into
            # playlist entries (seconds, no quota, no Spotify pages to fetch).
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            report.cooldown_until = getattr(self.spotify, "blocked_until", 0.0) or 0.0
            self._detail(
                report,
                "✍️ Writes-only run — no searches were made, no new matches looked for",
            )
            self._send_single(report)
            return report

        resume = self.state.get_resume_point()
        resume_id = resume.get("track_id", "")
        resume_seen = not resume_id
        if resume_id and not cfg.dry_run:
            self._detail(
                report,
                f"▶️ Resumed after <code>{escape(resume.get('name', ''))}</code>",
            )

        synced_isrcs = {
            (v.get("isrc") or "").upper()
            for v in self.state.data["synced"].values()
            if v.get("isrc")
        }
        # One song, one version. Built from `synced` (oldest first) so the first
        # version that reached a playlist keeps the claim, and grown as this run
        # adds tracks so two source playlists in the SAME run cannot both win.
        families = self.state.family_index() if cfg.dedupe_versions else {}
        self._run_families = families
        processed = 0

        for track in tracks:
            if cfg.max_tracks_per_run and processed >= cfg.max_tracks_per_run:
                break

            # Explicitly skipped by a human: no search, no unmatched record.
            if self.state.is_skipped(track.id):
                report.skipped_by_choice += 1
                continue

            entry = self.state.get_synced(track.id) or {}

            # Same recording already synced under a different Spotify ID?
            # Checked first: an exact ISRC match is a true duplicate, not a
            # "different version", and deserves the plainer report line.
            if track.isrc and track.isrc.upper() in synced_isrcs and not entry:
                report.duplicates += 1
                continue

            # Another version of a song we already added? Decide BEFORE any
            # search: it costs no YouTube quota and cannot reach either playlist.
            if cfg.dedupe_versions and not entry:
                if self.state.is_version_duplicate(track.id):
                    report.version_duplicates += 1
                    continue
                key = song_family(track.name, track.artists)
                kept = families.get(key) if key else None
                if kept and kept.get("spotify_id") != track.id:
                    report.version_duplicates += 1
                    if not cfg.dry_run:
                        self.state.mark_version_duplicate(track, kept)
                    continue

            spotify_done = bool(entry.get("spotify_added")) or track.id in target_spotify_ids
            youtube_done = bool(entry.get("youtube_added")) or not cfg.youtube_enabled

            if spotify_done and youtube_done:
                report.already_synced += 1
                continue

            # Retry policy for previously unmatched tracks.
            if not youtube_done and not entry.get("youtube_video_id") and self._deferred(track):
                report.deferred += 1
                continue

            if not resume_seen:
                if track.id == resume_id:
                    resume_seen = True
                else:
                    report.deferred += 1
                    continue

            processed += 1
            try:
                self._process_track(
                    track, entry, spotify_done, youtube_done,
                    target_spotify_ids, target_youtube_ids, synced_isrcs, report,
                )
            except QuotaExceeded as exc:
                report.quota_stopped = True
                report.quota_reason = str(exc)
                self.telegram.warn(f"YouTube quota reached: {escape(str(exc))}")
                break
            except RateLimited as exc:
                report.spotify_throttled = True
                report.rate_limited_skips += 1
                report.quota_reason = str(exc)
                held = self._hold_resume_point(track)
                self.telegram.warn(
                    f"⏸ Spotify rate limit — skipping {escape(track.display())} "
                    f"and continuing ({report.rate_limited_skips} skipped, "
                    f"{'resume point saved' if held else 'no resume point'})"
                )
                if held:
                    # Writes are parked for the whole window. Do not burn the
                    # remaining tracks one 429 at a time: stop writing, keep the
                    # resume point, and let the run finish normally so the
                    # YouTube side, the state file and the summary still land.
                    self._park_remaining(tracks[processed:], report)
                    break
                continue  # short 429s were already retried: skip this track only
            except Exception as exc:
                log.exception("Track %s failed", track.id)
                report.errors.append(f"{track.display()}: {exc}")
                report.skipped += 1
                self.telegram.warn(
                    f"⏭ Skipped {escape(track.display())}: {escape(str(exc))[:160]}"
                )

        blocked_until = getattr(self.spotify, "blocked_until", 0.0)
        if blocked_until:
            report.spotify_throttled = True
            self._spotify_reason = report.quota_reason or ""
            report.spotify_note = self._spotify_diagnosis()
            report.spotify_note_writes = True
            report.quota_reason = (
                "Spotify asked to slow down until "
                + datetime.fromtimestamp(blocked_until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                + " — this run only, nothing is stored"
            )
            self.telegram.warn(escape(report.quota_reason))

        if resume_id and resume_seen and not cfg.dry_run:
            self.state.clear_resume_point()

        if not cfg.dry_run:
            self._flush_queue()
            self._retry_pending_queue()
            self._audit_claimed_spotify(report)
            # Always rewrite it: an emptied bucket must not leave a stale file
            # claiming tracks are still waiting.
            self.state.write_manual_queue(cfg.manual_queue_path)
            self.state.write_version_queue(cfg.version_queue_path)

        # Record method statistics for every method that was exercised.
        self.state.record_run()
        if cfg.youtube_enabled:
            report.oauth_switches = int(getattr(self.youtube, "oauth_switches", 0) or 0)
            report.oauth_accounts = int(getattr(self.youtube, "oauth_account_count", 1) or 1)
            report.searches_used = int(getattr(self.youtube, "searches_this_run", 0) or 0)
            report.free_searches = int(getattr(self.youtube, "free_searches_this_run", 0) or 0)
            report.free_pages = int(getattr(self.youtube, "free_pages_this_run", 0) or 0)
            report.free_matches = int(getattr(self, "_free_matches", 0) or 0)
            report.inserted = int(getattr(self.youtube.quota, "inserts", 0) or 0)
            report.youtube_inserted = int(
                getattr(self.youtube, "inserts_this_run", 0) or 0
            )
            report.search_mode = getattr(self.youtube, "search_mode", "")
            report.write_mode = getattr(self.youtube, "write_mode", "")
            self.state.save_quota(self.youtube.quota)
        report.youtube_enabled = bool(cfg.youtube_enabled)
        report.youtube_daily_cap = int(cfg.youtube_max_inserts_per_day or 0)
        report.spotify_target_id = cfg.spotify_target_playlist
        report.youtube_target_id = cfg.youtube_target_playlist if cfg.youtube_enabled else ""
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if not cfg.dry_run:
            self.state.save()
        if self.cache is not None:
            self.cache.save()
        self.source_cache.save()

        self._send_single(report)
        plain = report.summary_text().replace("<b>", "").replace("</b>", "")
        log.info("\n%s", plain)
        log.debug(
            "Full run report:\n%s",
            report.verbose_summary_text().replace("<b>", "").replace("</b>", ""),
        )
        return report

    # -------------------------------------------------------------- helpers

    def _collect_source_tracks(self, report: RunReport) -> list[Track]:
        cfg = self.config
        read_limit = cfg.dry_run_track_limit if cfg.dry_run else 0
        if read_limit and cfg.max_tracks_per_run:
            read_limit = min(read_limit, cfg.max_tracks_per_run)
        report.source_read_limit = read_limit
        report.max_tracks_per_run = cfg.max_tracks_per_run

        seen: dict[str, Track] = {}
        for playlist_id in cfg.spotify_source_playlists:
            if read_limit and len(seen) >= read_limit:
                break
            remaining = read_limit - len(seen) if read_limit else None
            cached = self.source_cache.get(playlist_id)
            snapshot = ""
            if cached is not None:
                # One cheap call instead of N pages: reuse the cached read while
                # the playlist has not changed (this is what keeps us under the
                # Spotify rate limit on repeat runs).
                snapshot_fn = getattr(self.spotify, "playlist_snapshot", None)
                try:
                    snapshot = snapshot_fn(playlist_id) if snapshot_fn else ""
                except Exception:
                    snapshot = ""
                if cached["snapshot"] and snapshot == cached["snapshot"]:
                    tracks = cached["tracks"]
                    report.cached_sources += 1
                    self._merge_tracks(seen, tracks, playlist_id, read_limit)
                    report.sources_read += 1
                    continue
                usable = self.source_cache.fresh(playlist_id) or (
                    self.source_cache.allow_stale and bool(cached)
                )
                if not snapshot and usable and not self.source_cache.fresh(playlist_id):
                    age = self.source_cache.age_hours(playlist_id)
                    self.telegram.warn(
                        f"♻️ Spotify did not answer for <code>{playlist_id}</code> — replaying "
                        f"the cached read ({len(cached['tracks'])} tracks, {age:.0f}h old)"
                    )
                    report.cached_sources += 1
                    report.stale_reads += 1
                    self._merge_tracks(seen, cached["tracks"], playlist_id, read_limit)
                    report.sources_read += 1
                    continue
                if not snapshot and self.source_cache.fresh(playlist_id):
                    # Snapshot unknown (blocked?) but the cache is fresh: prefer
                    # the cache over hammering Spotify again.
                    self.telegram.warn(
                        f"♻️ Spotify snapshot unavailable for <code>{playlist_id}</code> — "
                        f"using the cached read ({len(cached['tracks'])} tracks)"
                    )
                    report.cached_sources += 1
                    self._merge_tracks(seen, cached["tracks"], playlist_id, read_limit)
                    report.sources_read += 1
                    continue
            budget_note = ""
            if not self._web_api_budget_left():
                budget_note = self._web_api_note()
            elif not self._startup_budget_left():
                budget_note = "startup time budget used up"
            if budget_note:
                # A self-imposed budget is not a Spotify 429: never claim it is.
                self._detail(
                    report,
                    f"⏩ Reading <code>{escape(playlist_id)}</code> without the Web API "
                    f"— {escape(budget_note)}",
                )
            if budget_note or getattr(self.spotify, "blocked_until", 0.0) > time.time():
                # Spotify already told us to go away this run: do not spend
                # 6 retries (and a longer block) re-learning that. Read the
                # playlist without the Web API instead.
                web_tracks = self._read_pathfinder(playlist_id, report) or \
                    self._read_public_playlist(playlist_id, report)
                if web_tracks:
                    self._merge_tracks(seen, web_tracks, playlist_id, read_limit)
                    report.sources_read += 1
                    if not budget_note:
                        report.spotify_throttled = True
                    continue
                if cached is not None and (
                    self.source_cache.fresh(playlist_id) or self.source_cache.allow_stale
                ):
                    report.cached_sources += 1
                    if not self.source_cache.fresh(playlist_id):
                        report.stale_reads += 1
                    self._merge_tracks(seen, cached["tracks"], playlist_id, read_limit)
                    report.sources_read += 1
                    if not budget_note:
                        report.spotify_throttled = True
                    continue
                if budget_note:
                    # A self-imposed budget: take the saved collection if there is
                    # one, otherwise skip this playlist (counted, not silent).
                    collection = self._read_collection(cfg.collection_path, playlist_id)
                    if collection:
                        self._merge_tracks(seen, collection, playlist_id, read_limit)
                        report.sources_read += 1
                        continue
                    report.skipped_sources += 1
                    continue
                report.spotify_throttled = True
                break
            try:
                tracks = self.spotify.playlist_tracks(playlist_id, max_tracks=remaining)
                try:
                    self.source_cache.put(playlist_id, tracks, snapshot)
                except Exception as exc:  # cache is best-effort
                    log.debug("Could not write source cache: %s", exc)
            except RateLimited as exc:
                if getattr(self.spotify, "blocked_until", 0.0) > time.time():
                    log.warning(
                        "Spotify parked this run (%s) — reading the rest without the Web API",
                        str(exc).splitlines()[0][:120],
                    )
                report.spotify_throttled = True
                # 1) web player endpoint first (the route that actually works),
                #    then the public page — and only complain if both fail.
                web_tracks = self._read_pathfinder(playlist_id, report)
                if not web_tracks:
                    web_tracks = self._read_public_playlist(playlist_id, report, quiet=True)
                if web_tracks:
                    self._merge_tracks(seen, web_tracks, playlist_id, read_limit)
                    report.sources_read += 1
                    continue
                # 2) last good cache
                if cached is not None:
                    self.telegram.warn(
                        f"♻️ Spotify rate limit for <code>{playlist_id}</code> — using the "
                        f"cached read ({len(cached['tracks'])} tracks)"
                    )
                    report.cached_sources += 1
                    self._merge_tracks(seen, cached["tracks"], playlist_id, read_limit)
                    report.sources_read += 1
                    continue
                # 3) collection saved from an earlier run (copy/paste workflow)
                collection = self._read_collection(cfg.collection_path, playlist_id)
                if collection:
                    self._merge_tracks(seen, collection, playlist_id, read_limit)
                    report.sources_read += 1
                    continue
                raise
            except Exception as exc:
                report.errors.append(f"Source {playlist_id}: {exc}")
                self.telegram.warn(f"Could not read source <code>{playlist_id}</code>: {escape(str(exc))}")
                continue
            report.sources_read += 1
            if getattr(self.spotify, "partial_read", False):
                self.spotify.partial_read = False
                report.partial_sources += 1
                report.spotify_throttled = True
            for track in tracks:
                if read_limit and len(seen) >= read_limit:
                    break
                if track.id in seen:
                    seen[track.id].sources.append(playlist_id)
                else:
                    seen[track.id] = track
            log.info("Source %s: %d tracks", playlist_id, len(tracks))
        report.unique_tracks = len(seen)
        backend = getattr(self.spotify, "backend", "")
        if backend == "web-player" and not report.backend:
            report.backend = "web player (api-partner)"
        elif backend and not report.backend:
            report.backend = "REST (api.spotify.com)"
        return list(seen.values())

    def _clear_stale_cooldown(self, report: RunReport) -> None:
        """Drop a stored cooldown that no longer has to be respected.

        A run that was NOT throttled simply deletes the old hint, so the age of a
        block can never pile up into "runs keep skipping themselves".
        """
        stored = self.state.spotify_cooldown_until()
        if not stored:
            return
        if report.spotify_throttled and getattr(self.spotify, "blocked_until", 0.0) > time.time():
            return  # a real, current block: keep the hint until it expires
        self.state.clear_spotify_cooldown()

    def _send_single(self, report: RunReport) -> None:
        """Send the one message even when the run died early."""
        from .telegram import dedupe_lines

        report.added_tracks = dedupe_lines(report.added_tracks)
        report.failed_tracks = dedupe_lines(report.failed_tracks)
        self._clear_stale_cooldown(report)
        started = _parse_iso(report.started_at)
        if started and not report.elapsed_seconds:
            report.elapsed_seconds = max(
                0.0, (datetime.now(timezone.utc) - started).total_seconds()
            )
        report.cooldown_until = getattr(self.spotify, "blocked_until", 0.0) or 0.0
        self._name_target_playlists(report)
        verbose = os.environ.get("PHONK_VERBOSE_SUMMARY", "").strip().lower() in {
            "1", "true", "yes",
        }
        self.telegram.flush(
            summary=report.verbose_summary_text() if verbose else report.summary_text()
        )

    def _name_target_playlists(self, report: RunReport) -> None:
        """Best-effort human names for the two target playlists.

        A name is a nicety, never a reason to fail or slow a run: every lookup
        is guarded, a throttled Spotify is not re-probed, and the last name we
        learned is remembered in the state file so a 429 run still has it.
        """
        cfg = self.config
        report.spotify_target_id = report.spotify_target_id or cfg.spotify_target_playlist
        if cfg.youtube_enabled:
            report.youtube_target_id = report.youtube_target_id or cfg.youtube_target_playlist
        names = self.state.data.setdefault("playlist_names", {})
        learned = False
        for key, pid in (
            ("spotify:" + report.spotify_target_id, report.spotify_target_id),
            ("youtube:" + report.youtube_target_id, report.youtube_target_id),
        ):
            if not pid:
                continue
            known = str(names.get(key) or "")
            if not known and not report.setup_failed:
                known = self._lookup_playlist_name(key.startswith("youtube:"), pid)
                if known:
                    names[key] = known
                    learned = True
            if key.startswith("youtube:"):
                report.youtube_target_name = known
            else:
                report.spotify_target_name = known
        if learned and not cfg.dry_run:
            # The title lookup may have spent one YouTube unit: keep the
            # counters and the new name in the same saved state.
            if self.youtube is not None:
                self.state.save_quota(self.youtube.quota)
            try:
                self.state.save()
            except OSError as exc:
                log.debug("Could not save the playlist names: %s", exc)

    def _lookup_playlist_name(self, is_youtube: bool, playlist_id: str) -> str:
        if is_youtube:
            if self.youtube is None:
                return ""
            try:
                return str(getattr(self.youtube, "playlist_title", lambda _pid: "")(playlist_id) or "")
            except Exception as exc:
                log.debug("Could not read the YouTube playlist name: %s", exc)
                return ""
        # Spotify: never spend retries re-learning a 429 we already know about.
        if getattr(self.spotify, "blocked_until", 0.0) > time.time():
            return ""
        try:
            return str(self.spotify.playlist_info(playlist_id).get("name") or "")
        except Exception as exc:
            log.debug("Could not read the Spotify playlist name: %s", exc)
            return ""

    def _web_api_budget_left(self) -> bool:
        """Cheap-read budget: Web API calls are slow (2s pace) and rate limited."""
        if getattr(self, "_cooldown_hint_left", 0.0) > 0:
            return False  # a remembered 429: use the web-player route instead
        if getattr(self.spotify, "rate_limited_this_run", False):
            return False
        cap = getattr(self.config, "spotify_max_requests_per_run", 0)
        if not cap:
            return True
        used = int(getattr(self.spotify, "requests_made", 0) or 0)
        if used >= cap:
            return False
        return True

    def _web_api_note(self) -> str:
        return (
            f"reached the per-run Web API budget "
            f"({self.config.spotify_max_requests_per_run} calls, "
            f"{self.config.spotify_request_delay_seconds}s apart)"
        )

    def _startup_budget_left(self) -> bool:
        budget = getattr(self.config, "startup_time_budget_seconds", 0)
        if not budget:
            return True
        started = getattr(self, "_prework_started", 0.0)
        return (time.time() - started) < budget

    def _write_capability_line(self) -> str:
        """One line that says whether this run *could* write, and to what."""
        cookie = getattr(self.secrets, "spotify_cookie", "") or ""
        parts = [
            f"cookie {'set' if cookie else 'MISSING'} ({len(cookie)} chars)",
            f"target {self.config.spotify_target_playlist or 'MISSING'}",
            f"route {'web-player' if getattr(self.spotify, 'prefer_pathfinder', False) else 'rest'}",
            f"proxy {'on' if (getattr(self.spotify, 'proxy_url', '') or '') else 'off'}",
        ]
        if self.config.dry_run:
            parts.append("DRY RUN — nothing will be written")
        return "🔑 " + "; ".join(parts)

    def _detail(self, report: RunReport, text: str) -> None:
        """A line for the end-of-run message (never a message of its own)."""
        if text and text not in report.daily_details:
            report.daily_details.append(text)

    def _read_pathfinder(self, playlist_id: str, report: RunReport) -> list[Track]:
        """Fallback #1b: the web player's own GraphQL endpoint.

        ``api.spotify.com`` answers 429 to web-player tokens, so the *web player*
        itself never uses it for playlist contents: it posts to api-partner.
        Two ways in, tried in order:

        1. the account bearer we already have (same route the web player uses,
           works for private playlists too) — this is the "web player backend"
        2. an anonymous *client token* (no login needed), for public playlists
        """
        tracks: list[Track] = []
        reader = getattr(self.spotify, "_pathfinder_tracks", None)
        if reader is not None:
            try:
                tracks = reader(playlist_id)
                if tracks:
                    self.spotify.pathfinder_ok = True
                    self.spotify.backend = "web-player"
            except Exception as exc:
                log.warning("Web player read failed for %s: %s", playlist_id, exc)
        if not tracks:
            try:
                from .spotify import client_token, pathfinder_playlist

                tracks = pathfinder_playlist(playlist_id, client_token=client_token())
                if tracks:
                    log.info("Read %s with an anonymous client token", playlist_id)
            except Exception as exc:
                log.warning("Pathfinder read failed for %s: %s", playlist_id, exc)
                return []
        if not tracks:
            return []
        report.web_reads += 1
        try:
            self.source_cache.put(playlist_id, tracks, "")
        except Exception as exc:  # cache is best-effort
            log.debug("Could not cache the pathfinder read: %s", exc)
        return tracks

    def _read_public_playlist(
        self, playlist_id: str, report: RunReport, quiet: bool = False
    ) -> list[Track]:
        """Fallback #2: public HTML page (works while the API is throttled)."""
        if self.public_spotify is None:
            return []
        try:
            tracks = self.public_spotify.playlist_tracks(playlist_id)
        except Exception as exc:
            log.warning("Public playlist read failed: %s", exc)
            return []
        if not tracks:
            if not quiet:
                self.telegram.warn(
                    f"🌐 Could not read <code>{playlist_id}</code> from the public page "
                    f"({self.public_spotify.last_error or 'unknown'}) — is it public?"
                )
            return []
        report.web_reads += 1
        try:
            self.source_cache.put(playlist_id, tracks, "")
        except Exception:
            pass
        return tracks

    def _read_collection(self, path: str, playlist_id: str) -> list[Track]:
        """Fallback #2: tracks captured from a copy/paste (see `phonksync collect`)."""
        if not path:
            return []
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entry = (data.get("playlists") or {}).get(playlist_id) or {}
        tracks = [Track(**t) for t in entry.get("tracks") or []]
        if tracks:
            self.telegram.warn(
                f"📥 Using the saved collection for <code>{playlist_id}</code> "
                f"({len(tracks)} tracks)"
            )
        return tracks

    def _merge_tracks(
        self,
        seen: dict[str, Track],
        tracks: list[Track],
        playlist_id: str,
        read_limit: int | None,
    ) -> None:
        for track in tracks:
            if read_limit and len(seen) >= read_limit:
                break
            if track.id in seen:
                seen[track.id].sources.append(playlist_id)
            else:
                seen[track.id] = Track(
                    id=track.id, name=track.name, artists=list(track.artists),
                    album=track.album, duration_ms=track.duration_ms, isrc=track.isrc,
                    uri=track.uri, popularity=track.popularity, sources=[playlist_id],
                )

    # ------------------------------------------------------- spotify writes

    def _add_spotify(self, track: Track, target_ids: set[str], report: RunReport) -> bool:
        """Add one track to the Spotify target, cheaply.

        In optional ``queue`` mode the track is parked in the player queue and
        the queue is flushed into the playlist in batches: Spotify then
        de-duplicates server side, so a 200-track run costs ~5 calls instead of
        200. Any queue failure falls back to a normal add, so nothing is lost.

        Returns True only when Spotify really took the track. A throttled run
        parks it instead (manual bucket + pending queue) — and the caller must
        then NOT record it as ``spotify_added``, or the DB reports a success
        that never happened and every later run skips the track forever.
        """
        cfg = self.config
        if getattr(self.spotify, "blocked_until", 0.0) > time.time():
            # The Web API is cooling down. That does NOT throttle the web
            # player's own endpoint — the route the write probe proves — so try
            # it before parking anything. Parking here unconditionally is what
            # left tracks in the manual bucket while writes actually worked.
            writer = getattr(self.spotify, "add_tracks_web_player", None)
            if writer is None or not getattr(self.spotify, "web_player_write_ready", False):
                return self._park_spotify(track, report)
            try:
                writer(cfg.spotify_target_playlist, [track.uri])
            except Exception as exc:
                log.warning(
                    "Web player write refused during the Web API cooldown (%s) — "
                    "parking this track", exc,
                )
                return self._park_spotify(track, report)
            target_ids.add(track.id)
            self.state.clear_manual([track.id])
            self.state.clear_pending_spotify([track.id])
            report.web_player_writes += 1
            log.info(
                "Added %s through the web player (Web API cooling down)", track.display()
            )
            return True
        if cfg.spotify_write_mode == "queue" and getattr(
            self.spotify, "queue_supported", False
        ):
            try:
                if self.spotify.queue_track(track.uri):
                    target_ids.add(track.id)
                    report.queued += 1
                    if self.spotify.queued >= cfg.spotify_queue_flush_size:
                        sent = self.spotify.save_queue_to_playlist(
                            cfg.spotify_target_playlist
                        )
                        report.queued_flushes += 1
                        log.info("Saved %d queued tracks into the target playlist", sent)
                    return True
            except Exception as exc:
                log.warning(
                    "Queue add failed (%s) — switching this run to direct adds", exc
                )
                self.spotify.queue_supported = False
                self._flush_queue()
        self.spotify.add_tracks(cfg.spotify_target_playlist, [track.uri])
        target_ids.add(track.id)
        # It is added now, so any parked copy is stale — never add it twice.
        self.state.clear_manual([track.id])
        self.state.clear_pending_spotify([track.id])
        return True

    def _park_spotify(self, track: Track, report: RunReport) -> bool:
        """Both write routes are closed: keep the track for a later run.

        Nothing is lost and nothing is claimed: the track is NOT recorded as
        ``spotify_added``, so a later run (or `phonksync manual --apply`) adds it.
        """
        self.state.add_manual(track, reason="Spotify asked to slow down")
        self.state.add_pending_spotify(track)
        report.manual_queued += 1
        return False

    def _repair_parked_bucket(self) -> int:
        """Fix the state file a throttled run used to write.

        Until this version, a parked track was *also* recorded as
        ``spotify_added`` — so the backlog existed only in the manual bucket and
        no run would ever retry it. A track that is both parked and "added" can
        only come from that bug, so it is re-queued once, here.
        """
        repaired = 0
        for track_id, entry in self.state.manual_items().items():
            state = self.state.data["synced"].get(track_id) or {}
            if not state.get("spotify_added"):
                continue
            if not _same_instant(entry.get("queued_at"), state.get("added_at")):
                # Parked and really added at different times: a later run did
                # add it. Only the one-instant case is the old bug.
                continue
            state["spotify_added"] = False
            track = Track(
                id=track_id,
                name=entry.get("name", ""),
                artists=list(entry.get("artists") or []),
                uri=entry.get("uri", ""),
            )
            self.state.add_pending_spotify(track)
            repaired += 1
        if repaired:
            log.warning(
                "Re-queued %d track(s) that an older run parked but recorded as "
                "added — they will be added now", repaired
            )
        return repaired

    def _reconcile_parked(self, present_ids: set[str]) -> int:
        """Drop parked tracks that are already in the target playlist.

        The playlist is the truth, not our state file. A track added by hand (or
        by a *write probe*) is in the playlist but still parked here; adding it
        again would duplicate it, and the summary would claim a write that was
        not needed. Never guesses: only ids actually read from the playlist.
        """
        if not present_ids:
            return 0
        pending = self.state.pending_spotify()
        if not pending:
            return 0
        there = [track_id for track_id in pending if track_id in present_ids]
        if not there:
            return 0
        for track_id in there:
            entry = self.state.data["synced"].get(track_id)
            if entry is not None:
                entry["spotify_added"] = True
            else:
                info = pending[track_id]
                self.state.mark_synced(
                    Track(
                        id=track_id,
                        name=info.get("name", ""),
                        artists=list(info.get("artists") or []),
                        uri=info.get("uri", ""),
                    ),
                    "", "", True, False,
                )
        self.state.clear_pending_spotify(there)
        self.state.clear_manual(there)
        log.info(
            "%d parked track(s) were already in the playlist — counted, not re-added",
            len(there),
        )
        return len(there)

    def _spotify_diagnosis(self) -> str:
        """One line that says *why* a Spotify write was refused.

        The same symptom (nothing in the playlist) has three very different
        causes, and the summary has to name the one that happened.
        """
        cfg = self.config
        bits: list[str] = []
        reason = str(getattr(self, "_spotify_reason", "") or "").strip()
        first = reason.splitlines()[0][:160] if reason else ""
        if first:
            bits.append(f"reason: {escape(first)}")
        writes = int(getattr(self.spotify, "writes_this_run", 0) or 0)
        requests = int(getattr(self.spotify, "requests_made", 0) or 0)
        bits.append(f"{writes} Spotify write(s), {requests} request(s) before it")
        # A run that never got a token cannot write, whatever else is configured:
        # name the exact answer instead of "Spotify asked to slow down".
        token_errors = getattr(self.spotify, "last_token_errors", []) or []
        if token_errors and not getattr(self.spotify, "_access_token", ""):
            bits.append(
                "no token: " + escape("; ".join(token_errors[:3])[:300])
            )
            bits.append(
                f"route: {getattr(self.spotify, 'last_token_route', '') or 'direct'}, "
                f"proxy {'set' if getattr(self.spotify, 'proxy_url', '') else 'off'}, "
                f"cookie #{getattr(self.spotify, '_cookie_index', 0) + 1} of "
                f"{len(getattr(self.spotify, '_cookie_pool', []) or [''])} "
                f"({len(getattr(self.spotify, 'sp_dc', '') or '')} chars)"
            )
        if cfg.spotify_write_mode == "queue" and not getattr(
            self.spotify, "queue_supported", True
        ):
            bits.append(
                "the player queue was refused (Premium-only call) — set "
                "SPOTIFY_WRITE_MODE=direct to skip it"
            )
        delay = getattr(cfg, "spotify_request_delay_seconds", 0) or 0
        if delay:
            cap = getattr(cfg, "spotify_max_requests_per_run", 0) or 0
            bits.append(
                f"pacing {delay:g}s/call"
                + (f", {cap} calls per run" if cap else "")
            )
        return "ℹ️ " + "; ".join(bits)

    def _audit_claimed_spotify(self, report: RunReport) -> None:
        """One read at the end: are the tracks we claimed really in the playlist?

        Cheap (one paged read of our own target) and it turns "the run said it
        added them" into a fact. Anything claimed but absent is un-claimed, so the
        next run retries it instead of skipping it forever.
        """
        claimed = [
            track_id
            for track_id, entry in (self.state.data["synced"] or {}).items()
            if entry.get("spotify_added") and entry.get("added_at", "") >= (
                (report.started_at or "")[:19]
            )
        ]
        if not claimed:
            return
        verify = getattr(self.spotify, "verify_added", None)
        if verify is None:
            return
        uris = [
            (self.state.data["synced"][t].get("spotify_id") and
             f"spotify:track:{self.state.data['synced'][t]['spotify_id']}")
            or f"spotify:track:{t}"
            for t in claimed
        ]
        try:
            landed = verify(self.config.spotify_target_playlist, uris)
        except Exception as exc:
            log.warning("Could not audit this run's Spotify writes: %s", exc)
            return
        if not landed:
            return  # the playlist read itself failed; do not un-claim on a guess
        missing = [u for u in uris if u not in landed]
        if not missing:
            log.info("Verified: all %d claimed Spotify write(s) are in the playlist", len(uris))
            return
        for track_id, uri in zip(claimed, uris):
            if uri in missing:
                self.state.data["synced"][track_id]["spotify_added"] = False
                entry = self.state.data["synced"][track_id]
                self.state.add_pending_spotify(
                    Track(
                        id=track_id,
                        name=entry.get("name", ""),
                        artists=list(entry.get("artists") or []),
                        uri=uri,
                    )
                )
        report.spotify_writes_missing = len(missing)
        log.warning(
            "%d claimed Spotify write(s) are not in the playlist — un-claimed and "
            "queued for the next run", len(missing),
        )
        # Record the verdict *and* the evidence, so the reason is readable from
        # data/sync-state.json without the (unreliable) job log.
        hint = ""
        try:
            hint = self.spotify.permission_hint(self.config.spotify_target_playlist)
            total = len(self.spotify._pathfinder_tracks(self.config.spotify_target_playlist))
        except Exception:
            total = -1
        message = (
            f"Spotify accepted the write but {len(missing)} of {len(uris)} track(s) "
            f"are not in the playlist (playlist now holds {total} track(s))"
        )
        if total == 0:
            report.spotify_playlist_empty = True
            message += (
                " — the target playlist reads as EMPTY: check SPOTIFY_TARGET_PLAYLIST, "
                "or the playlist read (fetchPlaylist) is not answering"
            )
        if hint:
            message += f" | {hint}"
        self.state.note_write_problem(
            message,
            route=getattr(self.spotify, "last_write_route", ""),
            account=getattr(self.spotify, "account_username", ""),
        )

    def _confirm_spotify_writes(self, uris: list[str], report: RunReport) -> list[str] | None:
        """Which of these URIs are really in the target playlist now?

        ``None`` means "could not check" (no Spotify cookie, an unexpected
        client): callers then trust the API answer rather than blocking.
        """
        verify = getattr(self.spotify, "verify_added", None)
        if verify is None:
            return None
        try:
            landed = verify(self.config.spotify_target_playlist, uris)
        except Exception as exc:
            log.warning("Could not verify the playlist write: %s", exc)
            return None
        return list(landed)

    def _retry_pending_spotify(self, report: RunReport) -> None:
        """Add the tracks an earlier throttled run could only park.

        These were never added (that is why they are not ``spotify_added``), so
        this is a plain add, in one batch, at the end of the run — not a search.
        """
        pending = self.state.pending_spotify()
        if not pending:
            return
        cooling = getattr(self.spotify, "blocked_until", 0.0) > time.time()
        web_writer = getattr(self.spotify, "add_tracks_web_player", None)
        web_ready = bool(getattr(self.spotify, "web_player_write_ready", False))
        if cooling and (web_writer is None or not web_ready):
            # Both routes are closed: trying now would be one more refused write.
            report.parked_spotify = len(pending)
            log.info("Keeping %d parked track(s) for the next run", len(pending))
            return
        # A later run may already have added some of them (the resume point made
        # it start past the cooldown): never add those twice.
        done = [
            track_id
            for track_id in pending
            if (self.state.data["synced"].get(track_id) or {}).get("spotify_added")
        ]
        if done:
            self.state.clear_pending_spotify(done)
            self.state.clear_manual(done)
            pending = self.state.pending_spotify()
        if not pending:
            return
        uris = [entry.get("uri", "") for entry in pending.values() if entry.get("uri")]
        if not uris:
            self.state.clear_pending_spotify()
            return
        try:
            if cooling:
                # The Web API is still throttled, but the backlog goes through
                # the web player — the same route that just worked above.
                log.info(
                    "Adding %d parked track(s) through the web player "
                    "(Web API cooling down)", len(uris),
                )
                web_writer(self.config.spotify_target_playlist, uris)
                report.web_player_writes += len(uris)
            else:
                self.spotify.add_tracks(self.config.spotify_target_playlist, uris)
        except Exception as exc:
            problem = getattr(self.spotify, "last_write_error", "") or str(exc)
            log.warning(
                "Still could not add %d parked Spotify track(s): %s", len(uris), problem
            )
            self.state.note_write_problem(
                problem,
                route=getattr(self.spotify, "last_write_route", ""),
                account=getattr(self.spotify, "account_username", ""),
            )
            report.parked_spotify = len(uris)
            hint = ""
            try:
                hint = self.spotify.permission_hint(self.config.spotify_target_playlist)
            except Exception:  # best effort: never hide the real error
                hint = ""
            report.spotify_note = "ℹ️ write refused: " + escape(problem[:300])
            if hint:
                report.spotify_note += "\nℹ️ " + escape(hint)
                self.state.note_write_problem(f"{problem} | {hint}",
                                              route=getattr(self.spotify, "last_write_route", ""),
                                              account=getattr(self.spotify, "account_username", ""))
            return
        landed = self._confirm_spotify_writes(uris, report)
        if landed is None:  # could not check: trust the API answer
            landed = list(uris)
        self.state.note_write_receipt(
            len(uris), len(landed),
            getattr(self.spotify, "last_write_route", "")
            + " | vars=" + ",".join(getattr(self.spotify, "last_write_variables", []) or []),
            note="parked backlog" if len(landed) == len(uris) else "parked backlog, partial",
        )
        self.spotify.last_add_accepted = bool(landed)
        missing = [u for u in uris if u not in landed]
        if missing:
            report.spotify_writes_missing += len(missing)
            keep = {tid for tid, e in pending.items() if e.get("uri") in missing}
            log.warning(
                "%d of %d parked track(s) did NOT land in the playlist — kept for a "
                "retry", len(missing), len(uris),
            )
            report.parked_spotify = len(missing)
            self.state.note_write_problem(
                f"Spotify accepted the batch but {len(missing)} of {len(uris)} parked "
                f"track(s) are not in the playlist",
                route=getattr(self.spotify, "last_write_route", ""),
            )
            uris = landed
            pending = {k: v for k, v in pending.items() if k not in keep}
        for track_id, entry in pending.items():
            track = Track(
                id=track_id,
                name=entry.get("name", ""),
                artists=list(entry.get("artists") or []),
                uri=entry.get("uri", ""),
            )
            entry_state = self.state.data["synced"].get(track_id) or {}
            if entry_state:
                entry_state["spotify_added"] = True
            else:
                self.state.mark_synced(track, "", "", True, False)
        done_ids = [k for k in pending]
        self.state.clear_pending_spotify(done_ids)
        self.state.clear_manual(done_ids)
        # `parked_spotify` keeps whatever must stay in the backlog.
        report.parked_saved = len(uris)
        log.info(
            "Added %d track(s) that an earlier run had to park (Spotify cooldown)",
            len(uris),
        )

    def _retry_pending_queue(self) -> None:
        """A previous run could not save its queue — try again now."""
        pending = self.state.get_pending_queue()
        if not pending:
            return
        try:
            self.spotify.add_tracks(self.config.spotify_target_playlist, pending)
            self.state.clear_pending_queue()
            log.info("Saved %d tracks left over from the previous run", len(pending))
        except Exception as exc:
            log.warning("Still could not save %d pending tracks: %s", len(pending), exc)

    def _flush_queue(self) -> int:
        """Save anything left in the queue (also used at the end of a run)."""
        cfg = self.config
        if not getattr(self.spotify, "queued", 0):
            return 0
        try:
            return self.spotify.save_queue_to_playlist(cfg.spotify_target_playlist)
        except Exception as exc:
            log.error("Could not save the queued tracks: %s", exc)
            self.state.set_pending_queue(getattr(self.spotify, "_queued", []))
            return 0

    def _park_remaining(self, remaining: list[Track], report: RunReport) -> None:
        """Park the not-yet-processed source tracks so the next run retries them."""
        for track in remaining:
            key = self.state.data["synced"].get(track.id) or {}
            if not key.get("spotify_added"):
                self.state.add_manual(track, reason="Spotify asked to slow down")
                self.state.add_pending_spotify(track)
                report.manual_queued += 1

    def _hold_resume_point(self, track: Track) -> bool:
        """Long cooldown mid-run: remember this track so the next run starts here.

        Returns True when the run should stop (a long block was detected).
        """
        blocked_until = getattr(self.spotify, "blocked_until", 0.0)
        if not blocked_until:
            return False
        if getattr(self.spotify, "web_player_write_ready", False) and hasattr(
            self.spotify, "add_tracks_web_player"
        ):
            # Only the Web API is blocked. The web player still writes, so the
            # rest of the playlist must not be parked: keep going and let each
            # track land on the route that works.
            log.info(
                "Spotify Web API is cooling down, but the web player still writes — "
                "continuing instead of parking the rest of the run"
            )
            return False
        # Keep the resume point (so the next run starts at this track) but never
        # store a cooldown: the next run must be free to try again immediately.
        self.state.set_resume_point(track.id, track.name, track.artists, blocked_until)
        return True

    def _remember_best_unmatched(self, track: Track, report: RunReport) -> None:
        """Keep the closest candidate for a track that failed the checks.

        Nothing is written to any playlist now (the checks said no). But when a
        write credential appears later (a refresh token after weeks of
        read-only runs), the useful candidates are the ones already found — no
        need to re-search 54 queries to rediscover them. They go to a separate
        bucket, added only on an explicit opt-in (`phonksync pending --add`).
        """
        pool = getattr(self.matcher, "last_pool", None) or []
        if not pool:
            return
        best = None
        best_score = 0.0
        want = track.name.lower()
        for cand in pool:
            title = (cand.title or "").lower()
            if not title:
                continue
            score = 0.0
            for token in {t for t in want.split() if len(t) > 3}:
                if token in title:
                    score += 1.0
            if score > best_score:
                best, best_score = cand, score
        if best is None:
            return
        self.state.add_candidate(
            track,
            video_id=best.video_id,
            title=best.title,
            channel=best.channel,
            duration_s=best.duration_s,
            methods=len(self.matcher.last_tried),
        )
        report.candidate_pool += 1

    def _flush_pending_youtube(self, report: RunReport) -> None:
        """Add matches found while writes were off — no new searches needed."""
        yt = self.youtube
        if yt is None or not getattr(yt, "can_write", False):
            return
        pending = self.state.pending_youtube()
        if not pending:
            return
        added = 0
        for entry in pending[:50]:  # small batches: inserts cost 50 units each
            video_id = entry.get("youtube_video_id", "")
            if not video_id:
                continue
            try:
                if hasattr(yt, "add_to_playlist"):
                    yt.add_to_playlist(self.config.youtube_target_playlist, video_id)
                entry["youtube_added"] = True
                added += 1
            except QuotaExceeded as exc:
                report.quota_reason = str(exc)
                break
            except AuthError as exc:  # credential refused: stop once, not 50x
                report.youtube_auth_failed = True
                report.youtube_write_problem = str(exc)
                self._detail(
                    report,
                    "✍️ YouTube writes stopped at the first refusal — nothing else "
                    f"was attempted ({escape(str(exc))})",
                )
                break
            except Exception as exc:  # keep the rest for the next run
                log.warning("Could not add pending %s: %s", video_id, exc)
                break
        if added:
            report.pending_youtube_flushed = added
            self.state.save()  # flush immediately: a crash must not double-add
            log.info("Added %d previously matched video(s) to the YouTube playlist", added)

    def _search_budget_exhausted(self) -> bool:
        """True when the run (or the day) ran out of YouTube search budget."""
        yt = self.youtube
        if yt is None:
            return False
        if getattr(yt, "searches_this_run", 0) >= getattr(yt, "max_searches_per_run", 0) > 0:
            return True
        daily = getattr(yt, "max_searches_per_day", 0)
        quota = getattr(yt, "quota", None)
        return bool(daily and quota is not None and quota.searches >= daily)

    def _deferred(self, track: Track) -> bool:
        tries = self.state.unmatched_tries(track.id)
        if tries == 0:
            return False
        last = _parse_iso(self.state.unmatched_last_try(track.id))
        if not last:
            return False
        age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        return age_days < self.config.retry_unmatched_after_days

    def _process_track(
        self,
        track: Track,
        entry: dict[str, Any],
        spotify_done: bool,
        youtube_done: bool,
        target_spotify_ids: set[str],
        target_youtube_ids: set[str],
        synced_isrcs: set[str],
        report: RunReport,
    ) -> None:
        cfg = self.config
        if not cfg.youtube_enabled:
            self._process_track_spotify_only(
                track, spotify_done, target_spotify_ids, synced_isrcs, report
            )
            return

        video_id = entry.get("youtube_video_id", "")
        method = entry.get("method", "")

        # A previously validated match is durable even if a YouTube write
        # failed or was disabled. Retry that video without spending searches.
        if not youtube_done and not video_id:
            started = time.time()
            result = self.matcher.match(track)
            elapsed = time.time() - started
            report.track_times.append((track.display(), elapsed, len(self.matcher.last_tried)))
            # Record tried/won stats for all exercised methods.
            won = self.matcher.last_won
            for code in self.matcher.last_tried:
                self.state.record_method(code, tried=True, won=(code == won))
            if elapsed >= 20:
                log.warning(
                    "slow track %.1fs: %s (%d methods)%s",
                    elapsed, track.display(), len(self.matcher.last_tried),
                    "" if result else " — no match",
                )
            if result is None:
                # "unmatched" and "we ran out of search budget" are different:
                # the first means the song is probably not on YouTube, the second
                # means it was never really tried. Do not mark it unmatched —
                # otherwise the retry policy defers it for days.
                budget_hit = self._search_budget_exhausted()
                self.state.mark_unmatched(
                    track, self.matcher.last_tried, budget_exhausted=budget_hit
                )
                if budget_hit:
                    report.budget_skips += 1
                else:
                    report.unmatched += 1
                if budget_hit:
                    pass  # already summarised as "budget ran out"; not a failure
                else:
                    report.failed_tracks.append(
                        f"{escape(track.display())} — no YouTube match "
                        f"({len(self.matcher.last_tried)} method(s) tried)"
                    )
                self._remember_best_unmatched(track, report)
                if cfg.require_youtube_match_for_spotify:
                    return
                # Spotify side is exact (same track ID) — still add it.
                if not cfg.dry_run and not spotify_done:
                    spotify_added = self._add_spotify(track, target_spotify_ids, report)
                else:
                    spotify_added = spotify_done
                if not cfg.dry_run:
                    self.state.mark_synced(track, "", "", spotify_added, False)
                    if spotify_added and track.isrc:
                        synced_isrcs.add(track.isrc.upper())
                    self._claim_family(track, spotify_added, False)
                return
            if getattr(result, "free", False):
                self._free_matches += 1
            video_id = result.candidate.video_id
            method = result.method
            report.method_wins[method] = report.method_wins.get(method, 0) + 1

        if video_id and video_id in target_youtube_ids:
            youtube_done = True

        if cfg.dry_run:
            report.added += 1
            line = (
                f"{escape(track.display())} <code>[{escape(method or 'cached')}]</code>"
            )
            report.added_tracks.append(line)
            return

        # YouTube FIRST. Spotify has no daily budget, YouTube does: writing
        # Spotify first meant the track that exhausted the YouTube budget still
        # landed on Spotify, so the playlists drifted apart one song per run.
        # Now a YouTube QuotaExceeded propagates before Spotify is touched.
        youtube_writable = (
            getattr(self.youtube, "can_write", True) and not report.youtube_auth_failed
        )
        youtube_added = youtube_done
        if video_id and not youtube_done:
            if youtube_writable:
                self.youtube.add_to_playlist(cfg.youtube_target_playlist, video_id)
                target_youtube_ids.add(video_id)
                youtube_added = True
            else:
                if not report.quota_reason:
                    report.quota_reason = (
                        "YouTube playlist writes disabled (API key only) — set "
                        "YT_CLIENT_ID + YT_CLIENT_SECRET + YT_REFRESH_TOKEN to add videos"
                    )
                report.youtube_readonly += 1

        # Parity: if YouTube could not take this song, do not let Spotify run
        # ahead of it. YouTube writes being unavailable entirely is a setup
        # problem, not a budget stop, so Spotify still proceeds in that case.
        hold_spotify = (
            cfg.spotify_youtube_parity
            and bool(video_id)
            and youtube_writable
            and not youtube_added
        )
        if not spotify_done and not hold_spotify:
            spotify_added = self._add_spotify(track, target_spotify_ids, report)
        else:
            spotify_added = spotify_done

        # Only claim what actually happened: a parked track keeps
        # spotify_added=False so the next run retries it instead of skipping it
        # as "already synced" forever.
        self.state.mark_synced(track, video_id, method, spotify_added, youtube_added)
        if track.isrc:
            synced_isrcs.add(track.isrc.upper())
        self._claim_family(track, spotify_added, youtube_added)
        report.added += 1
        link = (
            f' · <a href="https://www.youtube.com/watch?v={video_id}">YT</a>'
            if video_id
            else ""
        )
        # One line, listed once — in the single end-of-run message. A song that
        # is already in the DB never reaches this point, so it cannot repeat.
        report.added_tracks.append(
            f"{escape(track.display())} <code>[{escape(method or 'cached')}]</code>{link}"
        )

    def _claim_family(self, track: Track, spotify_added: bool, youtube_added: bool) -> None:
        """Reserve this song so a later version of it is recognised as a duplicate.

        Only a track that actually reached a playlist claims the song: a parked
        or unmatched track must not block the version that does land.
        """
        if not self.config.dedupe_versions or not (spotify_added or youtube_added):
            return
        families = getattr(self, "_run_families", None)
        if families is None:
            return
        key = song_family(track.name, track.artists)
        if key:
            families.setdefault(key, {
                "spotify_id": track.id,
                "name": track.name,
                "artists": track.artists,
            })

    def _process_track_spotify_only(
        self,
        track: Track,
        spotify_done: bool,
        target_spotify_ids: set[str],
        synced_isrcs: set[str],
        report: RunReport,
    ) -> None:
        """Spotify-only mode: copy the exact track ID into the target playlist.

        No YouTube search, matching, or API calls are made.
        """
        cfg = self.config
        if spotify_done:
            return

        if cfg.dry_run:
            report.added += 1
            report.added_tracks.append(
                f"{escape(track.display())} <code>[SPOTIFY]</code>"
            )
            return

        added = self._add_spotify(track, target_spotify_ids, report)
        self.state.mark_synced(track, "", "", added, False)
        if track.isrc:
            synced_isrcs.add(track.isrc.upper())
        self._claim_family(track, added, False)
        report.added += 1
        report.added_tracks.append(f"{escape(track.display())} <code>[SPOTIFY]</code>")
