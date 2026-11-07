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
    # a persistent 429 is a cooldown now, not a hard failure
    assert "stopping early" in msg and "web-player route" in msg
    assert client.blocked_until > 0


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


# ------------------------------------------------- delay + backoff knobs


def test_request_delay_defaults_to_two_seconds(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.delenv("REQUEST_DELAY_SECONDS", raising=False)
    assert sp.request_delay_seconds() == 2.0
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=_TokSession())
    assert client.request_delay == 2.0


def test_request_delay_env_override_and_bad_values(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setenv("REQUEST_DELAY_SECONDS", "1.5")
    assert sp.request_delay_seconds() == 1.5
    monkeypatch.setenv("REQUEST_DELAY_SECONDS", "nonsense")
    assert sp.request_delay_seconds() == 2.0
    monkeypatch.setenv("REQUEST_DELAY_SECONDS", "999")
    assert sp.request_delay_seconds() == 30.0  # clamped
    monkeypatch.setenv("REQUEST_DELAY_SECONDS", "-4")
    assert sp.request_delay_seconds() == 0.0


def test_backoff_seconds_uses_retry_after_then_exponential():
    import phonksync.spotify as sp

    assert 29 <= sp.backoff_seconds({"Retry-After": "30"}) <= 32
    # no header -> 2**attempt, floored at 2s
    assert 2 <= sp.backoff_seconds({}, 1) <= 4
    assert 8 <= sp.backoff_seconds({}, 3) <= 10  # 2**3 + jitter
    # never above the cap (+ jitter)
    assert sp.backoff_seconds({"Retry-After": "76000"}) <= 47
    # HTTP-date form is understood
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
    assert 25 <= sp.backoff_seconds({"Retry-After": future}) <= 33


def test_pace_uses_configured_delay(monkeypatch):
    import phonksync.spotify as sp

    slept = []
    monkeypatch.setattr(sp.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setenv("REQUEST_DELAY_SECONDS", "1.25")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=_TokSession())
    client._pace()
    assert slept and abs(slept[0] - 1.25) < 0.01


# --------------------------------------------- public page reader (no API)


def _html_payload(items):
    import json as _json

    return (
        '<html><script id="__NEXT_DATA__">'
        + _json.dumps({"props": {"pageProps": {"state": {"data": {"playlistV2": {
            "content": {"items": items}}}}}}})
        + "</script></html>"
    )


def _v2_item(vid, name, artist, ms=200000):
    return {"itemV2": {"data": {
        "uri": f"spotify:track:{vid}",
        "name": name,
        "artists": {"items": [{"profile": {"name": artist}}]},
        "albumOfTrack": {"name": "Album"},
        "trackDuration": {"totalMilliseconds": ms},
    }}}


class _HtmlSession:
    def __init__(self, html, status=200):
        self.headers = {}
        self.html = html
        self.status = status
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})

        class R:
            status_code = self.status
            text = self.html

        return R()


def test_public_reader_parses_playlist_page():
    import phonksync.spotify as sp

    session = _HtmlSession(_html_payload([
        _v2_item("4uLU6hMCjMI75M1A2tKUQC", "Never Gonna Give You Up", "Rick Astley", 213573),
        _v2_item("0VjIjW4GlUZAMYd2vXMi3b", "Blinding Lights", "The Weeknd", 200040),
    ]))
    reader = sp.PublicPlaylistReader(session)
    tracks = reader.playlist_tracks("37i9dQZF1DXcBWIGoYBM5M")
    assert [t.name for t in tracks] == ["Never Gonna Give You Up", "Blinding Lights"]
    assert tracks[0].artists == ["Rick Astley"]
    assert tracks[0].duration_ms == 213573
    assert tracks[0].uri.startswith("spotify:track:")
    # the render payload needs the browser patch header, not an API token
    assert session.calls[0]["headers"]["_patch"] == '%22browser%22'
    assert "Authorization" not in session.calls[0]["headers"]


def test_public_reader_reports_private_playlist(monkeypatch):
    import phonksync.spotify as sp

    # No pathfinder fallback in this test: it is a network call.
    monkeypatch.setattr(sp, "pathfinder_playlist", lambda *a, **k: [])
    monkeypatch.setattr(sp, "client_token", lambda: "")
    reader = sp.PublicPlaylistReader(_HtmlSession("<html>nope</html>"))
    assert reader.playlist_tracks("x") == []
    assert "payload not found" in reader.last_error


