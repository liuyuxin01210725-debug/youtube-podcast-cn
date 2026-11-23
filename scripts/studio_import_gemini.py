"""Accept an existing Gemini web export into a separate, unqueued Studio job."""

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys

from studio_recovery import (
    StudioRecoveryError, _read_regular_bytes, _save_bytes, decode_json,
    decode_json_object, podcast_job_lock,
)
from temporary_audio import sha256_file


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


def receive(job, base_document, source_audio, manifest_path, export_path):
    job = job.resolve(strict=True)
    _require_unqueued(job)
    output = job / 'document.json'
    if base_document.resolve() == output:
        raise ValueError('基础稿与接收目标必须分开')
    if not source_audio.is_absolute():
        source_audio = job / source_audio
    if source_audio.parent.resolve() != job or not stat.S_ISREG(source_audio.lstat().st_mode):
        raise ValueError('请使用隔离 job 内的常规音源文件')

    manifest_raw = _read_regular_bytes(manifest_path)
    manifest = decode_json_object(manifest_raw)
    if (set(manifest) != {'schema_version', 'document_sha256', 'source_sha256',
                          'export_sha256', 'segments'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1):
        raise ValueError('接收清单格式无效')
    expected = _segment_keys(manifest['segments'], {'id', 'speaker_id'})
    export_raw = _read_regular_bytes(export_path)
    if _sha256(export_raw) != manifest['export_sha256']:
        raise ValueError('网页导出 SHA-256 与接收清单不一致')
    rows = decode_json(export_raw.decode('utf-8'))
    if _segment_keys(rows, {'id', 'speaker_id', 'zh'}) != expected:
        raise ValueError('网页译稿段数、ID、顺序或说话人与接收清单不一致')
    for row in rows:
        if not isinstance(row['zh'], str) or not row['zh'].strip():
            raise ValueError('网页译稿含空译或非法译文')

    base_raw = _read_regular_bytes(base_document)
    if _sha256(base_raw) != manifest['document_sha256']:
        raise ValueError('基础 document.json SHA-256 与接收清单不一致')
    document = decode_json_object(base_raw)
    if type(document.get('schema_version')) is not int or document['schema_version'] != 1:
        raise ValueError('基础稿须为工作室第一版 document.json')
    if 'translation_import' in document:
        raise ValueError('请使用原始基础稿，不能用已接收的外部译稿作为基础稿')
    source_keys = _segment_keys(document.get('segments'), None)
    translated = {row['id']: row['zh'] for row in rows}
    selected = [key for key in source_keys if key[0] in translated]
    if selected != expected:
        raise ValueError('接收清单与基础稿段落、顺序或说话人不一致')

    for segment in document['segments']:
        if segment['id'] in translated:
            if segment.get('is_ad'):
                raise ValueError('接收清单包含广告段')
            segment['chinese'] = translated[segment['id']]
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
    document['translation_import'] = receipt
    raw = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')

    with podcast_job_lock(job):
        _require_unqueued(job)
        source_lock = decode_json_object(_read_regular_bytes(job / 'source-identity.json'))
        if source_lock.get('sha256') != receipt['source_sha256'] or sha256_file(source_audio) != receipt['source_sha256']:
            raise ValueError('音源或来源锁 SHA-256 与接收清单不一致')
        if _read_regular_bytes(base_document) != base_raw:
            raise ValueError('接收前基础稿已改变')
        try:
            prior = _read_regular_bytes(output)
        except FileNotFoundError:
            prior = None
        if prior is not None:
            if prior != raw:
                raise ValueError('目标已有不同译稿或接收记录，原文件已保留')
            return {'status': 'unchanged', 'imported_document_sha256': _sha256(raw), **receipt}
        try:
            _save_bytes(output, raw, exclusive=True)
        except FileExistsError:
            raise ValueError('目标已由其他写入者创建，原文件已保留') from None
        except OSError:
            # An error syncing the directory can arrive after the atomic link.
            # Under the job lock remove only this attempt's complete document.
            if output.exists() and _read_regular_bytes(output) == raw:
                output.unlink()
            raise
    return {'status': 'received', 'imported_document_sha256': _sha256(raw), **receipt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-dir', type=Path, required=True)
    parser.add_argument('--base-document', type=Path, required=True)
    parser.add_argument('--source-audio', type=Path, required=True, help='隔离 job 根内的音源文件名')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--export', type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = receive(args.job_dir, args.base_document, args.source_audio, args.manifest, args.export)
    except (OSError, ValueError, RecursionError, StudioRecoveryError) as error:
        print(f'接收拒绝：{error}', file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
