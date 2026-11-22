import Foundation
import Combine

/// 私人听后感：本期「对我意味着什么」。
/// ⊥ ListenMark ⊥ extract.* ⊥ listen_behavior ⊥ starred/deep_request。
/// Local-first；不上云混训；不因感自动入队/深抽/建桥。

struct ListenNote: Codable, Hashable, Identifiable {
    var episodeId: String
    var headline: String
    var body: String
    var tags: [String]
    var updatedAt: String

    var id: String { episodeId }

    enum CodingKeys: String, CodingKey {
        case episodeId = "episode_id"
        case headline, body, tags
        case updatedAt = "updated_at"
    }

    /// 列表/空态：无实质内容则视为「无听后感」。
    var hasContent: Bool {
        !Self.trimmed(headline).isEmpty
            || !Self.trimmed(body).isEmpty
            || !tags.filter { !Self.trimmed($0).isEmpty }.isEmpty
    }

    /// 库列表灰字：优先短标题，否则正文首行（截断）。
    var listSummary: String? {
        let h = Self.trimmed(headline)
        if !h.isEmpty { return h }
        let first = Self.trimmed(body)
            .split(whereSeparator: \.isNewline)
            .map(String.init)
            .first(where: { !Self.trimmed($0).isEmpty })
        guard let line = first.map(Self.trimmed), !line.isEmpty else { return nil }
        if line.count <= 36 { return line }
        let idx = line.index(line.startIndex, offsetBy: 36)
        return String(line[..<idx]) + "…"
    }

    static func trimmed(_ s: String) -> String {
        s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func normalizeTags(_ raw: [String]) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for t in raw {
            let v = trimmed(t)
            guard !v.isEmpty else { continue }
            let key = v.lowercased()
            if seen.insert(key).inserted {
                out.append(v)
            }
        }
        return out
    }
}

@MainActor
final class ListenNoteStore: ObservableObject {
    static let fileName = "listen_notes.json"
    static let schema = "shengjian.listen_notes.v1"
    static let schemaVersion = 1
    static let shareExcludedFileNames: Set<String> = [fileName, "listen-note-probe.json", ListenNoteDownstream.insightsFileName]

    struct FileDoc: Codable {
        var schema: String
        var schemaVersion: Int
        var updatedAt: String
        var notes: [String: ListenNote]
        /// episode_id → ISO8601 dismiss time（听完轻提示，与内容分离）
        var promptDismissed: [String: String]

        enum CodingKeys: String, CodingKey {
            case schema
            case schemaVersion = "schema_version"
            case updatedAt = "updated_at"
            case notes
            case promptDismissed = "prompt_dismissed"
        }
    }

    @Published private(set) var notes: [String: ListenNote] = [:]
    @Published private(set) var promptDismissed: [String: String] = [:]
    @Published private(set) var isReadOnly = false

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private let fileURL: URL
    private var loadedBytes: Data?

