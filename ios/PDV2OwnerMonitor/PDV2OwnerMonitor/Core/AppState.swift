import Foundation
import SwiftUI

/// Erro já traduzido para a tela.
struct ScreenError: Equatable {
    enum Kind { case offline, permission, general, signedOut }
    let kind: Kind
    let message: String
}

/// Estado global do app: fase (login, MFA, início…), sessão, empresa/loja/período.
@MainActor
final class AppState: ObservableObject {
    enum Phase: Equatable {
        case launching
        case login
        case mfa(MFAChallenge)
        case accessDenied
        case home
    }

    enum Tab: Hashable { case platform, summary, sales, cash, stock, more }
    enum MoreRoute: Hashable { case stock, credit, terminals, conflicts, account }

    private enum Keys {
        static let tenant = "pdv2.context.tenant"
        static let shop = "pdv2.context.shop"
        static let period = "pdv2.context.period"
        static let biometricLock = "pdv2.biometricLock"
        static let biometricOfferShown = "pdv2.biometricOfferShown"
        static let server = "pdv2.debug.server"
    }

    @Published private(set) var phase: Phase = .launching
    @Published var loginMessage: String?
    @Published var mfaMessage: String?
    /// Abertura falhou por rede: o login mostra "Tentar de novo" sem apagar a sessão.
    @Published private(set) var canRetryRestore = false
    @Published private(set) var me: Me?
    @Published private(set) var tenant: TenantAccess?
    @Published var shopID: String? { didSet { defaults.set(shopID, forKey: Keys.shop) } }
    @Published var period: Period { didSet { defaults.set(period.rawValue, forKey: Keys.period) } }
    @Published var selectedTab: Tab = .summary
    @Published var morePath: [MoreRoute] = []
    @Published var showTenantPicker = false
    @Published var showBiometricOffer = false
    @Published private(set) var isLocked = false
    @Published private(set) var isBusy = false
    @Published var biometricLockEnabled: Bool { didSet { defaults.set(biometricLockEnabled, forKey: Keys.biometricLock) } }
    @Published var serverOverride: String { didSet { defaults.set(serverOverride, forKey: Keys.server) } }

    let config: AppConfig
    let biometrics: BiometricAuthenticating
    private let defaults: UserDefaults
    private let transport: HTTPTransport
    private let store: SessionStore
    private(set) var session: SessionManager?
    private(set) var api: GestaoAPI
    private var backgroundedAt: Date?

    init(config: AppConfig = .fromBundle(),
         biometrics: BiometricAuthenticating = SystemBiometrics(),
         defaults: UserDefaults = .standard,
         transport: HTTPTransport = URLSessionTransport(),
         store: SessionStore = KeychainSessionStore(),
         api: GestaoAPI? = nil) {
        self.config = config
        self.biometrics = biometrics
        self.defaults = defaults
        self.transport = transport
        self.store = store
        self.period = Period(rawValue: defaults.string(forKey: Keys.period) ?? "") ?? .today
        self.shopID = defaults.string(forKey: Keys.shop)
        self.biometricLockEnabled = defaults.bool(forKey: Keys.biometricLock)
        self.serverOverride = config.allowServerOverride ? (defaults.string(forKey: Keys.server) ?? "") : ""
        if let data = defaults.data(forKey: Keys.tenant) {
            self.tenant = try? JSONDecoder().decode(TenantAccess.self, from: data)
        }
        self.api = api ?? PreviewGestaoAPI(failWith: .offline)
        if api == nil { rebuildSession() }
    }

    // MARK: Atalhos

    var isPlatform: Bool { me?.hasPlatform == true }
    var canManageAccess: Bool { me?.platform?.canManageAccess == true }
    var shops: [ShopRef] { tenant?.shops ?? [] }
    var links: PublicLinks { me?.links ?? .production }
    var selectedShop: ShopRef? { shops.first { $0.id == shopID } }
    var canSwitchTenant: Bool { isPlatform || (me?.tenants.count ?? 0) > 1 }
    var biometryLabel: String { biometrics.biometryName ?? "biometria" }

