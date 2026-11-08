# 🔐 Secrets Guide (required)

Every credential lives in **GitHub → your repo → Settings → Secrets and variables → Actions → New repository secret**.
Nothing secret is ever committed to the repo. This page shows exactly how to obtain each value.

> **Default mode is Spotify-only.** You do NOT need any YouTube / Google secrets unless you explicitly enable YouTube sync (`youtube_enabled: true`).

## Summary table

| Secret | Required | What it is |
|---|---|---|
| `SPOTIFY_COOKIE` | ✅ | Full `Cookie` header from a logged-in open.spotify.com request |
| `SPOTIFY_SOURCE_PLAYLISTS` | ✅ | Comma/newline-separated source playlist IDs or URLs |
| `SPOTIFY_TARGET_PLAYLIST` | ✅ | Your Spotify target playlist ID or URL |
| `SPOTIFY_COOKIE_2` / `_3` / `_4` | optional | Cookies of **other** Spotify accounts — the client rotates to them when one gets rate limited (see [SPOTIFY-LIMITS.md](SPOTIFY-LIMITS.md)) |
| `TELEGRAM_BOT_TOKEN` | optional | Bot token from @BotFather (for logs) |
| `TELEGRAM_CHAT_ID` | optional | Your Telegram chat ID (for logs) |
| `YOUTUBE_API_KEY` | only if YouTube enabled | Read-only API key (`AIza...`) used for search + metadata |
| `YOUTUBE_TARGET_PLAYLIST` | only if YouTube enabled | Your YouTube target playlist ID (`PL...`) |
| `YT_CLIENT_JSON` | only if videos must be **added** | Entire Google OAuth client JSON file contents |
| `YT_REFRESH_TOKEN` | only if videos must be **added** | Long-lived YouTube OAuth refresh token |

> **API key vs OAuth:** Google allows *reads* (search, video details, playlist listing) with a plain API key, but **playlist inserts always require OAuth**. So `YOUTUBE_API_KEY` alone = matching works, nothing gets added. Add `YT_CLIENT_JSON` + `YT_REFRESH_TOKEN` to actually write to the YouTube playlist. Both bill the same 10,000 units/day project quota.

> Playlist secrets are optional only if you put the IDs in `config.json` instead. Secrets override `config.json`. For a public repo, secrets are the cleaner choice.

---

