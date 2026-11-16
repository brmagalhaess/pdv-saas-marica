import SwiftUI

/// Regras locais do formulário "Liberar acesso" (o servidor valida de novo).
struct AccessGrantForm: Equatable {
    enum Scope: String, CaseIterable, Identifiable {
        case tenant, platform
        var id: String { rawValue }
        var label: String { self == .tenant ? "Empresa cliente" : "Equipe CrownTech" }
    }

    var scope: Scope = .tenant
    var tenantID: String?
    var userID: Int?
    var allShops = true
    var shopIDs: Set<String> = []
    var permissions: Set<String> = Set(AppPermission.allCases.map(\.rawValue))
    var expires = false
    var expiresOn: Date = Date()
    var reason = ""

    /// Mensagem do que falta, ou nil se dá para enviar.
    var problem: String? {
        if scope == .tenant && tenantID == nil { return "Escolha a empresa." }
        if userID == nil { return "Escolha a pessoa." }
        if scope == .tenant {
            if !allShops && shopIDs.isEmpty { return "Escolha ao menos uma loja." }
            if permissions.isEmpty { return "Escolha ao menos uma permissão." }
        }
        if ReasonRule.validated(reason) == nil { return ReasonRule.message }
        return nil
    }

    func request() -> AccessGrantRequest? {
        guard problem == nil, let userID, let reason = ReasonRule.validated(reason) else { return nil }
        let order = AppPermission.allCases.map(\.rawValue)
        return AccessGrantRequest(
            scope: scope.rawValue,
            userID: userID,
            tenantID: scope == .tenant ? tenantID : nil,
            shopIDs: scope == .tenant ? (allShops ? [] : shopIDs.sorted()) : nil,
            permissions: scope == .tenant ? order.filter { permissions.contains($0) } : nil,
            expiresOn: expires ? Format.ymd(expiresOn) : nil,
            reason: reason)
    }
}

