import XCTest
@testable import PDV2Gestao

/// Sessão: tokens só em memória/Keychain, renovação single-flight e saídas forçadas.
final class SessionManagerTests: XCTestCase {
    private func tokens(_ suffix: String) -> [String: Any] {
        ["access_token": "pa_access_\(suffix)_xxxxxxxxxxxxxxxx", "access_expires_at": "2026-11-16T14:18:12-03:00",
         "access_expires_in": 900, "refresh_token": "pr_refresh_\(suffix)_xxxxxxxxxxxxxxxx",
         "refresh_expires_at": "2027-01-15T14:03:12-03:00"]
    }

    private func bodyObject(_ request: URLRequest) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: Any]) ?? [:]
    }

    private func makeManager(transport: MockTransport, store: SessionStore = InMemorySessionStore(),
                             clock: TestClock = TestClock()) -> SessionManager {
        SessionManager(baseURL: testBaseURL, transport: transport, store: store, device: testDevice,
                       userAgent: "PDV2Gestao/1.0.0 (iOS 26.1; iPhone16,2)", now: { clock.now })
    }

    /// Servidor falso: login devolve o par "1"; cada refresh devolve o par seguinte.
    private func standardServer(refreshDelay: UInt64 = 0, meHandler: ((URLRequest) -> MockTransport.Response)? = nil)
        -> MockTransport {
        let counter = Counter()
        return MockTransport { request in
            switch request.url?.path {
            case "/api/v1/app/auth/login":
                return .init(status: 200, body: try fixture("login_ok"))
            case "/api/v1/app/auth/refresh":
                let n = counter.next() + 1
                return .init(status: 200, body: json(["tokens": self.tokens("\(n)")]), delay: refreshDelay)
            case "/api/v1/app/me":
                if let meHandler { return meHandler(request) }
                return .init(status: 200, body: try fixture("me"))
            case "/api/v1/app/auth/logout":
                return .init(status: 200, body: json(["ok": true]))
            default:
                return .init(status: 404, body: json(["error": "Registro não encontrado.", "code": "not_found"]))
            }
        }
    }

    func testLoginKeepsAccessInMemoryAndRefreshInStore() async throws {
        let store = InMemorySessionStore()
        let transport = standardServer()
        let manager = makeManager(transport: transport, store: store)

        guard case let .signedIn(me) = try await manager.login(email: "ana@mercadobompreco.com.br", password: "x") else {
            return XCTFail("Esperava login direto")
        }
        XCTAssertEqual(me.user.name, "Ana Souza")
        let saved = try XCTUnwrap(store.load())
        XCTAssertTrue(saved.refreshToken.hasPrefix("pr_"))
        XCTAssertEqual(saved.deviceID, "d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90")
        XCTAssertEqual(saved.apiBaseURL, "https://pdv2.crowntech.com.br")
        let stored = try JSONEncoder().encode(saved)
        XCTAssertFalse(String(decoding: stored, as: UTF8.self).contains("pa_"), "Access token nunca é gravado")

        let loginBody = bodyObject(try XCTUnwrap(transport.requests(to: "/api/v1/app/auth/login").first))
        XCTAssertEqual(Set(loginBody.keys), ["email", "password", "device"])
        XCTAssertNil(transport.requests.first?.value(forHTTPHeaderField: "Authorization"))

        let _: Me = try await manager.send(.get("/api/v1/app/me"))
        let meRequest = try XCTUnwrap(transport.requests(to: "/api/v1/app/me").first)
        XCTAssertTrue(meRequest.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer pa_") ?? false)
        let refreshCount = await manager.refreshCount
        XCTAssertEqual(refreshCount, 0)
    }

    func testMFAFlow() async throws {
        let transport = MockTransport { request in
            switch request.url?.path {
            case "/api/v1/app/auth/login": return .init(status: 200, body: try fixture("login_mfa"))
            case "/api/v1/app/auth/mfa":
                let body = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: Any]) ?? [:]
                if body["code"] as? String == "123456" { return .init(status: 200, body: try fixture("mfa_ok")) }
                return .init(status: 401, body: json(["error": "Código incorreto. Confira o aplicativo autenticador.",
                                                      "code": "mfa_invalid"]))
            default: return .init(status: 404, body: json([:] as [String: String]))
            }
        }
        let store = InMemorySessionStore()
        let manager = makeManager(transport: transport, store: store)
        guard case let .mfaRequired(challenge) = try await manager.login(email: "admin@crowntech.com.br", password: "x") else {
            return XCTFail("Esperava MFA")
        }
        XCTAssertNil(store.load(), "Sem tokens antes do código")
        XCTAssertEqual(challenge.email, "admin@crowntech.com.br")

        do {
            _ = try await manager.verifyMFA(challenge: challenge, code: "000000", recoveryCode: nil)
            XCTFail("Código errado deveria falhar")
        } catch let error as APIError {
            XCTAssertEqual(error.code, "mfa_invalid")
            XCTAssertEqual(error.action, .showMessage)
        }
        let me = try await manager.verifyMFA(challenge: challenge, code: "123456", recoveryCode: nil)
        XCTAssertTrue(me.hasPlatform)
        XCTAssertNotNil(store.load())
        let body = bodyObject(try XCTUnwrap(transport.requests(to: "/api/v1/app/auth/mfa").last))
        XCTAssertEqual(Set(body.keys), ["mfa_token", "code"])
    }

    func testTokenExpiredRefreshesAndRetriesOnce() async throws {
        let calls = Counter()
        let transport = standardServer(meHandler: { _ in
            calls.next() == 0
                ? .init(status: 401, body: (try? fixture("error_token_expired")) ?? Data())
                : .init(status: 200, body: (try? fixture("me")) ?? Data())
        })
        let store = InMemorySessionStore()
        let manager = makeManager(transport: transport, store: store)
        _ = try await manager.login(email: "a@b.c", password: "x")

        let me: Me = try await manager.send(.get("/api/v1/app/me"))
        XCTAssertEqual(me.user.id, 12)
        let meCalls = transport.requests(to: "/api/v1/app/me")
        XCTAssertEqual(meCalls.count, 2)
        XCTAssertEqual(meCalls[1].value(forHTTPHeaderField: "Authorization"), "Bearer pa_access_1_xxxxxxxxxxxxxxxx")
        XCTAssertEqual(transport.requests(to: "/api/v1/app/auth/refresh").count, 1)
        // Refresh rotativo: o novo refresh substitui o antigo no armazenamento.
        XCTAssertEqual(store.load()?.refreshToken, "pr_refresh_1_xxxxxxxxxxxxxxxx")
        let refreshBody = bodyObject(try XCTUnwrap(transport.requests(to: "/api/v1/app/auth/refresh").first))
        XCTAssertTrue((refreshBody["refresh_token"] as? String)?.hasPrefix("pr_Zm9v") ?? false)
    }

    func testSecondTokenExpiredSignsOut() async throws {
        let transport = standardServer(meHandler: { _ in
            .init(status: 401, body: (try? fixture("error_token_expired")) ?? Data())
        })
        let store = InMemorySessionStore()
        let manager = makeManager(transport: transport, store: store)
        _ = try await manager.login(email: "a@b.c", password: "x")
        do {
            let _: Me = try await manager.send(.get("/api/v1/app/me"))
            XCTFail("Deveria sair")
        } catch let error as APIError {
            XCTAssertEqual(error.code, "unauthenticated")
        }
        XCTAssertEqual(transport.requests(to: "/api/v1/app/me").count, 2, "Repete só UMA vez")
        XCTAssertNil(store.load())
    }

    func testConcurrentRequestsShareSingleRefresh() async throws {
        let clock = TestClock()
        let transport = standardServer(refreshDelay: 150_000_000)
        let manager = makeManager(transport: transport, clock: clock)
        _ = try await manager.login(email: "a@b.c", password: "x")
        clock.advance(1000) // access venceu

        async let first: Me = manager.send(.get("/api/v1/app/me"))
        async let second: Me = manager.send(.get("/api/v1/app/me"))
        async let third: Me = manager.send(.get("/api/v1/app/me"))
        let results = try await [first, second, third]
        XCTAssertEqual(results.count, 3)
        XCTAssertEqual(transport.requests(to: "/api/v1/app/auth/refresh").count, 1, "Uma renovação por vez")
        for request in transport.requests(to: "/api/v1/app/me") {
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer pa_access_1_xxxxxxxxxxxxxxxx")
        }
    }

    func testRenewsProactivelyWhenLessThan60Seconds() async throws {
        let clock = TestClock()
        let transport = standardServer()
        let manager = makeManager(transport: transport, clock: clock)
        _ = try await manager.login(email: "a@b.c", password: "x")
        clock.advance(830) // faltam 70 s: não renova
        let _: Me = try await manager.send(.get("/api/v1/app/me"))
        XCTAssertEqual(transport.requests(to: "/api/v1/app/auth/refresh").count, 0)
        clock.advance(20) // faltam 50 s: renova antes
        let _: Me = try await manager.send(.get("/api/v1/app/me"))
        XCTAssertEqual(transport.requests(to: "/api/v1/app/auth/refresh").count, 1)
        XCTAssertEqual(transport.requests(to: "/api/v1/app/me").last?.value(forHTTPHeaderField: "Authorization"),
                       "Bearer pa_access_1_xxxxxxxxxxxxxxxx")
    }

    func testDeviceRevokedOnRefreshClearsSession() async throws {
        let store = InMemorySessionStore(StoredSession(refreshToken: "pr_old", deviceID: "d1",
                                                       apiBaseURL: "https://pdv2.crowntech.com.br"))
        let transport = MockTransport { _ in
            .init(status: 401, body: json(["error": "Este aparelho foi desconectado da sua conta. Entre de novo.",
                                           "code": "device_revoked"]))
        }
        let manager = makeManager(transport: transport, store: store)
        do {
            _ = try await manager.restore()
            XCTFail("Deveria falhar")
        } catch let error as APIError {
            XCTAssertEqual(error.action, .signOut(message: "Este aparelho foi desconectado da sua conta. Entre de novo."))
        }
        XCTAssertNil(store.load())
        let hasSession = await manager.hasStoredSession
        XCTAssertFalse(hasSession)
    }

    func testAccessEndedOnRefreshClearsSession() async throws {
        let store = InMemorySessionStore(StoredSession(refreshToken: "pr_old", deviceID: "d1",
                                                       apiBaseURL: "https://pdv2.crowntech.com.br"))
        let transport = MockTransport { _ in
            .init(status: 403, body: json(["error": "Acesso ao app não liberado.", "code": "app_access_denied"]))
        }
        let manager = makeManager(transport: transport, store: store)
        do {
            _ = try await manager.restore()
            XCTFail("Deveria falhar")
        } catch let error as APIError {
            XCTAssertEqual(error.action, .accessDenied)
        }
        XCTAssertNil(store.load())
    }

    func testRestoreUsesStoredRefresh() async throws {
        let store = InMemorySessionStore(StoredSession(refreshToken: "pr_saved", deviceID: "d1",
                                                       apiBaseURL: "https://pdv2.crowntech.com.br"))
        let transport = standardServer()
        let manager = makeManager(transport: transport, store: store)
        let me = try await manager.restore()
        XCTAssertEqual(me.user.id, 12)
        let body = bodyObject(try XCTUnwrap(transport.requests(to: "/api/v1/app/auth/refresh").first))
        XCTAssertEqual(body["refresh_token"] as? String, "pr_saved")
        XCTAssertEqual(store.load()?.deviceID, "d1")
        XCTAssertEqual(store.load()?.refreshToken, "pr_refresh_1_xxxxxxxxxxxxxxxx")
    }

    func testOfflineRestoreKeepsStoredSession() async throws {
        let store = InMemorySessionStore(StoredSession(refreshToken: "pr_saved", deviceID: "d1",
                                                       apiBaseURL: "https://pdv2.crowntech.com.br"))
        let transport = MockTransport { _ in throw APIError.offline }
        let manager = makeManager(transport: transport, store: store)
        do {
            _ = try await manager.restore()
            XCTFail("Deveria falhar")
        } catch {
            XCTAssertEqual(error as? APIError, .offline)
        }
        XCTAssertNotNil(store.load(), "Sem rede não apaga a sessão")
    }

    func testChangingServerDiscardsSession() async {
        let store = InMemorySessionStore(StoredSession(refreshToken: "pr_saved", deviceID: "d1",
                                                       apiBaseURL: "http://127.0.0.1:8000"))
        let manager = makeManager(transport: standardServer(), store: store)
        let hasSession = await manager.hasStoredSession
        XCTAssertFalse(hasSession)
        XCTAssertNil(store.load())
    }

    func testLogoutCallsServerAndClears() async throws {
        let store = InMemorySessionStore()
        let transport = standardServer()
        let manager = makeManager(transport: transport, store: store)
        _ = try await manager.login(email: "a@b.c", password: "x")
        await manager.logout()
        let logout = try XCTUnwrap(transport.requests(to: "/api/v1/app/auth/logout").first)
        XCTAssertTrue(logout.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer pa_") ?? false)
        XCTAssertNotNil(bodyObject(logout)["refresh_token"])
        XCTAssertNil(store.load())
        let access = await manager.currentAccessToken
        XCTAssertNil(access)
    }

    func testLogoutIgnoresServerFailure() async throws {
        let store = InMemorySessionStore()
        let transport = standardServer()
        let manager = makeManager(transport: transport, store: store)
        _ = try await manager.login(email: "a@b.c", password: "x")
        transport.setHandler { _ in throw APIError.offline }
        await manager.logout()
        XCTAssertNil(store.load())
    }

    func testKeychainStoreRoundTrip() throws {
        let store = KeychainSessionStore(service: "br.com.crowntech.pdv2gestao.tests", account: UUID().uuidString)
        defer { store.clear() }
        XCTAssertNil(store.load())
        let session = StoredSession(refreshToken: "pr_x", deviceID: "d", apiBaseURL: "https://pdv2.crowntech.com.br")
        do {
            try store.save(session)
        } catch let error as NSError where error.code == Int(errSecMissingEntitlement) {
            // Build de simulador com CODE_SIGNING_ALLOWED=NO não tem acesso ao Keychain.
            throw XCTSkip("Keychain indisponível sem assinatura (errSecMissingEntitlement)")
        }
        XCTAssertEqual(store.load(), session)
        try store.save(StoredSession(refreshToken: "pr_y", deviceID: "d", apiBaseURL: "https://pdv2.crowntech.com.br"))
        XCTAssertEqual(store.load()?.refreshToken, "pr_y")
        store.clear()
        XCTAssertNil(store.load())
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(session)) as? [String: String]
        XCTAssertEqual(Set(encoded?.keys ?? [:].keys), ["refresh_token", "device_id", "api_base_url"])
    }
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    /// Devolve o valor atual e incrementa.
    func next() -> Int { lock.withLock { defer { value += 1 }; return value } }
}
