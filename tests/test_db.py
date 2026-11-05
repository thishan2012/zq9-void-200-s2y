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
