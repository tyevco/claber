//  ReconcileView.swift
//
//  The cart beside the receipt, at the kitchen table.
//
//  Replaces the old Triage screen and does its job: turning a receipt
//  into what things cost. The difference is where the objects come from
//  - Triage asked her to attribute money to listings that already
//  existed, and this reconciles against what she decided to buy while
//  she was standing in front of it.
//
//  **Nothing here is written until she confirms it.** The server
//  proposes and says how sure it is; every row arrives with an amount
//  filled in and a sentence saying why, and the amount is hers to
//  change. A receipt itemises by department, so where two things came
//  out of HOUSEWARES the pick is a coin toss - and a coin toss written
//  into `paid` is indistinguishable from a figure she checked, for ever
//  afterwards, in every margin.
//
//  The screen still works with no candidates behind it. That is what a
//  receipt from a trip where she photographed nothing looks like, and
//  the old Triage screen was the only thing that handled it - so it says
//  what to do rather than showing an empty list.

import SwiftUI

struct ReconcileView: View {
    let trip: Trip

    @State private var proposal: ProposalResponse?
    @State private var lines: [ReceiptLine] = []
    @State private var amounts: [Int: String] = [:]
    @State private var chosen: Set<Int> = []
    @State private var loading = true
    @State private var busy = false
    @State private var error: String?
    @State private var note: String?
    @State private var scanning = false
    @FocusState private var focused: Int?

