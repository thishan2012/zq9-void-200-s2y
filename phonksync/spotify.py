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
import functools
import hashlib
import hmac
import json
import logging
import os
import random
import re
import struct
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from urllib.parse import quote, urlencode
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
# The endpoint the actual web player uses for playlist data. It does NOT go
# through the Web API (api.spotify.com answers 429 to web-player tokens), so a
# throttled Web API does not block it.
_PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
_CLIENT_TOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken"
# Used by the web player itself; overridable in case Spotify bumps it.
_APP_VERSION = "1.3.8.461.g274622d4ef7d"
_WEB_CLIENT_ID = os.environ.get("SPOTIFY_WEB_CLIENT_ID", "d8a5ed958d274c2e8ee717e6a4b0971d")
_MAX_RETRIES = 6
# A long Retry-After is an answer, not an obstacle: retrying into a 24h block
# only burns minutes (4 retries = 117s wasted in one real run). Read it first.
_LONG_BLOCK = 600.0
_PAGE_SIZE = 50
# Delay between every API request. 2s by default (REQUEST_DELAY_SECONDS env
# overrides) — the single cheapest way to stay under the rolling window.
_PACE_SECONDS = 2.0
_MAX_QUEUE = 50
# Spotify can hand out multi-hour blocks after a retry storm. Never sleep that
# long inside a run: stop cleanly, remember the cooldown, continue next run.
_MAX_INLINE_WAIT = 120
# When Spotify answers 429 over and over with short Retry-After values, stop
# knocking and come back later (this is the "5 days" trap we never want).
_SHORT_BLOCK_COOLDOWN = 1800


def _header_seconds(headers: Any) -> float:
    """Raw Retry-After value in seconds (0 when absent/unparsable)."""
    try:
        value = headers.get("Retry-After", "") or ""
    except AttributeError:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def request_delay_seconds() -> float:
    """Delay applied before every request (env: REQUEST_DELAY_SECONDS)."""
    raw = os.environ.get("REQUEST_DELAY_SECONDS", "").strip()
    if not raw:
        return _PACE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning("Ignoring invalid REQUEST_DELAY_SECONDS=%r", raw)
        return _PACE_SECONDS
    return max(0.0, min(value, 30.0))


