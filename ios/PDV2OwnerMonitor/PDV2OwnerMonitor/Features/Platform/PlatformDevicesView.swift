import SwiftUI

/// Aparelhos de todas as pessoas: listar e desconectar (superusuário).
struct PlatformDevicesView: View {
    enum Filter: String, CaseIterable, Identifiable {
        case active, all
        var id: String { rawValue }
        var label: String { self == .active ? "Ativos" : "Todos" }
    }

    private struct Key: Hashable { let query: String; let filter: Filter }

    @EnvironmentObject private var app: AppState
    @StateObject private var state = PagedState<PlatformDevice>()
    @State private var filter: Filter = .active
    @State private var toRevoke: PlatformDevice?
    @State private var askReason = false
    @State private var actionError: String?
    let query: String

    var body: some View {
        PagedListView(state: state, key: Key(query: query, filter: filter),
                      empty: EmptyContent(title: "Nenhum aparelho", message: "Ninguém entrou no app ainda.",
                                          icon: "iphone"),
                      fetch: { cursor in
                          try await app.api.platformDevices(status: filter.rawValue, query: query.isEmpty ? nil : query,
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
                      row: { device in
                          PlatformDeviceRow(device: device)
                              .swipeActions {
                                  if device.status == "active" {
                                      Button("Desconectar", role: .destructive) { toRevoke = device; askReason = true }
                                  }
                              }
                              .contextMenu {
                                  if device.status == "active" {
                                      Button("Desconectar", systemImage: "xmark.circle", role: .destructive) {
                                          toRevoke = device
                                          askReason = true
                                      }
                                  }
                              }
                      })
            .reasonPrompt(isPresented: $askReason, title: "Desconectar aparelho",
                          message: "\(toRevoke?.name ?? "O aparelho") de \(toRevoke?.user.name ?? "") vai precisar entrar de novo. Informe o motivo.",
                          actionTitle: "Desconectar") { reason in
                guard let device = toRevoke else { return }
                Task { await revoke(device, reason: reason) }
            }
            .alert("Não foi possível concluir", isPresented: Binding(
                get: { actionError != nil }, set: { if !$0 { actionError = nil } })) {
                Button("OK", role: .cancel) {}
            } message: { Text(actionError ?? "") }
    }

    private func revoke(_ device: PlatformDevice, reason: String) async {
        guard let reason = ReasonRule.validated(reason) else { actionError = ReasonRule.message; return }
        do {
            let updated = try await app.api.revokePlatformDevice(id: device.id, reason: reason)
            if filter == .active { state.remove(id: updated.id) } else { state.replace(updated) }
            if updated.id == app.me?.device.id { await app.signOut() }
        } catch {
            actionError = app.handle(error)?.message
        }
        toRevoke = nil
    }
}

struct PlatformDeviceRow: View {
    let device: PlatformDevice

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(device.user.name).font(.subheadline.weight(.semibold))
                Spacer()
                StatusBadge(text: device.status == "active" ? "Ativo" : "Desconectado",
                            tone: device.status == "active" ? .positive : .neutral)
            }
            Text("\(device.name) · \(device.osVersion)").font(.caption).foregroundStyle(.secondary)
            HStack {
                Text("App \(device.appVersion)")
                Spacer()
                Text("Último uso: \(Format.relative(device.lastSeenAt))")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

#Preview("Aparelhos") {
    NavigationStack { PlatformDevicesView(query: "") }.environmentObject(AppState.preview(platform: true))
}
