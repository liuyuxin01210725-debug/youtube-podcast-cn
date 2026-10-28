# 私人听多音色审计 · Mark Cuban / DOAC（2026-10-28 Asia/Shanghai）

状态：`expected_speakers` 硬闸已接上入队、准备译稿和合成。`voices_confirmed` 不能绕过人数或共槽。Cuban / Creatine 已出片的成片不返工。验收是音色数等于真实人数且逐人可辨。

## 对象
- Job：`/Users/liuyuxin/Desktop/音频/中文播客工作台/jobs/07c1dddbdc684968b69199ad4f32e86b`
- 源：`6VTSAs4Rsck` · Mark Cuban · The Diary Of A CEO
- 本集：**不返工**（用户确认）

## 根因（有证据）
1. **说话人过度切分，人数未卡死**  
   - `document.json` → `speakers` 共 **6** 个 ID：`SPEAKER_00/01/02/03/05/06`  
   - `references/` 对应 **6** 个 wav；`segments` 978 段按上述 ID 分布（00=467, 02=247, 01=139, 03=58, 05=38, 06=29）  
   - 原文为 **双人**对谈，声纹把同一人切成多 ID（含女声槽）。
2. **config 无 `speaker_count`**  
   - `config.json` 仅有 `diarization_threshold: 0.9`，**没有**人数硬上限 / 期望人数字段。  
   - 合成前把每个 SPEAKER 派到 `voice_slot`：`male_1/2/3` + `female_1/2`（**5 个不同槽**；`SPEAKER_03` 与 `00` 同落 `male_1`，部分塌缩但仍远超 2）。
3. **同病对照**  
   - Creatine DOAC `jobs/6d898d4192de495fafcec86b9c938430`：`speakers` **5** 个（00/01/02/05/10）→ male_1/2/3+female_1/2；config 同样无 `speaker_count`。  
   - 记忆中的「说话人数超出范围修复 yt9」管的是块内 ID 爆表，**不等于**「成片音色数==声明 N」；Cuban 在人数硬闸未生效下发片。

机制一句话：diarization 多切 → 无人卡 N → 每 SPEAKER 绑不同 Edge 槽 → 听感「两人却很多音色」。

## 本集为何不修也能听懂
旁白与问答内容完整、字幕源（`transcript_source: subtitles`）+ DeepSeek 中文已齐；乱的是**声线身份**，不是语义空洞——继续听内容可，不必重跑 99MB 成片。

## 下一集必须上的硬闸（合成前）
1. 入队/配置声明 `expected_speakers=N`（双人访谈默认 2；可人工改）。  
2. **合成前断言**：`len(unique speaker_id in segments) == N`；否则 fail / 强制 merge 后再合成，禁止带病出片。  
3. **音色表断言**：`len(unique voice_slot) == N`，且 speaker→voice **一对一稳定绑定**（禁止段级随机 voice、禁止同一人多槽）。  
4. 性别槽：双人男对谈禁止出现 `female_*`（除非 metadata/人工标有女声）；错位即拦。

## 其它可直接优化（≤8，按优先级）
1. **硬闸落地**：`expected_speakers` 写入 job `config.json`，TTS 入口统一检查。  
2. **过度切分折叠**：短时长/低占比 SPEAKER 并入最近邻主说话人（Cuban 的 05/06 女声槽 + 03 即典型碎渣）。  
3. **Creatine 同修策略复用**：性别不错位 + filler 清理后 **不得丢 speaker_id**。  
4. **语气词**：翻译后轻度剥「嗯/啊/那个」再送 Edge（低成本）。  
5. **阈值确认 UI**：`voices_confirmed` 在人数≠声明 N 时不可自动 true。  
6. **进度/章节**：长书/长集用章节锚点（对标微信读书类听读），避免只能从头听——产品侧，非本集阻塞。  
7. **私人听多渠道**（书/中英）：复用同一「N 人音色硬闸」管线，勿另开一套随机配音。  
8. **阈值回归样例**：固定「双人 DOAC」金样，合成前 diff `n_speakers`/`n_voice_slots`。

## 模型表（是不是 DeepSeek？）
| 环节 | 实际 | 证据 |
|------|------|------|
| **翻译/润色** | **DeepSeek**（是） | `config.json` → `"provider": "deepseek"` |
| TTS | **Edge TTS** | `config.json` → `"tts": "edge"`；槽位 male_*/female_* |
| ASR/转写源 | 本集用 **YouTube 字幕** | `document.json` → `"transcript_source": "subtitles"`（非本机重 ASR） |
| 声纹/分离 | 本地 diarization（sherpa/pyannote 管线） | 产出 `speakers` + `references/*.wav`；阈值 `0.9` |
| 标题中译 | DeepSeek 路径（同 provider） | `metadata.title_status: translated` |

**直接回答 HO鑫：转换（英→中）默认是 DeepSeek；朗读默认是 Edge TTS。不是全程 DeepSeek，也不是 Kimi/GPT 做主译（以 job config 为准）。**

配置路径：
- Job：`…/中文播客工作台/jobs/<id>/config.json`
- 声纹与音色产物：同目录 `document.json`（`speakers` / `segments` / `references`）
