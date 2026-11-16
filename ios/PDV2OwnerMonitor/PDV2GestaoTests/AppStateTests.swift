import XCTest
@testable import PDV2Gestao

/// Fluxo de entrada e reação aos códigos de erro.
@MainActor
final class AppStateTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suiteName = ""

    override func setUp() async throws {
        suiteName = "pdv2.tests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDown() async throws {
        defaults.removePersistentDomain(forName: suiteName)
    }

    private let config = AppConfig(defaultBaseURL: "https://pdv2.crowntech.com.br", allowServerOverride: false,
                                   allowsInsecureHTTP: false, marketingVersion: "1.0.0", buildNumber: "1")

    private func makeState(_ transport: MockTransport, store: SessionStore = InMemorySessionStore()) -> AppState {
        AppState(config: config, biometrics: FakeBiometrics(biometryName: nil), defaults: defaults,
                 transport: transport, store: store)
    }

    func testTenantLoginGoesHomeWithFirstCompany() async throws {
        let transport = MockTransport { _ in .init(status: 200, body: try fixture("login_ok")) }
        let state = makeState(transport)
        await state.start()
        XCTAssertEqual(state.phase, .login)
        await state.signIn(email: " ana@mercadobompreco.com.br ", password: "x")
        XCTAssertEqual(state.phase, .home)
        XCTAssertFalse(state.isPlatform)
        XCTAssertEqual(state.tenant?.name, "Mercado Bom Preço")
        XCTAssertEqual(state.selectedTab, .summary)
        XCTAssertTrue(state.can(.cash))
        XCTAssertEqual(state.queryContext?.period, .today)
        let body = try JSONSerialization.jsonObject(with: transport.requests.first?.httpBody ?? Data()) as? [String: Any]
        XCTAssertEqual(body?["email"] as? String, "ana@mercadobompreco.com.br")
    }

    func testAccessDeniedLeavesNoSession() async {
        let store = InMemorySessionStore()
        let transport = MockTransport { _ in
            .init(status: 403, body: json(["error": "Acesso ao app não liberado.", "code": "app_access_denied"]))
        }
        let state = makeState(transport, store: store)
        await state.signIn(email: "a@b.c", password: "x")
        XCTAssertEqual(state.phase, .accessDenied)
        XCTAssertNil(store.load())
        XCTAssertNil(state.me)
    }

    func testWrongPasswordStaysOnLogin() async {
        let transport = MockTransport { _ in
            .init(status: 401, body: json(["error": "E-mail ou senha incorretos.", "code": "invalid_credentials"]))
        }
        let state = makeState(transport)
        await state.signIn(email: "a@b.c", password: "x")
        XCTAssertEqual(state.phase, .login)
        XCTAssertEqual(state.loginMessage, "E-mail ou senha incorretos.")
    }

    func testPlatformLoginAsksForCodeThenOpensPlatformTab() async throws {
        let transport = MockTransport { request in
            request.url?.path == "/api/v1/app/auth/login"
                ? .init(status: 200, body: try fixture("login_mfa"))
                : .init(status: 200, body: try fixture("mfa_ok"))
        }
        let state = makeState(transport)
        await state.signIn(email: "admin@crowntech.com.br", password: "x")
        guard case .mfa = state.phase else { return XCTFail("Esperava a tela do código") }
        await state.verifyMFA(code: "123456", recoveryCode: nil)
        XCTAssertEqual(state.phase, .home)
        XCTAssertTrue(state.isPlatform)
        XCTAssertTrue(state.canManageAccess)
        XCTAssertNil(state.tenant)
        XCTAssertEqual(state.selectedTab, .platform)
        XCTAssertTrue(state.can(.stock))
        state.openPlatformTenant(id: "t-9", name: "Padaria Sol")
        XCTAssertEqual(state.tenant?.id, "t-9")
        XCTAssertEqual(state.selectedTab, .summary)
    }

    func testExpiredMFAChallengeReturnsToLogin() async throws {
        let transport = MockTransport { request in
            request.url?.path == "/api/v1/app/auth/login"
                ? .init(status: 200, body: try fixture("login_mfa"))
                : .init(status: 401, body: json(["error": "O tempo para digitar o código acabou. Entre de novo.",
                                                 "code": "mfa_expired"]))
        }
        let state = makeState(transport)
        await state.signIn(email: "admin@crowntech.com.br", password: "x")
        await state.verifyMFA(code: "123456", recoveryCode: nil)
        XCTAssertEqual(state.phase, .login)
        XCTAssertEqual(state.loginMessage, "O tempo para digitar o código acabou. Entre de novo.")
    }

    func testErrorActionsAdjustContext() async throws {
        let transport = MockTransport { _ in .init(status: 200, body: try fixture("login_ok")) }
        let state = makeState(transport)
        await state.signIn(email: "a@b.c", password: "x")

        state.shopID = "a1b2c3d4-0001-4000-8000-000000000001"
        let shop = state.handle(APIError.server(status: 403, code: "shop_forbidden", message: "", retryAfter: nil))
        XCTAssertNil(state.shopID)
        XCTAssertEqual(shop?.message, "Você não tem acesso a esta loja.")

        state.period = .month
        _ = state.handle(APIError.server(status: 400, code: "invalid_period", message: "", retryAfter: nil))
        XCTAssertEqual(state.period, .today)

        let permission = state.handle(APIError.server(status: 403, code: "permission_denied", message: "", retryAfter: nil))
        XCTAssertEqual(permission?.kind, .permission)

        XCTAssertEqual(state.handle(APIError.offline)?.kind, .offline)
        XCTAssertNil(state.handle(CancellationError()))

        let unavailable = state.handle(APIError.unavailable)
        XCTAssertEqual(unavailable?.message, "O servidor não respondeu como esperado. Tente de novo em instantes.")
    }

    func testRevokedDeviceForcesLogin() async throws {
        let store = InMemorySessionStore()
        let transport = MockTransport { _ in .init(status: 200, body: try fixture("login_ok")) }
        let state = makeState(transport, store: store)
        await state.signIn(email: "a@b.c", password: "x")
        XCTAssertNotNil(store.load())
        await state.forceLogin(message: APIErrorCatalog.deviceRevokedMessage)
        XCTAssertEqual(state.phase, .login)
        XCTAssertEqual(state.loginMessage, "Este aparelho foi desconectado da sua conta. Entre de novo.")
        XCTAssertNil(store.load())
        XCTAssertNil(state.tenant)
    }

    func testSelectingAnotherTenantResetsShop() async throws {
        let transport = MockTransport { _ in .init(status: 200, body: try fixture("login_ok")) }
        let state = makeState(transport)
        await state.signIn(email: "a@b.c", password: "x")
        state.shopID = "a1b2c3d4-0001-4000-8000-000000000001"
        state.selectTenant(.platformView(id: "other", name: "Outra"))
        XCTAssertNil(state.shopID)
        XCTAssertEqual(state.tenant?.name, "Outra")
    }
}
