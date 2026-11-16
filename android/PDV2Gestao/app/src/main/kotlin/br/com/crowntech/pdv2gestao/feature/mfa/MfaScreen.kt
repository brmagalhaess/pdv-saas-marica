package br.com.crowntech.pdv2gestao.feature.mfa

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.Screen
import br.com.crowntech.pdv2gestao.core.biometric.BiometricLock
import br.com.crowntech.pdv2gestao.feature.login.BrandMark
import br.com.crowntech.pdv2gestao.ui.theme.dangerColor
import kotlinx.coroutines.launch

/** Normaliza o código de recuperação para PDV2-XXXX-XXXX. */
fun normalizeRecoveryCode(raw: String): String {
    val clean = raw.uppercase().filter { it.isLetterOrDigit() }
    val body = if (clean.startsWith("PDV2")) clean.drop(4) else clean
    if (body.length != 8) return raw.trim().uppercase()
    return "PDV2-${body.take(4)}-${body.drop(4)}"
}

/** S2 Código de verificação (só equipe da plataforma). */
@Composable
fun MfaScreen(vm: AppViewModel, state: Screen.Mfa) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var useRecovery by rememberSaveable { mutableStateOf(false) }
    var code by rememberSaveable { mutableStateOf("") }
    var recovery by rememberSaveable { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }

    val valid = if (useRecovery) recovery.filter { it.isLetterOrDigit() }.length >= 8 else code.length == 6

    fun submit() {
        if (!valid || busy) return
        busy = true
        message = null
        scope.launch {
            val available = (context as? FragmentActivity)?.let { BiometricLock.isAvailable(it) } ?: false
            message = if (useRecovery) {
                vm.verifyMfa(state.token, null, normalizeRecoveryCode(recovery), available)
            } else {
                vm.verifyMfa(state.token, code, null, available)
            }
            busy = false
        }
    }

    Column(
        Modifier.fillMaxSize().safeDrawingPadding().imePadding().verticalScroll(rememberScrollState()).padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        BrandMark()
        Text("Código de verificação", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() })
        state.user?.email?.takeIf { it.isNotBlank() }?.let {
            Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (!useRecovery) {
            Text("Digite o código de 6 números do seu aplicativo autenticador.", textAlign = TextAlign.Center,
                style = MaterialTheme.typography.bodyLarge)
            OutlinedTextField(
                value = code,
                onValueChange = { v -> code = v.filter { it.isDigit() }.take(6) },
                label = { Text("Código") },
                singleLine = true,
                enabled = !busy,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { submit() }),
                modifier = Modifier.fillMaxWidth(),
            )
            TextButton(onClick = { useRecovery = true; message = null }) { Text("Usar código de recuperação") }
        } else {
            Text("Digite um dos seus códigos de recuperação.", textAlign = TextAlign.Center, style = MaterialTheme.typography.bodyLarge)
            OutlinedTextField(
                value = recovery,
                onValueChange = { recovery = it.take(20) },
                label = { Text("Código de recuperação") },
                placeholder = { Text("PDV2-XXXX-XXXX") },
                singleLine = true,
                enabled = !busy,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { submit() }),
                modifier = Modifier.fillMaxWidth(),
            )
            TextButton(onClick = { useRecovery = false; message = null }) { Text("Usar o aplicativo autenticador") }
        }
        if (message != null) {
            Text(message!!, color = dangerColor(), textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth())
        }
        Button(onClick = ::submit, enabled = valid && !busy, modifier = Modifier.fillMaxWidth().height(52.dp)) {
            if (busy) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
            else Text("Confirmar")
        }
        OutlinedButton(onClick = vm::backToLogin, enabled = !busy, modifier = Modifier.fillMaxWidth()) { Text("Voltar") }
    }
}
