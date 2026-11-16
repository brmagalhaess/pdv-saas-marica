import SwiftUI

/// Cores da marca. Textos usam `.primary`/`.secondary` do sistema para
/// funcionar no modo escuro.
enum PDVTheme {
    /// #146BD6
    static let blue = Color(red: 20 / 255, green: 107 / 255, blue: 214 / 255)
    /// #0A4599
    static let blueDeep = Color(red: 10 / 255, green: 69 / 255, blue: 153 / 255)
    /// #091F3D
    static let navy = Color(red: 9 / 255, green: 31 / 255, blue: 61 / 255)
    /// #2EBF94
    static let mint = Color(red: 46 / 255, green: 191 / 255, blue: 148 / 255)
    static let warning = Color.orange
    static let danger = Color.red
    static let card = Color(uiColor: .secondarySystemGroupedBackground)
    static let background = Color(uiColor: .systemGroupedBackground)
    static let field = Color(uiColor: .tertiarySystemFill)
}

struct BrandMark: View {
    @ScaledMetric(relativeTo: .largeTitle) private var size: CGFloat = 64

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.26, style: .continuous)
            .fill(LinearGradient(colors: [PDVTheme.blue, PDVTheme.blueDeep],
                                 startPoint: .topLeading, endPoint: .bottomTrailing))
            .frame(width: size, height: size)
            .overlay {
                Text("P2")
                    .font(.system(.title, design: .rounded).weight(.heavy))
                    .foregroundStyle(.white)
                    .minimumScaleFactor(0.5)
            }
            .accessibilityHidden(true)
    }
}

struct CardModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(PDVTheme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }
}

extension View {
    func card() -> some View { modifier(CardModifier()) }
}

/// Selo colorido (estado da venda, do terminal, saúde…).
struct StatusBadge: View {
    enum Tone { case positive, warning, negative, neutral, info }

    let text: String
    let tone: Tone

    var color: Color {
        switch tone {
        case .positive: return PDVTheme.mint
        case .warning: return PDVTheme.warning
        case .negative: return PDVTheme.danger
        case .neutral: return .secondary
        case .info: return PDVTheme.blue
        }
    }

    var body: some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .foregroundStyle(color)
            .background(color.opacity(0.14), in: Capsule())
            .fixedSize()
    }

    static func sale(_ status: String) -> StatusBadge {
        StatusBadge(text: Labels.saleStatus(status), tone: status == "completed" ? .positive : .negative)
    }

    static func terminal(_ status: String) -> StatusBadge {
        StatusBadge(text: Labels.terminalStatus(status),
                    tone: status == "online" ? .positive : status == "offline" ? .warning : .neutral)
    }

    static func health(_ health: String, text: String) -> StatusBadge {
        StatusBadge(text: text, tone: health == "ok" ? .positive : health == "attention" ? .warning : .neutral)
    }

    static func cashDifference(_ cents: Int?) -> StatusBadge {
        guard let cents else { return StatusBadge(text: "—", tone: .neutral) }
        return StatusBadge(text: Format.cashDifference(cents), tone: cents < 0 ? .negative : cents > 0 ? .warning : .positive)
    }
}

/// Linha "rótulo …… valor".
struct ValueRow: View {
    let title: String
    let value: String
    var emphasized = false

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).foregroundStyle(emphasized ? .primary : .secondary)
            Spacer(minLength: 12)
            Text(value)
                .fontWeight(emphasized ? .semibold : .regular)
                .multilineTextAlignment(.trailing)
                .monospacedDigit()
        }
        .font(.subheadline)
        .accessibilityElement(children: .combine)
    }
}
