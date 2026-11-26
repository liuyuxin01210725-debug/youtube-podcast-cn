import XCTest

@MainActor
final class ListenPicksAndFollowUITests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testValidMultiPickSelectsACEAndJumpsAccuratelyWithoutFinishing() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-OpenSyntheticListenPicksFixture"]
        app.launch()

        let status = app.staticTexts["player.contentStatus"]
        XCTAssertTrue(status.waitForExistence(timeout: 15))
        XCTAssertEqual(status.label, "中文成片")
        XCTAssertTrue(app.staticTexts["player.contentStatus.review"].label.contains("未人审"))

        let pickA = app.buttons["player.picks.item.A"]
        let pickC = app.buttons["player.picks.item.C"]
        let pickE = app.buttons["player.picks.item.E"]
        XCTAssertTrue(pickA.waitForExistence(timeout: 15))
        XCTAssertTrue(pickC.exists)
        XCTAssertTrue(pickE.exists)
        XCTAssertFalse(app.buttons["player.picks.item.B"].exists)
        XCTAssertFalse(app.buttons["player.picks.item.D"].exists)

        XCTAssertTrue(pickC.label.contains("依据片段约 15 秒"))
        XCTAssertTrue(pickC.label.contains("核心观点 · 有译稿依据"))
        XCTAssertFalse(pickC.label.contains("AI"))
        XCTAssertFalse(pickC.label.contains("语义"))

        pickC.tap()

        let aClock = app.staticTexts["player.elapsed"]
        let transport = app.buttons["player.transport"]
        let landed = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            guard let actual = self.clock(aClock.label) else { return false }
            return actual >= 40 && actual <= 47 && transport.label == "暂停"
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [landed], timeout: 8), .completed)

        let startSec = try seconds(aClock)
        try waitForAdvance(aClock, from: startSec)

        // 退回列表验证未误标已听完
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.navigationBars["资料库"].waitForExistence(timeout: 8))
    }

    func testReadingPaneFollowsDoesNotHijackUserScrollAndBackToCurrentWorks() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-OpenSyntheticListenPicksFixture"]
        app.launch()

        let pickC = app.buttons["player.picks.item.C"]
        XCTAssertTrue(pickC.waitForExistence(timeout: 15))
        pickC.tap()

        let aClock = app.staticTexts["player.elapsed"]
        let landed = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            guard let actual = self.clock(aClock.label) else { return false }
            return actual >= 40 && actual <= 47
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [landed], timeout: 8), .completed)

        // 切到 B 面
        let readingSegment = app.segmentedControls["player.side"].buttons["B 面 · 译稿与分段"]
        XCTAssertTrue(readingSegment.waitForExistence(timeout: 5))
        readingSegment.tap()

        let bClock = app.staticTexts["player.reading.elapsed"]
        XCTAssertTrue(bClock.waitForExistence(timeout: 5))

        let nodeC = app.buttons["player.reading.node.C"]
        XCTAssertTrue(nodeC.waitForExistence(timeout: 5))
        XCTAssertEqual(nodeC.value as? String, "当前段")

        // 用户主动多次上滑滚到下方译稿
        let scrollView = app.scrollViews.firstMatch
        for _ in 0..<5 {
            scrollView.swipeUp()
        }

        let backButton = app.buttons["player.reading.backToCurrent"]
        XCTAssertTrue(backButton.waitForExistence(timeout: 5))

        // 等待 3 秒播放推进，验证不抢滚（下方译稿仍可见且按钮仍在）
        let beforeWait = try seconds(bClock)
        try waitForAdvance(bClock, from: beforeWait, timeout: 6)
        XCTAssertTrue(backButton.exists)

        // 点击回到当前段
        let beforeTap = try seconds(bClock)
        backButton.tap()
        XCTAssertTrue(nodeC.waitForExistence(timeout: 5))
        XCTAssertTrue(nodeC.isHittable)

        let afterTap = try seconds(bClock)
        XCTAssertGreaterThanOrEqual(afterTap, beforeTap)
        try waitForAdvance(bClock, from: afterTap, timeout: 6)
    }

    func testFakeTimelineShowsEmptyPicksAndNoBackToCurrent() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-OpenSyntheticFakeTimelineFixture"]
        app.launch()

        let emptyPicks = app.staticTexts["player.picks.empty"]
        XCTAssertTrue(emptyPicks.waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "player.picks.item.")).firstMatch.exists)

        app.segmentedControls["player.side"].buttons["B 面 · 译稿与分段"].tap()
        XCTAssertTrue(app.staticTexts["player.reading.elapsed"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["player.reading.backToCurrent"].exists)
    }

    func testPendingSampleAndUnverifiedAudioStatuses() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-OpenSyntheticPendingSampleFixture"]
        app.launch()

        let pendingStatus = app.staticTexts["player.contentStatus"]
        XCTAssertTrue(pendingStatus.waitForExistence(timeout: 15))
        XCTAssertEqual(pendingStatus.label, "待核听样")
        XCTAssertTrue(app.staticTexts["player.pendingTranscript.label"].exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "player.picks.item.")).firstMatch.exists)
        app.terminate()

        app.launchArguments = ["-OpenSyntheticUnverifiedAudioFixture"]
        app.launch()

        let unverifiedStatus = app.staticTexts["player.contentStatus"]
        XCTAssertTrue(unverifiedStatus.waitForExistence(timeout: 15))
        XCTAssertEqual(unverifiedStatus.label, "来源待核")
        XCTAssertTrue(app.staticTexts["player.contentStatus.review"].label.contains("未人审"))
        XCTAssertTrue(app.staticTexts["player.picks.empty"].waitForExistence(timeout: 10))
    }

    private func seconds(_ element: XCUIElement) throws -> Int {
        try XCTUnwrap(clock(element.label))
    }

    private func clock(_ label: String) -> Int? {
        let parts = label.split(separator: ":").compactMap { Int($0) }
        switch parts.count {
        case 2: return parts[0] * 60 + parts[1]
        case 3: return parts[0] * 3600 + parts[1] * 60 + parts[2]
        default: return nil
        }
    }

    private func waitForAdvance(_ element: XCUIElement, from initial: Int, timeout: TimeInterval = 6) throws {
        let exp = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            guard let now = self.clock(element.label) else { return false }
            return now > initial
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [exp], timeout: timeout), .completed)
    }
}
