# Norton 真机四连完成回报

- 分支：`feature/shengjian-point-listen-jump-continue`。
- 装机源码提交：`3752757afb4b20347a42a0bcc05e1de5d077ac93`；资料提交为本文件所在的后续 evidence commit，可用 `git log --oneline -- attention/shengjian-point-listen-device-accept-2026-11-09/manual-four/codex-last-message.md` 定位。
- 装机包：`dist/PrivateListen-norton-device-four-2026-11-09-3752757-iphoneos.app`；覆盖安装到 `00008160-001639DA3480000A`，未卸载、未 wipe。
- 原始手机报告：[device-report.json](device-report.json)；四连报告：[manual-four-report.json](manual-four-report.json)。唯一运行编号：`norton-four-3752757-20261109-b`。真实 iPhone 执行与 copy-from 回执齐全，19 项核验通过。

| 验收项 | 结果 | 事实 |
| --- | --- | --- |
| P-Play | PASS | Norton s1 / i9 两段真实 jumpContinue，实际推进约 4.45 / 4.16 秒，越过 t_end 不停；点击未写 completed。 |
| P-Continue | PASS | 退出同路径 checkpoint 后保存约 439.8473 / 2160.6583 秒；Continue 为本集，未完成；额外普通冷启动落盘位点一致。 |
| P-ListenNote | PASS | 以装机源码 SHA 绑定的源码断言：PlayerView 无条件挂载听后感 TextEditor，提炼之前可达；未自动写听后感，文件字节不变。 |
| P-Merge | PASS | 单集已落盘、library.json 未整库盖、基线 52 保留；引用既有 device-file-verification，本轮前后字节与 SHA 一致。 |

`make shengjian-before-claim-ready` 在 CURRENT 更新到本包后通过。模拟器 fixture 没有被冒用，实际物理手点为 false，manual_four_probes_performed=true 表示用户授权的真机等价自驱验收。

四连任务：**无用户步骤**。仍禁止宣称可听：`claim_phone_listenable=false`，音频语义落点未听辨；时长门禁、seek 偏差 0 秒和真实播放不替代语义验证。

首轮延后取样出现晚于探针完成的另一次进度写入，已保留 [事实记录](attempt-a/summary.json)；最终采用立即冷启动核验通过的 b 轮证据，未虚报首轮完成。

PR#31 保持 OPEN / draft，不合并；未全库重抽、未自动入队、未整库 staging 覆盖、未重推库。最终远端状态见 [pr-after.json](pr-after.json)。
