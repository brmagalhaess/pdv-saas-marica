import SwiftUI

/// Estado vazio, de erro ou sem internet, com "Tentar de novo".
struct StateMessageView: View {
    let error: ScreenError
    var retry: (() -> Void)?

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: icon)
        } description: {
            Text(error.message)
        } actions: {
            if let retry, error.kind != .signedOut, error.kind != .permission {
                Button("Tentar de novo", action: retry)
                    .buttonStyle(.borderedProminent)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var title: String {
        switch error.kind {
        case .offline: return "Sem conexão"
        case .permission: return "Sem permissão"
        case .signedOut: return "Sessão encerrada"
        case .general: return "Não foi possível carregar"
        }
    }

    private var icon: String {
        switch error.kind {
        case .offline: return "wifi.slash"
        case .permission: return "lock"
        case .signedOut: return "person.crop.circle.badge.exclamationmark"
        case .general: return "exclamationmark.triangle"
        }
    }
}

struct EmptyContent {
    let title: String
    let message: String
    let icon: String
}

struct EmptyStateView: View {
    let content: EmptyContent

    var body: some View {
        ContentUnavailableView(content.title, systemImage: content.icon, description: Text(content.message))
            .frame(maxWidth: .infinity)
    }
}

struct LoadingView: View {
    var body: some View {
        ProgressView("Carregando…")
            .frame(maxWidth: .infinity, minHeight: 160)
    }
}

/// "Não foi possível atualizar. Mostrando dados de HH:MM."
struct StaleBanner: View {
    let loadedAt: Date?
    let error: ScreenError

    var body: some View {
        Label {
            Text(text).font(.footnote)
        } icon: {
            Image(systemName: error.kind == .offline ? "wifi.slash" : "exclamationmark.triangle.fill")
        }
        .foregroundStyle(.orange)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var text: String {
        if let loadedAt { return "Não foi possível atualizar. Mostrando dados de \(Format.time(loadedAt))." }
        return error.message
    }
}

/// Rodapé "Atualizado há X", atualizado a cada 30 s.
struct UpdatedFooter: View {
    let date: Date?

    var body: some View {
        if let date {
            TimelineView(.periodic(from: .now, by: 30)) { context in
                Text(Format.updated(date, now: context.date))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
            }
        }
    }
}

/// Lista paginada com estados de carregamento, vazio, erro e sem internet.
/// Puxar para atualizar funciona em todos os estados.
struct PagedListView<Item: Identifiable, Header: View, Row: View>: View {
    @EnvironmentObject private var app: AppState
    @ObservedObject var state: PagedState<Item>
    let key: AnyHashable
    let empty: EmptyContent
    var footnote: String?
    let fetch: PagedState<Item>.Fetch
    @ViewBuilder var header: () -> Header
    @ViewBuilder var row: (Item) -> Row

    var body: some View {
        List {
            header()
            if let error = state.error, !state.items.isEmpty {
                Section { StaleBanner(loadedAt: state.loadedAt, error: error) }
            }
            if state.items.isEmpty {
                Section {
                    if let error = state.error {
                        StateMessageView(error: error) { Task { await state.reload(app: app, fetch: fetch) } }
                    } else if !state.hasLoaded || state.isLoading {
                        LoadingView()
                    } else {
                        EmptyStateView(content: empty)
                    }
                }
                .listRowBackground(Color.clear)
            } else {
                Section {
                    ForEach(state.items) { item in
                        row(item)
                            .onAppear {
                                if item.id == state.items.last?.id {
                                    Task { await state.loadMore(app: app, fetch: fetch) }
                                }
                            }
                    }
                    if state.isLoadingMore {
                        ProgressView().frame(maxWidth: .infinity)
                    }
                } footer: {
                    VStack(spacing: 6) {
                        if let footnote { Text(footnote).frame(maxWidth: .infinity) }
                        UpdatedFooter(date: state.loadedAt)
                    }
                    .padding(.top, 6)
                }
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { await state.reload(app: app, fetch: fetch) }
        .task(id: key) { await state.ensureLoaded(key: key, app: app, fetch: fetch) }
    }
}

/// Tela rolável que carrega um objeto (Resumo e detalhes).
struct LoadedScrollView<Value, Content: View>: View {
    @EnvironmentObject private var app: AppState
    @ObservedObject var state: LoadState<Value>
    let key: AnyHashable
    let load: () async throws -> Value
    @ViewBuilder var content: (Value) -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let value = state.value {
                    if let error = state.error { StaleBanner(loadedAt: state.loadedAt, error: error).card() }
                    content(value)
                    UpdatedFooter(date: state.loadedAt).padding(.top, 4)
                } else if let error = state.error {
                    StateMessageView(error: error) { Task { await state.reload(app: app, load) } }
                        .padding(.top, 40)
                } else {
                    LoadingView().padding(.top, 40)
                }
            }
            .padding(16)
        }
        .background(PDVTheme.background)
        .refreshable { await state.reload(app: app, load) }
        .task(id: key) { await state.ensureLoaded(key: key, app: app, load) }
    }
}

/// Exige empresa escolhida (e a permissão da aba) antes de mostrar o conteúdo.
struct TenantGate<Content: View>: View {
    @EnvironmentObject private var app: AppState
    let permission: AppPermission?
    @ViewBuilder var content: (QueryContext) -> Content

    var body: some View {
        if let context = app.queryContext {
            if let permission, !app.can(permission) {
                ContentUnavailableView("Sem permissão", systemImage: "lock",
                                       description: Text(APIErrorCatalog.permissionMessage))
            } else {
                content(context)
            }
        } else if app.isPlatform {
            ContentUnavailableView {
                Label("Nenhuma empresa escolhida", systemImage: "building.2")
            } description: {
                Text("Escolha uma empresa na aba Plataforma.")
            } actions: {
                Button("Ir para Plataforma") { app.selectedTab = .platform }
                    .buttonStyle(.borderedProminent)
            }
        } else {
            ContentUnavailableView {
                Label("Escolha uma empresa", systemImage: "building.2")
            } actions: {
                Button("Escolher empresa") { app.showTenantPicker = true }
                    .buttonStyle(.borderedProminent)
            }
        }
    }
}

/// Barra de contexto: Empresa ▾ · Loja ▾ e, quando cabe, o período.
struct ContextBar: View {
    @EnvironmentObject private var app: AppState
    var showsPeriod: Bool

    var body: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                tenantButton
                shopMenu
            }
            if showsPeriod {
                Picker("Período", selection: $app.period) {
                    ForEach(Period.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private var tenantButton: some View {
        Button {
            app.showTenantPicker = true
        } label: {
            chip(text: app.tenant?.name ?? "Empresa", icon: "building.2", showsChevron: app.canSwitchTenant)
        }
        .buttonStyle(.plain)
        .allowsHitTesting(app.canSwitchTenant)
        .accessibilityLabel("Empresa: \(app.tenant?.name ?? "nenhuma")")
        .accessibilityHint(app.canSwitchTenant ? "Toque para trocar de empresa" : "")
    }

    private var shopMenu: some View {
        Menu {
            Button {
                app.shopID = nil
            } label: {
                if app.shopID == nil { Label("Todas as lojas", systemImage: "checkmark") } else { Text("Todas as lojas") }
            }
            ForEach(app.shops) { shop in
                Button {
                    app.shopID = shop.id
                } label: {
                    if app.shopID == shop.id { Label(shop.name, systemImage: "checkmark") } else { Text(shop.name) }
                }
            }
        } label: {
            chip(text: app.selectedShop?.name ?? "Todas as lojas", icon: "storefront", showsChevron: true)
        }
        .accessibilityLabel("Loja: \(app.selectedShop?.name ?? "Todas as lojas")")
        .accessibilityHint("Toque para escolher a loja")
    }

    private func chip(text: String, icon: String, showsChevron: Bool) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon).foregroundStyle(PDVTheme.blue)
            Text(text).lineLimit(1).foregroundStyle(.primary)
            if showsChevron { Image(systemName: "chevron.down").font(.caption2.weight(.bold)).foregroundStyle(.secondary) }
        }
        .font(.subheadline.weight(.medium))
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
        .background(PDVTheme.field, in: Capsule())
    }
}

