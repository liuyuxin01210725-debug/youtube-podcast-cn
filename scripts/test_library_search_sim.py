"""Run the library UI acceptance tests with synthetic media on a fresh simulator."""

import argparse
import hashlib
import json
import plistlib
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = "com.liuyuxin.privatelisten"
SYNTHETIC_ID = "a11ece00150a11ed0000000000261110"
SEARCH_CASE = "PrivateListenUITests/LibrarySearchUITests"
JOURNEY_CASE = "PrivateListenUITests/LibraryJourneyUITests"
ABSENT_TEST = "testLaunchArgumentLeavesLibraryVisible"
RECENT_ID = "ui-search-row-55"
NEXT_ID = "ui-search-row-54"
NOTE_APPEND = "\nAdded by acceptance"
LIBRARY_PATH = Path("Documents/中文播客私人库")
PREFERENCES_PATH = Path(f"Library/Preferences/{BUNDLE}.plist")
NOTES_PATH = Path("Documents/listen_notes.json")
INSIGHTS_PATH = Path("Documents/listen_note_insights.json")


def run(*args, capture=False):
    result = subprocess.run(
        args, cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout.strip() if capture else None


def add_episode(episodes, episode_id, title, audio):
    folder = episodes / episode_id
    folder.mkdir()
    shutil.copyfile(audio, folder / "output.mp3")
    row = {"id": episode_id, "title": title, "duration_sec": 8}
    (folder / "episode.json").write_text(json.dumps(row))
    return row


def write_catalog(episodes, rows):
    listing = episodes.parent / "library.json"
    temporary = listing.with_suffix(".partial")
    temporary.write_text(json.dumps({"episodes": rows}))
    temporary.replace(listing)


def seed_layout(container, audio):
    library = container / LIBRARY_PATH
    library.parent.mkdir(exist_ok=True)
    library.mkdir()
    episodes = library / "episodes"
    episodes.mkdir()
    rows = []
    for number in range(1, 57):
        title = "LibrarySearchNeedle" if number == 56 else f"Layout fixture episode {number}"
        rows.append(add_episode(episodes, f"ui-search-row-{number:02d}", title, audio))
    write_catalog(episodes, rows)
    return episodes, rows


def seed_existing_state(container):
    preferences = container / PREFERENCES_PATH
    preferences.parent.mkdir(parents=True, exist_ok=True)
    values = plistlib.loads(preferences.read_bytes()) if preferences.is_file() else {}
    values.update({
        "PrivateListen.progress": json.dumps({"ui-search-row-55": {"seconds": 3, "finished": False}}).encode(),
        "PrivateListen.continueDismissed": [SYNTHETIC_ID],
        "PrivateListen.continueTouched": b'{"ui-search-row-55":1}',
    })
    preferences.write_bytes(plistlib.dumps(values))
    timestamp = "2026-11-19T00:00:00.000Z"
    notes = {
        "schema": "shengjian.listen_notes.v1",
        "schema_version": 1,
        "updated_at": timestamp,
        "notes": {SYNTHETIC_ID: {
            "episode_id": SYNTHETIC_ID,
            "headline": "Existing synthetic draft",
            "body": "Existing synthetic draft",
            "tags": [" legacy "],
            "updated_at": timestamp,
        }},
        "prompt_dismissed": {},
    }
    (container / NOTES_PATH).write_text(json.dumps(notes))


def fixture_error(phase, detail):
    return SystemExit(f"{phase}: simulator fixture/container problem: {detail}")


def resolve_container(simctl, device, phase):
    try:
        container = Path(run(*simctl, "get_app_container", device, BUNDLE, "data", capture=True))
    except subprocess.CalledProcessError as error:
        raise fixture_error(phase, f"could not resolve the app data container for {device}") from error
    if not container.is_absolute() or not container.is_dir():
        raise fixture_error(phase, f"app data container is unavailable: {container}")
    return container


def require_layout(container, rows, phase):
    library = container / LIBRARY_PATH
    catalog = library / "library.json"
    episodes = library / "episodes"
    try:
        actual = json.loads(catalog.read_text())
    except (OSError, ValueError) as error:
        raise fixture_error(phase, f"cannot read seeded catalog {catalog}: {error}") from error
    if actual != {"episodes": rows}:
        raise fixture_error(phase, f"seeded catalog differs at {catalog} (expected {len(rows)} rows)")
    for row in rows:
        for name in ("episode.json", "output.mp3"):
            file = episodes / row["id"] / name
            if not file.is_file():
                raise fixture_error(phase, f"missing seeded episode file {file}")
    return episodes


def snapshot(container):
    preferences = container / PREFERENCES_PATH
    values = plistlib.loads(preferences.read_bytes()) if preferences.is_file() else {}
    state = {name: values.get(f"PrivateListen.{name}") for name in (
        "progress", "continueDismissed", "continueTouched",
    )}
    for path in (NOTES_PATH, INSIGHTS_PATH):
        file = container / path
        state[path.name] = hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None
    return state


def seed_journeys(container, episodes, rows, audio):
    for row in rows:
        if row["id"] in (RECENT_ID, NEXT_ID):
            folder = episodes / row["id"]
            row["duration_sec"] = 60
            shutil.copyfile(audio, folder / "output.mp3")
            (folder / "episode.json").write_text(json.dumps(row))
    write_catalog(episodes, rows)

    preferences = container / PREFERENCES_PATH
    values = plistlib.loads(preferences.read_bytes())
    values.update({
        "PrivateListen.progress": json.dumps({
            RECENT_ID: {"seconds": 9, "finished": False},
            NEXT_ID: {"seconds": 7, "finished": False},
        }).encode(),
        "PrivateListen.continueTouched": json.dumps({RECENT_ID: 2, NEXT_ID: 1}).encode(),
        "PrivateListen.starred": json.dumps([RECENT_ID]).encode(),
    })
    preferences.write_bytes(plistlib.dumps(values))

    notes_file = container / NOTES_PATH
    notes = json.loads(notes_file.read_text())
    notes["notes"][NEXT_ID] = {
        "episode_id": NEXT_ID, "headline": "Keep this other note", "body": "Untouched body",
        "tags": ["fixture"], "updated_at": notes["updated_at"],
    }
    notes["notes"][SYNTHETIC_ID].update({
        "headline": "Original listening note",
        "body": "\n".join([f"Earlier paragraph {n:02d}: The original note stays here."
                           for n in range(1, 41)] + ["Final note marker"]),
        "tags": ["fixture"],
    })
    notes_file.write_text(json.dumps(notes))

    # A target insight already present should survive the editor's normal save path.
    insights = {
        "schema": "shengjian.listen_note_insights.v1", "schema_version": 1,
        "updated_at": notes["updated_at"],
        "insights": {SYNTHETIC_ID: {
            "episode_id": SYNTHETIC_ID, "summary": "Existing insight", "body": "Keep this insight",
            "keywords": [], "updated_at": notes["updated_at"],
        }},
    }
    (container / INSIGHTS_PATH).write_text(json.dumps(insights))
    return values, notes, (container / INSIGHTS_PATH).read_bytes()


def assert_journeys(container, before):
    preferences, notes, insights = before
    saved = json.loads((container / NOTES_PATH).read_text())
    original = notes["notes"][SYNTHETIC_ID]
    edited = saved["notes"].get(SYNTHETIC_ID, {})
    expected = {**original, "body": original["headline"] + "\n" + original["body"] + NOTE_APPEND}
    if {**edited, "updated_at": original["updated_at"]} != expected:
        raise SystemExit("Listen note on disk did not preserve the whole original and append at the end")
    if {**saved, "updated_at": notes["updated_at"],
        "notes": {**saved["notes"], SYNTHETIC_ID: original}} != notes:
        raise SystemExit("Listen note journey changed another episode or the prompt bookkeeping")
    if (container / INSIGHTS_PATH).read_bytes() != insights:
        raise SystemExit("Listen note journey changed the existing insights file")
    print("Listen note saved the full original and append; other notes and insights unchanged", flush=True)

    values = plistlib.loads((container / PREFERENCES_PATH).read_bytes())
    for name in ("progress", "continueTouched", "starred"):
        key = f"PrivateListen.{name}"
        if key not in values or json.loads(values[key]) != json.loads(preferences[key]):
            raise SystemExit(f"Continue swipe changed {key}; both progress marks must stay unfinished")
    expected_dismissed = set(preferences["PrivateListen.continueDismissed"]) | {RECENT_ID}
    if set(values.get("PrivateListen.continueDismissed", [])) != expected_dismissed:
        raise SystemExit("Continue swipe did not persist dismissal of only the newest card")
    print("Continue swipe persisted dismissal; episode files, unfinished progress and stars retained", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("simulator_udid")
    parser.add_argument("output", type=Path, help="new directory for builds and test results")
    parser.add_argument("--simctl-screenshots", action="store_true", help="also capture the simulator screen after each phase")
    args = parser.parse_args()
    device = args.simulator_udid
    simctl = ("xcrun", "simctl")
    try:
        devices = json.loads(run(*simctl, "list", "devices", "booted", "-j", capture=True))
    except subprocess.CalledProcessError as error:
        raise SystemExit("Simulator preflight failed: simctl could not list booted devices") from error
    if not any(row["udid"] == device and row["name"].startswith("Shengjian Search Isolated")
               and "iPhone" in row.get("deviceTypeIdentifier", "")
               for rows in devices["devices"].values() for row in rows):
        raise SystemExit("Require a booted disposable iPhone simulator named Shengjian Search Isolated*")
    existing = subprocess.run(
        [*simctl, "get_app_container", device, BUNDLE, "data"], capture_output=True, text=True
    )
    if existing.returncode == 0:
        raise SystemExit("The simulator already has Shengjian installed. Use a fresh simulator.")

    output = args.output.resolve()
    output.mkdir()
    audio = output / "layout-tone.mp3"
    run("ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=8", str(audio))
    build = (
        "xcodebuild", "-project", "ios/PrivateListen/PrivateListen.xcodeproj", "-scheme", "PrivateListen",
        "-destination", f"platform=iOS Simulator,id={device}", "-parallel-testing-enabled", "NO",
    )

    def configuration(name):
        return (*build, "-configuration", name, "-derivedDataPath", str(output / name))

    def test(name, phase, rows, case, *methods, expected_snapshot=None):
        result = output / f"{phase}.xcresult"
        initial = resolve_container(simctl, device, f"{phase} before test")
        require_layout(initial, rows, f"{phase} before test")
        if expected_snapshot is not None and snapshot(initial) != expected_snapshot:
            raise fixture_error(phase, "seeded search state differs in the live container before the test")
        try:
            run(*configuration(name), "-resultBundlePath", str(result),
                *(f"-only-testing:{case}/{method}" for method in methods), "test-without-building")
        finally:
            try:
                live = resolve_container(simctl, device, f"{phase} after test")
                if initial != live:
                    print(f"{phase}: app data container moved from {initial.name} to {live.name}", flush=True)
                episodes = require_layout(live, rows, f"{phase} after test")
            finally:
                if result.is_dir():
                    run("xcrun", "xcresulttool", "export", "attachments", "--path", str(result),
                        "--output-path", str(output / "attachments" / phase))
        if args.simctl_screenshots:
            run(*simctl, "io", device, "screenshot", str(output / f"{phase}-simctl.png"))
        return live, episodes

    run(*configuration("Debug"), "build-for-testing")
    app = output / "Debug/Build/Products/Debug-iphonesimulator/PrivateListen.app"
    run(*simctl, "install", device, str(app))
    container = resolve_container(simctl, device, "search seed")
    episodes, rows = seed_layout(container, audio)
    seed_existing_state(container)
    before = snapshot(container)
    container, episodes = test("Debug", "debug-missing-fixed-id", rows, SEARCH_CASE, ABSENT_TEST,
                               expected_snapshot=before)
    if before != snapshot(container):
        raise SystemExit("Debug missing-fixed-ID launch changed progress, Continue bookkeeping, notes, or insights")
    print("Debug missing-fixed-ID snapshot unchanged", flush=True)

    rows.append(add_episode(episodes, SYNTHETIC_ID, "TEST-ISOLATE UI TEST", audio))
    write_catalog(episodes, rows)
    container, _ = test("Debug", "debug-search-and-entry", rows, SEARCH_CASE,
                        "testSearchFieldAndToolbarFocusWithLargeLibrary",
                        "testSyntheticLaunchStaysPausedAndSearchDoesNotHitMiniPlayer", expected_snapshot=before)
    if before != snapshot(container):
        raise SystemExit("Debug search/entry changed progress, Continue bookkeeping, notes, or insights")
    print("Debug snapshot unchanged", flush=True)

    run(*configuration("Release"), "build-for-testing")
    container, _ = test("Release", "release-entry-unavailable", rows, SEARCH_CASE, ABSENT_TEST,
                        expected_snapshot=before)
    if before != snapshot(container):
        raise SystemExit("Release launch changed progress, Continue bookkeeping, notes, or insights")
    print("Release snapshot unchanged", flush=True)

    # Start the new journeys with fresh preferences, after the search assertions.
    # Writing a plist over an already loaded UserDefaults domain can leave cached values.
    long_audio = output / "continue-tone.mp3"
    run("ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=60", str(long_audio))
    run(*simctl, "uninstall", device, BUNDLE)
    run(*simctl, "install", device, str(app))
    container = resolve_container(simctl, device, "journey seed")
    episodes, rows = seed_layout(container, audio)
    seed_existing_state(container)
    rows.append(add_episode(episodes, SYNTHETIC_ID, "TEST-ISOLATE UI TEST", audio))
    journey_before = seed_journeys(container, episodes, rows, long_audio)
    container, _ = test("Debug", "debug-library-journeys", rows, JOURNEY_CASE,
                        "testLongListenNoteAppendsScrollsAndPersists",
                        "testSwipingContinueCardPromotesNextUnfinishedEpisode")
    assert_journeys(container, journey_before)


if __name__ == "__main__":
    main()
