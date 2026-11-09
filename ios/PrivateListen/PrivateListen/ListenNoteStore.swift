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

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private var fileURL: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(Self.fileName)
    }

    init() {
        reload()
    }

    func reload() {
        guard let data = try? Data(contentsOf: fileURL),
              let doc = try? JSONDecoder().decode(FileDoc.self, from: data) else {
            notes = [:]
            promptDismissed = [:]
            return
        }
        notes = doc.notes
        promptDismissed = doc.promptDismissed
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

    /// 空内容则删除条目；否则 upsert。永不触发深抽/ListenMark。
    func upsert(episodeId: String, headline: String, body: String, tags: [String]) {
        let h = ListenNote.trimmed(headline)
        let b = ListenNote.trimmed(body)
        let t = ListenNote.normalizeTags(tags)
        if h.isEmpty && b.isEmpty && t.isEmpty {
            if notes.removeValue(forKey: episodeId) != nil {
                persist()
            }
            return
        }
        let now = Self.iso.string(from: Date())
        notes[episodeId] = ListenNote(
            episodeId: episodeId,
            headline: h,
            body: b,
            tags: t,
            updatedAt: now
        )
        persist()
    }

    func clear(episodeId: String) {
        guard notes.removeValue(forKey: episodeId) != nil else { return }
        persist()
    }

    func isPromptDismissed(episodeId: String) -> Bool {
        promptDismissed[episodeId] != nil
    }

    func dismissPrompt(episodeId: String) {
        promptDismissed[episodeId] = Self.iso.string(from: Date())
        persist()
        UIEventLog.note("listen_note_prompt_dismiss", episodeId: episodeId)
    }

    /// 是否应展示听完轻提示（一次机会：finished + 无感 + 未 dismiss）。
    func shouldShowFinishedPrompt(episodeId: String, isFinished: Bool) -> Bool {
        guard isFinished else { return false }
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

    private func persist() {
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
        guard let data = try? encoder.encode(doc) else { return }
        let dir = fileURL.deletingLastPathComponent()
        let temporary = dir.appendingPathComponent(
            ".listen_notes.json.\(ProcessInfo.processInfo.processIdentifier).tmp"
        )
        do {
            try data.write(to: temporary, options: .atomic)
            _ = try? FileManager.default.removeItem(at: fileURL)
            try FileManager.default.moveItem(at: temporary, to: fileURL)
        } catch {
            try? FileManager.default.removeItem(at: temporary)
        }
    }
}
