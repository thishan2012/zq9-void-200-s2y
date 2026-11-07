"""End-to-end engine tests with fake Spotify/YouTube/Telegram (no network)."""
import time

import pytest
from conftest import FakeYouTube, make_candidate, make_track

from phonksync import engine as engine_mod
from phonksync.errors import RateLimited
from phonksync.settings import Config


class FakeSpotify:
    def __init__(self, sources, target_ids=None):
        self.sources = sources  # playlist_id -> [Track]
        self.target_ids = set(target_ids or [])
        self.added = []
        self.read_calls = []

    def playlist_tracks(self, playlist_id, max_tracks=None):
        self.read_calls.append((playlist_id, max_tracks))
        if playlist_id == "target":
            from conftest import make_track as mt

            tracks = [mt(id=t) for t in sorted(self.target_ids)]
        else:
            tracks = self.sources.get(playlist_id, [])
        return tracks if max_tracks is None else tracks[:max_tracks]

    def playlist_track_ids(self, playlist_id):
        return set(self.target_ids)

    def add_tracks(self, playlist_id, uris):
        self.added.extend(uris)
        for uri in uris:
            self.target_ids.add(uri.split(":")[-1])


class EngineYouTube(FakeYouTube):
    def __init__(self, *a, playlist_ids=None, **kw):
        super().__init__(*a, **kw)
        self.playlist_ids = set(playlist_ids or [])
        self.inserted = []
        self.quota = type("Q", (), {"rollover": lambda self: None, "to_dict": lambda self: {}})()

    def playlist_video_ids(self, playlist_id):
        return set(self.playlist_ids)

    def add_to_playlist(self, playlist_id, video_id):
        self.inserted.append(video_id)
        self.playlist_ids.add(video_id)

    can_write = True


class SilentTelegram:
    """Stands in for TelegramLogger: collects, never sends (one message/run)."""

    def __init__(self, *a, **kw):
        self.messages = []
        self._track_buffer = []
        self.one_message = True
        self.flushed = 0
        self.last_summary = ""

    def flush(self, summary=""):
        self.flushed += 1
        self.last_summary = summary
        if summary:
            self.messages.append(summary)
        self.messages.extend(self._track_buffer)
        self._track_buffer = []
        return 1

    def _buffer_track(self, line):
        self._track_buffer.append(line)

    def info(self, t):
        self.messages.append(t)

    def warn(self, t):
        self.messages.append(t)

    def error(self, t):
        self.messages.append(t)

    def send(self, t, **kw):
        self.messages.append(t)

    def track_added(self, *a):
        pass

    def track_unmatched(self, *a):
        pass

    def flush_tracks(self):
        pass


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.spotify_source_playlists = ["src111111111111111111111"]
    c.spotify_target_playlist = "target"
    c.youtube_enabled = True
    c.youtube_target_playlist = "PLtarget0000000000000000000000"
    c.state_path = str(tmp_path / "state.json")
    c.overrides_path = str(tmp_path / "overrides.json")
    c.cache_path = str(tmp_path / "cache.json")
    c.source_cache_path = str(tmp_path / "source-cache.json")
    # every run() rewrites the manual bucket — never the repo's real file
    c.manual_queue_path = str(tmp_path / "manual-queue.txt")
    (tmp_path / "overrides.json").write_text('{"overrides": {}}')
    return c


def build_engine(monkeypatch, cfg, spotify, youtube):
    monkeypatch.setattr(engine_mod, "SpotifyClient", lambda *a, **kw: spotify)
    monkeypatch.setattr(engine_mod, "YouTubeClient", lambda *a, **kw: youtube)
    monkeypatch.setattr(engine_mod, "TelegramLogger", SilentTelegram)
    from phonksync.settings import Secrets

    return engine_mod.SyncEngine(cfg, Secrets())


