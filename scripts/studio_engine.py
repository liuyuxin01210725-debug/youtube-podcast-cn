#!/usr/bin/env python3
"""Persistent, editable studio jobs; ML models live only in child processes."""
from __future__ import annotations
import sys

if __name__ == '__main__':
    # Check before asyncio or HTTP imports bind the overridden socket class.
    from studio_recovery import StudioRecoveryError, require_translation_network

    try:
        require_translation_network()
    except StudioRecoveryError as error:
        raise SystemExit(f'{error.code}: {error}') from None

import argparse
import asyncio
from dataclasses import asdict
import errno
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import wave

import aiohttp
from edge_tts.exceptions import NoAudioReceived, WebSocketError

from podcast_cn import (fetch_video_info, download_english_subtitles, create_translation_client,
                        translate_with_resume, chunk_segments, get_provider_config, _is_exact_youtube_url,
                        video_metadata_from_info, PodcastPipelineError,
                        TranslationConfigurationError, TranslationValidationError)
from podcast_captions import CaptionPiece, SubtitleSegment, parse_vtt_pieces, group_caption_pieces
from diarization_core import (SpeakerBounds, align_caption_pieces, fill_unassigned_for_review,
                              validate_diarization)
from diarization_cache import assign_studio_voices
import diarization_runtime
from diarization_runtime import (DiarizationRuntimeError, DiarizationSetupError,
                                 ensure_diarization_runtime, run_sherpa_backend)
from studio_preview import select_preview_segments
from studio_progress import build_progress_payload
from studio_ads import ad_only_speakers, fold_ad_speakers, group_key, mark_sponsor_windows, split_group_key
from studio_document import check_source_coverage
from studio_edge_input import (EDGE_INPUT_VERSION, EDGE_SILENCE, SILENCE_FRAMES, SILENCE_RATE,
                               clean_tts_text, speakable_tts, valid_edge_input)
from studio_recovery import (
    StudioRecoveryError, TranslationJournal, _read_regular_bytes, _save_bytes, _sync_dir,
    decode_json_object, load_source,
    podcast_job_lock, save_source,
)
from studio_speakers import (SpeakerGateError, expected_speakers, requires_voice_mapping, use_male_slots,
                             validate_speaker_ids, validate_speaker_voices)
from temporary_audio import download_audio_only, sha256_file
from translation_budget import TranslationBudget, TranslationRequestError, supports_policy
from tts_backends import EDGE_ACCESS_DENIED_MESSAGE, SynthesisRequest, TTSBackendError, build_backend

ROOT = Path(__file__).resolve().parents[1]
CLONE_MODEL = 'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit'
ASR_MODEL = 'mlx-community/whisper-small.en-mlx'




class StudioInputError(ValueError):
    """Engine-owned validation message, safe for display."""
    pass


