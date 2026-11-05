# 🔐 Secrets Guide (required)

Every credential lives in **GitHub → your repo → Settings → Secrets and variables → Actions → New repository secret**.
Nothing secret is ever committed to the repo. This page shows exactly how to obtain each value.

## Summary table

| Secret | Required | What it is |
|---|---|---|
| `SPOTIFY_COOKIE` | ✅ | Full `Cookie` header from a logged-in open.spotify.com request |
| `YT_CLIENT_JSON` | ✅ | Entire Google OAuth client JSON file contents |
| `YT_REFRESH_TOKEN` | ✅ | Long-lived YouTube OAuth refresh token |
| `TELEGRAM_BOT_TOKEN` | ✅ for logs | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ for logs | Your Telegram chat ID |
| `SPOTIFY_SOURCE_PLAYLISTS` | recommended | Comma/newline-separated source playlist IDs or URLs |
| `SPOTIFY_TARGET_PLAYLIST` | recommended | Your Spotify target playlist ID or URL |
| `YOUTUBE_TARGET_PLAYLIST` | recommended | Your YouTube target playlist ID (`PL...`) |

> The three playlist secrets are optional only if you put the IDs in `config.json` instead. Secrets override `config.json`. For a public repo, secrets are the cleaner choice.

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

**Notes**
- The only value that matters is `sp_dc`; everything else is ignored, so pasting the full header is safe and easiest.
- If you prefer, a bare `sp_dc=...` pair or just the `sp_dc` value also works.
- The cookie expires eventually (weeks/months). If runs start failing with an auth error, repeat these steps and update the secret. Logging out of Spotify in that browser can invalidate it — use a browser profile you stay logged into.
- ⚠️ This cookie grants access to your Spotify account. Never share it, never commit it, never paste it into issues or chats.

## 2. `YT_CLIENT_JSON` — Google OAuth client 📄

1. Go to **https://console.cloud.google.com** and create a project (e.g. `phonk-sync`).
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External**.
   - Fill app name + your email; save.
   - Add yourself as a **Test user** (your Google account's email).
   - Publishing status can stay "Testing" — refresh tokens for test users expire after 7 days, so either keep it in testing and re-auth weekly, or **Publish app** (no verification needed for personal use of the `youtube` scope; you'll see an "unverified app" warning you can click through).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Create, then **Download JSON**.
5. Open the downloaded file. It looks like:
   ```json
   {"installed":{"client_id":"....apps.googleusercontent.com","project_id":"...","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","client_secret":"...","redirect_uris":["http://localhost"]}}
   ```
6. Create the secret `YT_CLIENT_JSON` and paste the **entire file contents** (the whole JSON, one line or pretty-printed — both work).

## 3. `YT_REFRESH_TOKEN` — one-time authorization 🔑

On your own computer (needs a browser):

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

The scope requested is `https://www.googleapis.com/auth/youtube` (read + write playlists).
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

## 6. Playlist secrets 🎵

| Secret | Example value |
|---|---|
| `SPOTIFY_SOURCE_PLAYLISTS` | `37i9dQZF1DXcBWIGoYBM5M, 37i9dQZF1DX0XUsuxWHRQd, https://open.spotify.com/playlist/...` |
| `SPOTIFY_TARGET_PLAYLIST` | `https://open.spotify.com/playlist/...` (a playlist **you own**) |
| `YOUTUBE_TARGET_PLAYLIST` | `PLxxxxxxxxxxxxxxxx` (from the playlist URL's `list=` parameter) |

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
