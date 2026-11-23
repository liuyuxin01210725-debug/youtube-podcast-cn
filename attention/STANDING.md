<!-- 声间本项目 Standing 指针：只约束本 App 真机人工矩阵，不改跨产品共用规则，不另开 L0。 -->

「可听」以装机后的真机人工矩阵为准，包括人耳实听和手点。详情见 `attention/装机宣称可听前必跑.md`。听后感读取失败时锁写并保留原字节。单集音轨或修订损坏只隔离该集。

官方签包只走本安全工程的 `make shengjian-sign-iphoneos OUTPUT=<隔离目录>`。要求干净树（含非 ignored 未跟踪文件）、已推送的安全分支 tip、真实源 SHA、构建墙钟戳和产物 hash 校验。路径、参数或门禁不满足即停止。禁止改用直接 xcodebuild 或旧脚本、传 `-allowProvisioningUpdates`、下载 profile 或向源码写版本戳。`branch@SHA` 绑定源版本，`BUILD_TIMESTAMP` 记录本次构建墙钟（Asia/Shanghai，禁止使用 Git AuthorDate）。签包成功只留隔离产物与记录。

真实官方安装成功且校验通过后，才调用 `shengjian_current_promote.py` 原子提升 CURRENT，依据实际安装和校验记录，禁止手填成功标记。未装、安装失败或校验失败时保留原 CURRENT。隔离或 mock 干跑仅验证门禁。2026-11-22 用户明确本轮只检查和准备，手机和 live CURRENT 保持原样，覆盖安装由本人另行授权。

Mac 普通发布的音频预检纠正及目录事务验收边界见 [Mac 发布音频预检纠正](t624-mac-publish-preflight.md)。
