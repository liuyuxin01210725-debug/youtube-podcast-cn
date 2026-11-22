"""Offline regressions for Edge retry, Studio cache reuse and failure reporting."""

import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import wave
from unittest.mock import AsyncMock, Mock

import aiohttp
from aiohttp.client_reqrep import ConnectionKey
import edge_tts
import pytest
from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_engine
import studio_server
import tts_backends
from studio_engine import Cancelled, StudioEngine, write_pcm_silence
from tts_backends import EdgeTTSBackend, SynthesisRequest, TTSBackendError


FAKE_CONTEXT = (
    'wss://speech.invalid/path?sig=FAKE_QUERY Authorization: Basic FAKE_AUTH '
    "headers={'X-Private':'FAKE_HEADER'} token: FAKE_TOKEN FAKE_BODY"
)
FORBIDDEN = ('speech.invalid', 'FAKE_QUERY', 'FAKE_AUTH', 'FAKE_HEADER',
             'FAKE_TOKEN', 'FAKE_BODY', 'Authorization', 'X-Private')


def connector_error():
    key = ConnectionKey('speech.invalid', 443, True, True, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError(None, FAKE_CONTEXT))


def handshake(status):
    request = aiohttp.RequestInfo(URL('wss://speech.invalid/?sig=FAKE_QUERY'),
                                  'GET', {'Authorization': 'Basic FAKE_AUTH'})
    return aiohttp.WSServerHandshakeError(
        request, (), status=status, message='Invalid response status',
        headers={'X-Private': 'FAKE_HEADER'},
    )


def use_edge(monkeypatch, outcomes):
    calls = []
    instances = []
    results = iter(outcomes)
    unfinished = []

    class OfflineCommunicate:
        def __init__(self, text, voice):
            self.text = text
            instances.append(self)

        async def save(self, filename):
            assert self not in calls, 'Edge stream was reused'
            path = Path(filename)
            assert all(not partial.exists() for partial in unfinished), 'half-written file survived into retry'
            calls.append(self)
            outcome = next(results)
            if outcome is None:
                write_pcm_silence(path)
                return
            path.write_bytes(b'partial audio')
            unfinished.append(path)
            raise outcome

    monkeypatch.setattr(edge_tts, 'Communicate', OfflineCommunicate)
    sleep = AsyncMock()
    monkeypatch.setattr(tts_backends.asyncio, 'sleep', sleep)
    return calls, instances, sleep


def synthesize(path, text='合成测试文本', slot='male_1'):
    return asyncio.run(EdgeTTSBackend().synthesize(SynthesisRequest(text, slot, 'conversation'), path))


@pytest.mark.parametrize('failures', [1, 2])
def test_connector_recovers_with_fresh_stream_and_clean_file(monkeypatch, tmp_path, failures):
    calls, instances, sleep = use_edge(monkeypatch, [connector_error() for _ in range(failures)] + [None])
    output = tmp_path / 'spoken.mp3'
    synthesize(output)
    assert calls == instances and len(calls) == failures + 1
    assert sleep.await_count == failures
    assert tuple(call.args[0] for call in sleep.await_args_list) == (1.0, 2.0)[:failures]
    with wave.open(str(output), 'rb') as clip:
        assert clip.getnframes() > 0


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_transient_http_status_wins_over_handshake_message(monkeypatch, tmp_path, status):
    calls, _, _ = use_edge(monkeypatch, [handshake(status), None])
    synthesize(tmp_path / 'spoken.mp3')
    assert len(calls) == 2


@pytest.mark.parametrize('error_type', [aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError,
                                        TimeoutError, edge_tts.exceptions.NoAudioReceived,
                                        edge_tts.exceptions.WebSocketError])
def test_stream_failures_retry_for_speakable_text(monkeypatch, tmp_path, error_type):
    calls, _, _ = use_edge(monkeypatch, [error_type(FAKE_CONTEXT), None])
    synthesize(tmp_path / 'spoken.mp3')
    assert len(calls) == 2


