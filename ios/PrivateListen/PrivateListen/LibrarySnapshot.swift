import CryptoKit
import Foundation

/// The catalog owns membership and metadata. The revision manifest binds its
/// media and sidecars to one immutable directory before the catalog is exposed.
struct LibrarySnapshot {
    let records: [EpisodeRecord]
    let object: [String: Any]
    let rows: [[String: Any]]

    init(_ data: Data) throws {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rows = object["episodes"] as? [[String: Any]] else {
            throw LibrarySnapshotError.invalidCatalog
        }
        let records = try JSONDecoder.privateListen().decode(EpisodeLibrary.self, from: data).episodes
        var seen: Set<String> = []
        for (record, row) in zip(records, rows) {
            guard let id = record.catalogID, EpisodeID.isSafe(id), seen.insert(id).inserted,
                  record.file == nil || record.file == "output.mp3",
                  (row["media_revision"] == nil ? record.mediaRevision == nil : record.mediaRevision?.isValid(for: id) == true),
                  record.mediaRevision == nil || record.pendingReference(for: id) == nil else {
                throw LibrarySnapshotError.invalidCatalog
            }
        }
        self.records = records
        self.object = object
        self.rows = rows
    }

    func preservingExtensions(from current: LibrarySnapshot?) throws -> Data {
        var merged = current?.object ?? [:]
        merged.merge(object) { _, incoming in incoming }
        let priorRows = Dictionary(uniqueKeysWithValues: zip(current?.records ?? [], current?.rows ?? []).compactMap {
            (record, row) in record.catalogID.map { ($0, row) }
        })
        // Known publisher fields may be intentionally removed, including old
        // playback marks and artwork. Opaque fields travel with their row.
        let publisherFields: Set<String> = [
            "id", "episode_id", "source_job_id", "title", "created_at", "duration_sec", "file",
            "youtube_id", "artwork", "artwork_kind", "media_revision", "resume_seconds",
            "resume_sec", "position_seconds", "playback_progress", "language", "audio_kind",
            "transcript_status", "status", "transcript", "manifest", "pending_units",
        ]
        merged["episodes"] = zip(records, rows).map { record, row in
            let inherited = record.catalogID.flatMap { priorRows[$0] } ?? [:]
            return inherited.filter { !publisherFields.contains($0.key) }.merging(row) { _, incoming in incoming }
        }
        return try Self.json(merged)
    }

    func canonicalData() throws -> Data { try Self.json(object) }

    static func json(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    func bodyDigest() throws -> String {
        var body = object
        body.removeValue(forKey: "transactions")
        return Self.digest(try Self.json(body))
    }

    static func digest(_ bytes: Data) -> String { hex(SHA256.hash(data: bytes)) }

    static func hex(_ digest: SHA256Digest) -> String {
        digest.map { String(format: "%02x", $0) }.joined()
    }

    static func path(_ relative: String, in root: URL, directory: Bool = false) throws -> URL {
        let parts = relative.split(separator: "/", omittingEmptySubsequences: false)
        guard !parts.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) else {
            throw LibrarySnapshotError.invalidPath
        }
        let resolvedRoot = root.standardizedFileURL.resolvingSymlinksInPath()
        let url = resolvedRoot.appendingPathComponent(relative, isDirectory: directory)
        // standardizedFileURL drops a trailing slash that appendingPathComponent
        // may add for directories; still refuse a symlink that changes the location.
        let constructed = url.standardizedFileURL
        let resolved = url.resolvingSymlinksInPath().standardizedFileURL
        guard resolved.path == constructed.path,
              (try? url.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) != true else {
            throw LibrarySnapshotError.invalidPath
        }
        return url
    }
}

private struct EpisodeLibrary: Decodable {
    let episodes: [EpisodeRecord]
}

struct MediaRevision: Decodable {
    let sha256: String
    let path: String
    let previousPath: String
    let resumePolicy: String
    let files: [String: String]

    func isValid(for id: String) -> Bool {
        let objectPath = "objects/\(sha256)"
        let prefix = "revisions/\(id)/"
        let priorRevision = String(previousPath.dropFirst(prefix.count))
        let priorObject = String(previousPath.dropFirst("objects/".count))
        let previousOK = previousPath != path && (
            previousPath == "episodes/\(id)"
            || (previousPath.hasPrefix(prefix) && previousPath == prefix + priorRevision && EpisodeID.isRevisionDigest(priorRevision))
            || (previousPath.hasPrefix("objects/") && previousPath == "objects/\(priorObject)" && EpisodeID.isRevisionDigest(priorObject))
            || (previousPath.isEmpty && path == objectPath)
        )
        guard EpisodeID.isRevisionDigest(sha256), (path == prefix + sha256 || path == objectPath),
              previousOK,
              resumePolicy == "reset_unmapped",
              Set(["output.mp3", "document.json", "source-identity.json", "render-identity.json", "episode.json"])
                .isSubset(of: files.keys),
              files.allSatisfy({ name, hash in
                  !name.hasPrefix(".") && !name.isEmpty &&
                  name.utf8.allSatisfy { (45...46).contains($0) || (48...57).contains($0)
                      || (65...90).contains($0) || $0 == 95 || (97...122).contains($0) } &&
                  EpisodeID.isRevisionDigest(hash)
              }) else { return false }

        // Matches the frozen publisher's sorted manifest and json_bytes.
        let entries = files.sorted { $0.key < $1.key }.map { "  \"\($0.key)\": \"\($0.value)\"" }
        let manifest = "{\n" + entries.joined(separator: ",\n") + "\n}\n"
        return LibrarySnapshot.digest(Data(manifest.utf8)) == sha256
    }

