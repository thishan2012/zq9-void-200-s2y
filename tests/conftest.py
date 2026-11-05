"""Shared test fixtures: a fake YouTube API and tmp-path helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonksync.cache import SearchCache
from phonksync.matcher import Matcher
from phonksync.models import Candidate, Track
from phonksync.overrides import Overrides


class FakeYouTube:
    """In-memory YouTube API. `catalog` maps video_id -> Candidate.

    `search_map` maps a normalized query -> list of video ids.
    """

    def __init__(self, catalog=None, search_map=None):
        self.catalog: dict[str, Candidate] = catalog or {}
        self.search_map: dict[str, list[str]] = search_map or {}
        self.searches: list[str] = []
        self.region_code = "US"

    def search(self, query: str, max_results: int = 10) -> list[str]:
        self.searches.append(" ".join(query.lower().split()))
        key = " ".join(query.lower().split())
        return self.search_map.get(key, [])[:max_results]

    def videos(self, video_ids: list[str]) -> list[Candidate]:
        return [self.catalog[v] for v in video_ids if v in self.catalog]


def make_track(**kw) -> Track:
    base = dict(
        id="aaaaaaaaaaaaaaaaaaaaaa",
        name="MONTAGEM MIAU",
        artists=["Nulteex"],
        album="MONTAGEM MIAU - Single",
        duration_ms=120000,
        isrc="BRABC2400001",
        uri="spotify:track:aaaaaaaaaaaaaaaaaaaaaa",
    )
    base.update(kw)
    return Track(**base)


def make_candidate(vid: str, title: str, channel: str = "Phonk Music", **kw) -> Candidate:
    base = dict(video_id=vid, title=title, channel=channel, description="", duration_s=120, views=1000)
    base.update(kw)
    return Candidate(**base)


@pytest.fixture
def cache(tmp_path):
    return SearchCache(tmp_path / "cache.json", ttl_days=14)


@pytest.fixture
def overrides(tmp_path):
    p = tmp_path / "overrides.json"
    p.write_text('{"overrides": {}}', encoding="utf-8")
    return Overrides(p)


def build_matcher(yt, cache, overrides, **kw) -> Matcher:
    return Matcher(yt, cache, overrides, **kw)
