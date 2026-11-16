package br.com.crowntech.pdv2gestao.feature.summary

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.repeatOnLifecycle
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.core.model.Dashboard
import br.com.crowntech.pdv2gestao.core.model.Series
import br.com.crowntech.pdv2gestao.feature.home.ContextBar
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.feature.home.RequireTenant
import br.com.crowntech.pdv2gestao.feature.home.Routes
import br.com.crowntech.pdv2gestao.ui.common.BigValue
import br.com.crowntech.pdv2gestao.ui.common.RefreshableList
import br.com.crowntech.pdv2gestao.ui.common.SectionCard
import br.com.crowntech.pdv2gestao.ui.common.StatRow
import br.com.crowntech.pdv2gestao.ui.common.UpdatedFooter
import br.com.crowntech.pdv2gestao.ui.common.errorItem
import br.com.crowntech.pdv2gestao.ui.common.loadingItem
import br.com.crowntech.pdv2gestao.ui.common.rememberLoad
import br.com.crowntech.pdv2gestao.ui.common.staleItem
import br.com.crowntech.pdv2gestao.ui.theme.dangerColor
import br.com.crowntech.pdv2gestao.ui.theme.warningColor
import kotlinx.coroutines.delay

@Composable
fun SummaryScreen(vm: AppViewModel, onOpen: (String) -> Unit) {
    PageScaffold(title = "Resumo") { padding ->
        Column(Modifier.padding(padding)) {
            ContextBar(vm, showPeriod = true)
            RequireTenant(vm) { scope ->
                val state = rememberLoad(scope, onError = vm::handleDataError) {
                    vm.api.dashboard(scope).also { vm.updateShops(it.tenant.id, it.tenant.name, it.shops) }
                }
                // Atualiza sozinho a cada 60 s, só com período Hoje e app em primeiro plano.
                val lifecycle = LocalLifecycleOwner.current.lifecycle
                LaunchedEffect(state, scope.period) {
                    if (scope.period != PeriodKey.TODAY.key) return@LaunchedEffect
                    lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
                        while (true) {
                            delay(60_000)
                            state.reload()
                        }
                    }
                }
                // Faz o "Atualizado há X" andar sozinho.
                var tick by remember { mutableLongStateOf(0L) }
                LaunchedEffect(Unit) { while (true) { delay(30_000); tick++ } }

                RefreshableList(refreshing = state.refreshing, onRefresh = { state.reload(userInitiated = true) }) {
                    val data = state.data
                    val error = state.error
                    when {
                        data != null -> {
                            staleItem(state.staleError, state.updatedAt)
                            dashboardItems(data, onOpen)
                            item(key = "footer-$tick") { UpdatedFooter(state.updatedAt) }
                        }
                        error != null -> errorItem(error) { state.reload() }
                        else -> loadingItem()
                    }
                }
            }
        }
    }
}

