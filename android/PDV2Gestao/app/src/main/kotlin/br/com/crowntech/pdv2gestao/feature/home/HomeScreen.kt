package br.com.crowntech.pdv2gestao.feature.home

import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.BarChart
import androidx.compose.material.icons.filled.Business
import androidx.compose.material.icons.filled.Inventory2
import androidx.compose.material.icons.filled.MoreHoriz
import androidx.compose.material.icons.filled.Payments
import androidx.compose.material.icons.filled.ShoppingCart
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.feature.account.AccountScreen
import br.com.crowntech.pdv2gestao.feature.account.DeleteAccountScreen
import br.com.crowntech.pdv2gestao.feature.cash.CashDetailScreen
import br.com.crowntech.pdv2gestao.feature.cash.CashScreen
import br.com.crowntech.pdv2gestao.feature.conflicts.ConflictsScreen
import br.com.crowntech.pdv2gestao.feature.credit.CreditScreen
import br.com.crowntech.pdv2gestao.feature.more.MoreScreen
import br.com.crowntech.pdv2gestao.feature.platform.AccessGrantScreen
import br.com.crowntech.pdv2gestao.feature.platform.PlatformScreen
import br.com.crowntech.pdv2gestao.feature.sales.SaleDetailScreen
import br.com.crowntech.pdv2gestao.feature.sales.SalesScreen
import br.com.crowntech.pdv2gestao.feature.stock.StockScreen
import br.com.crowntech.pdv2gestao.feature.summary.SummaryScreen
import br.com.crowntech.pdv2gestao.feature.terminals.TerminalsScreen

/** Rotas do app. */
object Routes {
    const val PLATFORM = "platform"
    const val SUMMARY = "summary"
    const val SALES = "sales"
    const val CASH = "cash"
    const val STOCK = "stock"
    const val MORE = "more"
    const val SALE_DETAIL = "sale/{id}"
    const val CASH_DETAIL = "cash/{id}"
    const val CREDIT = "credit"
    const val TERMINALS = "terminals"
    const val CONFLICTS = "conflicts"
    const val ACCOUNT = "account"
    const val DELETE_ACCOUNT = "account/delete"
    const val GRANT = "platform/grant"

    fun sale(id: String) = "sale/$id"
    fun cash(id: String) = "cash/$id"
}

data class Tab(val route: String, val label: String, val icon: ImageVector)

/** Empresa: Resumo | Vendas | Caixa | Estoque | Mais. Plataforma: Plataforma | Resumo | Vendas | Caixa | Mais. */
fun tabsFor(platform: Boolean): List<Tab> {
    val summary = Tab(Routes.SUMMARY, "Resumo", Icons.Filled.BarChart)
    val sales = Tab(Routes.SALES, "Vendas", Icons.Filled.ShoppingCart)
    val cash = Tab(Routes.CASH, "Caixa", Icons.Filled.Payments)
    val stock = Tab(Routes.STOCK, "Estoque", Icons.Filled.Inventory2)
    val more = Tab(Routes.MORE, "Mais", Icons.Filled.MoreHoriz)
    return if (platform) listOf(Tab(Routes.PLATFORM, "Plataforma", Icons.Filled.Business), summary, sales, cash, more)
    else listOf(summary, sales, cash, stock, more)
}

private fun NavHostController.goToTab(route: String) {
    navigate(route) {
        popUpTo(graph.findStartDestination().id) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }
}

@Composable
fun HomeScreen(vm: AppViewModel) {
    val me by vm.me.collectAsStateWithLifecycle()
    val platform = me?.isPlatform == true
    val tabs = tabsFor(platform)
    val nav = rememberNavController()
    val backStack by nav.currentBackStackEntryAsState()
    val currentRoute = backStack?.destination?.route
    val tabRequest by vm.tabRequest.collectAsStateWithLifecycle()
    val showPicker by vm.showTenantPicker.collectAsStateWithLifecycle()

    LaunchedEffect(tabRequest) {
        val route = tabRequest ?: return@LaunchedEffect
        nav.goToTab(route)
        vm.consumeTabRequest()
    }

    Scaffold(
        contentWindowInsets = WindowInsets(0),
        bottomBar = {
            NavigationBar {
                tabs.forEach { tab ->
                    val selected = currentRoute == tab.route || belongsTo(currentRoute, tab.route, platform)
                    NavigationBarItem(
                        selected = selected,
                        onClick = { nav.goToTab(tab.route) },
                        icon = { Icon(tab.icon, contentDescription = null) },
                        label = { Text(tab.label, maxLines = 1) },
                    )
                }
            }
        },
    ) { padding ->
        NavHost(
            navController = nav,
            startDestination = if (platform) Routes.PLATFORM else Routes.SUMMARY,
            modifier = Modifier.padding(padding),
        ) {
            composable(Routes.PLATFORM) { PlatformScreen(vm, onGrant = { nav.navigate(Routes.GRANT) }) }
            composable(Routes.GRANT) { AccessGrantScreen(vm, onBack = { nav.popBackStack() }) }
            composable(Routes.SUMMARY) {
                SummaryScreen(vm, onOpen = { route -> if (route in tabs.map { it.route }) nav.goToTab(route) else nav.navigate(route) })
            }
            composable(Routes.SALES) { SalesScreen(vm, onOpen = { nav.navigate(Routes.sale(it)) }) }
            composable(Routes.SALE_DETAIL, arguments = listOf(navArgument("id") { type = NavType.StringType })) { entry ->
                SaleDetailScreen(vm, entry.arguments?.getString("id").orEmpty(), onBack = { nav.popBackStack() })
            }
            composable(Routes.CASH) { CashScreen(vm, onOpen = { nav.navigate(Routes.cash(it)) }) }
            composable(Routes.CASH_DETAIL, arguments = listOf(navArgument("id") { type = NavType.StringType })) { entry ->
                CashDetailScreen(vm, entry.arguments?.getString("id").orEmpty(), onBack = { nav.popBackStack() })
            }
            composable(Routes.STOCK) {
                StockScreen(vm, onBack = if (platform) ({ nav.popBackStack() }) else null)
            }
            composable(Routes.MORE) { MoreScreen(vm, onOpen = { nav.navigate(it) }) }
            composable(Routes.CREDIT) { CreditScreen(vm, onBack = { nav.popBackStack() }) }
            composable(Routes.TERMINALS) { TerminalsScreen(vm, onBack = { nav.popBackStack() }) }
            composable(Routes.CONFLICTS) { ConflictsScreen(vm, onBack = { nav.popBackStack() }) }
            composable(Routes.ACCOUNT) {
                AccountScreen(vm, onBack = { nav.popBackStack() }, onDelete = { nav.navigate(Routes.DELETE_ACCOUNT) })
            }
            composable(Routes.DELETE_ACCOUNT) { DeleteAccountScreen(vm, onBack = { nav.popBackStack() }) }
        }
    }

    if (showPicker) {
        TenantPickerSheet(vm, onDismiss = vm::closeTenantPicker)
    }
}

/** Telas internas marcam a aba de origem como selecionada. */
private fun belongsTo(route: String?, tab: String, platform: Boolean): Boolean = when (route) {
    Routes.SALE_DETAIL -> tab == Routes.SALES
    Routes.CASH_DETAIL -> tab == Routes.CASH
    Routes.GRANT -> tab == Routes.PLATFORM
    Routes.CREDIT, Routes.TERMINALS, Routes.CONFLICTS, Routes.ACCOUNT, Routes.DELETE_ACCOUNT -> tab == Routes.MORE
    Routes.STOCK -> platform && tab == Routes.MORE
    else -> false
}
