详情以实际观点或可检验问题导航，点击从最终中文 output.mp3 秒轴 jumpContinue 续听，越过 t_end 不自动停。点击与 seek 落地不写完成，真实播放后保存该集 Continue/ListenMark；保留 clip 窗停旁路、听后感与星标。生产 64/16 位 transcript 指纹归一，缺失、错绑、stale、非法和超时长锚点继续拒跳。

新增仅 DEBUG 真机显式启动的 `-NortonDeviceFourProbe`，与模拟器 fixture 隔离，读取手机已有 Norton 单集 7 条索引和真实音频 SHA。探针复用生产 LibraryView 进度回调与退出 checkpoint，读取 AVPlayer 实际时钟，在 Documents 写 JSON 回执；不自动写听后感，不重推库。

验证与真机证据：

- 装机源码 `3752757`；Debug-iphoneos 构建、签名、指定 iPhone 覆盖安装和唯一编号启动通过。未 uninstall / wipe。
- **P-Play PASS**：Norton s1 / i9 两段分别真实推进约 4.45 / 4.16 秒，越过原始 t_end 不停；点击未写完成。
- **P-Continue PASS**：生产 checkpoint 后 Continue 为 Norton，保存约 439.8473 / 2160.6583 秒且未完成；额外普通冷启动后偏好文件仍保存末段真实位点。
- **P-ListenNote PASS（源码断言）**：装机源码 SHA 一致，PlayerView 无条件呈现听后感 TextEditor，位于提炼之前；手机听后感文件字节不变，未自动写入。
- **P-Merge PASS（只读核验）**：单集已落盘、library.json 未整库盖、基线 52 保留。与既有 device-file-verification 及本轮前后回读 SHA 一致；没有执行整库发布。
- `make shengjian-before-claim-ready` 在 CURRENT 更新到实际新装包后通过；四连报告 19 项核验全过。真实设备原始报告通过 devicectl copy from 拉回；未以模拟器结果冒充。

证据：`attention/shengjian-point-listen-device-accept-2026-11-09/manual-four/manual-four-report.json`、`device-report.json`、`codex-last-message.md`；目录 README 说明完整边界。首轮延后取样有一次探针完成后的额外进度写入，触发者未确定；最终采用重跑后立即冷启动核验通过的 b 轮证据，原差异保留。

`manual_four_probes_performed=true` 表示已授权的真机等价自驱验收，实际物理手点为 false。**音频语义落点未听辨，claim_phone_listenable=false，继续禁止宣称真机可听。** 0 秒 seek 偏差与时长门禁不证明语义正确。四连任务无用户步骤，保持 draft，不合并；未全库重抽、未自动入队、未整库 staging 覆盖。

本轮未重跑完整 make test；首轮 test-public 曾因 31 处既有本地资料个人路径失败停止，未宣称完整套件全绿。
