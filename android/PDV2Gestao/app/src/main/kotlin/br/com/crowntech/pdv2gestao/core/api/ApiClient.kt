package br.com.crowntech.pdv2gestao.core.api

import br.com.crowntech.pdv2gestao.core.model.ApiErrorBody
import br.com.crowntech.pdv2gestao.core.model.AppJson
import io.ktor.client.HttpClient
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.defaultRequest
import io.ktor.client.request.header
import io.ktor.client.request.parameter
import io.ktor.client.request.request
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.contentType
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException

/**
 * Cliente HTTP de baixo nível (Ktor). Sem cookies: o app só usa Bearer.
 * Erros viram [ApiException] com o `code` do contrato.
 */
class ApiClient(
    engine: HttpClientEngine? = null,
    private val userAgent: String = "PDV2Gestao",
) {
    val http: HttpClient = (engine?.let { HttpClient(it) { configure() } } ?: HttpClient(OkHttp) { configure() })

    private fun io.ktor.client.HttpClientConfig<*>.configure() {
        expectSuccess = false
        followRedirects = false
        install(HttpTimeout) {
            requestTimeoutMillis = TIMEOUT_MS
            connectTimeoutMillis = TIMEOUT_MS
            socketTimeoutMillis = TIMEOUT_MS
        }
        defaultRequest {
            header(HttpHeaders.Accept, "application/json")
            header(HttpHeaders.AcceptLanguage, "pt-BR")
            header(HttpHeaders.UserAgent, userAgent)
        }
    }

    /**
     * Faz a requisição e decodifica a resposta. [body] já vem serializado em JSON.
     */
    suspend fun <T> send(
        method: HttpMethod,
        baseUrl: String,
        path: String,
        deserializer: KSerializer<T>,
        query: Map<String, String?> = emptyMap(),
        jsonBody: String? = null,
        bearer: String? = null,
        tenantId: String? = null,
    ): T {
        val text: String
        val status: Int
        try {
            val response = http.request(baseUrl.trimEnd('/') + path) {
                this.method = method
                query.forEach { (k, v) -> if (!v.isNullOrEmpty()) parameter(k, v) }
                if (bearer != null) header(HttpHeaders.Authorization, "Bearer $bearer")
                if (tenantId != null) header("X-Tenant-ID", tenantId)
                if (jsonBody != null) {
                    contentType(ContentType.Application.Json)
                    setBody(jsonBody)
                }
            }
            status = response.status.value
            text = response.bodyAsText()
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiException) {
            throw e
        } catch (e: Exception) {
            // Sem rede, timeout, TLS, DNS...
            throw ApiException.network()
        }
        if (status in 200..299) {
            return try {
                AppJson.decodeFromString(deserializer, text)
            } catch (e: SerializationException) {
                throw ApiException.server(status)
            } catch (e: IllegalArgumentException) {
                throw ApiException.server(status)
            }
        }
        throw parseError(status, text)
    }

    companion object {
        const val TIMEOUT_MS = 20_000L

        /** Converte resposta de erro em [ApiException] (§2.1). Não-JSON ou 5xx = "unavailable". */
        fun parseError(status: Int, text: String): ApiException {
            if (status >= 500) return ApiException.server(status)
            val body = runCatching { AppJson.decodeFromString(ApiErrorBody.serializer(), text) }.getOrNull()
                ?: return ApiException.server(status)
            val code = body.code?.takeIf { it.isNotBlank() } ?: ApiException.defaultCode(status)
            val message = body.error?.takeIf { it.isNotBlank() } ?: ApiException.defaultMessage(code)
            return ApiException(status, code, message, body.retryAfterSeconds, body.accessId)
        }
    }
}
