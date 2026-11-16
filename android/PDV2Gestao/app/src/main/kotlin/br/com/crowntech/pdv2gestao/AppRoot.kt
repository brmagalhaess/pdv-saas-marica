package br.com.crowntech.pdv2gestao

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import br.com.crowntech.pdv2gestao.feature.accessdenied.AccessDeniedScreen
import br.com.crowntech.pdv2gestao.feature.home.HomeScreen
import br.com.crowntech.pdv2gestao.feature.login.BiometricOfferScreen
import br.com.crowntech.pdv2gestao.feature.login.LockScreen
import br.com.crowntech.pdv2gestao.feature.login.LoginScreen
import br.com.crowntech.pdv2gestao.feature.mfa.MfaScreen

@Composable
fun AppRoot(vm: AppViewModel) {
    val screen by vm.screen.collectAsStateWithLifecycle()
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        when (val s = screen) {
            Screen.Starting -> Box(Modifier.fillMaxSize().semantics { contentDescription = "Carregando" }, contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
            is Screen.Locked -> LockScreen(onUnlocked = { vm.unlocked(s.coldStart) }, onLogout = vm::logout)
            is Screen.Login -> LoginScreen(vm, s.message)
            is Screen.Mfa -> {
                BackHandler { vm.backToLogin() }
                MfaScreen(vm, s)
            }
            Screen.AccessDenied -> AccessDeniedScreen(onRetry = vm::backToLogin)
            Screen.BiometricOffer -> BiometricOfferScreen(onAnswer = vm::answerBiometricOffer)
            Screen.Home -> HomeScreen(vm)
        }
    }
}
