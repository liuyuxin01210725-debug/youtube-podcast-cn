import SwiftUI

/// 详情提炼区：默认只展待确认；health「仅记录」+免责；跳回原句只 seek。
struct ExtractDetailSection: View {
    let episode: Episode
    @ObservedObject var extracts: ExtractStore
    @EnvironmentObject private var player: PlayerModel
    @State private var showSchemaPicker = false
    @State private var showConfirmed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if let packet = extracts.packet(for: episode.id) {
                statusLine(packet)
                if packet.mode == .light {
                    Text(packet.summaryLine ?? "听完轻摘")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Text("轻摘不进待确认")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                } else {
                    pendingBlocks(packet)
                    if showConfirmed {
                        confirmedBlocks(packet)
                    } else if hasConfirmed(packet) {
                        Button("显示已确认 / 低置信") { showConfirmed = true }
                            .font(.footnote)
                    }
                }
            } else {
                Text("星标本集以启动深抽，或听完后自动轻摘。")
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
        case .ready: return packet.mode == .light ? "轻摘就绪" : "待确认 \(packet.pendingConfirmCount ?? packet.computedPendingCount)"
        case .failed:
            return packet.summaryLine ?? "失败"
        case .stale: return "已过期"
        }
    }

    private func pendingBlocks(_ packet: ExtractionPacket) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(packet.methods.filter { $0.status == .candidate && !($0.lowConfidence ?? false) }, id: \.id) { method in
                methodCard(method, packet: packet)
            }
            ForEach(packet.claims.filter { $0.status == .candidate && !($0.lowConfidence ?? false) }, id: \.id) { claim in
                claimCard(claim, packet: packet)
            }
            ForEach(packet.entities.filter { $0.status == .candidate && !($0.lowConfidence ?? false) }, id: \.id) { entity in
                entityCard(entity)
            }
            ForEach(packet.openQuestions.filter { $0.status == .candidate }, id: \.id) { q in
                openCard(q)
            }
        }
    }

    private func confirmedBlocks(_ packet: ExtractionPacket) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("已确认 / 其它")
                .font(.caption)
                .foregroundStyle(.secondary)
            ForEach(packet.methods.filter { $0.status != .candidate || ($0.lowConfidence ?? false) }, id: \.id) { method in
                methodCard(method, packet: packet)
            }
            ForEach(packet.claims.filter { $0.status != .candidate || ($0.lowConfidence ?? false) }, id: \.id) { claim in
                claimCard(claim, packet: packet)
            }
        }
    }

    private func hasConfirmed(_ packet: ExtractionPacket) -> Bool {
        packet.methods.contains { $0.status != .candidate || ($0.lowConfidence ?? false) }
            || packet.claims.contains { $0.status != .candidate || ($0.lowConfidence ?? false) }
    }

    private func methodCard(_ method: ExtractMethod, packet: ExtractionPacket) -> some View {
        VStack(alignment: .leading, spacing: 6) {
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
            HStack(spacing: 12) {
                let label = method.uiPrimaryLabel
                    ?? (packet.schemaId == .health && method.pointType == "method_dose" ? "仅记录" : primaryLabel(method.defaultAction))
                Button(label) {
                    extracts.setItemStatus(episode: episode, itemId: method.id, status: .confirmed)
                }
                .buttonStyle(.borderedProminent)
                if let disclaimer = method.disclaimer, !disclaimer.isEmpty {
                    Text(disclaimer)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                } else if packet.schemaId == .health && method.pointType == "method_dose" {
                    Text("非诊断、非处方，可丢弃")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                seekButton(tStart: method.tStart)
                Button("丢弃", role: .destructive) {
                    extracts.setItemStatus(episode: episode, itemId: method.id, status: .discarded)
                }
                .font(.caption)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    private func claimCard(_ claim: ExtractClaim, packet: ExtractionPacket) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(claim.text)
                .font(.subheadline)
            // §13.5 biz_invest：主视觉不展示看多/看空
            if packet.schemaId != .biz_invest, let stance = claim.stance, !stance.isEmpty, stance != "bullish", stance != "bearish" {
                Text(stance)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button("确认") {
                    extracts.setItemStatus(episode: episode, itemId: claim.id, status: .confirmed)
                }
                .buttonStyle(.bordered)
                Spacer()
                seekButton(tStart: claim.tStart)
                Button("丢弃", role: .destructive) {
                    extracts.setItemStatus(episode: episode, itemId: claim.id, status: .discarded)
                }
                .font(.caption)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    private func entityCard(_ entity: ExtractEntity) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(entity.type) · \(entity.nameZh ?? entity.nameSrc)")
                    .font(.subheadline)
                Text("默认候选，确认后才关注")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("确认") {
                extracts.setItemStatus(episode: episode, itemId: entity.id, status: .confirmed)
            }
            .buttonStyle(.bordered)
            seekButton(tStart: entity.tStart)
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    private func openCard(_ q: ExtractOpenQuestion) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(q.text)
                .font(.subheadline)
            HStack {
                Button("确认") {
                    extracts.setItemStatus(episode: episode, itemId: q.id, status: .confirmed)
                }
                .buttonStyle(.bordered)
                Spacer()
                seekButton(tStart: q.tStart)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }

    /// §13.2：跳回原句只 seek，不写 ListenMark。
    private func seekButton(tStart: Double?) -> some View {
        Button {
            guard let tStart else { return }
            player.seek(to: tStart, autoplay: true)
        } label: {
            Image(systemName: "text.badge.checkmark")
        }
        .accessibilityLabel("跳回原句")
        .disabled(tStart == nil)
    }

    private func primaryLabel(_ action: String?) -> String {
        switch action {
        case "record_only": return "仅记录"
        case "try_later": return "待实践"
        case "ferment_note": return "记工艺"
        case "monitor_candidate": return "观察候选"
        default: return "确认"
        }
    }
}
