import pytest

from phonksync.settings import (
    Config,
    ConfigError,
    Secrets,
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


def test_spotify_only_config_and_secrets_do_not_require_youtube(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFY_SOURCE_PLAYLISTS", "37i9dQZF1DXcBWIGoYBM5M")
    monkeypatch.setenv("SPOTIFY_TARGET_PLAYLIST", "37i9dQZF1DX4WYpdgoIcn6")
    monkeypatch.delenv("YOUTUBE_ENABLED", raising=False)
    monkeypatch.delenv("YOUTUBE_TARGET_PLAYLIST", raising=False)
    cfg = Config.load(tmp_path / "missing.json")
    assert cfg.youtube_enabled is False
    cfg.validate()
    secrets = Secrets(spotify_cookie="sp_dc=fake")
    secrets.validate(require_youtube=cfg.youtube_enabled)


def test_youtube_enabled_requires_target_and_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFY_SOURCE_PLAYLISTS", "37i9dQZF1DXcBWIGoYBM5M")
    monkeypatch.setenv("SPOTIFY_TARGET_PLAYLIST", "37i9dQZF1DX4WYpdgoIcn6")
    monkeypatch.setenv("YOUTUBE_ENABLED", "true")
    monkeypatch.delenv("YOUTUBE_TARGET_PLAYLIST", raising=False)
    cfg = Config.load(tmp_path / "missing.json")
    assert cfg.youtube_enabled is True
    with pytest.raises(ConfigError):
        cfg.validate()
    secrets = Secrets(spotify_cookie="sp_dc=fake")
    with pytest.raises(ConfigError):
        secrets.validate(require_youtube=True)


def test_a_phone_share_link_yields_the_plain_id():
    """What the Spotify mobile app copies: locale path + tracking parameters."""
    from phonksync.settings import parse_spotify_playlist_id

    shared = (
        "https://open.spotify.com/playlist/6YBLs3ihGXHCSAESWGRML4"
        "?si=EZxh2vL8RTqk4TgbEvMIxQ&utm_source=copy-link&pi=8mRhH-0OS1-df"
    )
    assert parse_spotify_playlist_id(shared) == "6YBLs3ihGXHCSAESWGRML4"
    assert parse_spotify_playlist_id(shared.replace("&", "&amp;")) == (
        "6YBLs3ihGXHCSAESWGRML4"
    )
    assert parse_spotify_playlist_id(
        "https://open.spotify.com/intl-de/playlist/6YBLs3ihGXHCSAESWGRML4?si=x"
    ) == "6YBLs3ihGXHCSAESWGRML4"
    assert parse_spotify_playlist_id(
        "https%3A%2F%2Fopen.spotify.com%2Fplaylist%2F6YBLs3ihGXHCSAESWGRML4"
    ) == "6YBLs3ihGXHCSAESWGRML4"
    assert parse_spotify_playlist_id("spotify:playlist:6YBLs3ihGXHCSAESWGRML4") == (
        "6YBLs3ihGXHCSAESWGRML4"
    )


def test_a_track_or_share_link_is_refused_not_misread():
    from phonksync.errors import ConfigError
    from phonksync.settings import parse_spotify_playlist_id

    with pytest.raises(ConfigError) as track:
        parse_spotify_playlist_id(
            "https://open.spotify.com/track/6YBLs3ihGXHCSAESWGRML4?si=x"
        )
    assert "not a playlist link" in str(track.value)
    with pytest.raises(ConfigError) as share:
        parse_spotify_playlist_id("https://spotify.link/AbCdEf123")
    assert "share" in str(share.value)
