#!/usr/bin/env python3
"""Tests for one-URL YouTube podcast preparation."""

from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
FIXTURE = ROOT / "tests/fixtures/podcast-sample.en.vtt"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "podcast_cn", ROOT / "scripts/podcast_cn.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 scripts/podcast_cn.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


podcast_cn = load_module()


class VttParserTests(unittest.TestCase):
    def test_parses_and_deduplicates_rolling_captions(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")

        segments = podcast_cn.parse_vtt(text)

        self.assertEqual(
            [segment.text for segment in segments],
            [
                "Hello & welcome to the show.",
                "Today we talk health.",
                "Exactly.",
            ],
        )
        self.assertEqual([segment.id for segment in segments], [
            "seg-000001",
            "seg-000002",
            "seg-000003",
        ])
        self.assertEqual(segments[0].start_ms, 0)
        self.assertEqual(segments[0].end_ms, 4000)
        self.assertEqual(segments[-1].start_ms, 7500)

    def test_segment_ids_are_stable(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        first = podcast_cn.parse_vtt(text)
        second = podcast_cn.parse_vtt(text)

        self.assertEqual(first, second)


class CaptionSelectionTests(unittest.TestCase):
    def test_prefers_exact_manual_english_over_automatic(self) -> None:
        info = {
            "subtitles": {"en": [{"ext": "vtt"}]},
            "automatic_captions": {"en": [{"ext": "vtt"}]},
        }
        self.assertEqual(podcast_cn.select_english_track(info), ("en", False))

    def test_prefers_regional_manual_english_over_automatic(self) -> None:
        info = {
            "subtitles": {"en-US": [{"ext": "vtt"}]},
            "automatic_captions": {"en": [{"ext": "vtt"}]},
        }
        self.assertEqual(podcast_cn.select_english_track(info), ("en-US", False))

    def test_falls_back_to_automatic_english(self) -> None:
        info = {
            "subtitles": {"es": [{"ext": "vtt"}]},
            "automatic_captions": {"en": [{"ext": "vtt"}]},
        }
        self.assertEqual(podcast_cn.select_english_track(info), ("en", True))

    def test_rejects_video_without_english_captions(self) -> None:
        info = {
            "subtitles": {"es": [{"ext": "vtt"}]},
            "automatic_captions": {"fr": [{"ext": "vtt"}]},
        }
        with self.assertRaises(podcast_cn.CaptionUnavailableError):
            podcast_cn.select_english_track(info)


class YoutubeCommandTests(unittest.TestCase):
    def test_fetches_metadata_for_exact_url_without_search_or_media(self) -> None:
        calls = []
        expected_info = {"id": "abc123", "title": "Test episode"}

        def fake_runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, json.dumps(expected_info), "")

        url = "https://www.youtube.com/watch?v=abc123"
        info = podcast_cn.fetch_video_info(url, runner=fake_runner)

        self.assertEqual(info, expected_info)
        args, kwargs = calls[0]
        self.assertEqual(args[-1], url)
        self.assertIn("--dump-single-json", args)
        self.assertIn("--skip-download", args)
        self.assertIn("--no-playlist", args)
        self.assertFalse(any("ytsearch" in arg for arg in args))
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])

    def test_derives_small_video_metadata(self) -> None:
        metadata = podcast_cn.video_metadata_from_info(
            {
                "id": "abc123",
                "title": "Test episode",
                "webpage_url": "https://www.youtube.com/watch?v=abc123",
                "channel": "Test channel",
                "duration": 123.4,
                "subtitles": {"en": [{"ext": "vtt"}]},
            }
        )

        self.assertEqual(metadata.video_id, "abc123")
        self.assertEqual(metadata.source_title, "Test episode")
        self.assertEqual(metadata.channel, "Test channel")
        self.assertEqual(metadata.duration_seconds, 123.4)

    def test_downloads_manual_subtitles_without_downloading_media(self) -> None:
        calls = []
        info = {"subtitles": {"en": [{"ext": "vtt"}]}}

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)

            def fake_runner(args, **kwargs):
                calls.append((args, kwargs))
                (job_dir / "subtitles.en.vtt").write_text(
                    FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
                )
                return subprocess.CompletedProcess(args, 0, "", "")

            url = "https://www.youtube.com/watch?v=abc123"
            result = podcast_cn.download_english_subtitles(
                url, job_dir, info, runner=fake_runner
            )

            self.assertEqual(result, job_dir / "subtitles.en.vtt")
            args, kwargs = calls[0]
            self.assertEqual(args[-1], url)
            self.assertIn("--write-subs", args)
            self.assertNotIn("--write-auto-subs", args)
            self.assertIn("--skip-download", args)
            self.assertIn("--sub-format", args)
            self.assertFalse(any("ytsearch" in arg for arg in args))
            self.assertTrue(kwargs["check"])

    def test_downloads_automatic_subtitles_only_as_fallback(self) -> None:
        calls = []
        info = {"automatic_captions": {"en": [{"ext": "vtt"}]}}

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)

            def fake_runner(args, **kwargs):
                calls.append(args)
                (job_dir / "subtitles.en.vtt").write_text(
                    FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
                )
                return subprocess.CompletedProcess(args, 0, "", "")

            podcast_cn.download_english_subtitles(
                "https://www.youtube.com/watch?v=abc123",
                job_dir,
                info,
                runner=fake_runner,
            )

            self.assertIn("--write-auto-subs", calls[0])
            self.assertNotIn("--write-subs", calls[0])


