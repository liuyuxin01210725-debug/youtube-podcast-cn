import SwiftUI

/// 播放页「听后感」：输入极简（单一多行框）；价值在写完后的下游动作卡。
struct ListenNoteSection: View {
    let episodeId: String
    let isFinished: Bool
    @EnvironmentObject private var listenNotes: ListenNoteStore
    @EnvironmentObject private var library: LibraryStore

    @State private var draftText: String = ""
    @State private var tags: [String] = []
    @State private var commitTask: Task<Void, Never>?
    @State private var savedFlash = false
    @State private var confirmClear = false
    @State private var hideDownstream = false
    @State private var insightSaved = false
    @State private var libraryHit: ListenNoteDownstream.LibraryHit?
    @State private var libraryChecked = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if listenNotes.shouldShowFinishedPrompt(episodeId: episodeId, isFinished: isFinished) {
                finishedPrompt
            }

            HStack(alignment: .firstTextBaseline) {
                Text("听后感")
                    .font(.headline)
                Spacer(minLength: 0)
                if savedFlash {
                    Text("已记下")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .transition(.opacity.combined(with: .move(edge: .trailing)))
                        .accessibilityLabel("听后感已保存")
                }
            }

            // 单一多行输入：placeholder 即引导；无芯片/长说明/双层展开
            ZStack(alignment: .topLeading) {
                if ListenNote.trimmed(draftText).isEmpty {
                    Text("这期对你真正想记住的是什么？")
                        .font(.body)
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 16)
                        .allowsHitTesting(false)
                }
                TextEditor(text: $draftText)
                    .frame(minHeight: 88, maxHeight: 160)
                    .padding(8)
                    .scrollContentBackground(.hidden)
                    .focused($focused)
                    .accessibilityLabel("听后感")
            }
            .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .stroke(Color.primary.opacity(0.08), lineWidth: 1)
            )

            if hasDraftContent {
                HStack {
                    Spacer(minLength: 0)
                    Button("清除听后感", role: .destructive) {
                        confirmClear = true
                    }
                    .font(.footnote)
                }
            }

            if hasDraftContent && !hideDownstream {
                downstreamCards
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear(perform: loadFromStore)
        .onChange(of: episodeId) { _, _ in
            commit()
            loadFromStore()
            savedFlash = false
            hideDownstream = false
        }
        .onChange(of: draftText) { _, _ in scheduleCommit() }
        .onChange(of: focused) { old, new in
            if old == true, new == false {
                commitNow(flash: true)
            }
        }
        .onDisappear { commit() }
        .onAppear {
            if listenNotes.shouldShowFinishedPrompt(episodeId: episodeId, isFinished: isFinished) {
                listenNotes.markPromptShown(episodeId: episodeId)
            }
        }
        .confirmationDialog(
            "清除这期听后感？",
            isPresented: $confirmClear,
            titleVisibility: .visible
        ) {
            Button("清除", role: .destructive) {
                draftText = ""
                tags = []
                libraryHit = nil
                libraryChecked = false
                insightSaved = false
                listenNotes.clear(episodeId: episodeId)
                UIEventLog.note("listen_note_clear", episodeId: episodeId)
                flashSaved()
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("仅本机删除这期听后感。")
        }
    }

    // MARK: - Downstream（最多 2 卡）

    @ViewBuilder
    private var downstreamCards: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("写完之后")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Button("收起") {
                    hideDownstream = true
                    UIEventLog.note("listen_note_downstream_dismiss", episodeId: episodeId)
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)
            }

            // 卡 1：库内关联 或 诚实「暂无」
            libraryRelateCard

            // 卡 2：记一笔给后续（insights sidecar）
            insightCard

            // 极轻：标待回看（只加 tag，不占「动作卡」名额以外的视觉重量）
            if !tags.contains(where: { $0 == "待回看" }) {
                Button {
                    tags = ListenNote.normalizeTags(tags + ["待回看"])
                    commitNow(flash: true)
                    UIEventLog.note(
                        "listen_note_tag_add",
                        episodeId: episodeId,
                        fields: ["tag": "待回看"]
                    )
                } label: {
                    Text("标待回看")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
            } else {
                Text("已标 · 待回看")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(12)
        .background(Color.accentColor.opacity(0.06), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .onAppear { refreshDownstream(writeInsightIfNeeded: true) }
        .onChange(of: draftText) { _, _ in
            // 文案变了：重新匹配；insights 在 commit 时写
            libraryChecked = false
            refreshDownstream(writeInsightIfNeeded: false)
        }
    }

    @ViewBuilder
    private var libraryRelateCard: some View {
        if let hit = resolvedLibraryHit, let ep = library.episodes.first(where: { $0.id == hit.episodeId }) {
            VStack(alignment: .leading, spacing: 6) {
                Text("库内关联")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(hit.rationaleZh)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                NavigationLink(value: ep) {
                    HStack(spacing: 10) {
                        Image(uiImage: ArtworkImage.uiImage(for: ep))
                            .resizable()
                            .scaledToFill()
                            .frame(width: 36, height: 36)
                            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(ep.title)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(.primary)
                                .lineLimit(2)
                            Text("点开即播 · 库内已有")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.tertiary)
                    }
                    .padding(10)
                    .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("库内关联 \(ep.title)")
                .simultaneousGesture(TapGesture().onEnded {
                    UIEventLog.note(
                        "listen_note_library_hit_open",
                        episodeId: episodeId,
                        fields: [
                            "to_episode_id": hit.episodeId,
                            "source": hit.source,
                        ]
                    )
                })
            }
        } else if libraryChecked {
            Text("暂无库内关联")
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityLabel("暂无库内关联")
        }
    }

    private var insightCard: some View {
        HStack(alignment: .center, spacing: 8) {
            Image(systemName: insightSaved ? "checkmark.circle.fill" : "tray.and.arrow.down")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(insightSaved ? "已留给后续关联" : "记一笔给后续")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            if !insightSaved {
                Button("写下") {
                    _ = ListenNoteDownstream.writeInsight(episodeId: episodeId, noteText: draftText)
                    insightSaved = true
                    flashSaved()
                }
                .font(.caption.weight(.semibold))
            }
        }
        .accessibilityLabel(insightSaved ? "已留给后续关联" : "记一笔给后续")
    }

    private var resolvedLibraryHit: ListenNoteDownstream.LibraryHit? {
        libraryHit
    }

    private func refreshDownstream(writeInsightIfNeeded: Bool) {
        let text = ListenNote.trimmed(draftText)
        guard !text.isEmpty else {
            libraryHit = nil
            libraryChecked = false
            insightSaved = false
            return
        }
        if !libraryChecked {
            libraryHit = ListenNoteDownstream.findLibraryHit(
                currentEpisodeId: episodeId,
                noteText: text,
                ownedEpisodes: library.episodes,
                noteLookup: { listenNotes.note(for: $0) }
            )
            libraryChecked = true
            if let hit = libraryHit {
                UIEventLog.note(
                    "listen_note_library_hit",
                    episodeId: episodeId,
                    fields: [
                        "to_episode_id": hit.episodeId,
                        "source": hit.source,
                    ]
                )
            } else {
                UIEventLog.note("listen_note_library_miss", episodeId: episodeId)
            }
        }
        if writeInsightIfNeeded {
            if ListenNoteDownstream.insight(for: episodeId) != nil {
                insightSaved = true
            } else {
                _ = ListenNoteDownstream.writeInsight(episodeId: episodeId, noteText: text)
                insightSaved = true
            }
        } else if ListenNoteDownstream.insight(for: episodeId) != nil {
            insightSaved = true
        }
    }

    // MARK: - Prompt / load / commit

    private var finishedPrompt: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "lightbulb")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 4) {
                Text("听完了 · 留下一句？")
                    .font(.subheadline.weight(.semibold))
                Text("写完后可看库内关联或留给后续。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            Button("以后") {
                listenNotes.dismissPrompt(episodeId: episodeId)
            }
            .font(.caption.weight(.semibold))
        }
        .padding(12)
        .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("听完轻提示：留下听后感，或点以后关闭")
    }

    private var hasDraftContent: Bool {
        !ListenNote.trimmed(draftText).isEmpty || !tags.isEmpty
    }

    private func loadFromStore() {
        if let n = listenNotes.note(for: episodeId) {
            draftText = ListenNoteDownstream.mergedDisplayText(headline: n.headline, body: n.body)
            tags = n.tags
            insightSaved = ListenNoteDownstream.insight(for: episodeId) != nil
            libraryChecked = false
            if n.hasContent {
                refreshDownstream(writeInsightIfNeeded: false)
            }
        } else {
            draftText = ""
            tags = []
            libraryHit = nil
            libraryChecked = false
            insightSaved = false
        }
    }

    private func scheduleCommit() {
        commitTask?.cancel()
        commitTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 400_000_000)
            guard !Task.isCancelled else { return }
            commit()
        }
    }

    private func commitNow(flash: Bool) {
        commitTask?.cancel()
        commit()
        if flash { flashSaved() }
        if hasDraftContent {
            // 保存后刷新下游 + 自动写 insights
            libraryChecked = false
            refreshDownstream(writeInsightIfNeeded: true)
        }
    }

    private func commit() {
        let text = draftText
        let trimmed = ListenNote.trimmed(text)
        // 单字段收敛：全文进 body；首行填 headline（兼容旧列表摘要 / 搜索）
        let headline = trimmed.isEmpty ? "" : ListenNoteDownstream.firstLine(trimmed)
        let body = trimmed
        listenNotes.upsert(
            episodeId: episodeId,
            headline: headline,
            body: body,
            tags: tags
        )
    }

    private func flashSaved() {
        withAnimation(.easeInOut(duration: 0.15)) {
            savedFlash = true
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            withAnimation(.easeInOut(duration: 0.25)) {
                savedFlash = false
            }
        }
    }
}
