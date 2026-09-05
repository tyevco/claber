//  Session.swift
//
//  Where the credential lives, where the server address lives, and the
//  observable bit of state the UI switches on.

import Foundation
import Security

extension Notification.Name {
    /// Posted when a 401 comes back, from whatever thread noticed. The
    /// UI listens rather than the network layer reaching into it.
    static let mplabelSignedOut = Notification.Name("mplabel.signedOut")
}

// MARK: - the token

/// The session token in the keychain rather than UserDefaults.
///
/// It is a bearer credential for a service holding customers' names and
/// home addresses, and UserDefaults is a plist in the app container -
/// readable from a backup of the phone. The keychain also survives
/// reinstall-free upgrades, which is the behaviour wanted: she should
/// not be signing in again every time this is rebuilt onto her phone.
///
/// `kSecAttrAccessibleAfterFirstUnlock` and not `WhenUnlocked`: nothing
/// here runs in the background today, but a widget or a notification
/// action would, and the difference only shows up as a mysterious
/// failure on a locked phone.
enum Keychain {
    private static let service = "com.tyevco.mplabel"
    private static let account = "session-token"

    static var token: String? {
        get {
            let q: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: account,
                kSecReturnData as String: true,
                kSecMatchLimit as String: kSecMatchLimitOne,
            ]
            var out: CFTypeRef?
            guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
                  let data = out as? Data else { return nil }
            return String(data: data, encoding: .utf8)
        }
        set {
            let q: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: account,
            ]
            SecItemDelete(q as CFDictionary)
            guard let value = newValue?.data(using: .utf8) else { return }
            var add = q
            add[kSecValueData as String] = value
            add[kSecAttrAccessible as String] =
                kSecAttrAccessibleAfterFirstUnlock
            SecItemAdd(add as CFDictionary, nil)
        }
    }
}

// MARK: - where the server is

enum Settings {
    private static let key = "mplabel.serverURL"

    /// Normalised on the way in, because the two ways this gets typed
    /// wrong both produce a confusing failure rather than an obvious
    /// one: no scheme at all, and a trailing slash that turns
    /// `/api/v1/orders` into `//api/v1/orders` on some hosts.
    static var serverURL: String? {
        get { UserDefaults.standard.string(forKey: key) }
        set {
            guard var v = newValue?.trimmingCharacters(in: .whitespaces),
                  !v.isEmpty else {
                UserDefaults.standard.removeObject(forKey: key)
                return
            }
            if !v.contains("://") { v = "https://" + v }
            while v.hasSuffix("/") { v.removeLast() }
            UserDefaults.standard.set(v, forKey: key)
        }
    }
}

// MARK: - what the UI switches on

@MainActor
final class Session: ObservableObject {
    static let shared = Session()

    enum State: Equatable {
        case needsServer
        case signedOut
        case signedIn
    }

    @Published private(set) var state: State = .signedOut

    private init() {
        NotificationCenter.default.addObserver(
            forName: .mplabelSignedOut, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        refresh()
    }

    func refresh() {
        if Settings.serverURL == nil {
            state = .needsServer
        } else if Keychain.token == nil {
            state = .signedOut
        } else {
            state = .signedIn
        }
    }

    func signOut() {
        Keychain.token = nil
        refresh()
    }
}
