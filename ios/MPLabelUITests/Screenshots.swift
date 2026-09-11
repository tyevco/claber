//  Screenshots.swift
//
//  A walk through every screen, saving a picture of each.
//
//  This exists because looking at the app found two things the assertions
//  did not: a photograph that failed to load sat on a spinner for ever,
//  and the save button had drifted underneath two optional panels. Both
//  are invisible to a test that asks whether a string is on screen, and
//  both are obvious in a picture.
//
//  It is skipped unless `MPLABEL_SHOTS=1`, so the ordinary suite does not
//  pay two minutes for pictures nobody is going to look at. Run it with
//  `./ios/screenshots.sh`, which starts the same seeded server the UI
//  tests use and pulls the PNGs out of the result bundle afterwards.
//
//  Deliberately not a snapshot test. Nothing here compares against a
//  committed image: a pixel diff on a design that is still moving fails
//  every time a padding changes, and the failure says "something moved"
//  rather than "this is wrong". The judgement is a person's; this only
//  makes it cheap to make.

import XCTest

final class ScreenshotTests: XCTestCase {

    private var server: ServerHarness!

    override func setUpWithError() throws {
        continueAfterFailure = false
        try XCTSkipUnless(
            ProcessInfo.processInfo.environment["MPLABEL_SHOTS"] == "1",
            "Screenshots are off. Run ./ios/screenshots.sh to take them.")
        server = try ServerHarness.fromEnvironment()
    }

    /// Sheets do not have a back button and do not reliably go away on
    /// `swipeDown()` - a drag from the top of the card to the bottom of
    /// the screen is what actually dismisses one. Getting this wrong
    /// failed silently and took every screen after it down with it.
    private func dismissSheet(_ app: XCUIApplication) {
        let top = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5,
                                                                dy: 0.12))
        let bottom = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5,
                                                                   dy: 0.97))
        top.press(forDuration: 0.15, thenDragTo: bottom)
    }

    private func shot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func testEveryScreen() throws {
        let app = XCUIApplication()
        app.launchEnvironment["MPLABEL_UITEST"] = "1"
        app.launchEnvironment["MPLABEL_UITEST_RESET"] = "1"
        app.launchEnvironment["MPLABEL_UITEST_SERVER"] = server.baseURL
        app.launchEnvironment["MPLABEL_UITEST_TOKEN"] = server.token
        app.launch()

        XCTAssertTrue(app.staticTexts["To ship"].waitForExistence(timeout: 15))
        shot("01-to-ship")

        app.staticTexts["7QK"].tap()
        XCTAssertTrue(app.staticTexts["Ships to"].waitForExistence(timeout: 10))
        shot("02-order-detail")
        app.swipeUp()
        shot("02b-order-detail-lower")
        if app.buttons["Edit"].waitForExistence(timeout: 5) {
            app.buttons["Edit"].tap()
            sleep(1)
            shot("02c-order-detail-fixing")
        }
        app.navigationBars.buttons.firstMatch.tap()

        app.buttons["Capture"].tap()
        XCTAssertTrue(app.staticTexts["No camera here"]
            .waitForExistence(timeout: 15))
        shot("03-capture")

        _ = app.buttons["GOODWILL 214"].waitForExistence(timeout: 8)
        shot("04-which-shop")
        if app.buttons["GOODWILL 214"].exists {
            app.buttons["GOODWILL 214"].tap()
        }
        app.buttons["Go to the cart"].tap()
        XCTAssertTrue(app.staticTexts["What did it cost?"]
            .waitForExistence(timeout: 15))
        shot("05-reconcile")

        app.buttons["Shelf"].tap()
        XCTAssertTrue(app.staticTexts["Where things are"]
            .waitForExistence(timeout: 15))
        shot("06-shelf")

        app.staticTexts["Sourcing runs"].tap()
        XCTAssertTrue(app.staticTexts["Runs"].waitForExistence(timeout: 10))
        shot("07-runs")
        app.staticTexts["GOODWILL 214"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts["What came home"]
            .waitForExistence(timeout: 10))
        shot("08-one-trip")

        app.buttons["Shelf"].tap()
        app.buttons["Add"].tap()
        app.buttons["New item"].tap()
        XCTAssertTrue(app.staticTexts["New item"].waitForExistence(timeout: 10))
        shot("09-add-an-item")
        app.swipeUp()
        shot("10-add-an-item-lower")
        dismissSheet(app)

        app.buttons["Profit"].tap()
        XCTAssertTrue(app.staticTexts["Profit"].waitForExistence(timeout: 15))
        shot("11-profit")

        app.buttons["Scan"].tap()
        XCTAssertTrue(app.staticTexts["No camera here"]
            .waitForExistence(timeout: 15))
        shot("12-scan")

        // Settings, and the screen pushed inside it. Worth a picture for
        // one specific reason: this screen grows downwards as the server
        // answers - an options card, then a result, then a region
        // picker - and the hold button has to stay above all of it. That
        // is the exact drift a screenshot found on the add-item screen
        // and no assertion here would notice.
        app.buttons["To ship"].tap()
        XCTAssertTrue(app.staticTexts["To ship"].waitForExistence(timeout: 10))
        app.buttons["Settings"].tap()
        XCTAssertTrue(app.staticTexts["Settings"].waitForExistence(timeout: 10))
        shot("13-settings")
        app.staticTexts["Print a label"].tap()
        XCTAssertTrue(app.staticTexts["Choose a PDF"]
            .waitForExistence(timeout: 10))
        shot("14-print-a-label")
        // A drag dismisses the whole sheet, pushed screen and all. The
        // assertion afterwards is the point: this used to fail silently
        // and take every screen after it down, and the fix is to notice
        // here rather than in a picture of the wrong thing.
        dismissSheet(app)
        XCTAssertTrue(app.staticTexts["To ship"].waitForExistence(timeout: 10),
                      "the Settings sheet did not go away")

        // Last, because it is a sheet and a sheet does not reliably go
        // away on a swipe - dismissing one failed silently mid-walk and
        // took every screen after it with it. Nothing follows this, so
        // it cannot.
        app.staticTexts["to print"].tap()
        XCTAssertTrue(app.staticTexts["Pending labels"]
            .waitForExistence(timeout: 10))
        shot("15-pending")
    }
}
