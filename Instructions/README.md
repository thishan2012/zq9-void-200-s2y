# 📖 Setup Instructions

Step-by-step setup for the Phonk Playlist Syncer. Follow the pages in order:

1. **[SECRETS.md](SECRETS.md)** — create every credential and add it to GitHub Secrets ⭐ (required)
2. **[MATCHING.md](MATCHING.md)** — how the 31 search methods and 3 safety checks work
3. **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — quota, expired cookies, wrong matches

---

## 1. What you need

| Thing | Where it comes from |
|---|---|
| Spotify account with the source playlists + a target playlist you own | open.spotify.com |
| Google Cloud project with **YouTube Data API v3** enabled + OAuth client JSON | console.cloud.google.com |
| A YouTube playlist you own (target) | youtube.com |
| Telegram bot + your chat ID | @BotFather |

## 2. Prepare the playlists

1. **Sources:** collect the Spotify playlist URLs you want to read from (any count — 20 is fine). You can use playlists you don't own, as long as they're public or followed by your account.
2. **Spotify target:** create a playlist **on the account whose cookie you will copy** (e.g. "Phonk Sync"). The syncer adds the exact Spotify tracks here.
3. **YouTube target:** create a playlist on the YouTube channel you authorize (e.g. "Phonk Sync"). The syncer adds matched videos here.

Playlist IDs can go into `config.json` (public values) **or** into secrets (recommended if you don't want playlist IDs in a public repo):

- `SPOTIFY_SOURCE_PLAYLISTS` — comma- or newline-separated IDs/URLs
- `SPOTIFY_TARGET_PLAYLIST` — one ID/URL
- `YOUTUBE_TARGET_PLAYLIST` — one ID/URL (`PL...`)

Secrets override `config.json`.

## 3. Create the credentials

Follow **[SECRETS.md](SECRETS.md)** exactly. It covers:

- copying the **full Spotify Cookie header** (the `sp_dc` value is extracted automatically),
- creating the Google OAuth client and getting a **refresh token** (browser-only OAuth Playground method, or the built-in terminal helper),
- creating the Telegram bot and finding your chat ID.

## 4. Add GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add all secrets listed in [SECRETS.md](SECRETS.md). Never commit credentials to the repo — `.gitignore` already blocks `.env`, `client_secret*.json`, and `token*.json`.

## 5. Enable and run

1. Push this repository to GitHub (public is fine — no secrets are in the code).
2. **Actions** tab → enable workflows if prompted.
3. Run it once manually: **Actions → Phonk Sync → Run workflow**.
   - First run: tick `dry_run` to preview matches without writing anything. Dry runs read only the first 200 source tracks (`dry_run_track_limit` in `config.json`).
   - Then run it live. Live syncs read the full source playlists.
4. After that it runs automatically every 12 hours (`.github/workflows/sync.yml`, cron `0 */12 * * *`).

Each live run commits `data/sync-state.json` back to the repo — that is the database that prevents duplicate adds.

## 6. Verify

- Telegram should receive a start message, per-track ✅/❌ messages, and a summary.
- `Actions → latest run → Run sync` log shows the same summary.
- `python -m phonksync stats` (locally, after pulling) shows per-method tried/won counts.

## 7. Optional tuning (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `max_tracks_per_run` | `0` | 0 = unlimited; otherwise cap processed tracks per run |
| `dry_run_track_limit` | `200` | dry-run only: max source tracks to read (0 = unlimited) |
| `max_searches_per_run` | `80` | hard cap on YouTube searches per run |
| `youtube_daily_quota_units` | `9500` | local budget (Google's default daily quota is 10000) |
| `search_results_per_query` | `10` | results fetched per search |
| `duration_tolerance_seconds` | `7` | DURATION/TITLEONLY tolerance |
| `exact_duration_tolerance_seconds` | `3` | DURVER tolerance |
| `retry_unmatched_after_days` | `7` | wait before retrying an unmatched track |
| `telegram_per_track` | `true` | per-track Telegram messages |
| `require_youtube_match_for_spotify` | `false` | if true, a track is only added to Spotify when a YouTube match exists |
| `youtube_region_code` | `US` | search region bias |

## 8. Fixing a wrong or missing match

Add a manual mapping to `data/track-overrides.json` and commit:

```json
{
  "overrides": {
    "11dFghVXanM5MeABsZcnj1": "dQw4w9WgXcQ",
    "BRABC2400001": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
  }
}
```

Keys: Spotify track ID, `spotify:track:` URI, or ISRC. Values: YouTube video ID or URL.
The `MANUAL` method runs first and bypasses all search. If the track was previously synced incorrectly, remove its entry from `data/sync-state.json` (and remove the wrong video from the YouTube playlist) so it gets re-added.