def test_connector_exhaustion_removes_partial_output(monkeypatch, tmp_path):
    calls, _, sleep = use_edge(monkeypatch, [connector_error() for _ in range(6)])
    output = tmp_path / 'spoken.mp3'
    with pytest.raises(aiohttp.ClientConnectorError):
        synthesize(output)
    assert len(calls) == 6
    assert tuple(call.args[0] for call in sleep.await_args_list) == (1.0, 2.0, 4.0, 8.0, 15.0)
    assert not output.exists()


def test_failed_replacement_keeps_previous_complete_output(monkeypatch, tmp_path):
    use_edge(monkeypatch, [connector_error() for _ in range(6)])
    output = tmp_path / 'spoken.mp3'
    write_pcm_silence(output)
    previous = output.read_bytes()
    with pytest.raises(aiohttp.ClientConnectorError):
        synthesize(output)
    assert output.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [output]


def test_403_has_safe_upgrade_hint_and_no_retry(monkeypatch, tmp_path):
    calls, _, sleep = use_edge(monkeypatch, [handshake(403)])
    output = tmp_path / 'spoken.mp3'
    with pytest.raises(TTSBackendError, match='edge-tts') as captured:
        synthesize(output)
    assert not captured.value.retryable
    assert len(calls) == 1 and not output.exists()
    assert_safe(str(captured.value))
    sleep.assert_not_awaited()


@pytest.mark.parametrize('error', [
    handshake(401), handshake(404), handshake(501), RuntimeError(FAKE_CONTEXT),
    ValueError('bad voice'), RuntimeError('Invalid response status'), OSError(errno.ENOSPC, 'disk full'),
])
def test_permanent_failures_pass_through_without_retry(monkeypatch, tmp_path, error):
    calls, _, sleep = use_edge(monkeypatch, [error])
    output = tmp_path / 'spoken.mp3'
    with pytest.raises(type(error)) as captured:
        synthesize(output)
    assert captured.value is error
    assert len(calls) == 1 and not output.exists()
    sleep.assert_not_awaited()


def test_invalid_slot_never_constructs_communicate(monkeypatch, tmp_path):
    calls, instances, sleep = use_edge(monkeypatch, [])
    with pytest.raises(ValueError, match='voice slot'):
        synthesize(tmp_path / 'spoken.mp3', slot='not-a-slot')
    assert not calls and not instances
    sleep.assert_not_awaited()


def test_no_audio_for_unspeakable_text_does_not_retry(monkeypatch, tmp_path):
    calls, _, sleep = use_edge(monkeypatch, [edge_tts.exceptions.NoAudioReceived(FAKE_CONTEXT)])
    output = tmp_path / 'part.mp3'
    with pytest.raises(edge_tts.exceptions.NoAudioReceived):
        synthesize(output, text='？！')
    assert len(calls) == 1 and not output.exists()
    sleep.assert_not_awaited()


@pytest.mark.parametrize('cancel_type', [Cancelled, asyncio.CancelledError])
@pytest.mark.parametrize('during_backoff', [False, True])
def test_cancel_propagates_and_cleans_partial(monkeypatch, tmp_path, cancel_type, during_backoff):
    cancel = cancel_type('cancelled')
    calls, _, sleep = use_edge(monkeypatch, [connector_error() if during_backoff else cancel])
    sleep.side_effect = cancel
    output = tmp_path / 'spoken.mp3'
    with pytest.raises(cancel_type) as captured:
        synthesize(output)
    assert captured.value is cancel
    assert sleep.await_count == int(during_backoff)
    assert len(calls) == 1 and not output.exists()


def test_hanging_save_hits_attempt_deadline(monkeypatch, tmp_path):
    calls = []

    class HangingCommunicate:
        def __init__(self, text, voice):
            pass

        async def save(self, filename):
            calls.append(filename)
            Path(filename).write_bytes(b'partial')
            await asyncio.Event().wait()

    monkeypatch.setattr(edge_tts, 'Communicate', HangingCommunicate)
    monkeypatch.setattr(tts_backends, '_EDGE_ATTEMPT_TIMEOUT', .02)
    monkeypatch.setattr(tts_backends.asyncio, 'sleep', AsyncMock())
    output = tmp_path / 'spoken.mp3'
    with pytest.raises(TimeoutError):
        synthesize(output)
    assert len(calls) == 6 and not list(tmp_path.iterdir())


