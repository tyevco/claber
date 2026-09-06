//  Theme.swift
//
//  What is left here after `Design/Tokens.swift` took the palette: the
//  two bits of *logic* that are about this domain rather than about
//  looks. Both are shared with the PWA's `app.js` by intent - the two
//  clients must say the same thing about the same parcel, and "TODAY"
//  in one and "0 DAYS" in the other is a bug she would have to notice
//  herself.

import SwiftUI

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
