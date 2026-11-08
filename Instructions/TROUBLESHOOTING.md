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

## Songs reach YouTube, but Spotify says "Parked for the next run" / "Spotify asked to slow down"

**This was a bug, not your secrets.** Spotify has two independent write doors:

| Door | Endpoint | Throttled by the 429? |
|---|---|---|
| Web API | `api.spotify.com/v1/playlists/<id>/tracks` | ✅ yes — this is what answers 429 |
| Web player | `api-partner.spotify.com` → `addToPlaylist` | ❌ no — separate endpoint, separate budget |

A 429 on the **Web API** used to park *every* remaining Spotify write for the run
without ever trying the web-player door — even though the write probe
(`spotify-write-test`) proved that door lands the track. That is why a run could
report YouTube 10/10 and Spotify 4/10 with six tracks sitting in
`data/manual-queue.txt`.

Now a Web API cooldown **writes through the web player instead of parking**, and
the parked backlog is retried on that route in the same run. The summary says so:

```text
🌐 Added 6 track(s) through the web player while the Spotify Web API was cooling down
```

A track is only parked when **both** doors refuse — and a parked track is never
recorded as added, so nothing is lost:

```bash
python -m phonksync manual           # what is still parked, and why
python -m phonksync manual --apply   # add them now
```

In Actions the same backlog is retried automatically on the next
**Phonk Sync → Run workflow**. You do not need to change any secret for this.

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
2. Search YouTube manually; if the video exists, pin it in `data/track-overrides.json` (see [REPO.md](../REPO.md) §8). The `MANUAL` method bypasses all search.
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

## Telegram spam / too many messages

Every run sends **exactly one message**. Everything the run has to say — the
source reads, rate-limit notes, and the `✅ Added` list — is collected and sent
once, at the end:

```
━━ 📊 Sync run finished ━━
🆕 Added this run: 7 (YouTube inserts: 7)
🎵 Sources: 3 playlist(s), 240 unique tracks
✅ Added (7):
  • Nulteex — MONTAGEM MIAU [CHANNEL] · YT
  ...
❌ Failed / unmatched (2):
  • … — no YouTube match (6 method(s) tried)
Already in the playlist (untouched): 233 · Deferred: 0 · Duplicates: 0 · Skipped by choice: 0
```

- a song that is **already in the state DB** is not in the list — re-reads,
  re-runs and pending flushes never repeat it;
