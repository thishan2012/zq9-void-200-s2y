"""Sync orchestrator.

Default mode is Spotify-only: read every source Spotify playlist and copy the
exact tracks into one target Spotify playlist. YouTube sync is optional and
disabled by default; when enabled, it also finds the exact same recording on
YouTube through the 31-method pipeline and adds it to a YouTube playlist.
The JSON state DB (committed back to the repo by CI) guarantees a track is
never added twice.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .cache import SearchCache, SourceCache
from .db import StateDB
from .errors import AuthError, QuotaExceeded, RateLimited
from .matcher import Matcher
from .models import Track
from .overrides import Overrides
from .settings import Config, ConfigError, Secrets
from .spotify import SpotifyClient
from .telegram import TelegramLogger, escape
from .youtube import YouTubeClient


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
    deferred: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    method_wins: dict[str, int] = field(default_factory=dict)
    quota_stopped: bool = False
    quota_reason: str = ""
    partial_sources: int = 0
    cached_sources: int = 0
    queued: int = 0
    queued_flushes: int = 0
    manual_queued: int = 0
    spotify_throttled: bool = False
    searches_used: int = 0
    inserted: int = 0
    search_mode: str = ""
    dry_run: bool = False
    source_read_limit: int = 0

    def summary_text(self) -> str:
        lines = [
            "📊 <b>Sync run finished</b>",
            f"Sources read: {self.sources_read}",
            f"Unique tracks: {self.unique_tracks}",
            f"Already synced: {self.already_synced}",
            f"✅ Added: {self.added}",
            f"❌ Unmatched: {self.unmatched}",
            f"⏭ Deferred (retry later): {self.deferred}",
            f"♻️ Duplicates skipped: {self.duplicates}",
        ]
        if self.method_wins:
            wins = ", ".join(f"{k}={v}" for k, v in sorted(self.method_wins.items()))
            lines.append(f"🏆 Methods: {wins}")
        if self.searches_used:
            lines.append(
                f"🔎 Searches this run: {self.searches_used} (mode: {self.search_mode or 'n/a'})"
            )
        if self.queued:
            lines.append(
                f"🧺 Spotify writes via queue: {self.queued} tracks "
                f"({self.queued_flushes} batch save(s))"
            )
        if self.manual_queued:
            lines.append(
                f"📋 Manual bucket: {self.manual_queued} tracks in "
                "data/manual-queue.txt (Spotify was cooling down)"
            )
        if self.cached_sources:
            lines.append(f"♻️ Cached playlist reads reused: {self.cached_sources}")
        if self.spotify_throttled:
            lines.append(
                "⚠️ Spotify rate limited (429): used the tracks read so far — "
                "wait a few minutes and run again to continue"
            )
        if self.quota_stopped:
            lines.append(f"⚠️ Stopped early: {escape(self.quota_reason or 'YouTube quota budget reached')}")
        elif self.inserted:
            lines.append("ℹ️ Next run continues from where this one stopped")
        if self.dry_run:
            if self.source_read_limit:
                lines.append(
                    f"🧪 DRY RUN — source reads limited to {self.source_read_limit} tracks; nothing was written"
                )
            else:
                lines.append("🧪 DRY RUN — nothing was written")
        if self.errors:
            lines.append(f"🛑 Errors: {len(self.errors)}")
            lines.extend(escape(e)[:200] for e in self.errors[:5])
        return "\n".join(lines)


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
        self.spotify = SpotifyClient(
            secrets.spotify_cookie, extra_cookies=secrets.spotify_spare_cookies
        )
        self.source_cache = SourceCache(
            config.source_cache_path, ttl_hours=config.source_cache_ttl_hours
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
                refresh_token=secrets.youtube_refresh_token,
                token_json=secrets.youtube_token_json,
                api_key=secrets.youtube_api_key,
                daily_quota_units=config.youtube_daily_quota_units,
                max_searches_per_run=config.max_searches_per_run,
                max_searches_per_day=config.youtube_max_searches_per_day,
                max_inserts_per_day=config.youtube_max_inserts_per_day,
                quota_state=self.state.quota,
                region_code=config.youtube_region_code,
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

    def run(self) -> RunReport:
        cfg = self.config
        report = RunReport(
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            dry_run=cfg.dry_run,
        )
        mode = "DRY RUN" if cfg.dry_run else "live"
        if cfg.dry_run and cfg.dry_run_track_limit:
            mode += f" (first {cfg.dry_run_track_limit} source tracks)"
        targets = "Spotify + YouTube" if cfg.youtube_enabled else "Spotify only"
        if cfg.youtube_enabled and self.youtube is not None and not getattr(
            self.youtube, "can_write", True
        ):
            targets += " (read-only API key: search works, playlist adds need OAuth)"
        self.telegram.info(
            f"🎧 <b>Phonk sync started</b>\n"
            f"Sources: {len(cfg.spotify_source_playlists)} playlists\n"
            f"Targets: {targets}\n"
            f"Mode: {mode}"
        )

        cooldown_left = self.state.spotify_cooldown_until() - time.time()
        if cooldown_left > 0 and not cfg.dry_run:
            report.spotify_throttled = True
            report.quota_reason = (
                f"Spotify cooldown — {_human_wait(cooldown_left)} remaining; "
                "this run was skipped on purpose (state is safe)"
            )
            self.telegram.warn(escape(report.quota_reason))
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return report

        try:
            tracks = self._collect_source_tracks(report)
            target_spotify_ids = self.spotify.playlist_track_ids(cfg.spotify_target_playlist)
            target_youtube_ids = (
                self.youtube.playlist_video_ids(cfg.youtube_target_playlist)
                if cfg.youtube_enabled
                else set()
            )
        except (AuthError, ConfigError) as exc:
            report.errors.append(str(exc))
            self.telegram.error(f"Setup failed: {escape(str(exc))}")
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return report
        except RateLimited as exc:
            report.errors.append(str(exc))
            report.spotify_throttled = True
            report.quota_reason = str(exc)
            self.telegram.error(f"Spotify rate limit: {escape(str(exc))}")
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return report

        synced_isrcs = {
            (v.get("isrc") or "").upper()
            for v in self.state.data["synced"].values()
            if v.get("isrc")
        }
        processed = 0

        for track in tracks:
            if cfg.max_tracks_per_run and processed >= cfg.max_tracks_per_run:
                break

            entry = self.state.get_synced(track.id) or {}
            spotify_done = bool(entry.get("spotify_added")) or track.id in target_spotify_ids
            youtube_done = bool(entry.get("youtube_added")) or not cfg.youtube_enabled

            if spotify_done and youtube_done:
                report.already_synced += 1
                continue

            # Same recording already synced under a different Spotify ID?
            if track.isrc and track.isrc.upper() in synced_isrcs and not entry:
                report.duplicates += 1
                continue

            # Retry policy for previously unmatched tracks.
            if not youtube_done and self._deferred(track):
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
            except Exception as exc:
                log.exception("Track %s failed", track.id)
                report.errors.append(f"{track.display()}: {exc}")

        blocked_until = getattr(self.spotify, "blocked_until", 0.0)
        if blocked_until:
            report.spotify_throttled = True
            self.state.set_spotify_cooldown(blocked_until, "Spotify 429 (long Retry-After)")
            report.quota_reason = (
                "Spotify cooldown until "
                + datetime.fromtimestamp(blocked_until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            )
            self.telegram.warn(escape(report.quota_reason))

        if not cfg.dry_run:
            self._flush_queue()
            self._retry_pending_queue()
            if report.manual_queued:
                self.state.write_manual_queue(cfg.manual_queue_path)

        # Record method statistics for every method that was exercised.
        self.state.record_run()
        if cfg.youtube_enabled:
            report.searches_used = int(getattr(self.youtube, "searches_this_run", 0) or 0)
            report.inserted = int(getattr(self.youtube.quota, "inserts", 0) or 0)
            report.search_mode = getattr(self.youtube, "search_mode", "")
            self.state.save_quota(self.youtube.quota)
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if not cfg.dry_run:
            self.state.save()
        if self.cache is not None:
            self.cache.save()
        self.source_cache.save()

        self.telegram.flush_tracks()
        self.telegram.send(report.summary_text())
        log.info("\n%s", report.summary_text().replace("<b>", "").replace("</b>", ""))
        return report

    # -------------------------------------------------------------- helpers

    def _collect_source_tracks(self, report: RunReport) -> list[Track]:
        cfg = self.config
        read_limit = cfg.dry_run_track_limit if cfg.dry_run else 0
        if read_limit and cfg.max_tracks_per_run:
            read_limit = min(read_limit, cfg.max_tracks_per_run)
        report.source_read_limit = read_limit

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
                    self.telegram.info(
                        f"♻️ Using cached read of <code>{playlist_id}</code> "
                        f"({len(tracks)} tracks, playlist unchanged)"
                    )
                    report.cached_sources += 1
                    self._merge_tracks(seen, tracks, playlist_id, read_limit)
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
            try:
                tracks = self.spotify.playlist_tracks(playlist_id, max_tracks=remaining)
                try:
                    self.source_cache.put(playlist_id, tracks, snapshot)
                except Exception as exc:  # cache is best-effort
                    log.debug("Could not write source cache: %s", exc)
            except RateLimited:
                if cached is not None:
                    self.telegram.warn(
                        f"♻️ Spotify rate limit for <code>{playlist_id}</code> — using the "
                        f"cached read ({len(cached['tracks'])} tracks)"
                    )
                    report.cached_sources += 1
                    report.spotify_throttled = True
                    self._merge_tracks(seen, cached["tracks"], playlist_id, read_limit)
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
        return list(seen.values())

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

    def _add_spotify(self, track: Track, target_ids: set[str], report: RunReport) -> None:
        """Add one track to the Spotify target, cheaply.

        In ``queue`` mode (default) the track is parked in the player queue and
        the queue is flushed into the playlist in batches: Spotify then
        de-duplicates server side, so a 200-track run costs ~5 calls instead of
        200. Any queue failure falls back to a normal add, so nothing is lost.
        """
        cfg = self.config
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
                    return
            except Exception as exc:
                log.warning(
                    "Queue add failed (%s) — switching this run to direct adds", exc
                )
                self.spotify.queue_supported = False
                self._flush_queue()
        self.spotify.add_tracks(cfg.spotify_target_playlist, [track.uri])
        target_ids.add(track.id)

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

        if not youtube_done:
            result = self.matcher.match(track)
            # Record tried/won stats for all exercised methods.
            won = self.matcher.last_won
            for code in self.matcher.last_tried:
                self.state.record_method(code, tried=True, won=(code == won))
            if result is None:
                self.state.mark_unmatched(track, self.matcher.last_tried)
                report.unmatched += 1
                if cfg.telegram_per_track:
                    self.telegram.track_unmatched(track.display(), len(self.matcher.last_tried))
                if cfg.require_youtube_match_for_spotify:
                    return
                # Spotify side is exact (same track ID) — still add it.
                if not spotify_done and not cfg.dry_run:
                    self._add_spotify(track, target_spotify_ids, report)
                if not cfg.dry_run:
                    self.state.mark_synced(track, "", "", True, False)
                return
            video_id = result.candidate.video_id
            method = result.method
            report.method_wins[method] = report.method_wins.get(method, 0) + 1

        if video_id and video_id in target_youtube_ids:
            youtube_done = True

        if cfg.dry_run:
            report.added += 1
            self.telegram.info(
                f"🧪 Would add: {escape(track.display())}\n"
                f"Method: <code>{method or 'cached'}</code> → <code>{video_id}</code>"
            )
            return

        if not spotify_done:
            self._add_spotify(track, target_spotify_ids, report)
        if video_id and not youtube_done:
            if getattr(self.youtube, "can_write", True):
                self.youtube.add_to_playlist(cfg.youtube_target_playlist, video_id)
                target_youtube_ids.add(video_id)
            elif not report.quota_reason:
                report.quota_reason = (
                    "YouTube playlist writes disabled (API key only) — set "
                    "YT_CLIENT_JSON + YT_REFRESH_TOKEN to add videos to the playlist"
                )

        self.state.mark_synced(
            track, video_id, method, True,
            bool(video_id) and getattr(self.youtube, "can_write", True),
        )
        if track.isrc:
            synced_isrcs.add(track.isrc.upper())
        report.added += 1
        if cfg.telegram_per_track:
            self.telegram.track_added(
                track.display(), method or "cached",
                f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
            )

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
            self.telegram.info(f"🧪 Would add to Spotify: {escape(track.display())}")
            return

        self._add_spotify(track, target_spotify_ids, report)
        self.state.mark_synced(track, "", "", True, False)
        if track.isrc:
            synced_isrcs.add(track.isrc.upper())
        report.added += 1
        if cfg.telegram_per_track:
            self.telegram.track_added(track.display(), "SPOTIFY", "")
