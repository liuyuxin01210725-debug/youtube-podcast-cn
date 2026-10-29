#!/usr/bin/env python3
"""声间「收藏≠拥有」抽取管线（MVP）。

权威规格：docs/shengjian-extract-design-v1-2026-10-29.md §13.1–13.5 + §13.8。
本模块负责：透镜→schema、校验门禁、light/deep 产物、mock 抽取、
extract.json 合并保留、取消星标撤 queued、Markdown 导出。
真模型调用路径预留；无 API key 时用确定性 mock。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_IDS = ('general', 'ferment', 'biz_invest', 'health', 'edu', 'embodied')
PACKET_STATUSES = ('queued', 'running', 'ready', 'failed', 'stale')
ITEM_STATUSES = ('candidate', 'confirmed', 'discarded')
EXTRACT_MODES = ('light', 'deep', 'manual')

# §13.8.2 透镜中文名 / lens_id ↔ schema_id
_LENS_ID_TO_SCHEMA = {
    'frontier-tech': 'general',
    'embodied-robotics': 'embodied',
    'health-longevity': 'health',
    'ferment-food': 'ferment',
    'ai-education': 'edu',
    'business-investing': 'biz_invest',
    'niche-life-brands': 'general',
}

_LENS_LABEL_TO_SCHEMA = {
    '通用': 'general',
    '未分类': 'general',
    '无透镜': 'general',
    '发酵': 'ferment',
    '发芽': 'ferment',
    '发芽发酵 / 食品': 'ferment',
    '商业': 'biz_invest',
    '投资': 'biz_invest',
    '商业与投资': 'biz_invest',
    '健康': 'health',
    '运动': 'health',
    '健康长寿': 'health',
    '教育': 'edu',
    'AI 时代教育': 'edu',
    '具身': 'embodied',
    '具身与机器人': 'embodied',
    '前沿科技': 'general',
    '小众生活与品牌': 'general',
}

# §13.8.3 要点类型样例标签（反万能摘要）
SCHEMA_POINT_TYPES = {
    'general': {'claim_debate', 'entity_roster', 'open_question'},
    'ferment': {'method_craft', 'product_niche', 'claim_flavor'},
    'biz_invest': {'claim_outlook', 'org_watch', 'framework_name'},
    'health': {'method_dose', 'claim_controversy', 'product_gear'},
    'edu': {'claim_edu', 'method_pedagogy', 'software_tool'},
    'embodied': {'method_practice', 'claim_somatic', 'product_prop'},
}

SECONDARY_CAP = 5
LIST_BADGE_DENSITY_CAP = 3


class ExtractError(Exception):
    """Safe message for callers."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def transcript_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def schema_from_lens_id(lens_id: str | None) -> str:
    if not lens_id:
        return 'general'
    return _LENS_ID_TO_SCHEMA.get(str(lens_id).strip(), 'general')


def schema_from_lens_label(label: str | None) -> str:
    if not label:
        return 'general'
    text = str(label).strip()
    if text in _LENS_LABEL_TO_SCHEMA:
        return _LENS_LABEL_TO_SCHEMA[text]
    for key, schema in _LENS_LABEL_TO_SCHEMA.items():
        if key and key in text:
            return schema
    return 'general'


def resolve_schema(lens_ids: list[str] | None, *, primary_lens: str | None = None,
                   explicit_schema: str | None = None,
                   allow_embodied: bool = False) -> dict[str, Any]:
    """§13.8.1：无透镜→general；禁止深抽前轻分类；embodied MVP 可降级。

    Returns dict with schema_id, secondary_schemas, degraded_from.
    """
    if explicit_schema in SCHEMA_IDS:
        schema = explicit_schema
        secondary: list[str] = []
        degraded = None
    else:
        ids = [str(x) for x in (lens_ids or []) if x]
        if primary_lens and primary_lens in ids:
            ordered = [primary_lens] + [x for x in ids if x != primary_lens]
        else:
            ordered = ids
        mapped = []
        seen = set()
        for lid in ordered:
            sid = schema_from_lens_id(lid)
            if sid not in seen:
                mapped.append(sid)
                seen.add(sid)
        if not mapped:
            schema = 'general'
            secondary = []
        else:
            schema = mapped[0]
            secondary = mapped[1:]
        degraded = None

    if schema == 'embodied' and not allow_embodied:
        degraded = 'embodied'
        schema = 'general'  # §13.8.1：显式降级 general+Method，禁止静默当 health
    return {
        'schema_id': schema,
        'secondary_schemas': secondary if explicit_schema not in SCHEMA_IDS else [],
        'degraded_from': degraded,
        'forbid_preclassify': True,
    }


