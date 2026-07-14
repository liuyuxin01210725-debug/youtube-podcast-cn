# Qwen3-TTS Quality Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, resumable Qwen3-TTS high-quality mode to the existing Chinese podcast pipeline while preserving EdgeTTS as the default and producing three real A/B listening samples.

**Architecture:** Keep text cleanup, speaker assignment, segmentation, merge, and atomic MP3 publication in `scripts/tts-cn.py`. Move provider-specific synthesis behind a small asynchronous backend interface in `scripts/tts_backends.py`, and put content-addressed fragment persistence in `scripts/tts_cache.py`. The shell wrapper remains responsible for loading the DashScope key from macOS Keychain only when Qwen synthesis is selected.

**Tech Stack:** Python 3.10+, asyncio, edge-tts 7.2.8, DashScope Python SDK 1.24.5, pydub 0.25.1, ffmpeg/ffprobe, unittest, Bash, macOS Keychain.

## Global Constraints

- Support macOS and Python 3.10 or newer; do not add Qwen local inference in this project.
- Keep `edge` as the default backend and preserve existing commands when `--tts` is omitted.
- Add only `edge` and `qwen3-api`; reject arbitrary backend names.
- Use `qwen3-tts-instruct-flash` with `language_type="Chinese"` for Qwen synthesis.
- Use semantic voice slots. Qwen defaults are `female_1=Cherry`, `female_2=Serena`, `male_1=Ethan`, and `male_2=Dylan`.
- Use `conversation` and `documentary` as the only style values.
- Do not clone the source speaker's voice.
- Read `DASHSCOPE_API_KEY` from the environment after the shell wrapper loads the existing `podcast-cn-qwen` Keychain item.
- Never put an API key in a command argument, log, cache key, exception, test fixture, repository file, or generated audio metadata.
- The DashScope key previously pasted into chat must be revoked and replaced before the first real API smoke test; the replacement must enter through hidden Keychain setup, not chat.
- Keep Qwen requests below the documented 512-token input ceiling by using a conservative 450-character fragment limit.
- Persist only complete, decodable fragments. Backend, model, resolved voice, style instruction, text, and audio-affecting parameters must all participate in the cache fingerprint.
- Never silently mix Edge and Qwen fragments in one output. If any fragment fails, do not replace an existing complete MP3.
- Automated tests must not contact EdgeTTS, DashScope, or signed audio URLs.
- Do not commit private transcripts, generated audio, A/B files, signed URLs, or API usage records.
- Project B, the private WeChat audio library, is outside this plan.

Official contracts verified on 2026-07-14:

- Qwen non-realtime TTS guide: <https://help.aliyun.com/zh/model-studio/non-realtime-tts-user-guide>
- Qwen TTS API contract: <https://help.aliyun.com/zh/model-studio/qwen-tts-api>
- Qwen system voice list: <https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list>

---

## File Map

- Create `scripts/tts_backends.py`: semantic voice slots, style presets, backend protocol, Edge backend, Qwen3 API backend, DashScope response parsing, signed-audio download.
- Create `scripts/tts_cache.py`: deterministic fragment keys, safe cache paths, corrupt-entry removal, atomic cache publication.
- Modify `scripts/tts-cn.py`: use semantic slots and backend objects, provider-aware chunk sizes, persistent cache, preview paragraph limit, provider/style CLI flags, usage logging, generic audio decoding.
- Modify `bin/tts-cn.sh`: parse `--tts`, load `podcast-cn-qwen` from Keychain only for Qwen synthesis, keep help and dump modes key-free.
- Modify `requirements-podcast.txt`: pin DashScope SDK 1.24.5.
- Create `tests/test_tts_backends.py`: provider contract and no-network Qwen tests.
- Create `tests/test_tts_cache.py`: fingerprint, isolation, corruption, and atomic-publication tests.
- Modify `tests/test_tts_cn.py`: semantic slot, backend orchestration, cache statistics, preview, and WAV merge regression tests.
- Modify `tests/test_public_package.py`: dependency, documentation, backend allowlist, and secret-handling assertions.
- Modify `README.md`: user-facing backend selection, quality mode, cost/key behavior, cache behavior, and A/B workflow.
- Modify `skills/podcast-translate/SKILL.md`: distinguish Qwen translation from Qwen speech and route explicit quality requests to `--tts qwen3-api`.

---

### Task 1: Introduce semantic voice slots and an Edge backend boundary

**Files:**
- Create: `scripts/tts_backends.py`
- Modify: `scripts/tts-cn.py:53-85,217-367,387-408,445-532`
- Create: `tests/test_tts_backends.py`
- Modify: `tests/test_tts_cn.py:17-35,40-182`

**Interfaces:**
- Produces: `SynthesisRequest(text: str, voice_slot: str, style: str)`.
- Produces: `SynthesisUsage(request_id: str | None, input_tokens: int, output_tokens: int, audio_tokens: int)`.
- Produces: `TTSBackend.synthesize(request: SynthesisRequest, output: Path) -> SynthesisUsage`.
- Produces: `build_backend(name: str, api_key: str | None = None) -> TTSBackend`.
- Produces: `voice_slot_for_label(label: str, assigned: dict[str, str]) -> str` in `scripts/tts-cn.py`.
- Preserves: no network access in unit tests and the current Edge output behavior.

- [ ] **Step 1: Add failing backend and semantic-slot tests**

Create `tests/test_tts_backends.py` with these tests and a loader that adds `scripts/` to `sys.path`:

