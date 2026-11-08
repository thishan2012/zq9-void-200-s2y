"""OAuth account failover: run account 1 until Google says stop, then continue.

The user's rule: no hand-made cap on the first account. Let it add as much as it
can, and only move to account 2 when the real quotaExceeded error comes back.
"""
import json

import pytest

from phonksync.errors import AuthError, QuotaExceeded
from phonksync.settings import ConfigError, Secrets
from phonksync.youtube import COST_PLAYLIST_INSERT, QuotaState, YouTubeClient


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or (json.dumps(self._payload) if payload is not None else "")

    def json(self):
        return self._payload


def quota_error():
    return Resp(403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})


class Scripted:
    """A session that replays a fixed list of responses and records the auth."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.refreshes = []

    def post(self, url, data=None, **kw):
        """Real token refresh: the token is named after the client that asked."""
        self.refreshes.append(dict(data or {}))
        return Resp(200, {
            "access_token": f"tok-{(data or {}).get('client_id', '?')}",
            "expires_in": 3600,
        })

    def request(self, method, url, **kw):
        self.calls.append({"url": url, "headers": dict(kw.get("headers") or {}),
                           "json": kw.get("json")})
        if not self.responses:
            raise AssertionError("no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def mount(self, *a, **kw):
        pass


def client_with_two_accounts(responses, quota=None, **kw):
    session = Scripted(responses)
    yt = YouTubeClient(
        client_id="proj-one.apps.googleusercontent.com",
        client_secret="secret-one",
        refresh_token="refresh-one",
        extra_oauth=[{
            "client_id": "proj-two.apps.googleusercontent.com",
            "client_secret": "secret-two",
            "refresh_token": "refresh-two",
        }],
        quota_state=quota,
        session=session,
        free_search=False,
        **kw,
    )
    # Token refresh is not what these tests are about.
    yt._access_token = "token-account-1"
    yt._access_expiry = 9e12
    return yt, session


def insert(yt, video="vid00000001"):
    return yt.add_to_playlist("PLtarget0000000000000000000000", video)


# ------------------------------------------------------------------ failover

def test_a_real_quota_error_hands_over_to_account_two_and_retries_the_song():
    yt, session = client_with_two_accounts([
        quota_error(),                      # account 1 is out of quota
        Resp(200, {"id": "item1"}),         # the SAME insert, on account 2
    ])
    insert(yt)

    assert yt.oauth_account == 2, "did not move to the second account"
    assert yt.oauth_switches == 1
    assert len(session.calls) == 2, "the song was dropped at the handover"
    assert session.calls[1]["headers"]["Authorization"] == (
        "Bearer tok-proj-two.apps.googleusercontent.com"
    ), "the retry did not use account 2's token"
    assert session.refreshes[-1]["client_id"] == "proj-two.apps.googleusercontent.com"
    assert session.refreshes[-1]["refresh_token"] == "refresh-two"
    assert yt.quota.account_exhausted("0")
    assert not yt.quota.account_exhausted("1")


def test_the_first_account_is_not_capped_by_hand():
    """40/day must not stop account 1 while Google still has quota."""
    yt, _ = client_with_two_accounts(
        [Resp(200, {"id": "x"})] * 60, max_inserts_per_day=40,
    )
    yt.quota.inserts = 40
    yt.quota.units_used = 9000
    for _ in range(5):
        insert(yt)
    assert yt.oauth_switches == 0, "switched before Google complained"
    assert yt.oauth_account == 1


def test_a_single_account_still_honours_the_local_cap():
    """No spare configured = the old safety cap still applies."""
    session = Scripted([Resp(200, {"id": "x"})] * 2)
    yt = YouTubeClient(
        client_id="one.apps.googleusercontent.com", client_secret="s",
        refresh_token="r", session=session, max_inserts_per_day=2, free_search=False,
    )
    yt._access_token, yt._access_expiry = "t", 9e12
    insert(yt)
    insert(yt)
    with pytest.raises(QuotaExceeded):
        insert(yt)


def test_all_accounts_empty_ends_the_run_gracefully():
    yt, _ = client_with_two_accounts([quota_error(), quota_error()])
    with pytest.raises(QuotaExceeded) as exc:
        insert(yt)
    assert "2 YouTube OAuth accounts" in str(exc.value)
    assert yt.quota.account_exhausted("0") and yt.quota.account_exhausted("1")


def test_a_rate_limit_is_retried_not_treated_as_an_empty_account():
    """429 is per-second, not the day's quota: never burn a spare account on it."""
    yt, session = client_with_two_accounts([
        Resp(429, {}), Resp(200, {"id": "ok"}),
    ])
    import phonksync.youtube as mod
    orig_sleep = mod.time.sleep
    mod.time.sleep = lambda *_: None
    try:
        insert(yt)
    finally:
        mod.time.sleep = orig_sleep
    assert yt.oauth_switches == 0
    assert yt.oauth_account == 1
    assert len(session.calls) == 2


