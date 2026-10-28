#!/usr/bin/env python3
"""Persistent, editable studio jobs; ML models live only in child processes."""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import wave

from podcast_cn import (fetch_video_info, download_english_subtitles, create_translation_client,
                        translate_with_resume, get_provider_config, _is_exact_youtube_url,
                        video_metadata_from_info, PodcastPipelineError)
from podcast_captions import CaptionPiece, SubtitleSegment, parse_vtt_pieces, group_caption_pieces
from diarization_core import (SpeakerBounds, align_caption_pieces, fill_unassigned_for_review,
                              validate_diarization)
from diarization_cache import assign_studio_voices
from diarization_runtime import ensure_diarization_runtime, run_sherpa_backend
from studio_preview import select_preview_segments
from studio_progress import build_progress_payload
from studio_ads import ad_only_speakers, fold_ad_speakers, group_key, mark_sponsor_windows, split_group_key
from studio_speakers import SpeakerGateError, expected_speakers, use_male_slots, validate_speaker_ids, validate_speaker_voices
from temporary_audio import download_audio_only, sha256_file
from tts_backends import TTSBackendError, build_backend, SynthesisRequest

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


_LEAKED_DETAIL = re.compile(r"https?://|bearer\s+\S+|token=", re.IGNORECASE)


def user_facing_message(error):
    """Return a message safe for progress.json, or None to keep the generic one.

    Arbitrary SDK text can contain keys and signed URLs. Known pipeline and
    TTS errors are fixed Chinese (or an env-var name, not the secret).
    """
    if isinstance(error, (StudioError, StudioInputError, SpeakerGateError, PodcastPipelineError, TTSBackendError)):
        text = str(error)
    elif type(error).__name__.startswith('Diarization'):
        text = str(error)
    else:
        return None
    if not text or _LEAKED_DETAIL.search(text):
        return None
    return text[:500]


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


# 0.12 s at 24 kHz. Edge rejects bare punctuation with NoAudioReceived and
# aborts the whole job; a short silence keeps the timeline without a request.
SILENCE_FRAMES = 2880
SILENCE_RATE = 24000
_SPEAKABLE_TTS = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_TTS_TURN_MARKER = re.compile(r"(?<!\S)>>+\s*")
_TTS_STAGE = re.compile(
    r"[\[【〔(（]"
    r"(?:音乐|背景音乐|笑声?|掌声|叹气|清嗓子?|咳嗽|鼓掌|沉默|停顿|吸气|呼气|听不清|"
    r"music|laughter|laughs?|applause|gasps?|sighs?|coughs?|snorts?|clears throat|inaudible|crosstalk|beat)"
    r"[\]】〕)）]",
    re.IGNORECASE,
)


