#!/bin/bash
set -e
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ] || [ ! -x .speech-venv/bin/python ]; then
  echo '首次使用需要安装运行环境，正在打开本地安装程序…'
  bash bin/setup-studio.sh
fi
if ! command -v ffmpeg >/dev/null; then echo '缺少 ffmpeg，请先运行 brew install ffmpeg'; read -r -p '按回车关闭'; exit 1; fi
if curl --silent --fail http://127.0.0.1:8765/api/session >/dev/null; then
  open http://127.0.0.1:8765
  exit 0
fi
(sleep 1; open http://127.0.0.1:8765) &
echo '工作台运行中。关闭此窗口会停止服务；未完成任务下次可以继续。'
exec .venv/bin/python scripts/studio_server.py
