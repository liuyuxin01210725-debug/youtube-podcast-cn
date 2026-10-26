# 私人听

给一个人用的 iPhone 播放器。Mac 上的中文播客工作台把**完整** `output.mp3` 放进 iCloud Drive，手机从同一个文件夹播放。没有账号、没有发现页、没有公开 RSS。

## 资料库位置

```text
~/Library/Mobile Documents/com~apple~CloudDocs/中文播客私人库/
  library.json
  inbox/
    <32位编号>.json
    <32位编号>.status.json
  episodes/<任务编号>/
    output.mp3
    episode.json
    cover.jpg（或 cover.png / cover.webp）
```

这是 iCloud Drive 里一个普通文件夹，Finder 里显示为「iCloud 云盘 / 中文播客私人库」。Mac 已登录 iCloud，并且打开了 iCloud 云盘时，手机会同步它。没有 iCloud 时，发布脚本仍会在这个路径建目录，但手机看不到。

可用环境变量 `PRIVATE_LISTEN_LIBRARY` 改掉根目录（测试或临时盘）。工作台和补发命令都认这个变量。

## 什么时候会发布

只在**完整**成品写好之后：

- 引擎 `run` / `render` 成功，且没有 `--preview`
- 工作台里「生成」成功（不是「试听」，也不是只准备译稿）

发布做这几件事：

1. 把 `output.mp3` **复制**到 `episodes/<任务编号>/`（不用硬链接，避免 iCloud 占位文件和同一 inode 不同步）。
2. 放一张封面：任务里自带的图，否则 YouTube 缩略图，再不行就用标题画一张。封面失败不影响音频发布。
3. 写 `episode.json`。
4. 扫描全部 `episodes/*/episode.json`，按 `created_at` 新的在前，原子替换 `library.json`。

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
| `artwork` | 有封面时才写，`cover.jpg` / `cover.png` / `cover.webp` |
| `artwork_kind` | `provided`（任务里自带的图）、`thumbnail`（YouTube oEmbed 缩略图，发布时下载）、`title`（用标题画的卡片）、`placeholder`（纯色底，手机锁屏会改用标题卡片） |

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

## 从系统分享到 Mac 收件箱

手机不转换。Safari、YouTube、Substack 或播客 App 里点系统分享，选「私人听」，一条小 JSON 进资料库的 `inbox/`。Mac 上的监听看见之后，用工作台默认的可辨音色（Edge）排队整期转换，成功后仍走原来的发布。没有账号，也没有另建的云服务。

个人团队不能开 iCloud 容器，也不能开 App Group。分享扩展和主 App 各自用「文件」选择器记住「中文播客私人库」，写的是 iCloud 云盘里这个普通文件夹，不是 App 自己的容器。

`inbox/<编号>.json`：

| 字段 | 含义 |
| --- | --- |
| `id` | 32 位小写十六进制，和文件名一致 |
| `url` | 分享到的 https 链接 |
| `shared_at` | UTC，ISO-8601 |
| `source_app` | 系统给得到才写。分享扩展通常拿不到来源 App 的 bundle id |

监听处理后写 `inbox/<编号>.status.json`：`queued`（已进工作室队列）、`published`（已进资料库）、`duplicate`（同一条链接已经在排队或已完成）、`failed`（转换或发布失败，`error` 是给人看的短句）。失败只留下这个状态文件，监听进程继续看下一封。

同一条链接只保留一条还在跑或已经完成的任务。失败之后再分享一次，会再排一条。若要手动重试，删掉对应的 `.status.json`；如果工作室里那条任务仍是排队中或已完成，新分享会记成「已经收过」。

手机列表顶部的「收件箱」只显示还没发布的几条：等待 Mac、正在转换、转换失败、已经收过。已发布的不占位置，节目出现在下面的列表里。

## 在 Mac 上跑收件箱监听

工作台开着时，监听只把任务放进队列（`action` 为 `run`，`tts` 为 `edge`），由工作台串行合成并发布。工作台没开时，监听自己跑引擎，同一时间只跑一条，并且会占用工作台那把锁，避免两头同时合成。

先看一轮，不常驻：

```bash
python3 scripts/studio_inbox.py --once
```

一直看着（默认每 20 秒）：

```bash
python3 scripts/studio_inbox.py
```

只建任务、自己不合成：

```bash
python3 scripts/studio_inbox.py --no-run
```

`--library` 和 `--jobs` 可以改资料库和任务根目录。资料库仍认 `PRIVATE_LISTEN_LIBRARY`。任务默认在 `~/Desktop/音频/中文播客工作台/jobs`。YouTube 链接走原来的字幕路径；Substack、播客页和其它公开 https 走 `web`，用 yt-dlp 取音频，再进同一套可辨音色。本机地址和 http 链接会记失败，不会下载。

