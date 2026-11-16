package br.com.crowntech.pdv2gestao

import br.com.crowntech.pdv2gestao.core.api.DataScope
import br.com.crowntech.pdv2gestao.core.demo.DemoApi
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
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
 * Qualquer tentativa de gravar preferências ou sessão aqui estouraria (não há Context real),
 * então passar sem erro também prova que a demonstração não grava nada.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class DemoViewModelTest {
    private val dispatcher = StandardTestDispatcher()
    private lateinit var app: PDV2App
    private lateinit var vm: AppViewModel

    @Before fun setUp() {
        Dispatchers.setMain(dispatcher)
        app = PDV2App().apply { onCreate() }
        app.container.demo.demoFactory = { DemoApi(now = { Instant.parse("2026-11-16T17:40:00-03:00") }, latencyMillis = 0) }
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
    }
}
