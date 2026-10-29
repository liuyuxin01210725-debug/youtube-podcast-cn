# 声间 ·「收藏≠拥有」抽取产品设计 v1（完整草案）

- **日期**：2026-10-29（Asia/Shanghai）
- **作者**：Javis（联创拍板稿；草案由执行臂起草）
- **受众**：HO鑫 × Javis（决策用；可直接当产品规格读）
- **Javis 已拍**：Q1 建议路径——收藏/星标=深抽；Q2 MVP 只做单集详情；Q3 MVP 声间内状态机第一落点 + Markdown 导出（外部同步 v2）。**你已确认 Q1/Q3**（2026-10-29「好」）。
- **前序**：`shengjian-extract-landscape-2026-10-29.md` · `shengjian-ia-minimal-find-2026-10-28.md`
- **成功标准**：读完即知——声间怎么抽、App 哪露面、第一版做什么/不做什么

---

> **UX 裁定（2026-10-29，HO鑫纠正）**：收藏/星标＝深抽；packet ready 后**直接展示提炼结果**（方法/观点/实体/金句/开放问题），用于看精度、查漏、再改抽取。数据模型可保留 `status`（candidate 等），但 UI **禁止**默认「待确认」审批流与逐条「确认/丢掉」主交互。列表徽章 ready+deep 显示「已提炼」或「N 要点」，不写「N待确认」。逐条确认降为可选/不做默认。


## 0. 一句话问题 + 非目标

### 0.1 问题

听是为了拿知识与想法；**收藏只是入口**。声间必须把已入库播客/书里可二次利用的价值点抽成带时间戳的结构化对象，并给出默认动作——否则「收藏 = 假拥有」。

### 0.2 非目标（本设计明确不做）

| 非目标 | 理由 |
|---|---|
| 空 Discover / 预译商场 / 新垃圾场 Tab | 对齐 IA 硬禁令；透镜只筛已拥有 |
| 万能噪音摘要当主交付 | 对标 Podcast Notes：要可行动槽，不要 Listen411 式 dump |
| 发酵「一人工作台」立项 | 可标可产品化点；不单开产品线 |
| 听中实时手记完整编辑器 | 摩擦高；MVP 以听后/收藏触发为主 |
| 与每集「Chat」作 P0 | Snipd 奢侈层；搜索与卡片先于 Chat |
| 封闭假知识墓园（无导出、无状态） | 输给 Snipd→Readwise→Obsidian |
| 把 Bayt 听译叙事当差异 | Bayt 弱在抽取；声间打「抽出来能用」 |
| 实体自动入队（人/节目） | **默认候选卡，须确认**——对齐点转规则 |
| 医学/投资建议背书 | 健康/投资抽取仅作记录；须免责与可丢弃 |

### 0.3 一句话定位

> **收藏触发抽取；主题定 schema；卡片带动作；人与节目默认候选确认。**

---

## 1. 原则

| # | 原则 | 含义 |
|---|---|---|
| P1 | **收藏触发抽取** | 星标/收藏/「提炼」= 启动抽取任务，不是只改布尔值 |
| P2 | **可行动槽，非万能摘要** | 每条抽出物对应默认二次动作（试用/关注/待读/讨论…） |
| P3 | **主题差异化 schema** | 共性槽全主题有；差异槽按透镜切换（发酵≠投资） |
| P4 | **时间戳 + 原文 span 绑定** | 无 span / 无时间戳的实体默认低置信或拒收——防幻觉 |
| P5 | **默认候选，须确认才入队** | Person / Show（及建议关注的 Org）→ **候选卡**；用户确认后才进关注/队列 |
| P6 | **主表面极简** | 一池 + 智能芯片；抽取结果以**条目详情内嵌 + 轻量徽标**露面，不新开主 Tab |
| P7 | **先抽再讨论** | 管线先产出 ExtractionPacket；路由/二次开发在确认后讨论，不在听中打断 |
| P8 | **成本门禁** | 轻抽 / 深抽分档；无 transcript 不深抽；额度与进度可见 |
| P9 | **状态机防假拥有** | 每条：`候选 → 已确认 → 行动中/入库/丢弃`；周报回访可选 |
| P10 | **中文主体，外名保留** | UI/说明中文；产品名、书名、链接、节目名保留原文 |

---

## 2. 对象模型

