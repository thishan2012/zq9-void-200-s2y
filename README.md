# 🎧 Phonk Playlist Syncer

Accuracy-first sync from **many Spotify playlists** → **one YouTube playlist + one Spotify playlist**, running automatically on GitHub Actions with Telegram logs.

Reads every track from your source Spotify playlists (20, 30, any number), finds the **exact same recording** on YouTube through a 31-method search pipeline, and adds it to both targets. A JSON database committed to this repo guarantees a song is **never added twice**.

> All repository content is in English by design (public repo).

## ✨ Features

- **Many sources → two targets.** Any number of Spotify source playlists; one YouTube target + one Spotify target.
- **Cookie-based Spotify auth.** Paste the whole browser `Cookie` header into a secret — no Spotify app registration needed.
- **YouTube Data API (OAuth).** Uses your own Google Cloud OAuth client JSON + refresh token (read & write).
- **Accuracy first.** 31 search methods + manual overrides, with 3 safety checks under every step:
  1. `same_song_title()` — blocks a different song that shares only one word
  2. `version_ok()` — never swaps `slowed` ↔ original ↔ `sped up` ↔ `remix`
  3. `artist_ok()` — blocks the same title by a different artist
- **Telegram logs.** Per-track ✅/❌ messages and a run summary.
- **No duplicates.** `data/sync-state.json` is committed back to the repo after every run; ISRC dedup catches the same recording under different Spotify IDs.
- **Quota-safe.** Local YouTube quota budget + persistent search cache; runs stop cleanly and resume where they left off.
- **Manual overrides.** Pin any track to an exact video in `data/track-overrides.json`.

## 🚀 Quick start

Full setup guide: **[Instructions/README.md](Instructions/README.md)**
Secrets guide (required reading): **[Instructions/SECRETS.md](Instructions/SECRETS.md)**

1. Create a Google Cloud OAuth client (YouTube Data API v3) and run the one-time auth:
   ```bash
   pip install -r requirements.txt
   python -m phonksync youtube-auth --client-json client_secret.json
   ```
2. Add the repository secrets (see [Instructions/SECRETS.md](Instructions/SECRETS.md)):
   `SPOTIFY_COOKIE`, `YT_CLIENT_JSON`, `YT_REFRESH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `SPOTIFY_SOURCE_PLAYLISTS`, `SPOTIFY_TARGET_PLAYLIST`, `YOUTUBE_TARGET_PLAYLIST`.
3. Push to GitHub. The **Phonk Sync** workflow runs every 6 hours (and on demand via *Actions → Phonk Sync → Run workflow*).
4. Watch Telegram. ✅

## 🧪 Local usage

```bash
python -m phonksync check --live      # validate config + credentials
python -m phonksync sync --dry-run    # match only, write nothing
python -m phonksync sync --max-tracks 5
python -m phonksync stats             # method win statistics
```

Local runs can use a `.env` file (git-ignored) instead of exported environment variables.

## 🔎 How matching works

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
.github/workflows/    # scheduled sync + tests
tests/                # offline test suite (fake APIs, no network)
```

## ⚠️ Notes

- YouTube API quota is 10,000 units/day by default (1 search = 100 units). A large first sync may span several scheduled runs — state is persisted, nothing is re-added. See [Instructions/TROUBLESHOOTING.md](Instructions/TROUBLESHOOTING.md).
- The Spotify cookie is a session credential: treat it like a password, keep it only in GitHub Secrets, and refresh it if it expires.
- Intended for personal use with your own accounts and playlists.
