import XCTest
@testable import PDV2Gestao

final class ErrorMappingTests: XCTestCase {
    func testCodeToAction() {
        XCTAssertEqual(APIErrorCatalog.action(for: "invalid_request"), .showMessage)
        XCTAssertEqual(APIErrorCatalog.action(for: "tenant_required"), .openTenantPicker)
        XCTAssertEqual(APIErrorCatalog.action(for: "invalid_period"), .resetPeriod)
        XCTAssertEqual(APIErrorCatalog.action(for: "invalid_credentials"), .showMessage)
        XCTAssertEqual(APIErrorCatalog.action(for: "unauthenticated"), .signOut(message: "Sua sessão terminou. Entre de novo."))
        XCTAssertEqual(APIErrorCatalog.action(for: "token_expired"), .refreshAndRetry)
        XCTAssertEqual(APIErrorCatalog.action(for: "refresh_invalid"), .signOut(message: "Sua sessão terminou. Entre de novo."))
        XCTAssertEqual(APIErrorCatalog.action(for: "device_revoked"),
                       .signOut(message: "Este aparelho foi desconectado da sua conta. Entre de novo."))
        XCTAssertEqual(APIErrorCatalog.action(for: "mfa_invalid"), .showMessage)
        XCTAssertEqual(APIErrorCatalog.action(for: "mfa_expired"),
                       .backToLogin(message: "O tempo para digitar o código acabou. Entre de novo."))
        XCTAssertEqual(APIErrorCatalog.action(for: "app_access_denied"), .accessDenied)
        XCTAssertEqual(APIErrorCatalog.action(for: "mfa_setup_required"), .showMessage)
        XCTAssertEqual(APIErrorCatalog.action(for: "tenant_forbidden"), .reloadMeAndPickTenant)
        XCTAssertEqual(APIErrorCatalog.action(for: "shop_forbidden"), .resetShop)
        XCTAssertEqual(APIErrorCatalog.action(for: "permission_denied"), .permissionDenied)
        for code in ["platform_forbidden", "not_found", "method_not_allowed", "access_exists", "member_required",
                     "superuser_implicit", "conflict", "rate_limited", "mfa_locked", "codigo_novo"] {
            XCTAssertEqual(APIErrorCatalog.action(for: code), .showMessage, code)
        }
    }

    func testParsesErrorBodies() throws {
        let rate = APIErrorCatalog.error(status: 429, data: try fixture("error_rate_limited"))
        XCTAssertEqual(rate, .server(status: 429, code: "rate_limited",
                                     message: "Muitas tentativas. Aguarde alguns minutos e tente de novo.", retryAfter: 600))
        XCTAssertEqual(rate.userMessage, "Muitas tentativas. Aguarde alguns minutos e tente de novo.")

        // Sem `code`: usa o padrão pelo status.
        XCTAssertEqual(APIErrorCatalog.error(status: 403, data: json(["error": "Proibido"])).code, "permission_denied")
        XCTAssertEqual(APIErrorCatalog.error(status: 404, data: json([String: String]())).userMessage, "Registro não encontrado.")
        XCTAssertEqual(APIErrorCatalog.error(status: 409, data: json(["error": "x"])).code, "conflict")
    }

    func testServerErrorsAndNonJSONNeverShowBody() {
        let crash = APIErrorCatalog.error(status: 500, data: json(["error": "Traceback secreto", "code": "unavailable"]))
        XCTAssertEqual(crash, .unavailable)
        XCTAssertEqual(crash.userMessage, "O servidor não respondeu como esperado. Tente de novo em instantes.")
        XCTAssertEqual(APIErrorCatalog.error(status: 401, data: Data("<html>".utf8)), .unavailable)
        XCTAssertEqual(APIError.offline.userMessage, "Sem internet ou servidor fora do ar. Confira a conexão e tente de novo.")
    }

    func testDecodeTreatsInvalidSuccessBodyAsUnavailable() {
        let response = HTTPURLResponse(url: testBaseURL, statusCode: 200, httpVersion: nil, headerFields: nil)!
        XCTAssertThrowsError(try APIRequestBuilder.decode(Me.self, data: Data("ok".utf8), response: response)) { error in
            XCTAssertEqual(error as? APIError, .unavailable)
        }
    }

