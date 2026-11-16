import Foundation
import XCTest
@testable import PDV2Gestao

/// Lê uma fixture do contrato (PDV2GestaoTests/Fixtures/*.json).
func fixture(_ name: String, file: StaticString = #filePath, line: UInt = #line) throws -> Data {
    let bundle = Bundle(for: FixtureToken.self)
    let url = try XCTUnwrap(bundle.url(forResource: name, withExtension: "json"),
                            "Fixture \(name).json não está no alvo de testes", file: file, line: line)
    return try Data(contentsOf: url)
}

func decodeFixture<T: Decodable>(_ type: T.Type, _ name: String, file: StaticString = #filePath, line: UInt = #line) throws -> T {
    try JSONDecoder().decode(T.self, from: fixture(name, file: file, line: line))
}

/// Compara dois JSON como objetos (sem depender da ordem das chaves).
func jsonObject(_ data: Data) throws -> NSObject {
    try XCTUnwrap(JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) as? NSObject)
}

final class FixtureToken {}

/// Transporte falso: responde por rota e grava as requisições.
final class MockTransport: HTTPTransport, @unchecked Sendable {
    struct Response {
        var status: Int
        var body: Data
        var delay: UInt64 = 0
    }

    private let lock = NSLock()
    private var _requests: [URLRequest] = []
    private var handler: (URLRequest) throws -> Response

    init(handler: @escaping (URLRequest) throws -> Response) {
        self.handler = handler
    }

    var requests: [URLRequest] { lock.withLock { _requests } }

    func requests(to path: String) -> [URLRequest] {
        requests.filter { $0.url?.path == path }
    }

    func setHandler(_ handler: @escaping (URLRequest) throws -> Response) {
        lock.withLock { self.handler = handler }
    }

    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let response: Response = try lock.withLock {
            _requests.append(request)
            return try handler(request)
        }
        if response.delay > 0 { try await Task.sleep(nanoseconds: response.delay) }
        let http = HTTPURLResponse(url: request.url!, statusCode: response.status, httpVersion: "HTTP/1.1",
                                   headerFields: ["Content-Type": "application/json"])!
        return (response.body, http)
    }
}

func json(_ object: Any) -> Data {
    try! JSONSerialization.data(withJSONObject: object)
}

/// Relógio controlável para testar a renovação antecipada.
final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var current: Date

    init(_ start: Date = Date(timeIntervalSince1970: 1_800_000_000)) { current = start }

    var now: Date { lock.withLock { current } }
    func advance(_ seconds: TimeInterval) { lock.withLock { current = current.addingTimeInterval(seconds) } }
}

let testDevice = LoginRequest.Device(platform: "ios", name: "iPhone", model: "iPhone16,2",
                                     osVersion: "iOS 26.1", appVersion: "1.0.0 (1)")
let testBaseURL = URL(string: "https://pdv2.crowntech.com.br")!