def test_adds_to_both_playlists_and_records_state(monkeypatch, cfg):
    track = make_track(id="track000000000000000001")
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube(
        catalog={"vid00000001": good},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.added == 1
    assert spotify.added == [track.uri]
    assert youtube.inserted == ["vid00000001"]
    assert eng.state.is_synced(track.id)


def test_second_run_does_not_readd(monkeypatch, cfg):
    track = make_track(id="track000000000000000001")
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube(
        catalog={"vid00000001": good},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    eng.run()
    # Second run: fresh engine instance reading persisted state.
    eng2 = build_engine(monkeypatch, cfg, spotify, youtube)
    report2 = eng2.run()
    assert report2.added == 0
    assert report2.already_synced == 1
    assert len(spotify.added) == 1
    assert len(youtube.inserted) == 1


def test_dry_run_writes_nothing(monkeypatch, cfg):
    cfg.dry_run = True
    track = make_track(id="track000000000000000001")
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube(
        catalog={"vid00000001": good},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.added == 1
    assert spotify.added == []
    assert youtube.inserted == []
    assert not eng.state.is_synced(track.id)


def test_unmatched_track_retried_later(monkeypatch, cfg):
    track = make_track(id="track000000000000000002", name="No Match Song", artists=["Solo"])
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.unmatched == 1
    # Spotify side still added (exact track ID).
    assert spotify.added == [track.uri]
    assert eng.state.unmatched_tries(track.id) == 1
    # Immediately re-running defers the YouTube retry.
    eng2 = build_engine(monkeypatch, cfg, spotify, youtube)
    report2 = eng2.run()
    assert report2.deferred == 1


def test_duplicate_isrc_skipped(monkeypatch, cfg):
    t1 = make_track(id="track000000000000000001", isrc="BRABC2400001")
    t2 = make_track(id="track000000000000000003", isrc="BRABC2400001", name="MONTAGEM MIAU")
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [t1, t2]})
    youtube = EngineYouTube(
        catalog={"vid00000001": good},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.added == 1
    assert report.duplicates == 1


def _many_tracks(count):
    return [
        make_track(id=f"track{i:017d}", name=f"Song {i}", artists=["Solo"], isrc="")
        for i in range(count)
    ]


def test_dry_run_reads_only_first_200_source_tracks(monkeypatch, cfg):
    cfg.dry_run = True
    source_id = cfg.spotify_source_playlists[0]
    spotify = FakeSpotify({source_id: _many_tracks(2689)})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.source_read_limit == 200
    assert report.unique_tracks == 200
    assert spotify.read_calls == [(source_id, 200)]
    assert spotify.added == []
    assert youtube.inserted == []


def test_dry_run_stops_before_later_playlists(monkeypatch, cfg):
    cfg.dry_run = True
    first = cfg.spotify_source_playlists[0]
    second = "src222222222222222222222"
    cfg.spotify_source_playlists = [first, second]
    spotify = FakeSpotify({first: _many_tracks(250), second: _many_tracks(10)})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.unique_tracks == 200
    assert report.sources_read == 1
    assert spotify.read_calls == [(first, 200)]


def test_live_sync_does_not_apply_dry_run_read_limit(monkeypatch, cfg):
    cfg.dry_run = False
    source_id = cfg.spotify_source_playlists[0]
    spotify = FakeSpotify({source_id: _many_tracks(205)})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.source_read_limit == 0
    assert report.unique_tracks == 205
    assert spotify.read_calls == [(source_id, None)]


def test_spotify_only_adds_without_youtube(monkeypatch, cfg):
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    track = make_track(id="track000000000000000001")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.added == 1
    assert spotify.added == [track.uri]
    assert youtube.inserted == []
    assert eng.youtube is None
    assert eng.state.is_synced(track.id)
    entry = eng.state.get_synced(track.id)
    assert entry["spotify_added"] is True
    assert entry["youtube_added"] is False


def test_spotify_only_second_run_does_not_readd(monkeypatch, cfg):
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    track = make_track(id="track000000000000000001")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    eng.run()
    eng2 = build_engine(monkeypatch, cfg, spotify, youtube)
    report2 = eng2.run()
    assert report2.added == 0
    assert report2.already_synced == 1
    assert len(spotify.added) == 1


def test_spotify_only_dry_run_writes_nothing(monkeypatch, cfg):
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    cfg.dry_run = True
    track = make_track(id="track000000000000000001")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.added == 1
    assert spotify.added == []
    assert youtube.inserted == []
    assert not eng.state.is_synced(track.id)


def test_api_key_only_mode_adds_to_spotify_and_warns_about_writes(monkeypatch, cfg):
    """With just an API key, search runs but nothing is inserted into YouTube."""
    track = make_track(id="track000000000000000001")
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube(
        catalog={"vid00000001": good},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    youtube.can_write = False
    youtube.search_mode = "api_key"
    youtube.searches_this_run = 1
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.added == 1
    assert spotify.added == [track.uri]
    assert youtube.inserted == []
    assert "API key only" in report.quota_reason
    entry = eng.state.get_synced(track.id)
    assert entry["youtube_added"] is False


def test_dry_run_does_not_record_unmatched_as_synced(monkeypatch, cfg):
    cfg.dry_run = True
    track = make_track(id="track000000000000000009", name="No Match Song", artists=["Solo"])
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)
    report = eng.run()
    assert report.unmatched == 1
    assert not eng.state.is_synced(track.id)
    assert spotify.added == []


def test_partial_source_read_still_processes_tracks(monkeypatch, cfg):
    """A 429 in the middle of a source read must not lose the whole run."""
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    cfg.dry_run = True
    source_id = cfg.spotify_source_playlists[0]
    tracks = _many_tracks(3)
    spotify = FakeSpotify({source_id: tracks})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)

    def partial(playlist_id, max_tracks=None):
        spotify.partial_read = True
        return tracks

    monkeypatch.setattr(spotify, "playlist_tracks", partial)
    report = eng.run()
    assert report.partial_sources == 1
    assert report.spotify_throttled is True
    assert "rate limited" in report.summary_text()
    assert report.added == 3


def test_queue_mode_batches_writes(monkeypatch, cfg):
    """Queue mode: one queue call per track + a batch save, not one insert each."""
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    cfg.spotify_write_mode = "queue"
    cfg.spotify_queue_flush_size = 2
    tracks = _many_tracks(3)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    spotify.queue_supported = True
    spotify._queued = []
    spotify.queue_saves = 0
    spotify.queue_calls = []
    spotify.batch_calls = []

    def queue_track(uri, max_items=50):
        spotify.queue_calls.append(uri)
        spotify._queued.append(uri)
        return True

    def save_queue_to_playlist(playlist_id):
        sent = list(spotify._queued)
        spotify.batch_calls.append(list(sent))
        spotify._queued.clear()
        spotify.queue_saves += 1
        return len(sent)

    spotify.queue_track = queue_track
    spotify.save_queue_to_playlist = save_queue_to_playlist
    type(spotify).queued = property(lambda self: len(self._queued))
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    report = eng.run()
    assert len(spotify.queue_calls) == 3
    assert spotify.added == []  # no per-track inserts
    assert report.queued == 3
    assert "queue" in report.summary_text()


def test_queue_failure_falls_back_to_direct_add(monkeypatch, cfg):
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    cfg.spotify_write_mode = "queue"
    track = make_track(id="track000000000000000001")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    spotify.queue_supported = True
    spotify._queued = []
    spotify.queue_saves = 0

    def boom(uri, max_items=50):
        raise RuntimeError("player not available")

    spotify.queue_track = boom
    type(spotify).queued = property(lambda self: len(self._queued))
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    report = eng.run()
    assert spotify.added == [track.uri]  # direct fallback worked
    assert report.added == 1


def test_track_error_is_skipped_and_run_continues(monkeypatch, cfg):
    """One bad track must not end the run — skip it, do the next one."""
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    tracks = _many_tracks(3)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})

    calls = {"n": 0}

    def add_tracks(playlist_id, uris):
        calls["n"] += 1
        if calls["n"] == 2:  # the middle track fails
            raise RuntimeError("playlist write failed")
        spotify.added.extend(uris)

    monkeypatch.setattr(spotify, "add_tracks", add_tracks)
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    report = eng.run()
    assert report.added == 2          # tracks 1 and 3 went through
    assert report.skipped == 1        # the bad one was skipped, not fatal
    assert "Errored: 1" in report.summary_text()


def test_long_block_mid_run_records_resume_point(monkeypatch, cfg):
    """A long 429 stops cleanly and remembers where to restart."""
    import time

    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    tracks = _many_tracks(4)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())

    calls = {"n": 0}

    def add_one(track, target_ids, report):
        calls["n"] += 1
        if calls["n"] == 2:
            spotify.blocked_until = time.time() + 3600
            raise RateLimited(3600, "Spotify blocked this session for ~1h 0m")
        spotify.add_tracks(cfg.spotify_target_playlist, [track.uri])
        target_ids.add(track.id)
        return True

    monkeypatch.setattr(eng, "_add_spotify", add_one)
    report = eng.run()
    resume = eng.state.get_resume_point()
    assert resume["track_id"] == tracks[1].id
    assert report.rate_limited_skips == 1
    # A cooldown is never written to the state file: no run may skip itself.
    assert eng.state.spotify_cooldown_until() == 0.0

    # Next run starts after the skipped track instead of from the top.
    spotify.blocked_until = 0.0
    eng.state.save()
    eng2 = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    eng2.run()
    # Every track is added exactly once, whether the run that parked it
    # recovered it straight away or the next run did.
    assert len(spotify.added) == len(tracks)
    # (the fake tracks share one URI, so count calls, not set size)
    assert all(v.get("spotify_added") for v in eng2.state.data["synced"].values())
    assert not eng2.state.get_resume_point()