    var body: some View {
        MPScreen(eyebrow: trip.store, title: "What did it cost?") {
            if let error { MPError(message: error) }
            if let note { MPNote(message: note) }

            if loading {
                ProgressView().padding(.top, MP.S.x7)
            } else {
                summary
                if lines.isEmpty {
                    noReceipt
                } else if (proposal?.proposals.isEmpty ?? true) {
                    noCandidates
                } else {
                    rows
                    confirm
                }
            }
        }
        .task { await load() }
        .sheet(isPresented: $scanning) {
            NavigationStack {
                ReceiptScanView(trip: trip, onRead: { _ in
                    Task { await load() }
                })
            }
        }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { focused = nil }
            }
        }
    }

    // MARK: - the shape of the problem

    private var summary: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("This run")
                HStack(alignment: .top, spacing: MP.S.x2) {
                    figure(money(trip.receiptTotal), "on the receipt")
                    figure("\(proposal?.carted ?? 0)", "in the cart")
                    figure("\(proposal?.itemLines ?? 0)", "goods lines")
                }
                if let p = proposal, p.carted != p.itemLines,
                   !lines.isEmpty {
                    // Said plainly rather than hidden in the rows: the
                    // counts not matching is the single best signal that
                    // the reading or the cart is incomplete.
                    Text("Those two numbers disagree, so something is "
                         + "missing on one side - a line the scan dropped, "
                         + "or something carted that never got "
                         + "photographed.")
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.alert)
                }
            }
        }
    }

    private func figure(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.system(size: 17, weight: .semibold).monospaced())
                .foregroundStyle(MP.Palette.fg)
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(MP.Palette.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var noReceipt: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("No receipt yet")
                Text("Nothing has been read off this run's receipt. Scan "
                     + "it and the cart gets laid beside it.")
                    .font(.system(size: 12.5))
                    .foregroundStyle(MP.Palette.muted)
                Button("Scan the receipt") { scanning = true }
                    .font(.system(size: 14, weight: .semibold))
            }
        }
    }

    /// A receipt with nothing carted behind it. The old Triage screen was
    /// the only thing that handled this, so it has to say what to do
    /// rather than showing an empty list and looking broken.
    private var noCandidates: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("Nothing in the cart")
                Text("This run has a receipt but nothing was photographed "
                     + "in the shop, so there is nothing to attribute the "
                     + "amounts to. Add the items by hand from the Shelf "
                     + "tab and they can be costed there.")
                    .font(.system(size: 12.5))
                    .foregroundStyle(MP.Palette.muted)
            }
        }
    }

    private var rows: some View {
        MPCard {
            VStack(alignment: .leading, spacing: MP.S.x2) {
                MPEyebrow("What came home")
                Text("Each amount is a proposal. Change any of them.")
                    .font(.system(size: 12.5))
                    .foregroundStyle(MP.Palette.muted)

                ForEach(proposal?.proposals ?? []) { row in
                    VStack(alignment: .leading, spacing: MP.S.x1) {
                        HStack(spacing: MP.S.x2) {
                            Button { toggle(row) } label: {
                                Image(systemName: chosen.contains(row.candidate)
                                      ? "checkmark.square.fill" : "square")
                                    .foregroundStyle(
                                        chosen.contains(row.candidate)
                                        ? MP.Palette.accent
                                        : MP.Palette.borderStrong)
                            }
                            .buttonStyle(.plain)

                            VStack(alignment: .leading, spacing: 1) {
                                Text(row.title ?? "(untitled)")
                                    .font(.system(size: 13.5))
                                    .foregroundStyle(MP.Palette.fg)
                                    .lineLimit(2)
                                Text((row.label ?? "no line") + " · "
                                     + row.sentence)
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(row.isMatched
                                                     ? MP.Palette.subtle
                                                     : MP.Palette.warn)
                            }
                            Spacer(minLength: MP.S.x2)
                            HStack(spacing: 2) {
                                Text("$").foregroundStyle(MP.Palette.subtle)
                                TextField("0.00", text: binding(for: row))
                                    .keyboardType(.decimalPad)
                                    .multilineTextAlignment(.trailing)
                                    .frame(width: 68)
                                    .focused($focused, equals: row.candidate)
                                    .accessibilityIdentifier(
                                        "amount-\(row.candidate)")
                            }
                            .font(.system(size: 14).monospaced())
                        }
                    }
                    .padding(.vertical, MP.S.x1)
                }

                if let left = proposal?.unclaimedLines, !left.isEmpty {
                    Text("Not attributed to anything: "
                         + left.map { ($0.label ?? "?") + " "
                                    + money($0.amount) }
                               .joined(separator: ", ")
                         + ". That stays on the run.")
                        .font(.system(size: 11.5))
                        .foregroundStyle(MP.Palette.subtle)
                        .padding(.top, MP.S.x1)
                }
            }
        }
    }

    private var confirm: some View {
        MPHoldButton(title: chosen.isEmpty
                     ? "Tick what you bought"
                     : "Hold to add \(chosen.count) to inventory",
                     enabled: !busy && !chosen.isEmpty) {
            apply()
        }
        .padding(.top, MP.S.x2)
    }

    // MARK: - doing the work

    private func binding(for row: Proposal) -> Binding<String> {
        Binding(
            get: {
                if let typed = amounts[row.candidate] { return typed }
                guard let amount = row.amount else { return "" }
                return String(format: "%.2f", amount)
            },
            set: { amounts[row.candidate] = $0 })
    }

    private func toggle(_ row: Proposal) {
        if chosen.contains(row.candidate) {
            chosen.remove(row.candidate)
        } else {
            chosen.insert(row.candidate)
        }
    }

    private func load() async {
        loading = true
        error = nil
        do {
            lines = try await APIClient.shared.receiptLines(trip: trip.id)
            let out = try await APIClient.shared.proposal(trip: trip.id)
            proposal = out
            // Pre-ticked only where the department actually fits. A
            // guess arrives unticked on purpose: the tick is the moment
            // she takes responsibility for the number, and pre-ticking a
            // coin toss would collect that consent without asking.
            chosen = Set(out.proposals.filter(\.isMatched).map(\.candidate))
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }

    private func apply() {
        busy = true
        error = nil
        Task {
            do {
                let assignments = (proposal?.proposals ?? [])
                    .filter { chosen.contains($0.candidate) }
                    .map { row -> [String: Double] in
                        let typed = amounts[row.candidate]
                            .flatMap { Double($0) }
                        return ["candidate": Double(row.candidate),
                                "amount": typed ?? row.amount ?? 0]
                    }
                let made = try await APIClient.shared.reconcile(
                    trip: trip.id, assignments: assignments)
                note = "\(made) added to inventory."
                chosen = []
                amounts = [:]
                await load()
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}
