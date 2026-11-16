import Foundation

/// Uma chamada à API do app (`/api/v1/app/...`, sem barra final).
struct Endpoint {
    var method: String = "GET"
    var path: String
    var query: [URLQueryItem] = []
    var body: Data?
    /// Empresa da consulta (cabeçalho `X-Tenant-ID`).
    var tenantID: String?
    /// `false` para login, MFA e refresh (sem Bearer).
    var authenticated = true

    static func get(_ path: String, query: [URLQueryItem] = [], tenantID: String? = nil) -> Endpoint {
        Endpoint(path: path, query: query.filter { !($0.value ?? "").isEmpty }, tenantID: tenantID)
    }

    static func send<Body: Encodable>(_ method: String, _ path: String, body: Body,
                                      authenticated: Bool = true) throws -> Endpoint {
        Endpoint(method: method, path: path, body: try JSONEncoder().encode(body), authenticated: authenticated)
    }
}

/// Transporte HTTP trocável (testes usam uma versão falsa).
protocol HTTPTransport: Sendable {
    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

/// Transporte real: sessão efêmera, sem cookies nem cache, tempo limite de 20 s.
final class URLSessionTransport: HTTPTransport, @unchecked Sendable {
    private let session: URLSession

    init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 40
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: configuration)
    }

    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw APIError.unavailable }
            return (data, http)
        } catch let error as APIError {
            throw error
        } catch let error as URLError where error.code == .cancelled {
            throw CancellationError()
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw APIError.offline
        }
    }
}

/// Monta `URLRequest` com os cabeçalhos do contrato e interpreta as respostas.
struct APIRequestBuilder {
    let baseURL: URL
    let userAgent: String

    func request(for endpoint: Endpoint, accessToken: String?) throws -> URLRequest {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw APIError.invalidServerURL("Endereço do servidor inválido.")
        }
        let basePath = components.path.hasSuffix("/") ? String(components.path.dropLast()) : components.path
        components.path = basePath + endpoint.path
        components.queryItems = endpoint.query.isEmpty ? nil : endpoint.query
        guard let url = components.url else { throw APIError.invalidServerURL("Endereço do servidor inválido.") }
        var request = URLRequest(url: url)
        request.httpMethod = endpoint.method
        request.timeoutInterval = 20
        request.httpShouldHandleCookies = false
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("pt-BR", forHTTPHeaderField: "Accept-Language")
        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        if let tenantID = endpoint.tenantID { request.setValue(tenantID, forHTTPHeaderField: "X-Tenant-ID") }
        if let accessToken { request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization") }
        if let body = endpoint.body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        return request
    }

    static func decode<T: Decodable>(_ type: T.Type, data: Data, response: HTTPURLResponse) throws -> T {
        guard (200..<300).contains(response.statusCode) else {
            throw APIErrorCatalog.error(status: response.statusCode, data: data)
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            // Resposta que não é o JSON esperado: tratar como 5xx.
            throw APIError.unavailable
        }
    }
}