def setup_job(tmp_path, texts):
    engine = StudioEngine(tmp_path / 'synthetic-job')
    speaker = {'voice_slot': 'male_1'}
    segments = [
        {'id': f'seg-{i:06d}', 'chinese': text, 'speaker_id': 'narrator',
         'start_ms': (i - 1) * 1000, 'end_ms': i * 1000}
        for i, text in enumerate(texts, 1)
    ]
    document = {'segments': segments, 'speakers': {'narrator': speaker}, 'references': {}}
    (engine.job / 'document.json').write_text(json.dumps(document), encoding='utf-8')
    config = {'source': {'kind': 'youtube'}, 'tts': 'edge', 'expected_speakers': 1}
    (engine.job / 'source-identity.json').write_text(json.dumps({
        **config['source'], 'sha256': 'a' * 64, 'bytes': 100, 'duration_ms': 1000 * len(segments),
    }))
    (engine.job / 'config.json').write_text(json.dumps(config))
    return engine, config


def read_job(engine, name):
    return json.loads((engine.job / name).read_text(encoding='utf-8'))


def assert_safe(text):
    for value in FORBIDDEN:
        assert value not in text


@pytest.mark.parametrize('error', [
    studio_engine.StudioError('process_failed', '转译失败 ?sig=FAKE_QUERY'),
    studio_engine.StudioError('process_failed', '转译失败 Authorization: Basic FAKE_AUTH'),
    studio_engine.StudioError('process_failed', "转译失败 headers={'X-Private':'FAKE_HEADER'}"),
    studio_engine.StudioError('process_failed', '转译失败 token: FAKE_TOKEN'),
    TTSBackendError(FAKE_CONTEXT + 'A' * 20000),
    TTSBackendError('FAKE_BODY'),
    studio_engine.PodcastPipelineError('FAKE_BODY'),
    RuntimeError('FAKE_BODY'),
])
def test_debug_and_progress_reject_unapproved_provider_text(monkeypatch, tmp_path, error):
    engine = StudioEngine(tmp_path / 'synthetic-job')

    def fail(config):
        raise error

    monkeypatch.setattr(engine, 'acquire', fail)
    with pytest.raises(type(error)):
        engine.run({'source': {'kind': 'youtube'}}, 'prepare')
    for name in ('debug-failure.json', 'progress.json'):
        text = (engine.job / name).read_text()
        assert len(text.encode()) < 1024
        assert_safe(text)


def test_controlled_exception_message_is_capped_before_persistence(monkeypatch, tmp_path):
    engine = StudioEngine(tmp_path / 'synthetic-job')
    message = '请检查输入。' * 200

    def fail(config):
        raise studio_engine.StudioInputError(message)

    monkeypatch.setattr(engine, 'acquire', fail)
    with pytest.raises(studio_engine.StudioInputError):
        engine.run({'source': {'kind': 'youtube'}}, 'prepare')
    assert read_job(engine, 'debug-failure.json')['exc_message'] == message[:500]
    assert read_job(engine, 'progress.json')['message'] == message[:500]


@pytest.mark.parametrize('during_backoff', [False, True])
def test_server_cancel_marker_stops_engine_inside_edge(monkeypatch, tmp_path, during_backoff):
    engine, config = setup_job(tmp_path, ['片段甲'])
    calls = []

    async def cancel_and_block():
        (engine.job / 'cancel').touch()
        await asyncio.Event().wait()

    class BlockingCommunicate:
        def __init__(self, text, voice):
            calls.append(text)

        async def save(self, filename):
            Path(filename).write_bytes(b'partial')
            if during_backoff:
                raise connector_error()
            await cancel_and_block()

    monkeypatch.setattr(edge_tts, 'Communicate', BlockingCommunicate)
    monkeypatch.setattr(tts_backends.asyncio, 'sleep', lambda delay: cancel_and_block())
    with pytest.raises(Cancelled):
        engine.run(config, 'render')
    progress = read_job(engine, 'progress.json')
    assert (progress['stage'], progress['completed'], progress['total']) == ('cancelled', 0, 1)
    assert len(calls) == 1 and not engine.temp.exists()
    assert not list((engine.job / 'audio-cache').glob('*.wav'))


