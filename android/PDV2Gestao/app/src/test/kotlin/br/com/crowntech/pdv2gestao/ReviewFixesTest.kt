package br.com.crowntech.pdv2gestao

import br.com.crowntech.pdv2gestao.core.api.ApiClient
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.AppApi
import br.com.crowntech.pdv2gestao.core.api.DataScope
import br.com.crowntech.pdv2gestao.core.model.AppJson
import br.com.crowntech.pdv2gestao.core.model.DeviceInfo
import br.com.crowntech.pdv2gestao.core.model.Me
import br.com.crowntech.pdv2gestao.core.model.ShopRef
import br.com.crowntech.pdv2gestao.core.session.SessionEvent
import br.com.crowntech.pdv2gestao.core.session.SessionManager
import br.com.crowntech.pdv2gestao.core.session.SessionStore
import br.com.crowntech.pdv2gestao.core.session.StoredSession
import br.com.crowntech.pdv2gestao.feature.platform.validateGrant
import br.com.crowntech.pdv2gestao.ui.common.newItemsOnly
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.TextContent
import io.ktor.http.headersOf
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import kotlinx.datetime.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger

private class MemStore(var value: StoredSession? = null) : SessionStore {
    override suspend fun load() = value
    override suspend fun save(session: StoredSession) { value = session }
    override suspend fun clear() { value = null }
}

private const val URL = "https://pdv2.crowntech.com.br"

private fun fixtureText(name: String): String =
    requireNotNull(ReviewFixesTest::class.java.classLoader?.getResource("fixtures/$name")).readText()

private fun pair(access: String, refresh: String) =
    """{"tokens": {"access_token": "$access", "access_expires_at": "2026-11-16T14:18:12-03:00", "access_expires_in": 900,
        "refresh_token": "$refresh", "refresh_expires_at": "2027-01-15T14:03:12-03:00"}}"""

private fun MockRequestHandleScope.json(body: String, status: HttpStatusCode = HttpStatusCode.OK): HttpResponseData =
    respond(body, status, headersOf(HttpHeaders.ContentType, "application/json"))

class ReviewFixesTest {
    private val clock = Instant.parse("2026-11-16T14:03:12-03:00")

    private fun build(store: MemStore, handler: suspend MockRequestHandleScope.(HttpRequestData) -> HttpResponseData): Pair<SessionManager, AppApi> {
        val client = ApiClient(MockEngine { request -> handler(request) })
        val session = SessionManager(client, store) { clock }
        session.useBaseUrl(URL)
        return session to AppApi(client, session)
    }

    // ---- Achado 1: "carregar mais" com offset não pode repetir itens ----

    @Test fun loadMoreDropsItemsAlreadyOnScreen() {
        val firstPage = (1..50).map { "venda-$it" }
        // Entraram 2 vendas novas: o offset 50 devolve de novo a 49 e a 50.
        val secondPage = listOf("venda-49", "venda-50", "venda-51", "venda-52")
        val added = newItemsOnly(firstPage, secondPage) { it }
        assertEquals(listOf("venda-51", "venda-52"), added)
        val all = firstPage + added
        assertEquals(all.size, all.toSet().size)
    }

    @Test fun repeatedKeysInsideOnePageAreDroppedToo() {
        data class Row(val id: String, val v: Int)
        val added = newItemsOnly(emptyList(), listOf(Row("a", 1), Row("a", 2), Row("b", 3))) { it.id }
        assertEquals(listOf("a", "b"), added.map { it.id })
    }

    // ---- Achado 2: 403 app_access_denied em rota de dados ----

    @Test fun accessDeniedOnDataRouteClearsSessionAndShowsS3() = runTest {
        val store = MemStore(StoredSession("pr_old", "dev-1", URL))
        val (session, api) = build(store) { request ->
            if (request.url.encodedPath == "/api/v1/app/auth/refresh") json(pair("pa_1", "pr_1"))
            else json("""{"error": "Acesso ao app não liberado.", "code": "app_access_denied"}""", HttpStatusCode.Forbidden)
        }
        assertTrue(session.restore(URL))
        val event = async(start = CoroutineStart.UNDISPATCHED) { session.events.first() }
        try {
            api.dashboard(DataScope("t", null, "today"))
            fail("devia falhar")
        } catch (e: ApiException) {
            assertEquals("app_access_denied", e.code)
        }
        assertEquals(SessionEvent.AccessDenied, event.await())
        assertNull("O refresh revogado não pode ficar guardado", store.value)
        assertFalse(session.hasSession)
    }

    @Test fun accessDeniedOnMeClearsSession() = runTest {
        val store = MemStore(StoredSession("pr_old", "dev-1", URL))
        val (session, api) = build(store) { request ->
            if (request.url.encodedPath == "/api/v1/app/auth/refresh") json(pair("pa_1", "pr_1"))
            else json("""{"error": "Acesso ao app não liberado.", "code": "app_access_denied"}""", HttpStatusCode.Forbidden)
        }
        session.restore(URL)
        val event = async(start = CoroutineStart.UNDISPATCHED) { session.events.first() }
        try { api.me(); fail() } catch (e: ApiException) { assertEquals("app_access_denied", e.code) }
        assertEquals(SessionEvent.AccessDenied, event.await())
        assertNull(store.value)
    }