```
Work (Episode | Book | Article)
  └─ ExtractionPacket          # 一次抽取作业的结果包
        ├─ schema_id           # 主题 schema
        ├─ mode                # light | deep | manual
        ├─ claims[]            # Claim
        ├─ methods[]           # Method
        ├─ entities[]          # Entity
        ├─ quotes[]            # Quote
        └─ open_questions[]    # OpenQuestion
```

### 2.1 Work

| 字段 | 说明 |
|---|---|
| `work_id` | 稳定 ID |
| `kind` | `episode` \| `book` \| `article` |
| `title_zh` / `title_src` | 中英/原文标题 |
| `show_id` / `authors[]` | 节目或作者 |
| `lens_ids[]` | 已有透镜标签（可多挂，Bear 式） |
| `progress` | 未听 / 在听 / 听完 |
| `starred` / `favorited_at` | 收藏态与时间 |
| `transcript_ref` / `translation_ref` | 已有听译产物指针（studio 产出） |
| `duration_sec` / `added_at` | 时长与入库时间 |

### 2.2 ExtractionPacket

| 字段 | 说明 |
|---|---|
| `packet_id` / `work_id` | |
| `schema_id` | 见 §3 |
| `mode` | `light` \| `deep` \| `manual` |
| `status` | `queued` \| `running` \| `ready` \| `failed` \| `stale` |
| `created_at` / `model_meta` | 时间与模型/版本（审计用，不暴露给用户当卖点） |
| `cost_units` | 计费/额度单位 |
| `summary_line` | **一行**给人扫的「本集抽了什么」（非长摘要正文） |

### 2.3 Claim（观点 / 判断）

| 字段 | 说明 |
|---|---|
| `text` | 可辩论主张，≤2 句 |
| `stance` | 可选：看多/看空/中性/争议 |
| `span` + `t_start`/`t_end` | 原文片段与时间戳（书用页码/章节） |
| `confidence` | 0–1 |
| `status` | 候选/确认/丢弃 |
| `default_action` | `debate` \| `kb_inbox` \| `monitor_candidate` |

### 2.4 Method（实操 / 工艺 / 教法）

| 字段 | 说明 |
|---|---|
| `title` | 短名 |
| `steps[]` | 有序步骤；可含参数（温度、时长、剂量、频率） |
| `params{}` | 结构化参数（主题相关） |
| `cautions[]` | 坑 / 禁忌 |
| `span` + 时间戳 | 强制 |
| `default_action` | `try_later` \| `kb_method` \| `ferment_note`（仅标记，不立项工作台） |

### 2.5 Entity

```
Entity
  type: Product | Software | Person | Org | Show | Book | ArticleRef
  name_src / name_zh?
  aliases[]
  url?                 # 若口述提到链接则保留；否则空，不瞎补
  span + t_start/t_end
  confidence
  status: candidate | confirmed | discarded | queued
  attrs{}              # 类型特有：公司行业、产品形态、软件平台等
```

**硬规则**：`Person` / `Show`（及「建议订阅」的 Show）默认 `status=candidate`；**禁止**自动写入关注列表或播放队列。

### 2.6 Quote（金句）

短、可引用；强制 span+时间戳；默认动作：`review` / `share_card`（分享可后置）。

### 2.7 OpenQuestion

听完仍不确定或值得讨论的问题；默认进「讨论队列」候选，不自动开任务。

### 2.8 关系与去重

- 同一 `work` 可有多份 Packet（light→deep 升级；schema 改后可重抽并标 `stale`）。
- Entity 跨集按 `name_src` 规范化合并为 **EntityIdentity**（候选合并也须确认）。
- Claim/Method 不去重合并为「真理」；只做「同源提示」。

---

## 3. 主题 schema 矩阵

透镜 = 已拥有内容上的过滤（IA）；**schema = 抽取字段模板**。一集可挂多透镜，抽取时取**主 schema**（用户确认或置信最高），次透镜只加「选抽」槽，不双倍噪音。

### 3.1 共性槽（所有 schema）

| 槽 | 必/选 | 说明 | 默认二次 |
|---|---|---|---|
| Claim | 必 | 可辩论观点/判断 | 讨论 / 知识库收件 |
| Method | 选→主题升级为必 | 可执行步骤 | 待实践 |
| Quote | 选 | 金句 | 复习 |
| Book / ArticleRef | 必（若提及） | 提到的书文 | 待读 / 听译候选 |
| Show | 必（若提及） | 其他节目 | **候选关注** |
| Person | 必（若提及） | 嘉宾/作者/提到的人 | **候选信源** |
| OpenQuestion | 选 | 待验证 | 讨论队列 |
| Software / Product | 选→主题升级 | 工具与实物 | 试用清单 |

