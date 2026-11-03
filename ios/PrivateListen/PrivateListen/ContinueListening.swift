import Foundation

/// Rules for 资料库「继续播放」.
/// The rail reads ListenMark only (seconds, finished, swipe-dismiss). It does not
/// follow the 「听这段」 playhead. `PlayerModel` still refuses to write ListenMark
/// while a clip is active; `record` repeats that gate so the probe can lock it.
/// The default rail is one card: the latest unfinished episode.
enum ContinueListening {
    struct Mark: Equatable {
        var seconds: Double
        var finished: Bool
        var touchedAt: TimeInterval
    }

    struct Item: Equatable, Identifiable {
        var id: String
        var seconds: Double
        var remaining: Double?
        var fraction: Double?
        var touchedAt: TimeInterval
    }

    /// How far the live playhead may sit from the saved resume point before a
    /// continue tap seeks back to ListenMark. Wider than the 5s progress checkpoint
    /// so ordinary playback still toggles, while a jump (听这段) seeks back.
    static let resumeTolerance = 8.0
    /// One resume card. Most recently touched unfinished episode wins.
    /// Swipe-dismiss still only hides that card; the next unfinished one can take the slot.
    static let displayLimit = 1

    /// In progress (≥5s, not finished), not swipe-dismissed.
    /// Most recently touched first. Unknown touch time sinks below dated marks.
    static func items(
        marks: [String: Mark],
        durations: [String: Double],
        dismissed: Set<String>,
        limit: Int = displayLimit
    ) -> [Item] {
        let cap = max(0, limit)
        let ranked = marks.compactMap { id, mark -> Item? in
            guard !dismissed.contains(id) else { return nil }
            guard mark.seconds.isFinite, mark.seconds >= 5, !mark.finished else { return nil }
            let duration = durations[id] ?? 0
            let remaining: Double? = duration > 1 ? max(0, duration - mark.seconds) : nil
            let fraction: Double? = duration > 1 ? min(1, max(0, mark.seconds / duration)) : nil
            return Item(
                id: id,
                seconds: mark.seconds,
                remaining: remaining,
                fraction: fraction,
                touchedAt: mark.touchedAt
            )
        }
        .sorted { lhs, rhs in
            let lhsDated = lhs.touchedAt > 0
            let rhsDated = rhs.touchedAt > 0
            if lhsDated != rhsDated { return lhsDated && !rhsDated }
            if lhs.touchedAt != rhs.touchedAt { return lhs.touchedAt > rhs.touchedAt }
            if lhs.seconds != rhs.seconds { return lhs.seconds > rhs.seconds }
            return lhs.id > rhs.id
        }
        return Array(ranked.prefix(cap))
    }

    static func remainingLabel(seconds: Double, duration: Double?) -> String {
        if let duration, duration > 1 {
            return "剩余 \(clock(max(0, duration - seconds)))"
        }
        return "上次听到 \(clock(seconds))"
    }

    /// Left swipe removes the card from the rail. The ListenMark seconds stay,
    /// so opening the episode still resumes. Playback progress does not undo this;
    /// opening or pressing the continue pill does (`acknowledging`).
    static func dismissing(_ dismissed: Set<String>, id: String) -> Set<String> {
        guard !id.isEmpty else { return dismissed }
        var next = dismissed
        next.insert(id)
        return next
    }

    static func acknowledging(_ dismissed: Set<String>, id: String) -> Set<String> {
        guard !id.isEmpty else { return dismissed }
        var next = dismissed
        next.remove(id)
        return next
    }

    /// Same gate as `PlayerModel.checkpoint`: a clip window must not create a continue card.
    static func acceptsProgressSample(clipSuppressListenMark: Bool) -> Bool {
        !clipSuppressListenMark
    }

    /// Mirrors `LibraryStore.reducing` finished rule (within 15s of the end).
    /// When `clipSuppressListenMark` is true the map is returned unchanged.
    static func record(
        marks: [String: Mark],
        id: String,
        seconds: Double,
        duration: Double,
        touchedAt: TimeInterval,
        clipSuppressListenMark: Bool
    ) -> [String: Mark] {
        guard acceptsProgressSample(clipSuppressListenMark: clipSuppressListenMark) else { return marks }
        guard seconds.isFinite, seconds >= 0, !id.isEmpty else { return marks }
        let finished = duration > 1 && seconds >= max(0, duration - 15)
        var next = marks
        next[id] = Mark(seconds: seconds, finished: finished, touchedAt: touchedAt)
        return next
    }

    /// Continue pill: seek back to ListenMark when this episode is loaded but the
    /// playhead has moved away (听这段). Otherwise the caller toggles play/pause.
    static func shouldReopenAtListenMark(
        currentID: String?,
        episodeID: String,
        playhead: Double,
        resumeAt: Double,
        clipInProgress: Bool = false,
        tolerance: Double = resumeTolerance
    ) -> Bool {
        if clipInProgress { return true }
        guard currentID == episodeID else { return true }
        guard playhead.isFinite, resumeAt.isFinite else { return true }
        return abs(playhead - resumeAt) > tolerance
    }

    static func clock(_ seconds: Double) -> String {
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
}
