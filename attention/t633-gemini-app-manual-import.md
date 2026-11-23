# t633 Gemini 网页译稿文件接收

小范围核收使用 `scripts/studio_import_gemini.py`。将导出原文件交给 CLI，用户无需手抄。该入口只消费现有文件。生产仍保留 DeepSeek 回退，既有 provider、译稿、请求缓存与计费记录继续沿原路径。

接收前固定本批应译段的原 `id`、`speaker_id` 与顺序，并记录基础 `document.json` 和锁定音源的 SHA-256。将原始网页导出的 SHA-256 一同写入清单。清单为 JSON 对象，字段为 `schema_version: 1`、`document_sha256`、`source_sha256`、`export_sha256` 和 `segments`（按原顺序排列的 `{id, speaker_id}`）。导出为 UTF-8 JSON 数组，每段含 `id`、`speaker_id`、`zh`。

```sh
python3 scripts/studio_import_gemini.py --job-dir <未入队隔离job> \
  --base-document <原document.json副本> --source-audio source.mp3 \
  --manifest <接收清单.json> --export <原网页导出.txt>
```

隔离 job 内须有独立的音源文件和与其绑定的 `source-identity.json`。CLI 原子创建标准 `document.json`，只更新批准段的 `chinese`，其余段及所有说话人、ASR、待核和时轴均保留。来源收据随文档保存，记 `gemini_app_manual`、界面显示 `Neon 高`、官方模型 ID 未知，以及原始输入哈希。相同输入重跑保留原字节，冲突拒绝。原稿留在独立路径，可丢弃隔离产物后重建。

接收只完成译稿入件。说话人复核、音色、音源和逐段音频完整性仍由现有入口检查。接收不确认说话人、不认定「可听」或质量 PASS、不触发合成、发布或导入手机。
