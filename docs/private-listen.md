# 私人听

给一个人用的 iPhone 播放器。Mac 上的中文播客工作台把**完整** `output.mp3` 放进 iCloud Drive，手机从同一个文件夹播放。没有账号、没有发现页、没有公开 RSS。

## 资料库位置

```text
~/Library/Mobile Documents/com~apple~CloudDocs/中文播客私人库/
  library.json
  episodes/<任务编号>/
    output.mp3
    episode.json
```

这是 iCloud Drive 里一个普通文件夹，Finder 里显示为「iCloud 云盘 / 中文播客私人库」。Mac 已登录 iCloud，并且打开了 iCloud 云盘时，手机会同步它。没有 iCloud 时，发布脚本仍会在这个路径建目录，但手机看不到。

可用环境变量 `PRIVATE_LISTEN_LIBRARY` 改掉根目录（测试或临时盘）。工作台和补发命令都认这个变量。

## 什么时候会发布

只在**完整**成品写好之后：

- 引擎 `run` / `render` 成功，且没有 `--preview`
- 工作台里「生成」成功（不是「试听」，也不是只准备译稿）

发布做三件事：

1. 把 `output.mp3` **复制**到 `episodes/<任务编号>/`（不用硬链接，避免 iCloud 占位文件和同一 inode 不同步）。
2. 写 `episode.json`。
3. 扫描全部 `episodes/*/episode.json`，按 `created_at` 新的在前，原子替换 `library.json`。

`episode.json` 至少包含：

| 字段 | 含义 |
| --- | --- |
| `id` | 任务目录名 |
| `title` | 手机上的显示标题，格式是 `中文主题 · 原节目名`。不会是 YouTube 或别的链接 |
| `created_at` | 成品 `output.mp3` 的修改时间，UTC，ISO-8601 |
| `duration_sec` | 有 `ffprobe` 就探测，否则为 `null` |
| `source_job_id` | 同 `id` |
| `youtube_id` | 能从 `metadata.json`、来源 URL 或标题里认出来才写 |
| `file` | 固定 `output.mp3` |

同一任务再次完整生成会覆盖那一期，不会在清单里出现两次。播放进度按 `id` 存在手机上，所以覆盖后仍是同一期。

失败是软的：引擎把原因打到标准输出，并在任务目录写 `private-listen.json`（`ok: false`）。`progress.json` 仍是 `complete`，进程退出码不变。工作台启动子进程时丢掉了引擎日志，所以服务进程会再试一次，并把结果打在工作台窗口里。两次都失败时，成品仍留在任务目录。

试听只写 `preview.mp3`，不会进资料库。

## 标题

手机列表和播放器都用 `episode.json` 的 `title`。约定是：

```text
中文主题 · 原节目名
```

例如 Creatine 这一期（The Diary Of A CEO）：

```text
肌酸专家：肌酸是减肥的秘密 · The Diary Of A CEO
```

中文在前，一眼能看出在讲什么。节目名、主持人、品牌保持原文，不勉强译成中文，例如 Huberman、All-In、The Diary Of A CEO、DOAC。节目名来自 YouTube 频道名，原样接在间隔号后面。

任务目录里的 `metadata.json`：

| 字段 | 含义 |
| --- | --- |
| `source_title` | YouTube 上的英文原题，由 oEmbed 或 yt-dlp 写入，不会是链接 |
| `display_title` | 译好的中文显示标题（含节目名）。没有这一项时，发布不会拿链接充数 |
| `title_zh` | 只含中文主题，不含节目名 |
| `channel` | 原节目名 |
| `video_id` | YouTube 视频编号 |
| `title_status` | `translated`（接口译出）、`source_zh`（原题已是中文）、`untranslated`（没有可用的翻译接口） |

