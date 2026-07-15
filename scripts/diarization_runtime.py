#!/usr/bin/env python3
"""Discover, lazily install, and invoke the isolated sherpa runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from diarization_core import DiarizationTurn, SpeakerBounds


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = ROOT / "scripts/diarization_models.json"
DEFAULT_REQUIREMENTS_PATH = ROOT / "requirements-diarization.txt"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SPEAKER_ID = re.compile(r"^SPEAKER_\d{2,}$")
DEFAULT_CHUNK_DURATION_MS = 15 * 60 * 1000
DEFAULT_CHUNK_OVERLAP_MS = 5 * 60 * 1000


class DiarizationSetupError(RuntimeError):
    """Raised when the isolated runtime cannot be prepared safely."""


class DiarizationRuntimeError(RuntimeError):
    """Raised when the isolated worker returns invalid output."""


@dataclass(frozen=True)
class DiarizationRuntime:
    python: Path
    model_dir: Path
    segmentation_model: Path
    embedding_model: Path
    fingerprint: dict[str, object]


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _chunk_ranges(
    duration_ms: int,
    *,
    chunk_duration_ms: int = DEFAULT_CHUNK_DURATION_MS,
    overlap_ms: int = DEFAULT_CHUNK_OVERLAP_MS,
) -> list[tuple[int, int]]:
    if duration_ms <= 0:
        raise DiarizationRuntimeError("声纹音频时长无效")
    if chunk_duration_ms <= 0 or not 0 <= overlap_ms < chunk_duration_ms:
        raise DiarizationRuntimeError("声纹分块参数无效")
    ranges: list[tuple[int, int]] = []
    start_ms = 0
    while start_ms < duration_ms:
        end_ms = min(duration_ms, start_ms + chunk_duration_ms)
        ranges.append((start_ms, end_ms))
        if end_ms == duration_ms:
            break
        start_ms = end_ms - overlap_ms
    return ranges


def _turn_overlap_ms(
    left: DiarizationTurn,
    right: DiarizationTurn,
    *,
    window_start_ms: int,
    window_end_ms: int,
) -> int:
    return max(
        0,
        min(left.end_ms, right.end_ms, window_end_ms)
        - max(left.start_ms, right.start_ms, window_start_ms),
    )


def _best_overlap_mapping(
    existing: list[DiarizationTurn],
    incoming: list[DiarizationTurn],
    *,
    overlap_start_ms: int,
    overlap_end_ms: int,
) -> dict[str, str]:
    global_ids = sorted({turn.speaker_id for turn in existing})
    local_ids = sorted({turn.speaker_id for turn in incoming})
    scores: dict[tuple[str, str], int] = {}
    for local_id in local_ids:
        for global_id in global_ids:
            scores[(local_id, global_id)] = sum(
                _turn_overlap_ms(
                    local_turn,
                    global_turn,
                    window_start_ms=overlap_start_ms,
                    window_end_ms=overlap_end_ms,
                )
                for local_turn in incoming
                if local_turn.speaker_id == local_id
                for global_turn in existing
                if global_turn.speaker_id == global_id
            )

    best_score = -1
    best_signature: tuple[str, ...] | None = None
    best_mapping: dict[str, str] = {}

    def visit(
        index: int,
        used_global_ids: set[str],
        mapping: dict[str, str],
        score: int,
    ) -> None:
        nonlocal best_mapping, best_score, best_signature
        if index == len(local_ids):
            signature = tuple(mapping.get(local_id, "~") for local_id in local_ids)
            if score > best_score or (
                score == best_score
                and (best_signature is None or signature < best_signature)
            ):
                best_score = score
                best_signature = signature
                best_mapping = dict(mapping)
            return
        local_id = local_ids[index]
        visit(index + 1, used_global_ids, mapping, score)
        for global_id in global_ids:
            overlap = scores[(local_id, global_id)]
            if overlap <= 0 or global_id in used_global_ids:
                continue
            mapping[local_id] = global_id
            used_global_ids.add(global_id)
            visit(index + 1, used_global_ids, mapping, score + overlap)
            used_global_ids.remove(global_id)
            mapping.pop(local_id)

    visit(0, set(), {}, 0)
    return best_mapping


def _next_speaker_id(used_ids: set[str]) -> str:
    index = 0
    while f"SPEAKER_{index:02d}" in used_ids:
        index += 1
    return f"SPEAKER_{index:02d}"


def _stitch_diarization_chunks(
    existing: list[DiarizationTurn],
    incoming: list[DiarizationTurn],
    *,
    overlap_start_ms: int,
    overlap_end_ms: int,
) -> list[DiarizationTurn]:
    if not existing:
        return sorted(
            incoming, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id)
        )
    if overlap_end_ms <= overlap_start_ms:
        raise DiarizationRuntimeError("声纹分块重叠区无效")
    mapping = _best_overlap_mapping(
        existing,
        incoming,
        overlap_start_ms=overlap_start_ms,
        overlap_end_ms=overlap_end_ms,
    )
    used_ids = {turn.speaker_id for turn in existing}
    for local_id in sorted({turn.speaker_id for turn in incoming}):
        if local_id not in mapping:
            new_id = _next_speaker_id(used_ids)
            mapping[local_id] = new_id
            used_ids.add(new_id)

    handoff_ms = (overlap_start_ms + overlap_end_ms) // 2
    stitched: list[DiarizationTurn] = []
    for turn in existing:
        clipped_end = min(turn.end_ms, handoff_ms)
        if turn.start_ms < clipped_end:
            stitched.append(
                DiarizationTurn(turn.start_ms, clipped_end, turn.speaker_id)
            )
    for turn in incoming:
        clipped_start = max(turn.start_ms, handoff_ms)
        if clipped_start < turn.end_ms:
            stitched.append(
                DiarizationTurn(
                    clipped_start,
                    turn.end_ms,
                    mapping[turn.speaker_id],
                )
            )
    return sorted(
        stitched, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id)
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_model_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, object]:
    """Load and validate the non-commercial-model-free asset manifest."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiarizationSetupError(f"无法读取声纹模型清单: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise DiarizationSetupError("声纹模型清单版本无效")
    if manifest.get("backend") != "sherpa-onnx":
        raise DiarizationSetupError("声纹后端必须是 sherpa-onnx")
    if not isinstance(manifest.get("sherpa_onnx_version"), str):
        raise DiarizationSetupError("声纹运行时版本无效")
    if not isinstance(manifest.get("model_bundle"), str):
        raise DiarizationSetupError("声纹模型包名无效")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise DiarizationSetupError("声纹模型清单不含资产")
    names: set[str] = set()
    targets: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise DiarizationSetupError("声纹模型资产格式无效")
        name = asset.get("name")
        target = asset.get("target")
        url = asset.get("url")
        if not isinstance(name, str) or not name or name in names:
            raise DiarizationSetupError("声纹模型资产名无效")
        if (
            not isinstance(target, str)
            or not target
            or Path(target).name != target
            or target in targets
        ):
            raise DiarizationSetupError("声纹模型目标文件名无效")
        if not isinstance(url, str) or urlsplit(url).scheme != "https":
            raise DiarizationSetupError("声纹模型下载地址必须是 HTTPS")
        if not _SHA256.fullmatch(str(asset.get("target_sha256", ""))):
            raise DiarizationSetupError("声纹模型 SHA-256 无效")
        if asset.get("license") not in {"MIT", "Apache-2.0"}:
            raise DiarizationSetupError("声纹模型许可证不在商业可用白名单")
        names.add(name)
        targets.add(target)
    if names != {"segmentation", "embedding"}:
        raise DiarizationSetupError("声纹模型清单必须同时包含分割和嵌入模型")
    if "reverb" in json.dumps(manifest, ensure_ascii=False).casefold():
        raise DiarizationSetupError("禁止使用限制商业用途的 Reverb 模型")
    return manifest