def test_a_throttled_add_is_never_recorded_as_added(monkeypatch, cfg):
    """The DB used to say `spotify_added: true` for a track it only parked.

    That is what the real run on 2026-11-07 did: Spotify was throttled, every
    track went to the manual bucket, and every one was written as added — so no
    later run would ever retry them.
    """
    import time as _time

    cfg.youtube_enabled = False
    tracks = _many_tracks(4)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})

    calls = {"n": 0}

    def add_tracks(playlist_id, uris):
        calls["n"] += 1
        if calls["n"] == 1:  # the first write is throttled, for an hour
            spotify.blocked_until = _time.time() + 3600
            raise RateLimited(3600, "Spotify blocked this session for ~1h 0m")
        spotify.added.extend(uris)  # pragma: no cover - must never happen now

    monkeypatch.setattr(spotify, "add_tracks", add_tracks)
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    report = eng.run()

    # The rest of the run is parked, not written off as done.
    assert report.manual_queued == 3
    assert report.added == 0
    assert not any(v.get("spotify_added") for v in eng.state.data["synced"].values()), (
        "a parked track was recorded as added — it will never be retried"
    )
    assert len(eng.state.pending_spotify()) == 3
    assert len(eng.state.manual_items()) == 3
    assert "Parked for the next run: 3 track(s)" in report.summary_text()

    # Next run, cooldown over: every track is added exactly once.
    spotify.blocked_until = 0.0
    spotify.added.clear()
    calls["n"] = 99  # the cooldown is over: no write is refused any more
    eng.state.save()
    eng2 = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    eng2.run()
    assert len(spotify.added) == len(tracks)
    assert not eng2.state.pending_spotify()
    assert not eng2.state.manual_items()
    assert all(v.get("spotify_added") for v in eng2.state.data["synced"].values())


class _WebReader:
    """Stand-in for PublicPlaylistReader (no network)."""

    def __init__(self, tracks, error=""):
        self.tracks = tracks
        self.last_error = error

    def playlist_tracks(self, playlist_id):
        return self.tracks


def test_web_fallback_reads_public_page_when_api_is_throttled(monkeypatch, cfg):
    """429 on the API -> read the public page instead of failing the run."""
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    tracks = _many_tracks(2)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    youtube = EngineYouTube()
    eng = build_engine(monkeypatch, cfg, spotify, youtube)

    def throttled(playlist_id, max_tracks=None):
        raise RateLimited(1800, "still 429")

    monkeypatch.setattr(spotify, "playlist_tracks", throttled)
    monkeypatch.setattr(spotify, "playlist_track_ids", lambda pid: set())
    eng.public_spotify = _WebReader(tracks)

    report = eng.run()
    assert report.web_reads == 1
    assert report.added == 2  # dry run counts what it would add
    assert report.sources_read == 1
    assert "public page" in report.summary_text().lower() or report.web_reads == 1


def test_web_fallback_failure_falls_back_to_cache(monkeypatch, cfg):
    cfg.youtube_enabled = False
    cfg.youtube_target_playlist = ""
    tracks = _many_tracks(2)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    eng.source_cache.put(cfg.spotify_source_playlists[0], tracks, "")

    def throttled(playlist_id, max_tracks=None):
        raise RateLimited(1800, "still 429")

    monkeypatch.setattr(spotify, "playlist_tracks", throttled)
    monkeypatch.setattr(spotify, "playlist_track_ids", lambda pid: set())
    eng.public_spotify = _WebReader([], error="HTTP 404")

    report = eng.run()
    assert report.web_reads == 0
    assert report.cached_sources >= 1
    assert report.added == 2


def test_rate_limited_run_does_not_store_a_cooldown(monkeypatch, cfg):
    """A 429 ends this run; the next run starts immediately."""
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    spotify.blocked_until = 0.0

    def boom(playlist_id, max_tracks=None):
        raise RateLimited(600, "Spotify is throttling the token endpoint (HTTP 429)")

    spotify.playlist_tracks = boom
    spotify.playlist_track_ids = lambda *a, **k: set()
    engine = build_engine(monkeypatch, cfg, spotify, FakeYouTube())

    # Pretend the token call parked the client for 10 minutes and then failed.
    def collect(report):
        engine.spotify.blocked_until = time.time() + 600
        raise RateLimited(600, "Spotify is throttling the token endpoint (HTTP 429)")

    monkeypatch.setattr(engine, "_collect_source_tracks", collect)
    report = engine.run()
    assert report.spotify_throttled                     # reported for this run
    assert engine.state.spotify_cooldown_until() == 0.0  # nothing persisted


def test_main_returns_success_when_run_only_hit_a_cooldown(cfg, monkeypatch, tmp_path, capsys):
    """A 429 cooldown is expected behaviour — do not fail the Actions job."""
    import phonksync.__main__ as main
    from phonksync.engine import RunReport

    class FakeEngine:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            return RunReport(
                started_at="now", dry_run=False, spotify_throttled=True,
                quota_reason="Spotify cooldown — 25m remaining",
            )

    class FakeConfig(Config):
        @classmethod
        def load(cls, path):
            c = Config()
            c.spotify_source_playlists = ["src111111111111111111111"]
            c.spotify_target_playlist = "target"
            c.state_path = str(tmp_path / "state.json")
            c.overrides_path = str(tmp_path / "overrides.json")
            c.cache_path = str(tmp_path / "cache.json")
            return c

    monkeypatch.setenv("SPOTIFY_COOKIE", "sp_dc=" + "a" * 60)
    monkeypatch.delenv("YOUTUBE_ENABLED", raising=False)
    monkeypatch.setattr(main, "Config", FakeConfig)
    monkeypatch.setattr(main, "SyncEngine", FakeEngine, raising=False)
    import phonksync.engine as engine_mod

    monkeypatch.setattr(engine_mod, "SyncEngine", FakeEngine)
    assert main.main(["sync"]) == 0
    assert "Nothing was lost" in capsys.readouterr().err


def test_a_parked_spotify_run_still_finishes(monkeypatch, cfg):
    """429 on writes must park the rest, not kill the run."""
    track_a = make_track(id="track000000000000000001", name="A")
    track_b = make_track(id="track000000000000000002", name="B")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track_a, track_b]})
    engine = build_engine(monkeypatch, cfg, spotify, EngineYouTube())

    def boom(*a, **kw):
        engine.spotify.blocked_until = time.time() + 1800
        raise RateLimited(1800, "Spotify kept answering 429")

    monkeypatch.setattr(engine, "_add_spotify", boom)
    monkeypatch.setattr(engine, "_flush_queue", lambda: None)
    monkeypatch.setattr(engine, "_retry_pending_queue", lambda: None)
    report = engine.run()
    # the run completed and reported, instead of raising out of run()
    assert report.finished_at
    assert report.spotify_throttled
    assert report.manual_queued >= 1          # nothing lost: parked
    # parked in the manual bucket, but nothing stops the next run
    assert engine.state.spotify_cooldown_until() == 0.0


def test_pathfinder_is_used_when_spotify_already_parked(monkeypatch, cfg):
    """A parked run reads playlists without the Web API instead of retrying."""
    track = make_track(id="track000000000000000001")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    spotify.blocked_until = time.time() + 1800

    def never(playlist_id, max_tracks=None):
        raise AssertionError("the Web API must not be called while parked")

    spotify.playlist_tracks = never
    youtube = EngineYouTube()
    engine = build_engine(monkeypatch, cfg, spotify, youtube)

    reached = []
    monkeypatch.setattr(
        engine, "_read_pathfinder",
        lambda pid, report: reached.append(pid) or [track],
    )
    report = engine.run()
    assert reached == [cfg.spotify_source_playlists[0]]
    assert report.spotify_throttled


