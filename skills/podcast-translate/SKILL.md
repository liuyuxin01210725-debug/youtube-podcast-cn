---
name: podcast-translate
description: "Use when the user wants to turn a YouTube English podcast or video into Chinese audio, synthesize an existing Chinese transcript into MP3, or explicitly requests Qwen3-TTS or high-quality Chinese speech. Trigger on: 把英文播客翻成中文, 转成中文 podcast, 英文音频翻译, podcast 翻译, 转中文音频, 千问翻译, 千问语音, Qwen3-TTS, 高质量音质, 把这个 YouTube 视频做成中文音频, 把中文逐字稿转成音频, translate this podcast."
---

# podcast-translate: YouTube URL / 中文稿 → 中文 mp3

YouTube URL 是默认主线:自动抓英文字幕 → 本地声纹区分 speaker → DeepSeek / 通义千问翻译 → EdgeTTS / 千问语音合成。翻译默认 DeepSeek，语音默认 Edge。**千问翻译**和**千问语音**是两个独立选择，不能因为用户只提到其中一个就同时切换两段流程。

| 用户表达 | 翻译选择 | 语音选择 |
|---|---|---|
| 未指定模型 | DeepSeek | Edge（默认） |
| “用千问翻译”“用千问把这个播客翻成中文” | `--provider qwen` | Edge |
| “千问语音”“Qwen3-TTS”“高质量音质”“用千问合成声音” | DeepSeek（URL 默认） | `--tts qwen3-api` |
| 明确要求千问翻译并用千问语音 | `--provider qwen` | `--tts qwen3-api` |

URL 翻译需要所选翻译模型的 API Key；已有中文稿只做 Edge TTS 时不需要 API，使用千问语音时需要百炼 Key 并消耗用户额度。

**核心设计(2026-07)**:Agent **不重写、不长篇翻译、不清理** transcript 正文。翻译模型按稳定字幕 ID 分批翻译并落盘；YouTube URL 优先直接使用本地声纹生成的 speaker 序列，只有明确回退时 Agent 才逐段判断。文本清理交给 Python。任何翻译缺段或 TTS 片段失败都会中止，不发布残缺 MP3。

---

## ⚠️ 必读:为什么 Claude 不许重写文本

旧版让 Claude 把整篇中文 transcript 重写一遍(清理 + 加标签 + 精修)。后果:单次响应要输出几万字,跨境网络下 socket 撑不住,频繁报 `API Error: socket connection closed unexpectedly`。分块到 6000 字一块也照样断。

**根因**:只要 Claude 需要"长时间处理 + 长输出",跨境长连接就会被中间网络掐断。这不是 prompt 能解决的。

**解法(speaker 序列模式)**:
- Claude 唯一的工作 = 判断每段 speaker,输出一个几百字符的"序列文件"
- 文本清理 = Python 脚本的 `strip_markdown` 自动做
- 贴 speaker 标签 = Python 按序列文件自动贴
- Claude 全程不输出长文本 → 不会 socket 超时

**代价(必须如实告诉用户)**:这个模式下,以下精修**不做** —— 术语统一、度量单位换算、翻译腔修复。因为这些都要 Claude 重写文本,会走回超时的老路。这是物理取舍,不是偷懒。

---

## ⚠️ 必读:读大文件也会断(ECONNRESET)

上一节讲 Claude **输出**长文本会断。还有一个对称的雷区:Claude **读入**大文件时,文件内容会变成一个超大的 tool-result 请求,跨境网络下同样被掐断 —— 报 `API Error: Unable to connect to API (ECONNRESET)`,然后卡死在重试。

**会中招的两个文件**:
- 原稿 `.md` —— 逐字稿动辄上千行
- 段落清单 `.segments.txt` —— 几百段时几十 KB

**铁律:读这两个文件一律分块,一次 Read 最多 250 行,用 offset/limit 翻完。** 绝不一次 Read 整个大文件。这条贯穿第 1、3、4 步。

(脚本侧也已配合:`--dump-segments` 把清单写成文件、首句只留 28 字,不再整张打到 stdout。)

---

## 输入识别(第 0 步)

按输入直接分流，不要让用户再手工下载字幕或打开翻译网页:

