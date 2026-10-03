import SwiftUI

struct RootView: View {
    @ObservedObject var session: AppSession

    var body: some View {
        Group {
            if session.isAuthenticated {
                OwnerDashboardView(session: session)
            } else {
                LoginView(session: session)
            }
        }
        .tint(Color(red: 0.09, green: 0.43, blue: 0.83))
    }
}

struct LoginView: View {
    @ObservedObject var session: AppSession
    @State private var email = ""
    @State private var password = ""
    @State private var showingPassword = false

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.04, green: 0.12, blue: 0.24), Color(red: 0.08, green: 0.34, blue: 0.58)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
                .ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    VStack(alignment: .leading, spacing: 8) {
                        Image(systemName: "chart.line.uptrend.xyaxis")
                            .font(.system(size: 34, weight: .bold))
                            .foregroundStyle(.white)
                        Text("PDV2 Monitor")
                            .font(.system(size: 34, weight: .bold, design: .rounded))
                            .foregroundStyle(.white)
                        Text("Acompanhe sua operação em tempo real.")
                            .font(.subheadline)
                            .foregroundStyle(.white.opacity(0.78))
                    }
                    .padding(.top, 56)

                    VStack(alignment: .leading, spacing: 16) {
                        Text("Acesso do proprietário")
                            .font(.title3.bold())
                        Text("Use o mesmo e-mail e senha do admin da empresa.")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        TextField("E-mail", text: $email)
                            .textContentType(.username)
                            .keyboardType(.emailAddress)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .monitorField()
                        HStack {
                            Group {
                                if showingPassword { TextField("Senha", text: $password) }
                                else { SecureField("Senha", text: $password) }
                            }
                            .textContentType(.password)
                            Button(showingPassword ? "Ocultar" : "Mostrar") { showingPassword.toggle() }
                                .font(.caption.bold())
                                .foregroundStyle(.blue)
                        }
                        .monitorField()
                        TextField("URL da API", text: $session.apiBaseURL)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .monitorField()
                        Text("No Simulador use http://127.0.0.1:8000. No iPhone, informe o domínio HTTPS do servidor.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if !session.message.isEmpty {
                            Label(session.message, systemImage: "exclamationmark.triangle.fill")
                                .font(.subheadline)
                                .foregroundStyle(.red)
                        }
                        Button {
                            Task { await session.login(email: email, password: password) }
                        } label: {
                            HStack {
                                Spacer()
                                if session.isLoading { ProgressView().tint(.white) }
                                else { Text("Entrar no monitoramento").fontWeight(.bold) }
                                Spacer()
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                        .disabled(session.isLoading || email.isEmpty || password.isEmpty)
                    }
                    .padding(24)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
                }
                .padding(.horizontal, 20)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
        }
    }
}

private extension View {
    func monitorField() -> some View {
        self.padding(.horizontal, 14).padding(.vertical, 12)
            .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
    }
}
