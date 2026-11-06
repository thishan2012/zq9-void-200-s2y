# 🎧 Phonk Playlist Syncer — පුද්ගලික භාවිතය සඳහා

[![Phonk Sync](https://github.com/thishan2012/zq9-void-200-s2y/actions/workflows/sync.yml/badge.svg)](https://github.com/thishan2012/zq9-void-200-s2y/actions/workflows/sync.yml)
[![Tests](https://github.com/thishan2012/zq9-void-200-s2y/actions/workflows/tests.yml/badge.svg)](https://github.com/thishan2012/zq9-void-200-s2y/actions/workflows/tests.yml)

> ⚠️ **මෙය පුද්ගලික සින්ක් ටූල් එකක් පමණයි. කරුණාකර Google / Search Engine වල Index නොකරන්න.**
> `noindex, nofollow` — මෙම repository එක public වුවත් minutes සීමාව නිසා private කිරීමට නොහැක.

මෙම ටූල් එකෙන් **Spotify playlists ගොඩක් → එක Spotify playlist එකකට** auto copy වෙනවා. GitHub Actions + Telegram log එක්ක වැඩ කරනවා.

ඔබේ source playlists වල තියෙන හැම track එකක්ම target playlist එකට copy කරනවා. `data/sync-state.json` එක repo එකේම save වෙන නිසා එකම සිංදුව දෙපාරක් add වෙන්නේ නෑ.

> Default එක **Spotify-only**. YouTube sync ඕන නම් `YOUTUBE_ENABLED=true` දාන්න ඕන.

## ✨ විශේෂාංග

- **Sources ගොඩක් → target එකයි.** Spotify source playlists 20ක් 30ක් උනත් කමක් නෑ.
- **Cookie වලින් Spotify login.** `sp_dc` cookie එක paste කළාම ඇති — Spotify app එකක් හදන්න ඕන නෑ.
- **Telegram log.** run එකකට **එක message** එකයි: add/skip/fail ගණන් + add නොවුණු songs විතරක් (spam නෑ).
- **Duplicate නෑ.** ISRC වලින් එකම recording එක වෙන ID එකකින් තිබ්බත් අඳුරගන්නවා.
- **Dry-run preview.** default 200 tracks විතරක් කියවලා write නොකර preview කරනවා.
- **YouTube sync (optional, default off).** ඕන නම් `youtube_enabled: true` කරලා 31-method pipeline එකෙන් YouTube එකටත් add කරනවා.

## 🚀 ඉක්මන් setup

සම්පූර්ණ guide: **[Instructions/README.md](Instructions/README.md)** | Secrets: **[Instructions/SECRETS.md](Instructions/SECRETS.md)**

1. Spotify target playlist එකක් හදන්න (ඔයාගේ account එකේ).
2. GitHub Secrets දාන්න (Spotify **Premium / developer app ඕන නෑ** — cookie එකෙන් විතරයි):
   `SPOTIFY_COOKIE`, `SPOTIFY_SOURCE_PLAYLISTS`, `SPOTIFY_TARGET_PLAYLIST`
   Telegram ඕන නම්: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   YouTube ඕන නම්: `YOUTUBE_ENABLED=true`, `YOUTUBE_API_KEY`, `YOUTUBE_TARGET_PLAYLIST`
   (playlist එකට video **දාන්න** නම් — API key එකෙන් බෑ. ලේසිම: `YT_COOKIE` (Google Console / OAuth / VPN කිසිවක් ඕන නෑ, `python -m phonksync youtube-auth --cookie-help` බලන්න). දිගුකාලීනව: `YT_REFRESH_TOKEN` + `YT_CLIENT_ID` + `YT_CLIENT_SECRET`)
3. GitHub → **Actions → Phonk Sync → Run workflow** → `dry_run` ✅ කරලා preview බලන්න. ඊට පස්සේ live run කරන්න.
4. Telegram බලන්න. ✅

> Auto schedule pause කරලා තියෙන්නේ. manually run කරන්න විතරයි. `sync.yml` එකේ `schedule:` cron එක ආයේ දැම්මම auto හැදෙනවා.

## 🧪 Local (terminal තියෙනවා නම්)

```bash
python -m phonksync check --live      # config + cookie check
python -m phonksync sync --dry-run    # preview only, පළමු 200 tracks
python -m phonksync sync --max-tracks 5
python -m phonksync stats             # stats බලන්න
python -m phonksync cookie-fix --show # cookie එකෙන් sp_dc එක විතරක් ගන්න
```

Local එකේ `.env` file එකෙන් secrets දාන්න පුළුවන් (git-ignored).

## 🔎 YouTube matching (default off)

`YOUTUBE_ENABLED=true` දැම්මම පරණ accuracy-first pipeline එක වැඩ:

```
MANUAL → STRICT → LOOSE → DETAIL → REVERSED → SINGLE → CHANNEL → TOPIC →
SUFFIX → COUNTRY → FEAT → DURATION → ARTISTS → SHORT → ARTISTLESS → EXACT →
WORDSET → SLOWED → UPLOADER → LINKED → FIELD → VARIANTS → DURVER →
CLEANARTIST → TITLEONLY → SONGTAG → ISRC → DESCTEXT → BROKEN → ALBUM →
PART → POPULAR → TOPRESULT
```

- 29 numbered steps + 2 classic = 31 methods + DETAIL + MANUAL = 33 pipeline entries.
- **Search + metadata කියවීම API key එකෙන් (`YOUTUBE_API_KEY`) — read-only, OAuth refresh ඕන නෑ.** Playlist එකට add කිරීම විතරයි OAuth ඕන (Google එකේ නීතිය).
- API key එක විතරක් තියෙනවා නම්: search සම්පූර්ණ වැඩ කරනවා, `python -m phonksync check --live` එකෙන් key එක test කරන්න පුළුවන්, ඒත් videos playlist එකට එකතු වෙන්නේ නෑ — Telegram report එකේ ඒ ගැන warning එකක් එනවා.
- Condition නැති step එක `never tried` ලෙස record වෙනවා.
- `ISRC` / `LINKED` exact identity විශ්වාස කරනවා. අනිත් හැම එකම safety checks 3න් pass විය යුතුයි.

Table: **[Instructions/MATCHING.md](Instructions/MATCHING.md)**

## 📁 Files

```
phonksync/            # package එක
  matcher.py          # 31-method pipeline + safety checks
  engine.py           # orchestration, dedup, retry
  spotify.py          # cookie-auth Spotify client
  youtube.py          # OAuth YouTube Data API client + quota
  db.py               # JSON database (repo එකේ commit වෙනවා)
  telegram.py         # Telegram logger
data/
  sync-state.json     # database — මොන සිංදුව add කළාද කියලා
  track-overrides.json# manual mappings (MANUAL method)
Instructions/         # setup docs
.github/workflows/    # manual sync + tests
tests/                # offline tests
```

## ⚠️ Notes

- Default Spotify-only නිසා YouTube quota වැය වෙන්නේ නෑ.
- YouTube on කළොත් daily quota **10,000 units** (1 search = 100, 1 playlist add = 50). ඒක නිසා app එකේම cap දෙකක් තියෙනවා: **80 search/දවස** (8,000 units) + **40 add/දවස** (2,000 units). `data/sync-state.json` එකේ මේවා දවස අනුව save වෙනවා නිසා දවසට run කීපයක් කළත් limit එක පනින්නේ නෑ.
- **80 search මදි නම් (දවසට 80 ට වඩා ඕන නම්):** Google quota එක යන්නේ **project එකකට**, key එකකට නෙවෙයි. ඉතින් **වෙනම Google Cloud project එකකින් තව key එකක්** හදලා `YOUTUBE_API_KEY` එකේ comma එකෙන් වෙන් කරලා දාන්න:
  ```
  YOUTUBE_API_KEY = AIzaKey1...,AIzaKey2...
  ```
  එතකොට 2 keys = **16,000 units/දවසකට = 160 search** (adds 40ක් සමඟ). Key එකක් ඉවර උනාම code එක ඉබේම ඊළඟ key එකට මාරු වෙනවා (403 `quotaExceeded` ආවත් run එක නවත්තන්නේ නෑ). Keys 3ක් = 240 search/දවස.
- එකම project එකේ keys 2ක් දැම්මොත් quota **වැඩි වෙන්නේ නෑ** — project එකට 10,000 තමයි. ඒ නිසා key එකකට එක project එකක්. [Instructions/TROUBLESHOOTING.md](Instructions/TROUBLESHOOTING.md) බලන්න.
- **සරලම setting:** `REQUEST_DELAY_SECONDS=2` (හැම Spotify call එකට තත්පර 2ක pause) — rate limit වැදෙන එක ගොඩක් අඩු කරනවා. 429 ආවොත් `Retry-After` අනුව backoff වෙලා retry කරනවා (6 වතාවක් වෙනකන්) — run එක fail වෙන්නේ නෑ.
- **Song එකක් add කරන්න බැරි වුනොත්:** ඒක skip කරලා ඊළඟ එකට යනවා (`⏭ Skipped`). දිග block එකක් නම් resume point එක save වෙනවා — ඊළඟ run එක ඒ තැනින්ම පටන් ගන්නවා.
- **API 429 වුනත් වැඩේ නවතින්නේ නෑ:** public playlist එකක් නම් **public page එකෙන්** (token නැතුව) tracks කියවනවා, ඒකත් බැරි නම් **cache** එකෙන්, ඒකත් නැත්නම් ඔයා save කරපු **collection** එකෙන්.
- **Spotify rate limit (429) වලට defence 4ක්:** pages 50 + pause, playlist snapshot cache (1 call), queue එකෙන් writes (batch), සහ cooldown මතක තියාගෙන live runs skip කිරීම. විස්තර: **[Instructions/SPOTIFY-LIMITS.md](Instructions/SPOTIFY-LIMITS.md)**
- **Spare accounts:** `SPOTIFY_COOKIE_2` (තව Spotify account එකක cookie) දාලා තිබ්බොත්, එකක් block වුනාම code එක ඊළඟ එකට මාරු වෙනවා — දවස් 5 ඉන්න ඕන නෑ.
- Spotify rate limit (429) ආවොත්: code එක pages 50 බැගින්, පොඩි pause එකක් එක්ක ඉල්ලනවා; 6 වතාවක් retry කරනවා. ඒත් 429 ආවොත් ඒ වෙනකොට කියවලා තියෙන tracks වලින් වැඩේ කරනවා (නැති කරන්නේ නෑ) — විනාඩි 5ක් ඉඳලා ආයේ run කරන්න.
- Spotify cookie එක password එකක් වගේ — GitHub Secrets වල විතරක් තියාගන්න, expire උනොත් refresh කරන්න.
- පුද්ගලික භාවිතය සඳහා පමණයි.

## 🔒 Privacy / Search

මෙම repo එක public වුවත් පුද්ගලික sync එකක් සඳහා පමණයි. Minutes සීමාව (2000) ඉවර නිසා private කිරීමට නොහැක. කරුණාකර **Google / Bing / Search Engine index නොකරන්න.**

ඔබට මෙම repo එක fork කර ඔබේම private repo එකේ භාවිතා කිරීමට පුළුවන්.

---
*මේ ගොනුවේ නම **`REPO.md`** — `README.md` කියන්නේ ඒකට යන symlink එකක් විතරයි (GitHub මුල් පිටුවේ පෙන්වන්න). නම මාරු කළේ Google search එකෙන් "README" වගේ generic වචන වලට හසු නොවෙන්නයි. Technical docs ඉංග්‍රීසියෙන් `Instructions/` folder එකේ තියෙනවා.*
