import SwiftUI
import UIKit

struct PlayerView: View {
    let episode: Episode
    private let opensPaused: Bool
    @EnvironmentObject private var player: PlayerModel
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var extracts: ExtractStore
    @State private var dragging = false
    @State private var scrub = 0.0
    @State private var isEditingListenNote = false

    init(episode: Episode, opensPaused: Bool = false) {
        self.episode = episode
        self.opensPaused = opensPaused
    }

    private var isPassiveOpen: Bool { opensPaused || episode.pendingTranscript != nil }

    private var playerScrollView: some View {
        ScrollView {
            VStack(spacing: 28) {
                VStack(spacing: 8) {
                    Text(episode.title)
                        .font(.title2)
                        .multilineTextAlignment(.center)
                    Text(episode.dateLabel)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
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
                    HStack {
                        Text(listenClock(dragging ? scrub : player.current))
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
                    .accessibilityLabel("后退 15 秒")
                    Button { player.toggle() } label: {
                        Image(systemName: player.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                            .font(.system(size: 68))
                    }
                    .accessibilityLabel(player.isPlaying ? "暂停" : (continueCaption == nil ? "播放" : "继续听"))
                    Button { player.skip(15) } label: {
                        Image(systemName: "goforward.15")
                            .font(.title)
                    }
                    .accessibilityLabel("前进 15 秒")
                }
                .buttonStyle(.plain)
                .disabled(player.errorText != nil)
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
                if let errorText = player.errorText {
                    VStack(spacing: 8) {
                        Text(errorText)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                        Button("重新检查") {
                            library.reload()
                            openEpisode(force: true)
                        }
                    }
                }

                if let reference = episode.pendingTranscript {
                    PendingTranscriptSection(episodeID: episode.id, audioURL: episode.audioURL, reference: reference)
                }
                Divider()
                ListenNoteSection(
                    episodeId: episode.id,
                    isFinished: library.isFinished(episode),
                    recordsOnOpen: !isPassiveOpen,
                    isEditing: $isEditingListenNote
                )
                if episode.pendingTranscript == nil {
                    ExtractDetailSection(episode: episode, extracts: extracts)
                }
            }
            .padding(24)
        }
    }

    var body: some View {
        ScrollViewReader { scroll in
            playerScrollView
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
        .safeAreaInset(edge: .top, spacing: 0) {
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
            if !isPassiveOpen {
                // A regular open clears a swipe-dismiss without writing ListenMark.
                library.acknowledgeContinue(episode)
            }
            openEpisode()
            extracts.load(episode: episode)
        }
        .onDisappear {
            player.fullPlayerOpen = false
            if player.isCurrent(episode) { player.checkpoint() }
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
