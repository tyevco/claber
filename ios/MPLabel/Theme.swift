//  Theme.swift
//
//  What is left here after `Design/Tokens.swift` took the palette: the
//  two bits of *logic* that are about this domain rather than about
//  looks, plus three colour aliases kept alive while the screens are
//  ported one at a time.

import SwiftUI

// MARK: - the aliases

/// These forward to `MP.Palette` so a screen that has not been restyled
/// yet still draws in the design's colours rather than the three
/// approximations that used to live here.
///
/// They are a migration aid and nothing more. Once every screen uses
/// `MP.Palette` directly these go, and nothing new should reach for
/// them - the palette has three text weights and several tints, and a
/// three-colour vocabulary is what made the first version look like
/// stock SwiftUI.
extension Color {
    static var mpAccent: Color { MP.Palette.accent }
    static var mpMuted: Color { MP.Palette.muted }
    static var mpAlert: Color { MP.Palette.alert }
}

/// Superseded by `MPError`, which uses the design's alert tint and edge
/// rather than a flat red bar. Kept for the same reason as the colours.
struct ErrorBanner: View {
    let message: String

    var body: some View { MPError(message: message) }
}

// MARK: - domain logic

/// Ship-by is a hard Facebook commitment, so urgency is said in words
/// rather than leaving her to subtract dates - the same rule the PWA
/// follows, and the same wording, so the two agree at a glance.
struct Due {
    let label: String
    let urgent: Bool

    init(shipBy: String?) {
        guard let s = shipBy, !s.isEmpty else {
            label = "—"; urgent = false; return
        }
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.timeZone = .current
        guard let date = f.date(from: s) else {
            label = s; urgent = false; return
        }
        let cal = Calendar.current
        let days = cal.dateComponents([.day],
                                      from: cal.startOfDay(for: Date()),
                                      to: cal.startOfDay(for: date)).day ?? 0
        switch days {
        case ..<0:  label = "OVERDUE";  urgent = true
        case 0:     label = "TODAY";    urgent = true
        case 1:     label = "TOMORROW"; urgent = false
        default:    label = "\(days) DAYS"; urgent = false
        }
    }
}

func money(_ v: Double?) -> String {
    guard let v else { return "—" }
    return String(format: "$%.2f", v)
}
