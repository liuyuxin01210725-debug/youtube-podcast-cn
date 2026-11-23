"""Edge render evidence exercised through the pinned SDK without opening a socket."""

import asyncio
import hashlib
import json
from pathlib import Path
import shutil

import edge_tts
import pytest

import studio_engine
import studio_publish_icloud as publisher
from studio_engine import StudioEngine, write_pcm_silence
from tts_backends import EdgeTTSBackend, SynthesisRequest


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_render_observes_sdk_input_and_reuses_only_bound_evidence(monkeypatch, tmp_path):
    engine = StudioEngine(tmp_path / 'edge-identity-fixture')
    engine.temp.mkdir()
    job = engine.job
    config = {'tts': 'edge', 'expected_speakers': 1}
    document = {'speakers': {'a': {'voice_slot': 'male_1'}}, 'references': {}, 'segments': [
        {'id': 's1', 'speaker_id': 'a', 'chinese': ' >> 你好[音乐] Edge 12，OK! ',
         'start_ms': 0, 'end_ms': 300, 'text': 'Synthetic sample'},
        {'id': 'ad', 'speaker_id': 'a', 'chinese': '', 'is_ad': True,
         'start_ms': 300, 'end_ms': 600, 'text': 'Synthetic ad'},
        {'id': 'cue', 'speaker_id': 'a', 'chinese': '！？',
         'start_ms': 600, 'end_ms': 900, 'text': 'Synthetic cue'},
    ]}
    document_bytes = json.dumps(document, ensure_ascii=False).encode()
    (job / 'document.json').write_bytes(document_bytes)
    (job / 'source-identity.json').write_text(json.dumps({
        'duration_ms': 1000, 'bytes': 1, 'sha256': hashlib.sha256(b'fixture source').hexdigest(),
    }))
    backend = EdgeTTSBackend(voice_map={'male_1': 'zh-TW-YunJheNeural'})
    monkeypatch.setattr(studio_engine, 'build_backend', lambda *a, **kw: backend)
    sdk = edge_tts.Communicate
    calls = []

    def communicate(text, voice, **kwargs):
        calls.append((text, voice, kwargs))
        return sdk(text, voice, **kwargs)

    async def save_without_network(self, path):
        write_pcm_silence(path)

    monkeypatch.setattr(sdk, 'save', save_without_network)
    monkeypatch.setattr(edge_tts, 'Communicate', communicate)
    target = engine.render(config, document)
    assert (job / 'document.json').read_bytes() == document_bytes
    assert document['segments'][0]['chinese'] == ' >> 你好[音乐] Edge 12，OK! '
    sent = '你好 Edge 12，OK!'
    assert calls == [(sent, 'zh-TW-YunJheNeural', {})]
    receipt_path = job / 'render-identity.json'
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt['audio_sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert receipt['document_sha256'] == studio_engine.fingerprint(document)
    assert receipt['edge_input_version'] == 1
    spoken, cue = receipt['timeline']
    assert [spoken['id'], cue['id']] == ['s1', 'cue']
    evidence = spoken['edge_input']
    assert evidence['voice_slot'] == 'male_1'
    assert evidence['codepoints'] == 14
    assert evidence['text_sha256'] == hashlib.sha256(sent.encode()).hexdigest()
    assert evidence['request']['voice'] == 'Microsoft Server Speech Text to Speech Voice (zh-TW, YunJheNeural)'
    assert evidence['request']['voice_argument'] == 'zh-TW-YunJheNeural'
    assert evidence['request']['language'] == 'zh-TW'
    assert evidence['request']['ssml_language'] == 'en-US'
    assert evidence['request']['speed'] == '+0%'
    assert evidence['request']['sdk_version'] == edge_tts.__version__
    assert cue['edge_input'] == {'mode': 'silence', 'codepoints': 0}
    assert cue['end_frame'] - cue['start_frame'] == studio_engine.SILENCE_FRAMES

    duration = publisher.probe_duration_sec(target)
    assert publisher._validate_render(job, config, duration) == receipt['document_sha256']
    for key, bad in [('speed', None), ('speed', '-7%'),
                     ('voice', 'Microsoft Server Speech Text to Speech Voice (zh-TW, HsiaoChenNeural)'),
                     ('language', 'en-US')]:
        bad_receipt = json.loads(receipt_path.read_bytes())
        bad_receipt['timeline'][0]['edge_input']['request'][key] = bad
        receipt_path.write_text(json.dumps(bad_receipt))
        with pytest.raises(publisher.PublishError, match='无法核验'):
            publisher._validate_render(job, config, duration)
        receipt_path.write_text(json.dumps(receipt))
    bad_receipt = json.loads(receipt_path.read_bytes())
    bad_receipt['timeline'][0]['edge_input']['codepoints'] += 1
    receipt_path.write_text(json.dumps(bad_receipt))
    with pytest.raises(publisher.PublishError, match='无法核验'):
        publisher._validate_render(job, config, duration)

    engine.render(config, document)
    assert json.loads(receipt_path.read_bytes())['timeline'] == receipt['timeline']
    assert len(calls) == 1
    # Removing or mismatching the per-clip evidence never authorizes another call
    # or lets a cached clip acquire today's request defaults.
    [sidecar] = [path for path in (job / 'audio-cache').glob('*.edge.json')
                 if json.loads(path.read_bytes())['edge_input']['mode'] == 'request']
    payload = json.loads(sidecar.read_bytes())
    payload['clip_sha256'] = '0' * 64
    sidecar.write_text(json.dumps(payload))
    for invalidate in (False, True):
        if invalidate:
            sidecar.unlink()
        engine.render(config, document)
        assert json.loads(receipt_path.read_bytes())['timeline'][0]['edge_input'] is None
        assert len(calls) == 1
        with pytest.raises(publisher.PublishError, match='无法核验'):
            publisher._validate_render(job, config, publisher.probe_duration_sec(target))


def test_speed_is_observed_from_sdk_instance(monkeypatch, tmp_path):
    sdk = edge_tts.Communicate

    async def local_save(self, path):
        Path(path).write_bytes(b'fixture audio')

    monkeypatch.setattr(sdk, 'save', local_save)
    monkeypatch.setattr(edge_tts, 'Communicate', lambda text, voice: sdk(text, voice, rate='-7%'))
    backend = EdgeTTSBackend(voice_map={'male_1': 'zh-TW-YunJheNeural'})
    usage = asyncio.run(backend.synthesize(
        SynthesisRequest('合成夹具。', 'male_1', 'conversation'), tmp_path / 'fixture.mp3',
    ))
    assert usage.edge_input['request']['speed'] == '-7%'


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_ordinary_publish_does_not_load_test_only_baseline(tmp_path):
    job = tmp_path / 'edge-fixture'
    job.mkdir()
    tone = (Path(__file__).resolve().parents[1] / 'fixtures' / 'point-listen' /
            'episodes' / 'point-listen-isolated' / 'output.mp3')
    shutil.copyfile(tone, job / 'output.mp3')
    # Test-only decoy. Neither the job configuration nor a nearby JSON is trusted calibration.
    test_only = {'chars_per_sec': 5, 'voice': 'fixture', 'speed': '+0%',
                 'language': 'zh-CN', 'source': 'test-only'}
    (job / 'baseline.json').write_text(json.dumps(test_only))
    (job / 'config.json').write_text(json.dumps({'baseline': test_only, 'character_count': 450}))
    library = tmp_path / 'private-fixture-library'
    library.mkdir()
    catalog = library / 'library.json'
    previous = b'{"episodes":[{"id":"old"}]}\n'
    catalog.write_bytes(previous)
    assert publisher.main(['--job-dir', str(job), '--library', str(library), '--no-device']) == 1
    assert {p.name: p.read_bytes() for p in library.iterdir()} == {'library.json': previous}
    report = json.loads((job / 'preflight' / 'preflight-report.json').read_bytes())
    assert report['verdict'] == 'REVIEW'
    finding = next(item for item in report['findings'] if item['code'] == 'duration_vs_chars')
    assert finding['severity'] == 'REVIEW'
    assert finding['character_count'] is None and 'baseline' not in finding
