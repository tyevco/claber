//  QueueView.swift
//
//  What has to go out. Sorted by the server (ship_by, nulls last),
//  which is the order she works in.

import SwiftUI

struct QueueView: View {
    @State private var orders: [Order] = []
    @State private var error: String?
    @State private var loading = false

    var body: some View {
        NavigationStack {
            List {
                if let error {
                    ErrorBanner(message: error).listRowInsets(EdgeInsets())
                }
                if orders.isEmpty && !loading {
                    ContentUnavailableView("Nothing to ship",
                                           systemImage: "checkmark.circle",
                                           description: Text("Every order is away."))
                }
                ForEach(orders) { order in
                    NavigationLink(value: order.id) {
                        OrderRow(order: order)
                    }
                }
            }
            .navigationTitle("To ship")
            .navigationDestination(for: Int.self) { id in
                OrderDetailView(orderID: id, onChange: { Task { await load() } })
            }
            .refreshable { await load() }
            .task { await load() }
            .overlay { if loading && orders.isEmpty { ProgressView() } }
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
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                // The parcel code is a handle, not just a marking:
                // `reprint` and `ship` both take it, and it is the only
                // identifier printed on the box.
                Text(order.code ?? "—")
                    .font(.system(.subheadline, design: .monospaced))
                    .fontWeight(.semibold)
                Text(due.label)
                    .font(.caption2)
                    .fontWeight(.bold)
                    .foregroundStyle(due.urgent ? Color.mpAlert : Color.mpMuted)
                Spacer()
                if !order.printed && order.hasLabel {
                    Image(systemName: "printer")
                        .font(.caption)
                        .foregroundStyle(Color.mpMuted)
                }
            }
            Text(order.item ?? "(no item)")
                .font(.subheadline)
                .lineLimit(2)
            HStack(spacing: 6) {
                if let buyer = order.buyer { Text(buyer) }
                Text(money(order.price))
            }
            .font(.caption)
            .foregroundStyle(Color.mpMuted)

            // A print failure is written to sales.notes, and without
            // showing it here the note only exists on the detail screen
            // of an order she has no reason to suspect.
            if let notes = order.notes, !notes.isEmpty {
                Text(notes)
                    .font(.caption2)
                    .foregroundStyle(Color.mpAlert)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, 2)
    }
}
