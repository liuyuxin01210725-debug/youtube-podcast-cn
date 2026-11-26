import SwiftUI
import UIKit

struct PlayerView: View {
    private let routeEpisode: Episode
    private let opensPaused: Bool
    private let player: PlayerModel
    private let onPlayedEpisodeExit: () -> Void
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var extracts: ExtractStore
    @State private var readingSide = false
    @State private var reading: PlayerReading?
    @State private var isEditingListenNote = false
    @State private var originalPeek: OriginalPeek?
    @State private var pickRefusalMessage: String?

    init(episode: Episode, player: PlayerModel, opensPaused: Bool = false,
         onPlayedEpisodeExit: @escaping () -> Void = {}) {
        self.player = player
        self.onPlayedEpisodeExit = onPlayedEpisodeExit
        routeEpisode = episode
        self.opensPaused = opensPaused
    }

    private var episode: Episode {
        library.episodes.first {
            $0.id == routeEpisode.id && ($0.mediaKey == routeEpisode.mediaKey
                || $0.unavailableReason != nil || routeEpisode.unavailableReason != nil)
        } ?? routeEpisode
    }

    private var isPassiveOpen: Bool { opensPaused || episode.pendingTranscript != nil }

    private func playerScrollView(scroll: ScrollViewProxy) -> some View {
        ScrollView {
            VStack(spacing: 14) {
                VStack(spacing: 6) {
                    Text(episode.title)
                        .font(.title2.weight(.semibold))
                        .lineLimit(3)
                        .multilineTextAlignment(.center)
                    Text(episode.dateLabel)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    contentStatusRow
                }
                PlaybackControls(
                    player: player,
                    episode: episode,
                    reading: reading,
                    jump: jump,
                    peekNode: peekNode
                ) {
                    library.reload()
                    openEpisode(force: true)
                }

                if episode.unavailableReason == nil, episode.pendingTranscript == nil {
                    listenPicksSection
                }

                Divider()
                ListenAlongSection(
                    episode: episode,
                    player: player,
                    reading: reading,
                    onJumpToSeconds: jumpToSeconds
                ) {
                    withAnimation {
                        scroll.scrollTo(ListenNoteSection.editorScrollID, anchor: .top)
                    }
                }
                Divider()
                ListenNoteSection(
                    episodeId: episode.id,
                    isFinished: library.isFinished(episode),
                    recordsOnOpen: !isPassiveOpen && episode.unavailableReason == nil,
                    isEditing: $isEditingListenNote
                )
                if episode.pendingTranscript == nil {
                    ExtractDetailSection(episode: episode, extracts: extracts)
                }
            }
            .padding(16)
        }
    }

    private var contentStatusRow: some View {
        let status = EpisodeContentStatus.resolve(episode: episode)
        return HStack(spacing: 6) {
            Text(status.primaryLabel)
                .font(.caption.weight(.medium))
                .foregroundStyle(.primary)
                .accessibilityIdentifier("player.contentStatus")
            Text("·")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(status.detailLabel)
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("player.contentStatus.review")
        }
        .lineLimit(1)
    }

