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
