import Foundation

enum APIClientError: LocalizedError {
    case invalidURL
    case invalidResponse
    case server(message: String, status: Int)
    case offline

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "URL do servidor inválida."
        case .invalidResponse: return "O servidor enviou uma resposta inválida."
        case .server(let message, let status):
            if status == 401 { return "E-mail ou senha inválidos." }
            if status == 403 { return message.isEmpty ? "Sua conta não tem acesso de proprietário." : message }
            if status == 429 { return "Muitas tentativas. Aguarde alguns minutos e tente novamente." }
            return message.isEmpty ? "Não foi possível concluir a solicitação." : message
        case .offline: return "Sem conexão com o servidor."
        }
    }
}

final class APIClient {
    let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder

    init(baseURLString: String) throws {
        var value = baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") { value.removeLast() }
        guard let url = URL(string: value),
              let scheme = url.scheme, ["http", "https"].contains(scheme),
              url.host != nil else { throw APIClientError.invalidURL }
        self.baseURL = url
        let configuration = URLSessionConfiguration.default
        configuration.httpCookieStorage = HTTPCookieStorage.shared
        configuration.httpShouldSetCookies = true
        configuration.httpCookieAcceptPolicy = .always
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 30
        self.session = URLSession(configuration: configuration)
        self.decoder = JSONDecoder()
    }

    func login(email: String, password: String) async throws -> LoginResponse {
        // Django protects POST requests with CSRF. Fetching this first also
        // establishes the session cookie that URLSession will retain.
        let csrf = try await fetchCSRF()
        return try await send(path: "/api/v1/auth/login", method: "POST",
                              body: LoginBody(email: email, password: password),
                              headers: ["X-CSRFToken": csrf])
    }

    func monitoring(tenantID: String) async throws -> MonitoringSnapshot {
        try await send(path: "/api/v1/owner/monitoring", method: "GET",
                       headers: ["X-Tenant-ID": tenantID])
    }

    func logout() async {
        guard let csrf = try? await fetchCSRF() else { return }
        _ = try? await send(path: "/api/v1/auth/logout", method: "POST",
                            body: EmptyBody(), headers: ["X-CSRFToken": csrf]) as EmptyResponse
    }

    func clearCookies() {
        HTTPCookieStorage.shared.cookies?.forEach { HTTPCookieStorage.shared.deleteCookie($0) }
    }

    private func fetchCSRF() async throws -> String {
        let response: CSRFResponse = try await send(path: "/api/v1/csrf", method: "GET")
        guard !response.csrfToken.isEmpty else { throw APIClientError.invalidResponse }
        return response.csrfToken
    }

    private func send<T: Decodable>(path: String, method: String,
                                    body: Encodable? = nil,
                                    headers: [String: String] = [:]) async throws -> T {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw APIClientError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        headers.forEach { request.setValue($1, forHTTPHeaderField: $0) }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(AnyEncodable(body))
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIClientError.offline
        }
        guard let http = response as? HTTPURLResponse else { throw APIClientError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            let message = (try? decoder.decode(ErrorResponse.self, from: data).error) ?? ""
            throw APIClientError.server(message: message, status: http.statusCode)
        }
        if T.self == EmptyResponse.self { return EmptyResponse() as! T }
        do { return try decoder.decode(T.self, from: data) }
        catch { throw APIClientError.invalidResponse }
    }
}

private struct LoginBody: Encodable { let email: String; let password: String }
private struct EmptyBody: Encodable {}
private struct EmptyResponse: Decodable {}
private struct ErrorResponse: Decodable { let error: String }
private struct CSRFResponse: Decodable {
    let csrfToken: String
    enum CodingKeys: String, CodingKey { case csrfToken = "csrfToken" }
}

/// Type erasure for the small Encodable request bodies used by the client.
private struct AnyEncodable: Encodable {
    private let encodeValue: (Encoder) throws -> Void
    init(_ value: Encodable) { encodeValue = value.encode(to:) }
    func encode(to encoder: Encoder) throws { try encodeValue(encoder) }
}
