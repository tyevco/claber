//  TestHooks.swift
//
//  How a UI test points the app at a server and gets past the login
//  screen. Compiled out of release builds entirely.
//
//  The alternative was a mock: a stub HTTP server written in Swift,
//  answering what we believe `web.py` answers. That belief is exactly
//  what has been wrong twice, so a mock would have agreed with the
//  models on both occasions and proved nothing. The UI tests run the
//  real server instead - it is a stdlib Python process that starts in a
//  second - and this is the seam that lets them.

import Foundation

enum TestHooks {

    /// Applied once at launch. In a release build this function does not
    /// exist, so there is no path from a launch argument to the app's
    /// credentials on a device.
    static func applyIfPresent() {
        #if DEBUG
        let env = ProcessInfo.processInfo.environment

        // A UI test gets a clean slate: the keychain survives app
        // reinstalls by design, which is right for her and wrong for a
        // test that expects to start signed out.
        if env["MPLABEL_UITEST_RESET"] == "1" {
            Keychain.token = nil
            Settings.serverURL = nil
        }

        if let url = env["MPLABEL_UITEST_SERVER"], !url.isEmpty {
            Settings.serverURL = url
        }
        // A token rather than a password: signing in is worth testing
        // once, and worth skipping on every other test that needs to get
        // to a screen behind it.
        if let token = env["MPLABEL_UITEST_TOKEN"], !token.isEmpty {
            Keychain.token = token
        }
        Session.shared.refresh()
        #endif
    }

    /// True while a UI test is driving. Used only to make animations
    /// deterministic - never to change what a screen shows, because a
    /// test that exercises a different app than she runs is worse than
    /// no test.
    static var isUITesting: Bool {
        #if DEBUG
        return ProcessInfo.processInfo.environment["MPLABEL_UITEST"] == "1"
        #else
        return false
        #endif
    }
}
