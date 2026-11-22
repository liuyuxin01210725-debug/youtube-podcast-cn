#!/usr/bin/env python3
"""Import a local sanitized gen2 library into Search Isolated and run real-library regressions.

Uninstalls the Search Isolated app only when it is already installed. Does not
erase that simulator. Does not touch Point Listen Isolated. Does not upload or
commit the library copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = "com.liuyuxin.privatelisten"
SEARCH_NAME_PREFIX = "Shengjian Search Isolated"
POINT_NAME_PREFIX = "Shengjian Point Listen Isolated"
IDS_SWIFT = ROOT / "ios/PrivateListen/PrivateListenUITests/RealLibraryIDs.swift"
TEST_CLASS = "PrivateListenUITests/RealLibraryRegressionUITests"
CASES = [
    "testShortPlaybackReturnsJustPlayedToContinue",
    "testSwipingContinueCardKeepsUnfinishedProgress",
]


def run(*args, capture=False, check=True, timeout=None):
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=timeout,
    )
    return result.stdout.strip() if capture else result


def booted_devices():
    payload = json.loads(run("xcrun", "simctl", "list", "devices", "booted", "-j", capture=True))
    return [row for group in payload["devices"].values() for row in group]


def require_search_isolated(device: str) -> None:
    rows = booted_devices()
    if not any(row["udid"] == device and row["name"].startswith(SEARCH_NAME_PREFIX) for row in rows):
        raise SystemExit("Require a booted disposable simulator named Shengjian Search Isolated*")
    point = [row for row in rows if row["name"].startswith(POINT_NAME_PREFIX)]
    if point:
        print("leaving booted Point Listen Isolated untouched:", point[0]["udid"], flush=True)


def uninstall_search_app_only(device: str) -> bool:
    existing = subprocess.run(
        ["xcrun", "simctl", "get_app_container", device, BUNDLE, "data"],
        capture_output=True,
        text=True,
    )
    if existing.returncode != 0:
        return False
    run("xcrun", "simctl", "uninstall", device, BUNDLE)
    gone = subprocess.run(
        ["xcrun", "simctl", "get_app_container", device, BUNDLE, "data"],
        capture_output=True,
        text=True,
    )
    if gone.returncode == 0:
        raise SystemExit("uninstall left the Search Isolated app installed")
    print("uninstalled existing Search Isolated app only", flush=True)
    return True


def escape_swift(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def write_ids_swift(selection: dict) -> None:
    lines = [
        "// Generated from a local non-exportable sanitized library. Do not commit.",
        "enum RealLibraryIDs {",
        f'    static let searchEpisodeID = "{escape_swift(selection["search_episode_id"])}"',
        f'    static let searchNeedle = "{escape_swift(selection["search_needle"])}"',
        f'    static let longNoteEpisodeID = "{escape_swift(selection["long_note_episode_id"])}"',
        f'    static let unfinishedEpisodeID = "{escape_swift(selection["unfinished_episode_id"])}"',
        f'    static let unfinishedTitle = "{escape_swift(selection["unfinished_title"])}"',
        "}",
        "",
    ]
    IDS_SWIFT.write_text("\n".join(lines))


def copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    result = subprocess.run(["cp", "-cR", str(src), str(dest)], capture_output=True, text=True)
    if result.returncode == 0:
        return
    if src.is_dir():
        shutil.copytree(src, dest, copy_function=shutil.copy2)
    else:
        shutil.copy2(src, dest)


def seed_container(container: Path, sanitized: Path) -> None:
    documents = container / "Documents"
    documents.mkdir(exist_ok=True)
    for child in (sanitized / "Documents").iterdir():
        copy_tree(child, documents / child.name)

    src_plist = sanitized / "Library/Preferences/com.liuyuxin.privatelisten.plist"
    dest_plist = container / f"Library/Preferences/{BUNDLE}.plist"
    dest_plist.parent.mkdir(parents=True, exist_ok=True)
    existing = plistlib.loads(dest_plist.read_bytes()) if dest_plist.is_file() else {}
    incoming = plistlib.loads(src_plist.read_bytes())
    for key, value in incoming.items():
        if key.startswith("PrivateListen."):
            existing[key] = value
    dest_plist.write_bytes(plistlib.dumps(existing))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def structural_snapshot(library_json: Path, documents: Path) -> dict:
    catalog = json.loads(library_json.read_text())
    episodes = []
    for row in catalog.get("episodes") or []:
        revision = row.get("media_revision") or {}
        episodes.append(
            {
                "id": row.get("id"),
                "media_sha256": revision.get("sha256"),
                "path": revision.get("path"),
                "files": revision.get("files"),
            }
        )
    staging = documents / ".push-staging"
    return {
        "schema": catalog.get("schema"),
        "generation": catalog.get("generation"),
        "parent": catalog.get("parent"),
        "transactions": catalog.get("transactions"),
        "episode_ids": [row.get("id") for row in catalog.get("episodes") or []],
        "episode_revisions": episodes,
        "push_staging": sorted(p.name for p in staging.iterdir()) if staging.exists() else [],
    }


def verify_sidecar_hashes(library_dir: Path, snapshot: dict) -> list[str]:
    problems = []
    for row in snapshot["episode_revisions"]:
        path = row.get("path")
        files = row.get("files") or {}
        if not path or not files:
            continue
        folder = library_dir / path
        for name, digest in files.items():
            target = folder / name
            if not target.is_file():
                problems.append(f"missing {path}/{name}")
                continue
            if name.endswith(".mp3"):
                continue
            if sha256_file(target) != digest:
                problems.append(f"hash mismatch {path}/{name}")
    return problems


def launch_once(device: str) -> None:
    subprocess.run(["xcrun", "simctl", "terminate", device, BUNDLE], check=False)
    run("xcrun", "simctl", "launch", device, BUNDLE)
    time.sleep(8)
    subprocess.run(["xcrun", "simctl", "terminate", device, BUNDLE], check=False)
    time.sleep(1)

def snapshot_user_state(container: Path) -> dict:
    import plistlib

    prefs = container / f"Library/Preferences/{BUNDLE}.plist"
    values = plistlib.loads(prefs.read_bytes()) if prefs.is_file() else {}
    progress = values.get("PrivateListen.progress")
    if isinstance(progress, (bytes, bytearray, str)):
        progress = json.loads(progress)
    elif not isinstance(progress, dict):
        progress = {}
    dismissed = values.get("PrivateListen.continueDismissed") or []
    if isinstance(dismissed, (bytes, bytearray)):
        dismissed = json.loads(dismissed)
    starred = values.get("PrivateListen.starred") or []
    if isinstance(starred, (bytes, bytearray)):
        starred = json.loads(starred)

    def file_sha(name: str):
        path = container / "Documents" / name
        return sha256_file(path) if path.is_file() else None

    slim = {
        key: {"finished": (value or {}).get("finished"), "seconds": (value or {}).get("seconds")}
        for key, value in progress.items()
        if isinstance(value, dict)
    }
    return {
        "progress": slim,
        "continue_dismissed": sorted(str(item) for item in dismissed),
        "starred": sorted(str(item) for item in starred),
        "listen_notes_sha256": file_sha("listen_notes.json"),
        "book_notes_sha256": file_sha("book_notes.v1.json"),
        "book_state_sha256": file_sha("book_state.v1.json"),
    }


def compare_user_state(before: dict, after: dict, selection: dict) -> dict:
    unfinished = selection.get("unfinished_episode_id") or ""
    prefix = unfinished + "."

    def related(keys):
        return sorted(key for key in keys if key == unfinished or key.startswith(prefix))

    before_prog = before.get("progress") or {}
    after_prog = after.get("progress") or {}
    related_keys = sorted(set(related(before_prog)) | set(related(after_prog)))
    progress_rows = []
    for key in related_keys:
        left = before_prog.get(key) or {}
        right = after_prog.get(key) or {}
        progress_rows.append({
            "key_kind": "bare_id" if key == unfinished else "revision_alias",
            "before_finished": left.get("finished"),
            "after_finished": right.get("finished"),
            "before_seconds": left.get("seconds"),
            "after_seconds": right.get("seconds"),
            "finished_unchanged": left.get("finished") == right.get("finished"),
        })
    dismissed_before = set(before.get("continue_dismissed") or [])
    dismissed_after = set(after.get("continue_dismissed") or [])
    return {
        "unfinished_finished_unchanged": all(row["finished_unchanged"] for row in progress_rows) if progress_rows else True,
        "unfinished_progress_rows": progress_rows,
        "dismissed_related_before": related(dismissed_before),
        "dismissed_related_after": related(dismissed_after),
        "starred_unchanged": before.get("starred") == after.get("starred"),
        "listen_notes_unchanged": before.get("listen_notes_sha256") == after.get("listen_notes_sha256"),
        "book_notes_unchanged": before.get("book_notes_sha256") == after.get("book_notes_sha256"),
        "book_state_unchanged": before.get("book_state_sha256") == after.get("book_state_sha256"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("simulator_udid")
    parser.add_argument("sanitized_library", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    device = args.simulator_udid
    sanitized = args.sanitized_library.resolve()
    output = args.output.resolve()
    require_search_isolated(device)
    if not (sanitized / "DO-NOT-EXPORT.txt").is_file():
        raise SystemExit("refusing to import a library that lacks DO-NOT-EXPORT.txt")
    selection = json.loads((sanitized / "selection.json").read_text())
    output.mkdir(parents=True, exist_ok=True)

    uninstall_search_app_only(device)
    write_ids_swift(selection)

    derived = output / "DerivedData"
    destination = f"platform=iOS Simulator,id={device}"
    run(
        "xcodebuild",
        "-project",
        "ios/PrivateListen/PrivateListen.xcodeproj",
        "-scheme",
        "PrivateListen",
        "-destination",
        destination,
        "-parallel-testing-enabled",
        "NO",
        "-configuration",
        "Debug",
        "-derivedDataPath",
        str(derived),
        "build-for-testing",
    )
    app = derived / "Build/Products/Debug-iphonesimulator/PrivateListen.app"
    run("xcrun", "simctl", "install", device, str(app))
    container = Path(run("xcrun", "simctl", "get_app_container", device, BUNDLE, "data", capture=True))
    seed_container(container, sanitized)

    before_user = snapshot_user_state(container)
    (output / "user-state-before.json").write_text(
        json.dumps(before_user, ensure_ascii=False, indent=2) + "\n"
    )

    library_json = container / "Documents/中文播客私人库/library.json"
    before = structural_snapshot(library_json, container / "Documents")
    mapping_before = verify_sidecar_hashes(container / "Documents/中文播客私人库", before)
    launch_once(device)
    after = structural_snapshot(library_json, container / "Documents")
    mapping_after = verify_sidecar_hashes(container / "Documents/中文播客私人库", after)
    merge_report = {
        "schema_stayed_v2": after["schema"] == "private-listen.library.v2" and after["schema"] == before["schema"],
        "generation_stayed_2": after["generation"] == 2 and after["generation"] == before["generation"],
        "parent_unchanged": after["parent"] == before["parent"],
        "transactions_unchanged": after["transactions"] == before["transactions"],
        "episode_ids_unchanged": after["episode_ids"] == before["episode_ids"],
        "media_revisions_unchanged": after["episode_revisions"] == before["episode_revisions"],
        "push_staging_unchanged": after["push_staging"] == before["push_staging"],
        "sidecar_hash_problems_before": mapping_before,
        "sidecar_hash_problems_after": mapping_after,
    }
    (output / "p-merge-and-mapping.json").write_text(
        json.dumps(merge_report, ensure_ascii=False, indent=2) + "\n"
    )

    result_bundle = output / "real-library.xcresult"
    only = []
    for case in CASES:
        only.extend(["-only-testing", f"{TEST_CLASS}/{case}"])
    test = subprocess.run(
        [
            "xcodebuild",
            "-project",
            "ios/PrivateListen/PrivateListen.xcodeproj",
            "-scheme",
            "PrivateListen",
            "-destination",
            destination,
            "-parallel-testing-enabled",
            "NO",
            "-configuration",
            "Debug",
            "-derivedDataPath",
            str(derived),
            "-resultBundlePath",
            str(result_bundle),
            *only,
            "test-without-building",
        ],
        cwd=ROOT,
        text=True,
    )
    if result_bundle.is_dir():
        subprocess.run(
            [
                "xcrun",
                "xcresulttool",
                "export",
                "attachments",
                "--path",
                str(result_bundle),
                "--output-path",
                str(output / "attachments"),
            ],
            check=False,
        )
    container = Path(run("xcrun", "simctl", "get_app_container", device, BUNDLE, "data", capture=True))
    after_user = snapshot_user_state(container)
    (output / "user-state-after.json").write_text(
        json.dumps(after_user, ensure_ascii=False, indent=2) + "\n"
    )
    (output / "user-state-compare.json").write_text(
        json.dumps(compare_user_state(before_user, after_user, selection), ensure_ascii=False, indent=2) + "\n"
    )
    (output / "xcodebuild-exit.txt").write_text(f"{test.returncode}\n")
    if test.returncode != 0:
        raise SystemExit(test.returncode)
    print("real-library isolated regressions completed", flush=True)


if __name__ == "__main__":
    main()
