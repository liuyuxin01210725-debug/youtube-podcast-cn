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


def run_sherpa_backend(
    audio_path: Path,
    bounds: SpeakerBounds,
    *,
    runtime: DiarizationRuntime | None = None,
    runner: Runner = subprocess.run,
    progress_callback: Callable[[int], None] | None = None,
) -> list[DiarizationTurn]:
    """Run the isolated worker and validate its JSON boundary."""
    selected_runtime = runtime or ensure_diarization_runtime()
    descriptor, output_name = tempfile.mkstemp(
        prefix=".diarization-output-", suffix=".json", dir=audio_path.parent
    )
    os.close(descriptor)
    output_path = Path(output_name)
    output_path.unlink()
    command = [
        str(selected_runtime.python),
        str(ROOT / "scripts/sherpa_diarization_worker.py"),
        "--audio",
        str(audio_path),
        "--segmentation-model",
        str(selected_runtime.segmentation_model),
        "--embedding-model",
        str(selected_runtime.embedding_model),
        "--output",
        str(output_path),
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
        if progress_callback is not None:
            for match in re.finditer(r"(?m)^PROGRESS (\d{1,3})$", stderr):
                progress_callback(min(100, int(match.group(1))))
        if completed.returncode != 0:
            detail = stderr[-500:].strip() or "未知错误"
            raise DiarizationRuntimeError(f"声纹分离失败: {detail}")
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
        return sorted(turns, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id))
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
