"""OAuth account failover: pace each account, then continue on the next one.

The agreed pacing target is 195 playlist adds per usable OAuth account per
Pacific day. A real Google quotaExceeded still hands over immediately, even if
the account has not reached the local target yet.
"""
import json

import pytest

from phonksync.errors import AuthError, QuotaExceeded
from phonksync.settings import ConfigError, Secrets
from phonksync.youtube import COST_PLAYLIST_INSERT, QuotaState, YouTubeClient


@pytest.fixture(autouse=True)
def _skip_shorts_probe_for_existing_offline_tests(monkeypatch):
    # These tests exercise API authentication/quota, not public-page detection.
    monkeypatch.setattr(YouTubeClient, "ensure_not_short", lambda self, video_id: None)
    monkeypatch.setattr(YouTubeClient, "ensure_video_eligible", lambda self, video_id, duration_s=None: None)


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
    # Token refresh is not what these tests are about. If persisted quota made
    # the client start on a spare, leave its token empty so the first request
    # refreshes that spare instead of pretending to be account 1.
    if yt.oauth_account == 1:
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


def test_the_local_insert_target_is_per_account_and_hands_over_at_195():
    """Account 1 stops at 195 adds; account 2 continues the same run."""
    yt, session = client_with_two_accounts(
        [Resp(200, {"id": "x"})] * 200, max_inserts_per_day=195,
    )
    for _ in range(195):
        insert(yt)
    assert yt.oauth_account == 1
    assert yt.oauth_switches == 0
    assert yt.quota.account("0")["inserts"] == 195

    insert(yt)  # the 196th add must continue on account 2, not stop
    assert yt.oauth_account == 2
    assert yt.oauth_switches == 1
    assert yt.quota.account("0")["inserts"] == 195
    assert yt.quota.account("1")["inserts"] == 1
    assert len(session.calls) == 196
    assert session.calls[-1]["headers"]["Authorization"] == (
        "Bearer tok-proj-two.apps.googleusercontent.com"
    )


def test_a_zero_insert_target_means_no_local_cap():
    """0 keeps the old behaviour: only Google's real quotaExceeded hands over."""
    yt, _ = client_with_two_accounts(
        [Resp(200, {"id": "x"})] * 60, max_inserts_per_day=0,
    )
    yt.quota.inserts = 40
    yt.quota.units_used = 9000
    for _ in range(5):
        insert(yt)
    assert yt.oauth_switches == 0, "switched before Google complained"
    assert yt.oauth_account == 1


def test_the_insert_target_is_remembered_across_runs_the_same_pacific_day():
    quota = QuotaState()
    yt, _ = client_with_two_accounts(
        [Resp(200, {"id": "x"})] * 195, quota=quota, max_inserts_per_day=195,
    )
    for _ in range(195):
        insert(yt)
    saved = QuotaState.from_dict(json.loads(json.dumps(quota.to_dict())))

    yt2, session2 = client_with_two_accounts(
        [Resp(200, {"id": "y"})], quota=saved, max_inserts_per_day=195,
    )
    assert yt2.oauth_account == 2, "a later run re-probed the capped account"
    insert(yt2)
    assert len(session2.calls) == 1
    assert yt2.quota.account("1")["inserts"] == 1


def test_all_accounts_at_the_insert_target_stop_cleanly():
    quota = QuotaState()
    quota.rollover()  # date it today, or the client would roll the counters over
    quota.account("0")["inserts"] = 195
    quota.account("1")["inserts"] = 195
    yt, session = client_with_two_accounts([], quota=quota, max_inserts_per_day=195)
    yt._access_token, yt._access_expiry = "t", 9e12  # no refresh before the budget check
    with pytest.raises(QuotaExceeded) as exc:
        insert(yt)
    assert "daily insert target" in str(exc.value)
    assert session.calls == []


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


def test_the_next_pacific_day_starts_on_account_one_again():
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


# --------------------- the check command must show the spare, in the browser

def _run_check(monkeypatch, capsys, live=False, **env):
    import os
    from phonksync.__main__ import main

    for key in list(os.environ):
        if key.startswith(("YT_", "YOUTUBE_", "SPOTIFY_")):
            os.environ.pop(key, None)
    base = {
        "SPOTIFY_COOKIE": "sp_dc=x",
        "SPOTIFY_SOURCE_PLAYLISTS": "1G84VxuubZb4cofjVmk4OD",
        "SPOTIFY_TARGET_PLAYLIST": "6YBLs3ihGXHCSAESWGRML4",
        "YOUTUBE_ENABLED": "true",
        "YOUTUBE_TARGET_PLAYLIST": "PLtarget0000000000000000000000",
        "YT_REFRESH_TOKEN": "1//primary",
        "YT_CLIENT_ID": "one.apps.googleusercontent.com",
        "YT_CLIENT_SECRET": "s1",
    }
    base.update(env)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    main(["check"] + (["--live"] if live else []))
    return capsys.readouterr().out