def test_pathfinder_reader_parses_pages(monkeypatch):
    """The web player's own endpoint is the answer to a Web-API 429."""
    import json as _json

    import phonksync.spotify as sp

    pages = [
        {"data": {"playlistV2": {"content": {"totalCount": 3, "items": [
            _v2_item("aaa", "One", "A"), _v2_item("bbb", "Two", "B"),
        ]}}}},
        {"data": {"playlistV2": {"content": {"totalCount": 3, "items": [
            _v2_item("ccc", "Three", "C"),
        ]}}}},
    ]
    calls = []

    def fake_fetch(url, payload, headers):
        calls.append((url, payload, headers))
        return _json.dumps(pages[len(calls) - 1])

    monkeypatch.setattr(sp, "request_delay_seconds", lambda: 0)
    tracks = sp.pathfinder_playlist("PL123", client_token="ct", page_size=2, fetcher=fake_fetch)
    assert [t.name for t in tracks] == ["One", "Two", "Three"]
    assert calls[0][0] == "https://api-partner.spotify.com/pathfinder/v2/query"
    assert calls[0][2]["client-token"] == "ct"
    assert calls[0][1]["operationName"] == "fetchPlaylist"
    assert calls[0][1]["variables"]["uri"] == "spotify:playlist:PL123"
    assert calls[0][1]["extensions"]["persistedQuery"]["sha256Hash"] == sp._KNOWN_PLAYLIST_HASH
    assert calls[1][1]["variables"]["offset"] == 2


def test_pathfinder_reader_refreshes_a_stale_hash(monkeypatch):
    import json as _json

    import phonksync.spotify as sp

    new_hash = "b" * 64
    state = {"n": 0}

    def fake_fetch(url, payload, headers):
        state["n"] += 1
        if state["n"] == 1:
            return _json.dumps({"errors": [{"message": "PersistedQueryNotFound"}]})
        return _json.dumps({"data": {"playlistV2": {"content": {"totalCount": 1, "items": [
            _v2_item("aaa", "One", "A")]}}}})

    monkeypatch.setattr(sp, "_discover_persisted_hashes", lambda ops: {"fetchPlaylist": new_hash})
    monkeypatch.setattr(sp, "request_delay_seconds", lambda: 0)
    tracks = sp.pathfinder_playlist("PL1", client_token="ct", page_size=10, fetcher=fake_fetch)
    assert [t.name for t in tracks] == ["One"]
    assert state["n"] == 2


def test_public_reader_falls_back_to_pathfinder(monkeypatch):
    import phonksync.spotify as sp
    from phonksync.models import Track as _Track

    monkeypatch.setattr(
        sp, "pathfinder_playlist",
        lambda pid, client_token="": [_Track(id="tid1", name="Via Pathfinder",
                                             artists=["X"], album="Al", duration_ms=1000,
                                             uri="spotify:track:tid1")],
    )
    reader = sp.PublicPlaylistReader(_HtmlSession("<html>no payload</html>"), client_token="ct")
    tracks = reader.playlist_tracks("PL1")
    assert [t.name for t in tracks] == ["Via Pathfinder"]
    assert reader.last_error == ""


def test_public_reader_handles_http_errors():
    import phonksync.spotify as sp

    reader = sp.PublicPlaylistReader(_HtmlSession("", status=403))
    assert reader.playlist_tracks("x") == []
    assert "403" in reader.last_error


def test_persistent_short_429_becomes_a_cooldown(monkeypatch):
    """A flapping short Retry-After ends as a cooldown after two knocks."""
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setenv("SPOTIFY_TOTP_REMOTE", "0")

    class Resp:
        status_code = 429
        headers = {"Retry-After": "5"}
        text = "{}"

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
    assert "stopping early" in str(exc.value)
    assert client.blocked_until > time.time() + 60
    assert client.blocked_until <= time.time() + sp._SHORT_BLOCK_COOLDOWN + 5


# --------------------------------------------------------------------------- #
# CORS-proxy routing (SPOTIFY_PROXY_URL)
# --------------------------------------------------------------------------- #
class _ProxyResp:
    def __init__(self, code, payload=None, headers=None, text="{}"):
        self.status_code = code
        self.headers = headers or {}
        self.text = text
        self._p = payload if payload is not None else {"items": [], "next": None}

    def json(self):
        return self._p


