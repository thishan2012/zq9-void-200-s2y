# 🛠 Troubleshooting

## Public repo, Google indexing, and runtime

- **Public repo** → standard GitHub-hosted Actions runs do not consume the private-repo monthly minute allowance. The code and committed files *can* still be indexed by Google; there is no reliable per-repo `noindex` on GitHub.
- **No custom syncer time cap** is applied. GitHub's own platform/job limits still exist, but the syncer itself does not stop after a fixed number of minutes.
- **Default mode is Spotify-only**, so no YouTube API quota is used. If you enable YouTube sync, the official YouTube Data API daily quota (default 10,000 units; one search costs 100) applies and the quota budget stops YouTube searching cleanly, resuming on the next run.
- **Dry runs are sampled:** by default they read only the first 200 source tracks (`dry_run_track_limit` in `config.json`) and write nothing. Live syncs read the full source playlists.

**Keep personal data out of a public repo**
The code itself is generic. Keep the *personal* parts out of the public repo:
- Playlist IDs → GitHub Secrets (`SPOTIFY_SOURCE_PLAYLISTS`, `SPOTIFY_TARGET_PLAYLIST`, `YOUTUBE_TARGET_PLAYLIST`), not `config.json`.
- Credentials → GitHub Secrets (already required).
- The state DB (`data/sync-state.json`) contains song names; if that must stay private while the repo is public, store it in a **private gist** or a separate private repo instead of committing it. (Ask and this can be wired up.)

**Reduce load if needed**
- Increase the cron interval (e.g. daily: `0 3 * * *`).
- Lower `max_searches_per_run` or `dry_run_track_limit`.
- After the initial backlog is synced, runs only handle newly added songs and are short.

## `check` fails: "spotify_target_playlist is not configured"
You have not told the syncer which Spotify playlist to add songs into.
Fix: create a playlist you own on Spotify, copy its link, and add the GitHub secret `SPOTIFY_TARGET_PLAYLIST` (or set `spotify_target_playlist` in `config.json`). It must not also be one of the source playlists.

## `check` fails: "Missing required secrets: YT_REFRESH_TOKEN (or YT_TOKEN_JSON)"
This only happens if YouTube sync is enabled (`youtube_enabled: true`). The default Spotify-only mode does not need YouTube secrets.
Fix: either disable YouTube sync (`youtube_enabled: false`), or follow **Instructions/SECRETS.md §3 Option A** (browser-only OAuth Playground) to get a refresh token and add the GitHub secret `YT_REFRESH_TOKEN`. Make sure `YT_CLIENT_JSON` is the JSON of the same OAuth client you used in the Playground.

## "Spotify returned an anonymous token" / 401 from Spotify
The `sp_dc` cookie expired or was copied from a logged-out session.
Fix: log into open.spotify.com again, copy the full Cookie header fresh, update `SPOTIFY_COOKIE`. Avoid logging out of that browser profile.

## "YouTube token refresh failed" / `invalid_grant`
- Refresh token revoked or expired. OAuth consent screen in "Testing" mode expires refresh tokens after 7 days → publish the app (personal use) or re-run `python -m phonksync youtube-auth`.
- Re-run the auth helper and update `YT_REFRESH_TOKEN`.

## "YouTube API quota exceeded" / run stops early
Normal on big first syncs. Google's default quota is 10,000 units/day; one search costs 100.
- The syncer stops cleanly and resumes on the next scheduled run; nothing is lost or re-added.
- The search cache means later runs cost far less.
- You can request a quota increase in Google Cloud Console → APIs & Services → YouTube Data API v3 → Quotas.
- Tune `max_searches_per_run` / `youtube_daily_quota_units` in `config.json`.

## A track never matches
1. Check the Telegram ❌ message / run log for how many methods were tried.
2. Search YouTube manually; if the video exists, pin it in `data/track-overrides.json` (see [README.md](README.md) §8). The `MANUAL` method bypasses all search.
3. Unmatched tracks are retried automatically after `retry_unmatched_after_days` (default 7).

## A wrong video was added
1. Remove the video from the YouTube playlist.
2. Delete the track's entry from `data/sync-state.json` (search by Spotify track ID).
3. Add the correct mapping to `data/track-overrides.json`.
4. Run the workflow again.

## The same song keeps getting added twice
- Make sure the workflow's "Commit sync state" step succeeds (it needs `contents: write` permission, already set in `sync.yml`). If state isn't committed, the next run starts blind.
- Don't run two syncs concurrently — the workflow uses a concurrency group.
- Same recording under different Spotify IDs is deduped by ISRC; tracks without ISRC are deduped by Spotify ID only.

## Telegram messages don't arrive
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (see [SECRETS.md](SECRETS.md) §4–5).
- You must press **Start** in the bot chat first; bots can't message users who never initiated contact.
- Telegram failures never break the sync — they're logged as warnings.

## "Target Spotify playlist must not also be a source playlist"
Remove the target playlist ID from `SPOTIFY_SOURCE_PLAYLISTS` — syncing a playlist into itself is rejected.

## Playlist read errors (404)
- Spotify source playlists must be public or accessible to the cookie's account.
- The YouTube target must belong to the authorized channel.

## Local runs: where do credentials go?
Use a `.env` file in the repo root (git-ignored):
```
SPOTIFY_COOKIE=...
YT_CLIENT_JSON={"installed":{...}}
YT_REFRESH_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```
Then `python -m phonksync check --live`.
