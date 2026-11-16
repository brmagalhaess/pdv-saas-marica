package br.com.crowntech.pdv2gestao.feature.home

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.outlined.Business
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.DataScope
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.core.model.TenantAccess
import br.com.crowntech.pdv2gestao.ui.common.Segmented
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay

/** Estrutura padrão das telas: barra de título (com voltar opcional) e conteúdo. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PageScaffold(
    title: String,
    onBack: (() -> Unit)? = null,
    actions: @Composable RowScope.() -> Unit = {},
    content: @Composable (PaddingValues) -> Unit,
) {
    Scaffold(
        contentWindowInsets = WindowInsets(0),
        topBar = {
            TopAppBar(
                title = { Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                navigationIcon = {
                    if (onBack != null) {
                        IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Voltar") }
                    }
                },
                actions = actions,
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
            )
        },
    ) { padding -> content(padding) }
}

/**
 * Barra de contexto: "Empresa ▾" · "Loja ▾" e, opcionalmente, o período.
 * Trocar de empresa volta para "Todas as lojas".
 */
@Composable
fun ContextBar(vm: AppViewModel, showPeriod: Boolean, modifier: Modifier = Modifier) {
    val ctx by vm.context.collectAsStateWithLifecycle()
    val me by vm.me.collectAsStateWithLifecycle()
    var shopMenu by remember { mutableStateOf(false) }
    val canPick = me?.isPlatform == true || (me?.tenants?.size ?: 0) > 1

    Column(modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            AssistChip(
                onClick = { if (canPick) vm.openTenantPicker() },
                enabled = true,
                label = { Text(ctx.tenantName ?: "Empresa", maxLines = 1, overflow = TextOverflow.Ellipsis) },
                leadingIcon = { Icon(Icons.Outlined.Business, contentDescription = null, modifier = Modifier.size(18.dp)) },
                trailingIcon = if (canPick) ({ Icon(Icons.Filled.ArrowDropDown, contentDescription = "Trocar de empresa") }) else null,
                modifier = Modifier.weight(1f, fill = false),
            )
            Box {
                AssistChip(
                    onClick = { shopMenu = true },
                    label = { Text(ctx.shopName ?: "Todas as lojas", maxLines = 1, overflow = TextOverflow.Ellipsis) },
                    trailingIcon = { Icon(Icons.Filled.ArrowDropDown, contentDescription = "Trocar de loja") },
                )
                DropdownMenu(expanded = shopMenu, onDismissRequest = { shopMenu = false }) {
                    DropdownMenuItem(
                        text = { Text("Todas as lojas") },
                        onClick = { vm.chooseShop(null); shopMenu = false },
                        leadingIcon = { if (ctx.shopId == null) Icon(Icons.Filled.Check, contentDescription = "Selecionada") },
                    )
                    ctx.shops.forEach { shop ->
                        DropdownMenuItem(
                            text = { Text(shop.name) },
                            onClick = { vm.chooseShop(shop.id); shopMenu = false },
                            leadingIcon = { if (ctx.shopId == shop.id) Icon(Icons.Filled.Check, contentDescription = "Selecionada") },
                        )
                    }
                }
            }
        }
        if (showPeriod) {
            Segmented(
                options = PeriodKey.entries.map { it to it.label },
                selected = ctx.period,
                onSelect = vm::choosePeriod,
            )
        }
    }
}

/**
 * Garante que há empresa escolhida. Plataforma sem empresa: orienta a usar a aba Plataforma.
 */
@Composable
fun RequireTenant(vm: AppViewModel, content: @Composable (DataScope) -> Unit) {
    val ctx by vm.context.collectAsStateWithLifecycle()
    val me by vm.me.collectAsStateWithLifecycle()
    val scope = ctx.scope()
    if (scope != null) {
        content(scope)
        return
    }
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
    ) {
        if (me?.isPlatform == true) {
            Text("Escolha uma empresa na aba Plataforma.", style = MaterialTheme.typography.bodyLarge, textAlign = TextAlign.Center)
            Button(onClick = { vm.requestTab(Routes.PLATFORM) }) { Text("Ir para Plataforma") }
        } else {
            Text("Escolha uma empresa.", style = MaterialTheme.typography.bodyLarge, textAlign = TextAlign.Center)
            Button(onClick = vm::openTenantPicker) { Text("Escolher empresa") }
        }
    }
}

/** Lista de empresas (/app/tenants); busca só para a plataforma. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TenantPickerSheet(vm: AppViewModel, onDismiss: () -> Unit) {
    val me by vm.me.collectAsStateWithLifecycle()
    val ctx by vm.context.collectAsStateWithLifecycle()
    val platform = me?.isPlatform == true
    var query by rememberSaveable { mutableStateOf("") }
    val results = remember { mutableStateListOf<TenantAccess>() }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val sheet = rememberModalBottomSheetState(skipPartiallyExpanded = true)

    LaunchedEffect(query, platform) {
        if (!platform) {
            results.clear()
            results.addAll(me?.tenants.orEmpty())
            return@LaunchedEffect
        }
        delay(300)
        loading = true
        try {
            val page = vm.api.tenants(query.trim().ifBlank { null })
            results.clear()
            results.addAll(page.results)
            error = null
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiException) {
            error = e.userMessage
        } finally {
            loading = false
        }
    }

    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheet) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Escolha a empresa", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
            if (platform) {
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it.take(120) },
                    label = { Text("Buscar empresa") },
                    leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            if (loading) {
                Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            }
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            LazyColumn(Modifier.fillMaxWidth().heightIn(max = 480.dp)) {
                items(results, key = { it.id }) { tenant ->
                    TenantRow(tenant, selected = tenant.id == ctx.tenantId) { vm.chooseTenant(tenant) }
                    HorizontalDivider()
                }
                if (!loading && results.isEmpty()) {
                    item { Text("Nenhuma empresa encontrada.", color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(16.dp)) }
                }
            }
        }
    }
}

@Composable
private fun TenantRow(tenant: TenantAccess, selected: Boolean, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(role = Role.Button, onClick = onClick).padding(vertical = 14.dp, horizontal = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(tenant.name, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium)
            val shops = if (tenant.allShops) "Todas as lojas" else tenant.shops.joinToString { it.name }
            Text(shops, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1,
                overflow = TextOverflow.Ellipsis)
        }
        if (selected) Icon(Icons.Filled.Check, contentDescription = "Empresa atual", tint = MaterialTheme.colorScheme.primary)
    }
}

/** Atalho: conteúdo em coluna com padding (telas simples). */
@Composable
fun PaddedColumn(padding: PaddingValues, content: @Composable ColumnScope.() -> Unit) {
    Column(Modifier.fillMaxSize().padding(padding), content = content)
}
