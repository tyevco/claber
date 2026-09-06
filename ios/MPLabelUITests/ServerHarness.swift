//  ServerHarness.swift
//
//  Runs the real `mplabel serve` for the duration of a UI test.
//
//  Not a mock, on purpose. A Swift stub would answer what we *believe*
//  web.py answers, and that belief has been wrong twice - so a mock
//  would have agreed with the models both times and caught neither. The
//  server is stdlib Python and starts in about a second; the fidelity is
//  free.
//
//  A UI test bundle runs on the Mac, not in the simulator, so it can
//  spawn a process. The simulator shares the host's network stack, so
//  127.0.0.1 in the app reaches this server.

import Foundation
import XCTest

final class ServerHarness {

    private let process = Process()
    private let home: URL
    let port: Int
    let password = "uitest-password"

    var baseURL: String { "http://127.0.0.1:\(port)" }

    /// Where the repo is, found by walking up from this file. Beats
    /// hard-coding a path or requiring an env var for the common case,
    /// and `#filePath` is exact.
    private static var repoRoot: URL {
        URL(fileURLWithPath: #filePath)          // …/ios/MPLabelUITests/…
            .deletingLastPathComponent()          // …/ios/MPLabelUITests
            .deletingLastPathComponent()          // …/ios
            .deletingLastPathComponent()          // repo root
    }

    /// `MPLABEL_PYTHON` overrides. Default is whatever `python3` is on
    /// PATH, which on a Mac with the repo checked out is the usual case.
    private static var python: String {
        ProcessInfo.processInfo.environment["MPLABEL_PYTHON"] ?? "python3"
    }

    init() throws {
        home = FileManager.default.temporaryDirectory
            .appendingPathComponent("mplabel-uitest-\(UUID().uuidString)")
        try FileManager.default.createDirectory(
            at: home.appendingPathComponent("labels"),
            withIntermediateDirectories: true)

        // Ephemeral-ish: a fixed port would collide with a server left
        // running from a previous session, and the failure would look
        // like the app talking to stale data.
        port = Int.random(in: 49_200...49_900)
    }

    /// Seeds a database and starts the server. Throws with something
    /// readable if Python is not where we think - "connection refused"
    /// three layers up is not a useful way to learn that.
    func start() throws {
        try seed()

        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = [Self.python, "-m", "mplabel", "serve",
                             "--bind", "127.0.0.1", "--port", "\(port)"]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = Self.repoRoot.appendingPathComponent("src").path
        env["MPLABEL_HOME"] = home.path
        env["MPLABEL_WEB_PASSWORD_HASH"] = try passwordHash()
        env["MPLABEL_WEB_BIND"] = "127.0.0.1"
        env["MPLABEL_WEB_PORT"] = "\(port)"
        process.environment = env
        process.standardOutput = Pipe()
        process.standardError = Pipe()

        do {
            try process.run()
        } catch {
            throw XCTSkip("could not start the server with \(Self.python): "
                          + "\(error.localizedDescription). Set "
                          + "MPLABEL_PYTHON to a Python that has this repo "
                          + "installed.")
        }
        try waitUntilAnswering()
    }

    func stop() {
        if process.isRunning { process.terminate() }
        try? FileManager.default.removeItem(at: home)
    }

    /// A token, so tests that are not about signing in do not have to.
    func signIn() throws -> String {
        struct Out: Decodable { let token: String }
        var req = URLRequest(url: URL(string: baseURL + "/api/login")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Mplabel")
        req.httpBody = try JSONEncoder().encode(["password": password])
        let data = try syncData(for: req)
        return try JSONDecoder().decode(Out.self, from: data).token
    }

    // MARK: - the bits that shell out

    private func run(_ args: [String]) throws -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = [Self.python] + args
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = Self.repoRoot.appendingPathComponent("src").path
        env["MPLABEL_HOME"] = home.path
        p.environment = env
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        try p.run()
        p.waitUntilExit()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        return String(decoding: data, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func passwordHash() throws -> String {
        try run(["-c",
                 "from mplabel import web; "
                 + "print(web.hash_password('\(password)'))"])
    }

    /// The same seed the model fixtures use, so a UI test and a decoding
    /// test are looking at the same rows. One generator, one set of
    /// awkward cases - a second hand-written seed would drift from it.
    private func seed() throws {
        _ = try run(["-c", """
import pathlib, sys
sys.path.insert(0, r'\(Self.repoRoot.appendingPathComponent("tests").path)')
from mplabel import cli
import make_ios_fixtures as gen
conn = cli.connect_db(pathlib.Path(r'\(home.path)'))
gen.seed(conn)
conn.close()
"""])
    }

    private func waitUntilAnswering(timeout: TimeInterval = 20) throws {
        let deadline = Date().addingTimeInterval(timeout)
        var last: Error?
        while Date() < deadline {
            do {
                _ = try syncData(for: URLRequest(
                    url: URL(string: baseURL + "/healthz")!))
                return
            } catch {
                last = error
                Thread.sleep(forTimeInterval: 0.25)
            }
        }
        throw last ?? URLError(.timedOut)
    }

    /// Synchronous on purpose: this is setup, not app code, and an async
    /// XCTestCase setUp for a one-shot health check buys nothing.
    private func syncData(for request: URLRequest) throws -> Data {
        var result: Result<Data, Error>!
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, _, error in
            result = error.map { .failure($0) } ?? .success(data ?? Data())
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 15)
        return try result.get()
    }
}
