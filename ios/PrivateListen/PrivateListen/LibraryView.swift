import SwiftUI
import UniformTypeIdentifiers

/// Option A: one chronological library list. No Discover mall, no extra tab.
/// 「继续播放」is at most the latest unfinished episode, including a short play
/// under 5 seconds. The card stays when the mini player is that same episode,
/// so leaving the player does not keep an older one in the slot. 「未听完」hides
/// finished; 「星」and「已听」stay on the chip bar. 在听／未听／主题 sit in「更多」.
/// Extract badges and the extract design entry stay as they are.
struct LibraryView: View {
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var player: PlayerModel
    @EnvironmentObject private var extracts: ExtractStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var path = NavigationPath()
    @State private var picking = false
    @State private var showExtractDesign = false
    @State private var filter: LibraryShelf.Chip = LibraryShelf.defaultChip
    @State private var searchText = ""
    @State private var searchPresented = false

    private let searchAlwaysVisibleAt = 40

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
            Section {
                chipBar
                    .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                    .listRowBackground(Color.clear)
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
            if !library.inbox.isEmpty && filter == LibraryShelf.defaultChip && searchText.isEmpty {
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
                    emptyFilterRow
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
        let rail = shelfRail
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(rail.resident) { chip in
                    chipButton(chip)
                }
                moreMenu(rail.more)
            }
            .padding(.vertical, 2)
        }
    }

    private func chipButton(_ chip: LibraryShelf.Chip) -> some View {
        Button {
            filter = chip
        } label: {
            chipLabel(chip.label, on: filter == chip)
        }
        .buttonStyle(.plain)
    }

    /// Existing filter popover. Not a tab, not Discover.
    private func moreMenu(_ chips: [LibraryShelf.Chip]) -> some View {
        let on = chips.contains(filter)
        let lead = chips.filter { !isShowChip($0) }
        let shows = chips.filter { isShowChip($0) }
        return Menu {
            ForEach(lead) { chip in
                moreItem(chip)
            }
            if !shows.isEmpty {
                Divider()
                ForEach(shows) { chip in
                    moreItem(chip)
                }
            }
        } label: {
            chipLabel(LibraryShelf.moreLabel, on: on)
        }
        .accessibilityLabel("更多筛选")
    }

    private func moreItem(_ chip: LibraryShelf.Chip) -> some View {
        Button {
            filter = chip
        } label: {
            if filter == chip {
                Label(chip.label, systemImage: "checkmark")
            } else {
                Text(chip.label)
            }
        }
    }

    private func chipLabel(_ title: String, on: Bool) -> some View {
        Text(title)
            .font(.subheadline.weight(on ? .semibold : .regular))
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(on ? Color.primary.opacity(0.9) : Color.secondary.opacity(0.12), in: Capsule())
            .foregroundStyle(on ? Color(.systemBackground) : Color.primary)
    }

    private func isShowChip(_ chip: LibraryShelf.Chip) -> Bool {
        if case .show = chip { return true }
        return false
    }

    private var episodeRows: some View {
        var density = 0
        return ForEach(visibleEpisodes) { episode in
            let badge = extracts.badge(for: episode.id, densityUsed: &density)
            NavigationLink(value: episode) {
                episodeRow(episode, badge: badge)
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

    private var shelfRail: LibraryShelf.ChipRail {
        LibraryShelf.rail(
            lenses: DiscoverCatalog.ownedLensChips(from: library.episodes),
            shows: distinctShows
        )
    }

    private var distinctShows: [String] {
        let names = Set(library.episodes.map(\.showName).filter { $0 != "未分类" })
        return names.sorted()
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

    private var emptyFilterState: LibraryShelf.EmptyState {
        LibraryShelf.emptyState(
            chip: filter,
            query: searchText,
            hasFinished: library.episodes.contains { library.isFinished($0.id) }
        )
    }

    private var emptyFilterRow: some View {
        let state = emptyFilterState
        return VStack(alignment: .leading, spacing: 6) {
            Text(state.message)
                .font(.footnote)
                .foregroundStyle(.secondary)
            if let action = state.action, let label = state.actionLabel {
                Button(label) {
                    applyEmptyAction(action)
                }
                .font(.footnote.weight(.semibold))
                .buttonStyle(.borderless)
            }
        }
        .padding(.vertical, 4)
    }

    private func applyEmptyAction(_ action: LibraryShelf.EmptyAction) {
        switch action {
        case .showArchive:
            filter = .finished
        case .showUnfinished:
            filter = LibraryShelf.defaultChip
        }
    }

    private func sanitizeFilter() {
        let rail = shelfRail
        switch filter {
        case .all, .starred, .finished, .inProgress, .unplayed:
            return
        case .lens, .show:
            if !rail.more.contains(filter) {
                filter = LibraryShelf.defaultChip
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
            guard ContinueListening.showsContinueCard(
                continueID: episode.id,
                playerID: player.currentID,
                controlsUsable: miniTransportUsable
            ) else { return nil }
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

    /// Mini-player transport can play or pause this episode. A download or open
    /// error still shows the bar, but that bar is not usable control, so the
    /// Continue card stays. This does not create a player.
    private var miniTransportUsable: Bool {
        guard let id = player.currentID, !id.isEmpty else { return false }
        if let error = player.errorText?.trimmingCharacters(in: .whitespacesAndNewlines), !error.isEmpty {
            return false
        }
        return true
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

    /// Title is the identity. One secondary line. Listen-note gray（上次听到）stays
    /// off the row; the player detail is one tap away and the list keeps its place.
    /// Chips sit under the title so large Dynamic Type cannot crush it sideways.
    private func episodeRow(_ episode: Episode, badge: String?) -> some View {
        let marks = LibraryShelf.rowMarks(
            listen: LibraryShelf.rowListenChip(
                chip: filter,
                finished: library.isFinished(episode.id),
                inProgress: library.isInProgress(episode.id),
                unplayed: library.isUnplayed(episode.id)
            ),
            starred: extracts.isStarred(episode.id),
            extra: badge
        )
        let secondary = rowSecondary(episode)
        return VStack(alignment: .leading, spacing: 4) {
            Text(episode.title)
                .font(.body)
                .foregroundStyle(.primary)
                .lineLimit(LibraryShelf.titleLineLimit)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            if !secondary.isEmpty {
                Text(secondary)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            if !marks.isEmpty {
                HStack(spacing: 6) {
                    ForEach(0..<marks.count, id: \.self) { index in
                        rowMark(marks[index])
                    }
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func rowSecondary(_ episode: Episode) -> String {
        let remaining: String?
        if library.isInProgress(episode.id), let duration = episode.durationSec, duration > 1 {
            remaining = ContinueListening.remainingLabel(
                seconds: library.resumeSeconds(for: episode.id),
                duration: duration
            )
        } else {
            remaining = nil
        }
        return LibraryShelf.rowSecondary(
            showName: episode.showName,
            durationLabel: episode.durationLabel,
            remainingLabel: remaining,
            downloading: episode.downloading
        )
    }

    @ViewBuilder
    private func rowMark(_ mark: LibraryShelf.RowMark) -> some View {
        switch mark {
        case .star:
            Image(systemName: "star.fill")
                .font(.caption2)
                .foregroundStyle(.yellow)
        case .text(let title, let emphasized):
            listenBadge(title, emphasized: emphasized)
        }
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
