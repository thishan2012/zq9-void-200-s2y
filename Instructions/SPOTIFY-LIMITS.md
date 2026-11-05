# 🚦 Spotify rate limits — how this repo stays out of them

Spotify's Web API limit is a **rolling window** and it is keyed on the
**access token / account / app client id — not on your IP**. A GitHub runner gets a
fresh IP every run, yet a block follows the cookie, which is how you can tell.

When you trip it you get `429` plus `Retry-After`. That number can be seconds…
or **hours** (5h, 12h, 21h and ~24h have all been reported after retry storms).
Retrying through it is what makes it last days. So this repo never does that.

## The simple knobs (start here)

```text
REQUEST_DELAY_SECONDS = 2     # gap before EVERY Spotify call (config.json: spotify_request_delay_seconds)
```

That one number is the cheapest protection: 2 seconds between calls is slow enough
to stay out of the window, and a 2,689-track playlist still finishes in a few
minutes thanks to the caches below.

```
429 → backoff_seconds(Retry-After) → retry (up to 6x)
```
Every retry path uses `backoff_seconds()` (`phonksync/spotify.py`):
`Retry-After` (seconds *or* HTTP-date) → exponential `2**attempt` → +jitter,
capped. A 429 therefore **never fails the run**; it slows it down.

## The four defences (all automatic)

| # | Defence | What it saves |
|---|---|---|
| 1 | **Paced pages** — 50 tracks/page, `REQUEST_DELAY_SECONDS` (2s) between calls, `Retry-After` honoured | avoids tripping the limit at all |
| 2 | **Source cache** — 1 `snapshot_id` call instead of N pages when a playlist is unchanged | ~1 request instead of 54 per 2,689-track playlist |
| 3 | **Queue writes** — tracks are parked in the player queue and saved to the playlist in batches | ~1 call per track instead of 1 insert per track, and Spotify de-dupes on save |
| 4 | **Cooldown memory** — a long `Retry-After` is stored in `data/sync-state.json`, live runs skip until it expires | no retry storm, no 5-day block |

Plus two manual escape hatches:

- **Spare accounts** — `SPOTIFY_COOKIE_2`, `SPOTIFY_COOKIE_3`, `SPOTIFY_COOKIE_4`
  (different Spotify accounts). When one cookie gets a long block the client
  switches to the next one and keeps going. Spotify limits per account, so this
  is the clean way to keep working.
- **Manual bucket** — if Spotify is blocked while *writing*, tracks are parked in
  `data/manual-queue.txt` (and `state.spotify.manual`). Nothing is lost:
  ```bash
  python -m phonksync manual           # show the bucket
  python -m phonksync manual --apply   # add them now
  ```

## Queue mode (default)

```json
{ "spotify_write_mode": "queue", "spotify_queue_flush_size": 40 }
```

Each track: `POST /me/player/queue?uri=…` (cheap) — every 40 tracks the queue is
saved with **one** `POST /playlists/{id}/tracks`. If the player API is not
available (no active device, account restriction), the run logs it once and
falls back to normal adds for the rest of the run.

Prefer the old behaviour? `"spotify_write_mode": "direct"`.

## One bad track never kills the run

- a write error → that track is skipped, the rest continue
  (`⏭ Skipped …` in the report; it is retried on a later run)
- a short 429 → waited out and retried inline
- a *long* 429 (block) → the run stops cleanly and saves a **resume point**, so the
  next run continues from the track after the skipped one instead of the top

## Checking state

```bash
python -m phonksync cooldown          # is a Spotify cooldown active?
python -m phonksync cooldown --clear  # override it
python -m phonksync stats             # what has been synced so far
```

## If you get blocked anyway

1. Read `Retry-After` — that is the real answer; the run already recorded it.
2. Add a spare account cookie (`SPOTIFY_COOKIE_2`) to continue immediately.
3. Small batches (`max_tracks=50`) plus the source cache mean the next run costs
   a handful of requests, not hundreds.

> No IP is ever "banned" — Spotify only lengthens the cooldown if you keep
> knocking. Do not loop retries; that is the one thing that turns an hour into a
> week.
