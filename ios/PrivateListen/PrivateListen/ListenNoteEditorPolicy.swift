import Foundation

/// Pure helpers for listen-note caret / reveal policy (probe-friendly, no UIKit).
enum ListenNoteEditorPolicy {
    /// Reopen and first focus should append at the end for a non-empty note.
    static func shouldMoveCaretToEndOnFocus(text: String, isFirstFocusAfterLoad: Bool) -> Bool {
        guard isFirstFocusAfterLoad else { return false }
        return hasContent(text)
    }

    /// Viewport should reveal the end when a stored body is loaded into the editor.
    static func shouldRevealEndOnLoad(text: String) -> Bool {
        hasContent(text)
    }

    private static func hasContent(_ text: String) -> Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}
