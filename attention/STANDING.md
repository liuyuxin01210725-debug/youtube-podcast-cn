<!-- 声间本项目 Standing 指针：只约束本 App 真机人工矩阵，不改跨产品共用规则，不另开 L0。 -->

本项目「可听」的权威仍是真机人工矩阵（人耳实听 + 装机后手点），不是宣称文、错回归表或模拟器自动回归。构建版本以签包记录里的源完整 SHA 与 BUILD_TIMESTAMP（本次构建墙钟，时区 Asia/Shanghai，不是 Git AuthorDate）为准；CURRENT 只在官方实机安装并且对应校验通过后才原子更新。详情见 `attention/装机宣称可听前必跑.md`。签包走 `make shengjian-sign-iphoneos OUTPUT=…`，不要用绕过门禁的临时 xcodebuild 当官方入口。
