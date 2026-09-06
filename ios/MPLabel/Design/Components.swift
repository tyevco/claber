//  Components.swift
//
//  The vocabulary the design repeats. Six pieces cover almost every
//  screen in `mplabel PWA.dc.html`, which is why they are worth having
//  as types rather than as a modifier copied around.
//
//  These are presentation only. Nothing here talks to APIClient, holds
//  state that outlives a screen, or knows what a parcel is.

import SwiftUI

// MARK: - eyebrow

/// The uppercase, letter-spaced label above every screen title, and
/// above most sections inside one. It is the single most repeated
/// element in the design and the cheapest way to make a screen read as
/// belonging to it.
struct MPEyebrow: View {
    let text: String

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 11, weight: .semibold))
            .tracking(1.5)
            .foregroundStyle(MP.Palette.muted)
            // The uppercasing is styling, and it must not leak into the
            // accessibility tree. Two reasons, and the second is the one
            // that bit: VoiceOver spells some all-caps strings out
            // letter by letter, and a UI test asking for the string the
            // caller passed - `staticTexts["Ships to"]` - silently finds
            // nothing, which reads as a screen that never appeared
            // rather than a caption that shouted.
            .accessibilityLabel(text)
    }
}

// MARK: - card

/// The raised row. Everything list-shaped in the design sits on one of
/// these rather than in a plain `List` row, which is most of why the
/// stock version looks like a different product.
struct MPCard<Content: View>: View {
    var padding: CGFloat = MP.S.x3
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(MP.Palette.raised,
                        in: RoundedRectangle(cornerRadius: MP.R.row))
            .overlay(
                RoundedRectangle(cornerRadius: MP.R.row)
                    .strokeBorder(MP.Palette.border, lineWidth: 1))
    }
}

// MARK: - chip

/// A stat block: a value with its label underneath. The design uses
/// these in a horizontal scroller for bins and counts, which is exactly
/// the shape the shelf needs.
struct MPChip: View {
    let value: String
    let label: String
    var tint: Color = MP.Palette.fg

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.system(size: 15, weight: .semibold).monospaced())
                .foregroundStyle(tint)
            Text(label)
                .font(.system(size: 10.5))
                .foregroundStyle(MP.Palette.muted)
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 9)
        .background(MP.Palette.raised,
                    in: RoundedRectangle(cornerRadius: MP.R.chip))
        .overlay(
            RoundedRectangle(cornerRadius: MP.R.chip)
                .strokeBorder(MP.Palette.border, lineWidth: 1))
    }
}

// MARK: - tag

/// An inline pill, for a bin name or a state, sitting in a line of text.
/// Distinct from `MPChip`: that is a block with its own label, this is a
/// word with a background.
struct MPTag: View {
    let text: String
    var style: Style = .normal

    enum Style {
        case normal
        /// "No bin", "not set" - a real answer rather than a missing
        /// value, so it is present but quiet.
        case absent
        case alert
    }

    var body: some View {
        Text(text)
            .font(.system(size: 10.5, weight: style == .absent ? .regular : .semibold))
            .foregroundStyle(foreground)
            .padding(.horizontal, 7)
            .padding(.vertical, 2.5)
            .background(background, in: RoundedRectangle(cornerRadius: MP.R.tag))
    }

    private var foreground: Color {
        switch style {
        case .normal: return MP.Palette.fg
        case .absent: return MP.Palette.muted
        case .alert:  return MP.Palette.alert
        }
    }

    private var background: Color {
        switch style {
        case .normal: return MP.Palette.raised
        case .absent: return .clear
        case .alert:  return MP.Palette.alertTint
        }
    }
}

// MARK: - hold to confirm

/// Hold to confirm, with the fill animation from the design.
///
/// This is not decoration and it does not get simplified into a plain
/// button. It guards ship and print - the two actions that cannot be
/// taken back. Printing spends physical stock onto media the printer
/// cannot tell you about, and marking shipped releases a parcel code for
/// reuse. The PWA has held this behaviour since it was written
/// (`holdStart` / `wireHolds` in app.js); losing it in the port would
/// quietly remove a guard nobody asked to remove.
struct MPHoldButton: View {
    let title: String
    var tint: Color = MP.Palette.accent
    var ink: Color = MP.Palette.accentInk
    var duration: TimeInterval = 0.8
    var enabled: Bool = true
    let action: () -> Void

