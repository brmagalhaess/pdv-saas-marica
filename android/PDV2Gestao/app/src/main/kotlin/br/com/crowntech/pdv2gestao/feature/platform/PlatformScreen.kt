package br.com.crowntech.pdv2gestao.feature.platform

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.format.Labels
import br.com.crowntech.pdv2gestao.core.model.AppAccessObj
import br.com.crowntech.pdv2gestao.core.model.PlatformDevice
import br.com.crowntech.pdv2gestao.core.model.PlatformTenant
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.ui.common.ConfirmDialog
import br.com.crowntech.pdv2gestao.ui.common.InfiniteScroll
import br.com.crowntech.pdv2gestao.ui.common.ListRow
import br.com.crowntech.pdv2gestao.ui.common.PageResult
import br.com.crowntech.pdv2gestao.ui.common.PagedState
import br.com.crowntech.pdv2gestao.ui.common.ReasonDialog
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
import br.com.crowntech.pdv2gestao.ui.theme.successColor
import br.com.crowntech.pdv2gestao.ui.theme.warningColor
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@Composable
fun healthColor(health: String) = when (health) {
    "ok" -> successColor()
    "attention" -> warningColor()
    else -> dangerColor()
}

@Composable
fun PlatformScreen(vm: AppViewModel, onGrant: () -> Unit) {
    val me by vm.me.collectAsStateWithLifecycle()
    val canManage = me?.platform?.canManageAccess == true
    var section by rememberSaveable { mutableStateOf("tenants") }
    PageScaffold(
        title = "Plataforma",
        actions = {
            if (canManage && section == "access") {
                IconButton(onClick = onGrant) { Icon(Icons.Filled.Add, contentDescription = "Liberar acesso") }
            }
        },
    ) { padding ->
        Column(Modifier.padding(padding)) {
            if (canManage) {
                Segmented(
                    options = listOf("tenants" to "Empresas", "access" to "Acessos", "devices" to "Aparelhos"),
                    selected = section,
                    onSelect = { section = it },
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
                )
            }
            when (if (canManage) section else "tenants") {
                "access" -> AccessList(vm)
                "devices" -> DevicesList(vm)
                else -> TenantsList(vm)
            }
        }
    }
}

@Composable
private fun SearchField(value: String, onChange: (String) -> Unit, label: String) {
    OutlinedTextField(
        value = value,
        onValueChange = { onChange(it.take(120)) },
        label = { Text(label) },
        leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
        singleLine = true,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
    )
}

/** Espera o usuário parar de digitar antes de buscar. */
@Composable
private fun debounced(value: String): String {
    var current by remember { mutableStateOf(value) }
    LaunchedEffect(value) {
        delay(350)
        current = value
    }
    return current
}