    func can(_ permission: AppPermission) -> Bool {
        isPlatform || tenant?.has(permission) == true
    }

    var queryContext: QueryContext? {
        tenant.map { QueryContext(tenantID: $0.id, shopID: shopID, period: period) }
    }

    // MARK: Sessão

    @discardableResult
    func rebuildSession() -> Bool {
        do {
            let url = try config.resolvedBaseURL(override: serverOverride)
            let descriptor = DeviceDescriptor.current(config: config)
            let manager = SessionManager(baseURL: url, transport: transport, store: store,
                                         device: descriptor.loginDevice, userAgent: descriptor.userAgent)
            session = manager
            api = LiveGestaoAPI(session: manager)
            return true
        } catch {
            session = nil
            loginMessage = (error as? APIError)?.userMessage ?? "Endereço do servidor inválido."
            return false
        }
    }

    /// S0: abertura do app.
    func start() async {
        guard phase == .launching else { return }
        guard let session, await session.hasStoredSession else {
            phase = .login
            return
        }
        if biometricLockEnabled && biometrics.canAuthenticate {
            isLocked = true
            await unlock()
        } else {
            await restoreSession()
        }
    }

    func unlock() async {
        let ok = await biometrics.authenticate(reason: "Desbloqueie para ver os dados da sua empresa")
        guard ok else { return }
        isLocked = false
        if phase == .launching { await restoreSession() }
    }

    func restoreSession() async {
        guard let session else { phase = .login; return }
        isBusy = true
        defer { isBusy = false }
        do {
            let me = try await session.restore()
            canRetryRestore = false
            enterHome(me, firstLogin: false)
        } catch let error as APIError where error.action == .accessDenied {
            await session.clearLocal()
            canRetryRestore = false
            phase = .accessDenied
        } catch let error as APIError where Self.endsSession(error) {
            // Sessão terminou de fato (revogado, refresh inválido…): apaga e pede login.
            await session.clearLocal()
            canRetryRestore = false
            switch error.action {
            case .signOut(let message), .backToLogin(let message): loginMessage = message
            default: loginMessage = nil
            }
            phase = .login
        } catch {
            // Erro passageiro (sem rede, 5xx, 429, 409…): mantém o refresh guardado
            // e deixa a pessoa tentar de novo, sem apagar a sessão.
            loginMessage = (error as? APIError)?.userMessage ?? APIErrorCatalog.genericMessage
            canRetryRestore = true
            phase = .login
        }
    }

    /// Só estes erros encerram a sessão guardada na abertura.
    private static func endsSession(_ error: APIError) -> Bool {
        switch error.action {
        case .signOut, .backToLogin: return true
        default: return false
        }
    }

