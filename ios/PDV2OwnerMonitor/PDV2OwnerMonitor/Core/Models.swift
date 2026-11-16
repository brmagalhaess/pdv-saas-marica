import Foundation

// DTOs do contrato /api/v1/app/* (seção 6). Chaves snake_case explícitas;
// opcionais onde o contrato admite `null`. Chaves desconhecidas são ignoradas
// pelo `Decodable` padrão, então o servidor pode acrescentar campos.

// MARK: - Objetos comuns

struct UserRef: Codable, Hashable {
    let id: Int
    let name: String
    let email: String
}

struct ShopRef: Codable, Hashable, Identifiable {
    let id: String
    let name: String
}

struct TenantRef: Codable, Hashable, Identifiable {
    let id: String
    let name: String
}

struct TokenPair: Codable, Equatable {
    let accessToken: String
    let accessExpiresAt: String
    let accessExpiresIn: Int
    let refreshToken: String
    let refreshExpiresAt: String

    enum CodingKeys: String, CodingKey {
        case accessToken = "access_token"
        case accessExpiresAt = "access_expires_at"
        case accessExpiresIn = "access_expires_in"
        case refreshToken = "refresh_token"
        case refreshExpiresAt = "refresh_expires_at"
    }
}

struct AppDeviceInfo: Codable, Hashable, Identifiable {
    let id: String
    let platform: String
    let name: String
    let model: String
    let osVersion: String
    let appVersion: String
    let createdAt: String
    let lastSeenAt: String?
    let current: Bool

    enum CodingKeys: String, CodingKey {
        case id, platform, name, model, current
        case osVersion = "os_version"
        case appVersion = "app_version"
        case createdAt = "created_at"
        case lastSeenAt = "last_seen_at"
    }
}

/// Permissões que o administrador libera por empresa.
enum AppPermission: String, CaseIterable, Codable, Identifiable {
    case sales, cash, stock, credit, terminals

    var id: String { rawValue }

    var label: String {
        switch self {
        case .sales: return "Vendas"
        case .cash: return "Caixa"
        case .stock: return "Estoque"
        case .credit: return "Fiado"
        case .terminals: return "Terminais e conflitos"
        }
    }
}

struct TenantAccess: Codable, Hashable, Identifiable {
    let id: String
    let name: String
    let allShops: Bool
    let shops: [ShopRef]
    let permissions: [String]
    let accessExpiresOn: String?

    enum CodingKeys: String, CodingKey {
        case id, name, shops, permissions
        case allShops = "all_shops"
        case accessExpiresOn = "access_expires_on"
    }

    func has(_ permission: AppPermission) -> Bool { permissions.contains(permission.rawValue) }

    /// Empresa aberta pela equipe da plataforma: todas as lojas e permissões.
    static func platformView(id: String, name: String, shops: [ShopRef] = []) -> TenantAccess {
        TenantAccess(id: id, name: name, allShops: true, shops: shops,
                     permissions: AppPermission.allCases.map(\.rawValue), accessExpiresOn: nil)
    }

    func with(shops newShops: [ShopRef]) -> TenantAccess {
        TenantAccess(id: id, name: name, allShops: allShops, shops: newShops,
                     permissions: permissions, accessExpiresOn: accessExpiresOn)
    }
}

struct PlatformInfo: Codable, Hashable {
    let enabled: Bool
    let superuser: Bool
    let canManageAccess: Bool
    let roles: [String]

    enum CodingKeys: String, CodingKey {
        case enabled, superuser, roles
        case canManageAccess = "can_manage_access"
    }
}

struct PublicLinks: Codable, Hashable {
    let privacyURL: String
    let supportURL: String
    let accountDeletionURL: String

    enum CodingKeys: String, CodingKey {
        case privacyURL = "privacy_url"
        case supportURL = "support_url"
        case accountDeletionURL = "account_deletion_url"
    }

    static let production = PublicLinks(privacyURL: "https://pdv2.crowntech.com.br/privacidade/",
                                        supportURL: "https://pdv2.crowntech.com.br/suporte/",
                                        accountDeletionURL: "https://pdv2.crowntech.com.br/excluir-conta/")
}

