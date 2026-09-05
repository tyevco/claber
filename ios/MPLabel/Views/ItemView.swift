//  ItemView.swift
//
//  One thing, and the only question usually being asked of it: where
//  does it go.

import SwiftUI

struct ItemView: View {
    let itemID: Int
    var preloaded: InventoryItem?

    @State private var item: InventoryItem?
    @State private var bins: [Bin] = []
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        List {
            if let error { ErrorBanner(message: error).listRowInsets(EdgeInsets()) }

            if let it = item {
                Section {
                    Text(it.title ?? "(untitled)").font(.headline)
                    LabeledContent("Price", value: money(it.price))
                    LabeledContent("State", value: it.state ?? "—")
                    if let code = it.inventoryCode {
                        LabeledContent("Code") {
                            Text(code).font(.system(.body, design: .monospaced))
                        }
                    }
                }

                Section {
                    Button { move(to: "") } label: {
                        HStack {
                            Text("No bin")
                            Spacer()
                            if it.binCode == nil {
                                Image(systemName: "checkmark")
                                    .foregroundStyle(Color.mpAccent)
                            }
                        }
                    }
                    .disabled(busy)

                    ForEach(bins) { bin in
                        Button { move(to: bin.code) } label: {
                            HStack {
                                Text(bin.name)
                                Spacer()
                                if bin.code == it.binCode {
                                    Image(systemName: "checkmark")
                                        .foregroundStyle(Color.mpAccent)
                                }
                            }
                        }
                        .disabled(busy)
                    }
                } header: {
                    Text("Where it is")
                } footer: {
                    // No save button and no history, and both are
                    // deliberate: this records where a thing *is*, which
                    // is the question being asked.
                    Text("Takes effect immediately."
                         + (it.binMates.map { $0 > 0
                            ? " \($0) other thing\($0 == 1 ? "" : "s") in there."
                            : "" } ?? ""))
                }
            } else {
                ProgressView()
            }
        }
        .navigationTitle(item?.inventoryCode ?? "Item")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if item == nil { item = preloaded }
            await load()
            bins = (try? await APIClient.shared.bins()) ?? []
        }
    }

    private func load() async {
        do {
            item = try await APIClient.shared.item(itemID)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func move(to code: String) {
        guard !busy else { return }
        busy = true
        Task {
            do {
                try await APIClient.shared.move(item: itemID, toBin: code)
                error = nil
                await load()
                bins = (try? await APIClient.shared.bins()) ?? bins
            } catch {
                // "no bin FLOOR. Make it first - a thing cannot be
                // somewhere that has no name" is the server's sentence,
                // and it says what to do next.
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}
