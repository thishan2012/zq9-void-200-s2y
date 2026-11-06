"""Persistent JSON cache for YouTube searches and video metadata.

The cache lives in `.cache/yt-cache.json` (git-ignored by default, optionally
saved by GitHub Actions cache). It dramatically reduces quota usage because
most fallback methods reuse the same queries across runs.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import Candidate


class SearchCache:
    def __init__(self, path: str | Path, ttl_days: int = 14, max_queries: int = 4000):
        self.path = Path(path)
        self.ttl = ttl_days * 86400
        self.max_queries = max_queries
        self._queries: dict[str, dict[str, Any]] = {}
        self._videos: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        now = time.time()
        for key, entry in (data.get("queries") or {}).items():
            if now - entry.get("ts", 0) <= self.ttl:
                self._queries[key] = entry
        for key, entry in (data.get("videos") or {}).items():
            if now - entry.get("ts", 0) <= self.ttl:
                self._videos[key] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Trim oldest queries if the cache grows too large.
        if len(self._queries) > self.max_queries:
            oldest = sorted(self._queries.items(), key=lambda kv: kv[1].get("ts", 0))
            for key, _ in oldest[: len(self._queries) - self.max_queries]:
                del self._queries[key]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"queries": self._queries, "videos": self._videos},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def size(self) -> int:
        return len(self._queries)

    @staticmethod
    def query_key(query: str, max_results: int, region: str) -> str:
        return f"{region}|{max_results}|{' '.join(query.lower().split())}"

    def get_query(self, key: str) -> list[str] | None:
        entry = self._queries.get(key)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > self.ttl:
            del self._queries[key]
            return None
        return list(entry.get("ids", []))

    def put_query(self, key: str, ids: list[str]) -> None:
        self._queries[key] = {"ts": time.time(), "ids": ids}

    def get_videos(self, ids: list[str]) -> tuple[list[Candidate], list[str]]:
        found: list[Candidate] = []
        missing: list[str] = []
        now = time.time()
        for vid in ids:
            entry = self._videos.get(vid)
            if entry and now - entry.get("ts", 0) <= self.ttl:
                found.append(Candidate(**entry["data"]))
            else:
                missing.append(vid)
        return found, missing

    def put_videos(self, candidates: list[Candidate]) -> None:
        now = time.time()
        for cand in candidates:
            self._videos[cand.video_id] = {"ts": now, "data": asdict(cand)}


class SourceCache:
    """Cache of Spotify playlist reads (1 snapshot call instead of N pages).

    The Web API is what gets rate limited, so on repeat runs we ask for one
    page (``total`` + ``snapshot_id``) and reuse the cached track list when the
    playlist has not changed. A stale cache is still better than a 429 block:
    when Spotify is unreachable we fall back to it and say so in the log.
    """

    def __init__(
        self,
        path: str | Path,
        ttl_hours: int = 12,
        max_playlists: int = 50,
        allow_stale: bool = True,
        stale_days: int = 30,
    ):
        self.path = Path(path)
        self.ttl = max(1, ttl_hours) * 3600
        self.max_playlists = max_playlists
        # A stale read is still a read: while Spotify is throttling, replaying a
        # 30-day-old playlist costs zero API calls.
        self.allow_stale = allow_stale
        self.stale_ttl = max(1, stale_days) * 86400
        self._items: dict[str, dict[str, Any]] = {}
        self._trimmed = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        now = time.time()
        for pid, entry in (data.get("playlists") or {}).items():
            if now - entry.get("ts", 0) <= self.stale_ttl:
                self._items[pid] = entry
            else:
                self._trimmed += 1

    def fresh(self, playlist_id: str) -> bool:
        entry = self._items.get(playlist_id)
        return bool(entry) and time.time() - entry.get("ts", 0) <= self.ttl

    def get(self, playlist_id: str) -> dict[str, Any] | None:
        """Return {"snapshot": str, "tracks": [Track], "fresh": bool} or None."""
        entry = self._items.get(playlist_id)
        if not entry:
            return None
        from .models import Track

        tracks = [
            Track(
                id=t["id"],
                name=t.get("name", ""),
                artists=list(t.get("artists") or []),
                album=t.get("album", ""),
                duration_ms=int(t.get("duration_ms") or 0),
                isrc=t.get("isrc", ""),
                uri=t.get("uri", ""),
                popularity=int(t.get("popularity") or 0),
                sources=list(t.get("sources") or [playlist_id]),
            )
            for t in entry.get("tracks", [])
        ]
        return {
            "snapshot": entry.get("snapshot", ""),
            "tracks": tracks,
            "fresh": self.fresh(playlist_id),
        }

    def age_hours(self, playlist_id: str) -> float:
        entry = self._items.get(playlist_id)
        if not entry:
            return 0.0
        return (time.time() - entry.get("ts", 0)) / 3600

    def sources(self) -> list[dict[str, Any]]:
        """Every cached playlist: id, track count, age, freshness."""
        now = time.time()
        out: list[dict[str, Any]] = []
        for pid, entry in self._items.items():
            age = now - entry.get("ts", 0)
            out.append(
                {
                    "playlist": pid,
                    "tracks": len(entry.get("tracks") or []),
                    "age_hours": age / 3600,
                    "fresh": age <= self.ttl,
                    "usable": self.allow_stale or age <= self.ttl,
                }
            )
        return sorted(out, key=lambda row: row["age_hours"])

    def put(self, playlist_id: str, tracks: list[Any], snapshot: str = "") -> None:
        self._items[playlist_id] = {
            "ts": time.time(),
            "snapshot": snapshot,
            "tracks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "artists": list(t.artists),
                    "album": t.album,
                    "duration_ms": t.duration_ms,
                    "isrc": t.isrc,
                    "uri": t.uri,
                    "popularity": t.popularity,
                    "sources": list(t.sources),
                }
                for t in tracks
            ],
        }
        if len(self._items) > self.max_playlists:
            oldest = sorted(self._items.items(), key=lambda kv: kv[1].get("ts", 0))
            for key, _ in oldest[: len(self._items) - self.max_playlists]:
                del self._items[key]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"playlists": self._items}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(self.path)
