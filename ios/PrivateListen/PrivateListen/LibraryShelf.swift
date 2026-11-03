import Foundation

/// What the 资料库 list shows.
///
/// Default「全部」is the shelf still worth opening: 未听 and 在听. Finished episodes
/// leave that surface and stay on disk. 「已听」is the same chip row's archive, not a
/// new tab and not a Discover mall (Option A).
///
/// Apple Podcasts keeps one「继续播放」and lets played episodes leave Up Next while
/// staying inside a show. Overcast and Pocket Casts put played items behind one
/// filter. This shelf does both with the chips that already exist:
/// - 「全部」hides finished until you search.
/// - 「已听」browses finished.
/// - 「★」still lists a finished episode that was starred.
/// - 「按节目」and a lens include that show or topic's finished episodes, the way a
///   show page still lists what you already played.
enum LibraryShelf {
    /// Chip the user taps to browse finished episodes.
    static let archiveLabel = "已听"
    /// Shown when「全部」is empty only because everything left is finished.
    static let archiveEmptyHint = "听完的节目在「已听」。"

    enum Chip: Hashable, Identifiable {
        case all
        case inProgress
        case unplayed
        case finished
        case starred
        case lens(id: String, label: String)
        case show(String)

        var id: String {
            switch self {
            case .all: return "all"
            case .inProgress: return "inProgress"
            case .unplayed: return "unplayed"
            case .finished: return "finished"
            case .starred: return "starred"
            case .lens(let id, _): return "lens:\(id)"
            case .show(let name): return "show:\(name)"
            }
        }

        var label: String {
            switch self {
            case .all: return "全部"
            case .inProgress: return "在听"
            case .unplayed: return "未听"
            case .finished: return LibraryShelf.archiveLabel
            case .starred: return "★"
            case .lens(_, let label): return label
            case .show(let name): return name
            }
        }
    }

    /// One library row, with listen state already decided by `LibraryStore`.
    struct Row: Equatable {
        var id: String
        var title: String
        var showName: String
        var guests: [String]
        var finished: Bool
        var inProgress: Bool
        var unplayed: Bool
        var starred: Bool
        var lensIDs: [String]
    }

    /// Progressive chips. 「已听」appears only once something is finished.
    static func chips(
        hasInProgress: Bool,
        hasUnplayed: Bool,
        hasFinished: Bool,
        hasStarred: Bool,
        lenses: [(id: String, label: String)]
    ) -> [Chip] {
        var chips: [Chip] = [.all]
        if hasInProgress { chips.append(.inProgress) }
        if hasUnplayed { chips.append(.unplayed) }
        if hasFinished { chips.append(.finished) }
        if hasStarred { chips.append(.starred) }
        for lens in lenses {
            chips.append(.lens(id: lens.id, label: lens.label))
        }
        return chips
    }

    /// Keeps library order. Does not drop or rewrite episodes.
    static func visible(_ rows: [Row], chip: Chip, query: String) -> [Row] {
        rows.filter { include($0, chip: chip, query: query) }
    }

    static func include(_ row: Row, chip: Chip, query: String) -> Bool {
        let q = trimmed(query)
        switch chip {
        case .all:
            // Resting「全部」hides finished. A typed search still finds them,
            // the way podcast search finds a played episode by name.
            if row.finished && q.isEmpty { return false }
        case .inProgress:
            if !row.inProgress { return false }
        case .unplayed:
            if !row.unplayed { return false }
        case .finished:
            if !row.finished { return false }
        case .starred:
            if !row.starred { return false }
        case .lens(let id, _):
            if !row.lensIDs.contains(id) { return false }
        case .show(let name):
            if row.showName != name { return false }
        }
        return matches(row, query: q)
    }

    /// The continue rail sits on the shelf you are still working through.
    static func showsContinue(chip: Chip, query: String) -> Bool {
        guard trimmed(query).isEmpty else { return false }
        switch chip {
        case .all, .inProgress:
            return true
        case .unplayed, .finished, .starred, .lens, .show:
            return false
        }
    }

    static func sectionTitle(chip: Chip, query: String, count: Int) -> String {
        if !trimmed(query).isEmpty {
            return "搜索结果 · \(count)"
        }
        switch chip {
        case .all: return "全部 · \(count)"
        case .inProgress: return "在听 · \(count)"
        case .unplayed: return "未听 · \(count)"
        case .finished: return "\(archiveLabel) · \(count)"
        case .starred: return "星标 · \(count)"
        case .lens(_, let label): return "\(label) · \(count)"
        case .show(let name): return "\(name) · \(count)"
        }
    }

    static func emptyMessage(chip: Chip, query: String, hasFinished: Bool) -> String {
        if !trimmed(query).isEmpty {
            return "没有匹配的节目。"
        }
        if chip == .all && hasFinished {
            return archiveEmptyHint
        }
        return "这个筛选下暂时没有节目。"
    }

    private static func matches(_ row: Row, query: String) -> Bool {
        if query.isEmpty { return true }
        if row.title.localizedCaseInsensitiveContains(query) { return true }
        if row.showName.localizedCaseInsensitiveContains(query) { return true }
        return row.guests.contains { $0.localizedCaseInsensitiveContains(query) }
    }

    private static func trimmed(_ query: String) -> String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
