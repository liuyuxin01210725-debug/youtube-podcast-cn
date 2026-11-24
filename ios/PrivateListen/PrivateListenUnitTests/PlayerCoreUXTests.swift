import XCTest
@testable import PrivateListen

final class PlayerCoreUXTests: XCTestCase {
    func testSmartResumeUsesInclusiveBoundaryAndClampsToStart() {
        XCTAssertNil(PlayerPlaybackPolicy.smartResumeTarget(current: 12, pausedFor: .milliseconds(14999)))
        XCTAssertEqual(PlayerPlaybackPolicy.smartResumeTarget(current: 12, pausedFor: .seconds(15)), 9)
        XCTAssertEqual(PlayerPlaybackPolicy.smartResumeTarget(current: 2, pausedFor: .seconds(60)), 0)
        XCTAssertNil(PlayerPlaybackPolicy.smartResumeTarget(current: 0, pausedFor: .seconds(60)))
    }

    func testEpisodeOverridesSurviveColdReadAndPreserveGlobalDefault() throws {
        let suite = "PrivateListen.speed-test.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set("1.25", forKey: "PrivateListen.speed")
        var a = episode(), b = episode()
        b.id = "second"
        XCTAssertEqual(a.mediaKey, a.id)
        let store = PlaybackSpeeds(defaults: defaults)
        store.set(.half, for: a.mediaKey)
        a.mediaRevision = String(repeating: "a", count: 64)
        XCTAssertEqual(store.value(for: a.mediaKey, legacyID: a.id), .half)
        XCTAssertEqual(store.value(for: b.mediaKey), .quarter)
        store.set(.two, for: a.mediaKey)
        store.set(.one, for: b.mediaKey)
        let reopened = PlaybackSpeeds(defaults: try XCTUnwrap(UserDefaults(suiteName: suite)))
        XCTAssertEqual(reopened.value(for: a.mediaKey, legacyID: a.id), .two)
        XCTAssertEqual(reopened.value(for: a.id), .half)
        XCTAssertEqual(reopened.value(for: b.mediaKey), .one)
        XCTAssertEqual(reopened.value(for: nil), .quarter)
        XCTAssertEqual(defaults.string(forKey: "PrivateListen.speed"), "1.25")
    }

    func testLegacyEpisodeUsesContinuousTimelineEvenWithSourceTimedTranscript() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        var item = episode()
        item.audioURL = folder.appendingPathComponent("output.mp3")
        let empty = await PlayerReading.load(item)
        XCTAssertTrue(empty.nodes.isEmpty)
        XCTAssertTrue(empty.paragraphs.isEmpty)
        let json = #"{"segments":[{"t_start":2,"t_end":8,"text":"source","zh":"中文"},{"text":"第二段"}]}"#
        try Data(json.utf8).write(to: folder.appendingPathComponent("transcript_zh.json"))
        let readable = await PlayerReading.load(item)
        XCTAssertTrue(readable.nodes.isEmpty)
        XCTAssertEqual(readable.paragraphs, ["中文", "第二段"])
        item.revisionFiles = ["output.mp3"]
        let unpublished = await PlayerReading.load(item)
        XCTAssertTrue(unpublished.paragraphs.isEmpty)
    }

    private func episode() -> Episode {
        Episode(id: "first", title: "Fixture", guests: [], lensIds: [], createdAt: .distantPast,
                audioURL: URL(fileURLWithPath: "/fixture/output.mp3"), downloading: false)
    }
}
