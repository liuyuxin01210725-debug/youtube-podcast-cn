# 中文播客工作台

双击 [bin/中文播客工作台.command](bin/中文播客工作台.command)，在浏览器打开本机中文界面。支持批量 YouTube 链接及本地音频/视频，完整英文识别、说话人区分、中文翻译、译稿编辑、可辨系统音色（默认）、可选原声克隆、短试听与完整 MP3。任务串行执行，适合 Apple Silicon / 16 GB Mac。

## 工作台使用

1. 粘贴每行一个 YouTube 链接，或选择本地音视频（每个最多 2 GB）。
2. 新建任务默认使用「可辨音色」（系统声槽，每人一槽可改）；原声克隆为可选。选择「准备译稿」，校对左右对应的英文和中文，必要时调整说话人或音色槽；也可直接完整转换。现有任务可在可辨音色与克隆之间切换，保留译稿与来源。
3. 保存后试听（优先覆盖不同说话人），再生成完整 MP3。试听和完整成品分别展示与下载，双语稿也可下载。**验收：** 试听几段不同说话人，声色不混就算过。若说话人切分不准，界面会提示待核对段落，改正并保存后再生成（不会悄悄混声）。
4. 取消后可继续；重启后中断任务会保留，手动继续。修改译稿或切换声音后旧音频仍保留，并按试听/完整成品分别标记上一版；重新准备译稿也会重新核对音频版本。删除任务会删除上传副本、声音参考、缓存与成品。

任务默认保存在 `~/Desktop/音频/中文播客工作台/jobs`。界面只使用本机 `127.0.0.1:8765`，关闭启动窗口会停止服务；不需要浏览器账号。YouTube 可能返回 429 或要求登录，此时请等待或导入自己已下载的文件。工具不自动读取浏览器 cookies。

**首次安装：** 先安装 Python 3.12 和 ffmpeg（例如 `brew install python@3.12 ffmpeg`），运行 `bash bin/setup-studio.sh`。安装脚本创建独立运行环境并下载固定版本的 MLX Whisper small.en 与 Qwen3-TTS 0.6B Base 8bit 模型，首次需要网络、数 GB 磁盘及等待时间；正常启动不会重复安装。翻译密钥使用现有 `bin/setup-podcast-deepseek.sh` 或 `bin/setup-podcast-qwen.sh` 配置，只保存在环境或 macOS 钥匙串，不进入网页或任务配置。

本地识别和克隆不按次收费；翻译需联网且可能产生供应商费用。默认可辨系统音色（Edge）也需联网。长音频通常需较长时间，界面显示真实阶段和已处理段数，不承诺实时速度。模型能力、原声质量与分段均影响效果，不保证与专业播客相同。完整转换不做摘要或自行增加对话。说话人编号是匿名槽位，系统声音名称不是身份或性别推断。原声参考会持久保存在任务中以便断点复用；仅处理你有权使用的素材。

开发启动：`.venv/bin/python scripts/studio_server.py`。可用 `--jobs /absolute/path` 指定任务根目录，`--port 8765` 指定端口。引擎接口见 [docs/STUDIO-ENGINE.md](docs/STUDIO-ENGINE.md)。工作台不提供任意本地文件读取接口。

## 私人听（只给自己的 iPhone）

完整 MP3 生成成功后，工作台会把这一期复制到 iCloud Drive 的「中文播客私人库」。试听不会发布。复制失败只打日志，任务仍然算成功。路径、清单格式和补发命令见 [docs/private-listen.md](docs/private-listen.md)。

在 Mac 上打开 [ios/PrivateListen/PrivateListen.xcodeproj](ios/PrivateListen/PrivateListen.xcodeproj)：

1. 选中 PrivateListen 目标，打开 Signing & Capabilities，Team 选你的个人开发团队。Bundle id 已是 `com.liuyuxin.privatelisten`。
2. 确认有 iCloud → iCloud Documents，容器为 `iCloud.com.liuyuxin.privatelisten`。
3. 运行目标选这台 iPhone「不取经的小刘」，然后 Run。模拟器只用来确认工程能编译；收听用这台手机。
4. 第一次打开点「选择资料库」，在 iCloud Drive 里选中「中文播客私人库」。之后下拉或重新打开即可看到新节目。没有登录，也不访问互联网。

已有成品（例如 Creatine，任务 `6d898d4192de495fafcec86b9c938430`）不用重跑合成，按文档里的命令补发一次即可。

