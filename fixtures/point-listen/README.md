# 点段跳听隔离样包

仅本机/独立模拟器开发用，不发布到用户库，不代表 Norton 已绑定或真机可听。

- `output.mp3` 是 90 秒 440Hz 测试音，不是节目语音。
- `extract.json` 是手工 `mode=manual` 测试输入，不通过 MockExtractor，也不冒充生产深抽。
- `point-listen.json` 明确绑定该文件的 SHA-256、包 id、16 hex 译稿指纹与 `output.mp3.seconds`；5 句观点/问题，1 条隐藏寒暄；`span` 为子串。
- 秒轴含 `3.25`、`18.5`、`37.125`，每条的 `t_end` 故意很近，用于证明主跳播越过段尾不停。
- 最后一条 76 秒用于末 15 秒完成边界，实际音频时长由解码确定。

音频可用 FFmpeg 生成：`ffmpeg -f lavfi -i sine=frequency=440:sample_rate=16000:duration=90 -c:a libmp3lame -b:a 16k output.mp3`。重新生成可能改变字节，必须同步更新索引 hash，不可复制旧绑定。

`make test-point-listen` 验证绑定与 PL-D*。模拟器运行时探针显式传 `-PointListenProbe`，只在 Debug simulator 编译，报告位于 App Documents/point-listen-probe.json。普通启动不写 fixture、不入队、不启动探针。

复现：先用 Xcode 编译 Debug simulator App，创建并启动名称以 `Shengjian Point Listen Isolated` 开头的一次性模拟器，再运行：

```sh
bash scripts/probe_point_listen_sim.sh SIMULATOR_UDID /path/to/PrivateListen.app /tmp/point-listen-report.json
```

脚本校验隔离名称和旧库内容，安装 App 后重新取容器位置，再复制该 fixture；只保留本集，不触碰真机/iCloud。`-PointListenPreview` 可显示同一个生产列表组件的隔离预览（不是完整播放页 UI 四连）。
