from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import studio_import_gemini
import studio_recovery
from studio_import_gemini import receive


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'studio_import_gemini.py'


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Workspace:
    def __init__(self, root):
        self.job = root / 'job'
        self.job.mkdir()
        self.output = self.job / 'document.json'
        self.base = root / 'original.json'
        self.source = self.job / 'source.mp3'
        self.lock = self.job / 'source-identity.json'
        self.manifest = root / 'manifest.json'
        self.export = root / 'export.txt'
        self.inputs = (self.job, self.base, self.source, self.manifest, self.export)
        self.append_inputs = (self.job, None, self.source, self.manifest, self.export)
        self.cli = [sys.executable, '-B', str(SCRIPT), '--job-dir', str(self.job),
                    '--source-audio', self.source.name, '--manifest', str(self.manifest),
                    '--export', str(self.export)]
        self.document = {
            'schema_version': 1, 'transcript_source': 'asr',
            'speakers': {'s1': {'voice_slot': 'male_1'}, 's2': {'voice_slot': 'male_2'}},
            'references': {}, 'voices_confirmed': False,
            'segments': [
                {'id': f'seg-{i:06d}', 'speaker_id': speaker, 'text': 'Existing ASR',
                 'chinese': '' if i % 2 == 0 else '原译', 'ambiguous': True, 'is_ad': False,
                 'start_ms': i, 'end_ms': i + 1}
                for i, speaker in enumerate(('s1', 's2', 's1', 's1', 's2', 's2'), 1)
            ],
        }
        _write(self.base, self.document)
        self.notes = self.job / 'notes.json'
        _write(self.notes, {'note': '保留'})
        self.source.write_bytes(b'fixture source bytes')
        _write(self.lock, {'sha256': _digest(self.source)})
        self.stage(self.base, (1, 3))

    def stage(self, base, numbers):
        selection = [{key: self.document['segments'][i - 1][key] for key in ('id', 'speaker_id')}
                     for i in numbers]
        self.rows = [{**row, 'zh': f"  译文 {row['id']}  "} for row in selection]
        _write(self.export, self.rows)
        self.plan = {
            'schema_version': 1, 'document_sha256': _digest(base),
            'source_sha256': _digest(self.source), 'export_sha256': _digest(self.export),
            'segments': selection,
        }
        _write(self.manifest, self.plan)

    def run(self, *, append=False, revise=False):
        mode = ['--base-document', str(self.base)]
        if revise:
            mode = ['--revise']
        elif append:
            mode = ['--append']
        return subprocess.run(self.cli + mode, text=True, capture_output=True, check=False)

    def snapshot(self):
        return {str(path.relative_to(self.job.parent)): _digest(path)
                for path in self.job.parent.rglob('*') if path.is_file()
                and path.name != '.podcast-run.lock'}


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


@pytest.fixture
def append_ws(ws):
    result = ws.run()
    assert result.returncode == 0, result.stderr
    ws.stage(ws.output, (2, 4))
    return ws


def test_cli_imports_exact_text_and_is_idempotent(ws):
    before = ws.snapshot()
    result = ws.run()
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['status'] == 'received'
    assert receipt['imported_document_sha256'] == _digest(ws.output)
    actual = json.loads(ws.output.read_bytes())
    assert actual.pop('translation_import') == {
        'schema_version': 1, 'provider': 'gemini_app_manual', 'display_model': 'Neon 高',
        'official_model_id': None, 'document_sha256': _digest(ws.base),
        'source_sha256': _digest(ws.source), 'export_sha256': _digest(ws.export),
        'manifest_sha256': _digest(ws.manifest), 'segment_ids': [r['id'] for r in ws.rows],
    }
    expected = deepcopy(ws.document)
    expected['segments'][0]['chinese'] = ws.rows[0]['zh']
    expected['segments'][2]['chinese'] = ws.rows[1]['zh']
    assert actual == expected
    after = ws.snapshot()
    assert after == {**before, 'job/document.json': _digest(ws.output)}
    rerun = ws.run()
    assert rerun.returncode == 0, rerun.stderr
    assert json.loads(rerun.stdout) == {**receipt, 'status': 'unchanged'}
    assert ws.snapshot() == after