    // ---- Achado 3: rede ruim na abertura ----

    @Test fun refreshRetriesOnceAfterNetworkFailure() = runTest {
        val store = MemStore(StoredSession("pr_old", "dev-1", URL))
        val calls = AtomicInteger()
        val (session, _) = build(store) { request ->
            assertEquals("/api/v1/app/auth/refresh", request.url.encodedPath)
            // 1ª resposta se perde (timeout); a 2ª, poucos segundos depois, chega.
            if (calls.incrementAndGet() == 1) throw java.io.IOException("timeout")
            assertTrue((request.body as TextContent).text.contains("pr_old"))
            json(pair("pa_1", "pr_1"))
        }
        session.restore(URL)
        session.refreshNow()
        assertEquals(2, calls.get())
        assertEquals("pr_1", store.value!!.refreshToken)
    }

    @Test fun refreshDoesNotRetryOnSessionEnd() = runTest {
        val store = MemStore(StoredSession("pr_old", "dev-1", URL))
        val calls = AtomicInteger()
        val (session, _) = build(store) {
            calls.incrementAndGet()
            json("""{"error": "x", "code": "refresh_invalid"}""", HttpStatusCode.Unauthorized)
        }
        session.restore(URL)
        try { session.refreshNow(); fail() } catch (e: ApiException) { assertEquals("refresh_invalid", e.code) }
        assertEquals(1, calls.get())
    }

    @Test fun newLoginLogsOutOldStoredDevice() = runTest {
        val store = MemStore(StoredSession("pr_old", "dev-old", URL))
        val paths = mutableListOf<String>()
        var logoutBody = ""
        val (session, _) = build(store) { request ->
            paths += request.url.encodedPath
            when (request.url.encodedPath) {
                "/api/v1/app/auth/logout" -> {
                    logoutBody = (request.body as TextContent).text
                    json("""{"ok": true}""")
                }
                "/api/v1/app/auth/login" -> json(fixtureText("login_ok.json"))
                else -> error("rota inesperada")
            }
        }
        session.restore(URL)
        session.login("ana@mercadobompreco.com.br", "Senha-forte-1", DeviceInfo("android", "Pixel", "Pixel 8", "Android 15", "1.0.0 (1)"))
        assertEquals(listOf("/api/v1/app/auth/logout", "/api/v1/app/auth/login"), paths)
        assertTrue(logoutBody.contains("pr_old"))
        assertEquals("d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90", store.value!!.deviceId)
    }

    // ---- Achado 4: desmarcar "Todas as lojas" sem escolher loja ----

    @Test fun grantWithoutShopsWhenAllShopsUncheckedIsRefused() {
        assertNotNull(validateGrant("tenant", "t", 1, listOf("sales"), "Dona pediu", allShops = false, shopIds = emptyList()))
        assertNull(validateGrant("tenant", "t", 1, listOf("sales"), "Dona pediu", allShops = false, shopIds = listOf("s1")))
        assertNull(validateGrant("tenant", "t", 1, listOf("sales"), "Dona pediu", allShops = true, shopIds = emptyList()))
        assertNull(validateGrant("platform", null, 7, emptyList(), "Atendimento", allShops = false, shopIds = emptyList()))
    }

    // ---- Achado 5: plataforma reabrindo com loja salva e sem lista de lojas ----

    private fun platformMe(): Me = AppJson.decodeFromString(Me.serializer(), fixtureText("me_platform.json")).copy(tenants = emptyList())

    @Test fun platformRestoreWithoutShopListDropsSavedShop() {
        val restored = DataContext(tenantId = "tenant-x", tenantName = "Empresa X", shopId = "loja-centro")
        val me = platformMe()
        assertTrue(me.isPlatform)
        val ctx = contextAfterMe(restored, me)
        assertEquals("tenant-x", ctx.tenantId)
        assertNull("Sem a lista de lojas não dá para garantir o rótulo: volta para Todas", ctx.shopId)
        assertEquals("Todas as lojas", ctx.shopLabel)
        assertNull(ctx.scope()!!.shopId)
    }

    @Test fun platformKeepsShopWhenListIsKnown() {
        val current = DataContext(tenantId = "tenant-x", tenantName = "Empresa X", shopId = "s1", shops = listOf(ShopRef("s1", "Loja Centro")))
        val ctx = contextAfterMe(current, platformMe())
        assertEquals("s1", ctx.shopId)
        assertEquals("Loja Centro", ctx.shopLabel)
    }

    @Test fun shopLabelNeverSaysAllWhenFiltered() {
        assertEquals("Loja selecionada", DataContext(tenantId = "t", shopId = "s9").shopLabel)
        assertEquals("Todas as lojas", DataContext(tenantId = "t").shopLabel)
    }
}