/// "Liberar acesso": tipo, empresa, pessoa, lojas, permissões, validade e motivo.
struct AccessGrantView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss
    let onGranted: () -> Void

    @State private var form = AccessGrantForm()
    @State private var tenantName: String?
    @State private var options: AccessOptions?
    @State private var optionsError: String?
    @State private var loadingOptions = false
    @State private var sending = false
    @State private var submitError: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Tipo", selection: $form.scope) {
                        ForEach(AccessGrantForm.Scope.allCases) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.segmented)
                } footer: {
                    Text("O app é só de consulta. A pessoa entra com o mesmo e-mail e senha do painel.")
                }

                if form.scope == .tenant {
                    Section("Empresa") {
                        NavigationLink {
                            TenantSearchView { tenant in
                                form.tenantID = tenant.id
                                tenantName = tenant.name
                            }
                        } label: {
                            Text(tenantName ?? "Escolher empresa").foregroundStyle(tenantName == nil ? .secondary : .primary)
                        }
                    }
                }

                if form.scope == .platform || form.tenantID != nil {
                    peopleSection
                }

                if form.scope == .tenant, let options, !options.shops.isEmpty {
                    Section("Lojas") {
                        Toggle("Todas as lojas", isOn: $form.allShops)
                        if !form.allShops {
                            ForEach(options.shops) { shop in
                                Toggle(shop.name, isOn: Binding(
                                    get: { form.shopIDs.contains(shop.id) },
                                    set: { on in if on { form.shopIDs.insert(shop.id) } else { form.shopIDs.remove(shop.id) } }))
                            }
                        }
                    }
                }

                if form.scope == .tenant {
                    Section("Permissões") {
                        ForEach(AppPermission.allCases) { permission in
                            Toggle(permission.label, isOn: Binding(
                                get: { form.permissions.contains(permission.rawValue) },
                                set: { on in
                                    if on { form.permissions.insert(permission.rawValue) } else { form.permissions.remove(permission.rawValue) }
                                }))
                        }
                    }
                }

                Section {
                    Toggle("Definir validade", isOn: $form.expires)
                    if form.expires {
                        DatePicker("Válido até", selection: $form.expiresOn, in: Date()..., displayedComponents: .date)
                            .environment(\.locale, Format.locale)
                            .environment(\.timeZone, Format.timeZone)
                    }
                } header: {
                    Text("Validade")
                } footer: {
                    Text(form.expires ? "O acesso vale até o fim do dia escolhido." : "Sem prazo: vale até ser revogado.")
                }

                Section("Motivo") {
                    TextField("Ex.: dona da empresa pediu acesso", text: $form.reason, axis: .vertical)
                        .lineLimit(2...4)
                }

                if let submitError {
                    Section { Text(submitError).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Liberar acesso")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancelar") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if sending {
                        ProgressView()
                    } else {
                        Button("Liberar") { Task { await submit() } }
                            .disabled(form.problem != nil)
                    }
                }
            }
            .task(id: optionsKey) { await loadOptions() }
            .onChange(of: form.scope) { _, _ in
                form.userID = nil
                options = nil
            }
        }
    }

    private var optionsKey: String { form.scope == .platform ? "platform" : (form.tenantID ?? "") }

    @ViewBuilder
    private var peopleSection: some View {
        Section {
            if loadingOptions {
                ProgressView().frame(maxWidth: .infinity)
            } else if let optionsError {
                Text(optionsError).foregroundStyle(.secondary)
            } else if let options {
                if options.members.isEmpty {
                    Text(form.scope == .tenant ? "Essa empresa não tem pessoas na equipe. Cadastre no painel da empresa primeiro."
                                               : "Não há equipe CrownTech sem acesso. Dê um papel de plataforma no painel web primeiro.")
                        .foregroundStyle(.secondary)
                }
                ForEach(options.members) { member in
                    Button {
                        form.userID = member.userID
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(member.name).foregroundStyle(member.hasActiveAccess ? .secondary : .primary)
                                Text("\(member.email) · \(member.roleText)").font(.caption).foregroundStyle(.secondary)
                                if member.hasActiveAccess {
                                    Text("Já tem acesso").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            if form.userID == member.userID {
                                Image(systemName: "checkmark").foregroundStyle(PDVTheme.blue)
                            }
                        }
                    }
                    .disabled(member.hasActiveAccess)
                    .accessibilityAddTraits(form.userID == member.userID ? .isSelected : [])
                }
            }
        } header: {
            Text("Pessoa")
        }
    }

    private func loadOptions() async {
        guard form.scope == .platform || form.tenantID != nil else { return }
        loadingOptions = true
        optionsError = nil
        defer { loadingOptions = false }
        do {
            let result = try await app.api.accessOptions(tenantID: form.scope == .tenant ? form.tenantID : nil)
            options = result
            if let userID = form.userID, !result.members.contains(where: { $0.userID == userID && !$0.hasActiveAccess }) {
                form.userID = nil
            }
            form.shopIDs = form.shopIDs.filter { id in result.shops.contains { $0.id == id } }
        } catch is CancellationError {
        } catch {
            optionsError = app.handle(error)?.message
        }
    }

    private func submit() async {
        guard let request = form.request() else { submitError = form.problem; return }
        sending = true
        submitError = nil
        defer { sending = false }
        do {
            _ = try await app.api.grantAccess(request)
            onGranted()
            dismiss()
        } catch {
            submitError = app.handle(error)?.message
        }
    }
}

/// Busca de empresa (todas, para a plataforma).
struct TenantSearchView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss
    @StateObject private var state = PagedState<TenantAccess>()
    @State private var searchText = ""
    @State private var query = ""
    let onSelect: (TenantAccess) -> Void

    var body: some View {
        PagedListView(state: state, key: query,
                      empty: EmptyContent(title: "Nenhuma empresa", message: "Confira o nome digitado.", icon: "building.2"),
                      fetch: { cursor in try await app.api.tenants(query: query.isEmpty ? nil : query, cursor: cursor) },
                      header: { EmptyView() },
                      row: { tenant in
                          Button {
                              onSelect(tenant)
                              dismiss()
                          } label: {
                              Text(tenant.name).foregroundStyle(.primary)
                          }
                      })
            .searchable(text: $searchText, prompt: "Buscar empresa")
            .task(id: searchText) {
                try? await Task.sleep(nanoseconds: 400_000_000)
                if !Task.isCancelled { query = String(searchText.trimmingCharacters(in: .whitespaces).prefix(120)) }
            }
            .navigationTitle("Empresa")
            .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview {
    AccessGrantView {}.environmentObject(AppState.preview(platform: true))
}