```python
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

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

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
            with mock.patch.object(tts_backends.edge_tts, "Communicate", FakeCommunicate):
                usage = asyncio.run(backend.synthesize(request, output))
            self.assertEqual(output.read_bytes(), b"edge-audio")
            self.assertEqual(calls[0][:2], ("你好。", backend.resolve_voice("female_1")))
            self.assertEqual(usage.output_tokens, 0)


if __name__ == "__main__":
    unittest.main()
```

Add to `tests/test_tts_cn.py`:

```python
class VoiceSlotTests(unittest.TestCase):
    def test_assigns_stable_semantic_slots_by_gender(self) -> None:
        assigned: dict[str, str] = {}
        self.assertEqual(tts_cn.voice_slot_for_label("主持人-女", assigned), "female_1")
        self.assertEqual(tts_cn.voice_slot_for_label("嘉宾甲-男", assigned), "male_1")
        self.assertEqual(tts_cn.voice_slot_for_label("嘉宾乙-男", assigned), "male_2")
        self.assertEqual(tts_cn.voice_slot_for_label("主持人-女", assigned), "female_1")

    def test_plain_text_uses_female_one_slot(self) -> None:
        self.assertEqual(tts_cn.parse_segments("这是纯文本。"), [("female_1", "这是纯文本。")])
```

- [ ] **Step 2: Run the focused tests and verify the new contract is absent**

Run:

```bash
.venv/bin/python -m unittest tests.test_tts_backends tests.test_tts_cn.VoiceSlotTests -v
```

Expected: FAIL because `scripts/tts_backends.py` and `voice_slot_for_label` do not exist.

- [ ] **Step 3: Implement the backend protocol and Edge adapter**

Create `scripts/tts_backends.py` with these public types and behavior:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import edge_tts

VOICE_SLOTS = ("male_1", "male_2", "female_1", "female_2")
STYLE_NAMES = ("conversation", "documentary")


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    voice_slot: str
    style: str


@dataclass(frozen=True)
class SynthesisUsage:
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    audio_tokens: int = 0


class TTSBackend(Protocol):
    name: str
    model: str
    file_suffix: str
    max_chars: int
    concurrency: int

    def resolve_voice(self, voice_slot: str) -> str:
        raise NotImplementedError

    def fingerprint(self) -> dict[str, object]:
        raise NotImplementedError

    async def synthesize(
        self, request: SynthesisRequest, output: Path
    ) -> SynthesisUsage:
        raise NotImplementedError


def _edge_voice_map() -> dict[str, str]:
    legacy_male = os.getenv("VOICE_MALE")
    legacy_female = os.getenv("VOICE_FEMALE")
    return {
        "male_1": os.getenv(
            "VOICE_MALE_1", legacy_male or "zh-CN-YunyangNeural"
        ),
        "male_2": os.getenv("VOICE_MALE_2", "zh-CN-YunjianNeural"),
        "female_1": os.getenv(
            "VOICE_FEMALE_1", legacy_female or "zh-CN-XiaoxiaoNeural"
        ),
        "female_2": os.getenv("VOICE_FEMALE_2", "zh-CN-XiaoyiNeural"),
    }


class EdgeTTSBackend:
    name = "edge"
    model = "edge-tts"
    file_suffix = ".mp3"
    max_chars = 2500

    def __init__(self, voice_map: dict[str, str] | None = None) -> None:
        self.voice_map = voice_map or _edge_voice_map()
        self.concurrency = int(os.getenv("TTS_CONCURRENCY", "8"))

    def resolve_voice(self, voice_slot: str) -> str:
        if voice_slot not in VOICE_SLOTS:
            raise ValueError(f"未知 voice slot: {voice_slot}")
        return self.voice_map[voice_slot]

    def fingerprint(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "model": self.model,
            "voices": self.voice_map,
            "max_chars": self.max_chars,
        }

    async def synthesize(
        self, request: SynthesisRequest, output: Path
    ) -> SynthesisUsage:
        communicate = edge_tts.Communicate(
            request.text, self.resolve_voice(request.voice_slot)
        )
        await communicate.save(str(output))
        return SynthesisUsage()


def build_backend(name: str, api_key: str | None = None) -> TTSBackend:
    if name == "edge":
        return EdgeTTSBackend()
    raise ValueError(f"不支持的 TTS 后端: {name}")
```

- [ ] **Step 4: Refactor orchestration to use semantic slots and the Edge backend**

In `scripts/tts-cn.py`:

1. Import `SynthesisRequest`, `TTSBackend`, and `build_backend` from `tts_backends`.
2. Remove the provider-specific voice constants from lines 67-78.
3. Rename `voice_for_label` to `voice_slot_for_label` and return `male_1`, `male_2`, `female_1`, or `female_2` using stable per-label assignment.
4. Make `parse_segments` return `list[tuple[str, str]]` where the first element is a semantic slot.
5. Change `synth_all` to accept `backend: TTSBackend` and `style: str`, use `backend.max_chars`, create files with `backend.file_suffix`, and call `backend.synthesize(SynthesisRequest(text=text, voice_slot=voice_slot, style=style), out)`.
6. Change `merge` to use `AudioSegment.from_file(f)` so both Edge MP3 fragments and Qwen WAV fragments decode.
7. Change `run` to accept a backend and style; `main` creates `build_backend("edge")` in this task so behavior is unchanged.

The worker call must have this form:

```python
request = SynthesisRequest(text=text, voice_slot=voice_slot, style=style)
usage = await backend.synthesize(request, out)
return (out, voice_slot, usage)
```

The merge input must remain `list[tuple[Path, str]]`; strip `SynthesisUsage` after updating statistics, before calling `merge`.

Before loading `scripts/tts-cn.py` in `tests/test_tts_cn.py`, insert the scripts directory so the new sibling modules resolve when the test file is executed directly by the Makefile:

```python
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
```

- [ ] **Step 5: Update the existing fake functions and generic decoder test**

Update the fake signatures in `tests/test_tts_cn.py` to receive `backend`, `style`, and later optional cache arguments by keyword. Change `FakeAudioSegment.from_mp3` to:

```python
@staticmethod
def from_file(path):
    raise ValueError("corrupt audio")
