//  ModelDecodingTests.swift
//
//  Decode every fixture in Fixtures/ into the model that screen uses.
//
//  These are the tests the two real bugs so far would have failed. Both
//  were the same shape - the Swift models disagreeing with what the
//  server actually sends - and neither was catchable in Swift, because
//  there was nothing on this side that had ever seen a real payload:
//
//  * `/lookup` answered without the row id, so a scanned code decoded
//    into nothing. It reached a phone as "Key id not found in key
//    decoding container", after a label had been printed and pointed at.
//  * Five CodingKeys were validated against payloads that were empty
//    lists, which is a test passing because it has nothing to disagree
//    with.
//
//  The fixtures are not hand-written, which is the whole point: a JSON
//  file typed out here would carry the same belief as the model it is
//  checking. `tests/make_ios_fixtures.py` starts a real server against a
//  real database and writes what comes back, and a pytest test fails if
//  the server drifts away from the committed copies.

import XCTest
@testable import MPLabel

final class ModelDecodingTests: XCTestCase {

    /// Fixtures are a folder reference in the test bundle.
    private func fixture(_ name: String) throws -> Data {
        guard let url = Bundle(for: type(of: self))
            .url(forResource: name, withExtension: "json",
                 subdirectory: "Fixtures")
            ?? Bundle(for: type(of: self))
                .url(forResource: name, withExtension: "json") else {
            XCTFail("fixture \(name).json is not in the test bundle - check "
                    + "Fixtures/ is added as a folder reference")
            throw CocoaError(.fileNoSuchFile)
        }
        return try Data(contentsOf: url)
    }

    private func decode<T: Decodable>(_ type: T.Type,
                                      from name: String) throws -> T {
        try JSONDecoder().decode(type, from: try fixture(name))
    }

    // MARK: - orders

    func testOrdersDecode() throws {
        let out = try decode(OrdersResponse.self, from: "orders")
        XCTAssertEqual(out.orders.count, 2)

        // The awkward row is the point: a local pickup has no tracking,
        // no ship_by and no label file, and it must decode rather than
        // taking the screen down with it.
        let pickup = try XCTUnwrap(out.orders.first { $0.code == "B4M" })
        XCTAssertFalse(pickup.hasLabel)
        XCTAssertNil(pickup.shipBy)
        XCTAssertNil(pickup.buyer)

        let posted = try XCTUnwrap(out.orders.first { $0.code == "7QK" })
        XCTAssertTrue(posted.hasLabel)
        // First name only in the queue payload - the surname is stripped
        // server-side and this asserts the client is not expecting it.
        XCTAssertEqual(posted.buyer, "Sam")
    }

    func testOrderDetailDecodesAndCarriesTheAddress() throws {
        let d = try decode(OrderDetail.self, from: "order")
        XCTAssertEqual(d.code, "7QK")
        XCTAssertNotNil(d.shipTo, "the detail payload is the only one with "
                        + "an address, and this screen needs it")
        XCTAssertNotNil(d.tracking)
    }

    func testPendingDecodes() throws {
        _ = try decode(PendingResponse.self, from: "pending")
    }

    func testBatchResultDecodes() throws {
        let out = try decode(BatchResult.self, from: "batch")
        // A dry run answers with would_print and no printed key at all.
        XCTAssertNotNil(out.wouldPrint)
        XCTAssertNil(out.printed)
    }

    // MARK: - the shelf

    func testInventoryDecodes() throws {
        let out = try decode(InventoryResponse.self, from: "inventory")
        XCTAssertFalse(out.items.isEmpty)
        // The bin *name* is joined server-side; a client holding only a
        // code would have to resolve it with a second request.
        let binned = out.items.first { $0.binCode != nil }
        XCTAssertEqual(binned?.bin, "ATTIC")
    }

    func testItemDecodesWithBinMates() throws {
        let out = try decode(ItemResponse.self, from: "item")
        XCTAssertNotNil(out.item.binMates,
                        "the item payload carries bin_mates; the list rows "
                        + "do not, which is why it is Optional")
    }

    func testBinsAndContentsDecode() throws {
        let bins = try decode(BinsResponse.self, from: "bins")
        let attic = try XCTUnwrap(bins.bins.first { $0.name == "ATTIC" })
        XCTAssertEqual(attic.code.count, 3)

        let contents = try decode(BinContents.self, from: "bin")
        XCTAssertEqual(contents.bin.name, "ATTIC")
    }

    // MARK: - sold and stats

    func testSoldCarriesDaysToSellAndToleratesItsAbsence() throws {
        let out = try decode(InventoryResponse.self, from: "sold")
        let days = out.items.map(\.daysToSell)
        XCTAssertTrue(days.contains(28))
        // The saved-page import carried no dates, so plenty of sold rows
        // have no answer. Nil, not zero - zero would read as "sold the
        // same day", which is a different and flattering claim.
        XCTAssertTrue(days.contains(where: { $0 == nil }))
    }

