package br.com.crowntech.pdv2gestao.core.api

import br.com.crowntech.pdv2gestao.core.model.AccessOptions
import br.com.crowntech.pdv2gestao.core.model.AppAccessObj
import br.com.crowntech.pdv2gestao.core.model.CashSession
import br.com.crowntech.pdv2gestao.core.model.Conflict
import br.com.crowntech.pdv2gestao.core.model.CreditResponse
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
import br.com.crowntech.pdv2gestao.core.model.TenantAccess
import br.com.crowntech.pdv2gestao.core.model.TerminalsResponse

/**
 * O que as telas pedem ao servidor. [AppApi] fala com a API real;
 * [br.com.crowntech.pdv2gestao.core.demo.DemoApi] responde com dados fictícios, sem rede.
 */
interface GestaoApi {
    suspend fun me(): Me
    suspend fun tenants(q: String? = null, cursor: String? = null): Page<TenantAccess>
    suspend fun dashboard(scope: DataScope): Dashboard
    suspend fun sales(scope: DataScope, status: String, cursor: String?): Page<SaleRow>
    suspend fun sale(tenantId: String, id: String): SaleDetail
    suspend fun cashSessions(scope: DataScope, status: String, cursor: String?): Page<CashSession>
    suspend fun cashSession(tenantId: String, id: String): CashSession
    suspend fun stockAlerts(scope: DataScope, status: String, cursor: String?): StockAlertsResponse
    suspend fun credit(scope: DataScope, status: String, cursor: String?): CreditResponse
    suspend fun terminals(scope: DataScope): TerminalsResponse
    suspend fun conflicts(scope: DataScope, status: String, cursor: String?): Page<Conflict>
    suspend fun devices(): Page<Device>
    suspend fun revokeDevice(id: String): OkResponse
    suspend fun requestDeletion(note: String?): DeletionResponse

    // ---------- Plataforma ----------
    suspend fun platformTenants(q: String?, attentionOnly: Boolean, cursor: String?): Page<PlatformTenant>
    suspend fun platformAccess(activeOnly: Boolean, q: String?, cursor: String?): Page<AppAccessObj>
    suspend fun grantAccess(request: GrantRequest): AppAccessObj
    suspend fun revokeAccess(id: String, reason: String): AppAccessObj
    suspend fun accessOptions(tenantId: String?): AccessOptions
    suspend fun platformDevices(activeOnly: Boolean, q: String?, cursor: String?): Page<PlatformDevice>
    suspend fun revokePlatformDevice(id: String, reason: String): PlatformDevice
}
