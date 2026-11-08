"""One song, one version — and the two playlists growing together.

Two source playlists overlap: playlist 1 carries "Song - Slowed", playlist 2
carries "Song - Ultra Slowed". They are the same song, so only the FIRST one
found belongs in the target. The matcher's own version checks stay precise:
grouping is a playlist decision, not a matching decision.
"""
import pytest

from phonksync.normalize import extract_version, song_family
from phonksync.errors import QuotaExceeded

from conftest import make_candidate, make_track
from test_engine import EngineYouTube, FakeSpotify, build_engine


# --------------------------------------------------------------- family key

@pytest.mark.parametrize("a,b", [
    ("MONTAGEM MANADO - Slowed", "MONTAGEM MANADO - Ultra Slowed"),
    ("SHAKA - Slowed", "SHAKA - Super Slowed"),
    ("FUNK MISTERIO - Slowed + Reverb", "FUNK MISTERIO - Slowed"),
    ("MALEMO - Sped Up", "MALEMO - Ultra Sped Up"),
    ("MONTAGEM BREAKDOWN (Slowed & Reverb)", "MONTAGEM BREAKDOWN"),
    ("Song - Slowed and Reverb", "Song - Slowed"),
    ("Song - Extremely Slowed", "Song - Slowed"),
    ("Song (feat. X) - Slowed", "Song - Slowed"),
    ("ULTRA", "ULTRA - Slowed"),
])
def test_versions_of_one_song_share_a_family(a, b):
    assert song_family(a, ["DJ Artista"]) == song_family(b, ["DJ Artista"])


@pytest.mark.parametrize("a,aa,b,bb", [
    ("Song - Slowed", ["Artist A"], "Song - Slowed", ["Artist B"]),
    ("MONTAGEM MANADO", ["A"], "MONTAGEM MANADO 2", ["A"]),
    ("Me and You", ["A"], "Me", ["A"]),
    ("NIGHT DRIVE", ["A"], "NIGHT DRIFT", ["A"]),
])
def test_different_songs_never_share_a_family(a, aa, b, bb):
    assert song_family(a, aa) != song_family(b, bb)


def test_grouping_does_not_blur_the_matchers_version_signature():
    """version_ok() must still tell Slowed from Slowed + Reverb."""
    assert extract_version("AWE - Slowed") == frozenset({"slowed"})
    assert extract_version("AWE - Slowed + Reverb") == frozenset({"slowed", "reverb"})
    assert extract_version("AWE - Ultra Slowed") == frozenset({"slowed"})


# ------------------------------------------------------------------- engine

def _two_version_tracks():
    slowed = make_track(
        id="v" * 22, name="MONTAGEM MANADO - Slowed",
        artists=["Nulteex"], isrc="BRABC2400010",
        uri=f"spotify:track:{'v' * 22}",
    )
    ultra = make_track(
        id="w" * 22, name="MONTAGEM MANADO - Ultra Slowed",
        artists=["Nulteex"], isrc="BRABC2400011",
        uri=f"spotify:track:{'w' * 22}",
    )
    return slowed, ultra


def _youtube_for(name, vid):
    good = make_candidate(vid, f"Nulteex - {name}", channel="Nulteex - Topic")
    return EngineYouTube(
        catalog={vid: good},
        search_map={f"nulteex {name.lower()}": [vid]},
    )


def test_the_second_source_playlists_version_is_not_added(monkeypatch, cfg):
    """The user's exact scenario, across two source playlists in ONE run."""
    slowed, ultra = _two_version_tracks()
    cfg.spotify_source_playlists = ["src111111111111111111111", "src222222222222222222222"]
    spotify = FakeSpotify({
        "src111111111111111111111": [slowed],
        "src222222222222222222222": [ultra],
    })
    youtube = _youtube_for("MONTAGEM MANADO - Slowed", "vidMANADO01")
    report = build_engine(monkeypatch, cfg, spotify, youtube).run()

    assert report.added == 1
    assert report.version_duplicates == 1
    assert spotify.added == [slowed.uri], "the Ultra Slowed version reached Spotify"
    assert youtube.inserted == ["vidMANADO01"], "a second version reached YouTube"


