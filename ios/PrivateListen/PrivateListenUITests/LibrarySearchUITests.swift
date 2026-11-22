import XCTest

@MainActor
final class LibrarySearchUITests: XCTestCase {
    private let syntheticTitle = "TEST-ISOLATE UI TEST"
    private let needle = "LibrarySearchNeedle"
    private let needleID = "library.episode.ui-search-row-56"

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testSearchFieldAndToolbarFocusWithLargeLibrary() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)[needleID].waitForExistence(timeout: 5))
        captureAcceptanceScreenshot("library-before-search")
        let field = searchField(in: app)
        assertDisjoint(field, fromEpisodeRowsIn: app)
        XCTAssertFalse(app.buttons["library.miniPlayer.open"].exists)
        field.tap()
        assertCanTypeAndFilter(in: app)

        app.terminate()
        app.launch()
        let searchButton = app.buttons["library.search"]
        XCTAssertTrue(searchButton.waitForExistence(timeout: 5))
        XCTAssertTrue(searchButton.isHittable)
        assertDisjoint(searchButton, fromEpisodeRowsIn: app)
        searchButton.tap()
        assertCanTypeAndFilter(in: app)
    }

    func testSyntheticLaunchStaysPausedAndSearchDoesNotHitMiniPlayer() {
        let app = XCUIApplication()
        app.launchArguments = ["-OpenSyntheticEpisode", "ui-search-row-56"]
        app.launch()
        let player = app.navigationBars["正在播放"]
        XCTAssertTrue(player.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts[syntheticTitle].exists)
        XCTAssertTrue(app.staticTexts["听后感"].exists)
        let play = app.buttons["播放"]
        XCTAssertTrue(play.exists)
        XCTAssertTrue(play.isEnabled)
        let played = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true"), object: app.buttons["暂停"]
        )
        played.isInverted = true
        wait(for: [played], timeout: 2)
        XCTAssertTrue(app.staticTexts["0:00"].exists)
        captureAcceptanceScreenshot("paused-player-listen-note")

        player.buttons.element(boundBy: 0).tap()
        let miniOpen = app.buttons["library.miniPlayer.open"]
        let miniTransport = app.buttons["library.miniPlayer.transport"]
        XCTAssertTrue(miniOpen.waitForExistence(timeout: 5))
        XCTAssertTrue(miniTransport.isHittable)
        let field = searchField(in: app)
        let toolbar = app.buttons["library.search"]
        for control in [field, toolbar] {
            assertDisjoint(control, fromEpisodeRowsIn: app)
            XCTAssertFalse(control.frame.intersects(miniOpen.frame))
            XCTAssertFalse(control.frame.intersects(miniTransport.frame))
        }

        field.tap()
        assertCanTypeAndFilter(in: app)
        XCTAssertFalse(miniOpen.exists)
        XCTAssertFalse(miniTransport.exists)
    }

    // Run against Debug before the fixed fixture is copied, and against Release
    // after it is copied. Both launches must leave the app at the library.
    func testLaunchArgumentLeavesLibraryVisible() {
        let app = XCUIApplication()
        app.launchArguments = ["-OpenSyntheticEpisode", "ui-search-row-56"]
        app.launch()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.descendants(matching: .any)[needleID].waitForExistence(timeout: 5))
        let navigated = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true"), object: app.navigationBars["正在播放"]
        )
        navigated.isInverted = true
        wait(for: [navigated], timeout: 2)
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
        field.typeText(needle)
        XCTAssertEqual(field.value as? String, needle)
        XCTAssertTrue(app.staticTexts["搜索结果 · 1"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.descendants(matching: .any)[needleID].exists)
        XCTAssertFalse(app.navigationBars["正在播放"].exists)
        assertDisjoint(field, fromEpisodeRowsIn: app)
        captureAcceptanceScreenshot("library-search-filtered")
    }
}
