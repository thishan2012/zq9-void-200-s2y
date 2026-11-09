# ⏰ Click නොකර දවසට 4 පාරක් sync කරන්න — external timer setup

මේ repo එකේ GitHub cron එක තියෙනවා, ඒත් ඒකෙන් run එකක් ආවේ නැති අවස්ථා තියෙනවා. GitHub cron එක පරක්කු වෙන්න හෝ drop වෙන්න පුළුවන්. ඒ නිසා **cron-job.org → GitHub Run workflow API** කියන backup ක්‍රමය හදලා තියෙනවා. GitHub එකේ workflow එකමයි දුවන්නේ; playlist sync code එක වෙනස් වෙන්නේ නෑ.

**මේ guide එකේ setup එක එක පාරක් ඔයා browser එකෙන් කරන්න ඕන.** Terminal ඕනේ නෑ. GitHub token එකයි cron-job.org login එකයි මට දෙන්න එපා; chat/repo එකේ දාන්නත් එපා.

> දැන් තියෙන තත්ත්වය: workflow support එක code එකේ තියෙනවා. පහත token + timer setup එක ඉවර වෙනකල් external automatic runs පටන් ගන්නේ නෑ.

## වැඩ කරන වේලාවන්

Timezone එක **Asia/Colombo (Sri Lanka, UTC+5:30)** දාන්න:

- රෑ **1:45 AM**
- උදේ **7:45 AM**
- දවල් **1:45 PM**
- හවස **7:45 PM**

එක automatic run එකකට **අලුත් songs උපරිම 100යි**. දැනටමත් synced songs ගණන් ගන්නේ නෑ. අලුත් song එකක්ම නැත්නම් `Added: 0` එන එක සාමාන්‍යයි. YouTube daily quota එක තවදුරටත් account එකකට 195 inserts; timer එකෙන් ඒ limit එක වැඩි වෙන්නේ නෑ.

## 1. GitHub token එකක් හදන්න

1. GitHub → profile picture → **Settings**.
2. පහළට ගිහින් **Developer settings → Personal access tokens → Fine-grained tokens**.
3. **Generate new token** ඔබන්න. GitHub password/MFA ඉල්ලුවොත් login එක සම්පූර්ණ කරන්න.
4. **Token name:** `phonk-sync-timer`.
5. **Resource owner:** `thishan2012`.
6. **Expiration:** ඔයාට පරිස්සම් කරන්න පුළුවන් දිනයක් තෝරන්න. Expire වුණාම timer එක නවතිනවා; ඒ දිනයට reminder එකක් දාන්න.
7. **Repository access → Only select repositories →** `thishan2012/zq9-void-200-s2y` **විතරක්** තෝරන්න.
8. **Repository permissions → Actions → Read and write**. Metadata read permission GitHub විසින් අවශ්‍ය නම් auto-add වෙයි. Contents write, Administration, අනිත් permissions දෙන්න ඕන නෑ.
9. **Generate token** → token එක copy කරන්න.

