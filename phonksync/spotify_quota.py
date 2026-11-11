"""Durable Spotify 429 cooldowns shared by sync and diagnostic processes.

Only timestamps are stored; no account credentials are written. Web API and
login-token cooldowns are separate so an API block does not disable a working
web-player session. Use one serialized workflow for writers of this file.
"""
import json
import math
import os
import time
from pathlib import Path

from .errors import RateLimited


class SpotifyQuota:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("SPOTIFY_QUOTA_FILE", "data/spotify-quota.json"))

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot safely read Spotify cooldown file {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Invalid Spotify cooldown file: {self.path}")
        return data

    def until(self, scope):
        data = self._read()
        try:
            value = float(data.get(scope, 0) or 0)
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid timestamp")
            return value
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid Spotify cooldown timestamp in {self.path}") from exc

    def block(self, scope, seconds):
        data = self._read()
        until = max(self.until(scope), time.time() + max(0, seconds))
        data[scope] = until
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(self.path)
        return until

    def check(self, scope):
        remaining = self.until(scope) - time.time()
        if remaining > 0:
            raise RateLimited(remaining, f"Spotify {scope} cooldown active until "
                              f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(time.time() + remaining))}")
