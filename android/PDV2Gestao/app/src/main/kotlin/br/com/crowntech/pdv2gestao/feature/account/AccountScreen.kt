package br.com.crowntech.pdv2gestao.feature.account

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.AppConfig
import br.com.crowntech.pdv2gestao.core.biometric.BiometricLock
import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.model.Device
import br.com.crowntech.pdv2gestao.core.model.Links
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.ui.common.ConfirmDialog
import br.com.crowntech.pdv2gestao.ui.common.RefreshableList
import br.com.crowntech.pdv2gestao.ui.common.SectionCard
import br.com.crowntech.pdv2gestao.ui.common.StatRow
import br.com.crowntech.pdv2gestao.ui.common.openUrl
import br.com.crowntech.pdv2gestao.ui.common.rememberLoad
import br.com.crowntech.pdv2gestao.ui.theme.dangerColor
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

@Composable
fun AccountScreen(vm: AppViewModel, onBack: () -> Unit, onDelete: () -> Unit) {
    val me by vm.me.collectAsStateWithLifecycle()
    val biometric by vm.biometricEnabled.collectAsStateWithLifecycle()
    val demo by vm.demoActive.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val links = me?.links ?: Links()
    val devices = rememberLoad("devices", onError = {}) { vm.api.devices() }
    var toRevoke by remember { mutableStateOf<Device?>(null) }
    var actionError by remember { mutableStateOf<String?>(null) }
    var confirmLogout by remember { mutableStateOf(false) }
    val biometricAvailable = remember { (context as? FragmentActivity)?.let { BiometricLock.isAvailable(it) } ?: false }

    PageScaffold(title = "Conta", onBack = onBack) { padding ->
        RefreshableList(devices.refreshing, { devices.reload(true) }, modifier = Modifier.padding(padding)) {
            item(key = "me") {
                SectionCard {
                    Text(me?.user?.name ?: "", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
                    Text(me?.user?.email ?: "", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            me?.device?.let { d ->
                item(key = "this-device") {
                    SectionCard(title = "Este aparelho") {
                        StatRow("Nome", d.name)
                        StatRow("Sistema", d.osVersion.ifBlank { "—" })
                        StatRow("Conectado em", Format.dateTime(d.createdAt))
                    }
                }
            }
            item(key = "devices") {
                SectionCard(title = "Aparelhos conectados") {
                    val list = devices.data?.results
                    when {
                        list != null -> {
                            if (list.isEmpty()) Text("Nenhum aparelho.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                            list.forEachIndexed { index, d ->
                                if (index > 0) HorizontalDivider(Modifier.padding(vertical = 4.dp))
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Column(Modifier.weight(1f)) {
                                        Text(d.name + if (d.current) " (este aparelho)" else "", style = MaterialTheme.typography.bodyLarge)
                                        Text(
                                            listOf(d.osVersion, "Último uso " + (d.lastSeenAt?.let { Format.relative(it) } ?: "—"))
                                                .filter { it.isNotBlank() }.joinToString(" · "),
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        )
                                    }
                                    TextButton(onClick = { toRevoke = d }, modifier = Modifier.semantics { contentDescription = "Desconectar ${d.name}" }) {
                                        Text("Desconectar", color = dangerColor())
                                    }
                                }
                            }
                        }
                        devices.error != null -> Text(devices.error!!.userMessage, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        else -> CircularProgressIndicator()
                    }
                    actionError?.let { Text(it, color = dangerColor()) }
                }
            }
            item(key = "security") {
                SectionCard(title = "Segurança") {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("Abrir com biometria", style = MaterialTheme.typography.bodyLarge)
                            Text(
                                when {
                                    demo -> "Indisponível na demonstração."
                                    biometricAvailable -> "Pede digital, rosto ou bloqueio de tela ao abrir o app."
                                    else -> "Configure a biometria ou o bloqueio de tela do aparelho para usar."
                                },
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        Switch(
                            checked = biometric,
                            onCheckedChange = { vm.setBiometric(it) },
                            enabled = !demo && (biometricAvailable || biometric),
                            modifier = Modifier.semantics { contentDescription = "Abrir com biometria" },
                        )
                    }
                }
            }
            item(key = "links") {
                SectionCard {
                    TextButton(onClick = { openUrl(context, links.privacyUrl) }) { Text("Política de privacidade") }
                    TextButton(onClick = { openUrl(context, links.supportUrl) }) { Text("Suporte") }
                    TextButton(onClick = onDelete) { Text("Excluir minha conta", color = dangerColor()) }
                }
            }
            item(key = "logout") {
                OutlinedButton(onClick = { confirmLogout = true }, modifier = Modifier.fillMaxWidth()) { Text("Sair") }
            }
            item(key = "version") {
                Text("PDV2 Gestão ${AppConfig.appVersionLabel}", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.fillMaxWidth().padding(top = 8.dp))
            }
        }
    }

    toRevoke?.let { device ->
        ConfirmDialog(
            title = "Desconectar aparelho?",
            message = if (device.current) "Este aparelho sairá da sua conta agora." else "\"${device.name}\" precisará entrar de novo para usar o app.",
            confirmLabel = "Desconectar",
            onDismiss = { toRevoke = null },
            onConfirm = {
                toRevoke = null
                scope.launch {
                    try {
                        vm.api.revokeDevice(device.id)
                        actionError = null
                        if (device.current) vm.localLogout(null) else devices.reload()
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: ApiException) {
                        actionError = e.userMessage
                    }
                }
            },
        )
    }
    if (confirmLogout) {
        ConfirmDialog(
            title = if (demo) "Sair da demonstração?" else "Sair da conta?",
            message = if (demo) "Você volta para a tela Entrar." else "Você precisará do e-mail e da senha para entrar de novo.",
            confirmLabel = "Sair",
            onDismiss = { confirmLogout = false },
            onConfirm = { confirmLogout = false; vm.logout() },
        )
    }
}

/** Excluir minha conta: pedido registrado + saída de todos os aparelhos. */
@Composable
fun DeleteAccountScreen(vm: AppViewModel, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    var note by rememberSaveable { mutableStateOf("") }
    var confirm by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var done by remember { mutableStateOf<String?>(null) }

    PageScaffold(title = "Excluir minha conta", onBack = if (done == null) onBack else null) { padding ->
        Column(
            Modifier.padding(padding).padding(16.dp).imePadding(),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            if (done != null) {
                Text(done!!, style = MaterialTheme.typography.bodyLarge)
                Button(onClick = { vm.localLogout(null) }, modifier = Modifier.fillMaxWidth()) { Text("Ok") }
                return@Column
            }
            Text(
                "O pedido desativa seu login no PDV2 (app e painel) e apaga seus dados pessoais em até 15 dias. " +
                    "Os dados da empresa, como vendas e notas, continuam com a empresa, como exige a lei.",
                style = MaterialTheme.typography.bodyLarge,
            )
            OutlinedTextField(
                value = note,
                onValueChange = { note = it.take(500) },
                label = { Text("Motivo (opcional)") },
                minLines = 3,
                enabled = !busy,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences),
                modifier = Modifier.fillMaxWidth(),
            )
            error?.let { Text(it, color = dangerColor()) }
            Button(
                onClick = { confirm = true },
                enabled = !busy,
                colors = ButtonDefaults.buttonColors(containerColor = dangerColor(), contentColor = MaterialTheme.colorScheme.onError),
                modifier = Modifier.fillMaxWidth(),
            ) {
                if (busy) CircularProgressIndicator(strokeWidth = 2.dp) else Text("Pedir exclusão")
            }
        }
    }

    if (confirm) {
        ConfirmDialog(
            title = "Pedir exclusão da conta?",
            message = "Você sairá de todos os aparelhos agora. Seu login será desativado em até 15 dias.",
            confirmLabel = "Pedir exclusão",
            onDismiss = { confirm = false },
            onConfirm = {
                confirm = false
                busy = true
                scope.launch {
                    try {
                        val r = vm.api.requestDeletion(note)
                        done = r.message.ifBlank {
                            "Recebemos seu pedido. Seu login será desativado e seus dados pessoais apagados em até 15 dias. Você vai receber a confirmação por e-mail."
                        }
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: ApiException) {
                        error = e.userMessage
                    } finally {
                        busy = false
                    }
                }
            },
        )
    }
}
