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
  echo "运行: cd \"$PROJECT_ROOT\" && python3 -m venv .venv && .venv/bin/pip install -r requirements-podcast.txt"
  exit 1
fi

TTS_BACKEND="edge"
EXPECT_TTS=0
SKIP_TTS_KEY=0
for ARG in "$@"; do
  if [ "$EXPECT_TTS" -eq 1 ]; then
    TTS_BACKEND="$ARG"
    EXPECT_TTS=0
    continue
  fi
  case "$ARG" in
    --tts)
      EXPECT_TTS=1
      ;;
    --tts=*)
      TTS_BACKEND="${ARG#--tts=}"
      ;;
    --dump-segments|-h|--help)
      SKIP_TTS_KEY=1
      ;;
  esac
done

if [ "$TTS_BACKEND" = "qwen3-api" ] \
  && [ "$SKIP_TTS_KEY" -eq 0 ] \
  && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
  KEYCHAIN_ACCOUNT="${USER:-$(id -un)}"
  KEYCHAIN_VALUE="$(
    security find-generic-password \
      -a "$KEYCHAIN_ACCOUNT" \
      -s "podcast-cn-qwen" \
      -w 2>/dev/null || true
  )"
  if [ -n "$KEYCHAIN_VALUE" ]; then
    export DASHSCOPE_API_KEY="$KEYCHAIN_VALUE"
  fi
  unset KEYCHAIN_VALUE
fi

if [ "$TTS_BACKEND" = "qwen3-api" ] \
  && [ "$SKIP_TTS_KEY" -eq 0 ] \
  && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
  echo "错误:还没有配置 DASHSCOPE_API_KEY。" >&2
  echo "首次使用请运行: bash $PROJECT_ROOT/bin/setup-podcast-model.sh qwen" >&2
  exit 2
fi

exec "$VENV_PY" "$PROJECT_ROOT/scripts/tts-cn.py" "$@"
