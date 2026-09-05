//  Theme.swift
//
//  The PWA's palette, so the two do not look like different products
//  while both are on her phone. Values lifted from app.css.

import SwiftUI

extension Color {
    static let mpAccent = Color(red: 0.431, green: 0.741, blue: 0.447)   // #6ebd72
    static let mpMuted  = Color(red: 0.659, green: 0.624, blue: 0.545)   // #a89f8b
    static let mpAlert  = Color(red: 0.878, green: 0.435, blue: 0.376)   // warm red
}

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

/// One place for "the request failed and she needs to know". The server
/// writes its refusals as sentences meant for a person, so they are
/// shown verbatim.
struct ErrorBanner: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.footnote)
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(Color.mpAlert, in: RoundedRectangle(cornerRadius: 10))
            .padding(.horizontal)
    }
}
