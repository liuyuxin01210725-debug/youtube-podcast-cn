"""Public HTTPS checks for shared links. No network."""
from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_TRACKING = {
    'fbclid',
    'gclid',
    'igshid',
    'mc_cid',
    'mc_eid',
    'ref',
    'ref_src',
    'si',
    'spm',
}


def is_public_https(value: str) -> bool:
    """True for a normal public https URL. Rejects localhost, private IPs, and credentials."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > 2000 or any(ch.isspace() for ch in text):
        return False
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or '').rstrip('.').casefold()
    if not host or host in {'localhost', 'localhost.localdomain'} or host.endswith('.local'):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return True


def canonical_url(value: str) -> str | None:
    """One form per episode. YouTube collapses to a watch URL; tracking params drop."""
    if not is_public_https(value):
        return None
    from studio_publish_icloud import youtube_id_from_url

    video_id = youtube_id_from_url(value.strip())
    if video_id:
        return f'https://www.youtube.com/watch?v={video_id}'
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or '').rstrip('.').casefold()
    port = parsed.port
    netloc = host if port in (None, 443) else f'{host}:{port}'
    path = parsed.path or '/'
    if path != '/' and path.endswith('/'):
        path = path.rstrip('/')
    kept = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith('utm_') or lowered in _TRACKING:
            continue
        kept.append((key, item))
    return urlunsplit(('https', netloc, path, urlencode(kept), ''))


def web_label(url: str) -> str:
    """Short studio title that is not itself a link."""
    parsed = urlsplit(url)
    host = (parsed.hostname or '链接').rstrip('.').casefold()
    tail = unquote(parsed.path.rstrip('/').rsplit('/', 1)[-1]).replace('-', ' ').replace('_', ' ')
    tail = ' '.join(tail.split())
    if tail and '://' not in tail and len(tail) > 1:
        return f'{tail} · {host}'[:200]
    return host[:200]
