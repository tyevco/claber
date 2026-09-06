//  Models.swift
//
//  Mirrors of the JSON `web.py` actually returns. Written against
//  `_order_row`, `_order_detail`, `h_inventory`, `h_item` and `h_bins`
//  rather than against the design, because the server is the thing that
//  will be running.
//
//  Almost every field is Optional on purpose. The database is filled
//  from Facebook emails and a saved-page import, and CLAUDE.md is blunt
//  about how much of it parses as NULL on real mail - listing_id and
//  order_id were 0 of 18. A non-optional here is not a stricter model,
//  it is a decode failure that empties the whole screen.

import Foundation

// MARK: - orders

struct Order: Codable, Identifiable, Hashable {
    let id: Int
    let code: String?
    let item: String?
    /// First name only in the queue payload - `_order_row` strips the
    /// rest deliberately, so a list on a kitchen counter carries less
    /// than the one screen that needs it.
    let buyer: String?
    let price: Double?
    let shipBy: String?
    let status: String?
    let printed: Bool
    /// Not the same question as `printed`. A local-pickup sale has no
    /// label file and never will; a recorded-but-unprinted one has a
    /// file waiting. One is a state, the other is a job.
    let hasLabel: Bool
    let notes: String?

    enum CodingKeys: String, CodingKey {
        case id, code, item, buyer, price, status, printed, notes
        case shipBy = "ship_by"
        case hasLabel = "has_label"
    }
}

struct OrderDetail: Codable, Identifiable, Hashable {
    let id: Int
    let code: String?
    let item: String?
    let buyer: String?
    let price: Double?
    let shipBy: String?
    let status: String?
    let printed: Bool
    let hasLabel: Bool
    let notes: String?
    let orderId: String?
    let listingId: String?
    let receivedAt: String?
    let tracking: String?
    /// The buyer's home address. The one field in this app that is
    /// genuinely sensitive, which is why the queue payload has no such
    /// field at all and this arrives only when a screen asks for it.
    let shipTo: String?
    let weight: String?
    let service: String?
    let printedAt: String?
    let printCount: Int?

    enum CodingKeys: String, CodingKey {
        case id, code, item, buyer, price, status, printed, notes
        case tracking, weight, service
        case shipBy = "ship_by"
        case hasLabel = "has_label"
        case orderId = "order_id"
        case listingId = "listing_id"
        case receivedAt = "received_at"
        case shipTo = "ship_to"
        case printedAt = "printed_at"
        case printCount = "print_count"
    }
}

// MARK: - the shelf

struct InventoryItem: Codable, Identifiable, Hashable {
    let id: Int
    let listingId: String?
    let title: String?
    let price: Double?
    let state: String?
    let category: String?
    let inventoryCode: String?
    /// The bin's three-character code - what a tag carries.
    let binCode: String?
    /// The bin's *name*, joined server-side. This is what a person
    /// reads; showing the code instead would be showing her the phone's
    /// own homework.
    let bin: String?
    let listedAt: String?
    let soldAt: String?
    /// Only `h_item` sends this; the list rows do not.
    let binMates: Int?

    enum CodingKeys: String, CodingKey {
        case id, title, price, state, category, bin
        case listingId = "listing_id"
        case inventoryCode = "inventory_code"
        case binCode = "bin_code"
        case listedAt = "listed_at"
        case soldAt = "sold_at"
        case binMates = "bin_mates"
    }
}

struct Bin: Codable, Identifiable, Hashable {
    /// Three characters, minted server-side and never reused - the tag
    /// on the shelf outlives the row, so this is the stable identity
    /// and `name` is not.
    let code: String
    let name: String
    let notes: String?
    let createdAt: String?
    /// Absent from `create_bin`'s response, present in the list.
    let count: Int?

    var id: String { code }

    enum CodingKeys: String, CodingKey {
        case code, name, notes, count
        case createdAt = "created_at"
    }
}

struct BinContents: Codable {
    let bin: Bin
    let items: [InventoryItem]
}

// MARK: - scanning

/// What `/api/lookup/{code}` says a scanned code names. Three characters
/// is a parcel, four is a thing on a shelf - and the server checks sales
/// first, because a code currently on a box waiting to go out is the
/// more urgent reading.
/// Hashable and Identifiable are declared *here*, on the enum itself,
/// and not in an extension next to the screen that uses them. Swift only
/// synthesises `==` and `hash(into:)` for an enum with associated values
/// in the file that declares the enum - an extension anywhere else
/// compiles as a demand to write both by hand.
///
/// The synthesis then works only because `OrderDetail` and
/// `InventoryItem` are both Hashable. Drop it from either and the error
/// lands here rather than on them.
enum Lookup: Hashable, Identifiable {
    case sale(OrderDetail)
    case listing(InventoryItem)

    /// `navigationDestination(item:)` wants this.
    var id: String {
        switch self {
        case .sale(let d):     return "sale-\(d.id)"
        case .listing(let it): return "listing-\(it.id)"
        }
    }
}

extension Lookup: Decodable {
    private enum Keys: String, CodingKey { case kind, detail, listing }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        switch try c.decode(String.self, forKey: .kind) {
        case "sale":
            self = .sale(try c.decode(OrderDetail.self, forKey: .detail))
        case "listing":
            self = .listing(try c.decode(InventoryItem.self, forKey: .listing))
        default:
            throw DecodingError.dataCorruptedError(
                forKey: .kind, in: c,
                debugDescription: "unknown lookup kind")
        }
    }
}

// MARK: - envelopes

/// `/print/pending` answers in one of two shapes depending on
/// `dry_run`, and never both. Optionals rather than two types, because
/// the caller already knows which it asked for.
struct BatchResult: Codable {
    let printed: [Order]?
    let failed: [BatchFailure]?
    let wouldPrint: [Order]?

    enum CodingKeys: String, CodingKey {
        case printed, failed
        case wouldPrint = "would_print"
    }
}

/// Failures are per row: one bad label must not abandon the rest of the
/// batch, which is the whole reason the Pending screen exists.
struct BatchFailure: Codable, Identifiable {
    let id: Int
    let error: String
}

struct OrdersResponse: Codable { let orders: [Order] }
struct PendingResponse: Codable { let pending: [Order] }
struct InventoryResponse: Codable { let items: [InventoryItem]; let count: Int }
struct ItemResponse: Codable { let item: InventoryItem }
struct BinsResponse: Codable { let bins: [Bin] }
struct BinResponse: Codable { let bin: Bin }
struct LoginResponse: Codable {
    let ok: Bool
    let token: String
    let expiresIn: Int

    enum CodingKeys: String, CodingKey {
        case ok, token
        case expiresIn = "expires_in"
    }
}

/// Every refusal from the server is `{"error": "a sentence"}`, and the
/// sentences are written to be shown to a person - "there is already a
/// bin called ATTIC", "no bin FLOOR. Make it first". Show them rather
/// than replacing them with something vaguer.
struct ServerError: Codable, LocalizedError {
    let error: String
    var errorDescription: String? { error }
}