def backoff_seconds(headers: Any = None, attempt: int = 0, cap: float = 45.0) -> float:
    """Seconds to wait after a 429.

    Order of truth: ``Retry-After`` (seconds or HTTP-date) -> exponential
    backoff ``2 ** attempt`` (floored at 2s) -> jitter, capped at ``cap``.
    This is the one function every retry path uses.
    """
    wait = _header_seconds(headers)
    if not wait:
        try:
            raw = headers.get("Retry-After", "") if headers is not None else ""
        except AttributeError:
            raw = ""
        raw = str(raw or "").strip()
        if raw:
            try:  # HTTP-date form
                wait = max(
                    0.0,
                    (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError):
                wait = 0.0
    if not wait:
        wait = 2 ** max(0, attempt)
    wait = max(wait, 2.0)
    return min(wait, cap) + random.uniform(0, 1.5)


def _retry_after_seconds(headers: Any, attempt: int) -> float:
    """Backwards-compatible alias for :func:`backoff_seconds`."""
    return backoff_seconds(headers, attempt)

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
_API_HOST = "https://api.spotify.com"
_OPEN_HOST = "https://open.spotify.com"
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


def _build_url(host: str, path: str) -> str:
    """URL builder that honours SPOTIFY_PROXY_URL (CORS-proxy style).

    A proxy is expected to accept ``?url=<encoded absolute url>`` (corsproxy.io,
    allorigins, most Vercel/Cloudflare CORS proxies). Two modes:

      * forward — SPOTIFY_PROXY_SECRET is set: the absolute URL is sent in the
        ``x-spotify-url`` header and the proxy forwards it untouched
      * query   — otherwise: ``<proxy>?url=<encoded>`` (corsproxy.io style)
    """
    proxy = os.environ.get("SPOTIFY_PROXY_URL", "").strip().rstrip("/")
    absolute = f"{host}{path}"
    if not proxy:
        return absolute
    if proxy.endswith("raw") and "allorigins" in proxy:
        return f"{proxy}?url={quote(absolute, safe='')}"
    if os.environ.get("SPOTIFY_PROXY_SECRET", "").strip():
        # header mode keeps the path visible; the proxy does the rewrite
        return f"{proxy}/{absolute}"
    return f"{proxy}?url={quote(absolute, safe='')}"


def _with_proxy(proxy: str, url: str, params: dict[str, Any] | None) -> str:
    """Return the proxy URL for ``url`` (params folded in when needed)."""
    proxy = proxy.rstrip("/")
    target = url
    if params:
        target = f"{url}?{urlencode(params, doseq=True)}"
    if os.environ.get("SPOTIFY_PROXY_SECRET", "").strip():
        return f"{proxy}/{target}"
    return f"{proxy}?url={quote(target, safe='')}"


def proxy_headers() -> dict[str, str]:
    """Extra headers for a header-forwarding proxy (header mode only)."""
    secret = os.environ.get("SPOTIFY_PROXY_SECRET", "").strip()
    if not secret:
        return {}
    headers = {"x-proxy-secret": secret, "x-spotify-proxy-secret": secret}
    name = os.environ.get("SPOTIFY_PROXY_SECRET_HEADER", "x-proxy-secret").strip()
    if name and name not in headers:
        headers[name] = secret
    return headers


def proxy_enabled() -> bool:
    return bool(os.environ.get("SPOTIFY_PROXY_URL", "").strip())


_HTML_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


_JS_BUNDLE_RE = re.compile(
    r"(?:https://open\.spotifycdn\.com)?/cdn/build/web-player/web-player\.[0-9a-f]+\.js"
)
# Spotify registers persisted queries as either a query or a **mutation**
# (addToPlaylist is a mutation). Matching only "query" made the self-healing
# scrape silently useless for every write.
_HASH_RE_TEMPLATE = r'"{op}"\s*,\s*"(?:query|mutation)"\s*,\s*"([0-9a-f]{{64}})"'
# Known-good hash for fetchPlaylist (2026-11). Reddit/Spyt/Spotifly all use it;
# refreshed automatically from the web-player bundle when Spotify rotates it.
_KNOWN_PLAYLIST_HASH = "21ea11a6ac75a5f3e01fd365d29c18e0a4a8b7dc09f89bbb5d73910190c6acaf"
# Operation -> persisted-query document hash (Spotify re-uses one document for all
# playlist reads and one for all playlist writes). Healed from the live bundle.
_OP_HASHES: dict[str, str] = {
    "fetchPlaylist": _KNOWN_PLAYLIST_HASH,
    # Seeded from songmirror (working cookie backend); the bundle scrape heals it.
    "fetchPlaylistContents": "a65e12194ed5fc443a1cdebed5fabe33ca5b07b987185d63c72483867ad13cb4",
    # The operation the real web player/syncer uses for a playlist read; a
    # fallback for when the older `fetchPlaylistContents` name is refused.
    "fetchPlaylist": _KNOWN_PLAYLIST_HASH,
    # Proven in a working cookie-based syncer: this is the hash the real web
    # player uses for addToPlaylist. The old value here (which was also pasted
    # into removeFromPlaylist) is not it, and a wrong hash makes the mutation
    # silently do nothing — "the call worked, the playlist stayed empty".
    "addToPlaylist": "b907ad6b088d5bf7680fa81198996d58398c2d55dfcdf59fcc959e0be98a418a",
    # (removeFromPlaylist needs its own hash — they were the same value here by
    # mistake, so a removal would have silently failed. It is unused for now.)
    "removeFromPlaylist": "",
    "searchTracks": "",
}


class PublicPlaylistReader:
    """Read a **public** playlist without any Spotify credentials.

    Used when the Web API is throttled for the account: a patch-request header
    (``_patch=%5B%22browser%22%5D``) makes Spotify's HTML endpoint return the
    render payload server side, which contains ``items`` with the same track
    fields the API returns. No login, no token, no rate-limit budget — the very
    same trick the web player uses to render the page.

    Returns ``None`` when the payload cannot be read (private playlist, layout
    change, odd CDN response) so the caller can fall back to the cache.
    """

    def __init__(self, session: requests.Session | None = None, client_token: str = ""):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _HTML_UA})
        self.last_error = ""
        self._client_token = client_token
        self._token_tried = bool(client_token)
        self._pathfinder_error = ""

    def token(self) -> str:
        if not self._token_tried:
            self._client_token = client_token()
            self._token_tried = True
        return self._client_token

    def _payload(self, playlist_id: str) -> dict[str, Any] | None:
        url = f"{_OPEN_HOST}/playlist/{playlist_id}"
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://open.spotify.com/",
            "app-platform": "WebPlayer",
            "_patch": '%22browser%22',
            **proxy_headers(),
        }
        proxy = os.environ.get("SPOTIFY_PROXY_URL", "").strip()
        try:
            if proxy:
                resp = self.session.get(_with_proxy(proxy, url, None), headers=headers, timeout=30)
            else:
                resp = self.session.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            self.last_error = f"network: {exc}"
            return None
        if resp.status_code != 200:
            self.last_error = f"HTTP {resp.status_code}"
            return None
        data = _find_playlist_payload(resp.text)
        if data:
            return data
        # The HTML page only inlines the payload for some renders. The web
        # player normally uses api-partner instead — try that before giving up.
        fallback = self._pathfinder(playlist_id)
        if fallback:
            return fallback
        if not self.last_error:
            self.last_error = "payload not found in HTML"
            if self._pathfinder_error:
                self.last_error += f"; web player: {self._pathfinder_error}"
        return None

    def _pathfinder(self, playlist_id: str) -> dict[str, Any] | None:
        token = self.token()
        try:
            tracks = pathfinder_playlist(playlist_id, client_token=token)
            self._pathfinder_error = ""
        except Exception as exc:
            log.warning("pathfinder read failed: %s", exc)
            # Keep the reason: "0 tracks" from a rejected hash and "0 tracks"
            # from an empty playlist look identical from the outside.
            self._pathfinder_error = str(exc).splitlines()[0][:160]
            return None
        if not tracks:
            self.last_error = (
                "payload not found in HTML; web player answered 0 tracks "
                f"({self._pathfinder_error or 'empty playlist?'})"
            )
            return None
        items = [
            {"itemV2": {"data": {
                "uri": t.uri, "name": t.name,
                "artists": {"items": [{"profile": {"name": a}} for a in t.artists]},
                "albumOfTrack": {"name": t.album},
                "trackDuration": {"totalMilliseconds": t.duration_ms},
            }}}
            for t in tracks
        ]
        self.last_error = ""
        return {"playlistV2": {"content": {"items": items, "totalCount": len(items)}}}

    def snapshot(self, playlist_id: str) -> str:
        data = self._payload(playlist_id)
        if not data:
            return ""
        items = ((data.get("playlistV2") or {}).get("content") or {}).get("items") or []
        return f"{len(items)}:{data.get('snapshotId', '')}"

    def playlist_tracks(self, playlist_id: str, max_tracks: int = 0) -> list[Track]:
        self.last_error = ""
        data = self._payload(playlist_id)
        if not data:
            if not self.last_error:
                self.last_error = "payload not found in HTML"
            return []
        content = (data.get("playlistV2") or {}).get("content") or {}
        items = content.get("items") or []
        tracks: list[Track] = []
        for item in items:
            track = _track_from_item(item)
            if track:
                track.sources = [playlist_id]
                tracks.append(track)
        if not tracks:
            self.last_error = "no tracks in payload"
        return tracks


def _proxy_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> str:
    """POST ``payload`` to ``url``, optionally through SPOTIFY_PROXY_URL.

    Returns the response text. Raises ``requests.RequestException`` on network
    errors and ``_ProxyUnavailable`` when the proxy itself fails, so the caller
    can decide to go direct.
    """
    proxy = os.environ.get("SPOTIFY_PROXY_URL", "").strip()
    if not proxy:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        return resp.text
    proxied = _with_proxy(proxy, url, None)
    try:
        resp = requests.post(
            proxied, json=payload, headers={**headers, **proxy_headers()}, timeout=30
        )
    except requests.RequestException as exc:
        log.warning("Proxy POST failed (%s) — using a direct connection", exc)
        return requests.post(url, json=payload, headers=headers, timeout=30).text
    if resp.status_code >= 400:
        log.warning(
            "Proxy POST returned HTTP %s — using a direct connection", resp.status_code
        )
        return requests.post(url, json=payload, headers=headers, timeout=30).text
    try:
        json.loads(resp.text)
    except ValueError:
        # Many CORS proxies only forward GET; their HTML error page is not JSON.
        log.warning("Proxy POST returned non-JSON — using a direct connection")
        return requests.post(url, json=payload, headers=headers, timeout=30).text
    return resp.text


class _ProxyUnavailable(RuntimeError):
    """The CORS proxy could not serve the request (fall back to direct)."""


def _hash_overrides() -> dict[str, str]:
    """Per-operation hash overrides (`SPOTIFY_HASH_<OP>` env, else empty).

    When Spotify rotates a hash the bundle scrape normally heals it; this is the
    escape hatch when the bundle layout changes too:
    ``SPOTIFY_HASH_ADDTOPLAYLIST=<64 hex chars>`` (also as a repo secret).
    """
    found: dict[str, str] = {}
    for op in list(_OP_HASHES):
        key = "SPOTIFY_HASH_" + op.upper()
        value = os.environ.get(key, "").strip()
        if len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower()):
            found[op] = value
    return found


