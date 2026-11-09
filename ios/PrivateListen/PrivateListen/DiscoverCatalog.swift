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

/// One tap target on Discover: always backed by ≥1 playable library episode.
struct DiscoverLibraryShow: Identifiable, Hashable {
    var id: String { displayName }
    var displayName: String
    var role: String?
    var people: [String]
    var episodes: [Episode]
}

struct DiscoverLibraryLens: Identifiable, Hashable {
    var id: String
    var label_zh: String
    var order: Int
    var shows: [DiscoverLibraryShow]
}

enum DiscoverFeature {
    /// Option A: Discover tab removed; lenses live as Library chips on owned content only.
    static let discoverEnabled = false
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
        ], observe: nil, placeholder: nil),
    ]

    /// Derive lens ids for one episode by catalog / heuristic match (owned-content only).
    static func derivedLensIDs(for episode: Episode) -> [String] {
        lenses.filter { episodeMatches(episode, lens: $0) }.map(\.id)
    }

    /// Lenses that currently have ≥1 owned episode. Zero-result lenses never appear.
    static func ownedLensChips(from episodes: [Episode]) -> [(id: String, label: String)] {
        lenses.compactMap { lens in
            let hit = episodes.contains { belongs($0, toLensID: lens.id) }
            return hit ? (lens.id, lens.label_zh) : nil
        }
    }

    static func belongs(_ episode: Episode, toLensID lensID: String) -> Bool {
        if !episode.lensIds.isEmpty {
            return episode.lensIds.contains(lensID)
        }
        guard let lens = lenses.first(where: { $0.id == lensID }) else { return false }
        return episodeMatches(episode, lens: lens)
    }

    static func episodeMatches(_ episode: Episode, lens: DiscoverLens) -> Bool {
        for show in lens.shows {
            if namesMatch(episode.showName, show.name) || namesMatch(episode.title, show.name) {
                return true
            }
            for person in show.people ?? [] {
                if namesMatch(episode.showName, person)
                    || namesMatch(episode.title, person)
                    || episode.guests.contains(where: { namesMatch($0, person) }) {
                    return true
                }
            }
            if episode.guests.contains(where: { namesMatch($0, show.name) }) {
                return true
            }
        }
        for name in lens.observe ?? [] {
            if namesMatch(episode.showName, name) || namesMatch(episode.title, name) {
                return true
            }
        }
        return heuristicMatch(episode, lensID: lens.id)
    }

    /// Episode-level heuristics from title/topic/guest — never show-name alone for multi-topic shows (DOAC).
    /// Used only when library.json has no lens_ids yet.
    private static func heuristicMatch(_ episode: Episode, lensID: String) -> Bool {
        let title = episode.title.lowercased()
        let show = episode.showName.lowercased()
        let guests = episode.guests.map { $0.lowercased() }.joined(separator: " ")
        let topic = "\(title) \(guests)"
        switch lensID {
        case "ferment-food":
            return topic.contains("发酵") || topic.contains("ferment")
                || topic.contains("微生物") || topic.contains("肠道")
                || topic.contains("gut") || topic.contains("microbiome")
                || topic.contains("spector") || show.contains("spector")
        case "health-longevity":
            return show.contains("huberman") || show.contains("attia") || show.contains("foundmyfitness")
                || topic.contains("huberman") || topic.contains("attia")
                || topic.contains("肌酸") || topic.contains("creatine")
                || topic.contains("补剂") || topic.contains("长寿")
                || topic.contains("减重") || topic.contains("肠道健康")
                || topic.contains("spector") || show.contains("spector")
        case "business-investing":
            // Never map whole DOAC show → business; require topic/guest or single-topic biz shows.
            return topic.contains("mark cuban")
                || topic.contains("投资") || topic.contains("崩盘")
                || topic.contains("融资") || topic.contains("估值") || topic.contains("股市")
                || show.contains("lenny") || show.contains("lightcone")
                || show.contains("mad podcast") || show.contains("a16z")
                || show.contains("my first million")
        default:
            return false
        }
    }

    /// Build Discover content from the current library only. Dead catalog rows never appear.
    static func libraryBackedLenses(from episodes: [Episode]) -> [DiscoverLibraryLens] {
        let playable = episodes.filter(\.isPlayableNow).sorted { $0.createdAt > $1.createdAt }
        guard !playable.isEmpty else { return [] }

        var out: [DiscoverLibraryLens] = []
        for lens in lenses {
            let matched = playable.filter { belongs($0, toLensID: lens.id) }
            guard !matched.isEmpty else { continue }
            var byShow: [String: [Episode]] = [:]
            for episode in matched {
                byShow[episode.showName, default: []].append(episode)
            }
            let shows = byShow.keys.sorted().map { name in
                DiscoverLibraryShow(
                    displayName: name,
                    role: "资料库已有",
                    people: [],
                    episodes: byShow[name]!.sorted { $0.createdAt > $1.createdAt }
                )
            }
            out.append(DiscoverLibraryLens(
                id: lens.id,
                label_zh: lens.label_zh,
                order: lens.order,
                shows: shows
            ))
        }
        return out.sorted { $0.order < $1.order }
    }

    static func hasLibraryBackedContent(from episodes: [Episode]) -> Bool {
        !libraryBackedLenses(from: episodes).isEmpty
    }

    static func namesMatch(_ a: String, _ b: String) -> Bool {
        let na = normalize(a)
        let nb = normalize(b)
        guard !na.isEmpty, !nb.isEmpty else { return false }
        if na == nb { return true }
        if na.contains(nb) || nb.contains(na) { return true }
        let tokensA = significantTokens(na)
        let tokensB = significantTokens(nb)
        return !tokensA.isDisjoint(with: tokensB)
    }

    private static func normalize(_ value: String) -> String {
        value
            .lowercased()
            .replacingOccurrences(of: "’", with: "'")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func significantTokens(_ value: String) -> Set<String> {
        let parts = value.split { ch in
            !(ch.isLetter || ch.isNumber)
        }.map(String.init)
        return Set(parts.filter { $0.count >= 4 })
    }
}
