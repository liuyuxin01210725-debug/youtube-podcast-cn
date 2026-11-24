import Combine
import Foundation

struct ListenAlongNote: Codable, Hashable, Identifiable {
    var id: String
    var mediaKey: String
    var episodeId: String
    var positionSec: Double
    var text: String
    var createdAt: String

    enum CodingKeys: String, CodingKey {
        case id
        case mediaKey = "media_key"
        case episodeId = "episode_id"
        case positionSec = "position_sec"
        case text
        case createdAt = "created_at"
    }
}

@MainActor
final class ListenAlongNoteStore: ObservableObject {
    static let fileName = "listen_along_notes.json"
    static let schema = "shengjian.listen_along_notes.v1"
    static let schemaVersion = 1

    struct FileDoc: Codable {
        var schema: String
        var schemaVersion: Int
        var updatedAt: String
        var notes: [ListenAlongNote]

        enum CodingKeys: String, CodingKey {
            case schema
            case schemaVersion = "schema_version"
            case updatedAt = "updated_at"
            case notes
        }
    }

    @Published private(set) var notes: [ListenAlongNote] = []
    @Published private(set) var isReadOnly = false

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let knownDocumentKeys: Set<String> = [
        "schema", "schema_version", "updated_at", "notes",
    ]
    private static let knownNoteKeys: Set<String> = [
        "id", "media_key", "episode_id", "position_sec", "text", "created_at",
    ]

    private let fileURL: URL
    private var loadedBytes: Data?
    private var preservedTopLevelExtensions: [String: Any] = [:]
    private var preservedNoteExtensions: [String: [String: Any]] = [:]

    init(directory: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]) {
        fileURL = directory.appendingPathComponent(Self.fileName)
        reload()
    }

    func reload() {
        do {
            guard let data = try readExistingFile() else {
                if loadedBytes != nil { isReadOnly = true }
                return
            }
            let doc = try JSONDecoder().decode(FileDoc.self, from: data)
            guard doc.schema == Self.schema, doc.schemaVersion == Self.schemaVersion,
                  let rawObject = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let rawNotes = rawObject["notes"] as? [[String: Any]] else {
                isReadOnly = true
                return
            }
            var topExtensions: [String: Any] = [:]
            for (key, value) in rawObject where !Self.knownDocumentKeys.contains(key) {
                topExtensions[key] = value
            }
            var noteExtensions: [String: [String: Any]] = [:]
            for rawNote in rawNotes {
                guard let id = rawNote["id"] as? String else { continue }
                let extras = rawNote.filter { !Self.knownNoteKeys.contains($0.key) }
                if !extras.isEmpty {
                    noteExtensions[id] = extras
                }
            }
            notes = doc.notes
            preservedTopLevelExtensions = topExtensions
            preservedNoteExtensions = noteExtensions
            loadedBytes = data
            isReadOnly = false
        } catch {
            isReadOnly = true
        }
    }

    func notes(for mediaKey: String, legacyEpisodeId: String? = nil) -> [ListenAlongNote] {
        let exact = notes.filter { $0.mediaKey == mediaKey }
        if !exact.isEmpty {
            return Self.sorted(exact)
        }
        // legacy 回退仅适用于同一 episodeId 对应的裸 id 旧记录，防止跨集或跨修订串集。
        if let legacy = legacyEpisodeId, !legacy.isEmpty, legacy != mediaKey,
           mediaKey.hasPrefix(legacy + ".") {
            let legacyExact = notes.filter {
                ($0.mediaKey == legacy || $0.mediaKey.isEmpty) && $0.episodeId == legacy
            }
            if !legacyExact.isEmpty {
                return Self.sorted(legacyExact)
            }
        }
        return []
    }

    @discardableResult
    func add(mediaKey: String, episodeId: String, position: Double, text: String) -> ListenAlongNote? {
        guard !isReadOnly else { return nil }
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return nil }
        let clamped = position.isFinite ? max(0, position) : 0
        let note = ListenAlongNote(
            id: UUID().uuidString,
            mediaKey: mediaKey,
            episodeId: episodeId,
            positionSec: clamped,
            text: body,
            createdAt: Self.iso.string(from: Date())
        )
        var updated = notes
        updated.append(note)
        guard persist(notes: updated) else { return nil }
        notes = updated
        return note
    }

    @discardableResult
    func delete(id: String) -> Bool {
        guard !isReadOnly else { return false }
        var updated = notes
        let beforeCount = updated.count
        updated.removeAll { $0.id == id }
        guard updated.count != beforeCount else { return true }
        guard persist(notes: updated) else { return false }
        preservedNoteExtensions.removeValue(forKey: id)
        notes = updated
        return true
    }

    private static func sorted(_ items: [ListenAlongNote]) -> [ListenAlongNote] {
        items.sorted { lhs, rhs in
            if lhs.positionSec != rhs.positionSec { return lhs.positionSec < rhs.positionSec }
            if lhs.createdAt != rhs.createdAt { return lhs.createdAt < rhs.createdAt }
            return lhs.id < rhs.id
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

    private func persist(notes: [ListenAlongNote]) -> Bool {
        guard !isReadOnly else { return false }
        do {
            guard try readExistingFile() == loadedBytes else {
                isReadOnly = true
                return false
            }
        } catch {
            isReadOnly = true
            return false
        }
        var merged = preservedTopLevelExtensions.filter { !Self.knownDocumentKeys.contains($0.key) }
        merged["schema"] = Self.schema
        merged["schema_version"] = Self.schemaVersion
        merged["updated_at"] = Self.iso.string(from: Date())
        merged["notes"] = notes.map { note -> [String: Any] in
            var item = (preservedNoteExtensions[note.id] ?? [:]).filter { !Self.knownNoteKeys.contains($0.key) }
            item["id"] = note.id
            item["media_key"] = note.mediaKey
            item["episode_id"] = note.episodeId
            item["position_sec"] = note.positionSec.isFinite ? max(0, note.positionSec) : 0
            item["text"] = note.text
            item["created_at"] = note.createdAt
            return item
        }
        do {
            let data = try JSONSerialization.data(withJSONObject: merged, options: [.prettyPrinted, .sortedKeys])
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
