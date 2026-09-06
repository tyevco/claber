//  TripView.swift
//
//  One shop visit, and what happened to the money.
//
//  The three numbers across the top are the whole screen: what the till
//  said, what the things she brought home are listed for, and what is
//  still unattributed. That last one is the only number in this app that
//  is a *question* rather than a fact - it is the money whose object she
//  has not named yet, and it is null rather than zero when nobody wrote
//  the receipt total down. Those are different answers and the screen
//  says so in words rather than showing a confident 0.00.
//
//  There is no receipt line-item table behind this and there is not
//  going to be: a thrift receipt itemises by department - "HOUSEWARES
//  $4.99" - so a line is not an object, and parsing one into rows would
//  invent a precision the paper does not have. The photograph is the
//  record; the attribution is hers.

import SwiftUI

/// Every run, newest first. The way in to one of them.
struct TripsView: View {
    @State private var trips: [Trip] = []
    @State private var loading = true
    @State private var error: String?

    var body: some View {
        MPScreen(eyebrow: "Sourcing", title: "Runs") {
            if let error { MPError(message: error) }

            if loading && trips.isEmpty {
                ProgressView().padding(.top, MP.S.x7)
            } else if trips.isEmpty {
                MPEmpty(title: "No runs yet",
                        detail: "A run is one shop on one day. Triage makes "
                              + "one from the receipt in front of you.",
                        symbol: "bag")
            }

            ForEach(trips) { trip in
                NavigationLink(value: TripRef(id: trip.id)) {
                    MPCard { TripRow(trip: trip) }
                }
                .buttonStyle(.plain)
            }
        }
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        loading = true
        do { trips = try await APIClient.shared.trips() }
        catch { self.error = error.localizedDescription }
        loading = false
    }
}

/// "Show me the runs" as a value, so the shelf can push the list with
/// the same `NavigationPath` it uses for everything else.
struct Runs: Hashable {}

/// A trip id in a `NavigationPath`. Wrapped rather than pushed as an
/// `Int`, because the shelf already pushes item ids as `Int` and the two
/// would be indistinguishable - a tap on a run would open whatever
/// listing happened to share its number.
struct TripRef: Hashable {
    let id: Int
}

private struct TripRow: View {
    let trip: Trip

    var body: some View {
        VStack(alignment: .leading, spacing: MP.S.x2) {
            HStack(alignment: .firstTextBaseline) {
                Text(trip.store)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(MP.Palette.fg)
                Spacer(minLength: MP.S.x2)
                Text(money(trip.receiptTotal))
                    .font(.system(size: 14).monospaced())
                    .foregroundStyle(MP.Palette.fg)
            }
            HStack(spacing: MP.S.x2) {
                Text(trip.occurredAt ?? "—")
                    .font(.system(size: 11.5))
                    .foregroundStyle(MP.Palette.muted)
                Text("\(trip.items ?? 0) things")
                    .font(.system(size: 11.5))
                    .foregroundStyle(MP.Palette.muted)
                Spacer(minLength: MP.S.x2)
                if let left = trip.unassigned, left > 0 {
                    MPTag(text: money(left) + " unattributed", style: .alert)
                }
            }
        }
    }
}

struct TripView: View {
    let tripID: Int

    @State private var detail: TripDetail?
    @State private var error: String?

    var body: some View {
        MPScreen(eyebrow: detail.map { $0.trip.occurredAt ?? "One run" }
                          ?? "One run",
                 title: detail?.trip.store ?? "—") {
            if let error { MPError(message: error) }

            if let detail {
                figures(for: detail.trip)
                cameHome(detail)
                if !detail.photos.isEmpty { receipts(detail) }
            } else {
                ProgressView().padding(.top, MP.S.x7)
            }
        }
        .task { await load() }
    }

    /// Named `figures`, not `money`: a method called `money` on this
    /// view shadows the global formatter of the same name, and every
    /// call inside it then resolves to the view builder.
    private func figures(for trip: Trip) -> some View {
        MPCard {
            HStack(alignment: .top, spacing: MP.S.x2) {
                figure(money(trip.receiptTotal), "spent")
                figure(money(trip.listedFor), "listed for")
                figure(trip.unassigned.map { money($0) } ?? "—", "unattributed",
                       tint: (trip.unassigned ?? 0) > 0 ? MP.Palette.alert
                                                        : MP.Palette.fg)
            }
        }
    }

    private func figure(_ value: String, _ label: String,
                        tint: Color = MP.Palette.fg) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.system(size: 17, weight: .semibold).monospaced())
                .foregroundStyle(tint)
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(MP.Palette.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func cameHome(_ detail: TripDetail) -> some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("What came home")
                if detail.items.isEmpty {
                    Text("Nothing is attributed to this run yet. Triage is "
                         + "where that happens.")
                        .font(.system(size: 12.5))
                        .foregroundStyle(MP.Palette.muted)
                }
                ForEach(detail.items) { item in
                    NavigationLink(value: item.id) {
                        HStack(alignment: .firstTextBaseline, spacing: MP.S.x2) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.title ?? "(untitled)")
                                    .font(.system(size: 13.5))
                                    .foregroundStyle(MP.Palette.fg)
                                    .lineLimit(2)
                                Text("paid " + money(item.paid))
                                    .font(.system(size: 11).monospaced())
                                    .foregroundStyle(MP.Palette.subtle)
                            }
                            Spacer(minLength: MP.S.x2)
                            Text(money(item.price))
                                .font(.system(size: 13).monospaced())
                                .foregroundStyle(MP.Palette.muted)
                        }
                        .padding(.vertical, MP.S.x1)
                    }
                    .buttonStyle(.plain)
                }
                if let left = detail.trip.unassigned, left > 0 {
                    Text(money(left) + " of this run has no home yet. "
                         + "Leftover cost stays on the run, so a $4 lot of "
                         + "four things still shows up in profit.")
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.subtle)
                        .padding(.top, MP.S.x1)
                } else if detail.trip.unassigned == nil {
                    Text("No receipt total was recorded for this run, so "
                         + "there is nothing to reconcile against.")
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.subtle)
                        .padding(.top, MP.S.x1)
                }
            }
        }
    }

    private func receipts(_ detail: TripDetail) -> some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("The receipt")
                Text("\(detail.photos.count) filed against this run.")
                    .font(.system(size: 12.5))
                    .foregroundStyle(MP.Palette.muted)
            }
        }
    }

    private func load() async {
        do { detail = try await APIClient.shared.trip(tripID) }
        catch { self.error = error.localizedDescription }
    }
}