```

Keep the existing assertions that corrupt fragments abort the merge and a failed synthesis never overwrites an old MP3.

- [ ] **Step 6: Run all TTS regression tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_tts_backends tests.test_tts_cn -v
```

Expected: all tests PASS; no real network request occurs.

- [ ] **Step 7: Commit the provider-neutral boundary**

```bash
git add scripts/tts_backends.py scripts/tts-cn.py tests/test_tts_backends.py tests/test_tts_cn.py
git commit -m "refactor: add provider-neutral TTS backend"
```

---

### Task 2: Implement the Qwen3 non-realtime API backend

**Files:**
- Modify: `scripts/tts_backends.py`
- Modify: `tests/test_tts_backends.py`
- Modify: `requirements-podcast.txt`

**Interfaces:**
- Consumes: `SynthesisRequest`, `SynthesisUsage`, and `TTSBackend` from Task 1.
- Produces: `Qwen3APIBackend(api_key: str, model: str | None = None, voice_map: dict[str, str] | None = None)`.
- Produces: `TTSBackendError`, whose visible message never contains provider response text, credentials, or signed URLs.
- Produces: `STYLE_INSTRUCTIONS: dict[str, str]`.
- Extends: `build_backend("qwen3-api", api_key)`.

- [ ] **Step 1: Add failing Qwen request-contract tests**

Add these cases to `tests/test_tts_backends.py`:

```python
class QwenBackendTests(unittest.TestCase):
    def test_requires_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY"):
            tts_backends.build_backend("qwen3-api", api_key="")

    def test_maps_all_semantic_voice_slots(self) -> None:
        backend = tts_backends.Qwen3APIBackend(api_key="test-key")
        self.assertEqual(
            {slot: backend.resolve_voice(slot) for slot in tts_backends.VOICE_SLOTS},
            {
                "male_1": "Ethan",
                "male_2": "Dylan",
                "female_1": "Cherry",
                "female_2": "Serena",
            },
        )

    def test_sends_instruct_request_and_downloads_audio(self) -> None:
        captured: dict[str, object] = {}

        class FakeConversation:
            @staticmethod
            def call(**kwargs):
                captured.update(kwargs)
                return {
                    "status_code": 200,
                    "request_id": "request-123",
                    "output": {"audio": {"url": "http://dashscope-result.example/test.wav"}},
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
                mock.patch.object(tts_backends, "_load_dashscope", return_value=fake_dashscope),
                mock.patch.object(tts_backends, "_download_audio", side_effect=fake_download),
            ):
                usage = asyncio.run(backend.synthesize(request, output))

        self.assertEqual(captured["model"], "qwen3-tts-instruct-flash")
        self.assertEqual(captured["voice"], "Cherry")
        self.assertEqual(captured["language_type"], "Chinese")
        self.assertEqual(captured["text"], "欢迎收听。")
        self.assertTrue(captured["instructions"])
        self.assertTrue(captured["optimize_instructions"])
        self.assertFalse(captured["stream"])
        self.assertEqual(usage.request_id, "request-123")
        self.assertEqual(usage.audio_tokens, 30)

    def test_error_message_never_contains_api_key_or_signed_url(self) -> None:
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
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(backend.synthesize(request, Path("unused.wav")))
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("request-failed", str(caught.exception))

    def test_sdk_exception_is_wrapped_without_sensitive_text(self) -> None:
        secret = "secret-test-key"

        class FakeConversation:
            @staticmethod
            def call(**kwargs):
                raise RuntimeError(
                    f"Authorization: Bearer {secret} "
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
```

- [ ] **Step 2: Run the Qwen tests and verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.test_tts_backends.QwenBackendTests -v
```

Expected: FAIL because `Qwen3APIBackend` and Qwen backend selection do not exist.

- [ ] **Step 3: Add the pinned DashScope SDK**

Append exactly this line to `requirements-podcast.txt`:

```text
dashscope==1.24.5
```

Install the updated environment:

```bash
.venv/bin/pip install -r requirements-podcast.txt
```

Expected: installation succeeds and `.venv/bin/python -c 'import dashscope'` exits 0.

- [ ] **Step 4: Implement style presets and Qwen voice mapping**

Add to `scripts/tts_backends.py`:

```python
class TTSBackendError(RuntimeError):
    """Safe-to-log provider error with sensitive response details removed."""


STYLE_INSTRUCTIONS = {
    "conversation": (
        "自然、松弛的中文播客对话；语气随语义自然变化；避免新闻播音腔；"
        "表达清晰稳定，不夸张，不使用营销语气。"
    ),
    "documentary": (
        "平静、可信、清楚的中文知识讲解；中等语速；停顿自然；"
        "避免营销感、喊话感和夸张情绪。"
    ),
}


