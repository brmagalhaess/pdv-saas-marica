package br.com.crowntech.pdv2gestao.feature.stock

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.model.StockAlert
import br.com.crowntech.pdv2gestao.core.model.StockCounts
import br.com.crowntech.pdv2gestao.feature.home.ContextBar
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.feature.home.RequireTenant
import br.com.crowntech.pdv2gestao.ui.common.InfiniteScroll
import br.com.crowntech.pdv2gestao.ui.common.ListRow
import br.com.crowntech.pdv2gestao.ui.common.PageResult
import br.com.crowntech.pdv2gestao.ui.common.RefreshableList
import br.com.crowntech.pdv2gestao.ui.common.Segmented
import br.com.crowntech.pdv2gestao.ui.common.StatusBadge
import br.com.crowntech.pdv2gestao.ui.common.emptyItem
import br.com.crowntech.pdv2gestao.ui.common.errorItem
import br.com.crowntech.pdv2gestao.ui.common.loadingItem
import br.com.crowntech.pdv2gestao.ui.common.moreFooter
import br.com.crowntech.pdv2gestao.ui.common.rememberPaged
import br.com.crowntech.pdv2gestao.ui.common.staleItem
import br.com.crowntech.pdv2gestao.ui.theme.dangerColor
import br.com.crowntech.pdv2gestao.ui.theme.warningColor

@Composable
fun StockScreen(vm: AppViewModel, onBack: (() -> Unit)?) {
    var status by rememberSaveable { mutableStateOf("zeroed") }
    PageScaffold(title = "Estoque", onBack = onBack) { padding ->
        Column(Modifier.padding(padding)) {
            ContextBar(vm, showPeriod = false)
            Segmented(
                options = listOf("zeroed" to "Zerados", "below_minimum" to "Abaixo do mínimo", "all" to "Todos"),
                selected = status,
                onSelect = { status = it },
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
            )
            RequireTenant(vm) { scope ->
                val paged = rememberPaged<StockCounts, StockAlert>(scope.tenantId, scope.shopId, status, onError = vm::handleDataError) { cursor ->
                    val r = vm.api.stockAlerts(scope, status, cursor)
                    PageResult(r.counts, r.results, r.nextCursor)
                }
                val listState = rememberLazyListState()
                InfiniteScroll(listState, paged.nextCursor != null) { paged.loadMore() }
                RefreshableList(paged.refreshing, { paged.reload(true) }, listState = listState) {
                    val error = paged.error
                    when {
                        paged.loaded -> {
                            staleItem(paged.staleError, paged.updatedAt)
                            paged.header?.let { c ->
                                item(key = "counts") {
                                    Text("${c.zeroed} zerados · ${c.belowMinimum} abaixo do mínimo", style = MaterialTheme.typography.bodyMedium,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                            }
                            if (paged.items.isEmpty()) emptyItem("Nenhum produto nesta situação.")
                            items(paged.items, key = { "${it.shop?.id}-${it.productId}" }) { StockRow(it) }
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
private fun StockRow(a: StockAlert) {
    ListRow {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(a.name, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            if (a.status == "zeroed") StatusBadge("Zerado", dangerColor()) else StatusBadge("Abaixo do mínimo", warningColor())
        }
        if (a.sku.isNotBlank()) Text("SKU " + a.sku, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(a.shop?.name ?: "—", style = MaterialTheme.typography.bodyMedium)
        Text(
            Format.quantity(a.quantityMilli, a.unit) + " / mínimo " + Format.quantity(a.minimumMilli, a.unit),
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.Medium,
        )
    }
}
