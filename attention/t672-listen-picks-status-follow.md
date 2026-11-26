# t672 精选入口、内容可消费保守状态与分段随播高亮边界

- **实现基线**：基于 `bd3c9e72db69cdf4ec3ab460b1333b646a150a8b`（分支 `feat/ios-core-ux-20261124`）。
- **精选入口（`ListenPicks.swift`）**：
  - 仅采纳通过 `PointListening.refusal` 的成片秒轴（`output.mp3.seconds` + 音频 SHA-256 绑定）且 `tEnd - tStart >= 1.0` 秒、具备严格唯一槽位（`claims`/`methods`/`quotes`/`openQuestions`）或真实译稿 `span` 依据的 2–3 条观点；不足 2 条返回空，绝不回退 `prefix(3)`，绝不从译稿轴或 `MockExtractor` / `.light` / `.stale` / `pendingTranscript` 生成精选。
  - 展示文案明确使用「起点 mm:ss · 依据片段约 N 秒」与基于真实字段的透明理由，并从 `tStart` 连续向下播放（不在 `tEnd` 停止）。
- **内容可消费保守状态（`EpisodeContentStatus.swift`）**：
  - 按真实已有字段保守推断（`中文成片` / `待核听样` / `待核听样（校验失败）` / `来源待核` / `下载中` / `音轨不可用` / `版本待核`），列表行仅在既有 `LibraryShelf.rowMarks` 未满 3 枚时末位追加例外短标，不挤占「要点 N」等既有徽章。
  - **本轮不读取 `human_review` 键，人审维度恒显「未人审」**，绝不把有音频或 `complete` 推断为质量 PASS。
- **分段随播高亮与「回到当前段」（`SegmentPlaybackTracker.swift`）**：
  - 仅对已校验 `reading.nodes` 生效，且门禁 `player.isCurrent(episode)` 与 `audio.isCurrent`；中文译稿（`reading.lines`）与待核听样不做随播高亮。
  - 用户主动滚动立即退出自动跟随（绝不抢滚），点击「回到当前段」仅滚动视口回当前已校验分段，不改播放头、不影响听后感与听中笔记。

## 必须保留的未核边界
- **本轮仅过隔离模拟器 `Shengjian iOS Core UX 20261124`（`BD8CFCAE-F1D1-462F-9B78-20D26072112B`）自动化回归**。
- **`bd3c9e7` 真人冒烟与真机四探针（P-Play / P-Continue / P-ListenNote / P-Merge）仍未核**，不替代真机人耳验收，不得宣称真机「可听」或「质量 PASS」。

> 预留 Standing 一句指针：
> `成片精选入口、内容可消费保守状态（人审恒显未人审，本轮不读 human_review）及已校验分段随播高亮/不抢滚边界见 [t672 精选入口与随播高亮](t672-listen-picks-status-follow.md)。`