struct Me: Codable, Hashable {
    let user: UserRef
    let device: AppDeviceInfo
    let platform: PlatformInfo?
    let tenants: [TenantAccess]
    let links: PublicLinks
    let serverTime: String

    enum CodingKeys: String, CodingKey {
        case user, device, platform, tenants, links
        case serverTime = "server_time"
    }

    var hasPlatform: Bool { platform?.enabled == true }
}

// MARK: - Autenticação

struct MFAUser: Codable, Hashable {
    let name: String
    let email: String
}

struct LoginResponse: Decodable {
    let status: String
    let tokens: TokenPair?
    let me: Me?
    let mfaToken: String?
    let mfaExpiresAt: String?
    let user: MFAUser?

    enum CodingKeys: String, CodingKey {
        case status, tokens, me, user
        case mfaToken = "mfa_token"
        case mfaExpiresAt = "mfa_expires_at"
    }
}

struct RefreshResponse: Decodable {
    let tokens: TokenPair
}

struct OkResponse: Decodable {
    let ok: Bool
}

struct LoginRequest: Encodable {
    struct Device: Encodable, Equatable {
        let platform: String
        let name: String
        let model: String
        let osVersion: String
        let appVersion: String

        enum CodingKeys: String, CodingKey {
            case platform, name, model
            case osVersion = "os_version"
            case appVersion = "app_version"
        }
    }

    let email: String
    let password: String
    let device: Device
}

struct MFARequest: Encodable {
    let mfaToken: String
    let code: String?
    let recoveryCode: String?

    enum CodingKeys: String, CodingKey {
        case mfaToken = "mfa_token"
        case code
        case recoveryCode = "recovery_code"
    }

    // Exatamente um dos dois: o servidor recusa chaves a mais.
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(mfaToken, forKey: .mfaToken)
        if let code { try container.encode(code, forKey: .code) }
        if let recoveryCode { try container.encode(recoveryCode, forKey: .recoveryCode) }
    }
}

struct RefreshRequest: Encodable {
    let refreshToken: String
    enum CodingKeys: String, CodingKey { case refreshToken = "refresh_token" }
}

struct LogoutRequest: Encodable {
    let refreshToken: String?
    enum CodingKeys: String, CodingKey { case refreshToken = "refresh_token" }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let refreshToken { try container.encode(refreshToken, forKey: .refreshToken) }
    }
}

// MARK: - Paginação

struct Page<Item> {
    let results: [Item]
    let nextCursor: String?
    /// Dados da lista inteira que vêm junto da página (totais do Fiado, contagens do Estoque).
    let summary: (any Sendable)?

    init(results: [Item], nextCursor: String?, summary: (any Sendable)? = nil) {
        self.results = results
        self.nextCursor = nextCursor
        self.summary = summary
    }
}

extension Page: Decodable where Item: Decodable {
    enum CodingKeys: String, CodingKey {
        case results
        case nextCursor = "next_cursor"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        results = try container.decode([Item].self, forKey: .results)
        nextCursor = try container.decodeIfPresent(String.self, forKey: .nextCursor)
        summary = nil
    }
}

// MARK: - Período

enum Period: String, CaseIterable, Identifiable, Codable {
    case today
    case yesterday
    case sevenDays = "7d"
    case month

    var id: String { rawValue }

    var label: String {
        switch self {
        case .today: return "Hoje"
        case .yesterday: return "Ontem"
        case .sevenDays: return "7 dias"
        case .month: return "Mês"
        }
    }
}

struct PeriodInfo: Codable, Hashable {
    let key: String
    let label: String
    let dateFrom: String
    let dateTo: String
    let from: String
    let to: String

    enum CodingKeys: String, CodingKey {
        case key, label, from, to
        case dateFrom = "date_from"
        case dateTo = "date_to"
    }
}

// MARK: - Resumo (dashboard)

struct Dashboard: Decodable {
    let generatedAt: String
    let tenant: TenantRef
    let shop: ShopRef?
    let shops: [ShopRef]
    let period: PeriodInfo
    let permissions: [String]
    let sales: SalesSummary?
    let payments: PaymentsSummary?
    let series: Series?
    let cash: CashSummary?
    let stock: StockSummary?
    let credit: CreditSummary?
    let terminals: TerminalsSummary?

