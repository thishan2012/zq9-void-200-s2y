# 🛠 Troubleshooting

## Public vs private repo, Google indexing, and Actions minutes

**The trade-off**
- **Public repo** → unlimited GitHub Actions minutes, but the code and any committed files *can* be indexed by Google. There is no reliable per-repo `noindex` on GitHub.
- **Private repo** → never indexed by Google, but the free plan includes **2,000 Actions minutes/month**.

**Why private is usually fine here (the 43-minute fear)**
A 43-minute run for 500 songs usually comes from *scraping* YouTube search (no quota). This syncer uses the **official YouTube Data API**, where each search costs 100 quota units and the default daily quota is 10,000 units. The built-in quota budget therefore caps each run to roughly **95 searches (~a few minutes)**, and a large first sync automatically spreads across several days. On top of that, `max_minutes_per_run` (default **25**) is a hard wall-clock cap.

So with the default every-12-hours schedule, worst case is about `25 min × 2 runs/day × 30 days = 1,500 min/month` — under the 2,000 free private minutes, and in practice far less because runs are quota-limited and only *new* songs are processed after the first sync.

**If you still want a public repo (unlimited minutes) without exposing personal data**
The code itself is generic and harmless if indexed. Keep the *personal* parts out of the public repo:
- Playlist IDs → GitHub Secrets (`SPOTIFY_SOURCE_PLAYLISTS`, `SPOTIFY_TARGET_PLAYLIST`, `YOUTUBE_TARGET_PLAYLIST`), not `config.json`.
- Credentials → GitHub Secrets (already required).
- The state DB (`data/sync-state.json`) contains song names; if that must stay private while the repo is public, store it in a **private gist** or a separate private repo instead of committing it. (Ask and this can be wired up.)

**Reduce minutes further**
- Increase the cron interval (e.g. daily: `0 3 * * *`).
- Lower `max_minutes_per_run`.
- After the initial backlog is synced, runs only handle newly added songs and are short.

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
