"""Manual track overrides (the MANUAL method).

`data/track-overrides.json` lets you pin a Spotify track to an exact YouTube
video, bypassing all search. Keys may be a Spotify track ID, a full
spotify:track: URI, or an ISRC. Values may be a YouTube video ID or URL.

Example:
{
  "overrides": {
    "11dFghVXanM5MeABsZcnj1": "dQw4w9WgXcQ",
    "USRC17607839": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
  }
}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import Track
from .youtube import extract_video_id

log = logging.getLogger(__name__)


class Overrides:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.by_key: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read overrides (%s); ignoring", exc)
            return
        raw = data.get("overrides", data) if isinstance(data, dict) else {}
        for key, value in raw.items():
            try:
                self.by_key[str(key).strip()] = extract_video_id(str(value))
            except ValueError:
                log.warning("Override for %s has invalid YouTube value %r", key, value)

    def lookup(self, track: Track) -> str | None:
        for key in (track.id, track.uri, f"spotify:track:{track.id}", track.isrc):
            if key and key in self.by_key:
                return self.by_key[key]
        return None
