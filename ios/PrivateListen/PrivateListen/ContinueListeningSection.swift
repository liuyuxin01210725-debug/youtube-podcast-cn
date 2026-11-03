import SwiftUI

/// 资料库「继续播放」. One row per in-progress episode. Trailing swipe removes
/// it from this rail only; the episode stays in the library and keeps its ListenMark.
struct ContinueListeningSection: View {
    struct Row: Identifiable {
        let episode: Episode
        let item: ContinueListening.Item
        let playing: Bool

        var id: String { episode.id }
    }

    let rows: [Row]
    let onPlay: (Episode) -> Void
    let onDismiss: (Episode) -> Void

    var body: some View {
        Section("继续播放") {
            ForEach(rows) { row in
                HStack(spacing: 12) {
                    NavigationLink(value: row.episode) {
                        HStack(spacing: 12) {
                            Image(uiImage: ArtworkImage.uiImage(for: row.episode))
                                .resizable()
                                .scaledToFill()
                                .frame(width: 56, height: 56)
                                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                            VStack(alignment: .leading, spacing: 4) {
                                Text(row.episode.title)
                                    .font(.body)
                                    .foregroundStyle(.primary)
                                    .lineLimit(2)
                                    .multilineTextAlignment(.leading)
                                Text(ContinueListening.remainingLabel(
                                    seconds: row.item.seconds,
                                    duration: row.episode.durationSec
                                ))
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                if let fraction = row.item.fraction {
                                    ProgressView(value: fraction)
                                        .progressViewStyle(.linear)
                                        .tint(Color.primary.opacity(0.55))
                                }
                            }
                        }
                    }
                    Button {
                        onPlay(row.episode)
                    } label: {
                        Image(systemName: row.playing ? "pause.fill" : "play.fill")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(Color(.systemBackground))
                            .frame(width: 36, height: 36)
                            .background(Color.primary, in: Capsule())
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel(row.playing ? "暂停" : "继续播放")
                }
                .padding(.vertical, 4)
                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                    Button(role: .destructive) {
                        onDismiss(row.episode)
                    } label: {
                        Label("删除", systemImage: "trash")
                    }
                }
                .accessibilityElement(children: .contain)
                .accessibilityLabel("继续播放 \(row.episode.title)")
            }
        }
    }
}
