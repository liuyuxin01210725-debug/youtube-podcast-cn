import Foundation

/// 按 episode 独立 `extract.json`（§13.2）；星标触发深抽；听完 light；取消星标撤 queued。
@MainActor
final class ExtractStore: ObservableObject {
        @Published private(set) var packets: [String: ExtractionPacket] = [:]
    @Published private(set) var starred: Set<String> = []
    @Published var banner: String?

    private let starKey = "PrivateListen.starred"
    private var runningTasks: [String: Task<Void, Never>] = [:]
    private let listBadgeDensityCap = 3

    init() {
        if let data = UserDefaults.standard.data(forKey: starKey),
           let ids = try? JSONDecoder().decode([String].self, from: data) {
            starred = Set(ids)
        }
    }

    func isStarred(_ id: String) -> Bool { starred.contains(id) }

    func packet(for id: String) -> ExtractionPacket? { packets[id] }

    func badge(for id: String, densityUsed: inout Int) -> String? {
        guard let packet = packets[id] else { return nil }
        switch packet.status {
        case .stale: return "需重抽"
        case .queued: return "提炼排队"
        case .running: return "提炼中"
        case .failed:
            if packet.failureReason == "cancelled_unstar" { return nil }
            if packet.failureReason == "no_transcript" { return "缺译稿" }
            return "提炼失败"
        case .ready:
            if packet.mode == .light { return nil }
            let n = packet.visiblePointCount
            if n <= 0 { return "已提炼" }
            if densityUsed >= listBadgeDensityCap { return "已提炼" }
            densityUsed += 1
            return "\(n)要点"
        }
    }

    func load(from episodeFolder: URL) {
        let id = episodeFolder.lastPathComponent
        let url = episodeFolder.appendingPathComponent("extract.json")
        guard let data = try? Data(contentsOf: url),
              let packet = try? JSONDecoder().decode(ExtractionPacket.self, from: data) else { return }
        packets[id] = packet
    }

    func reloadAll(episodes: [Episode]) {
        var next: [String: ExtractionPacket] = [:]
        for episode in episodes {
            let folder = episode.audioURL.deletingLastPathComponent()
            let url = folder.appendingPathComponent("extract.json")
            if let data = try? Data(contentsOf: url),
               let packet = try? JSONDecoder().decode(ExtractionPacket.self, from: data) {
                next[episode.id] = packet
            } else if let existing = packets[episode.id] {
                next[episode.id] = existing
            }
        }
        packets = next
    }

    /// 星标 → deep（有译稿才跑）。取消星标且 queued/running → 撤销不扣满额。
    func toggleStar(episode: Episode, lensIds: [String] = [], schemaOverride: ExtractSchemaID? = nil) {
        if starred.contains(episode.id) {
            unstar(episode: episode)
        } else {
            star(episode: episode, lensIds: lensIds, schemaOverride: schemaOverride)
        }
    }

    func star(episode: Episode, lensIds: [String] = [], schemaOverride: ExtractSchemaID? = nil) {
        starred.insert(episode.id)
        persistStars()
        let resolved = ExtractSchemaMap.resolve(lensIds: lensIds, explicit: schemaOverride)
        let folder = episode.audioURL.deletingLastPathComponent()
        let hasTranscript = Self.hasTranscript(in: folder)
        guard hasTranscript else {
            let failed = ExtractionPacket(
                packetId: "\(episode.id)-deep-fail",
                workId: episode.id,
                schemaId: resolved.schemaId,
                mode: .deep,
                status: .failed,
                createdAt: ISO8601DateFormatter().string(from: Date()),
                summaryLine: "无译稿，无法深抽。请先完成听译。",
                failureReason: "no_transcript",
                claims: [], methods: [], entities: [], quotes: [], openQuestions: []
            )
            packets[episode.id] = failed
            save(failed, folder: folder)
            banner = failed.summaryLine
            return
        }
        let queued = ExtractionPacket(
            packetId: "\(episode.id)-deep-q",
            workId: episode.id,
            schemaId: resolved.schemaId,
            mode: .deep,
            status: .queued,
            createdAt: ISO8601DateFormatter().string(from: Date()),
            summaryLine: "深抽排队中",
            degradedFrom: resolved.degradedFrom?.rawValue,
            claims: [], methods: [], entities: [], quotes: [], openQuestions: [],
            secondarySchemas: resolved.secondary.map(\.rawValue)
        )
        packets[episode.id] = queued
        save(queued, folder: folder)
        runDeep(episode: episode, schema: resolved.schemaId)
    }

    func unstar(episode: Episode) {
        starred.remove(episode.id)
        persistStars()
        guard var packet = packets[episode.id] else { return }
        if packet.mode == .deep || packet.mode == .manual {
            if packet.status == .queued || packet.status == .running {
                runningTasks[episode.id]?.cancel()
                runningTasks[episode.id] = nil
                packet.status = .failed
                packet.failureReason = "cancelled_unstar"
                packet.costUnits = 0
                packet.summaryLine = "已取消星标，深抽任务已撤销"
                packets[episode.id] = packet
                save(packet, folder: episode.audioURL.deletingLastPathComponent())
            }
        }
    }

