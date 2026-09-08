//  LogicTests.swift
//
//  The parts of the app that are decisions rather than drawing, and can
//  therefore be wrong quietly.

import UIKit
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

    // MARK: - the on-device model

    /// The guard the whole feature rests on. `paid` must not be
    /// reachable from anything a model produces: it is the input to every
    /// margin in the analytics, and a hallucinated 4.99 would be
    /// indistinguishable from a real one for ever afterwards. Asserted by
    /// reflection rather than by reading the file, so adding the field
    /// back breaks the build's tests rather than being noticed in review.
    func testTheModelCannotSuggestWhatSomethingCost() throws {
        let suggested = OnDevice.Suggested(title: "", era: "", condition: "",
                                           category: "", estimate: "")
        let fields = Mirror(reflecting: suggested).children.compactMap(\.label)
        XCTAssertFalse(fields.contains("paid"),
                       "a suggested cost would corrupt every margin")
        XCTAssertFalse(fields.contains("price"),
                       "asking price is hers to set, not the model's")
        // `estimate` is the model's own guess at a sale price and is
        // allowed - it is what she might *ask*, offered as a guess and
        // shown apart from the comparables. `paid` is what she hands
        // over, and no model gets an opinion about that.
        XCTAssertEqual(Set(fields),
                       ["title", "era", "condition", "category", "estimate"])
    }

    /// A missing era or condition is **named**, not omitted.
    ///
    /// Measured, and the opposite of what looked right: asked about a
    /// cast iron skillet with nothing said about its condition, three
    /// runs of three invented damage - rust, a bent handle, chipping.
    /// Naming the gap stopped it three of three. An invented chip is a
    /// false statement about an object a buyer is going to unwrap.
    func testThePromptNamesTheGapsThatGetInvented() {
        let sparse = OnDevice.Item(title: "Hobnail milk glass vase", era: "",
                                   condition: "", asking: "")
        let prompt = OnDevice.prompt(for: sparse)
        XCTAssertTrue(prompt.contains("Item: Hobnail milk glass vase"))
        XCTAssertTrue(prompt.contains("Era: not stated"))
        XCTAssertTrue(prompt.contains("Condition: not stated"))
        // Price is the exception: it does not get invented, and offering
        // "not stated" would only invite the model to suggest a number.
        XCTAssertFalse(prompt.contains("Asking price"))

        let full = OnDevice.Item(title: "Oil portrait", era: "c. 1910",
                                 condition: "Craquelure", asking: "145")
        let second = OnDevice.prompt(for: full)
        XCTAssertTrue(second.contains("Era: c. 1910"))
        XCTAssertTrue(second.contains("Condition: Craquelure"))
        XCTAssertTrue(second.contains("Asking price: $145"))
    }

    /// Whitespace is not content. A field holding a space would otherwise
    /// reach the prompt as an empty label.
    /// The draft must not carry the model's acknowledgement of the gap.
    /// She would delete "Condition: not stated" by hand every time, and
    /// asking the model to stay quiet does not work - it echoes the line
    /// back verbatim. So it comes out here, deterministically.
    func testTheDraftLosesTheModelsNoteAboutWhatItWasNotTold() {
        let raw = "Cast iron skillet. Material: cast iron. Condition: not "
                + "stated."
        XCTAssertEqual(OnDevice.tidy(raw),
                       "Cast iron skillet. Material: cast iron.")

        XCTAssertEqual(
            OnDevice.tidy("A vase. No condition details provided."),
            "A vase.")
        // A real condition survives - this must not eat the sentence that
        // matters most in a second-hand listing.
        XCTAssertEqual(
            OnDevice.tidy("A vase. Small chip on the rim."),
            "A vase. Small chip on the rim.")
        // Nothing left is empty, not a stray full stop.
        XCTAssertEqual(OnDevice.tidy("Condition: not stated."), "")
    }

    func testWhitespaceCountsAsNotTyped() {
        let item = OnDevice.Item(title: "A vase", era: "   ", condition: " ",
                                 asking: "")
        XCTAssertTrue(OnDevice.prompt(for: item).contains("Era: not stated"))
        XCTAssertFalse(OnDevice.Item(title: " ", era: "", condition: "",
                                     asking: "").isEmpty == false)
    }

    /// Every unavailable state has to say something a person can act on -
    /// "off in Settings" and "this phone cannot" are different sentences,
    /// and the empty string belongs only to the ready case.
    func testEveryUnavailableStateHasWords() {
        for state: OnDevice.Readiness in [.notEnabled, .notEligible, .warming,
                                          .unavailable("something")] {
            XCTAssertFalse(state.sentence.isEmpty,
                           "\(state) reaches the screen with nothing to say")
            XCTAssertFalse(state.canGenerate)
        }
        XCTAssertTrue(OnDevice.Readiness.ready.canGenerate)
        XCTAssertEqual(OnDevice.Readiness.ready.sentence, "")
    }

    // MARK: - push

    /// Every state has to say something a person can act on. "Not asked
    /// yet" and "she said no" are different sentences and the fix for one
    /// is not the fix for the other.
    @MainActor
    func testEveryPushStateHasWords() {
        let states: [Push.State] = [.unknown, .notAsked, .refused,
                                    .registering, .registered,
                                    .failed("network")]
        for state in states {
            XCTAssertFalse(state.sentence.isEmpty, "\(state) says nothing")
        }
        XCTAssertNotEqual(Push.State.notAsked.sentence,
                          Push.State.refused.sentence,
                          "not asked and refused need different answers")
        XCTAssertTrue(Push.State.failed("network").sentence
            .contains("network"), "the reason has to survive")
    }

    /// A debug build carries the development entitlement and gets a
    /// sandbox token, which the production APNs host rejects with
    /// BadDeviceToken - an error that reads like the token is malformed
    /// rather than addressed to the wrong Apple. So the environment
    /// travels with it.
    @MainActor
    func testTheEnvironmentTravelsWithTheToken() {
        #if DEBUG
        XCTAssertEqual(Push.environment, "sandbox")
        #else
        XCTAssertEqual(Push.environment, "production")
        #endif
    }

    /// An `@available` annotation cannot rescue a symbol that is absent
    /// from the SDK's headers. The image API is in a beta Xcode, the
    /// release runner has whatever ships, and naming the type there does
    /// not compile - so the *build* has a say as well as the phone.
    @MainActor
    func testTheImageApiIsGatedOnTheSdkNotJustTheOs() {
        #if compiler(>=6.4)
        // Built with the beta SDK: the answer is then purely about the
        // phone, and the simulator this runs on is iOS 27.
        if #available(iOS 27.0, *) {
            XCTAssertTrue(OnDevice.canSeePictures)
        } else {
            XCTAssertFalse(OnDevice.canSeePictures)
        }
        #else
        // Built without it: no phone can make this true.
        XCTAssertFalse(OnDevice.canSeePictures,
                       "a build made without the SDK cannot show a picture")
        #endif
    }

    // MARK: - what it is worth

    /// Evidence and a guess are different things and the screen must
    /// keep them apart. `Worth` carries only what her own sold listings
    /// support; the model's number lives on `Suggested` and never gets
    /// added to, averaged with, or shown as the same figure.
    func testWorthCarriesNoGuess() {
        let fields = Mirror(reflecting: Worth(
            comparables: 0, low: nil, high: nil, wide: nil, median: nil,
            typicalDays: nil, usualMargin: nil, payUnder: nil,
            examples: [])).children.compactMap(\.label)
        XCTAssertFalse(fields.contains("estimate"),
                       "the model's guess must not ride in with the comps")
        XCTAssertFalse(fields.contains("guess"))
    }

    /// No comparables is a real answer. Being told "no idea" while
    /// holding a $40 lamp is worth more than a number from nowhere,
    /// because she will act on the number.
    func testNoComparablesIsAnAnswer() {
        let empty = Worth(comparables: 0, low: nil, high: nil, wide: nil,
                          median: nil, typicalDays: nil, usualMargin: nil,
                          payUnder: nil, examples: [])
        XCTAssertFalse(empty.hasEvidence)

        let some = Worth(comparables: 2, low: 28, high: 34, wide: false,
                         median: 31, typicalDays: 12, usualMargin: 0.6,
                         payUnder: 12.4, examples: [])
        XCTAssertTrue(some.hasEvidence)
    }

    /// A ceiling with no margin behind it is a number invented about her
    /// business, so the model has to be able to say it does not have one
    /// while still having comparables.
    func testAPriceRangeCanExistWithoutACeiling() throws {
        let json = Data("""
        {"comparables": 2, "low": 28.0, "high": 34.0, "median": 31.0,
         "typical_days": 12, "usual_margin": null, "pay_under": null,
         "examples": []}
        """.utf8)
        let worth = try JSONDecoder().decode(Worth.self, from: json)
        XCTAssertTrue(worth.hasEvidence)
        XCTAssertNil(worth.payUnder)
        XCTAssertNil(worth.usualMargin)
    }

    /// `@UIApplicationDelegateAdaptor` constructs its own instance of the
    /// type it is given. Pointing it at `Push` made a second `Push` that
    /// received every callback while the screen watched `Push.shared` -
    /// the token arrived and nothing was listening, and Settings sat at
    /// "Registering…" for ever.
    ///
    /// So the delegate is its own type and forwards to the shared one.
    /// Asserted by reflection rather than by reading, because the
    /// symptom of getting this wrong is a hang with no error in it.
    @MainActor
    func testTheDelegateIsNotTheObservedObject() {
        XCTAssertFalse(Push.shared is UIApplicationDelegate,
                       "if Push is the delegate type, the adaptor will "
                       + "build a second one and the screen watches the "
                       + "wrong object")
        XCTAssertTrue(PushDelegate() is UIApplicationDelegate)
    }

    /// A token handed to the shared object moves it off "Registering…".
    /// Without a server it lands on `failed`, which is still an answer -
    /// the state that must never persist is the one with nothing in it.
    @MainActor
    func testAReceivedTokenLeavesTheRegisteringState() async {
        Push.shared.failed(NSError(domain: "test", code: 1, userInfo: [
            NSLocalizedDescriptionKey: "no valid aps-environment"]))
        if case .failed(let why) = Push.shared.state {
            XCTAssertTrue(why.contains("aps-environment"),
                          "Apple's own words survive to the screen")
        } else {
            XCTFail("a failure must be a state, not a silence")
        }
    }
}
