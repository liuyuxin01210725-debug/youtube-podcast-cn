import Foundation

/// The translated manuscript can use source timestamps. Only a final-audio point index drives seeks.
struct PlayerReading {
    struct Line: Equatable, Identifiable {
        let id: String
        let displayText: String
        let originalText: String?
        let start: Double?
        let end: Double?
    }

    var lines: [Line] = []
    var nodes: [ListenViewpoint] = []
    var audio: PointListenAudio?

    var paragraphs: [String] { lines.map(\.displayText) }

    func originalText(for node: ListenViewpoint) -> String? {
        guard let nodeStart = node.tStart, nodeStart.isFinite else { return nil }
        let nodeEnd: Double
        if let end = node.tEnd, end.isFinite, end >= nodeStart {
            nodeEnd = end
        } else {
            nodeEnd = nodeStart
        }
        let matched = lines.compactMap { line -> String? in
            guard let original = line.originalText, !original.isEmpty,
                  let lineStart = line.start, lineStart.isFinite else { return nil }
            let lineEnd: Double
            if let end = line.end, end.isFinite, end >= lineStart {
                lineEnd = end
            } else {
                lineEnd = lineStart
            }
            guard lineStart <= nodeEnd, lineEnd >= nodeStart else { return nil }
            return original
        }
        guard !matched.isEmpty else { return nil }
        return matched.joined(separator: "\n\n")
    }

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
            if matchesAudio { result.lines = transcript.lines }
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
            let id: String?
            let text: String?
            let zh: String?
            let en: String?
            let t_start: Double?
            let t_end: Double?

            enum CodingKeys: String, CodingKey {
                case id, text, zh, en, t_start, t_end
            }

            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                if let s = try? c.decodeIfPresent(String.self, forKey: .id) {
                    id = s
                } else if let n = try? c.decodeIfPresent(Int.self, forKey: .id) {
                    id = String(n)
                } else {
                    id = nil
                }
                text = try? c.decodeIfPresent(String.self, forKey: .text)
                zh = try? c.decodeIfPresent(String.self, forKey: .zh)
                en = try? c.decodeIfPresent(String.self, forKey: .en)
                t_start = try? c.decodeIfPresent(Double.self, forKey: .t_start)
                t_end = try? c.decodeIfPresent(Double.self, forKey: .t_end)
            }
        }
        let segments: [Segment]?
        let text: String?
        let audio_sha256: String?

        var lines: [Line] {
            let trimmedSegments: [Line] = (segments ?? []).enumerated().compactMap { index, segment in
                let zh = segment.zh?.trimmingCharacters(in: .whitespacesAndNewlines)
                let rawText = segment.text?.trimmingCharacters(in: .whitespacesAndNewlines)
                let en = segment.en?.trimmingCharacters(in: .whitespacesAndNewlines)
                let validZh = (zh?.isEmpty == false) ? zh : nil
                let validText = (rawText?.isEmpty == false) ? rawText : nil
                let validEn = (en?.isEmpty == false) ? en : nil
                guard let display = validZh ?? validText else { return nil }

                let original: String?
                if let validEn, validEn != display {
                    original = validEn
                } else if validZh != nil, let validText, validText != display {
                    original = validText
                } else {
                    original = nil
                }

                let start = segment.t_start.flatMap { $0.isFinite ? $0 : nil }
                let end = segment.t_end.flatMap { $0.isFinite ? $0 : nil }
                let lineID = (segment.id?.isEmpty == false) ? segment.id! : "line-\(index)"
                return Line(id: lineID, displayText: display, originalText: original, start: start, end: end)
            }
            if !trimmedSegments.isEmpty { return trimmedSegments }
            return (text?.split(whereSeparator: \.isNewline).map(String.init) ?? []).enumerated().map { index, value in
                Line(id: "line-\(index)", displayText: value, originalText: nil, start: nil, end: nil)
            }
        }

        var paragraphs: [String] { lines.map(\.displayText) }
    }
}
