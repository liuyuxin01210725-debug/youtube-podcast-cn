from copy import deepcopy
import hashlib
import json
from pathlib import Path
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

    def run(self, *, append=False):
        mode = ['--append'] if append else ['--base-document', str(self.base)]
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

    def fail_after_link(_):
        assert ws.output.is_file()
        raise OSError('fixture: directory sync failed after link')

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
