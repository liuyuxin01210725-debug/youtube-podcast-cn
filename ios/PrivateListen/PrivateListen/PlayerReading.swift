import Foundation

/// The translated manuscript can use source timestamps. Only a final-audio point index drives seeks.
struct PlayerReading {
    var paragraphs: [String] = []
    var nodes: [ListenViewpoint] = []
    var audio: PointListenAudio?

    static func load(_ episode: Episode) async -> Self {
        guard episode.unavailableReason == nil, episode.pendingTranscript == nil else { return Self() }
        let transcriptName = "transcript_zh.json"
        func asset(_ name: String) -> URL? {
            if let url = episode.publishedAsset(name) {
                return try? LibrarySnapshot.path(name, in: url.deletingLastPathComponent())
            }
            guard let old = episode.legacyCompanions, !old.declaredFiles.contains(name) else { return nil }
            return try? LibrarySnapshot.path(name, in: old.folder)
        }
        func data(_ name: String) -> Data? {
            guard let url = asset(name), LocalAudio.isPlayable(url) else { return nil }
            return try? Data(contentsOf: url)
        }

        var result = Self()
        if let bytes = data(transcriptName), let transcript = try? JSONDecoder().decode(Transcript.self, from: bytes) {
            var matchesAudio = true
            if let old = episode.legacyCompanions, episode.publishedAsset(transcriptName) == nil {
                if let digest = transcript.audio_sha256 {
                    matchesAudio = digest == old.audioDigest
                } else if let url = try? LibrarySnapshot.path("output.mp3", in: old.folder) {
                    matchesAudio = (try? await PointListenAudio.load(url).sha256) == old.audioDigest
                } else {
                    matchesAudio = false
                }
            } else if let digest = transcript.audio_sha256 {
                matchesAudio = (try? await PointListenAudio.load(episode.audioURL).sha256) == digest
            }
            if matchesAudio { result.paragraphs = transcript.paragraphs }
        }
        guard !Task.isCancelled,
              let bytes = data(PointListening.indexFileName),
              let index = try? JSONDecoder().decode(PointListenIndex.self, from: bytes),
              let packet = ["extract.json", "extract.live.json"].lazy
                .compactMap({ data($0).flatMap(ExtractPacketDecoding.decode) }).first(where: { $0.packetId == index.packetId }),
              let audio = try? await PointListenAudio.load(episode.audioURL) else { return result }
        result.audio = audio
        result.nodes = PointListening.rows(packet: packet, index: index).filter {
            PointListening.refusal(row: $0, index: index, packet: packet, episodeID: episode.id,
                                   audioHash: audio.sha256, duration: audio.duration) == nil
        }.sorted { ($0.tStart ?? 0) < ($1.tStart ?? 0) }
        return result
    }

    struct Transcript: Decodable {
        struct Segment: Decodable {
            let text: String?
            let zh: String?
        }
        let segments: [Segment]?
        let text: String?
        let audio_sha256: String?

        var paragraphs: [String] {
            let lines = (segments ?? []).compactMap { segment in
                [segment.zh, segment.text].compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
                    .first { !$0.isEmpty }
            }
            if !lines.isEmpty { return lines }
            return text?.split(whereSeparator: \.isNewline).map(String.init) ?? []
        }
    }
}
