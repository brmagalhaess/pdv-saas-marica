package br.com.crowntech.pdv2gestao

import br.com.crowntech.pdv2gestao.core.model.AccessOptions
import br.com.crowntech.pdv2gestao.core.model.AppAccessObj
import br.com.crowntech.pdv2gestao.core.model.AppJson
import br.com.crowntech.pdv2gestao.core.model.CashSession
import br.com.crowntech.pdv2gestao.core.model.Conflict
import br.com.crowntech.pdv2gestao.core.model.CreditResponse
import br.com.crowntech.pdv2gestao.core.model.Dashboard
import br.com.crowntech.pdv2gestao.core.model.DeletionRequestBody
import br.com.crowntech.pdv2gestao.core.model.DeletionResponse
import br.com.crowntech.pdv2gestao.core.model.Device
import br.com.crowntech.pdv2gestao.core.model.DeviceInfo
import br.com.crowntech.pdv2gestao.core.model.GrantRequest
import br.com.crowntech.pdv2gestao.core.model.LoginRequest
import br.com.crowntech.pdv2gestao.core.model.LoginResponse
import br.com.crowntech.pdv2gestao.core.model.LogoutRequest
import br.com.crowntech.pdv2gestao.core.model.Me
import br.com.crowntech.pdv2gestao.core.model.MfaRequest
import br.com.crowntech.pdv2gestao.core.model.Page
import br.com.crowntech.pdv2gestao.core.model.PlatformDevice
import br.com.crowntech.pdv2gestao.core.model.PlatformTenant
import br.com.crowntech.pdv2gestao.core.model.RefreshResponse
import br.com.crowntech.pdv2gestao.core.model.SaleDetail
import br.com.crowntech.pdv2gestao.core.model.SaleRow
import br.com.crowntech.pdv2gestao.core.model.StockAlertsResponse
import br.com.crowntech.pdv2gestao.core.model.TenantAccess
import br.com.crowntech.pdv2gestao.core.model.TerminalsResponse
import br.com.crowntech.pdv2gestao.core.model.TokenPair
import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Os exemplos JSON do contrato (§6) são as fixtures; todos precisam decodificar. */
class ContractDecodingTest {

    private fun fixture(name: String): String =
        requireNotNull(javaClass.classLoader?.getResource("fixtures/$name")) { "fixture $name ausente" }.readText()

    private fun <T> decode(name: String, serializer: KSerializer<T>): T = AppJson.decodeFromString(serializer, fixture(name))

    private fun encodeObj(json: String): JsonObject = Json.parseToJsonElement(json).jsonObject

    @Test fun tokenPair() {
        val t = decode("token_pair.json", TokenPair.serializer())
        assertEquals("pa_Q2x...", t.accessToken)
        assertEquals(900L, t.accessExpiresIn)
        assertEquals("pr_Zm9...", t.refreshToken)
        assertEquals("2027-01-15T14:03:12-03:00", t.refreshExpiresAt)
    }

    @Test fun device() {
        val d = decode("device.json", Device.serializer())
        assertEquals("d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90", d.id)
        assertEquals("iOS 26.1", d.osVersion)
        assertTrue(d.current)
    }

    @Test fun tenantAccess() {
        val t = decode("tenant_access.json", TenantAccess.serializer())
        assertEquals("Mercado Bom Preço", t.name)
        assertFalse(t.allShops)
        assertEquals(1, t.shops.size)
        assertEquals(listOf("sales", "cash", "stock", "credit", "terminals"), t.permissions)
        assertNull(t.accessExpiresOn)
    }

    @Test fun meCompany() {
        val me = decode("me.json", Me.serializer())
        assertEquals(12L, me.user.id)
        assertNull(me.platform)
        assertFalse(me.isPlatform)
        assertEquals(1, me.tenants.size)
        assertEquals("https://pdv2.crowntech.com.br/excluir-conta/", me.links.accountDeletionUrl)
        assertEquals("d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90", me.device?.id)
    }

    @Test fun mePlatform() {
        val me = decode("me_platform.json", Me.serializer())
        assertTrue(me.isPlatform)
        assertTrue(me.platform!!.canManageAccess)
        assertTrue(me.tenants.isEmpty())
        assertEquals(3, me.platform!!.roles.size)
    }

