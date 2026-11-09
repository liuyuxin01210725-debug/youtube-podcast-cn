#!/usr/bin/env python3
"""Watch the private-listen inbox and enqueue the existing Edge studio pipeline.

The phone only drops JSON. This process creates a studio job (tts=edge, action=run).
One bad file becomes a status JSON and the loop continues.
"""
from __future__ import annotations

import argparse
import fcntl
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from studio_publish_icloud import atomic_write_json, default_library_root, read_json
from studio_urls import canonical_url

ROOT = Path(__file__).resolve().parents[1]
_ID = re.compile(r'^[a-f0-9]{32}$')
_TERMINAL = {'published', 'duplicate', 'failed'}
_RETRY_PARSE = {'收件记录无法读取', '收件记录没有链接'}
_BLOCKING = {'queued', 'running', 'cancelling', 'interrupted', 'complete'}


def inbox_dir(library: Path) -> Path:
    return Path(library) / 'inbox'


def _status_path(inbox: Path, ident: str) -> Path:
    return inbox / f'{ident}.status.json'


def _read_status(inbox: Path, ident: str) -> dict:
    data = read_json(_status_path(inbox, ident))
    return data if isinstance(data, dict) else {}


def write_status(inbox: Path, ident: str, status: str, *, job_id: str | None = None, error: str | None = None) -> None:
    if not _ID.fullmatch(ident) or status not in {'queued', 'published', 'duplicate', 'failed'}:
        return
    payload = {
        'status': status,
        'job_id': job_id,
        'error': (error or None) and str(error)[:300],
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    atomic_write_json(_status_path(inbox, ident), payload)


def _source_url(job: Path) -> str | None:
    data = read_json(job / 'config.json')
    if not isinstance(data, dict):
        return None
    source = data.get('source')
    if not isinstance(source, dict):
        return None
    value = source.get('value')
    return value.strip() if isinstance(value, str) and value.strip() else None


def _job_state(job: Path) -> str:
    data = read_json(job / 'state.json')
    if isinstance(data, dict) and isinstance(data.get('status'), str):
        return data['status']
    return ''


def blocking_job(jobs: Path, url: str) -> Path | None:
    """A job that should keep a second share from starting another conversion."""
    canonical = canonical_url(url)
    if not canonical or not Path(jobs).is_dir():
        return None
    for child in Path(jobs).iterdir():
        if child.is_symlink() or not child.is_dir() or not _ID.fullmatch(child.name):
            continue
        source = _source_url(child)
        if not source or canonical_url(source) != canonical:
            continue
        audio = child / 'output.mp3'
        if _job_state(child) in _BLOCKING or (audio.is_file() and not audio.is_symlink()):
            return child
    return None


def _owner(inbox: Path, job_id: str, skip: str | None = None) -> str | None:
    if not inbox.is_dir():
        return None
    for path in inbox.glob('*.status.json'):
        ident = path.name[: -len('.status.json')]
        if ident == skip or not _ID.fullmatch(ident):
            continue
        data = read_json(path)
        if isinstance(data, dict) and data.get('job_id') == job_id:
            return ident
    return None


def iter_requests(inbox: Path) -> list[dict]:
    """Inbox records old enough to be a finished iCloud write. Skips junk and symlinks."""
    if not inbox.is_dir():
        return []
    found = []
    now = time.time()
    for path in inbox.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith('.') or not path.name.endswith('.json') or path.name.endswith('.status.json'):
            continue
        ident = path.name[:-5]
        if not _ID.fullmatch(ident):
            continue
        try:
            if path.resolve().parent != inbox.resolve():
                continue
            age = now - path.stat().st_mtime
            data = read_json(path)
        except OSError:
            continue
        if not isinstance(data, dict) or data.get('id') not in (None, ident):
            if age >= 120:
                write_status(inbox, ident, 'failed', error='收件记录无法读取')
            continue
        url = data.get('url')
        if not isinstance(url, str) or not url.strip():
            if age >= 120:
                write_status(inbox, ident, 'failed', error='收件记录没有链接')
            continue
        source_app = data.get('source_app')
        if not isinstance(source_app, str) or not source_app.strip():
            source_app = None
        else:
            source_app = source_app.strip()[:120]
        found.append({
            'id': ident,
            'url': url.strip(),
            'shared_at': data.get('shared_at') if isinstance(data.get('shared_at'), str) else '',
            'source_app': source_app,
            'content_type': data.get('content_type', 'conversation'),
            'expected_speakers': data.get('expected_speakers'),
        })
    found.sort(key=lambda item: (item['shared_at'], item['id']))
    return found


def enqueue(jobs: Path, url: str, *, content_type: str = 'conversation', expected_speakers: int | None = None) -> str:
    """Create one queued studio job. YouTube keeps captions; other https uses the same Edge path."""
    canonical = canonical_url(url)
    if not canonical:
        raise ValueError('不是公开的 HTTPS 链接')
    from studio_publish_icloud import youtube_id_from_url
    from studio_server import Studio

    kind = 'youtube' if youtube_id_from_url(canonical) else 'web'
    studio = Studio(jobs, recover=False)
    return studio.create(
        [{'kind': kind, 'value': canonical}],
        {'action': 'run', 'tts': 'edge', 'provider': 'deepseek', 'style': 'conversation',
         'content_type': content_type, 'expected_speakers': expected_speakers},
    )[0]


def job_inbox_status(job: Path) -> tuple[str, str | None]:
    """Map a studio job onto an inbox status. Message is already user-facing."""
    status = _job_state(job)
    if status in {'queued', 'running', 'cancelling', 'interrupted'}:
        return 'queued', None
    if status == 'cancelled':
        return 'failed', '任务已取消'
    note = read_json(job / 'private-listen.json')
    if status == 'complete':
        if isinstance(note, dict) and note.get('ok'):
            return 'published', None
        if isinstance(note, dict) and isinstance(note.get('error'), str) and note['error'].strip():
            return 'failed', note['error'].strip()[:300]
        audio = job / 'output.mp3'
        if audio.is_file() and not audio.is_symlink():
            return 'failed', '已合成，但没有放进资料库'
        return 'failed', '任务结束但没有成品'
    progress = read_json(job / 'progress.json')
    if isinstance(progress, dict) and isinstance(progress.get('message'), str) and progress['message'].strip():
        return 'failed', progress['message'].strip()[:300]
    return 'failed', '转换失败'


def server_busy(jobs: Path) -> bool:
    """True when the workbench holds the studio lock and will run the queue itself."""
    path = Path(jobs) / '.server.lock'
    if not path.exists():
        return False
    try:
        fd = path.open('a+')
    except OSError:
        return False
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        fd.close()


def engine_command(job: Path) -> list[str]:
    python = ROOT / '.venv' / 'bin' / 'python'
    if not python.is_file():
        python = Path(sys.executable)
    return [
        str(python),
        str(ROOT / 'scripts' / 'studio_engine.py'),
        '--job-dir', str(job),
        '--config', str(job / 'config.json'),
        '--action', 'run',
    ]


def run_job(jobs: Path, ident: str) -> int | None:
    """Run one engine process. None means the workbench lock is already held."""
    job = Path(jobs) / ident
    lock_path = Path(jobs) / '.server.lock'
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = lock_path.open('a+')
    except OSError as error:
        print(f'收件箱：无法锁定任务目录：{type(error).__name__}', flush=True)
        return 1
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
    state = read_json(job / 'state.json')
    if not isinstance(state, dict):
        state = {}
    state['status'] = 'running'
    state['action'] = state.get('action') or 'run'
    state['updated_at'] = time.time()
    try:
        atomic_write_json(job / 'state.json', state)
        proc = subprocess.Popen(engine_command(job), start_new_session=True)
        code = proc.wait()
    except OSError as error:
        code = 1
        print(f'收件箱：引擎没有启动：{type(error).__name__}', flush=True)
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
    state = read_json(job / 'state.json')
    if not isinstance(state, dict):
        state = {}
    state['status'] = 'cancelled' if code == 130 else ('complete' if code == 0 else 'failed')
    state['updated_at'] = time.time()
    try:
        atomic_write_json(job / 'state.json', state)
    except OSError:
        pass
    return code


def _sync(inbox: Path, ident: str, job: Path) -> str:
    status, error = job_inbox_status(job)
    if _job_state(job) == 'interrupted':
        state = read_json(job / 'state.json')
        if isinstance(state, dict):
            state['status'] = 'queued'
            state['updated_at'] = time.time()
            try:
                atomic_write_json(job / 'state.json', state)
            except OSError:
                pass
        status = 'queued'
    write_status(inbox, ident, status, job_id=job.name, error=error)
    return status


def process_inbox(library: Path, jobs: Path, *, run: bool = False) -> list[dict]:
    """Enqueue every new share. Optionally run one job when the workbench is closed."""
    library = Path(library)
    jobs = Path(jobs)
    inbox = inbox_dir(library)
    results = []
    runnable: list[tuple[str, str]] = []
    for record in iter_requests(inbox):
        ident = record['id']
        current = _read_status(inbox, ident)
        if current.get('status') in _TERMINAL and current.get('error') not in _RETRY_PARSE:
            results.append({'id': ident, 'status': current.get('status')})
            continue
        try:
            canonical = canonical_url(record['url'])
            if not canonical:
                write_status(inbox, ident, 'failed', error='不是公开的 HTTPS 链接')
                results.append({'id': ident, 'status': 'failed'})
                continue
            existing = blocking_job(jobs, canonical)
            bound = current.get('job_id') if isinstance(current.get('job_id'), str) else None
            if existing is not None:
                owner = _owner(inbox, existing.name, skip=ident)
                if owner and owner != ident and bound != existing.name:
                    write_status(inbox, ident, 'duplicate', job_id=existing.name)
                    results.append({'id': ident, 'status': 'duplicate', 'job_id': existing.name})
                    continue
                status = _sync(inbox, ident, existing)
                if status == 'queued':
                    runnable.append((ident, existing.name))
                results.append({'id': ident, 'status': status, 'job_id': existing.name})
                continue
            if bound and (jobs / bound).is_dir():
                status = _sync(inbox, ident, jobs / bound)
                if status == 'queued':
                    runnable.append((ident, bound))
                results.append({'id': ident, 'status': status, 'job_id': bound})
                continue
            if record.get('content_type') == 'article':
                from studio_articles import enqueue_article, normalize_article_url
                if not normalize_article_url(canonical):
                    write_status(inbox, ident, 'failed', error='文章链接无效（或是 YouTube，请当播客分享）')
                    results.append({'id': ident, 'status': 'failed'})
                    continue
                # execute=False: ledger only; full article TTS stays stub until explicitly enabled.
                info = enqueue_article(jobs, canonical, execute=False)
                write_status(inbox, ident, 'queued', job_id=info['job_id'])
                print(f'收件箱：文章已入账 {ident} → {info["job_id"]}（TTS stub）', flush=True)
                results.append({'id': ident, 'status': 'queued', 'job_id': info['job_id'], 'article': True, 'stub_tts': True})
                continue
            job_id = enqueue(jobs, canonical, content_type=record['content_type'], expected_speakers=record['expected_speakers'])
            write_status(inbox, ident, 'queued', job_id=job_id)
            runnable.append((ident, job_id))
            print(f'收件箱：已排队 {ident} → {job_id}', flush=True)
            results.append({'id': ident, 'status': 'queued', 'job_id': job_id})
        except Exception as error:
            print(f'收件箱：{ident} 没有放进队列：{type(error).__name__}', flush=True)
            try:
                write_status(inbox, ident, 'failed', error='没有放进队列')
            except OSError:
                pass
            results.append({'id': ident, 'status': 'failed'})
    if run and runnable and not server_busy(jobs):
        ident, job_id = runnable[0]
        code = run_job(jobs, job_id)
        if code is not None and (jobs / job_id).is_dir():
            status = _sync(inbox, ident, jobs / job_id)
            for item in results:
                if item.get('id') == ident:
                    item['status'] = status
            if status == 'failed':
                print(f'收件箱：转换失败 {ident}', flush=True)
            elif status == 'published':
                print(f'收件箱：已发布 {ident}', flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='监听私人听收件箱，用默认可辨音色排队转换')
    parser.add_argument('--library', type=Path, default=None, help='资料库根目录，默认 iCloud Drive 里的「中文播客私人库」')
    parser.add_argument('--jobs', type=Path, default=Path('~/Desktop/音频/中文播客工作台/jobs'))
    parser.add_argument('--interval', type=float, default=20)
    parser.add_argument('--once', action='store_true', help='处理一轮就退出')
    parser.add_argument('--no-run', action='store_true', help='只建任务。工作台开着时会自己跑队列。')
    args = parser.parse_args(argv)
    library = Path(args.library).expanduser() if args.library else default_library_root()
    jobs = Path(args.jobs).expanduser()
    try:
        library.mkdir(parents=True, exist_ok=True)
        inbox_dir(library).mkdir(parents=True, exist_ok=True)
        jobs.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f'收件箱：打不开资料库或任务目录：{error}', file=sys.stderr)
        return 1
    lock_path = inbox_dir(library) / '.watch.lock'
    try:
        lock_fd = lock_path.open('a+')
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('收件箱：已有一个监听在跑', flush=True)
        return 0
    except OSError as error:
        print(f'收件箱：无法锁定监听：{error}', file=sys.stderr)
        return 1
    stop = {'flag': False}

    def request_stop(*_args):
        stop['flag'] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(f'收件箱：监听 {inbox_dir(library)}', flush=True)
    while not stop['flag']:
        try:
            process_inbox(library, jobs, run=not args.no_run)
        except Exception as error:
            print(f'收件箱：本轮失败，继续监听：{type(error).__name__}', flush=True)
        if args.once or stop['flag']:
            break
        slept = 0.0
        interval = max(2.0, float(args.interval))
        while slept < interval and not stop['flag']:
            time.sleep(min(0.5, interval - slept))
            slept += 0.5
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
