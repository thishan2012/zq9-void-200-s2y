import pytest

from phonksync.settings import (
    Config,
    ConfigError,
    parse_spotify_playlist_id,
    parse_youtube_playlist_id,
)


def test_parse_spotify_id_forms():
    pid = "37i9dQZF1DXcBWIGoYBM5M"
    assert parse_spotify_playlist_id(pid) == pid
    assert parse_spotify_playlist_id(f"spotify:playlist:{pid}") == pid
    assert parse_spotify_playlist_id(f"https://open.spotify.com/playlist/{pid}?si=abc") == pid


def test_parse_spotify_invalid():
    with pytest.raises(ConfigError):
        parse_spotify_playlist_id("not-a-playlist")
    with pytest.raises(ConfigError):
        parse_spotify_playlist_id("")


def test_parse_youtube_id_forms():
    assert parse_youtube_playlist_id("PLabcdefgh1234567") == "PLabcdefgh1234567"
    assert (
        parse_youtube_playlist_id("https://www.youtube.com/playlist?list=PLabcdefgh1234567")
        == "PLabcdefgh1234567"
    )


def test_config_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFY_SOURCE_PLAYLISTS", "37i9dQZF1DXcBWIGoYBM5M,37i9dQZF1DX0XUsuxWHRQd")
    monkeypatch.setenv("SPOTIFY_TARGET_PLAYLIST", "37i9dQZF1DX4WYpdgoIcn6")
    monkeypatch.setenv("YOUTUBE_TARGET_PLAYLIST", "PLabcdefgh1234567")
    cfg = Config.load(tmp_path / "missing.json")
    assert len(cfg.spotify_source_playlists) == 2
    cfg.validate()


def test_config_rejects_target_as_source(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFY_SOURCE_PLAYLISTS", "37i9dQZF1DXcBWIGoYBM5M")
    monkeypatch.setenv("SPOTIFY_TARGET_PLAYLIST", "37i9dQZF1DXcBWIGoYBM5M")
    monkeypatch.setenv("YOUTUBE_TARGET_PLAYLIST", "PLabcdefgh1234567")
    cfg = Config.load(tmp_path / "missing.json")
    with pytest.raises(ConfigError):
        cfg.validate()
