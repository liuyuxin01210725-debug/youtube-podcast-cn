from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

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
        self.cli = [sys.executable, '-B', str(SCRIPT), '--job-dir', str(self.job),
                    '--base-document', str(self.base), '--source-audio', self.source.name,
                    '--manifest', str(self.manifest), '--export', str(self.export)]
        self.document = {
            'schema_version': 1, 'transcript_source': 'asr',
            'speakers': {'s1': {'voice_slot': 'male_1'}, 's2': {'voice_slot': 'male_2'}},
            'references': {}, 'voices_confirmed': False,
            'segments': [
                {'id': f'seg-{i:06d}', 'speaker_id': speaker, 'text': 'Existing ASR',
                 'chinese': '原译', 'ambiguous': True, 'start_ms': i, 'end_ms': i + 1}
                for i, speaker in enumerate(('s1', 's2', 's1'), 1)
            ],
        }
        _write(self.base, self.document)
        self.notes = self.job / 'notes.json'
        _write(self.notes, {'note': '保留'})
        self.source.write_bytes(b'fixture source bytes')
        _write(self.lock, {'sha256': _digest(self.source)})
        selection = [{key: row[key] for key in ('id', 'speaker_id')}
                     for row in self.document['segments'][::2]]
        self.rows = [{**row, 'zh': f'  译文 {i}  '} for i, row in enumerate(selection, 1)]
        _write(self.export, self.rows)
        self.plan = {
            'schema_version': 1, 'document_sha256': _digest(self.base),
            'source_sha256': _digest(self.source), 'export_sha256': _digest(self.export),
            'segments': selection,
        }
        _write(self.manifest, self.plan)

    def run(self):
        return subprocess.run(self.cli, text=True, capture_output=True, check=False)

    def snapshot(self):
        return {str(path.relative_to(self.job.parent)): _digest(path)
                for path in self.job.parent.rglob('*') if path.is_file()
                and path.name != '.podcast-run.lock'}


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


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
