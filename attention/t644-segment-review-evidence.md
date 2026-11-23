# 逐段说话人确认凭证

`confirm_segments`（`POST /api/jobs/{id}/confirm-segments`）是清除已有 `ambiguous` 标记的唯一出口。`update_document` 可以修改说话人归属，保留每段原有的待核状态，并在改归属时将 `voices_confirmed` 置为 `False`。需要改标时先保存，再用更新后的 `speaker_id` 和 `document_sha256` 提交证据。证据确认只处理逐段待核状态。声槽确认走 `confirm-voices`，凭证不会自动设定 `voices_confirmed=True`。短音听核记录的是模型辅助听核，不能用作人耳确认或质量 PASS。

## 凭证模式与字段

每项都提交段落 `id`、当前 `speaker_id` 和绑定的 `evidence_hash`。请求的 `document_sha256` 是当前 `document.json` 原始文件字节的 SHA-256。

- `evidence_type` 省略时按 `official_transcript` 处理。显式值只接受 `official_transcript` 或 `audio_clip_review`。历史记录省略此字段时同样按 `official_transcript` 解读，保留原记录。新确认记录显式保存模式及绑定哈希。
- `official_transcript` 沿用 `official_source_sha256`、`official_speaker`、`official_locator`、`official_excerpt` 四个非空字符串及原 `segment_evidence_hash`。官方哈希前像逐字节沿用原规则，排除 `evidence_type`。
- 两种模式的正文凭证键互斥。混填任一对方键，即使值为空字符串或 `null`，以及添加未知键，都会拒绝该项。

| `audio_clip_review` 必填键 | 校验规则 |
| --- | --- |
| `evidence_type` | 固定为 `audio_clip_review` |
| `source_audio_sha256` | 64 位小写十六进制，按下述来源锁规则核对 |
| `clip_sha256` | 64 位小写十六进制 |
| `clip_target_start_ms` | 非布尔整数，且大于或等于 0 |
| `clip_target_end_ms` | 非布尔整数，且大于起点 |
| `review_response_sha256` | 64 位小写十六进制 |
| `review_source` | 去掉首尾空白后非空，提交值至多 128 字符 |
| `review_basis` | 固定为 `model_assisted_audio_review`，随确认记录保存 |
| `review_summary` | 去掉首尾空白后非空，提交值至多 500 字符 |

## 来源锁与绑定哈希

对本地来源，`source-identity.json` 的 `input_sha256` 锁定输入原始音频文件的字节。只要存在这个字段，`source_audio_sha256` 必须与它严格一致，错配时拒绝该项。引擎把来源转成规范化 WAV 后，才记录该 WAV 的 `sha256`、`bytes` 和 `duration_ms`。仅有 WAV 锁时，接口对原始音频哈希执行格式校验。

`studio_document.audio_review_evidence_hash(document_sha256, segment, evidence)` 计算短音听核绑定。前像是扁平 JSON，包含域标记 `"evidence_schema": "audio_clip_review:v1"`、`document_sha256`、当前段的 `id`、`speaker_id`、`start_ms`、`end_ms`、`text`，以及表中的全部凭证字段。按原值以 `sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False` 序列化，编码为 UTF-8 后计算 SHA-256。接口核对绑定及声明字段，不自行解析模型回复。