@pytest.mark.parametrize('defect', [
    'invalid-json', 'duplicate-field', 'blank', 'missing', 'duplicate', 'id', 'speaker',
    'order', 'plan-duplicate', 'plan-wrong-speaker', 'stale-document', 'stale-source',
    'source-lock', 'stale-export', 'queued', 'old-document', 'import-conflict',
])
def test_cli_rejects_without_changing_files(ws, defect):
    if defect == 'invalid-json':
        ws.export.write_text('[', encoding='utf-8')
    elif defect == 'duplicate-field':
        ws.export.write_text('[{"id":"x","id":"y","speaker_id":"s1","zh":"译"}]',
                             encoding='utf-8')
    elif defect in ('blank', 'missing', 'duplicate', 'id', 'speaker', 'order',
                    'stale-export', 'import-conflict'):
        if defect == 'missing':
            ws.rows.pop()
        elif defect == 'duplicate':
            ws.rows[1] = ws.rows[0]
        elif defect == 'order':
            ws.rows.reverse()
        else:
            if defect == 'import-conflict':
                assert ws.run().returncode == 0
            key, value = {'blank': ('zh', '  \n'), 'id': ('id', 'seg-000999'),
                          'speaker': ('speaker_id', 's2'), 'stale-export': ('zh', '新输出'),
                          'import-conflict': ('zh', '另一份有效译文')}[defect]
            ws.rows[0][key] = value
        _write(ws.export, ws.rows)
    elif defect == 'plan-duplicate':
        ws.plan['segments'].append(ws.plan['segments'][0])
    elif defect == 'plan-wrong-speaker':
        ws.plan['segments'][0]['speaker_id'] = ws.rows[0]['speaker_id'] = 's2'
        _write(ws.export, ws.rows)
    elif defect == 'stale-document':
        ws.base.write_bytes(ws.base.read_bytes() + b'\n')
    elif defect == 'stale-source':
        ws.source.write_bytes(b'replaced audio')
    elif defect == 'source-lock':
        _write(ws.lock, {'sha256': '0' * 64})
    elif defect == 'queued':
        _write(ws.job / 'state.json', {'status': 'queued'})
    elif defect == 'old-document':
        _write(ws.output, ws.document)
    if defect != 'stale-export':
        ws.plan['export_sha256'] = _digest(ws.export)
    _write(ws.manifest, ws.plan)
    before = ws.snapshot()
    result = ws.run()
    assert result.returncode == 1
    assert '接收拒绝：' in result.stderr
    assert not result.stdout
    assert ws.snapshot() == before
    if defect == 'queued':
        assert not (ws.job / '.podcast-run.lock').exists()


def test_failed_directory_sync_rolls_back_new_document(ws, monkeypatch):
    before = ws.snapshot()
    sync_dir = studio_recovery._sync_dir
    attempts = []

    def fail_after_link(directory):
        attempts.append(directory)
        if len(attempts) == 1:
            assert ws.output.is_file()
            raise OSError('fixture: directory sync failed after link')
        assert not ws.output.exists()
        sync_dir(directory)

    with monkeypatch.context() as patched:
        patched.setattr(studio_recovery, '_sync_dir', fail_after_link)
        with pytest.raises(OSError, match='directory sync failed'):
            receive(*ws.inputs)
    assert ws.snapshot() == before
    result = ws.run()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'received'


def test_cli_appends_multiple_batches_and_replays_without_writes(append_ws):
    ws = append_ws
    before = ws.snapshot()
    expected = json.loads(ws.output.read_bytes())
    batch2 = ws.manifest.read_bytes(), ws.export.read_bytes()
    result = ws.run(append=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt.pop('status') == 'received'
    digest = receipt.pop('imported_document_sha256')
    assert digest == _digest(ws.output)
    assert receipt == {
        'schema_version': 1, 'provider': 'gemini_app_manual', 'display_model': 'Neon 高',
        'official_model_id': None, 'document_sha256': before['job/document.json'],
        'source_sha256': before['job/source.mp3'], 'export_sha256': before['export.txt'],
        'manifest_sha256': before['manifest.json'], 'segment_ids': [r['id'] for r in ws.rows],
    }
    expected['segments'][1]['chinese'] = ws.rows[0]['zh']
    expected['segments'][3]['chinese'] = ws.rows[1]['zh']
    expected['translation_imports'] = [receipt]
    assert json.loads(ws.output.read_bytes()) == expected
    after = ws.snapshot()
    assert after == {**before, 'job/document.json': digest}
    rerun = ws.run(append=True)
    assert rerun.returncode == 0, rerun.stderr
    assert json.loads(rerun.stdout) == {
        **receipt, 'status': 'unchanged', 'imported_document_sha256': digest,
    }
    assert ws.snapshot() == after

    ws.stage(ws.output, (6,))
    result = ws.run(append=True)
    assert result.returncode == 0, result.stderr
    receipt3 = json.loads(result.stdout)
    assert receipt3.pop('status') == 'received'
    digest3 = receipt3.pop('imported_document_sha256')
    assert digest3 == _digest(ws.output)
    assert receipt3['document_sha256'] == digest
    assert receipt3['export_sha256'] == _digest(ws.export)
    expected['segments'][5]['chinese'] = ws.rows[0]['zh']
    expected['translation_imports'].append(receipt3)
    assert json.loads(ws.output.read_bytes()) == expected

    ws.manifest.write_bytes(batch2[0])
    ws.export.write_bytes(batch2[1])
    after = ws.snapshot()
    replay = ws.run(append=True)
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout) == {
        **receipt, 'status': 'unchanged', 'imported_document_sha256': digest3,
    }
    assert ws.snapshot() == after


