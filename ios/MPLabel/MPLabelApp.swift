//  MPLabelApp.swift
//
//  The native client for `mplabel serve`. Same API as the PWA, same
//  bearer token, same /api/v1 prefix - what this buys over the web app
//  is the camera: VisionKit reads the QR on a printed label with
//  Apple's own decoder, which is the whole reason this target exists.

import SwiftUI

@main
@MainActor
struct MPLabelApp: App {
    @StateObject private var session = Session.shared

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
        // Five, and the four that are not Scan are the design's own tab
        // bar. Settings is not among them - it is a gear on the queue,
        // the way the PWA has it, because a tab is for something she
        // does and settings is something she did once.
        TabView {
            QueueView()
                .tabItem { Label("To ship", systemImage: "shippingbox") }
            PendingView()
                .tabItem { Label("Pending", systemImage: "printer.dotmatrix") }
            ShelfView()
                .tabItem { Label("Shelf", systemImage: "square.grid.2x2") }
            ScanView()
                .tabItem { Label("Scan", systemImage: "qrcode.viewfinder") }
            ProfitView()
                .tabItem { Label("Profit", systemImage: "chart.bar") }
        }
    }
}
