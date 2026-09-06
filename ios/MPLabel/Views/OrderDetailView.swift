//  OrderDetailView.swift
//
//  One order, and the two things that actually happen to it: a label
//  comes out, and it goes in the post. Both are held rather than
//  tapped - see MPHoldButton for why.

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
        MPScreen(eyebrow: detail?.status ?? "Order",
                 title: detail?.code ?? "—") {
            if let error { MPError(message: error) }
            if let note { MPNote(message: note) }

            if let d = detail {
                MPCard {
                    VStack(alignment: .leading, spacing: MP.S.x2) {
                        Text(d.item ?? "(no item)")
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundStyle(MP.Palette.fg)
                        HStack(spacing: MP.S.x2) {
                            MPTag(text: Due(shipBy: d.shipBy).label,
                                  style: Due(shipBy: d.shipBy).urgent
                                         ? .alert : .normal)
                            Text(money(d.price))
                                .font(.system(size: 13))
                                .foregroundStyle(MP.Palette.muted)
                        }
                    }
                }

                MPCard {
                    VStack(spacing: 0) {
                        MPRow(label: "Sold for", value: money(d.price))
                        MPRow(label: "Ship by", value: d.shipBy ?? "—")
                        MPRow(label: "Buyer", value: d.buyer ?? "—")
                        if let w = d.weight { MPRow(label: "Weight", value: w) }
                        if let s = d.service { MPRow(label: "Service", value: s) }
                    }
                }

                // The address is the one genuinely sensitive thing in
                // this app, and it is on this screen only - the queue
                // payload has no such field at all.
                MPCard {
                    VStack(alignment: .leading, spacing: MP.S.x1) {
                        MPEyebrow("Ships to")
                        Text(d.shipTo ?? "—")
                            .font(.system(size: 14))
                            .foregroundStyle(MP.Palette.fg)
                            .textSelection(.enabled)
                        if let t = d.tracking, !t.isEmpty {
                            Text(t)
                                .font(.system(size: 11.5).monospaced())
                                .foregroundStyle(MP.Palette.muted)
                                .textSelection(.enabled)
                                .padding(.top, MP.S.x1)
                        }
                    }
                }

                if let notes = d.notes, !notes.isEmpty {
                    MPError(message: notes)
                }

                VStack(spacing: MP.S.x2) {
                    MPHoldButton(title: d.printed ? "Hold to print again"
                                                  : "Hold to print label",
                                 enabled: !busy && d.hasLabel) {
                        act { code in
                            let printed = try await APIClient.shared.printLabel(d.id)
                            return "Label \(printed ?? code ?? "") sent to the printer."
                        }
                    }
                    MPHoldButton(title: "Hold to mark shipped",
                                 enabled: !busy) {
                        act { _ in
                            try await APIClient.shared.markShipped(d.id)
                            return "Code \(d.code ?? "") is shipped and free again."
                        }
                    }
                }
                .padding(.top, MP.S.x2)

                Group {
                    if !d.hasLabel {
                        Text("No label file for this one - a local pickup "
                             + "sale never gets one.")
                    } else if d.printed {
                        // A successful write is not proof a label came
                        // out: the printer is write-only, so the paper
                        // is the only source of truth. Saying "printed"
                        // flatly would overstate what is known.
                        Text("Recorded as printed"
                             + (d.printCount.map { " \($0)x" } ?? "")
                             + ". The printer cannot confirm it, so the "
                             + "paper is the only proof.")
                    }
                }
                .font(.system(size: 11.5))
                .foregroundStyle(MP.Palette.subtle)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, MP.S.x1)
            } else {
                ProgressView().padding(.top, MP.S.x7)
            }
        }
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
