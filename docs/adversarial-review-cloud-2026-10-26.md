# 对抗性审阅 · 中文播客工作台（2026-10-26）

**产品验收（2026-10-28 起）**：音色数必须等于真实说话人数，且逐人可辨。每人固定一个不同声槽。`voices_confirmed` 不能绕过。下文「大约 4–5 个声能分开」只描述这次审查当时的旧门槛，不再作为验收标准。已出片的 Cuban / Creatine 不返工。

基线：`codex/podcast-studio-20261018` @ `0f8e35d`（`fix(studio): lock diarization ghosts + stage ETA progress`）。私人英→中流水线。审查当时的验收是一期里大约 4–5 个声能分开，默认 Edge/系统声槽，克隆可选。

## 实施状态

P0 已实现，上限仍是 6。`_run_worker_chunk` 在 `_validate_chunk_turns` 之前调用 `_fit_turns_to_speaker_budget`：返回给拼接的 turns（含第一块）按 `exact` 或 `maximum` 折叠，embedding 键跟着幸存者改写。锁死「7 人必须先拒绝」的测试已换成折叠后通过。

同一 PR 上又收了一批会明显伤体验的平台层问题：试听按说话人覆盖到最多 5 个声槽；已知的安全中文错误能进 `progress.json`；工作室里单个远处字幕变成待核对，不再在声纹跑完后作废整期；CLI 声纹缓存的 `stitching_version` 升到 3。缺 ffmpeg 不再被说成「音频无效」；解码失败的缓存仍当作坏片段重做。

下文「缺陷」是裁定时的证据。「当前仍会先拒绝」说的是 P0 修复前的顺序。做了什么、明确没做什么，以文末「架构 / 流程优化」为准。

未重跑 `https://www.youtube.com/watch?v=Jk7RAkFN4vk`。下面的判断来自这条报错在当前代码里的唯一落点，以及它前后还有哪些同类的门。

## 扫读结论

- 工作台没有另写一套声纹。CLI 与工作室都进 `run_sherpa_backend`。DOAC 这次炸在共享运行时，不是 UI 独有的校验。
- `b68e53c` **不在这棵树上**。没有 `_fit_turns_to_speaker_budget`。单块人数校验仍在折叠之前。单元测试还把这个顺序写成规格，并且当前是绿的。
- 报错原文 `声纹分块检测到的说话人数超出配置范围` 只来自 `_validate_chunk_turns`。折叠函数 `_collapse_minor_speakers` 在全部分块拼接成功之后才跑，所以 `c20e482` 的鬼影折叠救不了这次失败。
- 假设中的「校验前先折进预算，并把上限 6 改成 8」方向对了一半。先折是对的；把可辨人数抬到 8 不够，而且会撞上还没改的第二道门。可辨声槽仍然是 5 个。第 6 人已经和其中一槽撞声。
- 立刻要改的只有这一件事：在配置上限之内把多余 ID 折掉，再做硬拒绝。折叠必须作用在真正拿去拼接的 turns 上（含第一块），并且同步改写 embedding。不要先把常量从 6 改成 8。
- 10 月 25 日草稿里的字幕回退落盘、preview 单一来源、阶段 ETA、软闭环、短鬼影折叠，都已经在这棵树上。它们没有引入第二套说话人校验。

## 对 b68e53c 的独立判断

描述是：校验前调用 `_fit_turns_to_speaker_budget`，可辨音色槽上限 6→8。

**当前分支没有这个修复。** `git cat-file -t b68e53c` 失败；全库搜不到该符号。`tests/test_diarization_runtime.py` 的 `test_rejects_more_local_speakers_than_configured_before_stitching` 用 7 个各 5ms 的 ID 调用 `_validate_chunk_turns(SpeakerBounds())`，期望抛「人数」。这 7 段加起来远小于折叠阈值 20s，现有折叠函数会把它们收掉。测试要求的是先拒绝。本分支上该测试通过。

即便按描述把补丁补回来，只做这两处也不够：

