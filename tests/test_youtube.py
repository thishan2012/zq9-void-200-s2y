"""YouTube client tests: read-only API key vs OAuth playlist writes, quota caps."""
import json
from datetime import datetime, timezone

import pytest

from phonksync.errors import AuthError, QuotaExceeded
from phonksync.youtube import QuotaState, YouTubeClient


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params or {}),
             "json": json, "headers": dict(headers or {})}
        )
        return self.handler(method, url, params or {}, json, headers or {})

    def post(self, url, data=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "data": dict(data or {})})
        return FakeResponse(200, {"access_token": "tok", "expires_in": 3600})


TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

CLIENT_JSON = json.dumps(
    {"installed": {"client_id": "cid", "client_secret": "secret",
                   "token_uri": "https://oauth2.googleapis.com/token"},
     "refresh_token": "rtok"}
)


def search_handler(method, url, params, body, headers):
    if url.endswith("/search"):
        return FakeResponse(200, {"items": [{"id": {"videoId": "vid00000001"}}]})
    if url.endswith("/videos"):
        return FakeResponse(200, {"items": []})
    if url.endswith("/playlistItems") and method == "GET":
        return FakeResponse(200, {"items": []})
    return FakeResponse(200, {})


def test_api_key_is_used_for_search_and_no_oauth_token_needed():
    session = FakeSession(search_handler)
    yt = YouTubeClient(api_key="KEY123", session=session)
    ids = yt.search("nulteex montagem miau", max_results=3)
    assert ids == ["vid00000001"]
    call = session.calls[0]
    assert call["params"]["key"] == "KEY123"
    assert "Authorization" not in call["headers"]
    assert yt.search_mode == "api_key"
    assert yt.can_write is False


def test_api_key_only_client_cannot_add_to_playlist():
    session = FakeSession(search_handler)
    yt = YouTubeClient(api_key="KEY123", session=session)
    with pytest.raises(AuthError):
        yt.add_to_playlist("PLtarget", "vid00000001")
    # No wasted HTTP call: the write is refused before hitting the network.
    assert all(c["method"] != "POST" for c in session.calls)


def test_oauth_still_required_for_inserts():
    session = FakeSession(search_handler)
    yt = YouTubeClient(token_json=CLIENT_JSON, session=session)
    assert yt.can_write is True
    yt.add_to_playlist("PLtarget", "vid00000002")
    insert = [c for c in session.calls if c["method"] == "POST" and "playlistItems" in c["url"]]
    assert insert, "playlistItems insert was not attempted"
    assert insert[-1]["headers"]["Authorization"] == "Bearer tok"
    assert yt.quota.inserts == 1
    assert yt.quota.units_used == 50


def test_both_key_and_oauth_search_prefers_key():
    session = FakeSession(search_handler)
    yt = YouTubeClient(api_key="KEY123", token_json=CLIENT_JSON, session=session)
    yt.search("q")
    assert session.calls[0]["params"]["key"] == "KEY123"
    assert yt.can_write is True


def test_oauth_only_config_still_reads_with_oauth():
    session = FakeSession(search_handler)
    yt = YouTubeClient(token_json=CLIENT_JSON, session=session)
    assert yt.api_key == ""
    yt.search("q")
    search = [c for c in session.calls if c["url"].endswith("/search")][0]
    assert search["headers"]["Authorization"] == "Bearer tok"
    assert "key" not in search["params"]


def test_daily_search_cap_survives_multiple_runs():
    session = FakeSession(search_handler)
    quota = QuotaState(date=TODAY, searches=80, units_used=8000)
    yt = YouTubeClient(api_key="KEY123", max_searches_per_run=80,
                       max_searches_per_day=80, quota_state=quota, session=session)
    with pytest.raises(QuotaExceeded) as exc:
        yt.search("q")
    assert "Daily search limit" in str(exc.value)
    # The message must say the key is fine — this cap is ours, not Google's.
    assert "still valid" in str(exc.value)


def test_daily_insert_cap():
    session = FakeSession(search_handler)
    quota = QuotaState(date=TODAY, inserts=40, units_used=2000)
    yt = YouTubeClient(token_json=CLIENT_JSON, max_inserts_per_day=40,
                       quota_state=quota, session=session)
    with pytest.raises(QuotaExceeded):
        yt.add_to_playlist("PLtarget", "vid00000003")


