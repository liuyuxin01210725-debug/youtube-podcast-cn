import CoreFoundation
import Foundation

/// 播放页「提炼」该显示哪一包。
///
/// 收藏星在 UserDefaults（`PrivateListen.starred`），要点在 `extract.json`。
/// `reloadAll` 以前按「这一次库列表」整表替换内存：iCloud 清单短暂缺集、
/// `extract.json` 还是占位/排队壳、或解码失败时，已就绪的要点会被丢掉，
/// 详情只剩「点星开始提炼」，但右上角星仍是实心的。
/// 这里保证：已可展示的 deep / 上次成功包（LKG）不会输给缺读、失败壳或轻摘。
enum ExtractDisplay {
    /// 详情能列出要点，或至少是一条听完轻摘。排队 / 失败 / 空 ready 不算。
    static func isShowable(_ packet: ExtractionPacket) -> Bool {
        switch packet.status {
        case .ready, .stale:
            if packet.mode == .light { return true }
            return packet.visiblePointCount > 0
        case .queued, .running, .failed:
            return false
        }
    }

    /// 磁盘、内存、上次成功包。有要点的 deep 优先于轻摘和排队/失败壳。
    /// 磁盘上新的可展示 deep 优先于更旧的内存包。
    static func merge(
        disk: ExtractionPacket?,
        memory: ExtractionPacket?,
        lkg: ExtractionPacket?
    ) -> ExtractionPacket? {
        let ordered = [disk, memory, lkg].compactMap { $0 }
        if let deep = ordered.first(where: { $0.mode != .light && isShowable($0) }) {
            return deep
        }
        if let light = ordered.first(where: { isShowable($0) }) {
            return light
        }
        return disk ?? memory
    }

    /// 一次库刷新读到的 `extract.json`。`packet == nil` 表示没读到或解不出来，不是「用户删了」。
    struct DiskRead {
        var id: String
        var packet: ExtractionPacket?
    }

    /// 清单里没出现的 id 保留内存包。出现了但读失败时，用内存或 LKG，不用空状态替换就绪包。
    static func reload(
        previous: [String: ExtractionPacket],
        lkg: [String: ExtractionPacket],
        reads: [DiskRead]
    ) -> [String: ExtractionPacket] {
        var next = previous
        for read in reads {
            let merged = merge(disk: read.packet, memory: previous[read.id], lkg: lkg[read.id])
            if let merged {
                next[read.id] = merged
            }
        }
        return next
    }

    /// 非可展示包（排队/失败/轻摘）不得覆盖磁盘上已有的 deep 要点。
    static func shouldReplaceFile(existing: ExtractionPacket?, incoming: ExtractionPacket) -> Bool {
        if let existing, existing.mode != .light, isShowable(existing) {
            return incoming.mode != .light && isShowable(incoming)
        }
        return true
    }

    enum EmptyCaption: Equatable {
        /// 未收藏、也没有任何包：点星才深抽。
        case promptToStar
        /// 已收藏但这一帧还没有可显示的包。不能画成「点星开始提炼」。
        case syncingStarred
    }

    static func emptyCaption(isStarred: Bool) -> EmptyCaption {
        isStarred ? .syncingStarred : .promptToStar
    }

    /// 已收藏且已有就绪/LKG 时，空 CTA 不得出现。未收藏且没有包时仍是点星。
    static func showsPromptToStar(isStarred: Bool, display: ExtractionPacket?) -> Bool {
        display == nil && emptyCaption(isStarred: isStarred) == .promptToStar
    }
}

/// `extract.json` 严格 Codable 失败时的兜底（透镜 id 写进 schema、缺数组、params 不是字符串）。
enum ExtractPacketDecoding {
    static func decode(_ data: Data) -> ExtractionPacket? {
        if let packet = try? JSONDecoder().decode(ExtractionPacket.self, from: data) {
            return packet
        }
        return decodeLoose(data)
    }

