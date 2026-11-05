# 声间 · 要点跳听 + 要点收藏拓展 设计附录（2026-10-30）

- **版本**：addendum · point-jump-expand（2026-10-30）+ 听这段最小窗口修订
- **日期**：2026-10-30（Asia/Shanghai）
- **主规格**：[`shengjian-extract-design-v1-2026-10-29.md`](./shengjian-extract-design-v1-2026-10-29.md)（§5.1 原文跳转 · §13.1 output.mp3 轴 · §13.2 跳回不写 ListenMark · §14 UX 直出）

---

## 切片 A · 听这段（含最小窗口）

**主手势**：要点卡 **「听这段」**。

**行为契约**：

1. Seek 到扩窗后的 `clipStart`（output.mp3 轴），`autoplay=true`。
2. 播一小段后暂停；**不**改写 `ListenMark` / 续听书签（§13.2）。
3. 无 `t_start` 的卡：听按钮灰掉。

### 听这段最小窗口（播放器侧，不重抽 JSON）

真抽 `t_start`/`t_end` span 常偏紧（Cuban 中位 ~8.5s、最短 ~1.9s；Katz c1 ~4.2s）。若播到裸 `t_end`，用户会感觉「十几字就停」。因此 **App 播放器**在 `resolveClipWindow` / `playClip` 扩窗：

| 参数 | 默认 | 规则 |
|---|---:|---|
| `leadIn` | 2.5s | `clipStart = max(0, t_start - leadIn)` |
| `minDuration` | 28s | 有合法 `t_end` 时 `clipEnd = max(t_end, t_start + minDuration)` |
| `fallbackSeconds` | 28s | 缺 `t_end` 或 `t_end ≤ t_start` → `t_start + fallback` |
| `softCap` | 60s | 相对 `clipStart`；偶发超长 span 不强制听到底 |

落地：`PlayerModel.resolveClipWindow` / `playClip`。**严禁**为扩窗改写 ListenMark。

---

## 切片 B · 要点级收藏

- 每条要点卡独立小星（橙/灰），与整集大星分层。
- 持久化 `PointStar { work_id, point_id, kind, reason?, starred_at }`（`PointStarStore` · UserDefaults 键 `PrivateListen.pointStars`）。
- 点星即收藏；`reason` 四态可后置。
- 取消要点星 **不**撤销整集深抽；不自动入队人/节目；拓展 UI（切片 C）本切片不做。

---

## 切片 C

本附录提及、**本次不做**。

---

*听这段最小窗口修订 · 2026-10-30 Asia/Shanghai*