    func testStatsDecode() throws {
        let s = try decode(Stats.self, from: "stats")
        XCTAssertFalse(s.priceBands.isEmpty)
        // Aggregates over a table full of nulls: every number here is
        // Optional because AVG of nothing is nothing.
        XCTAssertNoThrow(s.monthly.first?.avgOrder as Any)
    }

    // MARK: - scanning

    func testLookupOfAnInventoryCodeCanBeOpened() throws {
        let found = try decode(Lookup.self, from: "lookup_listing")
        guard case .listing(let item) = found else {
            return XCTFail("a four-character code is a thing on a shelf")
        }
        // The bug this file exists for: without an id the app identifies
        // an item it cannot open.
        XCTAssertGreaterThan(item.id, 0)
        XCTAssertEqual(item.inventoryCode, "7K2M")
    }

    func testLookupOfAParcelCodeIsASale() throws {
        let found = try decode(Lookup.self, from: "lookup_sale")
        guard case .sale(let sale) = found else {
            return XCTFail("a three-character code is a parcel, and sales "
                           + "are searched first because a box waiting to "
                           + "go out is the more urgent reading")
        }
        XCTAssertEqual(sale.code, "7QK")
    }

    func testLoginResponseDecodes() throws {
        let out = try decode(LoginResponse.self, from: "login")
        XCTAssertTrue(out.ok)
        XCTAssertGreaterThan(out.expiresIn, 0)
    }

    // MARK: - refusals

    func testAServerRefusalDecodesIntoSomethingShowable() throws {
        // The server writes its refusals as sentences meant for a person
        // - "there is already a bin called ATTIC" - and the app shows
        // them verbatim rather than replacing them with something vaguer.
        let raw = #"{"error": "no bin FLOOR. Make it first"}"#.data(using: .utf8)!
        let err = try JSONDecoder().decode(ServerError.self, from: raw)
        XCTAssertEqual(err.errorDescription, "no bin FLOOR. Make it first")
    }

    // MARK: - the sourcing half

    func testATripCarriesTheMoneySummaryTheScreenNeeds() throws {
        let out = try decode(TripsResponse.self, from: "trips")
        let trip = try XCTUnwrap(out.trips.first)
        XCTAssertEqual(trip.store, "GOODWILL 214")
        XCTAssertEqual(trip.receiptTotal, 21.40)
        XCTAssertEqual(trip.assigned, 18.0)
        // What the till said minus what has been attributed. Not derived
        // on this side: the server owns the arithmetic so the phone and
        // the CLI cannot disagree about it.
        XCTAssertEqual(trip.unassigned, 3.40)
    }

    /// The distinction the whole triage screen turns on. Null means
    /// nobody wrote the till total down; zero means every penny is
    /// attributed. Decoding null into 0 would make the second sentence
    /// appear over the first situation, which is a screen telling her
    /// the job is finished when it has not started.
    func testUnassignedNullIsNotZero() throws {
        let json = Data("""
        {"id": 9, "store": "ESTATE SALE", "occurred_at": null,
         "receipt_total": null, "notes": null, "assigned": null,
         "listed_for": null, "unassigned": null, "items": 0}
        """.utf8)
        let trip = try JSONDecoder().decode(Trip.self, from: json)
        XCTAssertNil(trip.unassigned)
        XCTAssertNotEqual(trip.unassigned, 0)
    }

    func testATripsItemsCarryWhatTheyCost() throws {
        let out = try decode(TripDetail.self, from: "trip")
        XCTAssertEqual(out.items.count, 2)
        let vase = try XCTUnwrap(out.items.first {
            $0.title == "Hobnail milk glass vase" })
        XCTAssertEqual(vase.paid, 6.0)
        XCTAssertEqual(vase.price, 28.0)
        // The reduced row the trip payload sends omits most of what an
        // InventoryItem can carry; every one of those has to be optional
        // or the screen decodes into nothing.
        XCTAssertNil(vase.listingId)
    }

    func testThePileIsCapturesThatAreAboutNothing() throws {
        let out = try decode(PhotosResponse.self, from: "photos")
        let shot = try XCTUnwrap(out.photos.first)
        XCTAssertNil(shot.itemID)
        XCTAssertNil(shot.tripId)
        XCTAssertFalse(shot.isTriaged)
        XCTAssertEqual(shot.sha256, "deadbeef")
    }

    /// `photos.listing_id` is the listings *row id*, an integer, while
    /// `InventoryItem.listingId` is Facebook's string id for the same
    /// table. One name, two types, and decoding one as the other is a
    /// crash rather than a wrong number - so they are named apart on
    /// this side.
    func testAPhotosItemReferenceIsTheRowIdNotFacebooks() throws {
        let json = Data("""
        {"id": 3, "path": "photos/x.jpg", "sha256": null, "taken_at": null,
         "created_at": null, "listing_id": 7, "trip_id": null}
        """.utf8)
        let photo = try JSONDecoder().decode(Photo.self, from: json)
        XCTAssertEqual(photo.itemID, 7)
        XCTAssertTrue(photo.isTriaged)
    }

