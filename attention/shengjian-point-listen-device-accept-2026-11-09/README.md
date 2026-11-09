# 声间点段跳听 · Norton 真机装机回执

已完成生产指纹修复、Norton 单集绑定、覆盖装机及设备文件回读；**真机手点四连未做，四层证据未齐，不能宣称「手机可听」「真机可听」或「四连通过」**。

- 分支：`feature/shengjian-point-listen-jump-continue`。
- 实现及装机源提交：`f4b3c47`，基于 `f570351`；本回执由后续资料提交追加，未再改 App 源码。
- [PR#31](https://github.com/liuyuxin01210725-debug/youtube-podcast-cn/pull/31)：OPEN / draft，目标 `feature/shengjian-point-clip-tend`；不合并。最终远端状态以 `pr-after.json` 为准。
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

## 装机与文件落盘

- 包：`dist/PrivateListen-point-listen-norton-2026-11-09-f4b3c47-iphoneos.app`
- 构建：Xcode 27.2，Debug-iphoneos，`DEVELOPMENT_TEAM=3CK4AMUHH5`，BUILD SUCCEEDED；正常本机信任服务下签名校验 exit 0。
- 设备：`不取经的小刘`，UDID `00008160-001639DA3480000A`，available / paired。
- 覆盖安装成功，未 uninstall / wipe；App 在架、启动成功，进程 PID `3254` 与新包安装 URL 一致。启动成功不代表打开了详情或实际播放。
- `dist/CURRENT-iphoneos` 与 `dist/PrivateListen-CURRENT-iphoneos.app` 已指向本包；`CURRENT-iphoneos.sha.txt` 已更新，旧值见 `dist-pointers.json`。dist 本机包及指针沿用既有不入库方式。
- 只向 `Documents/中文播客私人库/episodes/42786039aaf346a1bf851613199e260a/` 写入：output.mp3、cover.png、transcript_zh.json、extract.live.json、extract.lkg.json、extract.json、point-listen.json、episode.json；未写入 star/deep_request 等控制文件。
- 8 文件从手机回读后全部 SHA-256 和长度匹配，音频 hash 与索引一致；回读字节也通过生产 Swift 解码/绑定/时长门禁 **7/7**。该探针在 Mac 上检查手机回读文件，不是手机内播放器探针。
- 基线 52 集 → 52 集；其余 51 集目录记录相同；手机 `library.json` 回读字节前后完全一致。未整库重推。

## 实测与未完成边界

| 四层证据 | 本轮结果 |
| --- | --- |
| (a) 成片音频 | SHA、ffprobe、ffmpeg 全段解码、AVFoundation 通过 |
| (b) 提炼/索引 | ready deep 包、7 条合法窗口、生产 Swift 绑定校验通过 |
| (c) 远端字节回读 | App 容器远端回读 8 文件一致；未重新回读/发布 OSS 或 iCloud |
| (d) App 打开详情并实测 ≥2 段跳听 | **未手点、未验收**；只有安装/启动/进程证据 |

| 四连探针 | 自动证据 | 真机手点 |
| --- | --- | --- |
| P-Continue | ContinueUXProbe / LibraryListProbe、seek 与实际播放完成边界通过 | **未做** 3–5 秒播放→退出→Continue 卡与真实位点 |
| P-Merge | 单集直拷，52 集基线保留，library.json 字节未变 | **未手点**；本轮未执行整库发布旅程 |
| P-Play | 装机、在架、启动、回读音频与 7 条绑定门禁通过 | **未做**详情 ≥2 段普通跳播、落点、越段尾续听、Continue 更新及点击不完成 |
| P-ListenNote | 当前安装包的源码/二进制听后感探针通过 | **未做**播放页入口目视/操作 |

`make shengjian-before-claim-ready` 通过，且 CURRENT 更新后再次通过。既有 `PointListenProbe` 只在 DEBUG simulator 编译，本轮未冒用为真机探针。额外 `make test` 在 `test-public` 停止：`test_public_files_contain_no_personal_absolute_paths` 对既有本地资料报 31 处失败，本次改动文件不在失败列表，未擅改这些资料；其余完整套件目标未执行，不能称完整测试全绿。

真实剩余验收：没有现成的真机 UI 自动化，本轮未手点上述四连，未听辨这 7 条现有锚点的语义落点。无装机/拷贝/本机门禁阻塞；四层尚缺 (d)，保持草稿，不作可听宣称。

## 可核查证据

- [实现 red 回归](hash-regression-red.log)、[最终本机门禁](installed-build-local-gate.log)、[完整测试范围外失败摘要](full-tests-summary.json)
- [构建清单](build-manifest.json)、[覆盖安装回执](device-install.json)、[App 在架/启动/运行探针](device-app-probe.json)
- [设备文件字节比对与基线保留](device-file-verification.json)、[手机回读文件 Swift 绑定探针](norton-device-readback-binding-probe.json)
- [staging 绑定探针](norton-staging-binding-probe.json)、[索引筛选审计](norton-index-audit.json)、[音频解码记录](audio-decode.json)
- 原始构建、完整测试、全进程等较大本机日志保留在本目录，提交只选本任务必要证据；不提交无关 `_raw`、`.tmp` 或音频/App 二进制。
