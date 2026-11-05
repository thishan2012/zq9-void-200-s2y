# 🎧 Phonk Playlist Syncer

Sync from **many Spotify playlists** → **one Spotify playlist**, running automatically on GitHub Actions with Telegram logs.

Reads every track from your source Spotify playlists (20, 30, any number) and copies the exact same tracks into your target Spotify playlist. A JSON database committed to this repo guarantees a song is **never added twice**.

> YouTube sync has been removed from the default setup. The syncer now runs **Spotify-only** unless you explicitly enable YouTube.
> All repository content is in English by design (public repo).

## ✨ Features

- **Many sources → one Spotify target.** Any number of Spotify source playlists; one Spotify target playlist.
- **Cookie-based Spotify auth.** Paste the whole browser `Cookie` header into a secret — no Spotify app registration needed.
- **Telegram logs.** Per-track ✅ messages and a run summary.
- **No duplicates.** `data/sync-state.json` is committed back to the repo after every run; ISRC dedup catches the same recording under different Spotify IDs.
- **Dry-run preview.** Reads only the first 200 source tracks by default and writes nothing.
- **Optional YouTube sync (disabled).** The old YouTube matching pipeline is still in the codebase but is off by default (`youtube_enabled: false`).

## 🚀 Quick start

Full setup guide: **[Instructions/README.md](Instructions/README.md)**
Secrets guide (required reading): **[Instructions/SECRETS.md](Instructions/SECRETS.md)**

1. Create a Spotify target playlist you own (e.g. `Phonk Sync`).
2. Add the repository secrets (see [Instructions/SECRETS.md](Instructions/SECRETS.md)):
   `SPOTIFY_COOKIE`, `SPOTIFY_SOURCE_PLAYLISTS`, `SPOTIFY_TARGET_PLAYLIST`.
   Optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` for logs.
   YouTube secrets are **not needed** for the default Spotify-only mode.
3. Push to GitHub. The **Phonk Sync** workflow is currently **paused for automatic runs**; it only runs when you manually trigger it via *Actions → Phonk Sync → Run workflow*. A manual `dry_run` previews only the first 200 source tracks.
4. Watch Telegram. ✅

## 🧪 Local usage

```bash
python -m phonksync check --live      # validate config + credentials
python -m phonksync sync --dry-run    # preview only, write nothing, first 200 source tracks
python -m phonksync sync --max-tracks 5
python -m phonksync stats             # sync statistics
```

Local runs can use a `.env` file (git-ignored) instead of exported environment variables.

## 🔎 Optional YouTube matching (disabled by default)

YouTube sync is off by default (`youtube_enabled: false`). If you enable it, the old accuracy-first pipeline is used.

Pipeline order (first verified win stops the search):

```
MANUAL → STRICT → LOOSE → DETAIL → REVERSED → SINGLE → CHANNEL → TOPIC →
SUFFIX → COUNTRY → FEAT → DURATION → ARTISTS → SHORT → ARTISTLESS → EXACT →
WORDSET → SLOWED → UPLOADER → LINKED → FIELD → VARIANTS → DURVER →
CLEANARTIST → TITLEONLY → SONGTAG → ISRC → DESCTEXT → BROKEN → ALBUM →
PART → POPULAR → TOPRESULT
```

- **29 numbered fallback steps + 2 classic methods = 31 search methods**, plus the `DETAIL` metadata search and `MANUAL` overrides (33 pipeline entries).
- Steps whose condition doesn't hold are recorded as **never tried**; steps that ran but didn't win are kept forever (no pruning).
- `ISRC` and `LINKED` trust exact recording identity (ISRC code / Spotify track link in the video description). `MANUAL` trusts your pinned mapping.
- Every other step must pass all three safety checks.

Full method table: **[Instructions/MATCHING.md](Instructions/MATCHING.md)**

## 📁 Repository layout

```
phonksync/            # the syncer package
  matcher.py          # 31-method pipeline + 3 safety checks
  engine.py           # orchestration, dedup, retry policy
  spotify.py          # cookie-auth Spotify client
  youtube.py          # OAuth YouTube Data API client + quota budget
  db.py               # JSON state database (committed to repo)
  telegram.py         # Telegram bot logger
data/
  sync-state.json     # the database — what was added, by which method
  track-overrides.json# your manual mappings (MANUAL method)
Instructions/         # setup, secrets, matching, troubleshooting docs
.github/workflows/    # manual sync (paused schedule) + tests
tests/                # offline test suite (fake APIs, no network)
```

## ⚠️ Notes

- Default mode is Spotify-only, so no YouTube API quota is used. If you enable YouTube sync, the YouTube Data API daily quota (10,000 units/day; 1 search = 100 units) applies. See [Instructions/TROUBLESHOOTING.md](Instructions/TROUBLESHOOTING.md).
- The Spotify cookie is a session credential: treat it like a password, keep it only in GitHub Secrets, and refresh it if it expires.
- Intended for personal use with your own accounts and playlists.
