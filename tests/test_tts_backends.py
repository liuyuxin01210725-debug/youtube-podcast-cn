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

    def test_edge_default_slot_honors_legacy_voice_default(self) -> None:
        with mock.patch.dict(
            tts_backends.os.environ,
            {"VOICE_DEFAULT": "zh-CN-XiaohanNeural"},
            clear=False,
        ):
            backend = tts_backends.EdgeTTSBackend()
        self.assertEqual(
            backend.resolve_voice("default"), "zh-CN-XiaohanNeural"
        )

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


class QwenBackendTests(unittest.TestCase):
    def test_requires_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY"):
            tts_backends.build_backend("qwen3-api", api_key="")

    def test_maps_all_semantic_voice_slots(self) -> None:
        backend = tts_backends.Qwen3APIBackend(api_key="test-key")
        self.assertEqual(
            {slot: backend.resolve_voice(slot) for slot in tts_backends.VOICE_SLOTS},
            {
                "default": "Cherry",
                "male_1": "Ethan",
                "male_2": "Dylan",
                "female_1": "Cherry",
                "female_2": "Serena",
            },
        )

    def test_qwen_default_slot_is_cherry_and_explicit_female_stays_separate(self) -> None:
        backend = tts_backends.Qwen3APIBackend(
            api_key="test-key",
            voice_map={
                "default": "Cherry",
                "male_1": "Ethan",
                "male_2": "Dylan",
                "female_1": "Serena",
                "female_2": "Chelsie",
            },
        )
        self.assertEqual(backend.resolve_voice("default"), "Cherry")
        self.assertEqual(backend.resolve_voice("female_1"), "Serena")

    def test_rejects_non_https_or_non_official_base_urls_without_secrets(self) -> None:
        secret = "secret-test-key"
        rejected = (
            "http://dashscope.aliyuncs.com/api/v1",
            "https://dashscope.aliyuncs.com.evil.example/api/v1",
            f"https://evil.example/api/v1?api_key={secret}",
            f"https://dashscope.aliyuncs.com:{secret}/api/v1",
        )
        for base_url in rejected:
            with self.subTest(base_url=base_url):
                with mock.patch.dict(
                    tts_backends.os.environ,
                    {"QWEN_TTS_BASE_URL": base_url},
                    clear=False,
                ):
                    with self.assertRaises(ValueError) as caught:
                        tts_backends.Qwen3APIBackend(api_key=secret)
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(base_url, str(caught.exception))

    def test_accepts_official_china_and_international_base_urls(self) -> None:
        for host in (
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
        ):
            base_url = f"https://{host}/api/v1"
            with self.subTest(host=host):
                with mock.patch.dict(
                    tts_backends.os.environ,
                    {"QWEN_TTS_BASE_URL": base_url},
                    clear=False,
                ):
                    backend = tts_backends.Qwen3APIBackend(api_key="test-key")
                self.assertEqual(backend.base_url, base_url)

    def test_sends_instruct_request_and_downloads_audio(self) -> None:
        captured: dict[str, object] = {}

        class FakeConversation:
            @staticmethod
            def call(**kwargs):
                captured.update(kwargs)
                return {
                    "status_code": 200,
                    "request_id": "request-123",
                    "output": {
                        "audio": {
                            "url": "http://dashscope-result.example/test.wav"
                        }
                    },
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 34,
                        "output_tokens_details": {"audio_tokens": 30},
                    },
                }

        fake_dashscope = types.SimpleNamespace(MultiModalConversation=FakeConversation)

        def fake_download(url: str, output: Path) -> None:
            captured["download_url"] = url
            output.write_bytes(b"RIFF-test-wave")

        request = tts_backends.SynthesisRequest(
            text="欢迎收听。", voice_slot="female_1", style="conversation"
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "part.wav"
            backend = tts_backends.Qwen3APIBackend(api_key="test-key")
            with (
                mock.patch.object(
                    tts_backends, "_load_dashscope", return_value=fake_dashscope
                ),
                mock.patch.object(
                    tts_backends, "_download_audio", side_effect=fake_download
                ),
            ):
                usage = asyncio.run(backend.synthesize(request, output))

        self.assertEqual(captured["model"], "qwen3-tts-instruct-flash")
        self.assertEqual(captured["voice"], "Cherry")
        self.assertEqual(captured["language_type"], "Chinese")
        self.assertEqual(captured["text"], "欢迎收听。")
        self.assertTrue(captured["instructions"])
        self.assertTrue(captured["optimize_instructions"])
        self.assertFalse(captured["stream"])
        self.assertEqual(
            fake_dashscope.base_http_api_url,
            "https://dashscope.aliyuncs.com/api/v1",
        )
        self.assertEqual(usage.request_id, "request-123")
        self.assertEqual(usage.audio_tokens, 30)

    def test_error_message_never_contains_api_key_or_provider_message(self) -> None:
        secret = "secret-test-key"

        class FakeConversation:
            @staticmethod
            def call(**kwargs):
                return {
                    "status_code": 500,
                    "request_id": "request-failed",
                    "code": "InternalError",
                    "message": f"failure {secret}",
                }

        fake_dashscope = types.SimpleNamespace(MultiModalConversation=FakeConversation)
        backend = tts_backends.Qwen3APIBackend(api_key=secret)
        request = tts_backends.SynthesisRequest(
            text="测试。", voice_slot="male_1", style="documentary"
        )
        with mock.patch.object(
            tts_backends, "_load_dashscope", return_value=fake_dashscope
        ):
            with self.assertRaises(tts_backends.TTSBackendError) as caught:
                asyncio.run(backend.synthesize(request, Path("unused.wav")))
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("request-failed", str(caught.exception))

    def test_arrearage_response_is_non_retryable(self) -> None:
        class FakeConversation:
            @staticmethod
            def call(**kwargs):
                return {
                    "status_code": 400,
                    "request_id": "request-arrearage",
                    "code": "Arrearage",
                    "message": "account balance is insufficient",
                }

        fake_dashscope = types.SimpleNamespace(MultiModalConversation=FakeConversation)
        backend = tts_backends.Qwen3APIBackend(api_key="test-key")
        request = tts_backends.SynthesisRequest(
            text="测试。", voice_slot="male_1", style="documentary"
        )
        with mock.patch.object(
            tts_backends, "_load_dashscope", return_value=fake_dashscope
        ):
            with self.assertRaises(tts_backends.TTSBackendError) as caught:
                asyncio.run(backend.synthesize(request, Path("unused.wav")))
        self.assertFalse(caught.exception.retryable)
        self.assertIn("Arrearage", str(caught.exception))

    def test_sdk_exception_is_wrapped_without_sensitive_text(self) -> None:
        secret = "secret-test-key"

        class FakeConversation:
            @staticmethod
            def call(**kwargs):
                auth_prefix = "Authorization" + ": Bearer "
                raise RuntimeError(
                    f"{auth_prefix}{secret} "
                    "https://signed.example/audio.wav?token=private"
                )

        fake_dashscope = types.SimpleNamespace(MultiModalConversation=FakeConversation)
        backend = tts_backends.Qwen3APIBackend(api_key=secret)
        request = tts_backends.SynthesisRequest(
            text="测试。", voice_slot="male_1", style="documentary"
        )
        with mock.patch.object(
            tts_backends, "_load_dashscope", return_value=fake_dashscope
        ):
            with self.assertRaises(tts_backends.TTSBackendError) as caught:
                asyncio.run(backend.synthesize(request, Path("unused.wav")))
        visible = str(caught.exception)
        self.assertNotIn(secret, visible)
        self.assertNotIn("signed.example", visible)
        self.assertEqual(visible, "Qwen TTS 请求失败")

    def test_signed_aliyun_download_is_upgraded_to_https(self) -> None:
        captured: dict[str, str] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self) -> bytes:
                return b"RIFF-wave"

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = str(timeout)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "audio.wav"
            with mock.patch.object(
                tts_backends.urllib.request, "urlopen", side_effect=fake_urlopen
            ):
                tts_backends._download_audio(
                    "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav?sig=x",
                    output,
                )
            self.assertEqual(output.read_bytes(), b"RIFF-wave")
        self.assertTrue(captured["url"].startswith("https://"))
        self.assertEqual(captured["timeout"], "60")


if __name__ == "__main__":
    unittest.main()
