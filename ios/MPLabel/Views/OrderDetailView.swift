//  OrderDetailView.swift
//
//  One order, and the two things that actually happen to it: a label
//  comes out, and it goes in the post.

import SwiftUI

struct OrderDetailView: View {
    var orderID: Int?
    var preloaded: OrderDetail?
    var onChange: (() -> Void)?

    @State private var detail: OrderDetail?
    @State private var error: String?
    @State private var note: String?
    /// One mutating action at a time. A slow connection invites a second
    /// tap, and two prints for the same sale collide over the same
    /// stamped temporary file server-side.
    @State private var busy = false

    var body: some View {
        List {
            if let error { ErrorBanner(message: error).listRowInsets(EdgeInsets()) }
            if let note {
                Text(note).font(.footnote).foregroundStyle(Color.mpAccent)
            }

            if let d = detail {
                Section {
                    LabeledContent("Code") {
                        Text(d.code ?? "—")
                            .font(.system(.body, design: .monospaced))
                    }
                    LabeledContent("Item", value: d.item ?? "—")
                    LabeledContent("Price", value: money(d.price))
                    LabeledContent("Due", value: Due(shipBy: d.shipBy).label)
                    LabeledContent("Status", value: d.status ?? "—")
                }

                Section("Posting to") {
                    Text(d.buyer ?? "—")
                    // The one genuinely sensitive field in the app. It
                    // arrives only on this screen because the queue
                    // payload has no address field at all.
                    Text(d.shipTo ?? "—")
                        .font(.callout)
                        .foregroundStyle(Color.mpMuted)
                        .textSelection(.enabled)
                    if let t = d.tracking, !t.isEmpty {
                        LabeledContent("Tracking") {
                            Text(t)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                        }
                    }
                    if let s = d.service { LabeledContent("Service", value: s) }
                    if let w = d.weight { LabeledContent("Weight", value: w) }
                }

                Section {
                    Button {
                        act { code in
                            let printed = try await APIClient.shared.printLabel(d.id)
                            return "Label \(printed ?? code ?? "") sent to the printer."
                        }
                    } label: {
                        Label(d.printed ? "Print again" : "Print label",
                              systemImage: "printer")
                    }
                    .disabled(busy || !d.hasLabel)

                    Button {
                        act { _ in
                            try await APIClient.shared.markShipped(d.id)
                            return "Code \(d.code ?? "") is shipped and free again."
                        }
                    } label: {
                        Label("Mark shipped", systemImage: "checkmark.circle")
                    }
                    .disabled(busy)
                } footer: {
                    if !d.hasLabel {
                        Text("No label file for this one - a local pickup "
                             + "sale never gets one.")
                    } else if d.printed {
                        // A successful write is not proof a label came
                        // out: the printer is write-only, so the paper is
                        // the only source of truth. Saying "printed" flatly
                        // would overstate what is known.
                        Text("Recorded as printed"
                             + (d.printCount.map { " \($0)x" } ?? "")
                             + ". The printer cannot confirm it, so the "
                             + "paper is the only proof.")
                    }
                }

                if let notes = d.notes, !notes.isEmpty {
                    Section("Note") {
                        Text(notes).foregroundStyle(Color.mpAlert)
                    }
                }
            } else {
                ProgressView()
            }
        }
        .navigationTitle(detail?.code ?? "Order")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func load() async {
        if let preloaded, detail == nil { detail = preloaded }
        guard let id = orderID ?? preloaded?.id else { return }
        do {
            detail = try await APIClient.shared.order(id)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func act(_ work: @escaping (String?) async throws -> String) {
        guard !busy else { return }
        busy = true
        note = nil
        error = nil
        Task {
            do {
                note = try await work(detail?.code)
                await load()
                onChange?()
            } catch {
                // The server refuses a reprint when the archived label no
                // longer matches the sale it is filed against. That is
                // the backstop against a parcel posted to a stranger, so
                // its sentence is shown rather than softened.
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}
