import SwiftUI

/// Conta: dados da pessoa, aparelhos conectados, Face ID, links, exclusão e saída.
struct AccountView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.openURL) private var openURL
    @StateObject private var devices = PagedState<AppDeviceInfo>()
    @State private var deviceToRevoke: AppDeviceInfo?
    @State private var confirmSignOut = false
    @State private var actionError: String?

    var body: some View {
        List {
            if let me = app.me {
                Section {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(me.user.name).font(.headline)
                        Text(me.user.email).font(.subheadline).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                }

                Section("Este aparelho") {
                    ValueRow(title: "Nome", value: me.device.name)
                    ValueRow(title: "Sistema", value: me.device.osVersion)
                    ValueRow(title: "Conectado em", value: Format.dateTime(me.device.createdAt))
                }
            }

            Section {
                if devices.items.isEmpty {
                    if let error = devices.error {
                        Text(error.message).font(.subheadline).foregroundStyle(.secondary)
                    } else {
                        ProgressView().frame(maxWidth: .infinity)
                    }
                }
                ForEach(devices.items) { device in
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(device.name).font(.subheadline.weight(.semibold))
                                if device.current { StatusBadge(text: "Este aparelho", tone: .info) }
                            }
                            Text("\(device.osVersion) · app \(device.appVersion)")
                                .font(.caption).foregroundStyle(.secondary)
                            Text("Último uso: \(Format.relative(device.lastSeenAt))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Desconectar", role: .destructive) { deviceToRevoke = device }
                            .buttonStyle(.borderless)
                            .font(.subheadline)
                            .accessibilityLabel("Desconectar \(device.name)")
                    }
                }
            } header: {
                Text("Aparelhos conectados")
            } footer: {
                Text("Desconecte aparelhos que você não usa mais. Eles vão precisar entrar de novo.")
            }

            Section {
                Toggle("Abrir com \(app.biometryLabel)", isOn: Binding(
                    get: { app.biometricLockEnabled },
                    set: { value in Task { await app.setBiometricLock(value) } }))
                    .disabled(!app.biometrics.canAuthenticate || app.isDemo)
            } footer: {
                Text(app.isDemo ? "Na demonstração esta opção fica desligada."
                     : "Pede \(app.biometryLabel) ou o código do aparelho ao abrir o app e ao voltar depois de 5 minutos.")
            }

            Section {
                Button { open(app.links.privacyURL) } label: {
                    Label("Política de privacidade", systemImage: "hand.raised")
                }
                Button { open(app.links.supportURL) } label: {
                    Label("Suporte", systemImage: "questionmark.circle")
                }
                NavigationLink {
                    DeleteAccountView()
                } label: {
                    Label("Excluir minha conta", systemImage: "person.crop.circle.badge.minus")
                        .foregroundStyle(.red)
                }
            }

            Section {
                if app.isDemo {
                    Button("Sair da demonstração", role: .destructive) { app.exitDemo() }
                        .frame(maxWidth: .infinity)
                } else {
                    Button("Sair", role: .destructive) { confirmSignOut = true }
                        .frame(maxWidth: .infinity)
                }
            } footer: {
                Text("PDV2 Gestão · versão \(app.config.versionText) · © 2026 CrownTech")
                    .frame(maxWidth: .infinity)
                    .padding(.top, 8)
            }
        }
        .navigationTitle("Conta")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await devices.reload(app: app, fetch: { _ in try await app.api.devices() }) }
        .task { await devices.ensureLoaded(key: "devices", app: app, fetch: { _ in try await app.api.devices() }) }
        .confirmationDialog("Desconectar este aparelho?", isPresented: Binding(
            get: { deviceToRevoke != nil }, set: { if !$0 { deviceToRevoke = nil } }),
                            titleVisibility: .visible, presenting: deviceToRevoke) { device in
            Button("Desconectar", role: .destructive) { Task { await revoke(device) } }
        } message: { device in
            Text(device.current ? "Você vai sair do app neste aparelho." : "\(device.name) vai precisar entrar de novo.")
        }
        .confirmationDialog("Sair do PDV2 Gestão?", isPresented: $confirmSignOut, titleVisibility: .visible) {
            Button("Sair", role: .destructive) { Task { await app.signOut() } }
        }
        .alert("Não foi possível concluir", isPresented: Binding(
            get: { actionError != nil }, set: { if !$0 { actionError = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(actionError ?? "")
        }
    }

    private func open(_ value: String) {
        if let url = URL(string: value) { openURL(url) }
    }

    private func revoke(_ device: AppDeviceInfo) async {
        do {
            try await app.api.revokeDevice(id: device.id)
            if device.current {
                await app.signOut()
            } else {
                devices.remove(id: device.id)
            }
        } catch {
            actionError = app.handle(error)?.message
        }
    }
}

#Preview {
    NavigationStack { AccountView() }.environmentObject(AppState.preview())
}
