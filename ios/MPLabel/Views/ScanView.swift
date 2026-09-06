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
//
//  The first version of this screen showed no camera at all, for two
//  reasons that compounded: nothing ever asked for camera permission,
//  and `startScanning()` was called as `try?`, so the error explaining
//  that was discarded. A blank rectangle with an instruction printed
//  over it is the worst possible way to say "access denied", so both
//  halves are handled explicitly below and every failure has words.

import AVFoundation
import SwiftUI
import UIKit
import Vision
import VisionKit

struct ScanView: View {
    /// Permission is a state with four answers, not a Bool. Collapsing
    /// "not asked yet" into "no" is what produced a blank screen.
    private enum Access {
        case checking
        case granted
        case denied
        case unsupported
    }

    @State private var access: Access = .checking
    @State private var scanned: String?
    @State private var result: Lookup?
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Group {
                switch access {
                case .checking:
                    ProgressView().task { await check() }
                case .granted:
                    scanner
                case .denied:
                    denied
                case .unsupported:
                    unsupported
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

    // MARK: - permission

    private func check() async {
        guard DataScannerViewController.isSupported else {
            access = .unsupported
            return
        }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            access = .granted
        case .notDetermined:
            // Ask. This is what puts the NSCameraUsageDescription string
            // in front of her, and until something calls it the status
            // stays .notDetermined for ever.
            access = await AVCaptureDevice.requestAccess(for: .video)
                ? .granted : .denied
        default:
            access = .denied
        }
    }

    // MARK: - the states

    private var scanner: some View {
        ZStack(alignment: .bottom) {
            CodeScanner(
                isActive: !busy && result == nil,
                onCode: { code in
                    guard scanned != code else { return }
                    scanned = code
                    look(up: code)
                },
                onFailure: { message in
                    // Whatever stopped the scanner starting, say it.
                    // Silence here is indistinguishable from a camera
                    // pointed at something unreadable.
                    error = message
                })
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .ignoresSafeArea(edges: .bottom)

            VStack(spacing: 10) {
                if let error { ErrorBanner(message: error) }
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

    private var denied: some View {
        ContentUnavailableView {
            Label("No camera access", systemImage: "camera.slash")
        } description: {
            Text("Scanning needs the camera. Turn it on in Settings, or "
                 + "type the code from the label on the Shelf tab.")
        } actions: {
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    UIApplication.shared.open(url)
                }
            }
        }
    }

    /// The simulator has no camera, and neither do some devices. Both are
    /// ordinary situations rather than errors - the four-character code
    /// is printed on the label in large type for exactly this reason.
    private var unsupported: some View {
        ContentUnavailableView {
            Label("No camera here", systemImage: "camera.slash")
        } description: {
            Text("Type the code from the label on the Shelf tab instead.")
        }
    }

    // MARK: - looking a code up

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
    let onFailure: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onCode: onCode, onFailure: onFailure)
    }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let vc = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            // .accurate, not .balanced: the target is a QR printed at
            // five dots per module on thermal paper, where the modules
            // are small and the edges bleed. Frame rate is worth less
            // here than getting it on the first try.
            qualityLevel: .accurate,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isHighlightingEnabled: true)
        vc.delegate = context.coordinator
        return vc
    }

    func updateUIViewController(_ vc: DataScannerViewController,
                                context: Context) {
        context.coordinator.onCode = onCode
        context.coordinator.onFailure = onFailure

        guard isActive else {
            if vc.isScanning { vc.stopScanning() }
            return
        }
        guard !vc.isScanning else { return }

        // Deferred a turn of the run loop on purpose. `startScanning()`
        // throws if the view is not yet in a window, and the first
        // `updateUIViewController` runs before it is - which was the
        // original bug, made invisible by a `try?`.
        DispatchQueue.main.async {
            guard !vc.isScanning, vc.view.window != nil else { return }
            do {
                try vc.startScanning()
            } catch {
                context.coordinator.onFailure(
                    "The camera would not start: \(error.localizedDescription)")
            }
        }
    }

    static func dismantleUIViewController(_ vc: DataScannerViewController,
                                          coordinator: Coordinator) {
        vc.stopScanning()
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        var onCode: (String) -> Void
        var onFailure: (String) -> Void

        init(onCode: @escaping (String) -> Void,
             onFailure: @escaping (String) -> Void) {
            self.onCode = onCode
            self.onFailure = onFailure
        }

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

        func dataScanner(_ scanner: DataScannerViewController,
                         becameUnavailableWithError error:
                            DataScannerViewController.ScanningUnavailable) {
            onFailure("Scanning stopped: \(error.localizedDescription)")
        }
    }
}

// `Lookup`'s Hashable and Identifiable conformances live on the enum in
// Models.swift, not here: Swift synthesises `==` and `hash(into:)` for an
// enum with associated values only in the file that declares it, so an
// extension in this file was a demand to write both by hand.
