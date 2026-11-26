"""Targeted tests for incremental backfill, batch filtering, materialization, evidence checks, and restore."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import shengjian_library_snapshot as lib_snap
import studio_backfill_sidecars as backfill
import studio_original_pending as original_pending
import studio_publish_icloud as publisher
from studio_recovery import fingerprint
from studio_sidecars import compute_transcript_hash


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _make_object_package(library: Path, ep_id: str, audio_bytes: bytes, *, doc: dict | None = None,
                         render_id: dict | None = None) -> tuple[str, dict]:
    stage = library / '.tmp-pkg'
    stage.mkdir(parents=True, exist_ok=True)
    (stage / 'output.mp3').write_bytes(audio_bytes)
    _write_json(stage / 'episode.json', {'id': ep_id, 'title': ep_id})
    _write_json(stage / 'document.json', doc if doc is not None else {})
    _write_json(stage / 'source-identity.json', {})
    _write_json(stage / 'render-identity.json', render_id if render_id is not None else {})
    files = {p.name: original_pending.file_digest(p) for p in sorted(stage.iterdir())}
    sha = original_pending.digest(original_pending.json_bytes(files))
    final = library / 'objects' / sha
    final.parent.mkdir(parents=True, exist_ok=True)
    stage.rename(final)
    return sha, {
        'sha256': sha,
        'path': f'objects/{sha}',
        'previous_path': '',
        'resume_policy': 'reset_unmapped',
        'files': files,
    }


def test_d3_order_preservation_cas_double_check_and_rollback(tmp_path, monkeypatch):
    library = tmp_path / 'library'
    library.mkdir()
    sha1, rev1 = _make_object_package(library, 'ep-a', b'audio-a')
    sha2, rev2 = _make_object_package(library, 'ep-b', b'audio-b')
    # Intentionally non-chronological order (like mostprise in live catalog)
    catalog = {
        'schema': publisher.LIBRARY_SCHEMA,
        'generation': 4,
        'episodes': [
            {'id': 'ep-a', 'title': 'A', 'created_at': '2026-11-19T00:00:00+00:00',
             'show': 'Show A', 'media_revision': rev1},
            {'id': original_pending.EPISODE_ID, 'title': 'Pending', 'created_at': '2026-11-23T00:00:00+00:00'},
            {'id': 'ep-b', 'title': 'B', 'created_at': '2026-11-21T00:00:00+00:00',
             'lens_ids': ['health'], 'media_revision': rev2},
        ],
    }
    listing = library / 'library.json'
    _write_json(listing, catalog)
    orig_bytes = listing.read_bytes()
    orig_sha = original_pending.digest(orig_bytes)
    orig_copy = tmp_path / 'library.json.orig'
    orig_copy.write_bytes(orig_bytes)

    # 9d: refuse mostprise
    tr_bytes = json.dumps({'segments': [{'id': 's1', 'zh': '译稿正文'}]}).encode('utf-8')
    with pytest.raises(backfill.BackfillError, match='pending_review'):
        backfill.apply_incremental_backfill(
            library, expected_catalog_sha256=orig_sha,
            target_sidecars={original_pending.EPISODE_ID: {'transcript_zh.json': tr_bytes}},
        )

    # 6a: update only ep-a; order, ep-b, and mostprise untouched
    res = backfill.apply_incremental_backfill(
        library,
        expected_catalog_sha256=orig_sha,
        target_sidecars={'ep-a': {'transcript_zh.json': tr_bytes}},
        tx_id='tx-1',
        request_digest='r' * 64,
    )
    assert res['generation'] == 5
    assert res['parent'] == orig_sha
    after = json.loads(listing.read_bytes())
    assert [r['id'] for r in after['episodes']] == ['ep-a', original_pending.EPISODE_ID, 'ep-b']
    assert after['episodes'][1] == catalog['episodes'][1]
    assert after['episodes'][2] == catalog['episodes'][2]
    assert (library / 'objects' / sha1).is_dir()

    # 10a: logical rollback keeps generation monotonic
    rb = backfill.rollback_batch(
        library, expected_live_sha256=res['catalog_sha256'],
        old_revisions={'ep-a': rev1}, tx_id='tx-rb', request_digest='r' * 64,
    )
    assert rb['generation'] == 6
    assert json.loads(listing.read_bytes())['episodes'][0]['media_revision'] == rev1

    # 10b: emergency single-file restore-catalog preserves inbox/ and episodes/
    (library / 'inbox').mkdir()
    (library / 'inbox' / 'new-link.json').write_text('{"url":"https://example.com"}', encoding='utf-8')
    (library / 'episodes' / 'ep-a').mkdir(parents=True)
    (library / 'episodes' / 'ep-a' / 'extra.json').write_text('{}', encoding='utf-8')
    backfill.restore_catalog_from_orig(
        library,
        orig_path=orig_copy,
        expected_live_sha256=rb['catalog_sha256'],
        expected_orig_sha256=orig_sha,
    )
    assert listing.read_bytes() == orig_bytes
    assert (library / 'inbox' / 'new-link.json').is_file()
    assert (library / 'episodes' / 'ep-a' / 'extra.json').is_file()

    # 6b: pre-write double-check aborts on external modification
    real_read = original_pending.read_bytes
    calls = {'n': 0}

    def tampered_read(path):
        data = real_read(path)
        if path == listing:
            calls['n'] += 1
            if calls['n'] == 2:
                return data + b' '
        return data

    monkeypatch.setattr(original_pending, 'read_bytes', tampered_read)
    with pytest.raises(backfill.BackfillError, match='已改变'):
        backfill.apply_incremental_backfill(
            library, expected_catalog_sha256=orig_sha,
            target_sidecars={'ep-a': {'transcript_zh.json': tr_bytes}},
        )


def test_batch1_extract_filtering_and_batch2_materialization(tmp_path):
    # 8a: Batch 1 independent per-file extract validation
    ep_dir = tmp_path / 'episodes' / 'ep-2744'
    ep_dir.mkdir(parents=True)
    _write_json(ep_dir / 'extract.json', {
        'packet_id': 'p-light', 'work_id': 'ep-2744', 'mode': 'light', 'status': 'ready',
    })
    _write_json(ep_dir / 'extract.live.json', {
        'packet_id': 'p-deep', 'work_id': 'ep-2744', 'mode': 'deep', 'status': 'ready',
        'transcript_hash': 'a' * 16, 'claims': [{'id': 'c1', 'text': '真实有效深度观点正文'}],
    })
    accepted = backfill.filter_batch1_extract_files(ep_dir, episode_id='ep-2744')
    assert list(accepted) == ['extract.live.json']

    # 8b: Batch 2 5-point cross-check vs source.seconds fallback
    pkg = tmp_path / 'pkg'
    pkg.mkdir()
    audio_sha = 'c' * 64
    doc = {'segments': [
        {'id': 's1', 'start_ms': 0, 'end_ms': 1000, 'text': 'Hello', 'chinese': '你好世界'},
        {'id': 'ad', 'start_ms': 1000, 'end_ms': 1500, 'is_ad': True},
    ]}
    _write_json(pkg / 'document.json', doc)
    _write_json(pkg / 'render-identity.json', {
        'schema_version': 1, 'sample_rate': 24000, 'audio_sha256': audio_sha,
        'document_sha256': fingerprint(doc),
        'timeline': [{'id': 's1', 'start_frame': 0, 'end_frame': 24000}],
    })
    _, meta_ok = backfill.materialize_batch2_transcript(pkg, audio_sha256=audio_sha, duration_sec=1.0)
    assert meta_ok['timeline'] == 'output.mp3.seconds'

    # Mismatched duration (> 0.12s) -> fallback to source.seconds
    _, meta_dur = backfill.materialize_batch2_transcript(pkg, audio_sha256=audio_sha, duration_sec=2.0)
    assert meta_dur['timeline'] == 'source.seconds'

    # Tampered document -> document_sha256 mismatch -> fallback to source.seconds
    doc_tampered = {'segments': [{**doc['segments'][0], 'chinese': '被篡改'}, doc['segments'][1]]}
    _write_json(pkg / 'document.json', doc_tampered)
    _, meta_doc = backfill.materialize_batch2_transcript(pkg, audio_sha256=audio_sha, duration_sec=1.0)
    assert meta_doc['timeline'] == 'source.seconds'


def test_batch3_evidence_snapshot_without_mp3_and_source_axis_b_side(tmp_path):
    # 9a/9c: snapshot without .mp3 verified via manifest.json + library.json; source-axis long transcript allowed
    snap_lib = tmp_path / 'snap_src' / 'roots' / 'library'
    ep_id = 'ep-ev-1'
    (snap_lib / 'episodes' / ep_id).mkdir(parents=True)
    playing_sha = 'd' * 64
    tr_payload = {
        'segments': [{'id': 's1', 'zh': '英文源轴长于成片的正文', 't_start': 0.0, 't_end': 500.0}]
    }
    tr_path = snap_lib / 'episodes' / ep_id / 'transcript_zh.json'
    _write_json(tr_path, tr_payload)
    _write_json(snap_lib / 'library.json', {
        'episodes': [{'id': ep_id, 'media_revision': {'files': {'output.mp3': playing_sha}}}]
    })
    snap_dest = tmp_path / 'meta-snap'
    lib_snap.snapshot(snap_dest, library=snap_lib, label='test-ev')

    res = backfill.verify_evidence_transcript(
        snap_dest / 'roots' / 'library' / 'episodes' / ep_id / 'transcript_zh.json',
        playing_audio_sha=playing_sha,
        snapshot_manifest_path=snap_dest / 'manifest.json',
        snapshot_catalog_path=snap_dest / 'roots' / 'library' / 'library.json',
        episode_id=ep_id,
    )
    assert res['proof_path'] == 'path2_snapshot_manifest'
    assert res['is_output_axis'] is False

    # Tamper 1 byte -> manifest mismatch -> BackfillError
    snap_tr = snap_dest / 'roots' / 'library' / 'episodes' / ep_id / 'transcript_zh.json'
    snap_tr.write_bytes(snap_tr.read_bytes() + b' ')
    with pytest.raises(backfill.BackfillError):
        backfill.verify_evidence_transcript(
            snap_tr,
            playing_audio_sha=playing_sha,
            snapshot_manifest_path=snap_dest / 'manifest.json',
            snapshot_catalog_path=snap_dest / 'roots' / 'library' / 'library.json',
            episode_id=ep_id,
        )

    # 9b: Path 3 & condition (b) extract transcript_hash check
    th = compute_transcript_hash('英文源轴长于成片的正文')
    ok_p3 = backfill.verify_evidence_transcript(
        tr_path, playing_audio_sha=playing_sha, extract_packet={'transcript_hash': th + '0' * 48},
    )
    assert ok_p3['proof_path'] == 'path3_extract_hash'
    with pytest.raises(backfill.BackfillError, match='条件 b'):
        backfill.verify_evidence_transcript(
            tr_path, playing_audio_sha=playing_sha, extract_packet={'transcript_hash': 'f' * 16},
        )