def test_quota_state_round_trips_new_counters():
    q = QuotaState(date="2099-01-01", units_used=123, searches=2, inserts=1)
    again = QuotaState.from_dict(q.to_dict())
    assert (again.units_used, again.searches, again.inserts) == (123, 2, 1)


def test_invalid_api_key_raises_auth_error_not_crash():
    def handler(method, url, params, body, headers):
        return FakeResponse(
            400,
            {"error": {"errors": [{"reason": "keyInvalid"}]}},
            text='{"error":{"errors":[{"reason":"keyInvalid"}],"message":"API key not valid"}}',
        )

    yt = YouTubeClient(api_key="BAD", session=FakeSession(handler))
    with pytest.raises(AuthError):
        yt.search("q")


def test_missing_credentials_message_mentions_api_key():
    with pytest.raises(AuthError) as exc:
        YouTubeClient()
    assert "YOUTUBE_API_KEY" in str(exc.value)


def test_multiple_api_keys_rotate_when_one_is_exhausted():
    def handler(method, url, params, body, headers):
        if url.endswith("/search"):
            if params.get("key") == "KEY1":
                return FakeResponse(
                    403,
                    {"error": {"errors": [{"reason": "quotaExceeded"}]}},
                    text='{"error":{"errors":[{"reason":"quotaExceeded"}]}}',
                )
            return FakeResponse(200, {"items": [{"id": {"videoId": "v2"}}]})
        return FakeResponse(200, {})

    session = FakeSession(handler)
    yt = YouTubeClient(api_key="KEY1, KEY2", session=session)
    assert yt.api_keys == ["KEY1", "KEY2"]
    assert yt.search("q") == ["v2"]
    used = [c["params"].get("key") for c in session.calls if c["url"].endswith("/search")]
    assert used == ["KEY1", "KEY2"]  # rotated after the 403, not aborted


def test_all_keys_exhausted_raises_quota_exceeded():
    def handler(method, url, params, body, headers):
        return FakeResponse(
            403,
            {"error": {"errors": [{"reason": "quotaExceeded"}]}},
            text='{"error":{"errors":[{"reason":"quotaExceeded"}]}}',
        )

    yt = YouTubeClient(api_key="K1 K2", session=FakeSession(handler))
    with pytest.raises(QuotaExceeded):
        yt.search("q")
    assert yt.search_mode == "api_key x2"


def test_per_key_budget_from_state_is_respected():
    key_state = {"date": TODAY, "keys": {"0": 9800, "1": 0}, "units_used": 9800}
    quota = QuotaState.from_dict(key_state)
    session = FakeSession(search_handler)
    yt = YouTubeClient(api_key="K1,K2", quota_state=quota, session=session)
    yt.search("q")
    search = [c for c in session.calls if c["url"].endswith("/search")][0]
    assert search["params"]["key"] == "K2"  # K1 has no units left today


# --------------------------------------------------------------- free tier
def test_free_search_parses_the_public_results_page():
    from phonksync.youtube import YouTubeClient

    class Web:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append((url, params))

            class R:
                status_code = 200
                text = '{"videoRenderer":{"videoId":"dQw4w9WgXcQ"}},{"videoId":"aaaaaaaaaaa"}'

            return R()

    web = Web()
    yt = YouTubeClient(api_key="K", session=object(), web_session=web, free_search_pause=0)
    ids = yt.free_search_ids("rick astley never gonna give you up")
    assert ids == ["dQw4w9WgXcQ", "aaaaaaaaaaa"]
    assert web.calls[0][0] == "https://www.youtube.com/results"
    assert yt.searches_this_run == 0            # not a metered search


def test_free_search_is_skipped_when_disabled():
    from phonksync.youtube import YouTubeClient

    yt = YouTubeClient(api_key="K", session=object(), free_search=False)
    assert yt.free_search_ids("anything") == []


