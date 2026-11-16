package br.com.crowntech.pdv2gestao.core.security

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first

private val Context.prefsDataStore: DataStore<Preferences> by preferencesDataStore(name = "app_prefs")

/** Preferências que não são segredo: contexto escolhido, trava biométrica, servidor de desenvolvimento. */
data class SavedPrefs(
    val tenantId: String? = null,
    val tenantName: String? = null,
    val shopId: String? = null,
    val period: String? = null,
    val biometricEnabled: Boolean = false,
    val biometricOffered: Boolean = false,
    val devServer: String? = null,
)

/** `open` só para os testes contarem gravações (a demonstração não pode gravar nada). */
open class AppPrefs(private val context: Context) {
    private val tenantId = stringPreferencesKey("tenant_id")
    private val tenantName = stringPreferencesKey("tenant_name")
    private val shopId = stringPreferencesKey("shop_id")
    private val period = stringPreferencesKey("period")
    private val biometricEnabled = booleanPreferencesKey("biometric_enabled")
    private val biometricOffered = booleanPreferencesKey("biometric_offered")
    private val devServer = stringPreferencesKey("dev_server")

    open suspend fun load(): SavedPrefs {
        val p = context.prefsDataStore.data.first()
        return SavedPrefs(
            tenantId = p[tenantId],
            tenantName = p[tenantName],
            shopId = p[shopId],
            period = p[period],
            biometricEnabled = p[biometricEnabled] ?: false,
            biometricOffered = p[biometricOffered] ?: false,
            devServer = p[devServer],
        )
    }

    open suspend fun saveContext(tenant: String?, name: String?, shop: String?, periodKey: String) {
        context.prefsDataStore.edit {
            if (tenant != null) it[tenantId] = tenant else it.remove(tenantId)
            if (name != null) it[tenantName] = name else it.remove(tenantName)
            if (shop != null) it[shopId] = shop else it.remove(shopId)
            it[period] = periodKey
        }
    }

    open suspend fun setBiometric(enabled: Boolean) {
        context.prefsDataStore.edit {
            it[biometricEnabled] = enabled
            it[biometricOffered] = true
        }
    }

    open suspend fun markBiometricOffered() {
        context.prefsDataStore.edit { it[biometricOffered] = true }
    }

    open suspend fun setDevServer(url: String?) {
        context.prefsDataStore.edit { if (url != null) it[devServer] = url else it.remove(devServer) }
    }

    /** Ao sair: esquece o contexto e a escolha de biometria (o próximo login pergunta de novo). */
    open suspend fun clearSession() {
        context.prefsDataStore.edit {
            it.remove(tenantId); it.remove(tenantName); it.remove(shopId); it.remove(period)
            it.remove(biometricEnabled); it.remove(biometricOffered)
        }
    }
}
