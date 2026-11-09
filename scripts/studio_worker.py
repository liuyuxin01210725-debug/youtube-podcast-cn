#!/usr/bin/env python3
"""Isolated MLX worker. One invocation owns one model and releases it on exit."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request')
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text())
    output = Path(request['output'])
    from huggingface_hub import snapshot_download
    revisions = {
        'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit': '50f45ef0047cde7e84c2ef04326acb8ada2436a7',
        'mlx-community/whisper-small.en-mlx': '52a88bf6e98b114a210c21bb83e22d6e1505cb73',
    }
    model_path = snapshot_download(request['model'], revision=revisions.get(request['model']), local_files_only=True)
    if request['operation'] == 'asr':
        import mlx_whisper
        result = mlx_whisper.transcribe(request['audio'], path_or_hf_repo=model_path, language='en', word_timestamps=True)
        output.write_text(json.dumps({'segments': result['segments']}, ensure_ascii=False))
    else:
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
