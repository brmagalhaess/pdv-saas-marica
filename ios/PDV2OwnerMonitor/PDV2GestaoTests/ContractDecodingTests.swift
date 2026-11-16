import XCTest
@testable import PDV2Gestao

/// Cada exemplo JSON do contrato precisa decodificar nos DTOs do app.
final class ContractDecodingTests: XCTestCase {
    func testLoginOkAndMe() throws {
        let response = try decodeFixture(LoginResponse.self, "login_ok")
        XCTAssertEqual(response.status, "ok")
        XCTAssertEqual(response.tokens?.accessExpiresIn, 900)
        XCTAssertTrue(response.tokens?.accessToken.hasPrefix("pa_") ?? false)
        XCTAssertTrue(response.tokens?.refreshToken.hasPrefix("pr_") ?? false)
        let me = try XCTUnwrap(response.me)
        XCTAssertEqual(me.user.id, 12)
        XCTAssertNil(me.platform)
        XCTAssertFalse(me.hasPlatform)
        XCTAssertTrue(me.device.current)
        XCTAssertEqual(me.device.osVersion, "iOS 26.1")
        XCTAssertEqual(me.tenants.first?.shops.first?.name, "Loja Centro")
        XCTAssertNil(me.tenants.first?.accessExpiresOn)
        XCTAssertEqual(me.links.accountDeletionURL, "https://pdv2.crowntech.com.br/excluir-conta/")
    }

    func testMeWithPlatform() throws {
        let me = try decodeFixture(Me.self, "me_platform")
        XCTAssertTrue(me.hasPlatform)
        XCTAssertEqual(me.platform?.canManageAccess, true)
        XCTAssertEqual(me.platform?.roles.count, 3)
        XCTAssertTrue(me.tenants.isEmpty)
        let mfa = try decodeFixture(LoginResponse.self, "mfa_ok")
        XCTAssertEqual(mfa.me?.platform?.superuser, true)
    }

    func testLoginMFARequired() throws {
        let response = try decodeFixture(LoginResponse.self, "login_mfa")
        XCTAssertEqual(response.status, "mfa_required")
        XCTAssertNil(response.tokens)
        XCTAssertTrue(response.mfaToken?.hasPrefix("pm_") ?? false)
        XCTAssertEqual(response.user?.email, "admin@crowntech.com.br")
    }

    func testRefreshAndOk() throws {
        XCTAssertEqual(try decodeFixture(RefreshResponse.self, "refresh").tokens.accessExpiresIn, 900)
        XCTAssertTrue(try decodeFixture(OkResponse.self, "ok").ok)
    }

    func testTenants() throws {
        let page = try decodeFixture(Page<TenantAccess>.self, "tenants")
        XCTAssertNil(page.nextCursor)
        XCTAssertEqual(page.results.first?.permissions, ["sales", "cash", "stock", "credit", "terminals"])
        XCTAssertEqual(page.results.first?.has(.credit), true)
    }

    func testDashboardFull() throws {
        let dashboard = try decodeFixture(Dashboard.self, "dashboard")
        XCTAssertNil(dashboard.shop)
        XCTAssertEqual(dashboard.shops.count, 2)
        XCTAssertEqual(dashboard.period.key, "today")
        XCTAssertEqual(dashboard.period.dateTo, "2026-11-16")
        let sales = try XCTUnwrap(dashboard.sales)
        let payments = try XCTUnwrap(dashboard.payments)
        XCTAssertEqual(sales.revenueCents, 352000)
        XCTAssertEqual(sales.netRevenueCents, sales.revenueCents - sales.returnsCents)
        // Invariante do contrato: faturamento = recebido + fiado + crédito da loja.
        XCTAssertEqual(sales.revenueCents,
                       payments.receivedTotalCents + payments.creditAccountCents + payments.storeCreditCents)
        XCTAssertEqual(payments.receivedTotalCents,
                       payments.cashCents + payments.pixCents + payments.cardCreditCents
                       + payments.cardDebitCents + payments.otherCents)
        XCTAssertEqual(dashboard.series?.kind, "hour")
        XCTAssertEqual(dashboard.series?.points.last?.label, "14h")
        XCTAssertEqual(dashboard.cash?.shortageCents, -1500)
        XCTAssertEqual(dashboard.stock?.belowMinimumCount, 7)
        XCTAssertEqual(dashboard.credit?.overdueCustomers, 4)
        XCTAssertEqual(dashboard.terminals?.onlineWindowSeconds, 300)
    }

    func testDashboardSectionsWithoutPermissionAreNull() throws {
        let dashboard = try decodeFixture(Dashboard.self, "dashboard_restricted")
        XCTAssertEqual(dashboard.shop?.name, "Loja Centro")
        XCTAssertEqual(dashboard.series?.kind, "day")
        XCTAssertEqual(dashboard.series?.points.first?.label, "10/11")
        XCTAssertNil(dashboard.cash)
        XCTAssertNil(dashboard.stock)
        XCTAssertNil(dashboard.credit)
        XCTAssertNil(dashboard.terminals)
    }

