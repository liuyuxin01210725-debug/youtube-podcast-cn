import SwiftUI
import UniformTypeIdentifiers

/// Option A: one chronological library list + progressive filter chips (owned only).
/// Keeps today's extract design entry, listen resume bar, ExtractStore badges/stars.
struct LibraryView: View {
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var player: PlayerModel
    @EnvironmentObject private var extracts: ExtractStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var picking = false
    @State private var showExtractDesign = false
    @State private var filter: LibraryChip = .all
    @State private var searchText = ""
    @State private var searchPresented = false

    private let searchAlwaysVisibleAt = 40
    private let showFilterAtDistinctShows = 2

    var body: some View {
        NavigationStack {
            Group {
                if !library.hasCloudFolder && library.episodes.isEmpty {
                    prompt(
                        title: "选择资料库",
                        systemImage: "folder",
                        detail: "在 iCloud 云盘里选中「中文播客私人库」。只需选一次。分享进来的链接和完整生成的节目都在这个文件夹里。"
                    )
                } else if library.episodes.isEmpty && library.inbox.isEmpty {
                    prompt(
                        title: "还没有节目",
                        systemImage: "music.note.list",
                        detail: "从 Safari、YouTube 或播客 App 分享链接到「声间」。Mac 转换完成后，下拉即可看到。试听不会出现在这里。"
                    )
                } else {
                    list
                }
            }
            .navigationTitle("资料库")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if library.episodes.count < searchAlwaysVisibleAt {
                        Button {
                            searchPresented = true
                        } label: {
                            Image(systemName: "magnifyingglass")
                        }
                        .accessibilityLabel("搜索")
                    }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Menu {
                        Button {
                            Task { @MainActor in
                                showExtractDesign = true
                            }
                        } label: {
                            Label("收藏≠拥有 · 设计", systemImage: "doc.plaintext")
                        }
                        Button(library.hasCloudFolder ? "重新选择资料库" : "选择资料库") {
                            picking = true
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                    .accessibilityLabel("更多")
                }
            }
            .searchable(text: $searchText, isPresented: $searchPresented, prompt: "标题、节目、嘉宾")
            .onAppear {
                player.onProgress = { id, seconds, duration in
                    Task { @MainActor in
                        library.remember(id: id, seconds: seconds, duration: duration)
                        if let episode = library.episodes.first(where: { $0.id == id }) {
                            extracts.maybeLightOnFinished(episode: episode, seconds: seconds, duration: duration)
                        }
                    }
                }
                library.reload()
                extracts.reloadAll(episodes: library.episodes)
                sanitizeFilter()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    library.reload()
                    extracts.reloadAll(episodes: library.episodes)
                    sanitizeFilter()
                } else {
                    player.checkpoint()
                }
            }
            .onChange(of: library.episodes.map(\.id)) { _, _ in
                sanitizeFilter()
            }
            .task(id: scenePhase) {
                guard scenePhase == .active else { return }
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(library.waitingOnCloud ? 8 : 60))
                    if Task.isCancelled { break }
                    library.reload()
                    extracts.reloadAll(episodes: library.episodes)
                }
            }
            .fileImporter(isPresented: $picking, allowedContentTypes: [.folder], allowsMultipleSelection: false) { result in
                switch result {
                case .success(let urls):
                    if let url = urls.first { library.adopt(url) }
                case .failure(let error):
                    if (error as? CocoaError)?.code == .userCancelled { return }
                    library.message = "没有选中资料库文件夹。"
                }
            }
            .navigationDestination(for: Episode.self) { episode in
                PlayerView(episode: episode)
            }
            .navigationDestination(isPresented: $showExtractDesign) {
                ExtractDesignView()
            }
        }
    }

    private var list: some View {
        List {
            if !availableChips.isEmpty {
                Section {
                    chipBar
                        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                        .listRowBackground(Color.clear)
                }
            }
            if library.readingLocalCopyOnly {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("现在只显示连 Mac 时拷进来的本机副本。Mac 发布到 iCloud 的新节目要选好资料库才会自动出现。")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                        Button("在 iCloud 云盘里选择「中文播客私人库」") { picking = true }
                            .font(.footnote)
                    }
                    .padding(.vertical, 2)
                }
            }
            if let message = library.message, !message.isEmpty {
                Section {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            if let banner = extracts.banner, !banner.isEmpty {
                Section {
                    Text(banner)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            if !library.inbox.isEmpty && filter == .all && searchText.isEmpty {
                Section("收件箱") {
                    ForEach(library.inbox) { item in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.label)
                                .font(.body)
                            Text(item.line)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 4)
                    }
                }
            }
            Section {
                if visibleEpisodes.isEmpty {
                    Text(emptyFilterMessage)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                } else {
                    episodeRows
                }
            } header: {
                Text(sectionHeader)
            }
        }
        .refreshable {
            library.reload()
            extracts.reloadAll(episodes: library.episodes)
            sanitizeFilter()
        }
    }

    private var chipBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(availableChips) { chip in
                    chipButton(chip)
                }
                if distinctShows.count >= showFilterAtDistinctShows {
                    Menu {
                        ForEach(distinctShows, id: \.self) { show in
                            Button(show) { filter = .show(show) }
                        }
                    } label: {
                        HStack(spacing: 4) {
                            Text(showChipLabel)
                                .font(.subheadline.weight(isShowFilter ? .semibold : .regular))
                            Image(systemName: "chevron.down")
                                .font(.caption2.weight(.semibold))
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .background(isShowFilter ? Color.primary.opacity(0.9) : Color.secondary.opacity(0.12), in: Capsule())
                        .foregroundStyle(isShowFilter ? Color(.systemBackground) : Color.primary)
                    }
                }
            }
            .padding(.vertical, 2)
        }
    }

    private func chipButton(_ chip: LibraryChip) -> some View {
        let on = filter == chip
        return Button {
            filter = chip
        } label: {
            Text(chip.label)
                .font(.subheadline.weight(on ? .semibold : .regular))
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(on ? Color.primary.opacity(0.9) : Color.secondary.opacity(0.12), in: Capsule())
                .foregroundStyle(on ? Color(.systemBackground) : Color.primary)
        }
        .buttonStyle(.plain)
    }

    private var episodeRows: some View {
        var density = 0
        return ForEach(visibleEpisodes) { episode in
            let badge = extracts.badge(for: episode.id, densityUsed: &density)
            NavigationLink(value: episode) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(episode.title)
                            .font(.body)
                            .foregroundStyle(.primary)
                        if library.isUnplayed(episode.id) {
                            listenBadge("未听", emphasized: true)
                        } else if library.isInProgress(episode.id) {
                            listenBadge("在听", emphasized: false)
                        } else if library.isFinished(episode.id) {
                            listenBadge("已听完", emphasized: false)
                        }
                        if extracts.isStarred(episode.id) {
                            Image(systemName: "star.fill")
                                .font(.caption2)
                                .foregroundStyle(.yellow)
                        }
                        if let badge {
                            listenBadge(badge, emphasized: badge.contains("待确认"))
                        }
                    }
                    Text(subtitle(episode))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    if let fraction = library.playedFraction(for: episode) {
                        listenBar(fraction)
                    }
                }
                .padding(.vertical, 4)
            }
            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                Button {
                    extracts.toggleStar(episode: episode, lensIds: episode.effectiveLensIds)
                } label: {
                    Label(
                        extracts.isStarred(episode.id) ? "取消星标" : "星标",
                        systemImage: extracts.isStarred(episode.id) ? "star.slash" : "star.fill"
                    )
                }
                .tint(.orange)
            }
        }
    }

    private var availableChips: [LibraryChip] {
        var chips: [LibraryChip] = [.all]
        let eps = library.episodes
        if eps.contains(where: { library.isInProgress($0.id) }) {
            chips.append(.inProgress)
        }
        if eps.contains(where: { library.isUnplayed($0.id) }) {
            chips.append(.unplayed)
        }
        if eps.contains(where: { extracts.isStarred($0.id) }) {
            chips.append(.starred)
        }
        for lens in DiscoverCatalog.ownedLensChips(from: eps) {
            chips.append(.lens(id: lens.id, label: lens.label))
        }
        return chips
    }

    private var distinctShows: [String] {
        let names = Set(library.episodes.map(\.showName).filter { $0 != "未分类" })
        return names.sorted()
    }

    private var isShowFilter: Bool {
        if case .show = filter { return true }
        return false
    }

    private var showChipLabel: String {
        if case .show(let name) = filter { return name }
        return "按节目"
    }

    private var visibleEpisodes: [Episode] {
        var items = library.episodes
        switch filter {
        case .all:
            break
        case .inProgress:
            items = items.filter { library.isInProgress($0.id) }
        case .unplayed:
            items = items.filter { library.isUnplayed($0.id) }
        case .starred:
            items = items.filter { extracts.isStarred($0.id) }
        case .lens(let id, _):
            items = items.filter { DiscoverCatalog.belongs($0, toLensID: id) }
        case .show(let name):
            items = items.filter { $0.showName == name }
        }
        let q = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !q.isEmpty {
            items = items.filter { episode in
                episode.title.localizedCaseInsensitiveContains(q)
                    || episode.showName.localizedCaseInsensitiveContains(q)
                    || episode.guests.contains { $0.localizedCaseInsensitiveContains(q) }
            }
        }
        return items
    }

    private var sectionHeader: String {
        let count = visibleEpisodes.count
        if !searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "搜索结果 · \(count)"
        }
        switch filter {
        case .all: return "全部 · \(count)"
        case .inProgress: return "在听 · \(count)"
        case .unplayed: return "未听 · \(count)"
        case .starred: return "星标 · \(count)"
        case .lens(_, let label): return "\(label) · \(count)"
        case .show(let name): return "\(name) · \(count)"
        }
    }

    private var emptyFilterMessage: String {
        if !searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "没有匹配的节目。"
        }
        return "这个筛选下暂时没有节目。"
    }

    private func sanitizeFilter() {
        let chips = availableChips
        switch filter {
        case .all:
            return
        case .inProgress, .unplayed, .starred, .lens:
            if !chips.contains(filter) {
                filter = .all
            }
        case .show(let name):
            if !distinctShows.contains(name) {
                filter = .all
            }
        }
    }

    private func subtitle(_ episode: Episode) -> String {
        var parts: [String] = []
        if episode.showName != "未分类" {
            parts.append(episode.showName)
        }
        parts.append(episode.durationLabel)
        parts.append(episode.dateLabel)
        if let heard = library.continuePointLabel(for: episode.id) {
            parts.append(heard)
        }
        if episode.downloading { parts.append("正在从 iCloud 下载") }
        return parts.joined(separator: " · ")
    }

    private func listenBadge(_ title: String, emphasized: Bool) -> some View {
        Text(title)
            .font(.caption2)
            .foregroundStyle(emphasized ? Color.accentColor : Color.secondary)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(
                (emphasized ? Color.accentColor : Color.primary).opacity(emphasized ? 0.15 : 0.08),
                in: Capsule()
            )
    }

    private func listenBar(_ fraction: Double) -> some View {
        ProgressView(value: min(1, max(0, fraction)))
            .progressViewStyle(.linear)
            .tint(Color.primary.opacity(0.55))
            .accessibilityLabel("上次听到")
            .accessibilityValue("\(Int((fraction * 100).rounded()))%")
    }

    private func prompt(title: String, systemImage: String, detail: String) -> some View {
        ScrollView {
            VStack(spacing: 16) {
                Image(systemName: systemImage)
                    .font(.system(size: 40))
                    .foregroundStyle(.secondary)
                Text(title)
                    .font(.title2)
                Text(detail)
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                if let message = library.message {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                Button("选择资料库") { picking = true }
                    .buttonStyle(.borderedProminent)
            }
            .padding(24)
            .frame(maxWidth: .infinity, minHeight: 360)
        }
        .refreshable {
            library.reload()
            extracts.reloadAll(episodes: library.episodes)
        }
    }
}

private enum LibraryChip: Identifiable, Hashable {
    case all
    case inProgress
    case unplayed
    case starred
    case lens(id: String, label: String)
    case show(String)

    var id: String {
        switch self {
        case .all: return "all"
        case .inProgress: return "inProgress"
        case .unplayed: return "unplayed"
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
        case .starred: return "★"
        case .lens(_, let label): return label
        case .show(let name): return name
        }
    }
}
