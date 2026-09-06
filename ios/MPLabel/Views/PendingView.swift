//  PendingView.swift
//
//  Labels that were recorded but never came out.
//
//  This is the recovery path, and it exists because a successful write
//  is not proof a label printed: the printer is write-only, so out of
//  paper, head open and a jam all look like a clean print. `run` cannot
//  fix it either - once a message is in `sales` the poller skips it on
//  sight, which is what stops a re-poll reprinting everything.
//
//  The batch is idempotent server-side unless forced: a row that already
//  has `printed_at` is dropped from the list before printing. That is
//  not politeness, it is what makes a second tap safe after the first
//  one timed out - and a batch of nine can outlive Cloudflare's 100
//  second edge timeout, so a second tap is likely rather than unusual.

import SwiftUI

struct PendingView: View {
    @State private var rows: [Order] = []
    @State private var selected: Set<Int> = []
    @State private var dryRun = false
    @State private var error: String?
    @State private var note: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            MPScreen(eyebrow: "Recovery", title: "Pending labels") {
                if let error { MPError(message: error) }
                if let note { MPNote(message: note) }

                if rows.isEmpty {
                    MPEmpty(title: "Nothing pending",
                            detail: "Every recorded label has been printed.",
                            symbol: "checkmark.circle")
                } else {
                    Text("These were recorded but never came out. Usually "
                         + "the printer was off. Today only - older ones "
                         + "may already have been posted by hand.")
                        .font(.system(size: 12))
                        .foregroundStyle(MP.Palette.muted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.bottom, MP.S.x1)

                    ForEach(rows) { order in
                        Button { toggle(order.id) } label: {
                            MPCard {
                                HStack(alignment: .top, spacing: MP.S.x3) {
                                    Image(systemName: selected.contains(order.id)
                                          ? "checkmark.circle.fill" : "circle")
                                        .font(.system(size: 18))
                                        .foregroundStyle(selected.contains(order.id)
                                                         ? MP.Palette.accent
                                                         : MP.Palette.subtle)
                                    OrderRow(order: order)
                                }
                            }
                        }
                        .buttonStyle(.plain)
                    }

                    Button { dryRun.toggle() } label: {
                        MPCard {
                            HStack(spacing: MP.S.x3) {
                                Image(systemName: dryRun
                                      ? "checkmark.square.fill" : "square")
                                    .font(.system(size: 16))
                                    .foregroundStyle(dryRun ? MP.Palette.accent
                                                            : MP.Palette.subtle)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("Dry run")
                                        .font(.system(size: 14, weight: .semibold))
                                        .foregroundStyle(MP.Palette.fg)
                                    Text("Show what would print. No labels used.")
                                        .font(.system(size: 12))
                                        .foregroundStyle(MP.Palette.muted)
                                }
                            }
                        }
                    }
                    .buttonStyle(.plain)
                    .padding(.top, MP.S.x1)

                    MPHoldButton(
                        title: selected.isEmpty
                            ? "Choose the ones to run again"
                            : (dryRun ? "Hold to preview \(selected.count)"
                                      : "Hold to print \(selected.count)"),
                        enabled: !busy && !selected.isEmpty
                    ) { run() }
                    .padding(.top, MP.S.x2)
                }
            }
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private func toggle(_ id: Int) {
        if selected.contains(id) { selected.remove(id) } else { selected.insert(id) }
    }

    private func load() async {
        do {
            rows = try await APIClient.shared.pending()
            // Drop anything that has since printed rather than leaving it
            // ticked - a stale selection is how a second tap becomes a
            // second label.
            selected = selected.intersection(Set(rows.map(\.id)))
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func run() {
        guard !busy, !selected.isEmpty else { return }
        busy = true
        error = nil
        note = nil
        let ids = Array(selected)
        let wasDry = dryRun
        Task {
            do {
                let out = try await APIClient.shared.printPending(ids: ids,
                                                                  dryRun: wasDry)
                if wasDry {
                    note = "\(out.wouldPrint?.count ?? 0) would print. "
                         + "Nothing used."
                } else {
                    let n = out.printed?.count ?? 0
                    note = "\(n) label\(n == 1 ? "" : "s") sent to the printer."
                    if let failed = out.failed, !failed.isEmpty {
                        // Per-row failures: one bad label must not
                        // abandon the rest of the batch, so both halves
                        // are reported.
                        error = failed
                            .map { "\($0.id): \($0.error)" }
                            .joined(separator: "\n")
                    }
                    selected = []
                }
                await load()
            } catch {
                // The batch may have partly succeeded before the edge
                // timed out, so reload rather than trusting what is on
                // screen. The server skips anything already printed,
                // which is what makes a re-fire safe.
                self.error = error.localizedDescription
                selected = []
                await load()
            }
            busy = false
        }
    }
}
