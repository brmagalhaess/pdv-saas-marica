import Foundation

/// Modo demonstração: dados fictícios do "Mercadinho Exemplo", gerados no aparelho.
///
/// Serve para a revisão da App Store e para quem quer conhecer o app sem conta.
/// Nada aqui faz chamada de rede, grava token ou mexe no Keychain: tudo é calculado
/// em memória a partir da data de hoje e de uma semente fixa, para que a mesma
/// venda, o mesmo caixa e os mesmos totais apareçam iguais em todas as telas.
final class DemoGestaoAPI: GestaoAPI {
    private let now: () -> Date

    /// Vendas de dias que já terminaram não mudam: ficam guardadas em memória enquanto a
    /// demonstração está aberta, para a rolagem de "Mês" não refazer tudo a cada página.
    private let cacheLock = NSLock()
    private var pastMoments: [String: [Moment]] = [:]

    init(now: @escaping () -> Date = Date.init) { self.now = now }

    // MARK: Cadastro fictício

    struct Shop {
        let index: Int
        let id: String
        let name: String
        let tenantID: String
        /// Faturamento e número de vendas de um dia normal.
        let dailyRevenueCents: Int
        let dailySales: Int
        let terminals: [String]
        let operators: [String]
    }

    struct Product {
        let name: String
        let unit: String
        let priceCents: Int
    }

    static let mainTenantID = "de300000-0000-4000-8000-000000000001"
    static let secondTenantID = "de300000-0000-4000-8000-000000000002"
    static let mainTenantName = "Mercadinho Exemplo"
    static let secondTenantName = "Padaria Exemplo"

    static let shops: [Shop] = [
        Shop(index: 0, id: "de500000-0000-4000-8000-000000000001", name: "Loja Centro", tenantID: mainTenantID,
             dailyRevenueCents: 486_000, dailySales: 104, terminals: ["Caixa 1", "Caixa 2", "Caixa 3"],
             operators: ["Carla", "Rafael", "Juliana"]),
        Shop(index: 1, id: "de500000-0000-4000-8000-000000000002", name: "Loja Jardim", tenantID: mainTenantID,
             dailyRevenueCents: 312_000, dailySales: 71, terminals: ["Caixa 1"],
             operators: ["Marcos", "Patrícia"]),
        Shop(index: 2, id: "de500000-0000-4000-8000-000000000003", name: "Loja Única", tenantID: secondTenantID,
             dailyRevenueCents: 188_000, dailySales: 142, terminals: ["Balcão"],
             operators: ["Dona Rosa", "Felipe"]),
    ]

    static let groceries: [Product] = [
        Product(name: "Arroz tipo 1 5 kg", unit: "UN", priceCents: 2890),
        Product(name: "Feijão carioca 1 kg", unit: "UN", priceCents: 899),
        Product(name: "Café torrado 500 g", unit: "UN", priceCents: 1890),
        Product(name: "Açúcar refinado 1 kg", unit: "UN", priceCents: 549),
        Product(name: "Óleo de soja 900 ml", unit: "UN", priceCents: 799),
        Product(name: "Leite integral 1 L", unit: "UN", priceCents: 549),
        Product(name: "Pão francês", unit: "KG", priceCents: 1690),
        Product(name: "Banana prata", unit: "KG", priceCents: 699),
        Product(name: "Tomate", unit: "KG", priceCents: 799),
        Product(name: "Queijo muçarela", unit: "KG", priceCents: 4990),
        Product(name: "Macarrão espaguete 500 g", unit: "UN", priceCents: 489),
        Product(name: "Refrigerante 2 L", unit: "UN", priceCents: 999),
        Product(name: "Cerveja lata 350 ml", unit: "UN", priceCents: 429),
        Product(name: "Água mineral 1,5 L", unit: "UN", priceCents: 299),
        Product(name: "Biscoito recheado 140 g", unit: "UN", priceCents: 349),
        Product(name: "Detergente 500 ml", unit: "UN", priceCents: 249),
        Product(name: "Sabão em pó 1 kg", unit: "UN", priceCents: 1590),
        Product(name: "Papel higiênico 12 rolos", unit: "UN", priceCents: 2190),
    ]

    static let bakery: [Product] = [
        Product(name: "Pão francês", unit: "KG", priceCents: 1690),
        Product(name: "Pão de queijo", unit: "KG", priceCents: 5990),
        Product(name: "Bolo de fubá (fatia)", unit: "UN", priceCents: 650),
        Product(name: "Café coado", unit: "UN", priceCents: 450),
        Product(name: "Sonho de creme", unit: "UN", priceCents: 690),
        Product(name: "Presunto fatiado", unit: "KG", priceCents: 3990),
        Product(name: "Leite integral 1 L", unit: "UN", priceCents: 549),
    ]

    static let creditCustomers: [(name: String, phone: String, open: Int, overdue: Int, daysLate: Int, entries: Int)] = [
        ("João da Silva", "21900000001", 18_450, 9_200, 27, 4),
        ("Maria Aparecida", "21900000002", 12_300, 0, 0, 3),
        ("Antônio Pereira", "21900000003", 26_780, 14_500, 41, 6),
        ("Lourdes Santos", "21900000004", 7_990, 0, 0, 2),
        ("Bar do Zé", "21900000005", 41_200, 18_900, 19, 9),
        ("Cláudia Ribeiro", "21900000006", 5_430, 0, 0, 1),
        ("Sebastião Lima", "21900000007", 9_870, 4_350, 12, 3),
    ]