### 3.2 差异矩阵

| Schema | 必抽（相对共性加码） | 选抽 | 禁噪音 |
|---|---|---|---|
| **通用 `general`** | Claim；提到的 Book/Show/Person | Method；Quote；Software | 长剧情复述；广告口播当观点；无 span 实体 |
| **发酵 `ferment`** | **Method（工艺+参数+坑）**；**Product（小众原料/成品/设备）**；Claim（菌种/风味判断） | Software（温控/记录 App）；Org（工坊/品牌） | 「美食感想」无步骤；通用养生鸡汤；把酱型盐曲标成液体盐曲等张冠李戴 |
| **商业投资 `biz_invest`** | **Claim（未来判断、行业判断）**；**Org（公司/标的）**；框架名 | Person（分析师/创始人）；Show；ArticleRef | 荐股口吻当事实；无论据金句堆；把广告赞助商当「必关注标的」 |
| **教育 `edu`** | **Claim（AI 时代教育判断）**；**Method（教法）** | Software（学习工具）；Book；Person | 招生广告；无方法的鸡汤 |
| **健康运动 `health`** | **Method（剂量/频率/动作）**；Claim（争议观点） | Product（器械/补剂）；Person（研究者） | 诊断/处方口吻；无参数的「多运动」；医疗承诺 |
| **具身 `embodied`** | **Method（练习/感知协议）**；Claim（身心框架） | Product（道具）；Person；Show | 玄学无步骤；与健康重复的纯补剂清单（归 health） |

### 3.3 Schema 选择规则

1. 入库时若已有 `lens_ids`，映射到 schema（发酵透镜 → `ferment`）。  
2. 无透镜：轻分类模型建议 1 个主 schema +「通用」兜底；**深抽前用户可改**。  
3. 多主题集：主 schema 全量差异槽；次主题只抽其「必抽」且条数封顶（见成本门禁）。  
4. 书与播客共用 schema；Book 的时间戳改为 `chapter` / `loc`。

### 3.4 每条抽出物的最小合格线

- 有 `type` + 可读 `text`/`name`  
- 有 `span`（≥8 字原文）与时间码或章节  
- `confidence` 写入；`<0.5` 默认折叠在「低置信」区  
- Person/Show：`status=candidate`

---

## 4. 触发、档位与成本门禁

### 4.1 三档触发

| 触发 | 档 | 产出 | 条件 |
|---|---|---|---|
| **听完**（progress→听完） | 轻抽 `light` | 至多：3 Claim + 5 Entity 名录（书/人/节目/产品粗提）+ 1 行 summary_line；**不做**完整 Method 拆步 | 有 transcript 或译稿；否则只标「可提炼」 |
| **收藏 / 星标** | 深抽 `deep` | 主 schema 全量必抽 + 选抽上限内；完整 Method；实体带 span | 必须已有 transcript/译稿；否则先排队补听译再抽 |
| **手动「提炼」** | `manual` | 同深抽；可改 schema 后重跑 | 用户显式；可对已 light 的包升级 |

### 4.2 成本门禁（建议默认）

| 门 | 规则 |
|---|---|
| G1 | **无 transcript/译稿 → 禁止 deep/manual**；UI 提示「先听译/转写」 |
| G2 | 单集 deep：Claim ≤8；Method ≤6；Entity 总量 ≤20；Quote ≤5；OpenQuestion ≤5 |
| G3 | light：总抽出物 ≤12；禁止生成长摘要正文 |
| G4 | 同一 `work_id` 24h 内 deep 默认 1 次；改 schema 重跑计 1 次 |
| G5 | 日/周额度（自用可先松）：超额则只入队，显示进度 |
| G6 | ASR 质量分过低 → 自动降为 light 或失败可重试，不硬出深包 |
| G7 | 广告/赞助章节（若有 chapter 标签）默认不抽实体 |

### 4.3 进度与失败可见

收藏后：条目上行内态 `提炼中…` / `可确认` / `失败·重试`。禁止黑盒静默（对齐景观坑：PodNote 进度不清）。

---

## 5. App 落点（对齐「一池 + 智能芯片」）

**总原则**：不新增与「我的库」平级的「知识 Tab / 发现商场」。抽取是**库内能力**。

### 5.1 MVP（最小可感）

