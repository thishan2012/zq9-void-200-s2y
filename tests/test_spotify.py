"""Spotify client pagination + cookie parsing tests (no network)."""
import pytest

from phonksync.errors import AuthError
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


# ---------------------------------------------------------------- sp_dc parsing

TOKEN = "AQBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdefg"


def test_extract_sp_dc_plain_header():
    from phonksync.spotify import extract_sp_dc

    assert extract_sp_dc(f"sp_t=abc; sp_dc={TOKEN}; sp_key=1") == TOKEN


def test_extract_sp_dc_webview_multiline_dump():
    """Real-world report: '# WebView ...' + one 'name=value;' per line."""
    from phonksync.spotify import extract_sp_dc

    raw = (
        "# WebView android-webkit\n"
        "__Host-device_id=AQDeviceIdValue;\n"
        "sp_t=abc123def456;\n"
        f"sp_dc={TOKEN};\n"
        "sp_key=xyz\n"
    )
    assert extract_sp_dc(raw) == TOKEN


def test_extract_sp_dc_devtools_copy_request_headers():
    from phonksync.spotify import extract_sp_dc

    raw = f"cookie: sp_t=abc; sp_dc={TOKEN};\n  _ga=GA1.2.3.4\n"
    assert extract_sp_dc(raw) == TOKEN


def test_extract_sp_dc_netscape_tab_separated():
    from phonksync.spotify import extract_sp_dc

    raw = f".spotify.com\tTRUE\t/\tTRUE\t1800000000\tsp_dc\t{TOKEN}\n"
    assert extract_sp_dc(raw) == TOKEN


def test_extract_sp_dc_json_exports():
    from phonksync.spotify import extract_sp_dc

    assert extract_sp_dc('{"sp_dc": "%s", "sp_t": "x"}' % TOKEN) == TOKEN
    assert extract_sp_dc('[{"name":"sp_dc","value":"%s"}]' % TOKEN) == TOKEN


def test_extract_sp_dc_bare_value_and_quotes():
    from phonksync.spotify import extract_sp_dc

    assert extract_sp_dc(TOKEN) == TOKEN
    assert extract_sp_dc(f'"sp_dc={TOKEN}; sp_t=x"') == TOKEN
    assert extract_sp_dc(f"`sp_dc={TOKEN}`") == TOKEN
    assert extract_sp_dc(f'sp_dc = "{TOKEN}" ; sp_t=1') == TOKEN
    assert extract_sp_dc(f"Cookie: sp_t=x\r\nsp_dc={TOKEN}\r\n") == TOKEN


def test_extract_sp_dc_empty_value_explains_what_is_wrong():
    from phonksync.spotify import extract_sp_dc

    with pytest.raises(AuthError) as exc:
        extract_sp_dc("# WebView\nsp_t=abc\nsp_dc=\n")
    msg = str(exc.value)
    assert "sp_dc" in msg
    assert "sp_t" in msg  # names found are listed for debugging


def test_extract_sp_dc_missing_cookie_names_the_problem():
    from phonksync.spotify import extract_sp_dc

    with pytest.raises(AuthError) as exc:
        extract_sp_dc("sp_t=abc; sp_key=1")
    assert "does not contain a 'sp_dc' cookie" in str(exc.value)
