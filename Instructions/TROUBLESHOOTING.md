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
- Automatic runs are currently paused. If you re-enable a schedule, use a longer cron interval (e.g. daily: `0 3 * * *`).
- Lower `max_searches_per_run` or `dry_run_track_limit`.
- After the initial backlog is synced, runs only handle newly added songs and are short.

## `check` fails: "spotify_target_playlist is not configured"
You have not told the syncer which Spotify playlist to add songs into.
Fix: create a playlist you own on Spotify, copy its link, and add the GitHub secret `SPOTIFY_TARGET_PLAYLIST` (or set `spotify_target_playlist` in `config.json`). It must not also be one of the source playlists.

## YouTube: search works but nothing is added to the playlist
An **API key cannot write**. `search.list` / `videos.list` accept `YOUTUBE_API_KEY`, but `playlistItems.insert` requires OAuth. The Telegram report shows *"YouTube playlist writes disabled (API key only)"* in this case.
Fix: add `YT_CLIENT_JSON` + `YT_REFRESH_TOKEN` (SECRETS.md §2–§3) **in addition to** `YOUTUBE_API_KEY`. The key stays the fast read path; OAuth is used only for inserts.

## `check` fails: "Missing required secrets: YOUTUBE_API_KEY ..."
This only happens if YouTube sync is enabled (`youtube_enabled: true`). The default Spotify-only mode does not need YouTube secrets.
Fix: either disable YouTube sync (`youtube_enabled: false`), or add `YOUTUBE_API_KEY` (read-only search). For actual playlist writes also add `YT_CLIENT_JSON` + `YT_REFRESH_TOKEN` — follow **Instructions/SECRETS.md §3 Option A** (browser-only OAuth Playground). Make sure `YT_CLIENT_JSON` is the JSON of the same OAuth client you used in the Playground.

## "YouTube API key invalid" / `keyInvalid`
- Re-copy the `AIza...` value into the `YOUTUBE_API_KEY` secret (no spaces/quotes).
- The key's project must have **YouTube Data API v3 enabled** (Console → APIs & Services → Library).
- If the key has API restrictions, YouTube Data API v3 must be on the allow-list.
- `python -m phonksync check --live` prints the exact error.

## "Could not find sp_dc in SPOTIFY_COOKIE"
The error lists the cookie **names** it found, e.g. `cookie names found: sp_t, sp_key`. If `sp_dc` is missing from that list, the copy came from the wrong place (`accounts.spotify.com`, a mobile app cookie, or a logged-out tab).

Accepted formats (all of these parse):
- full `Cookie:` header on one line, with or without the `Cookie:` prefix
- multiline dumps / DevTools "Copy request headers" (`cookie:` followed by indented lines)
- WebView or Netscape cookie exports (`name<TAB>value`)
- JSON cookie exports (`{"sp_dc": "AQ..."}` or `[{"name":"sp_dc","value":"AQ..."}]`)
- a bare `sp_dc=AQ...` pair, or the `AQ...` value alone

Fastest reliable route:
```bash
python -m phonksync cookie-fix --show   # paste the whole dump on stdin
```
Then store **only** the printed `AQ...` value as `SPOTIFY_COOKIE`. The value is long (usually 200+ chars); a short value usually means the cookie was truncated by the clipboard or the request was a redirect.

If `sp_dc` is in the list but parsing still failed, the value itself was empty/truncated — copy it again from a **200 OK** request to `open.spotify.com`.

## "Spotify token endpoint returned 403" / `invalid token`
Since March 2025 `open.spotify.com/get_access_token` rejects a bare `sp_dc`
cookie: the web player also sends a **TOTP** code. The syncer handles this
automatically (secret → XOR unscramble → base32 → RFC 6238, then
`GET /api/token?totp=…&totpServer=…&totpVer=…`, with `init` retry and the legacy
endpoint as fallbacks).

If it still fails:
1. The cookie is stale → copy a fresh `Cookie:` header from a logged-in
   open.spotify.com tab (private window recommended) and update `SPOTIFY_COOKIE`.
