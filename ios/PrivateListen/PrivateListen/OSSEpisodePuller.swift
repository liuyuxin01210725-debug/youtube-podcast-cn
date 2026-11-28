import Foundation

struct OSSPulledEpisodePackage: Sendable {
    let id: String
    let revision: String
    let stagingFolder: URL
    let fileHashes: [String: String]
    let sanitizedRowData: Data
    let cleanupRoot: URL
}

final class OSSEpisodePuller: @unchecked Sendable {
    static let requiredSidecars = [
        "document.json",
        "source-identity.json",
        "render-identity.json",
        "episode.json",
    ]
    static let optionalSidecars = [
        "extract.json",
        "point-listen.json",
    ]

    let maxEpisodesPerActivation: Int
    let successCooldown: TimeInterval
    let failureCooldown: TimeInterval
    let client: OSSLibraryClient

    private let now: @Sendable () -> Date
    private let lock = NSLock()
    private var nextAllowedAttemptAt: Date?
    private var lockedOutFingerprint: String?

    init(
        client: OSSLibraryClient = OSSLibraryClient(),
        maxEpisodesPerActivation: Int = 3,
        successCooldown: TimeInterval = 60,
        failureCooldown: TimeInterval = 300,
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.client = client
        self.maxEpisodesPerActivation = max(1, maxEpisodesPerActivation)
        self.successCooldown = successCooldown
        self.failureCooldown = failureCooldown
        self.now = now
    }

    func shouldAttemptNow(configFingerprint: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if let locked = lockedOutFingerprint {
            if locked == configFingerprint {
                return false
            }
            lockedOutFingerprint = nil
            nextAllowedAttemptAt = nil
        }
        if let next = nextAllowedAttemptAt, now() < next {
            return false
        }
        return true
    }

    func recordSuccess() {
        lock.lock()
        lockedOutFingerprint = nil
        nextAllowedAttemptAt = now().addingTimeInterval(successCooldown)
        lock.unlock()
    }

    func recordFailure(_ error: Error, configFingerprint: String) {
        lock.lock()
        if case OSSClientError.unauthorized = error {
            lockedOutFingerprint = configFingerprint
            nextAllowedAttemptAt = nil
        } else {
            nextAllowedAttemptAt = now().addingTimeInterval(failureCooldown)
        }
        lock.unlock()
    }

    func fetchCatalog(baseURL: URL, token: String) async throws -> [OSSRemoteCatalogItem] {
        try await client.fetchCatalog(baseURL: baseURL, token: token)
    }

