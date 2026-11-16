package br.com.crowntech.pdv2gestao

import br.com.crowntech.pdv2gestao.core.api.ApiClient
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.AppApi
import br.com.crowntech.pdv2gestao.core.api.DataScope
import br.com.crowntech.pdv2gestao.core.api.GestaoApi
import br.com.crowntech.pdv2gestao.core.demo.DemoApi
import br.com.crowntech.pdv2gestao.core.demo.DemoData
import br.com.crowntech.pdv2gestao.core.demo.DemoMode
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.core.session.SessionManager
import br.com.crowntech.pdv2gestao.core.session.SessionStore
import br.com.crowntech.pdv2gestao.core.session.StoredSession
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respondError
import io.ktor.http.HttpStatusCode
import kotlinx.coroutines.test.runTest
import kotlinx.datetime.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger

/** Modo demonstração: sem rede, sem gravar nada, dados coerentes entre telas. */
class DemoModeTest {
    /** Segunda-feira, 16/11/2026, 17:40 em São Paulo. */
    private val fixedNow = Instant.parse("2026-11-16T17:40:00-03:00")

    private class CountingStore : SessionStore {
        val writes = AtomicInteger()
        override suspend fun load(): StoredSession? = null
        override suspend fun save(session: StoredSession) { writes.incrementAndGet() }
        override suspend fun clear() { writes.incrementAndGet() }
    }

    private class Real {
        val requests = AtomicInteger()
        val store = CountingStore()
        val client = ApiClient(MockEngine { requests.incrementAndGet(); respondError(HttpStatusCode.ServiceUnavailable) })
        val session = SessionManager(client, store).also { it.useBaseUrl("https://pdv2.crowntech.com.br") }
        val api = AppApi(client, session)
    }

    private fun demoMode(real: Real) = DemoMode(real.api) { DemoApi(now = { fixedNow }, latencyMillis = 0) }

    /** Abre todas as telas de dados, em todas as lojas e períodos, como o app faria. */
    private suspend fun visitEverything(api: GestaoApi) {
        val me = api.me()
        api.tenants(null, null)
        api.devices()
        for (tenant in me.tenants) {
            for (shop in listOf<String?>(null) + tenant.shops.map { it.id }) {
                for (period in PeriodKey.entries) {
                    val scope = DataScope(tenant.id, shop, period.key)
                    api.dashboard(scope)
                    for (status in listOf("all", "completed", "cancelled")) {
                        val page = api.sales(scope, status, null)
                        page.results.firstOrNull()?.let { api.sale(tenant.id, it.id) }
                    }
                    for (status in listOf("open", "closed")) {
                        val page = api.cashSessions(scope, status, null)
                        page.results.firstOrNull()?.let { api.cashSession(tenant.id, it.id) }
                    }
                    for (status in listOf("zeroed", "below_minimum", "all")) api.stockAlerts(scope, status, null)
                    for (status in listOf("open", "overdue")) api.credit(scope, status, null)
                    api.terminals(scope)
                    for (status in listOf("open", "all")) api.conflicts(scope, status, null)
                }
            }
        }
    }

    @Test fun enteringDemoMakesNoNetworkCallAndStoresNothing() = runTest {
        val real = Real()
        val mode = demoMode(real)
        mode.enter()
        assertTrue(mode.active.value)
        visitEverything(mode.api)
        assertEquals("nenhuma requisição HTTP no modo demonstração", 0, real.requests.get())
        assertEquals("nada gravado no armazenamento seguro", 0, real.store.writes.get())
        assertFalse(real.session.hasSession)
    }

