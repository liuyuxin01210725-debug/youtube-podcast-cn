import SwiftUI

/// One primary list: actual statements/questions. Legacy extraction slots remain internal inputs.
struct ExtractDetailSection: View {
    let episode: Episode
    @ObservedObject var extracts: ExtractStore
    @State private var showSchemaPicker = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if let packet = extracts.packet(for: episode.id) {
                Text(ExtractDisplay.detailStatusLabel(packet))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if extracts.refreshing.contains(episode.id) {
                    Text("正在更新提炼…").font(.caption).foregroundStyle(.secondary)
                }
                if ExtractDisplay.isShowable(packet) {
                    PointListenRows(episode: episode, packet: packet)
                } else {
                    Text(packet.failureReason == ExtractDisplay.mockNotOfficialReason
                         ? ExtractDisplay.missingLiveKeySummary
                         : packet.summaryLine ?? "观点索引尚未就绪")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            } else {
                Text(extracts.isStarred(episode.id) ? "已收藏，提炼结果同步中" : "点星开始提炼")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.top, 8)
        .confirmationDialog("本集改主题重抽", isPresented: $showSchemaPicker, titleVisibility: .visible) {
            ForEach(ExtractSchemaID.allCases, id: \.self) { schema in
                Button(schema.labelZH) { extracts.reextract(episode: episode, schema: schema) }
            }
            Button("取消", role: .cancel) {}
        }
    }

    private var header: some View {
        HStack {
            Text("点段跳听").font(.headline)
            Spacer()
            Button { extracts.toggleStar(episode: episode) } label: {
                Image(systemName: extracts.isStarred(episode.id) ? "star.fill" : "star")
            }
            .accessibilityLabel(extracts.isStarred(episode.id) ? "取消星标" : "星标并深抽")
            Menu {
                Button("本集改主题重抽") { showSchemaPicker = true }
                if let md = extracts.exportMarkdown(for: episode) {
                    ShareLink(item: md, subject: Text(episode.title), message: Text("抽取导出")) {
                        Label("导出 Markdown", systemImage: "square.and.arrow.up")
                    }
                }
            } label: { Image(systemName: "ellipsis.circle") }
            .accessibilityLabel("提炼菜单")
        }
    }
}

private struct PointListenRows: View {
    let episode: Episode
    let packet: ExtractionPacket
    @EnvironmentObject private var player: PlayerModel
    @EnvironmentObject private var pointStars: PointStarStore
    @State private var index: PointListenIndex?
    @State private var audio: PointListenAudio?
    @State private var verificationText = "正在核对本集成片…"

    var body: some View {
        let rows = PointListening.rows(packet: packet, index: index)
        VStack(alignment: .leading, spacing: 10) {
            Text("点一句，从这里继续往后听")
                .font(.caption).foregroundStyle(.secondary)
            if rows.isEmpty {
                Text("本集暂无一句话观点或问题索引；仍可完整收听。")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            if audio == nil {
                Text(verificationText).font(.caption).foregroundStyle(.secondary)
            }
            ForEach(rows) { row in
                let reason = refusal(row)
                HStack(alignment: .top, spacing: 8) {
                    Button {
                        guard let audio, let index else { return }
                        if !player.jumpContinue(episode: episode, row: row, index: index, packet: packet, audio: audio) {
                            self.audio = nil
                            verificationText = "成片已变化或无法定位，请重新打开本集核对。"
                        }
                    } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(row.viewpointText).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                            if let reason {
                                Text(reason).font(.caption2).foregroundStyle(.secondary)
                            } else if let start = row.tStart {
                                Label("\(listenClock(start)) 起继续听", systemImage: "play.fill")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(reason != nil)
                    .accessibilityIdentifier("point-listen-\(row.id)")
                    .accessibilityHint(reason ?? "跳到这一句对应处，连续往后播放")
                    Button {
                        pointStars.toggle(workId: episode.id, pointId: row.sourcePointId ?? row.id, kind: row.sourceKind ?? "viewpoint")
                    } label: {
                        Image(systemName: pointStars.isStarred(workId: episode.id, pointId: row.sourcePointId ?? row.id) ? "star.fill" : "star")
                            .font(.caption).frame(width: 28, height: 28)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("收藏要点")
                }
                .padding(12)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
            }
        }
        .task(id: Source(packet: packet, url: episode.audioURL)) { await verify() }
    }

    private struct Source: Hashable {
        let packet: ExtractionPacket
        let url: URL
    }

    private func refusal(_ row: ListenViewpoint) -> String? {
        if let audio, !audio.isCurrent { return "成片已变化，请重新打开本集核对" }
        return PointListening.refusal(row: row, index: index, packet: packet, episodeID: episode.id,
            audioHash: audio?.sha256, duration: audio?.duration ?? 0)
    }

    @MainActor
    private func verify() async {
        index = nil
        audio = nil
        let url = episode.audioURL.deletingLastPathComponent().appendingPathComponent(PointListening.indexFileName)
        guard FileManager.default.fileExists(atPath: url.path) else {
            verificationText = "尚无成片绑定索引，以下仅供阅读。"
            return
        }
        do {
            let loaded = try JSONDecoder().decode(PointListenIndex.self, from: Data(contentsOf: url))
            index = loaded
            verificationText = "正在核对本集成片…"
            guard LocalAudio.isPlayable(episode.audioURL) else { throw CocoaError(.fileReadNoSuchFile) }
            let verified = try await PointListenAudio.load(episode.audioURL)
            guard !Task.isCancelled else { return }
            audio = verified
        } catch {
            guard !Task.isCancelled else { return }
            verificationText = "本集索引或成片尚无法核实，跳转已停用。"
        }
    }
}