    func testSalesAndDetail() throws {
        let page = try decodeFixture(Page<SaleListItem>.self, "sales")
        let sale = try XCTUnwrap(page.results.first)
        XCTAssertEqual(sale.totalCents, 4590)
        XCTAssertEqual(sale.paymentMethods, ["pix"])
        XCTAssertEqual(sale.stationName, "")

        let detail = try decodeFixture(SaleDetail.self, "sale_detail")
        XCTAssertEqual(detail.items.count, 2)
        XCTAssertEqual(detail.items[1].quantityMilli, 2000)
        XCTAssertEqual(detail.items.map(\.lineTotalCents).reduce(0, +), detail.totalCents)
        XCTAssertEqual(detail.grossCents - detail.promoDiscountCents - detail.discountCents, detail.totalCents)
        XCTAssertEqual(detail.payments.first?.label, "Pix")
        XCTAssertNil(detail.cancelledAt)
        XCTAssertTrue(detail.returns.isEmpty)

        let refunded = try decodeFixture(SaleDetail.self, "sale_detail_refunded")
        XCTAssertEqual(refunded.returns.first?.refundLabel, "Dinheiro")
        XCTAssertNotNil(refunded.cancelledAt)
    }

    func testCashSessions() throws {
        let open = try decodeFixture(Page<CashSession>.self, "cash_sessions").results[0]
        XCTAssertEqual(open.status, "open")
        XCTAssertNil(open.closedAt)
        XCTAssertNil(open.differenceCents)
        XCTAssertFalse(open.incomplete)

        let closed = try decodeFixture(Page<CashSession>.self, "cash_sessions_closed").results[0]
        XCTAssertEqual(closed.differenceCents, -1500)

        let detail = try decodeFixture(CashSessionDetail.self, "cash_session_detail")
        XCTAssertEqual(detail.session.id, open.id)
        XCTAssertEqual(detail.byMethod.map(\.method), ["cash", "pix"])
        XCTAssertEqual(detail.movements.first?.label, "Sangria")
        XCTAssertEqual(detail.creditReceivedCents, 0)
    }

    func testStockCreditTerminalsConflicts() throws {
        let stock = try decodeFixture(StockAlertsPage.self, "stock_alerts")
        XCTAssertEqual(stock.counts, StockCounts(zeroed: 3, belowMinimum: 7))
        XCTAssertEqual(stock.results.first?.minimumMilli, 12000)
        XCTAssertEqual(stock.results.first?.status, "zeroed")

        let credit = try decodeFixture(CreditPage.self, "credit")
        XCTAssertEqual(credit.totals.customers, 12)
        XCTAssertEqual(credit.results.first?.oldestDueDate, "2026-10-20")

        let terminals = try decodeFixture(TerminalsPage.self, "terminals")
        XCTAssertEqual(terminals.onlineWindowSeconds, 300)
        XCTAssertEqual(terminals.results.first?.status, "online")
        XCTAssertNil(terminals.nextCursor)

        let conflicts = try decodeFixture(Page<SyncConflict>.self, "conflicts")
        XCTAssertEqual(conflicts.results.first?.reasonText, "Estoque negativo")
        XCTAssertEqual(conflicts.results.first?.statusText, "Em aberto")
    }

    func testAccountObjects() throws {
        XCTAssertEqual(try decodeFixture(Page<AppDeviceInfo>.self, "devices").results.first?.current, true)
        let deletion = try decodeFixture(DeletionResponse.self, "deletion_request")
        XCTAssertEqual(deletion.status, "received")
        XCTAssertTrue(deletion.message.contains("15 dias"))
    }

    func testPlatformObjects() throws {
        let tenants = try decodeFixture(Page<PlatformTenant>.self, "platform_tenants")
        XCTAssertEqual(tenants.nextCursor, "50")
        XCTAssertEqual(tenants.results.first?.health, "attention")
        XCTAssertEqual(tenants.results.first?.subscriptionStatusText, "Ativa")

        let access = try decodeFixture(Page<AppAccessItem>.self, "platform_access")
        XCTAssertEqual(access.results.count, 2)
        XCTAssertEqual(access.results[0].tenant?.name, "Mercado Bom Preço")
        XCTAssertNil(access.results[0].revokedBy)
        XCTAssertNil(access.results[1].tenant)
        XCTAssertTrue(access.results[1].allShops)
        XCTAssertEqual(access.results[1].expiresOn, "2026-12-31")
        XCTAssertEqual(try decodeFixture(AppAccessItem.self, "access").devicesActive, 1)

        let options = try decodeFixture(AccessOptions.self, "access_options")
        XCTAssertEqual(options.members.first?.roleText, "Proprietário")
        let platformOptions = try decodeFixture(AccessOptions.self, "access_options_platform")
        XCTAssertNil(platformOptions.tenant)
        XCTAssertEqual(platformOptions.members.first?.role, "platform")

        let devices = try decodeFixture(Page<PlatformDevice>.self, "platform_devices")
        XCTAssertEqual(devices.results.first?.lastIP, "177.10.20.30")
        XCTAssertEqual(try decodeFixture(PlatformDevice.self, "platform_device").user.id, 12)
    }

