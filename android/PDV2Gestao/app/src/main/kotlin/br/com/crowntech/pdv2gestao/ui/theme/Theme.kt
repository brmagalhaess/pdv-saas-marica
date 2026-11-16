package br.com.crowntech.pdv2gestao.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/** Cores da marca (DesignSystem.swift do iOS). */
object Brand {
    val Blue = Color(0xFF146BD6)
    val BlueDeep = Color(0xFF0A4599)
    val Navy = Color(0xFF091F3D)
    val Mint = Color(0xFF2EBF94)
    val Danger = Color(0xFFC62828)
    val DangerDark = Color(0xFFFF8A80)
    val Warning = Color(0xFFB26A00)
    val WarningDark = Color(0xFFFFC266)
}

private val LightColors = lightColorScheme(
    primary = Brand.Blue,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFD7E6FB),
    onPrimaryContainer = Brand.Navy,
    secondary = Brand.Mint,
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFCFF3E6),
    onSecondaryContainer = Color(0xFF00382A),
    tertiary = Brand.BlueDeep,
    background = Color(0xFFF6F8FB),
    surface = Color(0xFFF6F8FB),
    surfaceContainer = Color.White,
    surfaceContainerLow = Color.White,
    surfaceContainerHigh = Color(0xFFEFF3F8),
    error = Brand.Danger,
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF7DB3FF),
    onPrimary = Brand.Navy,
    primaryContainer = Brand.BlueDeep,
    onPrimaryContainer = Color(0xFFD7E6FB),
    secondary = Brand.Mint,
    onSecondary = Color(0xFF00382A),
    secondaryContainer = Color(0xFF00513D),
    onSecondaryContainer = Color(0xFFCFF3E6),
    tertiary = Color(0xFF9CC3FF),
    background = Color(0xFF06152A),
    surface = Color(0xFF06152A),
    surfaceContainer = Brand.Navy,
    surfaceContainerLow = Brand.Navy,
    surfaceContainerHigh = Color(0xFF12305A),
    error = Brand.DangerDark,
)

/** dynamicColor = false: o app sempre usa as cores da marca, no claro e no escuro. */
@Composable
fun PDV2Theme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (darkTheme) DarkColors else LightColors, content = content)
}

@Composable
fun dangerColor(): Color = MaterialTheme.colorScheme.error

@Composable
fun warningColor(): Color = if (isSystemInDarkTheme()) Brand.WarningDark else Brand.Warning

@Composable
fun successColor(): Color = if (isSystemInDarkTheme()) Brand.Mint else Color(0xFF117A5B)
