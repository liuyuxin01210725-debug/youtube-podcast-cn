import XCTest

extension XCTestCase {
    /// Open one library episode for read-only observation.
    ///
    /// Covers the four real combinations used in production: the episode may be
    /// unfinished (visible under 未听完, and maybe on Continue) or finished
    /// (hidden by the default 未听完 filter, visible under 已听). Never types,
    /// swipes to delete, or changes finished / stars / notes.
    @MainActor
    func openLibraryEpisodeForReadOnlyObservation(_ episodeID: String, in app: XCUIApplication) {
        XCTAssertFalse(episodeID.isEmpty)
        if tapContinueCard(for: episodeID, in: app) {
            return
        }
        if tapEpisodeRow(episodeID, in: app, timeout: 2) {
            return
        }
        if openEpisodeFromFilter("已听", episodeID: episodeID, in: app) {
            return
        }
        if openEpisodeFromFilter("未听完", episodeID: episodeID, in: app) {
            return
        }
        XCTFail("Could not open episode \(episodeID) from Continue, 已听, or 未听完")
    }

    @MainActor
    func observePlayPageControlsAndListenNote(in app: XCUIApplication, screenshotPrefix: String) {
        let player = app.navigationBars["正在播放"]
        XCTAssertTrue(player.waitForExistence(timeout: 10), "Play page navigation title 正在播放 must appear")
        captureAcceptanceScreenshot("\(screenshotPrefix)-play-page-controls")

        let pause = app.buttons["暂停"]
        let resume = app.buttons["继续听"]
        let play = app.buttons["播放"]
        XCTAssertTrue(
            pause.exists || resume.exists || play.exists,
            "Play page must show a primary transport control (暂停, 继续听, or 播放)"
        )

        revealListenNote(in: app)
        let heading = app.staticTexts["听后感"]
        let editor = app.textViews["听后感"]
        let placeholder = app.staticTexts["这期对你真正想记住的是什么？"]
        XCTAssertTrue(
            heading.exists || editor.exists || placeholder.exists,
            "Play page must show 听后感 heading, editor, or the empty-state placeholder"
        )
        captureAcceptanceScreenshot("\(screenshotPrefix)-play-page-listen-note")
    }

    @MainActor
    private func tapContinueCard(for episodeID: String, in app: XCUIApplication) -> Bool {
        let continueCard = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label == %@", "继续播放 \(episodeID)"))
            .firstMatch
        let titleInContinue = app.staticTexts[episodeID]
        if continueCard.exists, titleInContinue.exists, titleInContinue.isHittable {
            titleInContinue.tap()
            return true
        }
        if continueCard.exists, continueCard.isHittable {
            // Leading half follows the NavigationLink, not the in-card play/pause button.
            continueCard.coordinate(withNormalizedOffset: CGVector(dx: 0.25, dy: 0.5)).tap()
            return true
        }
        return false
    }

    @MainActor
    private func episodeRow(_ episodeID: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)["library.episode.\(episodeID)"]
    }

    @MainActor
    private func tapEpisodeRow(_ episodeID: String, in app: XCUIApplication, timeout: TimeInterval) -> Bool {
        let row = episodeRow(episodeID, in: app)
        guard row.waitForExistence(timeout: timeout) else { return false }
        if !row.isHittable {
            app.swipeUp()
        }
        guard row.exists, row.isHittable else { return false }
        row.tap()
        return true
    }

    @MainActor
    private func openEpisodeFromFilter(_ filter: String, episodeID: String, in app: XCUIApplication) -> Bool {
        let chip = app.buttons[filter]
        guard chip.waitForExistence(timeout: 5), chip.isHittable else { return false }
        chip.tap()
        captureAcceptanceScreenshot("library-filter-\(filter)")
        for _ in 0..<8 {
            if tapEpisodeRow(episodeID, in: app, timeout: 1) {
                return true
            }
            app.swipeUp()
        }
        return false
    }

    @MainActor
    private func revealListenNote(in app: XCUIApplication) {
        let heading = app.staticTexts["听后感"]
        let editor = app.textViews["听后感"]
        let placeholder = app.staticTexts["这期对你真正想记住的是什么？"]
        if heading.exists || editor.exists || placeholder.exists { return }
        for _ in 0..<3 {
            app.swipeUp()
            if heading.exists || editor.exists || placeholder.exists { return }
        }
    }
}
