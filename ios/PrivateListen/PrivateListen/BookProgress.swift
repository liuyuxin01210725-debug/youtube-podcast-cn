import Foundation

/// Book identity never enters the Episode / ListenMark namespace.
struct BookPlaybackReference: Codable, Hashable {
    var bookID: String
    var packageRevision: String
    var chapterID: String
    var audioRevision: String
    var key: String { [bookID, packageRevision, chapterID, audioRevision].joined(separator: "|") }
}
struct BookPlayedRange: Codable, Equatable { var start: Double; var end: Double }
struct BookMark: Codable, Equatable {
    var reference: BookPlaybackReference
    var bookTitle: String
    var chapterTitle: String
    var seconds: Double = 0
    var duration: Double = 0
    var finished = false
    var lastActuallyListenedAt: TimeInterval = 0
    var playedRanges: [BookPlayedRange] = []
}
enum BookProgressPolicy {
    static func actualAdvance(previous: Double?, current: Double, playing: Bool, rate: Float) -> Double {
        guard let previous, playing, previous.isFinite, current.isFinite else { return 0 }
        let delta = current - previous
        return delta > 0 && delta <= max(1, Double(rate)) * 2.5 ? delta : 0
    }
    static func merging(_ ranges: [BookPlayedRange]) -> [BookPlayedRange] {
        let sorted = ranges.filter { $0.start.isFinite && $0.end.isFinite && $0.start >= 0 && $0.end > $0.start }.sorted { $0.start < $1.start }
        var result: [BookPlayedRange] = []
        for range in sorted {
            if let last = result.last, range.start <= last.end + 0.05 {
                result[result.count - 1].end = max(last.end, range.end)
            } else { result.append(range) }
        }
        return result
    }
    static func record(_ mark: BookMark, seconds: Double, duration: Double, playedDelta: Double, ended: Bool, at: TimeInterval) -> BookMark {
        guard seconds.isFinite, duration.isFinite, seconds >= 0, duration > 0 else { return mark }
        var next = mark
        next.seconds = min(seconds, duration); next.duration = duration
        if playedDelta > 0, playedDelta.isFinite, playedDelta <= 5 {
            if next.finished && seconds < duration - 1 { next.finished = false }
            next.lastActuallyListenedAt = at
            next.playedRanges = merging(next.playedRanges + [BookPlayedRange(start: max(0, seconds - playedDelta), end: min(seconds, duration))])
        }
        // Natural EOF alone is insufficient: seeking to the end must not mark a whole chapter heard.
        let covered = next.playedRanges.reduce(0) { $0 + $1.end - $1.start }
        if ended && next.lastActuallyListenedAt > 0 && covered >= max(0, duration - 1) { next.finished = true }
        return next
    }
    static func bookWins(bookTouch: TimeInterval?, podcastTouch: TimeInterval?) -> Bool {
        guard let bookTouch, bookTouch > 0 else { return false }
        return podcastTouch == nil || bookTouch > podcastTouch!
    }
}
