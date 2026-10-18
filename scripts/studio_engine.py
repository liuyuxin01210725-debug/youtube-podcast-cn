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
                        translate_with_resume, get_provider_config, SherpaDiarizationBackend, _is_exact_youtube_url)
from podcast_captions import CaptionPiece, SubtitleSegment, parse_vtt_pieces, group_caption_pieces
from diarization_core import SpeakerBounds, align_caption_pieces, validate_diarization
from temporary_audio import download_audio_only, sha256_file
from tts_backends import build_backend, SynthesisRequest

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
    return StudioError('process_failed', '处理程序执行失败，请检查当前阶段的依赖、音源与模型配置后重试。')


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
        self.reference_pieces = None
        self.temp = self.job / 'temporary'

    def check_cancel(self):
        if self.cancelled or (self.job / 'cancel').exists():
            raise Cancelled('任务已取消，可继续使用已完成的缓存')

    def progress(self, stage, completed=0, total=0, **extra):
        if stage not in ('failed', 'cancelled'):
            self.stage = stage
        atomic_json(self.job / 'progress.json', {'stage': stage, 'completed': completed, 'total': total, 'updated_at': time.time(), **extra})

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
        if source['kind'] == 'youtube':
            if not _is_exact_youtube_url(source['value']):
                raise StudioInputError('请输入完整的 YouTube 视频链接')
            media = download_audio_only(source['value'], self.temp, runner=self.runner)
        elif source['kind'] == 'local':
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
        if config['source']['kind'] == 'youtube':
            try:
                info = fetch_video_info(config['source']['value'], runner=self.runner)
                vtt = download_english_subtitles(config['source']['value'], self.temp, info, runner=self.runner)
                vtt_text = vtt.read_text()
                pieces = parse_vtt_pieces(vtt_text)
                self.reference_pieces = raw_reference_pieces(vtt_text)
                if pieces:
                    return pieces, 'subtitles'
            except Cancelled:
                raise
            except Exception:
                self.progress('asr', message='英文字幕不可用，改用本地识别')
        with wave.open(str(wav)) as audio:
            seconds = audio.getnframes() / audio.getframerate()
        pieces = []
        # Small fixed chunks keep ASR memory bounded. Offsets preserve source time.
        for index, start in enumerate(range(0, int(seconds) + 1, 60)):
            if start >= seconds:
                break
            self.progress('asr', index, max(1, int(seconds / 60) + 1))
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

    def prepare(self, config, wav):
        self.diarization_threshold = float(config.get('diarization_threshold', 0.5))
        if not 0 < self.diarization_threshold < 2:
            raise StudioInputError('声纹聚类阈值须介于 0 和 2 之间')
        self.progress('transcript')
        pieces, transcript_source = self.transcribe(wav, config)
        if not pieces:
            raise StudioInputError('没有识别出英文对白，请检查文件或字幕')
        self.progress('diarization')
        backend = SherpaDiarizationBackend(runner=self.runner)
        bounds = SpeakerBounds(exact=config.get('speaker_count'))
        turns = backend.diarize(wav, bounds)
        assignments = align_caption_pieces(pieces, turns)
        validate_diarization(turns, duration_ms=self.duration_ms(wav), bounds=bounds, assignments=assignments)
        if any(a.speaker_id is None for a in assignments):
            raise StudioInputError('部分对白无法匹配说话人，请检查音源')
        segments = group_caption_pieces(pieces, {a.source_index: a.speaker_id for a in assignments}, {a.source_index for a in assignments if a.ambiguous}, max_chars=180)
        self.progress('translation', 0, len(segments))
        provider = config.get('provider', 'deepseek')
        client = create_translation_client(provider, api_key=self.credential(provider))
        def translate(batch):
            self.check_cancel()
            return client.translate(batch)
        translated = translate_with_resume(segments, self.job / 'translations', translate, translation_identity=client.translation_identity)
        reference_pieces = self.reference_pieces if self.reference_pieces is not None else pieces
        reference_assignments = align_caption_pieces(reference_pieces, turns) if self.reference_pieces is not None else assignments
        refs = select_references(reference_pieces, reference_assignments, turns)
        references = {}
        reference_dir = self.job / 'references'
        reference_dir.mkdir(exist_ok=True)
        for speaker, ref in refs.items():
            # Speaker identifiers never become filesystem paths.
            name = fingerprint(speaker)[:16] + '.wav'
            output = reference_dir / name
            self.ffmpeg('-ss', ref['start_ms'] / 1000, '-i', wav, '-t', (ref['end_ms'] - ref['start_ms']) / 1000, output)
            references[speaker] = {**ref, 'path': str(output.relative_to(self.job)), 'sha256': sha256_file(output)}
        document = {'schema_version': 1, 'transcript_source': transcript_source,
                    'segments': [{**asdict(s), 'chinese': translated[s.id], 'speaker_id': s.speaker_id} for s in segments],
                    'speakers': {speaker: {'voice_slot': ['male_1', 'male_2', 'male_3', 'female_1', 'female_2'][index % 5]} for index, speaker in enumerate(dict.fromkeys(s.speaker_id for s in segments))}, 'references': references}
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
        except RuntimeError:
            return False

    def render(self, config, document, preview=None):
        segments = document['segments']
        if not segments or len({s['id'] for s in segments}) != len(segments):
            raise StudioInputError('段落为空或编号重复')
        selected = segments if preview is None else segments[:int(preview)]
        if not selected:
            raise StudioInputError('试听段数必须大于零')
        backend_name = config.get('tts', 'clone')
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
            speaker = segment['speaker_id']
            voice = document['speakers'][speaker]
            ref = document['references'].get(speaker)
            identity = {'text': text, 'voice': voice, 'backend': backend_name, 'style': config.get('style', 'conversation'), 'model': model if backend is None else backend.fingerprint(), 'version': 1, 'revision': '50f45ef0047cde7e84c2ef04326acb8ada2436a7' if model == CLONE_MODEL else model}
            if backend is None:
                if ref is None:
                    raise StudioInputError(f'{speaker} 没有干净的参考声音，请换用系统音色或选择参考片段')
                reference = self.owned(ref['path'])
                identity['reference'] = {'sha256': sha256_file(reference), 'text': ref['text']}
            target = cache / (fingerprint(identity) + '.wav')
            self.progress('synthesis', index, len(selected), segment_id=segment['id'])
            if not self.valid_audio(target):
                raw = self.temp / ('synthesis.wav' if backend is None else 'synthesis' + backend.file_suffix)
                if backend is None:
                    self.worker({'operation': 'tts', 'model': model, 'text': text, 'reference': str(reference), 'reference_text': ref['text'], 'output': str(raw)})
                else:
                    asyncio.run(backend.synthesize(SynthesisRequest(text, voice.get('voice_slot', 'male_1'), config.get('style', 'conversation')), raw))
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

    def run(self, config, action='run', preview=None):
        self.temp.mkdir(exist_ok=True)
        try:
            self.progress('source')
            self.check_cancel()
            source_identity = dict(config['source'])
            if source_identity.get('kind') == 'local':
                source_identity['sha256'] = sha256_file(Path(source_identity['value']).expanduser())
            identity_path = self.job / 'source-identity.json'
            if identity_path.exists() and json.loads(identity_path.read_text()) != source_identity:
                raise StudioInputError('来源已经改变，请为新来源创建新任务')
            atomic_json(identity_path, source_identity)
            document_path = self.job / 'document.json'
            if action == 'prepare' or not document_path.exists():
                wav = self.acquire(config)
                document = self.prepare(config, wav)
            else:
                document = json.loads(document_path.read_text())
            output = None
            if action != 'prepare':
                output = self.render(config, document, preview)
            self.progress('complete', output=str(output) if output else None)
            return output
        except Cancelled:
            self.progress('cancelled', message='任务已取消；已完成片段可继续复用')
            raise
        except Exception as error:
            # Never serialize arbitrary exception strings: SDKs may embed credentials.
            self.progress('failed', failed_stage=self.stage, error_code=error.code if isinstance(error, StudioError) else ('invalid_input' if isinstance(error, (ValueError, FileNotFoundError)) else self.stage + '_failed'), message=str(error) if isinstance(error, (StudioError, StudioInputError)) else '处理失败，请检查当前阶段、依赖和模型配置后重试')
            raise
        finally:
            shutil.rmtree(self.temp, ignore_errors=False)


def main():
    parser = argparse.ArgumentParser(description='播客工作室任务引擎')
    parser.add_argument('--job-dir', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--action', choices=['run', 'prepare', 'render'], default='run')
    parser.add_argument('--preview', type=int)
    args = parser.parse_args()
    engine = StudioEngine(args.job_dir)
    def cancel(*_):
        engine.cancelled = True
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        engine.run(json.loads(Path(args.config).read_text()), args.action, args.preview)
        return 0
    except Cancelled:
        return 130
    except Exception:
        print('任务失败，请查看 progress.json 中的阶段并检查配置后重试', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
