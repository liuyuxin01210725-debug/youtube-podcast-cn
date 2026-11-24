import XCTest

/// The runner supplies local fixture IDs so this suite can travel without the library.
@MainActor
final class PlayerCoreUXUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testPlayingAcrossSidesAndSeekingToBoundNode() throws {
        let app = try openFixture("CORE_UX_SEGMENTED_ID")
        let aClock = app.staticTexts["player.elapsed"]
        let transport = app.buttons["player.transport"]
        if transport.label != "暂停" { transport.tap() }
        let markers = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "player.timeline.node."))
        XCTAssertTrue(markers.firstMatch.waitForExistence(timeout: 25))
        let origin = try seconds(aClock)
        try waitForAdvance(aClock, from: origin)
        let beforeSwitch = try seconds(aClock)
        let switchedAt = Date()
        switchSide(in: app, toReading: true)
        let bClock = app.staticTexts["player.reading.elapsed"]
        XCTAssertTrue(bClock.waitForExistence(timeout: 5))
        let onB = try seconds(bClock)
        XCTAssertGreaterThanOrEqual(onB, beforeSwitch)
        XCTAssertLessThanOrEqual(onB - beforeSwitch, Int(-switchedAt.timeIntervalSinceNow * 2) + 3)
        try waitForAdvance(bClock, from: onB)
        captureAcceptanceScreenshot("core-ux-reading")
        switchSide(in: app, toReading: false)
        XCTAssertEqual(transport.label, "暂停")
        XCTAssertGreaterThan(try seconds(aClock), onB)
        let current = try seconds(aClock)
        let marker = try XCTUnwrap(markers.allElementsBoundByIndex.first {
            guard let label = $0.label.components(separatedBy: "，").first, let target = clock(label)
            else { return false }
            return $0.isHittable && abs(target - current) > 12
        })
        let target = try XCTUnwrap(clock(marker.label.components(separatedBy: "，")[0]))
        marker.tap()
        let landed = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            guard let actual = self.clock(aClock.label) else { return false }
            return actual >= target && actual <= target + 6 && transport.label == "暂停"
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [landed], timeout: 5), .completed)
        try waitForAdvance(aClock, from: try seconds(aClock))
    }

    func testContinuousTimelineAndDirectNoteEntry() throws {
        let app = try openFixture("CORE_UX_EMPTY_ID")
        XCTAssertTrue(app.sliders["player.timeline.slider"].isHittable)
        XCTAssertTrue(app.buttons["player.skip.back"].isHittable)
        XCTAssertTrue(app.buttons["player.skip.forward"].isHittable)
        XCTAssertTrue(app.staticTexts["听后感"].isHittable)
        XCTAssertTrue(app.staticTexts["player.listenNote.placeholder"].exists)
        let editor = app.textViews["player.listenNote.editor"]
        XCTAssertTrue(editor.isHittable)
        captureAcceptanceScreenshot("core-ux-playback")
        editor.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 4))
        editor.typeText("Core UX")
        XCTAssertEqual(editor.value as? String, "Core UX")
        switchSide(in: app, toReading: true)
        XCTAssertTrue(app.staticTexts["player.reading.empty"].waitForExistence(timeout: 20))
        switchSide(in: app, toReading: false)
        XCTAssertEqual(editor.value as? String, "Core UX")
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "player.timeline.node.")).firstMatch.exists)
        let transport = app.buttons["player.transport"]
        if transport.label != "暂停" { transport.tap() }
        let elapsed = app.staticTexts["player.elapsed"]
        try waitForAdvance(elapsed, from: try seconds(elapsed))
    }

    private func openFixture(_ key: String) throws -> XCUIApplication {
        guard let id = ProcessInfo.processInfo.environment[key], !id.isEmpty else {
            throw XCTSkip("This journey requires a preinstalled sanitized library and \(key).")
        }
        let app = XCUIApplication()
        app.launch()
        openLibraryEpisodeForReadOnlyObservation(id, in: app)
        XCTAssertTrue(app.navigationBars["正在播放"].waitForExistence(timeout: 8))
        return app
    }

    private func switchSide(in app: XCUIApplication, toReading: Bool) {
        app.segmentedControls["player.side"].buttons[toReading ? "B 面 · 译稿与分段" : "A 面 · 播放"].tap()
    }

    private func clock(_ label: String) -> Int? {
        let fields = label.split(separator: ":")
        let values = fields.compactMap { Int($0) }
        guard (2...3).contains(fields.count), values.count == fields.count else { return nil }
        return values.reduce(0) { $0 * 60 + $1 }
    }

    private func seconds(_ element: XCUIElement) throws -> Int {
        try XCTUnwrap(clock(element.label))
    }

    private func waitForAdvance(_ element: XCUIElement, from start: Int) throws {
        let advanced = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            guard let now = self.clock(element.label) else { return false }
            return now > start
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [advanced], timeout: 6), .completed)
    }
}