def _qwen_voice_map() -> dict[str, str]:
    return {
        "male_1": os.getenv("QWEN_TTS_VOICE_MALE_1", "Ethan"),
        "male_2": os.getenv("QWEN_TTS_VOICE_MALE_2", "Dylan"),
        "female_1": os.getenv("QWEN_TTS_VOICE_FEMALE_1", "Cherry"),
        "female_2": os.getenv("QWEN_TTS_VOICE_FEMALE_2", "Serena"),
    }
```

The Qwen backend must expose:

```python
class Qwen3APIBackend:
    name = "qwen3-api"
    file_suffix = ".wav"
    max_chars = 450

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        voice_map: dict[str, str] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("qwen3-api 需要 DASHSCOPE_API_KEY")
        self._api_key = api_key
        self.model = model or os.getenv(
            "QWEN_TTS_MODEL", "qwen3-tts-instruct-flash"
        )
        self.voice_map = voice_map or _qwen_voice_map()
        self.concurrency = int(os.getenv("QWEN_TTS_CONCURRENCY", "2"))

    def resolve_voice(self, voice_slot: str) -> str:
        if voice_slot not in VOICE_SLOTS:
            raise ValueError(f"未知 voice slot: {voice_slot}")
        return self.voice_map[voice_slot]

    def fingerprint(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "model": self.model,
            "voices": self.voice_map,
            "styles": STYLE_INSTRUCTIONS,
            "max_chars": self.max_chars,
            "language_type": "Chinese",
            "optimize_instructions": True,
        }
```

- [ ] **Step 5: Implement the non-streaming API call and signed-audio download**

Use lazy SDK loading so Edge-only use does not require importing DashScope at module import time:

```python
def _load_dashscope():
    import dashscope
    return dashscope


def _field(container, name: str, default=None):
    if isinstance(container, dict):
        return container.get(name, default)
    return getattr(container, name, default)