| 落点 | 行为 |
|---|---|
| 库列表行 | 收藏后出现小徽标：`提炼中` → `N 条待确认`（有货才显示） |
| 条目详情 | 新增折叠区 **「提炼」**：按槽分组的卡片列表；每卡：原文跳转、确认/丢弃、默认动作按钮 |
| 星标/收藏 | 触发 deep（G1–G7） |
| 听完 | 静默 light；不弹窗打断；徽标可点进详情 |
| 芯片 | **不**为抽取新建透镜；沿用已有主题透镜过滤「已拥有」条目。可选后续：`待确认提炼` 状态芯片（有货才出现） |
| 禁止 | 新 Tab；空「知识广场」；未确认实体进队列 |

### 5.2 v1

| 落点 | 行为 |
|---|---|
| 提炼 Inbox | 从库顶栏「更多」进入的**二级页**（非主 Tab）：跨集待确认卡片时间序；批量确认/丢弃 |
| 候选卡专区 | Person / Show 单独一列「候选信源/节目」；确认后才写入关注或「稍后再听」 |
| 动作落地 | 待读 Book/Article → 可一键「加入听译/阅读候选」（仍候选）；Software/Product →「试用清单」；Claim →「讨论」标记 |
| 导出 | 单集 Markdown（含时间戳链接语义）；可选复制到剪贴板 |
| 搜索 | 标题/节目/嘉宾之外，索引已确认 Entity 名与 Claim 首句（owned only） |
| Schema | 深抽前可改主题；记住节目级默认 schema |

### 5.3 v2（规划，非承诺工期）

| 落点 | 行为 |
|---|---|
| 周报回访 | 本周已确认抽出物 10 条复习（Readwise 向）；防二次假拥有 |
| 导出同步 | Notion / Obsidian / 本地目录（一等公民，但非 MVP） |
| Entity 图谱轻览 | 「这个人还在哪些已听集出现」——仍是库内，不是社交 |
| Custom schema | 长尾主题用自定义槽（Snipd Custom prompts 向） |
| Chat with episode | 建立在 Packet + transcript 上；不替代卡片 |
| 状态芯片 | `待确认提炼` / `行动中` 在有货时出现在芯片条（硬上限：状态≤4 + 透镜少数） |

### 5.4 与 IA 选项 A 的对齐检查

- 主表面仍是一条已转列表 ✔️  
- 透镜只过滤已拥有 ✔️  
- 提炼 Inbox = 渐进披露的二级面，不是 Discover ✔️  
- 0 条待确认 → 不渲染「待确认」chip ✔️  

---

## 6. 二次利用动作卡

**规则**：每张卡暴露 **1 个主按钮 + 确认/丢弃**；主按钮默认把对象推入**候选**目标，不静默执行外部副作用。

| 对象类型 | 主按钮（默认） | 次要 | 落地态 |
|---|---|---|---|
| Claim | 标「讨论」 | 入库知识收件；丢弃 | `discuss` / `kb_inbox` |
| Method | 「待实践」 | 复制步骤；发酵相关标 `ferment_note` | 实践清单（轻列表，非工作台） |
| Product | 「试用清单」 | 搜索外链（用户点开）；丢弃 | `to_try` |
| Software | 「待试用」 | 保留官网名/链接若有 | `to_try` |
| Book / ArticleRef | 「待读/听」 | 请求听译（进 studio 候选，**须确认**） | `read_listen_candidate` |
| Show | 「候选关注」 | 查看已库内是否已有集 | **仅 candidate** → 确认后 subscribe/queue |
| Person | 「候选信源」 | 同名合并提示 | **仅 candidate** → 确认后 sources |
| Org（投资） | 「观察名单」 | 讨论 | `watchlist_candidate` |
| Quote | 「复习」 | 复制 | review queue |
| OpenQuestion | 「讨论队列」 | — | discuss |

全路径默认：**抽出 → 候选卡 → 用户确认 → 才入队/入库/关注**。  
「先抽再讨论」：Packet `ready` 后，Javis/HO鑫 可在对话里基于卡片谈二次开发；App 内不自动开项目。

---

## 7. 管线：听译之上跑抽取；与 studio 关系

### 7.1 依赖顺序

```
入库 Work
  → studio：转写 / 听译 / 译稿对齐（已有能力）
  → transcript_ref + translation_ref 就绪
  → Extractor（本设计）：按 schema 结构化输出 ExtractionPacket
  → 用户确认 / 动作路由
  → （可选）导出 / 周报
```

抽取 **不替代** 听译；**不**在无稿上瞎编。Bayt 对标停在通达；声间差异在 Extractor。