private fun androidx.compose.foundation.lazy.LazyListScope.dashboardItems(d: Dashboard, onOpen: (String) -> Unit) {
    d.sales?.let { s ->
        item(key = "revenue") {
            SectionCard(title = "Faturamento") {
                BigValue(Format.money(s.revenueCents), "Líquido de devoluções: " + Format.money(s.netRevenueCents))
            }
        }
        item(key = "count-ticket") {
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                SectionCard(title = "Vendas", modifier = Modifier.weight(1f)) {
                    Text(s.count.toString(), style = MaterialTheme.typography.headlineSmall)
                }
                SectionCard(title = "Ticket médio", modifier = Modifier.weight(1f)) {
                    Text(Format.money(s.averageTicketCents), style = MaterialTheme.typography.headlineSmall)
                }
            }
        }
        item(key = "cancel") {
            SectionCard(title = "Cancelamentos") {
                StatRow("Quantidade", s.cancelledCount.toString())
                StatRow("Valor", Format.money(s.cancelledCents))
            }
        }
    }
    d.payments?.let { p ->
        item(key = "payments") {
            SectionCard(title = "Recebido nas vendas") {
                StatRow("Dinheiro", Format.money(p.cashCents))
                StatRow("Pix", Format.money(p.pixCents))
                StatRow("Cartão de crédito", Format.money(p.cardCreditCents))
                StatRow("Cartão de débito", Format.money(p.cardDebitCents))
                StatRow("Outras formas", Format.money(p.otherCents))
                StatRow("Total", Format.money(p.receivedTotalCents), emphasize = true)
                HorizontalDivider(Modifier.padding(vertical = 4.dp))
                StatRow("Vendido no fiado", Format.money(p.creditAccountCents))
                StatRow("Pago com crédito da loja", Format.money(p.storeCreditCents))
                StatRow("Fiado recebido", Format.money(p.creditReceivedCents))
            }
        }
    }
    d.series?.let { series ->
        item(key = "chart") {
            SectionCard(title = if (series.kind == "hour") "Faturamento por hora" else "Faturamento por dia") {
                SeriesChart(series)
            }
        }
    }
    d.cash?.let { c ->
        item(key = "cash") {
            SectionCard(title = "Caixas", onClick = { onOpen(Routes.CASH) }, onClickLabel = "Abrir Caixa") {
                StatRow("Caixas abertos", c.openCount.toString())
                StatRow(
                    "Quebras no período",
                    if (c.shortageCount == 0L) "Nenhuma" else "${c.shortageCount} · ${Format.money(c.shortageCents)}",
                    valueColor = if (c.shortageCount > 0) dangerColor() else androidx.compose.ui.graphics.Color.Unspecified,
                )
            }
        }
    }
    d.stock?.let { s ->
        item(key = "stock") {
            SectionCard(title = "Estoque", onClick = { onOpen(Routes.STOCK) }, onClickLabel = "Abrir Estoque") {
                StatRow("Produtos zerados", s.zeroedCount.toString(),
                    valueColor = if (s.zeroedCount > 0) dangerColor() else androidx.compose.ui.graphics.Color.Unspecified)
                StatRow("Abaixo do mínimo", s.belowMinimumCount.toString(),
                    valueColor = if (s.belowMinimumCount > 0) warningColor() else androidx.compose.ui.graphics.Color.Unspecified)
            }
        }
    }
    d.credit?.let { c ->
        item(key = "credit") {
            SectionCard(title = "Fiado", onClick = { onOpen(Routes.CREDIT) }, onClickLabel = "Abrir Fiado") {
                StatRow("Em aberto", Format.money(c.openCents))
                StatRow("Vencido", Format.money(c.overdueCents) + if (c.overdueCustomers > 0) " · ${c.overdueCustomers} clientes" else "",
                    valueColor = if (c.overdueCents > 0) dangerColor() else androidx.compose.ui.graphics.Color.Unspecified)
            }
        }
    }
    d.terminals?.let { t ->
        item(key = "terminals") {
            SectionCard(title = "Terminais", onClick = { onOpen(Routes.TERMINALS) }, onClickLabel = "Abrir Terminais") {
                StatRow("Online", "${t.online} de ${t.total}")
                if (t.unpaired > 0) StatRow("Não pareados", t.unpaired.toString())
                StatRow("Conflitos de sincronização", t.conflictsOpen.toString(),
                    valueColor = if (t.conflictsOpen > 0) warningColor() else androidx.compose.ui.graphics.Color.Unspecified)
            }
        }
    }
}

/** Barras desenhadas com Canvas do Compose (sem biblioteca extra). */
@Composable
fun SeriesChart(series: Series) {
    val points = series.points
    if (points.isEmpty()) {
        Text("Sem vendas no período.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        return
    }
    val max = points.maxOf { it.revenueCents }.coerceAtLeast(1)
    val best = points.maxByOrNull { it.revenueCents }
    val total = points.sumOf { it.revenueCents }
    val description = if (total == 0L) "Gráfico sem vendas no período." else
        "Gráfico de faturamento. Maior valor: ${Format.money(best?.revenueCents ?: 0)} em ${best?.label}."
    val barColor = MaterialTheme.colorScheme.primary
    val baseColor = MaterialTheme.colorScheme.outlineVariant
    Column(Modifier.semantics(mergeDescendants = true) { contentDescription = description }) {
        Canvas(Modifier.fillMaxWidth().height(140.dp)) {
            val n = points.size
            val slot = size.width / n
            val barWidth = (slot * 0.7f).coerceAtLeast(1f)
            points.forEachIndexed { i, p ->
                val h = (p.revenueCents.toFloat() / max) * (size.height - 4f)
                if (h > 0f) {
                    drawRoundRect(
                        color = barColor,
                        topLeft = Offset(i * slot + (slot - barWidth) / 2, size.height - h),
                        size = Size(barWidth, h),
                        cornerRadius = CornerRadius(barWidth / 4, barWidth / 4),
                    )
                }
            }
            drawLine(baseColor, Offset(0f, size.height), Offset(size.width, size.height), strokeWidth = 2f)
        }
        Row(Modifier.fillMaxWidth().padding(top = 4.dp)) {
            val labels = axisLabels(points.map { it.label })
            labels.forEach { label ->
                Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f), textAlign = TextAlign.Center)
            }
        }
    }
}

/** Até 4 rótulos espaçados no eixo. */
internal fun axisLabels(labels: List<String>): List<String> {
    if (labels.size <= 4) return labels
    val idx = listOf(0, labels.size / 3, (labels.size * 2) / 3, labels.size - 1)
    return idx.map { labels[it] }
}
