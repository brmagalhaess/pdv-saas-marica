package br.com.crowntech.pdv2gestao.core.demo

import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.format.Labels
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.core.model.CashMethod
import br.com.crowntech.pdv2gestao.core.model.CashMovement
import br.com.crowntech.pdv2gestao.core.model.CashSession
import br.com.crowntech.pdv2gestao.core.model.CashSummary
import br.com.crowntech.pdv2gestao.core.model.Conflict
import br.com.crowntech.pdv2gestao.core.model.CreditCustomer
import br.com.crowntech.pdv2gestao.core.model.CreditSummary
import br.com.crowntech.pdv2gestao.core.model.CreditTotals
import br.com.crowntech.pdv2gestao.core.model.Dashboard
import br.com.crowntech.pdv2gestao.core.model.Device
import br.com.crowntech.pdv2gestao.core.model.Links
import br.com.crowntech.pdv2gestao.core.model.Me
import br.com.crowntech.pdv2gestao.core.model.PaymentsSummary
import br.com.crowntech.pdv2gestao.core.model.Period
import br.com.crowntech.pdv2gestao.core.model.SaleDetail
import br.com.crowntech.pdv2gestao.core.model.SaleItem
import br.com.crowntech.pdv2gestao.core.model.SalePayment
import br.com.crowntech.pdv2gestao.core.model.SaleReturn
import br.com.crowntech.pdv2gestao.core.model.SaleRow
import br.com.crowntech.pdv2gestao.core.model.SalesSummary
import br.com.crowntech.pdv2gestao.core.model.Series
import br.com.crowntech.pdv2gestao.core.model.SeriesPoint
import br.com.crowntech.pdv2gestao.core.model.ShopRef
import br.com.crowntech.pdv2gestao.core.model.StockAlert
import br.com.crowntech.pdv2gestao.core.model.StockSummary
import br.com.crowntech.pdv2gestao.core.model.TenantAccess
import br.com.crowntech.pdv2gestao.core.model.TenantRef
import br.com.crowntech.pdv2gestao.core.model.Terminal
import br.com.crowntech.pdv2gestao.core.model.TerminalsSummary
import br.com.crowntech.pdv2gestao.core.model.UserRef
import kotlinx.datetime.DatePeriod
import kotlinx.datetime.DateTimeUnit
import kotlinx.datetime.Instant
import kotlinx.datetime.LocalDate
import kotlinx.datetime.LocalDateTime
import kotlinx.datetime.LocalTime
import kotlinx.datetime.atTime
import kotlinx.datetime.minus
import kotlinx.datetime.plus
import kotlinx.datetime.toInstant
import kotlinx.datetime.toLocalDateTime
import kotlin.random.Random
import kotlin.time.Duration.Companion.hours
import kotlin.time.Duration.Companion.minutes
import kotlin.time.Duration.Companion.seconds

/**
 * Dados fictícios do modo demonstração ("Mercadinho Exemplo").
 * Tudo é calculado na hora a partir de [now] com sementes fixas: as mesmas vendas
 * alimentam o Resumo, a lista de Vendas, o detalhe e os Caixas, então os números batem.
 * Nada aqui toca rede, disco ou Keychain/Keystore.
 */
class DemoData(private val now: () -> Instant) {

    // ---------- Cadastro fixo ----------

    data class DemoShop(val id: String, val name: String, val tenantId: String, val baseSales: Int, val terminals: List<DemoTerminal>)
    data class DemoTerminal(val id: String, val name: String, val operator: String, val online: Boolean, val version: String = "2.1.0")
    data class Product(val id: String, val name: String, val sku: String, val unit: String, val priceCents: Long)

    private val zone = Format.zone

    val shops: List<DemoShop> = listOf(
        DemoShop(
            SHOP_CENTRO, "Loja Centro", TENANT_MERCADINHO, 68,
            listOf(
                DemoTerminal("demo-term-centro-1", "Caixa 1", "Carla Mendes", online = true),
                DemoTerminal("demo-term-centro-2", "Caixa 2", "Rafael Lima", online = true),
            ),
        ),
        DemoShop(
            SHOP_JARDIM, "Loja Jardim", TENANT_MERCADINHO, 44,
            listOf(
                DemoTerminal("demo-term-jardim-1", "Caixa 1", "Juliana Rocha", online = true),
                DemoTerminal("demo-term-jardim-2", "Caixa 2", "Pedro Alves", online = false, version = "2.0.4"),
            ),
        ),
        DemoShop(
            SHOP_EMPORIO, "Loja Matriz", TENANT_EMPORIO, 30,
            listOf(DemoTerminal("demo-term-emporio-1", "Caixa 1", "Sônia Prado", online = true)),
        ),
    )