def span_locatable(span: str, transcript: str, *, min_len: int = 8) -> bool:
    """§13.1：span 须能在 transcript_hash 对应文本中定位。"""
    if not isinstance(span, str):
        return False
    cleaned = span.strip()
    if len(cleaned) < min_len:
        return False
    if cleaned in transcript:
        return True
    # 归一化空白后的模糊对齐
    def norm(s: str) -> str:
        return re.sub(r'\s+', '', s)
    return norm(cleaned) in norm(transcript)


def reject_or_keep_item(item: dict, transcript: str, *, schema_id: str,
                        axis_ok: bool) -> dict | None:
    """§13.1 / 13.5：映射失败拒收；健康剂量对不齐丢弃。"""
    if not axis_ok:
        return None
    span = item.get('span') or ''
    if not span_locatable(span, transcript):
        # 健康/投资剂量类对不齐直接丢弃
        if schema_id == 'health' and item.get('kind') == 'method':
            return None
        if schema_id == 'biz_invest' and item.get('kind') in ('claim', 'entity'):
            item = dict(item)
            item['confidence'] = min(float(item.get('confidence') or 0), 0.4)
            item['status'] = 'candidate'
            item['low_confidence'] = True
            return item
        item = dict(item)
        item['confidence'] = min(float(item.get('confidence') or 0), 0.4)
        item['low_confidence'] = True
        return item
    return item


def validate_output_axis(t_start, t_end, *, duration_sec: float | None = None) -> bool:
    """抽出物时间必须是 output.mp3 轴；失败拒收。"""
    try:
        start = float(t_start)
        end = float(t_end)
    except (TypeError, ValueError):
        return False
    if start < 0 or end < 0 or end < start:
        return False
    if duration_sec is not None and duration_sec > 0 and start > duration_sec + 1:
        return False
    return True


def looks_like_universal_summary(packet: dict) -> bool:
    """§13.8.1 反万能摘要：深包每条须能标成要点类型样例之一。"""
    if packet.get('mode') == 'light':
        return False
    schema = packet.get('schema_id') or 'general'
    allowed = SCHEMA_POINT_TYPES.get(schema, SCHEMA_POINT_TYPES['general'])
    items = []
    for key in ('claims', 'methods', 'entities', 'quotes', 'open_questions'):
        items.extend(packet.get(key) or [])
    if not items:
        # 深抽无任何类型卡 → 流水账风险
        return True
    typed = 0
    for item in items:
        pt = item.get('point_type')
        if pt in allowed:
            typed += 1
    return typed == 0


def empty_packet(*, work_id: str, schema_id: str, mode: str,
                 status: str = 'queued', summary_line: str = '') -> dict:
    return {
        'packet_id': f'{work_id}-{mode}-{utc_now().replace(":", "")}',
        'work_id': work_id,
        'schema_id': schema_id,
        'mode': mode,
        'status': status,
        'created_at': utc_now(),
        'model_meta': {'provider': 'mock', 'version': 'mvp-1'},
        'cost_units': 0,
        'summary_line': summary_line,
        'transcript_hash': None,
        'claims': [],
        'methods': [],
        'entities': [],
        'quotes': [],
        'open_questions': [],
        'actions': [],
    }


