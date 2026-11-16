package br.com.crowntech.pdv2gestao.feature.login

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Fingerprint
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import br.com.crowntech.pdv2gestao.core.biometric.BiometricLock

/** Trava biométrica (abertura a frio ou volta do segundo plano após 5 min). */
@Composable
fun LockScreen(onUnlocked: () -> Unit, onLogout: () -> Unit) {
    val activity = LocalContext.current as? FragmentActivity
    var attempt by remember { mutableIntStateOf(0) }
    LaunchedEffect(attempt) {
        if (activity == null) {
            onUnlocked()
            return@LaunchedEffect
        }
        // Sem biometria nem bloqueio de tela configurado: não há como travar; segue.
        if (!BiometricLock.isAvailable(activity)) {
            onUnlocked()
            return@LaunchedEffect
        }
        if (BiometricLock.authenticate(activity)) onUnlocked()
    }
    Column(
        Modifier.fillMaxSize().safeDrawingPadding().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
    ) {
        BrandMark()
        Text("PDV2 Gestão", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() })
        Text(BiometricLock.PROMPT_SUBTITLE, style = MaterialTheme.typography.bodyLarge, textAlign = TextAlign.Center,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Button(onClick = { attempt++ }, modifier = Modifier.fillMaxWidth()) {
            Icon(Icons.Filled.Fingerprint, contentDescription = null, modifier = Modifier.size(20.dp))
            Text("  Desbloquear")
        }
        TextButton(onClick = onLogout) { Text("Sair da conta") }
    }
}

/** S4: pergunta uma vez, depois do 1º login, se quer usar biometria. */
@Composable
fun BiometricOfferScreen(onAnswer: (Boolean) -> Unit) {
    Column(
        Modifier.fillMaxSize().safeDrawingPadding().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
    ) {
        Icon(Icons.Filled.Fingerprint, contentDescription = null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(72.dp))
        Text("Abrir o app com biometria?", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold,
            textAlign = TextAlign.Center, modifier = Modifier.semantics { heading() })
        Text(
            "Ao abrir o PDV2 Gestão, o aparelho pede sua digital, rosto ou o bloqueio de tela. Você pode mudar isso depois em Conta.",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Button(onClick = { onAnswer(true) }, modifier = Modifier.fillMaxWidth()) { Text("Usar") }
        OutlinedButton(onClick = { onAnswer(false) }, modifier = Modifier.fillMaxWidth()) { Text("Agora não") }
    }
}
