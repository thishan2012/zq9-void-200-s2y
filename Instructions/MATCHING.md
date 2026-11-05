# 🔎 Matching Pipeline (optional YouTube mode)

> This pipeline is only used when YouTube sync is enabled (`youtube_enabled: true`). The default Spotify-only mode copies exact Spotify track IDs and does not search YouTube.

Accuracy is the top priority: a wrong song is worse than no song. The pipeline
tries methods in a fixed order and **stops at the first verified win**.

## The three safety checks (run under every step)

| Check | Blocks |
|---|---|
| `same_song_title()` | A different song that only shares one word. Titles are normalized (accents folded, case folded, marketing noise like `Official Video`/`Lyrics`/`HD` removed, feat clauses and version tags stripped). Fuzzy matches must reach the step's similarity floor; multi-word titles sharing ≤1 word are rejected outright. |
| `version_ok()` | Version swaps. The version signature (`slowed`, `reverb`, `sped_up`, `nightcore`, `remix`, `mashup`, `edit`, `bass_boosted`, `8d`, `cover`, `live`, `acoustic`, `instrumental`, `extended`, `lofi`, `tiktok`) of source title+album must **exactly equal** the candidate's. Original never matches slowed/sped-up/remix and vice versa. |
| `artist_ok()` | Same title by a different artist. Artist evidence comes from `Artist - Title` segments, `Artist:` description lines, and `Artist - Topic` channels (strong), plus plain uploader names (weak). Strong conflicting evidence always blocks; a promo channel name alone never blocks. |

Exceptions (authoritative identity, documented): `MANUAL` (your pinned mapping),
`ISRC` (exact recording code in the description), `LINKED` (exact
`open.spotify.com/track/<id>` link in the description, still version-checked).

## A. Classic (run first)

| Method | What it does |
|---|---|
| `STRICT` | Searches `artist + title`; accepts only a 100% normalized title match with no artist conflict. The main workhorse. |
| `LOOSE` | On the same results: title ≥85% similar, same version/artist checks. |
| `DETAIL` | Builds artist/title metadata from `Artist:` / `Song:` lines in video descriptions and searches with it. |
| `MANUAL` | Your mapping in `data/track-overrides.json` — no search at all. Runs before everything. |

## B. The 29 fallback steps (in order)

| # | Code | What it does |
|---|---|---|
| 1 | `REVERSED` | Detects `Song - Artist` (reversed) titles and re-searches `title artist`. |
| 2 | `SINGLE` | Collab: searches with only the first artist's name. |
| 3 | `CHANNEL` | Treats the YouTube channel name as the artist; searches the title alone. |
| 4 | `TOPIC` | Finds `Artist - Topic` auto-generated channels and recovers the real artist name. |
| 5 | `SUFFIX` | Strips marketing words (`Official Video`, `Lyrics`, ...) and re-searches. |
| 6 | `COUNTRY` | Accent/script differences (`SALÃO` ↔ `SALAO`, etc.) via Unicode folding. |
| 7 | `FEAT` | Promotes `(feat. X)` to the main artist and searches `X + title`. |
| 8 | `DURATION` | Several same-name versions in the pool → picks the one whose video duration is closest (±7s). |
| 9 | `ARTISTS` | Tries every credited artist separately. |
| 10 | `SHORT` | Very short names (`LUZ`, `AWE`): exact title + positively confirmed artist required. |
| 11 | `ARTISTLESS` | Title-only search when artist metadata is unreliable on both sides. |
| 12 | `EXACT` | Version words removed; token sequences must be identical. |
| 13 | `WORDSET` | Same words, different order. |
| 14 | `SLOWED` | Only when the source itself is slowed: searches `<song> slowed`, accepts slowed only. |
| 15 | `UPLOADER` | Searches using the top result's uploader name + title. |
| 16 | `LINKED` | Description contains `open.spotify.com/track/<this track>` → direct match, no name search. |
| 17 | `FIELD` | Field-style query `track:"SHAKA" artist:"Nulteex"` so name and artist can't mix. |
| 18 | `VARIANTS` | Three query shapes at once (`artist title`, `title artist`, `artist - title`). |
| 19 | `DURVER` | Slightly different title + duration within ±3s + version match. |
| 20 | `CLEANARTIST` | Cleaned title + primary artist. |
| 21 | `TITLEONLY` | Title-only search; requires both duration match and every title word present in the video title. |
| 22 | `SONGTAG` | Description/hashtags: `Song:`, `Artist:`, ♪ lines, `#MontagemMiau`. |
| 23 | `ISRC` | Description contains `ISRC: BRABC…` matching the source → exact recording; name/artist checks bypassed. |
| 24 | `DESCTEXT` | An unlabeled description line used as the song name (0.92 similarity floor). |
| 25 | `BROKEN` | `DESTELLO - HDN` style: right side is the artist, left side the song. |
| 26 | `ALBUM` | Uses the album name (`X - Single`) to disambiguate versions. |
| 27 | `PART` | `Pt. 1` / `Pt. 2` split titles; part numbers must agree. |
| 28 | `POPULAR` | Multiple verified same-title matches → highest view count; a tie means no pick. |
| 29 | `TOPRESULT` | Last resort: the very first search result, still verified by all 3 checks. |

## C. Statistics semantics

- **Never tried** — the step's precondition didn't hold (e.g. `ISRC` with no ISRC in any description, `SLOWED` when the source isn't slowed, `SINGLE` for a solo track).
- **Tried but never won** — kept forever; steps are never pruned.
- Per-method `tried` / `won` counters accumulate in `data/sync-state.json` and are shown by `python -m phonksync stats`.

## D. Quota behavior

- One shared candidate pool per track; a query is issued at most once per track.
- Queries and video metadata are cached for 14 days (`.cache/yt-cache.json`, persisted via GitHub Actions cache).
- A local budget (default 9500 units/day, 80 searches/run) stops the run cleanly; the next scheduled run continues where it stopped.
