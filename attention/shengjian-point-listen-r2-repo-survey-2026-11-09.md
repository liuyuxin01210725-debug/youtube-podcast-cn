# 声间 · 点段跳听 R2 仓库契约调查（只读）

- **日期**：2026-11-09（Asia/Shanghai）
- **分支**：`cursor/shengjian-point-listen-contract-survey-e49b`（基于 `origin/fix/shengjian-unfinished-mark-heard` @ `7d8403d`）
- **调查范围**：Git 仓内路径证据；**未**拉云/OSS、**未**读 Norton 真机目录、**未**跑真机/UI 自动化、**未**改 App 行为
- **CURRENT tip 参照**：`attention/shengjian-CURRENT-tip-note-2026-11-04.md`（continue-shortplay 装机包）；源码 tip 以 `fix/shengjian-unfinished-mark-heard` 线为最新合入（含 PR#28 短播 Continue + PR#29）
- **说明**：`main` 仅有播客翻译流水线，**无** `ios/PrivateListen`；下列路径均存在于上述声间 tip 分支

---

## 相对 R2 handoff 的 M1–M5 强制补正（以仓内事实为准）

| # | handoff 主张 | 仓内结论 | 编码契约建议 |
|---|--------------|----------|--------------|
| **M1** | 验收用例勿占用 ops **D1–D5** 名 | **已证实**：`attention/shengjian-known-regressions.md` 中 D1–D5 已用于 mock/Continue/staging/听后感等**出门闸** | 本功能验收改用 **PL-D1…**；ops D1–D5 **并行同过** |
| **M2** | `t_start` 禁止未核前写死 ms | **已证实：秒（浮点）**。<br>• `fixtures/extract/*_golden.json`：`"t_start": 30.0`<br>• `PlayerModel.seek`：`CMTime(seconds: targetSeconds, …)`<br>• `studio_extract.validate_output_axis`：`float(t_start)`，注释「output.mp3 轴」 | 契约写「与 `PlayerModel.seek` / `CMTime` **秒轴**一致」；**无** ms 型锚点字段 |
| **M3** | `span` 形状 | **已证实：原文子串**（`String`），非 `{start,end}` 偏移对象。<br>• `ExtractionModels`：`var span: String?`<br>• `studio_extract.span_locatable`：子串精确或去空白后模糊匹配 `transcript_hash` 对应全文 | 新契约沿用子串 + 定位闸；若改偏移须单独映射层 |
| **M4** | 一句 = 观点**或**问题 | **UNKNOWN（产品层）**：现 UI 仍按 schema 槽展示 `claim.text` / `method.title` 等，**无** `viewpoint_text` 字段或单列表主面 | R3 新增导航列表时再显式允许问题句 |
| **M5** | 包 `degraded` vs 段级灰 | **已证实：无包级 `degraded` 枚举**。<br>• `PacketStatus`：`queued \| running \| ready \| failed \| stale`<br>• `degraded_from` 仅表 **schema 降级来源**（如 embodied→general）<br>• 段级：`seekButton` 在 `tStart == nil` 时 `.disabled` | **ready 包内**可有点无锚；用段级禁用/隐藏，**勿**发明包级 `degraded` Unless 扩展枚举 |

---

## §4 待查明十项（证据结论）

### 1) Player seek 与 extract/display/anchors 时间单位

**结论：已证实 — 统一为秒（`Double`），非毫秒。**

| 层 | 证据 |
|----|------|
| 播放器 | `ios/PrivateListen/PrivateListen/PlayerModel.swift`：`seek(to seconds: Double)` → `CMTime(seconds: targetSeconds, preferredTimescale: 600)` |
| 模型解析 | `ExtractDisplay.swift` / `ExtractionModels.swift`：`tStart`/`tEnd` ← JSON `t_start`/`t_end` 经 `double()` |
|  Mac 抽 | `scripts/studio_extract.py`：`validate_output_axis` 对 `t_start`/`t_end` 做 `float()`；mock 项如 `t_start=40.0` |
| 夹具 | `fixtures/extract/health_golden.json` 等：`30.0`、`55.0` |