    enum CodingKeys: String, CodingKey {
        case tenant, shop, shops, period, permissions, sales, payments, series, cash, stock, credit, terminals
        case generatedAt = "generated_at"
    }
}

struct SalesSummary: Decodable {
    let count: Int
    let revenueCents: Int
    let averageTicketCents: Int
    let cancelledCount: Int
    let cancelledCents: Int
    let returnsCents: Int
    let netRevenueCents: Int

    enum CodingKeys: String, CodingKey {
        case count
        case revenueCents = "revenue_cents"
        case averageTicketCents = "average_ticket_cents"
        case cancelledCount = "cancelled_count"
        case cancelledCents = "cancelled_cents"
        case returnsCents = "returns_cents"
        case netRevenueCents = "net_revenue_cents"
    }
}

struct PaymentsSummary: Decodable {
    let cashCents: Int
    let pixCents: Int
    let cardCreditCents: Int
    let cardDebitCents: Int
    let otherCents: Int
    let receivedTotalCents: Int
    let creditAccountCents: Int
    let storeCreditCents: Int
    let creditReceivedCents: Int

    enum CodingKeys: String, CodingKey {
        case cashCents = "cash_cents"
        case pixCents = "pix_cents"
        case cardCreditCents = "card_credit_cents"
        case cardDebitCents = "card_debit_cents"
        case otherCents = "other_cents"
        case receivedTotalCents = "received_total_cents"
        case creditAccountCents = "credit_account_cents"
        case storeCreditCents = "store_credit_cents"
        case creditReceivedCents = "credit_received_cents"
    }
}

struct SeriesPoint: Decodable, Identifiable, Hashable {
    let key: String
    let label: String
    let count: Int
    let revenueCents: Int

    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, label, count
        case revenueCents = "revenue_cents"
    }
}

struct Series: Decodable {
    let kind: String
    let points: [SeriesPoint]
}

struct CashSummary: Decodable {
    let openCount: Int
    let closedCount: Int
    let shortageCount: Int
    let shortageCents: Int

    enum CodingKeys: String, CodingKey {
        case openCount = "open_count"
        case closedCount = "closed_count"
        case shortageCount = "shortage_count"
        case shortageCents = "shortage_cents"
    }
}

struct StockSummary: Decodable {
    let zeroedCount: Int
    let belowMinimumCount: Int

    enum CodingKeys: String, CodingKey {
        case zeroedCount = "zeroed_count"
        case belowMinimumCount = "below_minimum_count"
    }
}

struct CreditSummary: Decodable {
    let openCents: Int
    let overdueCents: Int
    let overdueCustomers: Int

    enum CodingKeys: String, CodingKey {
        case openCents = "open_cents"
        case overdueCents = "overdue_cents"
        case overdueCustomers = "overdue_customers"
    }
}

struct TerminalsSummary: Decodable {
    let total: Int
    let online: Int
    let offline: Int
    let unpaired: Int
    let onlineWindowSeconds: Int
    let conflictsOpen: Int

    enum CodingKeys: String, CodingKey {
        case total, online, offline, unpaired
        case onlineWindowSeconds = "online_window_seconds"
        case conflictsOpen = "conflicts_open"
    }
}

// MARK: - Vendas

struct SaleListItem: Decodable, Identifiable {
    let id: String
    let createdAt: String
    let shop: ShopRef
    let terminalName: String
    let stationName: String
    let operatorName: String
    let customerName: String
    let totalCents: Int
    let status: String
    let itemsCount: Int
    let paymentMethods: [String]

    enum CodingKeys: String, CodingKey {
        case id, shop, status
        case createdAt = "created_at"
        case terminalName = "terminal_name"
        case stationName = "station_name"
        case operatorName = "operator_name"
        case customerName = "customer_name"
        case totalCents = "total_cents"
        case itemsCount = "items_count"
        case paymentMethods = "payment_methods"
    }
}

struct SaleItem: Decodable, Identifiable {
    let line: Int
    let name: String
    let unit: String
    let quantityMilli: Int
    let unitPriceCents: Int
    let grossCents: Int
    let promoDiscountCents: Int
    let discountCents: Int
    let lineTotalCents: Int

    var id: Int { line }

