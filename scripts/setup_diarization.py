#!/usr/bin/env python3
"""Atomically install the pinned local sherpa diarization runtime and models."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from diarization_runtime import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_REQUIREMENTS_PATH,
    DiarizationSetupError,
    backend_fingerprint,
    default_model_root,
    load_model_manifest,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_DOWNLOAD_HOSTS = {"github.com", "githubusercontent.com", "huggingface.co"}


def _check_platform() -> None:
    version = sys.version_info
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise DiarizationSetupError("当前声纹版本仅支持 macOS arm64")
    if not (version.major == 3 and 10 <= version.minor <= 14):
        raise DiarizationSetupError(
            "声纹运行时需要 Python 3.10 到 3.14;"
            "请安装后重试 bin/setup-podcast-diarization.sh"
        )


def _allowed_download_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and any(
        host == allowed or host.endswith(f".{allowed}")
        for allowed in _ALLOWED_DOWNLOAD_HOSTS
    )


def _download_asset(
    asset: dict[str, object],
    destination: Path,
    opener: Callable[..., object],
) -> None:
    url = str(asset["url"])
    if not _allowed_download_url(url):
        raise DiarizationSetupError("声纹模型下载域名不在白名单")
    request = urllib.request.Request(
        url, headers={"User-Agent": "youtube-podcast-cn/diarization-setup"}
    )
    response = opener(request, timeout=120)
    try:
        final_url = response.geturl()
        if not _allowed_download_url(final_url):
            raise DiarizationSetupError("声纹模型下载重定向到未授权域名")
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not destination.is_file() or destination.stat().st_size == 0:
        raise DiarizationSetupError("下载的声纹模型为空")
    if sha256_file(destination) != asset["target_sha256"]:
        raise DiarizationSetupError(f"{asset['name']} 模型 SHA-256 校验失败")


def _models_are_valid(model_root: Path, manifest: dict[str, object]) -> bool:
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        return False
    try:
        return all(
            isinstance(asset, dict)
            and (model_root / str(asset["target"])).is_file()
            and sha256_file(model_root / str(asset["target"]))
            == asset["target_sha256"]
            for asset in assets
        )
    except (OSError, KeyError):
        return False


def _publish_directory(staging: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    staging.replace(destination)


def setup_diarization(
    *,
    runtime_root: Path,
    model_root: Path,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    requirements_path: Path = DEFAULT_REQUIREMENTS_PATH,
    opener: Callable[..., object] = urllib.request.urlopen,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Install into staging directories, verify, then publish atomically."""
    _check_platform()
    manifest = load_model_manifest(manifest_path)
    if not requirements_path.is_file():
        raise DiarizationSetupError("缺少 requirements-diarization.txt")

    runtime_staging = runtime_root.with_name(f"{runtime_root.name}.partial-{os.getpid()}")
    model_staging = model_root.with_name(f"{model_root.name}.partial-{os.getpid()}")
    shutil.rmtree(runtime_staging, ignore_errors=True)
    shutil.rmtree(model_staging, ignore_errors=True)
    publish_models = not _models_are_valid(model_root, manifest)
    selected_models = model_staging if publish_models else model_root

    try:
        runtime_staging.parent.mkdir(parents=True, exist_ok=True)
        completed = runner(
            [sys.executable, "-m", "venv", str(runtime_staging)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise DiarizationSetupError(
                f"创建声纹隔离环境失败: {(completed.stderr or '')[-500:].strip()}"
            )
        isolated_python = runtime_staging / "bin/python"
        completed = runner(
            [
                str(isolated_python),
                "-m",
                "pip",
                "install",
                "--requirement",
                str(requirements_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise DiarizationSetupError(
                f"安装声纹依赖失败: {(completed.stderr or '')[-500:].strip()}"
            )

        if publish_models:
            model_staging.mkdir(parents=True)
            assets = manifest["assets"]
            if not isinstance(assets, list):
                raise DiarizationSetupError("声纹模型清单资产无效")
            for asset in assets:
                if not isinstance(asset, dict):
                    raise DiarizationSetupError("声纹模型资产无效")
                staging_file = model_staging / f"{asset['target']}.download"
                _download_asset(asset, staging_file, opener)
                staging_file.replace(model_staging / str(asset["target"]))
            if not _models_are_valid(model_staging, manifest):
                raise DiarizationSetupError("声纹模型发布前校验失败")

        assets_by_name = {
            str(asset["name"]): asset
            for asset in manifest["assets"]
            if isinstance(asset, dict)
        }
        completed = runner(
            [
                str(isolated_python),
                str(ROOT / "scripts/sherpa_diarization_worker.py"),
                "--segmentation-model",
                str(selected_models / str(assets_by_name["segmentation"]["target"])),
                "--embedding-model",
                str(selected_models / str(assets_by_name["embedding"]["target"])),
                "--smoke-test",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise DiarizationSetupError(
                f"声纹模型本地自检失败: {(completed.stderr or '')[-500:].strip()}"
            )

        state = {
            "schema_version": 1,
            "requirements_sha256": sha256_file(requirements_path),
            "manifest_sha256": sha256_file(manifest_path),
            "fingerprint": backend_fingerprint(manifest),
        }
        (runtime_staging / "runtime-state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if publish_models:
            _publish_directory(model_staging, model_root)
        _publish_directory(runtime_staging, runtime_root)
    except Exception:
        shutil.rmtree(runtime_staging, ignore_errors=True)
        shutil.rmtree(model_staging, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    manifest = load_model_manifest(DEFAULT_MANIFEST_PATH)
    parser = argparse.ArgumentParser(description="安装本地声纹分离运行时")
    parser.add_argument("--runtime-root", type=Path, default=ROOT / ".diarization-venv")
    parser.add_argument("--model-root", type=Path, default=default_model_root(manifest))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        setup_diarization(runtime_root=args.runtime_root, model_root=args.model_root)
    except DiarizationSetupError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    print("本地声纹分离运行时已安装并校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
