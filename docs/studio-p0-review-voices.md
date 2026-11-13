# 待核对与声槽（私人收听）

长访谈里，`fill_unassigned_for_review` 会把对不上的字幕贴到最近的说话人，并标成「待核对」。以前界面和引擎都拒绝试听、生成，一百多段就得点一百多次。现在三条路：

1. **全部按当前说话人确认。** 一个按钮把所有待核对标成已确认，说话人保持现在的分配。之后试听和生成走普通路径。
2. **带风险继续。** 试听或生成时若仍有待核对，确认框会写明「可能串人」。继续后引擎带 `--accept-unreviewed` 合成，段落上的待核对标记留着，页面也会注明这次是带风险继续。
3. **只改某一段的说话人。** 保存时只签收这一段。只改中文译文不再把所有待核对一起签收。

不点确认、也不带 `--accept-unreviewed` 时，引擎仍然拒绝待核对段。

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

绑定哈希 v1：将 `document_sha256`、当前段的 `id`、`speaker_id`、
`start_ms`、`end_ms`、`text`，以及上述四个 `official_*` 字段组成一个扁平
JSON 对象，再计算：

```python
sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False,
                  separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
```

字段按原值编码，不裁剪、不转换类型、不做 Unicode 归一化。共享函数是
`studio_document.segment_evidence_hash(document_sha256, segment, evidence)`。
四个官方证据字段都必须为非空字符串。此接口绑定审核者提交的证据，
不自行下载或核验官方逐字稿内容。`speaker_id` 必须与当前段一致；改说话人仍用原译稿接口。

文件哈希过期时整次请求返回 400，不写入任何内容。逐项错误返回 200，
`applied` 是通过的段落 ID 列表，`rejected` 包含 `id` 和 `reason`。
缺省或空 `items` 返回两个空列表，不写入；不接受额外的批量确认参数。
只有通过的段会清除 `ambiguous`。通过项的完整绑定字段、哈希、版本和时间
追加到译稿顶层 `segment_confirmations`，与标记变更一起原子保存。
响应还返回新的 `document_sha256`；连续逐段确认必须用新哈希重新计算证据绑定。
拆段后同样必须使用拆段后的文件哈希、段落 ID、时间和原文。

## 声槽怎么改

新建译稿按**第一次出现的顺序**分配：男 1、男 2、男 3、女 1、女 2。第 6 人与男 1 同槽。说话人上限仍是 6，不增加到 8。这是系统声槽，不是名人克隆。CLI 的 `speaker-map.json` 仍从女声起排，不会被改写。

已经做好的任务（例如先出现的男嘉宾被分到女声）可以：

- 在每位说话人旁边的下拉框里改声槽，然后保存；
- 或点 **男声起排**，按上面的顺序重排。

试听或生成前，若还没确认过音色，会弹出当前「说话人 → 声槽」列表。取消后可以改下拉框再来。改过声槽或点过男声起排之后，会再问一次。
