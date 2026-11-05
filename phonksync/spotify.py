"""Spotify Web API client authenticated with the Web Player cookie (sp_dc).

The user pastes the *whole* Cookie header for open.spotify.com into the
SPOTIFY_COOKIE secret. We extract `sp_dc` (the only value needed) and exchange
it for a short-lived Web API access token.

Since March 2025 Spotify's token endpoint also requires a *TOTP* code that the
web player generates from a versioned secret baked into its JS bundle. We
derive that secret the same way the web player does (see ``_totp_secret``) and
fall back to the older ``get_access_token`` endpoint if needed.

No Spotify password, client secret, OAuth app, or Premium account is required.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import struct
import time
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from typing import Any

import requests

from .errors import AuthError, NotFound, RateLimited
from .models import Track

log = logging.getLogger(__name__)

_TOKEN_URL = "https://open.spotify.com/api/token"
_LEGACY_TOKEN_URL = (
    "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
)
_SERVER_TIME_URL = "https://open.spotify.com/"
_MAX_RETRIES = 6
_PAGE_SIZE = 50
_PACE_SECONDS = 0.35


def _retry_after_seconds(headers: Any, attempt: int) -> float:
    """Spotify asks for ~30s; honour Retry-After but cap and add jitter."""
    value = ""
    try:
        value = headers.get("Retry-After", "") or ""
    except AttributeError:
        value = ""
    try:
        wait = float(value)
    except (TypeError, ValueError):
        wait = 2 ** attempt
    # Some responses send a HTTP-date; treat anything unparsable as backoff.
    wait = max(wait, 2.0)
    jitter = random.uniform(0, 1.5)
    return min(wait, 45.0) + jitter

# Spotify's TOTP secret, versioned. The cipher bytes are XOR-scrambled with
# (index % 33) + 9 before being turned into a base32 TOTP key. The web player
# rotates this every few months; the newest known version is used and can be
# overridden with SPOTIFY_TOTP_VER / SPOTIFY_TOTP_CIPHER, or refreshed from the
# community mirror (SPOTIFY_TOTP_REMOTE=0 disables that fetch).
_TOTP_SECRETS: dict[str, list[int]] = {
    "59": [123, 105, 79, 70, 110, 59, 52, 125, 60, 49, 80, 70, 89, 75, 80, 86,
           63, 53, 123, 37, 117, 49, 52, 93, 77, 62, 47, 86, 48, 104, 68, 72],
    "60": [79, 109, 69, 123, 90, 65, 46, 74, 94, 34, 58, 48, 70, 71, 92, 85,
           122, 63, 91, 64, 87, 87],
    "61": [44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75, 76,
           94, 102, 43, 69, 49, 120, 118, 80, 64, 78],
}
_TOTP_SECRETS_URL = (
    "https://raw.githubusercontent.com/xyloflake/spot-secrets-go/main/secrets/secretDict.json"
)
_TOTP_REMOTE_CACHE: dict[str, Any] = {"ts": 0.0, "ver": ""}
_API = "https://api.spotify.com/v1"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# Cookie names that are never the session token; useful for diagnostics only.
_SP_DC_RE = re.compile(r"\bsp_dc\b", re.IGNORECASE)
# A value-shaped sp_dc token (Spotify's values are long; be lenient).
_SP_DC_TOKEN_RE = re.compile(r"sp_dc\W{0,3}?([A-Za-z0-9._~+/=%\-]{20,})", re.IGNORECASE)


def _clean_cookie(raw: str) -> str:
    """Normalise copy-paste variants into a single-line ``k=v; k=v`` string.

    Handles:
      * ``Cookie: ...`` prefix and Chrome/Edge "Copy request headers" output
        (``cookie:`` line followed by indented values, or one ``cookie:`` line
        per cookie)
      * multiline dumps where each line is ``name=value;`` or ``name<TAB>value``
        (WebView / Netscape cookie files)
      * markdown/backtick or comment noise (``# WebView ...``)
      * stray CRLF, tabs, doubled quotes
    """
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = text.replace("\u00a0", " ").strip()
    if not text:
        return ""

    lines = [ln.strip() for ln in text.split("\n")]
    # Drop comment lines (# ...) but keep everything else.
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        return ""

    # Remove a leading "cookie:" / "set-cookie:" label wherever it appears and
    # strip surrounding backticks/quotes per line.
    cleaned: list[str] = []
    for ln in lines:
        ln = ln.strip()
        low = ln.lower()
        if low.startswith("set-cookie:"):
            ln = ln[len("set-cookie:"):].strip()
        elif low.startswith("cookie:"):
            ln = ln[len("cookie:"):].strip()
        ln = ln.strip("`").strip()
        if ln:
            cleaned.append(ln)
        # A "cookie:" header may be followed by indented continuation lines in
        # DevTools "Copy request headers" output; those are already collected.
    text = " ".join(cleaned)

    # Netscape/WebView files are TAB separated -> already normalised to space.
    # Join whitespace-separated pairs that lost their ';' (e.g. "sp_t abc sp_dc AQ..")
    if "sp_dc" in text.lower() and "=" not in text.split("sp_dc", 1)[1][:3]:
        m = _SP_DC_TOKEN_RE.search(text)
        if m:
            # Rebuild a minimal header so later steps can parse it.
            return f"sp_dc={m.group(1)}"
    return text


def _normalise_value(value: str) -> str:
    return (value or "").strip().strip('"\'').strip()


def extract_sp_dc(cookie_header: str) -> str:
    """Accept a full Cookie header, a bare sp_dc value, or 'sp_dc=...'.

    Robust to common copy-paste mistakes:
      * "Cookie: " prefix from DevTools (Copy request headers / Copy as cURL)
      * multiline dumps, "name=value" lines, or "name<TAB>value" (WebView)
      * outer quotes around the whole value ("sp_dc=...; ...")
      * quoted sp_dc value (sp_dc="AQ..."), stray whitespace, case differences
      * JSON-ish output from browser extensions (sp_dc: "AQ...")
    """
    raw = (cookie_header or "").strip()
    if not raw:
        raise AuthError("SPOTIFY_COOKIE is empty — add it to .env or GitHub Secrets")

    text = _clean_cookie(raw)

    # Strip outer matching quotes around the entire header.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1].strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
            text = text[1:-1].strip()

    # Bare token value — just the sp_dc value itself (token-shaped, no pairs).
    if (
        "=" not in text
        and ";" not in text
        and len(text) > 40
        and re.fullmatch(r"[A-Za-z0-9._~+/%\-=]+", text)
    ):
        return _normalise_value(text)

    # JSON output (browser cookie extensions, "Copy as JSON") — the object may
    # be a flat list of cookies or wrap an array under a key.
    if text[:1] in "{[":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = None
        if data is not None:
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    # {"name": "sp_dc", "value": "AQ..."} (extension exports)
                    if (
                        str(node.get("name", "")).lower() == "sp_dc"
                        and isinstance(node.get("value"), str)
                        and node["value"].strip()
                    ):
                        return _normalise_value(node["value"])
                    for key, value in node.items():
                        if str(key).lower() == "sp_dc" and isinstance(value, str) and value.strip():
                            return _normalise_value(value)
                        stack.append(value)
                elif isinstance(node, list):
                    stack.extend(node)

    # Try SimpleCookie first (handles sp_dc="..." correctly).
    jar = SimpleCookie()
    try:
        jar.load(text)
    except Exception:
        pass
    if "sp_dc" in jar:
        val = _normalise_value(jar["sp_dc"].value)
        if val:
            return val

    # Fallback: tolerant regex (sp_dc = "AQ..." / sp_dc:AQ... / SP_DC=AQ...).
    m = re.search(r"sp_dc\s*[=:]\s*\"?([^;\"\s]+)", text, re.IGNORECASE)
    if m and _normalise_value(m.group(1)):
        return _normalise_value(m.group(1))

    # Last resort: any long token sitting next to the sp_dc marker (WebView /
    # Netscape dumps where the separator was mangled).
    m = _SP_DC_TOKEN_RE.search(text)
    if m and _normalise_value(m.group(1)):
        return _normalise_value(m.group(1))

    names = re.findall(r"([A-Za-z0-9_.\-]+)\s*=", text)
    has_sp_dc = "sp_dc" in text.lower()
    seen = ", ".join(dict.fromkeys(names))[:200] or "none"
    hint = (
        f" Got {len(raw)} chars, cookie names found: {seen}."
        + (" It contains 'sp_dc' but no usable value followed it — the value may be "
           "empty or truncated. Copy the Cookie header again from a request to "
           "open.spotify.com that is 200 OK."
           if has_sp_dc else " It does not contain a 'sp_dc' cookie at all — make sure "
           "you copied from open.spotify.com while logged in, not accounts.spotify.com.")
    )
    raise AuthError(
        "Could not find sp_dc in SPOTIFY_COOKIE. Paste the full Cookie header "
        "from an authenticated open.spotify.com request (e.g. 'sp_t=...; sp_dc=AQ...; ...')."
        + hint
    )



def _totp_secret(cipher: list[int]) -> str:
    """Rebuild the base32 TOTP key from Spotify's scrambled cipher bytes."""
    transformed = [byte ^ ((index % 33) + 9) for index, byte in enumerate(cipher)]
    joined = "".join(str(number) for number in transformed)
    raw = bytes.fromhex(joined.encode().hex())
    return base64.b32encode(raw).decode().rstrip("=")


def _totp_code(secret_b32: str, timestamp: int, digits: int = 6, interval: int = 30) -> str:
    """RFC 6238 TOTP (SHA-1) — what the web player sends as ``totp``."""
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded)
    counter = int(timestamp) // interval
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _totp_version() -> str:
    """Version to use: explicit env override, else the newest known version."""
    override = os.environ.get("SPOTIFY_TOTP_VER", "").strip()
    if override:
        if override not in _TOTP_SECRETS:
            raise AuthError(
                f"SPOTIFY_TOTP_VER={override} has no known secret. Set "
                "SPOTIFY_TOTP_CIPHER too (comma-separated bytes)."
            )
        return override
    return max(_TOTP_SECRETS, key=lambda v: int(v))


