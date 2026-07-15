#!/usr/bin/env python3
"""Tests for backend-neutral speaker diarization alignment."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from diarization_core import (  # noqa: E402
    DiarizationQualityError,
    DiarizationTurn,
    SpeakerBounds,
    align_caption_pieces,
    validate_diarization,
)
from podcast_captions import CaptionPiece  # noqa: E402


class SpeakerBoundsTests(unittest.TestCase):
    def test_rejects_invalid_exact_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "speaker-count"):
            SpeakerBounds(exact=0)
        with self.assertRaisesRegex(ValueError, "speaker-count"):
            SpeakerBounds(exact=7)

    def test_rejects_invalid_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "speaker 范围"):
            SpeakerBounds(minimum=3, maximum=2)


class AlignmentTests(unittest.TestCase):
    def test_selects_largest_total_overlap(self) -> None:
        piece = CaptionPiece(1000, 3000, "hello", 0)
        turns = [
            DiarizationTurn(900, 1700, "SPEAKER_00"),
            DiarizationTurn(1700, 3100, "SPEAKER_01"),
        ]

        assignment = align_caption_pieces([piece], turns)[0]

        self.assertEqual(assignment.speaker_id, "SPEAKER_01")
        self.assertEqual(assignment.overlap_ms, 1300)
        self.assertAlmostEqual(assignment.confidence, 1300 / 2000)
        self.assertEqual(assignment.method, "overlap")
        self.assertFalse(assignment.ambiguous)

    def test_sums_multiple_turns_for_the_same_speaker(self) -> None:
        piece = CaptionPiece(0, 2000, "hello", 0)
        turns = [
            DiarizationTurn(0, 400, "SPEAKER_00"),
            DiarizationTurn(400, 1200, "SPEAKER_01"),
            DiarizationTurn(1200, 1800, "SPEAKER_00"),
        ]

        assignment = align_caption_pieces([piece], turns)[0]

        self.assertEqual(assignment.speaker_id, "SPEAKER_00")
        self.assertEqual(assignment.overlap_ms, 1000)

    def test_tie_is_deterministic_and_ambiguous(self) -> None:
        piece = CaptionPiece(0, 1000, "hello", 0)
        turns = [
            DiarizationTurn(0, 500, "SPEAKER_01"),
            DiarizationTurn(500, 1000, "SPEAKER_00"),
        ]

        assignment = align_caption_pieces([piece], turns)[0]

        self.assertEqual(assignment.speaker_id, "SPEAKER_00")
        self.assertTrue(assignment.ambiguous)

    def test_uses_nearest_turn_only_within_1500_ms(self) -> None:
        pieces = [
            CaptionPiece(2000, 2300, "near", 0),
            CaptionPiece(4000, 4300, "far", 1),
        ]

        assignments = align_caption_pieces(
            pieces, [DiarizationTurn(0, 1000, "SPEAKER_00")]
        )

        self.assertEqual(assignments[0].speaker_id, "SPEAKER_00")
        self.assertEqual(assignments[0].method, "nearest")
        self.assertTrue(assignments[0].ambiguous)
        self.assertIsNone(assignments[1].speaker_id)
        self.assertEqual(assignments[1].method, "unassigned")

    def test_short_unassigned_cue_inherits_equal_direct_neighbors(self) -> None:
        pieces = [
            CaptionPiece(0, 1000, "before", 0),
            CaptionPiece(5000, 5600, "aside", 1),
            CaptionPiece(10_000, 11_000, "after", 2),
        ]
        turns = [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(10_000, 11_000, "SPEAKER_00"),
        ]

        assignment = align_caption_pieces(pieces, turns)[1]

        self.assertEqual(assignment.speaker_id, "SPEAKER_00")
        self.assertEqual(assignment.method, "neighbor")
        self.assertTrue(assignment.ambiguous)

    def test_never_smooths_across_a_speaker_change(self) -> None:
        pieces = [
            CaptionPiece(0, 1000, "before", 0),
            CaptionPiece(5000, 5600, "aside", 1),
            CaptionPiece(10_000, 11_000, "after", 2),
        ]
        turns = [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(10_000, 11_000, "SPEAKER_01"),
        ]

        assignment = align_caption_pieces(pieces, turns)[1]

        self.assertIsNone(assignment.speaker_id)
        self.assertEqual(assignment.method, "unassigned")

    def test_concurrent_speech_is_ambiguous(self) -> None:
        piece = CaptionPiece(0, 1000, "cross talk", 0)
        turns = [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(0, 500, "SPEAKER_01"),
        ]

        assignment = align_caption_pieces([piece], turns)[0]

        self.assertEqual(assignment.speaker_id, "SPEAKER_00")
        self.assertEqual(assignment.confidence, 2 / 3)
        self.assertTrue(assignment.ambiguous)


class QualityTests(unittest.TestCase):
    def test_returns_quality_counts(self) -> None:
        pieces = [
            CaptionPiece(0, 1000, "one", 0),
            CaptionPiece(1000, 2000, "two", 1),
        ]
        turns = [
            DiarizationTurn(0, 1000, "SPEAKER_00"),
            DiarizationTurn(1000, 2000, "SPEAKER_01"),
        ]
        assignments = align_caption_pieces(pieces, turns)

        quality = validate_diarization(
            turns,
            duration_ms=2000,
            bounds=SpeakerBounds(exact=2),
            assignments=assignments,
        )

        self.assertEqual(quality.detected_speakers, 2)
        self.assertEqual(quality.turn_count, 2)
        self.assertEqual(quality.unassigned_count, 0)
        self.assertEqual(quality.ambiguous_count, 0)

    def test_rejects_pathological_fragmentation(self) -> None:
        turns = [
            DiarizationTurn(i * 500, i * 500 + 300, f"SPEAKER_{i % 2:02d}")
            for i in range(101)
        ]

        with self.assertRaisesRegex(DiarizationQualityError, "切换过于频繁"):
            validate_diarization(
                turns, duration_ms=60_000, bounds=SpeakerBounds()
            )

    def test_rejects_out_of_bounds_turn(self) -> None:
        with self.assertRaisesRegex(DiarizationQualityError, "超出音频范围"):
            validate_diarization(
                [DiarizationTurn(0, 1300, "SPEAKER_00")],
                duration_ms=1000,
                bounds=SpeakerBounds(),
            )

    def test_rejects_more_than_twenty_percent_unassigned(self) -> None:
        pieces = [
            CaptionPiece(index * 4000, index * 4000 + 1000, str(index), index)
            for index in range(5)
        ]
        turns = [DiarizationTurn(0, 1000, "SPEAKER_00")]
        assignments = align_caption_pieces(pieces, turns)

        with self.assertRaisesRegex(DiarizationQualityError, "超过 20%"):
            validate_diarization(
                turns,
                duration_ms=20_000,
                bounds=SpeakerBounds(),
                assignments=assignments,
            )


if __name__ == "__main__":
    unittest.main()
