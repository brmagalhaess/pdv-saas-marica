package br.com.crowntech.pdv2gestao.feature.sales

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.format.Labels
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.core.model.SaleDetail
import br.com.crowntech.pdv2gestao.core.model.SaleRow
import br.com.crowntech.pdv2gestao.feature.home.ContextBar
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.feature.home.RequireTenant
import br.com.crowntech.pdv2gestao.ui.common.BigValue
import br.com.crowntech.pdv2gestao.ui.common.InfiniteScroll
import br.com.crowntech.pdv2gestao.ui.common.ListRow
import br.com.crowntech.pdv2gestao.ui.common.PageResult
import br.com.crowntech.pdv2gestao.ui.common.RefreshableList
import br.com.crowntech.pdv2gestao.ui.common.SectionCard
import br.com.crowntech.pdv2gestao.ui.common.Segmented
import br.com.crowntech.pdv2gestao.ui.common.StatRow
import br.com.crowntech.pdv2gestao.ui.common.StatusBadge
import br.com.crowntech.pdv2gestao.ui.common.emptyItem
import br.com.crowntech.pdv2gestao.ui.common.errorItem
import br.com.crowntech.pdv2gestao.ui.common.loadingItem
import br.com.crowntech.pdv2gestao.ui.common.moreFooter
import br.com.crowntech.pdv2gestao.ui.common.rememberLoad
import br.com.crowntech.pdv2gestao.ui.common.rememberPaged
import br.com.crowntech.pdv2gestao.ui.common.staleItem
import br.com.crowntech.pdv2gestao.ui.theme.dangerColor
import br.com.crowntech.pdv2gestao.ui.theme.successColor
import br.com.crowntech.pdv2gestao.ui.theme.warningColor

@Composable
fun saleStatusColor(status: String): Color = when (status) {
    "completed" -> successColor()
    "cancelled" -> dangerColor()
    "refunded" -> warningColor()
    else -> MaterialTheme.colorScheme.onSurfaceVariant
}

/** "Loja · Caixa" (com estação quando houver). */
fun placeText(shop: String?, terminal: String, station: String = ""): String =
    listOfNotNull(shop?.ifBlank { null }, terminal.ifBlank { null }, station.ifBlank { null }).joinToString(" · ").ifBlank { "—" }

@Composable
fun SalesScreen(vm: AppViewModel, onOpen: (String) -> Unit) {
    var filter by rememberSaveable { mutableStateOf("all") }
    PageScaffold(title = "Vendas") { padding ->
        Column(Modifier.padding(padding)) {
            ContextBar(vm, showPeriod = true)
            Segmented(
                options = listOf("all" to "Todas", "completed" to "Concluídas", "cancelled" to "Canceladas"),
                selected = filter,
                onSelect = { filter = it },
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
            )
            RequireTenant(vm) { scope ->
                val paged = rememberPaged<Unit, SaleRow>(scope, filter, onError = vm::handleDataError) { cursor ->
                    val page = vm.api.sales(scope, filter, cursor)
                    PageResult(null, page.results, page.nextCursor)
                }
                val listState = rememberLazyListState()
                InfiniteScroll(listState, paged.nextCursor != null) { paged.loadMore() }
                val onlyTime = scope.period == PeriodKey.TODAY.key
                RefreshableList(paged.refreshing, { paged.reload(true) }, listState = listState) {
                    val error = paged.error
                    when {
                        paged.loaded -> {
                            staleItem(paged.staleError, paged.updatedAt)
                            if (paged.items.isEmpty()) emptyItem("Nenhuma venda neste período.")
                            items(paged.items, key = { it.id }) { sale -> SaleRowItem(sale, onlyTime) { onOpen(sale.id) } }
                            moreFooter(paged.loadingMore, paged.moreError, paged.nextCursor != null) { paged.loadMore() }
                        }
                        error != null -> errorItem(error) { paged.reload() }
                        else -> loadingItem()
                    }
                }
            }
        }
    }
}