@functools.lru_cache(maxsize=4)
def _discover_cached(stamp: int, ops: tuple[str, ...]) -> dict[str, str]:
    return _discover_persisted_hashes_once(list(ops))


def _discover_persisted_hashes(ops: list[str]) -> dict[str, str]:
    """Read persisted-query hashes, caching a *successful* scrape for 10 min.

    A failed scrape is never cached (the next attempt may well succeed), because
    caching an empty answer would leave the reader permanently broken.
    """
    stamp = int(time.time() // 600)
    found = dict(_discover_cached(stamp, tuple(sorted(ops))))
    if not found:
        _discover_cached.cache_clear()
    return found


def _discover_persisted_hashes_once(ops: list[str]) -> dict[str, str]:
    """Read persisted-query hashes from the live web-player bundle.

    Spotify rotates these; they are constants inside the JS bundle, so reading
    the bundle keeps the reader working without a code change.
    """
    found: dict[str, str] = {}
    try:
        home = requests.get(
            _SERVER_TIME_URL,
            headers={"User-Agent": _HTML_UA, "Accept": "text/html,application/xhtml+xml"},
            timeout=30,
        )
        match = _JS_BUNDLE_RE.search(home.text)
        if not match:
            return found
        bundle_url = match.group(0)
        if bundle_url.startswith("/"):
            bundle_url = "https://open.spotifycdn.com" + bundle_url
        bundle = requests.get(
            bundle_url,
            headers={"User-Agent": _HTML_UA, "Accept": "*/*", "Accept-Encoding": "gzip"},
            timeout=60,
        )
        for op in ops:
            for pattern in (
                _HASH_RE_TEMPLATE.format(op=op),
                r'\.l\("' + re.escape(op) + r'","(?:query|mutation)","([a-f0-9]{64})"',
            ):
                hit = re.search(pattern, bundle.text)
                if hit:
                    found[op] = hit.group(1)
                    break
    except Exception as exc:  # discovery is best-effort
        log.debug("Could not read persisted-query hashes: %s", exc)
    return found


_PLAY_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://open.spotify.com/",
    "Origin": "https://open.spotify.com",
    "app-platform": "WebPlayer",
    "spotify-app-version": _APP_VERSION,
}


def pathfinder_playlist(
    playlist_id: str,
    client_token: str = "",
    page_size: int = 250,
    max_tracks: int = 0,
    fetcher: Any = None,
) -> list[Track]:
    """Read a playlist through api-partner (the web player's own endpoint).

    No login cookie needed: the *client token* is enough, which is exactly what
    open.spotify.com gets for a logged-out visitor. This path is not the Web API,
    so the account/API 429 does not apply to it.
    """
    fetch = fetcher or _proxy_post
    hashes = {"fetchPlaylist": _KNOWN_PLAYLIST_HASH, "fetchPlaylistContents": _KNOWN_PLAYLIST_HASH}
    tracks: dict[str, Track] = {}
    offset = 0
    total = None
    op = "fetchPlaylist"
    while True:
        payload = {
            "variables": {
                "uri": f"spotify:playlist:{playlist_id}",
                "offset": offset,
                "limit": page_size,
                "enableWatchFeedEntrypoint": True,
                "includeEpisodeContentRatingsV2": True,
            },
            "operationName": op,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": hashes[op]}},
        }
        headers = dict(_PLAY_HEADERS)
        headers["User-Agent"] = _HTML_UA
        if client_token:
            headers["client-token"] = client_token
        text = fetch(_PATHFINDER_URL, payload, headers)
        try:
            data = json.loads(text)
        except ValueError:
            raise RuntimeError(f"pathfinder returned non-JSON ({text[:120]!r})")
        if data.get("errors"):
            message = json.dumps(data["errors"])[:200]
            if "persisted" in message.lower():
                fresh = _discover_persisted_hashes(["fetchPlaylist", "fetchPlaylistContents"])
                if op in fresh and fresh[op] != hashes[op]:
                    hashes[op] = fresh[op]
                    continue
            raise RuntimeError(f"pathfinder error: {message}")
        body = (data.get("data") or {}).get("playlistV2") or {}
        if not body:
            break
        content = body.get("content") or {}
        items = content.get("items") or []
        if total is None:
            try:
                total = int(content.get("totalCount") or 0) or None
            except (TypeError, ValueError):
                total = None
        for item in items:
            track = _track_from_item(item)
            if track:
                tracks.setdefault(track.id, track)
        offset += len(items)
        if not items or (total and offset >= total) or len(items) < page_size:
            break
        if max_tracks and len(tracks) >= max_tracks:
            break
        time.sleep(request_delay_seconds())
    for track in tracks.values():
        track.sources = [playlist_id]
    return list(tracks.values())


def client_token() -> str:
    """A web-player *client token* (no login needed), or "" when refused."""
    payload = {
        "client_data": {
            "client_version": _APP_VERSION,
            "client_id": _WEB_CLIENT_ID,
            "js_sdk_data": {
                "device_brand": "unknown",
                "device_model": "unknown",
                "os": "windows",
                "os_version": "10",
                "device_id": os.urandom(16).hex(),
                "device_type": "computer",
            },
        }
    }
    try:
        text = _proxy_post(_CLIENT_TOKEN_URL, payload, {"User-Agent": _HTML_UA, **_PLAY_HEADERS})
        granted = (json.loads(text).get("granted_token") or {})
        token = granted.get("token")
        return token if isinstance(token, str) else ""
    except (requests.RequestException, ValueError, _ProxyUnavailable) as exc:
        log.debug("No client token: %s", exc)
        return ""


def playlist_tracks_from_html(html: str, playlist_id: str = "") -> list[Track]:
    """Parse tracks out of any Spotify page/payload text (collection import)."""
    data = _find_playlist_payload(html)
    if not data:
        return []
    items = ((data.get("playlistV2") or {}).get("content") or {}).get("items") or []
    tracks: list[Track] = []
    for item in items:
        track = _track_from_item(item)
        if track:
            track.sources = [playlist_id] if playlist_id else []
            tracks.append(track)
    return tracks


def _find_playlist_payload(html: str) -> dict[str, Any] | None:
    """Locate the JSON entity that holds ``playlistV2`` in a render payload."""
    decoder = json.JSONDecoder()
    start = 0
    needles = ('"playlistV2"', '\\"playlistV2\\"')
    while True:
        best = -1
        for needle in needles:
            found = html.find(needle, start)
            if found != -1 and (best == -1 or found < best):
                best = found
        if best == -1:
            return None
        brace = html.rfind("{", max(0, best - 4000), best)
        while brace != -1:
            try:
                obj, _ = decoder.raw_decode(html[brace:])
            except ValueError:
                brace = html.rfind("{", max(0, best - 4000), brace)
                continue
            data = _unwrap_entity(obj)
            if isinstance(data, dict) and isinstance(data.get("playlistV2"), dict):
                return data
            brace = html.rfind("{", max(0, best - 4000), brace)
        start = best + 1


