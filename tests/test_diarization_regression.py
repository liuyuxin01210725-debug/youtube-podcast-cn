#!/usr/bin/env python3
"""JSON-backed regression locks for soft closed-world stitch and ghost collapse."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from diarization_core import DiarizationTurn, SpeakerBounds  # noqa: E402
import diarization_runtime as runtime_module  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/diarization-regression.json"


def _turns(rows):
    return [DiarizationTurn(start, end, speaker) for start, end, speaker in rows]


class DiarizationRegressionFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in cls.payload["cases"]}

    def test_fixture_documents_default_threshold_strategy(self) -> None:
        strategy = self.payload["default_threshold_strategy"]
        self.assertEqual(strategy["local"], 0.5)
        self.assertEqual(strategy["youtube"], 0.9)
        self.assertIn("20s", strategy["rationale"])

    def test_soft_closed_world_remap_from_fixture(self) -> None:
        case = self.cases["soft_closed_world_remap"]
        mapping = runtime_module._speaker_mapping(
            _turns(case["existing"]),
            _turns(case["incoming"]),
            overlap_start_ms=case["overlap_start_ms"],
            overlap_end_ms=case["overlap_end_ms"],
            bounds=SpeakerBounds(),
            existing_embeddings=case["existing_embeddings"],
            incoming_embeddings=case["incoming_embeddings"],
        )
        self.assertEqual(mapping, case["expected_mapping"])
        self.assertTrue(set(mapping.values()).isdisjoint(case["forbidden_globals"]))

    def test_collapse_speakers_under_about_20s(self) -> None:
        case = self.cases["collapse_under_20s_into_dominant"]
        collapsed = runtime_module._collapse_minor_speakers(
            _turns(case["turns"]),
            case["prototypes"],
            min_ms=case["min_ms"],
        )
        self.assertEqual(
            {turn.speaker_id for turn in collapsed},
            set(case["expected_speakers"]),
        )
        remapped = next(
            turn for turn in collapsed if turn.start_ms == case["turns"][1][0]
        )
        self.assertEqual(
            remapped.speaker_id,
            case["expected_remap"]["SPEAKER_02"],
        )

    def test_first_chunk_even_id_oversplit_ghosts_collapse(self) -> None:
        case = self.cases["first_chunk_even_id_oversplit_no_ghost_keep"]
        collapsed = runtime_module._collapse_minor_speakers(
            _turns(case["turns"]),
            case["prototypes"],
            min_ms=case["min_ms"],
        )
        speakers = {turn.speaker_id for turn in collapsed}
        self.assertEqual(speakers, set(case["expected_speakers"]))
        self.assertTrue(speakers.isdisjoint(case["forbidden_speakers"]))

    def test_multi_chunk_soft_closed_world_then_collapse_no_invented_ghosts(
        self,
    ) -> None:
        case = self.cases["multi_chunk_soft_closed_world_no_invent"]
        mapping = runtime_module._speaker_mapping(
            _turns(case["first"]),
            _turns(case["second"]),
            overlap_start_ms=case["overlap_start_ms"],
            overlap_end_ms=case["overlap_end_ms"],
            bounds=SpeakerBounds(),
            existing_embeddings=case["existing_embeddings"],
            incoming_embeddings=case["incoming_embeddings"],
        )
        # Soft closed-world must not invent NEW even-id globals beyond first-chunk set.
        self.assertTrue(
            set(mapping.values()).isdisjoint(set(case["forbidden_new_speakers"]))
        )
        stitched = runtime_module._stitch_diarization_chunks(
            _turns(case["first"]),
            _turns(case["second"]),
            overlap_start_ms=case["overlap_start_ms"],
            overlap_end_ms=case["overlap_end_ms"],
            bounds=SpeakerBounds(),
            mapping=mapping,
        )
        collapsed = runtime_module._collapse_minor_speakers(
            stitched,
            {
                **case["existing_embeddings"],
                **{
                    mapping[local]: vector
                    for local, vector in case["incoming_embeddings"].items()
                },
            },
            min_ms=case["min_ms"],
        )
        speakers = {turn.speaker_id for turn in collapsed}
        self.assertEqual(speakers, set(case["expected_speakers"]))
        self.assertNotIn("SPEAKER_02", speakers)
        self.assertNotIn("SPEAKER_04", speakers)


if __name__ == "__main__":
    unittest.main()
