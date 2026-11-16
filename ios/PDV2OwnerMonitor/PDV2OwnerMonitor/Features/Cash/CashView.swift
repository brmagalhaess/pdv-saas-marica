import SwiftUI

struct CashRoute: Hashable { let id: String }

/// Aba Caixa: turnos abertos (agora) e fechados (no período).
struct CashView: View {
    enum Filter: String, CaseIterable, Identifiable {
        case open, closed
        var id: String { rawValue }
        var label: String { self == .open ? "Abertos" : "Fechados" }
    }

    private struct Key: Hashable { let tenantID: String; let shopID: String?; let period: Period?; let filter: Filter }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<CashSession>()
    @State private var filter: Filter = .open

    var body: some View {
        TenantGate(permission: .cash) { context in
            PagedListView(state: state,
                          key: Key(tenantID: context.tenantID, shopID: context.shopID,
                                   period: filter == .closed ? context.period : nil, filter: filter),
                          empty: EmptyContent(title: filter == .open ? "Nenhum caixa aberto" : "Nenhum caixa fechado",
                                              message: filter == .open ? "Quando um caixa abrir, ele aparece aqui."
                                                  : "Nenhum fechamento em \(context.period.label.lowercased()).",
                                              icon: "banknote"),
                          fetch: { cursor in try await app.api.cashSessions(context, status: filter.rawValue, cursor: cursor) },
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
                          row: { session in
                              NavigationLink(value: CashRoute(id: session.id)) { CashSessionRow(session: session) }
                          })
            .navigationDestination(for: CashRoute.self) { route in
                CashDetailView(sessionID: route.id, tenantID: context.tenantID)
            }
        }
        .contextBar(showsPeriod: filter == .closed)
        .navigationTitle("Caixa")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct CashSessionRow: View {
    let session: CashSession

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.subheadline.weight(.semibold))
                Spacer()
                if session.status == "closed" {
                    StatusBadge.cashDifference(session.differenceCents)
                } else {
                    StatusBadge(text: Labels.cashStatus(session.status), tone: .info)
                }
            }
            Text("\(session.shop.name)\(session.operatorName.isEmpty ? "" : " · " + session.operatorName)")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Text(session.closedAt != nil ? "Fechado em \(Format.dateTime(session.closedAt))"
                                         : "Aberto em \(Format.dateTime(session.openedAt))")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Text(Format.plural(session.salesCount, "venda", "vendas") + " · " + Format.money(session.salesTotalCents))
                Spacer()
                Text("Dinheiro: \(Format.money(session.cashNetCents))")
            }
            .font(.caption)
            .monospacedDigit()
            if session.incomplete {
                Text("Abertura não recebida").font(.caption).foregroundStyle(.orange)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private var title: String {
        [session.terminalName, session.stationName].filter { !$0.isEmpty }.joined(separator: " / ")
            .ifEmpty("Caixa")
    }
}

extension String {
    func ifEmpty(_ fallback: String) -> String { isEmpty ? fallback : self }
}

#Preview {
    NavigationStack { CashView() }.environmentObject(AppState.preview())
}
