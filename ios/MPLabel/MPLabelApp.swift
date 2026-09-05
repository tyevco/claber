//  MPLabelApp.swift
//
//  The native client for `mplabel serve`. Same API as the PWA, same
//  bearer token, same /api/v1 prefix - what this buys over the web app
//  is the camera: VisionKit reads the QR on a printed label with
//  Apple's own decoder, which is the whole reason this target exists.

import SwiftUI

@main
struct MPLabelApp: App {
    @StateObject private var session = Session.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(session)
                .tint(.mpAccent)
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
        TabView {
            QueueView()
                .tabItem { Label("To ship", systemImage: "shippingbox") }
            ShelfView()
                .tabItem { Label("Shelf", systemImage: "square.grid.2x2") }
            ScanView()
                .tabItem { Label("Scan", systemImage: "qrcode.viewfinder") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
    }
}