    /// 听完近结尾（沿用 duration-15s）→ light；轻摘一览，点星可深抽。
    func maybeLightOnFinished(episode: Episode, seconds: Double, duration: Double) {
        guard duration > 1, seconds >= max(0, duration - 15) else { return }
        if let existing = packets[episode.id], existing.mode == .deep, existing.status == .ready || existing.status == .queued || existing.status == .running {
            return
        }
        if let existing = packets[episode.id], existing.mode == .light, existing.status == .ready {
            return
        }
        let schema = ExtractSchemaMap.resolve(lensIds: []).schemaId
        let light = MockExtractor.lightSummary(workId: episode.id, schema: schema, title: episode.title)
        packets[episode.id] = light
        save(light, folder: episode.audioURL.deletingLastPathComponent())
    }

    /// 详情「本集改主题重抽」。
    func reextract(episode: Episode, schema: ExtractSchemaID) {
        if var old = packets[episode.id] {
            old.status = .stale
            packets[episode.id] = old
            save(old, folder: episode.audioURL.deletingLastPathComponent())
        }
        if !starred.contains(episode.id) {
            starred.insert(episode.id)
            persistStars()
        }
        star(episode: episode, schemaOverride: schema)
    }

    func setItemStatus(episode: Episode, itemId: String, status: ItemStatus) {
        guard var packet = packets[episode.id] else { return }
        packet.claims = packet.claims.map { var x = $0; if x.id == itemId { x.status = status }; return x }
        packet.methods = packet.methods.map { var x = $0; if x.id == itemId { x.status = status }; return x }
        packet.entities = packet.entities.map { var x = $0; if x.id == itemId { x.status = status }; return x }
        packet.quotes = packet.quotes.map { var x = $0; if x.id == itemId { x.status = status }; return x }
        packet.openQuestions = packet.openQuestions.map { var x = $0; if x.id == itemId { x.status = status }; return x }
        packet.pendingConfirmCount = packet.computedPendingCount
        packets[episode.id] = packet
        save(packet, folder: episode.audioURL.deletingLastPathComponent())
    }

    func exportMarkdown(for episode: Episode) -> String? {
        guard let packet = packets[episode.id] else { return nil }
        return Self.markdown(from: packet)
    }

    // MARK: - private

    private func runDeep(episode: Episode, schema: ExtractSchemaID) {
        runningTasks[episode.id]?.cancel()
        let folder = episode.audioURL.deletingLastPathComponent()
        runningTasks[episode.id] = Task { @MainActor in
            guard var packet = packets[episode.id], packet.status == .queued else { return }
            packet.status = .running
            packets[episode.id] = packet
            save(packet, folder: folder)
            let transcript = Self.readTranscript(in: folder) ?? Self.mockTranscript(for: schema)
            do {
                if Task.isCancelled { throw ExtractRuntimeError.cancelled }
                let extractor: any EpisodeExtracting = {
                    if let key = ExtractorGate.apiKey, !key.isEmpty {
                        return LiveExtractor(apiKey: key)
                    }
                    return MockExtractor()
                }()
                var ready = try await extractor.deepExtract(
                    workId: episode.id,
                    schema: schema,
                    transcript: transcript,
                    durationSec: episode.durationSec ?? 600
                )
                if Task.isCancelled {
                    // 取消星标路径会先写 cancelled；此处不覆盖 ready。
                    if packets[episode.id]?.failureReason == "cancelled_unstar" { return }
                }
                // merge 保留旧确认
                if let old = packets[episode.id], old.status == .ready {
                    ready = Self.merge(old: old, incoming: ready)
                }
                packets[episode.id] = ready
                save(ready, folder: folder)
            } catch is CancellationError {
                return
            } catch {
                var failed = packets[episode.id] ?? ExtractionPacket(
                    packetId: "\(episode.id)-err", workId: episode.id, schemaId: schema,
                    mode: .deep, status: .failed,
                    createdAt: ISO8601DateFormatter().string(from: Date()),
                    claims: [], methods: [], entities: [], quotes: [], openQuestions: []
                )
                failed.status = .failed
                failed.failureReason = (error as? ExtractRuntimeError) == .noTranscript ? "no_transcript" : "error"
                failed.summaryLine = error.localizedDescription
                packets[episode.id] = failed
                save(failed, folder: folder)
                banner = failed.summaryLine
            }
        }
    }

    private func save(_ packet: ExtractionPacket, folder: URL) {
        let url = folder.appendingPathComponent("extract.json")
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(packet) else { return }
        try? data.write(to: url, options: .atomic)
    }

    private func persistStars() {
        let ids = Array(starred).sorted()
        if let data = try? JSONEncoder().encode(ids) {
            UserDefaults.standard.set(data, forKey: starKey)
        }
    }

