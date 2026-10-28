# 私人听 · 发现透镜 v0（主题优先 IA）

- **日期**：2026-10-28（Asia/Shanghai）
- **分支**：`feature/discover-lenses-article-ingest`
- **基线**：`codex/podcast-studio-20261018`（含 PR#16）；**不**叠在书听 PR#17 上
- **权威 IA**：主题透镜（theme lenses）为主；人/节目挂在透镜下，不当顶栏 peer

## 为什么是主题、不是人

商业化若按「Huberman / Attia / Latent Space」做人设导航，发现页会变成网红货架，品味门闩（confirm-v2 + GEV「转」才入队）会被热度带偏。按主题划区后：

1. 同一主题可换 show 而不改导航骨架（Huberman 出圈 → 观察池；Attia/FMF 顶上）。
2. 人/show 仍可见，但是 **chip 嵌在透镜内**，不是顶层 tabs。
3. 空透镜（AI 教育、小众品牌）为文章 URL 与后续信源留位，不必先找名人。

HO鑫对 Bayt：「界面一般，分类可借鉴」→ **借专区芯片/分区发现**；**不借** Bayt 视觉 chrome、预译漏斗、任意搜索墙。

## Bayt：借与弃

| 借 | 弃 |
|---|---|
| 清晰 zone chips / sectioned discovery | App Store 式浏览墙作主入口 |
| 文章 URL → 语音第二管道 | 每月 1 次免费任意译焦虑 |
| （日后）小时包作超量阀门 | Voice clone 当护城河；假周帽 |

详见 Desktop `attention/bayt-recon-2026-10-28.md` 附录「落地决定」。

## 默认透镜（中文标签）

| order | lens_id | 标签 | confirm-v2 日扫 show |
|---:|---|---|---|
| 1 | frontier-tech | 前沿科技 | Latent Space, Dwarkesh, How I AI |
| 2 | embodied-robotics | 具身与机器人 | No Priors, RoboPapers, Researcher's Guide to Physical AI |
| 3 | health-longevity | 健康长寿 | FoundMyFitness, The Drive / Peter Attia |
| 4 | ferment-food | 发芽发酵 / 食品 | Fermentation Cast |
| 5 | ai-education | AI 时代教育 | （空；文章/后续） |
| 6 | business-investing | 商业与投资 | Lenny's, YC Lightcone, MAD Podcast |
| 7 | niche-life-brands | 小众生活与品牌 | （空；小众发现官卡） |

数据：`data/lenses.json`（映射）+ `data/showlist-locked-v2.json`（源权威）。iOS 内嵌同份 `lenses.json`。

人/show 示例（透镜内 chip，非顶栏）：

- **健康长寿** → Attia · FoundMyFitness（Huberman 仅 observe）
- **前沿科技** → Latent Space · Dwarkesh · How I AI

## iOS 发现表面（v0）

- Tab：**发现** | **资料库**
- 发现：安静纸感 chip 条 → 点入透镜 → show 列表（无成片时占位文案，不假装有预译）
- 审美：quiet / paper-ish；反 Relive 忙乱；**不是** Bayt 克隆
- 「粘贴文章链接」写入 iCloud inbox，`content_type=article`；Share Extension 仍可投普通播客链

## 文章 URL 入队（Mac）

```
iOS 粘贴/分享 HTTPS
    → inbox/*.json { content_type: "article", expected_speakers: 1 }
    → studio_inbox 识别 article
    → studio_articles.normalize + extract_main_text
    → 写入 job 内 source.md + config（DeepSeek + Edge，单人）
    → execute=False 默认只做账本；真跑 TTS 显式打开
```

- 抽取：优先 fixture；真网可后续接 readability 类；失败写 inbox failed，不拖垮循环
- **Stub**：默认 `execute=False`；全量文章 TTS / 真抓页为 stub，测 normalize + enqueue 账本

## 文件

| 路径 | 作用 |
|---|---|
| `docs/private-listen-discover-lenses-v0.md` | 本设计 |
| `data/lenses.json` / `data/showlist-locked-v2.json` | 透镜 ↔ 12 源 |
| `scripts/studio_lenses.py` | 映射 API |
| `scripts/studio_articles.py` | URL 规范化 / 抽取 / 入队 |
| `ios/.../DiscoverView.swift` 等 | 发现 UI + 粘贴框 |
| `tests/test_studio_lenses.py` / `test_studio_articles.py` | 单测 |

## 非目标（v0）

- 不合并书听 PR#17；不自动「转」；不做兴趣图谱；不抄 Bayt 预译首页。
