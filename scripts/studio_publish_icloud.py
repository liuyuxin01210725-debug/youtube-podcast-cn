#!/usr/bin/env python3
"""Copy a finished studio episode into the personal iCloud Drive library.

The iPhone app reads the same folder. Preview renders are not published.
Failures are the caller's problem to swallow: this module raises PublishError
and does not know about studio exit codes.
"""
from __future__ import annotations

import argparse
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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import studio_original_pending as original_pending
from studio_document import check_source_coverage
from studio_recovery import _sync_dir, decode_json_object
from studio_speakers import validate_speaker_voices
from temporary_audio import sha256_file
from studio_urls import is_public_https

LIBRARY_NAME = '中文播客私人库'
AUDIO_NAME = 'output.mp3'
RENDER_FILES = ('document.json', 'source-identity.json', 'render-identity.json')
_JOB_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_YOUTUBE_ID = re.compile(r'^[A-Za-z0-9_-]{6,32}$')
_YOUTUBE_HOSTS = {
    'youtube.com',
    'www.youtube.com',
    'm.youtube.com',
    'music.youtube.com',
}
# Phone title: Chinese topic, then the original show name. Brands are not translated.
DISPLAY_SEPARATOR = ' · '
_PROVIDERS = ('deepseek', 'qwen')


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
    except (OSError, ValueError, RecursionError):
        return None


def atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def looks_like_url_title(value: str) -> bool:
    """True when the whole string is a link, not a sentence that mentions one."""
    text = value.strip()
    if not text:
        return False
    if youtube_id_from_url(text):
        return True
    lowered = text.casefold()
    if lowered.startswith(('http://', 'https://', '//')):
        return True
    if '://' in lowered and ' ' not in text:
        return True
    if re.match(r'^(?:www\.|m\.|music\.)?youtube\.com/\S', lowered):
        return True
    if lowered.startswith('youtu.be/'):
        return True
    if lowered.startswith('www.') and ' ' not in text:
        return True
    return False


