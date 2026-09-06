//  LogicTests.swift
//
//  The parts of the app that are decisions rather than drawing, and can
//  therefore be wrong quietly.

import XCTest
@testable import MPLabel

final class DueTests: XCTestCase {

    private func due(daysFromToday days: Int) -> Due {
        let date = Calendar.current.date(byAdding: .day, value: days,
                                         to: Date())!
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return Due(shipBy: f.string(from: date))
    }

    /// Ship-by is a hard Facebook commitment, so this decides what the
    /// queue shouts about. Getting "overdue" wrong in the safe direction
    /// still means a parcel that looks fine and is late.
    func testTheWordsMatchTheDays() {
        XCTAssertEqual(due(daysFromToday: -1).label, "OVERDUE")
        XCTAssertTrue(due(daysFromToday: -1).urgent)
        XCTAssertEqual(due(daysFromToday: 0).label, "TODAY")
        XCTAssertTrue(due(daysFromToday: 0).urgent)
        XCTAssertEqual(due(daysFromToday: 1).label, "TOMORROW")
        // Tomorrow is not urgent: it is the difference between a thing to
        // do and a thing to drop what you are doing for, and a queue that
        // shouts about everything says nothing.
        XCTAssertFalse(due(daysFromToday: 1).urgent)
        XCTAssertEqual(due(daysFromToday: 3).label, "3 DAYS")
    }

    /// This must agree with `due()` in app.js. Two clients disagreeing
    /// about the same parcel - "TODAY" on the phone and "0 DAYS" in the
    /// browser - is a bug she would have to notice herself, and the two
    /// implementations are the reason it can happen at all.
    func testTheVocabularyIsTheSameAsThePWA() {
        let words = Set([-5, -1, 0, 1, 2, 9].map { due(daysFromToday: $0).label })
        XCTAssertTrue(words.isSubset(of: ["OVERDUE", "TODAY", "TOMORROW",
                                          "2 DAYS", "9 DAYS"]))
    }

    /// Missing dates are ordinary here: on real mail plenty of fields
    /// parse as NULL, and a local pickup has no ship-by at all.
    func testNoDateIsADashRatherThanACrash() {
        XCTAssertEqual(Due(shipBy: nil).label, "—")
        XCTAssertEqual(Due(shipBy: "").label, "—")
        XCTAssertFalse(Due(shipBy: nil).urgent)
        // Something unparseable is shown as itself rather than swallowed:
        // seeing the raw value is how you find out the format moved.
        XCTAssertEqual(Due(shipBy: "next tuesday").label, "next tuesday")
    }
}

final class MoneyTests: XCTestCase {
    func testMoneyIsDollarsAndMissingIsADash() {
        XCTAssertEqual(money(28), "$28.00")
        XCTAssertEqual(money(28.5), "$28.50")
        // Not "$0.00". A missing price is unknown, and printing zero
        // would make an unpriced listing look free.
        XCTAssertEqual(money(nil), "—")
    }
}

final class SettingsTests: XCTestCase {

    override func setUp() {
        super.setUp()
        Settings.serverURL = nil
    }

    override func tearDown() {
        Settings.serverURL = nil
        super.tearDown()
    }

    /// Both of these are typed wrong often enough to be worth handling,
    /// and both fail confusingly rather than obviously: no scheme gives
    /// a URL that will not build, and a trailing slash gives
    /// `//api/v1/orders` on some hosts.
    func testTheAddressIsNormalisedOnTheWayIn() {
        Settings.serverURL = "mplabel.example.com"
        XCTAssertEqual(Settings.serverURL, "https://mplabel.example.com")

        Settings.serverURL = "https://mplabel.example.com/"
        XCTAssertEqual(Settings.serverURL, "https://mplabel.example.com")

        // http is left alone rather than upgraded: a LAN address during
        // development is a real case, and silently rewriting it would be
        // a connection failure with no explanation.
        Settings.serverURL = "http://10.0.2.250:8080"
        XCTAssertEqual(Settings.serverURL, "http://10.0.2.250:8080")

        Settings.serverURL = "   "
        XCTAssertNil(Settings.serverURL)
    }
}

final class KeychainTests: XCTestCase {

    override func tearDown() {
        Keychain.token = nil
        super.tearDown()
    }

    /// The token is a bearer credential for a service holding customers'
    /// names and home addresses. It is in the keychain rather than
    /// UserDefaults for that reason, and a round trip that silently
    /// failed would log her out on every launch.
    func testTheTokenSurvivesARoundTrip() {
        Keychain.token = "abc.def"
        XCTAssertEqual(Keychain.token, "abc.def")

        // Overwriting must replace rather than accumulate: the setter
        // deletes before adding, and without that SecItemAdd returns
        // errSecDuplicateItem and the old token stays.
        Keychain.token = "ghi.jkl"
        XCTAssertEqual(Keychain.token, "ghi.jkl")

        Keychain.token = nil
        XCTAssertNil(Keychain.token)
    }
}
