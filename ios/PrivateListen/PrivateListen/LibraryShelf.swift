import Foundation

/// What the 资料库 list shows.
///
/// Default「未听完」is the shelf still worth opening: 未听 and 在听 (`D = U ∪ I`).
/// Finished episodes leave that surface and stay on disk. 「已听」is a resident chip
/// on the same bar, not a new tab and not a Discover mall (Option A).
///
/// The bar is at most four controls: 未听完｜星｜已听｜更多. 在听／未听／主题／按节目
/// live in the existing 更多 menu. That menu is a filter popover, not a new surface.
///
/// Apple Podcasts keeps one「继续播放」and lets played episodes leave Up Next while
/// staying inside a show. Overcast and Pocket Casts put played items behind one
/// filter. This shelf does both with the chips that already exist:
/// - 「未听完」hides finished until you search. The set is unchanged from「全部」.
/// - 「已听」browses finished, one tap from the bar.
/// - 「星」still lists a finished episode that was starred.
/// - A show or a lens, reached from「更多」, includes that show or topic's finished episodes.
enum LibraryShelf {
    /// Cold start. Same set as the old「全部」chip: unfinished ∪ in progress.
    static let defaultChip = Chip.all
    static let unfinishedLabel = "未听完"
    static let starLabel = "星"
    static let archiveLabel = "已听"
    static let moreLabel = "更多"
    /// Default-row title wraps, but never collapses to a forced single line.
    static let titleLineLimit = 2
    /// Status chips on a row are a cap, not a quota. Empty means no chips.
    static let rowStatusChipCap = 3

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
            case .all: return LibraryShelf.unfinishedLabel
            case .inProgress: return "在听"
            case .unplayed: return "未听"
            case .finished: return LibraryShelf.archiveLabel
            case .starred: return LibraryShelf.starLabel
            case .lens(_, let label): return label
            case .show(let name): return name
            }
        }
    }

    /// Resident chips plus the filters that belong behind「更多」.
    struct ChipRail: Equatable {
        var resident: [Chip]
        var more: [Chip]
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

    enum RowMark: Equatable {
        case text(String, emphasized: Bool)
        case star
    }

    enum EmptyAction: Equatable {
        case showArchive
        case showUnfinished
    }

    struct EmptyState: Equatable {
        var message: String
        var action: EmptyAction?

        var actionLabel: String? {
            switch action {
            case .showArchive: return LibraryShelf.archiveLabel
            case .showUnfinished: return LibraryShelf.unfinishedLabel
            case nil: return nil
            }
        }
    }

    /// Bar is always 未听完｜星｜已听, plus a 更多 button in the view (≤4).
    /// 在听／未听 stay one menu tap away even when their sets are empty.
    /// Themes and shows are listed only when the library actually has them.
    /// `shows` is included when there are at least two distinct show names.
    static func rail(
        lenses: [(id: String, label: String)],
        shows: [String]
    ) -> ChipRail {
        var more: [Chip] = [.inProgress, .unplayed]
        for lens in lenses {
            more.append(.lens(id: lens.id, label: lens.label))
        }
        if shows.count >= 2 {
            for name in shows where !name.isEmpty {
                more.append(.show(name))
            }
        }
        return ChipRail(resident: [.all, .starred, .finished], more: more)
    }

    /// Keeps library order. Does not drop or rewrite episodes.
    /// Play and pause do not reorder; a finished episode leaves the default shelf.
    static func visible(_ rows: [Row], chip: Chip, query: String) -> [Row] {
        rows.filter { include($0, chip: chip, query: query) }
    }

    static func include(_ row: Row, chip: Chip, query: String) -> Bool {
        let q = trimmed(query)
        switch chip {
        case .all:
            // Resting「未听完」hides finished. A typed search still finds them.
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
    /// Which episode is on the card, and whether the mini player is that same
    /// episode, is decided by `ContinueListening`. This function does not know
    /// about the player.
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
        case .all: return "\(unfinishedLabel) · \(count)"
        case .inProgress: return "在听 · \(count)"
        case .unplayed: return "未听 · \(count)"
        case .finished: return "\(archiveLabel) · \(count)"
        case .starred: return "星标 · \(count)"
        case .lens(_, let label): return "\(label) · \(count)"
        case .show(let name): return "\(name) · \(count)"
        }
    }

    static func emptyState(chip: Chip, query: String, hasFinished: Bool) -> EmptyState {
        if !trimmed(query).isEmpty {
            return EmptyState(message: "没有匹配的节目。", action: nil)
        }
        switch chip {
        case .all where hasFinished:
            return EmptyState(message: "都听完了。", action: .showArchive)
        case .finished:
            return EmptyState(message: "还没有已听完的节目。", action: .showUnfinished)
        case .all:
            return EmptyState(message: "这个筛选下暂时没有节目。", action: nil)
        default:
            return EmptyState(message: "这个筛选下暂时没有节目。", action: .showUnfinished)
        }
    }

    /// One secondary line: 节目 · 时长, or 节目 · 剩余 when that label is real.
    /// 「上次听到」is a listen note. It stays off this line; the player still has the resume point.
    /// An unknown duration is omitted rather than replaced with a made-up percent.
    static func rowSecondary(
        showName: String,
        durationLabel: String,
        remainingLabel: String?,
        downloading: Bool
    ) -> String {
        var parts: [String] = []
        let show = showName.trimmingCharacters(in: .whitespacesAndNewlines)
        if !show.isEmpty && show != "未分类" {
            parts.append(show)
        }
        let remaining = remainingLabel?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if remaining.hasPrefix("剩余") {
            parts.append(remaining)
        } else if !durationLabel.isEmpty && durationLabel != "时长未知" {
            parts.append(durationLabel)
        }
        if downloading {
            parts.append("正在从 iCloud 下载")
        }
        return parts.joined(separator: " · ")
    }

    /// Listen chip for one row. The default shelf does not repeat「未听」on every row.
    /// A chip that only restates the current filter is omitted.
    static func rowListenChip(
        chip: Chip,
        finished: Bool,
        inProgress: Bool,
        unplayed: Bool
    ) -> String? {
        switch chip {
        case .all:
            if finished { return "已听完" }
            if inProgress { return "在听" }
            return nil
        case .inProgress, .unplayed, .finished:
            return nil
        default:
            if finished { return "已听完" }
            if inProgress { return "在听" }
            if unplayed { return "未听" }
            return nil
        }
    }

    /// At most `rowStatusChipCap` marks. Missing listen / star / extra are skipped, not filled in.
    static func rowMarks(listen: String?, starred: Bool, extra: String?) -> [RowMark] {
        var marks: [RowMark] = []
        if let listen {
            let trimmed = listen.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                marks.append(.text(trimmed, emphasized: trimmed == "未听"))
            }
        }
        if starred {
            marks.append(.star)
        }
        if let extra {
            let trimmed = extra.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                let emphasized = trimmed.contains("要点") || trimmed == "已提炼"
                marks.append(.text(trimmed, emphasized: emphasized))
            }
        }
        if marks.count > rowStatusChipCap {
            return Array(marks.prefix(rowStatusChipCap))
        }
        return marks
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