def light_packet(*, work_id: str, schema_id: str, summary_line: str,
                 roster: list[dict] | None = None) -> dict:
    """§13.3：light 只保留 summary_line + 折叠名录；不进待确认。"""
    packet = empty_packet(work_id=work_id, schema_id=schema_id, mode='light',
                          status='ready', summary_line=summary_line)
    packet['cost_units'] = 0
    packet['roster'] = roster or []
    packet['pending_confirm_count'] = 0
    return packet


def pending_confirm_count(packet: dict) -> int:
    if packet.get('mode') == 'light':
        return 0
    if packet.get('status') not in ('ready',):
        return 0
    n = 0
    for key in ('claims', 'methods', 'entities', 'quotes', 'open_questions'):
        for item in packet.get(key) or []:
            if item.get('status') == 'candidate' and not item.get('low_confidence'):
                n += 1
    return n


def mock_deep_extract(*, work_id: str, schema_id: str, transcript: str,
                      duration_sec: float = 600.0,
                      secondary_schemas: list[str] | None = None,
                      allow_embodied: bool = False) -> dict:
    """确定性 mock：按 schema 产出合格类型卡（fixtures 同源逻辑）。"""
    resolved = resolve_schema([], explicit_schema=schema_id, allow_embodied=allow_embodied)
    schema = resolved['schema_id']
    th = transcript_hash(transcript)
    packet = empty_packet(work_id=work_id, schema_id=schema, mode='deep', status='ready')
    packet['transcript_hash'] = th
    packet['cost_units'] = 1
    packet['degraded_from'] = resolved.get('degraded_from')
    builders = {
        'ferment': _mock_ferment,
        'biz_invest': _mock_biz,
        'health': _mock_health,
        'edu': _mock_edu,
        'general': _mock_general,
        'embodied': _mock_embodied,
    }
    builders.get(schema, _mock_general)(packet, transcript, duration_sec)

    # 次透镜：只抽必抽，合计 ≤5
    secondary_items = 0
    for sec in (secondary_schemas or [])[:3]:
        if secondary_items >= SECONDARY_CAP:
            break
        bucket = []
        tmp = empty_packet(work_id=work_id, schema_id=sec, mode='deep')
        builders.get(sec, _mock_general)(tmp, transcript, duration_sec)
        for key in ('claims', 'methods', 'entities'):
            for item in tmp.get(key) or []:
                if secondary_items >= SECONDARY_CAP:
                    break
                item = dict(item)
                item['from_secondary_schema'] = sec
                bucket.append((key, item))
                secondary_items += 1
        for key, item in bucket:
            packet.setdefault(key, []).append(item)

    # §13.1 过滤
    packet = _filter_packet_axis(packet, transcript, duration_sec)

    if looks_like_universal_summary(packet):
        packet['status'] = 'failed'
        packet['failure_reason'] = 'universal_summary'
        packet['summary_line'] = '抽取像流水账，请改主题重抽'
        return packet

    packet['summary_line'] = packet.get('summary_line') or _default_summary(schema, packet)
    packet['pending_confirm_count'] = pending_confirm_count(packet)
    return packet


def _pick_span(transcript: str, needle: str, fallback: str) -> str:
    """Prefer a >=8 char substring of transcript containing needle."""
    text = transcript.strip()
    if needle and needle in text:
        idx = text.index(needle)
        left = max(0, idx - 8)
        right = min(len(text), idx + max(len(needle), 8) + 16)
        chunk = text[left:right].strip()
        if len(chunk) >= 8:
            return chunk
    if fallback and fallback in text and len(fallback) >= 8:
        return fallback
    if len(text) >= 12:
        return text[: min(48, len(text))]
    base = fallback if fallback else text
    return base if len(base) >= 8 else (base + '……补充对齐文本')


