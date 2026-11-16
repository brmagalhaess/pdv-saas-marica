import XCTest
@testable import PDV2Gestao

final class FormatTests: XCTestCase {
    func testMoney() {
        XCTAssertEqual(Format.money(0), "R$ 0,00")
        XCTAssertEqual(Format.money(5), "R$ 0,05")
        XCTAssertEqual(Format.money(4590), "R$ 45,90")
        XCTAssertEqual(Format.money(123456), "R$ 1.234,56")
        XCTAssertEqual(Format.money(352000), "R$ 3.520,00")
        XCTAssertEqual(Format.money(123456789), "R$ 1.234.567,89")
        XCTAssertEqual(Format.money(-1500), "-R$ 15,00")
    }

    func testQuantity() {
        XCTAssertEqual(Format.quantity(milli: 12000, unit: "UN"), "12 un")
        XCTAssertEqual(Format.quantity(milli: 0, unit: "UN"), "0 un")
        XCTAssertEqual(Format.quantity(milli: 1_200_000, unit: "UN"), "1.200 un")
        XCTAssertEqual(Format.quantity(milli: 1250, unit: "KG"), "1,250 kg")
        XCTAssertEqual(Format.quantity(milli: 50, unit: "KG"), "0,050 kg")
        XCTAssertEqual(Format.quantity(milli: 2000, unit: "L"), "2,000 L")
        XCTAssertEqual(Format.quantity(milli: -500, unit: "KG"), "-0,500 kg")
    }

    func testDates() {
        XCTAssertEqual(Format.dateTime("2026-11-16T14:03:12-03:00"), "16/11/2026 14:03")
        XCTAssertEqual(Format.time("2026-11-16T14:03:12-03:00"), "14:03")
        // O mesmo instante em UTC continua mostrado no horário de São Paulo.
        XCTAssertEqual(Format.dateTime("2026-11-16T17:03:12Z"), "16/11/2026 14:03")
        XCTAssertEqual(Format.dateTime("2026-11-16T17:03:12.123456Z"), "16/11/2026 14:03")
        XCTAssertEqual(Format.dateTime(nil), "—")
        XCTAssertEqual(Format.dateTime("não é data"), "—")
        XCTAssertEqual(Format.day("2026-10-20"), "20/10/2026")
        XCTAssertEqual(Format.day(nil), "—")
        XCTAssertEqual(Format.saleMoment("2026-11-16T13:58:40-03:00", period: .today), "13:58")
        XCTAssertEqual(Format.saleMoment("2026-11-16T13:58:40-03:00", period: .month), "16/11/2026 13:58")
        let date = Format.parse("2026-11-30T12:00:00-03:00")!
        XCTAssertEqual(Format.ymd(date), "2026-11-30")
    }

    func testRelative() {
        let now = Format.parse("2026-11-16T14:03:12-03:00")!
        XCTAssertEqual(Format.relative(now.addingTimeInterval(-20), now: now), "agora mesmo")
        XCTAssertEqual(Format.relative(now.addingTimeInterval(-180), now: now), "há 3 min")
        XCTAssertEqual(Format.relative(now.addingTimeInterval(-7200), now: now), "há 2 h")
        XCTAssertEqual(Format.relative(now.addingTimeInterval(-86400), now: now), "há 1 dia")
        XCTAssertEqual(Format.relative(now.addingTimeInterval(-3 * 86400), now: now), "há 3 dias")
        XCTAssertEqual(Format.relative(nil as String?, now: now), "Sem contato")
        XCTAssertEqual(Format.updated(now.addingTimeInterval(-180), now: now), "Atualizado há 3 min")
        XCTAssertEqual(Format.updated(now, now: now), "Atualizado agora mesmo")
    }

    func testCashDifference() {
        XCTAssertEqual(Format.cashDifference(-1500), "Faltou R$ 15,00")
        XCTAssertEqual(Format.cashDifference(200), "Sobrou R$ 2,00")
        XCTAssertEqual(Format.cashDifference(0), "Sem diferença")
        XCTAssertEqual(Format.cashDifference(nil), "—")
    }

    func testLabels() {
        XCTAssertEqual(Labels.payment("card_credit"), "Cartão de crédito")
        XCTAssertEqual(Labels.payment("credit_account"), "Fiado")
        XCTAssertEqual(Labels.payment("other"), "Outra forma")
        XCTAssertEqual(Labels.payment("other", name: "Vale-refeição"), "Vale-refeição")
        XCTAssertEqual(Labels.saleStatus("refunded"), "Estornada")
        XCTAssertEqual(Labels.refund("account"), "Abatido no fiado")
        XCTAssertEqual(Labels.terminalStatus("offline"), "Sem contato")
        XCTAssertEqual(Labels.movement("supplied"), "Suprimento")
        XCTAssertEqual(Labels.permission("terminals"), "Terminais e conflitos")
        XCTAssertEqual(Labels.permissions(["sales", "cash", "stock", "credit", "terminals"]), "Todas")
        XCTAssertEqual(Labels.permissions(["sales", "stock"]), "Vendas, Estoque")
        XCTAssertEqual(Period.allCases.map(\.label), ["Hoje", "Ontem", "7 dias", "Mês"])
        XCTAssertEqual(Period.sevenDays.rawValue, "7d")
    }
}
