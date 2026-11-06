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
429 → backoff_seconds(Retry-After) → retry (up to 6x) → this run stops using the Web API
(nothing is stored; the next run starts immediately on the web-player route), run exits 0
```
Every retry path uses `backoff_seconds()` (`phonksync/spotify.py`):
`Retry-After` (seconds *or* HTTP-date) → exponential `2**attempt` → +jitter,
capped. A 429 therefore **never fails the run**; it slows it down.

### The token endpoint is throttled too

The *first* call of every run is the web-player token request, and Spotify
rate-limits that too. It is now handled like any other 429: short windows are
waited out, long ones park the run for 30 minutes, and a throttled token is
**never** reported as a broken cookie. The job also ends with exit code `0` when
the only reason it stopped was a cooldown — a red ✗ in Actions meant "throttled",
not "broken".

## The cache is the real budget saver

Two caches, both on disk and now **committed with the state** so they survive
between GitHub Actions runs (`.cache/` never did — that directory is git-ignored):

| file | what it holds |
|---|---|
| `data/source-cache.json` | the last read of every source playlist (tracks + snapshot) |
| `data/yt-cache.json` | YouTube searches and video metadata (extras keep the quota) |

```text
spotify_source_cache_ttl_hours = 12     # "unchanged playlist → reuse, 1 snapshot call"
spotify_cache_allow_stale      = true   # replay an old read instead of calling Spotify
spotify_cache_max_age_days     = 30     # how old a replayed read may be
```

A run that hits 429 now walks: web player → public page → **fresh cache** →
**stale cache (≤30 days, zero calls)** → collection. So a throttled day costs
nothing: the same 164 tracks are replayed from disk and the YouTube side carries
on. The summary says so: `🗄 Stale cache reads replayed: 1`.

Fill it deliberately when Spotify is answering:

```bash
python -m phonksync cache-fill     # read every source once, store it, no writes
python -m phonksync cache --search # age/track count of everything stored
python -m phonksync cache --clear  # start over
```

In Actions: **Run workflow → `fill_cache: true`** does just that (no sync, no
writes), and every later run restores the file from the checkout.

## Routing through your own CORS proxy (clean egress IP)

A token-level block follows the cookie, so a proxy is not a magic reset. It *is*
useful for two real things: your proxy's egress (Vercel) is not a shared GitHub
runner IP, and the public-page read works from it too.

```bash
export SPOTIFY_PROXY_URL=https://corsly-proxy2.camdvr.org   # or vars/secret in Actions
export SPOTIFY_PROXY_SECRET=...        # only if your proxy checks a header
python -m phonksync proxy-test --playlist <public-playlist-id>
```

* **Query mode** (default): URL becomes `<proxy>?url=<encoded spotify url>` —
  the corsproxy.io / allorigins / most-Vercel-proxy convention.
* **Header mode** (`SPOTIFY_PROXY_SECRET` set): URL becomes
  `<proxy>/<absolute url>` and `x-proxy-secret` is sent, so the proxy can
  forward headers **including `Authorization` and `Cookie`** untouched.
* The first `429` on a request **switches route** (proxy ⇄ direct) before any
  waiting, because a different egress sometimes gets a different answer.
* When the proxy fails (network error/5xx) the request falls back to direct —
  a broken proxy never breaks a run.
* `Authorization`, `Cookie` and query params are preserved; the proxy must not
  rewrite headers in query mode (it cannot: it only sees the URL).

In GitHub Actions set the repository **variable** `SPOTIFY_PROXY_URL` (or the
secret of the same name) and the run picks it up automatically; `config.json`
also accepts `spotify_proxy_url`.

## The real answer: the Web API is not the only door

Spotify's own web player does **not** read playlists from `api.spotify.com` — that
API answers `429` to web-player tokens (this is well documented by several
projects in 2026). The player calls the **Partner GraphQL endpoint**:

```text
POST https://api-partner.spotify.com/pathfinder/v2/query
     operationName: fetchPlaylist
     variables: {uri: spotify:playlist:<id>, offset, limit, ...}
     headers: client-token: <anonymous client token>   ← no login needed
```

`clienttoken.spotify.com/v1/clienttoken` hands out that token to anyone, so a
throttled Web API (and even a stale cookie) no longer stops a read:

1. `fetchPlaylist` / `fetchPlaylistContents` (the web player's own operation),
   paged 250 at a time.
2. The public HTML page (kept as a second option).
3. Cached read → saved collection.

The persisted-query hash is read from the live web-player bundle when Spotify
rotates it (a known-good hash is built in as a fallback). Expected log line once the web player route is in use:

```text
🌐 Read 1G84… from the web player endpoint — 164 tracks (Web API not used)
```

and **no** "Could not read … from the public page" warning next to it: the public
page is only attempted, and only reported, when the web player route produced
nothing. If you ever need to pin a rotated hash yourself:

```bash
SPOTIFY_HASH_ADDTOPLAYLIST=<64 hex chars>   # or SPOTIFY_HASH_FETCHPLAYLIST
```

Check what works from
your runner at any time:

```bash
python -m phonksync diag            # token / client token / pathfinder / page
```

**Writes too.** `addToPlaylist` is a pathfinder mutation
(`playlistUri`, `playlistItemUris`, `newPosition {moveType: BOTTOM_OF_PLAYLIST}`).
So once Spotify has throttled the Web API, adds go through the web player instead
of parking the track. Force the backend from the start with:

```bash
SPOTIFY_BACKEND=web-player          # or the `backend` input in the workflow
```

The run summary names the backend it used (`🔀 Spotify backend: web player
(api-partner)`), and the state file remembers it: a proven backend is reused, so
the first 429 of a run is the only one you pay for.

If the Web API is already parked, the run now reads with pathfinder, parks the
unwritten tracks in `data/manual-queue.txt`, finishes normally and exits `0`
instead of dying on the first 429.

Note: these endpoints are unofficial and can change; that is why every layer
falls back to the next one instead of failing the run.

## When the API is throttled anyway — read the public page

A **public** playlist can be read without any token, so a 429 on the API no
longer stops a run:

1. **Public page fallback (automatic).** `GET https://open.spotify.com/playlist/<id>`
   with the web player's render header returns the whole track list server side.
   The syncer parses it and uses it exactly like an API read
   (`🌐 Read … from the public page — no API quota used`).
2. **Collection (manual, always works).** Capture once and replay forever:

   ```bash
   python -m phonksync collect --from-web <playlist-url>     # or:
   python -m phonksync collect <playlist-url> < page.html    # Ctrl+U → save → pipe
   python -m phonksync collect --show
   ```

   Saved in `data/sync-state.json` (`collection.playlists`) and used when both the
   API and the public page fail. This is the "copy the playlist once" trick, minus
   the copy/paste.
3. **Source cache** — last good read, reused for the rest of the run.

Only *writes* need the API. If writes are throttled the track goes to the manual
bucket (`data/manual-queue.txt`, `phonksync manual --apply`).

> Scope note: this is a plain public-page read (no auth bypass, no private data).
> If a playlist is private the fallback cannot read it — use a spare account
> cookie (`SPOTIFY_COOKIE_2`) or wait out the window.

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