    val tenants: List<TenantAccess> = listOf(
        TenantAccess(
            id = TENANT_MERCADINHO, name = "Mercadinho Exemplo", allShops = true,
            shops = shops.filter { it.tenantId == TENANT_MERCADINHO }.map { ShopRef(it.id, it.name) },
            permissions = PERMISSIONS,
        ),
        TenantAccess(
            id = TENANT_EMPORIO, name = "Empório Exemplo", allShops = true,
            shops = shops.filter { it.tenantId == TENANT_EMPORIO }.map { ShopRef(it.id, it.name) },
            permissions = PERMISSIONS,
        ),
    )

    private val products = listOf(
        Product("p01", "Arroz tipo 1 5 kg", "2100001000011", "UN", 2899),
        Product("p02", "Feijão carioca 1 kg", "2100001000028", "UN", 849),
        Product("p03", "Café torrado 500 g", "2100001000035", "UN", 1890),
        Product("p04", "Açúcar refinado 1 kg", "2100001000042", "UN", 499),
        Product("p05", "Óleo de soja 900 ml", "2100001000059", "UN", 789),
        Product("p06", "Leite integral 1 L", "2100001000066", "UN", 549),
        Product("p07", "Pão francês", "2000000000071", "KG", 1599),
        Product("p08", "Banana prata", "2000000000088", "KG", 699),
        Product("p09", "Tomate", "2000000000095", "KG", 899),
        Product("p10", "Queijo muçarela", "2000000000101", "KG", 4990),
        Product("p11", "Refrigerante cola 2 L", "2100001000110", "UN", 999),
        Product("p12", "Água de coco 1 L", "2100001000127", "UN", 429),
        Product("p13", "Sabão em pó 1 kg", "2100001000134", "UN", 1490),
        Product("p14", "Papel higiênico 12 rolos", "2100001000141", "UN", 2190),
        Product("p15", "Detergente 500 ml", "2100001000158", "UN", 279),
        Product("p16", "Ovos brancos dúzia", "2100001000165", "UN", 1290),
        Product("p17", "Macarrão espaguete 500 g", "2100001000172", "UN", 469),
        Product("p18", "Biscoito recheado 140 g", "2100001000189", "UN", 349),
        Product("p19", "Iogurte natural 170 g", "2100001000196", "UN", 329),
        Product("p20", "Água mineral 1,5 L", "2100001000202", "UN", 299),
    )

    private val customers = listOf(
        "Maria das Graças", "João Batista", "Dona Cida", "Antônio Ferreira", "Luciana Martins",
        "Seu Zé da Padaria", "Patrícia Gomes", "Roberto Nunes",
    )

    fun shop(id: String?): DemoShop? = shops.firstOrNull { it.id == id }
    fun tenant(id: String?): TenantAccess? = tenants.firstOrNull { it.id == id }

    /** Lojas do escopo: uma loja, ou todas da empresa. */
    fun shopsIn(tenantId: String, shopId: String?): List<DemoShop> =
        shops.filter { it.tenantId == tenantId && (shopId == null || it.id == shopId) }

    // ---------- Tempo ----------

    fun localNow(): LocalDateTime = now().toLocalDateTime(zone)
    fun today(): LocalDate = localNow().date

    private fun at(date: LocalDate, minuteOfDay: Int): Instant =
        date.atTime(LocalTime(minuteOfDay / 60, minuteOfDay % 60)).toInstant(zone)

    /** Mesmo formato do servidor: "2026-11-16T14:03:12-03:00". */
    fun iso(instant: Instant): String {
        val d = instant.toLocalDateTime(zone)
        fun two(n: Int) = n.toString().padStart(2, '0')
        return "${d.year}-${two(d.monthNumber)}-${two(d.dayOfMonth)}T${two(d.hour)}:${two(d.minute)}:${two(d.second)}-03:00"
    }