def test_the_first_version_wins_and_the_decision_survives_the_next_run(monkeypatch, cfg):
    slowed, ultra = _two_version_tracks()
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [slowed]})
    youtube = _youtube_for("MONTAGEM MANADO - Slowed", "vidMANADO01")
    first = build_engine(monkeypatch, cfg, spotify, youtube)
    first.run()
    state_path = cfg.state_path

    # Run 2: the user adds a NEW source playlist, and it carries the Ultra
    # Slowed version. (A new playlist id, so run 1's source cache is not reused.)
    cfg.spotify_source_playlists = [
        cfg.spotify_source_playlists[0], "src222222222222222222222"
    ]
    spotify2 = FakeSpotify({
        cfg.spotify_source_playlists[0]: [slowed],
        "src222222222222222222222": [ultra],
    })
    youtube2 = _youtube_for("MONTAGEM MANADO - Ultra Slowed", "vidMANADO02")
    cfg.state_path = state_path
    second = build_engine(monkeypatch, cfg, spotify2, youtube2)
    report = second.run()

    assert report.version_duplicates == 1
    assert spotify2.added == [], "the later version was added on the second run"
    assert youtube2.inserted == []
    kept = second.state.version_duplicate_entry(ultra.id)
    assert kept["kept_spotify_id"] == slowed.id


def test_a_version_duplicate_costs_no_youtube_search(monkeypatch, cfg):
    """The decision is made before matching, so it burns no quota."""
    slowed, ultra = _two_version_tracks()
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [slowed, ultra]})
    youtube = _youtube_for("MONTAGEM MANADO - Slowed", "vidMANADO01")
    engine = build_engine(monkeypatch, cfg, spotify, youtube)

    searched = []
    real_match = engine.matcher.match

    def spy(track):
        searched.append(track.id)
        return real_match(track)

    engine.matcher.match = spy
    engine.run()
    assert searched == [slowed.id], "the duplicate version was searched anyway"


def test_turning_the_check_off_lets_both_versions_through(monkeypatch, cfg):
    slowed, ultra = _two_version_tracks()
    cfg.dedupe_versions = False
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [slowed, ultra]})
    youtube = EngineYouTube(
        catalog={
            "vidMANADO01": make_candidate(
                "vidMANADO01", "Nulteex - MONTAGEM MANADO - Slowed",
                channel="Nulteex - Topic"),
            "vidMANADO02": make_candidate(
                "vidMANADO02", "Nulteex - MONTAGEM MANADO - Ultra Slowed",
                channel="Nulteex - Topic"),
        },
        search_map={
            "nulteex montagem manado - slowed": ["vidMANADO01"],
            "nulteex montagem manado - ultra slowed": ["vidMANADO02"],
        },
    )
    report = build_engine(monkeypatch, cfg, spotify, youtube).run()
    assert report.version_duplicates == 0
    assert len(spotify.added) == 2


def test_reset_versions_makes_the_other_version_eligible_again(monkeypatch, cfg, tmp_path):
    from phonksync.db import StateDB

    slowed, ultra = _two_version_tracks()
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [slowed, ultra]})
    youtube = _youtube_for("MONTAGEM MANADO - Slowed", "vidMANADO01")
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    engine.run()
    assert engine.state.is_version_duplicate(ultra.id)

    fresh = StateDB(cfg.state_path)
    cleared = fresh.reset(["versions"])
    fresh.save()
    assert cleared["versions"] == 1
    assert not StateDB(cfg.state_path).is_version_duplicate(ultra.id)


# ------------------------------------------------------------------- parity