@Composable
private fun TenantsList(vm: AppViewModel) {
    var query by rememberSaveable { mutableStateOf("") }
    var attention by rememberSaveable { mutableStateOf(false) }
    var chosen by remember { mutableStateOf<PlatformTenant?>(null) }
    val q = debounced(query.trim())
    SearchField(query, { query = it }, "Buscar empresa")
    Row(Modifier.padding(horizontal = 16.dp)) {
        FilterChip(selected = attention, onClick = { attention = !attention }, label = { Text("Precisam de atenção") })
    }
    val paged = rememberPaged<Unit, PlatformTenant>(q, attention, onError = {}) { cursor ->
        val page = vm.api.platformTenants(q.ifBlank { null }, attention, cursor)
        PageResult(null, page.results, page.nextCursor)
    }
    PagedList(paged, empty = "Nenhuma empresa encontrada.") { t ->
        ListRow(onClick = { chosen = t }) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(t.name, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                StatusBadge(t.healthText.ifBlank { t.health }, healthColor(t.health))
            }
            Text("Assinatura: " + t.subscriptionStatusText.ifBlank { "—" }, style = MaterialTheme.typography.bodyMedium)
            Text(
                "${t.terminalsOffline} terminais sem contato · ${t.conflictsOpen} conflitos",
                style = MaterialTheme.typography.bodySmall,
                color = if (t.terminalsOffline > 0 || t.conflictsOpen > 0) warningColor() else MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text("Última venda: " + (t.lastSaleAt?.let { Format.dateTime(it) } ?: "—"), style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
    chosen?.let { t ->
        ConfirmDialog(
            title = t.name,
            message = "Abrir esta empresa para ver o Resumo, as vendas e o caixa?",
            confirmLabel = "Abrir empresa",
            destructive = false,
            onDismiss = { chosen = null },
            onConfirm = {
                chosen = null
                vm.openTenantFromPlatform(t.id, t.name)
            },
        )
    }
}

@Composable
private fun AccessList(vm: AppViewModel) {
    val scope = rememberCoroutineScope()
    var query by rememberSaveable { mutableStateOf("") }
    var activeOnly by rememberSaveable { mutableStateOf(true) }
    var toRevoke by remember { mutableStateOf<AppAccessObj?>(null) }
    var busy by remember { mutableStateOf(false) }
    var revokeError by remember { mutableStateOf<String?>(null) }
    val q = debounced(query.trim())
    SearchField(query, { query = it }, "Buscar pessoa ou empresa")
    Row(Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        FilterChip(selected = activeOnly, onClick = { activeOnly = true }, label = { Text("Ativos") })
        FilterChip(selected = !activeOnly, onClick = { activeOnly = false }, label = { Text("Todos") })
    }
    val paged = rememberPaged<Unit, AppAccessObj>(q, activeOnly, onError = {}) { cursor ->
        val page = vm.api.platformAccess(activeOnly, q.ifBlank { null }, cursor)
        PageResult(null, page.results, page.nextCursor)
    }
    PagedList(paged, empty = "Nenhum acesso encontrado.") { a ->
        var menu by remember { mutableStateOf(false) }
        ListRow {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(a.user.name.ifBlank { a.user.email }, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
                    Text(a.user.email, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                StatusBadge(a.statusText.ifBlank { Labels.accessStatus(a.status) }, if (a.status == "active") successColor() else dangerColor())
                if (a.status == "active") {
                    Box {
                        IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, contentDescription = "Ações de ${a.user.name}") }
                        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                            DropdownMenuItem(text = { Text("Revogar", color = dangerColor()) }, onClick = { menu = false; toRevoke = a })
                        }
                    }
                }
            }
            Text(if (a.scope == "platform") "Equipe CrownTech" else a.tenant?.name ?: "—", style = MaterialTheme.typography.bodyMedium)
            if (a.scope == "tenant") {
                Text("Lojas: " + if (a.allShops) "Todas" else a.shops.joinToString { it.name }, style = MaterialTheme.typography.bodySmall)
                Text("Permissões: " + a.permissions.joinToString { Labels.permission(it) }, style = MaterialTheme.typography.bodySmall)
            }
            Text(
                "Validade: " + (a.expiresOn?.let { Format.date(it) } ?: "Sem prazo") + " · ${a.devicesActive} aparelhos",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
    toRevoke?.let { a ->
        ReasonDialog(
            title = "Revogar acesso",
            message = "${a.user.name.ifBlank { a.user.email }} deixa de ver ${if (a.scope == "platform") "a plataforma" else a.tenant?.name ?: "a empresa"} no app.",
            confirmLabel = "Revogar",
            busy = busy,
            error = revokeError,
            onDismiss = { toRevoke = null; revokeError = null },
            onConfirm = { reason ->
                busy = true
                scope.launch {
                    try {
                        vm.api.revokeAccess(a.id, reason)
                        toRevoke = null
                        revokeError = null
                        paged.reload()
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: ApiException) {
                        revokeError = e.userMessage
                    } finally {
                        busy = false
                    }
                }
            },
        )
    }
}

@Composable
private fun DevicesList(vm: AppViewModel) {
    val scope = rememberCoroutineScope()
    var query by rememberSaveable { mutableStateOf("") }
    var activeOnly by rememberSaveable { mutableStateOf(true) }
    var toRevoke by remember { mutableStateOf<PlatformDevice?>(null) }
    var busy by remember { mutableStateOf(false) }
    var revokeError by remember { mutableStateOf<String?>(null) }
    val q = debounced(query.trim())
    SearchField(query, { query = it }, "Buscar pessoa ou aparelho")
    Row(Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        FilterChip(selected = activeOnly, onClick = { activeOnly = true }, label = { Text("Ativos") })
        FilterChip(selected = !activeOnly, onClick = { activeOnly = false }, label = { Text("Todos") })
    }
    val paged = rememberPaged<Unit, PlatformDevice>(q, activeOnly, onError = {}) { cursor ->
        val page = vm.api.platformDevices(activeOnly, q.ifBlank { null }, cursor)
        PageResult(null, page.results, page.nextCursor)
    }
    PagedList(paged, empty = "Nenhum aparelho encontrado.") { d ->
        ListRow {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(d.user?.name?.ifBlank { d.user.email } ?: "—", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
                    Text(d.name, style = MaterialTheme.typography.bodyMedium)
                }
                if (d.status == "active") {
                    TextButton(onClick = { toRevoke = d }) { Text("Desconectar", color = dangerColor()) }
                } else {
                    StatusBadge("Desconectado", MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            Text(
                listOf(d.osVersion, "App " + d.appVersion).filter { it.isNotBlank() }.joinToString(" · "),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text("Último uso: " + (d.lastSeenAt?.let { Format.relative(it) } ?: "—"), style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
    toRevoke?.let { d ->
        ReasonDialog(
            title = "Desconectar aparelho",
            message = "${d.name} de ${d.user?.name ?: "—"} precisará entrar de novo.",
            confirmLabel = "Desconectar",
            busy = busy,
            error = revokeError,
            onDismiss = { toRevoke = null; revokeError = null },
            onConfirm = { reason ->
                busy = true
                scope.launch {
                    try {
                        vm.api.revokePlatformDevice(d.id, reason)
                        toRevoke = null
                        revokeError = null
                        paged.reload()
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: ApiException) {
                        revokeError = e.userMessage
                    } finally {
                        busy = false
                    }
                }
            },
        )
    }
}

@Composable
private fun <T> PagedList(paged: PagedState<Unit, T>, empty: String, row: @Composable (T) -> Unit) {
    val listState = rememberLazyListState()
    InfiniteScroll(listState, paged.nextCursor != null) { paged.loadMore() }
    RefreshableList(paged.refreshing, { paged.reload(true) }, listState = listState) {
        val error = paged.error
        when {
            paged.loaded -> {
                staleItem(paged.staleError, paged.updatedAt)
                if (paged.items.isEmpty()) emptyItem(empty)
                items(paged.items) { row(it) }
                moreFooter(paged.loadingMore, paged.moreError, paged.nextCursor != null) { paged.loadMore() }
            }
            error != null -> errorItem(error) { paged.reload() }
            else -> loadingItem()
        }
    }
}
