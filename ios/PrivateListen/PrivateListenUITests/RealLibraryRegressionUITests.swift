import Vision
import XCTest

/// Real-library regressions for a local sanitized gen2 copy.
/// IDs and the search needle come from the generated, gitignored RealLibraryIDs.swift.
@MainActor
final class RealLibraryRegressionUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testSearchFieldFocusOnRealLibrary() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        captureAcceptanceScreenshot("real-lib-before-search")
        let field = searchField(in: app)
        assertDisjoint(field, fromEpisodeRowsIn: app)
        field.tap()
        assertCanTypeAndFilter(in: app)

        app.terminate()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        let searchButton = app.buttons["library.search"]
        XCTAssertTrue(searchButton.waitForExistence(timeout: 5))
        XCTAssertTrue(searchButton.isHittable)
        assertDisjoint(searchButton, fromEpisodeRowsIn: app)
        searchButton.tap()
        assertCanTypeAndFilter(in: app)
    }

    func testLongListenNoteAppendsScrollsAndPersists() throws {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        openLibraryEpisodeForReadOnlyObservation(RealLibraryIDs.longNoteEpisodeID, in: app)
        let editor = revealAndRequireListenNoteEditor(in: app)
        captureAcceptanceScreenshot("real-lib-listen-note-opened")
        let original = try XCTUnwrap(editor.value as? String)
        XCTAssertFalse(original.isEmpty, "Expected a pre-existing sanitized listen note")

        editor.tap()
        let keyboard = app.keyboards.firstMatch
        XCTAssertTrue(keyboard.waitForExistence(timeout: 5), "Listen note did not take focus")
        let aboveKeyboard = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            guard editor.isHittable, keyboard.exists else { return false }
            return !editor.frame.isEmpty && !keyboard.frame.isEmpty && editor.frame.maxY <= keyboard.frame.minY + 1
        }, object: nil)
        let visibility = XCTWaiter.wait(for: [aboveKeyboard], timeout: 5)
        XCTAssertEqual(visibility, .completed, "The listen note editor must sit above the software keyboard")

        editor.typeText("\nSIM-REG-APPEND")
        let expected = original + "\nSIM-REG-APPEND"
        XCTAssertEqual(editor.value as? String, expected)
        captureAcceptanceScreenshot("real-lib-listen-note-appended")

        let scrollStart = editor.coordinate(withNormalizedOffset: CGVector(dx: 0.55, dy: 0.20))
        let scrollEnd = editor.coordinate(withNormalizedOffset: CGVector(dx: 0.55, dy: 0.82))
        scrollStart.press(forDuration: 0.05, thenDragTo: scrollEnd, withVelocity: .slow, thenHoldForDuration: 0)
        captureAcceptanceScreenshot("real-lib-listen-note-after-inner-scroll")
        let scrolled = try visibleText(in: editor)
        XCTAssertFalse(scrolled.contains("SIM-REG-APPEND"), "The appended line should scroll out of view")
        XCTAssertEqual(editor.value as? String, expected, "Inner scroll must not change the note")

        app.navigationBars["正在播放"].buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 5))
        app.terminate()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        openLibraryEpisodeForReadOnlyObservation(RealLibraryIDs.longNoteEpisodeID, in: app)
        let reopened = revealAndRequireListenNoteEditor(in: app)
        captureAcceptanceScreenshot("real-lib-listen-note-reopened")
        XCTAssertEqual(reopened.value as? String, expected, "A fresh app process must load the saved note")
    }

    func testShortPlaybackReturnsJustPlayedToContinue() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        XCTAssertFalse(RealLibraryIDs.unfinishedEpisodeID.isEmpty, "Need an unfinished real-library episode")
        openUnfinishedEpisodeFromContinueOrShelf(in: app)
        let player = app.navigationBars["正在播放"]
        XCTAssertTrue(player.waitForExistence(timeout: 10))
        startPlaybackIfNeeded(in: app)
        XCTAssertTrue(app.buttons["暂停"].waitForExistence(timeout: 8), "Playback must actually start")
        // Resume may already be mid-episode (legacy bare-id cursor). Play 3–5s of real audio.
        RunLoop.current.run(until: Date().addingTimeInterval(4))
        XCTAssertTrue(app.buttons["暂停"].exists, "Short play must keep playing for a few seconds")
        player.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 5))
        let current = continueCard(RealLibraryIDs.unfinishedTitle, in: app)
        XCTAssertTrue(current.waitForExistence(timeout: 8), "Continue must show the just-played episode")
        XCTAssertTrue(current.isHittable)
        captureAcceptanceScreenshot("real-lib-continue-after-short-play")

        app.terminate()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        let restored = continueCard(RealLibraryIDs.unfinishedTitle, in: app)
        XCTAssertTrue(restored.waitForExistence(timeout: 8), "Continue must still show the just-played episode after relaunch")
        XCTAssertTrue(restored.isHittable)
        captureAcceptanceScreenshot("real-lib-continue-after-short-play-relaunch")
    }

    func testSwipingContinueCardKeepsUnfinishedProgress() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        var card = continueCard(RealLibraryIDs.unfinishedTitle, in: app)
        if !card.waitForExistence(timeout: 5) {
            // Establish the card only if this test runs alone. Do not open the episode again after the swipe.
            openUnfinishedEpisodeFromContinueOrShelf(in: app)
            let player = app.navigationBars["正在播放"]
            XCTAssertTrue(player.waitForExistence(timeout: 10))
            startPlaybackIfNeeded(in: app)
            _ = app.buttons["暂停"].waitForExistence(timeout: 8)
            RunLoop.current.run(until: Date().addingTimeInterval(4))
            player.buttons.element(boundBy: 0).tap()
            XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 5))
            card = continueCard(RealLibraryIDs.unfinishedTitle, in: app)
        }
        XCTAssertTrue(card.waitForExistence(timeout: 8), "Continue card should show the unfinished episode")
        let starredBefore = episodeRow(RealLibraryIDs.unfinishedEpisodeID, in: app).images["star.fill"].exists
        captureAcceptanceScreenshot("real-lib-continue-before-swipe")
        let start = card.coordinate(withNormalizedOffset: CGVector(dx: 0.86, dy: 0.5))
        let end = card.coordinate(withNormalizedOffset: CGVector(dx: 0.40, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0)
        let delete = app.buttons["删除"]
        XCTAssertTrue(delete.waitForExistence(timeout: 5))
        XCTAssertTrue(delete.isHittable)
        captureAcceptanceScreenshot("real-lib-continue-delete-revealed")
        delete.tap()
        XCTAssertFalse(card.waitForExistence(timeout: 3), "The swiped card should leave the Continue slot")
        captureAcceptanceScreenshot("real-lib-continue-after-swipe")

        // Do not open this episode again. Opening would acknowledgeContinue and legally restore the card.
        XCTAssertTrue(unfinishedRowVisible(in: app), "Swipe must not hide or finish the unfinished episode")
        XCTAssertEqual(episodeRow(RealLibraryIDs.unfinishedEpisodeID, in: app).images["star.fill"].exists, starredBefore, "Swipe must not change the star")

        app.terminate()
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        XCTAssertFalse(continueCard(RealLibraryIDs.unfinishedTitle, in: app).waitForExistence(timeout: 3), "The dismissed card must stay off Continue after relaunch")
        XCTAssertTrue(unfinishedRowVisible(in: app), "Unfinished progress must survive relaunch without reopening the episode")
        XCTAssertEqual(episodeRow(RealLibraryIDs.unfinishedEpisodeID, in: app).images["star.fill"].exists, starredBefore, "Star must survive relaunch")
        captureAcceptanceScreenshot("real-lib-unfinished-after-swipe-relaunch")
    }

    /// Real-library Continue is labeled with the episode title, not the bare id.
    /// Prefer that card (the compat fix should already be showing it). Never hunt
    /// through 已听 first; after scrolling, swipe down so 未听完 stays hittable.
    private func openUnfinishedEpisodeFromContinueOrShelf(in app: XCUIApplication) {
        let card = continueCard(RealLibraryIDs.unfinishedTitle, in: app)
        if card.waitForExistence(timeout: 5) {
            if !card.isHittable {
                app.swipeDown()
            }
            card.coordinate(withNormalizedOffset: CGVector(dx: 0.25, dy: 0.5)).tap()
            XCTAssertTrue(app.navigationBars["正在播放"].waitForExistence(timeout: 10), "Continue card should open the unfinished episode")
            return
        }
        revealFilterChips(in: app)
        XCTAssertTrue(
            openEpisodeFromFilter("未听完", episodeID: RealLibraryIDs.unfinishedEpisodeID, in: app),
            "Unfinished episode should be visible under 未听完"
        )
    }

    private func revealFilterChips(in app: XCUIApplication) {
        for _ in 0..<4 {
            if app.buttons["未听完"].exists, app.buttons["已听"].exists { return }
            app.swipeDown()
        }
    }

    private func startPlaybackIfNeeded(in app: XCUIApplication) {
        if app.buttons["暂停"].waitForExistence(timeout: 2) { return }
        let play = app.buttons["播放"]
        let resume = app.buttons["继续听"]
        if play.exists, play.isHittable { play.tap() }
        else if resume.exists, resume.isHittable { resume.tap() }
    }

    /// Confirm the unfinished episode still sits under 未听完 without opening it.
    private func unfinishedRowVisible(in app: XCUIApplication) -> Bool {
        revealFilterChips(in: app)
        let chip = app.buttons["未听完"]
        guard chip.waitForExistence(timeout: 5), chip.isHittable else { return false }
        chip.tap()
        let row = episodeRow(RealLibraryIDs.unfinishedEpisodeID, in: app)
        for _ in 0..<8 {
            if row.waitForExistence(timeout: 1) { return true }
            app.swipeUp()
        }
        return row.exists
    }

    private func searchField(in app: XCUIApplication) -> XCUIElement {
        let field = app.searchFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertTrue(field.isHittable)
        return field
    }

    private func assertDisjoint(_ control: XCUIElement, fromEpisodeRowsIn app: XCUIApplication) {
        XCTAssertTrue(control.isHittable)
        let rows = app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier BEGINSWITH %@", "library.episode."))
            .allElementsBoundByIndex.filter(\.isHittable)
        XCTAssertFalse(rows.isEmpty)
        for row in rows {
            XCTAssertFalse(control.frame.intersects(row.frame), "\(control.frame) overlaps \(row.identifier) \(row.frame)")
        }
    }

    private func assertCanTypeAndFilter(in app: XCUIApplication) {
        let field = searchField(in: app)
        field.typeText(RealLibraryIDs.searchNeedle)
        XCTAssertEqual(field.value as? String, RealLibraryIDs.searchNeedle)
        XCTAssertTrue(app.staticTexts["搜索结果 · 1"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.descendants(matching: .any)["library.episode.\(RealLibraryIDs.searchEpisodeID)"].exists)
        XCTAssertFalse(app.navigationBars["正在播放"].exists)
        assertDisjoint(field, fromEpisodeRowsIn: app)
        captureAcceptanceScreenshot("real-lib-search-filtered")
    }

    private func revealAndRequireListenNoteEditor(in app: XCUIApplication) -> XCUIElement {
        XCTAssertTrue(app.navigationBars["正在播放"].waitForExistence(timeout: 10))
        revealListenNote(in: app)
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

    private func waitForPlaybackSeconds(in app: XCUIApplication, timeout: TimeInterval) -> Bool {
        let labels = ["0:03", "0:04", "0:05", "0:06"]
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if labels.contains(where: { app.staticTexts[$0].exists }) {
                return true
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        return labels.contains(where: { app.staticTexts[$0].exists })
    }

    private func visibleText(in editor: XCUIElement) throws -> String {
        let screenshot = try XCTUnwrap(editor.screenshot().image.cgImage)
        let request = VNRecognizeTextRequest()
        try VNImageRequestHandler(cgImage: screenshot).perform([request])
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
    }
}
