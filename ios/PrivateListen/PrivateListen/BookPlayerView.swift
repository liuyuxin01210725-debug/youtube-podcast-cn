import SwiftUI

/// True Book type player: chapter TOC + resume cursor. Not one fake podcast per chapter.
struct BookPlayerView: View {
    let episode: Episode
    @EnvironmentObject private var player: PlayerModel
    @EnvironmentObject private var library: LibraryStore
    @EnvironmentObject private var bookResume: BookResumeStore
    @State private var toc: BookTOC?
    @State private var statusMessage: String?

    var body: some View {
        Group {
            if let toc {
                list(toc)
            } else if episode.downloading {
                ContentUnavailableView(
                    "书目录还在下载",
                    systemImage: "icloud.and.arrow.down",
                    description: Text("book.json 尚未就绪。下拉或稍后再进。")
                )
            } else {
                ContentUnavailableView(
                    "打不开这本书",
                    systemImage: "book.closed",
                    description: Text(statusMessage ?? "缺少 book.json，或章目录格式不对。")
                )
            }
        }
        .navigationTitle(episode.title)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { reloadAndResume() }
        .refreshable { reloadAndResume() }
    }

    @ViewBuilder
    private func list(_ toc: BookTOC) -> some View {
        let cursor = bookResume.resolve(toc: toc)
        List {
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    Text(toc.language == "en" ? "英文书 · 译+朗读（试点）" : "书")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Text(resumeLabel(cursor, toc: toc))
                        .font(.subheadline)
                    if let statusMessage, !statusMessage.isEmpty {
                        Text(statusMessage)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    if player.currentID?.hasPrefix(bookBlockPrefix) == true {
                        HStack {
                            Text(player.isPlaying ? "正在朗读" : "已暂停")
                                .font(.footnote)
                            Spacer()
                            Text(listenClock(player.current))
                                .font(.footnote.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.vertical, 4)
            }
            ForEach(toc.chapters) { chapter in
                Section(chapter.title) {
                    ForEach(chapter.blocks) { block in
                        Button {
                            play(block: block, chapter: chapter, toc: toc)
                        } label: {
                            HStack(alignment: .firstTextBaseline) {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(blockRowTitle(block, cursor: cursor))
                                        .foregroundStyle(.primary)
                                    Text(blockStatusLabel(block))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                if cursor.blockId == block.id {
                                    Image(systemName: "bookmark.fill")
                                        .foregroundStyle(Color.accentColor)
                                        .accessibilityLabel("续听位置")
                                }
                            }
                            .padding(.vertical, 2)
                        }
                        .disabled(block.audioStatus != .ready)
                    }
                }
            }
        }
    }

    private var bookBlockPrefix: String { "book:\(episode.id)/" }

    private func reloadAndResume() {
        library.reload()
        guard let loaded = library.loadBookTOC(for: episode) else {
            toc = nil
            statusMessage = "还没有 book.json。"
            return
        }
        toc = loaded
        let cursor = bookResume.resolve(toc: loaded)
        bookResume.remember(bookId: episode.id, cursor: cursor, chapters: loaded.chapters)
        statusMessage = nil
        autoOpen(cursor: cursor, toc: loaded)
    }

    private func autoOpen(cursor: BookResumeCursor, toc: BookTOC) {
        guard let chapter = toc.chapters.first(where: { $0.id == cursor.chapterId }),
              let block = chapter.blocks.first(where: { $0.id == cursor.blockId }) else {
            return
        }
        guard block.audioStatus == .ready else {
            statusMessage = "续听块还没合成音频（\(block.id)）。已停在目录，不假装已听完。"
            return
        }
        play(block: block, chapter: chapter, toc: toc, resumeSeconds: cursor.audioSeconds ?? 0, force: false)
    }

    private func play(
        block: BookBlockRef,
        chapter: BookChapterRef,
        toc: BookTOC,
        resumeSeconds: Double = 0,
        force: Bool = true
    ) {
        guard block.audioStatus == .ready else {
            statusMessage = "这一块还是「\(block.audioStatus.rawValue)」——没有现成 mp3，不能开播。"
            return
        }
        let url = episode.blockAudioURL(chapterId: chapter.id, blockId: block.id)
        guard LocalAudio.isPlayable(url) else {
            try? FileManager.default.startDownloadingUbiquitousItem(at: url)
            statusMessage = "块音频还在下载：chapters/\(chapter.id)/\(block.id).mp3"
            return
        }
        let cursor = BookResumeCursor(
            chapterId: chapter.id,
            blockId: block.id,
            offsetChars: 0,
            audioSeconds: resumeSeconds > 0 ? resumeSeconds : nil
        )
        bookResume.remember(bookId: episode.id, cursor: cursor, chapters: toc.chapters)
        bookResume.mirrorToDocumentsLibrary(bookId: episode.id, cursor: cursor)
        let prefix = bookBlockPrefix
        let bookId = episode.id
        let chapterId = chapter.id
        let blockId = block.id
        let chapters = toc.chapters
        player.onProgress = { id, seconds, duration in
            Task { @MainActor in
                guard id.hasPrefix(prefix) else { return }
                let updated = BookResumeCursor(
                    chapterId: chapterId,
                    blockId: blockId,
                    offsetChars: 0,
                    audioSeconds: seconds
                )
                bookResume.remember(bookId: bookId, cursor: updated, chapters: chapters)
                // Coarse mark so library「未听」在开听后清除；细粒度续听仍在 BookResumeStore。
                library.remember(id: bookId, seconds: max(5, seconds), duration: max(duration, 1))
            }
        }
        let blockEpisode = Episode(
            id: "\(bookBlockPrefix)\(block.id)",
            title: "\(toc.title) · \(chapter.title)",
            createdAt: episode.createdAt,
            durationSec: nil,
            audioURL: url,
            downloading: false,
            artworkFile: episode.artworkFile,
            artworkKind: episode.artworkKind,
            contentType: .book,
            folderURL: episode.folderURL
        )
        player.open(blockEpisode, resumeAt: resumeSeconds, force: force)
        statusMessage = nil
    }

    private func resumeLabel(_ cursor: BookResumeCursor, toc: BookTOC) -> String {
        let chapterTitle = toc.chapters.first(where: { $0.id == cursor.chapterId })?.title ?? cursor.chapterId
        if let seconds = cursor.audioSeconds, seconds > 0 {
            return "续听：\(chapterTitle) · \(cursor.blockId) · \(listenClock(seconds))"
        }
        return "续听：\(chapterTitle) · \(cursor.blockId) · 字偏移 \(cursor.offsetChars)"
    }

    private func blockRowTitle(_ block: BookBlockRef, cursor: BookResumeCursor) -> String {
        let base = "块 \(block.index + 1)"
        if cursor.blockId == block.id {
            return "\(base) · 上次听到这"
        }
        return base
    }

    private func blockStatusLabel(_ block: BookBlockRef) -> String {
        switch block.audioStatus {
        case .ready:
            return block.audioFile ?? "chapters/…/\(block.id).mp3"
        case .queued:
            return "排队合成中"
        case .running:
            return "正在合成"
        case .failed:
            return "合成失败"
        case .missing:
            return "未合成"
        }
    }
}
