#!/usr/bin/env python3
"""Privacy-safe diarization caches and editable synthesis voice maps."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from diarization_core import DiarizationTurn, SpeakerAssignment, SpeakerBounds
from podcast_captions import CaptionPiece, SubtitleSegment


SCHEMA_VERSION = 1
ALIGNMENT_VERSION = 1
VOICE_ORDER = ("female_1", "male_1", "female_2", "male_2", "male_3")
# Studio listen jobs. Interviews usually open with a male host or guest, so the
# first-seen speaker must not land on female_1. CLI speaker maps stay on
# VOICE_ORDER above; do not retarget those historical defaults.
STUDIO_VOICE_SLOTS = ("male_1", "male_2", "male_3", "female_1", "female_2")
CHINESE_ORDINALS = ("甲", "乙", "丙", "丁", "戊", "己")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VOICE_SUFFIX = {
    "female_1": "女声1",
    "female_2": "女声2",
    "male_1": "男声1",
    "male_2": "男声2",
    "male_3": "男声3",
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _atomic_json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _bounds_payload(bounds: SpeakerBounds) -> dict[str, int | None]:
    return {
        "exact": bounds.exact,
        "minimum": bounds.minimum,
        "maximum": bounds.maximum,
    }


def caption_pieces_sha256(pieces: list[CaptionPiece]) -> str:
    payload = [
        {
            "start_ms": piece.start_ms,
            "end_ms": piece.end_ms,
            "text": piece.text,
            "source_index": piece.source_index,
        }
        for piece in pieces
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class DiarizationCache:
    """Own the two derived cache files stored inside one podcast job."""

    def __init__(self, job_dir: Path) -> None:
        self.job_dir = job_dir
        self.turns_path = job_dir / "diarization.json"
        self.assignments_path = job_dir / "speaker-assignments.json"

    def _invalidate_turns(self) -> None:
        _safe_unlink(self.turns_path)
        _safe_unlink(self.assignments_path)

    def store_turns(
        self,
        source_identity: dict[str, object],
        bounds: SpeakerBounds,
        backend_fingerprint: dict[str, object],
        turns: list[DiarizationTurn],
        source_audio_sha256: str,
        warnings: list[str] | None = None,
    ) -> None:
        identity = {
            "video_id": source_identity.get("video_id"),
            "duration_ms": source_identity.get("duration_ms"),
        }
        if not isinstance(identity["video_id"], str) or not identity["video_id"]:
            raise ValueError("source_identity.video_id 无效")
        if type(identity["duration_ms"]) is not int or identity["duration_ms"] <= 0:
            raise ValueError("source_identity.duration_ms 无效")
        if not _SHA256.fullmatch(source_audio_sha256):
            raise ValueError("source_audio_sha256 必须是 64 位小写十六进制")
        warning_list = warnings or []
        if not all(isinstance(item, str) for item in warning_list):
            raise ValueError("warnings 必须是字符串列表")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "alignment_version": ALIGNMENT_VERSION,
            "source_identity": identity,
            "backend_fingerprint": backend_fingerprint,
            "speaker_bounds": _bounds_payload(bounds),
            "source_audio_sha256": source_audio_sha256,
            "turns": [asdict(turn) for turn in turns],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": warning_list,
        }
        _safe_unlink(self.assignments_path)
        _atomic_json_write(self.turns_path, payload)

    def load_turns(
        self,
        source_identity: dict[str, object],
        bounds: SpeakerBounds,
        backend_fingerprint: dict[str, object],
    ) -> list[DiarizationTurn] | None:
        if not self.turns_path.exists():
            return None
        expected_identity = {
            "video_id": source_identity.get("video_id"),
            "duration_ms": source_identity.get("duration_ms"),
        }
        try:
            payload = json.loads(self.turns_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError
            if payload.get("alignment_version") != ALIGNMENT_VERSION:
                raise ValueError
            if payload.get("source_identity") != expected_identity:
                raise ValueError
            if payload.get("speaker_bounds") != _bounds_payload(bounds):
                raise ValueError
            if payload.get("backend_fingerprint") != backend_fingerprint:
                raise ValueError
            if not _SHA256.fullmatch(payload.get("source_audio_sha256", "")):
                raise ValueError
            if not isinstance(payload.get("created_at"), str):
                raise ValueError
            warnings = payload.get("warnings")
            if not isinstance(warnings, list) or not all(
                isinstance(item, str) for item in warnings
            ):
                raise ValueError
            raw_turns = payload.get("turns")
            if not isinstance(raw_turns, list):
                raise ValueError
            turns: list[DiarizationTurn] = []
            for item in raw_turns:
                if not isinstance(item, dict) or set(item) != {
                    "start_ms",
                    "end_ms",
                    "speaker_id",
                }:
                    raise ValueError
                if (
                    type(item["start_ms"]) is not int
                    or type(item["end_ms"]) is not int
                    or not isinstance(item["speaker_id"], str)
                    or not item["speaker_id"]
                ):
                    raise ValueError
                turns.append(DiarizationTurn(**item))
            return turns
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            self._invalidate_turns()
            return None

    def store_assignments(
        self,
        pieces: list[CaptionPiece],
        assignments: list[SpeakerAssignment],
    ) -> None:
        if len(pieces) != len(assignments):
            raise ValueError("字幕 cue 与 speaker 分配数量不一致")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "alignment_version": ALIGNMENT_VERSION,
            "caption_pieces_sha256": caption_pieces_sha256(pieces),
            "assignments": [asdict(item) for item in assignments],
        }
        _atomic_json_write(self.assignments_path, payload)

    def load_assignments(
        self, pieces: list[CaptionPiece]
    ) -> list[SpeakerAssignment] | None:
        if not self.assignments_path.exists():
            return None
        try:
            payload = json.loads(
                self.assignments_path.read_text(encoding="utf-8")
            )
            if not isinstance(payload, dict):
                raise ValueError
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError
            if payload.get("alignment_version") != ALIGNMENT_VERSION:
                raise ValueError
            if payload.get("caption_pieces_sha256") != caption_pieces_sha256(pieces):
                raise ValueError
            raw_assignments = payload.get("assignments")
            if not isinstance(raw_assignments, list) or len(raw_assignments) != len(
                pieces
            ):
                raise ValueError
            assignments: list[SpeakerAssignment] = []
            expected_fields = {
                "source_index",
                "speaker_id",
                "overlap_ms",
                "confidence",
                "ambiguous",
                "method",
            }
            for item in raw_assignments:
                if not isinstance(item, dict) or set(item) != expected_fields:
                    raise ValueError
                if (
                    type(item["source_index"]) is not int
                    or (
                        item["speaker_id"] is not None
                        and not isinstance(item["speaker_id"], str)
                    )
                    or type(item["overlap_ms"]) is not int
                    or type(item["confidence"]) not in (int, float)
                    or type(item["ambiguous"]) is not bool
                    or item["method"]
                    not in {"overlap", "nearest", "neighbor", "unassigned"}
                ):
                    raise ValueError
                assignments.append(SpeakerAssignment(**item))
            if [item.source_index for item in assignments] != [
                piece.source_index for piece in pieces
            ]:
                raise ValueError
            return assignments
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            _safe_unlink(self.assignments_path)
            return None


def assign_studio_voices(speaker_ids) -> dict[str, str]:
    """Map first-seen speakers to male-first system slots.

    A sixth speaker reuses ``male_1``. The distinguishable pool stays at five
    slots; this does not raise the diarization speaker cap.
    """
    assigned: dict[str, str] = {}
    for speaker_id in speaker_ids:
        if not isinstance(speaker_id, str) or not speaker_id or speaker_id in assigned:
            continue
        assigned[speaker_id] = STUDIO_VOICE_SLOTS[len(assigned) % len(STUDIO_VOICE_SLOTS)]
    return assigned


def default_speaker_record(index: int) -> dict[str, str]:
    if not 0 <= index < len(CHINESE_ORDINALS):
        raise ValueError("自动 speaker 映射最多支持 6 人")
    return {
        "role": f"说话人{CHINESE_ORDINALS[index]}",
        "display_name": "",
        "voice_slot": VOICE_ORDER[index % len(VOICE_ORDER)],
        "mapping_confidence": "unknown",
    }


def _validate_speaker_map(mapping: object) -> dict[str, dict[str, str]]:
    if not isinstance(mapping, dict):
        raise ValueError("speaker-map.json 必须是 JSON 对象")
    validated: dict[str, dict[str, str]] = {}
    required_fields = {"role", "display_name", "voice_slot", "mapping_confidence"}
    for speaker_id, record in mapping.items():
        if not isinstance(speaker_id, str) or not speaker_id:
            raise ValueError("speaker ID 无效")
        if not isinstance(record, dict) or not required_fields <= set(record):
            raise ValueError(f"{speaker_id} 的映射字段不完整")
        if not all(isinstance(record[field], str) for field in required_fields):
            raise ValueError(f"{speaker_id} 的映射值必须是字符串")
        if not record["role"].strip():
            raise ValueError(f"{speaker_id} 的 role 不能为空")
        if record["voice_slot"] not in VOICE_ORDER:
            raise ValueError(f"{speaker_id} 的 voice_slot 无效")
        validated[speaker_id] = dict(record)
    return validated


def write_speaker_map(path: Path, mapping: dict[str, dict[str, str]]) -> None:
    """Validate and atomically publish a user-editable speaker map."""
    _atomic_json_write(path, _validate_speaker_map(mapping))


def load_or_create_speaker_map(
    path: Path, speaker_ids: list[str]
) -> dict[str, dict[str, str]]:
    """Preserve all user records and append only newly observed speakers."""
    if path.exists():
        try:
            mapping = _validate_speaker_map(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"无法读取 speaker-map.json: {error}") from error
    else:
        mapping = {}

    changed = not path.exists()
    for speaker_id in dict.fromkeys(speaker_ids):
        if not isinstance(speaker_id, str) or not speaker_id:
            raise ValueError("speaker ID 无效")
        if speaker_id not in mapping:
            mapping[speaker_id] = default_speaker_record(len(mapping))
            changed = True
    if changed:
        write_speaker_map(path, mapping)
    return mapping


def label_for_record(record: dict[str, str]) -> str:
    base = record["display_name"].strip() or record["role"].strip()
    return f"{base}-{_VOICE_SUFFIX[record['voice_slot']]}"


def render_speaker_sequence(
    segments: list[SubtitleSegment], mapping: dict[str, dict[str, str]]
) -> list[str]:
    """Render exactly one numbered synthesis label for every segment."""
    validated = _validate_speaker_map(mapping)
    labels: list[str] = []
    for segment in segments:
        if segment.speaker_id is None:
            raise ValueError(f"{segment.id} 缺少 speaker_id")
        try:
            labels.append(label_for_record(validated[segment.speaker_id]))
        except KeyError:
            raise ValueError(f"speaker-map.json 缺少 {segment.speaker_id}") from None
    if len(labels) != len(segments):
        raise ValueError("speaker 序列与段落数不一致")
    return labels
