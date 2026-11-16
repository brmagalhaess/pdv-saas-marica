package br.com.crowntech.pdv2gestao.core.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * DTOs do contrato /api/v1/app/... (CONTRATO §6).
 * Dinheiro sempre em centavos (Long); quantidades em milésimos (Long).
 * Datas-hora chegam como texto ISO 8601 com fuso (-03:00) e são lidas em core/format.
 */
val AppJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    encodeDefaults = true
    coerceInputValues = true
}

// ---------- Objetos comuns ----------

@Serializable
data class UserRef(
    @SerialName("id") val id: Long = 0,
    @SerialName("name") val name: String = "",
    @SerialName("email") val email: String = "",
)

@Serializable
data class ShopRef(
    @SerialName("id") val id: String,
    @SerialName("name") val name: String = "",
)

@Serializable
data class TenantRef(
    @SerialName("id") val id: String,
    @SerialName("name") val name: String = "",
)

@Serializable
data class TokenPair(
    @SerialName("access_token") val accessToken: String,
    @SerialName("access_expires_at") val accessExpiresAt: String,
    @SerialName("access_expires_in") val accessExpiresIn: Long = 900,
    @SerialName("refresh_token") val refreshToken: String,
    @SerialName("refresh_expires_at") val refreshExpiresAt: String,
)

@Serializable
data class Device(
    @SerialName("id") val id: String,
    @SerialName("platform") val platform: String = "",
    @SerialName("name") val name: String = "",
    @SerialName("model") val model: String = "",
    @SerialName("os_version") val osVersion: String = "",
    @SerialName("app_version") val appVersion: String = "",
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("last_seen_at") val lastSeenAt: String? = null,
    @SerialName("current") val current: Boolean = false,
)

@Serializable
data class TenantAccess(
    @SerialName("id") val id: String,
    @SerialName("name") val name: String = "",
    @SerialName("all_shops") val allShops: Boolean = true,
    @SerialName("shops") val shops: List<ShopRef> = emptyList(),
    @SerialName("permissions") val permissions: List<String> = emptyList(),
    @SerialName("access_expires_on") val accessExpiresOn: String? = null,
)

@Serializable
data class Links(
    @SerialName("privacy_url") val privacyUrl: String = DEFAULT_PRIVACY_URL,
    @SerialName("support_url") val supportUrl: String = DEFAULT_SUPPORT_URL,
    @SerialName("account_deletion_url") val accountDeletionUrl: String = DEFAULT_DELETION_URL,
) {
    companion object {
        const val DEFAULT_PRIVACY_URL = "https://pdv2.crowntech.com.br/privacidade/"
        const val DEFAULT_SUPPORT_URL = "https://pdv2.crowntech.com.br/suporte/"
        const val DEFAULT_DELETION_URL = "https://pdv2.crowntech.com.br/excluir-conta/"
    }
}

@Serializable
data class PlatformInfo(
    @SerialName("enabled") val enabled: Boolean = false,
    @SerialName("superuser") val superuser: Boolean = false,
    @SerialName("can_manage_access") val canManageAccess: Boolean = false,
    @SerialName("roles") val roles: List<String> = emptyList(),
)

@Serializable
data class Me(
    @SerialName("user") val user: UserRef,
    @SerialName("device") val device: Device? = null,
    @SerialName("platform") val platform: PlatformInfo? = null,
    @SerialName("tenants") val tenants: List<TenantAccess> = emptyList(),
    @SerialName("links") val links: Links = Links(),
    @SerialName("server_time") val serverTime: String? = null,
) {
    val isPlatform: Boolean get() = platform?.enabled == true
}

@Serializable
data class Page<T>(
    @SerialName("results") val results: List<T> = emptyList(),
    @SerialName("next_cursor") val nextCursor: String? = null,
)

@Serializable
data class OkResponse(@SerialName("ok") val ok: Boolean = true)

@Serializable
data class ApiErrorBody(
    @SerialName("error") val error: String? = null,
    @SerialName("code") val code: String? = null,
    @SerialName("retry_after_seconds") val retryAfterSeconds: Long? = null,
    @SerialName("access_id") val accessId: String? = null,
)

// ---------- Autenticação ----------

@Serializable
data class DeviceInfo(
    @SerialName("platform") val platform: String = "android",
    @SerialName("name") val name: String,
    @SerialName("model") val model: String,
    @SerialName("os_version") val osVersion: String,
    @SerialName("app_version") val appVersion: String,
)

