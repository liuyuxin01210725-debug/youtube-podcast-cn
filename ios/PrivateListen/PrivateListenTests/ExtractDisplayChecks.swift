import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// 不依赖模拟器：直接跑「已收藏 + 就绪包」在 reload / 失败读 / 空 CTA 上的规则，
/// 以及占位 mock 不得盖过真 deep / LKG、无 Live key 不得落成 ready。
/// `swiftc -parse-as-library`：
/// ExtractionModels.swift ExtractSchemaMap.swift ExtractDisplay.swift MockExtractor.swift 和本文件。
@main
struct ExtractDisplayChecks {
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

        let episode = "B-1103-03"
        let ready = packet(id: episode, mode: .deep, status: .ready, points: 10, packetId: "live-ready")
        let queued = packet(id: episode, mode: .deep, status: .queued, points: 0, packetId: "shell-q")
        let failed = packet(id: episode, mode: .deep, status: .failed, points: 0, packetId: "shell-f")
        let light = packet(id: episode, mode: .light, status: .ready, points: 0, packetId: "light")

        let keptWhenLibraryOmitsEpisode = ExtractDisplay.reload(
            previous: [episode: ready],
            lkg: [:],
            reads: []
        )
        check(
            "reload 清单短暂缺集时保留已就绪要点",
            keptWhenLibraryOmitsEpisode[episode]?.visiblePointCount == 10
        )

        let keptWhenReadFails = ExtractDisplay.reload(
            previous: [episode: ready],
            lkg: [episode: ready],
            reads: [ExtractDisplay.DiskRead(id: episode, packet: nil)]
        )
        check(
            "extract.json 读失败时不把就绪包换成空",
            keptWhenReadFails[episode]?.status == .ready
                && keptWhenReadFails[episode]?.visiblePointCount == 10
        )

        let keptWhenDiskIsQueuedShell = ExtractDisplay.reload(
            previous: [episode: ready],
            lkg: [episode: ready],
            reads: [ExtractDisplay.DiskRead(id: episode, packet: queued)]
        )
        check(
            "排队壳不能盖过已有要点",
            keptWhenDiskIsQueuedShell[episode]?.packetId == "live-ready"
        )

        let keptWhenDiskFails = ExtractDisplay.merge(disk: failed, memory: nil, lkg: ready)
        check(
            "失败包不能盖过 LKG",
            keptWhenDiskFails?.status == .ready && keptWhenDiskFails?.visiblePointCount == 10
        )

        let coldStart = ExtractDisplay.reload(
            previous: [:],
            lkg: [episode: ready],
            reads: [ExtractDisplay.DiskRead(id: episode, packet: nil)]
        )
        check(
            "杀进程后 iCloud 读失败仍用 LKG",
            coldStart[episode]?.visiblePointCount == 10
        )
        check(
            "已收藏且 LKG 就绪时不显示点星 CTA",
            ExtractDisplay.showsPromptToStar(isStarred: true, display: coldStart[episode]) == false
        )
        check(
            "已收藏但这一帧还没有包时也不是点星 CTA",
            ExtractDisplay.emptyCaption(isStarred: true) == .syncingStarred
                && ExtractDisplay.showsPromptToStar(isStarred: true, display: nil) == false
        )
        check(
            "未收藏且没有包仍是点星开始提炼",
            ExtractDisplay.showsPromptToStar(isStarred: false, display: nil)
                && ExtractDisplay.emptyCaption(isStarred: false) == .promptToStar
        )

        let lightDoesNotHideDeep = ExtractDisplay.merge(disk: light, memory: nil, lkg: ready)
        check("轻摘不能盖过 deep 要点", lightDoesNotHideDeep?.mode == .deep)

        let newer = packet(id: episode, mode: .deep, status: .ready, points: 12, packetId: "live-newer")
        let diskWins = ExtractDisplay.merge(disk: newer, memory: ready, lkg: ready)
        check("磁盘上新的就绪包仍然生效", diskWins?.packetId == "live-newer")

        let queuedStaysWhenNothingElse = ExtractDisplay.merge(disk: queued, memory: nil, lkg: nil)
        check("只有排队包时保留排队状态而不是 nil", queuedStaysWhenNothingElse?.status == .queued)

        let liveBesideShell = ExtractDisplay.merge(disk: queued, memory: ready, lkg: nil)
        check(
            "extract.json 是壳而 extract.live.json 已就绪时用 live",
            liveBesideShell?.packetId == "live-ready"
        )

        let invented = ExtractDisplay.reload(previous: [:], lkg: [:], reads: [])
        check("刷新不会凭空造出深抽包", invented.isEmpty)

        check(
            "排队/失败不得覆盖磁盘上的 deep 文件",
            ExtractDisplay.shouldReplaceFile(existing: ready, incoming: queued) == false
                && ExtractDisplay.shouldReplaceFile(existing: ready, incoming: failed) == false
                && ExtractDisplay.shouldReplaceFile(existing: ready, incoming: light) == false
                && ExtractDisplay.shouldReplaceFile(existing: ready, incoming: newer)
        )
        check(
            "第一次收藏仍可写入排队包",
            ExtractDisplay.shouldReplaceFile(existing: nil, incoming: queued)
        )

