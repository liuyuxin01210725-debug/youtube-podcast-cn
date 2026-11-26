#!/usr/bin/env python3
"""Incremental sidecar backfill, batch rollback, and CAS catalog restore (D3-compliant)."""
from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import studio_original_pending as original_pending
from studio_publish_icloud import (
    AUDIO_NAME,
    LIBRARY_SCHEMA,
    _record_identity,
    _revision_files,
    atomic_write_json,
    decode_library_index,
    probe_duration_sec,
)
from studio_recovery import _sync_dir
from studio_sidecars import (
    EXTRACT_FILES,
    POINT_LISTEN_FILE,
    TRANSCRIPT_FILE,
    SidecarError,
    decode_json_strict,
    normalize_transcript_hash,
    validate_extract_packet,
    validate_point_listen,
    validate_transcript_zh,
    materialize_transcript_zh_from_document,
)


class BackfillError(ValueError):
    """Raised when an incremental backfill, rollback, or restore check fails."""


def _canonical_result_digest(payload_without_tx: dict) -> str:
    return original_pending.digest(
        json.dumps(
            payload_without_tx,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
            allow_nan=False,
        ).encode('utf-8')
    )


def _copy_old_object_files(old_dir: Path, stage: Path, expected_files: dict[str, str]) -> None:
    if not old_dir.is_dir() or old_dir.is_symlink():
        raise BackfillError(f'旧对象目录无效：{old_dir}')
    for name, expected_sha in expected_files.items():
        src = old_dir / name
        raw = original_pending.read_bytes(src)
        if original_pending.digest(raw) != expected_sha:
            raise BackfillError(f'旧对象文件哈希不匹配：{old_dir.name}/{name}')
        original_pending.write_bytes(stage / name, raw)


def filter_batch1_extract_files(episode_dir: Path, *, episode_id: str) -> dict[str, bytes]:
    """Independently validate extract.json and extract.live.json; keep only deep-ready packets."""
    accepted: dict[str, bytes] = {}
    for name in EXTRACT_FILES:
        path = episode_dir / name
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            raw = original_pending.read_bytes(path)
            info = validate_extract_packet(raw, episode_id=episode_id)
        except (OSError, original_pending.IntakeError, SidecarError):
            continue
        if info['is_deep_ready']:
            accepted[name] = raw
    return accepted


def materialize_batch2_transcript(
    package_dir: Path, *, audio_sha256: str, duration_sec: float | None
) -> tuple[bytes, dict]:
    """Materialize transcript_zh.json from published document.json and optional render-identity.json."""
    doc_raw = original_pending.read_bytes(package_dir / 'document.json')
    document = decode_json_strict(doc_raw)
    id_path = package_dir / 'render-identity.json'
    render_identity: dict | None = None
    if id_path.exists() or id_path.is_symlink():
        id_raw = original_pending.read_bytes(id_path)
        render_identity = decode_json_strict(id_raw)
    return materialize_transcript_zh_from_document(
        document,
        render_identity,
        audio_sha256=audio_sha256,
        duration_sec=duration_sec,
        strict_all_non_ad=True,
    )