    /**
     * Período com que a demonstração abre. As lojas fictícias abrem às 07:00 (Brasília):
     * antes disso o "Hoje" ainda está vazio, então a demonstração começa em "Ontem",
     * que tem o dia inteiro de vendas e caixas fechados (revisores das lojas costumam abrir de madrugada).
     */
    fun initialPeriod(): PeriodKey = if (localNow().hour < OPEN_HOUR) PeriodKey.YESTERDAY else PeriodKey.TODAY

    fun dateRange(period: PeriodKey): List<LocalDate> {
        val today = today()
        val from = when (period) {
            PeriodKey.TODAY -> today
            PeriodKey.YESTERDAY -> today.minus(1, DateTimeUnit.DAY)
            PeriodKey.SEVEN_DAYS -> today.minus(6, DateTimeUnit.DAY)
            PeriodKey.MONTH -> LocalDate(today.year, today.monthNumber, 1)
        }
        val to = if (period == PeriodKey.YESTERDAY) from else today
        val days = mutableListOf<LocalDate>()
        var d = from
        while (d <= to) { days += d; d = d.plus(1, DateTimeUnit.DAY) }
        return days
    }

    fun period(period: PeriodKey): Period {
        val days = dateRange(period)
        val first = days.first()
        val last = days.last()
        return Period(
            key = period.key,
            label = period.label,
            dateFrom = first.toString(),
            dateTo = last.toString(),
            from = iso(at(first, 0)),
            to = iso(last.plus(DatePeriod(days = 1)).atTime(LocalTime(0, 0)).toInstant(zone)),
        )
    }

    // ---------- Vendas (geradas por loja e dia, sempre iguais) ----------

    data class GenSale(
        val id: String,
        val shop: DemoShop,
        val terminal: DemoTerminal,
        val at: Instant,
        val items: List<SaleItem>,
        val payments: List<SalePayment>,
        val customer: String,
        val status: String,
        val discountCents: Long,
        val totalCents: Long,
        val changeCents: Long,
    )

    private val hourWeights = mapOf(
        7 to 4, 8 to 8, 9 to 10, 10 to 11, 11 to 14, 12 to 16, 13 to 12, 14 to 9,
        15 to 9, 16 to 11, 17 to 16, 18 to 19, 19 to 15, 20 to 9, 21 to 4,
    )

    private fun seed(vararg parts: Any): Long = parts.joinToString("|").hashCode().toLong() * 2_654_435_761L

    private fun dayKey(date: LocalDate) = "${date.year}${date.monthNumber.toString().padStart(2, '0')}${date.dayOfMonth.toString().padStart(2, '0')}"

    private val dayCache = HashMap<String, List<GenSale>>()

    /** Todas as vendas do dia na loja (inclusive futuras, se o dia é hoje: quem chama filtra por [now]). */
    fun salesOfDay(shop: DemoShop, date: LocalDate): List<GenSale> = synchronized(dayCache) {
        dayCache.getOrPut(shop.id + dayKey(date)) { generateDay(shop, date) }
    }

    private fun generateDay(shop: DemoShop, date: LocalDate): List<GenSale> {
        val rnd = Random(seed(shop.id, date.toString()))
        val weekendBoost = when (date.dayOfWeek.ordinal) { 4 -> 1.15; 5 -> 1.3; 6 -> 0.8; else -> 1.0 }
        val count = ((shop.baseSales * weekendBoost) + rnd.nextInt(-6, 7)).toInt().coerceAtLeast(10)
        val totalWeight = hourWeights.values.sum()
        val minutes = (0 until count).map {
            var pick = rnd.nextInt(totalWeight)
            var hour = 7
            for ((h, w) in hourWeights) { if (pick < w) { hour = h; break }; pick -= w }
            hour * 60 + rnd.nextInt(60)
        }.sorted()
        return minutes.mapIndexed { index, minute ->
            val terminal = shop.terminals[rnd.nextInt(shop.terminals.size)]
            val instant = at(date, minute) + rnd.nextInt(0, 60).seconds
            buildSale("demo-sale-${shop.id.removePrefix("demo-")}-${dayKey(date)}-${index + 1}", shop, terminal, instant, rnd)
        }
    }

