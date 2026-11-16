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
        // Só em Debug: `-PDV2Demo` abre direto na demonstração (a mesma do botão
        // "Ver demonstração"); `-PDV2DemoPlatform` usa as fixtures do contrato com o
        // perfil da equipe. Os argumentos abaixo escolhem a tela, para gerar capturas:
        //   -PDV2Tab resumo|vendas|caixa|estoque|mais   -PDV2More fiado|terminais|conflitos|conta
        //   -PDV2Period hoje|ontem|7d|mes   -PDV2DemoNow 2026-11-13T18:30:00-03:00 (relógio fixo)
        // Testes de interface: `-PDV2CleanLaunch` usa sessão só em memória (ignora o Keychain do
        // simulador) e preferências próprias; `-PDV2ResetDefaults` apaga essas preferências antes.
        let arguments = ProcessInfo.processInfo.arguments
        if arguments.contains("-PDV2DemoPlatform") { return .preview(platform: true) }
        let state = arguments.contains("-PDV2CleanLaunch")
            ? DebugLaunch.cleanState(reset: arguments.contains("-PDV2ResetDefaults"))
            : AppState()
        if arguments.contains("-PDV2Demo") {
            state.enterDemo(api: DemoGestaoAPI(now: DebugLaunch.demoClock(arguments)))
            DebugLaunch.apply(arguments, to: state)
        }
        return state
        #else
        return AppState()
        #endif
    }
}

#if DEBUG
/// Leitura dos argumentos de lançamento usados só nas capturas de tela.
enum DebugLaunch {
    static func value(_ name: String, in arguments: [String]) -> String? {
        rawValue(name, in: arguments)?.lowercased()
    }

    static func rawValue(_ name: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: name), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }

    /// Relógio da demonstração: fixo com `-PDV2DemoNow` (capturas), senão o do aparelho.
    static func demoClock(_ arguments: [String]) -> () -> Date {
        guard let text = rawValue("-PDV2DemoNow", in: arguments),
              let fixed = ISO8601DateFormatter().date(from: text) else { return Date.init }
        return { fixed }
    }

    /// Estado sem o Keychain do simulador e com preferências separadas, para os testes de interface.
    @MainActor
    static func cleanState(reset: Bool) -> AppState {
        let suite = "pdv2.uitests"
        if reset { UserDefaults().removePersistentDomain(forName: suite) }
        return AppState(defaults: UserDefaults(suiteName: suite) ?? .standard, store: InMemorySessionStore())
    }

    @MainActor
    static func apply(_ arguments: [String], to state: AppState) {
        switch value("-PDV2Period", in: arguments) {
        case "ontem": state.period = .yesterday
        case "7d": state.period = .sevenDays
        case "mes", "mês": state.period = .month
        default: break
        }
        switch value("-PDV2Tab", in: arguments) {
        case "vendas": state.selectedTab = .sales
        case "caixa": state.selectedTab = .cash
        case "estoque": state.selectedTab = .stock
        case "mais": state.selectedTab = .more
        default: state.selectedTab = .summary
        }
        switch value("-PDV2More", in: arguments) {
        case "fiado": state.goToMore(.credit)
        case "terminais": state.goToMore(.terminals)
        case "conflitos": state.goToMore(.conflicts)
        case "conta": state.goToMore(.account)
        default: break
        }
    }
}
#endif
