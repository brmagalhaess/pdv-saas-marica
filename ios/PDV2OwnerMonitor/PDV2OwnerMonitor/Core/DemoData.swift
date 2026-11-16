import Foundation

/// Modo demonstração: dados fictícios do "Mercadinho Exemplo", gerados no aparelho.
///
/// Serve para a revisão da App Store e para quem quer conhecer o app sem conta.
/// Nada aqui faz chamada de rede, grava token ou mexe no Keychain: tudo é calculado
/// em memória a partir da data de hoje e de uma semente fixa, para que a mesma
/// venda, o mesmo caixa e os mesmos totais apareçam iguais em todas as telas.
final class DemoGestaoAPI: GestaoAPI {
    private let now: () -> Date

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

    /// Até que hora "hoje" já tem vendas. Fica entre 11h e 21h para a demonstração
    /// nunca abrir vazia, mesmo de madrugada.
    private func lastHourToday() -> Int {
        let hour = calendar.component(.hour, from: now())
        return min(max(hour, 11), 21)
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

    /// Horas com venda num dia (hoje para na hora atual).
    private func hours(of day: Date) -> [Int] {
        isToday(day) ? Array(7...lastHourToday()) : Self.openingHours
    }

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
        ["user": ["id": 1, "name": "Ana Exemplo", "email": "ana@mercadinhoexemplo.com.br"],
         "device": deviceJSON,
         "platform": NSNull(),
         "tenants": [tenantJSON(Self.mainTenantID), tenantJSON(Self.secondTenantID)],
         "links": ["privacy_url": PublicLinks.production.privacyURL,
                   "support_url": PublicLinks.production.supportURL,
                   "account_deletion_url": PublicLinks.production.accountDeletionURL],
         "server_time": iso(now())]
    }

    /// Sessão fictícia usada ao entrar na demonstração.
    func demoMe() -> Me { decode(meJSON) }

    // MARK: Resumo