        let other = packet(id: "OTHER", mode: .deep, status: .ready, points: 2, packetId: "other")
        let partialSnapshot = ExtractDisplay.reload(
            previous: [episode: ready, "OTHER": other],
            lkg: [:],
            reads: [ExtractDisplay.DiskRead(id: "OTHER", packet: nil)]
        )
        check(
            "只刷新到别的集时本集要点还在",
            partialSnapshot[episode]?.visiblePointCount == 10
                && partialSnapshot["OTHER"]?.visiblePointCount == 2
        )

        check("严格 JSON 仍能解出要点", strictRoundTrip())
        check("health-longevity / 数字 params / 缺数组仍解出要点", looseLivePacket())

        let general = MockExtractor.build(
            workId: episode,
            schema: .general,
            transcript: "收藏只是入口抽成可行动卡片才算拥有。本期嘉宾提到一个值得关注的人。也提到另一个节目值得候选关注。这套方法换到中文语境是否仍成立。",
            durationSec: 600
        )
        check(
            "general 夹具就是截图那四条",
            general.visiblePointCount == 4
                && general.packetId.hasSuffix("-deep-mock")
                && general.claims.contains { $0.text == "收藏只是入口，抽成可行动卡片才算拥有。" }
                && general.entities.contains { $0.type == "Person" && $0.nameZh == "嘉宾" }
                && general.entities.contains { $0.type == "Show" && $0.nameSrc == "Some Show" }
                && general.openQuestions.contains { $0.text == "这套方法换到中文语境是否仍成立？" }
        )
        check(
            "无 key 的深抽路由拒绝 mock 成功",
            ExtractorGate.route(apiKey: nil) == .missingApiKey
                && ExtractorGate.route(apiKey: "") == .missingApiKey
                && ExtractorGate.route(apiKey: " \n") == .missingApiKey
        )
        check(
            "有 Live key 才走 live",
            ExtractorGate.route(apiKey: " sk-live ") == .live("sk-live")
        )

        for schema in ExtractSchemaID.allCases {
            let built = MockExtractor.build(
                workId: episode,
                schema: schema,
                transcript: "占位夹具",
                durationSec: 600
            )
            let kept = ExtractDisplay.merge(disk: built, memory: nil, lkg: ready)
            check(
                "\(schema.rawValue) mock 不能盖 LKG / 真 deep，无 key 不落盘",
                ExtractDisplay.isPlaceholderDeep(built)
                    && ExtractDisplay.isShowable(built) == false
                    && ExtractDisplay.shouldReplaceFile(existing: ready, incoming: built) == false
                    && ExtractDisplay.shouldReplaceFile(existing: nil, incoming: built) == false
                    && ExtractDisplay.readyDeepForDisk(apiKey: nil, packet: built) == nil
                    && ExtractDisplay.readyDeepForDisk(apiKey: "", packet: built) == nil
                    && ExtractDisplay.readyDeepForDisk(apiKey: " ", packet: built) == nil
                    && ExtractDisplay.readyDeepForDisk(apiKey: "sk-live", packet: built) == nil
                    && kept?.packetId == "live-ready"
                    && kept?.visiblePointCount == 10
                    && ExtractDisplay.detailStatusLabel(built) == "无法提炼"
                    && ExtractDisplay.detailStatusLabel(built).contains("已提炼") == false
            )
        }

