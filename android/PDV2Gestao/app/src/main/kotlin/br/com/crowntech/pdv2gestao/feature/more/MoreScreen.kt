package br.com.crowntech.pdv2gestao.feature.more

import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material.icons.filled.CreditScore
import androidx.compose.material.icons.filled.Inventory2
import androidx.compose.material.icons.filled.PointOfSale
import androidx.compose.material.icons.filled.SyncProblem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.foundation.clickable
import androidx.compose.ui.semantics.Role
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import br.com.crowntech.pdv2gestao.AppViewModel
import br.com.crowntech.pdv2gestao.feature.home.PageScaffold
import br.com.crowntech.pdv2gestao.feature.home.Routes

private data class MoreItem(val route: String, val label: String, val icon: ImageVector)

/** Mais: (Estoque, só para plataforma) · Fiado · Terminais · Conflitos de sincronização · Conta. */
@Composable
fun MoreScreen(vm: AppViewModel, onOpen: (String) -> Unit) {
    val me by vm.me.collectAsStateWithLifecycle()
    val items = buildList {
        if (me?.isPlatform == true) add(MoreItem(Routes.STOCK, "Estoque", Icons.Filled.Inventory2))
        add(MoreItem(Routes.CREDIT, "Fiado", Icons.Filled.CreditScore))
        add(MoreItem(Routes.TERMINALS, "Terminais", Icons.Filled.PointOfSale))
        add(MoreItem(Routes.CONFLICTS, "Conflitos de sincronização", Icons.Filled.SyncProblem))
        add(MoreItem(Routes.ACCOUNT, "Conta", Icons.Filled.AccountCircle))
    }
    PageScaffold(title = "Mais") { padding ->
        LazyColumn(Modifier.padding(padding)) {
            items.forEach { item ->
                item(key = item.route) {
                    ListItem(
                        headlineContent = { Text(item.label) },
                        leadingContent = { Icon(item.icon, contentDescription = null) },
                        trailingContent = { Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, contentDescription = null) },
                        modifier = Modifier.clickable(role = Role.Button) { onOpen(item.route) },
                    )
                    HorizontalDivider()
                }
            }
        }
    }
}