1. **YouTube URL** → 走“路径 U”，这是默认主线。
2. **已有中文 `.md` / `.txt`** → 走“路径 A”。
3. **直接贴出的中文文本** → 先存到 `~/Desktop/音频/<标题>.md`，再走“路径 A”。
4. **本地英文文件** → 走“路径 B”。URL 与本地文件不要混为一谈。

## 路径 U:YouTube URL 自动准备(默认主线)

### U1. 运行字幕抓取和模型翻译

直接执行下列已安装工具，必须把用户给出的准确 URL 原样传入，禁止改成搜索词:

用户没有指定模型时，默认使用 DeepSeek:

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/podcast-cn.sh" "$YOUTUBE_URL"
```

上述默认命令隐式使用 `--diarize auto`，用户无需增加任何说法。只在用户明确表达时追加：

| 用户表达 | 追加参数 |
|---|---|
| “不要分析原音频” | `--diarize off` |
| “必须准确区分说话人” | `--diarize required` |
| “这是两个人的播客” | `--speaker-count 2` |

参数可与 `--provider qwen` 合并，但不要自行猜测人数。

用户明确要求“千问翻译”或“用千问把播客翻成中文”时，选择千问翻译:

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/podcast-cn.sh" "$YOUTUBE_URL" --provider qwen
```

这里的 `--provider` 只控制翻译，不控制语音。当前只支持 DeepSeek 和通义千问这两种翻译供应商。用户要求 Kimi、豆包、MiniMax、智谱或其他平台时，说明尚未加入该预设；不要偷偷映射到 DeepSeek 或千问，也不要把任意接口地址当成已验证平台。

脚本优先抓人工英文字幕，没有时才用英文自动字幕。默认会临时下载原音频做本地声纹分析；成功、失败或中断后都删除，不会保留。记住 stdout 中的 `中文字幕:` 路径，并检查是否同时有 `说话人序列:`。

如果提示还没有配置 API Key，让用户在自己的终端运行对应命令，并在隐藏输入提示中粘贴 Key。

DeepSeek（环境变量 `DEEPSEEK_API_KEY`）:

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/setup-podcast-model.sh" deepseek
```

通义千问翻译或千问语音（共用环境变量 `DASHSCOPE_API_KEY`）:

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/setup-podcast-model.sh" qwen
```

Key 会进入各自的 macOS 钥匙串，后续翻译或 TTS 脚本按需自动读取。**不要让用户把任何翻译 API Key 或 TTS API Key 粘贴到对话、稿件、文件或命令参数里**。如果网络或翻译中断，修复原因后原命令重跑，同一视频会从 `~/Desktop/音频/.podcast-jobs/$VIDEO_ID/` 的已完成批次继续。

### U2. 接入 speaker 与 TTS

先把 U1 输出的 `中文字幕:` 路径按路径 A 的第 1-2 步定标题并复制到音频目录。内部 `<!-- seg-000001 -->` 注释复制时保留，TTS 会自动剥除。然后严格按 stdout 分流：

1. 有 `说话人序列:`：记住该文件路径，**跳过路径 A 的第 3-4 步**，不再读全稿、不再 dump，也不人工逐段判断。直接执行第 5 步，将该路径传给 `bin/tts-cn.sh --speakers` 。
2. 有 `已回退到内容判断` 或 `说话人识别:已关闭`：按路径 A 的第 3-5 步执行现有 Agent speaker 判断流程，不得伪称已由声纹识别。
3. `--diarize required` 报错：停止翻译/TTS 后续，告知用户错误摘要和恢复命令 `bash "__PODCAST_TRANSLATE_ROOT__/bin/setup-podcast-diarization.sh"`。

自动序列的 Edge 命令形状必须是：

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/tts-cn.sh" \
  "$HOME/Desktop/音频/<中文标题>.md" \
  --speakers "$GENERATED_SPEAKERS" --tts edge