def baseline_key(text, spoken=None):
    voice_map = {
        'default': 'zh-CN-XiaoxiaoNeural', 'male_1': 'zh-CN-YunyangNeural',
        'male_2': 'zh-CN-YunjianNeural', 'male_3': 'zh-CN-YunxiNeural',
        'female_1': 'zh-CN-XiaoxiaoNeural', 'female_2': 'zh-CN-XiaoyiNeural',
    }
    identity = {
        'text': text, 'voice': {'voice_slot': 'male_1'}, 'backend': 'edge',
        'style': 'conversation', 'model': {
            'backend': 'edge', 'model': 'edge-tts', 'voices': voice_map, 'max_chars': 2500,
        },
        'version': 1, 'revision': '50f45ef0047cde7e84c2ef04326acb8ada2436a7',
    }
    if spoken is not None:
        identity['spoken'] = spoken
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest() + '.wav'


requires_ffmpeg = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')


@requires_ffmpeg
def test_exhaustion_preserves_pcm_clips_and_records_failed_segment(monkeypatch, tmp_path):
    engine, config = setup_job(tmp_path, ['片段甲', '片段乙', '片段丙'])
    cache = engine.job / 'audio-cache'
    cache.mkdir()
    preserved = cache / baseline_key('片段甲')
    write_pcm_silence(preserved)
    original = preserved.read_bytes()
    calls, _, _ = use_edge(monkeypatch, [None] + [connector_error() for _ in range(6)])

    with pytest.raises(aiohttp.ClientConnectorError):
        engine.run(config, 'render')

    assert [item.text for item in calls] == ['片段乙'] + ['片段丙'] * 6
    files = sorted(cache.glob('*.wav'))
    assert set(p.name for p in files) == {baseline_key('片段甲'), baseline_key('片段乙')}
    assert preserved.read_bytes() == original
    assert all(engine.valid_audio(path) for path in files)
    assert not engine.temp.exists()
    debug = read_job(engine, 'debug-failure.json')
    assert (debug['stage'], debug['segment_id'], debug['exc_type']) == (
        'synthesis', 'seg-000003', 'ClientConnectorError',
    )
    progress = read_job(engine, 'progress.json')
    assert (progress['stage'], progress['completed'], progress['total']) == ('failed', 2, 3)
    assert progress['segment_id'] == 'seg-000003'
    assert_safe(json.dumps([debug, progress]))


@requires_ffmpeg
def test_baseline_edge_keys_hit_with_zero_provider_calls(monkeypatch, tmp_path):
    engine, config = setup_job(tmp_path, ['片段甲', '>> 片段乙[音乐]'])
    cache = engine.job / 'audio-cache'
    cache.mkdir()
    for name in (baseline_key('片段甲'), baseline_key('>> 片段乙[音乐]', '片段乙')):
        write_pcm_silence(cache / name)
    before = {p.name: p.read_bytes() for p in cache.iterdir()}
    calls, instances, _ = use_edge(monkeypatch, [])
    backend = EdgeTTSBackend()
    synth = AsyncMock(wraps=backend.synthesize)
    monkeypatch.setattr(backend, 'synthesize', synth)
    monkeypatch.setattr(studio_engine, 'build_backend', lambda *args, **kwargs: backend)
    output = engine.run(config, 'render', preview=2)
    assert engine.valid_audio(output)
    synth.assert_not_called()
    assert not calls and not instances
    assert {p.name: p.read_bytes() for p in cache.iterdir()} == before


@requires_ffmpeg
def test_studio_keeps_punctuation_silence_without_edge_calls(monkeypatch, tmp_path):
    engine, config = setup_job(tmp_path, ['？！'])
    calls, instances, _ = use_edge(monkeypatch, [])
    assert engine.valid_audio(engine.run(config, 'render', preview=1))
    assert not calls and not instances


