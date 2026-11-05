"""Spotify client pagination + cookie parsing tests (no network)."""
import time

import pytest

from phonksync.errors import AuthError, RateLimited
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
    # 50 per page now (gentler on the rate window) -> 3 pages, then stop.
    assert len(calls) == 2  # the fake page always returns 100 items
    assert calls[0][2]["limit"] == 50


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
    assert calls[0][2]["limit"] == 50


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


# ---------------------------------------------------------------- TOTP (web player)

from phonksync.spotify import (  # noqa: E402
    _TOTP_SECRETS,
    _totp_code,
    _totp_secret,
    _totp_version,
)


def test_totp_secret_matches_web_player_bundle():
    """v61 cipher must decode to the base32 key the web player uses."""
    assert _totp_secret(_TOTP_SECRETS["61"]) == (
        "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRR"
        "GA2TCMJSHE3TCMBY"
    )


def test_totp_code_is_6_digits_and_time_based():
    secret = _totp_secret(_TOTP_SECRETS["61"])
    code = _totp_code(secret, 1770000000)
    assert len(code) == 6 and code.isdigit()
    # same 30s window -> same code, next window -> different code
    assert _totp_code(secret, 1770000010) == code
    assert _totp_code(secret, 1770000030) != code


def test_totp_version_prefers_env(monkeypatch):
    monkeypatch.setenv("SPOTIFY_TOTP_VER", "60")
    assert _totp_version() == "60"
    monkeypatch.delenv("SPOTIFY_TOTP_VER")
    assert _totp_version() == max(_TOTP_SECRETS, key=int)


def test_cipher_override_from_env(monkeypatch):
    from phonksync.spotify import _apply_cipher_override

    monkeypatch.setenv("SPOTIFY_TOTP_VER", "99")
    monkeypatch.setenv("SPOTIFY_TOTP_CIPHER", "1,2,3,4,5,6,7,8,9,10")
    _apply_cipher_override()
    assert _TOTP_SECRETS["99"] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


class _TokResp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class _TokSession:
    """Fake session: token endpoint fails until the TOTP flow is used."""

    def __init__(self, totp_required=True):
        self.totp_required = totp_required
        self.calls = []
        self.headers = {}

    def head(self, url, timeout=None):
        return _TokResp(headers={"Date": "Wed, 04 Feb 2026 10:00:00 GMT"})

    def get(self, url, params=None, headers=None, cookies=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if url.endswith("/api/token"):
            if self.totp_required and not (params or {}).get("totp"):
                return _TokResp(403, {"error": {"status": 403, "message": "invalid token"}})
            return _TokResp(
                200,
                {
                    "accessToken": "WEBTOKEN",
                    "accessTokenExpirationTimestampMs": 9999999999999,
                    "isAnonymous": False,
                },
            )
        return _TokResp(403, {})


def test_token_flow_sends_totp_to_api_token():
    from phonksync.spotify import SpotifyClient

    session = _TokSession()
    client = SpotifyClient("sp_dc=" + "a" * 60, session=session)
    assert client._ensure_token() == "WEBTOKEN"
    first = [c for c in session.calls if c["url"].endswith("/api/token")][0]
    assert first["params"]["totp"] and first["params"]["totpServer"]
    assert first["params"]["productType"] == "web-player"


def test_token_flow_falls_back_to_legacy_endpoint():
    from phonksync.spotify import SpotifyClient

    class Session(_TokSession):
        def get(self, url, params=None, headers=None, cookies=None, timeout=None):
            self.calls.append({"url": url, "params": dict(params or {})})
            if "get_access_token" in url:
                return _TokResp(200, {"accessToken": "LEGACY", "isAnonymous": False})
            return _TokResp(403, {"error": {"message": "invalid token"}})

    session = Session()
    client = SpotifyClient("sp_dc=" + "a" * 60, session=session)
    assert client._ensure_token() == "LEGACY"
    assert any("get_access_token" in c["url"] for c in session.calls)


def test_anonymous_token_raises_auth_error():
    from phonksync.spotify import SpotifyClient

    class Session(_TokSession):
        def get(self, url, params=None, headers=None, cookies=None, timeout=None):
            self.calls.append({"url": url, "params": dict(params or {})})
            return _TokResp(200, {"accessToken": "ANON", "isAnonymous": True})

    client = SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    with pytest.raises(AuthError) as exc:
        client._ensure_token()
    assert "anonymous" in str(exc.value).lower()


def test_token_flow_error_mentions_help(monkeypatch):
    from phonksync.spotify import SpotifyClient

    monkeypatch.setenv("SPOTIFY_TOTP_REMOTE", "0")

    class Session(_TokSession):
        def get(self, url, params=None, headers=None, cookies=None, timeout=None):
            self.calls.append({"url": url, "params": dict(params or {})})
            return _TokResp(403, {"error": {"message": "invalid token"}})

    client = SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    with pytest.raises(AuthError) as exc:
        client._ensure_token()
    msg = str(exc.value)
    assert "SPOTIFY_TOTP_VER" in msg and "SPOTIFY_COOKIE" in msg


# ------------------------------------------------------------- rate limiting


def test_429_is_retried_with_retry_after(monkeypatch):
    import phonksync.spotify as sp

    slept = []
    monkeypatch.setattr(sp.time, "sleep", lambda s: slept.append(s))

    class Resp:
        def __init__(self, code, headers=None, payload=None):
            self.status_code = code
            self.headers = headers or {}
            self.text = "{}"
            self._p = payload or {"items": [], "next": None}

        def json(self):
            return self._p

    class Session:
        def __init__(self):
            self.headers = {}
            self.n = 0

        def request(self, *a, **kw):
            self.n += 1
            if self.n == 1:
                return Resp(429, {"Retry-After": "7"})
            return Resp(200)

    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "t")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    client.playlist_tracks("x", max_tracks=10)
    # 7s from Retry-After (+ jitter) plus the pacing sleep
    assert any(6.9 <= s <= 50 for s in slept)
    assert client._rate_limit_wait > 0