def _base_item(kind: str, **kwargs) -> dict:
    item = {
        'id': kwargs.pop('id', f'{kind}-{kwargs.get("point_type", "x")}'),
        'kind': kind,
        'status': kwargs.pop('status', 'candidate'),
        'confidence': kwargs.pop('confidence', 0.82),
        'span': kwargs.pop('span', ''),
        't_start': kwargs.pop('t_start', 12.0),
        't_end': kwargs.pop('t_end', 28.0),
    }
    item.update(kwargs)
    return item


def _mock_ferment(packet: dict, transcript: str, duration: float) -> None:
    span = _pick_span(transcript, '盐曲', '盐曲按两斤盐一斤曲，密封室温发酵')
    packet['methods'].append(_base_item(
        'method', id='m-craft-1', point_type='method_craft',
        title='盐曲工艺',
        steps=['称盐与曲', '按 2:1 混合', '密封室温放置'],
        params={'盐比': '2:1', '温度': '室温', '时长': '7天'},
        cautions=['勿用碘盐'],
        span=span, t_start=40.0, t_end=75.0,
        default_action='ferment_note',
    ))
    packet['entities'].append(_base_item(
        'entity', id='e-prod-1', point_type='product_niche',
        type='Product', name_src='koji', name_zh='米曲',
        status='candidate',
        span=_pick_span(transcript, '米曲', '米曲是小众原料可用于家用发酵'),
        t_start=80.0, t_end=95.0,
        default_action='try_later',
    ))
    packet['claims'].append(_base_item(
        'claim', id='c-flavor-1', point_type='claim_flavor',
        text='短时高盐比会压住杂菌、风味更干净。',
        stance='neutral',
        span=_pick_span(transcript, '风味', '短时高盐比会压住杂菌风味更干净'),
        t_start=100.0, t_end=120.0,
        default_action='debate',
    ))
    packet['summary_line'] = '抽出盐曲工艺与米曲原料'


def _mock_biz(packet: dict, transcript: str, duration: float) -> None:
    packet['claims'].append(_base_item(
        'claim', id='c-outlook-1', point_type='claim_outlook',
        text='未来两年推理成本下降会重塑应用层毛利。',
        stance='bullish',  # 内部字段；UI 主视觉不展示看多看空
        span=_pick_span(transcript, '推理成本', '未来两年推理成本下降会重塑应用层毛利'),
        t_start=30.0, t_end=55.0,
        default_action='monitor_candidate',
        attrs={'framework': '成本曲线'},
    ))
    packet['entities'].append(_base_item(
        'entity', id='e-org-1', point_type='org_watch',
        type='Org', name_src='OpenAI', name_zh=None,
        status='candidate',
        span=_pick_span(transcript, 'OpenAI', '嘉宾提到 OpenAI 作为观察标的而非荐股'),
        t_start=60.0, t_end=78.0,
        default_action='monitor_candidate',
    ))
    packet['summary_line'] = '抽出行业判断与观察标的'


def _mock_health(packet: dict, transcript: str, duration: float) -> None:
    packet['methods'].append(_base_item(
        'method', id='m-dose-1', point_type='method_dose',
        title='肌酸补充记录',
        steps=['每日固定时间服用', '配足量水'],
        params={'剂量': '5g', '频率': '每日一次'},
        cautions=['非处方；个体差异大'],
        span=_pick_span(transcript, '5克', '肌酸每日约5克，仅作记录非处方'),
        t_start=22.0, t_end=48.0,
        default_action='record_only',  # §13.5 主按钮「仅记录」
        ui_primary_label='仅记录',
        disclaimer='非诊断、非处方，可丢弃',
    ))
    packet['claims'].append(_base_item(
        'claim', id='c-cont-1', point_type='claim_controversy',
        text='长期高剂量肌酸是否有额外收益仍有争议。',
        stance='争议',
        span=_pick_span(transcript, '争议', '长期高剂量肌酸是否有额外收益仍有争议'),
        t_start=50.0, t_end=70.0,
        default_action='debate',
    ))
    packet['summary_line'] = '抽出剂量方法（仅记录）与争议观点'