def test_youtube_is_written_before_spotify(monkeypatch, cfg):
    """A YouTube budget stop must not leave Spotify one song ahead."""
    track = make_track(id="z" * 22, uri=f"spotify:track:{'z' * 22}")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = _youtube_for(track.name, "vidORDER001")

    order = []
    real_yt = youtube.add_to_playlist
    real_sp = spotify.add_tracks

    def yt(*a, **kw):
        order.append("youtube")
        return real_yt(*a, **kw)

    def sp(*a, **kw):
        order.append("spotify")
        return real_sp(*a, **kw)

    youtube.add_to_playlist = yt
    spotify.add_tracks = sp
    build_engine(monkeypatch, cfg, spotify, youtube).run()
    assert order == ["youtube", "spotify"]


def test_a_youtube_quota_stop_adds_nothing_to_spotify(monkeypatch, cfg):
    track = make_track(id="y" * 22, uri=f"spotify:track:{'y' * 22}")
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = _youtube_for(track.name, "vidQUOTA001")

    def refuse(*a, **kw):
        raise QuotaExceeded("Daily YouTube insert budget reached (40)")

    youtube.add_to_playlist = refuse
    report = build_engine(monkeypatch, cfg, spotify, youtube).run()

    assert spotify.added == [], "Spotify ran ahead of the YouTube budget"
    assert report.quota_stopped


def test_a_song_missing_from_youtube_still_reaches_spotify(monkeypatch, cfg):
    """The user's choice: Spotify completeness beats exact count parity."""
    track = make_track(id="x" * 22, name="OBSCURE PHONK", uri=f"spotify:track:{'x' * 22}")
    cfg.require_youtube_match_for_spotify = False
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [track]})
    youtube = EngineYouTube()          # nothing matches
    report = build_engine(monkeypatch, cfg, spotify, youtube).run()

    assert spotify.added == [track.uri]
    assert youtube.inserted == []
    assert report.unmatched == 1


# ------------------------------------------------- the browser-readable list

def test_the_skipped_versions_are_listed_in_a_committed_file(monkeypatch, cfg):
    slowed, ultra = _two_version_tracks()
    spotify = FakeSpotify({cfg.spotify_source_playlists[0]: [slowed, ultra]})
    youtube = _youtube_for("MONTAGEM MANADO - Slowed", "vidMANADO01")
    engine = build_engine(monkeypatch, cfg, spotify, youtube)
    engine.run()

    from pathlib import Path
    text = Path(cfg.version_queue_path).read_text(encoding="utf-8")
    assert "Ultra Slowed" in text
    assert "kept instead: MONTAGEM MANADO - Slowed" in text
    assert ultra.id in text


def test_an_emptied_bucket_rewrites_the_list(monkeypatch, cfg, tmp_path):
    """A stale list would read as a problem that no longer exists."""
    from pathlib import Path
    from phonksync.db import StateDB

    db = StateDB(cfg.state_path)
    kept = {"spotify_id": "a" * 22, "name": "Song - Slowed"}
    db.mark_version_duplicate(make_track(id="b" * 22, name="Song - Ultra Slowed"), kept)
    assert db.write_version_queue(cfg.version_queue_path) == 1
    assert "Ultra Slowed" in Path(cfg.version_queue_path).read_text(encoding="utf-8")

    db.reset(["versions"])
    assert db.write_version_queue(cfg.version_queue_path) == 0
    text = Path(cfg.version_queue_path).read_text(encoding="utf-8")
    assert "No duplicate versions" in text
    assert "Ultra Slowed" not in text


def test_the_workflow_commits_the_version_list():
    import yaml
    from pathlib import Path

    wf = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/sync.yml")
        .read_text(encoding="utf-8")
    )
    steps = {s["name"]: s for s in wf["jobs"]["sync"]["steps"] if "name" in s}
    assert "data/version-duplicates.txt" in steps["Commit sync state"]["run"]
    assert "versions" in wf[True]["workflow_dispatch"]["inputs"]["reset"]["description"]
