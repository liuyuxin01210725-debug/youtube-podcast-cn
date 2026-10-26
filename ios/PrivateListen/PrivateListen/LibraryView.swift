import SwiftUI
import UniformTypeIdentifiers

struct LibraryView: View {
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var player: PlayerModel
    @Environment(\.scenePhase) private var scenePhase
    @State private var picking = false

    var body: some View {
        NavigationStack {
            Group {
                if !library.hasFolder {
                    prompt(
                        title: "选择资料库",
                        systemImage: "folder",
                        detail: "在 iCloud 云盘里选中「中文播客私人库」。Mac 上完整生成的节目会出现在这里。只需选一次。"
                    )
                } else if library.episodes.isEmpty {
                    prompt(
                        title: "还没有节目",
                        systemImage: "music.note.list",
                        detail: "等 Mac 上这一期完整生成结束后，下拉刷新。试听不会出现在这里。"
                    )
                } else {
                    list
                }
            }
            .navigationTitle("私人听")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button(library.hasFolder ? "重新选择" : "选择资料库") {
                        picking = true
                    }
                }
            }
            .onAppear {
                player.onProgress = { id, seconds, duration in
                    Task { @MainActor in
                        library.remember(id: id, seconds: seconds, duration: duration)
                    }
                }
                library.reload()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { library.reload() }
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
        }
    }

    private var list: some View {
        List(library.episodes) { episode in
            NavigationLink(value: episode) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(episode.title)
                            .font(.body)
                            .foregroundStyle(.primary)
                        if library.isUnplayed(episode.id) {
                            Text("未听")
                                .font(.caption2)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Color.accentColor.opacity(0.15), in: Capsule())
                        }
                    }
                    Text(subtitle(episode))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 4)
            }
        }
        .refreshable { library.reload() }
    }

    private func subtitle(_ episode: Episode) -> String {
        var parts = [episode.durationLabel, episode.dateLabel]
        if episode.downloading { parts.append("正在从 iCloud 下载") }
        return parts.joined(separator: " · ")
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
        .refreshable { library.reload() }
    }
}