### 7.2 Extractor 职责边界

| 做 | 不做 |
|---|---|
| LLM structured output + schema 约束 | 自由长文摘要当主产物 |
| 强制 span 对齐到 transcript 句子 | 无依据补全书名/URL |
| 主题差异字段 | 发酵 ERP / 下单 / 实验排程 |
| 候选状态机 | 自动关注、自动加播放队列 |
| 失败重试与降级 light | 黑盒扣额度 |

### 7.3 与 studio 接口（逻辑）

- 输入：`work_id`，`transcript`（ ideally 句级时间戳），`schema_id`，`mode`  
- 输出：`ExtractionPacket` JSON（字段见 §2）  
- 幂等：同 `work_id+schema+mode+transcript_hash` 可复用  
- studio 进度事件可附带 `extract_eligible=true`  

### 7.4 书 / 文章

- Book：以已导入文本/听书稿为源；loc/chapter 代替时间戳  
- Article：入库文章（若有）同 schema；无正文则只允许手动摘录，不自动 deep  

---

## 8. 失败模式与对策（≥8）

| # | 失败模式 | 对策 |
|---|---|---|
| F1 | **假收藏**：只星标无结构 | 星标=深抽任务；无稿则明确阻断并引导听译 |
| F2 | **噪音摘要** | 禁长摘要主交付；槽位上限；summary_line 仅一行 |
| F3 | **实体幻觉**（编书名/产品） | 强制 span；置信阈值；低置信折叠；一键丢弃；禁止无 span 自动确认 |
| F4 | **一刀切 schema** | 主题矩阵；深抽前可改；节目默认 schema |
| F5 | **人/节目自动入队污染库** | Person/Show 默认 candidate；确认门闩与点转规则一致 |
| F6 | **新垃圾场 Tab / 空发现** | 落点锁在详情+二级 Inbox；零结果不渲染 chip |
| F7 | **抽取后再次假拥有** | 状态机 +（v2）周报回访；动作卡默认出口 |
| F8 | **ASR 烂导致胡抽** | 质量门禁降级 light/失败；用户可见原因 |
| F9 | **成本爆炸** | G1–G7；light/deep 分档；24h 重跑限制 |
| F10 | **广告当干货** | 跳过赞助章；实体白名单可后置 |
| F11 | **多主题双倍噪音** | 主 schema 全量 + 次主题必抽封顶 |
| F12 | **封闭墓园** | v1 Markdown 导出；同步后置但不堵数据模型 |
| F13 | **健康/投资被当成建议** | UI 免责短句；Method/Claim 可丢弃；不提供「跟买/跟练」自动化 |
| F14 | **处理黑盒** | 行内态与失败重试；队列位置可见 |

---

## 9. 借弃表

| 产品 | 借 | 弃 |
|---|---|---|
| **Snipd** | 收藏/snip 立刻有结构；书+嘉宾实体；导出一等；Daily recap 思路 | 额度焦虑当产品叙事；Chat 抢 P0；无主题 schema |
| **Podcast Notes** | ideas/tools/tactics 可行动槽；人工级「什么值得留」标准 | 只读别人的笔记；覆盖不到私有中文/小众库 |
| **Podwise** | 单集多视图与强导出；Skills/工作流映射「周行动」 | 一刀切字段；credits 黑盒感；Keywords 无行动态 |
| **Readwise** | 高亮≠拥有 → 需要复现；导出到第二脑 | 不做听中捕获；无播客主题 schema |
| **Bayt** | 听译通达、说话人、字幕搜索（能力层可并存） | 收藏无抽取；Discover/专区商场；用听译当知识拥有 |

景观结论可压缩为：**抄 Snipd 的触发与实体最小环，抄 Podcast Notes 的槽哲学，抄 Readwise 的复现意识，用主题 schema 打公开竞品空白，用候选确认打过自动关注。**

---

## 10. MVP 拍板建议 + 需 HO鑫 决定的问题

### 10.1 MVP 建议（建议拍板「做」）

1. **对象模型**落地：Packet + Claim/Method/Entity/Quote/OpenQuestion（可先 JSON 存）。  
2. **触发**：听完 light；收藏 deep；手动提炼。  
3. **Schema**：先上 `general` + `ferment` + `biz_invest` + `health` + `edu`（`embodied` 可先映射 general+Method 加码）。  
4. **App**：列表徽标 + 详情「提炼」区 + 确认/丢弃 + Person/Show 候选门闩。  
5. **管线**：只吃已有 transcript/译稿；无稿不深抽。  
6. **动作**：试用清单 / 待读候选 / 讨论标记 / 候选关注——全候选。  
7. **不做**：新 Tab、Chat、Notion 同步、发酵工作台、自动入队。