```

Add `asyncio`, `urllib.parse`, and `urllib.request` imports. Implement `_download_audio` without logging:

```python
def _download_audio(url: str, output: Path) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise TTSBackendError("Qwen 返回了不支持的音频地址")
    if parsed.scheme == "http" and (parsed.hostname or "").endswith(".aliyuncs.com"):
        parsed = parsed._replace(scheme="https")
        url = urllib.parse.urlunparse(parsed)
    request = urllib.request.Request(
        url, headers={"User-Agent": "youtube-podcast-cn/1.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        audio = response.read()
    if not audio:
        raise TTSBackendError("Qwen 返回了空音频")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(audio)
```

Implement `Qwen3APIBackend.synthesize` as an `async` method that calls the synchronous SDK through `asyncio.to_thread`:

```python
try:
    dashscope = _load_dashscope()
    response = await asyncio.to_thread(
        dashscope.MultiModalConversation.call,
        api_key=self._api_key,
        model=self.model,
        text=request.text,
        voice=self.resolve_voice(request.voice_slot),
        language_type="Chinese",
        instructions=STYLE_INSTRUCTIONS[request.style],
        optimize_instructions=True,
        stream=False,
    )
except Exception as exc:
    raise TTSBackendError("Qwen TTS 请求失败") from exc
```

Then:

1. Read `status_code`, `request_id`, `code`, `output.audio.url`, and `usage` using `_field`.
2. On non-200 status, raise `RuntimeError` containing only the status, provider code, and request ID. Do not include the provider message.
3. Reject a missing audio URL using the request ID only.
4. Download through `await asyncio.to_thread(_download_audio, audio_url, output)` inside a `try` block; wrap every download exception as `TTSBackendError("Qwen 音频下载失败")` without copying the original message.
5. Return `SynthesisUsage` populated from `usage.input_tokens`, `usage.output_tokens`, and `usage.output_tokens_details.audio_tokens`.

Extend the factory:

```python
if name == "qwen3-api":
    return Qwen3APIBackend(api_key=api_key or "")
```

- [ ] **Step 6: Run provider tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_tts_backends -v
```

Expected: all Edge and Qwen tests PASS without network access.

- [ ] **Step 7: Commit the Qwen backend**

```bash
git add requirements-podcast.txt scripts/tts_backends.py tests/test_tts_backends.py
git commit -m "feat: add Qwen3 TTS API backend"
```

---

### Task 3: Add decodable, content-addressed fragment caching

**Files:**
- Create: `scripts/tts_cache.py`
- Create: `tests/test_tts_cache.py`
- Modify: `scripts/tts-cn.py:320-379,445-518`
- Modify: `tests/test_tts_cn.py`

**Interfaces:**
- Consumes: `SynthesisRequest` and `backend.fingerprint()`.
- Produces: `FragmentCache(root: Path, backend_fingerprint: dict[str, object], suffix: str)`.
- Produces: `FragmentCache.key_for(request: SynthesisRequest) -> str`.
- Produces: `FragmentCache.get(request, validator) -> Path | None`.
- Produces: `FragmentCache.store(request, generated: Path) -> Path`.
- Extends synthesis stats with `cached`, `provider_calls`, `input_tokens`, `output_tokens`, and `audio_tokens`.

- [ ] **Step 1: Write failing cache tests**

Create `tests/test_tts_cache.py` covering the exact behaviors below:

```python
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

    def test_key_changes_with_backend_model_voice_style_or_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = FragmentCache(
                Path(tmp),
                {"backend": "qwen3-api", "model": "model-a", "voices": {"female_1": "Cherry"}},
                ".wav",
            )
            second = FragmentCache(
                Path(tmp),
                {"backend": "qwen3-api", "model": "model-b", "voices": {"female_1": "Cherry"}},
                ".wav",
            )
            self.assertNotEqual(first.key_for(self.request), second.key_for(self.request))
            self.assertNotEqual(
                first.key_for(self.request),
                first.key_for(SynthesisRequest("不同正文", "female_1", "conversation")),
            )

    def test_store_and_get_valid_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated.wav"
            generated.write_bytes(b"valid-audio")
            cache = FragmentCache(root / "cache", {"backend": "qwen3-api"}, ".wav")
            stored = cache.store(self.request, generated)
            hit = cache.get(self.request, lambda path: path.read_bytes() == b"valid-audio")
            self.assertEqual(hit, stored)
            self.assertTrue(stored.is_file())

    def test_corrupt_hit_is_deleted_and_treated_as_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated.wav"
            generated.write_bytes(b"corrupt")
            cache = FragmentCache(root / "cache", {"backend": "qwen3-api"}, ".wav")
            stored = cache.store(self.request, generated)
            self.assertIsNone(cache.get(self.request, lambda path: False))
            self.assertFalse(stored.exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run cache tests and verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.test_tts_cache -v
```

Expected: FAIL because `scripts/tts_cache.py` does not exist.

- [ ] **Step 3: Implement deterministic keys and atomic storage**

Create `scripts/tts_cache.py` with:

```python
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable

from tts_backends import SynthesisRequest


class FragmentCache:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: Path,
        backend_fingerprint: dict[str, object],
        suffix: str,
    ) -> None:
        self.root = root
        self.backend_fingerprint = backend_fingerprint
        self.suffix = suffix

    def key_for(self, request: SynthesisRequest) -> str:
        payload = {
            "schema": self.SCHEMA_VERSION,
            "backend": self.backend_fingerprint,
            "text": request.text,
            "voice_slot": request.voice_slot,
            "style": request.style,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def path_for(self, request: SynthesisRequest) -> Path:
        key = self.key_for(request)
        backend = str(self.backend_fingerprint.get("backend", "unknown"))
        model = str(self.backend_fingerprint.get("model", "unknown"))
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{backend}-{model}")
        return self.root / safe / key[:2] / f"{key}{self.suffix}"

    def get(
        self, request: SynthesisRequest, validator: Callable[[Path], bool]
    ) -> Path | None:
        path = self.path_for(request)
        if not path.is_file():
            return None
        if path.stat().st_size > 0 and validator(path):
            return path
        path.unlink(missing_ok=True)
        return None

    def store(self, request: SynthesisRequest, generated: Path) -> Path:
        if not generated.is_file() or generated.stat().st_size == 0:
            raise RuntimeError("拒绝缓存空音频片段")
        destination = self.path_for(request)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        shutil.copy2(generated, staged)
        os.replace(staged, destination)
        return destination
```

- [ ] **Step 4: Integrate cache hits into synthesis orchestration**

In `scripts/tts-cn.py`:

1. Add `audio_is_decodable(path: Path) -> bool` that returns `True` only when `AudioSegment.from_file(path)` succeeds and has positive duration.
2. Add `cache: FragmentCache` to `synth_all`.
3. For each request, call `cache.get(request, audio_is_decodable)` before acquiring the provider semaphore.
4. On a hit, increment both `ok` and `cached`, and return the cached path without calling the backend.
5. On a miss, synthesize to the task's temporary path, verify it is decodable, then call `cache.store` and return the persistent path.
6. Accumulate `input_tokens`, `output_tokens`, and `audio_tokens` from each `SynthesisUsage` under `progress_lock`.
7. Initialize statistics exactly as:

```python
stats = {
    "total": total,
    "ok": 0,
    "failed": 0,
    "retried": 0,
    "cached": 0,
    "provider_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "audio_tokens": 0,
}
```

Increment `provider_calls` immediately before every `backend.synthesize` attempt, including attempts that later fail. A cache hit does not increment it.

In `run`, create the cache at `output_path.parent / ".tts-cache"` using the chosen backend fingerprint and file suffix.

- [ ] **Step 5: Add orchestration tests for hits, corruption, and usage**

Add tests to `tests/test_tts_cn.py` using a fake backend whose `synthesize` writes a valid fake fragment while `AudioSegment.from_file` is mocked. Assert:

- first run calls the backend and stores the fragment;
- second run with the same request does not call the backend and reports `cached == 1`;
- changing style or model calls the backend again;
- a corrupt cached file is removed and regenerated;
- provider call count and Qwen usage totals are summed without logging a signed URL or API key.

The fake backend must implement the complete Task 1 interface; do not patch `synth_all` itself for these tests.

- [ ] **Step 6: Run cache and TTS tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_tts_cache tests.test_tts_backends tests.test_tts_cn -v
```

Expected: all tests PASS and make zero network calls.

- [ ] **Step 7: Commit persistent fragment caching**

```bash
git add scripts/tts_cache.py scripts/tts-cn.py tests/test_tts_cache.py tests/test_tts_cn.py
git commit -m "feat: cache complete TTS fragments"
```

---

### Task 4: Wire CLI selection, Keychain loading, preview limits, and safe logs

**Files:**
- Modify: `scripts/tts-cn.py:445-532`
- Modify: `bin/tts-cn.sh:1-26`
- Modify: `tests/test_tts_cn.py`
- Modify: `tests/test_public_package.py`

**Interfaces:**
- Produces CLI: `--tts {edge,qwen3-api}` with default `edge`.
- Produces CLI: `--style {conversation,documentary}` with default `conversation`.
- Produces CLI: `--preview-paragraphs N` for short A/B samples; omitted means full input.
- Uses Keychain service `podcast-cn-qwen` and environment variable `DASHSCOPE_API_KEY` only for `qwen3-api` synthesis.

- [ ] **Step 1: Add failing parser, preview, and shell-security tests**

Add to `tests/test_tts_cn.py`:

```python
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
```

Add to `tests/test_public_package.py`:

```python
def test_tts_wrapper_loads_qwen_key_only_for_qwen_backend(self) -> None:
    wrapper = (ROOT / "bin/tts-cn.sh").read_text(encoding="utf-8")
    self.assertIn("qwen3-api", wrapper)
    self.assertIn("podcast-cn-qwen", wrapper)
    self.assertIn("DASHSCOPE_API_KEY", wrapper)
    self.assertIn("--dump-segments", wrapper)
    self.assertNotIn("sk-", wrapper)
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.test_tts_cn.CliTests tests.test_public_package.PublicPackageTests.test_tts_wrapper_loads_qwen_key_only_for_qwen_backend -v
```

Expected: FAIL because the new parser fields and wrapper behavior do not exist.

- [ ] **Step 3: Extract `build_parser` and implement preview-safe speaker preparation**

In `scripts/tts-cn.py`, move parser construction into `build_parser()`. Add:

```python
parser.add_argument(
    "--tts", choices=("edge", "qwen3-api"), default="edge",
    help="语音后端: edge 免费稳定; qwen3-api 高质量且消耗百炼额度",
)
parser.add_argument(
    "--style", choices=("conversation", "documentary"), default="conversation",
    help="表达预设: conversation 访谈; documentary 知识讲解",
)
parser.add_argument(
    "--preview-paragraphs", type=int,
    help="只合成清理后的前 N 段,用于短试听；不指定则合成全文",
)
```

Extract `apply_speaker_sequence(cleaned, sequence, preview_paragraphs=None) -> str`. It must validate the full sequence length against the full cleaned paragraph list before slicing both lists to the same positive limit. When no speakers file is supplied, preview mode slices cleaned paragraphs directly and rejoins them with blank lines. Reject zero or negative values through `parser.error("--preview-paragraphs 必须大于 0")`.

In `main`, create the backend as:

```python
api_key = os.getenv("DASHSCOPE_API_KEY") if args.tts == "qwen3-api" else None
backend = build_backend(args.tts, api_key=api_key)
```

- [ ] **Step 4: Make `bin/tts-cn.sh` load the existing Qwen Keychain item conditionally**

Keep the existing virtual-environment check. Before `exec`, scan arguments for `--tts qwen3-api`, `--tts=qwen3-api`, `--dump-segments`, and `--help`. Only if Qwen synthesis is selected and neither dump nor help mode is active:

1. Use `DASHSCOPE_API_KEY` if already exported.
2. Otherwise call `security find-generic-password -a "${USER:-$(id -un)}" -s "podcast-cn-qwen" -w`.
3. Export the returned value without printing it.
4. If still empty, exit 2 and print `bash bin/setup-podcast-model.sh qwen` as the setup command.

The final line remains:

```bash
exec "$VENV_PY" "$PROJECT_ROOT/scripts/tts-cn.py" "$@"
```

Use this argument and Keychain block before that final `exec`:

```bash
TTS_BACKEND="edge"
EXPECT_TTS=0
SKIP_TTS_KEY=0
for ARG in "$@"; do
  if [ "$EXPECT_TTS" -eq 1 ]; then
    TTS_BACKEND="$ARG"
    EXPECT_TTS=0
    continue
  fi
  case "$ARG" in
    --tts)
      EXPECT_TTS=1
      ;;
    --tts=*)
      TTS_BACKEND="${ARG#--tts=}"
      ;;
    --dump-segments|-h|--help)
      SKIP_TTS_KEY=1
      ;;
  esac