def test_web_player_read_does_not_also_warn_about_the_public_page(monkeypatch, cfg):
    """One working route means one message — not a scary warning next to it."""
    track = make_track(id="track000000000000000001")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    spotify.blocked_until = time.time() + 1800
    spotify.playlist_tracks = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API"))
    warnings = []

    class Tel(SilentTelegram):
        def warn(self, text):
            warnings.append(text)

        def info(self, text):
            warnings.append(text)

    monkeypatch.setattr(engine_mod, "TelegramLogger", Tel)
    engine = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    monkeypatch.setattr(engine, "_read_pathfinder", lambda pid, report: [track])
    report = engine.run()
    assert report.sources_read == 1
    assert not any("public page" in w for w in warnings)


# ---------------------------------------------------------------- caching
def _cache_with(monkeypatch, cfg, tracks, age_hours=0.0, snapshot=""):
    import json as _json
    import time as _time
    from pathlib import Path

    path = Path(cfg.source_cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"playlists": {cfg.spotify_source_playlists[0]: {
        "ts": _time.time() - age_hours * 3600,
        "snapshot": snapshot,
        "tracks": [{
            "id": t.id, "name": t.name, "artists": t.artists, "album": t.album,
            "duration_ms": t.duration_ms, "isrc": t.isrc, "uri": t.uri,
            "popularity": t.popularity, "sources": t.sources,
        } for t in tracks],
    }}}), encoding="utf-8")


def test_stale_cache_is_replayed_when_spotify_is_parked(monkeypatch, cfg):
    """A 10-day-old read beats a 429: zero calls, full track list."""
    track = make_track(id="track000000000000000009")
    _cache_with(monkeypatch, cfg, [track], age_hours=240)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    spotify.blocked_until = time.time() + 1800
    spotify.playlist_tracks = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API"))
    engine = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    monkeypatch.setattr(engine, "_read_pathfinder", lambda pid, report: [])
    monkeypatch.setattr(engine, "_read_public_playlist", lambda pid, report, **kw: [])
    report = engine.run()
    assert report.stale_reads == 1
    assert report.sources_read == 1


def test_expired_cache_is_not_used(monkeypatch, cfg):
    track = make_track(id="track000000000000000010")
    cfg.spotify_cache_max_age_days = 1
    _cache_with(monkeypatch, cfg, [track], age_hours=24 * 5)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    spotify.blocked_until = time.time() + 1800
    engine = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    monkeypatch.setattr(engine, "_read_pathfinder", lambda pid, report: [])
    monkeypatch.setattr(engine, "_read_public_playlist", lambda pid, report, **kw: [])
    report = engine.run()
    assert report.stale_reads == 0
    assert report.sources_read == 0


def test_cache_can_be_disabled(monkeypatch, cfg):
    track = make_track(id="track000000000000000011")
    cfg.spotify_cache_allow_stale = False
    _cache_with(monkeypatch, cfg, [track], age_hours=240)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    spotify.blocked_until = time.time() + 1800
    engine = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    monkeypatch.setattr(engine, "_read_pathfinder", lambda pid, report: [])
    monkeypatch.setattr(engine, "_read_public_playlist", lambda pid, report, **kw: [])
    report = engine.run()
    assert report.stale_reads == 0


def test_cache_fill_stores_tracks_without_the_web_api(tmp_path, monkeypatch, capsys):
    import phonksync.__main__ as main
    from phonksync import spotify as sp

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    cache_path = tmp_path / "source-cache.json"

    class Cfg(Config):
        @classmethod
        def load(cls, path):
            c = Config()
            c.spotify_source_playlists = ["src111111111111111111111"]
            c.source_cache_path = str(cache_path)
            c.spotify_cache_allow_stale = True
            c.spotify_cache_max_age_days = 30
            return c

    track = make_track(id="track000000000000000099", name="Cached One")
    monkeypatch.setattr(main, "Config", Cfg)
    monkeypatch.setattr(sp, "SpotifyClient", lambda *a, **kw: type(
        "C", (), {"_pathfinder_tracks": lambda self, pid: [track]})())
    monkeypatch.setenv("SPOTIFY_COOKIE", "sp_dc=" + "a" * 60)
    assert main.main(["cache-fill"]) == 0
    import json

    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    ids = list(saved["playlists"]["src111111111111111111111"]["tracks"])
    assert [t["name"] for t in ids] == ["Cached One"]
    assert "stored" in capsys.readouterr().out


def test_cache_command_lists_stored_playlists(monkeypatch, tmp_path, capsys):
    import json

    import phonksync.__main__ as main

    cache_path = tmp_path / "source-cache.json"
    cache_path.write_text(json.dumps({"playlists": {
        "src111111111111111111111": {"ts": time.time(), "snapshot": "", "tracks": [{"id": "a", "name": "n"}]}
    }}), encoding="utf-8")

    class Cfg(Config):
        @classmethod
        def load(cls, path):
            c = Config()
            c.spotify_source_playlists = ["src111111111111111111111"]
            c.source_cache_path = str(cache_path)
            return c

    monkeypatch.setattr(main, "Config", Cfg)
    assert main.main(["cache"]) == 0
    out = capsys.readouterr().out
    assert "1 playlist(s)" in out and "src111111111111111111111" in out and "fresh" in out


def test_budget_exhaustion_is_reported_separately_from_unmatched(monkeypatch, cfg):
    """Running out of searches must not look like "not on YouTube"."""
    tracks = [make_track(id=f"track{i:020d}") for i in range(3)]
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    youtube = EngineYouTube()
    youtube.max_searches_per_run = 0
    youtube.searches_this_run = 0
    engine = build_engine(monkeypatch, cfg, spotify, youtube)

    class NoBudgetMatcher:
        last_tried = ["STRICT"]
        last_won = ""

        def match(self, track):
            youtube.searches_this_run += 5      # budget is spent
            return None

    engine.matcher = NoBudgetMatcher()
    engine.youtube.max_searches_per_run = 3
    report = engine.run()
    assert report.budget_skips == 3
    assert report.unmatched == 0
    # a budget skip must not start the multi-day "retry later" clock
    assert engine.state.unmatched_tries(tracks[0].id) == 0


def test_skipped_tracks_cost_nothing_and_are_not_unmatched(monkeypatch, cfg):
    keep = make_track(id="track000000000000000021", name="Keep Me")
    drop = make_track(id="track000000000000000022", name="Not On YouTube")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [keep, drop]})
    youtube = EngineYouTube()
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    engine.state.skip(drop, "not on YouTube")
    report = engine.run()
    assert report.skipped_by_choice == 1
    assert drop.id not in (engine.state.data.get("unmatched") or {})
    searched = [" ".join(q.split()) for q in youtube.searches]
    assert not any("not on youtube" in q for q in searched)
    assert engine.state.unmatched_tries(drop.id) == 0
    assert engine.state.is_skipped(drop.id)


def test_skip_cli_marks_and_clears(tmp_path, monkeypatch, capsys):
    import phonksync.__main__ as main

    class Cfg(Config):
        @classmethod
        def load(cls, path):
            c = Config()
            c.state_path = str(tmp_path / "state.json")
            return c

    monkeypatch.setattr(main, "Config", Cfg)
    assert main.main(["skip", "abc123", "--reason", "not on YouTube"]) == 0
    assert "Skipped abc123" in capsys.readouterr().out
    assert main.main(["skip", "--show"]) == 0
    out = capsys.readouterr().out
    assert "abc123" in out and "not on YouTube" in out
    assert main.main(["skip", "--clear", "abc123"]) == 0
    assert "Un-skipped" in capsys.readouterr().out


