#!/usr/bin/env python3
"""Discover, lazily install, and invoke the isolated sherpa runtime."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.request
import wave
from dataclasses import dataclass
from functools import lru_cache
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
PopenFactory = Callable[..., object]


@dataclass(frozen=True)
class WorkerChunk:
    turns: list[DiarizationTurn]
    embeddings: dict[str, list[float]]


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

    return _maximum_weight_mapping(
        local_ids,
        global_ids,
        scores,
        minimum_score=0.0,
    )


def _maximum_weight_mapping(
    local_ids: list[str],
    global_ids: list[str],
    scores: dict[tuple[str, str], float | int],
    *,
    minimum_score: float,
) -> dict[str, str]:
    """Return an exact maximum-weight partial matching for at most six IDs."""
    ordered_local = sorted(local_ids)
    ordered_global = sorted(global_ids)
    if len(ordered_local) > 6 or len(ordered_global) > 6:
        raise DiarizationRuntimeError("声纹匹配人数超出安全上限")

    @lru_cache(maxsize=None)
    def solve(
        local_index: int, used_mask: int
    ) -> tuple[float, tuple[str, ...]]:
        if local_index == len(ordered_local):
            return 0.0, ()
        local_id = ordered_local[local_index]
        remaining_score, remaining_signature = solve(local_index + 1, used_mask)
        best = (remaining_score, ("~",) + remaining_signature)
        for global_index, global_id in enumerate(ordered_global):
            if used_mask & (1 << global_index):
                continue
            score = float(scores.get((local_id, global_id), float("-inf")))
            if score <= minimum_score:
                continue
            tail_score, tail_signature = solve(
                local_index + 1,
                used_mask | (1 << global_index),
            )
            candidate = (score + tail_score, (global_id,) + tail_signature)
            if candidate[0] > best[0] or (
                candidate[0] == best[0] and candidate[1] < best[1]
            ):
                best = candidate
        return best

    _, signature = solve(0, 0)
    return {
        local_id: global_id
        for local_id, global_id in zip(ordered_local, signature, strict=True)
        if global_id != "~"
    }


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )


def _speaker_cap(bounds: SpeakerBounds) -> int:
    """Configured speaker ceiling. exact wins; the default maximum stays 6."""
    return bounds.exact if bounds.exact is not None else bounds.maximum


def _validate_chunk_turns(
    turns: list[DiarizationTurn], bounds: SpeakerBounds
) -> None:
    if len({turn.speaker_id for turn in turns}) > _speaker_cap(bounds):
        raise DiarizationRuntimeError("声纹分块检测到的说话人数超出配置范围")


def _fit_turns_to_speaker_budget(
    turns: list[DiarizationTurn],
    embeddings: dict[str, list[float]],
    bounds: SpeakerBounds,
) -> tuple[list[DiarizationTurn], dict[str, list[float]]]:
    """Merge extra cluster IDs down to the configured cap before validation.

    Sherpa can emit more than six IDs in one chunk even when the worker was
    passed ``--max-speakers``. Rejecting those IDs before folding aborts the
    episode (the chunk-count error). Fold the turns stitching will actually
    see, including the first chunk, and keep embedding keys on the survivors.
    The cap is the existing exact count or maximum (6). It is not raised.
    """
    cap = _speaker_cap(bounds)
    duration: dict[str, int] = {}
    for turn in turns:
        duration[turn.speaker_id] = duration.get(turn.speaker_id, 0) + (
            turn.end_ms - turn.start_ms
        )
    if cap < 1 or len(duration) <= cap:
        return turns, embeddings

    vectors = {speaker_id: list(vector) for speaker_id, vector in embeddings.items()}
    alias = {speaker_id: speaker_id for speaker_id in duration}

    def nearest_or_longest(donor: str, others: list[str]) -> str:
        donor_vector = vectors.get(donor) or []
        best_id: str | None = None
        best_key: tuple[float, int, str] | None = None
        if donor_vector:
            for candidate in others:
                score = _cosine_similarity(donor_vector, vectors.get(candidate) or [])
                if score <= -1.0:
                    continue
                key = (score, duration[candidate], candidate)
                if best_key is None or key > best_key:
                    best_key = key
                    best_id = candidate
        if best_id is not None:
            return best_id
        return max(others, key=lambda speaker_id: (duration[speaker_id], speaker_id))

    while len(duration) > cap:
        donor = min(duration, key=lambda speaker_id: (duration[speaker_id], speaker_id))
        others = [speaker_id for speaker_id in duration if speaker_id != donor]
        target = nearest_or_longest(donor, others)
        duration[target] += duration.pop(donor)
        donor_vector = vectors.pop(donor, None)
        target_vector = vectors.get(target)
        if donor_vector is not None:
            if target_vector is None:
                vectors[target] = donor_vector
            elif len(target_vector) == len(donor_vector):
                vectors[target] = [
                    (left + right) / 2
                    for left, right in zip(target_vector, donor_vector, strict=True)
                ]
        for original, current in list(alias.items()):
            if current == donor:
                alias[original] = target

    fitted = [
        DiarizationTurn(
            turn.start_ms,
            turn.end_ms,
            alias.get(turn.speaker_id, turn.speaker_id),
        )
        for turn in turns
    ]
    surviving = set(alias.values())
    fitted_embeddings = {
        speaker_id: vectors[speaker_id]
        for speaker_id in surviving
        if speaker_id in vectors
    }
    return fitted, fitted_embeddings


def _speaker_mapping(
    existing: list[DiarizationTurn],
    incoming: list[DiarizationTurn],
    *,
    overlap_start_ms: int,
    overlap_end_ms: int,
    bounds: SpeakerBounds,
    existing_embeddings: dict[str, list[float]] | None = None,
    incoming_embeddings: dict[str, list[float]] | None = None,
) -> dict[str, str]:
    local_ids = sorted({turn.speaker_id for turn in incoming})
    global_ids = sorted({turn.speaker_id for turn in existing})
    if bounds.exact == 1:
        return {local_id: "SPEAKER_00" for local_id in local_ids}
    mapping = _best_overlap_mapping(
        existing,
        incoming,
        overlap_start_ms=overlap_start_ms,
        overlap_end_ms=overlap_end_ms,
    )
    used_global_ids = set(mapping.values())
    existing_vectors = existing_embeddings or {}
    incoming_vectors = incoming_embeddings or {}
    unmatched_local = [item for item in local_ids if item not in mapping]
    available_global = [item for item in global_ids if item not in used_global_ids]
    prototype_scores = {
        (local_id, global_id): _cosine_similarity(
            incoming_vectors.get(local_id, []),
            existing_vectors.get(global_id, []),
        )
        for local_id in unmatched_local
        for global_id in available_global
    }
    prototype_mapping = _maximum_weight_mapping(
        unmatched_local,
        available_global,
        prototype_scores,
        minimum_score=0.35,
    )
    mapping.update(prototype_mapping)
    used_global_ids.update(prototype_mapping.values())

    unmatched_local = [item for item in local_ids if item not in mapping]
    available_global = [item for item in global_ids if item not in used_global_ids]
    if (
        bounds.exact is not None
        and unmatched_local
        and len(unmatched_local) == len(available_global)
        and all(item in incoming_vectors for item in unmatched_local)
        and all(item in existing_vectors for item in available_global)
    ):
        # An exact user-confirmed count gives us a closed one-to-one set. If
        # overlap and the conservative 0.35 threshold both fail, keep speaker
        # identities stable by choosing the best complete prototype matching.
        fallback_mapping = _maximum_weight_mapping(
            unmatched_local,
            available_global,
            {
                pair: score + 2.0
                for pair, score in prototype_scores.items()
            },
            minimum_score=0.0,
        )
        mapping.update(fallback_mapping)
        used_global_ids.update(fallback_mapping.values())

    # Soft closed-world for long audio: when overlap/prototype evidence is
    # weak, prefer remapping onto existing globals over inventing new IDs.
    # Inventing across many chunks exhausts the speaker budget and then fails
    # with "无法安全映射说话人" on interviews that are only 2–3 talkers.
    unmatched_local = [item for item in local_ids if item not in mapping]
    for local_id in unmatched_local:
        if local_id not in incoming_vectors:
            continue
        best_global: str | None = None
        best_score = float("-inf")
        for global_id in global_ids:
            if global_id not in existing_vectors:
                continue
            score = _cosine_similarity(
                incoming_vectors[local_id], existing_vectors[global_id]
            )
            unused = global_id not in used_global_ids
            if score > best_score or (
                score == best_score
                and unused
                and (best_global is None or best_global in used_global_ids)
            ):
                best_score = score
                best_global = global_id
        if best_global is not None and best_score > -1.0:
            mapping[local_id] = best_global
            used_global_ids.add(best_global)

    # When a chunk local has no voice print, inventing a new global ID is how
    # long interviews grow ghost speakers (SPEAKER_02/04/…). With no exact
    # count, attach those leftovers to the dominant existing talker instead.
    unmatched_local = [item for item in local_ids if item not in mapping]
    if (
        unmatched_local
        and bounds.exact is None
        and len(global_ids) >= 2
    ):
        duration_by: dict[str, int] = {}
        for turn in existing:
            duration_by[turn.speaker_id] = duration_by.get(turn.speaker_id, 0) + (
                turn.end_ms - turn.start_ms
            )
        dominant = max(global_ids, key=lambda sid: (duration_by.get(sid, 0), sid))
        for local_id in unmatched_local:
            if local_id in incoming_vectors:
                continue
            mapping[local_id] = dominant
            used_global_ids.add(dominant)

    maximum = _speaker_cap(bounds)
    all_used_ids = set(global_ids)
    for local_id in local_ids:
        if local_id in mapping:
            continue
        if len(all_used_ids) < maximum:
            new_id = _next_speaker_id(all_used_ids)
            mapping[local_id] = new_id
            all_used_ids.add(new_id)
            used_global_ids.add(new_id)
            continue
        raise DiarizationRuntimeError(
            "声纹分块缺少可靠的重叠或声纹证据，无法安全映射说话人"
        )
    return mapping



def _collapse_minor_speakers(
    turns: list[DiarizationTurn],
    prototypes: dict[str, list[float]],
    *,
    min_ms: int = 20_000,
) -> list[DiarizationTurn]:
    """Merge speakers shorter than min_ms into the nearest lasting voice.

    Sherpa clustering on interview chunks often emits tiny ghost IDs alongside
    the real talkers. Collapsing them after stitch keeps voice slots usable
    without raising the global cluster threshold (which collapses real guests).
    """
    if not turns or min_ms <= 0:
        return turns
    duration_by: dict[str, int] = {}
    for turn in turns:
        duration_by[turn.speaker_id] = duration_by.get(turn.speaker_id, 0) + (
            turn.end_ms - turn.start_ms
        )
    majors = {sid for sid, total in duration_by.items() if total >= min_ms}
    minors = [sid for sid in duration_by if sid not in majors]
    if not minors or not majors:
        return turns
    remap: dict[str, str] = {}
    dominant = max(majors, key=lambda sid: (duration_by[sid], sid))
    for minor in minors:
        best = dominant
        best_score = float("-inf")
        if minor in prototypes:
            for major in majors:
                if major not in prototypes:
                    continue
                score = _cosine_similarity(prototypes[minor], prototypes[major])
                if score > best_score:
                    best_score = score
                    best = major
        remap[minor] = best
    return [
        DiarizationTurn(
            turn.start_ms,
            turn.end_ms,
            remap.get(turn.speaker_id, turn.speaker_id),
        )
        for turn in turns
    ]


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
    bounds: SpeakerBounds,
    existing_embeddings: dict[str, list[float]] | None = None,
    incoming_embeddings: dict[str, list[float]] | None = None,
    mapping: dict[str, str] | None = None,
) -> list[DiarizationTurn]:
    if not existing:
        return sorted(
            (
                DiarizationTurn(
                    turn.start_ms,
                    turn.end_ms,
                    mapping.get(turn.speaker_id, turn.speaker_id)
                    if mapping is not None
                    else turn.speaker_id,
                )
                for turn in incoming
            ),
            key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id),
        )
    if overlap_end_ms <= overlap_start_ms:
        raise DiarizationRuntimeError("声纹分块重叠区无效")
    selected_mapping = mapping or _speaker_mapping(
        existing,
        incoming,
        overlap_start_ms=overlap_start_ms,
        overlap_end_ms=overlap_end_ms,
        bounds=bounds,
        existing_embeddings=existing_embeddings,
        incoming_embeddings=incoming_embeddings,
    )

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
                    selected_mapping[turn.speaker_id],
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
        "stitching_version": 2,
        "speaker_prototype_max_seconds": 30,
        "speaker_prototype_similarity": 0.35,
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
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 1
            or state.get("requirements_sha256") != sha256_file(requirements_path)
            or state.get("manifest_sha256") != sha256_file(manifest_path)
            or not isinstance(state.get("fingerprint"), dict)
        ):
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


def _read_worker_output(output_path: Path) -> WorkerChunk:
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiarizationRuntimeError("声纹 worker 没有返回有效 JSON") from error
    if (
        not isinstance(payload, dict)
        or not set(payload).issubset({"turns", "embeddings"})
        or "turns" not in payload
    ):
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
    raw_embeddings = payload.get("embeddings", {})
    if not isinstance(raw_embeddings, dict):
        raise DiarizationRuntimeError("声纹 worker 声纹向量无效")
    embeddings: dict[str, list[float]] = {}
    vector_size: int | None = None
    for speaker_id, vector in raw_embeddings.items():
        if (
            not isinstance(speaker_id, str)
            or not _SPEAKER_ID.fullmatch(speaker_id)
            or not isinstance(vector, list)
            or not vector
            or len(vector) > 4096
            or any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in vector
            )
        ):
            raise DiarizationRuntimeError("声纹 worker 声纹向量无效")
        if vector_size is None:
            vector_size = len(vector)
        elif len(vector) != vector_size:
            raise DiarizationRuntimeError("声纹 worker 声纹向量维度不一致")
        embeddings[speaker_id] = [float(value) for value in vector]
    return WorkerChunk(
        sorted(
            turns,
            key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id),
        ),
        embeddings,
    )


def _overall_progress(
    local_progress: int, chunk_index: int, chunk_count: int
) -> int:
    return min(
        100,
        round((chunk_index + min(100, local_progress) / 100) * 100 / chunk_count),
    )


def _stream_worker(
    command: list[str],
    *,
    popen_factory: PopenFactory,
    on_progress: Callable[[int], None],
) -> tuple[int, str]:
    process = popen_factory(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_stream = getattr(process, "stderr", None)
    if stderr_stream is None:
        raise DiarizationRuntimeError("声纹 worker 无法读取进度")
    tail = ""
    try:
        for raw_line in stderr_stream:
            line = str(raw_line).rstrip("\r\n")
            match = re.fullmatch(r"PROGRESS (\d{1,3})", line)
            if match:
                on_progress(min(100, int(match.group(1))))
            else:
                tail = f"{tail}{line}\n"[-500:]
    except BaseException:
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        except Exception:
            pass
        raise
    return int(process.wait()), tail.strip()


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
    popen_factory: PopenFactory | None,
    progress_callback: Callable[[int], None] | None,
) -> WorkerChunk:
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
            [
                "--min-speakers",
                "1",
                "--max-speakers",
                str(bounds.exact if bounds.exact is not None else bounds.maximum),
            ]
        )
    try:
        last_progress = -1

        def report(local_progress: int) -> None:
            nonlocal last_progress
            overall = _overall_progress(local_progress, chunk_index, chunk_count)
            if progress_callback is not None and overall > last_progress:
                last_progress = overall
                progress_callback(overall)

        selected_popen = popen_factory
        if selected_popen is None and runner is subprocess.run:
            selected_popen = subprocess.Popen
        if selected_popen is not None:
            returncode, stderr = _stream_worker(
                command,
                popen_factory=selected_popen,
                on_progress=report,
            )
        else:
            completed = runner(command, capture_output=True, text=True, check=False)
            returncode = completed.returncode
            stderr = completed.stderr or ""
        if returncode != 0:
            detail = stderr[-500:].strip() or "未知错误"
            if returncode < 0:
                detail = f"进程被信号 {-returncode} 终止; {detail}"
            raise DiarizationRuntimeError(f"声纹分离失败: {detail}")
        if selected_popen is None:
            progress_matches = re.findall(r"(?m)^PROGRESS (\d{1,3})$", stderr)
            if progress_matches:
                report(max(int(value) for value in progress_matches))
        result = _read_worker_output(output_path)
        # Fit before the hard count check. The first chunk is stitched as-is,
        # so the folded turns and embeddings are what later chunks map against.
        turns, embeddings = _fit_turns_to_speaker_budget(
            result.turns, result.embeddings, bounds
        )
        fitted = WorkerChunk(turns, embeddings)
        _validate_chunk_turns(fitted.turns, bounds)
        return fitted
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
    popen_factory: PopenFactory | None = None,
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
    prototypes: dict[str, list[float]] = {}
    previous_end_ms = 0
    last_reported_progress = -1

    def emit_progress(value: int) -> None:
        nonlocal last_reported_progress
        if progress_callback is None:
            return
        if (
            last_reported_progress < 0
            or value == 100
            or value >= last_reported_progress + 5
        ):
            last_reported_progress = value
            progress_callback(value)

    for index, (start_ms, end_ms) in enumerate(ranges):
        chunk = _run_worker_chunk(
            audio_path,
            bounds,
            selected_runtime,
            start_ms=start_ms,
            end_ms=end_ms,
            chunk_index=index,
            chunk_count=len(ranges),
            runner=runner,
            popen_factory=popen_factory,
            progress_callback=emit_progress,
        )
        chunk_turns = chunk.turns
        if index == 0:
            stitched = chunk_turns
            prototypes = dict(chunk.embeddings)
        else:
            mapping = _speaker_mapping(
                stitched,
                chunk_turns,
                overlap_start_ms=start_ms,
                overlap_end_ms=previous_end_ms,
                bounds=bounds,
                existing_embeddings=prototypes,
                incoming_embeddings=chunk.embeddings,
            )
            stitched = _stitch_diarization_chunks(
                stitched,
                chunk_turns,
                overlap_start_ms=start_ms,
                overlap_end_ms=previous_end_ms,
                bounds=bounds,
                mapping=mapping,
            )
            for local_id, vector in chunk.embeddings.items():
                global_id = mapping[local_id]
                previous = prototypes.get(global_id)
                if previous is None or len(previous) != len(vector):
                    prototypes[global_id] = vector
                else:
                    prototypes[global_id] = [
                        (left + right) / 2
                        for left, right in zip(previous, vector, strict=True)
                    ]
        previous_end_ms = end_ms
    return _collapse_minor_speakers(stitched, prototypes)
