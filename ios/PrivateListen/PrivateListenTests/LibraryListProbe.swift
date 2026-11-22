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
    @MainActor
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

        // Exercise the actual store write, persisted ListenMark and existing rail policy.
        let suite = "LibraryListProbe.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        let episodes = ["manual", "fallback"].map { id in
            Episode(id: id, title: id, guests: [], lensIds: [], createdAt: .distantPast,
                    durationSec: nil, audioURL: URL(fileURLWithPath: "/tmp/\(id).mp3"), downloading: false)
        }
        store.remember(id: "fallback", seconds: 2, duration: 0)
        store.remember(id: "manual", seconds: 3, duration: 0)
        check("手动归档前 Continue 是目标集", store.continueEntries(in: episodes).map(\.id) == ["manual"])
        store.markFinished("manual")
        let manualRows = episodes.map { episode in
            row(episode.id, title: episode.title, show: "Probe",
                finished: store.isFinished(episode.id), inProgress: store.isInProgress(episode.id),
                unplayed: store.isUnplayed(episode.id), starred: episode.id == "manual")
        }
        check("手动标已听后未听完过滤掉该 id", LibraryShelf.visible(manualRows, chip: .all, query: "").map(\.id) == ["fallback"])
        check("手动标已听后已听含该 id", LibraryShelf.visible(manualRows, chip: .finished, query: "").map(\.id) == ["manual"])
        check("手动标已听后星标仍能找到该 id", LibraryShelf.visible(manualRows, chip: .starred, query: "").map(\.id) == ["manual"])
        check("手动标已听后 Continue 换槽且最多一条", store.continueEntries(in: episodes).map(\.id) == ["fallback"])
        let restored = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        check("重读后完成事实仍在且不再续听该集", restored.isFinished("manual") && restored.resumeSeconds(for: "manual") == 0
            && restored.continueEntries(in: episodes).map(\.id) == ["fallback"])
        let persisted = try! JSONDecoder().decode([String: LibraryStore.ListenMark].self,
            from: defaults.data(forKey: "PrivateListen.progress")!)
        check("完成只写既有 finished 且保留真实进度", persisted["manual"]?.finished == true && persisted["manual"]?.seconds == 3)
        restored.dismissContinue("fallback")
        check("Continue 卡左滑不写完成且保留进度", !restored.isFinished("fallback") && restored.resumeSeconds(for: "fallback") == 2)
        store.markFinished("fallback")
        check("最后一集完成后 Continue 隐藏", store.continueEntries(in: episodes).isEmpty)
        store.markFinished("never-started")
        check("未播且时长未知也可直接完成", store.isFinished("never-started") && !store.isUnplayed("never-started"))

        func episode(_ id: String, duration: Double) -> Episode {
            Episode(id: id, title: id, guests: [], lensIds: [], createdAt: .distantPast,
                    durationSec: duration, audioURL: URL(fileURLWithPath: "/tmp/\(id).mp3"), downloading: false)
        }
        let shortID = "play-note-short-isolate"
        let shortCatalog = [episode(shortID, duration: 8)]
        store.remember(id: shortID, seconds: 8, duration: 8, finished: true)
        let ended = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        check("自然播完的短集重读后从头播放", ended.isFinished(shortID) && ended.resumeSeconds(for: shortID) == 0)
        ended.remember(id: shortID, seconds: 0.45, duration: 8, finished: false)
        let replayed = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        check(
            "短集复听 0.45 秒重读后仍为已听，Continue 使用该游标",
            replayed.isFinished(shortID) && replayed.resumeSeconds(for: shortID) == 0.45
                && !replayed.isInProgress(shortID)
                && replayed.continueEntries(in: shortCatalog).first?.seconds == 0.45
        )
        replayed.remember(id: shortID, seconds: 8, duration: 8, finished: true)
        let shortEOF = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        check("短集复听自然到尾后仍为已听，清除游标，Continue 无候选",
              shortEOF.isFinished(shortID) && shortEOF.resumeSeconds(for: shortID) == 0
                && shortEOF.continueEntries(in: shortCatalog).isEmpty)
        replayed.remember(id: shortID, seconds: 7.5, duration: 8, finished: false)
        check(
            "短集复听最后半秒仍可读回，且仍为已听",
            LibraryStore(defaults: defaults, loadLibraryOnInit: false).isFinished(shortID)
                && LibraryStore(defaults: defaults, loadLibraryOnInit: false).resumeSeconds(for: shortID) == 7.5
        )
        let tailID = "point-listen-tail"
        let tailCatalog = [episode(tailID, duration: 600)]
        let justFinished = replayed.remember(id: tailID, seconds: 590, duration: 600)
        check("点段维持原来最后十五秒的完成规则，并报告本次新完成",
              justFinished && replayed.isFinished(tailID) && replayed.continueEntries(in: tailCatalog).isEmpty)
        let finishedAgain = replayed.remember(id: tailID, seconds: 594, duration: 600, finished: justFinished)
        check("本轮点段已经记为已听，后续同轮写回不会被当成复听",
              !finishedAgain && replayed.resumeSeconds(for: tailID) == 0 && replayed.continueEntries(in: tailCatalog).isEmpty)
        replayed.remember(id: tailID, seconds: 595, duration: 600)
        check("重新点末段复听可以进入 Continue，仍是已听",
              replayed.isFinished(tailID) && replayed.continueEntries(in: tailCatalog).first?.seconds == 595)

        let oldJSON = Data(#"{"lauren":{"seconds":996.984,"finished":true}}"#.utf8)
        let migrated = try! JSONDecoder().decode([String: LibraryStore.ListenMark].self, from: oldJSON)
        check(
            "旧完成记录只保留原秒数与完成值，来源留空即未知，不自动改归类",
            migrated["lauren"]?.seconds == 996.984
                && migrated["lauren"]?.finished == true
                && migrated["lauren"]?.finishedSource == nil
                && migrated["lauren"]?.replaySeconds == nil
                && LibraryStore.resumeSeconds(in: migrated, id: "lauren") == 0
        )

        let longID = "already-finished-long"
        store.remember(id: longID, seconds: 2648.904, duration: 2662, finished: true)
        store.remember(id: longID, seconds: 213.674, duration: 2662)
        let jumped = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        check(
            "已听长集点段到中段仍为已听，Continue 使用重读的复听游标",
            jumped.isFinished(longID)
                && jumped.resumeSeconds(for: longID) == 213.674
                && jumped.continueEntries(in: [episode(longID, duration: 2662)]).first?.seconds == 213.674
        )
        jumped.remember(id: longID, seconds: 0, duration: 2662, finished: false)
        check(
            "已听集被写入 0 秒也不改成未听",
            LibraryStore(defaults: defaults, loadLibraryOnInit: false).isFinished(longID)
                && LibraryStore(defaults: defaults, loadLibraryOnInit: false).resumeSeconds(for: longID) == 0
        )
        jumped.remember(id: longID, seconds: 2662, duration: 2662, finished: true)
        check(
            "已听长集再次自然到尾后仍从头开始",
            LibraryStore(defaults: defaults, loadLibraryOnInit: false).isFinished(longID)
                && LibraryStore(defaults: defaults, loadLibraryOnInit: false).resumeSeconds(for: longID) == 0
        )
        check(
            "手动归档来源是 manual，且不发明复听游标",
            persisted["manual"]?.finished == true
                && persisted["manual"]?.finishedSource == .manual
                && persisted["manual"]?.replaySeconds == nil
        )

        let undoneID = "manual-undo-midplay"
        store.remember(id: undoneID, seconds: 1200, duration: 1800, finished: true)
        store.remember(id: undoneID, seconds: 80, duration: 1800, finished: false)
        check(
            "普通进度写入 finished=false 不能当作撤销接口，已听仍保持",
            store.isFinished(undoneID) && store.resumeSeconds(for: undoneID) == 80
        )
        store.markUnfinished(undoneID)
        check(
            "存储接口撤销后不再是已听，并保留当时游标",
            !store.isFinished(undoneID) && store.resumeSeconds(for: undoneID) == 80
        )
        store.remember(id: undoneID, seconds: 95, duration: 1800, finished: false)
        store.remember(id: undoneID, seconds: 110, duration: 1800)
        let afterUndo = LibraryStore(defaults: defaults, loadLibraryOnInit: false)
        check(
            "撤销后再中段播放不会被 sticky 改回已听",
            !afterUndo.isFinished(undoneID)
                && afterUndo.resumeSeconds(for: undoneID) == 110
        )
        afterUndo.remember(id: undoneID, seconds: 1800, duration: 1800, finished: true)
        check(
            "撤销后若自然到尾，可以再次记为已听",
            LibraryStore(defaults: defaults, loadLibraryOnInit: false).isFinished(undoneID)
                && LibraryStore(defaults: defaults, loadLibraryOnInit: false).resumeSeconds(for: undoneID) == 0
        )

        let a = "finished-A-replay"
        let b = "unfinished-B-continue"
        let continueSuite = "LibraryListProbe.continue-compat.\(UUID().uuidString)"
        let continueDefaults = UserDefaults(suiteName: continueSuite)!
        defer { continueDefaults.removePersistentDomain(forName: continueSuite) }
        let continueStore = LibraryStore(defaults: continueDefaults, loadLibraryOnInit: false)
        let catalog = [episode(a, duration: 2662), episode(b, duration: 600)]
        continueStore.remember(id: b, seconds: 40, duration: 600, finished: false)
        continueStore.remember(id: a, seconds: 2648.904, duration: 2662, finished: true)
        continueStore.acknowledgeContinue(a)
        var replayGate = PodcastProgressGate()
        func checkpointReplay(_ seconds: Double) {
            guard replayGate.observe(seconds: seconds) else { return }
            continueStore.remember(id: a, seconds: seconds, duration: 2662)
        }
        checkpointReplay(213)
        check("只打开或拖到已听 A 的 213 秒时没有复听游标，Continue 仍为 B",
              continueStore.resumeSeconds(for: a) == 0 && continueStore.continueEntries(in: catalog).map(\.id) == [b])
        replayGate.beginPlayback(at: 213)
        checkpointReplay(213)
        check("点段已定位且请求播放，时钟还没推进时 Continue 仍为 B",
              continueStore.resumeSeconds(for: a) == 0 && continueStore.continueEntries(in: catalog).map(\.id) == [b])
        checkpointReplay(217.4)
        let reopenedContinue = LibraryStore(defaults: continueDefaults, loadLibraryOnInit: false)
        let continueItems = reopenedContinue.continueEntries(in: catalog)
        let finishedRows = LibraryShelf.visible(
            catalog.map { ep in
                LibraryShelf.Row(
                    id: ep.id, title: ep.title, showName: "Probe", guests: [],
                    finished: continueStore.isFinished(ep.id),
                    inProgress: continueStore.isInProgress(ep.id),
                    unplayed: continueStore.isUnplayed(ep.id),
                    starred: false, lensIDs: []
                )
            },
            chip: .finished,
            query: ""
        ).map(\.id)
        check(
            "已听 A 点段复听 4.4 秒后暂停回库，重读后 Continue 为 A 且带 217.4 秒游标",
            reopenedContinue.isFinished(a)
                && reopenedContinue.resumeSeconds(for: a) == 217.4
                && continueItems.map(\.id) == [a] && continueItems.first?.seconds == 217.4
                && finishedRows == [a]
                && reopenedContinue.resumeSeconds(for: b) == 40
        )
        reopenedContinue.dismissContinue(a)
        check("左滑隐藏复听 A 后卡让位给 B，且保留 A 已听与游标",
              reopenedContinue.continueEntries(in: catalog).map(\.id) == [b]
                && reopenedContinue.isFinished(a) && reopenedContinue.resumeSeconds(for: a) == 217.4)
        reopenedContinue.acknowledgeContinue(a)
        check("取消隐藏后仍只有一张卡，位置恢复为 A",
              reopenedContinue.continueEntries(in: catalog).map(\.id) == [a])
        reopenedContinue.remember(id: b, seconds: 45, duration: 600, finished: false)
        check("切到 B 播放后 Continue 换成 B，各自停点和已听归类保留",
              reopenedContinue.continueEntries(in: catalog).map(\.id) == [b]
                && reopenedContinue.resumeSeconds(for: b) == 45
                && reopenedContinue.resumeSeconds(for: a) == 217.4 && reopenedContinue.isFinished(a))
        reopenedContinue.remember(id: a, seconds: 222, duration: 2662, finished: false)
        reopenedContinue.remember(id: a, seconds: 2662, duration: 2662, finished: true)
        check("A 复听自然播完后让位给 B，A 仍为已听并从头开始",
              reopenedContinue.continueEntries(in: catalog).map(\.id) == [b]
                && reopenedContinue.resumeSeconds(for: a) == 0 && reopenedContinue.isFinished(a))
        reopenedContinue.remember(id: a, seconds: 217.4, duration: 2662, finished: false)
        reopenedContinue.markFinished(a)
        let manualAfterReplay = LibraryStore(defaults: continueDefaults, loadLibraryOnInit: false)
        check("复听 A 后手动标已听清除游标，重读后 Continue 为 B",
              manualAfterReplay.continueEntries(in: catalog).map(\.id) == [b]
                && manualAfterReplay.resumeSeconds(for: a) == 0 && manualAfterReplay.isFinished(a))

        let baseline = Data(#"{"A":{"seconds":2648.904,"finished":true},"C":{"seconds":12,"finished":false}}"#.utf8)
        let currentNoBaseline = Data(#"{"missing-baseline":{"seconds":996.984,"finished":true}}"#.utf8)
        let currentWithBaseline = Data(#"{"A":{"seconds":213.674,"finished":false},"C":{"seconds":12,"finished":false}}"#.utf8)
        let decodedNoBaseline = try! JSONDecoder().decode([String: LibraryStore.ListenMark].self, from: currentNoBaseline)
        let decodedCurrent = try! JSONDecoder().decode([String: LibraryStore.ListenMark].self, from: currentWithBaseline)
        let decodedBaseline = try! JSONDecoder().decode([String: LibraryStore.ListenMark].self, from: baseline)
        check(
            "缺前基线：只加载当前旧记录，来源未知，不发明完成理由",
            decodedNoBaseline["missing-baseline"]?.finished == true
                && decodedNoBaseline["missing-baseline"]?.seconds == 996.984
                && decodedNoBaseline["missing-baseline"]?.finishedSource == nil
                && decodedNoBaseline["missing-baseline"]?.replaySeconds == nil
                && LibraryStore.resumeSeconds(in: decodedNoBaseline, id: "missing-baseline") == 0
        )
        check(
            "有前基线也只加载当前值，不把基线已听写回已变成未听的A",
            decodedBaseline["A"]?.finished == true
                && decodedCurrent["A"]?.finished == false
                && decodedCurrent["A"]?.seconds == 213.674
                && decodedCurrent["A"]?.finishedSource == nil
                && LibraryStore.resumeSeconds(in: decodedCurrent, id: "A") == 213.674
        )
        let migrateSuite = "LibraryListProbe.migrate.\(UUID().uuidString)"
        let migrateDefaults = UserDefaults(suiteName: migrateSuite)!
        defer { migrateDefaults.removePersistentDomain(forName: migrateSuite) }
        migrateDefaults.set(currentWithBaseline, forKey: "PrivateListen.progress")
        // Baseline is kept aside only; the store must not consult it.
        migrateDefaults.set(baseline, forKey: "PrivateListen.progressBaseline.doNotUse")
        let loadedCurrent = LibraryStore(defaults: migrateDefaults, loadLibraryOnInit: false)
        check(
            "有前基线的存储加载只读当前进度键，A保持未听，不冒造恢复",
            !loadedCurrent.isFinished("A")
                && loadedCurrent.resumeSeconds(for: "A") == 213.674
                && !loadedCurrent.isFinished("C")
                && migrateDefaults.data(forKey: "PrivateListen.progress") == currentWithBaseline
                && migrateDefaults.data(forKey: "PrivateListen.progressBaseline.doNotUse") == baseline
        )
        loadedCurrent.markFinished("A")
        loadedCurrent.markUnfinished("A")
        let reloadedUndone = LibraryStore(defaults: migrateDefaults, loadLibraryOnInit: false)
        check(
            "用户已撤销后重载仍是当前未听，不按基线恢复为已听",
            !reloadedUndone.isFinished("A")
                && reloadedUndone.resumeSeconds(for: "A") == 213.674
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
