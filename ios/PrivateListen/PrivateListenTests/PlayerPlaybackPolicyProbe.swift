import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Locks play-at-end restart and seek progress persistence.
/// `swiftc -parse-as-library` ContinueListening.swift PlayerPlaybackPolicy.swift and this file.
@main
struct PlayerPlaybackPolicyProbe {
    static func main() {
        var failures: [String] = []
        func check(_ name: String, _ ok: Bool) {
            if ok {
                print("ok  \(name)")
            } else {
                failures.append(name)
                print("FAIL \(name)")
            }
        }

        check(
            "已在终点再点播放应重头开始",
            PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 8, duration: 8)
        )
        check(
            "显示同为 0:08 但时钟略短于时长时仍应重头开始",
            PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 7.2, duration: 8, liveClock: 8)
        )
        check(
            "仅靠 liveClock 在终点也应重头开始",
            PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 0, duration: 8, liveClock: 8)
        )
        check(
            "中段暂停点播放不要重头",
            PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 3, duration: 8) == false
        )
        check(
            "时长未知时不误判终点",
            PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 8, duration: 0) == false
        )
        check(
            "距终点超过 epsilon 不重头",
            PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 6.5, duration: 8, epsilon: 1) == false
        )

        check(
            "seek 到 0 后即使 liveClock 仍停在旧终点，也要记下 0",
            PlayerPlaybackPolicy.progressAfterSeek(target: 0, liveClock: 8) == 0
        )
        check(
            "seek 到 0 且 liveClock 已落地，用 liveClock",
            PlayerPlaybackPolicy.progressAfterSeek(target: 0, liveClock: 0.2) == 0.2
        )
        check(
            "中段 seek 仍信任落地时钟",
            PlayerPlaybackPolicy.progressAfterSeek(target: 40, liveClock: 40.1) == 40.1
        )
        check(
            "中段 seek 时 premature 0 不能盖掉目标",
            PlayerPlaybackPolicy.progressAfterSeek(target: 40, liveClock: 0) == 40
        )

        if !failures.isEmpty {
            fputs("\(failures.count) failed: \(failures.joined(separator: ", "))\n", stderr)
            exit(1)
        }
        print("player playback policy probe passed")
    }
}