    enum CodingKeys: String, CodingKey {
        case line, name, unit
        case quantityMilli = "quantity_milli"
        case unitPriceCents = "unit_price_cents"
        case grossCents = "gross_cents"
        case promoDiscountCents = "promo_discount_cents"
        case discountCents = "discount_cents"
        case lineTotalCents = "line_total_cents"
    }
}

struct MethodAmount: Decodable, Identifiable {
    let method: String
    let label: String
    let amountCents: Int

    var id: String { method + label }

    enum CodingKeys: String, CodingKey {
        case method, label
        case amountCents = "amount_cents"
    }
}

struct SaleReturn: Decodable, Identifiable {
    let id: String
    let createdAt: String
    let totalCents: Int
    let refundMethod: String
    let refundLabel: String
    let reason: String

    enum CodingKeys: String, CodingKey {
        case id, reason
        case createdAt = "created_at"
        case totalCents = "total_cents"
        case refundMethod = "refund_method"
        case refundLabel = "refund_label"
    }
}

struct SaleDetail: Decodable, Identifiable {
    let id: String
    let createdAt: String
    let receivedAt: String?
    let status: String
    let shop: ShopRef
    let terminalName: String
    let stationName: String
    let operatorName: String
    let sellerName: String
    let customerName: String
    let grossCents: Int
    let promoDiscountCents: Int
    let discountCents: Int
    let totalCents: Int
    let changeCents: Int
    let items: [SaleItem]
    let payments: [MethodAmount]
    let returns: [SaleReturn]
    let cancelledAt: String?
    let cancellationReason: String

    enum CodingKeys: String, CodingKey {
        case id, status, shop, items, payments, returns
        case createdAt = "created_at"
        case receivedAt = "received_at"
        case terminalName = "terminal_name"
        case stationName = "station_name"
        case operatorName = "operator_name"
        case sellerName = "seller_name"
        case customerName = "customer_name"
        case grossCents = "gross_cents"
        case promoDiscountCents = "promo_discount_cents"
        case discountCents = "discount_cents"
        case totalCents = "total_cents"
        case changeCents = "change_cents"
        case cancelledAt = "cancelled_at"
        case cancellationReason = "cancellation_reason"
    }
}

// MARK: - Caixa

struct CashSession: Decodable, Identifiable {
    let id: String
    let shop: ShopRef
    let terminalName: String
    let stationName: String
    let operatorName: String
    let status: String
    let openedAt: String
    let closedAt: String?
    let openingCents: Int
    let salesCount: Int
    let salesTotalCents: Int
    let cashNetCents: Int
    let suppliedCents: Int
    let withdrawnCents: Int
    let expectedCents: Int?
    let countedCents: Int?
    let differenceCents: Int?
    let incomplete: Bool

    enum CodingKeys: String, CodingKey {
        case id, shop, status, incomplete
        case terminalName = "terminal_name"
        case stationName = "station_name"
        case operatorName = "operator_name"
        case openedAt = "opened_at"
        case closedAt = "closed_at"
        case openingCents = "opening_cents"
        case salesCount = "sales_count"
        case salesTotalCents = "sales_total_cents"
        case cashNetCents = "cash_net_cents"
        case suppliedCents = "supplied_cents"
        case withdrawnCents = "withdrawn_cents"
        case expectedCents = "expected_cents"
        case countedCents = "counted_cents"
        case differenceCents = "difference_cents"
    }
}

struct CashMovement: Decodable, Identifiable {
    let id: String
    let kind: String
    let label: String
    let amountCents: Int
    let reason: String
    let operatorName: String
    let createdAt: String

    enum CodingKeys: String, CodingKey {
        case id, kind, label, reason
        case amountCents = "amount_cents"
        case operatorName = "operator_name"
        case createdAt = "created_at"
    }
}

/// Detalhe do turno: mesmos campos da lista e mais os desta struct.
struct CashSessionDetail: Decodable {
    let session: CashSession
    let closedByName: String
    let supervisorName: String
    let creditReceivedCents: Int
    let byMethod: [MethodAmount]
    let movements: [CashMovement]

    enum CodingKeys: String, CodingKey {
        case movements
        case closedByName = "closed_by_name"
        case supervisorName = "supervisor_name"
        case creditReceivedCents = "credit_received_cents"
        case byMethod = "by_method"
    }

