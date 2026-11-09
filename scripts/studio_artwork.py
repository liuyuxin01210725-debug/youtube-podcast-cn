"""Episode cover next to output.mp3. Failures stay inside this module."""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import zlib
from pathlib import Path

from studio_urls import is_public_https

_PROVIDED = (
    'cover.jpg',
    'cover.jpeg',
    'cover.png',
    'artwork.jpg',
    'artwork.jpeg',
    'artwork.png',
    'artwork.webp',
    'cover.webp',
)
_FONTS = (
    Path('/System/Library/Fonts/PingFang.ttc'),
    Path('/System/Library/Fonts/STHeiti Light.ttc'),
    Path('/System/Library/Fonts/Supplemental/Songti.ttc'),
    Path('/System/Library/Fonts/Hiragino Sans GB.ttc'),
    Path('/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'),
    Path('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc'),
    Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
    Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
)


def _sniff(data: bytes) -> str | None:
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if len(data) >= 12 and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    return None


def _write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(f'.{dest.name}.{os.getpid()}.partial')
    try:
        temporary.write_bytes(data)
        os.replace(temporary, dest)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png(width: int, height: int, rgb: bytes, title: str) -> bytes:
    raw = b''.join(b'\x00' + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    text = title.encode('utf-8')[:200]
    itxt = b'Title\x00\x00\x00\x00\x00' + text
    return b''.join((
        b'\x89PNG\r\n\x1a\n',
        _chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)),
        _chunk(b'iTXt', itxt),
        _chunk(b'IDAT', zlib.compress(raw, 9)),
        _chunk(b'IEND', b''),
    ))


def _color(title: str) -> tuple[int, int, int]:
    digest = hashlib.sha256(title.encode('utf-8')).digest()
    return 24 + digest[0] % 36, 36 + digest[1] % 48, 48 + digest[2] % 56


def solid_placeholder_png(title: str, size: int = 64) -> bytes:
    """Small offline card. The title is stored in the PNG so it is not a blank file."""
    label = (title or '私人听').strip()[:200] or '私人听'
    red, green, blue = _color(label)
    pixel = bytes((red, green, blue))
    return _png(size, size, pixel * (size * size), label)


def _cjk_font(path: Path) -> bool:
    name = path.name.casefold()
    return any(token in name for token in ('pingfang', 'heiti', 'songti', 'hiragino', 'noto', 'cjk', 'wqy', 'droid', 'fallback', 'sourcehan'))


def _contains_cjk(text: str) -> bool:
    return any('\u4e00' <= char <= '\u9fff' for char in text)


def _find_font(title: str) -> Path | None:
    for path in _FONTS:
        if not path.is_file():
            continue
        if _contains_cjk(title) and not _cjk_font(path):
            continue
        return path
    return None


def _wrap(title: str) -> str:
    text = ' '.join((title or '私人听').split()) or '私人听'
    lines = []
    while text and len(lines) < 4:
        lines.append(text[:14])
        text = text[14:]
    if text:
        lines[-1] = lines[-1][:13] + '…'
    return '\n'.join(lines)


def _filter_path(path: Path) -> str:
    return str(path).replace('\\', '\\\\').replace(':', '\\:').replace("'", r"\'")


def render_title_png(title: str, dest: Path) -> bool:
    """Draw the title with ffmpeg when a font exists. False leaves the caller a solid card."""
    font = _find_font(title)
    ffmpeg = shutil.which('ffmpeg')
    if font is None or not ffmpeg:
        return False
    red, green, blue = _color(title)
    color = f'0x{red:02x}{green:02x}{blue:02x}'
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='cover-') as folder:
            text_file = Path(folder) / 'title.txt'
            text_file.write_text(_wrap(title), encoding='utf-8')
            staged = Path(folder) / 'cover.png'
            draw = (
                f"drawtext=fontfile='{_filter_path(font)}':textfile='{_filter_path(text_file)}'"
                f":fontsize=42:fontcolor=white:line_spacing=14:x=(w-text_w)/2:y=(h-text_h)/2"
            )
            result = subprocess.run(
                [
                    ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', f'color=c={color}:s=640x640',
                    '-vf', draw,
                    '-frames:v', '1',
                    str(staged),
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0 or not staged.is_file():
                return False
            data = staged.read_bytes()
    except (OSError, subprocess.TimeoutExpired):
        return False
    if _sniff(data) != 'png':
        return False
    _write_bytes(dest, data)
    return True


def download_image(url: str, timeout: float = 12) -> bytes | None:
    if not is_public_https(url) or len(url) > 800:
        return None
    request = urllib.request.Request(
        url,
        headers={'User-Agent': 'podcast-studio-private-listen', 'Accept': 'image/*'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            chunks = []
            total = 0
            while total <= 6_000_000:
                block = response.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                chunks.append(block)
    except (OSError, ValueError):
        return None
    if total > 6_000_000:
        return None
    data = b''.join(chunks)
    if _sniff(data) is None or len(data) < 32:
        return None
    return data


def _provided(job: Path) -> Path | None:
    for name in _PROVIDED:
        path = job / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
            head = path.read_bytes()[:16]
        except OSError:
            continue
        if 32 <= size <= 8_000_000 and _sniff(head):
            return path
    return None


def _ext_for(kind: str) -> str:
    if kind == 'png':
        return 'png'
    if kind == 'webp':
        return 'webp'
    return 'jpg'


def attach_cover(job: Path, episode_dir: Path, title: str, thumbnail_url: str | None = None) -> dict | None:
    """Write cover.jpg/png/webp. Never raises; the episode still publishes without a cover."""
    try:
        return _attach(Path(job), Path(episode_dir), title, thumbnail_url)
    except Exception:
        return None


def _attach(job: Path, episode_dir: Path, title: str, thumbnail_url: str | None) -> dict | None:
    provided = _provided(job)
    if provided is not None:
        data = provided.read_bytes()
        kind = _sniff(data)
        if kind and len(data) >= 32:
            name = f'cover.{_ext_for(kind)}'
            _write_bytes(episode_dir / name, data)
            return {'file': name, 'kind': 'provided'}
    if isinstance(thumbnail_url, str):
        data = download_image(thumbnail_url)
        sniffed = _sniff(data or b'')
        if data and sniffed:
            name = f'cover.{_ext_for(sniffed)}'
            _write_bytes(episode_dir / name, data)
            return {'file': name, 'kind': 'thumbnail'}
    label = (title or '').strip() or job.name
    titled = episode_dir / 'cover.png'
    if render_title_png(label, titled):
        return {'file': 'cover.png', 'kind': 'title'}
    _write_bytes(episode_dir / 'cover.png', solid_placeholder_png(label))
    return {'file': 'cover.png', 'kind': 'placeholder'}