**UNKNOWN**：Mac 侧源字幕 `start_ms` → `output.mp3` 轴的映射实现细节（设计文档称 Mac 映射）；仓内 **未** 在本次调查中逐行跟读映射代码。

---

### 2) `span` 是子串还是偏移；与 `transcript_hash` 关系

**结论：已证实 — 子串；`transcript_hash` = UTF-8 全文 SHA256 截断 16 hex。**

- **子串**：各 `Extract*` 模型 `span: String?`；JSON 样例为引号内短文（`fixtures/extract/ferment_golden.json`）。
- **定位**：`scripts/studio_extract.py`：

```python
def transcript_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]

def span_locatable(span: str, transcript: str, *, min_len: int = 8) -> bool:
    ...
    if cleaned in transcript:
        return True
    def norm(s: str) -> str:
        return re.sub(r'\s+', '', s)
    return norm(cleaned) in norm(transcript)
```

- **拒收/降信**：`reject_or_keep_item` 在 `axis_ok` 且 span 不可定位时降置信或丢弃（health 剂量等）。

**UNKNOWN**：App **运行时**是否复验 span（iOS 侧 grep **无** `span_locatable` 等价实现）；手机侧目前主要展示/跳时间，**不**在客户端重算 hash。

---

### 3) 详情主面组件树；旧 Claim/Method/Spine 是否仍可见

**结论：已证实 — 播放页主结构为运输控件 → 听后感 → 提炼区；Claim/Method 仍在提炼区内可见；无 Spine UI。**

```
PlayerView
  └─ … 进度/倍速 …
  └─ ListenNoteSection
  └─ ExtractDetailSection
        ├─ header（星标/菜单）
        ├─ statusLine
        └─ resultBlocks
              ├─ ForEach methods → methodCard（标签「方法」）
              ├─ ForEach claims → claimCard（标签「观点」）
              └─ entities / quotes / openQuestions …
```

- 证据：`ios/PrivateListen/PrivateListen/PlayerView.swift`（`ListenNoteSection` 在 `ExtractDetailSection` **之前**）；`ExtractDetailSection.swift` `resultBlocks`。
- **Spine**：`git grep Spine ios/` → **无匹配**（无独立 Spine 面）。
- 与 R1「主面唯一一句列表」的差距：**现网仍是多槽 Claim/Method 卡片 +「听这段」**，非目标 `jump_continue` 主列表。

---

### 4) ListenMark 写入时机；Continue 卡 episodeId 与进度来源

**结论：已证实（机制）；P-Continue 真机旅程未在本环境执行。**

| 机制 | 路径 |
|------|------|
| 存储 | `LibraryStore.ListenMark`：`seconds: Double`, `finished: Bool`；`UserDefaults` key `PrivateListen.progress` |
| 写入入口 | `LibraryView`：`player.onProgress` → `library.remember(id:seconds:duration:)` |
| 节流 | `PlayerModel.handle`：播放中每 **≥5s** 调 `checkpoint()`；`seek` 完成时亦 `checkpoint()`；`onDisappear` `checkpoint()` |
| clip 不写 | `PlayerModel.checkpoint`：`guard !clipSuppressListenMark`；`ContinueListening.acceptsProgressSample` 同闸 |
| finished | `LibraryStore.reducing` / `ContinueListening.record`：`seconds >= duration - 15` → `finished=true`（**非**点击完成） |
| Continue 排序 | `continueTouched[id]` 更新时间；`ContinueListening.items` 取 **最近触摸** 的未 finished 集，`displayLimit = 1` |
| Continue episodeId | `continueEntries` 把 `progress` 中 mark 映射到 `Episode.id`；卡面即该 **episode id**（非 clip 子 id） |

**UNKNOWN**：`jump_continue` 新路径是否复用同一 `remember`/`checkpoint` 链（**尚未实现**）。

---

### 5) 跳播 seek 是否会被既有「完成」监听误伤

**结论：部分已证实 — 风险点已定位；jump_continue 未实现。**

