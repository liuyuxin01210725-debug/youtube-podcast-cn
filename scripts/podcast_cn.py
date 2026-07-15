#!/usr/bin/env python3
"""Prepare a Chinese podcast transcript from one exact YouTube URL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from diarization_cache import (
    DiarizationCache,
    load_or_create_speaker_map,
    render_speaker_sequence,
)
from diarization_core import (
    DiarizationBackend,
    DiarizationTurn,
    SpeakerBounds,
    align_caption_pieces,
    validate_diarization,
)
from diarization_runtime import (
    DiarizationRuntime,
    DiarizationSetupError,
    backend_fingerprint,
    ensure_diarization_runtime,
    load_model_manifest,
    run_sherpa_backend,
)

from podcast_captions import (
    CaptionPiece,
    CaptionUnavailableError,
    PodcastPipelineError,
    SubtitleSegment,
    group_caption_pieces,
    parse_vtt,
    parse_vtt_pieces,
)
from temporary_audio import (
    TemporaryAudioCleanupError,
    with_temporary_audio,
)


Runner = Callable[..., subprocess.CompletedProcess[str]]


class TranslationValidationError(PodcastPipelineError):
    """Raised when a translation response cannot be matched to its source."""


class TranslationConfigurationError(PodcastPipelineError):
    """Raised when a translation provider is unsupported or unconfigured."""


class DeepSeekConfigurationError(TranslationConfigurationError):
    """Backward-compatible DeepSeek configuration error."""


class DiarizationBackendError(PodcastPipelineError):
    """Raised when required local diarization cannot complete safely."""


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    display_name: str
    api_key_env: str
    model_env: str
    base_url_env: str
    default_model: str
    default_base_url: str
    keychain_service: str


_PROVIDER_CONFIGS = {
    "deepseek": ProviderConfig(
        name="deepseek",
        display_name="DeepSeek",
        api_key_env="DEEPSEEK_API_KEY",
        model_env="DEEPSEEK_MODEL",
        base_url_env="DEEPSEEK_BASE_URL",
        default_model="deepseek-chat",
        default_base_url="https://api.deepseek.com",
        keychain_service="podcast-cn-deepseek",
    ),
    "qwen": ProviderConfig(
        name="qwen",
        display_name="通义千问",
        api_key_env="DASHSCOPE_API_KEY",
        model_env="QWEN_MODEL",
        base_url_env="QWEN_BASE_URL",
        default_model="qwen-plus",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        keychain_service="podcast-cn-qwen",
    ),
}


def get_provider_config(name: str) -> ProviderConfig:
    """Return one supported translation provider configuration."""
    try:
        return _PROVIDER_CONFIGS[name.casefold()]
    except (AttributeError, KeyError):
        supported = ", ".join(_PROVIDER_CONFIGS)
        raise TranslationConfigurationError(
            f"不支持翻译模型供应商 {name!r};当前只支持 {supported}"
        ) from None


@dataclass(frozen=True)
class VideoMetadata:
    video_id: str
    source_title: str
    webpage_url: str
    channel: str
    duration_seconds: float | None


@dataclass(frozen=True)
class DiarizationReport:
    status: Literal["success", "cache_hit", "fallback", "off"]
    backend: str | None
    detected_speakers: int
    aligned_segments: int
    ambiguous_segments: int
    temporary_audio: Literal["deleted", "not_created"]
    warning: str | None = None


@dataclass(frozen=True)
class PreparedEpisode:
    video_id: str
    source_title: str
    transcript_path: Path
    state_path: Path
    speakers_path: Path | None = None
    speaker_map_path: Path | None = None
    diarization_report: DiarizationReport | None = None


class SherpaDiarizationBackend:
    """Lazy production adapter whose fingerprint needs no model installation."""

    name = "sherpa-onnx"

    def __init__(self, runner: Runner = subprocess.run) -> None:
        manifest = load_model_manifest()
        self.model_fingerprint = backend_fingerprint(manifest)
        self._runner = runner
        self._runtime: DiarizationRuntime | None = None

    def prepare(self) -> None:
        if self._runtime is None:
            self._runtime = ensure_diarization_runtime(runner=self._runner)

    def diarize(
        self, audio_path: Path, speaker_bounds: SpeakerBounds
    ) -> list[DiarizationTurn]:
        self.prepare()
        if self._runtime is None:
            raise DiarizationSetupError("声纹运行时未就绪")
        return run_sherpa_backend(
            audio_path,
            speaker_bounds,
            runtime=self._runtime,
            runner=self._runner,
        )


def _english_track_key(tracks: object) -> str | None:
    if not isinstance(tracks, dict):
        return None
    available = [
        str(key)
        for key, formats in tracks.items()
        if formats and (str(key).casefold() == "en" or str(key).casefold().startswith("en-"))
    ]
    if not available:
        return None
    return sorted(available, key=lambda key: (key.casefold() != "en", key.casefold()))[0]


def select_english_track(info: dict[str, Any]) -> tuple[str, bool]:
    """Return (language key, is_automatic), preferring manual captions."""
    manual = _english_track_key(info.get("subtitles"))
    if manual is not None:
        return manual, False
    automatic = _english_track_key(info.get("automatic_captions"))
    if automatic is not None:
        return automatic, True
    raise CaptionUnavailableError("这个视频没有英文字幕")


def fetch_video_info(url: str, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Fetch yt-dlp metadata for one exact video URL without media."""
    args = [
        "yt-dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        url,
    ]
    try:
        completed = runner(args, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise PodcastPipelineError("找不到 yt-dlp,请先安装") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()[-500:]
        raise PodcastPipelineError(f"读取 YouTube 信息失败:{detail}") from error

    try:
        info = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise PodcastPipelineError("yt-dlp 返回的视频信息不是有效 JSON") from error
    if not isinstance(info, dict):
        raise PodcastPipelineError("yt-dlp 返回的视频信息格式无效")
    return info


def video_metadata_from_info(info: dict[str, Any]) -> VideoMetadata:
    """Keep only the small metadata record needed by the job state."""
    video_id = str(info.get("id") or "").strip()
    title = str(info.get("title") or "").strip()
    if not video_id or not title:
        raise PodcastPipelineError("视频信息缺少 ID 或标题")

    duration_value = info.get("duration")
    duration = float(duration_value) if duration_value is not None else None
    return VideoMetadata(
        video_id=video_id,
        source_title=title,
        webpage_url=str(info.get("webpage_url") or ""),
        channel=str(info.get("channel") or info.get("uploader") or ""),
        duration_seconds=duration,
    )


def download_english_subtitles(
    url: str,
    job_dir: Path,
    info: dict[str, Any],
    runner: Runner = subprocess.run,
) -> Path:
    """Download only the selected English VTT and normalize its filename."""
    language, is_automatic = select_english_track(info)
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / "subtitles.en.vtt"
    output_template = job_dir / "subtitles.%(ext)s"
    caption_flag = "--write-auto-subs" if is_automatic else "--write-subs"
    args = [
        "yt-dlp",
        "--skip-download",
        "--no-playlist",
        caption_flag,
        "--sub-langs",
        language,
        "--sub-format",
        "vtt",
        "--output",
        str(output_template),
        url,
    ]
    try:
        runner(args, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise PodcastPipelineError("找不到 yt-dlp,请先安装") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()[-500:]
        raise PodcastPipelineError(f"下载英文字幕失败:{detail}") from error

    expected = job_dir / f"subtitles.{language}.vtt"
    if expected.is_file() and expected != target:
        expected.replace(target)
    if not target.is_file():
        candidates = sorted(job_dir.glob("subtitles*.vtt"))
        if not candidates:
            raise PodcastPipelineError("yt-dlp 没有生成英文 VTT 字幕")
        candidates[0].replace(target)
    return target


def chunk_segments(
    segments: list[SubtitleSegment], max_chars: int = 8000
) -> list[list[SubtitleSegment]]:
    """Group whole segments into ordered character-bounded chunks."""
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")

    chunks: list[list[SubtitleSegment]] = []
    current: list[SubtitleSegment] = []
    current_chars = 0
    for segment in segments:
        segment_chars = len(segment.text)
        if current and current_chars + segment_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += segment_chars
    if current:
        chunks.append(current)
    return chunks


def validate_translation_payload(
    payload: object, expected_ids: list[str]
) -> dict[str, str]:
    """Validate a complete one-to-one structured translation response."""
    if not isinstance(payload, dict):
        raise TranslationValidationError("翻译结果必须是 JSON 对象")
    items = payload.get("translations")
    if not isinstance(items, list):
        raise TranslationValidationError("翻译结果缺少 translations 数组")

    translated: dict[str, str] = {}
    actual_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise TranslationValidationError("translations 中存在非对象条目")
        segment_id = item.get("id")
        text = item.get("text")
        if not isinstance(segment_id, str) or not segment_id:
            raise TranslationValidationError("翻译条目缺少有效 id")
        if segment_id in translated:
            raise TranslationValidationError(f"翻译结果包含重复 id:{segment_id}")
        if not isinstance(text, str) or not text.strip():
            raise TranslationValidationError(f"翻译结果为空:{segment_id}")
        actual_ids.append(segment_id)
        translated[segment_id] = text.strip()

    if actual_ids != expected_ids:
        missing = [segment_id for segment_id in expected_ids if segment_id not in translated]
        extra = [segment_id for segment_id in actual_ids if segment_id not in expected_ids]
        raise TranslationValidationError(
            f"翻译 ID 无法与原文逐段对应;缺少 {missing};多出 {extra}"
        )
    return translated


_TRANSLATION_SYSTEM_PROMPT = """你是英文播客的忠实中文翻译。
把每段英文翻译成自然、适合朗读的简体中文。不得总结、删减、合并段落或添加原文没有的事实。
必须保留每个 id,并严格按输入顺序返回 JSON 对象:
{"translations":[{"id":"seg-000001","text":"中文译文"}]}。
只返回 JSON,不要解释。"""


class OpenAICompatibleTranslationClient:
    """OpenAI-compatible translation client with strict ID validation."""

    def __init__(
        self,
        provider: str | ProviderConfig,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        config = (
            get_provider_config(provider)
            if isinstance(provider, str)
            else provider
        )
        resolved_key = api_key or os.getenv(config.api_key_env)
        if not resolved_key:
            error_type = (
                DeepSeekConfigurationError
                if config.name == "deepseek"
                else TranslationConfigurationError
            )
            raise error_type(
                f"缺少 {config.api_key_env},请先配置 {config.display_name} API Key"
            )
        resolved_base_url = (
            base_url
            or os.getenv(config.base_url_env)
            or config.default_base_url
        )
        self.config = config
        self.provider = config.name
        self._api_key = resolved_key
        self.model = model or os.getenv(config.model_env) or config.default_model
        self.base_url = resolved_base_url.rstrip("/")
        self._opener = opener

    @property
    def translation_identity(self) -> str:
        return f"{self.provider}|{self.model}|{self.base_url}"

    def translate(self, segments: list[SubtitleSegment]) -> dict[str, str]:
        expected_ids = [segment.id for segment in segments]
        source_payload = [
            {"id": segment.id, "text": segment.text} for segment in segments
        ]
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _TRANSLATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(source_payload, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=120) as response:
                response_bytes = response.read()
        except Exception:
            raise PodcastPipelineError(
                f"{self.config.display_name} 翻译请求失败,可直接重跑继续"
            ) from None

        try:
            response_payload = json.loads(response_bytes.decode("utf-8"))
            choices = response_payload["choices"]
            content = choices[0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content is not text")
            cleaned = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.I)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            translation_payload = json.loads(cleaned)
        except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise TranslationValidationError(
                f"{self.config.display_name} 返回的翻译 JSON 无法解析"
            ) from None

        return validate_translation_payload(translation_payload, expected_ids)


class DeepSeekClient(OpenAICompatibleTranslationClient):
    """Backward-compatible DeepSeek client wrapper."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        super().__init__(
            "deepseek",
            api_key=api_key,
            model=model,
            base_url=base_url,
            opener=opener,
        )


def create_translation_client(
    provider: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> OpenAICompatibleTranslationClient:
    return OpenAICompatibleTranslationClient(
        provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        opener=opener,
    )


def _source_hash(segments: list[SubtitleSegment]) -> str:
    source = [
        {"id": segment.id, "text": segment.text} for segment in segments
    ]
    canonical = json.dumps(
        source, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _load_valid_chunk_cache(
    path: Path,
    chunk_index: int,
    segments: list[SubtitleSegment],
    source_hash: str,
    translation_identity: str,
) -> dict[str, str] | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            return None
        source_ids = [segment.id for segment in segments]
        if (
            cached.get("schema_version") != 2
            or cached.get("chunk_index") != chunk_index
            or cached.get("source_hash") != source_hash
            or cached.get("source_ids") != source_ids
            or cached.get("translation_identity") != translation_identity
        ):
            return None
        return validate_translation_payload(
            {"translations": cached.get("translations")}, source_ids
        )
    except (OSError, json.JSONDecodeError, TranslationValidationError):
        return None


def translate_with_resume(
    segments: list[SubtitleSegment],
    chunk_dir: Path,
    translator: Callable[[list[SubtitleSegment]], dict[str, str]],
    max_chars: int = 8000,
    translation_identity: str = "custom:test",
) -> dict[str, str]:
    """Translate chunks with validated, atomic checkpoints for resuming."""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    merged: dict[str, str] = {}
    for chunk_index, chunk in enumerate(
        chunk_segments(segments, max_chars=max_chars), start=1
    ):
        source_ids = [segment.id for segment in chunk]
        source_hash = _source_hash(chunk)
        cache_path = chunk_dir / f"chunk-{chunk_index:04d}.json"
        translated = _load_valid_chunk_cache(
            cache_path,
            chunk_index,
            chunk,
            source_hash,
            translation_identity,
        )
        if translated is None:
            raw_translated = translator(chunk)
            if not isinstance(raw_translated, dict):
                raise TranslationValidationError("翻译器必须返回 ID 到译文的对象")
            translated = validate_translation_payload(
                {
                    "translations": [
                        {"id": segment_id, "text": text}
                        for segment_id, text in raw_translated.items()
                    ]
                },
                source_ids,
            )
            cache_payload = {
                "schema_version": 2,
                "chunk_index": chunk_index,
                "source_hash": source_hash,
                "source_ids": source_ids,
                "translation_identity": translation_identity,
                "translations": [
                    {"id": segment_id, "text": translated[segment_id]}
                    for segment_id in source_ids
                ],
            }
            _write_json_atomic(cache_path, cache_payload)
        merged.update(translated)

    expected_ids = [segment.id for segment in segments]
    if list(merged) != expected_ids:
        raise TranslationValidationError("合并后的翻译顺序与原字幕不一致")
    return merged


def _write_job_state(
    state_path: Path,
    phase: str,
    url: str,
    metadata: VideoMetadata,
) -> None:
    _write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "phase": phase,
            "video_id": metadata.video_id,
            "source_title": metadata.source_title,
            "source_url": url,
        },
    )


def _write_subtitle_jsonl(path: Path, segments: list[SubtitleSegment]) -> None:
    text = "".join(
        json.dumps(asdict(segment), ensure_ascii=False) + "\n"
        for segment in segments
    )
    _write_text_atomic(path, text)


def _render_chinese_transcript(
    metadata: VideoMetadata,
    source_url: str,
    segments: list[SubtitleSegment],
    translations: dict[str, str],
) -> str:
    lines = [
        "# 中文逐字稿",
        "",
        f"> 英文标题:{metadata.source_title}",
        f"> 来源:{source_url}",
        "",
    ]
    for segment in segments:
        lines.extend(
            [
                f"<!-- {segment.id} -->",
                translations[segment.id],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _transcript_is_complete(path: Path, segments: list[SubtitleSegment]) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return all(f"<!-- {segment.id} -->" in text for segment in segments)


def _translation_metadata_matches(
    path: Path,
    source_hash: str,
    translation_identity: str,
) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and payload.get("source_hash") == source_hash
        and payload.get("translation_identity") == translation_identity
    )


def _translation_metadata_payload(
    source_hash: str,
    translation_identity: str,
) -> dict[str, str | int | None]:
    identity_parts = translation_identity.split("|", 2)
    provider = identity_parts[0] if identity_parts else "custom"
    model = identity_parts[1] if len(identity_parts) >= 2 else None
    base_url = identity_parts[2] if len(identity_parts) >= 3 else None
    return {
        "schema_version": 1,
        "source_hash": source_hash,
        "translation_identity": translation_identity,
        "provider": provider,
        "model": model,
        "base_url": base_url,
    }


def resolve_speaker_bounds(
    speaker_count: int | None,
    minimum: int | None,
    maximum: int | None,
) -> SpeakerBounds:
    """Resolve CLI speaker options while rejecting ambiguous combinations."""
    if speaker_count is not None and (minimum is not None or maximum is not None):
        raise ValueError(
            "--speaker-count 不能同时与 --min-speakers/--max-speakers 使用"
        )
    if speaker_count is not None:
        return SpeakerBounds(exact=speaker_count)
    return SpeakerBounds(
        minimum=1 if minimum is None else minimum,
        maximum=6 if maximum is None else maximum,
    )


def _safe_diarization_warning(error: BaseException) -> str:
    text = re.sub(r"https?://\S+", "[远程地址已隐藏]", str(error))
    return text.strip()[-300:] or "本地声纹识别失败"


def _speaker_lines(path: Path) -> list[str]:
    try:
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as error:
        raise DiarizationBackendError("无法读取说话人序列") from error


def _verify_speaker_sequence(path: Path, segments: list[SubtitleSegment]) -> None:
    if len(_speaker_lines(path)) != len(segments):
        raise DiarizationBackendError("speaker 序列与最终段落数不一致")


def prepare_episode(
    url: str,
    output_root: Path,
    runner: Runner = subprocess.run,
    translator: Callable[[list[SubtitleSegment]], dict[str, str]] | None = None,
    provider: str = "deepseek",
    translation_identity: str | None = None,
    diarize: Literal["auto", "off", "required"] = "auto",
    speaker_bounds: SpeakerBounds | None = None,
    diarization_backend: DiarizationBackend | None = None,
) -> PreparedEpisode:
    """Prepare a resumable Chinese transcript job for one YouTube URL."""
    if diarize not in {"auto", "off", "required"}:
        raise ValueError("diarize 必须是 auto、off 或 required")
    resolved_bounds = speaker_bounds or SpeakerBounds()
    info = fetch_video_info(url, runner=runner)
    metadata = video_metadata_from_info(info)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", metadata.video_id):
        raise PodcastPipelineError("视频 ID 格式异常,无法创建任务目录")

    output_root = output_root.expanduser()
    job_dir = output_root / ".podcast-jobs" / metadata.video_id
    job_dir.mkdir(parents=True, exist_ok=True)
    state_path = job_dir / "state.json"
    transcript_path = job_dir / "transcript.zh.md"
    sequence_path = job_dir / "transcript.zh.speakers.txt"
    speaker_map_path = job_dir / "speaker-map.json"

    _write_json_atomic(job_dir / "metadata.json", asdict(metadata))
    _write_job_state(state_path, "metadata_ready", url, metadata)

    vtt_path = job_dir / "subtitles.en.vtt"
    pieces: list[CaptionPiece] | None = None
    if vtt_path.is_file():
        try:
            pieces = parse_vtt_pieces(vtt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, CaptionUnavailableError):
            vtt_path.unlink(missing_ok=True)
    if pieces is None:
        vtt_path = download_english_subtitles(url, job_dir, info, runner=runner)
        pieces = parse_vtt_pieces(vtt_path.read_text(encoding="utf-8"))
    _write_job_state(state_path, "captions_ready", url, metadata)

    speakers_path: Path | None = None
    final_speaker_map_path: Path | None = None
    if diarize == "off":
        sequence_path.unlink(missing_ok=True)
        segments = group_caption_pieces(pieces)
        diarization_report = DiarizationReport(
            status="off",
            backend=None,
            detected_speakers=0,
            aligned_segments=0,
            ambiguous_segments=0,
            temporary_audio="not_created",
        )
    else:
        cache = DiarizationCache(job_dir)
        selected_backend = diarization_backend or SherpaDiarizationBackend(runner)
        backend_name = str(getattr(selected_backend, "name", "unknown"))
        fingerprint = getattr(selected_backend, "model_fingerprint", None)
        temporary_audio_status: Literal["deleted", "not_created"] = "not_created"
        try:
            if not isinstance(fingerprint, dict):
                raise DiarizationBackendError("声纹后端缺少有效模型指纹")
            if metadata.duration_seconds is None:
                raise DiarizationBackendError("YouTube 信息缺少音频时长")
            duration_ms = round(metadata.duration_seconds * 1000)
            if duration_ms <= 0:
                raise DiarizationBackendError("YouTube 音频时长无效")
            source_identity = {
                "video_id": metadata.video_id,
                "duration_ms": duration_ms,
            }
            turns = cache.load_turns(
                source_identity, resolved_bounds, fingerprint
            )
            cache_hit = turns is not None
            if turns is None:
                prepare_backend = getattr(selected_backend, "prepare", None)
                if callable(prepare_backend):
                    print("准备本地声纹模型", file=sys.stderr)
                    prepare_backend()
                print("下载临时原音频", file=sys.stderr)
                print("转换 16 kHz WAV", file=sys.stderr)

                def analyze_audio(wav_path: Path) -> list[DiarizationTurn]:
                    print("本地识别说话人", file=sys.stderr)
                    detected = selected_backend.diarize(wav_path, resolved_bounds)
                    validate_diarization(
                        detected,
                        duration_ms=duration_ms,
                        bounds=resolved_bounds,
                    )
                    return detected

                try:
                    temporary_result = with_temporary_audio(
                        url, job_dir, analyze_audio, runner=runner
                    )
                except TemporaryAudioCleanupError:
                    raise
                except Exception:
                    temporary_audio_status = "deleted"
                    print("删除临时原音频", file=sys.stderr)
                    raise
                else:
                    temporary_audio_status = "deleted"
                    print("删除临时原音频", file=sys.stderr)
                turns = temporary_result.value
                cache.store_turns(
                    source_identity,
                    resolved_bounds,
                    fingerprint,
                    turns,
                    temporary_result.source_audio_sha256,
                )
            validate_diarization(
                turns,
                duration_ms=duration_ms,
                bounds=resolved_bounds,
            )
            _write_job_state(state_path, "diarization_ready", url, metadata)

            print("对齐字幕", file=sys.stderr)
            assignments = cache.load_assignments(pieces)
            if assignments is None:
                assignments = align_caption_pieces(pieces, turns)
                validate_diarization(
                    turns,
                    duration_ms=duration_ms,
                    bounds=resolved_bounds,
                    assignments=assignments,
                )
                cache.store_assignments(pieces, assignments)
            else:
                validate_diarization(
                    turns,
                    duration_ms=duration_ms,
                    bounds=resolved_bounds,
                    assignments=assignments,
                )
            if any(item.speaker_id is None for item in assignments):
                raise DiarizationBackendError("仍有字幕无法分配到说话人")
            _write_job_state(state_path, "speaker_alignment_ready", url, metadata)

            speakers = {
                item.source_index: item.speaker_id
                for item in assignments
                if item.speaker_id is not None
            }
            ambiguous_indexes = {
                item.source_index for item in assignments if item.ambiguous
            }
            segments = group_caption_pieces(
                pieces,
                speakers,
                ambiguous_indexes,
            )
            speaker_ids = list(dict.fromkeys(turn.speaker_id for turn in turns))
            speaker_map = load_or_create_speaker_map(
                speaker_map_path, speaker_ids
            )
            labels = render_speaker_sequence(segments, speaker_map)
            _write_text_atomic(sequence_path, "\n".join(labels) + "\n")
            _verify_speaker_sequence(sequence_path, segments)
            _write_job_state(state_path, "speaker_sequence_ready", url, metadata)
            speakers_path = sequence_path
            final_speaker_map_path = speaker_map_path
            diarization_report = DiarizationReport(
                status="cache_hit" if cache_hit else "success",
                backend=backend_name,
                detected_speakers=len(set(speaker_ids)),
                aligned_segments=len(segments),
                ambiguous_segments=sum(segment.ambiguous for segment in segments),
                temporary_audio=temporary_audio_status,
            )
        except TemporaryAudioCleanupError as error:
            raise DiarizationBackendError(str(error)) from error
        except Exception as error:
            if diarize == "required":
                if isinstance(error, DiarizationBackendError):
                    raise
                raise DiarizationBackendError(
                    _safe_diarization_warning(error)
                ) from error
            sequence_path.unlink(missing_ok=True)
            segments = group_caption_pieces(pieces)
            speakers_path = None
            final_speaker_map_path = None
            diarization_report = DiarizationReport(
                status="fallback",
                backend=backend_name,
                detected_speakers=0,
                aligned_segments=0,
                ambiguous_segments=0,
                temporary_audio=temporary_audio_status,
                warning=_safe_diarization_warning(error),
            )

    prepared = PreparedEpisode(
        video_id=metadata.video_id,
        source_title=metadata.source_title,
        transcript_path=transcript_path,
        state_path=state_path,
        speakers_path=speakers_path,
        speaker_map_path=final_speaker_map_path,
        diarization_report=diarization_report,
    )

    _write_subtitle_jsonl(job_dir / "subtitles.en.jsonl", segments)

    client: OpenAICompatibleTranslationClient | None = None
    if translator is None:
        client = create_translation_client(provider)
        effective_translator = client.translate
        effective_identity = client.translation_identity
    else:
        effective_translator = translator
        effective_identity = translation_identity or "custom:test"

    source_hash = _source_hash(segments)
    translation_metadata_path = job_dir / "translation-metadata.json"
    if (
        _transcript_is_complete(transcript_path, segments)
        and _translation_metadata_matches(
            translation_metadata_path,
            source_hash,
            effective_identity,
        )
    ):
        if speakers_path is not None:
            _verify_speaker_sequence(speakers_path, segments)
        _write_job_state(state_path, "transcript_ready", url, metadata)
        return prepared

    translations = translate_with_resume(
        segments,
        job_dir / "translation-chunks",
        effective_translator,
        translation_identity=effective_identity,
    )
    _write_job_state(state_path, "translation_ready", url, metadata)
    transcript = _render_chinese_transcript(metadata, url, segments, translations)
    _write_text_atomic(transcript_path, transcript)
    if speakers_path is not None:
        _verify_speaker_sequence(speakers_path, segments)
        translated_paragraphs = len(
            re.findall(r"(?m)^<!-- seg-\d+ -->$", transcript)
        )
        if translated_paragraphs != len(_speaker_lines(speakers_path)):
            raise DiarizationBackendError(
                "speaker 序列与中文逐字稿段落数不一致"
            )
    _write_json_atomic(
        translation_metadata_path,
        _translation_metadata_payload(source_hash, effective_identity),
    )
    _write_job_state(state_path, "transcript_ready", url, metadata)
    return prepared


def _is_exact_youtube_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
    return parsed.scheme in {"http", "https"} and host in allowed_hosts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把一个 YouTube 英文播客准备成可朗读的中文逐字稿"
    )
    parser.add_argument("url", help="准确的 YouTube 视频 URL")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / "Desktop" / "音频",
        help="任务和最终音频的根目录",
    )
    parser.add_argument(
        "--provider",
        choices=tuple(_PROVIDER_CONFIGS),
        default="deepseek",
        help="翻译模型供应商（默认: deepseek）",
    )
    parser.add_argument(
        "--diarize",
        choices=("auto", "off", "required"),
        default="auto",
        help="说话人识别模式（默认: auto）",
    )
    parser.add_argument(
        "--speaker-count",
        type=int,
        help="已知的精确说话人数（1-6）",
    )
    parser.add_argument("--min-speakers", type=int, help="最少说话人数（1-6）")
    parser.add_argument("--max-speakers", type=int, help="最多说话人数（1-6）")
    args = parser.parse_args(argv)

    try:
        speaker_bounds = resolve_speaker_bounds(
            args.speaker_count,
            args.min_speakers,
            args.max_speakers,
        )
    except ValueError as error:
        parser.error(str(error))

    if not _is_exact_youtube_url(args.url):
        print("错误:请输入准确的 YouTube URL,不能输入搜索词", file=sys.stderr)
        return 2
    if shutil.which("yt-dlp") is None:
        print("错误:找不到 yt-dlp,请先安装", file=sys.stderr)
        return 2
    provider_config = get_provider_config(args.provider)
    if not os.getenv(provider_config.api_key_env):
        print(f"错误:缺少环境变量 {provider_config.api_key_env}", file=sys.stderr)
        print(
            "首次使用请运行: "
            f"bash bin/setup-podcast-model.sh {provider_config.name}",
            file=sys.stderr,
        )
        return 2

    try:
        prepared = prepare_episode(
            args.url,
            args.output_root,
            provider=args.provider,
            diarize=args.diarize,
            speaker_bounds=speaker_bounds,
        )
    except (PodcastPipelineError, OSError) as error:
        print(f"错误:{error}", file=sys.stderr)
        return 1

    print(f"英文标题:{prepared.source_title}")
    print(f"中文字幕:{prepared.transcript_path}")
    report = prepared.diarization_report
    if prepared.speakers_path is not None:
        print(f"说话人序列:{prepared.speakers_path}")
    if report is not None:
        if report.status in {"success", "cache_hit"}:
            print(
                "说话人识别:成功（"
                f"{report.detected_speakers} 人，"
                f"{report.aligned_segments} 段，"
                f"{report.ambiguous_segments} 段需留意）"
            )
            if report.status == "cache_hit":
                print("临时原音频:已删除（本次命中缓存，未重新下载）")
            else:
                print("临时原音频:已删除")
        elif report.status == "off":
            print("说话人识别:已关闭")
            print("临时原音频:未下载")
        else:
            print(
                "说话人识别:已回退到内容判断（"
                f"{report.warning or '未知错误'}）"
            )
            if report.temporary_audio == "deleted":
                print("临时原音频:已删除")
            else:
                print("临时原音频:未下载")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
