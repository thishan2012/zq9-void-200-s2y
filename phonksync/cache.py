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
