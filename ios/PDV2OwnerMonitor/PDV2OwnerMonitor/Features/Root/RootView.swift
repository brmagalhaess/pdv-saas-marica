import SwiftUI

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
                HomeView()
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
        .sheet(isPresented: $app.showBiometricOffer) {
            BiometricOfferView()
                .presentationDetents([.medium])
                .interactiveDismissDisabled()
        }
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
