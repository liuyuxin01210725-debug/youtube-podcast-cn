"""Targeted regression tests for sidecar packaging, D4 gate, point-listen rules, and legacy refresh."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from array import array
import math
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_original_pending as original_pending
import studio_publish_icloud as publisher
from studio_recovery import fingerprint
from studio_sidecars import (
    SidecarError,
    build_point_listen_from_bound_segments,
    compute_transcript_hash,
    materialize_transcript_zh_from_document,
    normalize_transcript_hash,
    validate_point_listen,
    validate_transcript_zh,
)
from temporary_audio import sha256_file


@pytest.fixture(autouse=True)
def _stub_hooks(monkeypatch):
    monkeypatch.setattr(publisher, '_require_audio_preflight', lambda *a, **k: None)
    monkeypatch.setattr(publisher, '_snapshot_library_or_refuse', lambda *a, **k: None)
    monkeypatch.setattr(publisher, '_restore_library_snapshot', lambda *a, **k: None)
    monkeypatch.setattr(publisher, '_discard_library_snapshot', lambda *a, **k: None)
    monkeypatch.setattr('studio_artwork.attach_cover', lambda *a, **k: None)


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _make_wav_and_mp3(folder: Path, freq: int = 440, seconds: int = 2) -> tuple[Path, Path]:
    rate = 24000
    wav = folder / 'src.wav'
    frames = array('h', (int(3000 * math.sin(2 * math.pi * freq * n / rate)) for n in range(rate * seconds)))
    with wave.open(str(wav), 'wb') as handle:
        handle.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        handle.writeframes(frames.tobytes())
    mp3 = folder / 'output.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(wav), str(mp3)], check=True)
    return wav, mp3


def _make_render_job(job: Path, *, freq: int = 440, zh1: str = '第一段中文译稿内容足够长',
                     zh2: str = '第二段中文译稿内容足够长', include_ad: bool = False) -> tuple[dict, str]:
    job.mkdir(parents=True, exist_ok=True)
    wav, mp3 = _make_wav_and_mp3(job, freq=freq, seconds=2)
    source = {'kind': 'youtube', 'value': 'https://www.youtube.com/watch?v=MM-C3JqCXBk'}
    lock = job / 'source-identity.json'
    _write_json(lock, {**source, 'sha256': sha256_file(wav), 'bytes': wav.stat().st_size, 'duration_ms': 2000})
    _write_json(job / 'config.json', {'source': source, 'expected_speakers': 1})
    _write_json(job / 'state.json', {'title': '侧车测试节目'})
    segments = [
        {'id': 'seg-1', 'speaker_id': 'host', 'start_ms': 0, 'end_ms': 1000,
         'text': 'First segment', 'chinese': zh1, 'ambiguous': False},
        {'id': 'seg-2', 'speaker_id': 'host', 'start_ms': 1000, 'end_ms': 2000,
         'text': 'Second segment', 'chinese': zh2, 'ambiguous': False},
    ]
    if include_ad:
        segments.insert(1, {'id': 'ad-1', 'speaker_id': 'host', 'start_ms': 900, 'end_ms': 1000,
                            'text': 'Ad break', 'is_ad': True})
    document = {'speakers': {'host': {'voice_slot': 'male_1'}}, 'references': {}, 'segments': segments}
    _write_json(job / 'document.json', document)
    _write_json(job / 'render-identity.json', {
        'schema_version': 1,
        'sample_rate': 24000,
        'audio_sha256': sha256_file(mp3),
        'document_sha256': fingerprint(document),
        'source_identity_sha256': sha256_file(lock),
        'timeline': [
            {'id': 'seg-1', 'start_frame': 0, 'end_frame': 24000},
            {'id': 'seg-2', 'start_frame': 24000, 'end_frame': 48000},
        ],
    })
    return document, sha256_file(mp3)


def _deep_packet(work_id: str, th_hash: str, *, packet_id: str = 'pkt-deep-1') -> dict:
    return {
        'packet_id': packet_id,
        'work_id': work_id,
        'schema_id': 'general',
        'mode': 'deep',
        'status': 'ready',
        'created_at': '2026-11-26T00:00:00+00:00',
        'transcript_hash': th_hash,
        'claims': [
            {
                'id': '1',
                'text': '第一条深度提炼观点文本足够长',
                'span': '第一段中文译稿内容足够长',
                'status': 'confirmed',
                'point_type': 'claim_core',
            }
        ],
        'methods': [
            {
                'id': '1',
                'title': '实操方法一',
                'steps': ['按照第二段中文译稿执行步骤'],
                'span': '第二段中文译稿内容足够长',
                'status': 'candidate',
                'point_type': 'method_craft',
            }
        ],
        'entities': [],
        'quotes': [],
        'open_questions': [],
        'actions': [],
    }


def test_first_render_publishes_directly_to_objects_with_all_sidecars(tmp_path):
    library = tmp_path / 'library'
    job = tmp_path / 'jobs' / 'ep-sidecar-01'
    _make_render_job(job)
    tr_text = '第一段中文译稿内容足够长\n第二段中文译稿内容足够长'
    th64 = hashlib.sha256(tr_text.encode('utf-8')).hexdigest()
    pkt = _deep_packet(job.name, th64)
    _write_json(job / 'extract.json', pkt)
    _write_json(job / 'extract.live.json', {**pkt, 'packet_id': 'pkt-live-1'})

    result = publisher.publish_job(job, library)
    rev = result['media_revision']
    assert rev['path'] == f"objects/{rev['sha256']}"
    assert rev['previous_path'] == ''
    for name in ('transcript_zh.json', 'extract.json', 'extract.live.json', 'point-listen.json'):
        assert name in rev['files']
    assert original_pending.digest(original_pending.json_bytes(dict(sorted(rev['files'].items())))) == rev['sha256']
    assert not (library / 'revisions').exists()


def test_d4_gate_blocks_missing_or_partial_transcript_across_all_paths(tmp_path):
    library = tmp_path / 'library'
    # 1) First publish without render and without transcript_zh.json -> PublishError
    job_plain = tmp_path / 'jobs' / 'ep-plain'
    job_plain.mkdir(parents=True)
    (job_plain / 'output.mp3').write_bytes(b'plain-audio')
    with pytest.raises(publisher.PublishError, match='D4'):
        publisher.publish_job(job_plain, library)
    assert not (library / 'library.json').exists()

    # 2) Partial document (2 non-ad segments, 2nd missing chinese) -> PublishError
    job_partial = tmp_path / 'jobs' / 'ep-partial'
    _make_render_job(job_partial, zh2='')
    with pytest.raises(publisher.PublishError, match='译文'):
        publisher.publish_job(job_partial, library)

    # 3) Positive: ad segment without chinese is skipped and published
    job_ad = tmp_path / 'jobs' / 'ep-ad-ok'
    _make_render_job(job_ad, include_ad=True)
    res = publisher.publish_job(job_ad, library)
    tr = json.loads((Path(res['episode_dir']) / 'transcript_zh.json').read_text(encoding='utf-8'))
    assert [seg['id'] for seg in tr['segments']] == ['seg-1', 'seg-2']


def test_republish_objects_refresh_and_stale_and_point_listen_stripping(tmp_path):
    library = tmp_path / 'library'
    job = tmp_path / 'jobs' / 'ep-repub'
    _make_render_job(job)
    tr_text = '第一段中文译稿内容足够长\n第二段中文译稿内容足够长'
    th64 = hashlib.sha256(tr_text.encode('utf-8')).hexdigest()
    _write_json(job / 'extract.json', _deep_packet(job.name, th64))
    _write_json(job / 'extract.live.json', _deep_packet(job.name, th64, packet_id='pkt-live'))

    first = publisher.publish_job(job, library)
    first_dir = Path(first['episode_dir'])
    assert (first_dir / 'point-listen.json').is_file()

    # 4a: non-render same-audio refresh on objects/<old_sha>
    job_refresh = tmp_path / 'jobs_refresh' / job.name
    job_refresh.mkdir(parents=True)
    (job_refresh / 'output.mp3').write_bytes((job / 'output.mp3').read_bytes())
    _write_json(job_refresh / 'state.json', {'title': '刷新标题'})
    refreshed = publisher.publish_job(job_refresh, library)
    ref_dir = Path(refreshed['episode_dir'])
    assert refreshed['media_revision']['previous_path'] == first['media_revision']['path']
    assert first_dir.is_dir()
    assert (ref_dir / 'point-listen.json').is_file()

    # 4b: render republish with same transcript text -> extract stays ready, point-listen kept
    (job / 'extract.json').unlink()
    (job / 'extract.live.json').unlink()
    _write_json(job / 'state.json', {'title': '再次渲染同译稿'})
    same_tr = publisher.publish_job(job, library)
    same_dir = Path(same_tr['episode_dir'])
    assert json.loads((same_dir / 'extract.json').read_text(encoding='utf-8'))['status'] == 'ready'
    assert json.loads((same_dir / 'extract.live.json').read_text(encoding='utf-8'))['status'] == 'ready'
    assert (same_dir / 'point-listen.json').is_file()

    # 4c-1: changed transcript text -> extract stale(transcript_hash_changed), point-listen stripped
    _make_render_job(job, zh1='修改后的第一段译稿内容足够长')
    changed_tr = publisher.publish_job(job, library)
    chg_dir = Path(changed_tr['episode_dir'])
    assert json.loads((chg_dir / 'extract.json').read_text(encoding='utf-8'))['stale_reason'] == 'transcript_hash_changed'
    assert not (chg_dir / 'point-listen.json').exists()

    # 4c-2: same transcript text but audio_changed=True -> extract stale(audio_changed), point-listen stripped
    _make_render_job(job, freq=880, zh1='修改后的第一段译稿内容足够长')
    new_th = compute_transcript_hash('修改后的第一段译稿内容足够长\n第二段中文译稿内容足够长')
    _write_json(job / 'extract.json', _deep_packet(job.name, new_th))
    publisher.publish_job(job, library)
    (job / 'extract.json').unlink()
    _make_render_job(job, freq=660, zh1='修改后的第一段译稿内容足够长')
    changed_audio = publisher.publish_job(job, library)
    ca_dir = Path(changed_audio['episode_dir'])
    assert json.loads((ca_dir / 'extract.json').read_text(encoding='utf-8'))['stale_reason'] == 'audio_changed'
    assert not (ca_dir / 'point-listen.json').exists()


def test_legacy_episodes_refresh_point_listen_keep_and_strip_r5(tmp_path):
    """R5 positive A, positive B, and negative (transcript changed -> unlink legacy point-listen.json)."""
    library = tmp_path / 'library'
    job = tmp_path / 'jobs' / 'ep-legacy-r5'
    job.mkdir(parents=True)
    _, mp3 = _make_wav_and_mp3(job, freq=440, seconds=4)
    audio_sha = sha256_file(mp3)
    tr_data = {
        'timeline': 'output.mp3.seconds',
        'audio_sha256': audio_sha,
        'segments': [
            {'id': 's1', 'zh': '第一段中文译稿内容足够长', 't_start': 0.0, 't_end': 2.0},
            {'id': 's2', 'zh': '第二段中文译稿内容足够长', 't_start': 2.0, 't_end': 4.0},
        ],
    }
    _write_json(job / 'transcript_zh.json', tr_data)
    th = compute_transcript_hash('第一段中文译稿内容足够长\n第二段中文译稿内容足够长')
    pkt = _deep_packet(job.name, th)
    _write_json(job / 'extract.json', pkt)
    pl_v1 = {
        'schema_version': 1, 'work_id': job.name, 'packet_id': pkt['packet_id'],
        'transcript_hash': th, 'audio_sha256': audio_sha, 'timeline': 'output.mp3.seconds',
        'viewpoints': [
            {'id': 'claim-1', 'viewpoint_text': '第一条深度提炼观点文本足够长', 'span': '第一段中文译稿内容足够长',
             't_start': 0.0, 't_end': 1.5, 'source_kind': 'claim', 'source_point_id': '1'},
            {'id': 'method-1', 'viewpoint_text': '实操方法一：按照第二段中文译稿执行步骤', 'span': '第二段中文译稿内容足够长',
             't_start': 2.0, 't_end': 3.5, 'source_kind': 'method', 'source_point_id': '1'},
        ],
    }
    _write_json(job / 'point-listen.json', pl_v1)
    publisher.publish_job(job, library)
    legacy = library / 'episodes' / job.name
    assert (legacy / 'point-listen.json').is_file()

    # Positive A: same audio, same transcript, job/ omits point-listen.json -> keep legacy point-listen.json
    (job / 'point-listen.json').unlink()
    (job / 'extract.json').unlink()
    _write_json(job / 'state.json', {'title': '正例A标题'})
    publisher.publish_job(job, library)
    assert json.loads((legacy / 'extract.json').read_text(encoding='utf-8'))['status'] == 'ready'
    assert (legacy / 'point-listen.json').is_file()
    assert json.loads((legacy / 'point-listen.json').read_text(encoding='utf-8'))['viewpoints'][0]['t_end'] == 1.5

    # Positive B: job/ provides updated valid point-listen.json -> legacy point-listen.json updated
    pl_v2 = {**pl_v1, 'viewpoints': [{**pl_v1['viewpoints'][0], 't_end': 1.8}, pl_v1['viewpoints'][1]]}
    _write_json(job / 'point-listen.json', pl_v2)
    publisher.publish_job(job, library)
    assert json.loads((legacy / 'point-listen.json').read_text(encoding='utf-8'))['viewpoints'][0]['t_end'] == 1.8

    # Negative: transcript changed -> extract stale, stage stripped, legacy/point-listen.json unlinked
    (job / 'point-listen.json').unlink()
    _write_json(job / 'transcript_zh.json', {
        **tr_data,
        'segments': [
            {'id': 's1', 'zh': '改稿后的第一段中文译稿内容', 't_start': 0.0, 't_end': 2.0},
            tr_data['segments'][1],
        ],
    })
    publisher.publish_job(job, library)
    assert json.loads((legacy / 'extract.json').read_text(encoding='utf-8'))['status'] == 'stale'
    assert not (legacy / 'point-listen.json').exists()


def test_point_listen_contracts_and_materialization_signature(tmp_path):
    # 3b: normalize_transcript_hash strict 16/64
    assert normalize_transcript_hash('A' * 16) == 'a' * 16
    assert normalize_transcript_hash('F' * 64) == 'f' * 16
    for bad in ('a' * 15, 'a' * 17, 'a' * 32, 'g' * 16, '', None):
        assert normalize_transcript_hash(bad) is None

    # 3a & 3c: refuse unlabelled timeline / source.seconds; method without text & id prefix
    audio_sha = 'b' * 64
    th = compute_transcript_hash('第一段中文译稿内容足够长\n第二段中文译稿内容足够长')
    pkt = _deep_packet('ep-1', th)
    src_summary = validate_transcript_zh(json.dumps({
        'timeline': 'source.seconds', 'audio_sha256': audio_sha,
        'segments': [
            {'id': 's1', 'zh': '第一段中文译稿内容足够长', 't_start': 0.0, 't_end': 1.5},
            {'id': 's2', 'zh': '第二段中文译稿内容足够长', 't_start': 1.5, 't_end': 3.0},
        ],
    }).encode('utf-8'))
    assert build_point_listen_from_bound_segments(
        episode_id='ep-1', audio_sha256=audio_sha, duration_sec=4.0,
        transcript_summary=src_summary, packet=pkt,
    ) is None

    out_summary = validate_transcript_zh(json.dumps({
        'timeline': 'output.mp3.seconds', 'audio_sha256': audio_sha,
        'segments': [
            {'id': 's1', 'zh': '第一段中文译稿内容足够长', 't_start': 0.0, 't_end': 1.5},
            {'id': 's2', 'zh': '第二段中文译稿内容足够长', 't_start': 1.5, 't_end': 3.0},
        ],
    }).encode('utf-8'))
    built = build_point_listen_from_bound_segments(
        episode_id='ep-1', audio_sha256=audio_sha, duration_sec=4.0,
        transcript_summary=out_summary, packet=pkt,
    )
    assert built is not None
    assert [vp['id'] for vp in built['viewpoints']] == ['claim-1', 'method-1']

    # 3d: duration_sec fail-closed
    raw_pl = json.dumps(built).encode('utf-8')
    for bad_dur in (None, 0, -1, float('nan')):
        with pytest.raises(SidecarError):
            validate_point_listen(raw_pl, episode_id='ep-1', audio_sha256=audio_sha,
                                  duration_sec=bad_dur, extract_packets=[pkt])

    # 8c: duration_sec is required keyword-only argument
    doc = {'segments': [{'id': 's1', 'chinese': '第一段', 'start_ms': 0, 'end_ms': 1000}]}
    with pytest.raises(TypeError):
        materialize_transcript_zh_from_document(doc, {}, audio_sha256=audio_sha)  # type: ignore[call-arg]


def test_v2_snapshot_l1079_and_fail_closed_and_read_translations(tmp_path, monkeypatch):
    library = tmp_path / 'library'
    library.mkdir()
    listing = library / 'library.json'
    v2_init = {
        'schema': publisher.LIBRARY_SCHEMA,
        'generation': 4,
        'episodes': [{'id': 'old-1', 'title': 'Old', 'created_at': '2026-11-20T00:00:00+00:00'}],
    }
    _write_json(listing, v2_init)
    before_bytes = listing.read_bytes()

    # 6c-2: _write_snapshot old_index=None fail-closed on non-empty v2 catalog
    with pytest.raises(publisher.PublishError, match='基线字节'):
        publisher._write_snapshot(listing, v2_init, {'old-1': v2_init['episodes'][0]}, old_index=None)
    assert listing.read_bytes() == before_bytes

    # 6c-1: non-render first publish on v2 catalog passes old_index at L1079
    job = tmp_path / 'jobs' / 'ep-v2-plain'
    job.mkdir(parents=True)
    (job / 'output.mp3').write_bytes(b'plain-v2')
    _write_json(job / 'transcript_zh.json', {'segments': [{'id': 's1', 'zh': '译稿一行'}]})
    publisher.publish_job(job, library)
    after = json.loads(listing.read_bytes())
    assert after['generation'] == 5
    assert after['parent'] == original_pending.digest(before_bytes)

    # 6c-3: _read_library_index error translations
    broken_listing = tmp_path / 'broken.json'
    broken_listing.write_bytes(b'\xff\xfe')
    with pytest.raises(publisher.PublishError, match='索引格式无效'):
        publisher._read_library_index(broken_listing)

    monkeypatch.setattr(publisher, '_read_library_bytes', lambda p: (_ for _ in ()).throw(PermissionError('denied')))
    with pytest.raises(publisher.PublishError, match='无法读取现有资料库索引'):
        publisher._read_library_index(listing)
