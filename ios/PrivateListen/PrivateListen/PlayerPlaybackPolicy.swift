import Foundation

/// Pure playhead rules for the now-playing transport. Kept free of AVFoundation so
/// `swiftc -parse-as-library` probes (and mirrored Python checks) can lock the policy.
enum PlayerPlaybackPolicy {
    /// The live clock can round at the boundary. A pause in the last second is
    /// still resumable. Natural EOF is also tracked by PlayerModel.
    static let endEpsilon: Double = 1.0 / 600

    /// Only return a seek target when replaying context after a real pause.
    static func smartResumeTarget(current: Double, pausedFor: Duration) -> Double? {
        guard pausedFor >= .seconds(15), current.isFinite, current > 0 else { return nil }
        return max(0, current - 3)
    }

    /// Play while paused at/near the end must restart from 0. Mid-track play stays put.
    static func shouldRestartFromBeginning(
        current: Double,
        duration: Double,
        liveClock: Double? = nil,
        epsilon: Double = endEpsilon
    ) -> Bool {
        guard duration.isFinite, duration > 0 else { return false }
        var playhead = current
        if let liveClock, liveClock.isFinite, liveClock >= 0 {
            playhead = liveClock
        }
        guard playhead.isFinite, playhead > 0 else { return false }
        return playhead >= duration - max(0, epsilon)
    }

    /// A completed seek still needs a credible live clock before progress can be written.
    static func didLandSeek(finished: Bool, target: Double, liveClock: Double, tolerance: Double = 0.1) -> Bool {
        guard finished, target.isFinite, target >= 0, liveClock.isFinite, liveClock >= 0 else { return false }
        return abs(liveClock - target) < tolerance
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
