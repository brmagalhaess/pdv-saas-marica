import SwiftUI

/// S3: conta existe, mas o app não foi liberado. Nenhuma sessão fica guardada.
struct AccessDeniedView: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                Image(systemName: "person.crop.circle.badge.xmark")
                    .font(.system(.largeTitle))
                    .foregroundStyle(.orange)
                    .padding(.top, 60)
                    .accessibilityHidden(true)
                Text("Acesso ao app não liberado")
                    .font(.title.bold())
                    .multilineTextAlignment(.center)
                    .accessibilityAddTraits(.isHeader)
                Text("Sua conta existe, mas o administrador do PDV2 ainda não liberou o uso do aplicativo para você. Peça a liberação e tente de novo.")
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Button {
                    app.backToLogin()
                } label: {
                    Text("Tentar de novo").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                Button("Sair", role: .destructive) {
                    Task { await app.signOut() }
                }
            }
            .padding(24)
            .frame(maxWidth: 520)
            .frame(maxWidth: .infinity)
        }
        .background(PDVTheme.background.ignoresSafeArea())
    }
}

#Preview {
    AccessDeniedView().environmentObject(AppState.preview(phase: .accessDenied))
}
