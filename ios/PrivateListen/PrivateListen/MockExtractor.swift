import Foundation

/// 抽取协议：无 API key 时走确定性 mock；真调用路径预留。
protocol EpisodeExtracting: Sendable {
    func deepExtract(
        workId: String,
        schema: ExtractSchemaID,
        transcript: String,
        durationSec: Double
    ) async throws -> ExtractionPacket
}

enum ExtractorGate {
    /// 环境变量 / Info 日后可接；MVP 无 key → mock。
    static var apiKey: String? {
        ProcessInfo.processInfo.environment["SHENGJIAN_EXTRACT_API_KEY"]
    }

    static var usesMock: Bool { apiKey?.isEmpty != false }
}

/// 确定性 mock：按 schema 产出合格类型卡（与 fixtures/extract 对齐）。
struct MockExtractor: EpisodeExtracting {
    func deepExtract(
        workId: String,
        schema: ExtractSchemaID,
        transcript: String,
        durationSec: Double
    ) async throws -> ExtractionPacket {
        // 小延迟模拟排队→running→ready，便于 UI 冒烟。
        try await Task.sleep(for: .milliseconds(80))
        return Self.build(workId: workId, schema: schema, transcript: transcript, durationSec: durationSec)
    }

    static func build(
        workId: String,
        schema: ExtractSchemaID,
        transcript: String,
        durationSec: Double
    ) -> ExtractionPacket {
        let hash = Self.hash(transcript)
        var packet = ExtractionPacket(
            packetId: "\(workId)-deep-mock",
            workId: workId,
            schemaId: schema,
            mode: .deep,
            status: .ready,
            createdAt: ISO8601DateFormatter().string(from: Date()),
            summaryLine: nil,
            transcriptHash: hash,
            costUnits: 1,
            failureReason: nil,
            degradedFrom: nil,
            claims: [],
            methods: [],
            entities: [],
            quotes: [],
            openQuestions: []
        )
        switch schema {
        case .ferment:
            packet.methods = [
                ExtractMethod(
                    id: "m-craft-1", title: "盐曲工艺",
                    steps: ["称盐与曲", "按 2:1 混合", "密封室温放置"],
                    params: ["盐比": "2:1", "温度": "室温", "时长": "7天"],
                    cautions: ["勿用碘盐"],
                    span: Self.span(in: transcript, needle: "盐曲", fallback: "盐曲按两斤盐一斤曲密封室温发酵"),
                    tStart: 40, tEnd: 75, confidence: 0.86, status: .candidate,
                    defaultAction: "ferment_note", pointType: "method_craft"
                )
            ]
            packet.entities = [
                ExtractEntity(
                    id: "e-prod-1", type: "Product", nameSrc: "koji", nameZh: "米曲",
                    span: Self.span(in: transcript, needle: "米曲", fallback: "米曲是小众原料可用于家用发酵"),
                    tStart: 80, tEnd: 95, confidence: 0.8, status: .candidate,
                    defaultAction: "try_later", pointType: "product_niche"
                )
            ]
            packet.claims = [
                ExtractClaim(
                    id: "c-flavor-1", text: "短时高盐比会压住杂菌、风味更干净。",
                    stance: "neutral",
                    span: Self.span(in: transcript, needle: "风味", fallback: "短时高盐比会压住杂菌风味更干净"),
                    tStart: 100, tEnd: 120, confidence: 0.78, status: .candidate,
                    defaultAction: "debate", pointType: "claim_flavor"
                )
            ]
            packet.summaryLine = "抽出盐曲工艺与米曲原料"
        case .biz_invest:
            packet.claims = [
                ExtractClaim(
                    id: "c-outlook-1",
                    text: "未来两年推理成本下降会重塑应用层毛利。",
                    stance: "bullish",
                    span: Self.span(in: transcript, needle: "推理成本", fallback: "未来两年推理成本下降会重塑应用层毛利"),
                    tStart: 30, tEnd: 55, confidence: 0.84, status: .candidate,
                    defaultAction: "monitor_candidate", pointType: "claim_outlook"
                )
            ]
            packet.entities = [
                ExtractEntity(
                    id: "e-org-1", type: "Org", nameSrc: "OpenAI", nameZh: nil,
                    span: Self.span(in: transcript, needle: "OpenAI", fallback: "嘉宾提到 OpenAI 作为观察标的而非荐股"),
                    tStart: 60, tEnd: 78, confidence: 0.81, status: .candidate,
                    defaultAction: "monitor_candidate", pointType: "org_watch"
                )
            ]
            packet.summaryLine = "抽出行业判断与观察标的"
        case .health:
            packet.methods = [
                ExtractMethod(
                    id: "m-dose-1", title: "肌酸补充记录",
                    steps: ["每日固定时间服用", "配足量水"],
                    params: ["剂量": "5g", "频率": "每日一次"],
                    cautions: ["非处方；个体差异大"],
                    span: Self.span(in: transcript, needle: "肌酸", fallback: "肌酸每日约5克，仅作记录非处方"),
                    tStart: 22, tEnd: 48, confidence: 0.88, status: .candidate,
                    defaultAction: "record_only", pointType: "method_dose",
                    uiPrimaryLabel: "仅记录",
                    disclaimer: "非诊断、非处方，可丢弃"
                )
            ]
            packet.claims = [
                ExtractClaim(
                    id: "c-cont-1",
                    text: "长期高剂量肌酸是否有额外收益仍有争议。",
                    stance: "争议",
                    span: Self.span(in: transcript, needle: "争议", fallback: "长期高剂量肌酸是否有额外收益仍有争议"),
                    tStart: 50, tEnd: 70, confidence: 0.76, status: .candidate,
                    defaultAction: "debate", pointType: "claim_controversy"
                )
            ]
            packet.summaryLine = "抽出剂量方法（仅记录）与争议观点"
        case .edu:
            packet.claims = [
                ExtractClaim(
                    id: "c-edu-1",
                    text: "AI 时代应先教提问与校对，再教标准答案。",
                    span: Self.span(in: transcript, needle: "提问", fallback: "AI 时代应先教提问与校对再教标准答案"),
                    tStart: 15, tEnd: 40, confidence: 0.8, status: .candidate,
                    defaultAction: "debate", pointType: "claim_edu"
                )
            ]
            packet.methods = [
                ExtractMethod(
                    id: "m-ped-1", title: "双栏笔记教法",
                    steps: ["左栏摘录", "右栏用自己的话重述", "课后互评"],
                    params: nil, cautions: nil,
                    span: Self.span(in: transcript, needle: "双栏", fallback: "双栏笔记左摘录右重述课后互评"),
                    tStart: 45, tEnd: 80, confidence: 0.79, status: .candidate,
                    defaultAction: "try_later", pointType: "method_pedagogy"
                )
            ]
            packet.summaryLine = "抽出教育判断与教法"
        case .embodied:
            // 若未降级直接调用：练习协议，不当 health。
            packet.methods = [
                ExtractMethod(
                    id: "m-prac-1", title: "三分钟身体扫描",
                    steps: ["坐下", "从头到脚扫描感受", "不做评判"],
                    params: ["时长": "3分钟"], cautions: nil,
                    span: Self.span(in: transcript, needle: "扫描", fallback: "三分钟身体扫描从头到脚不做评判"),
                    tStart: 20, tEnd: 50, confidence: 0.83, status: .candidate,
                    defaultAction: "try_later", pointType: "method_practice"
                )
            ]
            packet.summaryLine = "抽出练习协议"
        case .general:
            packet.claims = [
                ExtractClaim(
                    id: "c-gen-1",
                    text: "收藏只是入口，抽成可行动卡片才算拥有。",
                    span: Self.span(in: transcript, needle: "收藏", fallback: "收藏只是入口抽成可行动卡片才算拥有"),
                    tStart: 10, tEnd: 30, confidence: 0.85, status: .candidate,
                    defaultAction: "debate", pointType: "claim_debate"
                )
            ]
            packet.entities = [
                ExtractEntity(
                    id: "e-person-1", type: "Person", nameSrc: "Guest", nameZh: "嘉宾",
                    span: Self.span(in: transcript, needle: "嘉宾", fallback: "本期嘉宾提到一个值得关注的人"),
                    tStart: 35, tEnd: 50, confidence: 0.7, status: .candidate,
                    defaultAction: "follow_candidate", pointType: "entity_roster"
                ),
                ExtractEntity(
                    id: "e-show-1", type: "Show", nameSrc: "Some Show", nameZh: nil,
                    span: Self.span(in: transcript, needle: "节目", fallback: "也提到另一个节目值得候选关注"),
                    tStart: 55, tEnd: 70, confidence: 0.7, status: .candidate,
                    defaultAction: "follow_candidate", pointType: "entity_roster"
                )
            ]
            packet.openQuestions = [
                ExtractOpenQuestion(
                    id: "q-1",
                    text: "这套方法换到中文语境是否仍成立？",
                    span: Self.span(in: transcript, needle: "成立", fallback: "这套方法换到中文语境是否仍成立"),
                    tStart: 75, tEnd: 90, confidence: 0.72, status: .candidate,
                    defaultAction: "debate", pointType: "open_question"
                )
            ]
            packet.summaryLine = "抽出主张、提及实体与待验证问题"
        }
        // 反万能摘要：深包必须有要点类型
        let typed = packet.claims.map(\.pointType) + packet.methods.map(\.pointType)
            + packet.entities.map(\.pointType) + packet.openQuestions.map(\.pointType)
        if typed.compactMap({ $0 }).isEmpty {
            packet.status = .failed
            packet.failureReason = "universal_summary"
            packet.summaryLine = "抽取像流水账，请改主题重抽"
        }
        packet.pendingConfirmCount = packet.computedPendingCount
        _ = durationSec
        return packet
    }

