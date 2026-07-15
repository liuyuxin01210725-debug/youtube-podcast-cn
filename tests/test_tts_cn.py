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
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tts_backends import SynthesisUsage, TTSBackendError
from tts_cache import FragmentCache


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


class CliTests(unittest.TestCase):
    def test_defaults_to_edge_conversation(self) -> None:
        args = tts_cn.build_parser().parse_args(["episode.md"])
        self.assertEqual(args.tts, "edge")
        self.assertEqual(args.style, "conversation")
        self.assertIsNone(args.preview_paragraphs)

    def test_accepts_qwen_documentary_preview(self) -> None:
        args = tts_cn.build_parser().parse_args(
            [
                "episode.md",
                "--tts",
                "qwen3-api",
                "--style",
                "documentary",
                "--preview-paragraphs",
                "8",
            ]
        )
        self.assertEqual(args.tts, "qwen3-api")
        self.assertEqual(args.style, "documentary")
        self.assertEqual(args.preview_paragraphs, 8)

    def test_preview_slices_paragraphs_and_speaker_sequence_together(self) -> None:
        cleaned = "第一段。\n\n第二段。\n\n第三段。"
        rendered = tts_cn.apply_speaker_sequence(
            cleaned,
            ["主持人-女", "嘉宾-男", "主持人-女"],
            preview_paragraphs=2,
        )
        self.assertIn("第一段", rendered)
        self.assertIn("第二段", rendered)
        self.assertNotIn("第三段", rendered)

    def test_preview_validates_full_speaker_sequence_before_slicing(self) -> None:
        cleaned = "第一段。\n\n第二段。\n\n第三段。"
        with self.assertRaisesRegex(ValueError, "speaker 序列行数"):
            tts_cn.apply_speaker_sequence(
                cleaned,
                ["主持人-女", "嘉宾-男"],
                preview_paragraphs=2,
            )


class VoiceSlotTests(unittest.TestCase):
    def test_honors_explicit_numbered_voice_slots(self) -> None:
        assigned: dict[str, str] = {}
        self.assertEqual(
            tts_cn.voice_slot_for_label("说话人甲-女声2", assigned),
            "female_2",
        )
        self.assertEqual(
            tts_cn.voice_slot_for_label("说话人乙-男声1", assigned),
            "male_1",
        )

    def test_assigns_stable_semantic_slots_by_gender(self) -> None:
        assigned: dict[str, str] = {}
        self.assertEqual(
            tts_cn.voice_slot_for_label("主持人-女", assigned), "female_1"
        )
        self.assertEqual(
            tts_cn.voice_slot_for_label("嘉宾甲-男", assigned), "male_1"
        )
        self.assertEqual(
            tts_cn.voice_slot_for_label("嘉宾乙-男", assigned), "male_2"
        )
        self.assertEqual(
            tts_cn.voice_slot_for_label("主持人-女", assigned), "female_1"
        )

    def test_plain_text_uses_default_slot_without_relabeling_explicit_female(self) -> None:
        self.assertEqual(
            tts_cn.parse_segments("这是纯文本。"), [("default", "这是纯文本。")]
        )
        self.assertEqual(
            tts_cn.parse_segments("[主持人-女] 这是显式女声。"),
            [("female_1", "这是显式女声。")],
        )


class ChunkLongTests(unittest.TestCase):
    def assert_valid_chunks(self, text: str, max_chars: int) -> None:
        chunks = tts_cn.chunk_long(text, max_chars=max_chars)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk) <= max_chars for chunk in chunks))

    def test_hard_splits_451_character_text_without_punctuation(self) -> None:
        self.assert_valid_chunks("甲" * 451, max_chars=450)

    def test_hard_splits_long_sentence_with_only_trailing_punctuation(self) -> None:
        self.assert_valid_chunks("乙" * 451 + "。", max_chars=450)