@Serializable
data class LoginRequest(
    @SerialName("email") val email: String,
    @SerialName("password") val password: String,
    @SerialName("device") val device: DeviceInfo,
)

@Serializable
data class MfaUser(
    @SerialName("name") val name: String = "",
    @SerialName("email") val email: String = "",
)

@Serializable
data class LoginResponse(
    @SerialName("status") val status: String,
    @SerialName("tokens") val tokens: TokenPair? = null,
    @SerialName("me") val me: Me? = null,
    @SerialName("mfa_token") val mfaToken: String? = null,
    @SerialName("mfa_expires_at") val mfaExpiresAt: String? = null,
    @SerialName("user") val user: MfaUser? = null,
)

@Serializable
data class MfaRequest(
    @SerialName("mfa_token") val mfaToken: String,
    @SerialName("code") val code: String? = null,
    @SerialName("recovery_code") val recoveryCode: String? = null,
)

@Serializable
data class RefreshRequest(@SerialName("refresh_token") val refreshToken: String)

@Serializable
data class LogoutRequest(@SerialName("refresh_token") val refreshToken: String? = null)

@Serializable
data class RefreshResponse(@SerialName("tokens") val tokens: TokenPair)

// ---------- Painel (dashboard) ----------

@Serializable
data class Period(
    @SerialName("key") val key: String,
    @SerialName("label") val label: String = "",
    @SerialName("date_from") val dateFrom: String = "",
    @SerialName("date_to") val dateTo: String = "",
    @SerialName("from") val from: String = "",
    @SerialName("to") val to: String = "",
)

@Serializable
data class SalesSummary(
    @SerialName("count") val count: Long = 0,
    @SerialName("revenue_cents") val revenueCents: Long = 0,
    @SerialName("average_ticket_cents") val averageTicketCents: Long = 0,
    @SerialName("cancelled_count") val cancelledCount: Long = 0,
    @SerialName("cancelled_cents") val cancelledCents: Long = 0,
    @SerialName("returns_cents") val returnsCents: Long = 0,
    @SerialName("net_revenue_cents") val netRevenueCents: Long = 0,
)

@Serializable
data class PaymentsSummary(
    @SerialName("cash_cents") val cashCents: Long = 0,
    @SerialName("pix_cents") val pixCents: Long = 0,
    @SerialName("card_credit_cents") val cardCreditCents: Long = 0,
    @SerialName("card_debit_cents") val cardDebitCents: Long = 0,
    @SerialName("other_cents") val otherCents: Long = 0,
    @SerialName("received_total_cents") val receivedTotalCents: Long = 0,
    @SerialName("credit_account_cents") val creditAccountCents: Long = 0,
    @SerialName("store_credit_cents") val storeCreditCents: Long = 0,
    @SerialName("credit_received_cents") val creditReceivedCents: Long = 0,
)

@Serializable
data class SeriesPoint(
    @SerialName("key") val key: String,
    @SerialName("label") val label: String = "",
    @SerialName("count") val count: Long = 0,
    @SerialName("revenue_cents") val revenueCents: Long = 0,
)

@Serializable
data class Series(
    @SerialName("kind") val kind: String = "hour",
    @SerialName("points") val points: List<SeriesPoint> = emptyList(),
)

@Serializable
data class CashSummary(
    @SerialName("open_count") val openCount: Long = 0,
    @SerialName("closed_count") val closedCount: Long = 0,
    @SerialName("shortage_count") val shortageCount: Long = 0,
    @SerialName("shortage_cents") val shortageCents: Long = 0,
)

@Serializable
data class StockSummary(
    @SerialName("zeroed_count") val zeroedCount: Long = 0,
    @SerialName("below_minimum_count") val belowMinimumCount: Long = 0,
)

@Serializable
data class CreditSummary(
    @SerialName("open_cents") val openCents: Long = 0,
    @SerialName("overdue_cents") val overdueCents: Long = 0,
    @SerialName("overdue_customers") val overdueCustomers: Long = 0,
)

@Serializable
data class TerminalsSummary(
    @SerialName("total") val total: Long = 0,
    @SerialName("online") val online: Long = 0,
    @SerialName("offline") val offline: Long = 0,
    @SerialName("unpaired") val unpaired: Long = 0,
    @SerialName("online_window_seconds") val onlineWindowSeconds: Long = 300,
    @SerialName("conflicts_open") val conflictsOpen: Long = 0,
)

