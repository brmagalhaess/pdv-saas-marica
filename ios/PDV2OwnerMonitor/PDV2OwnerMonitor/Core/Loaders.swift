import Foundation

/// Carrega um objeto (Resumo, detalhes). Mantém os dados antigos se a
/// atualização falhar, para a tela mostrar a faixa "Mostrando dados de HH:MM".
@MainActor
final class LoadState<Value>: ObservableObject {
    @Published private(set) var value: Value?
    @Published private(set) var error: ScreenError?
    @Published private(set) var isLoading = false
    @Published private(set) var loadedAt: Date?
    private var lastKey: AnyHashable?
    private var generation = 0

    init(value: Value? = nil) {
        self.value = value
        if value != nil { loadedAt = Date() }
    }

    func reload(app: AppState, _ operation: () async throws -> Value) async {
        generation += 1
        let current = generation
        isLoading = true
        do {
            let result = try await operation()
            guard current == generation else { return }
            value = result
            error = nil
            loadedAt = Date()
        } catch {
            guard current == generation else { return }
            if let screen = app.handle(error) { self.error = screen }
        }
        if current == generation { isLoading = false }
    }

    /// Chamado pelo `.task(id:)`: troca de filtro limpa; voltar à aba só recarrega se passou 1 min.
    func ensureLoaded(key: AnyHashable, app: AppState, _ operation: () async throws -> Value) async {
        if key != lastKey {
            lastKey = key
            reset()
            await reload(app: app, operation)
        } else if loadedAt == nil || Date().timeIntervalSince(loadedAt ?? .distantPast) > 60 {
            await reload(app: app, operation)
        }
    }

    func reset() {
        generation += 1
        value = nil
        error = nil
        loadedAt = nil
        isLoading = false
    }
}

/// Lista com rolagem infinita por `next_cursor`.
@MainActor
final class PagedState<Item: Identifiable>: ObservableObject {
    typealias Fetch = (String?) async throws -> Page<Item>

    @Published private(set) var items: [Item] = []
    @Published private(set) var nextCursor: String?
    @Published private(set) var error: ScreenError?
    @Published private(set) var isLoading = false
    @Published private(set) var isLoadingMore = false
    @Published private(set) var loadedAt: Date?
    private var lastKey: AnyHashable?
    private var generation = 0

    init(items: [Item] = []) {
        self.items = items
        if !items.isEmpty { loadedAt = Date() }
    }

    var hasLoaded: Bool { loadedAt != nil || error != nil }

    func reload(app: AppState, fetch: Fetch) async {
        generation += 1
        let current = generation
        isLoading = true
        do {
            let page = try await fetch(nil)
            guard current == generation else { return }
            items = page.results
            nextCursor = page.nextCursor
            error = nil
            loadedAt = Date()
        } catch {
            guard current == generation else { return }
            if let screen = app.handle(error) { self.error = screen }
        }
        if current == generation { isLoading = false }
    }

    func ensureLoaded(key: AnyHashable, app: AppState, fetch: Fetch) async {
        if key != lastKey {
            lastKey = key
            reset()
            await reload(app: app, fetch: fetch)
        } else if loadedAt == nil || Date().timeIntervalSince(loadedAt ?? .distantPast) > 60 {
            await reload(app: app, fetch: fetch)
        }
    }

    func loadMore(app: AppState, fetch: Fetch) async {
        guard let cursor = nextCursor, !isLoadingMore, !isLoading else { return }
        let current = generation
        isLoadingMore = true
        defer { if current == generation { isLoadingMore = false } }
        do {
            let page = try await fetch(cursor)
            guard current == generation else { return }
            let known = Set(items.map(\.id))
            items.append(contentsOf: page.results.filter { !known.contains($0.id) })
            nextCursor = page.nextCursor
        } catch {
            guard current == generation else { return }
            if let screen = app.handle(error) { self.error = screen }
        }
    }

    func reset() {
        generation += 1
        items = []
        nextCursor = nil
        error = nil
        loadedAt = nil
        isLoading = false
        isLoadingMore = false
    }

    /// Troca um item depois de uma ação (ex.: acesso revogado).
    func replace(_ item: Item) {
        if let index = items.firstIndex(where: { $0.id == item.id }) { items[index] = item }
    }

    func remove(id: Item.ID) {
        items.removeAll { $0.id == id }
    }
}
