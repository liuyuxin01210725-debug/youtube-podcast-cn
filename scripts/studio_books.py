"""Chapter/block scaffold for private-listen books.

The user supplies a text file they already have the right to read. This module
does not download, search, or scrape book sources. A long book is stored as
chapters and timed blocks with a resume cursor. It is never one whole-book episode.
"""
from __future__ import annotations

import re
from pathlib import Path

NARRATOR_ID = 'narrator'
NARRATOR_SLOT = 'male_1'
ZH_PER_MIN = 230
EN_WORDS_PER_MIN = 150
ZH_TARGET = 15 * ZH_PER_MIN
ZH_MAX = 20 * ZH_PER_MIN
EN_TARGET = 15 * EN_WORDS_PER_MIN
EN_MAX = 20 * EN_WORDS_PER_MIN
MAX_CHARS = 2_000_000
TEXT_SUFFIXES = ('.txt', '.md')

_HEADING = re.compile(
    r'^(?:'
    r'#{1,3}[ \t]+\S.{0,80}'
    r'|第[0-9０-９一二三四五六七八九十百千万零〇两]+[章节回部卷][^\n]{0,40}'
    r'|(?:前言|序言|楔子|引子|后记|尾声|附录|跋|序章)(?:[：: ].{0,40})?'
    r'|(?:chapter|part)[ \t]+(?:\d+|[ivxlcdm]+)(?:[ \t.:：\-—].{0,60})?'
    r')$',
    re.IGNORECASE,
)


def read_user_text(path: Path) -> str:
    """UTF-8 text the user placed on disk. Rejects binaries and web URLs."""
    path = Path(path)
    raw_name = str(path)
    if raw_name.strip().lower().startswith(('http://', 'https://')):
        raise ValueError('书听不会从链接抓书')
    data = path.read_bytes()
    if b'\0' in data:
        raise ValueError('书稿不是纯文本')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('书稿请存成 UTF-8 文本') from None
    if len(text) > MAX_CHARS:
        raise ValueError('书稿过长')
    return text


def build_book(text: str, *, title: str = '', language: str = 'zh') -> dict:
    """Split user text into chapters and 15–20 minute blocks with a resume cursor."""
    if language not in ('zh', 'en'):
        raise ValueError('书的语言无效')
    if not isinstance(text, str):
        raise ValueError('书稿不是纯文本')
    if text.strip().lower().startswith(('http://', 'https://')) and '\n' not in text.strip():
        raise ValueError('书听不会从链接抓书')
    if len(text) > MAX_CHARS:
        raise ValueError('书稿过长')
    chapters = _chapters(text, language)
    if not chapters:
        raise ValueError('书里没有可听的正文')
    book = {
        'schema_version': 1,
        'content_type': 'book',
        'title': (title or '未命名').strip()[:120] or '未命名',
        'language': language,
        'listen_unit': 'chapter_block',
        'whole_book_file': False,
        'diarization': 'skipped',
        'expected_speakers': 1,
        'narrator': {'speaker_id': NARRATOR_ID, 'voice_slot': NARRATOR_SLOT},
        'chapters': chapters,
        'progress': {
            'chapter_id': chapters[0]['id'],
            'block_id': chapters[0]['blocks'][0]['id'],
            'offset_chars': 0,
            'completed_block_ids': [],
        },
    }
    _guard_shape(book, language)
    return book


def narrator_document(book: dict) -> dict:
    """One narrator, one segment per block. Not a single whole-book segment."""
    segments = []
    for chapter in book['chapters']:
        for block in chapter['blocks']:
            body = block['text']
            segments.append({
                'id': block['id'],
                'chapter_id': chapter['id'],
                'block_id': block['id'],
                'start_ms': 0,
                'end_ms': 0,
                'text': body,
                'chinese': body if book.get('language') == 'zh' else '',
                'speaker_id': NARRATOR_ID,
                'ambiguous': False,
            })
    return {
        'schema_version': 1,
        'transcript_source': 'book',
        'content_type': 'book',
        'segments': segments,
        'speakers': {NARRATOR_ID: {'voice_slot': NARRATOR_SLOT}},
        'references': {},
        'voices_confirmed': False,
    }


def resume_fields(book: dict) -> dict:
    progress = book.get('progress') or {}
    return {
        'chapter_id': progress.get('chapter_id'),
        'block_id': progress.get('block_id'),
        'offset_chars': progress.get('offset_chars', 0),
    }


