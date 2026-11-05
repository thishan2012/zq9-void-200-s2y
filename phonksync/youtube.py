"""YouTube Data API v3 client (OAuth refresh-token flow).

Only the endpoints the syncer needs are implemented:
  * search.list           (100 quota units)
  * videos.list           (1 quota unit)
  * playlistItems.list    (1 quota unit)
  * playlistItems.insert  (50 quota units)

A local quota budget is enforced so a run stops cleanly before Google returns
403 quotaExceeded, and the state file remembers how much was spent today.
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

    def rollover(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.date != today:
            self.date = today
            self.units_used = 0

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "units_used": self.units_used}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "QuotaState":
        data = data or {}
        return cls(date=data.get("date", ""), units_used=int(data.get("units_used", 0)))


class YouTubeClient:
    def __init__(
        self,
        client_json: str = "",
        refresh_token: str = "",
        token_json: str = "",
        daily_quota_units: int = 9500,
        max_searches_per_run: int = 80,
        quota_state: QuotaState | None = None,
        region_code: str = "US",
        session: requests.Session | None = None,
    ) -> None:
        self.client_id = ""
        self.client_secret = ""
        self.token_uri = "https://oauth2.googleapis.com/token"
        self.refresh_token = refresh_token.strip()

        blob: dict[str, Any] = {}
        if token_json:
            blob = json.loads(token_json)
        elif client_json:
            blob = json.loads(client_json)
        else:
            raise AuthError("YouTube OAuth client JSON is missing")

        section = blob.get("installed") or blob.get("web") or blob
        self.client_id = section.get("client_id", "")
        self.client_secret = section.get("client_secret", "")
        self.token_uri = section.get("token_uri", self.token_uri)
        if not self.refresh_token:
            self.refresh_token = blob.get("refresh_token", "")
        if not self.client_id or not self.client_secret:
            raise AuthError("YouTube client JSON must contain client_id and client_secret")
        if not self.refresh_token:
            raise AuthError(
                "YouTube refresh_token is missing. Run the one-time OAuth setup "
                "(Instructions/SECRETS.md) and store YT_REFRESH_TOKEN."
            )

        self.daily_quota_units = daily_quota_units
        self.max_searches_per_run = max_searches_per_run
        self.searches_this_run = 0
        self.quota = quota_state or QuotaState()
        self.quota.rollover()
        self.region_code = region_code
        self.session = session or requests.Session()
        self._access_token = ""
        self._access_expiry = 0.0

    # ------------------------------------------------------------------ auth

    def _ensure_token(self) -> str:
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

    def _budget(self, cost: int, search: bool = False) -> None:
        self.quota.rollover()
        if self.quota.units_used + cost > self.daily_quota_units:
            raise QuotaExceeded(
                f"Local YouTube quota budget reached "
                f"({self.quota.units_used}/{self.daily_quota_units} units today)"
            )
        if search and self.searches_this_run >= self.max_searches_per_run:
            raise QuotaExceeded(
                f"Per-run search limit reached ({self.max_searches_per_run} searches)"
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
    ) -> dict[str, Any]:
        self._budget(cost, search=search)
        token = self._ensure_token()
        url = f"{_API}/{path}"
        headers = {"Authorization": f"Bearer {token}"}
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
            except requests.RequestException as exc:
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
                    self.quota.units_used = self.daily_quota_units
                    raise QuotaExceeded(f"YouTube API quota exceeded: {reason}")
                raise AuthError(f"YouTube API forbidden: {reason}")
            if resp.status_code == 404:
                raise NotFound(f"YouTube resource not found: {path}")
            if resp.status_code == 409:
                # playlistItems.insert: video already in playlist.
                return {}
            if resp.status_code >= 400:
                raise RuntimeError(f"YouTube API error {resp.status_code}: {resp.text[:300]}")

            self.quota.units_used += cost
            if search:
                self.searches_this_run += 1
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
            data = self._request(
                "GET", "playlistItems", params=params, cost=COST_PLAYLIST_LIST
            )
            for item in data.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    ids.add(vid)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids

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
        )
