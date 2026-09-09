//  PrintLabelView.swift
//
//  Print a 4x6 that did not come from a Marketplace email - eBay, a
//  carrier's own site, a parcel that is not a sale at all.
//
//  Everything else in this app prints a label the Pi already has, off a
//  sales row, checked against the address recorded when that sale was
//  filed. This sends up a file she picked a second ago, and the whole
//  screen is shaped by the two things that follow from that:
//
//  * Nothing is recorded. Not a sale, not a listing, no id to come back
//    to and no reprint - printd's journal is the only trace, and where
//    the backend writes straight to a device there is not even that. The
//    answer says which, in words, rather than implying a record that
//    does not exist.
//  * `label_belongs_to` has nothing to check against, because there is
//    no recorded recipient to compare the PDF with. The backstop here is
//    that she chose the file, which is a weaker guarantee - so checking
//    first is offered as the default rather than buried as an option.
//
//  The crop and the rotation happen on the Pi. That is deliberate:
//  finding the label on a US Letter page lives in `label.py` next to the
//  geometry that has real labels behind it, and a second implementation
//  here would be a second thing to be wrong about a page nobody can see.

import SwiftUI
import UniformTypeIdentifiers

struct PrintLabelView: View {
    @State private var picking = false
    @State private var name: String?
    @State private var pdf: Data?
    @State private var dryRun = true
    @State private var rotate: Int?
    @State private var page = 1
    @State private var region: Int?
    @State private var result: PrintedLabel?
    @State private var error: String?
    @State private var busy = false

    private let turns: [(String, Int?)] = [("Auto", nil), ("0°", 0),
                                           ("90°", 90), ("180°", 180),
                                           ("270°", 270)]

    var body: some View {
        MPScreen(eyebrow: "Any 4x6", title: "Print a label") {
            if let error { MPError(message: error) }

            Button { picking = true } label: {
                MPCard {
                    HStack(spacing: MP.S.x3) {
                        Image(systemName: "doc.text")
                            .font(.system(size: 20))
                            .foregroundStyle(MP.Palette.accent)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(name ?? "Choose a PDF")
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundStyle(MP.Palette.fg)
                                .lineLimit(2)
                            Text(name == nil
                                 ? "From Files, or whatever the seller sent"
                                 : "Tap to choose another")
                                .font(.system(size: 12))
                                .foregroundStyle(MP.Palette.muted)
                        }
                    }
                }
            }
            .buttonStyle(.plain)

            if pdf != nil {
                Button { dryRun.toggle(); result = nil; error = nil } label: {
                    MPCard {
                        HStack(spacing: MP.S.x3) {
                            Image(systemName: dryRun ? "checkmark.square.fill"
                                                     : "square")
                                .font(.system(size: 16))
                                .foregroundStyle(dryRun ? MP.Palette.accent
                                                        : MP.Palette.subtle)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Check only")
                                    .font(.system(size: 14, weight: .semibold))
                                    .foregroundStyle(MP.Palette.fg)
                                // This printer cannot report a failure,
                                // so a wrong crop costs a label and says
                                // nothing about it. Checking is free.
                                Text("Crop it and measure it. No label used.")
                                    .font(.system(size: 12))
                                    .foregroundStyle(MP.Palette.muted)
                            }
                        }
                    }
                }
                .buttonStyle(.plain)

                MPCard {
                    VStack(alignment: .leading, spacing: MP.S.x3) {
                        MPEyebrow("Turn")
                        MPPills(titles: turns.map(\.0),
                                selected: turns.first { $0.1 == rotate }?.0
                                          ?? "Auto") { title in
                            rotate = turns.first { $0.0 == title }?.1 ?? nil
                            forget()
                        }
                        Stepper(value: $page, in: 1...50) {
                            HStack {
                                Text("Page")
                                    .font(.system(size: 13))
                                    .foregroundStyle(MP.Palette.muted)
                                Spacer(minLength: MP.S.x2)
                                Text("\(page)")
                                    .font(.system(size: 14).monospaced())
                                    .foregroundStyle(MP.Palette.fg)
                            }
                        }
                        .onChange(of: page) { _, _ in forget() }
                    }
                }

                // Above the answer, not below it. The last two times a
                // screen in this app put its reference material between
                // the form and its primary action, the action ended up
                // under a fold and a screenshot was what found it.
                MPHoldButton(title: busy ? "Working…"
                                         : dryRun ? "Hold to check"
                                                  : "Hold to print",
                             enabled: !busy) { run() }
                    .padding(.top, MP.S.x1)
            }

