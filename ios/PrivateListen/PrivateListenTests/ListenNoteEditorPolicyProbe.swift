import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Runs the editor policy and production store against synthetic notes.
@main
struct ListenNoteEditorPolicyProbe {
    @MainActor
    static func main() throws {
        var failures: [String] = []
        func check(_ name: String, _ ok: Bool) {
            if ok {
                print("ok  \(name)")
            } else {
                failures.append(name)
                print("FAIL \(name)")
            }
        }

        let longNote = """
        TEST-P0 1 syntheticisolatedlisten-note body
        appendreproduction
        TEST-R4-END
        TEST-PM-1
        """

        check(
            "有正文的听后感加载后应露出文末",
            ListenNoteEditorPolicy.shouldRevealEndOnLoad(text: longNote)
        )
        check(
            "空听后感加载不必跳文末",
            ListenNoteEditorPolicy.shouldRevealEndOnLoad(text: "  \n") == false
        )
        check(
            "重开后第一次聚焦应把光标放到文末",
            ListenNoteEditorPolicy.shouldMoveCaretToEndOnFocus(
                text: longNote,
                isFirstFocusAfterLoad: true
            )
        )
        check(
            "用户已聚焦编辑过后再点进中间，不要强行拖回文末",
            ListenNoteEditorPolicy.shouldMoveCaretToEndOnFocus(
                text: longNote,
                isFirstFocusAfterLoad: false
            ) == false
        )
        check(
            "空笔记第一次聚焦不必跳文末",
            ListenNoteEditorPolicy.shouldMoveCaretToEndOnFocus(
                text: "",
                isFirstFocusAfterLoad: true
            ) == false
        )

        let files = FileManager.default
        let root = files.temporaryDirectory.appendingPathComponent("ListenNoteProbe.\(UUID().uuidString)")
        let directory = root.appendingPathComponent("documents")
        try files.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? files.removeItem(at: root) }
        let writer = ListenNoteStore(directory: directory)
        let firstID = "play-note-synthetic-a"
        let otherID = "play-note-synthetic-b"
        let headline = "原有标题"
        let body = String(repeating: "隔离集原文 🎧\n", count: 30) + longNote
        let tags = ["原有标签"]
        check("写入合成的旧版标题、正文及标签",
              writer.upsert(episodeId: firstID, headline: headline, body: body, tags: tags))
        check("另一集合成笔记单独保存", writer.saveDraft(episodeId: otherID, text: "另一集旧笔记", tags: []))
        let fileURL = directory.appendingPathComponent(ListenNoteStore.fileName)
        let original = try Data(contentsOf: fileURL)
        let reopened = ListenNoteStore(directory: directory)
        let display = ListenNoteDownstream.mergedDisplayText(headline: headline, body: body)
        check("重开读回原来的全文与标签",
              reopened.note(for: firstID)?.headline == headline
              && reopened.note(for: firstID)?.body == body
              && reopened.note(for: firstID)?.tags == tags)
        check("未编辑的退出保存成功", reopened.saveDraft(episodeId: firstID, text: display, tags: tags))
        let afterUnchangedClose = try Data(contentsOf: fileURL)
        check("未编辑的退出保留原文件与时间戳", afterUnchangedClose == original)
        let appended = display + "\n新添尾句 🌱"
        check("文末追加写盘成功", reopened.saveDraft(episodeId: firstID, text: appended, tags: tags))
        let cold = ListenNoteStore(directory: directory)
        check("再开仍有原文、追加内容、标签及另一集",
              cold.note(for: firstID)?.body == appended && cold.note(for: firstID)?.tags == tags
              && cold.note(for: otherID)?.body == "另一集旧笔记")

        let movedDirectory = root.appendingPathComponent("previous-documents")
        try files.moveItem(at: directory, to: movedDirectory)
        check("写盘失败返回失败，内存与原文件仍保留上次成功保存的笔记",
              !cold.saveDraft(episodeId: firstID, text: "未保存", tags: [])
              && cold.note(for: firstID)?.body == appended
              && ListenNoteStore(directory: movedDirectory).note(for: firstID)?.body == appended)

        if !failures.isEmpty {
            fputs("\(failures.count) failed: \(failures.joined(separator: ", "))\n", stderr)
            exit(1)
        }
        print("listen note editor and persistence probe passed")
    }
}
