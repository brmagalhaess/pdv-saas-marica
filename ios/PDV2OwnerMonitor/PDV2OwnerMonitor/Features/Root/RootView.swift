import SwiftUI
import UIKit

/// Decide a tela pela fase da sessão (S0–S3 e Início) e aplica a trava biométrica.
struct RootView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ZStack {
            switch app.phase {
            case .launching:
                LaunchView()
            case .login:
                LoginView()
            case .mfa(let challenge):
                MFAView(challenge: challenge)
            case .accessDenied:
                AccessDeniedView()
            case .home:
                VStack(spacing: 0) {
                    if app.isDemo { DemoBanner() }
                    HomeView()
                }
            }
            if app.isLocked {
                LockView()
                    .transition(.opacity)
                    .zIndex(1)
            }
        }
        .tint(PDVTheme.blue)
        .task { await app.start() }
        .onChange(of: scenePhase) { _, newPhase in app.scenePhaseChanged(newPhase) }
        // A camada do ZStack fica por baixo de sheets, alertas e diálogos. A trava de verdade
        // é uma janela própria acima de tudo (ver `LockOverlay`).
        .onChange(of: app.isLocked, initial: true) { _, locked in
            LockOverlay.shared.update(locked: locked, app: app)
        }
        .sheet(isPresented: $app.showBiometricOffer) {
            BiometricOfferView()
                .presentationDetents([.medium])
                .interactiveDismissDisabled()
        }
    }
}

/// Faixa fixa no alto durante a demonstração, com a saída sempre à mão.
struct DemoBanner: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkles")
                .foregroundStyle(PDVTheme.blue)
                .accessibilityHidden(true)
            Text("Demonstração · dados fictícios")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer(minLength: 8)
            Button("Sair da demonstração") { app.exitDemo() }
                .font(.footnote.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .accessibilityIdentifier("sairDemonstracao")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity)
        .background(PDVTheme.blue.opacity(0.10).ignoresSafeArea(edges: .top))
        .overlay(alignment: .bottom) { Divider() }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("faixaDemonstracao")
    }
}

struct LaunchView: View {
    var body: some View {
        VStack(spacing: 16) {
            BrandMark()
            ProgressView("Abrindo…")
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PDVTheme.background)
    }
}

/// Mostra a `LockView` numa janela própria, acima de qualquer sheet, alerta ou diálogo
/// (eles são apresentados na janela principal e ficariam por cima de uma camada comum).
@MainActor
final class LockOverlay {
    static let shared = LockOverlay()

    /// Nível da janela da trava: acima dos alertas do sistema apresentados pelo app.
    static let windowLevel = UIWindow.Level.alert + 1

    private(set) var window: UIWindow?

    var isShowing: Bool { window.map { !$0.isHidden } ?? false }

    func update(locked: Bool, app: AppState, scene: UIWindowScene? = nil) {
        if locked {
            show(app: app, scene: scene)
        } else {
            hide()
        }
    }

    private func show(app: AppState, scene: UIWindowScene?) {
        if let window, !window.isHidden { return }
        guard let scene = scene ?? Self.activeScene() else { return }
        // Tira o foco de qualquer campo (o teclado fica acima de outras janelas).
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        let window = UIWindow(windowScene: scene)
        window.windowLevel = Self.windowLevel
        let host = UIHostingController(rootView: LockView().environmentObject(app).tint(PDVTheme.blue))
        host.view.backgroundColor = .systemBackground
        window.rootViewController = host
        window.accessibilityViewIsModal = true
        window.makeKeyAndVisible()
        self.window = window
    }

    private func hide() {
        guard let window else { return }
        window.isHidden = true
        window.rootViewController = nil
        self.window = nil
        // Devolve o foco à janela principal do app.
        window.windowScene?.windows.first { $0.windowLevel == .normal }?.makeKey()
    }

    private static func activeScene() -> UIWindowScene? {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        return scenes.first { $0.activationState == .foregroundActive }
            ?? scenes.first { $0.activationState == .foregroundInactive }
            ?? scenes.first
    }
}

/// Tela que cobre os dados até a pessoa se identificar.
struct LockView: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        VStack(spacing: 20) {
            Spacer()
            BrandMark()
            Text("PDV2 Gestão").font(.title2.bold())
            Text("Desbloqueie para ver os dados da sua empresa")
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button {
                Task { await app.unlock() }
            } label: {
                Label("Desbloquear com \(app.biometryLabel)", systemImage: "faceid")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            Spacer()
            Button("Sair da conta", role: .destructive) {
                Task { await app.signOut() }
            }
            .padding(.bottom, 12)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PDVTheme.background.ignoresSafeArea())
    }
}

/// S4: oferta de desbloqueio por Face ID depois do 1º login.
struct BiometricOfferView: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "faceid")
                .font(.system(.largeTitle))
                .foregroundStyle(PDVTheme.blue)
                .accessibilityHidden(true)
            Text("Abrir o app com \(app.biometryLabel)?")
                .font(.title2.bold())
                .multilineTextAlignment(.center)
            Text("Os dados da sua empresa ficam protegidos quando alguém pegar o seu celular. Você pode mudar isso depois em Conta.")
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button {
                app.acceptBiometricOffer()
            } label: {
                Text("Usar").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            Button("Agora não") { app.declineBiometricOffer() }
        }
        .padding(24)
    }
}

#Preview("Trava") {
    LockView().environmentObject(AppState.preview())
}

#Preview("Oferta de Face ID") {
    BiometricOfferView().environmentObject(AppState.preview())
}