def verify_evidence_transcript(
    transcript_path: Path,
    *,
    playing_audio_sha: str,
    extract_packet: dict | None = None,
    snapshot_manifest_path: Path | None = None,
    snapshot_catalog_path: Path | None = None,
    snapshot_audio_path: Path | None = None,
    episode_id: str | None = None,
) -> dict:
    """Verify an off-library evidence transcript_zh.json (Batch 3 rules)."""
    raw_tr = original_pending.read_bytes(transcript_path)
    summary = validate_transcript_zh(raw_tr)
    data = summary['data']
    tr_sha = original_pending.digest(raw_tr)
    proof_path: str | None = None

    # Condition (a) Path 1: transcript_zh.json itself carries matching audio_sha256.
    if summary['audio_sha256'] == playing_audio_sha:
        proof_path = 'path1_audio_sha256'

    # Condition (a) Path 2: snapshot audio or snapshot catalog+manifest matches playing_audio_sha.
    if proof_path is None and snapshot_audio_path is not None and snapshot_audio_path.is_file():
        if original_pending.file_digest(snapshot_audio_path) == playing_audio_sha:
            proof_path = 'path2_snapshot_audio'

    if (
        proof_path is None
        and snapshot_manifest_path is not None
        and snapshot_catalog_path is not None
        and episode_id is not None
    ):
        manifest = decode_json_strict(original_pending.read_bytes(snapshot_manifest_path))
        catalog_bytes = original_pending.read_bytes(snapshot_catalog_path)
        catalog_sha = original_pending.digest(catalog_bytes)
        raw_files = manifest.get('files')
        cat_entry = None
        tr_entry = None
        tr_rel = f'episodes/{episode_id}/transcript_zh.json'
        if isinstance(raw_files, list):
            for entry in raw_files:
                if not isinstance(entry, dict) or entry.get('root') != 'library':
                    continue
                if entry.get('rel') == 'library.json':
                    cat_entry = entry
                elif entry.get('rel') == tr_rel:
                    tr_entry = entry
        elif isinstance(raw_files, dict):
            cat_entry = raw_files.get('library.json')
            tr_entry = raw_files.get(tr_rel)
        if (
            isinstance(cat_entry, dict)
            and cat_entry.get('sha256') == catalog_sha
            and isinstance(tr_entry, dict)
            and tr_entry.get('sha256') == tr_sha
        ):
            _, snap_by_id = decode_library_index(catalog_bytes.decode('utf-8'))
            snap_row = snap_by_id.get(episode_id) or {}
            snap_rev = snap_row.get('media_revision') if isinstance(snap_row.get('media_revision'), dict) else {}
            snap_files = snap_rev.get('files') if isinstance(snap_rev.get('files'), dict) else {}
            if snap_files.get(AUDIO_NAME) == playing_audio_sha:
                proof_path = 'path2_snapshot_manifest'

    # Condition (a) Path 3 & Condition (b): matching ready extract.transcript_hash.
    tr_norm = normalize_transcript_hash(summary['transcript_hash'])
    if extract_packet is not None:
        ext_norm = normalize_transcript_hash(extract_packet.get('transcript_hash'))
        if ext_norm is None or tr_norm != ext_norm:
            raise BackfillError('evidence 译稿指纹与现网 extract.transcript_hash 不一致（条件 b 失败）')
        if proof_path is None:
            proof_path = 'path3_extract_hash'

    if proof_path is None:
        raise BackfillError('evidence 译稿无法证明与当前在播 output.mp3 同成片（条件 a 失败）')

    is_output_axis = data.get('timeline') == 'output.mp3.seconds' and summary['audio_sha256'] == playing_audio_sha
    return {
        'raw_bytes': raw_tr,
        'proof_path': proof_path,
        'transcript_hash': tr_norm,
        'is_output_axis': is_output_axis,
        'line_count': summary['line_count'],
    }


