//  MPLabelApp.swift
//
//  The native client for `mplabel serve`. Same API as the PWA, same
//  bearer token, same /api/v1 prefix - what this buys over the web app
//  is the camera: VisionKit reads the QR on a printed label with
//  Apple's own decoder, which is the whole reason this target exists.

import SwiftUI
import UIKit

@main
@MainActor
struct MPLabelApp: App {
    @StateObject private var session = Session.shared
    // The delegate exists only to receive the APNs token - SwiftUI has
    // no other way to be handed one.
    // `PushDelegate`, not `Push`: the adaptor builds its own instance of
    // whatever it is given, and pointing it at `Push` made a second one
    // that got every callback while the screen watched `Push.shared`.
    @UIApplicationDelegateAdaptor(PushDelegate.self) private var pushDelegate

    init() {
        // Before any view reads Settings or the keychain. In release
        // this is a call to a function whose body is #if DEBUG'd away.
        TestHooks.applyIfPresent()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(session)
                .tint(MP.Palette.accent)
                // The design is dark-first. Both palettes are defined,
                // and `Color(light:dark:)` resolves per environment, so
                // this is the default rather than a lock - the system
                // setting still wins on a device set to light.
                .background(MP.Palette.bg)
        }
    }
}

struct RootView: View {
    @EnvironmentObject private var session: Session

    var body: some View {
        switch session.state {
        case .needsServer:
            ServerSetupView()
        case .signedOut:
            LoginView()
        case .signedIn:
            MainTabs()
        }
    }
}

struct MainTabs: View {
    var body: some View {
        // Five, which is the most iOS shows before it starts hiding
        // them behind "More" - and a tab she cannot see is a feature
        // that does not exist. Settings is not among them: it is a gear
        // on the queue, the way the PWA has it, because a tab is for
        // something she does and settings is something she did once.
        //
        // Capture takes Pending's place rather than adding a sixth.
        // Pending is a recovery screen for a printer that was off all
        // morning; Capture is one of the four moments the app is for and
        // is useless if it is two taps deep while she is holding a cart.
        // Pending keeps its own count on the queue's chip strip, which
        // is where she looks when a label did not come out.
        TabView {
            QueueView()
                .tabItem { Label("To ship", systemImage: "shippingbox") }
            CaptureView()
                .tabItem { Label("Capture", systemImage: "camera") }
            ShelfView()
                .tabItem { Label("Shelf", systemImage: "square.grid.2x2") }
            ScanView()
                .tabItem { Label("Scan", systemImage: "qrcode.viewfinder") }
            ProfitView()
                .tabItem { Label("Profit", systemImage: "chart.bar") }
        }
    }
}
