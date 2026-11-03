import SwiftUI
import UniformTypeIdentifiers

/// Option A: one chronological library list + progressive filter chips (owned only).
/// 「继续播放」is at most the latest unfinished episode. 「全部」hides finished;
/// 「已听」on the same chip row is the archive. Discover mall stays out.
/// Extract badges, stars, and the extract design entry stay as they are.
struct LibraryView: View {
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var player: PlayerModel
    @EnvironmentObject private var extracts: ExtractStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var path = NavigationPath()
    @State private var picking = false
    @State private var showExtractDesign = false
    @State private var filter: LibraryShelf.Chip = .all
    @State private var searchText = ""
    @State private var searchPresented = false

    private let searchAlwaysVisibleAt = 40
    private let showFilterAtDistinctShows = 2

    var body: some View {
        NavigationStack(path: $path) {
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
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if let episode = miniEpisode, !player.fullPlayerOpen {
                    MiniPlayerBar(
                        episode: episode,
                        isPlaying: player.isPlaying,
                        fraction: miniFraction,
                        status: player.errorText,
                        onToggle: { toggleMini(episode) }
                    )
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
            if showsContinueSection && !continueSectionRows.isEmpty {
                ContinueListeningSection(
                    rows: continueSectionRows,
                    onPlay: playFromLibrary,
                    onDismiss: { library.dismissContinue($0.id) }
                )
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

    private func chipButton(_ chip: LibraryShelf.Chip) -> some View {
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
                            listenBadge(badge, emphasized: badge.contains("要点") || badge == "已提炼")
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

    private var availableChips: [LibraryShelf.Chip] {
        let eps = library.episodes
        return LibraryShelf.chips(
            hasInProgress: eps.contains { library.isInProgress($0.id) },
            hasUnplayed: eps.contains { library.isUnplayed($0.id) },
            hasFinished: eps.contains { library.isFinished($0.id) },
            hasStarred: eps.contains { extracts.isStarred($0.id) },
            lenses: DiscoverCatalog.ownedLensChips(from: eps)
        )
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

    private var shelfRows: [LibraryShelf.Row] {
        library.episodes.map { episode in
            LibraryShelf.Row(
                id: episode.id,
                title: episode.title,
                showName: episode.showName,
                guests: episode.guests,
                finished: library.isFinished(episode.id),
                inProgress: library.isInProgress(episode.id),
                unplayed: library.isUnplayed(episode.id),
                starred: extracts.isStarred(episode.id),
                lensIDs: episode.effectiveLensIds
            )
        }
    }

    private var visibleEpisodes: [Episode] {
        let shown = Set(LibraryShelf.visible(shelfRows, chip: filter, query: searchText).map(\.id))
        return library.episodes.filter { shown.contains($0.id) }
    }

    private var sectionHeader: String {
        LibraryShelf.sectionTitle(chip: filter, query: searchText, count: visibleEpisodes.count)
    }

    private var emptyFilterMessage: String {
        LibraryShelf.emptyMessage(
            chip: filter,
            query: searchText,
            hasFinished: library.episodes.contains { library.isFinished($0.id) }
        )
    }

    private func sanitizeFilter() {
        let chips = availableChips
        switch filter {
        case .all:
            return
        case .inProgress, .unplayed, .finished, .starred, .lens:
            if !chips.contains(filter) {
                filter = .all
            }
        case .show(let name):
            if !distinctShows.contains(name) {
                filter = .all
            }
        }
    }

    private var showsContinueSection: Bool {
        LibraryShelf.showsContinue(chip: filter, query: searchText)
    }

    private var continueSectionRows: [ContinueListeningSection.Row] {
        let entries = library.continueEntries(in: library.episodes)
        let byID = Dictionary(uniqueKeysWithValues: library.episodes.map { ($0.id, $0) })
        return entries.compactMap { item in
            guard let episode = byID[item.id] else { return nil }
            let nearResume = !ContinueListening.shouldReopenAtListenMark(
                currentID: player.currentID,
                episodeID: episode.id,
                playhead: player.current,
                resumeAt: item.seconds,
                clipInProgress: player.isClipPlayback
            )
            let playing = nearResume && player.currentID == episode.id && player.isPlaying
            return ContinueListeningSection.Row(episode: episode, item: item, playing: playing)
        }
    }

    private var miniEpisode: Episode? {
        guard let id = player.currentID else { return nil }
        return library.episodes.first { $0.id == id }
    }

    private var miniFraction: Double? {
        if player.duration > 1 {
            return min(1, max(0, player.current / player.duration))
        }
        return miniEpisode.flatMap { library.playedFraction(for: $0) }
    }

    /// Continue pill uses ListenMark. If 「听这段」 left the playhead elsewhere, seek back.
    private func playFromLibrary(_ episode: Episode) {
        library.acknowledgeContinue(episode.id)
        let resumeAt = library.resumeSeconds(for: episode.id)
        guard episode.isPlayableNow else {
            path.append(episode)
            return
        }
        if player.errorText != nil || ContinueListening.shouldReopenAtListenMark(
            currentID: player.currentID,
            episodeID: episode.id,
            playhead: player.current,
            resumeAt: resumeAt,
            clipInProgress: player.isClipPlayback
        ) {
            player.open(episode, resumeAt: resumeAt, force: true)
        } else if player.isPlaying {
            player.pause()
        } else {
            player.resume()
        }
    }

    private func toggleMini(_ episode: Episode) {
        if player.currentID == episode.id {
            player.toggle()
        } else {
            player.open(episode, resumeAt: library.resumeSeconds(for: episode.id))
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
