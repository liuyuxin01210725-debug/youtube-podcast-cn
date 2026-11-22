#!/usr/bin/env python3
"""Isolated speech worker. One invocation owns one model and releases it on exit."""
import argparse
import json
import math
import os
from numbers import Real
from pathlib import Path


REVISIONS = {
    'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit': '50f45ef0047cde7e84c2ef04326acb8ada2436a7',
    'mlx-community/whisper-small.en-mlx': '52a88bf6e98b114a210c21bb83e22d6e1505cb73',
}


def local_snapshot(model):
    from huggingface_hub import snapshot_download

    return snapshot_download(model, revision=REVISIONS.get(model), local_files_only=True)


def mlx_asr(request):
    import mlx.core as mx
    import mlx_whisper

    if not mx.metal.is_available():
        raise RuntimeError('Metal unavailable')
    return mlx_whisper.transcribe(
        request['audio'], path_or_hf_repo=local_snapshot(request['model']),
        language='en', word_timestamps=True,
    )


def cpu_asr(request):
    import whisper

    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    model = cache / 'whisper' / 'small.en.pt'
    if not model.is_file():
        raise FileNotFoundError('cached OpenAI Whisper small.en checkpoint missing')
    # Passing a file path (not the model name) keeps load_model offline.
    return whisper.load_model(str(model.resolve(strict=True)), device='cpu').transcribe(
        request['audio'], language='en', word_timestamps=True, fp16=False,
    )


def _finite_seconds(value):
    """Accept built-in and NumPy timestamps; reject bool and non-finite values."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def sanitize_asr_segments(segments):
    if not isinstance(segments, list):
        return None
    cleaned = []
    for row in segments:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str):
            return None
        start = _finite_seconds(row.get('start'))
        end = _finite_seconds(row.get('end'))
        if start is None or end is None or not 0 <= start <= end:
            return None
        item = {'start': start, 'end': end, 'text': row['text']}
        if type(row.get('id')) is int:
            item['id'] = row['id']
        cleaned.append(item)
    return cleaned


def run_asr(request, *, mlx=mlx_asr, cpu=cpu_asr):
    diagnostic = {'backend': None, 'attempts': []}
    path = Path(request['diagnostic'])

    def save_diagnostic():
        staged = path.with_suffix('.tmp')
        staged.write_text(json.dumps(diagnostic))
        staged.replace(path)

    for name, transcribe in (('mlx', mlx), ('cpu', cpu)):
        attempt = {'backend': name, 'status': 'running'}
        diagnostic['attempts'].append(attempt)
        save_diagnostic()
        try:
            segments = transcribe(request)['segments']
        except Exception as error:
            attempt.update(status='failed', error_type=type(error).__name__)
            save_diagnostic()
            continue
        # An invalid backend result is a hard error, not a successful ASR response.
        cleaned = sanitize_asr_segments(segments)
        if cleaned is None:
            attempt.update(status='failed', error_type='InvalidSegments')
            save_diagnostic()
            continue
        output = Path(request['output'])
        staged = output.with_suffix('.tmp')
        staged.write_text(json.dumps({'segments': cleaned}, ensure_ascii=False, allow_nan=False))
        staged.replace(output)
        attempt['status'] = 'ok'
        diagnostic['backend'] = name
        save_diagnostic()
        return
    raise RuntimeError('MLX 和 CPU ASR 均不可用')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request')
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text())
    output = Path(request['output'])
    if request['operation'] == 'asr':
        run_asr(request)
    else:
        model_path = local_snapshot(request['model'])
        import numpy as np
        from mlx_audio.audio_io import write
        from mlx_audio.tts.utils import load_model
        model = load_model(model_path)
        # One bounded utterance per request; no all-episode waveform allocation.
        cap = min(4096, max(512, len(request['text']) * 32))
        results = model.generate(text=request['text'], ref_audio=request['reference'], ref_text=request['reference_text'], lang_code='Chinese', max_tokens=cap, stream=False)
        parts = []
        rate = None
        for result in results:
            rate = result.sample_rate
            parts.append(np.asarray(result.audio))
            if getattr(result, 'audio_samples', 0) == 0 and parts[-1].size == 0:
                raise RuntimeError('语音模型输出为空')
            # A generation reaching its token budget may have lost its ending.
            tokens = getattr(result, 'token_count', None)
            if tokens is not None and tokens >= cap:
                raise RuntimeError('语音达到长度上限，请缩短该段后重试')
        if not parts or not rate:
            raise RuntimeError('语音模型没有输出')
        write(str(output), np.concatenate(parts), rate)


if __name__ == '__main__':
    main()