def default_model_root(manifest: dict[str, object]) -> Path:
    base = Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / "Library/Caches")))
    return (
        base
        / "youtube-podcast-cn"
        / "diarization"
        / str(manifest["model_bundle"])
    )


def backend_fingerprint(manifest: dict[str, object]) -> dict[str, object]:
    assets = manifest["assets"]
    if not isinstance(assets, list):
        raise DiarizationSetupError("声纹模型清单资产无效")
    return {
        "backend": "sherpa-onnx",
        "runtime": manifest["sherpa_onnx_version"],
        "model_bundle": manifest["model_bundle"],
        "models": {
            str(asset["name"]): asset["target_sha256"]
            for asset in assets
            if isinstance(asset, dict)
        },
        "cluster_threshold": 0.5,
        "min_duration_on": 0.3,
        "min_duration_off": 0.5,
        "chunk_duration_ms": DEFAULT_CHUNK_DURATION_MS,
        "chunk_overlap_ms": DEFAULT_CHUNK_OVERLAP_MS,
    }


def _asset_by_name(manifest: dict[str, object], name: str) -> dict[str, object]:
    assets = manifest["assets"]
    if not isinstance(assets, list):
        raise DiarizationSetupError("声纹模型清单资产无效")
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") == name:
            return asset
    raise DiarizationSetupError(f"声纹模型清单缺少 {name}")


