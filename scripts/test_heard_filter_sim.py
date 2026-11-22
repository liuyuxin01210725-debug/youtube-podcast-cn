"""Seed an isolated Search simulator and run read-only 已听 / 未听完 navigation tests."""

import argparse
import json
import plistlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = "com.liuyuxin.privatelisten"
PREFERENCES_PATH = Path(f"Library/Preferences/{BUNDLE}.plist")
LIBRARY_PATH = Path("Documents/中文播客私人库")
FINISHED_ID = "ui-search-row-01"
UNFINISHED_ID = "ui-search-row-55"


def run(*args, capture=False):
    result = subprocess.run(
        args, cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout.strip() if capture else None


def write_catalog(episodes, rows):
    listing = episodes.parent / "library.json"
    temporary = listing.with_suffix(".partial")
    temporary.write_text(json.dumps({"episodes": rows}))
    temporary.replace(listing)


def seed(container, audio):
    library = container / LIBRARY_PATH
    library.parent.mkdir(exist_ok=True)
    library.mkdir()
    episodes = library / "episodes"
    episodes.mkdir()
    rows = []
    for number in range(1, 57):
        title = "LibrarySearchNeedle" if number == 56 else f"Layout fixture episode {number}"
        folder = episodes / f"ui-search-row-{number:02d}"
        folder.mkdir()
        shutil.copyfile(audio, folder / "output.mp3")
        row = {"id": f"ui-search-row-{number:02d}", "title": title, "duration_sec": 8}
        (folder / "episode.json").write_text(json.dumps(row))
        rows.append(row)
    write_catalog(episodes, rows)
    preferences = container / PREFERENCES_PATH
    preferences.parent.mkdir(parents=True, exist_ok=True)
    values = plistlib.loads(preferences.read_bytes()) if preferences.is_file() else {}
    values["PrivateListen.progress"] = json.dumps({
        FINISHED_ID: {"seconds": 8, "finished": True},
        UNFINISHED_ID: {"seconds": 3, "finished": False},
    }).encode()
    preferences.write_bytes(plistlib.dumps(values))
    return rows


def resolve_container(device):
    path = run("xcrun", "simctl", "get_app_container", device, BUNDLE, "data", capture=True)
    return Path(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("simulator_udid")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    device = args.simulator_udid
    devices = json.loads(run("xcrun", "simctl", "list", "devices", "booted", "-j", capture=True))
    if not any(
        row["udid"] == device and row["name"].startswith("Shengjian Search Isolated")
        for rows in devices["devices"].values() for row in rows
    ):
        raise SystemExit("Require a booted disposable iPhone simulator named Shengjian Search Isolated*")
    existing = subprocess.run(
        ["xcrun", "simctl", "get_app_container", device, BUNDLE, "data"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        raise SystemExit("The simulator already has Shengjian installed. Use a fresh simulator.")

    output = args.output.resolve()
    output.mkdir()
    audio = output / "heard-filter-tone.mp3"
    run("ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=8", str(audio))
    derived = output / "DerivedData"
    destination = f"platform=iOS Simulator,id={device}"
    run(
        "xcodebuild", "-project", "ios/PrivateListen/PrivateListen.xcodeproj",
        "-scheme", "PrivateListen", "-destination", destination,
        "-parallel-testing-enabled", "NO", "-configuration", "Debug",
        "-derivedDataPath", str(derived), "build-for-testing",
    )
    app = derived / "Build/Products/Debug-iphonesimulator/PrivateListen.app"
    run("xcrun", "simctl", "install", device, str(app))
    container = resolve_container(device)
    seed(container, audio)
    result = output / "heard-filter.xcresult"
    run(
        "xcodebuild", "-project", "ios/PrivateListen/PrivateListen.xcodeproj",
        "-scheme", "PrivateListen", "-destination", destination,
        "-parallel-testing-enabled", "NO", "-configuration", "Debug",
        "-derivedDataPath", str(derived),
        "-resultBundlePath", str(result),
        "-only-testing:PrivateListenUITests/LibraryFilterNavigationUITests/testFinishedFixtureOpensFromHeardFilter",
        "-only-testing:PrivateListenUITests/LibraryFilterNavigationUITests/testUnfinishedFixtureOpensFromUnfinishedFilter",
        "test-without-building",
    )
    if result.is_dir():
        run(
            "xcrun", "xcresulttool", "export", "attachments",
            "--path", str(result), "--output-path", str(output / "attachments"),
        )
    print("heard-filter isolated navigation tests completed", flush=True)


if __name__ == "__main__":
    main()
