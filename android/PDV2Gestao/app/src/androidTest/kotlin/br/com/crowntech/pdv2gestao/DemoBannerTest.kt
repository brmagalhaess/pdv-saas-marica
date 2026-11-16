package br.com.crowntech.pdv2gestao

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import br.com.crowntech.pdv2gestao.ui.common.DEMO_BANNER_TAG
import br.com.crowntech.pdv2gestao.ui.common.DEMO_BANNER_TEXT
import br.com.crowntech.pdv2gestao.ui.common.DEMO_EXIT_LABEL
import org.junit.Assert.assertFalse
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** No aparelho: "Ver demonstração" mostra a faixa e "Sair da demonstração" volta à tela Entrar. */
@RunWith(AndroidJUnit4::class)
class DemoBannerTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    @Test fun bannerAppearsInDemoAndExitGoesBackToLogin() {
        compose.waitUntil(10_000) { compose.onAllNodes(hasText("Ver demonstração")).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag(DEMO_BANNER_TAG).assertDoesNotExist()

        compose.onNodeWithText("Ver demonstração").performScrollTo().performClick()
        compose.onNodeWithTag(DEMO_BANNER_TAG).assertIsDisplayed()
        compose.onNodeWithText(DEMO_BANNER_TEXT).assertIsDisplayed()
        compose.waitUntil(10_000) { compose.onAllNodes(hasText("Faturamento")).fetchSemanticsNodes().isNotEmpty() }

        compose.onNodeWithText(DEMO_EXIT_LABEL).performClick()
        compose.waitForIdle()
        compose.onNodeWithTag(DEMO_BANNER_TAG).assertDoesNotExist()
        compose.onNodeWithText("Ver demonstração").assertExists()
        val app = compose.activity.application as PDV2App
        assertFalse(app.container.demo.active.value)
        assertFalse(app.container.session.hasSession)
    }
}
