package br.com.crowntech.pdv2gestao.feature.terminals

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.format.Labels
import br.com.crowntech.pdv2gestao.core.model.Terminal
import br.com.crowntech.pdv2gestao.feature.home.ContextBar
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.feature.home.RequireTenant
import br.com.crowntech.pdv2gestao.ui.common.ListRow
import br.com.crowntech.pdv2gestao.ui.common.RefreshableList
import br.com.crowntech.pdv2gestao.ui.common.StatusBadge
import br.com.crowntech.pdv2gestao.ui.common.emptyItem
import br.com.crowntech.pdv2gestao.ui.common.errorItem
import br.com.crowntech.pdv2gestao.ui.common.loadingItem
import br.com.crowntech.pdv2gestao.ui.common.rememberLoad
import br.com.crowntech.pdv2gestao.ui.common.staleItem
import br.com.crowntech.pdv2gestao.ui.theme.dangerColor
import br.com.crowntech.pdv2gestao.ui.theme.successColor
import br.com.crowntech.pdv2gestao.ui.theme.warningColor

@Composable
fun terminalColor(status: String): Color = when (status) {
    "online" -> successColor()
    "offline" -> warningColor()
    else -> MaterialTheme.colorScheme.onSurfaceVariant
}

@Composable
fun TerminalsScreen(vm: AppViewModel, onBack: () -> Unit) {
    PageScaffold(title = "Terminais", onBack = onBack) { padding ->
        Column(Modifier.padding(padding)) {
            ContextBar(vm, showPeriod = false)
            RequireTenant(vm) { scope ->
                val state = rememberLoad(scope.tenantId, scope.shopId, onError = vm::handleDataError) { vm.api.terminals(scope) }
                RefreshableList(state.refreshing, { state.reload(true) }) {
                    val d = state.data
                    val error = state.error
                    when {
                        d != null -> {
                            staleItem(state.staleError, state.updatedAt)
                            if (d.results.isEmpty()) emptyItem("Nenhum terminal cadastrado.")
                            items(d.results, key = { it.id }) { TerminalRow(it) }
                        }
                        error != null -> errorItem(error) { state.reload() }
                        else -> loadingItem()
                    }
                }
            }
        }
    }
}

@Composable
private fun TerminalRow(t: Terminal) {
    ListRow {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(t.name, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            StatusBadge(Labels.terminalStatus(t.status), terminalColor(t.status))
        }
        Text(t.shop?.name ?: "—", style = MaterialTheme.typography.bodyMedium)
        Text(
            listOfNotNull(
                t.version.takeIf { it.isNotBlank() }?.let { "Versão $it" },
                "Último contato " + (t.lastSeenAt?.let { Format.relative(it) } ?: "—"),
            ).joinToString(" · "),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (t.pendingEvents > 0) {
            Text(if (t.pendingEvents == 1L) "1 venda na fila" else "${t.pendingEvents} vendas na fila",
                style = MaterialTheme.typography.bodyMedium, color = warningColor())
        }
        if (t.error.isNotBlank()) Text(t.error, style = MaterialTheme.typography.bodySmall, color = dangerColor())
    }
}
