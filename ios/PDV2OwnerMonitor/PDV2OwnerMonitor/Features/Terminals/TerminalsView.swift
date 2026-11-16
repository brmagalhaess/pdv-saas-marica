import SwiftUI

/// Terminais (caixas) das lojas: online, sem contato e vendas na fila.
struct TerminalsView: View {
    private struct Key: Hashable { let tenantID: String; let shopID: String? }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<TerminalItem>()

    var body: some View {
        TenantGate(permission: .terminals) { context in
            PagedListView(state: state, key: Key(tenantID: context.tenantID, shopID: context.shopID),
                          empty: EmptyContent(title: "Nenhum terminal", message: "Nenhum caixa pareado nesta loja.",
                                              icon: "desktopcomputer"),
                          footnote: "Online = contato nos últimos 5 minutos.",
                          fetch: { _ in
                              let page = try await app.api.terminals(context)
                              return Page(results: page.results, nextCursor: nil)
                          },
                          header: { EmptyView() },
                          row: { TerminalRow(terminal: $0) })
        }
        .contextBar(showsPeriod: false)
        .navigationTitle("Terminais")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct TerminalRow: View {
    let terminal: TerminalItem

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(terminal.name).font(.subheadline.weight(.semibold))
                Spacer()
                StatusBadge.terminal(terminal.status)
            }
            Text(terminal.shop.name).font(.caption).foregroundStyle(.secondary)
            HStack {
                Text(terminal.version.isEmpty ? "Versão —" : "Versão \(terminal.version)")
                Spacer()
                Text("Último contato: \(Format.relative(terminal.lastSeenAt))")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            if terminal.pendingEvents > 0 {
                Text(Format.plural(terminal.pendingEvents, "venda na fila", "vendas na fila"))
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            if !terminal.error.isEmpty {
                Text(terminal.error).font(.caption).foregroundStyle(.red)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

#Preview {
    NavigationStack { TerminalsView() }.environmentObject(AppState.preview())
}