    init(from decoder: Decoder) throws {
        session = try CashSession(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        closedByName = try container.decode(String.self, forKey: .closedByName)
        supervisorName = try container.decode(String.self, forKey: .supervisorName)
        creditReceivedCents = try container.decode(Int.self, forKey: .creditReceivedCents)
        byMethod = try container.decode([MethodAmount].self, forKey: .byMethod)
        movements = try container.decode([CashMovement].self, forKey: .movements)
    }
}

// MARK: - Estoque

struct StockAlert: Decodable, Identifiable {
    let shop: ShopRef
    let productID: String
    let name: String
    let sku: String
    let unit: String
    let quantityMilli: Int
    let minimumMilli: Int
    let status: String

    var id: String { productID + "@" + shop.id }

    enum CodingKeys: String, CodingKey {
        case shop, name, sku, unit, status
        case productID = "product_id"
        case quantityMilli = "quantity_milli"
        case minimumMilli = "minimum_milli"
    }
}

struct StockCounts: Decodable, Equatable {
    let zeroed: Int
    let belowMinimum: Int

    enum CodingKeys: String, CodingKey {
        case zeroed
        case belowMinimum = "below_minimum"
    }
}

struct StockAlertsPage: Decodable {
    let counts: StockCounts
    let results: [StockAlert]
    let nextCursor: String?

    enum CodingKeys: String, CodingKey {
        case counts, results
        case nextCursor = "next_cursor"
    }
}

// MARK: - Fiado

struct CreditTotals: Decodable, Equatable {
    let openCents: Int
    let overdueCents: Int
    let customers: Int
    let overdueCustomers: Int

    enum CodingKeys: String, CodingKey {
        case customers
        case openCents = "open_cents"
        case overdueCents = "overdue_cents"
        case overdueCustomers = "overdue_customers"
    }
}

struct CreditCustomer: Decodable, Identifiable {
    let customerID: String
    let name: String
    let phone: String
    let openCents: Int
    let overdueCents: Int
    let oldestDueDate: String?
    let entriesCount: Int

    var id: String { customerID }

    enum CodingKeys: String, CodingKey {
        case name, phone
        case customerID = "customer_id"
        case openCents = "open_cents"
        case overdueCents = "overdue_cents"
        case oldestDueDate = "oldest_due_date"
        case entriesCount = "entries_count"
    }
}

struct CreditPage: Decodable {
    let totals: CreditTotals
    let results: [CreditCustomer]
    let nextCursor: String?

    enum CodingKeys: String, CodingKey {
        case totals, results
        case nextCursor = "next_cursor"
    }
}

// MARK: - Terminais e conflitos

struct TerminalItem: Decodable, Identifiable {
    let id: String
    let name: String
    let shop: ShopRef
    let status: String
    let version: String
    let lastSeenAt: String?
    let pendingEvents: Int
    let error: String

    enum CodingKeys: String, CodingKey {
        case id, name, shop, status, version, error
        case lastSeenAt = "last_seen_at"
        case pendingEvents = "pending_events"
    }
}

struct TerminalsPage: Decodable {
    let onlineWindowSeconds: Int
    let results: [TerminalItem]
    let nextCursor: String?

    enum CodingKeys: String, CodingKey {
        case results
        case onlineWindowSeconds = "online_window_seconds"
        case nextCursor = "next_cursor"
    }
}

struct SyncConflict: Decodable, Identifiable {
    let id: String
    let createdAt: String
    let shop: ShopRef
    let terminalName: String
    let reason: String
    let reasonText: String
    let status: String
    let statusText: String

    enum CodingKeys: String, CodingKey {
        case id, shop, reason, status
        case createdAt = "created_at"
        case terminalName = "terminal_name"
        case reasonText = "reason_text"
        case statusText = "status_text"
    }
}

// MARK: - Conta

struct DeletionRequest: Encodable {
    let note: String?

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let note, !note.isEmpty { try container.encode(note, forKey: .note) }
    }

    enum CodingKeys: String, CodingKey { case note }
}

struct DeletionResponse: Decodable {
    let status: String
    let message: String
}

// MARK: - Plataforma

