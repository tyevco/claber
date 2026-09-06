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
        var req = URLRequest(url: URL(string: baseURL + path)!)
        req.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        req.setValue("1", forHTTPHeaderField: "X-Mplabel")

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
