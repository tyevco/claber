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
import UIKit

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
    /// Whether the thumbnail fetches have finished. Before they have, a
    /// missing picture is "not yet"; after, it is "not there".
    @State private var thumbsLoaded = false
    @State private var busy = false
    @State private var error: String?
    // The on-device model's two offerings. Both are held here rather
    // than written into the fields, which is the guard: a suggestion
    // becomes a value when she taps it and not before.
    @State private var suggested: OnDevice.Suggested?
    @State private var looking = false
    @State private var draft: String?
    @State private var drafting = false
    @State private var modelError: String?
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
            // Suggestions sit *above* the button because they fill the
            // form in - they are part of typing it. The listing kit sits
            // below, because it produces something to paste elsewhere
            // and is not part of getting the thing recorded.
            if !attached.isEmpty { suggestions }

            MPHoldButton(title: canSave ? "Hold to save" : "A title, at least",
                         enabled: canSave) {
                save()
            }
            .padding(.top, MP.S.x2)

            // Last, deliberately. Saving is the point of the screen and
            // the button for it should not be underneath two optional
            // panels she has to scroll past - which is exactly what
            // happened when this was added in the middle.
            listingKit
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
                    // Third time this app has had to say it: a row can
                    // outlive its file, and a blank tile is one she taps
                    // expecting a picture. Same treatment as triage.
                    MP.Palette.sunken.overlay {
                        Image(systemName: thumbsLoaded
                              ? "photo.badge.exclamationmark" : "photo")
                            .font(.system(size: 16))
                            .foregroundStyle(MP.Palette.subtle)
                    }
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

    // MARK: - what the model offers

    /// Chips, not a filled-in form. Tapping one writes that single
    /// field; nothing here writes anything on its own, and `paid` is not
    /// among them at all - a guessed cost would be indistinguishable
    /// from a real one in every margin thereafter.
    private var suggestions: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                HStack {
                    MPEyebrow("From the photograph")
                    Spacer()
                    if looking {
                        ProgressView().scaleEffect(0.7)
                    } else if OnDevice.readiness.canGenerate, canSeePictures {
                        Button(suggested == nil ? "Look" : "Look again") {
                            look()
                        }
                        .font(.system(size: 13, weight: .semibold))
                    }
                }

                if let modelError { MPError(message: modelError) }

                if !OnDevice.readiness.canGenerate {
                    Text(OnDevice.readiness.sentence)
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.muted)
                } else if !canSeePictures {
                    // The text model landed a version before it could be
                    // shown a picture, so this is a real distinction and
                    // not a permission problem.
                    Text("Reading a photograph needs iOS 27. The draft "
                         + "below works on this one.")
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.muted)
                } else if let suggested {
                    FlowChips(chips: chips(from: suggested))
                    Text("Tap to accept. Nothing here fills itself in, and "
                         + "it never guesses what you paid.")
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.subtle)
                } else {
                    Text("It can read the picture and offer a title, an "
                         + "era and what looks wrong with it. On this "
                         + "phone - nothing is sent anywhere.")
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.muted)
                }
            }
        }
    }

    private var listingKit: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                HStack {
                    MPEyebrow("Listing kit")
                    Spacer()
                    if drafting {
                        ProgressView().scaleEffect(0.7)
                    } else if OnDevice.readiness.canGenerate {
                        Button(draft == nil ? "Draft it" : "Again") {
                            makeDraft()
                        }
                        .font(.system(size: 13, weight: .semibold))
                        .disabled(title.trimmingCharacters(in: .whitespaces)
                                    .isEmpty)
                    }
                }

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
                    Button("Copy text") {
                        UIPasteboard.general.string = draft
                    }
                    .font(.system(size: 13, weight: .semibold))
                } else {
                    Text("A description built from what you have typed, "
                         + "written on the phone. It invents nothing you "
                         + "have not put in.")
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.muted)
                }
            }
        }
    }

    /// Whether this OS can hand the model a picture at all. The text
    /// half is iOS 26 and the image half is 27.
    private var canSeePictures: Bool {
        if #available(iOS 27.0, *) { return true }
        return false
    }

    private func chips(from s: OnDevice.Suggested) -> [(String, String, () -> Void)] {
        var out: [(String, String, () -> Void)] = []
        func add(_ label: String, _ value: String, _ apply: @escaping () -> Void) {
            let trimmed = value.trimmingCharacters(in: .whitespaces)
            // An empty field is the model declining, which the
            // instructions ask it to do rather than guess. Do not offer
            // an empty chip as though it were an answer.
            if !trimmed.isEmpty { out.append((label, trimmed, apply)) }
        }
        add("Title", s.title) { title = s.title }
        add("Era", s.era) { era = s.era }
        add("Condition", s.condition) { condition = s.condition }
        return out
    }

    private func look() {
        guard let id = attached.first, let data = thumbs[id],
              let cg = UIImage(data: data)?.cgImage else {
            modelError = "That photograph could not be read."
            return
        }
        looking = true
        modelError = nil
        Task {
            do {
                if #available(iOS 27.0, *) {
                    suggested = try await OnDevice.suggestions(
                        from: cg, typedTitle: title)
                }
            } catch {
                modelError = error.localizedDescription
            }
            looking = false
        }
    }

    private func makeDraft() {
        drafting = true
        modelError = nil
        Task {
            do {
                draft = try await OnDevice.draftListing(
                    for: OnDevice.Item(title: title, era: era,
                                       condition: condition, asking: asking))
            } catch {
                modelError = error.localizedDescription
            }
            drafting = false
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
            // arrive is marked, not a broken screen.
            thumbs[photo.id] = try? await APIClient.shared.photoData(photo.id)
        }
        thumbsLoaded = true
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
