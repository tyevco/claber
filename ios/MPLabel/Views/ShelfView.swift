//  ShelfView.swift
//
//  Where things are. One search across title, bin name, bin code,
//  category and inventory code, because that is how a thing is actually
//  looked for - "the blue one", "ATTIC", "glass" - and separate filters
//  would make her choose which kind of remembering she is doing before
//  she has remembered.

import SwiftUI

struct ShelfView: View {
    @State private var items: [InventoryItem] = []
    @State private var bins: [Bin] = []
    @State private var query = ""
    @State private var error: String?
    @State private var note: String?
    @State private var newBinName = ""
    @State private var askingForBin = false
    @State private var addingItem = false
    @State private var trips: [Trip] = []
    @State private var path = NavigationPath()

    var body: some View {
        NavigationStack(path: $path) {
            MPScreen(eyebrow: "Shelf", title: "Where things are") {
                // One affordance, two things that can be new here: a
                // place, and a thing to put in one. A second button in
                // the header would make her read two icons to find out
                // they are not the same plus.
                Menu {
                    Button("New item") { addingItem = true }
                    Button("New bin") { askingForBin = true }
                } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(MP.Palette.fg)
                        .frame(width: 40, height: 40)
                        .background(MP.Palette.raised,
                                    in: RoundedRectangle(cornerRadius: MP.R.chip))
                        .overlay(RoundedRectangle(cornerRadius: MP.R.chip)
                            .strokeBorder(MP.Palette.border, lineWidth: 1))
                }
                .accessibilityLabel("Add")
            } content: {
                if let error { MPError(message: error) }
                if let note { MPNote(message: note) }

                // The way to the sourcing runs, and it carries the one
                // number worth interrupting her for: money that came out
                // of a till and has not been attached to anything yet.
                // On the shelf because that is where she is when the
                // question "what did this cost" occurs to her.
                Button { path.append(Runs()) } label: {
                    MPCard {
                        HStack(spacing: MP.S.x2) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Sourcing runs")
                                    .font(.system(size: 14, weight: .semibold))
                                    .foregroundStyle(MP.Palette.fg)
                                Text(runsLine)
                                    .font(.system(size: 11.5))
                                    .foregroundStyle(MP.Palette.muted)
                            }
                            Spacer(minLength: MP.S.x2)
                            Image(systemName: "chevron.right")
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(MP.Palette.subtle)
                        }
                    }
                }
                .buttonStyle(.plain)

                if !bins.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: MP.S.x2) {
                            ForEach(bins) { bin in
                                Button { path.append(bin.code) } label: {
                                    // The name is what she reads across a
                                    // room; the code is for a scanner.
                                    MPChip(value: bin.name,
                                           label: "\(bin.count ?? 0) item"
                                                + ((bin.count ?? 0) == 1 ? "" : "s"))
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    .padding(.bottom, MP.S.x1)
                }

                ForEach(items) { item in
                    Button { path.append(item.id) } label: {
                        MPCard { ItemRow(item: item) }
                    }
                    .buttonStyle(.plain)
                }

                if items.isEmpty {
                    MPEmpty(title: "Nothing here",
                            detail: query.isEmpty
                                ? "No listings imported yet."
                                : "No item matches that.",
                            symbol: "shippingbox")
                }
            }
            .searchable(text: $query, prompt: "Title, bin, category or code")
            .task(id: query) {
                // Debounced: this is a phone on house Wi-Fi talking to a
                // Pi, and a request per keystroke makes the list jump
                // under her thumb. A cancelled task swallows the sleep,
                // so there is no timer to manage.
                try? await Task.sleep(for: .milliseconds(220))
                await loadItems()
            }
            .task { await loadBins() }
            .task { await loadTrips() }
            .refreshable { await loadItems(); await loadBins() }
            .navigationDestination(for: Int.self) { ItemView(itemID: $0) }
            .navigationDestination(for: TripRef.self) { TripView(tripID: $0.id) }
            .navigationDestination(for: Runs.self) { _ in TripsView() }
            // A sheet, like the settings screen: adding a thing is a
            // detour from looking at the shelf, not a place in it.
            .sheet(isPresented: $addingItem) {
                NavigationStack {
                    AddItemView(onSaved: { _ in Task { await loadItems() } })
                }
            }
            .navigationDestination(for: String.self) { BinView(code: $0) }
            .alert("Name this place", isPresented: $askingForBin) {
                TextField("FLOOR, ATTIC, B5…", text: $newBinName)
                Button("Cancel", role: .cancel) { newBinName = "" }
                Button("Make it") { makeBin() }
            } message: {
                Text("The code is minted for you - print its tag afterwards.")
            }
        }
    }

    private var runsLine: String {
        guard !trips.isEmpty else { return "Where the things came from" }
        // Null is not zero: a run with no recorded till total cannot say
        // how much is unattributed, and summing it as 0 would report
        // "all accounted for" about a question nobody has asked yet.
        let owed = trips.compactMap(\.unassigned).reduce(0, +)
        if owed <= 0 { return "\(trips.count) runs, every penny attributed" }
        return "\(trips.count) runs - " + money(owed) + " has no home yet"
    }

    private func loadTrips() async {
        trips = (try? await APIClient.shared.trips()) ?? []
    }

    private func loadItems() async {
        do {
            items = try await APIClient.shared.inventory(query: query)
            error = nil
        } catch is CancellationError {
            // A newer keystroke replaced this one. Not a failure.
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func loadBins() async {
        bins = (try? await APIClient.shared.bins()) ?? bins
    }

    private func makeBin() {
        let name = newBinName
        newBinName = ""
        guard !name.isEmpty else { return }
        Task {
            do {
                let made = try await APIClient.shared.makeBin(name: name)
                error = nil
                await loadBins()
                // The code is what she needs next, because the tag has
                // to be printed for it.
                note = "\(made.name) is \(made.code) — print its tag"
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

struct ItemRow: View {
    let item: InventoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: MP.S.x1) {
            Text(item.title ?? "(untitled)")
                .font(.system(size: 14))
                .foregroundStyle(MP.Palette.fg)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
            HStack(spacing: MP.S.x2) {
                if let bin = item.bin {
                    MPTag(text: bin)
                } else {
                    // Not a missing value. It is what a thing in her
                    // hand is, on its way somewhere.
                    MPTag(text: "No bin", style: .absent)
                }
                if let code = item.inventoryCode {
                    Text(code)
                        .font(.system(size: 11).monospaced())
                        .foregroundStyle(MP.Palette.subtle)
                }
                if item.state == "sold" {
                    Text("sold")
                        .font(.system(size: 11))
                        .foregroundStyle(MP.Palette.subtle)
                }
                Spacer(minLength: 0)
                Text(money(item.price))
                    .font(.system(size: 12))
                    .foregroundStyle(MP.Palette.muted)
            }
        }
    }
}

struct BinView: View {
    let code: String
    @State private var contents: BinContents?

    var body: some View {
        MPScreen(eyebrow: "Bin \(code)",
                 title: contents?.bin.name ?? code) {
            if let c = contents {
                if c.items.isEmpty {
                    MPEmpty(title: "Empty",
                            detail: "Nothing is in this bin. It still exists - "
                                  + "someone named it and printed its tag.",
                            symbol: "tray")
                }
                ForEach(c.items) { item in
                    NavigationLink(value: item.id) {
                        MPCard { ItemRow(item: item) }
                    }
                    .buttonStyle(.plain)
                }
            } else {
                ProgressView().padding(.top, MP.S.x7)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .task { contents = try? await APIClient.shared.binContents(code) }
    }
}
