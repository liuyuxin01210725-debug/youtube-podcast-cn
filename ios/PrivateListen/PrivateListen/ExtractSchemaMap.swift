import Foundation

/// §13.8.2 透镜 → schema；无透镜默认 general；embodied MVP 降级。
enum ExtractSchemaMap {
    static let lensIdToSchema: [String: ExtractSchemaID] = [
        "frontier-tech": .general,
        "embodied-robotics": .embodied,
        "health-longevity": .health,
        "ferment-food": .ferment,
        "ai-education": .edu,
        "business-investing": .biz_invest,
        "niche-life-brands": .general,
    ]

    static let labelToSchema: [String: ExtractSchemaID] = [
        "通用": .general,
        "未分类": .general,
        "发酵": .ferment,
        "发芽": .ferment,
        "发芽发酵 / 食品": .ferment,
        "商业": .biz_invest,
        "投资": .biz_invest,
        "商业与投资": .biz_invest,
        "健康": .health,
        "运动": .health,
        "健康长寿": .health,
        "教育": .edu,
        "AI 时代教育": .edu,
        "具身": .embodied,
        "具身与机器人": .embodied,
        "前沿科技": .general,
        "小众生活与品牌": .general,
    ]

    struct Resolution: Hashable {
        var schemaId: ExtractSchemaID
        var secondary: [ExtractSchemaID]
        var degradedFrom: ExtractSchemaID?
        /// 禁止深抽前另跑轻分类。
        var forbidPreclassify: Bool { true }
    }

    /// - Parameter allowEmbodied: MVP 默认 false → 降级 general + Method，禁止静默当 health。
    static func resolve(
        lensIds: [String],
        primaryLens: String? = nil,
        explicit: ExtractSchemaID? = nil,
        allowEmbodied: Bool = false
    ) -> Resolution {
        if let explicit {
            return Resolution(schemaId: degradeIfNeeded(explicit, allowEmbodied: allowEmbodied).schema,
                              secondary: [],
                              degradedFrom: degradeIfNeeded(explicit, allowEmbodied: allowEmbodied).from)
        }
        var ordered = lensIds
        if let primaryLens, let idx = ordered.firstIndex(of: primaryLens) {
            ordered.remove(at: idx)
            ordered.insert(primaryLens, at: 0)
        }
        var mapped: [ExtractSchemaID] = []
        for id in ordered {
            let schema = lensIdToSchema[id] ?? .general
            if !mapped.contains(schema) { mapped.append(schema) }
        }
        guard let first = mapped.first else {
            return Resolution(schemaId: .general, secondary: [], degradedFrom: nil)
        }
        let degraded = degradeIfNeeded(first, allowEmbodied: allowEmbodied)
        let secondary = Array(mapped.dropFirst())
        return Resolution(schemaId: degraded.schema, secondary: secondary, degradedFrom: degraded.from)
    }

    private static func degradeIfNeeded(_ schema: ExtractSchemaID, allowEmbodied: Bool) -> (schema: ExtractSchemaID, from: ExtractSchemaID?) {
        if schema == .embodied && !allowEmbodied {
            return (.general, .embodied)
        }
        return (schema, nil)
    }
}
