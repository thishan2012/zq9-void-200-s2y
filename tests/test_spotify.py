"""Spotify client pagination tests (no network)."""
from phonksync.spotify import SpotifyClient


def _item(i):
    tid = f"track{i:017d}"
    return {
        "is_local": False,
        "track": {
            "id": tid,
            "name": f"Song {i}",
            "type": "track",
            "artists": [{"name": "Solo"}],
            "album": {"name": "Album"},
            "duration_ms": 120000,
            "external_ids": {"isrc": ""},
            "uri": f"spotify:track:{tid}",
            "popularity": 1,
        },
    }


def _page(start, count, next_url=None):
    return {"items": [_item(i) for i in range(start, start + count)], "next": next_url}


def _client(monkeypatch, pages):
    client = SpotifyClient("sp_dc=" + "a" * 60)
    calls = []

    def fake_request(method, path, *, params=None, json_body=None, _retry_auth=True):
        calls.append((method, path, params))
        return pages[len(calls) - 1]

    monkeypatch.setattr(client, "_request", fake_request)
    return client, calls


def test_playlist_tracks_max_tracks_stops_pagination(monkeypatch):
    pages = [
        _page(0, 100, "https://api.spotify.com/v1/playlists/x/tracks?offset=100&limit=100"),
        _page(100, 100, "https://api.spotify.com/v1/playlists/x/tracks?offset=200&limit=100"),
        _page(200, 100, None),
    ]
    client, calls = _client(monkeypatch, pages)
    tracks = client.playlist_tracks("x", max_tracks=150)
    assert len(tracks) == 150
    assert len(calls) == 2
    assert calls[0][2]["limit"] == 100


def test_playlist_tracks_small_max_tracks_uses_small_page(monkeypatch):
    pages = [_page(0, 50, "https://api.spotify.com/v1/playlists/x/tracks?offset=50&limit=50")]
    client, calls = _client(monkeypatch, pages)
    tracks = client.playlist_tracks("x", max_tracks=50)
    assert len(tracks) == 50
    assert len(calls) == 1
    assert calls[0][2]["limit"] == 50


def test_playlist_tracks_full_read_without_limit(monkeypatch):
    pages = [
        _page(0, 100, "https://api.spotify.com/v1/playlists/x/tracks?offset=100&limit=100"),
        _page(100, 5, None),
    ]
    client, calls = _client(monkeypatch, pages)
    tracks = client.playlist_tracks("x")
    assert len(tracks) == 105
    assert len(calls) == 2
    assert calls[0][2]["limit"] == 100
