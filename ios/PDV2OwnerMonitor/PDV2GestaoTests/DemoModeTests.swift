import XCTest
@testable import PDV2Gestao

/// Guarda de sessão que conta toda tentativa de gravar ou apagar.
private final class SpySessionStore: SessionStore, @unchecked Sendable {
    private let lock = NSLock()
    private var stored: StoredSession?
    private(set) var saves = 0
    private(set) var clears = 0
    private(set) var loads = 0

    init(_ initial: StoredSession? = nil) { stored = initial }

    func load() -> StoredSession? { lock.withLock { loads += 1; return stored } }
    func save(_ session: StoredSession) throws { lock.withLock { saves += 1; stored = session } }
    func clear() { lock.withLock { clears += 1; stored = nil } }
}

/// Modo demonstração ("Ver demonstração"): sem rede, sem token, sem estado no aparelho.
/// A faixa "Demonstração · dados fictícios" é conferida em PDV2GestaoUITests/DemoModeUITests.
@MainActor
final class DemoModeTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suiteName = ""
    private var transport: MockTransport!
    private var store: SpySessionStore!

    private let config = AppConfig(defaultBaseURL: "https://pdv2.crowntech.com.br", allowServerOverride: false,
                                   allowsInsecureHTTP: false, marketingVersion: "1.0.0", buildNumber: "1")

    override func setUp() async throws {
        suiteName = "pdv2.tests.demo.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        transport = MockTransport { request in
            XCTFail("A demonstração não pode chamar a rede: \(request.url?.absoluteString ?? "")")
            return .init(status: 500, body: Data())
        }
        store = SpySessionStore()
    }

    override func tearDown() async throws {
        defaults.removePersistentDomain(forName: suiteName)
    }

    private func makeState() -> AppState {
        AppState(config: config, biometrics: FakeBiometrics(), defaults: defaults, transport: transport, store: store)
    }

    private var savedKeys: [String] {
        defaults.persistentDomain(forName: suiteName)?.keys.sorted() ?? []
    }

    /// Passa por todas as leituras que as telas fazem, em todos os filtros.
    private func exerciseEveryScreen(_ state: AppState) async throws {
        let api = state.api
        for tenant in state.me?.tenants ?? [] {
            state.selectTenant(tenant)
            for shopID in [nil] + state.shops.map(\.id) as [String?] {
                state.shopID = shopID
                for period in Period.allCases {
                    state.period = period
                    let context = try XCTUnwrap(state.queryContext)
                    let dashboard = try await api.dashboard(context)
                    XCTAssertEqual(dashboard.tenant.id, tenant.id)
                    XCTAssertGreaterThan(dashboard.sales?.revenueCents ?? 0, 0, "\(tenant.name) \(period)")
                    let payments = try XCTUnwrap(dashboard.payments)
                    XCTAssertEqual(payments.cashCents + payments.pixCents + payments.cardCreditCents
                                   + payments.cardDebitCents + payments.otherCents, payments.receivedTotalCents)
                    let revenue = dashboard.series?.points.reduce(0) { $0 + $1.revenueCents }
                    XCTAssertEqual(revenue, dashboard.sales?.revenueCents)
                    for status in ["all", "completed", "cancelled"] {
                        var cursor: String?
                        repeat {
                            let page = try await api.sales(context, status: status, cursor: cursor)
                            for sale in page.results where status != "all" { XCTAssertEqual(sale.status, status) }
                            if let first = page.results.first {
                                let detail = try await api.sale(id: first.id, tenantID: tenant.id)
                                XCTAssertEqual(detail.totalCents, first.totalCents)
                                XCTAssertEqual(detail.items.reduce(0) { $0 + $1.lineTotalCents }, detail.totalCents)
                            }
                            cursor = page.nextCursor
                        } while cursor != nil
                    }
                    for status in ["open", "closed"] {
                        let page = try await api.cashSessions(context, status: status, cursor: nil)
                        if let first = page.results.first {
                            let detail = try await api.cashSession(id: first.id, tenantID: tenant.id)
                            XCTAssertEqual(detail.session.status, status)
                            XCTAssertEqual(detail.byMethod.reduce(0) { $0 + $1.amountCents }, detail.session.salesTotalCents)
                        }
                    }
                    for status in ["zeroed", "below_minimum", "all"] {
                        _ = try await api.stockAlerts(context, status: status, cursor: nil)
                    }
                    for status in ["open", "overdue"] { _ = try await api.credit(context, status: status, cursor: nil) }
                    _ = try await api.terminals(context)
                    for status in ["open", "all"] { _ = try await api.conflicts(context, status: status, cursor: nil) }
                }
            }
        }
        _ = try await api.me()
        _ = try await api.tenants(query: nil, cursor: nil)
        _ = try await api.devices()
        _ = try await api.requestAccountDeletion(note: nil)
    }

    func testEnterDemoOpensMercadinhoWithoutNetworkOrStoredState() async throws {
        let state = makeState()
        await state.start()
        XCTAssertEqual(state.phase, .login)

        state.enterDemo()

        XCTAssertTrue(state.isDemo)
        XCTAssertEqual(state.phase, .home)
        XCTAssertEqual(state.selectedTab, .summary)
        XCTAssertFalse(state.isPlatform)
        XCTAssertEqual(state.tenant?.name, "Mercadinho Exemplo")
        XCTAssertEqual(state.shops.map(\.name), ["Loja Centro", "Loja Jardim"])
        XCTAssertTrue(state.canSwitchTenant, "Troca de empresa precisa funcionar na demonstração")
        XCTAssertTrue(AppPermission.allCases.allSatisfy { state.can($0) })
        XCTAssertNil(state.session, "A demonstração não cria sessão")
        XCTAssertTrue(state.api is DemoGestaoAPI)
        XCTAssertFalse(state.showBiometricOffer)

        try await exerciseEveryScreen(state)
        await state.reloadMe()
        await state.setBiometricLock(true)
        state.scenePhaseChanged(.background)
        state.scenePhaseChanged(.active)

        XCTAssertTrue(transport.requests.isEmpty, "Nenhuma chamada de rede na demonstração")
        XCTAssertEqual(store.saves, 0, "Nada gravado no Keychain")
        XCTAssertEqual(store.clears, 0)
        XCTAssertFalse(state.biometricLockEnabled)
        XCTAssertFalse(state.isLocked)
        XCTAssertEqual(savedKeys, [], "Nada gravado nas preferências")
    }

    func testExitDemoReturnsToLoginWithoutLeavingState() async throws {
        let state = makeState()
        await state.start()
        state.enterDemo()
        state.selectTenant(try XCTUnwrap(state.me?.tenants.last))
        state.shopID = state.shops.first?.id
        state.period = .month
        state.goToMore(.credit)

        state.exitDemo()

        XCTAssertFalse(state.isDemo)
        XCTAssertEqual(state.phase, .login)
        XCTAssertNil(state.me)
        XCTAssertNil(state.tenant)
        XCTAssertNil(state.shopID)
        XCTAssertEqual(state.period, .today)
        XCTAssertEqual(state.morePath, [])
        XCTAssertNil(state.loginMessage)
        XCTAssertTrue(state.api is LiveGestaoAPI, "Depois da demonstração o login volta a usar o servidor")
        XCTAssertNotNil(state.session)
        XCTAssertTrue(transport.requests.isEmpty)
        XCTAssertEqual(store.saves, 0)
        XCTAssertEqual(store.clears, 0)
        XCTAssertEqual(savedKeys, [])
        let hasStored = await state.session?.hasStoredSession
        XCTAssertEqual(hasStored, false, "Nenhum token guardado")
    }

    func testExitDemoKeepsContextSavedBeforeTheDemo() async throws {
        let realTenant = try decodeFixture(Page<TenantAccess>.self, "tenants").results[0]
        defaults.set(try JSONEncoder().encode(realTenant), forKey: "pdv2.context.tenant")
        defaults.set("7d", forKey: "pdv2.context.period")
        let before = defaults.persistentDomain(forName: suiteName) as NSDictionary?

        let state = makeState()
        state.enterDemo()
        state.period = .yesterday
        state.shopID = state.shops.last?.id
        state.selectTenant(try XCTUnwrap(state.me?.tenants.last))
        state.exitDemo()

        XCTAssertEqual(state.tenant?.id, realTenant.id)
        XCTAssertEqual(state.period, .sevenDays)
        XCTAssertEqual(defaults.persistentDomain(forName: suiteName) as NSDictionary?, before)
    }

    func testSignOutAndSessionErrorsInDemoOnlyLeaveTheDemo() async {
        let state = makeState()
        state.enterDemo()
        await state.signOut()
        XCTAssertEqual(state.phase, .login)
        XCTAssertFalse(state.isDemo)

        state.enterDemo()
        await state.forceLogin(message: "Sua sessão terminou.")
        XCTAssertEqual(state.phase, .login)
        XCTAssertFalse(state.isDemo)
        XCTAssertTrue(transport.requests.isEmpty)
        XCTAssertEqual(store.clears, 0)
    }

    func testDemoDataIsStableAndInPortuguese() async throws {
        let fixed = Date(timeIntervalSince1970: 1_794_844_800)  // 2026-11-16 13:00 -03:00
        let a = DemoGestaoAPI(now: { fixed })
        let b = DemoGestaoAPI(now: { fixed })
        let context = QueryContext(tenantID: DemoGestaoAPI.mainTenantID, shopID: nil, period: .today)
        let first = try await a.dashboard(context)
        let second = try await b.dashboard(context)
        XCTAssertEqual(first.sales?.revenueCents, second.sales?.revenueCents)
        XCTAssertEqual(first.period.label, "Hoje")
        let sales = try await a.sales(context, status: "all", cursor: nil)
        XCTAssertEqual(sales.results.count, 25)
        XCTAssertNotNil(sales.nextCursor, "Lista com rolagem infinita")
        let credit = try await a.credit(context, status: "overdue", cursor: nil)
        XCTAssertTrue(credit.results.allSatisfy { $0.overdueCents > 0 })
        XCTAssertEqual(credit.totals.overdueCustomers, credit.results.count)
        let stock = try await a.stockAlerts(context, status: "zeroed", cursor: nil)
        XCTAssertEqual(first.stock?.zeroedCount, stock.counts.zeroed)
        let terminals = try await a.terminals(context)
        XCTAssertEqual(first.terminals?.total, terminals.results.count)
        let open = try await a.cashSessions(context, status: "open", cursor: nil)
        XCTAssertEqual(first.cash?.openCount, open.results.count)
    }
}
