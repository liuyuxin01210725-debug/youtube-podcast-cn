#!/usr/bin/env python3
"""Regression tests for the Chinese podcast TTS pipeline."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_tts_module():
    """Load the script without requiring network/audio packages in tests."""
    sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))
    if "pydub" not in sys.modules:
        pydub = types.ModuleType("pydub")
        pydub.AudioSegment = object
        sys.modules["pydub"] = pydub

    spec = importlib.util.spec_from_file_location("tts_cn", ROOT / "scripts/tts-cn.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 scripts/tts-cn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tts_cn = load_tts_module()


class NoiseParagraphTests(unittest.TestCase):
    def test_strips_internal_segment_id_comments(self) -> None:
        cleaned = tts_cn.strip_markdown(
            "<!-- seg-000001 -->\n这是中文译文。\n<!-- seg-000002 -->\n下一段。"
        )
        self.assertEqual(cleaned, "这是中文译文。\n下一段。")

    def test_preserves_short_real_dialogue(self) -> None:
        self.assertEqual(tts_cn.drop_noise_paragraphs(["没错"]), ["没错"])

    def test_removes_known_metadata_but_keeps_short_sentence(self) -> None:
        self.assertEqual(
            tts_cn.drop_noise_paragraphs(["对。", "播客详情"]),
            ["对。"],
        )


class OutputIntegrityTests(unittest.TestCase):
    def test_accepts_complete_synthesis(self) -> None:
        tts_cn.ensure_all_synthesized({"failed": 0, "ok": 3, "total": 3})

    def test_rejects_any_failed_fragment(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "1 个音频片段合成失败"):
            tts_cn.ensure_all_synthesized({"failed": 1, "ok": 2, "total": 3})

    def test_rejects_missing_fragment_even_without_failure_count(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "音频片段不完整"):
            tts_cn.ensure_all_synthesized({"failed": 0, "ok": 2, "total": 3})

    def test_atomic_publish_replaces_destination_only_with_nonempty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prepared = directory / "prepared.mp3"
            output = directory / "episode.mp3"
            prepared.write_bytes(b"new audio")
            output.write_bytes(b"old audio")

            tts_cn.publish_mp3_atomically(prepared, output)

            self.assertEqual(output.read_bytes(), b"new audio")
            self.assertFalse(prepared.exists())

    def test_atomic_publish_keeps_destination_when_prepared_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prepared = directory / "prepared.mp3"
            output = directory / "episode.mp3"
            prepared.write_bytes(b"")
            output.write_bytes(b"old audio")

            with self.assertRaisesRegex(RuntimeError, "为空"):
                tts_cn.publish_mp3_atomically(prepared, output)

            self.assertEqual(output.read_bytes(), b"old audio")

    def test_merge_rejects_a_corrupt_fragment_instead_of_skipping_it(self) -> None:
        class FakeSegment:
            def export(self, out, format):
                Path(out).write_bytes(b"partial audio")

        class FakeAudioSegment:
            @staticmethod
            def silent(duration):
                return FakeSegment()

            @staticmethod
            def empty():
                return FakeSegment()

            @staticmethod
            def from_mp3(path):
                raise ValueError("corrupt mp3")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fragment = directory / "seg.mp3"
            output = directory / "episode.mp3"

            with (
                mock.patch.object(tts_cn, "AudioSegment", FakeAudioSegment),
                mock.patch.object(tts_cn, "fix_mp3_header"),
                self.assertRaisesRegex(RuntimeError, "片段无法解码"),
            ):
                tts_cn.merge([(fragment, "voice")], output)

            self.assertFalse(output.exists())

    def test_run_rejects_partial_synthesis_before_merge(self) -> None:
        async def fake_synth_all(segments, tmp):
            return [(tmp / "seg.mp3", "voice")], {
                "failed": 1,
                "ok": 1,
                "total": 2,
                "retried": 0,
            }

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "episode.md"
            output = directory / "episode.mp3"
            source.write_text("这是一段测试。", encoding="utf-8")
            output.write_bytes(b"old audio")

            with (
                mock.patch.object(tts_cn, "synth_all", fake_synth_all),
                mock.patch.object(tts_cn, "merge") as merge_mock,
                self.assertRaisesRegex(RuntimeError, "1 个音频片段合成失败"),
            ):
                asyncio.run(tts_cn.run(source, output))

            merge_mock.assert_not_called()
            self.assertEqual(output.read_bytes(), b"old audio")

    def test_run_merges_to_temporary_file_before_publishing(self) -> None:
        async def fake_synth_all(segments, tmp):
            return [(tmp / "seg.mp3", "voice")], {
                "failed": 0,
                "ok": 1,
                "total": 1,
                "retried": 0,
            }

        merge_outputs: list[Path] = []

        def fake_merge(files_with_voices, out):
            merge_outputs.append(out)
            out.write_bytes(b"new audio")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "episode.md"
            output = directory / "episode.mp3"
            source.write_text("这是一段测试。", encoding="utf-8")
            output.write_bytes(b"old audio")

            with (
                mock.patch.object(tts_cn, "synth_all", fake_synth_all),
                mock.patch.object(tts_cn, "merge", fake_merge),
            ):
                asyncio.run(tts_cn.run(source, output))

            self.assertEqual(len(merge_outputs), 1)
            self.assertNotEqual(merge_outputs[0], output)
            self.assertEqual(merge_outputs[0].parent, output.parent)
            self.assertEqual(output.read_bytes(), b"new audio")


if __name__ == "__main__":
    unittest.main()
