import SwiftUI

@main
struct PDV2GestaoApp: App {
    @StateObject private var app = PDV2GestaoApp.makeState()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
        }
    }

    @MainActor
    private static func makeState() -> AppState {
        #if DEBUG
        // Só em Debug: `-PDV2Demo` (empresa) ou `-PDV2DemoPlatform` abre com os dados
        // fictícios do contrato, sem servidor. Serve para conferir telas e gerar capturas.
        let arguments = ProcessInfo.processInfo.arguments
        if arguments.contains("-PDV2DemoPlatform") { return .preview(platform: true) }
        if arguments.contains("-PDV2Demo") { return .preview() }
        #endif
        return AppState()
    }
}
