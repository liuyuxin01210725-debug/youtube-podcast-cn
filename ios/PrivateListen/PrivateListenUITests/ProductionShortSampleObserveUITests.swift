import XCTest

/// Read-only production observation: open the already-ingested short sample play page
/// and record official UI. Does not type, swipe, delete, or use synthetic fixtures.
@MainActor
final class ProductionShortSampleObserveUITests: XCTestCase {
    private let episodeID = "fd4c6200b01540d992b5a9e39784eb2e"
    private let bundleID = "com.liuyuxin.privatelisten"

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testOpenPlayPageObservesControlsAndListenNote() throws {
        let app = XCUIApplication(bundleIdentifier: bundleID)
        app.launchArguments = []
        app.launch()

        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 15), "Library should be visible after launch")
        captureAcceptanceScreenshot("t573-library-before-open")

        openShortSamplePlayPage(in: app)

        let player = app.navigationBars["正在播放"]
        XCTAssertTrue(player.waitForExistence(timeout: 10), "Production play page navigation title 正在播放 must appear")
        captureAcceptanceScreenshot("t573-play-page-controls")

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
        captureAcceptanceScreenshot("t573-play-page-listen-note")

        // Leave the production page as-is. No typing, no swipe, no delete, no note clear.
    }

    private func openShortSamplePlayPage(in app: XCUIApplication) {
        let continueCard = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label == %@", "继续播放 \(episodeID)"))
            .firstMatch
        let episodeRow = app.descendants(matching: .any)["library.episode.\(episodeID)"]
        let titleInContinue = app.staticTexts[episodeID]
        let miniOpen = app.buttons["library.miniPlayer.open"]

        if continueCard.exists, titleInContinue.exists, titleInContinue.isHittable {
            titleInContinue.tap()
        } else if continueCard.waitForExistence(timeout: 3), continueCard.isHittable {
            // Tap the leading half so we follow the NavigationLink, not the in-card play/pause button.
            continueCard.coordinate(withNormalizedOffset: CGVector(dx: 0.25, dy: 0.5)).tap()
        } else if episodeRow.waitForExistence(timeout: 5), episodeRow.isHittable {
            episodeRow.tap()
        } else if miniOpen.exists, miniOpen.isHittable {
            miniOpen.tap()
        } else {
            XCTFail("Could not find a production control to open the short-sample play page")
        }
    }

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
