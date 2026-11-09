import Foundation

/// 声间抽取对象模型（对齐设计 §2 / §13）。
enum ExtractMode: String, Codable, Hashable {
    case light, deep, manual
}

enum PacketStatus: String, Codable, Hashable {
    case queued, running, ready, failed, stale
}

enum ItemStatus: String, Codable, Hashable {
    case candidate, confirmed, discarded
}

enum ExtractSchemaID: String, Codable, CaseIterable, Hashable {
    case general
    case ferment
    case biz_invest
    case health
    case edu
    case embodied

    var labelZH: String {
        switch self {
        case .general: return "通用"
        case .ferment: return "发酵"
        case .biz_invest: return "商业投资"
        case .health: return "健康运动"
        case .edu: return "教育"
        case .embodied: return "具身"
        }
    }
}

struct ExtractionPacket: Codable, Hashable {
    var packetId: String
    var workId: String
    var schemaId: ExtractSchemaID
    var mode: ExtractMode
    var status: PacketStatus
    var createdAt: String
    var summaryLine: String?
    var transcriptHash: String?
    var costUnits: Int?
    var failureReason: String?
    var degradedFrom: String?
    var claims: [ExtractClaim]
    var methods: [ExtractMethod]
    var entities: [ExtractEntity]
    var quotes: [ExtractQuote]
    var openQuestions: [ExtractOpenQuestion]
    var roster: [ExtractRosterItem]?
    var actions: [ExtractAction]?
    var secondarySchemas: [String]?
    var pendingConfirmCount: Int?

    enum CodingKeys: String, CodingKey {
        case packetId = "packet_id"
        case workId = "work_id"
        case schemaId = "schema_id"
        case mode, status
        case createdAt = "created_at"
        case summaryLine = "summary_line"
        case transcriptHash = "transcript_hash"
        case costUnits = "cost_units"
        case failureReason = "failure_reason"
        case degradedFrom = "degraded_from"
        case claims, methods, entities, quotes
        case openQuestions = "open_questions"
        case roster, actions
        case secondarySchemas = "secondary_schemas"
        case pendingConfirmCount = "pending_confirm_count"
    }

    var computedPendingCount: Int {
        if mode == .light { return 0 }
        guard status == .ready else { return 0 }
        func countPending<T>(_ items: [T], status: (T) -> ItemStatus, low: (T) -> Bool?) -> Int {
            items.filter { status($0) == .candidate && !(low($0) ?? false) }.count
        }
        return countPending(claims, status: { $0.status }, low: { $0.lowConfidence })
            + countPending(methods, status: { $0.status }, low: { $0.lowConfidence })
            + countPending(entities, status: { $0.status }, low: { $0.lowConfidence })
            + countPending(quotes, status: { $0.status }, low: { $0.lowConfidence })
            + countPending(openQuestions, status: { $0.status }, low: { $0.lowConfidence })
    }

    /// UI 结果列表用：非丢弃要点总数（status 仍可保留 candidate）。
    var visiblePointCount: Int {
        func countVisible<T>(_ items: [T], status: (T) -> ItemStatus) -> Int {
            items.filter { status($0) != .discarded }.count
        }
        return countVisible(claims, status: { $0.status })
            + countVisible(methods, status: { $0.status })
            + countVisible(entities, status: { $0.status })
            + countVisible(quotes, status: { $0.status })
            + countVisible(openQuestions, status: { $0.status })
    }
}

protocol ExtractItem {
    var id: String { get }
    var status: ItemStatus { get }
    var span: String? { get }
    var tStart: Double? { get }
    var tEnd: Double? { get }
    var confidence: Double? { get }
    var lowConfidence: Bool? { get }
    var pointType: String? { get }
}

struct ExtractClaim: Codable, Hashable, ExtractItem {
    var id: String
    var text: String
    var stance: String?
    var span: String?
    var tStart: Double?
    var tEnd: Double?
    var confidence: Double?
    var status: ItemStatus
    var defaultAction: String?
    var pointType: String?
    var lowConfidence: Bool?
    var attrs: [String: String]?

    enum CodingKeys: String, CodingKey {
        case id, text, stance, span, confidence, status, attrs
        case tStart = "t_start"
        case tEnd = "t_end"
        case defaultAction = "default_action"
        case pointType = "point_type"
        case lowConfidence = "low_confidence"
    }
}

struct ExtractMethod: Codable, Hashable, ExtractItem {
    var id: String
    var title: String
    var steps: [String]?
    var params: [String: String]?
    var cautions: [String]?
    var span: String?
    var tStart: Double?
    var tEnd: Double?
    var confidence: Double?
    var status: ItemStatus
    var defaultAction: String?
    var pointType: String?
    var lowConfidence: Bool?
    var uiPrimaryLabel: String?
    var disclaimer: String?

    enum CodingKeys: String, CodingKey {
        case id, title, steps, params, cautions, span, confidence, status
        case tStart = "t_start"
        case tEnd = "t_end"
        case defaultAction = "default_action"
        case pointType = "point_type"
        case lowConfidence = "low_confidence"
        case uiPrimaryLabel = "ui_primary_label"
        case disclaimer
    }
}

struct ExtractEntity: Codable, Hashable, ExtractItem {
    var id: String
    var type: String
    var nameSrc: String
    var nameZh: String?
    var span: String?
    var tStart: Double?
    var tEnd: Double?
    var confidence: Double?
    var status: ItemStatus
    var defaultAction: String?
    var pointType: String?
    var lowConfidence: Bool?

    enum CodingKeys: String, CodingKey {
        case id, type, span, confidence, status
        case nameSrc = "name_src"
        case nameZh = "name_zh"
        case tStart = "t_start"
        case tEnd = "t_end"
        case defaultAction = "default_action"
        case pointType = "point_type"
        case lowConfidence = "low_confidence"
    }
}

struct ExtractQuote: Codable, Hashable, ExtractItem {
    var id: String
    var text: String
    var span: String?
    var tStart: Double?
    var tEnd: Double?
    var confidence: Double?
    var status: ItemStatus
    var defaultAction: String?
    var pointType: String?
    var lowConfidence: Bool?

    enum CodingKeys: String, CodingKey {
        case id, text, span, confidence, status
        case tStart = "t_start"
        case tEnd = "t_end"
        case defaultAction = "default_action"
        case pointType = "point_type"
        case lowConfidence = "low_confidence"
    }
}

struct ExtractOpenQuestion: Codable, Hashable, ExtractItem {
    var id: String
    var text: String
    var span: String?
    var tStart: Double?
    var tEnd: Double?
    var confidence: Double?
    var status: ItemStatus
    var defaultAction: String?
    var pointType: String?
    var lowConfidence: Bool?

    enum CodingKeys: String, CodingKey {
        case id, text, span, confidence, status
        case tStart = "t_start"
        case tEnd = "t_end"
        case defaultAction = "default_action"
        case pointType = "point_type"
        case lowConfidence = "low_confidence"
    }
}

struct ExtractRosterItem: Codable, Hashable {
    var name: String
    var type: String?
}

struct ExtractAction: Codable, Hashable {
    var id: String
    var kind: String
    var itemId: String?

    enum CodingKeys: String, CodingKey {
        case id, kind
        case itemId = "item_id"
    }
}
