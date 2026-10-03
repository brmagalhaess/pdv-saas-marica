import Foundation
import Combine

@MainActor
final class AppSession: ObservableObject {
    @Published private(set) var user: UserProfile?
    @Published private(set) var membership: Membership?
    @Published private(set) var snapshot: MonitoringSnapshot?
    @Published private(set) var isLoading = false
    @Published private(set) var isRefreshing = false
    @Published var message = ""
    @Published var apiBaseURL: String

    private var client: APIClient?
    private var pollingTask: Task<Void, Never>?
    private var tenantID: String?

    var isAuthenticated: Bool { user != nil && membership != nil && tenantID != nil }
    var companyName: String { membership?.tenantName ?? "Sua empresa" }

    init() {
        apiBaseURL = UserDefaults.standard.string(forKey: "pdv2.apiBaseURL") ?? "http://127.0.0.1:8000"
    }

    func login(email: String, password: String) async {
        message = ""
        isLoading = true
        defer { isLoading = false }
        do {
            let api = try APIClient(baseURLString: apiBaseURL)
            let response = try await api.login(email: email.trimmingCharacters(in: .whitespacesAndNewlines),
                                               password: password)
            guard let owner = response.memberships.first(where: { $0.role == "owner" }) else {
                throw OwnerAccessError()
            }
            self.client = api
            self.user = response.user
            self.membership = owner
            self.tenantID = owner.tenantID
            UserDefaults.standard.set(apiBaseURL, forKey: "pdv2.apiBaseURL")
            await refresh()
        } catch {
            message = (error as? LocalizedError)?.errorDescription ?? "Não foi possível entrar."
        }
    }

    func refresh() async {
        guard let client, let tenantID else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            snapshot = try await client.monitoring(tenantID: tenantID)
            message = ""
        } catch {
            if snapshot == nil {
                message = (error as? LocalizedError)?.errorDescription ?? "Não foi possível carregar o monitoramento."
            }
        }
    }

    func startMonitoring() {
        pollingTask?.cancel()
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }

    func stopMonitoring() { pollingTask?.cancel(); pollingTask = nil }

    func logout() async {
        stopMonitoring()
        await client?.logout()
        client?.clearCookies()
        client = nil
        user = nil
        membership = nil
        tenantID = nil
        snapshot = nil
        message = ""
    }
}

struct OwnerAccessError: LocalizedError {
    var errorDescription: String? {
        "Este aplicativo é exclusivo do proprietário da empresa. Use o login que possui o papel proprietário."
    }
}
