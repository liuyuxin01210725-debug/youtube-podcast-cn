import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Locks the continue rail: who appears, swipe-dismiss, and the clip gate.
/// `swiftc -parse-as-library` ContinueListening.swift and this file.
@main
struct ContinueUXProbe {
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

        let durations = ["a": 600.0, "b": 180.0, "c": 90.0]
        let marks: [String: ContinueListening.Mark] = [
            "a": .init(seconds: 40, finished: false, touchedAt: 10),
            "b": .init(seconds: 4.9, finished: false, touchedAt: 50),
            "c": .init(seconds: 80, finished: true, touchedAt: 80),
            "d": .init(seconds: 12, finished: false, touchedAt: 0),
        ]

        let shown = ContinueListening.items(marks: marks, durations: durations, dismissed: [], limit: 20)
        check("在听进入继续播放", shown.contains { $0.id == "a" && $0.remaining == 560 })
        let one = ContinueListening.items(marks: marks, durations: durations, dismissed: [])
        check(
            "默认继续播放只留最近摸过的未听完，短于 5 秒也算",
            one.map(\.id) == ["b"] && ContinueListening.displayLimit == 1
        )
        var withZero = marks
        withZero["z"] = .init(seconds: 0, finished: false, touchedAt: 99)
        let noZero = ContinueListening.items(marks: withZero, durations: durations, dismissed: [], limit: 20)
        check(
            "播放头没动过不算继续播放",
            noZero.allSatisfy { $0.id != "z" } && noZero.first?.id == "b"
        )
        check("已听完不进继续播放", shown.allSatisfy { $0.id != "c" })
        check("没有触摸时间的排在有时间的后面", shown.map(\.id) == ["b", "a", "d"])
        check(
            "剩余文案",
            ContinueListening.remainingLabel(seconds: 120, duration: 180) == "剩余 1:00"
        )
        check(
            "没有总时长时用上次听到",
            ContinueListening.remainingLabel(seconds: 65, duration: nil) == "上次听到 1:05"
        )

        let hidden = ContinueListening.dismissing([], id: "a")
        let afterDismiss = ContinueListening.items(marks: marks, durations: durations, dismissed: hidden, limit: 20)
        check(
            "左滑删除只拿掉这一条，不写成听完",
            afterDismiss.map(\.id) == ["b", "d"] && marks["a"]?.seconds == 40 && marks["a"]?.finished == false
        )
        check("空 id 不能当成删除", ContinueListening.dismissing(hidden, id: "") == hidden)
        let restored = ContinueListening.acknowledging(hidden, id: "a")
        let afterAck = ContinueListening.items(marks: marks, durations: durations, dismissed: restored, limit: 20)
        check(
            "打开会把滑掉的一条放回候选，但不会单靠打开抢走最近的短播",
            afterAck.contains { $0.id == "a" } && afterAck.first?.id == "b" && !restored.contains("a")
        )
        check("打开不会把这一集写进滑掉名单", ContinueListening.acknowledging([], id: "a").isEmpty)

        let clip = ContinueListening.record(
            marks: marks,
            id: "b",
            seconds: 300,
            duration: 600,
            touchedAt: 99,
            clipSuppressListenMark: true
        )
        check("听这段不写继续播放", clip == marks && !ContinueListening.acceptsProgressSample(clipSuppressListenMark: true))

        let listened = ContinueListening.record(
            marks: [:],
            id: "e",
            seconds: 30,
            duration: 600,
            touchedAt: 3,
            clipSuppressListenMark: false
        )
        check(
            "正常进度会进入继续播放",
            ContinueListening.items(marks: listened, durations: ["e": 600], dismissed: []).map(\.id) == ["e"]
        )

        let finished = ContinueListening.record(
            marks: [:],
            id: "f",
            seconds: 100,
            duration: 110,
            touchedAt: 4,
            clipSuppressListenMark: false
        )
        check(
            "快结束记为听完，不进继续播放",
            finished["f"]?.finished == true
                && ContinueListening.items(marks: finished, durations: ["f": 110], dismissed: []).isEmpty
        )

        var many: [String: ContinueListening.Mark] = [:]
        for index in 0..<13 {
            many["id-\(index)"] = .init(seconds: 20, finished: false, touchedAt: TimeInterval(index + 1))
        }
        let capped = ContinueListening.items(marks: many, durations: [:], dismissed: [])
        check("多条未听完时继续播放只留最近听的", capped.count == 1 && capped.first?.id == "id-12")
        let hiddenLatest = ContinueListening.dismissing([], id: "id-12")
        let nextUp = ContinueListening.items(marks: many, durations: [:], dismissed: hiddenLatest)
        check("滑掉最近一条后，下一条未听完顶上，仍只有一条", nextUp.count == 1 && nextUp.first?.id == "id-11")

        check(
            "播放头还在断点附近就不要拉回",
            ContinueListening.shouldReopenAtListenMark(currentID: "a", episodeID: "a", playhead: 41, resumeAt: 40) == false
        )
        check(
            "听这段把播放头拉走后，继续播放要回到 ListenMark",
            ContinueListening.shouldReopenAtListenMark(currentID: "a", episodeID: "a", playhead: 400, resumeAt: 40)
        )
        check(
            "听这段即使停在断点附近，继续播放也回到 ListenMark",
            ContinueListening.shouldReopenAtListenMark(
                currentID: "a",
                episodeID: "a",
                playhead: 40,
                resumeAt: 40,
                clipInProgress: true
            )
        )
        check(
            "换一集就按那一集的 ListenMark 打开",
            ContinueListening.shouldReopenAtListenMark(currentID: "a", episodeID: "b", playhead: 40, resumeAt: 10)
        )