| 若只做了 | 长访谈上实际会怎样 |
| --- | --- |
| 只在 `_validate_chunk_turns` 前面把本块折到 ≤8，matcher 仍写死 6 | 第一块能过。第二块拼接调用 `_maximum_weight_mapping`，任一侧超过 6 就抛 `声纹匹配人数超出安全上限`。DOAC 这种超过 15 分钟的节目有多块（默认 15 分钟一块、重叠 5 分钟），会换成下一条硬错误。 |
| 只把 `SpeakerBounds.maximum` 改成 8，折叠仍是「短于 20s 才合并」 | 每块里活过 20s 的第 7、第 8 个 ID 会留下来。最终 `validate_diarization` 若仍是 6 会再拒一次；若放宽到 8，多出来的人没有第 6–8 套 Edge 声，`VOICE_ORDER` 只有 5 个，取模后和前面的人同声。 |
| 拟合时看的是副本，返回的仍是原始 turns | 校验通过，拼接收到的仍是 7+ 个 ID。第一块不经过 `_speaker_mapping`，原样成为全局说话人集合。 |
| 运行时放行 7–8 人，CLI 的 `default_speaker_record` 仍在第 7 人抛「自动 speaker 映射最多支持 6 人」 | `diarize=auto` 会把这个异常收成「已回退到内容判断」，说话人整段丢掉，任务看起来成功。工作室没有这条静默回退。 |

Sherpa 侧也没有「最多 N 人」这个开关被用上。`_run_worker_chunk` 在 `exact is None`（工作室默认，UI 不写 `speaker_count`）时传入 `--min-speakers 1 --max-speakers 6`。`sherpa_diarization_worker.py` 只把 `--num-speakers` 写成 `num_clusters`；`min_speakers` / `max_speakers` 只出现在 argparse 里，运行时不读。未知人数时 `num_clusters=-1`，只靠距离阈值聚类。官方语义是：阈值越大，簇越少。YouTube 默认 0.9 已经是「更少说话人」那一侧。DOAC 在这条路径上仍然超过 6，说明下一刀不该再抬阈值。

因此：先按**当前配置上限**做人数预算折叠，再校验。预算用 `exact` 或 `maximum`（今天是 6），不要改成 8。可辨声以现有 5 个系统声槽为准；第 6 个 ID 若被保留，工作室会把它循环到 `female_1`，和第一位撞声。这是既有行为，不是这次该顺便扩大的能力。

## 这次报错的门，按执行顺序

工作室 `prepare` 使用 `SpeakerBounds(exact=config.get("speaker_count"))`。创建任务的 UI 不写 `speaker_count`，于是 `exact is None`，`maximum` 为 6。YouTube 任务会把 `diarization_threshold=0.9` 写进 `config.json`，worker 的 `--cluster-threshold` 会覆盖配置对象里的 0.5。

1. **本块硬拒绝（DOAC 停在这里）。** `scripts/diarization_runtime.py` 的 `_run_worker_chunk` 读完 JSON 后立刻 `_validate_chunk_turns`。唯一 ID 数大于 `exact` 或 `maximum` 就抛 `声纹分块检测到的说话人数超出配置范围`。`exact=2` 时更紧：一块里 3 个原始 ID 也会死，哪怕其中一段是 2 秒鬼影。`docs/STUDIO-ENGINE.md` 的示例配置带了 `"speaker_count": 2`。
2. **拼接匹配硬拒绝（第一块若带着超过 6 个 ID 活下来，第二块会死在这里）。** `_maximum_weight_mapping` 在任一侧超过 6 时抛 `声纹匹配人数超出安全上限`。注释写的是安全上限，DP 本身到 8（2^8）仍然很小；6 是策略常数，不是算力墙。第一块（`index == 0`）不经过映射，原始 turns 直接成为已拼接结果。
3. **时长折叠（现在的鬼影治理，跑不到第 1 步失败的任务）。** `run_sherpa_backend` 返回前调用 `_collapse_minor_speakers`，只合并合计短于约 20s 的 ID。每一段都 ≥20s 的第 7 个簇会留下来。
4. **成片质量门。** `validate_diarization` 再查一次人数，文案是 `检测到的说话人数超出配置范围`（没有「分块」）。工作室在这之后还有一条更严的规则：任何一个 cue 的 `speaker_id is None` 就放弃整次准备（见 P1）。

