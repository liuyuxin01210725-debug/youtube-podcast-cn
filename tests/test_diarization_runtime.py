#!/usr/bin/env python3
"""Offline tests for the isolated sherpa-onnx diarization runtime."""

from __future__ import annotations

import hashlib
import contextlib
import inspect
import io
import json
import multiprocessing
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
import setup_diarization as setup_module  # noqa: E402
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


def _hold_setup_lock(path: str, acquired: object, release: object) -> None:
    with setup_module._setup_lock(Path(path)):
        acquired.set()
        release.wait(5)


def _acquire_setup_lock(path: str, acquired: object) -> None:
    with setup_module._setup_lock(Path(path)):
        acquired.set()


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
        self.assertEqual(fingerprint["stitching_version"], 3)
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
            bounds=SpeakerBounds(exact=2),
        )

        self.assertEqual({turn.speaker_id for turn in stitched}, {"SPEAKER_00", "SPEAKER_01"})
        self.assertTrue(all(turn.end_ms <= 750 for turn in stitched[:2]))
        self.assertEqual(stitched[-1], DiarizationTurn(1_000, 1_500, "SPEAKER_01"))

    def test_maximum_weight_mapping_beats_adversarial_greedy_choice(self) -> None:
        scores = {
            ("L0", "G0"): 10.0,
            ("L0", "G1"): 9.0,
            ("L1", "G0"): 9.0,
            ("L1", "G1"): 0.0,
        }

        mapping = runtime_module._maximum_weight_mapping(
            ["L0", "L1"],
            ["G0", "G1"],
            scores,
            minimum_score=0.0,
        )

        self.assertEqual(mapping, {"L0": "G1", "L1": "G0"})

    def test_collapse_minor_speakers_merges_ghosts_by_prototype(self) -> None:
        turns = [
            DiarizationTurn(0, 100_000, "SPEAKER_00"),
            DiarizationTurn(100_000, 105_000, "SPEAKER_02"),
            DiarizationTurn(105_000, 400_000, "SPEAKER_06"),
        ]
        collapsed = runtime_module._collapse_minor_speakers(
            turns,
            {
                "SPEAKER_00": [1.0, 0.0],
                "SPEAKER_02": [0.95, 0.05],
                "SPEAKER_06": [0.0, 1.0],
            },
            min_ms=20_000,
        )
        self.assertEqual(
            {turn.speaker_id for turn in collapsed},
            {"SPEAKER_00", "SPEAKER_06"},
        )
        self.assertEqual(collapsed[1].speaker_id, "SPEAKER_00")

    def test_missing_prototypes_attach_to_dominant_instead_of_inventing(

        self,
    ) -> None:
        mapping = runtime_module._speaker_mapping(
            [
                DiarizationTurn(0, 500, "SPEAKER_00"),
                DiarizationTurn(500, 600, "SPEAKER_01"),
            ],
            [
                DiarizationTurn(800, 900, "SPEAKER_00"),
                DiarizationTurn(900, 1000, "SPEAKER_01"),
            ],
            overlap_start_ms=600,
            overlap_end_ms=800,
            bounds=SpeakerBounds(),
            existing_embeddings={
                "SPEAKER_00": [1.0, 0.0],
                "SPEAKER_01": [0.0, 1.0],
            },
            incoming_embeddings={},  # worker failed to publish prototypes
        )
        self.assertEqual(set(mapping.values()), {"SPEAKER_00"})
        self.assertEqual(mapping["SPEAKER_00"], "SPEAKER_00")
        self.assertEqual(mapping["SPEAKER_01"], "SPEAKER_00")

    def test_soft_closed_world_remaps_instead_of_inventing_without_exact_count(
        self,
    ) -> None:
        """Long interviews must not invent SPEAKER_02.. when prototypes exist."""
        mapping = runtime_module._speaker_mapping(
            [
                DiarizationTurn(0, 300, "SPEAKER_00"),
                DiarizationTurn(300, 600, "SPEAKER_01"),
            ],
            [
                DiarizationTurn(800, 900, "SPEAKER_00"),
                DiarizationTurn(900, 1000, "SPEAKER_01"),
            ],
            overlap_start_ms=600,
            overlap_end_ms=800,
            bounds=SpeakerBounds(),  # no exact → previously invented / failed later
            existing_embeddings={
                "SPEAKER_00": [1.0, 0.0],
                "SPEAKER_01": [0.0, 1.0],
            },
            incoming_embeddings={
                "SPEAKER_00": [0.2, 0.8],  # below 0.35 vs unused; still clearly 01
                "SPEAKER_01": [0.8, 0.2],
            },
        )
        self.assertEqual(
            mapping,
            {"SPEAKER_00": "SPEAKER_01", "SPEAKER_01": "SPEAKER_00"},
        )
        self.assertEqual(set(mapping.values()), {"SPEAKER_00", "SPEAKER_01"})

    def test_mapping_fails_when_capacity_is_full_without_identity_evidence(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            runtime_module.DiarizationRuntimeError, "可靠.*证据"
        ):
            runtime_module._speaker_mapping(
                [
                    DiarizationTurn(0, 300, "SPEAKER_00"),
                    DiarizationTurn(300, 600, "SPEAKER_01"),
                ],
                [
                    DiarizationTurn(800, 900, "SPEAKER_00"),
                    DiarizationTurn(900, 1000, "SPEAKER_01"),
                ],
                overlap_start_ms=600,
                overlap_end_ms=800,
                bounds=SpeakerBounds(exact=2),
            )

    def test_stitching_uses_voice_prototypes_when_overlap_is_silent(self) -> None:
        stitched = runtime_module._stitch_diarization_chunks(
            [
                DiarizationTurn(0, 400, "SPEAKER_00"),
                DiarizationTurn(400, 600, "SPEAKER_01"),
            ],
            [DiarizationTurn(800, 1000, "SPEAKER_00")],
            overlap_start_ms=600,
            overlap_end_ms=800,
            bounds=SpeakerBounds(exact=2),
            existing_embeddings={
                "SPEAKER_00": [1.0, 0.0],
                "SPEAKER_01": [0.0, 1.0],
            },
            incoming_embeddings={"SPEAKER_00": [0.99, 0.01]},
        )

        self.assertEqual(stitched[-1].speaker_id, "SPEAKER_00")

    def test_exact_count_uses_best_complete_prototype_mapping_when_confidence_is_low(
        self,
    ) -> None:
        mapping = runtime_module._speaker_mapping(
            [
                DiarizationTurn(0, 300, "SPEAKER_00"),
                DiarizationTurn(300, 600, "SPEAKER_01"),
            ],
            [
                DiarizationTurn(800, 900, "SPEAKER_00"),
                DiarizationTurn(900, 1000, "SPEAKER_01"),
            ],
            overlap_start_ms=600,
            overlap_end_ms=800,
            bounds=SpeakerBounds(exact=2),
            existing_embeddings={
                "SPEAKER_00": [1.0, 0.0],
                "SPEAKER_01": [0.0, 1.0],
            },
            incoming_embeddings={
                "SPEAKER_00": [-0.1, -0.9],
                "SPEAKER_01": [-0.9, -0.1],
            },
        )

        self.assertEqual(
            mapping,
            {
                "SPEAKER_00": "SPEAKER_00",
                "SPEAKER_01": "SPEAKER_01",
            },
        )

    def test_exact_one_never_invents_a_second_global_speaker(self) -> None:
        stitched = runtime_module._stitch_diarization_chunks(
            [DiarizationTurn(0, 400, "SPEAKER_00")],
            [DiarizationTurn(800, 1000, "SPEAKER_00")],
            overlap_start_ms=400,
            overlap_end_ms=800,
            bounds=SpeakerBounds(exact=1),
        )

        self.assertEqual({turn.speaker_id for turn in stitched}, {"SPEAKER_00"})

    def test_stitching_applies_mapping_after_an_initial_silent_chunk(self) -> None:
        stitched = runtime_module._stitch_diarization_chunks(
            [],
            [DiarizationTurn(800, 1000, "SPEAKER_03")],
            overlap_start_ms=400,
            overlap_end_ms=800,
            bounds=SpeakerBounds(),
            mapping={"SPEAKER_03": "SPEAKER_00"},
        )

        self.assertEqual(
            stitched,
            [DiarizationTurn(800, 1000, "SPEAKER_00")],
        )

    def test_fits_extra_chunk_speakers_before_the_hard_reject(self) -> None:
        """Seven raw IDs must be folded to the existing cap, then accepted.

        The old test required ``_validate_chunk_turns`` to reject this list
        before stitching. That is the DOAC failure: one sherpa chunk over the
        cap aborted the episode even when the extra ID was only a few milliseconds.
        """
        with self.assertRaises(ValueError):
            SpeakerBounds(maximum=8)

        raw = [
            DiarizationTurn(index * 10, index * 10 + 5, f"SPEAKER_{index:02d}")
            for index in range(7)
        ]
        fitted, embeddings = runtime_module._fit_turns_to_speaker_budget(
            raw, {}, SpeakerBounds()
        )
        runtime_module._validate_chunk_turns(fitted, SpeakerBounds())
        self.assertEqual(len(fitted), 7)
        self.assertEqual(
            {turn.speaker_id for turn in fitted},
            {f"SPEAKER_{index:02d}" for index in range(1, 7)},
        )
        self.assertEqual(fitted[0], DiarizationTurn(0, 5, "SPEAKER_06"))
        self.assertEqual(embeddings, {})

        exact, _ = runtime_module._fit_turns_to_speaker_budget(
            raw, {}, SpeakerBounds(exact=2)
        )
        runtime_module._validate_chunk_turns(exact, SpeakerBounds(exact=2))
        self.assertEqual(
            {turn.speaker_id for turn in exact},
            {"SPEAKER_05", "SPEAKER_06"},
        )

    def test_long_extra_speakers_fit_to_cap_and_follow_nearest_embedding(self) -> None:
        """Duration collapse keeps every ID at or above 20s. The budget fold must not."""
        specs = [
            (0, 50_000, "SPEAKER_00", [1.0, 0.0]),
            (50_000, 100_000, "SPEAKER_01", [0.0, 1.0]),
            (100_000, 121_000, "SPEAKER_02", [0.2, 0.8]),
            (121_000, 151_000, "SPEAKER_03", [0.9, 0.1]),
            (151_000, 191_000, "SPEAKER_04", [-1.0, 0.0]),
            (191_000, 231_000, "SPEAKER_05", [0.0, -1.0]),
            (231_000, 261_000, "SPEAKER_06", [0.95, 0.05]),
        ]
        raw = [
            DiarizationTurn(start, end, speaker)
            for start, end, speaker, _vector in specs
        ]
        prototypes = {speaker: vector for _start, _end, speaker, vector in specs}
        untouched = runtime_module._collapse_minor_speakers(
            raw, prototypes, min_ms=20_000
        )
        self.assertEqual(len({turn.speaker_id for turn in untouched}), 7)

        fitted, embeddings = runtime_module._fit_turns_to_speaker_budget(
            raw, prototypes, SpeakerBounds()
        )
        runtime_module._validate_chunk_turns(fitted, SpeakerBounds())
        self.assertEqual(len({turn.speaker_id for turn in fitted}), 6)
        merged = next(turn for turn in fitted if turn.start_ms == 100_000)
        self.assertEqual(merged.speaker_id, "SPEAKER_01")
        self.assertNotIn("SPEAKER_02", embeddings)
        self.assertEqual(embeddings["SPEAKER_01"], [0.1, 0.9])
        self.assertEqual(embeddings["SPEAKER_00"], [1.0, 0.0])
        self.assertEqual(set(embeddings), {turn.speaker_id for turn in fitted})

    def test_first_chunk_over_cap_is_fitted_before_stitch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime = SimpleNamespace(
                python=base / "python",
                model_dir=base,
                segmentation_model=base / "segmentation.onnx",
                embedding_model=base / "embedding.onnx",
                fingerprint={"backend": "fake"},
            )

            def fake_runner(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                start = int(command[command.index("--start-ms") + 1])
                output = Path(command[command.index("--output") + 1])
                origin = 0 if start == 0 else 700
                turns = [
                    {
                        "start_ms": origin + index * 10,
                        "end_ms": origin + index * 10 + 5,
                        "speaker_id": f"SPEAKER_{index:02d}",
                    }
                    for index in range(7)
                ]
                embeddings = {
                    f"SPEAKER_{index:02d}": [
                        1.0 if axis == index else 0.0 for axis in range(7)
                    ]
                    for index in range(7)
                }
                output.write_text(
                    json.dumps({"turns": turns, "embeddings": embeddings}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "PROGRESS 100\n")

            turns = run_sherpa_backend(
                FIXTURE,
                SpeakerBounds(),
                runtime=runtime,
                runner=fake_runner,
                chunk_duration_ms=600,
                chunk_overlap_ms=200,
            )

        self.assertEqual(len(turns), 14)
        self.assertLessEqual(len({turn.speaker_id for turn in turns}), 6)
        self.assertNotIn("SPEAKER_00", {turn.speaker_id for turn in turns})
        self.assertIn(
            DiarizationTurn(0, 5, "SPEAKER_06"),
            turns,
        )

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
                "fingerprint": {"legacy_pipeline_values": "must_not_reinstall"},
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

            setup_module.setup_diarization(
                runtime_root=runtime_root,
                model_root=model_root,
                manifest_path=manifest_path,
                requirements_path=requirements_path,
                opener=mock.Mock(side_effect=AssertionError("no network")),
                runner=mock.Mock(side_effect=AssertionError("no subprocess")),
            )

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

    def test_parent_passes_exact_count_to_every_long_audio_worker(self) -> None:
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
            progress: list[int] = []

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
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "",
                    "PROGRESS 0\nPROGRESS 50\nPROGRESS 100\n",
                )

            turns = run_sherpa_backend(
                FIXTURE,
                SpeakerBounds(exact=2),
                runtime=runtime,
                runner=fake_runner,
                chunk_duration_ms=600,
                chunk_overlap_ms=200,
                progress_callback=progress.append,
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
            self.assertEqual(progress, [50, 100])
            for call in calls:
                self.assertEqual(
                    call[call.index("--num-speakers") + 1],
                    "2",
                )

    def test_production_progress_is_emitted_before_worker_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime = SimpleNamespace(
                python=base / "python",
                model_dir=base,
                segmentation_model=base / "segmentation.onnx",
                embedding_model=base / "embedding.onnx",
                fingerprint={"backend": "fake"},
            )
            events: list[object] = []

            class FakeProcess:
                def __init__(self) -> None:
                    self.stderr = io.StringIO("PROGRESS 10\nPROGRESS 100\n")
                    self.returncode: int | None = None

                def wait(self) -> int:
                    events.append("wait")
                    self.returncode = 0
                    return 0


            def fake_popen(
                command: list[str], **kwargs: object
            ) -> FakeProcess:
                output = Path(command[command.index("--output") + 1])
                output.write_text(json.dumps({"turns": []}), encoding="utf-8")
                return FakeProcess()

            run_sherpa_backend(
                FIXTURE,
                SpeakerBounds(),
                runtime=runtime,
                runner=mock.Mock(side_effect=AssertionError("subprocess.run forbidden")),
                popen_factory=fake_popen,
                progress_callback=lambda value: events.append(value),
            )

            self.assertLess(events.index(10), events.index("wait"))
            self.assertLess(events.index(100), events.index("wait"))

    def test_production_progress_is_throttled_to_five_percent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runtime = SimpleNamespace(
                python=base / "python",
                model_dir=base,
                segmentation_model=base / "segmentation.onnx",
                embedding_model=base / "embedding.onnx",
                fingerprint={"backend": "fake"},
            )
            progress: list[int] = []

            class FakeProcess:
                def __init__(self) -> None:
                    self.stderr = io.StringIO(
                        "".join(f"PROGRESS {value}\n" for value in range(101))
                    )

                def wait(self) -> int:
                    return 0

            def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
                output = Path(command[command.index("--output") + 1])
                output.write_text(json.dumps({"turns": []}), encoding="utf-8")
                return FakeProcess()

            run_sherpa_backend(
                FIXTURE,
                SpeakerBounds(),
                runtime=runtime,
                popen_factory=fake_popen,
                progress_callback=progress.append,
            )

            self.assertEqual(progress[0], 0)
            self.assertEqual(progress[-1], 100)
            self.assertLessEqual(len(progress), 22)

    def test_streaming_worker_is_terminated_on_keyboard_interrupt(self) -> None:
        events: list[str] = []

        class InterruptingStream:
            def __init__(self) -> None:
                self.lines = iter(["PROGRESS 10\n"])

            def __iter__(self) -> object:
                return self

            def __next__(self) -> str:
                try:
                    return next(self.lines)
                except StopIteration:
                    raise KeyboardInterrupt

        class FakeProcess:
            def __init__(self) -> None:
                self.stderr = InterruptingStream()

            def terminate(self) -> None:
                events.append("terminate")

            def wait(self, timeout: int | None = None) -> int:
                events.append(f"wait:{timeout}")
                return 0

            def kill(self) -> None:
                events.append("kill")

        with self.assertRaises(KeyboardInterrupt):
            runtime_module._stream_worker(
                ["worker"],
                popen_factory=lambda *args, **kwargs: FakeProcess(),
                on_progress=lambda value: None,
            )

        self.assertEqual(events, ["terminate", "wait:5"])

    def test_publish_restores_previous_directory_after_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            staging = base / "runtime.partial"
            destination = base / "runtime"
            staging.mkdir()
            destination.mkdir()
            (staging / "version").write_text("new", encoding="utf-8")
            (destination / "version").write_text("old", encoding="utf-8")
            original_replace = Path.replace

            def interrupt_staging(source: Path, target: Path) -> Path:
                if source == staging and target == destination:
                    raise KeyboardInterrupt
                return original_replace(source, target)

            with mock.patch.object(Path, "replace", interrupt_staging):
                with self.assertRaises(KeyboardInterrupt):
                    setup_module._publish_directory(staging, destination)

            self.assertEqual(
                (destination / "version").read_text(encoding="utf-8"),
                "old",
            )

    def test_pair_publication_rolls_back_models_when_runtime_publish_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            model_staging = base / "models.partial"
            runtime_staging = base / "runtime.partial"
            model_destination = base / "models"
            runtime_destination = base / "runtime"
            for path, value in (
                (model_staging, "new-models"),
                (runtime_staging, "new-runtime"),
                (model_destination, "old-models"),
                (runtime_destination, "old-runtime"),
            ):
                path.mkdir()
                (path / "version").write_text(value, encoding="utf-8")
            original_replace = Path.replace

            def interrupt_runtime(source: Path, target: Path) -> Path:
                if source == runtime_staging and target == runtime_destination:
                    raise KeyboardInterrupt
                return original_replace(source, target)

            with mock.patch.object(Path, "replace", interrupt_runtime):
                with self.assertRaises(KeyboardInterrupt):
                    setup_module._publish_directories(
                        [
                            (model_staging, model_destination),
                            (runtime_staging, runtime_destination),
                        ]
                    )

            self.assertEqual(
                (model_destination / "version").read_text(encoding="utf-8"),
                "old-models",
            )
            self.assertEqual(
                (runtime_destination / "version").read_text(encoding="utf-8"),
                "old-runtime",
            )

    def test_setup_lock_serializes_concurrent_installers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            lock_path = str(Path(directory) / "setup.lock")
            first_acquired = context.Event()
            release_first = context.Event()
            second_acquired = context.Event()
            first = context.Process(
                target=_hold_setup_lock,
                args=(lock_path, first_acquired, release_first),
            )
            second = context.Process(
                target=_acquire_setup_lock,
                args=(lock_path, second_acquired),
            )
            first.start()
            self.assertTrue(first_acquired.wait(2))
            second.start()
            self.assertFalse(second_acquired.wait(0.2))
            release_first.set()
            self.assertTrue(second_acquired.wait(2))
            first.join(2)
            second.join(2)
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)

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

    def test_worker_reserves_final_progress_for_prototype_publication(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            worker._progress_callback(10, 10)

        self.assertEqual(stderr.getvalue().strip(), "PROGRESS 95")

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