@Serializable
data class Dashboard(
    @SerialName("generated_at") val generatedAt: String? = null,
    @SerialName("tenant") val tenant: TenantRef,
    @SerialName("shop") val shop: ShopRef? = null,
    @SerialName("shops") val shops: List<ShopRef> = emptyList(),
    @SerialName("period") val period: Period,
    @SerialName("permissions") val permissions: List<String> = emptyList(),
    @SerialName("sales") val sales: SalesSummary? = null,
    @SerialName("payments") val payments: PaymentsSummary? = null,
    @SerialName("series") val series: Series? = null,
    @SerialName("cash") val cash: CashSummary? = null,
    @SerialName("stock") val stock: StockSummary? = null,
    @SerialName("credit") val credit: CreditSummary? = null,
    @SerialName("terminals") val terminals: TerminalsSummary? = null,
)

// ---------- Vendas ----------

@Serializable
data class SaleRow(
    @SerialName("id") val id: String,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("shop") val shop: ShopRef? = null,
    @SerialName("terminal_name") val terminalName: String = "",
    @SerialName("station_name") val stationName: String = "",
    @SerialName("operator_name") val operatorName: String = "",
    @SerialName("customer_name") val customerName: String = "",
    @SerialName("total_cents") val totalCents: Long = 0,
    @SerialName("status") val status: String = "completed",
    @SerialName("items_count") val itemsCount: Long = 0,
    @SerialName("payment_methods") val paymentMethods: List<String> = emptyList(),
)

@Serializable
data class SaleItem(
    @SerialName("line") val line: Int = 0,
    @SerialName("name") val name: String = "",
    @SerialName("unit") val unit: String = "UN",
    @SerialName("quantity_milli") val quantityMilli: Long = 0,
    @SerialName("unit_price_cents") val unitPriceCents: Long = 0,
    @SerialName("gross_cents") val grossCents: Long = 0,
    @SerialName("promo_discount_cents") val promoDiscountCents: Long = 0,
    @SerialName("discount_cents") val discountCents: Long = 0,
    @SerialName("line_total_cents") val lineTotalCents: Long = 0,
)

@Serializable
data class SalePayment(
    @SerialName("method") val method: String,
    @SerialName("label") val label: String = "",
    @SerialName("amount_cents") val amountCents: Long = 0,
)

@Serializable
data class SaleReturn(
    @SerialName("id") val id: String,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("total_cents") val totalCents: Long = 0,
    @SerialName("refund_method") val refundMethod: String = "",
    @SerialName("refund_label") val refundLabel: String = "",
    @SerialName("reason") val reason: String = "",
)

@Serializable
data class SaleDetail(
    @SerialName("id") val id: String,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("received_at") val receivedAt: String? = null,
    @SerialName("status") val status: String = "completed",
    @SerialName("shop") val shop: ShopRef? = null,
    @SerialName("terminal_name") val terminalName: String = "",
    @SerialName("station_name") val stationName: String = "",
    @SerialName("operator_name") val operatorName: String = "",
    @SerialName("seller_name") val sellerName: String = "",
    @SerialName("customer_name") val customerName: String = "",
    @SerialName("gross_cents") val grossCents: Long = 0,
    @SerialName("promo_discount_cents") val promoDiscountCents: Long = 0,
    @SerialName("discount_cents") val discountCents: Long = 0,
    @SerialName("total_cents") val totalCents: Long = 0,
    @SerialName("change_cents") val changeCents: Long = 0,
    @SerialName("items") val items: List<SaleItem> = emptyList(),
    @SerialName("payments") val payments: List<SalePayment> = emptyList(),
    @SerialName("returns") val returns: List<SaleReturn> = emptyList(),
    @SerialName("cancelled_at") val cancelledAt: String? = null,
    @SerialName("cancellation_reason") val cancellationReason: String = "",
)

// ---------- Caixa ----------

@Serializable
data class CashMethod(
    @SerialName("method") val method: String,
    @SerialName("label") val label: String = "",
    @SerialName("amount_cents") val amountCents: Long = 0,
)

@Serializable
data class CashMovement(
    @SerialName("id") val id: String,
    @SerialName("kind") val kind: String = "",
    @SerialName("label") val label: String = "",
    @SerialName("amount_cents") val amountCents: Long = 0,
    @SerialName("reason") val reason: String = "",
    @SerialName("operator_name") val operatorName: String = "",
    @SerialName("created_at") val createdAt: String? = null,
)