def human_title(value, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or looks_like_url_title(text):
        return None
    return text[:limit]


def contains_cjk(text: str) -> bool:
    return any('\u4e00' <= char <= '\u9fff' for char in text)


def _clean_show(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or looks_like_url_title(text):
        return None
    return text[:200]


def topic_without_show(title: str, channel: str | None) -> str:
    """Drop a trailing or leading show name so it can be appended unchanged."""
    topic = title.strip()
    show = (channel or '').strip()
    if not show:
        return topic
    escaped = re.escape(show)
    patterns = (
        rf'\s*(?:[\|\-–—:：]|·)\s*{escaped}\s*$',
        rf'^{escaped}\s*(?:[\|\-–—:：]|·)\s*',
        rf'\s*[（(]\s*{escaped}\s*[）)]\s*$',
    )
    for pattern in patterns:
        updated = re.sub(pattern, '', topic, flags=re.IGNORECASE).strip()
        if updated and updated != topic:
            return updated
    return topic


def compose_display_title(topic: str, channel: str | None) -> str:
    """`中文主题 · Huberman`. The show name stays in its original spelling."""
    text = topic.strip()
    show = _clean_show(channel)
    if not show or show.casefold() in text.casefold():
        return text[:500]
    suffix = f'{DISPLAY_SEPARATOR}{show}'
    if len(text) + len(suffix) <= 500:
        return text + suffix
    room = 500 - len(suffix)
    if room <= 0:
        return show[:500]
    return text[:room].rstrip() + suffix


def _metadata_dict(job: Path) -> dict:
    data = read_json(job / 'metadata.json')
    return dict(data) if isinstance(data, dict) else {}


def _state_dict(job: Path) -> dict:
    data = read_json(job / 'state.json')
    return dict(data) if isinstance(data, dict) else {}


def episode_title(job: Path, job_id: str) -> str:
    """Title written into the phone library. Never a URL.

    Preference: Chinese display_title, then any Chinese candidate, then the
    English source topic with the original show name, then channel plus
    video id, then the job id. A stored https link is not a candidate.
    """
    metadata = _metadata_dict(job)
    state = _state_dict(job)
    show = _clean_show(metadata.get('channel') or metadata.get('uploader'))
    video_id = episode_youtube_id(job)
    display = human_title(metadata.get('display_title'))
    if display and contains_cjk(display):
        return display
    chinese: list[str] = []
    other: list[str] = []
    for candidate in (metadata.get('source_title'), metadata.get('title'), state.get('title')):
        text = human_title(candidate)
        if not text:
            continue
        if contains_cjk(text):
            chinese.append(text)
        else:
            other.append(text)
    if chinese:
        return compose_display_title(topic_without_show(chinese[0], show), show)
    if other:
        return compose_display_title(topic_without_show(other[0], show), show)
    if show and video_id:
        return compose_display_title(show, video_id)
    if video_id:
        return video_id
    if display:
        return display
    return job_id


def _should_replace_state_title(current, new_title: str, *, video_id: str | None, job_id: str) -> bool:
    if not new_title:
        return False
    if not isinstance(current, str) or not current.strip():
        return True
    text = current.strip()
    if looks_like_url_title(text) or text == job_id or (video_id and text == video_id):
        return True
    return contains_cjk(new_title) and not contains_cjk(text)


def translate_episode_topic(topic: str, *, provider: str = 'deepseek', client=None) -> str | None:
    """Translate a title only when the caller supplies a scoped client.

    Metadata lookup and publication have a local title fallback. They do
    not acquire translation credentials or a paid allowance on their own.
    """
    text = (topic or '').strip()
    if not text or looks_like_url_title(text) or client is None:
        return None
    try:
        translated = client.translate_title(text)
    except Exception:
        return None
    cleaned = human_title(translated)
    if not cleaned or not contains_cjk(cleaned):
        return None
    return cleaned


def _accept_translator_result(result) -> str | None:
    if not isinstance(result, str):
        return None
    cleaned = human_title(result)
    if not cleaned or not contains_cjk(cleaned):
        return None
    return cleaned


def remember_source_title(
    job: Path,
    *,
    source_title: str | None = None,
    video_id: str | None = None,
    channel: str | None = None,
    webpage_url: str | None = None,
    thumbnail_url: str | None = None,
    provider: str = 'deepseek',
    translator=None,
    force_translate: bool = False,
) -> str:
    """Store the English source title and a Chinese display title when we can.

    `title_status` is `translated` or `source_zh` when the phone title is
    Chinese, and `untranslated` when no translation API was available. The
    untranslated fallback is still the English topic plus the original show
    name, never the watch URL.
    """
    job = Path(job)
    metadata = _metadata_dict(job)
    if provider not in _PROVIDERS:
        provider = 'deepseek'
    incoming = human_title(source_title)
    if incoming:
        metadata['source_title'] = incoming
    elif not human_title(metadata.get('source_title')):
        metadata.pop('source_title', None)
    found_id = _valid_youtube_id(video_id) or youtube_id_from_url(str(webpage_url or ''))
    if not found_id:
        found_id = _valid_youtube_id(metadata.get('video_id')) or episode_youtube_id(job)
    if found_id:
        metadata['video_id'] = found_id
    show = _clean_show(channel)
    if show:
        metadata['channel'] = show
    elif not _clean_show(metadata.get('channel')):
        metadata.pop('channel', None)
    show = _clean_show(metadata.get('channel'))
    if isinstance(webpage_url, str) and webpage_url.strip():
        metadata['webpage_url'] = webpage_url.strip()[:500]
    if isinstance(thumbnail_url, str):
        cleaned_thumb = thumbnail_url.strip()
        if is_public_https(cleaned_thumb) and len(cleaned_thumb) <= 800:
            metadata['thumbnail_url'] = cleaned_thumb
    if isinstance(metadata.get('title'), str) and looks_like_url_title(metadata['title']):
        metadata.pop('title', None)

    source = human_title(metadata.get('source_title'))
    existing_display = human_title(metadata.get('display_title'))
    if existing_display and not contains_cjk(existing_display):
        existing_display = None
        metadata.pop('display_title', None)
    display = None
    if source:
        topic = topic_without_show(source, show)
        translated = None
        status = 'untranslated'
        if topic and contains_cjk(topic):
            translated = human_title(topic)
            status = 'source_zh'
        elif topic and not looks_like_url_title(topic) and (force_translate or not existing_display):
            try:
                raw = translator(topic) if translator is not None else translate_episode_topic(topic, provider=provider)
            except Exception:
                raw = None
            translated = _accept_translator_result(raw)
            status = 'translated' if translated else 'untranslated'
        elif existing_display:
            translated = None
            status = 'translated'
        if translated:
            display = compose_display_title(translated, show)
            metadata['display_title'] = display
            metadata['title_zh'] = translated
            metadata['title_status'] = status
            metadata['title'] = display
        elif existing_display and contains_cjk(existing_display):
            display = existing_display
            metadata['display_title'] = existing_display
            metadata['title_status'] = metadata.get('title_status') or 'translated'
            metadata['title'] = existing_display
        else:
            metadata.pop('display_title', None)
            metadata.pop('title_zh', None)
            metadata['title_status'] = 'untranslated'
            fallback = compose_display_title(topic_without_show(source, show), show)
            if fallback and not looks_like_url_title(fallback):
                metadata['title'] = fallback
    elif existing_display:
        display = existing_display
    if metadata or (job / 'metadata.json').is_file():
        atomic_write_json(job / 'metadata.json', metadata)

    state = _state_dict(job)
    published = display or episode_title(job, job.name)
    # A brand-new folder with no label yet should not gain a state file that only repeats the job id.
    has_label = bool(state) or published != job.name
    if published and not looks_like_url_title(published) and has_label and _should_replace_state_title(state.get('title'), published, video_id=found_id, job_id=job.name):
        state['title'] = published
        atomic_write_json(job / 'state.json', state)
    return episode_title(job, job.name)


def _oembed_metadata(video_id: str, timeout: float) -> dict | None:
    watch = f'https://www.youtube.com/watch?v={video_id}'
    query = urlencode({'url': watch, 'format': 'json'})
    request = urllib.request.Request(
        f'https://www.youtube.com/oembed?{query}',
        headers={'Accept': 'application/json', 'User-Agent': 'podcast-studio-private-listen'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode('utf-8', errors='replace'))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    result = {'video_id': video_id, 'webpage_url': watch}
    title = human_title(payload.get('title'))
    if title:
        result['source_title'] = title
    channel = _clean_show(payload.get('author_name'))
    if channel:
        result['channel'] = channel
    thumbnail = payload.get('thumbnail_url')
    if isinstance(thumbnail, str) and is_public_https(thumbnail.strip()) and len(thumbnail.strip()) <= 800:
        result['thumbnail_url'] = thumbnail.strip()
    return result


def _ytdlp_metadata(url: str, timeout: float) -> dict | None:
    command = [sys.executable, '-m', 'yt_dlp', '--dump-single-json', '--skip-download', '--no-playlist', '--no-warnings', url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        info = json.loads(result.stdout)
        from podcast_cn import video_metadata_from_info
        metadata = video_metadata_from_info(info)
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
    except Exception:
        return None
    title = human_title(metadata.source_title)
    if not title:
        return None
    found = {
        'source_title': title,
        'video_id': _valid_youtube_id(metadata.video_id),
        'webpage_url': metadata.webpage_url or url,
    }
    channel = _clean_show(metadata.channel)
    if channel:
        found['channel'] = channel
    return {key: value for key, value in found.items() if value}


def fetch_youtube_metadata(url: str, *, allow_ytdlp: bool = False, timeout: float = 8) -> dict | None:
    """English title, channel, and video id. Never returns the URL as a title."""
    video_id = youtube_id_from_url(url)
    if not video_id:
        return None
    oembed = _oembed_metadata(video_id, timeout) or {'video_id': video_id}
    if human_title(oembed.get('source_title')) or not allow_ytdlp:
        return oembed
    dumped = _ytdlp_metadata(url, max(timeout, 25))
    if not dumped:
        return oembed
    merged = dict(oembed)
    for key, value in dumped.items():
        if value:
            merged[key] = value
    return merged


def _job_source_url(job: Path) -> str:
    for name in ('config.json', 'source-identity.json'):
        source = _source_dict(job, name)
        if not source:
            continue
        value = source.get('value')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _job_provider(job: Path) -> str:
    data = read_json(job / 'config.json')
    if isinstance(data, dict) and data.get('provider') in _PROVIDERS:
        return data['provider']
    return 'deepseek'


def ensure_publishable_title(job: Path, *, translator=None, fetch: bool = True, allow_ytdlp: bool = False, force_translate: bool = False) -> str:
    """Fill source_title / display_title before a publish. Does not render audio."""
    job = Path(job)
    job_id = job.name
    if not force_translate:
        current = episode_title(job, job_id)
        if contains_cjk(current) and not looks_like_url_title(current):
            return current
    fetched: dict = {}
    if fetch and (force_translate or not human_title(_metadata_dict(job).get('source_title'))):
        url = _job_source_url(job)
        if youtube_id_from_url(url):
            try:
                fetched = fetch_youtube_metadata(url, allow_ytdlp=allow_ytdlp, timeout=8 if allow_ytdlp else 5) or {}
            except Exception:
                fetched = {}
    remember_source_title(
        job,
        source_title=fetched.get('source_title'),
        video_id=fetched.get('video_id'),
        channel=fetched.get('channel'),
        webpage_url=fetched.get('webpage_url') or (_job_source_url(job) or None),
        thumbnail_url=fetched.get('thumbnail_url'),
        provider=_job_provider(job),
        translator=translator,
        force_translate=force_translate,
    )
    title = episode_title(job, job_id)
    if looks_like_url_title(title):
        return job_id
    return title


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
    if value < 0 or not math.isfinite(value):
        return None
    return round(value, 3)


def _pending_episode_record(episode_dir: Path) -> dict | None:
    """Check the pending projection and its sidecars without reading media bodies."""
    names = ('episode.json', original_pending.MANIFEST, AUDIO_NAME, original_pending.TRANSCRIPT,
             original_pending.AUDIO, original_pending.RAW)
    try:
        for name in names:
            info = (episode_dir / name).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_size:
                return None
        episode_bytes = original_pending.read_bytes(episode_dir / 'episode.json')
        data = decode_json_object(episode_bytes.decode('utf-8'))
        manifest = decode_json_object(original_pending.read_bytes(
            episode_dir / original_pending.MANIFEST).decode('utf-8'))
        if (
            data['id'] == manifest['episode_id'] == original_pending.EPISODE_ID
            and data['status'] == manifest['status'] == 'pending_review'
            and data['audio_kind'] == 'original_audio'
            and data['transcript_status'] == 'asr_unreviewed'
            and data['language'] == 'zh'
            and data['source_url'] == original_pending.EPISODE_URL
            and isinstance(data['title'], str) and data['title'].strip()
            and isinstance(data['created_at'], str)
            and (data['file'], data['manifest'], data['transcript'])
                == (AUDIO_NAME, original_pending.MANIFEST, original_pending.TRANSCRIPT)
            and manifest['sha256']['episode.json'] == hashlib.sha256(episode_bytes).hexdigest()
            and not original_pending.PENDING_JOB_FIELDS.intersection(data)
        ):
            json.dumps(data, allow_nan=False, ensure_ascii=False).encode('utf-8')
            return data
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    return None


def _episode_record(episode_dir: Path, previous: dict) -> dict | None:
    ident = episode_dir.name
    if not _JOB_ID.fullmatch(ident):
        return None
    if ident == original_pending.EPISODE_ID:
        record = _pending_episode_record(episode_dir)
        if record is None:
            return None
        extensions = {key: value for key, value in previous.items()
                      if key not in original_pending.PENDING_JOB_FIELDS}
        return {**extensions, **record}
    data = read_json(episode_dir / 'episode.json')
    if not isinstance(data, dict) or data.get('status') == 'pending_review':
        return None
    try:
        json.dumps(data, allow_nan=False, ensure_ascii=False).encode('utf-8')
    except (ValueError, RecursionError):
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
    artwork = data.get('artwork')
    kind = data.get('artwork_kind')
    if isinstance(artwork, str) and artwork in ('cover.jpg', 'cover.png', 'cover.webp') and kind in ('provided', 'thumbnail', 'title', 'placeholder'):
        record['artwork'] = artwork
        record['artwork_kind'] = kind
    # Keep catalog annotations when the episode file also supplies an extension.
    disk_fields = {'id', 'title', 'created_at', 'duration_sec', 'source_job_id', 'file',
                   'youtube_id', 'artwork', 'artwork_kind'}
    extensions = {key: value for key, value in data.items() if key not in disk_fields}
    return {**extensions, **previous, **record}


def decode_library_index(text: str) -> tuple[dict, dict[str, dict]]:
    prior = decode_json_object(text)
    # Check all values, including extension fields, before preserving them.
    json.dumps(prior, allow_nan=False, ensure_ascii=False).encode('utf-8')
    if not isinstance(prior.get('episodes'), list):
        raise ValueError
    prior_by_id = {}
    for item in prior['episodes']:
        if not isinstance(item, dict):
            raise ValueError
        # An explicit episode ID takes precedence over the source job alias.
        ident = next((item[key] for key in ('id', 'episode_id', 'source_job_id')
                      if item.get(key) is not None), None)
        if not isinstance(ident, str) or not _JOB_ID.fullmatch(ident) or ident in prior_by_id:
            raise ValueError
        prior_by_id[ident] = item
    return prior, prior_by_id


def _read_library_index(listing: Path) -> tuple[dict, dict[str, dict]]:
    try:
        mode = listing.lstat().st_mode
    except FileNotFoundError:
        return {}, {}
    except OSError:
        raise PublishError('无法读取现有资料库索引') from None

    try:
        if not stat.S_ISREG(mode):
            raise ValueError
        return decode_library_index(listing.read_text(encoding='utf-8'))
    except OSError:
        raise PublishError('无法读取现有资料库索引') from None
    except (ValueError, RecursionError):
        raise PublishError('现有资料库索引格式无效') from None


def rebuild_library(library: Path) -> dict:
    library.mkdir(parents=True, exist_ok=True)
    with original_pending.library_lock(library):
        return _rebuild_library_locked(library)


def _rebuild_library_locked(library: Path) -> dict:
    listing = library / 'library.json'
    prior, prior_by_id = _read_library_index(listing)
    root = library / 'episodes'
    pending_id = original_pending.EPISODE_ID
    pending_dir = root / pending_id
    # During a pending-package rebuild, neighboring rows can still be in transit.
    keep_neighbors = pending_id in prior_by_id or (not pending_dir.is_symlink() and pending_dir.is_dir())
    records = prior_by_id.copy() if keep_neighbors else {
        ident: row for ident, row in prior_by_id.items() if 'media_revision' in row
    }
    records.pop(pending_id, None)
    if root.is_dir():
        for episode_dir in root.iterdir():
            if not episode_dir.is_dir() or episode_dir.is_symlink():
                continue
            ident = episode_dir.name
            if 'media_revision' in prior_by_id.get(ident, {}):
                continue
            record = _episode_record(episode_dir, prior_by_id.get(ident, {}))
            if record is not None:
                records[ident] = record
    return _write_snapshot(listing, prior, records)


def _write_snapshot(listing, prior, records):
    ordered = sorted(records.items(), key=lambda entry: (
        entry[1]['created_at'] if isinstance(entry[1].get('created_at'), str) else '', entry[0],
    ), reverse=True)
    payload = {
        **prior,
        'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'episodes': [record for _, record in ordered],
    }
    atomic_write_json(listing, payload)
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


def _validate_render(folder: Path, config: dict, duration_sec: float | None) -> str:
    try:
        document, identity, receipt = [
            decode_json_object(original_pending.read_bytes(folder / name)) for name in RENDER_FILES
        ]
        overflow = check_source_coverage(document, identity)
        if overflow:
            raise PublishError('译稿有段落超出锁定音源时长，禁止发布')
        validate_speaker_voices(config, document)
        if any(row.get('ambiguous') for row in document['segments']):
            raise PublishError('说话人仍待逐段核对，禁止发布')
        document_sha = original_pending.digest(json.dumps(
            document, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())
        if (type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
                or type(receipt['sample_rate']) is not int or receipt['sample_rate'] != 24000
                or receipt['document_sha256'] != document_sha
                or receipt['source_identity_sha256'] != sha256_file(folder / 'source-identity.json')
                or receipt['audio_sha256'] != sha256_file(folder / AUDIO_NAME)):
            raise ValueError
        timeline = receipt['timeline']
        ids = [row['id'] for row in document['segments'] if not row.get('is_ad')]
        if not ids or len(set(ids)) != len(ids) or [row['id'] for row in timeline] != ids:
            raise ValueError
        frames = 0
        for row in timeline:
            if (type(row['start_frame']) is not int or type(row['end_frame']) is not int
                    or row['start_frame'] != frames or row['end_frame'] <= frames):
                raise ValueError
            frames = row['end_frame']
        # MP3 encoding adds frame padding to the PCM timeline.
        if duration_sec is None or not math.isfinite(duration_sec) or duration_sec <= 0 \
                or abs(duration_sec - frames / 24000) > 0.12:
            raise ValueError
        return document_sha
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        raise PublishError('新版音频、译稿或合成时轴无法核验') from None


def _validate_revision_audio(path):
    try:
        result = subprocess.run(
            ['ffmpeg', '-v', 'error', '-xerror', '-f', 'mp3', '-i', str(path),
             '-map', '0:a:0', '-f', 'null', '-'],
            capture_output=True, timeout=120, check=False,
        )
        if result.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise PublishError('新版 MP3 无法完整解码')


def _current_episode_dir(library, job_id, old_record):
    revision = old_record.get('media_revision')
    if 'media_revision' not in old_record:
        path = library / 'episodes' / job_id
    else:
        digest = revision.get('sha256') if isinstance(revision, dict) else None
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise PublishError('现有节目修订指针无效')
        path = library / 'revisions' / job_id / digest
        if revision.get('path') != path.relative_to(library).as_posix():
            raise PublishError('现有节目修订指针无效')
    if path.resolve() != path:
        raise PublishError('节目目录不能使用符号链接')
    return path


def _revision_files(folder):
    return {entry.name: original_pending.file_digest(entry) for entry in sorted(folder.iterdir())}


def _publish_revision(library, current, prior, records, record, stage, *, old_index):
    ident = record['id']
    files = _revision_files(stage)
    revision = original_pending.digest(original_pending.json_bytes(files))
    parent = library / 'revisions' / ident
    if parent.resolve() != parent:
        raise PublishError('修订目录不能使用符号链接')
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / revision
    if final.is_symlink() or (final.exists() and (not final.is_dir() or _revision_files(final) != files)):
        raise PublishError('已有修订对象与发布内容不一致')
    if not final.exists():
        for name in files:
            with original_pending.open_regular(stage / name) as asset:
                os.fsync(asset.fileno())
        _sync_dir(stage)
        stage.rename(final)
    for folder in (parent, parent.parent, library):
        _sync_dir(folder)
    listing = library / 'library.json'
    if (listing.read_bytes() if listing.exists() else None) != old_index:
        raise PublishError('资料库索引已变化，请重新核对再发布')
    old_record = records.get(ident, {})
    # Keep unrelated extensions. Listening marks are bound to the old media.
    inherited = {key: value for key, value in old_record.items()
                 if key not in ('resume_seconds', 'resume_sec', 'position_seconds', 'playback_progress',
                                'artwork', 'artwork_kind')}
    row = {**inherited, **record, 'media_revision': {
        'sha256': revision, 'path': final.relative_to(library).as_posix(), 'files': files,
        'previous_path': (old_record['media_revision']['previous_path']
                          if final == current else current.relative_to(library).as_posix()),
        'resume_policy': 'reset_unmapped',
    }}
    _write_snapshot(listing, prior, {**records, ident: row})
    return final, row



def _require_audio_preflight(src, job):
    """Apply the default audio checks and refuse BLOCK or REVIEW before merging."""
    from shengjian_audio_preflight import refuse_if_blocked_or_review, run_preflight, PreflightError
    report = run_preflight(src, output_dir=Path(job) / 'preflight')
    try:
        refuse_if_blocked_or_review(report)
    except PreflightError as error:
        raise PublishError(str(error)) from error
    return report


def _snapshot_library_or_refuse(library):
    from shengjian_library_snapshot import SnapshotError, snapshot
    dest = Path(tempfile.mkdtemp(prefix='.shengjian-pub-snap-', dir=str(Path(library).parent)))
    dest.rmdir()
    try:
        return snapshot(dest, library=library, label='publish_job')
    except SnapshotError as error:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise PublishError(f'备份失败，拒绝合并: {error}') from error


def _restore_library_snapshot(snap, library):
    if not snap:
        return
    from shengjian_library_snapshot import restore
    restore(snap, library=library, measure=True)


def _discard_library_snapshot(snap):
    if snap and Path(snap).exists():
        shutil.rmtree(snap, ignore_errors=True)


def publish_job(job_dir, library_root=None) -> dict:
    """Publish a staged render. A same-ID revision only switches the catalog."""
    job = Path(job_dir).expanduser().resolve()
    if not job.is_dir() or job.is_symlink():
        raise PublishError('任务目录不存在')
    job_id = job.name
    if not _JOB_ID.fullmatch(job_id):
        raise PublishError('任务编号无效')
    if job_id == original_pending.EPISODE_ID:
        raise PublishError('已选中文原音节目编号不能作为工作室任务发布')
    src = job / AUDIO_NAME
    if not src.is_file() or src.is_symlink():
        raise PublishError('任务目录里没有完整的 output.mp3')
    library = (Path(library_root).expanduser() if library_root else default_library_root()).resolve()
    try:
        library.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PublishError(f'无法创建 iCloud 资料库：{error}') from error
    _require_audio_preflight(src, job)
    try:
        with original_pending.library_lock(library):
            snap = _snapshot_library_or_refuse(library)
            try:
                episode_dir, record = _publish_job_locked(job, src, library, snap=snap)
            finally:
                _discard_library_snapshot(snap)
    except (OSError, original_pending.IntakeError) as error:
        raise PublishError(f'写入节目清单失败：{error}') from error
    try:
        record_status(job, ok=True, episode_dir=str(episode_dir), episode_id=job_id)
    except OSError:
        pass
    return {**record, 'episode_dir': str(episode_dir), 'library': str(library)}


def _publish_job_locked(job, src, library, *, snap):
    job_id = job.name
    listing = library / 'library.json'
    prior, records = _read_library_index(listing)
    old_index = listing.read_bytes() if listing.exists() else None
    legacy = library / 'episodes' / job_id
    current = _current_episode_dir(library, job_id, records.get(job_id, {}))
    existing = job_id in records or current.exists()
    has_render = any((job / name).exists() or (job / name).is_symlink() for name in RENDER_FILES)
    if existing and not has_render:
        if current != legacy or not (current / AUDIO_NAME).is_file() \
                or original_pending.file_digest(src) != original_pending.file_digest(current / AUDIO_NAME):
            raise PublishError('同编号新版需要可核验的音频、译稿与合成时轴')
    created_at = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
    title = episode_title(job, job_id)
    if looks_like_url_title(title):
        title = job_id
    record = {
        'id': job_id,
        'title': title,
        'created_at': created_at,
        'duration_sec': None,
        'source_job_id': job_id,
        'file': AUDIO_NAME,
    }
    youtube_id = episode_youtube_id(job)
    if youtube_id:
        record['youtube_id'] = youtube_id
    with tempfile.TemporaryDirectory(prefix='.render-', dir=library) as staging:
        stage = Path(staging)
        _copy_audio(src, stage)
        record['duration_sec'] = probe_duration_sec(stage / AUDIO_NAME)
        if has_render:
            for name in RENDER_FILES:
                original_pending.write_bytes(stage / name, original_pending.read_bytes(job / name))
            config = read_json(job / 'config.json')
            if (job / 'config.json').exists() and not isinstance(config, dict):
                raise PublishError('任务配置无效')
            document_sha = _validate_render(stage, config or {}, record['duration_sec'])
        _stage_cover(job, stage, record)
        if existing and has_render:
            _validate_revision_audio(stage / AUDIO_NAME)
            original_pending.file_digest(current / AUDIO_NAME)
            extract = current / 'extract.json'
            if extract.exists():
                from studio_extract import preserve_extract_on_republish

                original_pending.write_bytes(stage / extract.name, original_pending.read_bytes(extract))
                preserve_extract_on_republish(stage, new_transcript_hash=document_sha)
            atomic_write_json(stage / 'episode.json', record)
            return _publish_revision(library, current, prior, records, record, stage,
                                     old_index=old_index)
        atomic_write_json(stage / 'episode.json', record)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        if not existing:
            stage.rename(legacy)
        try:
            if existing:
                # Same audio; metadata and cover refresh keep the existing media.
                for entry in stage.iterdir():
                    if entry.name != AUDIO_NAME:
                        entry.replace(legacy / entry.name)
            if old_index is None:
                _rebuild_library_locked(library)
            else:
                row = _episode_record(legacy, records.get(job_id, {}))
                if row is None:
                    raise PublishError('发布的节目清单格式无效')
                _write_snapshot(listing, prior, {**records, job_id: row})
        except Exception:
            if existing:
                _restore_library_snapshot(snap, library)
            else:
                # The old catalog has not been replaced. Remove only this append's files.
                shutil.rmtree(legacy)
            raise
    return legacy, record


def _stage_cover(job, episode_dir, record):
    try:
        from studio_artwork import attach_cover
        thumbnail = _metadata_dict(job).get('thumbnail_url')
        cover = attach_cover(job, episode_dir, record['title'],
                             thumbnail_url=thumbnail if isinstance(thumbnail, str) else None)
    except Exception:
        cover = None
    if isinstance(cover, dict) and cover.get('file') in ('cover.jpg', 'cover.png', 'cover.webp') and cover.get('kind') in ('provided', 'thumbnail', 'title', 'placeholder'):
        record['artwork'] = cover['file']
        record['artwork_kind'] = cover['kind']


def publish_original_pending(package_dir, *, library_root, expected_catalog_sha256, confirm_episode):
    from studio_original_delivery import receive

    return receive(package_dir, library_root=library_root,
                   expected_catalog_sha256=expected_catalog_sha256, confirm_episode=confirm_episode)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'original-pending':
        from studio_original_delivery import receive_cli

        return receive_cli(argv[1:], publish_original_pending)
    parser = argparse.ArgumentParser(description='把已完成的工作室任务发布到 iCloud 私人听资料库')
    parser.add_argument('--job-dir', required=True, type=Path)
    parser.add_argument('--library', type=Path, default=None, help='覆盖资料库根目录，默认 iCloud Drive 里的「中文播客私人库」')
    parser.add_argument('--refresh-title', action='store_true', help='发布前刷新视频标题和节目名，不重新合成音频')
    parser.add_argument('--no-device', action='store_true', help='不往连着 Mac 的 iPhone 直接拷贝，只发布到 iCloud')
    args = parser.parse_args(argv)
    job = args.job_dir.expanduser()
    if job.resolve().name == original_pending.EPISODE_ID:
        print('私人听：发布失败：已选中文原音节目编号不能作为工作室任务发布', file=sys.stderr)
        return 1
    try:
        if args.refresh_title and job.is_dir():
            ensure_publishable_title(job, fetch=True, allow_ytdlp=True, force_translate=True)
            status = _metadata_dict(job).get('title_status')
            if status == 'untranslated':
                print('私人听：此操作没有标题翻译额度，标题暂用英文主题和原节目名。', file=sys.stderr)
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
    if not args.no_device:
        try:
            from studio_push_iphone import push_after_publish
            push_after_publish(result, job)
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
