import SwiftUI

enum PDVTheme {
    static let navy = Color(red: 0.035, green: 0.12, blue: 0.24)
    static let blue = Color(red: 0.08, green: 0.42, blue: 0.84)
    static let blueDeep = Color(red: 0.04, green: 0.27, blue: 0.60)
    static let mint = Color(red: 0.18, green: 0.75, blue: 0.58)
    static let ink = Color(red: 0.08, green: 0.12, blue: 0.19)
    static let muted = Color(red: 0.38, green: 0.46, blue: 0.56)
    static let surface = Color(uiColor: .secondarySystemBackground)
}

struct BrandMark: View {
    var size: CGFloat = 58

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
            .fill(LinearGradient(colors: [PDVTheme.blue, PDVTheme.mint],
                                 startPoint: .topLeading, endPoint: .bottomTrailing))
            .frame(width: size, height: size)
            .overlay {
                Image(systemName: "chart.line.uptrend.xyaxis")
                    .font(.system(size: size * 0.42, weight: .bold))
                    .foregroundStyle(.white)
            }
            .shadow(color: PDVTheme.blue.opacity(0.24), radius: 14, y: 7)
    }
}

struct MonitorCardModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(18)
            .background(.background, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .stroke(Color.primary.opacity(0.055), lineWidth: 1)
            }
            .shadow(color: .black.opacity(0.055), radius: 16, y: 6)
    }
}

extension View {
    func monitorCard() -> some View { modifier(MonitorCardModifier()) }
}

func brl(_ cents: Int) -> String {
    let formatter = NumberFormatter()
    formatter.numberStyle = .currency
    formatter.currencyCode = "BRL"
    formatter.locale = Locale(identifier: "pt_BR")
    return formatter.string(from: NSNumber(value: Double(cents) / 100)) ?? "R$ 0,00"
}

func shortDate(_ value: String?) -> String {
    guard let value, let date = value.asDate else { return "Sem contato" }
    return date.formatted(.relative(presentation: .named))
}

extension String {
    var asDate: Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: self) ?? ISO8601DateFormatter().date(from: self)
    }
}
