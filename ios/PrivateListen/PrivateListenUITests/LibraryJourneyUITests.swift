import Vision
import XCTest

@MainActor
final class LibraryJourneyUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testLongListenNoteAppendsScrollsAndPersists() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-OpenSyntheticEpisode"]
        app.launch()
        let editor = pausedListenNote(in: app)
        captureAcceptanceScreenshot("listen-note-paused-player")
        let original = try XCTUnwrap(editor.value as? String)
        XCTAssertTrue(original.hasPrefix("Original listening note\n"), "Expected the seeded legacy headline")
        XCTAssertTrue(original.hasSuffix("Final note marker"), "Expected the seeded long body")

        editor.tap()
        let keyboard = app.keyboards.firstMatch
        XCTAssertTrue(keyboard.waitForExistence(timeout: 5), "Listen note did not take focus")
        let aboveKeyboard = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            guard editor.isHittable, keyboard.exists else { return false }
            let editorFrame = editor.frame
            let keyboardFrame = keyboard.frame
            return !editorFrame.isEmpty && !keyboardFrame.isEmpty && editorFrame.maxY <= keyboardFrame.minY
        }, object: nil)
        let visibility = XCTWaiter.wait(for: [aboveKeyboard], timeout: 3)
        captureAcceptanceScreenshot("listen-note-focused-at-end")
        XCTAssertEqual(visibility, .completed, "The listen note editor must be fully above the software keyboard")
        XCTAssertTrue(try visibleText(in: editor).contains("Final note marker"), "The note tail must be visible on focus")

        let appended = "\nAdded by acceptance"
        editor.typeText(appended)
        let expected = original + appended
        XCTAssertEqual(editor.value as? String, expected, "Typing should append at the end")
        captureAcceptanceScreenshot("listen-note-appended-before-scroll")
        XCTAssertTrue(try visibleText(in: editor).contains("Added by acceptance"))

        let scrollStart = editor.coordinate(withNormalizedOffset: CGVector(dx: 0.55, dy: 0.20))
        let scrollEnd = editor.coordinate(withNormalizedOffset: CGVector(dx: 0.55, dy: 0.82))
        scrollStart.press(forDuration: 0.05, thenDragTo: scrollEnd, withVelocity: .slow, thenHoldForDuration: 0)
        captureAcceptanceScreenshot("listen-note-after-inner-scroll")
        let scrolled = try visibleText(in: editor)
        XCTAssertTrue(scrolled.contains("Earlier paragraph"), "The drag should show earlier lines inside the editor")
        XCTAssertFalse(scrolled.contains("Added by acceptance"), "The appended line should scroll out of view")
        XCTAssertEqual(editor.value as? String, expected)

        app.navigationBars["正在播放"].buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 5))
        app.terminate()
        app.launch()
        let reopened = pausedListenNote(in: app)
        captureAcceptanceScreenshot("listen-note-reopened")
        XCTAssertEqual(reopened.value as? String, expected, "A fresh app process must load the saved note")
    }

    func testSwipingContinueCardPromotesNextUnfinishedEpisode() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 5))
        let recent = continueCard("Layout fixture episode 55", in: app)
        let next = continueCard("Layout fixture episode 54", in: app)
        XCTAssertTrue(recent.waitForExistence(timeout: 5), "Expected the newer unfinished podcast fixture")
        XCTAssertTrue(recent.isHittable)
        XCTAssertFalse(next.exists)
        captureAcceptanceScreenshot("continue-card-before-swipe")

        let start = recent.coordinate(withNormalizedOffset: CGVector(dx: 0.86, dy: 0.5))
        let end = recent.coordinate(withNormalizedOffset: CGVector(dx: 0.40, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0)
        let delete = app.buttons["删除"]
        XCTAssertTrue(delete.waitForExistence(timeout: 5))
        XCTAssertTrue(delete.isHittable)
        captureAcceptanceScreenshot("continue-card-delete-revealed")
        delete.tap()

        XCTAssertTrue(next.waitForExistence(timeout: 5), "The other unfinished podcast should take the slot")
        XCTAssertTrue(next.isHittable)
        XCTAssertFalse(recent.exists)
        captureAcceptanceScreenshot("continue-card-next-after-dismissal")

        app.terminate()
        app.launch()
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        XCTAssertFalse(recent.exists, "The newest card must stay dismissed after relaunch")
    }

    func testShortPlaybackReturnsTheJustPlayedEpisodeToContinue() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 5))
        let cards = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label BEGINSWITH %@", "继续播放 "))
        XCTAssertTrue(cards.firstMatch.waitForExistence(timeout: 5))
        XCTAssertEqual(cards.count, 1)
        XCTAssertTrue(cards.firstMatch.label.hasPrefix("继续播放 Layout fixture episode "))

        let episode = app.descendants(matching: .any)["library.episode.ui-search-row-56"]
        XCTAssertTrue(episode.waitForExistence(timeout: 5))
        XCTAssertTrue(episode.isHittable)
        episode.tap()
        let player = app.navigationBars["正在播放"]
        XCTAssertTrue(player.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["LibrarySearchNeedle"].exists)
        XCTAssertTrue(app.buttons["暂停"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["0:03"].waitForExistence(timeout: 5), "Real playback must reach three seconds")
        player.buttons.element(boundBy: 0).tap()

        let current = continueCard("LibrarySearchNeedle", in: app)
        XCTAssertTrue(current.waitForExistence(timeout: 5))
        XCTAssertTrue(current.isHittable)
        let shortPosition = current.descendants(matching: .any).matching(NSPredicate(
            format: "label CONTAINS %@ OR label CONTAINS %@", "剩余 0:55", "剩余 0:56"
        )).firstMatch
        XCTAssertTrue(shortPosition.exists, "A 60-second fixture should have 55–56 seconds remaining after the short play")
        XCTAssertEqual(cards.count, 1, "The just-played episode must replace the older card")
        captureAcceptanceScreenshot("continue-card-after-short-play")
    }

    private func pausedListenNote(in app: XCUIApplication) -> XCUIElement {
        XCTAssertTrue(app.navigationBars["正在播放"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["TEST-ISOLATE UI TEST"].exists)
        XCTAssertTrue(app.buttons["播放"].isEnabled)
        XCTAssertFalse(app.buttons["暂停"].exists)
        let editor = app.textViews["听后感"]
        if !editor.isHittable {
            app.scrollViews.firstMatch.swipeUp()
        }
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        XCTAssertTrue(editor.isHittable)
        return editor
    }

    private func continueCard(_ title: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)
            .matching(NSPredicate(format: "label == %@", "继续播放 \(title)"))
            .firstMatch
    }

    private func visibleText(in editor: XCUIElement) throws -> String {
        // UITextView's accessibility value exposes the whole note, regardless of scroll position.
        let screenshot = try XCTUnwrap(editor.screenshot().image.cgImage)
        let request = VNRecognizeTextRequest()
        try VNImageRequestHandler(cgImage: screenshot).perform([request])
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
    }
}

extension XCTestCase {
    @MainActor
    func captureAcceptanceScreenshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