以下为保留的旧版命令行 / Agent Skill 文档，其字幕回退策略不代表工作台引擎；工作台的说话人区分失败会明确停止，避免悄悄改变声音方案。

---

# YouTube 英文播客转中文音频

给一个带英文字幕的 YouTube 播客链接，自动完成：

```text
英文字幕 → 本地说话人识别 → DeepSeek / 通义千问翻译 → 中文 MP3
```

它同时是一套可安装到 Codex 或 Claude Code 的 Agent Skill。安装后可以直接说：

```text
把这个 YouTube 播客转成中文音频：https://www.youtube.com/watch?v=...
```

或者：

```text
用通义千问把这个 YouTube 播客转成中文音频：https://www.youtube.com/watch?v=...
```

## 功能

- 自动获取 YouTube 英文人工字幕；没有时回退到英文自动字幕。
- 默认用本地 `sherpa-onnx` 按原音频时间轴区分匿名说话人，失败时整集回退到内容判断。
- 支持 DeepSeek 和通义千问，默认使用 DeepSeek。
- 按字幕 ID 严格校验，不允许静默缺段、重复或乱序。
- 翻译分块落盘，中断后重跑只补未完成部分。
- 切换供应商、模型或接口地址时自动隔离旧缓存。
- API Key 隐藏输入并保存到 macOS 钥匙串，不写入仓库或任务文件。
- EdgeTTS 默认免费合成，也可显式选择 Qwen3-TTS 高质量语音；Qwen 会在付费调用前校验音色性别和模型兼容性，任一音频片段失败时不发布残缺成品。

## 系统要求

当前公开版只支持 macOS arm64（Apple Silicon），需要：

- Python 3.10–3.14
- `ffmpeg`（没有时可运行 `brew install ffmpeg`）
- Codex 或 Claude Code（二选一即可；只用命令行也可以）

## 安装

```bash
git clone https://github.com/liuyuxin01210725-debug/youtube-podcast-cn.git
cd youtube-podcast-cn
bash install.sh
```

安装器会在当前仓库创建 `.venv`，安装固定版本依赖，并把 Skill 安装到：

- `~/.codex/skills/podcast-translate/`
- `~/.claude/skills/podcast-translate/`

安装后重新打开 Codex 或 Claude Code，让它重新发现 Skill。

普通安装只准备翻译和 TTS 主环境，不会立即下载声纹依赖。首次处理 YouTube 链接时，工具会懒创建独立的 `.diarization-venv` 并下载约 47 MB 的可复用模型。需要提前安装或修复时运行：

```bash
bash bin/setup-podcast-diarization.sh
```

## 配置模型 Key

翻译默认使用 DeepSeek，因此初次使用至少配置一个翻译模型。千问翻译和千问语音共用阿里云百炼 Key。Key 会通过隐藏输入进入 macOS 钥匙串，请不要把 Key 粘贴到聊天中。

DeepSeek：

```bash
bash bin/setup-podcast-model.sh deepseek
```

通义千问（阿里云百炼）：

```bash
bash bin/setup-podcast-model.sh qwen
```

若接口提示余额或额度不足，请先在对应供应商控制台充值或开通额度，再原命令重试；不要为排错把 Key 粘贴到聊天、Issue 或日志中。

## 直接使用命令行

默认 DeepSeek：

```bash
bash bin/podcast-cn.sh "https://www.youtube.com/watch?v=..."
```

使用通义千问翻译：

```bash
bash bin/podcast-cn.sh "https://www.youtube.com/watch?v=..." --provider qwen
```

说话人识别默认为 `--diarize auto`：本地分析可用就生成说话人序列，不可用则整集安全回退。也可显式选择：

```bash
# 默认：自动识别，失败则回退
bash bin/podcast-cn.sh "$URL" --diarize auto

# 不下载、不分析原音频
bash bin/podcast-cn.sh "$URL" --diarize off

# 必须成功识别，否则在翻译/TTS 前停止
bash bin/podcast-cn.sh "$URL" --diarize required

# 已知是两人对谈
bash bin/podcast-cn.sh "$URL" --speaker-count 2
```

声纹分析完全在本机运行，不消耗翻译 token、DeepSeek 额度或阿里云百炼额度。工具会临时下载用户指定视频的原音频，转为 16 kHz WAV 后分析；无论成功、失败还是中断，临时原音频都会删除，不会保留。长期只保留匿名说话人时间段、字幕对齐和可编辑音色映射等小型元数据。

