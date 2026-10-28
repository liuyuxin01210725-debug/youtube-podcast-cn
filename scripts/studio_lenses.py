"""Theme-primary discover lenses for private-listen.

People/shows hang under a lens. Data lives in data/lenses.json and must cover
every confirm-v2 daily_scan show_id exactly once.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LENSES = ROOT / 'data' / 'lenses.json'
DEFAULT_SHOWLIST = ROOT / 'data' / 'showlist-locked-v2.json'


def load_json(path: Path | None = None) -> dict:
    target = Path(path) if path else DEFAULT_LENSES
    data = json.loads(target.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('lenses'), list):
        raise ValueError('lenses.json 格式无效')
    return data


def load_showlist(path: Path | None = None) -> dict:
    target = Path(path) if path else DEFAULT_SHOWLIST
    data = json.loads(target.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('daily_scan'), list):
        raise ValueError('showlist 格式无效')
    return data


def lenses_ordered(data: dict | None = None) -> list[dict]:
    payload = data if data is not None else load_json()
    lenses = [item for item in payload['lenses'] if isinstance(item, dict) and item.get('id')]
    return sorted(lenses, key=lambda item: (item.get('order', 999), item['id']))


def shows_for_lens(lens_id: str, data: dict | None = None) -> list[dict]:
    for lens in lenses_ordered(data):
        if lens['id'] == lens_id:
            shows = lens.get('shows') or []
            return [show for show in shows if isinstance(show, dict)]
    raise KeyError(lens_id)


def lens_for_show(show_id: int, data: dict | None = None) -> dict | None:
    for lens in lenses_ordered(data):
        for show in lens.get('shows') or []:
            if isinstance(show, dict) and show.get('show_id') == show_id:
                return {'lens_id': lens['id'], 'label_zh': lens.get('label_zh'), 'show': show}
    return None


def people_chips(lens_id: str, data: dict | None = None) -> list[str]:
    """People labels nested under a lens — never top-level peers."""
    names: list[str] = []
    seen: set[str] = set()
    for show in shows_for_lens(lens_id, data):
        for person in show.get('people') or []:
            if not isinstance(person, str):
                continue
            label = person.strip()
            if not label or label in seen:
                continue
            seen.add(label)
            names.append(label)
        name = show.get('name')
        if isinstance(name, str) and name.strip() and name.strip() not in seen:
            # Show chip accompanies people; keep show name if no people listed.
            if not (show.get('people') or []):
                seen.add(name.strip())
                names.append(name.strip())
    return names


def mapped_show_ids(data: dict | None = None) -> list[int]:
    ids: list[int] = []
    for lens in lenses_ordered(data):
        for show in lens.get('shows') or []:
            if isinstance(show, dict) and isinstance(show.get('show_id'), int):
                ids.append(show['show_id'])
    return ids


def validate_against_showlist(lenses: dict | None = None, showlist: dict | None = None) -> list[str]:
    """Return human-readable problems. Empty list means confirm-v2 coverage is clean."""
    lens_data = lenses if lenses is not None else load_json()
    shows = showlist if showlist is not None else load_showlist()
    problems: list[str] = []
    daily = [item for item in shows.get('daily_scan') or [] if isinstance(item, dict)]
    expected = {item['id'] for item in daily if isinstance(item.get('id'), int)}
    mapped = mapped_show_ids(lens_data)
    if len(mapped) != len(set(mapped)):
        problems.append('透镜内 show_id 重复')
    missing = sorted(expected - set(mapped))
    extra = sorted(set(mapped) - expected)
    if missing:
        problems.append(f'未映射日扫源: {missing}')
    if extra:
        problems.append(f'透镜多出未知 show_id: {extra}')
    labels = [lens.get('label_zh') for lens in lenses_ordered(lens_data)]
    if len(labels) != len(set(labels)):
        problems.append('透镜中文标签重复')
    return problems
