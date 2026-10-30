import SwiftUI

/// 详情提炼区：点星深抽后直接展示要点结果（方法/观点/实体/金句/开放问题）。
/// status 字段仍保留在模型里；UI 不做「待确认→确认/丢掉」主交互。
struct ExtractDetailSection: View {
    let episode: Episode
    @ObservedObject var extracts: ExtractStore
    @EnvironmentObject private var player: PlayerModel
    @State private var showSchemaPicker = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if let packet = extracts.packet(for: episode.id) {
                statusLine(packet)
                if packet.mode == .light {
                    Text(packet.summaryLine ?? "听完轻摘")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Text("轻摘一览；点星可深抽更多要点")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    resultBlocks(packet)
                } else {
                    switch packet.status {
                    case .queued, .running:
                        Text(packet.summaryLine ?? "正在提炼本集要点…")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    case .failed:
                        Text(packet.summaryLine ?? packet.failureReason ?? "提炼失败，可改主题重抽")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    case .ready, .stale:
                        if resultIsEmpty(packet) {
                            Text("暂无要点。可从菜单改主题重抽。")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        } else {
                            resultBlocks(packet)
                        }
                    }
                }
            } else {
                Text("点星开始提炼")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.top, 8)
        .confirmationDialog("本集改主题重抽", isPresented: $showSchemaPicker, titleVisibility: .visible) {
            ForEach(ExtractSchemaID.allCases, id: \.self) { schema in
                Button(schema.labelZH) {
                    extracts.reextract(episode: episode, schema: schema)
                }
            }
            Button("取消", role: .cancel) {}
        }
    }

    private var header: some View {
        HStack {
            Text("提炼")
                .font(.headline)
            Spacer()
            Button {
                extracts.toggleStar(episode: episode)
            } label: {
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
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .accessibilityLabel("提炼菜单")
        }
    }

    private func statusLine(_ packet: ExtractionPacket) -> some View {
        HStack(spacing: 8) {
            Text(packet.schemaId.labelZH)
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 2)
                .background(Color.primary.opacity(0.08), in: Capsule())
            Text(statusLabel(packet))
                .font(.caption)
                .foregroundStyle(.secondary)
            if packet.status == .stale {
                Text("需重抽")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
            Spacer()
        }
    }

    private func statusLabel(_ packet: ExtractionPacket) -> String {
        switch packet.status {
        case .queued: return "排队中"
        case .running: return "抽取中"
        case .ready:
            if packet.mode == .light { return "轻摘就绪" }
            let n = packet.visiblePointCount
            return n > 0 ? "已提炼 · 要点 \(n)" : "已提炼"
        case .failed:
            return packet.summaryLine ?? "失败"
        case .stale: return "已过期"
        }
    }

    private func resultIsEmpty(_ packet: ExtractionPacket) -> Bool {
        packet.visiblePointCount == 0
    }

    /// 直接列出 schema 有的槽；丢弃项不展；低置信仍展（便于查漏）。
    private func resultBlocks(_ packet: ExtractionPacket) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(packet.methods.filter { $0.status != .discarded }, id: \.id) { method in
                methodCard(method, packet: packet)
            }
            ForEach(packet.claims.filter { $0.status != .discarded }, id: \.id) { claim in
                claimCard(claim, packet: packet)
            }
            ForEach(packet.entities.filter { $0.status != .discarded }, id: \.id) { entity in
                entityCard(entity)
            }
            ForEach(packet.quotes.filter { $0.status != .discarded }, id: \.id) { quote in
                quoteCard(quote)
            }
            ForEach(packet.openQuestions.filter { $0.status != .discarded }, id: \.id) { q in
                openCard(q)
            }
        }
    }

    private func methodCard(_ method: ExtractMethod, packet: ExtractionPacket) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("方法")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                seekButton(tStart: method.tStart, tEnd: method.tEnd)
            }
            Text(method.title)
                .font(.subheadline.weight(.semibold))
            if let steps = method.steps, !steps.isEmpty {
                ForEach(Array(steps.enumerated()), id: \.offset) { idx, step in
                    Text("\(idx + 1). \(step)")
                        .font(.footnote)
                }
            }
            if let params = method.params, !params.isEmpty {
                Text(params.map { "\($0.key)=\($0.value)" }.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let disclaimer = method.disclaimer, !disclaimer.isEmpty {
                Text(disclaimer)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else if packet.schemaId == .health && method.pointType == "method_dose" {
                Text("非诊断、非处方，仅作记录")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if method.lowConfidence == true {
                Text("低置信 · 可改主题重抽")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    private func claimCard(_ claim: ExtractClaim, packet: ExtractionPacket) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("观点")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                seekButton(tStart: claim.tStart, tEnd: claim.tEnd)
            }
            Text(claim.text)
                .font(.subheadline)
            // §13.5 biz_invest：主视觉不展示看多/看空
            if packet.schemaId != .biz_invest, let stance = claim.stance, !stance.isEmpty, stance != "bullish", stance != "bearish" {
                Text(stance)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if claim.lowConfidence == true {
                Text("低置信 · 可改主题重抽")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    private func entityCard(_ entity: ExtractEntity) -> some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text("实体")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text("\(entity.type) · \(entity.nameZh ?? entity.nameSrc)")
                    .font(.subheadline)
                if entity.lowConfidence == true {
                    Text("低置信")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
            Spacer()
            seekButton(tStart: entity.tStart, tEnd: entity.tEnd)
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    private func quoteCard(_ quote: ExtractQuote) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("金句")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                seekButton(tStart: quote.tStart, tEnd: quote.tEnd)
            }
            Text(quote.text)
                .font(.subheadline)
            if quote.lowConfidence == true {
                Text("低置信")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    private func openCard(_ q: ExtractOpenQuestion) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("开放问题")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                seekButton(tStart: q.tStart, tEnd: q.tEnd)
            }
            Text(q.text)
                .font(.subheadline)
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    /// 切片 A「听这段」：seek → t_start，播到 t_end（缺省则 +20s）后暂停；§13.2 不写 ListenMark。
    private func seekButton(tStart: Double?, tEnd: Double?) -> some View {
        Button {
            guard let tStart else { return }
            player.playClip(from: tStart, to: tEnd)
        } label: {
            Label("听这段", systemImage: "headphones")
                .font(.caption.weight(.semibold))
                .labelStyle(.titleAndIcon)
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .accessibilityLabel("听这段")
        .disabled(tStart == nil)
    }
}
