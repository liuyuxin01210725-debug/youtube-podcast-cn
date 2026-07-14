#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
KEYCHAIN_ACCOUNT="${USER:-$(id -un)}"
export PATH="$ROOT/.venv/bin:$PATH"
PROVIDER="deepseek"
EXPECT_PROVIDER=0
SHOW_HELP=0

for ARG in "$@"; do
  if [ "$EXPECT_PROVIDER" -eq 1 ]; then
    PROVIDER="$ARG"
    EXPECT_PROVIDER=0
    continue
  fi
  case "$ARG" in
    --provider)
      EXPECT_PROVIDER=1
      ;;
    --provider=*)
      PROVIDER="${ARG#--provider=}"
      ;;
    -h|--help)
      SHOW_HELP=1
      ;;
  esac
done

if [ "$EXPECT_PROVIDER" -eq 1 ]; then
  PROVIDER=""
fi

if [ ! -x "$PYTHON" ]; then
  echo "错误:找不到 $PYTHON,请先安装 requirements-podcast.txt" >&2
  exit 1
fi

case "$PROVIDER" in
  deepseek)
    KEYCHAIN_SERVICE="podcast-cn-deepseek"
    KEY_ENV_NAME="DEEPSEEK_API_KEY"
    KEY_VALUE="${DEEPSEEK_API_KEY:-}"
    ;;
  qwen)
    KEYCHAIN_SERVICE="podcast-cn-qwen"
    KEY_ENV_NAME="DASHSCOPE_API_KEY"
    KEY_VALUE="${DASHSCOPE_API_KEY:-}"
    ;;
  *)
    KEYCHAIN_SERVICE=""
    KEY_ENV_NAME=""
    KEY_VALUE=""
    ;;
esac

if [ -n "$KEYCHAIN_SERVICE" ] \
    && [ -z "$KEY_VALUE" ] \
    && command -v security >/dev/null 2>&1; then
  KEYCHAIN_VALUE="$(
    security find-generic-password \
      -a "$KEYCHAIN_ACCOUNT" \
      -s "$KEYCHAIN_SERVICE" \
      -w 2>/dev/null || true
  )"
  if [ -n "$KEYCHAIN_VALUE" ]; then
    if [ "$PROVIDER" = "deepseek" ]; then
      export DEEPSEEK_API_KEY="$KEYCHAIN_VALUE"
    else
      export DASHSCOPE_API_KEY="$KEYCHAIN_VALUE"
    fi
    KEY_VALUE="$KEYCHAIN_VALUE"
  fi
  unset KEYCHAIN_VALUE
fi

if [ -n "$KEYCHAIN_SERVICE" ] \
    && [ -z "$KEY_VALUE" ] \
    && [ "$SHOW_HELP" -eq 0 ]; then
  echo "错误:还没有配置 $KEY_ENV_NAME。" >&2
  echo "首次使用请运行: bash $ROOT/bin/setup-podcast-model.sh $PROVIDER" >&2
  exit 2
fi

exec "$PYTHON" "$ROOT/scripts/podcast_cn.py" "$@"