    private fun buildSale(id: String, shop: DemoShop, terminal: DemoTerminal, at: Instant, rnd: Random): GenSale {
        val lines = rnd.nextInt(1, 8)
        val chosen = products.shuffled(rnd).take(lines)
        val items = chosen.mapIndexed { i, p ->
            val qty = if (p.unit == "KG") rnd.nextInt(250, 1600).toLong() else (rnd.nextInt(1, 4) * 1000).toLong()
            val gross = p.priceCents * qty / 1000
            val promo = if (p.id == "p12" && qty >= 3000) 90L else 0L
            SaleItem(
                line = i + 1, name = p.name, unit = p.unit, quantityMilli = qty, unitPriceCents = p.priceCents,
                grossCents = gross, promoDiscountCents = promo, discountCents = 0, lineTotalCents = gross - promo,
            )
        }
        val subtotal = items.sumOf { it.lineTotalCents }
        val discount = if (subtotal > 15000 && rnd.nextInt(10) == 0) 500L else 0L
        val total = subtotal - discount
        val roll = rnd.nextInt(100)
        val method = when {
            roll < 40 -> "pix"
            roll < 60 -> "card_debit"
            roll < 77 -> "card_credit"
            roll < 95 -> "cash"
            roll < 99 -> "credit_account"
            else -> "store_credit"
        }
        val customer = if (method == "credit_account" || method == "store_credit") customers[rnd.nextInt(customers.size)] else ""
        var change = 0L
        val payments = if (method == "cash" && total > 2000 && rnd.nextBoolean()) {
            // Parte em dinheiro, parte no Pix.
            val cash = (total / 2 / 100) * 100
            listOf(SalePayment("cash", Labels.payment("cash"), cash), SalePayment("pix", Labels.payment("pix"), total - cash))
        } else {
            if (method == "cash") {
                val paid = ((total + 999) / 1000) * 1000
                change = paid - total
            }
            listOf(SalePayment(method, Labels.payment(method), total))
        }
        val statusRoll = rnd.nextInt(100)
        val status = when {
            statusRoll < 3 -> "cancelled"
            statusRoll < 4 -> "refunded"
            else -> "completed"
        }
        return GenSale(id, shop, terminal, at, items, payments, customer, status, discount, total, change)
    }

    /** Vendas do escopo no período, mais recentes primeiro, só até agora. */
    fun salesIn(tenantId: String, shopId: String?, period: PeriodKey): List<GenSale> {
        val limit = now()
        return dateRange(period).flatMap { date ->
            shopsIn(tenantId, shopId).flatMap { salesOfDay(it, date) }
        }.filter { it.at <= limit }.sortedByDescending { it.at }
    }

    fun findSale(id: String): GenSale? {
        // demo-sale-<loja>-<aaaammdd>-<n>
        val parts = id.removePrefix("demo-sale-").split("-")
        if (parts.size < 3) return null
        val day = parts[parts.size - 2]
        val shop = shops.firstOrNull { it.id == "demo-" + parts.dropLast(2).joinToString("-") } ?: return null
        val date = runCatching { LocalDate(day.substring(0, 4).toInt(), day.substring(4, 6).toInt(), day.substring(6, 8).toInt()) }.getOrNull()
            ?: return null
        return salesOfDay(shop, date).firstOrNull { it.id == id }
    }

    fun row(s: GenSale) = SaleRow(
        id = s.id, createdAt = iso(s.at), shop = ShopRef(s.shop.id, s.shop.name), terminalName = s.terminal.name,
        operatorName = s.terminal.operator, customerName = s.customer, totalCents = s.totalCents, status = s.status,
        itemsCount = s.items.size.toLong(), paymentMethods = s.payments.map { it.method }.distinct(),
    )

