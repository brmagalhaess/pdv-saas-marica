package br.com.crowntech.pdv2gestao.core.demo

import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.DataScope
import br.com.crowntech.pdv2gestao.core.api.GestaoApi
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.core.model.AccessOptions
import br.com.crowntech.pdv2gestao.core.model.AppAccessObj
import br.com.crowntech.pdv2gestao.core.model.CashSession
import br.com.crowntech.pdv2gestao.core.model.Conflict
import br.com.crowntech.pdv2gestao.core.model.CreditResponse
import br.com.crowntech.pdv2gestao.core.model.CreditTotals
import br.com.crowntech.pdv2gestao.core.model.Dashboard
import br.com.crowntech.pdv2gestao.core.model.DeletionResponse
import br.com.crowntech.pdv2gestao.core.model.Device
import br.com.crowntech.pdv2gestao.core.model.GrantRequest
import br.com.crowntech.pdv2gestao.core.model.Me
import br.com.crowntech.pdv2gestao.core.model.OkResponse
import br.com.crowntech.pdv2gestao.core.model.Page
import br.com.crowntech.pdv2gestao.core.model.PlatformDevice
import br.com.crowntech.pdv2gestao.core.model.PlatformTenant
import br.com.crowntech.pdv2gestao.core.model.SaleDetail
import br.com.crowntech.pdv2gestao.core.model.SaleRow
import br.com.crowntech.pdv2gestao.core.model.StockAlertsResponse
import br.com.crowntech.pdv2gestao.core.model.StockCounts
import br.com.crowntech.pdv2gestao.core.model.TenantAccess
import br.com.crowntech.pdv2gestao.core.model.TerminalsResponse
import kotlinx.coroutines.delay
import kotlinx.datetime.Clock
import kotlinx.datetime.Instant

/**
 * API do modo demonstração: responde com [DemoData], sem nenhuma chamada de rede
 * e sem gravar nada. Ações que mudariam algo no servidor não fazem nada e explicam o porquê.
 */