@requires_ffmpeg
def test_export_failure_has_no_stale_segment(monkeypatch, tmp_path):
    engine, config = setup_job(tmp_path, ['片段甲'])
    use_edge(monkeypatch, [None])
    error = RuntimeError(FAKE_CONTEXT + 'A' * 20000)
    original = Path.write_text

    def fail_manifest(path, *args, **kwargs):
        # The concat manifest belongs to export, even before ffmpeg starts.
        if path.name == 'concat.txt':
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', fail_manifest)
    with pytest.raises(RuntimeError):
        engine.run(config, 'render')
    progress = read_job(engine, 'progress.json')
    assert (progress['failed_stage'], progress['completed'], progress['total']) == ('export', 1, 1)
    assert 'segment_id' not in progress
    debug = read_job(engine, 'debug-failure.json')
    assert debug['stage'] == 'export' and debug['segment_id'] is None
    assert debug['exc_type'] == 'UnexpectedError'
    assert len(debug['exc_message']) <= 500
    assert_safe(json.dumps(debug))


def test_debug_failure_is_cleared_after_successful_render(monkeypatch, tmp_path):
    engine, config = setup_job(tmp_path, ['片段甲'])
    debug = engine.job / 'debug-failure.json'
    prior = b'{"stage":"synthesis","segment_id":"seg-000001"}'
    debug.write_bytes(prior)
    output = engine.job / 'preview.mp3'

    def render(*args, **kwargs):
        assert debug.read_bytes() == prior
        return output

    monkeypatch.setattr(engine, 'render', render)
    assert engine.run(config, 'render', preview=1) == output
    assert read_job(engine, 'progress.json')['stage'] == 'complete'
    assert not debug.exists()


@pytest.mark.parametrize('error_type', [None, Cancelled, asyncio.CancelledError, RuntimeError])
def test_cleanup_errors_preserve_terminal_progress_and_artifacts(monkeypatch, tmp_path, error_type):
    engine, config = setup_job(tmp_path, ['片段甲'])
    debug = engine.job / 'debug-failure.json'
    debug.write_text('{"stage":"synthesis"}')
    output = engine.job / 'output.mp3'
    output.write_bytes(b'synthetic completed audio')
    library = tmp_path / 'library'

    def render(*args, **kwargs):
        engine.progress('export', 1, 1)
        if error_type is not None:
            raise error_type('synthetic primary outcome')
        from studio_engine import fingerprint, sha256_file

        (engine.job / 'render-identity.json').write_text(json.dumps({
            'schema_version': 1, 'sample_rate': 24000, 'audio_sha256': sha256_file(output),
            'source_identity_sha256': sha256_file(engine.job / 'source-identity.json'),
            'document_sha256': fingerprint(read_job(engine, 'document.json')),
            'timeline': [{'id': 'seg-000001', 'start_frame': 0, 'end_frame': 24000}],
        }))
        return output

    original_rmtree = shutil.rmtree
    original_unlink = Path.unlink

    def fail_temporary(path, *args, **kwargs):
        if path == engine.temp:
            raise PermissionError(errno.EACCES, 'synthetic temporary cleanup error')
        return original_rmtree(path, *args, **kwargs)

    def fail_debug(path, *args, **kwargs):
        if path == debug:
            raise PermissionError(errno.EPERM, 'synthetic debug cleanup error')
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setenv('PRIVATE_LISTEN_LIBRARY', str(library))
    monkeypatch.setattr(engine, 'render', render)
    monkeypatch.setattr(engine, 'push_private_device', Mock())
    monkeypatch.setattr('studio_publish_icloud.probe_duration_sec', lambda _: 1)
    monkeypatch.setattr('studio_artwork.attach_cover', lambda *args, **kwargs: None)
    monkeypatch.setattr(shutil, 'rmtree', fail_temporary)
    monkeypatch.setattr(Path, 'unlink', fail_debug)

    if error_type is None:
        assert engine.run(config, 'render') == output
        expected = 'complete'
        assert read_job(engine, 'private-listen.json')['ok'] is True
        assert (library / 'episodes' / engine.job.name / output.name).read_bytes() == output.read_bytes()
        assert json.loads((library / 'library.json').read_bytes())['episodes'][0]['id'] == engine.job.name
        assert read_job(engine, 'cleanup-debug-failure.json')['errno'] == errno.EPERM
        engine.push_private_device.assert_called_once()
        assert read_job(engine, 'debug-failure.json')['stage'] == 'synthesis'
    else:
        with pytest.raises(error_type, match='synthetic primary outcome'):
            engine.run(config, 'render')
        expected = 'failed' if error_type is RuntimeError else 'cancelled'
        assert not (library / 'library.json').exists()
        assert not (engine.job / 'cleanup-debug-failure.json').exists()
        engine.push_private_device.assert_not_called()
        assert read_job(engine, 'debug-failure.json')['stage'] == (
            'export' if expected == 'failed' else 'synthesis'
        )
    assert output.read_bytes() == b'synthetic completed audio'
    progress = read_job(engine, 'progress.json')
    assert progress['stage'] == expected
    assert (progress['completed'], progress['total']) == ((0, 0) if error_type is None else (1, 1))
    cleanup = read_job(engine, 'cleanup-temporary-failure.json')
    assert (cleanup['stage'], cleanup['target'], cleanup['errno']) == (expected, 'temporary', errno.EACCES)


