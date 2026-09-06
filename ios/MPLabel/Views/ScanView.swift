//  ScanView.swift
//
//  The reason this app exists rather than staying a web page.
//
//  Safari has no BarcodeDetector, so the PWA can only read the shelf
//  marker - our own format, decoded by our own JavaScript. Here
//  VisionKit reads the QR with Apple's decoder, which was settled on
//  paper before a line of this was written: a printed
//  `inventory-label --qr` read first time in the stock Camera app, so
//  the module size at 5 dots survives thermal bleed.
//
//  That is also why there is no marker decoder here and will not be one.
//  `marker.py` and `marker.js` are only trustworthy because 137
//  assertions pin them to each other; a third implementation in Swift
//  would have none of that harness.

import AVFoundation
import SwiftUI
import UIKit
import Vision
import VisionKit

struct ScanView: View {
    @State private var scanned: String?
    @State private var result: Lookup?
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Group {
                if DataScannerViewController.isSupported
                    && DataScannerViewController.isAvailable {
                    scanner
                } else {
                    unavailable
                }
            }
            .navigationTitle("Scan")
            .navigationDestination(item: $result) { found in
                switch found {
                case .sale(let d):      OrderDetailView(preloaded: d)
                case .listing(let it):  ItemView(itemID: it.id, preloaded: it)
                }
            }
        }
    }

    private var scanner: some View {
        ZStack(alignment: .bottom) {
            CodeScanner(isActive: !busy && result == nil) { code in
                guard scanned != code else { return }
                scanned = code
                look(up: code)
            }
            .ignoresSafeArea(edges: .bottom)

            VStack(spacing: 10) {
                if let error {
                    ErrorBanner(message: error)
                }
                Text("Point at the QR on a label")
                    .font(.footnote)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(.black.opacity(0.55), in: Capsule())
                    .padding(.bottom, 24)
            }
        }
    }

    /// The simulator has no camera and neither does a phone whose owner
    /// said no. Both are ordinary situations rather than errors, and the
    /// four-character code is printed on the label in large type for
    /// exactly this reason.
    private var unavailable: some View {
        ContentUnavailableView {
            Label("No camera here", systemImage: "camera.slash")
        } description: {
            Text("Type the code from the label on the Shelf tab instead.")
        }
    }

    private func look(up code: String) {
        busy = true
        error = nil
        Task {
            do {
                result = try await APIClient.shared.lookUp(code: code)
            } catch {
                // Say which code. The next question after "nothing
                // found" is always "what did it think it read", and on
                // thermal paper that is a real question.
                self.error = "\(code): \(error.localizedDescription)"
                // Let the same label be tried again after a failure.
                scanned = nil
            }
            busy = false
        }
    }
}

/// `DataScannerViewController` wrapped for SwiftUI.
///
/// Restricted to QR alone. Letting it read every symbology it knows
/// means a barcode on a courier label or a book spine in shot becomes a
/// lookup for a code that was never ours - and the failure looks like
/// the app misreading her label rather than reading someone else's.
struct CodeScanner: UIViewControllerRepresentable {
    let isActive: Bool
    let onCode: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onCode: onCode) }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let vc = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isHighlightingEnabled: true)
        vc.delegate = context.coordinator
        return vc
    }

    func updateUIViewController(_ vc: DataScannerViewController,
                                context: Context) {
        context.coordinator.onCode = onCode
        if isActive {
            try? vc.startScanning()
        } else {
            vc.stopScanning()
        }
    }

    static func dismantleUIViewController(_ vc: DataScannerViewController,
                                          coordinator: Coordinator) {
        vc.stopScanning()
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        var onCode: (String) -> Void

        init(onCode: @escaping (String) -> Void) { self.onCode = onCode }

        func dataScanner(_ scanner: DataScannerViewController,
                         didAdd addedItems: [RecognizedItem],
                         allItems: [RecognizedItem]) {
            for item in addedItems {
                if case .barcode(let code) = item,
                   let text = code.payloadStringValue {
                    // A haptic, because she is looking at a box rather
                    // than at the screen.
                    UINotificationFeedbackGenerator()
                        .notificationOccurred(.success)
                    onCode(text.trimmingCharacters(in: .whitespacesAndNewlines))
                    return
                }
            }
        }
    }
}

// `Lookup`'s Hashable and Identifiable conformances live on the enum in
// Models.swift, not here: Swift synthesises `==` and `hash(into:)` for an
// enum with associated values only in the file that declares it, so an
// extension in this file was a demand to write both by hand.
