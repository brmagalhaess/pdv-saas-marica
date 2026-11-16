package br.com.crowntech.pdv2gestao

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import br.com.crowntech.pdv2gestao.core.api.ApiException
import br.com.crowntech.pdv2gestao.core.api.AppConfig
import br.com.crowntech.pdv2gestao.core.api.DataScope
import br.com.crowntech.pdv2gestao.core.api.ErrorAction
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.core.model.DeviceInfo
import br.com.crowntech.pdv2gestao.core.model.Me
import br.com.crowntech.pdv2gestao.core.model.MfaUser
import br.com.crowntech.pdv2gestao.core.model.ShopRef
import br.com.crowntech.pdv2gestao.core.model.TenantAccess
import br.com.crowntech.pdv2gestao.core.session.SessionEvent
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlin.time.Duration.Companion.minutes
import kotlin.time.TimeSource

/** Telas de nível superior (CONTRATO §11: S0–S4 e Início). */
sealed interface Screen {
    data object Starting : Screen
    /** Trava biométrica. [coldStart] = abertura a frio (depois continua a renovação). */
    data class Locked(val coldStart: Boolean) : Screen
    data class Login(val message: String? = null) : Screen
    data class Mfa(val token: String, val user: MfaUser?, val expiresAt: String?) : Screen
    data object AccessDenied : Screen
    data object BiometricOffer : Screen
    data object Home : Screen
}

/** Empresa, loja e período escolhidos (guardados no aparelho; não são segredo). */
data class DataContext(
    val tenantId: String? = null,
    val tenantName: String? = null,
    val shopId: String? = null,
    val shops: List<ShopRef> = emptyList(),
    val period: PeriodKey = PeriodKey.TODAY,
) {
    val shopName: String? get() = shops.firstOrNull { it.id == shopId }?.name

    fun scope(): DataScope? = tenantId?.let { DataScope(it, shopId, period.key) }
}

class AppViewModel(app: Application) : AndroidViewModel(app) {
    val container = (app as PDV2App).container
    val api get() = container.api
    private val session get() = container.session
    private val prefs get() = container.prefs

    private val _screen = MutableStateFlow<Screen>(Screen.Starting)
    val screen: StateFlow<Screen> = _screen.asStateFlow()

    private val _me = MutableStateFlow<Me?>(null)
    val me: StateFlow<Me?> = _me.asStateFlow()

    private val _context = MutableStateFlow(DataContext())
    val context: StateFlow<DataContext> = _context.asStateFlow()

    private val _biometricEnabled = MutableStateFlow(false)
    val biometricEnabled: StateFlow<Boolean> = _biometricEnabled.asStateFlow()

    /** Pede à interface para abrir o seletor de empresa. */
    private val _showTenantPicker = MutableStateFlow(false)
    val showTenantPicker: StateFlow<Boolean> = _showTenantPicker.asStateFlow()

    /** Pedido para trocar de aba (ex.: "Abrir empresa" na Plataforma → Resumo). */
    private val _tabRequest = MutableStateFlow<String?>(null)
    val tabRequest: StateFlow<String?> = _tabRequest.asStateFlow()

    private val _devServer = MutableStateFlow(AppConfig.defaultBaseUrl)
    val devServer: StateFlow<String> = _devServer.asStateFlow()

    private var started = false
    private var backgroundMark: TimeSource.Monotonic.ValueTimeMark? = null

    init {
        viewModelScope.launch {
            session.events.collect { event ->
                when (event) {
                    is SessionEvent.Ended -> resetToLogin(event.message)
                    SessionEvent.AccessDenied -> {
                        resetLocal()
                        _screen.value = Screen.AccessDenied
                    }
                }
            }
        }
    }

    // ---------- Abertura (S0) ----------

    fun start() {
        if (started) return
        started = true
        viewModelScope.launch {
            val saved = prefs.load()
            val base = if (AppConfig.allowServerOverride) {
                saved.devServer?.let { AppConfig.validateBaseUrl(it) } ?: AppConfig.defaultBaseUrl
            } else {
                AppConfig.defaultBaseUrl
            }
            _devServer.value = base
            _biometricEnabled.value = saved.biometricEnabled
            _context.value = DataContext(
                tenantId = saved.tenantId,
                tenantName = saved.tenantName,
                shopId = saved.shopId,
                period = PeriodKey.from(saved.period),
            )
            val hasSession = session.restore(base)
            if (!hasSession) {
                _screen.value = Screen.Login()
                return@launch
            }
            if (saved.biometricEnabled) {
                _screen.value = Screen.Locked(coldStart = true)
            } else {
                resumeSession()
            }
        }
    }

