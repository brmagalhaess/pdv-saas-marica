import Foundation

struct MFAChallenge: Equatable {
    let token: String
    let expiresAt: String
    let userName: String
    let email: String
}

enum LoginOutcome {
    case signedIn(Me)
    case mfaRequired(MFAChallenge)
}

/// Guarda a sessão do app e faz as chamadas autenticadas.
///
/// - Access token só em memória; refresh + device_id + URL no `SessionStore`.
/// - Renovação "single-flight": uma por vez; quem recebe `token_expired` espera
///   a renovação em andamento e repete a chamada UMA vez.
/// - Renova antes se faltar menos de 60 s para o access vencer.
actor SessionManager {
    nonisolated let baseURL: URL
    private let transport: HTTPTransport
    private let store: SessionStore
    private let builder: APIRequestBuilder
    private let device: LoginRequest.Device
    private let now: @Sendable () -> Date

    private var accessToken: String?
    private var accessExpiry: Date?
    private var stored: StoredSession?
    private var refreshTask: Task<Void, Error>?

    /// Quantas renovações foram feitas (diagnóstico e testes).
    private(set) var refreshCount = 0

    init(baseURL: URL, transport: HTTPTransport, store: SessionStore,
         device: LoginRequest.Device, userAgent: String,
         now: @escaping @Sendable () -> Date = { Date() }) {
        self.baseURL = baseURL
        self.transport = transport
        self.store = store
        self.device = device
        self.builder = APIRequestBuilder(baseURL: baseURL, userAgent: userAgent)
        self.now = now
        let saved = store.load()
        if let saved, saved.apiBaseURL == baseURL.absoluteString {
            stored = saved
        } else {
            // Servidor mudou: descarta tudo.
            if saved != nil { store.clear() }
            stored = nil
        }
    }

    var hasStoredSession: Bool { stored != nil }
    var deviceID: String? { stored?.deviceID }

    // MARK: Entrada

    func login(email: String, password: String) async throws -> LoginOutcome {
        clearLocal()
        let body = LoginRequest(email: email, password: password, device: device)
        let response: LoginResponse = try await perform(
            try Endpoint.send("POST", "/api/v1/app/auth/login", body: body, authenticated: false), token: nil)
        return try outcome(from: response)
    }

    func verifyMFA(challenge: MFAChallenge, code: String?, recoveryCode: String?) async throws -> Me {
        let body = MFARequest(mfaToken: challenge.token, code: code, recoveryCode: recoveryCode)
        let response: LoginResponse = try await perform(
            try Endpoint.send("POST", "/api/v1/app/auth/mfa", body: body, authenticated: false), token: nil)
        guard case let .signedIn(me) = try outcome(from: response) else { throw APIError.unavailable }
        return me
    }

    private func outcome(from response: LoginResponse) throws -> LoginOutcome {
        switch response.status {
        case "ok":
            guard let tokens = response.tokens, let me = response.me else { throw APIError.unavailable }
            try apply(tokens, deviceID: me.device.id)
            return .signedIn(me)
        case "mfa_required":
            guard let token = response.mfaToken else { throw APIError.unavailable }
            return .mfaRequired(MFAChallenge(token: token, expiresAt: response.mfaExpiresAt ?? "",
                                             userName: response.user?.name ?? "",
                                             email: response.user?.email ?? ""))
        default:
            throw APIError.unavailable
        }
    }

    /// Abertura a frio: renova com o refresh guardado e carrega `/me`.
    func restore() async throws -> Me {
        guard stored != nil else { throw APIError.signedOut() }
        try await refreshTokens()
        return try await send(.get("/api/v1/app/me"))
    }

    // MARK: Chamadas autenticadas

    func send<T: Decodable & Sendable>(_ endpoint: Endpoint) async throws -> T {
        if accessToken == nil || accessExpiringSoon {
            try await refreshTokens()
        }
        let used = accessToken
        do {
            return try await perform(endpoint, token: used)
        } catch let error as APIError where error.code == "token_expired" {
            // Se outra chamada já renovou, só repete; senão renova (uma vez só).
            if accessToken == used { try await refreshTokens() }
            do {
                return try await perform(endpoint, token: accessToken)
            } catch let retry as APIError where retry.code == "token_expired" {
                clearLocal()
                throw APIError.signedOut()
            }
        }
    }

    private var accessExpiringSoon: Bool {
        guard let accessExpiry else { return true }
        return accessExpiry.timeIntervalSince(now()) < 60
    }

    func refreshTokens() async throws {
        if let running = refreshTask {
            return try await running.value
        }
        let task = Task { try await self.performRefresh() }
        refreshTask = task
        defer { refreshTask = nil }
        try await task.value
    }

    private func performRefresh() async throws {
        guard let stored else { throw APIError.signedOut() }
        refreshCount += 1
        let response: RefreshResponse = try await perform(
            try Endpoint.send("POST", "/api/v1/app/auth/refresh",
                              body: RefreshRequest(refreshToken: stored.refreshToken), authenticated: false),
            token: nil)
        try apply(response.tokens, deviceID: stored.deviceID)
    }

    private func apply(_ tokens: TokenPair, deviceID: String) throws {
        accessToken = tokens.accessToken
        // Usa a duração (não o relógio do servidor) para não sofrer com relógio errado no aparelho.
        accessExpiry = now().addingTimeInterval(TimeInterval(max(tokens.accessExpiresIn, 0)))
        let session = StoredSession(refreshToken: tokens.refreshToken, deviceID: deviceID,
                                    apiBaseURL: baseURL.absoluteString)
        stored = session
        do {
            try store.save(session)
        } catch {
            // Sem Keychain (ex.: build sem assinatura): a sessão vale só até fechar o app.
        }
    }

    private func perform<T: Decodable & Sendable>(_ endpoint: Endpoint, token: String?) async throws -> T {
        let request = try builder.request(for: endpoint, accessToken: endpoint.authenticated ? token : nil)
        let (data, response) = try await transport.perform(request)
        do {
            return try APIRequestBuilder.decode(T.self, data: data, response: response)
        } catch let error as APIError {
            if case .signOut = error.action { clearLocal() }
            if error.code == "app_access_denied" && endpoint.path.hasSuffix("/auth/refresh") { clearLocal() }
            throw error
        }
    }

    // MARK: Saída

    /// Chama `auth/logout` (ignora falhas) e apaga tudo localmente.
    func logout() async {
        let refresh = stored?.refreshToken
        let token = accessToken
        if refresh != nil || token != nil,
           let endpoint = try? Endpoint.send("POST", "/api/v1/app/auth/logout",
                                             body: LogoutRequest(refreshToken: refresh)) {
            if let request = try? builder.request(for: endpoint, accessToken: token) {
                _ = try? await transport.perform(request)
            }
        }
        clearLocal()
    }

    func clearLocal() {
        accessToken = nil
        accessExpiry = nil
        stored = nil
        store.clear()
    }

    // Para testes.
    var currentAccessToken: String? { accessToken }
}
