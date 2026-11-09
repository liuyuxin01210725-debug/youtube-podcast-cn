import Foundation

/// 听后感写完之后的本地下游：库内关联候选 + insights sidecar。
/// 硬闸：owned-only、可读理由、不自动入队/深抽/改 bridges.json；诚实弱智可解释。
enum ListenNoteDownstream {
    static let insightsFileName = "listen_note_insights.json"
    static let insightsSchema = "shengjian.listen_note_insights.v1"
    static let insightsSchemaVersion = 1

    /// 简单中文关键词表（诚实弱智；可解释）。
    static let keywordTable: [String] = [
        "小而美", "小众", "创业", "投资", "发酵", "品牌", "康普茶", "产线",
        "渠道", "CPG", "品类", "自建", "创始", "融资", "增长", "商业",
        "透镜", "待回看", "灵感", "研究",
    ]

    struct LibraryHit: Hashable, Identifiable {
        var id: String { episodeId }
        var episodeId: String
        var title: String
        var rationaleZh: String
        var source: String // "bridge" | "keyword"
    }

    struct InsightsDoc: Codable {
        var schema: String
        var schemaVersion: Int
        var updatedAt: String
        var insights: [String: Insight]

        enum CodingKeys: String, CodingKey {
            case schema
            case schemaVersion = "schema_version"
            case updatedAt = "updated_at"
            case insights
        }
    }

    struct Insight: Codable, Hashable {
        var episodeId: String
        var summary: String
        var body: String
        var keywords: [String]
        var updatedAt: String

