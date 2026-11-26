#!/usr/bin/env python3
"""Shared sidecar validation, materialization, and point-listen verification.

Mirrors the iOS PrivateListen contracts (PlayerReading, PointListening,
ListenPicks, ExtractionModels, MockExtractor) so Mac publishing and backfill
never admit unverified or mismatched sidecars into objects/<sha>/.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone

TRANSCRIPT_FILE = 'transcript_zh.json'
POINT_LISTEN_FILE = 'point-listen.json'
EXTRACT_FILES = ('extract.json', 'extract.live.json')
SIDECAR_FILES = (TRANSCRIPT_FILE, POINT_LISTEN_FILE, *EXTRACT_FILES)
SAMPLE_RATE = 24000
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_HEX_CHARS = set('0123456789abcdef')
_MIN_PICK_COUNT = 2
_MIN_PICK_DURATION_SEC = 1.0


class SidecarError(ValueError):
    """Raised when a sidecar fails structural or binding validation."""


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _trimmed_len(text: object) -> int:
    if not isinstance(text, str):
        return 0
    return len(''.join(text.split()))


def _reject_constant(token: str) -> object:
    raise SidecarError(f'JSON 包含非法数值常量：{token}')


def decode_json_strict(raw_bytes: bytes) -> dict:
    """Decode strict UTF-8 JSON object without NaN/Infinity."""
    try:
        text = raw_bytes.decode('utf-8')
        data = json.loads(text, parse_constant=_reject_constant)
    except SidecarError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SidecarError(f'JSON 格式无效：{error}') from error
    if not isinstance(data, dict):
        raise SidecarError('JSON 顶层必须是对象')
    return data


def normalize_transcript_hash(value: object) -> str | None:
    """Mirror TranscriptFingerprint.normalized: strict 16 or 64 hex chars -> lower[:16]."""
    if not isinstance(value, str):
        return None
    lower = value.lower()
    if len(lower) not in (16, 64) or not all(ch in _HEX_CHARS for ch in lower):
        return None
    return lower[:16]


def compute_transcript_hash(text: str) -> str:
    """Compute 16-char hex transcript fingerprint matching studio_extract.transcript_hash."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def extract_transcript_lines(data: dict) -> list[dict]:
    """Mirror PlayerReading.Transcript.lines extraction rules."""
    segments = data.get('segments')
    trimmed_segments: list[dict] = []
    if isinstance(segments, list):
        for index, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            zh = seg.get('zh').strip() if isinstance(seg.get('zh'), str) else ''
            raw_text = seg.get('text').strip() if isinstance(seg.get('text'), str) else ''
            en = seg.get('en').strip() if isinstance(seg.get('en'), str) else ''
            valid_zh = zh or None
            valid_text = raw_text or None
            valid_en = en or None
            display = valid_zh if valid_zh is not None else valid_text
            if not display:
                continue
            if valid_en is not None and valid_en != display:
                original = valid_en
            elif valid_zh is not None and valid_text is not None and valid_text != display:
                original = valid_text
            else:
                original = None
            raw_start = seg.get('t_start')
            raw_end = seg.get('t_end')
            t_start = float(raw_start) if _is_finite_number(raw_start) else None
            t_end = float(raw_end) if _is_finite_number(raw_end) else None
            seg_id = seg.get('id')
            if isinstance(seg_id, str) and seg_id:
                line_id = seg_id
            elif isinstance(seg_id, int) and not isinstance(seg_id, bool):
                line_id = str(seg_id)
            else:
                line_id = f'line-{index}'
            trimmed_segments.append({
                'id': line_id,
                'display_text': display,
                'original_text': original,
                't_start': t_start,
                't_end': t_end,
            })
    if trimmed_segments:
        return trimmed_segments
    raw_text = data.get('text')
    if not isinstance(raw_text, str):
        return []
    lines: list[dict] = []
    for index, line in enumerate(raw_text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        lines.append({
            'id': f'line-{index}',
            'display_text': stripped,
            'original_text': None,
            't_start': None,
            't_end': None,
        })
    return lines


def validate_transcript_zh(raw_bytes: bytes, *, expected_audio_sha256: str | None = None) -> dict:
    """Validate transcript_zh.json against PlayerReading.Transcript and D4 rules."""
    if expected_audio_sha256 is not None and not _HEX64.fullmatch(expected_audio_sha256):
        raise SidecarError('预期音频 SHA-256 格式无效')
    data = decode_json_strict(raw_bytes)
    lines = extract_transcript_lines(data)
    if not lines or not any(line['display_text'].strip() for line in lines):
        raise SidecarError('中文译稿缺少可显示正文行')
    audio_sha = data.get('audio_sha256')
    if audio_sha is not None:
        if not isinstance(audio_sha, str) or not _HEX64.fullmatch(audio_sha):
            raise SidecarError('中文译稿 audio_sha256 格式无效')
        if expected_audio_sha256 is not None and audio_sha != expected_audio_sha256:
            raise SidecarError('中文译稿 audio_sha256 与当前成片不匹配')
    joined_text = '\n'.join(line['display_text'] for line in lines)
    th_64 = hashlib.sha256(joined_text.encode('utf-8')).hexdigest()
    timed_count = sum(
        1 for line in lines
        if line['t_start'] is not None and line['t_end'] is not None and line['t_end'] > line['t_start'] >= 0
    )
    return {
        'line_count': len(lines),
        'timed_line_count': timed_count,
        'transcript_hash': th_64[:16],
        'transcript_hash_sha256': th_64,
        'audio_sha256': audio_sha,
        'timeline': data.get('timeline'),
        'lines': lines,
        'data': data,
    }


def _check_render_identity_cross(
    document: dict,
    render_identity: dict | None,
    non_ad: list[dict],
    *,
    audio_sha256: str,
    duration_sec: float | None,
) -> dict[str, tuple[float, float]] | None:
    """Verify all 5 C.5 cross-checks for output.mp3.seconds timeline."""
    if not isinstance(render_identity, dict) or not render_identity:
        return None
    if type(render_identity.get('schema_version')) is not int or render_identity['schema_version'] != 1:
        return None
    if type(render_identity.get('sample_rate')) is not int or render_identity['sample_rate'] != SAMPLE_RATE:
        return None
    if render_identity.get('audio_sha256') != audio_sha256:
        return None
    doc_sha = hashlib.sha256(
        json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')
    ).hexdigest()
    if render_identity.get('document_sha256') != doc_sha:
        return None
    timeline = render_identity.get('timeline')
    if not isinstance(timeline, list) or not timeline:
        return None
    non_ad_ids = [seg.get('id') for seg in non_ad]
    if any(not isinstance(ident, str) or not ident for ident in non_ad_ids) or len(set(non_ad_ids)) != len(non_ad_ids):
        return None
    timeline_ids = [row.get('id') if isinstance(row, dict) else None for row in timeline]
    if timeline_ids != non_ad_ids:
        return None
    frames = 0
    mapping: dict[str, tuple[float, float]] = {}
    for row in timeline:
        start_f = row.get('start_frame')
        end_f = row.get('end_frame')
        if type(start_f) is not int or type(end_f) is not int:
            return None
        if start_f != frames or end_f <= start_f:
            return None
        frames = end_f
        mapping[row['id']] = (round(start_f / SAMPLE_RATE, 3), round(end_f / SAMPLE_RATE, 3))
    if not _is_finite_number(duration_sec) or float(duration_sec) <= 0:
        return None
    if abs(float(duration_sec) - frames / SAMPLE_RATE) > 0.12:
        return None
    return mapping


def materialize_transcript_zh_from_document(
    document: dict,
    render_identity: dict | None,
    *,
    audio_sha256: str,
    duration_sec: float | None,
    strict_all_non_ad: bool = True,
    generated_at: str | None = None,
) -> tuple[bytes, dict]:
    """Materialize transcript_zh.json from document.json and optional render-identity.json."""
    if not isinstance(document, dict) or not _HEX64.fullmatch(audio_sha256):
        raise SidecarError('物化译稿参数无效')
    raw_segments = document.get('segments')
    if not isinstance(raw_segments, list) or not raw_segments:
        raise SidecarError('document.json 缺少 segments，无法物化译稿（D4）')
    non_ad = [seg for seg in raw_segments if isinstance(seg, dict) and not bool(seg.get('is_ad'))]
    if not non_ad:
        raise SidecarError('document.json 缺少非广告段落，禁止发布（D4）')

    ad_skipped = len(raw_segments) - len(non_ad)
    if strict_all_non_ad:
        for seg in non_ad:
            ch = seg.get('chinese')
            if not isinstance(ch, str) or not ch.strip():
                raise SidecarError('非广告段缺少中文译文，禁止发布（D4）')

    output_timing = _check_render_identity_cross(
        document, render_identity, non_ad, audio_sha256=audio_sha256, duration_sec=duration_sec
    )
    if output_timing is not None:
        timeline_name = 'output.mp3.seconds'
        source_name = 'document.json+render-identity.json'
    else:
        timeline_name = 'source.seconds'
        source_name = 'document.json'

    out_segments: list[dict] = []
    for index, seg in enumerate(non_ad):
        ch = seg.get('chinese')
        if not isinstance(ch, str) or not ch.strip():
            continue
        zh_text = ch.strip()
        seg_id = seg.get('id')
        seg_id_str = seg_id if isinstance(seg_id, str) and seg_id else (
            str(seg_id) if isinstance(seg_id, int) and not isinstance(seg_id, bool) else f'seg-{index}'
        )
        item: dict[str, object] = {'id': seg_id_str, 'zh': zh_text}
        en_raw = seg.get('text')
        if isinstance(en_raw, str) and en_raw.strip():
            item['en'] = en_raw.strip()
        if output_timing is not None:
            t_start, t_end = output_timing[seg['id']]
            item['t_start'] = t_start
            item['t_end'] = t_end
        else:
            start_ms = seg.get('start_ms')
            end_ms = seg.get('end_ms')
            if type(start_ms) is int and type(end_ms) is int and 0 <= start_ms < end_ms:
                item['t_start'] = round(start_ms / 1000.0, 3)
                item['t_end'] = round(end_ms / 1000.0, 3)
        out_segments.append(item)

    if not out_segments:
        raise SidecarError('document.json 无有效非空中文段落，禁止发布（D4）')

    full_text = '\n'.join(str(seg['zh']) for seg in out_segments)
    payload = {
        'schema_version': 1,
        'generated_at': generated_at or datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source': source_name,
        'timeline': timeline_name,
        'audio_sha256': audio_sha256,
        'text': full_text,
        'segments': out_segments,
    }
    raw_bytes = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    meta = {
        'timeline': timeline_name,
        'source': source_name,
        'audio_sha256': audio_sha256,
        'transcript_hash': compute_transcript_hash(full_text),
        'segment_count': len(out_segments),
        'ad_skipped': ad_skipped,
    }
    return raw_bytes, meta


def is_placeholder_deep_packet(packet: dict) -> bool:
    """Mirror MockExtractor.isFixturePacket (all 7 rules)."""
    if not isinstance(packet, dict):
        return False
    if packet.get('mode') == 'light':
        return False
    packet_id = packet.get('packet_id')
    if isinstance(packet_id, str) and '-deep-mock' in packet_id:
        return True
    claims = {c.get('text') for c in (packet.get('claims') or []) if isinstance(c, dict)}
    methods = {m.get('title') for m in (packet.get('methods') or []) if isinstance(m, dict)}
    entities = {e.get('name_src') for e in (packet.get('entities') or []) if isinstance(e, dict)}
    questions = {q.get('text') for q in (packet.get('open_questions') or []) if isinstance(q, dict)}
    if (
        '收藏只是入口，抽成可行动卡片才算拥有。' in claims
        and 'Some Show' in entities
        and '这套方法换到中文语境是否仍成立？' in questions
    ):
        return True
    if '盐曲工艺' in methods and '短时高盐比会压住杂菌、风味更干净。' in claims:
        return True
    if '未来两年推理成本下降会重塑应用层毛利。' in claims and 'OpenAI' in entities:
        return True
    if '肌酸补充记录' in methods and '长期高剂量肌酸是否有额外收益仍有争议。' in claims:
        return True
    if '双栏笔记教法' in methods and 'AI 时代应先教提问与校对，再教标准答案。' in claims:
        return True
    if '三分钟身体扫描' in methods and packet.get('summary_line') == '抽出练习协议':
        return True
    return False


def validate_extract_packet(raw_bytes: bytes, *, episode_id: str) -> dict:
    """Validate an extract.json or extract.live.json packet."""
    packet = decode_json_strict(raw_bytes)
    packet_id = packet.get('packet_id')
    if not isinstance(packet_id, str) or not packet_id.strip():
        raise SidecarError('提炼包缺少有效 packet_id')
    if packet.get('work_id') != episode_id:
        raise SidecarError('提炼包 work_id 与节目编号不匹配')
    is_deep_ready = (
        packet.get('status') == 'ready'
        and packet.get('mode') != 'light'
        and not is_placeholder_deep_packet(packet)
    )
    return {
        'packet': packet,
        'packet_id': packet_id,
        'is_deep_ready': is_deep_ready,
        'normalized_transcript_hash': normalize_transcript_hash(packet.get('transcript_hash')),
    }


def _unique_slot_match(row: dict, packet: dict) -> tuple[str, dict | None]:
    """Mirror ListenPicks.uniqueSlotMatch (returns ('none'|'invalid'|'matched', item))."""
    source_point_id = row.get('source_point_id')
    if isinstance(source_point_id, str) and source_point_id.strip():
        target_id = source_point_id.strip()
    else:
        fallback = row.get('id')
        if not isinstance(fallback, str) or not fallback.strip():
            return 'none', None
        target_id = fallback.strip()

    kind = row.get('source_kind')
    kind_norm = kind.strip().lower() if isinstance(kind, str) else ''

    matches: list[dict] = []
    buckets = {
        'claim': packet.get('claims') or [],
        'method': packet.get('methods') or [],
        'quote': packet.get('quotes') or [],
        'open': packet.get('open_questions') or [],
    }
    if kind_norm == '':
        selected_lists = list(buckets.values())
    elif kind_norm == 'claim':
        selected_lists = [buckets['claim']]
    elif kind_norm == 'method':
        selected_lists = [buckets['method']]
    elif kind_norm == 'quote':
        selected_lists = [buckets['quote']]
    elif kind_norm in ('open', 'open_question', 'question'):
        selected_lists = [buckets['open']]
    else:
        return 'none', None

    for lst in selected_lists:
        if not isinstance(lst, list):
            continue
        for item in lst:
            if isinstance(item, dict) and item.get('id') == target_id:
                matches.append(item)

    if len(matches) != 1:
        return 'none', None
    matched = matches[0]
    if matched.get('status') == 'discarded' or matched.get('low_confidence') is True:
        return 'invalid', None
    return 'matched', matched


def validate_point_listen(
    raw_bytes: bytes,
    *,
    episode_id: str,
    audio_sha256: str,
    duration_sec: float | None,
    extract_packets: list[dict],
) -> dict:
    """Fail-closed validation mirroring PointListening.refusal + ListenPicks.select."""
    if not _is_finite_number(duration_sec) or float(duration_sec) <= 0:
        raise SidecarError('成片时长未核实，拒绝校验或入包精选')
    dur = float(duration_sec)
    if not _HEX64.fullmatch(audio_sha256):
        raise SidecarError('成片 audio_sha256 格式无效')

    index = decode_json_strict(raw_bytes)
    if index.get('work_id') != episode_id:
        raise SidecarError('精选索引 work_id 与节目编号不匹配')
    index_packet_id = index.get('packet_id')
    if not isinstance(index_packet_id, str) or not index_packet_id:
        raise SidecarError('精选索引缺少 packet_id')

    matched_packet: dict | None = None
    for pkt in extract_packets:
        if not isinstance(pkt, dict):
            continue
        if (
            pkt.get('packet_id') == index_packet_id
            and pkt.get('work_id') == episode_id
            and pkt.get('status') == 'ready'
            and pkt.get('mode') != 'light'
            and not is_placeholder_deep_packet(pkt)
        ):
            matched_packet = pkt
            break
    if matched_packet is None:
        raise SidecarError('精选索引缺少对应的已就绪深度提炼包')

    idx_hash = normalize_transcript_hash(index.get('transcript_hash'))
    pkt_hash = normalize_transcript_hash(matched_packet.get('transcript_hash'))
    if idx_hash is None or pkt_hash is None or idx_hash != pkt_hash:
        raise SidecarError('精选索引 transcript_hash 与提炼包不一致或格式无效')

    if index.get('timeline') != 'output.mp3.seconds':
        raise SidecarError('精选索引 timeline 必须是 output.mp3.seconds')
    if index.get('audio_sha256') != audio_sha256:
        raise SidecarError('精选索引 audio_sha256 与当前成片不匹配')

    viewpoints = index.get('viewpoints')
    if not isinstance(viewpoints, list) or not viewpoints:
        raise SidecarError('精选索引缺少 viewpoints')

    seen_ids: set[str] = set()
    qualified_count = 0
    for row in viewpoints:
        if not isinstance(row, dict):
            raise SidecarError('精选条目必须是对象')
        row_id = row.get('id')
        if not isinstance(row_id, str) or not row_id or row_id in seen_ids:
            raise SidecarError('精选条目 id 无效或重复')
        seen_ids.add(row_id)

        if row.get('index_include') is False:
            continue
        t_start = row.get('t_start')
        t_end = row.get('t_end')
        if not (_is_finite_number(t_start) and _is_finite_number(t_end)):
            continue
        ts, te = float(t_start), float(t_end)
        if not (0 <= ts < te <= dur) or (te - ts) < _MIN_PICK_DURATION_SEC:
            continue
        vp_len = _trimmed_len(row.get('viewpoint_text'))
        if vp_len < 8:
            continue
        slot_state, _ = _unique_slot_match(row, matched_packet)
        if slot_state == 'invalid':
            continue
        if slot_state == 'none' and (_trimmed_len(row.get('span')) < 8 or vp_len < 12):
            continue
        qualified_count += 1

    if qualified_count < _MIN_PICK_COUNT:
        raise SidecarError(f'精选合格候选不足 {_MIN_PICK_COUNT} 条（当前 {qualified_count} 条）')

    return {
        'index': index,
        'packet_id': index_packet_id,
        'transcript_hash': idx_hash,
        'qualified_count': qualified_count,
    }


def _find_bound_span_timing(
    span: object,
    bound_lines: list[dict],
    item_t_start: object,
    item_t_end: object,
    duration_sec: float,
) -> tuple[float, float] | None:
    """Locate real output.mp3.seconds timing from bound transcript segments only."""
    timed_lines = [
        line for line in bound_lines
        if line['t_start'] is not None and line['t_end'] is not None
        and 0 <= line['t_start'] < line['t_end'] <= duration_sec
    ]
    if not timed_lines:
        return None
    if isinstance(span, str) and span.strip():
        needle = ''.join(span.split())
        if needle:
            hits = [line for line in timed_lines if needle in ''.join(line['display_text'].split())]
            if len(hits) == 1:
                ts, te = float(hits[0]['t_start']), float(hits[0]['t_end'])
                if te - ts >= _MIN_PICK_DURATION_SEC:
                    return round(ts, 3), round(te, 3)
    if _is_finite_number(item_t_start) and _is_finite_number(item_t_end):
        ts, te = float(item_t_start), float(item_t_end)
        if 0 <= ts < te <= duration_sec and (te - ts) >= _MIN_PICK_DURATION_SEC:
            if isinstance(span, str) and _trimmed_len(span) > 0:
                needle = ''.join(span.split())
                for line in timed_lines:
                    if needle in ''.join(line['display_text'].split()):
                        if abs(ts - line['t_start']) <= 5.0 and abs(te - line['t_end']) <= 5.0:
                            return round(ts, 3), round(te, 3)
    return None


def build_point_listen_from_bound_segments(
    *,
    episode_id: str,
    audio_sha256: str,
    duration_sec: float | None,
    transcript_summary: dict,
    packet: dict,
) -> dict | None:
    """Deterministically build point-listen.json from bound output.mp3.seconds segments (0 AI calls)."""
    if not _is_finite_number(duration_sec) or float(duration_sec) <= 0:
        return None
    dur = float(duration_sec)
    if not _HEX64.fullmatch(audio_sha256):
        return None
    if transcript_summary.get('timeline') != 'output.mp3.seconds':
        return None
    if transcript_summary.get('audio_sha256') != audio_sha256:
        return None
    if (
        not isinstance(packet, dict)
        or packet.get('work_id') != episode_id
        or packet.get('status') != 'ready'
        or packet.get('mode') == 'light'
        or is_placeholder_deep_packet(packet)
    ):
        return None
    pkt_hash = normalize_transcript_hash(packet.get('transcript_hash'))
    packet_id = packet.get('packet_id')
    if not pkt_hash or not isinstance(packet_id, str) or not packet_id:
        return None

    bound_lines = transcript_summary.get('lines') or []
    viewpoints: list[dict] = []
    sources = (
        ('claim', packet.get('claims') or []),
        ('method', packet.get('methods') or []),
        ('quote', packet.get('quotes') or []),
        ('open', packet.get('open_questions') or []),
    )
    for kind, items in sources:
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get('status') == 'discarded' or item.get('low_confidence') is True:
                continue
            item_id = item.get('id')
            if not isinstance(item_id, str) or not item_id.strip():
                continue
            if kind == 'method':
                title = (item.get('title') or '').strip() if isinstance(item.get('title'), str) else ''
                steps = item.get('steps') if isinstance(item.get('steps'), list) else []
                first_step = next((s.strip() for s in steps if isinstance(s, str) and s.strip()), '')
                if title and first_step:
                    vp_text = f'{title}：{first_step}'
                elif _trimmed_len(title) >= 8:
                    vp_text = title
                else:
                    continue
            else:
                raw_text = item.get('text')
                if not isinstance(raw_text, str) or not raw_text.strip():
                    continue
                vp_text = raw_text.strip()
            if _trimmed_len(vp_text) < 8:
                continue
            span = item.get('span')
            timing = _find_bound_span_timing(
                span, bound_lines, item.get('t_start'), item.get('t_end'), dur
            )
            if timing is None:
                continue
            ts, te = timing
            vp: dict[str, object] = {
                'id': f'{kind}-{item_id.strip()}',
                'viewpoint_text': vp_text,
                't_start': ts,
                't_end': te,
                'index_include': True,
                'source_kind': kind,
                'source_point_id': item_id.strip(),
            }
            if isinstance(span, str) and span.strip():
                vp['span'] = span.strip()
            viewpoints.append(vp)

    if len(viewpoints) < _MIN_PICK_COUNT:
        return None
    candidate = {
        'schema_version': 1,
        'work_id': episode_id,
        'packet_id': packet_id,
        'transcript_hash': pkt_hash,
        'audio_sha256': audio_sha256,
        'timeline': 'output.mp3.seconds',
        'viewpoints': viewpoints,
    }
    raw_bytes = (json.dumps(candidate, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    try:
        validate_point_listen(
            raw_bytes,
            episode_id=episode_id,
            audio_sha256=audio_sha256,
            duration_sec=dur,
            extract_packets=[packet],
        )
    except SidecarError:
        return None
    return candidate