def test_skip_all_unmatched(tmp_path, monkeypatch, capsys):
    import phonksync.__main__ as main
    from phonksync.db import StateDB

    class Cfg(Config):
        @classmethod
        def load(cls, path):
            c = Config()
            c.state_path = str(tmp_path / "state.json")
            return c

    monkeypatch.setattr(main, "Config", Cfg)
    db = StateDB(str(tmp_path / "state.json"))
    db.mark_unmatched(make_track(id="t1", name="Song One"), ["STRICT"])
    db.mark_unmatched(make_track(id="t2", name="Song Two"), ["STRICT"])
    db.save()
    assert main.main(["skip", "--all-unmatched"]) == 0
    db2 = StateDB(str(tmp_path / "state.json"))
    assert db2.is_skipped("t1") and db2.is_skipped("t2")
    assert db2.data["unmatched"] == {}


def test_summary_mentions_skips_and_budget(monkeypatch, cfg):
    from phonksync.engine import RunReport

    report = RunReport(started_at="now", dry_run=False)
    report.skipped_by_choice = 2
    report.budget_skips = 1
    report.free_searches = 40
    report.free_matches = 30
    text = report.summary_text()
    assert "Skipped by choice: 2" in text
    assert "Out of search budget: 1" in text
    assert "Free YouTube: 40 search(es)" in text


# ------------------------------------------------ YouTube writes are off
class ReadOnlyYouTube(EngineYouTube):
    @property
    def can_write(self):
        return False


