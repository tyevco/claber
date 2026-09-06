//  AddItemView.swift
//
//  Something enters the system without an email. Two things arrive this
//  way and neither has Facebook behind it: a **local pickup sale**,
//  which produces no label email at all and is otherwise invisible, and
//  a thing off a shelf being listed for the first time.
//
//  The server keys it with `title_key`, the same derivation the
//  saved-page import and `link_sales` use, so when the sale does turn up
//  later knowing only the item's name it lands on *this* row rather than
//  making a second one beside it. That is why the title is the one
//  required field and why it is worth typing carefully.
//
//  `paid` is the field this screen exists for. Every margin in the
//  analytics is null without it, and Profit can only say "gross" - so it
//  sits next to the asking price rather than being buried under an
//  "advanced" disclosure.

import SwiftUI

struct AddItemView: View {
    /// Prefilled when the screen is opened from a trip, so the thing is
    /// attributed to the run it came home from without being asked.
    var trip: Trip?
    var onSaved: ((InventoryItem) -> Void)?

    @Environment(\.dismiss) private var dismiss

    @State private var title = ""
    @State private var paid = ""
    @State private var asking = ""
    @State private var era = ""
    @State private var condition = ""
    @State private var bins: [Bin] = []
    @State private var chosenBin: String?
    @State private var pile: [Photo] = []
    @State private var thumbs: [Int: Data] = [:]
    @State private var attached: Set<Int> = []
    @State private var busy = false
    @State private var error: String?
    @FocusState private var focused: Field?

    private enum Field { case title, paid, asking, era, condition }

    private var canSave: Bool {
        !title.trimmingCharacters(in: .whitespaces).isEmpty && !busy
    }