def test_with_proxy_builds_query_and_header_urls(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.delenv("SPOTIFY_PROXY_SECRET", raising=False)
    url = sp._with_proxy("https://p.example/", "https://api.spotify.com/v1/me?x=1", None)
    assert url == "https://p.example?url=https%3A%2F%2Fapi.spotify.com%2Fv1%2Fme%3Fx%3D1"

    monkeypatch.setenv("SPOTIFY_PROXY_SECRET", "s3cret")
    assert sp.proxy_headers()["x-proxy-secret"] == "s3cret"
    url = sp._with_proxy("https://p.example", "https://api.spotify.com/v1/me", {"limit": 50})
    assert url == "https://p.example/https://api.spotify.com/v1/me?limit=50"


def test_requests_go_through_the_proxy_when_configured(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setenv("SPOTIFY_PROXY_URL", "https://corsly-proxy2.camdvr.org")
    monkeypatch.delenv("SPOTIFY_PROXY_SECRET", raising=False)
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "tok")

    seen = []

    class Session:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, **kw):
            seen.append(url)
            return _ProxyResp(200, {"items": [], "next": None})

    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    client.playlist_tracks("x", max_tracks=10)
    assert seen and all("corsly-proxy2.camdvr.org" in u for u in seen)
    assert seen[0].startswith("https://corsly-proxy2.camdvr.org?url=")
    assert client.proxy_used > 0 and client.direct_used == 0


def test_429_switches_route_from_proxy_to_direct(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setenv("SPOTIFY_PROXY_URL", "https://p.example")
    monkeypatch.delenv("SPOTIFY_PROXY_SECRET", raising=False)
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "tok")

    seen = []

    class Session:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, **kw):
            seen.append(url)
            if "p.example" in url:
                return _ProxyResp(429, headers={"Retry-After": "3"})
            return _ProxyResp(200, {"items": [], "next": None})

    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    client.playlist_tracks("x", max_tracks=10)
    assert len(seen) >= 2
    assert "p.example" in seen[0] and seen[1].startswith("https://api.spotify.com/v1/")
    assert client.proxy_used == 1 and client.direct_used >= 1


def test_token_request_sends_cookie_header_through_the_proxy(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setenv("SPOTIFY_PROXY_URL", "https://p.example")
    monkeypatch.delenv("SPOTIFY_PROXY_SECRET", raising=False)
    monkeypatch.setattr(sp, "_refresh_totp_secrets", lambda session: None)
    monkeypatch.setattr(sp, "_totp_version", lambda: 61)
    monkeypatch.setattr(sp, "_server_time", lambda session: 12345)

    seen = []

    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, url, headers=None, timeout=None, **kw):
            seen.append((url, headers or {}))
            return _ProxyResp(200, {"accessToken": "abc", "expiresIn": 3600})

    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    monkeypatch.setattr(client, "_totp_params", lambda ts, ver: {"reason": "transport"})
    assert client._ensure_token() == "abc"
    url, headers = seen[0]
    assert url.startswith("https://p.example?url=")
    assert headers["Cookie"].startswith("sp_dc=")
    assert "cookies" not in seen[0][1]


def test_proxy_test_command_reports_missing_setting(monkeypatch, capsys):
    import phonksync.__main__ as main

    monkeypatch.delenv("SPOTIFY_PROXY_URL", raising=False)
    assert main.main(["proxy-test"]) == 2
    assert "SPOTIFY_PROXY_URL is not set" in capsys.readouterr().err


def test_broken_proxy_falls_back_to_direct(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setenv("SPOTIFY_PROXY_URL", "https://dead.example")
    monkeypatch.delenv("SPOTIFY_PROXY_SECRET", raising=False)
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "tok")

    seen = []

    class Session:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, **kw):
            seen.append(url)
            if "dead.example" in url:
                raise sp.requests.ConnectionError("proxy down")
            return _ProxyResp(200, {"items": [], "next": None})

    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    client.playlist_tracks("x", max_tracks=10)
    assert "dead.example" in seen[0]
    assert seen[-1].startswith("https://api.spotify.com/v1/")
    assert client.direct_used >= 1


def test_proxy_5xx_falls_back_to_direct(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setenv("SPOTIFY_PROXY_URL", "https://p.example")
    monkeypatch.delenv("SPOTIFY_PROXY_SECRET", raising=False)
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "tok")

    seen = []

    class Session:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, **kw):
            seen.append(url)
            if "p.example" in url:
                return _ProxyResp(502)
            return _ProxyResp(200, {"items": [], "next": None})

    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=Session())
    client.playlist_tracks("x", max_tracks=10)
    assert "p.example" in seen[0] and seen[-1].startswith("https://api.spotify.com/v1/")


def _token_only_session(responses):
    """Session that serves token responses from a scripted list."""
    class Session:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def get(self, url, headers=None, timeout=None, **kw):
            self.calls += 1
            r = responses[min(self.calls - 1, len(responses) - 1)]
            return r

    return Session()