    /// S1: entrar com e-mail e senha.
    func signIn(email: String, password: String) async {
        loginMessage = nil
        canRetryRestore = false
        guard rebuildSession(), let session else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            switch try await session.login(email: email.trimmingCharacters(in: .whitespacesAndNewlines),
                                           password: password) {
            case .signedIn(let me):
                enterHome(me, firstLogin: true)
            case .mfaRequired(let challenge):
                mfaMessage = nil
                phase = .mfa(challenge)
            }
        } catch {
            handleEntryError(error)
        }
    }

    /// S2: código do autenticador ou de recuperação.
    func verifyMFA(code: String?, recoveryCode: String?) async {
        guard case let .mfa(challenge) = phase, let session else { return }
        mfaMessage = nil
        isBusy = true
        defer { isBusy = false }
        do {
            let me = try await session.verifyMFA(challenge: challenge, code: code, recoveryCode: recoveryCode)
            enterHome(me, firstLogin: true)
        } catch let error as APIError where error.code == "mfa_invalid" || error.code == "mfa_locked"
            || error.code == "rate_limited" || error.code == "invalid_request" || error == .offline {
            mfaMessage = error.userMessage
        } catch {
            handleEntryError(error)
        }
    }

    private func handleEntryError(_ error: Error) {
        guard let apiError = error as? APIError else {
            loginMessage = APIErrorCatalog.genericMessage
            phase = .login
            return
        }
        switch apiError.action {
        case .accessDenied:
            phase = .accessDenied
        case .backToLogin(let message), .signOut(let message):
            loginMessage = message
            phase = .login
        default:
            loginMessage = apiError.userMessage
            phase = .login
        }
    }

    func backToLogin() {
        mfaMessage = nil
        loginMessage = nil
        phase = .login
    }

    /// Sair: avisa o servidor, apaga tokens e contexto, volta ao login.
    func signOut(message: String? = nil) async {
        await session?.logout()
        resetLocalState()
        loginMessage = message
        phase = .login
    }

    /// Sessão terminou do lado do servidor (revogado, refresh inválido…).
    func forceLogin(message: String) async {
        await session?.clearLocal()
        resetLocalState()
        loginMessage = message
        phase = .login
    }

    private func resetLocalState() {
        me = nil
        setTenant(nil)
        shopID = nil
        period = .today
        morePath = []
        isLocked = false
        canRetryRestore = false
        showTenantPicker = false
        showBiometricOffer = false
    }

    private func enterHome(_ me: Me, firstLogin: Bool) {
        self.me = me
        reconcileContext()
        selectedTab = (isPlatform && tenant == nil) ? .platform : .summary
        morePath = []
        loginMessage = nil
        phase = .home
        if firstLogin && !defaults.bool(forKey: Keys.biometricOfferShown) && biometrics.biometryName != nil {
            defaults.set(true, forKey: Keys.biometricOfferShown)
            showBiometricOffer = true
        }
    }

    /// Ajusta empresa/loja guardadas ao que a pessoa pode ver agora.
    private func reconcileContext() {
        guard let me else { return }
        if let current = tenant, let fresh = me.tenants.first(where: { $0.id == current.id }) {
            setTenant(fresh)
        } else if isPlatform {
            // Plataforma pode abrir qualquer empresa: mantém a guardada (ou nenhuma).
        } else {
            setTenant(me.tenants.first)
        }
        if let shopID, let tenant, !tenant.shops.isEmpty, !tenant.shops.contains(where: { $0.id == shopID }) {
            self.shopID = nil
        }
        if tenant == nil { shopID = nil }
    }

    func reloadMe() async {
        do {
            me = try await api.me()
            reconcileContext()
        } catch {
            _ = handle(error)
        }
    }

    // MARK: Contexto

    private func setTenant(_ value: TenantAccess?) {
        tenant = value
        if let value, let data = try? JSONEncoder().encode(value) {
            defaults.set(data, forKey: Keys.tenant)
        } else {
            defaults.removeObject(forKey: Keys.tenant)
        }
    }

    /// Trocar de empresa volta para "Todas as lojas".
    func selectTenant(_ value: TenantAccess) {
        if value.id != tenant?.id { shopID = nil }
        setTenant(value)
        showTenantPicker = false
    }

    /// Plataforma: "Abrir empresa" vira a empresa atual e vai ao Resumo.
    func openPlatformTenant(id: String, name: String) {
        if let own = me?.tenants.first(where: { $0.id == id }) {
            selectTenant(own)
        } else {
            selectTenant(.platformView(id: id, name: name, shops: tenant?.id == id ? shops : []))
        }
        selectedTab = .summary
    }

    /// O Resumo traz as lojas no alcance; usa para o seletor de loja.
    func updateShops(from dashboard: Dashboard) {
        guard let tenant, tenant.id == dashboard.tenant.id, tenant.shops != dashboard.shops else { return }
        setTenant(tenant.with(shops: dashboard.shops))
    }

    func goToStock() {
        if isPlatform {
            selectedTab = .more
            morePath = [.stock]
        } else {
            selectedTab = .stock
        }
    }

    func goToMore(_ route: MoreRoute) {
        selectedTab = .more
        morePath = [route]
    }

    func requestTenantChoice() {
        if isPlatform {
            selectedTab = .platform
        } else {
            showTenantPicker = true
        }
    }

    // MARK: Erros

    /// Executa a ação do contrato para o `code` e devolve o que a tela mostra.
    @discardableResult
    func handle(_ error: Error) -> ScreenError? {
        if error is CancellationError { return nil }
        guard let apiError = error as? APIError else {
            return ScreenError(kind: .general, message: APIErrorCatalog.genericMessage)
        }
        switch apiError.action {
        case .signOut(let message), .backToLogin(let message):
            Task { await forceLogin(message: message) }
            return ScreenError(kind: .signedOut, message: message)
        case .accessDenied:
            Task {
                await session?.clearLocal()
                resetLocalState()
                phase = .accessDenied
            }
            return ScreenError(kind: .signedOut, message: apiError.userMessage)
        case .openTenantPicker:
            requestTenantChoice()
            return ScreenError(kind: .general, message: apiError.userMessage)
        case .resetPeriod:
            period = .today
            return ScreenError(kind: .general, message: apiError.userMessage)
        case .reloadMeAndPickTenant:
            setTenant(nil)
            shopID = nil
            Task {
                await reloadMe()
                requestTenantChoice()
            }
            return ScreenError(kind: .general, message: apiError.userMessage)
        case .resetShop:
            shopID = nil
            return ScreenError(kind: .general, message: apiError.userMessage)
        case .permissionDenied:
            return ScreenError(kind: .permission, message: apiError.userMessage)
        case .refreshAndRetry, .showMessage:
            return ScreenError(kind: apiError == .offline ? .offline : .general, message: apiError.userMessage)
        }
    }

    // MARK: Biometria e ciclo de vida

    func acceptBiometricOffer() {
        biometricLockEnabled = true
        showBiometricOffer = false
    }

    func declineBiometricOffer() {
        showBiometricOffer = false
    }

    /// Liga/desliga a trava pela tela Conta (pede a biometria para ligar).
    func setBiometricLock(_ enabled: Bool) async {
        if enabled {
            guard biometrics.canAuthenticate else { biometricLockEnabled = false; return }
            biometricLockEnabled = await biometrics.authenticate(reason: "Confirme para ligar o desbloqueio do app")
        } else {
            biometricLockEnabled = false
        }
    }

    func scenePhaseChanged(_ newPhase: ScenePhase) {
        switch newPhase {
        case .background:
            backgroundedAt = Date()
        case .active:
            let shouldLock = phase == .home && biometrics.canAuthenticate &&
                BiometricLockPolicy.shouldLock(enabled: biometricLockEnabled, backgroundedAt: backgroundedAt, now: Date())
            backgroundedAt = nil
            if shouldLock && !isLocked {
                isLocked = true
                Task { await unlock() }
            }
        default:
            break
        }
    }

    // MARK: Previews

    static func preview(platform: Bool = false, phase: Phase = .home) -> AppState {
        let defaults = UserDefaults(suiteName: "preview") ?? .standard
        let state = AppState(config: .fromBundle(), biometrics: FakeBiometrics(), defaults: defaults,
                             store: InMemorySessionStore(), api: PreviewGestaoAPI())
        let json = platform ? PreviewFixtures.mePlatform : PreviewFixtures.me
        state.me = try? JSONDecoder().decode(Me.self, from: Data(json.utf8))
        if platform {
            state.tenant = .platformView(id: "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10", name: "Mercado Bom Preço",
                                         shops: [ShopRef(id: "a1b2c3d4-0001-4000-8000-000000000001", name: "Loja Centro")])
        } else {
            state.tenant = state.me?.tenants.first
        }
        state.shopID = nil
        state.period = .today
        state.phase = phase
        return state
    }

    /// Só para testes: entra direto na fase indicada.
    func setPhaseForTesting(_ value: Phase) { phase = value }
}
