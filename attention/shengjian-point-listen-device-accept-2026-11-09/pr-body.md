详情以一句实际观点或可检验问题导航；点击从最终中文 output.mp3 的 Double 秒轴继续播放，越过 t_end 不停，并把真实播放头保存到该集 Continue/ListenMark。点击和 seek 落地不写完成，实际播放后沿用末 15 秒完成规则；clip 保留独立窗停旁路，听后感、整集星、要点星保留。

生产 extract 的 64 hex transcript SHA 与既有 16 hex 统一为小写前 16 位，覆盖 packet 严格/宽松解码、index 解码和跳转比较；错误 16 位、非法指纹、缺失/失配音频、stale、非法或超时长锚点继续拒跳。ops D1–D5 原义未改，用例仍为 PL-D1–PL-D5。

Norton 单集新增 7 条 claim 索引，时间原样取自现有 extract，仅收录 3809.112 秒内有效窗口，不采用 method 大标题。绑定音频 SHA-256：`fcb337634866181e6107eb6cb519006fe9102c756b35f83bb8d1410a04749f25`。时间门禁不证明语义落点；未重抽、入队或发布整库。

本轮验证：

- `make shengjian-before-claim-ready` 通过；含 ContinueUXProbe、LibraryListProbe、ExtractDisplayChecks、64→16/错16回归及 PL-D1–PL-D5，fixture 仍绿。CURRENT 指向新包后再次通过。
- Debug-iphoneos 构建、签名校验、覆盖安装到已配对真机通过；App 在架、启动与进程验证通过。
- 仅拷 Norton 8 个必要文件，手机回读 SHA 和长度全部匹配；生产 Swift 对回读文件的绑定探针 7/7。资料库 52→52，其余 51 集目录记录不变，library.json 字节完全相同。
- 音频全段解码通过；AVFoundation 时长 3809.160 秒，比 ffprobe 多 48 毫秒，已记录。
- 额外 `make test` 在公共包个人路径检查中报 31 处既有本地资料失败，停于 test-public；未称完整套件通过，未清理这些无关文件。

回执与精简证据：`attention/shengjian-point-listen-device-accept-2026-11-09/README.md`。装机源提交 `f4b3c47`，验收资料由后续提交追加。

**未完成：真机手点四连、详情 ≥2 段普通跳播与语义落点、短播退出后的 Continue、听后感入口目视。** 四层证据缺 (d)；Mac 对手机回读文件的绑定验证不等于手机播放器实测。现有 PointListenProbe 仅支持模拟器。保持 draft，不能宣称真机可听，不合并。
