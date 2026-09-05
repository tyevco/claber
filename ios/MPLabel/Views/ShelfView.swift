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
    @State private var newBinName = ""
    @State private var askingForBin = false

    var body: some View {
        NavigationStack {
            List {
                if let error { ErrorBanner(message: error).listRowInsets(EdgeInsets()) }

                if !bins.isEmpty {
                    Section("Bins") {
                        ForEach(bins) { bin in
                            NavigationLink(value: bin.code) {
                                HStack {
                                    // The name is what she reads across
                                    // a room; the code is for a scanner.
                                    Text(bin.name)
                                    Spacer()
                                    Text("\(bin.count ?? 0)")
                                        .foregroundStyle(Color.mpMuted)
                                        .font(.callout)
                                }
                            }
                        }
                    }
                }

                Section(items.isEmpty ? "" : "Items") {
                    ForEach(items) { item in
                        NavigationLink(value: item.id) { ItemRow(item: item) }
                    }
                }
            }
            .navigationTitle("Shelf")
            .searchable(text: $query, prompt: "Title, bin, category or code")
            .task(id: query) {
                // Debounced: this is a phone on house Wi-Fi talking to a
                // Pi, and a request per keystroke makes the list jump
                // under her thumb. A cancelled task swallows the sleep,
                // so no timer to manage.
                try? await Task.sleep(for: .milliseconds(220))
                await loadItems()
            }
            .task { await loadBins() }
            .refreshable { await loadItems(); await loadBins() }
            .navigationDestination(for: Int.self) { ItemView(itemID: $0) }
            .navigationDestination(for: String.self) { BinView(code: $0) }
            .toolbar {
                Button { askingForBin = true } label: {
                    Label("New bin", systemImage: "plus")
                }
            }
            .alert("Name this place", isPresented: $askingForBin) {
                TextField("FLOOR, ATTIC, B5…", text: $newBinName)
                Button("Cancel", role: .cancel) { newBinName = "" }
                Button("Make it") { makeBin() }
            } message: {
                Text("The code is minted for you - print its tag afterwards.")
            }
        }
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
                // The code is the thing she needs next, because the tag
                // has to be printed for it.
                self.error = "\(made.name) is \(made.code) — print its tag"
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

struct ItemRow: View {
    let item: InventoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(item.title ?? "(untitled)").font(.subheadline).lineLimit(2)
            HStack(spacing: 6) {
                if let bin = item.bin {
                    Text(bin)
                        .font(.caption2).fontWeight(.semibold)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Color.mpMuted.opacity(0.18),
                                    in: RoundedRectangle(cornerRadius: 5))
                } else {
                    // Not a missing value. It is what a thing in her
                    // hand is, on its way somewhere.
                    Text("No bin").font(.caption2).foregroundStyle(Color.mpMuted)
                }
                if let code = item.inventoryCode {
                    Text(code).font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(Color.mpMuted)
                }
                if item.state == "sold" {
                    Text("sold").font(.caption2).foregroundStyle(Color.mpMuted)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

struct BinView: View {
    let code: String
    @State private var contents: BinContents?

    var body: some View {
        List {
            if let c = contents {
                if c.items.isEmpty {
                    ContentUnavailableView(
                        "Empty", systemImage: "tray",
                        description: Text("Nothing is in this bin. It still "
                                          + "exists - someone named it and "
                                          + "printed its tag."))
                }
                ForEach(c.items) { item in
                    NavigationLink(value: item.id) { ItemRow(item: item) }
                }
            } else {
                ProgressView()
            }
        }
        .navigationTitle(contents?.bin.name ?? code)
        .navigationBarTitleDisplayMode(.inline)
        .task { contents = try? await APIClient.shared.binContents(code) }
    }
}
