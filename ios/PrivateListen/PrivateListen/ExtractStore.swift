import Combine
import Foundation

/// 按 episode 独立 `extract.json`（§13.2）；星标触发深抽；听完 light；取消星标撤 queued。
/// 已就绪的要点同时记一份本机 LKG。库刷新 / iCloud 占位 / 失败壳不能把详情打回「点星开始提炼」。
/// 无 Live API key 时不跑 MockExtractor，也不把占位四条写成 ready deep。
@MainActor
final class ExtractStore: ObservableObject {
    @Published private(set) var packets: [String: ExtractionPacket] = [:]
    @Published private(set) var starred: Set<String> = []
    /// 正在重抽、但界面仍展示上一包要点时为 true。
    @Published private(set) var refreshing: Set<String> = []
    @Published var banner: String?

    private let starKey = "PrivateListen.starred"
    private var runningTasks: [String: Task<Void, Never>] = [:]
    private var jobs: [String: ExtractionPacket] = [:]
    private var jobTokens: [String: UUID] = [:]
    /// 最近一次可展示的包（进程内 + Application Support）。杀进程后 iCloud 读失败仍用它。
    private var lastKnownReady: [String: ExtractionPacket] = [:]
    private let listBadgeDensityCap = 3

    init() {
        if let data = UserDefaults.standard.data(forKey: starKey),
           let ids = try? JSONDecoder().decode([String].self, from: data) {
            starred = Set(ids)
        }
        loadPersistedLKG()
    }

    func isStarred(_ id: String) -> Bool { starred.contains(id) }

    /// 详情和徽章用的包：排队/失败壳让路给仍在的就绪包或 LKG。
    func packet(for id: String) -> ExtractionPacket? {
        ExtractDisplay.merge(disk: nil, memory: packets[id], lkg: lastKnownReady[id])
    }

    func badge(for id: String, densityUsed: inout Int) -> String? {
        guard let packet = packet(for: id) else { return nil }
        switch packet.status {
        case .stale: return "需重抽"
        case .queued: return "提炼排队"
        case .running: return "提炼中"
        case .failed:
            if packet.failureReason == "cancelled_unstar" { return nil }
            if packet.failureReason == "no_transcript" { return "缺译稿" }
            return "提炼失败"
        case .ready:
            if ExtractDisplay.isPlaceholderDeep(packet) { return "提炼失败" }
            if packet.mode == .light { return nil }
            let n = packet.visiblePointCount
            if n <= 0 { return "已提炼" }
            if densityUsed >= listBadgeDensityCap { return "已提炼" }
            densityUsed += 1
            return "\(n)要点"
        }
    }

    func load(episode: Episode) {
        let disk = readPacket(in: episode.audioURL.deletingLastPathComponent())
        guard let merged = ExtractDisplay.merge(
            disk: disk,
            memory: packets[episode.id],
            lkg: lastKnownReady[episode.id]
        ) else { return }
        rememberIfShowable(episode.id, merged)
        packets[episode.id] = merged
    }

    func reloadAll(episodes: [Episode]) {
        let reads = episodes.map { episode in
            ExtractDisplay.DiskRead(
                id: episode.id,
                packet: readPacket(in: episode.audioURL.deletingLastPathComponent())
            )
        }
        let merged = ExtractDisplay.reload(previous: packets, lkg: lastKnownReady, reads: reads)
        for (id, packet) in merged {
            rememberIfShowable(id, packet)
        }
        packets = merged
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
            let failed = failedPacket(
                episode: episode,
                schema: resolved.schemaId,
                reason: "no_transcript",
                summary: "无译稿，无法深抽。请先完成听译。"
            )
            banner = failed.summaryLine
            publishIfNoShowable(episode.id, failed, folder: folder)
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
        beginJob(episode: episode, queued: queued, folder: folder, schema: resolved.schemaId)
    }

