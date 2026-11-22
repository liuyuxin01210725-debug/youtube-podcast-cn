# 声间 · 先 push 闸与脱敏公开谱系（2026-11-22）

时区：Asia/Taipei。执行指针，不替代既有权威：`shengjian-git-backup-gate-2026-11-03.md`、`bot-git-retention-crossline-2026-11-03.md`。

- 可运行 tip 对外生效或作为正式装机 tip 前，必须能在 GitHub 远端取到该提交。入口：`make shengjian-require-remote-tip TIP='branch@完整SHA'`（也可 `SHA=`）。`make shengjian-before-claim-ready` 必须提供 `TIP`/`SHA`，并先验证远端分支、构建源记录和 CURRENT 包。离线单测覆盖「远端不存在的 tip 必须拒绝」，普通 `make test` 不访问网络。
- 本分支 `fix/shengjian-unified-backend-e-r-u-20261120-sanitized` 是本地现装源 tip `0c259eeb2a34e428b8b94c10edf3fcc591753ebf` 的脱敏公开对应，不是原 SHA，不能用来冒充现装提交。既有远端基线（`a002646` 及其祖先）如已含设备标识，本轮不改已推历史，也不宣称那些旧对象已被清理。
- 同版安全索引（无 UDID、无私人路径）：10:22 签包 `PrivateListen.debug.dylib` SHA-256 `67048e2cb3f27b116323439d37ebb2b1510a48cc2b51bc1597dfa4567ce2fac7`，CDHash `bccd630b51f3c9119c9c0788bc467b8a5ce22f80`，stub SHA-256 `556be37abb2406a10a371d2f643fcf559f25cb46c8e4ffa7141e6ca7b20aed65`；官方安装发生于 2026-11-22 10:23 Asia/Taipei，容器 `EE943AF5-347C-4E46-B2B1-8E28CE0CEB9A`。现机资料库 generation 2，`library.json` SHA-256 `95a162690f9f6745e405967dd21f80d4754cf60e6ee0421f3a408996d3d74658`。`dist/CURRENT-iphoneos` 仍指向较旧包，本轮不改现场生产树。