def _token_client(monkeypatch, session):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp, "_refresh_totp_secrets", lambda s: None)
    monkeypatch.setattr(sp, "_totp_version", lambda: 61)
    monkeypatch.setattr(sp, "_server_time", lambda s: 12345)
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=session)
    monkeypatch.setattr(client, "_totp_params", lambda ts, ver: {"reason": "transport"})
    return client


def test_token_429_is_waited_out_not_reported_as_bad_cookie(monkeypatch):
    import phonksync.spotify as sp

    slept = []
    monkeypatch.setattr(sp.time, "sleep", lambda s: slept.append(s))
    session = _token_only_session([
        _ProxyResp(429, headers={"Retry-After": "5"}),
        _ProxyResp(200, {"accessToken": "tok", "expiresIn": 3600}),
    ])
    client = _token_client(monkeypatch, session)
    assert client._ensure_token() == "tok"
    assert any(4.9 <= s <= 40 for s in slept)


def test_token_429_forever_parks_the_run_instead_of_auth_error(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    session = _token_only_session([_ProxyResp(429, headers={"Retry-After": "2"})])
    client = _token_client(monkeypatch, session)
    with pytest.raises(RateLimited) as info:
        client._ensure_token()
    assert "throttling the token endpoint" in str(info.value)
    assert client.blocked_until > time.time() + 1700


def test_token_429_with_long_retry_after_parks_immediately(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    session = _token_only_session([_ProxyResp(429, headers={"Retry-After": "7200"})])
    client = _token_client(monkeypatch, session)
    with pytest.raises(RateLimited):
        client._ensure_token()
    assert session.calls == 1
    assert client.blocked_until > time.time() + 7100


def test_proxy_post_falls_back_to_direct_on_html_error(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setenv("SPOTIFY_PROXY_URL", "https://p.example")
    calls = []

    class Resp:
        def __init__(self, text, code=200):
            self.text = text
            self.status_code = code

    def fake_post(url, **kw):
        calls.append(url)
        if "p.example" in url:
            return Resp("<html>cors proxy says no</html>")
        return Resp('{"data": {}}')

    monkeypatch.setattr(sp.requests, "post", fake_post)
    text = sp._proxy_post("https://api-partner.spotify.com/x", {"a": 1}, {})
    assert text == '{"data": {}}'
    assert len(calls) == 2 and "p.example" in calls[0]


def test_diag_command_reports_routes(monkeypatch, capsys, tmp_path):
    import phonksync.__main__ as main

    monkeypatch.delenv("SPOTIFY_COOKIE", raising=False)
    monkeypatch.setattr(main, "client_token", lambda: "ct", raising=False)
    args = main.main(["diag", "--playlist", "PL1"])
    out = capsys.readouterr().out
    assert "client token" in out and "pathfinder" in out
    assert args == 0


def _pf_client(monkeypatch, responses):
    """SpotifyClient whose pathfinder POSTs answer from a scripted list."""
    import json as _json

    import phonksync.spotify as sp

    seen = []

    def fake_post(url, payload, headers):
        seen.append({"url": url, "payload": payload, "headers": headers})
        return _json.dumps(responses[min(len(seen) - 1, len(responses) - 1)])

    monkeypatch.setattr(sp, "_proxy_post", fake_post)
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "tok")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=_TokSession())
    return client, seen


def test_pathfinder_tracks_pages_with_the_bearer(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    client, seen = _pf_client(monkeypatch, [
        {"data": {"playlistV2": {"content": {"totalCount": 2, "items": [
            _v2_item("aaa", "One", "A"), _v2_item("bbb", "Two", "B")]}}}},
    ])
    tracks = client._pathfinder_tracks("PL1")
    assert [t.name for t in tracks] == ["One", "Two"]
    assert seen[0]["url"] == "https://api-partner.spotify.com/pathfinder/v2/query"
    assert seen[0]["payload"]["operationName"] == "fetchPlaylistContents"
    assert seen[0]["payload"]["variables"]["uri"] == "spotify:playlist:PL1"
    assert seen[0]["headers"]["Authorization"] == "Bearer tok"
    assert seen[0]["headers"]["app-platform"] == "WebPlayer"


def test_pathfinder_add_writes_through_the_web_player(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    client, seen = _pf_client(monkeypatch, [{"data": {"addItemsToPlaylist": {"__typename": "AddItemsToPlaylistPayload"}}}])
    client._pathfinder_add("PL9", ["spotify:track:aaa", "spotify:track:bbb"])
    # One mutation for the batch, like the browser sends (fewer calls, less 429).
    assert len(seen) == 1
    first = seen[0]["payload"]
    assert first["operationName"] == "addToPlaylist"
    assert first["variables"]["playlistUri"] == "spotify:playlist:PL9"
    assert first["variables"]["playlistItemUris"] == [
        "spotify:track:aaa",
        "spotify:track:bbb",
    ]
    assert first["variables"]["interactionId"] is None
    assert first["variables"]["newPosition"]["moveType"] == "BOTTOM_OF_PLAYLIST"
    assert first["extensions"]["persistedQuery"]["sha256Hash"] == sp._OP_HASHES["addToPlaylist"]


def test_write_falls_back_to_pathfinder_when_the_api_429s(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    client, seen = _pf_client(
        monkeypatch, [{"data": {"addItemsToPlaylist": {"__typename": "AddItemsToPlaylistPayload"}}}]
    )

    def boom(*a, **kw):
        raise RateLimited(30, "429")

    monkeypatch.setattr(client, "_request", boom)
    client.pathfinder_ok = True
    client.add_tracks("PL1", ["spotify:track:aaa"])
    assert seen and seen[0]["payload"]["operationName"] == "addToPlaylist"
    assert client.prefer_pathfinder is True


def test_a_throttled_write_tries_the_web_player_and_names_both_routes(monkeypatch):
    """Never give up on the first 429: the web player route is one call away."""
    client, seen = _pf_client(monkeypatch, [{"data": {"playlistV2": {"__typename": "Error"}}}])
    monkeypatch.setattr(
        client, "_request", lambda *a, **kw: (_ for _ in ()).throw(RateLimited(30, "429"))
    )
    with pytest.raises(RuntimeError) as exc:
        client.add_tracks("PL1", ["spotify:track:aaa"])
    assert seen, "the web player was never tried"
    message = str(exc.value)
    assert "both routes" in message
    assert "Web API (429)" in message
    assert "web-player" in message


def test_spotify_backend_env_selects_web_player(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setenv("SPOTIFY_BACKEND", "web-player")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=_TokSession())
    assert client.backend == "web-player" and client.prefer_pathfinder


def test_hash_discovery_caches_success_but_not_failure(monkeypatch):
    import phonksync.spotify as sp

    sp._discover_cached.cache_clear()
    calls = []

    def fake_once(ops):
        calls.append(list(ops))
        return {"fetchPlaylist": "h" * 64} if len(calls) > 1 else {}

    monkeypatch.setattr(sp, "_discover_persisted_hashes_once", fake_once)
    assert sp._discover_persisted_hashes(["fetchPlaylist"]) == {}   # failure: not cached
    assert sp._discover_persisted_hashes(["fetchPlaylist"]) == {"fetchPlaylist": "h" * 64}
    assert sp._discover_persisted_hashes(["fetchPlaylist"]) == {"fetchPlaylist": "h" * 64}
    assert len(calls) == 2
    sp._discover_cached.cache_clear()


def test_public_reader_names_the_pathfinder_reason(monkeypatch):
    import phonksync.spotify as sp

    def boom(*a, **k):
        raise RuntimeError("pathfinder error: PersistedQueryNotFound")

    monkeypatch.setattr(sp, "pathfinder_playlist", boom)
    monkeypatch.setattr(sp, "client_token", lambda: "ct")
    reader = sp.PublicPlaylistReader(_HtmlSession("<html>no payload</html>"), client_token="ct")
    assert reader.playlist_tracks("PL1") == []
    assert "PersistedQueryNotFound" in reader.last_error
    assert "payload not found" in reader.last_error


def test_public_reader_retry_clears_an_old_error(monkeypatch):
    import phonksync.spotify as sp  # noqa: F401  (module under test)

    monkeypatch.setattr(
        sp, "pathfinder_playlist",
        lambda *a, **k: [sp.Track(id="t1", name="N", artists=[], album="", duration_ms=1,
                                  uri="spotify:track:t1")],
    )
    reader = sp.PublicPlaylistReader(_HtmlSession("<html>no payload</html>"), client_token="ct")
    reader.last_error = "stale error from an earlier attempt"
    assert [t.id for t in reader.playlist_tracks("PL1")] == ["t1"]
    assert reader.last_error == ""


def test_hash_can_be_overridden_from_the_environment(monkeypatch):
    import phonksync.spotify as sp

    monkeypatch.setenv("SPOTIFY_HASH_ADDTOPLAYLIST", "c" * 64)
    monkeypatch.setenv("SPOTIFY_HASH_FETCHPLAYLIST", "not-a-hash")
    overrides = sp._hash_overrides()
    assert overrides == {"addToPlaylist": "c" * 64}


def test_a_24h_block_is_not_retried_even_once(monkeypatch):
    """The real run burned 117s (45+13+45+14) on a Retry-After of 24h."""
    import phonksync.spotify as sp

    sleeps = []
    monkeypatch.setattr(sp.time, "sleep", lambda s: sleeps.append(s))

    class Resp:
        status_code = 429
        headers = {"Retry-After": "129599"}  # ~24h, like the real run
        text = "{}"

        def json(self):
            return {}

    class Session:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def request(self, *a, **kw):
            self.calls += 1
            return Resp()

    session = Session()
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "t")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=session)
    with pytest.raises(RateLimited) as exc:
        client.playlist_tracks("x", max_tracks=10)
    assert session.calls == 1, "a multi-hour block must not be retried"
    assert sleeps == [], "nothing may be slept on a 24h block"
    assert client.blocked_until > 0
    assert "Nothing was retried" in str(exc.value)


def test_a_flapping_short_block_stops_after_two_knocks(monkeypatch):
    """Retry-After flapping 45s -> 13s must not cost ~2 minutes of waiting."""
    import phonksync.spotify as sp

    slept = []
    monkeypatch.setattr(sp.time, "sleep", lambda s: slept.append(s))

    class Resp:
        status_code = 429
        headers = {"Retry-After": "45"}
        text = "{}"

        def json(self):
            return {}

    class Session:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def request(self, *a, **kw):
            self.calls += 1
            return Resp()

    session = Session()
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "t")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=session)
    with pytest.raises(RateLimited):
        client.playlist_tracks("x", max_tracks=10)
    assert session.calls == 2
    assert sum(slept) <= 60  # one Retry-After (+jitter), not two minutes