    @MainActor
    func validateAssets(in folder: URL, downloading: Bool, verifier: RevisionAssetVerifier) throws {
        if downloading {
            try? FileManager.default.startDownloadingUbiquitousItem(at: folder)
        }
        let assets = try files.map { name, digest in (try LibrarySnapshot.path(name, in: folder), digest) }
        if downloading {
            for (asset, _) in assets where !LocalAudio.isPlayable(asset) {
                try? FileManager.default.startDownloadingUbiquitousItem(at: asset)
            }
        }
        for (asset, digest) in assets {
            try verifier.validate(asset, digest: digest)
        }
    }
}

enum LibrarySnapshotError: Error, Equatable {
    case invalidCatalog
    case invalidPath
    case incompleteRevision
    case staleCatalog
    case rejectedIngest
    case transactionConflict
}

struct CatalogTransaction: Equatable {
    var requestDigest: String
    var generation: Int
    var resultDigest: String

    init(requestDigest: String, generation: Int, resultDigest: String) {
        self.requestDigest = requestDigest
        self.generation = generation
        self.resultDigest = resultDigest
    }

    init?(object: Any) {
        guard let row = object as? [String: Any],
              let request = row["request_digest"] as? String, !request.isEmpty,
              let generation = row["generation"] as? Int, generation > 0,
              let result = row["result_digest"] as? String, EpisodeID.isRevisionDigest(result) else {
            return nil
        }
        requestDigest = request
        self.generation = generation
        resultDigest = result
    }

    var json: [String: Any] {
        ["request_digest": requestDigest, "generation": generation, "result_digest": resultDigest]
    }
}

enum IngestFault: String {
    case none
    case afterReceive
    case afterVerify
    case midInstall
    case beforeCommit
    case afterCommitBeforeReceipt
    case diskFull
}

struct IngestReceipt: Equatable {
    var txId: String
    var requestDigest: String
    var generation: Int
    var resultDigest: String
    var rebuilt: Bool
}

enum PushStaging {
    static let schema = "private-listen.library.v2"
    static let stagingName = ".push-staging"
    static let privateName = ".ingest-private"
    static let manifestName = "manifest.json"
    static let readyName = "ready"
    static let receiptName = "receipt.json"

    static func requestDigest(txId: String, packages: [[String: Any]]) throws -> String {
        let body: [String: Any] = ["tx_id": txId, "packages": packages]
        let data = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
        return LibrarySnapshot.digest(data)
    }
}

extension LibrarySnapshot {
    var generation: Int {
        object["generation"] as? Int ?? 0
    }

    var parentDigest: String? {
        object["parent"] as? String
    }

    var transactions: [String: CatalogTransaction] {
        let raw = object["transactions"] as? [String: Any] ?? [:]
        return raw.reduce(into: [:]) { result, item in
            if let parsed = CatalogTransaction(object: item.value) {
                result[item.key] = parsed
            }
        }
    }

    func stamped(generation: Int, parent: String?, transactions: [String: CatalogTransaction]) throws -> Data {
        var merged = object
        merged["schema"] = PushStaging.schema
        merged["generation"] = generation
        if let parent, !parent.isEmpty {
            merged["parent"] = parent
        } else {
            merged.removeValue(forKey: "parent")
        }
        merged["transactions"] = Dictionary(uniqueKeysWithValues: transactions.map { ($0.key, $0.value.json) })
        return try Self.json(merged)
    }
}

@MainActor
final class RevisionAssetVerifier {
    private struct Stamp: Equatable {
        let digest: String
        let inode: NSNumber
        let size: NSNumber
        let modified: Date
    }

    private var verified: [URL: Stamp] = [:]

    func validate(_ url: URL, digest: String) throws {
        guard let before = stamp(url, digest: digest) else { throw LibrarySnapshotError.incompleteRevision }
        if verified[url] == before { return }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            hash.update(data: chunk)
        }
        guard LibrarySnapshot.hex(hash.finalize()) == digest,
              stamp(url, digest: digest) == before else {
            throw LibrarySnapshotError.incompleteRevision
        }
        verified[url] = before
    }

    private func stamp(_ url: URL, digest: String) -> Stamp? {
        let fresh = URL(fileURLWithPath: url.path)
        guard LocalAudio.isPlayable(fresh),
              let attributes = try? FileManager.default.attributesOfItem(atPath: fresh.path),
              attributes[.type] as? FileAttributeType == .typeRegular,
              let inode = attributes[.systemFileNumber] as? NSNumber,
              let size = attributes[.size] as? NSNumber,
              let modified = attributes[.modificationDate] as? Date else { return nil }
        return Stamp(digest: digest, inode: inode, size: size, modified: modified)
    }
}
