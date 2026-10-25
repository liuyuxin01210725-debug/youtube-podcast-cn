"""Provider-specific speech synthesis behind a small common interface."""

from __future__ import annotations

import asyncio
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import edge_tts


VOICE_SLOTS = (
    "default",
    "male_1",
    "male_2",
    "male_3",
    "female_1",
    "female_2",
)
STYLE_NAMES = ("conversation", "documentary")

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

VOICE_SLOT_INSTRUCTIONS = {
    "male_1": "使用清晰、自然、可辨识的成年男性声线；保持男性音色，避免偏女性或童声。",
    "male_2": "使用清晰、自然、可辨识的成年男性声线；保持男性音色，避免偏女性或童声。",
    "male_3": "使用清晰、自然、可辨识的成年男性声线；保持男性音色，避免偏女性或童声。",
}

# Only voices used by this project (plus the previously shipped Dylan) are
# catalogued here. Unknown values are left available for custom/future voices,
# while known system voices are checked against the selected model family.
_QWEN_SYSTEM_VOICE_GENDERS = {
    "Cherry": "female",
    "Serena": "female",
    "Chelsie": "female",
    "Ethan": "male",
    "Kai": "male",
    "Dylan": "male",
    "Vincent": "male",
    "Neil": "male",
    "Eldric Sage": "male",
}

_QWEN_SYSTEM_VOICE_MODEL_FAMILIES = {
    "Cherry": {"qwen3-tts-instruct-flash", "qwen3-tts-flash", "qwen-tts"},
    "Serena": {"qwen3-tts-instruct-flash", "qwen3-tts-flash", "qwen-tts"},
    "Chelsie": {"qwen3-tts-instruct-flash", "qwen3-tts-flash", "qwen-tts"},
    "Ethan": {"qwen3-tts-instruct-flash", "qwen3-tts-flash", "qwen-tts"},
    "Kai": {"qwen3-tts-instruct-flash", "qwen3-tts-flash"},
    "Dylan": {"qwen3-tts-flash", "qwen-tts"},
    "Vincent": {"qwen3-tts-instruct-flash", "qwen3-tts-flash"},
    "Neil": {"qwen3-tts-instruct-flash", "qwen3-tts-flash"},
    "Eldric Sage": {"qwen3-tts-instruct-flash", "qwen3-tts-flash"},
}

_QWEN_MODEL_FAMILIES = (
    "qwen3-tts-instruct-flash",
    "qwen3-tts-flash",
    "qwen-tts",
)


