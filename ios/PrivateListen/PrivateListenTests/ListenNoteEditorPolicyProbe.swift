import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Locks long listen-note reopen / first-focus append policy.
/// `swiftc -parse-as-library` ListenNoteEditorPolicy.swift and this file.
@main
struct ListenNoteEditorPolicyProbe {
    static func main() {
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

        if !failures.isEmpty {
            fputs("\(failures.count) failed: \(failures.joined(separator: ", "))\n", stderr)
            exit(1)
        }
        print("listen note editor policy probe passed")
    }
}