def test_debug_failure_is_cleared_after_successful_book_prepare(tmp_path):
    engine = StudioEngine(tmp_path / 'synthetic-book')
    source = engine.job / 'book.txt'
    source.write_text('第一章 开端\n\n这是一段用于书听单测的文字。', encoding='utf-8')
    debug = engine.job / 'debug-failure.json'
    debug.write_text('{"stage":"book"}', encoding='utf-8')
    config = {'source': {'kind': 'text', 'value': str(source)}, 'content_type': 'book',
              'expected_speakers': 1}

    assert engine.run(config, 'prepare') is None
    assert read_job(engine, 'progress.json')['stage'] == 'complete'
    assert not debug.exists()


@pytest.mark.parametrize('error_type', [RuntimeError, Cancelled, asyncio.CancelledError, KeyboardInterrupt])
def test_incomplete_run_keeps_or_updates_debug_failure(monkeypatch, tmp_path, error_type):
    engine, config = setup_job(tmp_path, ['片段甲'])
    debug = engine.job / 'debug-failure.json'
    prior = b'{"stage":"synthesis","segment_id":"seg-000001"}'
    debug.write_bytes(prior)

    def stop_render(*args, **kwargs):
        assert debug.read_bytes() == prior
        engine.progress('export')
        raise error_type()

    monkeypatch.setattr(engine, 'render', stop_render)
    with pytest.raises(error_type):
        engine.run(config, 'render', preview=1)

    progress = read_job(engine, 'progress.json')
    if error_type is RuntimeError:
        assert progress['stage'] == 'failed'
        assert read_job(engine, 'debug-failure.json')['stage'] == 'export'
    else:
        assert progress['stage'] == ('export' if error_type is KeyboardInterrupt else 'cancelled')
        assert debug.read_bytes() == prior


@requires_ffmpeg
@pytest.mark.parametrize('prior_failure', [False, True])
def test_publish_failure_keeps_complete_export_and_only_records_publish_status(
    monkeypatch, tmp_path, capsys, prior_failure,
):
    engine, config = setup_job(tmp_path, ['片段甲'])
    use_edge(monkeypatch, [None])
    error = RuntimeError(FAKE_CONTEXT + 'A' * 20000)
    debug = engine.job / 'debug-failure.json'
    prior = b'{"stage":"synthesis","segment_id":"seg-000001"}'
    if prior_failure:
        debug.write_bytes(prior)
    seen_debug = []
    record_failure = Mock(wraps=engine.record_failure)
    monkeypatch.setattr(engine, 'record_failure', record_failure)
    monkeypatch.setattr('studio_publish_icloud.ensure_publishable_title', lambda job: None)

    def fail_publish(job, library_root=None):
        assert (job / 'output.mp3').is_file()
        seen_debug.append(debug.read_bytes() if debug.exists() else None)
        raise error

    monkeypatch.setattr('studio_publish_icloud.publish_job', fail_publish)
    output = engine.run(config, 'render')
    assert output == engine.job / 'output.mp3' and engine.valid_audio(output)
    assert seen_debug == [prior if prior_failure else None]
    progress = read_job(engine, 'progress.json')
    assert progress['stage'] == 'complete' and 'segment_id' not in progress
    status = read_job(engine, 'private-listen.json')
    assert status['ok'] is False and 0 < len(status['error']) <= 500
    assert_safe(json.dumps(status))
    assert_safe(capsys.readouterr().out)
    record_failure.assert_not_called()
    assert not debug.exists()