    @ViewBuilder
    private var listenPicksSection: some View {
        if let reading {
            if !reading.picks.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("本集精选")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                    ForEach(reading.picks) { pick in
                        Button {
                            jumpToPick(pick, in: reading)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(pick.node.viewpointText)
                                    .font(.subheadline)
                                    .foregroundStyle(.primary)
                                    .lineLimit(1)
                                    .multilineTextAlignment(.leading)
                                Text("起点 \(listenClock(pick.tStart)) · \(pick.anchorSpanLabel) · \(pick.reason)")
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 8))
                        }
                        .buttonStyle(.plain)
                        .simultaneousGesture(
                            LongPressGesture(minimumDuration: 0.5).onEnded { _ in
                                peekNode(pick.node, in: reading)
                            }
                        )
                        .accessibilityLabel("\(listenClock(pick.tStart))，\(pick.anchorSpanLabel)，\(pick.reason)，\(pick.node.viewpointText)")
                        .accessibilityIdentifier("player.picks.item.\(pick.id)")
                    }
                    if let pickRefusalMessage {
                        Text(pickRefusalMessage)
                            .font(.caption)
                            .foregroundStyle(.orange)
                            .accessibilityIdentifier("player.picks.refusal")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                Text("本集尚无精选")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("player.picks.empty")
            }
        }
    }

    private func jumpToPick(_ pick: ListenPick, in reading: PlayerReading) {
        guard originalPeek == nil else { return }
        guard episode.unavailableReason == nil,
              episode.pendingTranscript == nil,
              episode.isPlayableNow,
              player.isCurrent(episode),
              let audio = reading.audio,
              audio.isCurrent,
              audio.url == episode.audioURL,
              let packet = reading.packet,
              PointListening.refusal(
                  row: pick.node,
                  index: reading.index,
                  packet: packet,
                  episodeID: episode.id,
                  audioHash: audio.sha256,
                  duration: audio.duration
              ) == nil else {
            pickRefusalMessage = "成片已变化，无法跳到该精选"
            return
        }
        pickRefusalMessage = nil
        player.seek(to: pick.tStart, autoplay: true, waitForPlaybackAdvance: true, keepTransportRunning: true)
    }

    private func jump(_ node: ListenViewpoint, in reading: PlayerReading) {
        guard originalPeek == nil else { return }
        guard episode.unavailableReason == nil, player.isCurrent(episode), let audio = reading.audio, audio.isCurrent,
              audio.url == episode.audioURL, let seconds = node.tStart else { return }
        player.seek(to: seconds, waitForPlaybackAdvance: true, keepTransportRunning: true)
    }

    private func jumpToSeconds(_ seconds: Double) {
        guard episode.unavailableReason == nil, episode.isPlayableNow, player.isCurrent(episode) else { return }
        player.seek(to: seconds, waitForPlaybackAdvance: true, keepTransportRunning: true)
    }

    private func peekNode(_ node: ListenViewpoint, in reading: PlayerReading) {
        originalPeek = OriginalPeek(
            zhText: node.viewpointText,
            originalText: reading.originalText(for: node)
        )
    }

    private struct OriginalPeek: Identifiable {
        let id = UUID()
        let zhText: String
        let originalText: String?
    }

    private struct ReadingSource: Equatable {
        var episode: Episode
        var files: Set<String>?
        var unavailable: Episode.UnavailableReason?
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 4) {
                Picker("播放页", selection: $readingSide) {
                    Text("A 面 · 播放").tag(false)
                    Text("B 面 · 译稿与分段").tag(true)
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("player.side")
                .padding(.horizontal)
                .padding(.vertical, 4)
                if episode.pendingTranscript != nil {
                    Text("中文原音／自动识别／未人审")
                        .font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                        .background(.regularMaterial)
                        .accessibilityIdentifier("player.pendingTranscript.label")
                }
            }
            .background(.regularMaterial)
            .zIndex(1)

            if readingSide {
                PlayerReadingPane(
                    episode: episode,
                    player: player,
                    reading: reading,
                    jump: jump,
                    peekNode: peekNode,
                    onPeekLine: { zhText, originalText in
                        originalPeek = OriginalPeek(zhText: zhText, originalText: originalText)
                    }
                )
            } else {
                ScrollViewReader { scroll in
                    playerScrollView(scroll: scroll)
                        .onChange(of: isEditingListenNote) { _, isEditing in
                            guard isEditing else { return }
                            scroll.scrollTo(ListenNoteSection.editorScrollID)
                        }
                        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardDidShowNotification)) { _ in
                            guard isEditingListenNote else { return }
                            // Reposition the whole field once the keyboard has reduced the viewport.
                            withAnimation {
                                scroll.scrollTo(ListenNoteSection.editorScrollID, anchor: .center)
                            }
                        }
                }
            }
        }
        .onChange(of: readingSide) { _, reading in
            if reading { isEditingListenNote = false }
        }
        .navigationTitle("正在播放")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if episode.pendingTranscript == nil {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        extracts.toggleStar(episode: episode)
                    } label: {
                        Image(systemName: extracts.isStarred(episode.id) ? "star.fill" : "star")
                    }
                    .accessibilityLabel(extracts.isStarred(episode.id) ? "取消星标" : "星标并深抽")
                }
            }
        }
        .onAppear {
            player.fullPlayerOpen = true
            if !isPassiveOpen && episode.unavailableReason == nil {
                // A regular open clears a swipe-dismiss without writing ListenMark.
                library.acknowledgeContinue(episode)
            }
            openEpisode()
            extracts.load(episode: episode)
        }
        .task(id: ReadingSource(episode: episode, files: episode.revisionFiles, unavailable: episode.unavailableReason)) {
            reading = nil
            let loaded = await PlayerReading.load(episode)
            if !Task.isCancelled { reading = loaded }
        }
        .onDisappear {
            if player.isCurrent(episode), player.checkpoint() { onPlayedEpisodeExit() }
            player.fullPlayerOpen = false
        }
        .sheet(item: $originalPeek) { peek in
            NavigationStack {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if let original = peek.originalText, !original.isEmpty {
                            Text("对应原文")
                                .font(.headline)
                            Text(original)
                                .font(.body)
                                .textSelection(.enabled)
                                .accessibilityIdentifier("player.reading.original")
                        } else {
                            Text("本集没有对应原文")
                                .font(.body)
                                .foregroundStyle(.secondary)
                                .accessibilityIdentifier("player.reading.original.unavailable")
                        }
                        if !peek.zhText.isEmpty {
                            Divider()
                            Text("中文译稿")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                            Text(peek.zhText)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(18)
                }
                .navigationTitle("原文对照")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("关闭") { originalPeek = nil }
                    }
                }
            }
            .presentationDetents([.medium, .large])
        }
    }

    private func openEpisode(force: Bool = false) {
        player.open(
            episode,
            resumeAt: library.resumeSeconds(for: episode),
            force: force,
            autoplay: !library.isFinished(episode) && !isPassiveOpen
        )
    }
}