命令会生成中文逐字稿；声纹对齐成功时还会生成与段落一一对应的说话人序列。Agent Skill 直接把该序列交给 EdgeTTS 或 Qwen3-TTS，无需人工逐段标注。输出默认位于：

```text
~/Desktop/音频/
```

## 语音质量模式

EdgeTTS 仍是默认：免费、稳定，而且不需要 TTS API Key。只有明确选择 `qwen3-api` 时，才会调用千问语音并消耗用户自己的阿里云百炼额度。

```bash
# 免费稳定模式，也是默认值
bash bin/tts-cn.sh transcript.md --speakers transcript.speakers.txt --tts edge

# 千问高质量访谈模式，复用已有的百炼 Keychain 配置
bash bin/tts-cn.sh transcript.md --speakers transcript.speakers.txt \
  --tts qwen3-api --style conversation

# 文章或论文的平静讲解模式
bash bin/tts-cn.sh article.md --tts qwen3-api --style documentary
```

`--provider qwen` 只选择千问翻译，`--tts qwen3-api` 只选择千问语音，两者相互独立。比如“用千问翻译这个播客”会使用千问翻译和默认 Edge 语音；只有同时明确选择两个参数，翻译和语音才都会走千问。

多人播客的 Qwen 默认男性音色为 `Vincent`、`Neil`、`Eldric Sage`，分别用于男声 1、2、3。三者均已用默认 `qwen3-tts-instruct-flash` 实际调用验证，并会额外使用明确的成年男性表达指令。已知女性系统音色不能配置到男性音色槽；已知不受当前模型支持的音色会在调用前报错，避免生成整期后才发现声音不对。

成功的音频片段会保存在最终输出目录的 `.tts-cache/`，重跑时自动复用，避免重复等待和重复计费。删除 `.tts-cache/` 是安全的，但之后再次运行会重新合成，并可能再次消耗额度。任何片段缺失、为空或无法解码时，程序都不会发布最终 MP3，也不会覆盖已有的完整音频。

## 模型配置

| 用途 | 供应商 / 后端 | 默认模型 | API Key 环境变量 | 可选覆盖 |
|---|---|---|---|---|
| 翻译 | DeepSeek | `deepseek-chat` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL` |
| 翻译 | 通义千问 | `qwen-plus` | `DASHSCOPE_API_KEY` | `QWEN_MODEL`、`QWEN_BASE_URL` |
| 语音 | Qwen3-TTS | `qwen3-tts-instruct-flash` | `DASHSCOPE_API_KEY` | `QWEN_TTS_MODEL`、`QWEN_TTS_BASE_URL`、`QWEN_TTS_VOICE_*` |

翻译参数只允许经过测试的 `deepseek` 和 `qwen`，语音参数只允许 `edge` 和 `qwen3-api`；不接受任意第三方接口。

## 测试

安装依赖后运行：

```bash
make test
```

测试覆盖字幕解析、翻译结果对账、模型缓存隔离、钥匙串入口、Skill 选择规则和 TTS 完整性。

## 使用边界

- 视频必须带可用的英文字幕；当前版本不保留原视频，也不做 Whisper 语音识别。
- 翻译 API 以及可选的千问语音费用由用户自己的 DeepSeek 或阿里云账户承担。
- 本地模型只能区分匿名声音簇，不能凭声音确认姓名、身份或生理性别。交叉说话、背景音乐和很短的插话可能误判。
- `男声` / `女声` 标签只表示合成音色，不是对原说话人性别的识别。可编辑 `speaker-map.json` 后重跑生成序列。
- 请只处理你有权访问和转换的内容，并遵守 YouTube 条款及相应版权要求。

## 安全

- 不要把 API Key 写进 `.env`、Skill、逐字稿、日志或命令参数。
- 不要提交 `~/Desktop/音频/.podcast-jobs/` 中的个人任务数据。
- 仓库不包含作者的 API Key、播客逐字稿或个人知识库内容。

## 许可证

项目代码使用 MIT License。本地声纹运行时 `sherpa-onnx` 使用 Apache-2.0；`pyannote/segmentation-3.0` 模型使用 MIT；3D-Speaker ERes2Net 嵌入模型使用 Apache-2.0。请同时遵守上游许可证。
