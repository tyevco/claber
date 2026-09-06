//  TriageView.swift
//
//  The calm half of sourcing: at the kitchen table, turning a
//  photographed receipt into what things cost.
//
//  There is no OCR and there is not going to be. A thrift receipt
//  itemises by department - "HOUSEWARES $4.99" - so a machine can read
//  every character on it and still not know which $4.99 was the
//  stoneware vase. The attribution is the person's, and this screen is
//  built to make it quick rather than to pretend it can be skipped: the
//  photograph on one side, the things that came home on the other, one
//  field each.
//
//  What "done" means here is deliberately narrow. Filing the receipt
//  against a trip takes it out of the pile; it does not claim the money
//  is all attributed. That second question has its own number and it is
//  `unassigned` on the trip, which stays visible precisely because a $4
//  lot of four things is one line on the paper and four rows in the
//  database.

import SwiftUI

struct TriageView: View {
    @State private var pile: [Photo] = []
    @State private var index = 0
    @State private var image: Data?
    /// Why there is no picture, when there is no picture. Nil while it is
    /// still coming.
    @State private var imageFailure: String?
    @State private var trips: [Trip] = []
    @State private var detail: TripDetail?
    @State private var costs: [Int: String] = [:]
    @State private var newTitle = ""
    @State private var loading = true
    @State private var busy = false
    @State private var error: String?
    @State private var note: String?
    /// Which cost field has the keyboard. A decimal pad has no return
    /// key, so `onSubmit` never fires on a phone - losing focus is the
    /// only "done" signal this field gets, and without it every figure
    /// she typed would be discarded on the way to the next one.
    @FocusState private var focused: Int?

    private var current: Photo? {
        pile.indices.contains(index) ? pile[index] : nil
    }

