package br.com.crowntech.pdv2gestao.feature.cash

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
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
import br.com.crowntech.pdv2gestao.core.model.CashSession
import br.com.crowntech.pdv2gestao.feature.home.ContextBar
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.feature.home.RequireTenant
import br.com.crowntech.pdv2gestao.feature.sales.placeText
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
fun differenceColor(cents: Long?): Color = when {
    cents == null -> MaterialTheme.colorScheme.onSurfaceVariant
    cents < 0 -> dangerColor()
    cents > 0 -> warningColor()
    else -> successColor()
}

@Composable
fun CashScreen(vm: AppViewModel, onOpen: (String) -> Unit) {
    var status by rememberSaveable { mutableStateOf("open") }
    PageScaffold(title = "Caixa") { padding ->
        Column(Modifier.padding(padding)) {
            ContextBar(vm, showPeriod = status == "closed")
            Segmented(
                options = listOf("open" to "Abertos", "closed" to "Fechados"),
                selected = status,
                onSelect = { status = it },
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
            )
            RequireTenant(vm) { scope ->
                // Abertos ignoram o período (a chave muda só quando importa).
                val periodKey = if (status == "closed") scope.period else "-"
                val paged = rememberPaged<Unit, CashSession>(scope.tenantId, scope.shopId, periodKey, status, onError = vm::handleDataError) { cursor ->
                    val page = vm.api.cashSessions(scope, status, cursor)
                    PageResult(null, page.results, page.nextCursor)
                }
                val listState = rememberLazyListState()
                InfiniteScroll(listState, paged.nextCursor != null) { paged.loadMore() }
                RefreshableList(paged.refreshing, { paged.reload(true) }, listState = listState) {
                    val error = paged.error
                    when {
                        paged.loaded -> {
                            staleItem(paged.staleError, paged.updatedAt)
                            if (paged.items.isEmpty()) emptyItem(if (status == "open") "Nenhum caixa aberto agora." else "Nenhum caixa fechado neste período.")
                            items(paged.items, key = { it.id }) { s -> CashRow(s) { onOpen(s.id) } }
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
private fun CashRow(s: CashSession, onClick: () -> Unit) {
    ListRow(onClick = onClick) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(placeText(s.shop?.name, s.terminalName, s.stationName), style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            if (s.status == "closed") {
                StatusBadge(if (s.incomplete) "Abertura não recebida" else Labels.cashDifference(s.differenceCents), differenceColor(s.differenceCents))
            } else {
                StatusBadge(Labels.cashStatus(s.status), successColor())
            }
        }
        Text("Operador: " + s.operatorName.ifBlank { "—" }, style = MaterialTheme.typography.bodyMedium)
        Text(
            if (s.status == "closed") "Fechado em " + Format.dateTime(s.closedAt) else "Aberto em " + Format.dateTime(s.openedAt),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            Text("${s.salesCount} vendas · ${Format.money(s.salesTotalCents)}", style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
            Text("Dinheiro " + Format.money(s.cashNetCents), style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
fun CashDetailScreen(vm: AppViewModel, id: String, onBack: () -> Unit) {
    PageScaffold(title = "Caixa", onBack = onBack) { padding ->
        Column(Modifier.padding(padding)) {
            RequireTenant(vm) { scope ->
                val state = rememberLoad(scope.tenantId, id, onError = vm::handleDataError) { vm.api.cashSession(scope.tenantId, id) }
                RefreshableList(state.refreshing, { state.reload(true) }) {
                    val d = state.data
                    val error = state.error
                    when {
                        d != null -> {
                            staleItem(state.staleError, state.updatedAt)
                            cashDetailItems(d)
                        }
                        error != null -> errorItem(error) { state.reload() }
                        else -> loadingItem()
                    }
                }
            }
        }
    }
}

private fun androidx.compose.foundation.lazy.LazyListScope.cashDetailItems(s: CashSession) {
    item(key = "summary") {
        SectionCard(title = placeText(s.shop?.name, s.terminalName, s.stationName)) {
            StatRow("Situação", if (s.incomplete) "Abertura não recebida" else Labels.cashStatus(s.status))
            StatRow("Operador", s.operatorName.ifBlank { "—" })
            StatRow("Aberto em", Format.dateTime(s.openedAt))
            if (s.closedAt != null) StatRow("Fechado em", Format.dateTime(s.closedAt))
            if (s.closedByName.isNotBlank()) StatRow("Fechado por", s.closedByName)
            if (s.supervisorName.isNotBlank()) StatRow("Supervisor", s.supervisorName)
            HorizontalDivider(Modifier.padding(vertical = 4.dp))
            StatRow("Abertura", Format.money(s.openingCents))
            StatRow("Vendas", "${s.salesCount} · ${Format.money(s.salesTotalCents)}")
            StatRow("Dinheiro (sem troco e devoluções)", Format.money(s.cashNetCents))
            StatRow("Suprimentos", Format.money(s.suppliedCents))
            StatRow("Sangrias", Format.money(s.withdrawnCents))
            StatRow("Fiado recebido", Format.money(s.creditReceivedCents))
            HorizontalDivider(Modifier.padding(vertical = 4.dp))
            StatRow("Esperado na gaveta", Format.money(s.expectedCents))
            StatRow("Contado", Format.money(s.countedCents))
            StatRow("Diferença", Labels.cashDifference(s.differenceCents), emphasize = true, valueColor = differenceColor(s.differenceCents))
        }
    }
    item(key = "methods") {
        SectionCard(title = "Formas de pagamento") {
            if (s.byMethod.isEmpty()) Text("Sem vendas neste caixa.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            s.byMethod.forEach { StatRow(it.label.ifBlank { Labels.payment(it.method) }, Format.money(it.amountCents)) }
        }
    }
    item(key = "movements") {
        SectionCard(title = "Sangrias e suprimentos") {
            if (s.movements.isEmpty()) Text("Nenhuma sangria ou suprimento.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            s.movements.forEachIndexed { index, m ->
                if (index > 0) HorizontalDivider(Modifier.padding(vertical = 4.dp))
                StatRow(m.label.ifBlank { Labels.movement(m.kind) } + " · " + Format.time(m.createdAt), Format.money(m.amountCents))
                val who = listOf(m.reason, m.operatorName).filter { it.isNotBlank() }.joinToString(" · ")
                if (who.isNotBlank()) Text(who, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}
