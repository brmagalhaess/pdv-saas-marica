import SwiftUI

/// Conflitos de sincronização (só leitura; resolve-se no painel web).
struct ConflictsView: View {
    enum Filter: String, CaseIterable, Identifiable {
        case open, all
        var id: String { rawValue }
        var label: String { self == .open ? "Em aberto" : "Todos" }
    }

    private struct Key: Hashable { let tenantID: String; let shopID: String?; let filter: Filter }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<SyncConflict>()
    @State private var filter: Filter = .open

    var body: some View {
        TenantGate(permission: .terminals) { context in
            PagedListView(state: state, key: Key(tenantID: context.tenantID, shopID: context.shopID, filter: filter),
                          empty: EmptyContent(title: "Nenhum conflito", message: "Os caixas estão sincronizados.",
                                              icon: "checkmark.seal"),
                          footnote: "Abra o painel web para resolver.",
                          fetch: { cursor in try await app.api.conflicts(context, status: filter.rawValue, cursor: cursor) },
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
                          row: { ConflictRow(conflict: $0) })
        }
        .contextBar(showsPeriod: false)
        .navigationTitle("Conflitos")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct ConflictRow: View {
    let conflict: SyncConflict

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(conflict.reasonText).font(.subheadline.weight(.semibold))
                Spacer()
                StatusBadge(text: conflict.statusText,
                            tone: conflict.status == "open" ? .warning : conflict.status == "resolved" ? .positive : .neutral)
            }
            Text([conflict.shop.name, conflict.terminalName].filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(Format.dateTime(conflict.createdAt)).font(.caption).foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

#Preview {
    NavigationStack { ConflictsView() }.environmentObject(AppState.preview())
}