    /** Depois do desbloqueio (ou sem trava): auth/refresh → me → Início. Falhou → Login. */
    private suspend fun resumeSession() {
        _screen.value = Screen.Starting
        try {
            session.refreshNow()
            val me = api.me()
            applyMe(me)
            _screen.value = Screen.Home
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiException) {
            when (e.action) {
                ErrorAction.AccessDenied -> { resetLocal(); _screen.value = Screen.AccessDenied }
                ErrorAction.EndSession -> resetToLogin(e.userMessage)
                // Sem rede/servidor fora: vai ao Login com a explicação, mas guarda a sessão para a próxima abertura.
                else -> if (_screen.value !is Screen.Login) _screen.value = Screen.Login(e.userMessage)
            }
        }
    }

    fun unlocked(coldStart: Boolean) {
        if (coldStart) {
            viewModelScope.launch { resumeSession() }
        } else {
            _screen.value = Screen.Home
        }
    }

    fun onBackground() {
        backgroundMark = TimeSource.Monotonic.markNow()
    }

    /** Ao voltar do segundo plano após 5 min, pede a biometria de novo. */
    fun onForeground() {
        val mark = backgroundMark ?: return
        backgroundMark = null
        if (_biometricEnabled.value && _screen.value == Screen.Home && mark.elapsedNow() >= 5.minutes) {
            _screen.value = Screen.Locked(coldStart = false)
        }
    }

    // ---------- Login (S1/S2) ----------

    fun setDevServer(url: String): Boolean {
        val valid = AppConfig.validateBaseUrl(url) ?: return false
        _devServer.value = valid
        viewModelScope.launch { prefs.setDevServer(valid) }
        return true
    }

    private fun deviceInfo() = DeviceInfo(
        platform = "android",
        name = AppConfig.deviceName(),
        model = AppConfig.model(),
        osVersion = AppConfig.osVersion(),
        appVersion = AppConfig.appVersionLabel,
    )

