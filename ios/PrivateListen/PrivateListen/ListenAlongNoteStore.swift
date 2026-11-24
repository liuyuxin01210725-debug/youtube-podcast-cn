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

    private let fileURL: URL
    private var loadedBytes: Data?

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
            guard doc.schema == Self.schema, doc.schemaVersion == Self.schemaVersion else {
                isReadOnly = true
                return
            }
            notes = doc.notes
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
        if let legacy = legacyEpisodeId, !legacy.isEmpty, legacy != mediaKey {
            let legacyExact = notes.filter { $0.mediaKey == legacy }
            if !legacyExact.isEmpty {
                return Self.sorted(legacyExact)
            }
            let byEpisode = notes.filter { $0.episodeId == legacy }
            if !byEpisode.isEmpty {
                return Self.sorted(byEpisode)
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
        let doc = FileDoc(
            schema: Self.schema,
            schemaVersion: Self.schemaVersion,
            updatedAt: Self.iso.string(from: Date()),
            notes: notes
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