def _installed_runtime(
    runtime_root: Path,
    model_root: Path,
    manifest: dict[str, object],
    manifest_path: Path,
    requirements_path: Path,
) -> DiarizationRuntime | None:
    python = runtime_root / "bin/python"
    state_path = runtime_root / "runtime-state.json"
    segmentation = model_root / str(_asset_by_name(manifest, "segmentation")["target"])
    embedding = model_root / str(_asset_by_name(manifest, "embedding")["target"])
    if not python.is_file() or not os.access(python, os.X_OK) or not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        expected_state = {
            "schema_version": 1,
            "requirements_sha256": sha256_file(requirements_path),
            "manifest_sha256": sha256_file(manifest_path),
            "fingerprint": backend_fingerprint(manifest),
        }
        if state != expected_state:
            return None
        for name, path in (("segmentation", segmentation), ("embedding", embedding)):
            asset = _asset_by_name(manifest, name)
            if not path.is_file() or sha256_file(path) != asset["target_sha256"]:
                return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return DiarizationRuntime(
        python=python,
        model_dir=model_root,
        segmentation_model=segmentation,
        embedding_model=embedding,
        fingerprint=backend_fingerprint(manifest),
    )


def ensure_diarization_runtime(
    *,
    runtime_root: Path | None = None,
    model_root: Path | None = None,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    requirements_path: Path = DEFAULT_REQUIREMENTS_PATH,
    opener: Callable[..., object] = urllib.request.urlopen,
    runner: Runner = subprocess.run,
) -> DiarizationRuntime:
    """Reuse a verified runtime or install it atomically on first use."""
    manifest = load_model_manifest(manifest_path)
    resolved_runtime_root = runtime_root or ROOT / ".diarization-venv"
    resolved_model_root = model_root or default_model_root(manifest)
    installed = _installed_runtime(
        resolved_runtime_root,
        resolved_model_root,
        manifest,
        manifest_path,
        requirements_path,
    )
    if installed is not None:
        return installed

    from setup_diarization import setup_diarization

    setup_diarization(
        runtime_root=resolved_runtime_root,
        model_root=resolved_model_root,
        manifest_path=manifest_path,
        requirements_path=requirements_path,
        opener=opener,
        runner=runner,
    )
    installed = _installed_runtime(
        resolved_runtime_root,
        resolved_model_root,
        manifest,
        manifest_path,
        requirements_path,
    )
    if installed is None:
        raise DiarizationSetupError("声纹运行时安装完成后校验失败")
    return installed


def _audio_duration_ms(audio_path: Path) -> int:
    try:
        with wave.open(str(audio_path), "rb") as source:
            if (
                source.getframerate() != 16_000
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
            ):
                raise DiarizationRuntimeError("声纹输入必须是 16 kHz 单声道 PCM WAV")
            return round(source.getnframes() * 1000 / source.getframerate())
    except (OSError, wave.Error) as error:
        raise DiarizationRuntimeError("无法读取声纹 WAV") from error


