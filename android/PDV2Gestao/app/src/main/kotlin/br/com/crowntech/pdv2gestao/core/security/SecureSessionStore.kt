package br.com.crowntech.pdv2gestao.core.security

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import br.com.crowntech.pdv2gestao.core.model.AppJson
import br.com.crowntech.pdv2gestao.core.session.SessionStore
import br.com.crowntech.pdv2gestao.core.session.StoredSession
import kotlinx.coroutines.flow.first
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

private val Context.secureDataStore: DataStore<Preferences> by preferencesDataStore(name = "secure_session")

/**
 * Guarda {"refresh_token","device_id","api_base_url"} cifrado com AES-256-GCM.
 * A chave fica no Android Keystore (alias pdv2_session_key), sem exigir biometria:
 * a renovação em segundo plano continua funcionando; a trava biométrica é da interface.
 * Se a chave sumir ou der erro, apaga tudo e o app volta ao login.
 */
class SecureSessionStore(private val context: Context) : SessionStore {

    private val payloadKey = stringPreferencesKey("payload")
    private val ivKey = stringPreferencesKey("iv")

    override suspend fun load(): StoredSession? {
        val prefs = context.secureDataStore.data.first()
        val payload = prefs[payloadKey] ?: return null
        val iv = prefs[ivKey] ?: return null
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, existingKey() ?: throw IllegalStateException("sem chave"),
                GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)))
            val plain = cipher.doFinal(Base64.decode(payload, Base64.NO_WRAP))
            AppJson.decodeFromString(StoredSession.serializer(), plain.toString(Charsets.UTF_8))
        } catch (e: Exception) {
            clear()
            null
        }
    }

    override suspend fun save(session: StoredSession) {
        try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, existingKey() ?: createKey())
            val encrypted = cipher.doFinal(AppJson.encodeToString(StoredSession.serializer(), session).toByteArray(Charsets.UTF_8))
            val iv = Base64.encodeToString(cipher.iv, Base64.NO_WRAP)
            val payload = Base64.encodeToString(encrypted, Base64.NO_WRAP)
            context.secureDataStore.edit {
                it[payloadKey] = payload
                it[ivKey] = iv
            }
        } catch (e: Exception) {
            // Sem armazenamento seguro, não guarda nada: a sessão vale só enquanto o app estiver aberto.
            clear()
        }
    }

    override suspend fun clear() {
        context.secureDataStore.edit { it.clear() }
    }

    private fun keyStore(): KeyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }

    private fun existingKey(): SecretKey? =
        runCatching { (keyStore().getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.secretKey }.getOrNull()

    private fun createKey(): SecretKey {
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setUserAuthenticationRequired(false)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    companion object {
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "pdv2_session_key"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}