done

if [ "$TTS_BACKEND" = "qwen3-api" ] \
  && [ "$SKIP_TTS_KEY" -eq 0 ] \
  && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
  KEYCHAIN_ACCOUNT="${USER:-$(id -un)}"
  KEYCHAIN_VALUE="$(
    security find-generic-password \
      -a "$KEYCHAIN_ACCOUNT" \
      -s "podcast-cn-qwen" \
      -w 2>/dev/null || true
  )"
  if [ -n "$KEYCHAIN_VALUE" ]; then
    export DASHSCOPE_API_KEY="$KEYCHAIN_VALUE"
  fi
  unset KEYCHAIN_VALUE
fi

if [ "$TTS_BACKEND" = "qwen3-api" ] \
  && [ "$SKIP_TTS_KEY" -eq 0 ] \
  && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
  echo "错误:还没有配置 DASHSCOPE_API_KEY。" >&2
  echo "首次使用请运行: bash $PROJECT_ROOT/bin/setup-podcast-model.sh qwen" >&2
  exit 2
fi
```

- [ ] **Step 5: Log backend, model, cache, and token totals without secrets**

At synthesis start, log backend name, model, style, concurrency, and retry count. At completion, add `服务调用`, `缓存命中`, `输入 tokens`, `输出 tokens`, and `音频 tokens` when the corresponding value is nonzero. Do not log SDK response objects, API keys, provider messages, or signed download URLs.

- [ ] **Step 6: Run parser, shell syntax, and regression tests**

Run:

```bash
bash -n bin/tts-cn.sh
.venv/bin/python -m unittest tests.test_tts_cn tests.test_public_package -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the CLI and Keychain integration**

```bash
git add scripts/tts-cn.py bin/tts-cn.sh tests/test_tts_cn.py tests/test_public_package.py
git commit -m "feat: expose Qwen TTS quality mode"
```