# ------------------------------------------------ anonymous token / proxy route
class _AnonSession:
    """Cookie #1 is dead (anonymous); only the good value gets a real token."""

    def __init__(self, good):
        self.good = good
        self.headers = {}
        self.cookies_seen = []
        # _server_time() probes HEAD and expects a response with headers
        self.head = lambda *a, **kw: type(
            "R", (), {"headers": {}, "status_code": 200}
        )()

    def get(self, url, params=None, headers=None, cookies=None, timeout=None):
        value = (cookies or {}).get("sp_dc") or ""
        if not value:
            header = (headers or {}).get("Cookie", "")
            for part in header.split(";"):
                key, _, val = part.strip().partition("=")
                if key.strip() == "sp_dc":
                    value = val
        self.cookies_seen.append(value)

        class R:
            status_code = 200
            text = "{}"
            headers = {}

            def json(self_inner):
                if value == self.good:
                    return {"accessToken": "ya29.good", "isAnonymous": False}
                return {"accessToken": "anon", "isAnonymous": True}

        return R()

    def post(self, *a, **kw):  # not used
        raise AssertionError


def test_a_dead_cookie_falls_back_to_the_spare(monkeypatch):
    """A stale SPOTIFY_COOKIE used to hide a perfectly good SPOTIFY_COOKIE_2."""
    import phonksync.spotify as sp

    session = _AnonSession(good="good-cookie")
    client = sp.SpotifyClient(
        "sp_dc=dead-cookie", extra_cookies=["sp_dc=good-cookie"], session=session
    )
    assert client._ensure_token() == "ya29.good"
    assert client.cookie_switches == 1
    assert session.cookies_seen[0] == "dead-cookie"