def test_rate_limit_gives_up_with_helpful_message(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)

    class Resp:
        status_code = 429
        headers = {"Retry-After": "30"}
        text = '{"error": "rate limited"}'

        def json(self):
            return {}

    class Session:
        def __init__(self):
            self.headers = {}

        def request(self, *a, **kw):
            return Resp()

    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "t")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    with pytest.raises(RateLimited) as exc:
        client.playlist_tracks("x", max_tracks=10)
    msg = str(exc.value)
    assert "wait a few minutes" in msg and "max_tracks" in msg


def test_partial_playlist_survives_rate_limit(monkeypatch):
    """A 429 mid-pagination keeps the tracks already read."""
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)

    def item(i):
        return {
            "is_local": False,
            "track": {
                "id": f"track{i:017d}", "name": f"S{i}", "type": "track",
                "artists": [{"name": "A"}], "album": {"name": "B"},
                "duration_ms": 1000, "external_ids": {}, "uri": f"spotify:track:t{i}",
            },
        }

    class Resp:
        def __init__(self, payload):
            self.status_code = 200
            self.headers = {}
            self.text = "{}"
            self._p = payload

        def json(self):
            return self._p

    class Session:
        def __init__(self):
            self.headers = {}
            self.n = 0

        def request(self, *a, **kw):
            self.n += 1
            if self.n == 1:
                return Resp({"items": [item(i) for i in range(5)], "next": "next-url"})
            raise RateLimited(30, "Spotify rate limit")

    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "t")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    tracks = client.playlist_tracks("x")
    assert len(tracks) == 5
    assert client.partial_read is True


# ------------------------------------------------- cookie pool + queue mode


def test_spare_cookies_rotate_on_long_block(monkeypatch):
    """A long Retry-After on cookie #1 switches to cookie #2 instead of dying."""
    import phonksync.spotify as sp

    class Resp:
        def __init__(self, code, headers=None, payload=None):
            self.status_code = code
            self.headers = headers or {}
            self.text = "{}"
            self._p = payload or {"items": [], "next": None}

        def json(self):
            return self._p

    class Session:
        def __init__(self):
            self.headers = {}
            self.cookie_seen = []

        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            auth = (headers or {}).get("Authorization", "")
            self.cookie_seen.append(auth)
            if len(self.cookie_seen) == 1:
                return Resp(429, {"Retry-After": "76000"})
            return Resp(200)

    monkeypatch.setattr(sp, "_refresh_totp_secrets", lambda *a, **k: None)
    monkeypatch.setattr(sp, "SpotifyClient", sp.SpotifyClient)
    session = Session()
    client = sp.SpotifyClient(
        "sp_dc=" + "a" * 60,
        session=session,
        extra_cookies=["sp_dc=" + "b" * 60],
    )
    client._access_token = "t1"
    client._access_expiry = time.time() + 600
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "token")
    client.playlist_tracks("x", max_tracks=1)
    assert client.cookie_count == 2
    assert client.cookie_switches == 1
    assert client.blocked_until == 0.0


def test_queue_track_then_save_batches_calls(monkeypatch):
    import phonksync.spotify as sp

    calls = []

    class Session:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            calls.append((method, url, dict(params or {}), json))

            class R:
                status_code = 204
                headers = {}
                text = ""

                def json(self):
                    return {}

            return R()

    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "t")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    assert client.queue_track("spotify:track:one") is True
    assert client.queue_track("spotify:track:two") is True
    assert client.queued == 2
    sent = client.save_queue_to_playlist("PLTARGET")
    assert sent == 2
    assert calls[-1][1].endswith("/playlists/PLTARGET/tracks")
    assert calls[-1][3] == {"uris": ["spotify:track:one", "spotify:track:two"]}
    assert client.queued == 0 and client.queue_saves == 1


def test_queue_stops_at_max_items():
    class Session:
        def __init__(self):
            self.headers = {}
            self.n = 0

        def request(self, *a, **kw):
            self.n += 1

            class R:
                status_code = 204
                headers = {}
                text = ""

            return R()

    client = __import__("phonksync.spotify", fromlist=["SpotifyClient"]).SpotifyClient(
        "sp_dc=" + "a" * 60, session=Session()
    )
    client._access_token = "t"
    client._access_expiry = time.time() + 600
    for i in range(3):
        assert client.queue_track(f"spotify:track:{i}", max_items=3) is True
    assert client.queue_track("spotify:track:overflow", max_items=3) is False
