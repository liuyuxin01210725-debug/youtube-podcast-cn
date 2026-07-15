# YouTube 英文播客转中文音频

给一个带英文字幕的 YouTube 播客链接，自动完成：

```text
英文字幕 → DeepSeek / 通义千问翻译 → 说话人判断 → 中文 MP3
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
- 支持 DeepSeek 和通义千问，默认使用 DeepSeek。
- 按字幕 ID 严格校验，不允许静默缺段、重复或乱序。
- 翻译分块落盘，中断后重跑只补未完成部分。
- 切换供应商、模型或接口地址时自动隔离旧缓存。
- API Key 隐藏输入并保存到 macOS 钥匙串，不写入仓库或任务文件。
- EdgeTTS 默认免费合成，也可显式选择 Qwen3-TTS 高质量语音；任一音频片段失败时不发布残缺成品。

## 系统要求

当前公开版只支持 macOS，需要：

- Python 3.10 或更高版本
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

命令先生成中文逐字稿。完整的 Agent Skill 会继续判断主持人与嘉宾，并调用 EdgeTTS 生成 MP3。输出默认位于：

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

成功的音频片段会保存在最终输出目录的 `.tts-cache/`，重跑时自动复用，避免重复等待和重复计费。删除 `.tts-cache/` 是安全的，但之后再次运行会重新合成，并可能再次消耗额度。任何片段缺失、为空或无法解码时，程序都不会发布最终 MP3，也不会覆盖已有的完整音频。

## 模型配置

| 用途 | 供应商 / 后端 | 默认模型 | API Key 环境变量 | 可选覆盖 |
|---|---|---|---|---|
| 翻译 | DeepSeek | `deepseek-chat` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL` |
| 翻译 | 通义千问 | `qwen-plus` | `DASHSCOPE_API_KEY` | `QWEN_MODEL`、`QWEN_BASE_URL` |
| 语音 | Qwen3-TTS | `qwen3-tts-instruct-flash` | `DASHSCOPE_API_KEY` | `QWEN_TTS_MODEL`、`QWEN_TTS_BASE_URL` |

翻译参数只允许经过测试的 `deepseek` 和 `qwen`，语音参数只允许 `edge` 和 `qwen3-api`；不接受任意第三方接口。

## 测试

安装依赖后运行：

```bash
make test
```

测试覆盖字幕解析、翻译结果对账、模型缓存隔离、钥匙串入口、Skill 选择规则和 TTS 完整性。

## 使用边界

- 视频必须带可用的英文字幕；当前版本不下载原视频，也不做 Whisper 语音识别。
- 翻译 API 以及可选的千问语音费用由用户自己的 DeepSeek 或阿里云账户承担。
- 说话人由 Agent 根据上下文判断，偶尔可能判断错误；可以修改 `.speakers.txt` 后重新合成。
- 请只处理你有权访问和转换的内容，并遵守 YouTube 条款及相应版权要求。

## 安全

- 不要把 API Key 写进 `.env`、Skill、逐字稿、日志或命令参数。
- 不要提交 `~/Desktop/音频/.podcast-jobs/` 中的个人任务数据。
- 仓库不包含作者的 API Key、播客逐字稿或个人知识库内容。

## 许可证

MIT License。
