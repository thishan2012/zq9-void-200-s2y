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
    publish = next(v for k, v in steps.items() if "Commit the answer" in k)
    assert "diagnostics/spotify-write-probe-" in publish["run"]
    assert "git push" in publish["run"]


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
