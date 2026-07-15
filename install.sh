#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PODCAST_TRANSLATE_ROOT="$ROOT"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "错误:当前安装器只支持 macOS。" >&2
  exit 1
fi

for COMMAND in python3 ffmpeg security; do
  if ! command -v "$COMMAND" >/dev/null 2>&1; then
    if [ "$COMMAND" = "ffmpeg" ]; then
      echo "错误:找不到 ffmpeg。安装 Homebrew 后运行: brew install ffmpeg" >&2
    else
      echo "错误:找不到 $COMMAND。" >&2
    fi
    exit 1
  fi
done

python3 -m venv "$ROOT/.venv"
PYTHON="$ROOT/.venv/bin/python"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r "$ROOT/requirements-podcast.txt"

"$PYTHON" - "$PODCAST_TRANSLATE_ROOT" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
source = root / "skills/podcast-translate/SKILL.md"
template = source.read_text(encoding="utf-8")
placeholder = "__PODCAST_TRANSLATE_ROOT__"
if placeholder not in template:
    raise SystemExit("Skill 模板缺少安装路径占位符")

for platform in (".codex", ".claude"):
    target = Path.home() / platform / "skills/podcast-translate"
    marker = target / ".youtube-podcast-cn-source"
    if target.exists() and any(target.iterdir()):
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != str(root):
            raise SystemExit(
                f"不会覆盖已有 Skill: {target}\n"
                "请先备份或移走该目录，再重新运行安装器。"
            )
    target.mkdir(parents=True, exist_ok=True)
    installed = template.replace(placeholder, str(root))
    (target / "SKILL.md").write_text(installed, encoding="utf-8")
    marker.write_text(str(root) + "\n", encoding="utf-8")
    issues = target / "ISSUES.md"
    if not issues.exists():
        issues.write_text("# podcast-translate 问题日志\n\n## 未修复\n\n## 已修复\n", encoding="utf-8")
    print(f"已安装 Skill: {target}")
PY

mkdir -p "$HOME/Desktop/音频"

echo ""
echo "安装完成。下一步任选一个模型配置 Key:"
echo "  bash $ROOT/bin/setup-podcast-model.sh deepseek"
echo "  bash $ROOT/bin/setup-podcast-model.sh qwen"
echo "说话人识别会在首次 YouTube 处理时懒安装，正常安装不下载声纹模型。"
echo "如需提前安装或修复: bash $ROOT/bin/setup-podcast-diarization.sh"
echo "然后重新打开 Codex 或 Claude Code。"