    @Test fun loginOk() {
        val r = decode("login_ok.json", LoginResponse.serializer())
        assertEquals("ok", r.status)
        assertEquals("pa_Q2x...", r.tokens?.accessToken)
        assertEquals("Ana Souza", r.me?.user?.name)
    }

    @Test fun loginMfaRequired() {
        val r = decode("login_mfa.json", LoginResponse.serializer())
        assertEquals("mfa_required", r.status)
        assertEquals("pm_aGVs...", r.mfaToken)
        assertNull(r.tokens)
        assertEquals("admin@crowntech.com.br", r.user?.email)
    }

    @Test fun mfaOk() {
        val r = decode("mfa_ok.json", LoginResponse.serializer())
        assertTrue(r.me!!.isPlatform)
    }

    @Test fun refresh() {
        val r = decode("refresh.json", RefreshResponse.serializer())
        assertEquals("pr_Zm9...", r.tokens.refreshToken)
    }

    @Test fun tenantsAndDevicesPages() {
        val t = decode("tenants.json", Page.serializer(TenantAccess.serializer()))
        assertEquals(1, t.results.size)
        assertNull(t.nextCursor)
        val d = decode("devices.json", Page.serializer(Device.serializer()))
        assertTrue(d.results.single().current)
    }

    @Test fun dashboardFullAndInvariant() {
        val d = decode("dashboard.json", Dashboard.serializer())
        assertNull(d.shop)
        assertEquals(2, d.shops.size)
        assertEquals("today", d.period.key)
        val s = d.sales!!
        val p = d.payments!!
        assertEquals(352000L, s.revenueCents)
        assertEquals(350800L, s.netRevenueCents)
        // Invariante do contrato: faturamento = recebido + fiado + crédito da loja.
        assertEquals(s.revenueCents, p.receivedTotalCents + p.creditAccountCents + p.storeCreditCents)
        assertEquals(p.receivedTotalCents, p.cashCents + p.pixCents + p.cardCreditCents + p.cardDebitCents + p.otherCents)
        assertEquals("hour", d.series!!.kind)
        assertEquals(-1500L, d.cash!!.shortageCents)
        assertEquals(7L, d.stock!!.belowMinimumCount)
        assertEquals(4L, d.credit!!.overdueCustomers)
        assertEquals(1L, d.terminals!!.conflictsOpen)
    }

    @Test fun dashboardSectionsWithoutPermissionAreNullAndUnknownKeysIgnored() {
        val d = decode("dashboard_no_cash.json", Dashboard.serializer())
        assertEquals("Loja Centro", d.shop?.name)
        assertNull(d.cash)
        assertNull(d.stock)
        assertNull(d.credit)
        assertNull(d.terminals)
        assertEquals("day", d.series!!.kind)
    }

    @Test fun sales() {
        val page = decode("sales.json", Page.serializer(SaleRow.serializer()))
        val s = page.results.single()
        assertEquals(4590L, s.totalCents)
        assertEquals(listOf("pix"), s.paymentMethods)
        assertEquals("Loja Centro", s.shop?.name)
    }

    @Test fun saleDetail() {
        val d = decode("sale_detail.json", SaleDetail.serializer())
        assertEquals(2, d.items.size)
        assertEquals(2000L, d.items[1].quantityMilli)
        assertEquals(110L, d.promoDiscountCents)
        assertEquals("Pix", d.payments.single().label)
        assertEquals("cash", d.returns.single().refundMethod)
        assertNull(d.cancelledAt)
        assertEquals(d.totalCents, d.items.sumOf { it.lineTotalCents })
    }

    @Test fun cashSessions() {
        val page = decode("cash_sessions.json", Page.serializer(CashSession.serializer()))
        val s = page.results.single()
        assertEquals("open", s.status)
        assertNull(s.closedAt)
        assertNull(s.differenceCents)
        assertFalse(s.incomplete)
        assertTrue(s.movements.isEmpty())
    }

    @Test fun cashSessionDetail() {
        val s = decode("cash_session_detail.json", CashSession.serializer())
        assertEquals(-1500L, s.differenceCents)
        assertEquals(2, s.byMethod.size)
        assertEquals("withdrawn", s.movements.single().kind)
    }

    @Test fun stockAlerts() {
        val r = decode("stock_alerts.json", StockAlertsResponse.serializer())
        assertEquals(3L, r.counts.zeroed)
        assertEquals(12000L, r.results.single().minimumMilli)
    }

