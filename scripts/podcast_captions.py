#!/usr/bin/env python3
"""Atomic WebVTT parsing and speaker-aware subtitle grouping."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


class PodcastPipelineError(RuntimeError):
    """Base error for failures that can be shown directly to the user."""


class CaptionUnavailableError(PodcastPipelineError):
    """Raised when a video has no usable English caption track."""


@dataclass(frozen=True)
class CaptionPiece:
    start_ms: int
    end_ms: int
    text: str
    source_index: int


@dataclass(frozen=True)
class SubtitleSegment:
    id: str
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str | None = None
    ambiguous: bool = False


_TIMING_LINE = re.compile(
    r"^(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?$"
)
_VTT_TAG = re.compile(r"<[^>]+>")
_TOKEN_EDGE = re.compile(r"^[\W_]+|[\W_]+$")
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)]*$")


def _timestamp_ms(value: str) -> int:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes_text, seconds_text = parts
    elif len(parts) == 3:
        hours = int(parts[0])
        minutes_text, seconds_text = parts[1:]
    else:
        raise ValueError(f"无法解析字幕时间:{value}")
    seconds, milliseconds = seconds_text.split(".")
    return (
        hours * 3_600_000
        + int(minutes_text) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )


def _clean_cue_text(lines: list[str]) -> str:
    cleaned_lines: list[str] = []
    for line in lines:
        without_tags = _VTT_TAG.sub("", line)
        cleaned = html.unescape(without_tags).strip()
        if cleaned:
            cleaned_lines.append(cleaned)
    return " ".join(cleaned_lines)


def _comparable_token(token: str) -> str:
    return _TOKEN_EDGE.sub("", token.casefold())


def _new_rolling_text(previous_cue: str, current_cue: str) -> str:
    """Remove only the repeated prefix introduced by rolling captions."""
    if not previous_cue:
        return current_cue

    previous_tokens = previous_cue.split()
    current_tokens = current_cue.split()
    previous_cmp = [_comparable_token(token) for token in previous_tokens]
    current_cmp = [_comparable_token(token) for token in current_tokens]
    overlap = 0
    for size in range(min(len(previous_cmp), len(current_cmp)), 0, -1):
        if previous_cmp[-size:] == current_cmp[:size]:
            overlap = size
            break
    return " ".join(current_tokens[overlap:]).strip()


def parse_vtt_pieces(text: str) -> list[CaptionPiece]:
    """Parse the smallest deduplicated text additions from WebVTT cues."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[CaptionPiece] = []
    previous_cue = ""

    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next(
            (index for index, line in enumerate(lines) if _TIMING_LINE.match(line)),
            None,
        )
        if timing_index is None:
            continue

        timing = _TIMING_LINE.match(lines[timing_index])
        if timing is None:
            continue
        cue_text = _clean_cue_text(lines[timing_index + 1 :])
        if not cue_text:
            continue

        new_text = _new_rolling_text(previous_cue, cue_text)
        previous_cue = cue_text
        if not new_text:
            continue

        pieces.append(
            CaptionPiece(
                start_ms=_timestamp_ms(timing.group("start")),
                end_ms=_timestamp_ms(timing.group("end")),
                text=new_text,
                source_index=len(pieces),
            )
        )

    if not pieces:
        raise CaptionUnavailableError("英文字幕为空或无法解析")
    return pieces


def group_caption_pieces(
    pieces: list[CaptionPiece],
    speakers: dict[int, str] | None = None,
    ambiguous_indexes: set[int] | None = None,
    *,
    max_chars: int = 360,
    max_gap_ms: int = 2000,
) -> list[SubtitleSegment]:
    """Group adjacent cues without crossing an assigned speaker boundary."""
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")
    if max_gap_ms < 0:
        raise ValueError("max_gap_ms 不能小于 0")

    ambiguous_indexes = ambiguous_indexes or set()
    segments: list[SubtitleSegment] = []
    paragraph_parts: list[str] = []
    paragraph_start = 0
    paragraph_end = 0
    paragraph_speaker: str | None = None
    paragraph_ambiguous = False

    def flush_paragraph() -> None:
        nonlocal paragraph_parts
        nonlocal paragraph_start
        nonlocal paragraph_end
        nonlocal paragraph_speaker
        nonlocal paragraph_ambiguous
        if not paragraph_parts:
            return
        segments.append(
            SubtitleSegment(
                id=f"seg-{len(segments) + 1:06d}",
                start_ms=paragraph_start,
                end_ms=paragraph_end,
                text=" ".join(paragraph_parts),
                speaker_id=paragraph_speaker,
                ambiguous=paragraph_ambiguous,
            )
        )
        paragraph_parts = []
        paragraph_speaker = None
        paragraph_ambiguous = False

    for piece in pieces:
        if speakers is None:
            speaker = None
        else:
            try:
                speaker = speakers[piece.source_index]
            except KeyError:
                raise ValueError("字幕 cue 缺少 speaker 分配") from None

        if paragraph_parts and (
            piece.start_ms - paragraph_end > max_gap_ms
            or speaker != paragraph_speaker
        ):
            flush_paragraph()
        if not paragraph_parts:
            paragraph_start = piece.start_ms
            paragraph_speaker = speaker
        paragraph_parts.append(piece.text)
        paragraph_end = piece.end_ms
        paragraph_ambiguous = (
            paragraph_ambiguous or piece.source_index in ambiguous_indexes
        )
        paragraph_text = " ".join(paragraph_parts)
        if _SENTENCE_END.search(paragraph_text) or len(paragraph_text) >= max_chars:
            flush_paragraph()
    flush_paragraph()

    if not segments:
        raise CaptionUnavailableError("英文字幕为空或无法解析")
    return segments


def parse_vtt(text: str) -> list[SubtitleSegment]:
    """Compatibility wrapper for the existing non-diarized path."""
    return group_caption_pieces(parse_vtt_pieces(text))