```

用户明确要千问语音时，只把末尾换成 `--tts qwen3-api --style conversation`。

生成的 `说话人甲-女声1` / `说话人乙-男声1` 中，“男声/女声”只是合成音色，不代表工具识别了原说话人的性别。如果有可靠的公开姓名或角色，可编辑说话人序列同目录下的 `speaker-map.json`；重跑 U1 会保留用户编辑并重生序列。

---

## 路径 A:已有中文稿的 speaker 序列流程

全程 6 步。**Claude 不重写任何正文,一个字都不改。**

### 第 1 步:读开头,定标题和角色

Read 原稿前 100-150 行。确定:
- **标题**:提炼成纯中文文件名(无空格、无特殊符号)。例:`Claude Code 当第二大脑`
- **主持人 / 嘉宾是谁、性别**:从节目介绍、人名推断。判断不准就用默认"主持人-女 + 嘉宾-男"。

### 第 2 步:复制原稿到音频目录

不改一个字,直接复制:

```bash
cp "<原始逐字稿路径>" "$HOME/Desktop/音频/<中文标题>.md"
```

原稿带时间戳、frontmatter、章节标题、`\[音乐\]` 标注都没关系 —— Python 会自动清理。

### 第 3 步:dump 段落清单

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/tts-cn.sh" "$HOME/Desktop/音频/<中文标题>.md" --dump-segments
```

脚本把段落清单(每段"编号 + 首句28字")写到 `<中文标题>.segments.txt`,**stdout 只回两行:清单文件路径 + 总段数 N**。记住这个 N。

清单不再走 stdout 整张打印 —— 段数多时那是 100KB+ 的 tool-result,Claude 读它必触发 `ECONNRESET`(见上方必读)。清单留到第 4 步分块 Read `.segments.txt`。

### 第 4 步:判断每段 speaker → 写序列文件

这是 Claude 唯一的判断工作。

1. **分块 Read** 第 2 步复制的 `<中文标题>.md`(通读,理解谁在说话)。逐字稿动辄上千行,**一次 Read 最多 250 行**,用 offset/limit 翻完 —— 一次读整个大文件会触发 `ECONNRESET`(见上方必读)。
2. **分块 Read** 第 3 步的 `<中文标题>.segments.txt`(共 N 段),同样每次 ≤ 250 行。对照逐段判断是主持人还是嘉宾。
3. **Write** 序列文件 `$HOME/Desktop/音频/<中文标题>.speakers.txt`,内容是**正好 N 行**,每行一个标签:

```
[主持人-女]
[嘉宾-男]
[嘉宾-男]
[主持人-女]
```

第 i 行 = `.segments.txt` 里第 i 号段的 speaker。标签格式见下方"Speaker 标记规则"。

4. **Write 完立刻自检行数**(强制,不可跳过):

```bash
wc -l "$HOME/Desktop/音频/<中文标题>.speakers.txt"
```

必须 = N。不齐就改到对齐再进第 5 步。脚本虽然也会校验、不齐就报错退出,但那是把报错甩给用户 —— 自己先 `wc -l` 挡掉。

要点:
- **行数必须 = N**。段数多、分多轮判断时极易多写或漏写一行,所以第 4 点的 `wc -l` 自检是强制步骤。
- 这一步 Claude 的输出只有 N 行标签(几百字符),**不会 socket 超时**。
- 段数 > 150 时,可以分几轮判断(每轮在对话里先列一批),全部判断完后**一次性 Write 完整序列**。Write 始终只有一次、很短。
- 判断技巧见下方"speaker 判断技巧"。

### 第 5 步:跑 TTS

默认免费稳定模式（Edge）:

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/tts-cn.sh" "$HOME/Desktop/音频/<中文标题>.md" \
  --speakers "$HOME/Desktop/音频/<中文标题>.speakers.txt" --tts edge
```

只有用户明确说“千问语音”“Qwen3-TTS”“高质量音质”或“用千问合成声音”时，使用高质量模式:

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/tts-cn.sh" "$HOME/Desktop/音频/<中文标题>.md" \
  --speakers "$HOME/Desktop/音频/<中文标题>.speakers.txt" \
  --tts qwen3-api --style conversation
```

Qwen 多人播客默认使用三个经过实际调用验证的男性音色：`male_1=Vincent`、`male_2=Neil`、`male_3=Eldric Sage`。程序会在付费调用前检查已知系统音色的性别槽与模型兼容性；不兼容时必须停止并修正配置，不能带错音色继续生成。

