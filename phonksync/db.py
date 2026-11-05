"""Persistent sync state, stored as JSON inside the repository.

The GitHub Actions workflow commits this file after every run, so the next run
knows exactly which tracks were already added and never adds them twice.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Track
from .youtube import QuotaState

log = logging.getLogger(__name__)

STATE_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "version": STATE_VERSION,
            "updated_at": _now(),
            "synced": {},
            "unmatched": {},
            "stats": {"methods": {}, "runs": 0, "total_added": 0},
            "quota": {},
            "spotify": {},
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state file (%s); starting fresh", exc)
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)
        self.data.setdefault("synced", {})
        self.data.setdefault("unmatched", {})
        self.data.setdefault("stats", {"methods": {}, "runs": 0, "total_added": 0})
        self.data["stats"].setdefault("methods", {})

    # ----------------------------------------------------------------- quota

    @property
    def quota(self) -> QuotaState:
        return QuotaState.from_dict(self.data.get("quota"))

    def save_quota(self, quota: QuotaState) -> None:
        self.data["quota"] = quota.to_dict()

    # --------------------------------------------------------------- spotify

    @property
    def spotify(self) -> dict[str, Any]:
        self.data.setdefault("spotify", {})
        return self.data["spotify"]

    def spotify_cooldown_until(self) -> float:
        try:
            return float(self.spotify.get("cooldown_until", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def set_spotify_cooldown(self, until: float, reason: str = "") -> None:
        self.spotify["cooldown_until"] = float(until)
        self.spotify["cooldown_reason"] = reason
        self.spotify["cooldown_set_at"] = _now()

    def clear_spotify_cooldown(self) -> None:
        if self.spotify.pop("cooldown_until", None) is not None:
            self.spotify["cooldown_cleared_at"] = _now()

    def add_manual(self, track: Track) -> None:
        bucket = self.spotify.setdefault("manual", {})
        bucket[track.id] = {
            "name": track.name,
            "artists": track.artists,
            "uri": track.uri or f"spotify:track:{track.id}",
            "queued_at": _now(),
        }

    def write_manual_queue(self, path: str | Path) -> int:
        """Dump the manual bucket as spotify:track: URIs (copy-paste friendly)."""
        items = self.spotify.get("manual") or {}
        if not items:
            return 0
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {len(items)} tracks that could not be added automatically "
            f"({_now()})."
        ]
        lines.append("# Paste these into the target playlist (or run "
                     "`python -m phonksync manual --apply`).")
        for entry in items.values():
            lines.append(f"{entry.get('uri', '')}  # {', '.join(entry.get('artists', []))} "
                         f"- {entry.get('name', '')}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(items)

    def clear_manual(self) -> None:
        self.spotify.pop("manual", None)

    def set_resume_point(self, track_id: str, name: str, artists: list[str], until: float) -> None:
        self.spotify["resume"] = {
            "track_id": track_id,
            "name": name,
            "artists": list(artists),
            "blocked_until": float(until),
            "saved_at": _now(),
        }

    def get_resume_point(self) -> dict[str, Any]:
        return dict(self.spotify.get("resume") or {})

    def clear_resume_point(self) -> None:
        self.spotify.pop("resume", None)

    def set_pending_queue(self, uris: list[str]) -> None:
        self.spotify["pending_queue"] = list(uris)

    def get_pending_queue(self) -> list[str]:
        return list(self.spotify.get("pending_queue") or [])

    def clear_pending_queue(self) -> None:
        self.spotify.pop("pending_queue", None)

    # ---------------------------------------------------------------- synced

    def is_synced(self, track_id: str) -> bool:
        return track_id in self.data["synced"]

    def get_synced(self, track_id: str) -> dict[str, Any] | None:
        return self.data["synced"].get(track_id)

    def mark_synced(
        self,
        track: Track,
        video_id: str,
        method: str,
        spotify_added: bool,
        youtube_added: bool,
    ) -> None:
        prev = self.data["synced"].get(track.id)
        self.data["synced"][track.id] = {
            "spotify_id": track.id,
            "name": track.name,
            "artists": track.artists,
            "isrc": track.isrc,
            "youtube_video_id": video_id,
            "method": method,
            "added_at": _now(),
            "spotify_added": spotify_added,
            "youtube_added": youtube_added,
        }
        if youtube_added:
            self.data["unmatched"].pop(track.id, None)
        already_counted = bool(prev and (prev.get("spotify_added") or prev.get("youtube_added")))
        if (spotify_added or youtube_added) and not already_counted:
            self.data["stats"]["total_added"] = self.data["stats"].get("total_added", 0) + 1

    # ------------------------------------------------------------- unmatched

    def mark_unmatched(self, track: Track, tried_methods: list[str]) -> None:
        prev = self.data["unmatched"].get(track.id, {})
        self.data["unmatched"][track.id] = {
            "spotify_id": track.id,
            "name": track.name,
            "artists": track.artists,
            "last_try": _now(),
            "tries": int(prev.get("tries", 0)) + 1,
            "tried_methods": tried_methods,
        }

    def unmatched_tries(self, track_id: str) -> int:
        return int(self.data["unmatched"].get(track_id, {}).get("tries", 0))

    def unmatched_last_try(self, track_id: str) -> str:
        return self.data["unmatched"].get(track_id, {}).get("last_try", "")

    # ----------------------------------------------------------------- stats

    def record_method(self, code: str, tried: bool, won: bool) -> None:
        methods = self.data["stats"]["methods"]
        entry = methods.setdefault(code, {"tried": 0, "won": 0})
        if tried:
            entry["tried"] += 1
        if won:
            entry["won"] += 1

    def record_run(self) -> None:
        self.data["stats"]["runs"] = self.data["stats"].get("runs", 0) + 1

    # ------------------------------------------------------------------ save

    def save(self) -> None:
        self.data["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)
