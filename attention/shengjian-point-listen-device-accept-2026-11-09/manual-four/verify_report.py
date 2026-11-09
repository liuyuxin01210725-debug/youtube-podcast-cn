#!/usr/bin/env python3
"""Verify raw physical-device receipts plus the permitted ListenNote source assertion.

Run from the repository root. Raw catalog/preferences snapshots stay local;
the output retains their hashes and only the Norton progress fact.
"""
import hashlib
import json
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def read(name):
    return json.loads((HERE / name).read_text())


def sha(data):
    return hashlib.sha256(data).hexdigest()


device = read("device-report.json")
manifest = read("build-manifest.json")
checks = []


def check(name, passed):
    checks.append({"name": name, "pass": bool(passed)})


udid = manifest["device_udid"]
check("physical_done_current_run", device["device"] == "physical" and device["done"]
      and device["run_id"] == manifest["run_id"] and device["device_checks_pass"])
check("device_checks_all_pass", bool(device["checks"]) and all(c["pass"] for c in device["checks"]))
for name in ["device-install.json", "device-launch.json", "copy-device-report.json",
             "copy-library-before.json", "copy-library-after.json", "normal-relaunch.json", "copy-preferences.json"]:
    receipt = read(name)
    check(name, receipt["info"]["outcome"] == "success" and udid in receipt["info"]["arguments"])
launch_args = read("device-launch.json")["info"]["arguments"]
check("explicit_device_probe_launch", "-NortonDeviceFourProbe" in launch_args and manifest["run_id"] in launch_args)
check("normal_relaunch_has_no_probe", "-NortonDeviceFourProbe" not in read("normal-relaunch.json")["info"]["arguments"])

source_hashes_match = all(sha((ROOT / p).read_bytes()) == digest for p, digest in manifest["source_sha256"].items())
check("installed_source_hashes_match", source_hashes_match)
app = ROOT / manifest["app_path"]
check("installed_app_hashes_match", all(sha((app / p).read_bytes()) == digest for p, digest in manifest["app_sha256"].items()))
check("signed_Debug_iphoneos_build", manifest["configuration"] == "Debug-iphoneos"
      and manifest["build_exit_code"] == manifest["codesign_verify_exit_code"] == 0)
check("installed_before_claim_gate", "声间：装机/宣称可听前检查通过" in (HERE / "installed-local-gate.log").read_text())

view_path = "ios/PrivateListen/PrivateListen/PlayerView.swift"
section_path = "ios/PrivateListen/PrivateListen/ListenNoteSection.swift"
view = (ROOT / view_path).read_text()
section = (ROOT / section_path).read_text()
call = "ListenNoteSection(episodeId: episode.id, isFinished: library.isFinished(episode.id))"
note_source_pass = (source_hashes_match and view.count(call) == 1
                    and call + "\n                ExtractDetailSection(episode: episode, extracts: extracts)" in view
                    and 'Text("听后感")' in section and 'TextEditor(text: $draftText)' in section
                    and '.accessibilityLabel("听后感")' in section)
check("P-ListenNote_unconditional_player_entry_and_accessible_editor", note_source_pass)

before = (HERE / "library-before.json").read_bytes()
after = (HERE / "library-after.json").read_bytes()
old = json.loads((HERE.parent / "device-file-verification.json").read_text())
before_ids = {e["id"] for e in json.loads(before)["episodes"]}
after_ids = {e["id"] for e in json.loads(after)["episodes"]}
merge_pass = before == after and sha(after) == old["library_json_sha256"] and len(after_ids) == 52 and before_ids == after_ids
check("P-Merge_52_retained_and_bytes_match_prior_verification", merge_pass and old["pass"])

prefs_bytes = (HERE / "preferences-after-relaunch.plist").read_bytes()
prefs = plistlib.loads(prefs_bytes)
marks = json.loads(prefs["PrivateListen.progress"])
touched = json.loads(prefs["PrivateListen.continueTouched"])
dismissed = prefs.get("PrivateListen.continueDismissed", [])
episode_id = device["episode_id"]
mark = marks[episode_id]
last = device["samples"][-1]
eligible = [key for key, value in marks.items() if key in after_ids and value["seconds"] > 0
            and not value["finished"] and key not in dismissed]
eligible.sort(key=lambda key: (touched.get(key, 0), marks[key]["seconds"], key), reverse=True)
persisted_pass = (not mark["finished"] and abs(mark["seconds"] - last["saved"]) < 0.75
                  and eligible[:1] == [episode_id])
check("P-Continue_after_real_process_restart", persisted_pass)
check("two_distinct_real_windows_crossed", len(device["samples"]) >= 2
      and len({s["id"] for s in device["samples"]}) >= 2
      and all(s["heard_clock"] - s["landed_clock"] >= 3 and s["heard_clock"] > s["t_end"]
              and abs(s["landing_delta_seconds"]) < 0.75 for s in device["samples"]))

groups = {name: "PASS" if device["groups"].get(name) else "FAIL" for name in ["P-Play", "P-Continue", "P-Merge"]}
groups["P-Continue"] = "PASS" if groups["P-Continue"] == "PASS" and persisted_pass else "FAIL"
groups["P-Merge"] = "PASS" if groups["P-Merge"] == "PASS" and merge_pass else "FAIL"
groups["P-ListenNote"] = "PASS" if note_source_pass and all(
    c["pass"] for c in device["checks"] if c["group"] == "P-ListenNote") else "FAIL"
passed = all(c["pass"] for c in checks) and all(value == "PASS" for value in groups.values())
report = {
    "schema": "shengjian.norton-device-four.acceptance.v1", "pass": passed,
    "run_id": device["run_id"], "app_source_commit": manifest["app_source_commit"],
    "device_udid": udid, "device_report": "device-report.json", "groups": groups, "checks": checks,
    "manual_four_probes_performed": passed, "actual_hand_taps_performed": False,
    "acceptance_mode": "physical-device equivalent self-driven playback; ListenNote source assertion",
    "listen_note": {"visibility_pass": note_source_pass, "mode": "source assertion; no note written",
                    "player_source": view_path, "player_line": view[:view.index(call)].count("\n") + 1,
                    "section_source": section_path, "automatic_note_write": False},
    "merge": {"scope": "单集已落盘、library.json 未整库盖、基线 52 保留",
              "prior_evidence": "../device-file-verification.json", "prior_evidence_sha256": sha((HERE.parent / "device-file-verification.json").read_bytes()),
              "before_count": len(before_ids), "after_count": len(after_ids),
              "library_sha256": sha(after), "byte_identical": before == after},
    "after_process_restart": {"preferences_sha256": sha(prefs_bytes), "Norton_mark": mark,
                              "continue_card_id": eligible[0] if eligible else None},
    "samples": device["samples"], "semantic_landing_verified": False,
    "claim_phone_listenable": False, "remaining_user_steps_for_four_probe": "无用户步骤",
    "remaining_limit": "未做音频语义听辨；时长门禁和 seek 偏差不证明语义正确，继续禁止宣称可听。",
}
(HERE / "manual-four-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"pass": passed, "groups": groups, "checks": len(checks)}, ensure_ascii=False))
raise SystemExit(0 if passed else 1)