        check(
            "没有迷你条时继续播放还在，不为此新建播放器",
            ContinueListening.showsContinueCard(continueID: "a", playerID: nil, controlsUsable: false)
        )
        check(
            "回库后迷你条还停在同一集，继续播放仍显示这一集",
            ContinueListening.showsContinueCard(continueID: "a", playerID: "a", controlsUsable: true)
        )
        check(
            "同一集但控制还没就绪时留下继续播放卡",
            ContinueListening.showsContinueCard(continueID: "a", playerID: "a", controlsUsable: false)
        )
        check(
            "迷你条在播另一集时继续播放照实显示",
            ContinueListening.showsContinueCard(continueID: "a", playerID: "b", controlsUsable: true)
        )
        check(
            "没有继续播放目标时不留空卡",
            ContinueListening.showsContinueCard(continueID: nil, playerID: "b", controlsUsable: true) == false
                && ContinueListening.showsContinueCard(continueID: "", playerID: "a", controlsUsable: true) == false
        )

        let older = ["b": ContinueListening.Mark(seconds: 40, finished: false, touchedAt: 10)]
        let journeyDurations = ["a": 600.0, "b": 600.0]
        for sample in [2.5, 6.0] {
            let left = ContinueListening.afterLeavingPlayer(
                marks: older,
                dismissed: [],
                durations: journeyDurations,
                episodeID: "a",
                seconds: sample,
                duration: 600,
                touchedAt: 100,
                clipSuppressListenMark: false,
                playerID: "a",
                controlsUsable: true
            )
            check(
                "短播 \(sample) 秒后回库，继续播放是刚听的那集",
                left.cardID == "a"
                    && left.marks["a"]?.seconds == sample
                    && left.marks["a"]?.finished == false
                    && ContinueListening.items(
                        marks: left.marks,
                        durations: journeyDurations,
                        dismissed: left.dismissed
                    ).count == 1
            )
        }

        let wasDismissed = ContinueListening.dismissing([], id: "a")
        var dismissedMarks = older
        dismissedMarks["a"] = .init(seconds: 30, finished: false, touchedAt: 1)
        let replayed = ContinueListening.afterLeavingPlayer(
            marks: dismissedMarks,
            dismissed: wasDismissed,
            durations: journeyDurations,
            episodeID: "a",
            seconds: 3,
            duration: 600,
            touchedAt: 80,
            clipSuppressListenMark: false,
            playerID: "a",
            controlsUsable: true
        )
        check(
            "滑掉之后再短播，打开会取消隐藏并且继续播放换成这一集",
            replayed.cardID == "a" && !replayed.dismissed.contains("a")
        )
        let swipedAgain = ContinueListening.dismissing(replayed.dismissed, id: "a")
        let afterSwipe = ContinueListening.items(
            marks: replayed.marks,
            durations: journeyDurations,
            dismissed: swipedAgain
        )
        check(
            "短播之后再左滑，槽让回上一条未听完，仍只有一条",
            afterSwipe.count == 1 && afterSwipe.first?.id == "b"
        )

        let clipLeave = ContinueListening.afterLeavingPlayer(
            marks: older,
            dismissed: [],
            durations: journeyDurations,
            episodeID: "a",
            seconds: 30,
            duration: 600,
            touchedAt: 200,
            clipSuppressListenMark: true,
            playerID: "a",
            controlsUsable: true
        )
        check(
            "听这段后回库不改继续播放，也不写 ListenMark",
            clipLeave.cardID == "b" && clipLeave.marks == older
        )

        let untouched = ContinueListening.afterLeavingPlayer(
            marks: older,
            dismissed: [],
            durations: journeyDurations,
            episodeID: "a",
            seconds: 0,
            duration: 600,
            touchedAt: 300,
            clipSuppressListenMark: false,
            playerID: "a",
            controlsUsable: true
        )
        check(
            "打开后播放头还是 0 就离开，继续播放仍是原来那集",
            untouched.cardID == "b" && untouched.marks["a"]?.seconds == 0
        )

        let finishedLeave = ContinueListening.afterLeavingPlayer(
            marks: older,
            dismissed: [],
            durations: journeyDurations,
            episodeID: "a",
            seconds: 590,
            duration: 600,
            touchedAt: 400,
            clipSuppressListenMark: false,
            playerID: "a",
            controlsUsable: true
        )
        check(
            "听到接近结尾再回库，不把听完的放进继续播放",
            finishedLeave.cardID == "b" && finishedLeave.marks["a"]?.finished == true
        )

        check(
            "离开播放器时用真实播放头，不把刚过 5 秒写成上一拍",
            ContinueListening.playheadToPersist(observed: 4.5, liveClock: 5.2) == 5.2
                && ContinueListening.playheadToPersist(observed: 0, liveClock: 2.5) == 2.5
        )
        check(
            "续播定位还没落地时，0 不能盖掉已经记下的播放头",
            ContinueListening.playheadToPersist(observed: 40, liveClock: 0) == 40
                && ContinueListening.playheadToPersist(observed: 0.4, liveClock: 0) == 0.4
                && ContinueListening.playheadToPersist(observed: 40, liveClock: .nan) == 40
        )

        if !failures.isEmpty {
            fputs("\(failures.count) failed: \(failures.joined(separator: ", "))\n", stderr)
            exit(1)
        }
        print("continue ux probe passed")
    }
}