def clean_tts_text(text):
    """Drop YouTube ``>>`` turn markers and short stage directions before Edge."""
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _TTS_TURN_MARKER.sub("", cleaned)
    cleaned = _TTS_STAGE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def speakable_tts(text):
    return _SPEAKABLE_TTS.search(text or "") is not None


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
        self._stage_started_at = time.time()
        self.subtitle_fallback_reason = None
        self.reference_pieces = None
        self.temp = self.job / 'temporary'

    def check_cancel(self):
        if self.cancelled or (self.job / 'cancel').exists():
            raise Cancelled('任务已取消，可继续使用已完成的缓存')

    def progress(self, stage, completed=0, total=0, **extra):
        now = time.time()
        if stage not in ('failed', 'cancelled'):
            if stage != self.stage:
                self._stage_started_at = now
            self.stage = stage
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
            **extra,
        )
        atomic_json(self.job / 'progress.json', payload)

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
        atomic_json(request, payload)
        self.command([str(ROOT / '.speech-venv/bin/python'), str(ROOT / 'scripts/studio_worker.py'), str(request)])

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
            except Cancelled:
                raise
            except Exception as error:
                reason = f'{type(error).__name__}: {str(error)[:180]}'
            try:
                atomic_json(self.job / 'debug-subtitle-fallback.json', {
                    'reason': reason or 'unknown',
                    'source': config['source'].get('value'),
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
        # Small fixed chunks keep ASR memory bounded. Offsets preserve source time.
        asr_total = max(1, int(seconds / 60) + 1)
        for index, start in enumerate(range(0, int(seconds) + 1, 60)):
            if start >= seconds:
                break
            self.progress('asr', index, asr_total)
            chunk = self.temp / 'asr.wav'
            output = self.temp / 'asr.json'
            self.ffmpeg('-ss', start, '-i', wav, '-t', 60, chunk)
            self.worker({'operation': 'asr', 'audio': str(chunk), 'output': str(output), 'model': config.get('asr_model', ASR_MODEL)})
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
        runtime = ensure_diarization_runtime(runner=self.runner)

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

    def prepare(self, config, wav):
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
        spoken = [
            SubtitleSegment(
                id=record['id'], start_ms=record['start_ms'], end_ms=record['end_ms'], text=record['text'],
                speaker_id=record.get('speaker_id'), ambiguous=bool(record.get('ambiguous')),
            )
            for record in records if not record.get('is_ad')
        ]
        if not spoken:
            raise StudioInputError('整段都被识别成口播广告，没有可朗读的对白')
        self.progress('translation', 0, len(spoken))
        provider = config.get('provider', 'deepseek')
        client = create_translation_client(provider, api_key=self.credential(provider))
        translated_count = 0
        def translate(batch):
            nonlocal translated_count
            self.check_cancel()
            result = client.translate(batch)
            translated_count += len(batch)
            self.progress('translation', translated_count, len(spoken))
            return result
        translated = translate_with_resume(spoken, self.job / 'translations', translate, translation_identity=client.translation_identity)
        if translated_count < len(spoken):
            # Cache hits never call translate(); snap progress to done for ETA UI.
            self.progress('translation', len(spoken), len(spoken))
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
        )
        document = {'schema_version': 1, 'transcript_source': transcript_source,
                    'segments': [{
                        **record,
                        'chinese': '' if record.get('is_ad') else translated[record['id']],
                        'speaker_id': record.get('speaker_id'),
                    } for record in records],
                    'speakers': {speaker: {'voice_slot': slot} for speaker, slot in voice_slots.items()}, 'references': references}
        atomic_json(self.job / 'document.json', document)
        return document

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

    def render(self, config, document, preview=None, accept_unreviewed=False):
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
        backend_name = config.get('tts', 'edge')
        pending = [segment['id'] for segment in selected if segment.get('ambiguous')]
        if pending and not accept_unreviewed:
            raise StudioInputError(f'有 {len(pending)} 段说话人待核对。可全部按当前说话人确认，或带风险继续后再试听或生成')
        model = config.get('clone_model', CLONE_MODEL)
        backend = None
        if backend_name != 'clone':
            backend = build_backend(backend_name, api_key=self.credential('qwen') if backend_name == 'qwen3-api' else None)
        cache = self.job / 'audio-cache'
        cache.mkdir(exist_ok=True)
        clips = []
        for index, segment in enumerate(selected):
            self.check_cancel()
            text = segment['chinese'].strip()
            if not text:
                raise StudioInputError(f"{segment['id']} 译文为空")
            spoken = clean_tts_text(text) if backend_name == 'edge' else text
            speaker = segment['speaker_id']
            voice = document['speakers'][speaker]
            ref = document['references'].get(speaker)
            identity = {'text': text, 'voice': voice, 'backend': backend_name, 'style': config.get('style', 'conversation'), 'model': model if backend is None else backend.fingerprint(), 'version': 1, 'revision': '50f45ef0047cde7e84c2ef04326acb8ada2436a7' if model == CLONE_MODEL else model}
            if spoken != text:
                # Only changed lines pick up a new cache key, so unchanged Edge clips stay reusable.
                identity['spoken'] = spoken
            if backend is None:
                if ref is None:
                    raise StudioInputError(f'{speaker} 没有干净的参考声音，请换用系统音色或选择参考片段')
                reference = self.owned(ref['path'])
                identity['reference'] = {'sha256': sha256_file(reference), 'text': ref['text']}
            target = cache / (fingerprint(identity) + '.wav')
            self.progress('synthesis', index, len(selected), segment_id=segment['id'])
            if not self.valid_audio(target):
                if backend is not None and backend_name == 'edge' and not speakable_tts(spoken):
                    write_pcm_silence(target)
                    if not self.valid_audio(target):
                        raise StudioInputError(f"{segment['id']} 音频无效，请重试")
                else:
                    raw = self.temp / ('synthesis.wav' if backend is None else 'synthesis' + backend.file_suffix)
                    if backend is None:
                        self.worker({'operation': 'tts', 'model': model, 'text': text, 'reference': str(reference), 'reference_text': ref['text'], 'output': str(raw)})
                    else:
                        asyncio.run(backend.synthesize(SynthesisRequest(spoken, voice.get('voice_slot', 'male_1'), config.get('style', 'conversation')), raw))
                    staged = self.temp / 'normalized.wav'
                    self.ffmpeg('-i', raw, '-ac', 1, '-ar', 24000, '-c:a', 'pcm_s16le', staged)
                    if not self.valid_audio(staged):
                        raise StudioInputError(f"{segment['id']} 音频无效，请重试")
                    staged.replace(target)
            clips.append(target)
        listing = self.temp / 'concat.txt'
        # Cache paths consist only of hex names; concat manifest cannot inject directives.
        listing.write_text(''.join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in clips))
        self.progress('export', len(selected), len(selected))
        staged = self.temp / 'output.mp3'
        self.ffmpeg('-f', 'concat', '-safe', 0, '-i', listing, '-c:a', 'libmp3lame', '-b:a', '128k', staged)
        if not self.valid_audio(staged):
            raise StudioInputError('完整音频校验失败，已有导出保持不变')
        target = self.job / ('preview.mp3' if preview is not None else 'output.mp3')
        staged.replace(target)
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

    def publish_private_library(self):
        """Copy a finished full episode to iCloud. Never changes the job result."""
        try:
            import studio_publish_icloud
            try:
                studio_publish_icloud.ensure_publishable_title(self.job)
            except Exception:
                pass
            result = studio_publish_icloud.publish_job(self.job)
        except Exception as error:
            message = f'{type(error).__name__}: {error}'[:500]
            print(f'私人听：发布失败（成品已保留，任务仍成功）：{message}', flush=True)
            try:
                import studio_publish_icloud
                studio_publish_icloud.record_status(self.job, ok=False, error=message)
            except Exception:
                pass
            return None
        print(f"私人听：已发布「{result.get('title', '')}」→ {result.get('episode_dir', '')}", flush=True)
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

    def run(self, config, action='run', preview=None, accept_unreviewed=False):
        self.temp.mkdir(exist_ok=True)
        try:
            self.progress('source')
            self.check_cancel()
            source_identity = dict(config['source'])
            if source_identity.get('kind') == 'text':
                owned = self.owned_text(source_identity.get('value'))
                source_identity['value'] = str(owned)
                source_identity['sha256'] = sha256_file(owned)
            elif source_identity.get('kind') == 'local':
                source_identity['sha256'] = sha256_file(Path(source_identity['value']).expanduser())
            identity_path = self.job / 'source-identity.json'
            if identity_path.exists() and json.loads(identity_path.read_text()) != source_identity:
                raise StudioInputError('来源已经改变，请为新来源创建新任务')
            atomic_json(identity_path, source_identity)
            if config.get('content_type') == 'book':
                return self.finish_book(config, action)
            document_path = self.job / 'document.json'
            if action == 'prepare' or not document_path.exists():
                wav = self.acquire(config)
                document = self.prepare(config, wav)
            else:
                document = json.loads(document_path.read_text())
            output = None
            if action != 'prepare':
                output = self.render(config, document, preview, accept_unreviewed=accept_unreviewed)
            # Preview stays on the Mac. Only a full output.mp3 is offered to the phone.
            published = None
            if preview is None and output is not None and Path(output).name == 'output.mp3':
                published = self.publish_private_library()
            self.progress('complete', output=str(output) if output else None)
            self.push_private_device(published)
            return output
        except Cancelled:
            self.progress('cancelled', message='任务已取消；已完成片段可继续复用')
            raise
        except Exception as error:
            # Durable local diagnosis (Chinese app strings / type names — not stderr dumps).
            try:
                atomic_json(self.job / 'debug-failure.json', {
                    'stage': self.stage,
                    'exc_type': type(error).__name__,
                    'exc_message': str(error)[:500],
                    'updated_at': time.time(),
                })
            except OSError:
                pass
            visible = user_facing_message(error)
            # Never serialize arbitrary exception strings: SDKs may embed credentials.
            self.progress('failed', failed_stage=self.stage, error_code=error.code if isinstance(error, (StudioError, SpeakerGateError)) else ('invalid_input' if isinstance(error, (ValueError, FileNotFoundError)) else self.stage + '_failed'), message=visible or '处理失败，请检查当前阶段、依赖和模型配置后重试', **self._book_cursor())
            raise
        finally:
            shutil.rmtree(self.temp, ignore_errors=False)


def main():
    parser = argparse.ArgumentParser(description='播客工作室任务引擎')
    parser.add_argument('--job-dir', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--action', choices=['run', 'prepare', 'render'], default='run')
    parser.add_argument('--preview', type=int)
    parser.add_argument('--accept-unreviewed', action='store_true')
    args = parser.parse_args()
    engine = StudioEngine(args.job_dir)
    def cancel(*_):
        engine.cancelled = True
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        engine.run(json.loads(Path(args.config).read_text()), args.action, args.preview, accept_unreviewed=args.accept_unreviewed)
        return 0
    except Cancelled:
        return 130
    except Exception:
        print('任务失败，请查看 progress.json 中的阶段并检查配置后重试', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