软闭环（重叠 / 原型对不上时，优先映到已有全局 ID，没有原型的残余贴到主导说话人）只存在于 `_speaker_mapping`。它不处理「第一块自己已经超过 6」。

CLI 的 `SherpaDiarizationBackend.diarize` 调用同一个 `run_sherpa_backend`，且不追加 `--cluster-threshold`，worker 默认 0.5，比工作室 YouTube 的 0.9 更容易拆出多余 ID。`diarize=auto`（CLI 默认）在 `prepare_episode` 里抓住异常后删掉说话人序列，打印「已回退到内容判断」。同一道门：工作室大声失败，CLI 默认把说话人整段拿掉然后继续。不要把 CLI 的 auto 回退抄进工作室。

## 缺陷

### P0 · 单块人数在折叠前就失败，测试把这个顺序锁死了

- **现象。** 一块 sherpa 输出超过配置人数（默认 6）时，准备阶段失败，文案为 `声纹分块检测到的说话人数超出配置范围`。UI 没有说话人数旋钮。`--max-speakers` 看起来传了，worker 不用。重试同一链接会再炸。短于 20s 的鬼影本来可以被折掉，但折叠在这次抛错之后。
- **证据。** `scripts/diarization_runtime.py`：`_validate_chunk_turns`；`_run_worker_chunk` 在读完 worker 输出后调用它，并在 `exact is None` 时附加 `--min-speakers` / `--max-speakers`（其中 `bounds.exact if bounds.exact is not None else bounds.maximum` 写在已经判定 `exact is None` 的分支里，是死代码）。`scripts/sherpa_diarization_worker.py`：`FastClusteringConfig(num_clusters=...)` 只用 `num_speakers`；`--min-speakers` / `--max-speakers` 无读取点。折叠只在 `run_sherpa_backend` 的 `return _collapse_minor_speakers(...)`。`scripts/studio_engine.py` 的 `prepare` 用 `SpeakerBounds(exact=config.get("speaker_count"))`。`scripts/studio_server.py` 创建任务时不写 `speaker_count`。测试：`RuntimeTests.test_rejects_more_local_speakers_than_configured_before_stitching`（本分支执行通过）。回归夹具 `tests/fixtures/diarization-regression.json` 直接调用映射和折叠，不经过 `_run_worker_chunk`。
- **为何重要。** 这就是 DOAC Creatine 的报错字符串，而且是共享核心。长访谈第一块 15 分钟里，片头、广告、笑声、另一支麦克风都可能多出簇。阈值 0.9 已经是「更少簇」。再抬阈值会把真的客人并掉，代码注释已经反对这么做。
- **建议改法。** 在 `_run_worker_chunk` 返回前，对**将要返回的** turns 做人数预算折叠，再 `_validate_chunk_turns`。预算是 `bounds.exact` 或 `bounds.maximum`，不要改成 8。人数仍超标时，反复把「当前最短的 ID」并进其余 ID 里余弦最近的一个（没有原型就并进累计时长最长的）。同步改写这块的 embedding 键。第一块因此带着折过的 ID 进入拼接，`_maximum_weight_mapping` 的 6 人上限才不会在第二块被踩中。全片结束处的 20s 折叠可以留着，当第二道清扫。最终 `validate_diarization` 继续当背书，不要删。用「7 个短 ID 折成 ≤6 且不抛错」替换现在这条先拒绝测试；再加一条「7 个都超过 20s 的 ID 仍折到上限」。`--min-speakers` / `--max-speakers` 要么删掉，要么不要再假装 sherpa 会遵守。已知代价：若第 7 个簇又长又远，它会并进最近的长簇，短嘉宾可能贴到主持人身上。这符合审查当时的「4–5 个声能分开、整期要能做完」，不符合「永远不并错人」。现行验收改为音色数等于真实人数且逐人可辨。

