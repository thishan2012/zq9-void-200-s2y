"""The workflows are part of the product: keep them valid and wired up.

A YAML typo or a forgotten `env:` line is invisible until a run silently uses
the defaults — which is exactly how a "fast" setting never reached the run.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")  # PyYAML is in requirements.txt

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _steps(doc: dict, job: str = "sync") -> dict[str, dict]:
    return {s.get("name", ""): s for s in doc["jobs"][job]["steps"]}


def test_every_input_is_passed_to_the_run():
    """Each dispatch input must reach the sync step, or it does nothing."""
    doc = _load("sync.yml")
    inputs = doc[True]["workflow_dispatch"]["inputs"]
    env = _steps(doc)["Run sync"]["env"]
    expected = {
        "max_tracks": "MAX_TRACKS_PER_RUN",
        "dry_run": "DRY_RUN",
        "request_delay": "REQUEST_DELAY_SECONDS",
        "free_search_pause": "YOUTUBE_FREE_SEARCH_PAUSE",
        "results_per_query": "SEARCH_RESULTS_PER_QUERY",
        "max_inserts": "MAX_INSERTS_PER_DAY",
        "backend": "SPOTIFY_BACKEND",
    }
    for input_name, env_name in expected.items():
        assert input_name in inputs, f"input {input_name} disappeared"
        assert f"inputs.{input_name}" in env.get(env_name, ""), (
            f"{input_name} is not passed as {env_name}"
        )


def test_the_run_log_is_kept_and_mirrored_to_the_summary():
    doc = _load("sync.yml")
    steps = _steps(doc)
    run_step = steps["Run sync"]["run"]
    assert "tee run.log" in run_step
    assert "GITHUB_STEP_SUMMARY" in run_step
    # a failed run must still publish the log: capture the code, summarise, then exit
    assert "|| code=$?" in run_step and 'exit "$code"' in run_step
    keep = steps["Keep the run log"]
    assert keep["uses"].startswith("actions/upload-artifact")
    assert "run.log" in str(keep["with"]["path"])
    assert keep["if"].startswith("${{ always()")


def test_no_step_reads_a_secret_into_its_name_or_the_summary():
    """The summary and artifact names must stay secret-free."""
    doc = _load("sync.yml")
    for name, step in _steps(doc).items():
        assert "secrets." not in name
        # the artifact NAME must not interpolate a secret
        if "with" in step and "name" in step["with"]:
            assert "secrets." not in str(step["with"]["name"]), name


def test_tests_workflow_runs_on_every_branch_and_uses_an_interpretable_runner():
    doc = _load("tests.yml")
    assert "push" in doc[True]
    assert "pull_request" in doc[True]
    versions = doc["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    assert "3.11" in [str(v) for v in versions]


def test_the_sync_job_has_a_manual_switch_for_youtube_writes_only():
    """A run that only adds the pending backlog must not spend search budget."""
    doc = _load("sync.yml")
    inputs = doc[True]["workflow_dispatch"]["inputs"]
    env = _steps(doc)["Run sync"]["env"]
    assert "writes_only" in inputs
    assert "inputs.writes_only" in env.get("YOUTUBE_WRITES_ONLY", "")


def test_the_wall_clock_line_names_the_pacing_it_actually_used():
    """`pause= results=` read like "zero pause"; empty means "use config.json"."""
    step = _steps(_load("sync.yml"))["Run time"]
    assert "YOUTUBE_FREE_SEARCH_PAUSE" in str(step.get("env", {}))
    script = step["run"]
    assert "YOUTUBE_FREE_SEARCH_PAUSE:-" in script
    assert "config.json" in script
    assert 'pause=${{ inputs.free_search_pause }}' not in script


def test_a_writes_only_run_is_visible_in_the_summary():
    script = _steps(_load("sync.yml"))["Run time"]["run"]
    assert "inputs.writes_only" in script


def test_the_spotify_write_probe_is_manual_only_and_publishes_its_answer():
    """Logs are not downloadable here; the probe writes its answer to a branch."""
    doc = _load("tests.yml")
    triggers = doc[True]
    assert "workflow_dispatch" in triggers, "the probe needs a manual trigger"
    probe = doc["jobs"]["spotify-write-probe"]
    assert "inputs.spotify_write_probe" in str(probe.get("if", ""))
    steps = {s.get("name", ""): s for s in probe["steps"]}
    run = next(v for k, v in steps.items() if "One real write" in k)
    assert "spotify-write-test" in run["run"]
    assert "GITHUB_STEP_SUMMARY" in run["run"]
    publish = next(v for k, v in steps.items() if "spotify-probe branch" in k)
    assert "diagnostics/spotify-write-probe-" in publish["run"]
    assert "git push" in publish["run"]
    # A probe once reported success while the push silently did nothing
    # (`git push ... || true` on a non-fast-forward). It must rebase onto the
    # branch and try again, and it must keep the inputs with the answer.
    assert "git fetch origin spotify-probe" in publish["run"]
    assert "attempt" in publish["run"]
    attach = next(v for k, v in steps.items() if "artifact" in k)
    assert "PROBE_INPUTS" in attach["run"]


def test_the_sync_workflow_can_reset_the_state_db():
    """Actions → Run workflow → reset = parked|quota|all, with a backup kept."""
    doc = _load("sync.yml")
    inputs = doc[True]["workflow_dispatch"]["inputs"]
    assert "reset" in inputs
    steps = _steps(doc)
    step = steps["Reset the state DB (optional)"]
    assert "inputs.reset" in str(step.get("if", ""))
    assert "phonksync reset" in step["run"]
    assert "--yes" in step["run"]          # the workflow must not stop to ask
    assert "GITHUB_STEP_SUMMARY" in step["run"]
    commit = steps["Commit sync state"]["run"]
    assert "sync-state.json.bak-" in commit, "the pre-reset copy was not committed"


def test_the_probe_can_reproduce_a_batch_write():
    doc = _load("tests.yml")
    inputs = doc[True]["workflow_dispatch"]["inputs"]
    assert "spotify_probe_batch" in inputs
    steps = {s.get("name", ""): s for s in doc["jobs"]["spotify-write-probe"]["steps"]}
    run = next(v for k, v in steps.items() if "One real write" in k)["run"]
    assert "--batch" in run and "inputs.spotify_probe_batch" in run


def test_credential_check_is_manual_readonly_and_live():
    """A one-click secret check must not sync, write, or leak token prefixes."""
    doc = _load("credential-check.yml")
    triggers = doc[True]
    assert "workflow_dispatch" in triggers
    assert "schedule" not in triggers and "push" not in triggers
    assert doc["permissions"]["contents"] == "read"
    steps = _steps(doc, job="check")
    run = steps["Live credential check (read-only)"]["run"]
    assert "phonksync check --live" in run
    assert "youtube-auth --check-token" in run
    assert "GITHUB_STEP_SUMMARY" in run
    assert "redact" in run
    for name, step in steps.items():
        assert "secrets." not in name
        if "with" in step and "name" in step["with"]:
            assert "secrets." not in str(step["with"]["name"])


def test_the_run_summary_says_whether_the_run_could_write():
    """Run 27: 59s, green, 'Run sync: success' — and `Commit sync state: skipped`.
    A dry run and a missing secret look identical from outside; the summary must
    name the inputs so that cannot happen silently again."""
    doc = _load("sync.yml")
    steps = _steps(doc)
    runtime = steps["Run time"]["run"]
    assert "SPOTIFY_COOKIE" in runtime
    assert "dry_run" in runtime
    assert "MISSING" in runtime
    # The receipt lives in the backlog step: it must not be skipped on a live run.
    backlog = steps["Parked Spotify backlog"]
    assert "dry_run" not in str(backlog.get("if", "")), (
        "the write receipt is skipped on dry runs, where its evidence is needed"
    )


def test_the_sync_and_the_probe_share_the_spotify_environment():
    """Run 28 parked all six tracks with 'Spotify asked to slow down' while the
    probe wrote fine. The probe's env had four secrets the sync never had, and
    the sync took the proxy from a repo variable the probe does not use."""
    doc = _load("sync.yml")
    env = _steps(doc)["Run sync"]["env"]
    for name in ("SPOTIFY_TOTP_VER", "SPOTIFY_TOTP_CIPHER",
                 "SPOTIFY_WEB_TOKEN", "SPOTIFY_ENCRYPTED_TOKEN"):
        assert name in env, f"the probe sets {name}; the sync must too"
    # A repo *variable* proxy is the silent difference: drop it.
    assert "vars.SPOTIFY_PROXY_URL" not in env["SPOTIFY_PROXY_URL"]
    summary = _steps(doc)["Run time"]["run"]
    assert "Spotify proxy" in summary, "the run must say whether a proxy was used"


def test_the_reset_input_is_never_interpolated_into_the_shell():
    """`${{ inputs.reset }}` inside `run:` is shell injection, and it also
    word-splits a comma list into an unknown scope. It must travel via env."""
    steps = _steps(_load("sync.yml"))
    step = steps["Reset the state DB (optional)"]
    script = step["run"]
    assert "inputs.reset" not in script, "user text must not be pasted into the script"
    assert step["env"]["RESET_SCOPES"] == "${{ inputs.reset }}"
    assert 'reset "$RESET_SCOPES" --yes' in script


def test_a_refused_reset_fails_the_run_instead_of_looking_green():
    """`|| true` left the job green with nothing reset, then synced the old state."""
    script = _steps(_load("sync.yml"))["Reset the state DB (optional)"]["run"]
    assert "|| true" not in script.split("grep -E")[0], "the reset exit code was swallowed"
    assert "|| code=$?" in script
    assert "::error title=State reset refused::" in script
    assert 'exit "$code"' in script


def test_a_successful_sync_summary_does_not_ask_what_went_wrong():
    """`if [ -s run.log ]` was true on every normal run.

    The summary then printed `### ❌ What went wrong` and grepped the harmless
    `❌ Failed / unmatched: 0` line, so a green run read like a failure.
    """
    script = _steps(_load("sync.yml"))["Run sync"]["run"]
    assert 'if [ "$code" -ne 0 ] && [ -s run.log ]; then' in script
    assert 'echo "### ❌ What went wrong"' in script
    assert 'echo "### ✅ The sync command exited 0' in script
    # The failure heading must be guarded by the exit code, not log presence.
    before_heading = script.split('echo "### ❌ What went wrong"', 1)[0]
    assert 'if [ -s run.log ]; then' not in before_heading.split('tail -n 120 run.log')[-1]


def test_the_sync_schedule_is_four_paced_runs_per_day():
    """Auto runs: every 6h just after YouTube's Pacific reset, 100 tracks/run."""
    doc = _load("sync.yml")
    schedules = doc[True]["schedule"]
    assert schedules == [{
        "cron": "15 0,6,12,18 * * *",
        "timezone": "America/Los_Angeles",
    }]
    env = _steps(doc)["Run sync"]["env"]
    assert env["MAX_TRACKS_PER_RUN"] == (
        "${{ github.event_name == 'schedule' && '100' || inputs.max_tracks }}"
    )
    assert env["DRY_RUN"] == (
        "${{ github.event_name == 'schedule' && 'false' || inputs.dry_run }}"
    )
    steps = _steps(doc)
    assert "github.event_name == 'workflow_dispatch'" in str(steps["Reset the state DB (optional)"]["if"])
    assert "github.event_name == 'workflow_dispatch'" in str(steps["Fill source cache"]["if"])
    assert "github.event_name == 'schedule'" in str(steps["Commit sync state"]["if"])
    runtime = steps["Run time"]
    assert runtime["env"]["EFFECTIVE_MAX_TRACKS"] == env["MAX_TRACKS_PER_RUN"]
    assert "EFFECTIVE_MAX_TRACKS" in runtime["run"]


def test_the_default_insert_target_is_195_per_oauth_account():
    import json
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert cfg["youtube_max_inserts_per_day"] == 195
    from phonksync.settings import Config
    assert Config().youtube_max_inserts_per_day == 195