文章、论文或单人知识讲解的千问语音使用 `--style documentary`。用 `run_in_background: true` 跑,然后用 Monitor 看进度,不要阻塞。脚本自动:清理文本 → 按序列贴标签 → 语音合成 → pydub 拼接 → ffmpeg 修 mp3 header → 输出 `<中文标题>.mp3`。

成功片段会缓存到输出目录的 `.tts-cache/` 并在重跑时复用。删除缓存是安全的，但千问语音会重新合成并再次消耗额度。只要有片段缺失、为空或无法解码，脚本就不会发布或覆盖最终 MP3。

### 内部 A/B 试听（仅验证音质时）

`--preview-paragraphs N` 只用于短试听和 Edge/Qwen A/B，不加入正常全量流程。两边必须使用同一份稿件、speaker 文件、段数和 style，例如:

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/tts-cn.sh" "$HOME/Desktop/音频/<中文标题>.md" \
  --speakers "$HOME/Desktop/音频/<中文标题>.speakers.txt" \
  --tts qwen3-api --style conversation --preview-paragraphs 10
```

### 第 6 步:报告

见下方"报告格式"。

### speaker 判断技巧

原稿(YouTube 字幕 / 沉浸式翻译输出)通常没有显式 speaker 标记,靠内容判断:
- 提问、引导、"欢迎来到节目"、"我们今天聊聊…" → 多半是主持人
- 长篇阐述、回答、"谢谢邀请" → 多半是嘉宾
- 第一人称经历 → 看是谁的经历
- 对谈通常一问一答交替,但同一人可以连续说多段
- 拿不准 → 按对话逻辑选最合理的,不要卡住

判断会有错,正常。用户听完发现声音对不上,改 `.speakers.txt` 对应行,重跑第 5 步即可(不用重新 dump)。

---

## 路径 B:本地英文文件——不在本次自动化范围

本地英文 transcript 若让 Agent 直接翻整篇，会产生长输出并容易 socket 超时。不要硬翻，也不要误称 YouTube URL 不支持——URL 必须回到路径 U。

遇到英文输入,**不要硬翻**。告诉用户:

> 这个输入是本地英文文件，不是 YouTube URL。当前自动化只负责 URL；请先用沉浸式翻译把本地文件转成中文稿，或提供对应的 YouTube URL。

沉浸式翻译模型推荐见下方"预翻译推荐"。

只有用户明确说"我知道会报错,还是要你翻"时,才考虑分小块硬翻 —— 但要提前说明大概率失败。

---

## Speaker 标记规则

**标签必须带性别后缀**,TTS 靠标签里的"男/女"字分声音池。

本地声纹自动生成的编号标签可用 `女声1/女声2/男声1/男声2`。这些是合成音色编号，不是身份或性别推断。以下姓名与性别规则只用于 Agent 内容判断回退路径。

格式:`[主持人-女]` / `[嘉宾-男]`。也可以真名 + 性别:`[Rhonda-女]` / `[Brady-男]`。

**多嘉宾(2 人以上)** 用甲乙丙丁区分,每个带自己的性别:`[嘉宾甲-男]` `[嘉宾乙-女]`。TTS 自动给同性别的多个 speaker 轮换声音；男声池有 3 个音色、女声池有 2 个音色。

**性别判断信号(强 → 弱)**:
1. 明确人名:Rhonda Patrick(女)、Brady Holmer(男)、Peter Attia(男)、Kelly Starrett(男)、Andrew Huberman(男)、Sara Gottfried(女)
2. 节目元信息(描述、章节标题)里的姓名
3. 称呼线索:"Dr. Patrick"(女)、"my wife"(说话人是男)
4. 找不到线索 → 默认"主持人-女 + 嘉宾-男"(健康类播客最常见)

序列文件里**每段一行**,即使同一 speaker 连续多段,每行都要写标签。

---

## 通用:文件命名 / 报告

### 文件命名

纯中文,无空格。例:"Is fasting beneficial for longevity?" → `禁食对长寿有益吗`。

一个 podcast 产生 4 个文件,都在 `~/Desktop/音频/`:
- `<中文标题>.md` — 原稿副本(第 2 步复制)
- `<中文标题>.segments.txt` — 段落清单(第 3 步 `--dump-segments` 自动生成)
- `<中文标题>.speakers.txt` — speaker 序列(第 4 步写)
- `<中文标题>.mp3` — 最终输出(第 5 步脚本自动生成)

### 报告格式

跑完后给用户:

```
✅ 完成
- mp3: ~/Desktop/音频/<标题>.mp3
- 时长: X 分 XX 秒  /  大小: X.X MB
- 段数: N  /  失败: M
- 手机播放: AirDrop 或微信传输助手发到手机,微信播放器自带 0.75x-2x 倍速
```

提醒用户:speaker 判断可能有错,听到声音对不上就改 `.speakers.txt` 对应行,重跑第 5 步。

---

## 可选环境变量(用户显式要求时用)

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `VOICE_MALE_1` | `zh-CN-YunyangNeural` | 男声 A(新闻腔) |
| `VOICE_MALE_2` | `zh-CN-YunjianNeural` | 男声 B(运动解说腔,活力) |
| `VOICE_FEMALE_1` | `zh-CN-XiaoxiaoNeural` | 女声 A(通用) |
| `VOICE_FEMALE_2` | `zh-CN-XiaoyiNeural` | 女声 B(温暖) |
| `VOICE_DEFAULT` | `= VOICE_FEMALE_1` | 无 speaker 标记时用 |

千问语音可用 `QWEN_TTS_MODEL`、`QWEN_TTS_BASE_URL`、`QWEN_TTS_CONCURRENCY` 和 `QWEN_TTS_VOICE_MALE_1` 等变量覆盖默认配置；只在用户明确要求调整时使用。

千问默认音色：`QWEN_TTS_VOICE_MALE_1=Vincent`、`QWEN_TTS_VOICE_MALE_2=Neil`、`QWEN_TTS_VOICE_MALE_3=Eldric Sage`、`QWEN_TTS_VOICE_FEMALE_1=Cherry`、`QWEN_TTS_VOICE_FEMALE_2=Serena`。不要把 `Dylan` 配给默认的 `qwen3-tts-instruct-flash`；该组合不在官方支持范围内，程序也会拒绝。

`TTS_CONCURRENCY`(默认 8)、`TTS_RETRIES`(默认 3)、`PAUSE_SAME_MS`(200)、`PAUSE_SWITCH_MS`(500)。

如果用户事后发现"性别反了"想临时翻转,不用改 `.speakers.txt`,直接换环境变量跑第 5 步:

```bash
VOICE_MALE_1=zh-CN-XiaoxiaoNeural VOICE_FEMALE_1=zh-CN-YunyangNeural \
  bash "__PODCAST_TRANSLATE_ROOT__/bin/tts-cn.sh" "$HOME/Desktop/音频/xxx.md" --speakers "$HOME/Desktop/音频/xxx.speakers.txt"
