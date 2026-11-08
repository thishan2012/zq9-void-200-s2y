"""YouTube Data API v3 client.

Search and metadata reads (``search.list`` / ``videos.list``) can run on a plain
**read-only API key** (``YOUTUBE_API_KEY``) sent as ``key=...``. Playlist writes
(``playlistItems.insert``) *require* OAuth — Google does not allow key-based
writes — so a refresh-token flow is used only for those calls.

Writes have a second, OAuth-free path: a **signed-in browser cookie** from
``music.youtube.com`` (``YT_COOKIE`` + ``YT_AUTHUSER``), used against the same
internal ``youtubei/v1/browse/edit_playlist`` endpoint the web player calls.
Nothing has to be registered in Google Cloud Console and no OAuth consent screen
is involved — so it also works where the Console is unreachable.

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

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import requests

from .errors import AuthError, NotFound, QuotaExceeded, RateLimited
from .models import Candidate

log = logging.getLogger(__name__)

_API = "https://www.googleapis.com/youtube/v3"
# A browser UA: YouTube's public results page answers bots with a consent/JS shell.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# videoId entries in the results page's JSON (watch?v= links also match).
def _fallback_clients() -> list[dict[str, str]]:
    """Client pairs that may have issued the refresh token (never hard-coded)."""
    try:
        from .oauth_setup import well_known_clients

        return well_known_clients()
    except Exception:  # never let an optional source break auth
        return []


def _elapsed(resp: Any) -> float:
    """How long the request behind ``resp`` took (0 when unknown)."""
    check = getattr(resp, "elapsed", None)
    total = getattr(check, "total_seconds", None)
    if callable(total):
        try:
            return max(0.0, float(total()))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


_FREE_ID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
_SCOPE = "https://www.googleapis.com/auth/youtube"
_YTM_API = "https://music.youtube.com/youtubei/v1/"
_YTM_KEY = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
_YTM_ORIGIN = "https://music.youtube.com"
# params for the web client's "songs" search filter (base64 protobuf). If YouTube
# ever changes it the call simply returns nothing and plain search is used.
_YTM_SEARCH_SONGS = "EgWKAQIIAWoMEA4QChADEAQQCRAF"

COST_SEARCH = 100
COST_VIDEOS = 1
COST_PLAYLIST_LIST = 1
COST_PLAYLIST_INSERT = 50

_ISO_DURATION = re.compile(
    r"^P(?:(?P<d>\d+)D)?T?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


def _candidate_from_watch_page(video_id: str, html: str) -> Candidate | None:
    """Pull title/channel/duration/description out of a watch page (free)."""
    marker = "ytInitialPlayerResponse"
    at = html.find(marker)
    if at == -1:
        return None
    brace = html.find("{", at)
    if brace == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[brace:])
    except ValueError:
        return None
    details = data.get("videoDetails") or {}
    if not details.get("title"):
        return None
    duration_s = 0
    try:
        duration_s = int(details.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        duration_s = 0
    return Candidate(
        video_id=details.get("videoId") or video_id,
        title=details.get("title", ""),
        channel=details.get("author", ""),
        description=(details.get("shortDescription") or "")[:5000],
        duration_s=duration_s,
        views=int(details.get("viewCount") or 0),
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


_PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_quota_date(now: datetime | None = None) -> str:
    """YouTube's daily quota resets at midnight Pacific, not UTC midnight."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_PACIFIC).strftime("%Y-%m-%d")


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
    # OAuth account index -> {"units", "inserts", "exhausted"}. Quota is per
    # Google Cloud project, so a second OAuth client from another project has
    # its own day. "exhausted" is sticky until the Pacific rollover, so a later
    # run does not waste calls re-discovering that account 1 is already empty.
    accounts: dict[str, dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.keys is None:
            self.keys = {}
        if self.accounts is None:
            self.accounts = {}

    def rollover(self) -> None:
        today = pacific_quota_date()
        if self.date != today:
            self.date = today
            self.units_used = 0
            self.searches = 0
            self.inserts = 0
            self.keys = {}
            self.accounts = {}

    def account(self, index: str) -> dict[str, Any]:
        return self.accounts.setdefault(
            str(index), {"units": 0, "inserts": 0, "exhausted": False}
        )

    def account_units(self, index: str) -> int:
        return int(self.account(index).get("units", 0))

    def spend_on_account(self, index: str, cost: int, insert: bool = False) -> None:
        entry = self.account(index)
        entry["units"] = int(entry.get("units", 0)) + cost
        if insert:
            entry["inserts"] = int(entry.get("inserts", 0)) + 1

    def exhaust_account(self, index: str) -> None:
        self.account(index)["exhausted"] = True

    def account_exhausted(self, index: str) -> bool:
        return bool(self.account(index).get("exhausted"))

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
            "accounts": {k: dict(v) for k, v in self.accounts.items()},
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
            accounts={
                str(k): dict(v)
                for k, v in (data.get("accounts") or {}).items()
                if isinstance(v, dict)
            },
        )


