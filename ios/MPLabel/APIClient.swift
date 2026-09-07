//  APIClient.swift
//
//  One place that knows how to talk to `mplabel serve`.
//
//  Three things this has to get right, all of them learned on the server
//  side already:
//
//  * `/api/v1`. The versioned prefix exists precisely because the PWA
//    ships with the server and can change in the same commit as a route,
//    where an app on a phone cannot. Never call the unversioned path.
//  * `Authorization: Bearer`. The session token was always a stateless
//    signed token; a cookie was just how a browser carried one. Cookie
//    handling outside a browser is the sort of thing that works until it
//    silently does not.
//  * `X-Mplabel: 1` on mutating requests, or the server answers 400
//    "missing X-Mplabel header".

import Foundation
import UIKit

actor APIClient {
    static let shared = APIClient()

    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    // MARK: - where and who

    /// The host is configuration, not a constant: it is loopback in
    /// development, a tunnel hostname in the house, and it will move
    /// again when the order side goes to the cluster. Baking it in would
    /// mean a rebuild to follow a DNS change.
    private var baseURL: URL? {
        guard let s = Settings.serverURL, let u = URL(string: s) else { return nil }
        return u
    }

    private func request(_ path: String,
                         method: String = "GET",
                         body: (any Encodable)? = nil,
                         authorised: Bool = true) throws -> URLRequest {
        guard let base = baseURL else { throw APIError.noServerConfigured }
        guard let url = URL(string: "/api/v1" + path, relativeTo: base) else {
            throw APIError.badPath(path)
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        // 20s rather than the 60s default. Every call here is a small
        // query against SQLite on a Pi; a minute of a spinner tells her
        // nothing she cannot learn in twenty seconds.
        req.timeoutInterval = 20
        req.setValue("1", forHTTPHeaderField: "X-Mplabel")
        if authorised, let token = Keychain.token {
            req.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.httpBody = try JSONEncoder().encode(AnyEncodable(body))
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    private func send<T: Decodable>(_ req: URLRequest, as: T.Type) async throws -> T {
        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("no HTTP response")
        }
        if http.statusCode == 401 {
            // The token has expired or the password changed - both mean
            // the same thing to her, and both are fixed the same way.
            // Clear it here so the next call cannot loop, and let the UI
            // hear about it rather than reaching into a @MainActor type
            // from inside this actor.
            Keychain.token = nil
            NotificationCenter.default.post(name: .mplabelSignedOut, object: nil)
            throw APIError.unauthorised
        }
        guard (200..<300).contains(http.statusCode) else {
            // The server's refusals are written to be read by a person.
            // Prefer its sentence over anything invented here.
            if let e = try? JSONDecoder().decode(ServerError.self, from: data) {
                throw APIError.server(e.error, http.statusCode)
            }
            throw APIError.server("HTTP \(http.statusCode)", http.statusCode)
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(String(describing: error))
        }
    }

    // MARK: - session

    func logIn(password: String) async throws {
        struct Body: Encodable { let password: String }
        let req = try request("/login", method: "POST",
                              body: Body(password: password),
                              authorised: false)
        let out = try await send(req, as: LoginResponse.self)
        Keychain.token = out.token
    }

    /// Cheap authenticated call, used to decide whether a stored token is
    /// still good before showing her a screen that will 401.
    func checkSession() async -> Bool {
        guard Keychain.token != nil else { return false }
        guard let req = try? request("/orders") else { return false }
        return (try? await send(req, as: OrdersResponse.self)) != nil
    }

    // MARK: - orders

    func orders() async throws -> [Order] {
        try await send(request("/orders"), as: OrdersResponse.self).orders
    }

    func pending() async throws -> [Order] {
        try await send(request("/pending"), as: PendingResponse.self).pending
    }

    func order(_ id: Int) async throws -> OrderDetail {
        try await send(request("/orders/\(id)"), as: OrderDetail.self)
    }

    func markShipped(_ id: Int) async throws {
        _ = try await send(request("/orders/\(id)/ship", method: "POST"),
                           as: EmptyResponse.self)
    }

    func unship(_ id: Int) async throws {
        _ = try await send(request("/orders/\(id)/unship", method: "POST"),
                           as: EmptyResponse.self)
    }

    /// Reprint. Note the server refuses if the archived label no longer
    /// matches the sale it is filed against - that check is the backstop
    /// between a reprint and a parcel posted to a stranger, so surface
    /// its message rather than retrying.
    func printLabel(_ id: Int) async throws -> String? {
        struct Out: Decodable { let code: String? }
        return try await send(request("/orders/\(id)/print", method: "POST"),
                              as: Out.self).code
    }

    /// Batch reprint. `dryRun` asks what would happen and uses no
    /// labels; without `force` the server drops anything already
    /// printed, which is what makes a second tap safe after the first
    /// one timed out.
    func printPending(ids: [Int], dryRun: Bool) async throws -> BatchResult {
        struct Body: Encodable {
            let ids: [Int]
            let dryRun: Bool
            enum CodingKeys: String, CodingKey {
                case ids
                case dryRun = "dry_run"
            }
        }
        return try await send(request("/print/pending", method: "POST",
                                      body: Body(ids: ids, dryRun: dryRun)),
                              as: BatchResult.self)
    }

    // MARK: - the shelf

    func inventory(query: String = "") async throws -> [InventoryItem] {
        var path = "/inventory"
        if !query.isEmpty {
            let q = query.addingPercentEncoding(
                withAllowedCharacters: .urlQueryAllowed) ?? ""
            path += "?q=" + q
        }
        return try await send(request(path), as: InventoryResponse.self).items
    }

    func item(_ id: Int) async throws -> InventoryItem {
        try await send(request("/inventory/\(id)"), as: ItemResponse.self).item
    }

    func bins() async throws -> [Bin] {
        try await send(request("/bins"), as: BinsResponse.self).bins
    }

    func binContents(_ codeOrName: String) async throws -> BinContents {
        let e = codeOrName.addingPercentEncoding(
            withAllowedCharacters: .urlPathAllowed) ?? codeOrName
        return try await send(request("/bins/\(e)"), as: BinContents.self)
    }

    func makeBin(name: String) async throws -> Bin {
        struct Body: Encodable { let name: String }
        return try await send(request("/bins", method: "POST",
                                      body: Body(name: name)),
                              as: BinResponse.self).bin
    }

    /// An empty `bin` takes the thing off the shelf. That is a real
    /// answer - it is what an item in her hand is, on its way somewhere -
    /// so there is no separate delete.
    func move(item id: Int, toBin code: String) async throws {
        struct Body: Encodable { let bin: String }
        _ = try await send(request("/inventory/\(id)/bin", method: "POST",
                                   body: Body(bin: code)),
                           as: EmptyResponse.self)
    }

    /// What has sold, newest first. A separate route from `/inventory`
    /// because it carries `days_to_sell`, which the shelf does not need
    /// and this screen is largely about.
    func sold() async throws -> [InventoryItem] {
        try await send(request("/sold"), as: InventoryResponse.self).items
    }

    /// Note this is not cheap on the Pi: `h_stats` rebuilds the derived
    /// listing picture on every request. Fine to pull to refresh, wrong
    /// to poll.
    func stats() async throws -> Stats {
        try await send(request("/stats"), as: Stats.self)
    }

    /// Correct what the email parser got wrong.
    ///
    /// The server allow-lists the columns, so this sends only what the
    /// screen can edit and never names a column the request invented.
    /// Every argument is optional and an omitted one is left alone -
    /// `nil` here means "do not touch", which is why clearing a value
    /// is done by sending an empty string rather than by omitting it.
    func correct(order id: Int, item: String? = nil, buyer: String? = nil,
                 price: String? = nil, shipBy: String? = nil,
                 notes: String? = nil) async throws -> OrderDetail {
        struct Body: Encodable {
            let item: String?
            let buyer: String?
            let price: String?
            let ship_by: String?
            let notes: String?
        }
        return try await send(
            request("/orders/\(id)/fields", method: "POST",
                    body: Body(item: item, buyer: buyer, price: price,
                               ship_by: shipBy, notes: notes)),
            as: OrderDetail.self)
    }

    /// The archived label, as it was filed. Fetched rather than linked:
    /// it needs the bearer token, and a `Link` cannot carry one.
    ///
    /// A 404 here is an ordinary answer, not a fault - a local pickup
    /// sale never had a label, and a row can outlive its file.
    func labelPDF(_ id: Int) async throws -> Data {
        let (data, response) = try await session.data(
            for: request("/orders/\(id)/label"))
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("no HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError.server(
                http.statusCode == 404
                    ? "There is no label file for this order."
                    : "The label could not be loaded.",
                http.statusCode)
        }
        return data
    }

    /// Ask the Pi to tell this phone things.
    ///
    /// The environment travels with the token because a sandbox token is
    /// meaningless to the production APNs host and vice versa - and the
    /// rejection reads like a malformed token rather than one addressed
    /// to the wrong Apple.
    func registerDevice(token: String, environment: String) async throws {
        struct Body: Encodable {
            let token: String
            let environment: String
            let label: String
        }
        _ = try await send(
            request("/devices", method: "POST",
                    body: Body(token: token, environment: environment,
                               label: UIDevice.current.name)),
            as: EmptyResponse.self)
    }

    // MARK: - the sourcing half

    func trips() async throws -> [Trip] {
        try await send(request("/trips"), as: TripsResponse.self).trips
    }

    func trip(_ id: Int) async throws -> TripDetail {
        try await send(request("/trips/\(id)"), as: TripDetail.self)
    }

    func makeTrip(store: String, receiptTotal: Double? = nil,
                  occurredAt: String? = nil) async throws -> Trip {
        struct Body: Encodable {
            let store: String
            let receipt_total: Double?
            let occurred_at: String?
        }
        return try await send(
            request("/trips", method: "POST",
                    body: Body(store: store, receipt_total: receiptTotal,
                               occurred_at: occurredAt)),
            as: TripResponse.self).trip
    }

    /// The triage pile: captures that are not about anything yet.
    func untriaged() async throws -> [Photo] {
        try await send(request("/photos"), as: PhotosResponse.self).photos
    }

    /// One photograph, as raw bytes with a real Content-Type.
    ///
    /// Not multipart: there is one file and no other fields, so the trip
    /// rides in the query string and the body is the image. The server
    /// keys the row on the sha256 of exactly these bytes, which is what
    /// makes a retry from a shop with one bar of signal safe - the same
    /// photo twice is one row, not two receipts in the pile.
    func uploadPhoto(_ data: Data, contentType: String = "image/jpeg",
                     trip: Int? = nil) async throws -> Photo {
        var path = "/photos"
        if let trip { path += "?trip=\(trip)" }
        var req = try request(path, method: "POST")
        req.httpBody = data
        req.setValue(contentType, forHTTPHeaderField: "Content-Type")
        // A photo over a house wifi is not a small SQLite query, so the
        // 20 seconds the rest of this client uses is the wrong number.
        req.timeoutInterval = 120
        return try await send(req, as: PhotoResponse.self).photo
    }

    /// The bytes, fetched rather than handed to `AsyncImage`, which
    /// cannot carry the bearer token.
    func photoData(_ id: Int) async throws -> Data {
        let (data, response) = try await session.data(for: request("/photos/\(id)"))
        guard let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode) else {
            throw APIError.server("the photo could not be loaded",
                                  (response as? HTTPURLResponse)?.statusCode ?? 0)
        }
        return data
    }

    func attach(photo id: Int, toItem item: Int) async throws {
        struct Body: Encodable { let listing: Int }
        _ = try await send(request("/photos/\(id)/attach", method: "POST",
                                   body: Body(listing: item)),
                           as: EmptyResponse.self)
    }

    /// File a receipt against the run it records. This is what takes a
    /// capture out of the triage pile - a receipt is never about one
    /// listing, it is the record of a trip several of whose items it
    /// paid for.
    func attach(photo id: Int, toTrip trip: Int) async throws {
        struct Body: Encodable { let trip: Int }
        _ = try await send(request("/photos/\(id)/attach", method: "POST",
                                   body: Body(trip: trip)),
                           as: EmptyResponse.self)
    }

    /// Add something by hand: a local pickup, which produces no label
    /// email at all, or a thing off a shelf being listed for the first
    /// time.
    func makeItem(title: String, paid: Double? = nil, price: Double? = nil,
                  era: String? = nil, condition: String? = nil,
                  category: String? = nil, bin: String? = nil,
                  trip: Int? = nil,
                  photos: [Int] = []) async throws -> InventoryItem {
        struct Body: Encodable {
            let title: String
            let paid: Double?
            let price: Double?
            let era: String?
            let condition: String?
            let category: String?
            let bin: String?
            let trip: Int?
            let photos: [Int]
        }
        return try await send(
            request("/inventory", method: "POST",
                    body: Body(title: title, paid: paid, price: price,
                               era: era, condition: condition,
                               category: category, bin: bin, trip: trip,
                               photos: photos)),
            as: ItemResponse.self).item
    }

    /// What one object cost. `nil` clears it back to unknown, which is a
    /// real answer and not the same as zero.
    func setCost(item id: Int, paid: Double?) async throws -> InventoryItem {
        struct Body: Encodable { let paid: Double? }
        return try await send(request("/inventory/\(id)/fields",
                                      method: "POST", body: Body(paid: paid)),
                              as: ItemResponse.self).item
    }

    // MARK: - scanning

    func lookUp(code: String) async throws -> Lookup {
        let e = code.uppercased().addingPercentEncoding(
            withAllowedCharacters: .urlPathAllowed) ?? code
        return try await send(request("/lookup/\(e)"), as: Lookup.self)
    }
}

