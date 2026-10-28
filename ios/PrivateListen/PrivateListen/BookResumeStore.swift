import Foundation

/// Persists per-book resume cursors separately from podcast `ListenMark` seconds.
/// UserDefaults is the always-on store; Documents library `resume.json` is a
/// best-effort mirror when the episode folder is writable (USB copy / sandbox).
@MainActor
final class BookResumeStore: ObservableObject {
    static let defaultsKey = "PrivateListen.bookResume"

    @Published private(set) var cursors: [String: BookResumeCursor] = [:]

    init() {
        reloadFromDefaults()
    }

    func cursor(for bookId: String) -> BookResumeCursor? {
        cursors[bookId]
    }

    func remember(bookId: String, cursor: BookResumeCursor, chapters: [BookChapterRef]) {
        guard EpisodeID.isSafe(bookId) else { return }
        let clamped = BookChapterIndex.clamp(cursor, in: chapters)
        cursors[bookId] = clamped
        persistDefaults()
    }

    /// Merge a TOC's embedded progress with any newer local cursor (local wins if present).
    func resolve(toc: BookTOC) -> BookResumeCursor {
        if let local = cursors[toc.bookId] {
            return BookChapterIndex.clamp(local, in: toc.chapters)
        }
        return BookChapterIndex.clamp(toc.progress, in: toc.chapters)
    }

    /// Best-effort write beside the book folder under Documents/中文播客私人库.
    func mirrorToDocumentsLibrary(bookId: String, cursor: BookResumeCursor) {
        guard EpisodeID.isSafe(bookId) else { return }
        guard let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
            return
        }
        let folder = docs
            .appendingPathComponent("中文播客私人库", isDirectory: true)
            .appendingPathComponent("episodes", isDirectory: true)
            .appendingPathComponent(bookId, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let url = folder.appendingPathComponent("resume.json")
            let data = try JSONEncoder.bookResume.encode(cursor)
            try data.write(to: url, options: [.atomic])
        } catch {
            // Scaffold: mirroring is optional; UserDefaults remains source of truth on device.
        }
    }

    func loadMirrorFromDocumentsLibrary(bookId: String) -> BookResumeCursor? {
        guard EpisodeID.isSafe(bookId) else { return nil }
        guard let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
            return nil
        }
        let url = docs
            .appendingPathComponent("中文播客私人库", isDirectory: true)
            .appendingPathComponent("episodes", isDirectory: true)
            .appendingPathComponent(bookId, isDirectory: true)
            .appendingPathComponent("resume.json")
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder.bookResume.decode(BookResumeCursor.self, from: data)
    }

    private func reloadFromDefaults() {
        guard let data = UserDefaults.standard.data(forKey: Self.defaultsKey),
              let stored = try? JSONDecoder.bookResume.decode([String: BookResumeCursor].self, from: data) else {
            cursors = [:]
            return
        }
        cursors = stored
    }

    private func persistDefaults() {
        if let data = try? JSONEncoder.bookResume.encode(cursors) {
            UserDefaults.standard.set(data, forKey: Self.defaultsKey)
        }
    }
}

private extension JSONEncoder {
    static let bookResume: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }()
}

private extension JSONDecoder {
    static let bookResume: JSONDecoder = {
        JSONDecoder()
    }()
}
