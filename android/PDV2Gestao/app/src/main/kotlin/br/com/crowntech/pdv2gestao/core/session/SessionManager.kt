package br.com.crowntech.pdv2gestao.core.session

import br.com.crowntech.pdv2gestao.core.api.ApiClient
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.ErrorAction
import br.com.crowntech.pdv2gestao.core.model.AppJson
import br.com.crowntech.pdv2gestao.core.model.DeviceInfo
import br.com.crowntech.pdv2gestao.core.model.LoginRequest
import br.com.crowntech.pdv2gestao.core.model.LoginResponse
import br.com.crowntech.pdv2gestao.core.model.LogoutRequest
import br.com.crowntech.pdv2gestao.core.model.MfaRequest
import br.com.crowntech.pdv2gestao.core.model.OkResponse
import br.com.crowntech.pdv2gestao.core.model.RefreshRequest
import br.com.crowntech.pdv2gestao.core.model.RefreshResponse
import br.com.crowntech.pdv2gestao.core.model.TokenPair
import io.ktor.http.HttpMethod
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.datetime.Clock
import kotlinx.datetime.Instant
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlin.time.Duration.Companion.seconds

/** O que fica guardado (cifrado) no aparelho. O access token NUNCA é gravado. */
@Serializable
data class StoredSession(
    @SerialName("refresh_token") val refreshToken: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("api_base_url") val apiBaseUrl: String,
)

interface SessionStore {
    suspend fun load(): StoredSession?
    suspend fun save(session: StoredSession)
    suspend fun clear()
}

sealed interface SessionEvent {
    /** Sessão terminou (refresh inválido, aparelho revogado...). Ir ao Login mostrando [message]. */
    data class Ended(val message: String) : SessionEvent
    /** O acesso ao app foi retirado. Mostrar "Acesso não liberado". */
    data object AccessDenied : SessionEvent
}

/**
 * Guarda os tokens e renova o access token com "single-flight":
 * uma renovação por vez; quem recebe `token_expired` espera e repete UMA vez.
 * Renova também antes da hora se faltar menos de 60 s para vencer.
 */