def apply_incremental_backfill(
    library: Path,
    *,
    expected_catalog_sha256: str,
    target_sidecars: dict[str, dict[str, bytes]],
    tx_id: str | None = None,
    request_digest: str | None = None,
) -> dict:
    """Incrementally bind validated sidecars into new objects/<new_sha>/ rows (D3)."""
    if original_pending.EPISODE_ID in target_sidecars:
        raise BackfillError('禁止对 pending_review 原音待核条目执行补绑')
    listing = library / 'library.json'
    with original_pending.library_lock(library):
        before = original_pending.read_bytes(listing)
        before_sha = original_pending.digest(before)
        if before_sha != expected_catalog_sha256:
            raise BackfillError('library.json 基线 SHA-256 不匹配，拒绝写入')
        prior, prior_by_id = decode_library_index(before.decode('utf-8'))
        prior_episodes = prior.get('episodes') or []
        prior_ids = [_record_identity(row) for row in prior_episodes]

        updated_rows: dict[str, dict] = {}
        skipped_ids: list[str] = []
        for ep_id, sidecar_map in target_sidecars.items():
            if ep_id not in prior_by_id:
                raise BackfillError(f'目标节目不在目录中：{ep_id}')
            old_row = prior_by_id[ep_id]
            old_rev = old_row.get('media_revision')
            if not isinstance(old_rev, dict):
                raise BackfillError(f'目标节目缺少 media_revision：{ep_id}')
            old_sha = old_rev.get('sha256')
            old_path = old_rev.get('path')
            old_files = old_rev.get('files')
            if not isinstance(old_sha, str) or old_path != f'objects/{old_sha}' or not isinstance(old_files, dict):
                raise BackfillError(f'目标节目修订指针无效：{ep_id}')

            old_dir = library / 'objects' / old_sha
            with tempfile.TemporaryDirectory(prefix=f'.backfill-{ep_id}-', dir=library) as tmp:
                stage = Path(tmp)
                _copy_old_object_files(old_dir, stage, old_files)
                for fname, raw_bytes in sidecar_map.items():
                    original_pending.write_bytes(stage / fname, raw_bytes)

                audio_sha = original_pending.file_digest(stage / AUDIO_NAME)
                if audio_sha != old_files.get(AUDIO_NAME):
                    raise BackfillError(f'补绑过程音频 SHA 发生变化：{ep_id}')

                # Validate staged sidecars before committing object directory.
                if (stage / TRANSCRIPT_FILE).exists():
                    validate_transcript_zh(
                        original_pending.read_bytes(stage / TRANSCRIPT_FILE),
                        expected_audio_sha256=audio_sha,
                    )
                extract_packets: list[dict] = []
                for ext_name in EXTRACT_FILES:
                    if (stage / ext_name).exists():
                        ext_info = validate_extract_packet(
                            original_pending.read_bytes(stage / ext_name),
                            episode_id=ep_id,
                        )
                        extract_packets.append(ext_info['packet'])
                if (stage / POINT_LISTEN_FILE).exists():
                    dur = old_row.get('duration_sec')
                    if dur is None:
                        dur = probe_duration_sec(stage / AUDIO_NAME)
                    validate_point_listen(
                        original_pending.read_bytes(stage / POINT_LISTEN_FILE),
                        episode_id=ep_id,
                        audio_sha256=audio_sha,
                        duration_sec=dur,
                        extract_packets=extract_packets,
                    )

                new_files = _revision_files(stage)
                new_sha = original_pending.digest(original_pending.json_bytes(new_files))
                if new_sha == old_sha:
                    skipped_ids.append(ep_id)
                    continue

                objects_dir = library / 'objects'
                objects_dir.mkdir(parents=True, exist_ok=True)
                final = objects_dir / new_sha
                if final.is_symlink() or (final.exists() and (not final.is_dir() or _revision_files(final) != new_files)):
                    raise BackfillError(f'目标对象目录冲突：{new_sha}')
                if not final.exists():
                    for name in new_files:
                        with original_pending.open_regular(stage / name) as asset:
                            os.fsync(asset.fileno())
                    _sync_dir(stage)
                    stage.rename(final)
                _sync_dir(objects_dir)

                new_row = copy.deepcopy(old_row)
                new_row['media_revision'] = {
                    'sha256': new_sha,
                    'path': f'objects/{new_sha}',
                    'previous_path': old_path,
                    'resume_policy': 'reset_unmapped',
                    'files': new_files,
                }
                updated_rows[ep_id] = new_row

        if not updated_rows:
            return {
                'changed': False,
                'skipped_ids': skipped_ids,
                'catalog_sha256': before_sha,
                'generation': prior.get('generation'),
            }

        new_episodes = []
        for row in prior_episodes:
            ident = _record_identity(row)
            if ident in updated_rows:
                new_episodes.append(updated_rows[ident])
            else:
                new_episodes.append(copy.deepcopy(row))

        prior_gen = prior['generation'] if type(prior.get('generation')) is int else 0
        new_gen = prior_gen + 1
        payload = {
            **prior,
            'schema': LIBRARY_SCHEMA,
            'generation': new_gen,
            'parent': before_sha,
            'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'episodes': new_episodes,
        }
        if tx_id and request_digest:
            body_for_digest = {k: v for k, v in payload.items() if k != 'transactions'}
            txs = dict(prior.get('transactions') or {}) if isinstance(prior.get('transactions'), dict) else {}
            txs[tx_id] = {
                'request_digest': request_digest,
                'generation': new_gen,
                'result_digest': _canonical_result_digest(body_for_digest),
            }
            payload['transactions'] = txs

        if original_pending.read_bytes(listing) != before:
            raise BackfillError('提交前目录已改变，中止写入')
        atomic_write_json(listing, payload)
        _sync_dir(library)

        after_bytes = original_pending.read_bytes(listing)
        after_prior, _ = decode_library_index(after_bytes.decode('utf-8'))
        after_ids = [_record_identity(r) for r in (after_prior.get('episodes') or [])]
        if after_ids != prior_ids:
            raise BackfillError('回读校验失败：目录行顺序发生改变')
        return {
            'changed': True,
            'updated_ids': list(updated_rows),
            'skipped_ids': skipped_ids,
            'catalog_sha256': original_pending.digest(after_bytes),
            'generation': new_gen,
            'parent': before_sha,
        }


