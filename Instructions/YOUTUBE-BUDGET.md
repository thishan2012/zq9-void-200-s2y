# 🔎 YouTube quota — how to stop running out after 40 tracks

The daily quota is **10,000 units per Google Cloud project**, and one `search`
call costs **100 units**. 80 searches = 8,000 units, so with the old setup a run
stopped after roughly 40 tracks — exactly what you saw:

```
⚠️ YouTube quota reached: Per-run search limit reached (80 searches)
```

## The free tier (new, on by default)

`youtube_free_search = true`

Every search now goes to **YouTube's public results page first** (no API key, no
quota), plus each watch page for title/channel/duration/description — also free.
The matcher verifies those candidates exactly like API results, so a match found
this way costs **0 units**.

```
free search + free metadata → verify → (only if that fails) 100-unit API search
```

Two things follow:

- searches are only *paid* for tracks the free pass could not resolve;
- the free results are cached under a separate `|free` key, so a miss never
  poisons the API cache and the same query is never bought twice.

Turn it off per run with the repo variable `YOUTUBE_FREE_SEARCH=false`, or in
`config.json` (`youtube_free_search`). Politeness gap between page loads:
`youtube_free_search_pause` (default 1.0 s).

Summary now says:

```
🆓 Free YouTube searches: 58 (0 quota), matches: 37
🔎 Searches this run: 6 (mode: api_key)
```

## The honest limits of the free tier

- It scrapes `youtube.com/results` and `youtube.com/watch`. YouTube can rate-limit
  or serve a consent page; when that happens the code just falls through to the
  API search (No crash, no wasted budget).
- The free page returns results in YouTube's order, not the API's; the matcher
  verifies them like any other candidate, so a *wrong* match is still unlikely —
  but expect slightly fewer "top result" style wins.
- Metadata (views/duration) comes from the watch page; if YouTube hides the
  player response the candidate is skipped and the API is used for that query.

## Other levers (unchanged)

| knob | effect |
|---|---|
| `max_searches_per_run` | per-run cap (raise it — free searches no longer count) |
| `youtube_max_searches_per_day` | daily cap across runs; `0` = unlimited |
| more API keys (`YOUTUBE_API_KEY` comma-separated) | +10,000 units per key/day |
| `data/yt-cache.json` (committed) | repeats cost nothing across runs |

Default caps are now deliberately generous, because the free tier absorbs most
lookups: `max_searches_per_run = 200`, `youtube_max_searches_per_day = 0`.
The real guard is the per-key unit budget (`youtube_daily_quota_units`), which
still refuses to exceed Google's 10,000/day.

## Songs that simply are not on YouTube (or you do not want)

Mark them once and the syncer stops looking — no searches, no "unmatched"
noise, no retry clock:

```bash
python -m phonksync skip --show                 # skips + what is still unmatched
python -m phonksync skip --all-unmatched        # "none of these, stop trying"
python -m phonksync skip <spotify-track-id> --reason "not on YouTube"
python -m phonksync skip --clear <spotify-track-id>
```

The summary then reads `🙅 Skipped by choice: 2 (not on YouTube / not wanted …)`
instead of `❌ Unmatched`. Skips live in `data/sync-state.json` (committed), so
they survive between runs.

## "Unmatched" vs "out of budget"

A track that was still being tried when the search budget ran out is **not**
recorded as `unmatched` (that used to start a multi-day "retry later" clock and
hide the track). It is counted separately:

```
⏸ Search budget ran out for 2 track(s) — they were NOT tried properly and are
  not marked unmatched; the next run continues
```
