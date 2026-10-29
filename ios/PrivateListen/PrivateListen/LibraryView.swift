import SwiftUI
import UniformTypeIdentifiers

struct LibraryView: View {
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var player: PlayerModel
    @Environment(\.scenePhase) private var scenePhase
    @State private var picking = false
    @State private var showExtractDesign = false

    var body: some View {
        NavigationStack {
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
                        detail: "从 Safari、YouTube 或播客 App 分享链接到「私人听」。Mac 转换完成后，下拉即可看到。试听不会出现在这里。"
                    )
                } else {
                    list
                }
            }
            .navigationTitle("私人听")
            .toolbar {
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Menu {
                        Button {
                            Task { @MainActor in
                                showExtractDesign = true
                            }
                        } label: {
                            Label("收藏≠拥有 · 设计", systemImage: "doc.plaintext")
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                    .accessibilityLabel("更多")

                    Button(library.hasCloudFolder ? "重新选择" : "选择资料库") {
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
                if phase == .active {
                    library.reload()
                } else {
                    player.checkpoint()
                }
            }
            .task(id: scenePhase) {
                // While open, keep picking up Mac publishes and finished iCloud downloads.
                guard scenePhase == .active else { return }
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(library.waitingOnCloud ? 8 : 60))
                    if Task.isCancelled { break }
                    library.reload()
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
            if !library.inbox.isEmpty {
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
            if !library.episodes.isEmpty {
                Section {
                    ForEach(library.episodes) { episode in
                        NavigationLink(value: episode) {
                            VStack(alignment: .leading, spacing: 4) {
                                HStack(alignment: .firstTextBaseline, spacing: 8) {
                                    Text(episode.title)
                                        .font(.body)
                                        .foregroundStyle(.primary)
                                    if library.isUnplayed(episode.id) {
                                        listenBadge("未听", emphasized: true)
                                    } else if library.isFinished(episode.id) {
                                        listenBadge("已听完", emphasized: false)
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
                    }
                }
            }
        }
        .refreshable { library.reload() }
    }

    private func subtitle(_ episode: Episode) -> String {
        var parts = [episode.durationLabel, episode.dateLabel]
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
        .refreshable { library.reload() }
    }
}
