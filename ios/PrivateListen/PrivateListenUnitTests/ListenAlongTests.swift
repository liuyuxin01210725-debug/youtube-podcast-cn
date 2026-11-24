import CryptoKit
import XCTest
@testable import PrivateListen

@MainActor
final class ListenAlongTests: XCTestCase {
    func testSaveListSortDeleteAndLegacyEpisodeFallback() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = ListenAlongNoteStore(directory: directory)
        XCTAssertFalse(store.isReadOnly)
        XCTAssertNil(store.add(mediaKey: "ep1", episodeId: "ep1", position: 12, text: "   \n  "))

        let legacyNote = try XCTUnwrap(store.add(mediaKey: "ep1", episodeId: "ep1", position: 42, text: "旧版本笔记"))
        let revKey = "ep1." + String(repeating: "a", count: 64)
        XCTAssertEqual(store.notes(for: revKey, legacyEpisodeId: "ep1"), [legacyNote])

        let second = try XCTUnwrap(store.add(mediaKey: revKey, episodeId: "ep1", position: 95, text: "后半段"))
        let first = try XCTUnwrap(store.add(mediaKey: revKey, episodeId: "ep1", position: -5, text: "开头夹紧"))
        XCTAssertEqual(first.positionSec, 0)

        let reopened = ListenAlongNoteStore(directory: directory)
        let listed = reopened.notes(for: revKey, legacyEpisodeId: "ep1")
        XCTAssertEqual(listed.map(\.id), [first.id, second.id])
        XCTAssertEqual(reopened.notes(for: "ep1").map(\.id), [legacyNote.id])

        XCTAssertTrue(reopened.delete(id: first.id))
        XCTAssertEqual(reopened.notes(for: revKey, legacyEpisodeId: "ep1").map(\.id), [second.id])
        XCTAssertTrue(reopened.delete(id: second.id))
        XCTAssertEqual(reopened.notes(for: revKey, legacyEpisodeId: "ep1").map(\.id), [legacyNote.id])
    }

    func testCorruptAndUnsupportedFilesStayReadOnlyAndByteIdentical() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent(ListenAlongNoteStore.fileName)

        let unknownSchema = """
        {"schema":"unknown","schema_version":1,"updated_at":"","notes":[]}
        """
        let futureVersion = """
        {"schema":"shengjian.listen_along_notes.v1","schema_version":2,"updated_at":"","notes":[]}
        """
        for invalid in ["{broken", unknownSchema, futureVersion].map({ Data($0.utf8) }) {
            try invalid.write(to: file)
            let store = ListenAlongNoteStore(directory: directory)
            XCTAssertTrue(store.isReadOnly)
            XCTAssertNil(store.add(mediaKey: "ep1", episodeId: "ep1", position: 10, text: "不能写入"))
            XCTAssertFalse(store.delete(id: "any"))
            let preserved = try Data(contentsOf: file)
            XCTAssertEqual(preserved, invalid)
            XCTAssertEqual(SHA256.hash(data: preserved), SHA256.hash(data: invalid))
        }
    }

    func testExternalTamperingLocksWritesWithoutOverwritingFile() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent(ListenAlongNoteStore.fileName)

        let store = ListenAlongNoteStore(directory: directory)
        let saved = try XCTUnwrap(store.add(mediaKey: "ep1", episodeId: "ep1", position: 15, text: "原笔记"))
        let tampered = Data("external overwrite".utf8)
        try tampered.write(to: file)

        XCTAssertFalse(store.delete(id: saved.id))
        XCTAssertTrue(store.isReadOnly)
        XCTAssertNil(store.add(mediaKey: "ep1", episodeId: "ep1", position: 20, text: "后续写入"))
        XCTAssertEqual(try Data(contentsOf: file), tampered)
    }

    func testShareExcludedFileNamesIncludeListenAlongAndPreserveExisting() {
        XCTAssertTrue(ListenNoteStore.isShareExcluded("listen_notes.json"))
        XCTAssertTrue(ListenNoteStore.isShareExcluded("listen-note-probe.json"))
        XCTAssertTrue(ListenNoteStore.isShareExcluded(ListenNoteDownstream.insightsFileName))
        XCTAssertTrue(ListenNoteStore.isShareExcluded(ListenAlongNoteStore.fileName))
    }

    func testAssociationMatchesNearbyPointsAndListenNoteAndIsReversible() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ListenAlongNoteStore(directory: directory)

        let note = try XCTUnwrap(store.add(mediaKey: "ep1", episodeId: "ep1", position: 100, text: "关于小而美品牌的思考"))
        let points = [
            ListenViewpoint(id: "overlap", viewpointText: "品牌定位的核心", tStart: 95, tEnd: 110),
            ListenViewpoint(id: "near", viewpointText: "渠道节奏", tStart: 125, tEnd: 130),
            ListenViewpoint(id: "nearThird", viewpointText: "第三条较近点段", tStart: 72, tEnd: 74),
            ListenViewpoint(id: "far", viewpointText: "远离的点段", tStart: 200, tEnd: 220),
        ]
        let listenNote = ListenNote(
            episodeId: "ep1",
            headline: "小而美创业笔记",
            body: "值得反复听",
            tags: ["品牌"],
            updatedAt: ""
        )

        let links = ListenAlongAssociation.links(for: note, points: points, listenNote: listenNote)
        XCTAssertEqual(links.count, 3)

        if case let .point(id, _, tStart, reason) = links[0] {
            XCTAssertEqual(id, "overlap")
            XCTAssertEqual(tStart, 95)
            XCTAssertTrue(reason.contains("1:35"))
            XCTAssertTrue(reason.contains("品牌"))
        } else {
            XCTFail("Expected overlapping point first")
        }

        if case let .point(id, _, tStart, _) = links[1] {
            XCTAssertEqual(id, "near")
            XCTAssertEqual(tStart, 125)
        } else {
            XCTFail("Expected second nearest point")
        }

        if case let .listenNote(episodeId, summary, reason) = links[2] {
            XCTAssertEqual(episodeId, "ep1")
            XCTAssertEqual(summary, "小而美创业笔记")
            XCTAssertTrue(reason.contains("小而美"))
        } else {
            XCTFail("Expected listen note link third")
        }

        let farOnly = ListenAlongAssociation.links(
            for: note,
            points: [ListenViewpoint(id: "far", viewpointText: "小而美品牌远离", tStart: 200, tEnd: 220)],
            listenNote: nil
        )
        XCTAssertTrue(farOnly.isEmpty)

        let plainNote = ListenNote(episodeId: "ep1", headline: "另一想法", body: "无重合词", tags: [], updatedAt: "")
        let plainLinks = ListenAlongAssociation.links(for: note, points: [], listenNote: plainNote)
        XCTAssertEqual(
            plainLinks,
            [.listenNote(episodeId: "ep1", summary: "另一想法", reason: "同一期的听后感")]
        )

        XCTAssertTrue(store.delete(id: note.id))
        let remainingLinks = store.notes(for: "ep1", legacyEpisodeId: "ep1").flatMap {
            ListenAlongAssociation.links(for: $0, points: points, listenNote: listenNote)
        }
        XCTAssertTrue(remainingLinks.isEmpty)
    }

    private func makeDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("listen-along-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        return directory
    }
}