def test_anonymous_for_every_cookie_says_which_one_and_why(monkeypatch):
    import phonksync.spotify as sp
    from phonksync.errors import AuthError

    session = _AnonSession(good="nobody")
    client = sp.SpotifyClient(
        "sp_dc=dead-one", extra_cookies=["sp_dc=dead-two"], session=session
    )
    with pytest.raises(AuthError) as exc:
        client._ensure_token()
    msg = str(exc.value)
    assert "Cookie tried: #2 of 2" in msg
    assert "Both cookies were tried" in msg
    assert "cookie-fix --show" in msg


def test_anonymous_with_a_single_cookie_points_at_the_spare(monkeypatch):
    import phonksync.spotify as sp
    from phonksync.errors import AuthError

    session = _AnonSession(good="nobody")
    client = sp.SpotifyClient("sp_dc=dead", session=session)
    with pytest.raises(AuthError) as exc:
        client._ensure_token()
    assert "SPOTIFY_COOKIE_2 is empty" in str(exc.value)


def test_a_proxy_that_answers_anonymous_is_retried_direct(monkeypatch):
    """The proxy is the prime suspect: never fail while direct is untried."""
    import phonksync.spotify as sp

    session = _AnonSession(good="nobody")  # anonymous for every cookie
    client = sp.SpotifyClient("sp_dc=dead", session=session)
    client.proxy_url = "https://proxy.example/"
    data = client._token_request()          # no raise: it just reports anonymous
    assert data["isAnonymous"] is True
    assert client.proxy_forced_direct is True
    assert client.last_token_route == "direct"


