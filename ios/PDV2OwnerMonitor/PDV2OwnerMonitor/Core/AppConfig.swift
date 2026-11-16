import Foundation
import UIKit

/// Configuração lida do Info.plist (vem do xcconfig de cada build).
struct AppConfig: Equatable {
    static let productionURL = "https://pdv2.crowntech.com.br"
    static let developmentURL = "http://127.0.0.1:8000"

    /// URL padrão da API (`PDV2APIBaseURL`).
    let defaultBaseURL: String
    /// Debug permite trocar o servidor na tela de login (`PDV2AllowServerOverride`).
    let allowServerOverride: Bool
    /// `true` só em build Debug: aceita `http` (servidor local).
    let allowsInsecureHTTP: Bool
    let marketingVersion: String
    let buildNumber: String

    static var isDebugBuild: Bool {
        #if DEBUG
        return true
        #else
        return false
        #endif
    }

    static func fromBundle(_ bundle: Bundle = .main) -> AppConfig {
        let info = bundle.infoDictionary ?? [:]
        let debug = isDebugBuild
        let rawURL = (info["PDV2APIBaseURL"] as? String)?.trimmingCharacters(in: .whitespaces) ?? ""
        let fallback = debug ? developmentURL : productionURL
        let override = (info["PDV2AllowServerOverride"] as? String).map { ["YES", "TRUE", "1"].contains($0.uppercased()) }
            ?? (info["PDV2AllowServerOverride"] as? Bool) ?? false
        return AppConfig(defaultBaseURL: rawURL.isEmpty || rawURL.contains("$(") ? fallback : rawURL,
                         allowServerOverride: debug && override,
                         allowsInsecureHTTP: debug,
                         marketingVersion: info["CFBundleShortVersionString"] as? String ?? "1.0.0",
                         buildNumber: info["CFBundleVersion"] as? String ?? "1")
    }

    /// Valida a URL do servidor. Fora de Debug só aceita `https`.
    static func validatedBaseURL(_ raw: String, allowInsecure: Bool) throws -> URL {
        var value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") { value.removeLast() }
        guard let url = URL(string: value), let scheme = url.scheme?.lowercased(),
              let host = url.host, !host.isEmpty, url.query == nil else {
            throw APIError.invalidServerURL("Endereço do servidor inválido.")
        }
        if scheme == "https" { return url }
        if scheme == "http" && allowInsecure { return url }
        throw APIError.invalidServerURL("O endereço do servidor precisa começar com https://.")
    }

    /// URL efetiva: a sobrescrita (só em Debug) ou a do Info.plist.
    func resolvedBaseURL(override: String?) throws -> URL {
        if allowServerOverride, let override, !override.trimmingCharacters(in: .whitespaces).isEmpty {
            return try AppConfig.validatedBaseURL(override, allowInsecure: allowsInsecureHTTP)
        }
        return try AppConfig.validatedBaseURL(defaultBaseURL, allowInsecure: allowsInsecureHTTP)
    }

    var versionText: String { "\(marketingVersion) (\(buildNumber))" }
}

/// Dados do aparelho enviados no login e no User-Agent.
struct DeviceDescriptor: Equatable {
    let name: String
    let model: String
    let osVersion: String
    let appVersion: String
    let marketingVersion: String

    @MainActor
    static func current(config: AppConfig) -> DeviceDescriptor {
        DeviceDescriptor(name: String(UIDevice.current.name.prefix(80)),
                         model: String(machineIdentifier().prefix(60)),
                         osVersion: "iOS \(UIDevice.current.systemVersion)",
                         appVersion: config.versionText,
                         marketingVersion: config.marketingVersion)
    }

    static func machineIdentifier() -> String {
        if let simulated = ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"] { return simulated }
        var info = utsname()
        uname(&info)
        let mirror = Mirror(reflecting: info.machine)
        return mirror.children.reduce(into: "") { result, element in
            guard let value = element.value as? Int8, value != 0 else { return }
            result.append(Character(UnicodeScalar(UInt8(value))))
        }
    }

    /// `PDV2Gestao/1.0.0 (iOS 26.1; iPhone16,2)`
    var userAgent: String { "PDV2Gestao/\(marketingVersion) (\(osVersion); \(model))" }

    var loginDevice: LoginRequest.Device {
        LoginRequest.Device(platform: "ios", name: name.isEmpty ? "iPhone" : name, model: model,
                            osVersion: String(osVersion.prefix(40)), appVersion: String(appVersion.prefix(40)))
    }
}
