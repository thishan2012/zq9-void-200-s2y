# ⏰ GitHub එකෙන්ම automatic sync — external service එකක් නැතුව

**කෙටි උත්තරය:** පුළුවන්. GitHub Actions වල `schedule` trigger එක තමයි GitHub එකේ built-in timer එක. ඒත් මේ repo එකේ මේ දක්වා `schedule` run එකක් නිරීක්ෂණය වෙලා නෑ; manual runs විතරයි තියෙන්නේ. GitHub cron එක පරක්කු වෙන්න හෝ drop වෙන්න පුළුවන් කියලා GitHubම කියනවා. ඒ නිසා **දවසට හරියටම 4 පාරක්, නියම වේලාවට, 100% guarantee** කරන්න GitHub-only ක්‍රමයකට බෑ.

මේ නිසා දැන් GitHub ඇතුළේම කුඩා **Phonk Sync Watchdog** workflow එකක් තියෙනවා. ඒකට cron-job.org account එකක්, අලුත් GitHub PAT එකක්, හෝ අලුත් repository secret එකක් ඕනේ නෑ.

> **වැදගත් සීමාව:** watchdog එකත් දුවන්නේ GitHubගේ `schedule` service එකෙන්මයි. native sync cron එක වැඩ නොකරන හේතුවම scheduler එකේ නම්, watchdog cron එකත් නොඑන්න පුළුවන්. අලුත් workflow එකෙන් schedule registration එක අලුත් වෙනවා; ඒක fix එකක් වෙන්න පුළුවන්, ඒත් guarantee එකක් නෙවෙයි. මේක independent timer එකක් නෙවෙයි.

## වැඩ කරන විදිහ

| Workflow | UTC cron | ලංකා වේලාවන් | වැඩේ |
|---|---|---|---|
| **Phonk Sync** | `15 2,8,14,20 * * *` | 01:45 / 07:45 / 13:45 / 19:45 | native sync run එක |
| **Phonk Sync Watchdog** | `45 2,8,14,20 * * *` | 02:15 / 08:15 / 14:15 / 20:15 | ඉහත වේලාවෙන් විනාඩි 30කට පස්සේ check කරනවා |

Watchdog scheduled check එකක් ආවම:

1. පසුගිය පැය **3** ඇතුළත `main` එකේ Phonk Sync run එකක් create වෙලා තියෙනවද බලනවා.
2. තිබ්බොත් **skip** කරනවා. ඒ run එක success, failed, cancelled, dry-run හෝ cache-only වුණත් skip කරනවා; මේක failed sync retry කරන workflow එකක් නෙවෙයි.
3. නැත්නම් GitHub එකේම built-in `GITHUB_TOKEN` එකෙන් `sync.yml` එකට `trigger_source=cron` dispatch කරනවා. ඒ run එක **live**, අලුත් songs උපරිම **100**යි.

Native cron එක විනාඩි 30කට වඩා පරක්කු වුණොත් duplicate dispatch එකක් වෙන්න පුළුවන්. `phonk-sync` concurrency group එක එකවර sync runs දුවන්න දෙන්නේ නෑ; saved state එක එකම song එක නැවත add වීම වළක්වනවා. ඒත් අමතර capped run එකක් වෙන්න පුළුවන් නිසා “හරියටම 4 runs” කියලා promise කරන්නේ නෑ.

## 1. PR එක `main` එකට merge කරන්න

Schedule triggers වැඩ කරන්නේ default branch එකේ තියෙන workflow file වලින් විතරයි. Branch එකේ තියෙන අලුත් watchdog file එක merge වෙනකල් automatic වැඩේ පටන් ගන්නේ නෑ.

PR page එකේ checks green නම් **Merge pull request → Confirm merge** ඔබන්න. Terminal ඕනේ නෑ.

## 2. Native scheduler එක නැවත register කරන්න උත්සාහ කරන්න

මේක සමහර repos වලට වැඩ කරලා තියෙනවා කියලා community reports තියෙනවා; මේ repo එකේ තාම prove වෙලා නෑ.

