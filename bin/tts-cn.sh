#!/usr/bin/env bash
# 中文 transcript → mp3 (免费 EdgeTTS)
# 用法:
#   bash bin/tts-cn.sh <中文文件> [输出 mp3]
#   bash bin/tts-cn.sh raw/podcast/2026-05-18-attia-zone2.md

set -e

if [ -z "$1" ]; then
  echo "用法: bash $0 <中文 markdown/txt> [输出 mp3]"
  echo ""
  echo "输入格式说明: 见 scripts/tts-cn.py 文件头"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PY="$PROJECT_ROOT/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "❌ venv 不存在: $VENV_PY"
  echo "运行: python3 -m venv .venv && .venv/bin/pip install edge-tts pydub audioop-lts"
  exit 1
fi

exec "$VENV_PY" "$PROJECT_ROOT/scripts/tts-cn.py" "$@"
