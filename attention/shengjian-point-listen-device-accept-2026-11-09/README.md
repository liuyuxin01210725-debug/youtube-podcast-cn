# 声间点段跳听 · Norton 真机装机回执

已完成生产指纹修复、Norton 单集绑定、覆盖装机和真机四连等价自驱验收。**P-Play / P-Continue / P-ListenNote / P-Merge 全部 PASS**；这是指定 iPhone 的真实 AVPlayer 回执，实际手点为 false。语义落点仍未听辨，`claim_phone_listenable=false`，继续禁止宣称「手机可听」「真机可听」。

- 分支：`feature/shengjian-point-listen-jump-continue`。
- 当前装机源提交：`3752757afb4b20347a42a0bcc05e1de5d077ac93`；核心实现与指纹修复来自 `f570351` / `f4b3c47`。本轮仅追加 DEBUG 真机探针、只读时钟和 opt-in 启动入口；模拟器 fixture 仍隔离。
- [PR#31](https://github.com/liuyuxin01210725-debug/youtube-podcast-cn/pull/31)：OPEN / draft，目标 `feature/shengjian-point-clip-tend`；不合并。本轮远端状态以 `manual-four/pr-after.json` 为准。
- 本会话模型从当前 session 的 `turn_context.model` 核实为 `gpt-6.1-sol`；未启动 CloudAgent，未切模型、未委派。

## 契约与 Norton 索引

生产包的 64 hex transcript SHA 与既有 16 hex 统一为小写前 16 位。严格/宽松 packet 解码、索引解码、跳转比较共用同一归一规则；非法长度/字符、错误 16 位继续拒跳。保留 extract 原始字节中的完整 64 位，不重写生产提炼包。回归先复现失败，再通过 64→16 可跳、大小写归一、错 16 与非法指纹拒跳；PL-D1–PL-D5 和隔离 fixture 通过，ops D1–D5 未改。

- work_id：`42786039aaf346a1bf851613199e260a`
- packet_id：`42786039aaf346a1bf851613199e260a-spine-v1-live-20261103T134310Z`，ready / deep，非 mock。
- transcript_hash：`16962cd5c25558ff`
- audio_sha256：`fcb337634866181e6107eb6cb519006fe9102c756b35f83bb8d1410a04749f25`
- timeline：`output.mp3.seconds`
- ffprobe / episode 时长：3809.112 秒；AVFoundation：3809.160 秒，相差 48 毫秒，探针按 0.1 秒容差核验，时间窗口仍按较短的 3809.112 秒生成门禁。
- 索引：[point-listen.json](point-listen.json)，7 条一句话 claim，顺序保留 extract 的观点先后。已同步到 staging 的该集目录及 App 本机副本。

| claim | t_start | t_end |
| --- | ---: | ---: |
| s1 | 435.4 | 438.9 |
| s2 | 2816.0 | 2837.7 |
| s3 | 2887.4 | 2904.9 |
| i3 | 1187.4 | 1194.1 |
| i8 | 783.7 | 788.1 |
| i9 | 2156.5 | 2158.1 |
| i11 | 1513.8 | 1544.0 |

时间取自现有 extract 锚点且已做时长门禁，未改任何起止时间；越界 claim 不收录，method 大标题不作主观点。`span` 保留原始 `String?` 子串，7 条均在译稿中查到。**时长内不等于语义落点正确**，整份 extract 有疑似错轴条目，仍需真机听辨。未重抽、未自动入队、未全库发布，未覆盖 OSS/iCloud 或 `library.json`，未使用历史 `73fd…` 作为音频 hash。

## 首轮装机与文件落盘（历史证据）

- 包：`dist/PrivateListen-point-listen-norton-2026-11-09-f4b3c47-iphoneos.app`
- 构建：Xcode 27.2，Debug-iphoneos，`DEVELOPMENT_TEAM=3CK4AMUHH5`，BUILD SUCCEEDED；正常本机信任服务下签名校验 exit 0。
- 设备：`不取经的小刘`，UDID `00008160-001639DA3480000A`，available / paired。
- 覆盖安装成功，未 uninstall / wipe；App 在架、启动成功，进程 PID `3254` 与新包安装 URL 一致。启动成功不代表打开了详情或实际播放。
- `dist/CURRENT-iphoneos` 与 `dist/PrivateListen-CURRENT-iphoneos.app` 已指向本包；`CURRENT-iphoneos.sha.txt` 已更新，旧值见 `dist-pointers.json`。dist 本机包及指针沿用既有不入库方式。
- 只向 `Documents/中文播客私人库/episodes/42786039aaf346a1bf851613199e260a/` 写入：output.mp3、cover.png、transcript_zh.json、extract.live.json、extract.lkg.json、extract.json、point-listen.json、episode.json；未写入 star/deep_request 等控制文件。
- 8 文件从手机回读后全部 SHA-256 和长度匹配，音频 hash 与索引一致；回读字节也通过生产 Swift 解码/绑定/时长门禁 **7/7**。该探针在 Mac 上检查手机回读文件，不是手机内播放器探针。
- 基线 52 集 → 52 集；其余 51 集目录记录相同；手机 `library.json` 回读字节前后完全一致。未整库重推。

## 本轮真机四连结果

- 当前包：`dist/PrivateListen-norton-device-four-2026-11-09-3752757-iphoneos.app`；Debug-iphoneos 构建及签名校验 exit 0，覆盖安装到同一 UDID，未 uninstall / wipe。两个 CURRENT 指针及说明均已更新到本包。
- 启动：`devicectl device process launch … -- -NortonDeviceFourProbe -NortonProbeRunID norton-four-3752757-20261109-b`。探针只在 `DEBUG && !targetEnvironment(simulator)` 编译，且必须显式带参；普通启动不运行。
- [真机原始回执](manual-four/device-report.json)：设备 Documents 写入后用 `devicectl device copy from` 拉回，`device=physical`、`done=true`、`device_checks_pass=true`，运行编号与启动回执一致。
- [四连汇总报告](manual-four/manual-four-report.json)：19 项 host/device/source 核验通过，`manual_four_probes_performed=true`；该字段表示用户授权的等价自驱验收，`actual_hand_taps_performed=false`，不冒称物理手点。

| 四连 | 结果 | 本轮证据与边界 |
| --- | --- | --- |
| P-Play | **PASS** | 同集真实 `jumpContinue` 两段；s1 435.4 → 439.8468 秒，i9 2156.5 → 2160.6582 秒，均真实推进 ≥3 秒并越过原始 t_end；仍在播、无 clip 窗停。点击时没有进度/完成写入，播放后仍未完成。 |
| P-Continue | **PASS** | 两段短播约 4.45 / 4.16 秒后调用与播放页退出相同的 checkpoint；沿用 LibraryView 生产回调，不替换为探针写入。Continue 卡均为 Norton，保存 439.8473 / 2160.6583 秒。额外普通冷启动后，真实偏好文件与末段保存位点一致，最新未完成条目仍为 Norton。 |
| P-ListenNote | **PASS（源码断言）** | 装机源码 SHA 一致；PlayerView 第 96 行无条件呈现 ListenNoteSection，位于提炼之前，TextEditor 可访问标签为「听后感」。真机回执确认听后感文件前后字节相同；未自动写听后感，未冒称目视或手点。 |
| P-Merge | **PASS（本地只读核验）** | 单集已落盘、library.json 未整库盖、基线 52 保留；真机可见集数与 catalog 均 52，本轮前后 library.json 字节相同，SHA 仍与既有 device-file-verification 一致。本轮没有上传任何库文件，也未执行整库发布旅程。 |

两锚点 AVPlayer 首次落地均与 t_start 偏差 0 秒；记录只证明时钟定位，**不证明观点与音频语义正确**。7 条原锚点均未改。

首轮 a 的设备播放检查通过，但延后回取偏好时出现 1193.6758 秒，写入时间比探针完成晚 13 秒。该后续写入的触发者未确定，不能据此认定持久化缺陷；本轮 b 立即冷启动回取后通过。[首轮差异记录](manual-four/attempt-a/summary.json)保留事实与推断边界，未以失败首轮作完成宣称。

`make shengjian-before-claim-ready` 本轮通过，CURRENT 指向实际新装包后再次通过；[最终门禁日志](manual-four/installed-local-gate.log)。初次受限环境的 Swift 缓存写入失败，通过显式可写的 CLANG_MODULE_CACHE_PATH 重跑解决。Xcode / CoreDevice / 签名使用本机服务权限执行。

| 四层证据 | 当前结果 |
| --- | --- |
| (a) 成片音频 | 首轮 SHA、ffprobe、ffmpeg 全段解码、AVFoundation 通过；本轮手机内重新核对真实音频 SHA。 |
| (b) 提炼/索引 | ready deep 包、7 条合法窗口、生产 Swift 绑定校验；本轮直接读取手机 Norton 包。 |
| (c) 远端字节回读 | 既有 App 容器回读 8 文件一致；本轮未重新发布 OSS/iCloud 或重拷资料库。 |
| (d) 真机实际 ≥2 段跳听 | 本轮指定 iPhone 自驱真实 AVPlayer 路径通过；允许的听后感源码断言通过。未做物理手点与音频语义听辨。 |

本轮没有重跑完整 `make test`；首轮完整测试曾在 `test-public` 因 31 处既有本地资料个人路径失败而停止，仍不宣称完整套件全绿。

四连任务**无用户步骤**；未听辨语义落点的限制继续保留，不把验证转交用户，不作可听宣称。PR 保持 OPEN / draft，不合并。

## 可核查证据

- [实现 red 回归](hash-regression-red.log)、[最终本机门禁](installed-build-local-gate.log)、[完整测试范围外失败摘要](full-tests-summary.json)
- [构建清单](build-manifest.json)、[覆盖安装回执](device-install.json)、[App 在架/启动/运行探针](device-app-probe.json)
- [设备文件字节比对与基线保留](device-file-verification.json)、[手机回读文件 Swift 绑定探针](norton-device-readback-binding-probe.json)
- [staging 绑定探针](norton-staging-binding-probe.json)、[索引筛选审计](norton-index-audit.json)、[音频解码记录](audio-decode.json)
- 原始构建、完整测试、全进程等较大本机日志保留在本目录，提交只选本任务必要证据；不提交无关 `_raw`、`.tmp` 或音频/App 二进制。

- 本轮 [构建清单](manual-four/build-manifest.json)、[覆盖安装](manual-four/device-install.json)、[唯一编号启动](manual-four/device-launch.json)、[报告回取](manual-four/copy-device-report.json)、[普通冷启动](manual-four/normal-relaunch.json)
- [核验脚本](manual-four/verify_report.py)、[完成回报](manual-four/codex-last-message.md)。完整偏好/52 集目录原件只留本机；提交包含核验 SHA 与 Norton 的必要进度事实。