    /** Retorna a mensagem de erro para a tela, ou null se avançou. */
    suspend fun login(email: String, password: String, biometricAvailable: Boolean): String? {
        session.useBaseUrl(_devServer.value)
        return try {
            val response = session.login(email, password, deviceInfo())
            when (response.status) {
                "ok" -> {
                    response.me?.let { applyMe(it) } ?: applyMe(api.me())
                    afterLogin(biometricAvailable)
                    null
                }
                "mfa_required" -> {
                    _screen.value = Screen.Mfa(response.mfaToken ?: "", response.user, response.mfaExpiresAt)
                    null
                }
                else -> ApiException.SERVER_ERROR_MESSAGE
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiException) {
            if (e.action == ErrorAction.AccessDenied) {
                resetLocal()
                _screen.value = Screen.AccessDenied
                null
            } else {
                e.userMessage
            }
        }
    }

    suspend fun verifyMfa(token: String, code: String?, recovery: String?, biometricAvailable: Boolean): String? {
        return try {
            val response = session.verifyMfa(token, code, recovery)
            if (response.status != "ok") return ApiException.SERVER_ERROR_MESSAGE
            response.me?.let { applyMe(it) } ?: applyMe(api.me())
            afterLogin(biometricAvailable)
            null
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiException) {
            when (e.code) {
                "mfa_expired" -> { resetToLogin(e.userMessage); null }
                "app_access_denied" -> { resetLocal(); _screen.value = Screen.AccessDenied; null }
                else -> e.userMessage
            }
        }
    }

    private suspend fun afterLogin(biometricAvailable: Boolean) {
        val saved = prefs.load()
        _screen.value = if (biometricAvailable && !saved.biometricOffered) Screen.BiometricOffer else Screen.Home
    }

    fun answerBiometricOffer(use: Boolean) {
        viewModelScope.launch {
            prefs.setBiometric(use)
            _biometricEnabled.value = use
            _screen.value = Screen.Home
        }
    }

    fun setBiometric(enabled: Boolean) {
        viewModelScope.launch {
            prefs.setBiometric(enabled)
            _biometricEnabled.value = enabled
        }
    }

    fun backToLogin() {
        viewModelScope.launch { resetToLogin(null) }
    }

    // ---------- Sessão ----------

    fun logout() {
        viewModelScope.launch {
            session.logout()
            resetToLogin(null)
        }
    }

    /** Depois de pedir exclusão ou desconectar este aparelho: só apaga localmente. */
    fun localLogout(message: String?) {
        viewModelScope.launch {
            session.clearLocal()
            resetToLogin(message)
        }
    }

    private suspend fun resetLocal() {
        session.clearLocal()
        prefs.clearSession()
        _me.value = null
        _context.value = DataContext()
        _biometricEnabled.value = false
        _showTenantPicker.value = false
    }

    private suspend fun resetToLogin(message: String?) {
        resetLocal()
        _screen.value = Screen.Login(message)
    }

    // ---------- Contexto (empresa / loja / período) ----------

    private fun applyMe(me: Me) {
        _me.value = me
        val current = _context.value
        if (me.isPlatform) {
            val access = me.tenants.firstOrNull { it.id == current.tenantId }
            _context.value = if (access != null) current.copy(tenantName = access.name, shops = access.shops) else current
        } else {
            val access = me.tenants.firstOrNull { it.id == current.tenantId } ?: me.tenants.firstOrNull()
            _context.value = if (access == null) {
                DataContext(period = current.period)
            } else {
                val shopOk = current.tenantId == access.id && access.shops.any { it.id == current.shopId }
                current.copy(
                    tenantId = access.id,
                    tenantName = access.name,
                    shops = access.shops,
                    shopId = if (shopOk) current.shopId else null,
                )
            }
        }
        persistContext()
    }

    fun reloadMe() {
        viewModelScope.launch {
            try {
                applyMe(api.me())
            } catch (e: CancellationException) {
                throw e
            } catch (_: ApiException) {
            }
        }
    }

    fun chooseTenant(access: TenantAccess) {
        _context.update { it.copy(tenantId = access.id, tenantName = access.name, shops = access.shops, shopId = null) }
        _showTenantPicker.value = false
        persistContext()
    }

    /** "Abrir empresa" na aba Plataforma: vira a empresa atual e vai ao Resumo. */
    fun openTenantFromPlatform(id: String, name: String) {
        _context.update { it.copy(tenantId = id, tenantName = name, shops = emptyList(), shopId = null) }
        persistContext()
        _tabRequest.value = "summary"
    }

    fun consumeTabRequest() {
        _tabRequest.value = null
    }

    fun requestTab(route: String) {
        _tabRequest.value = route
    }

    fun chooseShop(shopId: String?) {
        _context.update { it.copy(shopId = shopId) }
        persistContext()
    }

    fun choosePeriod(period: PeriodKey) {
        _context.update { it.copy(period = period) }
        persistContext()
    }

    /** Atualiza a lista de lojas com o que o servidor devolveu (dashboard.shops). */
    fun updateShops(tenantId: String, tenantName: String, shops: List<ShopRef>) {
        _context.update {
            if (it.tenantId != tenantId) it
            else it.copy(shops = shops, tenantName = tenantName, shopId = it.shopId?.takeIf { id -> shops.any { s -> s.id == id } })
        }
    }

    fun openTenantPicker() {
        _showTenantPicker.value = true
    }

    fun closeTenantPicker() {
        _showTenantPicker.value = false
    }

    /** O seletor de empresa só abre com mais de 1 empresa ou para a plataforma. */
    fun canPickTenant(): Boolean {
        val me = _me.value ?: return false
        return me.isPlatform || me.tenants.size > 1
    }

    private fun persistContext() {
        val c = _context.value
        viewModelScope.launch { prefs.saveContext(c.tenantId, c.tenantName, c.shopId, c.period.key) }
    }

    /** Reação padrão a erros nas telas de dados (§2.1). */
    fun handleDataError(e: ApiException) {
        when (e.action) {
            ErrorAction.ChooseTenant -> _showTenantPicker.value = true
            ErrorAction.ResetPeriod -> choosePeriod(PeriodKey.TODAY)
            ErrorAction.ResetShop -> chooseShop(null)
            ErrorAction.ReloadMeAndChooseTenant -> {
                _context.update { it.copy(tenantId = null, tenantName = null, shopId = null, shops = emptyList()) }
                persistContext()
                reloadMe()
                _showTenantPicker.value = true
            }
            else -> Unit
        }
    }
}
