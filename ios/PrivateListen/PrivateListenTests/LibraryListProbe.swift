import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Locks the library shelf: 「未听完」hides finished, 「已听」browses them, 星 still finds a finished star.
/// `swiftc -parse-as-library` LibraryShelf.swift and this file.
@main
struct LibraryListProbe {
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

        let rows = [
            row("new", title: "还没听", show: "Latent Space", finished: false, unplayed: true),
            row("mid", title: "听到一半", show: "Latent Space", inProgress: true),
            row("done", title: "已经听完", show: "Latent Space", guests: ["谭木声"], finished: true),
            row("starred-done", title: "听完还标了星", show: "DOAC", finished: true, starred: true, lensIDs: ["health"]),
            row("other", title: "另一节目未听", show: "Huberman Lab", unplayed: true, lensIDs: ["health"]),
        ]

        check(
            "冷启动默认是未听完",
            LibraryShelf.defaultChip == .all && LibraryShelf.defaultChip.label == "未听完"
        )

        let shelf = LibraryShelf.visible(rows, chip: LibraryShelf.defaultChip, query: "")
        check("未听完不含已听完", shelf.map(\.id) == ["new", "mid", "other"])
        check("未听完仍保留未听和在听的顺序", shelf.map(\.id) == ["new", "mid", "other"])
        check("已听完混入默认是 0", shelf.allSatisfy { !$0.finished })

        let archive = LibraryShelf.visible(rows, chip: .finished, query: "")
        check("已听只有听完的", archive.map(\.id) == ["done", "starred-done"])
        check("已听文案", LibraryShelf.Chip.finished.label == "已听")

        let stars = LibraryShelf.visible(rows, chip: .starred, query: "")
        check("星标仍能找到已听完的星标", stars.map(\.id) == ["starred-done"])
        check("未听完不因为星标把听完的留在主列表", !shelf.contains { $0.id == "starred-done" })

        check(
            "在听只有听到一半的",
            LibraryShelf.visible(rows, chip: .inProgress, query: "").map(\.id) == ["mid"]
        )
        check(
            "未听不含已听完",
            LibraryShelf.visible(rows, chip: .unplayed, query: "").map(\.id) == ["new", "other"]
        )

        let show = LibraryShelf.visible(rows, chip: .show("Latent Space"), query: "")
        check("按节目仍看得到该节目里听完的", show.map(\.id) == ["new", "mid", "done"])

        let lens = LibraryShelf.visible(rows, chip: .lens(id: "health", label: "健康"), query: "")
        check("透镜仍包含该主题里听完的", lens.map(\.id) == ["starred-done", "other"])

        let found = LibraryShelf.visible(rows, chip: .all, query: "谭木声")
        check("在未听完里搜索仍能找到已听完", found.map(\.id) == ["done"])
        check("空白搜索不算搜索，听完的继续藏着", LibraryShelf.visible(rows, chip: .all, query: "  ").map(\.id) == shelf.map(\.id))
        check(
            "在听里搜索不会把已听完翻出来",
            LibraryShelf.visible(rows, chip: .inProgress, query: "听完").isEmpty
        )
        check(
            "已听里的搜索只留听完且匹配的",
            LibraryShelf.visible(rows, chip: .finished, query: "星").map(\.id) == ["starred-done"]
        )

        let offered = LibraryShelf.rail(
            lenses: [(id: "health", label: "健康")],
            shows: ["DOAC", "Latent Space"]
        )
        check(
            "常驻是未听完、星、已听",
            offered.resident.map(\.label) == ["未听完", "星", "已听"]
        )
        check(
            "在听、未听、主题、按节目在更多里",
            offered.more.map(\.label) == ["在听", "未听", "健康", "DOAC", "Latent Space"]
        )
        check(
            "已听和星不埋进更多",
            !offered.more.contains(.finished) && !offered.more.contains(.starred) && !offered.more.contains(.all)
        )
        check("常驻三枚，加上更多仍不超过四", offered.resident.count == 3)
        let noThemes = LibraryShelf.rail(lenses: [], shows: ["Only"])
        check(
            "没有主题、节目不到两个时，更多仍留在听和未听",
            noThemes.more.map(\.label) == ["在听", "未听"] && noThemes.resident.map(\.label) == ["未听完", "星", "已听"]
        )

        check("未听完和在听显示继续播放", LibraryShelf.showsContinue(chip: .all, query: "") && LibraryShelf.showsContinue(chip: .inProgress, query: ""))
        check("已听、星标、按节目不显示继续播放", !LibraryShelf.showsContinue(chip: .finished, query: "") && !LibraryShelf.showsContinue(chip: .starred, query: "") && !LibraryShelf.showsContinue(chip: .show("Latent Space"), query: ""))
        check("搜索时不显示继续播放", !LibraryShelf.showsContinue(chip: .all, query: "听"))