    @Test fun credit() {
        val r = decode("credit.json", CreditResponse.serializer())
        assertEquals(12L, r.totals.customers)
        assertEquals("2026-10-20", r.results.single().oldestDueDate)
    }

    @Test fun terminals() {
        val r = decode("terminals.json", TerminalsResponse.serializer())
        assertEquals(300L, r.onlineWindowSeconds)
        assertEquals("online", r.results.single().status)
    }

    @Test fun conflicts() {
        val r = decode("conflicts.json", Page.serializer(Conflict.serializer()))
        assertEquals("Estoque negativo", r.results.single().reasonText)
    }

    @Test fun deletion() {
        val r = decode("deletion.json", DeletionResponse.serializer())
        assertEquals("received", r.status)
        assertTrue(r.message.startsWith("Recebemos seu pedido"))
    }

    @Test fun platformTenants() {
        val r = decode("platform_tenants.json", Page.serializer(PlatformTenant.serializer()))
        assertEquals("50", r.nextCursor)
        assertEquals("attention", r.results.single().health)
    }

    @Test fun appAccess() {
        val a = decode("app_access.json", AppAccessObj.serializer())
        assertEquals("tenant", a.scope)
        assertEquals("Mercado Bom Preço", a.tenant?.name)
        assertNull(a.revokedBy)
        assertEquals(1L, a.devicesActive)
        val p = decode("app_access_platform.json", AppAccessObj.serializer())
        assertNull(p.tenant)
        assertEquals("2026-12-31", p.expiresOn)
        assertEquals("Bruno", p.revokedBy?.name)
    }

    @Test fun accessOptions() {
        val o = decode("access_options.json", AccessOptions.serializer())
        assertEquals("Proprietário", o.members.single().roleText)
        val p = decode("access_options_platform.json", AccessOptions.serializer())
        assertNull(p.tenant)
        assertEquals("platform", p.members.single().role)
    }

    @Test fun platformDevice() {
        val d = decode("platform_device.json", PlatformDevice.serializer())
        assertEquals("177.10.20.30", d.lastIp)
        assertEquals(12L, d.user?.id)
    }

    // ---------- Corpos enviados ----------

    @Test fun loginRequestHasExactKeys() {
        val body = AppJson.encodeToString(LoginRequest.serializer(), LoginRequest("ana@x.com", "s", DeviceInfo(
            platform = "android", name = "Samsung SM-S918B", model = "SM-S918B", osVersion = "Android 15", appVersion = "1.0.0 (1)",
        )))
        val obj = encodeObj(body)
        assertEquals(setOf("email", "password", "device"), obj.keys)
        assertEquals(setOf("platform", "name", "model", "os_version", "app_version"), obj["device"]!!.jsonObject.keys)
    }

    @Test fun mfaRequestSendsExactlyOneCode() {
        val withCode = encodeObj(AppJson.encodeToString(MfaRequest.serializer(), MfaRequest("pm_x", code = "123456")))
        assertEquals(setOf("mfa_token", "code"), withCode.keys)
        val withRecovery = encodeObj(AppJson.encodeToString(MfaRequest.serializer(), MfaRequest("pm_x", recoveryCode = "PDV2-ABCD-EFGH")))
        assertEquals(setOf("mfa_token", "recovery_code"), withRecovery.keys)
    }

    @Test fun grantRequestPlatformOmitsTenantFields() {
        val obj = encodeObj(AppJson.encodeToString(GrantRequest.serializer(), GrantRequest(scope = "platform", userId = 7, reason = "Atendimento")))
        assertEquals(setOf("scope", "user_id", "reason"), obj.keys)
        val tenant = encodeObj(AppJson.encodeToString(GrantRequest.serializer(), GrantRequest(
            scope = "tenant", userId = 12, tenantId = "t", shopIds = emptyList(),
            permissions = listOf("sales"), expiresOn = "2026-12-31", reason = "Dona pediu",
        )))
        assertEquals(setOf("scope", "user_id", "tenant_id", "shop_ids", "permissions", "expires_on", "reason"), tenant.keys)
    }

    @Test fun optionalBodiesBecomeEmptyObjects() {
        assertEquals("{}", AppJson.encodeToString(DeletionRequestBody.serializer(), DeletionRequestBody(null)))
        assertEquals("{}", AppJson.encodeToString(LogoutRequest.serializer(), LogoutRequest(null)))
    }
}