    func testRequestHeaders() throws {
        let builder = APIRequestBuilder(baseURL: testBaseURL, userAgent: "PDV2Gestao/1.0.0 (iOS 26.1; iPhone16,2)")
        let context = QueryContext(tenantID: "t-1", shopID: nil, period: .sevenDays)
        let endpoint = Endpoint.get("/api/v1/app/dashboard", query: [context.periodItem, context.shopItem], tenantID: "t-1")
        let request = try builder.request(for: endpoint, accessToken: "pa_abc")
        XCTAssertEqual(request.url?.absoluteString, "https://pdv2.crowntech.com.br/api/v1/app/dashboard?period=7d")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer pa_abc")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Tenant-ID"), "t-1")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "application/json")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Accept-Language"), "pt-BR")
        XCTAssertEqual(request.value(forHTTPHeaderField: "User-Agent"), "PDV2Gestao/1.0.0 (iOS 26.1; iPhone16,2)")
        XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
        XCTAssertFalse(request.httpShouldHandleCookies)

        let login = try Endpoint.send("POST", "/api/v1/app/auth/login",
                                      body: LoginRequest(email: "a@b.c", password: "x", device: testDevice),
                                      authenticated: false)
        let loginRequest = try builder.request(for: login, accessToken: nil)
        XCTAssertNil(loginRequest.value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(loginRequest.value(forHTTPHeaderField: "Content-Type"), "application/json")
    }
}

final class AppConfigTests: XCTestCase {
    func testReleaseRefusesHTTP() {
        XCTAssertThrowsError(try AppConfig.validatedBaseURL("http://pdv2.crowntech.com.br", allowInsecure: false)) { error in
            XCTAssertEqual(error as? APIError,
                           .invalidServerURL("O endereço do servidor precisa começar com https://."))
        }
        XCTAssertEqual(try AppConfig.validatedBaseURL("https://pdv2.crowntech.com.br/", allowInsecure: false).absoluteString,
                       "https://pdv2.crowntech.com.br")
    }

    func testDebugAcceptsLocalHTTP() throws {
        XCTAssertEqual(try AppConfig.validatedBaseURL("http://127.0.0.1:8000", allowInsecure: true).absoluteString,
                       "http://127.0.0.1:8000")
        XCTAssertThrowsError(try AppConfig.validatedBaseURL("ftp://x", allowInsecure: true))
        XCTAssertThrowsError(try AppConfig.validatedBaseURL("nada", allowInsecure: true))
    }

    func testReleaseConfigIgnoresOverride() throws {
        let release = AppConfig(defaultBaseURL: "https://pdv2.crowntech.com.br", allowServerOverride: false,
                                allowsInsecureHTTP: false, marketingVersion: "1.0.0", buildNumber: "1")
        XCTAssertEqual(try release.resolvedBaseURL(override: "http://127.0.0.1:8000").absoluteString,
                       "https://pdv2.crowntech.com.br")
        let releaseWithHTTP = AppConfig(defaultBaseURL: "http://pdv2.crowntech.com.br", allowServerOverride: false,
                                        allowsInsecureHTTP: false, marketingVersion: "1.0.0", buildNumber: "1")
        XCTAssertThrowsError(try releaseWithHTTP.resolvedBaseURL(override: nil))
    }

    func testDebugBundleConfig() {
        // Os testes rodam no build Debug: URL local e servidor trocável.
        // Testes hospedados no app: Bundle.main é o PDV2Gestao.app.
        let config = AppConfig.fromBundle(.main)
        XCTAssertEqual(config.defaultBaseURL, "http://127.0.0.1:8000")
        XCTAssertTrue(config.allowServerOverride)
        XCTAssertEqual(config.marketingVersion, "1.0.0")
        XCTAssertEqual(Bundle.main.bundleIdentifier, "br.com.crowntech.pdv2gestao")
        XCTAssertEqual(Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String, "PDV2 Gestão")
        XCTAssertNotNil(Bundle.main.object(forInfoDictionaryKey: "NSFaceIDUsageDescription"))
    }

    func testDeviceDescriptorUserAgent() {
        let descriptor = DeviceDescriptor(name: "iPhone", model: "iPhone16,2", osVersion: "iOS 26.1",
                                          appVersion: "1.0.0 (1)", marketingVersion: "1.0.0")
        XCTAssertEqual(descriptor.userAgent, "PDV2Gestao/1.0.0 (iOS 26.1; iPhone16,2)")
        XCTAssertEqual(descriptor.loginDevice, testDevice)
    }

    func testBiometricLockPolicy() {
        let now = Date()
        XCTAssertFalse(BiometricLockPolicy.shouldLock(enabled: true, backgroundedAt: now.addingTimeInterval(-60), now: now))
        XCTAssertTrue(BiometricLockPolicy.shouldLock(enabled: true, backgroundedAt: now.addingTimeInterval(-301), now: now))
        XCTAssertFalse(BiometricLockPolicy.shouldLock(enabled: false, backgroundedAt: now.addingTimeInterval(-900), now: now))
        XCTAssertFalse(BiometricLockPolicy.shouldLock(enabled: true, backgroundedAt: nil, now: now))
    }
}