class StudioError(RuntimeError):
    """Only fixed, approved messages cross the subprocess/UI boundary."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def classify_failure(stderr, executable=''):
    text = stderr.casefold()
    if '429' in text or 'too many requests' in text or 'sign in to confirm' in text or 'login required' in text or 'not a bot' in text:
        return StudioError('source_access_denied', 'YouTube 限流或要求登录。请稍后重试，或选择已下载的本地音频/视频文件。')
    if any(value in text for value in ('localentrynotfounderror', 'cannot find an appropriate cached snapshot', 'not found in the cached', 'no such file or directory') ) and ('snapshot' in text or 'huggingface' in text):
        return StudioError('model_missing', '本地模型尚未安装或缓存不完整，请先完成语音模型安装后重试。')
    if any(value in text for value in ('modulenotfounderror', 'no module named', 'library not loaded')):
        return StudioError('dependency_missing', '运行依赖缺失，请修复项目 Python/语音环境后重试。')
    if any(value in text for value in ('timed out', 'connection refused', 'network is unreachable', 'temporary failure in name resolution', 'failed to resolve', 'unable to download')):
        return StudioError('network_unavailable', '网络连接失败，请检查连接后重试；YouTube 来源也可改用本地文件。')
    if any(value in text for value in ('private video', 'video unavailable', 'this video is unavailable', 'video is private', 'age-restricted', 'confirm your age')):
        return StudioError('source_access_denied', '这个视频不可用、私享或有年龄限制。请改用已下载的本地文件。')
    return StudioError('process_failed', '处理程序执行失败，请检查当前阶段的依赖、音源与模型配置后重试。')


_LEAKED_DETAIL = re.compile(
    r"://|\?[^\s=]+\s*=|\b(?:bearer|basic)\s+\S+|"
    r"\b(?:[\w-]*token|authorization|cookie|[\w-]*api[_-]?key|headers?)['\"]?\s*[:=]",
    re.IGNORECASE,
)
_FAILURE_MESSAGE = '处理失败，请检查当前阶段、依赖和模型配置后重试'
_TRANSLATION_CONFIG_MESSAGES = {
    '缺少 DEEPSEEK_API_KEY,请先配置 DeepSeek API Key',
    '缺少 DASHSCOPE_API_KEY,请先配置 通义千问 API Key',
}
_PIPELINE_MESSAGES = {
    '找不到 yt-dlp,请先安装',
    'yt-dlp 返回的视频信息不是有效 JSON',
    'yt-dlp 返回的视频信息格式无效',
    '视频信息缺少 ID 或标题',
    '这个视频没有英文字幕',
    'yt-dlp 没有生成英文 VTT 字幕',
    'DeepSeek 翻译请求失败,可直接重跑继续',
    '通义千问 翻译请求失败,可直接重跑继续',
}
_TTS_MESSAGES = {
    EDGE_ACCESS_DENIED_MESSAGE,
    '系统音色服务暂时拒绝连接。请升级 edge-tts',
    'Qwen 返回了不支持的音频地址',
    'Qwen 返回了空音频',
    'Qwen TTS 请求失败',
    'Qwen TTS 未返回音频',
    'Qwen 音频下载失败',
}


def diagnostic_segment_id(value):
    # Current producers use seg-000001; accept the older numbered spellings too.
    return value if isinstance(value, str) and re.fullmatch(r'(?:seg-?|s)\d{1,12}', value, re.ASCII) else None


def user_facing_message(error):
    """Return a message safe for progress.json, or None to keep the generic one.

    Arbitrary SDK text can contain keys and signed URLs. Known pipeline and
    TTS errors are fixed Chinese (or an env-var name, not the secret).
    """
    if isinstance(error, StudioRecoveryError):
        text = str(error)
        return text if text and not _LEAKED_DETAIL.search(text) else None
    if isinstance(error, TranslationValidationError):
        return '翻译结果无法与原文逐段对应，请重试。'
    if isinstance(error, (DiarizationSetupError, DiarizationRuntimeError)):
        return '声纹分析失败，请检查本地声纹环境和任务配置。'
    if isinstance(error, (StudioError, StudioInputError, SpeakerGateError, PodcastPipelineError, TTSBackendError)):
        text = str(error)
    else:
        return None
    if not text or _LEAKED_DETAIL.search(text):
        return None
    if isinstance(error, TranslationConfigurationError):
        if text in _TRANSLATION_CONFIG_MESSAGES:
            return text
        return '翻译模型供应商配置无效，请检查配置。'
    if isinstance(error, TTSBackendError):
        if text.startswith(('Qwen TTS 请求失败(', 'Qwen TTS 未返回音频(')):
            return text.split('(', 1)[0]
        return text if text in _TTS_MESSAGES else None
    if isinstance(error, PodcastPipelineError):
        if text.startswith(('读取 YouTube 信息失败:', '下载英文字幕失败:')):
            return text.split(':', 1)[0]
        return text if text in _PIPELINE_MESSAGES else None
    return text[:500]


def failure_details(error):
    """Describe errors without serializing SDK messages, request data or causes."""
    if isinstance(error, StudioRecoveryError) and error.code == 'translation_transport_unknown':
        transport = (error.evidence or {}).get('transport_failure')
        if transport:
            return transport['exception_type'], f"翻译传输失败（{transport['phase']}，{transport['reason']}）。"
    if isinstance(error, aiohttp.ClientResponseError):
        category = 'WSServerHandshakeError' if isinstance(error, aiohttp.WSServerHandshakeError) else 'ClientResponseError'
        status = error.status
        message = f'音色服务返回 HTTP {status}。' if type(status) is int and 100 <= status <= 599 else '音色服务返回异常响应。'
        return category, message
    for kind, message in (
        (aiohttp.ClientSSLError, '音色服务 TLS 连接失败。'),
        (aiohttp.ServerFingerprintMismatch, '音色服务 TLS 证书校验失败。'),
        (aiohttp.ClientConnectorError, '无法连接音色服务。'),
        (TimeoutError, '处理请求超时。'),
        (asyncio.TimeoutError, '处理请求超时。'),
        (aiohttp.ServerDisconnectedError, '音色服务连接中断。'),
        (aiohttp.ClientPayloadError, '音色服务响应未完整接收。'),
        (WebSocketError, '音色服务 WebSocket 连接中断。'),
        (NoAudioReceived, '音色服务未返回音频。'),
        (ConnectionError, '处理连接中断。'),
        (aiohttp.ClientConnectionError, '音色服务连接中断。'),
    ):
        if isinstance(error, kind):
            return kind.__name__, message
    for kind in (StudioError, StudioInputError, SpeakerGateError, TTSBackendError,
                 DiarizationSetupError, DiarizationRuntimeError,
                 TranslationConfigurationError, TranslationValidationError, TranslationRequestError,
                 StudioRecoveryError, PodcastPipelineError):
        if isinstance(error, kind):
            return kind.__name__, user_facing_message(error) or _FAILURE_MESSAGE
    if isinstance(error, OSError):
        if error.errno == errno.ENOSPC or error.args == ('disk full',):
            return 'OSError', '磁盘空间不足（disk full）。'
        return 'OSError', '文件或系统操作失败。'
    if isinstance(error, ValueError):
        return 'ValueError', '任务输入无效。'
    return 'UnexpectedError', _FAILURE_MESSAGE


def raw_reference_pieces(vtt):
    """Parse each full cue independently so rolling-prefix dedup cannot alter ref_text."""
    pieces = []
    for block in re.split(r'\n\s*\n', vtt.replace('\r\n', '\n')):
        try:
            records = parse_vtt_pieces(block)
        except Exception:
            continue
        for piece in records:
            pieces.append(CaptionPiece(piece.start_ms, piece.end_ms, piece.text, len(pieces)))
    return pieces


class Cancelled(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cached_edge_input(clip, sidecar, spoken, voice_slot):
    try:
        saved = decode_json_object(_read_regular_bytes(sidecar))
        evidence = saved['edge_input']
        if (saved.get('cache_key') == clip.stem and valid_edge_input(evidence, spoken, voice_slot)
                and saved.get('clip_sha256') == sha256_file(clip)):
            return evidence
    except (OSError, ValueError, KeyError, RecursionError):
        pass
    return None


def write_pcm_silence(path, frames=SILENCE_FRAMES, rate=SILENCE_RATE):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0" * frames)


def asr_pieces(records, offset_ms=0):
    pieces = []
    for record in records:
        text = str(record.get('text', '')).strip()
        start = offset_ms + round(float(record['start']) * 1000)
        end = offset_ms + round(float(record['end']) * 1000)
        if text and end > start:
            pieces.append(CaptionPiece(start, end, text, len(pieces)))
    return pieces


def select_references(pieces, assignments, turns):
    """Only use complete, unambiguous cues wholly inside a single clean turn."""
    selected = {}
    candidates = []
    paired = list(zip(pieces, assignments))
    for index, (piece, assignment) in enumerate(paired):
        group = [piece]
        if not assignment.ambiguous and assignment.speaker_id:
            for following, next_assignment in paired[index + 1:]:
                if next_assignment.ambiguous or next_assignment.speaker_id != assignment.speaker_id or not 0 <= following.start_ms - group[-1].end_ms <= 500 or following.end_ms - piece.start_ms > 15000:
                    break
                group.append(following)
            if len(group) > 1:
                piece = CaptionPiece(piece.start_ms, group[-1].end_ms, ' '.join(p.text for p in group), piece.source_index)
        candidates.append((piece, assignment))
    for piece, assignment in candidates:
        if assignment.ambiguous or assignment.speaker_id is None:
            continue
        speaker = assignment.speaker_id
        if not 2500 <= piece.end_ms - piece.start_ms <= 15000:
            continue
        if not any(t.speaker_id == speaker and t.start_ms <= piece.start_ms and t.end_ms >= piece.end_ms for t in turns):
            continue
        if any(t.speaker_id != speaker and t.start_ms < piece.end_ms and t.end_ms > piece.start_ms for t in turns):
            continue
        candidate = {'start_ms': piece.start_ms, 'end_ms': piece.end_ms, 'text': piece.text}
        if speaker not in selected or len(piece.text) > len(selected[speaker]['text']):
            selected[speaker] = candidate
    return selected


class StudioEngine:
    def __init__(self, job_dir):
        self.job = Path(job_dir).resolve()
        self.job.mkdir(parents=True, exist_ok=True)
        self.cancelled = False
        self.process = None
        self.stage = 'queued'
        self.completed = self.total = 0
        self.segment_id = None
        self._stage_started_at = time.time()
        self.subtitle_fallback_reason = None
        self.reference_pieces = None
        self.asr_chunks = []
        self.temp = self.job / 'temporary'

    def check_cancel(self):
        if self.cancelled or (self.job / 'cancel').exists():
            raise Cancelled('任务已取消，可继续使用已完成的缓存')

    def progress(self, stage, completed=0, total=0, *, segment_id=None, **extra):
        now = time.time()
        if stage in ('failed', 'cancelled'):
            completed, total = self.completed, self.total
        else:
            if stage != self.stage:
                self._stage_started_at = now
            self.stage = stage
            self.completed, self.total = completed, total
            self.segment_id = diagnostic_segment_id(segment_id)
        reason = extra.pop('subtitle_fallback_reason', None)
        if reason:
            self.subtitle_fallback_reason = reason
        payload = build_progress_payload(
            stage,
            completed=completed,
            total=total,
            stage_started_at=getattr(self, '_stage_started_at', now),
            updated_at=now,
            subtitle_fallback_reason=self.subtitle_fallback_reason,
            segment_id=self.segment_id,
            **extra,
        )
        atomic_json(self.job / 'progress.json', payload)

    def record_failure(self, error):
        category, message = failure_details(error)
        active = path = self.job / 'debug-failure.json'
        diagnosis = {
            'stage': self.stage,
            'segment_id': self.segment_id,
            'completed': self.completed,
            'total': self.total,
            'exc_type': category,
            'exc_message': message,
            'updated_at': time.time(),
        }
        if isinstance(error, (StudioError, SpeakerGateError)):
            diagnosis['error_code'] = error.code
        if isinstance(error, StudioRecoveryError):
            if error.code == 'translation_transport_unknown':
                diagnosis['error_code'] = error.code
            if error.code == 'translation_request_already_recorded' and (error.evidence or {}).get('receipt'):
                path = self.job / 'translation-retry-blocked.json'
                diagnosis['error_code'] = error.code
            if error.code == 'translation_legacy_unverified' and (path.exists() or path.is_symlink()):
                return message
            if error.evidence is not None:
                diagnosis['translation_request'] = error.evidence
        if path == active:
            try:
                self._archive_debug_failure(active)
            except OSError as archive_error:
                self.record_cleanup_failure('debug', archive_error, stage=self.stage)
                return message
        try:
            atomic_json(path, diagnosis)
        except OSError:
            pass
        return message

    def _archive_debug_failure(self, active):
        try:
            raw = _read_regular_bytes(active)
        except FileNotFoundError:
            return
        archive = self.job / 'debug-failures'
        archive.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(archive.lstat().st_mode):
            raise OSError(errno.EINVAL, 'Invalid debug failure archive')
        _sync_dir(self.job)
        snapshot = archive / (hashlib.sha256(raw).hexdigest() + '.json')
        try:
            _save_bytes(snapshot, raw, exclusive=True)
        except FileExistsError:
            if _read_regular_bytes(snapshot) != raw:
                raise OSError(errno.EIO, 'Conflicting debug failure archive') from None
            _sync_dir(archive)

    def record_cleanup_failure(self, target, error, *, stage):
        event = {'stage': stage, 'target': target, 'errno': error.errno, 'updated_at': time.time()}
        try:
            atomic_json(self.job / f'cleanup-{target}-failure.json', event)
        except OSError as write_error:
            logging.getLogger(__name__).warning(
                '任务清理失败且无法写入清理诊断（%s，清理 errno=%s，写入 errno=%s）',
                target, error.errno, write_error.errno,
            )

    def command(self, args, **kwargs):
        self.check_cancel()
        if args and args[0] == 'yt-dlp':
            args = [sys.executable, '-m', 'yt_dlp', *args[1:]]
        # File-backed output avoids pipe deadlocks and unbounded child logs.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                self.process = subprocess.Popen(args, stdout=stdout, stderr=stderr, start_new_session=True, **kwargs)
            except FileNotFoundError:
                raise StudioError('dependency_missing', '找不到运行程序，请检查 ffmpeg、yt-dlp 和项目 Python 环境。') from None
            try:
                while self.process.poll() is None:
                    self.check_cancel()
                    time.sleep(.15)
                stdout.seek(0)
                stderr.seek(0)
                if self.process.returncode:
                    stderr.seek(0, 2)
                    size = stderr.tell()
                    stderr.seek(max(0, size - 16000))
                    raise classify_failure(stderr.read().decode(errors='replace'), str(args[0]))
                return subprocess.CompletedProcess(args, 0, stdout.read().decode(), '')
            finally:
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait()
                self.process = None

    def runner(self, args, **kwargs):
        # Compatible legacy subprocess.run adapter; errors are intentionally redacted.
        if len(args) > 1 and Path(args[1]).name == 'sherpa_diarization_worker.py':
            args = [*args, '--cluster-threshold', str(getattr(self, 'diarization_threshold', 0.5))]
        return self.command(args)

    def ffmpeg(self, *args):
        self.command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *map(str, args)])

    def worker(self, payload):
        request = self.temp / 'worker.json'
        python = ROOT / '.speech-venv/bin/python'
        if payload['operation'] == 'asr':
            diagnostic = self.temp / 'asr-diagnostic.json'
            payload = {**payload, 'diagnostic': str(diagnostic)}
            atomic_json(diagnostic, {'backend': None, 'attempts': []})
            # A dependency interpreter may live elsewhere. The script stays here.
            if not python.is_file():
                python = Path(sys.executable)
        atomic_json(request, payload)
        try:
            self.command([str(python), '-E', str(ROOT / 'scripts/studio_worker.py'), str(request)])
        except StudioError:
            if payload['operation'] != 'asr':
                raise
            raise StudioError('asr_failed', '本地语音识别失败，请核对 debug-asr.json 中的 MLX 与 CPU 诊断。') from None
        finally:
            if payload['operation'] == 'asr':
                result = json.loads(diagnostic.read_text())
                self.asr_chunks.append({'start_ms': payload['start_ms'], **result})
                used = {row['backend'] for row in self.asr_chunks if row['backend'] is not None}
                atomic_json(self.job / 'debug-asr.json', {
                    'backend': next(iter(used), None) if len(used) < 2 else 'mixed',
                    'chunks': self.asr_chunks,
                })

    def acquire(self, config):
        source = config['source']
        kind = source['kind']
        if kind == 'web' and isinstance(source.get('value'), str) and _is_exact_youtube_url(source['value']):
            kind = 'youtube'
        if kind == 'youtube':
            if not isinstance(source.get('value'), str) or not _is_exact_youtube_url(source['value']):
                raise StudioInputError('请输入完整的 YouTube 视频链接')
            media = download_audio_only(source['value'], self.temp, runner=self.runner)
        elif kind == 'web':
            from studio_urls import is_public_https
            if not isinstance(source.get('value'), str) or not is_public_https(source['value']):
                raise StudioInputError('请输入公开的 HTTPS 链接')
            media = download_audio_only(source['value'], self.temp, runner=self.runner)
        elif kind == 'local':
            media = Path(source['value']).expanduser().resolve(strict=True)
            if not media.is_file():
                raise StudioInputError('请选择本地音频或视频文件')
        else:
            raise StudioInputError('不支持的来源类型')
        wav = self.temp / 'source.wav'
        self.ffmpeg('-i', media, '-vn', '-ac', 1, '-ar', 16000, '-c:a', 'pcm_s16le', wav)
        return wav

    def duration_ms(self, wav):
        with wave.open(str(wav)) as audio:
            return round(audio.getnframes() / audio.getframerate() * 1000)

    def locked_source(self, selector, wav=None):
        path = self.job / 'source-identity.json'
        prior = None
        if path.exists() or path.is_symlink():
            try:
                if path.is_symlink():
                    raise ValueError
                prior = decode_json_object(path.read_bytes())
            except (ValueError, OSError, RecursionError):
                raise StudioInputError('已有来源身份无法核验，请保留原任务并核对音源') from None
        media_fields = {'sha256', 'bytes', 'duration_ms'}
        if prior is not None and (
            not isinstance(prior, dict)
            or {k: v for k, v in prior.items() if k not in media_fields} != selector
        ):
            raise StudioInputError('来源已经改变，请为新来源创建新任务')
        if wav is None:
            return prior
        identity = {**selector, 'sha256': sha256_file(wav), 'bytes': wav.stat().st_size,
                    'duration_ms': self.duration_ms(wav)}
        if prior is not None and prior != identity:
            raise StudioInputError('来源已经改变，或旧任务缺少音频锁，请创建新任务')
        if prior is None:
            atomic_json(path, identity)
        return identity

    def validate_coverage(self, document, identity):
        try:
            overflow = check_source_coverage(document, identity)
            checkpoint = self.job / 'translation-source.json'
            if checkpoint.exists() or checkpoint.is_symlink():
                if checkpoint.is_symlink():
                    raise ValueError('原始分段无法核验')
                saved = decode_json_object(checkpoint.read_bytes())
                draft = saved.get('draft')
                if (not isinstance(draft, dict) or saved.get('digest') != fingerprint(draft)
                        or draft.get('media_sha256') != identity['sha256']):
                    raise ValueError('原始分段无法核验')
                original_overflow = check_source_coverage(draft, identity)
                overflow = list(dict.fromkeys([*overflow, *original_overflow]))
                if max(row['end_ms'] for row in document['segments']) < max(
                    row['end_ms'] for row in draft['segments']
                ) and not overflow:
                    raise ValueError('译稿末段早于原始分段；请保留完整时轴')
        except ValueError as error:
            raise StudioError('source_coverage_unverified', str(error)) from None
        if overflow:
            atomic_json(self.job / 'debug-source-coverage.json', {
                'duration_ms': identity['duration_ms'], 'source_sha256': identity['sha256'],
                'last_end_ms': document['segments'][-1]['end_ms'], 'overflow_segment_ids': overflow,
            })
            raise StudioError('source_coverage_failed', '译稿有段落超出锁定音源时长；请核对来源与时轴，禁止删尾过闸。')

    def transcribe(self, wav, config):
        kind = config['source']['kind']
        if kind == 'web' and isinstance(config['source'].get('value'), str) and _is_exact_youtube_url(config['source']['value']):
            kind = 'youtube'
        if kind in ('youtube', 'web'):
            reason = None
            try:
                info = fetch_video_info(config['source']['value'], runner=self.runner)
                self.remember_video_info(info, config.get('provider', 'deepseek'))
                vtt = download_english_subtitles(config['source']['value'], self.temp, info, runner=self.runner)
                vtt_text = vtt.read_text()
                pieces = parse_vtt_pieces(vtt_text)
                self.reference_pieces = raw_reference_pieces(vtt_text)
                if pieces:
                    return pieces, 'subtitles'
                reason = '英文字幕文件为空或无法解析'
            except (Cancelled, asyncio.CancelledError):
                raise
            except Exception as error:
                reason = failure_details(error)[1]
                if reason == _FAILURE_MESSAGE:
                    reason = '英文字幕读取或解析失败'
            try:
                atomic_json(self.job / 'debug-subtitle-fallback.json', {
                    'reason': reason or 'unknown',
                    'updated_at': time.time(),
                })
            except OSError:
                pass
            self.progress(
                'asr',
                message=f'英文字幕不可用（{(reason or "")[:80]}），改用本地识别',
                subtitle_fallback_reason=(reason or 'unknown')[:180],
            )
        with wave.open(str(wav)) as audio:
            seconds = audio.getnframes() / audio.getframerate()
        pieces = []
        self.asr_chunks = []
        # Small fixed chunks keep ASR memory bounded. Offsets preserve source time.
        asr_total = max(1, int(seconds / 60) + 1)
        for index, start in enumerate(range(0, int(seconds) + 1, 60)):
            if start >= seconds:
                break
            self.progress('asr', index, asr_total)
            chunk = self.temp / 'asr.wav'
            output = self.temp / 'asr.json'
            output.unlink(missing_ok=True)
            self.ffmpeg('-ss', start, '-i', wav, '-t', 60, chunk)
            self.worker({'operation': 'asr', 'audio': str(chunk), 'output': str(output),
                         'start_ms': start * 1000, 'model': config.get('asr_model', ASR_MODEL)})
            current = asr_pieces(json.loads(output.read_text())['segments'], start * 1000)
            pieces.extend(current)
        return [CaptionPiece(p.start_ms, p.end_ms, p.text, i) for i, p in enumerate(pieces)], 'asr'

    def credential(self, provider):
        config = get_provider_config(provider)
        key = os.getenv(config.api_key_env)
        if not key:
            result = subprocess.run(['security', 'find-generic-password', '-a', os.getenv('USER', ''), '-s', config.keychain_service, '-w'], capture_output=True, text=True)
            if result.returncode == 0:
                key = result.stdout.strip()
        return key

    def diarize_with_progress(self, wav, bounds):
        """Run sherpa chunks with stage progress; keep threshold + cancel wiring."""
        runtime_script = Path(diarization_runtime.__file__).resolve()
        if runtime_script != ROOT / 'scripts/diarization_runtime.py':
            raise StudioError('runtime_mismatch', '声纹模块来自另一工作树，已停止执行。')
        atomic_json(self.job / 'debug-runtime.json', {
            'engine': str(Path(__file__).resolve()),
            'diarization_runtime': str(runtime_script),
            'diarization_runtime_sha256': sha256_file(runtime_script),
        })
        fold_report = {'expected_speakers': bounds.exact, 'folds': []}
        fold_path = self.job / 'debug-speaker-folds.json'
        atomic_json(fold_path, fold_report)
        runtime = ensure_diarization_runtime(runner=self.runner)

        def on_fold(event):
            fold_report['folds'].append(event)
            atomic_json(fold_path, fold_report)

        def popen_factory(command, **kwargs):
            args = list(command)
            if len(args) > 1 and Path(args[1]).name == 'sherpa_diarization_worker.py':
                args = [*args, '--cluster-threshold', str(getattr(self, 'diarization_threshold', 0.5))]
            self.check_cancel()
            process = subprocess.Popen(args, start_new_session=True, **kwargs)
            self.process = process
            return process

        def on_progress(percent):
            self.check_cancel()
            self.progress('diarization', int(percent), 100)

        try:
            return run_sherpa_backend(
                wav,
                bounds,
                runtime=runtime,
                runner=self.runner,
                popen_factory=popen_factory,
                progress_callback=on_progress,
                fold_callback=on_fold,
            )
        finally:
            self.process = None

    def owned_text(self, value):
        """A user-supplied text file inside this job. Never a remote book URL."""
        if not isinstance(value, str) or not value.strip():
            raise StudioInputError('书听只读你放进任务的文本，不会下载或抓取书源')
        if value.strip().lower().startswith(('http://', 'https://')):
            raise StudioInputError('书听不会从链接抓书')
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.job / path
        if path.is_symlink() or not path.is_file():
            raise StudioInputError('书稿必须在任务目录内')
        path = path.resolve()
        if not path.is_relative_to(self.job.resolve()):
            raise StudioInputError('书稿必须在任务目录内')
        if path.suffix.lower() not in ('.txt', '.md'):
            raise StudioInputError('书听只接受 txt 或 md')
        return path

    def _book_cursor(self):
        try:
            stored = json.loads((self.job / 'book.json').read_text())
        except (OSError, ValueError):
            return {}
        cursor = stored.get('progress') if isinstance(stored, dict) else None
        if not isinstance(cursor, dict):
            return {}
        return {key: cursor[key] for key in ('chapter_id', 'block_id', 'offset_chars') if key in cursor}

    def finish_book(self, config, action):
        """Write chapters/blocks and skip diarization. Do not render one whole-book file."""
        from studio_books import build_book, narrator_document, read_user_text, resume_fields

        source = config.get('source') or {}
        if source.get('kind') != 'text':
            raise StudioInputError('书听只读你放进任务的文本，不会下载或抓取书源')
        path = self.owned_text(source.get('value'))
        expected_speakers(config)
        if config.get('diarization') not in (None, 'skipped', 'off'):
            raise StudioInputError('书听不能开启声纹')
        book_path = self.job / 'book.json'
        document_path = self.job / 'document.json'
        if action == 'prepare' or not book_path.is_file():
            book = build_book(
                read_user_text(path),
                title=path.stem,
                language=config.get('book_language') or 'zh',
            )
            document = narrator_document(book)
            validate_speaker_voices(config, document)
            atomic_json(book_path, book)
            atomic_json(document_path, document)
        else:
            book = json.loads(book_path.read_text())
            document = json.loads(document_path.read_text()) if document_path.is_file() else narrator_document(book)
            validate_speaker_voices(config, document)
        self.stage = 'book'
        if action == 'prepare':
            self.progress(
                'complete',
                **resume_fields(book),
                message='书已按章和块切开。声纹已跳过。按章合成与手机目录留到下一版。',
            )
            return None
        raise StudioInputError('书听不生成整本一条 MP3。章节已写入 book.json，按章合成留到下一版。')

    @staticmethod
    def _translation_plan(draft):
        spoken = [
            SubtitleSegment(
                id=record['id'], start_ms=record['start_ms'], end_ms=record['end_ms'], text=record['text'],
                speaker_id=record.get('speaker_id'), ambiguous=bool(record.get('ambiguous')),
            )
            for record in draft['segments'] if not record.get('is_ad')
        ]
        if not spoken:
            raise StudioInputError('整段都被识别成口播广告，没有可朗读的对白')
        plan = [[segment.id for segment in chunk] for chunk in chunk_segments(spoken)]
        return spoken, plan

    def _pending_episode_title(self):
        from studio_publish_icloud import contains_cjk, human_title, topic_without_show

        try:
            try:
                metadata = decode_json_object(_read_regular_bytes(self.job / 'metadata.json'))
            except FileNotFoundError:
                metadata = {}
            if contains_cjk(human_title(metadata.get('display_title')) or ''):
                return None
            source = human_title(metadata.get('source_title'))
            if not source:
                state = decode_json_object(_read_regular_bytes(self.job / 'state.json'))
                source = human_title(state.get('title'))
            if not source:
                raise ValueError('Missing title')
            topic = topic_without_show(source, metadata.get('channel') or metadata.get('uploader'))
            return None if contains_cjk(topic) else topic
        except (OSError, TypeError, ValueError, RecursionError):
            raise TranslationRequestError('translation_plan_unverified', {}) from None

    def prepare(self, config, wav=None, *, source_identity=None):
        if config.get('content_type') == 'book':
            raise StudioInputError('书听禁止声纹分析')
        # Default cluster threshold strategy (do not raise to "fix" oversplit):
        # - local files: 0.5 (sherpa default; preserves real guest splits)
        # - YouTube interviews: 0.9 (0.5 over-splits long talk shows into ghost IDs)
        # Soft closed-world stitch + <~20s ghost collapse handle residual oversplit
        # without needing a higher global threshold. Explicit config always wins.
        source = config.get('source') or {}
        remote = source.get('kind')
        if remote == 'web' and isinstance(source.get('value'), str) and _is_exact_youtube_url(source['value']):
            remote = 'youtube'
        default_threshold = 0.9 if remote == 'youtube' else 0.5
        self.diarization_threshold = float(config.get('diarization_threshold', default_threshold))
        if not 0 < self.diarization_threshold < 2:
            raise StudioInputError('声纹聚类阈值须介于 0 和 2 之间')
        checkpoint = self.job / 'translation-source.json'
        chunks_dir = self.job / 'translations'
        journal = TranslationJournal(self.job)
        if (self.job / 'document.json').exists():
            raise StudioRecoveryError(
                'translation_document_exists', '已保留现有译稿与复核记录，重新识别请使用新任务',
            )
        resuming = checkpoint.exists() or checkpoint.is_symlink()
        if not resuming:
            if self._has_unbound_translation(chunks_dir, journal):
                raise StudioRecoveryError(
                    'translation_legacy_unverified',
                    '旧任务缺少可核验的原分段，现有缓存已保留，需先核对旧请求与来源',
                )
            wav = self.acquire(config) if wav is None else wav
            if source_identity is not None:
                source_identity = self.locked_source(source_identity, wav)
            draft = self._prepare_source(config, wav)
        elif source_identity is not None:
            # Remote URLs can resolve to new bytes. Verify before reusing a checkpoint.
            if source_identity.get('kind') in ('youtube', 'web'):
                wav = self.acquire(config) if wav is None else wav
            source_identity = self.locked_source(source_identity, wav)
            if not source_identity or not source_identity.get('duration_ms'):
                raise StudioRecoveryError('translation_source_mismatch', '旧任务缺少可核验的音源时长锁，原稿与缓存已保留')
        provider = config.get('provider', 'deepseek')
        budget = TranslationBudget(self.job)
        client = create_translation_client(
            provider, budget=budget,
            credential_loader=lambda: self.credential(provider), request_journal=journal,
        )
        binding_fields = {
            'version': 1,
            'source': source_identity if source_identity is not None else config.get('source'),
            'expected_speakers': expected_speakers(config),
            'diarization_threshold': self.diarization_threshold,
            'asr_model': config.get('asr_model', ASR_MODEL),
            'male_only': use_male_slots(config),
            'requires_voice_mapping': requires_voice_mapping(config),
            'translation_identity': client.translation_identity,
        }
        binding = fingerprint(binding_fields)
        if resuming:
            self.progress('translation')

            def rebind(envelope):
                # Older envelopes stored only the full fingerprint. Reconstruct the
                # previous default from the provider config and verify its preimage.
                config_provider = get_provider_config(provider)
                old = envelope.get('translation_identity',
                                   f'{provider}|{config_provider.default_model}|{config_provider.default_base_url}')
                draft = envelope['draft']
                if (not supports_policy(provider, client.model, client.base_url)
                        or not isinstance(old, str) or old == client.translation_identity
                        or fingerprint({**binding_fields, 'translation_identity': old}) != envelope['binding']
                        or check_source_coverage(draft, source_identity)
                        or draft.get('media_sha256') != source_identity['sha256']
                        or self._translation_plan(draft)[1] != draft.get('translation_plan')):
                    raise ValueError('Unproven checkpoint migration')
                speakers = validate_speaker_ids(config, (row.get('speaker_id') for row in draft['segments']))
                if set(draft['references']) != speakers or set(draft['speakers']) != speakers:
                    raise ValueError('Reference or speaker identity mismatch')
                with budget.unused_source():
                    save_source(checkpoint, binding, draft, translation_identity=client.translation_identity)

            draft = load_source(checkpoint, binding, lambda name: sha256_file(self.owned(name)), rebind=rebind)

        spoken, plan = self._translation_plan(draft)
        if resuming:
            if plan != draft.get('translation_plan'):
                raise StudioRecoveryError('translation_source_mismatch', '续做分块边界已变化，原稿与缓存已保留')
        else:
            draft['translation_plan'] = plan
            save_source(checkpoint, binding, draft, translation_identity=client.translation_identity)

        self.progress('translation', 0, len(spoken))

        def translate(batch):
            self.check_cancel()
            try:
                return client.translate(batch)
            except TranslationValidationError:
                raise StudioRecoveryError(
                    'translation_response_invalid', '翻译响应结构无法核验，需先核对原请求',
                    journal.last_evidence,
                ) from None

        translated = translate_with_resume(
            spoken, chunks_dir, translate, translation_identity=client.translation_identity,
            preserve_invalid=True,
            preflight=(lambda chunks: client.preflight_episode(chunks, self._pending_episode_title()))
            if budget.episode_cap is not None else None,
            on_chunk=lambda completed: self.progress('translation', completed, len(spoken)),
        )
        document = {key: draft[key] for key in ('schema_version', 'transcript_source', 'speakers', 'references')}
        document['segments'] = [
            {**record, 'chinese': '' if record.get('is_ad') else translated[record['id']]}
            for record in draft['segments']
        ]
        atomic_json(self.job / 'document.json', document)
        return document

    def _has_unbound_translation(self, chunks_dir, journal):
        if chunks_dir.exists() or chunks_dir.is_symlink() or journal.path.exists() or journal.path.is_symlink():
            return True
        # An old request may be evidenced even if its cache directory is gone.
        prior = self.job / 'debug-failure.json'
        if prior.is_symlink():
            return True
        try:
            status = json.loads(prior.read_bytes())
        except FileNotFoundError:
            return False
        except (OSError, ValueError, RecursionError):
            return True
        return not isinstance(status, dict) or status.get('stage') == 'translation'

    def _prepare_source(self, config, wav):
        self.progress('transcript')
        pieces, transcript_source = self.transcribe(wav, config)
        if not pieces:
            raise StudioInputError('没有识别出英文对白，请检查文件或字幕')
        self.progress('diarization', 0, 100)
        bounds = SpeakerBounds(exact=expected_speakers(config))
        turns = self.diarize_with_progress(wav, bounds)
        assignments = align_caption_pieces(pieces, turns)
        # One distant cue (outro, music) used to abort after diarization.
        # Keep the 20% unassigned gate above; attach the rest for review.
        assignments = fill_unassigned_for_review(pieces, turns, assignments)
        speaker_of = {item.source_index: item.speaker_id for item in assignments}
        ambiguous = {item.source_index for item in assignments if item.ambiguous}
        # Mark sponsor cues on raw lines so grouping cannot glue an ad onto the next answer.
        marked_pieces = mark_sponsor_windows([
            {
                'id': piece.source_index,
                'start_ms': piece.start_ms,
                'end_ms': piece.end_ms,
                'text': piece.text,
                'speaker_id': speaker_of[piece.source_index],
            }
            for piece in pieces
        ])
        grouped = group_caption_pieces(
            pieces,
            {row['id']: group_key(row.get('speaker_id'), row.get('is_ad')) for row in marked_pieces},
            ambiguous,
            max_chars=180,
        )
        records = []
        for segment in grouped:
            speaker, is_ad = split_group_key(segment.speaker_id)
            row = asdict(segment)
            row['speaker_id'] = speaker
            row['is_ad'] = is_ad
            records.append(row)
        ad_only = ad_only_speakers(turns, records)
        records = fold_ad_speakers(records, ad_only)
        # The hard gate counts everyone except a short voice that only sits in the sponsor window.
        validate_speaker_ids(config, (turn.speaker_id for turn in turns if turn.speaker_id not in ad_only))
        validate_speaker_ids(config, (record.get('speaker_id') for record in records))
        # Structural checks still run. Exact raw cluster count does not, because an ad read is an extra id.
        validate_diarization(turns, duration_ms=self.duration_ms(wav), bounds=SpeakerBounds(minimum=1, maximum=6), assignments=assignments)
        reference_pieces = self.reference_pieces if self.reference_pieces is not None else pieces
        reference_assignments = align_caption_pieces(reference_pieces, turns) if self.reference_pieces is not None else assignments
        refs = select_references(reference_pieces, reference_assignments, turns)
        kept = {record.get('speaker_id') for record in records}
        refs = {speaker: ref for speaker, ref in refs.items() if speaker in kept}
        references = {}
        reference_dir = self.job / 'references'
        reference_dir.mkdir(exist_ok=True)
        for speaker, ref in refs.items():
            # Speaker identifiers never become filesystem paths.
            name = fingerprint(speaker)[:16] + '.wav'
            output = reference_dir / name
            self.ffmpeg('-ss', ref['start_ms'] / 1000, '-i', wav, '-t', (ref['end_ms'] - ref['start_ms']) / 1000, output)
            references[speaker] = {**ref, 'path': str(output.relative_to(self.job)), 'sha256': sha256_file(output)}
        voice_slots = assign_studio_voices(
            [record.get('speaker_id') for record in records],
            male_only=use_male_slots(config),
            require_mapping=requires_voice_mapping(config),
        )
        return {
            'schema_version': 1, 'transcript_source': transcript_source,
            'segments': records, 'media_sha256': sha256_file(wav), 'references': references,
            'speakers': {speaker: {'voice_slot': slot} for speaker, slot in voice_slots.items()},
        }

    def owned(self, relative):
        path = (self.job / relative).resolve()
        if not path.is_relative_to(self.job):
            raise StudioInputError('音频路径超出任务目录')
        return path

    def valid_audio(self, path):
        if not path.is_file() or path.stat().st_size < 44:
            return False
        try:
            self.command(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-'])
            return True
        except Cancelled:
            raise
        except StudioError as error:
            # A missing ffmpeg is not a bad clip. Decode failures stay False so
            # a corrupt cache entry is regenerated instead of aborting the job.
            if getattr(error, 'code', None) == 'dependency_missing':
                raise
            return False
        except RuntimeError:
            return False

    async def synthesize(self, backend, request, output):
        # The server sets a cancellation flag or marker while Edge can be sleeping.
        self.check_cancel()
        task = asyncio.create_task(backend.synthesize(request, output))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=.15)
                self.check_cancel()
        except (Cancelled, asyncio.CancelledError):
            task.cancel()
            # Retrieve an exception if a cancellation marker won a completion race.
            await asyncio.gather(task, return_exceptions=True)
            raise
        return task.result()

    def render(self, config, document, preview=None, accept_unreviewed=False):
        self.progress('synthesis')
        validate_speaker_voices(config, document)
        segments = document['segments']
        if not segments or len({s['id'] for s in segments}) != len(segments):
            raise StudioInputError('段落为空或编号重复')
        # Drop sponsor windows. Do not insert silence; the surrounding lines join.
        segments = [segment for segment in segments if not segment.get('is_ad')]
        if not segments:
            raise StudioInputError('没有可朗读的对白（口播广告已跳过）')
        selected = segments if preview is None else select_preview_segments(segments, int(preview))
        if not selected:
            raise StudioInputError('试听段数必须大于零')
        self.total = len(selected)
        backend_name = config.get('tts', 'edge')
        edge = backend_name == 'edge'
        pending = [segment['id'] for segment in selected if segment.get('ambiguous')]
        if pending and not accept_unreviewed:
            raise StudioInputError(f'有 {len(pending)} 段说话人待核对。请拆段、逐段修正或凭证确认，再试听或生成。')
        model = config.get('clone_model', CLONE_MODEL)
        backend = None
        if backend_name != 'clone':
            backend = build_backend(backend_name, api_key=self.credential('qwen') if backend_name == 'qwen3-api' else None)
        cache = self.job / 'audio-cache'
        cache.mkdir(exist_ok=True)
        clips = []
        edge_inputs = []
        edge_occurrences = {}
        for index, segment in enumerate(selected):
            self.progress('synthesis', index, len(selected), segment_id=segment['id'])
            self.check_cancel()
            text = segment['chinese'].strip()
            if not text:
                raise StudioInputError('片段译文为空')
            spoken = clean_tts_text(text) if edge else text
            speaker = segment['speaker_id']
            voice = document['speakers'][speaker]
            voice_slot = voice.get('voice_slot', 'male_1')
            ref = document['references'].get(speaker)
            identity = {'text': text, 'voice': voice, 'backend': backend_name, 'style': config.get('style', 'conversation'), 'model': model if backend is None else backend.fingerprint(), 'version': 1, 'revision': '50f45ef0047cde7e84c2ef04326acb8ada2436a7' if model == CLONE_MODEL else model}
            if spoken != text:
                # Only changed lines pick up a new cache key, so unchanged Edge clips stay reusable.
                identity['spoken'] = spoken
            if edge and speakable_tts(spoken):
                key = (voice_slot, spoken)
                occurrence = edge_occurrences.get(key, 0)
                edge_occurrences[key] = occurrence + 1
                if occurrence:
                    # Keep a separate request receipt for each repetition in this render.
                    identity['edge_occurrence'] = occurrence
            if backend is None:
                if ref is None:
                    raise StudioInputError('说话人没有干净的参考声音，请换用系统音色或选择参考片段')
                reference = self.owned(ref['path'])
                identity['reference'] = {'sha256': sha256_file(reference), 'text': ref['text']}
            target = cache / (fingerprint(identity) + '.wav')
            sidecar = target.with_suffix('.edge.json')
            edge_input = None
            if not self.valid_audio(target):
                if edge:
                    sidecar.unlink(missing_ok=True)
                if edge and not speakable_tts(spoken):
                    write_pcm_silence(target)
                    if not self.valid_audio(target):
                        raise StudioInputError('片段音频无效，请重试')
                    edge_input = EDGE_SILENCE
                else:
                    raw = self.temp / ('synthesis.wav' if backend is None else 'synthesis' + backend.file_suffix)
                    if backend is None:
                        self.worker({'operation': 'tts', 'model': model, 'text': text, 'reference': str(reference), 'reference_text': ref['text'], 'output': str(raw)})
                    else:
                        request = SynthesisRequest(spoken, voice_slot, config.get('style', 'conversation'))
                        usage = asyncio.run(self.synthesize(backend, request, raw))
                        if edge:
                            edge_input = getattr(usage, 'edge_input', None)
                    staged = self.temp / 'normalized.wav'
                    self.ffmpeg('-i', raw, '-ac', 1, '-ar', 24000, '-c:a', 'pcm_s16le', staged)
                    if not self.valid_audio(staged):
                        raise StudioInputError('片段音频无效，请重试')
                    staged.replace(target)
                if edge and valid_edge_input(edge_input, spoken, voice_slot):
                    atomic_json(sidecar, {
                        'cache_key': target.stem, 'clip_sha256': sha256_file(target), 'edge_input': edge_input,
                    })
                else:
                    edge_input = None
            elif edge:
                edge_input = _cached_edge_input(target, sidecar, spoken, voice_slot)
            clips.append(target)
            if edge:
                edge_inputs.append(edge_input)
        self.progress('export', len(selected), len(selected))
        listing = self.temp / 'concat.txt'
        # Cache paths consist only of hex names; concat manifest cannot inject directives.
        listing.write_text(''.join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in clips))
        staged = self.temp / 'output.mp3'
        self.ffmpeg('-f', 'concat', '-safe', 0, '-i', listing, '-c:a', 'libmp3lame', '-b:a', '128k', staged)
        if not self.valid_audio(staged):
            raise StudioInputError('完整音频校验失败，已有导出保持不变')
        target = self.job / ('preview.mp3' if preview is not None else 'output.mp3')
        source_lock = self.job / 'source-identity.json'
        receipt_path = self.job / 'render-identity.json'
        if preview is None and source_lock.is_file():
            frames = 0
            timeline = []
            for index, (segment, clip) in enumerate(zip(selected, clips, strict=True)):
                evidence = edge_inputs[index] if edge else None
                with wave.open(str(clip)) as audio:
                    if audio.getframerate() != SILENCE_RATE:
                        raise StudioInputError('片段音频采样率与合成时轴不一致')
                    end = frames + audio.getnframes()
                    if evidence == EDGE_SILENCE and (
                            end - frames != SILENCE_FRAMES or audio.getnchannels() != 1
                            or audio.getsampwidth() != 2 or any(audio.readframes(SILENCE_FRAMES))):
                        evidence = None
                row = {'id': segment['id'], 'start_frame': frames, 'end_frame': end}
                if edge:
                    row['edge_input'] = evidence
                timeline.append(row)
                frames = end
            receipt = self.temp / 'render-identity.json'
            atomic_json(receipt, {
                'schema_version': 1, 'audio_sha256': sha256_file(staged),
                'document_sha256': fingerprint(document), 'source_identity_sha256': sha256_file(source_lock),
                'sample_rate': SILENCE_RATE, 'timeline': timeline,
                **({'edge_input_version': EDGE_INPUT_VERSION} if edge else {}),
            })
        staged.replace(target)
        if preview is None and source_lock.is_file():
            receipt.replace(receipt_path)
        return target

    def remember_video_info(self, info, provider='deepseek'):
        """Persist the source title before subtitles or ASR can fail."""
        try:
            import studio_publish_icloud
            thumb = info.get('thumbnail') if isinstance(info, dict) else None
            provider_name = provider if provider in ('deepseek', 'qwen') else 'deepseek'
            try:
                metadata = video_metadata_from_info(info)
            except PodcastPipelineError:
                metadata = None
            if metadata is None:
                title = info.get('title') if isinstance(info, dict) else None
                if not isinstance(title, str) or not title.strip():
                    return
                studio_publish_icloud.remember_source_title(
                    self.job,
                    source_title=title,
                    channel=str(info.get('channel') or info.get('uploader') or ''),
                    webpage_url=str(info.get('webpage_url') or ''),
                    thumbnail_url=thumb if isinstance(thumb, str) else None,
                    provider=provider_name,
                )
                return
            studio_publish_icloud.remember_source_title(
                self.job,
                source_title=metadata.source_title,
                video_id=metadata.video_id,
                channel=metadata.channel,
                webpage_url=metadata.webpage_url,
                thumbnail_url=thumb if isinstance(thumb, str) else None,
                provider=provider_name,
            )
        except Exception:
            return

    def publish_private_library(self, library_root=None):
        """Copy a finished full episode to iCloud. Never changes the job result."""
        try:
            import studio_publish_icloud
            try:
                studio_publish_icloud.ensure_publishable_title(self.job)
            except Cancelled:
                raise
            except Exception:
                pass
            result = studio_publish_icloud.publish_job(self.job, library_root=library_root)
        except Cancelled:
            raise
        except Exception as error:
            message = failure_details(error)[1]
            print(f'私人听：发布失败（成品已保留，任务仍成功）：{message}', flush=True)
            try:
                import studio_publish_icloud
                studio_publish_icloud.record_status(self.job, ok=False, error=message)
            except Exception:
                pass
            return None
        print('私人听：已发布到 iCloud 资料库。', flush=True)
        return result

    def push_private_device(self, published):
        """Extra copy into the app on a plugged-in iPhone. Never changes the job result."""
        if not published:
            return
        try:
            import studio_push_iphone
            studio_push_iphone.push_after_publish(published, self.job)
        except Exception:
            pass

    def run(self, config, action='run', preview=None, accept_unreviewed=False, *, publish=True, library_root=None):
        if config.get('content_type') == 'book':
            return self._run(config, action, preview, accept_unreviewed, publish, library_root)
        with podcast_job_lock(self.job):
            return self._run(config, action, preview, accept_unreviewed, publish, library_root)

    def _run(self, config, action, preview, accept_unreviewed, publish, library_root):
        terminal_stage = None
        try:
            self.progress('source')
            self.temp.mkdir(exist_ok=True)
            self.check_cancel()
            source_identity = dict(config['source'])
            if config.get('content_type') == 'book':
                if source_identity.get('kind') != 'text':
                    raise StudioInputError('书听只读你放进任务的文本，不会下载或抓取书源')
                owned = self.owned_text(source_identity.get('value'))
                source_identity['value'] = str(owned)
                source_identity['sha256'] = sha256_file(owned)
                identity_path = self.job / 'source-identity.json'
                if identity_path.exists() and json.loads(identity_path.read_text()) != source_identity:
                    raise StudioInputError('来源已经改变，请为新来源创建新任务')
                if not identity_path.exists():
                    atomic_json(identity_path, source_identity)
                output = self.finish_book(config, action)
            else:
                if source_identity.get('kind') == 'local':
                    source_identity['input_sha256'] = sha256_file(Path(source_identity['value']).expanduser())
                self.locked_source(source_identity)
                document_path = self.job / 'document.json'
                if action == 'prepare' or not document_path.exists():
                    document = self.prepare(config, source_identity=source_identity)
                else:
                    document = json.loads(document_path.read_text())
                output = None
                if action != 'prepare':
                    locked = self.locked_source(source_identity)
                    self.validate_coverage(document, locked)
                    output = self.render(config, document, preview, accept_unreviewed=accept_unreviewed)
                # Preview stays on the Mac. Only a full output.mp3 is offered to the phone.
                published = None
                if publish and preview is None and output is not None and Path(output).name == 'output.mp3':
                    if self.locked_source(source_identity) != locked:
                        raise StudioError('source_coverage_unverified', '合成期间音源身份发生变化，已停止发布。')
                    self.validate_coverage(document, locked)
                    if not any(row.get('ambiguous') for row in document['segments']):
                        self.progress('publish', self.completed, self.total)
                        published = self.publish_private_library(library_root)
                self.progress('complete', output=str(output) if output else None)
                if published:
                    self.push_private_device(published)
        except (Cancelled, asyncio.CancelledError):
            terminal_stage = 'cancelled'
            self.progress(terminal_stage, message='任务已取消；已完成片段可继续复用')
            raise
        except Exception as error:
            terminal_stage = 'failed'
            self.record_failure(error)
            visible = user_facing_message(error) or _FAILURE_MESSAGE
            if isinstance(error, (StudioError, SpeakerGateError, StudioRecoveryError)):
                code = error.code
            elif isinstance(error, (ValueError, FileNotFoundError)):
                code = 'invalid_input'
            else:
                code = self.stage + '_failed'
            if isinstance(error, StudioRecoveryError) and error.evidence:
                ids = [sid for value in error.evidence.get('segment_ids', [])
                       if (sid := diagnostic_segment_id(value))]
                if ids:
                    span = ids[0] if len(ids) == 1 else f'{ids[0]}–{ids[-1]}'
                    visible = f'{visible}（{span}）'
            self.progress(
                terminal_stage, failed_stage=self.stage, error_code=code, message=visible,
                **(self._book_cursor() if config.get('content_type') == 'book' else {}),
            )
            raise
        finally:
            try:
                if self.temp.exists():
                    shutil.rmtree(self.temp)
            except OSError as error:
                self.record_cleanup_failure('temporary', error, stage=terminal_stage or self.stage)
        try:
            active = self.job / 'debug-failure.json'
            self._archive_debug_failure(active)
            active.unlink(missing_ok=True)
        except OSError as error:
            self.record_cleanup_failure('debug', error, stage=self.stage)
        return output


def main():
    parser = argparse.ArgumentParser(description='播客工作室任务引擎')
    parser.add_argument('--job-dir', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--action', choices=['run', 'prepare', 'render'], default='run')
    parser.add_argument('--preview', type=int)
    parser.add_argument('--accept-unreviewed', action='store_true')
    parser.add_argument('--publish-library', type=Path)
    args = parser.parse_args()
    engine = StudioEngine(args.job_dir)
    def cancel(*_):
        engine.cancelled = True
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        engine.run(json.loads(Path(args.config).read_text()), args.action, args.preview,
                   accept_unreviewed=args.accept_unreviewed, publish=args.publish_library is not None,
                   library_root=args.publish_library)
        return 0
    except (Cancelled, asyncio.CancelledError):
        return 130
    except StudioRecoveryError as error:
        print(user_facing_message(error) or _FAILURE_MESSAGE, flush=True)
        return 1
    except Exception:
        print('任务失败，请查看 progress.json 中的阶段并检查配置后重试', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