class SessionManager(
    private val client: ApiClient,
    private val store: SessionStore,
    private val now: () -> Instant = { Clock.System.now() },
) {
    private val mutex = Mutex()

    @Volatile var baseUrl: String = ""
        private set
    @Volatile private var accessToken: String? = null
    @Volatile private var accessExpiresAt: Instant? = null
    @Volatile private var refreshToken: String? = null
    @Volatile var deviceId: String? = null
        private set

    private val _events = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 4, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    val events: SharedFlow<SessionEvent> = _events

    val hasSession: Boolean get() = refreshToken != null

    /** Carrega o refresh guardado. Se a base URL mudou, descarta tudo. */
    suspend fun restore(expectedBaseUrl: String): Boolean {
        val saved = runCatching { store.load() }.getOrNull()
        if (saved == null) {
            baseUrl = expectedBaseUrl
            return false
        }
        if (saved.apiBaseUrl.trimEnd('/') != expectedBaseUrl.trimEnd('/')) {
            runCatching { store.clear() }
            baseUrl = expectedBaseUrl
            return false
        }
        baseUrl = expectedBaseUrl
        refreshToken = saved.refreshToken
        deviceId = saved.deviceId
        accessToken = null
        accessExpiresAt = null
        return true
    }

    fun useBaseUrl(url: String) {
        baseUrl = url.trimEnd('/')
    }

    suspend fun login(email: String, password: String, device: DeviceInfo): LoginResponse {
        val body = AppJson.encodeToString(LoginRequest.serializer(), LoginRequest(email.trim(), password, device))
        val response = client.send(HttpMethod.Post, baseUrl, "/api/v1/app/auth/login", LoginResponse.serializer(), jsonBody = body)
        if (response.status == "ok") adopt(response)
        return response
    }

    suspend fun verifyMfa(mfaToken: String, code: String? = null, recoveryCode: String? = null): LoginResponse {
        val body = AppJson.encodeToString(MfaRequest.serializer(), MfaRequest(mfaToken, code, recoveryCode))
        val response = client.send(HttpMethod.Post, baseUrl, "/api/v1/app/auth/mfa", LoginResponse.serializer(), jsonBody = body)
        if (response.status == "ok") adopt(response)
        return response
    }

    private suspend fun adopt(response: LoginResponse) {
        val tokens = response.tokens ?: throw ApiException.server(200)
        val device = response.me?.device?.id ?: deviceId ?: ""
        mutex.withLock {
            deviceId = device
            applyTokens(tokens)
        }
    }

    private suspend fun applyTokens(tokens: TokenPair) {
        accessToken = tokens.accessToken
        val lifetime = tokens.accessExpiresIn.coerceIn(1, 24 * 3600)
        accessExpiresAt = now() + lifetime.seconds
        refreshToken = tokens.refreshToken
        store.save(StoredSession(tokens.refreshToken, deviceId ?: "", baseUrl))
    }

    /** Executa [block] com um access token válido, tratando `token_expired` e fim de sessão. */
    suspend fun <T> authorized(block: suspend (accessToken: String) -> T): T {
        val token = currentOrRefreshed(stale = null)
        return try {
            block(token)
        } catch (e: ApiException) {
            when (e.action) {
                ErrorAction.RefreshAndRetry -> {
                    val fresh = currentOrRefreshed(stale = token)
                    try {
                        block(fresh)
                    } catch (e2: ApiException) {
                        handleTerminal(e2)
                        throw e2
                    }
                }
                else -> {
                    handleTerminal(e)
                    throw e
                }
            }
        }
    }

    private suspend fun handleTerminal(e: ApiException) {
        when (e.action) {
            ErrorAction.EndSession -> endSession(e.userMessage)
            ErrorAction.RefreshAndRetry -> endSession(ApiException.SESSION_ENDED_MESSAGE)
            else -> Unit
        }
    }

    /**
     * Devolve um access token utilizável. Se [stale] é o token que acabou de falhar e outro
     * fluxo já renovou, reaproveita o novo (single-flight).
     */
    private suspend fun currentOrRefreshed(stale: String?): String = mutex.withLock {
        val current = accessToken
        val expires = accessExpiresAt
        val fresh = current != null && expires != null && (expires - now()) > PROACTIVE_WINDOW
        if (fresh && current != stale) return@withLock current!!
        doRefreshLocked()
        accessToken ?: throw ApiException(401, "unauthenticated", ApiException.SESSION_ENDED_MESSAGE)
    }

    /** Força uma renovação (usada na abertura do app). */
    suspend fun refreshNow() {
        mutex.withLock { doRefreshLocked() }
    }

    private suspend fun doRefreshLocked() {
        val refresh = refreshToken
        if (refresh == null) {
            clearLocal()
            _events.tryEmit(SessionEvent.Ended(ApiException.SESSION_ENDED_MESSAGE))
            throw ApiException(401, "refresh_invalid", ApiException.SESSION_ENDED_MESSAGE)
        }
        val body = AppJson.encodeToString(RefreshRequest.serializer(), RefreshRequest(refresh))
        try {
            val response = client.send(HttpMethod.Post, baseUrl, "/api/v1/app/auth/refresh", RefreshResponse.serializer(), jsonBody = body)
            applyTokens(response.tokens)
        } catch (e: ApiException) {
            when (e.action) {
                ErrorAction.EndSession, ErrorAction.RefreshAndRetry -> {
                    clearLocal()
                    _events.tryEmit(SessionEvent.Ended(ApiException.sessionEndedMessage(e.code, e.message)))
                }
                ErrorAction.AccessDenied -> {
                    clearLocal()
                    _events.tryEmit(SessionEvent.AccessDenied)
                }
                else -> Unit // sem rede, 5xx, 429: mantém a sessão para tentar depois
            }
            throw e
        }
    }

    /** Sair: avisa o servidor (melhor esforço) e apaga tudo localmente. */
    suspend fun logout() {
        val access = accessToken
        val refresh = refreshToken
        try {
            if (access != null || refresh != null) {
                val body = AppJson.encodeToString(LogoutRequest.serializer(), LogoutRequest(refresh))
                client.send(HttpMethod.Post, baseUrl, "/api/v1/app/auth/logout", OkResponse.serializer(), jsonBody = body, bearer = access)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            // Idempotente: o app apaga tudo de qualquer jeito.
        }
        mutex.withLock { clearLocal() }
    }

    /** Apaga a sessão e avisa a interface. */
    suspend fun endSession(message: String) {
        clearLocal()
        _events.tryEmit(SessionEvent.Ended(message))
    }

    /** Apaga só localmente (ex.: depois de pedir exclusão de conta ou desconectar este aparelho). */
    suspend fun clearLocal() {
        accessToken = null
        accessExpiresAt = null
        refreshToken = null
        deviceId = null
        runCatching { store.clear() }
    }

    companion object {
        val PROACTIVE_WINDOW = 60.seconds
    }
}
