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
                            for sale in page.results where status == "completed" { XCTAssertEqual(sale.status, status) }
                            for sale in page.results where status == "cancelled" { XCTAssertNotEqual(sale.status, "completed") }
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

    /// Relógio fixo no fuso das lojas (America/Sao_Paulo).
    private func clock(_ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = Format.timeZone
        return calendar.date(from: DateComponents(year: 2026, month: 11, day: day, hour: hour, minute: minute))!
    }

    private func allSales(_ api: GestaoAPI, _ context: QueryContext, status: String) async throws -> [SaleListItem] {
        var result: [SaleListItem] = []
        var cursor: String?
        repeat {
            let page = try await api.sales(context, status: status, cursor: cursor)
            result += page.results
            cursor = page.nextCursor
        } while cursor != nil
        return result
    }

    func testEnterDemoOpensMercadinhoWithoutNetworkOrStoredState() async throws {
        let state = makeState()
        await state.start()
        XCTAssertEqual(state.phase, .login)

        let fixed = clock(16, 13)  // segunda, 13h: "Hoje" já tem vendas a qualquer hora em que o teste rode
        state.enterDemo(api: DemoGestaoAPI(now: { fixed }))

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

    /// A lista de Vendas é a mesma do Resumo em todos os períodos: concluídas = "vendas",
    /// "Canceladas" (canceladas e devolvidas, como no servidor) = "cancelamentos".
    func testSalesListMatchesSummaryInEveryPeriod() async throws {
        let api = DemoGestaoAPI(now: { self.clock(13, 18, 30) })  // sexta, 18h30
        let shopIDs: [String?] = [nil] + DemoGestaoAPI.shops.filter { $0.tenantID == DemoGestaoAPI.mainTenantID }.map(\.id)
        for shopID in shopIDs {
            for period in Period.allCases {
                let context = QueryContext(tenantID: DemoGestaoAPI.mainTenantID, shopID: shopID, period: period)
                let dashboard = try await api.dashboard(context)
                let summary = try XCTUnwrap(dashboard.sales)
                let completed = try await allSales(api, context, status: "completed")
                let cancelled = try await allSales(api, context, status: "cancelled")
                let all = try await allSales(api, context, status: "all")
                let label = "\(shopID ?? "todas") \(period)"
                XCTAssertEqual(completed.count, summary.count, label)
                XCTAssertEqual(cancelled.count, summary.cancelledCount, label)
                XCTAssertEqual(all.count, completed.count + cancelled.count, label)
                XCTAssertEqual(Set(all.map(\.id)).count, all.count, "Sem venda repetida: \(label)")
                XCTAssertEqual(cancelled.reduce(0) { $0 + $1.totalCents }, summary.cancelledCents, label)
                if period == .sevenDays || period == .month {
                    XCTAssertGreaterThan(cancelled.count, 0, "Canceladas não fica vazia: \(label)")
                    XCTAssertGreaterThan(summary.count, 300, label)
                }
                // A lista vai até o primeiro dia do período (sem corte).
                let firstDay = try XCTUnwrap(all.last.flatMap { Format.parse($0.createdAt) })
                XCTAssertEqual(Format.ymd(firstDay), dashboard.period.dateFrom, label)
            }
        }
    }

    /// Os caixas de um dia somam as mesmas vendas e o mesmo faturamento do Resumo.
    func testClosedCashSessionsAddUpToTheSummary() async throws {
        let api = DemoGestaoAPI(now: { self.clock(13, 18, 30) })
        let context = QueryContext(tenantID: DemoGestaoAPI.mainTenantID, shopID: nil, period: .yesterday)
        let dashboard = try await api.dashboard(context)
        let summary = try XCTUnwrap(dashboard.sales)
        var sessions: [CashSession] = []
        var cursor: String?
        repeat {
            let page = try await api.cashSessions(context, status: "closed", cursor: cursor)
            sessions += page.results
            cursor = page.nextCursor
        } while cursor != nil
        XCTAssertEqual(sessions.reduce(0) { $0 + $1.salesCount }, summary.count)
        XCTAssertEqual(sessions.reduce(0) { $0 + $1.salesTotalCents }, summary.revenueCents)
    }

    /// Nada acontece depois do relógio, a qualquer hora do dia.
    func testNothingHappensAfterTheClock() async throws {
        for (hour, minute) in [(0, 30), (6, 50), (7, 5), (9, 10), (10, 0), (12, 59), (13, 30), (18, 30), (23, 40)] {
            let now = clock(16, hour, minute)
            let api = DemoGestaoAPI(now: { now })
            let label = String(format: "%02d:%02d", hour, minute)
            func notAfter(_ text: String?, _ what: String) {
                guard let text else { return }
                guard let date = Format.parse(text) else { return XCTFail("Data inválida \(text)") }
                XCTAssertLessThanOrEqual(date, now, "\(what) no futuro às \(label)")
            }
            XCTAssertEqual(api.initialPeriod, hour < 9 ? .yesterday : .today, label)
            for shopID in [nil] + DemoGestaoAPI.shops.filter { $0.tenantID == DemoGestaoAPI.mainTenantID }.map(\.id) as [String?] {
                let context = QueryContext(tenantID: DemoGestaoAPI.mainTenantID, shopID: shopID, period: .today)
                let dashboard = try await api.dashboard(context)
                for point in dashboard.series?.points ?? [] where Int(point.key) ?? 0 > hour {
                    XCTAssertEqual(point.count, 0, "Vendas às \(point.label) com relógio às \(label)")
                    XCTAssertEqual(point.revenueCents, 0)
                }
                let sales = try await allSales(api, context, status: "all")
                for sale in sales {
                    notAfter(sale.createdAt, "Venda")
                    let detail = try await api.sale(id: sale.id, tenantID: context.tenantID)
                    notAfter(detail.receivedAt, "Recebimento")
                    notAfter(detail.cancelledAt, "Cancelamento")
                    detail.returns.forEach { notAfter($0.createdAt, "Devolução") }
                }
                let open = try await api.cashSessions(context, status: "open", cursor: nil).results
                let closed = try await api.cashSessions(context, status: "closed", cursor: nil).results
                for session in open + closed {
                    notAfter(session.openedAt, "Abertura de caixa")
                    notAfter(session.closedAt, "Fechamento de caixa")
                    let detail = try await api.cashSession(id: session.id, tenantID: context.tenantID)
                    detail.movements.forEach { notAfter($0.createdAt, "Sangria") }
                }
                let terminals = try await api.terminals(context).results
                terminals.forEach { notAfter($0.lastSeenAt, "Terminal visto") }
                // Terminal online = caixa aberto agora; o offline do Centro aparece entre os fechados.
                XCTAssertEqual(terminals.filter { $0.status == "online" }.count, open.count, label)
                XCTAssertEqual(dashboard.cash?.openCount, open.count, label)
                let conflicts = try await api.conflicts(context, status: "all", cursor: nil).results
                for conflict in conflicts {
                    notAfter(conflict.createdAt, "Conflito")
                }
            }
        }
    }

    func testDemoOpensOnYesterdayBeforeNine() async throws {
        let state = makeState()
        let early = clock(16, 6, 40)
        state.enterDemo(api: DemoGestaoAPI(now: { early }))
        XCTAssertEqual(state.period, .yesterday)
        XCTAssertEqual(savedKeys, [], "Período escolhido pela demonstração não é gravado")
    }

    /// Os dados fictícios usam domínio reservado, que não aponta para empresa real.
    func testDemoAccountUsesReservedDomain() throws {
        let me = try DemoGestaoAPI().demoMe()
        XCTAssertTrue(me.user.email.hasSuffix("@example.com"), me.user.email)
    }
}