@Serializable
data class CashSession(
    @SerialName("id") val id: String,
    @SerialName("shop") val shop: ShopRef? = null,
    @SerialName("terminal_name") val terminalName: String = "",
    @SerialName("station_name") val stationName: String = "",
    @SerialName("operator_name") val operatorName: String = "",
    @SerialName("status") val status: String = "open",
    @SerialName("opened_at") val openedAt: String? = null,
    @SerialName("closed_at") val closedAt: String? = null,
    @SerialName("opening_cents") val openingCents: Long = 0,
    @SerialName("sales_count") val salesCount: Long = 0,
    @SerialName("sales_total_cents") val salesTotalCents: Long = 0,
    @SerialName("cash_net_cents") val cashNetCents: Long = 0,
    @SerialName("supplied_cents") val suppliedCents: Long = 0,
    @SerialName("withdrawn_cents") val withdrawnCents: Long = 0,
    @SerialName("expected_cents") val expectedCents: Long? = null,
    @SerialName("counted_cents") val countedCents: Long? = null,
    @SerialName("difference_cents") val differenceCents: Long? = null,
    @SerialName("incomplete") val incomplete: Boolean = false,
    // Só no detalhe (§6.11)
    @SerialName("closed_by_name") val closedByName: String = "",
    @SerialName("supervisor_name") val supervisorName: String = "",
    @SerialName("credit_received_cents") val creditReceivedCents: Long = 0,
    @SerialName("by_method") val byMethod: List<CashMethod> = emptyList(),
    @SerialName("movements") val movements: List<CashMovement> = emptyList(),
)

// ---------- Estoque ----------

@Serializable
data class StockCounts(
    @SerialName("zeroed") val zeroed: Long = 0,
    @SerialName("below_minimum") val belowMinimum: Long = 0,
)

@Serializable
data class StockAlert(
    @SerialName("shop") val shop: ShopRef? = null,
    @SerialName("product_id") val productId: String,
    @SerialName("name") val name: String = "",
    @SerialName("sku") val sku: String = "",
    @SerialName("unit") val unit: String = "UN",
    @SerialName("quantity_milli") val quantityMilli: Long = 0,
    @SerialName("minimum_milli") val minimumMilli: Long = 0,
    @SerialName("status") val status: String = "zeroed",
)

@Serializable
data class StockAlertsResponse(
    @SerialName("counts") val counts: StockCounts = StockCounts(),
    @SerialName("results") val results: List<StockAlert> = emptyList(),
    @SerialName("next_cursor") val nextCursor: String? = null,
)

// ---------- Fiado ----------

@Serializable
data class CreditTotals(
    @SerialName("open_cents") val openCents: Long = 0,
    @SerialName("overdue_cents") val overdueCents: Long = 0,
    @SerialName("customers") val customers: Long = 0,
    @SerialName("overdue_customers") val overdueCustomers: Long = 0,
)

@Serializable
data class CreditCustomer(
    @SerialName("customer_id") val customerId: String,
    @SerialName("name") val name: String = "",
    @SerialName("phone") val phone: String = "",
    @SerialName("open_cents") val openCents: Long = 0,
    @SerialName("overdue_cents") val overdueCents: Long = 0,
    @SerialName("oldest_due_date") val oldestDueDate: String? = null,
    @SerialName("entries_count") val entriesCount: Long = 0,
)

@Serializable
data class CreditResponse(
    @SerialName("totals") val totals: CreditTotals = CreditTotals(),
    @SerialName("results") val results: List<CreditCustomer> = emptyList(),
    @SerialName("next_cursor") val nextCursor: String? = null,
)

// ---------- Terminais e conflitos ----------

@Serializable
data class Terminal(
    @SerialName("id") val id: String,
    @SerialName("name") val name: String = "",
    @SerialName("shop") val shop: ShopRef? = null,
    @SerialName("status") val status: String = "offline",
    @SerialName("version") val version: String = "",
    @SerialName("last_seen_at") val lastSeenAt: String? = null,
    @SerialName("pending_events") val pendingEvents: Long = 0,
    @SerialName("error") val error: String = "",
)

@Serializable
data class TerminalsResponse(
    @SerialName("online_window_seconds") val onlineWindowSeconds: Long = 300,
    @SerialName("results") val results: List<Terminal> = emptyList(),
    @SerialName("next_cursor") val nextCursor: String? = null,
)

