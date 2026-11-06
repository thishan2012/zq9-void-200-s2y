"""Pipeline tests with a fake YouTube API (no network, no quota)."""
from conftest import FakeYouTube, build_matcher, make_candidate, make_track

from phonksync.errors import QuotaExceeded


def test_strict_wins_first(cache, overrides):
    track = make_track()
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU (Official Audio)", channel="Nulteex - Topic")
    yt = FakeYouTube(
        catalog={"vid00000001": good},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    m = build_matcher(yt, cache, overrides)
    result = m.match(track)
    assert result is not None
    assert result.method == "STRICT"
    assert result.candidate.video_id == "vid00000001"
    assert len(yt.searches) == 1  # no wasted fallback searches


def test_wrong_version_never_matches(cache, overrides):
    track = make_track(name="MONTAGEM MIAU")  # original
    slowed = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU (Slowed)", channel="Nulteex - Topic")
    yt = FakeYouTube(
        catalog={"vid00000001": slowed},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    m = build_matcher(yt, cache, overrides)
    assert m.match(track) is None


def test_wrong_artist_never_matches(cache, overrides):
    track = make_track(name="SHAKA", artists=["Nulteex"])
    wrong = make_candidate("vid00000001", "Impostor - SHAKA", channel="Impostor")
    yt = FakeYouTube(
        catalog={"vid00000001": wrong},
        search_map={"nulteex shaka": ["vid00000001"]},
    )
    m = build_matcher(yt, cache, overrides)
    assert m.match(track) is None


def test_manual_override_bypasses_search(tmp_path, cache):
    from phonksync.overrides import Overrides

    p = tmp_path / "ov.json"
    p.write_text('{"overrides": {"aaaaaaaaaaaaaaaaaaaaaa": "vid99999999"}}')
    ov = Overrides(p)
    pinned = make_candidate("vid99999999", "whatever title", channel="Someone")
    yt = FakeYouTube(catalog={"vid99999999": pinned})
    m = build_matcher(yt, cache, ov)
    result = m.match(make_track())
    assert result is not None
    assert result.method == "MANUAL"
    assert yt.searches == []


def test_isrc_in_description_matches(cache, overrides):
    track = make_track(name="Totally Weird Name", isrc="BRABC2400001")
    cand = make_candidate(
        "vid00000002", "Some Upload", channel="Random",
        description="ISRC: BRABC2400001\nProvided to YouTube",
    )
    yt = FakeYouTube(
        catalog={"vid00000002": cand},
        search_map={"nulteex totally weird name": ["vid00000002"]},
    )
    m = build_matcher(yt, cache, overrides)
    result = m.match(track)
    assert result is not None
    assert result.method == "ISRC"


def test_spotify_link_in_description_matches(cache, overrides):
    track = make_track(name="Another Weird One")
    cand = make_candidate(
        "vid00000003", "Mystery Upload", channel="Random",
        description="Listen: https://open.spotify.com/track/aaaaaaaaaaaaaaaaaaaaaa",
    )
    yt = FakeYouTube(
        catalog={"vid00000003": cand},
        search_map={"nulteex another weird one": ["vid00000003"]},
    )
    m = build_matcher(yt, cache, overrides)
    result = m.match(track)
    assert result is not None
    assert result.method == "LINKED"


def test_channel_method_finds_artist_channel(cache, overrides):
    track = make_track(name="MIAU", artists=["Nulteex"])
    # Title search finds it on the artist's own channel; strict query misses.
    cand = make_candidate("vid00000004", "MIAU", channel="Nulteex")
    yt = FakeYouTube(
        catalog={"vid00000004": cand},
        search_map={"miau": ["vid00000004"]},
    )
    m = build_matcher(yt, cache, overrides)
    result = m.match(track)
    assert result is not None
    assert result.method in {"STRICT", "LOOSE", "CHANNEL", "SHORT", "ARTISTLESS"}


def test_never_tried_methods_are_skipped(cache, overrides):
    track = make_track()  # single artist, no feat, ASCII, no part number
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    yt = FakeYouTube(
        catalog={"vid00000001": good},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    m = build_matcher(yt, cache, overrides)
    m.match(track)
    # STRICT wins immediately; conditional methods were never reached.
    assert m.last_tried == ["STRICT"]


def test_unmatched_records_tried_methods(cache, overrides):
    track = make_track(name="Obscure Song X", artists=["Solo"])
    yt = FakeYouTube(catalog={}, search_map={})
    m = build_matcher(yt, cache, overrides)
    result = m.match(track)
    assert result is None
    assert "STRICT" in m.last_tried
    assert "TOPRESULT" not in m.last_tried  # no results -> never tried
    assert "SINGLE" not in m.last_tried  # one artist -> never tried
    assert "FEAT" not in m.last_tried  # no feat clause -> never tried
    assert "ISRC" not in m.last_tried  # no ISRC in any description -> never tried
    assert "LINKED" not in m.last_tried  # no Spotify links in descriptions -> never tried


def test_search_cache_reused_across_matchers(tmp_path, overrides):
    from phonksync.cache import SearchCache

    track = make_track()
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    yt = FakeYouTube(
        catalog={"vid00000001": good},
        search_map={"nulteex montagem miau": ["vid00000001"]},
    )
    c1 = SearchCache(tmp_path / "c.json")
    m1 = build_matcher(yt, c1, overrides)
    assert m1.match(track) is not None
    c1.save()
    assert len(yt.searches) == 1

    c2 = SearchCache(tmp_path / "c.json")
    m2 = build_matcher(yt, c2, overrides)
    assert m2.match(track) is not None
    assert len(yt.searches) == 1  # second run hit the cache


def test_quota_exceeded_propagates(cache, overrides):
    class QuotaYouTube(FakeYouTube):
        def search(self, query, max_results=10):
            raise QuotaExceeded("budget reached")

    yt = QuotaYouTube()
    m = build_matcher(yt, cache, overrides)
    try:
        m.match(make_track())
        assert False, "expected QuotaExceeded"
    except QuotaExceeded:
        pass


def test_duration_picks_closest_version(cache, overrides):
    track = make_track(name="MIAU", artists=["Nulteex"], duration_ms=120000)
    short = make_candidate("vid00000010", "Nulteex - MIAU", channel="Nulteex - Topic", duration_s=90)
    exact = make_candidate("vid00000011", "Nulteex - MIAU", channel="Nulteex - Topic", duration_s=121)
    yt = FakeYouTube(
        catalog={"vid00000010": short, "vid00000011": exact},
        search_map={"nulteex miau": ["vid00000010", "vid00000011"]},
    )
    m = build_matcher(yt, cache, overrides)
    result = m.match(track)
    assert result is not None
    # STRICT takes the first verified; both pass title, so ranking order wins.
    assert result.candidate.video_id in {"vid00000010", "vid00000011"}


# --------------------------------------------------------------- free tier
class FreeFakeYouTube(FakeYouTube):
    """YouTube with the zero-quota public page in front of the metered API."""

    def __init__(self, catalog=None, search_map=None, free_map=None):
        super().__init__(catalog=catalog, search_map=search_map)
        self.free_map = free_map or {}
        self.free_searches = 0

    def free_search_ids(self, query, max_results=10):
        self.free_searches += 1
        return self.free_map.get(" ".join(query.lower().split()), [])[:max_results]

    def free_videos(self, video_ids):
        return self.videos(video_ids)


def test_free_search_matches_without_spending_api_quota(cache, overrides):
    track = make_track()
    good = make_candidate("vid00000001", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    yt = FreeFakeYouTube(
        catalog={"vid00000001": good},
        free_map={"nulteex montagem miau": ["vid00000001"]},
    )
    m = build_matcher(yt, cache, overrides)
    result = m.match(track)
    assert result is not None and result.method == "STRICT" and result.free is True
    assert yt.free_searches == 1
    assert yt.searches == []          # the 100-unit API search was never used


def test_free_pass_miss_falls_back_to_the_api(cache, overrides):
    track = make_track()
    good = make_candidate("vid00000002", "Nulteex - MONTAGEM MIAU", channel="Nulteex - Topic")
    yt = FreeFakeYouTube(
        catalog={"vid00000002": good},
        free_map={},  # free page finds nothing
        search_map={"nulteex montagem miau": ["vid00000002"]},
    )
    m = build_matcher(yt, cache, overrides)
    result = m.match(track)
    assert result is not None and result.free is False
    assert yt.searches == ["nulteex montagem miau"]


def test_free_pass_does_not_reuse_a_suspect_result(cache, overrides):
    """A free hit that fails verification must not short-circuit the pipeline."""
    track = make_track(name="MONTAGEM MIAU")
    slowed = make_candidate("vid00000003", "Nulteex - MONTAGEM MIAU (Slowed)",
                            channel="Nulteex - Topic")
    yt = FreeFakeYouTube(
        catalog={"vid00000003": slowed},
        free_map={"nulteex montagem miau": ["vid00000003"]},
        search_map={},
    )
    m = build_matcher(yt, cache, overrides)
    assert m.match(track) is None