---

### Task 5: Update the public Skill and documentation

**Files:**
- Modify: `README.md`
- Modify: `skills/podcast-translate/SKILL.md`
- Modify: `tests/test_public_package.py`

**Interfaces:**
- Documents: `edge` remains the default.
- Documents: `--provider qwen` selects translation; `--tts qwen3-api` selects speech synthesis.
- Documents: Qwen synthesis consumes the user's DashScope quota and reuses the existing Keychain setup.
- Documents: cache location and safe deletion behavior.

- [ ] **Step 1: Add failing public-documentation assertions**

Extend `tests/test_public_package.py`:

```python
def test_readme_documents_qwen_tts_without_changing_default(self) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    self.assertIn("--tts qwen3-api", readme)
    self.assertIn("--tts edge", readme)
    self.assertIn("EdgeTTS 仍是默认", readme)
    self.assertIn(".tts-cache", readme)

def test_skill_distinguishes_translation_provider_from_tts_backend(self) -> None:
    skill = (ROOT / "skills/podcast-translate/SKILL.md").read_text(
        encoding="utf-8"
    )
    self.assertIn("--provider qwen", skill)
    self.assertIn("--tts qwen3-api", skill)
    self.assertIn("千问翻译", skill)
    self.assertIn("千问语音", skill)
```

- [ ] **Step 2: Run documentation tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_public_package -v
```

Expected: the two new documentation tests FAIL.

- [ ] **Step 3: Update README with copy-pasteable commands and boundaries**

Add a “语音质量模式” section containing these commands:

```bash
# 免费稳定模式，也是默认值
bash bin/tts-cn.sh transcript.md --speakers transcript.speakers.txt --tts edge

# 千问高质量模式，使用已有的百炼 Keychain 配置
bash bin/tts-cn.sh transcript.md --speakers transcript.speakers.txt \
  --tts qwen3-api --style conversation

# 文章或论文的平静讲解模式
bash bin/tts-cn.sh article.md --tts qwen3-api --style documentary
```

State explicitly:

- EdgeTTS 仍是默认 and does not require a TTS API key.
- Qwen translation and Qwen speech are independent choices.
- `qwen3-api` consumes the user's DashScope quota.
- successful fragments are stored under the output directory's `.tts-cache/` and reused on retry;
- deleting `.tts-cache/` is safe but causes future runs to synthesize and charge again;
- no output is published if one fragment is missing or corrupt.

- [ ] **Step 4: Update Skill routing without making Qwen ambiguous**

In `skills/podcast-translate/SKILL.md`:

1. Keep “千问翻译” mapped to `--provider qwen`.
2. Map explicit “千问语音”, “Qwen3-TTS”, “高质量音质”, or “用千问合成声音” requests to `--tts qwen3-api`.
3. If the user says only “用千问翻译这个播客”, use Qwen translation and Edge speech.
4. If the user explicitly asks for both, run translation with `--provider qwen` and synthesis with `--tts qwen3-api`.
5. Change the TTS step to show both commands and continue to default to Edge.
6. Add `--preview-paragraphs` only to the internal A/B validation section, not the normal user flow.
7. Preserve the instruction never to ask users to paste API keys into chat.

- [ ] **Step 5: Run public-package tests and secret scan**

Run:

```bash
python3 -m unittest tests.test_public_package -v
rg -n 'sk-[A-Za-z0-9]{20,}|Authorization: Bearer [^$]' README.md skills scripts bin tests requirements-podcast.txt
```

Expected: tests PASS and `rg` returns no secret-like literal.

- [ ] **Step 6: Commit documentation and Skill routing**

```bash
git add README.md skills/podcast-translate/SKILL.md tests/test_public_package.py
git commit -m "docs: explain Qwen TTS quality mode"
```

---

### Task 6: Verify the full package and produce real A/B listening samples

**Files:**
- Verify: all tracked files
- Runtime only, never commit: `/Users/liuyuxin/Desktop/音频/Qwen3音质对比/`
- Source only, never modify or commit: three existing private transcripts under `/Users/liuyuxin/Desktop/音频/`

**Interfaces:**
- Consumes: the complete CLI and cache from Tasks 1-5.
- Produces: six short MP3 files, three blind A/B pairs, plus runtime and token observations.
- Produces: a user decision on whether Qwen becomes the recommended quality mode; Edge remains the default until that decision.

- [ ] **Step 1: Run the full automated verification suite**

Run:

```bash
.venv/bin/pip install -r requirements-podcast.txt
make test
git diff --check
git status --short
```

Expected: `All tests passed.`, no whitespace errors, and only intentional implementation changes are present before their commits. After the prior task commits, the worktree is clean.

- [ ] **Step 2: Confirm Keychain availability without printing the secret**

Run:

```bash
security find-generic-password -a "${USER:-$(id -un)}" -s "podcast-cn-qwen" >/dev/null
```

Expected: exit 0. If it fails, run `bash bin/setup-podcast-model.sh qwen` in the user's terminal so the key is entered through hidden input. Never print or copy the key into the task.

This presence check does not prove that the stored key was rotated. Before continuing to Step 3, require confirmation that the previously exposed key was revoked and that `podcast-cn-qwen` now contains its replacement. If not, stop before making any paid API request and complete the rotation first.

- [ ] **Step 3: Run a two-paragraph real Qwen smoke test**

Run:

```bash
mkdir -p "/Users/liuyuxin/Desktop/音频/Qwen3音质对比"
bash bin/tts-cn.sh "/Users/liuyuxin/Desktop/音频/健康日常的科学公式.md" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/冒烟测试.mp3" \
  --tts qwen3-api --style documentary --preview-paragraphs 2
ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/冒烟测试.mp3"
```

Expected: Qwen reports zero failed fragments; `ffprobe` prints a positive duration; the log contains no key or signed URL.

- [ ] **Step 4: Prove resume avoids a second paid synthesis**

Run the exact smoke-test command again.

Expected: every fragment is reported as a cache hit, provider token totals are zero for the second run, and the final MP3 still has a positive duration.

- [ ] **Step 5: Generate three matched Edge/Qwen pairs**

Use 10 preview paragraphs for each source so both providers receive identical cleaned text and speaker labels.

Pair 1, two-person business interview:

```bash
bash bin/tts-cn.sh "/Users/liuyuxin/Desktop/音频/2026企业AI现状-Aaron Levie访谈.md" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/企业AI-edge.mp3" \
  --speakers "/Users/liuyuxin/Desktop/音频/2026企业AI现状-Aaron Levie访谈.speakers.txt" \
  --tts edge --style conversation --preview-paragraphs 10
