import CryptoKit
import Foundation

struct PendingTranscript: Decodable, Sendable {
    struct Segment: Decodable, Identifiable, Sendable {
        let id: Int
        let start: Double
        let end: Double
        let text: String
        let status: String
    }

    let segments: [Segment]
    var start: Double { segments.map(\.start).min() ?? 0 }
    var end: Double { segments.map(\.end).max() ?? 0 }

    private let schemaVersion: Int
    private let episodeId: String
    private let language: String
    private let kind: String
    private let status: String
    private let timeline: String
    private let sourceAudioSha256: String
    private let playbackAudioSha256: String
    private let rawAsrSha256: String

    enum ReadFailure: Error, Equatable {
        case unavailable
        case invalidBinding
    }

    /// The URLs come from the library's selected root. The reader only opens the
    /// fixed V2 filenames, and releases the text only after checking the bindings.
    static func read(
        id: String, audioURL: URL, reference: PendingTranscriptReference
    ) -> Result<PendingTranscript, ReadFailure> {
        do {
            return .success(try verify(id: id, audioURL: audioURL, reference: reference))
        } catch let error as ReadFailure {
            return .failure(error)
        } catch {
            return .failure(.unavailable)
        }
    }

    private static func verify(
        id: String, audioURL: URL, reference: PendingTranscriptReference
    ) throws -> PendingTranscript {
        let folder = audioURL.deletingLastPathComponent()
        guard case let .package(packageID, duration, count) = reference,
              EpisodeID.isSafe(id), id == packageID, folder.lastPathComponent == id,
              audioURL.isFileURL, audioURL.lastPathComponent == "output.mp3" else {
            throw ReadFailure.invalidBinding
        }

        let decoder = JSONDecoder.privateListen()
        let manifestData = try sidecar("manifest.json", in: folder)
        guard let manifest = try? decoder.decode(Manifest.self, from: manifestData),
              manifest.schemaVersion == 1, manifest.episodeId == id, manifest.status == "pending_review",
              manifest.asr.status == "asr_unreviewed", manifest.asr.language == "zh",
              manifest.asr.segmentCount == count, !manifest.asr.listeningVerified,
              !manifest.asr.completenessVerified, manifest.media.playback.codec == "mp3" else {
            throw ReadFailure.invalidBinding
        }

        let recordData = try sidecar("episode.json", in: folder)
        let transcriptData = try sidecar("transcript.pending.json", in: folder)
        guard hash(recordData) == manifest.sha256["episode.json"],
              hash(transcriptData) == manifest.sha256["transcript.pending.json"],
              let record = try? decoder.decode(EpisodeRecord.self, from: recordData),
              record.pendingReference(for: id) == reference,
              let transcript = try? decoder.decode(Self.self, from: transcriptData),
              transcript.schemaVersion == 1, transcript.episodeId == id, transcript.language == "zh",
              transcript.kind == "original_audio_asr", transcript.status == "asr_unreviewed",
              transcript.timeline == "output.mp3.seconds", transcript.segments.count == count,
              manifest.derivation.output == "output.mp3",
              manifest.derivation.outputSha256 == transcript.playbackAudioSha256,
              manifest.sha256["output.mp3"] == transcript.playbackAudioSha256,
              manifest.derivation.inputSha256 == transcript.sourceAudioSha256,
              manifest.sha256[manifest.derivation.input] == transcript.sourceAudioSha256,
              isDigest(transcript.sourceAudioSha256), isDigest(transcript.rawAsrSha256),
              manifest.sha256.contains(where: { name, digest in
                  name.hasSuffix(".json") && name != "episode.json" && name != "transcript.pending.json"
                      && digest == transcript.rawAsrSha256
              }),
              transcript.hasValidSegments(through: min(duration, manifest.media.playback.decodedDurationS)) else {
            throw ReadFailure.invalidBinding
        }

        let actualAudioHash = try readFile(audioURL) { url in
            let before = try FileManager.default.attributesOfItem(atPath: url.path)
            let file = try FileHandle(forReadingFrom: url)
            defer { try? file.close() }
            var digest = SHA256()
            while let chunk = try file.read(upToCount: 1_048_576), !chunk.isEmpty {
                try Task.checkCancellation()
                digest.update(data: chunk)
            }
            let after = try FileManager.default.attributesOfItem(atPath: url.path)
            guard [FileAttributeKey.size, .modificationDate, .systemFileNumber].allSatisfy({
                (before[$0] as? NSObject) == (after[$0] as? NSObject)
            }) else { throw ReadFailure.invalidBinding }
            return hex(digest.finalize())
        }
        guard actualAudioHash == transcript.playbackAudioSha256 else { throw ReadFailure.invalidBinding }
        return transcript
    }

    private func hasValidSegments(through duration: Double) -> Bool {
        var ids: Set<Int> = []
        var previousStart = 0.0
        return segments.allSatisfy { segment in
            guard segment.status == "pending_review", segment.id >= 0, ids.insert(segment.id).inserted,
                  segment.start >= previousStart, segment.end > segment.start, segment.end <= duration else {
                return false
            }
            previousStart = segment.start
            return true
        }
    }

    private static func sidecar(_ name: String, in folder: URL) throws -> Data {
        try readFile(folder.appendingPathComponent(name)) { try Data(contentsOf: $0) }
    }

    private static func readFile<T>(_ url: URL, _ read: (URL) throws -> T) throws -> T {
        try Task.checkCancellation()
        func readRegularFile(_ fileURL: URL) throws -> T {
            let values = try fileURL.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
            guard values.isRegularFile == true, values.isSymbolicLink == false,
                  let size = values.fileSize, size > 0 else { throw ReadFailure.unavailable }
            return try read(fileURL)
        }
        // Ordinary local copies are read directly. iCloud items require coordination.
        let location = try url.resourceValues(forKeys: [.isUbiquitousItemKey])
        guard location.isUbiquitousItem == true else { return try readRegularFile(url) }
        var coordinationError: NSError?
        var result: Result<T, Error>?
        NSFileCoordinator().coordinate(readingItemAt: url, options: [], error: &coordinationError) { fileURL in
            result = Result { try readRegularFile(fileURL) }
        }
        guard coordinationError == nil, let result else { throw ReadFailure.unavailable }
        return try result.get()
    }

    private static func isDigest(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    private static func hash(_ data: Data) -> String { hex(SHA256.hash(data: data)) }

    private static func hex(_ bytes: SHA256.Digest) -> String {
        bytes.map { String(format: "%02x", $0) }.joined()
    }

    private struct Manifest: Decodable {
        struct ASR: Decodable {
            let status: String
            let language: String
            let segmentCount: Int
            let listeningVerified: Bool
            let completenessVerified: Bool
        }

        struct Media: Decodable {
            struct Playback: Decodable {
                let codec: String
                let decodedDurationS: Double
            }

            let playback: Playback
        }

        struct Derivation: Decodable {
            let input: String
            let output: String
            let inputSha256: String
            let outputSha256: String
        }

        let schemaVersion: Int
        let episodeId: String
        let status: String
        let sha256: [String: String]
        let asr: ASR
        let media: Media
        let derivation: Derivation
    }
}
