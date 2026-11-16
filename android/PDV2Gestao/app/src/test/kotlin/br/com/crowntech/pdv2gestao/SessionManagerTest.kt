package br.com.crowntech.pdv2gestao

import br.com.crowntech.pdv2gestao.core.api.ApiClient
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.AppApi
import br.com.crowntech.pdv2gestao.core.model.DeviceInfo
import br.com.crowntech.pdv2gestao.core.session.SessionEvent
import br.com.crowntech.pdv2gestao.core.session.SessionManager
import br.com.crowntech.pdv2gestao.core.session.SessionStore
import br.com.crowntech.pdv2gestao.core.session.StoredSession
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
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import kotlinx.datetime.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger
import kotlin.time.Duration.Companion.seconds

private class MemoryStore(var value: StoredSession? = null) : SessionStore {
    override suspend fun load() = value
    override suspend fun save(session: StoredSession) { value = session }
    override suspend fun clear() { value = null }
}

private const val BASE = "https://pdv2.crowntech.com.br"

private fun fixture(name: String): String =
    requireNotNull(SessionManagerTest::class.java.classLoader?.getResource("fixtures/$name")).readText()

private fun tokens(access: String, refresh: String, expiresIn: Int = 900) =
    """{"access_token": "$access", "access_expires_at": "2026-11-16T14:18:12-03:00", "access_expires_in": $expiresIn,
        "refresh_token": "$refresh", "refresh_expires_at": "2027-01-15T14:03:12-03:00"}"""

private fun MockRequestHandleScope.json(body: String, status: HttpStatusCode = HttpStatusCode.OK): HttpResponseData =
    respond(body, status, headersOf(HttpHeaders.ContentType, "application/json"))

private fun HttpRequestData.bodyText(): String = (body as? TextContent)?.text ?: ""

class SessionManagerTest {
    private var clock = Instant.parse("2026-11-16T14:03:12-03:00")

    private fun build(store: MemoryStore, handler: suspend MockRequestHandleScope.(HttpRequestData) -> HttpResponseData): Triple<SessionManager, AppApi, ApiClient> {
        val client = ApiClient(MockEngine { request -> handler(request) })
        val session = SessionManager(client, store) { clock }
        session.useBaseUrl(BASE)
        return Triple(session, AppApi(client, session), client)
    }

    private val device = DeviceInfo("android", "Samsung SM-S918B", "SM-S918B", "Android 15", "1.0.0 (1)")

    @Test fun loginStoresRefreshButNeverAccess() = runTest {
        val store = MemoryStore()
        val (session, _, _) = build(store) { request ->
            assertEquals("/api/v1/app/auth/login", request.url.encodedPath)
            assertNull(request.headers[HttpHeaders.Authorization])
            assertNull(request.headers[HttpHeaders.Cookie])
            json(fixture("login_ok.json"))
        }
        val r = session.login("ana@mercadobompreco.com.br", "Senha-forte-1", device)
        assertEquals("ok", r.status)
        val saved = store.value!!
        assertEquals("pr_Zm9...", saved.refreshToken)
        assertEquals("d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90", saved.deviceId)
        assertEquals(BASE, saved.apiBaseUrl)
        assertFalse(saved.toString().contains("pa_"))
    }

    @Test fun mfaRequiredDoesNotCreateSession() = runTest {
        val store = MemoryStore()
        val (session, _, _) = build(store) { json(fixture("login_mfa.json")) }
        val r = session.login("admin@crowntech.com.br", "x", device)
        assertEquals("mfa_required", r.status)
        assertNull(store.value)
        assertFalse(session.hasSession)
    }

