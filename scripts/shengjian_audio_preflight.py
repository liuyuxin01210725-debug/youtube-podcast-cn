#!/usr/bin/env python3
"""ffmpeg/ffprobe preflight at the end of 声间 synthesis, before publish-ready.

Default hook is studio_publish_icloud.publish_job. This script is not an
orphan: a BLOCK or REVIEW verdict refuses publish-ready. Automatic metrics
never stand in for Chinese-source fidelity, semantics, or a real listen, and
never claim an audio-quality PASS.

Verdicts:
  BLOCK  — decode empty/corrupt or other definite technical failure
  REVIEW — long silence, clipping, loudness anomaly, or duration-vs-chars
           without a calibrated same-voice/speed/language baseline
  CLEAR  — automatic gates found no BLOCK/REVIEW item; still not ear-PASS
           and still not a substitute for human listening

A 45s inspection MP3 (first 15 + mid 15 + last 15) is written next to the
report, with source offsets and the source hash. Sources shorter than 45s
are emitted honestly; they are not padded or looped. The clip is not a new
production master and is not published into the library.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Units are written into every report so thresholds are not unitless.
DEFAULTS = {
    'silence_noise_db': -50.0,          # dBFS, silencedetect noise
    'silence_duration_sec': 5.0,        # seconds; > this is REVIEW, not BLOCK
    'clip_peak_dbfs': 0.0,              # dBFS; peak at or above is REVIEW
    'loudness_high_i_lufs': -8.0,       # LUFS integrated; above is REVIEW
    'loudness_low_i_lufs': -30.0,       # LUFS integrated; below is REVIEW
    'duration_vs_chars_tolerance': 0.25 # only with a calibrated baseline
}
SEGMENT_SEC = 15.0
PREVIEW_TARGET_SEC = 45.0


class PreflightError(RuntimeError):
    pass


def which_ffmpeg():
    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise PreflightError('本机缺少 ffmpeg/ffprobe，无法检测，按失败硬拦')
    return ffmpeg, ffprobe


def sha256_file(path: Path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run_cmd(cmd, *, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def probe(path: Path, ffprobe: str):
    cmd = [
        ffprobe, '-v', 'error', '-print_format', 'json',
        '-show_format', '-show_streams', str(path),
    ]
    result = run_cmd(cmd, timeout=60)
    if result.returncode != 0:
        raise PreflightError('ffprobe 无法读取音频，按损坏硬拦')
    try:
        data = json.loads(result.stdout or '{}')
    except json.JSONDecodeError as exc:
        raise PreflightError('ffprobe 输出无法解析，按损坏硬拦') from exc
    streams = [s for s in data.get('streams') or [] if s.get('codec_type') == 'audio']
    if not streams:
        raise PreflightError('没有音频流，按空文件硬拦')
    fmt = data.get('format') or {}
    try:
        duration = float(fmt.get('duration') or streams[0].get('duration') or 0)
    except (TypeError, ValueError) as exc:
        raise PreflightError('时长无法解析，按损坏硬拦') from exc
    if not math.isfinite(duration) or duration <= 0:
        raise PreflightError('时长为空或非有限值，按空/损坏硬拦')
    return {'duration_sec': duration, 'format': fmt, 'stream': streams[0]}


def decode_check(path: Path, ffmpeg: str):
    result = run_cmd(
        [ffmpeg, '-v', 'error', '-xerror', '-i', str(path), '-map', '0:a:0', '-f', 'null', '-'],
        timeout=180,
    )
    err = (result.stderr or '').strip()
    if result.returncode != 0:
        raise PreflightError(f'解码失败，按损坏硬拦: {err[:400]}')
    if re.search(r'Invalid data|corrupt|error while decoding|could not find codec', err, re.I):
        raise PreflightError(f'解码报错，按损坏硬拦: {err[:400]}')
    return True


def parse_silence(stderr: str, duration_sec: float):
    starts = [float(x) for x in re.findall(r'silence_start:\s*([0-9.]+)', stderr)]
    ends = [float(x) for x in re.findall(r'silence_end:\s*([0-9.]+)', stderr)]
    periods = []
    # Pair starts with ends; a trailing start (silence through EOF) uses duration.
    used_ends = 0
    for start in starts:
        end = duration_sec
        if used_ends < len(ends) and ends[used_ends] >= start:
            end = ends[used_ends]
            used_ends += 1
        periods.append({'start_sec': start, 'end_sec': end, 'duration_sec': max(0.0, end - start)})
    return periods


def detect_silence(path: Path, ffmpeg: str, *, noise_db: float, duration_sec: float, min_sec: float):
    result = run_cmd(
        [
            ffmpeg, '-i', str(path), '-af',
            f'silencedetect=noise={noise_db}dB:d={min_sec}',
            '-f', 'null', '-',
        ],
        timeout=180,
    )
    return parse_silence(result.stderr or '', duration_sec)


def detect_levels(path: Path, ffmpeg: str):
    """Peak in dBFS (astats) and integrated loudness in LUFS (ebur128)."""
    result = run_cmd(
        [
            ffmpeg, '-i', str(path), '-af',
            'astats=metadata=1:reset=0,ebur128=framelog=quiet',
            '-f', 'null', '-',
        ],
        timeout=180,
    )
    text = (result.stderr or '') + (result.stdout or '')
    peak = None
    def _num(raw):
        token = raw.strip().rstrip(',').lower()
        if token in {'-inf', 'inf', '+inf', 'nan', '-nan', '-'}:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        return value
    peaks = [v for v in (_num(x) for x in re.findall(r'Peak level dB:\s*(\S+)', text)) if v is not None]
    if peaks:
        peak = max(peaks)
    # ebur128 "I:" line, last occurrence is the integrated loudness.
    integrated = None
    matches = re.findall(r'\bI:\s*(\S+)\s*LUFS', text)
    parsed = [v for v in (_num(x) for x in matches) if v is not None]
    if parsed:
        integrated = parsed[-1]
    return {
        'peak_dbfs': peak,
        'integrated_lufs': integrated,
        'units': {'peak': 'dBFS', 'integrated': 'LUFS'},
        'raw_ok': result.returncode == 0,
    }


def duration_vs_chars(duration_sec, character_count, baseline):
    """Only valid with a calibrated same-voice/speed/language baseline.

    baseline = {chars_per_sec, voice, speed, language, source}
    Without it this returns REVIEW and must not pretend truncation or PASS.
    """
    item = {
        'code': 'duration_vs_chars',
        'severity': 'REVIEW',
        'units': {'duration': 'sec', 'tolerance': 'fraction'},
    }
    if not baseline:
        item['reason'] = '没有校准的同声线/速度/语言基线，时长对字数不得判定截断或通过，待人耳'
        item['character_count'] = character_count
        item['duration_sec'] = duration_sec
        return item
    required = ('chars_per_sec', 'voice', 'speed', 'language', 'source')
    missing = [key for key in required if not baseline.get(key)]
    try:
        rate = float(baseline.get('chars_per_sec'))
    except (TypeError, ValueError):
        rate = 0.0
        missing.append('chars_per_sec')
    if missing or rate <= 0:
        item['reason'] = '基线不完整或未校准，时长对字数不得判定截断或通过，待人耳'
        item['missing'] = missing
        return item
    if not character_count or character_count <= 0:
        item['reason'] = '无有效字数，时长对字数不得判定截断或通过，待人耳'
        return item
    expected = character_count / rate
    delta = abs(duration_sec - expected) / expected if expected else math.inf
    item['expected_sec'] = expected
    item['duration_sec'] = duration_sec
    item['delta_fraction'] = delta
    item['tolerance'] = DEFAULTS['duration_vs_chars_tolerance']
    item['baseline'] = {k: baseline[k] for k in required}
    if delta > DEFAULTS['duration_vs_chars_tolerance']:
        item['reason'] = '时长相对校准基线偏离超过 25%，待人耳（自动指标不能代忠实/语义）'
        return item
    item['severity'] = 'INFO'
    item['reason'] = '时长相对校准基线在 25% 内；仍不能代替人耳与中文源忠实判断'
    return item


def preview_offsets(duration_sec):
    """Return segment list. Short sources are a single honest clip, never padded."""
    if duration_sec <= 0:
        return []
    if duration_sec < PREVIEW_TARGET_SEC:
        return [{'role': 'full', 'start_sec': 0.0, 'end_sec': duration_sec, 'duration_sec': duration_sec}]
    mid_start = max(0.0, (duration_sec - SEGMENT_SEC) / 2.0)
    last_start = max(0.0, duration_sec - SEGMENT_SEC)
    segs = [
        {'role': 'head', 'start_sec': 0.0, 'end_sec': SEGMENT_SEC, 'duration_sec': SEGMENT_SEC},
        {'role': 'mid', 'start_sec': mid_start, 'end_sec': mid_start + SEGMENT_SEC, 'duration_sec': SEGMENT_SEC},
        {'role': 'tail', 'start_sec': last_start, 'end_sec': duration_sec, 'duration_sec': SEGMENT_SEC},
    ]
    return segs


def write_preview(path: Path, dest: Path, *, ffmpeg: str, duration_sec: float, source_hash: str):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    segs = preview_offsets(duration_sec)
    if not segs:
        raise PreflightError('无法计算抽检片段')
    if len(segs) == 1:
        # Honest short file: remux/re-encode the whole source, no pad, no loop.
        result = run_cmd(
            [ffmpeg, '-y', '-i', str(path), '-vn', '-acodec', 'libmp3lame', '-q:a', '4', str(dest)],
            timeout=180,
        )
        if result.returncode != 0:
            raise PreflightError('短片抽检 MP3 写出失败')
        return {'path': str(dest), 'segments': segs, 'padded': False, 'source_sha256': source_hash, 'honest_short': True}

    work = Path(tempfile.mkdtemp(prefix='shengjian-preview-'))
    try:
        parts = []
        for index, seg in enumerate(segs):
            part = work / f'part-{index}.mp3'
            # Cut on the decoded timeline to reduce encoder padding error, then
            # re-encode each part. Offsets recorded here are source offsets.
            result = run_cmd(
                [
                    ffmpeg, '-y', '-ss', f'{seg["start_sec"]:.6f}', '-t', f'{seg["duration_sec"]:.6f}',
                    '-i', str(path), '-vn', '-acodec', 'libmp3lame', '-q:a', '4', str(part),
                ],
                timeout=180,
            )
            if result.returncode != 0 or not part.is_file():
                raise PreflightError(f'抽检片段 {seg["role"]} 写出失败')
            parts.append(part)
        listing = work / 'concat.txt'
        listing.write_text(''.join(f"file '{part}'\n" for part in parts), encoding='utf-8')
        result = run_cmd(
            [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', str(listing), '-c:a', 'libmp3lame', '-q:a', '4', str(dest)],
            timeout=180,
        )
        if result.returncode != 0 or not dest.is_file():
            raise PreflightError('抽检 MP3 拼接失败')
        return {
            'path': str(dest),
            'segments': segs,
            'padded': False,
            'source_sha256': source_hash,
            'honest_short': False,
            'note': '生成品仅供抽检，不是新的生产正片；编码会有帧填充，偏移按源时间轴记录',
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_preflight(
    source,
    *,
    output_dir,
    character_count=None,
    baseline=None,
    thresholds=None,
    write_preview_mp3=True,
    studio_job=None,
):
    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = {**DEFAULTS, **(thresholds or {})}
    findings = []
    verdict = 'CLEAR'
    try:
        ffmpeg, ffprobe = which_ffmpeg()
    except PreflightError as exc:
        findings.append({'code': 'tooling', 'severity': 'BLOCK', 'reason': str(exc)})
        return _finish(source, output_dir, verdict='BLOCK', findings=findings, cfg=cfg, info={})

    if not source.is_file():
        findings.append({'code': 'missing', 'severity': 'BLOCK', 'reason': '源音频不存在，按失败硬拦'})
        return _finish(source, output_dir, verdict='BLOCK', findings=findings, cfg=cfg, info={})

    source_hash = sha256_file(source)
    info = {'source': str(source), 'source_sha256': source_hash}
    try:
        probed = probe(source, ffprobe)
        decode_check(source, ffmpeg)
    except PreflightError as exc:
        findings.append({'code': 'decode', 'severity': 'BLOCK', 'reason': str(exc)})
        return _finish(source, output_dir, verdict='BLOCK', findings=findings, cfg=cfg, info=info)

    duration = probed['duration_sec']
    info['duration_sec'] = duration
    info['duration_unit'] = 'sec'

    silences = detect_silence(
        source, ffmpeg, noise_db=cfg['silence_noise_db'],
        duration_sec=duration, min_sec=cfg['silence_duration_sec'],
    )
    long = [s for s in silences if s['duration_sec'] > cfg['silence_duration_sec'] + 1e-9]
    if long:
        findings.append({
            'code': 'long_silence',
            'severity': 'REVIEW',
            'reason': f'检测到超过 {cfg["silence_duration_sec"]}s 的长静音（单位秒，阈值可配）。正常停顿不作为损坏，不能伪断定损坏。',
            'units': {'duration': 'sec', 'noise': 'dBFS'},
            'noise_db': cfg['silence_noise_db'],
            'threshold_sec': cfg['silence_duration_sec'],
            'periods': long,
        })

    levels = detect_levels(source, ffmpeg)
    info['levels'] = levels
    peak = levels.get('peak_dbfs')
    if peak is not None and peak >= cfg['clip_peak_dbfs']:
        findings.append({
            'code': 'clipping',
            'severity': 'REVIEW',
            'reason': '峰值达到或超过削波阈值，待人耳确认（不是自动损坏判定）',
            'units': {'peak': 'dBFS'},
            'peak_dbfs': peak,
            'threshold_dbfs': cfg['clip_peak_dbfs'],
        })
    loud = levels.get('integrated_lufs')
    if loud is not None and (loud > cfg['loudness_high_i_lufs'] or loud < cfg['loudness_low_i_lufs']):
        findings.append({
            'code': 'loudness',
            'severity': 'REVIEW',
            'reason': '响度超出可配阈值，待人耳确认（自动指标不能代实听）',
            'units': {'integrated': 'LUFS'},
            'integrated_lufs': loud,
            'threshold_high_lufs': cfg['loudness_high_i_lufs'],
            'threshold_low_lufs': cfg['loudness_low_i_lufs'],
        })

    if studio_job is None:
        findings.append(duration_vs_chars(duration, character_count, baseline))
    else:
        from studio_publish_icloud import segment_integrity
        findings.append(segment_integrity(source, Path(studio_job), ffprobe=ffprobe,
                                          stream=probed['stream'], source_hash=source_hash))

    preview = None
    if write_preview_mp3:
        preview_path = output_dir / 'inspect-45s.mp3'
        try:
            preview = write_preview(source, preview_path, ffmpeg=ffmpeg, duration_sec=duration, source_hash=source_hash)
            # Confirm short sources were not padded up to 45s.
            _, ffprobe = which_ffmpeg()
            preview_probe = probe(preview_path, ffprobe)
            preview['output_duration_sec'] = preview_probe['duration_sec']
            if preview.get('honest_short') and preview_probe['duration_sec'] > duration + 1.0:
                findings.append({
                    'code': 'preview_padded',
                    'severity': 'BLOCK',
                    'reason': '短片抽检被填充，拒绝',
                })
        except PreflightError as exc:
            findings.append({'code': 'preview', 'severity': 'REVIEW', 'reason': str(exc)})

    severities = {item.get('severity') for item in findings}
    if 'BLOCK' in severities:
        verdict = 'BLOCK'
    elif 'REVIEW' in severities:
        verdict = 'REVIEW'
    else:
        verdict = 'CLEAR'
    return _finish(
        source, output_dir, verdict=verdict, findings=findings, cfg=cfg, info=info, preview=preview,
    )


def _finish(source, output_dir, *, verdict, findings, cfg, info, preview=None):
    publish_ready = False  # automatic metrics never grant publish-ready
    report = {
        'verdict': verdict,
        'publish_ready': publish_ready,
        'audio_quality_pass': False,
        'ear_required': True,
        'cannot_stand_in_for': ['chinese_source_fidelity', 'semantics', 'human_listen'],
        'thresholds': cfg,
        'threshold_units': {
            'silence_noise_db': 'dBFS',
            'silence_duration_sec': 'sec',
            'clip_peak_dbfs': 'dBFS',
            'loudness_high_i_lufs': 'LUFS',
            'loudness_low_i_lufs': 'LUFS',
            'duration_vs_chars_tolerance': 'fraction',
        },
        'findings': findings,
        'source_info': info,
        'preview': preview,
        'note': '本报告不是音质 PASS，也不能直接作为发布 ready',
    }
    report_path = Path(output_dir) / 'preflight-report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report['report_path'] = str(report_path)
    return report


def refuse_if_blocked_or_review(report):
    """Hook helper for publish_job. BLOCK and REVIEW cannot become publish-ready.

    CLEAR means automatic gates found no blocking item; the catalog write may
    proceed, but the report still must not claim an audio-quality PASS or that
    a human listen is unnecessary.
    """
    verdict = (report or {}).get('verdict')
    path = (report or {}).get('report_path')
    if verdict == 'BLOCK':
        raise PreflightError(f'音频预检硬拦，拒绝发布 ready: {path}')
    if verdict != 'CLEAR':
        raise PreflightError(f'音频预检待人耳（REVIEW/未通过），不能直接发布 ready: {path}')
    return report


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='声间音频预检（不发布、不换源、不上传）')
    p.add_argument('source')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--character-count', type=int, default=None)
    p.add_argument('--baseline-json', default=None, help='校准基线 JSON，缺则时长对字数只能 REVIEW')
    args = p.parse_args()
    baseline = None
    if args.baseline_json:
        baseline = json.loads(Path(args.baseline_json).read_text(encoding='utf-8'))
    report = run_preflight(
        args.source, output_dir=args.output_dir,
        character_count=args.character_count, baseline=baseline,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Exit 2 for BLOCK, 3 for REVIEW, 0 for CLEAR (still not quality PASS).
    sys.exit({'BLOCK': 2, 'REVIEW': 3, 'CLEAR': 0}.get(report['verdict'], 2))