def test_a_forbidden_that_is_not_quota_does_not_switch_accounts():
    yt, _ = client_with_two_accounts([
        Resp(403, {"error": {"errors": [{"reason": "forbidden"}]}}),
    ])
    with pytest.raises(AuthError):
        insert(yt)
    assert yt.oauth_switches == 0


def test_the_exhausted_account_is_remembered_for_the_rest_of_the_day():
    quota = QuotaState()
    yt, _ = client_with_two_accounts([quota_error(), Resp(200, {"id": "a"})], quota=quota)
    insert(yt)
    saved = QuotaState.from_dict(json.loads(json.dumps(quota.to_dict())))

    # A later run the same day must START on account 2, not re-probe account 1.
    yt2, session2 = client_with_two_accounts([Resp(200, {"id": "b"})], quota=saved)
    yt2._access_token, yt2._access_expiry = "t", 9e12
    assert yt2.oauth_account == 2
    insert(yt2)
    assert len(session2.calls) == 1, "wasted a call on the known-empty account"


def test_the_next_utc_day_starts_on_account_one_again():
    quota = QuotaState()
    quota.date = "2000-01-01"
    quota.exhaust_account("0")
    yt, _ = client_with_two_accounts([Resp(200, {"id": "a"})], quota=quota)
    assert yt.oauth_account == 1
    assert not yt.quota.account_exhausted("0")


def test_three_accounts_chain_in_order():
    session = Scripted([quota_error(), quota_error(), Resp(200, {"id": "ok"})])
    yt = YouTubeClient(
        client_id="one", client_secret="s1", refresh_token="r1",
        extra_oauth=[
            {"client_id": "two", "client_secret": "s2", "refresh_token": "r2"},
            {"client_id": "three", "client_secret": "s3", "refresh_token": "r3"},
        ],
        session=session, free_search=False,
    )
    yt._access_token, yt._access_expiry = "t0", 9e12
    insert(yt)
    assert yt.oauth_account == 3
    assert yt.oauth_switches == 2
    assert [c["headers"]["Authorization"] for c in session.calls] == [
        "Bearer t0", "Bearer tok-two", "Bearer tok-three",
    ]


def test_per_account_usage_is_recorded_separately():
    yt, _ = client_with_two_accounts([Resp(200, {"id": "a"})])
    insert(yt)
    assert yt.quota.account_units("0") == COST_PLAYLIST_INSERT
    assert yt.quota.account("0")["inserts"] == 1
    assert yt.quota.account_units("1") == 0


# ------------------------------------------------------------------- secrets

def test_numbered_secrets_become_spare_accounts(monkeypatch):
    monkeypatch.setenv("YT_REFRESH_TOKEN", "primary")
    monkeypatch.setenv("YT_CLIENT_ID_2", "two")
    monkeypatch.setenv("YT_CLIENT_SECRET_2", "s2")
    monkeypatch.setenv("YT_REFRESH_TOKEN_2", "r2")
    monkeypatch.setenv("YT_REFRESH_TOKEN_4", "r4")     # a gap must not hide _4
    s = Secrets.from_env()
    assert s.youtube_refresh_token == "primary"
    assert [a["refresh_token"] for a in s.youtube_extra_oauth] == ["r2", "r4"]


def test_one_json_secret_can_hold_every_spare_account(monkeypatch):
    monkeypatch.setenv("YT_REFRESH_TOKEN", "primary")
    monkeypatch.setenv("YOUTUBE_OAUTH_ACCOUNTS", json.dumps([
        {"client_id": "two", "client_secret": "s2", "refresh_token": "r2"},
        {"client_id": "three", "client_secret": "s3", "refresh_token": "r3"},
    ]))
    s = Secrets.from_env()
    assert [a["client_id"] for a in s.youtube_extra_oauth] == ["two", "three"]


def test_a_broken_json_secret_is_a_config_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("YOUTUBE_OAUTH_ACCOUNTS", "{not json")
    with pytest.raises(ConfigError):
        Secrets.from_env()


