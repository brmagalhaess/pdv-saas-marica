import XCTest

/// "Ver demonstração" na tela de entrada: faixa fixa, telas reais e saída sem deixar estado.
final class DemoModeUITests: XCTestCase {
    private let bannerText = "Demonstração · dados fictícios"

    override func setUp() {
        continueAfterFailure = false
    }

    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launch()
        return app
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

        // Abrir de novo: nada da demonstração ficou guardado.
        app.terminate()
        let again = launch()
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
}
