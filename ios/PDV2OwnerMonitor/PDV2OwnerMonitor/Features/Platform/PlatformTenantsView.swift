import SwiftUI

/// Empresas com saúde (terminais sem contato, conflitos, assinatura).
struct PlatformTenantsView: View {
    private struct Key: Hashable { let query: String; let attention: Bool }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<PlatformTenant>()
    @State private var attentionOnly = false
    @State private var selected: PlatformTenant?
    let query: String

    var body: some View {
        PagedListView(state: state, key: Key(query: query, attention: attentionOnly),
                      empty: EmptyContent(title: attentionOnly ? "Tudo em dia" : "Nenhuma empresa",
                                          message: attentionOnly ? "Nenhuma empresa precisa de atenção agora."
                                              : "Nenhuma empresa encontrada.",
                                          icon: "building.2"),
                      fetch: { cursor in
                          try await app.api.platformTenants(query: query.isEmpty ? nil : query,
                                                            attentionOnly: attentionOnly, cursor: cursor)
                      },
                      header: {
                          Section {
                              Toggle("Precisam de atenção", isOn: $attentionOnly)
                          }
                      },
                      row: { tenant in
                          Button { selected = tenant } label: { PlatformTenantRow(tenant: tenant) }
                              .buttonStyle(.plain)
                      })
            .confirmationDialog(selected?.name ?? "", isPresented: Binding(
                get: { selected != nil }, set: { if !$0 { selected = nil } }),
                                titleVisibility: .visible, presenting: selected) { tenant in
                Button("Abrir empresa") { app.openPlatformTenant(id: tenant.id, name: tenant.name) }
            } message: { _ in
                Text("Mostra o Resumo, as vendas e os caixas desta empresa. A visita fica registrada.")
            }
    }
}

struct PlatformTenantRow: View {
    let tenant: PlatformTenant

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(tenant.name).font(.subheadline.weight(.semibold))
                Spacer()
                StatusBadge.health(tenant.health, text: tenant.healthText)
            }
            Text("Assinatura: \(tenant.subscriptionStatusText) · \(Format.plural(tenant.shopsCount, "loja", "lojas"))")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Text("Sem contato: \(tenant.terminalsOffline) de \(tenant.terminalsTotal)")
                Spacer()
                Text(Format.plural(tenant.conflictsOpen, "conflito", "conflitos"))
            }
            .font(.caption)
            .foregroundStyle(tenant.terminalsOffline + tenant.conflictsOpen > 0 ? .orange : .secondary)
            Text("Última venda: \(tenant.lastSaleAt == nil ? "nenhuma" : Format.relative(tenant.lastSaleAt))")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Toque para abrir a empresa")
    }
}

#Preview {
    NavigationStack { PlatformTenantsView(query: "") }.environmentObject(AppState.preview(platform: true))
}
