import XCTest

/// "Ver demonstração" na tela de entrada: faixa fixa, telas reais e saída sem deixar estado.
final class DemoModeUITests: XCTestCase {
    private let bannerText = "Demonstração · dados fictícios"

    override func setUp() {
        continueAfterFailure = false
    }

    /// Abre o app sem depender do simulador: sessão só em memória (ignora um login real
    /// guardado no Keychain) e preferências próprias, apagadas quando `reset` é verdadeiro.
    private func launch(reset: Bool = true, extra: [String] = []) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-PDV2CleanLaunch"] + (reset ? ["-PDV2ResetDefaults"] : []) + extra
        app.launch()
        return app
    }

    private func assertBannerVisible(_ app: XCUIApplication, _ screen: String, file: StaticString = #filePath, line: UInt = #line) {
        let banner = app.staticTexts[bannerText]
        XCTAssertTrue(banner.waitForExistence(timeout: 5), "Faixa em \(screen)", file: file, line: line)
        XCTAssertTrue(banner.isHittable, "Faixa visível (não coberta) em \(screen)", file: file, line: line)
        XCTAssertTrue(app.buttons["sairDemonstracao"].isHittable, "Saída à mão em \(screen)", file: file, line: line)
    }

    func testDemoShowsBannerOnEveryTabAndExitReturnsToLogin() {
        let app = launch()
        let demoButton = app.buttons["verDemonstracao"]
        XCTAssertTrue(demoButton.waitForExistence(timeout: 15), "Botão \"Ver demonstração\" na tela de entrada")
        XCTAssertFalse(app.staticTexts[bannerText].exists, "Sem faixa fora da demonstração")

        demoButton.tap()

        let banner = app.staticTexts[bannerText]
        XCTAssertTrue(banner.waitForExistence(timeout: 10), "Faixa da demonstração")
        XCTAssertTrue(app.buttons["sairDemonstracao"].exists)
        XCTAssertTrue(app.staticTexts["Mercadinho Exemplo"].waitForExistence(timeout: 10)
                      || app.buttons["Empresa: Mercadinho Exemplo"].exists)

        let tabs = app.tabBars.firstMatch
        for tab in ["Vendas", "Caixa", "Estoque", "Mais", "Resumo"] {
            tabs.buttons[tab].tap()
            XCTAssertTrue(banner.waitForExistence(timeout: 5), "Faixa fixa na aba \(tab)")
        }

        app.buttons["sairDemonstracao"].tap()

        XCTAssertTrue(app.buttons["verDemonstracao"].waitForExistence(timeout: 10), "Volta à tela de entrada")
        XCTAssertFalse(app.staticTexts[bannerText].exists)
        XCTAssertFalse(app.tabBars.firstMatch.exists)

        // Abrir de novo (sem apagar as preferências): nada da demonstração ficou guardado.
        app.terminate()
        let again = launch(reset: false)
        XCTAssertTrue(again.buttons["verDemonstracao"].waitForExistence(timeout: 15))
        XCTAssertFalse(again.staticTexts[bannerText].exists)
    }

    func testAccountInDemoOffersOnlyLeavingTheDemo() {
        let app = launch()
        XCTAssertTrue(app.buttons["verDemonstracao"].waitForExistence(timeout: 15))
        app.buttons["verDemonstracao"].tap()
        XCTAssertTrue(app.staticTexts[bannerText].waitForExistence(timeout: 10))

        app.tabBars.firstMatch.buttons["Mais"].tap()
        app.buttons["Conta"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Ana Exemplo"].waitForExistence(timeout: 10))
        // O botão "Sair" da Conta vira "Sair da demonstração" (o da faixa continua no alto).
        let leave = app.collectionViews.buttons["Sair da demonstração"].firstMatch
        if !leave.exists { app.collectionViews.firstMatch.swipeUp() }
        XCTAssertTrue(leave.waitForExistence(timeout: 5))
        leave.tap()
        XCTAssertTrue(app.buttons["verDemonstracao"].waitForExistence(timeout: 10))
    }

    /// A faixa continua visível nas telas empilhadas (detalhes, Fiado, Conta).
    func testBannerStaysOnStackedScreens() {
        // Relógio fixo (sexta, 18h30) para haver vendas e caixas abertos a qualquer hora.
        let app = launch(extra: ["-PDV2Demo", "-PDV2DemoNow", "2026-11-13T18:30:00-03:00"])
        assertBannerVisible(app, "Resumo")
        let tabs = app.tabBars.firstMatch

        tabs.buttons["Vendas"].tap()
        let sale = app.collectionViews.buttons.matching(NSPredicate(format: "label CONTAINS 'R$'")).firstMatch
        XCTAssertTrue(sale.waitForExistence(timeout: 10), "Lista de vendas")
        sale.tap()
        XCTAssertTrue(app.navigationBars["Venda"].waitForExistence(timeout: 10), "Detalhe da venda")
        assertBannerVisible(app, "detalhe da venda")

        tabs.buttons["Caixa"].tap()
        let session = app.collectionViews.buttons.matching(NSPredicate(format: "label CONTAINS 'Caixa 1'")).firstMatch
        XCTAssertTrue(session.waitForExistence(timeout: 10), "Lista de caixas")
        session.tap()
        XCTAssertTrue(app.navigationBars["Caixa"].buttons.firstMatch.waitForExistence(timeout: 10), "Detalhe do caixa")
        assertBannerVisible(app, "detalhe do caixa")

        tabs.buttons["Mais"].tap()
        app.buttons["Fiado"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts["João da Silva"].waitForExistence(timeout: 10), "Fiado")
        assertBannerVisible(app, "Fiado")
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["Conta"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Ana Exemplo"].waitForExistence(timeout: 10), "Conta")
        assertBannerVisible(app, "Conta")
    }
}
