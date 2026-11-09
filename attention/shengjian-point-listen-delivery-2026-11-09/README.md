# 声间点段跳听 · 本机功能交付回执

日期：2026-11-09；本机会话模型元数据：`gpt-6.1-sol`。基线为当前 tip `7d8403d`；新功能分支 `feature/shengjian-point-listen-jump-continue`；PR 目标为 `feature/shengjian-point-clip-tend`（远端仍为该 tip），独立功能提交，不合并。调查 PR #30 未纳入本分支；已有工作区文件未整理或混入提交。

## W1 / W2 的实际完成范围

- **W1：源码与隔离样包达到**。详情的主列表只有一句观点/可检验问题；不再逐槽渲染 Claim/Method/Entity/Quote。旧包仅复用实际 claim/question 文案，未授权音频绑定时只读。无起点的段禁跳；整集星、要点星、重抽菜单与听后感入口保留。明确索引数组按语义顺序，隐藏寒暄不裁音频。
- **W2：源码与真实模拟器 AVPlayer 达到**。Double 秒轴、无 lead-in、无段尾停播；跳后实际播放头通过 `onProgress → LibraryStore.remember` 持久化，Continue 仍用 `episode.id`。点击和 seek 落地不写 mark；实际向前播放后沿用原末 15 秒完成规则。设计和边界用例先于播放器改动建立。
- **W3/W4：本机覆盖**。clip 仍在独立旁路，无主面新按钮；修复其窗停/暂停后退出补写 mark 的泄漏。真实本地 `output.mp3` SHA-256 + 解码时长校验，索引绑定失配/为空/过期拒跳，`span: String?` 未变，包级 status 枚举未变。
- **W5：仅 fixture 等价样包**。5 条手工语义句/问题 + 1 条隐藏寒暄；测试音与包有独立可校验绑定。没有对生产 Norton 重抽或生成索引。
- **W6：未发布**。无云/OSS/iCloud/USB 推库；无 staging 覆盖、无自动入队；不改变合并逻辑。

## 已运行验证

| 验证 | 结果与证据 |
| --- | --- |
| `make shengjian-before-claim-ready` | PASS：ContinueUXProbe、LibraryListProbe、ExtractDisplayChecks、新增 PointListenChecks、现有源码/二进制门禁 |
| PL-D1–PL-D5 | PASS：缺锚、空/错音频绑定、错 work/packet、stale 译稿/包、mock、非法数字/源轴/反向范围、超时长；同包有效兄弟段仍可跳 |
| 末 15 秒合成边界 | 584.9/585/590（总长 600）点击不写；实际播放按原阈值完成；没有改 `LibraryStore.reducing` |
| Xcode Debug simulator build | PASS，Xcode 27.2 / iOS 27.2 SDK，arm64 + x86_64，`CODE_SIGNING_ALLOWED=NO` |
| FFprobe / FFmpeg | 样音 90 秒、180369 字节；完整解码 PASS；sha256 见 `verification.json` |
| 真实 AVPlayer 探针 | **14/14 PASS**，见 [simulator-probe.json](simulator-probe.json)；3.25→约7.28s、18.5→约22.56s，均超过各自 t_end；Continue 保存相同真实位点 |
| 竞态与旁路 | 快速连点只留最新落点、跨集旧回调作废、clip 窗停与退出不写 mark、clip 后主跳恢复续听写入、末段落地后暂停不完成，均 PASS |
| UI 视觉 | [viewpoint-preview.png](viewpoint-preview.png)：实际 SwiftUI 列表组件、5 句可见、无旧槽第二面；仅隔离预览截图，没有手动点按旅程记录 |
| `git diff --check` / shell syntax | PASS |

运行门禁时将编译缓存放 `/tmp/shengjian-point-module-cache`。初次受限编译因 SwiftUI 宏插件/CoreSimulator 权限失败，正常本机 Xcode 权限重跑成功；不是 App 编译缺陷。`dist/CURRENT` 的门禁扫描只验证已有二进制标记，不代表它已换成本功能；本功能编译产物在 `/tmp/shengjian-point-listen-build/Build/Products/Debug-iphonesimulator/PrivateListen.app`。

## 四连/四层边界（不混报）

- P-Continue / P-Play：真实模拟器播放器和持久化链路已跑（不是手点 UI/人听），同集两段、每段约4秒，段末不停。
- P-Merge：本批未推库，未做端到端发布合并旅程；既有代码不变。
- P-ListenNote：原入口源码顺序与二进制字符串门禁通过；本批未手点输入/保存听后感。
- 四层 (a)/(b)：仅隔离音频/手工索引已验证；(c) 生产远端回读未做；(d) 真机未跑。**不得称 Norton 已可跳、真机可听或完整四连已过。**

ops D1–D5 原义未改。D1 本机防 mock 门禁、D2 Continue、D5 新回归门禁有证据；D3 无发布且合并代码不动；D4 生产译稿发布闸未执行。它们不由 PL-D* 替代。

## 真实阻塞与未做项

Norton `42786039aaf346a1bf851613199e260a` 的 `audio_sha256`/权威成片版本仍未知，历史 `73fd…` 是包指纹；旧包不会空绑定放行。生产单集索引、单集基线∪增量发布、生产远端回读和真机 UI 四连/听感验收均未做。无全库重抽，无队列或外部费用调用。

## 相关文件

- `ios/PrivateListen/PrivateListen/ExtractDetailSection.swift`：一句导航主列表与段级不可跳提示。
- `ios/PrivateListen/PrivateListen/PointListening.swift`：索引模型、秒轴拒跳、音频核验、seek/实际播放分界。
- `ios/PrivateListen/PrivateListen/PlayerModel.swift`：`jumpContinue`、seek 回调隔离与 clip 不写 mark。
- `ios/PrivateListen/PrivateListen/PrivateListenApp.swift`、`PointListenProbe.swift`：显式 Debug simulator 探针/预览；普通启动不启用。
- `ios/PrivateListen/PrivateListenTests/PointListenChecks.swift`、`Makefile`：PL-D* 与边界门禁。
- `scripts/probe_point_listen_sim.sh`：隔离模拟器复现入口。
- `fixtures/point-listen/`：单集隔离样包；`docs/shengjian-point-listen-design.md`：设计边界。
- 本目录：14 项报告、验证元数据、组件截图与本回执。

回滚仅撤本功能提交及单集 `point-listen.json`；不删用户音频/星/听后感、不整库覆盖。