### 10.2 MVP 明确不做（再强调）

- 空发现、知识商场、无限摘要 feed  
- 发酵一人工作台立项  
- 无 span 实体自动确认  
- 为抽取单独做「主题首页」替代一池  

### 10.3 需 HO鑫 决定的 2–3 问

**Q1. 深抽触发键：星标 = 收藏 = 深抽，还是「星标仅标记、另有提炼按钮」？**  
- 建议：**收藏/星标即深抽**（对齐「收藏≠拥有」叙事）；另保留手动提炼改 schema。  
- 备选：星标轻量，深抽必须点「提炼」（更省成本，但易回到假收藏）。

**Q2. 提炼 Inbox：v1 就要跨集二级页，还是 MVP 只做单集详情内嵌？**  
- 建议：MVP 单集详情；待确认总数≥20 或一周后再上 Inbox 二级页。  

**Q3. 知识库落点：声间内「已确认卡片库」是否算第一知识库，还是确认后只导出到外部 Notion/Obsidian？**  
- 建议：MVP 声间内状态机即第一落点 + Markdown 导出；外部同步 v2。避免尚未抽稳就搭双库。  

（若只许问两个：优先 **Q1 + Q3**。）

---

## 11. 可产品化备忘（发酵工具意识 · 不立项）

> 本章不是 roadmap 立项，只是抽取系统顺带**标出**可产品化信号，供日后讨论。

| 信号（从 Method/Product 卡来） | 可能产品形态 | 边界 |
|---|---|---|
| 重复出现的工艺参数卡（温度/时长/菌种） | 轻量「工艺卡片模板」或家用记录 | ≠ 完整发酵 MES/工作台 |
| 小众成品/原料频繁「试用清单」 | 试吃卡/采购备忘联动（已有周行动传统） | 不自动下单；不锁品牌真相 |
| 温控/pH 等 Software 提及 | 工具试用列表 | 不自研硬件 App |
| 跨集「坑」Claim | 可汇编「避坑短册」 | 仍是内容，不是设备 |

**纪律**：Extractor 可打 `productizable=true` 标签；**禁止**因此开「发酵一人工作台」项目。与 `ferment-week-action` 类执行包的关系是：**人用卡片决策**，不是 App 内排产。

---

## 附录 A · 用户故事（验收口述）

1. 我听完一集发酵播客，收藏它 → 详情出现工艺步骤与小众产品卡，每条能跳回原句。  
2. 提到的人和节目在「候选」，我确认后才关注。  
3. 投资集抽出「未来判断」和公司名，进观察候选，而不是自动变成交易建议。  
4. 库首页仍是一条干净列表；只有徽标和（有货时）芯片变化。  
5. 没有译稿时点收藏，系统说先听译，而不是编一包假知识。

## 附录 B · 数据量与芯片（与 IA 共用）

| 规模 | 抽取相关 UI |
|---|---|
| <40 集 | 详情提炼区足够；搜索可藏 |
| 待确认 >0 | 可出现 `待确认` 状态芯片 |
| >300 集 | 已确认实体名进入搜索索引 |

## 附录 C · 阅读副本

本页是 2026-10-29 设计稿的装机副本，在声间里滚动读完即可。不打开 Notion，也不另开浏览器。

景观与信息架构两篇前序不在本页展开。

---

*草案止于产品设计；无代码、无真机改动。Javis 拍板后交 HO鑫。 · 2026-10-29 Asia/Shanghai*

---

## 12. Javis 拍板记录（2026-10-29）

| 项 | 裁定 |
|---|---|
| 定位 | 收藏触发抽取；主题定 schema；卡片带动作；人/节目默认候选确认 |
| MVP | 徽标 + 详情提炼区；听完 light / 收藏 deep；无稿不深抽；五 schema 先上 |
| 禁 | 新知识 Tab、空发现、Chat P0、自动入队、发酵工作台立项 |
| Q1（已确认） | **收藏=深抽**；另留手动提炼改 schema |
| Q2 | **已定**：MVP 单集详情；待确认多了再上 Inbox |
| Q3（已确认） | **声间内第一知识库 + MD 导出**；Notion/Obsidian 同步 v2 |

Codex 对抗审若后到，只增补失败模式，不改以上 MVP 边界。
