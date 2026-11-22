import CryptoKit
import XCTest
@testable import PrivateListen

@MainActor
final class ListenNoteStoreTests: XCTestCase {
    func testCorruptAndUnsupportedFilesRemainByteIdenticalAcrossEveryWritePath() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent(ListenNoteStore.fileName)
        let unknownSchema = """
        {"schema":"unknown","schema_version":1,"updated_at":"","notes":{},"prompt_dismissed":{}}
        """
        let futureVersion = """
        {"schema":"shengjian.listen_notes.v1","schema_version":2,"updated_at":"","notes":{},"prompt_dismissed":{}}
        """
        for invalid in ["{broken", unknownSchema, futureVersion].map({ Data($0.utf8) }) {
            try invalid.write(to: file)
            let store = ListenNoteStore(directory: directory)
            assertWritesRefused(store)
            let preserved = try Data(contentsOf: file)
            XCTAssertEqual(preserved, invalid)
            XCTAssertEqual(SHA256.hash(data: preserved), SHA256.hash(data: invalid))
        }
    }

    func testExternalCorruptionLocksWritesUntilAValidFileIsReadAgain() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent(ListenNoteStore.fileName)
        let store = ListenNoteStore(directory: directory)
        XCTAssertFalse(store.isReadOnly)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        XCTAssertTrue(store.saveDraft(episodeId: "fixture", text: "original", tags: []))
        let healthy = try Data(contentsOf: file)
        let bad = Data("corrupt replacement".utf8)
        try bad.write(to: file)
        XCTAssertFalse(store.clear(episodeId: "fixture"))
        assertWritesRefused(store)
        XCTAssertEqual(try Data(contentsOf: file), bad)
        XCTAssertEqual(store.note(for: "fixture")?.body, "original")

        try FileManager.default.removeItem(at: file)
        store.reload()
        assertWritesRefused(store)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))

        try healthy.write(to: file)
        store.reload()
        XCTAssertFalse(store.isReadOnly)
        XCTAssertTrue(store.saveDraft(episodeId: "second-fixture", text: "new", tags: []))
        let reopened = ListenNoteStore(directory: directory)
        XCTAssertEqual(reopened.note(for: "fixture")?.body, "original")
        XCTAssertEqual(reopened.note(for: "second-fixture")?.body, "new")
    }

    func testUnreadableExistingFileRemainsByteIdentical() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let fm = FileManager.default
        let file = directory.appendingPathComponent(ListenNoteStore.fileName)
        let store = ListenNoteStore(directory: directory)
        XCTAssertTrue(store.saveDraft(episodeId: "fixture", text: "original", tags: []))
        let original = try Data(contentsOf: file)
        try fm.setAttributes([.posixPermissions: 0], ofItemAtPath: file.path)
        defer { try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path) }
        guard (try? Data(contentsOf: file)) == nil else { throw XCTSkip("Process can read files with mode 000") }

        let unreadable = ListenNoteStore(directory: directory)
        assertWritesRefused(unreadable)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        let preserved = try Data(contentsOf: file)
        XCTAssertEqual(preserved, original)
        XCTAssertEqual(SHA256.hash(data: preserved), SHA256.hash(data: original))
        unreadable.reload()
        XCTAssertFalse(unreadable.isReadOnly)
        XCTAssertEqual(unreadable.note(for: "fixture")?.body, "original")

        try fm.setAttributes([.posixPermissions: 0o500], ofItemAtPath: directory.path)
        defer { try? fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path) }
        XCTAssertFalse(unreadable.saveDraft(episodeId: "fixture", text: "new", tags: []))
        assertWritesRefused(unreadable)
        XCTAssertEqual(try Data(contentsOf: file), original)
    }

    private func assertWritesRefused(_ store: ListenNoteStore, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(store.isReadOnly, file: file, line: line)
        XCTAssertFalse(store.saveDraft(episodeId: "fixture", text: "new", tags: []), file: file, line: line)
        XCTAssertFalse(store.upsert(episodeId: "fixture", headline: "", body: "new", tags: []), file: file, line: line)
        XCTAssertFalse(store.upsert(episodeId: "fixture", headline: "", body: "", tags: []), file: file, line: line)
        XCTAssertFalse(store.clear(episodeId: "fixture"), file: file, line: line)
        store.dismissPrompt(episodeId: "fixture")
        XCTAssertFalse(store.isPromptDismissed(episodeId: "fixture"), file: file, line: line)
    }

    private func makeDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("listen-note-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        return directory
    }
}
