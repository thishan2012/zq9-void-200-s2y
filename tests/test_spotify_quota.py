import json
import time
from email.utils import formatdate

import pytest

from phonksync.errors import RateLimited
from phonksync.spotify import SpotifyClient, _quota_wait
from phonksync.spotify_quota import SpotifyQuota


class Response:
    def __init__(self, seconds=None, payload=None):
        self.status_code = 429
        self.headers = {} if seconds is None else {"Retry-After": str(seconds)}
        self.payload = payload or {}
        self.text = "{}"
    def json(self):
        return self.payload


class Session:
    def __init__(self, response):
        self.headers = {}
        self.calls = 0
        self.response = response
    def request(self, *args, **kwargs):
        self.calls += 1
        return self.response


def test_persisted_429_blocks_a_new_client_without_network(monkeypatch, tmp_path):
    session = Session(Response(120))
    first = SpotifyClient("sp_dc=" + "a" * 60, session=session)
    monkeypatch.setattr(first, "_ensure_token", lambda: "token")
    with pytest.raises(RateLimited):
        first._request("GET", "me")
    assert session.calls == 1
    path = tmp_path / "spotify-quota.json"
    assert json.loads(path.read_text())["web_api"] > time.time() + 100
    second_session = Session(Response(120))
    second = SpotifyClient("sp_dc=" + "a" * 60, session=second_session)
    monkeypatch.setattr(second, "_ensure_token", lambda: pytest.fail("token requested during API cooldown"))
    with pytest.raises(RateLimited):
        second._request("GET", "me")
    assert second_session.calls == 0


def test_expiry_allows_api_and_token_block_is_separate(monkeypatch):
    guard = SpotifyQuota()
    guard.block("token", 120)
    session = Session(Response(2))
    client = SpotifyClient("sp_dc=" + "a" * 60, session=session)
    client._access_token, client._access_expiry = "existing", time.time() + 600
    assert client._ensure_token() == "existing"
    client._access_expiry = 0
    with pytest.raises(RateLimited):
        client._ensure_token()
    guard.path.write_text(json.dumps({"token": time.time() - 1, "web_api": time.time() - 1}))
    monkeypatch.setattr(client, "_ensure_token", lambda: "token")
    session.response.status_code = 200
    client._request("GET", "me")
    assert session.calls == 1


def test_explicit_quota_missing_header_and_http_date():
    assert _quota_wait(Response(payload={"error": {"reason": "QUOTA_EXCEEDED"}})) == 3600
    r = Response()
    r.headers = {"Retry-After": formatdate(time.time() + 3600, usegmt=True)}
    assert _quota_wait(r) > 3500
    assert _quota_wait(Response(10)) == 0
    assert _quota_wait(Response(11)) == 11


def test_guard_does_not_shorten_or_hide_corrupt_file():
    g = SpotifyQuota()
    old = g.block("web_api", 3600)
    assert g.block("web_api", 20) == old
    g.path.write_text("broken")
    with pytest.raises(RuntimeError):
        g.check("web_api")


def test_web_api_block_does_not_block_existing_token():
    guard = SpotifyQuota()
    guard.block("web_api", 3600)
    client = SpotifyClient("sp_dc=" + "a" * 60, session=Session(Response(1)))
    client._access_token, client._access_expiry = "existing", time.time() + 600
    assert client._ensure_token() == "existing"
    assert client.web_player_write_ready


def test_token_cooldown_survives_new_client_without_token_http(monkeypatch):
    import phonksync.spotify as sp
    monkeypatch.setattr(sp, "_refresh_totp_secrets", lambda *a: None)
    monkeypatch.setattr(sp, "_server_time", lambda *a: int(time.time()))
    first = SpotifyClient("sp_dc=" + "a" * 60, session=Session(Response(20)))
    calls = []
    monkeypatch.setattr(first, "_token_http", lambda *a: calls.append(1) or Response(20))
    with pytest.raises(RateLimited):
        first._ensure_token()
    assert len(calls) == 1
    second = SpotifyClient("sp_dc=" + "a" * 60, session=Session(Response(20)))
    monkeypatch.setattr(second, "_token_http", lambda *a: pytest.fail("token HTTP called during cooldown"))
    with pytest.raises(RateLimited):
        second._ensure_token()


def test_repeated_short_429_is_saved_without_third_request(monkeypatch):
    monkeypatch.setattr("phonksync.spotify.time.sleep", lambda _: None)
    s = Session(Response(1))
    c = SpotifyClient("sp_dc=" + "a" * 60, session=s)
    monkeypatch.setattr(c, "_ensure_token", lambda: "token")
    with pytest.raises(RateLimited):
        c._request("GET", "me")
    assert s.calls == 2
    assert SpotifyQuota().until("web_api") > time.time()
