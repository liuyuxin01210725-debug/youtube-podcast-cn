import SwiftUI

struct BookDetailView: View {
    let route: BookRoute
    @EnvironmentObject private var books: BookStore
    @EnvironmentObject private var player: PlayerModel
    var body: some View {
        Group {
            if let edition = books.edition(bookID: route.bookID, revision: route.revision) {
                if let id = route.chapterID, let chapter = edition.manifest.chapters.first(where: { $0.id == id }) {
                    BookChapterView(edition: edition, chapter: chapter, autoPlay: route.play)
                } else {
                    List {
                        Section {
                            Text(edition.manifest.title).font(.title2)
                            Text("源清单：\(edition.manifest.gates["source"] ?? "未核") · 中文：\(edition.manifest.gates["translation"] ?? "未审") · 声音：\(edition.manifest.gates["audio"] ?? "待核")")
                                .font(.footnote).foregroundStyle(.secondary)
                            Text("文件范围内 \(edition.manifest.scope.expectedBlockIds.count) 个文本块；未支持图字/公式等不会获得整本完成标签。章级音频与出版物完整性分开验收。")
                                .font(.footnote).foregroundStyle(.secondary)
                            if books.editions.filter({ $0.manifest.bookId == route.bookID }).count > 1 {
                                Menu("选择版本（旧位置与笔记保留）") {
                                    ForEach(books.editions.filter { $0.manifest.bookId == route.bookID }) { version in
                                        NavigationLink(value: BookRoute(bookID: route.bookID, revision: version.manifest.packageRevision)) {
                                            Text(String(version.manifest.packageRevision.prefix(12)))
                                        }
                                    }
                                }
                            }
                        }
                        if !edition.manifest.scope.unresolved.isEmpty {
                            Section("未覆盖/未支持 · 不冒充完整") {
                                ForEach(Array(edition.manifest.scope.unresolved.enumerated()), id: \.offset) { _, item in
                                    Text("\(item.kind) · \(item.href ?? item.sourceBlockId ?? "") · \(item.reason ?? "待核")").font(.footnote)
                                }
                            }
                        }
                        Section("章节 · 手动换章") {
                            ForEach(edition.manifest.chapters.sorted { $0.order < $1.order }) { chapter in
                                NavigationLink(value: BookRoute(bookID: route.bookID, revision: route.revision, chapterID: chapter.id)) {
                                    VStack(alignment: .leading) {
                                        Text(chapter.title)
                                        Text(chapter.audio == nil ? "译文/音频未就绪" : "有章音频 · 听感未核")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                        Section { BookNoteSection(bookID: route.bookID, revision: route.revision, chapterID: nil) }
                        let old = books.notes.notes.values.filter { $0.bookID == route.bookID && $0.packageRevision != route.revision && !$0.body.isEmpty }.sorted { $0.updatedAt > $1.updatedAt }
                        if !old.isEmpty {
                            Section("原版本听后感 · 未自动迁锚") {
                                ForEach(old) { note in Text(note.body).textSelection(.enabled) }
                            }
                        }
                    }
                    .onAppear { books.select(edition) }
                    .navigationTitle("书籍")
                }
            } else { ContentUnavailableView("此书版本待就绪", systemImage: "book.closed", description: Text("原进度与听后感保留；不把秒数移到另一音轨。")) }
        }
    }
}

struct BookChapterView: View {
    let edition: BookEdition
    let chapter: BookChapter
    let autoPlay: Bool
    @EnvironmentObject private var books: BookStore
    @EnvironmentObject private var player: PlayerModel
    @State private var requested = false
    @State private var showOriginal = false
    private var reference: BookPlaybackReference? { edition.reference(chapter) }
    private var active: Bool { reference != nil && player.currentBookReference == reference }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text(chapter.title).font(.title2)
                Text(edition.manifest.title).font(.subheadline).foregroundStyle(.secondary)
                if chapter.audio != nil {
                    HStack {
                        Button { playOrToggle() } label: { Label(active && player.isPlaying ? "暂停" : "播放本章", systemImage: active && player.isPlaying ? "pause.fill" : "play.fill") }
                            .buttonStyle(.borderedProminent).accessibilityIdentifier("book-play")
                        if active {
                            Button("−15秒") { player.skip(-15) }
                            Button("+15秒") { player.skip(15) }
                        }
                    }
                    if active {
                        Text("\(Int(player.current)) / \(Int(player.duration)) 秒").monospacedDigit()
                        Slider(value: Binding(get: { min(player.duration, max(0, player.current)) }, set: { player.seek(to: $0) }), in: 0...max(1, player.duration))
                            .accessibilityLabel("书章播放进度")
                        Picker("语速", selection: Binding(get: { player.speed }, set: { player.setSpeed($0) })) {
                            ForEach(ListenSpeed.allCases) { Text($0.label).tag($0) }
                        }.pickerStyle(.segmented)
                    }
                } else { Text("本章还没有审核后的真实音频；不会自动扣费补合成。").foregroundStyle(.secondary) }
                if active, let error = player.errorText { Text(error).foregroundStyle(.secondary) }
                if let error = books.errorText { Text(error).foregroundStyle(.secondary) }
                BookNoteSection(bookID: edition.manifest.bookId, revision: edition.manifest.packageRevision, chapterID: chapter.id)
                Divider()
                Text("完整中文译稿").font(.headline)
                Text(chapter.chineseText.isEmpty ? "尚未翻译，不以原文占位冒充中文。" : chapter.chineseText).textSelection(.enabled)
                DisclosureGroup("独立朗读稿（脚注策略可见）") { Text(chapter.readingText).textSelection(.enabled) }
                DisclosureGroup("英文原文", isExpanded: $showOriginal) { Text(chapter.sourceText).textSelection(.enabled) }
                Text("版本 \(edition.manifest.packageRevision.prefix(12)) · 没有句级时间对齐；换译稿/音轨不迁移旧秒数。").font(.caption).foregroundStyle(.secondary)
            }.padding()
        }
        .navigationTitle("章节")
        .onAppear {
            player.fullPlayerOpen = true
            if autoPlay && !requested { requested = true; start() }
        }
        .onDisappear { player.checkpoint(); player.fullPlayerOpen = false }
    }
    private func playOrToggle() { if active { player.toggle() } else { start() } }
    private func start() {
        guard let reference else { return }
        do {
            let url = try books.audioURL(edition: edition, chapter: chapter)
            player.openBook(reference: reference, title: chapter.title, bookTitle: edition.manifest.title, url: url, duration: chapter.audio?.durationSeconds ?? 0, resumeAt: books.resume(reference))
        } catch { books.errorText = error.localizedDescription }
    }
}

struct BookNoteSection: View {
    let bookID: String; let revision: String; let chapterID: String?
    @EnvironmentObject private var books: BookStore
    @State private var text = ""
    @State private var loaded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("听后感").font(.headline)
            ZStack(alignment: .topLeading) {
                if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Text("这本书里，你真正想记住的是什么？").foregroundStyle(.tertiary).padding(12).allowsHitTesting(false)
                }
                ListenNoteEditor(text: $text, revealEndToken: loaded ? 1 : 0)
                    .frame(height: 160).accessibilityIdentifier("book-listen-note")
            }.background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
            Text("仅本机 · 绑定创作时书/章/版本，不进入播客洞察门槛").font(.caption).foregroundStyle(.secondary)
        }
        .onAppear { text = books.note(bookID: bookID, revision: revision, chapterID: chapterID)?.body ?? ""; loaded = true }
        .onChange(of: text) { _, _ in save() }
        .onDisappear { save() }
    }
    private func save() { guard loaded else { return }; books.saveNote(bookID: bookID, revision: revision, chapterID: chapterID, text: text) }
}
