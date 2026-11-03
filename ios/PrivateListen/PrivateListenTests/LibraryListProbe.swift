import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Locks the library shelf: 「全部」hides finished, 「已听」browses them, ★ still finds a finished star.
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

        let shelf = LibraryShelf.visible(rows, chip: .all, query: "")
        check("全部不含已听完", shelf.map(\.id) == ["new", "mid", "other"])
        check("全部仍保留未听和在听的顺序", shelf.map(\.id) == ["new", "mid", "other"])

        let archive = LibraryShelf.visible(rows, chip: .finished, query: "")
        check("已听只有听完的", archive.map(\.id) == ["done", "starred-done"])
        check("已听文案", LibraryShelf.Chip.finished.label == "已听")

        let stars = LibraryShelf.visible(rows, chip: .starred, query: "")
        check("星标仍能找到已听完的星标", stars.map(\.id) == ["starred-done"])
        check("全部不因为星标把听完的留在主列表", !shelf.contains { $0.id == "starred-done" })

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
        check("在全部里搜索仍能找到已听完", found.map(\.id) == ["done"])
        check("空白搜索不算搜索，听完的继续藏着", LibraryShelf.visible(rows, chip: .all, query: "  ").map(\.id) == shelf.map(\.id))
        check(
            "在听里搜索不会把已听完翻出来",
            LibraryShelf.visible(rows, chip: .inProgress, query: "听完").isEmpty
        )
        check(
            "已听里的搜索只留听完且匹配的",
            LibraryShelf.visible(rows, chip: .finished, query: "星").map(\.id) == ["starred-done"]
        )

        let offered = LibraryShelf.chips(
            hasInProgress: true,
            hasUnplayed: true,
            hasFinished: true,
            hasStarred: true,
            lenses: [(id: "health", label: "健康")]
        )
        check(
            "已听接在听的状态后面、星标前面",
            offered.map(\.label) == ["全部", "在听", "未听", "已听", "★", "健康"]
        )
        let noArchive = LibraryShelf.chips(
            hasInProgress: false,
            hasUnplayed: true,
            hasFinished: false,
            hasStarred: false,
            lenses: []
        )
        check("没有听完的就不放已听", noArchive.map(\.label) == ["全部", "未听"])

        check("全部和在听显示继续播放", LibraryShelf.showsContinue(chip: .all, query: "") && LibraryShelf.showsContinue(chip: .inProgress, query: ""))
        check("已听、星标、按节目不显示继续播放", !LibraryShelf.showsContinue(chip: .finished, query: "") && !LibraryShelf.showsContinue(chip: .starred, query: "") && !LibraryShelf.showsContinue(chip: .show("Latent Space"), query: ""))
        check("搜索时不显示继续播放", !LibraryShelf.showsContinue(chip: .all, query: "听"))

        check(
            "全部被听完清空时指向已听",
            LibraryShelf.emptyMessage(chip: .all, query: "", hasFinished: true) == "听完的节目在「已听」。"
        )
        check(
            "还有未听完时不用这条提示",
            LibraryShelf.emptyMessage(chip: .all, query: "", hasFinished: false) == "这个筛选下暂时没有节目。"
        )
        check(
            "搜索落空仍是搜索文案",
            LibraryShelf.emptyMessage(chip: .all, query: "没有", hasFinished: true) == "没有匹配的节目。"
        )
        check(
            "已听标题带数量",
            LibraryShelf.sectionTitle(chip: .finished, query: "", count: 2) == "已听 · 2"
        )
        check(
            "全部标题只数还留在主列表里的",
            LibraryShelf.sectionTitle(chip: .all, query: "", count: shelf.count) == "全部 · 3"
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