class FragmentCacheOrchestrationTests(unittest.TestCase):
    class FakeBackend:
        name = "fake"
        file_suffix = ".wav"
        max_chars = 450
        concurrency = 1

        def __init__(self, model: str = "fake-model") -> None:
            self.model = model
            self.calls = 0

        def resolve_voice(self, voice_slot: str) -> str:
            return f"resolved-{voice_slot}"

        def fingerprint(self) -> dict[str, object]:
            return {
                "backend": self.name,
                "model": self.model,
                "voice": "resolved-female_1",
            }

        async def synthesize(self, request, output):
            self.calls += 1
            output.write_bytes(b"valid-audio")
            return SynthesisUsage(
                request_id=f"request-{self.calls}",
                input_tokens=11,
                output_tokens=22,
                audio_tokens=20,
            )

    def test_second_run_uses_cache_without_provider_call_or_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = self.FakeBackend()
            cache = FragmentCache(
                root / "cache", backend.fingerprint(), backend.file_suffix
            )
            segments = [("female_1", "测试正文。")]

            with mock.patch.object(
                tts_cn,
                "audio_is_decodable",
                side_effect=lambda path: path.read_bytes() == b"valid-audio",
            ):
                first_files, first_stats = asyncio.run(
                    tts_cn.synth_all(
                        segments,
                        root / "first",
                        backend=backend,
                        style="conversation",
                        cache=cache,
                    )
                )
                second_files, second_stats = asyncio.run(
                    tts_cn.synth_all(
                        segments,
                        root / "second",
                        backend=backend,
                        style="conversation",
                        cache=cache,
                    )
                )

            self.assertEqual(backend.calls, 1)
            self.assertEqual(first_stats["provider_calls"], 1)
            self.assertEqual(first_stats["cached"], 0)
            self.assertEqual(first_stats["audio_tokens"], 20)
            self.assertEqual(second_stats["provider_calls"], 0)
            self.assertEqual(second_stats["cached"], 1)
            self.assertEqual(second_stats["audio_tokens"], 0)
            self.assertEqual(first_files, second_files)

    def test_style_change_uses_a_different_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = self.FakeBackend()
            cache = FragmentCache(
                root / "cache", backend.fingerprint(), backend.file_suffix
            )
            segments = [("female_1", "测试正文。")]
            with mock.patch.object(tts_cn, "audio_is_decodable", return_value=True):
                asyncio.run(
                    tts_cn.synth_all(
                        segments,
                        root / "first",
                        backend=backend,
                        style="conversation",
                        cache=cache,
                    )
                )
                asyncio.run(
                    tts_cn.synth_all(
                        segments,
                        root / "second",
                        backend=backend,
                        style="documentary",
                        cache=cache,
                    )
                )
            self.assertEqual(backend.calls, 2)

    def test_duplicate_fragments_share_one_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = self.FakeBackend()
            cache = FragmentCache(
                root / "cache", backend.fingerprint(), backend.file_suffix
            )
            with mock.patch.object(tts_cn, "audio_is_decodable", return_value=True):
                files, stats = asyncio.run(
                    tts_cn.synth_all(
                        [("female_1", "相同正文。"), ("female_1", "相同正文。")],
                        root / "generated",
                        backend=backend,
                        style="conversation",
                        cache=cache,
                    )
                )
            self.assertEqual(len(files), 2)
            self.assertEqual(backend.calls, 1)
            self.assertEqual(stats["provider_calls"], 1)
            self.assertEqual(stats["cached"], 1)

    def test_non_retryable_provider_error_stops_after_one_call(self) -> None:
        class NonRetryableBackend(self.FakeBackend):
            async def synthesize(self, request, output):
                self.calls += 1
                raise TTSBackendError("账户余额不足", retryable=False)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = NonRetryableBackend()
            cache = FragmentCache(
                root / "cache", backend.fingerprint(), backend.file_suffix
            )
            _, stats = asyncio.run(
                tts_cn.synth_all(
                    [("female_1", "测试正文。")],
                    root / "generated",
                    backend=backend,
                    style="conversation",
                    cache=cache,
                )
            )

        self.assertEqual(backend.calls, 1)
        self.assertEqual(stats["provider_calls"], 1)
        self.assertEqual(stats["failed"], 1)

    def test_corrupt_cached_fragment_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = self.FakeBackend()
            cache = FragmentCache(
                root / "cache", backend.fingerprint(), backend.file_suffix
            )
            request = tts_cn.SynthesisRequest(
                text="测试正文。", voice_slot="female_1", style="conversation"
            )
            corrupt = cache.path_for(request)
            corrupt.parent.mkdir(parents=True)
            corrupt.write_bytes(b"corrupt")

            with mock.patch.object(
                tts_cn,
                "audio_is_decodable",
                side_effect=lambda path: path.read_bytes() == b"valid-audio",
            ):
                _, stats = asyncio.run(
                    tts_cn.synth_all(
                        [("female_1", "测试正文。")],
                        root / "generated",
                        backend=backend,
                        style="conversation",
                        cache=cache,
                    )
                )

            self.assertEqual(backend.calls, 1)
            self.assertEqual(stats["cached"], 0)
            self.assertEqual(corrupt.read_bytes(), b"valid-audio")


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
            def from_file(path):
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
        async def fake_synth_all(segments, tmp, *, backend, style, cache):
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
        async def fake_synth_all(segments, tmp, *, backend, style, cache):
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
