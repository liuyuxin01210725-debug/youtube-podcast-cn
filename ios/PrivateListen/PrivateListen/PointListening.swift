import AVFoundation
import CryptoKit
import Foundation

struct ListenViewpoint: Codable, Hashable, Identifiable {
    var id: String
    var viewpointText: String
    var span: String?
    var tStart: Double?
    var tEnd: Double?
    var indexInclude: Bool?
    var sourceKind: String?
    var sourcePointId: String?

    enum CodingKeys: String, CodingKey {
        case id, span
        case viewpointText = "viewpoint_text"
        case tStart = "t_start"
        case tEnd = "t_end"
        case indexInclude = "index_include"
        case sourceKind = "source_kind"
        case sourcePointId = "source_point_id"
    }
}

struct PointListenIndex: Codable, Hashable {
    var workId: String
    var packetId: String
    var transcriptHash: String
    var audioSHA256: String
    var outputVersion: String?
    var timeline: String
    var viewpoints: [ListenViewpoint]

    enum CodingKeys: String, CodingKey {
        case workId = "work_id"
        case packetId = "packet_id"
        case transcriptHash = "transcript_hash"
        case audioSHA256 = "audio_sha256"
        case outputVersion = "output_version"
        case timeline, viewpoints
    }
}

/// A locally verified final audio file. Never inferred from a packet/document hash.
struct PointListenAudio {
    let url: URL
    let sha256: String
    let duration: Double
    private let attributes: [FileAttributeKey: Any]

    static func load(_ url: URL) async throws -> PointListenAudio {
        let before = try FileManager.default.attributesOfItem(atPath: url.path)
        let hash = try await Task.detached(priority: .utility) {
            let file = try FileHandle(forReadingFrom: url)
            defer { try? file.close() }
            var hash = SHA256()
            while let chunk = try file.read(upToCount: 1_048_576), !chunk.isEmpty {
                hash.update(data: chunk)
            }
            return hash.finalize().map { String(format: "%02x", $0) }.joined()
        }.value
        let duration = try await AVURLAsset(url: url).load(.duration).seconds
        let result = PointListenAudio(url: url, sha256: hash, duration: duration, attributes: before)
        guard duration.isFinite, duration > 0, result.isCurrent else {
            throw CocoaError(.fileReadCorruptFile)
        }
        return result
    }

    var isCurrent: Bool {
        guard let now = try? FileManager.default.attributesOfItem(atPath: url.path) else { return false }
        return [FileAttributeKey.size, .modificationDate, .systemFileNumber].allSatisfy {
            (attributes[$0] as? NSObject) == (now[$0] as? NSObject)
        }
    }
}

enum PointListening {
    static let indexFileName = "point-listen.json"

    static func rows(packet: ExtractionPacket, index: PointListenIndex?) -> [ListenViewpoint] {
        if let index {
            guard Set(index.viewpoints.map(\.id)).count == index.viewpoints.count else { return [] }
            return index.viewpoints.filter { $0.indexInclude != false && !$0.viewpointText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        }
        // Reading-only migration: actual statements/questions, never generic method titles.
        let claims = packet.claims.filter { $0.status != .discarded }.map {
            ListenViewpoint(id: "claim-" + $0.id, viewpointText: $0.text, span: $0.span, tStart: $0.tStart, tEnd: $0.tEnd,
                sourceKind: "claim", sourcePointId: $0.id)
        }
        let questions = packet.openQuestions.filter { $0.status != .discarded }.map {
            ListenViewpoint(id: "question-" + $0.id, viewpointText: $0.text, span: $0.span, tStart: $0.tStart, tEnd: $0.tEnd,
                sourceKind: "open", sourcePointId: $0.id)
        }
        return (claims + questions).enumerated().sorted {
            let left = $0.element.tStart.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil } ?? .infinity
            let right = $1.element.tStart.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil } ?? .infinity
            return left == right ? $0.offset < $1.offset : left < right
        }.map(\.element)
    }

    static func refusal(
        row: ListenViewpoint, index: PointListenIndex?, packet: ExtractionPacket,
        episodeID: String, audioHash: String?, duration: Double
    ) -> String? {
        guard packet.status == .ready, packet.mode != .light,
              !ExtractDisplay.isPlaceholderDeep(packet) else { return "提炼尚未就绪或已过期" }
        guard let index else { return "尚无成片绑定索引" }
        guard index.workId == episodeID, packet.workId == episodeID,
              !index.packetId.isEmpty, index.packetId == packet.packetId else { return "索引不属于当前提炼包" }
        guard isHex(index.transcriptHash, count: 16), index.transcriptHash == packet.transcriptHash else { return "索引译稿版本已过期" }
        guard index.timeline == "output.mp3.seconds" else { return "索引不是中文成片秒轴" }
        guard Set(index.viewpoints.map(\.id)).count == index.viewpoints.count,
              index.viewpoints.allSatisfy({ !$0.id.isEmpty }) else { return "索引条目标识无效" }
        guard isHex(index.audioSHA256, count: 64), let audioHash,
              index.audioSHA256 == audioHash else { return "成片音频绑定未通过" }
        guard duration.isFinite, duration > 0 else { return "成片时长尚未核实" }
        guard row.indexInclude != false, index.viewpoints.contains(row),
              let start = row.tStart else { return "本条暂无成片起点" }
        guard start.isFinite, start >= 0 else { return "本条起点无效" }
        guard start < duration else { return "本条起点超出成片" }
        if let end = row.tEnd {
            guard end.isFinite, end > start else { return "本条时间范围无效" }
            guard end <= duration else { return "本条终点超出成片" }
        }
        return nil
    }

    private static func isHex(_ value: String, count: Int) -> Bool {
        value.count == count && value.allSatisfy { "0123456789abcdef".contains($0) }
    }
}

/// Separates the seek landing from actual playback; the existing finished rule stays in LibraryStore.
struct JumpContinueProgress {
    private var landing: Double?
    var allowsCheckpoint: Bool { landing == nil }

    mutating func didLand(at seconds: Double) { landing = seconds }
    mutating func reset() { landing = nil }
    mutating func observe(seconds: Double, isPlaying: Bool) -> Bool {
        guard let landing else { return true }
        guard isPlaying, seconds.isFinite, seconds > landing + 0.01 else { return false }
        self.landing = nil
        return true
    }
}
