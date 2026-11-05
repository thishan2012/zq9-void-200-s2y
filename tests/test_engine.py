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
