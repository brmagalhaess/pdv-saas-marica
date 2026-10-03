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
        .tint(PDVTheme.blue)
    }
}

struct LoginView: View {
    @ObservedObject var session: AppSession
    @State private var email = ""
    @State private var password = ""
    @State private var showingPassword = false
    @State private var showingAdvanced = false

    var body: some View {
        ZStack {
            LinearGradient(colors: [PDVTheme.navy, PDVTheme.blueDeep, Color(red: 0.06, green: 0.52, blue: 0.56)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
                .ignoresSafeArea()
            Circle()
                .fill(.white.opacity(0.08))
                .frame(width: 290, height: 290)
                .blur(radius: 4)
                .offset(x: 150, y: -360)

            ScrollView(showsIndicators: false) {
                VStack(spacing: 24) {
                    brandHeader
                    loginCard
                    footer
                }
                .padding(.horizontal, 20)
                .padding(.top, 44)
                .padding(.bottom, 24)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
        }
    }

    private var brandHeader: some View {
        VStack(spacing: 13) {
            BrandMark(size: 66)
            Text("PDV2 MONITOR")
                .font(.system(size: 14, weight: .bold, design: .rounded))
                .tracking(2.6)
                .foregroundStyle(.white.opacity(0.82))
            Text("A visão do seu negócio,\nsempre com você.")
                .font(.system(size: 30, weight: .bold, design: .rounded))
                .multilineTextAlignment(.center)
                .foregroundStyle(.white)
                .lineSpacing(2)
            Text("Indicadores essenciais para decidir mais rápido.")
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.72))
        }
    }

    private var loginCard: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Entrar no monitoramento")
                    .font(.title2.bold())
                    .foregroundStyle(PDVTheme.ink)
                Text("Use o mesmo acesso de proprietário do PDV2.")
                    .font(.subheadline)
                    .foregroundStyle(PDVTheme.muted)
            }

            VStack(spacing: 14) {
                InputField(title: "E-mail", systemImage: "envelope", text: $email,
                           contentType: .username, keyboard: .emailAddress)
                PasswordField(text: $password, isVisible: $showingPassword)
            }

            DisclosureGroup(isExpanded: $showingAdvanced) {
                VStack(alignment: .leading, spacing: 8) {
                    TextField("URL da API", text: $session.apiBaseURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .padding(.horizontal, 14)
                        .padding(.vertical, 13)
                        .background(PDVTheme.surface, in: RoundedRectangle(cornerRadius: 13))
                    Text("Simulador: http://127.0.0.1:8000  •  Produção: domínio HTTPS")
                        .font(.caption)
                        .foregroundStyle(PDVTheme.muted)
                }
                .padding(.top, 8)
            } label: {
                Label("Configuração do servidor", systemImage: "slider.horizontal.3")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(PDVTheme.blue)
            }

            if !session.message.isEmpty {
                Label(session.message, systemImage: "exclamationmark.triangle.fill")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Button {
                Task { await session.login(email: email, password: password) }
            } label: {
                HStack(spacing: 10) {
                    if session.isLoading {
                        ProgressView().tint(.white)
                        Text("Validando acesso…")
                    } else {
                        Text("Entrar")
                        Image(systemName: "arrow.right")
                    }
                }
                .font(.headline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 15)
            }
            .buttonStyle(.borderedProminent)
            .tint(PDVTheme.blue)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .disabled(session.isLoading || email.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || password.isEmpty)

            HStack(spacing: 8) {
                Image(systemName: "lock.shield.fill").foregroundStyle(PDVTheme.mint)
                Text("Acesso somente leitura e sessão protegida")
                    .font(.caption)
                    .foregroundStyle(PDVTheme.muted)
            }
        }
        .padding(24)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .stroke(.white.opacity(0.7), lineWidth: 1)
        }
        .shadow(color: .black.opacity(0.18), radius: 24, y: 12)
    }

    private var footer: some View {
        Text("PDV2 • Monitoramento para proprietários")
            .font(.caption)
            .foregroundStyle(.white.opacity(0.58))
    }
}

private struct InputField: View {
    let title: String
    let systemImage: String
    @Binding var text: String
    let contentType: UITextContentType
    let keyboard: UIKeyboardType

    var body: some View {
        HStack(spacing: 11) {
            Image(systemName: systemImage).foregroundStyle(PDVTheme.blue).frame(width: 20)
            TextField(title, text: $text)
                .textContentType(contentType)
                .keyboardType(keyboard)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .background(PDVTheme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

private struct PasswordField: View {
    @Binding var text: String
    @Binding var isVisible: Bool

    var body: some View {
        HStack(spacing: 11) {
            Image(systemName: "lock").foregroundStyle(PDVTheme.blue).frame(width: 20)
            Group {
                if isVisible { TextField("Senha", text: $text) }
                else { SecureField("Senha", text: $text) }
            }
            .textContentType(.password)
            Button(isVisible ? "Ocultar" : "Mostrar") { isVisible.toggle() }
                .font(.caption.weight(.bold))
                .foregroundStyle(PDVTheme.blue)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .background(PDVTheme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}
