#if DEBUG
import Foundation

/// 仅供测试的合成双语原文对照夹具：写入临时目录，不污染真实资料库。
struct DebugBilingualFixtureRoute: Hashable {
    static let flag = "-OpenSyntheticBilingualFixture"
    static let episodeID = "synthetic-bilingual-peek-fixture"
    static let firstOriginal = "First synthetic English original paragraph for peek verification."
    static let secondOriginal = "Second synthetic English paragraph via text field."
    static let viewpointID = "vp-bilingual-1"

    let episode: Episode

    private init(episode: Episode) {
        self.episode = episode
    }

    static func requested(arguments: [String] = ProcessInfo.processInfo.arguments) -> Bool {
        arguments.contains(flag)
    }

    static func resolve(
        arguments: [String] = ProcessInfo.processInfo.arguments,
        directory: URL = defaultDirectory
    ) -> Self? {
        guard requested(arguments: arguments),
              let episode = try? prepareEpisode(in: directory),
              episode.isPlayableNow else {
            return nil
        }
        return Self(episode: episode)
    }

    static var defaultDirectory: URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("shengjian-debug-bilingual-fixture", isDirectory: true)
    }

    static func prepareEpisode(in directory: URL) throws -> Episode {
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        let folder = directory.standardizedFileURL.resolvingSymlinksInPath()

        let audioURL = folder.appendingPathComponent("output.wav")
        let wavData = makeSilentWAV(durationSeconds: 6)
        try wavData.write(to: audioURL, options: .atomic)
        let audioSHA = LibrarySnapshot.digest(wavData)

        let transcriptObject: [String: Any] = [
            "audio_sha256": audioSHA,
            "segments": [
                [
                    "id": "seg-1",
                    "t_start": 0.0,
                    "t_end": 3.0,
                    "zh": "这是合成双语夹具的第一段中文译稿。",
                    "en": firstOriginal,
                    "text": firstOriginal,
                ],
                [
                    "id": "seg-2",
                    "t_start": 3.0,
                    "t_end": 6.0,
                    "zh": "第二段用 text 字段承载英文原文。",
                    "text": secondOriginal,
                ],
            ],
        ]
        let transcriptData = try JSONSerialization.data(
            withJSONObject: transcriptObject,
            options: [.prettyPrinted, .sortedKeys]
        )
        try transcriptData.write(to: folder.appendingPathComponent("transcript_zh.json"), options: .atomic)
        let transcriptHash = LibrarySnapshot.digest(transcriptData)

        let packetID = "synthetic-bilingual-packet-1"
        let extractObject: [String: Any] = [
            "packet_id": packetID,
            "work_id": episodeID,
            "schema_id": "general",
            "mode": "deep",
            "status": "ready",
            "created_at": "2026-11-24T00:00:00Z",
            "summary_line": "合成双语测试要点",
            "transcript_hash": transcriptHash,
            "claims": [
                [
                    "id": "claim-bilingual-1",
                    "text": "合成双语测试要点一",
                    "t_start": 0.5,
                    "t_end": 2.5,
                    "status": "candidate",
                    "point_type": "claim_bilingual_fixture",
                ],
            ],
            "methods": [],
            "entities": [],
            "quotes": [],
            "open_questions": [],
        ]
        let extractData = try JSONSerialization.data(
            withJSONObject: extractObject,
            options: [.prettyPrinted, .sortedKeys]
        )
        try extractData.write(to: folder.appendingPathComponent("extract.json"), options: .atomic)

        let pointIndexObject: [String: Any] = [
            "work_id": episodeID,
            "packet_id": packetID,
            "transcript_hash": transcriptHash,
            "audio_sha256": audioSHA,
            "timeline": "output.mp3.seconds",
            "viewpoints": [
                [
                    "id": viewpointID,
                    "viewpoint_text": "合成双语测试要点一",
                    "t_start": 0.5,
                    "t_end": 2.5,
                    "index_include": true,
                ],
            ],
        ]
        let pointIndexData = try JSONSerialization.data(
            withJSONObject: pointIndexObject,
            options: [.prettyPrinted, .sortedKeys]
        )
        try pointIndexData.write(to: folder.appendingPathComponent(PointListening.indexFileName), options: .atomic)

        return Episode(
            id: episodeID,
            title: "合成双语夹具",
            guests: [],
            lensIds: [],
            createdAt: .distantPast,
            durationSec: 6,
            audioURL: audioURL,
            downloading: false
        )
    }

    private static func makeSilentWAV(durationSeconds: Int, sampleRate: UInt32 = 8000) -> Data {
        let channels: UInt16 = 1
        let bitsPerSample: UInt16 = 16
        let bytesPerSample = UInt32(bitsPerSample / 8)
        let dataSize = sampleRate * UInt32(channels) * bytesPerSample * UInt32(max(1, durationSeconds))
        let byteRate = sampleRate * UInt32(channels) * bytesPerSample
        let blockAlign = channels * (bitsPerSample / 8)
        var data = Data(capacity: 44 + Int(dataSize))

        func appendUInt16(_ value: UInt16) {
            var le = value.littleEndian
            withUnsafeBytes(of: &le) { data.append(contentsOf: $0) }
        }
        func appendUInt32(_ value: UInt32) {
            var le = value.littleEndian
            withUnsafeBytes(of: &le) { data.append(contentsOf: $0) }
        }

        data.append(contentsOf: "RIFF".utf8)
        appendUInt32(36 + dataSize)
        data.append(contentsOf: "WAVE".utf8)
        data.append(contentsOf: "fmt ".utf8)
        appendUInt32(16)
        appendUInt16(1)
        appendUInt16(channels)
        appendUInt32(sampleRate)
        appendUInt32(byteRate)
        appendUInt16(blockAlign)
        appendUInt16(bitsPerSample)
        data.append(contentsOf: "data".utf8)
        appendUInt32(dataSize)
        data.append(Data(count: Int(dataSize)))
        return data
    }
}
#endif
