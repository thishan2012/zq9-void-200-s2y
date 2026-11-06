"""CLI entry point: python -m phonksync <command>."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

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


def cmd_check(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    secrets = Secrets.from_env()
    problems: list[str] = []
    try:
        cfg.validate()
        print(f"✅ Config OK — {len(cfg.spotify_source_playlists)} source playlists")
        if not cfg.youtube_enabled:
            print("ℹ️  YouTube sync disabled — Spotify-only mode")
        else:
            key_count = len([k for k in __import__("re").split(r"[\s,;]+", secrets.youtube_api_key) if k])
            print(
                f"🔎 YouTube search: API key (read-only, {key_count} key(s) / "
                f"{cfg.youtube_daily_quota_units * max(1, key_count)} units/day)"
                if key_count else "🔎 YouTube search: OAuth"
            )
            has_oauth = bool(
                (secrets.youtube_client_json or secrets.youtube_token_json)
                and (secrets.youtube_refresh_token or secrets.youtube_token_json)
            )
            if has_oauth:
                print("✅ YouTube playlist writes: OAuth enabled (refresh token — never expires)")
            elif secrets.youtube_access_token:
                print(
                    "⚠️  YouTube writes: manual access token — works ~1 hour, then dies. "
                    "Run `phonksync youtube-auth` once for a YT_REFRESH_TOKEN."
                )
            else:
                print(
                    "⚠️  YouTube playlist writes disabled — API key cannot add videos "
                    "(add YT_CLIENT_JSON + YT_REFRESH_TOKEN)"
                )
    except ConfigError as exc:
        problems.append(f"Config: {exc}")
    try:
        secrets.validate(require_youtube=cfg.youtube_enabled)
        print("✅ Required secrets present")
    except ConfigError as exc:
        problems.append(str(exc))

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

    proxy = os.environ.get("SPOTIFY_PROXY_URL", "").strip() or cfg.spotify_proxy_url
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
                    refresh_token=secrets.youtube_refresh_token,
                    token_json=secrets.youtube_token_json,
                    api_key=secrets.youtube_api_key,
                )
                if yt.api_keys:
                    hits = yt.search("phonk", max_results=1)
                    print(
                        f"✅ YouTube API key works (read-only search, {len(hits)} result, "
                        f"{len(yt.api_keys)} key(s) x {yt.daily_quota_units} units/day)"
                    )
                if yt.can_write:
                    yt.playlist_video_ids(cfg.youtube_target_playlist)
                    print("✅ YouTube OAuth valid, target playlist readable (writes enabled)")
                else:
                    print(
                        "⚠️  YouTube writes disabled (API key only) — add "
                        "YT_CLIENT_JSON + YT_REFRESH_TOKEN to add videos"
                    )
            except Exception as exc:
                problems.append(f"YouTube live check: {exc}")

    for p in problems:
        print(f"❌ {p}")
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
            client = SpotifyClient(secrets.spotify_cookie)
            client._ensure_token()
            print("1️⃣  Web API token : ✅")
        except Exception as exc:
            first = str(exc).splitlines()[0][:150]
            print(f"1️⃣  Web API token : ❌ {first}")
    else:
        print("1️⃣  Web API token : skipped (no SPOTIFY_COOKIE)")

    # 2) the web player backend with the account's bearer (the route that works)
    if secrets.spotify_cookie:
        try:
            client = SpotifyClient(secrets.spotify_cookie)
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
    items = db.spotify.get("manual") or {}
    if not items:
        print("✅ Manual bucket is empty.")
        return 0
    if args.apply:
        secrets = Secrets.from_env()
        if not secrets.spotify_cookie:
            print("❌ SPOTIFY_COOKIE is required to apply the bucket.", file=sys.stderr)
            return 2
        uris = [e.get("uri", "") for e in items.values() if e.get("uri")]
        client = SpotifyClient(secrets.spotify_cookie)
        for i in range(0, len(uris), 100):
            client.add_tracks(cfg.spotify_target_playlist, uris[i : i + 100])
        db.clear_manual()
        db.save()
        print(f"✅ Added {len(uris)} tracks to {cfg.spotify_target_playlist} and emptied the bucket.")
        return 0
    count = db.write_manual_queue(cfg.manual_queue_path)
    print(f"📋 {count} tracks waiting in {cfg.manual_queue_path}")
    print("   Apply them with: python -m phonksync manual --apply")
    for entry in list(items.values())[:10]:
        print(f"   • {', '.join(entry.get('artists', []))} — {entry.get('name', '')}")
    if count > 10:
        print(f"   … and {count - 10} more")
    return 0


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
    from .oauth_setup import auth_url, exchange_code, run_device_flow, run_oauth_flow

    if args.device:
        return run_device_flow(args.client_json)
    if args.auth_url:
        return auth_url(args.client_json, args.redirect_uri)
    if args.code:
        return exchange_code(args.client_json, args.code, args.redirect_uri)
    return run_oauth_flow(args.client_json, port=args.port, redirect_uri=args.redirect_uri)


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

    p_auth = sub.add_parser("youtube-auth", help="One-time YouTube OAuth setup (get refresh token)")
    p_auth.add_argument("--client-json", default="client_secret.json", help="OAuth client JSON file")
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