### P1 · 试听最多 3 个说话人，盖不住验收的 4–5 人

- **现象。** 按钮文案是「试听不同说话人」。超过 3 个说话人时，试听只含文稿顺序里先出现的 3 个人，每人一句最长中文。第 4、第 5 人要等整期合成才听得到。第 6 人还会和第一槽同声。
- **证据。** `scripts/studio_preview.py` 在 `len(ordered) >= limit` 时立刻返回。`scripts/studio_server.py` 固定 `--preview 3`。`docs/STUDIO-ENGINE.md` 仍写「`--preview N` 渲染前 N 段」，和现在的「每人最长一句」不一致。`scripts/diarization_cache.py` 的 `VOICE_ORDER` 长度为 5；`studio_engine` 用 `index % len(VOICE_ORDER)` 分配。
- **为何重要。** 试听是合成前确认「谁是谁」的唯一步骤。3 人上限是上一轮修「试听总是谢谢」时留下的，没有对齐审查当时 4–5 人的验收。现行验收是音色数等于真实人数且逐人可辨。
- **建议改法。** 试听人数改为「文档里的说话人数，最多 5」（声槽数），不要无上限。继续用 `select_preview_segments` 这一处实现。第 6 人撞声不要靠加声槽解决；把它留在 P0 的预算里讨论即可。

### P1 · 已经写好的中文原因到不了界面

- **现象。** 声纹类异常能进 `progress.json` 的 `message`，因为类型名以 `Diarization` 开头，会被包成 `StudioInputError`。同一条边界把其它已经消毒过的中文原因换成「处理失败，请检查当前阶段、依赖和模型配置后重试」。Edge 403 的升级提示、缺少 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 的配置错误都属于这一类。真实句子在 `debug-failure.json`，界面不读这个文件。服务端把引擎的 stderr 丢进 `DEVNULL`。
- **证据。** `scripts/studio_engine.py` 的 `run`：只有 `StudioError` / `StudioInputError`（以及被改写成后者的 Diarization*）会把 `str(error)` 写入 progress。`scripts/tts_backends.py` 的 Edge 403 抛的是 `RuntimeError`。`scripts/podcast_cn.py` 的缺 key 抛 `DeepSeekConfigurationError` / `TranslationConfigurationError`。`scripts/studio_server.py` 启动引擎时 `stderr=subprocess.DEVNULL`。`classify_failure` 对不上关键字的 yt-dlp 失败（下架、私享、年龄限制等）收成 `process_failed`，原始 stderr 不落盘。
- **为何重要。** 声纹这条已经证明：用户看见的是 progress 里的中文，不是调试文件。403 和缺 key 是同一条流水线里同样常见的停点，提示被写了又被抹掉。
- **建议改法。** 允许一段固定的安全文案出去：`StudioError`、`StudioInputError`、`PodcastPipelineError`，以及 Edge 那句 403 提示。继续禁止把任意 SDK 字符串写进 progress。`classify_failure` 增加「不可用 / 私享 / 年龄限制」等固定中文，仍然不要保存原始 stderr。

### P1 · 一个对不齐的字幕 cue 会在声纹跑完后放弃整期

- **现象。** 某条字幕离最近语音超过 1.5s 时，分配结果是 `speaker_id=None`。`validate_diarization` 允许最多 20% 未对齐。工作室（以及 CLI）在这之后只要发现任意一个 `None`，就整期失败：「部分对白无法匹配说话人」/「仍有字幕无法分配到说话人」。片尾、音乐、广告画面上的一条字幕就够。
- **证据。** `scripts/diarization_core.py` 的 `_direct_assignment`：间隙 `<= 1500` 才做最近邻，否则 `method="unassigned"`。`validate_diarization` 的 20% 门在 `scripts/studio_engine.py` `prepare` 里排在「任意 None 即失败」之前，后者更严，20% 门在工作室路径上基本走不到。CLI 在 `prepare_episode` 里有同样的任意 None 检查。
- **为何重要。** P0 修好之后，长 YouTube 很容易停在下一道门，而且声纹时间已经花掉。
- **建议改法。** 未对齐的 cue 标成待核对或从译稿里丢掉，不要在声纹成功之后把整期作废。待核对段继续禁止直接合成（这条保留）。