def seek(book: dict, *, chapter_id: str, block_id: str, offset_chars: int) -> dict:
    """Move the resume cursor. Offset is a character index into that block."""
    if type(offset_chars) is not int or offset_chars < 0:
        raise ValueError('进度偏移无效')
    for chapter in book.get('chapters') or []:
        if chapter.get('id') != chapter_id:
            continue
        for block in chapter.get('blocks') or []:
            if block.get('id') != block_id:
                continue
            if offset_chars > len(block.get('text') or ''):
                raise ValueError('进度超出这一块')
            done = list((book.get('progress') or {}).get('completed_block_ids') or [])
            book['progress'] = {
                'chapter_id': chapter_id,
                'block_id': block_id,
                'offset_chars': offset_chars,
                'completed_block_ids': done,
            }
            return book['progress']
    raise ValueError('找不到这一章或这一块')


def _measure(text: str, language: str) -> int:
    if language == 'en':
        return len(text.split())
    return len(re.sub(r'\s+', '', text))


def _chapters(text: str, language: str) -> list[dict]:
    raw = _split_headings(text)
    target, maximum = (EN_TARGET, EN_MAX) if language == 'en' else (ZH_TARGET, ZH_MAX)
    chapters = []
    for index, (title, body) in enumerate(raw, start=1):
        pieces = _pieces(body, language, maximum)
        blocks = []
        for block_index, block_text in enumerate(_pack(pieces, language, target, maximum), start=1):
            chapter_id = f'c{index:03d}'
            blocks.append({
                'id': f'{chapter_id}b{block_index:03d}',
                'text': block_text,
                'chars': len(block_text),
                'measure': _measure(block_text, language),
            })
        if not blocks:
            continue
        chapters.append({'id': f'c{index:03d}', 'title': title, 'blocks': blocks})
    return chapters


def _split_headings(text: str) -> list[tuple[str, str]]:
    title = None
    buf: list[str] = []
    found: list[tuple[str, str]] = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if _HEADING.match(line.strip()):
            body = '\n'.join(buf).strip()
            if title is not None or body:
                found.append((title or '正文', body))
            title = re.sub(r'^#{1,3}[ \t]+', '', line.strip())
            buf = []
        else:
            buf.append(line)
    body = '\n'.join(buf).strip()
    if title is None:
        return [('正文', body)] if body else []
    found.append((title, body))
    return [(name, chunk) for name, chunk in found if chunk.strip()]


def _paragraphs(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r'\n\s*\n', text.strip()) if part.strip()]
    if len(parts) <= 1:
        parts = [line.strip() for line in text.split('\n') if line.strip()]
    return parts or [text.strip()]


def _sentences(text: str, language: str) -> list[str]:
    if language == 'en':
        parts = re.findall(r'.+?(?:[.!?](?=\s|$)|\Z)', text, flags=re.S)
    else:
        parts = re.findall(r'[^。！？!?；;\n]+[。！？!?；;]?', text)
    cleaned = [part.strip() for part in parts if part and part.strip()]
    return cleaned or [text.strip()]


def _pieces(text: str, language: str, maximum: int) -> list[str]:
    pieces = []
    for paragraph in _paragraphs(text):
        if _measure(paragraph, language) <= maximum:
            pieces.append(paragraph)
            continue
        for sentence in _sentences(paragraph, language):
            if _measure(sentence, language) <= maximum:
                pieces.append(sentence)
                continue
            pieces.extend(_hard_windows(sentence, language, maximum))
    return [piece for piece in pieces if piece.strip()]


def _hard_windows(text: str, language: str, maximum: int) -> list[str]:
    if language == 'en':
        words = text.split()
        return [' '.join(words[i:i + maximum]) for i in range(0, len(words), maximum)] or [text]
    return [text[i:i + maximum] for i in range(0, len(text), maximum)] or [text]


def _pack(pieces: list[str], language: str, target: int, maximum: int) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in pieces:
        piece_size = _measure(piece, language)
        if current and size + piece_size > target:
            blocks.append('\n\n'.join(current))
            current, size = [], 0
        current.append(piece)
        size += piece_size
        if size >= maximum:
            blocks.append('\n\n'.join(current))
            current, size = [], 0
    if current:
        blocks.append('\n\n'.join(current))
    return [block for block in blocks if block.strip()]


def _guard_shape(book: dict, language: str) -> None:
    blocks = [block for chapter in book['chapters'] for block in chapter['blocks']]
    maximum = EN_MAX if language == 'en' else ZH_MAX
    total = sum(block['measure'] for block in blocks)
    if total > maximum and len(blocks) < 2:
        raise ValueError('长书必须分成多章或多块，不能只有整本一条')
    for block in blocks:
        if block['measure'] > maximum:
            raise ValueError('有一块超过约 20 分钟，不能整段收听')
    if any(block['text'] for block in blocks) and book.get('whole_book_file'):
        raise ValueError('书不能只存成整本一条')