def test_engine_drains_all_16k_when_exit_is_already_known(monkeypatch, tmp_path):
    # Model the selector as ready while unread bytes or EOF remain.
    payload = b'x' * (16 * 1024)
    path = tmp_path / 'synthetic-engine-output'
    path.write_bytes(payload)
    timeouts = []
    chunks = []

    class ReadySelector:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def register(self, *_):
            pass

        def select(self, timeout):
            timeouts.append(timeout)
            return [True]

    def read_chunk(fd, size):
        assert 0 < size <= 4096
        chunk = os.read(fd, size)
        chunks.append(len(chunk))
        return chunk

    monkeypatch.setattr(studio_server.selectors, 'DefaultSelector', ReadySelector)
    monkeypatch.setattr(studio_server, 'os', SimpleNamespace(read=read_chunk))
    with path.open('rb') as output:
        process = SimpleNamespace(stdout=output, poll=lambda: 1, wait=lambda: 1)
        assert studio_server.wait_for_engine(process) == (1, True)
    assert sum(chunks) == len(payload) and chunks[-1] == 0
    assert timeouts and all(timeout == 0 for timeout in timeouts)


@pytest.mark.parametrize('late_output', [False, True])
def test_engine_drains_exit_flush_without_waiting_for_inherited_fd(monkeypatch, late_output):
    read_fd, inherited_fd = os.pipe()
    make_selector = studio_server.selectors.DefaultSelector
    finished = not late_output
    timeouts = []

    class ExitSelector:
        def __enter__(self):
            self.selector = make_selector()
            return self

        def __exit__(self, *_):
            self.selector.close()

        def register(self, *args):
            self.selector.register(*args)

        def select(self, timeout):
            nonlocal finished
            timeouts.append(timeout)
            if not finished:
                # Exit and flush just after a select reported no readiness.
                os.write(inherited_fd, b'synthetic exit flush')
                finished = True
                return []
            return self.selector.select(timeout=0)

    monkeypatch.setattr(studio_server.selectors, 'DefaultSelector', ExitSelector)
    try:
        with os.fdopen(read_fd, 'rb') as output:
            process = SimpleNamespace(
                stdout=output, poll=lambda: 1 if finished else None, wait=lambda: 1,
            )
            assert studio_server.wait_for_engine(process) == (1, late_output)
        assert timeouts == ([.2, 0, 0] if late_output else [0])
    finally:
        # Leave the inherited writer open until after the worker has returned.
        os.close(inherited_fd)


def test_worker_drains_noisy_child_into_small_safe_job_log(tmp_path):
    script = tmp_path / 'fake-engine.py'
    script.write_text(
        'import sys\n'
        f'payload = ({FAKE_CONTEXT!r} + "X"*4000 + "\\n").encode()\n'
        'for stream in (sys.stdout.buffer, sys.stderr.buffer):\n'
        '    for _ in range(300): stream.write(payload)\n'
        'sys.exit(1)\n',
        encoding='utf-8',
    )
    studio = studio_server.Studio(tmp_path / 'jobs', engine=[sys.executable, str(script)])
    job = studio.root / 'synthetic'
    job.mkdir()
    studio_server.write(job / 'state.json', {'status': 'queued', 'action': 'prepare'})
    thread = threading.Thread(target=studio.worker, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while studio_server.read(job / 'state.json')['status'] != 'failed' and time.monotonic() < deadline:
            time.sleep(.02)
        assert studio_server.read(job / 'state.json')['status'] == 'failed'
    finally:
        studio.stop = True
        thread.join(6)
    assert not thread.is_alive()
    log = job / 'engine-error.log'
    assert 0 < log.stat().st_size <= 1024
    record = json.loads(log.read_text())
    assert record['exit_code'] == 1 and record['output_observed'] is True
    assert_safe(log.read_text())
    assert not (job / 'temporary').exists()
