#!/usr/bin/env python3
"""Tests for provider-neutral TTS backends."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tts_backends


class EdgeBackendTests(unittest.TestCase):
    def test_build_backend_defaults_to_edge_contract(self) -> None:
        backend = tts_backends.build_backend("edge")
        self.assertEqual(backend.name, "edge")
        self.assertEqual(backend.model, "edge-tts")
        self.assertEqual(backend.file_suffix, ".mp3")
        self.assertEqual(backend.max_chars, 2500)

    def test_edge_resolves_semantic_voice_slot(self) -> None:
        backend = tts_backends.EdgeTTSBackend(
            voice_map={
                "male_1": "edge-m1",
                "male_2": "edge-m2",
                "female_1": "edge-f1",
                "female_2": "edge-f2",
            }
        )
        self.assertEqual(backend.resolve_voice("female_2"), "edge-f2")

    def test_edge_synthesizes_through_edge_tts(self) -> None:
        calls: list[tuple[str, str, str]] = []

        class FakeCommunicate:
            def __init__(self, text: str, voice: str):
                self.text = text
                self.voice = voice

            async def save(self, output: str) -> None:
                calls.append((self.text, self.voice, output))
                Path(output).write_bytes(b"edge-audio")

        request = tts_backends.SynthesisRequest(
            text="你好。", voice_slot="female_1", style="conversation"
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "part.mp3"
            backend = tts_backends.EdgeTTSBackend()
            with mock.patch.object(
                tts_backends.edge_tts, "Communicate", FakeCommunicate
            ):
                usage = asyncio.run(backend.synthesize(request, output))
            self.assertEqual(output.read_bytes(), b"edge-audio")
            self.assertEqual(
                calls[0][:2], ("你好。", backend.resolve_voice("female_1"))
            )
            self.assertEqual(usage.output_tokens, 0)


if __name__ == "__main__":
    unittest.main()
