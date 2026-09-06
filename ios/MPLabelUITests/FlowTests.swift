//  FlowTests.swift
//
//  End-to-end: the real app, driving the real server, over HTTP.
//
//  These assert on what she would see and touch. They are deliberately
//  few and about whole journeys rather than individual controls - a UI
//  test per button is a maintenance bill that gets paid by deleting the
//  tests.

import XCTest

final class FlowTests: XCTestCase {

    private var server: ServerHarness!

    /// Set by the tests that change data, and put back in teardown.
    private var shippedSale: Int?
    private var unbinned: (listing: Int, bin: String)?

    override func setUpWithError() throws {
        continueAfterFailure = false
        // Skips with instructions if the script did not start one.
        server = try ServerHarness.fromEnvironment()
    }

    /// Undo the one destructive test, because the server is shared and
    /// **XCTest runs these in alphabetical order, not source order.**
    ///
    /// That is what the first version of this file got wrong. It said
    /// "anything that ships a parcel is the last thing to touch it" and
    /// put the shipping test last in the file - but `testShipping…`
    /// sorts before `testTheQueue…`, so 7QK was already gone by the time
    /// the queue test looked for it, and the failure read as a queue
    /// that had stopped rendering its rows.
    ///
    /// Restoring the row here is what makes the order stop mattering,
    /// rather than a name chosen to sort late - which would be the same
    /// trap set again for whoever adds the ninth test.
    override func tearDownWithError() throws {
        if let id = shippedSale {
            shippedSale = nil
            try server.post("/api/v1/orders/\(id)/unship")
        }
        if let moved = unbinned {
            unbinned = nil
            try server.post("/api/v1/inventory/\(moved.listing)/bin",
                            body: ["bin": moved.bin])
        }
    }