class YouTubeClient:
    def __init__(
        self,
        client_json: str = "",
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        token_json: str = "",
        api_key: str | list[str] = "",
        daily_quota_units: int = 9500,
        max_searches_per_run: int = 80,
        max_searches_per_day: int = 0,
        max_inserts_per_day: int = 0,
        quota_state: QuotaState | None = None,
        access_token: str = "",
        region_code: str = "US",
        session: requests.Session | None = None,
        free_search: bool = True,
        cookie: str = "",
        authuser: str = "0",
        free_search_pause: float = 1.0,
        web_session: requests.Session | None = None,
        extra_oauth: list[dict[str, str]] | None = None,
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
        # A refresh token is only usable with the client that issued it. If the
        # user has only the token (no YT_CLIENT_ID), the well-known clients are
        # tried as well — see `_refresh_access_token`.
        self._client_candidates: list[dict[str, str]] = []
        self._token_works = False
        self.client_used = ""
        self._explicit_pair: dict[str, str] = {}
        # Set when a client id exists but no secret: the exchange must fail
        # honestly instead of posting an empty secret.
        self.client_secret_missing = False
        self.token_uri = "https://oauth2.googleapis.com/token"
        self.refresh_token = refresh_token.strip()
        self._oauth_error = ""

        primary = self.oauth_bundle(
            client_json=client_json, client_id=client_id,
            client_secret=client_secret, refresh_token=refresh_token,
            token_json=token_json,
        )
        # Account 1 is what the user already had. Accounts 2..N are only ever
        # touched after Google says the previous project is out of quota.
        self._oauth_creds: list[dict[str, str]] = [primary]
        for extra in extra_oauth or []:
            if not isinstance(extra, dict):
                continue
            bundle = self.oauth_bundle(
                client_json=str(extra.get("client_json", "") or ""),
                client_id=str(extra.get("client_id", "") or ""),
                client_secret=str(extra.get("client_secret", "") or ""),
                refresh_token=str(extra.get("refresh_token", "") or ""),
                token_json=str(extra.get("token_json", "") or ""),
            )
            if bundle.get("refresh_token") or bundle.get("client_id"):
                self._oauth_creds.append(bundle)
        self.oauth_account_count = len(self._oauth_creds)
        # A spare that cannot possibly write must never be handed the run.
        # The secret can be a separate env value OR inside the account's JSON;
        # oauth_bundle() has already merged those shapes. An ID without its
        # secret is NOT ready. Keep the index for stable per-account counters.
        self._oauth_unusable: set[int] = {
            i for i, cred in enumerate(self._oauth_creds)
            if i and not self.oauth_bundle_complete(cred)
        }
        for i in sorted(self._oauth_unusable):
            log.warning(
                "YouTube OAuth account #%d is incomplete (needs its own client "
                "id AND secret next to the refresh token) — it will not be used",
                i + 1,
            )
        # Only a USABLE spare lifts the local cap: with none, the old
        # single-account safety behaviour must stay exactly as it was.
        self.oauth_usable_count = self.oauth_account_count - len(self._oauth_unusable)
        self.oauth_failover = self.oauth_usable_count > 1
        self.oauth_switches = 0
        self._oauth_index = 0
        self._apply_oauth_credential(0)

        if (
            not self.api_key
            and not (self.client_id and self.client_secret)
            and not self.refresh_token
            and not (cookie or "").strip()
        ):
            raise AuthError(
                "YouTube credentials are missing. Set YOUTUBE_API_KEY (read-only "
                "search) and/or YT_COOKIE (playlist writes, no Console) or "
                "YT_CLIENT_JSON + YT_REFRESH_TOKEN."
            )
        if (
            not self.refresh_token
            and not self.api_key
            and not access_token.strip()
            and not (cookie or "").strip()
        ):
            raise AuthError(
                "YouTube credentials are missing. Store YT_REFRESH_TOKEN (OAuth), "
                "YT_COOKIE (signed-in browser cookie, no Console needed) or "
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
        # Free tier: YouTube's public search page costs no API quota, so the
        # 100-unit API search is only spent when the free pass finds nothing.
        self.free_search = free_search
        self.free_search_pause = max(0.0, free_search_pause)
        self.free_searches_this_run = 0
        # The public web pages must not go through the API session (different
        # auth, different error handling, and tests inject a fake API session).
        self.web_session = web_session or requests.Session()
        # One keep-alive connection for every free page load: a fresh TLS
        # handshake per watch page was the biggest hidden cost of a run.
        try:
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
            self.web_session.mount("https://", adapter)
            self.web_session.mount("http://", adapter)
        except AttributeError:
            pass  # a test double, not a real session
        # Metadata already seen (per process): the 31 steps re-ask for the same
        # candidate set, and a repeated watch-page fetch costs half a second.
        self._video_memo: dict[str, Candidate] = {}
        self.free_pages_this_run = 0  # watch pages actually downloaded
        self.quota = quota_state or QuotaState()
        self.quota.rollover()
        self.region_code = region_code
        self.session = session or requests.Session()
        self._access_token = ""
        self._access_expiry = 0.0
        # A signed-in browser cookie: writes without any Cloud Console project.
        self.cookie = (cookie or "").strip()
        self.authuser = (authuser or "0").strip() or "0"
        # A hand-pasted access token (works ~1 hour, cannot be renewed).
        self.inserts_this_run = 0
        self._manual_access_token = access_token.strip()
        if self._manual_access_token:
            self._access_token = self._manual_access_token
            self._access_expiry = time.time() + 3300
        if self.oauth_failover:
            # An account Google already declared empty, or one that has reached
            # the agreed daily insert target, stays out until the Pacific
            # rollover: start on the first one that can still write.
            for idx in range(self.oauth_account_count):
                if idx in self._oauth_unusable:
                    continue
                if not self._oauth_account_done(idx):
                    if idx:
                        self._apply_oauth_credential(idx)
                    break

    # ------------------------------------------------- OAuth account failover
    @staticmethod
    def oauth_bundle_complete(cred: dict[str, str]) -> bool:
        """Structural readiness, not proof of validity: no network or values logged.

        A custom Google Cloud OAuth client needs all three parts. The secret
        may come from JSON instead of a separately named GitHub secret.
        """
        return all(str(cred.get(key, "") or "").strip()
                   for key in ("client_id", "client_secret", "refresh_token"))

    @staticmethod
    def oauth_bundle(
        *, client_json: str = "", client_id: str = "", client_secret: str = "",
        refresh_token: str = "", token_json: str = "",
    ) -> dict[str, str]:
        """Normalize one OAuth credential set (the parsing __init__ used to do).

        Kept separate so account 2 is parsed by exactly the same rules as
        account 1 — a client JSON, a bare id/secret pair, or a whole token.json.
        """
        blob: dict[str, Any] = {}
        raw_json = token_json or client_json
        if raw_json:
            try:
                blob = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                raise AuthError(f"YouTube OAuth JSON is not valid JSON: {exc}") from exc
            if not isinstance(blob, dict):
                raise AuthError(
                    "YouTube OAuth JSON must be one account object; for a list "
                    "of accounts use YOUTUBE_OAUTH_ACCOUNTS"
                )
        section = blob.get("installed") or blob.get("web") or blob
        if not isinstance(section, dict):
            raise AuthError("YouTube OAuth JSON: installed/web must be an object")
        cid = section.get("client_id", "") or (client_id or "").strip()
        secret = section.get("client_secret", "") or (client_secret or "").strip()
        explicit_id = (client_id or "").strip()
        return {
            "client_id": cid,
            "client_secret": secret,
            "token_uri": section.get("token_uri", "") or "https://oauth2.googleapis.com/token",
            "refresh_token": (refresh_token or "").strip()
            or blob.get("refresh_token", "")
            or section.get("refresh_token", ""),
            # A client JSON wins over YT_CLIENT_ID/SECRET, but a refresh token
            # issued by the *other* client must still be tried with that pair.
            "explicit_id": explicit_id if explicit_id and explicit_id != cid else "",
            "explicit_secret": (client_secret or "").strip(),
        }

    def _apply_oauth_credential(self, index: int) -> None:
        """Point the client at OAuth account `index` and drop its old token."""
        cred = self._oauth_creds[index]
        self._oauth_index = index
        self.client_id = cred["client_id"]
        self.client_secret = cred["client_secret"]
        self.token_uri = cred["token_uri"]
        self.refresh_token = cred["refresh_token"]
        self._explicit_pair = (
            {"client_id": cred["explicit_id"], "client_secret": cred["explicit_secret"]}
            if cred["explicit_id"]
            else {}
        )
        self._client_candidates = [
            pair for pair in _fallback_clients() if pair["client_id"] != self.client_id
        ]
        self._access_token = ""
        self._access_expiry = 0.0
        self._token_works = False
        self.client_used = ""
        self._oauth_error = ""
        self.client_secret_missing = False
        # A hand-pasted token belongs to the account it was minted for.
        self._manual_access_token = ""

    @property
    def oauth_account(self) -> int:
        """1-based number of the OAuth account currently in use."""
        return self._oauth_index + 1

    def _oauth_account_capped(self, index: int) -> bool:
        """True when this account has reached the local daily insert target."""
        if not self.max_inserts_per_day:
            return False
        return int(self.quota.account(str(index)).get("inserts", 0) or 0) >= self.max_inserts_per_day

    def _oauth_account_done(self, index: int) -> bool:
        """Google said empty, or the agreed daily insert target is reached."""
        return self.quota.account_exhausted(str(index)) or self._oauth_account_capped(index)

    def _next_oauth_account(self) -> int | None:
        """The next account that can still take over today."""
        for offset in range(1, self.oauth_account_count):
            idx = (self._oauth_index + offset) % self.oauth_account_count
            if idx in self._oauth_unusable:
                continue
            if not self._oauth_account_done(idx):
                return idx
        return None

    # ------------------------------------------------------- browser cookie
    @staticmethod
    def _cookie_value(raw: str, name: str) -> str:
        """Pull one cookie out of a pasted block, a ``Cookie:`` header or a value.

        Accepts every shape a browser hands out: a ``Cookie:`` line, the whole
        "Copy request headers" block, a quoted one-liner, tab/space after the
        colon, and any letter case for the cookie name. A secret pasted into
        GitHub usually keeps its line breaks, so newlines are treated as ``;``.
        """
        cooked = (raw or "").strip().replace("\r", "").replace("\n", ";")
        if cooked[:1] in "{[":
            # Cookie-editor extensions export [{"name": "...", "value": "..."}].
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = None
            if data is not None:
                stack = [data]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        if str(node.get("name", "")).lower() == name.lower():
                            value = str(node.get("value", "")).strip()
                            if value:
                                return value
                        stack.extend(node.values())
                    elif isinstance(node, list):
                        stack.extend(node)
        if "=" not in cooked:
            return ""

        def auth_key(key: str) -> str:
            """Cookie names travel through tools that eat underscores/dashes.

            Chrome's own Network panel, curl exports and JSON cookie exports all
            mangle `__Secure-3PAPISID` differently (`_Secure-3PAPISID`,
            `Secure3PAPISID`, `secure-3papisid`), and a name that lost a
            character silently disables every write. Compare on letters+digits.
            """
            return re.sub(r"[^a-z0-9]", "", key.lower())

        target = auth_key(name)
        for part in cooked.replace('"', "").split(";"):
            key, _, value = part.partition("=")
            key = key.strip()
            if key.lower().startswith("cookie:"):
                key = key[len("cookie:") :].strip()
            elif key.startswith(":") and key.lower() != ":cookie":
                continue  # devtools pseudo-header (:authority:, :method:, ...)
            if key and auth_key(key) == target and value.strip().strip('"'):
                return value.strip().strip('"')
        # Last resort (odd formatting / separators): unanchored, case-insensitive.
        # Anchored on a name boundary so "SID=" can never match the tail of
        # "__Secure-3PAPISID=" (a 7.9k pasted header used to look "full").
        token = r"[_\-. ]?".join(re.escape(ch) for ch in name.lower().lstrip("_"))
        match = re.search(
            r"(?<![a-z0-9_\-])" + token + r'\s*=\s*"?([^;\s"]+)', cooked, re.I
        )
        return match.group(1) if match else ""

    def _sapisid(self) -> str:
        return self._cookie_value(self.cookie, "__Secure-3PAPISID") or self._cookie_value(
            self.cookie, "SAPISID"
        )

    def cookie_diag(self) -> str:
        """One human line saying whether the cookie is usable (no secrets)."""
        if not self.cookie:
            return "no YT_COOKIE set"
        length = len(self.cookie)
        sapisid = self._sapisid()
        if not sapisid:
            hint = ""
            lowered = self.cookie.lower()
            if "sid" not in lowered and "papisid" not in lowered:
                hint = (
                    " — the value does not look like a cookie at all (paste the "
                    "Cookie request header, not the URL or the page text)"
                )
            elif "papisid" not in lowered:
                hint = (
                    " — the SAPISID family is missing entirely (copy every cookie "
                    "from a music.youtube.com request)"
                )
            else:
                hint = " — the value was there but empty or truncated"
            return f"cookie present ({length} chars) but no usable SAPISID{hint}"
        has_sid = bool(self._cookie_value(self.cookie, "SID") or self._cookie_value(self.cookie, "HSID"))
        kind = "full session cookie" if has_sid else (
            "SAPISID-only (narrow — if writes 401, paste the whole Cookie header)"
        )
        return (
            f"cookie ok ({length} chars, {kind}, __Secure-3PAPISID "
            f"{sapisid[:3]}…{sapisid[-3:]}, authuser {self.authuser})"
        )

    def _ytm_headers(self) -> dict[str, str]:
        """SAPISIDHASH headers, exactly what the YouTube Music web client sends."""
        sapisid = self._sapisid()
        if not sapisid:
            raise AuthError(
                "The YouTube cookie has no __Secure-3PAPISID — copy the whole "
                "Cookie request header from a signed-in music.youtube.com session."
            )
        stamp = str(int(time.time()))
        digest = hashlib.sha1(f"{stamp} {sapisid} {_YTM_ORIGIN}".encode()).hexdigest()
        return {
            "Authorization": f"SAPISIDHASH {stamp}_{digest}",
            "X-Goog-AuthUser": self.authuser,
            "X-Origin": _YTM_ORIGIN,
            "Origin": _YTM_ORIGIN,
            "Referer": _YTM_ORIGIN + "/",
            "Content-Type": "application/json",
            "Cookie": self.cookie,
            "User-Agent": _UA,
        }

    def _ytm_request(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "context": {
                "client": {
                    "clientName": "WEB_REMIX",
                    "clientVersion": "1."
                    + time.strftime("%Y%m%d", time.gmtime())
                    + ".01.00",
                    "hl": "en",
                    "gl": self.region_code or "US",
                },
                "user": {},
            }
        }
        payload.update(body)
        url = f"{_YTM_API}{endpoint}?alt=json&key={_YTM_KEY}&prettyPrint=false"
        try:
            resp = self.web_session.post(
                url, json=payload, headers=self._ytm_headers(), timeout=30
            )
        except requests.RequestException as exc:
            raise RateLimited(
                message=f"YouTube Music request failed ({type(exc).__name__}: {exc})"
            ) from exc
        if resp.status_code in (401, 403):
            raise AuthError(
                "YouTube rejected the cookie (HTTP "
                f"{resp.status_code}) — it was probably signed out or the cookie "
                "is incomplete. Copy a fresh Cookie header from "
                "music.youtube.com while signed in."
            )
        if resp.status_code == 429:
            raise RateLimited(message="YouTube Music rate-limited the request (429).")
        if resp.status_code >= 400:
            raise RateLimited(
                message=(
                    f"YouTube Music returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise RateLimited(
                message="YouTube Music returned non-JSON — cookie likely expired"
            ) from exc

    def cookie_playlist_entries(self, playlist_id: str) -> list[dict[str, str]]:
        """``[{video_id, set_video_id}]`` — set_video_id is what removal needs."""
        data = self._ytm_request("browse", {"browseId": "VL" + playlist_id})
        entries: list[dict[str, str]] = []
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                set_video_id = node.get("setVideoId")
                item = node.get("playlistItemData")
                if isinstance(item, dict) and item.get("videoId"):
                    entries.append(
                        {
                            "video_id": str(item["videoId"]),
                            "set_video_id": str(set_video_id or ""),
                        }
                    )
                for value in node.values():
                    stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
        return entries

    def cookie_remove_from_playlist(
        self, playlist_id: str, video_id: str, set_video_id: str
    ) -> bool:
        """Undo one add (used by the probe command). Returns True when confirmed."""
        data = self._ytm_request(
            "browse/edit_playlist",
            {
                "playlistId": playlist_id,
                "actions": [
                    {
                        "action": "ACTION_REMOVE_VIDEO",
                        "removedVideoId": video_id,
                        "setVideoId": set_video_id,
                    }
                ],
            },
        )
        return "SUCCEEDED" in str(data.get("status", ""))

    def cookie_playlist_video_ids(self, playlist_id: str) -> set[str]:
        """Private playlists, read with the same cookie (no API key needed)."""
        ids = {entry["video_id"] for entry in self.cookie_playlist_entries(playlist_id)}
        if ids:
            return ids
        # Layout fallback: any videoId anywhere in the response.
        data = self._ytm_request("browse", {"browseId": "VL" + playlist_id})
        return set(self._collect_video_ids(data))

    def cookie_add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        """``browse/edit_playlist`` + ``ACTION_ADD_VIDEO`` — the web player's own call."""
        self.quota.rollover()
        if self.max_inserts_per_day and self.quota.inserts >= self.max_inserts_per_day:
            raise QuotaExceeded(
                f"Daily YouTube insert budget reached ({self.max_inserts_per_day}); "
                "it resets at midnight Pacific."
            )
        data = self._ytm_request(
            "browse/edit_playlist",
            {
                "playlistId": playlist_id,
                "actions": [
                    {"action": "ACTION_ADD_VIDEO", "addedVideoId": video_id}
                ],
            },
        )
        status = str(data.get("status", ""))
        if "SUCCEEDED" not in status:
            # youtubei answers 200 with a failed status instead of an HTTP error,
            # so decide here — and never let a half-done add be recorded as done.
            detail = ""
            for result in data.get("playlistEditResults", []) or []:
                note = result.get("playlistEditVideoAddedResultData")
                if isinstance(note, dict) and note.get("setVideoId"):
                    detail = " (but a setVideoId came back — check the playlist)"
            raise RateLimited(
                "YouTube Music did not confirm the add (status "
                f"{status or 'missing'}){detail} — nothing was inserted"
            )
        # Counted in inserts, not in Data-API units: youtubei costs no quota.
        self.quota.inserts += 1
        self.inserts_this_run += 1
        log.info(
            "YouTube: added %s to %s (browser cookie, no quota)", video_id, playlist_id
        )

    # ------------------------------------------------------------------ auth

    @property
    def has_oauth(self) -> bool:
        """Refresh-token OAuth is configured (renewable, uses the public Data API).

        The token alone counts: it is exchanged with the client that issued it,
        and `_token_works` remembers once that has actually succeeded.
        """
        return bool(
            self.refresh_token
            and (self.client_id or self._client_candidates or self._token_works)
        )

    @property
    def can_write(self) -> bool:
        """Playlist inserts need either refresh-token OAuth or a signed-in cookie."""
        if self.has_oauth:
            return True
        return bool(self.cookie and self._sapisid())

    @property
    def write_mode(self) -> str:
        """How writes are authorised, for the log: oauth / manual-token / none."""
        if self.has_oauth:
            return "oauth (refresh token)"
        if self.cookie and self._sapisid():
            return "browser cookie (no OAuth, no Console, no VPN for the sync)"
        if self._manual_access_token:
            return "manual access token (expires in ~1h, not renewable)"
        return "none"

    @property
    def search_mode(self) -> str:
        if not self.api_keys:
            if self.cookie and self._sapisid():
                return "browser cookie (no API key, no quota)"
            return "oauth"
        return "api_key" if len(self.api_keys) == 1 else f"api_key x{len(self.api_keys)}"

    def _ensure_token(self) -> str:
        if not self.can_write and not self._manual_access_token:
            raise AuthError(
                "YouTube playlist writes need OAuth or a signed-in cookie. Add "
                "YT_COOKIE (no Console needed) or YT_REFRESH_TOKEN."
            )
        if self._manual_access_token and not self.can_write:
            if time.time() >= self._access_expiry:
                raise AuthError(
                    "The pasted YouTube access token expired (~1 hour). A refresh token "
                    "is the only renewable option: run `phonksync youtube-auth` once "
                    "(it prints a YT_REFRESH_TOKEN that never expires)."
                )
            return self._manual_access_token
        if self._access_token and time.time() < self._access_expiry - 60:
            return self._access_token
        if self.client_id and not self.client_secret and self.refresh_token:
            self.client_secret_missing = True
            raise AuthError(
                f"YouTube OAuth client {self.client_id[:22]}… has no client secret. "
                "A refresh token needs YT_CLIENT_SECRET + YT_CLIENT_ID from the SAME "
                "OAuth client (`phonksync youtube-auth --doctor`). A partially filled "
                "pair cannot be redeemed, so the run stops here instead of failing "
                "one insert at a time."
            )
        attempts = []
        if self.client_id:
            attempts.append({"client_id": self.client_id, "client_secret": self.client_secret})
        if self._explicit_pair:
            attempts.append(dict(self._explicit_pair))
        attempts.extend(self._client_candidates)
        if not attempts:
            raise AuthError(
                "YouTube writes need YT_CLIENT_ID + YT_CLIENT_SECRET next to "
                "YT_REFRESH_TOKEN (the token belongs to that OAuth client)."
            )
        last = ""
        for pair in attempts:
            body = {
                "client_id": pair["client_id"],
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }
            if pair.get("client_secret"):
                body["client_secret"] = pair["client_secret"]
            try:
                resp = self.session.post(self.token_uri, data=body, timeout=30)
            except requests.RequestException as exc:
                raise AuthError(f"YouTube token refresh failed: {exc}") from exc
            if resp.status_code != 200:
                last = f"{resp.status_code}: {resp.text[:200]}"
                log.debug("No YouTube token from client %s (%s)", pair["client_id"][:14], last)
                continue
            data = resp.json()
            self._access_token = data["access_token"]
            self._access_expiry = time.time() + int(data.get("expires_in", 3600))
            self._token_works = True
            self.client_used = pair["client_id"]
            if not self.client_id:
                # Remember where it worked: one exchange per run, not per call.
                self.client_id = pair["client_id"]
                self.client_secret = pair.get("client_secret", "")
            return self._access_token
        raise AuthError(
            "YouTube token refresh failed for every known OAuth client "
            f"({last}). The refresh token was probably issued to a different "
            "client: set YT_CLIENT_ID + YT_CLIENT_SECRET to match it, or make a "
            "new token with `phonksync youtube-auth`."
        )

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
        # With several usable OAuth accounts, the local insert target is applied
        # PER ACCOUNT (195/day by default), not as one shared total. Reads and
        # searches are not insert-capped. A real Google quotaExceeded still
        # hands over immediately, even before the local target is reached.
        if oauth and self.oauth_failover:
            if insert and self._oauth_account_capped(self._oauth_index):
                left = self._oauth_index
                nxt = self._next_oauth_account()
                if nxt is None:
                    raise QuotaExceeded(
                        f"All {self.oauth_usable_count} usable YouTube OAuth accounts "
                        f"reached the daily insert target ({self.max_inserts_per_day} "
                        "playlist adds per account today)"
                    )
                self._apply_oauth_credential(nxt)
                self.oauth_switches += 1
                log.warning(
                    "YouTube OAuth account #%d reached its daily insert target "
                    "(%d) — continuing on account #%d",
                    left + 1, self.max_inserts_per_day, nxt + 1,
                )
            return
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
                "Pacific rollover."
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
                    # OAuth: this Google Cloud project is out of quota. If a
                    # spare OAuth account is configured, continue on it instead
                    # of ending the run — and retry THIS request, so no song is
                    # lost at the handover.
                    if oauth and self.oauth_failover:
                        self.quota.exhaust_account(str(self._oauth_index))
                        left = self._oauth_index
                        nxt = self._next_oauth_account()
                        if nxt is not None:
                            self._apply_oauth_credential(nxt)
                            self.oauth_switches += 1
                            log.warning(
                                "YouTube OAuth account #%d hit its project quota — "
                                "continuing on account #%d",
                                left + 1, nxt + 1,
                            )
                            return self._request(
                                method, path, params=params, json_body=json_body,
                                cost=cost, search=search, insert=insert, oauth=oauth,
                            )
                        raise QuotaExceeded(
                            f"All {self.oauth_account_count} YouTube OAuth accounts "
                            f"are out of quota for today ({reason})"
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
            if oauth and self.oauth_failover:
                self.quota.spend_on_account(str(self._oauth_index), cost, insert=insert)
            if search:
                self.searches_this_run += 1
                self.quota.searches += 1
            if insert:
                self.quota.inserts += 1
                self.inserts_this_run += 1
            return resp.json() if resp.text else {}
        raise RuntimeError("YouTube request retry loop exhausted")

    # ------------------------------------------------------------- endpoints

    def free_search_ids(self, query: str, max_results: int = 10) -> list[str]:
        """Search YouTube's public results page: no API key, no quota.

        Used before every API search so the 100-unit queries are spent only on
        tracks the free pass could not resolve. Returns [] on any failure (the
        caller then just uses the API).
        """
        if not self.free_search:
            return []
        try:
            resp = self.web_session.get(
                "https://www.youtube.com/results",
                params={"search_query": query, "hl": "en", "gl": self.region_code},
                headers={
                    "User-Agent": _UA,
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=20,
            )
            if resp.status_code != 200:
                log.debug("Free search HTTP %s for %r", resp.status_code, query)
                return []
            ids = _FREE_ID_RE.findall(resp.text)
        except requests.RequestException as exc:
            log.debug("Free search failed for %r: %s", query, exc)
            return []
        seen: list[str] = []
        for video_id in ids:
            if video_id not in seen:
                seen.append(video_id)
        self.free_searches_this_run += 1
        self._pause_after(resp, self.free_search_pause)
        return seen[:max_results]

    def _pause_after(self, resp: Any, wanted: float) -> None:
        """Wait *wanted* seconds in total, minus the time the request took.

        A page that already needed 400ms does not need another full second on
        top: the gap exists to avoid hammering YouTube, not to pad the run.
        """
        elapsed = _elapsed(resp)
        gap = max(0.0, wanted - elapsed)
        if gap > 0:
            time.sleep(gap)

    def search(self, query: str, max_results: int = 10) -> list[str]:
        if not self.api_keys and self.cookie and self._sapisid():
            # Cookie mode: no Google Cloud project at all, so no Data API calls.
            free = self.free_search_ids(query, max_results=max_results)
            if free:
                return free
            return self.ytm_search_ids(query, max_results=max_results)
        if self.free_search:
            free = self.free_search_ids(query, max_results=max_results)
            if free:
                return free
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

    def ytm_search_ids(self, query: str, max_results: int = 10) -> list[str]:
        """Search through the web client's endpoint (cookie mode, no key/quota)."""
        if not self.cookie or not self._sapisid():
            return []
        ids: list[str] = []
        for body in (
            {"query": query, "params": _YTM_SEARCH_SONGS},
            {"query": query},  # if the filter constant ever rots, still search
        ):
            try:
                data = self._ytm_request("search", body)
                found = self._collect_video_ids(data)
            except (AuthError, RateLimited) as exc:
                log.debug("YouTube Music search failed for %r: %s", query, exc)
                break
            if found:
                ids = found
                break
        self.free_searches_this_run += 1
        return ids[:max_results]

    @staticmethod
    def _collect_video_ids(data: Any) -> list[str]:
        """Every videoId in a youtubei response, in document order."""
        ids: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                video_id = node.get("videoId")
                if isinstance(video_id, str) and video_id not in ids:
                    ids.append(video_id)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return ids

    def _ytm_detail(self, video_id: str) -> Candidate | None:
        try:
            data = self._ytm_request("player", {"videoId": video_id})
        except (AuthError, RateLimited) as exc:
            log.debug("YouTube Music details failed for %s: %s", video_id, exc)
            return None
        details = data.get("videoDetails") or {}
        if not details:
            return None
        title = str(details.get("title") or "")
        channel = str(details.get("author") or "")
        if not title:
            return None
        return Candidate(
            video_id=video_id,
            title=title,
            channel=channel,
            description=str(details.get("shortDescription") or ""),
            duration_s=int(details.get("lengthSeconds") or 0),
        )

    def ytm_videos(self, video_ids: list[str]) -> list[Candidate]:
        """Metadata from the web client's ``player`` endpoint (cookie mode)."""
        if not self.cookie or not self._sapisid():
            return []
        out: list[Candidate] = []
        for video_id in video_ids:
            candidate = self._ytm_detail(video_id)
            if candidate:
                out.append(candidate)
        return out

    def free_videos(self, video_ids: list[str]) -> list[Candidate]:
        """Metadata for free: each watch page's embedded player response.

        No API key and no quota. Enough for the matcher (title, channel,
        duration, description), so a result set from the free search never
        costs a single unit unless the real API key search is needed.
        """
        out: list[Candidate] = []
        memo = getattr(self, "_video_memo", None)
        if memo is None:
            memo = self._video_memo = {}
        for video_id in video_ids:
            if video_id in memo:
                out.append(memo[video_id])
                continue
            self.free_pages_this_run += 1
            try:
                resp = self.web_session.get(
                    "https://www.youtube.com/watch",
                    params={"v": video_id, "hl": "en"},
                    headers={
                        "User-Agent": _UA,
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                    timeout=20,
                )
                if resp.status_code != 200:
                    continue
                candidate = _candidate_from_watch_page(video_id, resp.text)
            except requests.RequestException as exc:
                log.debug("Free video fetch failed for %s: %s", video_id, exc)
                continue
            if candidate:
                memo[video_id] = candidate
                out.append(candidate)
            if self.free_search_pause:
                # A page that had to be fetched anyway; 0.25s is enough of a
                # politeness gap and keeps ~10 candidates under 3s.
                time.sleep(min(self.free_search_pause, 0.25))
        return out

    def videos(self, video_ids: list[str]) -> list[Candidate]:
        out: list[Candidate] = []
        if self.free_search and video_ids and not self.api_keys:
            # Public watch pages first (no cookie needed), the web endpoint for
            # whatever is left (cookie mode) — never a quota-billed API call.
            out = self.free_videos(video_ids)
            done = {candidate.video_id for candidate in out}
            rest = [v for v in video_ids if v not in done]
            return out if not rest else out + self.ytm_videos(rest)
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
        if not self.has_oauth and self.cookie and self._sapisid():
            return self.cookie_playlist_video_ids(playlist_id)
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
        if not self.has_oauth and self.cookie and self._sapisid():
            # Cookie mode: the public Data API cannot write on a cookie, so use
            # the web player's own youtubei endpoint instead.
            return self.cookie_add_to_playlist(playlist_id, video_id)
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
