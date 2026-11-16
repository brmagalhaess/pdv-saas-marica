package br.com.crowntech.pdv2gestao.feature.conflicts

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
import br.com.crowntech.pdv2gestao.core.format.Labels
import br.com.crowntech.pdv2gestao.core.model.Conflict
import br.com.crowntech.pdv2gestao.feature.home.ContextBar
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.feature.home.RequireTenant
import br.com.crowntech.pdv2gestao.feature.sales.placeText
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
import br.com.crowntech.pdv2gestao.ui.theme.successColor
import br.com.crowntech.pdv2gestao.ui.theme.warningColor

@Composable
fun ConflictsScreen(vm: AppViewModel, onBack: () -> Unit) {
    var status by rememberSaveable { mutableStateOf("open") }
    PageScaffold(title = "Conflitos de sincronização", onBack = onBack) { padding ->
        Column(Modifier.padding(padding)) {
            ContextBar(vm, showPeriod = false)
            Segmented(
                options = listOf("open" to "Em aberto", "all" to "Todos"),
                selected = status,
                onSelect = { status = it },
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
            )
            RequireTenant(vm) { scope ->
                val paged = rememberPaged<Unit, Conflict>(scope.tenantId, scope.shopId, status, onError = vm::handleDataError) { cursor ->
                    val page = vm.api.conflicts(scope, status, cursor)
                    PageResult(null, page.results, page.nextCursor)
                }
                val listState = rememberLazyListState()
                InfiniteScroll(listState, paged.nextCursor != null) { paged.loadMore() }
                RefreshableList(paged.refreshing, { paged.reload(true) }, listState = listState) {
                    val error = paged.error
                    when {
                        paged.loaded -> {
                            staleItem(paged.staleError, paged.updatedAt)
                            item(key = "hint") {
                                Text("Abra o painel web para resolver.", style = MaterialTheme.typography.bodyMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                            if (paged.items.isEmpty()) emptyItem("Nenhum conflito.")
                            items(paged.items, key = { it.id }) { ConflictRow(it) }
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
private fun ConflictRow(c: Conflict) {
    ListRow {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(c.reasonText.ifBlank { c.reason }, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold,
                modifier = Modifier.weight(1f))
            StatusBadge(c.statusText.ifBlank { Labels.conflictStatus(c.status) }, if (c.status == "open") warningColor() else successColor())
        }
        Text(placeText(c.shop?.name, c.terminalName), style = MaterialTheme.typography.bodyMedium)
        Text(Format.dateTime(c.createdAt), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