def test_no_spare_secrets_means_no_failover(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("YT_", "YOUTUBE_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YT_REFRESH_TOKEN", "primary")
    s = Secrets.from_env()
    assert s.youtube_extra_oauth == []


def test_the_workflow_passes_the_spare_secrets_through():
    import yaml
    from pathlib import Path

    wf = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/sync.yml")
        .read_text(encoding="utf-8")
    )
    envs = [
        s.get("env", {}) for s in wf["jobs"]["sync"]["steps"] if isinstance(s, dict)
    ]
    assert any("YT_REFRESH_TOKEN_2" in e for e in envs)
    assert any("YOUTUBE_OAUTH_ACCOUNTS" in e for e in envs)


def test_a_mistyped_spare_token_is_flagged_before_the_run(monkeypatch):
    """A bad spare would otherwise only surface at the handover, mid-run."""
    monkeypatch.setenv("YT_REFRESH_TOKEN", "1//good-primary")
    monkeypatch.setenv("YT_CLIENT_ID_2", "two")
    monkeypatch.setenv("YT_REFRESH_TOKEN_2", "ya29.an-access-token-by-mistake")
    problems = Secrets.from_env().token_problems()
    assert any("account #2" in p and "access token" in p for p in problems)
    assert not any("account #2" not in p for p in problems), "primary is fine"


# ------------------------------------------- pasting the JSON, not bare values