def test_the_proxy_message_blames_the_proxy_when_direct_also_failed(monkeypatch):
    import phonksync.spotify as sp
    from phonksync.errors import AuthError

    session = _AnonSession(good="nobody")
    client = sp.SpotifyClient("sp_dc=dead", session=session)
    client.proxy_url = "https://proxy.example/"
    client.proxy_forced_direct = True
    client.last_token_route = "proxy"
    with pytest.raises(AuthError) as exc:
        client._ensure_token()
    msg = str(exc.value)
    assert "The proxy route answered logged-out first" in msg
    assert "proxy off" in msg


def test_the_proxy_sends_a_plain_cookie_header(monkeypatch):
    """The proxy gets `sp_dc=<value>`; a CORS proxy mangles a full browser header."""
    import phonksync.spotify as sp

    seen = {}

    class Session:
        def __init__(self):
            self.headers = {}

        def head(self, *a, **kw):
            return type("R", (), {"headers": {}, "status_code": 200})()

        def get(self, url, params=None, headers=None, cookies=None, timeout=None):
            seen["cookie"] = (headers or {}).get("Cookie", "")
            seen["cookies"] = cookies

            class R:
                status_code = 200
                text = "{}"
                headers = {}

                def json(self_inner):
                    return {"accessToken": "t", "isAnonymous": False}

            return R()

    monkeypatch.setenv("SPOTIFY_PROXY_URL", "https://proxy.example/")
    client = sp.SpotifyClient("sp_dc=" + "z" * 60, session=Session())
    client.proxy_url = "https://proxy.example/"
    client._ensure_token()
    assert seen["cookie"].startswith("sp_dc=" + "z" * 60)
    assert seen["cookies"] is None  # the jar cannot cross a CORS proxy


# ---------------------------------------------------- free account / web player
def test_a_free_account_starts_on_the_web_player_route():
    """A bot account is never Premium: no queue call, no Web API write budget."""
    import phonksync.spotify as sp

    client = sp.SpotifyClient("sp_dc=" + "z" * 60, prefer_web_player=True)
    assert client.queue_supported is False
    assert client.prefer_pathfinder is True
    assert client.backend == "web-player"
    # SPOTIFY_BACKEND=rest is still an explicit opt-out
    other = sp.SpotifyClient("sp_dc=" + "z" * 60)
    assert other.queue_supported is True


def test_add_to_playlist_is_verified_and_heals_a_rotated_hash(monkeypatch):
    """A write must fail like a read does: retry on a stale persisted query."""
    import json as _json

    import phonksync.spotify as sp

    calls = []

    def fake_post(url, body, headers):
        calls.append(body["extensions"]["persistedQuery"]["sha256Hash"])
        if len(calls) == 1:
            return _json.dumps({"errors": [{"message": "PersistedQueryNotFound"}]})
        return _json.dumps({"data": {"addItemsToPlaylist": {"__typename": "AddItemsToPlaylistPayload"}}})

    monkeypatch.setattr(sp, "_proxy_post", fake_post)
    monkeypatch.setattr(
        sp, "_discover_persisted_hashes", lambda ops: {"addToPlaylist": "freshhash"}, raising=False
    )
    client = sp.SpotifyClient("sp_dc=" + "z" * 60, prefer_web_player=True)
    client._ensure_token = lambda: "token"
    assert client._pathfinder_write("pl" * 11, "spotify:track:abc") is True
    assert calls[0] != calls[1]  # the healed hash was used
    assert client.writes_this_run == 0  # counted by add_tracks, not here


def test_verify_added_reports_only_what_is_really_in_the_playlist(monkeypatch):
    import phonksync.spotify as sp
    from phonksync.models import Track

    client = sp.SpotifyClient("sp_dc=" + "z" * 60, prefer_web_player=True)
    client._pathfinder_tracks = lambda playlist_id, max_tracks=0: [
        Track(id="abc", name="in", artists=["a"], uri="spotify:track:abc")
    ]
    assert client.verify_added("pl", ["spotify:track:abc", "spotify:track:gone"]) == [
        "spotify:track:abc"
    ]