    fun detail(s: GenSale): SaleDetail {
        val returns = if (s.status == "refunded") listOf(
            SaleReturn(
                id = s.id + "-dev", createdAt = iso(s.at + 40.minutes), totalCents = s.totalCents,
                refundMethod = "cash", refundLabel = Labels.refund("cash"), reason = "Produto com defeito",
            ),
        ) else emptyList()
        return SaleDetail(
            id = s.id, createdAt = iso(s.at), receivedAt = iso(s.at + 2.seconds), status = s.status,
            shop = ShopRef(s.shop.id, s.shop.name), terminalName = s.terminal.name, operatorName = s.terminal.operator,
            sellerName = "", customerName = s.customer,
            grossCents = s.items.sumOf { it.grossCents }, promoDiscountCents = s.items.sumOf { it.promoDiscountCents },
            discountCents = s.discountCents, totalCents = s.totalCents, changeCents = s.changeCents,
            items = s.items, payments = s.payments, returns = returns,
            cancelledAt = if (s.status == "cancelled") iso(s.at + 3.minutes) else null,
            cancellationReason = if (s.status == "cancelled") "Cliente desistiu da compra" else "",
        )
    }

    // ---------- Resumo ----------

    fun dashboard(tenantId: String, shopId: String?, period: PeriodKey): Dashboard {
        val tenant = tenant(tenantId) ?: tenants.first()
        val sales = salesIn(tenant.id, shopId, period)
        val valid = sales.filter { it.status != "cancelled" }
        val cancelled = sales.filter { it.status == "cancelled" }
        val revenue = valid.sumOf { it.totalCents }
        val returns = valid.filter { it.status == "refunded" }.sumOf { it.totalCents }
        fun paid(method: String) = valid.sumOf { s -> s.payments.filter { it.method == method }.sumOf { it.amountCents } }
        val cash = paid("cash"); val pix = paid("pix"); val credit = paid("card_credit"); val debit = paid("card_debit")
        val days = dateRange(period)
        val creditReceived = days.sumOf { d -> shopsIn(tenant.id, shopId).sumOf { creditReceivedOn(it, d) } }

        val series = if (period == PeriodKey.TODAY || period == PeriodKey.YESTERDAY) {
            val byHour = valid.groupBy { it.at.toLocalDateTime(zone).hour }
            val lastHour = if (period == PeriodKey.TODAY) localNow().hour.coerceIn(7, 21) else 21
            Series("hour", (7..lastHour).map { h ->
                val list = byHour[h].orEmpty()
                val k = h.toString().padStart(2, '0')
                SeriesPoint(k, "${k}h", list.size.toLong(), list.sumOf { it.totalCents })
            })
        } else {
            val byDay = valid.groupBy { it.at.toLocalDateTime(zone).date }
            Series("day", days.map { d ->
                val list = byDay[d].orEmpty()
                SeriesPoint(d.toString(), "${d.dayOfMonth.toString().padStart(2, '0')}/${d.monthNumber.toString().padStart(2, '0')}",
                    list.size.toLong(), list.sumOf { it.totalCents })
            })
        }

        val sessions = cashSessionsIn(tenant.id, shopId, period)
        val open = openCashSessions(tenant.id, shopId)
        val shortages = sessions.filter { (it.differenceCents ?: 0) < 0 }
        val stock = stockAlerts(tenant.id, shopId)
        val creditCustomers = creditCustomers(tenant.id, shopId)
        val terminals = terminals(tenant.id, shopId)
        val conflictsOpen = conflicts(tenant.id, shopId).count { it.status == "open" }.toLong()

        return Dashboard(
            generatedAt = iso(now()),
            tenant = TenantRef(tenant.id, tenant.name),
            shop = shopId?.let { id -> shop(id)?.let { ShopRef(it.id, it.name) } },
            shops = tenant.shops,
            period = period(period),
            permissions = PERMISSIONS,
            sales = SalesSummary(
                count = valid.size.toLong(),
                revenueCents = revenue,
                averageTicketCents = if (valid.isEmpty()) 0 else revenue / valid.size,
                cancelledCount = cancelled.size.toLong(),
                cancelledCents = cancelled.sumOf { it.totalCents },
                returnsCents = returns,
                netRevenueCents = revenue - returns,
            ),
            payments = PaymentsSummary(
                cashCents = cash, pixCents = pix, cardCreditCents = credit, cardDebitCents = debit, otherCents = 0,
                receivedTotalCents = cash + pix + credit + debit,
                creditAccountCents = paid("credit_account"), storeCreditCents = paid("store_credit"),
                creditReceivedCents = creditReceived,
            ),
            series = series,
            cash = CashSummary(
                openCount = open.size.toLong(), closedCount = sessions.size.toLong(),
                shortageCount = shortages.size.toLong(), shortageCents = shortages.sumOf { it.differenceCents ?: 0 },
            ),
            stock = StockSummary(
                zeroedCount = stock.count { it.status == "zeroed" }.toLong(),
                belowMinimumCount = stock.count { it.status == "below_minimum" }.toLong(),
            ),
            credit = CreditSummary(
                openCents = creditCustomers.sumOf { it.openCents },
                overdueCents = creditCustomers.sumOf { it.overdueCents },
                overdueCustomers = creditCustomers.count { it.overdueCents > 0 }.toLong(),
            ),
            terminals = TerminalsSummary(
                total = terminals.size.toLong(),
                online = terminals.count { it.status == "online" }.toLong(),
                offline = terminals.count { it.status == "offline" }.toLong(),
                unpaired = 0,
                onlineWindowSeconds = 300,
                conflictsOpen = conflictsOpen,
            ),
        )
    }

