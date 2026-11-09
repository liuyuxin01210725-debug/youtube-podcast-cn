#!/usr/bin/env python3
"""Isolated sherpa-onnx worker; this is the only module importing sherpa."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Iterable


def result_to_turns(
    result: Iterable[object], *, offset_ms: int = 0
) -> list[dict[str, object]]:
    """Convert sherpa's second-based segments to stable millisecond JSON."""
    turns = [
        {
            "start_ms": offset_ms
            + round(float(getattr(segment, "start")) * 1000),
            "end_ms": offset_ms + round(float(getattr(segment, "end")) * 1000),
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
    diarization_percentage = round(max(0, min(100, percentage)) * 0.95)
    print(f"PROGRESS {diarization_percentage}", file=sys.stderr, flush=True)
    return 0


def _atomic_write(path: Path, payload: object) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _format_error(error: Exception) -> str:
    detail = str(error).strip() or "未提供错误详情"
    return f"{type(error).__name__}: {detail}"


def _frame_bounds(
    *,
    total_frames: int,
    sample_rate: int,
    start_ms: int,
    end_ms: int | None,
) -> tuple[int, int]:
    start_frame = round(start_ms * sample_rate / 1000)
    requested_end_frame = (
        total_frames
        if end_ms is None
        else round(end_ms * sample_rate / 1000)
    )
    rounding_tolerance_frames = max(1, round(sample_rate / 1000))
    if requested_end_frame > total_frames:
        if requested_end_frame - total_frames > rounding_tolerance_frames:
            raise RuntimeError("声纹分块时间范围无效")
        end_frame = total_frames
    else:
        end_frame = requested_end_frame
    if not 0 <= start_frame < end_frame <= total_frames:
        raise RuntimeError("声纹分块时间范围无效")
    return start_frame, end_frame


def _extract_speaker_embeddings(
    sherpa_onnx: object,
    embedding_config: object,
    audio: object,
    result: list[object],
    *,
    sample_rate: int,
    maximum_seconds: int = 30,
) -> dict[str, list[float]]:
    import numpy

    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(embedding_config)
    grouped: dict[int, list[tuple[int, int]]] = {}
    for segment in result:
        speaker = int(getattr(segment, "speaker"))
        start = max(0, round(float(getattr(segment, "start")) * sample_rate))
        end = min(len(audio), round(float(getattr(segment, "end")) * sample_rate))
        if start < end:
            grouped.setdefault(speaker, []).append((start, end))
    embeddings: dict[str, list[float]] = {}
    sample_limit = maximum_seconds * sample_rate
    for speaker, ranges in grouped.items():
        selected: list[object] = []
        remaining = sample_limit
        for start, end in sorted(
            ranges, key=lambda item: item[1] - item[0], reverse=True
        ):
            if remaining <= 0:
                break
            count = min(end - start, remaining)
            selected.append(audio[start : start + count])
            remaining -= count
        if not selected:
            continue
        samples = numpy.concatenate(selected).astype("float32", copy=False)
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate, samples)
        stream.input_finished()
        if not extractor.is_ready(stream):
            continue
        vector = [float(value) for value in extractor.compute(stream)]
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            embeddings[f"SPEAKER_{speaker:02d}"] = [
                value / norm for value in vector
            ]
    return embeddings


def run_worker(args: argparse.Namespace) -> None:
    import sherpa_onnx
    import soundfile

    config = _build_config(
        sherpa_onnx,
        args.segmentation_model,
        args.embedding_model,
        args.num_speakers,
    )
    threshold = getattr(args, 'cluster_threshold', 0.5)
    if not 0 < threshold < 2:
        raise ValueError('声纹聚类阈值须介于 0 和 2 之间')
    config.clustering.threshold = threshold
    if args.smoke_test:
        return
    if args.audio is None or args.output is None:
        raise RuntimeError("正常运行必须提供 --audio 和 --output")

    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    start_ms = args.start_ms or 0
    with soundfile.SoundFile(args.audio) as source:
        sample_rate = source.samplerate
        if sample_rate != 16_000 or source.channels != 1:
            raise RuntimeError("声纹输入必须是 16 kHz 单声道 WAV")
        start_frame, end_frame = _frame_bounds(
            total_frames=len(source),
            sample_rate=sample_rate,
            start_ms=start_ms,
            end_ms=args.end_ms,
        )
        source.seek(start_frame)
        audio = source.read(
            frames=end_frame - start_frame,
            dtype="float32",
            always_2d=True,
        )
    print("PROGRESS 0", file=sys.stderr, flush=True)
    result = diarizer.process(audio[:, 0], callback=_progress_callback)
    if hasattr(result, "sort_by_start_time"):
        sorted_result = result.sort_by_start_time()
        if sorted_result is not None:
            result = sorted_result
    segments = list(result)
    embeddings = _extract_speaker_embeddings(
        sherpa_onnx,
        config.embedding,
        audio[:, 0],
        segments,
        sample_rate=sample_rate,
    )
    _atomic_write(
        args.output,
        {
            "turns": result_to_turns(segments, offset_ms=start_ms),
            "embeddings": embeddings,
        },
    )
    print("PROGRESS 100", file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run isolated sherpa diarization")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--segmentation-model", type=Path, required=True)
    parser.add_argument("--embedding-model", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--num-speakers", type=int)
    parser.add_argument("--min-speakers", type=int)
    parser.add_argument("--max-speakers", type=int)
    parser.add_argument("--cluster-threshold", type=float, default=0.5)
    parser.add_argument("--smoke-test", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_worker(args)
    except Exception as error:
        print(_format_error(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
