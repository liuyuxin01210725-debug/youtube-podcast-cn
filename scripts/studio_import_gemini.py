"""Accept an existing Gemini web export into a separate, unqueued Studio job."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory

from studio_recovery import (
    StudioRecoveryError, _read_regular_bytes, _save_bytes, decode_json,
    decode_json_object, podcast_job_lock,
)
from temporary_audio import sha256_file


# Shelf Help's paid first chunk is unresolved (safe_to_retry=false).
# Link a separate web recovery to the lost response for traceability.
_PAID_SOURCE_SHA256 = '2e8df2585808525d13fe5aff030afe8a732562ec0f5b11be66cc5ba6558b4e4a'
_PAID_SEGMENT_IDS = {f'seg-{number:06d}' for number in range(2, 92)}
_RECOVERY_FIELD = 'recovery_of_missing_response'
_MISSING_RESPONSE = {
    'request_key': 'a257fa8fd7b4a33f1d51ebcbc7be51a142415fa479b28eb57cfce98f88101b48',
    'provider_response_id': '61a6ce7d-9b84-48a0-bf72-6d9881f1f406',
}


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _segment_keys(rows, fields):
    if not isinstance(rows, list) or not rows:
        raise ValueError('分段须为非空数组')
    keys = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or (fields is not None and set(row) != fields):
            raise ValueError('分段字段无法核验')
        ident, speaker = row.get('id'), row.get('speaker_id')
        if not all(isinstance(value, str) and value.strip() for value in (ident, speaker)):
            raise ValueError('分段缺少有效 ID 或说话人')
        if ident in ids:
            raise ValueError('分段 ID 重复')
        ids.add(ident)
        keys.append((ident, speaker))
    return keys


def _require_unqueued(job):
    state = job / 'state.json'
    if state.exists() or state.is_symlink():
        raise ValueError('接收只允许写入未入队的隔离 job')


def _source_version(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('请使用隔离 job 内的常规音源文件')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _append_history(document, receipt):
    previous = document.get('translation_imports', [])
    if not isinstance(previous, list):
        raise ValueError('已有接收历史格式无效')
    history = [document.get('translation_import'), *previous]
    imported = set()
    for item in history:
        if (not isinstance(item, dict) or item.keys() - {_RECOVERY_FIELD} != receipt.keys()
                or type(item['schema_version']) is not int
                or any(item[key] != receipt[key] for key in (
                    'schema_version', 'provider', 'display_model', 'official_model_id', 'source_sha256',
                ))):
            raise ValueError('追加须使用与既有 Gemini 接收记录同源的接收稿')
        ids = item['segment_ids']
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(ident, str) or not ident for ident in ids)
                or len(set(ids)) != len(ids) or imported.intersection(ids)):
            raise ValueError('已有接收历史段落无效')
        if _RECOVERY_FIELD in item and (
            item[_RECOVERY_FIELD] != _MISSING_RESPONSE
            or item['source_sha256'] != _PAID_SOURCE_SHA256
            or not _PAID_SEGMENT_IDS.issuperset(ids)
        ):
            raise ValueError('已有丢失正文恢复记录无效')
        imported.update(ids)
    return history, imported


def _replace_document(output, raw, verify_inputs):
    # Hold the old inode so a directory-sync error can restore it without
    # writing another copy of the document.
    with TemporaryDirectory(prefix='.gemini-rollback-', dir=output.parent) as directory:
        backup = Path(directory) / output.name
        os.link(output, backup, follow_symlinks=False)
        ready_to_install = False

        def before_install():
            nonlocal ready_to_install
            verify_inputs()
            ready_to_install = True

        try:
            _save_bytes(output, raw, before_install=before_install)
        except BaseException:
            if ready_to_install and output.exists() and _read_regular_bytes(output) == raw:
                os.replace(backup, output)
            raise


def receive(job, base_document, source_audio, manifest_path, export_path, *,
            revise=False, recovery_of_missing_response=False):
    """Receive an export, using exact old_chinese in revision manifest rows."""
    job = job.resolve(strict=True)
    _require_unqueued(job)
    output = job / 'document.json'
    append = base_document is None
    if (revise and recovery_of_missing_response) or (
        not append and (revise or recovery_of_missing_response)
    ):
        raise ValueError('修订或恢复须单独针对现有接收稿')
    if append:
        base_document = output
    elif base_document.resolve() == output:
        raise ValueError('基础稿与接收目标必须分开')
    if not source_audio.is_absolute():
        source_audio = job / source_audio
    if source_audio.parent.resolve() != job:
        raise ValueError('请使用隔离 job 内的常规音源文件')
    source_version = _source_version(source_audio)
    source_lock_path = job / 'source-identity.json'
    source_lock_raw = _read_regular_bytes(source_lock_path)
    source_lock = decode_json_object(source_lock_raw)
    base_raw = _read_regular_bytes(base_document)

    manifest_raw = _read_regular_bytes(manifest_path)
    manifest = decode_json_object(manifest_raw)
    if (set(manifest) != {'schema_version', 'document_sha256', 'source_sha256',
                          'export_sha256', 'segments'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1):
        raise ValueError('接收清单格式无效')
    segment_fields = {'id', 'speaker_id'}
    if revise:
        segment_fields.add('old_chinese')
    expected = _segment_keys(manifest['segments'], segment_fields)
    export_raw = _read_regular_bytes(export_path)
    if _sha256(export_raw) != manifest['export_sha256']:
        raise ValueError('网页导出 SHA-256 与接收清单不一致')
    rows = decode_json(export_raw.decode('utf-8'))
    if _segment_keys(rows, {'id', 'speaker_id', 'zh'}) != expected:
        raise ValueError('网页译稿段数、ID、顺序或说话人与接收清单不一致')
    for row in rows:
        if not isinstance(row['zh'], str) or not row['zh'].strip():
            raise ValueError('网页译稿含空译或非法译文')

    document = decode_json_object(base_raw)
    if type(document.get('schema_version')) is not int or document['schema_version'] != 1:
        raise ValueError('基础稿须为工作室第一版 document.json')
    if not append and ('translation_import' in document or 'translation_imports' in document):
        raise ValueError('请使用原始基础稿，不能用已接收的外部译稿作为基础稿')
    source_keys = _segment_keys(document.get('segments'), None)
    translated = {row['id']: row['zh'] for row in rows}
    selected = [key for key in source_keys if key[0] in translated]
    if selected != expected:
        raise ValueError('接收清单与基础稿段落、顺序或说话人不一致')

    selected_segments = [segment for segment in document['segments'] if segment['id'] in translated]
    if any(segment.get('is_ad') for segment in selected_segments):
        raise ValueError('接收清单包含广告段')
    receipt = {
        'schema_version': 1,
        'provider': 'gemini_app_manual',
        'display_model': 'Neon 高',
        'official_model_id': None,
        'document_sha256': manifest['document_sha256'],
        'source_sha256': manifest['source_sha256'],
        'export_sha256': manifest['export_sha256'],
        'manifest_sha256': _sha256(manifest_raw),
        'segment_ids': [ident for ident, _ in expected],
    }
    paid_source = receipt['source_sha256'] == _PAID_SOURCE_SHA256
    if recovery_of_missing_response:
        if not paid_source or not _PAID_SEGMENT_IDS.issuperset(translated):
            raise ValueError('丢失正文恢复只允许 Shelf Help 付费首块段落')
    elif not revise and paid_source and _PAID_SEGMENT_IDS.intersection(translated):
        raise ValueError('接收清单包含已隔离的付费首块段落')

    def verify_inputs():
        if (source_lock.get('sha256') != receipt['source_sha256']
                or _source_version(source_audio) != source_version
                or sha256_file(source_audio) != receipt['source_sha256']
                or _source_version(source_audio) != source_version
                or _read_regular_bytes(source_lock_path) != source_lock_raw):
            raise ValueError('音源或来源锁 SHA-256 与接收清单不一致，或接收期间已改变')
        _require_unqueued(job)
        if _read_regular_bytes(base_document) != base_raw:
            raise ValueError('接收前基础稿已改变')

    target_matches = all(segment.get('chinese') == translated[segment['id']]
                         for segment in selected_segments)
    if append:
        history, imported = _append_history(document, receipt)
        if revise:
            revisions = document.get('translation_revisions', [])
            if not isinstance(revisions, list):
                raise ValueError('已有修订历史格式无效')
            receipt['previous_chinese'] = {row['id']: row['old_chinese'] for row in manifest['segments']}
            replay_history = revisions
        else:
            if recovery_of_missing_response:
                receipt[_RECOVERY_FIELD] = dict(_MISSING_RESPONSE)
            replay_history = history[1:]
        # A recorded replay is read-only even though it uses the original CAS.
        if receipt in replay_history and target_matches:
            with podcast_job_lock(job):
                verify_inputs()
            return {'status': 'unchanged', 'imported_document_sha256': _sha256(base_raw), **receipt}
    if _sha256(base_raw) != receipt['document_sha256']:
        raise ValueError('基础 document.json SHA-256 与接收清单不一致')
    if revise:
        for segment in selected_segments:
            old = receipt['previous_chinese'][segment['id']]
            if not isinstance(old, str) or not old.strip() or segment.get('chinese') != old:
                raise ValueError('修订清单与段落旧译不一致或未填中文')
        if target_matches:
            with podcast_job_lock(job):
                verify_inputs()
            return {'status': 'unchanged', 'imported_document_sha256': _sha256(base_raw), **receipt}
        document.setdefault('translation_revisions', []).append(receipt)
    elif append:
        if imported.intersection(translated) or any(
            not isinstance(segment.get('chinese'), str) or segment['chinese'].strip()
            for segment in selected_segments
        ):
            raise ValueError('追加清单包含已导入或已有中文的段落')
        document.setdefault('translation_imports', []).append(receipt)
    else:
        document['translation_import'] = receipt
    for segment in selected_segments:
        segment['chinese'] = translated[segment['id']]
    raw = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')

    with podcast_job_lock(job):
        _require_unqueued(job)
        if append:
            _replace_document(output, raw, verify_inputs)
        else:
            try:
                prior = _read_regular_bytes(output)
            except FileNotFoundError:
                prior = None
            if prior is not None:
                if prior != raw:
                    raise ValueError('目标已有不同译稿或接收记录，原文件已保留')
                verify_inputs()
                return {'status': 'unchanged', 'imported_document_sha256': _sha256(raw), **receipt}
            try:
                _save_bytes(output, raw, exclusive=True, before_install=verify_inputs)
            except FileExistsError:
                raise ValueError('目标已由其他写入者创建，原文件已保留') from None
            except OSError:
                # Directory sync can fail after the atomic link.
                if output.exists() and _read_regular_bytes(output) == raw:
                    output.unlink()
                raise
    return {'status': 'received', 'imported_document_sha256': _sha256(raw), **receipt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-dir', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--base-document', type=Path, help='首次接收使用的原始基础稿')
    mode.add_argument('--append', action='store_true', help='追加到该 job 当前的 document.json')
    mode.add_argument('--revise', action='store_true', help='按清单逐段核对 old_chinese 后修订现稿')
    mode.add_argument('--recovery-of-missing-response', action='store_true',
                      help='用网页译文恢复 Shelf Help 付费首块仍为空的指定段落')
    parser.add_argument('--source-audio', type=Path, required=True, help='隔离 job 根内的音源文件名')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--export', type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = receive(args.job_dir, args.base_document, args.source_audio, args.manifest, args.export,
                          revise=args.revise, recovery_of_missing_response=args.recovery_of_missing_response)
    except (OSError, ValueError, RecursionError, StudioRecoveryError) as error:
        print(f'接收拒绝：{error}', file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
