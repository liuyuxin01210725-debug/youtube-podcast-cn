# 声间 · 已知错回归表（活文档）

| 日期 | 症状 | 根因一句 | 门禁 | 验收检查点 |
|------|------|----------|------|------------|
| 2026-11-03 | 要点变「收藏只是入口」四条占位 | deep-mock 当 ready 推真机 | D1 | 提炼非 mock；真机打开要点≠占位文案 |
| 2026-11-03 | Continue 消失；库骤减风险 | 误装缺 Continue 的 tip / 错包 | D2 | CURRENT tip 含 Continue 字符串/入口 |
| 2026-11-03 | 手机库被 staging 条数盖掉 | 整份 staging library.json 覆盖基线 | D3 | 推前 diff：基线条数不下降；合并∪增量 |
| 2026-10起 | 女嘉宾听感男声 | 人数闸过但无声学性别硬闸 | 发布闸+人听 | 上架前人听 30s 性别可辨 |
| 2026-11-03 | 入队后合成未过闸仍可能叙事成「可听」 | 盯合成未绑 E2E 手机可听成功标准 | E2E·C–E | job≠complete 或 OSS/库 0 hit → 禁称可听/E2E成功；同型须先补闸再重入 |
| 2026-11-04 | 听后感输入区从 tip/真机消失 | **未进 Git 的 UI 源文件会被 tip 丢掉**（ListenNote* untracked + 编包 silent aside `/tmp` 未还原） | listennote-anti-regress-gate · D5 | tip 含 ListenNote*；CURRENT 无静默遗漏或已备注；真机播放页见「听后感」+占位句；编包无 aside 残留 |

## 2026-11-04 · Continue 短播不跟最近（同型二次）
- **现象**：随便打开一集播几秒退出，资料库「继续播放」仍显示别的旧未听完，不是刚听的那集。
- **用户反馈**：改听后感/其他面后已修好的 Continue 又忘；要求 push 后自跑旅程。
- **状态**：PR#28 已合 tip@ef04e74（闸修推至 1efd099）；真机已装 continue-shortplay；`shengjian-before-claim-ready` PASS（J1 单测旅程）。真机手点短播 + P-Play/P-ListenNote **待真机补跑**。
- **D5**：同型二次 → 须补「装机前用户旅程自跑」门禁（Continue 短播探针 + 四连），禁再说下次注意。
| 2026-11-04 | 短播退出后 Continue 显示旧集非刚听 | Continue 续播指针/ListenMark 未随短播更新或被旧 checkpoint 盖住；流程缺 push 后自跑旅程 | continue-shortplay-journey · D5 | 播 A≥3–5s→退出→Continue 卡=A；四连；真机优先（Sim 标待真机补跑）；禁「装了你点点看」 |
