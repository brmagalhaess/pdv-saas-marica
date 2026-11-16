package br.com.crowntech.pdv2gestao.feature.platform

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.SelectableDates
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.format.Labels
import br.com.crowntech.pdv2gestao.core.model.ALL_PERMISSIONS
import br.com.crowntech.pdv2gestao.core.model.AccessOptions
import br.com.crowntech.pdv2gestao.core.model.GrantRequest
import br.com.crowntech.pdv2gestao.core.model.PlatformTenant
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.ui.common.Segmented
import br.com.crowntech.pdv2gestao.ui.theme.dangerColor
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.datetime.Clock
import kotlinx.datetime.Instant
import kotlinx.datetime.LocalDate
import kotlinx.datetime.TimeZone
import kotlinx.datetime.toLocalDateTime

/** Converte o milissegundo UTC do DatePicker em "YYYY-MM-DD". */
internal fun pickerMillisToDate(millis: Long): LocalDate =
    Instant.fromEpochMilliseconds(millis).toLocalDateTime(TimeZone.UTC).date

/** Valida a concessão antes de enviar (mesmas regras do servidor, §6.18). Retorna a mensagem de erro ou null. */
internal fun validateGrant(
    scope: String,
    tenantId: String?,
    userId: Long?,
    permissions: Collection<String>,
    reason: String,
    allShops: Boolean = true,
    shopIds: Collection<String> = emptyList(),
): String? = when {
    scope == "tenant" && tenantId == null -> "Escolha a empresa."
    userId == null -> "Escolha a pessoa."
    // shop_ids vazio no servidor = TODAS as lojas: nunca enviar vazio quando o admin desmarcou "Todas".
    scope == "tenant" && !allShops && shopIds.isEmpty() -> "Marque pelo menos uma loja ou escolha Todas as lojas."
    scope == "tenant" && permissions.isEmpty() -> "Marque pelo menos uma permissão."
    reason.trim().length !in 3..240 -> "Escreva o motivo (mínimo 3 letras)."
    else -> null
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AccessGrantScreen(vm: AppViewModel, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    var kind by rememberSaveable { mutableStateOf("tenant") }
    var tenantQuery by rememberSaveable { mutableStateOf("") }
    var tenant by remember { mutableStateOf<PlatformTenant?>(null) }
    val tenantResults = remember { mutableStateListOf<PlatformTenant>() }
    var options by remember { mutableStateOf<AccessOptions?>(null) }
    var optionsError by remember { mutableStateOf<String?>(null) }
    var userId by remember { mutableStateOf<Long?>(null) }
    var allShops by remember { mutableStateOf(true) }
    val shopIds = remember { mutableStateListOf<String>() }
    val permissions = remember { mutableStateListOf<String>().apply { addAll(ALL_PERMISSIONS) } }
    var expiresOn by remember { mutableStateOf<LocalDate?>(null) }
    var showDate by remember { mutableStateOf(false) }
    var reason by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var done by remember { mutableStateOf<String?>(null) }

    // Busca de empresas (só para o tipo Empresa cliente).
    LaunchedEffect(tenantQuery, kind, tenant) {
        if (kind != "tenant" || tenant != null) return@LaunchedEffect
        delay(300)
        try {
            val page = vm.api.platformTenants(tenantQuery.trim().ifBlank { null }, false, null)
            tenantResults.clear()
            tenantResults.addAll(page.results.take(20))
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiException) {
            optionsError = e.userMessage
        }
    }
    // Membros e lojas da empresa (ou equipe CrownTech).
    LaunchedEffect(kind, tenant?.id) {
        options = null
        userId = null
        shopIds.clear()
        allShops = true
        optionsError = null
        if (kind == "tenant" && tenant == null) return@LaunchedEffect
        try {
            options = vm.api.accessOptions(if (kind == "tenant") tenant?.id else null)
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiException) {
            optionsError = e.userMessage
        }
    }

    PageScaffold(title = "Liberar acesso", onBack = onBack) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).imePadding().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            if (done != null) {
                Text(done!!, style = MaterialTheme.typography.bodyLarge)
                Button(onClick = onBack, modifier = Modifier.fillMaxWidth()) { Text("Ok") }
                return@Column
            }
            Text("O app é só de consulta. A pessoa entra com o mesmo e-mail e senha do painel.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)

            Label("Tipo")
            Segmented(
                options = listOf("tenant" to "Empresa cliente", "platform" to "Equipe CrownTech"),
                selected = kind,
                onSelect = { kind = it; tenant = null },
            )

            if (kind == "tenant") {
                Label("Empresa")
                val t = tenant
                if (t != null) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(t.name, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
                        TextButton(onClick = { tenant = null }) { Text("Trocar") }
                    }
                } else {
                    OutlinedTextField(
                        value = tenantQuery,
                        onValueChange = { tenantQuery = it.take(120) },
                        label = { Text("Buscar empresa") },
                        leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    tenantResults.forEach { item ->
                        Text(
                            item.name,
                            style = MaterialTheme.typography.bodyLarge,
                            modifier = Modifier.fillMaxWidth().clickable(role = Role.Button) { tenant = item }.padding(vertical = 10.dp),
                        )
                        HorizontalDivider()
                    }
                }
            }

            optionsError?.let { Text(it, color = dangerColor()) }
            val opts = options
            if (opts == null && (kind == "platform" || tenant != null) && optionsError == null) {
                CircularProgressIndicator()
            }
            if (opts != null) {
                Label("Pessoa")
                if (opts.members.isEmpty()) {
                    Text(if (kind == "tenant") "Nenhuma pessoa na equipe desta empresa." else "Nenhuma pessoa da equipe CrownTech disponível.",
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                opts.members.forEach { m ->
                    val enabled = !m.hasActiveAccess
                    Row(
                        Modifier.fillMaxWidth().clickable(enabled = enabled, role = Role.RadioButton) { userId = m.userId },
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        RadioButton(selected = userId == m.userId, onClick = { userId = m.userId }, enabled = enabled)
                        Column(Modifier.weight(1f)) {
                            Text(m.name.ifBlank { m.email }, style = MaterialTheme.typography.bodyLarge)
                            Text(
                                listOf(m.email, m.roleText, if (m.hasActiveAccess) "já tem acesso" else "").filter { it.isNotBlank() }.joinToString(" · "),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }

                if (kind == "tenant") {
                    Label("Lojas")
                    CheckRow("Todas as lojas", allShops) { allShops = it; if (it) shopIds.clear() }
                    if (!allShops) {
                        if (opts.shops.isEmpty()) {
                            Text("Esta empresa não tem lojas ativas para escolher.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        opts.shops.forEach { shop ->
                            CheckRow(shop.name, shop.id in shopIds) { on -> if (on) shopIds.add(shop.id) else shopIds.remove(shop.id) }
                        }
                    }
                    Label("Permissões")
                    ALL_PERMISSIONS.forEach { p ->
                        CheckRow(Labels.permission(p), p in permissions) { on -> if (on) permissions.add(p) else permissions.remove(p) }
                    }
                }
            }

            Label("Validade")
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(expiresOn?.let { Format.date(it.toString()) } ?: "Sem prazo", style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
                TextButton(onClick = { showDate = true }) { Text("Escolher data") }
                if (expiresOn != null) TextButton(onClick = { expiresOn = null }) { Text("Sem prazo") }
            }

            OutlinedTextField(
                value = reason,
                onValueChange = { reason = it.take(240) },
                label = { Text("Motivo") },
                supportingText = { Text("Obrigatório. Ex.: Dona da empresa pediu acesso ao app.") },
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences),
                modifier = Modifier.fillMaxWidth(),
            )
            error?.let { Text(it, color = dangerColor()) }
            val missingShops = kind == "tenant" && !allShops && shopIds.isEmpty()
            if (missingShops) {
                Text("Marque pelo menos uma loja ou escolha Todas as lojas.", color = dangerColor())
            }
            Button(
                enabled = !busy && !missingShops,
                modifier = Modifier.fillMaxWidth(),
                onClick = {
                    val problem = validateGrant(kind, tenant?.id, userId, permissions, reason, allShops, shopIds)
                    if (problem != null) {
                        error = problem
                        return@Button
                    }
                    busy = true
                    error = null
                    val request = if (kind == "tenant") {
                        GrantRequest(
                            scope = "tenant",
                            userId = userId!!,
                            tenantId = tenant!!.id,
                            shopIds = if (allShops) emptyList() else shopIds.toList(),
                            permissions = ALL_PERMISSIONS.filter { it in permissions },
                            expiresOn = expiresOn?.toString(),
                            reason = reason.trim(),
                        )
                    } else {
                        GrantRequest(scope = "platform", userId = userId!!, expiresOn = expiresOn?.toString(), reason = reason.trim())
                    }
                    scope.launch {
                        try {
                            val access = vm.api.grantAccess(request)
                            done = "Acesso liberado para ${access.user.name.ifBlank { access.user.email }}."
                        } catch (e: CancellationException) {
                            throw e
                        } catch (e: ApiException) {
                            error = e.userMessage
                        } finally {
                            busy = false
                        }
                    }
                },
            ) {
                if (busy) CircularProgressIndicator(strokeWidth = 2.dp) else Text("Liberar acesso")
            }
            OutlinedButton(onClick = onBack, modifier = Modifier.fillMaxWidth()) { Text("Cancelar") }
        }
    }

    if (showDate) {
        val today = Clock.System.now().toLocalDateTime(Format.zone).date
        val state = rememberDatePickerState(selectableDates = object : SelectableDates {
            override fun isSelectableDate(utcTimeMillis: Long): Boolean = pickerMillisToDate(utcTimeMillis) >= today
        })
        DatePickerDialog(
            onDismissRequest = { showDate = false },
            confirmButton = {
                TextButton(onClick = {
                    state.selectedDateMillis?.let { expiresOn = pickerMillisToDate(it) }
                    showDate = false
                }) { Text("Confirmar") }
            },
            dismissButton = { TextButton(onClick = { showDate = false }) { Text("Cancelar") } },
        ) {
            DatePicker(state = state, title = { Text("Válido até", modifier = Modifier.padding(16.dp)) })
        }
    }
}

@Composable
private fun Label(text: String) {
    Text(text, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 4.dp).semantics { heading() })
}

@Composable
private fun CheckRow(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().toggleable(value = checked, role = Role.Checkbox, onValueChange = onChange),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(checked = checked, onCheckedChange = null)
        Text(label, style = MaterialTheme.typography.bodyLarge)
    }
}