def test_writes_off_is_announced_and_counted(monkeypatch, cfg):
    track = make_track(id="track000000000000000031")
    vid = make_candidate("vid00000031", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = ReadOnlyYouTube(
        catalog={"vid00000031": vid},
        search_map={"nulteex montagem miau": ["vid00000031"]},
    )
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    report = engine.run()
    assert report.youtube_readonly == 1
    assert youtube.inserted == []
    # the match is remembered, so a later run can add it without searching again
    assert engine.state.pending_youtube_count() == 1


def test_pending_youtube_matches_are_flushed_once_writes_work(monkeypatch, cfg):
    track = make_track(id="track000000000000000032")
    vid = make_candidate("vid00000032", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    youtube = EngineYouTube(catalog={"vid00000032": vid})
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    engine.state.mark_synced(track, "vid00000032", "STRICT", True, False)
    engine.state.save()
    report = engine.run()
    assert report.pending_youtube_flushed == 1
    assert youtube.inserted == ["vid00000032"]
    assert engine.state.pending_youtube_count() == 0

def test_writes_off_warning_names_the_cookie_when_that_is_the_problem(monkeypatch, cfg):
    track = make_track(id="track000000000000000041")
    vid = make_candidate("vid00000041", "Nulteex - PHONK", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube(catalog={"vid00000041": vid})
    youtube.can_write = False
    youtube.cookie_diag = lambda: (
        "cookie present (17 chars) but no __Secure-3PAPISID — only part of the "
        "cookie was pasted (need the whole Cookie header)"
    )
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    report = engine.run()
    summary = report.summary_text()
    assert "YT_COOKIE is set but unusable" in summary
    assert "whole Cookie header" in summary


def test_start_message_does_not_claim_oauth_is_the_only_option(monkeypatch, cfg):
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    youtube = EngineYouTube(catalog={})
    youtube.can_write = False
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    engine.run()
    joined = " ".join(
        m if isinstance(m, str) else " ".join(str(part) for part in m)
        for m in engine.telegram.messages
    )
    assert "YT_COOKIE" in joined
    assert "playlist adds need OAuth" not in joined


def test_one_message_per_run_even_with_many_tracks(monkeypatch, cfg):
    tracks = [make_track(id=f"track{i:021d}", name=f"SONG {i}") for i in range(12)]
    catalog, search_map = {}, {}
    for i in range(12):
        vid = f"vid{i:08d}"
        catalog[vid] = make_candidate(vid, f"Nulteex - SONG {i}", channel="Nulteex - Topic")
        search_map[f"nulteex song {i}"] = [vid]
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    youtube = EngineYouTube(catalog=catalog, search_map=search_map)
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    report = engine.run()
    assert report.added >= 1
    # SilentTelegram stands in for the real logger: it records one flush.
    assert engine.telegram.flushed == 1
    assert "Sync run finished" in engine.telegram.last_summary


def test_second_run_does_not_report_the_same_songs_again(monkeypatch, cfg):
    track = make_track(id="track000000000000000051", name="PHONK")
    vid = make_candidate("vid00000051", "Nulteex - PHONK", channel="Nulteex - Topic")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube(catalog={"vid00000051": vid}, search_map={"nulteex phonk": ["vid00000051"]})
    engine = build_engine(monkeypatch, cfg, spotify, youtube)

    first = engine.run()
    assert first.added == 1
    assert any("PHONK" in line for line in first.added_tracks)

    second = engine.run()
    assert second.added == 0
    assert second.added_tracks == []
    assert "PHONK" not in second.summary_text()
    assert second.already_synced == 1


def test_a_song_in_the_db_is_never_logged_even_if_the_playlist_repeats_it(monkeypatch, cfg):
    track = make_track(id="track000000000000000052", name="LOOP")
    vid = make_candidate("vid00000052", "Nulteex - LOOP", channel="Nulteex - Topic")
    # same track twice in one read (duplicate row in the source playlist)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track, track]})
    youtube = EngineYouTube(
        catalog={"vid00000052": vid}, search_map={"nulteex loop": ["vid00000052"]}
    )
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    report = engine.run()
    assert sum("LOOP" in line for line in report.added_tracks) <= 1


def test_web_api_budget_stops_the_slow_reads(monkeypatch, cfg):
    """Pre-work must not spend the whole run on 2s-paced Web API calls."""
    cfg.spotify_max_requests_per_run = 0  # 0 = unlimited for the first read
    tracks = [make_track(id="track000000000000000061", name="ONE")]
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    youtube = EngineYouTube(catalog={})
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    # pretend the Web API already answered a lot of calls
    spotify.requests_made = 40
    cfg.spotify_max_requests_per_run = 10
    engine.state.data["synced"]["track000000000000000061"] = {
        "spotify_id": "track000000000000000061", "spotify_added": True, "youtube_added": True,
    }
    report = engine.run()
    # the source was NOT read through the slow Web API path
    assert spotify.read_calls == []
    assert report.sources_read == 0
    assert report.skipped_sources == 1  # counted, never silent


def test_budget_fallback_is_not_reported_as_a_spotify_block(monkeypatch, cfg):
    cfg.spotify_max_requests_per_run = 1
    tracks = [make_track(id="track000000000000000062", name="TWO")]
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    youtube = EngineYouTube(catalog={})
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    spotify.requests_made = 5
    engine.state.data["synced"]["track000000000000000062"] = {
        "spotify_id": "track000000000000000062", "spotify_added": True, "youtube_added": True,
    }
    report = engine.run()
    # no slowdown call is made, and the fallback is explained as a budget, not a block
    assert spotify.read_calls == []
    assert any("Web API budget" in d for d in report.daily_details)


def test_summary_shows_counts_and_only_the_unadded_tracks():
    from phonksync.engine import RunReport

    report = RunReport(
        added=4, skipped_by_choice=3, unmatched=2,
        sources_read=2, unique_tracks=100, already_synced=90,
    )
    report.failed_tracks = ["Nulteex — hd — no YouTube match"]
    text = report.summary_text()
    assert "Added: <b>4</b>" in text
    assert "Skipped by choice: 3" in text
    assert "Failed / unmatched: 2" in text
    assert "<b>Not added</b> (1):" in text
    assert "Nulteex — hd" in text
    # no added-song listing at all
    assert "✅ Added (" not in text


def test_rate_limited_run_stops_spending_web_api_calls(monkeypatch, cfg):
    """A 429 must end the Web API spending immediately, not per-playlist."""
    cfg.spotify_source_playlists = [
        "src111111111111111111111", "src222222222222222222222",
    ]
    spotify = FakeSpotify({
        "src111111111111111111111": [],
        "src222222222222222222222": [],
    })
    youtube = EngineYouTube(catalog={})
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    spotify.rate_limited_this_run = True
    engine.run()
    assert spotify.read_calls == []


def test_dry_run_respects_the_user_track_cap(tmp_path, monkeypatch):
    from phonksync.settings import Config

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MAX_TRACKS_PER_RUN", "30")
    monkeypatch.setenv("YOUTUBE_ENABLED", "false")
    cfg = Config.load("config.json")
    # the default 200 must bend to the explicit 30
    assert cfg.max_tracks_per_run == 30
    assert cfg.dry_run_track_limit <= 30


def test_summary_mentions_the_user_cap_not_the_default():
    from phonksync.engine import RunReport

    report = RunReport(dry_run=True, added=29, source_read_limit=30, max_tracks_per_run=30)
    text = report.summary_text()
    assert "previewed 30 track(s) (your limit: 30)" in text


def test_a_stored_cooldown_never_skips_a_run(monkeypatch, cfg):
    """The old bug: "Spotify cooldown — 25m remaining; this run was skipped"."""
    import time

    track = make_track(id="track000000000000000071", name="LATE")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    engine = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    # a cooldown left behind by an earlier run (25 minutes to go)
    engine.state.set_spotify_cooldown(time.time() + 1500, "Spotify 429")
    engine.state.save()
    # the web player route works (no Web API, no 429)
    spotify._pathfinder_tracks = lambda pid, max_tracks=0: list(
        spotify.sources.get(pid, [])
    )

    report = engine.run()
    # the run still happened, through the web route
    assert spotify.read_calls == []  # zero Web API calls
    assert report.sources_read == 1
    assert report.unique_tracks == 1
    # …and it says why it used the web route instead of the Web API
    assert any("asked to slow down" in d for d in report.daily_details)
    # the stale cooldown is gone afterwards
    assert engine.state.spotify_cooldown_until() == 0.0


def test_stale_cooldown_is_cleared_when_the_run_is_not_blocked(monkeypatch, cfg):
    import time

    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    engine = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    engine.state.set_spotify_cooldown(time.time() + 60, "old block")
    engine.run()
    assert engine.state.spotify_cooldown_until() == 0.0


# ------------------------------------------------- workflow knobs (sync.yml)
def test_free_search_pause_and_results_per_query_come_from_the_environment(
    tmp_path, monkeypatch
):
    """The sync.yml inputs must reach the client: 0.2s pause, 5 results."""
    from phonksync.settings import Config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_FREE_SEARCH_PAUSE", "0.2")
    monkeypatch.setenv("SEARCH_RESULTS_PER_QUERY", "5")
    cfg = Config.load(cfg_path)
    assert cfg.youtube_free_search_pause == 0.2
    assert cfg.search_results_per_query == 5


def test_empty_or_bad_pacing_inputs_never_break_a_run(tmp_path, monkeypatch):
    """An empty workflow input ("") must mean "use config.json", not a crash."""
    from phonksync.settings import Config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_FREE_SEARCH_PAUSE", "")
    monkeypatch.setenv("SEARCH_RESULTS_PER_QUERY", "abc")
    cfg = Config.load(cfg_path)
    assert cfg.youtube_free_search_pause == 1.0
    assert cfg.search_results_per_query == 10


def test_pacing_env_values_are_clamped(tmp_path, monkeypatch):
    from phonksync.settings import Config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_FREE_SEARCH_PAUSE", "-3")
    monkeypatch.setenv("SEARCH_RESULTS_PER_QUERY", "9999")
    cfg = Config.load(cfg_path)
    assert cfg.youtube_free_search_pause == 0.0
    assert cfg.search_results_per_query == 50


def test_youtube_problem_separates_not_set_from_unusable(monkeypatch, cfg):
    """"no write credential" was ambiguous: set vs. set-but-rejected."""
    cfg.youtube_enabled = True
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})

    class NoCookie(EngineYouTube):
        cookie = ""
        can_write = False
        write_mode = "none"

        def cookie_diag(self):
            return "no YT_COOKIE set"

    engine = build_engine(monkeypatch, cfg, spotify, NoCookie())
    assert "not set at all" in engine._youtube_write_problem()

    class BadCookie(NoCookie):
        cookie = "SID=abc; HSID=def"

        def cookie_diag(self):
            return "cookie present (24 chars) but no usable SAPISID"

    engine2 = build_engine(monkeypatch, cfg, spotify, BadCookie())
    problem = engine2._youtube_write_problem()
    assert "set but unusable" in problem and "SAPISID" in problem


# ------------------------------------------- a write credential appears later
def test_unmatched_track_keeps_its_best_candidate_without_writing(monkeypatch, cfg):
    """A near-miss is remembered for review — never added, never re-searched."""
    cfg.youtube_enabled = True
    track = make_track(id="track000000000000000081", name="AWE - SLOWED", artists=["NVRVYN"])
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    # A promo upload with the right title words: found, but the artist evidence
    # is not good enough for the checks.
    close = make_candidate(
        "vidAWE00001", "NVRVYN - AWE (Super Slowed)", channel="promo channel"
    )
    youtube = EngineYouTube(catalog={"vidAWE00001": close},
                            search_map={"nvrvyn awe - slowed": ["vidAWE00001"]})
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    report = engine.run()

    assert report.unmatched == 1                     # still a miss
    assert youtube.inserted == []                    # nothing was written
    saved = engine.state.candidates()
    assert len(saved) == 1
    assert saved[0]["video_id"] == "vidAWE00001"
    assert report.candidate_pool == 1


def test_pending_lists_waiting_matches_and_adds_no_duplicates(monkeypatch, cfg, tmp_path, capsys):
    import phonksync.__main__ as main
    from phonksync.db import StateDB

    cfg.youtube_target_playlist = "PLtarget"
    monkeypatch.setenv("SPOTIFY_COOKIE", "sp_dc=" + "a" * 60)
    monkeypatch.setenv("YT_REFRESH_TOKEN", "1//refresh")
    monkeypatch.setenv("YT_CLIENT_ID", "cid")
    monkeypatch.setenv("YT_CLIENT_SECRET", "secret")

    db = StateDB(cfg.state_path)
    track = make_track(id="track000000000000000091", name="WAITING ONE")
    db.mark_synced(track, "vidWAIT00001", "STRICT", True, False)
    db.save()

    class FakeYT:
        can_write = True

        def __init__(self, *a, **kw):
            self.added = []

        def playlist_video_ids(self, playlist_id):
            return set()  # not there yet

        def add_to_playlist(self, playlist_id, video_id):
            self.added.append(video_id)

    import phonksync.youtube as yt_mod

    fake = FakeYT()
    monkeypatch.setattr(yt_mod, "YouTubeClient", lambda *a, **kw: fake)
    monkeypatch.setattr(main, "Config", type("C", (), {"load": classmethod(lambda cls, p: cfg)}))

    assert main.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "1 matched video(s) waiting" in out
    assert fake.added == []          # listing never writes

    assert main.main(["pending", "--add"]) == 0
    assert fake.added == ["vidWAIT00001"]
    assert StateDB(cfg.state_path).pending_youtube() == []


def test_pending_add_skips_videos_that_are_already_in_the_playlist(monkeypatch, cfg, capsys):
    import phonksync.__main__ as main
    from phonksync.db import StateDB

    cfg.youtube_target_playlist = "PLtarget"
    monkeypatch.setenv("SPOTIFY_COOKIE", "sp_dc=" + "a" * 60)
    db = StateDB(cfg.state_path)
    db.mark_synced(make_track(id="track000000000000000092"), "vidDUP000001", "STRICT", True, False)
    db.save()

    class FakeYT:
        can_write = True

        def __init__(self, *a, **kw):
            self.added = []

        def playlist_video_ids(self, playlist_id):
            return {"vidDUP000001"}     # already added on YouTube earlier

        def add_to_playlist(self, playlist_id, video_id):
            self.added.append(video_id)

    import phonksync.youtube as yt_mod

    fake = FakeYT()
    monkeypatch.setattr(yt_mod, "YouTubeClient", lambda *a, **kw: fake)
    monkeypatch.setattr(main, "Config", type("C", (), {"load": classmethod(lambda cls, p: cfg)}))

    assert main.main(["pending", "--add"]) == 0
    assert fake.added == []                       # no duplicate write
    assert "already there 1" in capsys.readouterr().out


def test_summary_mentions_the_near_miss_pool(monkeypatch, cfg):
    from phonksync.engine import RunReport

    report = RunReport(started_at="now")
    report.candidate_pool = 3
    assert "3" in report.summary_text()


def test_a_writes_only_run_adds_the_backlog_and_searches_nothing(monkeypatch, cfg):
    """After adding YT_REFRESH_TOKEN: one fast run, zero searches."""
    from phonksync.db import StateDB

    cfg.youtube_enabled = True
    cfg.youtube_writes_only = True
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    youtube = EngineYouTube()

    # the backlog is already in the state file when the run starts
    db = StateDB(cfg.state_path)
    db.mark_synced(make_track(id="track0000000000000000a1"), "vidQUEUE0001", "STRICT", True, False)
    db.save()

    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    report = engine.run()
    assert youtube.inserted == ["vidQUEUE0001"]   # the backlog was written
    assert youtube.searches == []                 # nothing was searched
    assert report.pending_youtube_flushed == 1


# ------------------------------------------------- secret audit (names only)
_AUDIT_ENV = {
    "SPOTIFY_COOKIE": "sp_dc=" + "a" * 60,
    "SPOTIFY_SOURCE_PLAYLISTS": "src" + "a" * 19,
    "SPOTIFY_TARGET_PLAYLIST": "tgt" + "b" * 19,
    "TELEGRAM_BOT_TOKEN": "1:abc",
    "TELEGRAM_CHAT_ID": "1",
    "YOUTUBE_ENABLED": "true",
    "YOUTUBE_TARGET_PLAYLIST": "PL" + "x" * 20,
    "YOUTUBE_API_KEY": "AIza",
}


def _run_check(monkeypatch, capsys, **extra):
    import phonksync.__main__ as main

    for key in list(_AUDIT_ENV) + ["YT_REFRESH_TOKEN", "YT_CLIENT_ID",
                                   "YT_CLIENT_SECRET", "YT_COOKIE", "PROXY_URL",
                                   "SPOTIFY_PROXY_URL"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**_AUDIT_ENV, **extra}.items():
        monkeypatch.setenv(key, value)
    code = main.main(["check"])
    return code, capsys.readouterr().out


def test_check_names_every_secret_and_never_prints_a_value(monkeypatch, capsys):
    code, out = _run_check(
        monkeypatch, capsys,
        YT_REFRESH_TOKEN="1//secret-token", YT_CLIENT_ID="1234-abcdef.apps.googleusercontent.com",
        YT_CLIENT_SECRET="shhh-client-secret", YT_COOKIE="SID=abc; __Secure-3PAPISID=xyz",
    )
    assert code == 0
    assert "YT_REFRESH_TOKEN" in out and "YT_CLIENT_SECRET" in out
    assert "YouTube writes will use: OAuth refresh token" in out
    for secret in ("secret-token", "shhh-client-secret", "AIza", "sp_dc="):
        assert secret not in out, f"a secret value leaked into the log: {secret}"


def test_half_an_oauth_pair_fails_the_check(monkeypatch, capsys):
    code, out = _run_check(
        monkeypatch, capsys,
        YT_REFRESH_TOKEN="1//t",
        YT_CLIENT_ID="1234-abcdefghijklmnopqrst.apps.googleusercontent.com",
    )
    assert code == 1
    assert "YT_CLIENT_SECRET is missing" in out


def test_proxy_url_short_name_now_works(monkeypatch, capsys):
    code, out = _run_check(monkeypatch, capsys, PROXY_URL="https://proxy.example/")
    assert code == 0
    assert "Spotify route: proxy https://proxy.example/" in out


def test_a_swapped_token_fails_the_check(monkeypatch, capsys):
    code, out = _run_check(
        monkeypatch, capsys,
        YT_REFRESH_TOKEN="ya29.access-token",
        YT_CLIENT_ID="1234-abcdefghijklmnopqrst.apps.googleusercontent.com",
        YT_CLIENT_SECRET="s",
    )
    assert code == 1
    assert "access token" in out


def test_a_present_but_empty_secret_is_reported(monkeypatch, capsys):
    """The GitHub UI hides blank secrets, so the run must say it out loud."""
    code, out = _run_check(
        monkeypatch, capsys,
        YT_REFRESH_TOKEN="1//t",
        YT_CLIENT_ID="1234-abcdefghijklmnop.apps.googleusercontent.com",
        YT_CLIENT_SECRET="",  # exists in Settings, empty here
    )
    assert code == 1
    assert "YT_CLIENT_SECRET exists but its value is empty" in out


def test_short_but_real_values_are_not_nagged(monkeypatch, capsys):
    code, out = _run_check(
        monkeypatch, capsys,
        YT_REFRESH_TOKEN="1//t",
        YT_CLIENT_ID="1234-abcdefghijklmnop.apps.googleusercontent.com",
        YT_CLIENT_SECRET="s",  # 1 char, but it is the user's value
    )
    assert code == 0, out
    assert "too short" not in out


def test_a_client_json_does_not_hide_the_explicit_pair(monkeypatch):
    """The failure mode when "the secret was already set": the JSON wins."""
    from phonksync.youtube import YouTubeClient

    json_client = "2222-jsonclient.apps.googleusercontent.com"
    client_json = (
        '{"installed": {"client_id": "%s", "client_secret": "jsonsecret"}}' % json_client
    )
    seen = []

    class Session:
        headers = {}

        def post(self, url, data=None, timeout=None):
            seen.append(dict(data or {}))

            class R:
                # only the *pair* may redeem this token
                status_code = 200 if (data or {}).get("client_id") == "pair-client" else 400
                text = "{}"
                headers = {}

                def json(self_inner):
                    return {"access_token": "ya29.ok", "expires_in": 3600}

            return R()

    yt = YouTubeClient(
        client_json=client_json,
        client_id="pair-client",
        client_secret="pair-secret",
        refresh_token="1//token",
        session=Session(),
        free_search=False,
    )
    assert yt._ensure_token() == "ya29.ok"
    assert [c["client_id"] for c in seen] == [json_client, "pair-client"]
    assert yt.client_used == "pair-client"


def test_a_setup_failure_is_printed_and_summarised(monkeypatch, cfg, capsys):
    """Exit 1 with an empty summary was the worst possible failure mode."""
    from phonksync.errors import AuthError

    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: []})
    engine = build_engine(monkeypatch, cfg, spotify, EngineYouTube())

    def refuse(report):
        raise AuthError("Spotify refused to issue a web-player token (403)")

    monkeypatch.setattr(engine, "_collect_source_tracks", refuse)
    report = engine.run()

    assert report.setup_failed is True
    assert "Nothing could run" in report.summary_text()
    assert "Spotify refused" in capsys.readouterr().err