class TranslationStructureTests(unittest.TestCase):
    def test_chunks_segments_without_reordering_or_dropping_them(self) -> None:
        segments = [
            podcast_cn.SubtitleSegment("seg-000001", 0, 1000, "abc"),
            podcast_cn.SubtitleSegment("seg-000002", 1000, 2000, "de"),
            podcast_cn.SubtitleSegment("seg-000003", 2000, 3000, "fgh"),
        ]

        chunks = podcast_cn.chunk_segments(segments, max_chars=5)

        self.assertEqual([[segment.id for segment in chunk] for chunk in chunks], [
            ["seg-000001", "seg-000002"],
            ["seg-000003"],
        ])

    def test_keeps_one_oversized_segment_as_its_own_chunk(self) -> None:
        segment = podcast_cn.SubtitleSegment("seg-000001", 0, 1000, "abcdef")
        self.assertEqual(
            podcast_cn.chunk_segments([segment], max_chars=3),
            [[segment]],
        )

    def test_validates_complete_ordered_translation_payload(self) -> None:
        payload = {
            "translations": [
                {"id": "seg-000001", "text": "你好"},
                {"id": "seg-000002", "text": "世界"},
            ]
        }
        self.assertEqual(
            podcast_cn.validate_translation_payload(
                payload, ["seg-000001", "seg-000002"]
            ),
            {"seg-000001": "你好", "seg-000002": "世界"},
        )

    def test_rejects_incomplete_or_malformed_translation_payloads(self) -> None:
        invalid_cases = [
            None,
            {"translations": "not-a-list"},
            {"translations": [{"id": "seg-000001", "text": "你好"}]},
            {
                "translations": [
                    {"id": "seg-000001", "text": "你好"},
                    {"id": "seg-000003", "text": "额外"},
                ]
            },
            {
                "translations": [
                    {"id": "seg-000001", "text": "你好"},
                    {"id": "seg-000001", "text": "重复"},
                ]
            },
            {
                "translations": [
                    {"id": "seg-000001", "text": "你好"},
                    {"id": "seg-000002", "text": "   "},
                ]
            },
        ]

        for payload in invalid_cases:
            with self.subTest(payload=payload):
                with self.assertRaises(podcast_cn.TranslationValidationError):
                    podcast_cn.validate_translation_payload(
                        payload, ["seg-000001", "seg-000002"]
                    )