1. [Actions → Phonk Sync](https://github.com/thishan2012/zq9-void-200-s2y/actions/workflows/sync.yml)විවෘත කරන්න.
2. දකුණු ඉහළ **… → Disable workflow**, ඊට පස්සේ **Enable workflow**.
3. [Actions → Phonk Sync Watchdog](https://github.com/thishan2012/zq9-void-200-s2y/actions/workflows/sync-watchdog.yml) එකේත් workflow එක active ද බලන්න; disable වෙලා නම් enable කරන්න.
4. **Phonk Sync → Run workflow** එක පාරක් කරන්න. සාමාන්‍ය settings තියන්න; අලුත් songs add වෙන්න පුළුවන්.
5. ඊළඟ scheduled වේලාව පහු වෙනකල් බලන්න. GitHubට පැය කීපයක් පරක්කු වෙන්න පුළුවන්.

## 3. GitHub-only dispatch path එක එක පාරක් test කරන්න

**Actions → Phonk Sync Watchdog → Run workflow → `main` → Run workflow.**

> Manual watchdog run එකක් recent-run check එක bypass කරලා **live Phonk Sync run එකක් අනිවාර්යයෙන් dispatch කරනවා**. Dry run එකක් නෙවෙයි.

බලන්න ඕන දේ:

- Watchdog summary: `result: dispatched`.
- අලුත් **Phonk Sync** run එකක්; එහි **Run time** summary එකේ `trigger: timer dispatch`.
- Watchdog run එක `403 Resource not accessible by integration` කියලා fail වුණොත් repo/org Actions token policy එකෙන් built-in token dispatch අවහිර වෙන්න පුළුවන්. Settings → Actions → General බලන්න. “Disable actions” හෝ workflow execution අවහිර කරන policy එකක් තිබ්බොත් ඒක owner ට වෙනස් කරන්න කියන්න. මේ workflow එකේ අවශ්‍ය `actions: write` permission එක දැනටමත් declare කරලා තියෙනවා; හැම workflow එකකටම write දෙන global setting එකක් වෙනස් කරන්න ඕනේ නෑ.

මේ test එකෙන් **dispatch path එක** විතරයි prove වෙන්නේ. GitHub cron එක ඉබේ fire වෙනවද කියලා prove වෙන්නේ scheduled watchdog/sync run එකක් ආවම විතරයි.

## 4. Automatic වැඩ කරනවද කොහොමද බලන්නේද

[Actions](https://github.com/thishan2012/zq9-void-200-s2y/actions) එකේ:

- **Phonk Sync** run එකක Event = `schedule` → native cron එක ආවා.
- **Phonk Sync Watchdog** Event = `schedule` → GitHub-only backup timer එක ආවා.
- Watchdog එකෙන් පස්සේ ආව **Phonk Sync** run එක Event = `workflow_dispatch`; summary එක `trigger: timer dispatch`.
- Telegram `/runs` එකේ dispatch runs **Manual** පෙන්වුවත් ඒකෙන් timer එක වැඩ නෑ කියලා අදහසක් නෑ.

දවසක් දෙකක් ගියත් `schedule` events එකක්වත් නැත්නම් GitHub-only timer එක මේ repo එකේ තාම වැඩ කරන්නේ නෑ. තව cron එකක් එකතු කරාම ඒක අනිවාර්යයෙන් හරියනවා කියලා කියන්න බෑ. GitHub Support එකට repo එක, workflow නම, cron expression එක, සහ schedule runs නැති බව යවන්න; නැත්නම් independent external timer option එක සලකා බලන්න: [AUTO-RUN.md](AUTO-RUN.md).

## නවත්වන්න / ආරක්ෂාව

Automatic attempts නවත්වන්න **Phonk Sync** සහ **Phonk Sync Watchdog** දෙකම Actions UI එකෙන් **Disable workflow** කරන්න. එකක් විතරක් disable කළොත් අනිත් timer එකෙන් runs එන්න පුළුවන්.

Watchdog එකට තියෙන්නේ `actions: write` permission එක විතරයි; repo contents write කරන්නේ sync workflow එකේ state-commit step එකයි. අලුත් PAT එකක් හදන්නේ නෑ, token එකක් තුන්වන පාර්ශ්වයකට දෙන්නේ නෑ. Built-in token එක run එක අවසානයේ expire වෙනවා.

Spotify/YouTube secrets, `Scoure playlist.txt` sources, live writes, සහ YouTube daily quota logic වෙනස් වෙලා නෑ. එක automatic sync run එකකට අලුත් songs 100යි; runs 4කින් YouTube quota වැඩි වෙන්නේ නෑ.

## ඇයි මේ සීමාව?

GitHub workflow එකකින් තව workflow එකක් `workflow_dispatch` කරන්න built-in `GITHUB_TOKEN` එකට පුළුවන්; ඒකට PAT එකක් ඕනේ නෑ. GitHubගේ official exception එක මෙන්න: [1](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow). ඒත් ඒ පළමු workflow එක නියම වේලාවට පටන් ගන්න යම් timer එකක් ඕනේ. GitHub ඇතුළේ තියෙන timer එකත් `schedule`මයි; ඒක delay/drop වෙන්න පුළුවන්: [2](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).

ඒ නිසා මේ setup එක **GitHub-only, no new secrets, best-effort backup** එකක්. “Click නොකර සදහටම හරියටම වැඩ කරනවා” කියන guarantee එකක් නෙවෙයි.