def _unwrap_entity(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    for key in ("data", "payload", "entity"):
        inner = obj.get(key)
        if isinstance(inner, dict):
            found = _unwrap_entity(inner)
            if found:
                return found
    return obj


def _name_of(node: Any) -> str:
    if isinstance(node, dict):
        for key in ("name", "title", "text"):
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _track_from_item(item: dict[str, Any]) -> Track | None:
    """Map a ``playlistV2`` item onto our Track model.

    The HTML payload mirrors the GraphQL shape:
    ``{"itemV2": {"data": {"uri", "name", "artists": {"items": [{"profile": {"name"}}]},
    "albumOfTrack": {"name"}, "trackDuration": {"totalMilliseconds"}}}}``
    """
    if not isinstance(item, dict):
        return None
    body = item.get("itemV2") or item.get("item") or item
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None

    uri = data.get("uri") if isinstance(data.get("uri"), str) else ""
    if not uri.startswith("spotify:track:"):
        uri = ""
    track_id = data.get("id") or (uri.split(":")[-1] if uri else "")
    if not track_id:
        return None

    artists = [
        _name_of(entry.get("profile"))
        for entry in (data.get("artists") or {}).get("items") or []
        if isinstance(entry, dict)
    ]
    duration_ms = 0
    duration = data.get("trackDuration")
    if isinstance(duration, dict) and duration.get("totalMilliseconds"):
        try:
            duration_ms = int(duration["totalMilliseconds"])
        except (TypeError, ValueError):
            duration_ms = 0

    return Track(
        id=str(track_id),
        name=_name_of(data.get("name")) or str(data.get("name") or ""),
        artists=[a for a in artists if a],
        album=_name_of(data.get("albumOfTrack")),
        duration_ms=duration_ms,
        isrc="",  # not exposed in the HTML payload; the matcher copes
        uri=uri or f"spotify:track:{track_id}",
        popularity=0,
    )


class SpotifyClient:
    def __init__(
        self,
        cookie_header: str,
        session: requests.Session | None = None,
        extra_cookies: list[str] | None = None,
        prefer_web_player: bool = False,
    ):
        _apply_cipher_override()
        self.sp_dc = extract_sp_dc(cookie_header)
        # Extra accounts (spares): if one cookie gets rate limited we simply
        # switch to the next. Spotify limits per token, so a different account
        # is a clean way to keep going without retry storming.
        self._cookie_pool: list[str] = [self.sp_dc]
        for raw in extra_cookies or []:
            if not raw.strip():
                continue
            try:
                value = extract_sp_dc(raw)
            except AuthError as exc:
                log.warning("Ignoring extra Spotify cookie: %s", exc)
                continue
            if value not in self._cookie_pool:
                self._cookie_pool.append(value)
        self._cookie_index = 0
        self.cookie_switches = 0
        self._request_count = 0
        self._rate_limited_this_run = False
        self.writes_this_run = 0  # playlist/queue writes attempted
        self.writes_landed = 0    # writes confirmed by the web-player endpoint
        self.writes_attempted = 0  # tracks that were supposed to be added
        self.verify_write_result = False
        self._read_op_name = ""  # the playlist-read operation that works
        self.account_username = ""   # who the cookie belongs to
        self.token_client_id = ""
        self.last_write_error = ""   # why the last write was refused
        self.last_write_route = ""   # "web-player" or "rest"
        self.last_write_answer = ""  # the mutation payload Spotify returned
        self.last_mutation_key = ""  # which data key carried it
        self.last_add_accepted = False  # the API said ok (not proof of landing)
        self.request_delay = request_delay_seconds()
        self._queued: list[str] = []
        self.queue_saves = 0
        self.queue_supported = True
        self.blocked_until = 0.0
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _UA})
        self._access_token = ""
        self._access_expiry = 0.0
        self._rate_limit_wait = 0.0
        self._pace_debt = 0.0
        self.partial_read = False
        self.proxy_url = os.environ.get("SPOTIFY_PROXY_URL", "").strip()
        self.proxy_used = 0
        self.direct_used = 0
        self.last_token_route = ""  # "proxy" | "direct", for the diagnostic
        self.proxy_forced_direct = False  # the proxy answered as logged-out
        # Set once pathfinder has been seen to work for this account: plays the
        # same role as a "backend" switch (rest -> web player).
        #
        # A free (non-Premium) account — which every bot account is — gets a 403
        # from the queue (Premium-only) and a fast 429 from the Web API, while the
        # web player's own endpoint does neither. Start there instead of waiting
        # for the first refusal.
        self.prefer_web_player = bool(prefer_web_player)
        self.queue_supported = not self.prefer_web_player
        self.pathfinder_ok = self.prefer_web_player or os.environ.get("SPOTIFY_BACKEND", "").strip().lower() in {
            "web-player", "webplayer", "pathfinder", "graphql",
        }
        self.prefer_pathfinder = self.pathfinder_ok
        if self.prefer_web_player:
            log.info(
                "Spotify writes: web-player route from the start (free account — "
                "no Premium queue, no Web API write budget)"
            )
        self.backend = "web-player" if self.prefer_pathfinder else "rest"

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
        """Get a web-player token; never let the proxy turn a good cookie anonymous."""
        data = self._token_request_once(use_proxy=bool(self.proxy_url))
        if data.get("isAnonymous") and self.proxy_url:
            log.warning(
                "The proxy route returned a logged-out Spotify token — retrying over a "
                "direct connection (the proxy is dropping or truncating the Cookie)"
            )
            self.proxy_forced_direct = True
            self.last_token_route = "direct"
            data = self._token_request_once(use_proxy=False)
        return data

    def _token_request_once(self, use_proxy: bool) -> dict[str, Any]:
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

        token_headers = {
            "Accept": "application/json",
            "Referer": "https://open.spotify.com/",
            "App-Platform": "WebPlayer",
        }
        # The token endpoint answers 429 too (and it is the *first* call of a
        # run, which is why a throttled token used to look like a broken
        # cookie).  Wait out short windows, park the run on a long one.
        self.last_token_route = "proxy" if use_proxy else "direct"
        for url, params, label in attempts:
            resp = None
            for _round in range(3):
                try:
                    resp = self._token_http(url, params, token_headers, use_proxy)
                except requests.RequestException as exc:
                    if use_proxy:
                        log.warning(
                            "Token request via proxy failed (%s) — trying direct", exc
                        )
                        use_proxy = False
                        continue
                    break
                if resp.status_code == 429 and _round < 2:
                    wait = _retry_after_seconds(resp.headers, _round)
                    raw_wait = _header_seconds(resp.headers) or 0.0
                    if max(wait, raw_wait) > _MAX_INLINE_WAIT:
                        wait = max(wait, raw_wait)
                        self.blocked_until = time.time() + wait
                        raise RateLimited(
                            wait,
                            f"Spotify is throttling the token endpoint for "
                            f"~{_human_duration(wait)}. Nothing was lost — the next run "
                            "resumes here, or add a spare cookie (SPOTIFY_COOKIE_2).",
                        )
                    if use_proxy:
                        use_proxy = False
                        log.warning("Token endpoint 429 — retrying on a direct connection")
                        continue
                    self._rate_limit_wait += wait
                    log.warning(
                        "Token endpoint rate limit (429) — waiting %.0fs before retry %d/2",
                        wait, _round + 2,
                    )
                    time.sleep(wait)
                    continue
                break
            if resp is None:
                errors.append(f"{label}: connection failed")
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    errors.append(f"{label}: response was not JSON")
                    continue
                if data.get("accessToken"):
                    log.debug("Spotify token acquired via %s", label)
                    username = data.get("username") or data.get("displayName") or ""
                    if username:
                        self.account_username = str(username)
                    self.token_client_id = str(data.get("clientId") or "")
                    return data
                errors.append(f"{label}: no accessToken in response")
                continue
            errors.append(f"{label}: HTTP {resp.status_code}")
            log.debug("Spotify token attempt failed (%s): %s", label, resp.text[:200])

        if any("HTTP 429" in e for e in errors):
            cooldown = _SHORT_BLOCK_COOLDOWN
            self.blocked_until = time.time() + cooldown
            raise RateLimited(
                cooldown,
                "Spotify is throttling the token endpoint (HTTP 429), so no API call can "
                f"be made right now. Parking the run for {_human_duration(cooldown)} — "
                "nothing was lost, the next run resumes here. Reading playlists from the "
                "public page still works in the meantime.",
            )
        if self._rotate_cookie():
            return self._token_request()
        self.totp_failed = True
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

    def _pathfinder(self, op: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run one web-player GraphQL operation (api-partner, no Web API quota).

        Uses the same bearer the web player uses (the API 429 mentioned above is
        the *Web API*; api-partner answers to web-player tokens). A rotated
        persisted-query hash is healed once, a 401 re-mints the token once.
        """
        hashes = dict(_OP_HASHES)
        for name, value in _hash_overrides().items():
            hashes[name] = value
        refreshed_hash = False
        refreshed_auth = False
        last = ""
        for _ in range(3):
            body = {
                "variables": variables,
                "operationName": op,
                "extensions": {
                    "persistedQuery": {"version": 1, "sha256Hash": hashes.get(op, "")}
                },
            }
            headers = dict(_PLAY_HEADERS)
            headers["User-Agent"] = _HTML_UA
            headers["Authorization"] = f"Bearer {self._ensure_token()}"
            headers.update(proxy_headers())
            try:
                text = _proxy_post(_PATHFINDER_URL, body, headers)
            except requests.RequestException as exc:
                raise RateLimited(5, f"pathfinder unreachable: {exc}") from exc
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                last = f"non-JSON answer ({text[:120]!r})"
                continue
            errors = payload.get("errors") or []
            message = json.dumps(errors)[:300] if errors else ""
            if errors and "persisted" in message.lower() and not refreshed_hash:
                fresh = _discover_persisted_hashes([op])
                if fresh.get(op):
                    hashes[op] = fresh[op]
                refreshed_hash = True
                continue
            if errors and ("401" in message or "unauthor" in message.lower()):
                if refreshed_auth:
                    raise AuthError(f"pathfinder rejected the token: {message}")
                self._access_token = ""
                refreshed_auth = True
                continue
            if errors:
                raise RuntimeError(f"pathfinder {op} error: {message}")
            return payload.get("data") or {}
        raise RuntimeError(f"pathfinder {op} failed: {last or 'no usable answer'}")

    def _read_op_variables(self, op: str, playlist_id: str, offset: int) -> dict[str, Any]:
        if op == "fetchPlaylist":
            # The browser sends these three flags; the endpoint is picky about them.
            return {
                "uri": f"spotify:playlist:{playlist_id}",
                "offset": offset,
                "limit": 100,
                "enableWatchFeedEntrypoint": True,
                "includeEpisodeContentRatingsV2": True,
            }
        return {"uri": f"spotify:playlist:{playlist_id}", "offset": offset, "limit": 100}

    def _pathfinder_write(self, playlist_id: str, uri: str) -> bool:
        """Add a single track (thin wrapper around the batch call)."""
        return self._pathfinder_write_many(playlist_id, [uri])

    def _pathfinder_write_many(self, playlist_id: str, uris: list[str]) -> bool:
        """Add one track via addToPlaylist; verified, and hash-healed like reads.

        Reads and writes must fail the same way: a rotating persisted-query hash
        (it happened for reads) would otherwise kill every write silently while
        the playlist stays empty.
        """
        self.verify_write_result = True
        hashes = dict(_OP_HASHES)
        for name, value in _hash_overrides().items():
            hashes[name] = value
        # The browser sends the whole batch in ONE call, an explicit interactionId,
        # and confirms on `addItemsToPlaylist.__typename`.
        variables = {
            "playlistUri": f"spotify:playlist:{playlist_id}",
            "playlistItemUris": list(uris) if isinstance(uris, list) else [uris],
            "newPosition": {"moveType": "BOTTOM_OF_PLAYLIST", "fromUid": None},
            "interactionId": None,
        }
        refreshed_hash = False
        refreshed_auth = False
        last = ""
        for _ in range(3):
            body = {
                "variables": variables,
                "operationName": "addToPlaylist",
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": hashes.get("addToPlaylist", ""),
                    }
                },
            }
            headers = dict(_PLAY_HEADERS)
            headers["User-Agent"] = _HTML_UA
            headers["Authorization"] = f"Bearer {self._ensure_token()}"
            headers.update(proxy_headers())
            try:
                text = _proxy_post(_PATHFINDER_URL, body, headers)
            except requests.RequestException as exc:
                raise RateLimited(5, f"pathfinder write unreachable: {exc}") from exc
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                last = f"non-JSON answer ({text[:120]!r})"
                continue
            errors = payload.get("errors") or []
            message = json.dumps(errors)[:300] if errors else ""
            if errors and "persisted" in message.lower() and not refreshed_hash:
                fresh = _discover_persisted_hashes(["addToPlaylist"])
                if fresh.get("addToPlaylist"):
                    hashes["addToPlaylist"] = fresh["addToPlaylist"]
                refreshed_hash = True
                continue
            if errors and ("401" in message or "unauthor" in message.lower()):
                if refreshed_auth:
                    raise AuthError(f"pathfinder write rejected the token: {message}")
                self._access_token = ""
                refreshed_auth = True
                continue
            if errors:
                raise RuntimeError(f"pathfinder addToPlaylist error: {message}")
            # A 200 with no `errors` is NOT proof: the persisted query can point at
            # an operation that does nothing. Require a mutation payload we
            # recognise (the web player answers `addToPlaylist`/`add`), and treat
            # anything else as a refusal so the caller never claims a write.
            data = payload.get("data") or {}
            mutated = (
                data.get("addItemsToPlaylist")
                or data.get("addToPlaylist")
                or data.get("add")
            )
            if isinstance(mutated, dict) and (
                mutated.get("__typename") or mutated.get("uri") or mutated.get("uid")
            ):
                self.last_write_answer = json.dumps(mutated)[:300]
                self.last_mutation_key = next(
                    key for key in ("addItemsToPlaylist", "addToPlaylist", "add")
                    if isinstance(data.get(key), dict)
                )
                return True
            status = (data.get("playlistV2") or {}).get("__typename") or ""
            last = (
                f"no mutation in the answer (keys: {sorted(data) or 'none'}"
                + (f", status {status}" if status else "")
                + ")"
            )
            continue
        raise RuntimeError(f"pathfinder addToPlaylist failed: {last or 'no usable answer'}")

    def _pathfinder_tracks(self, playlist_id: str, max_tracks: int = 0) -> list[Track]:
        """Read a playlist through the web player endpoint (paged)."""
        tracks: dict[str, Track] = {}
        offset = 0
        total = 0
        op = self._read_op_name or "fetchPlaylistContents"
        while True:
            data = self._pathfinder(op, self._read_op_variables(op, playlist_id, offset))
            content = ((data.get("playlistV2") or {}).get("content") or {})
            items = content.get("items") or []
            if not items and not self._read_op_name:
                # The older operation name may simply not exist any more; the
                # browser's own is `fetchPlaylist`. Remember whichever works so
                # the rest of the run pays for this once.
                other = "fetchPlaylist" if op != "fetchPlaylist" else ""
                if other and not offset:
                    try:
                        data = self._pathfinder(
                            other, self._read_op_variables(other, playlist_id, 0)
                        )
                        content = ((data.get("playlistV2") or {}).get("content") or {})
                        items = content.get("items") or []
                        if items:
                            self._read_op_name = other
                            op = other
                    except Exception as exc:
                        log.debug("Playlist read via %s failed: %s", other, exc)
            if not items and not self._read_op_name:
                self._read_op_name = op  # what we tried does answer, even if empty
            if not total:
                try:
                    total = int(content.get("totalCount") or 0)
                except (TypeError, ValueError):
                    total = 0
            for item in items:
                track = _track_from_item(item)
                if track:
                    track.sources = [playlist_id]
                    tracks.setdefault(track.id, track)
            offset += len(items)
            if not items or (total and offset >= total) or len(items) < 100:
                break
            if max_tracks and len(tracks) >= max_tracks:
                break
            self._pace()
        return list(tracks.values())

    def _pathfinder_add(self, playlist_id: str, uris: list[str]) -> None:
        """Add tracks through the web player (no Web API, no 30s rolling window)."""
        self.last_write_route = "web-player"
        # One mutation per 100 URIs, exactly like the browser: fewer calls, less
        # rate limit, and no half-applied batch to reason about.
        for start in range(0, len(uris), 100):
            chunk = uris[start : start + 100]
            try:
                self._pathfinder_write_many(playlist_id, chunk)
            except Exception as exc:
                self.last_write_error = f"web-player: {str(exc)[:200]}"
                raise
            self.writes_landed += len(chunk)
            self._pace()

    def playlist_info(self, playlist_id: str) -> dict[str, str]:
        """Owner/name of a playlist, best effort — no extra endpoint.

        With a *collaborative* playlist the account in the cookie must be the
        owner or an accepted collaborator, otherwise every write is refused while
        reads keep working. This is what tells those two apart.
        """
        info: dict[str, str] = {}
        try:
            data = self._pathfinder(
                "fetchPlaylistContents",
                {"uri": f"spotify:playlist:{playlist_id}", "offset": 0, "limit": 1},
            )
        except Exception as exc:
            log.debug("Could not read the playlist owner: %s", exc)
            return info
        playlist = data.get("playlistV2") or {}
        name = playlist.get("name")
        if isinstance(name, str) and name:
            info["name"] = name
        owner = (playlist.get("ownerV2") or {}).get("data") or {}
        for key in ("username", "uri", "name"):
            value = owner.get(key)
            if isinstance(value, str) and value:
                info["owner_" + key] = value
        if "collaborative" in playlist:
            info["collaborative"] = "true" if playlist.get("collaborative") else "false"
        return info

    def permission_hint(self, playlist_id: str) -> str:
        """One line that says whether *this* account may write to that playlist."""
        info = self.playlist_info(playlist_id)
        if not info:
            return ""
        me = self.account_username
        owner = info.get("owner_username") or info.get("owner_name") or ""
        collab = info.get("collaborative")
        bits = [f"playlist owner: {owner or 'unknown'}"]
        if me:
            bits.append(f"cookie account: {me}")
        if collab is not None:
            bits.append(f"collaborative: {collab}")
        if me and owner and me.lower() == owner.lower():
            bits.append("this account OWNS the playlist — a refusal is not permissions")
        elif me and owner:
            bits.append(
                "this account does NOT own the playlist: it must be collaborative "
                "AND this account an accepted collaborator"
            )
        return "; ".join(bits)

    def confirm_one(self, playlist_id: str, uri: str, expected_total: int | None = None) -> bool | None:
        """Is this one URI in the playlist? ``None`` = the read itself failed.

        Called right after a write answers "ok". A wrong persisted-query hash
        answers HTTP 200 with an empty body (a silent no-op), so the *answer* is
        not evidence — this read is.
        """
        try:
            tracks = self._pathfinder_tracks(playlist_id)
        except Exception as exc:
            log.debug("Could not confirm the write: %s", exc)
            return None
        present = {t.uri or f"spotify:track:{t.id}" for t in tracks}
        present |= {f"spotify:track:{t.id}" for t in tracks}
        if uri in present:
            return True
        if expected_total is not None and len(tracks) < expected_total:
            # The playlist read returned *less* than it had before the write: the
            # read is unreliable, not the write.
            return None
        return False

    def verify_added(self, playlist_id: str, uris: list[str]) -> list[str]:
        """Re-read the playlist and return the URIs that are really in it.

        A silent no-op is the worst failure mode here: five tracks were reported
        as added while the playlist stayed empty. This is the only proof.
        """
        try:
            tracks = self._pathfinder_tracks(playlist_id)
        except Exception as exc:  # pragma: no cover - network
            log.warning("Could not verify the playlist contents: %s", exc)
            return []
        present = {
            (t.uri or f"spotify:track:{t.id}") for t in tracks
        } | {f"spotify:track:{t.id}" for t in tracks}
        return [uri for uri in uris if uri in present]

    def _token_http(
        self,
        url: str,
        params: dict[str, str],
        token_headers: dict[str, str],
        use_proxy: bool,
    ) -> requests.Response:
        """One token request, through the proxy or direct."""
        if use_proxy:
            # A forwarding proxy relays headers but not a cookie jar, so sp_dc
            # travels as a plain Cookie header. `sp_dc=` + ClientToken only is
            # a *complete* login cookie for the web player; the browser's full
            # header (with sp_key/sp_t…) is not needed and CORS proxies tend to
            # mangle or truncate long Cookie values (that is what produced an
            # anonymous token while the direct route worked).
            resp = self.session.get(
                _with_proxy(self.proxy_url, url, params or None),
                headers={
                    **token_headers,
                    **proxy_headers(),
                    "Cookie": f"sp_dc={self.sp_dc}; sp_dc_csrf=1",
                },
                timeout=30,
            )
            self.proxy_used += 1
            return resp
        resp = self.session.get(
            url,
            params=params or None,
            headers=token_headers,
            cookies={"sp_dc": self.sp_dc},
            timeout=30,
        )
        self.direct_used += 1
        return resp

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._access_expiry - 60:
            return self._access_token
        data = self._token_request()
        if data.get("isAnonymous"):
            # The cookie in use is dead but another account may still work. This
            # used to raise immediately, so a stale SPOTIFY_COOKIE hid a perfectly
            # good SPOTIFY_COOKIE_2.
            if self._rotate_cookie():
                data = self._token_request()
            if data.get("isAnonymous"):
                raise AuthError(self._anonymous_message())
        self._access_token = data["accessToken"]
        expires_ms = data.get("accessTokenExpirationTimestampMs", 0)
        self._access_expiry = (expires_ms / 1000) if expires_ms else time.time() + 3000
        return self._access_token

    def _anonymous_message(self) -> str:
        """Why the token came back logged-out, in the order of real causes."""
        where = self.last_token_route or "direct"
        lines = [
            "Spotify returned an anonymous (logged-out) token.",
            "",
            f"Cookie tried: #{self._cookie_index + 1} of {len(self._cookie_pool)} "
            f"(sp_dc {len(self.sp_dc)} chars) via the {where} route.",
        ]
        if self.proxy_forced_direct:
            lines.insert(
                1,
                "The proxy route answered logged-out first, so it was retried "
                "direct and that failed too.",
            )
        if self.proxy_url and (where == "proxy" or self.proxy_forced_direct):
            lines += [
                "",
                "The proxy route is the prime suspect: a CORS/forwarding proxy can "
                "drop or truncate the Cookie header, which makes Spotify answer as "
                "logged-out for a cookie that works direct.",
                "Try one run with the proxy off (empty SPOTIFY_PROXY_URL / PROXY_URL).",
            ]
        if len(self._cookie_pool) > 1:
            lines.append(
                f"Both cookies were tried ({len(self._cookie_pool)}), so this is not "
                "just a stale spare."
            )
        else:
            lines.append(
                "Only one cookie is configured: SPOTIFY_COOKIE_2 is empty or was "
                "ignored, so a stale SPOTIFY_COOKIE has nothing to fall back to."
            )
        lines += [
            "",
            "How to get a good value: open.spotify.com in a private window → log in "
            "→ DevTools → Network → any request → copy the whole `Cookie:` header → "
            "`python -m phonksync cookie-fix --show` → paste ONLY the sp_dc value.",
        ]
        return "\n".join(lines)

    def _park_or_rotate(self, retry_after: float) -> None:
        """A long Retry-After: use a spare account, or stop this run at once.

        Retrying into a multi-hour block is pure loss (one real run waited 117s
        for a 24h block). Nothing is slept, nothing is stored.
        """
        if self._cookie_index + 1 < len(self._cookie_pool):
            return  # the caller retries with the next account's cookie
        self.blocked_until = time.time() + retry_after
        raise RateLimited(
            retry_after,
            f"Spotify blocked this account for ~{_human_duration(retry_after)} "
            "(Retry-After). Nothing was retried: a block this long is an answer, so "
            "this run continues on the web-player route (no Web API calls).",
        )

    def _rotate_cookie(self) -> bool:
        """Switch to the next spare account's cookie. True if one was left."""
        if self._cookie_index + 1 >= len(self._cookie_pool):
            return False
        self._cookie_index += 1
        self.sp_dc = self._cookie_pool[self._cookie_index]
        self._access_token = ""
        self._access_expiry = 0.0
        self.totp_failed = False
        self.cookie_switches += 1
        log.warning(
            "Spotify cookie #%d got rate limited — switching to spare cookie #%d/%d",
            self._cookie_index, self._cookie_index + 1, len(self._cookie_pool),
        )
        return True

    @property
    def cookie_count(self) -> int:
        return len(self._cookie_pool)

    @property
    def requests_made(self) -> int:
        """How many Web-API calls this client has made (pacing honesty check)."""
        return self._request_count

    @property
    def rate_limited_this_run(self) -> bool:
        """Spotify answered 429 during this run — stop spending calls on it."""
        return self._rate_limited_this_run

    def _pace(self) -> None:
        """Sleep the configured gap before a request (default 2s)."""
        delay = self.request_delay + min(self._pace_debt, 3.0)
        if delay > 0:
            time.sleep(delay)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        _retry_auth: bool = True,
        allow_404: bool = False,
    ) -> dict[str, Any]:
        self._request_count += 1
        token = self._ensure_token()
        url = path if path.startswith("http") else f"{_API}/{path}"
        # A proxy (clean IP) is preferred; on 429 we alternate proxy <-> direct
        # because Spotify limits by IP *and* account, so a different egress can
        # clear the block.
        use_proxy = bool(self.proxy_url)
        for attempt in range(_MAX_RETRIES):
            try:
                if use_proxy:
                    target = _with_proxy(self.proxy_url, url, params)
                    headers = {"Authorization": f"Bearer {token}", **proxy_headers()}
                    resp = self.session.request(
                        method, target, json=json_body, headers=headers, timeout=45,
                    )
                    self.proxy_used += 1
                else:
                    resp = self.session.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=45,
                    )
                    self.direct_used += 1
            except requests.RequestException as exc:
                if use_proxy:
                    # A broken proxy must never break a run: go direct.
                    log.warning("Proxy request failed (%s) — using a direct connection", exc)
                    use_proxy = False
                    continue
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(min(2 ** attempt, 30))
                continue

            if use_proxy and resp.status_code >= 500:
                log.warning("Proxy returned HTTP %s — using a direct connection", resp.status_code)
                use_proxy = False
                continue

            if resp.status_code == 200:
                # Cheap call, no rate pressure: keep the soft pace budget full.
                self._pace_debt = max(0.0, self._pace_debt - 1.0)
            if resp.status_code == 429:
                self._rate_limited_this_run = True
                retry_after = _retry_after_seconds(resp.headers, attempt)
                raw_retry = _header_seconds(resp.headers)
                self._pace_debt += 3.0
                if raw_retry >= _LONG_BLOCK:
                    # Do not sleep, do not retry: a multi-hour block is final.
                    rotated = self._cookie_index + 1 < len(self._cookie_pool)
                    self._park_or_rotate(raw_retry)
                    if rotated:
                        self._rotate_cookie()
                        return self._request(
                            method, path, params=params, json_body=json_body,
                            _retry_auth=_retry_auth, allow_404=allow_404,
                        )
                if self.proxy_url and attempt == 0:
                    # First 429: try the other egress before waiting.
                    use_proxy = not use_proxy
                    log.warning(
                        "Spotify 429 — retrying through the %s route",
                        "proxy" if use_proxy else "direct connection",
                    )
                    continue
                if raw_retry and raw_retry > _MAX_INLINE_WAIT:
                    # 2–10 minutes: too long to sleep through, too short to give
                    # up on the account — try a spare cookie, else park the run.
                    if self._cookie_index + 1 < len(self._cookie_pool):
                        self._rotate_cookie()
                        return self._request(
                            method, path, params=params, json_body=json_body,
                            _retry_auth=_retry_auth, allow_404=allow_404,
                        )
                    self._park_or_rotate(raw_retry)
                if attempt >= 1:
                    # Persistent 429 with short Retry-After values (Spotify
                    # re-arms the window on every knock): stop at the second
                    # answer instead of waiting ~2 minutes to learn it again.
                    cooldown = _SHORT_BLOCK_COOLDOWN
                    self.blocked_until = time.time() + cooldown
                    raise RateLimited(
                        cooldown,
                        f"Spotify kept answering 429 (Retry-After keeps flapping back "
                        f"to a few seconds, ~{int(self._rate_limit_wait)}s waited) — "
                        f"stopping early for {_human_duration(cooldown)} instead of "
                        "knocking for two more minutes. Nothing was lost: this run "
                        "continues on the web-player route.",
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
                    method, path, params=params, json_body=json_body,
                    _retry_auth=False, allow_404=allow_404,
                )
            if resp.status_code == 401:
                raise AuthError("Spotify access token rejected. Refresh SPOTIFY_COOKIE.")
            if resp.status_code == 403:
                raise AuthError(
                    f"Spotify forbidden for {path}: {resp.text[:200]}. "
                    "The cookie may lack permission for this playlist."
                )
            if resp.status_code == 404:
                if allow_404:
                    return {}
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

    def playlist_snapshot(self, playlist_id: str) -> str:
        """One cheap call: has this playlist changed since we last read it?

        Returns ``f"{snapshot_id}:{total}"``. Used together with the source
        cache so repeat runs cost 1 request instead of N pages.
        """
        data = self._request(
            "GET",
            f"playlists/{playlist_id}",
            params={"fields": "snapshot_id,tracks(total)"},
        )
        total = (data.get("tracks") or {}).get("total", "")
        return f"{data.get('snapshot_id', '')}:{total}"

    def whoami(self) -> dict[str, str]:
        """Cheap identity probe used to detect per-account throttling."""
        data = self._request("GET", "me", params={})
        return {"id": data.get("id", ""), "country": data.get("country", "")}

    def playlist_track_ids(self, playlist_id: str) -> set[str]:
        """Track ids in a playlist, through the route this client prefers.

        A web-player-first client must not read the playlist through the Web API:
        that costs requests it does not need and can 429 in the middle of the
        read, which is exactly how a target playlist looks EMPTY (and then every
        "already there" check is wrong).
        """
        if getattr(self, "prefer_web_player", False):
            return {t.id for t in self._pathfinder_tracks(playlist_id)}
        try:
            return {t.id for t in self.playlist_tracks(playlist_id)}
        except RateLimited:
            log.warning(
                "Web API playlist read was throttled — using the web player route"
            )
            return {t.id for t in self._pathfinder_tracks(playlist_id)}

    def queue_track(self, uri: str, max_items: int = _MAX_QUEUE) -> bool:
        """Park a track in the player queue (1 cheap call, no playlist write).

        Spotify applies *server-side* de-duplication when you later save the
        queue into a playlist, so a long batch costs a few calls instead of one
        insert per track. Returns False when the queue is full.
        """
        if len(self._queued) >= max_items:
            return False
        self.writes_this_run += 1
        self._request(
            "POST", "me/player/queue", params={"uri": uri}, allow_404=True
        )
        self._queued.append(uri)
        return True

    @property
    def queued(self) -> int:
        return len(self._queued)

    def save_queue_to_playlist(self, playlist_id: str) -> int:
        """Save the whole queue into the target playlist. Returns items sent."""
        if not self._queued:
            return 0
        payload = {"uris": list(self._queued)}
        self._request("POST", f"playlists/{playlist_id}/tracks", json_body=payload)
        sent = len(self._queued)
        self._queued.clear()
        self.queue_saves += 1
        return sent

    def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        self.writes_this_run += 1
        self.writes_attempted += len(uris)
        if getattr(self, "prefer_pathfinder", False):
            try:
                self._pathfinder_add(playlist_id, uris)
                return
            except Exception as web_exc:
                if self.blocked_until > time.time():
                    raise
                # The web player route refused (a rotated hash, a new mutation
                # shape, a permission quirk) — the Web API is the other route and
                # it costs one call to find out whether the playlist itself is
                # writable. Two independent chances per run.
                log.warning(
                    "Web player write failed (%s) — trying the Web API", web_exc
                )
                try:
                    self._rest_add(playlist_id, uris)
                except Exception as rest_exc:
                    message = (
                        "Spotify refused the write on both routes — "
                        f"web-player: {str(web_exc)[:200]}; "
                        f"Web API: {str(rest_exc)[:200]}"
                    )
                    self.last_write_error = message
                    raise RuntimeError(message) from rest_exc
                self.last_write_error = ""
                return
        self.last_write_route = "rest"
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            try:
                self._request(
                    "POST",
                    f"playlists/{playlist_id}/tracks",
                    json_body={"uris": chunk},
                )
            except RateLimited as exc:
                reason_rest = f"Web API (429): {str(exc)[:160]}"
                self.last_write_error = reason_rest
                # The Web API is throttled; the web player route is not. Try it even
                # if the account has not used the web player yet this run — and if
                # that is refused too, the error names BOTH routes.
                self.prefer_pathfinder = True
                self.pathfinder_ok = True
                log.warning("Web API write throttled — writing through the web player")
                try:
                    self._pathfinder_add(playlist_id, chunk)
                except Exception as web_exc:
                    message = (
                        f"Spotify refused the write on both routes — "
                        f"{reason_rest}; web-player: {str(web_exc)[:200]}"
                    )
                    self.last_write_error = message
                    raise RuntimeError(message) from web_exc

    def _rest_add(self, playlist_id: str, uris: list[str]) -> None:
        """The Web API write, on its own (used as the other route)."""
        self.last_write_route = "rest"
        for i in range(0, len(uris), 100):
            self._request(
                "POST",
                f"playlists/{playlist_id}/tracks",
                json_body={"uris": uris[i : i + 100]},
            )

    def current_user_id(self) -> str:
        data = self._request("GET", "me")
        return data.get("id", "")