        var renamed = general
        renamed.packetId = "\(episode)-deep-20261103T000000Z"
        let mockOnly = ExtractDisplay.merge(disk: renamed, memory: renamed, lkg: nil)
        check(
            "改掉 deep-mock id 后四条仍不能标成已提炼",
            ExtractDisplay.isPlaceholderDeep(renamed)
                && mockOnly?.visiblePointCount == 0
                && mockOnly?.claims.isEmpty == true
                && mockOnly?.entities.isEmpty == true
                && mockOnly?.openQuestions.isEmpty == true
                && mockOnly?.failureReason == ExtractDisplay.mockNotOfficialReason
                && mockOnly?.summaryLine == ExtractDisplay.missingLiveKeySummary
                && mockOnly.map { ExtractDisplay.detailStatusLabel($0) == "无法提炼" } == true
        )
        check(
            "内存里的 mock 也不能盖过 LKG",
            ExtractDisplay.merge(disk: nil, memory: general, lkg: ready)?.packetId == "live-ready"
        )
        let reloadedOverMock = ExtractDisplay.reload(
            previous: [episode: ready],
            lkg: [episode: ready],
            reads: [ExtractDisplay.DiskRead(id: episode, packet: general)]
        )
        check(
            "刷新读到 mock 文件时保留真要点",
            reloadedOverMock[episode]?.visiblePointCount == 10
                && reloadedOverMock[episode]?.packetId == "live-ready"
        )
        check(
            "空文件也不写入 mock ready",
            ExtractDisplay.blocksFileWrite(disk: nil, remembered: nil, incoming: general)
        )
        check(
            "磁盘已是 mock 时 LKG 真包仍挡住失败壳",
            ExtractDisplay.blocksFileWrite(disk: general, remembered: ready, incoming: failed)
        )
        check(
            "第一次排队包仍可写入空文件",
            ExtractDisplay.blocksFileWrite(disk: nil, remembered: nil, incoming: queued) == false
        )
        check(
            "有 Live key 的正式 deep 仍可落盘并替换",
            ExtractDisplay.readyDeepForDisk(apiKey: "sk-live", packet: newer)?.packetId == "live-newer"
                && ExtractDisplay.blocksFileWrite(disk: ready, remembered: ready, incoming: newer) == false
                && ExtractDisplay.shouldReplaceFile(existing: ready, incoming: newer)
        )
        let heard = MockExtractor.lightSummary(workId: episode, schema: .general, title: "与生物黑客")
        check("听完轻摘仍不是占位深包", ExtractDisplay.isPlaceholderDeep(heard) == false)
        check(
            "正式 ready 的状态行仍是已提炼",
            ExtractDisplay.detailStatusLabel(ready) == "已提炼 · 要点 10"
        )
        check(
            "普通失败状态行仍用摘要",
            ExtractDisplay.detailStatusLabel(failed) == failed.summaryLine
        )
        var missingKey = failed
        missingKey.failureReason = ExtractDisplay.missingLiveKeyReason
        missingKey.summaryLine = ExtractDisplay.missingLiveKeySummary
        check(
            "缺密钥失败不标已提炼",
            ExtractDisplay.detailStatusLabel(missingKey) == "无法提炼"
        )
        var partial = ready
        partial.claims.append(
            ExtractClaim(
                id: "c-extra",
                text: "收藏只是入口，抽成可行动卡片才算拥有。",
                stance: nil,
                span: nil,
                tStart: 1,
                tEnd: 2,
                confidence: 0.5,
                status: .candidate,
                defaultAction: nil,
                pointType: "claim_debate",
                lowConfidence: nil,
                attrs: nil
            )
        )
        check(
            "只含半句占位文案的真包仍可展示",
            ExtractDisplay.isPlaceholderDeep(partial) == false
                && ExtractDisplay.isShowable(partial)
                && ExtractDisplay.detailStatusLabel(partial).contains("已提炼")
        )

        if !failures.isEmpty {
            fputs("\(failures.count) failed: \(failures.joined(separator: ", "))\n", stderr)
            exit(1)
        }
        print("extract display checks passed")
    }

    private static func strictRoundTrip() -> Bool {
        let original = packet(id: "B-1103-03", mode: .deep, status: .ready, points: 3, packetId: "strict")
        let encoder = JSONEncoder()
        guard let data = try? encoder.encode(original),
              let decoded = ExtractPacketDecoding.decode(data) else { return false }
        return decoded.schemaId == .health
            && decoded.visiblePointCount == 3
            && decoded.status == .ready
    }

    private static func looseLivePacket() -> Bool {
        let json = """
        {"packet_id":"B-1103-03-live","work_id":"B-1103-03","schema_id":"health-longevity","mode":"deep","status":"ready","created_at":"2026-11-03T00:00:00Z","summary_line":"长寿要点","claims":[{"id":"c1","text":"高估了热量限制的收益","status":"candidate","t_start":12,"t_end":40}],"methods":[{"id":"m1","title":"肌酸记录","status":"candidate","params":{"剂量":5},"t_start":22,"t_end":48}]}
        """
        guard let data = json.data(using: .utf8),
              let decoded = ExtractPacketDecoding.decode(data) else { return false }
        let dose = decoded.methods.first?.params?["剂量"]
        return decoded.schemaId == .health
            && decoded.mode == .deep
            && decoded.visiblePointCount == 2
            && dose == "5"
            && decoded.claims.first?.text == "高估了热量限制的收益"
    }

    private static func packet(
        id: String,
        mode: ExtractMode,
        status: PacketStatus,
        points: Int,
        packetId: String
    ) -> ExtractionPacket {
        let claims: [ExtractClaim] = (0..<points).map { index in
            ExtractClaim(
                id: "c\(index)",
                text: "要点\(index)",
                stance: nil,
                span: nil,
                tStart: Double(index),
                tEnd: Double(index) + 1,
                confidence: 0.9,
                status: .candidate,
                defaultAction: nil,
                pointType: "claim_controversy",
                lowConfidence: nil,
                attrs: nil
            )
        }
        return ExtractionPacket(
            packetId: packetId,
            workId: id,
            schemaId: .health,
            mode: mode,
            status: status,
            createdAt: "2026-11-03T00:00:00Z",
            summaryLine: "已提炼",
            transcriptHash: nil,
            costUnits: 1,
            failureReason: status == .failed ? "error" : nil,
            degradedFrom: nil,
            claims: claims,
            methods: [],
            entities: [],
            quotes: [],
            openQuestions: []
        )
    }
}
