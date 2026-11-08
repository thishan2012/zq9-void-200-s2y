# 🧬 One song, one version

Source playlists overlap. Playlist 1 has **`Song - Slowed`**, playlist 2 has
**`Song - Ultra Slowed`**. Same song, two versions — the target playlist should
hold it **once**.

**Rule: the first version found wins.** The later version is skipped, remembered,
and never searched for again.

## What counts as "the same song"

Two things must both match:

1. **The title with the whole version phrase removed** — including the
   intensifier in front of it.
2. **The primary artist.**

So these are all one song:

| Title A | Title B |
|---|---|
| `MONTAGEM MANADO - Slowed` | `MONTAGEM MANADO - Ultra Slowed` |
| `SHAKA - Slowed` | `SHAKA - Super Slowed` |
| `FUNK MISTERIO - Slowed + Reverb` | `FUNK MISTERIO - Slowed` |
| `MALEMO - Sped Up` | `MALEMO - Ultra Sped Up` |
| `MONTAGEM BREAKDOWN (Slowed & Reverb)` | `MONTAGEM BREAKDOWN` |
| `Song - Slowed and Reverb` | `Song - Slowed` |
| `Song (feat. X) - Slowed` | `Song - Slowed` |

Recognised intensifiers: `ultra`, `super`, `mega`, `hyper`, `extra`, `extremely`,
`very`, `slightly`, `semi`, `hard`, `deep`, `more`, `most`, `really`.

These stay **different** songs:

- Same title, **different artist**
- `MONTAGEM MANADO` vs `MONTAGEM MANADO 2`
- `Me and You` vs `Me` (a real "and" in a title is never stripped)

## This does NOT loosen the matcher

Grouping versions is a **playlist decision**, not a matching decision.
`version_ok()` still compares version signatures exactly, so a Spotify
`Slowed` track is never matched to a YouTube `Ultra Slowed` video. The family key
lives in `normalize.song_family()` and is used only for the duplicate check.

## Where the decision is recorded

- `data/sync-state.json` → `version_duplicates` — the skipped track, and which
  track was kept instead.
- **`data/version-duplicates.txt`** — the same thing, readable in the browser.
  Rewritten after every run, so it clears itself when the bucket is empty.

```text
SKIPPED  Nulteex - MONTAGEM MANADO - Ultra Slowed
         kept instead: MONTAGEM MANADO - Slowed
         https://open.spotify.com/track/…
```

The check runs **before any YouTube search**, so a skipped version costs no quota.

## Changing your mind

**Actions → Phonk Sync → Run workflow → `reset`:**

| `reset` | effect |
|---|---|
| `versions` | forget the decisions; those versions are considered again |
| `synced, versions` | also forget which song claimed the family first |
| `all` | start completely over |

To turn the check off entirely, set `"dedupe_versions": false` in `config.json`.

## Adding more source playlists

Nothing to change in the code. Edit the **`SPOTIFY_SOURCE_PLAYLISTS`** secret
(Settings → Secrets and variables → Actions) and paste every playlist link or ID,
separated by commas or newlines. The count is arbitrary. Overlaps between them are
handled by this check plus the existing ISRC check.

---

# ⚖️ Keeping both playlists the same size

Spotify has no daily write budget; YouTube does (50 quota units per add, out of
9800/day). Writing Spotify first meant the song that used up the last YouTube
unit still landed on Spotify — so the playlists drifted apart.

**Now YouTube is written first.** If YouTube refuses the song because the day's
budget is gone, Spotify is not touched either, and the run stops. The next run
continues from there. Config: `"spotify_youtube_parity": true`.

One deliberate exception, as chosen: **a song that does not exist on YouTube
still goes to Spotify.** Spotify completeness wins over an exactly equal count,
so the Spotify playlist can end up slightly ahead by the number of tracks
YouTube simply does not have.

YouTube writes being unavailable *entirely* (no OAuth refresh token) is a setup
problem, not a budget stop, so Spotify still proceeds in that case.
