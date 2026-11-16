import Foundation
import LocalAuthentication

/// Trava da interface por Face ID/Touch ID (ou código do aparelho).
/// A chave do Keychain NÃO depende da biometria: a renovação continua
/// funcionando; a trava só esconde os dados até a pessoa se identificar.
protocol BiometricAuthenticating {
    /// "Face ID", "Touch ID" ou nil se o aparelho não tem biometria cadastrada.
    var biometryName: String? { get }
    /// O aparelho tem biometria ou código configurado.
    var canAuthenticate: Bool { get }
    func authenticate(reason: String) async -> Bool
}

struct SystemBiometrics: BiometricAuthenticating {
    var biometryName: String? {
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) else { return nil }
        switch context.biometryType {
        case .faceID: return "Face ID"
        case .touchID: return "Touch ID"
        case .opticID: return "Optic ID"
        default: return nil
        }
    }

    var canAuthenticate: Bool {
        var error: NSError?
        return LAContext().canEvaluatePolicy(.deviceOwnerAuthentication, error: &error)
    }

    func authenticate(reason: String) async -> Bool {
        let context = LAContext()
        context.localizedCancelTitle = "Cancelar"
        do {
            return try await context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason)
        } catch {
            return false
        }
    }
}

/// Para Previews e testes.
struct FakeBiometrics: BiometricAuthenticating {
    var biometryName: String? = "Face ID"
    var canAuthenticate = true
    var result = true
    func authenticate(reason: String) async -> Bool { result }
}

/// Regra da trava: na abertura a frio e ao voltar do segundo plano após 5 min.
enum BiometricLockPolicy {
    static let backgroundGrace: TimeInterval = 5 * 60

    static func shouldLock(enabled: Bool, backgroundedAt: Date?, now: Date) -> Bool {
        guard enabled, let backgroundedAt else { return false }
        return now.timeIntervalSince(backgroundedAt) >= backgroundGrace
    }
}
