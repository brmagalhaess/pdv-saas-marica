import UIKit
import XCTest
@testable import PDV2Gestao

/// Correções da revisão: trava acima de modais, refresh repetido dentro de 30 s,
/// abertura que não apaga a sessão por erro passageiro e totais das listas por chave.
@MainActor
final class ReviewFixesTests: XCTestCase {
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

    private func savedSession() -> InMemorySessionStore {
        InMemorySessionStore(StoredSession(refreshToken: "pr_saved", deviceID: "d1",
                                           apiBaseURL: "https://pdv2.crowntech.com.br"))
    }

    private func tokens(_ suffix: String) -> [String: Any] {
        ["access_token": "pa_access_\(suffix)_xxxxxxxxxxxxxxxx", "access_expires_at": "2026-11-16T14:18:12-03:00",
         "access_expires_in": 900, "refresh_token": "pr_refresh_\(suffix)_xxxxxxxxxxxxxxxx",
         "refresh_expires_at": "2027-01-15T14:03:12-03:00"]
    }

    private func refreshBody(_ request: URLRequest) -> String? {
        let object = try? JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: Any]
        return object?["refresh_token"] as? String
    }

    // MARK: Refresh com resposta perdida

    private func manager(_ transport: MockTransport, store: SessionStore) -> SessionManager {
        SessionManager(baseURL: testBaseURL, transport: transport, store: store, device: testDevice,
                       userAgent: "PDV2Gestao/1.0.0 (iOS 26.1; iPhone16,2)", refreshRetryDelays: [0, 0])
    }

    func testRefreshLostResponseIsRetriedWithSameToken() async throws {
        let attempts = Counter()
        let transport = MockTransport { request in
            switch request.url?.path {
            case "/api/v1/app/auth/refresh":
                if attempts.next() == 0 { throw APIError.offline }
                return .init(status: 200, body: json(["tokens": self.tokens("2")]))
            case "/api/v1/app/me":
                return .init(status: 200, body: try fixture("me"))
            default:
                return .init(status: 404, body: json(["error": "Registro não encontrado.", "code": "not_found"]))
            }
        }
        let store = savedSession()
        let me = try await manager(transport, store: store).restore()
        XCTAssertEqual(me.user.id, 12)
        let refreshes = transport.requests(to: "/api/v1/app/auth/refresh")
        XCTAssertEqual(refreshes.count, 2, "Repete logo, dentro da tolerância de 30 s")
        XCTAssertEqual(refreshes.map(refreshBody), ["pr_saved", "pr_saved"])
        XCTAssertEqual(store.load()?.refreshToken, "pr_refresh_2_xxxxxxxxxxxxxxxx")
    }

    func testRefreshRetriesServerErrorThenGivesUpKeepingSession() async throws {
        let transport = MockTransport { _ in .init(status: 502, body: Data("<html>".utf8)) }
        let store = savedSession()
        do {
            _ = try await manager(transport, store: store).restore()
            XCTFail("Deveria falhar")
        } catch {
            XCTAssertEqual(error as? APIError, .unavailable)
        }
        XCTAssertEqual(transport.requests(to: "/api/v1/app/auth/refresh").count, 3, "1 tentativa + 2 repetições")
        XCTAssertNotNil(store.load())
    }

    func testRefreshDoesNotRetryClientErrors() async throws {
        let transport = MockTransport { _ in
            .init(status: 429, body: json(["error": "Muitas tentativas. Aguarde alguns minutos e tente de novo.",
                                           "code": "rate_limited", "retry_after_seconds": 120]))
        }
        do {
            _ = try await manager(transport, store: savedSession()).restore()
            XCTFail("Deveria falhar")
        } catch {
            XCTAssertEqual((error as? APIError)?.code, "rate_limited")
        }
        XCTAssertEqual(transport.requests(to: "/api/v1/app/auth/refresh").count, 1)
    }

    // MARK: Abertura a frio

    private func restoreState(status: Int, body: Data, store: InMemorySessionStore) async -> AppState {
        let transport = MockTransport { _ in .init(status: status, body: body) }
        let state = AppState(config: config, biometrics: FakeBiometrics(biometryName: nil), defaults: defaults,
                             transport: transport, store: store)
        await state.start()
        return state
    }

    func testRateLimitedOnOpenKeepsSessionAndShowsMessage() async {
        let store = savedSession()
        let state = await restoreState(status: 429, body: json(["error": "Muitas tentativas. Aguarde alguns minutos e tente de novo.",
                                                                 "code": "rate_limited", "retry_after_seconds": 60]),
                                       store: store)
        XCTAssertEqual(state.phase, .login)
        XCTAssertNotNil(store.load(), "429 não apaga a sessão guardada")
        XCTAssertTrue(state.canRetryRestore)
        XCTAssertEqual(state.loginMessage, "Muitas tentativas. Aguarde alguns minutos e tente de novo.")
    }

    func testConflictOnOpenKeepsSession() async {
        let store = savedSession()
        let state = await restoreState(status: 409, body: json(["error": "Tente de novo.", "code": "conflict"]),
                                       store: store)
        XCTAssertNotNil(store.load())
        XCTAssertTrue(state.canRetryRestore)
        XCTAssertEqual(state.loginMessage, "Tente de novo.")
    }

    func testInvalidRefreshOnOpenClearsSession() async {
        let store = savedSession()
        let state = await restoreState(status: 401, body: json(["error": "Sua sessão terminou. Entre de novo.",
                                                                 "code": "refresh_invalid"]),
                                       store: store)
        XCTAssertEqual(state.phase, .login)
        XCTAssertNil(store.load())
        XCTAssertFalse(state.canRetryRestore)
        XCTAssertEqual(state.loginMessage, APIErrorCatalog.sessionEndedMessage)
    }

    func testRevokedDeviceOnOpenClearsSession() async {
        let store = savedSession()
        let state = await restoreState(status: 401, body: json(["error": "x", "code": "device_revoked"]), store: store)
        XCTAssertNil(store.load())
        XCTAssertEqual(state.loginMessage, APIErrorCatalog.deviceRevokedMessage)
    }

    // MARK: Totais das listas

    private struct Row: Identifiable { let id: String }

    func testSummaryIsClearedWhenKeyChangesAndNewFetchFails() async {
        let app = AppState.preview()
        let state = PagedState<Row>()
        let totalsA = CreditTotals(openCents: 120000, overdueCents: 30000, customers: 12, overdueCustomers: 4)
        await state.ensureLoaded(key: "loja-centro", app: app) { _ in
            Page(results: [Row(id: "1")], nextCursor: nil, summary: totalsA)
        }
        XCTAssertEqual(state.summary(as: CreditTotals.self), totalsA)

        await state.ensureLoaded(key: "loja-bairro", app: app) { _ in throw APIError.offline }
        XCTAssertNil(state.summary(as: CreditTotals.self), "Sem internet não pode mostrar os totais da outra loja")
        XCTAssertEqual(state.error?.kind, .offline)
        XCTAssertTrue(state.items.isEmpty)
    }

    func testLateResponseDoesNotOverwriteSummary() async throws {
        let app = AppState.preview()
        let state = PagedState<Row>()
        let countsA = StockCounts(zeroed: 3, belowMinimum: 7)
        let countsB = StockCounts(zeroed: 1, belowMinimum: 0)

        let slow = Task { @MainActor in
            await state.ensureLoaded(key: "empresa-a", app: app) { _ in
                try await Task.sleep(nanoseconds: 300_000_000)
                return Page(results: [Row(id: "a")], nextCursor: nil, summary: countsA)
            }
        }
        try await Task.sleep(nanoseconds: 50_000_000)
        await state.ensureLoaded(key: "empresa-b", app: app) { _ in
            Page(results: [Row(id: "b")], nextCursor: nil, summary: countsB)
        }
        await slow.value
        XCTAssertEqual(state.summary(as: StockCounts.self), countsB)
        XCTAssertEqual(state.items.map(\.id), ["b"])
    }

    // MARK: Trava acima de modais

    func testLockOverlayWindowSitsAboveSheetsAndAlerts() async throws {
        // O app hospedeiro pode ainda estar conectando a cena quando os testes começam.
        var found: UIWindowScene?
        for _ in 0..<50 {
            found = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
            if found != nil { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard let scene = found else {
            throw XCTSkip("Sem cena de janela no app hospedeiro (\(UIApplication.shared.connectedScenes.count) cenas)")
        }
        defaults.set(true, forKey: "pdv2.biometricLock")
        let state = AppState(config: config, biometrics: FakeBiometrics(result: false), defaults: defaults,
                             transport: MockTransport { _ in throw APIError.offline }, store: savedSession())
        await state.start()
        XCTAssertTrue(state.isLocked, "Face ID cancelado mantém a trava")

        let overlay = LockOverlay()
        overlay.update(locked: state.isLocked, app: state, scene: scene)
        let window = try XCTUnwrap(overlay.window)
        XCTAssertTrue(overlay.isShowing)
        XCTAssertGreaterThan(window.windowLevel, UIWindow.Level.alert,
                             "Sheets, alertas e diálogos ficam na janela normal, abaixo da trava")
        XCTAssertTrue(window.isKeyWindow)
        for other in scene.windows where other !== window && !other.isHidden {
            XCTAssertLessThan(other.windowLevel, window.windowLevel)
        }

        overlay.update(locked: false, app: state, scene: scene)
        XCTAssertFalse(overlay.isShowing)
        XCTAssertNil(overlay.window)
    }
}
