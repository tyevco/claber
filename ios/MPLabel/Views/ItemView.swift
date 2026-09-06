//  ItemView.swift
//
//  One thing, and the only question usually being asked of it: where
//  does it go.

import SwiftUI
import UIKit

struct ItemView: View {
    let itemID: Int
    var preloaded: InventoryItem?

    @State private var item: InventoryItem?
    @State private var bins: [Bin] = []
    @State private var error: String?
    @State private var busy = false
    // The listing kit, for something already on a shelf. Most of her
    // stock exists as a row long before she gets round to writing it up,
    // so this is the commoner case than drafting at the moment of entry.
    @State private var draft: String?
    @State private var drafting = false
    @State private var modelError: String?

    var body: some View {
        MPScreen(eyebrow: "Item",
                 title: item?.inventoryCode ?? "—") {
            if let error { MPError(message: error) }

            if let it = item {
                MPCard {
                    VStack(alignment: .leading, spacing: MP.S.x2) {
                        Text(it.title ?? "(untitled)")
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundStyle(MP.Palette.fg)
                        HStack(spacing: MP.S.x2) {
                            Text(money(it.price))
                                .font(.system(size: 13))
                                .foregroundStyle(MP.Palette.muted)
                            if let state = it.state { MPTag(text: state) }
                        }
                    }
                }

                MPCard {
                    VStack(spacing: 0) {
                        MPRow(label: "Bin", value: it.bin ?? "Not set")
                        if let mates = it.binMates {
                            MPRow(label: "With it in there", value: "\(mates)")
                        }
                        if let cat = it.category {
                            MPRow(label: "Category", value: cat)
                        }
                    }
                }

                MPEyebrow("Where it is")
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, MP.S.x2)

                // "No bin" first, and it is a real answer rather than a
                // missing one - it is what a thing in her hand is, on
                // its way somewhere.
                binButton(name: "No bin", detail: "Off the shelf",
                          selected: it.binCode == nil) { move(to: "") }

                ForEach(bins) { bin in
                    binButton(name: bin.name,
                              detail: "\(bin.count ?? 0) item"
                                    + ((bin.count ?? 0) == 1 ? "" : "s"),
                              selected: bin.code == it.binCode) {
                        move(to: bin.code)
                    }
                }

                // No save button and no history, and both are
                // deliberate: this records where a thing *is*, which is
                // the question being asked.
                Text("Takes effect immediately. There is no history - this "
                     + "records where a thing is, not where it has been.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(MP.Palette.subtle)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, MP.S.x1)

                listingKit(for: it)
            } else {
                ProgressView().padding(.top, MP.S.x7)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if item == nil { item = preloaded }
            await load()
            bins = (try? await APIClient.shared.bins()) ?? []
        }
    }

    /// A description drafted from what is already recorded about this
    /// thing. Same call the new-item screen makes, and the same rules:
    /// it invents nothing it was not given, and what it writes is a
    /// draft she edits rather than anything the app stores.
    private func listingKit(for it: InventoryItem) -> some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                HStack {
                    MPEyebrow("Listing kit")
                    Spacer()
                    if drafting {
                        ProgressView().scaleEffect(0.7)
                    } else if OnDevice.readiness.canGenerate {
                        Button(draft == nil ? "Draft it" : "Again") {
                            makeDraft(for: it)
                        }
                        .font(.system(size: 13, weight: .semibold))
                    }
                }

                if let modelError { MPError(message: modelError) }

                if !OnDevice.readiness.canGenerate {
                    Text(OnDevice.readiness.sentence)
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.muted)
                } else if let draft {
                    Text("DRAFT - yours to edit before it goes anywhere")
                        .font(.system(size: 10))
                        .foregroundStyle(MP.Palette.subtle)
                    Text(draft)
                        .font(.system(size: 13.5))
                        .foregroundStyle(MP.Palette.fg)
                        .textSelection(.enabled)
                    Button("Copy text") { UIPasteboard.general.string = draft }
                        .font(.system(size: 13, weight: .semibold))
                } else {
                    Text("A description written on the phone from what is "
                         + "recorded here. Nothing is sent anywhere.")
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.muted)
                }
            }
        }
    }

    private func makeDraft(for it: InventoryItem) {
        drafting = true
        modelError = nil
        Task {
            do {
                draft = try await OnDevice.draftListing(
                    for: OnDevice.Item(
                        title: it.title ?? "",
                        era: it.era ?? "",
                        condition: it.condition ?? "",
                        asking: it.price.map { String(format: "%.2f", $0) }
                                ?? ""))
            } catch {
                modelError = error.localizedDescription
            }
            drafting = false
        }
    }

    private func binButton(name: String, detail: String, selected: Bool,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            MPCard {
                HStack(spacing: MP.S.x3) {
                    Image(systemName: selected ? "checkmark.circle.fill"
                                               : "circle")
                        .font(.system(size: 18))
                        .foregroundStyle(selected ? MP.Palette.accent
                                                  : MP.Palette.subtle)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(name)
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(MP.Palette.fg)
                        Text(detail)
                            .font(.system(size: 12))
                            .foregroundStyle(MP.Palette.muted)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
        .buttonStyle(.plain)
        .disabled(busy)
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
