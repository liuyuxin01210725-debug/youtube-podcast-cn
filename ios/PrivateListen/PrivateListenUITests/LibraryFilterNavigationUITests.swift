import XCTest

/// Isolated-library read-only navigation: a finished fixture is hidden by 未听完
/// and must open from 已听; an unfinished fixture opens from 未听完. Requires the
/// official synthetic seed (see scripts/test_heard_filter_sim.py).
@MainActor
final class LibraryFilterNavigationUITests: XCTestCase {
    private let finishedID = "ui-search-row-01"
    private let unfinishedID = "ui-search-row-55"

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testFinishedFixtureOpensFromHeardFilter() {
        let app = XCUIApplication()
        app.launchArguments = []
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["未听完"].waitForExistence(timeout: 5))
        // 56 fixtures, one finished: the default chip should settle at 未听完 · 55.
        XCTAssertTrue(app.staticTexts["未听完 · 55"].waitForExistence(timeout: 5))
        XCTAssertFalse(
            app.descendants(matching: .any)["library.episode.\(finishedID)"].exists,
            "A finished fixture must not remain in the default 未听完 list"
        )
        captureAcceptanceScreenshot("heard-filter-before-open")
        openLibraryEpisodeForReadOnlyObservation(finishedID, in: app)
        XCTAssertTrue(app.staticTexts["Layout fixture episode 1"].waitForExistence(timeout: 10))
        observePlayPageControlsAndListenNote(in: app, screenshotPrefix: "heard-filter")
    }

    func testUnfinishedFixtureOpensFromUnfinishedFilter() {
        let app = XCUIApplication()
        app.launchArguments = []
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 10))
        captureAcceptanceScreenshot("unfinished-filter-before-open")
        openLibraryEpisodeForReadOnlyObservation(unfinishedID, in: app)
        XCTAssertTrue(app.staticTexts["Layout fixture episode 55"].waitForExistence(timeout: 10))
        observePlayPageControlsAndListenNote(in: app, screenshotPrefix: "unfinished-filter")
    }
}
