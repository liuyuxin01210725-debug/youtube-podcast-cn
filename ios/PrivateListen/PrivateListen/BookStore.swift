import Foundation
import Combine
import CryptoKit

struct BookAsset: Codable { var sha256: String; var bytes: Int }
struct BookAudio: Codable {
    var path: String; var sha256: String; var durationSeconds: Double; var audioRevision: String
}
struct BookChapter: Codable, Identifiable {
    var id: String; var title: String; var order: Int; var sourceBlockIds: [String]
    var sourceText: String; var chineseText: String; var readingText: String
    var translationReview: String; var audio: BookAudio?
}
struct BookUnresolved: Codable { var kind: String; var href: String?; var reason: String?; var sourceBlockId: String? }
struct BookScope: Codable { var kind: String; var expectedBlockIds: [String]; var unresolved: [BookUnresolved] }
struct BookManifest: Codable {
    var schemaVersion: Int; var contentType: String; var bookId: String; var packageRevision: String
    var title: String; var sourceRevision: String; var extractionRevision: String
    var translationRevision: String; var readingRevision: String; var audioRevision: String
    var scope: BookScope; var gates: [String: String]; var chapters: [BookChapter]
    var assets: [String: BookAsset]; var wholeBookVerified: Bool
}
struct BookEdition: Identifiable {
    let manifest: BookManifest; let root: URL
    var id: String { manifest.bookId + "|" + manifest.packageRevision }
    func reference(_ chapter: BookChapter) -> BookPlaybackReference? {
        guard let audio = chapter.audio else { return nil }
        return BookPlaybackReference(bookID: manifest.bookId, packageRevision: manifest.packageRevision, chapterID: chapter.id, audioRevision: audio.audioRevision)
    }
}
struct BookRoute: Hashable { var bookID: String; var revision: String; var chapterID: String? = nil; var play = false }
struct BookNote: Codable, Identifiable {
    var id: String; var bookID: String; var packageRevision: String; var chapterID: String?
    var body: String; var updatedAt: TimeInterval
}
struct BookLocalState: Codable {
    var schemaVersion = 1
    var selected: [String: String] = [:]
    var marks: [String: BookMark] = [:]
    var dismissed: Set<String> = []
}
struct BookLocalNotes: Codable { var schemaVersion = 1; var notes: [String: BookNote] = [:] }
struct BookFailure: LocalizedError { var message: String; var errorDescription: String? { message } }

enum BookFiles {
    static func safeID(_ value: String) -> Bool { (1...80).contains(value.count) && value.unicodeScalars.allSatisfy { CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-").contains($0) } }
    static func revision(_ value: String) -> Bool { value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) } }
    static func url(_ name: String, in root: URL) throws -> URL {
        guard !name.isEmpty, !name.hasPrefix("/"), !name.contains("\\"), !name.contains(":"), !name.components(separatedBy: "/").contains(where: { $0 == ".." || $0.isEmpty }) else { throw BookFailure(message: "书包路径不安全") }
        let resolved = root.appendingPathComponent(name).standardizedFileURL.resolvingSymlinksInPath()
        guard resolved.path.hasPrefix(root.standardizedFileURL.resolvingSymlinksInPath().path + "/") else { throw BookFailure(message: "书包路径越界") }
        return resolved
    }
    static func hash(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
        var h = SHA256()
        while let data = try handle.read(upToCount: 1024 * 1024), !data.isEmpty { h.update(data: data) }
        return h.finalize().map { String(format: "%02x", $0) }.joined()
    }
    static func manifest(_ root: URL) throws -> BookManifest {
        let data = try Data(contentsOf: url("manifest.json", in: root))
        guard data.count <= 16 * 1024 * 1024 else { throw BookFailure(message: "书包目录过大") }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let m = try decoder.decode(BookManifest.self, from: data)
        guard m.schemaVersion == 1, m.contentType == "book", safeID(m.bookId), revision(m.packageRevision), !m.chapters.isEmpty,
              m.chapters.count <= 2048, m.assets.count <= 4096,
              Set(m.chapters.map(\.id)).count == m.chapters.count,
              !m.scope.expectedBlockIds.isEmpty, Set(m.scope.expectedBlockIds).count == m.scope.expectedBlockIds.count, !m.assets.keys.contains("manifest.json"),
              Set(m.chapters.flatMap(\.sourceBlockIds)) == Set(m.scope.expectedBlockIds),
              m.chapters.flatMap(\.sourceBlockIds).count == m.scope.expectedBlockIds.count else { throw BookFailure(message: "不支持或不完整的书包契约") }
        if m.gates["source"] == "pass" && !m.scope.unresolved.isEmpty { throw BookFailure(message: "源清单与覆盖状态矛盾") }
        if m.wholeBookVerified && (m.gates["source"] != "pass" || m.gates["translation"] != "pass" || m.gates["audio"] != "pass" || m.assets["listening-review.json"] == nil || m.assets["source-review.json"] == nil || m.assets["review.json"] == nil) { throw BookFailure(message: "完整译听缺少独立三闸证据") }
        for c in m.chapters {
            guard safeID(c.id) else { throw BookFailure(message: "书章身份无效") }
            if let a = c.audio {
                guard c.translationReview == "pass", !c.chineseText.isEmpty, revision(a.audioRevision), a.durationSeconds.isFinite, a.durationSeconds > 0,
                      m.assets[a.path]?.sha256 == a.sha256 else { throw BookFailure(message: "章音频缺少译文审核或版本绑定") }
            }
        }
        for name in m.assets.keys { _ = try url(name, in: root) }
        return m
    }
    static func verifyAssets(_ m: BookManifest, root: URL) throws {
        var total: Int = 0
        for (name, asset) in m.assets {
            guard asset.bytes >= 0, asset.bytes <= 512 * 1024 * 1024, revision(asset.sha256) else { throw BookFailure(message: "书包资产大小/摘要无效") }
            total += asset.bytes
            guard total <= 2 * 1024 * 1024 * 1024 else { throw BookFailure(message: "书包超出本地导入上限") }
            let u = try url(name, in: root)
            guard (try u.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])).isRegularFile == true,
                  (try u.resourceValues(forKeys: [.fileSizeKey])).fileSize == asset.bytes,
                  try hash(u) == asset.sha256 else { throw BookFailure(message: "书包校验失败；旧版与笔记保持不变") }
        }
    }
}

