import Foundation

struct UserProfile: Codable {
    let id: Int
    let name: String
    let email: String
    let isStaff: Bool
    let isSuperuser: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, email
        case isStaff = "is_staff"
        case isSuperuser = "is_superuser"
    }
}

struct Membership: Codable, Identifiable {
    let id: String
    let tenantID: String
    let tenantName: String
    let role: String

    enum CodingKeys: String, CodingKey {
        case id, role
        case tenantID = "tenant_id"
        case tenantName = "tenant_name"
    }
}

struct LoginResponse: Codable {
    let user: UserProfile
    let memberships: [Membership]
}

struct TerminalStatus: Codable, Identifiable {
    let id: String
    let name: String
    let shopID: String
    let shopName: String
    let deviceID: String
    let version: String
    let paired: Bool
    let revoked: Bool
    let status: String
    let lastSeenAt: String?

    enum CodingKeys: String, CodingKey {
        case id, name, version, paired, revoked, status
        case shopID = "shop_id"
        case shopName = "shop_name"
        case deviceID = "device_id"
        case lastSeenAt = "last_seen_at"
    }

    var isOnline: Bool { status == "online" }
}

struct SalesSummary: Codable {
    let count: Int
    let revenueCents: Int
    let averageTicketCents: Int

    enum CodingKeys: String, CodingKey {
        case count
        case revenueCents = "revenue_cents"
        case averageTicketCents = "average_ticket_cents"
    }
}

struct TerminalSales: Codable, Identifiable {
    var id: String { terminalID ?? terminalName }
    let terminalID: String?
    let terminalName: String
    let shopName: String
    let salesCount: Int
    let revenueCents: Int
    let lastSaleAt: String?

    enum CodingKeys: String, CodingKey {
        case terminalName = "terminal_name"
        case shopName = "shop_name"
        case terminalID = "terminal_id"
        case salesCount = "sales_count"
        case revenueCents = "revenue_cents"
        case lastSaleAt = "last_sale_at"
    }
}

struct PaymentTotals: Codable {
    let cash: Int
    let pix: Int
    let cardCredit: Int
    let cardDebit: Int
    let other: Int

    enum CodingKeys: String, CodingKey {
        case cash, pix, other
        case cardCredit = "card_credit"
        case cardDebit = "card_debit"
    }
}

struct FinanceSummary: Codable {
    let enabled: Bool
    let openPayablesCents: Int
    let openReceivablesCents: Int

    enum CodingKeys: String, CodingKey {
        case enabled
        case openPayablesCents = "open_payables_cents"
        case openReceivablesCents = "open_receivables_cents"
    }
}

struct MonitoringSnapshot: Codable {
    let generatedAt: String
    let onlineWindowSeconds: Int
    let period: MonitoringPeriod
    let terminalsTotal: Int
    let terminalsOnline: Int
    let terminals: [TerminalStatus]
    let sales: SalesSummary
    let salesByTerminal: [TerminalSales]
    let paymentsCents: PaymentTotals
    let finance: FinanceSummary

    enum CodingKeys: String, CodingKey {
        case period, terminals, sales, finance
        case generatedAt = "generated_at"
        case onlineWindowSeconds = "online_window_seconds"
        case terminalsTotal = "terminals_total"
        case terminalsOnline = "terminals_online"
        case salesByTerminal = "sales_by_terminal"
        case paymentsCents = "payments_cents"
    }
}

struct MonitoringPeriod: Codable {
    let date: String
    let from: String
    let to: String
}
