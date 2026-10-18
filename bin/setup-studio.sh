#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -m)" != arm64 ]; then echo '本地模型需要 Apple Silicon Mac。'; exit 1; fi
command -v ffmpeg >/dev/null || { echo '请先安装 ffmpeg：brew install ffmpeg'; exit 1; }
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; .venv/bin/pip install -r requirements-podcast.txt; fi
bash bin/setup-podcast-diarization.sh
if [ ! -x .speech-venv/bin/python ]; then
  command -v python3.12 >/dev/null || { echo '请先安装 Python 3.12：brew install python@3.12'; exit 1; }
  python3.12 -m venv .speech-venv
  .speech-venv/bin/pip install -r requirements-speech.txt
fi
echo '环境安装完成。首次使用前需下载固定模型（参见 README 工作台章节），并配置翻译服务。'
.speech-venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
models = {
 'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit': '50f45ef0047cde7e84c2ef04326acb8ada2436a7',
 'mlx-community/whisper-small.en-mlx': '52a88bf6e98b114a210c21bb83e22d6e1505cb73',
}
for model, revision in models.items():
    snapshot_download(model, revision=revision)
print('固定版本语音模型已准备好。')
PY
