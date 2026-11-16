package br.com.crowntech.pdv2gestao.core.format

import kotlinx.datetime.Clock
import kotlinx.datetime.Instant
import kotlinx.datetime.LocalDate
import kotlinx.datetime.LocalDateTime
import kotlinx.datetime.TimeZone
import kotlinx.datetime.toLocalDateTime
import kotlin.math.abs

/** Formatação em pt-BR, independente do idioma do aparelho (CONTRATO §11). */
object Format {
    val zone: TimeZone = TimeZone.of("America/Sao_Paulo")

    /** R$ 1.234,56 — negativo como -R$ 15,00. */
    fun money(cents: Long): String {
        val negative = cents < 0
        val absolute = abs(cents)
        val reais = absolute / 100
        val centavos = absolute % 100
        val text = "R$ " + groupThousands(reais) + "," + centavos.toString().padStart(2, '0')
        return if (negative) "-$text" else text
    }

    fun money(cents: Long?): String = if (cents == null) "—" else money(cents)

    private fun groupThousands(value: Long): String {
        val digits = value.toString()
        val sb = StringBuilder()
        digits.forEachIndexed { index, c ->
            if (index > 0 && (digits.length - index) % 3 == 0) sb.append('.')
            sb.append(c)
        }
        return sb.toString()
    }

    /** UN como inteiro ("12 un"); KG/L com 3 casas e vírgula ("1,250 kg"). */
    fun quantity(milli: Long, unit: String): String {
        val u = unit.uppercase()
        val negative = milli < 0
        val a = abs(milli)
        val sign = if (negative) "-" else ""
        val whole = groupThousands(a / 1000)
        val frac = (a % 1000).toString().padStart(3, '0')
        return when (u) {
            "UN" -> if (a % 1000 == 0L) "$sign$whole un" else "$sign$whole,$frac un"
            "KG" -> "$sign$whole,$frac kg"
            "L" -> "$sign$whole,$frac L"
            else -> "$sign$whole,$frac ${unit.lowercase()}"
        }
    }

    fun parseInstant(iso: String?): Instant? {
        if (iso.isNullOrBlank()) return null
        return runCatching { Instant.parse(iso) }.getOrNull()
    }

    private fun local(iso: String?): LocalDateTime? = parseInstant(iso)?.toLocalDateTime(zone)

    private fun two(n: Int) = n.toString().padStart(2, '0')

    /** 16/11/2026 14:03 */
    fun dateTime(iso: String?): String {
        val d = local(iso) ?: return "—"
        return "${two(d.dayOfMonth)}/${two(d.monthNumber)}/${d.year} ${two(d.hour)}:${two(d.minute)}"
    }

    /** 14:03 */
    fun time(iso: String?): String {
        val d = local(iso) ?: return "—"
        return "${two(d.hour)}:${two(d.minute)}"
    }

    /** "2026-11-16" → 16/11/2026 */
    fun date(isoDate: String?): String {
        if (isoDate.isNullOrBlank()) return "—"
        val d = runCatching { LocalDate.parse(isoDate) }.getOrNull() ?: return "—"
        return "${two(d.dayOfMonth)}/${two(d.monthNumber)}/${d.year}"
    }

    /** "há 3 min", "há 2 h", "há 4 dias", "agora mesmo". */
    fun relative(iso: String?, now: Instant = Clock.System.now()): String {
        val then = parseInstant(iso) ?: return "—"
        return relative(then, now)
    }

    fun relative(then: Instant, now: Instant = Clock.System.now()): String {
        val seconds = (now - then).inWholeSeconds
        return when {
            seconds < 60 -> "agora mesmo"
            seconds < 3600 -> "há ${seconds / 60} min"
            seconds < 86_400 -> "há ${seconds / 3600} h"
            else -> {
                val days = seconds / 86_400
                if (days == 1L) "há 1 dia" else "há $days dias"
            }
        }
    }

    /** Hora ou data+hora, conforme o período (Vendas: hora em "Hoje"). */
    fun saleMoment(iso: String?, onlyTime: Boolean): String = if (onlyTime) time(iso) else dateTime(iso)
}

/** Rótulos fixos do contrato (§6, "Rótulos fixos"). */
object Labels {
    fun payment(method: String, name: String? = null): String = when (method) {
        "cash" -> "Dinheiro"
        "pix" -> "Pix"
        "card_credit" -> "Cartão de crédito"
        "card_debit" -> "Cartão de débito"
        "credit_account" -> "Fiado"
        "store_credit" -> "Crédito da loja"
        "card" -> "Cartão"
        else -> name?.takeIf { it.isNotBlank() } ?: "Outra forma"
    }

    fun saleStatus(status: String): String = when (status) {
        "completed" -> "Concluída"
        "cancelled" -> "Cancelada"
        "refunded" -> "Estornada"
        else -> status
    }

    fun refund(method: String): String = when (method) {
        "cash" -> "Dinheiro"
        "store_credit" -> "Crédito da loja"
        "account" -> "Abatido no fiado"
        "external" -> "Fora do PDV"
        else -> method
    }

    fun terminalStatus(status: String): String = when (status) {
        "online" -> "Online"
        "offline" -> "Sem contato"
        "unpaired" -> "Não pareado"
        else -> status
    }

    fun cashStatus(status: String): String = when (status) {
        "open" -> "Aberto"
        "closed" -> "Fechado"
        else -> status
    }

    /** Diferença de caixa: <0 "Faltou R$ X", >0 "Sobrou R$ X", 0 "Sem diferença", null "—". */
    fun cashDifference(cents: Long?): String = when {
        cents == null -> "—"
        cents < 0 -> "Faltou " + Format.money(-cents)
        cents > 0 -> "Sobrou " + Format.money(cents)
        else -> "Sem diferença"
    }

    fun movement(kind: String): String = when (kind) {
        "withdrawn" -> "Sangria"
        "supplied" -> "Suprimento"
        else -> kind
    }

    fun permission(key: String): String = when (key) {
        "sales" -> "Vendas"
        "cash" -> "Caixa"
        "stock" -> "Estoque"
        "credit" -> "Fiado"
        "terminals" -> "Terminais e conflitos"
        else -> key
    }

    fun accessStatus(status: String): String = when (status) {
        "active" -> "Ativo"
        "expired" -> "Expirado"
        "revoked" -> "Revogado"
        else -> status
    }

    fun conflictStatus(status: String): String = when (status) {
        "open" -> "Em aberto"
        "acknowledged" -> "Em análise"
        "resolved" -> "Resolvido"
        "ignored" -> "Ignorado"
        else -> status
    }
}

/** Períodos aceitos pela API (§2). */
enum class PeriodKey(val key: String, val label: String) {
    TODAY("today", "Hoje"),
    YESTERDAY("yesterday", "Ontem"),
    SEVEN_DAYS("7d", "7 dias"),
    MONTH("month", "Mês");

    companion object {
        fun from(key: String?): PeriodKey = entries.firstOrNull { it.key == key } ?: TODAY
    }
}
