package br.com.crowntech.pdv2gestao

import br.com.crowntech.pdv2gestao.core.format.Format
import br.com.crowntech.pdv2gestao.core.format.Labels
import br.com.crowntech.pdv2gestao.core.format.PeriodKey
import br.com.crowntech.pdv2gestao.feature.mfa.normalizeRecoveryCode
import br.com.crowntech.pdv2gestao.feature.platform.pickerMillisToDate
import br.com.crowntech.pdv2gestao.feature.platform.validateGrant
import br.com.crowntech.pdv2gestao.feature.summary.axisLabels
import kotlinx.datetime.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class FormatTest {

    @Test fun money() {
        assertEquals("R$ 0,00", Format.money(0))
        assertEquals("R$ 0,05", Format.money(5))
        assertEquals("R$ 45,90", Format.money(4590))
        assertEquals("R$ 1.234,56", Format.money(123456))
        assertEquals("R$ 3.520,00", Format.money(352000))
        assertEquals("R$ 1.234.567,89", Format.money(123456789))
        assertEquals("-R$ 15,00", Format.money(-1500))
        assertEquals("—", Format.money(null as Long?))
    }

    @Test fun quantity() {
        assertEquals("12 un", Format.quantity(12000, "UN"))
        assertEquals("0 un", Format.quantity(0, "UN"))
        assertEquals("1.200 un", Format.quantity(1_200_000, "UN"))
        assertEquals("1,250 kg", Format.quantity(1250, "KG"))
        assertEquals("0,500 L", Format.quantity(500, "L"))
        assertEquals("-2 un", Format.quantity(-2000, "UN"))
    }

    @Test fun dateTimesUseSaoPaulo() {
        assertEquals("16/11/2026 14:03", Format.dateTime("2026-11-16T14:03:12-03:00"))
        // Mesmo instante em UTC continua mostrando a hora de Brasília.
        assertEquals("16/11/2026 14:03", Format.dateTime("2026-11-16T17:03:12Z"))
        assertEquals("14:03", Format.time("2026-11-16T14:03:12-03:00"))
        assertEquals("16/11/2026", Format.date("2026-11-16"))
        assertEquals("—", Format.dateTime(null))
        assertEquals("—", Format.dateTime("lixo"))
        assertEquals("—", Format.date(""))
    }

    @Test fun relative() {
        val now = Instant.parse("2026-11-16T14:10:00-03:00")
        assertEquals("agora mesmo", Format.relative("2026-11-16T14:09:30-03:00", now))
        assertEquals("há 3 min", Format.relative("2026-11-16T14:07:00-03:00", now))
        assertEquals("há 2 h", Format.relative("2026-11-16T12:00:00-03:00", now))
        assertEquals("há 1 dia", Format.relative("2026-11-15T12:00:00-03:00", now))
        assertEquals("há 4 dias", Format.relative("2026-11-12T12:00:00-03:00", now))
    }

    @Test fun labels() {
        assertEquals("Dinheiro", Labels.payment("cash"))
        assertEquals("Cartão de crédito", Labels.payment("card_credit"))
        assertEquals("Fiado", Labels.payment("credit_account"))
        assertEquals("Vale refeição", Labels.payment("other", "Vale refeição"))
        assertEquals("Outra forma", Labels.payment("other"))
        assertEquals("Estornada", Labels.saleStatus("refunded"))
        assertEquals("Abatido no fiado", Labels.refund("account"))
        assertEquals("Sem contato", Labels.terminalStatus("offline"))
        assertEquals("Faltou R$ 15,00", Labels.cashDifference(-1500))
        assertEquals("Sobrou R$ 2,00", Labels.cashDifference(200))
        assertEquals("Sem diferença", Labels.cashDifference(0))
        assertEquals("—", Labels.cashDifference(null))
        assertEquals("Sangria", Labels.movement("withdrawn"))
        assertEquals("Terminais e conflitos", Labels.permission("terminals"))
        assertEquals("Em análise", Labels.conflictStatus("acknowledged"))
    }

    @Test fun periods() {
        assertEquals(listOf("Hoje", "Ontem", "7 dias", "Mês"), PeriodKey.entries.map { it.label })
        assertEquals(PeriodKey.SEVEN_DAYS, PeriodKey.from("7d"))
        assertEquals(PeriodKey.TODAY, PeriodKey.from("invalido"))
    }

    @Test fun recoveryCodeNormalization() {
        assertEquals("PDV2-ABCD-EFGH", normalizeRecoveryCode("pdv2 abcd efgh"))
        assertEquals("PDV2-ABCD-EFGH", normalizeRecoveryCode("ABCDEFGH"))
        assertEquals("PDV2-ABCD-EFGH", normalizeRecoveryCode("PDV2-ABCD-EFGH"))
    }

    @Test fun grantValidation() {
        assertNotNull(validateGrant("tenant", null, 1, listOf("sales"), "motivo"))
        assertNotNull(validateGrant("tenant", "t", null, listOf("sales"), "motivo"))
        assertNotNull(validateGrant("tenant", "t", 1, emptyList(), "motivo"))
        assertNotNull(validateGrant("tenant", "t", 1, listOf("sales"), "ok"))
        assertNull(validateGrant("tenant", "t", 1, listOf("sales"), "Dona pediu"))
        assertNull(validateGrant("platform", null, 7, emptyList(), "Atendimento"))
    }

    @Test fun datePickerMillis() {
        assertEquals("2026-12-31", pickerMillisToDate(Instant.parse("2026-12-31T00:00:00Z").toEpochMilliseconds()).toString())
    }

    @Test fun chartAxis() {
        assertEquals(listOf("00h", "01h"), axisLabels(listOf("00h", "01h")))
        val hours = (0..23).map { it.toString().padStart(2, '0') + "h" }
        assertEquals(listOf("00h", "08h", "16h", "23h"), axisLabels(hours))
    }
}
