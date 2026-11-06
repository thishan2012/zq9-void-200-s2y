# 📖 Setup Instructions

Step-by-step setup for the Phonk Playlist Syncer. Follow the pages in order:

1. **[SECRETS.md](SECRETS.md)** — create the credentials and add them to GitHub Secrets ⭐ (required)
2. **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — expired cookies, duplicates, common errors
3. **[MATCHING.md](MATCHING.md)** — optional YouTube matching (disabled by default)

> Default mode is **Spotify-only**: source Spotify playlists → one target Spotify playlist. YouTube sync is disabled unless you set `youtube_enabled: true`.

---

## 1. What you need

| Thing | Where it comes from |
|---|---|
| Spotify account with the source playlists + a target playlist you own | open.spotify.com |
| Telegram bot + your chat ID (optional, for logs) | @BotFather |
| Google Cloud OAuth client + YouTube playlist (only if you enable YouTube sync) | console.cloud.google.com / youtube.com |

## 2. Prepare the playlists

1. **Sources:** collect the Spotify playlist URLs you want to read from (any count — 20 is fine). You can use playlists you don't own, as long as they're public or followed by your account.
2. **Spotify target:** create a playlist **on the account whose cookie you will copy** (e.g. "Phonk Sync"). The syncer adds the exact Spotify tracks here.
3. **YouTube target (optional):** only needed if you enable YouTube sync. Create a playlist on the YouTube channel you authorize.

Playlist IDs can go into `config.json` (public values) **or** into secrets (recommended if you don't want playlist IDs in a public repo):

- `SPOTIFY_SOURCE_PLAYLISTS` — comma- or newline-separated IDs/URLs
- `SPOTIFY_TARGET_PLAYLIST` — one ID/URL
- `YOUTUBE_TARGET_PLAYLIST` — one ID/URL (`PL...`), only if YouTube sync is enabled

Secrets override `config.json`.

## 3. Create the credentials

Follow **[SECRETS.md](SECRETS.md)**. For the default Spotify-only mode you only need:

- copying the **full Spotify Cookie header** (the `sp_dc` value is extracted automatically),
- the source and target Spotify playlist IDs/URLs,
- optionally, the Telegram bot token and your chat ID.

The Google OAuth / YouTube refresh-token steps are only needed if you enable YouTube sync.

## 4. Add GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add all secrets listed in [SECRETS.md](SECRETS.md). Never commit credentials to the repo — `.gitignore` already blocks `.env`, `client_secret*.json`, and `token*.json`.

## 5. Enable and run

1. Push this repository to GitHub (public is fine — no secrets are in the code).
2. **Actions** tab → enable workflows if prompted.
3. Run it once manually: **Actions → Phonk Sync → Run workflow**.
   - First run: tick `dry_run` to preview matches without writing anything. Dry runs read only the first 200 source tracks (`dry_run_track_limit` in `config.json`).
   - Then run it live. Live syncs read the full source playlists.
4. Automatic scheduled runs are currently **paused**. The workflow only runs when you manually trigger it (`.github/workflows/sync.yml`). To re-enable automatic runs, add a `schedule:` cron trigger back to that workflow.

Each live run commits `data/sync-state.json` back to the repo — that is the database that prevents duplicate adds.

## 6. Verify

- Telegram should receive a start message, per-track ✅/❌ messages, and a summary.
- `Actions → latest run → Run sync` log shows the same summary.
- `python -m phonksync stats` (locally, after pulling) shows per-method tried/won counts.

## 7. Optional tuning (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `youtube_enabled` | `false` | YouTube sync is disabled by default (Spotify-only) |
| `max_tracks_per_run` | `0` | 0 = unlimited; otherwise cap processed tracks per run |
| `dry_run_track_limit` | `200` | dry-run only: max source tracks to read (0 = unlimited) |
| `telegram_per_track` | `true` | list each track inside the single end-of-run message (never one message per track) |
| `retry_unmatched_after_days` | `7` | wait before retrying an unmatched track (YouTube mode) |
| `max_searches_per_run` | `200` | YouTube only: cap on **paid** searches per run (free page searches do not count) |
| `youtube_free_search` | `true` | search youtube.com first: 0 API units (see `Instructions/YOUTUBE-BUDGET.md`) |
| — | — | `phonksync skip [--all-unmatched]` marks songs that are not on YouTube so they are never searched again |
| `youtube_max_searches_per_day` | `80` | YouTube only: cap on searches per UTC day (80 x 100 = 8,000 units) |
| `youtube_max_inserts_per_day` | `40` | YouTube only: cap on playlist adds per UTC day (40 x 50 = 2,000 units) |
| `youtube_daily_quota_units` | `9800` | YouTube only: overall local quota budget |
| `search_results_per_query` | `10` | YouTube only: results fetched per search |
| `duration_tolerance_seconds` | `7` | YouTube only: DURATION/TITLEONLY tolerance |
| `exact_duration_tolerance_seconds` | `3` | YouTube only: DURVER tolerance |
| `require_youtube_match_for_spotify` | `false` | YouTube only: require a YouTube match before adding to Spotify |
| `youtube_region_code` | `US` | YouTube only: search region bias |

## 8. Fixing a wrong or missing YouTube match (optional YouTube mode only)

If you enable YouTube sync and a track matches the wrong video, add a manual mapping to `data/track-overrides.json` and commit:

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
