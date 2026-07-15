#!/usr/bin/env python3
"""Offline tests for the isolated sherpa-onnx diarization runtime."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from diarization_core import SpeakerBounds  # noqa: E402
from diarization_runtime import (  # noqa: E402
    backend_fingerprint,
    ensure_diarization_runtime,
    load_model_manifest,
    run_sherpa_backend,
)
from sherpa_diarization_worker import result_to_turns  # noqa: E402


FIXTURE = ROOT / "tests/fixtures/diarization-silence.wav"
MANIFEST = ROOT / "scripts/diarization_models.json"
REQUIREMENTS = ROOT / "requirements-diarization.txt"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RuntimeTests(unittest.TestCase):
    def test_manifest_excludes_reverb_and_has_hashes_and_licenses(self) -> None:
        manifest = load_model_manifest(MANIFEST)
        serialized = json.dumps(manifest).casefold()
        self.assertNotIn("reverb", serialized)
        self.assertEqual(manifest["sherpa_onnx_version"], "1.13.2")
        self.assertEqual(len(manifest["assets"]), 2)
        for asset in manifest["assets"]:
            self.assertRegex(asset["target_sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(asset["license"], {"MIT", "Apache-2.0"})

    def test_dependency_versions_are_pinned(self) -> None:
        self.assertEqual(
            REQUIREMENTS.read_text(encoding="utf-8").splitlines(),
            ["sherpa-onnx==1.13.2", "soundfile==0.14.0"],
        )

    def test_fingerprint_contains_hashes_but_no_download_urls(self) -> None:
        fingerprint = backend_fingerprint(load_model_manifest(MANIFEST))
        serialized = json.dumps(fingerprint).casefold()
        self.assertEqual(fingerprint["cluster_threshold"], 0.5)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("license_url", serialized)

    def test_silence_fixture_is_exact_mono_pcm(self) -> None:
        self.assertEqual(FIXTURE.stat().st_size, 32_044)
        with wave.open(str(FIXTURE), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getframerate(), 16_000)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getnframes(), 16_000)

    def test_valid_runtime_and_models_use_no_network_or_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime_root = base / ".diarization-venv"
            model_root = base / "models"
            manifest_path = base / "manifest.json"
            requirements_path = base / "requirements.txt"
            requirements_path.write_text("fake-package==1.0\n", encoding="utf-8")
            assets = []
            for name in ("segmentation", "embedding"):
                content = f"{name}-model".encode()
                target = f"{name}.onnx"
                model_root.mkdir(parents=True, exist_ok=True)
                (model_root / target).write_bytes(content)
                assets.append(
                    {
                        "name": name,
                        "url": f"https://github.com/example/{target}",
                        "archive_member": None,
                        "target": target,
                        "target_sha256": hashlib.sha256(content).hexdigest(),
                        "license": "MIT" if name == "segmentation" else "Apache-2.0",
                        "license_url": "https://github.com/example/license",
                    }
                )
            manifest = {
                "schema_version": 1,
                "backend": "sherpa-onnx",
                "sherpa_onnx_version": "1.13.2",
                "model_bundle": "test-models",
                "assets": assets,
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            python = runtime_root / "bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
            state = {
                "schema_version": 1,
                "requirements_sha256": sha256(requirements_path),
                "manifest_sha256": sha256(manifest_path),
                "fingerprint": backend_fingerprint(manifest),
            }
            (runtime_root / "runtime-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )

            runtime = ensure_diarization_runtime(
                runtime_root=runtime_root,
                model_root=model_root,
                manifest_path=manifest_path,
                requirements_path=requirements_path,
                opener=mock.Mock(side_effect=AssertionError("no network")),
                runner=mock.Mock(side_effect=AssertionError("no subprocess")),
            )

            self.assertEqual(runtime.python, python)
            self.assertEqual(runtime.segmentation_model, model_root / "segmentation.onnx")

    def test_parent_passes_exact_speaker_count_and_validates_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            python = base / "bin/python"
            python.parent.mkdir()
            python.write_text("", encoding="utf-8")
            segmentation = base / "segmentation.onnx"
            embedding = base / "embedding.onnx"
            segmentation.write_text("seg", encoding="utf-8")
            embedding.write_text("emb", encoding="utf-8")
            runtime = SimpleNamespace(
                python=python,
                model_dir=base,
                segmentation_model=segmentation,
                embedding_model=embedding,
                fingerprint={"backend": "fake"},
            )
            calls: list[list[str]] = []

            def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                output = Path(command[command.index("--output") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "turns": [
                                {
                                    "start_ms": 0,
                                    "end_ms": 900,
                                    "speaker_id": "SPEAKER_00",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "PROGRESS 100\n")

            turns = run_sherpa_backend(
                FIXTURE,
                SpeakerBounds(exact=2),
                runtime=runtime,
                runner=fake_runner,
            )

            self.assertIn("--num-speakers", calls[0])
            self.assertEqual(calls[0][calls[0].index("--num-speakers") + 1], "2")
            self.assertEqual(turns[0].speaker_id, "SPEAKER_00")

    def test_worker_adapter_rounds_seconds_and_sorts(self) -> None:
        fake_result = [
            SimpleNamespace(start=1.2346, end=2.0004, speaker=1),
            SimpleNamespace(start=0.0004, end=0.9996, speaker=0),
        ]

        turns = result_to_turns(fake_result)

        self.assertEqual(
            turns,
            [
                {"start_ms": 0, "end_ms": 1000, "speaker_id": "SPEAKER_00"},
                {"start_ms": 1235, "end_ms": 2000, "speaker_id": "SPEAKER_01"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
