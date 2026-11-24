import SwiftUI

/// Library transport lives in the navigation bar so the list keeps its full height.
struct MiniPlayerBar: View {
    let episode: Episode
    let isPlaying: Bool
    let onToggle: () -> Void

    var body: some View {
        NavigationLink(value: episode) {
            Image(systemName: "waveform")
        }
        .accessibilityLabel("打开 \(episode.title)")
        .accessibilityIdentifier("library.miniPlayer.open")

        Button(action: onToggle) {
            Image(systemName: isPlaying ? "pause.fill" : "play.fill")
        }
        .accessibilityLabel(isPlaying ? "暂停" : "继续播放")
        .accessibilityIdentifier("library.miniPlayer.transport")
    }
}
