package br.com.crowntech.pdv2gestao.core.api

import android.os.Build
import br.com.crowntech.pdv2gestao.BuildConfig

/**
 * Endereço da API e identificação do app.
 * Release: URL fixa (https://pdv2.crowntech.com.br) e recusa qualquer endereço sem https.
 * Debug: permite trocar o servidor na tela de login ("Servidor (desenvolvimento)").
 */
object AppConfig {
    const val VERSION_NAME = BuildConfig.VERSION_NAME
    const val VERSION_CODE = BuildConfig.VERSION_CODE
    val defaultBaseUrl: String = BuildConfig.API_BASE_URL
    val allowServerOverride: Boolean = BuildConfig.ALLOW_SERVER_OVERRIDE

    /** "1.0.0 (1)" — como o servidor espera em device.app_version. */
    val appVersionLabel: String get() = "$VERSION_NAME ($VERSION_CODE)"

    /**
     * Normaliza e valida o endereço do servidor.
     * Retorna null se não for aceitável: sem http(s), ou http fora do modo de desenvolvimento.
     */
    fun validateBaseUrl(raw: String, allowHttp: Boolean = allowServerOverride): String? {
        val url = raw.trim().trimEnd('/')
        if (url.isEmpty() || url.any { it.isWhitespace() }) return null
        val lower = url.lowercase()
        val scheme = when {
            lower.startsWith("https://") -> "https"
            lower.startsWith("http://") -> "http"
            else -> return null
        }
        if (scheme == "http" && !allowHttp) return null
        val host = url.substringAfter("://").substringBefore('/').substringBefore('?')
        if (host.isBlank()) return null
        return url
    }

    fun userAgent(): String =
        "PDV2Gestao/$VERSION_NAME (Android ${Build.VERSION.RELEASE ?: "?"}; ${Build.MODEL ?: "?"})"

    fun deviceName(): String {
        val maker = (Build.MANUFACTURER ?: "").replaceFirstChar { it.uppercase() }
        val model = Build.MODEL ?: "Android"
        val name = if (maker.isNotBlank() && !model.startsWith(maker, ignoreCase = true)) "$maker $model" else model
        return name.take(80).ifBlank { "Android" }
    }

    fun osVersion(): String = "Android ${Build.VERSION.RELEASE ?: ""}".trim().take(40)

    fun model(): String = (Build.MODEL ?: "").take(60)
}
