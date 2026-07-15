#!/usr/bin/env python3
"""Backend-neutral types, caption alignment, and diarization quality gates."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from podcast_captions import CaptionPiece


class DiarizationQualityError(RuntimeError):
    """Raised when diarization output is unsafe to use automatically."""


@dataclass(frozen=True)
class SpeakerBounds:
    exact: int | None = None
    minimum: int = 1
    maximum: int = 6

    def __post_init__(self) -> None:
        if self.exact is not None and not 1 <= self.exact <= 6:
            raise ValueError("speaker-count 必须在 1 到 6 之间")
        if not 1 <= self.minimum <= self.maximum <= 6:
            raise ValueError("speaker 范围必须满足 1 <= min <= max <= 6")


@dataclass(frozen=True)
class DiarizationTurn:
    start_ms: int
    end_ms: int
    speaker_id: str


@dataclass(frozen=True)
class SpeakerAssignment:
    source_index: int
    speaker_id: str | None
    overlap_ms: int
    confidence: float
    ambiguous: bool
    method: str


@dataclass(frozen=True)
class DiarizationQuality:
    detected_speakers: int
    turn_count: int
    unassigned_count: int
    ambiguous_count: int


class DiarizationBackend(Protocol):
    name: str
    model_fingerprint: dict[str, object]

    def diarize(
        self, audio_path: Path, speaker_bounds: SpeakerBounds
    ) -> list[DiarizationTurn]:
        """Return ordered anonymous speaker turns for one audio file."""
        raise NotImplementedError


def _intersection_ms(piece: CaptionPiece, turn: DiarizationTurn) -> int:
    return max(0, min(piece.end_ms, turn.end_ms) - max(piece.start_ms, turn.start_ms))


def _interval_gap_ms(piece: CaptionPiece, turn: DiarizationTurn) -> int:
    if turn.end_ms <= piece.start_ms:
        return piece.start_ms - turn.end_ms
    if piece.end_ms <= turn.start_ms:
        return turn.start_ms - piece.end_ms
    return 0


def _direct_assignment(
    piece: CaptionPiece, turns: list[DiarizationTurn]
) -> SpeakerAssignment:
    overlap_by_speaker: dict[str, int] = {}
    for turn in turns:
        overlap_ms = _intersection_ms(piece, turn)
        if overlap_ms:
            overlap_by_speaker[turn.speaker_id] = (
                overlap_by_speaker.get(turn.speaker_id, 0) + overlap_ms
            )

    if overlap_by_speaker:
        candidates = sorted(
            overlap_by_speaker.items(), key=lambda item: (-item[1], item[0])
        )
        speaker_id, winner_overlap = candidates[0]
        total_overlap = sum(overlap_by_speaker.values())
        confidence = winner_overlap / total_overlap
        close_top_two = (
            len(candidates) > 1
            and winner_overlap - candidates[1][1] <= 0.15 * total_overlap
        )
        excessive_overlap = total_overlap > 1.15 * (piece.end_ms - piece.start_ms)
        return SpeakerAssignment(
            source_index=piece.source_index,
            speaker_id=speaker_id,
            overlap_ms=winner_overlap,
            confidence=confidence,
            ambiguous=close_top_two or confidence < 0.65 or excessive_overlap,
            method="overlap",
        )

    nearest = sorted(
        (
            (_interval_gap_ms(piece, turn), turn.speaker_id, turn.start_ms, turn.end_ms)
            for turn in turns
        ),
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )
    if nearest and nearest[0][0] <= 1500:
        return SpeakerAssignment(
            source_index=piece.source_index,
            speaker_id=nearest[0][1],
            overlap_ms=0,
            confidence=0.0,
            ambiguous=True,
            method="nearest",
        )
    return SpeakerAssignment(
        source_index=piece.source_index,
        speaker_id=None,
        overlap_ms=0,
        confidence=0.0,
        ambiguous=True,
        method="unassigned",
    )


def align_caption_pieces(
    pieces: list[CaptionPiece], turns: list[DiarizationTurn]
) -> list[SpeakerAssignment]:
    """Assign each atomic caption cue to a speaker deterministically."""
    assignments = [_direct_assignment(piece, turns) for piece in pieces]

    for index in range(1, len(assignments) - 1):
        piece = pieces[index]
        current = assignments[index]
        if current.speaker_id is not None or piece.end_ms - piece.start_ms > 800:
            continue
        previous = assignments[index - 1]
        following = assignments[index + 1]
        if (
            previous.method == "overlap"
            and following.method == "overlap"
            and not previous.ambiguous
            and not following.ambiguous
            and previous.speaker_id is not None
            and previous.speaker_id == following.speaker_id
        ):
            assignments[index] = replace(
                current,
                speaker_id=previous.speaker_id,
                ambiguous=True,
                method="neighbor",
            )

    return assignments


def validate_diarization(
    turns: list[DiarizationTurn],
    *,
    duration_ms: int,
    bounds: SpeakerBounds,
    assignments: list[SpeakerAssignment] | None = None,
) -> DiarizationQuality:
    """Reject structurally unsafe diarization before translation or TTS."""
    if duration_ms <= 0:
        raise DiarizationQualityError("音频时长无效")
    if not turns:
        raise DiarizationQualityError("没有检测到语音片段")
    if any(
        turn.start_ms < 0
        or turn.end_ms <= turn.start_ms
        or turn.end_ms > duration_ms + 250
        for turn in turns
    ):
        raise DiarizationQualityError("说话人时间段超出音频范围")

    speakers = {turn.speaker_id for turn in turns}
    minimum = bounds.exact if bounds.exact is not None else bounds.minimum
    maximum = bounds.exact if bounds.exact is not None else bounds.maximum
    if not minimum <= len(speakers) <= maximum:
        raise DiarizationQualityError("检测到的说话人数超出配置范围")

    max_turns = max(100, math.ceil(duration_ms / 60_000) * 40)
    if len(turns) > max_turns:
        raise DiarizationQualityError("说话人切换过于频繁")

    assignment_list = assignments or []
    unassigned = sum(item.speaker_id is None for item in assignment_list)
    if assignment_list and unassigned / len(assignment_list) > 0.20:
        raise DiarizationQualityError("超过 20% 的字幕无法对齐到语音")

    return DiarizationQuality(
        detected_speakers=len(speakers),
        turn_count=len(turns),
        unassigned_count=unassigned,
        ambiguous_count=sum(item.ambiguous for item in assignment_list),
    )
