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
        let noteDictationHint = app.staticTexts["player.listenNote.dictationHint"]
        XCTAssertTrue(noteDictationHint.exists)
        XCTAssertEqual(
            noteDictationHint.label,
            "先点输入框，再用系统键盘的麦克风听写；语音处理遵循你的系统与键盘设置。"
        )
        XCTAssertFalse(noteDictationHint.label.contains("不录音"))
        XCTAssertFalse(noteDictationHint.label.contains("不上传"))
        let editor = app.textViews["player.listenNote.editor"]
        XCTAssertTrue(editor.isHittable)
        captureAcceptanceScreenshot("core-ux-playback")
        editor.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 4))
        editor.typeText("Core UX")
        XCTAssertEqual(editor.value as? String, "Core UX")
        switchSide(in: app, toReading: true)
        XCTAssertTrue(app.staticTexts["player.reading.empty"].waitForExistence(timeout: 20))
        XCTAssertFalse(app.staticTexts["player.reading.originalHint"].exists)
        switchSide(in: app, toReading: false)
        XCTAssertEqual(editor.value as? String, "Core UX")
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "player.timeline.node.")).firstMatch.exists)
        let transport = app.buttons["player.transport"]
        if transport.label != "暂停" { transport.tap() }
        let elapsed = app.staticTexts["player.elapsed"]
        try waitForAdvance(elapsed, from: try seconds(elapsed))
    }

    func testListenAlongCaptureAndEmptyReadingPeek() throws {
        let app = try openFixture("CORE_UX_EMPTY_ID")
        let capture = app.buttons["player.listenAlong.capture"]
        XCTAssertTrue(capture.waitForExistence(timeout: 8))
        capture.tap()

        let editor = app.textFields["player.listenAlong.editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 4))
        let listenAlongDictationHint = app.staticTexts["player.listenAlong.dictationHint"]
        XCTAssertTrue(listenAlongDictationHint.exists)
        XCTAssertEqual(
            listenAlongDictationHint.label,
            "先点输入框，再用系统键盘的麦克风听写；语音处理遵循你的系统与键盘设置。"
        )
        XCTAssertFalse(listenAlongDictationHint.label.contains("不录音"))
        XCTAssertFalse(listenAlongDictationHint.label.contains("不上传"))
        editor.tap()
        editor.typeText("Step2 note")

        let save = app.buttons["player.listenAlong.save"]
        XCTAssertTrue(save.waitForExistence(timeout: 4))
        save.tap()

        let noteButton = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "player.listenAlong.note.")).firstMatch
        XCTAssertTrue(noteButton.waitForExistence(timeout: 5))
        XCTAssertTrue(noteButton.label.contains("Step2 note"))
        captureAcceptanceScreenshot("core-ux-listen-along")

        switchSide(in: app, toReading: true)
        let empty = app.staticTexts["player.reading.empty"]
        let firstParagraph = app.staticTexts["player.reading.paragraph.0"]
        if empty.waitForExistence(timeout: 15) {
            empty.press(forDuration: 0.8)
            XCTAssertTrue(empty.exists)
        } else if firstParagraph.waitForExistence(timeout: 5) {
            firstParagraph.press(forDuration: 0.8)
            assertOriginalPeekShown(in: app)
            app.buttons["关闭"].tap()
        } else {
            XCTFail("Expected either empty reading state or transcript paragraph on B side")
        }

        switchSide(in: app, toReading: false)
        XCTAssertTrue(app.textViews["player.listenNote.editor"].waitForExistence(timeout: 5))

        let deleteButton = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "player.listenAlong.delete.")).firstMatch
        if deleteButton.exists, deleteButton.isHittable {
            deleteButton.tap()
        }
    }

    func testOriginalPeekOnSegmentedEpisodeWithoutStoppingPlayback() throws {
        let app = try openFixture("CORE_UX_SEGMENTED_ID")
        let aClock = app.staticTexts["player.elapsed"]
        let transport = app.buttons["player.transport"]
        if transport.label != "暂停" { transport.tap() }
        let origin = try seconds(aClock)
        try waitForAdvance(aClock, from: origin)

        switchSide(in: app, toReading: true)
        let bClock = app.staticTexts["player.reading.elapsed"]
        XCTAssertTrue(bClock.waitForExistence(timeout: 5))
        let originalHint = app.staticTexts["player.reading.originalHint"]
        XCTAssertTrue(originalHint.waitForExistence(timeout: 15))
        let beforePeek = try seconds(bClock)

        let firstParagraph = app.staticTexts["player.reading.paragraph.0"]
        if scrollToElement(firstParagraph, in: app) {
            firstParagraph.press(forDuration: 0.8)
        } else {
            let firstNode = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "player.reading.node.")).firstMatch
            XCTAssertTrue(firstNode.waitForExistence(timeout: 15))
            firstNode.press(forDuration: 0.8)
        }

        let unavailable = app.staticTexts["player.reading.original.unavailable"]
        XCTAssertTrue(unavailable.waitForExistence(timeout: 5))
        XCTAssertEqual(unavailable.label, "本集没有对应原文")
        XCTAssertFalse(app.staticTexts["player.reading.original"].exists)
        captureAcceptanceScreenshot("core-ux-original-peek")
        app.buttons["关闭"].tap()

        XCTAssertTrue(bClock.waitForExistence(timeout: 5))
        XCTAssertGreaterThanOrEqual(try seconds(bClock), beforePeek)

        switchSide(in: app, toReading: false)
        XCTAssertTrue(aClock.waitForExistence(timeout: 5))
        XCTAssertGreaterThanOrEqual(try seconds(aClock), beforePeek)
        XCTAssertEqual(transport.label, "暂停")
    }

    func testOriginalPeekOnSyntheticBilingualFixtureShowsEnglishOriginal() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-OpenSyntheticBilingualFixture"]
        app.launch()
        XCTAssertTrue(app.navigationBars["正在播放"].waitForExistence(timeout: 8))

        switchSide(in: app, toReading: true)
        let originalHint = app.staticTexts["player.reading.originalHint"]
        XCTAssertTrue(originalHint.waitForExistence(timeout: 10))
        let firstParagraph = app.staticTexts["player.reading.paragraph.0"]
        XCTAssertTrue(firstParagraph.waitForExistence(timeout: 10))
        firstParagraph.press(forDuration: 0.8)

        let original = app.staticTexts["player.reading.original"]
        XCTAssertTrue(original.waitForExistence(timeout: 5))
        XCTAssertTrue(original.label.contains("First synthetic English original paragraph"))
        XCTAssertFalse(app.staticTexts["player.reading.original.unavailable"].exists)
        captureAcceptanceScreenshot("core-ux-original-peek-bilingual")
        app.buttons["关闭"].tap()

        let secondParagraph = app.staticTexts["player.reading.paragraph.1"]
        XCTAssertTrue(secondParagraph.waitForExistence(timeout: 5))
        secondParagraph.press(forDuration: 0.8)
        XCTAssertTrue(original.waitForExistence(timeout: 5))
        XCTAssertTrue(original.label.contains("Second synthetic English paragraph"))
        XCTAssertFalse(app.staticTexts["player.reading.original.unavailable"].exists)
        app.buttons["关闭"].tap()

        let firstNode = app.buttons["player.reading.node.vp-bilingual-1"]
        if firstNode.waitForExistence(timeout: 5), firstNode.isHittable {
            firstNode.press(forDuration: 0.8)
            XCTAssertTrue(original.waitForExistence(timeout: 5))
            XCTAssertTrue(original.label.contains("First synthetic English original paragraph"))
            XCTAssertFalse(app.staticTexts["player.reading.original.unavailable"].exists)
            app.buttons["关闭"].tap()
        }
    }

    private func assertOriginalPeekShown(in app: XCUIApplication) {
        let original = app.staticTexts["player.reading.original"]
        let unavailable = app.staticTexts["player.reading.original.unavailable"]
        let shown = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            original.exists || unavailable.exists
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [shown], timeout: 5), .completed)
    }

    private func scrollToElement(_ element: XCUIElement, in app: XCUIApplication, maxSwipes: Int = 8) -> Bool {
        if element.waitForExistence(timeout: 5), element.isHittable { return true }
        for _ in 0..<maxSwipes {
            if element.exists, element.isHittable { return true }
            app.swipeUp()
        }
        return element.exists && element.isHittable
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
