"""Article URL → extract main text → enqueue translate+TTS job bookkeeping.

Defaults: DeepSeek + Edge, single narrator. Network fetch is optional; tests use
fixtures. execute=False (default) only writes the job ledger — no TTS run.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from studio_publish_icloud import atomic_write_json, youtube_id_from_url
from studio_urls import canonical_url, is_public_https, web_label

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / 'fixtures' / 'articles'
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


class _MainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._in_main = 0
        self._chunks: list[str] = []
        self.title = ''
        self._capture_title = False

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        if name in {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'header', 'aside'}:
            self._skip += 1
            return
        if name in {'article', 'main'}:
            self._in_main += 1
        if name == 'title':
            self._capture_title = True

    def handle_endtag(self, tag):
        name = tag.lower()
        if name in {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'header', 'aside'}:
            self._skip = max(0, self._skip - 1)
            return
        if name in {'article', 'main'}:
            self._in_main = max(0, self._in_main - 1)
        if name == 'title':
            self._capture_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._capture_title and not self.title:
            self.title = text[:200]
        if self._skip:
            return
        if self._in_main or True:
            # Prefer article/main chunks when present; still collect body text.
            weight = 2 if self._in_main else 1
            self._chunks.append((weight, text))

    def text(self) -> str:
        preferred = [chunk for weight, chunk in self._chunks if weight > 1]
        parts = preferred or [chunk for _, chunk in self._chunks]
        body = _WS_RE.sub(' ', ' '.join(parts)).strip()
        return body


def normalize_article_url(value: str) -> str | None:
    """Public https only; reject YouTube (those stay on the audio/caption path)."""
    canonical = canonical_url(value)
    if not canonical:
        return None
    if youtube_id_from_url(canonical):
        return None
    return canonical


def extract_main_text(html_text: str) -> dict:
    if not isinstance(html_text, str) or not html_text.strip():
        raise ValueError('页面为空')
    parser = _MainTextParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as error:  # noqa: BLE001 — surface as input error
        raise ValueError(f'无法解析 HTML：{type(error).__name__}') from error
    body = parser.text()
    if len(body) < 40:
        # Fallback: crude strip when article/main missing.
        stripped = _TAG_RE.sub(' ', html_text)
        body = _WS_RE.sub(' ', html.unescape(stripped)).strip()
    if len(body) < 40:
        raise ValueError('正文太短，不像文章')
    title = parser.title or body[:48]
    return {'title': title[:200], 'text': body[:200_000], 'chars': len(body)}


def load_fixture(name: str = 'sample-article.html') -> str:
    path = FIXTURE_DIR / name
    return path.read_text(encoding='utf-8')


def fetch_or_fixture(url: str, *, fixture_html: str | None = None, allow_network: bool = False) -> str:
    if fixture_html is not None:
        return fixture_html
    # Deterministic offline path: known host → bundled fixture.
    host = (urlsplit(url).hostname or '').casefold()
    if host in {'example.com', 'www.example.com', 'fixture.local'.casefold()}:
        return load_fixture()
    if not allow_network:
        raise ValueError('未开启抓页；请提供 fixture 或 allow_network=True')
    import urllib.request

    req = urllib.request.Request(url, headers={'User-Agent': 'PrivateListenArticle/0.1'})
    with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310 — operator-gated
        charset = response.headers.get_content_charset() or 'utf-8'
        return response.read().decode(charset, errors='replace')


def article_job_id(url: str) -> str:
    digest = hashlib.sha256(f'article:{url}'.encode()).hexdigest()
    return digest[:32]


def enqueue_article(
    jobs: Path,
    url: str,
    *,
    execute: bool = False,
    fixture_html: str | None = None,
    allow_network: bool = False,
    provider: str = 'deepseek',
    tts: str = 'edge',
) -> dict:
    """Create article job ledger. TTS run is stubbed unless execute=True (still no auto-run here)."""
    canonical = normalize_article_url(url)
    if not canonical:
        raise ValueError('不是可入队的文章 HTTPS 链接')
    if provider not in ('deepseek', 'qwen') or tts not in ('edge', 'clone'):
        raise ValueError('provider/tts 无效')
    raw = fetch_or_fixture(canonical, fixture_html=fixture_html, allow_network=allow_network)
    extracted = extract_main_text(raw)
    jobs = Path(jobs)
    jobs.mkdir(parents=True, exist_ok=True)
    ident = uuid.uuid4().hex
    job = jobs / ident
    job.mkdir()
    source_path = job / 'source.md'
    body = f"# {extracted['title']}\n\n{extracted['text']}\n"
    source_path.write_text(body, encoding='utf-8')
    label = extracted['title'] or web_label(canonical)
    config = {
        'source': {'kind': 'text', 'value': str(source_path)},
        'provider': provider,
        'tts': tts,
        'style': 'documentary',
        'content_type': 'article',
        'expected_speakers': 1,
        'diarization': 'skipped',
        'article_url': canonical,
        'article': {
            'title': extracted['title'],
            'chars': extracted['chars'],
            'execute': bool(execute),
        },
    }
    state = {
        'title': label[:200],
        'status': 'queued' if execute else 'prepared',
        'action': 'run' if execute else 'prepare',
        'message': None if execute else '文章已入队（execute=False，未跑 TTS）',
        'updated_at': time.time(),
    }
    atomic_write_json(job / 'config.json', config)
    atomic_write_json(job / 'state.json', state)
    atomic_write_json(job / 'article.json', {
        'url': canonical,
        'title': extracted['title'],
        'chars': extracted['chars'],
        'execute': bool(execute),
        'stub_tts': not bool(execute),
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })
    return {
        'job_id': ident,
        'url': canonical,
        'title': extracted['title'],
        'chars': extracted['chars'],
        'execute': bool(execute),
        'stub_tts': not bool(execute),
        'path': str(job),
    }


def is_article_request(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get('content_type') == 'article':
        return True
    url = record.get('url')
    return isinstance(url, str) and bool(normalize_article_url(url)) and record.get('force_article') is True
