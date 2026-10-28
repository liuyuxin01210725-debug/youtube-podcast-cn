"""Per-chapter / per-block lazy synth queue for private-listen books.

On-disk layout (confirmed): ``chapters/<chapter_id>/<block_id>.mp3`` under the
job or iCloud episode folder. Enqueue defaults to ``execute=False`` (no network).
When ``execute=True``, ``synthesize_block`` runs DeepSeek EN→CN (stub-safe if no
key) then the same Edge TTS backend podcast episodes use. Optional
``materialize_tiny_block`` copies short silence for path tests only.
Whole-book ``output.mp3`` remains forbidden.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

QUEUE_NAME = 'synth_queue.json'
SCHEMA_VERSION = 1
VALID_STATUS = frozenset({'queued', 'running', 'ready', 'failed'})


class BookSynthError(ValueError):
    """User-visible queue / path error for book lazy synth."""


def queue_path(job: Path) -> Path:
    return Path(job) / QUEUE_NAME


def audio_path(job: Path, *, chapter_id: str, block_id: str) -> Path:
    """Agreed on-disk location for one block mp3. May not exist yet."""
    _require_id(chapter_id, '章')
    _require_id(block_id, '块')
    return Path(job) / 'chapters' / chapter_id / f'{block_id}.mp3'


def load_queue(job: Path) -> dict:
    path = queue_path(job)
    if not path.is_file():
        return {'schema_version': SCHEMA_VERSION, 'items': []}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise BookSynthError('合成队列损坏') from error
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise BookSynthError('合成队列格式无效')
    data.setdefault('schema_version', SCHEMA_VERSION)
    return data


def save_queue(job: Path, queue: dict) -> None:
    job = Path(job)
    job.mkdir(parents=True, exist_ok=True)
    path = queue_path(job)
    temporary = path.with_suffix(path.suffix + f'.{uuid.uuid4().hex}.partial')
    payload = json.dumps(queue, ensure_ascii=False, indent=2) + '\n'
    temporary.write_text(payload)
    temporary.replace(path)


def enqueue_chapter(job: Path, book: dict, chapter_id: str) -> dict:
    """Queue every block in a chapter. Does not start TTS."""
    chapter = _find_chapter(book, chapter_id)
    items = []
    for block in chapter['blocks']:
        items.append(enqueue_block(job, book, block['id']))
    return {'chapter_id': chapter_id, 'enqueued': items}


def enqueue_block(job: Path, book: dict, block_id: str) -> dict:
    """Record one pending block synth. Idempotent if already queued/running/ready."""
    chapter_id, block = _find_block(book, block_id)
    queue = load_queue(job)
    for item in queue['items']:
        if item.get('block_id') == block_id and item.get('status') in ('queued', 'running', 'ready'):
            return item
    dest = audio_path(job, chapter_id=chapter_id, block_id=block_id)
    item = {
        'id': f'synth-{uuid.uuid4().hex[:12]}',
        'chapter_id': chapter_id,
        'block_id': block_id,
        'status': 'queued',
        'audio_file': relative_audio_file(chapter_id, block_id),
        'chars': len(block.get('text') or ''),
        'created_at': _now(),
        'updated_at': _now(),
        'error': None,
        # Explicit: runners must not treat this stub as permission to burn TTS.
        'execute': False,
    }
    queue['items'].append(item)
    save_queue(job, queue)
    return item


def next_pending(job: Path) -> dict | None:
    queue = load_queue(job)
    for item in queue['items']:
        if item.get('status') == 'queued':
            return item
    return None


def mark(job: Path, item_id: str, status: str, *, error: str | None = None) -> dict:
    if status not in VALID_STATUS:
        raise BookSynthError('合成状态无效')
    queue = load_queue(job)
    for item in queue['items']:
        if item.get('id') != item_id:
            continue
        item['status'] = status
        item['updated_at'] = _now()
        item['error'] = error
        if status == 'ready':
            item['execute'] = False
        save_queue(job, queue)
        return item
    raise BookSynthError('找不到这一条合成任务')


def summarize(job: Path, book: dict) -> dict:
    """Merge queue status onto chapter_index rows for App / API consumers."""
    from studio_books import chapter_index

    queue = load_queue(job)
    by_block = {}
    for item in queue['items']:
        block_id = item.get('block_id')
        if isinstance(block_id, str):
            by_block[block_id] = item
    index = chapter_index(book)
    for chapter in index['chapters']:
        for block in chapter['blocks']:
            item = by_block.get(block['id'])
            path = audio_path(job, chapter_id=chapter['id'], block_id=block['id'])
            if path.is_file() and path.stat().st_size > 0:
                block['audio_status'] = 'ready'
                block['audio_file'] = str(path.relative_to(Path(job)))
            elif item:
                block['audio_status'] = item.get('status') or 'queued'
                block['audio_file'] = item.get('audio_file')
            else:
                block['audio_status'] = 'missing'
                block['audio_file'] = None
    return {
        'book': index,
        'queue': {
            'pending': sum(1 for item in queue['items'] if item.get('status') == 'queued'),
            'running': sum(1 for item in queue['items'] if item.get('status') == 'running'),
            'ready': sum(1 for item in queue['items'] if item.get('status') == 'ready'),
            'failed': sum(1 for item in queue['items'] if item.get('status') == 'failed'),
            'items': queue['items'],
        },
    }


def _find_chapter(book: dict, chapter_id: str) -> dict:
    _require_id(chapter_id, '章')
    for chapter in book.get('chapters') or []:
        if chapter.get('id') == chapter_id:
            if not chapter.get('blocks'):
                raise BookSynthError('这一章没有可合成的块')
            return chapter
    raise BookSynthError('找不到这一章')


def _find_block(book: dict, block_id: str) -> tuple[str, dict]:
    _require_id(block_id, '块')
    for chapter in book.get('chapters') or []:
        for block in chapter.get('blocks') or []:
            if block.get('id') == block_id:
                return chapter['id'], block
    raise BookSynthError('找不到这一块')


def _require_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise BookSynthError(f'{label}编号无效')
    allowed = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-')
    if any(ch not in allowed for ch in value):
        raise BookSynthError(f'{label}编号无效')


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def relative_audio_file(chapter_id: str, block_id: str) -> str:
    """Stable relative path used in queue items and iCloud episode trees."""
    _require_id(chapter_id, '章')
    _require_id(block_id, '块')
    return f'chapters/{chapter_id}/{block_id}.mp3'


def ensure_chapter_dir(job: Path, chapter_id: str) -> Path:
    _require_id(chapter_id, '章')
    folder = Path(job) / 'chapters' / chapter_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def contains_cjk(text: str) -> bool:
    return any('\u4e00' <= ch <= '\u9fff' for ch in (text or ''))


def _translation_client(provider: str = 'deepseek'):
    """Reuse studio DeepSeek/Qwen client; None when no key (tests/CI stay green)."""
    try:
        from podcast_cn import create_translation_client, get_provider_config
        import os
        import shutil
        import subprocess
        import sys

        config = get_provider_config(provider)
    except Exception:
        return None
    key = os.getenv(config.api_key_env, '').strip()
    if not key and sys.platform == 'darwin' and shutil.which('security'):
        try:
            result = subprocess.run(
                [
                    'security',
                    'find-generic-password',
                    '-a',
                    os.getenv('USER', ''),
                    '-s',
                    config.keychain_service,
                    '-w',
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            key = result.stdout.strip()
    if not key:
        return None
    try:
        return create_translation_client(provider, api_key=key)
    except Exception:
        return None


def translate_block_text(
    text: str,
    *,
    language: str | None = None,
    provider: str = 'deepseek',
    client=None,
) -> dict:
    """EN→CN for one book block. Stub-safe: returns source text when no key.

    Returns ``{text, translated, provider, stub}``. Already-CJK text is kept.
    """
    source = (text or '').strip()
    if not source:
        return {'text': '', 'translated': False, 'provider': None, 'stub': True}
    if language == 'zh' or contains_cjk(source):
        return {'text': source, 'translated': False, 'provider': None, 'stub': False}
    if client is None:
        client = _translation_client(provider)
    if client is None:
        return {'text': source, 'translated': False, 'provider': None, 'stub': True}
    try:
        from podcast_cn import SubtitleSegment

        translated = client.translate([SubtitleSegment('b1', 0, 1000, source)])
        out = (translated.get('b1') or '').strip()
    except Exception:
        return {'text': source, 'translated': False, 'provider': provider, 'stub': True}
    if not out or not contains_cjk(out):
        return {'text': source, 'translated': False, 'provider': provider, 'stub': True}
    return {'text': out, 'translated': True, 'provider': provider, 'stub': False}


def synthesize_block(
    job: Path,
    book: dict,
    block_id: str,
    *,
    execute: bool = False,
    voice_slot: str = 'male_1',
    translate: bool = True,
    provider: str = 'deepseek',
    backend=None,
    translator=None,
    style: str = 'narration',
) -> dict:
    """Synthesize one block to ``chapters/<c>/<b>.mp3``.

    ``execute=False`` (default): refuse network TTS — tests/CI stay offline.
    ``execute=True``: optional DeepSeek translate, then Edge TTS (same path as
    podcast episodes via ``tts_backends.build_backend('edge')``).
    """
    import asyncio
    import os

    chapter_id, block = _find_block(book, block_id)
    source_text = (block.get('text') or '').strip()
    if not source_text:
        raise BookSynthError('这一块没有可朗读的文字')

    queue = load_queue(job)
    item = None
    for row in queue['items']:
        if row.get('block_id') == block_id and row.get('status') in ('queued', 'running', 'ready', 'failed'):
            item = row
            break
    if item is None:
        item = enqueue_block(job, book, block_id)

    dest = audio_path(job, chapter_id=chapter_id, block_id=block_id)
    ensure_chapter_dir(job, chapter_id)

    if not execute:
        return {
            'ok': False,
            'execute': False,
            'skipped': True,
            'reason': 'execute=False',
            'chapter_id': chapter_id,
            'block_id': block_id,
            'audio_file': relative_audio_file(chapter_id, block_id),
            'item_id': item['id'],
        }

    mark(job, item['id'], 'running')
    language = book.get('language') if isinstance(book.get('language'), str) else None
    spoken = source_text
    translation = {'text': source_text, 'translated': False, 'provider': None, 'stub': False}
    if translate:
        translation = translate_block_text(
            source_text,
            language=language,
            provider=provider,
            client=translator,
        )
        spoken = translation['text']

    try:
        from studio_engine import clean_tts_text, speakable_tts
        from tts_backends import SynthesisRequest, TTSBackendError, build_backend

        cleaned = clean_tts_text(spoken)
        if not speakable_tts(cleaned):
            raise BookSynthError('译稿没有可朗读内容')
        if backend is None:
            backend = build_backend('edge')
        temporary = dest.with_suffix(dest.suffix + f'.{os.getpid()}.partial')
        if temporary.exists():
            temporary.unlink()
        asyncio.run(
            backend.synthesize(
                SynthesisRequest(cleaned, voice_slot, style),
                temporary,
            )
        )
        if not temporary.is_file() or temporary.stat().st_size < 32:
            temporary.unlink(missing_ok=True)
            raise BookSynthError('Edge TTS 未写出有效音频')
        temporary.replace(dest)
    except BookSynthError as error:
        mark(job, item['id'], 'failed', error=str(error))
        raise
    except TTSBackendError as error:
        mark(job, item['id'], 'failed', error=str(error)[:500])
        raise BookSynthError(str(error)) from error
    except Exception as error:
        mark(job, item['id'], 'failed', error=str(error)[:500])
        raise BookSynthError(f'合成失败：{error}') from error

    ready = mark(job, item['id'], 'ready')
    # Record that this ready item was produced with a real execute pass.
    queue = load_queue(job)
    for row in queue['items']:
        if row.get('id') == item['id']:
            row['execute'] = True
            row['translated'] = bool(translation.get('translated'))
            row['tts'] = 'edge'
            row['voice_slot'] = voice_slot
            row['chars_spoken'] = len(cleaned)
            row['updated_at'] = _now()
            save_queue(job, queue)
            ready = row
            break
    return {
        'ok': True,
        'execute': True,
        'skipped': False,
        'chapter_id': chapter_id,
        'block_id': block_id,
        'audio_file': relative_audio_file(chapter_id, block_id),
        'path': str(dest),
        'bytes': dest.stat().st_size,
        'translated': bool(translation.get('translated')),
        'translation_stub': bool(translation.get('stub')),
        'provider': translation.get('provider'),
        'item': ready,
    }


def materialize_tiny_block(
    job: Path,
    *,
    chapter_id: str,
    block_id: str,
    fixture_mp3: Path | None = None,
    mark_ready: bool = True,
) -> Path:
    """Copy a tiny silence mp3 into chapters/<c>/<b>.mp3. Not long TTS.

    Default fixture: repo ``fixtures/book-en-pilot/tiny-silence.mp3``.
    """
    dest = audio_path(job, chapter_id=chapter_id, block_id=block_id)
    ensure_chapter_dir(job, chapter_id)
    source = Path(fixture_mp3) if fixture_mp3 else _default_tiny_fixture()
    if not source.is_file():
        raise BookSynthError('找不到极短静音夹具')
    data = source.read_bytes()
    if len(data) < 32:
        raise BookSynthError('静音夹具无效')
    temporary = dest.with_suffix(dest.suffix + '.partial')
    temporary.write_bytes(data)
    temporary.replace(dest)
    if mark_ready:
        queue = load_queue(job)
        matched = False
        for item in queue['items']:
            if item.get('block_id') == block_id:
                item['status'] = 'ready'
                item['execute'] = False
                item['updated_at'] = _now()
                item['error'] = None
                item['audio_file'] = relative_audio_file(chapter_id, block_id)
                matched = True
        if not matched:
            queue['items'].append({
                'id': f'synth-{uuid.uuid4().hex[:12]}',
                'chapter_id': chapter_id,
                'block_id': block_id,
                'status': 'ready',
                'audio_file': relative_audio_file(chapter_id, block_id),
                'chars': 0,
                'created_at': _now(),
                'updated_at': _now(),
                'error': None,
                'execute': False,
            })
        save_queue(job, queue)
    return dest


def prepare_en_pilot(
    job: Path,
    *,
    source: Path | None = None,
    materialize_first: bool = True,
    execute: bool = False,
    voice_slot: str = 'male_1',
) -> dict:
    """Build EN pilot book.json, enqueue chapter 1, materialize or real-synth first block.

    Default ``execute=False``: tiny silence fixture (offline). ``execute=True`` on
    Mac: DeepSeek EN→CN (if key) + Edge TTS into ``chapters/<c>/<b>.mp3``.
    """
    from studio_books import build_book, chapter_index, read_user_text

    job = Path(job)
    job.mkdir(parents=True, exist_ok=True)
    src = Path(source) if source else _default_pilot_source()
    text = read_user_text(src)
    book = build_book(text, title='EN Pilot Open Field', language='en')
    book_path = job / 'book.json'
    book_path.write_text(json.dumps(book, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # episode-shaped sidecar for App library (true Book type, not per-chapter podcast)
    episode = {
        'id': job.name if job.name else 'en-pilot',
        'title': book['title'],
        'created_at': _now().replace('Z', '+00:00'),
        'content_type': 'book',
        'book_language': 'en',
        'listen_unit': 'chapter_block',
        'whole_book_file': False,
        'file': None,
    }
    (job / 'episode.json').write_text(json.dumps(episode, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    first_chapter = book['chapters'][0]['id']
    enqueued = enqueue_chapter(job, book, first_chapter)
    first_block = book['chapters'][0]['blocks'][0]
    materialized = None
    synth = None
    if execute:
        synth = synthesize_block(
            job,
            book,
            first_block['id'],
            execute=True,
            voice_slot=voice_slot,
            translate=True,
        )
        materialized = synth.get('audio_file')
    elif materialize_first:
        materialized = str(
            materialize_tiny_block(
                job,
                chapter_id=first_chapter,
                block_id=first_block['id'],
            ).relative_to(job)
        )
    index = chapter_index(book)
    summary = summarize(job, book)
    return {
        'book_path': str(book_path),
        'episode_path': str(job / 'episode.json'),
        'chapter_id': first_chapter,
        'block_id': first_block['id'],
        'audio_file': relative_audio_file(first_chapter, first_block['id']),
        'materialized': materialized,
        'execute': bool(execute),
        'synth': synth,
        'enqueued': len(enqueued['enqueued']),
        'chapters': len(index['chapters']),
        'queue': summary['queue'],
        'progress': index['progress'],
    }


def _default_tiny_fixture() -> Path:
    return Path(__file__).resolve().parents[1] / 'fixtures' / 'book-en-pilot' / 'tiny-silence.mp3'


def _default_pilot_source() -> Path:
    return Path(__file__).resolve().parents[1] / 'fixtures' / 'book-en-pilot' / 'source.md'