登录开机后一直跑的话，把下面保存成 `~/Library/LaunchAgents/com.liuyuxin.privatelisten.inbox.plist`，把两处路径换成自己的仓库目录，然后 `launchctl load` 那个文件：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.liuyuxin.privatelisten.inbox</string>
  <key>ProgramArguments</key>
  <array>
    <string>/替换成仓库/.venv/bin/python</string>
    <string>/替换成仓库/scripts/studio_inbox.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/替换成仓库</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

仓库里有 `.venv` 时，监听会用 `.venv/bin/python` 跑引擎。Finder 里收件箱文件如果还是云朵占位，等它下载到本机再被读到；空文件不会被当成失败。

## 在 iPhone 上打开

工程在 `ios/PrivateListen/PrivateListen.xcodeproj`。显示名是「私人听」，bundle id 是 `com.liuyuxin.privatelisten`。分享扩展是 `com.liuyuxin.privatelisten.share`，分享页里也显示「私人听」。签名用个人开发团队，在自己的 Mac 上 Debug 安装即可，不上架。

1. 用 Xcode 打开 `ios/PrivateListen/PrivateListen.xcodeproj`。
2. 选中 **PrivateListen** 和 **ShareExtension** 两个目标，Signing & Capabilities 里 Team 都选同一个个人团队，Automatically manage signing 打开。两个目标都不要加 iCloud，也不要加 App Groups。个人团队签不上这些能力。仓库里的 entitlements 是空的；如果 Xcode 里还留着以前的 iCloud 能力，删掉它再编。
3. 运行目标选 iPhone「不取经的小刘」，scheme 用 PrivateListen，按 Run。扩展会嵌进 App 一起装上。免费描述文件大约 7 天过期，扩展算第二个 App ID（个人团队最多 10 个）。过期后重新 Run 一次。
4. 手机上若提示不受信任的开发者：设置 → 通用 → VPN 与设备管理 → 信任你的开发者证书。装好后如果分享页没有「私人听」，在分享页拉到最下点「更多」（或「编辑」）打开它。分享页有缓存时，把来源 App 划掉重开一次。
5. App 打开后点「选择资料库」，在「浏览」里进入 iCloud 云盘，选中「中文播客私人库」。系统会记住这个文件夹。选错了可点「重新选择」。这一步是播放和看收件箱用的。
6. 第一次从别的 App 分享到「私人听」时，扩展还没有自己的书签，页面上点「选择中文播客私人库」，再选同一个文件夹。选过这一次之后，再分享会直接写入 `inbox/`，不用打开主 App。若主 App 里已经选过文件夹、又不想在分享页再选，可以点「打开私人听写入」，链接会交给主 App 写进收件箱。
7. 列表显示标题、时长、日期。没听过的有「未听」。上面的「收件箱」是还没发布的分享。下拉，或切回 App，会重新读清单。点进去即可播放、拖动、±15 秒、倍速 1 / 1.25 / 1.5 / 2。进度记在这台手机上（UserDefaults），不写回 iCloud。

手机不自己合成。音频还在下载时，列表会写「正在从 iCloud 下载」，下好后再点播放。

## 锁屏和控制中心的封面

播放时 `MPNowPlayingInfoCenter` 带上标题、节目名（标题里 ` · ` 后面的一段，没有就写「私人听」）、时长、进度，以及 `MPMediaItemPropertyArtwork`。

封面按这个顺序，在 **Mac 发布时** 写进 `episodes/<任务编号>/`，手机之后离线就能用：

1. 任务目录里已经有的 `cover.jpg` / `cover.png` / `artwork.jpg` 等图片。
2. YouTube oEmbed 的 `thumbnail_url`。创建任务或发布前补标题时会存进 `metadata.json`，发布时下载成 `cover.jpg`。
3. 用节目标题画一张卡片（本机有 ffmpeg 和中文字体时）。画不了就放一张纯色小图，手机锁屏改画标题卡片，不靠网络。

已经发布、目录里还没有封面的旧节目，锁屏会先显示手机按标题画的卡片。要换成视频缩略图，配好翻译密钥后对那一期再跑一次发布（不用重新合成），例如：

```bash
python3 scripts/studio_publish_icloud.py \
  --refresh-title \
  --job-dir "$HOME/Desktop/音频/中文播客工作台/jobs/6d898d4192de495fafcec86b9c938430"
```

验证：手机播放一集，锁屏和控制中心的「正在播放」应能看到封面，而不只是标题和进度。YouTube 那一期的 `episode.json` 里 `artwork_kind` 应为 `thumbnail`，旁边有 `cover.jpg`。

## 在 iPhone 上核对分享

1. Mac 先跑 `python3 scripts/studio_inbox.py`（或已经开着工作台，再用 `--no-run`）。
2. 手机 Safari 打开一条 YouTube 或 Substack 链接，分享 → 私人听。
3. Mac 上 `inbox/` 出现一条 json；监听打印「已排队」。工作室任务是 `run` + `edge`。
4. 同一条链接再分享一次，第二条 status 是 `duplicate`，不会再建任务。
5. 转换成功后，手机下拉，节目和封面出现在列表里。转换失败时 status 是 `failed`，监听不退出。
