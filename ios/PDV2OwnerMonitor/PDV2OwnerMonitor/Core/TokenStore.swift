import Foundation
import Security

/// O que fica guardado entre aberturas. O access token nunca é gravado.
struct StoredSession: Codable, Equatable {
    var refreshToken: String
    var deviceID: String
    var apiBaseURL: String

    enum CodingKeys: String, CodingKey {
        case refreshToken = "refresh_token"
        case deviceID = "device_id"
        case apiBaseURL = "api_base_url"
    }
}

protocol SessionStore: AnyObject, Sendable {
    func load() -> StoredSession?
    func save(_ session: StoredSession) throws
    func clear()
}

/// Keychain: `kSecClassGenericPassword`, service `br.com.crowntech.pdv2gestao`,
/// account `session`, acessível só neste aparelho após o primeiro desbloqueio.
final class KeychainSessionStore: SessionStore, @unchecked Sendable {
    let service: String
    let account: String

    init(service: String = "br.com.crowntech.pdv2gestao", account: String = "session") {
        self.service = service
        self.account = account
    }

    private var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    func load() -> StoredSession? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return try? JSONDecoder().decode(StoredSession.self, from: data)
    }

    func save(_ session: StoredSession) throws {
        let data = try JSONEncoder().encode(session)
        SecItemDelete(baseQuery as CFDictionary)
        var attributes = baseQuery
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(attributes as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
    }

    func clear() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}

/// Usado nos testes e nos Previews.
final class InMemorySessionStore: SessionStore, @unchecked Sendable {
    private let lock = NSLock()
    private var value: StoredSession?

    init(_ value: StoredSession? = nil) { self.value = value }

    func load() -> StoredSession? { lock.withLock { value } }
    func save(_ session: StoredSession) throws { lock.withLock { value = session } }
    func clear() { lock.withLock { value = nil } }
}