def test_free_video_metadata_comes_from_the_watch_page():
    import json as _json

    from phonksync.youtube import YouTubeClient

    payload = {
        "videoDetails": {
            "videoId": "dQw4w9WgXcQ",
            "title": "Rick Astley - Never Gonna Give You Up",
            "author": "Rick Astley",
            "lengthSeconds": "213",
            "shortDescription": "official video",
            "viewCount": "1500000000",
        }
    }
    html = "<html><script>var ytInitialPlayerResponse = " + _json.dumps(payload) + ";</script></html>"

    class Web:
        def get(self, url, params=None, headers=None, timeout=None):
            class R:
                status_code = 200

            R.text = html
            return R()

    yt = YouTubeClient(api_key="K", session=object(), web_session=Web(), free_search_pause=0)
    found = yt.free_videos(["dQw4w9WgXcQ"])
    assert found[0].title.startswith("Rick Astley")
    assert found[0].channel == "Rick Astley"
    assert found[0].duration_s == 213
    assert found[0].views == 1500000000


# ------------------------------------------------------- OAuth write modes
def _yt_with(**kw):
    from phonksync.youtube import YouTubeClient

    return YouTubeClient(api_key="K", session=object(), free_search=False, **kw)


def test_manual_access_token_enables_writes_for_an_hour():
    import time as _time

    yt = _yt_with(access_token="ya29.pasted")
    assert yt.can_write is False
    assert yt.write_mode.startswith("manual")
    assert yt._ensure_token() == "ya29.pasted"
    assert yt._access_expiry > _time.time() + 3000


def test_expired_manual_token_asks_for_a_refresh_token(monkeypatch):
    import time as _time

    from phonksync.errors import AuthError

    yt = _yt_with(access_token="ya29.old")
    monkeypatch.setattr(_time, "time", lambda: yt._access_expiry + 10)
    with pytest.raises(AuthError) as info:
        yt._ensure_token()
    assert "refresh token" in str(info.value)


def test_refresh_token_write_mode_is_labelled_oauth():
    yt = _yt_with(
        refresh_token="1//refresh",
        client_json='{"installed": {"client_id": "id", "client_secret": "sec"}}',
    )
    assert yt.can_write is True
    assert yt.write_mode.startswith("oauth")


