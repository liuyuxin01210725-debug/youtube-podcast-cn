import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_worker as worker
from studio_engine import StudioEngine, StudioError

SEGMENTS = [{'start': 0.1, 'end': 0.9, 'text': 'offline test'}]


@pytest.mark.parametrize('mlx_error, cpu_error, backend', [
    (None, None, 'mlx'),
    (ImportError('synthetic'), None, 'cpu'),
    (RuntimeError('Metal unavailable'), None, 'cpu'),
    (ImportError('synthetic'), FileNotFoundError('synthetic'), None),
])
def test_asr_backends_and_durable_job_diagnostic(tmp_path, monkeypatch, mlx_error, cpu_error, backend):
    engine = StudioEngine(tmp_path / 'job')
    engine.temp.mkdir()
    output = engine.temp / 'asr.json'
    mlx = Mock(return_value={'segments': SEGMENTS}, side_effect=mlx_error)
    cpu = Mock(return_value={'segments': SEGMENTS}, side_effect=cpu_error)
    real_is_file = Path.is_file

    def without_speech_venv(path):
        return False if path.parts[-3:] == ('.speech-venv', 'bin', 'python') else real_is_file(path)

    monkeypatch.setattr(Path, 'is_file', without_speech_venv)

    def command(args):
        assert args[0] == sys.executable
        assert Path(args[-2]).resolve().parent == Path(worker.__file__).resolve().parent
        assert '-E' in args
        try:
            worker.run_asr(json.loads(Path(args[-1]).read_bytes()), mlx=mlx, cpu=cpu)
        except RuntimeError:
            raise StudioError('process_failed', 'synthetic worker exit') from None

    monkeypatch.setattr(engine, 'command', command)
    request = {'operation': 'asr', 'output': str(output), 'start_ms': 0}
    if backend:
        engine.worker(request)
        assert json.loads(output.read_bytes()) == {'segments': SEGMENTS}
    else:
        with pytest.raises(StudioError, match='本地语音识别失败') as error:
            engine.worker(request)
        assert error.value.code == 'asr_failed'
        assert not output.exists()
    mlx.assert_called_once()
    assert cpu.call_count == (0 if backend == 'mlx' else 1)
    shutil.rmtree(engine.temp)
    report = json.loads((engine.job / 'debug-asr.json').read_bytes())
    assert report['backend'] == backend
    expected = [{'backend': 'mlx', 'status': 'ok'}]
    if mlx_error:
        expected = [{'backend': 'mlx', 'status': 'failed', 'error_type': type(mlx_error).__name__},
                    {'backend': 'cpu', 'status': 'ok'}]
    if cpu_error:
        expected[-1].update(status='failed', error_type=type(cpu_error).__name__)
    assert report['chunks'] == [{'start_ms': 0, 'backend': backend, 'attempts': expected}]


def test_official_backends_use_only_cached_models(monkeypatch, tmp_path):
    transcribe = Mock(return_value={'segments': SEGMENTS})
    load = Mock(return_value=SimpleNamespace(transcribe=transcribe))
    monkeypatch.setitem(sys.modules, 'whisper', SimpleNamespace(load_model=load))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    with pytest.raises(FileNotFoundError):
        worker.cpu_asr({'audio': 'offline.wav'})
    load.assert_not_called()
    checkpoint = tmp_path / 'whisper' / 'small.en.pt'
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b'placeholder for test double')
    assert worker.cpu_asr({'audio': 'offline.wav'}) == {'segments': SEGMENTS}
    load.assert_called_once_with(str(checkpoint), device='cpu')
    transcribe.assert_called_once_with('offline.wav', language='en', word_timestamps=True, fp16=False)

    download = Mock(return_value='/fixture/mlx')
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(snapshot_download=download))
    model = 'mlx-community/whisper-small.en-mlx'
    assert worker.local_snapshot(model) == '/fixture/mlx'
    download.assert_called_once_with(model, revision=worker.REVISIONS[model], local_files_only=True)



class _Float64(float):
    """Stand-in for numpy.float64: subclass of float, but type() is not float."""


def test_numpy_like_mlx_timestamps_are_accepted_and_serialized(tmp_path):
    diagnostic = tmp_path / 'asr-diagnostic.json'
    output = tmp_path / 'asr.json'
    request = {
        'audio': 'offline.wav',
        'output': str(output),
        'diagnostic': str(diagnostic),
        'model': 'mlx-community/whisper-small.en-mlx',
    }

    def mlx(_request):
        return {'segments': [{'start': _Float64(0.0), 'end': _Float64(3.92), 'text': 'hello'}]}

    def cpu(_request):
        raise AssertionError('CPU must not run when MLX segments only need numeric coercion')

    worker.run_asr(request, mlx=mlx, cpu=cpu)
    payload = json.loads(output.read_text(encoding='utf-8'))
    assert payload == {'segments': [{'start': 0.0, 'end': 3.92, 'text': 'hello'}]}
    assert all(type(row[key]) is float for row in payload['segments'] for key in ('start', 'end'))
    report = json.loads(diagnostic.read_text(encoding='utf-8'))
    assert report == {'backend': 'mlx', 'attempts': [{'backend': 'mlx', 'status': 'ok'}]}


def test_invalid_segments_fall_through_to_cpu(tmp_path):
    diagnostic = tmp_path / 'asr-diagnostic.json'
    output = tmp_path / 'asr.json'
    request = {
        'audio': 'offline.wav',
        'output': str(output),
        'diagnostic': str(diagnostic),
        'model': 'mlx-community/whisper-small.en-mlx',
    }

    def mlx(_request):
        return {'segments': [{'start': '0.1', 'end': '0.9', 'text': 'bad'}]}

    def cpu(_request):
        return {'segments': SEGMENTS}

    worker.run_asr(request, mlx=mlx, cpu=cpu)
    assert json.loads(output.read_text(encoding='utf-8')) == {'segments': SEGMENTS}
    report = json.loads(diagnostic.read_text(encoding='utf-8'))
    assert report['backend'] == 'cpu'
    assert report['attempts'][0] == {'backend': 'mlx', 'status': 'failed', 'error_type': 'InvalidSegments'}
    assert report['attempts'][1] == {'backend': 'cpu', 'status': 'ok'}
