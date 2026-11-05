"""End-to-end engine tests with fake Spotify/YouTube/Telegram (no network)."""
import json

import pytest
from conftest import FakeYouTube, make_candidate, make_track

from phonksync import engine as engine_mod
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
