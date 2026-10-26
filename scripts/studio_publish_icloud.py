#!/usr/bin/env python3
"""Copy a finished studio episode into the personal iCloud Drive library.

The iPhone app reads the same folder. Preview renders are not published.
Failures are the caller's problem to swallow: this module raises PublishError
and does not know about studio exit codes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

LIBRARY_NAME = '中文播客私人库'
AUDIO_NAME = 'output.mp3'
_JOB_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_YOUTUBE_ID = re.compile(r'^[A-Za-z0-9_-]{6,32}$')
_YOUTUBE_HOSTS = {
    'youtube.com',
    'www.youtube.com',
    'm.youtube.com',
    'music.youtube.com',
}


class PublishError(Exception):
    """Message is safe to print. It is not a studio job failure."""


def default_library_root() -> Path:
    override = os.environ.get('PRIVATE_LISTEN_LIBRARY', '').strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs' / LIBRARY_NAME


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def record_status(job_dir: Path, *, ok: bool, error: str | None = None, episode_dir: str | None = None, episode_id: str | None = None) -> None:
    atomic_write_json(Path(job_dir) / 'private-listen.json', {
        'ok': bool(ok),
        'error': error,
        'episode_dir': episode_dir,
        'episode_id': episode_id,
        'published_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    })


def youtube_id_from_url(value: str) -> str | None:
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return None
    host = (parsed.hostname or '').casefold()
    ident = ''
    if host == 'youtu.be':
        ident = parsed.path.strip('/').split('/')[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path == '/watch':
            ident = parse_qs(parsed.query).get('v', [''])[0]
        else:
            for prefix in ('/embed/', '/shorts/', '/live/', '/v/'):
                if parsed.path.startswith(prefix):
                    ident = parsed.path[len(prefix):].split('/')[0]
                    break
    return ident if _YOUTUBE_ID.fullmatch(ident or '') else None


def _valid_youtube_id(value) -> str | None:
    if isinstance(value, str) and _YOUTUBE_ID.fullmatch(value):
        return value
    return None


def _source_dict(job: Path, name: str):
    data = read_json(job / name)
    if not isinstance(data, dict):
        return None
    if name == 'config.json':
        source = data.get('source')
        return source if isinstance(source, dict) else None
    return data


def episode_title(job: Path, job_id: str) -> str:
    state = read_json(job / 'state.json')
    candidates = []
    if isinstance(state, dict):
        candidates.append(state.get('title'))
    metadata = read_json(job / 'metadata.json')
    if isinstance(metadata, dict):
        candidates.extend(metadata.get(key) for key in ('source_title', 'title'))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        text = candidate.strip()
        if text and youtube_id_from_url(text) is None and not text.startswith('http'):
            return text[:500]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:500]
    return job_id


def episode_youtube_id(job: Path) -> str | None:
    metadata = read_json(job / 'metadata.json')
    if isinstance(metadata, dict):
        found = _valid_youtube_id(metadata.get('video_id'))
        if found:
            return found
    for name in ('config.json', 'source-identity.json'):
        source = _source_dict(job, name)
        if not source:
            continue
        found = youtube_id_from_url(str(source.get('value') or ''))
        if found:
            return found
    state = read_json(job / 'state.json')
    if isinstance(state, dict):
        return youtube_id_from_url(str(state.get('title') or ''))
    return None


def probe_duration_sec(path: Path) -> float | None:
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < 0 or value != value:
        return None
    return round(value, 3)


def _episode_record(episode_dir: Path) -> dict | None:
    data = read_json(episode_dir / 'episode.json')
    if not isinstance(data, dict):
        return None
    ident = episode_dir.name
    if not _JOB_ID.fullmatch(ident):
        return None
    if data.get('file') not in (None, AUDIO_NAME):
        return None
    title = data.get('title')
    created = data.get('created_at')
    if not isinstance(title, str) or not title.strip() or not isinstance(created, str):
        return None
    duration = data.get('duration_sec')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        duration = None
    record = {
        'id': ident,
        'title': title.strip()[:500],
        'created_at': created,
        'duration_sec': duration,
        'source_job_id': ident,
        'file': AUDIO_NAME,
    }
    youtube_id = _valid_youtube_id(data.get('youtube_id'))
    if youtube_id:
        record['youtube_id'] = youtube_id
    return record


def rebuild_library(library: Path) -> dict:
    episodes = []
    root = library / 'episodes'
    if root.is_dir():
        for episode_dir in root.iterdir():
            if not episode_dir.is_dir() or episode_dir.is_symlink():
                continue
            record = _episode_record(episode_dir)
            if record is not None:
                episodes.append(record)
    episodes.sort(key=lambda item: (item.get('created_at') or '', item.get('id') or ''), reverse=True)
    payload = {
        'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'episodes': episodes,
    }
    atomic_write_json(library / 'library.json', payload)
    return payload


def _copy_audio(src: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / AUDIO_NAME
    temporary = dest_dir / f'.{AUDIO_NAME}.{os.getpid()}.partial'
    try:
        with src.open('rb') as incoming, temporary.open('wb') as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise PublishError(f'复制音频失败：{error}') from error


def publish_job(job_dir, library_root=None) -> dict:
    """Copy output.mp3 and rewrite library.json. Raises PublishError on failure."""
    job = Path(job_dir).expanduser().resolve()
    if not job.is_dir() or job.is_symlink():
        raise PublishError('任务目录不存在')
    job_id = job.name
    if not _JOB_ID.fullmatch(job_id):
        raise PublishError('任务编号无效')
    src = job / AUDIO_NAME
    if not src.is_file() or src.is_symlink():
        raise PublishError('任务目录里没有完整的 output.mp3')
    library = Path(library_root).expanduser().resolve() if library_root else default_library_root()
    try:
        library.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PublishError(f'无法创建 iCloud 资料库：{error}') from error
    created_at = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
    record = {
        'id': job_id,
        'title': episode_title(job, job_id),
        'created_at': created_at,
        'duration_sec': probe_duration_sec(src),
        'source_job_id': job_id,
        'file': AUDIO_NAME,
    }
    youtube_id = episode_youtube_id(job)
    if youtube_id:
        record['youtube_id'] = youtube_id
    episode_dir = library / 'episodes' / job_id
    _copy_audio(src, episode_dir)
    try:
        atomic_write_json(episode_dir / 'episode.json', record)
        rebuild_library(library)
    except OSError as error:
        raise PublishError(f'写入节目清单失败：{error}') from error
    try:
        record_status(job, ok=True, episode_dir=str(episode_dir), episode_id=job_id)
    except OSError:
        pass
    return {**record, 'episode_dir': str(episode_dir), 'library': str(library)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='把已完成的工作室任务发布到 iCloud 私人听资料库')
    parser.add_argument('--job-dir', required=True, type=Path)
    parser.add_argument('--library', type=Path, default=None, help='覆盖资料库根目录，默认 iCloud Drive 里的「中文播客私人库」')
    args = parser.parse_args(argv)
    job = args.job_dir.expanduser()
    try:
        result = publish_job(job, library_root=args.library)
    except Exception as error:
        message = str(error) or type(error).__name__
        print(f'私人听：发布失败：{message}', file=sys.stderr)
        if job.is_dir():
            try:
                record_status(job, ok=False, error=message[:500])
            except OSError:
                pass
        return 1
    print(f"私人听：已发布「{result['title']}」→ {result['episode_dir']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
