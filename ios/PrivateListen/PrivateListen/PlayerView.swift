import SwiftUI

struct PlayerView: View {
    let episode: Episode
    @EnvironmentObject private var player: PlayerModel
    @EnvironmentObject private var library: LibraryStore
    @State private var dragging = false
    @State private var scrub = 0.0

    var body: some View {
        VStack(spacing: 28) {
            Spacer()
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
                        player.open(episode, resumeAt: library.resumeSeconds(for: episode.id), force: true)
                    }
                }
            }
            Spacer()
        }
        .padding(24)
        .navigationTitle("正在播放")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            player.open(episode, resumeAt: library.resumeSeconds(for: episode.id))
        }
        .onDisappear {
            player.checkpoint()
        }
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
