package br.com.crowntech.pdv2gestao.core.api

import br.com.crowntech.pdv2gestao.core.model.AccessOptions
import br.com.crowntech.pdv2gestao.core.model.AppAccessObj
import br.com.crowntech.pdv2gestao.core.model.AppJson
import br.com.crowntech.pdv2gestao.core.model.CashSession
import br.com.crowntech.pdv2gestao.core.model.Conflict
import br.com.crowntech.pdv2gestao.core.model.CreditResponse
import br.com.crowntech.pdv2gestao.core.model.Dashboard
import br.com.crowntech.pdv2gestao.core.model.DeletionRequestBody
import br.com.crowntech.pdv2gestao.core.model.DeletionResponse
import br.com.crowntech.pdv2gestao.core.model.Device
import br.com.crowntech.pdv2gestao.core.model.GrantRequest
import br.com.crowntech.pdv2gestao.core.model.Me
import br.com.crowntech.pdv2gestao.core.model.OkResponse
import br.com.crowntech.pdv2gestao.core.model.Page
import br.com.crowntech.pdv2gestao.core.model.PlatformDevice
import br.com.crowntech.pdv2gestao.core.model.PlatformTenant
import br.com.crowntech.pdv2gestao.core.model.RevokeRequest
import br.com.crowntech.pdv2gestao.core.model.SaleDetail
import br.com.crowntech.pdv2gestao.core.model.SaleRow
import br.com.crowntech.pdv2gestao.core.model.StockAlertsResponse
import br.com.crowntech.pdv2gestao.core.model.TenantAccess
import br.com.crowntech.pdv2gestao.core.model.TerminalsResponse
import br.com.crowntech.pdv2gestao.core.session.SessionManager
import io.ktor.http.HttpMethod
import kotlinx.serialization.KSerializer

/** Contexto de dados: empresa (X-Tenant-ID), loja e período. */
data class DataScope(val tenantId: String, val shopId: String?, val period: String)

/** Rotas /api/v1/app/... (CONTRATO §6). */
class AppApi(private val client: ApiClient, private val session: SessionManager) {

    private suspend fun <T> call(
        method: HttpMethod,
        path: String,
        serializer: KSerializer<T>,
        query: Map<String, String?> = emptyMap(),
        body: String? = null,
        tenantId: String? = null,
    ): T = session.authorized { token ->
        client.send(method, session.baseUrl, path, serializer, query, body, bearer = token, tenantId = tenantId)
    }

    private fun scoped(scope: DataScope, vararg extra: Pair<String, String?>): Map<String, String?> =
        mapOf("shop_id" to scope.shopId) + extra

    suspend fun me(): Me = call(HttpMethod.Get, "/api/v1/app/me", Me.serializer())

    suspend fun tenants(q: String? = null, cursor: String? = null): Page<TenantAccess> =
        call(HttpMethod.Get, "/api/v1/app/tenants", Page.serializer(TenantAccess.serializer()),
            mapOf("q" to q?.take(120), "cursor" to cursor, "limit" to "50"))

    suspend fun dashboard(scope: DataScope): Dashboard =
        call(HttpMethod.Get, "/api/v1/app/dashboard", Dashboard.serializer(),
            scoped(scope, "period" to scope.period), tenantId = scope.tenantId)

    suspend fun sales(scope: DataScope, status: String, cursor: String?): Page<SaleRow> =
        call(HttpMethod.Get, "/api/v1/app/sales", Page.serializer(SaleRow.serializer()),
            scoped(scope, "period" to scope.period, "status" to status, "cursor" to cursor, "limit" to "50"),
            tenantId = scope.tenantId)

    suspend fun sale(tenantId: String, id: String): SaleDetail =
        call(HttpMethod.Get, "/api/v1/app/sales/$id", SaleDetail.serializer(), tenantId = tenantId)

    suspend fun cashSessions(scope: DataScope, status: String, cursor: String?): Page<CashSession> {
        val query = if (status == "closed") {
            scoped(scope, "status" to status, "period" to scope.period, "cursor" to cursor, "limit" to "50")
        } else {
            scoped(scope, "status" to status, "cursor" to cursor, "limit" to "50")
        }
        return call(HttpMethod.Get, "/api/v1/app/cash-sessions", Page.serializer(CashSession.serializer()), query, tenantId = scope.tenantId)
    }