def test_check_reports_a_spare_that_cannot_write(monkeypatch, capsys):
    out = _run_check(monkeypatch, capsys, YT_REFRESH_TOKEN_2="1//second")
    assert "2 configured, 1 usable" in out
    assert "No spare OAuth account is usable" in out


def test_check_confirms_failover_when_the_spare_is_complete(monkeypatch, capsys):
    out = _run_check(
        monkeypatch, capsys,
        YT_REFRESH_TOKEN_2="1//second", YT_CLIENT_JSON_2=json.dumps(CLIENT_JSON),
    )
    assert "2 configured, 2 usable" in out
    assert "Quota failover armed" in out
    assert "No spare" not in out


def test_check_says_nothing_about_failover_when_no_spare_exists(monkeypatch, capsys):
    out = _run_check(monkeypatch, capsys)
    assert "1 configured, 1 usable" in out
    assert "No spare OAuth account is usable" not in out


def test_the_credential_check_workflow_passes_the_spare_secrets():
    """Without this the browser check cannot see account 2 at all."""
    import yaml
    from pathlib import Path

    wf = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/credential-check.yml")
        .read_text(encoding="utf-8")
    )
    passed = set()
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if isinstance(step, dict):
                passed.update((step.get("env") or {}).keys())
    for name in ("YT_REFRESH_TOKEN_2", "YT_CLIENT_JSON_2",
                 "YOUTUBE_OAUTH_TOKEN_JSON_2", "YOUTUBE_OAUTH_ACCOUNTS"):
        assert name in passed, f"{name} never reaches the credential check"


# -------------------- the user's actual secret names: audit, don't guess values

def _oauth_env(monkeypatch, **values):
    import os
    for key in list(os.environ):
        if key.startswith(("YT_", "YOUTUBE_")):
            monkeypatch.delenv(key, raising=False)
    base = {
        "YT_REFRESH_TOKEN": "1//primary-test-token",
        "YT_CLIENT_ID": "one.apps.googleusercontent.com",
        "YT_CLIENT_SECRET": "primary-test-secret",
    }
    base.update(values)
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    return Secrets.from_env()


def test_an_id_and_token_without_the_second_clients_secret_are_not_ready(monkeypatch):
    secrets = _oauth_env(
        monkeypatch, YT_CLIENT_ID_2="two.apps.googleusercontent.com",
        YT_REFRESH_TOKEN_2="1//second-test-token",
        YOUTUBE_OAUTH_TOKEN_JSON_2=json.dumps({"refresh_token": "1//embedded-test-token"}),
    )
    problems = secrets.token_problems()
    assert any("account #2" in p and "YT_CLIENT_SECRET_2" in p for p in problems)
    yt = YouTubeClient(refresh_token=secrets.youtube_refresh_token,
                       extra_oauth=secrets.youtube_extra_oauth, free_search=False)
    assert yt.oauth_usable_count == 1
    assert yt._next_oauth_account() is None


def test_a_secret_inside_the_json_means_no_extra_github_secret_is_needed(monkeypatch):
    secrets = _oauth_env(
        monkeypatch, YT_CLIENT_ID_2="two.apps.googleusercontent.com",
        YT_REFRESH_TOKEN_2="1//second-test-token",
        YOUTUBE_OAUTH_TOKEN_JSON_2=json.dumps(CLIENT_JSON),
    )
    assert secrets.token_problems() == []
    yt = YouTubeClient(refresh_token=secrets.youtube_refresh_token,
                       extra_oauth=secrets.youtube_extra_oauth, free_search=False)
    assert yt.oauth_usable_count == 2
    assert yt._oauth_creds[1]["client_secret"] == "s2"


def test_the_json_can_supply_the_secret_next_to_a_separate_client_id(monkeypatch):
    secrets = _oauth_env(
        monkeypatch, YT_CLIENT_ID_2="two.apps.googleusercontent.com",
        YT_REFRESH_TOKEN_2="1//second-test-token",
        YOUTUBE_OAUTH_TOKEN_JSON_2=json.dumps({"client_secret": "s2"}),
    )
    assert secrets.token_problems() == []


def test_check_does_not_call_a_spare_without_a_secret_usable(monkeypatch, capsys):
    out = _run_check(
        monkeypatch, capsys, YT_CLIENT_ID_2="two.apps.googleusercontent.com",
        YT_REFRESH_TOKEN_2="1//second-test-token",
        YOUTUBE_OAUTH_TOKEN_JSON_2=json.dumps({"refresh_token": "1//embedded-test-token"}),
    )
    assert "2 configured, 1 usable" in out
    assert "YT_CLIENT_SECRET_2" in out
    assert "Quota failover armed" not in out