@pytest.mark.parametrize('defect, message', [
    ('filled', '已有中文'), ('imported', '已导入'), ('imported-cleared', '已导入'),
    ('stale-document', 'document.json SHA-256'), ('wrong-lock', '来源锁'),
    ('wrong-audio', '音源'), ('rebound-audio', '同源'), ('wrong-export', '网页导出 SHA-256'),
    ('missing', '段数'), ('id', '段数'), ('speaker', '段数'), ('order', '顺序'),
    ('plan-id', '与基础稿'), ('plan-speaker', '与基础稿'),
    ('plan-order', '与基础稿'), ('blank', '空译'), ('invalid-json', '接收拒绝'),
    ('ad', '广告段'), ('queued', '未入队'), ('without-history', '既有 Gemini'),
])
def test_cli_rejects_invalid_append_without_changing_files(append_ws, defect, message):
    ws = append_ws
    if defect in ('ad', 'without-history', 'imported-cleared'):
        document = json.loads(ws.output.read_bytes())
        if defect == 'ad':
            document['segments'][1]['is_ad'] = True
        elif defect == 'without-history':
            del document['translation_import']
        else:
            document['segments'][0]['chinese'] = ''
        _write(ws.output, document)
        ws.stage(ws.output, (1, 2) if defect == 'imported-cleared' else (2, 4))
    elif defect in ('filled', 'imported'):
        ws.stage(ws.output, (2, 5) if defect == 'filled' else (1, 2))
    elif defect == 'stale-document':
        ws.plan['document_sha256'] = _digest(ws.base)
    elif defect == 'wrong-lock':
        _write(ws.lock, {'sha256': '0' * 64})
    elif defect in ('wrong-audio', 'rebound-audio'):
        ws.source.write_bytes(b'replaced audio')
        if defect == 'rebound-audio':
            ws.plan['source_sha256'] = _digest(ws.source)
            _write(ws.lock, {'sha256': _digest(ws.source)})
    elif defect == 'queued':
        _write(ws.job / 'state.json', {'status': 'queued'})
    elif defect in ('order', 'plan-order'):
        ws.rows.reverse()
        if defect == 'plan-order':
            ws.plan['segments'].reverse()
    elif defect == 'missing':
        ws.rows.pop()
    else:
        if defect != 'invalid-json':
            key, value = {
                'id': ('id', 'seg-000999'), 'plan-id': ('id', 'seg-000999'),
                'speaker': ('speaker_id', 's1'), 'plan-speaker': ('speaker_id', 's1'),
                'blank': ('zh', ' \n'), 'wrong-export': ('zh', '新译'),
            }[defect]
            ws.rows[0][key] = value
            if defect.startswith('plan-'):
                ws.plan['segments'][0][key] = value
    _write(ws.export, ws.rows)
    if defect == 'invalid-json':
        ws.export.write_text('[', encoding='utf-8')
    if defect != 'wrong-export':
        ws.plan['export_sha256'] = _digest(ws.export)
    _write(ws.manifest, ws.plan)
    before = ws.snapshot()
    result = ws.run(append=True)
    assert result.returncode == 1
    assert message in result.stderr
    assert not result.stdout
    assert ws.snapshot() == before


def test_append_rejects_paid_chunk_bound_to_source(append_ws, monkeypatch):
    ws = append_ws
    monkeypatch.setattr(studio_import_gemini, '_PAID_SOURCE_SHA256', _digest(ws.source))
    before = ws.snapshot()
    with pytest.raises(ValueError, match='已隔离的付费首块'):
        receive(*ws.append_inputs)
    assert ws.snapshot() == before


@pytest.mark.parametrize('changed', ['document', 'source-lock', 'audio', 'state'])
def test_append_rechecks_inputs_after_staging(append_ws, monkeypatch, changed):
    ws = append_ws
    before = ws.snapshot()
    path = {'document': ws.output, 'source-lock': ws.lock, 'audio': ws.source,
            'state': ws.job / 'state.json'}[changed]
    modified = (path.read_bytes() if path.exists() else b'{}') + b'\n'
    fsync = studio_recovery.os.fsync

    def change_after_staging(fd):
        fsync(fd)
        path.write_bytes(modified)

    with monkeypatch.context() as patched:
        patched.setattr(studio_recovery.os, 'fsync', change_after_staging)
        with pytest.raises(ValueError, match='已改变|未入队'):
            receive(*ws.append_inputs)
    assert ws.snapshot() == {**before, f'job/{path.name}': hashlib.sha256(modified).hexdigest()}


