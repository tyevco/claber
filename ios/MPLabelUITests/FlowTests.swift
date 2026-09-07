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
    private var correctedSale: Int?

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
        if let id = correctedSale {
            correctedSale = nil
            // Put the parser's own reading back, so the queue test that
            // follows sees the row it expects.
            try server.post("/api/v1/orders/\(id)/fields",
                            body: ["buyer": "Sam Sample"])
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

    /// The simulator has no camera, so this is the same assertion the
    /// scanner gets and for the same reason: the first version of a
    /// camera screen in this app showed a blank rectangle, which is the
    /// worst possible way to say "there is no camera here".
    func testTheCaptureTabExplainsItselfWithoutACamera() throws {
        let app = try launch()
        app.buttons["Capture"].tap()
        XCTAssertTrue(app.staticTexts["No camera here"]
            .waitForExistence(timeout: 15))
    }

    /// Triage is the half that works without a camera, which is exactly
    /// why it is reachable from the screen that cannot use one.
    func testTriageShowsTheCaptureAndTheRunItCouldBelongTo() throws {
        let app = try launch()
        app.buttons["Capture"].tap()
        XCTAssertTrue(app.staticTexts["No camera here"]
            .waitForExistence(timeout: 15))
        app.buttons["Go to triage"].tap()

        XCTAssertTrue(app.staticTexts["What did it cost?"]
            .waitForExistence(timeout: 15))
        // The seeded capture is about nothing yet, so the pile has it.
        XCTAssertTrue(app.staticTexts["Receipt 1 of 1"].exists)
        // And the run it might belong to is offered rather than typed.
        XCTAssertTrue(app.staticTexts["GOODWILL 214"]
            .waitForExistence(timeout: 10))
    }

    /// A spinner that never stops is how a row pointing at a file that is
    /// gone used to present - the same failure `mplabel verify` exists to
    /// catch for labels. The seeded capture has a row and no file on
    /// disk, which is exactly that case, so the screen has to say so
    /// rather than wait for a picture that is never going to arrive.
    func testAPhotographThatCannotLoadSaysSoRatherThanSpinning() throws {
        let app = try launch()
        app.buttons["Capture"].tap()
        XCTAssertTrue(app.staticTexts["No camera here"]
            .waitForExistence(timeout: 15))
        app.buttons["Go to triage"].tap()

        XCTAssertTrue(app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS 'did not load'")
        ).firstMatch.waitForExistence(timeout: 15),
        "a missing photo file must be reported, not spun on")
    }

    /// Capture took Pending's tab, so the queue's chip is the only way to
    /// the recovery screen. A screen with no route to it is a feature
    /// that does not exist.
    func testThePendingLabelsAreStillReachableFromTheQueue() throws {
        let app = try launch()
        XCTAssertTrue(app.staticTexts["To ship"].waitForExistence(timeout: 15))
        app.staticTexts["to print"].tap()
        XCTAssertTrue(app.staticTexts["Pending labels"]
            .waitForExistence(timeout: 10))
    }

    /// The screen a local pickup depends on. It is also the one that puts
    /// a cost in, so the assertion is that the thing comes back with what
    /// it cost - a saved item with a null `paid` is the failure that
    /// leaves Profit saying "gross" for ever.
    func testAddingAnItemByHandKeepsWhatItCost() throws {
        let app = try launch()
        app.buttons["Shelf"].tap()
        XCTAssertTrue(app.staticTexts["Where things are"]
            .waitForExistence(timeout: 15))

        app.buttons["Add"].tap()
        app.buttons["New item"].tap()
        XCTAssertTrue(app.staticTexts["New item"].waitForExistence(timeout: 10))

        // By identifier, not by placeholder: a multi-line TextField is a
        // textView in the accessibility tree, and the placeholder is copy
        // that someone will reword without knowing a test reads it.
        let title = app.descendants(matching: .any)["item-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        title.tap()
        title.typeText("Brass candlestick pair")

        // The field this screen exists for.
        let paid = app.descendants(matching: .any)["item-paid"]
        XCTAssertTrue(paid.waitForExistence(timeout: 5))
        paid.tap()
        paid.typeText("7.50")
        app.buttons["Done"].firstMatch.tap()

        // Scroll it into reach first. `press` does not scroll, and a
        // press on an element that is off screen silently does nothing -
        // which reads as the save having failed rather than as never
        // having happened.
        let save = app.buttons["Hold to save"]
        XCTAssertTrue(save.waitForExistence(timeout: 10))
        var swipes = 0
        while !save.isHittable && swipes < 5 {
            app.swipeUp()
            swipes += 1
        }
        save.press(forDuration: 1.4)

        // Back on the shelf, and the thing is on it.
        XCTAssertTrue(app.staticTexts["Brass candlestick pair"]
            .waitForExistence(timeout: 15))

        // Left on the server deliberately. There is no delete endpoint
        // and this test is not the place to invent one: an inventory code
        // is never reused, including after the thing sells, so removing a
        // listing is a real decision rather than tidying. Nothing after
        // this asserts on a count, so an extra row is inert.
        let id = try server.listingID(forTitle: "Brass candlestick pair")
        // Unwrapped rather than compared as an Optional: nil is the
        // failure this asserts against, and `XCTAssertEqual(nil, 7.50)`
        // would report it as a value mismatch rather than as the cost
        // never having arrived.
        let paidBack = try XCTUnwrap(server.paid(forListing: id),
                                     "the item came back with no cost at all")
        XCTAssertEqual(paidBack, 7.50, accuracy: 0.001,
                       "the cost must survive the round trip")
    }

    /// Corrections are a first-class action because the parser gets a
    /// buyer or a price wrong often enough. Destructive, so it puts the
    /// buyer back in teardown.
    func testCorrectingAFieldSticks() throws {
        let app = try launch()
        XCTAssertTrue(app.staticTexts["7QK"].waitForExistence(timeout: 15))
        app.staticTexts["7QK"].tap()
        XCTAssertTrue(app.staticTexts["Ships to"].waitForExistence(timeout: 10))

        correctedSale = try server.saleID(code: "7QK")
        app.buttons["Edit"].tap()

        let buyer = app.descendants(matching: .any)["fix-buyer"]
        XCTAssertTrue(buyer.waitForExistence(timeout: 10))
        buyer.tap()
        // Clear it first: the field is prefilled with what the parser
        // read, which is the point of the screen.
        buyer.press(forDuration: 1.2)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) {
            app.menuItems["Select All"].tap()
        }
        buyer.typeText("Corrected Name")

        app.buttons["Hold to correct"].press(forDuration: 1.4)
        XCTAssertTrue(app.staticTexts["Corrected Name"]
            .waitForExistence(timeout: 15))
    }

    /// The seeded sale points at a label file that is not on disk, which
    /// is exactly the case `mplabel verify` exists for. The screen must
    /// say so rather than spin - the third place in this app where a row
    /// can outlive its file.
    func testAMissingLabelFileSaysSo() throws {
        let app = try launch()
        XCTAssertTrue(app.staticTexts["7QK"].waitForExistence(timeout: 15))
        app.staticTexts["7QK"].tap()
        XCTAssertTrue(app.staticTexts["The label"]
            .waitForExistence(timeout: 10))
        app.buttons["Show it"].tap()
        XCTAssertTrue(app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS 'no label file'")
        ).firstMatch.waitForExistence(timeout: 10),
        "a label file that is gone must be reported, not spun on")
    }

    func testProfitSaysWhatItDoesNotKnow() throws {
        let app = try launch()
        app.buttons["Profit"].tap()
        XCTAssertTrue(app.staticTexts["Profit"].waitForExistence(timeout: 15))
        // The screen must keep saying what is *not* in the figure.
        // Someone tidying the copy would be removing a caveat, not a
        // caption.
        //
        // Asserted on the promise rather than the sentence: this used to
        // pin the literal words "Gross, not profit", which stopped being
        // true when cost basis arrived - so the test was holding a
        // screen to a claim that had become false. What must never go is
        // the admission that postage and Facebook's fee are not counted,
        // whatever the wording around it.
        XCTAssertTrue(app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS 'fee'")
        ).firstMatch.waitForExistence(timeout: 10),
        "the profit screen stopped saying what it does not count")
    }
}