class TTSBackendError(RuntimeError):
    """Safe-to-log provider error with sensitive details removed."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


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
    male_1 = os.getenv(
        "VOICE_MALE_1", legacy_male or "zh-CN-YunyangNeural"
    )
    female_1 = os.getenv(
        "VOICE_FEMALE_1", legacy_female or "zh-CN-XiaoxiaoNeural"
    )
    return {
        "default": os.getenv("VOICE_DEFAULT", female_1),
        "male_1": male_1,
        "male_2": os.getenv("VOICE_MALE_2", "zh-CN-YunjianNeural"),
        "male_3": os.getenv("VOICE_MALE_3", "zh-CN-YunxiNeural"),
        "female_1": female_1,
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
        try:
            await communicate.save(str(output))
        except Exception as error:
            message = str(error)
            lowered = message.casefold()
            if "403" in message or "invalid response status" in lowered:
                raise TTSBackendError(
                    "系统音色服务暂时拒绝连接（常见于 edge-tts 过旧）。请升级到 edge-tts>=7.3.1 后重试："
                    ".venv/bin/pip install -U 'edge-tts>=7.3.1'"
                ) from error
            raise
        return SynthesisUsage()


def _qwen_voice_map() -> dict[str, str]:
    return {
        "default": os.getenv("QWEN_TTS_VOICE_DEFAULT", "Cherry"),
        "male_1": os.getenv("QWEN_TTS_VOICE_MALE_1", "Vincent"),
        "male_2": os.getenv("QWEN_TTS_VOICE_MALE_2", "Neil"),
        "male_3": os.getenv("QWEN_TTS_VOICE_MALE_3", "Eldric Sage"),
        "female_1": os.getenv("QWEN_TTS_VOICE_FEMALE_1", "Cherry"),
        "female_2": os.getenv("QWEN_TTS_VOICE_FEMALE_2", "Serena"),
    }


def _qwen_model_family(model: str) -> str | None:
    for family in _QWEN_MODEL_FAMILIES:
        if model == family or model.startswith(f"{family}-"):
            return family
    return None


def _validate_qwen_voice_map(model: str, voice_map: dict[str, str]) -> None:
    family = _qwen_model_family(model)
    for slot, voice in voice_map.items():
        gender = _QWEN_SYSTEM_VOICE_GENDERS.get(voice)
        if slot.startswith("male_") and gender == "female":
            raise ValueError(f"voice slot {slot} 不能使用女性音色 {voice}")
        if slot.startswith("female_") and gender == "male":
            raise ValueError(f"voice slot {slot} 不能使用男性音色 {voice}")

        supported_families = _QWEN_SYSTEM_VOICE_MODEL_FAMILIES.get(voice)
        if (
            family is not None
            and supported_families is not None
            and family not in supported_families
        ):
            raise ValueError(f"音色 {voice} 不支持模型 {model}（voice slot: {slot}）")


def _qwen_instructions(request: SynthesisRequest) -> str:
    return STYLE_INSTRUCTIONS[request.style] + VOICE_SLOT_INSTRUCTIONS.get(
        request.voice_slot, ""
    )


def _load_dashscope():
    import dashscope

    return dashscope


def _field(container, name: str, default=None):
    if isinstance(container, dict):
        return container.get(name, default)
    return getattr(container, name, default)


def _download_audio(url: str, output: Path) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise TTSBackendError("Qwen 返回了不支持的音频地址")
    if parsed.scheme == "http" and (parsed.hostname or "").endswith(
        ".aliyuncs.com"
    ):
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


_QWEN_API_HOSTS = {
    "dashscope.aliyuncs.com",
    "dashscope-intl.aliyuncs.com",
}


def _validate_qwen_base_url(base_url: str) -> str:
    """Accept only official DashScope HTTPS API endpoints."""
    try:
        parsed = urllib.parse.urlparse(base_url)
        port = parsed.port
    except ValueError:
        raise ValueError(
            "QWEN_TTS_BASE_URL 必须使用官方 DashScope HTTPS 地址"
        ) from None

    if (
        parsed.scheme != "https"
        or parsed.hostname not in _QWEN_API_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.params)
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ValueError(
            "QWEN_TTS_BASE_URL 必须使用官方 DashScope HTTPS 地址"
        )
    return base_url


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
        self.base_url = _validate_qwen_base_url(
            os.getenv(
                "QWEN_TTS_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
            )
        )
        self.voice_map = _qwen_voice_map()
        if voice_map:
            self.voice_map.update(voice_map)
        _validate_qwen_voice_map(self.model, self.voice_map)
        self.concurrency = int(os.getenv("QWEN_TTS_CONCURRENCY", "2"))

    def resolve_voice(self, voice_slot: str) -> str:
        if voice_slot not in VOICE_SLOTS:
            raise ValueError(f"未知 voice slot: {voice_slot}")
        return self.voice_map[voice_slot]

    def fingerprint(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "voices": self.voice_map,
            "styles": STYLE_INSTRUCTIONS,
            "voice_slot_instructions": VOICE_SLOT_INSTRUCTIONS,
            "max_chars": self.max_chars,
            "language_type": "Chinese",
            "optimize_instructions": True,
        }

    async def synthesize(
        self, request: SynthesisRequest, output: Path
    ) -> SynthesisUsage:
        try:
            dashscope = _load_dashscope()
            dashscope.base_http_api_url = self.base_url
            response = await asyncio.to_thread(
                dashscope.MultiModalConversation.call,
                api_key=self._api_key,
                model=self.model,
                text=request.text,
                voice=self.resolve_voice(request.voice_slot),
                language_type="Chinese",
                instructions=_qwen_instructions(request),
                optimize_instructions=True,
                stream=False,
            )
        except Exception as exc:
            raise TTSBackendError("Qwen TTS 请求失败") from exc

        status_code = _field(response, "status_code")
        request_id = _field(response, "request_id")
        provider_code = _field(response, "code")
        if status_code != 200:
            retryable = status_code in {408, 429} or (
                isinstance(status_code, int) and status_code >= 500
            )
            raise TTSBackendError(
                "Qwen TTS 请求失败"
                f"(status={status_code}, code={provider_code or '-'}, "
                f"request_id={request_id or '-'})",
                retryable=retryable,
            )

        output_data = _field(response, "output", {}) or {}
        audio_data = _field(output_data, "audio", {}) or {}
        audio_url = _field(audio_data, "url")
        if not audio_url:
            raise TTSBackendError(
                f"Qwen TTS 未返回音频(request_id={request_id or '-'})"
            )

        try:
            await asyncio.to_thread(_download_audio, audio_url, output)
        except Exception as exc:
            raise TTSBackendError("Qwen 音频下载失败") from exc

        usage = _field(response, "usage", {}) or {}
        output_details = _field(usage, "output_tokens_details", {}) or {}
        return SynthesisUsage(
            request_id=request_id,
            input_tokens=int(_field(usage, "input_tokens", 0) or 0),
            output_tokens=int(_field(usage, "output_tokens", 0) or 0),
            audio_tokens=int(_field(output_details, "audio_tokens", 0) or 0),
        )


def build_backend(name: str, api_key: str | None = None) -> TTSBackend:
    if name == "edge":
        return EdgeTTSBackend()
    if name == "qwen3-api":
        return Qwen3APIBackend(api_key=api_key or "")
    raise ValueError(f"不支持的 TTS 后端: {name}")
