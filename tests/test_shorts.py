"""Shorts filtering without live network or paid YouTube calls."""
import pytest
import requests

from phonksync.errors import ShortVideoError, ShortsCheckUnavailable
from phonksync.youtube import YouTubeClient
from conftest import FakeYouTube, build_matcher, make_track, make_candidate


class Response:
    def __init__(self, status, location=""):
        self.status_code = status
        self.headers = {"Location": location} if location else {}


class Web:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def head(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def client_for(response):
    web = Web(response)
    client = YouTubeClient(web_session=web, access_token="token", refresh_token="fake")
    client._eligible_durations = {"vid00000001": 120, "vid00000002": 120}
    return client, web


def test_confirmed_short_blocked_and_result_cached():
    client, web = client_for(Response(200))
    for _ in range(2):
        with pytest.raises(ShortVideoError):
            client.ensure_not_short("vid00000001")
    assert len(web.calls) == 1
    assert web.calls[0][1]["allow_redirects"] is False


def test_watch_redirect_is_normal_even_for_a_short_duration():
    client, web = client_for(Response(303, "/watch?v=vid00000001"))
    client.ensure_not_short("vid00000001")
    client.ensure_not_short("vid00000001")
    assert len(web.calls) == 1


@pytest.mark.parametrize("response", [
    Response(302, "https://consent.youtube.com/"),
    Response(302, "/watch?v=DIFFERENT01"),
    Response(302, "https://evil.example/watch?v=vid00000001"),
    Response(403), Response(404), Response(429), Response(500),
    requests.Timeout("unreachable"),
])
def test_unavailable_or_unexpected_result_never_allows_insert(response):
    client, _ = client_for(response)
    with pytest.raises(ShortsCheckUnavailable):
        client.add_to_playlist("PLtest", "vid00000001")
    assert client.quota.inserts == 0


def test_confirmed_short_is_blocked_at_playlist_insert():
    client, _ = client_for(Response(200))
    with pytest.raises(ShortVideoError):
        client.add_to_playlist("PLtest", "vid00000001")
    assert client.quota.inserts == 0


def test_network_outage_is_not_retried_for_every_candidate():
    client, web = client_for(requests.Timeout("down"))
    for vid in ("vid00000001", "vid00000002"):
        with pytest.raises(ShortsCheckUnavailable):
            client.ensure_not_short(vid)
    assert len(web.calls) == 1


def test_matcher_ignores_short_then_finds_full_video(cache, overrides):
    short = make_candidate("vidSHORT001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic", duration_s=20)
    full = make_candidate("vidFULL0001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic", duration_s=120)
    yt = FakeYouTube(catalog={short.video_id: short, full.video_id: full},
                     search_map={"nulteex montagem miau": [short.video_id, full.video_id]})
    def check(video_id):
        if video_id == short.video_id:
            raise ShortVideoError(video_id)
    yt.ensure_not_short = check
    result = build_matcher(yt, cache, overrides).match(make_track())
    assert result and result.candidate.video_id == full.video_id


def test_matcher_short_check_unavailable_is_retryable_not_unmatched(cache, overrides):
    candidate = make_candidate("vidFULL0001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    yt = FakeYouTube(catalog={candidate.video_id: candidate},
                     search_map={"nulteex montagem miau": [candidate.video_id]})
    def check(video_id):
        raise ShortsCheckUnavailable("network blocked")
    yt.ensure_not_short = check
    with pytest.raises(ShortsCheckUnavailable):
        build_matcher(yt, cache, overrides).match(make_track())
    assert len(yt.searches) == 1


@pytest.mark.parametrize("mode", ["manual", "isrc", "linked"])
def test_authoritative_match_paths_cannot_bypass_shorts(cache, tmp_path, mode):
    from phonksync.overrides import Overrides
    import json
    track = make_track()
    desc = "ISRC: BRABC2400001\nhttps://open.spotify.com/track/" + track.id
    candidate = make_candidate("vidSHORT001", "Mystery upload", channel="Unknown", description=desc)
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"overrides": {track.id: candidate.video_id} if mode == "manual" else {}}))
    yt = FakeYouTube(catalog={candidate.video_id: candidate},
                     search_map={"nulteex montagem miau": [candidate.video_id]})
    def check(video_id):
        raise ShortVideoError(video_id)
    yt.ensure_not_short = check
    assert build_matcher(yt, cache, Overrides(path)).match(track) is None


def test_legacy_pending_short_is_cleared_and_researched(monkeypatch, cfg):
    from test_engine import FakeSpotify, EngineYouTube, build_engine
    from phonksync.engine import RunReport
    from phonksync.db import StateDB

    track = make_track(id="track000000000000000001")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]}, target_ids={track.id})
    yt = EngineYouTube()
    def blocked(playlist_id, video_id):
        raise ShortVideoError(video_id)
    yt.add_to_playlist = blocked
    eng = build_engine(monkeypatch, cfg, spotify, yt)
    eng.state.mark_synced(track, "vidSHORT001", "STRICT", True, False)
    eng.state.save()
    eng._flush_pending_youtube(RunReport())
    entry = StateDB(cfg.state_path).get_synced(track.id)
    assert entry["spotify_added"] and not entry["youtube_added"]
    assert entry["youtube_video_id"] == ""
    assert not StateDB(cfg.state_path).pending_youtube()


def test_pending_unknown_short_status_keeps_match_and_reports_problem(monkeypatch, cfg):
    from test_engine import FakeSpotify, EngineYouTube, build_engine
    from phonksync.engine import RunReport

    track = make_track(id="track000000000000000001")
    yt = EngineYouTube()
    def unavailable(playlist_id, video_id):
        raise ShortsCheckUnavailable("network blocked")
    yt.add_to_playlist = unavailable
    eng = build_engine(monkeypatch, cfg, FakeSpotify({}), yt)
    eng.state.mark_synced(track, "vidWAIT0001", "STRICT", True, False)
    report = RunReport()
    eng._flush_pending_youtube(report)
    assert eng.state.pending_youtube()[0]["youtube_video_id"] == "vidWAIT0001"
    assert not eng.state.get_synced(track.id)["youtube_added"]
    assert report.youtube_write_problem == "network blocked"
