import SwiftUI

/// Pedido de exclusão de conta (o administrador conclui em até 15 dias).
struct DeleteAccountView: View {
    @EnvironmentObject private var app: AppState
    @State private var note = ""
    @State private var confirm = false
    @State private var isSending = false
    @State private var resultMessage: String?
    @State private var errorMessage: String?

    var body: some View {
        Form {
            Section {
                Text("O pedido desativa seu login no PDV2 (app e painel) e apaga seus dados pessoais em até 15 dias. Os dados da empresa, como vendas e notas, continuam com a empresa, como exige a lei.")
                    .font(.body)
            }
            Section("Motivo (opcional)") {
                TextField("Conte o motivo, se quiser", text: $note, axis: .vertical)
                    .lineLimit(3...6)
                    .onChange(of: note) { _, value in if value.count > 500 { note = String(value.prefix(500)) } }
            }
            if let errorMessage {
                Section { Text(errorMessage).foregroundStyle(.red) }
            }
            Section {
                Button(role: .destructive) {
                    confirm = true
                } label: {
                    HStack {
                        if isSending { ProgressView() }
                        Text("Pedir exclusão").frame(maxWidth: .infinity)
                    }
                }
                .disabled(isSending)
            }
        }
        .navigationTitle("Excluir minha conta")
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Pedir a exclusão da sua conta?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Pedir exclusão", role: .destructive) { Task { await send() } }
        } message: {
            Text("Você vai sair do app em todos os aparelhos.")
        }
        .alert("Pedido recebido", isPresented: Binding(
            get: { resultMessage != nil }, set: { _ in })) {
            Button("OK") {
                resultMessage = nil
                Task { await app.signOut() }
            }
        } message: {
            Text(resultMessage ?? "")
        }
    }

    private func send() async {
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        do {
            let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
            let response = try await app.api.requestAccountDeletion(note: trimmed.isEmpty ? nil : trimmed)
            resultMessage = response.message
        } catch {
            errorMessage = app.handle(error)?.message
        }
    }
}

#Preview {
    NavigationStack { DeleteAccountView() }.environmentObject(AppState.preview())
}
