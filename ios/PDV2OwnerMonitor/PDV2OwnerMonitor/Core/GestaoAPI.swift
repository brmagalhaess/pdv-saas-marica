import Foundation

/// Empresa, loja e período de uma consulta.
struct QueryContext: Hashable {
    let tenantID: String
    let shopID: String?
    let period: Period

    var shopItem: URLQueryItem { URLQueryItem(name: "shop_id", value: shopID) }
    var periodItem: URLQueryItem { URLQueryItem(name: "period", value: period.rawValue) }
}

/// Todas as leituras e ações que as telas usam. As telas só conhecem este
/// protocolo; os Previews usam `PreviewGestaoAPI` com dados fictícios.
protocol GestaoAPI: AnyObject {
    func me() async throws -> Me
    func tenants(query: String?, cursor: String?) async throws -> Page<TenantAccess>
    func dashboard(_ context: QueryContext) async throws -> Dashboard
    func sales(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<SaleListItem>
    func sale(id: String, tenantID: String) async throws -> SaleDetail
    func cashSessions(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<CashSession>
    func cashSession(id: String, tenantID: String) async throws -> CashSessionDetail
    func stockAlerts(_ context: QueryContext, status: String, cursor: String?) async throws -> StockAlertsPage
    func credit(_ context: QueryContext, status: String, cursor: String?) async throws -> CreditPage
    func terminals(_ context: QueryContext) async throws -> TerminalsPage
    func conflicts(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<SyncConflict>
    func devices() async throws -> Page<AppDeviceInfo>
    func revokeDevice(id: String) async throws
    func requestAccountDeletion(note: String?) async throws -> DeletionResponse
    func platformTenants(query: String?, attentionOnly: Bool, cursor: String?) async throws -> Page<PlatformTenant>
    func platformAccess(status: String, query: String?, cursor: String?) async throws -> Page<AppAccessItem>
    func grantAccess(_ request: AccessGrantRequest) async throws -> AppAccessItem
    func revokeAccess(id: String, reason: String) async throws -> AppAccessItem
    func accessOptions(tenantID: String?) async throws -> AccessOptions
    func platformDevices(status: String, query: String?, cursor: String?) async throws -> Page<PlatformDevice>
    func revokePlatformDevice(id: String, reason: String) async throws -> PlatformDevice
}

private func paging(_ cursor: String?, limit: Int = 50) -> [URLQueryItem] {
    [URLQueryItem(name: "limit", value: String(limit)), URLQueryItem(name: "cursor", value: cursor)]
}

/// Implementação real, sobre o `SessionManager`.
final class LiveGestaoAPI: GestaoAPI {
    let session: SessionManager

    init(session: SessionManager) { self.session = session }

    private func get<T: Decodable & Sendable>(_ path: String, _ query: [URLQueryItem] = [], tenant: String? = nil) async throws -> T {
        try await session.send(.get(path, query: query, tenantID: tenant))
    }

    func me() async throws -> Me { try await get("/api/v1/app/me") }

    func tenants(query: String?, cursor: String?) async throws -> Page<TenantAccess> {
        try await get("/api/v1/app/tenants", [URLQueryItem(name: "q", value: query)] + paging(cursor))
    }

    func dashboard(_ c: QueryContext) async throws -> Dashboard {
        try await get("/api/v1/app/dashboard", [c.periodItem, c.shopItem], tenant: c.tenantID)
    }

    func sales(_ c: QueryContext, status: String, cursor: String?) async throws -> Page<SaleListItem> {
        try await get("/api/v1/app/sales",
                      [c.periodItem, c.shopItem, URLQueryItem(name: "status", value: status)] + paging(cursor),
                      tenant: c.tenantID)
    }

    func sale(id: String, tenantID: String) async throws -> SaleDetail {
        try await get("/api/v1/app/sales/\(id)", tenant: tenantID)
    }

    func cashSessions(_ c: QueryContext, status: String, cursor: String?) async throws -> Page<CashSession> {
        var query = [c.shopItem, URLQueryItem(name: "status", value: status)] + paging(cursor)
        if status == "closed" { query.append(c.periodItem) }
        return try await get("/api/v1/app/cash-sessions", query, tenant: c.tenantID)
    }

    func cashSession(id: String, tenantID: String) async throws -> CashSessionDetail {
        try await get("/api/v1/app/cash-sessions/\(id)", tenant: tenantID)
    }

    func stockAlerts(_ c: QueryContext, status: String, cursor: String?) async throws -> StockAlertsPage {
        try await get("/api/v1/app/stock/alerts",
                      [c.shopItem, URLQueryItem(name: "status", value: status)] + paging(cursor), tenant: c.tenantID)
    }

    func credit(_ c: QueryContext, status: String, cursor: String?) async throws -> CreditPage {
        try await get("/api/v1/app/credit",
                      [c.shopItem, URLQueryItem(name: "status", value: status)] + paging(cursor), tenant: c.tenantID)
    }

    func terminals(_ c: QueryContext) async throws -> TerminalsPage {
        try await get("/api/v1/app/terminals", [c.shopItem], tenant: c.tenantID)
    }

    func conflicts(_ c: QueryContext, status: String, cursor: String?) async throws -> Page<SyncConflict> {
        try await get("/api/v1/app/conflicts",
                      [c.shopItem, URLQueryItem(name: "status", value: status)] + paging(cursor), tenant: c.tenantID)
    }

    func devices() async throws -> Page<AppDeviceInfo> { try await get("/api/v1/app/devices") }

    func revokeDevice(id: String) async throws {
        let _: OkResponse = try await session.send(Endpoint(method: "DELETE", path: "/api/v1/app/devices/\(id)"))
    }

    func requestAccountDeletion(note: String?) async throws -> DeletionResponse {
        try await session.send(try Endpoint.send("POST", "/api/v1/app/account/deletion-request",
                                                 body: DeletionRequest(note: note)))
    }

    func platformTenants(query: String?, attentionOnly: Bool, cursor: String?) async throws -> Page<PlatformTenant> {
        try await get("/api/v1/app/platform/tenants",
                      [URLQueryItem(name: "q", value: query),
                       URLQueryItem(name: "health", value: attentionOnly ? "attention" : "all")] + paging(cursor))
    }

    func platformAccess(status: String, query: String?, cursor: String?) async throws -> Page<AppAccessItem> {
        try await get("/api/v1/app/platform/access",
                      [URLQueryItem(name: "status", value: status), URLQueryItem(name: "q", value: query)] + paging(cursor))
    }

    func grantAccess(_ request: AccessGrantRequest) async throws -> AppAccessItem {
        try await session.send(try Endpoint.send("POST", "/api/v1/app/platform/access", body: request))
    }

    func revokeAccess(id: String, reason: String) async throws -> AppAccessItem {
        try await session.send(try Endpoint.send("POST", "/api/v1/app/platform/access/\(id)/revoke",
                                                 body: ReasonRequest(reason: reason)))
    }

    func accessOptions(tenantID: String?) async throws -> AccessOptions {
        try await get("/api/v1/app/platform/access/options", [URLQueryItem(name: "tenant_id", value: tenantID)])
    }

    func platformDevices(status: String, query: String?, cursor: String?) async throws -> Page<PlatformDevice> {
        try await get("/api/v1/app/platform/devices",
                      [URLQueryItem(name: "status", value: status), URLQueryItem(name: "q", value: query)] + paging(cursor))
    }

    func revokePlatformDevice(id: String, reason: String) async throws -> PlatformDevice {
        try await session.send(try Endpoint.send("POST", "/api/v1/app/platform/devices/\(id)/revoke",
                                                 body: ReasonRequest(reason: reason)))
    }
}

/// Dados fictícios (fixtures do contrato) para os Previews do SwiftUI.
final class PreviewGestaoAPI: GestaoAPI {
    var failWith: APIError?

    init(failWith: APIError? = nil) { self.failWith = failWith }

    private func load<T: Decodable>(_ json: String) throws -> T {
        if let failWith { throw failWith }
        return try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    func me() async throws -> Me { try load(PreviewFixtures.me) }
    func tenants(query: String?, cursor: String?) async throws -> Page<TenantAccess> { try load(PreviewFixtures.tenants) }
    func dashboard(_ context: QueryContext) async throws -> Dashboard { try load(PreviewFixtures.dashboard) }
    func sales(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<SaleListItem> { try load(PreviewFixtures.sales) }
    func sale(id: String, tenantID: String) async throws -> SaleDetail { try load(PreviewFixtures.saleDetailRefunded) }
    func cashSessions(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<CashSession> {
        try load(status == "closed" ? PreviewFixtures.cashSessionsClosed : PreviewFixtures.cashSessions)
    }
    func cashSession(id: String, tenantID: String) async throws -> CashSessionDetail { try load(PreviewFixtures.cashSessionDetail) }
    func stockAlerts(_ context: QueryContext, status: String, cursor: String?) async throws -> StockAlertsPage { try load(PreviewFixtures.stockAlerts) }
    func credit(_ context: QueryContext, status: String, cursor: String?) async throws -> CreditPage { try load(PreviewFixtures.credit) }
    func terminals(_ context: QueryContext) async throws -> TerminalsPage { try load(PreviewFixtures.terminals) }
    func conflicts(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<SyncConflict> { try load(PreviewFixtures.conflicts) }
    func devices() async throws -> Page<AppDeviceInfo> { try load(PreviewFixtures.devices) }
    func revokeDevice(id: String) async throws { if let failWith { throw failWith } }
    func requestAccountDeletion(note: String?) async throws -> DeletionResponse { try load(PreviewFixtures.deletionRequest) }
    func platformTenants(query: String?, attentionOnly: Bool, cursor: String?) async throws -> Page<PlatformTenant> {
        let page: Page<PlatformTenant> = try load(PreviewFixtures.platformTenants)
        return Page(results: page.results, nextCursor: nil)
    }
    func platformAccess(status: String, query: String?, cursor: String?) async throws -> Page<AppAccessItem> { try load(PreviewFixtures.platformAccess) }
    func grantAccess(_ request: AccessGrantRequest) async throws -> AppAccessItem { try load(PreviewFixtures.access) }
    func revokeAccess(id: String, reason: String) async throws -> AppAccessItem { try load(PreviewFixtures.access) }
    func accessOptions(tenantID: String?) async throws -> AccessOptions {
        try load(tenantID == nil ? PreviewFixtures.accessOptionsPlatform : PreviewFixtures.accessOptions)
    }
    func platformDevices(status: String, query: String?, cursor: String?) async throws -> Page<PlatformDevice> { try load(PreviewFixtures.platformDevices) }
    func revokePlatformDevice(id: String, reason: String) async throws -> PlatformDevice { try load(PreviewFixtures.platformDevice) }
}
