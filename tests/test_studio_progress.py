#!/usr/bin/env python3
"""Unit tests for stage ETA helper and progress payload shape."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from studio_progress import build_progress_payload, estimate_eta_seconds  # noqa: E402
from studio_engine import StudioEngine  # noqa: E402


class EstimateEtaTests(unittest.TestCase):
    def test_unknown_until_progress(self) -> None:
        self.assertIsNone(estimate_eta_seconds(0, 10, 100.0, now=110.0))
        self.assertIsNone(estimate_eta_seconds(3, 0, 100.0, now=110.0))

    def test_linear_remaining(self) -> None:
        # 4 of 10 done in 40s → 6 remain at same rate → 60s
        self.assertEqual(
            estimate_eta_seconds(4, 10, 100.0, now=140.0),
            60,
        )

    def test_done_is_zero(self) -> None:
        self.assertEqual(estimate_eta_seconds(10, 10, 100.0, now=150.0), 0)


class ProgressPayloadTests(unittest.TestCase):
    def test_includes_eta_and_fallback(self) -> None:
        payload = build_progress_payload(
            "asr",
            completed=2,
            total=5,
            stage_started_at=100.0,
            updated_at=160.0,
            subtitle_fallback_reason="no english track",
            message="fallback",
        )
        self.assertEqual(payload["stage"], "asr")
        self.assertEqual(payload["eta_seconds"], 90)
        self.assertEqual(payload["subtitle_fallback_reason"], "no english track")
        self.assertEqual(payload["stage_started_at"], 100.0)


class EngineProgressTests(unittest.TestCase):
    def test_progress_persists_eta_and_carries_fallback_reason(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            with patch("studio_engine.time.time", side_effect=[1000.0, 1060.0]):
                engine.progress(
                    "asr",
                    0,
                    4,
                    subtitle_fallback_reason="英文字幕文件为空或无法解析",
                )
                engine.progress("asr", 2, 4)
            payload = json.loads((engine.job / "progress.json").read_text())
            self.assertEqual(payload["stage"], "asr")
            self.assertEqual(payload["completed"], 2)
            self.assertEqual(payload["total"], 4)
            self.assertEqual(payload["subtitle_fallback_reason"], "英文字幕文件为空或无法解析")
            self.assertEqual(payload["eta_seconds"], 60)
            self.assertIn("stage_started_at", payload)


if __name__ == "__main__":
    unittest.main()
