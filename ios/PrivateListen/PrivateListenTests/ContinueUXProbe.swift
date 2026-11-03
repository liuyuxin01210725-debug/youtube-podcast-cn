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
        check("默认继续播放只留最近的未听完", one.map(\.id) == ["a"] && ContinueListening.displayLimit == 1)
        check("不到 5 秒不算继续播放", shown.allSatisfy { $0.id != "b" })
        check("已听完不进继续播放", shown.allSatisfy { $0.id != "c" })
        check("没有触摸时间的排在有时间的后面", shown.map(\.id) == ["a", "d"])
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
            afterDismiss.map(\.id) == ["d"] && marks["a"]?.seconds == 40 && marks["a"]?.finished == false
        )
        check("空 id 不能当成删除", ContinueListening.dismissing(hidden, id: "") == hidden)
        let restored = ContinueListening.acknowledging(hidden, id: "a")
        let afterAck = ContinueListening.items(marks: marks, durations: durations, dismissed: restored, limit: 20)
        check("打开或点继续播放会回到列表", afterAck.first?.id == "a")

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
            "同一集且控制可用时藏起继续播放卡",
            ContinueListening.showsContinueCard(continueID: "a", playerID: "a", controlsUsable: true) == false
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

        if !failures.isEmpty {
            fputs("\(failures.count) failed: \(failures.joined(separator: ", "))\n", stderr)
            exit(1)
        }
        print("continue ux probe passed")
    }
}
