import XCTest

/// Read-only production observation: open the already-ingested short sample play page
/// and record official UI. Does not type, swipe-to-delete, or use synthetic fixtures.
/// Works whether that episode is still unfinished (Continue / 未听完) or finished (已听).
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

        openLibraryEpisodeForReadOnlyObservation(episodeID, in: app)
        observePlayPageControlsAndListenNote(in: app, screenshotPrefix: "t573")

        // Leave the production page as-is. No typing, no swipe-to-delete, no note clear.
        // Do not change finished or stars just to surface Continue.
    }
}
