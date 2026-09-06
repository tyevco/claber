//  ProfitView.swift
//
//  Sell-through by price, month by month, and what has sat longest.
//
//  Two honesty problems live on this screen, and both are stated on it
//  rather than hidden:
//
//  * **It is gross, not profit.** There is no cost basis in the
//    database. The screen is called Profit because the design calls it
//    that; every number on it is takings.
//  * **Sell-through is meaningless without prices on unsold listings.**
//    Prices only reach the database if the listing email or an import
//    carried one, and neither the saved-page capture nor the console
//    snippet carried dates at all - so `v_aging` can be empty while
//    looking merely quiet. If Aging shows blank prices, the percentages
//    above it are lying, and saying so is cheaper than a footnote
//    nobody reads.

import SwiftUI

struct ProfitView: View {
    @State private var stats: Stats?
    @State private var error: String?
    @State private var loading = false

    var body: some View {
        NavigationStack {
            MPScreen(eyebrow: "Margin", title: "Profit") {
                if let error { MPError(message: error) }

                // Sold lives under Profit rather than taking a fifth
                // tab. The design's own tab bar is four items, and what
                // sold is a question asked while looking at the takings,
                // not on the way past.
                NavigationLink(value: "sold") {
                    MPCard {
                        HStack {
                            Text("What went out")
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundStyle(MP.Palette.fg)
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(MP.Palette.subtle)
                        }
                    }
                }
                .buttonStyle(.plain)

                if let s = stats {
                    if let latest = s.monthly.first {
                        MPCard {
                            VStack(alignment: .leading, spacing: MP.S.x2) {
                                MPEyebrow(monthName(latest.month))
                                HStack(alignment: .firstTextBaseline,
                                       spacing: MP.S.x4) {
                                    figure(money(latest.gross), "taken")
                                    figure("\(latest.orders ?? 0)", "orders")
                                    figure(money(latest.avgOrder), "average")
                                }
                            }
                        }
                    }

                    section("Sell-through by price") {
                        ForEach(s.priceBands) { band in
                            MPCard { BandRow(band: band) }
                        }
                    }

                    section("By month") {
                        ForEach(s.monthly) { m in
                            MPCard {
                                HStack {
                                    Text(monthName(m.month))
                                        .font(.system(size: 13).monospaced())
                                        .foregroundStyle(MP.Palette.fg)
                                    Spacer()
                                    Text("\(m.orders ?? 0)")
                                        .font(.system(size: 12))
                                        .foregroundStyle(MP.Palette.muted)
                                    Text(money(m.gross))
                                        .font(.system(size: 13, weight: .semibold))
                                        .foregroundStyle(MP.Palette.fg)
                                        .frame(minWidth: 72, alignment: .trailing)
                                }
                            }
                        }
                    }

                    section("Longest listed") {
                        ForEach(s.aging) { a in
                            MPCard {
                                VStack(alignment: .leading, spacing: MP.S.x1) {
                                    Text(a.title ?? "(untitled)")
                                        .font(.system(size: 13))
                                        .foregroundStyle(MP.Palette.fg)
                                        .lineLimit(1)
                                    HStack(spacing: MP.S.x2) {
                                        Text("\(a.daysListed ?? 0) days")
                                            .font(.system(size: 11.5))
                                            .foregroundStyle(MP.Palette.warn)
                                        Text(money(a.price))
                                            .font(.system(size: 11.5))
                                            .foregroundStyle(MP.Palette.muted)
                                    }
                                }
                            }
                        }
                        if s.aging.isEmpty {
                            Text("Nothing here means no listing has both a "
                                 + "listed date and an active state - the "
                                 + "saved-page import carried no dates, so "
                                 + "this stays empty until a DYI export lands.")
                                .font(.system(size: 11.5))
                                .foregroundStyle(MP.Palette.subtle)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }

                    Text("Gross, not profit - there is no cost basis yet. "
                         + "And sell-through needs prices on unsold listings "
                         + "to mean anything; blank prices above mean the "
                         + "percentages are lying.")
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.subtle)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, MP.S.x3)
                } else if !loading {
                    MPEmpty(title: "No numbers yet",
                            detail: "Nothing has sold, or the listings have "
                                  + "not been imported.",
                            symbol: "chart.bar")
                }
            }
            .navigationDestination(for: String.self) { route in
                if route == "sold" { SoldView() }
            }
            .navigationDestination(for: Int.self) { ItemView(itemID: $0) }
            .refreshable { await load() }
            .task { await load() }
            .overlay { if loading && stats == nil { ProgressView() } }
        }
    }

    private func figure(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.system(size: 20, weight: .semibold))
                .foregroundStyle(MP.Palette.fg)
            Text(label)
                .font(.system(size: 10.5))
                .foregroundStyle(MP.Palette.muted)
        }
    }

    @ViewBuilder
    private func section<C: View>(_ title: String,
                                  @ViewBuilder content: () -> C) -> some View {
        MPEyebrow(title)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, MP.S.x3)
        content()
    }

    /// `2026-08` is what the view produces. Rendering it as a month name
    /// costs nothing and stops the screen reading like a database.
    private func monthName(_ raw: String?) -> String {
        guard let raw else { return "—" }
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM"
        guard let d = f.date(from: raw) else { return raw }
        f.dateFormat = "LLLL yyyy"
        return f.string(from: d)
    }

    private func load() async {
        loading = true
        do {
            stats = try await APIClient.shared.stats()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }
}

struct BandRow: View {
    let band: PriceBand

    var body: some View {
        VStack(alignment: .leading, spacing: MP.S.x2) {
            HStack {
                Text(band.priceBand)
                    .font(.system(size: 13, weight: .semibold).monospaced())
                    .foregroundStyle(MP.Palette.fg)
                Spacer()
                Text("\(band.sold ?? 0)/\(band.listed ?? 0)")
                    .font(.system(size: 12))
                    .foregroundStyle(MP.Palette.muted)
                Text(band.sellThroughPct.map { String(format: "%.0f%%", $0) }
                     ?? "—")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(MP.Palette.accent)
                    .frame(minWidth: 44, alignment: .trailing)
            }
            // A bar rather than a number alone: the comparison between
            // bands is the whole question this screen answers.
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(MP.Palette.border)
                    Capsule()
                        .fill(MP.Palette.accent)
                        .frame(width: geo.size.width
                               * CGFloat((band.sellThroughPct ?? 0) / 100))
                }
            }
            .frame(height: 5)
            if let days = band.avgDaysToSell {
                Text(String(format: "%.0f days to sell on average", days))
                    .font(.system(size: 11))
                    .foregroundStyle(MP.Palette.subtle)
            }
        }
    }
}