### P1 · CLI 声纹缓存的指纹没有跟着折叠逻辑变

- **现象。** 软闭环（`5179175`）和 20s 折叠（`c20e482`）落地后，`backend_fingerprint` 里的 `stitching_version` 仍是 2，`cluster_threshold` 仍写死 0.5。CLI 缓存按这份指纹命中旧 turns，不会重跑新拼接。工作室自己不读 `DiarizationCache`，所以工作室的「重新准备」是新鲜的。
- **证据。** `scripts/diarization_runtime.py` 的 `backend_fingerprint`。`git log -S stitching_version` 停在 `334cd89`；折叠与软闭环的提交没有改这个字段。`scripts/diarization_cache.py` 的 `load_turns` 要求指纹全等。工作室 YouTube 实际阈值是 0.9，指纹里却是 0.5；今天工作室没用这份缓存，所以还没撞车。
- **为何重要。** 已经做好的鬼影治理对「缓存命中的 CLI 任务」不生效。若以后工作室复用这条缓存又不把真实阈值写进指纹，0.5 和 0.9 的结果会串。
- **建议改法。** 把 `stitching_version` 抬上去，并把实际使用的 `cluster_threshold` 写进指纹。只影响 CLI 下次重算，不改变工作室当前行为。

### P2 · 债，不挡下一刀

- **保存译稿会把所有段的 `ambiguous` 清掉。** `studio_server.py` 在写回每一段时设 `ambiguous=False`。界面文案把保存当成「我核对过了」，和实现一致。只改一个错字也会签收全部说话人。若要收紧：只清除 `speaker_id` 真的被改过的段。
- **`valid_audio` 把缺 ffmpeg 说成「音频无效」。** `StudioError` 是 `RuntimeError`，`valid_audio` 里除了取消以外的 `RuntimeError` 都返回 False。
- **文档落后于代码。** `STUDIO-ENGINE.md` 仍把 `diarization_threshold` 的默认值写成 0.5。YouTube 创建任务会写入 0.9，引擎在缺省时对 YouTube 也用 0.9，本地文件才是 0.5。同一页把 `--preview N` 写成「渲染前 N 段」，代码已改为每人最长一句。开区间 `(0, 2)` 与文档里的「不含边界」一致。
- **三套解释器是刻意的。** `.venv` 编排，`.speech-venv` 做 ASR/克隆，`.diarization-venv` 做 sherpa。合同里写了。别合成一个环境。
- **字幕失败后的 ASR 仍是 60 秒串行分块。** 回退原因已经会进进度和 `debug-subtitle-fallback.json`。慢是真的，静默丢字幕这件事已经不在了。
- **声纹分块 15 分钟、重叠 5 分钟、串行。** 长节目会重复算重叠区。这是时延，不是这次的正确性故障。
- **工作室分段 `max_chars=180`，CLI 默认 360。** 工作室段落更短、合成调用更多。Edge 单次上限 2500，180 离危险很远。
- **YouTube 0.9 / 本地 0.5。** 与夹具 `diarization-regression.json` 一致。配置里的显式值优先。不要再加第三档阈值。
- **下载阶段没有 ETA。** `source` / `transcript` 的 `total` 为 0，`estimate_eta_seconds` 回 `None`。声纹、翻译、合成有 ETA。
- **取消要等 sherpa 下一行 stderr。** `_stream_worker` 堵在读行上时，取消要等到下一条 `PROGRESS`。挂死且完全不打印时，取消也会等。
- **`finally` 里 `rmtree(temporary)` 使用 `ignore_errors=False`。** 清理失败会盖住原来的异常。
- **服务端只校验 YouTube 主机名是 https。** `https://www.youtube.com/feed/...` 这类地址要到 yt-dlp 才失败，再叠加上面的笼统 `process_failed`。
- **Qwen 三个男声槽的指令文本相同。** 区分靠音色名（Vincent / Neil / Eldric Sage）。默认后端是 Edge，五条神经音色是分开的。

