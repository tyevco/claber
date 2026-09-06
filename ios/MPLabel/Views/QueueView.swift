//  QueueView.swift
//
//  What has to go out. Sorted by the server (ship_by, nulls last),
//  which is the order she works in.

import SwiftUI

struct QueueView: View {
    @State private var orders: [Order] = []
    @State private var error: String?
    @State private var loading = false
    @State private var path: [Int] = []
    @State private var showingSettings = false

    /// Her own summary of the day, in the design's chip strip. Counts
    /// rather than money: this screen is about what has to happen, and
    /// the takings are a different screen.
    private var unprinted: Int { orders.filter { !$0.printed && $0.hasLabel }.count }
    private var overdue: Int { orders.filter { Due(shipBy: $0.shipBy).urgent }.count }

    var body: some View {
        NavigationStack(path: $path) {
            MPScreen(eyebrow: Date.now.formatted(.dateTime.weekday(.wide)
                                                 .day().month(.wide)),
                     title: "To ship") {
                Button { showingSettings = true } label: {
                    Image(systemName: "gearshape")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(MP.Palette.fg)
                        .frame(width: 40, height: 40)
                        .background(MP.Palette.raised,
                                    in: RoundedRectangle(cornerRadius: MP.R.chip))
                        .overlay(RoundedRectangle(cornerRadius: MP.R.chip)
                            .strokeBorder(MP.Palette.border, lineWidth: 1))
                }
                .accessibilityLabel("Settings")
            } content: {
                if let error { MPError(message: error) }

                if !orders.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: MP.S.x2) {
                            MPChip(value: "\(orders.count)", label: "open")
                            MPChip(value: "\(unprinted)", label: "to print")
                            MPChip(value: "\(overdue)", label: "due now",
                                   tint: overdue > 0 ? MP.Palette.alert
                                                     : MP.Palette.fg)
                        }
                    }
                    .padding(.bottom, MP.S.x1)
                }

                ForEach(orders) { order in
                    Button { path.append(order.id) } label: {
                        MPCard { OrderRow(order: order) }
                    }
                    .buttonStyle(.plain)
                }

                if orders.isEmpty && !loading {
                    MPEmpty(title: "Nothing to ship",
                            detail: "Every order is away.",
                            symbol: "checkmark.circle")
                }
            }
            .navigationDestination(for: Int.self) { id in
                OrderDetailView(orderID: id, onChange: { Task { await load() } })
            }
            .refreshable { await load() }
            .task { await load() }
            .overlay { if loading && orders.isEmpty { ProgressView() } }
            .sheet(isPresented: $showingSettings) { SettingsView() }
        }
    }

    private func load() async {
        loading = true
        do {
            orders = try await APIClient.shared.orders()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }
}

struct OrderRow: View {
    let order: Order

    var body: some View {
        let due = Due(shipBy: order.shipBy)
        VStack(alignment: .leading, spacing: MP.S.x1) {
            HStack(spacing: MP.S.x2) {
                // The parcel code is a handle, not just a marking:
                // `reprint` and `ship` both take it, and it is the only
                // identifier printed on the box. Monospaced so the
                // characters line up against what is on the label.
                Text(order.code ?? "—")
                    .font(.system(size: 15, weight: .semibold).monospaced())
                    .foregroundStyle(MP.Palette.fg)
                Text(due.label)
                    .font(.system(size: 10.5, weight: .bold))
                    .tracking(0.6)
                    .foregroundStyle(due.urgent ? MP.Palette.alert
                                                : MP.Palette.subtle)
                Spacer(minLength: MP.S.x2)
                if !order.printed && order.hasLabel {
                    Image(systemName: "printer")
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.muted)
                }
            }
            Text(order.item ?? "(no item)")
                .font(.system(size: 14))
                .foregroundStyle(MP.Palette.fg)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
            HStack(spacing: MP.S.x2) {
                if let buyer = order.buyer { Text(buyer) }
                Text(money(order.price))
            }
            .font(.system(size: 12))
            .foregroundStyle(MP.Palette.muted)

            // A print failure is written to sales.notes, and without
            // showing it here the note only exists on the detail screen
            // of an order she has no reason to suspect.
            if let notes = order.notes, !notes.isEmpty {
                Text(notes)
                    .font(.system(size: 11.5))
                    .foregroundStyle(MP.Palette.alert)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
            }
        }
    }
}

/// The design's empty state: quiet, and it says what the emptiness
/// means rather than just that there is nothing here.
struct MPEmpty: View {
    let title: String
    let detail: String
    var symbol = "tray"

    var body: some View {
        VStack(spacing: MP.S.x2) {
            Image(systemName: symbol)
                .font(.system(size: 26))
                .foregroundStyle(MP.Palette.subtle)
            Text(title)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(MP.Palette.fg)
            Text(detail)
                .font(.system(size: 13))
                .foregroundStyle(MP.Palette.muted)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, MP.S.x7)
    }
}
