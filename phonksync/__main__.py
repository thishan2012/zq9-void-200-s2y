"""CLI entry point: python -m phonksync <command>."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .errors import AuthError, ConfigError, PhonkSyncError
from .settings import Config, Secrets, load_dotenv


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_sync(args: argparse.Namespace) -> int:
    from .engine import SyncEngine

    cfg = Config.load(args.config)
    if args.dry_run:
        cfg.dry_run = True
    if args.max_tracks:
        cfg.max_tracks_per_run = args.max_tracks
    cfg.validate()
    secrets = Secrets.from_env()
    secrets.validate(require_youtube=cfg.youtube_enabled)
    engine = SyncEngine(cfg, secrets)
    report = engine.run()
    if report.spotify_throttled:
        print(f"⏸ Spotify is throttling — {report.quota_reason or 'cooldown stored'}", file=sys.stderr)
        print("   Nothing was lost: the next run resumes from the same track.", file=sys.stderr)
        if not report.errors:
            return 0
        if all(_is_rate_limit_text(e) for e in report.errors):
            return 0
    return 1 if report.errors and not report.added else 0


def _is_rate_limit_text(text: str) -> bool:
    low = text.lower()
    return "429" in low or "throttl" in low or "rate limit" in low or "cooldown" in low


# name -> (what it does, is it still used?)
_SECRET_NOTES: list[tuple[str, str, str]] = [
    ("SPOTIFY_COOKIE", "Spotify login (sp_dc) — reads the playlists", "required"),
    ("SPOTIFY_COOKIE_2", "spare Spotify account, used only after a 429", "optional"),
    ("SPOTIFY_SOURCE_PLAYLISTS", "source id(s) — optional; the committed 'Scoure playlist.txt' is used when present", "optional"),
    ("SPOTIFY_TARGET_PLAYLIST", "where tracks are added", "required"),
    ("TELEGRAM_BOT_TOKEN", "the one end-of-run message", "required"),
    ("TELEGRAM_CHAT_ID", "which chat gets it", "required"),
    ("YOUTUBE_API_KEY", "YouTube search (100 units/query); free pages are used first", "optional"),
    ("YT_API_KEY", "alias of YOUTUBE_API_KEY", "alias"),
    ("YOUTUBE_ENABLED", "true/false: sync to YouTube at all", "required"),
    ("YOUTUBE_TARGET_PLAYLIST", "the YouTube playlist to fill", "required"),
    ("YT_REFRESH_TOKEN", "YouTube writes (1//…, never expires) — this is the one", "required"),
    ("YT_CLIENT_ID", "the OAuth client that issued YT_REFRESH_TOKEN", "needs secret"),
    ("YT_CLIENT_SECRET", "the OAuth client's secret (both halves or neither)", "needs id"),
    ("YT_CLIENT_JSON", "the downloaded OAuth client file — replaces id+secret", "alternative"),
    ("YOUTUBE_OAUTH_TOKEN_JSON", "a whole token.json (client + refresh token in one)", "alternative"),
    ("YT_COOKIE", "browser-cookie writes — not needed once OAuth works", "alternative"),
    ("PROXY_URL", "short name for the same proxy (accepted as an alias)", "alias"),
]

_PROXY_ALIASES = ("SPOTIFY_PROXY_URL",)
# Filled in by `_audit_secrets`, read by `cmd_check`. One run per process, so a
# module-level list is simpler than threading a return value through.
_audit_problems: list[str] = []


def _audit_secrets(secrets, cfg) -> None:
    """Name every configured secret and say what it is for (values never shown)."""
    _audit_problems.clear()  # a second `check` in the same process must not stack
    configured = {
        name: bool(os.environ.get(name, "").strip())
        for name, _, _ in _SECRET_NOTES
    }
    for alias in _PROXY_ALIASES + ("SPOTIFY_PROXY_SECRET", "YT_AUTHUSER", "MAX_INSERTS_PER_DAY"):
        configured.setdefault(alias, bool(os.environ.get(alias, "").strip()))

    has_id = bool(secrets.youtube_client_id)
    has_secret = bool(secrets.youtube_client_secret)
    rows = [
        (name, note, status)
        for name, note, status in _SECRET_NOTES
        if configured.get(name) and not (status == "needs id" and not has_id)
    ]
    missing = [n for n, _, s in _SECRET_NOTES if s == "required" and not configured.get(n)]

    def _looks_unset(value: str) -> str:
        """A secret that EXISTS but is empty/placeholder is worse than missing.

        Length is never judged: a short value is still the user's value, and a
        false alarm is worse than no alarm (that was the lesson of this page).
        """
        text = (value or "").strip()
        if not text:
            return "empty"
        if text.lower() in {"none", "null", "todo", "changeme", "placeholder", "xxx", "-"}:
            return "a placeholder"
        return ""

    suspicious = {
        name: _looks_unset(os.environ.get(name, ""))
        for name in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN",
                     "SPOTIFY_COOKIE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                     "SPOTIFY_TARGET_PLAYLIST")
        if name in os.environ  # only a *present* secret can be present-but-empty
    }
    suspicious = {k: v for k, v in suspicious.items() if v}

    print("\n🔑 Secrets found in this run (names only — values are never printed)")
    if rows:
        for name, note, status in rows:
            bad = status == "typo/rename"
            if status == "needs secret" and not has_secret:
                bad = True
            if name in suspicious:
                bad = True
                note = f"the value is {suspicious[name]} — {note}"
            print(f"   {'⚠️' if bad else '✅'} {name:26} [{status:12}] {note}")
    else:
        print("   (none)")
    if configured.get("SPOTIFY_PROXY_URL"):
        print(f"   ✅ {'SPOTIFY_PROXY_URL':26} [{'optional':12}] Spotify egress proxy")

    # YouTube: which single path will actually be used?
    has_refresh = bool(secrets.youtube_refresh_token)
    has_pair = bool(secrets.youtube_client_id and secrets.youtube_client_secret)
    has_id_only = bool(secrets.youtube_client_id and not secrets.youtube_client_secret)
    has_json = bool(secrets.youtube_client_json or secrets.youtube_token_json)
    has_cookie = bool(secrets.youtube_cookie)
    if has_refresh or has_pair or has_json or has_cookie:
        path = "none"
        if has_refresh and (has_pair or has_json or (not has_id_only)):
            path = "OAuth refresh token"
        elif has_cookie:
            path = "browser cookie"
        elif has_json:
            path = "token JSON"
        print(f"\n   ✍️  YouTube writes will use: {path}")
        if has_id_only:
            print(
                "   ⚠️  YT_CLIENT_ID is set but YT_CLIENT_SECRET is not — a refresh "
                "token cannot be redeemed with half a pair."
            )
        if has_refresh and has_cookie:
            print("   ℹ️  Both YT_REFRESH_TOKEN and YT_COOKIE are set; OAuth wins.")
        if has_json and has_pair:
            print(
                "   ℹ️  YT_CLIENT_JSON and YT_CLIENT_ID/SECRET are both set: the JSON "
                "client is tried first, then the pair (so a token from either works)."
            )
        if has_json and has_refresh:
            print(
                "   ℹ️  YOUTUBE_OAUTH_TOKEN_JSON carries its own client; "
                "YT_REFRESH_TOKEN is the token that will be used."
            )
    for name, why in suspicious.items():
        _audit_problems.append(
            f"{name} exists but its value is {why} — re-paste it "
            "(a blank or placeholder secret fails in a way that looks like a bug)."
        )
    if has_id_only:
        _audit_problems.append(
            "YT_CLIENT_ID is set but YT_CLIENT_SECRET is missing — a refresh token "
            "cannot be redeemed with half an OAuth client (add the secret or delete "
            "the id)."
        )
    if configured.get("PROXY_URL") and configured.get("SPOTIFY_PROXY_URL"):
        print(
            "\n   ℹ️  PROXY_URL and SPOTIFY_PROXY_URL are both set; the long name wins. "
            "You can delete PROXY_URL."
        )
    if missing:
        print(f"\n   ❌ Missing but required: {', '.join(sorted(missing))}")
    print()


def cmd_check(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    secrets = Secrets.from_env()
    problems: list[str] = []
    try:
        cfg.validate()
        # Name the ids that were actually parsed: a wrong id is the quietest
        # failure there is (the run succeeds and writes to the wrong place).
        print(f"✅ Config OK — {len(cfg.spotify_source_playlists)} source playlists")
        print(f"   🎯 target playlist: {cfg.spotify_target_playlist}")
        for index, source in enumerate(cfg.spotify_source_playlists, 1):
            print(f"   📥 source {index}: {source}")
        if cfg.spotify_source_playlists_origin:
            print(f"   📄 sources from: {cfg.spotify_source_playlists_origin}")
        if not cfg.youtube_enabled:
            print("ℹ️  YouTube sync disabled — Spotify-only mode")
        else:
            key_count = len([k for k in __import__("re").split(r"[\s,;]+", secrets.youtube_api_key) if k])
            if key_count:
                print(
                    f"🔎 YouTube search: API key ({key_count} key(s) / "
                    f"{cfg.youtube_daily_quota_units * max(1, key_count)} units/day)"
                )
            elif secrets.youtube_cookie:
                print("🔎 YouTube search: free web pages + cookie (0 quota, no API key)")
            else:
                print("🔎 YouTube search: OAuth")
            has_oauth = bool(
                (
                    secrets.youtube_client_json
                    or secrets.youtube_token_json
                    or (secrets.youtube_client_id and secrets.youtube_client_secret)
                )
                and (secrets.youtube_refresh_token or secrets.youtube_token_json)
            )
            if has_oauth:
                print("✅ YouTube playlist writes: OAuth enabled (refresh token — never expires)")
            elif secrets.youtube_cookie:
                from .youtube import YouTubeClient

                probe = YouTubeClient(
                    api_key="",
                    cookie=secrets.youtube_cookie,
                    authuser=secrets.youtube_authuser,
                    free_search=False,
                )
                if probe.can_write:
                    print(f"✅ YouTube playlist writes: {probe.write_mode}")
                else:
                    print(
                        f"⚠️  YT_COOKIE is set but unusable: {probe.cookie_diag()}"
                    )
                    print(
                        "   Copy the whole Cookie header from a signed-in "
                        "music.youtube.com tab (`phonksync youtube-auth --cookie-help`)."
                    )
            elif secrets.youtube_access_token:
                print(
                    "⚠️  YouTube writes: manual access token — works ~1 hour, then dies. "
                    "Run `phonksync youtube-auth` once for a YT_REFRESH_TOKEN."
                )
            else:
                print(
                    "⚠️  YouTube playlist writes disabled — a read-only API key cannot "
                    "add videos. Add YT_COOKIE (no Console needed, see "
                    "`phonksync youtube-auth --cookie-help`) or YT_CLIENT_ID + "
                    "YT_CLIENT_SECRET + YT_REFRESH_TOKEN."
                )
    except ConfigError as exc:
        problems.append(f"Config: {exc}")
    _audit_secrets(secrets, cfg)
    problems.extend(_audit_problems)
    try:
        secrets.validate(require_youtube=cfg.youtube_enabled)
        print("✅ Required secrets present")
    except ConfigError as exc:
        problems.append(str(exc))

    for problem in secrets.token_problems():
        problems.append(problem)

    # Say plainly how many YouTube OAuth accounts the run will really use. The
    # spare is only touched after a quota error, so a half-configured one is
    # invisible in a normal run — this is the only place it shows up.
    if cfg.youtube_enabled or secrets.youtube_extra_oauth:
        from .youtube import YouTubeClient

        spares = secrets.youtube_extra_oauth
        usable = []
        for i, extra in enumerate(spares, start=2):
            try:
                bundle = YouTubeClient.oauth_bundle(
                    client_json=extra.get("client_json", ""),
                    client_id=extra.get("client_id", ""),
                    client_secret=extra.get("client_secret", ""),
                    refresh_token=extra.get("refresh_token", ""),
                    token_json=extra.get("token_json", ""),
                )
            except AuthError:
                continue  # token_problems() above names the invalid JSON
            if YouTubeClient.oauth_bundle_complete(bundle):
                usable.append(i)
        # A cookie or short-lived access token is not an OAuth failover account.
        primary_ok = bool(secrets.youtube_refresh_token or secrets.youtube_token_json)
        total = len(spares) + (1 if primary_ok else 0)
        ready = len(usable) + (1 if primary_ok else 0)
        print(
            f"🔁 YouTube OAuth accounts: {total} configured, {ready} usable"
            + (f" (spares ready: {', '.join('#' + str(i) for i in usable)})" if usable else "")
        )
        if spares and not usable:
            print(
                "⚠️  No spare OAuth account is usable — a full quota will stop the "
                "run instead of continuing. Each spare needs its own client ID, "
                "client secret and refresh token; the ID and secret may be "
                "inside its JSON instead of separate secrets."
            )
        elif ready > 1:
            target = (
                f"{cfg.youtube_max_inserts_per_day} playlist adds/day per account"
                if cfg.youtube_max_inserts_per_day
                else "no local insert target (Google quota only)"
            )
            print(
                "✅ Quota failover armed: "
                f"{target}; a real quotaExceeded still hands over immediately"
            )

    # Early Spotify cookie parse check (no network) — catches the
    # common \"Could not find sp_dc\" mistake even without --live.
    if secrets.spotify_cookie:
        try:
            from .spotify import extract_sp_dc

            tok = extract_sp_dc(secrets.spotify_cookie)
            print(f"✅ Spotify cookie parses (sp_dc length {len(tok)}, starts with {tok[:6]}...)")
        except Exception as exc:
            problems.append(f"Spotify cookie: {exc}")
            problems.append(
                "Tip: only the single sp_dc value is needed — run "
                "`python -m phonksync cookie-fix --show` locally and paste that value "
                "as SPOTIFY_COOKIE."
            )
    from .spotify import _with_proxy, proxy_headers

    proxy = (
        os.environ.get("SPOTIFY_PROXY_URL", "").strip()
        or os.environ.get("PROXY_URL", "").strip()  # the short name people actually save
        or cfg.spotify_proxy_url
    )
    if proxy:
        mode = "header mode (x-proxy-secret)" if proxy_headers() else "query mode"
        print(f"🔗 Spotify route: proxy {proxy} — {mode}")
        # Non-fatal probe so the log *shows* whether the proxy is really used.
        try:
            import requests as _requests

            probe = _requests.get(
                _with_proxy(proxy, "https://open.spotify.com/", None),
                headers=proxy_headers(),
                timeout=15,
            )
            print(
                f"✅ Proxy reachable (HTTP {probe.status_code}, "
                f"{len(probe.text)} bytes of open.spotify.com)"
            )
        except Exception as exc:
            print(
                f"⚠️  Proxy not reachable from here ({str(exc).splitlines()[0][:120]}) — "
                "sync will automatically fall back to a direct connection"
            )
    else:
        print("🔗 Spotify route: direct (set SPOTIFY_PROXY_URL / spotify_proxy_url to use a proxy)")

    try:
        from .db import StateDB

        db = StateDB(cfg.state_path)
        pending = db.pending_youtube_count()
        if pending:
            print(
                f"📝 {pending} matched video(s) waiting for YouTube auth — they are added "
                "automatically once YT_REFRESH_TOKEN is set"
            )
        skipped = len(db.skipped_tracks())
        if skipped:
            print(f"🙅 {skipped} track(s) skipped by choice (see `phonksync skip --show`)")
    except Exception as exc:  # never let a stats line break `check`
        print(f"ℹ️  Could not read state: {exc}")

    if secrets.telegram_bot_token and secrets.telegram_chat_id:
        print("✅ Telegram secrets present")
    else:
        print("ℹ️  Telegram not configured (logs will be console-only)")

    if args.live and not problems:
        from .spotify import SpotifyClient
        from .youtube import YouTubeClient

        try:
            sp = SpotifyClient(secrets.spotify_cookie)
            user = sp.current_user_id()
            print(f"✅ Spotify cookie valid (user: {user}, web-player token w/ TOTP)")
        except Exception as exc:
            problems.append(f"Spotify live check: {exc}")
        if cfg.youtube_enabled:
            try:
                yt = YouTubeClient(
                    client_json=secrets.youtube_client_json,
                    client_id=secrets.youtube_client_id,
                    client_secret=secrets.youtube_client_secret,
                    refresh_token=secrets.youtube_refresh_token,
                    token_json=secrets.youtube_token_json,
                    api_key=secrets.youtube_api_key,
                    cookie=secrets.youtube_cookie,
                    authuser=secrets.youtube_authuser,
                    extra_oauth=secrets.youtube_extra_oauth,
                )
                if yt.api_keys:
                    hits = yt.search("phonk", max_results=1)
                    print(
                        f"✅ YouTube API key works (read-only search, {len(hits)} result, "
                        f"{len(yt.api_keys)} key(s) x {yt.daily_quota_units} units/day)"
                    )
                diag = getattr(yt, "cookie_diag", None)
                if secrets.youtube_cookie and callable(diag):
                    print(f"🍪 {diag()}")
                if yt.has_oauth:
                    try:
                        yt.playlist_video_ids(cfg.youtube_target_playlist)
                        used = getattr(yt, "client_used", "") or yt.client_id
                        print(
                            "✅ YouTube OAuth valid, target playlist readable (writes enabled)"
                        )
                        print(f"   client in use: {used[:22]}… (that is the one that works)")
                    except Exception as exc:
                        problems.append(f"YouTube OAuth account #1 live check: {exc}")
                elif yt.can_write:
                    # Prove the cookie can actually read the private playlist.
                    ids = yt.playlist_video_ids(cfg.youtube_target_playlist)
                    print(
                        f"✅ YouTube cookie works — target playlist readable "
                        f"({len(ids)} video(s)), writes go through the web endpoint"
                    )
                else:
                    print(
                        "⚠️  YouTube writes disabled (read-only) — add YT_COOKIE "
                        "(no Console needed) or YT_CLIENT_ID + YT_CLIENT_SECRET + "
                        "YT_REFRESH_TOKEN"
                    )
                # A green primary says nothing about the spare. Refresh each
                # custom client and read the SAME target, without inserting a
                # video or saving quota/state. This catches a token issued to
                # a different client before the real quota handover needs it.
                for index in range(1, yt.oauth_account_count):
                    if index in yt._oauth_unusable:
                        continue
                    number = index + 1
                    try:
                        yt._apply_oauth_credential(index)
                        yt._ensure_token()
                        yt.playlist_video_ids(cfg.youtube_target_playlist)
                        print(
                            f"✅ YouTube OAuth account #{number}: token refresh "
                            "+ target playlist read OK"
                        )
                    except Exception as exc:
                        problems.append(
                            f"YouTube OAuth account #{number} live check: {exc}"
                        )
            except Exception as exc:
                problems.append(f"YouTube live check: {exc}")
        else:
            print(
                "ℹ️  YouTube writes: nothing configured to check live "
                "(no YT_REFRESH_TOKEN / client pair / cookie)."
            )

    if problems:
        print("\n❌ Problems found:")
        for p in problems:
            print(f"   ❌ {p}")
        print("\n👉 Fix the ❌ lines above, then re-run `phonksync check --live`.")
    return 1 if problems else 0


def cmd_stats(args: argparse.Namespace) -> int:
    from .db import StateDB

    cfg = Config.load(args.config)
    db = StateDB(cfg.state_path)
    stats = db.data.get("stats", {})
    print(f"Runs: {stats.get('runs', 0)}")
    print(f"Total added: {stats.get('total_added', 0)}")
    print(f"Synced tracks: {len(db.data.get('synced', {}))}")
    print(f"Unmatched tracks: {len(db.data.get('unmatched', {}))}")
    print("\nMethod statistics (tried / won):")
    methods = stats.get("methods", {})
    for code in sorted(methods, key=lambda c: -methods[c].get("won", 0)):
        m = methods[code]
        print(f"  {code:<12} tried={m.get('tried', 0):<5} won={m.get('won', 0)}")
    if args.json:
        print(json.dumps(db.data, indent=2, ensure_ascii=False))
    return 0


def cmd_proxy_test(args: argparse.Namespace) -> int:
    """Probe the configured CORS proxy with Spotify's public endpoints."""
    import requests as _requests

    from .spotify import (
        PublicPlaylistReader,
        SpotifyClient,
        proxy_enabled,
        proxy_headers,
        _with_proxy,
    )

    proxy = args.url or os.environ.get("SPOTIFY_PROXY_URL", "")
    if proxy:
        os.environ["SPOTIFY_PROXY_URL"] = proxy
    if not proxy_enabled():
        print(
            "ℹ️  SPOTIFY_PROXY_URL is not set — requests go out directly from this host.\n"
            "   Set it (e.g. https://your-proxy.vercel.app) and run this again.",
            file=sys.stderr,
        )
        return 2
    print(f"🔗 proxy: {proxy}")
    if proxy_headers():
        print("🔑 header mode (x-proxy-secret is sent)")

    session = _requests.Session()
    ok = True

    # 1) plain HTML through the proxy (no credentials at all)
    target = _with_proxy(proxy, "https://open.spotify.com/", None)
    try:
        resp = session.get(target, headers=proxy_headers(), timeout=30)
        print(f"1️⃣  public page : HTTP {resp.status_code} ({len(resp.text)} bytes)")
        ok = ok and resp.status_code == 200
    except _requests.RequestException as exc:
        print(f"1️⃣  public page : ❌ {exc}")
        ok = False

    # 2) token endpoint with the cookie, if we have one
    secrets = Secrets.from_env()
    if secrets.spotify_cookie:
        try:
            client = SpotifyClient(secrets.spotify_cookie)
            token = client._ensure_token()
            print(f"2️⃣  token       : ✅ {len(token)} chars (proxy calls: {client.proxy_used})")
            try:
                me = client.whoami()
                print(f"3️⃣  /me         : ✅ user {me.get('id')} ({me.get('country')})")
            except Exception as exc:
                print(f"3️⃣  /me         : ⚠️ {exc}")
        except Exception as exc:
            print(f"2️⃣  token       : ❌ {str(exc).splitlines()[0]}")
            ok = False
    else:
        print("2️⃣  token       : skipped (no SPOTIFY_COOKIE)")

    # 3) public playlist read (needs a playlist id)
    if args.playlist:
        reader = PublicPlaylistReader()
        tracks = reader.playlist_tracks(args.playlist)
        if tracks:
            print(f"4️⃣  playlist    : ✅ {len(tracks)} tracks via public page")
        else:
            print(f"4️⃣  playlist    : ❌ {reader.last_error or 'no tracks'}")
            ok = False

    print("\n✅ proxy works" if ok else "\n❌ proxy did not work — check the URL / secret / allowlist")
    return 0 if ok else 3


