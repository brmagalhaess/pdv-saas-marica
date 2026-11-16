import Foundation

/// Formatação em pt-BR. Datas no fuso de São Paulo (o mesmo dos períodos do servidor).
enum Format {
    static let timeZone = TimeZone(identifier: "America/Sao_Paulo") ?? .current
    static let locale = Locale(identifier: "pt_BR")

    // MARK: Dinheiro

    /// `123456` → `"R$ 1.234,56"`; negativo → `"-R$ 15,00"`.
    static func money(_ cents: Int) -> String {
        let magnitude = cents.magnitude
        let reais = grouped(String(magnitude / 100))
        let centavos = magnitude % 100
        return (cents < 0 ? "-" : "") + "R$ " + reais + "," + (centavos < 10 ? "0" : "") + String(centavos)
    }

    private static func grouped(_ digits: String) -> String {
        var result = ""
        for (index, character) in digits.reversed().enumerated() {
            if index > 0 && index % 3 == 0 { result.append(".") }
            result.append(character)
        }
        return String(result.reversed())
    }

    // MARK: Quantidade

    /// UN como inteiro (`"12 un"`); KG e L com 3 casas e vírgula (`"1,250 kg"`).
    static func quantity(milli: Int, unit: String) -> String {
        switch unit.uppercased() {
        case "KG": return decimal3(milli) + " kg"
        case "L": return decimal3(milli) + " L"
        default:
            let rounded = (milli >= 0 ? milli + 500 : milli - 500) / 1000
            return (rounded < 0 ? "-" : "") + grouped(String(rounded.magnitude)) + " un"
        }
    }

    private static func decimal3(_ milli: Int) -> String {
        let magnitude = milli.magnitude
        let fraction = String(magnitude % 1000)
        let padded = String(repeating: "0", count: 3 - fraction.count) + fraction
        return (milli < 0 ? "-" : "") + grouped(String(magnitude / 1000)) + "," + padded
    }

    // MARK: Datas

    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static let isoFractionalFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static func formatter(_ pattern: String) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = timeZone
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.dateFormat = pattern
        return formatter
    }

    private static let dateTimeFormatter = formatter("dd/MM/yyyy HH:mm")
    private static let timeFormatter = formatter("HH:mm")
    private static let dayFormatter = formatter("dd/MM/yyyy")
    private static let ymdFormatter = formatter("yyyy-MM-dd")

    /// `"2026-11-16T14:03:12-03:00"` → `Date`.
    static func parse(_ value: String?) -> Date? {
        guard let value, !value.isEmpty else { return nil }
        return isoFormatter.date(from: value) ?? isoFractionalFormatter.date(from: value)
    }

    /// `"16/11/2026 14:03"`; nulo → `"—"`.
    static func dateTime(_ value: String?) -> String {
        guard let date = parse(value) else { return "—" }
        return dateTimeFormatter.string(from: date)
    }

    static func dateTime(_ date: Date) -> String { dateTimeFormatter.string(from: date) }

    /// `"14:03"`.
    static func time(_ value: String?) -> String {
        guard let date = parse(value) else { return "—" }
        return timeFormatter.string(from: date)
    }

    static func time(_ date: Date) -> String { timeFormatter.string(from: date) }

    /// `"2026-10-20"` → `"20/10/2026"`.
    static func day(_ value: String?) -> String {
        guard let value, let date = ymdFormatter.date(from: value) else { return "—" }
        return dayFormatter.string(from: date)
    }

    /// `Date` → `"2026-11-30"` (para `expires_on`).
    static func ymd(_ date: Date) -> String { ymdFormatter.string(from: date) }

    /// Hora para "Hoje"; data e hora para os outros períodos.
    static func saleMoment(_ value: String?, period: Period) -> String {
        period == .today ? time(value) : dateTime(value)
    }

    /// `"agora mesmo"`, `"há 3 min"`, `"há 2 h"`, `"há 4 dias"`.
    static func relative(_ date: Date, now: Date = Date()) -> String {
        let seconds = max(0, Int(now.timeIntervalSince(date)))
        if seconds < 60 { return "agora mesmo" }
        let minutes = seconds / 60
        if minutes < 60 { return "há \(minutes) min" }
        let hours = minutes / 60
        if hours < 24 { return "há \(hours) h" }
        let days = hours / 24
        return days == 1 ? "há 1 dia" : "há \(days) dias"
    }

    static func relative(_ value: String?, now: Date = Date()) -> String {
        guard let date = parse(value) else { return "Sem contato" }
        return relative(date, now: now)
    }

    /// Rodapé "Atualizado há X".
    static func updated(_ date: Date, now: Date = Date()) -> String {
        let text = relative(date, now: now)
        return text == "agora mesmo" ? "Atualizado agora mesmo" : "Atualizado \(text)"
    }

    // MARK: Caixa

    /// Diferença do caixa: `"Faltou R$ 15,00"`, `"Sobrou R$ 2,00"`, `"Sem diferença"`, `"—"`.
    static func cashDifference(_ cents: Int?) -> String {
        guard let cents else { return "—" }
        if cents < 0 { return "Faltou " + money(-cents) }
        if cents > 0 { return "Sobrou " + money(cents) }
        return "Sem diferença"
    }

    static func plural(_ count: Int, _ singular: String, _ plural: String) -> String {
        "\(count) " + (count == 1 ? singular : plural)
    }
}

/// Rótulos fixos do contrato (seção 6).
enum Labels {
    static func payment(_ method: String, name: String? = nil) -> String {
        switch method {
        case "cash": return "Dinheiro"
        case "pix": return "Pix"
        case "card_credit": return "Cartão de crédito"
        case "card_debit": return "Cartão de débito"
        case "credit_account": return "Fiado"
        case "store_credit": return "Crédito da loja"
        case "card": return "Cartão"
        default:
            if let name, !name.isEmpty { return name }
            return "Outra forma"
        }
    }

    static func saleStatus(_ status: String) -> String {
        switch status {
        case "completed": return "Concluída"
        case "cancelled": return "Cancelada"
        case "refunded": return "Estornada"
        default: return status
        }
    }

    static func refund(_ method: String) -> String {
        switch method {
        case "cash": return "Dinheiro"
        case "store_credit": return "Crédito da loja"
        case "account": return "Abatido no fiado"
        case "external": return "Fora do PDV"
        default: return method
        }
    }

    static func terminalStatus(_ status: String) -> String {
        switch status {
        case "online": return "Online"
        case "offline": return "Sem contato"
        case "unpaired": return "Não pareado"
        default: return status
        }
    }

    static func cashStatus(_ status: String) -> String {
        status == "open" ? "Aberto" : status == "closed" ? "Fechado" : status
    }

    static func movement(_ kind: String) -> String {
        switch kind {
        case "withdrawn": return "Sangria"
        case "supplied": return "Suprimento"
        default: return kind
        }
    }

    static func permission(_ key: String) -> String {
        AppPermission(rawValue: key)?.label ?? key
    }

    static func permissions(_ keys: [String]) -> String {
        if Set(keys) == Set(AppPermission.allCases.map(\.rawValue)) { return "Todas" }
        return keys.map(permission).joined(separator: ", ")
    }

    static func stockStatus(_ status: String) -> String {
        switch status {
        case "zeroed": return "Zerado"
        case "below_minimum": return "Abaixo do mínimo"
        default: return status
        }
    }

    static func platform(_ value: String) -> String {
        switch value {
        case "ios": return "iPhone"
        case "android": return "Android"
        default: return value
        }
    }
}