```

列所有中文 voice:`"__PODCAST_TRANSLATE_ROOT__/.venv/bin/edge-tts" --list-voices | grep zh-CN`

---

## 相关文件

- `__PODCAST_TRANSLATE_ROOT__/bin/podcast-cn.sh` — 单 URL 准备入口
- `__PODCAST_TRANSLATE_ROOT__/bin/setup-podcast-model.sh` — 把 DeepSeek / 千问 Key 安全保存到各自的 macOS 钥匙串
- `__PODCAST_TRANSLATE_ROOT__/bin/setup-podcast-deepseek.sh` — DeepSeek 初始化兼容入口
- `__PODCAST_TRANSLATE_ROOT__/bin/setup-podcast-qwen.sh` — 通义千问初始化快捷入口
- `__PODCAST_TRANSLATE_ROOT__/bin/setup-podcast-diarization.sh` — 提前安装或修复本地声纹运行时
- `__PODCAST_TRANSLATE_ROOT__/scripts/podcast_cn.py` — 字幕抓取、VTT 解析、多模型分批翻译和断点续跑
- `__PODCAST_TRANSLATE_ROOT__/bin/tts-cn.sh` — bash 入口(参数透传给 python)
- `__PODCAST_TRANSLATE_ROOT__/scripts/tts-cn.py` — 核心脚本(`--dump-segments` / `--speakers` / `--tts`)
- `__PODCAST_TRANSLATE_ROOT__/scripts/tts_backends.py` — Edge 与 Qwen3-TTS 后端
- `__PODCAST_TRANSLATE_ROOT__/scripts/tts_cache.py` — 可解码音频片段缓存
- `__PODCAST_TRANSLATE_ROOT__/.venv/` — 项目 Python 隔离环境(edge-tts + DashScope + pydub + audioop-lts + yt-dlp)
- `~/Desktop/音频/` — 所有输出统一在这里

---

## 用户偏好的预翻译推荐(回答用户"我该用什么工具翻"时)

用户用沉浸式翻译 Pro 会员,模型按推荐排序:
1. **DeepSeek-V4-Flash + 意译大师**(首选,中文最自然)
2. **Qwen 3.5 Plus + 意译大师**(并列首选)
3. GLM-4.7(次选)
4. Gemini 3 Flash(次选)
5. GPT-5 mini(质量一般)
6. DeepL Pro(❌ 不推荐,翻译腔最重)

输出模式选**仅中文**(不要双语对照)。

---

## 自检 + 问题日志

问题日志在 `__PODCAST_TRANSLATE_ROOT__/skills/podcast-translate/ISSUES.md`。

### 触发条件(任一满足就在 ISSUES.md "未修复"区追加一条)

| 触发器 | 判断方式 |
|---|---|
| TTS 失败率 > 5% | 看脚本输出的 `失败 N` 占 `片段 M` 的比例 |
| 重试率 > 10% | 看 `重试成功 N` 占 `片段 M` 的比例 |
| 实时倍率 < 5x | 看 `实时倍率 N.Nx`,可能被微软限速 |
| dump 出来发现脏东西没清掉 | 段落首句里还残留时间戳/标注/链接等,`strip_markdown` 漏了 |
| Speaker 判断错很多 | 用户反馈大量声音对不上 |
| 输出文件异常 | 段数 > 100 但 mp3 < 5MB |
| 读文件报 ECONNRESET | Read `.md`/`.segments.txt` 报 `API Error ... ECONNRESET` → 分块不够小,**必须记** |
| 用户反馈错误 | 用户说"性别反了""漏了""听不清" → **必须记** |

### 记录格式

```
### YYYY-MM-DD HH:MM · <podcast 标题>