    private fun creditReceivedOn(shop: DemoShop, date: LocalDate): Long {
        if (date == today() && localNow().hour < 10) return 0
        return Random(seed("fiado", shop.id, date.toString())).nextInt(0, 4) * 2500L
    }

    // ---------- Caixas ----------

    private val openMinute = OPEN_HOUR * 60
    private val closeMinute = 22 * 60 + 5

    /** Sessão de caixa de um terminal num dia (null se o terminal não abriu). */
    fun cashSession(shop: DemoShop, terminal: DemoTerminal, date: LocalDate, withDetail: Boolean): CashSession? {
        val today = today()
        val nowLocal = localNow()
        val isToday = date == today
        if (isToday && !terminal.online) return null // terminal sem contato: não abriu hoje
        val opened = at(date, openMinute + (terminal.name.last().digitToIntOrNull() ?: 1) * 3)
        if (opened > now()) return null
        val rnd = Random(seed("caixa", terminal.id, date.toString()))
        val sales = salesOfDay(shop, date).filter { it.terminal.id == terminal.id && it.at <= now() && it.status != "cancelled" }
        val opening = 20000L
        val cashNet = sales.sumOf { s -> s.payments.filter { it.method == "cash" }.sumOf { it.amountCents } } -
            sales.filter { it.status == "refunded" }.sumOf { it.totalCents }
        val creditReceived = creditReceivedOn(shop, date) / shop.terminals.size
        val movements = mutableListOf<CashMovement>()
        val midday = at(date, 13 * 60 + 10 + rnd.nextInt(40))
        if (!isToday || nowLocal.hour >= 14) {
            movements += CashMovement(
                id = "demo-mov-${terminal.id}-${dayKey(date)}-1", kind = "withdrawn", label = Labels.movement("withdrawn"),
                amountCents = 15000, reason = "Sangria para o cofre", operatorName = terminal.operator, createdAt = iso(midday),
            )
        }
        if (rnd.nextInt(5) == 0 && (!isToday || nowLocal.hour >= 10)) {
            movements += CashMovement(
                id = "demo-mov-${terminal.id}-${dayKey(date)}-2", kind = "supplied", label = Labels.movement("supplied"),
                amountCents = 5000, reason = "Troco", operatorName = "Gerente", createdAt = iso(at(date, 9 * 60 + 30)),
            )
        }
        val withdrawn = movements.filter { it.kind == "withdrawn" }.sumOf { it.amountCents }
        val supplied = movements.filter { it.kind == "supplied" }.sumOf { it.amountCents }
        val closed = !isToday
        val expected = opening + cashNet + creditReceived + supplied - withdrawn
        val diffRoll = rnd.nextInt(12)
        val difference = when (diffRoll) { 0 -> -1500L; 1 -> 200L; 2 -> -350L; else -> 0L }
        val byMethod = listOf("cash", "pix", "card_debit", "card_credit", "credit_account", "store_credit").map { m ->
            CashMethod(m, Labels.payment(m), sales.sumOf { s -> s.payments.filter { it.method == m }.sumOf { it.amountCents } })
        }.filter { it.amountCents > 0 }
        return CashSession(
            id = "demo-cash-${terminal.id.removePrefix("demo-term-")}-${dayKey(date)}",
            shop = ShopRef(shop.id, shop.name),
            terminalName = terminal.name,
            operatorName = terminal.operator,
            status = if (closed) "closed" else "open",
            openedAt = iso(opened),
            closedAt = if (closed) iso(at(date, closeMinute + rnd.nextInt(20))) else null,
            openingCents = opening,
            salesCount = sales.size.toLong(),
            salesTotalCents = sales.sumOf { it.totalCents },
            cashNetCents = cashNet,
            suppliedCents = supplied,
            withdrawnCents = withdrawn,
            expectedCents = expected,
            countedCents = if (closed) expected + difference else null,
            differenceCents = if (closed) difference else null,
            incomplete = false,
            closedByName = if (closed && withDetail) terminal.operator else "",
            supervisorName = if (closed && withDetail && difference != 0L) "Gerente" else "",
            creditReceivedCents = if (withDetail) creditReceived else 0,
            byMethod = if (withDetail) byMethod else emptyList(),
            movements = if (withDetail) movements else emptyList(),
        )
    }

