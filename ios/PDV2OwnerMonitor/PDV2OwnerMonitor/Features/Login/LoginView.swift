import SwiftUI

/// S1: Entrar.
struct LoginView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.openURL) private var openURL
    @State private var email = ""
    @State private var password = ""
    @State private var showPassword = false
    @FocusState private var focus: Field?

    private enum Field { case email, password }

    private var canSubmit: Bool {
        !app.isBusy && email.contains("@") && !password.isEmpty
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                header
                if app.canRetryRestore { retryCard }
                form
                demo
                if app.config.allowServerOverride { serverField }
                footer
            }
            .padding(24)
            .frame(maxWidth: 520)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(PDVTheme.background.ignoresSafeArea())
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            BrandMark()
            Text("PDV2 Gestão")
                .font(.largeTitle.bold())
                .accessibilityAddTraits(.isHeader)
            Text("Acompanhe sua empresa pelo celular.")
                .font(.title3)
                .foregroundStyle(.secondary)
        }
        .padding(.top, 24)
    }

    private var retryCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Você já estava conectado neste aparelho.")
                .font(.subheadline)
            Text("Se passou muito tempo, pode ser preciso entrar de novo com e-mail e senha.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                Task { await app.restoreSession() }
            } label: {
                Label("Tentar de novo", systemImage: "arrow.clockwise")
            }
            .buttonStyle(.bordered)
            .disabled(app.isBusy)
        }
        .card()
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Text("E-mail").font(.subheadline.weight(.medium))
                TextField("seu@email.com.br", text: $email)
                    .textContentType(.username)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.next)
                    .focused($focus, equals: .email)
                    .onSubmit { focus = .password }
                    .padding(14)
                    .background(PDVTheme.field, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .accessibilityLabel("E-mail")
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("Senha").font(.subheadline.weight(.medium))
                HStack {
                    Group {
                        if showPassword {
                            TextField("Sua senha", text: $password)
                        } else {
                            SecureField("Sua senha", text: $password)
                        }
                    }
                    .textContentType(.password)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.go)
                    .focused($focus, equals: .password)
                    .onSubmit(submit)
                    .accessibilityLabel("Senha")
                    Button {
                        showPassword.toggle()
                    } label: {
                        Image(systemName: showPassword ? "eye.slash" : "eye")
                            .foregroundStyle(.secondary)
                    }
                    .accessibilityLabel(showPassword ? "Ocultar senha" : "Mostrar senha")
                }
                .padding(14)
                .background(PDVTheme.field, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }

            if let message = app.loginMessage {
                Label(message, systemImage: "exclamationmark.circle.fill")
                    .font(.subheadline)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.updatesFrequently)
            }

            Button(action: submit) {
                HStack {
                    if app.isBusy { ProgressView().tint(.white) }
                    Text(app.isBusy ? "Entrando…" : "Entrar")
                }
                .font(.headline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!canSubmit)
        }
    }

    private var serverField: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Servidor (desenvolvimento)").font(.subheadline.weight(.medium))
            TextField(app.config.defaultBaseURL, text: $app.serverOverride)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(14)
                .background(PDVTheme.field, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            Text("Vazio usa \(app.config.defaultBaseURL). Só aparece em versões de teste.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var demo: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                focus = nil
                app.enterDemo()
            } label: {
                Label("Ver demonstração", systemImage: "play.circle")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 4)
            }
            .buttonStyle(.bordered)
            .controlSize(.large)
            .disabled(app.isBusy)
            .accessibilityIdentifier("verDemonstracao")
            Text("Conheça o app com dados fictícios, sem precisar de conta.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("O acesso ao app é liberado pelo administrador do PDV2.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            HStack(spacing: 20) {
                Button("Suporte") { open(PublicLinks.production.supportURL) }
                Button("Política de privacidade") { open(PublicLinks.production.privacyURL) }
            }
            .font(.footnote.weight(.semibold))
        }
    }

    private func open(_ value: String) {
        if let url = URL(string: value) { openURL(url) }
    }

    private func submit() {
        guard canSubmit else { return }
        focus = nil
        Task { await app.signIn(email: email, password: password) }
    }
}

#Preview {
    LoginView().environmentObject(AppState.preview(phase: .login))
}