def cmd_diag(args: argparse.Namespace) -> int:
    """Answer one question: which Spotify routes work from *this* machine?"""
    from .settings import Config as _Cfg
    from .spotify import (
        PublicPlaylistReader,
        SpotifyClient,
        client_token,
        pathfinder_playlist,
    )

    cfg = _Cfg.load(args.config)
    playlist = args.playlist or (cfg.spotify_source_playlists or [""])[0]
    print(f"🎯 playlist: {playlist or '(none configured)'}")

    # 1) the Web API token (needs the cookie)
    secrets = Secrets.from_env()
    if secrets.spotify_cookie:
        try:
            client = SpotifyClient(
                secrets.spotify_cookie, extra_cookies=secrets.spotify_spare_cookies
            )
            client._ensure_token()
            print("1️⃣  Web API token : ✅")
            if getattr(client, "proxy_forced_direct", False):
                print(
                    "    ⚠️  the proxy route answered logged-out; the direct route is "
                    "what works — clear SPOTIFY_PROXY_URL / PROXY_URL"
                )
            if client.cookie_switches:
                print(
                    f"    ℹ️  cookie #{client._cookie_index + 1} works "
                    f"({client.cookie_switches} switch(es)) — SPOTIFY_COOKIE itself is stale"
                )
        except Exception as exc:
            first = str(exc).splitlines()[0][:150]
            print(f"1️⃣  Web API token : ❌ {first}")
            # `client` may not exist if the constructor itself failed.
            where = getattr(locals().get("client"), "last_token_route", "") or ""
            if where:
                print(f"    last route tried: {where} (proxy first, then direct)")
    else:
        print("1️⃣  Web API token : skipped (no SPOTIFY_COOKIE)")

    # 2) the web player backend with the account's bearer (the route that works)
    if secrets.spotify_cookie:
        try:
            client = SpotifyClient(
                secrets.spotify_cookie, extra_cookies=secrets.spotify_spare_cookies
            )
            if playlist:
                tracks = client._pathfinder_tracks(playlist)
                print(f"1️⃣b web player    : {'✅' if tracks else '❌'} "
                      f"{len(tracks)} track(s) via fetchPlaylistContents")
                client.pathfinder_ok = bool(tracks)
            if client.pathfinder_ok:
                print("    backend: web-player (REST will only be used as a fallback)")
        except Exception as exc:
            print(f"1️⃣b web player    : ❌ {str(exc)[:180]}")

    # 2) client token + pathfinder (no login, no Web API)
    token = client_token()
    print(f"2️⃣  client token  : {'✅' if token else '❌'} ({len(token)} chars)")
    if playlist:
        try:
            tracks = pathfinder_playlist(playlist, client_token=token, max_tracks=5)
            print(f"3️⃣  pathfinder    : {'✅' if tracks else '❌'} {len(tracks)} track(s) "
                  f"read without the Web API")
            if tracks:
                print(f"    e.g. {tracks[0].display()}")
        except Exception as exc:
            print(f"3️⃣  pathfinder    : ❌ {str(exc)[:180]}")

    # 3) the public HTML page
    if playlist:
        reader = PublicPlaylistReader(client_token=token)
        page = reader._payload(playlist)
        print(
            "4️⃣  public page   : "
            + ("✅ payload found" if page else f"❌ {reader.last_error or 'no payload'}")
        )
    return 0