class ProviderConfigTests(unittest.TestCase):
    def test_resolves_deepseek_provider_defaults(self) -> None:
        config = podcast_cn.get_provider_config("deepseek")

        self.assertEqual(config.api_key_env, "DEEPSEEK_API_KEY")
        self.assertEqual(config.model_env, "DEEPSEEK_MODEL")
        self.assertEqual(config.base_url_env, "DEEPSEEK_BASE_URL")
        self.assertEqual(config.default_model, "deepseek-chat")
        self.assertEqual(config.default_base_url, "https://api.deepseek.com")
        self.assertEqual(config.keychain_service, "podcast-cn-deepseek")

    def test_resolves_qwen_provider_defaults(self) -> None:
        config = podcast_cn.get_provider_config("qwen")

        self.assertEqual(config.api_key_env, "DASHSCOPE_API_KEY")
        self.assertEqual(config.model_env, "QWEN_MODEL")
        self.assertEqual(config.base_url_env, "QWEN_BASE_URL")
        self.assertEqual(config.default_model, "qwen-plus")
        self.assertEqual(
            config.default_base_url,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(config.keychain_service, "podcast-cn-qwen")

    def test_rejects_unsupported_provider(self) -> None:
        with self.assertRaisesRegex(
            podcast_cn.TranslationConfigurationError, "kimi"
        ):
            podcast_cn.get_provider_config("kimi")


class TranslationClientTests(unittest.TestCase):
    @staticmethod
    def response_payload() -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "translations": [
                                    {"id": "seg-000001", "text": "你好"},
                                    {"id": "seg-000002", "text": "世界"},
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    def test_qwen_posts_compatible_structured_request(self) -> None:
        requests: list[tuple[urllib.request.Request, float]] = []
        response_payload = self.response_payload()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps(response_payload, ensure_ascii=False).encode("utf-8")

        def fake_opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

        client = podcast_cn.OpenAICompatibleTranslationClient(
            "qwen", api_key="qwen-test-secret", opener=fake_opener
        )
        segments = [
            podcast_cn.SubtitleSegment("seg-000001", 0, 1000, "Hello"),
            podcast_cn.SubtitleSegment("seg-000002", 1000, 2000, "world"),
        ]

        translated = client.translate(segments)

        self.assertEqual(translated, {"seg-000001": "你好", "seg-000002": "世界"})
        request, timeout = requests[0]
        self.assertEqual(
            request.full_url,
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        self.assertEqual(timeout, 120)
        self.assertEqual(request.get_header("Authorization"), "Bearer qwen-test-secret")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "qwen-plus")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("qwen-test-secret", request.data.decode("utf-8"))

    def test_qwen_reads_model_and_base_url_from_environment(self) -> None:
        environment = {
            "DASHSCOPE_API_KEY": "env-test-key",
            "QWEN_MODEL": "qwen-max",
            "QWEN_BASE_URL": "https://example.invalid/openai/v1/",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            client = podcast_cn.OpenAICompatibleTranslationClient("qwen")

        self.assertEqual(client.model, "qwen-max")
        self.assertEqual(client.base_url, "https://example.invalid/openai/v1")
        self.assertEqual(
            client.translation_identity,
            "qwen|qwen-max|https://example.invalid/openai/v1",
        )
        self.assertNotIn("env-test-key", client.translation_identity)

    def test_identity_changes_with_provider_model_and_endpoint(self) -> None:
        deepseek = podcast_cn.OpenAICompatibleTranslationClient(
            "deepseek", api_key="test-key"
        )
        qwen = podcast_cn.OpenAICompatibleTranslationClient(
            "qwen", api_key="test-key"
        )
        qwen_model = podcast_cn.OpenAICompatibleTranslationClient(
            "qwen", api_key="test-key", model="qwen-max"
        )
        qwen_endpoint = podcast_cn.OpenAICompatibleTranslationClient(
            "qwen", api_key="test-key", base_url="https://example.invalid/v1"
        )

        identities = {
            deepseek.translation_identity,
            qwen.translation_identity,
            qwen_model.translation_identity,
            qwen_endpoint.translation_identity,
        }
        self.assertEqual(len(identities), 4)
        for identity in identities:
            self.assertNotIn("test-key", identity)

    def test_qwen_request_failure_does_not_expose_key(self) -> None:
        secret = "qwen-secret-that-must-not-leak"

        def failing_opener(request, timeout):
            raise RuntimeError(f"network error involving {secret}")

        client = podcast_cn.OpenAICompatibleTranslationClient(
            "qwen", api_key=secret, opener=failing_opener
        )
        segment = podcast_cn.SubtitleSegment("seg-000001", 0, 1000, "Hello")

        with self.assertRaisesRegex(
            podcast_cn.PodcastPipelineError, "通义千问"
        ) as raised:
            client.translate([segment])

        self.assertNotIn(secret, str(raised.exception))


class DeepSeekClientTests(unittest.TestCase):
    def test_requires_api_key_without_exposing_configuration(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                podcast_cn.DeepSeekConfigurationError, "DEEPSEEK_API_KEY"
            ):
                podcast_cn.DeepSeekClient()

    def test_posts_structured_translation_and_validates_response(self) -> None:
        requests: list[tuple[urllib.request.Request, float]] = []
        response_payload = {
            "choices": [
                {
                    "message": {
                        "content": "```json\n"
                        + json.dumps(
                            {
                                "translations": [
                                    {"id": "seg-000001", "text": "你好"},
                                    {"id": "seg-000002", "text": "世界"},
                                ]
                            },
                            ensure_ascii=False,
                        )
                        + "\n```"
                    }
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps(response_payload, ensure_ascii=False).encode("utf-8")

        def fake_opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

        segments = [
            podcast_cn.SubtitleSegment("seg-000001", 0, 1000, "Hello"),
            podcast_cn.SubtitleSegment("seg-000002", 1000, 2000, "world"),
        ]
        client = podcast_cn.DeepSeekClient(
            api_key="test-secret-key", opener=fake_opener
        )

        translated = client.translate(segments)

        self.assertEqual(translated, {"seg-000001": "你好", "seg-000002": "世界"})
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(timeout, 120)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        user_payload = json.loads(body["messages"][1]["content"])
        self.assertEqual(
            [set(item) for item in user_payload],
            [{"id", "text"}, {"id", "text"}],
        )
        self.assertNotIn("test-secret-key", request.data.decode("utf-8"))

    def test_reads_model_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"DEEPSEEK_MODEL": "deepseek-reasoner"}):
            client = podcast_cn.DeepSeekClient(api_key="test-key")
        self.assertEqual(client.model, "deepseek-reasoner")

    def test_request_failure_does_not_expose_key(self) -> None:
        secret = "secret-that-must-not-leak"

        def failing_opener(request, timeout):
            raise RuntimeError(f"network error involving {secret}")

        client = podcast_cn.DeepSeekClient(api_key=secret, opener=failing_opener)
        segment = podcast_cn.SubtitleSegment("seg-000001", 0, 1000, "Hello")

        with self.assertRaises(podcast_cn.PodcastPipelineError) as raised:
            client.translate([segment])

        self.assertNotIn(secret, str(raised.exception))


class TranslationResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segments = [
            podcast_cn.SubtitleSegment("seg-000001", 0, 1000, "aaaa"),
            podcast_cn.SubtitleSegment("seg-000002", 1000, 2000, "bbbb"),
            podcast_cn.SubtitleSegment("seg-000003", 2000, 3000, "cccc"),
        ]

    @staticmethod
    def translate_chunk(segments):
        return {segment.id: f"中文-{segment.id}" for segment in segments}

    def test_resumes_after_interruption_and_calls_only_missing_chunks(self) -> None:
        first_calls = []

        def interrupted_translator(segments):
            first_calls.append([segment.id for segment in segments])
            if len(first_calls) == 2:
                raise RuntimeError("simulated interruption")
            return self.translate_chunk(segments)

        with tempfile.TemporaryDirectory() as tmp:
            chunk_dir = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                podcast_cn.translate_with_resume(
                    self.segments,
                    chunk_dir,
                    interrupted_translator,
                    max_chars=4,
                )

            self.assertTrue((chunk_dir / "chunk-0001.json").is_file())
            self.assertFalse((chunk_dir / "chunk-0002.json").exists())

            resumed_calls = []

            def resumed_translator(segments):
                resumed_calls.append([segment.id for segment in segments])
                return self.translate_chunk(segments)

            result = podcast_cn.translate_with_resume(
                self.segments,
                chunk_dir,
                resumed_translator,
                max_chars=4,
            )

            self.assertEqual(resumed_calls, [["seg-000002"], ["seg-000003"]])
            self.assertEqual(list(result), [segment.id for segment in self.segments])
            self.assertFalse(list(chunk_dir.glob("*.tmp")))

    def test_reuses_only_matching_translation_identity(self) -> None:
        changed_identities = [
            "qwen|qwen-plus|https://dashscope.aliyuncs.com/compatible-mode/v1",
            "deepseek|deepseek-reasoner|https://api.deepseek.com",
            "deepseek|deepseek-chat|https://proxy.example.invalid/v1",
        ]
        original_identity = "deepseek|deepseek-chat|https://api.deepseek.com"

        for changed_identity in changed_identities:
            with self.subTest(changed_identity=changed_identity):
                with tempfile.TemporaryDirectory() as tmp:
                    chunk_dir = Path(tmp)
                    podcast_cn.translate_with_resume(
                        self.segments,
                        chunk_dir,
                        self.translate_chunk,
                        max_chars=4,
                        translation_identity=original_identity,
                    )

                    podcast_cn.translate_with_resume(
                        self.segments,
                        chunk_dir,
                        lambda segments: (_ for _ in ()).throw(
                            AssertionError("相同模型身份应复用缓存")
                        ),
                        max_chars=4,
                        translation_identity=original_identity,
                    )

                    calls = []

                    def changed_translator(segments):
                        calls.append([segment.id for segment in segments])
                        return self.translate_chunk(segments)

                    podcast_cn.translate_with_resume(
                        self.segments,
                        chunk_dir,
                        changed_translator,
                        max_chars=4,
                        translation_identity=changed_identity,
                    )

                    self.assertEqual(
                        calls,
                        [["seg-000001"], ["seg-000002"], ["seg-000003"]],
                    )
                    cached = json.loads(
                        (chunk_dir / "chunk-0001.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(cached["translation_identity"], changed_identity)
                    self.assertNotIn("API_KEY", json.dumps(cached))

    def test_retranslates_only_cache_whose_source_hash_is_wrong(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chunk_dir = Path(tmp)
            podcast_cn.translate_with_resume(
                self.segments,
                chunk_dir,
                self.translate_chunk,
                max_chars=4,
            )
            cache_path = chunk_dir / "chunk-0001.json"
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["source_hash"] = "wrong"
            cache_path.write_text(json.dumps(cached), encoding="utf-8")
            calls = []

            def recording_translator(segments):
                calls.append([segment.id for segment in segments])
                return self.translate_chunk(segments)

            podcast_cn.translate_with_resume(
                self.segments,
                chunk_dir,
                recording_translator,
                max_chars=4,
            )

            self.assertEqual(calls, [["seg-000001"]])
            repaired = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertNotEqual(repaired["source_hash"], "wrong")

    def test_does_not_write_captured_api_secret_to_chunk_files(self) -> None:
        secret = "deepseek-secret-never-persist"

        def translator_with_secret_in_scope(segments):
            self.assertTrue(secret)
            return self.translate_chunk(segments)

        with tempfile.TemporaryDirectory() as tmp:
            chunk_dir = Path(tmp)
            podcast_cn.translate_with_resume(
                self.segments,
                chunk_dir,
                translator_with_secret_in_scope,
                max_chars=4,
            )

            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(chunk_dir.glob("*.json"))
            )
            self.assertNotIn(secret, persisted)

    def test_rejects_incomplete_translator_result_without_caching_it(self) -> None:
        def incomplete_translator(segments):
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            chunk_dir = Path(tmp)
            with self.assertRaises(podcast_cn.TranslationValidationError):
                podcast_cn.translate_with_resume(
                    self.segments[:2],
                    chunk_dir,
                    incomplete_translator,
                    max_chars=8,
                )

            self.assertFalse(list(chunk_dir.glob("*.json")))

    def test_recovers_incomplete_chunk_by_translating_smaller_batches(self) -> None:
        calls = []

        def size_sensitive_translator(segments):
            calls.append([segment.id for segment in segments])
            if len(segments) > 1:
                return {segments[0].id: "只返回了第一段"}
            return self.translate_chunk(segments)

        with tempfile.TemporaryDirectory() as tmp:
            chunk_dir = Path(tmp)
            result = podcast_cn.translate_with_resume(
                self.segments[:2],
                chunk_dir,
                size_sensitive_translator,
                max_chars=8,
            )

            self.assertEqual(
                calls,
                [
                    ["seg-000001", "seg-000002"],
                    ["seg-000001"],
                    ["seg-000002"],
                ],
            )
            self.assertEqual(
                result,
                {
                    "seg-000001": "中文-seg-000001",
                    "seg-000002": "中文-seg-000002",
                },
            )
            self.assertTrue((chunk_dir / "chunk-0001.json").is_file())


class EpisodePreparationTests(unittest.TestCase):
    def test_prepares_persistent_job_and_reuses_completed_artifacts(self) -> None:
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        url = "https://www.youtube.com/watch?v=abc123"
        info = {
            "id": "abc123",
            "title": "Test episode",
            "webpage_url": url,
            "channel": "Test channel",
            "duration": 9.0,
            "subtitles": {"en": [{"ext": "vtt"}]},
        }
        runner_calls = []

        def fake_runner(args, **kwargs):
            runner_calls.append(args)
            if "--dump-single-json" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps(info), "")
            output_template = Path(args[args.index("--output") + 1])
            (output_template.parent / "subtitles.en.vtt").write_text(
                fixture_text, encoding="utf-8"
            )
            return subprocess.CompletedProcess(args, 0, "", "")

        translation_calls = []

        def translator(segments):
            translation_calls.append([segment.id for segment in segments])
            return {segment.id: f"中文-{segment.text}" for segment in segments}

        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            prepared = podcast_cn.prepare_episode(
                url,
                output_root,
                runner=fake_runner,
                translator=translator,
                diarize="off",
            )
            job_dir = output_root / ".podcast-jobs/abc123"

            self.assertEqual(prepared.video_id, "abc123")
            self.assertEqual(prepared.source_title, "Test episode")
            self.assertEqual(prepared.transcript_path, job_dir / "transcript.zh.md")
            self.assertEqual(prepared.state_path, job_dir / "state.json")
            self.assertTrue((job_dir / "metadata.json").is_file())
            self.assertTrue((job_dir / "subtitles.en.vtt").is_file())
            self.assertTrue((job_dir / "subtitles.en.jsonl").is_file())
            self.assertTrue((job_dir / "translation-chunks/chunk-0001.json").is_file())
            self.assertTrue(prepared.transcript_path.is_file())

            state = json.loads(prepared.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "transcript_ready")
            transcript = prepared.transcript_path.read_text(encoding="utf-8")
            for segment in podcast_cn.parse_vtt(fixture_text):
                self.assertIn(f"<!-- {segment.id} -->", transcript)
                self.assertIn(f"中文-{segment.text}", transcript)

            jsonl_records = [
                json.loads(line)
                for line in (job_dir / "subtitles.en.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [record["id"] for record in jsonl_records],
                ["seg-000001", "seg-000002", "seg-000003"],
            )

            subtitle_downloads_before = sum(
                "--write-subs" in args or "--write-auto-subs" in args
                for args in runner_calls
            )
            translation_count_before = len(translation_calls)

            prepared_again = podcast_cn.prepare_episode(
                url,
                output_root,
                runner=fake_runner,
                translator=lambda segments: (_ for _ in ()).throw(
                    AssertionError("已完成任务不应重新翻译")
                ),
                diarize="off",
            )

            subtitle_downloads_after = sum(
                "--write-subs" in args or "--write-auto-subs" in args
                for args in runner_calls
            )
            self.assertEqual(prepared_again, prepared)
            self.assertEqual(subtitle_downloads_after, subtitle_downloads_before)
            self.assertEqual(len(translation_calls), translation_count_before)

    def test_job_state_never_contains_deepseek_key(self) -> None:
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        url = "https://www.youtube.com/watch?v=abc123"
        secret = "deepseek-key-must-stay-in-memory"
        info = {
            "id": "abc123",
            "title": "Test episode",
            "webpage_url": url,
            "automatic_captions": {"en": [{"ext": "vtt"}]},
        }

        def fake_runner(args, **kwargs):
            if "--dump-single-json" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps(info), "")
            output_template = Path(args[args.index("--output") + 1])
            (output_template.parent / "subtitles.en.vtt").write_text(
                fixture_text, encoding="utf-8"
            )
            return subprocess.CompletedProcess(args, 0, "", "")

        def translator(segments):
            self.assertTrue(secret)
            return {segment.id: f"译文-{segment.id}" for segment in segments}

        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            podcast_cn.prepare_episode(
                url,
                output_root,
                runner=fake_runner,
                translator=translator,
                diarize="off",
            )
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (output_root / ".podcast-jobs/abc123").rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, persisted)

    def test_retranslates_completed_transcript_when_identity_changes(self) -> None:
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        url = "https://www.youtube.com/watch?v=abc123"
        info = {
            "id": "abc123",
            "title": "Test episode",
            "webpage_url": url,
            "subtitles": {"en": [{"ext": "vtt"}]},
        }

        def fake_runner(args, **kwargs):
            if "--dump-single-json" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps(info), "")
            output_template = Path(args[args.index("--output") + 1])
            (output_template.parent / "subtitles.en.vtt").write_text(
                fixture_text, encoding="utf-8"
            )
            return subprocess.CompletedProcess(args, 0, "", "")

        calls = []

        def translator(label):
            def translate(segments):
                calls.append(label)
                return {segment.id: f"{label}-{segment.id}" for segment in segments}

            return translate

        deepseek_identity = "deepseek|deepseek-chat|https://api.deepseek.com"
        qwen_identity = (
            "qwen|qwen-plus|"
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            podcast_cn.prepare_episode(
                url,
                output_root,
                runner=fake_runner,
                translator=translator("deepseek"),
                translation_identity=deepseek_identity,
                diarize="off",
            )
            podcast_cn.prepare_episode(
                url,
                output_root,
                runner=fake_runner,
                translator=lambda segments: (_ for _ in ()).throw(
                    AssertionError("相同身份应复用完整逐字稿")
                ),
                translation_identity=deepseek_identity,
                diarize="off",
            )
            podcast_cn.prepare_episode(
                url,
                output_root,
                runner=fake_runner,
                translator=translator("qwen"),
                translation_identity=qwen_identity,
                diarize="off",
            )

            self.assertEqual(calls, ["deepseek", "qwen"])
            job_dir = output_root / ".podcast-jobs/abc123"
            transcript = (job_dir / "transcript.zh.md").read_text(encoding="utf-8")
            self.assertIn("qwen-seg-000001", transcript)
            self.assertNotIn("deepseek-seg-000001", transcript)
            translation_metadata = json.loads(
                (job_dir / "translation-metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                translation_metadata["translation_identity"], qwen_identity
            )
            self.assertEqual(translation_metadata["provider"], "qwen")
            self.assertEqual(translation_metadata["model"], "qwen-plus")
            self.assertEqual(
                translation_metadata["base_url"],
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            )


class DiarizationPipelineTests(unittest.TestCase):
    def test_sherpa_backend_prints_chunk_progress_in_chinese(self) -> None:
        backend = podcast_cn.SherpaDiarizationBackend()
        backend._runtime = mock.Mock()

        def fake_run(*args: object, **kwargs: object) -> list[object]:
            callback = kwargs.get("progress_callback")
            self.assertTrue(callable(callback))
            callback(67)
            return []

        stderr = io.StringIO()
        with mock.patch.object(podcast_cn, "run_sherpa_backend", side_effect=fake_run):
            with contextlib.redirect_stderr(stderr):
                backend.diarize(Path("analysis.wav"), podcast_cn.SpeakerBounds())

        self.assertIn("声纹进度:67%", stderr.getvalue())

    URL = "https://www.youtube.com/watch?v=abc123"

    class FakeBackend:
        name = "fake-diarization"
        model_fingerprint = {
            "backend": "fake-diarization",
            "runtime": "1",
            "models": {"test": "a" * 64},
        }

        def __init__(self, error: BaseException | None = None) -> None:
            self.error = error
            self.calls = 0

        def diarize(self, audio_path, speaker_bounds):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return [
                podcast_cn.DiarizationTurn(0, 4000, "SPEAKER_00"),
                podcast_cn.DiarizationTurn(4000, 7000, "SPEAKER_01"),
                podcast_cn.DiarizationTurn(7000, 9000, "SPEAKER_00"),
            ]

    def make_runner(self, calls):
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        info = {
            "id": "abc123",
            "title": "Test episode",
            "webpage_url": self.URL,
            "channel": "Test channel",
            "duration": 9.0,
            "subtitles": {"en": [{"ext": "vtt"}]},
        }

        def fake_runner(args, **kwargs):
            calls.append(args)
            if "--dump-single-json" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps(info), "")
            if "--write-subs" in args or "--write-auto-subs" in args:
                output_template = Path(args[args.index("--output") + 1])
                (output_template.parent / "subtitles.en.vtt").write_text(
                    fixture_text, encoding="utf-8"
                )
            elif args[0] == "yt-dlp" and "bestaudio/best" in args:
                output_template = Path(args[args.index("--output") + 1])
                (output_template.parent / "source.webm").write_bytes(b"source")
            elif args[0] == "ffmpeg":
                Path(args[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(args, 0, "", "")

        return fake_runner

    @staticmethod
    def translator(segments):
        return {segment.id: f"中文-{segment.text}" for segment in segments}

    def test_success_writes_one_to_one_speakers(self) -> None:
        calls = []
        backend = self.FakeBackend()
        with tempfile.TemporaryDirectory() as tmp:
            prepared = podcast_cn.prepare_episode(
                self.URL,
                Path(tmp),
                runner=self.make_runner(calls),
                translator=self.translator,
                diarize="required",
                speaker_bounds=podcast_cn.SpeakerBounds(exact=2),
                diarization_backend=backend,
            )

            labels = prepared.speakers_path.read_text(
                encoding="utf-8"
            ).splitlines()
            transcript_ids = re.findall(
                r"<!-- (seg-\d+) -->",
                prepared.transcript_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(len(labels), len(transcript_ids))
            self.assertEqual(len(labels), 3)
            self.assertEqual(prepared.diarization_report.status, "success")
            self.assertEqual(prepared.diarization_report.detected_speakers, 2)
            self.assertEqual(prepared.diarization_report.temporary_audio, "deleted")
            self.assertTrue(prepared.speaker_map_path.is_file())
            self.assertFalse(
                list((Path(tmp) / ".podcast-jobs/abc123").glob(".diarization-media-*"))
            )

    def test_second_run_uses_cache_without_media_backend_or_translation(self) -> None:
        calls = []
        backend = self.FakeBackend()
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            runner = self.make_runner(calls)
            podcast_cn.prepare_episode(
                self.URL,
                output_root,
                runner=runner,
                translator=self.translator,
                diarize="required",
                speaker_bounds=podcast_cn.SpeakerBounds(exact=2),
                diarization_backend=backend,
            )
            media_downloads_before = sum("bestaudio/best" in call for call in calls)
            no_backend = self.FakeBackend(AssertionError("不应调用 backend"))

            again = podcast_cn.prepare_episode(
                self.URL,
                output_root,
                runner=runner,
                translator=lambda segments: (_ for _ in ()).throw(
                    AssertionError("不应重新翻译")
                ),
                diarize="required",
                speaker_bounds=podcast_cn.SpeakerBounds(exact=2),
                diarization_backend=no_backend,
            )

            self.assertEqual(again.diarization_report.status, "cache_hit")
            self.assertEqual(no_backend.calls, 0)
            self.assertEqual(
                sum("bestaudio/best" in call for call in calls),
                media_downloads_before,
            )

    def test_auto_falls_back_as_one_whole_episode(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            prepared = podcast_cn.prepare_episode(
                self.URL,
                Path(tmp),
                runner=self.make_runner(calls),
                translator=self.translator,
                diarize="auto",
                diarization_backend=self.FakeBackend(RuntimeError("model failed")),
            )

            self.assertIsNone(prepared.speakers_path)
            self.assertEqual(prepared.diarization_report.status, "fallback")
            self.assertIn("model failed", prepared.diarization_report.warning)
            subtitle_records = [
                json.loads(line)
                for line in (
                    Path(tmp) / ".podcast-jobs/abc123/subtitles.en.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(all(record["speaker_id"] is None for record in subtitle_records))

    def test_required_stops_before_translation(self) -> None:
        calls = []
        translation_mock = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(podcast_cn.DiarizationBackendError):
                podcast_cn.prepare_episode(
                    self.URL,
                    Path(tmp),
                    runner=self.make_runner(calls),
                    translator=translation_mock,
                    diarize="required",
                    diarization_backend=self.FakeBackend(RuntimeError("failed")),
                )

        translation_mock.assert_not_called()

    def test_auto_does_not_fallback_when_cleanup_cannot_be_verified(self) -> None:
        calls = []
        translation_mock = mock.Mock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                podcast_cn,
                "with_temporary_audio",
                side_effect=podcast_cn.TemporaryAudioCleanupError(
                    "临时原音频删除失败"
                ),
            ),
            self.assertRaises(podcast_cn.DiarizationBackendError),
        ):
            podcast_cn.prepare_episode(
                self.URL,
                Path(tmp),
                runner=self.make_runner(calls),
                translator=translation_mock,
                diarize="auto",
                diarization_backend=self.FakeBackend(),
            )

        translation_mock.assert_not_called()

    def test_off_never_downloads_media_or_calls_backend(self) -> None:
        calls = []
        backend = self.FakeBackend(AssertionError("不应调用 backend"))
        with tempfile.TemporaryDirectory() as tmp:
            prepared = podcast_cn.prepare_episode(
                self.URL,
                Path(tmp),
                runner=self.make_runner(calls),
                translator=self.translator,
                diarize="off",
                diarization_backend=backend,
            )

        self.assertEqual(prepared.diarization_report.status, "off")
        self.assertFalse(any("bestaudio/best" in call for call in calls))
        self.assertEqual(backend.calls, 0)


class SpeakerBoundsResolutionTests(unittest.TestCase):
    def test_resolves_defaults_and_exact_count(self) -> None:
        self.assertEqual(
            podcast_cn.resolve_speaker_bounds(None, None, None),
            podcast_cn.SpeakerBounds(),
        )
        self.assertEqual(
            podcast_cn.resolve_speaker_bounds(2, None, None),
            podcast_cn.SpeakerBounds(exact=2),
        )

    def test_rejects_exact_count_conflict_and_invalid_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能同时"):
            podcast_cn.resolve_speaker_bounds(2, 1, None)
        with self.assertRaisesRegex(ValueError, "speaker 范围"):
            podcast_cn.resolve_speaker_bounds(None, 4, 2)


class CommandLineTests(unittest.TestCase):
    def test_prints_two_line_result_for_successful_url(self) -> None:
        prepared = podcast_cn.PreparedEpisode(
            video_id="abc123",
            source_title="Test episode",
            transcript_path=Path("/tmp/transcript.zh.md"),
            state_path=Path("/tmp/state.json"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(
                podcast_cn, "prepare_episode", return_value=prepared
            ) as prepare_mock,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = podcast_cn.main(
                [
                    "https://www.youtube.com/watch?v=abc123",
                    "--output-root",
                    "/tmp/audio",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stdout.getvalue().splitlines(),
            ["英文标题:Test episode", "中文字幕:/tmp/transcript.zh.md"],
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(prepare_mock.call_args.args[1], Path("/tmp/audio"))
        self.assertEqual(prepare_mock.call_args.kwargs["provider"], "deepseek")
        self.assertEqual(prepare_mock.call_args.kwargs["diarize"], "auto")
        self.assertEqual(
            prepare_mock.call_args.kwargs["speaker_bounds"],
            podcast_cn.SpeakerBounds(),
        )

    def test_cli_passes_required_exact_speaker_count(self) -> None:
        prepared = podcast_cn.PreparedEpisode(
            video_id="abc123",
            source_title="Test episode",
            transcript_path=Path("/tmp/transcript.zh.md"),
            state_path=Path("/tmp/state.json"),
        )
        with (
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(
                podcast_cn, "prepare_episode", return_value=prepared
            ) as prepare_mock,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = podcast_cn.main(
                [
                    "https://www.youtube.com/watch?v=abc123",
                    "--diarize",
                    "required",
                    "--speaker-count",
                    "2",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(prepare_mock.call_args.kwargs["diarize"], "required")
        self.assertEqual(
            prepare_mock.call_args.kwargs["speaker_bounds"],
            podcast_cn.SpeakerBounds(exact=2),
        )

    def test_cli_passes_custom_speaker_range(self) -> None:
        prepared = podcast_cn.PreparedEpisode(
            video_id="abc123",
            source_title="Test episode",
            transcript_path=Path("/tmp/transcript.zh.md"),
            state_path=Path("/tmp/state.json"),
        )
        with (
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(
                podcast_cn, "prepare_episode", return_value=prepared
            ) as prepare_mock,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            podcast_cn.main(
                [
                    "https://www.youtube.com/watch?v=abc123",
                    "--diarize",
                    "off",
                    "--min-speakers",
                    "2",
                    "--max-speakers",
                    "4",
                ]
            )

        self.assertEqual(prepare_mock.call_args.kwargs["diarize"], "off")
        self.assertEqual(
            prepare_mock.call_args.kwargs["speaker_bounds"],
            podcast_cn.SpeakerBounds(minimum=2, maximum=4),
        )

    def test_cli_rejects_conflicting_or_invalid_speaker_options(self) -> None:
        cases = [
            ["--speaker-count", "2", "--min-speakers", "1"],
            ["--min-speakers", "4", "--max-speakers", "2"],
            ["--speaker-count", "7"],
        ]
        for options in cases:
            with self.subTest(options=options):
                with (
                    mock.patch.object(
                        podcast_cn, "prepare_episode"
                    ) as prepare_mock,
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    podcast_cn.main(
                        ["https://www.youtube.com/watch?v=abc123", *options]
                    )
                self.assertEqual(raised.exception.code, 2)
                prepare_mock.assert_not_called()

    def test_prints_diarization_success_status_in_chinese(self) -> None:
        prepared = podcast_cn.PreparedEpisode(
            video_id="abc123",
            source_title="Test episode",
            transcript_path=Path("/tmp/transcript.zh.md"),
            state_path=Path("/tmp/state.json"),
            speakers_path=Path("/tmp/transcript.zh.speakers.txt"),
            speaker_map_path=Path("/tmp/speaker-map.json"),
            diarization_report=podcast_cn.DiarizationReport(
                status="success",
                backend="fake",
                detected_speakers=2,
                aligned_segments=42,
                ambiguous_segments=3,
                temporary_audio="deleted",
            ),
        )
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(
                podcast_cn, "prepare_episode", return_value=prepared
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = podcast_cn.main(
                ["https://www.youtube.com/watch?v=abc123"]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("说话人序列:/tmp/transcript.zh.speakers.txt", stdout.getvalue())
        self.assertIn("说话人识别:成功（2 人，42 段，3 段需留意）", stdout.getvalue())
        self.assertIn("临时原音频:已删除", stdout.getvalue())

    def test_qwen_provider_uses_only_dashscope_key(self) -> None:
        prepared = podcast_cn.PreparedEpisode(
            video_id="abc123",
            source_title="Qwen episode",
            transcript_path=Path("/tmp/transcript.zh.md"),
            state_path=Path("/tmp/state.json"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": "qwen-test-key"}, clear=True
            ),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(
                podcast_cn, "prepare_episode", return_value=prepared
            ) as prepare_mock,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = podcast_cn.main(
                [
                    "https://www.youtube.com/watch?v=abc123",
                    "--provider",
                    "qwen",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(prepare_mock.call_args.kwargs["provider"], "qwen")

    def test_qwen_equals_syntax_selects_qwen(self) -> None:
        prepared = podcast_cn.PreparedEpisode(
            video_id="abc123",
            source_title="Qwen episode",
            transcript_path=Path("/tmp/transcript.zh.md"),
            state_path=Path("/tmp/state.json"),
        )
        with (
            mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": "qwen-test-key"}, clear=True
            ),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(
                podcast_cn, "prepare_episode", return_value=prepared
            ) as prepare_mock,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = podcast_cn.main(
                ["https://www.youtube.com/watch?v=abc123", "--provider=qwen"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(prepare_mock.call_args.kwargs["provider"], "qwen")

    def test_reports_missing_key_without_calling_pipeline(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(podcast_cn, "prepare_episode") as prepare_mock,
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = podcast_cn.main(
                ["https://www.youtube.com/watch?v=abc123"]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("DEEPSEEK_API_KEY", stderr.getvalue())
        self.assertIn("setup-podcast-model.sh deepseek", stderr.getvalue())
        prepare_mock.assert_not_called()

    def test_reports_missing_qwen_key_without_calling_pipeline(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(podcast_cn, "prepare_episode") as prepare_mock,
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = podcast_cn.main(
                [
                    "https://www.youtube.com/watch?v=abc123",
                    "--provider",
                    "qwen",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("DASHSCOPE_API_KEY", stderr.getvalue())
        self.assertIn("setup-podcast-model.sh qwen", stderr.getvalue())
        self.assertNotIn("DEEPSEEK_API_KEY", stderr.getvalue())
        prepare_mock.assert_not_called()

    def test_help_does_not_require_api_key(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            podcast_cn.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--provider", stdout.getvalue())

    def test_rejects_search_text_instead_of_exact_youtube_url(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch("shutil.which", return_value="/usr/local/bin/yt-dlp"),
            mock.patch.object(podcast_cn, "prepare_episode") as prepare_mock,
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = podcast_cn.main(["find a health podcast"])

        self.assertEqual(exit_code, 2)
        self.assertIn("YouTube URL", stderr.getvalue())
        prepare_mock.assert_not_called()

    def test_shell_wrapper_uses_project_virtualenv(self) -> None:
        wrapper = (ROOT / "bin/podcast-cn.sh").read_text(encoding="utf-8")
        self.assertIn(".venv/bin/python", wrapper)
        self.assertIn("scripts/podcast_cn.py", wrapper)
        self.assertIn('"$@"', wrapper)

    def test_shell_wrapper_loads_provider_key_from_macos_keychain(self) -> None:
        wrapper = (ROOT / "bin/podcast-cn.sh").read_text(encoding="utf-8")
        self.assertIn("security find-generic-password", wrapper)
        self.assertIn("podcast-cn-deepseek", wrapper)
        self.assertIn("podcast-cn-qwen", wrapper)
        self.assertIn("DEEPSEEK_API_KEY", wrapper)
        self.assertIn("DASHSCOPE_API_KEY", wrapper)
        self.assertIn("--provider=", wrapper)
        self.assertIn("setup-podcast-model.sh", wrapper)

    def test_keychain_setup_uses_hidden_input_and_never_echoes_key(self) -> None:
        setup = (ROOT / "bin/setup-podcast-model.sh").read_text(encoding="utf-8")
        self.assertIn("read -r -s", setup)
        self.assertIn("security add-generic-password", setup)
        self.assertIn("podcast-cn-deepseek", setup)
        self.assertIn("podcast-cn-qwen", setup)
        self.assertIn("deepseek|qwen", setup)
        self.assertNotIn('echo "$API_KEY"', setup)
        self.assertNotIn('printf "$API_KEY"', setup)

    def test_provider_setup_wrappers_delegate_without_reading_secrets(self) -> None:
        deepseek = (ROOT / "bin/setup-podcast-deepseek.sh").read_text(
            encoding="utf-8"
        )
        qwen = (ROOT / "bin/setup-podcast-qwen.sh").read_text(encoding="utf-8")

        self.assertIn('setup-podcast-model.sh" deepseek', deepseek)
        self.assertIn('setup-podcast-model.sh" qwen', qwen)
        for wrapper in (deepseek, qwen):
            self.assertNotIn("read -r", wrapper)
            self.assertNotIn("API_KEY", wrapper)

    def test_runtime_dependencies_are_pinned(self) -> None:
        requirements = (
            ROOT / "requirements-podcast.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            requirements,
            [
                "edge-tts==7.2.8",
                "pydub==0.25.1",
                'audioop-lts==0.2.2; python_version >= "3.13"',
                "yt-dlp==2026.7.4",
                "dashscope==1.24.5",
            ],
        )


class PodcastSkillContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = (ROOT / "skills/podcast-translate/SKILL.md").read_text(
            encoding="utf-8"
        )

    def test_default_url_workflow_keeps_deepseek_as_default(self) -> None:
        self.assertIn("默认使用 DeepSeek", self.skill)
        self.assertRegex(
            self.skill,
            r'bin/podcast-cn\.sh"?\s+"\$YOUTUBE_URL"',
        )

    def test_qwen_request_maps_to_qwen_provider_and_setup(self) -> None:
        self.assertIn("通义千问", self.skill)
        self.assertRegex(
            self.skill,
            r'bin/podcast-cn\.sh"?\s+"\$YOUTUBE_URL" --provider qwen',
        )
        self.assertRegex(
            self.skill,
            r'bin/setup-podcast-model\.sh"?\s+qwen',
        )
        self.assertIn("DASHSCOPE_API_KEY", self.skill)

    def test_unsupported_provider_is_not_silently_mapped(self) -> None:
        self.assertIn("当前只支持 DeepSeek 和通义千问", self.skill)
        self.assertIn("不要偷偷映射", self.skill)

    def test_api_key_rule_covers_both_providers_without_literal_secret(self) -> None:
        self.assertIn("DEEPSEEK_API_KEY", self.skill)
        self.assertIn("DASHSCOPE_API_KEY", self.skill)
        self.assertIn("任何翻译 API Key", self.skill)
        self.assertIsNone(re.search(r"s[k]-[A-Za-z0-9]{20,}", self.skill))


if __name__ == "__main__":
    unittest.main()
