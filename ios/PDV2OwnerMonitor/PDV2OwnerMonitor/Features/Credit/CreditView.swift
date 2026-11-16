import SwiftUI

/// Fiado por cliente: em aberto e vencido.
struct CreditView: View {
    enum Filter: String, CaseIterable, Identifiable {
        case open, overdue
        var id: String { rawValue }
        var label: String { self == .open ? "Em aberto" : "Vencido" }
    }

    private struct Key: Hashable { let tenantID: String; let shopID: String?; let filter: Filter }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<CreditCustomer>()
    @State private var filter: Filter = .open
    @State private var totals: CreditTotals?

    var body: some View {
        TenantGate(permission: .credit) { context in
            PagedListView(state: state, key: Key(tenantID: context.tenantID, shopID: context.shopID, filter: filter),
                          empty: EmptyContent(title: filter == .open ? "Nenhum fiado em aberto" : "Nenhum fiado vencido",
                                              message: "Quando houver vendas no fiado, os clientes aparecem aqui.",
                                              icon: "person.2"),
                          fetch: { cursor in
                              let page = try await app.api.credit(context, status: filter.rawValue, cursor: cursor)
                              totals = page.totals
                              return Page(results: page.results, nextCursor: page.nextCursor)
                          },
                          header: {
                              if let totals {
                                  Section {
                                      ValueRow(title: "Em aberto", value: Format.money(totals.openCents), emphasized: true)
                                      ValueRow(title: "Vencido", value: Format.money(totals.overdueCents))
                                      ValueRow(title: "Clientes", value: "\(totals.customers) (\(totals.overdueCustomers) com vencido)")
                                  }
                              }
                              Section {
                                  Picker("Situação", selection: $filter) {
                                      ForEach(Filter.allCases) { Text($0.label).tag($0) }
                                  }
                                  .pickerStyle(.segmented)
                              }
                              .listRowBackground(Color.clear)
                              .listRowInsets(EdgeInsets())
                          },
                          row: { CreditRow(customer: $0) })
        }
        .contextBar(showsPeriod: false)
        .navigationTitle("Fiado")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct CreditRow: View {
    let customer: CreditCustomer

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(customer.name).font(.subheadline.weight(.semibold))
                Spacer()
                Text(Format.money(customer.openCents)).font(.headline).monospacedDigit()
            }
            if !customer.phone.isEmpty {
                Text(customer.phone).font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Text(customer.overdueCents > 0 ? "Vencido: \(Format.money(customer.overdueCents))" : "Nada vencido")
                    .foregroundStyle(customer.overdueCents > 0 ? .red : .secondary)
                Spacer()
                if customer.oldestDueDate != nil {
                    Text("Desde \(Format.day(customer.oldestDueDate))").foregroundStyle(.secondary)
                }
            }
            .font(.caption)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

#Preview {
    NavigationStack { CreditView() }.environmentObject(AppState.preview())
}