            if let r = result {
                MPCard {
                    VStack(alignment: .leading, spacing: 0) {
                        MPRow(label: "Size",
                              value: "\(fmt(r.sizeIn.first)) x \(fmt(r.sizeIn.last)) in")
                        MPRow(label: "Turned",
                              value: "\(r.rotation)° · \(sourceWords(r))")
                        MPRow(label: "Page", value: "\(r.page)")
                        if r.dryRun != true {
                            MPRow(label: "Job", value: r.job, mono: true)
                            MPRow(label: "Recorded in", value: r.recorded)
                        }
                    }
                }

                if r.wasGuessed {
                    // The shape says the label is on its side. It cannot
                    // say which way up, and an upside-down label is a
                    // wasted one that looks like a bug in the crop.
                    MPNote(message: "There is no text on this label to read "
                           + "an orientation from, so that is its shape "
                           + "talking. It knows the label is on its side "
                           + "and not which way up - check it, and use Turn "
                           + "if it comes out upside down.")
                }

                if r.regionsFound > 1 {
                    MPCard {
                        VStack(alignment: .leading, spacing: MP.S.x2) {
                            MPEyebrow("Which block")
                            Text("This page has \(r.regionsFound) things on "
                                 + "it that could be the label. Check each "
                                 + "one before printing.")
                                .font(.system(size: 12))
                                .foregroundStyle(MP.Palette.muted)
                            MPPills(titles: (1...r.regionsFound).map(String.init),
                                    selected: String(region ?? r.region)) { pick in
                                region = Int(pick)
                                forget()
                            }
                        }
                    }
                }

                if r.dryRun == true {
                    Text("Nothing printed. Turn off Check only when it looks "
                         + "right.")
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.subtle)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }

            Text("A shipping label from anywhere else. It is not a sale and "
                 + "nothing here records it as one - it prints, and that is "
                 + "all. Sending the same file again is one job, not two "
                 + "labels.")
                .font(.system(size: 11.5))
                .foregroundStyle(MP.Palette.subtle)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, MP.S.x2)
        }
        .fileImporter(isPresented: $picking,
                      allowedContentTypes: [.pdf]) { outcome in
            take(outcome)
        }
    }

    /// Any option changes what the answer would be, so the old answer
    /// stops being about this. Leaving a crop on screen beside a
    /// rotation that has since changed is how somebody prints the thing
    /// they were just told not to.
    private func forget() {
        result = nil
        error = nil
    }

    private func fmt(_ v: Double?) -> String {
        guard let v else { return "?" }
        return v == v.rounded() ? String(Int(v)) : String(format: "%.2f", v)
    }

    private func sourceWords(_ r: PrintedLabel) -> String {
        switch r.rotationSource {
        case "aspect": return "guessed from its shape"
        case "forced": return "you chose it"
        default: return "read off the text"
        }
    }

    private func take(_ outcome: Result<URL, Error>) {
        switch outcome {
        case .failure(let err):
            error = err.localizedDescription
        case .success(let url):
            // A URL from the document picker is security-scoped and the
            // scope is not open by default. Reading without this fails
            // with a permissions error on a file she just chose in a
            // system picker, which reads as a bug in the app.
            let opened = url.startAccessingSecurityScopedResource()
            defer { if opened { url.stopAccessingSecurityScopedResource() } }
            do {
                pdf = try Data(contentsOf: url)
                name = url.lastPathComponent
                // Everything about the last file, gone with it. A page
                // number or a region left over from another PDF would
                // silently print a different part of this one.
                result = nil
                error = nil
                rotate = nil
                region = nil
                page = 1
                dryRun = true
            } catch {
                self.error = "That file could not be read: "
                           + error.localizedDescription
            }
        }
    }

    private func run() {
        guard let pdf, !busy else { return }
        busy = true
        error = nil
        let wasDry = dryRun
        Task {
            do {
                result = try await APIClient.shared.printLabel(
                    pdf, rotate: rotate, page: page, region: region,
                    dryRun: wasDry)
            } catch {
                // The server's sentence, not a status code. "This may not
                // be a shipping label" and "say which with --region" are
                // the whole point of the message, and only the person
                // holding the file can act on either.
                self.error = error.localizedDescription
                result = nil
            }
            busy = false
        }
    }
}
