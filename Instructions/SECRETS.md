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
| `TELEGRAM_BOT_TOKEN` | optional | Bot token from @BotFather (for logs) |
| `TELEGRAM_CHAT_ID` | optional | Your Telegram chat ID (for logs) |
| `YOUTUBE_API_KEY` | only if YouTube enabled | Read-only API key (`AIza...`) used for search + metadata |
| `YOUTUBE_TARGET_PLAYLIST` | only if YouTube enabled | Your YouTube target playlist ID (`PL...`) |
| `YT_CLIENT_JSON` | only if videos must be **added** | Entire Google OAuth client JSON file contents |
| `YT_REFRESH_TOKEN` | only if videos must be **added** | Long-lived YouTube OAuth refresh token |

> **API key vs OAuth:** Google allows *reads* (search, video details, playlist listing) with a plain API key, but **playlist inserts always require OAuth**. So `YOUTUBE_API_KEY` alone = matching works, nothing gets added. Add `YT_CLIENT_JSON` + `YT_REFRESH_TOKEN` to actually write to the YouTube playlist. Both bill the same 10,000 units/day project quota.

> Playlist secrets are optional only if you put the IDs in `config.json` instead. Secrets override `config.json`. For a public repo, secrets are the cleaner choice.

---

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
3. The terminal prints `✅ Success!` and the refresh token.
4. Create the secret `YT_REFRESH_TOKEN` with that value.

If Google doesn't return a refresh token: revoke the grant at https://myaccount.google.com/permissions and run the command again (it forces `prompt=consent`).

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
| `search.list` | 100 | 80 / day (`max_searches_per_run`, `youtube_max_searches_per_day`) | 8,000 per key |
| `playlistItems.insert` | 50 | 40 / day (`youtube_max_inserts_per_day`) | 2,000 |
| `videos.list` / `playlistItems.list` | 1 | — | ~100 |

Counters live in `data/sync-state.json` under `quota` (`date`, `units_used`, `searches`, `inserts`, `keys`) and roll over at UTC midnight, so **three manual runs in one day still cannot exceed the Google limit**. With N API keys (N different projects) the daily budget becomes N x 10,000 units, tracked per key. When a cap is hit the run stops cleanly, the report says which cap, and the next run continues where it left off.

Tune these in `config.json`: `max_searches_per_run`, `youtube_max_searches_per_day`, `youtube_max_inserts_per_day`, `youtube_daily_quota_units`.

## 6b. Playlist secrets 🎵

| Secret | Example value |
|---|---|
| `SPOTIFY_SOURCE_PLAYLISTS` | `37i9dQZF1DXcBWIGoYBM5M, 37i9dQZF1DX0XUsuxWHRQd, https://open.spotify.com/playlist/...` |
| `SPOTIFY_TARGET_PLAYLIST` | `https://open.spotify.com/playlist/...` (a playlist **you own**) |
| `YOUTUBE_TARGET_PLAYLIST` | `PLxxxxxxxxxxxxxxxx` (only if YouTube sync is enabled) |

Separate multiple sources with commas or newlines. Spotify URLs, `spotify:playlist:` URIs, and bare IDs are all accepted.

## 7. Verify everything ✅

- In GitHub: **Actions → Phonk Sync → Run workflow** with `dry_run = true`. The `Validate config and secrets` step must pass.
- Locally: put the same values in a `.env` file (git-ignored) and run:
  ```bash
  python -m phonksync check --live
  ```
  You should see `✅ Spotify cookie valid`, `✅ YouTube OAuth valid`.

## 🚫 Security rules

- Secrets only in **GitHub Secrets** (or local `.env`). Never in code, config, issues, or screenshots.
- `.gitignore` blocks `.env`, `client_secret*.json`, `token*.json`.
- If a credential leaks: rotate it immediately (new Spotify login / revoke Google grant / BotFather `/revoke`).
