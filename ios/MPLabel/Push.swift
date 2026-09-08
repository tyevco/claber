//  Push.swift
//
//  Asking to be told, and handing the Pi the token to tell.
//
//  Three things earn a notification and the server decides which - a
//  parcel is due, a label never printed, money has no home. Nothing is
//  decided here; this end only registers, which is why there is no
//  scheduling logic on the phone and no local notification fallback. Two
//  places deciding when to interrupt her is two places to get it wrong,
//  and the server is the one that knows what is due.
//
//  The token is per install *and per environment*: a build signed with a
//  development profile gets a sandbox token, which the production APNs
//  host rejects with `BadDeviceToken` - an error that reads like the
//  token is malformed when it is simply addressed to the wrong Apple.
//  So the environment is sent alongside it and stored with it.

import SwiftUI
import UIKit
import UserNotifications

/// The app delegate, which exists only to hand the token over.
///
/// **Separate from `Push` on purpose.** `@UIApplicationDelegateAdaptor`
/// *constructs its own instance* of whatever type it is given, so
/// pointing it at `Push` produced a second `Push` that received every
/// callback while the Settings screen watched `Push.shared` - which sat
/// at "Registering…" for ever, with the token already delivered to an
/// object nobody could see. It presents as a hang and is two objects.
@MainActor
final class PushDelegate: NSObject, UIApplicationDelegate {
    nonisolated func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken token: Data
    ) {
        Task { @MainActor in
            Push.shared.received(token)
        }
    }

    nonisolated func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        Task { @MainActor in
            Push.shared.failed(error)
        }
    }
}

@MainActor
final class Push: NSObject, ObservableObject {
    /// Where the registration got to. Four states again, because "not
    /// asked yet" and "she said no" want different sentences.
    enum State: Equatable {
        case unknown
        case notAsked
        case refused
        case registering
        case registered
        case failed(String)

        var sentence: String {
            switch self {
            case .unknown, .notAsked:
                return "This phone is not registered for notifications."
            case .refused:
                return "Notifications are off for this app. Turn them on in "
                     + "Settings if you want to be told a parcel is due."
            case .registering:
                return "Registering…"
            case .registered:
                return "Registered. You will be told when a parcel is due, "
                     + "a label never printed, or money has no home."
            case .failed(let why):
                return "Registration failed: \(why)"
            }
        }
    }

    static let shared = Push()

    @Published private(set) var state: State = .unknown

    func refresh() async {
        let settings = await UNUserNotificationCenter.current()
            .notificationSettings()
        switch settings.authorizationStatus {
        case .notDetermined: state = .notAsked
        case .denied:        state = .refused
        default:
            // Authorised is not the same as registered - the token is a
            // separate round trip and the app has to ask for it again on
            // every launch, because it can change.
            if state != .registered {
                state = .registering
                giveUpIfSilent()
            }
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    /// Registration is a round trip through Apple and it does not always
    /// come back. Sitting at "Registering…" for ever is the one state
    /// that tells her nothing and offers nothing - so it gives up after
    /// a while and says what to do.
    private func giveUpIfSilent() {
        Task {
            try? await Task.sleep(for: .seconds(20))
            if case .registering = state {
                state = .failed(
                    "Apple did not answer. That is usually no network, or "
                    + "a build without the push entitlement. Try again.")
            }
        }
    }

    func ask() async {
        do {
            let granted = try await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound, .badge])
            state = granted ? .registering : .refused
            if granted {
                UIApplication.shared.registerForRemoteNotifications()
                giveUpIfSilent()
            }
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    // MARK: - what the delegate hands over

    func received(_ token: Data) {
        let hex = token.map { String(format: "%02x", $0) }.joined()
        Task { await send(hex) }
    }

    func failed(_ error: Error) {
        // Apple's own words. The common one is "no valid aps-environment
        // entitlement string found", which reads as a provisioning
        // problem and is a missing key in the entitlements file.
        state = .failed(error.localizedDescription)
    }

    private func send(_ hex: String) async {
        do {
            try await APIClient.shared.registerDevice(
                token: hex, environment: Self.environment)
            state = .registered
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    /// Which Apple to send to. A debug build carries the development
    /// entitlement and gets a sandbox token; anything else is production.
    static var environment: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }
}