    func pullEpisode(
        item: OSSRemoteCatalogItem,
        baseURL: URL,
        token: String,
        expectedAudioSHA256: String? = nil
    ) async throws -> OSSPulledEpisodePackage {
        let fm = FileManager.default
        let cleanupRoot = fm.temporaryDirectory.appendingPathComponent("oss-pull-\(UUID().uuidString)", isDirectory: true)
        let workDir = cleanupRoot.appendingPathComponent("work", isDirectory: true)
        try fm.createDirectory(at: workDir, withIntermediateDirectories: true)

        var handoffSucceeded = false
        defer {
            if !handoffSucceeded {
                try? fm.removeItem(at: cleanupRoot)
            }
        }

        // 1. Always download and verify output.mp3 first.
        let audioDest = workDir.appendingPathComponent("output.mp3")
        let audioSignedURL = try await client.fetchSignedURL(
            baseURL: baseURL,
            token: token,
            workId: item.id,
            name: "output.mp3"
        )
        try await client.downloadFile(from: audioSignedURL, to: audioDest, expectAudio: true)
        let audioDigest = try RevisionAssetVerifier.streamDigestAndSize(of: audioDest).digest

        if let expectedAudioSHA256, audioDigest != expectedAudioSHA256 {
            throw LibrarySnapshotError.incompleteRevision
        }

        var fileHashes: [String: String] = ["output.mp3": audioDigest]
        var finalRowData = item.sanitizedRowData

        if let declaredFiles = item.declaredRevisionFiles {
            guard declaredFiles["output.mp3"] == audioDigest else {
                throw LibrarySnapshotError.incompleteRevision
            }
            for name in declaredFiles.keys.sorted() where name != "output.mp3" {
                try Task.checkCancellation()
                guard let expectedHash = declaredFiles[name],
                      OSSLibraryClient.allowedArtifactNames.contains(name) else {
                    throw LibrarySnapshotError.incompleteRevision
                }
                let dest = workDir.appendingPathComponent(name)
                let signedURL = try await client.fetchSignedURL(
                    baseURL: baseURL,
                    token: token,
                    workId: item.id,
                    name: name
                )
                try await client.downloadFile(from: signedURL, to: dest, expectAudio: false)
                let computed = try RevisionAssetVerifier.streamDigestAndSize(of: dest).digest
                guard computed == expectedHash else {
                    throw LibrarySnapshotError.incompleteRevision
                }
                fileHashes[name] = computed
            }
        } else {
            // Legacy remote row: best-effort sidecars, fallback {} for missing required JSONs.
            for name in Self.requiredSidecars {
                try Task.checkCancellation()
                let dest = workDir.appendingPathComponent(name)
                if await tryBestEffortDownload(baseURL: baseURL, token: token, workId: item.id, name: name, to: dest) {
                    fileHashes[name] = try RevisionAssetVerifier.streamDigestAndSize(of: dest).digest
                } else {
                    let fallback = Data("{}".utf8)
                    try fallback.write(to: dest, options: .atomic)
                    fileHashes[name] = LibrarySnapshot.digest(fallback)
                }
            }

            for name in Self.optionalSidecars {
                try Task.checkCancellation()
                let dest = workDir.appendingPathComponent(name)
                if await tryBestEffortDownload(baseURL: baseURL, token: token, workId: item.id, name: name, to: dest) {
                    fileHashes[name] = try RevisionAssetVerifier.streamDigestAndSize(of: dest).digest
                }
            }

            if let artwork = item.declaredArtwork {
                try Task.checkCancellation()
                let dest = workDir.appendingPathComponent(artwork.name)
                if await tryBestEffortDownload(baseURL: baseURL, token: token, workId: item.id, name: artwork.name, to: dest),
                   LocalAudio.isPlayable(dest) {
                    fileHashes[artwork.name] = try RevisionAssetVerifier.streamDigestAndSize(of: dest).digest
                } else {
                    try? fm.removeItem(at: dest)
                    finalRowData = try Self.removingArtworkFields(from: finalRowData)
                }
            }
        }

        let entries = fileHashes.sorted { $0.key < $1.key }.map { "  \"\($0.key)\": \"\($0.value)\"" }
        let manifest = "{\n" + entries.joined(separator: ",\n") + "\n}\n"
        let revision = LibrarySnapshot.digest(Data(manifest.utf8))

        if let declaredSHA = item.declaredRevisionSHA256, declaredSHA != revision {
            throw LibrarySnapshotError.incompleteRevision
        }

        let stagingFolder = cleanupRoot
            .appendingPathComponent("objects", isDirectory: true)
            .appendingPathComponent(revision, isDirectory: true)
        try fm.createDirectory(at: stagingFolder.deletingLastPathComponent(), withIntermediateDirectories: true)
        try fm.moveItem(at: workDir, to: stagingFolder)

        handoffSucceeded = true
        return OSSPulledEpisodePackage(
            id: item.id,
            revision: revision,
            stagingFolder: stagingFolder,
            fileHashes: fileHashes,
            sanitizedRowData: finalRowData,
            cleanupRoot: cleanupRoot
        )
    }

    private func tryBestEffortDownload(
        baseURL: URL,
        token: String,
        workId: String,
        name: String,
        to destination: URL
    ) async -> Bool {
        do {
            let signedURL = try await client.fetchSignedURL(
                baseURL: baseURL,
                token: token,
                workId: workId,
                name: name
            )
            try await client.downloadFile(from: signedURL, to: destination, expectAudio: false)
            return true
        } catch {
            return false
        }
    }

    private static func removingArtworkFields(from rowData: Data) throws -> Data {
        guard var row = try JSONSerialization.jsonObject(with: rowData) as? [String: Any] else {
            throw LibrarySnapshotError.invalidCatalog
        }
        row.removeValue(forKey: "artwork")
        row.removeValue(forKey: "artwork_kind")
        return try LibrarySnapshot.json(row)
    }
}
