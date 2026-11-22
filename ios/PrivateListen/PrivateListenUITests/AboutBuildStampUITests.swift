import XCTest

final class AboutBuildStampUITests: XCTestCase {
    func testAboutFooterShowsReadOnlyBuildStamp() {
        let app = XCUIApplication()
        app.launch()
        let more = app.buttons["更多"]
        XCTAssertTrue(more.waitForExistence(timeout: 10), "资料库应有「更多」")
        more.tap()
        let about = app.buttons["关于"]
        XCTAssertTrue(about.waitForExistence(timeout: 5), "更多菜单应有只读「关于」")
        about.tap()
        let sha = app.staticTexts["about.git-short-sha"]
        let stamp = app.staticTexts["about.build-timestamp"]
        XCTAssertTrue(sha.waitForExistence(timeout: 5))
        XCTAssertTrue(stamp.waitForExistence(timeout: 5))
        XCTAssertFalse((sha.label as String).isEmpty)
        XCTAssertFalse((stamp.label as String).isEmpty)
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "about-build-stamp"
        shot.lifetime = .keepAlways
        add(shot)
    }
}