## 看起来脏，但应留着

- Edge 默认、五声槽、克隆可选、克隆缺参考就停。不要为了「更像原声」加跨集声纹或名人克隆。
- 待核对段禁止试听和成片。这条是「不悄悄混声」。要收的是 P1 的「任意未对齐 cue 作废整期」和 P2 的「保存即签收全部」，不是这条禁令本身。
- 软闭环、无原型残余贴主导说话人、短于约 20s 的折叠。它们会把很短的真人嘉宾并进主导声。这是长访谈能做完的代价，留着。P0 只补「人数超标时按最短者并进最近原型」，不要再加新的阈值旋钮。
- preview 挑选只留 `scripts/studio_preview.py` 一处。上一轮 server/engine 各写一份的分叉已经合上。试听人数要改的是调用方的 `3`，不是再抄一份函数。
- 字幕优先，失败写 `subtitle_fallback_reason` 和 `debug-subtitle-fallback.json`。进度里一直带着这条原因，有点吵，但是找得到。
- 串行队列、原子 `progress.json`、任务目录外的路径拒绝、临时媒体在成功/失败/取消后删除。
- 工作室在声纹失败时失败关闭，而不是像 CLI `diarize=auto` 那样退回「没有说话人的译稿」。P0 要去掉的是过早的拒绝，不是失败关闭。
- 工作室不调用 `prepare_episode` 是有原因的：本地文件、可编辑文档、不能在声纹失败时静默丢掉说话人。共享的应当是 `run_sherpa_backend` 和字幕/翻译函数，而不是把 CLI 的 auto 回退接进来。

## 必须立刻修

只修 P0，并且一次补齐这些点：返回给拼接的 turns（含第一块）先折进**现有**上限，embedding 一起改，替换锁死旧顺序的测试，补一条「长于 20s 的多余 ID 也要折进上限」。不要把 6 改成 8。不要再抬 YouTube 阈值。不要让工作室在声纹失败时静默继续。

这一条已经落地。试听人数、安全中文错误、工作室单 cue、CLI 指纹里值得一次做完的部分，见下一节；那里同时写明哪些看起来相关、这次明确不做。

## 架构 / 流程优化

用户要求用 Opus 5.5 做整体审查。那次调用没有返回正文（周用量额度不足）。下面是审查当时的产品标准：私人英→中收听，大约 4–5 个声能分开，默认低成本 Edge，不要求名人克隆，不为「跨国串门儿」加匹配层。不是另一模型的输出。2026-10-28 起，验收改为音色数等于真实人数且逐人可辨。

流程仍是一条：取源 → 字幕或 60 秒 ASR → `run_sherpa_backend`（分块、预算折叠、拼接、短鬼影折叠）→ 对齐 → 20% 未对齐门 → 工作室补最近说话人并标待核对 → 翻译 → 试听或成片。CLI 与工作室共享声纹运行时。工作室不走 `prepare_episode`，因此不会在声纹失败时静默丢掉说话人。这是结构，不是债。

### 一次做完