def _mock_edu(packet: dict, transcript: str, duration: float) -> None:
    packet['claims'].append(_base_item(
        'claim', id='c-edu-1', point_type='claim_edu',
        text='AI 时代应先教提问与校对，再教标准答案。',
        span=_pick_span(transcript, '提问', 'AI 时代应先教提问与校对再教标准答案'),
        t_start=15.0, t_end=40.0,
        default_action='debate',
    ))
    packet['methods'].append(_base_item(
        'method', id='m-ped-1', point_type='method_pedagogy',
        title='双栏笔记教法',
        steps=['左栏摘录', '右栏用自己的话重述', '课后互评'],
        params={},
        span=_pick_span(transcript, '双栏', '双栏笔记左摘录右重述课后互评'),
        t_start=45.0, t_end=80.0,
        default_action='try_later',
    ))
    packet['summary_line'] = '抽出教育判断与教法'


def _mock_general(packet: dict, transcript: str, duration: float) -> None:
    packet['claims'].append(_base_item(
        'claim', id='c-gen-1', point_type='claim_debate',
        text='收藏只是入口，抽成可行动卡片才算拥有。',
        span=_pick_span(transcript, '收藏', '收藏只是入口抽成可行动卡片才算拥有'),
        t_start=10.0, t_end=30.0,
        default_action='debate',
    ))
    packet['entities'].append(_base_item(
        'entity', id='e-person-1', point_type='entity_roster',
        type='Person', name_src='Guest', name_zh='嘉宾',
        status='candidate',  # Person 默认 candidate
        span=_pick_span(transcript, '嘉宾', '本期嘉宾提到一个值得关注的人'),
        t_start=35.0, t_end=50.0,
        default_action='follow_candidate',
    ))
    packet['entities'].append(_base_item(
        'entity', id='e-show-1', point_type='entity_roster',
        type='Show', name_src='Some Show', name_zh=None,
        status='candidate',
        span=_pick_span(transcript, '节目', '也提到另一个节目值得候选关注'),
        t_start=55.0, t_end=70.0,
        default_action='follow_candidate',
    ))
    packet['open_questions'].append(_base_item(
        'open_question', id='q-1', point_type='open_question',
        text='这套方法换到中文语境是否仍成立？',
        span=_pick_span(transcript, '成立', '这套方法换到中文语境是否仍成立'),
        t_start=75.0, t_end=90.0,
        default_action='debate',
    ))
    packet['summary_line'] = '抽出主张、提及实体与待验证问题'


def _mock_embodied(packet: dict, transcript: str, duration: float) -> None:
    packet['methods'].append(_base_item(
        'method', id='m-prac-1', point_type='method_practice',
        title='三分钟身体扫描',
        steps=['坐下', '从头到脚扫描感受', '不做评判'],
        params={'时长': '3分钟'},
        span=_pick_span(transcript, '扫描', '三分钟身体扫描从头到脚不做评判'),
        t_start=20.0, t_end=50.0,
        default_action='try_later',
    ))
    packet['claims'].append(_base_item(
        'claim', id='c-som-1', point_type='claim_somatic',
        text='注意力回到身体感受，比直接改想法更稳。',
        span=_pick_span(transcript, '身体', '注意力回到身体感受比直接改想法更稳'),
        t_start=55.0, t_end=80.0,
        default_action='debate',
    ))
    packet['summary_line'] = '抽出练习协议与身心框架'


def _filter_packet_axis(packet: dict, transcript: str, duration_sec: float) -> dict:
    schema = packet.get('schema_id') or 'general'
    out = dict(packet)
    for key in ('claims', 'methods', 'entities', 'quotes', 'open_questions'):
        kept = []
        for item in packet.get(key) or []:
            axis_ok = validate_output_axis(item.get('t_start'), item.get('t_end'),
                                           duration_sec=duration_sec)
            filtered = reject_or_keep_item(item, transcript, schema_id=schema, axis_ok=axis_ok)
            if filtered is None:
                continue
            # Person/Show 强制 candidate
            if filtered.get('type') in ('Person', 'Show'):
                filtered['status'] = 'candidate'
            # health 剂量置信门
            if schema == 'health' and filtered.get('point_type') == 'method_dose':
                if float(filtered.get('confidence') or 0) < 0.5 or filtered.get('low_confidence'):
                    continue
            kept.append(filtered)
        out[key] = kept
    return out


