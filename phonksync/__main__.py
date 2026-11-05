"""CLI entry point: python -m phonksync <command>."""
from __future__ import annotations

import argparse
import json
import logging
import sys

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
    return 1 if report.errors and not report.added else 0


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
            print(
                "✅ YouTube playlist writes: OAuth enabled" if has_oauth
                else "⚠️  YouTube playlist writes disabled — API key cannot add videos "
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
            print(f"✅ Spotify cookie valid (user: {user})")
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
    from .oauth_setup import run_oauth_flow

    return run_oauth_flow(args.client_json, port=args.port)


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