def _apply_cipher_override() -> None:
    raw = os.environ.get("SPOTIFY_TOTP_CIPHER", "").strip()
    if not raw:
        return
    ver = os.environ.get("SPOTIFY_TOTP_VER", "").strip() or _totp_version()
    try:
        cipher = [int(part) for part in re.split(r"[,\s]+", raw) if part]
    except ValueError as exc:
        raise AuthError(f"SPOTIFY_TOTP_CIPHER must be comma-separated integers: {exc}")
    if len(cipher) < 8:
        raise AuthError("SPOTIFY_TOTP_CIPHER looks too short to be a Spotify TOTP cipher")
    _TOTP_SECRETS[ver] = cipher


def _refresh_totp_secrets(session: requests.Session, max_age: float = 21600) -> None:
    """Optionally pull the newest TOTP cipher from the community mirror.

    Fails silently: the embedded secrets keep working, and a network hiccup must
    never break a sync run. Set SPOTIFY_TOTP_REMOTE=0 to skip the call entirely.
    """
    if os.environ.get("SPOTIFY_TOTP_REMOTE", "").strip().lower() in {"0", "false", "no"}:
        return
    if time.time() - float(_TOTP_REMOTE_CACHE.get("ts", 0)) < max_age:
        return
    _TOTP_REMOTE_CACHE["ts"] = time.time()
    try:
        resp = session.get(_TOTP_SECRETS_URL, timeout=10)
        if resp.status_code != 200:
            return
        data = resp.json()
    except (requests.RequestException, ValueError):
        return
    newest = _totp_version()
    for ver, cipher in (data or {}).items():
        try:
            values = [int(v) for v in cipher]
        except (TypeError, ValueError):
            continue
        if len(values) >= 8:
            _TOTP_SECRETS[str(ver)] = values
            if int(ver) > int(newest):
                newest = str(ver)
    _TOTP_REMOTE_CACHE["ver"] = newest
    log.debug("Spotify TOTP secrets known versions: %s", sorted(_TOTP_SECRETS, key=int))


