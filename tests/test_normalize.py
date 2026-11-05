from phonksync.normalize import (
    base_title,
    channel_artist,
    extract_version,
    fold,
    has_non_ascii,
    part_number,
    split_feat,
    strip_marketing,
)


def test_fold_accents():
    assert fold("SALÃO") == "salao"
    assert fold("Ünïcödé Tëst") == "unicode test"


def test_fold_special_chars():
    assert fold("Ø") == "o"
    assert fold("Straße") == "strasse"


def test_base_title_removes_noise():
    assert base_title("MONTAGEM MIAU (Official Video)") == "montagem miau"
    assert base_title("Montagem Miau [Lyrics]") == "montagem miau"
    assert base_title("Montagem Miau HD") == "montagem miau"


def test_base_title_removes_version_and_feat():
    assert base_title("Song (feat. X) (Slowed)") == "song"
    assert base_title("Song - Sped Up") == "song"


def test_extract_version():
    assert extract_version("X (Slowed)") == {"slowed"}
    assert extract_version("X (Sped Up)") == {"sped_up"}
    assert extract_version("X (Slowed + Reverb)") == {"slowed", "reverb"}
    assert extract_version("X (Nightcore)") == {"nightcore"}
    assert extract_version("X (Remix)") == {"remix"}
    assert extract_version("X") == frozenset()


def test_split_feat():
    main, feat = split_feat("Song (feat. Alpha, Beta)")
    assert main == "Song"
    assert feat == ["Alpha", "Beta"]


def test_channel_artist_topic():
    assert channel_artist("Nulteex - Topic") == "Nulteex"
    assert channel_artist("Phonk Music") == "Phonk Music"


def test_part_number():
    assert part_number("Song Pt. 2") == "2"
    assert part_number("Song Part 3") == "3"
    assert part_number("Song") is None


def test_has_non_ascii():
    assert has_non_ascii("SALÃO")
    assert not has_non_ascii("SALAO")


def test_strip_marketing_phrases():
    assert "official video" not in strip_marketing("Song Official Video")
    assert strip_marketing("Song [Official Audio]") == "song"