| 路径 | 行为 |
|------|------|
| 普通 `seek` | _seek 回调里 `checkpoint()` → 可能写入 `finished`（若落在末 15s） |
| 播放到文件末 | `AVPlayerItemDidPlayToEndTime` → `handleEnded()` → `checkpoint()`（非 clip 时） |
| clip | `clipStopAt` 到达 → `pause` + **不** `checkpoint`；`clipSuppressListenMark` 全程拦写入 |
| 点击提炼 | 仅 `playClip`，走 clip 闸 |

**设计隐含风险（已证实代码规则）**：若未来 `jump_continue` 用普通 `seek` 且不区分「用户点段」与「自然听完」，**seek 到距结尾 ≤15s 会标 finished**，Continue 逻辑会排除该集（`!mark.finished`）。**未证实**产品是否接受；需 R3 单测/闸。

---

### 6) `audio_sha256` / `output_version` 可读权威源；Norton 样例

**结论：UNKNOWN（声间成片绑定）— 仓内无 App 可读字段。**

- `Episode` / `episode.json` 发布记录（`studio_publish_icloud.py`）：`id, title, created_at, duration_sec, file, …` — **无** `audio_sha256` / `output_version`。
- 仓内 `audio_sha256` 仅见于 **播客 diarization** 流水线（`scripts/diarization_cache.py` `source_audio_sha256`），与私人听 `output.mp3` **非同契约**。
- `grep output_version` 在 `ios/` + `fixtures/extract/` + `studio_extract.py` → **无**。
- Norton `42786039aaf346a1bf851613199e260a`：**仓内无**该 id 的 episode/fixture/OSS 镜像；R1 文档层 ready 包 sha **无法在本 clone 复验**。

---

### 7) `playClip` / clip 是否仍在 CURRENT tip；suppress 路径

**结论：已证实 — 仍在 tip 主路径（切片 A）。**

- `PlayerModel.playClip`：设 `clipSuppressListenMark = true`、`clipStopAt`，`seek(..., clearClip: false)`。
- UI：`ExtractDetailSection.seekButton` → `player.playClip(from:to:)`，文案「听这段」。
- suppress 落点：`checkpoint` / `ContinueListening.record` / `handleEnded`（clip 结束不清 finished 误写逻辑见 `handleEnded` 的 `suppress` 分支）。
- `PlayerModel.isClipPlayback` 供 Continue 卡与 `shouldReopenAtListenMark` 判断听段 playhead 漂移。

**UNKNOWN**：`jump_continue` 是否新增 API 或复用 `seek(clearClip: true)`（未实现）。

---

### 8) 包 `status` 与段级不可跳如何共存

**结论：已证实 — 包级 `PacketStatus` 无 `degraded`；段级靠 `t_start` 缺省禁用跳。**

- 枚举：`ExtractionModels.PacketStatus` → `ready` 等；失败/过时整包 `failed`/`stale`（`stale` 可由 `transcript_hash` 变化触发，见 `studio_extract`）。
- 段级：`seekButton`：`.disabled(tStart == nil)`；注释「无 t_start 灰掉听按钮」与 `docs/shengjian-point-jump-expand-2026-10-30.md` 一致。
- 低置信条目仍展示（`low_confidence` 橙标），**不**自动变包级 stale。

**与 M5 对齐**：允许 **`status=ready` 包 + 部分条目无 `t_start`**；UI 级禁用即可。

---

### 9) staging / 增量合并是否会整包覆盖

**结论：分层面 — 已证实代码行为 + ops 风险（D3）**

| 层面 | 结论 |
|------|------|
| Mac 发布 | `rebuild_library` **扫描** `episodes/*` 重写 `library.json`（原子写），**不**删未扫描到的目录；单集 `publish_job` 复制音频 + `preserve_extract_on_republish` |
| 手机读库 | `LibraryStore.merge`：iCloud 为主，USB 副本 **按 id 补缺**，`iCloud wins`；**非**整库替换 |
| extract 合并 | `ExtractDisplay.merge` / `readPacket`：`extract.json` 与 `extract.live.json` 合并，防壳盖 live |
| **风险（D3）** | `attention/shengjian-known-regressions.md`：运维上 **整份 staging `library.json` 覆盖基线** 曾导致库骤减；`studio_push_iphone` 可拷贝 **整份** `library.json` 到机 — **流程闸**依赖推前 diff，**非**代码自动 ∪ |