    @Test fun exitingDemoGoesBackToRealApiAndDropsDemoData() = runTest {
        val real = Real()
        val mode = demoMode(real)
        val demo = mode.enter()
        assertSame(demo, mode.api)
        mode.exit()
        assertFalse(mode.active.value)
        assertSame(real.api, mode.api)
        // De volta à API real: sem sessão, ela recusa (a demonstração não deixou login para trás).
        val e = runCatching { mode.api.me() }.exceptionOrNull()
        assertEquals("refresh_invalid", (e as ApiException).code)
        // Entrar de novo cria outra instância (nada da demonstração anterior sobra).
        val again = mode.enter()
        assertTrue(again !== demo)
    }

    @Test fun demoNumbersAreConsistentAcrossScreens() = runTest {
        val api = DemoApi(now = { fixedNow }, latencyMillis = 0)
        val tenant = DemoData.TENANT_MERCADINHO
        for (period in PeriodKey.entries) {
            val scope = DataScope(tenant, null, period.key)
            val d = api.dashboard(scope)
            val sales = d.sales!!
            assertEquals("Mercadinho Exemplo", d.tenant.name)
            assertEquals(2, d.shops.size)
            assertTrue("há vendas em ${period.label}", sales.count > 0)

            // Lista de Vendas (todas as páginas) bate com o Resumo.
            val rows = buildList {
                var cursor: String? = null
                do {
                    val page = api.sales(scope, "all", cursor)
                    addAll(page.results)
                    cursor = page.nextCursor
                } while (cursor != null)
            }
            val valid = rows.filter { it.status != "cancelled" }
            assertEquals(sales.count, valid.size.toLong())
            assertEquals(sales.revenueCents, valid.sumOf { it.totalCents })
            assertEquals(sales.cancelledCount, rows.count { it.status == "cancelled" }.toLong())
            val p = d.payments!!
            assertEquals(p.receivedTotalCents, p.cashCents + p.pixCents + p.cardCreditCents + p.cardDebitCents + p.otherCents)
            assertEquals(sales.revenueCents, p.receivedTotalCents + p.creditAccountCents + p.storeCreditCents)
            assertEquals(sales.revenueCents, d.series!!.points.sumOf { it.revenueCents })

            // Loja a loja soma o total da empresa.
            val byShop = d.shops.sumOf { api.dashboard(DataScope(tenant, it.id, period.key)).sales!!.revenueCents }
            assertEquals(sales.revenueCents, byShop)
        }
        // Detalhe da venda: itens e pagamentos fecham com o total.
        val first = api.sales(DataScope(tenant, null, "today"), "completed", null).results.first()
        val detail = api.sale(tenant, first.id)
        assertEquals(first.totalCents, detail.totalCents)
        assertEquals(detail.totalCents, detail.items.sumOf { it.lineTotalCents } - detail.discountCents)
        assertEquals(detail.totalCents, detail.payments.sumOf { it.amountCents })

        // Hoje: caixas abertos e sem venda no futuro.
        val today = api.dashboard(DataScope(tenant, null, "today"))
        assertEquals(3L, today.cash!!.openCount)
        assertTrue(api.sales(DataScope(tenant, null, "today"), "all", null).results.all { Instant.parse(it.createdAt!!) <= fixedNow })
        assertEquals(3L, today.terminals!!.online)
        assertEquals(4L, today.terminals!!.total)
        assertTrue(today.stock!!.zeroedCount > 0)
        assertTrue(today.credit!!.overdueCents > 0)
        assertNotNull(api.cashSession(tenant, api.cashSessions(DataScope(tenant, null, "7d"), "closed", null).results.first().id).byMethod)
    }

    @Test fun demoRefusesForeignTenantOrShop() = runTest {
        val api = DemoApi(now = { fixedNow }, latencyMillis = 0)
        val e = runCatching { api.dashboard(DataScope(DemoData.TENANT_MERCADINHO, DemoData.SHOP_EMPORIO, "today")) }.exceptionOrNull()
        assertEquals("shop_forbidden", (e as ApiException).code)
        val e2 = runCatching { api.dashboard(DataScope("outra", null, "today")) }.exceptionOrNull()
        assertEquals("tenant_forbidden", (e2 as ApiException).code)
    }
}
