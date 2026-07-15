#!/usr/bin/env python3
"""Tests for atomic VTT cues and speaker-aware grouping."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from podcast_captions import (  # noqa: E402
    CaptionPiece,
    group_caption_pieces,
    parse_vtt,
    parse_vtt_pieces,
)


FIXTURE = ROOT / "tests/fixtures/podcast-sample.en.vtt"


class AtomicCaptionTests(unittest.TestCase):
    def test_preserves_deduplicated_atomic_cues(self) -> None:
        pieces = parse_vtt_pieces(FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual([piece.source_index for piece in pieces], [0, 1, 2, 3])
        self.assertEqual(
            [piece.text for piece in pieces],
            [
                "Hello & welcome",
                "to the show.",
                "Today we talk health.",
                "Exactly.",
            ],
        )
        self.assertEqual((pieces[0].start_ms, pieces[0].end_ms), (0, 2000))
        self.assertEqual((pieces[-1].start_ms, pieces[-1].end_ms), (7500, 9000))

    def test_speaker_change_is_a_hard_segment_boundary(self) -> None:
        pieces = [
            CaptionPiece(0, 1000, "Hello", 0),
            CaptionPiece(1000, 2000, "there.", 1),
        ]

        segments = group_caption_pieces(
            pieces,
            {0: "SPEAKER_00", 1: "SPEAKER_01"},
        )

        self.assertEqual([segment.text for segment in segments], ["Hello", "there."])
        self.assertEqual(
            [segment.speaker_id for segment in segments],
            ["SPEAKER_00", "SPEAKER_01"],
        )

    def test_grouped_segment_is_ambiguous_when_any_piece_is_ambiguous(self) -> None:
        pieces = [
            CaptionPiece(0, 1000, "Hello", 0),
            CaptionPiece(1000, 2000, "there.", 1),
        ]

        segments = group_caption_pieces(
            pieces,
            {0: "SPEAKER_00", 1: "SPEAKER_00"},
            {1},
        )

        self.assertEqual(len(segments), 1)
        self.assertTrue(segments[0].ambiguous)

    def test_requires_every_piece_to_have_a_speaker_when_mapping_is_supplied(self) -> None:
        pieces = [CaptionPiece(0, 1000, "Hello.", 0)]

        with self.assertRaisesRegex(ValueError, "字幕 cue 缺少 speaker 分配"):
            group_caption_pieces(pieces, {})

    def test_non_diarized_wrapper_preserves_existing_output(self) -> None:
        segments = parse_vtt(FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual(
            [segment.text for segment in segments],
            [
                "Hello & welcome to the show.",
                "Today we talk health.",
                "Exactly.",
            ],
        )
        self.assertEqual(
            [segment.id for segment in segments],
            ["seg-000001", "seg-000002", "seg-000003"],
        )


if __name__ == "__main__":
    unittest.main()