// MARK: - plumbing

/// The server answers mutations with `{"ok": true, ...}` and varying
/// extra fields. Nothing here needs them, and decoding into a shape that
/// ignores them means a new field server-side is not a client crash.
struct EmptyResponse: Decodable {}

enum APIError: LocalizedError {
    case noServerConfigured
    case badPath(String)
    case unauthorised
    case transport(String)
    case server(String, Int)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .noServerConfigured:
            return "No server set yet. Add the address in Settings."
        case .badPath(let p):
            return "Bad path \(p)"
        case .unauthorised:
            return "Signed out. Sign in again."
        case .transport(let m):
            return m
        case .server(let m, _):
            return m
        case .decoding(let m):
            // Shown rather than swallowed: the client and server ship
            // separately now, so a shape change is a real possibility and
            // "something went wrong" would hide exactly the detail that
            // identifies it.
            return "The server sent something unexpected. \(m)"
        }
    }
}

/// `any Encodable` cannot be handed to JSONEncoder directly.
private struct AnyEncodable: Encodable {
    private let encodeIt: (Encoder) throws -> Void
    init(_ wrapped: any Encodable) {
        encodeIt = { try wrapped.encode(to: $0) }
    }
    func encode(to encoder: Encoder) throws { try encodeIt(encoder) }
}
