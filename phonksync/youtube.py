"""YouTube Data API v3 client.

Search and metadata reads (``search.list`` / ``videos.list``) can run on a plain
**read-only API key** (``YOUTUBE_API_KEY``) sent as ``key=...``. Playlist writes
(``playlistItems.insert``) *require* OAuth — Google does not allow key-based
writes — so a refresh-token flow is used only for those calls.

Endpoints used:
  * search.list           (read, 100 quota units, API key OK)
  * videos.list           (read, 1 quota unit, API key OK)
  * playlistItems.list    (read, 1 quota unit, API key OK for public playlists)
  * playlistItems.insert  (write, 50 quota units, OAuth only)

Quota: both the API key and the OAuth token bill the *same* Google Cloud
project, so one local budget is enforced across the two. A run stops cleanly
before Google returns 403 quotaExceeded, and the state file remembers how much
was spent today.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from .errors import AuthError, NotFound, QuotaExceeded, RateLimited
from .models import Candidate

log = logging.getLogger(__name__)

_API = "https://www.googleapis.com/youtube/v3"
_SCOPE = "https://www.googleapis.com/auth/youtube"

COST_SEARCH = 100
COST_VIDEOS = 1
COST_PLAYLIST_LIST = 1
COST_PLAYLIST_INSERT = 50

_ISO_DURATION = re.compile(
    r"^P(?:(?P<d>\d+)D)?T?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


def parse_iso_duration(value: str) -> int:
    m = _ISO_DURATION.match(value or "")
    if not m:
        return 0
    parts = {k: int(v or 0) for k, v in m.groupdict().items()}
    return parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]


def extract_video_id(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", value)
    if m:
        return m.group(1)
    raise ValueError(f"Cannot parse YouTube video ID from {value!r}")


@dataclass
class QuotaState:
    """Persisted daily quota usage."""

    date: str = ""
    units_used: int = 0
    # Daily counters, so a multi-run day cannot exceed the real project quota.
    searches: int = 0
    inserts: int = 0
    # key index -> units used today. Every API key needs its own Google Cloud
    # project (quota is per project), so each key gets its own daily budget.
    keys: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.keys is None:
            self.keys = {}

    def rollover(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.date != today:
            self.date = today
            self.units_used = 0
            self.searches = 0
            self.inserts = 0
            self.keys = {}

    def key_units(self, index: str) -> int:
        return int(self.keys.get(index, 0))

    def spend_on_key(self, index: str, cost: int) -> None:
        self.keys[index] = self.key_units(index) + cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "units_used": self.units_used,
            "searches": self.searches,
            "inserts": self.inserts,
            "keys": dict(self.keys),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "QuotaState":
        data = data or {}
        keys = {
            str(k): int(v) for k, v in (data.get("keys") or {}).items()
        }
        return cls(
            date=data.get("date", ""),
            units_used=int(data.get("units_used", 0) or 0),
            searches=int(data.get("searches", 0) or 0),
            inserts=int(data.get("inserts", 0) or 0),
            keys=keys,
        )


class YouTubeClient:
    def __init__(
        self,
        client_json: str = "",
        refresh_token: str = "",
        token_json: str = "",
        api_key: str | list[str] = "",
        daily_quota_units: int = 9500,
        max_searches_per_run: int = 80,
        max_searches_per_day: int = 0,
        max_inserts_per_day: int = 0,
        quota_state: QuotaState | None = None,
        region_code: str = "US",
        session: requests.Session | None = None,
    ) -> None:
        raw_keys: list[str] = []
        if isinstance(api_key, str):
            raw_keys = re.split(r"[\s,;]+", api_key or "")
        else:
            for item in api_key or []:
                raw_keys.extend(re.split(r"[\s,;]+", str(item or "")))
        # Preserve order, drop blanks and duplicates.
        self.api_keys = [k for k in dict.fromkeys(k.strip() for k in raw_keys) if k]
        self.api_key = self.api_keys[0] if self.api_keys else ""
        self._key_index = 0
        self._key_exhausted: set[int] = set()
        self.client_id = ""
        self.client_secret = ""
        self.token_uri = "https://oauth2.googleapis.com/token"
        self.refresh_token = refresh_token.strip()
        self._oauth_error = ""

        blob: dict[str, Any] = {}
        raw_json = token_json or client_json
        if raw_json:
            try:
                blob = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                raise AuthError(f"YouTube OAuth JSON is not valid JSON: {exc}") from exc
        section = blob.get("installed") or blob.get("web") or blob
        self.client_id = section.get("client_id", "")
        self.client_secret = section.get("client_secret", "")
        self.token_uri = section.get("token_uri", self.token_uri)
        if not self.refresh_token:
            # Token JSONs keep refresh_token at the top level; be lenient and
            # also look inside the installed/web section.
            self.refresh_token = (
                blob.get("refresh_token", "") or section.get("refresh_token", "")
            )

        if not self.api_key and not (self.client_id and self.client_secret):
            raise AuthError(
                "YouTube credentials are missing. Set YOUTUBE_API_KEY (read-only "
                "search) and/or YT_CLIENT_JSON + YT_REFRESH_TOKEN (playlist writes)."
            )
        if not self.refresh_token and not self.api_key:
            raise AuthError(
                "YouTube refresh_token is missing. Run the one-time OAuth setup "
                "(Instructions/SECRETS.md) and store YT_REFRESH_TOKEN, or set "
                "YOUTUBE_API_KEY for read-only search."
            )

        # Quota is per Google Cloud project, i.e. per key. With N keys the run
        # may spend N x daily_quota_units in total, but never more than
        # daily_quota_units on any single key.
        self.daily_quota_units = daily_quota_units
        self.total_quota_units = daily_quota_units * max(1, len(self.api_keys))
        self.max_searches_per_run = max_searches_per_run
        self.max_searches_per_day = max_searches_per_day
        self.max_inserts_per_day = max_inserts_per_day
        self.searches_this_run = 0
        self.quota = quota_state or QuotaState()
        self.quota.rollover()
        self.region_code = region_code
        self.session = session or requests.Session()
        self._access_token = ""
        self._access_expiry = 0.0

    # ------------------------------------------------------------------ auth

    @property
    def can_write(self) -> bool:
        """Playlist inserts need OAuth; an API key alone cannot write."""
        return bool(self.refresh_token and self.client_id and self.client_secret)

    @property
    def search_mode(self) -> str:
        if not self.api_keys:
            return "oauth"
        return "api_key" if len(self.api_keys) == 1 else f"api_key x{len(self.api_keys)}"

    def _ensure_token(self) -> str:
        if not self.can_write:
            raise AuthError(
                "YouTube playlist writes need OAuth (an API key cannot add videos). "
                "Add YT_CLIENT_JSON + YT_REFRESH_TOKEN to enable writes."
            )
        if self._access_token and time.time() < self._access_expiry - 60:
            return self._access_token
        try:
            resp = self.session.post(
                self.token_uri,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise AuthError(f"YouTube token refresh failed: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(
                f"YouTube token refresh failed ({resp.status_code}): {resp.text[:300]}"
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._access_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    def _next_key(self, cost: int) -> int | None:
        """Pick a key that still has quota left today (rotating)."""
        if not self.api_keys:
            return None
        for offset in range(len(self.api_keys)):
            idx = (self._key_index + offset) % len(self.api_keys)
            if idx in self._key_exhausted:
                continue
            if self.quota.key_units(str(idx)) + cost <= self.daily_quota_units:
                self._key_index = idx
                return idx
        return None

    def _budget(self, cost: int, search: bool = False, insert: bool = False, oauth: bool = False) -> None:
        self.quota.rollover()
        if oauth and not self.can_write:
            raise AuthError(
                "YouTube playlist writes need OAuth (an API key cannot add videos). "
                "Add YT_CLIENT_JSON + YT_REFRESH_TOKEN."
            )
        if self.quota.units_used + cost > self.total_quota_units:
            raise QuotaExceeded(
                f"Local YouTube quota budget reached "
                f"({self.quota.units_used}/{self.total_quota_units} units today)"
            )
        if not oauth and self.api_keys and self._next_key(cost) is None:
            raise QuotaExceeded(
                f"Every YouTube API key has used its daily budget "
                f"({len(self.api_keys)} key(s), {self.daily_quota_units} units each). "
                "Add another key from a different Google Cloud project or wait for the "
                "UTC rollover."
            )
        if search:
            if self.searches_this_run >= self.max_searches_per_run:
                raise QuotaExceeded(
                    f"Per-run search limit reached ({self.max_searches_per_run} searches)"
                )
            if self.max_searches_per_day and self.quota.searches >= self.max_searches_per_day:
                raise QuotaExceeded(
                    f"Daily search limit reached ({self.quota.searches}/"
                    f"{self.max_searches_per_day} searches today) — the API key is still "
                    "valid, this is the app's own safety cap. Raise "
                    "youtube_max_searches_per_day in config.json to use more of the day."
                )
        if insert and self.max_inserts_per_day and self.quota.inserts >= self.max_inserts_per_day:
            raise QuotaExceeded(
                f"Daily insert limit reached ({self.quota.inserts}/"
                f"{self.max_inserts_per_day} playlist adds today)"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        cost: int = 1,
        search: bool = False,
        insert: bool = False,
        oauth: bool = False,
    ) -> dict[str, Any]:
        self._budget(cost, search=search, insert=insert, oauth=oauth)
        params = dict(params or {})
        headers: dict[str, str] = {}
        key_index: int | None = None
        if oauth or not self.api_keys:
            # Writes always use OAuth. Reads use an API key when one is set
            # (no token refresh, no expiry in the hot search path) and fall back
            # to OAuth for configs that only have a refresh token.
            headers["Authorization"] = f"Bearer {self._ensure_token()}"
        else:
            key_index = self._next_key(cost)
            if key_index is None:  # pragma: no cover - _budget already raises
                raise QuotaExceeded("No YouTube API key has quota left today")
            params["key"] = self.api_keys[key_index]
        url = f"{_API}/{path}"
        for attempt in range(4):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=45,
                )
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 3:
                    raise RateLimited(message=f"YouTube HTTP {resp.status_code}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401:
                self._access_token = ""
                raise AuthError("YouTube access token rejected (401)")
            if resp.status_code == 403:
                try:
                    reason = resp.json()["error"]["errors"][0]["reason"]
                except Exception:
                    reason = resp.text[:200]
                if "quota" in reason.lower():
                    if key_index is not None:
                        # This key's project is out of quota; try the next key
                        # (a different project) instead of ending the run.
                        self.quota.spend_on_key(str(key_index), self.daily_quota_units)
                        self._key_exhausted.add(key_index)
                        log.warning(
                            "API key #%d hit its project quota — rotating to the next key",
                            key_index + 1,
                        )
                        if self._next_key(cost) is not None:
                            return self._request(
                                method, path,
                                params={k: v for k, v in params.items() if k != "key"},
                                json_body=json_body, cost=cost, search=search,
                                insert=insert, oauth=oauth,
                            )
                        raise QuotaExceeded(
                            "All YouTube API keys are out of quota for today "
                            f"({reason})"
                        )
                    self.quota.units_used = self.total_quota_units
                    raise QuotaExceeded(f"YouTube API quota exceeded: {reason}")
                if not oauth and "accessnotconfigured" in reason.lower().replace(" ", ""):
                    raise AuthError(
                        f"YouTube Data API v3 is not enabled for this key's project: {reason}"
                    )
                if not oauth and ("key" in reason.lower() or "apikey" in reason.lower()):
                    raise AuthError(
                        f"YouTube API key rejected: {reason}. Check YOUTUBE_API_KEY and "
                        "that YouTube Data API v3 is enabled in that project."
                    )
                raise AuthError(f"YouTube API forbidden: {reason}")
            if resp.status_code == 400 and not oauth:
                detail = resp.text[:300]
                if "API key not valid" in detail or "keyInvalid" in detail:
                    raise AuthError(
                        f"YouTube API key invalid: {detail}. Check YOUTUBE_API_KEY."
                    )
                raise RuntimeError(f"YouTube API error 400: {detail}")
            if resp.status_code == 404:
                raise NotFound(f"YouTube resource not found: {path}")
            if resp.status_code == 409:
                # playlistItems.insert: video already in playlist.
                return {}
            if resp.status_code >= 400:
                raise RuntimeError(f"YouTube API error {resp.status_code}: {resp.text[:300]}")

            self.quota.units_used += cost
            if key_index is not None:
                self.quota.spend_on_key(str(key_index), cost)
            if search:
                self.searches_this_run += 1
                self.quota.searches += 1
            if insert:
                self.quota.inserts += 1
            return resp.json() if resp.text else {}
        raise RuntimeError("YouTube request retry loop exhausted")

    # ------------------------------------------------------------- endpoints

    def search(self, query: str, max_results: int = 10) -> list[str]:
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "safeSearch": "none",
            "fields": "items(id/videoId)",
        }
        if self.region_code:
            params["regionCode"] = self.region_code
        data = self._request("GET", "search", params=params, cost=COST_SEARCH, search=True)
        return [
            item["id"]["videoId"]
            for item in data.get("items", [])
            if item.get("id", {}).get("videoId")
        ]

    def videos(self, video_ids: list[str]) -> list[Candidate]:
        out: list[Candidate] = []
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i : i + 50]
            data = self._request(
                "GET",
                "videos",
                params={
                    "part": "snippet,contentDetails,statistics",
                    "id": ",".join(chunk),
                    "fields": (
                        "items(id,snippet(title,channelTitle,description),"
                        "contentDetails(duration),statistics(viewCount))"
                    ),
                },
                cost=COST_VIDEOS,
            )
            for item in data.get("items", []):
                sn = item.get("snippet", {})
                out.append(
                    Candidate(
                        video_id=item["id"],
                        title=sn.get("title", ""),
                        channel=sn.get("channelTitle", ""),
                        description=sn.get("description", ""),
                        duration_s=parse_iso_duration(
                            item.get("contentDetails", {}).get("duration", "")
                        ),
                        views=int(item.get("statistics", {}).get("viewCount", 0) or 0),
                    )
                )
        return out

    def playlist_video_ids(self, playlist_id: str) -> set[str]:
        ids: set[str] = set()
        page_token = None
        while True:
            params: dict[str, Any] = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
                "fields": "nextPageToken,items(contentDetails/videoId)",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._playlist_page(params)
            for item in data.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    ids.add(vid)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids

    def _playlist_page(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a playlist page with OAuth when possible (private playlists).

        Falls back to the API key so public / unlisted playlists still work
        when only a key is configured.
        """
        if self.can_write:
            try:
                return self._request(
                    "GET", "playlistItems", params=params, cost=COST_PLAYLIST_LIST, oauth=True
                )
            except AuthError as exc:
                log.warning("OAuth playlist read failed (%s) — retrying with API key", exc)
        return self._request("GET", "playlistItems", params=params, cost=COST_PLAYLIST_LIST)

    def add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        self._request(
            "POST",
            "playlistItems",
            params={"part": "snippet"},
            json_body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
            cost=COST_PLAYLIST_INSERT,
            insert=True,
            oauth=True,
        )