def _read_worker_turns(output_path: Path) -> list[DiarizationTurn]:
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiarizationRuntimeError("声纹 worker 没有返回有效 JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"turns"}:
        raise DiarizationRuntimeError("声纹 worker 输出结构无效")
    raw_turns = payload["turns"]
    if not isinstance(raw_turns, list):
        raise DiarizationRuntimeError("声纹 worker 时间段无效")
    turns: list[DiarizationTurn] = []
    for item in raw_turns:
        if not isinstance(item, dict) or set(item) != {
            "start_ms",
            "end_ms",
            "speaker_id",
        }:
            raise DiarizationRuntimeError("声纹 worker 时间段字段无效")
        if (
            type(item["start_ms"]) is not int
            or type(item["end_ms"]) is not int
            or not isinstance(item["speaker_id"], str)
            or not _SPEAKER_ID.fullmatch(item["speaker_id"])
        ):
            raise DiarizationRuntimeError("声纹 worker 时间段值无效")
        turns.append(DiarizationTurn(**item))
    return sorted(
        turns, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id)
    )


def _run_worker_chunk(
    audio_path: Path,
    bounds: SpeakerBounds,
    runtime: DiarizationRuntime,
    *,
    start_ms: int,
    end_ms: int,
    chunk_index: int,
    chunk_count: int,
    runner: Runner,
    progress_callback: Callable[[int], None] | None,
) -> list[DiarizationTurn]:
    descriptor, output_name = tempfile.mkstemp(
        prefix=".diarization-output-", suffix=".json", dir=audio_path.parent
    )
    os.close(descriptor)
    output_path = Path(output_name)
    output_path.unlink()
    command = [
        str(runtime.python),
        str(ROOT / "scripts/sherpa_diarization_worker.py"),
        "--audio",
        str(audio_path),
        "--segmentation-model",
        str(runtime.segmentation_model),
        "--embedding-model",
        str(runtime.embedding_model),
        "--output",
        str(output_path),
        "--start-ms",
        str(start_ms),
        "--end-ms",
        str(end_ms),
    ]
    if bounds.exact is not None:
        command.extend(["--num-speakers", str(bounds.exact)])
    else:
        command.extend(
            ["--min-speakers", str(bounds.minimum), "--max-speakers", str(bounds.maximum)]
        )
    try:
        completed = runner(command, capture_output=True, text=True, check=False)
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            detail = stderr[-500:].strip() or "未知错误"
            if completed.returncode < 0:
                detail = f"进程被信号 {-completed.returncode} 终止; {detail}"
            raise DiarizationRuntimeError(f"声纹分离失败: {detail}")
        progress_matches = re.findall(r"(?m)^PROGRESS (\d{1,3})$", stderr)
        if progress_callback is not None and progress_matches:
            local_progress = min(100, max(int(value) for value in progress_matches))
            overall_progress = round(
                (chunk_index + local_progress / 100) * 100 / chunk_count
            )
            progress_callback(min(100, overall_progress))
        return _read_worker_turns(output_path)
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass


def run_sherpa_backend(
    audio_path: Path,
    bounds: SpeakerBounds,
    *,
    runtime: DiarizationRuntime | None = None,
    runner: Runner = subprocess.run,
    progress_callback: Callable[[int], None] | None = None,
    chunk_duration_ms: int = DEFAULT_CHUNK_DURATION_MS,
    chunk_overlap_ms: int = DEFAULT_CHUNK_OVERLAP_MS,
) -> list[DiarizationTurn]:
    """Run isolated bounded workers and stitch anonymous speakers across overlaps."""
    selected_runtime = runtime or ensure_diarization_runtime()
    duration_ms = _audio_duration_ms(audio_path)
    ranges = _chunk_ranges(
        duration_ms,
        chunk_duration_ms=chunk_duration_ms,
        overlap_ms=chunk_overlap_ms,
    )
    stitched: list[DiarizationTurn] = []
    previous_end_ms = 0
    for index, (start_ms, end_ms) in enumerate(ranges):
        chunk_turns = _run_worker_chunk(
            audio_path,
            bounds,
            selected_runtime,
            start_ms=start_ms,
            end_ms=end_ms,
            chunk_index=index,
            chunk_count=len(ranges),
            runner=runner,
            progress_callback=progress_callback,
        )
        if index == 0:
            stitched = chunk_turns
        else:
            stitched = _stitch_diarization_chunks(
                stitched,
                chunk_turns,
                overlap_start_ms=start_ms,
                overlap_end_ms=previous_end_ms,
            )
        previous_end_ms = end_ms
    return stitched
