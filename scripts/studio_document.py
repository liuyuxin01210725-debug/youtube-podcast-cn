"""Official document mutations that keep segment contracts intact."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

SEGMENT_ID_RE = re.compile(r"^seg-(\d+)$")
MAX_TEXT = 20_000
SEGMENT_CONFIRM_FIELDS = ("id", "speaker_id", "start_ms", "end_ms", "text")
OFFICIAL_EVIDENCE_FIELDS = (
    "official_source_sha256", "official_speaker", "official_locator", "official_excerpt",
)
AUDIO_REVIEW_EVIDENCE_FIELDS = (
    "evidence_type", "source_audio_sha256", "clip_sha256", "clip_target_start_ms",
    "clip_target_end_ms", "review_response_sha256", "review_source", "review_basis", "review_summary",
)


def segment_evidence_hash(document_sha256: str, segment: dict, evidence: dict) -> str:
    """SHA-256 of the v1 confirmation binding, serialized as UTF-8 JSON.

    The flat object contains document_sha256, SEGMENT_CONFIRM_FIELDS from the
    current segment, and OFFICIAL_EVIDENCE_FIELDS from the evidence. Serialize
    with sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False.
    Values are used verbatim, without trimming, coercion or Unicode normalization.
    """
    binding = {"document_sha256": document_sha256}
    binding.update({field: segment[field] for field in SEGMENT_CONFIRM_FIELDS})
    binding.update({field: evidence[field] for field in OFFICIAL_EVIDENCE_FIELDS})
    canonical = json.dumps(binding, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audio_review_evidence_hash(document_sha256: str, segment: dict, evidence: dict) -> str:
    """Hash the v1 audio review binding using the submitted field values."""
    binding = {"evidence_schema": "audio_clip_review:v1", "document_sha256": document_sha256}
    binding.update({field: segment[field] for field in SEGMENT_CONFIRM_FIELDS})
    binding.update({field: evidence[field] for field in AUDIO_REVIEW_EVIDENCE_FIELDS})
    canonical = json.dumps(binding, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audio_review_evidence_error(evidence: dict) -> str | None:
    for field in ("source_audio_sha256", "clip_sha256", "review_response_sha256"):
        value = evidence.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            return f"invalid_{field}"
    start, end = evidence.get("clip_target_start_ms"), evidence.get("clip_target_end_ms")
    if type(start) is not int or start < 0:
        return "invalid_clip_target_start_ms"
    if type(end) is not int or end <= start:
        return "invalid_clip_target_end_ms"
    for field, limit in (("review_source", 128), ("review_summary", 500)):
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            return f"invalid_{field}"
    if evidence.get("review_basis") != "model_assisted_audio_review":
        return "invalid_review_basis"
    return None


def normalize_ws(value: str) -> str:
    return " ".join((value or "").split())


def texts_join_original(left: str, right: str, original: str) -> bool:
    """Accept a clean cut with or without a space exactly at the boundary."""
    if not isinstance(left, str) or not isinstance(right, str) or not isinstance(original, str):
        return False
    expected = normalize_ws(original)
    if not expected:
        return False
    return normalize_ws(left + " " + right) == expected or normalize_ws(left + right) == expected


def next_segment_id(segments: list[dict]) -> str:
    used = {str(segment.get("id")) for segment in segments}
    highest = 0
    for segment_id in used:
        match = SEGMENT_ID_RE.fullmatch(segment_id)
        if match:
            highest = max(highest, int(match.group(1)))
    candidate = highest + 1
    while f"seg-{candidate:06d}" in used:
        candidate += 1
    return f"seg-{candidate:06d}"


def _require_side(side: dict, label: str) -> dict:
    if not isinstance(side, dict):
        raise ValueError(f"{label}侧数据无效")
    text = side.get("text")
    chinese = side.get("chinese")
    speaker_id = side.get("speaker_id")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{label}侧英文为空")
    if not isinstance(chinese, str) or not chinese.strip():
        raise ValueError(f"{label}侧译文为空")
    if not isinstance(speaker_id, str) or not speaker_id:
        raise ValueError(f"{label}侧说话人无效")
    if len(text) > MAX_TEXT or len(chinese) > MAX_TEXT:
        raise ValueError(f"{label}侧文本过长")
    return {"text": text, "chinese": chinese, "speaker_id": speaker_id}


def split_document_segment(document: dict, payload: dict) -> tuple[dict, str, str]:
    """Split one existing segment into two contiguous pieces.

    The left piece keeps the original id. The right piece gets the next unused
    ``seg-XXXXXX`` id. Other segment ids are not renumbered. The original
    time span is preserved with a touching cut: [start, cut) + [cut, end].
    """
    if not isinstance(document, dict) or not isinstance(document.get("segments"), list):
        raise ValueError("译稿无效")
    if not isinstance(document.get("speakers"), dict) or not document["speakers"]:
        raise ValueError("说话人无效")
    if not isinstance(payload, dict):
        raise ValueError("拆段数据无效")
    segment_id = payload.get("id") or payload.get("segment_id")
    cut_ms = payload.get("cut_ms")
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("拆段目标无效")
    if type(cut_ms) is not int:
        raise ValueError("切点必须是整数毫秒")

    index = next((i for i, segment in enumerate(document["segments"]) if segment.get("id") == segment_id), None)
    if index is None:
        raise ValueError("找不到要拆的段落")
    original = document["segments"][index]
    try:
        start_ms = int(original["start_ms"])
        end_ms = int(original["end_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("段落时间轴无效") from exc
    if not start_ms < cut_ms < end_ms:
        raise ValueError("切点必须落在原段起止之间")

    left = _require_side(payload.get("left") or {}, "左")
    right = _require_side(payload.get("right") or {}, "右")
    speakers = document["speakers"]
    if left["speaker_id"] not in speakers or right["speaker_id"] not in speakers:
        raise ValueError("拆段不能新增说话人")
    if not texts_join_original(left["text"], right["text"], str(original.get("text") or "")):
        raise ValueError("左右英文必须拼回原段原文")
    if not texts_join_original(left["chinese"], right["chinese"], str(original.get("chinese") or "")):
        raise ValueError("左右译文必须拼回原段中文")

    new_id = next_segment_id(document["segments"])
    kept = {key: deepcopy(original[key]) for key in original if key not in {"text", "chinese", "speaker_id", "start_ms", "end_ms", "id", "ambiguous"}}
    left_segment = {
        **deepcopy(original),
        "id": segment_id,
        "start_ms": start_ms,
        "end_ms": cut_ms,
        "text": left["text"],
        "chinese": left["chinese"],
        "speaker_id": left["speaker_id"],
        "ambiguous": True,
    }
    right_segment = {
        **kept,
        "id": new_id,
        "start_ms": cut_ms,
        "end_ms": end_ms,
        "text": right["text"],
        "chinese": right["chinese"],
        "speaker_id": right["speaker_id"],
        "ambiguous": True,
    }
    updated = deepcopy(document)
    segments = list(updated["segments"])
    segments[index] = left_segment
    segments.insert(index + 1, right_segment)
    ids = [segment["id"] for segment in segments]
    if len(ids) != len(set(ids)):
        raise ValueError("段落编号重复")
    updated["segments"] = segments
    return updated, segment_id, new_id


def render_contract_errors(document: dict) -> list[str]:
    """Checks render will apply before synthesis, excluding audio backends."""
    errors: list[str] = []
    segments = document.get("segments") if isinstance(document, dict) else None
    if not segments:
        return ["段落为空或编号重复"]
    ids = [segment.get("id") for segment in segments]
    if len(set(ids)) != len(ids) or any(not ident for ident in ids):
        errors.append("段落为空或编号重复")
    previous_end = None
    for segment in segments:
        ident = segment.get("id")
        if not str(segment.get("chinese") or "").strip():
            errors.append(f"{ident} 译文为空")
        try:
            start_ms = int(segment["start_ms"])
            end_ms = int(segment["end_ms"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{ident} 时间轴无效")
            continue
        if start_ms >= end_ms:
            errors.append(f"{ident} 时间轴无效")
        if previous_end is not None and start_ms < previous_end:
            errors.append(f"{ident} 与前段时间轴重叠")
        previous_end = end_ms
    return errors


def check_source_coverage(document, identity):
    """Check every source-timeline row, including ads, against the locked audio."""
    duration = identity.get('duration_ms') if isinstance(identity, dict) else None
    if (type(duration) is not int or duration <= 0
            or type(identity.get('bytes')) is not int or identity['bytes'] <= 0
            or not isinstance(identity.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', identity['sha256'])):
        raise ValueError('缺少可核验的音源时长锁，请核对原始音频')
    rows = document.get('segments') if isinstance(document, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError('译稿缺少可核验的时间轴')
    if any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
           or not row['id'] or type(row.get('start_ms')) is not int
           or type(row.get('end_ms')) is not int
           or not 0 <= row['start_ms'] < row['end_ms'] for row in rows):
        raise ValueError('译稿时间轴无效')
    return [row['id'] for row in rows if row['end_ms'] > duration]
