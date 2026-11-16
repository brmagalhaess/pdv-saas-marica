import SwiftUI

/// Aba Vendas: lista do período com filtro e rolagem infinita.
struct SalesView: View {
    enum Filter: String, CaseIterable, Identifiable {
        case all, completed, cancelled
        var id: String { rawValue }
        var label: String {
            switch self {
            case .all: return "Todas"
            case .completed: return "Concluídas"
            case .cancelled: return "Canceladas"
            }
        }
    }

    private struct Key: Hashable { let context: QueryContext; let filter: Filter }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<SaleListItem>()
    @State private var filter: Filter = .all

    var body: some View {
        TenantGate(permission: .sales) { context in
            PagedListView(state: state, key: Key(context: context, filter: filter),
                          empty: EmptyContent(title: "Nenhuma venda",
                                              message: "Não há vendas \(filter == .cancelled ? "canceladas " : "")em \(context.period.label.lowercased()).",
                                              icon: "cart"),
                          fetch: { cursor in try await app.api.sales(context, status: filter.rawValue, cursor: cursor) },
                          header: {
                              Section {
                                  Picker("Situação", selection: $filter) {
                                      ForEach(Filter.allCases) { Text($0.label).tag($0) }
                                  }
                                  .pickerStyle(.segmented)
                              }
                              .listRowBackground(Color.clear)
                              .listRowInsets(EdgeInsets())
                          },
                          row: { sale in
                              NavigationLink(value: sale.id) { SaleRow(sale: sale, period: context.period) }
                          })
            .navigationDestination(for: String.self) { id in
                SaleDetailView(saleID: id, tenantID: context.tenantID)
            }
        }
        .contextBar(showsPeriod: true)
        .navigationTitle("Vendas")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct SaleRow: View {
    let sale: SaleListItem
    let period: Period

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(Format.saleMoment(sale.createdAt, period: period))
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                Spacer()
                Text(Format.money(sale.totalCents))
                    .font(.headline)
                    .monospacedDigit()
            }
            HStack {
                Text(placeText).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                StatusBadge.sale(sale.status)
            }
            if !sale.paymentMethods.isEmpty {
                Text(sale.paymentMethods.map { Labels.payment($0) }.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private var placeText: String {
        let terminal = [sale.terminalName, sale.stationName].filter { !$0.isEmpty }.joined(separator: " / ")
        return terminal.isEmpty ? sale.shop.name : "\(sale.shop.name) · \(terminal)"
    }
}

#Preview {
    NavigationStack { SalesView() }.environmentObject(AppState.preview())
}