- the track list is capped at 60 lines (`… and more`); the counts stay exact;
- only a run with more than ~3900 characters is split (Telegram's hard limit),
  and then the pieces are consecutive, not one per track.

## A run takes 20–60 minutes

The slow part is never the matching — it is **Spotify's Web API**: every call is
paced `spotify_request_delay_seconds` (default **2s**) apart, and a large source
playlist needs one call per page. 30 minutes of run = mostly sleeping.

Two budgets now cap that, so pre-work can not eat the whole run:

| config | default | meaning |
|---|---|---|
| `spotify_max_requests_per_run` | `40` | after this many Web API calls, the remaining sources are read through the **web-player route** (no pacing, no rate-limit budget) or from the source cache |
| `startup_time_budget_seconds` | `420` | reads + target-playlist load may not take longer than 7 minutes; the run then continues with what it has |

Make it faster still:

- keep the source playlists cached: a repeat run reuses the read when the
  playlist has not changed (1 cheap call instead of N pages);
- raise `spotify_request_delay_seconds` only if you see 429s — lowering it makes
  the pace faster but risks a block;
- `max_tracks_per_run` (default `0` = no limit) caps how much work one run does.

The summary reports what happened, so a slow run is visible:
`🎵 3 playlist(s), 812 tracks read · 720 were already synced`.

## "I set 30 tracks, but it says 30 / it still hit a Spotify 429"

**The number.** `max_tracks` (workflow input) was only applied to live runs; a
dry run always previewed `dry_run_track_limit` (default 200). Now:

- the dry-run limit is `min(dry_run_track_limit, max_tracks_per_run)` when you
  passed `max_tracks`;
- the summary says which one won: `🧪 DRY RUN — previewed 30 track(s) (your limit: 30)`.

**The 429.** A dry run still *reads* Spotify (one call per ~100 tracks plus the
target playlist), and every call is paced 2s apart. A 429 ends Web API spending
for the rest of the run — the remaining sources are read through the web-player
route instead of retrying into a growing block.

**A long block is never retried.** A `Retry-After` of hours is an answer, not an
obstacle — the run stops spending Web API calls immediately instead of sleeping
(one real run waited 45+13+45+14 = 117s for a 24-hour block). Short values that
keep flapping back stop after the *second* answer.

**No cooldown is ever stored, and no run skips itself.** A 429 only affects the
run it happened in:

```
⚠️ Spotify asked to slow down until 2026-11-06 04:27 UTC — this run only, nothing is stored
⏩ Spotify asked to slow down (25m left) — reading through the web-player route, no Web API calls this run
```

The second line is what a later run prints if it starts while the old window is
still open: it runs anyway, it just avoids the Web API for that run.

## A run is slower than "2s per request" — where do the minutes go?

Read the timing lines in the summary:

```
🆓 Free YouTube: 10 search(es) + 47 video pages (0 quota), matches: 5
⏱ Took 4m 12s for 5 track(s) (50s per track)
```

A track that no method can match costs **up to 7 searches** (the 31 steps reuse
the same candidate pool, but each *new* query is a new page load). Old builds
also paused a full second after every search plus half a second after **each**
watch page, and re-downloaded pages a later step had already seen — that is
where "5 songs = 5 minutes" came from.

Now:

* the gap after a page counts the time the page itself took (`.elapsed`), so a
  900ms page no longer waits another full second;
* watch pages are paused 0.25s and cached for the rest of the process — a page
  is downloaded once, not once per step;
* all free page loads share one keep-alive connection (no TLS handshake each).

If you still want it faster, lower `search_results_per_query` (10 → 5 halves
the page fetches) or `youtube_free_search_pause` (1.0 → 0.3). Both are also
workflow inputs now: **Run workflow** → `results_per_query` / `free_search_pause`
(empty = `config.json`).

`🐌 Slowest: … 41s/25 methods` names the tracks that ate the minutes.

## Where is the log?

Every run keeps two copies of itself:

| where | what |
|---|---|
| **Actions → run → job summary** | the exact Telegram text, inputs, and the wall clock (opens on a phone, no scrolling) |
| `sync-log-<run_number>` artifact | the full `run.log` (INFO, every decision), 14 days |
| step log / `gh run view <id> --log` | the same, streamed live |

So a run never has to be diagnosed from the Telegram message alone. If a run
looks wrong, open the job summary first, then the artifact.

If you see that often, lower `spotify_max_requests_per_run` (default 40) — a
preview of 30 tracks then needs ~2 calls, not a page-turn loop.

## `YT_CLIENT_SECRET` is set — why did it say it is missing?

Three real causes, and the log now names the one you have:

| symptom in the log | cause | fix |
|---|---|---|
| `YT_CLIENT_SECRET exists but its value is empty` | the secret was created but nothing was pasted into it (the GitHub list shows a name + a date either way) | open it → **Update** → paste |
| `… its value is a placeholder` | `none` / `todo` / `changeme` / `xxxxxxxx` | paste the real value |
| the id and the secret come from **different** OAuth clients | `invalid_client` on every exchange | use the id **and** secret of the same client |

And the one that used to be silent: when `YT_CLIENT_JSON` (or
`YOUTUBE_OAUTH_TOKEN_JSON`) is present, its client used to *override*
`YT_CLIENT_ID`/`YT_CLIENT_SECRET` completely — so a perfectly good secret was
never tried. Now the JSON client is tried first and **then** the pair, and the
run prints which client actually worked:

```
✍️ YouTube writes: OAuth via client 1234-…apps.googleusercontent.com (the token works with this client)
```

Proof that the current state is fine, without guessing:

```bash
python -m phonksync check --live            # audits names, then really exchanges the token
python -m phonksync youtube-auth --check-token   # only the token → which client accepts it
```

## Which secrets do I actually need? (`check` prints the list)

Every run now starts with a names-only audit — no value is ever printed:

```
🔑 Secrets found in this run (names only — values are never printed)
   ✅ SPOTIFY_COOKIE             [required    ] Spotify login (sp_dc) — reads the playlists
   ✅ YT_REFRESH_TOKEN           [required    ] YouTube writes (1//…, never expires)
   ✅ YT_CLIENT_ID               [needs secret] the OAuth client that issued YT_REFRESH_TOKEN
   ⚠️ PROXY_URL                  [typo/rename ] NOT READ by the bot …
   ✍️  YouTube writes will use: OAuth refresh token
```

Rules it enforces:

| situation | result |
|---|---|
| `YT_CLIENT_ID` without `YT_CLIENT_SECRET` (or the reverse) | `check` fails — a half pair cannot redeem a token |
| `YT_REFRESH_TOKEN` holding a `ya29.` access token | `check` fails — that shape dies in ~1h |
| both `YT_REFRESH_TOKEN` and `YT_COOKIE` | OAuth wins, the log says so |
| `YT_CLIENT_JSON` **and** id+secret | the JSON wins, the log says so |
| `PROXY_URL` instead of `SPOTIFY_PROXY_URL` | accepted as an alias (the long name still wins) |

Only four things are truly required for a run: `SPOTIFY_COOKIE`,
`SPOTIFY_SOURCE_PLAYLISTS`, `SPOTIFY_TARGET_PLAYLIST`, and the Telegram pair.
Everything YouTube is optional until you want writes, and even then a refresh
token is a usable path on its own — once the client secret is there too.

## I only have `YT_REFRESH_TOKEN` — do I need the client id too?

**The token alone is enough**, as long as the bot knows the OAuth client that
issued it. A Google refresh token is not a standalone credential: it is only
redeemable by the client that created it. The bot therefore tries the
configured client (`YT_CLIENT_ID` / `YT_CLIENT_SECRET`) and then the clients
that ship with it (`DEFAULT_OAUTH_CLIENTS` in `phonksync/oauth_setup.py`, or the
`YT_KNOWN_CLIENTS` variable — `id:secret|||id:secret`).

Check it in one command, without a run:

```bash
python -m phonksync youtube-auth --check-token 1//…   # which client accepts it
python -m phonksync youtube-auth --show-client        # what this build knows (secret masked)
```

```
✅ Works with client 1234-…apps.googleusercontent.com
   access token ya29.abc…
👉 Only YT_REFRESH_TOKEN is needed — the bot already knows this client.
```

If no client is known, the answer is explicit instead of a confusing 400:

```
❌ No OAuth client is known, so the token cannot be redeemed.
   set YT_CLIENT_ID + YT_CLIENT_SECRET (or YT_KNOWN_CLIENTS / DEFAULT_OAUTH_CLIENTS)
```

Either set those two secrets, or — **once** — run
`python -m phonksync youtube-auth --device` with the client you own and paste
the printed `DEFAULT_OAUTH_CLIENTS = [{"client_id": …, "client_secret": …}]`
into `phonksync/oauth_setup.py`. After that, the token is the only secret anyone
has to touch again.

> ⚠️ Symmetry check: one command proves both directions —
> `--check-token` (my token → which client) and `--show-client` (which clients
> this build can use). Nothing is guessed silently: if the pair does not match,
> the message says so.

## I just added `YT_REFRESH_TOKEN` — was everything lost?

No. While writes were off, every matched video was stored as
`pending_youtube` ("matched, not added yet"), so the first run with a working
credential adds them **without searching again**.

```bash
python -m phonksync pending            # what is waiting (adds nothing)
python -m phonksync pending --add      # write them now: skips what is already there
python -m phonksync youtube-auth --doctor --live   # prove the token works
```

`pending --add` reads the playlist first and skips any video already in it, so
running it twice cannot create a duplicate.

Tracks that never passed the safety checks are kept separately as
**near-misses** (`❓ Near-miss candidates kept for review` in the summary). They
are **never** added automatically — review the list and pin the ones you trust:

```bash
python -m phonksync pending                     # shows the candidates
python -m phonksync manual --track <spotify id> --video <video id>
```

## "anonymous (logged-out) token" right after updating the cookie

Four causes, checked in this order by the code now:

1. **The proxy route.** A CORS/forwarding proxy can drop or truncate the
   `Cookie` header, so Spotify answers logged-out for a cookie that works
   direct. The run now retries **direct** automatically and says so in `diag`:
   ```
   ⚠️  the proxy route answered logged-out; the direct route is what works
       — clear SPOTIFY_PROXY_URL / PROXY_URL
   ```
   The proxy request also sends a plain `sp_dc=<value>` header (a complete
   web-player login cookie); the full browser header is not needed and proxies
   tend to mangle long values.
2. **A stale `SPOTIFY_COOKIE` with a good `SPOTIFY_COOKIE_2`.** The client now
   switches accounts on a logged-out answer instead of raising:
   ```
   ℹ️  cookie #2 works (1 switch(es)) — SPOTIFY_COOKIE itself is stale
   ```
3. **Only one cookie configured.** The error says it:
   `SPOTIFY_COOKIE_2 is empty or was ignored, so a stale SPOTIFY_COOKIE has nothing to fall back to.`
4. **The value is not an `sp_dc`.** Run `phonksync cookie-fix --show` and paste
   only the `sp_dc` value it prints.

## Resetting the state DB (it is the bot's memory)

`python -m phonksync reset` shows what is stored and what each part costs to
lose. Nothing changes without `--yes`, and every reset writes a backup next to
the state file (`data/sync-state.json.bak-<date>`).

| scope | forgets | why you would |
|---|---|---|
| `parked` | the Spotify write backlog (`manual` + `pending`) | start the backlog over |
| `quota` | today's YouTube counters | a used-up day, now | 
| `skipped` | songs you skipped by hand | give them another chance |
| `unmatched` | songs with no confident match | retry them now |
| `candidates` | near-miss candidates kept for review | clear the noise |
| `stats` | method statistics + run counters | clean numbers |
| `diagnostics` | the last recorded write problem | after fixing it |
| `synced` | **every processed track** | full re-match (slow) |
| `all` | everything above | a completely fresh start |

```bash
python -m phonksync reset                    # show the inventory
python -m phonksync reset parked             # dry run: "Nothing was changed"
python -m phonksync reset parked --yes       # clears it, keeps a backup
python -m phonksync reset quota stats --yes
python -m phonksync reset all --yes          # fresh start
```

In GitHub: **Actions → Phonk Sync → Run workflow → `reset` = `parked`** (or any
scope). The reset runs before the sync, appears in the summary, and the pre-reset
copy is committed to the repo.

Resetting `synced` is safe in one important way: the target playlist is read
first, so tracks already in it are **counted, not added again**. It is slow
because every source track is matched again — bound it with `max_tracks`.

## "It works from Tests, not from Phonk Sync" — the six-track clue

Run 28 is the controlled experiment: **the probe wrote three tracks, the sync
parked all six** with `reason: "Spotify asked to slow down"` and no receipt at
all. The playlist never changed, and `diagnostics` stayed `null`.

Four differences between the two entry points explained it:

| | probe (`tests.yml`) | sync (`sync.yml`) |
|---|---|---|
| `SPOTIFY_TOTP_VER` / `SPOTIFY_TOTP_CIPHER` | passed | **not passed** |
| `SPOTIFY_WEB_TOKEN` / `SPOTIFY_ENCRYPTED_TOKEN` | passed | **not passed** |
| `SPOTIFY_PROXY_URL` | repo **variable** only | input `>` repo variable `>` secret |
| what it does | one write, immediately | reads, matches, quota, backlog flush |

The proxy one is the dangerous one: a CORS proxy that drops or truncates the
`Cookie` header makes Spotify answer **logged-out**, which looks exactly like
"slow down" from inside the engine. And the token-endpoint failure was being
reported as a cooldown without ever naming the HTTP answer.

Now:

* `sync.yml` passes the **same four secrets** to the sync step as the probe.
* The proxy comes from the **dispatch input or a proxy secret — never a repository
  variable** (a variable set months ago cannot silently change the route).
* The run summary prints `Spotify proxy: off (direct)` or
  `**on** (a CORS proxy can silently log the cookie out)`.
* The engine's diagnosis line prints the token answer itself:
  `no token: …/api/token (transport, totp v12): HTTP 429; legacy: HTTP 400`,
  with `route`, `proxy`, `cookie #1 of 2 (412 chars)`.

## "It works from Tests, not from Phonk Sync"

Two runs can look identical and write nothing. Check these in this order — all
three are now visible without the job log:

1. **`Commit sync state: skipped`** in the run's step list. The state commit is
   skipped exactly when `dry_run = true`. A dry run prints
   `mode: DRY RUN` in the log and `DRY RUN — nothing will be written` in the
   run summary's **🔎 What this run could write** block.
2. **`SPOTIFY_COOKIE: MISSING (0 chars)`** in that same block. The probe
   (`Tests → spotify_write_probe`) uses the same secret name, so if the probe
   writes and the sync does not, the cookie is not the difference — the *mode*
   or the target playlist is.
3. **`🧾 last write: sent N → landed M`** in the **📋 Spotify backlog** step. If
   that step ran and printed nothing, the run never attempted a Spotify write.

The probe runs `spotify-write-test` directly, which never checks `dry_run` and
never reads the parked backlog: it writes one URI immediately. A sync run has to
match tracks, respect the quota and then flush the backlog — so "probe writes,
sync does not" almost always means the sync run was a dry run or had nothing it
was allowed to write.

## Accepted no-op: the write is answered ✅ and nothing lands

The worst failure this bot has met, and the one that produced weeks of "Spotify
add වෙලා නෑ":

* `addToPlaylist` answers `AddItemsToPlaylistPayload` — no error, no exception,
* the playlist is unchanged, and
* `diagnostics` stays `null`, because nothing was *refused*.

Evidence it is a no-op and not a slow playlist: in one run three URIs of a batch
landed and six did not, and the six were still absent days later.

Two things now make it visible instead of silent:

1. **`🧾 last write: sent N → landed M`** — recorded in `data/sync-state.json`
   (`diagnostics.last_write_receipt`) on every run that writes, printed by
   `python -m phonksync manual`, and raised as a `::warning` annotation on the
   run page when `M = 0`.
2. **The probe says it out loud**: if the playlist size does not change it
   prints `🚨 ACCEPTED NO-OP` and tells you to re-run with `--batch 1`.

The write now sends the **browser's own variables** — `playlistItemUris` *and*
`uris` (the browser sends the same list twice; one of them drives the mutation,
the other the UI update). A payload with only half of them is exactly the shape
that answers ✅ and changes nothing.

If a run still reports `sent 6 → landed 0`, run the probe with `--batch 1`. One
landing and two not landing means the mutation accepts single URIs only for this
account — then the fix is in `_pathfinder_add`, not in the hash.

## The probe: "1 → 4" is the verdict

The probe prints the target playlist's size **before** and **after** the write:

```
2️⃣b before   : ✅ 1 track(s) in the playlist
4️⃣ verify   : 4 track(s) in the playlist before/after: 1 → 4
   landed  : 3/3
```

* `1 → 4` with `landed 3/3` = **the batch shape works**, whatever else the run says.
* `7 → 7` = the write truly did nothing.

Run 128668847251 (`spotify_probe_batch = 3`) proved the batch path **does land**
three tracks in one call — the earlier "one works, nine do not" was the parked
batch of the *sync* run, not the mutation shape.

The report is attached as a run artifact and republished on branch
`spotify-probe`; the push is retried three times because a non-fast-forward used
to be swallowed by `|| true` and the report silently never arrived.

## A single write lands, a batch of ten does not

The browser sends **one** `addItemsToPlaylist` for a whole batch, but a batch can
be refused where a single URI is accepted (it happened: one probe track landed,
nine parked tracks in one call did not). The write path now degrades instead of
failing:

1. one mutation per 100 URIs (the browser's own shape),
2. if that call is refused, **the same tracks are retried one by one**,
3. only then is a track left in the backlog.

Reproduce it exactly whenever you want:

```
Actions → Tests → Run workflow
  spotify_write_probe = true
  spotify_probe_batch = 5        ← five parked tracks in ONE call
```

The probe now prints the playlist **before → after** as well:

```
2️⃣b before   : ✅ 7 track(s) in the playlist
3️⃣ write    : …
4️⃣ verify   : 11 track(s) in the playlist before/after: 7 → 11
   landed  : 4/5
   ✅ 0g3rBLGiHXo9hDEqVaL5sM
   ❌ spotify:track:1RD6UmOALWhLSXAe8GNczs
```

`7 → 7` means the write truly did nothing, whatever the answer said. That number
is the one piece of evidence that cannot lie.

## After a successful probe: do not double-add

The probe adds **one real track** to the target playlist. The state file still
lists it as parked, so the next sync reconciles first:

```
✅ 1 parked track(s) were already in the playlist — counted, not added twice
```

The playlist is the source of truth, never our state file: any parked track that
is already there is marked `spotify_added` and removed from the backlog.

## "Nothing gets added to Spotify" — the two-minute probe

Run **Actions → Tests → Run workflow → spotify_write_probe = true**. Nothing else.
It makes **one** real write to the target playlist and publishes Spotify's own
answer (status, GraphQL error strings, the payload) three ways:

1. the job summary,
2. a commit to the branch **`spotify-probe`**:
   `diagnostics/spotify-write-probe-<run>.md`,
3. `data/sync-state.json` → `diagnostics.last_write_problem`.

Locally the same thing, with your `.env` loaded:

```bash
python -m phonksync spotify-write-test --json
```

It prints exactly this (no cookie or token is ever printed):

```
1️⃣ token    : ✅ 260 chars, account phonkbot
2️⃣ read     : ✅ playlist owner: thisisan; cookie account: phonkbot; collaborative: true
3️⃣ write    : …
── attempt 1: addToPlaylist (b907ad6b088d…)
   variables: {"playlistUri": "spotify:playlist:…", "playlistItemUris": […], "interactionId": null}
   answer   : {"data": …}            ← the whole truth, verbatim
4️⃣ verify   : ❌ the track is NOT in the playlist
```

Read the `answer`:

* `"errors": [...]` → the persisted query or the variables are wrong; the error
  string names it.
* `"data": {}` (or `addItemsToPlaylist: null`) → the mutation ran as a no-op:
  wrong hash for this build, or the account may not write to that playlist.
* `addItemsToPlaylist: {"__typename": "AddItemsToPlaylistPayload"}` but verify
  still says NOT in the playlist → the **write went somewhere else**: compare
  `playlistUri` with `SPOTIFY_TARGET_PLAYLIST` (an empty target reads as 0 tracks).

## Where the web-player mutation details come from

The working values are the ones a real browser session uses (and an older
cookie-based syncer of ours used in production):

| piece | value |
|---|---|
| operation | `addToPlaylist` |
| persisted-query hash | `b907ad6b088d5bf7680fa81198996d58398c2d55dfcdf59fcc959e0be98a418a` |
| variables | `playlistUri`, `playlistItemUris[]`, `newPosition`, **`interactionId`** |
| success check | `data.addItemsToPlaylist.__typename == "AddItemsToPlaylistPayload"` |
| read operation | `fetchPlaylist` (with `enableWatchFeedEntrypoint`, `includeEpisodeContentRatingsV2`), fallback `fetchPlaylistContents` |

A wrong hash returns HTTP 200 with an empty `data` — a silent no-op, which is
indistinguishable from success unless the mutation payload is checked. The
self-healing scrape now matches `"query"` **and** `"mutation"` registrations.

## Spotify refuses every write but reads work (collaborative playlist)

A **collaborative** playlist is writable by its owner plus the collaborators
Spotify has accepted. Reading it needs nothing special, so the symptom is
"playlist reads fine, nothing is ever added".

The run now says which account it is and who owns the playlist, and it records
the refusal in `data/sync-state.json` (`diagnostics.last_write_problem`) so the
reason is readable **without the job log**:

```
ℹ️ write refused: Spotify refused the write on both routes — web-player: …; Web API: …
ℹ️ playlist owner: thishan; cookie account: phonkbot; collaborative: true
   → this account does NOT own the playlist: it must be collaborative AND this
     account an accepted collaborator
```

What to do if that line appears:

1. Open the playlist as the **cookie account** (the bot) in a browser; if it is
   not listed under *Your Library → Playlists*, it was never accepted as a
   collaborator — ask the owner to add that account again and accept the invite.
2. Or point `SPOTIFY_TARGET_PLAYLIST` at a playlist the cookie account **owns**.

## The Spotify account is a bot / free account (no Premium)

That is the expected setup here, and the code no longer fights it:

* **The player queue is Premium-only.** `me/player/queue` answers 403/404 for a
  free account, so it is not used at all — writes go through the **web player
  route** (`api-partner`, the same endpoint the browser tab uses), which needs no
  Premium and no Web API budget.
* **The Web API throttles bot writes fast** (429 after a few playlist updates).
  The web-player route does not share that window.
* **`SPOTIFY_BACKEND=rest`** is the opt-out if you ever want the old behaviour.

Writes are also **verified**: after one batch, the target playlist is re-read and
only the tracks that are really there are marked as added. Anything else stays in
the backlog and is retried, with this line in the summary:

```
🚨 3 track(s) were accepted by Spotify but are NOT in the target playlist —
   kept in the backlog, never marked as added
```

## YouTube got the tracks but the Spotify playlist is empty

Read the summary line the run prints now:

```
⚠️ Spotify rate limited (429): wrote what it could — wait a few minutes and run again
ℹ️ reason: Spotify blocked this session for ~1h 0m; 1 Spotify write(s), 12
   request(s) before it; the player queue was refused (Premium-only call) — set
   SPOTIFY_WRITE_MODE=direct to skip it; pacing 2s/call, 40 calls per run
```

* **`the player queue was refused`** → the queue endpoint (`me/player/queue`) is
  Premium-only. A free account answers 403/404, the engine falls back to direct
  playlist writes, and those are throttled harder. Set
  `SPOTIFY_WRITE_MODE=direct`.
* **`1 write(s), N request(s)`** → how much of the run's Web API budget was
  already spent before the first write. Raise `SPOTIFY_MAX_REQUESTS_PER_RUN` or
  lower `REQUEST_DELAY_SECONDS`, or read playlists from the web-player route.
* **Parked tracks are not lost.** They are flushed in **one batch at the start
  of the next run** (`♻️ Added N track(s) an earlier run had to park`) — before
  anything can throttle the run again — and they stay in
  `data/manual-queue.txt` until that batch succeeds.

## "Parked for the next run" — why a track was not added

A throttled run (Spotify 429 / slow-down) does **not** claim the track was
added any more. It goes to `data/manual-queue.txt` and to the state file's
pending list, and the next run adds it in one batch:

```
📋 Parked for the next run: 5 track(s) — queue full or Spotify slow-down
♻️ Added 5 track(s) an earlier run had to park
```

Older versions recorded a parked track as `spotify_added: true` **and** parked
it, so no later run would ever retry it. State files from that version are
repaired automatically on the next run (one line in the log:
`Re-queued N track(s) that an older run parked but recorded as added`).

* See the backlog any time: `python -m phonksync manual`
* Add it by hand now: `python -m phonksync manual --apply` (it reads the target
  playlist first, so nothing is added twice)
* Every real run also prints the backlog in the GitHub job summary
  (`### 📋 Spotify backlog`).

## `YT_COOKIE … no usable SAPISID`

The cookie name is `__Secure-3PAPISID`, but browsers and export tools mangle it
(`_Secure-3PAPISID`, `Secure3PAPISID`, lower-case, `Cookie:` prefix, JSON export).
All of those are now accepted — the matcher compares letters+digits only, so a
name that lost an underscore or dash still authenticates.

The diagnostic no longer repeats the cookie's own name (it used to be printed
next to a mangled copy, which read like `**Secure-3PAPISID`); it now says what is
wrong in plain words:

| message | meaning |
|---|---|
| `no usable SAPISID — not the URL` | you pasted the page URL / text, not the header |
| `no usable SAPISID — the SAPISID family is missing` | the header was clipped |
| `no usable SAPISID — the value was there but empty or truncated` | present but empty |
| `cookie ok (…, SAPISID-only (narrow …))` | auth works, but re-paste the whole header if writes 401 |