    func testErrorBodies() throws {
        let rate = try decodeFixture(APIErrorBody.self, "error_rate_limited")
        XCTAssertEqual(rate.code, "rate_limited")
        XCTAssertEqual(rate.retryAfterSeconds, 600)
        XCTAssertEqual(try decodeFixture(APIErrorBody.self, "error_access_exists").code, "access_exists")
    }

    func testUnknownKeysAreIgnored() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture("terminals")) as? [String: Any])
        object["novo_campo_do_servidor"] = ["qualquer": 1]
        let page = try JSONDecoder().decode(TerminalsPage.self, from: json(object))
        XCTAssertEqual(page.results.count, 1)
    }

    // MARK: Corpos enviados

    func testLoginRequestMatchesContract() throws {
        let body = LoginRequest(email: "ana@mercadobompreco.com.br", password: "Senha-forte-1", device: testDevice)
        XCTAssertEqual(try jsonObject(JSONEncoder().encode(body)), try jsonObject(fixture("login_request")))
    }

    func testAccessGrantRequestsMatchContract() throws {
        let tenant = AccessGrantRequest(scope: "tenant", userID: 12, tenantID: "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10",
                                        shopIDs: [], permissions: ["sales", "cash", "stock", "credit", "terminals"],
                                        expiresOn: nil, reason: "Dona da empresa pediu acesso ao app")
        XCTAssertEqual(try jsonObject(JSONEncoder().encode(tenant)), try jsonObject(fixture("access_grant_request")))

        let platform = AccessGrantRequest(scope: "platform", userID: 7, tenantID: "ignorado", shopIDs: ["x"],
                                          permissions: ["sales"], expiresOn: nil, reason: "Atendimento")
        XCTAssertEqual(try jsonObject(JSONEncoder().encode(platform)),
                       try jsonObject(fixture("access_grant_request_platform")))
    }

    func testMFAAndOptionalBodiesSendOnlyAllowedKeys() throws {
        let code = try jsonObject(JSONEncoder().encode(MFARequest(mfaToken: "pm_x", code: "123456", recoveryCode: nil)))
        XCTAssertEqual(code, ["mfa_token": "pm_x", "code": "123456"] as NSDictionary)
        let recovery = try jsonObject(JSONEncoder().encode(MFARequest(mfaToken: "pm_x", code: nil, recoveryCode: "PDV2-ABCD-EFGH")))
        XCTAssertEqual(recovery, ["mfa_token": "pm_x", "recovery_code": "PDV2-ABCD-EFGH"] as NSDictionary)
        XCTAssertEqual(try jsonObject(JSONEncoder().encode(DeletionRequest(note: nil))), [:] as NSDictionary)
        XCTAssertEqual(try jsonObject(JSONEncoder().encode(LogoutRequest(refreshToken: nil))), [:] as NSDictionary)
    }

    func testAccessGrantFormBuildsRequest() {
        var form = AccessGrantForm()
        XCTAssertEqual(form.problem, "Escolha a empresa.")
        form.tenantID = "t1"
        form.userID = 12
        form.reason = "ok"
        XCTAssertEqual(form.problem, ReasonRule.message)
        form.reason = "  Dona pediu  "
        form.allShops = false
        XCTAssertEqual(form.problem, "Escolha ao menos uma loja.")
        form.shopIDs = ["s2", "s1"]
        form.permissions = ["stock", "sales"]
        let request = form.request()
        XCTAssertEqual(request?.shopIDs, ["s1", "s2"])
        XCTAssertEqual(request?.permissions, ["sales", "stock"])
        XCTAssertEqual(request?.reason, "Dona pediu")
        XCTAssertNil(request?.expiresOn)
    }

    func testMFAInputValidation() {
        XCTAssertTrue(MFAInput.isValidCode("123456"))
        XCTAssertFalse(MFAInput.isValidCode("12345"))
        XCTAssertEqual(MFAInput.normalizedRecovery("pdv2-abcd-efgh"), "PDV2-ABCD-EFGH")
        XCTAssertEqual(MFAInput.normalizedRecovery("PDV2ABCDEFGH"), "PDV2-ABCD-EFGH")
        XCTAssertNil(MFAInput.normalizedRecovery("ABCD-EFGH"))
    }
}
