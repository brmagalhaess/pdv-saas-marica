package br.com.crowntech.pdv2gestao

import br.com.crowntech.pdv2gestao.core.api.ApiClient
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.AppConfig
import br.com.crowntech.pdv2gestao.core.api.ErrorAction
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ErrorAndConfigTest {

    @Test fun codeToAction() {
        val expected = mapOf(
            "invalid_request" to ErrorAction.ShowMessage,
            "tenant_required" to ErrorAction.ChooseTenant,
            "invalid_period" to ErrorAction.ResetPeriod,
            "invalid_credentials" to ErrorAction.ShowMessage,
            "unauthenticated" to ErrorAction.EndSession,
            "token_expired" to ErrorAction.RefreshAndRetry,
            "refresh_invalid" to ErrorAction.EndSession,
            "device_revoked" to ErrorAction.EndSession,
            "mfa_invalid" to ErrorAction.ShowMessage,
            "mfa_expired" to ErrorAction.EndSession,
            "app_access_denied" to ErrorAction.AccessDenied,
            "mfa_setup_required" to ErrorAction.ShowMessage,
            "tenant_forbidden" to ErrorAction.ReloadMeAndChooseTenant,
            "shop_forbidden" to ErrorAction.ResetShop,
            "permission_denied" to ErrorAction.NoPermission,
            "platform_forbidden" to ErrorAction.ShowMessage,
            "not_found" to ErrorAction.ShowMessage,
            "method_not_allowed" to ErrorAction.ShowMessage,
            "access_exists" to ErrorAction.ShowMessage,
            "member_required" to ErrorAction.ShowMessage,
            "superuser_implicit" to ErrorAction.ShowMessage,
            "conflict" to ErrorAction.ShowMessage,
            "rate_limited" to ErrorAction.ShowMessage,
            "mfa_locked" to ErrorAction.ShowMessage,
            "unavailable" to ErrorAction.ServerError,
            "network" to ErrorAction.Network,
        )
        expected.forEach { (code, action) -> assertEquals(code, action, ErrorAction.of(code)) }
    }

    @Test fun parsesErrorBodyWithExtras() {
        val e = ApiClient.parseError(429, """{"error": "Muitas tentativas. Aguarde alguns minutos e tente de novo.", "code": "rate_limited", "retry_after_seconds": 840}""")
        assertEquals("rate_limited", e.code)
        assertEquals(840L, e.retryAfterSeconds)
        assertEquals("Muitas tentativas. Aguarde alguns minutos e tente de novo.", e.userMessage)

        val exists = ApiClient.parseError(409, """{"error": "Essa pessoa já tem acesso liberado a esta empresa.", "code": "access_exists", "access_id": "abc"}""")
        assertEquals("abc", exists.accessId)
    }

    @Test fun missingCodeFallsBackByStatus() {
        assertEquals("not_found", ApiClient.parseError(404, """{"error": "Registro não encontrado."}""").code)
        assertEquals("permission_denied", ApiClient.parseError(403, """{"error": "x"}""").code)
        assertEquals("unauthenticated", ApiClient.parseError(401, "{}").code)
        assertEquals("invalid_request", ApiClient.parseError(413, "{}").code)
    }

    @Test fun serverErrorsAndNonJsonNeverShowBody() {
        val e5 = ApiClient.parseError(502, """{"error": "Traceback secreto", "code": "x"}""")
        assertEquals("unavailable", e5.code)
        assertEquals(ApiException.SERVER_ERROR_MESSAGE, e5.userMessage)
        val html = ApiClient.parseError(403, "<html>Forbidden</html>")
        assertEquals("unavailable", html.code)
        assertEquals(ApiException.SERVER_ERROR_MESSAGE, html.userMessage)
    }

    @Test fun sessionEndMessages() {
        val revoked = ApiClient.parseError(401, """{"error": "x", "code": "device_revoked"}""")
        assertEquals("Este aparelho foi desconectado da sua conta. Entre de novo.", revoked.userMessage)
        val refresh = ApiClient.parseError(401, """{"error": "x", "code": "refresh_invalid"}""")
        assertEquals("Sua sessão terminou. Entre de novo.", refresh.userMessage)
        assertEquals("Sem internet ou servidor fora do ar. Confira a conexão e tente de novo.", ApiException.network().userMessage)
    }

    @Test fun releaseRefusesHttp() {
        assertNull(AppConfig.validateBaseUrl("http://pdv2.crowntech.com.br", allowHttp = false))
        assertNull(AppConfig.validateBaseUrl("http://127.0.0.1:8000", allowHttp = false))
        assertEquals("https://pdv2.crowntech.com.br", AppConfig.validateBaseUrl("https://pdv2.crowntech.com.br/", allowHttp = false))
    }

    @Test fun debugAcceptsLocalHttp() {
        assertEquals("http://127.0.0.1:8000", AppConfig.validateBaseUrl(" http://127.0.0.1:8000/ ", allowHttp = true))
        assertNull(AppConfig.validateBaseUrl("ftp://x", allowHttp = true))
        assertNull(AppConfig.validateBaseUrl("https://", allowHttp = true))
        assertNull(AppConfig.validateBaseUrl("pdv2.crowntech.com.br", allowHttp = true))
    }

    @Test fun buildConfigDefaults() {
        // Testes rodam no Debug: servidor local e troca permitida; Release usa https fixo (build.gradle.kts).
        assertEquals("http://127.0.0.1:8000", BuildConfig.API_BASE_URL)
        assertTrue(BuildConfig.ALLOW_SERVER_OVERRIDE)
        assertFalse(AppConfig.validateBaseUrl(BuildConfig.API_BASE_URL, allowHttp = false) != null)
        assertEquals("br.com.crowntech.pdv2gestao", BuildConfig.APPLICATION_ID)
        assertEquals("1.0.0", BuildConfig.VERSION_NAME)
    }
}