> 🔁 **Want a second YouTube OAuth account** so a full quota does not stop the
> run? See [§6a](#6a-a-second-oauth-account--keep-going-when-the-first-one-runs-out-).
> Account 1 runs uncapped until Google says stop, then account 2 continues.

## 1. `SPOTIFY_COOKIE` — paste the whole cookie 🍪

You do **not** need to pick out individual values. Paste the entire cookie header; the syncer extracts `sp_dc` automatically.

1. Open **https://open.spotify.com** in Chrome/Edge/Firefox and log in.
2. Press `F12` → **Network** tab.
3. Refresh the page (`F5`). Click any request to `open.spotify.com` (e.g. the first document request, or a `get_access_token` / `api` call).
4. In **Request Headers**, find `Cookie:`.
5. Right-click the request → **Copy → Copy request headers** (or select the `Cookie` header value) and copy the **entire cookie string** — it looks like:
   ```
   sp_t=...; sp_dc=AQ...long...; _ga=...; OptanonConsent=...; ...
   ```
6. Create the secret `SPOTIFY_COOKIE` and paste that whole string as the value.

**If parsing fails (`Could not find sp_dc in SPOTIFY_COOKIE`)**
The syncer accepts the whole Cookie header, a `name=value;`-per-line dump, a
WebView/Netscape export (TAB separated), a JSON cookie export, a bare `sp_dc=...`
pair, or just the value. It also lists the cookie **names** it found so you can
see what was pasted. Quickest fix is to send only the one value:

```bash
python -m phonksync cookie-fix --show     # paste the dump when asked
```
Copy the printed `AQ...` string and put **only that** in `SPOTIFY_COOKIE`.

**No Premium or developer app is needed.** Since March 2025 the web-player token
endpoint also demands a **TOTP** code. The syncer derives it the same way the web
player does (versioned secret → XOR unscramble → base32 → RFC 6238 code) and asks
the token endpoint with `totp`, `totpServer` and `totpVer`. Optional knobs:

| Env var | Meaning |
|---|---|
| `SPOTIFY_TOTP_VER` | Force a TOTP secret version (default: newest known, e.g. `61`) |
| `SPOTIFY_TOTP_CIPHER` | Comma-separated cipher bytes for that version |
| `SPOTIFY_TOTP_REMOTE` | `0` disables the hourly fetch of newer secrets from the community mirror |

If Spotify rotates the secret and both keys fail, the error message says so and
`python -m phonksync check --live` shows the exact endpoint response.

**Notes**
- The only value that matters is `sp_dc`; everything else is ignored, so pasting the full header is safe and easiest.
- If you prefer, a bare `sp_dc=...` pair or just the `sp_dc` value also works.
- The cookie expires eventually (weeks/months). If runs start failing with an auth error, repeat these steps and update the secret. Logging out of Spotify in that browser can invalidate it — use a browser profile you stay logged into.
- ⚠️ This cookie grants access to your Spotify account. Never share it, never commit it, never paste it into issues or chats.

## 1b. `YOUTUBE_API_KEY` — read-only search key 🔎 (only if YouTube sync is enabled)

1. Go to **https://console.cloud.google.com** → create/select a project.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key**.
4. (Recommended) Restrict the key: **Application restrictions → None**, **API restrictions → YouTube Data API v3** only.
5. Create the secret `YOUTUBE_API_KEY` with the `AIza...` value.
   Aliases also accepted: `YT_API_KEY`, `YOUTUBE_DATA_API_KEY`, `GOOGLE_API_KEY`.
6. **Need more than 100 searches/day?** Quota is per **Google Cloud project**, not per key. Create a *second project*, enable YouTube Data API v3, create a key there, and put both in the same secret separated by a comma:
   ```text
   YOUTUBE_API_KEY = AIzaKey1...,AIzaKey2...
   ```
   The client rotates keys automatically and only gives up when *every* key is out of quota. Per-key usage is tracked in `data/sync-state.json` under `quota.keys`.

Test it: `python -m phonksync check --live` must print `✅ YouTube API key works (read-only search, 1 result)`.

## 2. `YT_CLIENT_JSON` — Google OAuth client 📄 (only if YouTube sync is enabled)

> Skip this section for the default Spotify-only mode.

1. Go to **https://console.cloud.google.com** and create a project (e.g. `phonk-sync`).
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External**.
   - Fill app name + your email; save.
   - Add yourself as a **Test user** (your Google account's email).
   - Publishing status can stay "Testing" — refresh tokens for test users expire after 7 days, so either keep it in testing and re-auth weekly, or **Publish app** (no verification needed for personal use of the `youtube` scope; you'll see an "unverified app" warning you can click through).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - If you will use the **browser-only Playground method** (§3 Option A), choose **Web application** and add this Authorized redirect URI: `https://developers.google.com/oauthplayground`.
   - If you will use the **local terminal helper** (§3 Option B), choose **Desktop app**.
   - Create, then **Download JSON**.
5. Open the downloaded file. A Desktop client looks like:
   ```json
   {"installed":{"client_id":"....apps.googleusercontent.com","project_id":"...","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","client_secret":"...","redirect_uris":["http://localhost"]}}
   ```
   A Web application client has the same fields under a `"web"` key instead of `"installed"`; both are accepted.
6. Create the secret `YT_CLIENT_JSON` and paste the **entire file contents** (the whole JSON, one line or pretty-printed — both work).

## 3. `YT_REFRESH_TOKEN` — one-time authorization 🔑 (only if YouTube sync is enabled)

> Skip this section for the default Spotify-only mode.

You only need to do this once. There are two ways:

### Option A — Browser only, no terminal ✅ recommended

Use Google's **OAuth 2.0 Playground** with a **Web application** OAuth client.

1. Go to **Google Cloud Console → APIs & Services → Credentials**.
2. Click **Create credentials → OAuth client ID**.
3. Application type: **Web application**.
4. Under **Authorized redirect URIs**, add exactly:
   ```text
   https://developers.google.com/oauthplayground
   ```
5. Click **Create**. Copy the **Client ID** and **Client secret**, and download the JSON file.
6. Update the GitHub secret `YT_CLIENT_JSON` with the **entire contents of this web client JSON**.
   > Important: the refresh token must belong to the same OAuth client as `YT_CLIENT_JSON`. If you previously used a Desktop client JSON, replace it with this Web client JSON.
7. Open **https://developers.google.com/oauthplayground**.
8. Click the **gear icon** (OAuth 2.0 configuration) → check **Use your own OAuth credentials** → paste the Client ID and Client secret → close the settings.
9. In **Step 1**, scroll to **Input your own scopes** and enter:
   ```text
   https://www.googleapis.com/auth/youtube
   ```
   Then click **Authorize APIs**.
10. Sign in with the Google account that owns your YouTube target playlist. If you see an "unverified app" warning, click **Advanced → Go to app** and allow access.
11. Back in the Playground, in **Step 2**, click **Exchange authorization code for tokens**.
12. Copy the **Refresh token** value and create the GitHub secret `YT_REFRESH_TOKEN`.

Notes:
- The scope is `https://www.googleapis.com/auth/youtube` (read + write playlists).
- Your OAuth consent screen must include your Google account as a **Test user**, or the app must be published. Otherwise authorization can fail.
- If no refresh token appears, revoke the app grant at https://myaccount.google.com/permissions and repeat the Playground authorization.

### No VPN-friendly Console? Use a **browser cookie** (no OAuth at all) 🍪

If the Cloud Console will not load (or Google's consent pages keep challenging
the sign-in), skip OAuth entirely. A signed-in `music.youtube.com` session is
enough: the sync then writes through the web player's own endpoint
(`youtubei/v1/browse/edit_playlist`), exactly like clicking "add to playlist".

```bash
python -m phonksync youtube-auth --cookie-help          # how to copy it
python -m phonksync youtube-auth --cookie-help --cookie-stdin   # check a paste
```

0. **Prove it works** (30 seconds, adds a video that is already in the playlist,
   confirms it and removes the extra copy again — so nothing changes):
   ```bash
   python -m phonksync youtube-auth --test-cookie --playlist <PLAYLIST_ID> --cookie-stdin
   ```
   `✅ Cookie writes VERIFIED end to end` means Google accepted a real write.
1. Open <https://music.youtube.com> in the browser where you **are** signed in
   (a VPN for this one page is fine).
2. `F12` → **Network** → reload → click any `browse?…` request →
   right-click → **Copy → Copy request headers** (or copy the `Cookie:` value).
3. Store the secret **`YT_COOKIE`** (optionally `YT_AUTHUSER`, default `0`).

| secret | needed | what |
|---|---|---|
| `YT_COOKIE` | ✅ | the `Cookie:` header of a signed-in session (needs `__Secure-3PAPISID`) |
| `YT_AUTHUSER` | – | which signed-in account, `0` unless you use several |

What changes when a cookie is present and there is no OAuth:

- writes → `browse/edit_playlist` + `ACTION_ADD_VIDEO` (no quota, no Console)
- playlist reads → `youtubei/v1/browse` with `browseId=VL<playlist>`
- **search and metadata work with no API key and no quota** (public results page +
  `youtubei/v1/player`)
- the summary shows `✍️ YouTube writes: browser cookie (no OAuth, no Console, no VPN for the sync)`

Trade-offs, honestly:

- the cookie is a **session credential** — Google can sign it out (password change,
  "sign out of all devices", or age). When that happens the run stops with a clear
  "YouTube rejected the cookie (HTTP 401)" message instead of doing nothing.
- a real refresh token (`YT_REFRESH_TOKEN` + client id/secret) is still the more
  durable option and **wins** automatically whenever it is present.

### No `client_secret.json`? A client **id** is almost enough

The downloaded JSON is just a wrapper around two strings. If all you have is the
**client id** (the `…apps.googleusercontent.com` value on the Credentials page),
that works too:

```bash
# a) writes that keep working — refresh token via the device flow
export YT_CLIENT_ID=676669…apps.googleusercontent.com
python -m phonksync youtube-auth --device
#    → prints YT_REFRESH_TOKEN + the one-line JSON (optional to store)

# b) what is still missing?
python -m phonksync youtube-auth --doctor
```

Add these instead of / next to `YT_CLIENT_JSON`:

| secret | value |
|---|---|
| `YT_CLIENT_ID` | `…apps.googleusercontent.com` |
| `YT_CLIENT_SECRET` | the "Client secret" of the same OAuth client |
| `YT_REFRESH_TOKEN` | printed by `youtube-auth` |

`YT_CLIENT_ID` + `YT_CLIENT_SECRET` + `YT_REFRESH_TOKEN` is a complete setup —
`phonksync check` counts it as OAuth, and the sync uses it for writes.

> The **client secret** is needed to exchange an authorization code. Desktop
> clients sometimes allow the device flow without it; if Google answers
> `invalid_client`, copy the secret (Credentials → your OAuth client) and re-run.
> Note the secret is not a user secret — it identifies the app; keep it out of
> the repo all the same.

### Access token vs refresh token (this is the "expires in 2 minutes" trap)

Google hands out **two different tokens**, and only one of them is useful here:

| token | lifetime | what it is |
|---|---|---|
| **access token** (`ya29.…`) | ~1 hour (often shown as "expires in 3599 s") | what every API call uses *right now* |
| **refresh token** (`1//0g…`) | **does not expire** — until you revoke the app, change the account password, or leave the OAuth app in "Testing" for 7+ days | the long-lived secret you store in `YT_REFRESH_TOKEN` |

The syncer stores the **refresh token** and mints a fresh access token on every
run (`grant_type=refresh_token`), which is why it keeps working for months.
If you paste an **access token** instead it will die within the hour — that is
expected, and it is exactly what "expires in 2 minutes" looks like.

If you only want to test writes **once** right now, a pasted access token is
supported (it is never treated as a refresh token):

```bash
# temporary — dies with the token (~1h)
YT_ACCESS_TOKEN=ya29.a0Af...     # GitHub secret or .env
```

The run summary then says
`✍️ YouTube writes: manual access token (expires in ~1h, not renewable)`.

### Option B — Local helper (needs a terminal)

On your own computer:

```bash
git clone <your-repo> && cd <repo>
pip install -r requirements.txt
# put the downloaded client JSON in the repo folder as client_secret.json (git-ignored)
python -m phonksync youtube-auth --client-json client_secret.json
```

1. A browser tab opens (or copy the printed URL).
2. Choose your Google account → click through the "unverified app" warning if shown → **Allow**.
3. The terminal prints `✅ Success!` and the refresh token **plus a one-line
   `YT_CLIENT_JSON`** you can paste as-is.
4. Create the secret `YT_REFRESH_TOKEN` with that value.

If Google doesn't return a refresh token: revoke the grant at https://myaccount.google.com/permissions and run the command again (it forces `prompt=consent`).

#### If the redirect URI is rejected (`redirect_uri_mismatch` / "invalid request")

Three ways out, in order of least effort:

```bash
# 1) no redirect URI at all — Google shows a code, you paste it on their page
python -m phonksync youtube-auth --client-json client_secret.json --device

# 2) print the consent URL for a redirect URI you registered yourself
python -m phonksync youtube-auth --client-json client_secret.json \
    --auth-url --redirect-uri http://127.0.0.1:8765/callback

# 3) turn the `code=` you got back into a refresh token
python -m phonksync youtube-auth --client-json client_secret.json \
    --code 4/0AX4Xf... --redirect-uri http://127.0.0.1:8765/callback
```

Notes that save a round trip:
- For a **Desktop app** client the loopback redirect is `http://127.0.0.1:8765/callback`
  — Google accepts **`127.0.0.1`**, not `localhost`, for this (2026 change), and
  it does not need to be registered at all for desktop clients.
- For a **Web application** client the URI must be registered exactly, character
  for character.
- `--device` needs a client of type **"TVs and Limited Input devices"**; Google
  refuses the device flow for other client types.
- The authorization code is single-use and good for ~10 minutes.

> Alternative: if you already have a token JSON containing `refresh_token`, `client_id`, and `client_secret`, you can store it as a single secret `YT_TOKEN_JSON` instead of the two secrets above.

## 4. `TELEGRAM_BOT_TOKEN` — the log bot 🤖

1. In Telegram, open **@BotFather** → `/newbot`.
2. Pick a name and a username ending in `bot` (e.g. `phonk_sync_log_bot`).
3. BotFather replies with a token like `123456789:AA...`.
4. Create the secret `TELEGRAM_BOT_TOKEN` with that token.

## 5. `TELEGRAM_CHAT_ID` — where logs go 💬

1. Open a chat with your new bot and press **Start** (or send any message, e.g. `hi`).
2. In a browser, visit (replace `<TOKEN>`):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. In the JSON response find `"chat":{"id":123456789,...}` — that number is your chat ID.
4. Create the secret `TELEGRAM_CHAT_ID` with that number.

> Group chats also work (IDs are negative, like `-1001234567890`); add the bot to the group and send a message there first.

## 6. Quota plan 📊 (10,000 units/day)

| Action | Units | App cap | Units used |
|---|---|---|---|
| `search.list` | 100 | only when the free page search misses (`youtube_free_search`) | up to 10,000 per key |
| `playlistItems.insert` | 50 | 40 / day (`youtube_max_inserts_per_day`) | 2,000 |
| `videos.list` / `playlistItems.list` | 1 | — | ~100 |

Counters live in `data/sync-state.json` under `quota` (`date`, `units_used`, `searches`, `inserts`, `keys`) and roll over at UTC midnight, so **three manual runs in one day still cannot exceed the Google limit**. With N API keys (N different projects) the daily budget becomes N x 10,000 units, tracked per key. When a cap is hit the run stops cleanly, the report says which cap, and the next run continues where it left off.

Tune these in `config.json`: `max_searches_per_run`, `youtube_max_searches_per_day`, `youtube_max_inserts_per_day`, `youtube_daily_quota_units`.

## 6a. A second OAuth account — keep going when the first one runs out 🔁

YouTube quota is **per Google Cloud project**. A second OAuth client from a
**different project** therefore comes with its own fresh 10,000 units/day.

**How it behaves, on purpose:**

- Account 1 gets **no hand-made cap**. It adds as many songs as Google allows.
- Nothing is guessed and nothing is throttled. The switch happens **only** when
  Google itself answers `403 quotaExceeded`.
- The song that triggered the error is **retried on account 2**, so nothing is
  lost at the handover.
- Account 1 is remembered as empty for the rest of the UTC day, so later runs
  start straight on account 2 instead of wasting a call re-discovering it.
- At UTC midnight everything resets and account 1 is first again.
- When **every** account is empty, the run stops cleanly as before.

A `429` (too many requests per second) is **not** a quota stop — it is retried on
the same account. A spare account is never burned on a temporary rate limit.

### Option A — numbered secrets (easiest in the GitHub UI)

Create a second OAuth client in a **different Google Cloud project**, authorize
it once (same steps as §2 and §3), then add these three secrets:

| Secret | Value |
|---|---|
| `YT_CLIENT_ID_2` | the new client's ID |
| `YT_CLIENT_SECRET_2` | the new client's secret |
| `YT_REFRESH_TOKEN_2` | the refresh token you got for it |

> ⚠️ **All three values are needed — but not necessarily three secret names.**
> A refresh token only works with the OAuth client that issued it. If
> `YOUTUBE_OAUTH_TOKEN_JSON_2` already contains the matching `client_id`,
> `client_secret` and `refresh_token`, **nothing else is missing**; a separate
> `YT_CLIENT_SECRET_2` or `YT_CLIENT_JSON_2` is unnecessary. See Option A2.
>
> If you already have `YT_CLIENT_ID_2` and `YT_REFRESH_TOKEN_2`, but the JSON
> does **not** contain that client's secret, add `YT_CLIENT_SECRET_2` from the
> same new OAuth client. GitHub cannot display saved secret values, so use the
> original JSON/download or update the secret with a known-complete value.
> Never paste credential values into chat, issues or screenshots.
>
> An incomplete spare is **never handed the run**: account 1 keeps working
> exactly as before. The credential check names the missing part, for example
> `OAuth account #2: has a refresh token but no client secret.`

The workflow reads `_2` through `_5`. Numbering may have gaps — `_2` and `_4`
both work. Leave the secrets you do not use out entirely; empty ones are ignored.

### Option A2 — you have the JSON file 📄 (one secret, nothing to copy out)

If you downloaded the client JSON, **do not pick values out of it**. Paste the
whole thing into one secret and add your refresh token to it.

Secret name — any of these works, use whichever matches your account-1 secret:

| Secret name | |
|---|---|
| `YT_CLIENT_JSON_2` | shortest |
| `YT_TOKEN_JSON_2` | |
| `YOUTUBE_OAUTH_TOKEN_JSON_2` | matches an account-1 `YOUTUBE_OAUTH_TOKEN_JSON` |
| `YT_OAUTH_TOKEN_JSON_2` / `YOUTUBE_CLIENT_JSON_2` | also accepted |

Value — your downloaded JSON with **one line added** (`refresh_token`):

```json
{
  "installed": {
    "client_id": "….apps.googleusercontent.com",
    "client_secret": "…",
    "token_uri": "https://oauth2.googleapis.com/token"
  },
  "refresh_token": "1//…"
}
```

`"web"` instead of `"installed"` is fine too. If your token JSON already has
`refresh_token` in it, paste it **unchanged** — nothing to add.

A flat shape works as well, if that is what you have:

```json
{"client_id": "…", "client_secret": "…", "refresh_token": "1//…"}
```

> The client JSON on its own **cannot add videos** — it names the app, not you.
> Without a `refresh_token` the run tells you before it starts:
> `OAuth account #2: has a client JSON but no refresh token.`

### Option B — one secret for all the spares

Secret name: **`YOUTUBE_OAUTH_ACCOUNTS`**. Value: a JSON list.

```json
[
  {"client_id": "…apps.googleusercontent.com", "client_secret": "…", "refresh_token": "1//…"},
  {"client_id": "…apps.googleusercontent.com", "client_secret": "…", "refresh_token": "1//…"}
]
```

**Every JSON shape is accepted here too**, so you can paste the downloads as they
are — no copying values around:

```json
[
  {
    "installed": {"client_id": "…", "client_secret": "…"},
    "refresh_token": "1//…"
  },
  {
    "client_json": {"web": {"client_id": "…", "client_secret": "…"}},
    "refresh_token": "1//…"
  }
]
```

`client_json` / `token_json` may be a nested object **or** a quoted JSON string.
A single account may be passed as one object instead of a list.

This one has no count limit. Both options can be mixed; the JSON list comes
first, then the numbered secrets.

### Seeing it happen

The run summary and Telegram get a line:

```text
🔁 YouTube quota: switched OAuth account 1 time(s) (2 accounts configured) — the run continued
```

Per-account usage is in `data/sync-state.json` → `quota.accounts`
(`units`, `inserts`, `exhausted` for each account).

> ⚠️ **A second account in the same Google Cloud project does not help** — the
> quota is shared by the project. It must be a different project.

> With a spare configured, `youtube_max_inserts_per_day` no longer limits OAuth
> writes: that is the "no hand-made cap" rule. With only one account, the old cap
> still applies exactly as before.

## 6b. Playlist secrets 🎵

| Secret | Example value |
|---|---|
| `SPOTIFY_SOURCE_PLAYLISTS` | `37i9dQZF1DXcBWIGoYBM5M, 37i9dQZF1DX0XUsuxWHRQd, https://open.spotify.com/playlist/...` |
| `SPOTIFY_TARGET_PLAYLIST` | `https://open.spotify.com/playlist/...` (a playlist **you own**) |
| `YOUTUBE_TARGET_PLAYLIST` | `PLxxxxxxxxxxxxxxxx` (only if YouTube sync is enabled) |

Separate multiple sources with commas or newlines. Spotify URLs, `spotify:playlist:` URIs, and bare IDs are all accepted.

## 7. Verify everything ✅

- **No terminal:** In GitHub go to **Actions → Credential Check → Run workflow**. It is read-only: it tests the stored Spotify cookie, refreshes the primary YouTube OAuth credential and every structurally complete spare, and reads the same YouTube target with each credential. It prints a redacted ✅/❌ summary. It does not add songs, save quota/state or commit anything. Look for `🔁 YouTube OAuth accounts: 2 configured, 2 usable`, `✅ Quota failover armed`, and a separate `✅ YouTube OAuth account #2: token refresh + target playlist read OK`. The inventory line alone is only a structural check; the account-#2 live-check line is the credential proof.
- In GitHub: **Actions → Phonk Sync → Run workflow** with `dry_run = true`. The `Validate config and secrets` step must pass.
- Locally: put the same values in a `.env` file (git-ignored) and run:
  ```bash
  python -m phonksync check --live
  python -m phonksync youtube-auth --check-token   # tests YT_REFRESH_TOKEN from .env
  ```
  You should see `✅ Spotify cookie valid`, `✅ YouTube OAuth valid`, and `✅ Works with client …` for the refresh token.

## 🚫 Security rules

- Secrets only in **GitHub Secrets** (or local `.env`). Never in code, config, issues, or screenshots.
- `.gitignore` blocks `.env`, `client_secret*.json`, `token*.json`.
- If a credential leaks: rotate it immediately (new Spotify login / revoke Google grant / BotFather `/revoke`).