@Composable
private fun SaleRowItem(sale: SaleRow, onlyTime: Boolean, onClick: () -> Unit) {
    ListRow(onClick = onClick) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(Format.saleMoment(sale.createdAt, onlyTime), style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f))
            Text(Format.money(sale.totalCents), style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        }
        Text(placeText(sale.shop?.name, sale.terminalName, sale.stationName), style = MaterialTheme.typography.bodyMedium)
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(Labels.saleStatus(sale.status), saleStatusColor(sale.status))
            Text(sale.paymentMethods.joinToString { Labels.payment(it) }, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
fun SaleDetailScreen(vm: AppViewModel, id: String, onBack: () -> Unit) {
    PageScaffold(title = "Venda", onBack = onBack) { padding ->
        Column(Modifier.padding(padding)) {
            RequireTenant(vm) { scope ->
                val state = rememberLoad(scope.tenantId, id, onError = vm::handleDataError) { vm.api.sale(scope.tenantId, id) }
                RefreshableList(state.refreshing, { state.reload(true) }) {
                    val d = state.data
                    val error = state.error
                    when {
                        d != null -> {
                            staleItem(state.staleError, state.updatedAt)
                            saleDetailItems(d)
                        }
                        error != null -> errorItem(error) { state.reload() }
                        else -> loadingItem()
                    }
                }
            }
        }
    }
}

private fun androidx.compose.foundation.lazy.LazyListScope.saleDetailItems(d: SaleDetail) {
    item(key = "head") {
        SectionCard {
            BigValue(Format.money(d.totalCents))
            StatusBadge(Labels.saleStatus(d.status), saleStatusColor(d.status))
            StatRow("Data", Format.dateTime(d.createdAt))
            StatRow("Loja", d.shop?.name ?: "—")
            StatRow("Caixa", placeText(null, d.terminalName, d.stationName))
            StatRow("Operador", d.operatorName.ifBlank { "—" })
            StatRow("Vendedor", d.sellerName.ifBlank { "—" })
            StatRow("Cliente", d.customerName.ifBlank { "—" })
        }
    }
    if (d.status != "completed" && (d.cancellationReason.isNotBlank() || d.cancelledAt != null)) {
        item(key = "cancel") {
            SectionCard(title = "Cancelamento") {
                d.cancelledAt?.let { StatRow("Cancelada em", Format.dateTime(it)) }
                Text(d.cancellationReason.ifBlank { "Motivo não informado." }, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
    item(key = "items") {
        SectionCard(title = "Itens") {
            d.items.forEachIndexed { index, it ->
                if (index > 0) HorizontalDivider(Modifier.padding(vertical = 4.dp))
                Row {
                    Text(it.name, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
                    Text(Format.money(it.lineTotalCents), style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium)
                }
                Text("${Format.quantity(it.quantityMilli, it.unit)} × ${Format.money(it.unitPriceCents)}",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (it.promoDiscountCents > 0) Text("Promoção: -" + Format.money(it.promoDiscountCents), style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (it.discountCents > 0) Text("Desconto: -" + Format.money(it.discountCents), style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (d.items.isEmpty()) Text("Sem itens.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
    item(key = "payments") {
        SectionCard(title = "Pagamentos") {
            d.payments.forEach { StatRow(it.label.ifBlank { Labels.payment(it.method) }, Format.money(it.amountCents)) }
            if (d.changeCents > 0) StatRow("Troco", Format.money(d.changeCents))
            if (d.payments.isEmpty()) Text("Sem pagamentos.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
    item(key = "discounts") {
        SectionCard(title = "Descontos") {
            StatRow("Valor bruto", Format.money(d.grossCents))
            StatRow("Promoção", Format.money(d.promoDiscountCents))
            StatRow("Desconto manual", Format.money(d.discountCents))
            StatRow("Total", Format.money(d.totalCents), emphasize = true)
        }
    }
    if (d.returns.isNotEmpty()) {
        item(key = "returns") {
            SectionCard(title = "Devoluções") {
                d.returns.forEachIndexed { index, r ->
                    if (index > 0) HorizontalDivider(Modifier.padding(vertical = 4.dp))
                    StatRow(Format.dateTime(r.createdAt), Format.money(r.totalCents))
                    Text(r.refundLabel.ifBlank { Labels.refund(r.refundMethod) }, style = MaterialTheme.typography.bodySmall)
                    if (r.reason.isNotBlank()) Text(r.reason, style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.fillMaxWidth())
                }
            }
        }
    }
}