        let finishedEmpty = LibraryShelf.emptyState(chip: .all, query: "", hasFinished: true)
        check(
            "未听完被听完清空时短接已听",
            finishedEmpty.message == "都听完了。" && finishedEmpty.action == .showArchive && finishedEmpty.actionLabel == "已听"
        )
        let stillOpen = LibraryShelf.emptyState(chip: .all, query: "", hasFinished: false)
        check(
            "还有未听完时不用这条提示",
            stillOpen.message == "这个筛选下暂时没有节目。" && stillOpen.action == nil
        )
        let archiveEmpty = LibraryShelf.emptyState(chip: .finished, query: "", hasFinished: false)
        check(
            "已听是空的可以回未听完",
            archiveEmpty.message == "还没有已听完的节目。" && archiveEmpty.action == .showUnfinished && archiveEmpty.actionLabel == "未听完"
        )
        let otherEmpty = LibraryShelf.emptyState(chip: .starred, query: "", hasFinished: true)
        check(
            "其它筛选空回到未听完",
            otherEmpty.message == "这个筛选下暂时没有节目。" && otherEmpty.action == .showUnfinished
        )
        check(
            "搜索落空仍是搜索文案",
            LibraryShelf.emptyState(chip: .all, query: "没有", hasFinished: true).message == "没有匹配的节目。"
                && LibraryShelf.emptyState(chip: .all, query: "没有", hasFinished: true).action == nil
        )
        check(
            "已听标题带数量",
            LibraryShelf.sectionTitle(chip: .finished, query: "", count: 2) == "已听 · 2"
        )
        check(
            "未听完标题只数还留在主列表里的",
            LibraryShelf.sectionTitle(chip: .all, query: "", count: shelf.count) == "未听完 · 3"
        )
        check(
            "默认行次要信息是节目和时长",
            LibraryShelf.rowSecondary(
                showName: "Latent Space",
                durationLabel: "45:00",
                remainingLabel: nil,
                downloading: false
            ) == "Latent Space · 45:00"
        )
        check(
            "在听用剩余，不叠总时长",
            LibraryShelf.rowSecondary(
                showName: "Latent Space",
                durationLabel: "45:00",
                remainingLabel: "剩余 12:00",
                downloading: false
            ) == "Latent Space · 剩余 12:00"
        )
        check(
            "上次听到不进默认行",
            LibraryShelf.rowSecondary(
                showName: "Latent Space",
                durationLabel: "45:00",
                remainingLabel: "上次听到 1:05",
                downloading: false
            ) == "Latent Space · 45:00"
        )
        check(
            "时长未知不造百分比",
            LibraryShelf.rowSecondary(
                showName: "未分类",
                durationLabel: "时长未知",
                remainingLabel: nil,
                downloading: false
            ).isEmpty
        )
        check(
            "下载状态仍在同一行",
            LibraryShelf.rowSecondary(
                showName: "DOAC",
                durationLabel: "1:00:00",
                remainingLabel: nil,
                downloading: true
            ) == "DOAC · 1:00:00 · 正在从 iCloud 下载"
        )
        check(
            "未听完行不重复未听",
            LibraryShelf.rowListenChip(chip: .all, finished: false, inProgress: false, unplayed: true) == nil
        )
        check(
            "未听完里的在听仍标在听",
            LibraryShelf.rowListenChip(chip: .all, finished: false, inProgress: true, unplayed: false) == "在听"
        )
        check(
            "已听筛选不再每行重复已听完",
            LibraryShelf.rowListenChip(chip: .finished, finished: true, inProgress: false, unplayed: false) == nil
        )
        check(
            "行内没有状态就不补芯片",
            LibraryShelf.rowMarks(listen: nil, starred: false, extra: nil).isEmpty
        )
        check(
            "行内芯片最多三枚",
            LibraryShelf.rowMarks(listen: "在听", starred: true, extra: "2要点").count == 3
                && LibraryShelf.rowStatusChipCap == 3
                && LibraryShelf.titleLineLimit == 2
        )

        if !failures.isEmpty {
            fputs("\(failures.count) failed: \(failures.joined(separator: ", "))\n", stderr)
            exit(1)
        }
        print("library list probe passed")
    }

    private static func row(
        _ id: String,
        title: String,
        show: String,
        guests: [String] = [],
        finished: Bool = false,
        inProgress: Bool = false,
        unplayed: Bool = false,
        starred: Bool = false,
        lensIDs: [String] = []
    ) -> LibraryShelf.Row {
        LibraryShelf.Row(
            id: id,
            title: title,
            showName: show,
            guests: guests,
            finished: finished,
            inProgress: inProgress,
            unplayed: unplayed,
            starred: starred,
            lensIDs: lensIDs
        )
    }
}