    @Test fun tokenExpiredRefreshesOnceAndRetriesWithNewToken() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        val refreshes = AtomicInteger()
        val seenTokens = mutableListOf<String>()
        val (session, api, _) = build(store) { request ->
            when (request.url.encodedPath) {
                "/api/v1/app/auth/refresh" -> {
                    refreshes.incrementAndGet()
                    val n = refreshes.get()
                    assertTrue(request.bodyText().contains(if (n == 1) "pr_old" else "pr_1"))
                    json("""{"tokens": ${tokens("pa_$n", "pr_$n")}}""")
                }
                "/api/v1/app/me" -> {
                    val auth = request.headers[HttpHeaders.Authorization]!!
                    seenTokens += auth
                    if (auth == "Bearer pa_1") json("""{"error": "Sessão expirada.", "code": "token_expired"}""", HttpStatusCode.Unauthorized)
                    else json(fixture("me.json"))
                }
                else -> error("rota inesperada ${request.url}")
            }
        }
        assertTrue(session.restore(BASE))
        val me = api.me()
        assertEquals("Ana Souza", me.user.name)
        assertEquals(2, refreshes.get())
        assertEquals(listOf("Bearer pa_1", "Bearer pa_2"), seenTokens)
        assertEquals("pr_2", store.value!!.refreshToken) // rotação: guarda sempre o refresh mais novo
    }

    @Test fun concurrentExpiredCallsShareOneRefresh() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        val refreshes = AtomicInteger()
        val (session, api, _) = build(store) { request ->
            when (request.url.encodedPath) {
                "/api/v1/app/auth/refresh" -> {
                    val n = refreshes.incrementAndGet()
                    json("""{"tokens": ${tokens("pa_$n", "pr_$n")}}""")
                }
                else -> {
                    if (request.headers[HttpHeaders.Authorization] == "Bearer pa_1") {
                        json("""{"error": "Sessão expirada.", "code": "token_expired"}""", HttpStatusCode.Unauthorized)
                    } else {
                        assertEquals("tenant-1", request.headers["X-Tenant-ID"])
                        json(fixture("terminals.json"))
                    }
                }
            }
        }
        session.restore(BASE)
        session.refreshNow() // pa_1, que o servidor considera vencido
        val scope = br.com.crowntech.pdv2gestao.core.api.DataScope("tenant-1", null, "today")
        val results = (1..5).map { async { api.terminals(scope) } }.awaitAll()
        assertEquals(5, results.size)
        assertEquals("Uma renovação inicial + uma só para as 5 chamadas", 2, refreshes.get())
    }

    @Test fun proactiveRefreshWhenLessThan60Seconds() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        val refreshes = AtomicInteger()
        val (session, api, _) = build(store) { request ->
            if (request.url.encodedPath == "/api/v1/app/auth/refresh") {
                val n = refreshes.incrementAndGet()
                json("""{"tokens": ${tokens("pa_$n", "pr_$n")}}""")
            } else json(fixture("me.json"))
        }
        session.restore(BASE)
        api.me()
        assertEquals(1, refreshes.get())
        clock += 800.seconds // faltam 100 s: ainda não renova
        api.me()
        assertEquals(1, refreshes.get())
        clock += 50.seconds // faltam 50 s: renova antes de chamar
        api.me()
        assertEquals(2, refreshes.get())
    }

    @Test fun refreshDeviceRevokedEndsSessionWithMessage() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        val (session, api, _) = build(store) {
            json("""{"error": "Este aparelho foi desconectado da sua conta. Entre de novo.", "code": "device_revoked"}""", HttpStatusCode.Unauthorized)
        }
        session.restore(BASE)
        val event = async(start = CoroutineStart.UNDISPATCHED) { session.events.first() }
        try {
            api.me()
            fail("devia falhar")
        } catch (e: ApiException) {
            assertEquals("device_revoked", e.code)
        }
        assertNull(store.value)
        assertFalse(session.hasSession)
        assertEquals(SessionEvent.Ended("Este aparelho foi desconectado da sua conta. Entre de novo."), event.await())
    }

    @Test fun refreshAccessDeniedEmitsAccessDenied() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        val (session, _, _) = build(store) {
            json("""{"error": "Acesso ao app não liberado.", "code": "app_access_denied"}""", HttpStatusCode.Forbidden)
        }
        session.restore(BASE)
        val event = async(start = CoroutineStart.UNDISPATCHED) { session.events.first() }
        try { session.refreshNow(); fail() } catch (e: ApiException) { assertEquals("app_access_denied", e.code) }
        assertNull(store.value)
        assertEquals(SessionEvent.AccessDenied, event.await())
    }

    @Test fun networkErrorKeepsSession() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        val (session, _, _) = build(store) { throw java.io.IOException("sem rede") }
        session.restore(BASE)
        try { session.refreshNow(); fail() } catch (e: ApiException) { assertEquals("network", e.code) }
        assertEquals("pr_old", store.value!!.refreshToken)
        assertTrue(session.hasSession)
    }

    @Test fun changedBaseUrlDiscardsStoredSession() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", "http://127.0.0.1:8000"))
        val (session, _, _) = build(store) { error("não deve chamar") }
        assertFalse(session.restore(BASE))
        assertNull(store.value)
    }

    @Test fun logoutAlwaysClearsLocally() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        var logoutBody = ""
        val (session, _, _) = build(store) { request ->
            if (request.url.encodedPath == "/api/v1/app/auth/logout") {
                logoutBody = request.bodyText()
                json("""{"error": "falhou", "code": "unavailable"}""", HttpStatusCode.InternalServerError)
            } else error("rota inesperada")
        }
        session.restore(BASE)
        session.logout()
        assertTrue(logoutBody.contains("pr_old"))
        assertNull(store.value)
        assertFalse(session.hasSession)
    }

    @Test fun permissionDeniedDoesNotEndSession() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        val (session, api, _) = build(store) { request ->
            if (request.url.encodedPath == "/api/v1/app/auth/refresh") json("""{"tokens": ${tokens("pa_1", "pr_1")}}""")
            else json("""{"error": "Seu acesso não inclui esta área.", "code": "permission_denied"}""", HttpStatusCode.Forbidden)
        }
        session.restore(BASE)
        try {
            api.cashSessions(br.com.crowntech.pdv2gestao.core.api.DataScope("t", "s", "today"), "open", null)
            fail()
        } catch (e: ApiException) {
            assertEquals("permission_denied", e.code)
        }
        assertTrue(session.hasSession)
    }

    @Test fun queryParametersFollowContract() = runTest {
        val store = MemoryStore(StoredSession("pr_old", "dev-1", BASE))
        val seen = mutableListOf<String>()
        val (session, api, _) = build(store) { request ->
            if (request.url.encodedPath == "/api/v1/app/auth/refresh") json("""{"tokens": ${tokens("pa_1", "pr_1")}}""")
            else {
                seen += request.url.encodedPath + "?" + request.url.encodedQuery
                json(fixture("cash_sessions.json"))
            }
        }
        session.restore(BASE)
        val scope = br.com.crowntech.pdv2gestao.core.api.DataScope("t", null, "7d")
        api.cashSessions(scope, "open", null)
        api.cashSessions(scope, "closed", "50")
        assertFalse("Abertos ignoram período", seen[0].contains("period="))
        assertFalse("Sem loja escolhida não manda shop_id", seen[0].contains("shop_id"))
        assertTrue(seen[1].contains("period=7d"))
        assertTrue(seen[1].contains("cursor=50"))
    }
}
