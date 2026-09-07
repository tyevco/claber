//  ReceiptScanView.swift
//
//  The receipt, read on the phone.
//
//  `VNDocumentCameraViewController` rather than the app's own shutter:
//  it does edge detection, perspective correction and multi-page for
//  free, and a thermal receipt is exactly the thing that needs all three
//  - it curls, it is longer than the frame, and it is photographed at an
//  angle on a kitchen table. It is also the control people already know
//  from Notes, which is worth more than consistency with a screen she
//  uses in a different room.
//
//  **The text is read here and only the text is sent.** Vision does it
//  on-device for nothing, so her receipts - which carry a store, a date
//  and what she spends - never go on the wire. What reaches the Pi is a
//  reading she can correct, not a photograph of her afternoon.

import SwiftUI
import UIKit
import Vision
import VisionKit

struct ReceiptScanView: View {
    let trip: Trip
    var onRead: ((ReceiptResponse) -> Void)?

    @Environment(\.dismiss) private var dismiss
    @State private var scanning = false
    @State private var text = ""
    @State private var lines: [ReceiptLine] = []
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        MPScreen(eyebrow: trip.store, title: "The receipt") {
            if let error { MPError(message: error) }

            if lines.isEmpty {
                MPCard {
                    VStack(alignment: .leading, spacing: MP.S.x2) {
                        MPEyebrow("Read it")
                        Text("Photograph the receipt and the phone reads "
                             + "it here. Only the text goes to the Pi - the "
                             + "picture stays on the phone.")
                            .font(.system(size: 12.5))
                            .foregroundStyle(MP.Palette.muted)
                        if !DataScannerViewController.isSupported {
                            Text("This device cannot scan documents. You "
                                 + "can still type the amounts in on the "
                                 + "next screen.")
                                .font(.system(size: 12))
                                .foregroundStyle(MP.Palette.subtle)
                        }
                        Button("Scan the receipt") { scanning = true }
                            .font(.system(size: 14, weight: .semibold))
                    }
                }
            } else {
                lineList
                MPHoldButton(title: "Hold to file this reading",
                             enabled: !busy) {
                    send()
                }
                .padding(.top, MP.S.x2)
            }
        }
        .sheet(isPresented: $scanning) {
            DocumentScanner(onText: { read in
                text = read
                lines = ReceiptReading.parse(read)
                scanning = false
            }, onFailure: { message in
                error = message
                scanning = false
            })
            .ignoresSafeArea()
        }
    }

    /// What was read, before it is filed. Shown because OCR on a curling
    /// thermal receipt drops characters, and a reading she can see is a
    /// reading she can reject - the alternative is discovering it in the
    /// costs a week later.
    private var lineList: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("What it read")
                ForEach(lines) { line in
                    HStack {
                        Text(line.label ?? "—")
                            .font(.system(size: 13))
                            .foregroundStyle(line.isGoods ? MP.Palette.fg
                                                          : MP.Palette.subtle)
                        Spacer(minLength: MP.S.x2)
                        Text(money(line.amount))
                            .font(.system(size: 13).monospaced())
                            .foregroundStyle(line.isGoods ? MP.Palette.fg
                                                          : MP.Palette.subtle)
                    }
                }
                // Cash tendered and change are the biggest numbers on the
                // paper. Saying which lines are goods is what stops one
                // of them being read as a cost.
                Text("Greyed lines are not goods - totals, tax, cash and "
                     + "change. Only the black ones can become a cost.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(MP.Palette.subtle)
                Button("Scan it again") { scanning = true }
                    .font(.system(size: 13, weight: .semibold))
            }
        }
    }

    private func send() {
        busy = true
        error = nil
        Task {
            do {
                let out = try await APIClient.shared.sendReceipt(
                    trip: trip.id, text: text)
                onRead?(out)
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}

/// The same parse the server does, for the preview above.
///
/// Deliberately *only* for the preview: the server's reading is the one
/// that gets stored, and two parsers that disagree would show her one
/// thing and file another. This exists so the list appears the instant
/// the scanner closes rather than after a round trip.
enum ReceiptReading {
    static func parse(_ text: String) -> [ReceiptLine] {
        var out: [ReceiptLine] = []
        var position = 0
        for raw in text.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard let match = line.range(
                of: #"(-?\$?\s*\d{1,4}[.,]\d{2})\s*[A-Z]?$"#,
                options: .regularExpression) else { continue }
            let amountText = line[match]
                .replacingOccurrences(of: "$", with: "")
                .replacingOccurrences(of: ",", with: ".")
                .trimmingCharacters(in: .whitespaces)
            guard let amount = Double(amountText) else { continue }
            var label = String(line[line.startIndex..<match.lowerBound])
                .trimmingCharacters(in: CharacterSet(charactersIn: " .:\t-"))
            if label.isEmpty { label = "(unlabelled)" }
            position += 1
            out.append(ReceiptLine(position: position, label: label,
                                   amount: amount, kind: kind(of: label)))
        }
        return out
    }

    private static func kind(of label: String) -> String {
        let lower = label.lowercased()
        if lower.contains("subtotal") || lower.contains("sub total") {
            return "subtotal"
        }
        if lower.contains("total") { return "total" }
        for word in ["tax", "vat", "hst", "gst"] where lower.contains(word) {
            return "tax"
        }
        for word in ["cash", "change", "card", "visa", "debit", "credit",
                     "tender", "balance", "thank"] where lower.contains(word) {
            return "ignored"
        }
        return "item"
    }
}

/// Apple's scanner, wrapped, with the text extracted on the way out.
struct DocumentScanner: UIViewControllerRepresentable {
    let onText: (String) -> Void
    let onFailure: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onText: onText, onFailure: onFailure)
    }

    func makeUIViewController(context: Context) -> VNDocumentCameraViewController {
        let vc = VNDocumentCameraViewController()
        vc.delegate = context.coordinator
        return vc
    }

    func updateUIViewController(_ vc: VNDocumentCameraViewController,
                                context: Context) {}

    final class Coordinator: NSObject, VNDocumentCameraViewControllerDelegate {
        let onText: (String) -> Void
        let onFailure: (String) -> Void

        init(onText: @escaping (String) -> Void,
             onFailure: @escaping (String) -> Void) {
            self.onText = onText
            self.onFailure = onFailure
        }

        func documentCameraViewController(
            _ controller: VNDocumentCameraViewController,
            didFinishWith scan: VNDocumentCameraScan
        ) {
            var pages: [String] = []
            for index in 0..<scan.pageCount {
                pages.append(Self.read(scan.imageOfPage(at: index)))
            }
            onText(pages.joined(separator: "\n"))
        }

        func documentCameraViewControllerDidCancel(
            _ controller: VNDocumentCameraViewController
        ) {
            onText("")
        }

        func documentCameraViewController(
            _ controller: VNDocumentCameraViewController,
            didFailWithError error: Error
        ) {
            onFailure(error.localizedDescription)
        }

        /// Accurate rather than fast, and no language correction.
        ///
        /// A receipt is department names and numbers, and the language
        /// model behind `usesLanguageCorrection` is trained on prose -
        /// it turns `LINENS 3.49` into words that look more like English
        /// and less like the receipt.
        static func read(_ image: UIImage) -> String {
            guard let cg = image.cgImage else { return "" }
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = false
            let handler = VNImageRequestHandler(cgImage: cg, options: [:])
            do {
                try handler.perform([request])
            } catch {
                return ""
            }
            let observations = request.results ?? []
            return observations
                .compactMap { $0.topCandidates(1).first?.string }
                .joined(separator: "\n")
        }
    }
}
