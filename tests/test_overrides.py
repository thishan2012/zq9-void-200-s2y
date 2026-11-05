import json

from conftest import make_track

from phonksync.overrides import Overrides


def test_lookup_by_id_uri_isrc(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text(json.dumps({"overrides": {
        "track000000000000000001": "vid00000001",
        "spotify:track:track000000000000000002": "https://youtu.be/vid00000002",
        "BRABC2400009": "vid00000003",
    }}))
    ov = Overrides(p)
    assert ov.lookup(make_track(id="track000000000000000001")) == "vid00000001"
    assert ov.lookup(make_track(id="track000000000000000002")) == "vid00000002"
    assert ov.lookup(make_track(id="track000000000000000009", isrc="BRABC2400009")) == "vid00000003"
    assert ov.lookup(make_track(id="track000000000000000099", isrc="XXYYY2400000")) is None


def test_invalid_value_ignored(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text(json.dumps({"overrides": {"track000000000000000001": "not a video"}}))
    ov = Overrides(p)
    assert ov.lookup(make_track(id="track000000000000000001")) is None