@pytest.mark.parametrize("filename,job,step_name", [
    ("sync.yml", "sync", "Validate config and secrets"),
    ("sync.yml", "sync", "Run sync"),
    ("credential-check.yml", "check", "Live credential check (read-only)"),
])
def test_every_oauth_input_reaches_the_step_that_actually_needs_it(filename, job, step_name):
    """The old tests unioned all steps: validation had the secrets, Run sync did not."""
    import yaml
    from pathlib import Path
    doc = yaml.safe_load((Path(__file__).resolve().parents[1] /
                          ".github/workflows" / filename).read_text(encoding="utf-8"))
    step = next(s for s in doc["jobs"][job]["steps"] if s.get("name") == step_name)
    env = step.get("env", {})
    names = [f"{prefix}_{n}" for n in range(2, 6) for prefix in (
        "YT_CLIENT_ID", "YOUTUBE_CLIENT_ID", "YT_CLIENT_SECRET", "YOUTUBE_CLIENT_SECRET",
        "YT_REFRESH_TOKEN", "YOUTUBE_REFRESH_TOKEN", "YT_CLIENT_JSON", "YOUTUBE_CLIENT_JSON",
        "YT_TOKEN_JSON", "YOUTUBE_OAUTH_TOKEN_JSON", "YT_OAUTH_TOKEN_JSON",
    )] + ["YOUTUBE_OAUTH_ACCOUNTS"]
    for name in names:
        assert env.get(name) == "${{ secrets." + name + " }}", f"{step_name}: {name}"


def test_credential_check_summary_includes_the_failover_line():
    import yaml
    from pathlib import Path
    doc = yaml.safe_load((Path(__file__).resolve().parents[1] /
                          ".github/workflows/credential-check.yml").read_text(encoding="utf-8"))
    script = next(s for s in doc["jobs"]["check"]["steps"]
                  if s.get("name") == "Live credential check (read-only)")["run"]
    line = next(line for line in script.splitlines() if "check.log | redact" in line)
    for codepoint in (0x2705, 0x274C, 0x26A0, 0x2139, 0x1F50E, 0x1F517, 0x1F645, 0x1F501):
        assert chr(codepoint) in line


def _live_check(monkeypatch, capsys, failures=()):
    from phonksync.spotify import SpotifyClient
    seen = []

    def fake_user(self):
        return "test-user"

    def fake_refresh(self):
        seen.append(self.client_id)
        if self.client_id in failures:
            raise AuthError("test refresh rejected")
        self._access_token = "test-access-token"
        return self._access_token

    def fake_read(self, playlist_id):
        if not self._access_token:
            self._ensure_token()  # the real OAuth read refreshes before the GET
        assert self._access_token == "test-access-token"
        return set()

    monkeypatch.setattr(SpotifyClient, "current_user_id", fake_user)
    monkeypatch.setattr(YouTubeClient, "_ensure_token", fake_refresh)
    monkeypatch.setattr(YouTubeClient, "playlist_video_ids", fake_read)
    out = _run_check(
        monkeypatch, capsys, live=True, YT_CLIENT_ID_2="two.apps.googleusercontent.com",
        YT_CLIENT_SECRET_2="s2", YT_REFRESH_TOKEN_2="1//second-test-token",
    )
    return out, seen


def test_live_credential_check_tests_the_spare_without_adding_videos(monkeypatch, capsys):
    def no_writes(*args, **kwargs):
        raise AssertionError("credential check must not insert videos")
    monkeypatch.setattr(YouTubeClient, "add_to_playlist", no_writes)
    out, seen = _live_check(monkeypatch, capsys)
    assert seen == ["one.apps.googleusercontent.com", "two.apps.googleusercontent.com"]
    assert "YouTube OAuth account #2: token refresh + target playlist read OK" in out


def test_live_credential_check_reports_a_rejected_spare(monkeypatch, capsys):
    out, seen = _live_check(monkeypatch, capsys, failures=("two.apps.googleusercontent.com",))
    assert "YouTube OAuth account #2 live check: test refresh rejected" in out
    assert "token refresh + target playlist read OK" not in out


def test_a_failed_spare_check_is_not_overwritten_by_a_green_primary_check():
    import yaml
    from pathlib import Path
    doc = yaml.safe_load((Path(__file__).resolve().parents[1] /
                          ".github/workflows/credential-check.yml").read_text(encoding="utf-8"))
    script = next(s for s in doc["jobs"]["check"]["steps"]
                  if s.get("name") == "Live credential check (read-only)")["run"]
    assert "code=0" in script
    assert "python -m phonksync check --live > check.log 2>&1 || code=$?" in script
    assert "python -m phonksync youtube-auth --check-token > refresh.log 2>&1 || code=$?" in script
    assert 'exit "$code"' in script


def test_a_broken_primary_does_not_hide_a_working_spare_in_the_live_check(monkeypatch, capsys):
    out, seen = _live_check(monkeypatch, capsys, failures=("one.apps.googleusercontent.com",))
    assert "YouTube OAuth account #1 live check: test refresh rejected" in out
    assert "YouTube OAuth account #2: token refresh + target playlist read OK" in out
    assert "two.apps.googleusercontent.com" in seen


@pytest.mark.parametrize("raw", ['[{"client_id":"x"}]', '{"installed": [1]}'])
def test_a_wrong_json_shape_is_reported_without_a_traceback(monkeypatch, raw):
    secrets = _oauth_env(monkeypatch, YOUTUBE_OAUTH_TOKEN_JSON_2=raw)
    problems = secrets.token_problems()
    assert any("account #2" in p and "JSON" in p for p in problems)