    // MARK: Datas

    static let timeZone = Format.timeZone

    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = Self.timeZone
        return calendar
    }

    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = timeZone
        return formatter
    }()

    private static let keyFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = timeZone
        formatter.dateFormat = "yyyyMMdd"
        return formatter
    }()

    private static let labelFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "pt_BR")
        formatter.timeZone = timeZone
        formatter.dateFormat = "dd/MM"
        return formatter
    }()

    private func iso(_ date: Date) -> String { Self.isoFormatter.string(from: date) }
    private func dayKey(_ date: Date) -> String { Self.keyFormatter.string(from: date) }
    private func ymd(_ date: Date) -> String { Format.ymd(date) }

    private func startOfDay(_ date: Date) -> Date { calendar.startOfDay(for: date) }

    private func at(_ day: Date, hour: Int, minute: Int = 0) -> Date {
        calendar.date(bySettingHour: hour, minute: minute, second: 0, of: day) ?? day
    }

    /// Horário de funcionamento das lojas fictícias.
    static let openingHours = Array(7...21)
    /// Peso de cada hora no faturamento do dia (soma 100).
    static let hourWeights = [2, 4, 6, 7, 8, 9, 8, 6, 6, 7, 9, 11, 9, 6, 2]

    /// Período em que a demonstração abre: "Hoje", ou "Ontem" antes das 9h (quando
    /// "Hoje" ainda tem pouca ou nenhuma venda). Nunca há venda depois do relógio.
    var initialPeriod: Period {
        calendar.component(.hour, from: now()) < 9 ? .yesterday : .today
    }

    /// Dias (início do dia) do período.
    private func days(for period: Period) -> [Date] {
        let today = startOfDay(now())
        switch period {
        case .today:
            return [today]
        case .yesterday:
            return [calendar.date(byAdding: .day, value: -1, to: today) ?? today]
        case .sevenDays:
            return (0..<7).reversed().compactMap { calendar.date(byAdding: .day, value: -$0, to: today) }
        case .month:
            let day = calendar.component(.day, from: today)
            return (0..<day).reversed().compactMap { calendar.date(byAdding: .day, value: -$0, to: today) }
        }
    }

    private func isToday(_ day: Date) -> Bool { calendar.isDate(day, inSameDayAs: now()) }

    // MARK: Números

    /// Variação estável (0,82 a 1,18) por chave, para os dias não ficarem iguais.
    private static func wobble(_ key: String) -> Double {
        var rng = SeededRandom(key)
        return 0.82 + Double(rng.next() % 3600) / 10_000
    }

    private func dayFactor(_ day: Date) -> Double {
        let weekday = calendar.component(.weekday, from: day)  // 1 = domingo
        switch weekday {
        case 1: return 0.7
        case 6: return 1.15
        case 7: return 1.3
        default: return 1
        }
    }

    private func hourRevenue(_ shop: Shop, _ day: Date, _ hour: Int) -> Int {
        let weight = Self.hourWeights[hour - 7]
        let base = Double(shop.dailyRevenueCents) * dayFactor(day) * Self.wobble("r|\(shop.id)|\(dayKey(day))")
        return Int((base * Double(weight) / 100 * Self.wobble("rh|\(shop.id)|\(dayKey(day))|\(hour)")).rounded())
    }

    private func hourCount(_ shop: Shop, _ day: Date, _ hour: Int) -> Int {
        let weight = Self.hourWeights[hour - 7]
        let base = Double(shop.dailySales) * dayFactor(day) * Self.wobble("c|\(shop.id)|\(dayKey(day))")
        return max(1, Int((base * Double(weight) / 100).rounded()))
    }

    /// Uma venda fictícia. Vendas, Resumo e Caixa saem todos desta mesma lista.
    struct Moment {
        let shop: Shop
        let date: Date
        /// Minuto do dia (hora * 60 + minuto): identifica a venda na loja e no dia.
        let seq: Int
        let status: String
        let terminalIndex: Int
        /// Parte do faturamento da hora que cabe a esta venda (só nas concluídas).
        let revenueCents: Int
    }

    /// Situação da venda, estável pelo identificador: ~1,8% canceladas e ~0,8% devolvidas.
    private static func saleStatus(shopIndex: Int, dayKey: String, seq: Int) -> String {
        var rng = SeededRandom("st|\(shopIndex)|\(dayKey)|\(seq)")
        let roll = rng.next() % 1000
        if roll < 18 { return "cancelled" }
        if roll < 26 { return "refunded" }
        return "completed"
    }

    /// Caixa da venda: o Caixa 3 do Centro só funciona de manhã (até 13h).
    private static func terminalIndex(_ shop: Shop, seq: Int) -> Int {
        let count = shop.terminals.count > 2 && seq >= 13 * 60 ? 2 : shop.terminals.count
        return seq % count
    }

    /// Vendas de uma loja num dia, da mais antiga para a mais nova. Hoje para no relógio:
    /// nenhuma venda fica no futuro.
    private func moments(_ shop: Shop, _ day: Date) -> [Moment] {
        let key = dayKey(day)
        let finished = day < startOfDay(now())
        let cacheKey = "\(shop.index)|\(key)"
        if finished, let cached = cacheLock.withLock({ pastMoments[cacheKey] }) { return cached }
        let result = buildMoments(shop, day, key: key)
        if finished { cacheLock.withLock { pastMoments[cacheKey] = result } }
        return result
    }

    private func buildMoments(_ shop: Shop, _ day: Date, key: String) -> [Moment] {
        let limit = isToday(day) ? now() : nil
        var result: [Moment] = []
        for hour in Self.openingHours {
            let count = min(hourCount(shop, day, hour), 60)
            let revenue = hourRevenue(shop, day, hour)
            var rng = SeededRandom("m|\(shop.id)|\(key)|\(hour)")
            let spread = max(1, 60 / count)
            for k in 0..<count {
                let minute = k * 60 / count + Int(rng.next() % UInt64(spread))
                let date = at(day, hour: hour, minute: minute)
                if let limit, date > limit { return result }
                let seq = hour * 60 + minute
                let status = Self.saleStatus(shopIndex: shop.index, dayKey: key, seq: seq)
                // Distribui o faturamento da hora pelas vendas concluídas, sem perder centavos.
                let share = revenue * (k + 1) / count - revenue * k / count
                result.append(Moment(shop: shop, date: date, seq: seq, status: status,
                                     terminalIndex: Self.terminalIndex(shop, seq: seq),
                                     revenueCents: status == "completed" ? share : 0))
            }
        }
        return result
    }

    private func shops(_ context: QueryContext) -> [Shop] {
        let all = Self.shops.filter { $0.tenantID == context.tenantID }
        guard let shopID = context.shopID else { return all }
        return all.filter { $0.id == shopID }
    }

    private func shopRef(_ shop: Shop) -> [String: Any] { ["id": shop.id, "name": shop.name] }

    private func tenantName(_ id: String) -> String {
        id == Self.secondTenantID ? Self.secondTenantName : Self.mainTenantName
    }

    // MARK: Conta

    private var deviceJSON: [String: Any] {
        let connected = calendar.date(byAdding: .day, value: -12, to: now()) ?? now()
        let os = ProcessInfo.processInfo.operatingSystemVersion
        return ["id": "de700000-0000-4000-8000-000000000001", "platform": "ios", "name": "iPhone de demonstração",
                "model": "iPhone", "os_version": "iOS \(os.majorVersion).\(os.minorVersion)", "app_version": "demonstração",
                "created_at": iso(connected), "last_seen_at": iso(now()), "current": true]
    }

    private func tenantJSON(_ id: String) -> [String: Any] {
        let shops = Self.shops.filter { $0.tenantID == id }.map(shopRef)
        return ["id": id, "name": tenantName(id), "all_shops": true, "shops": shops,
                "permissions": AppPermission.allCases.map(\.rawValue), "access_expires_on": NSNull()]
    }

    private var meJSON: [String: Any] {
        ["user": ["id": 1, "name": "Ana Exemplo", "email": "ana@example.com"],
         "device": deviceJSON,
         "platform": NSNull(),
         "tenants": [tenantJSON(Self.mainTenantID), tenantJSON(Self.secondTenantID)],
         "links": ["privacy_url": PublicLinks.production.privacyURL,
                   "support_url": PublicLinks.production.supportURL,
                   "account_deletion_url": PublicLinks.production.accountDeletionURL],
         "server_time": iso(now())]
    }

    /// Sessão fictícia usada ao entrar na demonstração.
    func demoMe() throws -> Me { try decode(meJSON) }

    // MARK: Resumo

    private func dashboardJSON(_ context: QueryContext) -> [String: Any] {
        let scope = shops(context)
        let periodDays = days(for: context.period)
        var revenue = 0
        var count = 0
        var points: [[String: Any]] = []
        let byHour = context.period == .today || context.period == .yesterday
        var cancelledCount = 0
        var cancelledCents = 0
        // Como no servidor: "vendas" e faturamento contam só as concluídas; canceladas e
        // devolvidas vão para "cancelamentos" (as mesmas que a aba Vendas mostra em "Canceladas").
        if byHour, let day = periodDays.first {
            let all = scope.flatMap { moments($0, day) }
            for hour in Self.openingHours {
                let inHour = all.filter { $0.seq / 60 == hour && $0.status == "completed" }
                let r = inHour.reduce(0) { $0 + $1.revenueCents }
                revenue += r
                count += inHour.count
                points.append(["key": String(format: "%02d", hour), "label": String(format: "%02dh", hour),
                               "count": inHour.count, "revenue_cents": r])
            }
            let reversed = all.filter { $0.status != "completed" }
            cancelledCount += reversed.count
            cancelledCents += reversed.reduce(0) { $0 + saleTotal($1) }
        } else {
            for day in periodDays {
                let all = scope.flatMap { moments($0, day) }
                let completed = all.filter { $0.status == "completed" }
                let r = completed.reduce(0) { $0 + $1.revenueCents }
                revenue += r
                count += completed.count
                let reversed = all.filter { $0.status != "completed" }
                cancelledCount += reversed.count
                cancelledCents += reversed.reduce(0) { $0 + saleTotal($1) }
                points.append(["key": ymd(day), "label": Self.labelFormatter.string(from: day),
                               "count": completed.count, "revenue_cents": r])
            }
        }
        let average = count > 0 ? revenue / count : 0
        let returns = revenue * 4 / 1000
        // Formas de pagamento: 2% fiado, 0,5% crédito da loja, o resto recebido na hora.
        let creditAccount = revenue * 2 / 100
        let storeCredit = revenue * 5 / 1000
        let received = revenue - creditAccount - storeCredit
        let pix = received * 43 / 100
        let cardCredit = received * 22 / 100
        let cardDebit = received * 18 / 100
        let other = received * 1 / 100
        let cash = received - pix - cardCredit - cardDebit - other

        let first = periodDays.first ?? startOfDay(now())
        let last = periodDays.last ?? first
        let periodJSON: [String: Any] = [
            "key": context.period.rawValue, "label": context.period.label,
            "date_from": ymd(first), "date_to": ymd(last),
            "from": iso(first), "to": iso(calendar.date(byAdding: .day, value: 1, to: last) ?? last),
        ]

        let terminals = terminalItems(context)
        let online = terminals.filter { ($0["status"] as? String) == "online" }.count
        let stock = stockItems(context)
        let credit = creditTotals()
        let open = openCashSessions(context).count
        let closed = closedCashSessions(context)
        let shortages = closed.filter { ($0["difference_cents"] as? Int ?? 0) < 0 }
        let tenantID = context.tenantID
        let allShops = Self.shops.filter { $0.tenantID == tenantID }

        return [
            "generated_at": iso(now()),
            "tenant": ["id": tenantID, "name": tenantName(tenantID)],
            "shop": scope.count == 1 && context.shopID != nil ? shopRef(scope[0]) : NSNull(),
            "shops": allShops.map(shopRef),
            "period": periodJSON,
            "permissions": AppPermission.allCases.map(\.rawValue),
            "sales": ["count": count, "revenue_cents": revenue, "average_ticket_cents": average,
                      "cancelled_count": cancelledCount, "cancelled_cents": cancelledCents,
                      "returns_cents": returns, "net_revenue_cents": revenue - returns],
            "payments": ["cash_cents": cash, "pix_cents": pix, "card_credit_cents": cardCredit,
                         "card_debit_cents": cardDebit, "other_cents": other, "received_total_cents": received,
                         "credit_account_cents": creditAccount, "store_credit_cents": storeCredit,
                         "credit_received_cents": revenue * 12 / 1000],
            "series": ["kind": byHour ? "hour" : "day", "points": points],
            "cash": ["open_count": open, "closed_count": closed.count, "shortage_count": shortages.count,
                     "shortage_cents": shortages.reduce(0) { $0 + ($1["difference_cents"] as? Int ?? 0) }],
            "stock": ["zeroed_count": stock.filter { ($0["status"] as? String) == "zeroed" }.count,
                      "below_minimum_count": stock.filter { ($0["status"] as? String) == "below_minimum" }.count],
            "credit": ["open_cents": credit.open, "overdue_cents": credit.overdue,
                       "overdue_customers": credit.overdueCustomers],
            "terminals": ["total": terminals.count, "online": online, "offline": terminals.count - online,
                          "unpaired": 0, "online_window_seconds": 300,
                          "conflicts_open": conflictItems(context).filter { ($0["status"] as? String) == "open" }.count],
        ]
    }

    // MARK: Vendas

    private static let pageSize = 25

    /// Vendas do período, da mais recente para a mais antiga: todas as do Resumo, sem corte.
    private func saleMoments(_ context: QueryContext) -> [Moment] {
        var result: [Moment] = []
        for day in days(for: context.period).reversed() {
            let sameDay = shops(context).flatMap { moments($0, day) }
            result += sameDay.sorted { a, b in
                a.date != b.date ? a.date > b.date : a.shop.index < b.shop.index
            }
        }
        return result
    }

    /// Total da venda (o mesmo do detalhe).
    private func saleTotal(_ moment: Moment) -> Int {
        (saleDetailJSON(id: saleID(moment.shop, moment.date, moment.seq))?["total_cents"] as? Int) ?? 0
    }

    private func saleID(_ shop: Shop, _ date: Date, _ seq: Int) -> String {
        "demo-venda-\(shop.index)-\(dayKey(date))-\(seq)"
    }

    private func saleDetailJSON(id: String) -> [String: Any]? {
        let parts = id.split(separator: "-")
        guard parts.count == 5, parts[0] == "demo", parts[1] == "venda",
              let shopIndex = Int(parts[2]), Self.shops.indices.contains(shopIndex),
              let day = Self.keyFormatter.date(from: String(parts[3])), let seq = Int(parts[4]) else { return nil }
        let shop = Self.shops[shopIndex]
        let created = at(day, hour: seq / 60, minute: seq % 60)
        var rng = SeededRandom(id)
        let catalog = shop.tenantID == Self.secondTenantID ? Self.bakery : Self.groceries
        let itemCount = 1 + Int(rng.next() % 6)
        var used = Set<Int>()
        var items: [[String: Any]] = []
        var gross = 0, promo = 0
        for line in 1...itemCount {
            var index = Int(rng.next() % UInt64(catalog.count))
            while used.contains(index) { index = (index + 1) % catalog.count }
            used.insert(index)
            let product = catalog[index]
            let quantity = product.unit == "KG" ? 250 + Int(rng.next() % 1500) : 1000 * (1 + Int(rng.next() % 3))
            let lineGross = product.priceCents * quantity / 1000
            let linePromo = rng.next() % 5 == 0 ? lineGross / 10 : 0
            gross += lineGross
            promo += linePromo
            items.append(["line": line, "name": product.name, "unit": product.unit, "quantity_milli": quantity,
                          "unit_price_cents": product.priceCents, "gross_cents": lineGross,
                          "promo_discount_cents": linePromo, "discount_cents": 0,
                          "line_total_cents": lineGross - linePromo])
        }
        let total = gross - promo
        let methods: [(String, String)] = [("pix", "Pix"), ("pix", "Pix"), ("card_debit", "Cartão de débito"),
                                           ("card_credit", "Cartão de crédito"), ("cash", "Dinheiro"),
                                           ("pix", "Pix"), ("card_credit", "Cartão de crédito"),
                                           ("cash", "Dinheiro"), ("credit_account", "Fiado")]
        let method = methods[Int(rng.next() % UInt64(methods.count))]
        let change = method.0 == "cash" ? (((total / 1000) + 1) * 1000 - total) % 1000 : 0
        let customer = method.0 == "credit_account"
            ? Self.creditCustomers[Int(rng.next() % UInt64(Self.creditCustomers.count))].name : ""
        let status = Self.saleStatus(shopIndex: shop.index, dayKey: String(parts[3]), seq: seq)
        let terminalIndex = Self.terminalIndex(shop, seq: seq)
        let terminal = shop.terminals[terminalIndex]
        let operatorName = shop.operators[terminalIndex % shop.operators.count]
        // Devolução 25 min depois, mas nunca depois do relógio.
        let later = iso(min(created.addingTimeInterval(25 * 60), max(created, now())))
        var returns: [[String: Any]] = []
        if status == "refunded" {
            returns.append(["id": "\(id)-devolucao", "created_at": later, "total_cents": total,
                            "refund_method": "cash", "refund_label": "Dinheiro", "reason": "Produto com defeito"])
        }
        return [
            "id": id, "created_at": iso(created), "received_at": iso(min(created.addingTimeInterval(3), max(created, now()))),
            "status": status, "shop": shopRef(shop), "terminal_name": terminal, "station_name": "",
            "operator_name": operatorName, "seller_name": "", "customer_name": customer,
            "gross_cents": gross, "promo_discount_cents": promo, "discount_cents": 0, "total_cents": total,
            "change_cents": change, "items": items,
            "payments": [["method": method.0, "label": method.1, "amount_cents": total]],
            "returns": returns,
            "cancelled_at": status == "cancelled" ? later : NSNull(),
            "cancellation_reason": status == "cancelled" ? "Cliente desistiu da compra" : "",
        ]
    }

    private func saleListJSON(_ detail: [String: Any]) -> [String: Any] {
        let payments = detail["payments"] as? [[String: Any]] ?? []
        return ["id": detail["id"] ?? "", "created_at": detail["created_at"] ?? "", "shop": detail["shop"] ?? [:],
                "terminal_name": detail["terminal_name"] ?? "", "station_name": "",
                "operator_name": detail["operator_name"] ?? "", "customer_name": detail["customer_name"] ?? "",
                "total_cents": detail["total_cents"] ?? 0, "status": detail["status"] ?? "completed",
                "items_count": (detail["items"] as? [Any])?.count ?? 0,
                "payment_methods": payments.compactMap { $0["method"] as? String }]
    }

    private func page(_ all: [[String: Any]], cursor: String?) -> [String: Any] {
        let start = Int(cursor ?? "") ?? 0
        let end = min(all.count, start + Self.pageSize)
        let slice = start < end ? Array(all[start..<end]) : []
        return ["results": slice, "next_cursor": end < all.count ? String(end) : NSNull()]
    }

    // MARK: Caixa

    private func openedAt(_ day: Date, terminalIndex: Int) -> Date {
        at(day, hour: 7, minute: 2 + terminalIndex * 9)
    }

    private func closedAt(_ shop: Shop, _ day: Date, terminalIndex: Int) -> Date {
        at(day, hour: isMorningOnly(shop, terminalIndex) ? 13 : 21, minute: 12)
    }

    /// O Caixa 3 do Centro só abre de manhã.
    private func isMorningOnly(_ shop: Shop, _ terminalIndex: Int) -> Bool {
        shop.terminals.count > 2 && terminalIndex == 2
    }

    /// Caixas abertos agora: já abriram hoje e (o da manhã) ainda não fecharam. Os demais
    /// ficam abertos até o fim do dia.
    private func isOpenNow(_ shop: Shop, terminalIndex: Int) -> Bool {
        let today = startOfDay(now())
        guard openedAt(today, terminalIndex: terminalIndex) <= now() else { return false }
        return !isMorningOnly(shop, terminalIndex) || closedAt(shop, today, terminalIndex: terminalIndex) > now()
    }

    private func openCashSessions(_ context: QueryContext) -> [[String: Any]] {
        let today = startOfDay(now())
        return shops(context).flatMap { shop in
            shop.terminals.indices.compactMap { offset -> [String: Any]? in
                guard isOpenNow(shop, terminalIndex: offset) else { return nil }
                return cashSessionJSON(shop: shop, terminalIndex: offset, day: today, open: true)
            }
        }
    }

    private func closedCashSessions(_ context: QueryContext) -> [[String: Any]] {
        var result: [[String: Any]] = []
        for day in days(for: context.period).reversed() {
            for shop in shops(context) {
                for offset in shop.terminals.indices {
                    // Hoje só aparece o que já fechou (o turno da manhã do Caixa 3, depois das 13h12).
                    if isToday(day) && (!isMorningOnly(shop, offset) || closedAt(shop, day, terminalIndex: offset) > now()) {
                        continue
                    }
                    result.append(cashSessionJSON(shop: shop, terminalIndex: offset, day: day, open: false))
                }
            }
        }
        return result
    }

    /// Sangria: no meio do turno, e só se já aconteceu.
    private func withdrawal(_ shop: Shop, _ day: Date, terminalIndex: Int, cashNet: Int) -> (cents: Int, at: Date)? {
        let time = at(day, hour: isMorningOnly(shop, terminalIndex) ? 11 : 14, minute: 30)
        guard cashNet > 40_000, time <= now() else { return nil }
        return (30_000, time)
    }

    private func cashSessionJSON(shop: Shop, terminalIndex: Int, day: Date, open: Bool) -> [String: Any] {
        // As vendas deste caixa são as mesmas da aba Vendas (mesmo caixa, só concluídas).
        let sales = moments(shop, day).filter { $0.terminalIndex == terminalIndex && $0.status == "completed" }
        let total = sales.reduce(0) { $0 + $1.revenueCents }
        let count = sales.count
        let cashNet = total * 15 / 100
        let opening = 20_000
        let withdrawn = withdrawal(shop, day, terminalIndex: terminalIndex, cashNet: cashNet)?.cents ?? 0
        let expected = opening + cashNet - withdrawn
        let key = "\(shop.index)-\(terminalIndex)-\(dayKey(day))"
        // Quebra de caixa: o turno da manhã do Caixa 3 sempre falta R$ 12,50; às vezes sobra R$ 3,00.
        let dayNumber = calendar.component(.day, from: day)
        let difference = isMorningOnly(shop, terminalIndex) ? -1250 : ((shop.index + terminalIndex + dayNumber) % 5 == 3 ? 300 : 0)
        return [
            "id": "demo-caixa-\(key)-\(open ? "aberto" : "fechado")",
            "shop": shopRef(shop), "terminal_name": shop.terminals[terminalIndex], "station_name": "",
            "operator_name": shop.operators[terminalIndex % shop.operators.count],
            "status": open ? "open" : "closed", "opened_at": iso(openedAt(day, terminalIndex: terminalIndex)),
            "closed_at": open ? NSNull() : iso(closedAt(shop, day, terminalIndex: terminalIndex)), "opening_cents": opening,
            "sales_count": count, "sales_total_cents": total, "cash_net_cents": cashNet,
            "supplied_cents": 0, "withdrawn_cents": withdrawn,
            "expected_cents": open ? NSNull() : expected,
            "counted_cents": open ? NSNull() : expected + difference,
            "difference_cents": open ? NSNull() : difference,
            "incomplete": false,
        ]
    }

    private func cashDetailJSON(id: String) -> [String: Any]? {
        let parts = id.split(separator: "-")
        guard parts.count == 6, parts[0] == "demo", parts[1] == "caixa",
              let shopIndex = Int(parts[2]), Self.shops.indices.contains(shopIndex),
              let terminalIndex = Int(parts[3]), Self.shops[shopIndex].terminals.indices.contains(terminalIndex),
              let day = Self.keyFormatter.date(from: String(parts[4])) else { return nil }
        let shop = Self.shops[shopIndex]
        var session = cashSessionJSON(shop: shop, terminalIndex: terminalIndex, day: day, open: parts[5] == "aberto")
        let total = session["sales_total_cents"] as? Int ?? 0
        let cashNet = session["cash_net_cents"] as? Int ?? 0
        let withdrawn = session["withdrawn_cents"] as? Int ?? 0
        let pix = total * 45 / 100
        let debit = total * 19 / 100
        let credit = total - cashNet - pix - debit
        let operatorName = session["operator_name"] as? String ?? ""
        session["closed_by_name"] = parts[5] == "aberto" ? "" : operatorName
        session["supervisor_name"] = parts[5] == "aberto" ? "" : "Ana Exemplo"
        session["credit_received_cents"] = total * 12 / 1000
        session["by_method"] = [
            ["method": "pix", "label": "Pix", "amount_cents": pix],
            ["method": "card_credit", "label": "Cartão de crédito", "amount_cents": credit],
            ["method": "card_debit", "label": "Cartão de débito", "amount_cents": debit],
            ["method": "cash", "label": "Dinheiro", "amount_cents": cashNet],
        ]
        let sangria = withdrawal(shop, day, terminalIndex: terminalIndex, cashNet: cashNet)
        session["movements"] = sangria.map { sangria in [[
            "id": "\(id)-sangria", "kind": "withdrawn", "label": "Sangria", "amount_cents": withdrawn,
            "reason": "Depósito no banco", "operator_name": operatorName,
            "created_at": iso(sangria.at),
        ]] } ?? []
        return session
    }

    // MARK: Estoque, fiado, terminais e conflitos

    private func stockItems(_ context: QueryContext) -> [[String: Any]] {
        let rows: [(shop: Int, name: String, sku: String, unit: String, quantity: Int, minimum: Int)] = [
            (0, "Leite integral 1 L", "7890000000011", "UN", 0, 24_000),
            (0, "Fermento biológico 10 g", "7890000000028", "UN", 0, 10_000),
            (1, "Café torrado 500 g", "7890000000035", "UN", 0, 12_000),
            (0, "Arroz tipo 1 5 kg", "7890000000042", "UN", 4_000, 15_000),
            (0, "Óleo de soja 900 ml", "7890000000059", "UN", 6_000, 24_000),
            (1, "Açúcar refinado 1 kg", "7890000000066", "UN", 5_000, 20_000),
            (1, "Papel higiênico 12 rolos", "7890000000073", "UN", 3_000, 10_000),
            (0, "Queijo muçarela", "2000000000014", "KG", 1_850, 5_000),
            (2, "Presunto fatiado", "2000000000021", "KG", 0, 3_000),
            (2, "Leite integral 1 L", "7890000000011", "UN", 6_000, 18_000),
        ]
        let scope = Set(shops(context).map(\.index))
        return rows.enumerated().filter { scope.contains($0.element.shop) }.map { offset, row in
            ["shop": shopRef(Self.shops[row.shop]),
             "product_id": String(format: "de600000-0000-4000-8000-%012d", offset + 1),
             "name": row.name, "sku": row.sku, "unit": row.unit, "quantity_milli": row.quantity,
             "minimum_milli": row.minimum, "status": row.quantity == 0 ? "zeroed" : "below_minimum"]
        }
    }

    private func creditTotals() -> (open: Int, overdue: Int, customers: Int, overdueCustomers: Int) {
        let list = Self.creditCustomers
        return (list.reduce(0) { $0 + $1.open }, list.reduce(0) { $0 + $1.overdue }, list.count,
                list.filter { $0.overdue > 0 }.count)
    }

    private func creditItems(status: String) -> [[String: Any]] {
        let today = startOfDay(now())
        return Self.creditCustomers.enumerated()
            .filter { status != "overdue" || $0.element.overdue > 0 }
            .sorted { $0.element.overdue > $1.element.overdue || ($0.element.overdue == $1.element.overdue && $0.element.open > $1.element.open) }
            .map { offset, customer in
                let due = calendar.date(byAdding: .day, value: customer.daysLate > 0 ? -customer.daysLate : 9, to: today) ?? today
                return ["customer_id": String(format: "de800000-0000-4000-8000-%012d", offset + 1),
                        "name": customer.name, "phone": customer.phone, "open_cents": customer.open,
                        "overdue_cents": customer.overdue, "oldest_due_date": ymd(due),
                        "entries_count": customer.entries]
            }
    }

    private func terminalItems(_ context: QueryContext) -> [[String: Any]] {
        shops(context).flatMap { shop in
            shop.terminals.enumerated().map { offset, name -> [String: Any] in
                // Online enquanto o caixa está aberto; fora disso, visto pela última vez ao fechar.
                let online = isOpenNow(shop, terminalIndex: offset)
                let today = startOfDay(now())
                let closedToday = closedAt(shop, today, terminalIndex: offset)
                let yesterday = calendar.date(byAdding: .day, value: -1, to: today) ?? today
                let lastClose = closedToday <= now() ? closedToday : closedAt(shop, yesterday, terminalIndex: offset)
                let seen = online ? now().addingTimeInterval(-Double(20 + offset * 35)) : lastClose.addingTimeInterval(120)
                // O Caixa 3 desligou depois do turno com eventos ainda por enviar.
                let pending = !online && isMorningOnly(shop, offset) && closedToday <= now() ? 3 : 0
                return ["id": String(format: "de900000-0000-4000-8000-%06d%06d", shop.index + 1, offset + 1),
                        "name": name, "shop": shopRef(shop), "status": online ? "online" : "offline",
                        "version": "2.1.0", "last_seen_at": iso(min(seen, now())), "pending_events": pending,
                        "error": ""]
            }
        }
    }

    private func conflictItems(_ context: QueryContext) -> [[String: Any]] {
        guard let centro = shops(context).first(where: { $0.index == 0 }) else { return [] }
        let today = startOfDay(now())
        let yesterday = calendar.date(byAdding: .day, value: -1, to: today) ?? today
        // Estoque negativo às 10h15 de hoje; antes disso, o de ontem no mesmo horário.
        let negativeStock = at(today, hour: 10, minute: 15) <= now() ? at(today, hour: 10, minute: 15) : at(yesterday, hour: 10, minute: 15)
        return [
            ["id": "dea00000-0000-4000-8000-000000000001", "created_at": iso(negativeStock),
             "shop": shopRef(centro), "terminal_name": "Caixa 3", "reason": "negative_stock",
             "reason_text": "Estoque negativo", "status": "open", "status_text": "Em aberto"],
            ["id": "dea00000-0000-4000-8000-000000000002", "created_at": iso(at(yesterday, hour: 16, minute: 40)),
             "shop": shopRef(centro), "terminal_name": "Caixa 2", "reason": "price_changed",
             "reason_text": "Preço alterado durante a venda", "status": "resolved", "status_text": "Resolvido"],
        ]
    }

    // MARK: Decodificação

    /// Os dicionários acima seguem o contrato. Se um modelo mudar e eles não, a tela mostra
    /// o erro comum ("não foi possível carregar") em vez de derrubar o app.
    private func decode<T: Decodable>(_ object: Any) throws -> T {
        do {
            let data = try JSONSerialization.data(withJSONObject: object)
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.server(status: 500, code: "demo_data", message: "Não foi possível montar os dados da demonstração.",
                                  retryAfter: nil)
        }
    }

    private static let notInDemo = APIError.server(status: 403, code: "demo_mode",
                                                   message: "Não disponível na demonstração.", retryAfter: nil)

    // MARK: GestaoAPI

    func me() async throws -> Me { try demoMe() }

    func tenants(query: String?, cursor: String?) async throws -> Page<TenantAccess> {
        try decode(["results": [tenantJSON(Self.mainTenantID), tenantJSON(Self.secondTenantID)], "next_cursor": NSNull()])
    }

    func dashboard(_ context: QueryContext) async throws -> Dashboard { try decode(dashboardJSON(context)) }

    func sales(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<SaleListItem> {
        // "Canceladas" inclui as devolvidas, como no servidor. Só a página pedida vira detalhe.
        let matching = saleMoments(context).filter { moment in
            switch status {
            case "all": return true
            case "cancelled": return moment.status != "completed"
            default: return moment.status == status
            }
        }
        let start = Int(cursor ?? "") ?? 0
        let end = min(matching.count, start + Self.pageSize)
        let slice = start < end ? Array(matching[start..<end]) : []
        let results = slice.compactMap { saleDetailJSON(id: saleID($0.shop, $0.date, $0.seq)) }.map(saleListJSON)
        let next: Any = end < matching.count ? String(end) : NSNull()
        return try decode(["results": results, "next_cursor": next])
    }

    func sale(id: String, tenantID: String) async throws -> SaleDetail {
        guard let json = saleDetailJSON(id: id) else { throw Self.notInDemo }
        return try decode(json)
    }

    func cashSessions(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<CashSession> {
        let all = status == "closed" ? closedCashSessions(context) : openCashSessions(context)
        return try decode(page(all, cursor: cursor))
    }

    func cashSession(id: String, tenantID: String) async throws -> CashSessionDetail {
        guard let json = cashDetailJSON(id: id) else { throw Self.notInDemo }
        return try decode(json)
    }

    func stockAlerts(_ context: QueryContext, status: String, cursor: String?) async throws -> StockAlertsPage {
        let all = stockItems(context)
        let filtered = all.filter { status == "all" || ($0["status"] as? String) == status }
        return try decode(["counts": ["zeroed": all.filter { ($0["status"] as? String) == "zeroed" }.count,
                                  "below_minimum": all.filter { ($0["status"] as? String) == "below_minimum" }.count],
                       "results": filtered, "next_cursor": NSNull()])
    }

    func credit(_ context: QueryContext, status: String, cursor: String?) async throws -> CreditPage {
        let totals = creditTotals()
        return try decode(["totals": ["open_cents": totals.open, "overdue_cents": totals.overdue,
                                  "customers": totals.customers, "overdue_customers": totals.overdueCustomers],
                       "results": creditItems(status: status), "next_cursor": NSNull()])
    }

    func terminals(_ context: QueryContext) async throws -> TerminalsPage {
        try decode(["online_window_seconds": 300, "results": terminalItems(context), "next_cursor": NSNull()])
    }

    func conflicts(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<SyncConflict> {
        let all = conflictItems(context).filter { status == "all" || ($0["status"] as? String) == status }
        return try decode(["results": all, "next_cursor": NSNull()])
    }

    func devices() async throws -> Page<AppDeviceInfo> {
        try decode(["results": [deviceJSON], "next_cursor": NSNull()])
    }

    func revokeDevice(id: String) async throws {}

    func requestAccountDeletion(note: String?) async throws -> DeletionResponse {
        try decode(["status": "received",
                "message": "Isto é só uma demonstração: nenhum pedido foi enviado. Com uma conta de verdade, o pedido de exclusão é feito por aqui mesmo."])
    }

    func platformTenants(query: String?, attentionOnly: Bool, cursor: String?) async throws -> Page<PlatformTenant> {
        Page(results: [], nextCursor: nil)
    }

    func platformAccess(status: String, query: String?, cursor: String?) async throws -> Page<AppAccessItem> {
        Page(results: [], nextCursor: nil)
    }

    func grantAccess(_ request: AccessGrantRequest) async throws -> AppAccessItem { throw Self.notInDemo }
    func revokeAccess(id: String, reason: String) async throws -> AppAccessItem { throw Self.notInDemo }
    func accessOptions(tenantID: String?) async throws -> AccessOptions { throw Self.notInDemo }

    func platformDevices(status: String, query: String?, cursor: String?) async throws -> Page<PlatformDevice> {
        Page(results: [], nextCursor: nil)
    }

    func revokePlatformDevice(id: String, reason: String) async throws -> PlatformDevice { throw Self.notInDemo }
}

/// Gerador pseudoaleatório com semente (SplitMix64): mesmo texto, mesma sequência.
struct SeededRandom {
    private var state: UInt64

    init(_ seed: String) {
        // FNV-1a do texto: estável entre execuções (ao contrário de `hashValue`).
        var hash: UInt64 = 0xcbf2_9ce4_8422_2325
        for byte in seed.utf8 {
            hash ^= UInt64(byte)
            hash = hash &* 0x0000_0100_0000_01b3
        }
        state = hash
    }

    mutating func next() -> UInt64 {
        state &+= 0x9e37_79b9_7f4a_7c15
        var z = state
        z = (z ^ (z >> 30)) &* 0xbf58_476d_1ce4_e5b9
        z = (z ^ (z >> 27)) &* 0x94d0_49bb_1331_11eb
        return z ^ (z >> 31)
    }
}
