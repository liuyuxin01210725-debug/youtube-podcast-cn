#!/usr/bin/env bash
set -euo pipefail

PROVIDER="${1:-}"
ACCOUNT="${USER:-$(id -un)}"

case "$PROVIDER" in
  deepseek)
    SERVICE="podcast-cn-deepseek"
    DISPLAY_NAME="DeepSeek"
    ;;
  qwen)
    SERVICE="podcast-cn-qwen"
    DISPLAY_NAME="通义千问"
    ;;
  *)
    echo "用法: bash $0 <deepseek|qwen>" >&2
    exit 2
    ;;
esac

if ! command -v security >/dev/null 2>&1; then
  echo "错误:这个安全保存方式只支持 macOS 钥匙串。" >&2
  exit 1
fi

printf "请输入 %s API Key（输入内容不会显示）: " "$DISPLAY_NAME" >&2
IFS= read -r -s API_KEY
printf "\n" >&2

if [ "${#API_KEY}" -lt 10 ]; then
  unset API_KEY
  echo "错误:Key 为空或长度异常,没有保存。" >&2
  exit 1
fi

security add-generic-password \
  -U \
  -a "$ACCOUNT" \
  -s "$SERVICE" \
  -w "$API_KEY" >/dev/null
unset API_KEY

echo "已保存到 macOS 钥匙串。以后运行 podcast-cn.sh 会自动读取。"