**UNKNOWN**：生产 Mac 上 staging 目录命名与合并脚本的现行 SOP（仓内仅回归表叙述）。

---

### 10) 既有四连 / 短播自动化与设备要求

**结论：已证实 — 自动化边界；无手机 UI E2E。**

| 探针 | 仓内实现 |
|------|----------|
| 短播 Continue | `make test-continue-ux` → 编译运行 `ContinueUXProbe.swift`（逻辑单测，**非** XCTest 真机） |
| 提炼竞态 | `test-extract-display` → `ExtractDisplayChecks` |
| 装机闸 | `make shengjian-before-claim-ready` = 上二者 + `attention/shengjian-pre-claim-ready.sh`（源码字符串 + 可选 `dist/CURRENT-iphoneos` / DerivedData 二进制 `strings`） |
| 设备 | 文档要求 **macOS + swiftc/Xcode**；**明确无**手机 UI 自动化（`attention/装机宣称可听前必跑.md`） |
| ops D1–D5 | 定义于 `attention/shengjian-known-regressions.md`（mock、Continue 字符串、staging 条数、听后感文件进 Git 等） |
| P-Merge / 真机 P-Play | **未**在仓内发现一键四连脚本；push iPhone 需 `xcrun devicectl` + 物理机（`scripts/studio_push_iphone.py`） |

**UNKNOWN**：Cloud Agent 环境 **无 swiftc** 时闸跳过策略（Makefile 仅当 `SHENGJIAN_REQUIRE_SWIFTC=1` 才硬失败）。

---

## jump_continue 改造契约刀口（相对现网 tip）

| 项 | 现网 tip | R1 目标 | 调查结论 |
|----|----------|---------|----------|
| 主交互 | Claim/Method 卡 + 「听这段」`playClip` | 一句列表 + `jump_continue` | **未实现**；需新 UI + 非 clip 的 seek 续播 |
| ListenMark | 线性播放/seek（非 clip）写入 | 跳播亦写真实进度 | 可复用 `remember`/`checkpoint`，**须不用** `clipSuppressListenMark` |
| Continue | 最近触摸未 finished 集 | 跳播后卡=本集 | 机制已存在；须避免误 `finished`（§5） |
| 版本绑定 | 无 sha 字段 | `audio_sha256`/`output_version` | **UNKNOWN** — 需先扩 `episode.json` 或等价 |
| Norton 首集 | 无仓内包 | 验收样例 | 仅文档 id；**无**本仓 JSON |

---

## 关键摘录索引（便于 PR review）

```text
ios/PrivateListen/PrivateListen/PlayerModel.swift      — seek 秒轴、playClip、checkpoint、clipSuppressListenMark
ios/PrivateListen/PrivateListen/ExtractDetailSection.swift — 主面 Claim/Method、seekButton→playClip
ios/PrivateListen/PrivateListen/LibraryStore.swift    — ListenMark、remember、finished 规则
ios/PrivateListen/PrivateListen/ContinueListening.swift — Continue 单卡、短播、clip 闸
ios/PrivateListen/PrivateListen/ExtractionModels.swift — span String、PacketStatus
scripts/studio_extract.py                             — transcript_hash、span_locatable、validate_output_axis
scripts/studio_publish_icloud.py                      — publish、rebuild_library
attention/shengjian-known-regressions.md                — ops D1–D5
attention/装机宣称可听前必跑.md                          — 自动化范围声明
Makefile                                              — shengjian-before-claim-ready
```

---

## 诚实边界（本 PR）

- **零** 功能实现、**零** 生产配置变更、**零** 全库重抽、**零** 自动入队。
- **不** 声称真机可听、**不** 声称 Norton 现网包可读。
- 若需把 §6 Norton 落到字节级证据，需另授权拉取 `episodes/42786039…/` 或 OSS（超出本 clone）。

*调查完 · 2026-11-09*