extension View {
    /// Coloca a barra de contexto no topo da tela.
    func contextBar(showsPeriod: Bool) -> some View {
        safeAreaInset(edge: .top, spacing: 0) { ContextBar(showsPeriod: showsPeriod) }
    }
}

/// Pede um motivo (revogar acesso, desconectar aparelho).
struct ReasonPrompt: ViewModifier {
    @Binding var isPresented: Bool
    let title: String
    let message: String
    let actionTitle: String
    let onConfirm: (String) -> Void
    @State private var reason = ""

    func body(content: Content) -> some View {
        content.alert(title, isPresented: $isPresented) {
            TextField("Motivo", text: $reason)
            Button("Cancelar", role: .cancel) { reason = "" }
            Button(actionTitle, role: .destructive) {
                let value = reason.trimmingCharacters(in: .whitespacesAndNewlines)
                reason = ""
                onConfirm(value)
            }
        } message: {
            Text(message)
        }
    }
}

extension View {
    func reasonPrompt(isPresented: Binding<Bool>, title: String, message: String, actionTitle: String,
                      onConfirm: @escaping (String) -> Void) -> some View {
        modifier(ReasonPrompt(isPresented: isPresented, title: title, message: message,
                              actionTitle: actionTitle, onConfirm: onConfirm))
    }
}
