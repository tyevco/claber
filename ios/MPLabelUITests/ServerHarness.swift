//  ServerHarness.swift
//
//  Finds the server a UI test talks to. It does **not** start one.
//
//  The first version of this file tried to, with `Process`, on the
//  premise that a UI test bundle runs on the Mac. It does not: the
//  XCUITest runner is an iOS process running on the simulator beside the
//  app, so `Process` is not even in scope there. That premise was wrong
//  and this is the correction.
//
//  So the server is started by `ios/run-ui-tests.sh`, on the Mac, before
//  xcodebuild is invoked - and its address arrives here as an
//  environment variable. The simulator does share the host's network
//  stack, which is the one part of the original reasoning that held: a
//  `127.0.0.1` URL in the app reaches a server on the Mac.
//
//  It is still the *real* server rather than a mock, which was the point.
//  A Swift stub would answer what we believe web.py answers, and that
//  belief has been wrong twice.

import Foundation
import XCTest

struct ServerHarness {

    let baseURL: String
    let password: String
    let token: String

    /// `xcodebuild test TEST_RUNNER_X=…` sets `X` on the runner process,
    /// prefix removed. `run-ui-tests.sh` passes the three below.
    ///
    /// Absent means someone pressed ⌘U rather than running the script,
    /// which is an ordinary thing to do - so this skips with a sentence
    /// saying what to run instead of failing eight tests with a
    /// connection error.
    static func fromEnvironment() throws -> ServerHarness {
        let env = ProcessInfo.processInfo.environment
        guard let base = env["MPLABEL_UITEST_SERVER"], !base.isEmpty,
              let token = env["MPLABEL_UITEST_TOKEN"], !token.isEmpty else {
            throw XCTSkip("""
                No server for the UI tests. These need one running, and \
                starting it is not something a test bundle on the \
                simulator can do. Run them with:

                    ./ios/run-ui-tests.sh

                which starts `mplabel serve` against a temporary seeded \
                database, passes its address in, and tears it down after.
                """)
        }
        return ServerHarness(
            baseURL: base,
            password: env["MPLABEL_UITEST_PASSWORD"] ?? "uitest-password",
            token: token)
    }

    /// Ask the server directly, for the handful of assertions that are
    /// about data rather than pixels.
    func get(_ path: String) throws -> Data {
        try request(path, method: "GET")
    }

    /// Change something directly. Used to put back what a destructive
    /// test changed - see `FlowTests.tearDownWithError`.
    @discardableResult
    func post(_ path: String, body: [String: Any]? = nil) throws -> Data {
        try request(path, method: "POST", body: body)
    }

    /// The database id of a sale, found by the code printed on the
    /// parcel. The screens show codes and the API takes ids, so a test
    /// that has driven the UI knows the wrong one of the two.
    func saleID(code: String) throws -> Int {
        let data = try get("/api/v1/orders")
        let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let orders = root?["orders"] as? [[String: Any]] ?? []
        guard let match = orders.first(where: { $0["code"] as? String == code }),
              let id = match["id"] as? Int else {
            throw XCTSkip("no sale with code \(code) on the server")
        }
        return id
    }

    /// The database id of a listing, by the inventory code on its own
    /// label. Same reasoning as `saleID`.
    func listingID(inventoryCode: String) throws -> Int {
        let data = try get("/api/v1/lookup/" + inventoryCode)
        let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let listing = root?["listing"] as? [String: Any]
        guard let id = listing?["id"] as? Int else {
            throw XCTSkip("no listing with inventory code \(inventoryCode)")
        }
        return id
    }

    /// A listing's id by its title, for a test that typed the title and
    /// needs to ask the server what it made of it.
    func listingID(forTitle title: String) throws -> Int {
        let data = try get("/api/v1/inventory")
        let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let items = root?["items"] as? [[String: Any]] ?? []
        guard let match = items.first(where: { $0["title"] as? String == title }),
              let id = match["id"] as? Int else {
            throw XCTSkip("no listing titled \(title)")
        }
        return id
    }

    /// What the server thinks one thing cost. The whole sourcing half
    /// exists to make this number not-null.
    func paid(forListing id: Int) throws -> Double? {
        let data = try get("/api/v1/inventory/\(id)")
        let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return (root?["item"] as? [String: Any])?["paid"] as? Double
    }

    private func request(_ path: String, method: String,
                         body: [String: Any]? = nil) throws -> Data {
        var req = URLRequest(url: URL(string: baseURL + path)!)
        req.httpMethod = method
        req.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        req.setValue("1", forHTTPHeaderField: "X-Mplabel")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        var result: Result<Data, Error>!
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, error in
            result = error.map { .failure($0) } ?? .success(data ?? Data())
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 15)
        return try result.get()
    }
}