def _default_summary(schema: str, packet: dict) -> str:
    counts = []
    for label, key in (('观点', 'claims'), ('方法', 'methods'), ('实体', 'entities')):
        n = len(packet.get(key) or [])
        if n:
            counts.append(f'{n}条{label}')
    return '、'.join(counts) or f'{schema} 抽取完成'


def merge_extract_state(existing: dict | None, incoming: dict) -> dict:
    """§13.2：再发布时合并保留确认/丢弃/动作；禁止整文件覆盖清库。"""
    if not existing or not isinstance(existing, dict):
        return incoming
    out = deepcopy(incoming)
    old_items = {}
    for key in ('claims', 'methods', 'entities', 'quotes', 'open_questions'):
        for item in existing.get(key) or []:
            iid = item.get('id')
            if iid:
                old_items[(key, iid)] = item
    for key in ('claims', 'methods', 'entities', 'quotes', 'open_questions'):
        merged = []
        for item in out.get(key) or []:
            iid = item.get('id')
            prev = old_items.get((key, iid))
            if prev:
                item = dict(item)
                # 保留用户确认态与动作
                if prev.get('status') in ITEM_STATUSES:
                    item['status'] = prev['status']
                if 'action_state' in prev:
                    item['action_state'] = prev['action_state']
                if prev.get('user_note'):
                    item['user_note'] = prev['user_note']
            merged.append(item)
        out[key] = merged
    # 动作表第三层
    if isinstance(existing.get('actions'), list) and existing['actions']:
        out['actions'] = deepcopy(existing['actions'])
    # 星标/作业态：若旧包 ready 且用户有确认，不因 republish 抹掉
    if existing.get('status') == 'ready' and incoming.get('status') in (None, 'queued', 'ready'):
        if pending_confirm_count(existing) != pending_confirm_count(existing):  # noqa: silly keep lint calm
            pass
        # transcript 变了 → stale
        old_hash = existing.get('transcript_hash')
        new_hash = incoming.get('transcript_hash')
        if old_hash and new_hash and old_hash != new_hash:
            out['status'] = 'stale'
            out['stale_reason'] = 'transcript_hash_changed'
            out['previous_packet_id'] = existing.get('packet_id')
    return out


def load_extract(episode_dir: Path) -> dict | None:
    data = _read_json(Path(episode_dir) / 'extract.json')
    return data if isinstance(data, dict) else None


def save_extract(episode_dir: Path, packet: dict) -> Path:
    path = Path(episode_dir) / 'extract.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.extract.json.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    return path


def preserve_extract_on_republish(episode_dir: Path, *, new_transcript_hash: str | None = None) -> dict | None:
    """发布流程调用：保留 extract.json；哈希变则标 stale。"""
    existing = load_extract(episode_dir)
    if not existing:
        return None
    out = deepcopy(existing)
    if new_transcript_hash and existing.get('transcript_hash') and existing['transcript_hash'] != new_transcript_hash:
        out['status'] = 'stale'
        out['stale_reason'] = 'transcript_hash_changed'
        save_extract(episode_dir, out)
    return out


def enqueue_deep(*, episode_dir: Path, work_id: str, schema_id: str,
                 has_transcript: bool) -> dict:
    """星标→deep；无稿不深抽。"""
    if not has_transcript:
        packet = empty_packet(work_id=work_id, schema_id=schema_id, mode='deep', status='failed')
        packet['failure_reason'] = 'no_transcript'
        packet['summary_line'] = '无译稿，无法深抽。请先完成听译。'
        save_extract(episode_dir, packet)
        return packet
    packet = empty_packet(work_id=work_id, schema_id=schema_id, mode='deep', status='queued')
    save_extract(episode_dir, packet)
    return packet


