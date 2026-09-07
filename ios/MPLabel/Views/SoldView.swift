//  SoldView.swift
//
//  What went out, newest first.
//
//  The design shows a margin column here. There is no margin yet: the
//  database has a price and no cost, so every margin this screen could
//  draw would be invented. It shows what is known instead and says why
//  the rest is missing - which is the same rule the PWA follows on the
//  sourcing screens, and the reason those are still placeholders.

import SwiftUI

struct SoldView: View {
    @State private var rows: [InventoryItem] = []
    @State private var error: String?
    @State private var loading = false

    /// Gross, not profit. Naming it "takings" rather than "earnings"
    /// keeps the difference visible while there is no cost basis.
    private var takings: Double { rows.compactMap(\.price).reduce(0, +) }

    /// What the number above it is and is not.
    ///
    /// This said flatly that there was no cost basis in the database,
    /// which was true when it was written and went on being said after
    /// she started entering one. It counts now - and still says what is
    /// missing, because postage is per parcel and Facebook's fee has
    /// never been confirmed against a real payout.
    private var soldCaveat: String {
        let costed = rows.filter { $0.paid != nil }.count
        if rows.isEmpty { return "" }
        if costed == 0 {
            return "Takings are gross: none of these has a cost against "
                 + "it, so nothing here is profit. Triage is where cost "
                 + "gets in."
        }
        let kept = rows.compactMap { item -> Double? in
            guard let price = item.price, let paid = item.paid else {
                return nil
            }
            return price - paid
        }.reduce(0, +)
        let scope = costed == rows.count
            ? "all of them"
            : "\(costed) of \(rows.count)"
        return "Kept " + money(kept) + " on " + scope
             + " after cost. Postage is not in that, and neither is "
             + "Facebook's fee - no payout has ever been seen to confirm "
             + "one."
    }

    /// Only over the rows that have both dates. Most of the saved-page
    /// import has neither, so averaging across everything would report a
    /// speed she never achieved.
    private var typicalDays: Int? {
        let known = rows.compactMap(\.daysToSell)
        guard !known.isEmpty else { return nil }
        return known.reduce(0, +) / known.count
    }

    // No NavigationStack of its own: this is pushed from Profit, and a
    // stack inside a stack gives two back buttons and a navigation state
    // neither of them owns.
    var body: some View {
        Group {
            MPScreen(eyebrow: "Sold", title: "What went out") {
                if let error { MPError(message: error) }

                if !rows.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: MP.S.x2) {
                            MPChip(value: "\(rows.count)", label: "sold")
                            MPChip(value: money(takings), label: "taken")
                            MPChip(value: typicalDays.map { "\($0)d" } ?? "—",
                                   label: "typical")
                        }
                    }
                    .padding(.bottom, MP.S.x1)
                }

                ForEach(rows) { item in
                    NavigationLink(value: item.id) {
                        MPCard { SoldRow(item: item) }
                    }
                    .buttonStyle(.plain)
                }

                if rows.isEmpty && !loading {
                    MPEmpty(title: "Nothing sold yet",
                            detail: "Sales appear here once the poller has "
                                  + "seen them.",
                            symbol: "bag")
                }

                if !rows.isEmpty {
                    Text(soldCaveat)
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.subtle)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, MP.S.x2)
                }
            }
            .refreshable { await load() }
            .task { await load() }
            .overlay { if loading && rows.isEmpty { ProgressView() } }
        }
        .navigationBarTitleDisplayMode(.inline)
    }

    private func load() async {
        loading = true
        do {
            rows = try await APIClient.shared.sold()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }
}

struct SoldRow: View {
    let item: InventoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: MP.S.x1) {
            Text(item.title ?? "(untitled)")
                .font(.system(size: 14))
                .foregroundStyle(MP.Palette.fg)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
            HStack(spacing: MP.S.x2) {
                Text(money(item.price))
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(MP.Palette.fg)
                if let days = item.daysToSell {
                    Text("\(days) day\(days == 1 ? "" : "s") to sell")
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.muted)
                }
                Spacer(minLength: 0)
                if let sold = item.soldAt {
                    Text(String(sold.prefix(10)))
                        .font(.system(size: 11).monospaced())
                        .foregroundStyle(MP.Palette.subtle)
                }
            }
        }
    }
}