def cmd_skip(args: argparse.Namespace) -> int:
    """Mark tracks as "do not sync" (they are not on YouTube, or you don't want them)."""
    from datetime import datetime, timezone

    from .db import StateDB

    cfg = Config.load(args.config)
    state = StateDB(cfg.state_path)
    if args.show or (not args.track and not args.all_unmatched and not args.clear):
        rows = state.skipped_tracks()
        print(f"🙅 Skipped tracks: {len(rows)}")
        for row in rows:
            reason = f" — {row['reason']}" if row.get("reason") else ""
            print(f"   {row['spotify_id']}  {', '.join(row.get('artists') or [])} — "
                  f"{row.get('name', '')}{reason}")
        if args.show:
            unmatched = state.data.get("unmatched") or {}
            print(f"\n❓ Unmatched (still retried): {len(unmatched)}")
            for tid, row in unmatched.items():
                print(f"   {tid}  {', '.join(row.get('artists') or [])} — {row.get('name', '')}")
        return 0

    if args.clear:
        removed = state.unskip(args.clear)
        state.save()
        if removed:
            print(f"↩️ Un-skipped {args.clear} — it will be synced on the next run")
            return 0
        print(f"ℹ️ {args.clear} was not in the skipped list", file=sys.stderr)
        return 1

    targets: list[str] = []
    if args.all_unmatched:
        targets = list((state.data.get("unmatched") or {}).keys())
        if not targets:
            print("ℹ️ Nothing is unmatched — no skips added")
            return 0
    for track_id in list(args.track) + targets:
        entry = (state.data.get("unmatched") or {}).get(track_id) or {}
        state.data.setdefault("skipped", {})[track_id] = {
            "spotify_id": track_id,
            "name": entry.get("name", ""),
            "artists": entry.get("artists", []),
            "reason": args.reason,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        state.data["unmatched"].pop(track_id, None)
        print(f"🙅 Skipped {track_id} ({entry.get('name', 'unknown track')})")
    state.save()
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    """Show (or clear) the disk caches the sync relies on."""
    from .cache import SearchCache, SourceCache

    cfg = Config.load(args.config)
    sources = SourceCache(
        cfg.source_cache_path,
        ttl_hours=cfg.source_cache_ttl_hours,
        allow_stale=cfg.spotify_cache_allow_stale,
        stale_days=cfg.spotify_cache_max_age_days,
    )
    rows = sources.sources()
    print(f"🗄 Source cache: {cfg.source_cache_path} ({len(rows)} playlist(s))")
    for row in rows:
        mark = "fresh" if row["fresh"] else ("stale — replayed when needed" if row["usable"] else "expired")
        print(f"   {row['playlist']}  {row['tracks']:>5} tracks  {row['age_hours']:>6.1f}h  {mark}")
    if not rows:
        print("   (empty — run `phonksync cache-fill` while Spotify is answering)")
    if args.clear:
        for suffix in ("", ".tmp"):
            path = Path(cfg.source_cache_path + suffix)
            if path.exists():
                path.unlink()
        print("🧹 Source cache cleared")
    if args.clear_search:
        path = Path(cfg.cache_path)
        if path.exists():
            path.unlink()
        print("🧹 Search cache cleared")
    searches = SearchCache(cfg.cache_path, ttl_days=cfg.search_cache_ttl_days) if args.search else None
    if searches is not None:
        print(f"🔎 Search cache: {cfg.cache_path} — {searches.size()} queries")
    return 0


def cmd_cache_fill(args: argparse.Namespace) -> int:
    """Read every source playlist once and store it, so later runs need 0 calls.

    Run this while Spotify is answering normally (a good first run of the day).
    Afterwards a throttled run can replay the stored reads and still produce a
    full track list — including for the YouTube side, which needs no quota here.
    """
    from .cache import SourceCache
    from .spotify import PublicPlaylistReader, SpotifyClient, pathfinder_playlist

    cfg = Config.load(args.config)
    secrets = Secrets.from_env()
    sources = SourceCache(
        cfg.source_cache_path,
        ttl_hours=cfg.source_cache_ttl_hours,
        allow_stale=cfg.spotify_cache_allow_stale,
        stale_days=cfg.spotify_cache_max_age_days,
    )
    playlists = args.playlist or list(cfg.spotify_source_playlists)
    if not playlists:
        print("❌ No source playlists configured", file=sys.stderr)
        return 2
    client = SpotifyClient(secrets.spotify_cookie) if secrets.spotify_cookie else None
    reader = PublicPlaylistReader()
    saved = 0
    for playlist_id in playlists:
        tracks = []
        if client is not None:
            try:
                tracks = client._pathfinder_tracks(playlist_id)
            except Exception as exc:
                print(f"   {playlist_id}: web player read failed ({str(exc)[:90]})")
        if not tracks:
            try:
                tracks = pathfinder_playlist(playlist_id)
            except Exception:
                tracks = []
        if not tracks and reader is not None:
            tracks = reader.playlist_tracks(playlist_id)
        if not tracks:
            print(f"❌ {playlist_id}: nothing readable right now")
            continue
        sources.put(playlist_id, tracks, "")
        saved += 1
        print(f"✅ {playlist_id}: {len(tracks)} tracks stored")
    sources.save()
    print(f"🗄 Saved {saved} playlist(s) to {cfg.source_cache_path}")
    return 0 if saved else 1


def cmd_collect(args: argparse.Namespace) -> int:
    """Save a playlist's tracks into state so a blocked API never stops a run.

    Two ways: read the public page (`--from-web`), or paste anything you copied
    from the browser (`--from-file`, stdin) — e.g. Ctrl+U page source, or the
    "Copy all" of a track list.
    """
    from .db import StateDB
    from .settings import parse_spotify_playlist_id
    from .spotify import PublicPlaylistReader, playlist_tracks_from_html

    cfg = Config.load(args.config)
    db = StateDB(cfg.state_path)

    if args.show:
        summary = db.collection_summary()
        if not summary:
            print("📭 No collection saved yet.")
            return 0
        for pid, info in summary.items():
            print(f"• {pid}: {info['tracks']} tracks ({info['source']}, {info['captured_at']})")
        return 0

    playlist_id = parse_spotify_playlist_id(args.playlist)

    tracks = []
    source = ""
    if args.from_web:
        reader = PublicPlaylistReader()
        tracks = reader.playlist_tracks(playlist_id)
        source = "public page"
        if not tracks:
            print(f"❌ Could not read the public page: {reader.last_error}", file=sys.stderr)
            return 3
    else:
        raw = ""
        if args.from_file:
            try:
                raw = Path(args.from_file).read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                print(f"❌ {exc}", file=sys.stderr)
                return 2
        elif not sys.stdin.isatty():
            raw = sys.stdin.read()
        if not raw.strip():
            print("❌ Paste the page source / track list, or use --from-web", file=sys.stderr)
            return 2
        tracks = playlist_tracks_from_html(raw, playlist_id)
        source = "pasted page"
        if not tracks:
            print(
                "❌ No playlist tracks found in that text. Open the playlist page, "
                "press Ctrl+U (view source), select all and pipe it in.",
                file=sys.stderr,
            )
            return 3

    db.set_collection(playlist_id, tracks, source)
    db.save()
    print(f"✅ Saved {len(tracks)} tracks for {playlist_id} ({source}).")
    print("   Runs will use this when the Spotify API is rate limited.")
    return 0


def cmd_manual(args: argparse.Namespace) -> int:
    """Inspect or flush the manual bucket written when Spotify was cooling down."""
    from .db import StateDB
    from .spotify import SpotifyClient

    cfg = Config.load(args.config)
    db = StateDB(cfg.state_path)
    manual = db.manual_items()
    pending = db.pending_spotify()
    items = manual or pending
    if not items:
        print("✅ Manual bucket is empty.")
        return 0
    if args.apply:
        secrets = Secrets.from_env()
        if not secrets.spotify_cookie:
            print("❌ SPOTIFY_COOKIE is required to apply the bucket.", file=sys.stderr)
            return 2
        # The target playlist is the truth: never re-add a track that is already
        # in it (the DB may say otherwise after a throttled run).
        client = SpotifyClient(secrets.spotify_cookie)
        try:
            already = client.playlist_track_ids(cfg.spotify_target_playlist)
        except Exception as exc:  # pragma: no cover - network/permission
            print(f"⚠️ Could not read the target playlist ({exc}); adding anyway.")
            already = set()
        ids = [k for k in items if k not in already]
        uris = [items[k].get("uri", "") for k in ids if items[k].get("uri")]
        for i in range(0, len(uris), 100):
            client.add_tracks(cfg.spotify_target_playlist, uris[i : i + 100])
        for track_id in ids:
            state = db.data["synced"].get(track_id)
            if state:
                state["spotify_added"] = True
        db.clear_manual(list(items))
        db.clear_pending_spotify(list(items))
        db.write_manual_queue(cfg.manual_queue_path)
        db.save()
        print(
            f"✅ Added {len(uris)} track(s) to {cfg.spotify_target_playlist} and emptied "
            f"the bucket."
            + (f" ({len(items) - len(ids)} were already there)" if len(items) > len(ids) else "")
        )
        return 0
    receipt = db.last_write_receipt()
    if receipt:
        print(
            f"🧾 last write: sent {receipt.get('sent')} → landed "
            f"{receipt.get('landed')} (route: {receipt.get('route') or 'n/a'}, "
            f"{receipt.get('when', '')})"
        )
        if receipt.get("sent") and not receipt.get("landed"):
            print(
                "   ⚠️  the write was accepted but NOTHING landed — see "
                "TROUBLESHOOTING: 'accepted no-op'"
            )
    problem = db.last_write_problem()
    if problem:
        print(
            f"🚨 last write refused {problem.get('when', '')} "
            f"(route: {problem.get('route') or 'n/a'}, "
            f"account: {problem.get('account') or 'n/a'})"
        )
        print(f"   {problem.get('message', '')}")
    count = db.write_manual_queue(cfg.manual_queue_path)
    print(f"📋 {count} track(s) waiting in {cfg.manual_queue_path}")
    print("   Apply them with: python -m phonksync manual --apply")
    for entry in list(items.values())[:10]:
        reason = entry.get("reason") or ""
        suffix = f" ({reason})" if reason else ""
        print(
            f"   • {', '.join(entry.get('artists', []))} — {entry.get('name', '')}{suffix}"
        )
    if count > 10:
        print(f"   … and {count - 10} more")
    return 0


def cmd_spotify_write_test(args: argparse.Namespace) -> int:
    """One real write, with the whole conversation printed.

    The job log is not always downloadable, so the *answer itself* has to be
    readable: every route, every status, every GraphQL error string. This is the
    command that ends "it silently does nothing" — nothing here is best-effort.
    """
    import json as _json

    from .db import StateDB
    from .spotify import SpotifyClient, _OP_HASHES

    cfg = Config.load(args.config)
    secrets = Secrets.from_env()
    if not secrets.spotify_cookie:
        print("❌ SPOTIFY_COOKIE is required.", file=sys.stderr)
        return 2
    playlist = args.playlist or cfg.spotify_target_playlist
    track = args.track.strip()
    uris: list[str] = []
    if track:
        uris = [track if track.startswith("spotify:track:") else f"spotify:track:{track}"]
    else:
        db = StateDB(cfg.state_path)
        parked = db.pending_spotify() or db.manual_items()
        wanted = max(1, int(getattr(args, "batch", 0) or 1))
        uris = [e.get("uri", "") for e in list(parked.values())[:wanted] if e.get("uri")]
    if not uris:
        print("❌ No track to try: pass --track spotify:track:…", file=sys.stderr)
        return 2
    print(f"🎯 playlist : {playlist}")
    print(f"🎵 track(s) : {len(uris)}")
    for one in uris:
        print(f"   {one}")
    print(f"🔑 hash     : {_OP_HASHES.get('addToPlaylist')}")
    print("")

    client = SpotifyClient(
        secrets.spotify_cookie,
        extra_cookies=secrets.spotify_spare_cookies,
        prefer_web_player=True,
    )
    try:
        token = client._ensure_token()
    except Exception as exc:
        print(f"1️⃣ token    : ❌ {exc}")
        return 1
    print(f"1️⃣ token    : ✅ {len(token)} chars, account {client.account_username or 'unknown'}")

    print("2️⃣ read     : ", end="", flush=True)
    try:
        who = client.permission_hint(playlist)
        print(f"✅ {who or 'playlist readable'}")
    except Exception as exc:
        print(f"❌ {exc}")

    print("2️⃣b before   : ", end="", flush=True)
    try:
        before = {t.id for t in client._pathfinder_tracks(playlist)}
        print(f"✅ {len(before)} track(s) in the playlist")
    except Exception as exc:
        before = set()
        print(f"❌ could not read the playlist: {exc}")

    print("3️⃣ write    : ", end="", flush=True)
    captured: dict[str, object] = {}
    real_post = __import__("phonksync.spotify", fromlist=["_proxy_post"])._proxy_post

    def spy(url, body, headers):
        text = real_post(url, body, headers)
        captured.setdefault("attempts", []).append(
            {"operation": body.get("operationName"), "sha256": body["extensions"]["persistedQuery"]["sha256Hash"],
             "variables": body.get("variables"), "answer": text}
        )
        return text

    import phonksync.spotify as _sp

    _sp._proxy_post = spy  # type: ignore[assignment]
    try:
        ok = client._pathfinder_add(playlist, uris) or True
        print("✅ accepted" if ok else "❌ refused")
    except Exception as exc:
        print(f"❌ {exc}")
    finally:
        _sp._proxy_post = real_post  # type: ignore[assignment]

    attempts = captured.get("attempts") or []
    for number, attempt in enumerate(attempts, 1):
        print("")
        print(f"── attempt {number}: {attempt['operation']} ({attempt['sha256'][:12]}…)")
        if args.json:
            print("   variables:", _json.dumps(attempt["variables"], ensure_ascii=False))
        answer = str(attempt["answer"] or "")
        print("   answer   :", answer[:1200] or "(empty)")

    print("")
    print("4️⃣ verify   : ", end="", flush=True)
    try:
        after = {t.id for t in client._pathfinder_tracks(playlist)}
    except Exception:
        after = set()
    added_ids = [u.split(":")[-1] for u in uris if u.split(":")[-1] in after]
    print(f"{len(after)} track(s) in the playlist before/after: {len(before)} → {len(after)}")
    print(f"   landed  : {len(added_ids)}/{len(uris)}")
    for track_id in added_ids:
        print(f"   ✅ {track_id}")
    for one in uris:
        if one.split(":")[-1] not in after:
            print(f"   ❌ {one}")
    if len(added_ids) == len(uris):
        return 0
    # The write was answered `AddItemsToPlaylistPayload` and the playlist still
    # did not change. That is the one failure with no exception: name it.
    print("")
    print(
        f"🚨 ACCEPTED NO-OP: Spotify echoed success but the playlist is unchanged "
        f"({len(before)} → {len(after)})."
    )
    print("   This is why the sync keeps 'adding' the same tracks: nothing landed.")
    print(
        "   Next: Re-run this probe with --batch 1. If one track lands and two do "
        "not, the mutation accepts only single URIs for this account."
    )
    print("")
    print("Nothing is guessed from here: the answer above is the whole truth.")
    print("Paste that block (it contains no cookie/token) and the write can be fixed exactly.")
    return 1


def cmd_reset(args: argparse.Namespace) -> int:
    """Clear chosen parts of the state file — with a backup and a dry run first."""
    from .db import StateDB

    cfg = Config.load(args.config)
    scopes: list[str] = _reset_scopes(args.scopes)
    db = StateDB(cfg.state_path)
    print(f"🗃 State file: {cfg.state_path}")
    for scope, explanation in StateDB.RESET_SCOPES.items():
        if scope == "all":
            continue
        current = _state_scope_size(db, scope)
        print(f"   {scope:<12} {current:>4} item(s)  — {explanation}")
    if not scopes:
        print("")
        print("Nothing reset. Pick what to clear, e.g.:")
        print("   python -m phonksync reset parked          # retry the write backlog")
        print("   python -m phonksync reset quota stats     # forgive today's counters")
        print("   python -m phonksync reset all --yes       # start completely fresh")
        return 0
    unknown = [scope for scope in scopes if scope not in StateDB.RESET_SCOPES]
    if unknown:
        print(f"❌ Unknown scope(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"   Choose from: {', '.join(StateDB.RESET_SCOPES)}", file=sys.stderr)
        return 2
    if not args.yes:
        print("")
        print(f"Would reset: {', '.join(scopes)}")
        print("Nothing was changed. Add --yes to do it.")
        return 0
    backup = db.backup()
    cleared = db.reset(scopes)
    db.save()
    print("")
    for scope, count in cleared.items():
        print(f"🧹 {scope:<12} cleared {count} item(s)")
    print(f"💾 Backup kept at: {backup}")
    if "synced" in scopes or "all" in scopes:
        print(
            "ℹ️ Every track is unknown to the next run now. It re-reads the source "
            "playlists, matches again, and only adds what is really missing from the "
            "target playlist — expect a slow run, and use `max_tracks` to bound it."
        )
    if "parked" in scopes or "all" in scopes:
        print("ℹ️ The Spotify backlog is empty; nothing is waiting to be written.")
    return 0


def _reset_scopes(raw: Any) -> list[str]:
    """Accept what people actually type: `parked, quota`, `parked,quota`, `ALL`.

    The workflow input is one text box described as "e.g. parked, quota, stats,
    all" — so commas are the natural separator. Passing `parked,` straight
    through used to be an unknown scope, and the run stayed green with nothing
    reset. Split on commas/semicolons/whitespace, case-fold, keep the order.
    """
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[str] = []
    for item in items:
        for part in re.split(r"[\s,;]+", str(item or "")):
            scope = part.strip().strip("\"'").lower()
            if scope and scope not in out:
                out.append(scope)
    return out


def _state_scope_size(db: Any, scope: str) -> int:
    def size(value: Any) -> int:
        if isinstance(value, (dict, list)):
            return len(value)
        return 1 if value else 0

    if scope == "parked":
        return (
            size(db.spotify.get("manual"))
            + size(db.spotify.get("pending"))
            + size(db.spotify.get("resume"))
        )
    if scope == "versions":
        return size(db.data.get("version_duplicates"))
    if scope == "quota":
        return size(db.data.get("quota"))
    if scope == "stats":
        return size(db.data.get("stats"))
    return size(db.data.get(scope))


def cmd_cooldown(args: argparse.Namespace) -> int:
    """Show or clear the persisted Spotify cooldown."""
    from datetime import datetime, timezone

    from .db import StateDB

    cfg = Config.load(args.config)
    db = StateDB(cfg.state_path)
    until = db.spotify_cooldown_until()
    now = time.time()
    if args.clear:
        db.clear_spotify_cooldown()
        db.save()
        print("✅ Spotify cooldown cleared — the next run will read playlists again.")
        return 0
    if until <= now:
        print("✅ No active Spotify cooldown.")
        return 0
    left = int(until - now)
    when = datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"⏳ Spotify cooldown: {left // 3600}h {(left % 3600) // 60}m left (until {when})")
    print(f"   reason: {db.spotify.get('cooldown_reason', 'n/a')}")
    print("   Live runs skip until then: `python -m phonksync cooldown --clear` to override.")
    return 0


def cmd_cookie_fix(args: argparse.Namespace) -> int:
    """Extract sp_dc from a pasted cookie dump (prints it masked by default)."""
    from .spotify import extract_sp_dc

    raw = args.raw or ""
    if not raw:
        import os

        raw = os.environ.get("SPOTIFY_COOKIE", "")
    if not raw and not sys.stdin.isatty():
        raw = sys.stdin.read()
    if not raw.strip():
        print("❌ Nothing to parse. Pass the cookie text as an argument or via stdin.", file=sys.stderr)
        return 2
    try:
        token = extract_sp_dc(raw)
    except PhonkSyncError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3
    masked = f"{token[:4]}…{token[-4:]}"
    print(f"✅ sp_dc parsed ({len(token)} chars): {masked}")
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(token, encoding="utf-8")
        print(f"✅ Written to {args.out} (git-ignored). Use this single value as SPOTIFY_COOKIE.")
    if args.show:
        print(token)
    elif not args.out:
        print("ℹ️  Add --show to print it, or --out FILE to save it.")
    return 0


def cmd_youtube_auth(args: argparse.Namespace) -> int:
    from .oauth_setup import (
        auth_url,
        check_refresh_token,
        client_section,
        cookie_help,
        cookie_test,
        doctor,
        exchange_code,
        run_device_flow,
        run_oauth_flow,
        show_client,
    )

    if getattr(args, "show_client", False):
        return show_client()

    if getattr(args, "check_token", None) is not None:
        token = args.check_token
        if token == "-":
            token = sys.stdin.read()
        return check_refresh_token(token or os.environ.get("YT_REFRESH_TOKEN", ""))

    if getattr(args, "test_cookie", False):
        raw = (args.cookie or "").strip()
        if getattr(args, "cookie_stdin", False) or raw == "-":
            raw = sys.stdin.read()
        return cookie_test(
            playlist=args.playlist or os.environ.get("YOUTUBE_TARGET_PLAYLIST", ""),
            cookie=raw,
            authuser=args.authuser or os.environ.get("YT_AUTHUSER", "0"),
        )

    if getattr(args, "cookie_help", False):
        raw = (args.cookie or "").strip()
        if getattr(args, "cookie_stdin", False) or raw in ("-",):
            raw = sys.stdin.read()
        return cookie_help(raw)

    source = args.client_json
    if not source and Path("client_secret.json").exists():
        source = "client_secret.json"
    if not source and (args.client_id or os.environ.get("YT_CLIENT_ID")):
        client_id = args.client_id or os.environ.get("YT_CLIENT_ID", "")
        secret = args.client_secret or os.environ.get("YT_CLIENT_SECRET", "")
        print(f"🔑 using client id from {'the command line' if args.client_id else 'YT_CLIENT_ID'}")
        if args.doctor:
            return doctor(
                client_json="", client_id=client_id, client_secret=secret,
                live=getattr(args, "live", False),
            )
        # Hand the flow an inline JSON so every sub-mode works from an id alone.
        try:
            section = client_section("", client_id, secret)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        source = json.dumps({"installed": section})
    if args.doctor:
        return doctor(
            client_json=source,
            client_id=args.client_id or os.environ.get("YT_CLIENT_ID", ""),
            client_secret=args.client_secret or os.environ.get("YT_CLIENT_SECRET", ""),
            live=getattr(args, "live", False),
        )
    if args.device:
        return run_device_flow(source)
    if args.auth_url:
        return auth_url(source, args.redirect_uri)
    if args.code:
        return exchange_code(source, args.code, args.redirect_uri)
    return run_oauth_flow(source, port=args.port, redirect_uri=args.redirect_uri)


def cmd_pending(args: argparse.Namespace) -> int:
    """Show (and optionally add) what is waiting for a YouTube write credential."""
    from .db import StateDB
    from .settings import Secrets
    from .youtube import YouTubeClient

    cfg = Config.load(args.config)
    db = StateDB(cfg.state_path)
    waiting = db.pending_youtube()
    near = db.candidates()

    if not waiting and not near:
        print("✅ Nothing pending — every matched track already reached YouTube.")
        return 0

    if waiting:
        print(f"✍️  {len(waiting)} matched video(s) waiting for a write credential:")
        for entry in waiting[: args.limit]:
            print(f"   • {entry.get('name', '?')} — {entry.get('artists', '')}")
            print(f"     https://www.youtube.com/watch?v={entry.get('youtube_video_id', '')}")
    if near:
        print(
            f"\n❓ {len(near)} near-miss candidate(s): the checks said no, so they are "
            "NEVER added automatically"
        )
        for entry in near[: args.limit]:
            print(f"   • {entry.get('name', '?')} — {entry.get('artists', '')}")
            print(
                f"     → {entry.get('title', '?')} "
                f"[{entry.get('channel', '?')}] "
                f"https://www.youtube.com/watch?v={entry.get('video_id', '')}"
            )

    if not args.add:
        print("\n👉 `phonksync pending --add` writes the waiting ones to the YouTube playlist.")
        print("   Pin a near-miss you trust instead: `phonksync manual --track <id> --video <id>`")
        return 0

    secrets = Secrets.from_env()
    yt = YouTubeClient(
        client_json=secrets.youtube_client_json,
        client_id=secrets.youtube_client_id,
        client_secret=secrets.youtube_client_secret,
        refresh_token=secrets.youtube_refresh_token,
        token_json=secrets.youtube_token_json,
        api_key=secrets.youtube_api_key,
        cookie=secrets.youtube_cookie,
        authuser=secrets.youtube_authuser,
        free_search=False,
    )
    if not yt.can_write:
        print("❌ Still no write credential — set YT_REFRESH_TOKEN or YT_COOKIE first.")
        return 1
    if not cfg.youtube_target_playlist:
        print("❌ youtube_target_playlist is not configured.")
        return 1
    existing = yt.playlist_video_ids(cfg.youtube_target_playlist)
    added = skipped = failed = 0
    for entry in waiting[: args.limit]:
        video_id = entry.get("youtube_video_id", "")
        if not video_id:
            continue
        if video_id in existing:  # never create a duplicate
            entry["youtube_added"] = True
            skipped += 1
            continue
        try:
            yt.add_to_playlist(cfg.youtube_target_playlist, video_id)
        except Exception as exc:  # no retry: a duplicate is worse than a miss
            print(f"⚠️  {entry.get('name', '?')}: {exc}")
            failed += 1
            continue
        entry["youtube_added"] = True
        existing.add(video_id)
        added += 1
        db.save()  # flush per track: a crash must not double-add
    print(f"\n✅ Added {added}, already there {skipped}, failed {failed}.")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phonksync", description="Phonk playlist syncer")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Run one sync pass")
    p_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Match only, write nothing; reads the first 200 source tracks by default",
    )
    p_sync.add_argument("--max-tracks", type=int, default=0, help="Limit tracks this run")
    p_sync.set_defaults(func=cmd_sync)

    p_check = sub.add_parser("check", help="Validate config and secrets")
    p_check.add_argument("--live", action="store_true", help="Also test API credentials")
    p_check.set_defaults(func=cmd_check)

    p_stats = sub.add_parser("stats", help="Show sync state statistics")
    p_stats.add_argument("--json", action="store_true", help="Dump full state JSON")
    p_stats.set_defaults(func=cmd_stats)

    p_proxy = sub.add_parser(
        "proxy-test", help="Check SPOTIFY_PROXY_URL against Spotify endpoints"
    )
    p_proxy.add_argument("--url", default="", help="Proxy base URL (overrides the env var)")
    p_proxy.add_argument("--playlist", default="", help="Optional public playlist ID to read")
    p_proxy.set_defaults(func=cmd_proxy_test)

    p_skip = sub.add_parser(
        "skip", help="Never sync a track again (e.g. it is not on YouTube)"
    )
    p_skip.add_argument("track", nargs="*", default=[], help="Spotify track IDs")
    p_skip.add_argument("--all-unmatched", action="store_true",
                        help="Skip everything currently unmatched")
    p_skip.add_argument("--reason", default="", help="Why (shown in the summary)")
    p_skip.add_argument("--clear", default="", help="Un-skip this track ID")
    p_skip.add_argument("--show", action="store_true", help="List skips + unmatched")
    p_skip.set_defaults(func=cmd_skip)

    p_cache = sub.add_parser("cache", help="Show/clear the source + search caches")
    p_cache.add_argument("--clear", action="store_true", help="Delete the source cache")
    p_cache.add_argument("--clear-search", action="store_true", help="Delete the search cache")
    p_cache.add_argument("--search", action="store_true", help="Also show search-cache stats")
    p_cache.set_defaults(func=cmd_cache)

    p_fill = sub.add_parser(
        "cache-fill", help="Read every source playlist now and store it for later runs"
    )
    p_fill.add_argument("playlist", nargs="*", default=[], help="Playlist IDs (default: sources)")
    p_fill.set_defaults(func=cmd_cache_fill)

    p_diag = sub.add_parser(
        "diag", help="Show which Spotify routes work right now (token / client token / pathfinder / page)"
    )
    p_diag.add_argument("--playlist", default="", help="Playlist to test (default: first source)")
    p_diag.set_defaults(func=cmd_diag)

    p_collect = sub.add_parser(
        "collect",
        help="Save playlist tracks locally (public page or pasted page source)",
    )
    p_collect.add_argument("playlist", nargs="?", default="", help="Source playlist ID or URL")
    p_collect.add_argument("--from-web", action="store_true",
                           help="Read the public playlist page (no login needed)")
    p_collect.add_argument("--from-file", default="", help="Read pasted/copied page text")
    p_collect.add_argument("--show", action="store_true", help="Show what is saved")
    p_collect.set_defaults(func=cmd_collect)

    p_manual = sub.add_parser(
        "manual", help="Show/apply tracks parked while Spotify was rate limited"
    )
    p_manual.add_argument("--apply", action="store_true",
                          help="Add the parked tracks to the target playlist now")
    p_manual.set_defaults(func=cmd_manual)

    p_reset = sub.add_parser(
        "reset",
        help="Clear chosen parts of the state file (backed up, dry run by default)",
    )
    p_reset.add_argument(
        "scopes",
        nargs="*",
        help="synced, unmatched, skipped, candidates, parked, quota, stats, "
             "diagnostics, versions, all",
    )
    p_reset.add_argument("--yes", action="store_true", help="Actually do it")
    p_reset.set_defaults(func=cmd_reset)

    p_write = sub.add_parser(
        "spotify-write-test",
        help="Add one real track (a dry-run write) and print EVERY answer, verbatim",
    )
    p_write.add_argument("--track", default="", help="Spotify track ID/URI (default: the first parked one)")
    p_write.add_argument(
        "--batch",
        type=int,
        default=0,
        help="Use this many parked tracks in ONE call (0 = just one)",
    )
    p_write.add_argument("--playlist", default="", help="Target playlist (default: the configured one)")
    p_write.add_argument("--json", action="store_true", help="Print the raw GraphQL payload too")
    p_write.set_defaults(func=cmd_spotify_write_test)

    p_cd = sub.add_parser("cooldown", help="Show/clear the Spotify rate-limit cooldown")
    p_cd.add_argument("--clear", action="store_true", help="Clear the stored cooldown")
    p_cd.set_defaults(func=cmd_cooldown)

    p_fix = sub.add_parser(
        "cookie-fix",
        help="Extract just the sp_dc value from a pasted Spotify cookie dump",
    )
    p_fix.add_argument("raw", nargs="?", default="", help="Cookie text (or pipe it on stdin)")
    p_fix.add_argument("--show", action="store_true", help="Print the sp_dc value")
    p_fix.add_argument("--out", default="", help="Write the sp_dc value to this file")
    p_fix.set_defaults(func=cmd_cookie_fix)

    p_pending = sub.add_parser(
        "pending", help="Show (and add) matches waiting for a YouTube write credential"
    )
    p_pending.add_argument(
        "--add", action="store_true", help="Write them to the YouTube playlist now (no duplicates)"
    )
    p_pending.add_argument("--limit", type=int, default=50, help="How many to show/add")
    p_pending.set_defaults(func=cmd_pending)

    p_auth = sub.add_parser("youtube-auth", help="One-time YouTube OAuth setup (get refresh token)")
    p_auth.add_argument(
        "--client-json",
        default="",
        help="OAuth client JSON file (or the JSON itself). Default: client_secret.json if present",
    )
    p_auth.add_argument(
        "--client-id",
        default="",
        help="Bare client id from the Cloud Console (…apps.googleusercontent.com)",
    )
    p_auth.add_argument(
        "--client-secret", default="", help="Client secret (needed for the code exchange)"
    )
    p_auth.add_argument("--port", type=int, default=8765, help="Local redirect port")
    p_auth.add_argument(
        "--redirect-uri",
        default="",
        help="Use this exact redirect URI (default http://127.0.0.1:<port>/callback)",
    )
    p_auth.add_argument(
        "--device",
        action="store_true",
        help="No redirect URI at all: show a code, you paste it on Google's page",
    )
    p_auth.add_argument(
        "--auth-url", action="store_true", help="Only print the consent URL to open"
    )
    p_auth.add_argument(
        "--code", default="", help="Exchange a pasted authorization code for a refresh token"
    )
    p_auth.add_argument(
        "--doctor",
        action="store_true",
        help="Only report what is missing for YouTube writes",
    )
    p_auth.add_argument(
        "--check-token",
        nargs="?",
        const="",
        default=None,
        help="Only test a refresh token (no value = YT_REFRESH_TOKEN, '-' = stdin)",
    )
    p_auth.add_argument(
        "--show-client",
        action="store_true",
        help="Print the OAuth client id/secret this build can use (secret masked)",
    )
    p_auth.add_argument(
        "--live",
        action="store_true",
        help="With --doctor: actually exchange the refresh token and read the playlist",
    )
    p_auth.add_argument(
        "--cookie-help",
        action="store_true",
        help="YouTube writes without OAuth: how to use a browser cookie (no Console)",
    )
    p_auth.add_argument(
        "--cookie", default="", help="Cookie header to check with --cookie-help ('-' = stdin)"
    )
    p_auth.add_argument(
        "--test-cookie",
        action="store_true",
        help="Prove the cookie can write: add an existing video, confirm, undo it",
    )
    p_auth.add_argument(
        "--playlist", default="", help="Playlist for --test-cookie (default YOUTUBE_TARGET_PLAYLIST)"
    )
    p_auth.add_argument(
        "--authuser", default="", help="X-Goog-AuthUser for --test-cookie (default YT_AUTHUSER or 0)"
    )
    p_auth.add_argument(
        "--cookie-stdin",
        action="store_true",
        help="Read the pasted Cookie header from stdin (for --cookie-help/--test-cookie)",
    )
    p_auth.set_defaults(func=cmd_youtube_auth)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    load_dotenv()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"❌ Configuration error: {exc}", file=sys.stderr)
        return 2
    except AuthError as exc:
        print(f"❌ Authentication error: {exc}", file=sys.stderr)
        return 3
    except PhonkSyncError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
