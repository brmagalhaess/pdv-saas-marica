import SwiftUI

/// Início com abas. Empresa: Resumo | Vendas | Caixa | Estoque | Mais.
/// Plataforma: Plataforma | Resumo | Vendas | Caixa | Mais (Estoque vai para Mais).
struct HomeView: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        TabView(selection: $app.selectedTab) {
            if app.isPlatform {
                NavigationStack { PlatformView() }
                    .tabItem { Label("Plataforma", systemImage: "building.2") }
                    .tag(AppState.Tab.platform)
            }
            NavigationStack { SummaryView() }
                .tabItem { Label("Resumo", systemImage: "chart.bar.xaxis") }
                .tag(AppState.Tab.summary)
            NavigationStack { SalesView() }
                .tabItem { Label("Vendas", systemImage: "cart") }
                .tag(AppState.Tab.sales)
            NavigationStack { CashView() }
                .tabItem { Label("Caixa", systemImage: "banknote") }
                .tag(AppState.Tab.cash)
            if !app.isPlatform {
                NavigationStack { StockView() }
                    .tabItem { Label("Estoque", systemImage: "shippingbox") }
                    .tag(AppState.Tab.stock)
            }
            MoreView()
                .tabItem { Label("Mais", systemImage: "ellipsis.circle") }
                .tag(AppState.Tab.more)
        }
        .sheet(isPresented: $app.showTenantPicker) {
            TenantPickerView()
        }
    }
}

/// Seletor de empresa. Plataforma busca em todas; empresa usa a lista do `/me`.
struct TenantPickerView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.dismiss) private var dismiss
    @StateObject private var state = PagedState<TenantAccess>()
    @State private var query = ""

    var body: some View {
        NavigationStack {
            Group {
                if app.isPlatform {
                    PagedListView(state: state, key: query,
                                  empty: EmptyContent(title: "Nenhuma empresa encontrada",
                                                      message: "Confira o nome digitado.", icon: "building.2"),
                                  fetch: { cursor in
                                      try await app.api.tenants(query: query.isEmpty ? nil : String(query.prefix(120)),
                                                                cursor: cursor)
                                  },
                                  header: { EmptyView() },
                                  row: row)
                    .searchable(text: $query, prompt: "Buscar empresa")
                } else {
                    List(app.me?.tenants ?? []) { row($0) }
                }
            }
            .navigationTitle("Empresa")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Fechar") { dismiss() } }
            }
        }
    }

    private func row(_ tenant: TenantAccess) -> some View {
        Button {
            app.selectTenant(tenant)
            dismiss()
        } label: {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(tenant.name).foregroundStyle(.primary)
                    Text(tenant.allShops ? "Todas as lojas" : Format.plural(tenant.shops.count, "loja liberada", "lojas liberadas"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if tenant.id == app.tenant?.id {
                    Image(systemName: "checkmark").foregroundStyle(PDVTheme.blue)
                        .accessibilityLabel("Empresa atual")
                }
            }
        }
    }
}

#Preview("Empresa") {
    HomeView().environmentObject(AppState.preview())
}

#Preview("Plataforma") {
    HomeView().environmentObject(AppState.preview(platform: true))
}
