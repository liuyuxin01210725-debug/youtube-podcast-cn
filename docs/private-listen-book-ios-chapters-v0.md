# 私人听 · 书听 iOS 章目录 / 续听 + 按章懒合成（v0 脚手架）

日期：2026-10-28（Asia/Shanghai）  
前置：PR#16 已合入 `codex/podcast-studio-20261018`（mergeCommit `ecba873`）。Mac 侧已有 `book.json` 章/块与 resume 字段；本 PR **只搭下一层**：手机章目录 + 续听光标，以及 Mac 按章懒合成的挂钩。不做完整产品。

## HO鑫确认（2026-10-28）

1. **真 Book 类型**（不是每章伪播客）
2. 试点：**英文书 → 译+朗读**（本脚手架先 EN 索引/队列/路径；长译流水线另续）
3. iCloud / 任务目录块音频路径确认：`chapters/<chapter_id>/<block_id>.mp3`
4. PR#17 **保持草稿**，继续接线；先不 merge / 不 ready-for-review

## 已接线（相对 v0 脚手架）

- App：`LibraryView` 书徽章 → `BookPlayerView` 章目录；打开书按 `BookResumeStore` 续听并 clamp
- 库扫描：`content_type=book` 或存在 `book.json` 即真书条目（不要求 `output.mp3`）
- Mac：`prepare_en_pilot` + `materialize_tiny_block` 证明 index→queue→路径；`execute=False`
- 夹具：`fixtures/book-en-pilot/`


## 目标（本脚手架）

私人听不止播客：英文书译+朗读、中文书听。长书不能整本一条；要借鉴微信读书式 **章目录 / 进度 / 续听**。  
**GEV 透明**：进度只写真实已合成/已听位置；没有音频就不假装「已听完」；懒合成队列状态可见（queued / running / ready / failed），不造假百分比。

## 章列表模型（App 侧）

一本 `content_type=book` 在库里仍可先以「书对象」出现（本脚手架用 `book.json` + 可选 `chapters/*.mp3`，不强制拆成伪播客多集）。

| 字段 | 含义 |
|---|---|
| `book_id` | 与任务/库条目 id 对齐（安全字符集同 EpisodeID） |
| `title` | 书名 |
| `language` | `zh` / `en` |
| `chapters[]` | `{ id, title, index, blocks[] }` |
| `blocks[]` | `{ id, index, chars, measure, audio_status, audio_file? }` |
| `progress` | `{ chapter_id, block_id, offset_chars, audio_seconds? }` |

`audio_status`：`missing` | `queued` | `running` | `ready` | `failed`。  
**没有 ready 的块：UI 显示「未合成」，禁止把进度条画成已听完。**

章目录解析：`scripts/studio_books.chapter_index(book)`（Python 契约）↔ iOS `BookChapterIndex.parse`（同 schema）。无 TOC 时 Mac 已按字数切块；App 只消费结果，不再二次切章。

## 续听光标

- 一等公民：`chapter_id` + `block_id` + `offset_chars`（字符偏移，对齐 Mac `book.json.progress`）。
- 可选：`audio_seconds`——仅当该块 `audio_status=ready` 且本地有 mp3 时写入；否则忽略，**不**用秒数冒充字符进度。
- 持久化：
  - 本机：`UserDefaults` 键 `PrivateListen.bookResume`（字典 book_id → cursor）
  - 库内副本（脚手架）：App Documents / 私人库下 `episodes/<id>/resume.json`（若目录可写）；与播客 `ListenMark` 分离，避免污染单集秒进度。
- **钳位（clamp）**：章/块不存在 → 落到第一章第一块 offset=0；offset 超出块长 → 夹到块末；负数 → 0。钳位是恢复用，不静默改写 Mac 任务里的权威 `book.json`（权威游标仍在 Mac；手机侧是本地续听）。

## 懒合成队列（Mac / studio）

挂钩模块：`scripts/studio_book_synth.py`。

| 动作 | 行为 |
|---|---|
| `enqueue_chapter` / `enqueue_block` | 写入 `synth_queue.json`；默认 `execute=False`，不触网 |
| `synthesize_block(..., execute=True)` | DeepSeek EN→CN（无 key 则 stub 原文）+ Edge TTS → `chapters/<c>/<b>.mp3` |
| `prepare_en_pilot(..., execute=True)` | 试点：建书 + 合成首块真 mp3（Mac；CI 仍 `execute=False`） |
| `audio_path` / `relative_audio_file` | 约定路径 `chapters/<chapter_id>/<block_id>.mp3` |

约束：

- **禁止**再走「整本一条 `output.mp3`」；`finish_book` 的拒绝逻辑保留。
- 默认 `execute=False`：单测/CI 离线。Mac 显式 `execute=True` 才烧 Edge / DeepSeek。
- 一次只合成用户即将听 / 正在听的章（或下一块），不预生成全书。

## App ↔ Mac studio 契约

```
Mac 任务目录/
  book.json          # 章/块 + progress（权威结构）
  document.json      # 单朗读者 segments
  synth_queue.json   # 懒合成队列（本 PR）
  chapters/c001/c001b001.mp3  # 按块音频（有则 ready）

iCloud / Documents「中文播客私人库」/
  episodes/<book_id>/
    episode.json     # content_type=book, file=null, whole_book_file=false
    book.json        # 与 Mac 任务同 schema（publish 必拷）
    chapters/<chapter_id>/<block_id>.mp3
    resume.json      # 手机侧续听（本机优先）

路径契约：`studio_publish_icloud.publish_book_job` / `copy_chapters_tree`
把 Mac 任务目录的 `book.json` + `chapters/` 树拷进上述 iCloud 布局；**不**写整本 `output.mp3`。
```

HTTP（工作台，后续接线；本 PR 只留 Python API）：

- `POST /api/jobs/<id>/book/synth` `{ "chapter_id" | "block_id" }` → enqueue
- `GET  /api/jobs/<id>/book` → book.json + queue 摘要

## 谁留在 Mac，谁在 iOS

| Mac / studio | iOS App |
|---|---|
| 切章、建 `book.json` | 章目录 UI 模型、列表展示 |
| 懒合成队列与 TTS（Edge，1 朗读者） | 续听光标读写与 clamp |
| 发布块音频到私人库 | 按块播放 / 缺音频时请求合成（后续） |
| 声纹跳过、人数=1 | 不跑声纹、不改广告硬闸 |

## 本 PR 交付 vs 刻意不做

**已落地（本 PR 续）**

- 真 Edge TTS（`execute=True`）+ DeepSeek EN→CN（无 key stub）
- iCloud publish 拷贝 `chapters/` + `book.json`
- iOS BookPlayer / Library 真 Book 类型；Discover 透镜与 #18 并存
- 单测 mock/`execute=False`；Mac 可选 `BOOK_SYNTH_SMOKE=1`

**仍 stub / 不做**

- HTTP book/synth API
- 整书预合成、epub/pdf 入库、真机装机冒烟
- 字偏移↔音频秒细对齐
- 未经询问就 merge / ready-for-review

## 下一人工决策（仍开放）

1. 首本真实英书稿与长译点头闸？
2. PR#17 何时 Ready / 合入？
