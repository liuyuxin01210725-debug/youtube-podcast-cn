import Foundation

struct Episode: Identifiable, Hashable {
    var id: String
    var title: String
    /// Explicit show from library.json / episode.json when present.
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

    var artworkURL: URL? {
        guard let artworkFile, ["cover.jpg", "cover.png", "cover.webp"].contains(artworkFile) else { return nil }
        return audioURL.deletingLastPathComponent().appendingPathComponent(artworkFile)
    }

    static func == (lhs: Episode, rhs: Episode) -> Bool { lhs.id == rhs.id }

    func hash(into hasher: inout Hasher) { hasher.combine(id) }

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
        !downloading && LocalAudio.isPlayable(audioURL)
    }

    /// Effective lens ids: JSON first, else catalog match on owned content.
    var effectiveLensIds: [String] {
        if !lensIds.isEmpty { return lensIds }
        return DiscoverCatalog.derivedLensIDs(for: self)
    }
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
    static func isSafe(_ id: String) -> Bool {
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        return (1...80).contains(id.count) && id.unicodeScalars.allSatisfy { allowed.contains($0) }
    }
}

enum LocalAudio {
    static func isPlayable(_ url: URL) -> Bool {
        let keys: Set<URLResourceKey> = [.isRegularFileKey, .ubiquitousItemDownloadingStatusKey, .fileSizeKey]
        guard let values = try? url.resourceValues(forKeys: keys) else {
            return FileManager.default.fileExists(atPath: url.path)
        }
        if values.isRegularFile == false { return false }
        if let status = values.ubiquitousItemDownloadingStatus, status != .current { return false }
        if let size = values.fileSize, size <= 0 { return false }
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