    /// Postage without its provenance is an estimate that the next screen
    /// reads as a fact - and no email carries the real charge, so almost
    /// every parcel starts with nobody knowing.
    func testPostageCarriesWhereItCameFrom() throws {
        let json = Data("""
        {"id": 1, "code": "7QK", "printed": false, "has_label": true,
         "postage": 12.40, "postage_source": "estimated", "kept": 82.60}
        """.utf8)
        let estimated = try JSONDecoder().decode(OrderDetail.self, from: json)
        XCTAssertEqual(estimated.postage, 12.40)
        XCTAssertFalse(estimated.postageIsMeasured,
                       "an estimate must not read as measured")

        let unknown = try decode(OrderDetail.self, from: "order")
        XCTAssertNil(unknown.postage)
        XCTAssertNil(unknown.kept, "no postage means no answer, not the "
                     + "whole price")
        XCTAssertFalse(unknown.postageIsMeasured)
    }

    /// The sentence under the numbers has to follow the data. It said
    /// "there is no cost basis yet" for as long as that was true and
    /// then went on saying it, which is a screen telling her the
    /// opposite of the truth.
    func testTheProfitCaveatFollowsHowMuchIsCosted() throws {
        let none = CostCoverage(sold: 9, costed: 0, margin: nil)
        XCTAssertTrue(none.sentence.contains("takings and not profit"))

        let some = CostCoverage(sold: 9, costed: 2, margin: 40)
        XCTAssertTrue(some.sentence.contains("2 of 9"),
                      "the fraction is what decides how much to trust it")

        let all = CostCoverage(sold: 9, costed: 9, margin: 400)
        XCTAssertTrue(all.all)
        XCTAssertTrue(all.sentence.contains("Every sold item"))

        // Every state says something, including the empty one.
        for state in [none, some, all,
                      CostCoverage(sold: 0, costed: 0, margin: nil)] {
            XCTAssertFalse(state.sentence.isEmpty)
            // And none of them claims a figure it does not have.
            XCTAssertTrue(state.sentence.contains("fee")
                          || state.sentence.contains("cost")
                          || state.sentence.contains("sold"))
        }
    }

    /// Postage and Facebook's fee are in none of these numbers, and the
    /// screen has to keep saying so - the fee has never been confirmed
    /// against a real payout.
    func testTheCaveatNeverClaimsFeesAreCounted() {
        XCTAssertTrue(CostCoverage(sold: 9, costed: 9, margin: 400)
            .sentence.contains("fee"))
        XCTAssertTrue(CostCoverage(sold: 9, costed: 2, margin: 40)
            .sentence.contains("fee"))
    }

    // MARK: - a label from anywhere

    /// The Pi's account of a 4x6 it had never seen before.
    ///
    /// The fixture is a dry run, which is the state most of these fields
    /// exist for: the crop and the rotation are real and nothing was
    /// printed, so it is the answer she is looking at when deciding
    /// whether to spend a label on it.
    func testPrintedLabelDecodes() throws {
        let out = try decode(PrintedLabelResponse.self, from: "print_label")
        XCTAssertEqual(out.label.sizeIn, [4.0, 6.0],
                       "anything else and the raw backends clip it")
        XCTAssertEqual(out.label.rotation, 90)
        XCTAssertEqual(out.label.dryRun, true)
        XCTAssertFalse(out.label.printed)
        XCTAssertEqual(out.label.regionsFound, 1)
        XCTAssertFalse(out.label.job.isEmpty)
    }

    /// `rotation_source` is the difference between an orientation that
    /// was measured off the label's own text and one guessed from its
    /// shape - and the shape knows the label is on its side without
    /// knowing which way up. A guess that decodes as a measurement is an
    /// upside-down label and a wasted one, so the distinction has to
    /// survive the wire rather than being flattened to a number.
    func testAGuessedOrientationIsDistinguishableFromAMeasuredOne() throws {
        let measured = try decode(PrintedLabelResponse.self,
                                  from: "print_label").label
        XCTAssertEqual(measured.rotationSource, "text")
        XCTAssertFalse(measured.wasGuessed)

        // The other two, which the fixture cannot carry: the synthetic
        // label has text on it, so the server has no reason to guess.
        for (source, guessed) in [("aspect", true), ("forced", false)] {
            let json = Data("""
                {"label": {"size_in": [4.0, 6.0], "rotation": 90,
                 "rotation_source": "\(source)", "page": 1,
                 "regions_found": 1, "region": 1, "job": "adhoc-x",
                 "recorded": "printd journal", "printed": true}}
                """.utf8)
            let out = try JSONDecoder().decode(PrintedLabelResponse.self,
                                               from: json)
            XCTAssertEqual(out.label.wasGuessed, guessed)
            // `dry_run` is absent on a real print, not false.
            XCTAssertNil(out.label.dryRun)
        }
    }
}
