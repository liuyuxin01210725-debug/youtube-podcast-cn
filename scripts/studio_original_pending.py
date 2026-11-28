#!/usr/bin/env python3
"""Package the selected MOSTPRISE Chinese original in a local review library."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from library_write_guard import (
    CatalogWriteGuardError,
    atomic_write_guarded_catalog,
    decode_catalog_bytes,
    load_local_catalog_baseline,
)

_CATALOG_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

EPISODE_ID = "mostprise-69320d8b4ef12f9fe9f8e1fe"
EPISODE_URL = "https://www.xiaoyuzhoufm.com/episode/69320d8b4ef12f9fe9f8e1fe"
AUDIO = "mostprise-original.m4a"
RAW = "mostprise-original-asr-raw.json"
LOCK = "mostprise-source-lock.json"
RECEIPT = "mostprise-asr-receipt.json"
OFFICIAL = "mostprise-official-audio-binding.json"
META = "INPUT-METADATA.json"
PLAYBACK = "output.mp3"
TRANSCRIPT = "transcript.pending.json"
MANIFEST = "manifest.json"
PENDING_JOB_FIELDS = {"source_job_id", "ready", "complete", "translated", "tts"}
EVIDENCE = Path(__file__).resolve().parents[2] / "evidence"

# Pin the independently supplied authority and receipts. No caller-selected
# fingerprints, URL, language or episode ID can widen this admission.
TRUSTED_METADATA = {
    META: "7846b657f830fea791abb35ca59ba94006febeb462702c08c56358765bd4e762",
    LOCK: "fa1064a1db151a113ec683905325e53e422b64edbd098c427541612b70b4bc19",
    RECEIPT: "ff2b327faa0e9471346f0dfa800811f423a88c17e531807d3782914664d8768d",
    OFFICIAL: "a7eafd8d2c1df86db79de08a70d2758bd55f731070940b2cda043745ef290244",
}
SOURCE_KEYS = ("title", "show", "host", "guest", "source_url", "audio_url")
MP3_OPTIONS = (
    "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
    "-af", "aresample=async=1:min_comp=0:min_hard_comp=0:first_pts=0",
    "-c:a", "libmp3lame", "-q:a", "2", "-write_xing", "1", "-f", "mp3",
)
UTC8 = timezone(timedelta(hours=8), "Asia/Shanghai")


class IntakeError(ValueError):
    """Safe, payload-free error message."""


def require(condition, message):
    if not condition:
        raise IntakeError(message)


def real_directory(path):
    path = Path(path).absolute()
    require(path == path.resolve(strict=True) and path.is_dir(),
            "需要真实目录，拒绝链接及路径穿越")
    return path


def regular_file(info):
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            "仅接受独立的普通文件，拒绝链接及特殊文件")
    return info


def open_regular(path):
    incoming = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
    try:
        regular_file(os.fstat(incoming.fileno()))
    except BaseException:
        incoming.close()
        raise
    return incoming


def read_bytes(path):
    with open_regular(path) as incoming:
        return incoming.read()


def write_bytes(path, data):
    with path.open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with open_regular(path) as incoming:
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@contextmanager
def library_lock(library):
    descriptor = os.open(library, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def checked_inputs(inputs):
    proofs = {name: read_bytes(inputs / name) for name in TRUSTED_METADATA}
    require(all(digest(proofs[name]) == sha for name, sha in TRUSTED_METADATA.items()),
            "元数据或授权绑定与此选集不符")
    meta, lock, receipt, official = (json.loads(proofs[name]) for name in (META, LOCK, RECEIPT, OFFICIAL))
    source = {key: lock[key] for key in SOURCE_KEYS}
    require(lock["id"] == EPISODE_ID and source["source_url"] == EPISODE_URL
            and (source["show"], source["host"], source["guest"]) == ("有个鲸喜MOSTPRISE", "Henrik", "Yikai")
            and all(meta[key] == value for key, value in source.items())
            and official["supplied_audio_filename_found_in_official_html"] is True
            and any(source["audio_url"] in part for part in official["matching_public_metadata_snippets"]),
            "不是已选中的 MOSTPRISE 官方原音")
    audio_sha, raw_sha = lock["audio"]["sha256"], lock["asr_evidence"]["raw_sha256"]
    size, duration = lock["audio"]["bytes"], lock["duration_s"]
    count = lock["asr_evidence"]["segment_count"]
    require(lock["asr_evidence"]["source_audio_binding"] is True
            and (audio_sha, raw_sha, size, duration, count) == (
                meta["original_sha256"], meta["asr_raw_sha256"], meta["original_bytes"],
                meta["duration_sec"], meta["asr_segments"])
            and (audio_sha, raw_sha, duration, count) == (
                receipt["audio_sha256"], receipt["asr_sha256"],
                receipt["audio_duration_s"], receipt["segments"])
            and all(receipt[field] is True for field in
                    ("offline", "not_translation", "not_listening_proof", "not_ready_or_published"))
            and receipt["paid_calls"] == 0, "离线 ASR 收据与来源锁不匹配")
    audio = inputs / AUDIO
    require(regular_file(audio.lstat()).st_size == size and file_digest(audio) == audio_sha,
            "原音字节数或 SHA-256 不符")
    proofs[RAW] = read_bytes(inputs / RAW)
    require(digest(proofs[RAW]) == raw_sha, "原始 ASR SHA-256 不符")
    raw = json.loads(proofs[RAW])
    result = raw["result"]
    require(raw["source_audio_sha256"] == audio_sha and raw["source_duration_s"] == duration
            and raw["offline"] is True and raw["new_downloads"] is False and raw["paid_calls"] == 0
            and raw["model_local"] == receipt["model"] and "whisper-small-mlx" in receipt["model"]
            and result["language"] == "zh", "ASR 音源、语言或离线模型不符")
    segments = result["segments"]
    require(isinstance(segments, list) and len(segments) == count and count > 0,
            "ASR 分段数不符")
    units, ids = [], set()
    for part in segments:
        ident, start, end, content = (part[key] for key in ("id", "start", "end", "text"))
        require(type(ident) is int and ident >= 0 and ident not in ids
                and all(type(t) in (int, float) and math.isfinite(t) for t in (start, end))
                and 0 <= start < min(end, duration)
                and (not units or start >= units[-1]["start"])
                and isinstance(content, str) and content.strip(), "ASR 分段结构或时间轴无效")
        ids.add(ident)
        units.append({"id": ident, "start": start, "end": min(end, duration),
                      "text": content, "status": "pending_review"})
    max_end = max(part["end"] for part in segments)
    require(units[0]["start"] == receipt["first_start_s"]
            and max_end == receipt["last_end_s"] == meta["raw_asr_max_end_sec"]
            and max_end - duration <= 0.101, "ASR 边界与收据不符")
    asr = {
        "status": "asr_unreviewed", "model": "whisper-small-mlx", "language": "zh",
        "segment_count": count, "raw_min_start_s": units[0]["start"], "raw_max_end_s": max_end,
        "raw_overrun_s": round(max(0, max_end - duration), 6),
        "clamped_end_count": sum(part["end"] > duration for part in segments),
        "normalization": "clip segment end to original container duration in playback copy only",
        "listening_verified": False, "completeness_verified": False,
    }
    if "license_status" in lock:
        source["license_status"] = lock["license_status"]
    return source, proofs, audio_sha, raw_sha, duration, asr, units


def media_command(tool, args):
    executable = shutil.which(tool)
    require(executable, f"缺少本地 {tool}")
    try:
        result = subprocess.run([executable, *args], check=True, capture_output=True,
                                text=True, timeout=300)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise IntakeError(f"本地 {tool} 媒体校验或转换失败") from None
    return result.stdout


def media_info(path, codec):
    info = json.loads(media_command("ffprobe", [
        "-v", "error", "-protocol_whitelist", "file", "-show_entries",
        "format=format_name,start_time,duration:stream=codec_name,codec_type,sample_rate,channels",
        "-of", "json", str(path),
    ]))
    container, streams = info["format"], info["streams"]
    expected_format = "m4a" if codec == "aac" else "mp3"
    require(len(streams) == 1 and streams[0]["codec_type"] == "audio"
            and streams[0]["codec_name"] == codec
            and expected_format in container["format_name"].split(","), "音频编码或封装无效")
    sample_rate, channels = int(streams[0]["sample_rate"]), streams[0]["channels"]
    frames = media_command("ffmpeg", [
        "-v", "error", "-nostdin", "-xerror", "-err_detect", "explode",
        "-protocol_whitelist", "file", "-i", str(path), "-map", "0:a:0",
        "-c:a", "pcm_s16le", "-f", "framecrc", "-",
    ]).splitlines()
    require(f"#tb 0: 1/{sample_rate}" in frames, "解码时间基无效")
    first = last = None
    count = samples = gap_count = gap_samples = 0
    first_gap_at = None
    for line in frames:
        if line.startswith("#"):
            continue
        stream, _, pts, duration, size, *_ = line.split(",")
        pts, duration = int(pts), int(duration)
        require(int(stream) == 0 and duration > 0 and int(size) == duration * channels * 2
                and (last is None or pts >= last), "解码音频帧重叠或无效")
        if last is not None and pts > last:
            if first_gap_at is None:
                first_gap_at = last
            gap_count += 1
            gap_samples += pts - last
        if first is None:
            first = pts
        last = pts + duration
        samples += duration
        count += 1
    require(first == 0 and samples > 0, "音频没有从零开始的解码覆盖")
    return {
        "codec": codec, "format": container["format_name"],
        "container_start_s": float(container["start_time"]),
        "container_duration_s": float(container["duration"]),
        "sample_rate_hz": sample_rate, "channels": channels, "decoded_frames": count,
        "decoded_samples_per_channel": samples, "decoded_first_sample_pts": first,
        "decoded_end_sample_pts": last, "decoded_duration_s": samples / sample_rate,
        "decoded_timeline_duration_s": last / sample_rate,
        "decoded_gap_count": gap_count, "decoded_gap_samples": gap_samples,
        "decoded_first_gap_at_sample": first_gap_at,
    }


def validate_coverage(source, playback, duration, units):
    require(abs(source["container_duration_s"] - duration) <= 0.002
            and abs(source["decoded_timeline_duration_s"] - duration) <= 0.05
            and abs(playback["container_duration_s"] - duration) <= 0.1
            and all(source[key] == playback[key] for key in ("sample_rate_hz", "channels"))
            and playback["decoded_gap_count"] == 0
            and playback["decoded_samples_per_channel"] ==
            source["decoded_samples_per_channel"] + source["decoded_gap_samples"]
            and max(unit["end"] for unit in units) <=
            min(playback["decoded_duration_s"], playback["container_duration_s"]),
            "原音、MP3 或待核稿的时间覆盖不一致")


def _read_catalog_baseline(library):
    try:
        baseline = load_local_catalog_baseline(Path(library), id_pattern=_CATALOG_ID, reader=read_bytes)
    except CatalogWriteGuardError as error:
        raise IntakeError(str(error)) from None
    if baseline.old_index is None:
        return {"episodes": []}, None, baseline
    _, by_id = decode_catalog_bytes(baseline.old_index, id_pattern=_CATALOG_ID)
    return baseline.prior, by_id.get(EPISODE_ID), baseline


def read_catalog(path):
    catalog, listed, _ = _read_catalog_baseline(Path(path).parent)
    return catalog, listed


def stage_catalog(catalog, record, staging):
    path = staging / "library.json"
    write_bytes(path, json_bytes({**catalog, "updated_at": datetime.now(UTC8).isoformat(timespec="seconds"),
                                  "episodes": [*catalog["episodes"], record]}))
    return path


def package(inputs, library, bound):
    source, proofs, audio_sha, raw_sha, duration, asr, units = bound
    catalog_path = library / "library.json"
    catalog, listed, baseline = _read_catalog_baseline(library)
    episodes = library / "episodes"
    if episodes.exists() or episodes.is_symlink():
        real_directory(episodes)
    dest = episodes / EPISODE_ID
    present = dest.exists() or dest.is_symlink()
    require(present or listed is None, "目录已有同标识条目但缺少对应节目，拒绝覆盖")
    hashes = {AUDIO: audio_sha, **{name: digest(data) for name, data in proofs.items()}}

    def documents(playback_sha, created_at):
        transcript = json_bytes({
            "schema_version": 1, "episode_id": EPISODE_ID, "language": "zh",
            "kind": "original_audio_asr", "status": "asr_unreviewed",
            "timeline": "output.mp3.seconds", "source_audio_sha256": audio_sha,
            "playback_audio_sha256": playback_sha, "raw_asr_sha256": raw_sha,
            "segments": units,
        })
        record = {
            "id": EPISODE_ID, "title": f'{source["title"]} · {source["show"]}（逐段待核）',
            "source_title": source["title"], "show": source["show"],
            "host": source["host"], "guests": [source["guest"]], "source_url": EPISODE_URL,
            "created_at": created_at, "duration_sec": duration, "file": PLAYBACK,
            "language": "zh", "audio_kind": "original_audio", "transcript_status": "asr_unreviewed",
            "status": "pending_review", "transcript": TRANSCRIPT, "manifest": MANIFEST,
            "pending_units": len(units),
        }
        assets = {**hashes, PLAYBACK: playback_sha, TRANSCRIPT: digest(transcript),
                  "episode.json": digest(json_bytes(record))}
        return record, transcript, assets

    if present:
        real_directory(dest)
        manifest = json.loads(read_bytes(dest / MANIFEST))
        playback_hash = file_digest(dest / PLAYBACK)
        record, _, assets = documents(playback_hash, manifest["created_at"])
        validate_coverage(manifest["media"]["source"], manifest["media"]["playback"], duration, units)
        require(manifest["sha256"] == assets and manifest["episode_id"] == EPISODE_ID
                and manifest["status"] == "pending_review" and manifest["source"] == source
                and manifest["asr"] == asr and manifest["derivation"] == derivation(playback_hash, audio_sha)
                and (listed is None or (
                    not PENDING_JOB_FIELDS.intersection(listed)
                    and all(listed.get(key) == value for key, value in record.items())
                ))
                and all(file_digest(dest / name) == sha for name, sha in assets.items() if name != PLAYBACK),
                "现有节目或目录与已验证包冲突，拒绝修改")
        if listed is not None:
            return "already_present", dest
        # A process can stop after the episode rename and before the catalog rename.
        next_bytes = json_bytes({
            **catalog,
            "updated_at": datetime.now(UTC8).isoformat(timespec="seconds"),
            "episodes": [*catalog["episodes"], record],
        })
        try:
            atomic_write_guarded_catalog(
                catalog_path,
                next_bytes,
                baseline=baseline,
                id_pattern=_CATALOG_ID,
                reader=read_bytes,
            )
        except CatalogWriteGuardError as error:
            raise IntakeError(str(error)) from None
        return "catalog_recovered", dest

    with tempfile.TemporaryDirectory(prefix=".original-pending-", dir=library) as temporary:
        stage = Path(temporary)
        payload = stage / "episode"
        payload.mkdir(mode=0o700)
        original = payload / AUDIO
        with open_regular(inputs / AUDIO) as incoming, original.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        require(file_digest(original) == audio_sha, "复制后的原音 SHA-256 不符")
        original_media = media_info(original, "aac")
        require(original_media["decoded_gap_samples"] / original_media["sample_rate_hz"] <= 0.1,
                "原音解码时间戳间隙超出此接收路径的范围")
        mp3 = payload / PLAYBACK
        media_command("ffmpeg", [
            "-v", "error", "-nostdin", "-xerror", "-err_detect", "explode", "-n",
            "-protocol_whitelist", "file", "-i", str(original), *MP3_OPTIONS, str(mp3),
        ])
        playback_media = media_info(mp3, "mp3")
        validate_coverage(original_media, playback_media, duration, units)
        with mp3.open("rb") as encoded:
            os.fsync(encoded.fileno())
        playback_hash = file_digest(mp3)
        created_at = datetime.now(UTC8).isoformat(timespec="seconds")
        record, transcript, assets = documents(playback_hash, created_at)
        manifest = {
            "schema_version": 1, "episode_id": EPISODE_ID, "status": "pending_review",
            "created_at": created_at, "source": source, "sha256": assets, "asr": asr,
            "media": {"source": original_media, "playback": playback_media},
            "derivation": derivation(playback_hash, audio_sha),
        }
        for name, data in proofs.items():
            write_bytes(payload / name, data)
        write_bytes(payload / TRANSCRIPT, transcript)
        write_bytes(payload / "episode.json", json_bytes(record))
        write_bytes(payload / MANIFEST, json_bytes(manifest))
        next_bytes = json_bytes({
            **catalog,
            "updated_at": datetime.now(UTC8).isoformat(timespec="seconds"),
            "episodes": [*catalog["episodes"], record],
        })
        created_episodes = not episodes.exists()
        if created_episodes:
            episodes.mkdir()
        try:
            os.rename(payload, dest)
            try:
                atomic_write_guarded_catalog(
                    catalog_path,
                    next_bytes,
                    baseline=baseline,
                    id_pattern=_CATALOG_ID,
                    reader=read_bytes,
                    allowed_temp_names=frozenset({stage.name}),
                )
            except (OSError, CatalogWriteGuardError) as error:
                os.rename(dest, payload)
                if isinstance(error, CatalogWriteGuardError):
                    raise IntakeError(str(error)) from None
                raise
        except (OSError, IntakeError):
            if created_episodes:
                episodes.rmdir()
            raise
    return "created", dest


def derivation(playback_sha, audio_sha):
    return {
        "kind": "local_ffmpeg_transcode", "input": AUDIO, "output": PLAYBACK,
        "input_sha256": audio_sha, "output_sha256": playback_sha,
        "output_options": list(MP3_OPTIONS),
        "timeline": "FFmpeg fills small positive AAC PTS gaps with silence at the original sample rate",
        "trim": "no explicit trim; compared FFmpeg decoded sample coverage plus source PTS gaps",
    }


def admit(inputs, library, *, confirmed=False):
    require(confirmed is True, "需显式确认仅接收已选中的中文原音")
    try:
        inputs, library = real_directory(inputs), real_directory(library)
        require(EVIDENCE in library.parents and not library.is_relative_to(inputs)
                and not inputs.is_relative_to(library), "目标必须是隔离证据目录下独立存在的测试资料库")
        bound = checked_inputs(inputs)
        with library_lock(library):
            result, dest = package(inputs, library, bound)
            manifest_hash = file_digest(dest / MANIFEST)
    except IntakeError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        raise IntakeError("输入、媒体或隔离资料库结构无效，或本地读写失败") from None
    return {"result": result, "status": "pending_review", "episode_id": EPISODE_ID,
            "episode_dir": str(dest), "manifest_sha256": manifest_hash}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--confirm-selected-original", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        result = admit(args.inputs, args.library, confirmed=args.confirm_selected_original)
    except IntakeError as error:
        print(f"隔离接收失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