    static func lightSummary(workId: String, schema: ExtractSchemaID, title: String) -> ExtractionPacket {
        ExtractionPacket(
            packetId: "\(workId)-light",
            workId: workId,
            schemaId: schema,
            mode: .light,
            status: .ready,
            createdAt: ISO8601DateFormatter().string(from: Date()),
            summaryLine: "听完：\(title)",
            transcriptHash: nil,
            costUnits: 0,
            failureReason: nil,
            degradedFrom: nil,
            claims: [],
            methods: [],
            entities: [],
            quotes: [],
            openQuestions: [],
            roster: [],
            pendingConfirmCount: 0
        )
    }

    private static func hash(_ text: String) -> String {
        // 轻量稳定指纹，非加密用途。
        var hash: UInt64 = 5381
        for byte in text.utf8 {
            hash = ((hash << 5) &+ hash) &+ UInt64(byte)
        }
        return String(hash, radix: 16)
    }

    private static func span(in transcript: String, needle: String, fallback: String) -> String {
        if let range = transcript.range(of: needle) {
            let start = transcript.index(range.lowerBound, offsetBy: -min(8, transcript.distance(from: transcript.startIndex, to: range.lowerBound)), limitedBy: transcript.startIndex) ?? transcript.startIndex
            let endOffset = min(24, transcript.distance(from: range.upperBound, to: transcript.endIndex))
            let end = transcript.index(range.upperBound, offsetBy: endOffset, limitedBy: transcript.endIndex) ?? transcript.endIndex
            let chunk = String(transcript[start..<end]).trimmingCharacters(in: .whitespacesAndNewlines)
            if chunk.count >= 8 { return chunk }
        }
        if fallback.count >= 8 { return fallback }
        return fallback + "……补充对齐文本"
    }
}

/// 真调用占位：有 key 时再接线，禁止静默吞掉。
struct LiveExtractor: EpisodeExtracting {
    let apiKey: String

    func deepExtract(
        workId: String,
        schema: ExtractSchemaID,
        transcript: String,
        durationSec: Double
    ) async throws -> ExtractionPacket {
        throw ExtractRuntimeError.notWired
    }
}

enum ExtractRuntimeError: LocalizedError {
    case notWired
    case noTranscript
    case cancelled

    var errorDescription: String? {
        switch self {
        case .notWired: return "真模型抽取尚未接线"
        case .noTranscript: return "无译稿，无法深抽。请先完成听译。"
        case .cancelled: return "已取消星标，深抽任务已撤销"
        }
    }
}