bash bin/tts-cn.sh "/Users/liuyuxin/Desktop/音频/2026企业AI现状-Aaron Levie访谈.md" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/企业AI-qwen.mp3" \
  --speakers "/Users/liuyuxin/Desktop/音频/2026企业AI现状-Aaron Levie访谈.speakers.txt" \
  --tts qwen3-api --style conversation --preview-paragraphs 10
```

Pair 2, technical interview with English proper nouns:

```bash
bash bin/tts-cn.sh "/Users/liuyuxin/Desktop/音频/Gemini联席负责人谈世界模型与强化学习.md" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/Gemini技术访谈-edge.mp3" \
  --speakers "/Users/liuyuxin/Desktop/音频/Gemini联席负责人谈世界模型与强化学习.speakers.txt" \
  --tts edge --style conversation --preview-paragraphs 10
bash bin/tts-cn.sh "/Users/liuyuxin/Desktop/音频/Gemini联席负责人谈世界模型与强化学习.md" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/Gemini技术访谈-qwen.mp3" \
  --speakers "/Users/liuyuxin/Desktop/音频/Gemini联席负责人谈世界模型与强化学习.speakers.txt" \
  --tts qwen3-api --style conversation --preview-paragraphs 10
```

Pair 3, single-narrator knowledge explanation:

```bash
bash bin/tts-cn.sh "/Users/liuyuxin/Desktop/音频/健康日常的科学公式.md" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/健康知识-edge.mp3" \
  --tts edge --style documentary --preview-paragraphs 10
bash bin/tts-cn.sh "/Users/liuyuxin/Desktop/音频/健康日常的科学公式.md" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/健康知识-qwen.mp3" \
  --tts qwen3-api --style documentary --preview-paragraphs 10
```

Expected: six complete MP3 files, no failed fragments, and each matched pair has the same source paragraph count.

- [ ] **Step 6: Present blind A/B pairs and collect the user's choice**

Before sharing, make neutral copies with a different provider position across samples:

```bash
cp "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/企业AI-qwen.mp3" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/样本1-A.mp3"
cp "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/企业AI-edge.mp3" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/样本1-B.mp3"
cp "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/Gemini技术访谈-edge.mp3" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/样本2-A.mp3"
cp "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/Gemini技术访谈-qwen.mp3" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/样本2-B.mp3"
cp "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/健康知识-qwen.mp3" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/样本3-A.mp3"
cp "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/健康知识-edge.mp3" \
  "/Users/liuyuxin/Desktop/音频/Qwen3音质对比/样本3-B.mp3"
```

Keep the provider mapping out of the user-facing message until the user chooses.

Ask the user to select A or B for each sample and optionally note:

- naturalness;
- proper-noun pronunciation;
- speaker consistency;
- pacing and listening fatigue.

The gate passes if the user prefers Qwen in at least two of three samples and no Qwen sample has a severe omission, mispronunciation, or voice drift.

- [ ] **Step 7: Record the decision in README without changing the safe default automatically**

If the gate passes, add one dated sentence under the quality section: `2026-07：三组真实试听中，Qwen3-TTS 至少两组胜出，因此作为推荐高质量模式；EdgeTTS 仍是默认免费模式。`

If the gate does not pass, add: `2026-07：Qwen3-TTS 真实试听未达到推荐闸门，暂保留为实验模式；EdgeTTS 仍是默认。`

Run:

```bash
python3 -m unittest tests.test_public_package -v
git diff --check
```

Expected: tests PASS and no whitespace errors.

- [ ] **Step 8: Commit the A/B decision and verify a clean worktree**

```bash
git add README.md
git commit -m "docs: record Qwen TTS listening result"
make test
git status --short
```

Expected: `All tests passed.` and an empty `git status --short`. Generated audio and private transcripts remain outside Git and are not committed.

---

## Completion Gate

Project A is complete only when all of the following are true:

- Existing Edge commands still work unchanged and Edge remains the default.
- `--tts qwen3-api` produces a complete MP3 using Qwen3-TTS Instruct Flash.
- Qwen and Edge fragments are never mixed silently.
- A failed or corrupt fragment never replaces an existing complete MP3.
- A repeated run reuses decodable cached fragments and avoids repeated Qwen calls.
- Keychain loading works without exposing `DASHSCOPE_API_KEY`.
- All automated tests pass without network access.
- A real API smoke test passes.
- Three real A/B pairs are generated and reviewed by the user.
- The user preference result is recorded before Project B planning starts.