struct PlatformTenant: Decodable, Identifiable {
    let id: String
    let name: String
    let active: Bool
    let subscriptionStatus: String
    let subscriptionStatusText: String
    let shopsCount: Int
    let terminalsTotal: Int
    let terminalsOffline: Int
    let conflictsOpen: Int
    let lastSaleAt: String?
    let lastSeenAt: String?
    let health: String
    let healthText: String

    enum CodingKeys: String, CodingKey {
        case id, name, active, health
        case subscriptionStatus = "subscription_status"
        case subscriptionStatusText = "subscription_status_text"
        case shopsCount = "shops_count"
        case terminalsTotal = "terminals_total"
        case terminalsOffline = "terminals_offline"
        case conflictsOpen = "conflicts_open"
        case lastSaleAt = "last_sale_at"
        case lastSeenAt = "last_seen_at"
        case healthText = "health_text"
    }
}

struct AppAccessItem: Decodable, Identifiable {
    let id: String
    let scope: String
    let user: UserRef
    let tenant: TenantRef?
    let allShops: Bool
    let shops: [ShopRef]
    let permissions: [String]
    let reason: String
    let grantedBy: UserRef?
    let grantedAt: String
    let expiresOn: String?
    let status: String
    let statusText: String
    let revokedAt: String?
    let revokedBy: UserRef?
    let revokeReason: String
    let devicesActive: Int

    enum CodingKeys: String, CodingKey {
        case id, scope, user, tenant, shops, permissions, reason, status
        case allShops = "all_shops"
        case grantedBy = "granted_by"
        case grantedAt = "granted_at"
        case expiresOn = "expires_on"
        case statusText = "status_text"
        case revokedAt = "revoked_at"
        case revokedBy = "revoked_by"
        case revokeReason = "revoke_reason"
        case devicesActive = "devices_active"
    }
}

/// Corpo de `POST /api/v1/app/platform/access`. Escopo `platform` não leva
/// `tenant_id`, `shop_ids` nem `permissions`; `expires_on` vai sempre (pode ser null).
struct AccessGrantRequest: Encodable, Equatable {
    let scope: String
    let userID: Int
    let tenantID: String?
    let shopIDs: [String]?
    let permissions: [String]?
    let expiresOn: String?
    let reason: String

    enum CodingKeys: String, CodingKey {
        case scope, permissions, reason
        case userID = "user_id"
        case tenantID = "tenant_id"
        case shopIDs = "shop_ids"
        case expiresOn = "expires_on"
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(scope, forKey: .scope)
        try container.encode(userID, forKey: .userID)
        if scope == "tenant" {
            try container.encode(tenantID, forKey: .tenantID)
            try container.encode(shopIDs ?? [], forKey: .shopIDs)
            try container.encode(permissions ?? [], forKey: .permissions)
        }
        try container.encode(expiresOn, forKey: .expiresOn)
        try container.encode(reason, forKey: .reason)
    }
}

struct ReasonRequest: Encodable {
    let reason: String
}

struct AccessMember: Decodable, Identifiable, Hashable {
    let userID: Int
    let name: String
    let email: String
    let role: String
    let roleText: String
    let hasActiveAccess: Bool

    var id: Int { userID }

    enum CodingKeys: String, CodingKey {
        case name, email, role
        case userID = "user_id"
        case roleText = "role_text"
        case hasActiveAccess = "has_active_access"
    }
}

struct AccessOptions: Decodable {
    let tenant: TenantRef?
    let shops: [ShopRef]
    let members: [AccessMember]
}

struct PlatformDevice: Decodable, Identifiable {
    let id: String
    let platform: String
    let name: String
    let model: String
    let osVersion: String
    let appVersion: String
    let createdAt: String
    let lastSeenAt: String?
    let lastIP: String
    let user: UserRef
    let status: String
    let revokedAt: String?
    let revokeReason: String

    enum CodingKeys: String, CodingKey {
        case id, platform, name, model, user, status
        case osVersion = "os_version"
        case appVersion = "app_version"
        case createdAt = "created_at"
        case lastSeenAt = "last_seen_at"
        case lastIP = "last_ip"
        case revokedAt = "revoked_at"
        case revokeReason = "revoke_reason"
    }
}

// MARK: - Erro da API

struct APIErrorBody: Decodable {
    let error: String?
    let code: String?
    let retryAfterSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case error, code
        case retryAfterSeconds = "retry_after_seconds"
    }
}
