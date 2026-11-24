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
        XCTAssertEqual(listenClock(first.positionSec), "0:00")
        XCTAssertEqual(listenClock(legacyNote.positionSec), "0:42")
        XCTAssertEqual(listenClock(second.positionSec), "1:35")

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
            XCTAssertEqual(reason, "时间接近 1:35 · 关键词重合：品牌")
            XCTAssertFalse(reason.contains("语义"))
        } else {
            XCTFail("Expected overlapping point first")
        }

        if case let .point(id, _, tStart, reason) = links[1] {
            XCTAssertEqual(id, "near")
            XCTAssertEqual(tStart, 125)
            XCTAssertEqual(reason, "时间接近 2:05")
            XCTAssertFalse(reason.contains("语义"))
        } else {
            XCTFail("Expected second nearest point")
        }

        if case let .listenNote(episodeId, summary, reason) = links[2] {
            XCTAssertEqual(episodeId, "ep1")
            XCTAssertEqual(summary, "小而美创业笔记")
            XCTAssertEqual(reason, "同一期的听后感 · 关键词重合：小而美、品牌")
            XCTAssertFalse(reason.contains("语义"))
        } else {
            XCTFail("Expected listen note link third")
        }

        let otherEpisodeListenNote = ListenNote(
            episodeId: "ep2",
            headline: "小而美另一集",
            body: "品牌思考",
            tags: ["品牌"],
            updatedAt: ""
        )
        XCTAssertTrue(ListenAlongAssociation.links(for: note, points: [], listenNote: otherEpisodeListenNote).isEmpty)

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

    func testMediaKeyIsolationAndLegacyFallbackScope() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = ListenAlongNoteStore(directory: directory)
        let revA1 = "epA." + String(repeating: "a", count: 64)
        let revA2 = "epA." + String(repeating: "b", count: 64)
        let revB = "epB." + String(repeating: "c", count: 64)

        let noteA = try XCTUnwrap(store.add(mediaKey: revA1, episodeId: "epA", position: 18, text: "epA 修订一笔记"))
        let legacyB = try XCTUnwrap(store.add(mediaKey: "epB", episodeId: "epB", position: 33, text: "epB 旧笔记"))

        // epA 的笔记不会出现在无关 epB，反之亦然。
        XCTAssertEqual(store.notes(for: revA1, legacyEpisodeId: "epA"), [noteA])
        XCTAssertEqual(store.notes(for: "epB", legacyEpisodeId: "epB"), [legacyB])
        XCTAssertEqual(store.notes(for: revB, legacyEpisodeId: "epB"), [legacyB])
        XCTAssertTrue(store.notes(for: "epC", legacyEpisodeId: "epC").isEmpty)

        // 有精确 mediaKey 命中时，即使误传其它集的 legacyEpisodeId 也不会串集。
        XCTAssertEqual(store.notes(for: revA1, legacyEpisodeId: "epB"), [noteA])

        // 无精确命中时，legacy 回退不得借用其它 episodeId 或同集其它修订的笔记。
        XCTAssertTrue(store.notes(for: revA2, legacyEpisodeId: "epB").isEmpty)
        XCTAssertTrue(store.notes(for: revA2, legacyEpisodeId: "epA").isEmpty)

        // 仅当存在同一 episodeId 的裸 id 旧记录时，新修订才回退读取。
        let legacyA = try XCTUnwrap(store.add(mediaKey: "epA", episodeId: "epA", position: 5, text: "epA 裸 id 旧笔记"))
        XCTAssertEqual(store.notes(for: revA2, legacyEpisodeId: "epA"), [legacyA])
        XCTAssertEqual(store.notes(for: revA1, legacyEpisodeId: "epA"), [noteA])
    }

    func testUnknownExtensionFieldsSurviveWriteAndDeleteRoundtrip() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent(ListenAlongNoteStore.fileName)

        let seeded = """
        {
          "future_doc_ext": {"flag": true, "items": [1, "keep"]},
          "notes": [
            {
              "created_at": "2026-11-24T00:00:00.000Z",
              "episode_id": "ep1",
              "future_note_ext": {"origin": "import", "rank": 2},
              "id": "note-seeded",
              "media_key": "ep1",
              "position_sec": 12.5,
              "text": "已有笔记"
            }
          ],
          "schema": "shengjian.listen_along_notes.v1",
          "schema_version": 1,
          "updated_at": "2026-11-24T00:00:00.000Z"
        }
        """
        try Data(seeded.utf8).write(to: file)

        let store = ListenAlongNoteStore(directory: directory)
        XCTAssertFalse(store.isReadOnly)
        XCTAssertEqual(store.notes(for: "ep1").map(\.id), ["note-seeded"])

        let added = try XCTUnwrap(store.add(mediaKey: "ep1", episodeId: "ep1", position: 30, text: "新增笔记"))
        let afterAdd = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        let docExt = try XCTUnwrap(afterAdd["future_doc_ext"] as? [String: Any])
        XCTAssertEqual(docExt["flag"] as? Bool, true)
        let rawNotes = try XCTUnwrap(afterAdd["notes"] as? [[String: Any]])
        let seededRow = try XCTUnwrap(rawNotes.first { ($0["id"] as? String) == "note-seeded" })
        let noteExt = try XCTUnwrap(seededRow["future_note_ext"] as? [String: Any])
        XCTAssertEqual(noteExt["origin"] as? String, "import")
        XCTAssertEqual(noteExt["rank"] as? Int, 2)

        let reopened = ListenAlongNoteStore(directory: directory)
        XCTAssertEqual(reopened.notes(for: "ep1").count, 2)
        XCTAssertTrue(reopened.delete(id: added.id))

        let afterDelete = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        XCTAssertEqual((afterDelete["future_doc_ext"] as? [String: Any])?["flag"] as? Bool, true)
        let remainingRows = try XCTUnwrap(afterDelete["notes"] as? [[String: Any]])
        XCTAssertEqual(remainingRows.count, 1)
        XCTAssertEqual((remainingRows[0]["future_note_ext"] as? [String: Any])?["origin"] as? String, "import")
    }

    func testPositionClampingForNonFiniteAndNegativeValuesPersistsAcrossReopen() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let store = ListenAlongNoteStore(directory: directory)
        for (index, rawPosition) in [-12.5, Double.nan, Double.infinity, -Double.infinity].enumerated() {
            let saved = try XCTUnwrap(store.add(mediaKey: "ep1", episodeId: "ep1", position: rawPosition, text: "位置\(index)"))
            XCTAssertEqual(saved.positionSec, 0)
            XCTAssertEqual(listenClock(saved.positionSec), "0:00")
        }

        let reopened = ListenAlongNoteStore(directory: directory)
        let listed = reopened.notes(for: "ep1")
        XCTAssertEqual(listed.count, 4)
        XCTAssertTrue(listed.allSatisfy { $0.positionSec == 0 && listenClock($0.positionSec) == "0:00" })
    }

    private func makeDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("listen-along-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        return directory
    }
}
