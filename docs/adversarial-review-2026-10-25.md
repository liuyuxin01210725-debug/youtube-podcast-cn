# 对抗性审查草稿 · 英→中播客工作台 vs CLI

## 用户立场
- Codex 几版已满足基础需求、更流畅更快；不要推倒重做
- 在可用底子上改不满意处；改不好再改

## 底子是什么
- CLI：Desktop/音频/youtube-podcast-cn（podcast-cn.sh）— 桌面已有大量成片
- 工作台：Codex 2026-10-18 在同一仓库加了 studio_server/engine（本机 Documents/.../podcast-studio worktree）
- 共享核心：podcast_cn / diarization_* / tts_*；差异主要在 studio_* 与 diarization_runtime 近期补丁

## 不合格点（对抗视角）
1. **速度**：YouTube 有字幕仍常掉进 60 秒串行 ASR（Huberman 约 31 段 ASR）；声纹默认 15 分钟块串行，31 分钟≈6–7 分钟。CLI/早期成片路径更直接。
2. **脆**：声纹拼接在长访谈默认阈值下曾直接失败；抬阈值又塌说话人。说明平台层把「能跑通」做成了「阈值玄学」，而不是稳健默认。
3. **双入口漂移**：studio_server 与 studio_engine 各有一份 preview 挑选逻辑，曾导致试听仍是「谢谢」。平台层自己不一致。
4. **验收自欺**：交付验证写 208 测通过 + 合成 WAV，但真实痛点（长访谈声纹、字幕优先、Edge 403）后补；用户感知「慢且易挂」。
5. **重做倾向**：P0 系统音色合入是对的，但 Huberman 自跑暴露核心 diarization 仍不合格——应在 CLI 已验证的 stitch（09150fb evidence-based）上修，而不是继续堆阈值/人数旋钮。

## 应保留
- Edge 默认系统声槽、job 模型、桌面启动器、译文编辑/试听/导出、音频缓存复用
- CLI podcast_cn 流水线与已有成片能力

## 最小改造方向（不重写）
A. YouTube：字幕优先且失败原因落盘；禁止无声掉进全量 ASR 而不告警
B. 声纹：保留今日 soft closed-world + collapse；补回归夹具用真实长访谈切片；默认阈值策略写死可解释
C. 去掉 server/engine 重复逻辑；preview/revision 单一来源
D. 进度与 ETA：ASR/声纹/翻译分阶段可见剩余时间
E. 对照 CLI：能复用的调用 podcast_cn 路径，工作台只做编排与 UI，少分叉

## 建议下一刀
先做 A+C（快、体感大），再加固 B 的测试夹具；D 可并行。