    private func dashboardJSON(_ context: QueryContext) -> [String: Any] {
        let scope = shops(context)
        let periodDays = days(for: context.period)
        var revenue = 0
        var count = 0
        var points: [[String: Any]] = []
        let byHour = context.period == .today || context.period == .yesterday
        if byHour, let day = periodDays.first {
            for hour in Self.openingHours {
                let has = hours(of: day).contains(hour)
                let r = has ? scope.reduce(0) { $0 + hourRevenue($1, day, hour) } : 0
                let c = has ? scope.reduce(0) { $0 + hourCount($1, day, hour) } : 0
                revenue += r
                count += c
                points.append(["key": String(format: "%02d", hour), "label": String(format: "%02dh", hour),
                               "count": c, "revenue_cents": r])
            }
        } else {
            for day in periodDays {
                var r = 0, c = 0
                for shop in scope {
                    for hour in hours(of: day) {
                        r += hourRevenue(shop, day, hour)
                        c += hourCount(shop, day, hour)
                    }
                }
                revenue += r
                count += c
                points.append(["key": ymd(day), "label": Self.labelFormatter.string(from: day),
                               "count": c, "revenue_cents": r])
            }
        }
        let cancelledCount = max(scope.isEmpty ? 0 : 1, count / 45)
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
                      "cancelled_count": cancelledCount, "cancelled_cents": cancelledCount * average * 8 / 10,
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
    private static let maxSales = 75

    /// Momentos das vendas do período, da mais recente para a mais antiga.
    private func saleMoments(_ context: QueryContext) -> [(shop: Shop, date: Date, seq: Int)] {
        let scope = shops(context)
        guard !scope.isEmpty else { return [] }
        var result: [(Shop, Date, Int)] = []
        var rng = SeededRandom("moments|\(context.tenantID)|\(context.shopID ?? "all")|\(context.period.rawValue)|\(dayKey(now()))")
        for day in days(for: context.period).reversed() {
            let lastHour = hours(of: day).last ?? 21
            var cursor = at(day, hour: lastHour, minute: 48)
            if isToday(day), calendar.component(.hour, from: now()) == lastHour {
                cursor = min(cursor, now().addingTimeInterval(-90))
            }
            let opening = at(day, hour: 7, minute: 2)
            while cursor > opening && result.count < Self.maxSales {
                let shop = scope[Int(rng.next() % UInt64(scope.count))]
                let seq = calendar.component(.hour, from: cursor) * 60 + calendar.component(.minute, from: cursor)
                result.append((shop, cursor, seq))
                cursor = cursor.addingTimeInterval(-Double(150 + Int(rng.next() % 540)))
            }
            if result.count >= Self.maxSales { break }
        }
        return result
    }

    private func saleID(_ shop: Shop, _ date: Date, _ seq: Int) -> String {
        "demo-venda-\(shop.index)-\(dayKey(date))-\(seq)"
    }

    private func status(forSeq seq: Int, shop: Shop) -> String {
        let mix = (seq * 7 + shop.index * 3) % 41
        if mix == 5 { return "cancelled" }
        if mix == 17 { return "refunded" }
        return "completed"
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
        let status = status(forSeq: seq, shop: shop)
        // Caixa 3 do Centro só funciona de manhã.
        let terminalCount = shop.terminals.count > 2 && seq >= 13 * 60 ? 2 : shop.terminals.count
        let terminal = shop.terminals[seq % terminalCount]
        let operatorName = shop.operators[(seq % terminalCount) % shop.operators.count]
        let later = iso(created.addingTimeInterval(25 * 60))
        var returns: [[String: Any]] = []
        if status == "refunded" {
            returns.append(["id": "\(id)-devolucao", "created_at": later, "total_cents": total,
                            "refund_method": "cash", "refund_label": "Dinheiro", "reason": "Produto com defeito"])
        }
        return [
            "id": id, "created_at": iso(created), "received_at": iso(created.addingTimeInterval(3)),
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

    private func openCashSessions(_ context: QueryContext) -> [[String: Any]] {
        let today = startOfDay(now())
        return shops(context).flatMap { shop in
            shop.terminals.enumerated().compactMap { offset, terminal -> [String: Any]? in
                // O Caixa 3 do Centro só abre de manhã e já fechou.
                if shop.terminals.count > 2 && offset == 2 { return nil }
                return cashSessionJSON(shop: shop, terminalIndex: offset, day: today, open: true)
            }
        }
    }

    private func closedCashSessions(_ context: QueryContext) -> [[String: Any]] {
        var result: [[String: Any]] = []
        for day in days(for: context.period).reversed() {
            for shop in shops(context) {
                for offset in shop.terminals.indices {
                    let morningOnly = shop.terminals.count > 2 && offset == 2
                    if isToday(day) && !morningOnly { continue }
                    if isToday(day) && lastHourToday() < 13 { continue }
                    result.append(cashSessionJSON(shop: shop, terminalIndex: offset, day: day, open: false))
                }
            }
        }
        return result
    }

    private func cashSessionJSON(shop: Shop, terminalIndex: Int, day: Date, open: Bool) -> [String: Any] {
        let morningOnly = shop.terminals.count > 2 && terminalIndex == 2
        let openedAt = at(day, hour: 7, minute: 2 + terminalIndex * 9)
        let closingHour = morningOnly ? 13 : 21
        let closedAt = at(day, hour: closingHour, minute: 12)
        let hoursWorked = hours(of: day).filter { !morningOnly || $0 < closingHour }
        // Cada caixa fica com uma parte diferente do movimento da loja.
        let weights: [Double] = shop.terminals.count > 2 ? [0.46, 0.34, 0.20] : [1.0 / Double(max(1, shop.terminals.count))]
        let share = weights[min(terminalIndex, weights.count - 1)]
        let total = Int(Double(hoursWorked.reduce(0) { $0 + hourRevenue(shop, day, $1) }) * share)
        let count = max(1, Int(Double(hoursWorked.reduce(0) { $0 + hourCount(shop, day, $1) }) * share))
        let cashNet = total * 15 / 100
        let opening = 20_000
        let withdrawn = cashNet > 40_000 ? 30_000 : 0
        let expected = opening + cashNet - withdrawn
        let key = "\(shop.index)-\(terminalIndex)-\(dayKey(day))"
        // Quebra de caixa: o turno da manhã do Caixa 3 sempre falta R$ 12,50; às vezes sobra R$ 3,00.
        let dayNumber = calendar.component(.day, from: day)
        let difference = morningOnly ? -1250 : ((shop.index + terminalIndex + dayNumber) % 5 == 3 ? 300 : 0)
        return [
            "id": "demo-caixa-\(key)-\(open ? "aberto" : "fechado")",
            "shop": shopRef(shop), "terminal_name": shop.terminals[terminalIndex], "station_name": "",
            "operator_name": shop.operators[terminalIndex % shop.operators.count],
            "status": open ? "open" : "closed", "opened_at": iso(openedAt),
            "closed_at": open ? NSNull() : iso(closedAt), "opening_cents": opening,
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
        session["movements"] = withdrawn > 0 ? [[
            "id": "\(id)-sangria", "kind": "withdrawn", "label": "Sangria", "amount_cents": withdrawn,
            "reason": "Depósito no banco", "operator_name": operatorName,
            "created_at": iso(at(day, hour: 14, minute: 30)),
        ]] : []
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
                let offline = shop.terminals.count > 2 && offset == 2
                let seen = offline ? at(startOfDay(now()), hour: 13, minute: 14) : now().addingTimeInterval(-Double(20 + offset * 35))
                return ["id": String(format: "de900000-0000-4000-8000-%06d%06d", shop.index + 1, offset + 1),
                        "name": name, "shop": shopRef(shop), "status": offline ? "offline" : "online",
                        "version": "2.1.0", "last_seen_at": iso(min(seen, now())), "pending_events": offline ? 3 : 0,
                        "error": ""]
            }
        }
    }

    private func conflictItems(_ context: QueryContext) -> [[String: Any]] {
        guard let centro = shops(context).first(where: { $0.index == 0 }) else { return [] }
        let today = startOfDay(now())
        let yesterday = calendar.date(byAdding: .day, value: -1, to: today) ?? today
        return [
            ["id": "dea00000-0000-4000-8000-000000000001", "created_at": iso(at(today, hour: 10, minute: 15)),
             "shop": shopRef(centro), "terminal_name": "Caixa 3", "reason": "negative_stock",
             "reason_text": "Estoque negativo", "status": "open", "status_text": "Em aberto"],
            ["id": "dea00000-0000-4000-8000-000000000002", "created_at": iso(at(yesterday, hour: 16, minute: 40)),
             "shop": shopRef(centro), "terminal_name": "Caixa 2", "reason": "price_changed",
             "reason_text": "Preço alterado durante a venda", "status": "resolved", "status_text": "Resolvido"],
        ]
    }

    // MARK: Decodificação

    private func decode<T: Decodable>(_ object: Any) -> T {
        // Os dicionários acima seguem o contrato; um erro aqui é bug do modo demonstração.
        let data = try! JSONSerialization.data(withJSONObject: object)
        return try! JSONDecoder().decode(T.self, from: data)
    }

    private static let notInDemo = APIError.server(status: 403, code: "demo_mode",
                                                   message: "Não disponível na demonstração.", retryAfter: nil)

    // MARK: GestaoAPI

    func me() async throws -> Me { demoMe() }

    func tenants(query: String?, cursor: String?) async throws -> Page<TenantAccess> {
        decode(["results": [tenantJSON(Self.mainTenantID), tenantJSON(Self.secondTenantID)], "next_cursor": NSNull()])
    }

    func dashboard(_ context: QueryContext) async throws -> Dashboard { decode(dashboardJSON(context)) }

    func sales(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<SaleListItem> {
        let all = saleMoments(context)
            .compactMap { saleDetailJSON(id: saleID($0.shop, $0.date, $0.seq)) }
            .filter { status == "all" || ($0["status"] as? String) == status }
            .map(saleListJSON)
        return decode(page(all, cursor: cursor))
    }

    func sale(id: String, tenantID: String) async throws -> SaleDetail {
        guard let json = saleDetailJSON(id: id) else { throw Self.notInDemo }
        return decode(json)
    }

    func cashSessions(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<CashSession> {
        let all = status == "closed" ? closedCashSessions(context) : openCashSessions(context)
        return decode(page(all, cursor: cursor))
    }

    func cashSession(id: String, tenantID: String) async throws -> CashSessionDetail {
        guard let json = cashDetailJSON(id: id) else { throw Self.notInDemo }
        return decode(json)
    }

    func stockAlerts(_ context: QueryContext, status: String, cursor: String?) async throws -> StockAlertsPage {
        let all = stockItems(context)
        let filtered = all.filter { status == "all" || ($0["status"] as? String) == status }
        return decode(["counts": ["zeroed": all.filter { ($0["status"] as? String) == "zeroed" }.count,
                                  "below_minimum": all.filter { ($0["status"] as? String) == "below_minimum" }.count],
                       "results": filtered, "next_cursor": NSNull()])
    }

    func credit(_ context: QueryContext, status: String, cursor: String?) async throws -> CreditPage {
        let totals = creditTotals()
        return decode(["totals": ["open_cents": totals.open, "overdue_cents": totals.overdue,
                                  "customers": totals.customers, "overdue_customers": totals.overdueCustomers],
                       "results": creditItems(status: status), "next_cursor": NSNull()])
    }

    func terminals(_ context: QueryContext) async throws -> TerminalsPage {
        decode(["online_window_seconds": 300, "results": terminalItems(context), "next_cursor": NSNull()])
    }

    func conflicts(_ context: QueryContext, status: String, cursor: String?) async throws -> Page<SyncConflict> {
        let all = conflictItems(context).filter { status == "all" || ($0["status"] as? String) == status }
        return decode(["results": all, "next_cursor": NSNull()])
    }

    func devices() async throws -> Page<AppDeviceInfo> {
        decode(["results": [deviceJSON], "next_cursor": NSNull()])
    }

    func revokeDevice(id: String) async throws {}

    func requestAccountDeletion(note: String?) async throws -> DeletionResponse {
        decode(["status": "received",
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
