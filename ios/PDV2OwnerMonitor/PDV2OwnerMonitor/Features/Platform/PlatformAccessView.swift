import SwiftUI

/// Validação do motivo exigido pelo servidor (3–240 caracteres).
enum ReasonRule {
    static func validated(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return (3...240).contains(trimmed.count) ? trimmed : nil
    }

    static let message = "Escreva um motivo com 3 a 240 caracteres."
}

/// Acessos ao app: listar e revogar (superusuário).
struct PlatformAccessView: View {
    enum Filter: String, CaseIterable, Identifiable {
        case active, all
        var id: String { rawValue }
        var label: String { self == .active ? "Ativos" : "Todos" }
    }

    private struct Key: Hashable { let query: String; let filter: Filter; let token: Int }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<AppAccessItem>()
    @State private var filter: Filter = .active
    @State private var toRevoke: AppAccessItem?
    @State private var askReason = false
    @State private var actionError: String?
    let query: String
    let reloadToken: Int

    var body: some View {
        PagedListView(state: state, key: Key(query: query, filter: filter, token: reloadToken),
                      empty: EmptyContent(title: "Nenhum acesso", message: "Toque em + para liberar o app para alguém.",
                                          icon: "person.badge.key"),
                      fetch: { cursor in
                          try await app.api.platformAccess(status: filter.rawValue, query: query.isEmpty ? nil : query,
                                                           cursor: cursor)
                      },
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
                      row: { access in
                          AccessRow(access: access)
                              .swipeActions {
                                  if access.status == "active" {
                                      Button("Revogar", role: .destructive) { toRevoke = access; askReason = true }
                                  }
                              }
                              .contextMenu {
                                  if access.status == "active" {
                                      Button("Revogar", systemImage: "xmark.circle", role: .destructive) {
                                          toRevoke = access
                                          askReason = true
                                      }
                                  }
                              }
                      })
            .reasonPrompt(isPresented: $askReason, title: "Revogar acesso",
                          message: "\(toRevoke?.user.name ?? "A pessoa") deixa de ver \(toRevoke?.tenant?.name ?? "a plataforma") no app na hora. Informe o motivo.",
                          actionTitle: "Revogar") { reason in
                guard let access = toRevoke else { return }
                Task { await revoke(access, reason: reason) }
            }
            .alert("Não foi possível concluir", isPresented: Binding(
                get: { actionError != nil }, set: { if !$0 { actionError = nil } })) {
                Button("OK", role: .cancel) {}
            } message: { Text(actionError ?? "") }
    }

    private func revoke(_ access: AppAccessItem, reason: String) async {
        guard let reason = ReasonRule.validated(reason) else { actionError = ReasonRule.message; return }
        do {
            let updated = try await app.api.revokeAccess(id: access.id, reason: reason)
            if filter == .active { state.remove(id: updated.id) } else { state.replace(updated) }
        } catch {
            actionError = app.handle(error)?.message
        }
        toRevoke = nil
    }
}

struct AccessRow: View {
    let access: AppAccessItem

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(access.user.name).font(.subheadline.weight(.semibold))
                Spacer()
                StatusBadge(text: access.statusText,
                            tone: access.status == "active" ? .positive : access.status == "expired" ? .warning : .neutral)
            }
            Text(access.user.email).font(.caption).foregroundStyle(.secondary)
            Text(access.scope == "platform" ? "Equipe CrownTech" : (access.tenant?.name ?? "—"))
                .font(.subheadline)
            if access.scope == "tenant" {
                Text("Lojas: \(access.allShops ? "Todas" : access.shops.map(\.name).joined(separator: ", "))")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Permissões: \(Labels.permissions(access.permissions))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Text(access.expiresOn == nil ? "Sem prazo" : "Até \(Format.day(access.expiresOn))")
                Spacer()
                Text(Format.plural(access.devicesActive, "aparelho", "aparelhos"))
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            if access.status == "revoked" && !access.revokeReason.isEmpty {
                Text("Motivo da revogação: \(access.revokeReason)").font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

#Preview("Acessos") {
    NavigationStack { PlatformAccessView(query: "", reloadToken: 0) }.environmentObject(AppState.preview(platform: true))
}