    fun openCashSessions(tenantId: String, shopId: String?): List<CashSession> =
        shopsIn(tenantId, shopId).flatMap { shop -> shop.terminals.mapNotNull { cashSession(shop, it, today(), false) } }
            .filter { it.status == "open" }

    fun cashSessionsIn(tenantId: String, shopId: String?, period: PeriodKey): List<CashSession> =
        dateRange(period).filter { it != today() }.flatMap { date ->
            shopsIn(tenantId, shopId).flatMap { shop -> shop.terminals.mapNotNull { cashSession(shop, it, date, false) } }
        }.sortedByDescending { it.closedAt }

    fun findCashSession(id: String): CashSession? {
        // demo-cash-<loja>-<n>-<aaaammdd>
        val day = id.substringAfterLast("-")
        val termKey = id.removePrefix("demo-cash-").substringBeforeLast("-")
        val shop = shops.firstOrNull { s -> s.terminals.any { it.id == "demo-term-$termKey" } } ?: return null
        val terminal = shop.terminals.first { it.id == "demo-term-$termKey" }
        val date = runCatching { LocalDate(day.substring(0, 4).toInt(), day.substring(4, 6).toInt(), day.substring(6, 8).toInt()) }.getOrNull()
            ?: return null
        return cashSession(shop, terminal, date, withDetail = true)
    }

    // ---------- Estoque ----------

    fun stockAlerts(tenantId: String, shopId: String?): List<StockAlert> = shopsIn(tenantId, shopId).flatMap { shop ->
        val list = when (shop.id) {
            SHOP_CENTRO -> listOf(
                Triple("p03", 0L, 12_000L), Triple("p10", 0L, 3_000L), Triple("p06", 8_000L, 24_000L),
                Triple("p14", 3_000L, 10_000L), Triple("p16", 4_000L, 12_000L),
            )
            SHOP_JARDIM -> listOf(
                Triple("p12", 0L, 48_000L), Triple("p01", 5_000L, 15_000L), Triple("p08", 2_350L, 10_000L),
                Triple("p20", 6_000L, 24_000L),
            )
            else -> listOf(Triple("p19", 0L, 12_000L), Triple("p05", 4_000L, 12_000L))
        }
        list.map { (pid, qty, min) ->
            val p = products.first { it.id == pid }
            StockAlert(
                shop = ShopRef(shop.id, shop.name), productId = "${shop.id}-${p.id}", name = p.name, sku = p.sku, unit = p.unit,
                quantityMilli = qty, minimumMilli = min, status = if (qty <= 0) "zeroed" else "below_minimum",
            )
        }
    }

    // ---------- Fiado ----------

