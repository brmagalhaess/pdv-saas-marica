import SwiftUI

/// S2: código de verificação (equipe da plataforma).
struct MFAView: View {
    @EnvironmentObject private var app: AppState
    let challenge: MFAChallenge
    @State private var code = ""
    @State private var recoveryCode = ""
    @State private var useRecovery = false
    @FocusState private var focused: Bool

    private var isValid: Bool {
        if useRecovery { return MFAInput.normalizedRecovery(recoveryCode) != nil }
        return MFAInput.isValidCode(code)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Image(systemName: "lock.shield")
                    .font(.largeTitle)
                    .foregroundStyle(PDVTheme.blue)
                    .accessibilityHidden(true)
                Text("Código de verificação")
                    .font(.largeTitle.bold())
                    .accessibilityAddTraits(.isHeader)
                if !challenge.email.isEmpty {
                    Text(challenge.email).font(.subheadline).foregroundStyle(.secondary)
                }
                if useRecovery {
                    Text("Digite um dos seus códigos de recuperação (formato PDV2-XXXX-XXXX).")
                        .font(.body)
                    TextField("PDV2-XXXX-XXXX", text: $recoveryCode)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        .font(.title3.monospaced())
                        .focused($focused)
                        .padding(14)
                        .background(PDVTheme.field, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .accessibilityLabel("Código de recuperação")
                } else {
                    Text("Digite o código de 6 números do seu aplicativo autenticador.")
                        .font(.body)
                    TextField("000000", text: $code)
                        .keyboardType(.numberPad)
                        .textContentType(.oneTimeCode)
                        .font(.title.monospacedDigit())
                        .multilineTextAlignment(.center)
                        .focused($focused)
                        .padding(14)
                        .background(PDVTheme.field, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .onChange(of: code) { _, value in code = String(value.filter(\.isNumber).prefix(6)) }
                        .accessibilityLabel("Código de 6 números")
                }

                Button(useRecovery ? "Usar o código do aplicativo" : "Usar código de recuperação") {
                    useRecovery.toggle()
                    app.mfaMessage = nil
                }
                .font(.subheadline.weight(.semibold))

                if let message = app.mfaMessage {
                    Label(message, systemImage: "exclamationmark.circle.fill")
                        .font(.subheadline)
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Button {
                    submit()
                } label: {
                    HStack {
                        if app.isBusy { ProgressView().tint(.white) }
                        Text("Confirmar")
                    }
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 6)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(!isValid || app.isBusy)

                Button("Voltar") { app.backToLogin() }
                    .frame(maxWidth: .infinity)
            }
            .padding(24)
            .frame(maxWidth: 520)
            .frame(maxWidth: .infinity)
        }
        .background(PDVTheme.background.ignoresSafeArea())
        .onAppear { focused = true }
    }

    private func submit() {
        guard isValid else { return }
        Task {
            if useRecovery {
                await app.verifyMFA(code: nil, recoveryCode: MFAInput.normalizedRecovery(recoveryCode))
            } else {
                await app.verifyMFA(code: code, recoveryCode: nil)
            }
        }
    }
}

/// Validação local dos códigos (o servidor confere de verdade).
enum MFAInput {
    static func isValidCode(_ value: String) -> Bool {
        value.count == 6 && value.allSatisfy(\.isNumber)
    }

    /// Aceita com ou sem hífens/minúsculas; devolve `PDV2-ABCD-EFGH`.
    static func normalizedRecovery(_ value: String) -> String? {
        let compact = value.uppercased().filter { $0.isLetter || $0.isNumber }
        guard compact.count == 12, compact.hasPrefix("PDV2") else { return nil }
        let body = Array(compact.dropFirst(4))
        return "PDV2-" + String(body[0..<4]) + "-" + String(body[4..<8])
    }
}

#Preview {
    MFAView(challenge: MFAChallenge(token: "pm_x", expiresAt: "", userName: "Bruno", email: "admin@crowntech.com.br"))
        .environmentObject(AppState.preview(phase: .login))
}