    private static func decodeLoose(_ data: Data) -> ExtractionPacket? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let claims = dicts(obj["claims"]).compactMap(claim)
        let methods = dicts(obj["methods"]).compactMap(method)
        let entities = dicts(obj["entities"]).compactMap(entity)
        let quotes = dicts(obj["quotes"]).compactMap(quote)
        let questions = dicts(obj["open_questions"]).compactMap(question)
        let pointCount = claims.count + methods.count + entities.count + quotes.count + questions.count
        guard let status = packetStatus(obj["status"], pointCount: pointCount) else { return nil }
        let workId = string(obj["work_id"]) ?? string(obj["packet_id"])
        guard let workId else { return nil }
        let packetId = string(obj["packet_id"]) ?? workId
        let mode = packetMode(obj["mode"], pointCount: pointCount)
        return ExtractionPacket(
            packetId: packetId,
            workId: workId,
            schemaId: schema(obj["schema_id"]),
            mode: mode,
            status: status,
            createdAt: string(obj["created_at"]) ?? "",
            summaryLine: string(obj["summary_line"]),
            transcriptHash: string(obj["transcript_hash"]),
            costUnits: int(obj["cost_units"]),
            failureReason: string(obj["failure_reason"]),
            degradedFrom: string(obj["degraded_from"]),
            claims: claims,
            methods: methods,
            entities: entities,
            quotes: quotes,
            openQuestions: questions,
            roster: nil,
            actions: nil,
            secondarySchemas: strings(obj["secondary_schemas"]),
            pendingConfirmCount: int(obj["pending_confirm_count"])
        )
    }

    private static func schema(_ value: Any?) -> ExtractSchemaID {
        let text = string(value) ?? ""
        if let known = ExtractSchemaID(rawValue: text) { return known }
        if let mapped = ExtractSchemaMap.lensIdToSchema[text] { return mapped }
        if let mapped = ExtractSchemaMap.labelToSchema[text] { return mapped }
        return .general
    }

    private static func packetMode(_ value: Any?, pointCount: Int) -> ExtractMode {
        if let text = string(value), let known = ExtractMode(rawValue: text) { return known }
        return pointCount > 0 ? .deep : .light
    }

    private static func packetStatus(_ value: Any?, pointCount: Int) -> PacketStatus? {
        if let text = string(value), let known = PacketStatus(rawValue: text) { return known }
        if pointCount > 0 { return .ready }
        return nil
    }

    private static func claim(_ raw: [String: Any]) -> ExtractClaim? {
        guard let id = string(raw["id"]), let text = string(raw["text"]) else { return nil }
        return ExtractClaim(
            id: id,
            text: text,
            stance: string(raw["stance"]),
            span: string(raw["span"]),
            tStart: double(raw["t_start"]),
            tEnd: double(raw["t_end"]),
            confidence: double(raw["confidence"]),
            status: itemStatus(raw["status"]),
            defaultAction: string(raw["default_action"]),
            pointType: string(raw["point_type"]),
            lowConfidence: bool(raw["low_confidence"]),
            attrs: stringMap(raw["attrs"])
        )
    }

    private static func method(_ raw: [String: Any]) -> ExtractMethod? {
        guard let id = string(raw["id"]), let title = string(raw["title"]) else { return nil }
        return ExtractMethod(
            id: id,
            title: title,
            steps: strings(raw["steps"]),
            params: stringMap(raw["params"]),
            cautions: strings(raw["cautions"]),
            span: string(raw["span"]),
            tStart: double(raw["t_start"]),
            tEnd: double(raw["t_end"]),
            confidence: double(raw["confidence"]),
            status: itemStatus(raw["status"]),
            defaultAction: string(raw["default_action"]),
            pointType: string(raw["point_type"]),
            lowConfidence: bool(raw["low_confidence"]),
            uiPrimaryLabel: string(raw["ui_primary_label"]),
            disclaimer: string(raw["disclaimer"])
        )
    }

    private static func entity(_ raw: [String: Any]) -> ExtractEntity? {
        guard let id = string(raw["id"]),
              let type = string(raw["type"]),
              let nameSrc = string(raw["name_src"]) else { return nil }
        return ExtractEntity(
            id: id,
            type: type,
            nameSrc: nameSrc,
            nameZh: string(raw["name_zh"]),
            span: string(raw["span"]),
            tStart: double(raw["t_start"]),
            tEnd: double(raw["t_end"]),
            confidence: double(raw["confidence"]),
            status: itemStatus(raw["status"]),
            defaultAction: string(raw["default_action"]),
            pointType: string(raw["point_type"]),
            lowConfidence: bool(raw["low_confidence"])
        )
    }

    private static func quote(_ raw: [String: Any]) -> ExtractQuote? {
        guard let id = string(raw["id"]), let text = string(raw["text"]) else { return nil }
        return ExtractQuote(
            id: id,
            text: text,
            span: string(raw["span"]),
            tStart: double(raw["t_start"]),
            tEnd: double(raw["t_end"]),
            confidence: double(raw["confidence"]),
            status: itemStatus(raw["status"]),
            defaultAction: string(raw["default_action"]),
            pointType: string(raw["point_type"]),
            lowConfidence: bool(raw["low_confidence"])
        )
    }

    private static func question(_ raw: [String: Any]) -> ExtractOpenQuestion? {
        guard let id = string(raw["id"]), let text = string(raw["text"]) else { return nil }
        return ExtractOpenQuestion(
            id: id,
            text: text,
            span: string(raw["span"]),
            tStart: double(raw["t_start"]),
            tEnd: double(raw["t_end"]),
            confidence: double(raw["confidence"]),
            status: itemStatus(raw["status"]),
            defaultAction: string(raw["default_action"]),
            pointType: string(raw["point_type"]),
            lowConfidence: bool(raw["low_confidence"])
        )
    }

    private static func itemStatus(_ value: Any?) -> ItemStatus {
        if let text = string(value), let known = ItemStatus(rawValue: text) { return known }
        return .candidate
    }

    private static func dicts(_ value: Any?) -> [[String: Any]] {
        value as? [[String: Any]] ?? []
    }

    private static func strings(_ value: Any?) -> [String]? {
        guard let array = value as? [Any] else { return nil }
        let items = array.compactMap { string($0) }
        return items
    }

    private static func stringMap(_ value: Any?) -> [String: String]? {
        guard let raw = value as? [String: Any] else { return nil }
        var mapped: [String: String] = [:]
        for (key, item) in raw {
            if let text = string(item) {
                mapped[key] = text
            }
        }
        return mapped
    }

    private static func string(_ value: Any?) -> String? {
        switch value {
        case let text as String:
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        case let number as NSNumber:
            if CFGetTypeID(number) == CFBooleanGetTypeID() {
                return number.boolValue ? "true" : "false"
            }
            return number.stringValue
        default:
            return nil
        }
    }

    private static func double(_ value: Any?) -> Double? {
        switch value {
        case let number as NSNumber:
            return number.doubleValue
        case let text as String:
            return Double(text)
        default:
            return nil
        }
    }

    private static func int(_ value: Any?) -> Int? {
        switch value {
        case let number as NSNumber:
            return number.intValue
        case let text as String:
            return Int(text)
        default:
            return nil
        }
    }

    private static func bool(_ value: Any?) -> Bool? {
        switch value {
        case let number as NSNumber:
            return number.boolValue
        case let text as String:
            return text == "true" || text == "1"
        default:
            return nil
        }
    }
}