@MainActor final class BookStore: ObservableObject {
    @Published private(set) var editions: [BookEdition] = []
    @Published private(set) var state = BookLocalState()
    @Published private(set) var notes = BookLocalNotes()
    @Published var errorText: String?
    @Published var importing = false
    let documents: URL
    var root: URL { documents.appendingPathComponent("Books", isDirectory: true) }
    private var stateReadable = true
    private var notesReadable = true
    init(documents: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]) {
        self.documents = documents
        do {
            if FileManager.default.fileExists(atPath: stateURL.path) { state = try JSONDecoder().decode(BookLocalState.self, from: Data(contentsOf: stateURL)); stateReadable = state.schemaVersion == 1 }
            if FileManager.default.fileExists(atPath: notesURL.path) { notes = try JSONDecoder().decode(BookLocalNotes.self, from: Data(contentsOf: notesURL)); notesReadable = notes.schemaVersion == 1 }
        } catch { stateReadable = false; notesReadable = false; errorText = "书籍状态未能读取；原文件保留，暂停写入" }
        reload()
    }
    private var stateURL: URL { documents.appendingPathComponent("book_state.v1.json") }
    private var notesURL: URL { documents.appendingPathComponent("book_notes.v1.json") }
    private func saveState() {
        guard stateReadable else { errorText = "书籍状态版本不支持，未覆盖原文件"; return }
        do { try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true); try JSONEncoder().encode(state).write(to: stateURL, options: .atomic) } catch { errorText = error.localizedDescription }
    }
    func reload() {
        let fm = FileManager.default
        let books = (try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil, options: .skipsHiddenFiles)) ?? []
        var found: [BookEdition] = []
        for book in books {
            for version in (try? fm.contentsOfDirectory(at: book, includingPropertiesForKeys: nil, options: .skipsHiddenFiles)) ?? [] {
                if let m = try? BookFiles.manifest(version), m.bookId == book.lastPathComponent, m.packageRevision == version.lastPathComponent { found.append(BookEdition(manifest: m, root: version)) }
            }
        }
        editions = found.sorted { $0.id < $1.id }
    }
    var selectedEditions: [BookEdition] {
        Dictionary(grouping: editions, by: { $0.manifest.bookId }).keys.sorted().compactMap { id in
            editions.first { $0.manifest.bookId == id && $0.manifest.packageRevision == state.selected[id] } ?? editions.first { $0.manifest.bookId == id }
        }
    }
    func edition(bookID: String, revision: String) -> BookEdition? { editions.first { $0.manifest.bookId == bookID && $0.manifest.packageRevision == revision } }
    func select(_ edition: BookEdition) { state.selected[edition.manifest.bookId] = edition.manifest.packageRevision; saveState() }
    func importPackage(_ source: URL) async {
        importing = true; defer { importing = false }
        let access = source.startAccessingSecurityScopedResource(); defer { if access { source.stopAccessingSecurityScopedResource() } }
        do {
            let m = try BookFiles.manifest(source); let destination = root.appendingPathComponent(m.bookId).appendingPathComponent(m.packageRevision)
            // Hash/copy away from the UI actor; only the package allowlist is copied.
            try await Task.detached(priority: .userInitiated) {
                try BookFiles.verifyAssets(m, root: source)
                let fm = FileManager.default
                if fm.fileExists(atPath: destination.path) {
                    guard try Data(contentsOf: BookFiles.url("manifest.json", in: destination)) == Data(contentsOf: BookFiles.url("manifest.json", in: source)) else { throw BookFailure(message: "同一书包修订的目录发生冲突；原版与笔记未覆盖") }
                    try BookFiles.verifyAssets(try BookFiles.manifest(destination), root: destination); return
                }
                let staging = destination.deletingLastPathComponent().appendingPathComponent(".incoming-" + UUID().uuidString)
                try fm.createDirectory(at: staging, withIntermediateDirectories: true)
                for name in ["manifest.json"] + m.assets.keys.sorted() {
                    let from = try BookFiles.url(name, in: source); let to = try BookFiles.url(name, in: staging)
                    try fm.createDirectory(at: to.deletingLastPathComponent(), withIntermediateDirectories: true)
                    try fm.copyItem(at: from, to: to)
                }
                try BookFiles.verifyAssets(try BookFiles.manifest(staging), root: staging)
                try fm.moveItem(at: staging, to: destination)
            }.value
            // New versions do not silently replace an already selected revision.
            if state.selected[m.bookId] == nil { state.selected[m.bookId] = m.packageRevision; saveState() }
            reload()
        } catch { errorText = error.localizedDescription }
    }
    func audioURL(edition: BookEdition, chapter: BookChapter) throws -> URL {
        guard let a = chapter.audio else { throw BookFailure(message: "该章尚无已审核音频") }
        let u = try BookFiles.url(a.path, in: edition.root)
        guard try BookFiles.hash(u) == a.sha256 else { throw BookFailure(message: "音轨缺失或版本失配；保留旧位置，不自动迁移秒数") }
        return u
    }
    func remember(_ ref: BookPlaybackReference, seconds: Double, duration: Double, playedDelta: Double, ended: Bool) {
        guard let e = edition(bookID: ref.bookID, revision: ref.packageRevision), let c = e.manifest.chapters.first(where: { $0.id == ref.chapterID }), e.reference(c) == ref else { return }
        let old = state.marks[ref.key] ?? BookMark(reference: ref, bookTitle: e.manifest.title, chapterTitle: c.title)
        let next = BookProgressPolicy.record(old, seconds: seconds, duration: duration, playedDelta: playedDelta, ended: ended, at: Date().timeIntervalSince1970)
        guard next != old else { return }
        state.marks[ref.key] = next
        if playedDelta > 0 { state.dismissed.remove(ref.key) }
        saveState()
    }
    var continueMark: BookMark? {
        state.marks.values.filter { !$0.finished && $0.lastActuallyListenedAt > 0 && !state.dismissed.contains($0.reference.key) }.sorted { $0.lastActuallyListenedAt > $1.lastActuallyListenedAt }.first
    }
    func dismissContinue(_ mark: BookMark) { state.dismissed.insert(mark.reference.key); saveState() }
    func resume(_ ref: BookPlaybackReference) -> Double { guard let m = state.marks[ref.key], !m.finished else { return 0 }; return m.seconds }
    func heard(_ edition: BookEdition) -> Bool {
        !edition.manifest.chapters.isEmpty && edition.manifest.chapters.allSatisfy { c in guard let ref = edition.reference(c) else { return false }; return state.marks[ref.key]?.finished == true }
    }
    func note(bookID: String, revision: String, chapterID: String?) -> BookNote? { notes.notes.values.first { $0.bookID == bookID && $0.packageRevision == revision && $0.chapterID == chapterID } }
    func saveNote(bookID: String, revision: String, chapterID: String?, text: String) {
        guard notesReadable else { errorText = "书籍笔记版本不支持，原文件未改"; return }
        let key = [bookID, revision, chapterID ?? "book"].joined(separator: "|")
        var next = notes; next.notes[key] = BookNote(id: key, bookID: bookID, packageRevision: revision, chapterID: chapterID, body: text, updatedAt: Date().timeIntervalSince1970)
        do { try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true); try JSONEncoder().encode(next).write(to: notesURL, options: .atomic); notes = next } catch { errorText = error.localizedDescription }
    }
}
