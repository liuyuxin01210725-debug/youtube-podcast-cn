import Foundation
@main struct BookImportProbe {
    @MainActor static func main() async throws {
        let source = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let docs = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        try FileManager.default.createDirectory(at: docs, withIntermediateDirectories: true)
        let legacy = docs.appendingPathComponent("listen_notes.json")
        let old = Data("legacy podcast note must survive".utf8); try old.write(to: legacy)
        let store = BookStore(documents: docs)
        await store.importPackage(source)
        precondition(store.errorText == nil, store.errorText ?? "")
        precondition(store.editions.count == 1)
        let e = store.editions[0]
        store.saveNote(bookID: e.manifest.bookId, revision: e.manifest.packageRevision, chapterID: nil, text: "Book-only note")
        let legacyCopy1 = try Data(contentsOf: legacy)
        precondition(legacyCopy1 == old)
        let reloaded = BookStore(documents: docs)
        precondition(reloaded.note(bookID: e.manifest.bookId, revision: e.manifest.packageRevision, chapterID: nil)?.body == "Book-only note")
        let legacyCopy2 = try Data(contentsOf: legacy)
        precondition(legacyCopy2 == old)
        precondition(!BookFiles.safeID("../escape"))
        do { _ = try BookFiles.url("../listen_notes.json", in: source); preconditionFailure("unsafe path accepted") } catch { }
        await store.importPackage(source)
        precondition(store.editions.count == 1)
        precondition(!BookFiles.revision(String(repeating: "a", count: 64) + "\n"))
        if CommandLine.arguments.count > 3 {
            let replacement = URL(fileURLWithPath: CommandLine.arguments[3], isDirectory: true)
            let before = try Data(contentsOf: docs.appendingPathComponent("book_notes.v1.json"))
            await store.importPackage(replacement)
            precondition(store.errorText == nil, store.errorText ?? "")
            precondition(store.editions.count == 2)
            precondition(store.state.selected[e.manifest.bookId] == e.manifest.packageRevision)
            let after = try Data(contentsOf: docs.appendingPathComponent("book_notes.v1.json"))
            precondition(before == after)
            if let c=e.manifest.chapters.first, c.audio != nil { _ = try store.audioURL(edition: e, chapter: c) }
            print("Book revision probe PASS: new parser edition imported without selecting it, changing old notes or losing old audio")
        }
        print("BookImportProbe PASS: manifest/assets import, restart and same-revision idempotence, legacy note bytes unchanged, isolated book note")
    }
}
