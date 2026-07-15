#!/usr/bin/env python3
"""Offline tests for the isolated sherpa-onnx diarization runtime."""

from __future__ import annotations

import hashlib
import inspect
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

from diarization_core import DiarizationTurn, SpeakerBounds  # noqa: E402
import diarization_runtime as runtime_module  # noqa: E402
from diarization_runtime import (  # noqa: E402
    backend_fingerprint,
    ensure_diarization_runtime,
    load_model_manifest,
    run_sherpa_backend,
)
from setup_diarization import _allowed_download_url  # noqa: E402
import sherpa_diarization_worker as worker  # noqa: E402
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

    def test_segmentation_asset_is_pinned_to_verified_hugging_face_revision(
        self,
    ) -> None:
        manifest = load_model_manifest(MANIFEST)
        segmentation = next(
            asset for asset in manifest["assets"] if asset["name"] == "segmentation"
        )
        self.assertIn(
            "/resolve/9403a6902bb58e3d5ae8c7e77c3422de279db2e0/",
            segmentation["url"],
        )
        self.assertEqual(
            segmentation["target_sha256"],
            "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079",
        )

    def test_dependency_versions_are_pinned(self) -> None:
        self.assertEqual(
            REQUIREMENTS.read_text(encoding="utf-8").splitlines(),
            ["sherpa-onnx==1.13.2", "soundfile==0.14.0"],
        )

    def test_download_whitelist_accepts_official_hugging_face_xet_bridge_only(
        self,
    ) -> None:
        self.assertTrue(
            _allowed_download_url(
                "https://cas-bridge.xethub.hf.co/xet-bridge-us/model.onnx"
            )
        )
        self.assertFalse(
            _allowed_download_url(
                "https://cas-bridge.xethub.hf.co.attacker.example/model.onnx"
            )
        )
        self.assertFalse(
            _allowed_download_url(
                "https://unexpected.cas-bridge.xethub.hf.co/model.onnx"
            )
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

    def test_long_audio_ranges_limit_each_worker_and_keep_wide_overlap(self) -> None:
        self.assertTrue(hasattr(runtime_module, "_chunk_ranges"))
        self.assertEqual(
            runtime_module._chunk_ranges(
                1_700_000,
                chunk_duration_ms=900_000,
                overlap_ms=300_000,
            ),
            [(0, 900_000), (600_000, 1_500_000), (1_200_000, 1_700_000)],
        )
        self.assertEqual(
            runtime_module._chunk_ranges(
                120_000,
                chunk_duration_ms=900_000,
                overlap_ms=300_000,
            ),
            [(0, 120_000)],
        )

    def test_overlap_stitching_maps_reversed_local_speakers_to_global_ids(
        self,
    ) -> None:
        self.assertTrue(hasattr(runtime_module, "_stitch_diarization_chunks"))
        first = [
            DiarizationTurn(0, 650, "SPEAKER_00"),
            DiarizationTurn(650, 750, "SPEAKER_01"),
            DiarizationTurn(750, 900, "SPEAKER_00"),
        ]
        second = [
            DiarizationTurn(600, 700, "SPEAKER_01"),
            DiarizationTurn(700, 760, "SPEAKER_00"),
            DiarizationTurn(760, 1_000, "SPEAKER_01"),
            DiarizationTurn(1_000, 1_500, "SPEAKER_00"),
        ]

        stitched = runtime_module._stitch_diarization_chunks(
            first,
            second,
            overlap_start_ms=600,
            overlap_end_ms=900,
        )

        self.assertEqual({turn.speaker_id for turn in stitched}, {"SPEAKER_00", "SPEAKER_01"})
        self.assertTrue(all(turn.end_ms <= 750 for turn in stitched[:2]))
        self.assertEqual(stitched[-1], DiarizationTurn(1_000, 1_500, "SPEAKER_01"))

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

    def test_parent_runs_long_audio_in_independent_overlapping_workers(self) -> None:
        parameters = inspect.signature(run_sherpa_backend).parameters
        self.assertIn("chunk_duration_ms", parameters)
        self.assertIn("chunk_overlap_ms", parameters)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime = SimpleNamespace(
                python=base / "python",
                model_dir=base,
                segmentation_model=base / "segmentation.onnx",
                embedding_model=base / "embedding.onnx",
                fingerprint={"backend": "fake"},
            )
            calls: list[list[str]] = []

            def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                start = int(command[command.index("--start-ms") + 1])
                end = int(command[command.index("--end-ms") + 1])
                output = Path(command[command.index("--output") + 1])
                if (start, end) == (0, 600):
                    turns = [
                        {"start_ms": 0, "end_ms": 500, "speaker_id": "SPEAKER_00"},
                        {"start_ms": 500, "end_ms": 600, "speaker_id": "SPEAKER_01"},
                    ]
                else:
                    turns = [
                        {"start_ms": 400, "end_ms": 500, "speaker_id": "SPEAKER_01"},
                        {"start_ms": 500, "end_ms": 650, "speaker_id": "SPEAKER_00"},
                        {"start_ms": 650, "end_ms": 1_000, "speaker_id": "SPEAKER_01"},
                    ]
                output.write_text(json.dumps({"turns": turns}), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "PROGRESS 100\n")

            turns = run_sherpa_backend(
                FIXTURE,
                SpeakerBounds(exact=2),
                runtime=runtime,
                runner=fake_runner,
                chunk_duration_ms=600,
                chunk_overlap_ms=200,
            )

            self.assertEqual(len(calls), 2)
            self.assertEqual(
                [
                    (
                        call[call.index("--start-ms") + 1],
                        call[call.index("--end-ms") + 1],
                    )
                    for call in calls
                ],
                [("0", "600"), ("400", "1000")],
            )
            self.assertEqual(turns[-1], DiarizationTurn(650, 1_000, "SPEAKER_00"))

    def test_worker_adapter_rounds_seconds_and_sorts(self) -> None:
        self.assertIn("offset_ms", inspect.signature(result_to_turns).parameters)
        fake_result = [
            SimpleNamespace(start=1.2346, end=2.0004, speaker=1),
            SimpleNamespace(start=0.0004, end=0.9996, speaker=0),
        ]

        turns = result_to_turns(fake_result, offset_ms=600_000)

        self.assertEqual(
            turns,
            [
                {"start_ms": 600_000, "end_ms": 601_000, "speaker_id": "SPEAKER_00"},
                {"start_ms": 601_235, "end_ms": 602_000, "speaker_id": "SPEAKER_01"},
            ],
        )

    def test_worker_error_formatter_keeps_exception_type_when_message_is_empty(
        self,
    ) -> None:
        self.assertTrue(hasattr(worker, "_format_error"))
        self.assertEqual(
            worker._format_error(RuntimeError()), "RuntimeError: 未提供错误详情"
        )

    def test_worker_frame_bounds_tolerate_only_final_millisecond_rounding(
        self,
    ) -> None:
        self.assertTrue(hasattr(worker, "_frame_bounds"))
        self.assertEqual(
            worker._frame_bounds(
                total_frames=27_347_534,
                sample_rate=16_000,
                start_ms=1_200_000,
                end_ms=1_709_221,
            ),
            (19_200_000, 27_347_534),
        )
        with self.assertRaises(RuntimeError):
            worker._frame_bounds(
                total_frames=27_347_534,
                sample_rate=16_000,
                start_ms=1_200_000,
                end_ms=1_710_000,
            )


if __name__ == "__main__":
    unittest.main()
