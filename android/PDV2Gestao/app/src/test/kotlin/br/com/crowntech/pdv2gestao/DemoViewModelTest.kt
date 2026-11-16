package br.com.crowntech.pdv2gestao

import br.com.crowntech.pdv2gestao.core.api.DataScope
import br.com.crowntech.pdv2gestao.core.demo.DemoApi
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.core.security.AppPrefs
import br.com.crowntech.pdv2gestao.core.security.SavedPrefs
import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.datetime.Instant
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Entrar e sair da demonstração pelo AppViewModel (JVM, android.jar de mentira).
 * As preferências são trocadas por [CountingPrefs], que conta cada gravação: os testes
 * conferem que a demonstração não grava nada no DataStore (o armazenamento seguro é
 * contado no DemoModeTest).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class DemoViewModelTest {
    private val dispatcher = StandardTestDispatcher()
    private lateinit var app: PDV2App
    private lateinit var vm: AppViewModel
    private lateinit var prefs: CountingPrefs

    /** Preferências em memória que contam toda gravação. */
    class CountingPrefs(context: Context, var saved: SavedPrefs = SavedPrefs()) : AppPrefs(context) {
        val writes = mutableListOf<String>()
        override suspend fun load(): SavedPrefs = saved
        override suspend fun saveContext(tenant: String?, name: String?, shop: String?, periodKey: String) { writes += "saveContext" }
        override suspend fun setBiometric(enabled: Boolean) { writes += "setBiometric" }
        override suspend fun markBiometricOffered() { writes += "markBiometricOffered" }
        override suspend fun setDevServer(url: String?) { writes += "setDevServer" }
        override suspend fun clearSession() { writes += "clearSession" }
    }

    private fun useClock(iso: String) {
        app.container.demo.demoFactory = { DemoApi(now = { Instant.parse(iso) }, latencyMillis = 0) }
    }

    @Before fun setUp() {
        Dispatchers.setMain(dispatcher)
        app = PDV2App().apply { onCreate() }
        prefs = CountingPrefs(app)
        app.container.prefs = prefs
        useClock("2026-11-16T17:40:00-03:00")
        vm = AppViewModel(app)
    }

    @After fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test fun enteringDemoOpensHomeWithBannerAndFictitiousCompany() = runTest(dispatcher) {
        assertFalse(showDemoBanner(Screen.Home, vm.demoActive.value))
        vm.enterDemo()
        advanceUntilIdle()
        assertTrue(vm.demoActive.value)
        assertEquals(Screen.Home, vm.screen.value)
        assertTrue("faixa de demonstração aparece", showDemoBanner(vm.screen.value, vm.demoActive.value))
        assertEquals("Mercadinho Exemplo", vm.context.value.tenantName)
        assertEquals(listOf("Loja Centro", "Loja Jardim"), vm.context.value.shops.map { it.name })
        assertTrue(vm.canPickTenant())
        assertFalse(app.container.session.hasSession)
        // As telas leem da API de demonstração, não da real.
        assertTrue(vm.api is DemoApi)
        val d = vm.api.dashboard(vm.context.value.scope()!!)
        assertTrue(d.sales!!.count > 0)
    }

    @Test fun switchingTenantShopAndPeriodWorksWithoutPersisting() = runTest(dispatcher) {
        vm.enterDemo()
        vm.chooseShop(vm.context.value.shops.last().id)
        vm.choosePeriod(PeriodKey.MONTH)
        advanceUntilIdle()
        assertEquals("Loja Jardim", vm.context.value.shopLabel)
        val scope: DataScope = vm.context.value.scope()!!
        assertEquals("month", scope.period)
        assertTrue(vm.api.dashboard(scope).sales!!.count > 0)

        vm.chooseTenant(vm.me.value!!.tenants.last())
        advanceUntilIdle()
        assertEquals("Empório Exemplo", vm.context.value.tenantName)
        assertNull(vm.context.value.shopId)
        assertTrue(vm.api.dashboard(vm.context.value.scope()!!).sales!!.count > 0)

        vm.setBiometric(true)
        advanceUntilIdle()
        assertFalse("biometria não liga na demonstração", vm.biometricEnabled.value)
        assertEquals("nenhuma gravação de preferências", emptyList<String>(), prefs.writes)
    }

    @Test fun exitingDemoClearsEverything() = runTest(dispatcher) {
        val before = vm.context.value
        vm.enterDemo()
        vm.chooseShop(vm.context.value.shops.first().id)
        vm.openTenantPicker()
        vm.requestTab("sales")
        advanceUntilIdle()

        vm.exitDemo()
        advanceUntilIdle()
        assertFalse(vm.demoActive.value)
        assertEquals(Screen.Login(), vm.screen.value)
        assertFalse("faixa some", showDemoBanner(vm.screen.value, vm.demoActive.value))
        assertNull(vm.me.value)
        assertEquals(before, vm.context.value)
        assertFalse(vm.showTenantPicker.value)
        assertNull(vm.tabRequest.value)
        assertFalse(vm.biometricEnabled.value)
        assertFalse(app.container.session.hasSession)
        assertSame(app.container.api, vm.api)
        assertEquals("nenhuma gravação de preferências", emptyList<String>(), prefs.writes)
    }

    @Test fun logoutAndDisconnectInsideDemoOnlyLeaveTheDemo() = runTest(dispatcher) {
        vm.enterDemo()
        vm.logout()
        advanceUntilIdle()
        assertFalse(vm.demoActive.value)
        assertTrue(vm.screen.value is Screen.Login)

        vm.enterDemo()
        vm.localLogout(null)
        advanceUntilIdle()
        assertFalse(vm.demoActive.value)
        assertTrue(vm.screen.value is Screen.Login)
        assertEquals("nenhuma gravação de preferências", emptyList<String>(), prefs.writes)
    }

    @Test fun beforeStoresOpenTheDemoStartsOnYesterdayWithData() = runTest(dispatcher) {
        // 03:10 em Brasília: "Hoje" ainda não tem vendas; a demonstração abre em "Ontem", cheia.
        useClock("2026-11-17T03:10:00-03:00")
        vm.enterDemo()
        advanceUntilIdle()
        assertEquals(PeriodKey.YESTERDAY, vm.context.value.period)
        val d = vm.api.dashboard(vm.context.value.scope()!!)
        assertTrue("vendas de ontem", d.sales!!.count > 50)
        assertTrue("caixas fechados de ontem", d.cash!!.closedCount > 0)
        assertTrue("gráfico com o dia inteiro", d.series!!.points.size >= 10)
        vm.exitDemo()

        // Das 07:00 em diante abre em "Hoje".
        useClock("2026-11-17T07:00:00-03:00")
        vm.enterDemo()
        assertEquals(PeriodKey.TODAY, vm.context.value.period)
        assertEquals(emptyList<String>(), prefs.writes)
    }

    @Test fun exitingDemoRestoresTheBiometricChoiceFromPrefs() = runTest(dispatcher) {
        // Biometria ligada no DataStore, mas sem sessão restaurável (ex.: Keystore apagado).
        prefs.saved = SavedPrefs(biometricEnabled = true)
        vm.start()
        advanceUntilIdle()
        assertTrue(vm.screen.value is Screen.Login)
        assertTrue(vm.biometricEnabled.value)

        vm.enterDemo()
        assertFalse("biometria desligada dentro da demonstração", vm.biometricEnabled.value)
        vm.exitDemo()
        advanceUntilIdle()
        assertTrue("volta o valor gravado", vm.biometricEnabled.value)
        assertEquals(emptyList<String>(), prefs.writes)
    }

    @Test fun bannerShowsOnEveryScreenOfTheDemoExceptLogin() {
        listOf(Screen.Home, Screen.Offline("sem rede"), Screen.Locked(coldStart = false), Screen.AccessDenied, Screen.BiometricOffer)
            .forEach { assertTrue("faixa em $it", showDemoBanner(it, demoActive = true)) }
        assertFalse(showDemoBanner(Screen.Login(), demoActive = true))
        assertFalse(showDemoBanner(Screen.Home, demoActive = false))
    }
}
