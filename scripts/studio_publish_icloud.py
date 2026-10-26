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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

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


def _translation_client(provider: str):
    """Reuse the studio's OpenAI-compatible translator when a key is configured."""
    try:
        from podcast_cn import create_translation_client, get_provider_config
        config = get_provider_config(provider)
    except Exception:
        return None
    key = os.getenv(config.api_key_env, '').strip()
    if not key and sys.platform == 'darwin' and shutil.which('security'):
        try:
            result = subprocess.run(
                ['security', 'find-generic-password', '-a', os.getenv('USER', ''), '-s', config.keychain_service, '-w'],
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


def translate_episode_topic(topic: str, *, provider: str = 'deepseek', client=None) -> str | None:
    """Chinese topic for one episode, or None when no translator is configured.

    This is the hook for the studio translation API. Show names are not
    passed here; `compose_display_title` appends the original channel.
    Without `DEEPSEEK_API_KEY` or `DASHSCOPE_API_KEY` (or the macOS keychain
    item the studio already uses) this returns None and publish keeps a
    non-URL fallback.
    """
    text = (topic or '').strip()
    if not text or looks_like_url_title(text):
        return None
    if provider not in _PROVIDERS:
        provider = 'deepseek'
    if client is None:
        client = _translation_client(provider)
    if client is None:
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
    title = episode_title(job, job_id)
    if looks_like_url_title(title):
        title = job_id
    record = {
        'id': job_id,
        'title': title,
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
    parser.add_argument('--refresh-title', action='store_true', help='发布前拉取视频标题并译成「中文主题 · 节目名」，不重新合成音频')
    args = parser.parse_args(argv)
    job = args.job_dir.expanduser()
    try:
        if args.refresh_title and job.is_dir():
            ensure_publishable_title(job, fetch=True, allow_ytdlp=True, force_translate=True)
            status = _metadata_dict(job).get('title_status')
            if status == 'untranslated':
                print('私人听：没有可用的翻译接口，标题暂用英文主题和原节目名，不是链接。配置 DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY 后可再执行 --refresh-title。', file=sys.stderr)
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