    nonisolated static func hasTranscript(in folder: URL) -> Bool {
        let candidates = [
            "transcript_zh.json", "transcript.json", "translation.json",
            "translations.json", "mock_transcript.txt"
        ]
        let fm = FileManager.default
        for name in candidates {
            let url = folder.appendingPathComponent(name)
            if fm.fileExists(atPath: url.path) { return true }
        }
        // MVP 冒烟：若存在 `.allow_mock_extract` 或 DEBUG 默认允许 mock 译稿
        if fm.fileExists(atPath: folder.appendingPathComponent(".allow_mock_extract").path) {
            return true
        }
        #if DEBUG
        return true
        #else
        return false
        #endif
    }

    nonisolated static func readTranscript(in folder: URL) -> String? {
        let txt = folder.appendingPathComponent("mock_transcript.txt")
        if let data = try? Data(contentsOf: txt), let s = String(data: data, encoding: .utf8), !s.isEmpty {
            return s
        }
        for name in ["transcript_zh.json", "transcript.json"] {
            let url = folder.appendingPathComponent(name)
            if let data = try? Data(contentsOf: url),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let text = obj["text"] as? String, !text.isEmpty { return text }
                if let segs = obj["segments"] as? [[String: Any]] {
                    let joined = segs.compactMap { $0["zh"] as? String ?? $0["text"] as? String }.joined()
                    if !joined.isEmpty { return joined }
                }
            }
        }
        return nil
    }

    nonisolated static func mockTranscript(for schema: ExtractSchemaID) -> String {
        switch schema {
        case .ferment:
            return "今天讲盐曲。盐曲按两斤盐一斤曲，密封室温发酵七天。米曲是小众原料可用于家用发酵。短时高盐比会压住杂菌风味更干净。勿用碘盐。"
        case .biz_invest:
            return "嘉宾判断未来两年推理成本下降会重塑应用层毛利。也提到 OpenAI 作为观察标的而非荐股。成本曲线这个框架很有用。"
        case .health:
            return "今天讨论肌酸补充。肌酸每日约5克，仅作记录非处方，配足量水。长期高剂量肌酸是否有额外收益仍有争议，不可当医嘱。"
        case .edu:
            return "AI 时代应先教提问与校对再教标准答案。双栏笔记左摘录右重述课后互评。"
        case .embodied:
            return "三分钟身体扫描从头到脚不做评判。注意力回到身体感受比直接改想法更稳。"
        case .general:
            return "收藏只是入口抽成可行动卡片才算拥有。本期嘉宾提到一个值得关注的人。也提到另一个节目值得候选关注。这套方法换到中文语境是否仍成立。"
        }
    }

    nonisolated static func merge(old: ExtractionPacket, incoming: ExtractionPacket) -> ExtractionPacket {
        var out = incoming
        func mergeList<T>(_ keyPath: WritableKeyPath<ExtractionPacket, [T]>, id: (T) -> String, status: WritableKeyPath<T, ItemStatus>) {
            let prev = Dictionary(uniqueKeysWithValues: old[keyPath: keyPath].map { (id($0), $0) })
            out[keyPath: keyPath] = out[keyPath: keyPath].map { item in
                var copy = item
                if let p = prev[id(item)] {
                    copy[keyPath: status] = p[keyPath: status]
                }
                return copy
            }
        }
        mergeList(\.claims, id: { $0.id }, status: \.status)
        mergeList(\.methods, id: { $0.id }, status: \.status)
        mergeList(\.entities, id: { $0.id }, status: \.status)
        mergeList(\.quotes, id: { $0.id }, status: \.status)
        mergeList(\.openQuestions, id: { $0.id }, status: \.status)
        if let actions = old.actions, !actions.isEmpty {
            out.actions = actions
        }
        out.pendingConfirmCount = out.computedPendingCount
        return out
    }

    nonisolated static func markdown(from packet: ExtractionPacket) -> String {
        var lines: [String] = [
            "# 抽取 · \(packet.workId)",
            "",
            "- schema: `\(packet.schemaId.rawValue)`",
            "- mode: `\(packet.mode.rawValue)`",
            "- status: `\(packet.status.rawValue)`",
            "- summary: \(packet.summaryLine ?? "")",
            ""
        ]
        if !packet.claims.isEmpty {
            lines.append("## 观点 Claim")
            lines.append("")
            for c in packet.claims {
                lines.append("- [\(c.status.rawValue)] \(c.text) (\(c.tStart ?? 0)–\(c.tEnd ?? 0)s)")
            }
            lines.append("")
        }
        if !packet.methods.isEmpty {
            lines.append("## 方法 Method")
            lines.append("")
            for m in packet.methods {
                lines.append("- [\(m.status.rawValue)] \(m.title) (\(m.tStart ?? 0)–\(m.tEnd ?? 0)s)")
            }
            lines.append("")
        }
        if !packet.entities.isEmpty {
            lines.append("## 实体 Entity")
            lines.append("")
            for e in packet.entities {
                lines.append("- [\(e.status.rawValue)] \(e.type) \(e.nameSrc)")
            }
            lines.append("")
        }
        return lines.joined(separator: "\n")
    }
}
