package br.com.crowntech.pdv2gestao

import android.graphics.Bitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.SemanticsNodeInteraction
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasScrollToNodeAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import br.com.crowntech.pdv2gestao.core.demo.DemoApi
import br.com.crowntech.pdv2gestao.core.format.Format
import kotlinx.datetime.Clock
import kotlinx.datetime.LocalTime
import kotlinx.datetime.atTime
import kotlinx.datetime.toInstant
import kotlinx.datetime.toLocalDateTime
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * Capturas de tela da Play Store a partir do modo demonstração.
 * Só roda quando pedido:
 *   adb shell am instrument -w -e storeShots true -e class br.com.crowntech.pdv2gestao.StoreScreenshotsTest \
 *     br.com.crowntech.pdv2gestao.test/androidx.test.runner.AndroidJUnitRunner
 * As imagens ficam em /sdcard/Android/data/br.com.crowntech.pdv2gestao/files/store/.
 */
@RunWith(AndroidJUnit4::class)
class StoreScreenshotsTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    private fun tab(label: String): SemanticsNodeInteraction =
        compose.onNode(hasText(label) and SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Tab))

    private fun waitText(text: String) {
        compose.waitUntil(10_000) { compose.onAllNodes(hasText(text, substring = true)).fetchSemanticsNodes().isNotEmpty() }
        compose.waitForIdle()
    }

    private fun shot(dir: File, name: String) {
        compose.waitForIdle()
        Thread.sleep(400)
        val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
        File(dir, "$name.png").outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    @Test fun captureStoreScreens() {
        assumeTrue(InstrumentationRegistry.getArguments().getString("storeShots") == "true")
        val dir = File(compose.activity.getExternalFilesDir(null), "store").apply { mkdirs() }

        // Relógio fixo às 17:40 de hoje: dia cheio de vendas e caixas abertos.
        val today = Clock.System.now().toLocalDateTime(Format.zone).date
        val fixed = today.atTime(LocalTime(17, 40)).toInstant(Format.zone)
        (compose.activity.application as PDV2App).container.demo.demoFactory = { DemoApi(now = { fixed }, latencyMillis = 0) }

        waitText("Ver demonstração")
        compose.onNodeWithText("Ver demonstração").performScrollTo().performClick()

        waitText("Faturamento")
        shot(dir, "01_resumo")

        compose.onNode(hasScrollToNodeAction()).performScrollToNode(hasText("Faturamento por hora"))
        shot(dir, "02_resumo_pagamentos_grafico")

        tab("Vendas").performClick()
        waitText("Caixa 1")
        shot(dir, "03_vendas")

        compose.onAllNodes(hasClickAction() and hasText("R$", substring = true)).onFirst().performClick()
        waitText("Itens")
        shot(dir, "04_detalhe_venda")
        compose.activity.runOnUiThread { compose.activity.onBackPressedDispatcher.onBackPressed() }

        tab("Caixa").performClick()
        waitText("Aberto em")
        shot(dir, "05_caixas")

        tab("Estoque").performClick()
        waitText("Zerado")
        shot(dir, "06_estoque")
    }
}