    var body: some View {
        MPScreen(eyebrow: pileLabel, title: "What did it cost?") {
            if let error { MPError(message: error) }
            if let note { MPNote(message: note) }

            if loading {
                ProgressView().padding(.top, MP.S.x7)
            } else if current == nil {
                MPEmpty(title: "Nothing to triage",
                        detail: "Every capture is filed. What is left is "
                              + "money without a home, and that shows on "
                              + "the trip itself.")
            } else {
                receipt
                whichTrip
                if detail != nil { candidates }
                done
            }
        }
        .task { await load() }
        .onChange(of: focused) { previous, _ in
            // Save the field being left, not the one being entered.
            if let previous, let item = detail?.items.first(
                where: { $0.id == previous }) {
                save(item)
            }
        }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { focused = nil }
            }
        }
    }

    private var pileLabel: String {
        pile.isEmpty ? "Triage" : "Receipt \(index + 1) of \(pile.count)"
    }

    // MARK: - the photograph

    private var receipt: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("The capture")
                if let image, let ui = UIImage(data: image) {
                    Image(uiImage: ui)
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: .infinity)
                        .frame(maxHeight: 260)
                        .clipShape(RoundedRectangle(cornerRadius: MP.R.sm))
                } else {
                    // Three states, not two. A spinner that never stops is
                    // how a row pointing at a file that is gone presents -
                    // and `photos.path` can absolutely point at nothing,
                    // which is the same failure `mplabel verify` exists to
                    // catch for labels. Saying so is what lets her file the
                    // receipt anyway rather than waiting on a picture that
                    // is never going to arrive.
                    RoundedRectangle(cornerRadius: MP.R.sm)
                        .fill(MP.Palette.sunken)
                        .frame(height: 140)
                        .overlay {
                            if let imageFailure {
                                VStack(spacing: MP.S.x1) {
                                    Image(systemName: "photo.badge.exclamationmark")
                                        .font(.system(size: 20))
                                    Text(imageFailure)
                                        .font(.system(size: 12))
                                        .multilineTextAlignment(.center)
                                }
                                .foregroundStyle(MP.Palette.muted)
                                .padding(MP.S.x3)
                            } else {
                                ProgressView()
                            }
                        }
                }
                if let taken = current?.takenAt {
                    Text(taken)
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.subtle)
                }
            }
        }
    }

    // MARK: - which trip it belongs to

    private var whichTrip: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("Which run")
                if trips.isEmpty {
                    Text("No trips yet. One is a shop and a day - make it "
                         + "from the receipt in front of you.")
                        .font(.system(size: 12.5))
                        .foregroundStyle(MP.Palette.muted)
                }
                ForEach(trips) { trip in
                    Button { pick(trip) } label: {
                        HStack(spacing: MP.S.x2) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(trip.store)
                                    .font(.system(size: 14, weight: .semibold))
                                    .foregroundStyle(MP.Palette.fg)
                                Text(trip.occurredAt ?? "—")
                                    .font(.system(size: 11.5))
                                    .foregroundStyle(MP.Palette.muted)
                            }
                            Spacer(minLength: MP.S.x2)
                            Text(money(trip.receiptTotal))
                                .font(.system(size: 13).monospaced())
                                .foregroundStyle(MP.Palette.muted)
                            Image(systemName: detail?.trip.id == trip.id
                                  ? "largecircle.fill.circle" : "circle")
                                .foregroundStyle(detail?.trip.id == trip.id
                                                 ? MP.Palette.accent
                                                 : MP.Palette.borderStrong)
                        }
                        .padding(.vertical, MP.S.x1)
                    }
                    .disabled(busy)
                }
            }
        }
    }

    // MARK: - what the money bought

    private var candidates: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("What came home")
                Text("Which item did this money buy?")
                    .font(.system(size: 12.5))
                    .foregroundStyle(MP.Palette.muted)

                ForEach(detail?.items ?? []) { item in
                    HStack(spacing: MP.S.x2) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.title ?? "(no title)")
                                .font(.system(size: 13.5))
                                .foregroundStyle(MP.Palette.fg)
                                .lineLimit(2)
                            if let code = item.inventoryCode {
                                Text(code)
                                    .font(.system(size: 10.5).monospaced())
                                    .foregroundStyle(MP.Palette.subtle)
                            }
                        }
                        Spacer(minLength: MP.S.x2)
                        HStack(spacing: 2) {
                            Text("$").foregroundStyle(MP.Palette.subtle)
                            TextField("0.00", text: binding(for: item))
                                .keyboardType(.decimalPad)
                                .multilineTextAlignment(.trailing)
                                .frame(width: 68)
                                .focused($focused, equals: item.id)
                                .onSubmit { save(item) }
                        }
                        .font(.system(size: 14).monospaced())
                    }
                    .padding(.vertical, MP.S.x1)
                }

                HStack(spacing: MP.S.x2) {
                    TextField("It's not listed yet - add the item",
                              text: $newTitle)
                        .font(.system(size: 13))
                        .textInputAutocapitalization(.sentences)
                    Button("Add") { add() }
                        .font(.system(size: 13, weight: .semibold))
                        .disabled(busy || newTitle.trimmingCharacters(
                            in: .whitespaces).isEmpty)
                }
                .padding(.top, MP.S.x1)

                if let trip = detail?.trip {
                    // The number the whole screen is about, and the
                    // reason `unassigned` is null rather than zero when
                    // nobody wrote the till total down.
                    Text(unassignedLine(trip))
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.subtle)
                        .padding(.top, MP.S.x1)
                }
            }
        }
    }

    private func unassignedLine(_ trip: Trip) -> String {
        guard let left = trip.unassigned else {
            return "No receipt total on this trip, so there is nothing to "
                 + "reconcile against. Add what the till said to see what "
                 + "is still unattributed."
        }
        if left <= 0 {
            return "Every penny of this trip is attributed."
        }
        return "\(money(left)) of this trip has no home yet. Leftover cost "
             + "stays on the trip, so a $4 lot of four things still shows "
             + "up in profit."
    }

    private var done: some View {
        MPHoldButton(title: detail == nil
                     ? "Pick a run first"
                     : "Hold to file and go to the next",
                     enabled: !busy && detail != nil) {
            file()
        }
        .padding(.top, MP.S.x2)
    }

    // MARK: - doing the work

    private func binding(for item: InventoryItem) -> Binding<String> {
        Binding(
            get: {
                if let typed = costs[item.id] { return typed }
                guard let paid = item.paid else { return "" }
                return String(format: "%.2f", paid)
            },
            set: { costs[item.id] = $0 })
    }

    private func load() async {
        loading = true
        error = nil
        do {
            pile = try await APIClient.shared.untriaged()
            trips = try await APIClient.shared.trips()
            index = min(index, max(0, pile.count - 1))
            await loadImage()
            // A capture already filed against a trip is not in the pile,
            // so anything here starts with no run chosen.
            detail = nil
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }

    private func loadImage() async {
        image = nil
        imageFailure = nil
        guard let photo = current else { return }
        do {
            image = try await APIClient.shared.photoData(photo.id)
        } catch {
            imageFailure = "The photograph did not load. The row is here; "
                         + "the file may not be."
        }
    }

    private func pick(_ trip: Trip) {
        busy = true
        error = nil
        Task {
            do {
                detail = try await APIClient.shared.trip(trip.id)
                costs = [:]
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }

    private func save(_ item: InventoryItem) {
        guard let typed = costs[item.id] else { return }
        let trimmed = typed.trimmingCharacters(in: .whitespaces)
        busy = true
        error = nil
        Task {
            do {
                // Empty clears it back to unknown, which is a real answer
                // and not the same as free.
                _ = try await APIClient.shared.setCost(
                    item: item.id,
                    paid: trimmed.isEmpty ? nil : Double(trimmed))
                if let trip = detail?.trip {
                    detail = try await APIClient.shared.trip(trip.id)
                }
                costs[item.id] = nil
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }

    private func add() {
        guard let trip = detail?.trip else { return }
        let title = newTitle.trimmingCharacters(in: .whitespaces)
        busy = true
        error = nil
        Task {
            do {
                _ = try await APIClient.shared.makeItem(title: title,
                                                        trip: trip.id)
                detail = try await APIClient.shared.trip(trip.id)
                newTitle = ""
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }

    /// File the capture against the run it records. That is what takes it
    /// out of the pile - not whether the money is all attributed, which
    /// is a different question with its own number.
    private func file() {
        guard let photo = current, let trip = detail?.trip else { return }
        busy = true
        error = nil
        Task {
            do {
                try await APIClient.shared.attach(photo: photo.id,
                                                  toTrip: trip.id)
                note = "Filed against \(trip.store)."
                detail = nil
                await load()
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}
