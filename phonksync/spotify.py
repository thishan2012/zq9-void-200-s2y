"""Spotify Web API client authenticated with a browser session cookie.

The user pastes the *whole* Cookie header for open.spotify.com into the
SPOTIFY_COOKIE secret. We extract `sp_dc` (the only value needed) and exchange
it for a short-lived Web API access token via Spotify's public token endpoint.
No Spotify password, client secret, or OAuth app is required.
"""
from __future__ import annotations

import logging
import re
import time
from http.cookies import SimpleCookie
from typing import Any

import requests

from .errors import AuthError, NotFound, RateLimited
from .models import Track

log = logging.getLogger(__name__)

_TOKEN_URL = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
_API = "https://api.spotify.com/v1"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def extract_sp_dc(cookie_header: str) -> str:
    """Accept a full Cookie header, a bare sp_dc value, or 'sp_dc=...'.

    Robust to common copy-paste mistakes:
      * \"Cookie: \" prefix from DevTools (Copy request headers)
      * outer quotes around the whole value (\"sp_dc=...; ...\")
      * quoted sp_dc value (sp_dc=\"AQ...\")
      * extra whitespace / case differences
    """
    raw = (cookie_header or "").strip()
    if not raw:
        raise AuthError("SPOTIFY_COOKIE is empty — add it to .env or GitHub Secrets")

    # Strip optional \"Cookie:\" prefix (DevTools \"Copy request headers\" includes it).
    if raw[:7].lower() == "cookie:":
        raw = raw[7:].strip()

    # Strip outer matching quotes around the entire header (e.g. pasted as \"...\").
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1].strip()
        # Handle double-wrapped case like '\"sp_dc=...\"'
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            raw = raw[1:-1].strip()

    # Bare token value (no '=' and no ';') — just the sp_dc value itself.
    if "=" not in raw and ";" not in raw and len(raw) > 40:
        return raw.strip('"\'').strip()

    # Try SimpleCookie first (handles sp_dc=\"...\" correctly).
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        pass
    if "sp_dc" in jar:
        val = jar["sp_dc"].value.strip('"\'').strip()
        if val:
            return val
    # Case-insensitive search that tolerates spaces and optional quotes:
    #   sp_dc = \"AQ...\"  /  SP_DC=AQ...;  /  sp_dc=\"AQ...\"
    m = re.search(r"sp_dc\s*=\s*\"?([^;\"\s]+)\"?", raw, re.IGNORECASE)
    if m:
        return m.group(1).strip().strip('"\'').strip()

    # Diagnostic hint without leaking the secret: show length + preview.
    preview = raw[:60].replace("\n", " ") if raw else ""
    has_sp_dc = "sp_dc" in raw.lower()
    hint = (
        f" Got {len(raw)} chars starting with {preview!r}."
        + (" It contains 'sp_dc' but parsing failed — check for stray quotes or newlines." if has_sp_dc else " It does not appear to contain 'sp_dc'.")
    )
    raise AuthError(
        "Could not find sp_dc in SPOTIFY_COOKIE. Paste the full Cookie header "
        "from an authenticated open.spotify.com request (e.g. 'sp_t=...; sp_dc=AQ...; ...')."
        + hint
    )


