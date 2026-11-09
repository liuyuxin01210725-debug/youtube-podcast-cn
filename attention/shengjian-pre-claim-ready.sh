#!/usr/bin/env bash
# 声间装机 / 宣称可听前的源码与二进制探针。
# 单测由 make shengjian-before-claim-ready 先跑（ContinueUXProbe + ExtractDisplayChecks）。
# 这里锁会反复回潮的字符串：继续播放、听后感、mock 不得冒充 ready。
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

fail() {
  echo "FAIL $*" >&2
  exit 1
}

python3 - "$root" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
app = root / "ios/PrivateListen/PrivateListen"

def fail(msg):
    print(f"FAIL {msg}", file=sys.stderr)
    sys.exit(1)

def read(name):
    path = app / name
    if not path.is_file():
        fail(f"缺少 {path.relative_to(root)}（播放页/资料库源码不在 App 目录里）")
    return path.read_text()

continue_src = read("ContinueListening.swift")
if "static let displayLimit = 1" not in continue_src:
    fail("继续播放必须只留 1 张卡")
if "static let minimumPlayedSeconds = 0.0" not in continue_src:
    fail("短播门槛 minimumPlayedSeconds 丢了")
if "mark.seconds > minimumPlayedSeconds" not in continue_src:
    fail("继续播放资格没有走短播门槛")
if "mark.seconds >= 5" in continue_src:
    fail("ContinueListening 又把 >= 5 秒写回继续播放资格")
if "func acceptsProgressSample" not in continue_src or "clipSuppressListenMark" not in continue_src:
    fail("听这段闸不在 ContinueListening")
if "playheadToPersist" not in continue_src or "afterLeavingPlayer" not in continue_src:
    fail("短播回库旅程没有留在 ContinueListening")

player = read("PlayerView.swift")
note = player.find("ListenNoteSection(")
extract = player.find("ExtractDetailSection(")
if note < 0 or extract < 0 or not note < extract:
    fail("播放页听后感必须在提炼区块之前")
appear = player.split(".onAppear", 1)[1].split(".onDisappear", 1)[0]
if "acknowledgeContinue" not in appear:
    fail("打开播放页要 acknowledgeContinue（取消左滑隐藏，不是把刚打开的集藏掉）")
if "dismissContinue" in appear:
    fail("打开播放页不能 dismissContinue")
if "ListenNoteSection(" not in player or "ExtractDetailSection(" not in player:
    fail("播放页缺少听后感或提炼区块")

model = read("PlayerModel.swift")
if "playheadToPersist" not in model:
    fail("离开播放器没有把真实播放头写入 ListenMark")
if "guard !clipSuppressListenMark else { return }" not in model:
    fail("听这段仍必须拦住 checkpoint")

store = read("LibraryStore.swift")
if "entry.seconds >= 5" not in store:
    fail("在听芯片的 5 秒门槛被挪走了")
if "continueTouched[id]" not in store:
    fail("ListenMark 写入没有更新 continueTouched")

section = read("ContinueListeningSection.swift")
if "继续播放" not in section:
    fail("继续播放标题丢了")

note_src = read("ListenNoteSection.swift")
if "听后感" not in note_src:
    fail("听后感文案丢了")
read("ListenNoteStore.swift")

mock = read("MockExtractor.swift")
if "enum ExtractorGate" not in mock or "struct MockExtractor" not in mock:
    fail("ExtractorGate / MockExtractor 结构变了，探针对不上")
import re
gate = mock.split("enum ExtractorGate", 1)[1].split("struct MockExtractor", 1)[0]
gate_code = re.sub(r"///[^\n]*", "", gate)
gate_code = re.sub(r"//[^\n]*", "", gate_code)
if "case mock" in gate_code or "MockExtractor" in gate_code:
    fail("ExtractorGate 不能把缺 key 转到 Mock")
if "missingApiKey" not in gate_code:
    fail("ExtractorGate 缺 missingApiKey")

display = read("ExtractDisplay.swift")
for token in ("mock_not_official", "readyDeepForDisk", "isPlaceholderDeep"):
    if token not in display:
        fail(f"ExtractDisplay 缺 {token}")
chunk = display.split("static func readyDeepForDisk", 1)[1].split("static func", 1)[0]
if "ExtractorGate.route" not in chunk or "return nil" not in chunk:
    fail("无 Live key 时 readyDeepForDisk 必须返回 nil")
if "isPlaceholderDeep(packet)" not in chunk:
    fail("占位 mock 不能通过 readyDeepForDisk")

print("ok  源码探针：继续播放短播 / 听后感在提炼前 / mock 不冒充 ready")
PY

scan_binary() {
  local bin="$1"
  # macOS `strings` often drops UTF-8 Chinese; search raw bytes instead.
  python3 -c '
import pathlib, sys
bin_path = pathlib.Path(sys.argv[1])
blob = bin_path.read_bytes()
for token in ("继续播放", "听后感", "mock_not_official"):
    if token.encode("utf-8") not in blob:
        print(f"FAIL 二进制缺少「{token}」：{bin_path}", file=sys.stderr)
        raise SystemExit(1)
print(f"ok  二进制含继续播放、听后感、mock_not_official：{bin_path}")
' "$bin" || fail "二进制探针失败：$bin"
}

shopt -s nullglob
found=0
# Only CURRENT tip + DerivedData — historic dist/*.app would false-fail on old builds.
search_roots=()
for cand in   "$root/dist/CURRENT-iphoneos"   "$root/dist/PrivateListen-CURRENT-iphoneos.app"   "$root/dist/CURRENT-iphoneos.app"; do
  if [[ -e "$cand" ]]; then
    search_roots+=("$cand")
  fi
done

for root_cand in "${search_roots[@]}"; do
  while IFS= read -r -d '' app; do
    # Debug tip builds put Chinese strings in PrivateListen.debug.dylib (thin stub is ~90KB).
    dylib="$app/PrivateListen.debug.dylib"
    exe="$app/PrivateListen"
    if [[ -f "$dylib" ]]; then
      scan_binary "$dylib"
      found=1
    elif [[ -f "$exe" ]]; then
      scan_binary "$exe"
      found=1
    fi
  done < <(find "$root_cand" -type d -name 'PrivateListen.app' -print0 2>/dev/null || true)
  # CURRENT may itself be the .app bundle
  if [[ -d "$root_cand" && -f "$root_cand/Info.plist" ]]; then
    dylib="$root_cand/PrivateListen.debug.dylib"
    exe="$root_cand/PrivateListen"
    if [[ -f "$dylib" ]]; then
      scan_binary "$dylib"
      found=1
    elif [[ -f "$exe" ]]; then
      scan_binary "$exe"
      found=1
    fi
  fi
done

while IFS= read -r -d '' bin; do
  case "$bin" in
    *swift-toolchain*|*usr/lib/swift*|*DerivedData/*/SourcePackages/*) continue ;;
  esac
  scan_binary "$bin"
  found=1
done < <(find "$root/ios" -type f -name '*.dylib' -print0 2>/dev/null || true)

if [[ "$found" -eq 0 ]]; then
  echo "ok  未发现已编译 App/dylib。没有手机 UI 自动化时，源码探针 + 继续播放/提炼单测就是装机前检查。"
fi
