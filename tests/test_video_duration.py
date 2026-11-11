"""One-minute cutoff applies to all automatic and pending writes."""
import json
import pytest

from conftest import FakeYouTube, build_matcher, make_candidate, make_track
from phonksync.errors import VideoTooShort, VideoDurationUnavailable, ShortVideoError
from phonksync.overrides import Overrides
from test_shorts import Response, client_for


@pytest.mark.parametrize("duration", [1, 20, 59, 60])
def test_cutoff_blocks_60_seconds_or_less_before_shorts_probe(duration):
    client, web = client_for(Response(303, "/watch?v=vid00000001"))
    with pytest.raises(VideoTooShort):
        client.ensure_video_eligible("vid00000001", duration)
    assert not web.calls


def test_61_seconds_regular_video_passes_and_duration_is_reused():
    client, web = client_for(Response(303, "/watch?v=vid00000001"))
    client.ensure_video_eligible("vid00000001", 61)
    client.ensure_video_eligible("vid00000001")
    assert len(web.calls) == 1


def test_longer_youtube_short_is_still_blocked():
    client, _ = client_for(Response(200))
    with pytest.raises(ShortVideoError):
        client.ensure_video_eligible("vid00000001", 120)


def test_missing_duration_fails_closed_without_insert():
    client, _ = client_for(Response(303, "/watch?v=vid00000001"))
    client._eligible_durations.clear()
    client.free_videos = lambda ids: []
    client.videos = lambda ids: []
    with pytest.raises(VideoDurationUnavailable):
        client.add_to_playlist("PLtarget", "vid00000001")
    assert client.quota.inserts == 0


def test_pending_video_duration_is_read_before_writing():
    client, _ = client_for(Response(303, "/watch?v=vid00000001"))
    client._eligible_durations.clear()
    client.free_videos = lambda ids: [make_candidate(ids[0], "song", duration_s=60)]
    with pytest.raises(VideoTooShort):
        client.add_to_playlist("PLtarget", "vid00000001")
    assert client.quota.inserts == 0


def test_missing_free_metadata_can_fall_back_to_api():
    client, _ = client_for(Response(303, "/watch?v=vid00000001"))
    client._eligible_durations.clear()
    client.free_videos = lambda ids: []
    client.videos = lambda ids: [make_candidate(ids[0], "song", duration_s=61)]
    client.ensure_video_eligible("vid00000001")
    assert client._eligible_durations["vid00000001"] == 61


def test_matcher_tries_next_video_when_first_is_exactly_one_minute(cache, overrides):
    clip = make_candidate("vidCLIP0001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic", duration_s=60)
    full = make_candidate("vidFULL0001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic", duration_s=61)
    yt = FakeYouTube(catalog={clip.video_id: clip, full.video_id: full},
                     search_map={"nulteex montagem miau": [clip.video_id, full.video_id]})
    result = build_matcher(yt, cache, overrides).match(make_track())
    assert result and result.candidate.video_id == full.video_id


def test_matcher_unknown_duration_is_retryable(cache, overrides):
    candidate = make_candidate("vidMISSING1", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic", duration_s=0)
    yt = FakeYouTube(catalog={candidate.video_id: candidate},
                     search_map={"nulteex montagem miau": [candidate.video_id]})
    with pytest.raises(VideoDurationUnavailable):
        build_matcher(yt, cache, overrides).match(make_track())


@pytest.mark.parametrize("mode", ["manual", "isrc", "linked"])
def test_cutoff_applies_to_authoritative_match_paths(cache, tmp_path, mode):
    track = make_track()
    desc = "ISRC: BRABC2400001\nhttps://open.spotify.com/track/" + track.id
    candidate = make_candidate("vidCLIP0001", "Mystery upload", description=desc, duration_s=30)
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"overrides": {track.id: candidate.video_id} if mode == "manual" else {}}))
    yt = FakeYouTube(catalog={candidate.video_id: candidate},
                     search_map={"nulteex montagem miau": [candidate.video_id]})
    assert build_matcher(yt, cache, Overrides(path)).match(track) is None


def test_pending_short_duration_is_cleared_without_losing_spotify(monkeypatch, cfg):
    from test_engine import FakeSpotify, EngineYouTube, build_engine
    from phonksync.engine import RunReport
    from phonksync.db import StateDB

    track = make_track(id="track000000000000000001")
    yt = EngineYouTube()
    def blocked(playlist_id, video_id):
        raise VideoTooShort(video_id)
    yt.add_to_playlist = blocked
    eng = build_engine(monkeypatch, cfg, FakeSpotify({}), yt)
    eng.state.mark_synced(track, "vidCLIP0001", "STRICT", True, False)
    eng.state.save()
    eng._flush_pending_youtube(RunReport())
    entry = StateDB(cfg.state_path).get_synced(track.id)
    assert entry["spotify_added"] and not entry["youtube_added"]
    assert entry["youtube_video_id"] == ""


def test_long_short_is_rejected_and_next_eligible_video_wins(cache, overrides):
    short = make_candidate("vidSHORT001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic", duration_s=90)
    full = make_candidate("vidFULL0001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic", duration_s=120)
    yt = FakeYouTube(catalog={short.video_id: short, full.video_id: full},
                     search_map={"nulteex montagem miau": [short.video_id, full.video_id]})
    def check(video_id):
        if video_id == short.video_id:
            raise ShortVideoError(video_id)
    yt.ensure_not_short = check
    result = build_matcher(yt, cache, overrides).match(make_track())
    assert result and result.candidate.video_id == full.video_id


def test_unknown_pending_duration_stays_saved_and_reported(monkeypatch, cfg):
    from test_engine import FakeSpotify, EngineYouTube, build_engine
    from phonksync.engine import RunReport

    track = make_track(id="track000000000000000001")
    yt = EngineYouTube()
    def unavailable(playlist_id, video_id):
        raise VideoDurationUnavailable("duration unavailable")
    yt.add_to_playlist = unavailable
    eng = build_engine(monkeypatch, cfg, FakeSpotify({}), yt)
    eng.state.mark_synced(track, "vidWAIT0001", "STRICT", True, False)
    report = RunReport()
    eng._flush_pending_youtube(report)
    assert eng.state.pending_youtube()[0]["youtube_video_id"] == "vidWAIT0001"
    assert report.youtube_write_problem == "duration unavailable"