class SpotifyClient:
    def __init__(self, cookie_header: str, session: requests.Session | None = None):
        self.sp_dc = extract_sp_dc(cookie_header)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _UA})
        self._access_token = ""
        self._access_expiry = 0.0

    # ------------------------------------------------------------------ auth

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._access_expiry - 60:
            return self._access_token
        try:
            resp = self.session.get(
                _TOKEN_URL,
                cookies={"sp_dc": self.sp_dc},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise AuthError(f"Spotify token request failed: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(
                f"Spotify token endpoint returned {resp.status_code}. "
                "The sp_dc cookie is probably expired; refresh SPOTIFY_COOKIE."
            )
        data = resp.json()
        if data.get("isAnonymous", True) or not data.get("accessToken"):
            raise AuthError(
                "Spotify returned an anonymous token. The sp_dc cookie is invalid "
                "or expired. Log into open.spotify.com and copy a fresh Cookie header."
            )
        self._access_token = data["accessToken"]
        expires_ms = data.get("accessTokenExpirationTimestampMs", 0)
        self._access_expiry = (expires_ms / 1000) if expires_ms else time.time() + 3000
        return self._access_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        _retry_auth: bool = True,
    ) -> dict[str, Any]:
        token = self._ensure_token()
        url = path if path.startswith("http") else f"{_API}/{path}"
        for attempt in range(4):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=45,
                )
            except requests.RequestException as exc:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2"))
                if attempt == 3:
                    raise RateLimited(retry_after, "Spotify rate limit")
                time.sleep(min(retry_after, 30))
                continue
            if resp.status_code >= 500:
                if attempt == 3:
                    raise RuntimeError(f"Spotify HTTP {resp.status_code}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401 and _retry_auth:
                self._access_token = ""
                return self._request(
                    method, path, params=params, json_body=json_body, _retry_auth=False
                )
            if resp.status_code == 401:
                raise AuthError("Spotify access token rejected. Refresh SPOTIFY_COOKIE.")
            if resp.status_code == 403:
                raise AuthError(
                    f"Spotify forbidden for {path}: {resp.text[:200]}. "
                    "The cookie may lack permission for this playlist."
                )
            if resp.status_code == 404:
                raise NotFound(f"Spotify resource not found: {path}")
            if resp.status_code >= 400:
                raise RuntimeError(f"Spotify API error {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.text else {}
        raise RuntimeError("Spotify request retry loop exhausted")

    # ------------------------------------------------------------- endpoints

    def playlist_tracks(
        self, playlist_id: str, max_tracks: int | None = None
    ) -> list[Track]:
        """Read playlist tracks.

        ``max_tracks`` stops pagination early and is used for dry-run previews.
        ``None`` reads the full playlist (live sync).
        """
        if max_tracks is not None and max_tracks <= 0:
            return []
        tracks: list[Track] = []
        path = f"playlists/{playlist_id}/tracks"
        page_size = min(100, max_tracks) if max_tracks else 100
        params: dict[str, Any] = {
            "limit": page_size,
            "fields": (
                "items(is_local,track(id,name,type,artists(name),album(name),"
                "duration_ms,external_ids(isrc),uri,popularity)),next"
            ),
        }
        while True:
            data = self._request("GET", path, params=params)
            for item in data.get("items", []):
                if max_tracks is not None and len(tracks) >= max_tracks:
                    break
                t = item.get("track") or {}
                if not t or item.get("is_local") or not t.get("id"):
                    continue
                if t.get("type") and t["type"] != "track":
                    continue  # skip podcast episodes
                tracks.append(
                    Track(
                        id=t["id"],
                        name=t.get("name", ""),
                        artists=[a.get("name", "") for a in t.get("artists", []) if a.get("name")],
                        album=(t.get("album") or {}).get("name", ""),
                        duration_ms=int(t.get("duration_ms") or 0),
                        isrc=(t.get("external_ids") or {}).get("isrc", ""),
                        uri=t.get("uri", f"spotify:track:{t['id']}"),
                        popularity=int(t.get("popularity") or 0),
                        sources=[playlist_id],
                    )
                )
            if max_tracks is not None and len(tracks) >= max_tracks:
                break
            next_url = data.get("next")
            if not next_url:
                break
            path = next_url
            params = {}
        return tracks

    def playlist_track_ids(self, playlist_id: str) -> set[str]:
        return {t.id for t in self.playlist_tracks(playlist_id)}

    def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            self._request(
                "POST",
                f"playlists/{playlist_id}/tracks",
                json_body={"uris": chunk},
            )

    def current_user_id(self) -> str:
        data = self._request("GET", "me")
        return data.get("id", "")