    /// The app gets the same address the runner was given. Note the
    /// server is *shared* across the tests in this file rather than one
    /// per test - starting it is the script's job and it happens once -
    /// so a test that changes data must put it back in teardown. Do not
    /// rely on running last instead: XCTest sorts by method name.
    private func launch(signedIn: Bool = true) throws -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["MPLABEL_UITEST"] = "1"
        app.launchEnvironment["MPLABEL_UITEST_RESET"] = "1"
        app.launchEnvironment["MPLABEL_UITEST_SERVER"] = server.baseURL
        if signedIn {
            app.launchEnvironment["MPLABEL_UITEST_TOKEN"] = server.token
        }
        app.launch()
        return app
    }

    // MARK: - the journeys

    func testSigningInReachesTheQueue() throws {
        let app = try launch(signedIn: false)

        let field = app.secureTextFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 10),
                      "with a server set but no token, the password screen "
                      + "is what should be showing")
        field.tap()
        field.typeText(server.password)
        app.buttons["Sign in"].tap()

        XCTAssertTrue(app.staticTexts["To ship"].waitForExistence(timeout: 15))
    }

    func testTheQueueShowsWhatHasToGoOut() throws {
        let app = try launch()
        XCTAssertTrue(app.staticTexts["To ship"].waitForExistence(timeout: 15))

        // Both seeded sales, including the local pickup that has no
        // ship-by and no label - the row that decodes to mostly nils and
        // has to render anyway.
        XCTAssertTrue(app.staticTexts["7QK"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["B4M"].exists)
    }

    func testAnOrderOpensAndShowsTheAddress() throws {
        let app = try launch()
        XCTAssertTrue(app.staticTexts["7QK"].waitForExistence(timeout: 15))
        app.staticTexts["7QK"].tap()

        XCTAssertTrue(app.staticTexts["Ships to"].waitForExistence(timeout: 10))
        // The detail screen is the only one that asks for an address, so
        // this also asserts the queue payload's omission is deliberate
        // rather than a bug the detail screen works around.
        XCTAssertTrue(app.staticTexts
            .containing(NSPredicate(format: "label CONTAINS 'SHELBYVILLE'"))
            .firstMatch.exists)
    }

    /// The guard on the two irreversible actions. If this ever fails
    /// because a tap was enough, the guard is gone - and a tap is how a
    /// parcel gets marked shipped by a thumb in a coat pocket.
    func testShippingNeedsAHoldNotATap() throws {
        let app = try launch()
        XCTAssertTrue(app.staticTexts["7QK"].waitForExistence(timeout: 15))
        app.staticTexts["7QK"].tap()

        let ship = app.buttons["Hold to mark shipped"]
        XCTAssertTrue(ship.waitForExistence(timeout: 10))

        // Before the hold, not after: a shipped sale is off `/orders`,
        // so this is the last moment the code can be turned into the id
        // teardown needs.
        shippedSale = try server.saleID(code: "7QK")

        // A tap: too short to complete the hold.
        ship.tap()
        XCTAssertFalse(
            app.staticTexts.containing(
                NSPredicate(format: "label CONTAINS 'shipped and free'")
            ).firstMatch.waitForExistence(timeout: 3),
            "a tap must not ship a parcel")

        // A hold: long enough.
        ship.press(forDuration: 1.4)
        XCTAssertTrue(
            app.staticTexts.containing(
                NSPredicate(format: "label CONTAINS 'shipped and free'")
            ).firstMatch.waitForExistence(timeout: 10),
            "holding it should have shipped the parcel")
    }

    func testTheShelfSearchesAcrossEverything() throws {
        let app = try launch()
        app.buttons["Shelf"].tap()
        XCTAssertTrue(app.staticTexts["Where things are"]
            .waitForExistence(timeout: 15))

        let search = app.searchFields.firstMatch
        XCTAssertTrue(search.waitForExistence(timeout: 10))
        search.tap()
        // A bin name, not a title: the server searches title, bin name,
        // bin code, category and inventory code in one query, and this
        // is the half a client-side title filter would miss.
        search.typeText("ATTIC")
        XCTAssertTrue(app.staticTexts["Hobnail milk glass vase"]
            .waitForExistence(timeout: 10))
    }

    func testMovingSomethingToABinSticks() throws {
        let app = try launch()
        app.buttons["Shelf"].tap()
        XCTAssertTrue(app.staticTexts["Hobnail milk glass vase"]
            .waitForExistence(timeout: 15))
        app.staticTexts["Hobnail milk glass vase"].tap()

        XCTAssertTrue(app.staticTexts["Where it is"]
            .waitForExistence(timeout: 10))

        // Before the tap. This is the other test that changes shared
        // data, and it is the one the shelf search depends on: that test
        // finds the vase by typing its *bin name*, so leaving the vase
        // off the shelf makes it fail - and `testMoving…` sorts first.
        unbinned = (listing: try server.listingID(inventoryCode: "7K2M"),
                    bin: "ATTIC")

        app.staticTexts["No bin"].firstMatch.tap()

        // Taking it off the shelf is a real answer, not a delete, so the
        // screen should now say so rather than emptying.
        XCTAssertTrue(app.staticTexts["Not set"].waitForExistence(timeout: 10))
    }

    /// The simulator has no camera. This asserts the app says so rather
    /// than showing a blank rectangle - which is exactly what it did on
    /// a real phone when nothing requested permission.
    func testTheScannerExplainsItselfWithoutACamera() throws {
        let app = try launch()
        app.buttons["Scan"].tap()
        XCTAssertTrue(app.staticTexts["No camera here"]
            .waitForExistence(timeout: 15))
    }

    func testProfitSaysWhatItDoesNotKnow() throws {
        let app = try launch()
        app.buttons["Profit"].tap()
        XCTAssertTrue(app.staticTexts["Profit"].waitForExistence(timeout: 15))
        // The screen must keep saying it is gross. Someone tidying the
        // copy would be removing a caveat, not a caption.
        XCTAssertTrue(app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS 'Gross, not profit'")
        ).firstMatch.waitForExistence(timeout: 10))
    }
}
