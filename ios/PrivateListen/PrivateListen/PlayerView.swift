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

    private var playerScrollView: some View {
        ScrollView {
            VStack(spacing: 16) {
                VStack(spacing: 8) {
                    Text(episode.title)
                        .font(.title2.weight(.semibold))
                        .lineLimit(3)
                        .multilineTextAlignment(.center)
                    Text(episode.dateLabel)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                PlaybackControls(player: player, episode: episode, reading: reading, jump: jump) {
                    library.reload()
                    openEpisode(force: true)
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
            .padding(18)
        }
    }

    private func jump(_ node: ListenViewpoint, in reading: PlayerReading) {
        guard episode.unavailableReason == nil, player.isCurrent(episode), let audio = reading.audio, audio.isCurrent,
              audio.url == episode.audioURL, let seconds = node.tStart else { return }
        player.seek(to: seconds, waitForPlaybackAdvance: true, keepTransportRunning: true)
    }

    private var readingScrollView: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                Text(episode.title).font(.title2.weight(.semibold))
                ReadingElapsed(player: player)
                if let reference = episode.pendingTranscript, episode.unavailableReason == nil {
                    PendingTranscriptSection(episodeID: episode.id, audioURL: episode.audioURL, reference: reference)
                } else if let reading {
                    if !reading.nodes.isEmpty {
                        Text("分段").font(.headline)
                        ForEach(reading.nodes) { node in
                            Button { jump(node, in: reading) } label: {
                                HStack(alignment: .firstTextBaseline) {
                                    Text(listenClock(node.tStart ?? 0)).monospacedDigit()
                                    Text(node.viewpointText).multilineTextAlignment(.leading)
                                }
                            }
                            .accessibilityIdentifier("player.reading.node.\(node.id)")
                        }
                    }
                    if !reading.paragraphs.isEmpty {
                        Text("中文译稿").font(.headline)
                        ForEach(reading.paragraphs.indices, id: \.self) { index in
                            Text(reading.paragraphs[index]).textSelection(.enabled)
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

            ScrollViewReader { scroll in
                VStack(spacing: 0) {
                    if readingSide {
                        readingScrollView
                    } else {
                        playerScrollView
                    }
                }
                .onChange(of: readingSide) { _, reading in
                    if reading { isEditingListenNote = false }
                }
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
        // A timecode strip below the scrubber keeps adjacent viewpoints separately tappable.
        ScrollView(.horizontal) {
            HStack(spacing: 8) {
                ForEach(reading.nodes) { node in
                    Button { jump(node, reading) } label: {
                        Label(listenClock(node.tStart ?? 0), systemImage: "diamond.fill")
                            .font(.caption.monospacedDigit())
                            .padding(.horizontal, 8)
                            .frame(minHeight: 36)
                            .background(.quaternary, in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(listenClock(node.tStart ?? 0))，\(node.viewpointText)")
                    .accessibilityIdentifier("player.timeline.node.\(node.id)")
                }
            }
        }
        .scrollIndicators(.hidden)
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