        enum CodingKeys: String, CodingKey {
            case episodeId = "episode_id"
            case summary, body, keywords
            case updatedAt = "updated_at"
        }
    }

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static var insightsURL: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(insightsFileName)
    }

    static func extractKeywords(from text: String) -> [String] {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return [] }
        var hits: [String] = []
        var seen = Set<String>()
        for kw in keywordTable {
            if t.localizedCaseInsensitiveContains(kw), seen.insert(kw.lowercased()).inserted {
                hits.append(kw)
            }
        }
        return hits
    }

    /// 优先撞 bridges.json（from→to owned）；否则关键词扫库内标题/嘉宾/透镜/听后感。
    /// 最多 1 条；无合格候选返回 nil。不入队、不改 bridges。
    static func findLibraryHit(
        currentEpisodeId: String,
        noteText: String,
        ownedEpisodes: [Episode],
        noteLookup: (String) -> ListenNote?
    ) -> LibraryHit? {
        let ownedIds = Set(ownedEpisodes.map(\.id))
        let byId = Dictionary(uniqueKeysWithValues: ownedEpisodes.map { ($0.id, $0) })

        // 1) bridges from current → owned to（优先策展 + strength）
        let bridgePool = LensBridgeCatalog.loadAll().filter { bridge in
            guard bridge.fromEpisodeId == currentEpisodeId else { return false }
            guard bridge.isPushable else { return false }
            guard ownedIds.contains(bridge.toEpisodeId) else { return false }
            return true
        }
        .sorted { lhs, rhs in
            if lhs.curated != rhs.curated { return lhs.curated && !rhs.curated }
            if lhs.strength != rhs.strength { return lhs.strength > rhs.strength }
            return lhs.id < rhs.id
        }
        if let best = bridgePool.first, let ep = byId[best.toEpisodeId] {
            let kw = extractKeywords(from: noteText)
            // 若有关键词，优先选与关键词/商业叙事更贴的桥；否则取最强桥
            if !kw.isEmpty {
                let scored = bridgePool.compactMap { bridge -> (LensBridge, Int)? in
                    guard let target = byId[bridge.toEpisodeId] else { return nil }
                    var score = bridge.strength * 10 + (bridge.curated ? 5 : 0)
                    let hay = (
                        bridge.rationaleZh + " " + target.title + " "
                            + target.guests.joined(separator: " ") + " "
                            + target.effectiveLensIds.joined(separator: " ")
                            + " " + (noteLookup(target.id)?.listSummary ?? "")
                    )
                    for k in kw where hay.localizedCaseInsensitiveContains(k) {
                        score += 20
                    }
                    // 听后感提到创业/投资/商业 → 抬高 business 侧
                    let bizHints = ["创业", "投资", "商业", "小而美", "小众", "品牌", "CPG"]
                    if kw.contains(where: { bizHints.contains($0) })
                        && target.effectiveLensIds.contains(where: {
                            $0.contains("business") || $0.contains("invest")
                        }) {
                        score += 15
                    }
                    return (bridge, score)
                }
                .sorted { $0.1 > $1.1 }
                if let pick = scored.first, let ep2 = byId[pick.0.toEpisodeId] {
                    return LibraryHit(
                        episodeId: ep2.id,
                        title: ep2.title,
                        rationaleZh: pick.0.rationaleZh,
                        source: "bridge"
                    )
                }
            }
            return LibraryHit(
                episodeId: ep.id,
                title: ep.title,
                rationaleZh: best.rationaleZh,
                source: "bridge"
            )
        }

        // 2) 关键词扫其他 owned 集（同透镜 / 标题嘉宾 / 对方听后感）
        let keywords = extractKeywords(from: noteText)
        guard !keywords.isEmpty else { return nil }

        var best: (LibraryHit, Int)?
        for ep in ownedEpisodes where ep.id != currentEpisodeId {
            var score = 0
            var matched: [String] = []
            let hay = (
                ep.title + " " + ep.showName + " " + ep.guests.joined(separator: " ")
                    + " " + ep.effectiveLensIds.joined(separator: " ")
                    + " " + (noteLookup(ep.id)?.listSummary ?? "")
                    + " " + (noteLookup(ep.id)?.body ?? "")
                    + " " + (noteLookup(ep.id)?.tags.joined(separator: " ") ?? "")
            )
            for k in keywords where hay.localizedCaseInsensitiveContains(k) {
                score += 10
                matched.append(k)
            }
            // 同透镜轻加分
            if let cur = byId[currentEpisodeId] {
                let overlap = Set(cur.effectiveLensIds).intersection(Set(ep.effectiveLensIds))
                if !overlap.isEmpty { score += 5 }
            }
            // 商业叙事关键词 → business lens
            let bizHints = ["创业", "投资", "商业", "小而美", "小众", "品牌", "CPG"]
            if keywords.contains(where: { bizHints.contains($0) })
                && ep.effectiveLensIds.contains(where: {
                    $0.contains("business") || $0.contains("invest")
                }) {
                score += 12
            }
            let fermentHints = ["发酵", "康普茶"]
            if keywords.contains(where: { fermentHints.contains($0) })
                && ep.effectiveLensIds.contains(where: { $0.contains("ferment") || $0.contains("food") }) {
                score += 12
            }
            guard score >= 10, !matched.isEmpty else { continue }
            let rationale = "听后感提到「\(matched.prefix(3).joined(separator: "、"))」；库内本集标题/透镜/嘉宾有重合"
            let hit = LibraryHit(
                episodeId: ep.id,
                title: ep.title,
                rationaleZh: rationale,
                source: "keyword"
            )
            if best == nil || score > best!.1 {
                best = (hit, score)
            }
        }
        return best?.0
    }

    /// 写入 Documents/listen_note_insights.json（供声间 bot / 研究底稿扫描）。不上传、不改 bridges。
    @discardableResult
    static func writeInsight(episodeId: String, noteText: String) -> Insight? {
        let body = noteText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return nil }
        let summary = summaryLine(body)
        let keywords = extractKeywords(from: body)
        let now = iso.string(from: Date())
        var doc = loadInsightsDoc()
        let insight = Insight(
            episodeId: episodeId,
            summary: summary,
            body: body,
            keywords: keywords,
            updatedAt: now
        )
        doc.insights[episodeId] = insight
        doc.updatedAt = now
        persistInsights(doc)
        UIEventLog.note(
            "listen_note_insight_write",
            episodeId: episodeId,
            fields: [
                "summary": String(summary.prefix(48)),
                "keyword_count": "\(keywords.count)",
            ]
        )
        return insight
    }

    static func insight(for episodeId: String) -> Insight? {
        loadInsightsDoc().insights[episodeId]
    }

    static func isShareExcluded(_ name: String) -> Bool {
        name == insightsFileName || name == "listen-note-probe.json"
    }

    // MARK: - helpers

    /// 正文首行（不截断；用于 headline 兼容字段）。
    static func firstLine(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let first = trimmed
            .split(whereSeparator: \.isNewline)
            .map(String.init)
            .first(where: { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty })
        return (first ?? trimmed).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// insights summary：首行截断。
    static func summaryLine(_ text: String) -> String {
        let line = firstLine(text)
        if line.count <= 48 { return line }
        let idx = line.index(line.startIndex, offsetBy: 48)
        return String(line[..<idx]) + "…"
    }

    /// 旧 headline+body 合并成单字段展示；headline 已是 body 首行则不重复。
    static func mergedDisplayText(headline: String, body: String) -> String {
        let h = headline.trimmingCharacters(in: .whitespacesAndNewlines)
        let b = body.trimmingCharacters(in: .whitespacesAndNewlines)
        if h.isEmpty { return b }
        if b.isEmpty { return h }
        let first = b
            .split(whereSeparator: \.isNewline)
            .map(String.init)
            .first?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if first.caseInsensitiveCompare(h) == .orderedSame { return b }
        if b.hasPrefix(h) { return b }
        return h + "\n" + b
    }

    private static func loadInsightsDoc() -> InsightsDoc {
        if let data = try? Data(contentsOf: insightsURL),
           let doc = try? JSONDecoder().decode(InsightsDoc.self, from: data) {
            return doc
        }
        return InsightsDoc(
            schema: insightsSchema,
            schemaVersion: insightsSchemaVersion,
            updatedAt: iso.string(from: Date()),
            insights: [:]
        )
    }

    private static func persistInsights(_ doc: InsightsDoc) {
        var out = doc
        out.schema = insightsSchema
        out.schemaVersion = insightsSchemaVersion
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(out) else { return }
        let dir = insightsURL.deletingLastPathComponent()
        let temporary = dir.appendingPathComponent(
            ".listen_note_insights.json.\(ProcessInfo.processInfo.processIdentifier).tmp"
        )
        do {
            try data.write(to: temporary, options: .atomic)
            _ = try? FileManager.default.removeItem(at: insightsURL)
            try FileManager.default.moveItem(at: temporary, to: insightsURL)
        } catch {
            try? FileManager.default.removeItem(at: temporary)
        }
    }
}
