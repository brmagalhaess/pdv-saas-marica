package br.com.crowntech.pdv2gestao.core.biometric

import android.os.Build
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_STRONG
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine

/**
 * Trava da interface (não protege a chave criptográfica).
 * Aceita biometria forte ou o bloqueio de tela do aparelho (PIN/padrão/senha).
 */
object BiometricLock {
    const val PROMPT_TITLE = "PDV2 Gestão"
    const val PROMPT_SUBTITLE = "Desbloqueie para ver os dados da sua empresa"

    /** BIOMETRIC_STRONG | DEVICE_CREDENTIAL não é aceito no Android 9 e 10. */
    private fun authenticators(): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) BIOMETRIC_STRONG or DEVICE_CREDENTIAL
        else BIOMETRIC_WEAK or DEVICE_CREDENTIAL

    fun isAvailable(activity: FragmentActivity): Boolean =
        BiometricManager.from(activity).canAuthenticate(authenticators()) == BiometricManager.BIOMETRIC_SUCCESS

    /** true se desbloqueou; false se cancelou ou falhou. */
    suspend fun authenticate(activity: FragmentActivity): Boolean = suspendCancellableCoroutine { cont ->
        val executor = ContextCompat.getMainExecutor(activity)
        val prompt = BiometricPrompt(activity, executor, object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                if (cont.isActive) cont.resume(true)
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                if (cont.isActive) cont.resume(false)
            }
        })
        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(PROMPT_TITLE)
            .setSubtitle(PROMPT_SUBTITLE)
            .setAllowedAuthenticators(authenticators())
            .build()
        try {
            prompt.authenticate(info)
        } catch (e: Exception) {
            if (cont.isActive) cont.resume(false)
        }
        cont.invokeOnCancellation { runCatching { prompt.cancelAuthentication() } }
    }
}