    fun creditCustomers(tenantId: String, shopId: String?): List<CreditCustomer> {
        val today = today()
        val base = when {
            tenantId == TENANT_EMPORIO -> listOf(
                CreditCustomer("demo-cli-9", "Restaurante Sabor Caseiro", "(11) 90000-0001", 64000, 0, today.plus(8, DateTimeUnit.DAY).toString(), 6),
            )
            else -> listOf(
                CreditCustomer("demo-cli-1", "Maria das Graças", "(11) 90000-0002", 38750, 12500, today.minus(12, DateTimeUnit.DAY).toString(), 7),
                CreditCustomer("demo-cli-2", "João Batista", "(11) 90000-0003", 21490, 21490, today.minus(25, DateTimeUnit.DAY).toString(), 4),
                CreditCustomer("demo-cli-3", "Dona Cida", "(11) 90000-0004", 15800, 0, today.plus(5, DateTimeUnit.DAY).toString(), 3),
                CreditCustomer("demo-cli-4", "Antônio Ferreira", "(11) 90000-0005", 9230, 4100, today.minus(3, DateTimeUnit.DAY).toString(), 2),
                CreditCustomer("demo-cli-5", "Luciana Martins", "(11) 90000-0006", 7650, 0, today.plus(12, DateTimeUnit.DAY).toString(), 2),
                CreditCustomer("demo-cli-6", "Seu Zé da Padaria", "(11) 90000-0007", 52300, 0, today.plus(20, DateTimeUnit.DAY).toString(), 9),
            )
        }
        // Uma loja específica vê só parte dos clientes (os de índice par ficam no Centro).
        return when (shopId) {
            null -> base
            SHOP_CENTRO -> base.filterIndexed { i, _ -> i % 2 == 0 }
            SHOP_JARDIM -> base.filterIndexed { i, _ -> i % 2 == 1 }
            else -> base
        }
    }

    // ---------- Terminais e conflitos ----------

    fun terminals(tenantId: String, shopId: String?): List<Terminal> = shopsIn(tenantId, shopId).flatMap { shop ->
        shop.terminals.map { t ->
            Terminal(
                id = t.id, name = t.name, shop = ShopRef(shop.id, shop.name),
                status = if (t.online) "online" else "offline", version = t.version,
                lastSeenAt = iso(if (t.online) now() - 40.seconds else now() - 2.hours - 15.minutes),
                pendingEvents = if (t.online) 0 else 3,
                error = if (t.online) "" else "Sem internet no caixa",
            )
        }
    }

    fun conflicts(tenantId: String, shopId: String?): List<Conflict> {
        val all = listOf(
            Conflict(
                id = "demo-conf-1", createdAt = iso(now() - 2.hours - 20.minutes), shop = ShopRef(SHOP_JARDIM, "Loja Jardim"),
                terminalName = "Caixa 2", reason = "negative_stock", reasonText = "Estoque negativo", status = "open", statusText = "Em aberto",
            ),
            Conflict(
                id = "demo-conf-2", createdAt = iso(now() - 26.hours), shop = ShopRef(SHOP_CENTRO, "Loja Centro"),
                terminalName = "Caixa 1", reason = "price_changed", reasonText = "Preço alterado durante a venda", status = "resolved",
                statusText = "Resolvido",
            ),
        )
        return all.filter { c -> shopsIn(tenantId, shopId).any { it.id == c.shop?.id } }
    }

    // ---------- Conta ----------

    fun me(): Me = Me(
        user = UserRef(id = 1, name = "Rita Exemplo", email = "rita@mercadinho.exemplo"),
        device = currentDevice(),
        platform = null,
        tenants = tenants,
        links = Links(),
        serverTime = iso(now()),
    )

    fun currentDevice() = Device(
        id = "demo-device-1", platform = "android", name = "Este celular", model = "Demonstração",
        osVersion = "Android", appVersion = "demonstração", createdAt = iso(now() - 30.minutes), lastSeenAt = iso(now()), current = true,
    )

    fun devices(): List<Device> = listOf(
        currentDevice(),
        Device(
            id = "demo-device-2", platform = "ios", name = "iPad do escritório", model = "iPad", osVersion = "iPadOS 26",
            appVersion = "1.0.0", createdAt = iso(now() - (24 * 20).hours), lastSeenAt = iso(now() - 26.hours), current = false,
        ),
    )

    companion object {
        /** Hora (Brasília) em que as lojas fictícias abrem e começam a vender. */
        const val OPEN_HOUR = 7
        const val TENANT_MERCADINHO = "demo-mercadinho"
        const val TENANT_EMPORIO = "demo-emporio"
        const val SHOP_CENTRO = "demo-loja-centro"
        const val SHOP_JARDIM = "demo-loja-jardim"
        const val SHOP_EMPORIO = "demo-loja-emporio"
        val PERMISSIONS = listOf("sales", "cash", "stock", "credit", "terminals")
    }
}