2. Spotify rotated the TOTP secret → the newest version is fetched from the
   community mirror on each run; set `SPOTIFY_TOTP_REMOTE=1` to force a refresh,
   or pin a version explicitly:
   ```text
   SPOTIFY_TOTP_VER=62
   SPOTIFY_TOTP_CIPHER=12,34,56,...
   ```
   (byte values from https://github.com/xyloflake/spot-secrets-go `secretDict.json`).
3. `python -m phonksync check --live` prints which endpoints were tried and why.

## "Spotify rate limit" / 429 during a sync — see [SPOTIFY-LIMITS.md](SPOTIFY-LIMITS.md)
The web-player token is throttled per rolling window. Reading 2689 tracks with pages of 100 hits 429s almost immediately.

What the syncer does now:
- pages of **50** with a ~0.35s pace between requests (≈170 tracks/minute)
- honours `Retry-After` (capped at 45s, plus jitter) and retries up to 6 times, backing off more if 429s repeat
- if a playlist is still throttled mid-read, it **keeps the tracks already read** and processes them; the rest is picked up next run
- the Telegram summary says `⚠️ Spotify rate limited (429): used the tracks read so far`

If it persists:
- wait 5–10 minutes — the window is short, nothing is corrupted
- run smaller batches: `MAX_TRACKS_PER_RUN=50` (or the `max_tracks` input)
- the YouTube search cache makes repeat runs much cheaper

## "Spotify returned an anonymous token" / 401 from Spotify
The `sp_dc` cookie expired or was copied from a logged-out session.
Fix: log into open.spotify.com again, copy the full Cookie header fresh, update `SPOTIFY_COOKIE`. Avoid logging out of that browser profile.

## "YouTube token refresh failed" / `invalid_grant`
- Refresh token revoked or expired. OAuth consent screen in "Testing" mode expires refresh tokens after 7 days → publish the app (personal use) or re-run `python -m phonksync youtube-auth`.
- Re-run the auth helper and update `YT_REFRESH_TOKEN`.

## "YouTube API quota exceeded" / "Daily search limit reached" / run stops early
Google's default quota is 10,000 units/day: **1 search = 100 units** (max 100 searches/day) and **1 playlist add = 50 units**.

The app caps itself below that so several runs in one day can never trip Google's 403:
- `max_searches_per_run` (default 200) — paid searches per run; free page
  searches are unlimited (see `Instructions/YOUTUBE-BUDGET.md`)
- `youtube_max_searches_per_day` (default 80) — per UTC day, ~8,000 units
- `youtube_max_inserts_per_day` (default 40) — per UTC day, ~2,000 units (adds are only needed once per track)

Counters persist in `data/sync-state.json` → `quota` and reset at UTC midnight.

- "Daily search limit reached" means **your** cap was hit, not Google's — the key is still valid. Wait for UTC rollover or raise `youtube_max_searches_per_day`.
- The run stops cleanly and the next one continues where it left off; the search cache makes repeat queries free.
- A second API key from a *different* Google Cloud project gives a second 10,000 units/day — separate project, separate quota. Add both to `YOUTUBE_API_KEY` separated by a comma (`AIza...,AIza...`); the client rotates automatically on 403 `quotaExceeded` and tracks usage per key in `quota.keys`.
- Two keys from the **same** project share the same 10,000 units — that does not help.

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


## The YouTube cookie stops working

`❌ Authentication error: YouTube rejected the cookie (HTTP 401)` means Google
signed that session out — nothing in the code is broken. Copy a fresh
`Cookie:` header from a signed-in `music.youtube.com` tab, re-check it:

```bash
python -m phonksync youtube-auth --cookie-help --cookie-stdin
```

and update the `YT_COOKIE` secret. A refresh token (`YT_REFRESH_TOKEN`) does not
have this problem; use the cookie when the Console is unreachable.


## "YouTube writes are OFF" in the summary

That line means the run matched tracks and remembered them, but had no
credential to add them to the playlist. The message now names the real cause:

| reason shown | fix |
|---|---|
| `no write credential: set YT_COOKIE …` | copy a `Cookie:` header from a signed-in `music.youtube.com` tab → secret `YT_COOKIE` (`python -m phonksync youtube-auth --cookie-help`) |
| `YT_COOKIE is set but unusable — … no __Secure-3PAPISID` | the paste was clipped or the session is signed out → copy the **whole** header again |
| `YT_COOKIE is set but unusable — … only part of the cookie` | you pasted the URL/host instead of the cookie value |

Everything that was matched is stored with `youtube_added: false`, so the first
run **after** the fix adds up to 50 of them per run with **no new searches**.
