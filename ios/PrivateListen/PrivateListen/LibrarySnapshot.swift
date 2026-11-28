import CryptoKit
import CoreFoundation
import Foundation

/// The catalog owns membership and metadata. The revision manifest binds its
/// media and sidecars to one immutable directory before the catalog is exposed.
struct LibrarySnapshot {
    let records: [EpisodeRecord]
    let object: [String: Any]
    let rows: [[String: Any]]
    let invalidRevisionIDs: Set<String>

    init(_ data: Data, allowInvalidRevisions: Bool = false) throws {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rows = object["episodes"] as? [[String: Any]] else {
            throw LibrarySnapshotError.invalidCatalog
        }
        if object["schema"] != nil || object["generation"] != nil {
            guard object["schema"] as? String == PushStaging.schema,
                  let number = object["generation"] as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID(),
                  let generation = number as? Int, generation > 0 else {
                throw LibrarySnapshotError.invalidCatalog
            }
        }
        let decoder = JSONDecoder.privateListen()
        var records: [EpisodeRecord]
        do {
            records = try decoder.decode(EpisodeLibrary.self, from: data).episodes
        } catch where allowInvalidRevisions {
            // Decode the catalog metadata separately so an ill-typed revision
            // field can disable its own episode without hiding the shelf.
            records = try rows.map { row in
                var fields = row
                let raw = fields.removeValue(forKey: "media_revision")
                var record = try decoder.decode(EpisodeRecord.self, from: Self.json(fields))
                if let raw, let revision = raw as? [String: Any] {
                    record.mediaRevision = try? decoder.decode(MediaRevision.self, from: Self.json(revision))
                }
                return record
            }
        }
        var seen: Set<String> = []
        var invalidRevisionIDs: Set<String> = []
        for (record, row) in zip(records, rows) {
            guard let id = record.catalogID, EpisodeID.isSafe(id), seen.insert(id).inserted,
                  record.file == nil || record.file == "output.mp3" else {
                throw LibrarySnapshotError.invalidCatalog
            }
            if row["media_revision"] != nil,
               record.mediaRevision?.isValid(for: id) != true || record.pendingReference(for: id) != nil {
                guard allowInvalidRevisions else { throw LibrarySnapshotError.invalidCatalog }
                invalidRevisionIDs.insert(id)
            }
        }
        self.records = records
        self.object = object
        self.rows = rows
        self.invalidRevisionIDs = invalidRevisionIDs
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
    static let requiredFiles: Set<String> = [
        "output.mp3", "document.json", "source-identity.json", "render-identity.json", "episode.json"
    ]
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
              Self.requiredFiles.isSubset(of: files.keys),
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

    func verifiedFiles(
        in folder: URL,
        downloading: Bool,
        verifier: RevisionAssetVerifier,
        allowMissing: Bool,
        mode: AssetVerificationMode = .full,
        checkCancellation: Bool = false
    ) throws -> Set<String> {
        if downloading {
            try? FileManager.default.startDownloadingUbiquitousItem(at: folder)
        }
        var verified = Set<String>()
        for (name, digest) in files {
            if checkCancellation { try Task.checkCancellation() }
            do {
                let asset = try LibrarySnapshot.path(name, in: folder)
                if downloading && !LocalAudio.isPlayable(asset) {
                    try? FileManager.default.startDownloadingUbiquitousItem(at: asset)
                }
                switch mode {
                case .lightweight:
                    switch verifier.lightweightStatus(asset, digest: digest) {
                    case .verified, .unverifiedCandidate:
                        verified.insert(name)
                    case .failed:
                        if !allowMissing {
                            throw LibrarySnapshotError.incompleteRevision
                        }
                    }
                case .full:
                    try verifier.validate(asset, digest: digest, checkCancellation: checkCancellation)
                    verified.insert(name)
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch where allowMissing {
                continue
            }
        }
        return verified
    }
}

enum AssetVerificationMode {
    case lightweight
    case full
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

final class RevisionAssetVerifier: @unchecked Sendable {
    enum Verdict {
        case verified
        case failed
    }

    enum LightweightStatus {
        case verified
        case unverifiedCandidate
        case failed
    }

    struct FileFingerprint: Equatable, Sendable {
        let inode: UInt64
        let size: UInt64
        let modified: TimeInterval
    }

    private struct Stamp: Equatable {
        let digest: String
        let inode: NSNumber
        let size: NSNumber
        let modified: Date
    }

    private struct CachedEntry {
        let stamp: Stamp
        let verdict: Verdict
    }

    private let lock = NSLock()
    private var entries: [URL: CachedEntry] = [:]

    #if DEBUG || os(macOS)
    private var _hashComputeCount = 0
    private var _hashedOnMainThread = false

    var hashComputeCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return _hashComputeCount
    }

    var hashedOnMainThread: Bool {
        lock.lock()
        defer { lock.unlock() }
        return _hashedOnMainThread
    }

    func resetMetrics() {
        lock.lock()
        _hashComputeCount = 0
        _hashedOnMainThread = false
        lock.unlock()
    }
    #endif

    static func streamDigestAndSize(
        of url: URL,
        chunkSize: Int = 1_048_576,
        checkCancellation: Bool = false
    ) throws -> (digest: String, byteCount: Int) {
        if checkCancellation { try Task.checkCancellation() }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        var byteCount = 0
        while true {
            if checkCancellation { try Task.checkCancellation() }
            let count: Int = try autoreleasepool {
                guard let chunk = try handle.read(upToCount: chunkSize), !chunk.isEmpty else {
                    return 0
                }
                hash.update(data: chunk)
                return chunk.count
            }
            if count == 0 { break }
            byteCount += count
        }
        return (LibrarySnapshot.hex(hash.finalize()), byteCount)
    }

    func lightweightStatus(_ url: URL, digest: String) -> LightweightStatus {
        guard let before = stamp(url, digest: digest) else { return .failed }
        let key = cacheKey(for: url)
        lock.lock()
        let cached = entries[key]
        lock.unlock()
        if let cached, cached.stamp == before {
            switch cached.verdict {
            case .verified: return .verified
            case .failed: return .failed
            }
        }
        return .unverifiedCandidate
    }

    func validate(_ url: URL, digest: String, checkCancellation: Bool = false) throws {
        if checkCancellation { try Task.checkCancellation() }
        guard let before = stamp(url, digest: digest) else {
            throw LibrarySnapshotError.incompleteRevision
        }
        let key = cacheKey(for: url)
        lock.lock()
        let cached = entries[key]
        lock.unlock()
        if let cached, cached.stamp == before {
            switch cached.verdict {
            case .verified: return
            case .failed: throw LibrarySnapshotError.incompleteRevision
            }
        }
        #if DEBUG || os(macOS)
        let isMain = Thread.isMainThread
        lock.lock()
        _hashComputeCount += 1
        if isMain { _hashedOnMainThread = true }
        lock.unlock()
        #endif
        let computed: (digest: String, byteCount: Int)
        do {
            computed = try Self.streamDigestAndSize(of: url, checkCancellation: checkCancellation)
        } catch is CancellationError {
            throw CancellationError()
        }
        let passed = computed.digest == digest && stamp(url, digest: digest) == before
        lock.lock()
        entries[key] = CachedEntry(stamp: before, verdict: passed ? .verified : .failed)
        lock.unlock()
        if !passed {
            throw LibrarySnapshotError.incompleteRevision
        }
    }

    private func cacheKey(for url: URL) -> URL {
        URL(fileURLWithPath: url.path).standardizedFileURL
    }

    func fingerprint(of url: URL) -> FileFingerprint? {
        let fresh = URL(fileURLWithPath: url.path)
        guard LocalAudio.isPlayable(fresh),
              let attributes = try? FileManager.default.attributesOfItem(atPath: fresh.path),
              attributes[.type] as? FileAttributeType == .typeRegular,
              let inode = attributes[.systemFileNumber] as? NSNumber,
              let size = attributes[.size] as? NSNumber,
              let modified = attributes[.modificationDate] as? Date else { return nil }
        return FileFingerprint(
            inode: inode.uint64Value,
            size: size.uint64Value,
            modified: modified.timeIntervalSinceReferenceDate
        )
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
