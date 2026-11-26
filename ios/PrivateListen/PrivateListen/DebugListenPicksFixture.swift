#if DEBUG
import CryptoKit
import Foundation

/// 仅供测试的精选与随播合成夹具（180s 避开末 15s 完成规则），写入临时目录，不污染真实资料库。
struct DebugListenPicksFixtureRoute: Hashable {
    enum Mode: String {
        case validMultiPick = "-OpenSyntheticListenPicksFixture"
        case fakeTimeline = "-OpenSyntheticFakeTimelineFixture"
        case pendingSample = "-OpenSyntheticPendingSampleFixture"
        case unverifiedAudio = "-OpenSyntheticUnverifiedAudioFixture"
    }

    static let validEpisodeID = "synthetic-listen-picks-180s"
    static let fakeTimelineEpisodeID = "synthetic-fake-timeline-180s"
    static let pendingEpisodeID = "synthetic-pending-sample-180s"
    static let unverifiedEpisodeID = "synthetic-unverified-audio-180s"

    let episode: Episode

    private init(episode: Episode) {
        self.episode = episode
    }

    static func requestedMode(arguments: [String] = ProcessInfo.processInfo.arguments) -> Mode? {
        for mode in [Mode.validMultiPick, .fakeTimeline, .pendingSample, .unverifiedAudio] {
            if arguments.contains(mode.rawValue) { return mode }
        }
        return nil
    }

    static func requested(arguments: [String] = ProcessInfo.processInfo.arguments) -> Bool {
        requestedMode(arguments: arguments) != nil
    }

    static func resolve(
        arguments: [String] = ProcessInfo.processInfo.arguments,
        directory: URL = defaultDirectory
    ) -> Self? {
        guard let mode = requestedMode(arguments: arguments),
              let episode = try? prepareEpisode(mode: mode, in: directory.appendingPathComponent(mode.rawValue, isDirectory: true)),
              episode.isPlayableNow else {
            return nil
        }
        return Self(episode: episode)
    }

