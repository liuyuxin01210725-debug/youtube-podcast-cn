import Foundation

/// Chapter TOC + resume cursor for book listen (scaffold).
/// Schema mirrors Mac `book.json` / `studio_books.chapter_index`.
/// Missing audio must stay `missing`/`queued` — never fake a finished listen.

struct BookBlockRef: Identifiable, Hashable, Codable {
    var id: String
    var index: Int
    var chars: Int
    var measure: Int
    var audioStatus: BookAudioStatus
    var audioFile: String?

    enum CodingKeys: String, CodingKey {
        case id, index, chars, measure
        case audioStatus = "audio_status"
        case audioFile = "audio_file"
    }
}

enum BookAudioStatus: String, Codable, Hashable {
    case missing
    case queued
    case running
    case ready
    case failed
}

struct BookChapterRef: Identifiable, Hashable, Codable {
    var id: String
    var title: String
    var index: Int
    var blocks: [BookBlockRef]
}

struct BookResumeCursor: Hashable, Codable {
    var chapterId: String
    var blockId: String
    var offsetChars: Int
    /// Only meaningful when the block audio is `ready` locally.
    var audioSeconds: Double?

    enum CodingKeys: String, CodingKey {
        case chapterId = "chapter_id"
        case blockId = "block_id"
        case offsetChars = "offset_chars"
        case audioSeconds = "audio_seconds"
    }
}

struct BookTOC: Hashable, Codable {
    var bookId: String
    var title: String
    var language: String
    var chapters: [BookChapterRef]
    var progress: BookResumeCursor

    enum CodingKeys: String, CodingKey {
        case bookId = "book_id"
        case title, language, chapters, progress
    }
}

enum BookChapterIndex {
    /// Flatten Mac `book.json` chapters into a TOC list. Returns nil if shape is wrong.
    static func parse(bookJSON data: Data, bookId: String) -> BookTOC? {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return parse(root: root, bookId: bookId)
    }

    static func parse(root: [String: Any], bookId: String) -> BookTOC? {
        guard bookId.count >= 1, bookId.count <= 80 else { return nil }
        let title = (root["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        let language = (root["language"] as? String) ?? "zh"
        guard language == "zh" || language == "en" else { return nil }
        guard let rawChapters = root["chapters"] as? [[String: Any]], !rawChapters.isEmpty else {
            return nil
        }
        var chapters: [BookChapterRef] = []
        for (chapterIndex, raw) in rawChapters.enumerated() {
            guard let chapterId = raw["id"] as? String, !chapterId.isEmpty else { continue }
            let chapterTitle = (raw["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let rawBlocks = raw["blocks"] as? [[String: Any]], !rawBlocks.isEmpty else { continue }
            var blocks: [BookBlockRef] = []
            for (blockIndex, block) in rawBlocks.enumerated() {
                guard let blockId = block["id"] as? String, !blockId.isEmpty else { continue }
                let chars = intValue(block["chars"]) ?? ((block["text"] as? String)?.count ?? 0)
                let measure = intValue(block["measure"]) ?? chars
                let statusRaw = block["audio_status"] as? String
                let status = BookAudioStatus(rawValue: statusRaw ?? "") ?? .missing
                let audioFile = block["audio_file"] as? String
                blocks.append(
                    BookBlockRef(
                        id: blockId,
                        index: blockIndex,
                        chars: max(0, chars),
                        measure: max(0, measure),
                        audioStatus: status,
                        audioFile: audioFile
                    )
                )
            }
            guard !blocks.isEmpty else { continue }
            let fallbackTitle = "第\(chapterIndex + 1)章"
            chapters.append(
                BookChapterRef(
                    id: chapterId,
                    title: (chapterTitle?.isEmpty == false ? chapterTitle! : fallbackTitle),
                    index: chapterIndex,
                    blocks: blocks
                )
            )
        }
        guard let first = chapters.first, let firstBlock = first.blocks.first else { return nil }
        let rawProgress = root["progress"] as? [String: Any] ?? [:]
        let requested = BookResumeCursor(
            chapterId: (rawProgress["chapter_id"] as? String) ?? first.id,
            blockId: (rawProgress["block_id"] as? String) ?? firstBlock.id,
            offsetChars: intValue(rawProgress["offset_chars"]) ?? 0,
            audioSeconds: doubleValue(rawProgress["audio_seconds"])
        )
        let clamped = clamp(requested, in: chapters)
        return BookTOC(
            bookId: bookId,
            title: (title?.isEmpty == false ? title! : "未命名"),
            language: language,
            chapters: chapters,
            progress: clamped
        )
    }

    /// Soft-clamp resume cursor into a valid chapter/block/offset. Never invent progress past known text.
    static func clamp(_ cursor: BookResumeCursor, in chapters: [BookChapterRef]) -> BookResumeCursor {
        guard let first = chapters.first, let firstBlock = first.blocks.first else {
            return BookResumeCursor(chapterId: cursor.chapterId, blockId: cursor.blockId, offsetChars: 0, audioSeconds: nil)
        }
        let chapter = chapters.first(where: { $0.id == cursor.chapterId }) ?? first
        let block = chapter.blocks.first(where: { $0.id == cursor.blockId }) ?? chapter.blocks.first ?? firstBlock
        let upper = max(0, block.chars)
        let offset = min(max(0, cursor.offsetChars), upper)
        let seconds: Double?
        if block.audioStatus == .ready, let value = cursor.audioSeconds, value.isFinite, value >= 0 {
            seconds = value
        } else {
            seconds = nil
        }
        return BookResumeCursor(
            chapterId: chapter.id,
            blockId: block.id,
            offsetChars: offset,
            audioSeconds: seconds
        )
    }

    /// Mark blocks ready when `chapters/<c>/<b>.mp3` exists locally; otherwise keep missing/queued.
    static func enrichAudioStatus(_ toc: BookTOC, folderURL: URL) -> BookTOC {
        var chapters = toc.chapters
        for cIndex in chapters.indices {
            var blocks = chapters[cIndex].blocks
            for bIndex in blocks.indices {
                let chapterId = chapters[cIndex].id
                let blockId = blocks[bIndex].id
                let url = folderURL
                    .appendingPathComponent("chapters", isDirectory: true)
                    .appendingPathComponent(chapterId, isDirectory: true)
                    .appendingPathComponent("\(blockId).mp3")
                if LocalAudio.isPlayable(url) {
                    blocks[bIndex].audioStatus = .ready
                    blocks[bIndex].audioFile = "chapters/\(chapterId)/\(blockId).mp3"
                } else if blocks[bIndex].audioStatus == .ready {
                    // Manifest said ready but file not here yet — do not fake finish.
                    blocks[bIndex].audioStatus = .missing
                    try? FileManager.default.startDownloadingUbiquitousItem(at: url)
                }
            }
            chapters[cIndex].blocks = blocks
        }
        let progress = clamp(toc.progress, in: chapters)
        return BookTOC(
            bookId: toc.bookId,
            title: toc.title,
            language: toc.language,
            chapters: chapters,
            progress: progress
        )
    }

        private static func intValue(_ any: Any?) -> Int? {
        if let value = any as? Int { return value }
        if let value = any as? Double { return Int(value) }
        if let value = any as? NSNumber { return value.intValue }
        return nil
    }

    private static func doubleValue(_ any: Any?) -> Double? {
        if let value = any as? Double { return value }
        if let value = any as? Int { return Double(value) }
        if let value = any as? NSNumber { return value.doubleValue }
        return nil
    }
}
