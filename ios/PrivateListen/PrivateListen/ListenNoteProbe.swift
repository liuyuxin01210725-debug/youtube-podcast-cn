#if DEBUG
import Foundation

/// Local-only: listen-note 极简输入 + 下游 MVP。Launch `-ListenNoteProbe`.
/// Writes `/Documents/listen-note-probe.json`.
@MainActor
enum ListenNoteProbe {
    static var enabled: Bool {
        ProcessInfo.processInfo.arguments.contains("-ListenNoteProbe")
    }

    static func runIfNeeded(listenNotes: ListenNoteStore, library: LibraryStore) async {
        guard enabled else { return }
        for _ in 0..<16 {
            if !library.episodes.isEmpty { break }
            try? await Task.sleep(nanoseconds: 250_000_000)
            library.reload()
        }

        let probeId = "listen-note-probe-episode"
        listenNotes.clear(episodeId: probeId)
        if listenNotes.isPromptDismissed(episodeId: probeId) {
            resetPromptDismissed(listenNotes, episodeId: probeId)
        }

        var steps: [[String: Any]] = []

        // A: empty → no summary / no search hit / prompt eligible when finished
        let emptySummary = listenNotes.listSummary(for: probeId)
        let emptySearch = listenNotes.matchesSearch(episodeId: probeId, query: "小而美")
        let promptEmpty = listenNotes.shouldShowFinishedPrompt(episodeId: probeId, isFinished: true)
        let promptUnfinished = listenNotes.shouldShowFinishedPrompt(episodeId: probeId, isFinished: false)
        steps.append([
            "name": "A_empty",
            "summaryNil": emptySummary == nil,
            "searchFalse": emptySearch == false,
            "promptWhenFinished": promptEmpty,
            "promptWhenUnfinishedFalse": promptUnfinished == false,
            "pass": emptySummary == nil && emptySearch == false && promptEmpty && !promptUnfinished,
        ])

        // B: write headline+body+tags → summary prefers headline; search hits tag/headline/body
        listenNotes.upsert(
            episodeId: probeId,
            headline: "小而美创业",
            body: "与发芽发酵小众公司理念连结。\n第二行不进摘要。",
            tags: ["小而美", "CPG", "小而美", "  "]
        )
        let note = listenNotes.note(for: probeId)
        let summary = listenNotes.listSummary(for: probeId)
        let searchTag = listenNotes.matchesSearch(episodeId: probeId, query: "cpg")
        let searchHeadline = listenNotes.matchesSearch(episodeId: probeId, query: "小而美创业")
        let searchBody = listenNotes.matchesSearch(episodeId: probeId, query: "发芽发酵")
        let searchMiss = listenNotes.matchesSearch(episodeId: probeId, query: "量子力学")
        let tagsOk = note?.tags == ["小而美", "CPG"]
        let promptHiddenWithNote = listenNotes.shouldShowFinishedPrompt(episodeId: probeId, isFinished: true) == false
        steps.append([
            "name": "B_upsert_summary_search_tags",
            "summary": summary as Any,
            "summaryIsHeadline": summary == "小而美创业",
            "tagsDeduped": tagsOk,
            "searchTag": searchTag,
            "searchHeadline": searchHeadline,
            "searchBody": searchBody,
            "searchMiss": searchMiss == false,
            "promptHiddenWithNote": promptHiddenWithNote,
            "pass": summary == "小而美创业"
                && tagsOk
                && searchTag
                && searchHeadline
                && searchBody
                && !searchMiss
                && promptHiddenWithNote,
        ])

        // C: body-only summary = first line
        listenNotes.upsert(
            episodeId: probeId,
            headline: "",
            body: "正文首行当摘要\n第二行",
            tags: []
        )
        let bodySummary = listenNotes.listSummary(for: probeId)
        steps.append([
            "name": "C_body_first_line_summary",
            "summary": bodySummary as Any,
            "pass": bodySummary == "正文首行当摘要",
        ])

        // D: dismiss prompt sticks even after clear note
        listenNotes.clear(episodeId: probeId)
        listenNotes.dismissPrompt(episodeId: probeId)
        let afterDismiss = listenNotes.shouldShowFinishedPrompt(episodeId: probeId, isFinished: true)
        steps.append([
            "name": "D_prompt_dismiss",
            "shouldShow": afterDismiss,
            "pass": afterDismiss == false,
        ])

        // E: hard gates — store file schema + share exclude; no deep_request side effect
        let excluded = ListenNoteStore.isShareExcluded("listen_notes.json")
            && ListenNoteStore.isShareExcluded("listen-note-probe.json")
            && ListenNoteStore.isShareExcluded(ListenNoteDownstream.insightsFileName)
        let schema = ListenNoteStore.schema == "shengjian.listen_notes.v1"
        steps.append([
            "name": "E_hard_gates_local",
            "shareExcluded": excluded,
            "schema": ListenNoteStore.schema,
            "insightsSchema": ListenNoteDownstream.insightsSchema,
            "pass": excluded && schema,
        ])

        // F: body-only（单字段收敛） hides finished prompt；mergedDisplay 兼容旧 headline+body
        resetPromptDismissed(listenNotes, episodeId: probeId)
        listenNotes.clear(episodeId: probeId)
        listenNotes.upsert(episodeId: probeId, headline: "", body: "只写正文也算有感", tags: [])
        let promptHiddenBodyOnly = listenNotes.shouldShowFinishedPrompt(
            episodeId: probeId, isFinished: true
        ) == false
        let merged = ListenNoteDownstream.mergedDisplayText(
            headline: "小而美创业",
            body: "与发芽发酵连结"
        )
        let mergedOk = merged == "小而美创业\n与发芽发酵连结"
        let mergedSame = ListenNoteDownstream.mergedDisplayText(
            headline: "首行",
            body: "首行\n第二行"
        ) == "首行\n第二行"
        steps.append([
            "name": "F_body_only_and_merge_compat",
            "promptHiddenBodyOnly": promptHiddenBodyOnly,
            "mergedOk": mergedOk,
            "mergedSame": mergedSame,
            "pass": promptHiddenBodyOnly && mergedOk && mergedSame,
        ])

        // G: insights sidecar write
        listenNotes.clear(episodeId: probeId)
        let insight = ListenNoteDownstream.writeInsight(
            episodeId: probeId,
            noteText: "小而美/小众创业理念\n第二行"
        )
        let insightReload = ListenNoteDownstream.insight(for: probeId)
        let insightOk = insight?.summary == "小而美/小众创业理念"
            && insightReload?.keywords.contains("小而美") == true
            && insightReload?.keywords.contains("创业") == true
        steps.append([
            "name": "G_insights_sidecar",
            "summary": insight?.summary as Any,
            "keywords": insightReload?.keywords as Any,
            "pass": insightOk,
        ])

        // H: library hit — prefer bridge when from matches owned; else keyword; never invent
        var bridgeHitOk = false
        var bridgeDetail: [String: Any] = [:]
        let owned = library.episodes
        if let sampleFrom = LensBridgeCatalog.loadAll().first(where: { bridge in
            bridge.isPushable && owned.contains(where: { $0.id == bridge.fromEpisodeId })
                && owned.contains(where: { $0.id == bridge.toEpisodeId })
        }) {
            let hit = ListenNoteDownstream.findLibraryHit(
                currentEpisodeId: sampleFrom.fromEpisodeId,
                noteText: "小而美创业理念",
                ownedEpisodes: owned,
                noteLookup: { listenNotes.note(for: $0) }
            )
            bridgeHitOk = hit != nil
                && hit?.episodeId == sampleFrom.toEpisodeId
                && !(hit?.rationaleZh.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
                && hit?.source == "bridge"
            bridgeDetail = [
                "from": sampleFrom.fromEpisodeId,
                "expectedTo": sampleFrom.toEpisodeId,
                "gotTo": hit?.episodeId as Any,
                "source": hit?.source as Any,
                "rationaleLen": hit?.rationaleZh.count as Any,
            ]
        } else if !owned.isEmpty {
            // 无桥时：对第一集写创业关键词，期望 keyword miss 或命中其他集——至少不崩溃且无假 hit 到自己
            let cur = owned[0].id
            let hit = ListenNoteDownstream.findLibraryHit(
                currentEpisodeId: cur,
                noteText: "量子力学玄学无匹配词zzz",
                ownedEpisodes: owned,
                noteLookup: { listenNotes.note(for: $0) }
            )
            bridgeHitOk = hit == nil
            bridgeDetail = ["mode": "no_bridge_expect_miss", "hit": hit?.episodeId as Any]
        } else {
            bridgeHitOk = true
            bridgeDetail = ["mode": "empty_library_skip"]
        }
        // keyword miss must not return self
        let selfHit = ListenNoteDownstream.findLibraryHit(
            currentEpisodeId: probeId,
            noteText: "无关键词匹配xxxxx",
            ownedEpisodes: owned,
            noteLookup: { listenNotes.note(for: $0) }
        )
        let noFake = selfHit == nil
        steps.append([
            "name": "H_library_hit_owned_rationale",
            "bridgeHitOk": bridgeHitOk,
            "noFakeWithoutKeywords": noFake,
            "detail": bridgeDetail,
            "pass": bridgeHitOk && noFake,
        ])

        listenNotes.clear(episodeId: probeId)

        let pass = steps.allSatisfy { ($0["pass"] as? Bool) == true }
        let report: [String: Any] = [
            "startedAt": ISO8601DateFormatter().string(from: Date()),
            "probeEpisodeId": probeId,
            "libraryEpisodeCount": library.episodes.count,
            "uxBatch": "listen-note-downstream-2026-11-02",
            "steps": steps,
            "pass": pass,
        ]
        write(report)
    }

    private static func resetPromptDismissed(_ store: ListenNoteStore, episodeId: String) {
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(ListenNoteStore.fileName)
        guard let data = try? Data(contentsOf: url),
              var obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        var dismissed = obj["prompt_dismissed"] as? [String: Any] ?? [:]
        dismissed.removeValue(forKey: episodeId)
        obj["prompt_dismissed"] = dismissed
        if let out = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]) {
            try? out.write(to: url, options: .atomic)
            store.reload()
        }
    }

    private static func write(_ report: [String: Any]) {
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("listen-note-probe.json")
        guard let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) else {
            return
        }
        try? data.write(to: url, options: .atomic)
    }
}
#endif