    @State private var progress: CGFloat = 0
    @State private var holding = false

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: MP.R.card)
                    .fill(enabled ? tint.opacity(0.18) : MP.Palette.border)
                RoundedRectangle(cornerRadius: MP.R.card)
                    .fill(tint)
                    .frame(width: geo.size.width * progress)
                Text(title)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(progress > 0.5 ? ink : MP.Palette.fg)
                    .frame(maxWidth: .infinity, alignment: .center)
            }
            .clipShape(RoundedRectangle(cornerRadius: MP.R.card))
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in if enabled { start() } }
                    .onEnded { _ in cancel() })
        }
        .frame(height: 52)
        .opacity(enabled ? 1 : 0.5)
        // It is a GeometryReader with a drag gesture, not a Button, so
        // nothing tells the accessibility tree it is one. Without these
        // it is announced as plain content - VoiceOver does not call it
        // a button, and `app.buttons[...]` in a UI test does not find
        // it either. The trait is the fix for both, and the first is
        // the one that matters.
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(title)
        .accessibilityHint("Press and hold to confirm")
        // VoiceOver cannot hold a button down, so it gets a plain
        // activation instead of being locked out of shipping a parcel.
        .accessibilityAction { if enabled { action() } }
    }

    private func start() {
        guard !holding else { return }
        holding = true
        withAnimation(.linear(duration: duration)) { progress = 1 }
        // The haptic fires with the action, not with the touch: it is
        // confirmation that something happened, and firing it on contact
        // would say "done" while the fill is still moving.
        DispatchQueue.main.asyncAfter(deadline: .now() + duration) {
            guard holding else { return }
            holding = false
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            action()
            progress = 0
        }
    }

    private func cancel() {
        holding = false
        withAnimation(.easeOut(duration: 0.18)) { progress = 0 }
    }
}

// MARK: - screen skeleton

/// The head-and-scroll shape every screen in the design shares: an
/// eyebrow, a title, optional trailing controls, then content on the
/// canvas colour.
struct MPScreen<Trailing: View, Content: View>: View {
    let eyebrow: String
    let title: String
    @ViewBuilder var trailing: Trailing
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    MPEyebrow(eyebrow)
                    Text(title)
                        .font(.system(size: 26, weight: .semibold))
                        .foregroundStyle(MP.Palette.fg)
                }
                Spacer(minLength: MP.S.x3)
                trailing
            }
            .padding(.horizontal, MP.S.x5)
            .padding(.top, MP.S.x3)
            .padding(.bottom, MP.S.x4)

            ScrollView {
                LazyVStack(spacing: MP.S.x2) { content }
                    .padding(.horizontal, MP.S.x5)
                    .padding(.bottom, MP.S.x8)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(MP.Palette.bg)
    }
}

extension MPScreen where Trailing == EmptyView {
    init(eyebrow: String, title: String, @ViewBuilder content: () -> Content) {
        self.init(eyebrow: eyebrow, title: title,
                  trailing: { EmptyView() }, content: content)
    }
}

// MARK: - small pieces

/// A label/value row, the design's equivalent of `LabeledContent`.
struct MPRow: View {
    let label: String
    let value: String
    var mono = false

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(.system(size: 13))
                .foregroundStyle(MP.Palette.muted)
            Spacer(minLength: MP.S.x3)
            Text(value)
                .font(mono ? .system(size: 14).monospaced() : .system(size: 14))
                .foregroundStyle(MP.Palette.fg)
                .multilineTextAlignment(.trailing)
        }
        .padding(.vertical, 5)
    }
}

/// An error, in the design's alert tint rather than a red banner.
struct MPError: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.system(size: 13))
            .foregroundStyle(MP.Palette.alert)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(MP.S.x3)
            .background(MP.Palette.alertTint,
                        in: RoundedRectangle(cornerRadius: MP.R.chip))
            .overlay(
                RoundedRectangle(cornerRadius: MP.R.chip)
                    .strokeBorder(MP.Palette.alertEdge, lineWidth: 1))
    }
}

/// A note in the accent tint - the design's way of confirming something
/// happened without a modal.
struct MPNote: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.system(size: 13))
            .foregroundStyle(MP.Palette.accent)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(MP.S.x3)
            .background(MP.Palette.accentTint,
                        in: RoundedRectangle(cornerRadius: MP.R.chip))
    }
}

/// Chips that wrap onto as many lines as they need.
///
/// A horizontal `ScrollView` was the obvious thing and is wrong for
/// suggestions: a chip that has scrolled off the right edge is one she
/// will not know was offered, and the whole point of the row is that she
/// sees every option and picks. Titles here run long, so they wrap.
struct FlowChips: View {
    let chips: [(String, String, () -> Void)]

    var body: some View {
        VStack(alignment: .leading, spacing: MP.S.x2) {
            ForEach(Array(chips.enumerated()), id: \.offset) { _, chip in
                Button(action: chip.2) {
                    HStack(alignment: .firstTextBaseline, spacing: MP.S.x2) {
                        Text(chip.0.uppercased())
                            .font(.system(size: 9.5, weight: .semibold))
                            .tracking(1)
                            .foregroundStyle(MP.Palette.muted)
                            .accessibilityLabel(chip.0)
                        Text(chip.1)
                            .font(.system(size: 13.5))
                            .foregroundStyle(MP.Palette.fg)
                            .multilineTextAlignment(.leading)
                        Spacer(minLength: MP.S.x2)
                        Image(systemName: "plus.circle")
                            .font(.system(size: 14))
                            .foregroundStyle(MP.Palette.accent)
                    }
                    .padding(.horizontal, MP.S.x3)
                    .padding(.vertical, MP.S.x2)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(MP.Palette.sunken,
                                in: RoundedRectangle(cornerRadius: MP.R.chip))
                }
                .buttonStyle(.plain)
            }
        }
    }
}