def test_a_pasted_access_token_does_not_satisfy_the_refresh_requirement(monkeypatch):
    from phonksync.settings import Secrets

    monkeypatch.setenv("SPOTIFY_COOKIE", "sp_dc=" + "a" * 60)
    monkeypatch.setenv("YT_ACCESS_TOKEN", "ya29.x")
    monkeypatch.delenv("YT_CLIENT_JSON", raising=False)
    monkeypatch.delenv("YT_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    secrets = Secrets.from_env()
    assert secrets.youtube_access_token == "ya29.x"
    assert secrets.youtube_refresh_token == ""


def test_oauth_helpers_build_a_consent_url(tmp_path, capsys):
    from phonksync import oauth_setup

    client = tmp_path / "client_secret.json"
    client.write_text(
        '{"installed": {"client_id": "cid.apps.googleusercontent.com", '
        '"client_secret": "shh", "token_uri": "https://oauth2.googleapis.com/token"}}',
        encoding="utf-8",
    )
    assert oauth_setup.auth_url(str(client), "http://127.0.0.1:8765/callback") == 0
    out = capsys.readouterr().out
    assert "client_id=cid.apps.googleusercontent.com" in out
    assert "access_type=offline" in out
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fcallback" in out


# ------------------------------------------------ client id without a JSON
def test_client_section_accepts_a_bare_client_id():
    from phonksync.oauth_setup import client_section

    section = client_section("", "6766690abc773-xyz123abc.apps.googleusercontent.com", "")
    assert section["client_id"].endswith("apps.googleusercontent.com")
    assert section["token_uri"] == "https://oauth2.googleapis.com/token"


def test_client_section_rejects_something_that_is_not_a_client_id():
    import pytest as _pytest

    from phonksync.oauth_setup import client_section

    with _pytest.raises(ValueError):
        client_section("", "not-a-client-id", "")


def test_client_section_still_accepts_a_json_file(tmp_path):
    from phonksync.oauth_setup import client_section

    path = tmp_path / "client_secret.json"
    path.write_text(
        '{"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "s"}}',
        encoding="utf-8",
    )
    assert client_section(str(path))["client_secret"] == "s"


def test_youtube_client_accepts_a_bare_id_and_secret():
    from phonksync.youtube import YouTubeClient

    yt = YouTubeClient(
        client_id="1234567890-abcdefghijklmnop.apps.googleusercontent.com",
        client_secret="sec",
        refresh_token="1//refresh",
        api_key="",
        free_search=False,
    )
    assert yt.can_write is True
    assert yt.write_mode.startswith("oauth")


def test_doctor_explains_what_is_missing(monkeypatch, capsys):
    from phonksync import oauth_setup

    monkeypatch.delenv("YT_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("YT_ACCESS_TOKEN", raising=False)
    assert oauth_setup.doctor(client_id="1234567890-abcdefghijklmnop.apps.googleusercontent.com") == 1
    out = capsys.readouterr().out
    assert "No client secret" in out
    assert "No refresh token" in out
    assert "--device" in out

    monkeypatch.setenv("YT_REFRESH_TOKEN", "1//refresh")
    monkeypatch.setenv("YOUTUBE_TARGET_PLAYLIST", "PLx")
    assert oauth_setup.doctor(
        client_id="1234567890-abcdefghijklmnop.apps.googleusercontent.com", client_secret="s"
    ) == 0


def test_token_refresh_uses_a_bare_client_id_and_secret():
    """YT_CLIENT_ID + YT_CLIENT_SECRET alone must be able to mint a token."""
    from phonksync.youtube import YouTubeClient

    calls = []

    class Session:
        def post(self, url, data=None, timeout=None, **kw):
            calls.append(dict(data or {}))

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"access_token": "ya29.minted", "expires_in": 3599}

            return R()

    yt = YouTubeClient(
        client_id="123.apps.googleusercontent.com",
        client_secret="GOCSPX-x",
        refresh_token="1//refresh",
        api_key="",
        session=Session(),
        free_search=False,
    )
    assert yt.can_write is True
    assert yt._ensure_token() == "ya29.minted"
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["client_id"] == "123.apps.googleusercontent.com"
    assert calls[0]["refresh_token"] == "1//refresh"
    assert yt.write_mode.startswith("oauth")


# ------------------------------------------------ cookie mode (no OAuth at all)
COOKIE = (
    "SID=abc; HSID=def; SSID=ghi; APISID=jkl; "
    "__Secure-3PAPISID=sapisid-value; __Secure-3PSID=psid"
)


class WebSession:
    """Records the requests the public web / YouTube Music pages receive."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "params": dict(params or {}),
                           "headers": dict(headers or {})})
        return self.handler("GET", url, params or {}, headers or {})

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "json": json,
                           "headers": dict(headers or {})})
        return self.handler("POST", url, json or {}, headers or {})


def cookie_client(handler, **kwargs):
    return YouTubeClient(
        api_key="",
        cookie=COOKIE,
        free_search=True,
        web_session=WebSession(handler),
        session=FakeSession(lambda *a, **k: FakeResponse(500, {})),
        **kwargs,
    )


def test_cookie_alone_can_write_and_says_so():
    yt = cookie_client(lambda *a: FakeResponse(200, {}))
    assert yt.can_write is True
    assert yt.has_oauth is False
    assert "cookie" in yt.write_mode
    assert "Console" in yt.write_mode


def test_cookie_without_sapisid_cannot_write():
    yt = YouTubeClient(api_key="", cookie="SID=abc; HSID=def", free_search=True)
    assert yt.can_write is False
    assert yt.write_mode == "none"


def test_cookie_add_posts_action_add_video_with_sapisidhash():
    def handler(method, url, payload, headers):
        return FakeResponse(200, {"status": "STATUS_SUCCEEDED"})

    yt = cookie_client(handler)
    yt.add_to_playlist("PLtest123", "vid00000001")
    call = yt.web_session.calls[-1]
    assert call["url"].startswith("https://music.youtube.com/youtubei/v1/browse/edit_playlist")
    assert call["json"]["playlistId"] == "PLtest123"
    assert call["json"]["actions"] == [
        {"action": "ACTION_ADD_VIDEO", "addedVideoId": "vid00000001"}
    ]
    assert call["json"]["context"]["client"]["clientName"] == "WEB_REMIX"
    assert call["headers"]["Authorization"].startswith("SAPISIDHASH ")
    assert call["headers"]["X-Goog-AuthUser"] == "0"
    assert call["headers"]["Cookie"] == COOKIE


def test_cookie_add_raises_when_youtube_does_not_confirm():
    from phonksync.errors import RateLimited

    yt = cookie_client(lambda *a: FakeResponse(200, {"status": "STATUS_FAILED"}))
    with pytest.raises(RateLimited):
        yt.add_to_playlist("PLtest", "vid1")


def test_cookie_auth_failure_is_reported_as_auth_error():
    yt = cookie_client(lambda *a: FakeResponse(401, {}))
    with pytest.raises(AuthError):
        yt.add_to_playlist("PLtest", "vid1")


def test_cookie_mode_reads_the_playlist_through_youtube_music():
    def handler(method, url, payload, headers):
        return FakeResponse(
            200,
            {"contents": {"items": [{"videoId": "aaa"}, {"videoId": "bbb"}]}},
        )

    yt = cookie_client(handler)
    assert yt.playlist_video_ids("PLtest") == {"aaa", "bbb"}


def test_cookie_mode_searches_and_reads_details_without_an_api_key():
    def handler(method, url, payload, headers):
        if "search" in url:
            return FakeResponse(200, {"a": {"videoId": "vidA"}, "b": {"videoId": "vidB"}})
        if "player" in url:
            return FakeResponse(
                200,
                {"videoDetails": {"title": "Nulteex - PHONK", "author": "Nulteex",
                                  "lengthSeconds": "131"}},
            )
        if "results" in str(url):
            return FakeResponse(200, text="")  # public search page finds nothing
        return FakeResponse(200, text="<html></html>")  # watch page: no metadata

    yt = cookie_client(handler)
    assert yt.search("nulteex phonk") == ["vidA", "vidB"]
    got = yt.videos(["vidA"])
    assert got and got[0].video_id == "vidA" and got[0].title == "Nulteex - PHONK"
    assert yt.search_mode.startswith("browser cookie")


def test_cookie_never_overrides_a_real_refresh_token():
    yt = YouTubeClient(
        client_id="1234567890-abcdefghijklmnop.apps.googleusercontent.com",
        client_secret="s",
        refresh_token="1//refresh",
        api_key="",
        cookie=COOKIE,
        free_search=False,
        session=FakeSession(lambda *a, **k: FakeResponse(500, {})),
    )
    assert yt.has_oauth is True
    assert yt.write_mode == "oauth (refresh token)"


def test_doctor_flags_swapped_token_types(monkeypatch, capsys):
    from phonksync.oauth_setup import doctor

    monkeypatch.setenv("YT_REFRESH_TOKEN", "ya29.access-token")
    monkeypatch.delenv("YT_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("YT_COOKIE", raising=False)
    assert doctor() == 1
    out = capsys.readouterr().out
    assert "access token (ya29…)" in out


def test_cookie_help_validates_a_pasted_header(capsys):
    from phonksync.oauth_setup import cookie_help

    assert cookie_help(COOKIE) == 0
    out = capsys.readouterr().out
    assert "__Secure-3PAPISID found" in out
    assert "No OAuth, no Console" in out or "no OAuth" in out

    assert cookie_help("SID=abc") == 1
    assert "No __Secure-3PAPISID" in capsys.readouterr().out


# ------------------------- how the cookie is pasted (the usual silent failure)
@pytest.mark.parametrize(
    "pasted",
    [
        "SID=abc; __Secure-3PAPISID=XYZ123; HSID=d",
        "Cookie: SID=abc; __Secure-3PAPISID=XYZ123",
        "cookie:\tSID=abc; __Secure-3PAPISID=XYZ123",
        ":authority: music.youtube.com\n:method: GET\ncookie: SID=abc; __Secure-3PAPISID=XYZ123",
        '"SID=abc; __Secure-3PAPISID=XYZ123"',
        "SID=abc; __secure-3papisid=XYZ123",  # some tools lower-case names
        "__Secure-3PAPISID=XYZ123",  # SAPISID only
    ],
)
def test_every_paste_shape_produces_a_usable_cookie(pasted):
    yt = YouTubeClient(api_key="", cookie=pasted, free_search=False)
    assert yt._sapisid() == "XYZ123"
    assert yt.can_write is True


def test_cookie_diag_explains_a_broken_paste_without_leaking_it():
    yt = YouTubeClient(api_key="", cookie="music.youtube.com", free_search=False)
    diag = yt.cookie_diag()
    assert "no usable SAPISID" in diag
    assert "not the URL" in diag
    assert "music.youtube.com" not in diag

    ok = YouTubeClient(api_key="", cookie=COOKIE, free_search=False).cookie_diag()
    assert "cookie ok" in ok and "full session cookie" in ok
    assert "sapisid-value" not in ok  # never the raw value


# ------------------------- proving the cookie really writes (probe command)
def test_cookie_probe_adds_an_existing_video_and_undoes_the_extra_copy(monkeypatch, capsys):
    from phonksync import oauth_setup

    state = {"items": [{"video_id": "vidA", "set_video_id": "set1"}]}
    calls = []

    def post(self, url, json=None, timeout=None, **kw):
        calls.append(json)
        action = json["actions"][0]["action"]
        if action == "ACTION_ADD_VIDEO":
            state["items"].append({"video_id": json["actions"][0]["addedVideoId"],
                                   "set_video_id": "set2"})
        else:
            state["items"] = [i for i in state["items"] if i["set_video_id"] != "set2"]
        return FakeResponse(200, {"status": "STATUS_SUCCEEDED"})

    def fake_request(self, endpoint, payload):
        calls.append(payload)
        if endpoint == "browse":
            return {
                "contents": [
                    {"playlistItemData": {"videoId": i["video_id"]},
                     "setVideoId": i["set_video_id"]}
                    for i in state["items"]
                ]
            }
        action = payload["actions"][0]["action"]
        if action == "ACTION_ADD_VIDEO":
            state["items"].append({"video_id": payload["actions"][0]["addedVideoId"],
                                   "set_video_id": "set2"})
        else:
            state["items"] = [i for i in state["items"] if i["set_video_id"] != "set2"]
        return {"status": "STATUS_SUCCEEDED"}

    monkeypatch.setattr("phonksync.youtube.YouTubeClient._ytm_request", fake_request)
    rc = oauth_setup.cookie_test(
        playlist="PLtest000000000000000000000000000", cookie=COOKIE
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Write accepted and confirmed" in out
    assert "Cookie writes VERIFIED end to end" in out
    assert "Undone" in out or "already present" in out
    # the undo left exactly what was there before the probe
    assert state["items"] == [{"video_id": "vidA", "set_video_id": "set1"}]


def test_cookie_probe_refuses_a_broken_cookie(capsys):
    from phonksync.oauth_setup import cookie_test

    assert cookie_test("PLtest", cookie="SID=a") == 1
    assert "no usable SAPISID" in capsys.readouterr().out


def test_cookie_probe_explains_a_network_failure(monkeypatch, capsys):
    from phonksync import oauth_setup
    from phonksync.errors import RateLimited
    from phonksync.youtube import YouTubeClient

    def boom(self, endpoint, body):
        raise RateLimited(message="YouTube Music request failed (SSLError: closed)")

    monkeypatch.setattr(YouTubeClient, "_ytm_request", boom)
    rc = oauth_setup.cookie_test("PLtest", cookie=COOKIE)
    out = capsys.readouterr().out
    assert rc == 1
    assert "no network path" in out


def test_run_summary_reports_confirmed_inserts():
    from phonksync.engine import RunReport

    report = RunReport(youtube_inserted=3, inserted=5, write_mode="browser cookie")
    text = report.summary_text()
    assert "YouTube inserts confirmed by Google: 3" in text


# ------------------------- cookie names mangled by browsers/export tools
@pytest.mark.parametrize(
    "pasted",
    [
        "SID=a; __Secure-3PAPISID=XYZ",          # canonical
        "SID=a; _Secure-3PAPISID=XYZ",           # one underscore lost
        "SID=a; __Secure3PAPISID=XYZ",           # dashes lost
        "sid=a; __secure-3papisid=XYZ",          # lower-cased
        "Cookie: SID=a; __Secure-3PAPISID=XYZ",  # curl export
        ':authority: music.youtube.com\ncookie: SID=a; __Secure-3PAPISID=XYZ',
        '[{"name":"SID","value":"a"},{"name":"__Secure-3PAPISID","value":"XYZ"}]',
    ],
)
def test_mangled_cookie_names_still_authenticate(pasted):
    yt = YouTubeClient(api_key="", cookie=pasted, free_search=False)
    assert yt._sapisid() == "XYZ"
    assert yt.can_write is True
    assert "cookie ok" in yt.cookie_diag()


def test_a_narrow_cookie_is_labelled_narrow():
    yt = YouTubeClient(api_key="", cookie="__Secure-3PAPISID=XYZ", free_search=False)
    # "SID" must never match the tail of "__Secure-3PAPISID"
    assert yt._cookie_value(yt.cookie, "SID") == ""
    assert "SAPISID-only" in yt.cookie_diag()


def test_diag_never_reprints_the_cookie_name_or_value():
    yt = YouTubeClient(api_key="", cookie="SID=a; __Secure-3PAPISID=", free_search=False)
    diag = yt.cookie_diag()
    assert "__Secure-3PAPISID" not in diag  # the old text got mangled into "**Secure"
    assert "no usable SAPISID" in diag


# ------------------------------------------------------- free-page speed
class _Resp:
    def __init__(self, text="", status=200, elapsed=0.0):
        self.text = text
        self.status_code = status
        self.elapsed = _Elapsed(elapsed)


class _Elapsed:
    def __init__(self, seconds):
        self._s = seconds

    def total_seconds(self):
        return self._s


class _Session:
    """Counts page loads; every page is a fixed-latency fake."""

    def __init__(self, latency=0.4, ids=None):
        self.latency = latency
        self.calls = []
        self.ids = ids or []

    def mount(self, *a, **kw):
        pass

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, (params or {}).get("v") or (params or {}).get("search_query")))
        text = ""
        if "results" in url:
            text = "".join(f'"videoId":"vid{i:08d}"' for i in range(len(self.ids)))
        else:
            vid = (params or {}).get("v") or ""
            text = 'ytInitialPlayerResponse = ' + json.dumps(
                {
                    "videoDetails": {
                        "videoId": vid,
                        "title": "Nulteex - MONTAGEM MIAU",
                        "author": "Nulteex - Topic",
                        "lengthSeconds": "120",
                    }
                }
            )
        return _Resp(text=text, elapsed=self.latency)


def _client(session, pause=1.0):
    from phonksync.youtube import YouTubeClient

    return YouTubeClient(
        cookie="SID=a; __Secure-3PAPISID=b",
        free_search=True,
        free_search_pause=pause,
        web_session=session,
    )


def test_a_video_page_is_never_downloaded_twice():
    ids = [f"vid{i:08d}" for i in range(3)]
    session = _Session(ids=ids)
    yt = _client(session, pause=0.0)
    yt.free_videos(ids)
    first = len(session.calls)
    yt.free_videos(ids)  # the next pipeline step asks for the same ids
    assert len(session.calls) == first  # 0 extra page loads
    assert yt.free_pages_this_run == 3


def test_page_gap_counts_the_time_the_page_already_took(monkeypatch):
    """A 0.9s page + 1.0s gap must not become 1.9s of waiting."""
    slept = []
    monkeypatch.setattr("phonksync.youtube.time.sleep", lambda s: slept.append(s))
    session = _Session(latency=0.9, ids=["vid00000000"])
    yt = _client(session, pause=1.0)
    yt.free_search_ids("nulteex montagem miau")
    assert slept and abs(slept[0] - 0.1) < 1e-6


def test_video_pages_are_paced_lighter_than_searches(monkeypatch):
    slept = []
    monkeypatch.setattr("phonksync.youtube.time.sleep", lambda s: slept.append(s))
    session = _Session(latency=0.0, ids=[])
    yt = _client(session, pause=1.0)
    yt.free_videos(["vid00000001", "vid00000002"])
    assert all(s <= 0.25 for s in slept), slept


def test_keepalive_adapter_is_mounted_once():
    session = _Session()
    yt = _client(session, pause=0.0)
    assert yt.web_session is session