private struct ReadingElapsed: View {
    @ObservedObject var player: PlayerModel

    var body: some View {
        Text(listenClock(player.current))
            .font(.caption.monospacedDigit())
            .foregroundStyle(.secondary)
            .accessibilityIdentifier("player.reading.elapsed")
    }
}

private struct PlaybackControls: View {
    @ObservedObject var player: PlayerModel
    let episode: Episode
    let reading: PlayerReading?
    let jump: (ListenViewpoint, PlayerReading) -> Void
    let peekNode: (ListenViewpoint, PlayerReading) -> Void
    let retry: () -> Void
    @State private var dragging = false
    @State private var scrub = 0.0

    var body: some View {
        VStack(spacing: 8) {
            Slider(
                value: Binding(
                    get: { min(max(0, dragging ? scrub : player.current), sliderUpper) },
                    set: { scrub = $0 }
                ),
                in: 0...sliderUpper,
                onEditingChanged: { editing in
                    if editing {
                        scrub = player.current
                        dragging = true
                    } else {
                        dragging = false
                        player.seek(to: scrub)
                    }
                }
            )
            .accessibilityLabel("播放进度")
            .accessibilityIdentifier("player.timeline.slider")
            .disabled(episode.unavailableReason != nil)
            if let reading, !reading.nodes.isEmpty {
                timelineNodes(reading)
            }
            HStack {
                Text(listenClock(dragging ? scrub : player.current))
                    .accessibilityIdentifier("player.elapsed")
                Spacer()
                Text(player.duration > 0 ? listenClock(player.duration) : episode.durationLabel)
            }
            .font(.caption.monospacedDigit())
            .foregroundStyle(.secondary)
        }
        if let continueCaption {
            Text(continueCaption)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        HStack(spacing: 36) {
            Button { player.skip(-15) } label: {
                Image(systemName: "gobackward.15")
                    .font(.title)
            }
            .accessibilityIdentifier("player.skip.back")
            .accessibilityLabel("后退 15 秒")
            Button { player.toggle() } label: {
                Image(systemName: player.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                    .font(.system(size: 68))
                    .contentShape(Rectangle())
            }
            .accessibilityIdentifier("player.transport")
            .accessibilityLabel(player.isPlaying ? "暂停" : (continueCaption == nil ? "播放" : "继续听"))
            Button { player.skip(15) } label: {
                Image(systemName: "goforward.15")
                    .font(.title)
            }
            .accessibilityIdentifier("player.skip.forward")
            .accessibilityLabel("前进 15 秒")
        }
        .buttonStyle(.plain)
        .disabled(player.errorText != nil || episode.unavailableReason != nil)
        Picker("倍速", selection: Binding(
            get: { player.speed },
            set: { player.setSpeed($0) }
        )) {
            ForEach(ListenSpeed.allCases) { speed in
                Text(speed.label).tag(speed)
            }
        }
        .pickerStyle(.segmented)
        .accessibilityLabel("倍速")
        .accessibilityIdentifier("player.speed")
        if let errorText = episode.unavailableReason?.rawValue ?? player.errorText {
            VStack(spacing: 8) {
                Text(errorText)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Button("重新检查", action: retry)
            }
        }
    }

    private func timelineNodes(_ reading: PlayerReading) -> some View {
        let activeID = activeNodeID(in: reading)
        // A timecode strip below the scrubber keeps adjacent viewpoints separately tappable.
        return ScrollView(.horizontal) {
            HStack(spacing: 8) {
                ForEach(reading.nodes) { node in
                    let isActive = node.id == activeID
                    Button { jump(node, reading) } label: {
                        Label(listenClock(node.tStart ?? 0), systemImage: "diamond.fill")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(isActive ? Color.accentColor : Color.primary)
                            .padding(.horizontal, 8)
                            .frame(minHeight: 36)
                            .background(
                                (isActive ? Color.accentColor.opacity(0.18) : Color.secondary.opacity(0.12)),
                                in: Capsule()
                            )
                    }
                    .buttonStyle(.plain)
                    .simultaneousGesture(
                        LongPressGesture(minimumDuration: 0.5).onEnded { _ in
                            peekNode(node, reading)
                        }
                    )
                    .accessibilityLabel("\(listenClock(node.tStart ?? 0))，\(node.viewpointText)")
                    .accessibilityValue(isActive ? "当前段" : "")
                    .accessibilityIdentifier("player.timeline.node.\(node.id)")
                }
            }
        }
        .scrollIndicators(.hidden)
    }

    private func activeNodeID(in reading: PlayerReading) -> String? {
        guard let audio = reading.audio else { return nil }
        return SegmentPlaybackTracker.activeNodeID(
            at: player.current,
            in: reading.nodes,
            duration: audio.duration,
            isCurrentEpisode: player.isCurrent(episode),
            isAudioCurrent: audio.isCurrent,
            isPendingTranscript: episode.pendingTranscript != nil,
            isUnavailable: episode.unavailableReason != nil
        )
    }

    private var sliderUpper: Double {
        let value = player.duration > 1 ? player.duration : max(episode.durationSec ?? 0, 1)
        return value
    }

    /// Paused in the middle of an episode. Playback still starts at `player.current`.
    private var continueCaption: String? {
        guard player.errorText == nil, !player.isPlaying else { return nil }
        let position = player.current
        guard position > 5 else { return nil }
        if player.duration > 1, position >= player.duration - 15 { return nil }
        return "继续听 · \(listenClock(position))"
    }
}

private struct PlayerReadingPane: View {
    let episode: Episode
    @ObservedObject var player: PlayerModel
    let reading: PlayerReading?
    let jump: (ListenViewpoint, PlayerReading) -> Void
    let peekNode: (ListenViewpoint, PlayerReading) -> Void
    let onPeekLine: (String, String?) -> Void

    @State private var followState = SegmentScrollFollowState()

    private var activeNodeID: String? {
        guard let reading, let audio = reading.audio else { return nil }
        return SegmentPlaybackTracker.activeNodeID(
            at: player.current,
            in: reading.nodes,
            duration: audio.duration,
            isCurrentEpisode: player.isCurrent(episode),
            isAudioCurrent: audio.isCurrent,
            isPendingTranscript: episode.pendingTranscript != nil,
            isUnavailable: episode.unavailableReason != nil
        )
    }

    var body: some View {
        ScrollViewReader { scroll in
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text(episode.title).font(.title2.weight(.semibold))
                    ReadingElapsed(player: player)
                    if let reference = episode.pendingTranscript, episode.unavailableReason == nil {
                        PendingTranscriptSection(episodeID: episode.id, audioURL: episode.audioURL, reference: reference)
                    } else if let reading {
                        if !reading.nodes.isEmpty || !reading.lines.isEmpty {
                            Text("长按段落或分段可查看对应原文；没有原文时会明确说明")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .accessibilityIdentifier("player.reading.originalHint")
                        }
                        if !reading.nodes.isEmpty {
                            Text("分段").font(.headline)
                            ForEach(reading.nodes) { node in
                                let isActive = node.id == activeNodeID
                                Button { jump(node, reading) } label: {
                                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                                        RoundedRectangle(cornerRadius: 2)
                                            .fill(isActive ? Color.accentColor : Color.clear)
                                            .frame(width: 3, height: 16)
                                        Text(listenClock(node.tStart ?? 0))
                                            .monospacedDigit()
                                            .foregroundStyle(isActive ? Color.accentColor : Color.secondary)
                                        Text(node.viewpointText)
                                            .foregroundStyle(.primary)
                                            .multilineTextAlignment(.leading)
                                    }
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.vertical, 6)
                                    .padding(.horizontal, 8)
                                    .background(
                                        isActive ? Color.accentColor.opacity(0.12) : Color.clear,
                                        in: RoundedRectangle(cornerRadius: 8)
                                    )
                                }
                                .buttonStyle(.plain)
                                .id(node.id)
                                .onAppear { followState.nodeAppeared(node.id) }
                                .onDisappear { followState.nodeDisappeared(node.id) }
                                .simultaneousGesture(
                                    LongPressGesture(minimumDuration: 0.5).onEnded { _ in
                                        peekNode(node, reading)
                                    }
                                )
                                .accessibilityIdentifier("player.reading.node.\(node.id)")
                                .accessibilityValue(isActive ? "当前段" : "")
                                .accessibilityHint("长按查看对应原文")
                            }
                        }
                        if !reading.lines.isEmpty {
                            Text("中文译稿").font(.headline)
                            ForEach(Array(reading.lines.enumerated()), id: \.offset) { index, line in
                                Text(line.displayText)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .contentShape(Rectangle())
                                    .onLongPressGesture {
                                        onPeekLine(line.displayText, line.originalText)
                                    }
                                    .accessibilityIdentifier("player.reading.paragraph.\(index)")
                                    .accessibilityHint("长按查看对应原文")
                            }
                        } else if reading.nodes.isEmpty {
                            Text("本集暂无译稿或分段。")
                                .foregroundStyle(.secondary)
                                .accessibilityIdentifier("player.reading.empty")
                        }
                    } else {
                        ProgressView("正在读取节目包")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(18)
            }
            .modifier(UserScrollDetector(onUserScroll: {
                followState.userDidScroll()
            }))
            .onAppear {
                if followState.isFollowing, let activeID = activeNodeID {
                    scroll.scrollTo(activeID, anchor: .center)
                    followState.markAutoScrolled(activeID)
                }
            }
            .onChange(of: activeNodeID) { _, newID in
                guard followState.shouldAutoScroll(to: newID), let newID else { return }
                withAnimation {
                    scroll.scrollTo(newID, anchor: .center)
                }
                followState.markAutoScrolled(newID)
            }
            .onChange(of: episode.mediaKey) { _, _ in
                followState.resetForNewEpisode()
            }
            .safeAreaInset(edge: .bottom) {
                if followState.showsBackToCurrentButton(activeID: activeNodeID),
                   let activeID = activeNodeID {
                    Button {
                        followState.returnToCurrent()
                        withAnimation {
                            scroll.scrollTo(activeID, anchor: .center)
                        }
                        followState.markAutoScrolled(activeID)
                    } label: {
                        Label("回到当前段", systemImage: "arrow.uturn.backward.circle.fill")
                            .font(.subheadline.weight(.medium))
                            .padding(.horizontal, 14)
                            .padding(.vertical, 8)
                            .background(.regularMaterial, in: Capsule())
                            .shadow(radius: 2)
                    }
                    .padding(.bottom, 8)
                    .accessibilityIdentifier("player.reading.backToCurrent")
                }
            }
        }
    }
}

private struct UserScrollDetector: ViewModifier {
    let onUserScroll: () -> Void

    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.onScrollPhaseChange { _, newPhase in
                if newPhase == .interacting {
                    onUserScroll()
                }
            }
        } else {
            content.simultaneousGesture(
                DragGesture(minimumDistance: 6).onChanged { _ in
                    onUserScroll()
                }
            )
        }
    }
}
