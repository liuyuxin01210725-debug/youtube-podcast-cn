import Foundation

struct Episode: Identifiable, Hashable {
    enum UnavailableReason: String {
        case audio = "当前音轨缺失或校验失败"
        case revision = "节目版本无法验证"
    }

    var id: String
    var title: String
    /// Explicit show from library.json when present.
    var show: String?
    var guests: [String]
    /// Theme lens ids from library.json when present; otherwise derived at filter time.
    var lensIds: [String]
    var createdAt: Date
    var durationSec: Double?
    var audioURL: URL
    var downloading: Bool
    var artworkFile: String?
    var artworkKind: String?
    var pendingTranscript: PendingTranscriptReference?
    var mediaRevision: String? = nil
    var revisionFiles: Set<String>? = nil
    var unavailableReason: UnavailableReason? = nil

    /// Optional reading companions can survive a same-audio migration in episodes/<id>.
    struct LegacyCompanions {
        let folder: URL
        let audioDigest: String
        let declaredFiles: Set<String>
    }
    var legacyCompanions: LegacyCompanions? = nil
    var audioKind: String? = nil
    var hasChineseRenderSidecar: Bool = false

    func publishedAsset(_ name: String) -> URL? {
        guard revisionFiles?.contains(name) != false else { return nil }
        return audioURL.deletingLastPathComponent().appendingPathComponent(name)
    }

    /// Unmapped audio changes get a fresh cursor. Legacy marks keep their existing keys.
    var mediaKey: String { mediaRevision.map { "\(id).\($0)" } ?? id }

    func matchesMedia(_ other: Episode) -> Bool {
        mediaKey == other.mediaKey && audioURL == other.audioURL
    }

    var artworkURL: URL? {
        guard let artworkFile, ["cover.jpg", "cover.png", "cover.webp"].contains(artworkFile) else { return nil }
        return publishedAsset(artworkFile)
    }

    static func == (lhs: Episode, rhs: Episode) -> Bool { lhs.matchesMedia(rhs) }

    func hash(into hasher: inout Hasher) {
        hasher.combine(mediaKey)
        hasher.combine(audioURL)
    }

    var dateLabel: String {
        if createdAt.timeIntervalSince1970 <= 0 { return "日期未知" }
        return createdAt.formatted(.dateTime.year().month().day())
    }

    var durationLabel: String {
        guard let durationSec, durationSec > 0 else { return "时长未知" }
        return listenClock(durationSec)
    }

    /// Prefer explicit `show`, else title after ` · ` / ` | `, else 「未分类」.
    var showName: String {
        if let show {
            let trimmed = show.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return trimmed }
        }
        for separator in [" · ", " | ", "|"] {
            guard let range = title.range(of: separator) else { continue }
            let extracted = title[range.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
            if !extracted.isEmpty { return String(extracted) }
        }
        return "未分类"
    }

    var isPlayableNow: Bool {
        unavailableReason == nil && !downloading && LocalAudio.isPlayable(audioURL)
    }

    /// Effective lens ids: JSON first, else catalog match on owned content.
    var effectiveLensIds: [String] {
        if !lensIds.isEmpty { return lensIds }
        return DiscoverCatalog.derivedLensIDs(for: self)
    }
}

enum PendingTranscriptReference: Equatable, Sendable {
    case invalid
    case package(id: String, duration: Double, count: Int)
}

func listenClock(_ seconds: Double) -> String {
    guard seconds.isFinite, seconds > 0 else { return "0:00" }
    let total = Int(seconds.rounded(.down))
    let hours = total / 3600
    let minutes = (total % 3600) / 60
    let secs = total % 60
    if hours > 0 {
        return String(format: "%d:%02d:%02d", hours, minutes, secs)
    }
    return String(format: "%d:%02d", minutes, secs)
}

enum EpisodeID {
    static func isRevisionDigest(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    static func isMediaKey(_ key: String) -> Bool {
        let parts = key.split(separator: ".", omittingEmptySubsequences: false)
        guard let id = parts.first, isSafe(String(id)) else { return false }
        if parts.count == 1 { return true }
        return parts.count == 2 && isRevisionDigest(String(parts[1]))
    }

    static func isSafe(_ id: String) -> Bool {
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        return (1...80).contains(id.count) && id.unicodeScalars.allSatisfy { allowed.contains($0) }
    }
}

enum LocalAudio {
    static func isPlayable(_ url: URL) -> Bool {
        let keys: Set<URLResourceKey> = [.isRegularFileKey, .ubiquitousItemDownloadingStatusKey, .fileSizeKey]
        guard let values = try? URL(fileURLWithPath: url.path).resourceValues(forKeys: keys),
              values.isRegularFile == true, let size = values.fileSize, size > 0 else { return false }
        if let status = values.ubiquitousItemDownloadingStatus, status != .current { return false }
        return true
    }
}

enum EpisodeDate {
    private static let formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    static func parse(_ value: String?) -> Date? {
        guard let value else { return nil }
        return formatter.date(from: value)
    }
}
