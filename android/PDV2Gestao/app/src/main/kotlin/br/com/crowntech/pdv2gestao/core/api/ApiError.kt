package br.com.crowntech.pdv2gestao.core.api

/**
 * Erro de chamada à API, já traduzido para a regra do contrato (§2.1).
 * [code] é o código estável do servidor ou "network" (sem conexão).
 */
class ApiException(
    val httpStatus: Int,
    val code: String,
    override val message: String,
    val retryAfterSeconds: Long? = null,
    val accessId: String? = null,
) : Exception(message) {
    val action: ErrorAction get() = ErrorAction.of(code)

    /** Texto para mostrar ao usuário. */
    val userMessage: String get() = when (action) {
        ErrorAction.ServerError -> SERVER_ERROR_MESSAGE
        ErrorAction.Network -> NETWORK_MESSAGE
        ErrorAction.EndSession -> sessionEndedMessage(code, message)
        ErrorAction.NoPermission -> "Seu acesso não inclui esta área."
        else -> message.ifBlank { defaultMessage(code) }
    }

    companion object {
        const val NETWORK_MESSAGE = "Sem internet ou servidor fora do ar. Confira a conexão e tente de novo."
        const val SERVER_ERROR_MESSAGE = "O servidor não respondeu como esperado. Tente de novo em instantes."
        const val SESSION_ENDED_MESSAGE = "Sua sessão terminou. Entre de novo."
        const val DEVICE_REVOKED_MESSAGE = "Este aparelho foi desconectado da sua conta. Entre de novo."

        fun network() = ApiException(0, "network", NETWORK_MESSAGE)
        fun server(status: Int) = ApiException(status, "unavailable", SERVER_ERROR_MESSAGE)

        fun sessionEndedMessage(code: String, serverMessage: String?): String = when (code) {
            "device_revoked" -> DEVICE_REVOKED_MESSAGE
            "mfa_expired" -> serverMessage?.ifBlank { null } ?: "O tempo para digitar o código acabou. Entre de novo."
            else -> SESSION_ENDED_MESSAGE
        }

        /** Código padrão pelo status, quando o servidor não manda `code` (§2.1). */
        fun defaultCode(status: Int): String = when (status) {
            400, 413 -> "invalid_request"
            401 -> "unauthenticated"
            403 -> "permission_denied"
            404 -> "not_found"
            405 -> "method_not_allowed"
            409 -> "conflict"
            429 -> "rate_limited"
            else -> if (status >= 500) "unavailable" else "invalid_request"
        }

        fun defaultMessage(code: String): String = when (code) {
            "invalid_request" -> "Dados inválidos. Confira e tente de novo."
            "tenant_required" -> "Escolha uma empresa."
            "invalid_period" -> "Período inválido."
            "invalid_credentials" -> "E-mail ou senha incorretos."
            "unauthenticated" -> "Entre de novo para continuar."
            "token_expired" -> "Sessão expirada."
            "refresh_invalid" -> SESSION_ENDED_MESSAGE
            "device_revoked" -> DEVICE_REVOKED_MESSAGE
            "mfa_invalid" -> "Código incorreto. Confira o aplicativo autenticador."
            "mfa_expired" -> "O tempo para digitar o código acabou. Entre de novo."
            "app_access_denied" -> "Acesso ao app não liberado. Peça ao administrador do PDV2 para liberar seu acesso."
            "mfa_setup_required" -> "Ative a verificação em duas etapas no painel web do PDV2 antes de usar o app."
            "tenant_forbidden" -> "Você não tem acesso a esta empresa."
            "shop_forbidden" -> "Você não tem acesso a esta loja."
            "permission_denied" -> "Seu acesso não inclui esta área."
            "platform_forbidden" -> "Somente a administração do PDV2 pode fazer isso."
            "not_found" -> "Registro não encontrado."
            "method_not_allowed" -> "Operação não permitida."
            "rate_limited" -> "Muitas tentativas. Aguarde alguns minutos e tente de novo."
            "mfa_locked" -> "Muitas tentativas com código errado. Aguarde 15 minutos."
            "unavailable" -> SERVER_ERROR_MESSAGE
            "network" -> NETWORK_MESSAGE
            else -> "Não foi possível concluir. Tente de novo."
        }
    }
}

/** O que o app faz com cada código (tabela §2.1, coluna "O app faz"). */
enum class ErrorAction {
    /** Mostra a frase `error` no lugar onde aconteceu. */
    ShowMessage,
    /** tenant_required: abre o seletor de empresa. */
    ChooseTenant,
    /** invalid_period: volta para Hoje. */
    ResetPeriod,
    /** token_expired: renova (refresh) e repete UMA vez. */
    RefreshAndRetry,
    /** unauthenticated / refresh_invalid / device_revoked / mfa_expired: apaga tokens e volta ao Login. */
    EndSession,
    /** app_access_denied: tela "Acesso não liberado". */
    AccessDenied,
    /** tenant_forbidden: recarrega /me e abre o seletor. */
    ReloadMeAndChooseTenant,
    /** shop_forbidden: volta para "Todas as lojas" e recarrega. */
    ResetShop,
    /** permission_denied: estado vazio "Sem permissão" na aba. */
    NoPermission,
    /** 5xx ou resposta não-JSON: mensagem genérica, sem mostrar o corpo. */
    ServerError,
    /** Sem rede, timeout ou TLS. */
    Network;

    companion object {
        fun of(code: String): ErrorAction = when (code) {
            "tenant_required" -> ChooseTenant
            "invalid_period" -> ResetPeriod
            "token_expired" -> RefreshAndRetry
            "unauthenticated", "refresh_invalid", "device_revoked", "mfa_expired" -> EndSession
            "app_access_denied" -> AccessDenied
            "tenant_forbidden" -> ReloadMeAndChooseTenant
            "shop_forbidden" -> ResetShop
            "permission_denied" -> NoPermission
            "unavailable" -> ServerError
            "network" -> Network
            else -> ShowMessage
        }
    }
}