GitHubගේ official dispatch API එකට fine-grained token එකේ **Actions: write** permission එක ඕන කියලා docs කියනවා: [1](https://docs.github.com/en/rest/actions/workflows).

> මේ token එකෙන් repo එකේ Actions run කරන්න පුළුවන්. ඒ නිසා password එකක් වගේ රකින්න. Classic `repo` token එකක් වෙනුවට මේ repo-only fine-grained token එක පාවිච්චි කරන්න.

## 2. cron-job.org එකේ timer එක හදන්න

[cron-job.org](https://cron-job.org/) එකේ sign up/login → **Create cronjob**. Custom schedules, immediate test runs, history, failure notifications සහ free service එක service එකේම ලැයිස්තුගත කරලා තියෙනවා: [1](https://cron-job.org/en/).

### Common settings

**Title:** `Phonk Sync — 4 times daily`

**URL** (මේකම copy කරන්න):

```text
https://api.github.com/repos/thishan2012/zq9-void-200-s2y/actions/workflows/sync.yml/dispatches
```

**Enabled:** on.

### Schedule

- **Timezone:** `Asia/Colombo` / Sri Lanka / UTC+05:30.
- **Days of month:** every day / all.
- **Months:** every month / all.
- **Days of week:** every day / all.
- **Hours:** `1`, `7`, `13`, `19` විතරක් තෝරන්න.
- **Minutes:** `45` විතරක් තෝරන්න.

12-hour UI එකක් නම් **1:45 AM, 7:45 AM, 1:45 PM, 7:45 PM**. “Every hour” හෝ “Every minute” තෝරන්න එපා. Next executions preview එකේ මේ 4 වේලාවන් පෙනෙනවා නම් හරි.

### Advanced settings

**Request method:** `POST`.

**Request body / POST data** (මේකම copy කරන්න):

```json
{"ref":"main","inputs":{"trigger_source":"cron"}}
```

**Headers** — name සහ value වෙන වෙනම fields වලට දාන්න:

| Header name | Value |
|---|---|
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer PASTE_YOUR_TOKEN_HERE` |
| `X-GitHub-Api-Version` | `2026-03-10` |
| `Content-Type` | `application/json` |

`Authorization` value එකේ `PASTE_YOUR_TOKEN_HERE` වෙනුවට ඔයා copy කරපු GitHub token එක දාන්න. **`Bearer ` කෑල්ලයි space එකයි තියන්න.** Token එක URL එකට, request body එකට හෝ GitHub repository secret එකකට දාන්න ඕන නෑ.

> cron-job.org කියන්නේ තුන්වන පාර්ශ්වයක free service එකක්. ඔයා ඒ service එක විශ්වාස කරනවා නම් විතරක් මේ ක්‍රමය පාවිච්චි කරන්න: Authorization header එක ඔවුන්ගේ job settings වල save වෙනවා. Token එක මේ repo එකට + Actions permission එකට විතරක් සීමා කරලා තියෙන්නේ ඒ risk එක අඩු කරන්න.

**Save / Create cronjob** ඔබන්න. Notifications තියෙනවා නම් **Notify on failure** on කරන්න.

## 3. එක පාරක් test කරන්න

cron-job.org job එකේ **Run now / Execute now** ඔබන්න. මේක **live sync run එකක්**; dry run එකක් නෙවෙයි.

1. cron history එකේ HTTP **200 හෝ 204** බලන්න. ඒ කියන්නේ GitHub එක run request එක භාරගත්තා. 204 නම් response body හිස් වීම සාමාන්‍යයි. HTTP success එකෙන් කියවෙන්නේ dispatch එක භාරගත්තා කියලා විතරයි; sync සාර්ථකද කියලා GitHub/Telegram එකෙන් බලන්න.
2. [GitHub → Actions → Phonk Sync](https://github.com/thishan2012/zq9-void-200-s2y/actions/workflows/sync.yml) විවෘත කරන්න. අලුත් run එක → **Run time** summary එකේ **`trigger: external timer`** තියෙනවා නම් backup path එක හරි.
3. Telegram message එකේ `Added | Skipped | Failed` බලන්න. අලුත් songs නැත්නම් `Added: 0` වෙන්න පුළුවන්.

**වැදගත්:** මේ backup එක GitHubගේ `workflow_dispatch` API එක පාවිච්චි කරන නිසා Telegram bot එකේ `/runs` එක තවදුරටත් **Manual** කියලා පෙන්විය හැකියි. ඒකෙන් timer එක වැඩ නෑ කියලා අදහසක් නෑ. **Run time → trigger: external timer** සහ cron-job.org history එක බලන්න. Native GitHub cron run එකක් නම් Event එක `schedule`; external timer run එකක් නම් `workflow_dispatch`.

## වැරදුණොත්

| HTTP status | බලන්න ඕන දේ |
|---|---|
| `401` | Token එක expire/revokedද? `Authorization: Bearer <token>` space එකත් හරිද? |
| `403` | Token එකේ මේ repo එකට **Actions: Read and write** තියෙනවද? Resource owner `thishan2012`ද? |
| `404` | URL එකේ repo/workflow නම හරිද? Token එකට මේ repo එක access තියෙනවද? |
| `422` | Body එකේ `ref: main` සහ `trigger_source: cron` හරිද? අලුත් workflow version එක `main` එකට merge වෙලාද? |
| `200/204`, ඒත් run නෑ | Actions → Phonk Sync බලන්න; ටිකක් queue වෙන්න පුළුවන්. URL එක `sync.yml` dispatch endpoint එකද කියලාත් බලන්න. |
| Timer green, sync red | GitHub run summary/Telegram error එක බලන්න. Cookie/token/quota ප්‍රශ්නයක් වෙන්න පුළුවන්; cron request එක සාර්ථක වීම sync success එකක් නෙවෙයි. |

Token එකක් leak වුණා කියලා හිතෙනවා නම් GitHub → Settings → Developer settings → Personal access tokens එකෙන් **revoke/delete** කරලා අලුත් එකක් හදලා cron header එක update කරන්න. Token එක chat එකට දාන්න එපා.

## Native GitHub cron එකත් තියෙනවා

`15 2,8,14,20 * * *` cron එක fallback එකක් විදිහට තියෙනවා. GitHub ඒක කවදාහරි fire කළොත් external timer එකත් ඒ වේලාවටම fire වෙන්න පුළුවන්. `phonk-sync` concurrency group එක runs එකවර දුවන්න දෙන්නේ නෑ; synced state එක නිසා ඒම song එක දෙපාරක් add කරන්නේ නෑ. ඒ දෙවෙනි run එක අමතර no-op run එකක් වෙන්න පුළුවන්. External timer එක ස්ථිරව වැඩ කරනවා තහවුරු වුණාට පස්සේ native cron එක අයින් කරන්න පුළුවන්.

GitHub cron delay/drop behaviour පිළිබඳ official note: [2](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