    var body: some View {
        MPScreen(eyebrow: trip.map { "From \($0.store)" } ?? "By hand",
                 title: "New item") {
            if let error { MPError(message: error) }

            what
            money
            describing
            whereItGoes
            if !pile.isEmpty { photographs }

            MPHoldButton(title: canSave ? "Hold to save" : "A title, at least",
                         enabled: canSave) {
                save()
            }
            .padding(.top, MP.S.x2)
        }
        .task { await load() }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { focused = nil }
            }
        }
    }

    // MARK: - the form

    private var what: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                HStack {
                    MPEyebrow("What it is")
                    Spacer()
                    Text("required")
                        .font(.system(size: 10.5))
                        .foregroundStyle(MP.Palette.subtle)
                }
                TextField("Oil portrait of a woman in blue", text: $title,
                          axis: .vertical)
                    .font(.system(size: 15))
                    .lineLimit(1...3)
                    .focused($focused, equals: .title)
                    // Identifiers rather than placeholders for the tests
                    // to hold on to. A placeholder is copy, and copy is
                    // the thing most likely to be reworded by someone
                    // who has no idea a test is reading it. Note a
                    // `TextField(axis:)` is a *textView* in the
                    // accessibility tree, not a textField, which is its
                    // own reason not to query by shape.
                    .accessibilityIdentifier("item-title")
                Text("The sale finds this row by its title, so write it "
                     + "the way the listing will say it.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(MP.Palette.subtle)
            }
        }
    }

    private var money: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("Money")
                HStack(spacing: MP.S.x4) {
                    field("Paid", text: $paid, focus: .paid)
                    field("Asking", text: $asking, focus: .asking)
                }
                // Not a nag: without a cost there is no margin, and the
                // profit screen has to keep saying gross.
                Text(paid.isEmpty
                     ? "Without what it cost, profit stays gross."
                     : "Both in dollars, not cents.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(MP.Palette.subtle)
            }
        }
    }

    private func field(_ label: String, text: Binding<String>,
                       focus: Field) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(.system(size: 11.5))
                .foregroundStyle(MP.Palette.muted)
            HStack(spacing: 2) {
                Text("$").foregroundStyle(MP.Palette.subtle)
                TextField("0.00", text: text)
                    .keyboardType(.decimalPad)
                    .focused($focused, equals: focus)
                    .accessibilityIdentifier("item-" + label.lowercased())
            }
            .font(.system(size: 16).monospaced())
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var describing: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("What a buyer asks")
                VStack(alignment: .leading, spacing: 3) {
                    Text("Era").font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.muted)
                    TextField("c. 1910", text: $era)
                        .font(.system(size: 15))
                        .focused($focused, equals: .era)
                        .accessibilityIdentifier("item-era")
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("Condition").font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.muted)
                    TextField("Craquelure, small chip", text: $condition,
                              axis: .vertical)
                        .font(.system(size: 15))
                        .lineLimit(1...3)
                        .focused($focused, equals: .condition)
                        .accessibilityIdentifier("item-condition")
                }
            }
        }
    }

    private var whereItGoes: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("Where it goes")
                if bins.isEmpty {
                    Text("No bins yet. A bin is a shelf with a printed tag "
                         + "on it - make one from the Shelf tab.")
                        .font(.system(size: 12.5))
                        .foregroundStyle(MP.Palette.muted)
                } else {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: MP.S.x2) {
                            // Off the shelf is a real answer, not the
                            // absence of one - it is what a thing in her
                            // hand is on the way somewhere.
                            binChip(name: "No bin", code: nil)
                            ForEach(bins) { bin in
                                binChip(name: bin.name, code: bin.code)
                            }
                        }
                    }
                }
            }
        }
    }

    private func binChip(name: String, code: String?) -> some View {
        let picked = chosenBin == code
        return Button { chosenBin = code } label: {
            Text(name)
                .font(.system(size: 13, weight: picked ? .semibold : .regular))
                .foregroundStyle(picked ? MP.Palette.accentInk : MP.Palette.fg)
                .padding(.horizontal, MP.S.x3)
                .padding(.vertical, MP.S.x2)
                .background(picked ? MP.Palette.accent : MP.Palette.raised,
                            in: RoundedRectangle(cornerRadius: MP.R.chip))
                .overlay(RoundedRectangle(cornerRadius: MP.R.chip)
                    .strokeBorder(MP.Palette.border, lineWidth: picked ? 0 : 1))
        }
        .buttonStyle(.plain)
    }

    /// Captures that are not about anything yet. This is the only way a
    /// photograph gets attached to a thing, because the shutter screen
    /// deliberately asks nothing at the time - the shop is where the
    /// picture has to be taken and the kitchen table is where it can be
    /// said what it was of.
    private var photographs: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("Photographs waiting")
                Text("Tap the ones this is a picture of.")
                    .font(.system(size: 12.5))
                    .foregroundStyle(MP.Palette.muted)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: MP.S.x2) {
                        ForEach(pile) { photo in
                            Button { toggle(photo) } label: {
                                thumbnail(photo)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
    }

    private func thumbnail(_ photo: Photo) -> some View {
        let on = attached.contains(photo.id)
        return ZStack(alignment: .topTrailing) {
            Group {
                if let data = thumbs[photo.id], let ui = UIImage(data: data) {
                    Image(uiImage: ui).resizable().scaledToFill()
                } else {
                    MP.Palette.sunken
                }
            }
            .frame(width: 64, height: 72)
            .clipShape(RoundedRectangle(cornerRadius: MP.R.sm))
            .overlay(RoundedRectangle(cornerRadius: MP.R.sm)
                .strokeBorder(on ? MP.Palette.accent : MP.Palette.border,
                              lineWidth: on ? 2 : 1))
            if on {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(MP.Palette.accent)
                    .padding(3)
            }
        }
    }

    // MARK: - doing the work

    private func load() async {
        do {
            bins = try await APIClient.shared.bins()
            pile = try await APIClient.shared.untriaged()
        } catch {
            self.error = error.localizedDescription
        }
        for photo in pile {
            // One at a time and best effort: a thumbnail that does not
            // arrive is a grey rectangle, not a broken screen.
            thumbs[photo.id] = try? await APIClient.shared.photoData(photo.id)
        }
    }

    private func toggle(_ photo: Photo) {
        if attached.contains(photo.id) {
            attached.remove(photo.id)
        } else {
            attached.insert(photo.id)
        }
    }

    private func save() {
        busy = true
        error = nil
        Task {
            do {
                let item = try await APIClient.shared.makeItem(
                    title: title.trimmingCharacters(in: .whitespaces),
                    paid: Double(paid.trimmingCharacters(in: .whitespaces)),
                    price: Double(asking.trimmingCharacters(in: .whitespaces)),
                    era: blank(era), condition: blank(condition),
                    bin: chosenBin, trip: trip?.id,
                    photos: Array(attached))
                onSaved?(item)
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }

    private func blank(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? nil : trimmed
    }
}
