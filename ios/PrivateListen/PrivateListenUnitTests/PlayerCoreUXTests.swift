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

    func testTranscriptLinesPreserveOriginalTextAndDegradeWhenChineseOnly() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        var item = episode()
        item.audioURL = folder.appendingPathComponent("output.mp3")

        let mixed = #"{"segments":[{"t_start":2,"t_end":8,"text":"source","zh":"中文"},{"text":"第二段"}]}"#
        try Data(mixed.utf8).write(to: folder.appendingPathComponent("transcript_zh.json"))
        let readable = await PlayerReading.load(item)
        XCTAssertEqual(readable.paragraphs, ["中文", "第二段"])
        XCTAssertEqual(readable.lines.count, 2)
        XCTAssertEqual(readable.lines[0].displayText, "中文")
        XCTAssertEqual(readable.lines[0].originalText, "source")
        XCTAssertEqual(readable.lines[0].start, 2)
        XCTAssertEqual(readable.lines[0].end, 8)
        XCTAssertEqual(readable.lines[1].displayText, "第二段")
        XCTAssertNil(readable.lines[1].originalText)

        let overlapNode = ListenViewpoint(id: "p1", viewpointText: "要点", tStart: 4, tEnd: 6)
        let distantNode = ListenViewpoint(id: "p2", viewpointText: "远处", tStart: 30, tEnd: 40)
        XCTAssertEqual(readable.originalText(for: overlapNode), "source")
        XCTAssertNil(readable.originalText(for: distantNode))

        let enPreferred = #"{"segments":[{"id":7,"t_start":10,"t_end":18,"text":"raw","zh":"中译","en":"English line"}]}"#
        try Data(enPreferred.utf8).write(to: folder.appendingPathComponent("transcript_zh.json"))
        let withEn = await PlayerReading.load(item)
        XCTAssertEqual(withEn.paragraphs, ["中译"])
        XCTAssertEqual(withEn.lines.first?.id, "7")
        XCTAssertEqual(withEn.lines.first?.originalText, "English line")

        let chineseOnly = #"{"segments":[{"t_start":1,"t_end":5,"text":"纯中文内容","zh":"纯中文内容"}]}"#
        try Data(chineseOnly.utf8).write(to: folder.appendingPathComponent("transcript_zh.json"))
        let degraded = await PlayerReading.load(item)
        XCTAssertEqual(degraded.paragraphs, ["纯中文内容"])
        XCTAssertNil(degraded.lines.first?.originalText)
        XCTAssertNil(degraded.originalText(for: ListenViewpoint(id: "p3", viewpointText: "要点", tStart: 2, tEnd: 4)))

        item.revisionFiles = ["output.mp3"]
        let unpublished = await PlayerReading.load(item)
        XCTAssertTrue(unpublished.lines.isEmpty)
        XCTAssertTrue(unpublished.paragraphs.isEmpty)
    }

    private func episode() -> Episode {
        Episode(id: "first", title: "Fixture", guests: [], lensIds: [], createdAt: .distantPast,
                audioURL: URL(fileURLWithPath: "/fixture/output.mp3"), downloading: false)
    }
}
