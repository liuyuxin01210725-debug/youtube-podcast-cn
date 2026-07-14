"""Provider-specific speech synthesis behind a small common interface."""

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