@Serializable
data class Conflict(
    @SerialName("id") val id: String,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("shop") val shop: ShopRef? = null,
    @SerialName("terminal_name") val terminalName: String = "",
    @SerialName("reason") val reason: String = "",
    @SerialName("reason_text") val reasonText: String = "",
    @SerialName("status") val status: String = "open",
    @SerialName("status_text") val statusText: String = "",
)

// ---------- Conta ----------

@Serializable
data class DeletionRequestBody(@SerialName("note") val note: String? = null)

@Serializable
data class DeletionResponse(
    @SerialName("status") val status: String = "received",
    @SerialName("message") val message: String = "",
)

// ---------- Plataforma ----------

@Serializable
data class PlatformTenant(
    @SerialName("id") val id: String,
    @SerialName("name") val name: String = "",
    @SerialName("active") val active: Boolean = true,
    @SerialName("subscription_status") val subscriptionStatus: String = "",
    @SerialName("subscription_status_text") val subscriptionStatusText: String = "",
    @SerialName("shops_count") val shopsCount: Long = 0,
    @SerialName("terminals_total") val terminalsTotal: Long = 0,
    @SerialName("terminals_offline") val terminalsOffline: Long = 0,
    @SerialName("conflicts_open") val conflictsOpen: Long = 0,
    @SerialName("last_sale_at") val lastSaleAt: String? = null,
    @SerialName("last_seen_at") val lastSeenAt: String? = null,
    @SerialName("health") val health: String = "ok",
    @SerialName("health_text") val healthText: String = "",
)

@Serializable
data class AppAccessObj(
    @SerialName("id") val id: String,
    @SerialName("scope") val scope: String = "tenant",
    @SerialName("user") val user: UserRef,
    @SerialName("tenant") val tenant: TenantRef? = null,
    @SerialName("all_shops") val allShops: Boolean = true,
    @SerialName("shops") val shops: List<ShopRef> = emptyList(),
    @SerialName("permissions") val permissions: List<String> = emptyList(),
    @SerialName("reason") val reason: String = "",
    @SerialName("granted_by") val grantedBy: UserRef? = null,
    @SerialName("granted_at") val grantedAt: String? = null,
    @SerialName("expires_on") val expiresOn: String? = null,
    @SerialName("status") val status: String = "active",
    @SerialName("status_text") val statusText: String = "",
    @SerialName("revoked_at") val revokedAt: String? = null,
    @SerialName("revoked_by") val revokedBy: UserRef? = null,
    @SerialName("revoke_reason") val revokeReason: String = "",
    @SerialName("devices_active") val devicesActive: Long = 0,
)

@Serializable
data class AccessMember(
    @SerialName("user_id") val userId: Long,
    @SerialName("name") val name: String = "",
    @SerialName("email") val email: String = "",
    @SerialName("role") val role: String = "",
    @SerialName("role_text") val roleText: String = "",
    @SerialName("has_active_access") val hasActiveAccess: Boolean = false,
)

@Serializable
data class AccessOptions(
    @SerialName("tenant") val tenant: TenantRef? = null,
    @SerialName("shops") val shops: List<ShopRef> = emptyList(),
    @SerialName("members") val members: List<AccessMember> = emptyList(),
)

@Serializable
data class GrantRequest(
    @SerialName("scope") val scope: String,
    @SerialName("user_id") val userId: Long,
    @SerialName("tenant_id") val tenantId: String? = null,
    @SerialName("shop_ids") val shopIds: List<String>? = null,
    @SerialName("permissions") val permissions: List<String>? = null,
    @SerialName("expires_on") val expiresOn: String? = null,
    @SerialName("reason") val reason: String,
)

@Serializable
data class RevokeRequest(@SerialName("reason") val reason: String)

@Serializable
data class PlatformDevice(
    @SerialName("id") val id: String,
    @SerialName("platform") val platform: String = "",
    @SerialName("name") val name: String = "",
    @SerialName("model") val model: String = "",
    @SerialName("os_version") val osVersion: String = "",
    @SerialName("app_version") val appVersion: String = "",
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("last_seen_at") val lastSeenAt: String? = null,
    @SerialName("last_ip") val lastIp: String = "",
    @SerialName("user") val user: UserRef? = null,
    @SerialName("status") val status: String = "active",
    @SerialName("revoked_at") val revokedAt: String? = null,
    @SerialName("revoke_reason") val revokeReason: String = "",
)

/** Permissões possíveis de um acesso de empresa (ordem de exibição). */
val ALL_PERMISSIONS = listOf("sales", "cash", "stock", "credit", "terminals")
