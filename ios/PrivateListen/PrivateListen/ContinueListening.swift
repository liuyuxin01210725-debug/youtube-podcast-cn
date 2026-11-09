import Foundation

/// Rules for 资料库「继续播放」.
/// The rail reads ListenMark only (seconds, finished, swipe-dismiss). It does not
/// follow the 「听这段」 playhead. `PlayerModel` still refuses to write ListenMark
/// while a clip is active; `record` repeats that gate so the probe can lock it.
/// The default rail is one card: the latest unfinished episode whose playhead moved,
/// including a short play under the 5s 「在听」chip.
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
    /// Playhead must have moved. Zero does not steal the slot (open and leave
    /// before any audio). This is not the 5s 「在听」chip; a few seconds count.
    static let minimumPlayedSeconds = 0.0

    /// Whether the library draws the one continue card.
    ///
    /// Leaving the full player leaves the mini player on the episode you just
    /// heard. Hiding the card then removes the only row that names that episode,
    /// so the rail looks unchanged. A missing id draws nothing. `playerID` and
    /// `controlsUsable` stay so the view can pass mini-player state; they do not
    /// open a player and do not decide「听完」.
    static func showsContinueCard(
        continueID: String?,
        playerID: String?,
        controlsUsable: Bool
    ) -> Bool {
        guard let continueID, !continueID.isEmpty else { return false }
        _ = playerID
        _ = controlsUsable
        return true
    }

    /// Unfinished, playhead moved, not swipe-dismissed.
    /// Most recently touched first. Unknown touch time sinks below dated marks.
    /// Under 5 seconds still qualifies; `LibraryStore.isInProgress` keeps the 5s chip.
    static func items(
        marks: [String: Mark],
        durations: [String: Double],
        dismissed: Set<String>,
        limit: Int = displayLimit
    ) -> [Item] {
        let cap = max(0, limit)
        let ranked = marks.compactMap { id, mark -> Item? in
            guard !dismissed.contains(id) else { return nil }
            guard mark.seconds.isFinite, mark.seconds > minimumPlayedSeconds, !mark.finished else { return nil }
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
    /// `finished` is not set, the file is not deleted, and the star is untouched
    /// (stars live outside this set). Playback progress does not undo this;
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

    /// Seconds to store when leaving the player.
    /// The periodic observer is half a second behind, and the first tick waits too.
    /// `liveClock` is the player clock at leave. A premature 0 must not wipe a
    /// trusted playhead (a resume seek can still report 0). A non-finite clock
    /// keeps `observed`.
    static func playheadToPersist(observed: Double, liveClock: Double) -> Double {
        let fallback = observed.isFinite && observed >= 0 ? observed : 0
        guard liveClock.isFinite, liveClock >= 0 else { return fallback }
        // Resume seek and a disappeared item can still report 0. Do not wipe
        // a playhead we already accepted, including a short tick under 1s.
        if liveClock == 0, fallback > 0 { return fallback }
        if liveClock < 1, fallback >= 1 { return fallback }
        return liveClock
    }

    struct LibrarySnapshot: Equatable {
        var marks: [String: Mark]
        var dismissed: Set<String>
        /// The card the library would draw. Nil when the slot is empty.
        var cardID: String?
    }

    /// Open the episode, play, leave back to the library.
    /// Opening acknowledges a swipe-dismiss (it does not add one). The sample
    /// is the playhead written on leave, including under 5 seconds. A clip
    /// window does not write ListenMark, so the previous card stays.
    /// `playerID` is the mini player after you pop the full player — usually
    /// this same episode, with transport usable. The card still names it.
    static func afterLeavingPlayer(
        marks: [String: Mark],
        dismissed: Set<String>,
        durations: [String: Double],
        episodeID: String,
        seconds: Double,
        duration: Double,
        touchedAt: TimeInterval,
        clipSuppressListenMark: Bool,
        playerID: String? = nil,
        controlsUsable: Bool = true
    ) -> LibrarySnapshot {
        let dismissedNext = acknowledging(dismissed, id: episodeID)
        let marksNext = record(
            marks: marks,
            id: episodeID,
            seconds: seconds,
            duration: duration,
            touchedAt: touchedAt,
            clipSuppressListenMark: clipSuppressListenMark
        )
        let ranked = items(marks: marksNext, durations: durations, dismissed: dismissedNext)
        let cardID: String?
        if let id = ranked.first?.id,
           showsContinueCard(continueID: id, playerID: playerID, controlsUsable: controlsUsable) {
            cardID = id
        } else {
            cardID = nil
        }
        return LibrarySnapshot(marks: marksNext, dismissed: dismissedNext, cardID: cardID)
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
