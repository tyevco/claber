//  Tokens.swift
//
//  The March Vector palette, transcribed from the design rather than
//  approximated. Every value here is a literal from the `[data-mp]`
//  block in `mplabel PWA.dc.html`, which is itself built on
//  `colors_and_type.css` - a warm, deliberately yellow-biased neutral
//  ramp with a green that is never neon.
//
//  Both themes are here because the design ships both, dark as the
//  default. Picking colours "close enough" by eye is what turns a
//  designed ramp into a set of unrelated greys, so the CSS names are
//  kept in the comments to make drift visible in review.
//
//  Typography is deliberately *not* here. The design uses Fraunces,
//  Inter and JetBrains Mono; this app uses the system faces, so the
//  character has to come from the palette, the spacing and the shapes.
//  `.monospaced` still gets used for codes, because a parcel or
//  inventory code read off thermal paper is the one place where digit
//  alignment earns its keep.

import SwiftUI

enum MP {

    // MARK: - colour

    /// One palette, resolved per colour scheme. Written as
    /// `Color(light:dark:)` so a view never asks which theme it is in -
    /// the environment already knows, and a view that branches on it is
    /// a view that will be wrong in a screenshot.
    struct Palette {
        /// `--bg` - the canvas.
        static let bg = Color(light: 0xfbf8f2, dark: 0x100e09)
        /// `--rai` - raised: cards, rows, chips.
        static let raised = Color(light: 0xffffff, dark: 0x1b180f)
        /// `--sun` - sunken: wells, the area behind a scroll.
        static let sunken = Color(light: 0xf4efe6, dark: 0x0a0906)
        /// `--elev` - elevated: sheets and docks over content.
        static let elevated = Color(light: 0xf4efe6, dark: 0x252017)

        /// `--fg` / `--mut` / `--sub` - the three weights of text. Three
        /// and not two: the design leans on a genuinely subtle third for
        /// captions, and collapsing it into `muted` flattens every screen.
        static let fg = Color(light: 0x1b180f, dark: 0xf4efe6)
        static let muted = Color(light: 0x6b6355, dark: 0xa89f8b)
        static let subtle = Color(light: 0xa89f8b, dark: 0x6b6355)

        /// `--bd` / `--bds` - hairline and strong borders.
        static let border = Color(light: 0xe6dfd0, dark: 0x2f2a22)
        static let borderStrong = Color(light: 0xd0c5af, dark: 0x4a4338)

        /// `--ac` / `--aci` / `--act` - March green, its ink, its tint.
        static let accent = Color(light: 0x3d9a46, dark: 0x6ebd72)
        static let accentInk = Color(light: 0xffffff, dark: 0x0e0c07)
        static let accentTint = Color(light: 0xdcecda, dark: 0x1a2f1c)

        /// `--al` / `--alt` / `--ale` - alert. Used for overdue, for a
        /// print failure, and for nothing decorative.
        static let alert = Color(light: 0xad2a1b, dark: 0xff7a5c)
        static let alertTint = Color(light: 0xfaeae5, dark: 0x2b1611)
        static let alertEdge = Color(light: 0xe0b6a9, dark: 0x7a3423)

        /// `--wa` / `--wat` - warning. Softer than alert: something to
        /// notice, not something that has gone wrong.
        static let warn = Color(light: 0x8a6a1e, dark: 0xd4b25a)
        static let warnTint = Color(light: 0xf7edd6, dark: 0x292013)
    }

    // MARK: - shape

    /// Radii as the design actually uses them. 12 dominates by a wide
    /// margin, 11 is the row, and the smaller ones are for tags and
    /// checks. `pill` is the design's 999px.
    enum R {
        static let sm: CGFloat = 8
        static let tag: CGFloat = 9
        static let chip: CGFloat = 10
        static let row: CGFloat = 11
        static let card: CGFloat = 12
        static let lg: CGFloat = 14
        static let pill: CGFloat = 999
    }

    /// The 4px base scale from `colors_and_type.css`.
    enum S {
        static let x1: CGFloat = 4
        static let x2: CGFloat = 8
        static let x3: CGFloat = 12
        static let x4: CGFloat = 16
        static let x5: CGFloat = 24
        static let x6: CGFloat = 32
        static let x7: CGFloat = 48
        static let x8: CGFloat = 64
    }
}

// MARK: - hex, and light/dark resolution

extension Color {
    /// Two hex literals, resolved by the environment's colour scheme.
    ///
    /// `UIColor(dynamicProvider:)` rather than a SwiftUI branch, so this
    /// resolves correctly everywhere a colour can be asked for -
    /// including inside `UIKit` views the app wraps, and in a snapshot
    /// taken in the other theme.
    init(light: UInt32, dark: UInt32) {
        self = Color(UIColor { traits in
            UIColor(hex: traits.userInterfaceStyle == .dark ? dark : light)
        })
    }
}

extension UIColor {
    convenience init(hex: UInt32) {
        self.init(red:   CGFloat((hex >> 16) & 0xff) / 255,
                  green: CGFloat((hex >> 8) & 0xff) / 255,
                  blue:  CGFloat(hex & 0xff) / 255,
                  alpha: 1)
    }
}