def test_the_old_parked_but_added_state_is_repaired_once(monkeypatch, cfg):
    """State written by the buggy version: parked AND marked added, same instant."""

    cfg.youtube_enabled = False
    tracks = _many_tracks(2)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())

    for track in tracks:
        eng.state.add_manual(track, reason="Spotify asked to slow down")
        eng.state.mark_synced(track, "", "", True, False)  # the old lie
        entry = eng.state.manual_items()[track.id]
        eng.state.data["synced"][track.id]["added_at"] = entry["queued_at"]
    assert len(eng.state.pending_spotify()) == 0

    repaired = eng._repair_parked_bucket()
    assert repaired == len(tracks)
    assert len(eng.state.pending_spotify()) == len(tracks)
    assert not any(v["spotify_added"] for v in eng.state.data["synced"].values())

    # A later genuine add (parked long ago, added this run) is left alone.
    old = tracks[0]
    eng.state.data["synced"][old.id]["spotify_added"] = True
    eng.state.data["synced"][old.id]["added_at"] = "2030-01-01T00:00:00+00:00"
    eng.state.clear_pending_spotify()
    assert eng._repair_parked_bucket() == 0


def test_the_parked_backlog_is_added_before_the_run_can_get_throttled(monkeypatch, cfg):
    """A Spotify slow-down later in the run used to swallow the backlog again."""
    import time as _time

    from phonksync.models import Track

    cfg.youtube_enabled = False
    tracks = _many_tracks(3)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())

    # A track parked by the previous (throttled) run, waiting for a batch add.
    waiting = Track(id="waiting00000000000000001", name="LUZ", artists=["HDN"],
                    uri="spotify:track:waiting00000000000000001")
    eng.state.add_manual(waiting, reason="Spotify asked to slow down")
    eng.state.add_pending_spotify(waiting)
    eng.state.save()

    calls = {"n": 0}

    def add_tracks(playlist_id, uris):
        calls["n"] += 1
        if calls["n"] == 2:  # the batch add worked, the album writes are throttled
            spotify.blocked_until = _time.time() + 3600
            raise RateLimited(3600, "Spotify blocked this session for ~1h 0m")
        spotify.added.extend(uris)

    monkeypatch.setattr(spotify, "add_tracks", add_tracks)
    report = eng.run()

    assert waiting.uri in spotify.added, "the backlog was not flushed before the throttle"
    assert report.parked_saved == 1
    assert waiting.id not in eng.state.pending_spotify()   # done, not re-queued
    assert waiting.id not in eng.state.manual_items()
    assert eng.state.data["synced"][waiting.id]["spotify_added"] is True
    # The tracks this run parked (throttled album writes) wait for the next run.
    assert eng.state.pending_spotify()


