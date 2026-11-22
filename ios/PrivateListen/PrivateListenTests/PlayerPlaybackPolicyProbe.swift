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
            "旧界面仍显示结束，可信时钟在半秒处时不再重头",
            !PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 8, duration: 8, liveClock: 0.5)
        )
        check(
            "暂停在最后半秒，下次播放仍从实际停点开始",
            !PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 7.5, duration: 8, liveClock: 7.5)
        )
        check(
            "不足一秒的音频在真实结束后仍可重播",
            PlayerPlaybackPolicy.shouldRestartFromBeginning(current: 0.8, duration: 0.8)
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
        check(
            "半秒续听须在真实落点后开播，不能接受取消或仍在零秒",
            PlayerPlaybackPolicy.didLandSeek(finished: true, target: 0.5, liveClock: 0.51)
                && !PlayerPlaybackPolicy.didLandSeek(finished: true, target: 0.5, liveClock: 0)
                && !PlayerPlaybackPolicy.didLandSeek(finished: false, target: 0.5, liveClock: 0.5)
        )
        check(
            "续听时不接受偏离停点的时钟，点段仍沿用原定位容差",
            !PlayerPlaybackPolicy.didLandSeek(finished: true, target: 30, liveClock: 29.5)
                && PlayerPlaybackPolicy.didLandSeek(finished: true, target: 30, liveClock: 29.5, tolerance: 1)
        )
        check(
            "终点重播只接受确实回到开头的 seek",
            PlayerPlaybackPolicy.didLandSeek(finished: true, target: 0, liveClock: 0)
                && !PlayerPlaybackPolicy.didLandSeek(finished: true, target: 0, liveClock: 0.8)
        )

        if !failures.isEmpty {
            fputs("\(failures.count) failed: \(failures.joined(separator: ", "))\n", stderr)
            exit(1)
        }
        print("player playback policy probe passed")
    }
}