翻译走工作台已经在用的 DeepSeek / 通义千问接口（`translate_title`）。密钥用任务里的 `provider`，从 `DEEPSEEK_API_KEY` 或 `DASHSCOPE_API_KEY` 读取；在 Mac 上也会查原来的钥匙串项。没有密钥时不会编造中文，显示标题退回「英文主题 · 原节目名」，或者只有频道名和视频编号、任务编号。这比链接好认，但主题仍是英文。配好密钥后用下面的 `--refresh-title` 再译一次即可，不用重新合成。

新建 YouTube 任务时就会拉取原题并尽量译成中文。引擎拿到 yt-dlp 信息后会再写一次，所以字幕失败改走本地识别时，标题也已经留下。

## 补发已有任务

不用重新合成，也不用重新朗读。任务目录里已经有完整 `output.mp3` 即可。Creatine 这一期的编号是 `6d898d4192de495fafcec86b9c938430`，默认在工作台任务根目录下。

只把现有成品拷进资料库（标题用任务里已经写好的非链接标题）：

```bash
python3 scripts/studio_publish_icloud.py \
  --job-dir "$HOME/Desktop/音频/中文播客工作台/jobs/6d898d4192de495fafcec86b9c938430"
```

要重新拉取英文原题、译成「中文主题 · 原节目名」，并改掉手机上这一期的标题：

```bash
python3 scripts/studio_publish_icloud.py \
  --refresh-title \
  --job-dir "$HOME/Desktop/音频/中文播客工作台/jobs/6d898d4192de495fafcec86b9c938430"
```

这会更新该任务的 `metadata.json`、`state.json` 标题，以及资料库里的 `episode.json` / `library.json`。音频仍然是原来的 `output.mp3`。

想自己指定手机上的句子时，把 `metadata.json` 的 `display_title` 写成 `中文主题 · 原节目名`，再跑不带 `--refresh-title` 的发布命令。`state.json` 里如果只剩一条链接，发布会忽略它。

成功后可以核对：

```bash
ls "$HOME/Library/Mobile Documents/com~apple~CloudDocs/中文播客私人库/episodes/6d898d4192de495fafcec86b9c938430"
```

这里应有 `output.mp3` 和 `episode.json`。上一级的 `library.json` 把这一期列在最前（如果它的成品时间最新）。

## 在 iPhone 上打开

工程在 `ios/PrivateListen/PrivateListen.xcodeproj`。显示名是「私人听」，bundle id 是 `com.liuyuxin.privatelisten`。签名用个人开发团队，在自己的 Mac 上 Debug 安装即可，不上架。

1. 用 Xcode 打开 `ios/PrivateListen/PrivateListen.xcodeproj`。
2. 选中 PrivateListen 目标 → Signing & Capabilities → Team 选你的个人团队。若签名改了 bundle id，保持和 entitlements 里的 `iCloud.com.liuyuxin.privatelisten` 一致，或让 Xcode 按新 id 生成容器。
3. 确认能力里有 iCloud Documents。容器标识是 `iCloud.com.liuyuxin.privatelisten`。这个容器只是让系统允许 iCloud；**节目不在这个容器里**，而在上面的「中文播客私人库」。Finder 里如果出现另一个叫「私人听」的文件夹，那是 App 容器，不要把节目手动挪进去。
4. 运行目标选 iPhone「不取经的小刘」，按 Run。首次安装按提示信任开发者。模拟器可以用来确认工程能编译，iCloud 文件夹选择和锁屏播放以真机为准。
5. App 打开后点「选择资料库」，在「浏览」里进入 iCloud Drive，选中「中文播客私人库」。系统会记住这个文件夹。选错了可点「重新选择」。
6. 列表显示标题、时长、日期。没听过的有「未听」。下拉，或切回 App，会重新读清单。点进去即可播放、拖动、±15 秒、倍速 1 / 1.25 / 1.5 / 2。进度记在这台手机上（UserDefaults），不写回 iCloud。锁屏和控制中心走系统正在播放。

手机只读 iCloud 里的文件，没有别的网络请求。音频还在下载时，列表会写「正在从 iCloud 下载」，下好后再点播放。
