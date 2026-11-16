import Foundation

/// O que o app faz ao receber cada `code` (tabela da seção 2.1 do contrato).
enum ErrorAction: Equatable {
    /// Mostra a frase do servidor (`error`).
    case showMessage
    /// Abre o seletor de empresa.
    case openTenantPicker
    /// Volta o período para Hoje.
    case resetPeriod
    /// Renova a sessão e repete a chamada uma vez.
    case refreshAndRetry
    /// Apaga os tokens e volta ao login com esta frase.
    case signOut(message: String)
    /// Volta ao login (desafio de MFA vencido).
    case backToLogin(message: String)
    /// Tela "Acesso não liberado".
    case accessDenied
    /// Recarrega `/me` e abre o seletor de empresa.
    case reloadMeAndPickTenant
    /// Volta para "Todas as lojas" e recarrega.
    case resetShop
    /// Estado vazio "Sem permissão" na aba.
    case permissionDenied
}

enum APIError: Error, Equatable {
    /// Sem rede, tempo esgotado ou falha de TLS.
    case offline
    /// Resposta 5xx ou corpo que não é JSON válido.
    case unavailable
    /// Erro com corpo `{"error","code"}`.
    case server(status: Int, code: String, message: String, retryAfter: Int?)
    /// URL do servidor recusada pela configuração (ex.: `http` em Release).
    case invalidServerURL(String)

    var code: String? {
        if case let .server(_, code, _, _) = self { return code }
        return nil
    }

    var status: Int? {
        if case let .server(status, _, _, _) = self { return status }
        return nil
    }

    var action: ErrorAction {
        guard let code else { return .showMessage }
        return APIErrorCatalog.action(for: code)
    }

    /// Frase que a pessoa vê. Nunca mostra o corpo de um 5xx.
    var userMessage: String {
        switch self {
        case .offline: return APIErrorCatalog.offlineMessage
        case .unavailable: return APIErrorCatalog.unavailableMessage
        case .invalidServerURL(let message): return message
        case let .server(status, code, message, _):
            if status >= 500 { return APIErrorCatalog.unavailableMessage }
            let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return trimmed }
            return APIErrorCatalog.defaultMessages[code] ?? APIErrorCatalog.genericMessage
        }
    }

    static func signedOut(_ code: String = "unauthenticated") -> APIError {
        .server(status: 401, code: code, message: APIErrorCatalog.defaultMessages[code] ?? "", retryAfter: nil)
    }
}

enum APIErrorCatalog {
    static let offlineMessage = "Sem internet ou servidor fora do ar. Confira a conexão e tente de novo."
    static let unavailableMessage = "O servidor não respondeu como esperado. Tente de novo em instantes."
    static let genericMessage = "Não foi possível concluir. Tente de novo."
    static let sessionEndedMessage = "Sua sessão terminou. Entre de novo."
    static let deviceRevokedMessage = "Este aparelho foi desconectado da sua conta. Entre de novo."
    static let mfaExpiredMessage = "O tempo para digitar o código acabou. Entre de novo."
    static let permissionMessage = "Seu acesso não inclui esta área."

    static let defaultMessages: [String: String] = [
        "invalid_request": "Dados inválidos. Confira e tente de novo.",
        "tenant_required": "Escolha uma empresa.",
        "invalid_period": "Período inválido.",
        "invalid_credentials": "E-mail ou senha incorretos.",
        "unauthenticated": "Entre de novo para continuar.",
        "token_expired": "Sessão expirada.",
        "refresh_invalid": sessionEndedMessage,
        "device_revoked": deviceRevokedMessage,
        "mfa_invalid": "Código incorreto. Confira o aplicativo autenticador.",
        "mfa_expired": mfaExpiredMessage,
        "app_access_denied": "Acesso ao app não liberado. Peça ao administrador do PDV2 para liberar seu acesso.",
        "mfa_setup_required": "Ative a verificação em duas etapas no painel web do PDV2 antes de usar o app.",
        "tenant_forbidden": "Você não tem acesso a esta empresa.",
        "shop_forbidden": "Você não tem acesso a esta loja.",
        "permission_denied": permissionMessage,
        "platform_forbidden": "Somente a administração do PDV2 pode fazer isso.",
        "not_found": "Registro não encontrado.",
        "method_not_allowed": "Operação não permitida.",
        "access_exists": "Essa pessoa já tem acesso liberado a esta empresa.",
        "member_required": "Essa pessoa não faz parte da equipe dessa empresa. Cadastre-a no painel da empresa primeiro.",
        "superuser_implicit": "O administrador principal já tem acesso ao app.",
        "rate_limited": "Muitas tentativas. Aguarde alguns minutos e tente de novo.",
        "mfa_locked": "Muitas tentativas com código errado. Aguarde 15 minutos.",
    ]

    static func action(for code: String) -> ErrorAction {
        switch code {
        case "tenant_required": return .openTenantPicker
        case "invalid_period": return .resetPeriod
        case "token_expired": return .refreshAndRetry
        case "unauthenticated", "refresh_invalid": return .signOut(message: sessionEndedMessage)
        case "device_revoked": return .signOut(message: deviceRevokedMessage)
        case "mfa_expired": return .backToLogin(message: mfaExpiredMessage)
        case "app_access_denied": return .accessDenied
        case "tenant_forbidden": return .reloadMeAndPickTenant
        case "shop_forbidden": return .resetShop
        case "permission_denied": return .permissionDenied
        default: return .showMessage
        }
    }

    /// Código padrão quando o servidor não manda `code` (mesma regra do decorador `app_endpoint`).
    static func defaultCode(forStatus status: Int) -> String {
        switch status {
        case 400, 413: return "invalid_request"
        case 401: return "unauthenticated"
        case 403: return "permission_denied"
        case 404: return "not_found"
        case 405: return "method_not_allowed"
        case 409: return "conflict"
        case 429: return "rate_limited"
        default: return status >= 500 ? "unavailable" : "invalid_request"
        }
    }

    /// Converte uma resposta HTTP de erro em `APIError`.
    static func error(status: Int, data: Data) -> APIError {
        if status >= 500 { return .unavailable }
        guard let body = try? JSONDecoder().decode(APIErrorBody.self, from: data) else {
            return .unavailable
        }
        let code = body.code ?? defaultCode(forStatus: status)
        return .server(status: status, code: code, message: body.error ?? "", retryAfter: body.retryAfterSeconds)
    }
}
