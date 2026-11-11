#!/usr/bin/env python3
"""Exact-count floor for the 20s minor-speaker fold.

The 20s collapse is a candidate cause of "declared N, actual N-1" failures.
These tests record before/after durations. They do not claim a specific
person (for example Joyce Chen) was the collapsed id in a production job.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from diarization_core import DiarizationTurn, SpeakerBounds
import diarization_runtime as runtime_module


def _duration_map(turns):
    totals = {}
    for turn in turns:
        totals[turn.speaker_id] = totals.get(turn.speaker_id, 0) + (turn.end_ms - turn.start_ms)
    return totals


def _prototypes(*ids):
    # Distinct unit vectors so nearest-major is deterministic when needed.
    basis = {
        "SPEAKER_00": [1.0, 0.0, 0.0, 0.0, 0.0],
        "SPEAKER_01": [0.0, 1.0, 0.0, 0.0, 0.0],
        "SPEAKER_02": [0.0, 0.0, 1.0, 0.0, 0.0],
        "SPEAKER_03": [0.0, 0.0, 0.0, 1.0, 0.0],
        "SPEAKER_04": [0.0, 0.0, 0.0, 0.0, 1.0],
        "SPEAKER_05": [0.7, 0.7, 0.0, 0.0, 0.0],
    }
    return {sid: basis[sid] for sid in ids}


class CollapseExactFloorTests(unittest.TestCase):
    def test_candidate_20s_fold_can_drop_below_declared_five_without_floor(self):
        """Before/after evidence for the candidate root cause (no person named)."""
        turns = [
            DiarizationTurn(0, 120_000, "SPEAKER_00"),
            DiarizationTurn(120_000, 240_000, "SPEAKER_01"),
            DiarizationTurn(240_000, 360_000, "SPEAKER_02"),
            DiarizationTurn(360_000, 480_000, "SPEAKER_03"),
            # Short content-like voice, under the 20s default, not a 1s blip.
            DiarizationTurn(480_000, 488_000, "SPEAKER_04"),
        ]
        before = _duration_map(turns)
        self.assertEqual(sorted(before), ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03", "SPEAKER_04"])
        self.assertEqual(before["SPEAKER_04"], 8_000)
        self.assertTrue(all(total >= 20_000 for sid, total in before.items() if sid != "SPEAKER_04"))
        collapsed = runtime_module._collapse_minor_speakers(
            turns, _prototypes(*before), min_ms=20_000
        )
        after = _duration_map(collapsed)
        # Default (no exact floor) still folds the short id away: 5 → 4.
        self.assertEqual(len(before), 5)
        self.assertEqual(len(after), 4)
        self.assertNotIn("SPEAKER_04", after)
        self.assertEqual(set(after), {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"})

    def test_exact_five_keeps_short_content_voice(self):
        turns = [
            DiarizationTurn(0, 120_000, "SPEAKER_00"),
            DiarizationTurn(120_000, 240_000, "SPEAKER_01"),
            DiarizationTurn(240_000, 360_000, "SPEAKER_02"),
            DiarizationTurn(360_000, 480_000, "SPEAKER_03"),
            DiarizationTurn(480_000, 488_000, "SPEAKER_04"),
        ]
        collapsed = runtime_module._collapse_minor_speakers(
            turns,
            _prototypes("SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03", "SPEAKER_04"),
            min_ms=20_000,
            bounds=SpeakerBounds(exact=5),
        )
        after = _duration_map(collapsed)
        self.assertEqual(len(after), 5)
        self.assertEqual(after["SPEAKER_04"], 8_000)
        self.assertEqual(collapsed[4].speaker_id, "SPEAKER_04")

    def test_ghost_above_exact_is_still_collapsed(self):
        """Counterexample: a short ghost beyond the declared count must not be kept."""
        turns = [
            DiarizationTurn(0, 100_000, "SPEAKER_00"),
            DiarizationTurn(100_000, 200_000, "SPEAKER_01"),
            DiarizationTurn(200_000, 300_000, "SPEAKER_02"),
            DiarizationTurn(300_000, 400_000, "SPEAKER_03"),
            DiarizationTurn(400_000, 500_000, "SPEAKER_04"),
            # 1.2s noise-like ghost, closest to SPEAKER_00.
            DiarizationTurn(500_000, 501_200, "SPEAKER_05"),
        ]
        prototypes = _prototypes("SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03", "SPEAKER_04", "SPEAKER_05")
        before = _duration_map(turns)
        self.assertEqual(len(before), 6)
        collapsed = runtime_module._collapse_minor_speakers(
            turns, prototypes, min_ms=20_000, bounds=SpeakerBounds(exact=5)
        )
        after = _duration_map(collapsed)
        self.assertEqual(len(after), 5)
        self.assertNotIn("SPEAKER_05", after)
        self.assertEqual(collapsed[-1].speaker_id, "SPEAKER_00")
        self.assertEqual(set(after), {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03", "SPEAKER_04"})

    def test_already_four_is_not_padded_to_declared_five(self):
        """If clustering only produced 4 ids, do not invent a 5th to pass the gate."""
        turns = [
            DiarizationTurn(0, 120_000, "SPEAKER_00"),
            DiarizationTurn(120_000, 240_000, "SPEAKER_01"),
            DiarizationTurn(240_000, 360_000, "SPEAKER_02"),
            DiarizationTurn(360_000, 368_000, "SPEAKER_03"),
        ]
        before = _duration_map(turns)
        self.assertEqual(len(before), 4)
        collapsed = runtime_module._collapse_minor_speakers(
            turns,
            _prototypes("SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"),
            min_ms=20_000,
            bounds=SpeakerBounds(exact=5),
        )
        after = _duration_map(collapsed)
        self.assertEqual(len(after), 4)
        self.assertEqual(after, before)
        self.assertIn("SPEAKER_03", after)

    def test_two_short_voices_only_fold_the_excess_shortest(self):
        turns = [
            DiarizationTurn(0, 80_000, "SPEAKER_00"),
            DiarizationTurn(80_000, 160_000, "SPEAKER_01"),
            DiarizationTurn(160_000, 240_000, "SPEAKER_02"),
            DiarizationTurn(240_000, 320_000, "SPEAKER_03"),
            DiarizationTurn(320_000, 328_000, "SPEAKER_04"),  # 8s keep
            DiarizationTurn(328_000, 329_000, "SPEAKER_05"),  # 1s fold
        ]
        collapsed = runtime_module._collapse_minor_speakers(
            turns,
            _prototypes("SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03", "SPEAKER_04", "SPEAKER_05"),
            min_ms=20_000,
            bounds=SpeakerBounds(exact=5),
        )
        after = _duration_map(collapsed)
        self.assertEqual(len(after), 5)
        self.assertIn("SPEAKER_04", after)
        self.assertNotIn("SPEAKER_05", after)

    def test_without_exact_bounds_legacy_fold_still_merges_all_minors(self):
        turns = [
            DiarizationTurn(0, 100_000, "SPEAKER_00"),
            DiarizationTurn(100_000, 105_000, "SPEAKER_02"),
            DiarizationTurn(105_000, 400_000, "SPEAKER_06"),
        ]
        collapsed = runtime_module._collapse_minor_speakers(
            turns,
            {
                "SPEAKER_00": [1.0, 0.0],
                "SPEAKER_02": [0.95, 0.05],
                "SPEAKER_06": [0.0, 1.0],
            },
            min_ms=20_000,
        )
        self.assertEqual({turn.speaker_id for turn in collapsed}, {"SPEAKER_00", "SPEAKER_06"})
