#!/usr/bin/env bash
# Run only on a disposable simulator created for this test, never on a phone/user library.
set -euo pipefail
device=${1:?usage: probe_point_listen_sim.sh SIMULATOR_UDID BUILT_APP [REPORT_JSON]}
app=${2:?built simulator .app required}
report=${3:-/tmp/shengjian-point-listen-sim-report.json}
root=$(cd "$(dirname "$0")/.." && pwd)
bundle=com.liuyuxin.privatelisten

xcrun simctl list devices --json | python3 -c '
import json, sys
device = next((d for ds in json.load(sys.stdin)["devices"].values() for d in ds if d["udid"] == sys.argv[1]), None)
if not device or not device["name"].startswith("Shengjian Point Listen Isolated") or device["state"] != "Booted":
    raise SystemExit("Require a booted disposable simulator named Shengjian Point Listen Isolated*")
' "$device"
xcrun simctl install "$device" "$app"
container=$(xcrun simctl get_app_container "$device" "$bundle" data)
destination="$container/Documents/中文播客私人库"
# Never overwrite a pre-existing unrelated library, even inside a named test simulator.
if [[ -e "$destination" ]]; then
    python3 - "$destination/library.json" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))['episodes']
assert len(rows) == 1 and rows[0]['id'] == 'point-listen-isolated', 'Refuse to replace unrelated library'
PY
fi
mkdir -p "$destination"
cp -R "$root/fixtures/point-listen/." "$destination/"
probe="$container/Documents/point-listen-probe.json"
[[ ! -f "$probe" ]] || mv "$probe" "$probe.previous"
xcrun simctl terminate "$device" "$bundle" >/dev/null 2>&1 || true
xcrun simctl launch "$device" "$bundle" -PointListenProbe
for ((i=0; i<60; i++)); do
    if [[ -f "$probe" ]]; then
        cp "$probe" "$report"
        python3 - "$report" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
for c in r['checks']:
    print(('ok  ' if c['pass'] else 'FAIL ') + c['name'])
assert r['done'] and r['pass'], 'runtime probe failed'
print('point listen simulator probe passed; report: ' + sys.argv[1])
PY
        exit 0
    fi
    sleep 1
done
echo "FAIL probe timeout; no report at $probe" >&2
exit 1