    init(directory: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]) {
        fileURL = directory.appendingPathComponent(Self.fileName)
        reload()
    }

    func reload() {
        do {
            guard let data = try readExistingFile() else {
                // Once a file was present or failed to load, absence is not recovery.
                if loadedBytes != nil { isReadOnly = true }
                return
            }
            let doc = try JSONDecoder().decode(FileDoc.self, from: data)
            guard doc.schema == Self.schema, doc.schemaVersion == Self.schemaVersion else {
                isReadOnly = true
                return
            }
            notes = doc.notes
            promptDismissed = doc.promptDismissed
            loadedBytes = data
            isReadOnly = false
        } catch {
            isReadOnly = true
        }
    }

    private func readExistingFile() throws -> Data? {
        let attributes: [FileAttributeKey: Any]
        do {
            attributes = try FileManager.default.attributesOfItem(atPath: fileURL.path)
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile || error.code == .fileNoSuchFile {
            return nil
        }
        guard attributes[.type] as? FileAttributeType == .typeRegular else {
            throw CocoaError(.fileReadUnknown)
        }
        return try Data(contentsOf: fileURL)
    }

    func note(for episodeId: String) -> ListenNote? {
        notes[episodeId]
    }

    func listSummary(for episodeId: String) -> String? {
        notes[episodeId]?.listSummary
    }

    func matchesSearch(episodeId: String, query: String) -> Bool {
        let q = ListenNote.trimmed(query)
        guard !q.isEmpty, let n = notes[episodeId] else { return false }
        if n.headline.localizedCaseInsensitiveContains(q) { return true }
        if n.body.localizedCaseInsensitiveContains(q) { return true }
        return n.tags.contains { $0.localizedCaseInsensitiveContains(q) }
    }

    /// The editor shows legacy headline + body in one field. Opening and closing
    /// an unchanged draft must preserve the original record, including its timestamp.
    func saveDraft(episodeId: String, text: String, tags: [String]) -> Bool {
        guard !isReadOnly else { return false }
        let body = ListenNote.trimmed(text)
        let tags = ListenNote.normalizeTags(tags)
        if let note = notes[episodeId],
           body == ListenNoteDownstream.mergedDisplayText(headline: note.headline, body: note.body),
           tags == note.tags {
            return true
        }
        return upsert(episodeId: episodeId, headline: ListenNoteDownstream.firstLine(body), body: body, tags: tags)
    }

    /// 空内容则删除条目；否则 upsert。永不触发深抽/ListenMark。
    @discardableResult
    func upsert(episodeId: String, headline: String, body: String, tags: [String]) -> Bool {
        let h = ListenNote.trimmed(headline)
        let b = ListenNote.trimmed(body)
        let t = ListenNote.normalizeTags(tags)
        if h.isEmpty && b.isEmpty && t.isEmpty {
            return clear(episodeId: episodeId)
        }
        let now = Self.iso.string(from: Date())
        var updated = notes
        updated[episodeId] = ListenNote(
            episodeId: episodeId,
            headline: h,
            body: b,
            tags: t,
            updatedAt: now
        )
        guard persist(notes: updated, promptDismissed: promptDismissed) else { return false }
        notes = updated
        return true
    }

    @discardableResult
    func clear(episodeId: String) -> Bool {
        guard !isReadOnly else { return false }
        var updated = notes
        guard updated.removeValue(forKey: episodeId) != nil else { return true }
        guard persist(notes: updated, promptDismissed: promptDismissed) else { return false }
        notes = updated
        return true
    }

    func isPromptDismissed(episodeId: String) -> Bool {
        promptDismissed[episodeId] != nil
    }

    func dismissPrompt(episodeId: String) {
        var updated = promptDismissed
        updated[episodeId] = Self.iso.string(from: Date())
        guard persist(notes: notes, promptDismissed: updated) else { return }
        promptDismissed = updated
        UIEventLog.note("listen_note_prompt_dismiss", episodeId: episodeId)
    }

    /// 是否应展示听完轻提示（一次机会：finished + 无感 + 未 dismiss）。
    func shouldShowFinishedPrompt(episodeId: String, isFinished: Bool) -> Bool {
        guard isFinished, !isReadOnly else { return false }
        if isPromptDismissed(episodeId: episodeId) { return false }
        if let n = notes[episodeId], n.hasContent { return false }
        return true
    }

    func markPromptShown(episodeId: String) {
        UIEventLog.note("listen_note_prompt_shown", episodeId: episodeId)
    }

    static func isShareExcluded(_ name: String) -> Bool {
        shareExcludedFileNames.contains(name)
    }

    private func persist(notes: [String: ListenNote], promptDismissed: [String: String]) -> Bool {
        guard !isReadOnly else { return false }
        // Refuse writes against a file that changed since the last successful read.
        do {
            guard try readExistingFile() == loadedBytes else {
                isReadOnly = true
                return false
            }
        } catch {
            isReadOnly = true
            return false
        }
        let now = Self.iso.string(from: Date())
        let doc = FileDoc(
            schema: Self.schema,
            schemaVersion: Self.schemaVersion,
            updatedAt: now,
            notes: notes,
            promptDismissed: promptDismissed
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        do {
            let data = try encoder.encode(doc)
            try data.write(to: fileURL, options: .atomic)
            loadedBytes = data
            return true
        } catch let error as CocoaError where error.code == .fileWriteNoPermission || error.code == .fileWriteVolumeReadOnly {
            isReadOnly = true
            return false
        } catch {
            return false
        }
    }
}
