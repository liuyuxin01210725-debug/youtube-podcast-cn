#!/usr/bin/env python3
"""Isolated sherpa-onnx worker; this is the only module importing sherpa."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable


def result_to_turns(result: Iterable[object]) -> list[dict[str, object]]:
    """Convert sherpa's second-based segments to stable millisecond JSON."""
    turns = [
        {
            "start_ms": round(float(getattr(segment, "start")) * 1000),
            "end_ms": round(float(getattr(segment, "end")) * 1000),
            "speaker_id": f"SPEAKER_{int(getattr(segment, 'speaker')):02d}",
        }
        for segment in result
    ]
    return sorted(
        turns,
        key=lambda item: (
            int(item["start_ms"]),
            int(item["end_ms"]),
            str(item["speaker_id"]),
        ),
    )


def _build_config(
    sherpa_onnx: object,
    segmentation_model: Path,
    embedding_model: Path,
    num_speakers: int | None,
) -> object:
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation_model)
            )
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding_model)
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers if num_speakers is not None else -1,
            threshold=0.5,
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("sherpa-onnx 模型配置无效")
    return config


def _progress_callback(processed: int, total: int) -> int:
    percentage = 100 if total <= 0 else round(processed * 100 / total)
    print(f"PROGRESS {max(0, min(100, percentage))}", file=sys.stderr, flush=True)
    return 0


def _atomic_write(path: Path, payload: object) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_worker(args: argparse.Namespace) -> None:
    import sherpa_onnx
    import soundfile

    config = _build_config(
        sherpa_onnx,
        args.segmentation_model,
        args.embedding_model,
        args.num_speakers,
    )
    if args.smoke_test:
        return
    if args.audio is None or args.output is None:
        raise RuntimeError("正常运行必须提供 --audio 和 --output")

    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    audio, sample_rate = soundfile.read(
        args.audio, dtype="float32", always_2d=True
    )
    if sample_rate != 16_000 or audio.shape[1] != 1:
        raise RuntimeError("声纹输入必须是 16 kHz 单声道 WAV")
    print("PROGRESS 0", file=sys.stderr, flush=True)
    result = diarizer.process(audio[:, 0], callback=_progress_callback)
    if hasattr(result, "sort_by_start_time"):
        sorted_result = result.sort_by_start_time()
        if sorted_result is not None:
            result = sorted_result
    _atomic_write(args.output, {"turns": result_to_turns(result)})
    print("PROGRESS 100", file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run isolated sherpa diarization")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--segmentation-model", type=Path, required=True)
    parser.add_argument("--embedding-model", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--num-speakers", type=int)
    parser.add_argument("--min-speakers", type=int)
    parser.add_argument("--max-speakers", type=int)
    parser.add_argument("--smoke-test", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_worker(args)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