| 问题 | 收益 | 改动面 |
| --- | --- | --- |
| 单块人数在折叠前硬拒。DOAC 类「一块 > 上限」停在 `声纹分块检测到的说话人数超出配置范围`。 | 折进现有 `exact` 或 `maximum`（6）之后才校验。第一块也折，embedding 跟着改，第二块不会再撞匹配器的 6 人门。 | 已在 `scripts/diarization_runtime.py`。上限不改成 8。 |
| 试听固定 3 段，盖不住 4–5 个可辨声。`revision()` 若仍按 3 算，和真实试听不一致时会把成品标成过期或不过期。 | 试听人数 = 文档里的说话人数，最多 5（现有声槽）。两人就试两句，不再用别人的句子垫满。服务端命令和过期检测用同一个 `preview_limit`。 | `scripts/studio_preview.py`、`scripts/studio_server.py`。挑选函数仍只有一处。 |
| 缺 key、Edge 403 的中文提示到不了界面；私享/下架/年龄限制收成笼统的「处理程序执行失败」。任意 SDK 字符串仍可能带密钥。 | `progress.json` 只放白名单类型的句子。带 `http(s)://`、`Bearer` 或 `token=` 的句子继续换成笼统失败。Edge 403 改抛 `TTSBackendError`，正文不含上游原文。私享类 yt-dlp 输出映射成固定中文，不落原始 stderr。 | `scripts/studio_engine.py` 的 `user_facing_message`、`classify_failure`；`scripts/tts_backends.py`。 |
| 一条离语音超过 1.5 秒的字幕，在声纹跑完后让工作室整期失败。20% 门比这条更松，实际走不到。 | 20% 门仍先执行。通过之后，工作室把仍未分配的 cue 贴到最近的已有说话人，标 `ambiguous`。试听和成片继续拒绝待核对段。克隆参考仍跳过这些 cue。 | 新函数 `fill_unassigned_for_review`，只由工作室 `prepare` 调用。CLI 的 1500ms 语义和「任意 None 即失败」不动。 |
| CLI 缓存指纹停在 `stitching_version` 2，软闭环、20 秒折叠和这次的人数预算都不会重跑。 | 升到 3，旧 turns 缓存失效，下次 CLI 声纹重算。 | `backend_fingerprint` 一个字段。 |
| `valid_audio` 把缺 ffmpeg 说成「音频无效」。若把所有 `StudioError` 都抛出去，坏缓存又会变成整期失败。 | 只有 `dependency_missing` 向上抛。ffmpeg 解码失败仍返回假，缓存重做。 | `StudioEngine.valid_audio`。 |

### 明确不做

- 不把说话人上限从 6 改成 8，不加第 6 套声槽。第 6 人仍会循环到已有槽，试听也不覆盖第 6 人。审查当时的验收是 4–5 个可辨声。现行验收是音色数等于真实人数且逐人可辨；人数不符或多人共槽都不能合成。
- 不再把 YouTube 聚类阈值抬过 0.9。阈值越大簇越少，再抬会并掉真的客人。
- 不把 CLI `diarize=auto` 的静默回退抄进工作室。声纹失败继续失败关闭。
- 不为工作室新做一份声纹缓存，不把工作室的 0.9 写进 CLI 指纹里那个写死的 `cluster_threshold`。工作室不读 `DiarizationCache`。指纹里的 0.5 只描述 CLI 默认。
- 不改保存译稿时清掉全部 `ambiguous`。界面把保存当成「我核对过了」。只改一个错字也会签收全部，这是已知代价，不是这次的炸点。
- 不改 CLI 对齐的 1500ms，不改 CLI「任意未分配 cue 即失败」。
- 不合并 `.venv`、`.speech-venv`、`.diarization-venv`。不并行声纹分块，不改 60 秒 ASR，不加跨集声纹或名人克隆。
- 不改 `rmtree(..., ignore_errors=False)`。清理失败盖住原异常的窗口很小。

### 做完之后仍在的边界

- 未分配比例超过 20% 时，填充之前就会失败。短文件里 4 条字幕有 1 条离得很远，仍可能整期停下。
- 待核对段在用户改正或保存之前，试听和成片都会停。保存仍会签收每一行。
- worker 的 `--max-speakers` 仍不读取。人数预算在 Python 折叠，不在 sherpa。
- 第 6 个说话人与某一声槽同声，试听听不到这个人。
- 未重跑 DOAC 那条 YouTube。单测覆盖的是「一块里 7 个 ID，含第一块、含都长于 20 秒」会折进 6 再继续，而不是那条视频本身。
- `tests/test_diarization_runtime.py` 里要求本机安装声纹运行时的那条，在 Linux 上仍会因「当前声纹版本仅支持 macOS arm64」失败。这是平台检查，不是这批改动。