    static var defaultDirectory: URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("shengjian-debug-listen-picks-fixture", isDirectory: true)
    }

    static func prepareEpisode(mode: Mode, in directory: URL) throws -> Episode {
        let fm = FileManager.default
        if fm.fileExists(atPath: directory.path) {
            try? fm.removeItem(at: directory)
        }
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        let folder = directory.standardizedFileURL.resolvingSymlinksInPath()

        let audioURL = folder.appendingPathComponent("output.wav")
        let wavData = makeSilentWAV(durationSeconds: 180)
        try wavData.write(to: audioURL, options: .atomic)
        let audioSHA = LibrarySnapshot.digest(wavData)

        switch mode {
        case .unverifiedAudio:
            return Episode(
                id: unverifiedEpisodeID,
                title: "合成缺字段老集（来源待核）",
                show: "合成测试",
                guests: [],
                lensIds: [],
                createdAt: Date(timeIntervalSince1970: 1_764_000_000),
                durationSec: 180,
                audioURL: audioURL,
                downloading: false,
                artworkFile: nil,
                artworkKind: nil,
                pendingTranscript: nil,
                mediaRevision: nil,
                revisionFiles: nil,
                unavailableReason: nil,
                audioKind: nil,
                hasChineseRenderSidecar: false
            )

        case .pendingSample:
            return Episode(
                id: pendingEpisodeID,
                title: "合成待核听样节目",
                show: "合成测试",
                guests: [],
                lensIds: [],
                createdAt: Date(timeIntervalSince1970: 1_764_000_000),
                durationSec: 180,
                audioURL: audioURL,
                downloading: false,
                artworkFile: nil,
                artworkKind: nil,
                pendingTranscript: .package(id: pendingEpisodeID, duration: 180, count: 4),
                mediaRevision: nil,
                revisionFiles: nil,
                unavailableReason: nil,
                audioKind: "original_audio",
                hasChineseRenderSidecar: false
            )

        case .validMultiPick, .fakeTimeline:
            let epID = mode == .validMultiPick ? validEpisodeID : fakeTimelineEpisodeID
            var segments: [[String: Any]] = []
            for idx in 1...32 {
                let start = Double(idx - 1) * 5.0
                let end = Double(idx) * 5.0
                segments.append([
                    "id": "seg-\(idx)",
                    "t_start": start,
                    "t_end": end,
                    "zh": "这是第 \(idx) 段合成中文译稿内容，用于测试 B 面长列表滚动与不抢滚行为。",
                ])
            }
            let transcriptObject: [String: Any] = [
                "audio_sha256": audioSHA,
                "segments": segments,
            ]
            let transcriptData = try JSONSerialization.data(
                withJSONObject: transcriptObject,
                options: [.prettyPrinted, .sortedKeys]
            )
            try transcriptData.write(to: folder.appendingPathComponent("transcript_zh.json"), options: .atomic)
            let transcriptHash = LibrarySnapshot.digest(transcriptData)

            let renderIdentity = Data("{\"schema\":\"render-identity.v1\"}\n".utf8)
            try renderIdentity.write(to: folder.appendingPathComponent("render-identity.json"), options: .atomic)

            let packetID = "synthetic-picks-packet-\(mode == .validMultiPick ? "valid" : "fake")"
            let claims: [[String: Any]] = [
                ["id": "claim-A", "text": "合成观点 A：先厘清音频时间轴绑定边界", "span": "先厘清音频时间轴绑定边界的译稿原文依据片段", "t_start": 10.0, "t_end": 22.0, "confidence": 0.85, "status": "candidate"],
                ["id": "claim-B", "text": "合成观点 B：局部重叠区间不重复占据精选", "span": "局部重叠区间不重复占据精选的译稿原文依据片段", "t_start": 18.0, "t_end": 30.0, "confidence": 0.85, "status": "candidate"],
                ["id": "claim-C", "text": "合成观点 C：中段精选验证准确跳听与随播高亮", "span": "中段精选验证准确跳听与随播高亮的译稿原文依据片段", "t_start": 40.0, "t_end": 55.0, "confidence": 0.85, "status": "candidate"],
                ["id": "claim-D", "text": "合成观点 D：后半段候选用于验证优先级分散挑选", "span": "后半段候选用于验证优先级分散挑选的译稿原文依据片段", "t_start": 95.0, "t_end": 110.0, "confidence": 0.85, "status": "candidate"],
                ["id": "claim-E", "text": "合成观点 E：已确认观点优先进入精选并按时间排序", "span": "已确认观点优先进入精选并按时间排序的译稿原文依据片段", "t_start": 130.0, "t_end": 140.0, "confidence": 0.85, "status": "confirmed"],
            ]
            let extractObject: [String: Any] = [
                "packet_id": packetID,
                "work_id": epID,
                "schema_id": "general",
                "mode": "deep",
                "status": "ready",
                "created_at": "2026-11-26T00:00:00Z",
                "summary_line": "合成精选测试摘要",
                "transcript_hash": transcriptHash,
                "claims": claims,
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

            let timeline = mode == .validMultiPick ? "output.mp3.seconds" : "source.start_ms"
            let pointListenObject: [String: Any] = [
                "work_id": epID,
                "packet_id": packetID,
                "transcript_hash": transcriptHash,
                "audio_sha256": audioSHA,
                "output_version": "1",
                "timeline": timeline,
                "viewpoints": [
                    ["id": "A", "viewpoint_text": "合成观点 A：先厘清音频时间轴绑定边界", "span": "先厘清音频时间轴绑定边界的译稿原文依据片段", "t_start": 10.0, "t_end": 22.0, "index_include": true, "source_kind": "claim", "source_point_id": "claim-A"],
                    ["id": "B", "viewpoint_text": "合成观点 B：局部重叠区间不重复占据精选", "span": "局部重叠区间不重复占据精选的译稿原文依据片段", "t_start": 18.0, "t_end": 30.0, "index_include": true, "source_kind": "claim", "source_point_id": "claim-B"],
                    ["id": "C", "viewpoint_text": "合成观点 C：中段精选验证准确跳听与随播高亮", "span": "中段精选验证准确跳听与随播高亮的译稿原文依据片段", "t_start": 40.0, "t_end": 55.0, "index_include": true, "source_kind": "claim", "source_point_id": "claim-C"],
                    ["id": "D", "viewpoint_text": "合成观点 D：后半段候选用于验证优先级分散挑选", "span": "后半段候选用于验证优先级分散挑选的译稿原文依据片段", "t_start": 95.0, "t_end": 110.0, "index_include": true, "source_kind": "claim", "source_point_id": "claim-D"],
                    ["id": "E", "viewpoint_text": "合成观点 E：已确认观点优先进入精选并按时间排序", "span": "已确认观点优先进入精选并按时间排序的译稿原文依据片段", "t_start": 130.0, "t_end": 140.0, "index_include": true, "source_kind": "claim", "source_point_id": "claim-E"],
                ],
            ]
            let pointListenData = try JSONSerialization.data(
                withJSONObject: pointListenObject,
                options: [.prettyPrinted, .sortedKeys]
            )
            try pointListenData.write(to: folder.appendingPathComponent("point-listen.json"), options: .atomic)

            return Episode(
                id: epID,
                title: mode == .validMultiPick ? "合成精选与随播高亮测试集" : "合成假时间轴负例集",
                show: "合成测试",
                guests: [],
                lensIds: [],
                createdAt: Date(timeIntervalSince1970: 1_764_000_000),
                durationSec: 180,
                audioURL: audioURL,
                downloading: false,
                artworkFile: nil,
                artworkKind: nil,
                pendingTranscript: nil,
                mediaRevision: nil,
                revisionFiles: nil,
                unavailableReason: nil,
                audioKind: "tts",
                hasChineseRenderSidecar: true
            )
        }
    }

    static func makeSilentWAV(durationSeconds: Int, sampleRate: Int = 8000) -> Data {
        let channels: UInt16 = 1
        let bitsPerSample: UInt16 = 16
        let numSamples = max(1, durationSeconds * sampleRate)
        let dataSize = UInt32(numSamples * Int(channels) * Int(bitsPerSample / 8))
        let byteRate = UInt32(sampleRate) * UInt32(channels) * UInt32(bitsPerSample / 8)
        let blockAlign = channels * (bitsPerSample / 8)

        var data = Data()
        func appendLE<T: FixedWidthInteger>(_ value: T) {
            var v = value.littleEndian
            withUnsafeBytes(of: &v) { data.append(contentsOf: $0) }
        }
        data.append(contentsOf: "RIFF".utf8)
        appendLE(UInt32(36) + dataSize)
        data.append(contentsOf: "WAVE".utf8)
        data.append(contentsOf: "fmt ".utf8)
        appendLE(UInt32(16))
        appendLE(UInt16(1))
        appendLE(channels)
        appendLE(UInt32(sampleRate))
        appendLE(byteRate)
        appendLE(blockAlign)
        appendLE(bitsPerSample)
        data.append(contentsOf: "data".utf8)
        appendLE(dataSize)
        data.append(Data(count: Int(dataSize)))
        return data
    }
}
#endif