def _server_time(session: requests.Session) -> int:
    """Spotify edge time (TOTP is time-sensitive); local clock as fallback."""
    try:
        resp = session.head(_SERVER_TIME_URL, timeout=5)
        date_header = resp.headers.get("Date", "")
        if date_header:
            return int(parsedate_to_datetime(date_header).timestamp())
    except (requests.RequestException, TypeError, ValueError):
        pass
    return int(time.time())


class SpotifyClient:
    def __init__(self, cookie_header: str, session: requests.Session | None = None):
        _apply_cipher_override()
        self.sp_dc = extract_sp_dc(cookie_header)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _UA})
        self._access_token = ""
        self._access_expiry = 0.0
        self._rate_limit_wait = 0.0
        self._pace_debt = 0.0
        self.partial_read = False

    # ------------------------------------------------------------------ auth

    def _totp_params(self, server_time: int, ver: str) -> dict[str, str]:
        cipher = _TOTP_SECRETS[ver]
        code = _totp_code(_totp_secret(cipher), server_time)
        params = {
            "reason": "transport",
            "productType": "web-player",
            "totp": code,
            "totpServer": code,
            "totpVer": ver,
        }
        if int(ver) < 10:
            now = int(time.time())
            day = time.strftime("%Y-%m-%d", time.gmtime(server_time))
            params.update(
                {
                    "sTime": str(server_time),
                    "cTime": str(now),
                    "buildDate": day,
                    "buildVer": f"web-player_{day}_{server_time * 1000}_{os.urandom(4).hex()}",
                }
            )
        return params

    def _token_request(self) -> dict[str, Any]:
        """Ask Spotify for a web-player token, retrying the known variants."""
        _refresh_totp_secrets(self.session)
        ver = _totp_version()
        server_time = _server_time(self.session)
        attempts: list[tuple[str, dict[str, str], str]] = []
        errors: list[str] = []
        for reason in ("transport", "init"):
            params = self._totp_params(server_time, ver)
            params["reason"] = reason
            attempts.append((_TOKEN_URL, params, f"{_TOKEN_URL} ({reason}, totp v{ver})"))
        # Last resort: the pre-TOTP endpoint (works for some accounts/regions).
        attempts.append(
            (_LEGACY_TOKEN_URL, {}, _LEGACY_TOKEN_URL)
        )

        for url, params, label in attempts:
            try:
                resp = self.session.get(
                    url,
                    params=params or None,
                    headers={
                        "Accept": "application/json",
                        "Referer": "https://open.spotify.com/",
                        "App-Platform": "WebPlayer",
                    },
                    cookies={"sp_dc": self.sp_dc},
                    timeout=30,
                )
            except requests.RequestException as exc:
                errors.append(f"{label}: {exc}")
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    errors.append(f"{label}: response was not JSON")
                    continue
                if data.get("accessToken"):
                    log.debug("Spotify token acquired via %s", label)
                    return data
                errors.append(f"{label}: no accessToken in response")
                continue
            errors.append(f"{label}: HTTP {resp.status_code}")
            log.debug("Spotify token attempt failed (%s): %s", label, resp.text[:200])

        raise AuthError(
            "Spotify refused to issue a web-player token (403/invalid token).\n"
            "• The WEB PLAYER auth flow changed or the sp_dc cookie is stale: log into "
            "open.spotify.com in a private window, copy a fresh Cookie header, update "
            "SPOTIFY_COOKIE.\n"
            "• If it still fails, Spotify rotated the TOTP secret: update "
            "SPOTIFY_TOTP_VER / SPOTIFY_TOTP_CIPHER (see Instructions/TROUBLESHOOTING.md), "
            "or run a sync with SPOTIFY_TOTP_REMOTE=1 so the newest secret is fetched.\n"
            "Details: " + " | ".join(errors[:3])
        )

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._access_expiry - 60:
            return self._access_token
        data = self._token_request()
        if data.get("isAnonymous"):
            raise AuthError(
                "Spotify returned an anonymous (logged-out) token. The sp_dc cookie is "
                "invalid or expired — copy a fresh Cookie header from a logged-in "
                "open.spotify.com tab."
            )
        self._access_token = data["accessToken"]
        expires_ms = data.get("accessTokenExpirationTimestampMs", 0)
        self._access_expiry = (expires_ms / 1000) if expires_ms else time.time() + 3000
        return self._access_token

    def _pace(self) -> None:
        """Soft pacing so a big playlist does not blow through the window."""
        time.sleep(_PACE_SECONDS + min(self._pace_debt, 3.0))

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
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=45,
                )
            except requests.RequestException:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(min(2 ** attempt, 30))
                continue

            if resp.status_code == 200:
                # Cheap call, no rate pressure: keep the soft pace budget full.
                self._pace_debt = max(0.0, self._pace_debt - 1.0)
            if resp.status_code == 429:
                retry_after = _retry_after_seconds(resp.headers, attempt)
                self._pace_debt += 3.0
                if attempt == _MAX_RETRIES - 1:
                    raise RateLimited(
                        retry_after,
                        "Spotify rate limit: still 429 after "
                        f"{_MAX_RETRIES} tries (~{int(self._rate_limit_wait)}s of waiting). "
                        "The web-player token is being throttled; wait a few minutes "
                        "and run again, or lower max_tracks for this run.",
                    )
                self._rate_limit_wait += retry_after
                log.warning(
                    "Spotify rate limit (429) — waiting %.0fs before retry %d/%d",
                    retry_after, attempt + 2, _MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                if attempt == _MAX_RETRIES - 1:
                    raise RuntimeError(
                        f"Spotify HTTP {resp.status_code} after {_MAX_RETRIES} tries"
                    )
                time.sleep(min(2 ** attempt, 30))
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
        # Smaller pages spread requests over time: the web-player token is
        # rate limited per rolling window, so big pages of 100 trip 429s.
        page_size = min(_PAGE_SIZE, max_tracks) if max_tracks else _PAGE_SIZE
        params: dict[str, Any] = {
            "limit": page_size,
            "fields": (
                "items(is_local,track(id,name,type,artists(name),album(name),"
                "duration_ms,external_ids(isrc),uri,popularity)),next"
            ),
        }
        offset = 0
        while True:
            try:
                data = self._request("GET", path, params=params)
            except RateLimited:
                if not tracks:
                    raise
                # Keep what we already have instead of losing the whole read.
                log.error(
                    "Spotify rate limit after %d tracks of playlist %s — using the "
                    "partial result; the rest will be picked up on the next run",
                    len(tracks), playlist_id,
                )
                self.partial_read = True
                break
            self._pace()
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
            offset += len(data.get("items", []))
            if max_tracks is not None:
                # `next` is not in our field mask when limiting — page by offset.
                params = {
                    "limit": page_size,
                    "offset": offset,
                    "fields": params.get("fields", ""),
                }
                continue
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