def rollback_batch(
    library: Path,
    *,
    expected_live_sha256: str,
    old_revisions: dict[str, dict],
    tx_id: str | None = None,
    request_digest: str | None = None,
) -> dict:
    """Logically roll back target rows' media_revision while keeping generation monotonic."""
    listing = library / 'library.json'
    with original_pending.library_lock(library):
        before = original_pending.read_bytes(listing)
        before_sha = original_pending.digest(before)
        if before_sha != expected_live_sha256:
            raise BackfillError('回滚前 library.json 基线 SHA-256 不匹配')
        prior, prior_by_id = decode_library_index(before.decode('utf-8'))
        prior_episodes = prior.get('episodes') or []
        for ep_id in old_revisions:
            if ep_id not in prior_by_id:
                raise BackfillError(f'回滚目标不在目录中：{ep_id}')

        new_episodes = []
        for row in prior_episodes:
            ident = _record_identity(row)
            copied = copy.deepcopy(row)
            if ident in old_revisions:
                copied['media_revision'] = copy.deepcopy(old_revisions[ident])
            new_episodes.append(copied)

        prior_gen = prior['generation'] if type(prior.get('generation')) is int else 0
        new_gen = prior_gen + 1
        payload = {
            **prior,
            'schema': LIBRARY_SCHEMA,
            'generation': new_gen,
            'parent': before_sha,
            'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'episodes': new_episodes,
        }
        if tx_id and request_digest:
            body_for_digest = {k: v for k, v in payload.items() if k != 'transactions'}
            txs = dict(prior.get('transactions') or {}) if isinstance(prior.get('transactions'), dict) else {}
            txs[tx_id] = {
                'request_digest': request_digest,
                'generation': new_gen,
                'result_digest': _canonical_result_digest(body_for_digest),
            }
            payload['transactions'] = txs

        if original_pending.read_bytes(listing) != before:
            raise BackfillError('回滚提交前目录已改变，中止写入')
        atomic_write_json(listing, payload)
        _sync_dir(library)
        after_bytes = original_pending.read_bytes(listing)
        return {
            'catalog_sha256': original_pending.digest(after_bytes),
            'generation': new_gen,
            'parent': before_sha,
        }


def restore_catalog_from_orig(
    library: Path,
    *,
    orig_path: Path,
    expected_live_sha256: str,
    expected_orig_sha256: str,
) -> dict:
    """Emergency in-lock single-file CAS restore of library.json without touching episodes/objects/inbox."""
    listing = library / 'library.json'
    with original_pending.library_lock(library):
        live_before = original_pending.read_bytes(listing)
        if original_pending.digest(live_before) != expected_live_sha256:
            raise BackfillError('应急恢复前活库 SHA-256 不匹配，中止覆盖')
        orig_bytes = original_pending.read_bytes(orig_path)
        orig_sha = original_pending.digest(orig_bytes)
        if orig_sha != expected_orig_sha256:
            raise BackfillError('冻结副本 SHA-256 与基线不匹配')
        decode_library_index(orig_bytes.decode('utf-8'))

        tmp = library / f'.library.json.restore.{os.getpid()}.tmp'
        try:
            with tmp.open('xb') as handle:
                handle.write(orig_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            if original_pending.read_bytes(listing) != live_before:
                raise BackfillError('应急恢复提交前活库已改变，中止写入')
            os.replace(tmp, listing)
            _sync_dir(library)
        finally:
            tmp.unlink(missing_ok=True)

        after = original_pending.read_bytes(listing)
        if original_pending.digest(after) != expected_orig_sha256:
            raise BackfillError('应急恢复后回读 SHA-256 校验失败')
        return {'restored_sha256': expected_orig_sha256}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='旧集增量补绑与应急单文件恢复工具')
    sub = parser.add_subparsers(dest='command', required=True)
    rc = sub.add_parser('restore-catalog', help='锁内单文件 CAS 恢复 library.json')
    rc.add_argument('--library', type=Path, required=True)
    rc.add_argument('--orig', type=Path, required=True)
    rc.add_argument('--expected-live-sha256', required=True)
    rc.add_argument('--expected-orig-sha256', required=True)
    args = parser.parse_args(argv)
    if args.command == 'restore-catalog':
        result = restore_catalog_from_orig(
            args.library,
            orig_path=args.orig,
            expected_live_sha256=args.expected_live_sha256,
            expected_orig_sha256=args.expected_orig_sha256,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
