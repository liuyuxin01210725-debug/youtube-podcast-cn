import Foundation

/// Pure playhead rules for the now-playing transport. Kept free of AVFoundation so
/// `swiftc -parse-as-library` probes (and mirrored Python checks) can lock the policy.
enum PlayerPlaybackPolicy {
    /// How close to `duration` counts as "already at the end" for a play tap.
    /// Slightly wider than a single 0.5s observer tick so a short isolate clip
    /// that ended (or was scrubbed to the end) still restarts instead of no-op.
    static let endEpsilon: Double = 1.0

    /// Play while paused at/near the end must restart from 0. Mid-track play stays put.
    static func shouldRestartFromBeginning(
        current: Double,
        duration: Double,
        liveClock: Double? = nil,
        epsilon: Double = endEpsilon
    ) -> Bool {
        guard duration.isFinite, duration > 1, current.isFinite, current >= 0 else { return false }
        var playhead = current
        if let liveClock, liveClock.isFinite, liveClock >= 0 {
            playhead = max(playhead, liveClock)
        }
        return playhead >= max(0, duration - max(0, epsilon))
    }

    /// Seconds to persist right after a seek lands.
    /// A restart seek to the start must not let a stale end clock revive the old playhead
    /// through `ContinueListening.playheadToPersist` (observed 0 + liveClock≈duration → duration).
    static func progressAfterSeek(target: Double, liveClock: Double) -> Double {
        guard target.isFinite, target >= 0 else { return 0 }
        if target < 1 {
            if liveClock.isFinite, liveClock >= 0, liveClock < 1 {
                return liveClock
            }
            return target
        }
        return ContinueListening.playheadToPersist(observed: target, liveClock: liveClock)
    }
}
