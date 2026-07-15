#!/usr/bin/env python3
"""Tests for safe diarization caches and editable speaker maps."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from diarization_cache import (  # noqa: E402
    DiarizationCache,
    load_or_create_speaker_map,
    render_speaker_sequence,
    write_speaker_map,
)
from diarization_core import (  # noqa: E402
    DiarizationTurn,
    SpeakerAssignment,
    SpeakerBounds,
)
from podcast_captions import CaptionPiece, SubtitleSegment  # noqa: E402


class DiarizationCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.job_dir = Path(self.temporary.name)
        self.cache = DiarizationCache(self.job_dir)
        self.identity = {"video_id": "video-123", "duration_ms": 12_000}
        self.bounds = SpeakerBounds(minimum=1, maximum=3)
        self.fingerprint = {
            "backend": "sherpa-onnx",
            "version": "1.13.2",
            "models": {"segmentation": "a" * 64, "embedding": "b" * 64},
        }
        self.turns = [
            DiarizationTurn(0, 3000, "SPEAKER_00"),
            DiarizationTurn(3000, 6000, "SPEAKER_01"),
        ]

    def test_valid_turn_cache_round_trips(self) -> None:
        self.cache.store_turns(
            self.identity,
            self.bounds,
            self.fingerprint,
            self.turns,
            "c" * 64,
        )

        loaded = self.cache.load_turns(
            self.identity, self.bounds, self.fingerprint
        )

        self.assertEqual(loaded, self.turns)

    def test_model_or_bounds_change_invalidates_turn_cache(self) -> None:
        self.cache.store_turns(
            self.identity,
            self.bounds,
            self.fingerprint,
            self.turns,
            "c" * 64,
        )

        self.assertIsNone(
            self.cache.load_turns(
                self.identity, SpeakerBounds(exact=2), self.fingerprint
            )
        )
        self.assertFalse(self.cache.turns_path.exists())

        self.cache.store_turns(
            self.identity,
            self.bounds,
            self.fingerprint,
            self.turns,
            "c" * 64,
        )
        self.assertIsNone(
            self.cache.load_turns(
                self.identity, self.bounds, {"backend": "changed"}
            )
        )

    def test_malformed_cache_is_deleted_and_treated_as_miss(self) -> None:
        self.cache.turns_path.write_text("{not-json", encoding="utf-8")

        loaded = self.cache.load_turns(
            self.identity, self.bounds, self.fingerprint
        )

        self.assertIsNone(loaded)
        self.assertFalse(self.cache.turns_path.exists())

    def test_turn_cache_contains_no_urls_paths_tokens_or_embeddings(self) -> None:
        self.cache.store_turns(
            self.identity,
            self.bounds,
            self.fingerprint,
            self.turns,
            "c" * 64,
        )

        payload = self.cache.turns_path.read_text(encoding="utf-8").casefold()

        for forbidden in (
            "youtube.com",
            "http://",
            "https://",
            "api_key",
            "access_token",
            "cookie",
            "embedding_value",
            ".wav",
        ):
            self.assertNotIn(forbidden, payload)

    def test_assignments_round_trip_and_vtt_change_invalidates_only_them(self) -> None:
        pieces = [CaptionPiece(0, 1000, "hello", 0)]
        assignments = [
            SpeakerAssignment(0, "SPEAKER_00", 1000, 1.0, False, "overlap")
        ]
        self.cache.store_turns(
            self.identity,
            self.bounds,
            self.fingerprint,
            self.turns,
            "c" * 64,
        )
        self.cache.store_assignments(pieces, assignments)

        self.assertEqual(self.cache.load_assignments(pieces), assignments)
        changed = [CaptionPiece(0, 1000, "changed", 0)]
        self.assertIsNone(self.cache.load_assignments(changed))
        self.assertFalse(self.cache.assignments_path.exists())
        self.assertTrue(self.cache.turns_path.exists())

    def test_publishing_new_turns_deletes_orphaned_assignments(self) -> None:
        pieces = [CaptionPiece(0, 1000, "hello", 0)]
        assignments = [
            SpeakerAssignment(0, "SPEAKER_00", 1000, 1.0, False, "overlap")
        ]
        self.cache.store_assignments(pieces, assignments)
        self.assertTrue(self.cache.assignments_path.exists())

        self.cache.store_turns(
            self.identity,
            self.bounds,
            self.fingerprint,
            self.turns,
            "c" * 64,
        )

        self.assertFalse(self.cache.assignments_path.exists())


class SpeakerMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "speaker-map.json"

    def test_creates_defaults_in_first_appearance_order(self) -> None:
        mapping = load_or_create_speaker_map(
            self.path, ["SPEAKER_02", "SPEAKER_02", "SPEAKER_00"]
        )

        self.assertEqual(list(mapping), ["SPEAKER_02", "SPEAKER_00"])
        self.assertEqual(mapping["SPEAKER_02"]["role"], "说话人甲")
        self.assertEqual(mapping["SPEAKER_02"]["voice_slot"], "female_1")
        self.assertEqual(mapping["SPEAKER_00"]["role"], "说话人乙")
        self.assertEqual(mapping["SPEAKER_00"]["voice_slot"], "male_1")

    def test_user_map_edits_are_preserved_and_new_speakers_only_append(self) -> None:
        mapping = load_or_create_speaker_map(
            self.path, ["SPEAKER_00", "SPEAKER_01"]
        )
        mapping["SPEAKER_00"]["display_name"] = "主持人"
        write_speaker_map(self.path, mapping)

        loaded = load_or_create_speaker_map(
            self.path, ["SPEAKER_00", "SPEAKER_02"]
        )

        self.assertEqual(loaded["SPEAKER_00"]["display_name"], "主持人")
        self.assertIn("SPEAKER_01", loaded)
        self.assertEqual(loaded["SPEAKER_02"]["role"], "说话人丙")

    def test_invalid_user_voice_slot_is_reported_not_overwritten(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "SPEAKER_00": {
                        "role": "主持人",
                        "display_name": "",
                        "voice_slot": "invented_voice",
                        "mapping_confidence": "unknown",
                    }
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "voice_slot"):
            load_or_create_speaker_map(self.path, ["SPEAKER_00"])

        self.assertIn("invented_voice", self.path.read_text(encoding="utf-8"))

    def test_sequence_is_one_to_one_and_uses_explicit_voice_numbers(self) -> None:
        segments = [
            SubtitleSegment(
                "seg-000001", 0, 1000, "one", speaker_id="SPEAKER_00"
            ),
            SubtitleSegment(
                "seg-000002", 1000, 2000, "two", speaker_id="SPEAKER_01"
            ),
        ]
        mapping = {
            "SPEAKER_00": {
                "role": "说话人甲",
                "display_name": "",
                "voice_slot": "female_1",
                "mapping_confidence": "unknown",
            },
            "SPEAKER_01": {
                "role": "说话人乙",
                "display_name": "",
                "voice_slot": "male_1",
                "mapping_confidence": "unknown",
            },
        }

        labels = render_speaker_sequence(segments, mapping)

        self.assertEqual(labels, ["说话人甲-女声1", "说话人乙-男声1"])
        self.assertEqual(len(labels), len(segments))

    def test_accepts_a_third_male_voice_for_three_man_podcasts(self) -> None:
        segments = [
            SubtitleSegment(
                "seg-000001", 0, 1000, "three", speaker_id="SPEAKER_02"
            )
        ]
        mapping = {
            "SPEAKER_02": {
                "role": "说话人丙",
                "display_name": "",
                "voice_slot": "male_3",
                "mapping_confidence": "confirmed",
            }
        }

        write_speaker_map(self.path, mapping)

        self.assertEqual(
            render_speaker_sequence(segments, mapping),
            ["说话人丙-男声3"],
        )


if __name__ == "__main__":
    unittest.main()
