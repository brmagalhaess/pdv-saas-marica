package br.com.crowntech.pdv2gestao.feature.credit

import androidx.compose.foundation.layout.Column
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
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.model.CreditCustomer
import br.com.crowntech.pdv2gestao.core.model.CreditTotals
import br.com.crowntech.pdv2gestao.feature.home.ContextBar
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.feature.home.RequireTenant
import br.com.crowntech.pdv2gestao.ui.common.InfiniteScroll
import br.com.crowntech.pdv2gestao.ui.common.ListRow
import br.com.crowntech.pdv2gestao.ui.common.PageResult
import br.com.crowntech.pdv2gestao.ui.common.RefreshableList
import br.com.crowntech.pdv2gestao.ui.common.SectionCard
import br.com.crowntech.pdv2gestao.ui.common.Segmented
import br.com.crowntech.pdv2gestao.ui.common.StatRow
import br.com.crowntech.pdv2gestao.ui.common.emptyItem
import br.com.crowntech.pdv2gestao.ui.common.errorItem
import br.com.crowntech.pdv2gestao.ui.common.loadingItem
import br.com.crowntech.pdv2gestao.ui.common.moreFooter
import br.com.crowntech.pdv2gestao.ui.common.rememberPaged
import br.com.crowntech.pdv2gestao.ui.common.staleItem
import br.com.crowntech.pdv2gestao.ui.theme.dangerColor

@Composable
fun CreditScreen(vm: AppViewModel, onBack: () -> Unit) {
    var status by rememberSaveable { mutableStateOf("open") }
    PageScaffold(title = "Fiado", onBack = onBack) { padding ->
        Column(Modifier.padding(padding)) {
            ContextBar(vm, showPeriod = false)
            Segmented(
                options = listOf("open" to "Em aberto", "overdue" to "Vencido"),
                selected = status,
                onSelect = { status = it },
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
            )
            RequireTenant(vm) { scope ->
                val paged = rememberPaged<CreditTotals, CreditCustomer>(scope.tenantId, scope.shopId, status, onError = vm::handleDataError) { cursor ->
                    val r = vm.api.credit(scope, status, cursor)
                    PageResult(r.totals, r.results, r.nextCursor)
                }
                val listState = rememberLazyListState()
                InfiniteScroll(listState, paged.nextCursor != null) { paged.loadMore() }
                RefreshableList(paged.refreshing, { paged.reload(true) }, listState = listState) {
                    val error = paged.error
                    when {
                        paged.loaded -> {
                            staleItem(paged.staleError, paged.updatedAt)
                            paged.header?.let { t ->
                                item(key = "totals") {
                                    SectionCard(title = "Totais") {
                                        StatRow("Em aberto", Format.money(t.openCents), emphasize = true)
                                        StatRow("Vencido", Format.money(t.overdueCents), valueColor = if (t.overdueCents > 0) dangerColor() else Color.Unspecified)
                                        StatRow("Clientes com fiado", t.customers.toString())
                                        StatRow("Clientes com vencido", t.overdueCustomers.toString())
                                    }
                                }
                            }
                            if (paged.items.isEmpty()) emptyItem(if (status == "overdue") "Nenhum cliente com fiado vencido." else "Nenhum fiado em aberto.")
                            items(paged.items, key = { it.customerId }) { CreditRow(it) }
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
private fun CreditRow(c: CreditCustomer) {
    ListRow {
        Text(c.name, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
        if (c.phone.isNotBlank()) Text(c.phone, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        StatRow("Em aberto", Format.money(c.openCents))
        StatRow("Vencido", Format.money(c.overdueCents), valueColor = if (c.overdueCents > 0) dangerColor() else Color.Unspecified)
        StatRow("Vencimento mais antigo", Format.date(c.oldestDueDate))
    }
}
