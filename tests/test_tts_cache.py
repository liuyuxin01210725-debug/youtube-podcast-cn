#!/usr/bin/env python3
"""Tests for persistent content-addressed TTS fragment caching."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tts_backends import SynthesisRequest
from tts_cache import FragmentCache


class FragmentCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = SynthesisRequest("正文", "female_1", "conversation")

    def test_key_changes_with_backend_model_style_or_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = FragmentCache(
                Path(tmp),
                {
                    "backend": "qwen3-api",
                    "model": "model-a",
                    "voices": {"female_1": "Cherry"},
                },
                ".wav",
            )
            second = FragmentCache(
                Path(tmp),
                {
                    "backend": "qwen3-api",
                    "model": "model-b",
                    "voices": {"female_1": "Cherry"},
                },
                ".wav",
            )
            self.assertNotEqual(
                first.key_for(self.request), second.key_for(self.request)
            )
            self.assertNotEqual(
                first.key_for(self.request),
                first.key_for(
                    SynthesisRequest("不同正文", "female_1", "conversation")
                ),
            )
            self.assertNotEqual(
                first.key_for(self.request),
                first.key_for(SynthesisRequest("正文", "female_1", "documentary")),
            )

    def test_store_and_get_valid_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated.wav"
            generated.write_bytes(b"valid-audio")
            cache = FragmentCache(
                root / "cache", {"backend": "qwen3-api"}, ".wav"
            )
            stored = cache.store(self.request, generated)
            hit = cache.get(
                self.request, lambda path: path.read_bytes() == b"valid-audio"
            )
            self.assertEqual(hit, stored)
            self.assertTrue(stored.is_file())

    def test_corrupt_hit_is_deleted_and_treated_as_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated.wav"
            generated.write_bytes(b"corrupt")
            cache = FragmentCache(
                root / "cache", {"backend": "qwen3-api"}, ".wav"
            )
            stored = cache.store(self.request, generated)
            self.assertIsNone(cache.get(self.request, lambda path: False))
            self.assertFalse(stored.exists())

    def test_rejects_empty_generated_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "empty.wav"
            generated.write_bytes(b"")
            cache = FragmentCache(root / "cache", {"backend": "edge"}, ".wav")
            with self.assertRaisesRegex(RuntimeError, "空音频"):
                cache.store(self.request, generated)


if __name__ == "__main__":
    unittest.main()