class DemoApi(
    now: () -> Instant = { Clock.System.now() },
    /** Pequena espera para os indicadores de carregamento aparecerem como no app real. */
    private val latencyMillis: Long = 250,
) : GestaoApi {
    val data = DemoData(now)

    private suspend fun pause() {
        if (latencyMillis > 0) delay(latencyMillis)
    }

    private fun <T> page(all: List<T>, cursor: String?, size: Int = PAGE_SIZE): Page<T> {
        val from = cursor?.toIntOrNull()?.coerceAtLeast(0) ?: 0
        val slice = all.drop(from).take(size)
        val next = (from + slice.size).takeIf { it < all.size }?.toString()
        return Page(slice, next)
    }

    private fun tenantOf(scope: DataScope): String =
        data.tenant(scope.tenantId)?.id ?: throw ApiException(403, "tenant_forbidden", "Você não tem acesso a esta empresa.")

    private fun shopOf(scope: DataScope): String? {
        val id = scope.shopId ?: return null
        val shop = data.shop(id)
        if (shop == null || shop.tenantId != scope.tenantId) throw ApiException(403, "shop_forbidden", "Você não tem acesso a esta loja.")
        return id
    }

    override suspend fun me(): Me = data.me()

    override suspend fun tenants(q: String?, cursor: String?): Page<TenantAccess> {
        val list = data.tenants.filter { q.isNullOrBlank() || it.name.contains(q, ignoreCase = true) }
        return page(list, cursor)
    }

    override suspend fun dashboard(scope: DataScope): Dashboard {
        pause()
        return data.dashboard(tenantOf(scope), shopOf(scope), PeriodKey.from(scope.period))
    }

    override suspend fun sales(scope: DataScope, status: String, cursor: String?): Page<SaleRow> {
        pause()
        val all = data.salesIn(tenantOf(scope), shopOf(scope), PeriodKey.from(scope.period)).filter {
            when (status) {
                "completed" -> it.status == "completed"
                "cancelled" -> it.status == "cancelled"
                else -> true
            }
        }
        return page(all, cursor).let { Page(it.results.map(data::row), it.nextCursor) }
    }

    override suspend fun sale(tenantId: String, id: String): SaleDetail {
        pause()
        val s = data.findSale(id)?.takeIf { it.shop.tenantId == tenantId } ?: throw notFound()
        return data.detail(s)
    }

    override suspend fun cashSessions(scope: DataScope, status: String, cursor: String?): Page<CashSession> {
        pause()
        val tenant = tenantOf(scope)
        val shop = shopOf(scope)
        val all = if (status == "closed") data.cashSessionsIn(tenant, shop, PeriodKey.from(scope.period))
        else data.openCashSessions(tenant, shop)
        return page(all, cursor)
    }

    override suspend fun cashSession(tenantId: String, id: String): CashSession {
        pause()
        val s = data.findCashSession(id) ?: throw notFound()
        if (data.shop(s.shop?.id)?.tenantId != tenantId) throw notFound()
        return s
    }

    override suspend fun stockAlerts(scope: DataScope, status: String, cursor: String?): StockAlertsResponse {
        pause()
        val all = data.stockAlerts(tenantOf(scope), shopOf(scope))
        val filtered = all.filter { status == "all" || it.status == status }
        val p = page(filtered, cursor)
        return StockAlertsResponse(
            counts = StockCounts(all.count { it.status == "zeroed" }.toLong(), all.count { it.status == "below_minimum" }.toLong()),
            results = p.results,
            nextCursor = p.nextCursor,
        )
    }

    override suspend fun credit(scope: DataScope, status: String, cursor: String?): CreditResponse {
        pause()
        val all = data.creditCustomers(tenantOf(scope), shopOf(scope))
        val filtered = if (status == "overdue") all.filter { it.overdueCents > 0 } else all
        val p = page(filtered.sortedByDescending { it.openCents }, cursor)
        return CreditResponse(
            totals = CreditTotals(
                openCents = all.sumOf { it.openCents },
                overdueCents = all.sumOf { it.overdueCents },
                customers = all.size.toLong(),
                overdueCustomers = all.count { it.overdueCents > 0 }.toLong(),
            ),
            results = p.results,
            nextCursor = p.nextCursor,
        )
    }

    override suspend fun terminals(scope: DataScope): TerminalsResponse {
        pause()
        return TerminalsResponse(300, data.terminals(tenantOf(scope), shopOf(scope)), null)
    }

    override suspend fun conflicts(scope: DataScope, status: String, cursor: String?): Page<Conflict> {
        pause()
        val all = data.conflicts(tenantOf(scope), shopOf(scope)).filter { status == "all" || it.status == status }
        return page(all, cursor)
    }

    override suspend fun devices(): Page<Device> = Page(data.devices(), null)

    override suspend fun revokeDevice(id: String): OkResponse {
        // Desconectar "este aparelho" sai da demonstração (a tela chama localLogout); os outros não mudam.
        if (id == data.currentDevice().id) return OkResponse(true)
        throw demoOnly()
    }

    override suspend fun requestDeletion(note: String?): DeletionResponse =
        DeletionResponse("received", "Na demonstração nenhum pedido é enviado. Toque em Ok para sair da demonstração.")

    // ---------- Plataforma (a conta de demonstração não é da plataforma) ----------

    override suspend fun platformTenants(q: String?, attentionOnly: Boolean, cursor: String?): Page<PlatformTenant> = throw platform()
    override suspend fun platformAccess(activeOnly: Boolean, q: String?, cursor: String?): Page<AppAccessObj> = throw platform()
    override suspend fun grantAccess(request: GrantRequest): AppAccessObj = throw platform()
    override suspend fun revokeAccess(id: String, reason: String): AppAccessObj = throw platform()
    override suspend fun accessOptions(tenantId: String?): AccessOptions = throw platform()
    override suspend fun platformDevices(activeOnly: Boolean, q: String?, cursor: String?): Page<PlatformDevice> = throw platform()
    override suspend fun revokePlatformDevice(id: String, reason: String): PlatformDevice = throw platform()

    private fun notFound() = ApiException(404, "not_found", "Registro não encontrado.")
    private fun platform() = ApiException(403, "platform_forbidden", "Somente a administração do PDV2 pode fazer isso.")
    private fun demoOnly() = ApiException(409, "demo_mode", DEMO_ACTION_MESSAGE)

    companion object {
        const val PAGE_SIZE = 50
        const val DEMO_ACTION_MESSAGE = "Na demonstração nada é alterado."
    }
}
