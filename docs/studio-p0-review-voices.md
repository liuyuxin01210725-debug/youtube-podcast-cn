# 待核对与声槽（私人收听）

长访谈中，`fill_unassigned_for_review` 将未对齐的字幕分到最近的说话人，并标为「待核对」。同一段有两人时先拆段。需要改说话人时先修改并保存，再按当前归属提交逐段证据。保存修改会保留待核标记，只有 `confirm-segments` 能清除。只要仍有待核段，试听和生成都会被挡住。

人数闸通过后，还须逐段核对说话轮次。声槽确认和待核对清零后，仍须检查译稿末段时间码是否超出锁定音源时长。发生越界时保留稿件并检查音源与时轴。

## 逐段证据确认 API

`POST /api/jobs/{id}/confirm-segments` 使用与其他写入接口相同的
`X-CSRF-Token`，只允许编辑已停止的任务。请求包括当前 `document.json`
**原始文件字节**的 `document_sha256`，以及逐段 `items`：

```json
{
  "document_sha256": "<当前文件 SHA-256>",
  "items": [{
    "id": "seg-000099",
    "speaker_id": "SPEAKER_02",
    "official_source_sha256": "<官方逐字稿来源 SHA-256>",
    "official_speaker": "Michael Aquino",
    "official_locator": "turn 22",
    "official_excerpt": "An excerpt from the official transcript.",
    "evidence_hash": "<绑定哈希>"
  }]
}
```

上例省略 `evidence_type`，按 `official_transcript` 处理。也可用
`evidence_type: "audio_clip_review"` 提交短音听核凭证，字段与绑定规则见
[逐段说话人确认凭证](../attention/t644-segment-review-evidence.md)。

官方逐字稿绑定哈希 v1：将 `document_sha256`、当前段的 `id`、`speaker_id`、
`start_ms`、`end_ms`、`text`，以及上述四个 `official_*` 字段组成一个扁平
JSON 对象，再计算：

```python
sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False,
                  separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
```

字段按原值编码，不裁剪、不转换类型、不做 Unicode 归一化。共享函数是
`studio_document.segment_evidence_hash(document_sha256, segment, evidence)`。
四个官方证据字段都必须为非空字符串。此接口绑定审核者提交的证据，
不自行下载或核验官方逐字稿内容。`speaker_id` 必须与当前段一致。改说话人仍用原译稿接口。

文件哈希过期时整次请求返回 400，不写入任何内容。逐项错误返回 200，
`applied` 是通过的段落 ID 列表，`rejected` 包含 `id` 和 `reason`。
缺省或空 `items` 返回两个空列表，不写入；不接受额外的批量确认参数。
只有通过的段会清除 `ambiguous`。通过项的完整绑定字段、哈希、版本和时间
追加到译稿顶层 `segment_confirmations`，与标记变更一起原子保存。
响应还返回新的 `document_sha256`；连续逐段确认必须用新哈希重新计算证据绑定。
拆段后同样必须使用拆段后的文件哈希、段落 ID、时间和原文。

## 声槽怎么改

按默认方案制作时，系统声槽从第一次出现的说话人起依次分配。非全男节目配置 `prefer_male_slots=false` 时，声槽留空，等待逐人设置。

在说话人旁选择声槽，保存后点击「确认声槽」调用 `confirm-voices`。更换说话人分配或声槽后，重新确认再合成。
