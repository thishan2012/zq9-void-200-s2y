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
    def __init__(self, *a, **kw):
        self.messages = []

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
    assert "Skipped (errors): 1" in report.summary_text()


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

    monkeypatch.setattr(eng, "_add_spotify", add_one)
    report = eng.run()
    resume = eng.state.get_resume_point()
    assert resume["track_id"] == tracks[1].id
    assert report.rate_limited_skips == 1
    assert eng.state.spotify_cooldown_until() > time.time()

    # Next run starts after the skipped track instead of from the top.
    spotify.blocked_until = 0.0
    # Simulate the cooldown having expired (normally an hour later).
    eng.state.clear_spotify_cooldown()
    eng.state.save()
    eng2 = build_engine(monkeypatch, cfg, spotify, EngineYouTube())
    report2 = eng2.run()
    assert report2.added == 3  # tracks 2, 3, 4
    assert not eng2.state.get_resume_point()


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


def test_rate_limited_run_records_a_cooldown(monkeypatch, cfg):
    """A run that dies on 429 must leave a cooldown so the next run skips fast."""
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
    assert report.spotify_throttled
    assert engine.state.spotify_cooldown_until() > time.time() + 500


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
    assert engine.state.spotify_cooldown_until() > time.time() + 1000


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
    assert "Search budget ran out for 1" in text
    assert "Free YouTube searches: 40" in text


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
    assert "YT_REFRESH_TOKEN" not in summary.split("Reason")[0]
    assert "csv" not in summary


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
