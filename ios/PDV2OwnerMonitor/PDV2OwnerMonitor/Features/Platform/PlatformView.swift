import SwiftUI

/// Aba Plataforma (equipe CrownTech): Empresas | Acessos | Aparelhos.
struct PlatformView: View {
    enum Segment: String, CaseIterable, Identifiable {
        case tenants, access, devices
        var id: String { rawValue }
        var label: String {
            switch self {
            case .tenants: return "Empresas"
            case .access: return "Acessos"
            case .devices: return "Aparelhos"
            }
        }
    }

    @EnvironmentObject private var app: AppState
    @State private var segment: Segment = .tenants
    @State private var searchText = ""
    @State private var query = ""
    @State private var showGrant = false
    @State private var accessReloadToken = 0

    private var segments: [Segment] {
        app.canManageAccess ? Segment.allCases : [.tenants]
    }

    var body: some View {
        Group {
            switch segment {
            case .tenants: PlatformTenantsView(query: query)
            case .access: PlatformAccessView(query: query, reloadToken: accessReloadToken)
            case .devices: PlatformDevicesView(query: query)
            }
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            if segments.count > 1 {
                Picker("Seção", selection: $segment) {
                    ForEach(segments) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(.bar)
            }
        }
        .searchable(text: $searchText, placement: .navigationBarDrawer(displayMode: .always), prompt: searchPrompt)
        .task(id: searchText) {
            // Espera a pessoa parar de digitar.
            try? await Task.sleep(nanoseconds: 400_000_000)
            if !Task.isCancelled { query = String(searchText.trimmingCharacters(in: .whitespaces).prefix(120)) }
        }
        .onChange(of: segment) { _, _ in searchText = ""; query = "" }
        .navigationTitle("Plataforma")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if segment == .access && app.canManageAccess {
                ToolbarItem(placement: .primaryAction) {
                    Button { showGrant = true } label: { Image(systemName: "plus") }
                        .accessibilityLabel("Liberar acesso")
                }
            }
        }
        .sheet(isPresented: $showGrant) {
            AccessGrantView { accessReloadToken += 1 }
        }
    }

    private var searchPrompt: String {
        switch segment {
        case .tenants: return "Buscar empresa"
        case .access, .devices: return "Buscar pessoa ou e-mail"
        }
    }
}

#Preview {
    NavigationStack { PlatformView() }.environmentObject(AppState.preview(platform: true))
}
