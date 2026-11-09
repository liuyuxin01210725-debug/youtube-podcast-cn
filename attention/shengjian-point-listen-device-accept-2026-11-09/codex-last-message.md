已完成 Norton 绑定、生产 hash 契约修复、覆盖装机和可自动化验证；**未做真机手点四连，不能宣称手机可听**。

- 模型：已从当前会话元数据核实 `gpt-6.1-sol`。
- 分支：`feature/shengjian-point-listen-jump-continue`；实现/装机源提交 `f4b3c47`（基于 `f570351`），验收资料由后续提交追加。
- PR：[草稿 #31](https://github.com/liuyuxin01210725-debug/youtube-podcast-cn/pull/31)，目标 `feature/shengjian-point-clip-tend`，不合并；最终状态见 `pr-after.json`。
- 契约：packet 严格/宽松解码、index 解码和跳转比较统一小写 16 hex；64 取前 16，原有 16 保持；错 16、非法字符/长度继续拒跳。回归和 PL-D1–PL-D5、fixture、本机门禁通过。
- Norton 索引：7 条 claim；`audio_sha256=fcb337634866181e6107eb6cb519006fe9102c756b35f83bb8d1410a04749f25`，ffprobe 3809.112 秒；只用现有时长内锚点，未猜时间、未重抽、未入队、未全库发布。
- 装机包：`dist/PrivateListen-point-listen-norton-2026-11-09-f4b3c47-iphoneos.app`；UDID `00008160-001639DA3480000A`。覆盖安装、App 在架、启动及进程探针通过，CURRENT 两指针已更新。
- 设备落盘：Norton 8 文件回读 SHA/长度均一致；设备音频 SHA 与索引一致；回读文件生产 Swift 绑定探针 7/7。资料库 52→52、其余 51 集目录记录未变、library.json 字节完全相同。
- 自动验证：最终本机门禁通过；全段音频解码通过。额外 `make test` 被既有个人路径检查 31 处失败挡在 test-public，未称全套绿。
- **明确未做**：真机详情 ≥2 段跳播/语义落点、短播退出→Continue、P-Merge 手点旅程、P-ListenNote 目视，即真机手点四连未完成。安装/启动和 Mac 回读绑定探针不能补足四层 (d)。现有播放器探针只支持模拟器，无现成真机 UI 自动化；保持草稿。

完整回执：[README.md](README.md)。