def test_append_directory_sync_failure_restores_previous_document(append_ws, monkeypatch):
    ws = append_ws
    before = ws.snapshot()

    def fail_after_replace(directory):
        assert directory == ws.job
        current = json.loads(ws.output.read_bytes())
        assert current['translation_imports'][0]['export_sha256'] == _digest(ws.export)
        raise OSError('fixture: directory sync failed after replace')

    with monkeypatch.context() as patched:
        patched.setattr(studio_recovery, '_sync_dir', fail_after_replace)
        with pytest.raises(OSError, match='directory sync failed'):
            receive(*ws.append_inputs)
    assert ws.snapshot() == before
    result = ws.run(append=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'received'


@pytest.fixture
def revise_ws(append_ws):
    ws = append_ws
    result = ws.run(append=True)
    assert result.returncode == 0, result.stderr
    ws.stage(ws.output, (1, 4))
    current = json.loads(ws.output.read_bytes())
    for plan, row, index in zip(ws.plan['segments'], ws.rows, (0, 3)):
        plan['old_chinese'] = current['segments'][index]['chinese']
        row['zh'] = f"定向修订 {row['id']}"
    _write(ws.export, ws.rows)
    ws.plan['export_sha256'] = _digest(ws.export)
    _write(ws.manifest, ws.plan)
    return ws


def test_cli_revises_exact_segments_and_retains_history(revise_ws):
    ws = revise_ws
    before = ws.snapshot()
    expected = json.loads(ws.output.read_bytes())
    result = ws.run(revise=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt.pop('status') == 'received'
    assert receipt.pop('imported_document_sha256') == _digest(ws.output)
    assert receipt == {
        'schema_version': 1, 'provider': 'gemini_app_manual', 'display_model': 'Neon 高',
        'official_model_id': None, 'document_sha256': before['job/document.json'],
        'source_sha256': before['job/source.mp3'], 'export_sha256': before['export.txt'],
        'manifest_sha256': before['manifest.json'], 'segment_ids': [r['id'] for r in ws.rows],
        'previous_chinese': {r['id']: r['old_chinese'] for r in ws.plan['segments']},
    }
    for index, row in zip((0, 3), ws.rows):
        expected['segments'][index]['chinese'] = row['zh']
    expected['translation_revisions'] = [receipt]
    assert json.loads(ws.output.read_bytes()) == expected
    after = ws.snapshot()
    assert after == {**before, 'job/document.json': _digest(ws.output)}
    replay = ws.run(revise=True)
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)['status'] == 'unchanged'
    assert ws.snapshot() == after

    ws.stage(ws.output, (6,))
    appended = ws.run(append=True)
    assert appended.returncode == 0, appended.stderr
    assert json.loads(ws.output.read_bytes())['translation_revisions'] == [receipt]


@pytest.mark.parametrize('defect, message', [
    ('stale', 'document.json SHA-256'), ('old', '旧译'), ('empty', '未填中文'),
    ('id', '与基础稿'), ('speaker', '与基础稿'), ('missing', '段数'), ('extra', '段数'),
])
def test_revision_rejects_invalid_batch_without_writes(revise_ws, defect, message):
    ws = revise_ws
    if defect == 'stale':
        ws.output.write_bytes(ws.output.read_bytes() + b'\n')
    elif defect == 'old':
        ws.plan['segments'][1]['old_chinese'] += ' '
    elif defect == 'empty':
        ws.plan['segments'][1] = {'id': 'seg-000006', 'speaker_id': 's2', 'old_chinese': ''}
        ws.rows[1].update(id='seg-000006', speaker_id='s2')
    elif defect in ('id', 'speaker'):
        key, wrong = ('id', 'seg-000999') if defect == 'id' else ('speaker_id', 's2')
        ws.plan['segments'][1][key] = ws.rows[1][key] = wrong
    elif defect == 'missing':
        ws.rows.pop()
    else:
        ws.rows.append({'id': 'seg-000005', 'speaker_id': 's2', 'zh': '未选择的译文'})
    _write(ws.export, ws.rows)
    ws.plan['export_sha256'] = _digest(ws.export)
    _write(ws.manifest, ws.plan)
    before = ws.snapshot()
    result = ws.run(revise=True)
    assert result.returncode == 1
    assert message in result.stderr
    assert ws.snapshot() == before


def test_revision_already_at_target_does_not_add_history(revise_ws):
    ws = revise_ws
    for row, plan in zip(ws.rows, ws.plan['segments']):
        row['zh'] = plan['old_chinese']
    _write(ws.export, ws.rows)
    ws.plan['export_sha256'] = _digest(ws.export)
    _write(ws.manifest, ws.plan)
    before = ws.snapshot()
    result = ws.run(revise=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'unchanged'
    assert ws.snapshot() == before


def test_replaying_an_old_revision_cannot_restore_superseded_text(revise_ws):
    ws = revise_ws
    first_manifest, first_export = ws.manifest.read_bytes(), ws.export.read_bytes()
    assert ws.run(revise=True).returncode == 0
    ws.plan['document_sha256'] = _digest(ws.output)
    for row, plan in zip(ws.rows, ws.plan['segments']):
        plan['old_chinese'] = row['zh']
        row['zh'] += '，再次修订'
    _write(ws.export, ws.rows)
    ws.plan['export_sha256'] = _digest(ws.export)
    _write(ws.manifest, ws.plan)
    assert ws.run(revise=True).returncode == 0
    ws.manifest.write_bytes(first_manifest)
    ws.export.write_bytes(first_export)
    before = ws.snapshot()
    result = ws.run(revise=True)
    assert result.returncode == 1
    assert 'document.json SHA-256' in result.stderr
    assert ws.snapshot() == before


@pytest.fixture
def recovery_ws(ws, monkeypatch):
    template = ws.document['segments'][1]
    ws.document['segments'] = [
        {**template, 'id': f'seg-{i:06d}', 'start_ms': i, 'end_ms': i + 1}
        for i in range(1, 95)
    ]
    _write(ws.base, ws.document)
    monkeypatch.setattr(studio_import_gemini, '_PAID_SOURCE_SHA256', _digest(ws.source))
    ws.stage(ws.base, (92,))
    receive(*ws.inputs)
    ws.stage(ws.output, (93,))
    receive(*ws.append_inputs)
    ws.stage(ws.output, range(2, 92))
    return ws


def test_declared_recovery_preserves_payment_and_fills_only_chunk(recovery_ws, monkeypatch, capsys):
    ws = recovery_ws
    # Guard even reads through the actual budget implementation. The synthetic
    # sentinel also catches accidental file modifications in this isolated job.
    (ws.job / 'translation-budget.sqlite3').write_bytes(b'fixture: paid; uncertain; safe_to_retry=false')

    def deny_ledger(*args, **kwargs):
        pytest.fail('recovery opened a translation ledger')

    monkeypatch.setattr(sqlite3, 'connect', deny_ledger)
    before = ws.snapshot()
    expected = json.loads(ws.output.read_bytes())
    monkeypatch.setattr(sys, 'argv', ws.cli[2:] + ['--recovery-of-missing-response'])
    assert studio_import_gemini.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt.pop('status') == 'received'
    assert receipt.pop('imported_document_sha256') == _digest(ws.output)
    assert receipt == {
        'schema_version': 1, 'provider': 'gemini_app_manual', 'display_model': 'Neon 高',
        'official_model_id': None, 'document_sha256': before['job/document.json'],
        'source_sha256': before['job/source.mp3'], 'export_sha256': before['export.txt'],
        'manifest_sha256': before['manifest.json'], 'segment_ids': [r['id'] for r in ws.rows],
        'recovery_of_missing_response': {
            'request_key': 'a257fa8fd7b4a33f1d51ebcbc7be51a142415fa479b28eb57cfce98f88101b48',
            'provider_response_id': '61a6ce7d-9b84-48a0-bf72-6d9881f1f406',
        },
    }
    for segment, row in zip(expected['segments'][1:91], ws.rows):
        assert segment['id'] == row['id'] and segment['chinese'] == ''
        segment['chinese'] = row['zh']
    expected['translation_imports'].append(receipt)
    assert json.loads(ws.output.read_bytes()) == expected
    assert not (ws.job / 'state.json').exists()
    after = ws.snapshot()
    assert after == {**before, 'job/document.json': _digest(ws.output)}
    assert receive(*ws.append_inputs, recovery_of_missing_response=True)['status'] == 'unchanged'
    assert ws.snapshot() == after
    ws.stage(ws.output, (2, 94))
    with pytest.raises(ValueError, match='已隔离的付费首块'):
        receive(*ws.append_inputs)
    ws.stage(ws.output, (94,))
    assert receive(*ws.append_inputs)['status'] == 'received'
    assert json.loads(ws.output.read_bytes())['translation_imports'][-2] == receipt


@pytest.mark.parametrize('defect, message', [
    ('append', '已隔离的付费首块'), ('first', '已隔离的付费首块'),
    ('deepseek-export', '分段字段'), ('deepseek-manifest', '清单格式'),
    ('stale', 'document.json SHA-256'), ('speaker', '与基础稿'),
    ('id', '与基础稿'), ('outside', '只允许 Shelf Help'), ('filled', '已有中文'),
])
def test_recovery_rejects_invalid_batch_without_writes(recovery_ws, defect, message):
    ws = recovery_ws
    if defect == 'first':
        ws.stage(ws.base, range(2, 92))
    elif defect == 'deepseek-export':
        ws.rows[0]['provider'] = 'deepseek'
    elif defect == 'deepseek-manifest':
        ws.plan['provider'] = 'deepseek'
    elif defect == 'stale':
        ws.output.write_bytes(ws.output.read_bytes() + b'\n')
    elif defect in ('speaker', 'id'):
        key, wrong = ('id', 'seg-000999') if defect == 'id' else ('speaker_id', 's1')
        ws.plan['segments'][0][key] = ws.rows[0][key] = wrong
    elif defect == 'outside':
        ws.stage(ws.output, (2, 94))
    elif defect == 'filled':
        document = json.loads(ws.output.read_bytes())
        document['segments'][90]['chinese'] = '已有中文'
        _write(ws.output, document)
        ws.stage(ws.output, range(2, 92))
    _write(ws.export, ws.rows)
    ws.plan['export_sha256'] = _digest(ws.export)
    _write(ws.manifest, ws.plan)
    before = ws.snapshot()
    with pytest.raises(ValueError, match=message):
        receive(*(ws.inputs if defect == 'first' else ws.append_inputs),
                recovery_of_missing_response=defect not in ('first', 'append'))
    assert ws.snapshot() == before


@pytest.mark.parametrize('fixture, mode', [
    ('revise_ws', 'revise'), ('recovery_ws', 'recovery_of_missing_response'),
])
@pytest.mark.parametrize('failure', ['stale-at-install', 'sync', 'interrupt'])
def test_failed_write_preserves_document(request, monkeypatch, fixture, mode, failure):
    ws = request.getfixturevalue(fixture)
    before = ws.snapshot()
    externally_changed = ws.output.read_bytes() + b'\n'
    fsync = studio_recovery.os.fsync

    def change_after_staging(fd):
        fsync(fd)
        ws.output.write_bytes(externally_changed)

    def fail_after_replace(directory):
        assert directory == ws.job
        assert _digest(ws.output) != before['job/document.json']
        error = OSError if failure == 'sync' else KeyboardInterrupt
        raise error('fixture: interrupted after atomic replacement')

    with monkeypatch.context() as patched:
        if failure == 'stale-at-install':
            patched.setattr(studio_recovery.os, 'fsync', change_after_staging)
            with pytest.raises(ValueError, match='基础稿已改变'):
                receive(*ws.append_inputs, **{mode: True})
        else:
            patched.setattr(studio_recovery, '_sync_dir', fail_after_replace)
            with pytest.raises(OSError if failure == 'sync' else KeyboardInterrupt, match='interrupted'):
                receive(*ws.append_inputs, **{mode: True})
    if failure == 'stale-at-install':
        before['job/document.json'] = hashlib.sha256(externally_changed).hexdigest()
    assert ws.snapshot() == before
    if failure != 'stale-at-install':
        assert receive(*ws.append_inputs, **{mode: True})['status'] == 'received'


@pytest.fixture
def pending_ws(ws):
    for row in ws.document['segments']:
        row['chinese'] = ''
    ws.document['segments'][0]['is_ad'] = True
    ws.document['segments'][3]['ambiguous'] = False
    _write(ws.base, ws.document)
    ws.stage(ws.base, (2, 3))
    receive(*ws.inputs)
    ws.stage(ws.output, (4,))
    receive(*ws.append_inputs)
    ws.stage(ws.output, (2,))
    ws.plan['segments'][0]['old_chinese'] = ws.rows[0]['zh']
    ws.rows[0]['zh'] = '修订译文'
    _write(ws.export, ws.rows)
    ws.plan['export_sha256'] = _digest(ws.export)
    _write(ws.manifest, ws.plan)
    receive(*ws.append_inputs, revise=True)
    return _prepare_pending_target(ws)


def _prepare_pending_target(ws):
    from studio_server import Studio

    ws.pending = ws.job.parent / 'pending.json'
    ws.output.rename(ws.pending)
    ws.state = ws.job / 'state.json'
    _write(ws.state, {'status': 'queued', 'action': 'prepare'})
    ws.studio = Studio(ws.job.parent, recover=False)
    ws.studio.action(ws.job.name, 'cancel')
    assert json.loads(ws.state.read_bytes())['status'] == 'cancelled'
    normalized = hashlib.sha256(b'normalized audio').hexdigest()
    _write(ws.lock, {'kind': 'local', 'sha256': normalized, 'input_sha256': _digest(ws.source),
                     'bytes': 64, 'duration_ms': 10})
    ws.checkpoint = ws.job / 'translation-source.json'
    ws.source_draft = deepcopy(ws.document)
    for row in ws.source_draft['segments']:
        del row['chinese']
    ws.source_draft['media_sha256'] = normalized
    studio_recovery.save_source(ws.checkpoint, 'fixture', ws.source_draft)
    ws.ledger = ws.job / 'translation-budget.sqlite3'
    ws.ledger.write_bytes(b'fixture: uncertain; do not open')
    (ws.job / 'audio-cache').mkdir()
    (ws.job / 'audio-cache/keep').write_bytes(b'cached audio')
    ws.source_sha = _digest(ws.source)
    return ws


def _install(ws, **overrides):
    options = {'job_id': ws.job.name, 'source_sha256': ws.source_sha,
               'document_sha256': _digest(ws.pending), **overrides}
    return studio_import_gemini.install_pending_draft(ws.job, ws.pending, ws.source, **options)


def test_pending_install_cli_is_exact_and_idempotent(pending_ws, monkeypatch, capsys):
    ws = pending_ws
    before = ws.snapshot()
    expected = ws.pending.read_bytes()
    cli = [str(SCRIPT), '--job-dir', str(ws.job), '--source-audio', ws.source.name,
           '--install-pending-draft', str(ws.pending), '--job-id', ws.job.name,
           '--source-sha256', ws.source_sha, '--document-sha256', _digest(ws.pending)]
    monkeypatch.setattr(sys, 'argv', cli)
    open_file, open_fd = Path.open, os.open

    def deny_sqlite(*args, **kwargs):
        pytest.fail('installation opened the translation ledger')

    def no_ledger_open(path, *args, **kwargs):
        if path == ws.ledger:
            deny_sqlite()
        return open_file(path, *args, **kwargs)

    def no_ledger_fd(path, *args, **kwargs):
        if path == ws.ledger:
            deny_sqlite()
        return open_fd(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(sqlite3, 'connect', deny_sqlite)
        patched.setattr(Path, 'open', no_ledger_open)
        patched.setattr(os, 'open', no_ledger_fd)
        assert studio_import_gemini.main() == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt == {'status': 'installed', 'job_id': ws.job.name,
                           'source_sha256': ws.source_sha, 'document_sha256': _digest(ws.pending)}
        assert ws.output.read_bytes() == expected
        first = ws.output.stat()
        assert studio_import_gemini.main() == 0
        assert json.loads(capsys.readouterr().out) == {**receipt, 'status': 'unchanged'}
        assert ws.output.stat().st_ino == first.st_ino
        assert ws.output.stat().st_mtime_ns == first.st_mtime_ns
    after = ws.snapshot()
    assert after == {**before, 'job/document.json': _digest(ws.pending),
                     '.server.lock': hashlib.sha256(b'').hexdigest()}


def test_pending_install_can_use_source_draft(pending_ws):
    ws = pending_ws
    ws.checkpoint.unlink()
    _write(ws.job / 'source-draft.json', ws.source_draft)
    _write(ws.state, {'status': 'failed'})
    state_raw = ws.state.read_bytes()
    assert _install(ws)['status'] == 'installed'
    assert ws.output.read_bytes() == ws.pending.read_bytes()
    assert ws.state.read_bytes() == state_raw


def test_pending_install_with_identical_document_needs_no_new_lock_files(pending_ws):
    ws = pending_ws
    (ws.job / '.podcast-run.lock').unlink()
    ws.output.write_bytes(ws.pending.read_bytes())
    before = ws.snapshot()
    assert _install(ws)['status'] == 'unchanged'
    assert ws.snapshot() == before
    assert not (ws.job / '.podcast-run.lock').exists()


def test_pending_install_retains_missing_response_recovery(recovery_ws):
    ws = recovery_ws
    receipt = receive(*ws.append_inputs, recovery_of_missing_response=True)
    _prepare_pending_target(ws)
    assert _install(ws)['status'] == 'installed'
    result = json.loads(ws.output.read_bytes())['translation_imports'][-1]
    assert result['recovery_of_missing_response'] == receipt['recovery_of_missing_response']
    assert ws.output.read_bytes() == ws.pending.read_bytes()


@pytest.mark.parametrize('defect, message', [
    ('different document', '不同译稿'), ('wrong job', '任务 ID'), ('wrong draft sha', '待审稿 SHA-256'),
    ('wrong input sha', '音源'), ('changed audio', '音源'), ('wrong source identity', '音源'),
    ('wrong normalized sha', '来源'), ('wrong checkpoint digest', '来源'),
    ('wrong segment', '段落'), ('wrong speaker', '说话人'), ('changed ambiguous', '待核'),
    ('changed source text', '原文'), ('missing provenance', '接收'), ('wrong revision sha', '来源'),
    ('queued', '活跃'), ('running', '活跃'), ('cancelling', '活跃'),
    ('malformed state', '状态'),
])
def test_pending_install_rejects_invalid_inputs_without_writes(pending_ws, defect, message):
    ws = pending_ws
    options = {}
    if defect == 'different document':
        ws.output.write_bytes(ws.pending.read_bytes() + b'\n')
    elif defect == 'wrong job':
        options['job_id'] = 'other-job'
    elif defect == 'wrong draft sha':
        options['document_sha256'] = '0' * 64
    elif defect == 'wrong input sha':
        options['source_sha256'] = '0' * 64
    elif defect == 'changed audio':
        ws.source.write_bytes(b'replaced audio')
    elif defect in ('wrong source identity', 'wrong normalized sha'):
        identity = json.loads(ws.lock.read_bytes())
        identity['input_sha256' if defect == 'wrong source identity' else 'sha256'] = '0' * 64
        _write(ws.lock, identity)
    elif defect == 'wrong checkpoint digest':
        checkpoint = json.loads(ws.checkpoint.read_bytes())
        checkpoint['digest'] = '0' * 64
        _write(ws.checkpoint, checkpoint)
    elif defect in ('queued', 'running', 'cancelling', 'malformed state'):
        _write(ws.state, {'status': defect} if defect != 'malformed state' else {})
    else:
        document = json.loads(ws.pending.read_bytes())
        if defect == 'missing provenance':
            del document['translation_import']
        elif defect == 'wrong revision sha':
            document['translation_revisions'][0]['source_sha256'] = '0' * 64
        else:
            key, value = {
                'wrong segment': ('id', 'seg-009999'), 'wrong speaker': ('speaker_id', 's1'),
                'changed ambiguous': ('ambiguous', False), 'changed source text': ('text', 'new text'),
            }[defect]
            document['segments'][1][key] = value
        _write(ws.pending, document)
    before = ws.snapshot()
    with pytest.raises(ValueError, match=message):
        _install(ws, **options)
    assert ws.snapshot() == before


@pytest.mark.parametrize('relative_lock', ['.server.lock', 'job/.podcast-run.lock'])
def test_pending_install_rejects_writer(pending_ws, relative_lock):
    ws = pending_ws
    (ws.job.parent / '.server.lock').touch()
    before = ws.snapshot()
    with (ws.job.parent / relative_lock).open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(studio_recovery.StudioRecoveryError, match='仍在'):
            _install(ws)
    assert ws.snapshot() == before


@pytest.mark.parametrize('failure', ['write', 'sync', 'interrupt', 'concurrent document', 'new state'])
def test_pending_install_rolls_back_failed_write(pending_ws, monkeypatch, failure):
    ws = pending_ws
    (ws.job.parent / '.server.lock').touch()
    before = ws.snapshot()
    fsync = studio_recovery.os.fsync
    sync_dir = studio_recovery._sync_dir
    attempts = []
    winner = ws.pending.read_bytes() + b'\n'

    def after_staging(fd):
        if failure == 'write':
            raise OSError('fixture write failure')
        fsync(fd)
        if failure == 'new state':
            _write(ws.state, {'status': 'queued'})
        else:
            ws.output.write_bytes(winner)

    def after_link(directory):
        attempts.append(directory)
        if len(attempts) == 1:
            assert directory == ws.job
            assert ws.output.read_bytes() == ws.pending.read_bytes()
            raise (KeyboardInterrupt if failure == 'interrupt' else OSError)('fixture sync failure')
        assert not ws.output.exists()
        sync_dir(directory)

    if failure in ('sync', 'interrupt'):
        monkeypatch.setattr(studio_recovery, '_sync_dir', after_link)
    else:
        monkeypatch.setattr(studio_recovery.os, 'fsync', after_staging)
    expected_error = {
        'write': OSError, 'sync': OSError, 'interrupt': KeyboardInterrupt,
        'concurrent document': ValueError, 'new state': ValueError,
    }[failure]
    with pytest.raises(expected_error):
        _install(ws)
    if failure in ('sync', 'interrupt'):
        assert attempts == [ws.job, ws.job]
    if failure == 'concurrent document':
        before['job/document.json'] = hashlib.sha256(winner).hexdigest()
    if failure == 'new state':
        before['job/state.json'] = _digest(ws.state)
    assert ws.snapshot() == before


def test_pending_install_preserves_confirmation_and_render_gates(pending_ws):
    from studio_document import segment_evidence_hash
    from studio_engine import StudioEngine, StudioInputError

    ws = pending_ws
    receipt = _install(ws)
    document = json.loads(ws.output.read_bytes())
    segment = document['segments'][1]
    assert segment['ambiguous'] is True
    assert document['segments'][0]['chinese'] == ''
    assert document['segments'][4]['chinese'] == ''
    with pytest.raises(StudioInputError, match='说话人待核对'):
        StudioEngine(ws.job).render({}, document)
    with pytest.raises(ValueError, match='confirm-segments'):
        ws.studio.confirm_speakers(ws.job.name)
    evidence = {'official_source_sha256': hashlib.sha256(b'official source').hexdigest(),
                'official_speaker': 'Speaker 2', 'official_locator': 'turn 2',
                'official_excerpt': segment['text']}
    digest = receipt['document_sha256']
    item = {'id': segment['id'], 'speaker_id': segment['speaker_id'], **evidence,
            'evidence_hash': segment_evidence_hash(digest, segment, evidence)}
    with pytest.raises(ValueError, match='document_sha256'):
        ws.studio.confirm_segments(ws.job.name, {'document_sha256': _digest(ws.base), 'items': [item]})
    stale = {**item, 'official_locator': 'turn 3'}
    request = {'document_sha256': digest, 'items': [stale]}
    assert ws.studio.confirm_segments(ws.job.name, request)['rejected'] == [
        {'id': segment['id'], 'reason': 'evidence_hash_mismatch'}]
    assert _digest(ws.output) == digest
    result = ws.studio.confirm_segments(ws.job.name, {**request, 'items': [item]})
    assert result['applied'] == [segment['id']]
    reviewed = json.loads(ws.output.read_bytes())
    assert reviewed['segments'][1]['ambiguous'] is False
    assert reviewed['segments'][2]['ambiguous'] is True
    for key in ('translation_import', 'translation_imports', 'translation_revisions'):
        assert reviewed[key] == document[key]
