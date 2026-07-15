#!/usr/bin/env python3
"""Job-scoped source-audio download, conversion, and verified deletion."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, Literal, TypeVar


T = TypeVar("T")
Runner = Callable[..., subprocess.CompletedProcess[str]]
_REMOTE_URL = re.compile(r"https?://\S+", re.IGNORECASE)


class TemporaryAudioError(RuntimeError):
    """Raised when temporary source audio cannot be prepared safely."""


class TemporaryAudioCleanupError(TemporaryAudioError):
    """Raised when deletion of temporary source media cannot be verified."""


@dataclass(frozen=True)
class TemporaryAudioResult(Generic[T]):
    value: T
    source_audio_sha256: str
    cleanup_status: Literal["deleted"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _completed_or_error(
    command: list[str],
    *,
    runner: Runner,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = runner(
            command, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as error:
        raise TemporaryAudioError(f"{action}失败: 找不到 {command[0]}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or "")[-500:].strip() or "未知错误"
        detail = _REMOTE_URL.sub("[远程地址已隐藏]", detail)
        raise TemporaryAudioError(f"{action}失败: {detail}")
    return completed


def download_audio_only(
    url: str, temp_dir: Path, *, runner: Runner = subprocess.run
) -> Path:
    """Download exactly one audio-only file into the owned directory."""
    download_args = [
        "yt-dlp",
        "--no-playlist",
        "--no-write-info-json",
        "--no-write-thumbnail",
        "--format",
        "bestaudio/best",
        "--output",
        str(temp_dir / "source.%(ext)s"),
        url,
    ]
    _completed_or_error(download_args, runner=runner, action="下载原音频")
    candidates = [
        path
        for path in temp_dir.iterdir()
        if path.name.startswith("source.") and not path.name.endswith(".part")
    ]
    if len(candidates) != 1:
        raise TemporaryAudioError("原音频下载必须产生恰好一个文件")
    source = candidates[0]
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size <= 0
        or source.resolve().parent != temp_dir.resolve()
    ):
        raise TemporaryAudioError("原音频文件不符合临时目录安全规则")
    return source


def convert_to_mono_16k(
    source: Path,
    wav: Path,
    *,
    runner: Runner = subprocess.run,
) -> Path:
    """Convert source media to signed 16-bit mono PCM at 16 kHz."""
    ffmpeg_args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav),
    ]
    _completed_or_error(ffmpeg_args, runner=runner, action="转换声纹 WAV")
    if (
        wav.is_symlink()
        or not wav.is_file()
        or wav.stat().st_size <= 0
        or wav.resolve().parent != source.parent.resolve()
    ):
        raise TemporaryAudioError("声纹 WAV 未正确生成")
    return wav


def with_temporary_audio(
    url: str,
    job_dir: Path,
    analyze: Callable[[Path], T],
    *,
    runner: Runner = subprocess.run,
) -> TemporaryAudioResult[T]:
    """Expose only a temporary WAV callback and always verify media deletion."""
    job_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(prefix=".diarization-media-", dir=job_dir)
    )
    primary_error: BaseException | None = None
    source_hash = ""
    try:
        source = download_audio_only(url, temp_dir, runner=runner)
        source_hash = sha256_file(source)
        wav = convert_to_mono_16k(
            source, temp_dir / "analysis.wav", runner=runner
        )
        value = analyze(wav)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            shutil.rmtree(temp_dir)
            if temp_dir.exists():
                raise OSError("目录仍然存在")
        except OSError as cleanup_error:
            message = f"临时原音频删除失败:{temp_dir}"
            if primary_error is not None:
                message = f"处理失败且{message}"
            raise TemporaryAudioCleanupError(message) from cleanup_error
    return TemporaryAudioResult(value, source_hash, "deleted")
