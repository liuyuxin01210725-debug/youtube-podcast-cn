import CryptoKit
import Foundation

@main
struct PendingTranscriptProbe {
    private static let id = "synthetic-original"
    private static let draftName = "transcript.pending.json"
    private static let expected = PendingTranscriptReference.package(id: id, duration: 2.9, count: 2)

    @MainActor
    static func main() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let folder = root.appendingPathComponent("episodes/\(id)")
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: root) }

        let row: [String: Any] = [
            "id": id, "title": "合成测试节目", "language": "zh", "audio_kind": "original_audio",
            "transcript_status": "asr_unreviewed", "status": "pending_review",
            "transcript": draftName, "manifest": "manifest.json", "file": "output.mp3",
            "duration_sec": 2.9, "pending_units": 2,
        ]
        let episodeData = try json(row)
        let catalog = root.appendingPathComponent("library.json")
        let audioURL = folder.appendingPathComponent("output.mp3")
        let draftURL = folder.appendingPathComponent(draftName)
        let manifestURL = folder.appendingPathComponent("manifest.json")
        let episodeURL = folder.appendingPathComponent("episode.json")
        // Hash fixture only. The pending reader does not instantiate an audio player.
        let audio = Data([1, 2, 3])
        let audioHash = hash(audio)
        let sourceHash = hash(Data([4]))
        let rawHash = hash(Data([5]))
        let segments: [[String: Any]] = [
            ["id": 0, "start": 0.0, "end": 1.6, "text": "合成甲", "status": "pending_review"],
            // V2 permits overlap. Preserve the final end below the decoded media duration.
            ["id": 1, "start": 1.5, "end": 2.9, "text": "合成乙", "status": "pending_review"],
        ]
        let draft: [String: Any] = [
            "schema_version": 1, "episode_id": id, "language": "zh", "kind": "original_audio_asr",
            "status": "asr_unreviewed", "timeline": "output.mp3.seconds", "source_audio_sha256": sourceHash,
            "playback_audio_sha256": audioHash, "raw_asr_sha256": rawHash, "segments": segments,
        ]
        let asr: [String: Any] = [
            "status": "asr_unreviewed", "language": "zh", "segment_count": 2,
            "listening_verified": false, "completeness_verified": false,
        ]
        let hashes = [
            "episode.json": hash(episodeData), "output.mp3": audioHash, draftName: hash(try json(draft)),
            "synthetic-source.m4a": sourceHash, "synthetic-raw.json": rawHash,
        ]
        let manifest: [String: Any] = [
            "schema_version": 1, "episode_id": id, "status": "pending_review", "asr": asr, "sha256": hashes,
            "media": ["playback": ["codec": "mp3", "decoded_duration_s": 2.903875]],
            "derivation": ["input": "synthetic-source.m4a", "output": "output.mp3",
                           "input_sha256": sourceHash, "output_sha256": audioHash],
        ]
        try json(["episodes": [row]]).write(to: catalog)
        try episodeData.write(to: episodeURL)
        try audio.write(to: audioURL)
        try json(draft).write(to: draftURL)
        try json(manifest).write(to: manifestURL)

        func load() -> Episode? {
            LibraryStore.loadEpisodes(from: root, localCopy: true).episodes.first
        }
        func read(_ reference: PendingTranscriptReference = expected) -> Result<PendingTranscript, PendingTranscript.ReadFailure> {
            PendingTranscript.read(id: id, audioURL: audioURL, reference: reference)
        }
        func refuses(_ result: Result<PendingTranscript, PendingTranscript.ReadFailure>,
                     _ reason: PendingTranscript.ReadFailure) -> Bool {
            if case .failure(reason) = result { return true }
            return false
        }
        func writeDraft(_ object: [String: Any]) throws {
            let data = try json(object)
            var changedHashes = hashes
            changedHashes[draftName] = hash(data)
            var matchingManifest = manifest
            matchingManifest["sha256"] = changedHashes
            try data.write(to: draftURL)
            try json(matchingManifest).write(to: manifestURL)
        }

        let inputs = [catalog, episodeURL, audioURL, draftURL, manifestURL]
        let before = try inputs.map { try Data(contentsOf: $0) }
        check("V2 episode keeps typed pending reference", load()?.pendingTranscript == expected)
        let firstRead = read(load()?.pendingTranscript ?? .invalid)
        guard case .success(let transcript) = firstRead else {
            if case .failure(let reason) = firstRead {
                check("app Swift path reads the bound transcript (\(reason))", false)
            }
            return
        }
        check("app Swift path reads short synthetic text", transcript.segments.map(\.text) == ["合成甲", "合成乙"])
        check("exact time bounds, including overlap and the clipped final end",
              transcript.start == 0 && transcript.end == 2.9 && transcript.segments[1].start == 1.5)
        let after = try inputs.map { try Data(contentsOf: $0) }
        let names = try fm.contentsOfDirectory(atPath: folder.path)
        check("reader leaves bytes and file set intact", before == after && names.count == 4)

        for (url, data) in zip(inputs.dropFirst(), before.dropFirst()) {
            try fm.removeItem(at: url)
            check("missing \(url.lastPathComponent) has a failure state", refuses(read(), .unavailable))
            if url == episodeURL {
                check("catalog keeps pending identity when episode sidecar is absent", load()?.pendingTranscript == expected)
            }
            try data.write(to: url)
        }

        for url in [audioURL, draftURL, episodeURL] {
            let data = try Data(contentsOf: url)
            try (data + Data([0x20])).write(to: url)
            check("changed bytes in \(url.lastPathComponent) are refused", refuses(read(), .invalidBinding))
            try data.write(to: url)
        }

        var changedManifest = manifest
        changedManifest["episode_id"] = "other-episode"
        try json(changedManifest).write(to: manifestURL)
        check("manifest must belong to episode", refuses(read(), .invalidBinding))
        changedManifest = manifest
        changedManifest["asr"] = asr.merging(["listening_verified": true]) { _, new in new }
        try json(changedManifest).write(to: manifestURL)
        check("manifest verification flag must stay false", refuses(read(), .invalidBinding))

        let invalidFieldValues: [(String, Any)] = [
            ("episode_id", "other-episode"), ("schema_version", 2), ("kind", "translated"),
            ("timeline", "source.m4a.seconds"), ("status", "complete"), ("language", "en"),
            ("playback_audio_sha256", rawHash), ("source_audio_sha256", rawHash), ("raw_asr_sha256", sourceHash),
            ("segments", [segments[0]]),
            ("segments", [segments[0], segments[1].merging(["status": "complete"]) { _, new in new }]),
            ("segments", [segments[0], segments[1].merging(["end": 3.0]) { _, new in new }]),
            ("segments", [segments[0], segments[1].merging(["id": 0]) { _, new in new }]),
        ]
        for (field, value) in invalidFieldValues {
            try writeDraft(draft.merging([field: value]) { _, new in new })
            check("updated hash cannot hide invalid \(field)", refuses(read(), .invalidBinding))
        }
        try writeDraft(draft)

        var wrongRow = row
        wrongRow["pending_units"] = 3
        try json(["episodes": [wrongRow]]).write(to: catalog)
        let wrongCount = PendingTranscriptReference.package(id: id, duration: 2.9, count: 3)
        check("catalog reference wins and the package must bind to it",
              load()?.pendingTranscript == wrongCount && refuses(read(wrongCount), .invalidBinding))

        wrongRow = row
        wrongRow["transcript"] = "../transcript.json"
        try json(["episodes": [wrongRow]]).write(to: catalog)
        check("non-contract catalog filename keeps pending failure", load()?.pendingTranscript == .invalid)
        try fm.removeItem(at: catalog)
        check("directory-only input cannot create a catalog row", load() == nil)

        let ordinary: [String: Any] = ["id": id, "title": "普通成片 · 逐段待核", "status": "complete",
                                       "audio_kind": "tts", "transcript_status": "translated"]
        try json(["episodes": [ordinary]]).write(to: catalog)
        check("ordinary catalog row uses no pending reader despite the on-disk sidecar",
              load()?.pendingTranscript == nil && load() != nil)
    }

    private static func check(_ name: String, _ condition: Bool) {
        guard condition else {
            FileHandle.standardError.write(Data("FAIL \(name)\n".utf8))
            exit(1)
        }
        print("PASS \(name)")
    }

    private static func json(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object, options: .sortedKeys)
    }

    private static func hash(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
