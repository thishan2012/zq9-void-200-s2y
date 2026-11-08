from conftest import make_track

from phonksync.db import StateDB


def test_synced_roundtrip(tmp_path):
    db = StateDB(tmp_path / "state.json")
    track = make_track()
    assert not db.is_synced(track.id)
    db.mark_synced(track, "vid00000001", "STRICT", True, True)
    db.save()

    db2 = StateDB(tmp_path / "state.json")
    assert db2.is_synced(track.id)
    entry = db2.get_synced(track.id)
    assert entry["youtube_video_id"] == "vid00000001"
    assert entry["method"] == "STRICT"


def test_unmatched_retry_tracking(tmp_path):
    db = StateDB(tmp_path / "state.json")
    track = make_track()
    db.mark_unmatched(track, ["STRICT", "LOOSE"])
    db.mark_unmatched(track, ["STRICT", "LOOSE"])
    assert db.unmatched_tries(track.id) == 2
    assert db.unmatched_last_try(track.id) != ""


def test_method_stats(tmp_path):
    db = StateDB(tmp_path / "state.json")
    db.record_method("STRICT", tried=True, won=True)
    db.record_method("STRICT", tried=True, won=False)
    db.record_method("CHANNEL", tried=True, won=False)
    m = db.data["stats"]["methods"]
    assert m["STRICT"] == {"tried": 2, "won": 1}
    assert m["CHANNEL"] == {"tried": 1, "won": 0}


def test_spotify_only_add_keeps_unmatched(tmp_path):
    db = StateDB(tmp_path / "state.json")
    track = make_track()
    db.mark_unmatched(track, ["STRICT"])
    db.mark_synced(track, "", "", spotify_added=True, youtube_added=False)
    # Unmatched record must survive so YouTube matching is retried later.
    assert db.unmatched_tries(track.id) == 1


def test_quota_counters_persist_across_runs(tmp_path):
    from phonksync.db import StateDB
    from phonksync.youtube import QuotaState

    db = StateDB(tmp_path / "state.json")
    quota = db.quota
    quota.rollover()
    quota.searches = 79
    quota.inserts = 10
    db.save_quota(quota)
    db.save()

    again = StateDB(tmp_path / "state.json").quota
    assert again.searches == 79
    assert again.inserts == 10
    assert again.date == quota.date
    assert QuotaState.from_dict(again.to_dict()).searches == 79


def test_a_parked_track_is_queued_for_the_next_run(tmp_path):
    """Parking is not adding: the track must stay retryable."""
    from phonksync.db import StateDB
    from phonksync.models import Track

    db = StateDB(tmp_path / "state.json")
    track = Track(id="parked000000000000000001", name="LUZ", artists=["HDN"],
                  uri="spotify:track:parked000000000000000001")
    db.add_manual(track, reason="Spotify asked to slow down")
    db.add_pending_spotify(track)
    db.save()

    assert db.manual_uris() == ["spotify:track:parked000000000000000001"]
    assert (db.manual_items()[track.id]["reason"]) == "Spotify asked to slow down"

    # Applying only what is still missing keeps the rest for a later attempt.
    assert db.clear_manual([track.id]) == 1
    assert db.manual_items() == {}
    assert db.pending_spotify()  # untouched
    assert db.clear_pending_spotify([track.id]) == 1
    assert db.pending_spotify() == {}


def test_the_last_write_problem_survives_without_a_log(tmp_path):
    """Job logs are not always downloadable; the state file is."""
    from phonksync.db import StateDB

    db = StateDB(tmp_path / "state.json")
    db.note_write_problem("web-player: no mutation in the answer", route="web-player",
                          account="phonkbot")
    db.save()

    again = StateDB(tmp_path / "state.json")
    problem = again.last_write_problem()
    assert "no mutation" in problem["message"]
    assert problem["route"] == "web-player"
    assert problem["account"] == "phonkbot"


def test_reset_clears_only_what_was_asked_for_and_keeps_a_backup(tmp_path):
    from phonksync.db import StateDB
    from phonksync.models import Track

    path = tmp_path / "state.json"
    db = StateDB(path)
    track = Track(id="keep000000000000000001", name="LUZ", artists=["HDN"],
                  uri="spotify:track:keep000000000000000001")
    db.mark_synced(track, "", "", True, False)
    db.add_manual(track)
    db.add_pending_spotify(track)
    db.data["skipped"][track.id] = {"reason": "no match"}
    db.save()

    backup = db.backup("test")
    assert backup.exists()

    cleared = db.reset(["parked"])
    assert cleared["parked"] == 2  # manual + pending
    assert db.pending_spotify() == {}
    assert db.manual_items() == {}
    assert db.data["synced"], "reset parked must not forget processed tracks"
    assert db.data["skipped"], "reset parked must not forget skipped tracks"
    assert db.data["last_reset_scopes"] == ["parked"]


def test_reset_all_starts_from_scratch_but_keeps_the_version(tmp_path):
    from phonksync.db import StateDB

    db = StateDB(tmp_path / "state.json")
    db.data["synced"]["x" * 22] = {"name": "x"}
    db.data["quota"] = {"date": "2026-11-07", "inserts": 9}
    db.data["stats"]["runs"] = 12
    db.data["diagnostics"] = {"last_write_problem": {"message": "x"}}
    db.reset(["all"])
    assert db.data["synced"] == {}
    assert db.data["quota"] == {}
    assert db.data["stats"]["runs"] == 0
    assert "diagnostics" not in db.data
    assert db.data["version"] == 1


def test_an_unknown_reset_scope_is_an_error(tmp_path):
    import pytest

    from phonksync.db import StateDB

    db = StateDB(tmp_path / "state.json")
    with pytest.raises(ValueError):
        db.reset(["everything"])


def test_an_emptied_bucket_rewrites_the_queue_file(tmp_path):
    """The committed file must not keep listing tracks that already landed.

    Real state after run 42467429607: bucket empty, all 15 tracks added — but
    data/manual-queue.txt still said "6 track(s) that could not be added".
    """
    from conftest import make_track

    queue = tmp_path / "manual-queue.txt"
    db = StateDB(tmp_path / "state.json")
    track = make_track()

    db.add_manual(track, reason="Spotify asked to slow down")
    assert db.write_manual_queue(queue) == 1
    assert track.uri in queue.read_text(encoding="utf-8")

    db.clear_manual([track.id])
    assert db.write_manual_queue(queue) == 0
    text = queue.read_text(encoding="utf-8")
    assert track.uri not in text, "a landed track was still listed as waiting"
    assert "Nothing waiting" in text
