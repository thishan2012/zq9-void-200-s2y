"""The reset option must accept what the Actions input box tells people to type.

The workflow describes the input as "e.g. parked, quota, stats, all", so commas
are the natural separator. `parked,` used to reach the CLI as an unknown scope:
exit 2, nothing reset — and the workflow's `|| true` left the job green.
"""
import json

import pytest

from phonksync.__main__ import _reset_scopes, main
from phonksync.db import StateDB


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["parked", "quota"], ["parked", "quota"]),
        (["parked, quota"], ["parked", "quota"]),
        (["parked,quota"], ["parked", "quota"]),
        (["parked; quota"], ["parked", "quota"]),
        (["  PARKED ,  Quota  "], ["parked", "quota"]),
        (["all"], ["all"]),
        (["quota, quota"], ["quota"]),
        (['"parked", "quota"'], ["parked", "quota"]),
        ([""], []),
        ([], []),
        (None, []),
    ],
)
def test_scopes_accept_commas_case_and_repeats(raw, expected):
    assert _reset_scopes(raw) == expected


@pytest.fixture
def state(tmp_path):
    """A state file with something in every bucket the reset can clear."""
    from conftest import make_track

    path = tmp_path / "state.json"
    db = StateDB(path)
    track = make_track()
    db.mark_synced(track, "vid00000001", "STRICT", True, True)
    db.add_manual(make_track(id="p" * 22, uri=f"spotify:track:{'p' * 22}"),
                  reason="Spotify asked to slow down")
    db.data["quota"] = {"date": "2026-11-08", "units_used": 473}
    db.save()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"state_path": str(path)}), encoding="utf-8")
    return cfg, path


def test_the_comma_form_really_resets_and_keeps_a_backup(state, capsys):
    cfg, path = state
    assert main(["--config", str(cfg), "reset", "parked, quota", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "🧹 quota" in out and "💾 Backup kept at" in out
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["quota"] == {}
    assert not (data.get("spotify") or {}).get("manual")
    assert data["synced"], "resetting quota must not forget processed tracks"
    assert list(path.parent.glob("state.json.bak-*")), "no way back without a backup"


def test_one_string_with_spaces_still_works(state, capsys):
    """Actions passes the whole box as ONE argument: "parked quota"."""
    cfg, path = state
    assert main(["--config", str(cfg), "reset", "parked quota", "--yes"]) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["quota"] == {}


def test_a_typo_is_refused_loudly_and_changes_nothing(state, capsys):
    cfg, path = state
    before = path.read_bytes()
    assert main(["--config", str(cfg), "reset", "everything", "--yes"]) == 2
    assert path.read_bytes() == before, "a refused reset must not touch the state"
    assert not list(path.parent.glob("state.json.bak-*"))


def test_without_yes_it_is_still_a_dry_run(state, capsys):
    cfg, path = state
    before = path.read_bytes()
    assert main(["--config", str(cfg), "reset", "all"]) == 0
    assert path.read_bytes() == before
    assert "Add --yes" in capsys.readouterr().out