def cancel_queued_on_unstar(episode_dir: Path) -> dict | None:
    """§13.4：取消星标且 queued（或可停的 running）→ 撤销，不扣满额。"""
    existing = load_extract(episode_dir)
    if not existing:
        return None
    status = existing.get('status')
    if status in ('queued', 'running') and existing.get('mode') in ('deep', 'manual'):
        out = deepcopy(existing)
        out['status'] = 'failed'
        out['failure_reason'] = 'cancelled_unstar'
        out['cost_units'] = 0
        out['summary_line'] = '已取消星标，深抽任务已撤销'
        save_extract(episode_dir, out)
        return out
    return existing


def run_mock_deep_from_queue(episode_dir: Path, transcript: str,
                             duration_sec: float = 600.0) -> dict:
    existing = load_extract(episode_dir) or {}
    if existing.get('status') not in ('queued', 'running', None, 'failed'):
        if existing.get('status') == 'ready':
            return existing
    schema = existing.get('schema_id') or 'general'
    work_id = existing.get('work_id') or Path(episode_dir).name
    running = dict(existing)
    running['status'] = 'running'
    save_extract(episode_dir, running)
    packet = mock_deep_extract(
        work_id=work_id, schema_id=schema, transcript=transcript, duration_sec=duration_sec,
        secondary_schemas=existing.get('secondary_schemas') or [],
    )
    # 合并旧确认态
    packet = merge_extract_state(existing if existing.get('status') == 'ready' else None, packet)
    save_extract(episode_dir, packet)
    return packet


def export_markdown(packet: dict) -> str:
    """§13.2：MD 从 extract 状态导出。"""
    lines = [
        f"# 抽取 · {packet.get('work_id', '')}",
        '',
        f"- schema: `{packet.get('schema_id')}`",
        f"- mode: `{packet.get('mode')}`",
        f"- status: `{packet.get('status')}`",
        f"- summary: {packet.get('summary_line') or ''}",
        '',
    ]
    sections = [
        ('观点 Claim', 'claims', 'text'),
        ('方法 Method', 'methods', 'title'),
        ('实体 Entity', 'entities', 'name_src'),
        ('金句 Quote', 'quotes', 'text'),
        ('待验证 OpenQuestion', 'open_questions', 'text'),
    ]
    for title, key, field in sections:
        items = packet.get(key) or []
        if not items:
            continue
        lines.append(f'## {title}')
        lines.append('')
        for item in items:
            label = item.get(field) or item.get('text') or item.get('name_zh') or item.get('id')
            lines.append(
                f"- [{item.get('status')}] {label} "
                f"({item.get('t_start')}–{item.get('t_end')}s) "
                f"span=`{(item.get('span') or '')[:40]}`"
            )
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def list_badge(packet: dict | None, *, density_used: int = 0) -> str | None:
    """§13.3 列表徽标；超额降级。"""
    if not packet:
        return None
    status = packet.get('status')
    if status == 'stale':
        return '需重抽'
    if status == 'queued':
        return '提炼排队'
    if status == 'running':
        return '提炼中'
    if status == 'failed':
        if packet.get('failure_reason') == 'no_transcript':
            return '缺译稿'
        if packet.get('failure_reason') == 'cancelled_unstar':
            return None
        return '提炼失败'
    if status == 'ready':
        if packet.get('mode') == 'light':
            return None  # light 不进待确认，列表不刷屏
        n = pending_confirm_count(packet)
        if n <= 0:
            return '已提炼'
        if density_used >= LIST_BADGE_DENSITY_CAP:
            return '有待确认'
        return f'{n}待确认'
    return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def real_extract_reserved(*, api_key: str | None, prompt: dict) -> dict:
    """预留真调用路径。无 key 时不调用。"""
    if not api_key:
        raise ExtractError('无 API key：请用 mock_deep_extract')
    raise ExtractError('真模型抽取尚未接线；请用 mock')
