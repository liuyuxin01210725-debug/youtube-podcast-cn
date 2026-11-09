# 点段跳听 · 本批设计与边界

基线 `7d8403d`；主路径 `jump_continue`。ops D1–D5 原义不变；本功能拒跳用例名为 PL-D1–PL-D5。

详情只显示一句观点/可检验问题导航。`point-listen.json` 是单集可撤销索引，显式绑定 `work_id`、`packet_id`、16 hex `transcript_hash`、最终 `output.mp3` 的完整 `audio_sha256` 和 `timeline=output.mp3.seconds`。`output_version` 可作附带元数据，单独提供它不能替代尚未核实的权威音频绑定。运行时校验实际本地音频 SHA-256 与解码时长；不新增 span 强制复验，`span` 继续为 `String?` 子串。无索引的旧包仅复用 claim/question 文案供阅读，不推测可跳锚点，不取 `anchors[0]`。

索引数组顺序是作者确认的语义先后；不按固定分钟切段。`index_include=false` 只隐藏导航，不裁音频。无起点、非法数、反向/超时长窗口逐段拒跳；绑定失配和 stale 拒绝整份索引跳转，包枚举不增加 degraded。

## 末 15 秒：先锁定边界，再接播放器

现有 `LibraryStore.reducing` 的完成规则仍为 `duration > 1 && seconds >= duration - 15`。本批不更改阈值、不造第二个完成阈值：

| 行为（总长 600 秒） | 预期 |
| --- | --- |
| 点击跳到 584.9 / 585 / 590 秒，seek 刚落地、没有实际播放 | 不写 ListenMark，不标 finished |
| 落地后立即退出/暂停，播放头未前进 | 不写该次跳转位点；保留此前 mark |
| 在 584.9 秒实际播放并 checkpoint | 按原规则未完成 |
| 在 585 / 590 秒实际播放并 checkpoint | 按原规则完成；Continue 不显示该已完成集 |
| 跳到普通段后播放 3–5 秒退出 | 保存真实播放头，Continue 卡为该 `episode.id` |

实现只隔离 seek 落地事件与已发生的播放进度。seek 中忽略旧 tick；回调必须属于当前 item/代次且真正成功，使用 AVPlayer 实际落点；落地后首个正在播放且向前移动的 tick 才允许 checkpoint。`t_end` 不参与停播。快速连点、跨集和失败回调不能覆盖新位置。clip 独立窗停，暂停/窗停/退出均不写主 mark；下一次普通播放或 jump_continue 才恢复正常写入。

开发验证只用隔离 fixture。Norton `42786039aaf346a1bf851613199e260a` 的真实音频绑定仍未知，无合格索引就拒跳；历史 `73fd…` 为提炼包指纹，绝不写作 audio hash。

真机四连与四层交付需要另有实测记录；本机门禁、模拟器验证不能替代真机听感。单集发布必须基线∪增量，本批不推库、不重抽、不自动入队。
