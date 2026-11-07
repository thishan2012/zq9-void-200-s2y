from conftest import make_candidate, make_track

from phonksync.checks import artist_ok, same_song_title, title_in_title, version_ok


def test_exact_title_match():
    assert same_song_title("MONTAGEM MIAU", "Montagem Miau (Official Video)", 1.0)


def test_title_with_artist_prefix():
    assert same_song_title("SHAKA", "Nulteex - SHAKA", 1.0)


def test_one_word_overlap_rejected():
    # Two multi-word titles sharing only one word = different songs.
    assert not same_song_title("Montagem Miau", "Montagem Lunar", 0.85)


def test_fuzzy_title():
    assert same_song_title("Montagem Miau", "Montagem Miauu", 0.85)
    assert not same_song_title("Montagem Miau", "Completely Other Song", 0.85)


def test_version_blocks_slowed_swap():
    original = make_track(name="Song")
    slowed = make_track(name="Song (Slowed)")
    assert not version_ok(original, "Song (Slowed)")
    assert not version_ok(slowed, "Song")
    assert not version_ok(slowed, "Song (Slowed + Reverb)")  # extra reverb = different signature
    assert version_ok(slowed, "Song (Slowed)")
    assert version_ok(original, "Song (Official Audio)")
    assert version_ok(slowed, "Song - Slowed")


def test_version_blocks_sped_up_and_remix():
    original = make_track(name="Song")
    assert not version_ok(original, "Song (Sped Up)")
    assert not version_ok(original, "Song (Remix)")
    assert not version_ok(original, "Song (Nightcore)")


def test_artist_conflict_blocked():
    track = make_track(name="SHAKA", artists=["Nulteex"])
    wrong = make_candidate("v1", "OtherArtist - SHAKA", channel="OtherArtist")
    assert not artist_ok(track, wrong, strict=True)
    assert not artist_ok(track, wrong, strict=False)  # strong conflict blocks even loose mode


def test_artist_topic_channel_confirms():
    track = make_track(name="SHAKA", artists=["Nulteex"])
    cand = make_candidate("v1", "SHAKA", channel="Nulteex - Topic")
    assert artist_ok(track, cand, strict=True)


def test_promo_channel_alone_does_not_block():
    track = make_track(name="SHAKA", artists=["Nulteex"])
    cand = make_candidate("v1", "SHAKA", channel="Phonk Music")
    assert artist_ok(track, cand, strict=False)
    assert not artist_ok(track, cand, strict=True)


def test_title_in_title():
    assert title_in_title("Montagem Miau", "Montagem Miau (Slowed)")
    assert not title_in_title("Montagem Miau", "Montagem Lunar")


def test_the_write_test_command_exists_and_needs_a_cookie(monkeypatch, capsys):
    """`spotify-write-test` is the command that prints the real GraphQL answer."""
    import phonksync.__main__ as cli

    monkeypatch.delenv("SPOTIFY_COOKIE", raising=False)
    code = cli.main(["spotify-write-test", "--track", "abc"])
    assert code == 2
    assert "SPOTIFY_COOKIE is required" in capsys.readouterr().err
