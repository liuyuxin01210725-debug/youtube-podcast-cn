import SwiftUI
import UIKit

/// Multline listen-note field that scrolls inside the player `ScrollView` and
/// opens/focuses at the end so append / dictation does not land mid-note.
struct ListenNoteEditor: UIViewRepresentable {
    @Binding var text: String
    /// Bumped when the episode (or a freshly loaded body) should reveal the end.
    var revealEndToken: Int
    var onEditingChanged: (Bool) -> Void = { _ in }

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeUIView(context: Context) -> UITextView {
        let view = ScrollFriendlyTextView()
        view.delegate = context.coordinator
        view.backgroundColor = .clear
        view.textColor = .label
        view.tintColor = .label
        view.font = UIFont.preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.isScrollEnabled = true
        view.alwaysBounceVertical = true
        view.showsVerticalScrollIndicator = true
        view.keyboardDismissMode = .interactive
        view.textContainerInset = UIEdgeInsets(top: 8, left: 4, bottom: 8, right: 4)
        view.textContainer.lineFragmentPadding = 4
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.accessibilityLabel = "听后感"
        view.text = text
        return view
    }

    func updateUIView(_ uiView: UITextView, context: Context) {
        context.coordinator.parent = self
        let coordinator = context.coordinator

        if coordinator.appliedRevealToken != revealEndToken {
            coordinator.appliedRevealToken = revealEndToken
            coordinator.placeCaretAtEndOnNextFocus = true
            if uiView.text != text {
                uiView.text = text
            }
            coordinator.moveCaretToEnd(uiView, scroll: true)
            return
        }

        if uiView.text != text {
            let wasFirstResponder = uiView.isFirstResponder
            let selected = uiView.selectedRange
            uiView.text = text
            if wasFirstResponder {
                let maxLocation = (uiView.text as NSString).length
                let location = min(selected.location, maxLocation)
                uiView.selectedRange = NSRange(location: location, length: 0)
            }
        }
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: ListenNoteEditor
        var appliedRevealToken = -1
        var placeCaretAtEndOnNextFocus = true

        init(_ parent: ListenNoteEditor) {
            self.parent = parent
        }

        func textViewDidChange(_ textView: UITextView) {
            parent.text = textView.text ?? ""
        }

        func textViewDidBeginEditing(_ textView: UITextView) {
            parent.onEditingChanged(true)
            // First focus after load/reopen: caret + viewport at the true end so
            // typing / system dictation appends instead of inserting in the middle.
            if placeCaretAtEndOnNextFocus {
                placeCaretAtEndOnNextFocus = false
                moveCaretToEnd(textView, scroll: true)
            }
        }

        func textViewDidEndEditing(_ textView: UITextView) {
            parent.onEditingChanged(false)
        }

        func moveCaretToEnd(_ textView: UITextView, scroll: Bool) {
            let end = (textView.text as NSString).length
            let range = NSRange(location: end, length: 0)
            // layoutIfNeeded so scrollRangeToVisible works on the first reveal.
            textView.layoutIfNeeded()
            textView.selectedRange = range
            if scroll {
                textView.scrollRangeToVisible(range)
            }
        }
    }
}

/// UITextView that keeps vertical pans when embedded in a SwiftUI `ScrollView`.
private final class ScrollFriendlyTextView: UITextView, UIGestureRecognizerDelegate {
    override var intrinsicContentSize: CGSize {
        CGSize(width: UIView.noIntrinsicMetric, height: UIView.noIntrinsicMetric)
    }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        panGestureRecognizer.delegate = self
    }

    func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer,
        shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
    ) -> Bool {
        false
    }

    override func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
        guard gestureRecognizer === panGestureRecognizer else {
            return super.gestureRecognizerShouldBegin(gestureRecognizer)
        }
        // Claim vertical drags when this field can scroll, so the outer player
        // ScrollView does not swallow them (mirroring cannot prove a dead zone;
        // this still matches the long-note clip/overflow report).
        if contentSize.height > bounds.height + 1 {
            return true
        }
        return super.gestureRecognizerShouldBegin(gestureRecognizer)
    }
}