def test_a_failed_web_player_write_falls_back_to_the_web_api(monkeypatch):
    """Two independent chances per run: one refused route is not a dead end."""
    client, seen = _pf_client(
        monkeypatch, [{"data": {"playlistV2": {"__typename": "Error"}}}]
    )
    rest_calls = []
    monkeypatch.setattr(
        client, "_request", lambda *a, **kw: rest_calls.append(a) or {"ok": True}
    )
    client.prefer_pathfinder = True
    client.add_tracks("PL1", ["spotify:track:aaa"])
    assert seen, "the web player was never tried"
    assert rest_calls, "the Web API was never tried after the refusal"
    assert client.last_write_error == ""


def test_both_routes_refusing_is_reported_as_such(monkeypatch):
    client, _ = _pf_client(
        monkeypatch, [{"data": {"playlistV2": {"__typename": "Error"}}}]
    )

    def boom(*a, **kw):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(client, "_request", boom)
    client.prefer_pathfinder = True
    with pytest.raises(RuntimeError) as exc:
        client.add_tracks("PL1", ["spotify:track:aaa"])
    assert "both routes" in str(exc.value)
    assert "403 Forbidden" in str(exc.value)
    assert "web-player" in client.last_write_error


def test_the_permission_hint_says_whether_this_account_may_write(monkeypatch):
    """A collaborative playlist + a bot cookie is the classic silent refusal."""
    client, _ = _pf_client(
        monkeypatch,
        [{"data": {"playlistV2": {
            "name": "phonk",
            "collaborative": True,
            "ownerV2": {"data": {"username": "thishan"}},
        }}}],
    )
    client.account_username = "phonkbot"
    hint = client.permission_hint("PL1")
    assert "playlist owner: thishan" in hint
    assert "cookie account: phonkbot" in hint
    assert "collaborative: true" in hint
    assert "does NOT own" in hint


def test_the_permission_hint_clears_the_owner(monkeypatch):
    client, _ = _pf_client(
        monkeypatch,
        [{"data": {"playlistV2": {"ownerV2": {"data": {"username": "phonkbot"}}}}}],
    )
    client.account_username = "phonkbot"
    assert "OWNS the playlist" in client.permission_hint("PL1")


def test_the_hash_scrape_finds_a_mutation_not_only_a_query(monkeypatch):
    """addToPlaylist is registered as a *mutation*: matching only "query" made
    the self-healing scrape useless for every write."""
    import phonksync.spotify as sp

    html = (
        '<script src="https://open.spotifycdn.com/cdn/build/web-player/'
        'web-player.0123456789abcdef.js"></script>'
    )
    bundle = (
        'x.l("fetchPlaylist","query","' + "a" * 64 + '");'
        'x.l("addToPlaylist","mutation","' + "b" * 64 + '");'
    )

    class Resp:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr(
        sp.requests,
        "get",
        lambda url, *a, **kw: Resp(bundle if "web-player" in str(url) else html),
    )
    found = sp._discover_persisted_hashes_once(["addToPlaylist", "fetchPlaylist"])
    assert found["addToPlaylist"] == "b" * 64
    assert found["fetchPlaylist"] == "a" * 64


def test_the_add_uses_the_proven_hash_and_confirms_on_the_payload():
    """The hash and the confirmation field a working syncer uses."""
    import phonksync.spotify as sp

    assert sp._OP_HASHES["addToPlaylist"] == (
        "b907ad6b088d5bf7680fa81198996d58398c2d55dfcdf59fcc959e0be98a418a"
    )


def test_a_playlist_read_falls_back_to_the_browser_operation(monkeypatch):
    """When `fetchPlaylistContents` answers nothing, try what the browser uses."""
    import phonksync.spotify as sp

    calls = []

    def fake_post(url, payload, headers):
        op = payload["operationName"]
        calls.append(op)
        if op == "fetchPlaylist":
            return (
                '{"data": {"playlistV2": {"content": {"totalCount": 1, "items": '
                '[{"itemV2": {"data": {"uri": "spotify:track:abc", "name": "LUZ", '
                '"artists": {"items": []}}}}]}}}}'
            )
        return '{"data": {"playlistV2": {"content": {"totalCount": 0, "items": []}}}}'

    monkeypatch.setattr(sp, "_proxy_post", fake_post)
    monkeypatch.setattr(sp.SpotifyClient, "_ensure_token", lambda self: "tok")
    client = sp.SpotifyClient("sp_dc=" + "a" * 60, session=_TokSession())
    tracks = client._pathfinder_tracks("PL1")
    assert [t.id for t in tracks] == ["abc"]
    assert calls[0] == "fetchPlaylistContents"
    assert "fetchPlaylist" in calls
    assert client._read_op_name == "fetchPlaylist"