    suspend fun cashSession(tenantId: String, id: String): CashSession =
        call(HttpMethod.Get, "/api/v1/app/cash-sessions/$id", CashSession.serializer(), tenantId = tenantId)

    suspend fun stockAlerts(scope: DataScope, status: String, cursor: String?): StockAlertsResponse =
        call(HttpMethod.Get, "/api/v1/app/stock/alerts", StockAlertsResponse.serializer(),
            scoped(scope, "status" to status, "cursor" to cursor, "limit" to "50"), tenantId = scope.tenantId)

    suspend fun credit(scope: DataScope, status: String, cursor: String?): CreditResponse =
        call(HttpMethod.Get, "/api/v1/app/credit", CreditResponse.serializer(),
            scoped(scope, "status" to status, "cursor" to cursor, "limit" to "50"), tenantId = scope.tenantId)

    suspend fun terminals(scope: DataScope): TerminalsResponse =
        call(HttpMethod.Get, "/api/v1/app/terminals", TerminalsResponse.serializer(), scoped(scope), tenantId = scope.tenantId)

    suspend fun conflicts(scope: DataScope, status: String, cursor: String?): Page<Conflict> =
        call(HttpMethod.Get, "/api/v1/app/conflicts", Page.serializer(Conflict.serializer()),
            scoped(scope, "status" to status, "cursor" to cursor, "limit" to "50"), tenantId = scope.tenantId)

    suspend fun devices(): Page<Device> =
        call(HttpMethod.Get, "/api/v1/app/devices", Page.serializer(Device.serializer()))

    suspend fun revokeDevice(id: String): OkResponse =
        call(HttpMethod.Delete, "/api/v1/app/devices/$id", OkResponse.serializer())

    suspend fun requestDeletion(note: String?): DeletionResponse =
        call(HttpMethod.Post, "/api/v1/app/account/deletion-request", DeletionResponse.serializer(),
            body = AppJson.encodeToString(DeletionRequestBody.serializer(), DeletionRequestBody(note?.trim()?.take(500)?.ifBlank { null })))

    // ---------- Plataforma ----------

    suspend fun platformTenants(q: String?, attentionOnly: Boolean, cursor: String?): Page<PlatformTenant> =
        call(HttpMethod.Get, "/api/v1/app/platform/tenants", Page.serializer(PlatformTenant.serializer()),
            mapOf("q" to q?.take(120), "health" to if (attentionOnly) "attention" else "all", "cursor" to cursor, "limit" to "50"))

    suspend fun platformAccess(activeOnly: Boolean, q: String?, cursor: String?): Page<AppAccessObj> =
        call(HttpMethod.Get, "/api/v1/app/platform/access", Page.serializer(AppAccessObj.serializer()),
            mapOf("status" to if (activeOnly) "active" else "all", "q" to q, "cursor" to cursor, "limit" to "50"))

    suspend fun grantAccess(request: GrantRequest): AppAccessObj =
        call(HttpMethod.Post, "/api/v1/app/platform/access", AppAccessObj.serializer(),
            body = AppJson.encodeToString(GrantRequest.serializer(), request))

    suspend fun revokeAccess(id: String, reason: String): AppAccessObj =
        call(HttpMethod.Post, "/api/v1/app/platform/access/$id/revoke", AppAccessObj.serializer(),
            body = AppJson.encodeToString(RevokeRequest.serializer(), RevokeRequest(reason.trim())))

    suspend fun accessOptions(tenantId: String?): AccessOptions =
        call(HttpMethod.Get, "/api/v1/app/platform/access/options", AccessOptions.serializer(), mapOf("tenant_id" to tenantId))

    suspend fun platformDevices(activeOnly: Boolean, q: String?, cursor: String?): Page<PlatformDevice> =
        call(HttpMethod.Get, "/api/v1/app/platform/devices", Page.serializer(PlatformDevice.serializer()),
            mapOf("status" to if (activeOnly) "active" else "all", "q" to q, "cursor" to cursor, "limit" to "50"))

    suspend fun revokePlatformDevice(id: String, reason: String): PlatformDevice =
        call(HttpMethod.Post, "/api/v1/app/platform/devices/$id/revoke", PlatformDevice.serializer(),
            body = AppJson.encodeToString(RevokeRequest.serializer(), RevokeRequest(reason.trim())))
}
