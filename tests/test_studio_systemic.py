"""Offline reproductions of source, speaker, and worktree publication gates."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diarization_runtime
from podcast_captions import CaptionPiece
from studio_engine import StudioEngine, StudioError, StudioInputError, main
from studio_recovery import save_source
from studio_server import Studio, write
from studio_speakers import SpeakerGateError
from temporary_audio import sha256_file

ROOT = Path(__file__).resolve().parents[1]
REMOTE = {'source': {'kind': 'web', 'value': 'https://example.invalid/official-audio'}}


def pcm(path, milliseconds, sample=0):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(sample.to_bytes(2, 'little', signed=True) * milliseconds * 16)


def segment(ident, start, end, speaker='host', *, ambiguous=False):
    return {'id': ident, 'start_ms': start, 'end_ms': end, 'text': 'First. Second.',
            'chinese': '第一句。第二句。', 'speaker_id': speaker, 'ambiguous': ambiguous}


def document(rows):
    return {'segments': rows, 'references': {}, 'speakers': {
        'host': {'voice_slot': 'male_1'}, 'guest': {'voice_slot': 'male_2'},
    }}


def lock(engine, *, sha256='a' * 64, duration=1000):
    identity = {**REMOTE['source'], 'sha256': sha256, 'bytes': 100, 'duration_ms': duration}
    write(engine.job / 'source-identity.json', identity)
    return identity


@pytest.mark.parametrize('checkpoint', [False, True], ids=['fresh-prepare', 'translation-resume'])
def test_acquired_bytes_lock_rejects_same_url_with_different_audio(tmp_path, monkeypatch, checkpoint):
    engine = StudioEngine(tmp_path / 'job')
    observed = []
    first_clip = tmp_path / 'first.wav'
    pcm(first_clip, 1000)
    acquire = Mock(return_value=first_clip)
    monkeypatch.setattr(engine, 'acquire', acquire)

    def stop_at_diarization(config, wav):
        observed.append(wav)
        raise SpeakerGateError('synthetic count failure')

    monkeypatch.setattr(engine, '_prepare_source', stop_at_diarization)
    with pytest.raises(SpeakerGateError):
        engine.run(REMOTE, 'prepare')
    saved = (engine.job / 'source-identity.json').read_bytes()
    assert json.loads(saved) == {
        **REMOTE['source'], 'sha256': sha256_file(first_clip),
        'bytes': first_clip.stat().st_size, 'duration_ms': 1000,
    }
    if checkpoint:
        save_source(engine.job / 'translation-source.json', 'synthetic binding', {'segments': []})
    second_clip = tmp_path / 'other.wav'
    pcm(second_clip, 1000, sample=31)
    assert first_clip.stat().st_size == second_clip.stat().st_size
    assert sha256_file(first_clip) != sha256_file(second_clip)
    acquire.return_value = second_clip
    client = Mock(side_effect=AssertionError('changed source reached translation'))
    monkeypatch.setattr('studio_engine.create_translation_client', client)
    with pytest.raises(StudioInputError, match='来源已经改变'):
        engine.run(REMOTE, 'prepare')
    assert acquire.call_count == 2 and observed == [first_clip]
    client.assert_not_called()
    assert (engine.job / 'source-identity.json').read_bytes() == saved


@pytest.mark.parametrize('speaker_count', [1, 2], ids=['cryan-single-cluster', 'short-second-speaker'])
def test_exact_fold_report_survives_job_failure_and_preserves_count(tmp_path, monkeypatch, speaker_count):
    engine = StudioEngine(tmp_path / 'job')
    source = tmp_path / 'source.wav'
    pcm(source, 26000)
    turns = [{'speaker_id': 'SPEAKER_00', 'start_ms': 0, 'end_ms': 22000}]
    pieces = [CaptionPiece(0, 1000, 'Host words.', 0)]
    if speaker_count == 2:
        turns.append({'speaker_id': 'SPEAKER_01', 'start_ms': 22000, 'end_ms': 26000})
        pieces.append(CaptionPiece(22500, 23500, 'Guest words.', 1))

    def worker(command, **kwargs):
        assert Path(command[1]).resolve() == ROOT / 'scripts/sherpa_diarization_worker.py'
        assert command[command.index('--num-speakers') + 1] == '2'
        write(Path(command[command.index('--output') + 1]), {'turns': turns})
        return 0, ''

    no_translation = Mock(side_effect=StudioError('test_stop', '离线夹具在翻译前停止'))
    monkeypatch.setattr('studio_engine.create_translation_client', no_translation)
    monkeypatch.setattr('studio_engine.ensure_diarization_runtime', lambda **_: SimpleNamespace(
        python=Path(sys.executable), segmentation_model=source, embedding_model=source))
    monkeypatch.setattr(diarization_runtime, '_stream_worker', worker)
    monkeypatch.setattr(engine, 'acquire', lambda _: source)
    monkeypatch.setattr(engine, 'transcribe', lambda *_: (pieces, 'asr'))
    monkeypatch.setattr(engine, 'ffmpeg', lambda *args: Path(args[-1]).write_bytes(b'fixture reference'))
    cause = SpeakerGateError if speaker_count == 1 else StudioError
    with pytest.raises(cause):
        engine.run({**REMOTE, 'expected_speakers': 2}, 'prepare')

    expected = {'cluster_count': speaker_count, 'duration_ms_by_id': {'SPEAKER_00': 22000}}
    if speaker_count == 2:
        expected['duration_ms_by_id']['SPEAKER_01'] = 4000
    report = json.loads((engine.job / 'debug-speaker-folds.json').read_bytes())
    assert report['expected_speakers'] == 2
    assert [fold['stage'] for fold in report['folds']] == ['chunk_budget', 'minor_speakers']
    assert all(fold['before'] == fold['after'] == expected for fold in report['folds'])
    assert not engine.temp.exists()
    failure = json.loads((engine.job / 'debug-failure.json').read_bytes())
    if speaker_count == 1:
        assert failure['error_code'] == 'speaker_gate_failed'
        no_translation.assert_not_called()
    else:
        assert failure['error_code'] == 'test_stop'
        no_translation.assert_called_once()
    runtime = json.loads((engine.job / 'debug-runtime.json').read_bytes())
    runtime_file = ROOT / 'scripts/diarization_runtime.py'
    assert runtime == {'engine': str(ROOT / 'scripts/studio_engine.py'),
                       'diarization_runtime': str(runtime_file),
                       'diarization_runtime_sha256': sha256_file(runtime_file)}


def test_worktree_never_labels_another_runtime_as_its_own(tmp_path, monkeypatch):
    studio = Studio(tmp_path / 'jobs')
    from diarization_core import SpeakerBounds

    engine = StudioEngine(tmp_path / 'jobs' / 'job')
    assert Path(studio.engine[-1]).resolve() == ROOT / 'scripts/studio_engine.py'
    assert '-E' in studio.engine
    monkeypatch.setattr(diarization_runtime, '__file__', str(tmp_path / 'main/diarization_runtime.py'))
    setup = Mock(side_effect=AssertionError('foreign runtime used'))
    monkeypatch.setattr('studio_engine.ensure_diarization_runtime', setup)
    with pytest.raises(StudioError, match='另一工作树'):
        engine.diarize_with_progress(tmp_path / 'unused.wav', SpeakerBounds(exact=2))
    setup.assert_not_called()
    assert not (engine.job / 'debug-runtime.json').exists()


def test_jacka_overflow_precedes_render_and_survives_tail_deletion(tmp_path, monkeypatch):
    engine = StudioEngine(tmp_path / 'job')
    fixture_sha = 'b1898dd2' + '0' * 56  # Synthetic stand-in for the brief's hash prefix.
    identity = lock(engine, sha256=fixture_sha)
    rows = [segment('seg-000001', 0, 1000)] + [
        segment(f'seg-{i + 2:06d}', 1000 + i * 100, 1100 + i * 100) for i in range(12)
    ]
    doc = document(rows)
    saved = engine.job / 'document.json'
    write(saved, doc)
    save_source(engine.job / 'translation-source.json', 'synthetic binding',
                {**doc, 'media_sha256': identity['sha256']})
    previous = engine.job / 'output.mp3'
    previous.write_bytes(b'last known good output')
    render, publish, push = Mock(return_value=previous), Mock(), Mock()
    monkeypatch.setattr(engine, 'render', render)
    monkeypatch.setattr(engine, 'publish_private_library', publish)
    monkeypatch.setattr(engine, 'push_private_device', push)

    before = saved.read_bytes()
    with pytest.raises(StudioError) as failure:
        engine.run(REMOTE, 'render')
    assert failure.value.code == 'source_coverage_failed'
    assert saved.read_bytes() == before
    report = json.loads((engine.job / 'debug-source-coverage.json').read_bytes())
    assert report['source_sha256'] == fixture_sha
    assert report['last_end_ms'] == 2200
    assert report['overflow_segment_ids'] == [row['id'] for row in rows[1:]]
    # Removing overflow rows cannot turn the original out-of-bounds source into a release.
    write(saved, document(rows[:1]))
    with pytest.raises(StudioError, match='禁止删尾过闸'):
        engine.run(REMOTE, 'render')
    render.assert_not_called()
    publish.assert_not_called()
    push.assert_not_called()
    assert previous.read_bytes() == b'last known good output'


def test_coverage_is_rechecked_after_synthesis(tmp_path, monkeypatch):
    engine = StudioEngine(tmp_path / 'job')
    lock(engine)
    write(engine.job / 'document.json', document([segment('seg-000001', 0, 1000)]))

    def finished(config, doc, *args, **kwargs):
        doc['segments'][-1]['end_ms'] = 1001
        path = engine.job / 'output.mp3'
        path.write_bytes(b'synthetic successful synthesis')
        return path

    monkeypatch.setattr(engine, 'render', finished)
    publish, push = Mock(), Mock()
    monkeypatch.setattr(engine, 'publish_private_library', publish)
    monkeypatch.setattr(engine, 'push_private_device', push)
    with pytest.raises(StudioError, match='锁定音源时长'):
        engine.run(REMOTE, 'render')
    publish.assert_not_called()
    push.assert_not_called()


def test_laschi_mixed_voice_mapping_requires_explicit_confirmation_without_retranslation(tmp_path, monkeypatch):
    studio = Studio(tmp_path / 'jobs')
    engine = StudioEngine(studio.root / 'laschi-fixture')
    config = {**REMOTE, 'expected_speakers': 2, 'prefer_male_slots': False}
    source = tmp_path / 'source.wav'
    pcm(source, 4000)
    from diarization_core import DiarizationTurn

    monkeypatch.setattr(engine, 'transcribe', lambda *_: (
        [CaptionPiece(0, 900, 'Host words.', 0), CaptionPiece(2100, 2900, 'Guest words.', 1)], 'asr'))
    monkeypatch.setattr(engine, 'diarize_with_progress', lambda *_: [
        DiarizationTurn(0, 2000, 'host'), DiarizationTurn(2000, 4000, 'guest'),
    ])
    monkeypatch.setattr(engine, 'ffmpeg', lambda *args: Path(args[-1]).write_bytes(b'reference'))
    draft = engine._prepare_source(config, source)
    assert draft['speakers'] == {'host': {'voice_slot': None}, 'guest': {'voice_slot': None}}
    assert draft.get('voices_confirmed') is not True
    rows = [segment('seg-000001', 0, 900), segment('seg-000002', 2100, 2900, 'guest')]
    doc = {**draft, 'segments': rows}
    doc_path = engine.job / 'document.json'
    write(doc_path, doc)
    write(engine.job / 'config.json', config)
    with pytest.raises(ValueError, match='逐人设置'):
        studio.reset_voice_slots(engine.job.name)
    with pytest.raises(SpeakerGateError, match='音色硬闸'):
        studio.confirm_voices(engine.job.name)

    # A legacy automatically assigned male map plus a boolean is insufficient.
    write(doc_path, {**doc, 'speakers': document(rows)['speakers'], 'voices_confirmed': True})
    synth = Mock(side_effect=StudioError('test_stop', '离线夹具在 TTS 前停止'))
    monkeypatch.setattr('studio_engine.build_backend', synth)
    retranslate = Mock(side_effect=AssertionError('voice edit requested translation'))
    monkeypatch.setattr(engine, 'prepare', retranslate)
    lock(engine, duration=4000)
    with pytest.raises(SpeakerGateError, match='confirm-voices'):
        engine.run(config, 'render', publish=False)
    synth.assert_not_called()
    studio.update_document(engine.job.name, {'segments': rows, 'voices': {'guest': 'female_1'}})
    studio.confirm_voices(engine.job.name)
    reviewed = json.loads(doc_path.read_bytes())
    assert reviewed['segments'] == rows
    assert reviewed['confirmed_voice_slots'] == {'host': 'male_1', 'guest': 'female_1'}
    with pytest.raises(StudioError, match='TTS 前停止'):
        engine.run(config, 'render', publish=False)
    retranslate.assert_not_called()
    synth.assert_called_once()


def test_farrar_split_preserves_id_and_only_edited_row_is_confirmed(tmp_path):
    studio = Studio(tmp_path / 'jobs')
    ident = 'farrar-fixture'
    job = studio.root / ident
    job.mkdir()
    path = job / 'document.json'
    rows = [segment('seg-000006', 0, 1000, ambiguous=True),
            segment('seg-000007', 1000, 2000, 'guest', ambiguous=True)]
    write(path, document(rows))
    result = studio.split_segment(ident, {'id': 'seg-000006', 'cut_ms': 500,
        'left': {'text': 'First.', 'chinese': '第一句。', 'speaker_id': 'host'},
        'right': {'text': 'Second.', 'chinese': '第二句。', 'speaker_id': 'guest'},
    })
    assert result['left_id'] == 'seg-000006' and result['right_id'] == 'seg-000008'
    split = json.loads(path.read_bytes())['segments']
    assert split[2] == rows[1]
    split[1]['speaker_id'] = 'host'
    studio.update_document(ident, {'segments': split})
    assert [s['ambiguous'] for s in json.loads(path.read_bytes())['segments']] == [True, False, True]
    before = path.read_bytes()
    with pytest.raises(ValueError, match='confirm-segments'):
        studio.confirm_speakers(ident)
    assert path.read_bytes() == before
    ui = (ROOT / 'studio_static/app.js').read_text()
    assert '全部按当前说话人确认' not in ui and '/confirm-speakers' not in ui
    assert all(option in ui for option in ('/split-segment', '/confirm-voices', '逐段'))


def test_standalone_render_and_failed_job_are_not_picked_up_for_publish(tmp_path, monkeypatch):
    studio = Studio(tmp_path / 'jobs')
    engine = StudioEngine(studio.root / 'failed-job')
    state_path = engine.job / 'state.json'
    write(state_path, {'status': 'failed', 'action': 'render'})
    lock(engine)
    write(engine.job / 'document.json', document([segment('seg-000001', 0, 1000)]))
    config = engine.job / 'config.json'
    write(config, REMOTE)
    library = tmp_path / 'other-library'
    monkeypatch.setenv('PRIVATE_LISTEN_LIBRARY', str(library))
    monkeypatch.setattr(sys, 'argv', ['studio_engine.py', '--action', 'render',
                                     '--job-dir', str(engine.job), '--config', str(config)])
    output = engine.job / 'output.mp3'
    output.write_bytes(b'standalone output')
    monkeypatch.setattr(StudioEngine, 'render', Mock(return_value=output))
    publish = Mock(side_effect=AssertionError('unexpected publish'))
    monkeypatch.setattr(StudioEngine, 'publish_private_library', publish)
    monkeypatch.setattr('studio_engine.signal.signal', Mock())
    assert main() == 0
    monkeypatch.setattr('studio_server.time.sleep', lambda *_: setattr(studio, 'stop', True))
    studio.worker()
    assert json.loads(state_path.read_bytes()) == {'status': 'failed', 'action': 'render'}
    publish.assert_not_called()
    assert not library.exists()
