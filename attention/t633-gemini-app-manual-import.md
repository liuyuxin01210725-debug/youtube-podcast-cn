# t633 Gemini 网页译稿文件接收

小范围核收使用 `scripts/studio_import_gemini.py`。将导出原文件交给 CLI，用户无需手抄。该入口只消费现有文件。生产仍保留 DeepSeek 回退，既有 provider、译稿、请求缓存与计费记录继续沿原路径。

接收前固定本批应译段的原 `id`、`speaker_id` 与顺序，并记录基础 `document.json` 和锁定音源的 SHA-256。将原始网页导出的 SHA-256 一同写入清单。清单为 JSON 对象，字段为 `schema_version: 1`、`document_sha256`、`source_sha256`、`export_sha256` 和 `segments`（按原顺序排列的 `{id, speaker_id}`）。导出为 UTF-8 JSON 数组，每段含 `id`、`speaker_id`、`zh`。

清单可加 `"display_model": "Hydrogen 高"`，填写网页完成本批回答后实际显示的模型。自动回退时必须填回答所显示的模型，禁止沿用提交前选择的模型。原五键清单继续兼容，省略此字段时沿用 `Neon 高`。显式标签须为非空、非纯空白的字符串。

```sh
python3 scripts/studio_import_gemini.py --job-dir <未入队隔离job> \
  --base-document <原document.json副本> --source-audio source.mp3 \
  --manifest <接收清单.json> --export <原网页导出.txt>
```

隔离 job 内须有独立的音源文件和与其绑定的 `source-identity.json`。CLI 原子创建标准 `document.json`，只更新批准段的 `chinese`，其余段及所有说话人、ASR、待核和时轴均保留。来源收据随文档保存，记录 `gemini_app_manual`、本批 `display_model` 和原始输入哈希。官方 `official_model_id` 未知时保留 `null`，不得猜测。

后续用 `--append` 接收，在本批清单中用当前 `document.json` 的 SHA-256 绑定本批的原始网页导出和实际显示模型。已有 `Neon 高` 收据原样保留，同一稿的接收或修订记录可以有不同的显示模型。`--install-pending-draft` 按指定的待审稿 SHA-256 校验并原样安装混合批次稿。相同输入重跑保留原字节，冲突拒绝。原稿留在独立路径，可丢弃隔离产物后重建。

接收只完成译稿入件。说话人复核、音色、音源和逐段音频完整性仍由现有入口检查。接收不确认说话人、不认定「可听」或质量 PASS、不触发合成、发布或导入手机。
