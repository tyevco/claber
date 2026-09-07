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

@MainActor
final class Push: NSObject, ObservableObject, UIApplicationDelegate {
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
            if state != .registered { state = .registering }
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    func ask() async {
        do {
            let granted = try await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound, .badge])
            state = granted ? .registering : .refused
            if granted { UIApplication.shared.registerForRemoteNotifications() }
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    // MARK: - UIApplicationDelegate

    nonisolated func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken token: Data
    ) {
        let hex = token.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in await self.send(hex) }
    }

    nonisolated func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        Task { @MainActor in
            self.state = .failed(error.localizedDescription)
        }
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
