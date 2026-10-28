# 英文书听试点夹具（合成短文）

- `source.md`：本地合成英文短章，**不是**从网上抓的正文。
- 用途：证明 `book.json` → `synth_queue` → `chapters/<c>/<b>.mp3` 路径形状。
- 默认 **不**跑长 Edge TTS；测试可用极短静音 mp3 占位（`execute=False` 仍成立）。
- 产品方向：英文 → 译+朗读；本夹具先验索引/队列/路径，译制流水线另 PR。