def test_the_summary_says_why_spotify_refused(monkeypatch, cfg):
    """Same symptom, three causes: name the one that happened."""
    import time as _time

    cfg.youtube_enabled = False
    cfg.spotify_write_mode = "queue"
    tracks = _many_tracks(2)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})

    def add(playlist_id, uris):
        spotify.blocked_until = _time.time() + 3600
        raise RateLimited(3600, "Spotify blocked this session for ~1h 0m")

    monkeypatch.setattr(spotify, "add_tracks", add)
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    spotify.queue_supported = False  # a non-Premium account
    report = eng.run()
    text = report.summary_text()
    assert "Spotify rate limited (429)" in text
    assert "reason: Spotify blocked this session" in text
    assert "player queue was refused" in text
    assert "SPOTIFY_WRITE_MODE=direct" in text
    assert "pacing 2s/call" in text


def test_a_write_spotify_accepts_but_drops_is_never_marked_added(monkeypatch, cfg):
    """The real failure: the call "worked", the playlist stayed empty."""
    from phonksync.models import Track

    cfg.youtube_enabled = False
    tracks = _many_tracks(2)
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: tracks})
    spotify.verify_added = lambda playlist_id, uris: []  # nothing lands
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())

    waiting = Track(id="ghost000000000000000001", name="LUZ", artists=["HDN"],
                    uri="spotify:track:ghost000000000000000001")
    eng.state.add_manual(waiting, reason="Spotify asked to slow down")
    eng.state.add_pending_spotify(waiting)
    eng.state.save()

    report = eng.run()

    assert waiting.id not in {
        k for k, v in eng.state.data["synced"].items() if v.get("spotify_added")
    }, "the ghost write was recorded as added"
    assert report.spotify_writes_missing >= 1
    assert report.parked_spotify >= 1
    assert "are NOT in the target playlist" in report.summary_text()


def test_a_write_that_really_landed_is_marked_added(monkeypatch, cfg):
    from phonksync.models import Track

    cfg.youtube_enabled = False
    source = _many_tracks(1)
    source[0].uri = f"spotify:track:{source[0].id}"  # the fake reuses one URI
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: source})
    added: list[str] = []

    def add_tracks(playlist_id, uris):
        added.extend(uris)

    spotify.add_tracks = add_tracks
    spotify.verify_added = lambda playlist_id, uris: [u for u in uris if u in added]
    eng = build_engine(monkeypatch, cfg, spotify, EngineYouTube())

    waiting = Track(id="real0000000000000000001", name="LUZ", artists=["HDN"],
                    uri="spotify:track:real0000000000000000001")
    eng.state.add_manual(waiting, reason="Spotify asked to slow down")
    eng.state.add_pending_spotify(waiting)
    eng.state.save()

    report = eng.run()
    assert report.parked_saved == 1
    assert waiting.id not in eng.state.pending_spotify()
    assert eng.state.data["synced"][waiting.id]["spotify_added"] is True
    assert "are NOT in the target playlist" not in report.summary_text()
