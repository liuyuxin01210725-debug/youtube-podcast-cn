import SwiftUI

/// Frosted bar pinned to the bottom of 资料库 while something is loaded.
/// Hidden on the full player. Transport only: it does not move the ListenMark resume point.
struct MiniPlayerBar: View {
    let episode: Episode
    let isPlaying: Bool
    let fraction: Double?
    let status: String?
    let onToggle: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            if let fraction {
                ProgressView(value: min(1, max(0, fraction)))
                    .progressViewStyle(.linear)
                    .tint(Color.primary.opacity(0.7))
                    .frame(height: 2)
                    .accessibilityHidden(true)
            }
            HStack(spacing: 12) {
                NavigationLink(value: episode) {
                    HStack(spacing: 12) {
                        Image(uiImage: ArtworkImage.uiImage(for: episode))
                            .resizable()
                            .scaledToFill()
                            .frame(width: 42, height: 42)
                            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(episode.title)
                                .font(.subheadline.weight(.medium))
                                .lineLimit(1)
                                .foregroundStyle(.primary)
                            if let status, !status.isEmpty {
                                Text(status)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel("打开 \(episode.title)")
                Button(action: onToggle) {
                    Image(systemName: isPlaying ? "pause.fill" : "play.fill")
                        .font(.title3)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(isPlaying ? "暂停" : "继续播放")
            }
            .padding(.leading, 12)
            .padding(.trailing, 4)
            .padding(.vertical, 6)
        }
        .background(.ultraThinMaterial)
    }
}