**类型**:TTS-失败率 / 清理-残留 / Speaker-判断 / 性能-限速 / 文件-体积 / 用户反馈
**症状**:具体现象(不超过 2 句)
**位置**:scripts/tts-cn.py:行号 / SKILL.md:段落名(定位得到的话)
**候选修复**:具体怎么改
**严重度**:高 / 中 / 低
```

### 报告时附带

触发了记录,在报告里加一行:`⚠ 本次记录到 ISSUES.md:<类型>。累计未修复 N 条。`

累积 ≥ 3 条同类问题 → 主动建议一起改。

不要做:每次都写 / 写主观条目 / 写完不告诉用户 / 自作主张改脚本(只记录,改动要用户确认)。

---

## 不要做的事

- ❌ 不要重写 / 翻译 / 清理 transcript 正文 —— 一个字都不改,清理交给 Python
- ❌ 不要输出整篇带标签的 md —— 只 Write N 行纯标签的 `.speakers.txt`
- ❌ 不要一次 Read 整个大 `.md` / `.segments.txt` —— 分块,每次 ≤250 行,否则跨境 ECONNRESET
- ❌ 序列文件行数 ≠ dump 出的段数 N —— Write 后必须 `wc -l` 自检,脚本也会报错
- ❌ 本地英文文件不要让 Agent 硬翻；YouTube URL 必须走自动路径 U
- ❌ 不要用浏览器自动操作沉浸式翻译，也不要调用它的非公开 API
- ❌ 不要把任何翻译或 TTS API Key（包括 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`）写进稿件、任务状态、日志或命令参数
- ❌ 不要修改用户其他项目的规则或资料文件(工具归工具,资料归资料)
- ❌ 不要把 md / mp3 输出到 `raw/` 或 `wiki/`
- ❌ 不要问用户"放哪个目录" —— 默认就 `~/Desktop/音频/`
- ❌ 不要假设输入语言 —— 先按第 0 步判断