    func unstar(episode: Episode) {
        starred.remove(episode.id)
        persistStars()
        jobTokens[episode.id] = nil
        jobs[episode.id] = nil
        refreshing.remove(episode.id)
        runningTasks[episode.id]?.cancel()
        runningTasks[episode.id] = nil
        guard var packet = packets[episode.id] else { return }
        if packet.mode == .deep || packet.mode == .manual {
            if packet.status == .queued || packet.status == .running {
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
    /// 已有 deep 要点时不写轻摘，避免盖掉 `extract.json`。
    func maybeLightOnFinished(episode: Episode, seconds: Double, duration: Double) {
        guard duration > 1, seconds >= max(0, duration - 15) else { return }
        if let existing = packet(for: episode.id) {
            if existing.mode != .light, ExtractDisplay.isShowable(existing) { return }
            if existing.status == .queued || existing.status == .running { return }
            if existing.mode == .light, existing.status == .ready { return }
        }
        let folder = episode.audioURL.deletingLastPathComponent()
        if let disk = readPacket(in: folder), disk.mode != .light, ExtractDisplay.isShowable(disk) {
            rememberIfShowable(episode.id, disk)
            packets[episode.id] = disk
            return
        }
        let schema = ExtractSchemaMap.resolve(lensIds: []).schemaId
        let light = MockExtractor.lightSummary(workId: episode.id, schema: schema, title: episode.title)
        guard ExtractDisplay.shouldReplaceFile(existing: lastKnownReady[episode.id] ?? packets[episode.id], incoming: light) else {
            return
        }
        packets[episode.id] = light
        save(light, folder: folder)
    }

    /// 详情「本集改主题重抽」。
    func reextract(episode: Episode, schema: ExtractSchemaID) {
        if var old = packets[episode.id] {
            old.status = .stale
            packets[episode.id] = old
            rememberIfShowable(episode.id, old)
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
        rememberIfShowable(episode.id, packet)
        save(packet, folder: episode.audioURL.deletingLastPathComponent())
    }

    func exportMarkdown(for episode: Episode) -> String? {
        guard let packet = packet(for: episode.id) else { return nil }
        return Self.markdown(from: packet)
    }

    // MARK: - private

    private func beginJob(episode: Episode, queued: ExtractionPacket, folder: URL, schema: ExtractSchemaID) {
        let token = UUID()
        jobTokens[episode.id] = token
        jobs[episode.id] = queued
        refreshing.insert(episode.id)
        publishIfNoShowable(episode.id, queued, folder: folder)
        runDeep(episode: episode, schema: schema, token: token)
    }

    /// 没有可展示要点时才把排队/失败壳放上界面并落盘。已有要点则继续展示，不改 `extract.json`。
    private func publishIfNoShowable(_ id: String, _ shell: ExtractionPacket, folder: URL) {
        let display = ExtractDisplay.merge(disk: nil, memory: packets[id], lkg: lastKnownReady[id])
        if let display, ExtractDisplay.isShowable(display) {
            packets[id] = display
            return
        }
        packets[id] = shell
        save(shell, folder: folder)
        if let kept = lastKnownReady[id], ExtractDisplay.isShowable(kept) {
            packets[id] = kept
        }
    }

    private func runDeep(episode: Episode, schema: ExtractSchemaID, token: UUID) {
        runningTasks[episode.id]?.cancel()
        let folder = episode.audioURL.deletingLastPathComponent()
        runningTasks[episode.id] = Task { @MainActor in
            guard jobTokens[episode.id] == token, var job = jobs[episode.id] else { return }
            job.status = .running
            jobs[episode.id] = job
            publishIfNoShowable(episode.id, job, folder: folder)
            do {
                if Task.isCancelled { throw ExtractRuntimeError.cancelled }
                guard case .live(let key) = ExtractorGate.route(apiKey: ExtractorGate.apiKey) else {
                    throw ExtractRuntimeError.missingApiKey
                }
                let transcript = Self.readTranscript(in: folder) ?? Self.mockTranscript(for: schema)
                let extractor: any EpisodeExtracting = LiveExtractor(apiKey: key)
                var ready = try await extractor.deepExtract(
                    workId: episode.id,
                    schema: schema,
                    transcript: transcript,
                    durationSec: episode.durationSec ?? 600
                )
                guard jobTokens[episode.id] == token else { return }
                if Task.isCancelled {
                    jobs[episode.id] = nil
                    jobTokens[episode.id] = nil
                    refreshing.remove(episode.id)
                    return
                }
                guard ExtractDisplay.readyDeepForDisk(apiKey: key, packet: ready) != nil else {
                    throw ExtractRuntimeError.missingApiKey
                }
                if let old = packet(for: episode.id),
                   !ExtractDisplay.isPlaceholderDeep(old),
                   old.status == .ready || old.status == .stale {
                    ready = Self.merge(old: old, incoming: ready)
                }
                guard ExtractDisplay.readyDeepForDisk(apiKey: key, packet: ready) != nil else {
                    throw ExtractRuntimeError.missingApiKey
                }
                jobs[episode.id] = nil
                jobTokens[episode.id] = nil
                refreshing.remove(episode.id)
                rememberIfShowable(episode.id, ready)
                packets[episode.id] = ready
                save(ready, folder: folder)
            } catch is CancellationError {
                guard jobTokens[episode.id] == token else { return }
                jobs[episode.id] = nil
                jobTokens[episode.id] = nil
                refreshing.remove(episode.id)
            } catch {
                guard jobTokens[episode.id] == token else { return }
                jobs[episode.id] = nil
                jobTokens[episode.id] = nil
                refreshing.remove(episode.id)
                let reason: String
                switch error as? ExtractRuntimeError {
                case .noTranscript:
                    reason = "no_transcript"
                case .missingApiKey:
                    reason = ExtractDisplay.missingLiveKeyReason
                default:
                    reason = "error"
                }
                let failed = failedPacket(
                    episode: episode,
                    schema: schema,
                    reason: reason,
                    summary: error.localizedDescription
                )
                banner = failed.summaryLine
                publishIfNoShowable(episode.id, failed, folder: folder)
            }
        }
    }

    private func failedPacket(episode: Episode, schema: ExtractSchemaID, reason: String, summary: String) -> ExtractionPacket {
        ExtractionPacket(
            packetId: "\(episode.id)-deep-fail",
            workId: episode.id,
            schemaId: schema,
            mode: .deep,
            status: .failed,
            createdAt: ISO8601DateFormatter().string(from: Date()),
            summaryLine: summary,
            failureReason: reason,
            claims: [], methods: [], entities: [], quotes: [], openQuestions: []
        )
    }

    /// 先读 `extract.json`，没有可展示要点时再看 `extract.live.json`。
    /// 排队/失败壳不会压过另一份里已经就绪的 deep。
    private func readPacket(in folder: URL) -> ExtractionPacket? {
        let canonical = readOne(folder.appendingPathComponent("extract.json"))
        let live = readOne(folder.appendingPathComponent("extract.live.json"))
        return ExtractDisplay.merge(disk: canonical, memory: live, lkg: nil)
    }

    private func readOne(_ url: URL) -> ExtractionPacket? {
        if Self.cloudWaiting(url) {
            try? FileManager.default.startDownloadingUbiquitousItem(at: url)
        }
        guard let data = Self.coordinatedData(url) else { return nil }
        return ExtractPacketDecoding.decode(data)
    }

    private static func cloudWaiting(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.ubiquitousItemDownloadingStatusKey]),
              let status = values.ubiquitousItemDownloadingStatus else {
            return false
        }
        return status != .current
    }

    private static func coordinatedData(_ url: URL) -> Data? {
        let coordinator = NSFileCoordinator()
        var result: Data?
        var error: NSError?
        coordinator.coordinate(readingItemAt: url, options: [], error: &error) { readURL in
            result = try? Data(contentsOf: readURL)
        }
        if result == nil {
            result = try? Data(contentsOf: url)
        }
        if let result, result.isEmpty { return nil }
        return result
    }

    private func save(_ packet: ExtractionPacket, folder: URL) {
        let id = folder.lastPathComponent
        let onDisk = readPacket(in: folder)
        let remembered = lastKnownReady[id]
        if ExtractDisplay.blocksFileWrite(disk: onDisk, remembered: remembered, incoming: packet) {
            if let onDisk { rememberIfShowable(id, onDisk) }
            if let remembered { rememberIfShowable(id, remembered) }
            return
        }
        let url = folder.appendingPathComponent("extract.json")
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(packet) else { return }
        try? data.write(to: url, options: .atomic)
        rememberIfShowable(id, packet)
    }

    private func rememberIfShowable(_ id: String, _ packet: ExtractionPacket) {
        guard EpisodeID.isSafe(id), ExtractDisplay.isShowable(packet) else { return }
        if packet.mode == .light,
           let existing = lastKnownReady[id],
           existing.mode != .light,
           ExtractDisplay.isShowable(existing) {
            return
        }
        if lastKnownReady[id] == packet { return }
        lastKnownReady[id] = packet
        guard let dir = lkgDirectory() else { return }
        let url = dir.appendingPathComponent("\(id).json")
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(packet) else { return }
        try? data.write(to: url, options: .atomic)
    }

    private func loadPersistedLKG() {
        guard let dir = lkgDirectory(),
              let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return }
        for url in files where url.pathExtension == "json" {
            let id = url.deletingPathExtension().lastPathComponent
            guard EpisodeID.isSafe(id),
                  let data = try? Data(contentsOf: url),
                  let packet = ExtractPacketDecoding.decode(data),
                  ExtractDisplay.isShowable(packet) else { continue }
            lastKnownReady[id] = packet
            if packets[id] == nil {
                packets[id] = packet
            }
        }
    }

    private func lkgDirectory() -> URL? {
        guard let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let dir = base.appendingPathComponent("PrivateListenExtractLKG", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        } catch {
            return nil
        }
        return dir
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
