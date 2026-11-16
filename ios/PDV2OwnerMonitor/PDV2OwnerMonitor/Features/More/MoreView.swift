import SwiftUI

/// Aba Mais: (Estoque, para plataforma) · Fiado · Terminais · Conflitos · Conta.
struct MoreView: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        NavigationStack(path: $app.morePath) {
            List {
                Section {
                    if app.isPlatform {
                        NavigationLink(value: AppState.MoreRoute.stock) {
                            Label("Estoque", systemImage: "shippingbox")
                        }
                    }
                    NavigationLink(value: AppState.MoreRoute.credit) {
                        Label("Fiado", systemImage: "person.2")
                    }
                    NavigationLink(value: AppState.MoreRoute.terminals) {
                        Label("Terminais", systemImage: "desktopcomputer")
                    }
                    NavigationLink(value: AppState.MoreRoute.conflicts) {
                        Label("Conflitos de sincronização", systemImage: "exclamationmark.arrow.triangle.2.circlepath")
                    }
                }
                Section {
                    NavigationLink(value: AppState.MoreRoute.account) {
                        Label("Conta", systemImage: "person.crop.circle")
                    }
                }
            }
            .navigationTitle("Mais")
            .navigationDestination(for: AppState.MoreRoute.self) { route in
                switch route {
                case .stock: StockView()
                case .credit: CreditView()
                case .terminals: TerminalsView()
                case .conflicts: ConflictsView()
                case .account: AccountView()
                }
            }
        }
    }
}

#Preview {
    MoreView().environmentObject(AppState.preview(platform: true))
}
