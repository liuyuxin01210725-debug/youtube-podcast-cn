import Foundation

struct DiscoverShow: Identifiable, Hashable, Codable {
    var show_id: Int
    var name: String
    var role: String?
    var people: [String]?

    var id: Int { show_id }
}

struct DiscoverLens: Identifiable, Hashable, Codable {
    var id: String
    var label_zh: String
    var order: Int
    var shows: [DiscoverShow]
    var observe: [String]?
    var placeholder: String?
}

struct DiscoverCatalogFile: Codable {
    var version: Int
    var ia: String?
    var lenses: [DiscoverLens]
}

enum DiscoverCatalog {
    static let lenses: [DiscoverLens] = load()

    private static func load() -> [DiscoverLens] {
        if let url = Bundle.main.url(forResource: "lenses", withExtension: "json"),
           let data = try? Data(contentsOf: url),
           let file = try? JSONDecoder().decode(DiscoverCatalogFile.self, from: data) {
            return file.lenses.sorted { $0.order < $1.order }
        }
        return Self.fallback
    }

    /// Quiet fallback if the bundled JSON is missing from the target.
    private static let fallback: [DiscoverLens] = [
        DiscoverLens(id: "frontier-tech", label_zh: "前沿科技", order: 1, shows: [
            DiscoverShow(show_id: 1, name: "Latent Space", role: "核心 AI 长谈", people: ["swyx"]),
            DiscoverShow(show_id: 2, name: "Dwarkesh Podcast", role: "超长深度", people: ["Dwarkesh Patel"]),
            DiscoverShow(show_id: 3, name: "How I AI", role: "产品×Agent 实操", people: []),
        ], observe: nil, placeholder: nil),
        DiscoverLens(id: "embodied-robotics", label_zh: "具身与机器人", order: 2, shows: [
            DiscoverShow(show_id: 7, name: "No Priors", role: "创业+机器人嘉宾", people: []),
            DiscoverShow(show_id: 8, name: "RoboPapers", role: "论文作者对谈", people: []),
            DiscoverShow(show_id: 9, name: "Researcher's Guide to Physical AI", role: "短周报雷达", people: []),
        ], observe: nil, placeholder: nil),
        DiscoverLens(id: "health-longevity", label_zh: "健康长寿", order: 3, shows: [
            DiscoverShow(show_id: 10, name: "FoundMyFitness", role: "营养/运动科学", people: ["Rhonda Patrick"]),
            DiscoverShow(show_id: 11, name: "The Drive / Peter Attia", role: "临床长寿深度", people: ["Peter Attia"]),
        ], observe: ["Huberman Lab"], placeholder: nil),
        DiscoverLens(id: "ferment-food", label_zh: "发芽发酵 / 食品", order: 4, shows: [
            DiscoverShow(show_id: 12, name: "Fermentation Cast", role: "发酵占位", people: []),
        ], observe: nil, placeholder: nil),
        DiscoverLens(id: "ai-education", label_zh: "AI 时代教育", order: 5, shows: [], observe: nil, placeholder: "文章链接与后续教育类信源"),
        DiscoverLens(id: "business-investing", label_zh: "商业与投资", order: 6, shows: [
            DiscoverShow(show_id: 4, name: "Lenny's Podcast", role: "产品 craft", people: ["Lenny Rachitsky"]),
            DiscoverShow(show_id: 5, name: "YC Lightcone", role: "技术创始人一线", people: []),
            DiscoverShow(show_id: 6, name: "MAD Podcast", role: "AI/数据创业", people: ["Matt Turck"]),
        ], observe: nil, placeholder: nil),
        DiscoverLens(id: "niche-life-brands", label_zh: "小众生活与品牌", order: 7, shows: [], observe: nil, placeholder: "小众发现官卡占位"),
    ]
}
