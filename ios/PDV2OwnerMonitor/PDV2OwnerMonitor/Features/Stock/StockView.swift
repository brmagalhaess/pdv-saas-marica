import SwiftUI

/// Estoque: produtos zerados e abaixo do mínimo.
struct StockView: View {
    enum Filter: String, CaseIterable, Identifiable {
        case zeroed, belowMinimum = "below_minimum", all
        var id: String { rawValue }
        var label: String {
            switch self {
            case .zeroed: return "Zerados"
            case .belowMinimum: return "Abaixo do mínimo"
            case .all: return "Todos"
            }
        }
    }

    private struct Key: Hashable { let tenantID: String; let shopID: String?; let filter: Filter }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<StockAlert>()
    @State private var filter: Filter = .zeroed
    /// Contagens da mesma resposta da lista: zeradas ao trocar empresa/loja/filtro.
    private var counts: StockCounts? { state.summary(as: StockCounts.self) }

    var body: some View {
        TenantGate(permission: .stock) { context in
            PagedListView(state: state, key: Key(tenantID: context.tenantID, shopID: context.shopID, filter: filter),
                          empty: EmptyContent(title: "Nada por aqui",
                                              message: filter == .zeroed ? "Nenhum produto zerado."
                                                  : filter == .belowMinimum ? "Nenhum produto abaixo do mínimo."
                                                  : "Nenhum produto com alerta de estoque.",
                                              icon: "shippingbox"),
                          fetch: { cursor in
                              let page = try await app.api.stockAlerts(context, status: filter.rawValue, cursor: cursor)
                              return Page(results: page.results, nextCursor: page.nextCursor, summary: page.counts)
                          },
                          header: {
                              Section {
                                  Picker("Situação", selection: $filter) {
                                      ForEach(Filter.allCases) { Text(label(for: $0)).tag($0) }
                                  }
                                  .pickerStyle(.segmented)
                              }
                              .listRowBackground(Color.clear)
                              .listRowInsets(EdgeInsets())
                          },
                          row: { StockAlertRow(alert: $0) })
        }
        .contextBar(showsPeriod: false)
        .navigationTitle("Estoque")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func label(for filter: Filter) -> String {
        guard let counts else { return filter.label }
        switch filter {
        case .zeroed: return "Zerados (\(counts.zeroed))"
        case .belowMinimum: return "Mínimo (\(counts.belowMinimum))"
        case .all: return "Todos"
        }
    }
}

struct StockAlertRow: View {
    let alert: StockAlert

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(alert.name).font(.subheadline.weight(.semibold))
                Spacer()
                StatusBadge(text: Labels.stockStatus(alert.status), tone: alert.status == "zeroed" ? .negative : .warning)
            }
            Text([alert.sku, alert.shop.name].filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("\(Format.quantity(milli: alert.quantityMilli, unit: alert.unit)) / mínimo \(Format.quantity(milli: alert.minimumMilli, unit: alert.unit))")
                .font(.subheadline)
                .monospacedDigit()
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

#Preview {
    NavigationStack { StockView() }.environmentObject(AppState.preview())
}