CLIENT_JSON = {
    "installed": {
        "client_id": "two.apps.googleusercontent.com",
        "client_secret": "s2",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def _account_from(shape):
    """Parse one pasted shape all the way to the credential the client will use."""
    import os

    for key in list(os.environ):
        if key.startswith(("YT_", "YOUTUBE_")):
            os.environ.pop(key, None)
    os.environ["YT_REFRESH_TOKEN"] = "1//primary"
    os.environ["YOUTUBE_OAUTH_ACCOUNTS"] = json.dumps(shape)
    s = Secrets.from_env()
    yt = YouTubeClient(
        refresh_token=s.youtube_refresh_token,
        extra_oauth=s.youtube_extra_oauth, free_search=False,
    )
    assert len(yt._oauth_creds) == 2, "the spare account was dropped"
    return yt._oauth_creds[1]


@pytest.mark.parametrize("shape_name", [
    "client_json_text", "client_json_object", "client_json_is_the_item",
    "flat_token_json", "web_client", "single_object_not_a_list",
])
def test_every_json_shape_a_google_download_comes_in(monkeypatch, shape_name):
    shapes = {
        "client_json_text": [
            {"client_json": json.dumps(CLIENT_JSON), "refresh_token": "1//r2"}],
        # Pasting the JSON inside the list makes it a nested OBJECT. str() on it
        # produced Python repr, which is not JSON and killed the whole run.
        "client_json_object": [{"client_json": CLIENT_JSON, "refresh_token": "1//r2"}],
        "client_json_is_the_item": [{**CLIENT_JSON, "refresh_token": "1//r2"}],
        "flat_token_json": [{
            "client_id": "two.apps.googleusercontent.com",
            "client_secret": "s2", "refresh_token": "1//r2",
        }],
        "web_client": [{"web": CLIENT_JSON["installed"], "refresh_token": "1//r2"}],
        "single_object_not_a_list": {**CLIENT_JSON, "refresh_token": "1//r2"},
    }
    cred = _account_from(shapes[shape_name])
    assert cred["client_id"] == "two.apps.googleusercontent.com"
    assert cred["client_secret"] == "s2"
    assert cred["refresh_token"] == "1//r2"


@pytest.mark.parametrize("secret_name", [
    "YT_CLIENT_JSON_2", "YT_TOKEN_JSON_2", "YOUTUBE_OAUTH_TOKEN_JSON_2",
    "YT_OAUTH_TOKEN_JSON_2", "YOUTUBE_CLIENT_JSON_2",
])
def test_a_numbered_secret_can_hold_the_whole_json_download(monkeypatch, secret_name):
    import os

    for key in list(os.environ):
        if key.startswith(("YT_", "YOUTUBE_")):
            os.environ.pop(key, None)
    monkeypatch.setenv("YT_REFRESH_TOKEN", "1//primary")
    monkeypatch.setenv(secret_name, json.dumps({**CLIENT_JSON, "refresh_token": "1//r2"}))
    s = Secrets.from_env()
    yt = YouTubeClient(
        refresh_token=s.youtube_refresh_token,
        extra_oauth=s.youtube_extra_oauth, free_search=False,
    )
    assert len(yt._oauth_creds) == 2
    assert yt._oauth_creds[1]["client_id"] == "two.apps.googleusercontent.com"
    assert yt._oauth_creds[1]["refresh_token"] == "1//r2"


def test_the_workflow_passes_every_json_secret_name_the_parser_reads():
    """A name the parser accepts but the workflow drops is silently ignored."""
    import yaml
    from pathlib import Path

    wf = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/sync.yml")
        .read_text(encoding="utf-8")
    )
    passed = set()
    for step in wf["jobs"]["sync"]["steps"]:
        if isinstance(step, dict):
            passed.update((step.get("env") or {}).keys())
    for n in range(2, 6):
        for name in (
            f"YT_CLIENT_JSON_{n}", f"YOUTUBE_CLIENT_JSON_{n}", f"YT_TOKEN_JSON_{n}",
            f"YOUTUBE_OAUTH_TOKEN_JSON_{n}", f"YT_OAUTH_TOKEN_JSON_{n}",
            f"YOUTUBE_CLIENT_ID_{n}", f"YOUTUBE_CLIENT_SECRET_{n}",
            f"YOUTUBE_REFRESH_TOKEN_{n}",
        ):
            assert name in passed, f"{name} is parsed but never reaches the run"


def test_a_client_json_without_a_refresh_token_is_flagged_early(monkeypatch):
    monkeypatch.setenv("YT_REFRESH_TOKEN", "1//primary")
    monkeypatch.setenv("YT_CLIENT_JSON_2", json.dumps(CLIENT_JSON))
    problems = Secrets.from_env().token_problems()
    assert any("account #2" in p and "no refresh token" in p for p in problems)


# ------------------------------- a half-configured spare must not break run 1

def _secrets_with(monkeypatch, **env):
    import os

    for key in list(os.environ):
        if key.startswith(("YT_", "YOUTUBE_")):
            os.environ.pop(key, None)
    monkeypatch.setenv("YT_REFRESH_TOKEN", "1//primary")
    monkeypatch.setenv("YT_CLIENT_ID", "one.apps.googleusercontent.com")
    monkeypatch.setenv("YT_CLIENT_SECRET", "s1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Secrets.from_env()


def test_a_refresh_token_without_its_client_is_reported_before_the_run(monkeypatch):
    """YT_REFRESH_TOKEN_2 alone: the token only works with its own client."""
    s = _secrets_with(monkeypatch, YT_REFRESH_TOKEN_2="1//second")
    problems = s.token_problems()
    assert any(
        "account #2" in p and "no OAuth client" in p and "YT_CLIENT_JSON_2" in p
        for p in problems
    ), problems


def test_a_token_plus_the_client_json_is_not_flagged(monkeypatch):
    s = _secrets_with(
        monkeypatch, YT_REFRESH_TOKEN_2="1//second",
        YT_CLIENT_JSON_2=json.dumps(CLIENT_JSON),
    )
    assert s.token_problems() == []


def test_an_unusable_spare_is_never_handed_the_run():
    """Account 1 must keep working; the handover must not die on a bad spare."""
    session = Scripted([quota_error()])
    yt = YouTubeClient(
        client_id="one", client_secret="s1", refresh_token="1//r1",
        extra_oauth=[{"refresh_token": "1//r2"}],       # no client at all
        session=session, free_search=False,
    )
    yt._access_token, yt._access_expiry = "t", 9e12
    assert yt.oauth_account_count == 2
    assert yt.oauth_usable_count == 1
    assert not yt.oauth_failover
    assert yt._next_oauth_account() is None
    with pytest.raises(QuotaExceeded):
        insert(yt)
    assert yt.oauth_account == 1, "switched to an account that cannot write"


def test_an_unusable_spare_does_not_lift_the_local_cap():
    """No working spare = the old single-account safety cap still applies."""
    session = Scripted([Resp(200, {"id": "x"})] * 2)
    yt = YouTubeClient(
        client_id="one", client_secret="s1", refresh_token="1//r1",
        extra_oauth=[{"refresh_token": "1//r2"}],
        session=session, max_inserts_per_day=2, free_search=False,
    )
    yt._access_token, yt._access_expiry = "t", 9e12
    insert(yt)
    insert(yt)
    with pytest.raises(QuotaExceeded):
        insert(yt)


def test_a_good_spare_is_still_used_when_a_bad_one_sits_before_it():
    session = Scripted([quota_error(), Resp(200, {"id": "ok"})])
    yt = YouTubeClient(
        client_id="one", client_secret="s1", refresh_token="1//r1",
        extra_oauth=[
            {"refresh_token": "1//broken"},                      # #2 unusable
            {"client_id": "three", "client_secret": "s3", "refresh_token": "1//r3"},
        ],
        session=session, free_search=False,
    )
    yt._access_token, yt._access_expiry = "t", 9e12
    assert yt.oauth_failover
    insert(yt)
    assert yt.oauth_account == 3, "did not skip the unusable account #2"
    assert yt.oauth_switches == 1
